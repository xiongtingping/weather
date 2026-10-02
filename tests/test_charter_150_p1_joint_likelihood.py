"""Charter 150: P0-M and P1 are one likelihood update each on a calibrated root.

User ruling 2026-09-19 ("按照方案全量落地并测试调优"):

* P0 is the only stage with post-hoc calibration (centre/width; the band
  table beside it retired with charter 232).
* P0-M = P0 x L_TAF under the observed-high mask -- TAF as a likelihood only.
* P1 = root x 1[k >= running-high bucket] x g(d_k | s_t) ** alpha(hours) -- one
  joint observation likelihood per decision, no chained layers after it.

These pin the arithmetic on the served envelope, the silences, the fallback,
source single-sidedness (no retired layer's receipt on either stage) and the
order-path gate that reads the new receipt.
"""
from __future__ import annotations

import json
import math

import pytest

from bayes_basket_target import metar_trajectory as MT
from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target import taf_likelihood as TL
from bayes_basket_target.contracts import ContractError, ProbabilityEnvelope
from bayes_basket_target.materialization import (
    PROBABILITY_MODEL_ZERO_FLOOR,
    materialize_p0m_observed,
)
from bayes_basket_target.p1_compiler import compile_station_p1_packets
from tests.test_bayes_basket_target_materialization import _envelope, _p0m_root

RETIRED_P1_KEYS = (
    "center_width_json",
    "band_reliability_json",
    "metar_ceiling_json",
    "regional_path_prior_json",
    "p1_four_layer_serving_json",
    "p1_shadow_four_layer_json",
    "p1_shadow_reanchor_json",
)


def _artifact(**overrides) -> dict:
    """A small but complete joint-likelihood artifact.

    Rise is likely with hours left and unlikely without; the continuation
    puts most of a rise on the first rung.
    """

    fallbacks = {name: 0.0 for name in OL.FULL_FEATURES}
    fallbacks.update({"cloud_cover_fraction": 0.4, "remaining_hours": 8.0})
    body = {
        "schema": OL.SCHEMA,
        "rule": OL.RULE,
        "feature_fallbacks": fallbacks,
        "phase": {
            "hours_edges": [0.0, 6.0, 10.0, 1e9],
            "bin_intercepts": [-2.0, 0.0, 1.5],
            "coefficients": {
                name: 0.0 for name in OL.PHASE_FEATURES
            } | {"cloud_cover_fraction": -1.0, "drawdown_from_high_c": -0.5},
        },
        "continuation_full": {
            "rungs": [2, 3],
            "rung_intercepts": [-1.0, -1.5],
            "coefficients": {name: 0.0 for name in OL.FULL_FEATURES},
        },
        "alpha_by_bin": [0.5, 1.0, 1.5],
        "trained_through": "2026-07-20",
        "content_sha256": "c" * 64,
    }
    body.update(overrides)
    return body


def _registry(root: ProbabilityEnvelope) -> list[dict]:
    return [
        {
            "station_id": root.station_id,
            "event_date": root.event_date,
            "settlement_timezone": root.settlement_timezone,
            "settlement_unit": "C",
            "outcome": bucket,
            "registry_json": json.dumps({"upper": upper}),
        }
        for bucket, upper in (("37", 37), ("38", 38), ("39", None))
    ]


def _observation(root, suffix: str, at: str, temperature_c: float) -> dict:
    return {
        "observation_id": f"obs-{suffix}",
        "station_id": root.station_id,
        "event_date_local": root.event_date,
        "settlement_timezone": root.settlement_timezone,
        "reported_temperature_c": temperature_c,
        "valid_at_utc": at,
        "canonical_committed_at_utc": at,
        "raw_sha256": suffix * 64,
        "source_lane": "AWC",
        "report_type": "METAR",
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
    }


def _compile(artifact=None, *, observations=None, at="2026-07-23T15:00:00Z"):
    root = _p0m_root(_envelope())
    return root, compile_station_p1_packets(
        predecessor=root,
        observations=observations or [_observation(root, "a", at, 36.8)],
        registry_rows=_registry(root),
        compilation_at_utc=at,
        observation_likelihood_artifact=artifact,
    )


def test_the_literals_the_order_gate_reads_are_the_module_constants() -> None:
    assert MT.P1_JOINT_LIKELIHOOD_MODE == OL.LIKELIHOOD_MODE
    assert MT.P1_JOINT_LIKELIHOOD_LINEAGE_KEY == OL.LINEAGE_KEY
    assert MT.P1_JOINT_LIKELIHOOD_RECEIPT_SCHEMA == OL.RECEIPT_SCHEMA


