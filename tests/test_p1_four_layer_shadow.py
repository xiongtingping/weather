"""The shadow, and the one property that makes it safe to run in production.

A shadow layer earns its way in by being scored on real days before it is
allowed to price anything.  That is only worth doing if running it cannot
change what is priced meanwhile, and cannot stop a P1 from compiling at all.
Both are pinned here, because both failure modes are silent: a shadow that
shifts the served distribution is an unannounced promotion, and a shadow that
raises is a measurement lane holding the order chain.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.four_layer_shadow import (  # noqa: E402
    SHADOW_LINEAGE_KEYS,
)
from bayes_basket_target.contracts import ProbabilityEnvelope  # noqa: E402
from bayes_basket_target.p1_compiler import compile_station_p1_packets  # noqa: E402
from tests.test_p1_trajectory_staged_cutover import (  # noqa: E402
    _REGISTRY,
    _p0_root,
    _promoted_artifact,
    _two_bucket_observations,
)

HAZARD = {
    "content_sha256": "h" * 64,
    "trained_through": "2026-08-08",
    "feature_fallbacks": {
        "remaining_hours": 6.0, "drawdown_from_high_c": 0.0,
        "momentum_c_per_hour": 0.0, "cloud_cover_fraction": 0.3,
        "precipitation_present": 0.0, "dewpoint_depression_c": 8.0,
        "wind_speed_mps": 3.0, "warming_so_far_c": 4.0,
        "curve_shape_realized": 0.5,
    },
    "phase": {
        "hours_edges": [0.0, 3.0, 6.0, 12.0, 1e9],
        "bin_intercepts": [-4.0, -2.0, 0.0, 2.0],
        "coefficients": {"cloud_cover_fraction": -0.8},
    },
    "continuation_full": {
        "rungs": [2, 3, 4],
        "rung_intercepts": [0.5, 0.0, -0.5],
        "coefficients": {"curve_shape_realized": -1.0},
    },
    "continuation_phase_only": {
        "rungs": [2, 3, 4],
        "rung_intercepts": [0.5, 0.0, -0.5],
        "coefficients": {"cloud_cover_fraction": 0.0},
    },
}
REANCHOR = {
    "content_sha256": "r" * 64,
    "hours_edges": [0.0, 6.0, 12.0, 1e9],
    "max_shift_c": 3.0,
    "minimum_bias_reports": 2,
    "bins": {"0": {"slope": 0.1}, "1": {"slope": 0.6}, "2": {"slope": 0.2}},
    "pooled": {"slope": 0.3},
}


def _root_with_intervals() -> ProbabilityEnvelope:
    """The staged-cutover root plus the geometry every real root publishes.

    99.9% of committed roots carry ``bucket_intervals_c_json``; the fixture
    next door omits it because the tests it serves never needed it.  The
    translation layer does, so this test supplies it.
    """

    body = _p0_root().to_dict()
    body["lineage"] = {
        **body["lineage"],
        "bucket_intervals_c_json": json.dumps({
            "75°F or below": [None, 23.89],
            "76-77°F": [23.89, 25.0],
            "78°F or higher": [25.0, None],
        }),
    }
    return ProbabilityEnvelope.from_mapping(body)


def _four_reports() -> list[dict]:
    """Four rising reports with parsable METAR bodies.

    The path state needs three observations and a body it can read before it
    calls itself complete, which is the same minimum the promoted trajectory
    methods carry.  The two-report fixture next door is deliberately below it,
    so the shadow correctly declines there.
    """

    rows = []
    for index, (hour, temperature) in enumerate(
        ((14, 20.0), (15, 21.0), (16, 22.0), (18, 23.0))
    ):
        rows.append({
            "observation_id": f"o-{index}",
            "station_id": "KLGA",
            "event_date_local": "2026-08-09",
            "settlement_timezone": "America/New_York",
            "reported_temperature_c": temperature,
            "valid_at_utc": f"2026-08-09T{hour:02d}:00:00Z",
            "canonical_committed_at_utc": f"2026-08-09T{hour:02d}:05:00Z",
            "raw_text": (
                f"METAR KLGA 09{hour:02d}00Z 24008KT 9999 FEW030 "
                f"{int(temperature):02d}/14 Q1013 NOSIG"
            ),
            "raw_sha256": str(index) * 64,
            "source_lane": "AWC",
            "report_type": "METAR",
            "evidence_role": "FORMAL",
            "qc_status": "ACCEPTED",
            "correction_suspected": False,
        })
    return rows


def _compile(**overrides):
    arguments = {
        "predecessor": _p0_root(),
        "observations": _two_bucket_observations(),
        "registry_rows": _REGISTRY,
        "compilation_at_utc": "2026-08-09T19:00:00Z",
        "trajectory_artifact": _promoted_artifact("AFTERNOON"),
        "soft_likelihood_policy": "ACTIVE",
    }
    arguments.update(overrides)
    return compile_station_p1_packets(**arguments)


def test_the_shadow_does_not_move_what_is_served() -> None:
    """The whole point: measured on real days, priced into nothing.

    Every served number has to match the run without artifacts -- the
    probabilities, the stage anchor the basket seats on, and the content hash
    that binds them.  Anything less is a promotion nobody voted for.
    """

    without = _compile(
        predecessor=_root_with_intervals(), observations=_four_reports()
    )
    with_shadow = _compile(
        predecessor=_root_with_intervals(),
        observations=_four_reports(),
        four_layer_artifacts={"hazard": HAZARD, "reanchor": REANCHOR},
    )
    assert len(without) == len(with_shadow) >= 2
    for plain, shadowed in zip(without, with_shadow):
        left = plain["expected_envelope"]
        right = shadowed["expected_envelope"]
        assert left["probabilities"] == right["probabilities"]
        assert left["lineage"]["model_center_bucket"] == (
            right["lineage"]["model_center_bucket"]
        )
        assert left["lineage"]["model_center_c"] == (
            right["lineage"]["model_center_c"]
        )
        assert plain["trajectory_proof"] == shadowed["trajectory_proof"]
        # The shadow keys are the only additions.
        added = set(right["lineage"]) - set(left["lineage"])
        assert added <= set(SHADOW_LINEAGE_KEYS)
        # Every other lineage value is untouched, except the two that name
        # the step before this one.  The shadow is part of the envelope, so
        # it moves that envelope's content hash, and the next step in the
        # chain names the new hash.  That is identity moving, not the served
        # distribution moving -- which is what the assertions above pin.
        chained = {"predecessor_content_sha256", "predecessor_checkpoint_id"}
        for key in set(left["lineage"]) - chained:
            assert left["lineage"][key] == right["lineage"][key], key


def test_without_artifacts_the_lineage_is_what_it_always_was() -> None:
    """No artifact, no key: every existing caller compiles what it compiled."""

    for packet in _compile(observations=_four_reports()):
        lineage = packet["expected_envelope"]["lineage"]
        assert not set(lineage) & set(SHADOW_LINEAGE_KEYS)


def test_the_shadow_records_the_promoted_set_and_the_reanchor_apart() -> None:
    """Two documents, because the layers are at different stages of argument.

    The promoted set carries a distribution the ledger can score.  The
    reanchor carries only what it would have moved, because it is recorded
    and not promoted.
    """

    packets = _compile(
        predecessor=_root_with_intervals(),
        observations=_four_reports(),
        four_layer_artifacts={"hazard": HAZARD, "reanchor": REANCHOR},
    )
    lineage = packets[-1]["expected_envelope"]["lineage"]
    promoted = json.loads(lineage["p1_shadow_four_layer_json"])
    assert promoted["layers"] == ["fact", "phase", "remaining_warming"]
    assert promoted["hazard_artifact_sha256"] == HAZARD["content_sha256"]
    assert sum(promoted["probabilities"].values()) == pytest.approx(1.0)
    assert promoted["centre_c"] > 0.0
    assert promoted["phase"]["state"] in {"APPLIED", "SPLIT_DEGENERATE"}

    reanchor = json.loads(lineage["p1_shadow_reanchor_json"])
    assert reanchor["state"] == "SHADOW_ONLY_NOT_PROMOTED"
    assert "centre_delta_c" in reanchor
    # It reports what it would have done, and no distribution of its own is
    # offered for anything to consume.
    assert "probabilities" not in reanchor


def test_a_broken_artifact_costs_the_shadow_and_nothing_else() -> None:
    """A measurement lane may not be the reason an order chain stops.

    The shadow swallows its own failures and records the refusal; the served
    envelope is the same one the run without artifacts produced.
    """

    broken = {**HAZARD, "phase": {"hours_edges": [0.0], "bin_intercepts": []}}
    plain = _compile(observations=_four_reports())
    damaged = _compile(
        observations=_four_reports(), four_layer_artifacts={"hazard": broken}
    )
    assert len(damaged) == len(plain)
    for left, right in zip(plain, damaged):
        assert left["expected_envelope"]["probabilities"] == (
            right["expected_envelope"]["probabilities"]
        )
    lineage = damaged[-1]["expected_envelope"]["lineage"]
    if "p1_shadow_four_layer_json" in lineage:
        document = json.loads(lineage["p1_shadow_four_layer_json"])
        assert document.get("state") == "REFUSED"


def test_a_prefix_too_short_for_a_path_state_records_no_shadow() -> None:
    """Declining is not failing.

    Two reports is below the minimum the physical state needs before it calls
    itself complete, so the shadow says nothing rather than saying something
    from an incomplete state -- the same rule the promoted trajectory methods
    already follow on those steps.
    """

    packets = _compile(
        four_layer_artifacts={"hazard": HAZARD, "reanchor": REANCHOR}
    )
    for packet in packets:
        lineage = packet["expected_envelope"]["lineage"]
        assert "p1_shadow_four_layer_json" not in lineage


def test_a_root_that_states_no_intervals_gets_no_reanchor_document() -> None:
    """The translation is the only layer that needs the geometry.

    Without intervals it is skipped rather than run on a geometry the root
    did not state -- and the promoted set, which reads none, still records.
    """

    packets = _compile(
        observations=_four_reports(),
        four_layer_artifacts={"hazard": HAZARD, "reanchor": REANCHOR},
    )
    lineage = packets[-1]["expected_envelope"]["lineage"]
    assert "p1_shadow_four_layer_json" in lineage
    assert "p1_shadow_reanchor_json" not in lineage
