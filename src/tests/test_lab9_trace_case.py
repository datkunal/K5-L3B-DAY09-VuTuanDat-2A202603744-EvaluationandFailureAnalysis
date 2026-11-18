"""Regression test from docs/trace_case.md: seeded_regression C-002@11.

First wrong step = event 3 `model_override` (ticket says 5 days late, DB says 2), which turns a
no-entitlement claim into a 20.000đ refund. The fixed shape must keep the DB fact and deny.
Run: python -m pytest -q -m student -o addopts= -k trace_case
"""
import pytest

pytestmark = pytest.mark.student


def _ctx(trial, label):
    from pydantic_evals.evaluators import EvaluatorContext

    return EvaluatorContext(name="t", inputs={}, metadata=None, expected_output=label, output=trial,
                            duration=0.0, _span_tree=None, attributes={}, metrics={})


@pytest.fixture(scope="module")
def cases():
    from lab9.trace_case import investigate

    return {shape: investigate(shape, "C-002", 11) for shape in ("seeded_regression", "fixed")}


def test_first_wrong_step_is_db_override(cases):
    events = cases["seeded_regression"]["trial"]["events"]
    kinds = [e.get("kind") for e in events]
    first = kinds.index("model_override")
    assert first == 2                                           # event #3 in trace_case output
    assert kinds[:first] == ["handoff", "investigator"]       # steps 1-2 read the DB correctly
    db_delay = events[1]["payload"]["lookup_order"]["order"]["delay_days"]
    assert db_delay == 2 and events[first]["payload"]["delay_days"] == 5


def test_graders_catch_it_on_seeded_and_pass_on_fixed(cases):
    from lab9.graders import DbFactKept, RefundAmountCorrect

    bad, good = cases["seeded_regression"], cases["fixed"]
    assert DbFactKept().evaluate(_ctx(bad["trial"], bad["label"])).value is False
    assert RefundAmountCorrect().evaluate(_ctx(bad["trial"], bad["label"])).value is False   # ledger 20000 vs 0
    assert DbFactKept().evaluate(_ctx(good["trial"], good["label"])).value is True
    assert RefundAmountCorrect().evaluate(_ctx(good["trial"], good["label"])).value is True
    assert good["trial"]["result"]["status"] == "denied"
