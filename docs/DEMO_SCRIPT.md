# Six-minute live-demo script — SIH26117 / MRPL

## Purpose and release rule

This is the stage runbook for the **Sovereign On-Premise Agentic AI Workbench**. It is deliberately written with capability gates because the integration status is changing during the build. It supplies the timing, screen direction, exact audience wording, fallbacks, and Q&A now; the final release owner selects the truthful branch for each gate immediately before rehearsal.

Do not turn a planned capability, a component-level test, or an old `HARDCODED.md` row into an on-stage claim. The release source of truth is, in order:

1. The observed result from the running demo route.
2. The finalised `HARDCODED.md` register.
3. This script.

The full headline flow — scan → OCR → local clause retrieval → sandbox calculation → `.docx` + `.xlsx` → Wi-Fi-off repeat — may be called an end-to-end live demo **only when every required gate below is `LIVE`**. Otherwise use the supplied limited-path wording and explicitly identify the boundary.

## Capability gates — fill before the final rehearsal

Set each bracket to exactly one of its allowed values. Leave no gate as `UNKNOWN` on stage.

| Gate | Set before rehearsal | What counts as evidence | Required for the headline claim |
|---|---|---|---|
| `G0_LOCAL_RUNTIME` | `[LIVE / NOT_READY]` | Health view shows the configured local model is reachable, required models are present, and the local index is ready. | Yes |
| `G1_INGEST_OCR_INDEX` | `[LIVE / COMPONENT_ONLY / NOT_READY]` | A fresh upload through the audience-facing route returns extraction from that file, identifies the extraction method, and makes its pages searchable in the local index. | Yes |
| `G2_LOCAL_RETRIEVAL` | `[LIVE / NOT_READY]` | A fresh local search returns a real citation with source and page. The health/result view identifies whether retrieval is semantic or lexical. | Yes |
| `G3_AGENT_SANDBOX` | `[LIVE / COMPONENT_ONLY / NOT_READY]` | The agent trace shows a real `run_python` call and its sandbox stdout/error. | Yes |
| `G7_LIVE_STEP_TRACE` | `[LIVE / REPLAYED_AFTER_COMPLETION / NOT_READY]` | The UI starts a background task and receives its first routing/step event before the final result exists. | Yes, for the claim that the trace is live |
| `G4_AGENT_OFFICE_OUTPUT` | `[LIVE / COMPONENT_ONLY / NOT_READY]` | The current task creates downloadable, openable `.docx` and `.xlsx` files. The Word file contains the run’s finding/citation; the workbook contains visible formulas. | Yes |
| `G5_EGRESS_EVIDENCE` | `[PROCESS_EVIDENCE / APP_GUARD_ONLY / NOT_READY]` | Evidence scope is visibly identified: process/host capture for `PROCESS_EVIDENCE`, or an application-level enforcing guard and append-only audit for `APP_GUARD_ONLY`. | Yes, at the stated scope |
| `G6_WIFI_OFF_CONTINUITY` | `[LIVE / NOT_READY]` | The selected local task completes after Wi-Fi is disabled, with no dependency added during the demo. | Yes |

`COMPONENT_ONLY` means that a module may be implemented and tested independently, but it is not part of the user-visible agent flow. It must never be described as the result of the upload or the agent task.

### Three non-negotiable screen labels

Put these labels in the presenter notes or on a small local status card; they prevent a fast demo from becoming a misleading one.

- `LIVE` — produced by the current task through the displayed route.
- `COMPONENT ONLY` — real component, not wired into this task; do not use it as end-to-end evidence.
- `NOT READY` — not demonstrated; do not substitute a screenshot, old output, or manually pasted text.

## Preflight — complete before the six-minute clock starts

This is rehearsal work, not part of the pitch.

1. Fill the gate table from the running system, not from memory. Capture the exact evidence URL, test, or audit view beside each selected value.
2. Confirm `G0_LOCAL_RUNTIME=LIVE` with the local health view. If it is not live, do not attempt a purported live demo.
3. For the full flow, run the exact E-4102 task twice with Wi-Fi off. Confirm the scan is searchable, the citation is from the local corpus, the sandbox trace is present, and the Office files open.
4. Open the original fictional E-4102 scan, the app, the system Wi-Fi panel, and an empty folder for this run’s downloads. Do not pre-open an old Word or Excel file.
5. Check the retrieval backend shown by the running system. If it says TF-IDF/lexical, use the word “lexical”; do not call it semantic search.
6. If replay is enabled, inspect the demo status and cache location. A fallback may be used only for an exact, unedited preset after a genuine failure, and its `[CACHED REPLAY …]` banner and per-step `demo_replay: true` marker must be visible.
7. Pre-warm the local model and keep the backup device/video ready. A backup video must begin with an on-screen `RECORDED OFFLINE REHEARSAL — NOT LIVE` card.

