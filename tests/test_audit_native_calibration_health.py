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
    BRIER_TOLERANCE_MAX_LOSS,
    BRIER_TOLERANCE_MIN_GAIN,
    POINT_FORECAST_SYSTEM_IDS,
    check_calibration_regression,
    check_gate_binding_condition,
    check_spread_bound,
    check_table_freshness,
    gate_condition_rates,
    murphy_decomposition,
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



class GateConditionRatesTest(unittest.TestCase):
    """Which condition is doing the rejecting, and when that stops being NLL."""

    @staticmethod
    def _conn_with(cases):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE calibration_cross_native_ensemble_oos_evaluation ("
            " oos_metrics_json TEXT, climatology_baseline_metrics_json TEXT,"
            " evaluated_at_utc TEXT)"
        )
        import json as _json

        for crps, brier, nll in cases:
            conn.execute(
                "INSERT INTO"
                " calibration_cross_native_ensemble_oos_evaluation"
                " VALUES (?,?,?)",
                (
                    _json.dumps(
                        {"crps": crps, "brier": brier, "nll": nll}
                    ),
                    _json.dumps(
                        {"crps": 1.0, "brier": 1.0, "nll": 1.0}
                    ),
                    "2026-08-17T00:00:00Z",
                ),
            )
        return conn

    def test_only_contested_candidates_are_counted(self) -> None:
        """The channel only ever runs on a candidate that lost Brier and won
        CRPS; anything else is decided by the strict path."""

        conn = self._conn_with([
            (0.9, 1.1, 0.9),   # contested
            (0.9, 0.9, 0.9),   # strict pass, not contested
            (1.1, 1.1, 1.1),   # loses both, not contested
        ])
        rates = gate_condition_rates(conn, since="2026-01-01T00:00:00Z")
        self.assertEqual(rates["contested_count"], 1)

    def test_each_condition_is_reported_separately(self) -> None:
        """Reading Brier and CRPS alone made the channel look punitive; the
        point of the breakdown is to show which one actually binds."""

        conn = self._conn_with([
            # Brier loss inside tolerance, CRPS wins big, NLL loses.
            (0.90, 1.01, 1.20),
            (0.90, 1.01, 1.20),
        ])
        rates = gate_condition_rates(conn, since="2026-01-01T00:00:00Z")
        self.assertEqual(rates["brier_loss_within_tolerance_share"], 1.0)
        self.assertEqual(rates["crps_gain_sufficient_share"], 1.0)
        self.assertEqual(rates["nll_gain_sufficient_share"], 0.0)
        self.assertEqual(rates["admitted_share"], 0.0)
        self.assertEqual(rates["blocked_by_brier_alone_share"], 0.0)

    def test_brier_alone_blocking_is_named_as_such(self) -> None:
        conn = self._conn_with([
            # CRPS and NLL both clear the margin; only the Brier loss is out.
            (0.90, 1.30, 0.90),
        ])
        rates = gate_condition_rates(conn, since="2026-01-01T00:00:00Z")
        self.assertEqual(rates["blocked_by_brier_alone_share"], 1.0)

    def test_a_thin_sample_raises_nothing(self) -> None:
        """A fortnight of weather is not a property of the gate."""

        conn = self._conn_with([(0.90, 1.30, 0.90)])
        rates = gate_condition_rates(conn, since="2026-01-01T00:00:00Z")
        self.assertEqual(check_gate_binding_condition(rates), [])

    def test_it_alarms_only_once_brier_is_the_sole_gatekeeper(self) -> None:
        self.assertEqual(
            check_gate_binding_condition({
                "contested_count": 500,
                "blocked_by_brier_alone_share": 0.10,
            }),
            [],
        )
        raised = check_gate_binding_condition({
            "contested_count": 500,
            "blocked_by_brier_alone_share": 0.80,
            "nll_gain_sufficient_share": 0.9,
            "median_brier_loss": 0.05,
            "median_crps_gain": 0.12,
        })
        self.assertEqual(len(raised), 1)
        self.assertEqual(
            raised[0]["check"],
            "tolerance_channel_now_gated_by_brier_alone",
        )

    def test_the_thresholds_match_the_gate_that_actually_runs(self) -> None:
        """Duplicated constants that drift describe a gate nobody runs."""

        from bayes_basket_target import native_ensemble_oos as oos

        self.assertEqual(
            BRIER_TOLERANCE_MAX_LOSS,
            oos.BRIER_TOLERANCE_MAX_RELATIVE_LOSS,
        )
        self.assertEqual(
            BRIER_TOLERANCE_MIN_GAIN,
            oos.BRIER_TOLERANCE_MIN_RELATIVE_GAIN,
        )


