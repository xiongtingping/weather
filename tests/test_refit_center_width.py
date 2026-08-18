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
    def test_every_stage_is_fitted_and_every_stage_is_served(self) -> None:
        """P0 joined once its correction became part of the gated math.

        Before the pin it was fitted for evidence only: hanging a scalar
        after an OOS-gated artifact would serve math the gate never scored.
        Now the holdout scores it and the artifact records which version,
        so the stage is served like the other two.
        """

        conn = _connection()
        stages = refit_all_center_width(conn, commit=False)
        self.assertEqual(sorted(stages), sorted(CENTER_WIDTH_STAGES))
        for stage in CENTER_WIDTH_STAGES:
            self.assertTrue(stages[stage]["served_stage"])
        self.assertEqual(
            sorted(CENTER_WIDTH_SERVED_STAGES), sorted(CENTER_WIDTH_STAGES)
        )

    def test_report_only_activates_nothing(self) -> None:
        conn = _connection()
        refit_all_center_width(conn, commit=False)
        for stage in CENTER_WIDTH_STAGES:
            self.assertIsNone(load_center_width(conn, stage=stage))

    def test_a_starved_p0_accumulates_rather_than_activating(self) -> None:
        """The pin makes P0 servable; evidence still decides whether it is."""

        conn = _connection()
        stages = refit_all_center_width(conn, commit=True)
        self.assertEqual(stages["P0"]["outcome"], "ACCUMULATING")
        self.assertIsNone(load_center_width(conn, stage="P0"))

    def test_a_starved_served_stage_stays_identity(self) -> None:
        conn = _connection()
        stages = refit_all_center_width(conn, commit=True)
        self.assertEqual(stages["P1"]["outcome"], "ACCUMULATING")
        self.assertIsNone(load_center_width(conn, stage="P1"))



class P0PinContractTest(unittest.TestCase):
    """What the gate scored is what serving reproduces, or nothing is served.

    P0's distribution is compiled against an OOS-gated artifact.  The stage
    scalars could only join it by becoming part of the gated math: the
    holdout scores them, the artifact records the version scored, and serving
    applies that version or fails closed.  These pin the three halves of that
    contract against each other.
    """

    def test_the_tag_and_the_pin_are_read_back_from_one_string(self) -> None:
        from bayes_basket_target.center_width_store import (
            CENTER_WIDTH_METHOD_TAG,
            pinned_center_width_sha,
        )

        method = (
            "train_rmse_inverse_family_capped_v1::PRODUCTION_NO_LIVE"
            "::band_reliability_v1::band_table_sha256=" + "a" * 64
            + "::" + CENTER_WIDTH_METHOD_TAG
            + "::center_width_sha256=" + "b" * 64
        )
        self.assertIn(CENTER_WIDTH_METHOD_TAG, method)
        self.assertEqual(pinned_center_width_sha(method), "b" * 64)

    def test_an_untagged_artifact_names_no_version(self) -> None:
        """Artifacts built before this existed keep serving their own math."""

        from bayes_basket_target.center_width_store import (
            CENTER_WIDTH_METHOD_TAG,
            pinned_center_width_sha,
        )

        legacy = (
            "train_rmse_inverse_family_capped_v1::PRODUCTION_NO_LIVE"
            "::band_reliability_v1"
        )
        self.assertNotIn(CENTER_WIDTH_METHOD_TAG, legacy)
        self.assertIsNone(pinned_center_width_sha(legacy))

    def test_a_pinned_version_that_is_gone_fails_closed(self) -> None:
        """Serving an approximation of gated math is what the pin forbids."""

        from bayes_basket_target.contracts import ContractError

        conn = _connection()
        with self.assertRaises(ContractError):
            load_center_width(conn, stage="P0", table_sha256="c" * 64)

    def test_the_serving_path_reads_the_tag_and_the_pin(self) -> None:
        """Both halves must be present in the compiler, not just the tag."""

        source = (
            ROOT / "bayes_basket_target" / "native_ensemble_p0.py"
        ).read_text()
        self.assertIn("CENTER_WIDTH_METHOD_TAG", source)
        self.assertIn("pinned_center_width_sha", source)
        self.assertIn("apply_center_width_calibration", source)

    def test_the_gate_pins_what_it_scored(self) -> None:
        source = (
            ROOT / "bayes_basket_target" / "native_ensemble_oos.py"
        ).read_text()
        self.assertIn("center_width_sha256", source)
        self.assertIn("CENTER_WIDTH_METHOD_TAG", source)
        self.assertIn("apply_center_width_calibration", source)

if __name__ == "__main__":
    unittest.main()
