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

import ast
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from src import config
from src.contracts import (
    AgentResult,
    AgentStep,
    Deliverable,
    DeliverableKind,
    SourceCitation,
    StepStatus,
    TaskRequest,
    TaskStatus,
    TaskType,
    utcnow,
)
from src.core import demo, prompts
from src.core.llm import Completion, InferenceError, LLMClient, Message
from src.core.router import Router, RoutingDecision
from src.core.tools import ToolRegistry

# Tool output longer than this is truncated before it re-enters the prompt.
# Without it, one big file read blows the context window and every later step
# degrades for reasons that are invisible in the trace.
_MAX_OBSERVATION_CHARS = 4000

# How many times the agent may repeat one identical tool call before the run is
# treated as stuck. Two nudges is generous; a third is a loop, not a retry.
_MAX_IDENTICAL_REPEATS = 2


@dataclass
class _Decision:
    """One parsed model turn."""

    thought: str
    tool: str | None
    tool_input: dict
    final_answer: str | None
    raw: str


@dataclass
class _Verdict:
    """Outcome of the self check."""

    verified: bool
    issues: list[str]


def _self_check_enabled() -> bool:
    return bool(config.get("agent.self_check", True))


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

    def stream(self, request: TaskRequest) -> Iterator[RoutingDecision | AgentStep | AgentResult]:
        """Yield the routing decision, then each ``AgentStep``, then the result.

        The API's SSE endpoint consumes this directly, so the UI trace fills in
        as the agent works rather than appearing all at once at the end.

        The routing decision is yielded rather than computed by the caller so it
        is made exactly once per run: routing may itself cost a model call, and
        doing it twice both doubles that cost and risks the badge disagreeing
        with the model that actually ran.
        """
        started = time.perf_counter()
        deadline = started + self.timeout_s

        decision = self.router.route(request.task, request.task_type_hint)
        yield decision

        steps: list[AgentStep] = []
        citations: list[SourceCitation] = []
        artifact_paths: list[str] = []
        # Signature -> observation, so an identical repeat is answered from
        # what we already have instead of being run again.
        executed: dict[str, str] = {}
        repeats: dict[str, int] = {}

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
                # Appended to the trailing user turn rather than added as a new
                # one: chat models expect strictly alternating roles, and two
                # user messages in a row degrades output on small models.
                notice = prompts.step_cap_warning(remaining)
                if conversation and conversation[-1].role == "user":
                    conversation[-1] = Message(role="user", content=conversation[-1].content + notice)
                else:
                    conversation.append(Message(role="user", content=notice.strip()))

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
            # A small model that finds one working tool call will happily make
            # it eight times and time the demo out. The step cap bounds that but
            # does not fix it: answer the repeat from what we already have, and
            # say plainly that repeating is not progress.
            signature = f"{parsed.tool}:{json.dumps(parsed.tool_input, sort_keys=True, default=str)}"

            if signature in executed:
                repeats[signature] = repeats.get(signature, 0) + 1
                observation = (
                    f"You already called {parsed.tool} with exactly these arguments and got the "
                    "result below. Calling it again will not produce anything new - use this "
                    "result, call a different tool, or give your final answer now.\n\n"
                    f"{executed[signature]}"
                )
                step = self._step(
                    step_number,
                    thought=parsed.thought,
                    tool=parsed.tool,
                    tool_input=parsed.tool_input,
                    tool_output=observation,
                    status=StepStatus.ERROR,
                    model=completion.model,
                    started=step_started,
                    metadata={"stage": "tool", "repeated": True, "repeat_count": repeats[signature]},
                )
                steps.append(step)
                yield step

                if repeats[signature] >= _MAX_IDENTICAL_REPEATS:
                    status = TaskStatus.TRUNCATED
                    error = (
                        f"Stopped early: the agent called {parsed.tool} with identical arguments "
                        f"{repeats[signature] + 1} times without making progress."
                    )
                    break

                conversation.append(Message(role="assistant", content=parsed.raw))
                conversation.append(Message(role="user", content=observation))
                continue

            outcome = self.registry.dispatch(parsed.tool, parsed.tool_input)
            observation = _truncate(outcome.as_observation())

            if outcome.ok:
                executed[signature] = observation

            if outcome.citations:
                citations.extend(outcome.citations)

            if outcome.ok and outcome.artifacts:
                artifact_paths.extend(outcome.artifacts)

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

        # --- 6. self check ------------------------------------------------
        # The proposal's Figure 2 makes a point of this step: the system checks
        # its own numbers against the sources before a human ever sees them. In
        # an inspection context a confidently wrong thickness is worse than no
        # answer, so a completed, cited answer gets one verification pass.
        if _self_check_enabled() and status is TaskStatus.COMPLETED and citations:
            check_started = time.perf_counter()
            verdict = self._verify(final_answer, citations, decision)

            if verdict is not None:
                if not verdict.verified and verdict.issues:
                    final_answer = (
                        f"{final_answer}\n\n---\n\n"
                        "**Self check flagged this answer.** The following claims could not be "
                        "matched against the retrieved sources, and a human reviewer should "
                        "confirm them before signing:\n\n"
                        + "\n".join(f"- {issue}" for issue in verdict.issues)
                    )

                step = self._step(
                    len(steps) + 1,
                    thought=(
                        "Checking every figure in the answer against the retrieved passages "
                        "before a human sees it."
                    ),
                    tool="self_check",
                    tool_input={"claims_checked": len(citations)},
                    tool_output=(
                        "All figures in the answer are supported by the cited passages."
                        if verdict.verified
                        else "Unsupported claims found:\n" + "\n".join(f"- {i}" for i in verdict.issues)
                    ),
                    status=StepStatus.OK if verdict.verified else StepStatus.ERROR,
                    model=decision.model,
                    started=check_started,
                    metadata={"stage": "self_check", "verified": verdict.verified},
                )
                steps.append(step)
                yield step

        return_value = AgentResult(
            task_id=request.task_id,
            status=status,
            final_text=final_answer,
            steps=steps,
            task_type=decision.task_type,
            model_used=decision.model,
            routing_reason=decision.reason,
            sources=_dedupe(citations),
            deliverables=_deliverables_from_paths(artifact_paths),
            total_duration_ms=int((time.perf_counter() - started) * 1000),
            error=error,
            completed_at=utcnow(),
        )
        yield return_value

    # -- internals --------------------------------------------------------

    def _generate(self, conversation: list[Message], decision: RoutingDecision) -> Completion:
        # Demo mode pins this to 0 so a rehearsed run takes the same path twice.
        temperature = demo.demo_temperature(float(config.get("agent.temperature", 0.2)))
        return self.client.chat(
            messages=conversation,
            model=decision.model,
            temperature=temperature,
        )

    def _verify(
        self, answer: str, citations: list[SourceCitation], decision: RoutingDecision
    ) -> _Verdict | None:
        """Check the answer's figures against the retrieved passages.

        Returns None if the check itself could not run - a verification failure
        must never turn a good answer into a failed run, so an unreachable model
        here simply means no self-check step appears in the trace.

        Deliberately one call with no tools: this is a reading-comprehension
        question over text we already hold, and giving it the tool loop back
        would let it wander.
        """
        sources = "\n\n".join(
            f"[{i}] {c.source_path}, page {c.page}:\n{c.snippet}"
            for i, c in enumerate(citations[:8], start=1)
        )

        try:
            completion = self.client.chat(
                messages=[
                    Message(
                        role="system",
                        content=(
                            "You are a checker. You are given SOURCES and an ANSWER written from "
                            "them. Find any number, threshold, limit, equipment tag or procedural "
                            "claim in the ANSWER that is NOT supported by the SOURCES.\n\n"
                            "Arithmetic the answer shows its working for counts as supported.\n"
                            "Do not restate the answer, do not suggest improvements, and do not "
                            "flag wording.\n\n"
                            'Reply with one JSON object only:\n'
                            '{"verified": true, "issues": []}\n'
                            'or {"verified": false, "issues": ["the claim, and what is missing"]}'
                        ),
                    ),
                    Message(
                        role="user",
                        content=f"SOURCES:\n{sources}\n\nANSWER:\n{answer}",
                    ),
                ],
                model=decision.model,
                temperature=0.0,
                max_tokens=400,
            )
        except Exception:  # noqa: BLE001 - the check is best-effort by design
            return None

        payload = _parse_json_object(completion.text)
        if payload is None:
            return None

        issues = payload.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        issues = [str(i).strip() for i in issues if str(i).strip()][:5]

        verified = bool(payload.get("verified", True)) and not issues
        return _Verdict(verified=verified, issues=issues)

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

