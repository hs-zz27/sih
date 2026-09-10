"""FastAPI application.

Every route returns data shaped exactly like ``src/contracts``. Response models
have not changed since Step 0 - M7 swapped the bodies underneath them, so the
interface layer needed no edits.

Status:

* **M-side routes are live.** ``/api/tools``, ``/api/documents``, ``/api/search``,
  ``/api/index`` and the ``/api/tasks`` family run the real engine.
* **H-side routes are still stubs**: ingestion extraction (H1) and the audit /
  network monitor (H4). They stay marked ``# STUB (H<n>)`` so the remaining
  fakes are greppable: ``grep -rn "STUB (" src/``.
* No hardcoded hosts or paths - read them from ``src.config``.

Run it with:  ``uvicorn src.api.main:app --reload``
"""

from __future__ import annotations

import asyncio
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from src import config
from src.api import fakes
from src.contracts import (
    AgentResult,
    AuditEvent,
    AuditEventType,
    Deliverable,
    Document,
    IngestResult,
    NetworkStatus,
    SourceCitation,
    TaskStatus,
    StreamEvent,
    TaskRequest,
    TaskType,
    ToolSpec,
    new_id,
    utcnow,
)
from src.core import demo
from src.core.orchestrator import Orchestrator, get_orchestrator
from src.core.router import RoutingDecision
from src.io import audit, netguard
from src.io.ingest import ingest_file
from src.io.netmonitor import get_monitor


def engine() -> Orchestrator:
    """The core engine (M7).

    Resolved per request rather than at import so that starting the API never
    depends on a model server being up: an unreachable Ollama surfaces as a
    FAILED result with an explanation, not as a process that will not boot.
    """
    return get_orchestrator()

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Build the engine at boot instead of during the first request.

    Constructing the orchestrator loads the embedding model from disk, which
    costs a few seconds. Paying that on the judges' first query looks exactly
    like a slow agent, so it is paid here. Failures are reported and swallowed:
    a workbench that cannot pre-warm must still start and explain itself through
    /api/health.
    """
    # H4: enforcement first, then corroboration. Installing the guard before
    # anything else touches the network means even the engine pre-warm below is
    # already covered by it.
    netguard.install()
    get_monitor().start()

    try:
        await asyncio.to_thread(engine().health)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] pre-warm skipped: {exc.__class__.__name__}: {exc}")
    yield

    get_monitor().stop()


app = FastAPI(
    lifespan=lifespan,
    title=config.get("app.name", "Sovereign AI Workbench"),
    version="0.2.0-core-engine",
    description=(
        "Local-only agentic workbench. Every route is served from localhost; "
        "the API makes no outbound calls."
    ),
)

# Dev-only: the UI runs on its own port until it is served from this origin for
# the demo. Origins come from config.yaml so no host is written down twice.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.get("server.cors_origins", []),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mirror of the orchestrator's run registry, kept so a task submitted before an
# engine reload is still resolvable by the UI. The orchestrator is authoritative.
_TASKS: dict[str, AgentResult] = {}


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness plus engine readiness.

    M7 adds the engine block: whether the local inference server is reachable,
    which configured models it actually has pulled, and the state of the index.
    Checking this before a rehearsal is cheaper than discovering mid-demo that
    a model was never pulled.
    """
    return {
        "status": "ok",
        "version": app.version,
        "time": utcnow().isoformat(),
        "engine": engine().health(),
    }


@app.get("/api/config")
def public_config() -> dict[str, Any]:
    """Safe subset of config.yaml for the UI (routing badge, step cap, limits).

    Deliberately does not expose filesystem paths.
    """
    return {
        "app_name": config.get("app.name"),
        "models": config.get("models", {}),
        "inference_endpoint": config.get("inference.endpoint"),
        "max_steps": config.get("agent.max_steps"),
        "task_types": [task_type.value for task_type in TaskType],
        "allowed_hosts": config.get("audit.allowed_hosts", []),
    }


@app.get("/api/tools", response_model=list[ToolSpec])
def list_tools() -> list[ToolSpec]:
    """Tool registry as the UI should display it.

    M3: the live registry. Names and schemas here are the ones the agent is
    actually offered in its prompt, so what the UI lists cannot drift from what
    the model can call.
    """
    return engine().tool_specs()


@app.get("/api/demo")
def demo_status() -> dict[str, Any]:
    """Rehearsed presets and exactly which demo shortcuts are active.

    Deliberately exposed rather than hidden: the UI shows it, and a judge asking
    "what is pinned here?" gets a straight answer from the running system rather
    than from a slide. Everything listed also appears in HARDCODED.md.
    """
    return demo.status()


