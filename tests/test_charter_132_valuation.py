"""Charter 132, stage B: one valuation centre, one uncertainty set.

The invariants the design review made pass conditions for stage B:

* every candidate reads the same vector -- a window's probability is a sum
  over it, so a subset window is never worth more than its superset;
* a fee or a depth change moves the action's cost and never the belief;
* a negative disagreement does not flip the conservatism of a bound;
* a book that does not describe the whole market refuses, it is not
  normalised into "the market";
* hard facts zero a bucket on both sources before the pool;
* the fit recovers a planted pool weight and holds below its hurdles.
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

from bayes_basket_target import valuation_store as store  # noqa: E402
from bayes_basket_target.basket_kernel import valuation_v1 as kernel  # noqa: E402


def _book(asks, bids=None, *, stamps=None, unmaterialized=()):
    rows = []
    for i, (bucket, ask) in enumerate(asks.items()):
        bid = (bids or {}).get(bucket)
        rows.append({
            "bucket": bucket,
            "ask_levels": [] if ask is None else [{"price": ask, "shares": 100.0}],
            "ask_available": ask is not None,
            "bid_levels": [] if bid is None else [{"price": bid, "shares": 50.0}],
            "bid_price": 0.0 if bid is None else bid,
            "unmaterialized_book": bucket in unmaterialized,
            "book_observed_at": (stamps or {}).get(bucket, "2026-09-13T00:00:00Z"),
        })
    return rows


# --------------------------------------------------------------------------
# Market reference
# --------------------------------------------------------------------------

def test_a_complete_book_normalises_its_mids():
    got = kernel.market_reference_v1(_book(
        {"a": 0.30, "b": 0.60, "c": 0.20}, {"a": 0.20, "b": 0.50, "c": 0.10},
    ))
    assert got["state"] == "complete"
    assert sum(got["vector"].values()) == pytest.approx(1.0)
    assert got["vector"]["b"] == pytest.approx(0.55 / (0.25 + 0.55 + 0.15))
    assert got["mid_sum"] == pytest.approx(0.95)


def test_a_cheap_one_sided_tail_is_a_bounded_mid_and_is_named():
    got = kernel.market_reference_v1(_book(
        {"a": 0.01, "b": 0.60}, {"b": 0.50},
    ))
    assert got["state"] == "complete"
    assert got["one_sided_buckets"] == ["a"]


@pytest.mark.parametrize(
    "asks, bids, reason",
    [
        ({"a": 0.30, "b": 0.60}, {"b": 0.50}, "one_sided_ask"),
        ({"a": None, "b": 0.60}, {"a": 0.40, "b": 0.50}, "one_sided_bid"),
        ({"a": None, "b": 0.60}, {"b": 0.50}, "empty_book"),
        ({"a": 0.30, "b": 0.60}, {"a": 0.40, "b": 0.50}, "crossed_book"),
    ],
)
def test_a_book_that_does_not_describe_the_market_refuses(asks, bids, reason):
    got = kernel.market_reference_v1(_book(asks, bids))
    assert got["state"] == "incomplete"
    assert str(got["reason"]).startswith(reason)


def test_an_unmaterialised_bucket_refuses():
    got = kernel.market_reference_v1(_book(
        {"a": 0.30, "b": 0.60}, {"a": 0.2, "b": 0.5}, unmaterialized=("a",),
    ))
    assert got["state"] == "incomplete" and got["reason"].startswith("unmaterialized")


def test_tokens_observed_too_far_apart_refuse():
    got = kernel.market_reference_v1(_book(
        {"a": 0.30, "b": 0.60}, {"a": 0.2, "b": 0.5},
        stamps={"a": "2026-09-13T00:00:00Z", "b": "2026-09-13T00:10:00Z"},
    ))
    assert got["state"] == "incomplete" and got["reason"] == "book_skew"


# --------------------------------------------------------------------------
# The pool
# --------------------------------------------------------------------------

M = {"a": 0.1, "b": 0.6, "c": 0.3}
P = {"a": 0.3, "b": 0.3, "c": 0.4}


def test_the_ends_of_the_pool_are_the_two_sources():
    assert kernel.valuation_vector_v1(P, M, 0.0) == pytest.approx(M)
    assert kernel.valuation_vector_v1(P, M, 1.0) == pytest.approx(P)


def test_a_hard_fact_zeroes_a_bucket_on_both_sources():
    got = kernel.valuation_vector_v1(P, M, 0.5, excluded=("c",))
    assert got["c"] == 0.0
    assert sum(got.values()) == pytest.approx(1.0)


def test_every_subset_window_is_worth_no_more_than_its_superset():
    """The cross-window inconsistency the audit reproduced cannot recur."""

    rng = random.Random(7)
    buckets = [f"b{i}" for i in range(9)]
    for _ in range(200):
        m = {b: rng.random() for b in buckets}
        p = {b: rng.random() for b in buckets}
        tm, tp = sum(m.values()), sum(p.values())
        m = {b: v / tm for b, v in m.items()}
        p = {b: v / tp for b, v in p.items()}
        vector = kernel.valuation_vector_v1(p, m, rng.random())
        start = rng.randrange(7)
        sub = buckets[start:start + 2]
        sup = buckets[max(0, start - 1):start + 3]
        assert kernel.window_mass(vector, sub) <= kernel.window_mass(vector, sup) + 1e-12


def test_a_price_change_moves_the_action_and_never_the_belief():
    vector = kernel.valuation_vector_v1(P, M, 0.3)
    again = kernel.valuation_vector_v1(P, M, 0.3)
    assert vector == again  # the vector takes no price at all
    cheap = kernel.action_value_v1([vector], {"b": 1.0}, 0.40)
    dear = kernel.action_value_v1([vector], {"b": 1.0}, 0.45)
    assert cheap["value_point"] - dear["value_point"] == pytest.approx(0.05)


def test_the_action_bound_is_a_quantile_across_whole_vectors():
    vectors = kernel.beta_draw_vectors(P, M, [0.0, 0.25, 0.5, 0.75, 1.0])
    got = kernel.action_value_v1(vectors, {"b": 1.0}, 0.40, quantile=0.0)
    masses = sorted(v["b"] for v in vectors)
    assert got["value_lcb"] == pytest.approx(masses[0] - 0.40)
    # A negative-disagreement action (weather says less than the market) takes
    # its worst value at the weather end, and the bound says so.
    assert P["b"] < M["b"]
    assert got["value_lcb"] == pytest.approx(P["b"] - 0.40)


def test_the_ladder_cost_walks_depth():
    cost, got = kernel.ladder_cost_v1([(0.40, 10.0), (0.50, 10.0)], 15.0)
    assert got == 15.0 and cost == pytest.approx(10 * 0.40 + 5 * 0.50)
    cost, got = kernel.ladder_cost_v1([(0.40, 10.0)], 15.0)
    assert got == 10.0


def test_capacity_stops_where_the_bound_stops_clearing():
    levels = {"b": [(0.40, 10.0), (0.60, 10.0)]}
    got = kernel.capacity_v1(
        window=["b"], ask_levels_by_bucket=levels, mass_lcb=0.50, hurdle=0.05,
    )
    # 10 shares cost 4.00 against 5.00 of lower-bound payout: clears 5%.
    # 20 shares cost 10.00 against 10.00: does not.
    assert got["capacity_shares"] == 10.0
    assert got["capacity_cost"] == pytest.approx(4.0)
    floor = kernel.capacity_v1(
        window=["b"], ask_levels_by_bucket=levels, mass_lcb=0.50, hurdle=0.05,
        min_order_size=15.0,
    )
    assert floor["capacity_shares"] == 0.0


# --------------------------------------------------------------------------
# The fit
# --------------------------------------------------------------------------

def _plant(conn, *, days, stations, weight, seed=11, stage="P0"):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS basket_policy_training_fact("
        " station_id TEXT, event_date TEXT, stage TEXT, decision_at_utc TEXT,"
        " probability_envelope_id TEXT, ws_snapshot_id TEXT);"
        "CREATE TABLE IF NOT EXISTS basket_probability_envelope("
        " envelope_id TEXT, probabilities_json TEXT);"
        "CREATE TABLE IF NOT EXISTS basket_official_ws_snapshot("
        " snapshot_id TEXT, buckets_json TEXT);"
        "CREATE TABLE IF NOT EXISTS paper_trade_candidate_window("
        " station_id TEXT, event_date TEXT, settled_bucket TEXT,"
        " settled_at_utc TEXT, truth_revision_no INTEGER);"
    )
    rng = random.Random(seed)
    buckets = ["a", "b", "c", "d"]
    n = 0
    for d in range(1, days + 1):
        day = f"2026-08-{d:02d}"
        for s in range(stations):
            m = [rng.random() + 0.1 for _ in buckets]
            p = [rng.random() + 0.1 for _ in buckets]
            m = [x / sum(m) for x in m]
            p = [x / sum(p) for x in p]
            truth = [(1 - weight) * mi + weight * pi for mi, pi in zip(m, p)]
            label = rng.choices(buckets, weights=truth)[0]
            # A two-sided book whose normalised mids are exactly ``m``.
            book = [
                {"bucket": b, "ask_levels": [{"price": mi + 0.01, "shares": 50}],
                 "ask_available": True,
                 "bid_levels": [{"price": mi - 0.01, "shares": 50}],
                 "bid_price": mi - 0.01, "book_observed_at": f"{day}T06:00:00Z"}
                for b, mi in zip(buckets, m)
            ]
            conn.execute(
                "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?)",
                (f"S{s}", day, stage, f"{day}T06:00:00Z", f"e{n}", f"w{n}"),
            )
            conn.execute(
                "INSERT INTO basket_probability_envelope VALUES(?,?)",
                (f"e{n}", json.dumps(dict(zip(buckets, p)))),
            )
            conn.execute(
                "INSERT INTO basket_official_ws_snapshot VALUES(?,?)",
                (f"w{n}", json.dumps(book)),
            )
            conn.execute(
                "INSERT INTO paper_trade_candidate_window VALUES(?,?,?,?,0)",
                (f"S{s}", day, label, f"{day}T23:00:00Z"),
            )
            n += 1
    conn.commit()


def test_the_fit_recovers_a_planted_pool_weight():
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=20, stations=60, weight=0.6)
    fit = store.fit_valuation(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
    )
    ev = fit["evidence"]
    assert not fit["accumulating"]
    assert abs(ev["beta"] - 0.6) < 0.2
    assert ev["beta_lcb"] <= ev["beta"] <= ev["beta_ucb"]
    assert ev["scores"]["log_pool"] >= ev["scores"]["log_market"]
    assert ev["scores"]["log_pool"] >= ev["scores"]["log_weather"]
    assert len(ev["beta_draws"]) == store.BOOTSTRAP_DRAWS


def test_a_market_only_world_fits_beta_at_zero():
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=20, stations=60, weight=0.0)
    fit = store.fit_valuation(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
    )
    assert fit["evidence"]["beta"] <= 0.15


def test_below_the_hurdles_the_fit_serves_no_draws():
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=3, stations=5, weight=0.5)
    fit = store.fit_valuation(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
    )
    assert fit["accumulating"] and fit["evidence"]["beta_draws"] == []


def test_a_label_not_yet_knowable_is_not_evidence():
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=2, stations=3, weight=0.5)
    rows, meta = store.population(
        conn, stage_scope="P0", cutoff_utc="2026-08-01T23:30:00Z",
        window_days=40,
    )
    assert {r["date"] for r in rows} == {"2026-08-01"}


def test_persist_load_and_verdict_round_trip():
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=20, stations=60, weight=0.4)
    fit = store.fit_valuation(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
    )
    store.persist_and_activate(conn, fit, now="2026-09-13T00:00:00Z")
    store.journal_fit(conn, fit=fit, outcome="ACTIVATED",
                      fitted_at_utc="2026-09-13T00:00:00Z")
    served = store.load_valuation_as_of(conn, stage_scope="P0")
    assert served["beta"] == fit["evidence"]["beta"]
    assert len(served["beta_draws"]) == store.BOOTSTRAP_DRAWS
    assert served["verdict"] is None
    store.persist_verdict(
        conn, stage_scope="P0", verdict="hold", reason="test", evidence={},
        computed_at_utc="2026-09-13T01:00:00Z", first_test_day=None,
        last_test_day=None,
    )
    assert store.load_valuation_as_of(conn, stage_scope="P0")["verdict"] == "hold"
    # As of a moment before the verdict, none stood.
    before = store.load_valuation_as_of(
        conn, stage_scope="P0", as_of_utc="2026-09-13T00:30:00Z",
    )
    assert before["verdict"] is None
