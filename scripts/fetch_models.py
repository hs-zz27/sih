"""One-time, dev-time weight download. NOT part of the runtime.

The embedding model must be on local disk before the demo, because
``src/core/rag.py`` sets ``HF_HUB_OFFLINE=1`` at import and will refuse to
reach for the network. Run this once, on a machine with connectivity:

    python scripts/fetch_models.py

Then verify the offline path actually works:

    python scripts/fetch_models.py --verify

This is the only file in the repo that is allowed to touch the network, it is
never imported by application code, and it does nothing at demo time.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def download() -> int:
    """Pull the configured embedding model into rag.embedding_cache_dir."""
    # Must be cleared BEFORE importing anything that touches huggingface_hub.
    for flag in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ.pop(flag, None)

    import yaml

    settings = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    model_name = settings["rag"]["embedding_model"]
    cache_dir = ROOT / settings["rag"]["embedding_cache_dir"]
    cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {model_name}")
    print(f"        -> {cache_dir}")

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("\nsentence-transformers is not installed. Run:")
        print("    pip install -r requirements.txt")
        return 1

    model = SentenceTransformer(model_name, cache_folder=str(cache_dir))
    dimensions = len(model.encode("warm-up sentence"))

    size_mb = sum(f.stat().st_size for f in cache_dir.rglob("*") if f.is_file()) / 1e6
    print(f"\nDone. {dimensions}-dimensional embeddings, {size_mb:.0f} MB on disk.")
    print("Now run `python scripts/fetch_models.py --verify` with the Wi-Fi OFF.")
    return 0


def verify() -> int:
    """Load the model with the offline flags set, exactly as the runtime does."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    from src.core.rag import RagIndex

    index = RagIndex(backend="sentence-transformers")
    stats = index.stats()
    print(f"Offline load OK: {stats['embedder']}")
    print(f"Index: {stats['chunks']} chunks from {stats['sources']} sources")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Load offline instead of downloading.")
    args = parser.parse_args()

    if args.verify:
        try:
            return verify()
        except Exception as exc:  # noqa: BLE001
            print(f"OFFLINE LOAD FAILED: {exc.__class__.__name__}: {exc}")
            print("The weights are not cached. Run this script without --verify while online.")
            return 1

    return download()


if __name__ == "__main__":
    raise SystemExit(main())
