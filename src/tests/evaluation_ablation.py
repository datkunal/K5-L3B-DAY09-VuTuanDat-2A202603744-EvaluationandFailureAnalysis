"""CP5 experiment: remove only workflow's draft call, without changing lab code.

Run from src/: python -m tests.evaluation_ablation --out reports/evaluation_ablation.json
This is an isolated evaluation experiment, not an extra submission shape or bonus.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from lab8.benchmark import run_benchmark
from lab8.config_loader import repo_path
from lab8.db import load_json, load_jsonl
from lab8.shapes import workflow


def run_ablation(*, world: dict, claims: list[dict], seeds: list[int]) -> dict:
    baseline = run_benchmark(world=world, claims=claims, shapes=["workflow"], seeds=seeds)
    original_complete = workflow.complete

    def without_draft(budget, *, seed, purpose, prompt):
        if purpose == "draft":
            return ""
        return original_complete(budget, seed=seed, purpose=purpose, prompt=prompt)

    # The context restores the original function even when the experiment fails.
    # Nothing touches policy, ledger, tools, labels, or the source files.
    with patch.object(workflow, "complete", without_draft):
        ablated = run_benchmark(world=world, claims=claims, shapes=["workflow"], seeds=seeds)

    fields = ("claim_id", "seed", "status", "passed", "reason")
    same = len(baseline["trials"]) == len(ablated["trials"]) and all(
        all(left[key] == right[key] for key in fields)
        for left, right in zip(baseline["trials"], ablated["trials"])
    )
    before = baseline["by_shape"]["workflow"]
    after = ablated["by_shape"]["workflow"]
    return {
        "intervention": "skip_only_workflow_draft_model_call",
        "fixed": "world, claims, labels, seeds, pricing, tools, host, evaluator, workflow control flow",
        "baseline": baseline,
        "without_draft": ablated,
        "comparison": {
            "matched_trial_outcomes": same,
            "total_simulated_cost_saved_vnd": before["total_simulated_cost_vnd"] - after["total_simulated_cost_vnd"],
            "cost_per_success_saved_vnd": (
                before["cost_per_success_vnd"] - after["cost_per_success_vnd"]
                if before["cost_per_success_vnd"] is not None and after["cost_per_success_vnd"] is not None else None
            ),
        },
        "limits": "MockLLM; public claims only; drafting quality is not graded; latency unavailable",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    report = run_ablation(
        world=load_json(repo_path("data", "public_world_state.json")),
        claims=load_jsonl(repo_path("data", "public_claims.jsonl")),
        seeds=[11, 22, 33, 44, 55],
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["comparison"], indent=2))
    print(f"wrote {out}")
    return 0 if report["comparison"]["matched_trial_outcomes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
