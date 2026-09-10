"""M6 - local retrieval over the SOP / manual corpus.

Everything here runs on this machine. The embedding model is loaded from
``rag.embedding_cache_dir`` with the HuggingFace offline flags set *before*
torch is imported, so a cold start with the Wi-Fi off either works or fails
loudly - it never quietly reaches for the network.

Two backends, chosen at construction time:

* **Preferred** - ``sentence-transformers`` embeddings in a persisted ChromaDB
  collection. This is what ships.
* **Fallback** - a pure-NumPy TF-IDF index persisted as ``.npz`` + JSON. It
  exists because those two packages are a ~2 GB install: the fallback keeps the
  agent loop, the tests and the demo corpus working on a machine where they are
  not present, and it is genuinely offline rather than a mock.

Which one is live is reported by :meth:`RagIndex.stats`, and shown in the UI. We
do not pretend the fallback is semantic search.

Retrieval always returns ``SourceCitation`` objects carrying the source document
and page, because attribution is a scoring requirement, not a nicety.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

import numpy as np

from src import config
from src.contracts import Document, SourceCitation, new_id

# Set before any transformers/torch import anywhere in the process. Belt and
# braces with the sandbox's own env: nothing downloads at runtime.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def chunk_document(
    document: Document,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> list[Document]:
    """Split one page-level ``Document`` into overlapping retrieval chunks.

    Page and source metadata are copied onto every chunk. Losing them would make
    a citation read "somewhere in the manual", which is worthless to an inspector.

    Splits on paragraph boundaries first and only falls back to hard character
    slicing when a single paragraph is longer than the window, so a chunk rarely
    begins mid-sentence.
    """
    chunk_size = int(chunk_size if chunk_size is not None else config.get("rag.chunk_size", 800))
    overlap = int(overlap if overlap is not None else config.get("rag.chunk_overlap", 120))
    overlap = max(0, min(overlap, chunk_size // 2))

    text = (document.text or "").strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [document.model_copy(deep=True)]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    windows: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > chunk_size:
            if current:
                windows.append(current)
                current = ""
            windows.extend(_hard_split(paragraph, chunk_size, overlap))
            continue

        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            windows.append(current)
            current = _tail(current, overlap) + "\n\n" + paragraph if overlap else paragraph

    if current.strip():
        windows.append(current)

    chunks: list[Document] = []
    for position, window in enumerate(windows):
        metadata = dict(document.metadata)
        metadata.update({"parent_id": document.id, "chunk_index": position, "chunk_count": len(windows)})
        chunks.append(
            Document(
                id=f"{document.id}::c{position}",
                source_path=document.source_path,
                page=document.page,
                text=window.strip(),
                metadata=metadata,
            )
        )
    return chunks


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    step = max(1, size - overlap)
    return [text[i : i + size] for i in range(0, len(text), step) if text[i : i + size].strip()]


def _tail(text: str, n: int) -> str:
    return text[-n:] if n and len(text) > n else text


# ---------------------------------------------------------------------------
# Embedding backends
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    name: str
    semantic: bool

    def fit(self, corpus: list[str]) -> None: ...
    def encode(self, texts: list[str]) -> np.ndarray: ...


class SentenceTransformerEmbedder:
    """Local sentence-transformers model. Weights must already be on disk."""

    semantic = True

    def __init__(self, model_name: str | None = None, cache_dir: Path | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # imported lazily - heavy

        self.model_name = model_name or config.get("rag.embedding_model", "sentence-transformers/all-MiniLM-L6-v2")
        cache = cache_dir or config.get_path("rag.embedding_cache_dir", "models/embeddings")
        self._model = SentenceTransformer(self.model_name, cache_folder=str(cache))
        self.name = f"sentence-transformers:{self.model_name}"

    def fit(self, corpus: list[str]) -> None:
        """No-op - the model is pre-trained, not fitted to our corpus."""

    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vectors, dtype=np.float32)


class TfidfEmbedder:
    """Pure-NumPy TF-IDF. Deterministic, dependency-free, genuinely offline.

    Lexical, not semantic: it matches on shared vocabulary. For an SOP corpus
    full of specific nouns ("wall thickness", "hydrotest", "E-4102") that is a
    workable retrieval signal, but it will miss a paraphrase that shares no
    words, and we say so rather than calling it semantic search.
    """

    semantic = False
    name = "tfidf-numpy"

    def __init__(self, max_features: int = 8192) -> None:
        self.max_features = max_features
        self._vocabulary: dict[str, int] = {}
        self._idf: np.ndarray = np.zeros(0, dtype=np.float32)

    def fit(self, corpus: list[str]) -> None:
        document_frequency: Counter[str] = Counter()
        for text in corpus:
            document_frequency.update(set(_tokenize(text)))
        if not document_frequency:
            self._vocabulary, self._idf = {}, np.zeros(0, dtype=np.float32)
            return

        most_common = document_frequency.most_common(self.max_features)
        self._vocabulary = {term: i for i, (term, _) in enumerate(most_common)}

        total = len(corpus)
        idf = np.zeros(len(self._vocabulary), dtype=np.float32)
        for term, index in self._vocabulary.items():
            idf[index] = math.log((1 + total) / (1 + document_frequency[term])) + 1.0
        self._idf = idf

    def encode(self, texts: list[str]) -> np.ndarray:
        if not self._vocabulary:
            return np.zeros((len(texts), 1), dtype=np.float32)

        matrix = np.zeros((len(texts), len(self._vocabulary)), dtype=np.float32)
        for row, text in enumerate(texts):
            counts = Counter(_tokenize(text))
            if not counts:
                continue
            peak = max(counts.values())
            for term, count in counts.items():
                column = self._vocabulary.get(term)
                if column is not None:
                    # Sublinear tf, normalised by the document's own peak term.
                    matrix[row, column] = (0.5 + 0.5 * count / peak) * self._idf[column]

        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.maximum(norms, 1e-9)

    def state(self) -> dict[str, Any]:
        return {"vocabulary": self._vocabulary, "idf": self._idf.tolist()}

    def load_state(self, state: dict[str, Any]) -> None:
        self._vocabulary = {str(k): int(v) for k, v in (state.get("vocabulary") or {}).items()}
        self._idf = np.asarray(state.get("idf") or [], dtype=np.float32)


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-/.]*")

# Common English words carry no retrieval signal in a technical corpus.
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have in into is it its of on or that the to was were
    will with this these those which what when where who whom how why shall should may can could
    would must if then than there their them they you your our we us do does did not no yes""".split()
)


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS]


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@dataclass
class RetrievedChunk:
    document: Document
    score: float


