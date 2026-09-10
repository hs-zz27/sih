# Sovereign On-Premise Agentic AI Workbench

**SIH26117** — Mangalore Refinery and Petrochemicals Limited (MRPL)
*Sovereign On-Premise Agentic AI Workbench using Open-Weight Multimodal LLMs for Confidential Industrial Work*

**Category:** Software · **Theme:** Smart Automation
**MVP deadline:** 12 September 2026 (~48 working hours)
**Team on MVP:** Manraj · Harkamal

---

## 1. Final Goal

A working, self-hosted AI workbench that runs entirely on local hardware, takes a scanned inspection report as input, and produces a real Word approval note as output — with a live network monitor proving that not a single external call was made at any point during the process.

If we demo nothing else, we demo that. Everything below serves it.

The single most important sentence in this problem statement:

> *"The system should also show, through logs or a visible network monitor, that no external calls are made at any point. That's the actual proof of the sovereign claim, not just a statement of it."*

Our winning moment is disconnecting the network in front of the judges and having the system keep working.

---

## 2. Hard Limits (non-negotiable)

These are constraints that, if violated, invalidate the entire submission. Read them before writing any code.

1. **No cloud LLM in the runtime. Ever.** No Claude, no OpenAI, no Gemini, no HuggingFace Inference API, no hosted endpoints. Open-weight models only, executing locally. This is the premise of the problem statement — the PS names Claude and Codex explicitly as the things that *cannot* be used.

2. **Claude is a development tool, not a component.** We use it to write our code. It never appears in the shipped system, in an architecture diagram, or in a dependency. Any `anthropic` or `openai` import in the final repo is a defect.

3. **HF GPUs are for development and weight download only.** Using a HuggingFace GPU box to *develop against* a larger model is fine. Calling a remote HF endpoint at demo time destroys the sovereign claim. Weights get downloaded once, then the system runs against a local inference server. **Design the model layer so the endpoint is a config value**, so we can point it at a local Ollama on a laptop or a local server on the GPU box without touching application code.

4. **No network egress at demo time.** By the final rehearsal the whole system must run with Wi-Fi off. That means: models pulled in advance, embedding model cached locally, no CDN-loaded fonts or JS in the UI, no `pip install` at runtime, no telemetry. Vendor everything.

5. **Bring our own corpus.** We supply the sample documents (inspection reports, SOPs, manuals). This is the reason we chose this PS — no external data dependency. Keep the corpus synthetic and clearly fictional; do not use real MRPL material.

6. **Open-weight licence only.** Qwen2.5, Llama 3.1, Mistral, gpt-oss class. No weights with a research-only or non-commercial restriction.

---

## 3. Scope

### Must have (MVP — this is what gets demoed)

| # | Feature | Owner |
|---|---|---|
| F1 | Local inference server running an open-weight model, exposed behind a config-driven client | Manraj |
| F2 | Model routing across ≥2 task types (document task vs code task), visibly logged | Manraj |
| F3 | Agent loop — plan, call tools, observe results, iterate, finish | Manraj |
| F4 | Tool layer: file read/write, local document search, sandboxed code execution | Manraj |
| F5 | Local RAG over our SOP/manual corpus, with source attribution | Manraj |
| F6 | Multimodal ingestion: scanned PDF / image → OCR → structured text | Harkamal |
| F7 | Deliverable generation: real `.docx` approval note, `.xlsx` with visible calculations | Harkamal |
| F8 | Web UI — task input, live agent step trace, model routing display, deliverable download | Harkamal |
| F9 | Audit log + live network monitor proving zero egress | Harkamal |
| F10 | Demo corpus, offline verification, and rehearsed demo script | Harkamal |

### Should have (build only if Day 1 closes clean)

- Vision model path for engineering drawings, alongside OCR
- PPTX generation
- Second agentic flow (code task run and verified in sandbox, end to end)

### Will not build (say so openly in the pitch)

- Multi-model hot-swapping at scale / dynamic model loading
- Handwriting recognition beyond what OCR gives us
- Real spreadsheet formula reasoning
- User auth, RBAC, multi-tenancy
- Anything that needs real MRPL data

Descoping deliberately and saying so is a strength. Pretending we built everything is how teams get taken apart in Q&A.

---

## 4. Architecture

