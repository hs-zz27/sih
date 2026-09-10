"""Tests for the core engine (M1-M7).

Everything here runs with no model server and no network. The LLM is replaced by
a scripted fake, which is the only way to test an agent loop deterministically:
the assertions are about the *loop's* behaviour - does it stop at the cap, does
it survive a tool error, does it recover from malformed JSON - not about what a
7B model happens to say on the day.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.contracts import (
    AgentResult,
    AgentStep,
    Document,
    StepStatus,
    TaskRequest,
    TaskStatus,
    TaskType,
    ToolSpec,
)
from src.core.agent import Agent, _parse_decision
from src.core.llm import Completion, LLMClient, SovereigntyError, endpoint_is_local
from src.core.rag import RagIndex, TfidfEmbedder, chunk_document
from src.core.router import Router
from src.core.sandbox import Sandbox
from src.core.tools import ToolOutcome, ToolRegistry, build_registry, resolve_within_roots


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class ScriptedClient:
    """An LLM that says exactly what the test tells it to, in order."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list] = []

    def chat(self, messages, model, temperature=None, max_tokens=None, stop=None) -> Completion:
        self.calls.append(messages)
        text = self.replies.pop(0) if self.replies else '{"thought": "done", "tool": null, "final_answer": "fallback"}'
        return Completion(text=text, model=model, duration_ms=1)


def tool_call(thought: str, tool: str, **kwargs) -> str:
    return json.dumps({"thought": thought, "tool": tool, "tool_input": kwargs})


def final(answer: str, thought: str = "I have what I need.") -> str:
    return json.dumps({"thought": thought, "tool": None, "final_answer": answer})


def make_agent(replies: list[str], registry: ToolRegistry, **kwargs) -> tuple[Agent, ScriptedClient]:
    client = ScriptedClient(replies)
    agent = Agent(registry=registry, router=Router(use_llm_tiebreak=False), client=client, **kwargs)  # type: ignore[arg-type]
    return agent, client


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    index = RagIndex(index_dir=tmp_path / "index", backend="tfidf")
    index.index_documents(
        [
            Document(
                id="sop-114::p3",
                source_path="corpus/SOP-114.txt",
                page=3,
                text=(
                    "Wall thickness readings below 80 percent of nominal shall be escalated to the "
                    "Inspection Engineer within one working day and recorded on Form IR-2."
                ),
            ),
            Document(
                id="manual::p12",
                source_path="corpus/HX_Manual.txt",
                page=12,
                text="Shell-side hydrotest pressure is 1.5 times the design pressure held for 30 minutes.",
            ),
        ]
    )
    return build_registry(index=index, sandbox=Sandbox(workdir=tmp_path / "work", timeout_s=15))


# ---------------------------------------------------------------------------
# M1 - inference client and the sovereignty guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:11434", "http://localhost:8000", "http://192.168.1.42:8000", "http://10.0.0.5:11434"],
)
def test_local_endpoints_accepted(endpoint: str):
    assert endpoint_is_local(endpoint, allowed_hosts=["127.0.0.1", "localhost", "::1"])


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.openai.com/v1",
        "https://api.anthropic.com",
        "https://api-inference.huggingface.co/models/x",
        "http://8.8.8.8:11434",
        "http://evil.example.com:11434",
    ],
)
def test_public_endpoints_rejected(endpoint: str):
    """The whole submission rests on this. A public host must never be usable."""
    assert not endpoint_is_local(endpoint, allowed_hosts=["127.0.0.1", "localhost", "::1"])


def test_client_refuses_to_construct_against_a_public_endpoint():
    with pytest.raises(SovereigntyError):
        LLMClient(endpoint="https://api.openai.com/v1")


