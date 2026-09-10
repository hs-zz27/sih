"""Pre-demo readiness check.

Run this before every rehearsal and once more before the real thing:

    python scripts/preflight.py

It checks the things that look identical to a broken product from the
audience's seat. A model that was never pulled, an empty index and a genuine
agent bug all present as "it just sits there", and you do not get to debug in
front of judges.

Exit code is 0 only when every REQUIRED check passes.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""
    required: bool = True


def _check(name: str, required: bool = True):
    """Wrap a check so an exception becomes a FAIL rather than a traceback."""

    def decorator(fn):
        def run() -> Check:
            try:
                status, detail, fix = fn()
                return Check(name, status, detail, fix, required)
            except Exception as exc:  # noqa: BLE001
                return Check(name, FAIL, f"{exc.__class__.__name__}: {exc}", "", required)

        run.__name__ = fn.__name__
        return run

    return decorator


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@_check("Config loads and the endpoint is local")
def check_config():
    from src import config
    from src.core.llm import endpoint_is_local

    endpoint = config.get("inference.endpoint")
    if not endpoint:
        return FAIL, "inference.endpoint is not set", "Set it in config.yaml"
    if not endpoint_is_local(endpoint):
        return FAIL, f"{endpoint} is NOT local - this breaks the whole claim", "Point it at loopback"
    return OK, endpoint, ""


@_check("Local model server is reachable")
def check_inference():
    from src.core.llm import LLMClient

    available, models = LLMClient().probe(max_age_s=0)
    if not available:
        return FAIL, "not reachable", "Start it:  ollama serve"
    return OK, f"{len(models)} model(s) present", ""


@_check("Every configured model is pulled")
def check_models():
    from src.core.llm import LLMClient
    from src.core.router import Router

    available, present = LLMClient().probe(max_age_s=0)
    if not available:
        return FAIL, "cannot check - server unreachable", "Start ollama first"

    table = Router(use_llm_tiebreak=False).routing_table()
    stems = {name.split(":")[0] for name in present} | set(present)
    missing = sorted({m for m in table.values() if m not in present and m.split(":")[0] not in stems})

    if missing:
        return FAIL, f"missing: {', '.join(missing)}", "  ".join(f"ollama pull {m}" for m in missing)
    return OK, " / ".join(sorted(set(table.values()))), ""


@_check("Model tier is real (more than one model)", required=False)
def check_tier():
    from src.core.router import Router

    table = Router(use_llm_tiebreak=False).routing_table()
    distinct = set(table.values())
    if len(distinct) < 2:
        return (
            WARN,
            f"all task types use {next(iter(distinct))}",
            "Fine, but do not claim 'heavy models wake only when needed' in the pitch",
        )
    return OK, f"{table['document']} for heavy work, {table['general']} for lookups", ""


@_check("Embedding weights are cached locally")
def check_embeddings():
    from src.core.rag import RagIndex

    stats = RagIndex().stats()
    if not stats["semantic"]:
        return (
            WARN if stats["chunks"] else FAIL,
            f"running the TF-IDF fallback ({stats['degraded_reason']})",
            "python scripts/fetch_models.py",
        )
    return OK, stats["embedder"].split(":")[-1], ""


@_check("Corpus is indexed")
def check_index():
    from src.core.rag import RagIndex

    stats = RagIndex().stats()
    if stats["chunks"] == 0:
        return FAIL, "index is empty - retrieval will find nothing", "curl -X POST http://127.0.0.1:8000/api/index"
    return OK, f"{stats['chunks']} chunks from {stats['sources']} sources", ""


@_check("Index holds the real corpus, not test leftovers")
def check_index_is_clean():
    from collections import Counter

    from src import config
    from src.core.rag import RagIndex

    uploads = str(config.get_path("app.uploads_dir")).lower()
    corpus = str(config.get_path("app.corpus_dir")).lower()

    sources = Counter()
    for document in RagIndex().list_documents():
        path = document.source_path.lower()
        sources["uploads" if path.startswith(uploads) else "corpus" if path.startswith(corpus) else "other"] += 1

    if not sources:
        return FAIL, "index is empty", "curl -X POST http://127.0.0.1:8000/api/index"
    if sources["corpus"] == 0:
        return (
            FAIL,
            f"index holds {sources['uploads']} upload(s) and NO corpus documents - "
            "the agent would cite leftovers",
            "Clear data/index, then POST /api/index",
        )
    if sources["uploads"]:
        return WARN, f"{sources['corpus']} corpus + {sources['uploads']} uploaded chunk(s)", ""
    return OK, f"{sources['corpus']} corpus chunks, no leftovers", ""


@_check("Retrieval returns a cited passage")
def check_retrieval():
    from src.core.rag import RagIndex

    hits = RagIndex().search_citations("wall thickness escalation threshold", top_k=1)
    if not hits:
        return FAIL, "no passage matched the demo query", "Re-index, or check the corpus"
    hit = hits[0]
    if not hit.page:
        return WARN, "hit has no page number - attribution will look weak", ""
    return OK, f"{Path(hit.source_path).name} p{hit.page}", ""


@_check("Sandbox executes and blocks egress")
def check_sandbox():
    from src.core.sandbox import Sandbox

    sandbox = Sandbox()
    if not sandbox.run("print(6 * 7)").ok:
        return FAIL, "sandbox cannot run Python", "Check sandbox.workdir is writable"

    blocked = sandbox.run("import socket; socket.socket().connect(('1.1.1.1', 80))")
    if not blocked.network_blocked:
        return FAIL, "sandbox did NOT block a network call", "Check sandbox.network is false"
    return OK, "runs code, refuses sockets", ""


@_check("Egress observer is working")
def check_netmonitor():
    from src.io import netmonitor

    result = netmonitor.poll_once()
    if result.error:
        return WARN, result.error, "Second evidence layer is unavailable on this machine"
    return OK, f"observing {len(result.connections)} connection(s), {len(result.external)} external", ""


@_check("OCR is installed", required=False)
def check_ocr():
    if shutil.which("tesseract"):
        return OK, shutil.which("tesseract"), ""
    return WARN, "tesseract binary not found - scanned-page ingestion will fail", "Install tesseract"


@_check("Deliverables directory is writable")
def check_downloads():
    from src import config

    target = config.get_path("app.downloads_dir") / ".preflight"
    target.write_text("ok", encoding="utf-8")
    target.unlink()
    return OK, str(config.get_path("app.downloads_dir")), ""


@_check("Demo replays are recorded", required=False)
def check_replays():
    from src.core import demo

    if not demo.fallback_enabled():
        return OK, "fallback disabled - every run will be live", ""
    cached = demo.cached_runs()
    missing = [p.id for p in demo.presets() if p.id not in cached]
    if missing:
        return (
            WARN,
            f"no cached run for: {', '.join(missing)}",
            "POST /api/demo/record/<preset> on THIS machine, with the demo model",
        )
    return OK, f"{len(cached)} preset(s) cached", ""


CHECKS = [
    check_config,
    check_inference,
    check_models,
    check_tier,
    check_embeddings,
    check_index,
    check_index_is_clean,
    check_retrieval,
    check_sandbox,
    check_netmonitor,
    check_ocr,
    check_downloads,
    check_replays,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="Only show problems.")
    args = parser.parse_args()

    print("\nSUTRA Workbench - preflight\n" + "=" * 66)

    results = [check() for check in CHECKS]
    symbol = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}

    for result in results:
        if args.quiet and result.status == OK:
            continue
        print(f"[{symbol[result.status]}] {result.name}")
        print(f"          {result.detail}")
        if result.fix and result.status != OK:
            print(f"          -> {result.fix}")

    failures = [r for r in results if r.status == FAIL and r.required]
    warnings = [r for r in results if r.status == WARN]

    print("=" * 66)
    if failures:
        print(f"NOT READY - {len(failures)} required check(s) failed:")
        for failure in failures:
            print(f"  - {failure.name}")
        print("\nFix these before rehearsing. Every one of them looks like a broken")
        print("agent from the audience's seat.")
        return 1

    if warnings:
        print(f"READY, with {len(warnings)} warning(s). Know what they are before you present.")
    else:
        print("READY. Now run it twice with the Wi-Fi off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
