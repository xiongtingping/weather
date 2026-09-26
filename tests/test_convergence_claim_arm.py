"""Arm B (2026-09-06): the convergence claim lift, shipped in shadow.

A window whose served claim rose while the market's price fell, and which
the market still prices above the claim, realises 1.3-1.4x what it claims
(P0-M/P1, probability layer); the positive-EV gate refuses it because the
claim is still under the price.  The arm multiplies such a window's claim
ahead of the gate.  In shadow it journals what it would have admitted and
changes nothing; on, it is the ledger arm.  These tests pin the state, the
shadow's inertness, the arm's effect, and that every reader hands in the
same evidence.
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import delta_v2_runtime as R  # noqa: E402
from bayes_basket_target.basket_kernel import delta_v2 as K  # noqa: E402
from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot  # noqa: E402
from bayes_basket_target.convergence_evidence import (  # noqa: E402
    load_convergence_evidence,
    window_sum,
)


def _snapshot(rows, *, stage="P0-M"):
    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "convergence-fixture",
        "sequence": 1,
        "observed_at": "2026-09-05T12:00:00Z",
        "market_id": "convergence-market",
        "station_id": "SBGR",
        "event_date": "2026-09-05",
        "quote_version": "quote-v1",
        "quote_observed_at": "2026-09-05T12:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-v1",
        "order_state_version": "order-v1",
        "evidence": {
            "probability_stage": stage,
            "posterior_version": "p-v1",
            "posterior_sha256": "a" * 64,
        },
        "buckets": [
            {
                "bucket": bucket,
                "ordinal": index,
                "probability": probability,
                "bid_price": round(ask - 0.001, 4),
                "bid_levels": [{"price": round(ask - 0.001, 4), "shares": 50.0}],
                "ask_levels": [{"price": ask, "shares": 500.0}],
                "price_tick_size": 0.001,
                "maker_queue_ahead_shares": 0.0,
                "maker_queue_known": True,
            }
            for index, (bucket, probability, ask) in enumerate(rows)
        ],
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
        "time_left_minutes": 600.0,
    })


# Claim now / ask now: every three-rung window holding 28°C is dear
# (a >= 1.1) and refused by the edge gate today.
ROWS = (
    ("26°C", 0.05, 0.08),
    ("27°C", 0.20, 0.25),
    ("28°C", 0.30, 0.30),
    ("29°C", 0.20, 0.25),
    ("30°C", 0.25, 0.30),
)
# Twelve hours earlier the model claimed less and the market asked more.
EVIDENCE = {
    "schema": "convergence_evidence_v1",
    "horizon_hours": 12.0,
    "prior_stage": "P0-M",
    "prior_decision_at_utc": "2026-09-04T23:00:00.000Z",
    "prior_book_observed_at": "2026-09-04T23:05:00.000Z",
    "prior_probabilities": {"26°C": 0.05, "27°C": 0.15, "28°C": 0.22, "29°C": 0.15, "30°C": 0.20},
    "prior_asks": {"26°C": 0.05, "27°C": 0.30, "28°C": 0.35, "29°C": 0.30, "30°C": 0.30},
}


def _windows(*, stage="P0-M", evidence=EVIDENCE):
    snapshot = _snapshot(ROWS, stage=stage)
    return R._support_windows(
        snapshot=snapshot,
        desired={"26°C", "27°C", "28°C", "29°C", "30°C"},
        probabilities={r.bucket: float(r.probability) for r in snapshot.buckets},
        fee_rate=0.0,
        maximum_support_buckets=3,
        minimum_support_buckets=3,
        center_bucket="28°C",
        probability_stage=stage,
        convergence_evidence=evidence,
    )


def test_the_state_needs_both_sides_moving_and_the_window_still_dear():
    base = dict(stage="P1", claim_now=0.66, claim_prev=0.50, price_now=0.80, price_prev=0.95, agreement_now=1.21)
    assert K.convergence_state_v1(**base)["converging"] is True
    assert K.convergence_state_v1(**{**base, "stage": "P0"})["reason"] == "stage"
    assert K.convergence_state_v1(**{**base, "claim_prev": None})["reason"] == "no_prior"
    assert K.convergence_state_v1(**{**base, "agreement_now": 1.05})["reason"] == "not_dear"
    assert K.convergence_state_v1(**{**base, "claim_prev": 0.65})["reason"] == "claim_not_rising"
    # The market's move alone -- the measured losing cell -- never qualifies.
    assert K.convergence_state_v1(**{**base, "claim_prev": 0.66})["reason"] == "claim_not_rising"
    assert K.convergence_state_v1(**{**base, "price_prev": 0.83})["reason"] == "price_not_falling"
    assert K.convergence_claim_factor({"converging": True}) == pytest.approx(K.CONVERGENCE_CLAIM_FACTOR)
    assert K.convergence_claim_factor({"converging": False}) == 1.0
    assert K.CONVERGENCE_CLAIM_MODE == "shadow"
    assert K.CONVERGENCE_CLAIM_STAGES == frozenset({"P0-M", "P1"})


def test_shadow_journals_the_lift_and_changes_no_window(monkeypatch):
    monkeypatch.setattr(K, "CONVERGENCE_CLAIM_MODE", "shadow")
    windows, audit = _windows()
    bare, bare_audit = _windows(evidence=None)
    receipt = audit["convergence_claim"]
    assert receipt["mode"] == "shadow" and receipt["evidence_present"] is True
    assert receipt["prior_stage"] == "P0-M"
    # Charter 131 (2026-09-12): a symmetric day seats only the mean-centred
    # three-rung window, so of the three this fixture used to price, two are
    # refused by shape before the price gate and one is a candidate.  (The
    # fixture's inherited support is appended and refused too, which is why
    # the edge counter reads one more than the candidates.)
    assert audit["shape_refused_window_count"] == 2
    assert receipt["candidate_count"] == 1
    # Charter 130 (2026-09-12): the shadow asks the gate's own question, and
    # the gate's pass mark is 5% of the cost.  The lifted edges here are
    # positive but thinner than that, so nothing would be admitted under
    # the served hurdle -- and everything would at the old pass mark of
    # zero, which is asserted below by patching the constant the gate reads.
    # Charter 230: the counts are whatever the one belief makes them; what the
    # shadow must never do is admit by its lift.
    assert receipt["would_admit_count"] == sum(
        bool(row["flips_gate"]) for row in receipt["candidates"]
    )
    assert receipt["admitted_by_lift_count"] == 0
    # At a pass mark of zero the shadow's question is the old one, edge
    # sign: asserted as an equivalence per candidate so it does not depend
    # on how large the lift happens to be in this fixture.
    monkeypatch.setattr(K, "ECONOMIC_HURDLE_V1", 0.0)
    _, audit_zero = _windows()
    for row in audit_zero["convergence_claim"]["candidates"]:
        assert row["flips_gate"] == (row["unlifted_edge"] <= 0 < row["lifted_edge"])
    monkeypatch.setattr(K, "ECONOMIC_HURDLE_V1", 0.05)
    # Every window reads exactly what it reads without the evidence: the
    # refused candidates are dropped in both runs, and what survives (the
    # inherited support) is byte-for-byte the same seatable list.
    assert [tuple(w["buckets"]) for w in windows] == [tuple(w["buckets"]) for w in bare]
    for row, twin in zip(bare, windows):
        assert twin["win_probability"] == pytest.approx(row["win_probability"])
        assert twin["edge"] == pytest.approx(row["edge"])
        assert twin["admissible"] == row["admissible"]
        assert twin["convergence_claim_factor"] == 1.0
    # The lift only ever raises the edge it journals.
    for candidate in receipt["candidates"]:
        assert candidate["unlifted_edge"] < candidate["lifted_edge"]
    assert bare_audit["convergence_claim"]["evidence_present"] is False
    assert audit["edge_refused_window_count"] == bare_audit["edge_refused_window_count"]


# Charter 230 retired the lambda / second-belief arm this test pinned.




def test_p0_and_off_and_no_evidence_leave_every_window_alone(monkeypatch):
    monkeypatch.setattr(K, "CONVERGENCE_CLAIM_MODE", "on")
    windows, audit = _windows(stage="P0")
    assert audit["convergence_claim"]["candidate_count"] == 0
    assert all(
        w.get("convergence") is None or w["convergence"]["reason"] == "stage"
        for w in windows
    )
    assert all(w["convergence_claim_factor"] == 1.0 for w in windows)
    monkeypatch.setattr(K, "CONVERGENCE_CLAIM_MODE", "off")
    windows, audit = _windows()
    assert audit["convergence_claim"]["mode"] == "off"
    assert all(w.get("convergence") is None for w in windows)
    assert audit["edge_refused_window_count"] > 0


def test_the_loader_pairs_the_earlier_checkpoint_with_the_book_it_compiled_on(tmp_path):
    conn = sqlite3.connect(tmp_path / "e.db", isolation_level=None)
    conn.execute("CREATE TABLE basket_probability_bridge_history (history_id, station_id, event_date, stage, decision_at_utc, state, created_at_utc)")
    conn.execute("CREATE TABLE basket_probability_bridge_bucket_probability (history_id, bucket, probability)")
    conn.execute("CREATE TABLE basket_official_ws_snapshot (station_id, event_date, observed_at, state, buckets_json)")
    conn.execute("INSERT INTO basket_probability_bridge_history VALUES('h1','SBGR','2026-09-05','P0-M','2026-09-04T22:00:00.000Z','COMMITTED','2026-09-04T22:00:00.000Z')")
    conn.execute("INSERT INTO basket_probability_bridge_history VALUES('h2','SBGR','2026-09-05','P1','2026-09-05T06:00:00.000Z','COMMITTED','2026-09-05T06:00:00.000Z')")
    for bucket, p in (("27°C", 0.15), ("28°C", 0.20), ("29°C", 0.15)):
        conn.execute("INSERT INTO basket_probability_bridge_bucket_probability VALUES('h1',?,?)", (bucket, p))
        conn.execute("INSERT INTO basket_probability_bridge_bucket_probability VALUES('h2',?,?)", (bucket, p + 0.05))
    book = '[{"bucket":"27°C","ask_levels":[{"price":0.30,"shares":10}]},{"bucket":"28°C","ask_levels":[{"price":0.35,"shares":10}]},{"bucket":"29°C","ask_levels":[{"price":0.30,"shares":10}]}]'
    conn.execute("INSERT INTO basket_official_ws_snapshot VALUES('SBGR','2026-09-05','2026-09-04T21:00:00.000Z','COMMITTED','[]')")
    conn.execute("INSERT INTO basket_official_ws_snapshot VALUES('SBGR','2026-09-05','2026-09-04T22:04:00.000Z','COMMITTED',?)", (book,))
    conn.execute("INSERT INTO basket_official_ws_snapshot VALUES('SBGR','2026-09-05','2026-09-05T11:00:00.000Z','COMMITTED','[]')")
    evidence = load_convergence_evidence(
        conn, station_id="SBGR", event_date="2026-09-05",
        as_of_utc="2026-09-05T12:00:00.000Z", horizon_hours=12.0,
    )
    # 12h before noon is midnight: h1 (22:00) is the latest at or before it,
    # h2 (06:00) is inside the horizon and must not be chosen.
    assert evidence["prior_decision_at_utc"] == "2026-09-04T22:00:00.000Z"
    assert evidence["prior_stage"] == "P0-M"
    assert evidence["prior_book_observed_at"] == "2026-09-04T22:04:00.000Z"
    assert window_sum(evidence["prior_probabilities"], ["27°C", "28°C", "29°C"]) == pytest.approx(0.50)
    assert window_sum(evidence["prior_asks"], ["27°C", "28°C", "29°C"]) == pytest.approx(0.95)
    assert window_sum(evidence["prior_asks"], ["27°C", "31°C"]) is None
    # An opening seat: nothing before it.
    assert load_convergence_evidence(
        conn, station_id="SBGR", event_date="2026-09-05",
        as_of_utc="2026-09-04T22:30:00.000Z", horizon_hours=12.0,
    ) is None
    conn.close()


def test_every_reader_hands_in_the_same_evidence():
    for module in ("production_io.py", "p0_preselection.py"):
        text = (ROOT / "bayes_basket_target" / module).read_text()
        assert "convergence_evidence=load_convergence_evidence(" in text, module
    runtime = (ROOT / "bayes_basket_target" / "runtime.py").read_text()
    assert runtime.count("convergence_evidence=convergence_evidence,") == 3
    production = (ROOT / "bayes_basket_target" / "production.py").read_text()
    assert '"convergence_claim": (' in production
