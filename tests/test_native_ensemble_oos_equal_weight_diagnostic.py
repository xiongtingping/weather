"""The three-way skill matrix: climatology, equal weight, learned weights.

The gate asks one question -- does the assembled distribution beat
climatology -- and a pass silently implies two more it never tested: that the
weather models carry information, and that our weighting of them beats simply
averaging what arrived.  Either can be false while the gate passes.  These
tests pin the decomposition that separates them, and pin it as a *diagnostic*:
metric bytes that reach the selection identity rebind every activation pointer
the P0 compile validates against, which is how the envelope pipeline went
silent the last time scores moved into that sha.
"""

from __future__ import annotations

import json
import math
import unittest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.native_ensemble_oos import (
    EQUAL_WEIGHT_DIAGNOSTIC_STORED_AS,
    expanded_equal_weight_diagnostic,
    stored_equal_weight_diagnostic,
    _equal_weight_diagnostic,
    _evaluation_identity,
    _selection_identity,
)


CLIMATE = {"crps": 1.91, "nll": 2.44, "brier": 0.92}
EQUAL = {"crps": 1.02, "nll": 1.98, "brier": 0.83}
LEARNED = {"crps": 0.8428, "nll": 1.8579, "brier": 0.794}


def _diagnostic(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "climate_metrics": CLIMATE,
        "equal_weight_metrics": EQUAL,
        "learned_metrics": LEARNED,
        "available_system_ids": [
            "ECCC_GEM_GLOBAL_ENS",
            "ECMWF_IFS_ENS_OPEN_PF50",
            "NOAA_GEFS_05",
        ],
        "seated_system_ids": [
            "ECMWF_IFS_ENS_OPEN_PF50",
            "NOAA_GEFS_05",
        ],
        "seated_weights": {
            "ECMWF_IFS_ENS_OPEN_PF50": 0.55,
            "NOAA_GEFS_05": 0.45,
        },
    }
    kwargs.update(overrides)
    return _equal_weight_diagnostic(**kwargs)  # type: ignore[arg-type]


def _candidate(**overrides: object) -> dict[str, object]:
    system_ids = ["ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"]
    cycle_by_system = {system_id: "00Z" for system_id in system_ids}
    candidate: dict[str, object] = {
        "station_id": "KLGA",
        "season_bin": "SUMMER",
        "lead_bucket": "h00_12",
        "run_cycle": "00Z",
        "run_cycle_scope": "SAME_NATIVE_CYCLE",
        "run_cycle_by_system": cycle_by_system,
        "run_cycle_by_system_sha256": content_sha256(cycle_by_system),
        "settlement_definition": "LOCAL_CALENDAR_DAY_MAX",
        "system_transport_provider_ids": {
            system_id: "OFFICIAL_NATIVE" for system_id in system_ids
        },
        "threshold_profile": "PRODUCTION_NO_LIVE",
        "selected_system_ids": system_ids,
        "selected_system_count": len(system_ids),
        "independent_family_count": 2,
        "system_weights": {system_id: 0.5 for system_id in system_ids},
        "family_weights": {"ECMWF": 0.5, "NOAA_GEFS": 0.5},
        "family_weight_caps": {"ECMWF": 0.75, "NOAA_GEFS": 0.75},
        "train_start_date": "2026-06-01",
        "train_end_date": "2026-07-21",
        "holdout_start_date": "2026-07-22",
        "holdout_end_date": "2026-08-15",
        "rolling_origin_fold_count": 24,
        "effective_sample_n": 24.0,
        "oos_metrics": dict(LEARNED),
        "cross_system_residual_correlation": {},
        "climatology_baseline_metrics": dict(CLIMATE),
        "equal_weight_diagnostic": _diagnostic(),
        "state": "PRODUCTION_NO_LIVE_OOS_PASS",
        "available_system_set_hash": content_sha256(sorted(system_ids)),
        "min_train_days": 30,
        "min_holdout_days": 10,
    }
    candidate.update(overrides)
    return candidate


