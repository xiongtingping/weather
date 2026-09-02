"""Per-stage evidence roots and the pre-observation bucket floor.

P0 opens on models, P0-M re-confirms on TAF, P1 adjusts on observed METAR.
The ledger was built for the last of those alone, so these pin what the two
earlier stages are entitled to stand on -- and what stops a stage that has
observed nothing from opening anywhere it likes.
"""

from __future__ import annotations

import pytest

from bayes_basket_target.stage_evidence import (
    SOFT_BOUND_KIND,
    StageEvidenceError,
    assert_intent_buckets_within_bound,
    build_model_envelope_soft_bound,
    build_stage_evidence_root,
)


ORDINALS = {"33": 0, "34": 1, "35": 2, "36": 3, "37": 4}
P0_LINEAGE = {
    "native_selection_evidence_sha256": "a" * 64,
    "native_cross_activation_sha256": "b" * 64,
    "native_cross_ensemble_artifact_sha256": "c" * 64,
    "model_envelope_lower_ordinal": "1",
    "model_envelope_upper_ordinal": "3",
}
P0M_LINEAGE = {
    "taf_revision_proof_sha256": "d" * 64,
    "taf_revision_artifact_sha256": "e" * 64,
    "taf_revision_activation_id": "activation-1",
    "forecast_revision_id": "revision-1",
    "model_envelope_lower_ordinal": "1",
    "model_envelope_upper_ordinal": "3",
}
# What ``materialize_p0m_observed`` actually writes: the forecast seated on
# the P0 root with an identity likelihood, and no artifact of any kind.
P0M_OBSERVED_LINEAGE = {
    "revision_mode": "observed_without_artifact",
    "taf_revision_state": "OBSERVED_NO_CALIBRATED_ARTIFACT",
    "taf_likelihood_mode": "IDENTITY_NO_ARTIFACT",
    "forecast_revision_id": "taf-revision-1",
    "forecast_source_object_sha256": "0" * 64,
    "taf_observed_tx_c": "22",
    "p0_root_checkpoint_id": "p0-native-1",
    "p0_root_content_sha256": "1" * 64,
    "p0_root_posterior_sha256": "2" * 64,
    "model_envelope_lower_ordinal": "1",
    "model_envelope_upper_ordinal": "3",
}


def _root(stage: str, lineage: dict) -> dict:
    return build_stage_evidence_root(
        probability_stage=stage,
        station_id="ZUUU",
        event_date="2026-08-12",
        settlement_timezone="Asia/Shanghai",
        checkpoint_id="checkpoint-1",
        posterior_sha256="f" * 64,
        lineage=lineage,
    )


def test_p0_opens_a_position_on_the_native_ensemble() -> None:
    root = _root("P0", P0_LINEAGE)

    assert root["position_role"] == "OPEN"
    assert root["bound_kind"] == SOFT_BOUND_KIND
    assert set(root["evidence"]) == {
        "native_selection_evidence_sha256",
        "native_cross_activation_sha256",
        "native_cross_ensemble_artifact_sha256",
    }


def test_p0m_adjusts_on_the_taf_revision_proof() -> None:
    root = _root("P0-M", P0M_LINEAGE)

    assert root["position_role"] == "ADJUST"
    assert root["evidence"]["forecast_revision_id"] == "revision-1"


def test_p0m_seated_on_the_forecast_alone_roots_on_what_it_recorded() -> None:
    """The observed route is every P0-M in production; it needs no witness.

    ``materialize_p0m_observed`` seats the stage on the forecast with an
    identity likelihood and says so under ``revision_mode``.  Asked for the
    member route's issuance witness -- which it structurally never carries --
    no P0-M root was ever built: 10,552 P0 roots and none for P0-M, and on
    2026-09-01 the forward day's stage-open refused 808 times on it.
    """

    root = _root("P0-M", P0M_OBSERVED_LINEAGE)

    assert root["position_role"] == "ADJUST"
    assert root["bound_kind"] == SOFT_BOUND_KIND
    assert set(root["evidence"]) == {
        "forecast_revision_id",
        "forecast_source_object_sha256",
        "taf_revision_state",
        "taf_observed_tx_c",
        "p0_root_content_sha256",
        "p0_root_posterior_sha256",
    }
    assert root["evidence"]["taf_revision_state"] == (
        "OBSERVED_NO_CALIBRATED_ARTIFACT"
    )
    # A different forecast revision is different evidence.
    other = _root(
        "P0-M", {**P0M_OBSERVED_LINEAGE, "forecast_revision_id": "taf-revision-2"}
    )
    assert other["content_sha256"] != root["content_sha256"]


