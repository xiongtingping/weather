"""Charter 233: the beta, valuation and convergence shadow lanes are retired.

User 2026-09-27 「退役影子」.  Each lane was a shadow no rule read: the basket
compile computed it on every priced window and the nightly sweep refitted it.
Pinned here: the lanes' code and refits are gone; the market's belief they
were first built for is still recorded on every priced window, because the
candidate-window evidence, the acceptance class and the charter EV page read
it; and the claim-transform key the convergence constants were part of did
not move, since the lift was a shadow and no claim ever went through it.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _snapshot():
    from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot

    labels = ["29°C", "30°C", "31°C", "32°C", "33°C", "34°C"]
    probs = [0.06, 0.20, 0.30, 0.20, 0.16, 0.08]
    asks = [0.05, 0.15, 0.30, 0.15, 0.10, 0.05]
    rows = [
        {
            "bucket": b, "ordinal": i + 2, "probability": p,
            "bid_price": max(0.0, a - 0.01),
            "bid_levels": [{"price": max(0.001, a - 0.01), "shares": 50.0}],
            "ask_levels": [{"price": a, "shares": 100.0}],
            "price_tick_size": 0.01,
            "maker_queue_ahead_shares": 0.0, "maker_queue_known": True,
        }
        for i, (b, p, a) in enumerate(zip(labels, probs, asks))
    ]
    snapshot = TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6", "snapshot_id": "c233", "sequence": 1,
        "observed_at": "2026-09-12T00:00:00Z", "market_id": "m", "station_id": "X",
        "event_date": "2026-09-13", "quote_version": "q",
        "quote_observed_at": "2026-09-12T00:00:00Z", "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "i", "order_state_version": "o",
        "evidence": {"probability_stage": "P0", "posterior_version": "p", "posterior_sha256": "a" * 64},
        "buckets": rows, "checkpoint_changed": True, "quote_changed": True,
        "fill_changed": True, "time_gate_changed": True, "evidence_fresh": True,
        "contract_matches": True, "order_state_known": True, "risk_inputs_complete": True,
        "time_left_minutes": 600.0,
    })
    return snapshot, dict(zip(labels, probs))


def test_every_priced_window_still_records_the_markets_belief() -> None:
    from bayes_basket_target.basket_kernel.valuation_v1 import (
        market_reference_v1,
        window_mass,
    )
    from bayes_basket_target.delta_v2_runtime import _support_windows

    snapshot, probabilities = _snapshot()
    windows, audit = _support_windows(
        snapshot=snapshot, desired={"30°C", "31°C", "32°C"},
        probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=4, minimum_support_buckets=3,
        center_bucket="31°C", probability_stage="P0", hours_to_cutoff=30.0,
    )
    market = market_reference_v1(snapshot.buckets)
    assert market["state"] == "complete"
    priced = [w for w in windows if w["all_in_price"] is not None]
    assert priced
    for w in priced:
        assert w["market_mid"] == pytest.approx(window_mass(market["vector"], w["buckets"]))
        assert w["hours_to_cutoff"] == pytest.approx(30.0)
    retired = ("beta_", "valuation_", "convergence")
    for w in windows:
        assert not [key for key in w if key.startswith(retired)], sorted(w)
    assert not [key for key in audit if key.startswith(retired)], sorted(audit)


def test_the_evidence_copies_the_market_mid_from_the_window() -> None:
    from bayes_basket_target.candidate_window_evidence import candidate_windows_v1

    snapshot, probabilities = _snapshot()
    from bayes_basket_target.delta_v2_runtime import _support_windows

    windows, _audit = _support_windows(
        snapshot=snapshot, desired={"30°C", "31°C", "32°C"},
        probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=4, minimum_support_buckets=3,
        center_bucket="31°C", probability_stage="P0", hours_to_cutoff=30.0,
    )
    rows = candidate_windows_v1(windows, seated_buckets=None)
    assert rows and all(row["market_mid"] is not None for row in rows)
    assert all(row["hours_to_cutoff"] == pytest.approx(30.0) for row in rows)


def test_the_claim_transform_key_did_not_move() -> None:
    """The convergence constants were hashed into the claim-transform key;
    the lift was a shadow, so their values stay as literals and no row's
    key moves for a change nothing went through."""

    from bayes_basket_target.basket_kernel.delta_v2 import claim_transform_fingerprint

    assert claim_transform_fingerprint(holds_centre=True) == (
        "56d8d5cca7b71ad1719b0bbc37dc8d1294f414e5a603e8a587d4fd529ea28e4b"
    )
    assert claim_transform_fingerprint(holds_centre=False) == (
        "8e9e2196d7b953ca176f22462de021035c90b4f3daa6685397b77b8967001584"
    )


def test_the_lanes_code_and_refits_are_gone() -> None:
    from bayes_basket_target import served_tables
    from bayes_basket_target.basket_kernel import delta_v2, valuation_v1

    for module in ("beta_edge_store", "convergence_evidence"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"bayes_basket_target.{module}")
    for name in ("BETA_TABLE_KEY", "BETA_EDGE_MODE", "beta_belief_v1",
                 "beta_conditional_edge_v3", "CONVERGENCE_CLAIM_MODE",
                 "convergence_state_v1"):
        assert not hasattr(delta_v2, name), name
    for name in ("VALUATION_MODE", "VALUATION_TABLE_KEY", "valuation_vector_v1",
                 "capacity_v1", "beta_draw_vectors"):
        assert not hasattr(valuation_v1, name), name
    # What the L4 lane and the market-priced lanes still read.
    assert callable(delta_v2.beta_lead_band)
    assert callable(valuation_v1.market_reference_v1)
    assert callable(valuation_v1.action_value_v1)
    source = Path(served_tables.__file__).read_text(encoding="utf-8")
    assert "beta" not in source.split('"""', 2)[2]
    sweep = (ROOT / "scripts" / "calibration_refit_sweep.sh").read_text(encoding="utf-8")
    assert "refit_beta_edge.py" not in sweep and "refit_valuation.py" not in sweep
    for script in ("refit_beta_edge.py", "refit_valuation.py"):
        assert not (ROOT / "scripts" / script).exists(), script


def test_the_charter_records_the_ruling() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter.split("\n233. ", 1)[1]
    assert "退役影子" in clause and "占号" not in clause.split("\n", 1)[0]