def test_client_builds_ollama_and_openai_payloads_differently():
    from src.core.llm import Message

    ollama = LLMClient(endpoint="http://127.0.0.1:11434", api_style="ollama")
    openai_style = LLMClient(endpoint="http://127.0.0.1:8000", api_style="openai-compatible")
    messages = [Message(role="user", content="hi")]

    ollama_payload = ollama._build_payload(messages, "m", 0.5, 100, None, stream=False)
    openai_payload = openai_style._build_payload(messages, "m", 0.5, 100, None, stream=False)

    assert ollama_payload["options"]["temperature"] == 0.5
    assert ollama_payload["options"]["num_predict"] == 100
    assert openai_payload["temperature"] == 0.5
    assert openai_payload["max_tokens"] == 100
    assert ollama._chat_url().endswith("/api/chat")
    assert openai_style._chat_url().endswith("/v1/chat/completions")


# ---------------------------------------------------------------------------
# M2 - router
# ---------------------------------------------------------------------------


def test_router_sends_document_work_to_the_document_model():
    decision = Router(use_llm_tiebreak=False).route(
        "Summarise the attached inspection report and draft an approval note"
    )
    assert decision.task_type is TaskType.DOCUMENT
    assert decision.matched_signals


def test_router_sends_code_work_to_the_code_model():
    decision = Router(use_llm_tiebreak=False).route("Write a Python script to debug this traceback")
    assert decision.task_type is TaskType.CODE


def test_router_honours_an_operator_override():
    decision = Router(use_llm_tiebreak=False).route("Write a Python script", hint=TaskType.DOCUMENT)
    assert decision.task_type is TaskType.DOCUMENT
    assert decision.method == "hint"


def test_router_falls_back_to_general_with_no_signals():
    assert Router(use_llm_tiebreak=False).route("hello there").task_type is TaskType.GENERAL


def test_every_routing_decision_is_explainable():
    """Visibility is the actual PS requirement, so it is a tested property."""
    router = Router(use_llm_tiebreak=False)
    for task in ["Draft an approval note from SOP-114", "Debug this Python function", "hello"]:
        decision = router.route(task)
        assert len(decision.reason) > 30, "a judge must be able to read why this model was chosen"
        assert decision.as_step_metadata()["model"]


def test_router_survives_a_dead_model_during_tiebreak():
    """An ambiguous task with no model running must still route, not raise."""

    class DeadClient:
        def chat(self, **_kwargs):
            raise RuntimeError("connection refused")

    # "compute" (code) against "report" (document) scores within the decisive
    # margin, so this task genuinely reaches the model tie-break.
    decision = Router(client=DeadClient()).route("compute the report")  # type: ignore[arg-type]
    assert decision.task_type in set(TaskType)
    assert decision.method == "fallback", "a dead model must degrade to the keyword leader"
    assert decision.model, "a fallback still has to name the model it will use"


# ---------------------------------------------------------------------------
# M3 - tool registry
# ---------------------------------------------------------------------------


def test_unknown_tool_is_an_observation_not_an_exception(registry: ToolRegistry):
    outcome = registry.dispatch("no_such_tool", {})
    assert not outcome.ok
    assert "no_such_tool" in outcome.error
    assert "search_documents" in outcome.error, "the error must tell the model what it CAN call"


def test_missing_required_argument_is_reported_with_the_schema(registry: ToolRegistry):
    outcome = registry.dispatch("read_file", {})
    assert not outcome.ok
    assert "path" in outcome.error


def test_a_raising_handler_becomes_an_observation():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="explode",
            description="Always fails.",
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=lambda: (_ for _ in ()).throw(ValueError("boom")),
        )
    )
    outcome = registry.dispatch("explode", {})
    assert not outcome.ok and "boom" in outcome.error


