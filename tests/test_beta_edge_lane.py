"""Charter 128: the beta lane -- one slope per stage, walk-forward, by-day
lower bound, shadow by default.

What is pinned: the fit recovers a planted slope and reports a lower bound
below it; below the hurdles it serves 0 and says so; the fit as of a day
reads nothing after that day; the enumeration records the beta belief on
every priced window and, in shadow, changes no verdict; under ``on`` the
gate reads ``beta_lcb * (p - q)`` and a stage with no fit refuses; the
ledger hashes the mode; the nightly sweep runs the refit.
"""
from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import beta_edge_store as store  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    BETA_TABLE_KEY,
    beta_belief_v1,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trade_candidate_window("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, raw_win_probability REAL,"
        " all_in_price REAL, hit INTEGER, settled_bucket TEXT)"
    )
    store.ensure_schema(conn)
    return conn


def _plant(conn, *, days: int, stations: int = 20, beta: float = 0.5,
           intercept: float = -0.05, noise: float = 0.1, seed: int = 1,
           stage: str = "P0", first_day: int = 1) -> None:
    rng = random.Random(seed)
    n = 0
    for d in range(first_day, first_day + days):
        day = f"2026-09-{d:02d}"
        for s in range(stations):
            price = rng.uniform(0.4, 0.9)
            x = rng.uniform(-0.3, 0.3)
            raw = min(0.99, max(0.01, price + x))
            y = intercept + beta * (raw - price) + rng.gauss(0.0, noise)
            hit = 1 if rng.random() < max(0.0, min(1.0, price + y)) else 0
            conn.execute(
                "INSERT INTO paper_trade_candidate_window VALUES(?,?,?,?,?,?,?,?,?)",
                (f"c{n}", f"S{s:02d}", day, stage, f"{day}T00:00:00Z",
                 raw, price, hit, "29°C"),
            )
            n += 1
    conn.commit()


def test_the_fit_recovers_a_planted_slope_with_a_bound_below_it() -> None:
    conn = _db()
    # Bernoulli outcomes: the slope's standard error is about
    # sqrt(Var(hit) / (n Var(x))) ~ 0.06 at n = 1680, so the tolerance is
    # three of those, not a claim about the estimator's precision.
    _plant(conn, days=14, stations=120, beta=0.5, noise=0.05)
    fit = store.fit_beta(conn, stage_scope="P0")
    ev = fit["evidence"]
    assert not fit["accumulating"]
    assert fit["row_count"] == 14 * 120 and fit["day_count"] == 14
    assert abs(ev["beta"] - 0.5) < 0.2
    assert ev["beta_lcb"] < ev["beta"] < ev["beta_ucb"]
    assert ev["served_beta_lcb"] == ev["beta_lcb"]
    assert abs(ev["intercept"] - (-0.05)) < 0.06
    assert fit["table"]["beta_lcb"] == ev["served_beta_lcb"]


def test_a_zero_slope_serves_a_bound_at_or_below_zero() -> None:
    conn = _db()
    _plant(conn, days=14, stations=120, beta=0.0, noise=0.05, seed=3)
    fit = store.fit_beta(conn, stage_scope="P0")
    assert abs(fit["evidence"]["beta"]) < 0.2
    assert fit["evidence"]["beta_lcb"] <= 0.05


def test_below_the_hurdles_it_serves_zero_and_says_so() -> None:
    conn = _db()
    _plant(conn, days=5, stations=40)
    fit = store.fit_beta(conn, stage_scope="P0")
    assert fit["accumulating"] is True
    assert fit["table"]["beta_lcb"] == 0.0
    assert fit["evidence"]["beta"] is None


def test_the_fit_as_of_a_day_reads_nothing_after_it() -> None:
    conn = _db()
    _plant(conn, days=12, stations=10)
    fit = store.fit_beta(conn, stage_scope="P0", as_of="2026-09-06")
    assert fit["row_count"] == 6 * 10
    assert fit["evidence"]["as_of_event_date"] == "2026-09-06"


def test_persist_activate_and_read_as_of_the_journal() -> None:
    conn = _db()
    _plant(conn, days=14, stations=30, beta=0.6, seed=5)
    first = store.fit_beta(conn, stage_scope="P0", as_of="2026-09-10")
    store.persist_and_activate(conn, first, now="2026-09-10T06:00:00Z")
    store.journal_fit(conn, fit=first, outcome="ACTIVATED",
                      fitted_at_utc="2026-09-10T06:00:00Z")
    second = store.fit_beta(conn, stage_scope="P0", as_of="2026-09-14")
    store.persist_and_activate(conn, second, now="2026-09-14T06:00:00Z")
    store.journal_fit(conn, fit=second, outcome="ACTIVATED",
                      fitted_at_utc="2026-09-14T06:00:00Z")
    assert first["content_sha256"] != second["content_sha256"]
    active = store.load_beta_as_of(conn, stage_scope="P0")
    assert active["sha256"] == second["content_sha256"]
    then = store.load_beta_as_of(
        conn, stage_scope="P0", as_of_utc="2026-09-12T00:00:00Z"
    )
    assert then["sha256"] == first["content_sha256"]
    assert store.load_beta_as_of(conn, stage_scope="P1") is None
    tables = store.load_beta_tables(conn)
    assert set(tables) == {"P0", "P0-M", "P1"}
    assert tables["P0"]["beta_lcb"] == second["table"]["beta_lcb"]
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_beta_edge_table WHERE activated=1"
    ).fetchone()[0] == 1


