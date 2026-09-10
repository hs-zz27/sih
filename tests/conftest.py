"""Test isolation.

Without this, the suite writes into the real data directories. That is not a
tidiness problem - it is a demo-day problem. Running the tests once left the
live retrieval index holding nine fake `%PDF-1.4 fake` uploads from the ingest
endpoint test and **zero** real corpus documents, so the agent would have cited
test fixtures in front of judges.

So: every test session redirects the workbench's data paths into a temporary
directory. Nothing a test does can reach `data/index`, `data/uploads`,
`data/downloads`, `data/sandbox` or the audit log.

The redirect patches the parsed config rather than the filesystem, because
`src.config` is the single place every path is resolved from - which is exactly
why that rule was worth having.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from src import config


@pytest.fixture(scope="session", autouse=True)
def isolate_workbench_data(tmp_path_factory: pytest.TempPathFactory):
    """Point every configured data path at a session-scoped temp directory."""
    root = tmp_path_factory.mktemp("workbench")

    real = config.get_config()
    isolated = copy.deepcopy(real)

    redirects = {
        ("app", "index_dir"): "index",
        ("app", "uploads_dir"): "uploads",
        ("app", "downloads_dir"): "downloads",
        ("app", "corpus_dir"): None,      # read-only in tests; keep the real corpus
        ("app", "audit_log"): "logs/audit.jsonl",
        ("sandbox", "workdir"): "sandbox",
        ("demo", "cache_dir"): "demo_cache",
    }

    for (section, key), relative in redirects.items():
        if relative is None or section not in isolated or key not in isolated[section]:
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        isolated[section][key] = str(target)

    original_get_config = config.get_config

    def patched_get_config():
        return isolated

    config.get_config = patched_get_config  # type: ignore[assignment]

    # Drop any singleton that resolved a path before this fixture ran.
    _reset_singletons()

    yield root

    config.get_config = original_get_config  # type: ignore[assignment]
    _reset_singletons()


def _reset_singletons() -> None:
    """Clear module-level caches that hold a resolved path or a built object."""
    from src.core import orchestrator, rag, sandbox, tools

    for reset in (
        rag.reset_index,
        sandbox.reset_sandbox,
        tools.reset_registry,
        orchestrator.reset_orchestrator,
    ):
        try:
            reset()
        except Exception:  # noqa: BLE001 - a missing hook must not break collection
            pass


@pytest.fixture(autouse=True)
def _quiet_index_between_tests():
    """No-op placeholder kept so per-test isolation has somewhere to live."""
    yield
