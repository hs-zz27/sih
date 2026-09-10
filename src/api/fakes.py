"""Canned Step-0 data.

Everything here is fiction. It exists so the UI (H3) and the ingestion /
deliverable paths (H1, H2) can be built and tested from hour one, before the
agent loop exists.

M7 deletes this file. Nothing outside ``src/api/main.py`` may import it, and
nothing in it may be reachable once the real orchestrator is wired in.

The scenario is the demo scenario: a scanned inspection report for a fictional
heat exchanger comes in, the agent cross-checks it against the SOP corpus, and
a Word approval note comes out.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from src import config
from src.contracts import (
    AgentResult,
    AgentStep,
    AuditEvent,
    AuditEventType,
    Deliverable,
    DeliverableKind,
    Document,
    NetworkStatus,
    SourceCitation,
    StepStatus,
    TaskStatus,
    TaskType,
    utcnow,
)

FAKE_REPORT = "data/corpus/INSP-2026-0412_E-4102.pdf"
FAKE_SOP = "data/corpus/SOP-114_heat_exchanger_inspection.pdf"
FAKE_MANUAL = "data/corpus/MM-07_shell_and_tube_maintenance.pdf"


def fake_documents() -> list[Document]:
    """Three pages of a scanned inspection report, as H1 will emit them."""
    pages = [
        (
            "MANGALA PETROCHEM WORKS (FICTIONAL)\n"
            "INSPECTION REPORT  Ref: INSP-2026-0412\n"
            "Equipment: Shell-and-tube heat exchanger E-4102, CDU-2 overhead train\n"
            "Inspection date: 2026-04-12   Inspector: R. Nayak (Cert. NDT-II)\n"
            "Method: Ultrasonic thickness survey, 48 grid points.",
            "ocr",
            0.91,
        ),
        (
            "FINDINGS\n"
            "1. Shell course 2, grid C4: wall thickness 7.1 mm against nominal 9.5 mm.\n"
            "   Measured loss 25.3%. Corrosion allowance remaining: 0.6 mm.\n"
            "2. Tube bundle: 3 tubes plugged at the north header, no new leakage.\n"
            "3. Insulation damaged over a 1.2 m span, CUI suspected at the saddle.",
            "ocr",
            0.87,
        ),
        (
            "RECOMMENDATION\n"
            "Shell course 2 is below the SOP-114 retirement threshold and requires\n"
            "engineering review before the next run. Re-inspection interval should be\n"
            "shortened to 6 months pending a fitness-for-service assessment.",
            "ocr",
            0.93,
        ),
    ]
    return [
        Document(
            source_path=FAKE_REPORT,
            page=index,
            text=text,
            metadata={
                "extraction_method": method,
                "ocr_confidence": confidence,
                "filename": Path(FAKE_REPORT).name,
                "mime_type": "application/pdf",
                "page_count": len(pages),
                "ingested_at": utcnow().isoformat(),
                "stub": True,
            },
        )
        for index, (text, method, confidence) in enumerate(pages, start=1)
    ]


def fake_sources() -> list[SourceCitation]:
    return [
        SourceCitation(
            document_id="doc_stub_sop114_p3",
            source_path=FAKE_SOP,
            page=3,
            snippet=(
                "SOP-114 ss4.2 - Any shell course measuring below 80% of nominal wall "
                "thickness shall be referred for engineering review prior to return to "
                "service. Retirement threshold for CDU overhead service: 7.6 mm."
            ),
            score=0.88,
        ),
        SourceCitation(
            document_id="doc_stub_mm07_p11",
            source_path=FAKE_MANUAL,
            page=11,
            snippet=(
                "MM-07 ss9 - Where corrosion under insulation is suspected, strip and "
                "inspect a minimum 500 mm beyond the damaged span before recladding."
            ),
            score=0.74,
        ),
    ]


def fake_steps(model: str = "qwen2.5:7b-instruct") -> list[AgentStep]:
    """A believable five-step trace: read -> retrieve -> compute -> write -> finish."""
    raw = [
        (
            "The operator attached a scanned inspection report. Read the extracted "
            "text before deciding anything.",
            "read_file",
            {"path": FAKE_REPORT},
            "3 pages extracted (OCR). Findings: shell course 2 at 7.1 mm vs 9.5 mm "
            "nominal; 3 tubes plugged; insulation damage over 1.2 m.",
            StepStatus.OK,
            820,
        ),
        (
            "7.1 mm may be below the retirement threshold. Check SOP-114 rather than "
            "guessing the limit.",
            "search_documents",
            {"query": "shell course retirement thickness threshold CDU overhead", "top_k": 5},
            "SOP-114 p.3: refer below 80% of nominal; retirement threshold 7.6 mm. "
            "MM-07 p.11: strip 500 mm beyond damaged insulation.",
            StepStatus.OK,
            1340,
        ),
        (
            "Compute the remaining margin against both the percentage rule and the "
            "absolute threshold, and show the working in the spreadsheet.",
            "run_python",
            {"code": "nominal=9.5; measured=7.1; print(measured/nominal, measured-7.6)"},
            "0.7473684210526316 -0.5  ->  74.7% of nominal (below the 80% rule) and "
            "0.5 mm below the 7.6 mm retirement threshold.",
            StepStatus.OK,
            410,
        ),
        (
            "Both rules are breached, so this is a referral, not an approval to run. "
            "Draft the approval note with that recommendation and cite the SOP.",
            "write_file",
            {"path": "data/downloads/approval_note_INSP-2026-0412.docx"},
            "Wrote approval_note_INSP-2026-0412.docx (18.4 kB) and "
            "thickness_assessment_E-4102.xlsx (9.1 kB).",
            StepStatus.OK,
            960,
        ),
        (
            "Deliverables are on disk and every figure is traceable to a source. "
            "Finishing.",
            None,
            {},
            "",
            StepStatus.OK,
            180,
        ),
    ]

    started = utcnow()
    steps: list[AgentStep] = []
    for index, (thought, tool, tool_input, output, status, duration) in enumerate(raw, start=1):
        steps.append(
            AgentStep(
                step_number=index,
                thought=thought,
                tool_name=tool,
                tool_input=tool_input,
                tool_output=output,
                status=status,
                duration_ms=duration,
                model_used=model,
                started_at=started,
                metadata={"stub": True},
            )
        )
        started = started + timedelta(milliseconds=duration)
    return steps


def _materialise(filename: str, body: str) -> Deliverable:
    """Write a placeholder into the downloads dir so the UI's download button works.

    H2 replaces these with real python-docx / openpyxl output. Extension is kept
    honest: the placeholder is a .txt sitting next to the intended name so nobody
    mistakes it for a finished document.
    """
    downloads = config.get_path("app.downloads_dir")
    placeholder = downloads / f"{filename}.PLACEHOLDER.txt"
    if not placeholder.exists():
        placeholder.write_text(body, encoding="utf-8")

    kind = DeliverableKind.OTHER
    suffix = Path(filename).suffix.lstrip(".").lower()
    if suffix in {kind_option.value for kind_option in DeliverableKind}:
        kind = DeliverableKind(suffix)

    return Deliverable(
        filename=placeholder.name,
        path=str(placeholder),
        kind=kind,
        size_bytes=placeholder.stat().st_size,
        download_url=f"/api/deliverables/{placeholder.name}",
        title=f"[STUB] {filename}",
    )


def fake_deliverables() -> list[Deliverable]:
    return [
        _materialise(
            "approval_note_INSP-2026-0412.docx",
            "STUB DELIVERABLE - replaced by src/io/deliverables/docx.py (H2).\n\n"
            "APPROVAL NOTE - Ref INSP-2026-0412 / E-4102\n"
            "Recommendation: REFER FOR ENGINEERING REVIEW. Shell course 2 measures "
            "7.1 mm (74.7% of nominal), below the SOP-114 threshold of 7.6 mm.\n",
        ),
        _materialise(
            "thickness_assessment_E-4102.xlsx",
            "STUB DELIVERABLE - replaced by src/io/deliverables/xlsx.py (H2).\n\n"
            "nominal_mm=9.5  measured_mm=7.1  loss_pct=25.3  threshold_mm=7.6  "
            "margin_mm=-0.5\n",
        ),
    ]


def fake_result(
    task_id: str,
    task_type: TaskType = TaskType.DOCUMENT,
    model: str = "qwen2.5:7b-instruct",
) -> AgentResult:
    steps = fake_steps(model)
    return AgentResult(
        task_id=task_id,
        status=TaskStatus.COMPLETED,
        final_text=(
            "Heat exchanger E-4102 cannot be approved for continued service as "
            "inspected.\n\n"
            "Shell course 2 (grid C4) measures 7.1 mm against a 9.5 mm nominal wall - "
            "74.7% of nominal, which breaches the 80% referral rule in SOP-114 ss4.2, "
            "and 0.5 mm below the 7.6 mm retirement threshold for CDU overhead "
            "service. Corrosion under insulation is additionally suspected at the "
            "saddle over a 1.2 m span.\n\n"
            "Recommendation: refer to engineering for a fitness-for-service "
            "assessment before return to service, strip and inspect 500 mm beyond "
            "the damaged insulation per MM-07 ss9, and shorten the re-inspection "
            "interval to 6 months.\n\n"
            "Approval note and thickness assessment are attached."
        ),
        steps=steps,
        deliverables=fake_deliverables(),
        task_type=task_type,
        model_used=model,
        routing_reason=(
            "Classified as a document task: the request references an attached "
            "inspection report and asks for an approval note."
        ),
        sources=fake_sources(),
        total_duration_ms=sum(step.duration_ms for step in steps),
        completed_at=utcnow(),
    )


def fake_audit_events(task_id: str | None = None) -> list[AuditEvent]:
    allowed = config.get("inference.endpoint", "http://127.0.0.1:11434")
    return [
        AuditEvent(
            event_type=AuditEventType.INGEST,
            actor="ui",
            summary=f"Ingested {Path(FAKE_REPORT).name} - 3 pages, 3 via OCR",
            detail={"source_path": FAKE_REPORT, "pages": 3, "ocr_pages": 3},
            task_id=task_id,
        ),
        AuditEvent(
            event_type=AuditEventType.MODEL_CALL,
            actor="agent",
            summary="qwen2.5:7b-instruct - 1 request, 1,204 tokens",
            detail={"endpoint": allowed, "model": "qwen2.5:7b-instruct", "tokens": 1204},
            task_id=task_id,
        ),
        AuditEvent(
            event_type=AuditEventType.TOOL_CALL,
            actor="agent",
            summary="search_documents - 5 chunks from the local Chroma index",
            detail={"tool": "search_documents", "top_k": 5, "hits": 5},
            task_id=task_id,
        ),
        AuditEvent(
            event_type=AuditEventType.SANDBOX_EXEC,
            actor="sandbox",
            summary="run_python - exit 0 in 0.41 s, network disabled",
            detail={"exit_code": 0, "duration_ms": 410, "network": False},
            task_id=task_id,
        ),
        AuditEvent(
            event_type=AuditEventType.DELIVERABLE,
            actor="system",
            summary="Wrote approval_note_INSP-2026-0412.docx",
            detail={"filename": "approval_note_INSP-2026-0412.docx", "bytes": 18432},
            task_id=task_id,
        ),
    ]


def fake_network_status(events: list[AuditEvent]) -> NetworkStatus:
    """The stub always reports zero egress - H4 replaces this with a real monitor.

    Until ``src/io/audit.py`` lands, this number is an assertion, which is
    exactly what PROPOSAL.md ss7 says is not good enough. Treat it as a
    placeholder shape for the UI panel, not as evidence.
    """
    local = sum(1 for event in events if not event.external)
    return NetworkStatus(
        external_calls=sum(1 for event in events if event.external),
        total_operations=len(events),
        local_calls=local,
        offline_since=utcnow() - timedelta(minutes=42),
        offline_duration_s=42 * 60,
        monitor="STUB - not yet backed by a real capture (H4)",
        allowed_hosts=config.get("audit.allowed_hosts", []),
        violations=[event for event in events if event.external],
    )