@pytest.mark.parametrize(
    "path",
    [
        "/etc/hosts",
        "/etc/passwd",
        "/tmp/evil.txt",
        "/Users/nonexistent/.ssh/id_rsa",
    ],
)
def test_file_tools_are_confined_to_the_workbench(registry: ToolRegistry, path: str):
    """An absolute path outside the allowed roots must be refused as such.

    Use a POSIX-absolute path: a Windows-style path like "C:/Windows/..." is not
    absolute on POSIX, so it is reinterpreted as a relative path and confined
    into the workspace - safe, but it exercises the suffix check rather than the
    confinement check this test exists to cover.
    """
    outcome = registry.dispatch("read_file", {"path": path})
    assert not outcome.ok
    assert "outside the workbench" in outcome.error


@pytest.mark.parametrize(
    "path",
    [
        "C:/Windows/System32/drivers/etc/hosts",  # not absolute on POSIX
        "~/.ssh/id_rsa",  # Path does not expanduser(), so ~ stays literal
    ],
)
def test_non_posix_absolute_paths_are_neutralised_not_followed(
    registry: ToolRegistry, path: str
):
    """Refused, and confined into the workspace rather than followed off-disk.

    These do not produce the "outside the workbench" message because neither is
    an absolute path to POSIX - they are confined, then rejected on suffix or
    non-existence. What matters is that the read never succeeds.
    """
    outcome = registry.dispatch("read_file", {"path": path})
    assert not outcome.ok


def test_traversal_out_of_the_workspace_is_refused():
    with pytest.raises(Exception):
        resolve_within_roots("../../../../etc/passwd")


def test_write_then_read_roundtrip(registry: ToolRegistry):
    written = registry.dispatch("write_file", {"path": "note.md", "content": "# Findings\n12.0 mm nominal"})
    assert written.ok and written.artifacts
    read_back = registry.dispatch("read_file", {"path": "note.md"})
    assert read_back.ok and "12.0 mm nominal" in read_back.output


def test_binary_files_are_refused_with_a_useful_explanation(registry: ToolRegistry, tmp_path: Path):
    """A real .pdf on disk, so the suffix guard is what refuses it.

    The earlier version of this test used a filename that did not exist, so it
    passed on the missing-file path and never reached the suffix check at all.
    """
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "scan.pdf").write_bytes(b"%PDF-1.4 binary junk")

    outcome = registry.dispatch("read_file", {"path": "scan.pdf"})
    assert not outcome.ok
    assert "not readable as text" in outcome.error
    assert "search_documents" in outcome.error, "point the model at the tool that does work"


def test_tools_are_confined_to_the_sandbox_they_were_built_with(tmp_path: Path):
    """Regression: the registry used to read its roots from config only.

    That meant an injected sandbox wrote to one directory while read_file looked
    in another - and in tests it quietly touched the real data/sandbox.
    """
    workspace = tmp_path / "isolated"
    registry = build_registry(
        index=RagIndex(index_dir=tmp_path / "index", backend="tfidf"),
        sandbox=Sandbox(workdir=workspace),
    )

    written = registry.dispatch("write_file", {"path": "scratch.md", "content": "local only"})
    assert written.ok
    assert Path(written.artifacts[0]).parent == workspace.resolve()

    read_back = registry.dispatch("read_file", {"path": "scratch.md"})
    assert read_back.ok and "local only" in read_back.output


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"query": "thickness", "top_k": "2"}, 2),   # models emit "2", not 2
        ({"query": "thickness", "top_k": 2}, 2),
    ],
)
def test_numeric_arguments_sent_as_strings_are_coerced(registry: ToolRegistry, arguments, expected):
    outcome = registry.dispatch("search_documents", arguments)
    assert outcome.ok
    assert outcome.metadata["hits"] <= expected


def test_a_wrongly_typed_argument_is_explained_not_crashed(registry: ToolRegistry):
    """Previously surfaced as a bare AttributeError from inside the handler."""
    outcome = registry.dispatch("search_documents", {"query": "x", "top_k": "many"})
    assert not outcome.ok
    assert "top_k" in outcome.error and "integer" in outcome.error


