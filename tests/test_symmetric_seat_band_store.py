"""The lane that fits charter 79's width band instead of a person setting it.

Every test here rests on the band being FITTED and ADMISSION-GATED.  If one
starts failing because a serving path began reading the candidate rather than
the activated row, the fix is the serving path.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target import symmetric_seat_band_store as store


def _lane(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "CREATE TABLE basket_p0_preselection_plan ("
        " station_id TEXT, event_date TEXT, created_at TEXT, plan_json TEXT);"
        "CREATE TABLE basket_policy_training_fact ("
        " fact_id TEXT, station_id TEXT, event_date TEXT);"
        "CREATE TABLE basket_policy_training_label ("
        " fact_id TEXT, winning_bucket TEXT);"
    )
    store.ensure_schema(conn)


def _plan(conn, *, station, day, created, sigma, forced, free, stage="P0"):
    payload = {
        "compiled_plan": {
            "stage": stage,
            "plan": {
                "basket_support_selection": {
                    "symmetric_seat_counterfactual": {
                        "schema": "symmetric_seat_counterfactual_v1",
                        "sigma_buckets": sigma,
                        "stage": stage,
                        "forced": forced,
                        "free": free,
                    }
                }
            },
        }
    }
    conn.execute(
        "INSERT INTO basket_p0_preselection_plan VALUES(?,?,?,?)",
        (station, day, created, json.dumps(payload)),
    )


def _truth(conn, *, station, day, bucket):
    fact = f"{station}-{day}"
    conn.execute("INSERT INTO basket_policy_training_fact VALUES(?,?,?)",
                 (fact, station, day))
    conn.execute("INSERT INTO basket_policy_training_label VALUES(?,?)",
                 (fact, bucket))


def _arm(buckets, price):
    return {"buckets": list(buckets), "all_in_price": price,
            "win_probability": 0.8, "edge": 0.1}


def test_a_settled_pair_is_worth_payout_minus_what_it_would_have_cost() -> None:
    assert store._arm_value(_arm(["21", "22"], 0.6), "22") == pytest.approx(0.4)
    assert store._arm_value(_arm(["21", "22"], 0.6), "25") == pytest.approx(-0.6)
    # The rule's hold-and-do-not-buy case is worth zero, not missing: no seat
    # means no cost and no payout, and dropping the day would silently
    # reweight the sample toward days the rule could act on.
    assert store._arm_value(None, "22") == 0.0
    assert store._arm_value({"buckets": ["22"], "all_in_price": None}, "22") == 0.0


def test_the_band_scores_forcing_above_the_bound_and_choosing_below_it() -> None:
    pairs = [
        {"event_date": "d1", "sigma_buckets": 0.6,
         "forced_value": -0.30, "free_value": 0.20, "forced_present": True},
        {"event_date": "d2", "sigma_buckets": 1.8,
         "forced_value": 0.50, "free_value": -0.10, "forced_present": True},
    ]
    # Force everywhere: the narrow day's bad forced arm is taken.
    assert store._score(pairs, 0.0) == pytest.approx((-0.30 + 0.50) / 2)
    # Force only above one step: each day gets its better arm.
    assert store._score(pairs, 1.0) == pytest.approx((0.20 + 0.50) / 2)
    # Never force: the wide day's good forced arm is given up.
    assert store._score(pairs, 2.0) == pytest.approx((0.20 - 0.10) / 2)
    assert store._score([], 1.0) == 0.0


def test_a_thin_lane_holds_the_incumbent_and_says_it_is_accumulating() -> None:
    conn = sqlite3.connect(":memory:")
    _lane(conn)
    for i in range(3):
        day = f"2026-09-0{i + 1}"
        _plan(conn, station="AAAA", day=day, created=f"{day}T10:00:00Z",
              sigma=0.5, forced=_arm(["21"], 0.9), free=_arm(["22"], 0.2))
        _truth(conn, station="AAAA", day=day, bucket="22")
    out = store.fit_symmetric_seat_band(conn, stage="P0", incumbent_lower=1.0)
    assert out["accumulating"] is True
    assert out["admitted"] is False
    assert out["lower_bound"] == 1.0
    assert out["evidence"]["basis"] == "ACCUMULATING"
    conn.close()


def _stock_lane(conn, *, narrow_free_wins=True):
    """Twelve days, six stations: narrow days where choosing wins, wide days
    where forcing wins.  The right answer is a bound between the two.  Sized
    past both floors on purpose -- a fixture under them proves only that the
    accumulating branch works, which its own test already does."""
    for i in range(12):
        day = f"2026-09-{i + 1:02d}"
        for s in range(6):
            station = f"ST{s:02d}"
            narrow = s < 3
            sigma = 0.6 if narrow else 1.8
            if narrow and narrow_free_wins:
                forced, free, winner = _arm(["21"], 0.9), _arm(["22"], 0.2), "22"
            else:
                forced, free, winner = _arm(["22"], 0.2), _arm(["21"], 0.9), "22"
            _plan(conn, station=station, day=day,
                  created=f"{day}T10:00:00Z", sigma=sigma,
                  forced=forced, free=free)
            _truth(conn, station=station, day=day, bucket=winner)


def test_the_bound_is_fitted_where_forcing_stops_paying() -> None:
    conn = sqlite3.connect(":memory:")
    _lane(conn)
    _stock_lane(conn)
    out = store.fit_symmetric_seat_band(conn, stage="P0", incumbent_lower=0.0)
    assert out["accumulating"] is False
    assert out["admitted"] is True
    # Anywhere above the narrow days and at or below the wide ones.
    assert 0.6 < out["lower_bound"] <= 1.8
    assert out["forward_gain_per_seat"] > store.MIN_FORWARD_GAIN_PER_SEAT
    # The whole grid is journalled, not just the winner: a band chosen from
    # a flat curve and one chosen from a peak are different claims.
    assert len(out["evidence"]["train_scores"]) == len(store.LOWER_BOUND_GRID)
    assert out["evidence"]["forward_day_count"] >= 1
    conn.close()


def test_a_candidate_that_does_not_beat_the_incumbent_forward_does_not_serve() -> None:
    conn = sqlite3.connect(":memory:")
    _lane(conn)
    _stock_lane(conn)
    # The incumbent is already the right answer, so the best candidate ties
    # it and the margin refuses the churn.
    out = store.fit_symmetric_seat_band(conn, stage="P0", incumbent_lower=1.0)
    assert out["accumulating"] is False
    assert out["admitted"] is False
    assert out["lower_bound"] == 1.0
    assert out["evidence"]["forward_gain_per_seat"] <= store.MIN_FORWARD_GAIN_PER_SEAT
    conn.close()


def test_only_the_activated_row_is_served_and_activation_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    _lane(conn)
    assert store.load_symmetric_seat_band(conn, stage="P0") is None
    first = store.persist_and_activate(
        conn, stage="P0", lower_bound=1.0, upper_bound=None,
        pair_count=90, day_count=10, forward_gain_per_seat=0.05,
    )
    assert first["state"] == "ACTIVATED"
    assert store.persist_and_activate(
        conn, stage="P0", lower_bound=1.0, upper_bound=None,
        pair_count=90, day_count=10, forward_gain_per_seat=0.05,
    )["state"] == "UNCHANGED"
    store.persist_and_activate(
        conn, stage="P0", lower_bound=0.5, upper_bound=None,
        pair_count=99, day_count=11, forward_gain_per_seat=0.06,
    )
    served = store.load_symmetric_seat_band(conn, stage="P0")
    assert served is not None and served[0] == 0.5 and served[1] == float("inf")
    # Exactly one row serves a stage at a time.
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_symmetric_seat_band"
        " WHERE stage='P0' AND activated=1"
    ).fetchone()[0] == 1
    # And a stage nobody fitted keeps serving nothing.
    assert store.load_symmetric_seat_band(conn, stage="P0-M") is None
    assert set(store.load_symmetric_seat_bands(conn)) == {"P0"}
    conn.close()


def test_one_stage_throwing_does_not_lose_the_other() -> None:
    conn = sqlite3.connect(":memory:")
    _lane(conn)
    _stock_lane(conn)
    real = store.fit_symmetric_seat_band

    def flaky(inner, *, stage, incumbent_lower, min_event_date=None):
        if stage == "P0":
            raise sqlite3.OperationalError("database is locked")
        return real(inner, stage=stage, incumbent_lower=incumbent_lower,
                    min_event_date=min_event_date)

    store.fit_symmetric_seat_band = flaky
    try:
        out = store.refit_all(
            conn, incumbent_lower_by_stage={"P0": 1.0, "P0-M": 1.0}, commit=True
        )
    finally:
        store.fit_symmetric_seat_band = real
    assert "database is locked" in out["P0"]["error"]
    assert "error" not in out["P0-M"]
    assert out["P0-M"]["outcome"] in {
        "ACCUMULATING", "REFUSED_FORWARD", "ACTIVATED"
    }
    conn.close()


def test_the_pair_is_read_from_the_last_plan_of_the_station_day() -> None:
    """A talkative day must not outweigh a quiet one."""
    conn = sqlite3.connect(":memory:")
    _lane(conn)
    _plan(conn, station="AAAA", day="2026-09-01", created="2026-09-01T08:00:00Z",
          sigma=0.5, forced=_arm(["21"], 0.9), free=_arm(["21"], 0.9))
    _plan(conn, station="AAAA", day="2026-09-01", created="2026-09-01T20:00:00Z",
          sigma=1.7, forced=_arm(["22"], 0.25), free=_arm(["23"], 0.75))
    _truth(conn, station="AAAA", day="2026-09-01", bucket="22")
    pairs = store.collect_pairs(conn, stage="P0")
    assert len(pairs) == 1
    assert pairs[0]["sigma_buckets"] == 1.7
    assert pairs[0]["forced_value"] == pytest.approx(0.75)
    assert pairs[0]["free_value"] == pytest.approx(-0.75)
    conn.close()


def test_a_day_with_no_settled_truth_is_not_a_pair() -> None:
    conn = sqlite3.connect(":memory:")
    _lane(conn)
    _plan(conn, station="AAAA", day="2026-09-01", created="2026-09-01T10:00:00Z",
          sigma=1.2, forced=_arm(["22"], 0.3), free=_arm(["23"], 0.4))
    assert store.collect_pairs(conn, stage="P0") == []
    conn.close()