@app.post("/api/demo/record/{preset_id}")
def record_demo_run(preset_id: str) -> dict[str, Any]:
    """Run a preset live and cache the result as the fallback for that preset.

    Run this during rehearsal on the demo machine. A cache recorded anywhere
    else, or against a different model, is worse than none.
    """
    item = demo.preset(preset_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No demo preset {preset_id!r}")

    result = engine().run(TaskRequest(task=item.task, task_type_hint=item.task_type_hint))
    if result.status is TaskStatus.FAILED:
        raise HTTPException(
            status_code=503,
            detail=f"Refusing to cache a failed run: {result.error}",
        )

    path = demo.record(preset_id, result)
    return {
        "preset": preset_id,
        "cached_to": str(path),
        "steps": len(result.steps),
        "status": result.status.value,
    }


# ---------------------------------------------------------------------------
# H1 - ingestion
# ---------------------------------------------------------------------------


@app.post("/api/ingest", response_model=IngestResult)
async def ingest(file: UploadFile) -> IngestResult:
    """Accept a PDF or image upload, extract it for real, and index it.

    H1: PyMuPDF text-layer extraction with a Tesseract OCR fallback
    (``src/io/ingest.py``), so a born-digital PDF and a degraded scan both come
    back as page-level ``Document``s. Newly extracted documents are pushed into
    the live retrieval index immediately, so an uploaded report is searchable
    (and citable) in the same run that uploaded it.
    """
    uploads = config.get_path("app.uploads_dir")
    filename = Path(file.filename or "upload.bin").name  # strip any client path
    destination = uploads / f"{new_id('up')}_{filename}"

    started = time.monotonic()
    with destination.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)

    try:
        result = ingest_file(destination)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ingest_file() reports the on-disk name (id-prefixed, to avoid collisions);
    # the API contract's `filename` is the user-facing original name.
    result.filename = filename
    result.duration_ms = int((time.monotonic() - started) * 1000)

    if result.documents:
        engine().index_documents(result.documents)

    audit.emit(
        AuditEventType.INGEST,
        f"Ingested {filename} - {result.page_count} page(s), {result.ocr_pages} via OCR",
        actor="ui",
        source_path=result.source_path,
        pages=result.page_count,
        ocr_pages=result.ocr_pages,
    )

    return result


@app.get("/api/documents", response_model=list[Document])
def list_documents() -> list[Document]:
    """Indexed corpus, for the source-attribution browser.

    M6: served from the live local index. Empty until a corpus is indexed via
    POST /api/index or pushed in by the ingestion pipeline.
    """
    return engine().list_documents()


@app.post("/api/index")
def reindex_corpus() -> dict[str, Any]:
    """Re-index the text corpus directory (M6).

    Exposed so the demo can be reset between rehearsals without restarting the
    process, and so H5's corpus can be loaded once it exists.
    """
    orchestrator = engine()
    chunks = orchestrator.index_corpus()
    return {"indexed_chunks": chunks, **orchestrator.index.stats()}


@app.get("/api/search", response_model=list[SourceCitation])
def search_corpus(query: str, top_k: int | None = None) -> list[SourceCitation]:
    """Direct retrieval, for the source-attribution browser and for debugging."""
    return engine().search(query, top_k)


# ---------------------------------------------------------------------------
# M2/M4 - tasks
# ---------------------------------------------------------------------------


@app.post("/api/tasks", response_model=AgentResult)
def create_task(request: TaskRequest, background: bool = False) -> AgentResult:
    """Run a task through the real agent loop (M7).

    ``background=false`` (default) runs to completion and returns the finished
    result - the simple path, unchanged from Step 0.

    ``background=true`` returns a RUNNING result immediately and executes on a
    worker thread, so ``/api/tasks/{id}/stream`` delivers the trace live as the
    agent works. That is the mode the demo UI should use: a real run takes tens
    of seconds, and a spinner for all of it wastes the most persuasive thing we
    have to show.
    """
    orchestrator = engine()
    result = orchestrator.start(request) if background else orchestrator.run(request)
    _TASKS[result.task_id] = result
    return result


