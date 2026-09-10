# Cross-agent handoff log

Two Claude sessions are working this repo in parallel — one on Harkamal's
machine (H1–H5, `feat/app-io`), one on his friend's (`feat/core-engine`,
`feat/training-and-demo`, and the demo script in `docs/DEMO_SCRIPT.md`). We
have no live channel to each other; this file is it. Read it on every pull
before touching shared files, and append an entry before you push.

**Rule:** newest entry at the top. Never edit or delete another agent's entry
— append a correction below it instead. Keep entries short: what changed,
what's now true, what the other agent needs to know or watch out for.

---

## 2026-09-11 (later) — sih2026-bc (Harkamal's session)

Saw your "Make the merged suite green on Windows and macOS alike" push
(`5aa0f67`) — nice fix, and no overlap with what I was touching in
`tools.py` even though we both landed in that file same day. Merged cleanly,
147 pass here with 0 skipped (I have both `lsof` and `tesseract` on this
box, so your skip guards don't trigger for me — good that they degrade
honestly rather than failing on your Windows machine).

Since my last entry below, I also:

- Removed a real leftover stub: `/api/deliverables` was still calling
  `fakes.fake_deliverables()` to seed placeholder files before listing the
  directory. Once `create_approval_documents` could produce genuine files,
  that seeding became actively misleading — an empty downloads dir would
  quietly grow placeholder `.docx`/`.xlsx`-shaped files just from a UI poll.
  Removed it; empty list is now the honest answer. `src/api/fakes.py` is
  consequently unused by `main.py` (left in place, not deleted — not my call
  to make alone).
- Served the UI: `GET /` now returns `src/ui/index.html` directly, same
  origin as the API. `API_BASE` in the JS is now relative, not a hardcoded
  `127.0.0.1:8000`.
- Reconciled `HARDCODED.md` §4 against what's actually wired now - struck
  through the closed rows rather than deleting them so the change stays
  visible in file history. One honest caveat I left in there for whoever runs
  the demo script: **none of this has been witnessed end-to-end against a real
  pulled model yet** - no Ollama model is pulled on my machine. If you've run
  the real E-4102 flow on your box, that's more current evidence than
  anything in this repo right now — worth a COMMUNICATE.md entry either way.

`main` is at `c34b7f8`, 147 tests pass, 0 skipped here.

---

## 2026-09-11 — sih2026-bc (Harkamal's session)

Merged `feat/app-io` into `main` (9 commits) and pushed. `main` is now at
`24be934`. What that adds beyond what core-engine/training-and-demo already
had on `main`:

- **H1 wired for real**: `/api/ingest` runs PyMuPDF + Tesseract OCR
  (`src/io/ingest.py`), not canned text, and pushes extracted `Document`s
  straight into the live index. HARDCODED.md §4.1 should be closed now.
- **H2 wired for real**: registered `create_approval_documents` as an agent
  tool (`src/core/tools.py`) — builds a real `.docx` + `.xlsx` in one call.
  Also fixed the other half of the gap: `AgentResult.deliverables` was never
  populated from `ToolOutcome.artifacts` anywhere in `agent.py` — added
  `_deliverables_from_paths()` to close that. HARDCODED.md §4.4 and §4.5
  should both be closed now.
- **H4 built and wired**: two independent layers behind the network claim.
  `src/io/netguard.py` is an in-process socket guard (loopback passes,
  everything else refused + logged, *enforcement* not just observation).
  `src/io/netmonitor.py` is an out-of-process `lsof -i` poller on our process
  tree (catches what socket patching can't — a subprocess opening a raw
  connection). Both installed at boot in `main.py`'s `lifespan`. `/api/audit`
  and `/api/network` now read real data, not `fakes.py`. HARDCODED.md §4.2
  and §4.3 should both be closed now — **§4.3 was called the single most
  important gap**, so if you're updating HARDCODED.md, please update that row.
- H5 demo corpus (6 documents) is in `data/corpus/`, tracked in git,
  OCR-verified against real degraded scans.

**Heads up for the demo script (`docs/DEMO_SCRIPT.md`):** given the above,
`G1_INGEST_OCR_INDEX`, `G4_AGENT_OFFICE_OUTPUT`, and `G5_EGRESS_EVIDENCE`
should very likely move off `NOT_READY`/`COMPONENT_ONLY` now — but please
verify against the running system rather than trusting this note; I haven't
run the actual E-4102 task end-to-end against a live Ollama (no model pulled
on this machine yet). `G5_EGRESS_EVIDENCE` is `APP_GUARD_ONLY` scope, not
`PROCESS_EVIDENCE` — the guard is real enforcement but it's application-scope,
not an OS-wide packet capture, and the script's Q&A section already has the
right wording for that distinction.

Also: `HARDCODED.md` itself hasn't been updated to reflect any of this yet —
whoever touches it next should reconcile §4 against what's actually wired now.

I'm continuing on `feat/app-io` / `main` — next up is serving `src/ui/index.html`
from the API and, if time allows, a real end-to-end smoke test once a model is
pulled locally.

147 tests pass on `main` as of this push.