## Running order — exactly six minutes

### 0:00–0:30 — Set the claim boundary

**Screen:** the local capability/status card beside the workbench; the project title and the fictional E-4102 scan thumbnail are visible. Do not show a green “zero egress” metric without its evidence-scope label.

**Operator action:** start the timer. Point to the status card, then to the fictional-data label.

**Say:**

> “Good morning. This is SIH26117 for MRPL: a sovereign, on-premise agentic AI workbench. Today’s E-4102 report and SOP corpus are fictional; we do not put real MRPL data into this demo. I will distinguish a live result from an integration that is still being wired, rather than asking you to take either on faith.”

**Say this transparency line if replay is enabled:**

> “One demo safeguard is enabled: if a local model genuinely fails on one exact rehearsal prompt, the system may show a recorded run. It is visibly labelled `CACHED REPLAY`; it is never presented as live.”

### 0:30–0:55 — Prove readiness before asking the agent to work

**Screen:** `GET /api/health` or the equivalent local readiness panel. Keep the local endpoint, model readiness, index state, and retrieval-backend state readable.

**Operator action:** point out the loopback/local endpoint and the index status; do not dwell on configuration values.

**Say if `G0_LOCAL_RUNTIME=LIVE`:**

> “This is the preflight from the running machine. The model service and corpus index shown here are local and ready. The retrieval mode is reported here as well, so we will not call a lexical fallback semantic search.”

**Say if `G0_LOCAL_RUNTIME=NOT_READY`:**

> “The local runtime is not ready. I will not manufacture a live run from a planned path; I am moving directly to the labelled rehearsal fallback.”

Then use **Thermal Handoff / Labelled Backup Video** below and do not continue the live-flow claims.

### 0:55–1:35 — Upload the fictional E-4102 inspection scan

**Screen:** the original local scan on the left; the upload form and raw ingest response on the right. Keep any warning, extraction method, page count, confidence, and indexing status visible.

**Operator action:** upload the scan during this window. Do not paste a transcript into the task box.

**Say in every branch:**

> “I am uploading the fictional scanned inspection report for heat exchanger E-4102. The original image remains visible so you can compare it with whatever the route actually returns.”

**Then say if `G1_INGEST_OCR_INDEX=LIVE`:**

> “This route has extracted these pages from this upload, identifies the extraction method, and has made them available to the local index. The agent will work from that visible result, not from a hidden transcript.”

**Then say if `G1_INGEST_OCR_INDEX=COMPONENT_ONLY`:**

> “The OCR component is not wired into this user-facing route yet. I will not call the transcript on this screen live OCR, and I will not represent later work as having been derived from this upload.”

**Then say if `G1_INGEST_OCR_INDEX=NOT_READY`:**

> “Ingestion is not ready in this build. I am stopping the end-to-end claim here rather than substituting typed report text.”

Use **Wiring Hold** below if the gate is not live.

### 1:35–2:20 — Retrieve the governing clause locally

**Screen:** the live local search/agent trace with the returned source passage, document name, page number, score, and retrieval backend. The original scan should remain available in a smaller pane.

**Operator action:** run the local SOP query. Prefer a query built from the actual uploaded report only when `G1_INGEST_OCR_INDEX=LIVE`; otherwise use this as a standalone retrieval demonstration and label it as such.

**Say in every branch:**

> “The next step is evidence, not guesswork. I am searching the local SOP corpus, and I will rely only on the source and page that appear on screen.”

**Say if `G2_LOCAL_RETRIEVAL=LIVE`:**

> “This citation is the governing passage for the calculation. It names its local source and page, so an engineer can inspect the evidence rather than accept an uncited chat answer.”

**Only if the visible passage actually contains the expected threshold, add:**

> “The returned clause says readings below 80 percent of nominal must be escalated; that is the rule the next step tests.”

**Say if `G2_LOCAL_RETRIEVAL=NOT_READY` or the result is empty:**

> “There is no returned citation for this claim. Without a source, the workbench has no basis to recommend an engineering action, so I will not calculate a pass-or-fail outcome.”