# Qwen3.5 and other reasoning models emit their scratchpad in <think> tags
# before the answer. That text routinely contains braces and quotes, which would
# derail brace-matching, so it is removed before anything else looks at the
# reply. An unclosed tag (a truncated generation) is treated as thinking all the
# way to the end.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Remove <think> blocks from a model reply, keeping the answer."""
    if not text or "<think>" not in text.lower():
        return text
    return _OPEN_THINK_RE.sub("", _THINK_RE.sub("", text)).strip()


def _loads_lenient(candidate: str) -> object | None:
    """Parse one candidate as JSON, falling back to a Python literal.

    The model sometimes emits `{'tool': 'x', 'tool_input': {...}}` - a Python
    dict repr, single quotes - instead of JSON. json.loads rejects it, the
    decision goes unparsed, and the caller shows the raw object to the user as
    though it were the answer while the tool call it described never runs.

    ast.literal_eval evaluates literals only - no calls, no names, no
    attribute access - so this cannot execute model-supplied code.
    """
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(candidate)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def _parse_decision(text: str) -> _Decision | None:
    """Extract one decision object from a model reply.

    Small models wrap JSON in markdown fences, prepend "Sure!", or emit two
    objects. Each of those is a lost demo turn, so we recover from all three
    rather than bouncing the model on a technicality.
    """
    if not text or not text.strip():
        return None

    text = strip_reasoning(text)
    if not text.strip():
        return None

    for candidate in _json_candidates(text):
        payload = _loads_lenient(candidate)
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


