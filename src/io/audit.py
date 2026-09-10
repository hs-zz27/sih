"""H4 - append-only audit log.

Every model call, tool call, file access and network attempt lands here as one
JSON object per line. The log is the evidence behind the sovereignty claim, so
two properties matter more than convenience:

* **Append-only.** Entries are never rewritten or deleted in place. A log that
  can be edited proves nothing.
* **Durable per write.** Each record is flushed before the call returns, so a
  crash mid-demo still leaves a complete record of what happened up to it.

Reading is deliberately tolerant: a truncated final line (killed mid-write) is
skipped rather than raising, because a partially-written log is still evidence.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from src import config
from src.contracts import AuditEvent, AuditEventType

_LOCK = threading.Lock()


def log_path() -> Path:
    """Resolve the configured audit log path, creating its directory."""
    raw = config.get("app.audit_log", "logs/audit.jsonl")
    path = Path(raw)
    if not path.is_absolute():
        path = config.ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record(event: AuditEvent) -> AuditEvent:
    """Append one event. Returns it so callers can chain.

    Safe to call from any thread - the network monitor polls from a background
    thread while request handlers write from the event loop.
    """
    if not config.get("audit.enabled", True):
        return event

    line = event.model_dump_json()
    with _LOCK:
        with log_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
    return event


def emit(
    event_type: AuditEventType,
    summary: str,
    *,
    actor: str = "system",
    task_id: str | None = None,
    external: bool = False,
    **detail: object,
) -> AuditEvent:
    """Convenience wrapper - build and append in one call."""
    return record(
        AuditEvent(
            event_type=event_type,
            actor=actor,
            summary=summary,
            task_id=task_id,
            external=external,
            detail=dict(detail),
        )
    )


def read_events(task_id: str | None = None, limit: int | None = None) -> list[AuditEvent]:
    """Read the log back, newest last. Malformed trailing lines are skipped."""
    path = log_path()
    if not path.exists():
        return []

    events: list[AuditEvent] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(AuditEvent.model_validate_json(line))
            except ValueError:
                # Truncated final line from a killed process - skip, don't raise.
                continue

    if task_id is not None:
        events = [event for event in events if event.task_id == task_id]
    if limit is not None:
        events = events[-limit:]
    return events


def counts() -> tuple[int, int]:
    """(total_operations, external_calls) across the whole log."""
    events = read_events()
    return len(events), sum(1 for event in events if event.external)
