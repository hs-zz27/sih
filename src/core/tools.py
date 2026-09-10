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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src import config
from src.contracts import SourceCitation, ToolSpec
from src.core.rag import RagIndex, get_index
from src.core.sandbox import Sandbox, get_sandbox

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


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


def allowed_roots() -> list[Path]:
    """Directories the filesystem tools may touch, resolved from config."""
    keys = ("sandbox.workdir", "app.corpus_dir", "app.uploads_dir", "app.downloads_dir")
    roots: list[Path] = []
    for key in keys:
        try:
            roots.append(config.get_path(key).resolve())
        except KeyError:
            continue
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
        raise ToolError(f"Path {raw_path!r} escapes the workbench directories.")
    return target


def _within(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _tool_read_file(path: str) -> ToolOutcome:
    target = resolve_within_roots(path)

    if not target.exists():
        nearby = _suggest(target.name)
        raise ToolError(
            f"No file at {path!r}."
            + (f" Did you mean one of: {nearby}?" if nearby else " Use list_files to see what exists.")
        )
    if target.is_dir():
        raise ToolError(f"{path!r} is a directory. Use list_files for directories.")
    if target.suffix.lower() not in _READABLE_SUFFIXES:
        raise ToolError(
            f"{target.suffix or 'This file type'} is not readable as text. "
            "PDFs and images must go through the ingestion pipeline, then be reached with search_documents."
        )

    text = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > _MAX_READ_CHARS
    body = text[:_MAX_READ_CHARS]

    return ToolOutcome(
        ok=True,
        output=body + (f"\n\n...[truncated, {len(text) - _MAX_READ_CHARS} more characters]" if truncated else ""),
        metadata={"path": str(target), "bytes": target.stat().st_size, "truncated": truncated},
    )


def _tool_write_file(path: str, content: str) -> ToolOutcome:
    target = resolve_within_roots(path)
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


def _tool_list_files(directory: str = ".") -> ToolOutcome:
    target = resolve_within_roots(directory)
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


def _suggest(filename: str, limit: int = 3) -> str:
    """Nearby filenames, so a wrong guess costs one step instead of three."""
    stem = Path(filename).stem.lower()[:4]
    if not stem:
        return ""
    matches: list[str] = []
    for root in allowed_roots():
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
            handler=_tool_read_file,
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
            handler=_tool_write_file,
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
            handler=_tool_list_files,
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