class RagIndex:
    """Persisted local vector index over the corpus.

    ``backend`` selects the embedder: ``"auto"`` prefers sentence-transformers
    and silently falls back to TF-IDF when it is not installed or its weights
    are not cached. Pass ``"tfidf"`` to force the fallback (tests do).
    """

    def __init__(self, index_dir: Path | None = None, backend: str = "auto") -> None:
        self.index_dir = index_dir or config.get_path("app.index_dir", "data/index")
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.collection = str(config.get("rag.collection", "mrpl_corpus"))

        self._documents: list[Document] = []
        self._vectors: np.ndarray = np.zeros((0, 1), dtype=np.float32)
        self._embedder = self._build_embedder(backend)
        self._degraded_reason: str | None = getattr(self, "_degraded_reason", None)

        self._load()

    # -- backend selection ------------------------------------------------

    def _build_embedder(self, backend: str) -> Embedder:
        if backend == "tfidf":
            return TfidfEmbedder()
        try:
            return SentenceTransformerEmbedder()
        except Exception as exc:  # noqa: BLE001 - missing package OR uncached weights
            self._degraded_reason = f"{exc.__class__.__name__}: {exc}"
            if backend == "sentence-transformers":
                raise
            return TfidfEmbedder()

    # -- indexing ---------------------------------------------------------

    def index_documents(self, documents: Iterable[Document], replace: bool = False) -> int:
        """Chunk, embed and persist ``documents``. Returns the chunk count.

        Re-indexing the same ``source_path`` replaces its chunks rather than
        duplicating them, so an operator re-uploading a corrected scan does not
        end up with both versions being retrieved.
        """
        incoming: list[Document] = []
        for document in documents:
            incoming.extend(chunk_document(document))

        if not incoming and not replace:
            return 0

        if replace:
            kept: list[Document] = []
        else:
            touched = {d.source_path for d in incoming}
            kept = [d for d in self._documents if d.source_path not in touched]

        self._documents = kept + incoming

        texts = [d.text for d in self._documents]
        if texts:
            # TF-IDF needs the whole corpus to compute idf; refitting on every
            # index call keeps scores consistent across chunks. Cheap at our size.
            self._embedder.fit(texts)
            self._vectors = self._embedder.encode(texts)
        else:
            self._vectors = np.zeros((0, 1), dtype=np.float32)

        self._persist()
        return len(incoming)

    def index_corpus_dir(self, corpus_dir: Path | None = None) -> int:
        """Index the plain-text files sitting in the corpus directory.

        PDFs and scans are H1's job - they arrive as ``Document`` objects through
        :meth:`index_documents`. This handles the ``.txt`` / ``.md`` corpus so
        the engine is testable without the ingestion pipeline.
        """
        corpus_dir = corpus_dir or config.get_path("app.corpus_dir", "data/corpus")
        documents: list[Document] = []

        for path in sorted(corpus_dir.rglob("*")):
            if path.suffix.lower() not in {".txt", ".md"} or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for page_number, page_text in enumerate(_split_pages(text), start=1):
                if not page_text.strip():
                    continue
                documents.append(
                    Document(
                        id=f"{path.stem}::p{page_number}",
                        source_path=str(path),
                        page=page_number,
                        text=page_text,
                        metadata={
                            "filename": path.name,
                            "doc_title": path.stem.replace("_", " "),
                            "extraction_method": "text_layer",
                        },
                    )
                )

        return self.index_documents(documents, replace=True)

    # -- retrieval --------------------------------------------------------

    def search(self, query: str, top_k: int | None = None, min_score: float = 0.0) -> list[RetrievedChunk]:
        """Nearest chunks to ``query``, best first."""
        top_k = int(top_k if top_k is not None else config.get("rag.top_k", 5))
        if not query.strip() or not self._documents or self._vectors.size == 0:
            return []

        query_vector = self._embedder.encode([query])[0]
        if query_vector.shape[0] != self._vectors.shape[1]:
            return []  # index built with a different backend; re-index required

        scores = self._vectors @ query_vector
        order = np.argsort(-scores)[: max(1, top_k)]

        return [
            RetrievedChunk(document=self._documents[i], score=float(scores[i]))
            for i in order
            if float(scores[i]) > min_score
        ]

    def search_citations(self, query: str, top_k: int | None = None) -> list[SourceCitation]:
        """Retrieval results as contract objects, ready for the UI."""
        return [
            SourceCitation(
                document_id=hit.document.id,
                source_path=hit.document.source_path,
                page=hit.document.page,
                snippet=_snippet(hit.document.text),
                score=round(hit.score, 4),
            )
            for hit in self.search(query, top_k)
        ]

    # -- introspection ----------------------------------------------------

    def list_documents(self) -> list[Document]:
        return list(self._documents)

    def stats(self) -> dict[str, Any]:
        """What the UI shows about the index, including honest degradation."""
        return {
            "collection": self.collection,
            "chunks": len(self._documents),
            "sources": len({d.source_path for d in self._documents}),
            "embedder": self._embedder.name,
            "semantic": self._embedder.semantic,
            "dimensions": int(self._vectors.shape[1]) if self._vectors.size else 0,
            "index_dir": str(self.index_dir),
            "degraded_reason": self._degraded_reason,
        }

    # -- persistence ------------------------------------------------------

    @property
    def _vectors_path(self) -> Path:
        return self.index_dir / f"{self.collection}.vectors.npz"

    @property
    def _documents_path(self) -> Path:
        return self.index_dir / f"{self.collection}.documents.json"

    def _persist(self) -> None:
        np.savez_compressed(self._vectors_path, vectors=self._vectors)
        payload: dict[str, Any] = {
            "embedder": self._embedder.name,
            "documents": [d.model_dump(mode="json") for d in self._documents],
        }
        if isinstance(self._embedder, TfidfEmbedder):
            payload["tfidf_state"] = self._embedder.state()
        self._documents_path.write_text(json.dumps(payload), encoding="utf-8")

    def _load(self) -> None:
        if not self._documents_path.exists() or not self._vectors_path.exists():
            return
        try:
            payload = json.loads(self._documents_path.read_text(encoding="utf-8"))
            if payload.get("embedder") != self._embedder.name:
                return  # backend changed since this index was written; ignore it
            self._documents = [Document.model_validate(d) for d in payload.get("documents", [])]
            self._vectors = np.load(self._vectors_path)["vectors"]
            if isinstance(self._embedder, TfidfEmbedder) and payload.get("tfidf_state"):
                self._embedder.load_state(payload["tfidf_state"])
        except Exception:  # noqa: BLE001 - a corrupt index must not stop startup
            self._documents, self._vectors = [], np.zeros((0, 1), dtype=np.float32)


def _split_pages(text: str) -> list[str]:
    """Honour explicit form-feed page breaks, else treat the file as one page."""
    return text.split("\f") if "\f" in text else [text]


def _snippet(text: str, limit: int = 320) -> str:
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else flattened[:limit].rsplit(" ", 1)[0] + "..."


_index: RagIndex | None = None


def get_index() -> RagIndex:
    global _index
    if _index is None:
        _index = RagIndex()
    return _index


def reset_index() -> None:
    global _index
    _index = None


__all__ = [
    "RagIndex",
    "RetrievedChunk",
    "TfidfEmbedder",
    "SentenceTransformerEmbedder",
    "chunk_document",
    "get_index",
    "reset_index",
    "new_id",
]