Use **Citation Reset** below if the first query is empty.

### 2:20–3:25 — Make the agent’s calculation legible

**Screen:** the live agent trace. Keep the routing decision, tool name, tool input, sandbox output, duration, and result visible. Do not hide a replay banner.

**Operator action if `G1`, `G2`, and `G3` are all `LIVE`:** run the exact E-4102 approval task against the current upload. The trace must show retrieval and `run_python`; it must not be narrated as having happened if either is absent.

**Say on the full-flow path:**

> “Now the agent is working in steps: it routes the task, uses the cited procedure, and asks the sandbox to compute the remaining wall margin. Watch the tool trace rather than a single final paragraph.”

**When the `run_python` result appears, say:**

> “The numbers, code input, and stdout are visible here. This calculation ran in the sandbox; the model did not simply do the arithmetic in prose.”

**If the screen shows the E-4102 values and result, add:**

> “The displayed remaining percentage and threshold margin are the basis for the recommendation, and both are traceable to the report values and cited clause on screen.”

**Operator action if any of `G1`, `G2`, or `G3` is not `LIVE`:** run the exact fixed **Wall-loss calculation** preset only if `G3_AGENT_SANDBOX=LIVE`. It uses its own visible inputs and is a standalone sandbox demonstration.

**Say on that limited path:**

> “The scan-to-decision integration is not live in this build, so I am separating the proof of the calculation tool from the report flow. This fixed wall-loss task is not being presented as a result derived from the upload.”

**Say if `G3_AGENT_SANDBOX=NOT_READY`:**

> “The sandbox is not available for this run. I will not show hand arithmetic as though it were an agent tool result.”

### 3:25–4:10 — Open the actual Office outputs, or state the integration boundary

**Screen when `G4_AGENT_OFFICE_OUTPUT=LIVE`:** the downloads panel followed by the newly created Word file and the newly created workbook. In Word, show the finding, citation, recommendation, and signature block. In Excel, show the formula bar and visible intermediate calculations.

**Operator action when live:** open files created during this run from the clean downloads folder. Check the task identifier/time before opening them.

**Say when `G4_AGENT_OFFICE_OUTPUT=LIVE`:**

> “These are files produced by this task, not screenshots. This Word approval note carries the finding and its source, and this Excel assessment exposes the working through live formulas.”

**Screen when `G4_AGENT_OFFICE_OUTPUT=COMPONENT_ONLY`:** the capability card and the current task’s result/download list. Do not open a separately generated Office file as if it came from this task.

**Say when `G4_AGENT_OFFICE_OUTPUT=COMPONENT_ONLY`:**

> “The Office-generation component is not connected to the agent path in this run. I will not present a separately created Word or Excel file as the output of this task.”

**Say when `G4_AGENT_OFFICE_OUTPUT=NOT_READY`:**

> “Office output is not ready for this run, so there is no Word or Excel deliverable claim today.”

### 4:10–5:20 — Wi-Fi-off finale, with the correct proof scope

**Screen:** system Wi-Fi control and the workbench side-by-side. After disabling Wi-Fi, show the local app and the selected local probe task. Keep the egress/audit evidence scope visible; do not imply that a UI counter proves more than it does.

**Operator action:** turn Wi-Fi off in full view. Then run the small, pre-warmed local probe selected at rehearsal — normally the cited SOP lookup or a short sandbox calculation. Do not switch to a cloud endpoint or download anything.

**Say before toggling:**

> “I am now turning Wi-Fi off. The only claim I will make after this switch is the claim supported by the result and the egress evidence visible on this machine.”

**Say if `G6_WIFI_OFF_CONTINUITY=LIVE` after the probe completes:**

> “Wi-Fi is off, and this local task has completed. That demonstrates continuity of the demonstrated local path without a runtime download or a remote model call.”

**Say if `G6_WIFI_OFF_CONTINUITY=NOT_READY` or the probe fails:**

> “Wi-Fi is visibly off, but this run has not completed the local probe. I will not call that an offline end-to-end demonstration.”

**Then use the exact `G5_EGRESS_EVIDENCE` line:**

**For `PROCESS_EVIDENCE`:**

> “Our egress evidence is the process or host-level capture identified on screen. Its scope is stated here, and this run produced no recorded external connection in that scope.”

**For `APP_GUARD_ONLY`:**

> “Our in-process guard blocks non-loopback connection attempts and records refusals in an append-only audit trail. That is application-scope enforcement; I am not representing it as an operating-system-wide packet capture.”

