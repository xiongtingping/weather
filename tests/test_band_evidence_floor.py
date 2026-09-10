"""The band lane bounds its evidence below, and holds rather than blanks.

This table measures how wrong the *served* claim is, so a decision taken
before the serving arithmetic changed describes a distribution nobody serves
any more.  The centre/width lane has carried that bound since 2026-08-27 and
its own docstring records what happens without one: "after the width fix of
2026-08-18 an unbounded fit reproduced the old 0.825 exactly, because weeks
of pre-fix decisions outvoted the handful of post-fix ones."

This lane had no bound at all.  Measured on the live store 2026-09-10, P0 was
fitting 41 days reaching back to 2026-07-28 -- across the width layer reaching
the fleet on 2026-09-04 -- and two of its three non-identity cells only clear
their interval on that pooled sample.  On the days after the change they cost
0.85 of a peak-hit point while buying 0.0020 nat; a fourteen-day bounded
window scored at least as well as the unbounded one at all three walk-forward
cuts tried, and never moved peak hit.

The second half is the consequence of adding the bound: a stage's window can
shorten by weeks in a single step, and ``refit_all_stages`` activates whatever
the fit returns.  Answering identity when starved would retire a fitted table
for the sole reason that the lane stopped being able to see enough days.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import band_reliability_store as store  # noqa: E402


def _evidence_lanes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "CREATE TABLE calibration_station_daily_high_truth ("
        " station_id TEXT, local_date TEXT, official_tmax_c REAL,"
        " revision_no INTEGER DEFAULT 0, source_lane TEXT DEFAULT 'AWC',"
        " settlement_definition TEXT DEFAULT 'official_daily_tmax_c');"
        "CREATE TABLE basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, history_id TEXT,"
        " envelope_json TEXT, decision_at_utc TEXT, stage TEXT,"
        " checkpoint_id TEXT DEFAULT '',"
        " envelope_content_sha256 TEXT DEFAULT '');"
        "CREATE TABLE basket_probability_bridge_bucket_probability ("
        " history_id TEXT, bucket TEXT, lower_c REAL, upper_c REAL,"
        " probability REAL);"
        "CREATE TABLE calibration_cross_native_ensemble_oos_prediction ("
        " station_id TEXT, event_date TEXT, probabilities_json TEXT,"
        " bucket_centers_c_json TEXT, cross_ensemble_artifact_id TEXT,"
        " trained_through_event_date TEXT, decision_at_utc TEXT,"
        " lead_bucket TEXT);"
        "CREATE TABLE calibration_cross_native_ensemble_artifact ("
        " cross_ensemble_artifact_id TEXT, combination_method TEXT);"
    )


def _day(conn: sqlite3.Connection, day: str, decided: str, warm: float) -> None:
    """One station-day whose claim on the settled bucket is ``warm``."""

    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth"
        "(station_id,local_date,official_tmax_c) VALUES (?,?,?)",
        ("ZBAA", day, 30.0),
    )
    history_id = f"h_{day}_{decided}"
    conn.execute(
        "INSERT INTO basket_probability_bridge_history"
        " (station_id, event_date, history_id, envelope_json,"
        "  decision_at_utc, stage) VALUES (?,?,?,?,?,?)",
        ("ZBAA", day, history_id, "{}", decided, "P0"),
    )
    for bucket, lower, upper, probability in (
        ("29°C", 28.5, 29.5, 1.0 - warm),
        ("30°C", 29.5, 30.5, warm),
    ):
        conn.execute(
            "INSERT INTO basket_probability_bridge_bucket_probability"
            " VALUES (?,?,?,?,?)",
            (history_id, bucket, lower, upper, probability),
        )


def test_the_bound_reaches_the_lane_and_its_inner_first_decision() -> None:
    """Bounded, the lane keeps the first decision THIS arithmetic made.

    The bound has to reach the inner MIN() as well.  Applied only to the outer
    query it would select on the retired decision's timestamp and then find no
    row at it, dropping the station-day entirely instead of moving to the
    first surviving one.
    """

    conn = sqlite3.connect(":memory:")
    _evidence_lanes(conn)
    _day(conn, "2026-08-20", "2026-08-20T02:00:00Z", 0.10)
    _day(conn, "2026-08-20", "2026-08-20T09:00:00Z", 0.70)
    truth = {("ZBAA", "2026-08-20"): 30.0}

    unbounded = store.serving_lane_by_day(conn, truth, stage="P0")
    # The retired 02:00 decision: 0.10 on the bucket that settled.
    assert sorted(unbounded["2026-08-20"][0]) == [(0.10, 1), (0.90, 0)]

    bounded = store.serving_lane_by_day(
        conn, truth, stage="P0", min_decision_at_utc="2026-08-20T06:00:00Z"
    )
    # The station-day survives, and it is the 09:00 decision, not dropped and
    # not the retired 02:00 one.
    claims = sorted(bounded["2026-08-20"][0])
    assert [hit for _claim, hit in claims] == [0, 1]
    assert [round(claim, 6) for claim, _hit in claims] == [0.3, 0.7]


def test_the_bound_excludes_the_days_it_names() -> None:
    conn = sqlite3.connect(":memory:")
    _evidence_lanes(conn)
    for day in ("2026-08-10", "2026-08-11", "2026-08-20"):
        _day(conn, day, f"{day}T09:00:00Z", 0.70)
    truth = {("ZBAA", day): 30.0 for day in
             ("2026-08-10", "2026-08-11", "2026-08-20")}
    assert len(store.serving_lane_by_day(conn, truth, stage="P0")) == 3
    bounded = store.serving_lane_by_day(
        conn, truth, stage="P0", min_decision_at_utc="2026-08-19T00:00:00Z"
    )
    assert sorted(bounded) == ["2026-08-20"]


def test_the_fit_defaults_its_bound_to_the_served_arithmetic_anchor() -> None:
    """Not a style check: a bound the fit accepts and drops fits unbounded."""

    import inspect

    signature = inspect.signature(store.fit_band_reliability_table)
    assert "min_decision_at_utc" in signature.parameters
    assert signature.parameters["min_decision_at_utc"].default is None
    source = inspect.getsource(store.fit_band_reliability_table)
    assert "served_arithmetic_anchor" in source
    assert "min_decision_at_utc=evidence_floor" in source


def test_a_starved_stage_holds_its_table_instead_of_blanking_it() -> None:
    """The consequence of the bound, and the reason it is safe to add.

    ``refit_all_stages`` activates whatever the fit returns.  With too few
    days the fit used to answer identity, so a stage whose evidence window
    shortened -- which the new bound can do by weeks in one step -- would have
    thrown away a fitted table on the next nightly sweep.
    """

    conn = sqlite3.connect(":memory:")
    _evidence_lanes(conn)
    store.ensure_band_table_schema(conn)
    incumbent = (
        ((0.0, 1.0), 1.0), ((1.0, 2.0), 1.0), ((2.0, 3.0), 1.0),
        ((3.0, 4.0), 1.0), ((4.0, 99.0), 0.75),
    )
    store.persist_and_activate_band_table(
        conn,
        stage="P0",
        axis=store.BAND_AXIS_CENTRE_DISTANCE,
        factors=incumbent,
        evidence={"schema": "band_reliability_fit_evidence_v2", "basis": {}},
        serving_day_count=20,
    )
    fitted = store.fit_band_reliability_table(
        conn, stage="P0", axis=store.BAND_AXIS_CENTRE_DISTANCE,
        bootstrap_samples=8, min_decision_at_utc="",
    )
    assert fitted["accumulating"] is True
    assert dict(fitted["factors"])[(4.0, 99.0)] == 0.75
    assert (
        fitted["evidence"]["basis"]["[4.0, 99.0]"]
        == "accumulating_evidence:held_incumbent"
    )


def test_a_starved_stage_on_a_new_axis_starts_at_identity() -> None:
    """An axis move is a decision somebody made; identity is its honest start.

    Holding here would serve claim-keyed factors through a distance lookup,
    which is the mis-keying charter 105 exists to forbid.
    """

    conn = sqlite3.connect(":memory:")
    _evidence_lanes(conn)
    store.ensure_band_table_schema(conn)
    store.persist_and_activate_band_table(
        conn,
        stage="P0",
        axis=store.BAND_AXIS_CENTRE_DISTANCE,
        factors=(
            ((0.0, 1.0), 1.0), ((1.0, 2.0), 1.0), ((2.0, 3.0), 1.0),
            ((3.0, 4.0), 1.0), ((4.0, 99.0), 0.75),
        ),
        evidence={"schema": "band_reliability_fit_evidence_v2", "basis": {}},
        serving_day_count=20,
    )
    fitted = store.fit_band_reliability_table(
        conn, stage="P0", axis=store.BAND_AXIS_CLAIM,
        bootstrap_samples=8, min_decision_at_utc="",
    )
    assert fitted["accumulating"] is True
    assert set(dict(fitted["factors"]).values()) == {1.0}