```
┌─────────────────────────────────────────────────┐
│  Web UI (Harkamal)                              │
│  task input · agent step trace · downloads      │
│  network monitor panel                          │
└────────────────────┬────────────────────────────┘
                     │  FastAPI  (shared contract)
┌────────────────────┴────────────────────────────┐
│  Agent Orchestrator (Manraj)                    │
│  plan → select tool → execute → observe → loop  │
└──┬──────────┬──────────┬──────────┬─────────────┘
   │          │          │          │
┌──┴────┐ ┌───┴────┐ ┌───┴────┐ ┌───┴──────────┐
│Router │ │  RAG   │ │Sandbox │ │ Tool registry│
│  (M)  │ │  (M)   │ │  (M)   │ │     (M)      │
└──┬────┘ └───┬────┘ └────────┘ └──────────────┘
   │          │
┌──┴──────────┴───────────────────┐
│ Local inference (Ollama / vLLM) │
│ endpoint = config value         │
└─────────────────────────────────┘

Ingestion (H):    scan/PDF → OCR → normalized text → RAG index
Deliverables (H): structured agent result → .docx / .xlsx
Audit (H):        every action logged · egress counter = 0
```

---

## 5. Branch Split

Two branches, one interface contract, minimal overlap. Each of us owns a full vertical of the system end to end.

- `main` — protected. Only merges via PR.
- `feat/core-engine` — **Manraj** — reasoning engine: inference, routing, agent loop, tools, retrieval
- `feat/app-io` — **Harkamal** — everything the judges see and touch: ingestion, deliverables, interface, sovereignty proof

### Step 0 — do this together, on `main`, before branching (60–90 min)

Nothing else starts until this is merged. This is what lets us work in parallel without collisions.

- [ ] Repo skeleton, `requirements.txt`, `.gitignore`, `README.md`
- [ ] **`src/contracts.py`** — Pydantic models both sides code against:
  - `TaskRequest` (task text, attached file paths, task type hint)
  - `AgentStep` (step number, thought, tool called, tool input, tool output, duration)
  - `AgentResult` (final text, list of steps, deliverable paths, model used, sources cited)
  - `ToolSpec` (name, description, JSON input schema, callable)
  - `Document` (id, source path, page, text, metadata)
- [ ] **`src/api/main.py`** — FastAPI stubs returning hardcoded fake data matching the contracts, so both verticals can be built and tested independently from hour one
- [ ] `config.yaml` — model endpoint, model names, paths. No hardcoded URLs anywhere else.
- [ ] Agree the demo scenario in one sentence and write it in the README

**Merge protocol:** `contracts.py` changes require both of us to agree. Otherwise, PR into `main` at the end of each day, review, merge. Never work directly on `main` after Step 0.

---

## 6. Manraj — `feat/core-engine`

Owns the reasoning engine: everything between a task arriving and a structured result coming back.

**M1 · Local inference layer**
- [ ] Ollama running locally; pull a 7–8B instruct model (Qwen2.5-7B-Instruct or Llama-3.1-8B-Instruct)
- [ ] Parallel setup on the HF GPU box with a larger model for development
- [ ] `src/core/llm.py` — thin client reading endpoint + model name from `config.yaml`. Streaming support. **No provider SDKs.**
- [ ] Verify both endpoints work behind the same interface, swapped by config only

**M2 · Model router**
- [ ] `src/core/router.py` — classify incoming task as `document` / `code` / `general`
- [ ] Route to the configured model per task type (may be the same model with different system prompts and sampling params on the MVP — that is honest and sufficient)
- [ ] Emit the routing decision into the step trace for display. **The PS asks us to *show* selection across two task types — visibility is the requirement, not intelligence.**

**M3 · Tool registry**
- [ ] `src/core/tools.py` — registry, JSON-schema-described tools, dispatch by name
- [ ] Implement: `read_file`, `write_file`, `list_files`, `search_documents`, `run_python`
- [ ] Robust failure handling — a tool error must return to the agent as an observation, never crash the loop

**M4 · Agent loop**
- [ ] `src/core/agent.py` — plan → select tool → execute → observe → repeat → finalize
- [ ] Hard iteration cap (8 steps) and a wall-clock timeout. A runaway loop during the demo is fatal.
- [ ] Every iteration emits an `AgentStep`
- [ ] Graceful termination with partial results if the cap is hit

**M5 · Sandboxed execution**
- [ ] `src/core/sandbox.py` — run generated Python in a subprocess with a timeout, captured stdout/stderr, restricted working directory
- [ ] No network access from inside the sandbox (this doubles as sovereignty evidence)
- [ ] Return a structured result the agent can reason about and retry on

**M6 · Local RAG**
- [ ] Local embedding model (sentence-transformers, cached to disk — must work offline)
- [ ] Chunking with page/section metadata preserved
- [ ] Vector store: ChromaDB or FAISS, persisted locally
- [ ] `search_documents` tool wired to it, returning text **plus source document and page** for attribution

**M7 · Wire the real API**
- [ ] Replace the Step-0 stubs in `src/api/main.py` with real orchestrator calls
- [ ] Keep response shapes identical to the contracts so the interface layer needs zero changes

