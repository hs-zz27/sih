"""End-to-end smoke test against a live local model.

Every other test in this repo scripts the LLM, which proves the loop's mechanics
but says nothing about whether a real model actually speaks our protocol. This
runs the real demo tasks against the real model server and reports what actually
happened:

  * did each turn parse as a decision object, or did we lose turns to bad JSON
  * which tools it chose
  * how long a run takes, which is the number that decides whether the live
    trace is watchable or painful
  * whether the answer carried citations

Run it after `scripts/preflight.py` says READY:

    python scripts/smoke_e2e.py
    python scripts/smoke_e2e.py --preset sop_lookup --repeat 3

Exit code is 0 only if every run completed.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.contracts import StepStatus, TaskRequest, TaskStatus  # noqa: E402
from src.core import demo  # noqa: E402
from src.core.orchestrator import Orchestrator  # noqa: E402


def run_one(orchestrator: Orchestrator, task: str, hint=None, verbose: bool = True):
    started = time.perf_counter()
    result = orchestrator.run(TaskRequest(task=task, task_type_hint=hint))
    elapsed = time.perf_counter() - started

    if verbose:
        print(f"  routed  : {result.task_type.value} -> {result.model_used}")
        print(f"  reason  : {result.routing_reason[:100]}")
        print(f"  status  : {result.status.value} in {elapsed:.1f}s ({len(result.steps)} steps)")

        for step in result.steps:
            mark = "ok " if step.status is StepStatus.OK else "ERR"
            tool = step.tool_name or "(final answer)"
            print(f"    [{mark}] {step.step_number}. {tool:<22} {step.duration_ms/1000:>5.1f}s")
            if step.status is StepStatus.ERROR and step.tool_output:
                print(f"          {step.tool_output.splitlines()[0][:100]}")

        if result.sources:
            print(f"  sources : {len(result.sources)} cited")
            for source in result.sources[:3]:
                print(f"    - {Path(source.source_path).name} p{source.page}")
        else:
            print("  sources : NONE - a document answer with no citation is a problem")

        if result.deliverables:
            print(f"  files   : {', '.join(d.filename for d in result.deliverables)}")

        print(f"  answer  : {' '.join(result.final_text.split())[:220]}")

    return result, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", help="Run one preset by id instead of all.")
    parser.add_argument("--task", help="Run an arbitrary task string.")
    parser.add_argument("--repeat", type=int, default=1, help="Runs per task (default 1).")
    args = parser.parse_args()

    orchestrator = Orchestrator()
    health = orchestrator.health()

    print("\nSUTRA end-to-end smoke test\n" + "=" * 70)
    print(f"endpoint : {health['inference_endpoint']}")
    print(f"available: {health['inference_available']}")
    print(f"routing  : {health['routing_table']}")
    print(f"index    : {health['index']['chunks']} chunks, embedder={health['index']['embedder']}")

    if not health["inference_available"]:
        print("\nModel server unreachable. Start it:  ollama serve")
        return 1
    if health.get("missing_models"):
        print(f"\nModels not pulled: {', '.join(health['missing_models'])}")
        print("Pull them first - a missing model looks exactly like a broken agent.")
        return 1
    if health["index"]["chunks"] == 0:
        print("\nIndex is empty; retrieval will find nothing. Index the corpus first.")
        return 1

    if args.task:
        jobs = [("custom", args.task, None)]
    elif args.preset:
        item = demo.preset(args.preset)
        if item is None:
            print(f"No such preset: {args.preset}")
            return 1
        jobs = [(item.id, item.task, item.task_type_hint)]
    else:
        jobs = [(p.id, p.task, p.task_type_hint) for p in demo.presets()]

    timings: list[float] = []
    outcomes: list[TaskStatus] = []
    parse_failures = 0
    total_steps = 0

    for name, task, hint in jobs:
        for attempt in range(args.repeat):
            label = f"{name}" + (f" (run {attempt + 1})" if args.repeat > 1 else "")
            print(f"\n{'-' * 70}\n{label}\n{'-' * 70}")
            result, elapsed = run_one(orchestrator, task, hint)

            timings.append(elapsed)
            outcomes.append(result.status)
            total_steps += len(result.steps)
            parse_failures += sum(
                1 for s in result.steps if s.metadata.get("stage") == "parse"
            )

    print("\n" + "=" * 70)
    completed = sum(1 for s in outcomes if s is TaskStatus.COMPLETED)
    print(f"runs        : {completed}/{len(outcomes)} completed")
    print(f"duration    : median {statistics.median(timings):.1f}s, max {max(timings):.1f}s")

    if total_steps:
        rate = 1 - (parse_failures / total_steps)
        print(f"parse rate  : {rate:.0%} ({parse_failures} malformed of {total_steps} turns)")
        if rate < 0.9:
            print("              ^ low. Tune src/core/prompts.py, or fine-tune with")
            print("                notebooks/train_agent_lora.ipynb - this is what it is for.")

    if max(timings) > 90:
        print("\nA run over ~90s is a long silence on stage. Consider the 4B for every")
        print("task type, or trim agent.max_steps.")

    if completed != len(outcomes):
        print("\nNot every run completed. Fix that before rehearsing.")
        return 1

    print("\nAll runs completed. Now do it again with the Wi-Fi off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
