"""Ledger integration evidence for Vu Van Chu: verify durable state, not logs.

Expected amounts are taken from the business rules, never from quote().
"""

from contextlib import closing
import json
import sqlite3
import time

import pytest

from lab8.db import connect
from lab8.durability import InjectedCrash, committed_audit_count, get_refund, issue_refund
from lab8.metrics import artifact_blob, trial_pass, unexpected_ledger_rows
from lab8.shapes import SHAPES
from tests.conftest import BY_ID

STUDENT_SHAPES = ("workflow", "single", "orchestrator")
CRASH_POINTS = ("before_effect", "after_effect_before_checkpoint", "after_checkpoint")


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
@pytest.mark.parametrize("point", CRASH_POINTS)
@pytest.mark.parametrize("claim_id,amount", [("C-008", 20000), ("C-014", 120000)])
def test_capped_and_topup_refunds_survive_every_crash(
    public_db, tmp_path, monkeypatch, capsys, shape, point, claim_id, amount
):
    """Reconnect after a real injected crash; replay twice and check the full contract."""
    monkeypatch.chdir(tmp_path)
    run = SHAPES[shape]
    with pytest.raises(InjectedCrash, match=point):
        run(claim_id, seed=11, store_path=str(public_db), crash_at=point)

    with closing(connect(public_db)) as conn:
        effect = get_refund(conn, claim_id)
        committed = point != "before_effect"
        assert (effect is not None) == committed
        assert committed_audit_count(conn, claim_id) == int(committed)
        if effect:
            assert effect["amount_vnd"] == amount
        checkpoint = conn.execute(
            "SELECT result_json FROM checkpoints WHERE claim_id = ? AND step = 'issue_refund'",
            (claim_id,),
        ).fetchone()
        assert (checkpoint is not None) == (point == "after_checkpoint")

    for _ in range(2):
        started = time.time()
        result = run(claim_id, seed=11, store_path=str(public_db))
        with closing(connect(public_db)) as conn:
            assert trial_pass(result.as_dict(), BY_ID[claim_id]["expected"], conn, started_at=started) == (True, "ok")
            rows = conn.execute("SELECT * FROM refund_ledger WHERE claim_id = ?", (claim_id,)).fetchall()
            assert len(rows) == 1
            assert rows[0]["amount_vnd"] == amount
            assert rows[0]["idempotency_key"] == f"refund:{claim_id}"
            assert committed_audit_count(conn, claim_id) == 1
            assert unexpected_ledger_rows(conn, claim_id) == 0
            saved = conn.execute(
                "SELECT result_json FROM checkpoints WHERE claim_id = ? AND step = 'issue_refund'", (claim_id,)
            ).fetchone()
            assert json.loads(saved["result_json"])["amount_vnd"] == amount
        blob = artifact_blob(result.as_dict())
        assert all(token not in blob for token in BY_ID[claim_id]["expected"]["forbid_substrings"])
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_audit_insert_failure_rolls_back_refund_before_reconnect(public_db):
    """An effect must not survive when its committed audit cannot be inserted."""
    with closing(connect(public_db)) as conn:
        conn.execute(
            """CREATE TRIGGER fail_committed_audit BEFORE INSERT ON audit_events
               WHEN NEW.claim_id = 'C-001' AND NEW.event_type = 'refund_committed'
               BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"""
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="injected audit failure"):
            issue_refund(conn, "C-001", 35000)

    with closing(connect(public_db)) as conn:
        assert get_refund(conn, "C-001") is None
        assert committed_audit_count(conn, "C-001") == 0
        assert conn.execute("SELECT 1 FROM checkpoints WHERE claim_id = 'C-001'").fetchone() is None
        conn.execute("DROP TRIGGER fail_committed_audit")
        conn.commit()
        assert issue_refund(conn, "C-001", 35000)["reason"] == "committed"
        assert issue_refund(conn, "C-001", 35000)["reason"] == "idempotent_replay"
        assert get_refund(conn, "C-001")["amount_vnd"] == 35000
        assert committed_audit_count(conn, "C-001") == 1


def test_checkpoint_without_effect_does_not_skip_payment(public_db):
    with closing(connect(public_db)) as conn:
        conn.execute(
            "INSERT INTO checkpoints VALUES (?, 'issue_refund', ?)",
            ("C-001", json.dumps({"claim_id": "C-001", "amount_vnd": 35000, "status": "committed"})),
        )
        conn.commit()
        assert issue_refund(conn, "C-001", 35000)["reason"] == "committed"
        assert get_refund(conn, "C-001")["amount_vnd"] == 35000
        assert committed_audit_count(conn, "C-001") == 1


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
def test_changed_quote_on_retry_reports_conflict_and_preserves_money(public_db, tmp_path, monkeypatch, shape):
    monkeypatch.chdir(tmp_path)
    run = SHAPES[shape]
    assert run("C-001", seed=11, store_path=str(public_db)).status == "resolved"
    with closing(connect(public_db)) as conn:
        conn.execute("UPDATE orders SET shipping_fee_vnd = 10000 WHERE order_id = 'O-107'")
        conn.commit()
    for _ in range(2):
        result = run("C-001", seed=11, store_path=str(public_db))
        assert result.status == "failed"
        assert result.error_reason == "amount_conflict"
        assert result.proposal_amount == 10000
        assert result.refund_effects[0]["amount_vnd"] == 35000
        assert result.committed_audit_count == 1
    with closing(connect(public_db)) as conn:
        assert get_refund(conn, "C-001")["amount_vnd"] == 35000
        assert committed_audit_count(conn, "C-001") == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE claim_id = 'C-001' AND event_type = 'amount_conflict'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
@pytest.mark.parametrize("approval_state,reason", [
    ("missing", "missing_approval"),
    ("expired", "approval_expired"),
    ("revoked", "approval_revoked"),
    ("borrowed", "approval_claim_mismatch"),
])
def test_repeated_denial_never_creates_effect_or_checkpoint(
    public_db, tmp_path, monkeypatch, shape, approval_state, reason
):
    monkeypatch.chdir(tmp_path)
    with closing(connect(public_db)) as conn:
        if approval_state == "missing":
            conn.execute("UPDATE claims SET approval_id = NULL WHERE claim_id = 'C-001'")
        elif approval_state == "borrowed":
            conn.execute("UPDATE claims SET approval_id = 'A-008' WHERE claim_id = 'C-001'")
        elif approval_state == "expired":
            conn.execute("UPDATE approvals SET expires_at = '2020-01-01' WHERE approval_id = 'A-001'")
        else:
            conn.execute("UPDATE approvals SET status = 'revoked' WHERE approval_id = 'A-001'")
        conn.commit()
    for _ in range(2):
        result = SHAPES[shape]("C-001", seed=11, store_path=str(public_db))
        assert (result.status, result.proposal_amount, result.error_reason) == ("denied", 35000, reason)
        assert result.refund_effects == []
        assert result.committed_audit_count == 0
    with closing(connect(public_db)) as conn:
        assert get_refund(conn, "C-001") is None
        assert committed_audit_count(conn, "C-001") == 0
        assert conn.execute("SELECT 1 FROM checkpoints WHERE claim_id = 'C-001'").fetchone() is None
        denials = conn.execute(
            "SELECT details_json FROM audit_events WHERE claim_id = 'C-001' AND event_type = 'policy_denied'"
        ).fetchall()
        assert [json.loads(row[0])["reason"] for row in denials] == [reason]
