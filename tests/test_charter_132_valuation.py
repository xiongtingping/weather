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


def test_the_action_bound_is_a_quantile_across_whole_vectors():
    """``action_value_v1`` (read by the L4 lane) bounds an action by a
    quantile of its value across whole draw vectors."""

    vectors = [
        {key: (1.0 - w) * M[key] + w * P[key] for key in P}
        for w in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    got = kernel.action_value_v1(vectors, {"b": 1.0}, 0.40, quantile=0.0)
    masses = sorted(v["b"] for v in vectors)
    assert got["value_lcb"] == pytest.approx(masses[0] - 0.40)
    assert P["b"] < M["b"]
    assert got["value_lcb"] == pytest.approx(P["b"] - 0.40)


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
        "CREATE TABLE IF NOT EXISTS basket_station_day_settlement_label("
        " station_id TEXT, event_date TEXT, truth_revision_no INTEGER,"
        " settled_bucket TEXT, known_at_utc TEXT);"
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
                "INSERT INTO basket_station_day_settlement_label VALUES(?,?,0,?,?)",
                (f"S{s}", day, label, f"{day}T23:00:00Z"),
            )
            n += 1
    conn.commit()


def test_a_label_not_yet_knowable_is_not_evidence():
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=2, stations=3, weight=0.5)
    rows, meta = store.population(
        conn, stage_scope="P0", cutoff_utc="2026-08-01T23:30:00Z",
        window_days=40,
    )
    assert {r["date"] for r in rows} == {"2026-08-01"}


def _with_anchor(conn, monkeypatch, anchor):
    """A store that keeps an anchor, answering ``anchor`` for every stage."""

    import bayes_basket_target.center_width_store as cw

    conn.execute("CREATE TABLE IF NOT EXISTS basket_probability_bridge_history(x)")
    if isinstance(anchor, Exception):
        def _raise(conn, *, stage, known_floor=None):
            raise anchor
        monkeypatch.setattr(cw, "served_arithmetic_anchor", _raise)
    else:
        monkeypatch.setattr(
            cw, "served_arithmetic_anchor",
            lambda conn, *, stage, known_floor=None: anchor,
        )


def test_decisions_before_the_serving_anchor_are_not_evidence(monkeypatch):
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=20, stations=4, weight=0.5)
    _with_anchor(conn, monkeypatch, "2026-08-11T06:00:00Z")
    rows, meta = store.population(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
    )
    # A decision at the anchor's own instant is on the served vector.
    assert min(r["date"] for r in rows) == "2026-08-11"
    assert meta["anchor_utc"] == "2026-08-11T06:00:00Z"
    assert meta["excluded_before_anchor"] == 10 * 4
    # The research reading sees everything and says so.
    wide, wide_meta = store.population(
        conn, stage_scope="P0", cutoff_utc="2026-12-31T00:00:00Z", window_days=40,
        anchored=False,
    )
    assert len(wide) == 20 * 4 and wide_meta["anchor_utc"] is None
    assert wide_meta["anchored"] is False


def test_an_anchor_set_after_the_cutoff_did_not_exist_then(monkeypatch):
    conn = sqlite3.connect(":memory:")
    _plant(conn, days=5, stations=3, weight=0.5)
    _with_anchor(conn, monkeypatch, "2026-09-14T05:52:13Z")
    rows, meta = store.population(
        conn, stage_scope="P0", cutoff_utc="2026-08-31T00:00:00Z", window_days=40,
    )
    assert len(rows) == 5 * 3
    assert meta["anchor_utc"] is None and meta["anchors_after_cutoff"] == 1