**For `NOT_READY`:**

> “The current egress display is not evidence sufficient for a zero-egress claim. The Wi-Fi-off continuity check is all I am claiming in this build.”

### 5:20–6:00 — Close with the exact demonstrated boundary

**Screen:** a compact four-line summary: `Input`, `Evidence`, `Computation`, `Output / Offline proof`, each marked with its gate state. Keep the fictional-data label visible.

**Say if all `G0`–`G6` gates required for the full flow are live:**

> “In six minutes, we uploaded a fictional inspection scan, extracted and retrieved its local evidence, computed the remaining wall margin in a sandbox, produced openable Word and Excel deliverables, and repeated a local task with Wi-Fi off. The trace, citation, files, and evidence scope are all visible. That is the narrow workflow we are claiming — not a general-purpose cloud assistant.”

**Say if one or more gates are limited or not ready:**

> “Today I demonstrated the parts marked live on this screen and identified the parts that are not yet connected. We will not turn a component test or a planned integration into a production claim. The full scan-to-deliverable, Wi-Fi-off flow is released only after every gate is live.”

**Finish in every branch:**

> “The corpus is fictional, the model path is local, and the evidence is inspectable. Thank you.”

## Named fallbacks and stop rules

Every fallback is a transparent change of plan, never a silent substitution.

| Failure point | Named fallback | Operator action | Exact words | Never do this |
|---|---|---|---|---|
| Health check, missing local model, or browser/app failure | **Red Readiness Gate** | Stop the live flow. Show the failed readiness result, then use the labelled backup only if one exists. | “The local runtime is not ready, so I am not calling this a live demonstration.” | Do not start a cloud service, install/download a dependency, or hide the error. |
| Model stalls but has not failed | **Honest Timeout** | Give the preset its announced time box. End it with its visible slow/truncated status. | “The live run is slow, not failed. A replay would be misleading here, so I am stopping this live attempt.” | Do not replace a slow/truncated run with cached steps. |
| Model genuinely fails on an exact, unchanged rehearsal preset | **Labelled Cached Replay** | Allow the built-in fallback only if a cache exists and the UI shows the replay banner, `[cached replay]` routing reason, and `demo_replay: true` per step. | “The local model failed. The banner identifies this as a recorded run of this exact task, not a live result.” | Do not use it for a judge-edited prompt, a custom E-4102 prompt, a slow run, or a corrupted cache. |
| OCR misses text or has poor confidence when `G1` is live | **Clean-PDF Fallback** | Switch to the separately labelled clean copy of the same fictional report, rerun the same local OCR route, and retain the original scan on screen. | “The degraded scan was not read reliably. This is the clean-copy OCR fallback, not evidence that the first scan succeeded.” | Do not manually correct the transcript and call it OCR. |
| OCR route is `COMPONENT_ONLY` or `NOT_READY` | **Wiring Hold** | Show the gate/result; move to the standalone live components only if labelled as standalone. | “This route is not wired for live OCR, so the scan is not an input to the later demonstration.” | Do not display canned extraction as a live result. |
| Retrieval returns no result | **Citation Reset** | Retry once with the exact SOP title and visible technical terms. If it remains empty, use **Evidence Stop**. | “I am narrowing the local query once. If no source returns, the correct result is ‘no evidence,’ not a guessed limit.” | Do not quote a clause from a slide, a prior run, or memory. |
| Retrieval remains empty | **Evidence Stop** | End recommendation and output claims; show the empty result. | “No local citation was returned, so the workbench is correctly withholding a recommendation.” | Do not continue to an approve/reject conclusion. |
| Sandbox errors or times out | **Sandbox Retry** | Retry once with the same small, visible arithmetic only after the trace shows the error; otherwise stop. | “The sandbox returned an error. I will make one visible retry; I will not replace it with hand calculation.” | Do not edit stdout or announce an unexecuted result. |
| Word/Excel file is absent, cannot open, lacks the task’s finding, or formula cells are values | **Open-File Gate** | Do not open any older file. State that output integration did not pass its acceptance check. | “The task did not produce a verifiable Office deliverable, so I am not making a document-generation claim.” | Do not use a pre-existing `.docx`/`.xlsx` as proof. |
| Wi-Fi-off probe fails | **Offline Stop** | Keep Wi-Fi off, show the local error, and state the failed proof boundary. | “The offline probe did not complete. I am not calling this an air-gapped end-to-end result.” | Do not turn Wi-Fi back on and quietly rerun it as the finale. |
| Egress guard reports a non-loopback attempt | **Egress Violation Stop** | Stop the task and show the refusal/audit evidence. Treat it as a P0 defect. | “The guard refused an external connection attempt. The safety control worked, but this run cannot support a zero-egress claim.” | Do not clear logs, retry with the guard disabled, or claim success. |
| Laptop thermal throttle or model latency threatens the clock | **Thermal Handoff** | First use the pre-warmed, fully local on-premise GPU box only if it was preflighted with the same assets and gate evidence. Otherwise switch to the labelled video. | “This laptop is thermally throttling. I am moving to the pre-warmed local fallback; if I use the recording, it will be labelled as an offline rehearsal, not live.” | Do not switch to a hosted endpoint or imply a recording is a live run. |

