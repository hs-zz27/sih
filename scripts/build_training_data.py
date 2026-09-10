"""Build the fine-tuning dataset for the agent protocol.

Why this exists
---------------
The single biggest live-demo risk is a small model that will not reliably emit
``{"thought": ..., "tool": ..., "tool_input": {...}}``. Retrieval supplies the
facts and the sandbox does the arithmetic, so the model's real job is narrow:
pick the right tool and format the call correctly. That is exactly the kind of
job a LoRA fixes.

The important property
----------------------
Every example is rendered through ``src.core.prompts.system_prompt`` and the
**live tool registry**. Training on a hand-written approximation of the prompt
would teach the model a format the runtime never sends, which is the usual way
this kind of fine-tune quietly does nothing. If the prompt or the tool schemas
change, re-run this script and retrain.

Output
------
``data/training/agent_sft.jsonl`` - one JSON object per line:

    {"messages": [{"role": "system", ...}, {"role": "user", ...},
                  {"role": "assistant", ...}, ...]}

which is the conversational format ``trl``'s ``SFTTrainer`` consumes directly.

Usage
-----
    python scripts/build_training_data.py                 # default corpus
    python scripts/build_training_data.py --n 600 --seed 7
    python scripts/build_training_data.py --stats
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.contracts import TaskType  # noqa: E402
from src.core import prompts  # noqa: E402
from src.core.rag import RagIndex  # noqa: E402
from src.core.sandbox import Sandbox  # noqa: E402
from src.core.tools import build_registry  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "training" / "agent_sft.jsonl"


# ---------------------------------------------------------------------------
# Seed material
#
# Clearly fictional equipment, matching the synthetic corpus. Kept here so the
# generator works before H5's corpus is merged, and so the dataset is
# reproducible from the repo alone.
# ---------------------------------------------------------------------------

EQUIPMENT = [
    ("E-4102", "shell and tube heat exchanger"),
    ("V-2207", "pressure vessel"),
    ("GB-114", "gearbox housing"),
    ("E-3301", "air cooler bundle"),
    ("V-1150", "knockout drum"),
    ("P-2204", "centrifugal pump casing"),
    ("T-0907", "distillation column shell"),
    ("D-1602", "separator vessel"),
]

THRESHOLD_QUERIES = [
    "wall thickness escalation threshold percent of nominal",
    "minimum remaining wall thickness before escalation",
    "ultrasonic thickness testing acceptance criteria",
    "hydrotest pressure and hold duration",
    "corrosion rate calculation method",
    "inspection interval for pressure vessels",
    "who must an inspection finding be escalated to",
    "confined space entry requirements",
]

REPORT_IDS = [f"INSP-2026-{n:04d}" for n in (198, 355, 412, 501, 620, 733)]


# ---------------------------------------------------------------------------
# Trajectory templates
#
# Each returns a list of (assistant_json, tool_observation) pairs ending with a
# final answer. Observations are written the way the real tools write them, so
# the model learns to read its own tools' output.
# ---------------------------------------------------------------------------


def _assistant(thought: str, tool: str | None, tool_input: dict | None = None, final: str | None = None) -> str:
    if tool is None:
        return json.dumps({"thought": thought, "tool": None, "final_answer": final}, ensure_ascii=False)
    return json.dumps({"thought": thought, "tool": tool, "tool_input": tool_input or {}}, ensure_ascii=False)


def _search_observation(source: str, page: int, snippet: str, extra: str = "") -> str:
    body = f"[1] {source}, page {page} (score 0.81):\n{snippet}"
    if extra:
        body += f"\n\n[2] {extra}"
    return body


def approval_note_trajectory(rng: random.Random) -> tuple[str, list[tuple[str, str]]]:
    """The headline flow: read a report, find the rule, compute, recommend."""
    tag, kind = rng.choice(EQUIPMENT)
    report = rng.choice(REPORT_IDS)
    nominal = rng.choice([10.0, 12.0, 14.0, 16.0, 18.0])
    ratio = rng.choice([0.62, 0.71, 0.758, 0.79, 0.83, 0.91])
    measured = round(nominal * ratio, 1)
    percent = round(measured / nominal * 100, 1)
    escalate = percent < 80.0

    task = (
        f"Read inspection report {report} for {kind} {tag}. Find the governing wall-thickness "
        "limit in the SOP corpus, calculate the remaining wall as a percentage of nominal, and "
        "draft an approval note stating whether it must be escalated. Cite the clause you used."
    )

    turns = [
        (
            _assistant(
                f"I need the report contents for {tag} before I can assess anything.",
                "read_file",
                {"path": f"{report}_{tag}.txt"},
            ),
            f"INSPECTION REPORT {report}\nEquipment: {tag} {kind}\nNominal wall thickness: "
            f"{nominal} mm\nMeasured minimum: {measured} mm\nVisual: localized pitting near the inlet nozzle",
        ),
        (
            _assistant(
                "I have the readings. Now I need the SOP threshold that decides whether this "
                "requires escalation - I must not assume a number.",
                "search_documents",
                {"query": "wall thickness escalation threshold percent of nominal"},
            ),
            _search_observation(
                "SOP-114_heat_exchanger_inspection.txt",
                3,
                "Wall thickness readings below 80 percent of nominal shall be escalated to the "
                "Inspection Engineer within one working day and recorded on Form IR-2.",
            ),
        ),
        (
            _assistant(
                "Confirm the percentage by calculation rather than asserting it, so the figure in "
                "the note is checkable.",
                "run_python",
                {
                    "code": f"nominal = {nominal}\nmeasured = {measured}\n"
                    "pct = measured / nominal * 100\n"
                    'print(f"remaining {pct:.1f}% of nominal, loss {nominal - measured:.1f} mm")'
                },
            ),
            f"exit code 0\nstdout:\nremaining {percent}% of nominal, loss {round(nominal - measured, 1)} mm",
        ),
        (
            _assistant(
                f"{percent}% is {'below' if escalate else 'above'} the 80% threshold in SOP-114, so "
                f"escalation is {'required' if escalate else 'not required'}. I have everything I need.",
                None,
                final=(
                    f"**Recommendation: {'ESCALATE' if escalate else 'ACCEPT - continue monitoring'}**\n\n"
                    f"Inspection report {report} records a minimum measured wall thickness of "
                    f"{measured} mm on {kind} {tag} against a nominal of {nominal} mm, which is "
                    f"**{percent} percent of nominal** (metal loss {round(nominal - measured, 1)} mm).\n\n"
                    f"SOP-114 (page 3) requires any reading below 80 percent of nominal to be escalated "
                    f"to the Inspection Engineer within one working day and recorded on Form IR-2. This "
                    f"reading is {abs(round(percent - 80.0, 1))} percentage points "
                    f"{'below' if escalate else 'above'} that threshold, so escalation is "
                    f"{'mandatory' if escalate else 'not triggered'}.\n\n"
                    "Localized pitting near the inlet nozzle is additionally noted and should be "
                    "recorded for the next inspection interval.\n\n"
                    "Source: SOP-114 Heat Exchanger Inspection, page 3."
                ),
            ),
            "",
        ),
    ]
    return task, turns


def lookup_trajectory(rng: random.Random) -> tuple[str, list[tuple[str, str]]]:
    """Pure retrieval with attribution - one tool call, then answer."""
    query = rng.choice(THRESHOLD_QUERIES)
    task = f"{query.capitalize()}? Cite the source document and page."
    turns = [
        (
            _assistant(
                "This is a procedural question, so the answer must come from the corpus rather "
                "than from memory.",
                "search_documents",
                {"query": query},
            ),
            _search_observation(
                "SOP-102_ultrasonic_thickness_testing.txt",
                2,
                "Thickness readings shall be taken at four points per grid location. Readings below "
                "80 percent of nominal are escalated to the Inspection Engineer within one working day.",
            ),
        ),
        (
            _assistant(
                "The corpus answers this directly and gives me a page to cite.",
                None,
                final=(
                    "Readings shall be taken at four points per grid location, and any reading below "
                    "80 percent of nominal is escalated to the Inspection Engineer within one working "
                    "day.\n\nSource: SOP-102 Ultrasonic Thickness Testing, page 2."
                ),
            ),
            "",
        ),
    ]
    return task, turns


def calculation_trajectory(rng: random.Random) -> tuple[str, list[tuple[str, str]]]:
    """Code path: compute in the sandbox, never in the model's head."""
    nominal = rng.choice([10.0, 12.0, 14.0, 16.0])
    measured = round(nominal * rng.uniform(0.6, 0.95), 1)
    years = rng.choice([2, 3, 4, 5])
    rate = round((nominal - measured) / years, 3)
    percent = round(measured / nominal * 100, 1)

    task = (
        f"Nominal wall thickness is {nominal} mm and the measured minimum is {measured} mm, "
        f"{years} years after the last inspection. Compute the remaining wall as a percentage of "
        "nominal and the average corrosion rate in mm per year."
    )
    turns = [
        (
            _assistant(
                "This is arithmetic, so I will run it rather than estimate it.",
                "run_python",
                {
                    "code": f"nominal, measured, years = {nominal}, {measured}, {years}\n"
                    "pct = measured / nominal * 100\nrate = (nominal - measured) / years\n"
                    'print(f"remaining: {pct:.1f}%")\nprint(f"corrosion rate: {rate:.3f} mm/yr")'
                },
            ),
            f"exit code 0\nstdout:\nremaining: {percent}%\ncorrosion rate: {rate:.3f} mm/yr",
        ),
        (
            _assistant(
                "The calculation is done and the numbers are confirmed.",
                None,
                final=(
                    f"Remaining wall is **{percent} percent of nominal** "
                    f"({measured} mm of {nominal} mm).\n\n"
                    f"Average corrosion rate over {years} years is **{rate:.3f} mm/yr** "
                    f"(({nominal} - {measured}) / {years}).\n\n"
                    "Both figures were computed in the sandbox rather than estimated."
                ),
            ),
            "",
        ),
    ]
    return task, turns