def _parse_json_object(text: str) -> dict | None:
    """First JSON object in a model reply, reusing the decision-parser's scanner."""
    if not text:
        return None
    for candidate in _json_candidates(strip_reasoning(text)):
        payload = _loads_lenient(candidate)
        if isinstance(payload, dict):
            return payload
    return None


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


def _deliverables_from_paths(paths: list[str]) -> list[Deliverable]:
    """Turn tool-reported artifact paths into the Deliverable list the UI reads.

    A tool (create_approval_documents, or any future one) reports file paths in
    ``ToolOutcome.artifacts``; without this, those files exist on disk but the
    UI's download panel and ``AgentResult.deliverables`` never learn about them.
    Deduplicated and order-preserved, since a retried tool call can repeat a path.
    """
    seen: set[str] = set()
    deliverables: list[Deliverable] = []
    for raw_path in paths:
        if raw_path in seen:
            continue
        seen.add(raw_path)

        path = Path(raw_path)
        if not path.is_file():
            continue  # a tool reported a path that is gone by the time the run ends

        suffix = path.suffix.lstrip(".").lower()
        kind = DeliverableKind(suffix) if suffix in set(DeliverableKind) else DeliverableKind.OTHER

        deliverables.append(
            Deliverable(
                filename=path.name,
                path=str(path),
                kind=kind,
                size_bytes=path.stat().st_size,
                download_url=f"/api/deliverables/{path.name}",
                title=path.stem,
            )
        )
    return deliverables


def _dedupe(citations: list[SourceCitation]) -> list[SourceCitation]:
    """One citation per document id, keeping the best-scoring occurrence."""
    best: dict[str, SourceCitation] = {}
    for citation in citations:
        existing = best.get(citation.document_id)
        if existing is None or (citation.score or 0) > (existing.score or 0):
            best[citation.document_id] = citation
    return sorted(best.values(), key=lambda c: -(c.score or 0))


__all__ = ["Agent", "TaskType"]