def test_numbers_sent_where_a_string_is_expected_are_coerced(registry: ToolRegistry):
    outcome = registry.dispatch("run_python", {"code": 123})
    # Coerced to "123", which is valid Python that simply prints nothing.
    assert outcome.ok or "exit code" in (outcome.error or "")


def test_prompt_description_lists_every_tool(registry: ToolRegistry):
    described = registry.describe_for_prompt()
    for name in registry.names():
        assert name in described


# ---------------------------------------------------------------------------
# M5 - sandbox
# ---------------------------------------------------------------------------


def test_sandbox_runs_code_and_captures_stdout(tmp_path: Path):
    result = Sandbox(workdir=tmp_path).run("print(round(9.1 / 12.0 * 100, 1))")
    assert result.ok and "75.8" in result.stdout


def test_sandbox_blocks_network_access(tmp_path: Path):
    """Doubles as sovereignty evidence: generated code cannot phone home."""
    result = Sandbox(workdir=tmp_path).run(
        "import urllib.request\nurllib.request.urlopen('http://example.com', timeout=2)"
    )
    assert not result.ok
    assert result.network_blocked


def test_sandbox_blocks_raw_sockets(tmp_path: Path):
    result = Sandbox(workdir=tmp_path).run("import socket\nsocket.socket().connect(('1.1.1.1', 80))")
    assert not result.ok and result.network_blocked


def test_blocking_network_does_not_break_stdlib_imports(tmp_path: Path):
    """ssl.py subclasses socket at import time - the block must survive that."""
    result = Sandbox(workdir=tmp_path).run("import urllib.request, ssl, http.client\nprint('ok')")
    assert result.ok and "ok" in result.stdout


def test_sandbox_enforces_its_timeout(tmp_path: Path):
    result = Sandbox(workdir=tmp_path, timeout_s=2).run("while True: pass")
    assert result.timed_out and not result.ok
    assert "TIMED OUT" in result.as_observation()


def test_traceback_line_numbers_match_the_submitted_code(tmp_path: Path):
    """Otherwise the model debugs against phantom line numbers and burns steps."""
    result = Sandbox(workdir=tmp_path).run("x = 1\ny = x / 0")
    assert ", line 2" in result.stderr


def test_sandbox_reports_files_it_created(tmp_path: Path):
    result = Sandbox(workdir=tmp_path).run("open('report.txt', 'w').write('body')")
    assert "report.txt" in result.created_files


# ---------------------------------------------------------------------------
# M6 - RAG
# ---------------------------------------------------------------------------


def test_chunking_preserves_page_and_source(tmp_path: Path):
    document = Document(
        id="d::p7",
        source_path="corpus/manual.txt",
        page=7,
        text="\n\n".join(f"Paragraph {i} on vessel inspection." for i in range(60)),
    )
    chunks = chunk_document(document, chunk_size=300, overlap=50)
    assert len(chunks) > 1
    assert all(c.page == 7 and c.source_path == "corpus/manual.txt" for c in chunks)


def test_retrieval_returns_the_right_document_with_a_citation(tmp_path: Path):
    index = RagIndex(index_dir=tmp_path, backend="tfidf")
    index.index_documents(
        [
            Document(id="a::p3", source_path="corpus/SOP-114.txt", page=3,
                     text="Wall thickness below 80 percent of nominal must be escalated to the Inspection Engineer."),
            Document(id="b::p1", source_path="corpus/PPE.txt", page=1,
                     text="Confined space entry requires a harness, a gas detector and a standby attendant."),
        ]
    )
    citations = index.search_citations("wall thickness escalation threshold", top_k=1)
    assert citations
    assert citations[0].source_path.endswith("SOP-114.txt")
    assert citations[0].page == 3, "attribution without a page number is not attribution"