@app.get("/api/tasks/{task_id}", response_model=AgentResult)
def get_task(task_id: str) -> AgentResult:
    result = engine().get_result(task_id) or _TASKS.get(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown task {task_id}")
    return result


@app.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: str) -> StreamingResponse:
    """Server-sent events for the live step trace - H3's visual centrepiece.

    Event names: ``routing`` | ``step`` | ``result`` | ``done``. Shapes unchanged
    from Step 0.

    M7: drains the real agent loop. For a task started with ``background=true``
    the steps arrive here as the agent produces them; for one already finished,
    the stored trace is replayed so the endpoint behaves the same either way.
    """
    orchestrator = engine()
    known = orchestrator.get_result(task_id) or _TASKS.get(task_id)
    if known is None:
        raise HTTPException(status_code=404, detail=f"Unknown task {task_id}")

    async def event_stream() -> AsyncIterator[str]:
        def encode(event: StreamEvent) -> str:
            return f"event: {event.event}\ndata: {event.model_dump_json()}\n\n"

        # The agent loop is synchronous, so each pull is awaited on a worker
        # thread. Pulling one item at a time (rather than draining the whole
        # iterator) is what keeps the trace live: a step reaches the browser the
        # moment the agent finishes it, and a slow model call never blocks the
        # other requests the UI is making.
        iterator = orchestrator.events(task_id)
        sentinel = object()
        final: AgentResult | None = None
        routed = False

        while True:
            item = await asyncio.to_thread(next, iterator, sentinel)
            if item is sentinel:
                break

            if isinstance(item, AgentResult):
                final = item
            elif isinstance(item, RoutingDecision):
                # Emitted by the agent rather than guessed here: for a background
                # run the task type is not known until the agent has routed, and
                # the badge must name the model that actually ran.
                routed = True
                yield encode(
                    StreamEvent(event="routing", task_id=task_id, payload=item.as_step_metadata())
                )
            else:
                yield encode(
                    StreamEvent(event="step", task_id=task_id, payload=item.model_dump(mode="json"))
                )

        final = final or orchestrator.get_result(task_id) or known

        if not routed:
            # Nothing streamed a decision (an empty replay). Fall back to the
            # stored result so the UI always receives a routing event.
            yield encode(
                StreamEvent(
                    event="routing",
                    task_id=task_id,
                    payload={
                        "task_type": final.task_type.value,
                        "model": final.model_used,
                        "reason": final.routing_reason,
                    },
                )
            )

        yield encode(
            StreamEvent(event="result", task_id=task_id, payload=final.model_dump(mode="json"))
        )
        yield encode(StreamEvent(event="done", task_id=task_id))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# H2 - deliverables
# ---------------------------------------------------------------------------


@app.get("/api/deliverables", response_model=list[Deliverable])
def list_deliverables() -> list[Deliverable]:
    """Everything currently sitting in the served downloads directory.

    This one is real: it lists the directory. The files in it are placeholders
    until H2 lands.
    """
    downloads = config.get_path("app.downloads_dir")
    fakes.fake_deliverables()  # STUB (H2): ensure the placeholders exist to list

    items: list[Deliverable] = []
    for path in sorted(downloads.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        items.append(
            Deliverable(
                filename=path.name,
                path=str(path),
                size_bytes=path.stat().st_size,
                download_url=f"/api/deliverables/{path.name}",
                title=path.stem,
            )
        )
    return items


@app.get("/api/deliverables/{filename}")
def download_deliverable(filename: str) -> FileResponse:
    """Serve one generated file.

    Real, including the traversal guard - the downloads directory is the only
    thing this API will ever hand out.
    """
    downloads = config.get_path("app.downloads_dir")
    candidate = (downloads / filename).resolve()

    if candidate.parent != downloads.resolve() or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"No deliverable named {filename!r}")

    return FileResponse(
        candidate,
        filename=candidate.name,
        media_type="application/octet-stream",
    )


# ---------------------------------------------------------------------------
# H4 - audit log and network monitor
# ---------------------------------------------------------------------------


@app.get("/api/audit", response_model=list[AuditEvent])
def audit_log(task_id: str | None = None, limit: int = 200) -> list[AuditEvent]:
    """Append-only action log - the real JSONL file, not fixtures.

    Every ingest, tool call, and network refusal recorded by the running
    process is here. ``limit`` returns the most recent N events.
    """
    return audit.read_events(task_id=task_id, limit=limit)


@app.get("/api/network", response_model=NetworkStatus)
def network_status() -> NetworkStatus:
    """The number the whole submission rests on: external calls.

    Backed by two independent layers, both real:

    * ``netguard`` - an in-process socket guard. Loopback passes; anything else
      is refused and logged before the exception is raised. Enforcement, not
      just observation.
    * ``netmonitor`` - an out-of-process ``lsof -i`` poll of our whole process
      tree, catching what socket-level patching cannot see (a subprocess
      opening a raw connection).

    ``external_calls`` counts blocked/observed attempts, not successes - the
    guard's whole point is that an attempt cannot become a success. A nonzero
    count here means "the guard caught something," which is itself evidence the
    guard is doing its job, not a failure of the sovereignty claim.
    """
    total_ops, external = audit.counts()
    monitor = get_monitor()
    violations = [event for event in audit.read_events() if event.external]

    return NetworkStatus(
        external_calls=external,
        total_operations=total_ops,
        local_calls=total_ops - external,
        offline_since=None,
        offline_duration_s=monitor.offline_duration_s,
        monitor=(
            f"netguard (in-process socket enforcement, installed={netguard.is_installed()}) "
            f"+ netmonitor (lsof -i poll every {monitor.interval_s}s, running={monitor.running})"
        ),
        allowed_hosts=netguard.allowed_hosts(),
        violations=violations,
    )


# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=config.get("server.host", "127.0.0.1"),
        port=int(config.get("server.port", 8000)),
    )
