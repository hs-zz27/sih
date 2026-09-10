# Future scope — what the MVP deliberately does not build

The SUTRA proposal describes the full product. This MVP builds the spine of it
and stops, on purpose.

Everything below is **designed for and not implemented**. Each entry says where
it would attach, so "future scope" means a known next step rather than a polite
way of saying we did not think about it.

**Why this file exists.** Our own planning doc puts it plainly: *"Descoping
deliberately and saying so is a strength. Pretending we built everything is how
teams get taken apart in Q&A."* Volunteering this list is what makes the built
half credible.

See `HARDCODED.md` for demo shortcuts — a different thing. This file is about
capability we have not written. That one is about corners cut in what we have.

---

## 1 · P&ID and drawings as data

**Proposal:** symbol detection, tag OCR, a DEXPI-style equipment graph, so
*"what feeds V-101"* becomes a real query.

**Status:** not started. Drawings are currently ingested as page images and
reached through OCR text, so a P&ID is searchable as text but not as a graph.

**Where it attaches:** a `pid_graph` tool in `src/core/tools.py` alongside
`search_documents`, backed by a detector in `src/io/`. The agent loop, the step
trace and the citation path need no changes — a graph query is just another
tool returning an observation.

**Why it is out:** a symbol detector needs a labelled dataset and training time
we do not have before the deadline, and a bad detector is worse than none in a
safety context. The proposal's own risk table already scopes this to *"clean or
vector drawings and about a dozen common symbols"* with a confidence score.

---

## 2 · Permission-aware retrieval

**Proposal:** access rights checked *before* search, so a contractor's question
never touches restricted pages.

**Status:** not started. Every indexed document is visible to every query.

**Where it attaches:** a filter on `RagIndex.search()` in `src/core/rag.py`.
`Document.metadata` is a free-form dict and already carries per-chunk data, so a
`clearance` key plus a caller identity is the shape — no contract change.

**Why it is out:** it needs an identity model, and identity needs auth, and auth
needs a user store. That is a vertical of its own. Note the honest ordering
here: filtering at retrieval is the *correct* design (post-filtering a model's
answer leaks the content into the context first), so this is a real feature
worth its own sprint, not a checkbox.

---

## 3 · Voice and Indic languages

**Proposal:** spoken questions in Hindi, Kannada and English —
faster-whisper, then AI4Bharat IndicConformer and Piper.

**Status:** not started. Text in, text out.

**Where it attaches:** an `/api/transcribe` endpoint producing a `TaskRequest`.
Nothing downstream changes: the agent never learns where the text came from.

**Why it is out:** it adds a model, a runtime and a set of language assets to
the offline bundle for no gain in the core claim. It is the cheapest item on
this list to add later, and the least persuasive to a judge if the document
pipeline is not already convincing.

---

## 4 · Guardrail models

**Proposal:** Llama Guard or Granite Guardian on input and output; NeMo
Guardrails with plant policies.

**Status:** not started. What *is* real: a tool allowlist, filesystem
confinement to the workbench directories, and a sandbox with no network.

**Where it attaches:** a wrapper around `LLMClient.chat` in `src/core/llm.py`.

**Why it is out:** a guardrail model doubles inference cost per turn on a
machine already running a 14B, and it defends against a threat model
(adversarial users) that an air-gapped internal tool largely does not have. The
structural controls above are the ones that matter here, and they are built.

**Related and genuinely open:** prompt injection via an uploaded document. We
treat retrieved text as data by convention and prompt, not by enforcement. A
malicious SOP could in principle steer the agent. Worth stating plainly if
asked, rather than claiming a defence we have not written.

---

## 5 · PPTX output

**Proposal:** `.pptx` among the deliverables.

**Status:** not started. `.docx` and `.xlsx` are real and the agent produces
them via `create_approval_documents`.

**Where it attaches:** a `python-pptx` builder in `src/io/deliverables/`, then
one more tool. `DeliverableKind.PPTX` already exists in the contracts.

**Why it is out:** it is the same pattern as `.docx` with none of the additional
proof. Two real Office formats demonstrate the capability; a third demonstrates
stamina.

---

## 6 · Hash-chained audit trail

**Proposal:** a hash-chained trail, reproducible months later.

**Status:** partially built. `src/io/audit.py` writes a real append-only JSONL
log of every model call, tool call and file access. It is **not** hash-chained,
so it is tamper-evident only insofar as the filesystem is.

**Where it attaches:** one field in the audit record — each entry carrying the
hash of the previous one.

**Why it is out:** genuinely small, and a fair criticism that it is not done.
Say "append-only, not yet hash-chained" rather than letting a judge find the
gap.

---

## 7 · Multi-user serving and scale

**Proposal Phase 2:** vLLM or SGLang, Qdrant with BGE-M3 and a reranker,
Postgres/pgvector, Neo4j.

**Status:** single-user by construction. Ollama, one local index, in-memory run
registry.

**Where it attaches:** mostly already abstracted. `inference.api_style` switches
the client to an OpenAI-compatible server (vLLM) with no code change, and
`SUTRA_API_STYLE` does it without even editing config. Retrieval sits behind
`RagIndex`, so a different vector store is one class.

**Why it is out:** an MVP demoed to three judges on one laptop has no concurrency
to serve. The point of the abstraction is that scaling is a deployment decision,
not a rewrite — which is the claim worth making.

---

## 8 · Handwriting

**Proposal:** handwritten notes as multimodal input.

**Status:** not started, and it was explicitly out of scope in the original
plan: *"Handwriting recognition beyond what OCR gives us."*

**Why it is out:** Tesseract does not do cursive. Doing this properly means a
handwriting model and a labelled sample set. Printed and degraded scans are
handled, and that is what an inspection report actually looks like.

---

## What is built, for contrast

Local inference with a real two-model tier; a routing decision explained in
words on every run; an agent loop with a step cap, wall-clock timeout, repeat
detection and partial results; five tools plus real `.docx`/`.xlsx` generation;
local retrieval with page-level citations; a sandbox that genuinely blocks
egress; two independent layers of egress evidence plus an append-only audit log;
a web UI with a live step trace; and a container topology with no route off the
host.

That is the spine. Everything above hangs off it without redesign, which is the
actual argument this list is making.
