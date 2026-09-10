"""FastAPI application - Step 0 stubs.

Every route below returns hardcoded data shaped exactly like ``src/contracts``.
That is the point: H1-H5 and M1-M6 get built against this at the same time, and
M7 swaps the fake calls for real orchestrator calls without the interface layer
changing a line.

Rules for this file:

* Response models never change during M7 - only the bodies do.
* Anything still fake is marked ``# STUB (M7)`` or ``# STUB (H<n>)`` so the
  remaining fakes are greppable: ``grep -rn "STUB (" src/``.
* No hardcoded hosts or paths - read them from ``src.config``.

Run it with:  ``.venv/bin/uvicorn src.api.main:app --reload``
"""

from __future__ import annotations

import asyncio
import shutil
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
    Deliverable,
    Document,
    IngestResult,
    NetworkStatus,
    StreamEvent,
    TaskRequest,
    TaskType,
    ToolSpec,
    new_id,
    utcnow,
)

app = FastAPI(
    title=config.get("app.name", "Sovereign AI Workbench"),
    version="0.1.0-step0",
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

# In-memory task store. Step 0 only - it exists so the SSE trace endpoint has
# something to look up after POST /api/tasks. M7 replaces it with the real
# orchestrator's run registry.
_TASKS: dict[str, AgentResult] = {}


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": app.version, "time": utcnow().isoformat()}


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

    STUB (M3): mirrors the five tools M3 will register. Kept here so the UI can
    render tool names and schemas before the registry exists.
    """
    return [
        ToolSpec(
            name="read_file",
            description="Read a text file from the local workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        ToolSpec(
            name="write_file",
            description="Write a text file into the workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
        ToolSpec(
            name="list_files",
            description="List files under a workspace directory.",
            input_schema={
                "type": "object",
                "properties": {"directory": {"type": "string"}},
                "required": ["directory"],
            },
        ),
        ToolSpec(
            name="search_documents",
            description=(
                "Semantic search over the local SOP/manual corpus. Returns passages "
                "with their source document and page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": config.get("rag.top_k", 5)},
                },
                "required": ["query"],
            },
        ),
        ToolSpec(
            name="run_python",
            description="Execute Python in a sandboxed subprocess with no network access.",
            input_schema={
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# H1 - ingestion
# ---------------------------------------------------------------------------


@app.post("/api/ingest", response_model=IngestResult)
async def ingest(file: UploadFile) -> IngestResult:
    """Accept a PDF or image upload and return page-level Documents.

    The upload half is real - the file genuinely lands in ``app.uploads_dir`` so
    the UI's preview pane has something to point at.

    STUB (H1): the extracted text is canned. ``src/io/ingest.py`` replaces the
    body with PyMuPDF text extraction plus Tesseract OCR fallback.
    """
    uploads = config.get_path("app.uploads_dir")
    filename = Path(file.filename or "upload.bin").name  # strip any client path
    destination = uploads / f"{new_id('up')}_{filename}"

    with destination.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)

    documents = fakes.fake_documents()
    for document in documents:
        document.source_path = str(destination)
        document.metadata["filename"] = filename

    return IngestResult(
        source_path=str(destination),
        filename=filename,
        page_count=len(documents),
        ocr_pages=len(documents),
        documents=documents,
        duration_ms=1450,
        warnings=["STUB (H1): extracted text is canned, OCR has not run."],
    )


@app.get("/api/documents", response_model=list[Document])
def list_documents() -> list[Document]:
    """Indexed corpus, for the source-attribution browser.

    STUB (M6): served from fixtures until the Chroma index is populated.
    """
    return fakes.fake_documents()


# ---------------------------------------------------------------------------
# M2/M4 - tasks
# ---------------------------------------------------------------------------


@app.post("/api/tasks", response_model=AgentResult)
def create_task(request: TaskRequest) -> AgentResult:
    """Run a task to completion and return the full result.

    STUB (M7): returns a canned five-step trace. The routing decision is faked
    from ``task_type_hint`` or a keyword sniff so the UI badge has something
    non-constant to render.
    """
    task_type = request.task_type_hint or _guess_task_type(request.task)
    model = config.get(f"models.{task_type.value}", "qwen2.5:7b-instruct")

    result = fakes.fake_result(request.task_id, task_type=task_type, model=model)
    _TASKS[result.task_id] = result
    return result


@app.get("/api/tasks/{task_id}", response_model=AgentResult)
def get_task(task_id: str) -> AgentResult:
    result = _TASKS.get(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown task {task_id}")
    return result


@app.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: str) -> StreamingResponse:
    """Server-sent events for the live step trace - H3's visual centrepiece.

    Event names: ``routing`` | ``step`` | ``result`` | ``done``.

    STUB (M7): replays the stored result with a delay between steps so the trace
    animates the way a real run will. The event shapes are final.
    """
    result = _TASKS.get(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown task {task_id}")

    async def event_stream() -> AsyncIterator[str]:
        def encode(event: StreamEvent) -> str:
            return f"event: {event.event}\ndata: {event.model_dump_json()}\n\n"

        yield encode(
            StreamEvent(
                event="routing",
                task_id=task_id,
                payload={
                    "task_type": result.task_type.value,
                    "model_used": result.model_used,
                    "reason": result.routing_reason,
                },
            )
        )

        for step in result.steps:
            await asyncio.sleep(0.6)  # STUB (M7): stands in for real model latency
            yield encode(
                StreamEvent(event="step", task_id=task_id, payload=step.model_dump(mode="json"))
            )

        yield encode(
            StreamEvent(event="result", task_id=task_id, payload=result.model_dump(mode="json"))
        )
        yield encode(StreamEvent(event="done", task_id=task_id))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _guess_task_type(task: str) -> TaskType:
    """Crude keyword router.

    STUB (M2): ``src/core/router.py`` replaces this with a model-backed
    classification. The contract - a TaskType plus a human-readable reason - does
    not change.
    """
    lowered = task.lower()
    code_words = ("python", "script", "code", "calculate", "compute", "plot", "debug")
    document_words = ("report", "approval", "note", "sop", "inspection", "docx", "summar")
    if any(word in lowered for word in code_words):
        return TaskType.CODE
    if any(word in lowered for word in document_words):
        return TaskType.DOCUMENT
    return TaskType.GENERAL


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
    """Append-only action log.

    STUB (H4): served from fixtures until ``src/io/audit.py`` writes the real
    JSONL log.
    """
    return fakes.fake_audit_events(task_id)[:limit]


@app.get("/api/network", response_model=NetworkStatus)
def network_status() -> NetworkStatus:
    """The number the whole submission rests on: external calls.

    STUB (H4): currently an assertion, not evidence. H4 backs it with a real
    ``lsof -i`` poll or packet capture, because "how do you know?" is the
    question the judges will ask.
    """
    return fakes.fake_network_status(fakes.fake_audit_events())


# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host=config.get("server.host", "127.0.0.1"),
        port=int(config.get("server.port", 8000)),
    )
