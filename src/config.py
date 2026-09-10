"""Loader for `config.yaml`.

PROPOSAL.md ss5: "No hardcoded URLs anywhere else." Import `get_config()` rather
than writing an endpoint, model name or path into application code.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Repo root = parent of src/
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = ROOT / "config.yaml"
LOCAL_OVERRIDE_PATH = ROOT / "config.local.yaml"  # gitignored, per-machine


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    """Read config.yaml, then apply config.local.yaml if present.

    `WORKBENCH_CONFIG` overrides the path entirely (used by tests and by the
    GPU-box deployment, which points `inference.endpoint` somewhere else).
    """
    path = Path(os.environ.get("WORKBENCH_CONFIG", DEFAULT_CONFIG_PATH))
    with path.open("r", encoding="utf-8") as fh:
        config: dict[str, Any] = yaml.safe_load(fh) or {}

    if path == DEFAULT_CONFIG_PATH and LOCAL_OVERRIDE_PATH.exists():
        with LOCAL_OVERRIDE_PATH.open("r", encoding="utf-8") as fh:
            config = _deep_merge(config, yaml.safe_load(fh) or {})

    return config


def get(dotted_key: str, default: Any = None) -> Any:
    """Fetch a nested value, e.g. `get("inference.endpoint")`."""
    node: Any = get_config()
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def get_path(dotted_key: str, default: str | None = None) -> Path:
    """Resolve a configured path against the repo root and create it if needed."""
    raw = get(dotted_key, default)
    if raw is None:
        raise KeyError(f"No path configured at {dotted_key!r}")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path