class MurphyDecompositionTest(unittest.TestCase):
    """BS = REL - RES + UNC, and which of the three anyone can act on."""

    @staticmethod
    def _conn(days):
        import json as _json

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE basket_probability_envelope ("
            " stage TEXT, state TEXT, station_id TEXT, event_date TEXT,"
            " bucket_ordinals_json TEXT, probabilities_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE calibration_station_daily_high_truth ("
            " station_id TEXT, local_date TEXT, official_tmax_c REAL,"
            " qc_status TEXT)"
        )
        keys = [f"{20 + i}°C" for i in range(11)]
        ordinals = {key: index for index, key in enumerate(keys)}
        for index, (probabilities, truth) in enumerate(days):
            day = f"2026-08-{index % 28 + 1:02d}"
            station = f"S{index // 28:03d}"
            conn.execute(
                "INSERT INTO basket_probability_envelope VALUES"
                " (?,?,?,?,?,?)",
                (
                    "P0", "COMMITTED", station, day,
                    _json.dumps(ordinals),
                    _json.dumps(dict(zip(keys, probabilities))),
                ),
            )
            conn.execute(
                "INSERT INTO calibration_station_daily_high_truth VALUES"
                " (?,?,?,?)",
                (station, day, truth, "ACCEPTED"),
            )
        return conn

    def test_the_three_terms_reconstruct_the_brier(self) -> None:
        """Any gap means the split is not of the score it claims to split."""

        import random

        rng = random.Random(3)
        days = []
        for _index in range(400):
            weights = [rng.random() for _ in range(11)]
            total = sum(weights)
            probabilities = [w / total for w in weights]
            landed = rng.randrange(11)
            days.append((probabilities, 20.0 + landed))
        murphy = murphy_decomposition(
            self._conn(days), since="2026-01-01T00:00:00Z"
        )
        self.assertTrue(murphy["estimable"])
        # Binned reliability is exact only when every forecast in a bin
        # carries the same probability, so the reported residual is the
        # within-bin spread.  What must hold is that it stays negligible
        # against the score being split.
        self.assertAlmostEqual(
            murphy["brier"],
            murphy["reliability"]
            - murphy["resolution"]
            + murphy["uncertainty"]
            + murphy["reconstruction_residual"],
            places=6,
        )
        self.assertLess(
            murphy["reconstruction_residual"] / murphy["brier"], 0.02
        )

    def test_a_forecast_that_never_varies_has_no_resolution(self) -> None:
        """Climatology's shape: perfectly calibrated, tells no days apart,
        and so its Brier is the uncertainty term alone."""

        flat = [1.0 / 11] * 11
        days = [(flat, 20.0 + index % 11) for index in range(400)]
        murphy = murphy_decomposition(
            self._conn(days), since="2026-01-01T00:00:00Z"
        )
        self.assertLess(murphy["resolution_share_of_uncertainty"], 0.01)
        self.assertAlmostEqual(
            murphy["brier"], murphy["uncertainty"], places=2
        )

    def test_a_sample_too_thin_declines(self) -> None:
        murphy = murphy_decomposition(
            self._conn([([1.0 / 11] * 11, 20.0)]),
            since="2026-01-01T00:00:00Z",
        )
        self.assertFalse(murphy["estimable"])

    def test_only_reliability_raises_an_alarm(self) -> None:
        """Resolution is bounded by upstream information and is nobody's bug;
        reliability is entirely the calibration layers' doing."""

        self.assertEqual(
            check_calibration_regression({
                "estimable": True,
                "reliability_share_of_uncertainty": 0.006,
                "resolution_share_of_uncertainty": 0.02,
            }),
            [],
        )
        raised = check_calibration_regression({
            "estimable": True,
            "stage": "P0",
            "reliability_share_of_uncertainty": 0.20,
            "resolution_share_of_uncertainty": 0.09,
            "brier_skill_score": -0.11,
            "station_day_count": 400,
        })
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0]["check"], "served_reliability_degraded")

    def test_an_unestimable_split_raises_nothing(self) -> None:
        self.assertEqual(
            check_calibration_regression({"estimable": False}), []
        )

if __name__ == "__main__":
    unittest.main()