def recovery_trajectory(rng: random.Random) -> tuple[str, list[tuple[str, str]]]:
    """Teach recovery: a tool error is an observation to act on, not a dead end.

    Without examples like these a small model tends to either repeat the failing
    call or give up and hallucinate an answer. Both are demo-enders.
    """
    tag, kind = rng.choice(EQUIPMENT)
    report = rng.choice(REPORT_IDS)
    task = f"Summarise the findings in inspection report {report} for {tag}."
    turns = [
        (
            _assistant("I will read the report file directly.", "read_file", {"path": f"{report}.pdf"}),
            "ERROR: .pdf is not readable as text. PDFs and images must go through the ingestion "
            "pipeline, then be reached with search_documents.",
        ),
        (
            _assistant(
                "The PDF cannot be read directly. It has been ingested, so I should search the "
                "indexed corpus for it instead.",
                "search_documents",
                {"query": f"{report} {tag} findings"},
            ),
            _search_observation(
                f"{report}_{tag}_scan.txt",
                1,
                f"Inspection of {kind} {tag}: localized pitting at the inlet nozzle, minimum wall "
                "9.4 mm against 12.0 mm nominal, no through-wall defects observed.",
            ),
        ),
        (
            _assistant(
                "I have the findings from the indexed scan.",
                None,
                final=(
                    f"Inspection report {report} for {kind} {tag} records localized pitting at the "
                    "inlet nozzle and a minimum wall thickness of 9.4 mm against 12.0 mm nominal "
                    "(78.3 percent). No through-wall defects were observed.\n\n"
                    f"Source: {report} {tag} scan, page 1."
                ),
            ),
            "",
        ),
    ]
    return task, turns


