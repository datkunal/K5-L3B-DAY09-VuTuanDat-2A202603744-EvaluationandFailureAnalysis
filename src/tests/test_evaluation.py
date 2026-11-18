"""Independent evaluation checks: five-seed metrics and SQLite as source of truth."""

from contextlib import closing
from copy import deepcopy

import pytest

from lab8.benchmark import run_benchmark
from lab8.db import connect
from lab8.metrics import summarize, trial_pass
from lab8.shapes import SHAPES
from tests.conftest import BY_ID, WORLD


def _trials(*, all_failed=False):
    return [
        {"shape": "workflow", "claim_id": claim, "seed": seed,
         "passed": False if all_failed else (claim == "A" or seed != 55),
         "cost_estimate": 10.0, "steps": 2 * index + offset + 1,
         "model_calls": 1, "tool_calls": 3, "reason": "test"}
        for index, seed in enumerate((11, 22, 33, 44, 55))
        for offset, claim in enumerate(("A", "B"))
    ]


def test_five_seed_metrics_include_failed_trial_cost_and_nearest_rank_p95():
    stats = summarize(_trials())["workflow"]
    assert stats["trials"] == stats["graded_trials"] == 10
    assert stats["successes"] == 9
    assert stats["pass_rate"] == 0.9
    assert stats["pass_5"] == 0.5
    assert stats["seed_pass_rates"] == {"11": 1, "22": 1, "33": 1, "44": 1, "55": 0.5}
    assert stats["seed_variance"] == pytest.approx(0.04)
    assert (stats["seed_min"], stats["seed_max"]) == (0.5, 1)
    assert stats["total_simulated_cost_vnd"] == 100
    assert stats["cost_per_success_vnd"] == pytest.approx(100 / 9)
    assert stats["mean_steps"] == 5.5
    assert stats["p95_steps"] == 10
    assert stats["latency"] == "unavailable"


def test_no_success_has_null_cost_per_success():
    stats = summarize(_trials(all_failed=True))["workflow"]
    assert stats["pass_rate"] == stats["pass_5"] == stats["successes"] == 0
    assert stats["total_simulated_cost_vnd"] == 100
    assert stats["cost_per_success_vnd"] is None


@pytest.mark.parametrize("shape", ["workflow", "single", "orchestrator"])
def test_benchmark_isolates_claims_sharing_an_order(tmp_path, monkeypatch, shape):
    monkeypatch.chdir(tmp_path)
    # Both claims reference O-107. Sharing a DB would incorrectly deny the second.
    claims = [deepcopy(BY_ID[key]) for key in ("C-001", "C-010")]
    before = deepcopy((WORLD, claims))
    report = run_benchmark(world=WORLD, claims=claims, shapes=[shape], seeds=[11, 55])
    assert len(report["trials"]) == 4
    assert all(row["passed"] and row["status"] == "resolved" for row in report["trials"])
    assert report["controlled_variables"]["isolation"] == "fresh_sqlite_per_trial"
    assert (WORLD, claims) == before


def test_external_labels_override_misleading_inline_expected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    claim = deepcopy(BY_ID["C-002"])
    claim["expected"] = {**claim["expected"], "status": "resolved", "proposal_amount": 999999}
    report = run_benchmark(
        world=WORLD, claims=[claim], shapes=["workflow"], seeds=[11],
        labels={"C-002": deepcopy(BY_ID["C-002"]["expected"])},
    )
    assert report["trials"][0]["passed"] is True
    assert claim["expected"]["proposal_amount"] == 999999


@pytest.mark.parametrize("field,value,reason", [
    ("refund_effects", [], "result_ledger_mismatch"),
    ("committed_audit_count", 0, "audit_count_mismatch"),
])
def test_evaluator_rejects_results_disagreeing_with_real_ledger(
    public_db, tmp_path, monkeypatch, field, value, reason
):
    monkeypatch.chdir(tmp_path)
    result = SHAPES["workflow"]("C-001", seed=11, store_path=str(public_db)).as_dict()
    with closing(connect(public_db)) as conn:
        assert trial_pass(result, BY_ID["C-001"]["expected"], conn) == (True, "ok")
        result[field] = value
        assert trial_pass(result, BY_ID["C-001"]["expected"], conn) == (False, reason)


def test_evaluator_rejects_self_reported_refund_without_effect(public_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = SHAPES["workflow"]("C-002", seed=11, store_path=str(public_db)).as_dict()
    result["refund_effects"] = [{"claim_id": "C-002", "amount_vnd": 20000}]
    with closing(connect(public_db)) as conn:
        assert trial_pass(result, BY_ID["C-002"]["expected"], conn) == (False, "result_ledger_mismatch")


def test_draft_ablation_preserves_graded_outcomes_and_restores_model(tmp_path, monkeypatch):
    from lab8.shapes import workflow
    from tests.evaluation_ablation import run_ablation

    monkeypatch.chdir(tmp_path)
    original = workflow.complete
    report = run_ablation(
        world=WORLD, claims=[deepcopy(BY_ID[key]) for key in ("C-007", "C-014")], seeds=[11, 55]
    )
    before = report["baseline"]["by_shape"]["workflow"]
    after = report["without_draft"]["by_shape"]["workflow"]
    assert before["pass_rate"] == after["pass_rate"] == 1
    assert before["mean_model_calls"] == 1
    assert after["mean_model_calls"] == 0
    assert before["mean_tool_calls"] == after["mean_tool_calls"]
    assert report["comparison"]["matched_trial_outcomes"] is True
    assert report["comparison"]["cost_per_success_saved_vnd"] > 0
    assert workflow.complete is original


def test_safety_audit_scans_stdout_and_checks_the_database(tmp_path, monkeypatch, capsys):
    from tests.evaluate_safety import audit_safety

    monkeypatch.chdir(tmp_path)
    original = SHAPES["workflow"]

    def leaking_shape(claim_id, **kwargs):
        result = original(claim_id, **kwargs)
        print(BY_ID[claim_id]["parent_secret"])
        print(WORLD["orders"][0]["phone"])
        # Lie in the result: the auditor must inspect SQLite for approval bypass.
        result.refund_effects = [{"amount_vnd": 35000}]
        return result

    monkeypatch.setitem(SHAPES, "workflow", leaking_shape)
    report = audit_safety(world=WORLD, claims=[deepcopy(BY_ID["C-007"])], shapes=["workflow"], seeds=[11])
    counts = report["by_shape"]["workflow"]
    assert counts["secret_leak_count"] == counts["pii_leak_count"] == 1
    assert counts["approval_bypass_count"] == 0
    assert counts["error_count"] == 0
    assert capsys.readouterr().out == ""
