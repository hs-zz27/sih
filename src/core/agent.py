"""M4 - the agent loop.

plan -> select tool -> execute -> observe -> repeat -> finalize.

Three things here exist purely because this code runs in front of judges:

* **A hard step cap and a wall-clock timeout.** A runaway loop mid-demo is
  fatal, so both are enforced by the loop itself rather than trusted to the
  model. As the cap approaches the model is told how many turns remain, so a run
  converges instead of being guillotined.
* **Partial results.** Hitting the cap or the timeout still returns every step
  taken plus the best answer available, marked ``TRUNCATED``. A truncated run
  that shows its work beats a spinner.
* **Every iteration emits an AgentStep**, including the failures. The step trace
  is the visual centrepiece; a step the UI cannot render is a hole on screen.

The loop yields steps as it goes, so the SSE endpoint can stream them live.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Iterator

from src import config
from src.contracts import (
    AgentResult,
    AgentStep,
    SourceCitation,
    StepStatus,
    TaskRequest,
    TaskStatus,
    TaskType,
    utcnow,
)
from src.core import prompts
from src.core.llm import Completion, InferenceError, LLMClient, Message
from src.core.router import Router, RoutingDecision
from src.core.tools import ToolRegistry

# Tool output longer than this is truncated before it re-enters the prompt.
# Without it, one big file read blows the context window and every later step
# degrades for reasons that are invisible in the trace.
_MAX_OBSERVATION_CHARS = 4000


@dataclass
class _Decision:
    """One parsed model turn."""

    thought: str
    tool: str | None
    tool_input: dict
    final_answer: str | None
    raw: str


class Agent:
    """Runs one task to completion, emitting a step per iteration."""

    def __init__(
        self,
        registry: ToolRegistry,
        router: Router,
        client: LLMClient,
        max_steps: int | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.registry = registry
        self.router = router
        self.client = client
        self.max_steps = int(max_steps if max_steps is not None else config.get("agent.max_steps", 8))
        self.timeout_s = float(timeout_s if timeout_s is not None else config.get("agent.wall_clock_timeout_s", 180))

    # -- public -----------------------------------------------------------

    def run(self, request: TaskRequest) -> AgentResult:
        """Run to completion and return the finished result."""
        result: AgentResult | None = None
        for item in self.stream(request):
            if isinstance(item, AgentResult):
                result = item
        assert result is not None  # stream always ends with a result
        return result

    def stream(self, request: TaskRequest) -> Iterator[AgentStep | AgentResult]:
        """Yield each ``AgentStep`` as it completes, then the final ``AgentResult``.

        The API's SSE endpoint consumes this directly, so the UI trace fills in
        as the agent works rather than appearing all at once at the end.
        """
        started = time.perf_counter()
        deadline = started + self.timeout_s

        decision = self.router.route(request.task, request.task_type_hint)
        steps: list[AgentStep] = []
        citations: list[SourceCitation] = []

        conversation = [
            Message(
                role="system",
                content=prompts.system_prompt(
                    decision.task_type,
                    self.registry.describe_for_prompt(),
                    request.attachments,
                ),
            ),
            Message(role="user", content=request.task.strip()),
        ]

        max_steps = self.max_steps
        final_answer: str | None = None
        status = TaskStatus.COMPLETED
        error: str | None = None

        for step_number in range(1, max_steps + 1):
            if time.perf_counter() > deadline:
                status = TaskStatus.TRUNCATED
                error = f"Wall-clock timeout of {self.timeout_s:.0f}s reached."
                break

            remaining = max_steps - step_number
            if remaining <= 2:
                conversation.append(Message(role="user", content=prompts.step_cap_warning(remaining)))

            step_started = time.perf_counter()

            # --- 1. think -------------------------------------------------
            try:
                completion = self._generate(conversation, decision)
            except InferenceError as exc:
                steps.append(
                    self._step(
                        step_number,
                        thought="Could not reach the local model.",
                        status=StepStatus.ERROR,
                        tool_output=str(exc),
                        model=decision.model,
                        started=step_started,
                        metadata={"stage": "inference"},
                    )
                )
                yield steps[-1]
                status, error = TaskStatus.FAILED, str(exc)
                # Answer with the fix, not just the symptom. "Connection refused"
                # on a projector tells the room nothing; the two commands that
                # repair it do.
                final_answer = (
                    "The local inference server is not reachable, so this task could not be run.\n\n"
                    f"{exc}\n\n"
                    "To fix it:\n"
                    "  1. Start the server:  ollama serve\n"
                    f"  2. Confirm the model is pulled:  ollama pull {decision.model}\n\n"
                    f"The endpoint is set to {config.get('inference.endpoint')} in config.yaml. "
                    "No external service is involved - this workbench only ever talks to this machine."
                )
                break

            parsed = _parse_decision(completion.text)

            # --- 2. malformed reply --------------------------------------
            if parsed is None:
                step = self._step(
                    step_number,
                    thought="Model reply was not valid JSON; asking it to reformat.",
                    status=StepStatus.ERROR,
                    tool_output=prompts.malformed_response_nudge(completion.text),
                    model=completion.model,
                    started=step_started,
                    metadata={"stage": "parse", "raw_preview": completion.text[:200]},
                )
                steps.append(step)
                yield step
                conversation.append(Message(role="assistant", content=completion.text))
                conversation.append(Message(role="user", content=step.tool_output))
                continue

            # --- 3. final answer -----------------------------------------
            if parsed.tool is None and parsed.final_answer is not None:
                final_answer = parsed.final_answer
                step = self._step(
                    step_number,
                    thought=parsed.thought or "Finalizing.",
                    status=StepStatus.OK,
                    model=completion.model,
                    started=step_started,
                    metadata={"stage": "final"},
                )
                steps.append(step)
                yield step
                break

            # A turn with neither a tool nor an answer: treat the prose as the answer
            # rather than burning another iteration on it.
            if parsed.tool is None:
                final_answer = parsed.thought or completion.text.strip()
                step = self._step(
                    step_number,
                    thought="No tool requested; treating the reply as the final answer.",
                    status=StepStatus.OK,
                    model=completion.model,
                    started=step_started,
                    metadata={"stage": "final", "inferred": True},
                )
                steps.append(step)
                yield step
                break

            # --- 4. act ---------------------------------------------------
            outcome = self.registry.dispatch(parsed.tool, parsed.tool_input)
            observation = _truncate(outcome.as_observation())

            if outcome.citations:
                citations.extend(outcome.citations)

            step = self._step(
                step_number,
                thought=parsed.thought,
                tool=parsed.tool,
                tool_input=parsed.tool_input,
                tool_output=observation,
                status=StepStatus.OK if outcome.ok else StepStatus.ERROR,
                model=completion.model,
                started=step_started,
                metadata={
                    "stage": "tool",
                    "artifacts": outcome.artifacts,
                    "citations": len(outcome.citations),
                    **outcome.metadata,
                },
            )
            steps.append(step)
            yield step

            # --- 5. observe ----------------------------------------------
            conversation.append(Message(role="assistant", content=parsed.raw))
            conversation.append(Message(role="user", content=f"Tool result ({parsed.tool}):\n{observation}"))

        else:
            # Loop exhausted without a break: the cap stopped it.
            status = TaskStatus.TRUNCATED
            error = f"Stopped after the {max_steps}-step cap without a final answer."

        if final_answer is None and status is TaskStatus.COMPLETED:
            status = TaskStatus.TRUNCATED

        if final_answer is None:
            final_answer = _salvage(steps, error)

        return_value = AgentResult(
            task_id=request.task_id,
            status=status,
            final_text=final_answer,
            steps=steps,
            task_type=decision.task_type,
            model_used=decision.model,
            routing_reason=decision.reason,
            sources=_dedupe(citations),
            total_duration_ms=int((time.perf_counter() - started) * 1000),
            error=error,
            completed_at=utcnow(),
        )
        yield return_value

    # -- internals --------------------------------------------------------

    def _generate(self, conversation: list[Message], decision: RoutingDecision) -> Completion:
        return self.client.chat(
            messages=conversation,
            model=decision.model,
            temperature=float(config.get("agent.temperature", 0.2)),
        )

    def _step(
        self,
        step_number: int,
        thought: str,
        status: StepStatus,
        started: float,
        model: str,
        tool: str | None = None,
        tool_input: dict | None = None,
        tool_output: str = "",
        metadata: dict | None = None,
    ) -> AgentStep:
        return AgentStep(
            step_number=step_number,
            thought=thought,
            tool_name=tool,
            tool_input=tool_input or {},
            tool_output=tool_output,
            status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            model_used=model,
            metadata=metadata or {},
        )


# ---------------------------------------------------------------------------
# Parsing the model's turn
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _parse_decision(text: str) -> _Decision | None:
    """Extract one decision object from a model reply.

    Small models wrap JSON in markdown fences, prepend "Sure!", or emit two
    objects. Each of those is a lost demo turn, so we recover from all three
    rather than bouncing the model on a technicality.
    """
    if not text or not text.strip():
        return None

    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        tool = payload.get("tool") or payload.get("tool_name") or payload.get("action")
        if isinstance(tool, str) and tool.strip().lower() in {"", "none", "null", "final", "finish"}:
            tool = None

        final_answer = payload.get("final_answer") or payload.get("answer") or payload.get("final")
        tool_input = payload.get("tool_input") or payload.get("arguments") or payload.get("input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {"value": tool_input}

        if tool is None and final_answer is None and "thought" not in payload:
            continue  # not a decision object, keep looking

        return _Decision(
            thought=str(payload.get("thought") or payload.get("reasoning") or "").strip(),
            tool=tool.strip() if isinstance(tool, str) else None,
            tool_input=tool_input,
            final_answer=str(final_answer).strip() if final_answer is not None else None,
            raw=candidate,
        )

    return None


def _json_candidates(text: str) -> list[str]:
    """Plausible JSON objects in ``text``, most likely first."""
    candidates: list[str] = []
    stripped = text.strip()

    if stripped.startswith("{"):
        candidates.append(stripped)

    candidates.extend(match.group(1).strip() for match in _FENCE_RE.finditer(text))

    # Brace matching, so a nested tool_input does not truncate the object.
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(text[start : index + 1])
                start = -1
            elif depth < 0:
                depth = 0

    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = _MAX_OBSERVATION_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n...[{len(text) - 2 * half} characters omitted]...\n{text[-half:]}"


def _salvage(steps: list[AgentStep], error: str | None) -> str:
    """Build a useful answer from a run that never produced one.

    The demo requirement is graceful termination with partial results, and the
    most useful partial result is the last thing a tool actually told us.
    """
    header = error or "The run ended before a final answer was produced."

    for step in reversed(steps):
        if step.status is StepStatus.OK and step.tool_output.strip():
            return (
                f"{header}\n\n"
                f"Work completed before stopping ({len(steps)} steps). "
                f"The last successful result was from `{step.tool_name}`:\n\n{step.tool_output.strip()}"
            )

    thoughts = [s.thought for s in steps if s.thought.strip()]
    if thoughts:
        return f"{header}\n\nNo tool call succeeded. The agent's last reasoning was:\n\n{thoughts[-1]}"
    return f"{header}\n\nNo steps completed."


def _dedupe(citations: list[SourceCitation]) -> list[SourceCitation]:
    """One citation per document id, keeping the best-scoring occurrence."""
    best: dict[str, SourceCitation] = {}
    for citation in citations:
        existing = best.get(citation.document_id)
        if existing is None or (citation.score or 0) > (existing.score or 0):
            best[citation.document_id] = citation
    return sorted(best.values(), key=lambda c: -(c.score or 0))


__all__ = ["Agent", "TaskType"]