def test_served_p1_is_root_times_mask_times_tempered_likelihood() -> None:
    root, packets = _compile(_artifact())
    envelope = packets[-1]["expected_envelope"]
    receipt = json.loads(envelope["lineage"][OL.LINEAGE_KEY])
    assert receipt["state"] == "APPLIED"
    # Rebuild the vector from the receipt's own features with the module's
    # own function -- not a copy of the rule.
    features = receipt["features"]
    likelihood, _ = OL.compile_observation_likelihood(
        {k: v for k, v in features.items()},
        _artifact(),
        bucket_ordinals=root.bucket_ordinals,
        bucket_centers_c=packets[-1]["bucket_centers_c"],
        running_high_c=receipt["running_high_c"],
        running_high_bucket=receipt["running_high_bucket"],
        hard_excluded_buckets=packets[-1]["hard_excluded_buckets"],
    )
    excluded = set(packets[-1]["hard_excluded_buckets"])
    weighted = {
        b: 0.0 if b in excluded else p * likelihood[b]
        for b, p in root.probabilities.items()
    }
    total = sum(weighted.values())
    expected = {b: v / total for b, v in weighted.items()}
    if any(
        v < PROBABILITY_MODEL_ZERO_FLOOR
        for b, v in expected.items() if b not in excluded
    ):
        pytest.skip("fixture landed under the floor; covered elsewhere")
    for bucket, value in expected.items():
        assert envelope["probabilities"][bucket] == pytest.approx(value, abs=1e-6)
    # The receipt's alpha is the artifact's value for this many hours left.
    assert receipt["alpha"] == OL.alpha_for(features["remaining_hours"], _artifact())


def test_the_compile_is_idempotent() -> None:
    _, first = _compile(_artifact())
    _, second = _compile(_artifact())
    assert first[-1]["expected_envelope"] == second[-1]["expected_envelope"]


def test_alpha_zero_is_the_root_under_the_hard_bound() -> None:
    """The ablation arm is this code with one constant changed."""

    _, bare = _compile(None)
    _, flat = _compile(_artifact(alpha_by_bin=[0.0, 0.0, 0.0]))
    assert (
        bare[-1]["expected_envelope"]["probabilities"]
        == flat[-1]["expected_envelope"]["probabilities"]
    )
    assert json.loads(
        bare[-1]["expected_envelope"]["lineage"][OL.LINEAGE_KEY]
    )["state"] == "SILENT_NO_ARTIFACT"


def test_no_retired_layer_speaks_on_p1() -> None:
    _, packets = _compile(_artifact())
    lineage = packets[-1]["expected_envelope"]["lineage"]
    assert not [key for key in RETIRED_P1_KEYS if key in lineage]
    assert lineage["observation_likelihood_mode"] == OL.LIKELIHOOD_MODE
    assert packets[-1]["expected_envelope"]["posterior_version"] == (
        "p1_joint_likelihood_v37"
    )


def test_silent_on_replayed_steps_behind_the_current_high() -> None:
    root = _p0m_root(_envelope())
    at1, at2 = "2026-07-23T15:00:00Z", "2026-07-23T16:00:00Z"
    _, packets = _compile(
        _artifact(),
        observations=[
            _observation(root, "a", at1, 36.8),
            _observation(root, "b", at2, 37.6),
        ],
        at=at2,
    )
    assert len(packets) == 2
    states = [
        json.loads(p["expected_envelope"]["lineage"][OL.LINEAGE_KEY])["state"]
        for p in packets
    ]
    assert states == ["SILENT_PREFIX_NOT_CURRENT", "APPLIED"]


def test_an_incomplete_path_state_falls_back_to_the_fleet_mean() -> None:
    """One report cannot complete a path state; the clock still speaks."""

    _, packets = _compile(_artifact())
    receipt = json.loads(
        packets[-1]["expected_envelope"]["lineage"][OL.LINEAGE_KEY]
    )
    assert receipt["state"] == "APPLIED"
    assert set(OL.STATE_FEATURES) <= set(receipt["fleet_mean_features"])
    assert receipt["features"]["remaining_hours"] > 0.0


def test_a_malformed_artifact_is_refused_and_the_p1_still_compiles() -> None:
    broken = _artifact()
    broken["phase"] = {"hours_edges": [0.0, 1e9], "bin_intercepts": []}
    _, packets = _compile(broken)
    receipt = json.loads(
        packets[-1]["expected_envelope"]["lineage"][OL.LINEAGE_KEY]
    )
    assert receipt["state"] == "REFUSED"
    assert receipt["reason"]