def test_index_survives_a_restart(tmp_path: Path):
    documents = [Document(id="a::p1", source_path="corpus/a.txt", page=1, text="hydrotest pressure limits")]
    RagIndex(index_dir=tmp_path, backend="tfidf").index_documents(documents)
    assert RagIndex(index_dir=tmp_path, backend="tfidf").stats()["chunks"] == 1


def test_reindexing_a_source_replaces_it_rather_than_duplicating(tmp_path: Path):
    index = RagIndex(index_dir=tmp_path, backend="tfidf")
    index.index_documents([Document(id="a::p1", source_path="corpus/a.txt", page=1, text="original text")])
    index.index_documents([Document(id="a::p1", source_path="corpus/a.txt", page=1, text="corrected text")])
    assert index.stats()["chunks"] == 1


def test_empty_index_returns_nothing_rather_than_raising(tmp_path: Path):
    assert RagIndex(index_dir=tmp_path, backend="tfidf").search_citations("anything") == []


def test_tfidf_is_declared_non_semantic():
    """We do not get to call the fallback semantic search in the pitch."""
    assert TfidfEmbedder().semantic is False


# ---------------------------------------------------------------------------
# M4 - agent loop
# ---------------------------------------------------------------------------


def test_agent_completes_a_multi_step_task(registry: ToolRegistry):
    agent, client = make_agent(
        [
            tool_call("Find the threshold.", "search_documents", query="wall thickness escalation threshold"),
            tool_call("Check the arithmetic.", "run_python", code="print(round(9.1/12.0*100, 1))"),
            final("75.8 percent of nominal, below the 80 percent threshold in SOP-114 p.3. Escalate."),
        ],
        registry,
    )
    result = agent.run(TaskRequest(task="Summarise inspection report IR-2291 and draft an approval note"))

    assert result.status is TaskStatus.COMPLETED
    assert len(result.steps) == 3
    assert [s.step_number for s in result.steps] == [1, 2, 3]
    assert result.steps[-1].tool_name is None, "the final step carries the answer, not a tool call"
    assert "75.8" in result.final_text
    assert result.sources, "a document task that cited the corpus must return its sources"
    assert result.model_used and result.routing_reason


def test_a_tool_failure_is_observed_and_recovered_from(registry: ToolRegistry):
    """M3's core requirement: a tool error returns to the agent, never crashes it."""
    agent, _ = make_agent(
        [
            tool_call("Try a file that is not there.", "read_file", path="does_not_exist.txt"),
            tool_call("Search instead.", "search_documents", query="hydrotest pressure"),
            final("Shell-side hydrotest is 1.5x design pressure for 30 minutes."),
        ],
        registry,
    )
    result = agent.run(TaskRequest(task="What is the hydrotest pressure?"))

    assert result.status is TaskStatus.COMPLETED
    assert result.steps[0].status is StepStatus.ERROR
    assert result.steps[1].status is StepStatus.OK


def test_step_cap_truncates_with_partial_results(registry: ToolRegistry):
    """A runaway loop mid-demo is fatal, so the cap is enforced by the loop.

    Each call differs, so this exercises the cap itself rather than the
    repeated-call guard below.
    """
    agent, _ = make_agent(
        [tool_call("Again.", "search_documents", query=f"thickness {i}") for i in range(20)],
        registry,
        max_steps=4,
    )
    result = agent.run(TaskRequest(task="Summarise the inspection report"))

    assert result.status is TaskStatus.TRUNCATED
    assert len(result.steps) == 4, "the cap is hard"
    assert result.final_text.strip(), "a truncated run must still return something usable"


def test_identical_repeated_calls_are_short_circuited(registry: ToolRegistry):
    """The top demo risk is a model that loops on one working call.

    The repeat is answered from the result we already have - the tool is not run
    again - and the observation says plainly that repeating is not progress.
    """
    same = tool_call("Searching again.", "search_documents", query="thickness")
    agent, _ = make_agent([same] * 12, registry, max_steps=8)
    result = agent.run(TaskRequest(task="Summarise the inspection report"))

    assert result.status is TaskStatus.TRUNCATED
    assert len(result.steps) < 8, "a stuck agent must stop early, not burn the whole cap"
    assert result.steps[0].status is StepStatus.OK, "the first call is genuine work"
    assert result.steps[1].metadata.get("repeated") is True
    assert "already called" in result.steps[1].tool_output
    assert result.error and "identical" in result.error


