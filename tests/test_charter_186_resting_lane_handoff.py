"""Charter 186: a partly filled order still resting is the resting lane's alone.

A partial fill parks the intent at ``reconcile_required`` (the ledger's
rule), and two lanes then asked the exchange about it every cycle: the
resting lane, by the order's own exchange id (authoritative), and the
unresolved-intent lane, by idempotency key plus the open-order enumeration
and the position -- 9-15s a cycle for one KHOU GTD rung resting until its
expiry (2026-09-21).  The unresolved lane now leaves an intent the resting
lane has just read as resting; one it did not reach, could not read or saw
end comes back.
"""

from __future__ import annotations

import time

import clob_order_service_client
from bayes_basket_target import delta_v2_runtime
from bayes_basket_target import production_worker
from bayes_basket_target import resting_ladder_lifecycle
from tests.test_charter_110_lost_handle_recovery import _conn, _timed_out, NOW


class _CountingGateway:
    made = 0

    def __init__(self, *args, **kwargs) -> None:
        type(self).made += 1

    def _post(self, path, payload):
        return {"ok": False, "found": False, "pending": False}

    def lookup_open_orders(self, request):
        return {"ok": True, "complete": True, "total_open_orders": 0, "open_orders": []}

    def lookup_position(self, request):
        return {"ok": True, "shares": 0.0}


def _worker(monkeypatch):
    conn = _conn([{"order_intent_id": "khou-rung"}])
    _timed_out(conn, "khou-rung")
    worker = object.__new__(production_worker.TargetProductionWorker)
    worker.commit_conn = conn
    _CountingGateway.made = 0
    monkeypatch.setattr(clob_order_service_client, "ClobOrderServiceClient", _CountingGateway)
    return worker


def test_an_order_the_resting_lane_just_read_as_resting_is_left_to_it(monkeypatch) -> None:
    worker = _worker(monkeypatch)
    worker._resting_lane_live = {"khou-rung": time.monotonic()}
    receipt = worker._reconcile_unresolved_intents(as_of_utc=NOW)
    assert receipt["state"] == "resting_lane_owned"
    assert receipt["resting_lane_owned"] == ["khou-rung"]
    assert receipt["examined"] == 0
    assert _CountingGateway.made == 0


def test_a_stale_reading_hands_it_back(monkeypatch) -> None:
    worker = _worker(monkeypatch)
    worker._resting_lane_live = {
        "khou-rung": time.monotonic() - production_worker.RESTING_LANE_HANDOFF_MAX_AGE_SEC - 1
    }
    receipt = worker._reconcile_unresolved_intents(as_of_utc=NOW)
    assert receipt["examined"] == 1
    assert receipt["resting_lane_owned"] == []
    assert _CountingGateway.made == 1


def test_no_resting_lane_reading_is_the_lane_as_before(monkeypatch) -> None:
    worker = _worker(monkeypatch)
    receipt = worker._reconcile_unresolved_intents(as_of_utc=NOW)
    assert receipt["examined"] == 1


def test_the_resting_pass_records_only_what_it_read_as_resting(monkeypatch) -> None:
    worker = _worker(monkeypatch)
    monkeypatch.setattr(delta_v2_runtime, "EXECUTION_MODE", "maker")
    monkeypatch.setattr(delta_v2_runtime, "MAKER_LADDER_SHAPE", "bid_anchored")
    monkeypatch.setattr(production_worker, "_publish_resting_reprice_receipt", lambda *a, **k: None)
    rungs = [
        {"order_intent_id": "a", "sync": {"state": "resting"}},
        {"order_intent_id": "b", "sync": {"state": "resting_partial"}},
        {"order_intent_id": "c", "sync": {"state": "filled"}},
        {"order_intent_id": "d", "sync": {"state": "expired"}},
        {"order_intent_id": "e", "verdict": {"action": "deferred"}},
    ]
    monkeypatch.setattr(
        resting_ladder_lifecycle, "reprice_resting_rungs",
        lambda conn, **kw: {"schema": "x", "state": "no_change", "rungs": rungs},
    )
    worker._reprice_resting_rungs(as_of_utc=NOW)
    assert set(worker._resting_lane_live) == {"a", "b"}


# --- the republisher fallback's activation walk ---------------------------------------

def test_every_cross_activation_walk_streams_off_the_generation_index(tmp_path) -> None:
    import ast
    import sqlite3
    from pathlib import Path

    from bayes_basket_target.ssot import apply_production_ssot_schema

    root = Path(__file__).resolve().parents[1] / "bayes_basket_target"
    walks = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = " ".join(node.value.split())
                # The first-match walks.  The compile's full collection
                # (every compatible activation, no early return) keeps the
                # window index on purpose: in generation order it would read
                # a scope's expired generations too, and a scope can hold
                # over a thousand of those (CYYZ h24_48, charter 176).
                if (
                    "FROM calibration_native_ensemble_activation a" in sql
                    and "artifact_kind='CROSS_NATIVE_ENSEMBLE'" in sql
                    and "ORDER BY a.generation DESC" in sql
                    and "+a.valid_until_utc>?" in sql
                ):
                    walks.append((path.name, sql))
    assert sorted(name for name, _ in walks) == [
        "native_ensemble_p0.py", "republisher_fallback_activation.py",
    ]
    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    for name, sql in walks:
        plan = " | ".join(
            str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, ("x",) * sql.count("?"))
        )
        assert "calibration_native_activation_generation_idx" in plan, (name, plan)
        assert "TEMP B-TREE" not in plan, (name, plan)
