"""Demo scaffolding - deterministic presets and a labelled fallback.

Everything in this module exists to make the MVP demo predictable. None of it
makes the system *look* like it did something it did not, and that distinction
is the whole design:

* **Presets** are rehearsed prompts. The agent runs them for real.
* **Determinism** pins sampling so the same prompt takes the same path twice.
* **Fallback** replays a cached trajectory *only* when live inference fails, and
  every replayed step is stamped ``demo_replay: true`` with a banner in the
  final text. A judge reading the screen can always tell.

Every shortcut here is also written down in ``HARDCODED.md``. If you add one and
do not record it there, you have made the pitch harder to defend, because the
honest answer to "is that real?" is the thing that survives Q&A.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src import config
from src.contracts import AgentResult, AgentStep, StepStatus, TaskStatus, TaskType, utcnow

# Prefixed onto the final text of any replayed run. Deliberately impossible to
# mistake for a live result.
REPLAY_BANNER = (
    "[CACHED REPLAY - the local model was unreachable, so this is a previously "
    "recorded run of this exact task, not a live one.]"
)


@dataclass
class DemoPreset:
    """One rehearsed task, shown as a button in the UI."""

    id: str
    label: str
    task: str
    task_type_hint: TaskType | None = None
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "task": self.task,
            "task_type_hint": self.task_type_hint.value if self.task_type_hint else None,
            "description": self.description,
        }


# The rehearsed set. These are ordinary tasks - nothing about running one takes
# a different code path from a task typed by hand.
PRESETS: list[DemoPreset] = [
    DemoPreset(
        id="approval_note",
        label="Draft approval note (E-4102)",
        task=(
            "Read the attached inspection report for heat exchanger E-4102. Find the governing "
            "wall-thickness limit in the SOP corpus, calculate the remaining wall as a percentage "
            "of nominal, and draft an approval note stating whether it must be escalated. Cite the "
            "SOP clause and page you relied on."
        ),
        task_type_hint=TaskType.DOCUMENT,
        description="The headline flow: scan in, cited approval note out.",
    ),
    DemoPreset(
        id="thickness_calc",
        label="Wall-loss calculation",
        task=(
            "Nominal wall thickness is 12.0 mm and the measured minimum is 9.1 mm. Write and run "
            "Python to compute the remaining wall as a percentage of nominal and the absolute "
            "metal loss in mm. Show the numbers you used."
        ),
        task_type_hint=TaskType.CODE,
        description="Shows the router picking the code path, and the sandbox running real Python.",
    ),
    DemoPreset(
        id="sop_lookup",
        label="SOP threshold lookup",
        task=(
            "What is the wall-thickness reading below which an inspection finding must be "
            "escalated, and who must it be escalated to? Cite the source document and page."
        ),
        task_type_hint=TaskType.DOCUMENT,
        description="Pure retrieval with attribution - fast, and proves the corpus is local.",
    ),
]


def presets() -> list[DemoPreset]:
    return list(PRESETS)


def preset(preset_id: str) -> DemoPreset | None:
    return next((p for p in PRESETS if p.id == preset_id), None)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def is_deterministic() -> bool:
    """True when demo mode pins sampling for reproducible runs."""
    return bool(config.get("demo.deterministic", True))


def demo_temperature(default: float) -> float:
    """Temperature to use. Demo mode pins it to 0 so a rehearsal is repeatable.

    This is a genuine setting, not a trick: greedy decoding is the right choice
    for extraction and citation work anyway.
    """
    return 0.0 if is_deterministic() else default


# ---------------------------------------------------------------------------
# Cached fallback
# ---------------------------------------------------------------------------


def cache_dir() -> Path:
    return config.get_path("demo.cache_dir", "data/demo_cache")


def _cache_path(preset_id: str) -> Path:
    return cache_dir() / f"{preset_id}.json"


def fallback_enabled() -> bool:
    return bool(config.get("demo.fallback_enabled", True))


def record(preset_id: str, result: AgentResult) -> Path:
    """Save a successful live run so it can be replayed if the model dies.

    Run this during rehearsal, on the machine that will be demoed, with the
    model that will be demoed. A cache recorded anywhere else is a liability.
    """
    target = _cache_path(preset_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return target


def has_cached(preset_id: str) -> bool:
    return _cache_path(preset_id).is_file()


def cached_runs() -> list[str]:
    """Preset ids with a recorded run available."""
    directory = cache_dir()
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


def load_replay(preset_id: str, task_id: str) -> AgentResult | None:
    """Load a cached run, stamped so it can never pass as live.

    Returns None when nothing is cached, which leaves the caller's genuine
    failure result in place - the correct outcome, because a demo with no
    fallback should fail visibly rather than silently.
    """
    path = _cache_path(preset_id)
    if not path.is_file():
        return None

    try:
        result = AgentResult.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt cache must not mask the real failure
        return None

    stamped_steps: list[AgentStep] = []
    for step in result.steps:
        metadata = dict(step.metadata)
        metadata["demo_replay"] = True
        stamped_steps.append(step.model_copy(update={"metadata": metadata}))

    return result.model_copy(
        update={
            "task_id": task_id,
            "steps": stamped_steps,
            "final_text": f"{REPLAY_BANNER}\n\n{result.final_text}",
            "routing_reason": f"[cached replay] {result.routing_reason}",
            "status": TaskStatus.COMPLETED,
            "completed_at": utcnow(),
        }
    )


def match_preset(task: str) -> str | None:
    """Which preset a task text corresponds to, if any.

    Exact-match only. A fallback must never fire for a task a judge typed
    themselves - if they change the prompt, they get the real outcome, including
    a real failure.
    """
    normalised = " ".join(task.split()).strip().lower()
    for item in PRESETS:
        if " ".join(item.task.split()).strip().lower() == normalised:
            return item.id
    return None


def status() -> dict[str, Any]:
    """What demo mode is doing right now, for the UI and for HARDCODED.md."""
    return {
        "enabled": bool(config.get("demo.enabled", True)),
        "deterministic": is_deterministic(),
        "fallback_enabled": fallback_enabled(),
        "presets": [p.as_dict() for p in PRESETS],
        "cached_runs": cached_runs(),
        "cache_dir": str(cache_dir()),
        "replay_banner": REPLAY_BANNER,
    }


__all__ = [
    "DemoPreset",
    "PRESETS",
    "REPLAY_BANNER",
    "cached_runs",
    "demo_temperature",
    "fallback_enabled",
    "has_cached",
    "is_deterministic",
    "load_replay",
    "match_preset",
    "preset",
    "presets",
    "record",
    "status",
]