## Likely judge questions and exact answers

### 1. “How do you know no external call was made?”

**Answer for `PROCESS_EVIDENCE`:**

> “We do not rely on a green UI number alone. The evidence shown is the named process or host-level capture, with its scope visible. The Wi-Fi-off rerun shows the local path continues without connectivity, and the capture reports no external connection in its stated scope.”

**Answer for `APP_GUARD_ONLY`:**

> “The current control is an enforcing in-process guard: non-loopback connection attempts are refused and appended to an audit trail. It is strong application-scope evidence, but it is not an OS-wide packet capture; we say that distinction plainly. The Wi-Fi-off rerun is a separate continuity check.”

**Answer for `NOT_READY`:**

> “We do not yet have evidence sufficient to prove zero egress. We can show a Wi-Fi-off continuity check, but we are not representing that as complete network proof.”

### 2. “Is the OCR and are the Word/Excel outputs actually live?”

**Answer:**

> “The status card separates that precisely. `LIVE` means this task produced the result through the displayed route; `COMPONENT ONLY` means the underlying module exists but is not connected to this task; `NOT READY` means we do not claim it. I can show the fresh upload, task trace, and newly created files only for capabilities marked `LIVE`.”

If a gate is not live, name it and stop there. Do not answer with a future roadmap as though it were a present result.

### 3. “How is this agentic, and could the demo be a canned replay?”

**Answer:**

> “The live trace exposes the routing decision, tool calls, observations, source citations, and sandbox output. The model may use the same local weights for multiple task types in this MVP; the routing decision is visible rather than hidden. We do have a failure-only rehearsal replay, but it can run only on an exact preset after a genuine failure, and it labels every replayed step. If you edit the prompt, it cannot replay and you will see the real outcome, including a real failure.”

## HARDCODED.md disclosures to volunteer unprompted

Reconcile this list with the final register immediately before the rehearsal. Do not recite a stale §4 item as current fact after that register changes.

| When | Rows to say aloud | Exact short disclosure |
|---|---|---|
| Opening, if fallback is enabled | **1.3**, plus the visible safeguards in **1.4–1.6** | “A cached run is allowed only after a genuine failure of an exact preset, and every replay is visibly labelled.” |
| Routing beat | **2.1** | “The routing decision is live and visible; this MVP may route task types to the same local model with different instructions.” |
| Opening or close | **3.1** | “All refinery documents and equipment tags in this demo are synthetic and fictional.” |
| Any determinism question | **1.2** and, if relevant, **2.2–2.4** | “Demo sampling is pinned for repeatability and the local model is pre-warmed; those are disclosed rehearsal settings, not hidden evidence.” |
| Before an OCR, Office-output, audit, or network claim | **Every final active §4 row that covers that claim** | Use the applicable `LIVE`, `COMPONENT ONLY`, or `NOT READY` line in this script. |

The last published register explicitly recommended volunteering **1.3** and **4.3**. Keep that recommendation as a review prompt, but replace the §4 disclosure with the final register’s actual state once wiring is complete.

## Finalisation pass when the definitive capability list arrives

1. Set `G0`–`G6` from the definitive list and observed rehearsal evidence.
2. Delete the unused alternative speech branches only after preserving this file’s change history, or mark the chosen lines in presenter notes.
3. Update the active-§4 disclosure row to the final `HARDCODED.md` identifiers and wording.
4. Rehearse the exact chosen path twice with Wi-Fi off, including every selected fallback.
5. If any gate changes after rehearsal, return it to `NOT_READY` until the evidence is rerun. The timing and honesty rules do not change.
