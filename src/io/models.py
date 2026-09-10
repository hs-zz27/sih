"""Structured input for H2's deliverable generators.

These are Harkamal-vertical models, not shared contracts - `src/contracts.py`
stays the frozen cross-branch interface. Once M7 lands, the orchestrator (or a
tool inside it) is responsible for turning an `AgentResult` into one of these
before calling `build_approval_note` / `build_thickness_assessment`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.contracts import SourceCitation


class FindingRow(BaseModel):
    """One row of the findings table in the approval note / assessment sheet."""

    item: str = Field(..., description="e.g. 'Shell course 2, grid C4'")
    observation: str = Field(..., description="What was found, in plain language.")
    measured_mm: float | None = None
    nominal_mm: float | None = None
    threshold_mm: float | None = None
    status: str = Field(default="INFO", description="OK | REFER | CRITICAL | INFO")


class ApprovalNoteData(BaseModel):
    """Everything H2's docx builder needs. No file paths - the builder decides those."""

    ref_number: str
    equipment: str
    inspection_date: str
    inspector: str
    findings: list[FindingRow]
    recommendation: str
    sources: list[SourceCitation] = Field(default_factory=list)
    prepared_for: str = "Mangala Petrochem Works (fictional)"
    reviewing_engineer: str = ""
    task_id: str | None = None


class ThicknessAssessmentData(BaseModel):
    """Everything H2's xlsx builder needs to show visible working, not just a verdict."""

    ref_number: str
    equipment: str
    rows: list[FindingRow]
    task_id: str | None = None