class DiagnosticMatrixTest(unittest.TestCase):
    def test_the_three_comparisons_are_all_present(self) -> None:
        comparisons = _diagnostic()["comparisons"]
        self.assertEqual(
            sorted(comparisons),
            [
                "a_climate_vs_equal_weight",
                "b_equal_weight_vs_learned",
                "c_climate_vs_learned",
            ],
        )

    def test_bits_decompose_additively(self) -> None:
        """A + B = C exactly, because a log score is a difference of logs.

        This is what makes the matrix a decomposition rather than three
        loosely related numbers: any gap would mean the three legs were not
        scored on one holdout with one grid.
        """

        comparisons = _diagnostic()["comparisons"]
        self.assertAlmostEqual(
            comparisons["a_climate_vs_equal_weight"]["bits"]
            + comparisons["b_equal_weight_vs_learned"]["bits"],
            comparisons["c_climate_vs_learned"]["bits"],
            places=12,
        )

    def test_bits_are_the_log_score_gap_in_base_two(self) -> None:
        c_leg = _diagnostic()["comparisons"]["c_climate_vs_learned"]
        self.assertAlmostEqual(
            c_leg["bits"],
            (CLIMATE["nll"] - LEARNED["nll"]) / math.log(2.0),
            places=12,
        )

    def test_sign_convention_survives_a_losing_leg(self) -> None:
        """Every score here is a loss, so positive must always mean "won".

        The leg most likely to come out negative is B -- equal weight beating
        the learned weights -- and a matrix that renders that as an
        improvement is worse than no matrix at all.
        """

        beaten_by_equal = _diagnostic(
            learned_metrics={"crps": 1.40, "nll": 2.30, "brier": 0.88}
        )["comparisons"]["b_equal_weight_vs_learned"]
        self.assertLess(beaten_by_equal["bits"], 0.0)
        self.assertLess(beaten_by_equal["crps_relative_gain"], 0.0)
        self.assertEqual(beaten_by_equal["verdict"], "WORSE")

    def test_it_reports_what_the_contest_actually_did(self) -> None:
        """B is near-tautological when nothing was dropped and weights are
        flat, so the reader needs both facts beside the comparison."""

        matrix = _diagnostic()
        self.assertEqual(
            matrix["dropped_system_ids"], ["ECCC_GEM_GLOBAL_ENS"]
        )
        self.assertAlmostEqual(
            matrix["weight_l1_distance_from_equal"], 0.1, places=12
        )
        self.assertAlmostEqual(
            matrix["effective_system_count"],
            1.0 / (0.55 ** 2 + 0.45 ** 2),
            places=12,
        )

    def test_a_flat_seated_blend_reports_itself_as_flat(self) -> None:
        matrix = _diagnostic(
            seated_system_ids=["ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"],
            seated_weights={
                "ECMWF_IFS_ENS_OPEN_PF50": 0.5,
                "NOAA_GEFS_05": 0.5,
            },
        )
        self.assertEqual(matrix["weight_l1_distance_from_equal"], 0.0)
        self.assertAlmostEqual(
            matrix["effective_system_count"], 2.0, places=12
        )

    def test_an_unmeasurable_equal_weight_is_stated_not_faked(self) -> None:
        matrix = _diagnostic(equal_weight_metrics=None)
        self.assertEqual(matrix["state"], "EQUAL_WEIGHT_UNAVAILABLE")
        self.assertNotIn("comparisons", matrix)
        self.assertNotIn("equal_weight_metrics", matrix)


class DiagnosticStaysOutOfTheSelectionShaTest(unittest.TestCase):
    def test_the_matrix_is_absent_from_the_selection_identity(self) -> None:
        """Adding, changing or dropping it must not move the selection sha."""

        with_matrix = _selection_identity(_candidate())
        without = _candidate()
        del without["equal_weight_diagnostic"]
        rewritten = _candidate(
            equal_weight_diagnostic=_diagnostic(
                learned_metrics={"crps": 99.0, "nll": 99.0, "brier": 9.0}
            )
        )
        self.assertEqual(with_matrix, _selection_identity(without))
        self.assertEqual(with_matrix, _selection_identity(rewritten))
        self.assertNotIn(
            "equal_weight_diagnostic",
            json.dumps(with_matrix, sort_keys=True),
        )

    def test_the_matrix_rides_the_evaluation_row(self) -> None:
        candidate = _candidate()
        identity = _evaluation_identity(
            candidate,
            selection_evidence_sha256=content_sha256(
                _selection_identity(candidate)
            ),
            evaluated_at_utc="2026-08-18T00:00:00Z",
        )
        metrics = json.loads(identity["oos_metrics_json"])
        stored = metrics["equal_weight_diagnostic"]
        # Charter 272: stored without what it derives, recomputed on read.
        self.assertNotIn("comparisons", stored)
        self.assertEqual(
            stored["derived_stored_as"], EQUAL_WEIGHT_DIAGNOSTIC_STORED_AS
        )
        comparisons = expanded_equal_weight_diagnostic(stored)["comparisons"]
        self.assertEqual(len(comparisons), 3)
        self.assertEqual(
            json.dumps(expanded_equal_weight_diagnostic(stored), sort_keys=True),
            json.dumps(candidate["equal_weight_diagnostic"], sort_keys=True),
        )
        # The scores themselves stay where a reader expects them: the
        # diagnostic is carried beside them, never in place of one.
        self.assertEqual(metrics["crps"], LEARNED["crps"])

    def test_a_candidate_without_the_matrix_still_evaluates(self) -> None:
        """Rows written before the matrix existed must stay readable."""

        candidate = _candidate()
        del candidate["equal_weight_diagnostic"]
        identity = _evaluation_identity(
            candidate,
            selection_evidence_sha256=content_sha256(
                _selection_identity(candidate)
            ),
            evaluated_at_utc="2026-08-18T00:00:00Z",
        )
        metrics = json.loads(identity["oos_metrics_json"])
        self.assertNotIn("equal_weight_diagnostic", metrics)
        self.assertEqual(metrics["crps"], LEARNED["crps"])


