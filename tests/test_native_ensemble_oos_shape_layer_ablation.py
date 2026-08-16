"""The per-layer ablation is recorded where it cannot unbind an activation.

Every shape layer rides in and out on the combination-method tag, and the OOS
gate only ever asks whether the assembled distribution beats climatology, so a
layer that costs the distribution passes as easily as one that earns its place.
The ablation closes that by scoring each layer against its own absence -- but
it is a diagnostic, and a diagnostic that lands in the selection identity
rebinds every activation pointer the P0 compile validates against, which is how
the envelope pipeline went silent the last time metric bytes moved.  These
tests pin both halves: the ablation reaches the evaluation row, and the
selection sha never sees it.
"""

from __future__ import annotations

import json
import unittest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.native_ensemble_oos import (
    _evaluation_identity,
    _selection_identity,
)


ABLATION = {
    "conditional_tail_skew": {"crps": -4.21, "nll": -1.92, "brier": -0.55},
    "band_reliability": {"crps": 0.31, "nll": 0.28, "brier": 0.04},
}


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
        "oos_metrics": {"crps": 0.8428, "nll": 1.8579, "brier": 0.794},
        "cross_system_residual_correlation": {},
        "climatology_baseline_metrics": {
            "crps": 1.91,
            "nll": 2.44,
            "brier": 0.92,
        },
        "shape_layer_ablation": ABLATION,
        "state": "PRODUCTION_NO_LIVE_OOS_PASS",
        "available_system_set_hash": content_sha256(sorted(system_ids)),
        "min_train_days": 30,
        "min_holdout_days": 10,
    }
    candidate.update(overrides)
    return candidate


class ShapeLayerAblationRecordTest(unittest.TestCase):
    def test_ablation_is_absent_from_the_selection_identity(self) -> None:
        """Adding, changing or dropping it must not move the selection sha."""

        with_ablation = _selection_identity(_candidate())
        without = _candidate()
        del without["shape_layer_ablation"]
        rewritten = _candidate(
            shape_layer_ablation={"conditional_tail_skew": {"crps": 99.0}}
        )
        self.assertEqual(with_ablation, _selection_identity(without))
        self.assertEqual(with_ablation, _selection_identity(rewritten))
        self.assertNotIn(
            "shape_layer_ablation", json.dumps(with_ablation, sort_keys=True)
        )

    def test_ablation_rides_the_evaluation_row(self) -> None:
        candidate = _candidate()
        identity = _evaluation_identity(
            candidate,
            selection_evidence_sha256=content_sha256(
                _selection_identity(candidate)
            ),
            evaluated_at_utc="2026-08-16T00:00:00Z",
        )
        metrics = json.loads(identity["oos_metrics_json"])
        self.assertEqual(metrics["shape_layer_ablation"], ABLATION)
        # The metrics themselves stay where a reader expects them: the
        # diagnostic is carried beside the scores, never in place of one.
        self.assertEqual(metrics["crps"], 0.8428)

    def test_a_candidate_without_the_diagnostic_still_evaluates(self) -> None:
        """Rows written before the ablation existed must stay readable."""

        candidate = _candidate()
        del candidate["shape_layer_ablation"]
        identity = _evaluation_identity(
            candidate,
            selection_evidence_sha256=content_sha256(
                _selection_identity(candidate)
            ),
            evaluated_at_utc="2026-08-16T00:00:00Z",
        )
        metrics = json.loads(identity["oos_metrics_json"])
        self.assertNotIn("shape_layer_ablation", metrics)
        self.assertEqual(metrics["crps"], 0.8428)


if __name__ == "__main__":
    unittest.main()
