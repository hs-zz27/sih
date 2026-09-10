# Hardcoding register

Every shortcut, pinned value and canned path in this repository, in one place.

**Why this file exists.** The submission is judged, and our own proposal treats
honest descoping as a strength: *"Descoping deliberately and saying so is a
strength. Pretending we built everything is how teams get taken apart in Q&A."*
This file is what lets us answer *"is that real?"* in one sentence instead of
guessing on the spot.

**Rule:** if you pin a value, script a path, cache a result, or make something
deterministic so the demo behaves, add a row here in the same commit. A shortcut
that is not written down here is a liability, not a feature.

**The line we do not cross:** nothing may be presented as a live result when it
is not. Anything replayed or canned must be visibly labelled in the UI, in the
step trace, and here.

Last updated: 12 September 2026 · applies to `feat/training-and-demo`

---

## 1 · Demo scaffolding (`src/core/demo.py`, `config.yaml` → `demo:`)

| # | What | Where | Live or scripted? | Why | To make it genuine |
|---|---|---|---|---|---|
| 1.1 | **Rehearsed task presets** — three fixed prompts shown as buttons | `src/core/demo.py` → `PRESETS` | **Fully live.** Running a preset takes exactly the same code path as a typed task | Rehearsed prompts avoid typos and known-bad phrasings on stage | Nothing to undo — presets are a UI convenience, not a shortcut |
| 1.2 | **Temperature pinned to 0** whenever `demo.deterministic` is true | `src/core/demo.py` → `demo_temperature()`, called from `agent.py::_generate` | Live | A rehearsed run takes the same path twice. Greedy decoding is also simply correct for extraction and citation work | Set `demo.deterministic: false` to use `agent.temperature` |
| 1.3 | **Cached replay on failure** — a recorded run is replayed if live inference fails | `src/core/demo.py` → `load_replay()`, triggered in `orchestrator.py::_maybe_replay` | **Scripted, and labelled** | Insurance against the model dying mid-demo | Set `demo.fallback_enabled: false` — the demo then fails visibly |
| 1.4 | Replays are stamped `demo_replay: true` on every step, `[CACHED REPLAY …]` banner on the final text, `[cached replay]` on the routing reason | same | — | So a replay can never pass as live, on screen or in the trace | — |
| 1.5 | Replay fires **only on exact preset text match** | `demo.py::match_preset()` | — | If a judge edits the prompt they get the real outcome, including a real failure | — |
| 1.6 | Replay fires **only when the run genuinely FAILED** | `orchestrator.py::_maybe_replay` | — | A slow or truncated run is still shown as itself | — |

### How to record a replay

Run this on the demo machine, with the demo model, during rehearsal. A cache
recorded anywhere else is worse than none:

```bash
curl -X POST http://127.0.0.1:8000/api/demo/record/approval_note
```

It refuses to cache a failed run. `GET /api/demo` reports what is cached and
exactly which of the above switches are active — the running system will answer
a judge's question about this directly.

---

## 2 · Model and inference settings (`config.yaml`)

| # | What | Value | Why |
|---|---|---|---|
| 2.1 | All three task types point at one model | `qwen3.5:4b` | The proposal permits this explicitly: *"may point at the same model with different system prompts and sampling params; the requirement is that the routing decision is VISIBLE."* Routing is real and visible; the destination is currently shared |
| 2.2 | Thinking mode disabled | `inference.enable_thinking: false` | Qwen3.5 emits `<think>…</think>` before answering. Good for quality, ruinous for demo latency. The parser strips the tags either way, so re-enabling cannot break parsing |
| 2.3 | `keep_alive: 30m` | | Keeps weights resident so no step stalls on a model reload |
| 2.4 | Engine pre-warmed at boot | `src/api/main.py` → `lifespan` | Loads the embedding model at startup instead of during the judges' first query |

**Model choice, for the record.** Qwen3.5-4B (Apache 2.0, March 2026) replaced
the Qwen2.5-7B named in the original proposal. Reasons: 3.4 GB and 4B params so
it is fast on a fanless laptop; native tool calling, which the agent loop
depends on; and it is natively multimodal, which the problem statement asks for.
The 9B variant is a one-line config change for the GPU box.

---

## 3 · Synthetic content

| # | What | Where | Note |
|---|---|---|---|
| 3.1 | Demo corpus — fictional refinery documents | `data/corpus/` | Deliberate, per proposal hard rule #5: no real MRPL material. Equipment tags (E-4102, V-2207, GB-114) are invented |
| 3.2 | Training data is **fully synthetic**, generated from templates | `scripts/build_training_data.py` | No real inspection data. Trajectories are hand-authored patterns with randomised equipment, thicknesses and report numbers |
| 3.3 | Seed equipment/threshold lists live in the generator | `build_training_data.py` → `EQUIPMENT`, `THRESHOLD_QUERIES`, `REPORT_IDS` | So the dataset is reproducible from the repo alone, before the corpus is merged |

---

## 4 · Still stubbed (not hardcoding — unbuilt)

Tracked here so the two lists are never confused. Grep for them: `grep -rn "STUB (" src/`

| # | What | Where | Owner |
|---|---|---|---|
| 4.1 | OCR extraction in the upload endpoint returns canned text | `src/api/main.py` → `/api/ingest` | H1 — real pipeline exists in `src/io/ingest.py` on `feat/app-io`, not yet wired in |
| 4.2 | Audit log served from fixtures | `src/api/main.py` → `/api/audit` | H4 — not built |
| 4.3 | **Network monitor is an assertion, not evidence** | `src/api/main.py` → `/api/network` | H4 — not built. This is the single most important gap: the proposal calls it "the feature the entire claim rests on" |
| 4.4 | Deliverable files are placeholder text | `src/api/fakes.py` | H2 — real generators exist on `feat/app-io`, not yet wired to the agent |
| 4.5 | **The agent cannot produce a `.docx`** | `src/core/tools.py` | No `create_approval_note` tool is registered. `build_approval_note()` exists but nothing in the loop calls it |

---

## 5 · What is genuinely real

Stated plainly, because the list above is long and the honest counterweight matters:

- The agent loop, tool dispatch, step cap, timeout and repeat detection are real.
- Retrieval is real: local embeddings, a persisted index, citations with page numbers.
- The sandbox is real: subprocess isolation, timeout, and network genuinely blocked — generated code that tries to connect raises `SandboxNetworkBlocked`, and the attempt appears in the trace.
- The endpoint guard is real: `LLMClient` refuses to construct against a non-local endpoint, and `tests/test_sovereignty.py` fails the build on any provider SDK import or hosted endpoint reference.
- Routing is real and explained in words on every run.
- 94 tests pass with no model server and no network.

---

## 6 · Before the demo

- [ ] `ollama pull qwen3.5:4b`
- [ ] `python scripts/fetch_models.py --verify` **with Wi-Fi off**
- [ ] `POST /api/index` to index the corpus
- [ ] Record a replay for each preset (`POST /api/demo/record/<id>`) on the demo machine
- [ ] `GET /api/demo` — confirm what is pinned matches this file
- [ ] Full run with Wi-Fi off, twice
- [ ] Decide as a team which rows above get said out loud in the pitch. Recommended: 1.3 and 4.3, unprompted — volunteering the weak points is what makes the strong claims credible
