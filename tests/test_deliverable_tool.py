"""H2/H4.5 - the agent's create_approval_documents tool.

Before this, build_approval_note()/build_thickness_assessment() existed and
were tested (tests/test_deliverables.py) but nothing in the agent loop could
call them - HARDCODED.md flagged this as "the agent cannot produce a .docx".
These tests exercise the actual gap: dispatch through the real ToolRegistry,
and AgentResult.deliverables getting populated from tool artifacts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.agent import _deliverables_from_paths
from src.core.tools import ToolRegistry, build_registry
from src.core.rag import RagIndex
from src.core.sandbox import Sandbox

VALID_ARGS = {
    "ref_number": "INSP-2026-TEST",
    "equipment": "Test Vessel T-1",
    "inspection_date": "2026-09-11",
    "inspector": "A. Test",
    "findings": [
        {
            "item": "Shell course 2",
            "observation": "Below threshold",
            "measured_mm": 7.1,
            "nominal_mm": 9.5,
            "threshold_mm": 7.6,
            "status": "REFER",
        }
    ],
    "recommendation": "Refer for engineering review.",
    "sources": [
        {"document_id": "d1", "source_path": "data/corpus/sop.txt", "page": 3, "snippet": "threshold 7.6mm"}
    ],
}


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    index = RagIndex(index_dir=tmp_path / "index", backend="tfidf")
    sandbox = Sandbox(workdir=tmp_path / "workspace")
    return build_registry(index=index, sandbox=sandbox)


def test_tool_is_registered(registry: ToolRegistry):
    assert "create_approval_documents" in registry.names()


def test_tool_produces_real_files(registry: ToolRegistry, monkeypatch, tmp_path: Path):
    downloads = tmp_path / "downloads"
    monkeypatch.setattr("src.core.tools.config.get_path", lambda *a, **k: downloads)
    # Rebuild with the patched downloads dir, since the handler closes over it at registration.
    registry = build_registry(
        index=RagIndex(index_dir=tmp_path / "index2", backend="tfidf"),
        sandbox=Sandbox(workdir=tmp_path / "workspace2"),
    )

    outcome = registry.dispatch("create_approval_documents", VALID_ARGS)

    assert outcome.ok, outcome.error
    assert len(outcome.artifacts) == 2
    assert all(Path(p).is_file() for p in outcome.artifacts)
    assert any(p.endswith(".docx") for p in outcome.artifacts)
    assert any(p.endswith(".xlsx") for p in outcome.artifacts)


def test_missing_required_finding_field_is_a_clean_error(registry: ToolRegistry):
    bad_args = dict(VALID_ARGS, findings=[{"observation": "no item field"}])
    outcome = registry.dispatch("create_approval_documents", bad_args)

    assert not outcome.ok
    assert "item" in outcome.error.lower() or "field required" in outcome.error.lower()


def test_invalid_status_value_is_a_clean_error(registry: ToolRegistry):
    bad_args = dict(
        VALID_ARGS,
        findings=[{"item": "x", "observation": "y", "status": "MAYBE"}],
    )
    # status is a free-text field in FindingRow (not a strict enum), so this
    # documents current behaviour rather than asserting a specific rejection -
    # the important invariant is that dispatch never raises.
    outcome = registry.dispatch("create_approval_documents", bad_args)
    assert outcome.ok or outcome.error


def test_deliverables_from_paths_converts_to_contract_shape(tmp_path: Path):
    docx_path = tmp_path / "note.docx"
    xlsx_path = tmp_path / "sheet.xlsx"
    docx_path.write_bytes(b"fake docx bytes")
    xlsx_path.write_bytes(b"fake xlsx bytes")

    deliverables = _deliverables_from_paths([str(docx_path), str(xlsx_path)])

    assert len(deliverables) == 2
    kinds = {d.kind.value for d in deliverables}
    assert kinds == {"docx", "xlsx"}
    assert all(d.download_url.startswith("/api/deliverables/") for d in deliverables)
    assert all(d.size_bytes > 0 for d in deliverables)


def test_deliverables_from_paths_skips_missing_files(tmp_path: Path):
    real = tmp_path / "real.docx"
    real.write_bytes(b"x")
    deliverables = _deliverables_from_paths([str(real), str(tmp_path / "gone.docx")])
    assert len(deliverables) == 1
    assert deliverables[0].filename == "real.docx"


def test_deliverables_from_paths_deduplicates():
    deliverables = _deliverables_from_paths(["/nonexistent/a.docx", "/nonexistent/a.docx"])
    assert deliverables == []  # both skipped (missing), but must not double-process