def listing_trajectory(rng: random.Random) -> tuple[str, list[tuple[str, str]]]:
    """Teach list_files when the filename is unknown."""
    task = "Which inspection reports are available in the workbench?"
    sample = rng.sample(REPORT_IDS, 3)
    turns = [
        (
            _assistant(
                "I do not know the filenames, so I should list the directory rather than guess.",
                "list_files",
                {"directory": "."},
            ),
            "Contents of corpus/:\n" + "\n".join(f"{r}_scan.txt  (2841 bytes)" for r in sample),
        ),
        (
            _assistant(
                "The directory listing answers the question directly.",
                None,
                final="Three inspection reports are available: " + ", ".join(sample) + ".",
            ),
            "",
        ),
    ]
    return task, turns


GENERATORS = [
    (approval_note_trajectory, TaskType.DOCUMENT, 4),
    (lookup_trajectory, TaskType.DOCUMENT, 3),
    (calculation_trajectory, TaskType.CODE, 3),
    (recovery_trajectory, TaskType.DOCUMENT, 3),
    (listing_trajectory, TaskType.GENERAL, 1),
]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def build_examples(count: int, seed: int, tool_descriptions: str) -> Iterator[dict[str, Any]]:
    """Yield SFT records in trl's conversational format."""
    rng = random.Random(seed)

    weighted: list[Any] = []
    for generator, task_type, weight in GENERATORS:
        weighted.extend([(generator, task_type)] * weight)

    for _ in range(count):
        generator, task_type = rng.choice(weighted)
        task, turns = generator(rng)

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                # The live prompt, not an approximation of it. This is the point.
                "content": prompts.system_prompt(task_type, tool_descriptions),
            },
            {"role": "user", "content": task},
        ]

        for index, (assistant_json, observation) in enumerate(turns):
            messages.append({"role": "assistant", "content": assistant_json})
            if observation:
                tool_name = json.loads(assistant_json).get("tool")
                messages.append(
                    {"role": "user", "content": f"Tool result ({tool_name}):\n{observation}"}
                )
            elif index != len(turns) - 1:
                raise AssertionError("only the final turn may have no observation")

        yield {"messages": messages}


