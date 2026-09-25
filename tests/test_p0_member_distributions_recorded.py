"""P0 records each member system's calibrated distribution on its envelope.

The pooled P0 is a weighted mixture of one calibrated distribution per seated
system (ECMWF/GEFS/ICON/ECCC in production).  Until 2026-09-21 those vectors
were hashed into ``native_probability_evidence_sha256`` and dropped, so the
only way to ask which member put mass where was to recompile a retained
packet at the original decision instant.  These tests compile a P0 and read
the vectors back off the envelope the compiler hands to materialization.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target import native_ensemble_p0 as p0
from bayes_basket_target.materialization import (
    PROBABILITY_MODEL_ZERO_FLOOR,
    floor_probabilities,
)
from bayes_basket_target.native_ensemble_p0 import compile_native_ensemble_p0
from bayes_basket_target.probability_packet import build_probability_envelope

from tests.test_native_ensemble_p0 import (
    _install_mixed_cycle_calibrations,
    _mixed_packet,
)

DECISION_AT_UTC = "2026-07-01T13:00:00Z"


@pytest.fixture(autouse=True)
def _fixture_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mixed-cycle fixture's own premises (see tests/test_native_ensemble_p0):
    contest-era membership, and station ``TEST`` mapped to a season."""

    from bayes_basket_target import native_ensemble_oos
    from bayes_basket_target import station_season

    monkeypatch.setattr(native_ensemble_oos, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setattr(p0, "P0_MEMBERSHIP_MODE", "subset_contest")
    monkeypatch.setitem(station_season._SEASON_ANCHOR_MONTH, "TEST", 7)


def _compile() -> dict:
    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    return compile_native_ensemble_p0(
        conn,
        forward_packet=_mixed_packet(),
        decision_at_utc=DECISION_AT_UTC,
    )


def test_each_seated_members_distribution_is_on_the_envelope_and_pools_to_it() -> None:
    compiled = _compile()
    envelope = compiled["packet"]["native_probability_envelope"]
    lineage = envelope["lineage"]

    members = json.loads(lineage["native_system_probabilities_json"])
    weights = json.loads(lineage["system_weights_json"])
    intervals = json.loads(lineage["bucket_intervals_c_json"])
    seated = json.loads(lineage["ensemble_system_ids_json"])

    # One vector per seated system, each over exactly the envelope's buckets
    # and a proper distribution.
    assert set(members) == set(seated) == set(weights)
    assert len(members) >= 2
    for system, vector in members.items():
        assert list(vector) == list(intervals), system
        assert set(vector) == set(envelope["probabilities"]), system
        assert all(value >= 0.0 for value in vector.values()), system
        assert sum(vector.values()) == pytest.approx(1.0, abs=1e-9), system
    # Not one vector copied under several names: the members disagree.
    distinct = {tuple(sorted(vector.items())) for vector in members.values()}
    assert len(distinct) == len(members)

    # Every layer between the pooling and the served vector is identity on
    # this fixture -- the bridge has no gap to close, there is no radiation
    # capture to shift on, and no centre/width, tail or band table is pinned
    # -- except the last: charter 224's model-zero floor, which lifts every
    # bucket below 1e-3 and renormalises.  So the served vector is the pooled
    # one through production's own floor, and the recorded members must
    # reproduce it through the weights the envelope names.
    assert json.loads(lineage["consensus_bridge_json"])["lambda"] == 0.0
    assert not json.loads(lineage["synoptic_cooling_json"]).get("shift_c")
    for absent in (
        "center_width_json", "tail_reliability_json", "band_reliability_json",
    ):
        assert absent not in lineage
    pooled = {
        bucket: sum(
            float(weights[system]) * float(members[system][bucket])
            for system in seated
        )
        for bucket in intervals
    }
    total = sum(pooled.values())
    assert total > 0.0
    floored, lifted = floor_probabilities(
        {bucket: value / total for bucket, value in pooled.items()}
    )
    receipt = lineage.get("probability_floor_json")
    if lifted:
        receipt = json.loads(receipt)
        assert receipt["epsilon"] == PROBABILITY_MODEL_ZERO_FLOOR
        assert receipt["floored_buckets"] == lifted
    else:
        assert receipt is None
    for bucket, served in envelope["probabilities"].items():
        assert floored[bucket] == pytest.approx(served, abs=1e-12), bucket


def test_the_recorded_vectors_are_the_calibrated_ones_the_pool_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What is recorded is the calibration's output for each system, not a
    raw member histogram rebuilt beside it."""

    produced: list[dict[str, float]] = []
    calibrated = p0._calibrated_system_probability

    def spy(**kwargs):
        out = calibrated(**kwargs)
        produced.append(dict(out))
        return out

    monkeypatch.setattr(p0, "_calibrated_system_probability", spy)
    compiled = _compile()
    lineage = compiled["packet"]["native_probability_envelope"]["lineage"]
    members = json.loads(lineage["native_system_probabilities_json"])

    assert len(produced) == len(members)
    recorded = sorted(
        json.dumps(vector, sort_keys=True) for vector in members.values()
    )
    returned = sorted(json.dumps(vector, sort_keys=True) for vector in produced)
    assert recorded == returned


def test_the_record_survives_the_materialization_envelope_and_its_hash() -> None:
    """Materialization rebuilds the envelope from the packet and hashes the
    whole lineage; the record has to come through both unchanged."""

    compiled = _compile()
    packet = compiled["packet"]
    lineage = packet["native_probability_envelope"]["lineage"]
    envelope = build_probability_envelope(packet)

    assert envelope.lineage["native_system_probabilities_json"] == lineage[
        "native_system_probabilities_json"
    ]
    assert envelope.to_dict() == packet["native_probability_envelope"]
    # The record changes the envelope's content hash but neither the
    # posterior nor the checkpoint: both were already bound to these vectors
    # through the evidence hash.
    assert envelope.posterior_sha256 == packet["native_probability_envelope"][
        "posterior_sha256"
    ]
    without = dict(envelope.lineage)
    del without["native_system_probabilities_json"]
    stripped = build_probability_envelope({
        **packet,
        "native_probability_envelope": {
            **packet["native_probability_envelope"],
            "lineage": without,
        },
    })
    assert stripped.checkpoint_id == envelope.checkpoint_id
    assert stripped.posterior_sha256 == envelope.posterior_sha256
    assert stripped.content_sha256 != envelope.content_sha256