def test_a_repeated_call_still_carries_the_earlier_result(registry: ToolRegistry):
    """The nudge must include the answer, or the model has nothing to act on."""
    same = tool_call("Again.", "search_documents", query="wall thickness escalation")
    agent, _ = make_agent([same] * 5, registry, max_steps=8)
    result = agent.run(TaskRequest(task="Summarise the report"))

    assert "escalated" in result.steps[1].tool_output.lower()


def test_the_cap_warning_never_creates_two_user_turns_in_a_row(registry: ToolRegistry):
    """Chat models expect alternating roles; small ones degrade without it."""
    agent, client = make_agent(
        [tool_call("Step.", "search_documents", query=f"q{i}") for i in range(20)], registry, max_steps=4
    )
    agent.run(TaskRequest(task="Summarise the report"))

    roles = [message.role for message in client.calls[-1]]
    consecutive = [i for i in range(len(roles) - 1) if roles[i] == roles[i + 1] == "user"]
    assert not consecutive, f"consecutive user turns at {consecutive}: {roles}"


def test_the_model_is_warned_before_the_cap_hits(registry: ToolRegistry):
    agent, client = make_agent(
        [tool_call("Again.", "search_documents", query=f"thickness {i}") for i in range(20)], registry, max_steps=4
    )
    agent.run(TaskRequest(task="Summarise the report"))
    conversation = "\n".join(m.content for m in client.calls[-1])
    assert "turns left" in conversation or "LAST turn" in conversation


def test_agent_recovers_from_malformed_json(registry: ToolRegistry):
    agent, _ = make_agent(
        ["Sure! I'll help with that.", final("Recovered and answered.")],
        registry,
    )
    result = agent.run(TaskRequest(task="Summarise the report"))

    assert result.status is TaskStatus.COMPLETED
    assert result.steps[0].status is StepStatus.ERROR
    assert result.final_text == "Recovered and answered."


def test_agent_reports_a_dead_model_as_a_failed_result(registry: ToolRegistry):
    from src.core.llm import InferenceError

    class DeadClient:
        def chat(self, **_kwargs):
            raise InferenceError("Cannot reach local inference server at http://127.0.0.1:11434")

    agent = Agent(registry=registry, router=Router(use_llm_tiebreak=False), client=DeadClient())  # type: ignore[arg-type]
    result = agent.run(TaskRequest(task="Summarise the report"))

    assert result.status is TaskStatus.FAILED
    assert result.error and "127.0.0.1" in result.error
    assert result.steps, "even a failed run shows the step where it failed"


def test_every_step_is_renderable_by_the_ui(registry: ToolRegistry):
    """An empty field is a hole on screen during the demo."""
    agent, _ = make_agent(
        [
            tool_call("Look it up.", "search_documents", query="hydrotest"),
            final("Done."),
        ],
        registry,
    )
    result = agent.run(TaskRequest(task="Summarise the report"))

    for step in result.steps:
        assert step.thought.strip()
        assert step.model_used
        assert step.duration_ms >= 0
        assert step.started_at is not None


def test_stream_yields_routing_then_steps_then_result(registry: ToolRegistry):
    """Routing is emitted by the agent so it is computed exactly once per run."""
    from src.core.router import RoutingDecision

    agent, _ = make_agent(
        [tool_call("Look it up.", "search_documents", query="hydrotest"), final("Done.")], registry
    )
    emitted = list(agent.stream(TaskRequest(task="Summarise the report")))

    assert isinstance(emitted[0], RoutingDecision)
    assert isinstance(emitted[-1], AgentResult)
    assert len(emitted) == 4  # routing + 2 steps + result
    assert all(isinstance(item, AgentStep) for item in emitted[1:-1])