---

## 7. Harkamal — `feat/app-io`

Owns the multimodal input path, the real-file output path, the interface, and the sovereignty proof — everything the judges actually see, plus the evidence the whole claim rests on.

**H1 · Multimodal ingestion pipeline**
- [ ] `src/io/ingest.py` — accept PDF and image uploads
- [ ] OCR via Tesseract or PaddleOCR (both run locally; install language data in advance)
- [ ] PDF text extraction for born-digital files (PyMuPDF); OCR only when the text layer is empty
- [ ] Page-level segmentation so extracted findings retain their source location
- [ ] Normalize to the `Document` contract and hand off to the indexer
- [ ] Verify the entire pipeline runs with the network off

**H2 · Deliverable generation**
- [ ] `src/io/deliverables/docx.py` — build a real approval note with `python-docx`: header, reference number, findings table, recommendation, signature block. It must look like an actual industrial document, not a text dump.
- [ ] `src/io/deliverables/xlsx.py` — `openpyxl`, calculations with visible intermediate steps rather than just final values
- [ ] One clean template each; templates matter more than variety here
- [ ] Files written to a served downloads directory with predictable naming

**H3 · Web UI**
- [ ] Build against the Step-0 fake endpoints immediately — independent of engine progress
- [ ] Task input with file upload and document preview
- [ ] **Live agent step trace** — each step showing thought, tool called, result. This is the visual centerpiece; it makes the agentic behaviour legible to judges instead of a black box.
- [ ] Model routing badge — which model handled this task, and why
- [ ] Source attribution display for retrieved passages
- [ ] Deliverable download panel
- [ ] All assets local — no CDN fonts, no CDN JS. Vendor everything.

**H4 · Audit log + network monitor — the feature the entire claim rests on**
- [ ] `src/io/audit.py` — append-only log of every model call, tool call, and file access with timestamps
- [ ] Live UI panel: total operations, **external calls: 0**, running offline duration
- [ ] Back it with something real: a packet capture, `lsof -i` polling, or a firewall rule. It must survive a judge asking *"how do you know?"*
- [ ] Egress test in CI — any outbound call during a test run fails the build
- [ ] Rehearse the Wi-Fi-off demo until it is boring

**H5 · Demo corpus, offline verification and script**
- [ ] Write 3 synthetic inspection reports (clearly fictional refinery equipment), print and scan or degrade them so OCR is genuinely exercised
- [ ] 2 SOP documents and 1 maintenance manual for the RAG corpus
- [ ] Full offline verification pass — every dependency vendored, every model cached, cold start with no network
- [ ] Written demo script with timings, plus a fallback path for every step that can fail
- [ ] Pre-recorded backup video of the full flow — insurance against live failure

---

## 8. Timeline

**Day 0 (10 Sept, evening)** — Step 0 together. Manraj: Ollama running, model responding. Harkamal: environment setup, OCR installed and tested on one scanned page. *Gate: a model answers a prompt locally, and OCR reads a scan.*

**Day 1 (11 Sept)** — Manraj: M3, M4, M6. Harkamal: H1, H2, H3. *Gate by end of day: agent loop completes a multi-step task in the terminal; UI renders a step trace from fake data; a `.docx` is generated from sample text.*

**Day 2 (12 Sept)** — Morning: merge both branches, wire the real API (M7), fix integration breakage. Afternoon: M2 routing visibility, H4 network monitor, end-to-end run. Evening: rehearse. *Gate: full flow works with Wi-Fi off, twice in a row.*

**Feature freeze: 12 Sept, 4 PM.** After that, only bug fixes and rehearsal. Every team that keeps adding features until the last hour demos a broken product.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Agent loops or stalls mid-demo | Hard step cap, timeout, pre-warmed model, rehearsed prompts |
| Laptop thermal throttling (fanless M4) | Pre-warm before demo, short queries, GPU box as primary if available |
| OCR quality on degraded scans | Test on real scans Day 0; keep a clean-PDF fallback document |
| Integration day collapse | Contracts fixed at Step 0; both sides code against them from the start |
| Something needs the network at demo time | Full offline rehearsal on Day 2 — treat any egress as a P0 bug |
| Model too slow for live demo | Drop to a smaller quant; have the backup video ready |

---

## 10. What We Say in the Pitch

- We built what the PS asked for: an air-gapped agentic workbench on open-weight models, and we can prove the air gap rather than assert it.
- We scoped deliberately — one agentic flow done properly rather than six done badly — and we will say exactly what is stubbed.
- The architecture is model-agnostic by design, because the open-weight landscape moves fast and the PS explicitly asks that new models be addable without redesign.
- It produces real deliverables — a Word approval note, a spreadsheet with visible working — not chat replies.