def validate(record: dict[str, Any]) -> None:
    """Fail loudly on a malformed record rather than training on it."""
    messages = record["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[-1]["role"] == "assistant", "an example must end on the model's turn"

    for message in messages:
        if message["role"] != "assistant":
            continue
        payload = json.loads(message["content"])  # every assistant turn must be valid JSON
        assert "thought" in payload
        if payload.get("tool") is None:
            assert payload.get("final_answer"), "a final turn needs an answer"
        else:
            assert isinstance(payload.get("tool_input"), dict)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=400, help="Number of examples (default 400).")
    parser.add_argument("--seed", type=int, default=20260912)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--stats", action="store_true", help="Print a summary and exit.")
    args = parser.parse_args()

    # The live registry, so the tool list in training matches the tool list the
    # agent actually offers at inference time.
    registry = build_registry(
        index=RagIndex(index_dir=ROOT / "data" / "index", backend="tfidf"),
        sandbox=Sandbox(workdir=ROOT / "data" / "sandbox"),
    )
    tool_descriptions = registry.describe_for_prompt()

    records = list(build_examples(args.n, args.seed, tool_descriptions))
    for record in records:
        validate(record)

    if args.stats:
        turn_counts = [sum(1 for m in r["messages"] if m["role"] == "assistant") for r in records]
        tools_used: dict[str, int] = {}
        for record in records:
            for message in record["messages"]:
                if message["role"] == "assistant":
                    name = json.loads(message["content"]).get("tool")
                    tools_used[name or "<final answer>"] = tools_used.get(name or "<final answer>", 0) + 1
        print(f"examples: {len(records)}")
        print(f"assistant turns: min {min(turn_counts)}, max {max(turn_counts)}")
        print("tool distribution:")
        for name, n in sorted(tools_used.items(), key=lambda kv: -kv[1]):
            print(f"  {name:<20} {n}")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    size_kb = args.out.stat().st_size / 1024
    print(f"Wrote {len(records)} examples to {args.out} ({size_kb:.0f} KB)")
    print("Tools in the training prompt:", ", ".join(registry.names()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
