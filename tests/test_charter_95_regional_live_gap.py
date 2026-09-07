"""Charter 95's second feature, and the as-of leak measuring it uncovered.

The feature: correct the curve by today's own error -- how far this run's
value for the hour now in progress sits from what the station just reported.
Built, fitted, measured, and not served: it points the right way for both
lanes and neither reading clears zero.

The leak: measuring it turned up that ``resolve_serving_input``'s run-age test
was one-sided, and that ``knowable_at_utc`` -- the guard that should have made
the age test unnecessary -- was an empty string on 904 backfilled rows.  An
empty string sorts before every timestamp, so those runs read as knowable at
every instant, and 74.1% of NBM and 78.0% of HRRR serving decisions in the
admission window were reading a forecast issued after the decision they were
being used for.  Both halves are fixed here, and both are tested: the guard
because data can go wrong again, the repair because a guard is not a reason to
leave the table lying.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from bayes_basket_target import regional_live_gap as G
from bayes_basket_target import regional_path_prior as R
from bayes_basket_target import regional_point_lane as L

ROOT = Path(__file__).resolve().parents[1]

PATH_ARTIFACT = {
    "schema": R.SCHEMA,
    "cells": {str(i): {"hours_from": 0.0, "hours_to": 1e9, "bias_c": 0.0,
                       "sigma_c": 0.9, "sample_n": 200, "backed_off": False}
              for i in range(10)},
    "stations": {}, "systems": {},
}
GAP_ARTIFACT = {
    "schema": G.SCHEMA,
    "systems": {
        "NCEP_NBM_POINT": {
            "sample_n": 8690, "slope_c_per_c": 0.2083, "gap_bar_c": 0.282,
            "stations": {},
        },
        "THIN_LANE": {"sample_n": 12, "slope_c_per_c": 0.9, "gap_bar_c": 0.0,
                      "stations": {}},
    },
}
# Issued 18:00Z, curve over the local afternoon.
RUN = {
    "ensemble_system_id": "NCEP_NBM_POINT", "run_init_utc": "2026-08-25T18:00:00Z",
    "archive_query_at_utc": "2026-08-25T19:00:00Z",
    "knowable_at_utc": "2026-08-25T19:00:00Z",
    "hourly_c": {"2026-08-25T19:00:00Z": 30.0, "2026-08-25T20:00:00Z": 31.5,
                 "2026-08-25T22:00:00Z": 29.0},
}


# --------------------------------------------------------------------------
# the leak
# --------------------------------------------------------------------------

def test_a_run_issued_after_the_decision_is_not_a_run_that_decision_could_read() -> None:
    """The age test was one-sided, and a negative age passed it.

    ``(as_of - run_init) > 12`` is false for a run thirteen hours in the
    future as surely as for one issued a minute ago.  Measured 2026-09-07 on
    the admission window: NBM 74.1%, HRRR 78.0%, MSM 0.0% -- which is why the
    reading that admitted the MSM survived the fix unchanged.
    """

    kwargs = dict(
        station_id="KATL", event_date="2026-08-25",
        settlement_timezone="America/New_York", artifact=PATH_ARTIFACT,
        systems={"NCEP_NBM_POINT"},
    )
    # 04:54Z on the 25th: the run above is thirteen hours ahead of it.
    assert R.resolve_serving_input(
        [RUN], as_of_utc="2026-08-25T04:54:30Z", **kwargs) is None
    # 19:30Z: issued, published, and an hour and a half old.
    served = R.resolve_serving_input(
        [RUN], as_of_utc="2026-08-25T19:30:00Z", **kwargs)
    assert served is not None
    assert served["remaining_peak_c"] == pytest.approx(31.5)


def test_the_lane_repairs_a_knowable_stamp_it_finds_empty() -> None:
    """904 rows carried '' and every as-of filter waved them through.

    The repair existed but ran only at the instant the column was added, so a
    backfill that raced the migration left its rows behind it.  Asking every
    time costs one indexed lookup, and the alternative is a replay that reads
    the future without saying so.
    """

    conn = sqlite3.connect(":memory:")
    L.ensure_schema(conn)
    conn.execute(
        f"INSERT INTO {L.TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("nbm-test", "NCEP_NBM_POINT", "KATL", "2026-08-25",
         "America/New_York", "2026-08-25T18:00:00Z", 18, 6.0, 31.5, 1, 8, 3,
         "{}", "NOAA_NBM_PDS_S3", "ncep_nbm_core_nearest_cell_v1",
         "2026-09-06T13:47:22Z", "", "s3://x", L.SCHEMA_VERSION, "a" * 64),
    )
    assert conn.execute(
        f"SELECT COUNT(*) FROM {L.TABLE} WHERE knowable_at_utc=''"
    ).fetchone()[0] == 1
    L.ensure_schema(conn)
    assert conn.execute(
        f"SELECT COUNT(*) FROM {L.TABLE} WHERE knowable_at_utc=''"
    ).fetchone()[0] == 0
    knowable, run_init = conn.execute(
        f"SELECT knowable_at_utc, run_init_utc FROM {L.TABLE}"
    ).fetchone()
    assert knowable >= run_init, "nothing is knowable before it is issued"


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


# --------------------------------------------------------------------------
# the feature
# --------------------------------------------------------------------------

def test_the_current_hour_is_this_hour_or_the_next_and_nothing_further() -> None:
    """Serving may not see a sample the fit would have thrown away.

    The lane stores the local peak window, not the whole day.  A wider
    fallback answers a pre-dawn report with the curve's midday value and calls
    the difference a live gap: measured, that fabricated a mean gap near -2C
    where the honest one is +0.35C, and drove the anchor 0.47C cold.
    """

    curve = RUN["hourly_c"]
    assert R.curve_value_at(curve, as_of_utc="2026-08-25T19:20:00Z") == 30.0
    # 21:00 is not on this curve; 22:00 is the next hour.
    assert R.curve_value_at(curve, as_of_utc="2026-08-25T21:10:00Z") == 29.0
    # 17:00 is before the curve starts, and 20:00 is not its next hour.
    assert R.curve_value_at(curve, as_of_utc="2026-08-25T17:10:00Z") is None
    assert R.curve_value_at(curve, as_of_utc="2026-08-25T23:30:00Z") is None


def test_the_correction_is_centred_so_it_never_moves_the_level() -> None:
    """Charter 99's cell bias is already the residual's mean.

    A layer serving ``a + b*gap`` would put its intercept on top of that mean
    and correct it twice -- the fault that sank the historical debias on the
    same morning.  At the fitted centre this layer contributes exactly zero.
    """

    slope = G.resolve_slope(GAP_ARTIFACT, system_id="NCEP_NBM_POINT",
                            systems={"NCEP_NBM_POINT"})
    at_centre, _ = G.correction_c(0.282, slope, enabled=True)
    assert at_centre == pytest.approx(0.0, abs=1e-9)
    warmer, note = G.correction_c(3.282, slope, enabled=True)
    assert warmer == pytest.approx(0.2083 * 3.0, abs=1e-6)
    assert note["regional_live_gap_applied"] == "true"
    colder, _ = G.correction_c(-2.718, slope, enabled=True)
    assert colder == pytest.approx(-0.2083 * 3.0, abs=1e-6)


def test_the_gap_and_the_product_are_both_clipped() -> None:
    slope = G.resolve_slope(GAP_ARTIFACT, system_id="NCEP_NBM_POINT",
                            systems={"NCEP_NBM_POINT"})
    huge, note = G.correction_c(40.0, slope, enabled=True)
    assert float(note["regional_live_gap_clipped_c"]) == pytest.approx(G.MAX_GAP_C)
    assert abs(huge) <= G.MAX_CORRECTION_C
    steep, _ = G.correction_c(
        4.0, {"slope_c_per_c": 5.0, "gap_bar_c": 0.0, "sample_n": 9000,
              "fit_system_id": "X"}, enabled=True)
    assert steep == pytest.approx(G.MAX_CORRECTION_C)


def test_a_thin_lane_and_a_missing_half_are_both_silence() -> None:
    assert G.resolve_slope(GAP_ARTIFACT, system_id="THIN_LANE",
                           systems={"THIN_LANE"}) is None
    assert G.resolve_slope(GAP_ARTIFACT, system_id="NCEP_NBM_POINT") is None, (
        "the admission list is empty until the gate rules"
    )
    assert G.live_gap_c(None, 30.0) is None
    assert G.live_gap_c(31.0, None) is None
    zero, note = G.resolve_correction_for(
        GAP_ARTIFACT, system_id="NCEP_NBM_POINT", latest_observed_c=31.0,
        curve_now_c=None, systems={"NCEP_NBM_POINT"}, enabled=True)
    assert zero == 0.0 and note["regional_live_gap_skip_reason"] == "NO_LIVE_GAP"


def test_a_slope_is_shrunk_toward_no_relationship() -> None:
    pairs = [(float(i % 7) - 3.0, 0.5 * (float(i % 7) - 3.0)) for i in range(20)]
    slope, gap_bar, count = G.shrunk_slope(pairs)
    assert count == 20
    assert gap_bar == pytest.approx(sum(g for g, _ in pairs) / 20)
    assert slope == pytest.approx(0.5 * (20 / (20 + G.SHRINK_K))), (
        "twenty samples buy a twentieth of the slope they claim"
    )
    assert G.shrunk_slope([]) == (0.0, 0.0, 0)


def test_the_layer_is_built_and_fitted_and_not_served() -> None:
    """The reading that set the default, kept where it cannot drift.

    On the admission window with the as-of leak closed, the live gap moves the
    served distribution by -0.0068 nat for the NBM (95% CI -0.018 to +0.004)
    and -0.0144 for the MSM (CI -0.041 to +0.013).  Both point the right way;
    neither clears zero, and the gate's own rule is that straddling zero is
    not admission.
    """

    assert G.ENABLED is False
    assert G.LIVE_GAP_SYSTEMS == frozenset()
    source = (ROOT / "bayes_basket_target" / "regional_live_gap.py").read_text(
        encoding="utf-8")
    assert "+0.294" in source, "the reading that motivated it rides with it"
    # Not wired into production as a *serving layer*: the gate refused it, so
    # this module gets no seat in the compiler and no line in the ledger
    # fingerprint.  The distinction that matters after charter 95 step two:
    # ``regional_live_gap_c`` is a feature name inside the temperature path's
    # regression and does appear in the compiler; ``regional_live_gap`` the
    # module, which multiplies a correction into a served anchor, does not.
    compiler = (ROOT / "bayes_basket_target" / "p1_compiler.py").read_text(
        encoding="utf-8")
    assert "regional_live_gap import" not in compiler
    assert "regional_live_gap." not in compiler
    ledger = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8")
    assert "regional_live_gap" not in ledger


def test_the_fitted_artifact_is_on_disk_and_says_what_it_measured() -> None:
    import json  # noqa: PLC0415

    if not G.DEFAULT_ARTIFACT.exists():
        pytest.skip("artifact not fitted here")
    payload = json.loads(G.DEFAULT_ARTIFACT.read_text(encoding="utf-8"))
    assert payload["schema"] == G.SCHEMA
    nbm = payload["systems"]["NCEP_NBM_POINT"]
    assert nbm["slope_c_per_c"] > 0.1, "a cold model now is a warm day later"
    assert G.replay_curve("2026-09-06") is not None


def test_the_gate_can_ask_about_a_layer_production_has_switched_off() -> None:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    gate = (ROOT / "scripts" / "measure_regional_serving_value.py").read_text(
        encoding="utf-8")
    assert "--live-gap" in gate
    assert "enabled=True" in gate
    served, note = G.resolve_correction_for(
        GAP_ARTIFACT, system_id="NCEP_NBM_POINT", latest_observed_c=33.282,
        curve_now_c=30.0, systems={"NCEP_NBM_POINT"}, enabled=True)
    assert served == pytest.approx(0.2083 * 3.0, abs=1e-6)
    silent, off = G.resolve_correction_for(
        GAP_ARTIFACT, system_id="NCEP_NBM_POINT", latest_observed_c=33.282,
        curve_now_c=30.0)
    assert silent == 0.0 and off["regional_live_gap_skip_reason"] == "DISABLED"