def test_the_anchor_is_the_served_vectors_weighted_centre() -> None:
    _, packets = _compile(_artifact())
    envelope = packets[-1]["expected_envelope"]
    centers = packets[-1]["bucket_centers_c"]
    mean = sum(
        float(p) * float(centers[b]) for b, p in envelope["probabilities"].items()
    )
    assert float(envelope["lineage"]["model_center_c"]) == pytest.approx(mean)


def test_the_order_gate_trades_only_a_step_the_likelihood_spoke_on() -> None:
    _, spoke = _compile(_artifact())
    MT.assert_p1_execution_evidence(spoke[-1]["expected_envelope"]["lineage"])
    _, silent = _compile(None)
    with pytest.raises(ContractError, match="p1_execution_evidence_missing"):
        MT.assert_p1_execution_evidence(
            silent[-1]["expected_envelope"]["lineage"]
        )


# -- P0-M ------------------------------------------------------------------


def _taf_artifact(state: str = TL.GATE_SERVE) -> dict:
    return {
        "schema": TL.SCHEMA,
        "rule": TL.RULE,
        "pool": {"a": 0.0, "b": 0.5, "tau": 1.0, "n": 100},
        "stations": {},
        "shrink_k": TL.SHRINK_K,
        "gate": {"state": state},
        "content_sha256": "d" * 64,
    }


def _p0m(root: ProbabilityEnvelope, artifact, tx: float | None = 39.0):
    return materialize_p0m_observed(
        root,
        p0_root=root,
        forecast_revision_id="taf-1",
        forecast_source_object_sha256="e" * 64,
        taf_tx_c=tx,
        decision_at_utc="2026-07-23T05:00:00Z",
        fresh_until_utc="2026-07-23T06:00:00Z",
        bridge_sha256="b" * 64,
        taf_likelihood_artifact=artifact,
    )


def _root_with_centre() -> ProbabilityEnvelope:
    base = _envelope()
    return ProbabilityEnvelope.from_mapping({
        **base.to_dict(),
        "lineage": {**base.lineage, "model_center_c": "38"},
    })


def test_a_closed_gate_leaves_p0m_equal_to_p0() -> None:
    root = _root_with_centre()
    envelope = _p0m(root, _taf_artifact(TL.GATE_IDENTITY))
    assert dict(envelope.probabilities) == dict(root.probabilities)
    assert envelope.lineage["taf_revision_state"] == "IDENTITY_GATE_CLOSED"
    assert envelope.posterior_version == "p0m_taf_likelihood_v29"


def test_an_open_gate_multiplies_p0_by_the_taf_gaussian() -> None:
    root = _root_with_centre()
    envelope = _p0m(root, _taf_artifact())
    receipt = json.loads(envelope.lineage[TL.LINEAGE_KEY])
    assert receipt["state"] == "APPLIED"
    # x = 1, y_k = k - 38, a = 0, b = 0.5, tau = 1.
    likelihood = {
        b: math.exp(-((1.0 - 0.5 * (float(b) - 38.0)) ** 2) / 2.0)
        for b in root.probabilities
    }
    weighted = {b: p * likelihood[b] for b, p in root.probabilities.items()}
    total = sum(weighted.values())
    for bucket, value in weighted.items():
        assert envelope.probabilities[bucket] == pytest.approx(value / total)
    assert envelope.lineage["taf_likelihood_mode"] == TL.LIKELIHOOD_MODE
    # The anchor follows the served vector once the likelihood speaks.
    centre = sum(float(b) * p for b, p in envelope.probabilities.items())
    assert float(envelope.lineage["model_center_c"]) == pytest.approx(centre)


def test_p0m_carries_no_centre_width_and_no_band() -> None:
    envelope = _p0m(_root_with_centre(), _taf_artifact())
    assert "center_width_json" not in envelope.lineage
    assert "band_reliability_json" not in envelope.lineage
    assert "center_width_taf_cell" not in envelope.lineage


def test_no_tx_is_identity_and_says_so() -> None:
    root = _root_with_centre()
    envelope = _p0m(root, _taf_artifact(), tx=None)
    assert dict(envelope.probabilities) == dict(root.probabilities)
    assert envelope.lineage["taf_revision_state"] == "IDENTITY_NO_TX"


def test_only_p0_serves_post_hoc_tables() -> None:
    from bayes_basket_target import band_reliability_store
    from bayes_basket_target.center_width_store import (
        CENTER_WIDTH_SERVED_STAGES,
    )

    assert CENTER_WIDTH_SERVED_STAGES == ("P0",)
    # Charter 232: the band table serves no stage at all.
    assert not hasattr(band_reliability_store, "BAND_SERVED_STAGES")
