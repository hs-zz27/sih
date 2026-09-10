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


# Environment overrides for the handful of settings that legitimately differ
# per deployment rather than per project. Containers cannot edit config.yaml,
# and the endpoint is exactly the value docker-compose has to change: the
# workbench talks to `http://ollama:11434` there and `http://127.0.0.1:11434`
# on a laptop. Everything else still belongs in config.yaml.
ENV_OVERRIDES: dict[str, str] = {
    "SUTRA_INFERENCE_ENDPOINT": "inference.endpoint",
    "SUTRA_API_STYLE": "inference.api_style",
    "SUTRA_MODEL_DOCUMENT": "models.document",
    "SUTRA_MODEL_CODE": "models.code",
    "SUTRA_MODEL_GENERAL": "models.general",
}

_KEY_TO_ENV: dict[str, str] = {v: k for k, v in ENV_OVERRIDES.items()}


def get(dotted_key: str, default: Any = None) -> Any:
    """Fetch a nested value, e.g. `get("inference.endpoint")`.

    An environment variable listed in ``ENV_OVERRIDES`` wins over the file, so a
    container can be pointed at a different model server without a rebuild.
    Read at call time rather than at load, so tests can set one with monkeypatch.
    """
    env_name = _KEY_TO_ENV.get(dotted_key)
    if env_name:
        override = os.environ.get(env_name)
        if override:
            return override

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
