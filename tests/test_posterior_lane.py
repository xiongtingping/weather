"""L4 posterior lane (first-principles baseline, shadow).

What is pinned:

* the kernel: zero parameters reproduce the market's vector, ``w = 1`` with
  ``c = 0`` reproduces the served claim, a hard fact zeroes a bucket, and a
  window is a sum over one vector;
* the fit recovers a planted log-odds weight and reads ~0 in a market-only
  world, with its interval containing zero; below the hurdles it serves no
  draws;
* persistence round-trips the stage row with its band rows, and the served
  mapping carries the lane under its own key;
* the runtime records the belief and its verdict on every priced window and
  changes no verdict and no effective win probability;
* the forward skill scores a decision only on a fit that was active before
  it was taken.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import posterior_store as store  # noqa: E402
from bayes_basket_target.basket_kernel import posterior_v1 as kernel  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402


# --------------------------------------------------------------------------
# Kernel
# --------------------------------------------------------------------------

def test_zero_parameters_reproduce_the_market() -> None:
    m = {"a": 0.2, "b": 0.5, "c": 0.3}
    p = {"a": 0.6, "b": 0.3, "c": 0.1}
    got = kernel.posterior_vector_v1(p, m, {"alpha": 0.0, "gamma": 0.0, "w": 0.0})
    for k in m:
        assert got[k] == pytest.approx(m[k], abs=1e-9)


def test_unit_weight_without_recalibration_reproduces_the_claim() -> None:
    m = {"a": 0.2, "b": 0.5, "c": 0.3}
    p = {"a": 0.6, "b": 0.3, "c": 0.1}
    got = kernel.posterior_vector_v1(p, m, (0.0, 0.0, 1.0))
    for k in m:
        assert got[k] == pytest.approx(p[k], abs=1e-6)


def test_a_hard_fact_zeroes_a_bucket_and_the_rest_renormalises() -> None:
    m = {"a": 0.2, "b": 0.5, "c": 0.3}
    p = {"a": 0.6, "b": 0.3, "c": 0.1}
    got = kernel.posterior_vector_v1(p, m, (0.0, 0.0, 0.5), excluded=["a"])
    assert got["a"] == 0.0
    assert sum(got.values()) == pytest.approx(1.0)


def test_window_mass_sums_one_vector_and_draws_are_whole_vectors() -> None:
    m = {"a": 0.2, "b": 0.5, "c": 0.3}
    p = {"a": 0.6, "b": 0.3, "c": 0.1}
    draws = [(0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.1, -0.2, 0.4)]
    vectors = kernel.posterior_draw_vectors_v1(p, m, draws)
    assert len(vectors) == 3
    for v in vectors:
        assert sum(v.values()) == pytest.approx(1.0)
        assert kernel.window_mass(v, ["a", "b"]) == pytest.approx(v["a"] + v["b"])


def test_the_mode_defaults_to_shadow() -> None:
    assert kernel.POSTERIOR_MODE == "shadow"


# --------------------------------------------------------------------------
# The fit
# --------------------------------------------------------------------------

def _plant(conn, *, days, stations, w, gamma=0.0, seed=11, stage="P0",
           decisions_per_day=2):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS basket_policy_training_fact("
        " station_id TEXT, event_date TEXT, stage TEXT, decision_at_utc TEXT,"
        " probability_envelope_id TEXT, ws_snapshot_id TEXT);"
        "CREATE TABLE IF NOT EXISTS basket_probability_envelope("
        " envelope_id TEXT, probabilities_json TEXT, settlement_timezone TEXT);"
        "CREATE TABLE IF NOT EXISTS basket_official_ws_snapshot("
        " snapshot_id TEXT, buckets_json TEXT);"
        "CREATE TABLE IF NOT EXISTS basket_station_day_settlement_label("
        " station_id TEXT, event_date TEXT, truth_revision_no INTEGER,"
        " settled_bucket TEXT, known_at_utc TEXT);"
    )
    rng = random.Random(seed)
    buckets = ["a", "b", "c", "d", "e"]
    n = 0
    # Two decisions per station-day at different leads (42h and 18h before
    # the end of the UTC event day), so two lead bands carry rows.
    clocks = ["T06:00:00Z", "T06:00:00Z"][:decisions_per_day]
    for d in range(1, days + 1):
        day = f"2026-08-{d:02d}"
        prev = f"2026-07-{30 + d:02d}" if d <= 1 else f"2026-08-{d - 1:02d}"
        for s in range(stations):
            m = [rng.random() + 0.1 for _ in buckets]
            p = [rng.random() + 0.1 for _ in buckets]
            m = [x / sum(m) for x in m]
            p = [x / sum(p) for x in p]
            scores = [
                kernel.expit(kernel.logit(mi) * (1.0 + gamma) + w * (kernel.logit(pi) - kernel.logit(mi)))
                for mi, pi in zip(m, p)
            ]
            truth = [x / sum(scores) for x in scores]
            label = rng.choices(buckets, weights=truth)[0]
            book = [
                {"bucket": b, "ask_levels": [{"price": mi + 0.01, "shares": 50}],
                 "ask_available": True,
                 "bid_levels": [{"price": max(0.001, mi - 0.01), "shares": 50}],
                 "bid_price": max(0.001, mi - 0.01), "book_observed_at": f"{day}T06:00:00Z"}
                for b, mi in zip(buckets, m)
            ]
            for i, clock in enumerate(clocks):
                at = (prev if i == 0 else day) + clock
                conn.execute(
                    "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?)",
                    (f"S{s}", day, stage, at, f"e{n}", f"w{n}"),
                )
                conn.execute(
                    "INSERT INTO basket_probability_envelope VALUES(?,?,?)",
                    (f"e{n}", json.dumps(dict(zip(buckets, p))), "UTC"),
                )
                conn.execute(
                    "INSERT INTO basket_official_ws_snapshot VALUES(?,?)",
                    (f"w{n}", json.dumps(book)),
                )
                n += 1
            conn.execute(
                "INSERT INTO basket_station_day_settlement_label VALUES(?,?,0,?,?)",
                (f"S{s}", day, label, f"{day}T23:00:00Z"),
            )
    conn.commit()


def test_the_population_carries_a_lead_band_per_decision() -> None:
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=2, stations=3, w=0.5)
    rows, meta = store.population(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
    )
    assert rows and meta["rows_without_clock"] == 0
    assert {r["lead_band"] for r in rows} == {">=36h", "12-24h"}


def test_the_fit_recovers_a_planted_log_odds_weight() -> None:
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=20, stations=60, w=0.6)
    fits = store.fit_stage_and_bands(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
    )
    whole = fits[0]
    ev = whole["evidence"]
    assert not whole["accumulating"]
    assert abs(ev["point"]["w"] - 0.6) < 0.2
    assert ev["ci"]["w"][0] <= ev["point"]["w"] <= ev["ci"]["w"][1]
    assert ev["skill"]["s_in_sample"] > 0.0
    assert ev["skill"]["ic_in_sample"] > 0.0
    assert len(ev["draws"]) == store.BOOTSTRAP_DRAWS
    bands = {f["lead_band"]: f for f in fits[1:]}
    assert set(bands) == set(store.LEAD_BAND_LABELS)
    # The two populated bands are shrunk to the stage and read near it.
    for label in (">=36h", "12-24h"):
        assert not bands[label]["accumulating"]
        assert abs(bands[label]["evidence"]["point"]["w"] - ev["point"]["w"]) < 0.3
    assert bands["<6h"]["accumulating"]


def test_a_market_only_world_fits_w_near_zero_with_zero_inside() -> None:
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=20, stations=60, w=0.0)
    fit = store.fit_posterior(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
    )
    ev = fit["evidence"]
    assert abs(ev["point"]["w"]) < 0.15
    assert ev["ci"]["w"][0] <= 0.0 <= ev["ci"]["w"][1]


def test_below_the_hurdles_the_fit_serves_no_draws() -> None:
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=3, stations=5, w=0.5)
    fit = store.fit_posterior(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
    )
    assert fit["accumulating"]
    assert fit["evidence"]["draws"] == []
    assert fit["evidence"]["point"]["w"] is None


def test_persist_load_round_trip_carries_the_band_rows() -> None:
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=20, stations=60, w=0.4)
    fits = store.fit_stage_and_bands(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
    )
    for fit in fits:
        store.persist_and_activate(conn, fit, now="2026-09-17T00:00:00Z")
        store.journal_fit(conn, fit=fit, outcome="ACTIVATED",
                          fitted_at_utc="2026-09-17T00:00:00Z")
    served = store.load_posterior_as_of(conn, stage_scope="P0")
    assert served["w"] == pytest.approx(fits[0]["evidence"]["point"]["w"])
    assert len(served["draws"]) == store.BOOTSTRAP_DRAWS
    assert set(served["lead_bands"]) == set(store.LEAD_BAND_LABELS)
    assert served["lead_bands"]["<6h"]["accumulating"]
    assert not served["lead_bands"][">=36h"]["accumulating"]
    # As-of before the activation: nothing served.
    assert store.load_posterior_as_of(
        conn, stage_scope="P0", as_of_utc="2026-09-16T00:00:00Z",
    ) is None
    assert store.load_posterior_as_of(conn, stage_scope="P1") is None
    row, band = store.serving_row(served, ">=36h")
    assert band == ">=36h" and row is served["lead_bands"][">=36h"]
    row, band = store.serving_row(served, "<6h")
    assert band is None and row is served
    assert store.register_hypotheses(conn, now="2026-09-17T00:00:00Z") == len(store.HYPOTHESES_V0)
    assert store.register_hypotheses(conn, now="2026-09-17T00:00:00Z") == 0


def test_the_served_tables_mapping_carries_the_lane_under_its_own_key() -> None:
    from bayes_basket_target.served_tables import load_served_tables

    conn = sqlite3.connect(":memory:")
    tables = load_served_tables(conn)
    assert kernel.POSTERIOR_TABLE_KEY in tables
    assert set(tables[kernel.POSTERIOR_TABLE_KEY]) == {"P0", "P0-M", "P1"}
    assert all(v is None for v in tables[kernel.POSTERIOR_TABLE_KEY].values())


def test_forward_skill_scores_only_decisions_after_a_fit() -> None:
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=20, stations=60, w=0.6)
    fit = store.fit_posterior(
        conn, stage_scope="P0", cutoff_utc="2026-08-10T23:30:00Z", window_days=40,
    )
    store.persist_and_activate(conn, fit, now="2026-08-10T23:30:00Z")
    store.journal_fit(conn, fit=fit, outcome="ACTIVATED",
                      fitted_at_utc="2026-08-10T23:30:00Z")
    fwd = store.forward_skill(conn, stage_scope="P0", window_days=40)
    assert fwd["before_first_fit"] > 0
    assert fwd["decisions"] > 0
    assert fwd["first_day"] >= "2026-08-11"
    assert fwd["s_forward"] > 0.0
    assert fwd["s_forward_ci"][0] is not None


# --------------------------------------------------------------------------
# Runtime shadow
# --------------------------------------------------------------------------

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
        "schema": "target_basket_snapshot_v6", "snapshot_id": "l4", "sequence": 1,
        "observed_at": "2026-09-17T00:00:00Z", "market_id": "m", "station_id": "X",
        "event_date": "2026-09-18", "quote_version": "q",
        "quote_observed_at": "2026-09-17T00:00:00Z", "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "i", "order_state_version": "o",
        "evidence": {"probability_stage": "P0", "posterior_version": "p", "posterior_sha256": "a" * 64},
        "buckets": rows, "checkpoint_changed": True, "quote_changed": True,
        "fill_changed": True, "time_gate_changed": True, "evidence_fresh": True,
        "contract_matches": True, "order_state_known": True, "risk_inputs_complete": True,
        "time_left_minutes": 600.0,
    })
    return snapshot, dict(zip(labels, probs))


def _windows(tables, **overrides):
    from bayes_basket_target.delta_v2_runtime import _support_windows

    snapshot, probabilities = _snapshot()
    kwargs = dict(
        snapshot=snapshot, desired={"30°C", "31°C", "32°C"},
        probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=4, minimum_support_buckets=3,
        center_bucket="31°C", probability_stage="P0",
        served_tables=tables,
    )
    kwargs.update(overrides)
    return _support_windows(**kwargs)


def test_shadow_records_the_posterior_reading_and_changes_no_verdict() -> None:
    import bayes_basket_target.delta_v2_runtime as R

    assert R.POSTERIOR_MODE == "shadow"
    plain, _ = _windows(None)
    params = {"alpha": 0.0, "gamma": 0.0, "w": 0.5}
    served = {kernel.POSTERIOR_TABLE_KEY: {"P0": {
        **params, "w_lcb": 0.3, "w_ucb": 0.7, "sha256": "x" * 64,
        "accumulating": False, "lead_band": "",
        "draws": tuple((0.0, 0.0, 0.3 + 0.02 * i) for i in range(21)),
        "lead_bands": {},
    }}}
    shadow, audit = _windows(served)
    snapshot, probabilities = _snapshot()
    market = market_reference_v1(snapshot.buckets)
    assert market["state"] == "complete"
    expected_vector = kernel.posterior_vector_v1(probabilities, market["vector"], params)
    by_plain = {tuple(w["buckets"]): w for w in plain}
    priced = 0
    for w in shadow:
        before = by_plain[tuple(w["buckets"])]
        assert w["admissible"] == before["admissible"]
        assert w["effective_win_probability"] == pytest.approx(before["effective_win_probability"])
        if w["all_in_price"] is None:
            assert w["posterior_mass"] is None
            continue
        priced += 1
        mass = kernel.window_mass(expected_vector, w["buckets"])
        assert w["posterior_mass"] == pytest.approx(mass)
        assert w["posterior_market_mass"] == pytest.approx(
            kernel.window_mass(market["vector"], w["buckets"])
        )
        assert w["posterior_edge_point"] == pytest.approx(mass - w["all_in_price"])
        assert w["posterior_edge_lcb"] is not None
        assert w["posterior_edge_lcb"] <= w["posterior_edge_point"] + 1e-9
        assert isinstance(w["posterior_admissible"], bool)
    assert priced > 0
    assert audit["posterior_mode"] == "shadow"
    assert audit["posterior_state"] == "shadow"
    assert audit["posterior_served"]["w"] == pytest.approx(0.5)
    assert audit["posterior_would_refuse_count"] + audit["posterior_would_admit_count"] >= 0
    # No fit for the stage: state says so and nothing is recorded.
    none, audit_none = _windows({kernel.POSTERIOR_TABLE_KEY: {"P0": None}})
    assert audit_none["posterior_state"] == "no_fit"
    assert all(w["posterior_mass"] is None for w in none)
    # An accumulating row: the lane serves b = m and records the state.
    acc, audit_acc = _windows({kernel.POSTERIOR_TABLE_KEY: {"P0": {
        "alpha": None, "gamma": None, "w": None, "accumulating": True,
        "lead_bands": {}, "sha256": "y" * 64, "draws": (),
    }}})
    assert audit_acc["posterior_state"] == "accumulating"
    assert all(w["posterior_admissible"] is None for w in acc)


# --------------------------------------------------------------------------
# Realisation: the windows L4 would buy, after they settled
# --------------------------------------------------------------------------







