"""Shared data contracts for the Sovereign On-Premise Agentic AI Workbench.

Both verticals code against this module and nothing else:

* ``feat/core-engine`` (Manraj) produces ``AgentStep`` / ``AgentResult`` and
  consumes ``TaskRequest`` / ``Document`` / ``ToolSpec``.
* ``feat/app-io`` (Harkamal) produces ``Document`` / ``Deliverable`` /
  ``AuditEvent`` and renders ``AgentStep`` / ``AgentResult``.

PROPOSAL.md ss5 merge protocol: changing anything in this file requires BOTH of
us to agree. Adding a field with a default is cheap; renaming or removing one
breaks the other branch's build. Prefer stuffing experimental data into the
``metadata`` dicts over editing a signature.

Nothing here may import a model provider SDK, and nothing here may hold a
network handle. These are plain data shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "TaskType",
    "StepStatus",
    "TaskStatus",
    "DeliverableKind",
    "AuditEventType",
    "TaskRequest",
    "Document",
    "IngestResult",
    "SourceCitation",
    "ToolSpec",
    "AgentStep",
    "Deliverable",
    "AgentResult",
    "AuditEvent",
    "NetworkStatus",
    "StreamEvent",
    "new_id",
    "utcnow",
]


def new_id(prefix: str) -> str:
    """Short, human-readable identifier - these end up in logs and in the UI."""
    return f"{prefix}_{uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class TaskType(str, Enum):
    """The router's decision (M2).

    The problem statement asks us to *show* model selection across at least two
    task types. These three are the full set for the MVP - do not add more
    without updating ``models:`` in config.yaml.
    """

    DOCUMENT = "document"
    CODE = "code"
    GENERAL = "general"


class StepStatus(str, Enum):
    """Outcome of one agent iteration.

    A failing tool is ``ERROR`` but is NOT an exception: M3 requires that tool
    failures come back to the agent as an observation so the loop can retry.
    """

    OK = "ok"
    ERROR = "error"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TRUNCATED = "truncated"  # hit max_steps / wall-clock cap, partial result returned


class DeliverableKind(str, Enum):
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    PDF = "pdf"
    OTHER = "other"


class AuditEventType(str, Enum):
    """Everything H4 has to be able to replay after the demo."""

    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    INGEST = "ingest"
    DELIVERABLE = "deliverable"
    SANDBOX_EXEC = "sandbox_exec"
    NETWORK = "network"  # any socket the monitor saw - see AuditEvent.external


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class TaskRequest(BaseModel):
    """What the UI sends to start a run."""

    task: str = Field(..., description="Natural-language instruction from the operator.")
    attachments: list[str] = Field(
        default_factory=list,
        description="Paths of already-uploaded files, relative to the repo root. "
        "The UI uploads via /api/ingest first, then passes the paths here.",
    )
    task_type_hint: TaskType | None = Field(
        default=None,
        description="Operator override for the router. None means the router decides.",
    )
    task_id: str = Field(default_factory=lambda: new_id("task"))


# ---------------------------------------------------------------------------
# Ingestion (H1) -> RAG (M6)
# ---------------------------------------------------------------------------


class Document(BaseModel):
    """One page of one source file, normalised to text.

    Page-level granularity is deliberate: source attribution in the UI has to
    say "SOP-114, page 3", so a document is never a whole-file blob.

    Conventional ``metadata`` keys written by the ingestion pipeline - read
    them, don't assume they exist:

    * ``extraction_method``  - ``"text_layer"`` | ``"ocr"``
    * ``ocr_confidence``     - float 0-1, present only when OCR ran
    * ``filename``           - original upload name
    * ``mime_type``          - e.g. ``application/pdf``
    * ``ingested_at``        - ISO-8601 timestamp
    * ``page_count``         - total pages in the source file
    """

    id: str = Field(default_factory=lambda: new_id("doc"))
    source_path: str = Field(..., description="Path on local disk. Never a URL.")
    page: int = Field(default=1, ge=0, description="1-based page number; 0 = whole file.")
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestResult(BaseModel):
    """Return shape of /api/ingest - what the UI shows in the preview pane."""

    source_path: str
    filename: str
    page_count: int = 0
    ocr_pages: int = Field(default=0, description="Pages that needed OCR (empty text layer).")
    documents: list[Document] = Field(default_factory=list)
    duration_ms: int = 0
    warnings: list[str] = Field(default_factory=list)


class SourceCitation(BaseModel):
    """A retrieved passage, carried through to the UI so every claim is traceable."""

    document_id: str
    source_path: str
    page: int = 1
    snippet: str = ""
    score: float | None = Field(default=None, description="Retrieval score, higher is better.")


# ---------------------------------------------------------------------------
# Tools (M3)
# ---------------------------------------------------------------------------


class ToolSpec(BaseModel):
    """A tool the agent may call.

    ``input_schema`` is a JSON Schema object - it is what gets rendered into the
    model prompt, so keep it small and literal. ``handler`` is excluded from
    serialisation; the API only ever ships the public half over the wire.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
    )
    handler: Callable[..., Any] | None = Field(default=None, exclude=True, repr=False)

    def public(self) -> dict[str, Any]:
        """Serialisable view for the UI and for the prompt."""
        return self.model_dump(exclude={"handler"})