class StoredFormRoundTripTest(unittest.TestCase):
    def test_storing_drops_only_what_expanding_recomputes(self) -> None:
        matrix = _diagnostic(
            equal_cohort_metrics={"crps": 0.9, "nll": 1.9, "brier": 0.81},
            shadow_weight_schemes={"inverse_variance": {"A": 0.6, "B": 0.4}},
            shadow_metrics={"inverse_variance": {"crps": 0.85, "nll": 1.86, "brier": 0.80}},
        )
        stored = stored_equal_weight_diagnostic(matrix)
        self.assertNotIn("comparisons", stored)
        self.assertEqual(
            set(stored["shadow_schemes"]["inverse_variance"]), {"metrics", "weights"}
        )
        again = expanded_equal_weight_diagnostic(json.loads(json.dumps(stored)))
        self.assertEqual(
            json.dumps(again, sort_keys=True), json.dumps(matrix, sort_keys=True)
        )

    def test_a_row_stored_whole_before_272_reads_as_it_was(self) -> None:
        matrix = _diagnostic()
        self.assertEqual(expanded_equal_weight_diagnostic(matrix), matrix)


if __name__ == "__main__":
    unittest.main()


class MinimumVarianceWeightTest(unittest.TestCase):
    """Weights that price redundancy, which inverse RMSE cannot see."""

    @staticmethod
    def _correlated_pair_plus_independent(
        seed: int = 11, days: int = 40
    ) -> dict[str, dict[str, float]]:
        import random

        rng = random.Random(seed)
        residuals: dict[str, dict[str, float]] = {"A": {}, "B": {}, "C": {}}
        for index in range(days):
            day = f"2026-07-{index + 1:02d}"
            shared = rng.gauss(0.0, 1.0)
            residuals["A"][day] = shared + rng.gauss(0.0, 0.3)
            residuals["B"][day] = shared + rng.gauss(0.0, 0.3)
            residuals["C"][day] = rng.gauss(0.0, 1.05)
        return residuals

    def test_redundant_systems_share_one_seat_between_them(self) -> None:
        """A and B make the same error; C is independent and no better alone.

        Inverse RMSE pays A and B in full for one piece of information.  The
        covariance solution must hand the redundant pair, together, less than
        it hands the system that is telling it something new.
        """

        from bayes_basket_target.native_ensemble_oos import (
            _minimum_variance_weights,
        )

        weights = _minimum_variance_weights(
            residuals_by_system=self._correlated_pair_plus_independent(),
            family_by_system={"A": "fa", "B": "fb", "C": "fc"},
            family_caps={},
            shrinkage=0.0,
        )
        assert weights is not None
        self.assertLess(weights["A"] + weights["B"], weights["C"])
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)

    def test_shrinking_to_the_diagonal_forgets_the_redundancy(self) -> None:
        """At full shrinkage it is inverse-variance and must stop correcting."""

        from bayes_basket_target.native_ensemble_oos import (
            _minimum_variance_weights,
        )

        residuals = self._correlated_pair_plus_independent()
        full = _minimum_variance_weights(
            residuals_by_system=residuals,
            family_by_system={"A": "fa", "B": "fb", "C": "fc"},
            family_caps={},
            shrinkage=0.0,
        )
        diagonal = _minimum_variance_weights(
            residuals_by_system=residuals,
            family_by_system={"A": "fa", "B": "fb", "C": "fc"},
            family_caps={},
            shrinkage=1.0,
        )
        assert full is not None and diagonal is not None
        self.assertGreater(
            diagonal["A"] + diagonal["B"], full["A"] + full["B"]
        )

    def test_no_weight_is_ever_negative(self) -> None:
        """A blend is a mixture; a negative share has no meaning in one.

        Two near-identical systems drive the unconstrained solution to large
        offsetting weights, which is exactly where an unguarded inverse would
        return one.
        """

        from bayes_basket_target.native_ensemble_oos import (
            _minimum_variance_weights,
        )

        import random

        rng = random.Random(5)
        residuals: dict[str, dict[str, float]] = {"A": {}, "B": {}, "C": {}}
        for index in range(40):
            day = f"2026-07-{index + 1:02d}"
            shared = rng.gauss(0.0, 1.0)
            residuals["A"][day] = shared + rng.gauss(0.0, 0.02)
            residuals["B"][day] = shared * 1.02 + rng.gauss(0.0, 0.02)
            residuals["C"][day] = rng.gauss(0.0, 3.0)
        weights = _minimum_variance_weights(
            residuals_by_system=residuals,
            family_by_system={"A": "fa", "B": "fb", "C": "fc"},
            family_caps={},
            shrinkage=0.0,
        )
        assert weights is not None
        self.assertTrue(all(value >= 0.0 for value in weights.values()))
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)

    def test_a_family_cap_binds_the_candidate_too(self) -> None:
        """Otherwise the comparison is of constraints, not of schemes."""

        from bayes_basket_target.native_ensemble_oos import (
            _minimum_variance_weights,
        )

        import random

        rng = random.Random(9)
        residuals: dict[str, dict[str, float]] = {"A": {}, "B": {}, "C": {}}
        for index in range(40):
            day = f"2026-07-{index + 1:02d}"
            residuals["A"][day] = rng.gauss(0.0, 0.2)
            residuals["B"][day] = rng.gauss(0.0, 0.2)
            # Worse than the pair but not hopeless: the shared family earns
            # far past its cap while this one still holds positive weight, so
            # the cap is what binds rather than the non-negativity.
            residuals["C"][day] = rng.gauss(0.0, 0.5)
        weights = _minimum_variance_weights(
            residuals_by_system=residuals,
            family_by_system={"A": "shared", "B": "shared", "C": "other"},
            family_caps={"shared": 0.6, "other": 0.75},
            shrinkage=0.0,
        )
        assert weights is not None
        self.assertLessEqual(weights["A"] + weights["B"], 0.6 + 1e-9)

    def test_too_few_days_declines_rather_than_guesses(self) -> None:
        from bayes_basket_target.native_ensemble_oos import (
            _minimum_variance_weights,
        )

        residuals = {
            "A": {f"2026-07-0{i}": 0.4 * i for i in range(1, 6)},
            "B": {f"2026-07-0{i}": 0.3 * i for i in range(1, 6)},
        }
        self.assertIsNone(_minimum_variance_weights(
            residuals_by_system=residuals,
            family_by_system={"A": "fa", "B": "fb"},
            family_caps={},
        ))

    def test_a_degenerate_system_declines_rather_than_dividing_by_zero(
        self,
    ) -> None:
        from bayes_basket_target.native_ensemble_oos import (
            _minimum_variance_weights,
        )

        residuals = {
            "A": {f"2026-07-{i:02d}": 0.0 for i in range(1, 21)},
            "B": {f"2026-07-{i:02d}": float(i % 3) for i in range(1, 21)},
        }
        self.assertIsNone(_minimum_variance_weights(
            residuals_by_system=residuals,
            family_by_system={"A": "fa", "B": "fb"},
            family_caps={},
        ))

    def test_it_declines_when_the_optimum_wants_a_single_family(self) -> None:
        """A blend the serving contract refuses is not a result to report.

        Where one family is so much worse that the non-negative optimum puts
        nothing on it, forcing it back in at some floor would be inventing a
        weight no criterion chose -- so the scheme reports no opinion instead.
        """

        from bayes_basket_target.native_ensemble_oos import (
            _minimum_variance_weights,
        )

        import random

        rng = random.Random(9)
        residuals: dict[str, dict[str, float]] = {"A": {}, "B": {}, "C": {}}
        for index in range(40):
            day = f"2026-07-{index + 1:02d}"
            residuals["A"][day] = rng.gauss(0.0, 0.2)
            residuals["B"][day] = rng.gauss(0.0, 0.2)
            residuals["C"][day] = rng.gauss(0.0, 4.0)
        self.assertIsNone(_minimum_variance_weights(
            residuals_by_system=residuals,
            family_by_system={"A": "shared", "B": "shared", "C": "other"},
            family_caps={},
            shrinkage=0.0,
        ))