def test_an_observed_p0m_missing_its_root_binding_is_still_refused() -> None:
    lineage = {**P0M_OBSERVED_LINEAGE}
    del lineage["p0_root_content_sha256"]

    with pytest.raises(
        StageEvidenceError, match="p0_root_content_sha256_missing"
    ):
        _root("P0-M", lineage)


def test_a_p0m_that_names_no_route_is_still_asked_for_the_witness() -> None:
    # Neither a transport proof nor the observed route's self-description:
    # this is the member route's shape, and the member route must carry the
    # issuance witness that drove it.
    lineage = {
        key: value for key, value in P0M_OBSERVED_LINEAGE.items()
        if key != "revision_mode"
    }

    with pytest.raises(
        StageEvidenceError, match="taf_envelope_witness_json_missing"
    ):
        _root("P0-M", lineage)


def test_a_stage_missing_its_own_evidence_is_refused() -> None:
    lineage = {**P0_LINEAGE}
    del lineage["native_selection_evidence_sha256"]

    with pytest.raises(
        StageEvidenceError, match="native_selection_evidence_sha256_missing"
    ):
        _root("P0", lineage)


def test_the_evidence_root_is_content_addressed() -> None:
    assert _root("P0", P0_LINEAGE)["content_sha256"] == (
        _root("P0", P0_LINEAGE)["content_sha256"]
    )
    assert _root("P0", P0_LINEAGE)["content_sha256"] != (
        _root("P0-M", P0M_LINEAGE)["content_sha256"]
    )


def test_the_soft_bound_admits_only_what_the_models_point_at() -> None:
    bound = build_model_envelope_soft_bound(
        probability_stage="P0",
        lineage=P0_LINEAGE,
        bucket_ordinals=ORDINALS,
    )

    assert bound["admitted_buckets"] == ["34", "35", "36"]
    assert bound["excluded_buckets"] == ["33", "37"]


def test_a_stage_without_an_envelope_gets_no_bound_rather_than_a_free_one() -> None:
    # Refusing is the point: an absent envelope must not read as "no limit",
    # which is how a pre-observation stage would open anywhere at all.
    lineage = {
        key: value for key, value in P0_LINEAGE.items()
        if not key.startswith("model_envelope")
    }

    with pytest.raises(
        StageEvidenceError, match="model_envelope_missing"
    ):
        build_model_envelope_soft_bound(
            probability_stage="P0",
            lineage=lineage,
            bucket_ordinals=ORDINALS,
        )


def test_p1_is_not_offered_a_soft_bound() -> None:
    # An observed high has ruled buckets out; a forecast envelope must never
    # be able to put them back.
    with pytest.raises(
        StageEvidenceError, match="soft_bound_not_applicable"
    ):
        build_model_envelope_soft_bound(
            probability_stage="P1",
            lineage=P0_LINEAGE,
            bucket_ordinals=ORDINALS,
        )


def test_a_basket_reaching_outside_the_bound_is_refused() -> None:
    bound = build_model_envelope_soft_bound(
        probability_stage="P0",
        lineage=P0_LINEAGE,
        bucket_ordinals=ORDINALS,
    )

    assert_intent_buckets_within_bound(
        bound=bound, intent_buckets=["34", "35"]
    )
    with pytest.raises(
        StageEvidenceError, match="intent_outside_bound:37"
    ):
        assert_intent_buckets_within_bound(
            bound=bound, intent_buckets=["35", "37"]
        )