# ---------------------------------------------------------------------------
# Agent output (M4) - the visual centrepiece of the demo
# ---------------------------------------------------------------------------


class AgentStep(BaseModel):
    """One iteration of plan -> tool -> observe.

    Every field here is rendered in the live trace, so an empty ``thought`` or a
    missing ``duration_ms`` shows up as a hole on screen during the demo.
    """

    step_number: int = Field(..., ge=1)
    thought: str = Field(default="", description="The model's reasoning for this step.")
    tool_name: str | None = Field(default=None, description="None on the final answer step.")
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_output: str = Field(default="", description="Observation fed back to the model.")
    status: StepStatus = StepStatus.OK
    duration_ms: int = 0
    model_used: str | None = Field(
        default=None,
        description="Model that produced this step - drives the routing badge (M2).",
    )
    started_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Deliverable(BaseModel):
    """A real file on disk in the served downloads directory (H2)."""

    filename: str
    path: str = Field(..., description="Local path under app.downloads_dir.")
    kind: DeliverableKind = DeliverableKind.OTHER
    size_bytes: int = 0
    download_url: str = Field(
        default="",
        description="Same-origin API path, e.g. /api/deliverables/<filename>. Never external.",
    )
    title: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class AgentResult(BaseModel):
    """Terminal state of a run. This is what /api/tasks returns."""

    task_id: str = Field(default_factory=lambda: new_id("task"))
    status: TaskStatus = TaskStatus.COMPLETED
    final_text: str = ""
    steps: list[AgentStep] = Field(default_factory=list)
    deliverables: list[Deliverable] = Field(default_factory=list)
    task_type: TaskType = TaskType.GENERAL
    model_used: str = Field(default="", description="Model the router selected for this task.")
    routing_reason: str = Field(default="", description="Why - shown next to the badge (M2).")
    sources: list[SourceCitation] = Field(default_factory=list)
    total_duration_ms: int = 0
    error: str | None = Field(default=None, description="Set when status is FAILED.")
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Sovereignty proof (H4)
# ---------------------------------------------------------------------------


class AuditEvent(BaseModel):
    """Append-only record of one action. The audit log is the evidence."""

    id: str = Field(default_factory=lambda: new_id("evt"))
    timestamp: datetime = Field(default_factory=utcnow)
    event_type: AuditEventType
    actor: str = Field(default="system", description="agent | ui | system | sandbox")
    summary: str = Field(default="", description="One line, rendered in the audit panel.")
    detail: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    external: bool = Field(
        default=False,
        description="True only if this touched a host outside audit.allowed_hosts. "
        "Any True here is a P0 bug - it is the number the judges will look at.",
    )


class NetworkStatus(BaseModel):
    """Live counter for the network monitor panel."""

    external_calls: int = Field(default=0, description="THE number. Must read 0.")
    total_operations: int = 0
    local_calls: int = 0
    offline_since: datetime | None = Field(
        default=None, description="When the monitor last saw the machine go offline."
    )
    offline_duration_s: int = 0
    monitor: str = Field(
        default="",
        description="How we know - e.g. 'lsof -i poll @1s'. Must answer 'how do you know?'.",
    )
    allowed_hosts: list[str] = Field(default_factory=list)
    violations: list[AuditEvent] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Streaming envelope (used by the SSE trace endpoint)
# ---------------------------------------------------------------------------


class StreamEvent(BaseModel):
    """One server-sent event on /api/tasks/{task_id}/stream.

    ``event`` values: ``routing`` | ``step`` | ``result`` | ``error`` | ``done``.
    The UI switches on it; the payload is the matching model above.
    """

    event: str
    task_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)
