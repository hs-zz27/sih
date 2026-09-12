"""M3 - tool registry.

Tools are the agent's only way to affect anything. Each one is a ``ToolSpec``
(name, description, JSON Schema, handler) and is dispatched by name.

The rule that matters most here is failure handling: **a tool error must come
back to the agent as an observation, never as an exception**. If a tool raises,
the loop dies mid-demo. So :meth:`ToolRegistry.dispatch` catches everything,
including a bad tool name and malformed arguments, and returns a ``ToolOutcome``
describing what went wrong and how to fix it. The model reads that and retries.

Filesystem tools are confined to an explicit set of roots. This is a sovereignty
property as much as a safety one: the agent can reach the corpus, the uploads
and its own workspace, and nothing else on the machine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from src import config
from src.contracts import SourceCitation, ToolSpec
from src.core.rag import RagIndex, get_index
from src.core.sandbox import Sandbox, get_sandbox
from src.io.deliverables.docx import build_approval_note
from src.io.deliverables.xlsx import build_thickness_assessment
from src.io.models import ApprovalNoteData, FindingRow, ThicknessAssessmentData

# Text files the agent is allowed to read. Anything else is refused with an
# explanation rather than being silently decoded as mojibake.
_READABLE_SUFFIXES = {".txt", ".md", ".json", ".csv", ".yaml", ".yml", ".log", ".py", ".xml", ".html"}

_MAX_READ_CHARS = 20_000
_MAX_LISTING = 200


@dataclass
class ToolOutcome:
    """Result of one tool call.

    Internal to core - it is folded into an ``AgentStep`` before crossing the
    API boundary, so extending it needs no contract change.
    """

    ok: bool
    output: str = ""
    error: str | None = None
    citations: list[SourceCitation] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_observation(self) -> str:
        """What the model sees. Errors are phrased as instructions, not blame."""
        if self.ok:
            return self.output or "(tool returned no output)"
        return f"ERROR: {self.error}"


class ToolError(Exception):
    """Raised inside a handler to signal a clean, explainable failure."""


class ToolRegistry:
    """Name -> ToolSpec, with schema-checked dispatch."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # -- registration -----------------------------------------------------

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool {spec.name!r} is already registered")
        if spec.handler is None:
            raise ValueError(f"Tool {spec.name!r} has no handler")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name] for name in self.names()]

    def public_specs(self) -> list[dict[str, Any]]:
        """Serialisable view for the UI and the prompt."""
        return [spec.public() for spec in self.specs()]

    def describe_for_prompt(self) -> str:
        """Compact tool documentation injected into the system prompt.

        Kept terse on purpose: a 7B model given three paragraphs per tool starts
        narrating the documentation back instead of calling anything.
        """
        blocks: list[str] = []
        for spec in self.specs():
            properties = (spec.input_schema or {}).get("properties", {}) or {}
            required = set((spec.input_schema or {}).get("required", []) or [])
            arguments = ", ".join(
                f"{name}: {meta.get('type', 'string')}{'' if name in required else ' (optional)'}"
                for name, meta in properties.items()
            )
            blocks.append(f"- {spec.name}({arguments})\n    {spec.description}")
        return "\n".join(blocks)

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, name: str, arguments: dict[str, Any] | None) -> ToolOutcome:
        """Call a tool by name. Never raises - every failure becomes an outcome."""
        spec = self._tools.get(name)
        if spec is None:
            return ToolOutcome(
                ok=False,
                error=(
                    f"No tool named {name!r}. Available tools: {', '.join(self.names())}. "
                    "Use one of those exact names."
                ),
            )

        arguments = arguments if isinstance(arguments, dict) else {}

        missing = [
            key
            for key in (spec.input_schema or {}).get("required", []) or []
            if key not in arguments or arguments[key] is None
        ]
        if missing:
            return ToolOutcome(
                ok=False,
                error=(
                    f"{name} is missing required argument(s): {', '.join(missing)}. "
                    f"Expected schema: {json.dumps(spec.input_schema)}"
                ),
            )

        try:
            arguments = _coerce_arguments(arguments, spec.input_schema or {})
        except ToolError as exc:
            return ToolOutcome(ok=False, error=f"{name}: {exc}")

        try:
            outcome = spec.handler(**arguments)  # type: ignore[misc]
        except ToolError as exc:
            return ToolOutcome(ok=False, error=str(exc))
        except TypeError as exc:
            return ToolOutcome(
                ok=False,
                error=(
                    f"{name} was called with arguments it does not accept ({exc}). "
                    f"Expected schema: {json.dumps(spec.input_schema)}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            return ToolOutcome(ok=False, error=f"{name} failed with {exc.__class__.__name__}: {exc}")

        if not isinstance(outcome, ToolOutcome):
            return ToolOutcome(ok=True, output=str(outcome))
        return outcome


def _coerce_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Nudge argument types towards what the schema declares.

    Small models emit ``{"top_k": "3"}`` and ``{"code": 12}`` constantly. Each
    one would otherwise surface as a confusing ``AttributeError`` from deep
    inside a handler, and cost a demo turn. Coerce what is unambiguous, and
    refuse the rest with a message naming the expected type.
    """
    properties = (schema or {}).get("properties", {}) or {}
    coerced = dict(arguments)

    for key, value in arguments.items():
        expected = (properties.get(key) or {}).get("type")
        if expected is None or value is None:
            continue

        if expected == "string" and not isinstance(value, str):
            if isinstance(value, (int, float, bool)):
                coerced[key] = str(value)
            else:
                raise ToolError(f"{key} must be a string, got {type(value).__name__}.")

        elif expected == "integer" and not isinstance(value, int):
            try:
                coerced[key] = int(str(value).strip())
            except (TypeError, ValueError):
                raise ToolError(f"{key} must be an integer, got {value!r}.") from None

        elif expected == "number" and not isinstance(value, (int, float)):
            try:
                coerced[key] = float(str(value).strip())
            except (TypeError, ValueError):
                raise ToolError(f"{key} must be a number, got {value!r}.") from None

        elif expected == "boolean" and not isinstance(value, bool):
            text = str(value).strip().lower()
            if text in {"true", "1", "yes"}:
                coerced[key] = True
            elif text in {"false", "0", "no"}:
                coerced[key] = False
            else:
                raise ToolError(f"{key} must be true or false, got {value!r}.")

    return coerced


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


def allowed_roots(workspace: Path | None = None) -> list[Path]:
    """Directories the filesystem tools may touch.

    ``workspace`` overrides the configured sandbox directory and is listed
    first, so it is where a relative write lands. The registry passes the
    workdir of the sandbox it was actually built with: without that, an
    injected sandbox writes to one directory while ``read_file`` looks in
    another, which in tests means quietly touching the real ``data/sandbox``.
    """
    roots: list[Path] = []
    if workspace is not None:
        roots.append(Path(workspace).resolve())

    keys = ("sandbox.workdir", "app.corpus_dir", "app.uploads_dir", "app.downloads_dir")
    for key in keys:
        try:
            candidate = config.get_path(key).resolve()
        except KeyError:
            continue
        if candidate not in roots:
            roots.append(candidate)
    return roots


def resolve_within_roots(raw_path: str, roots: list[Path] | None = None) -> Path:
    """Resolve ``raw_path`` and confirm it sits inside an allowed root.

    A bare relative path is interpreted against the sandbox workspace first,
    then each remaining root - so the agent can say ``read_file("SOP-114.txt")``
    and reach the corpus without knowing the directory layout.
    """
    roots = roots or allowed_roots()
    if not roots:
        raise ToolError("No allowed directories are configured; cannot access the filesystem.")

    candidate = Path(raw_path)

    if candidate.is_absolute():
        resolved = candidate.resolve()
        if _within(resolved, roots):
            return resolved
        raise ToolError(
            f"Path {raw_path!r} is outside the workbench directories. "
            f"Allowed: {', '.join(str(r) for r in roots)}"
        )

    # Relative to the repo root, if that lands inside a root.
    direct = (config.ROOT / candidate).resolve()
    if _within(direct, roots) and direct.exists():
        return direct

    for root in roots:
        attempt = (root / candidate).resolve()
        if _within(attempt, roots) and attempt.exists():
            return attempt

    # Nothing exists yet - a write. Default to the workspace.
    target = (roots[0] / candidate).resolve()
    if not _within(target, roots):
        # Same wording as the absolute-path branch above: the two differ only in
        # how the path was written, not in what went wrong. Keeping them
        # identical also makes the refusal assertable across platforms, since
        # "/etc/hosts" is absolute on POSIX but relative on Windows.
        raise ToolError(
            f"Path {raw_path!r} is outside the workbench directories. "
            f"Allowed: {', '.join(str(r) for r in roots)}"
        )
    return target


def _within(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _read_file(path: str, roots: list[Path]) -> ToolOutcome:
    target = resolve_within_roots(path, roots)

    # Suffix first: telling the model to use search_documents for a PDF is the
    # useful answer whether or not that particular PDF happens to exist.
    if target.suffix.lower() not in _READABLE_SUFFIXES:
        raise ToolError(
            f"{target.suffix or 'This file type'} is not readable as text. "
            "PDFs and images must go through the ingestion pipeline, then be reached with search_documents."
        )
    if not target.exists():
        nearby = _suggest(target.name, roots)
        raise ToolError(
            f"No file at {path!r}."
            + (f" Did you mean one of: {nearby}?" if nearby else " Use list_files to see what exists.")
        )
    if target.is_dir():
        raise ToolError(f"{path!r} is a directory. Use list_files for directories.")

    text = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > _MAX_READ_CHARS
    body = text[:_MAX_READ_CHARS]

    return ToolOutcome(
        ok=True,
        output=body + (f"\n\n...[truncated, {len(text) - _MAX_READ_CHARS} more characters]" if truncated else ""),
        metadata={"path": str(target), "bytes": target.stat().st_size, "truncated": truncated},
    )


def _write_file(path: str, content: str, roots: list[Path]) -> ToolOutcome:
    target = resolve_within_roots(path, roots)
    if target.suffix.lower() not in _READABLE_SUFFIXES:
        raise ToolError(
            f"write_file only writes text files ({', '.join(sorted(_READABLE_SUFFIXES))}). "
            "Word and Excel deliverables are produced by the deliverables pipeline, not by this tool."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return ToolOutcome(
        ok=True,
        output=f"Wrote {len(content)} characters to {target.name}.",
        artifacts=[str(target)],
        metadata={"path": str(target), "bytes": target.stat().st_size},
    )


def _list_files(directory: str, roots: list[Path]) -> ToolOutcome:
    target = resolve_within_roots(directory, roots)
    if not target.exists():
        raise ToolError(f"No directory at {directory!r}.")
    if not target.is_dir():
        raise ToolError(f"{directory!r} is a file, not a directory. Use read_file.")

    entries: list[str] = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if item.name.startswith("."):
            continue
        entries.append(f"{item.name}/" if item.is_dir() else f"{item.name}  ({item.stat().st_size} bytes)")
        if len(entries) >= _MAX_LISTING:
            entries.append(f"...[listing truncated at {_MAX_LISTING} entries]")
            break

    if not entries:
        return ToolOutcome(ok=True, output=f"{target.name}/ is empty.", metadata={"path": str(target)})

    return ToolOutcome(
        ok=True,
        output=f"Contents of {target.name}/:\n" + "\n".join(entries),
        metadata={"path": str(target), "count": len(entries)},
    )


def _make_search_documents(index: RagIndex) -> Callable[..., ToolOutcome]:
    def _tool_search_documents(query: str, top_k: int | None = None) -> ToolOutcome:
        # A model that asks for top_k=1 blinds itself: the single slot goes to
        # whichever passage scores highest overall, which on a threshold query
        # is the SOP rather than the report carrying the measurement. It then
        # concludes the report is not in the corpus. Treat the model's top_k as
        # a request for *at least* that many, never fewer than the configured
        # default.
        default_top_k = config.get("rag.top_k", 5)
        if top_k is None or top_k < default_top_k:
            top_k = default_top_k
        citations = index.search_citations(query, top_k=top_k)
        if not citations:
            stats = index.stats()
            if stats["chunks"] == 0:
                raise ToolError(
                    "The document index is empty - no corpus has been indexed yet. "
                    "Answer from the attached files instead, or say that no source is available."
                )
            raise ToolError(
                f"No passage in the corpus matched {query!r}. "
                "Try different wording, or fewer and more specific terms."
            )

        lines = [
            f"[{i}] {Path(c.source_path).name}, page {c.page} (score {c.score}):\n{c.snippet}"
            for i, c in enumerate(citations, start=1)
        ]
        return ToolOutcome(
            ok=True,
            output="\n\n".join(lines),
            citations=citations,
            metadata={"query": query, "hits": len(citations), "embedder": index.stats()["embedder"]},
        )

    return _tool_search_documents


def _make_run_python(sandbox: Sandbox) -> Callable[..., ToolOutcome]:
    def _tool_run_python(code: str) -> ToolOutcome:
        result = sandbox.run(code)
        return ToolOutcome(
            ok=result.ok,
            output=result.as_observation() if result.ok else "",
            error=None if result.ok else result.as_observation(),
            artifacts=[str(sandbox.workdir / name) for name in result.created_files],
            metadata={
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
                "network_blocked": result.network_blocked,
            },
        )

    return _tool_run_python


def _as_finding(row: object) -> object:
    """Accept a bare string where a finding object was expected.

    A small local model writes `findings: ["Shell course 2 is below the
    threshold"]` about as often as it writes the nested object, and strict
    validation turned that into four consecutive failed tool calls - two and a
    half minutes of a six-minute demo - before the agent gave up on the
    deliverable entirely. The text it does supply is a perfectly good
    observation, so keep it rather than rejecting the call.
    """
    if isinstance(row, str):
        # "Shell course 2, grid C4: wall thickness 7.1 mm" splits at the colon
        # into the location and what was seen there. No colon: it is all
        # observation, and the item column says so rather than inventing one.
        head, sep, tail = row.partition(":")
        if sep and tail.strip() and len(head) <= 60:
            return {"item": head.strip(), "observation": tail.strip()}
        return {"item": "Finding", "observation": row}
    return row


_CITATION_PAGE = re.compile(r"\bp(?:age)?\s*\.?\s*(\d+)", re.IGNORECASE)


def _as_citation(source: object) -> object:
    """Accept a bare string where a citation object was expected.

    Same failure as _as_finding: the model cites "REPORT.jpg, page 1" as a
    string. Pull the page number out if it is in there and keep the rest as the
    path, so a real citation still reaches the document instead of the whole
    call failing.
    """
    if not isinstance(source, str):
        return source
    page_match = _CITATION_PAGE.search(source)
    page = int(page_match.group(1)) if page_match else 1
    path = source[: page_match.start()] if page_match else source
    path = path.strip().rstrip(",").strip() or source.strip()
    return {"document_id": path, "source_path": path, "page": page, "snippet": ""}


def _make_create_approval_documents(downloads_dir: Path) -> Callable[..., ToolOutcome]:
    """H2/H4.5 - the tool that lets the agent produce real .docx/.xlsx output.

    One call builds both the Word approval note and the Excel thickness
    assessment from the same findings, since the demo always wants them as a
    pair. A single call is also more reliable for a small local model than
    coordinating two tool calls with duplicated arguments.
    """

    def _tool_create_approval_documents(
        ref_number: str,
        equipment: str,
        inspection_date: str,
        inspector: str,
        findings: list[dict],
        recommendation: str,
        sources: list[dict] | None = None,
        reviewing_engineer: str = "",
    ) -> ToolOutcome:
        try:
            finding_rows = [FindingRow.model_validate(_as_finding(row)) for row in findings]
            citations = [SourceCitation.model_validate(_as_citation(s)) for s in (sources or [])]
            note_data = ApprovalNoteData(
                ref_number=ref_number,
                equipment=equipment,
                inspection_date=inspection_date,
                inspector=inspector,
                findings=finding_rows,
                recommendation=recommendation,
                sources=citations,
                reviewing_engineer=reviewing_engineer,
            )
        except ValidationError as exc:
            raise ToolError(
                f"Invalid approval note data: {exc.errors()[0]['msg'] if exc.errors() else exc}. "
                "Each finding needs at least 'item' and 'observation'; status must be one of "
                "OK, REFER, CRITICAL, INFO."
            ) from None

        note = build_approval_note(note_data, output_dir=downloads_dir)
        sheet = build_thickness_assessment(
            ThicknessAssessmentData(ref_number=ref_number, equipment=equipment, rows=finding_rows),
            output_dir=downloads_dir,
        )

        return ToolOutcome(
            ok=True,
            output=(
                f"Created {note.filename} ({note.size_bytes} bytes, {note.download_url}) and "
                f"{sheet.filename} ({sheet.size_bytes} bytes, {sheet.download_url})."
            ),
            artifacts=[note.path, sheet.path],
            metadata={
                "docx_download_url": note.download_url,
                "xlsx_download_url": sheet.download_url,
            },
        )

    return _tool_create_approval_documents


def _suggest(filename: str, roots: list[Path], limit: int = 3) -> str:
    """Nearby filenames, so a wrong guess costs one step instead of three."""
    stem = Path(filename).stem.lower()[:4]
    if not stem:
        return ""
    matches: list[str] = []
    for root in roots:
        for item in root.rglob("*"):
            if item.is_file() and stem in item.name.lower():
                matches.append(item.name)
                if len(matches) >= limit:
                    return ", ".join(matches)
    return ", ".join(matches)


# ---------------------------------------------------------------------------
# Default registry
# ---------------------------------------------------------------------------


def build_registry(index: RagIndex | None = None, sandbox: Sandbox | None = None) -> ToolRegistry:
    """The five MVP tools, wired to the live index and sandbox."""
    index = index or get_index()
    sandbox = sandbox or get_sandbox()
    # Bound to THIS sandbox's workdir, so the registry and the sandbox always
    # agree about where the workspace is.
    roots = allowed_roots(sandbox.workdir)
    registry = ToolRegistry()

    registry.register(
        ToolSpec(
            name="read_file",
            description=(
                "Read a text file from the workbench directories (corpus, uploads, workspace). "
                "Use it when you know the filename."
            ),
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File name or path."}},
                "required": ["path"],
            },
            handler=lambda path, _roots=roots: _read_file(path, _roots),
        )
    )

    registry.register(
        ToolSpec(
            name="write_file",
            description=(
                "Write a text file into the workspace. Use it to save intermediate notes or "
                "the draft body of a document."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File name to write."},
                    "content": {"type": "string", "description": "Full file contents."},
                },
                "required": ["path", "content"],
            },
            handler=lambda path, content, _roots=roots: _write_file(path, content, _roots),
        )
    )

    registry.register(
        ToolSpec(
            name="list_files",
            description="List the files in a workbench directory. Use it when you do not know a filename.",
            input_schema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Directory to list. Defaults to the workspace."}
                },
                "required": [],
            },
handler=lambda directory=".", _roots=roots: _list_files(directory, _roots),
        )
    )

    registry.register(
        ToolSpec(
            name="search_documents",
            description=(
                "Search the local SOP and manual corpus. Returns passages with their source file "
                "and page number. Use it for any procedure, threshold or limit you must cite."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for, in a few specific terms."},
                    "top_k": {"type": "integer", "description": f"How many passages (default {config.get('rag.top_k', 5)})."},
                },
                "required": ["query"],
            },
            handler=_make_search_documents(index),
        )
    )

    registry.register(
        ToolSpec(
            name="run_python",
            description=(
                "Run Python in a sandboxed subprocess with no network access. Use it for every "
                "calculation rather than doing arithmetic in your head. print() what you need to see."
            ),
            input_schema={
                "type": "object",
                "properties": {"code": {"type": "string", "description": "Python source to execute."}},
                "required": ["code"],
            },
            handler=_make_run_python(sandbox),
        )
    )

    registry.register(
        ToolSpec(
            name="create_approval_documents",
            description=(
                "Produce the final deliverables: a Word approval note and an Excel thickness "
                "assessment, built from the same findings. Call this once, last, after you have "
                "the measurements and the cited threshold - not before you have both.\n"
                "Put every measurement in its own numeric field - measured_mm, nominal_mm and "
                "threshold_mm are numbers like 7.1, not text. The spreadsheet computes loss% and "
                "margin from those cells as live formulas, so a measurement left inside the "
                "observation sentence produces an empty column and no working to show."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ref_number": {"type": "string", "description": "Report reference, e.g. INSP-2026-0412."},
                    "equipment": {"type": "string", "description": "Equipment name and tag."},
                    "inspection_date": {"type": "string", "description": "e.g. 2026-04-12."},
                    "inspector": {"type": "string", "description": "Inspector name and certification."},
                    "findings": {
                        "type": "array",
                        "description": "One entry per finding.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item": {"type": "string", "description": "What was measured, e.g. 'Shell course 2, grid C4'."},
                                "observation": {"type": "string", "description": "What was found, in plain language."},
                                "measured_mm": {"type": "number"},
                                "nominal_mm": {"type": "number"},
                                "threshold_mm": {"type": "number"},
                                "status": {"type": "string", "description": "OK | REFER | CRITICAL | INFO"},
                            },
                            "required": ["item", "observation"],
                        },
                    },
                    "recommendation": {"type": "string", "description": "The engineering recommendation."},
                    "sources": {
                        "type": "array",
                        "description": "Citations from search_documents to attribute the threshold used.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "document_id": {"type": "string"},
                                "source_path": {"type": "string"},
                                "page": {"type": "integer"},
                                "snippet": {"type": "string"},
                            },
                        },
                    },
                    "reviewing_engineer": {"type": "string"},
                },
                "required": ["ref_number", "equipment", "inspection_date", "inspector", "findings", "recommendation"],
            },
            handler=_make_create_approval_documents(config.get_path("app.downloads_dir")),
        )
    )

    return registry


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None
