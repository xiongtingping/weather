"""The centre/width layer refits, and cannot be outvoted by retired math.

The store held the search, the forward admission rule, the persistence and
the journal, and nothing anywhere called any of it: the scalars in production
were one hand fit that could never refresh.  A layer whose entire job is to
say how wrong the served distribution is has to run again when the served
distribution changes, and it has to be able to ignore the days it no longer
describes -- otherwise it re-derives whatever it had been compensating for.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.center_width_store import (  # noqa: E402
    CENTER_WIDTH_SERVED_STAGES,
    CENTER_WIDTH_STAGES,
    ensure_center_width_schema,
    fit_center_width,
    load_center_width,
    refit_all_center_width,
    serving_cells_by_day,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history ("
        " stage TEXT, station_id TEXT, event_date TEXT,"
        " decision_at_utc TEXT, envelope_json TEXT,"
        " settlement_timezone TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth ("
        " station_id TEXT, local_date TEXT, official_tmax_c REAL,"
        " revision_no INTEGER, qc_status TEXT)"
    )
    return conn


def _envelope() -> str:
    """The shape the store actually scores: probabilities, ordinals, and the
    bucket intervals its lineage carries.  A cell missing any of the three is
    skipped, so a thinner fixture would test the skip and not the window."""

    keys = ["20", "21", "22"]
    intervals = {
        "20": [None, 20.5],
        "21": [20.5, 21.5],
        "22": [21.5, None],
    }
    return json.dumps({
        "probabilities": {"20": 0.3, "21": 0.4, "22": 0.3},
        "bucket_ordinals": {key: index for index, key in enumerate(keys)},
        "lineage": {"bucket_intervals_c_json": json.dumps(intervals)},
    })


class EvidenceWindowTest(unittest.TestCase):
    def test_the_window_excludes_days_below_the_bound(self) -> None:
        """A day the served arithmetic no longer describes must not vote."""

        conn = _connection()
        rows = [
            (
                "P1", "KLGA", day, f"{day}T12:00:00Z",
                _envelope(), "America/New_York",
            )
            for day in ("2026-08-10", "2026-08-11", "2026-08-17")
        ]
        conn.executemany(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?,?,?,?)",
            rows,
        )
        truth = {
            ("KLGA", "2026-08-10"): 20.0,
            ("KLGA", "2026-08-11"): 21.0,
            ("KLGA", "2026-08-17"): 20.0,
        }
        unbounded = serving_cells_by_day(conn, truth, stage="P1")
        bounded = serving_cells_by_day(
            conn, truth, stage="P1", min_event_date="2026-08-17"
        )
        self.assertEqual(len(unbounded), 3)
        self.assertEqual(sorted(bounded), ["2026-08-17"])

    def test_the_bound_is_absent_by_default(self) -> None:
        """Existing callers keep scoring every day on record."""

        conn = _connection()
        conn.execute(
            "INSERT INTO basket_probability_bridge_history VALUES"
            " (?,?,?,?,?,?)",
            (
                "P1", "KLGA", "2026-08-01", "2026-08-01T12:00:00Z",
                _envelope(), "America/New_York",
            ),
        )
        cells = serving_cells_by_day(
            conn, {("KLGA", "2026-08-01"): 20.0}, stage="P1"
        )
        self.assertEqual(sorted(cells), ["2026-08-01"])

    def test_the_bound_reaches_the_fit_and_its_evidence(self) -> None:
        conn = _connection()
        fitted = fit_center_width(
            conn, stage="P1", min_event_date="2026-08-18"
        )
        self.assertEqual(
            fitted["evidence"]["min_event_date"], "2026-08-18"
        )
        # No days at all is the accumulating case, not an error: the layer
        # says it has nothing to go on rather than inventing a scalar.
        self.assertTrue(fitted["accumulating"])
        self.assertEqual(fitted["delta_c"], 0.0)
        self.assertEqual(fitted["width_lambda"], 1.0)


class RefitAllStagesTest(unittest.TestCase):
    def test_every_stage_is_fitted_including_the_unserved_one(self) -> None:
        """P0 is fitted for evidence though nothing serves it.

        Its distribution is produced against a pinned artifact, so attaching
        a scalar the serving gate never saw is exactly the unbound-pointer
        failure; the fit still runs so the evidence exists when pinning does.
        """

        conn = _connection()
        stages = refit_all_center_width(conn, commit=False)
        self.assertEqual(sorted(stages), sorted(CENTER_WIDTH_STAGES))
        self.assertFalse(stages["P0"]["served_stage"])
        for stage in CENTER_WIDTH_SERVED_STAGES:
            self.assertTrue(stages[stage]["served_stage"])

    def test_report_only_activates_nothing(self) -> None:
        conn = _connection()
        refit_all_center_width(conn, commit=False)
        for stage in CENTER_WIDTH_STAGES:
            self.assertIsNone(load_center_width(conn, stage=stage))

    def test_an_unserved_stage_is_journalled_as_not_wired(self) -> None:
        conn = _connection()
        stages = refit_all_center_width(conn, commit=True)
        self.assertEqual(stages["P0"]["outcome"], "NOT_WIRED")
        self.assertIsNone(load_center_width(conn, stage="P0"))

    def test_a_starved_served_stage_stays_identity(self) -> None:
        conn = _connection()
        stages = refit_all_center_width(conn, commit=True)
        self.assertEqual(stages["P1"]["outcome"], "ACCUMULATING")
        self.assertIsNone(load_center_width(conn, stage="P1"))



class RadiationWidthCovariateShadowTest(unittest.TestCase):
    """Fitted beside the served pair, and never allowed to become it."""

    def test_an_unlabelled_cell_keeps_the_flat_width(self) -> None:
        """No radiation on record is not the same as an average day."""

        from bayes_basket_target.center_width_store import _effective_lambda

        cell = {"radiation_z": None}
        self.assertEqual(
            _effective_lambda(cell, width_lambda=0.9, radiation_slope=0.06),
            0.9,
        )

    def test_a_bright_day_widens_and_a_dull_day_narrows(self) -> None:
        from bayes_basket_target.center_width_store import _effective_lambda

        bright = _effective_lambda(
            {"radiation_z": 2.0}, width_lambda=0.9, radiation_slope=0.06
        )
        dull = _effective_lambda(
            {"radiation_z": -2.0}, width_lambda=0.9, radiation_slope=0.06
        )
        self.assertGreater(bright, 0.9)
        self.assertLess(dull, 0.9)

    def test_the_covariate_cannot_leave_the_served_width_bounds(self) -> None:
        """A shadow that proposes a width serving would refuse is not
        evidence about anything servable."""

        from bayes_basket_target.center_width_store import (
            MAX_WIDTH_LAMBDA,
            MIN_WIDTH_LAMBDA,
            _effective_lambda,
        )

        for z in (-8.0, 8.0):
            value = _effective_lambda(
                {"radiation_z": z}, width_lambda=1.0, radiation_slope=0.2
            )
            self.assertGreaterEqual(value, MIN_WIDTH_LAMBDA)
            self.assertLessEqual(value, MAX_WIDTH_LAMBDA)

    def test_a_zero_slope_is_exactly_the_flat_width(self) -> None:
        from bayes_basket_target.center_width_store import _effective_lambda

        self.assertEqual(
            _effective_lambda(
                {"radiation_z": 1.7}, width_lambda=0.83, radiation_slope=0.0
            ),
            0.83,
        )

    def test_the_shadow_never_reaches_the_activated_table(self) -> None:
        """Whatever the slope, what persists is two scalars."""

        conn = _connection()
        refit_all_center_width(conn, commit=True)
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(calibration_center_width_table)"
            )
        }
        self.assertNotIn("radiation_slope", columns)

if __name__ == "__main__":
    unittest.main()