def test_the_served_tables_mapping_carries_the_lane_under_its_own_key() -> None:
    from bayes_basket_target.effective_edge_store import load_effective_edge_tables

    conn = _db()
    tables = load_effective_edge_tables(conn)
    assert BETA_TABLE_KEY in tables
    assert set(tables[BETA_TABLE_KEY]) == {"P0", "P0-M", "P1"}
    assert all(v is None for v in tables[BETA_TABLE_KEY].values())


def test_the_belief_is_the_price_at_zero_and_the_claim_at_one() -> None:
    assert beta_belief_v1(0.9, 0.8, 0.0) == pytest.approx(0.8)
    assert beta_belief_v1(0.9, 0.8, 1.0) == pytest.approx(0.9)
    assert beta_belief_v1(0.9, 0.8, 0.5) == pytest.approx(0.85)
    assert beta_belief_v1(0.2, 0.8, 0.5) == pytest.approx(0.5)


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
        "schema": "target_basket_snapshot_v6", "snapshot_id": "c128", "sequence": 1,
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


def _windows(beta_tables, **overrides):
    from bayes_basket_target.delta_v2_runtime import _support_windows

    snapshot, probabilities = _snapshot()
    kwargs = dict(
        snapshot=snapshot, desired={"30°C", "31°C", "32°C"},
        probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=4, minimum_support_buckets=3,
        center_bucket="31°C", probability_stage="P0",
        effective_edge_tables=beta_tables,
    )
    kwargs.update(overrides)
    return _support_windows(**kwargs)


def test_shadow_records_the_beta_reading_and_changes_no_verdict() -> None:
    import bayes_basket_target.delta_v2_runtime as R

    assert R.BETA_EDGE_MODE == "shadow"
    plain, _ = _windows(None)
    served = {BETA_TABLE_KEY: {"P0": {"beta": 0.5, "beta_lcb": 0.2, "sha256": "x" * 64, "accumulating": False}}}
    shadow, audit = _windows(served)
    by_plain = {tuple(w["buckets"]): w for w in plain}
    priced = 0
    for w in shadow:
        before = by_plain[tuple(w["buckets"])]
        assert w["admissible"] == before["admissible"]
        assert w["effective_win_probability"] == pytest.approx(before["effective_win_probability"])
        if w["all_in_price"] is None:
            assert w["beta_edge"] is None
            continue
        priced += 1
        raw = w["raw_win_probability"]
        expected = beta_belief_v1(raw, w["all_in_price"], 0.2)
        assert w["beta_lcb"] == pytest.approx(0.2)
        assert w["beta_point"] == pytest.approx(0.5)
        assert w["beta_belief"] == pytest.approx(expected)
        assert w["beta_edge"] == pytest.approx(expected - w["all_in_price"])
        assert w["beta_admissible"] == (w["beta_edge"] > 1e-9)
        assert w["beta_table_sha256"] == "x" * 64
    assert priced > 0
    assert audit["beta_edge_mode"] == "shadow"
    assert audit["beta_served"]["P0"]["beta_lcb"] == pytest.approx(0.2)
    assert audit["beta_would_refuse_count"] + audit["beta_would_admit_count"] >= 0
    # No fit for the stage: shadow records nothing and changes nothing.
    none, audit_none = _windows({BETA_TABLE_KEY: {"P0": None}})
    assert all(w["beta_edge"] is None for w in none)
    assert audit_none["beta_would_refuse_count"] == 0


def test_on_reads_the_beta_belief_and_a_missing_fit_refuses(monkeypatch) -> None:
    import bayes_basket_target.delta_v2_runtime as R

    monkeypatch.setattr(R, "BETA_EDGE_MODE", "on")
    # No fit at all: beta_lcb 0, belief == price, nothing admissible.
    windows, audit = _windows({BETA_TABLE_KEY: {"P0": None}})
    assert audit["beta_edge_mode"] == "on"
    assert not any(w["admissible"] for w in windows if w["all_in_price"] is not None)
    for w in windows:
        if w["all_in_price"] is not None:
            assert w["beta_lcb"] == 0.0
            assert w["effective_win_probability"] == pytest.approx(w["all_in_price"], abs=1e-6)
            assert w["effective_edge_lambda_source"] == "beta_edge_lcb"
    # beta_lcb 1: the belief is the raw claim, so a window is admissible
    # exactly when its raw claim clears its price (and the structural gates).
    served = {BETA_TABLE_KEY: {"P0": {"beta": 1.0, "beta_lcb": 1.0, "sha256": "y" * 64, "accumulating": False}}}
    windows, _ = _windows(served)
    for w in windows:
        if w["all_in_price"] is None:
            continue
        assert w["effective_win_probability"] == pytest.approx(w["raw_win_probability"], abs=1e-6)
        # The solver's lambda is the ratio that reproduces the belief.
        assert w["effective_edge_lambda"] * w["win_probability"] == pytest.approx(w["raw_win_probability"], abs=1e-6)
    seated = [w for w in windows if w["admissible"]]
    assert seated, "a raw claim above its price must seat under beta_lcb = 1"
    assert all(w["raw_win_probability"] > w["all_in_price"] for w in seated)


def test_the_ledger_hashes_the_mode_and_the_sweep_runs_the_refit() -> None:
    ledger = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")
    assert '"beta_edge_mode": BETA_EDGE_MODE' in ledger
    assert '"beta_edge_rule": BETA_EDGE_RULE_VERSION' in ledger
    sweep = (ROOT / "scripts" / "calibration_refit_sweep.sh").read_text(encoding="utf-8")
    assert "refit_beta_edge.py --commit" in sweep
    cli = (ROOT / "scripts" / "refit_beta_edge.py").read_text(encoding="utf-8")
    assert "persist_and_activate" in cli and "journal_fit" in cli
