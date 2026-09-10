"""Demo scaffolding tests.

The behaviour these lock down is not "does the demo look good" - it is
"can a replay ever be mistaken for a live run". That is the property the
pitch's credibility rests on, so it is tested rather than trusted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.contracts import AgentResult, AgentStep, StepStatus, TaskStatus, TaskType
from src.core import demo
from src.core.agent import strip_reasoning


# ---------------------------------------------------------------------------
# Reasoning-model output
# ---------------------------------------------------------------------------


def test_think_block_is_stripped():
    assert strip_reasoning("<think>pondering</think>answer") == "answer"


def test_braces_inside_think_do_not_confuse_the_parser():
    """Qwen3.5 reasons before answering, and reasoning contains braces."""
    from src.core.agent import _parse_decision

    reply = (
        '<think>Maybe {"tool": "write_file"} would work, but no - I need to search first.</think>'
        '{"thought": "search", "tool": "search_documents", "tool_input": {"query": "threshold"}}'
    )
    decision = _parse_decision(reply)
    assert decision is not None
    assert decision.tool == "search_documents", "the decoy in the reasoning must not win"


def test_unclosed_think_block_is_handled():
    """A truncated generation leaves <think> open; it must not crash parsing."""
    assert strip_reasoning("<think>cut off mid-thought") == ""


def test_reply_without_think_tags_is_untouched():
    assert strip_reasoning('{"tool": null}') == '{"tool": null}'


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def test_every_preset_is_well_formed():
    assert demo.presets()
    for item in demo.presets():
        assert item.id and item.label and item.task.strip()
        assert len(item.task) > 40, "a rehearsed prompt should be specific"


def test_preset_lookup_by_id():
    assert demo.preset("approval_note") is not None
    assert demo.preset("does_not_exist") is None


def test_preset_matching_is_exact():
    """A judge who edits the prompt must get the real outcome, not a replay."""
    item = demo.presets()[0]
    assert demo.match_preset(item.task) == item.id
    assert demo.match_preset("  " + item.task.upper() + " ") == item.id, "whitespace/case only"
    assert demo.match_preset(item.task + " and also make it shorter") is None
    assert demo.match_preset("something else entirely") is None


# ---------------------------------------------------------------------------
# Replay labelling - the honesty property
# ---------------------------------------------------------------------------


def _result(task_id: str = "task_live") -> AgentResult:
    return AgentResult(
        task_id=task_id,
        status=TaskStatus.COMPLETED,
        final_text="Remaining wall is 75.8 percent of nominal. Escalate.",
        steps=[
            AgentStep(step_number=1, thought="look it up", tool_name="search_documents", status=StepStatus.OK),
            AgentStep(step_number=2, thought="done", status=StepStatus.OK),
        ],
        task_type=TaskType.DOCUMENT,
        model_used="qwen3.5:4b",
        routing_reason="document signals",
    )


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(demo, "cache_dir", lambda: tmp_path)
    return tmp_path


def test_record_then_replay_roundtrip(cache: Path):
    demo.record("approval_note", _result())
    assert demo.has_cached("approval_note")
    assert "approval_note" in demo.cached_runs()

    replay = demo.load_replay("approval_note", "task_new")
    assert replay is not None
    assert replay.task_id == "task_new", "a replay adopts the new task id"


def test_a_replay_is_unmistakably_labelled(cache: Path):
    """The whole fallback is only defensible because of this test."""
    demo.record("approval_note", _result())
    replay = demo.load_replay("approval_note", "task_new")

    assert replay is not None
    assert replay.final_text.startswith(demo.REPLAY_BANNER)
    assert "CACHED REPLAY" in replay.final_text
    assert "cached replay" in replay.routing_reason.lower()
    assert all(step.metadata.get("demo_replay") is True for step in replay.steps), (
        "every step must carry the marker, since the UI renders steps individually"
    )


def test_the_original_answer_survives_the_banner(cache: Path):
    demo.record("approval_note", _result())
    replay = demo.load_replay("approval_note", "task_new")
    assert "75.8 percent" in replay.final_text


def test_missing_cache_returns_none_rather_than_inventing_one(cache: Path):
    assert demo.load_replay("approval_note", "task_new") is None


def test_a_corrupt_cache_does_not_mask_the_real_failure(cache: Path):
    (cache / "approval_note.json").write_text("{not json", encoding="utf-8")
    assert demo.load_replay("approval_note", "task_new") is None


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


def _orchestrator(tmp_path: Path, client):
    from src.core.orchestrator import Orchestrator
    from src.core.rag import RagIndex
    from src.core.sandbox import Sandbox

    return Orchestrator(
        client=client,
        index=RagIndex(index_dir=tmp_path / "index", backend="tfidf"),
        sandbox=Sandbox(workdir=tmp_path / "work"),
    )


class DeadClient:
    def chat(self, **_kwargs):
        from src.core.llm import InferenceError

        raise InferenceError("Cannot reach local inference server at http://127.0.0.1:11434")


def test_a_failed_preset_run_replays_when_a_cache_exists(tmp_path: Path, cache: Path):
    from src.contracts import TaskRequest

    demo.record("approval_note", _result())
    orchestrator = _orchestrator(tmp_path, DeadClient())

    result = orchestrator.run(TaskRequest(task=demo.preset("approval_note").task))

    assert result.status is TaskStatus.COMPLETED
    assert result.final_text.startswith(demo.REPLAY_BANNER)


def test_a_failed_NON_preset_run_is_never_replaced(tmp_path: Path, cache: Path):
    """The critical case: an improvised prompt must show the real failure."""
    from src.contracts import TaskRequest

    demo.record("approval_note", _result())
    orchestrator = _orchestrator(tmp_path, DeadClient())

    result = orchestrator.run(TaskRequest(task="Something a judge just made up on the spot"))

    assert result.status is TaskStatus.FAILED
    assert demo.REPLAY_BANNER not in result.final_text
    assert "ollama" in result.final_text.lower()


def test_disabling_the_fallback_restores_the_real_failure(
    tmp_path: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.contracts import TaskRequest

    demo.record("approval_note", _result())
    monkeypatch.setattr(demo, "fallback_enabled", lambda: False)
    orchestrator = _orchestrator(tmp_path, DeadClient())

    result = orchestrator.run(TaskRequest(task=demo.preset("approval_note").task))
    assert result.status is TaskStatus.FAILED


def test_demo_status_reports_what_is_pinned():
    status = demo.status()
    assert set(status) >= {"enabled", "deterministic", "fallback_enabled", "presets", "cached_runs"}
    assert status["presets"], "the UI needs the preset list"