def test_a_committed_envelope_naming_only_buckets_still_gets_a_bound() -> None:
    # 18 of 20 stations carrying a P0 today predate the ordinal keys and name
    # the boundary buckets only.  Refusing them would leave the fleet with no
    # bound at all until every envelope is rewritten.
    lineage = {
        key: value for key, value in P0_LINEAGE.items()
        if not key.endswith("_ordinal")
    }
    lineage["model_envelope_lower_bucket"] = "34"
    lineage["model_envelope_upper_bucket"] = "36"

    bound = build_model_envelope_soft_bound(
        probability_stage="P0",
        lineage=lineage,
        bucket_ordinals=ORDINALS,
    )

    assert bound["admitted_buckets"] == ["34", "35", "36"]


def test_an_unresolvable_envelope_bucket_name_is_refused() -> None:
    lineage = {
        key: value for key, value in P0_LINEAGE.items()
        if not key.endswith("_ordinal")
    }
    lineage["model_envelope_lower_bucket"] = "34"
    lineage["model_envelope_upper_bucket"] = "99"

    with pytest.raises(
        StageEvidenceError, match="model_envelope_invalid"
    ):
        build_model_envelope_soft_bound(
            probability_stage="P0",
            lineage=lineage,
            bucket_ordinals=ORDINALS,
        )


def _memory_conn():
    import sqlite3
    from bayes_basket_target.ssot import apply_production_ssot_schema
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    return conn


def test_persisting_a_p0_root_and_its_soft_bound_is_idempotent() -> None:
    from bayes_basket_target.stage_evidence import persist_stage_evidence

    conn = _memory_conn()
    root = _root("P0", P0_LINEAGE)
    bound = build_model_envelope_soft_bound(
        probability_stage="P0",
        lineage=P0_LINEAGE,
        bucket_ordinals=ORDINALS,
    )
    with conn:
        first = persist_stage_evidence(
            conn,
            evidence_root=root,
            soft_bound=bound,
            created_at_utc="2026-08-11T10:00:00Z",
        )
        # A second pass over an unchanged envelope must not read as a new
        # position -- the same evidence hashes the same and writes nothing.
        second = persist_stage_evidence(
            conn,
            evidence_root=root,
            soft_bound=bound,
            created_at_utc="2026-08-11T10:05:00Z",
        )

    assert first["state"] == "committed"
    assert first["wrote_evidence_root"] is True
    assert second["state"] == "idempotent"
    assert second["wrote_evidence_root"] is False
    assert second["evidence_root_id"] == first["evidence_root_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_target_stage_evidence_roots"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_target_stage_bound_facts"
    ).fetchone()[0] == 1


def test_a_p1_root_persists_without_a_soft_bound() -> None:
    from bayes_basket_target.stage_evidence import persist_stage_evidence

    conn = _memory_conn()
    root = build_stage_evidence_root(
        probability_stage="P1",
        station_id="ZUUU",
        event_date="2026-08-12",
        settlement_timezone="Asia/Shanghai",
        checkpoint_id="checkpoint-p1",
        posterior_sha256="f" * 64,
        lineage={
            "canonical_observation_event_id": "obs-1",
            "canonical_observation_committed_at": "2026-08-12T02:00:00Z",
            "hard_bound_id": "hardbound-1",
            "source_lane": "AWC",
            "report_type": "METAR",
        },
    )
    with conn:
        receipt = persist_stage_evidence(
            conn,
            evidence_root=root,
            created_at_utc="2026-08-11T10:00:00Z",
        )

    assert receipt["wrote_soft_bound"] is False
    assert conn.execute(
        "SELECT bound_kind FROM basket_target_stage_evidence_roots"
    ).fetchone()[0] == "OBSERVED_REPORTED_HIGH_HARD"
    # The soft-bound table must never hold a P1 row; its CHECK says so.
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_target_stage_bound_facts"
    ).fetchone()[0] == 0