# ---------------------------------------------------------------------------
# Decision parsing - where small models actually break
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        '{"thought": "t", "tool": "read_file", "tool_input": {"path": "a.txt"}}',
        '```json\n{"thought": "t", "tool": "read_file", "tool_input": {"path": "a.txt"}}\n```',
        'Sure, here you go:\n{"thought": "t", "tool": "read_file", "tool_input": {"path": "a.txt"}}',
        '{"thought": "t", "action": "read_file", "arguments": {"path": "a.txt"}}',
    ],
)
def test_tool_calls_parse_through_common_model_quirks(reply: str):
    decision = _parse_decision(reply)
    assert decision is not None
    assert decision.tool == "read_file"
    assert decision.tool_input == {"path": "a.txt"}


@pytest.mark.parametrize(
    "reply",
    [
        '{"thought": "t", "tool": null, "final_answer": "the answer"}',
        '{"thought": "t", "tool": "none", "final_answer": "the answer"}',
        '{"thought": "t", "tool": null, "answer": "the answer"}',
    ],
)
def test_final_answers_parse_through_common_model_quirks(reply: str):
    decision = _parse_decision(reply)
    assert decision is not None and decision.tool is None
    assert decision.final_answer == "the answer"


def test_nested_tool_input_is_not_truncated():
    decision = _parse_decision('{"thought": "t", "tool": "run_python", "tool_input": {"code": "print({\\"a\\": 1})"}}')
    assert decision is not None
    assert decision.tool_input["code"] == 'print({"a": 1})'


@pytest.mark.parametrize("reply", ["", "   ", "I cannot do that.", "{broken json"])
def test_unparseable_replies_return_none(reply: str):
    assert _parse_decision(reply) is None


# ---------------------------------------------------------------------------
# M7 - orchestrator wiring and the live trace
# ---------------------------------------------------------------------------


def _orchestrator_with(replies: list[str], tmp_path: Path):
    """An orchestrator whose model is scripted and whose index is disposable."""
    from src.core.orchestrator import Orchestrator

    index = RagIndex(index_dir=tmp_path / "index", backend="tfidf")
    index.index_documents(
        [
            Document(
                id="sop-114::p3",
                source_path="corpus/SOP-114.txt",
                page=3,
                text="Wall thickness below 80 percent of nominal must be escalated to the Inspection Engineer.",
            )
        ]
    )
    orchestrator = Orchestrator(
        client=ScriptedClient(replies),  # type: ignore[arg-type]
        index=index,
        sandbox=Sandbox(workdir=tmp_path / "work", timeout_s=15),
    )
    return orchestrator


def test_orchestrator_runs_a_task_end_to_end(tmp_path: Path):
    orchestrator = _orchestrator_with(
        [
            tool_call("Find the rule.", "search_documents", query="wall thickness escalation"),
            final("Below 80 percent of nominal, escalate to the Inspection Engineer (SOP-114 p.3)."),
        ],
        tmp_path,
    )
    result = orchestrator.run(TaskRequest(task="Summarise the inspection report and draft an approval note"))

    assert result.status is TaskStatus.COMPLETED
    assert result.sources and result.sources[0].page == 3
    assert orchestrator.get_result(result.task_id) is result


