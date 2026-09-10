"""M7 - the seam the API talks to.

One object that owns the router, the tool registry, the RAG index, the sandbox
and the LLM client, and exposes exactly what ``src/api/main.py`` needs. The API
never constructs an ``Agent`` itself, so the wiring can change without the
interface layer moving.

Construction is lazy and never touches the network: importing this module on a
machine with no model server running must succeed, so that the API still starts
and reports *why* inference is unavailable rather than failing to boot.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Iterator

from src import config
from src.contracts import (
    AgentResult,
    AgentStep,
    Document,
    SourceCitation,
    TaskRequest,
    TaskStatus,
    TaskType,
    ToolSpec,
    utcnow,
)
from src.core.agent import Agent
from src.core.llm import InferenceError, LLMClient, SovereigntyError
from src.core.rag import RagIndex, get_index
from src.core.router import Router, RoutingDecision
from src.core.sandbox import Sandbox, get_sandbox
from src.core.tools import ToolRegistry, build_registry


class Orchestrator:
    """Everything behind the API, assembled once per process."""

    name = "core"

    def __init__(
        self,
        client: LLMClient | None = None,
        index: RagIndex | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self._client_override = client
        self._client: LLMClient | None = client
        self.index = index or get_index()
        self.sandbox = sandbox or get_sandbox()
        self.registry: ToolRegistry = build_registry(index=self.index, sandbox=self.sandbox)
        # Shares the orchestrator's client, resolved lazily so building the
        # orchestrator never touches the network.
        self.router = Router(client_provider=lambda: self.client)
        self._results: dict[str, AgentResult] = {}
        self._queues: dict[str, "queue.Queue[RoutingDecision | AgentStep | AgentResult | None]"] = {}
        self._lock = threading.Lock()

    # -- lazy client ------------------------------------------------------

    @property
    def client(self) -> LLMClient:
        """Built on first use. Raises SovereigntyError if the endpoint is not local."""
        if self._client is None:
            self._client = LLMClient()
        return self._client

    def health(self) -> dict[str, Any]:
        """Inference reachability plus index state, for /api/health and the UI."""
        endpoint = config.get("inference.endpoint", "")
        payload: dict[str, Any] = {
            "engine": self.name,
            "inference_endpoint": endpoint,
            "api_style": config.get("inference.api_style", "ollama"),
            "routing_table": self.router.routing_table(),
            "tools": self.registry.names(),
            "index": self.index.stats(),
        }
        try:
            available, models = self.client.probe()
            payload["endpoint_is_local"] = True
            payload["inference_available"] = available
            payload["models_present"] = models
        except SovereigntyError as exc:
            payload.update({"endpoint_is_local": False, "inference_available": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            payload.update({"inference_available": False, "error": f"{exc.__class__.__name__}: {exc}"})

        payload["missing_models"] = self._missing_models(payload.get("models_present") or [])
        return payload

    def _missing_models(self, present: list[str]) -> list[str]:
        """Configured models the local server has not pulled.

        Worth surfacing loudly: 'model not found' at demo time looks identical to
        a broken agent from the audience's seat.
        """
        if not present:
            return []
        normalised = {name.split(":")[0] for name in present} | set(present)
        return sorted(
            {
                model
                for model in self.router.routing_table().values()
                if model not in present and model.split(":")[0] not in normalised
            }
        )

    # -- execution --------------------------------------------------------

    def _agent(self, request: TaskRequest) -> Agent:
        return Agent(registry=self.registry, router=self.router, client=self.client)

    def run(self, request: TaskRequest) -> AgentResult:
        """Run a task to completion. Never raises - failures come back as a result."""
        try:
            result = self._agent(request).run(request)
        except (SovereigntyError, InferenceError) as exc:
            result = self._failed(request, str(exc))
        except Exception as exc:  # noqa: BLE001 - the demo must not see a 500
            result = self._failed(request, f"{exc.__class__.__name__}: {exc}")

        self._results[result.task_id] = result
        return result

    def stream(self, request: TaskRequest) -> Iterator[RoutingDecision | AgentStep | AgentResult]:
        """Yield steps live, then the final result. Backs the SSE trace endpoint."""
        try:
            for item in self._agent(request).stream(request):
                if isinstance(item, AgentResult):
                    self._results[item.task_id] = item
                yield item
        except (SovereigntyError, InferenceError) as exc:
            result = self._failed(request, str(exc))
            self._results[result.task_id] = result
            yield result
        except Exception as exc:  # noqa: BLE001
            result = self._failed(request, f"{exc.__class__.__name__}: {exc}")
            self._results[result.task_id] = result
            yield result

    # -- background runs (live trace) -------------------------------------

    def start(self, request: TaskRequest) -> AgentResult:
        """Kick the run off on a worker thread and return immediately.

        This is what makes the step trace *live*. Running the task inside the
        POST and replaying it afterwards would show the judges a long spinner
        followed by an instant replay - the opposite of the point. Instead the
        POST returns a RUNNING result straight away and the SSE endpoint drains
        steps as the agent produces them.
        """
        # Deliberately does NOT route here. Routing can cost a model call, so
        # doing it on this thread would make "returns immediately" a lie, and
        # doing it here *and* in the agent would run it twice per task. The
        # agent yields the real decision as the stream's first item.
        pending = AgentResult(
            task_id=request.task_id,
            status=TaskStatus.RUNNING,
            task_type=request.task_type_hint or TaskType.GENERAL,
        )

        events: "queue.Queue[RoutingDecision | AgentStep | AgentResult | None]" = queue.Queue()
        with self._lock:
            self._queues[request.task_id] = events
            self._results[request.task_id] = pending

        def worker() -> None:
            try:
                for item in self.stream(request):
                    events.put(item)
            finally:
                events.put(None)  # sentinel: the consumer stops here

        threading.Thread(target=worker, name=f"agent-{request.task_id}", daemon=True).start()
        return pending

    def events(
        self, task_id: str, timeout_s: float | None = None
    ) -> Iterator[RoutingDecision | AgentStep | AgentResult]:
        """Drain a background run's steps as they arrive.

        Falls back to replaying a finished result, so the endpoint behaves the
        same whether the UI connects mid-run, after it, or not at all.
        """
        with self._lock:
            events = self._queues.get(task_id)

        if events is None:
            yield from self._replay(task_id)
            return

        limit = timeout_s if timeout_s is not None else float(config.get("agent.wall_clock_timeout_s", 180)) + 30
        try:
            while True:
                try:
                    item = events.get(timeout=limit)
                except queue.Empty:
                    return
                if item is None:
                    break
                yield item
        finally:
            # Runs on normal completion AND when the consumer abandons the
            # generator (browser tab closed mid-run), so an undrained queue
            # cannot pin a whole run's steps in memory for the session.
            with self._lock:
                self._queues.pop(task_id, None)

    def _replay(self, task_id: str) -> Iterator[RoutingDecision | AgentStep | AgentResult]:
        """Re-emit a finished run in the same order a live one arrives in."""
        stored = self._results.get(task_id)
        if stored is None:
            return
        yield RoutingDecision(
            task_type=stored.task_type,
            model=stored.model_used,
            reason=stored.routing_reason,
            method="replay",
        )
        yield from stored.steps
        yield stored

    def _failed(self, request: TaskRequest, error: str) -> AgentResult:
        """A failure the UI can render as a result rather than a dead spinner."""
        decision = self.router.route(request.task, request.task_type_hint)
        return AgentResult(
            task_id=request.task_id,
            status=TaskStatus.FAILED,
            final_text=(
                "The task could not be run because the local inference server was not reachable.\n\n"
                f"{error}\n\n"
                "Start it with `ollama serve` and confirm the model is pulled "
                f"(`ollama pull {decision.model}`)."
            ),
            steps=[],
            task_type=decision.task_type,
            model_used=decision.model,
            routing_reason=decision.reason,
            error=error,
            completed_at=utcnow(),
        )

    # -- lookups ----------------------------------------------------------

    def get_result(self, task_id: str) -> AgentResult | None:
        return self._results.get(task_id)

    def tool_specs(self) -> list[ToolSpec]:
        return self.registry.specs()

    def list_documents(self) -> list[Document]:
        return self.index.list_documents()

    def search(self, query: str, top_k: int | None = None) -> list[SourceCitation]:
        return self.index.search_citations(query, top_k)

    def index_documents(self, documents: list[Document], replace: bool = False) -> int:
        """Entry point for the ingestion pipeline (H1) to push pages into RAG."""
        return self.index.index_documents(documents, replace=replace)

    def index_corpus(self) -> int:
        """Re-index the text corpus directory."""
        return self.index.index_corpus_dir()


# Alias: the name the API imports. Keeping both means renaming the class later
# does not touch the interface layer.
CoreEngine = Orchestrator

_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


def reset_orchestrator() -> None:
    global _orchestrator
    _orchestrator = None
