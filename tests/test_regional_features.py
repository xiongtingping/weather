"""The regional models' two covariates for P1's joint likelihood (charter 150).

What survives of the regional path prior's tests once the prior itself is
retired: the as-of gate a feature must pass (a run issued after the decision
is not a run that decision could read -- measured 2026-09-07, 74.1% of NBM and
78.0% of HRRR decisions in the then admission window were reading a future
run through a one-sided age test and an empty ``knowable_at_utc``), the
lane's repair of that stamp, and the hour the live gap is read against.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import regional_features as R
from bayes_basket_target import regional_point_lane as L

ROOT = Path(__file__).resolve().parents[1]

# Issued 18:00Z, curve over the local afternoon.
RUN = {
    "ensemble_system_id": "NCEP_NBM_POINT", "run_init_utc": "2026-08-25T18:00:00Z",
    "archive_query_at_utc": "2026-08-25T19:00:00Z",
    "knowable_at_utc": "2026-08-25T19:00:00Z",
    "hourly_c": {"2026-08-25T19:00:00Z": 30.0, "2026-08-25T20:00:00Z": 31.5,
                 "2026-08-25T22:00:00Z": 29.0},
}


def test_a_run_issued_after_the_decision_is_not_a_run_that_decision_could_read() -> None:
    """The age test is two-sided, and the knowable stamp is read too."""

    early = R.regional_features_at(
        [RUN], as_of_utc="2026-08-25T04:54:30Z", running_high_c=25.0,
        latest_observed_c=24.0,
    )
    assert early["regional_headroom_c"] is None
    served = R.regional_features_at(
        [RUN], as_of_utc="2026-08-25T19:30:00Z", running_high_c=30.5,
        latest_observed_c=30.4,
    )
    assert served["regional_system_id"] == "NCEP_NBM_POINT"
    assert served["regional_headroom_c"] == pytest.approx(31.5 - 30.5)
    assert served["regional_live_gap_c"] == pytest.approx(30.4 - 30.0)
    # Older than the age limit: nothing known.
    stale = R.regional_features_at(
        [RUN], as_of_utc="2026-08-26T07:00:01Z", running_high_c=30.5,
        latest_observed_c=30.4,
    )
    assert stale["regional_headroom_c"] is None


def test_the_lane_repairs_a_knowable_stamp_it_finds_empty() -> None:
    """904 rows carried '' and every as-of filter waved them through."""

    conn = sqlite3.connect(":memory:")
    L.ensure_schema(conn)
    conn.execute(
        f"INSERT INTO {L.TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("nbm-test", "NCEP_NBM_POINT", "KATL", "2026-08-25",
         "America/New_York", "2026-08-25T18:00:00Z", 18, 6.0, 31.5, 1, 8, 3,
         "{}", "NOAA_NBM_PDS_S3", "ncep_nbm_core_nearest_cell_v1",
         "2026-09-06T13:47:22Z", "", "s3://x", L.SCHEMA_VERSION, "a" * 64),
    )
    L.ensure_schema(conn)
    knowable, run_init = conn.execute(
        f"SELECT knowable_at_utc, run_init_utc FROM {L.TABLE}"
    ).fetchone()
    assert knowable and knowable >= run_init, "nothing is knowable before it is issued"


def test_the_live_table_carries_no_stamp_earlier_than_its_own_run() -> None:
    db = ROOT / "data" / "bayes_basket_target_ssot.db"
    if not db.exists():
        pytest.skip("no local ssot database")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (L.TABLE,)
    ).fetchone()
    if not present:
        pytest.skip("lane table not built here")
    assert conn.execute(
        f"SELECT COUNT(*) FROM {L.TABLE}"
        f" WHERE knowable_at_utc='' OR knowable_at_utc<run_init_utc"
    ).fetchone()[0] == 0


def test_the_current_hour_is_this_hour_or_the_next_and_nothing_further() -> None:
    """A wider fallback answers a pre-dawn report with the curve's midday
    value and calls the difference a live gap."""

    curve = RUN["hourly_c"]
    assert R.curve_value_at(curve, as_of_utc="2026-08-25T19:20:00Z") == 30.0
    assert R.curve_value_at(curve, as_of_utc="2026-08-25T21:10:00Z") == 29.0
    assert R.curve_value_at(curve, as_of_utc="2026-08-25T17:10:00Z") is None
    assert R.curve_value_at(curve, as_of_utc="2026-08-25T23:30:00Z") is None


def test_the_retired_path_prior_is_gone() -> None:
    """Charter 150: the regional curves reach P1 only as these covariates."""

    package = ROOT / "bayes_basket_target"
    for name in ("regional_path_prior.py", "regional_live_gap.py",
                 "metar_ceiling.py", "four_layer_shadow.py"):
        assert not (package / name).exists(), name
