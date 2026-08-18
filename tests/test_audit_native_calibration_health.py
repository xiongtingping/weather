"""The health check must not drown its own signal.

Every one of the 32 findings this check produced on 2026-08-18 was noise, and
the noise came from three separate mismatches between what it compared and
what the pipeline actually does.  A check that cries wolf four times for
every real finding is a check nobody will believe on the day it is right, so
each mismatch is pinned here.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from audit_native_calibration_health import (  # noqa: E402
    POINT_FORECAST_SYSTEM_IDS,
    check_spread_bound,
    check_table_freshness,
)


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_cross_native_ensemble_selection_evidence ("
        " station_id TEXT, season_bin TEXT, lead_bucket TEXT,"
        " run_cycle TEXT, train_end_date TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_cross_native_ensemble_oos_evaluation ("
        " station_id TEXT, season_bin TEXT, lead_bucket TEXT,"
        " run_cycle TEXT, train_end_date TEXT, oos_state TEXT,"
        " evaluated_at_utc TEXT)"
    )
    return conn


def _live(system: str, train_end: str, spread: str = "{}") -> dict:
    return {
        ("KLGA", system, "JJA", "h24_48", "18Z"): (
            "KLGA", system, "JJA", "h24_48", "18Z", 1,
            spread, "{}", train_end,
        )
    }


class SpreadScaleTest(unittest.TestCase):
    def test_it_judges_by_sigma_not_by_rmse(self) -> None:
        """RMSE carries the bias the centre shift already removed.

        Reading it here demanded back exactly that width and reported 127 of
        510 live scopes as too narrow on the day the fit moved to sigma.
        """

        spread = (
            '{"spread_factor": 2.0, "train_rmse_c": 5.0,'
            ' "train_residual_sigma_c": 2.0, "train_mean_spread_c": 1.0}'
        )
        self.assertEqual(
            check_spread_bound(_live("NOAA_GEFS_05", "2026-08-10", spread)),
            [],
        )

    def test_an_artifact_without_sigma_is_judged_by_its_own_rule(self) -> None:
        """Rows fitted before the change really were sized by RMSE."""

        spread = (
            '{"spread_factor": 2.0, "train_rmse_c": 5.0,'
            ' "train_mean_spread_c": 1.0}'
        )
        findings = check_spread_bound(
            _live("NOAA_GEFS_05", "2026-08-10", spread)
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "spread_below_evidence")

    def test_a_genuinely_narrow_spread_is_still_caught(self) -> None:
        spread = (
            '{"spread_factor": 1.0, "train_rmse_c": 4.0,'
            ' "train_residual_sigma_c": 3.5, "train_mean_spread_c": 1.0}'
        )
        findings = check_spread_bound(
            _live("NOAA_GEFS_05", "2026-08-10", spread)
        )
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0]["evidence_spread_factor"], 3.5)


class TableFreshnessTest(unittest.TestCase):
    def _seed(
        self,
        conn: sqlite3.Connection,
        *,
        evidence_cycle: str,
        evidence_end: str,
        evaluated_end: str,
        state: str = "PRODUCTION_NO_LIVE_OOS_PASS",
    ) -> None:
        conn.execute(
            "INSERT INTO"
            " calibration_cross_native_ensemble_selection_evidence"
            " VALUES (?,?,?,?,?)",
            ("KLGA", "JJA", "h24_48", evidence_cycle, evidence_end),
        )
        conn.execute(
            "INSERT INTO calibration_cross_native_ensemble_oos_evaluation"
            " VALUES (?,?,?,?,?,?,?)",
            (
                "KLGA", "JJA", "h24_48", evidence_cycle, evaluated_end,
                state, "2026-08-17T13:00:00Z",
            ),
        )

    def test_a_point_forecast_cannot_make_the_blend_look_stale(self) -> None:
        """TAF and METAR advance faster and never contest a P0 seat.

        Counting them in the correction-side maximum compared the blend
        against systems that are not in it -- 17 of the 32 findings.
        """

        conn = _connection()
        self._seed(
            conn,
            evidence_cycle="MIXED_NATIVE_CYCLES",
            evidence_end="2026-08-07",
            evaluated_end="2026-08-07",
        )
        live = _live(POINT_FORECAST_SYSTEM_IDS[0], "2026-08-14")
        self.assertEqual(check_table_freshness(conn, live), [])

    def test_a_retired_cycle_label_does_not_freeze_the_scope(self) -> None:
        """A cohort seating mixed cycles is labelled MIXED; the activations
        under it keep concrete cycles, so keying on cycle matched a live
        correction against a retired label's last evidence."""

        conn = _connection()
        self._seed(
            conn,
            evidence_cycle="18Z",
            evidence_end="2026-08-03",
            evaluated_end="2026-08-03",
        )
        self._seed(
            conn,
            evidence_cycle="MIXED_NATIVE_CYCLES",
            evidence_end="2026-08-07",
            evaluated_end="2026-08-07",
        )
        live = _live("NOAA_GEFS_05", "2026-08-10")
        self.assertEqual(check_table_freshness(conn, live), [])

    def test_the_shared_holdout_offset_is_not_staleness(self) -> None:
        """A cross evaluation reserves a holdout shared across its members,
        so its train ends earlier than any one member's.  Comparing the two
        measured that offset on every healthy scope."""

        conn = _connection()
        self._seed(
            conn,
            evidence_cycle="MIXED_NATIVE_CYCLES",
            evidence_end="2026-08-07",
            evaluated_end="2026-08-07",
        )
        live = _live("NOAA_GEFS_05", "2026-08-14")
        self.assertEqual(check_table_freshness(conn, live), [])

    def test_weights_lagging_their_own_evaluations_are_caught(self) -> None:
        """The failure the check exists for: evaluations arrive, weights do
        not turn over, and the state does not explain why."""

        conn = _connection()
        self._seed(
            conn,
            evidence_cycle="MIXED_NATIVE_CYCLES",
            evidence_end="2026-07-28",
            evaluated_end="2026-08-10",
        )
        live = _live("NOAA_GEFS_05", "2026-08-10")
        findings = check_table_freshness(conn, live)
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0]["check"], "weight_table_behind_correction_table"
        )
        self.assertEqual(findings[0]["gap_days"], 13)

    def test_a_rejection_is_reported_as_its_own_thing(self) -> None:
        """Frozen because nothing passed the gate is by design, and reading
        it as the same alarm hides the one that is not."""

        conn = _connection()
        self._seed(
            conn,
            evidence_cycle="MIXED_NATIVE_CYCLES",
            evidence_end="2026-07-28",
            evaluated_end="2026-08-10",
            state="OOS_REJECTED",
        )
        live = _live("NOAA_GEFS_05", "2026-08-10")
        findings = check_table_freshness(conn, live)
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0]["check"], "weight_table_frozen_by_rejection"
        )


if __name__ == "__main__":
    unittest.main()
