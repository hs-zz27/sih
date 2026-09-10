# Sovereign On-Premise Agentic AI Workbench

**SIH26117 — MRPL.** An agentic AI workbench that runs entirely on local
hardware: a scanned inspection report goes in, a real Word approval note comes
out, and a live network monitor shows that no external call was made at any
point.

See [PROPOSAL.md](<PROPOSAL (1).md>) for scope, ownership and timeline.

## Demo scenario

> A scanned inspection report for heat exchanger E-4102 is uploaded. The agent
> OCRs it, retrieves the relevant clause from the local SOP corpus, computes the
> remaining wall margin in a sandbox, and produces a Word approval note and an
> Excel thickness assessment — with the Wi-Fi off.

*(Wording to be confirmed by both of us before Day 1 — PROPOSAL.md §5.)*

## Hard rules

1. No cloud LLM in the runtime. No provider SDKs anywhere in the repo.
2. Endpoints and model names live in `config.yaml` and nowhere else.
3. Nothing downloads or installs at demo time — weights, embeddings, fonts and
   JS are all vendored in advance.
4. Every outbound socket at demo time is a P0 bug.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests -q
.venv/bin/uvicorn src.api.main:app --reload      # http://127.0.0.1:8000/docs
```

`PyMuPDF`, `sentence-transformers` and `chromadb` are heavy and only needed by
H1/M6; the Step-0 API runs without them.

Tesseract is a system binary, not a pip package:

```bash
brew install tesseract           # language data: tesseract-lang
```

## Layout

| Path | Owner | Contents |
|---|---|---|
| `src/contracts.py` | **both** | Pydantic models. Changes need both of us to agree. |
| `src/config.py`, `config.yaml` | both | The only place a host or path is written down. |
| `src/api/main.py` | both | FastAPI surface. Shapes are frozen; M7 swaps the bodies. |
| `src/api/fakes.py` | both | Step-0 canned data. **Deleted at M7.** |
| `src/core/` | Manraj | `llm`, `router`, `tools`, `agent`, `sandbox`, RAG |
| `src/io/` | Harkamal | `ingest`, `deliverables/`, `audit` |
| `data/corpus/` | Harkamal | Synthetic, clearly fictional demo documents (tracked) |
| `data/uploads/`, `data/downloads/`, `data/index/` | — | Runtime output (gitignored) |

## API surface (Step 0)

All routes return contract-shaped data today; only the bodies change at M7.

| Route | Returns | Real yet? |
|---|---|---|
| `GET /api/health` | status | yes |
| `GET /api/config` | model names, step cap, allowed hosts | yes |
| `GET /api/tools` | `ToolSpec[]` | **live (M3)** |
| `POST /api/ingest` | `IngestResult` | upload real, extraction stub (H1) |
| `GET /api/documents` | `Document[]` | **live (M6)** |
| `POST /api/tasks` | `AgentResult` | **live (M7)** — add `?background=true` for a streamed run |
| `GET /api/tasks/{id}` | `AgentResult` | **live (M7)** |
| `GET /api/tasks/{id}/stream` | SSE `StreamEvent` — `routing`/`step`/`result`/`done` | **live (M7)** |
| `POST /api/index` | index stats | **live (M6)** — re-index `data/corpus` |
| `GET /api/search?query=` | `SourceCitation[]` | **live (M6)** |
| `GET /api/deliverables` | `Deliverable[]` | lists the real directory |
| `GET /api/deliverables/{filename}` | file download | yes |
| `GET /api/audit` | `AuditEvent[]` | stub (H4) |
| `GET /api/network` | `NetworkStatus` | stub (H4) |

Find what is still fake: `grep -rn "STUB (" src/` — only H1 (OCR extraction) and H4 (audit / network monitor) remain.

## Local inference

The engine talks to a local model server over plain HTTP; there is no provider SDK anywhere.
`inference.api_style` in `config.yaml` selects the wire format — `ollama` or `openai-compatible`
(vLLM, llama.cpp, LM Studio) — so moving between the laptop and the GPU box is one line of config.

```bash
ollama serve
ollama pull qwen2.5:7b-instruct
ollama pull qwen2.5-coder:7b
```

`GET /api/health` reports whether the server is reachable and which configured models it is
actually missing. Check it before a rehearsal — at demo time a missing model looks identical
to a broken agent.

### Embedding weights (once, while online)

```bash
python scripts/fetch_models.py            # downloads into models/embeddings (~92 MB)
python scripts/fetch_models.py --verify   # re-run with Wi-Fi OFF to prove it loads offline
```

`src/core/rag.py` sets `HF_HUB_OFFLINE=1` at import, so the runtime never downloads anything.
If the weights are absent it falls back to a pure-NumPy TF-IDF index and says so in
`GET /api/health` — retrieval keeps working, but it is lexical, not semantic, and we do not
claim otherwise.

## Branches

- `main` — protected, PR-only after Step 0.
- `feat/core-engine` — Manraj (M1–M7)
- `feat/app-io` — Harkamal (H1–H5)