def test_background_run_streams_steps_as_they_happen(tmp_path: Path):
    """The live trace is the demo's visual centrepiece, so the path is tested.

    ``start`` must return before the run finishes, and ``events`` must then
    deliver each step followed by exactly one final result.
    """
    orchestrator = _orchestrator_with(
        [
            tool_call("Look it up.", "search_documents", query="wall thickness"),
            tool_call("Check the maths.", "run_python", code="print(round(9.1/12.0*100, 1))"),
            final("75.8 percent - below the threshold."),
        ],
        tmp_path,
    )

    request = TaskRequest(task="Summarise the inspection report")
    pending = orchestrator.start(request)
    assert pending.status is TaskStatus.RUNNING, "start() must not block on the run"
    assert pending.task_id == request.task_id

    emitted = list(orchestrator.events(request.task_id))
    steps = [item for item in emitted if isinstance(item, AgentStep)]
    results = [item for item in emitted if isinstance(item, AgentResult)]

    assert len(results) == 1
    assert len(steps) == 3
    assert isinstance(emitted[-1], AgentResult)
    assert results[0].status is TaskStatus.COMPLETED
    assert "75.8" in results[0].final_text


def test_start_returns_without_routing_on_the_request_thread(tmp_path: Path):
    """Routing can cost a model call; start() must not pay for it.

    Regression test: start() used to call the router itself, so an ambiguous
    task blocked the POST for the length of a model call, and the run was then
    routed a second time inside the agent.
    """
    import time

    class SlowClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, model, temperature=None, max_tokens=None, stop=None):
            self.calls += 1
            time.sleep(0.8)
            return Completion(text=final("done"), model=model)

    from src.core.orchestrator import Orchestrator

    slow = SlowClient()
    orchestrator = Orchestrator(
        client=slow,  # type: ignore[arg-type]
        index=RagIndex(index_dir=tmp_path / "index", backend="tfidf"),
        sandbox=Sandbox(workdir=tmp_path / "work"),
    )

    started = time.perf_counter()
    # "compute the report" is ambiguous, so it reaches the model tie-break.
    orchestrator.start(TaskRequest(task="compute the report"))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.3, f"start() blocked for {elapsed:.2f}s - it must not route"


def test_events_cleans_up_when_the_consumer_walks_away(tmp_path: Path):
    """A closed browser tab must not pin a run's steps in memory."""
    orchestrator = _orchestrator_with([final("Done.")], tmp_path)
    request = TaskRequest(task="Summarise the report")
    orchestrator.start(request)

    stream = orchestrator.events(request.task_id)
    next(stream)          # take the routing event, then abandon the generator
    stream.close()

    assert request.task_id not in orchestrator._queues


def test_events_replays_a_finished_run(tmp_path: Path):
    """The UI may connect after the run ends; it must still get the trace."""
    orchestrator = _orchestrator_with([final("Done.")], tmp_path)
    request = TaskRequest(task="Summarise the report")
    orchestrator.run(request)

    emitted = list(orchestrator.events(request.task_id))
    assert isinstance(emitted[-1], AgentResult)
    assert emitted[-1].final_text == "Done."


def test_health_reports_routing_table_tools_and_index(tmp_path: Path):
    health = _orchestrator_with([], tmp_path).health()
    assert set(health["routing_table"]) == {"document", "code", "general"}
    assert "search_documents" in health["tools"]
    assert health["index"]["chunks"] == 1
    assert health["inference_endpoint"]


def test_the_model_tier_is_real_not_three_names_for_one_model():
    """The proposal promises "heavy models wake only when needed".

    That claim is only true if the routing table actually resolves to more than
    one model, so it is asserted rather than assumed. If someone collapses the
    tier to fit a smaller machine, this test tells them the pitch line has to
    change too.
    """
    table = Router(use_llm_tiebreak=False).routing_table()
    assert len(set(table.values())) > 1, (
        f"all task types resolve to the same model {table} - the 'smallest model that can "
        "do it well' claim would not be true"
    )
    assert table["document"] != table["general"], "heavy work and cheap lookups must differ"


def test_the_router_classifies_with_the_cheap_model():
    """The tie-break call must not wake the 14B just to label a task."""
    router = Router(use_llm_tiebreak=False)
    assert router.model_for(TaskType.GENERAL) == router.routing_table()["general"]
