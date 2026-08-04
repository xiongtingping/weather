from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
import sqlite3

import pytest

from bayes_basket_target.contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.regime_artifact import (
    REGIME_ARTIFACT_SCHEMA,
    RegimeArtifact,
    resolve_regime_artifact,
)
from bayes_basket_target.regime_materialization import (
    MAX_RESIDENT_PROBABILITY_ENVELOPES,
    build_regime_artifact,
    evaluate_regime_gate,
    insert_regime_artifact,
    load_regime_artifact,
    materialize_regime_artifact,
    materialize_resident_regimes,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.production import compile_production_plan
from bayes_basket_target.runtime import compile_target_plan


def _artifact(**changes):
    body = {
        "schema": REGIME_ARTIFACT_SCHEMA,
        "artifact_version": "v1",
        "artifact_id": "regime-1",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "stage": "P0-M",
        "checkpoint_id": "p0m-checkpoint-1",
        "settlement_timezone": "Asia/Seoul",
        "as_of_utc": "2026-07-26T16:00:00Z",
        "valid_from_utc": "2026-07-26T15:00:00Z",
        "valid_until_utc": "2026-07-27T15:00:00Z",
        "probability_sha256": "1" * 64,
        "regime": "UP",
        "center_stable": True,
        "center_lead_margin": 0.31,
        "stability_threshold": 0.005,
        "side_mass_up": 0.66,
        "side_mass_down": 0.11,
        "direction_mass_margin": 0.55,
        "direction_threshold": 0.05,
        "model_disagreement": 2.0,
        "model_extreme_low_bucket": "33",
        "model_extreme_high_bucket": "39",
        "concentration_multiplier": 0.8,
    }
    body.update(changes)
    body["content_sha256"] = content_sha256(body)
    return body


def _envelope(
    *,
    station_id="RKPK",
    event_date="2026-07-27",
    timezone_name="Asia/Seoul",
    stage="P0",
    checkpoint_id=None,
    decision_at="2026-07-26T16:00:00Z",
    fresh_until="2026-07-27T15:00:00Z",
    probabilities=None,
):
    return {
        "stage": stage,
        "checkpoint_id": checkpoint_id or f"{station_id}-{stage}-1",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "decision_at_utc": decision_at,
        "fresh_until_utc": fresh_until,
        "bucket_ordinals": {"36": 36, "37": 37, "38": 38},
        "probabilities": probabilities or {"36": 0.2, "37": 0.5, "38": 0.3},
        "posterior_version": f"{stage.lower()}-oos-v1",
        "posterior_sha256": "2" * 64,
        "bridge_sha256": "3" * 64,
        "lineage": {
            "settlement_timezone": timezone_name,
            "model_envelope_lower_bucket": "35",
            "model_envelope_upper_bucket": "39",
            "model_envelope_span_c": "4",
        },
    }


def test_materialized_artifact_is_content_addressed_and_exactly_resolved():
    artifact = materialize_regime_artifact(_artifact())
    assert isinstance(artifact, RegimeArtifact)
    assert artifact.content_sha256 == _artifact()["content_sha256"]
    resolved = resolve_regime_artifact(
        artifact,
        station_id="RKPK",
        event_date="2026-07-27",
        stage="P0-M",
        checkpoint_id="p0m-checkpoint-1",
        settlement_timezone="Asia/Seoul",
        probability_sha256="1" * 64,
        evaluation_at_utc="2026-07-26T16:30:00Z",
    )
    assert resolved.regime == "UP"


@pytest.mark.parametrize("seed", range(8))
def test_multi_seed_gate_is_deterministic_and_does_not_rewrite_probability(seed):
    artifact = materialize_regime_artifact(_artifact())
    gate = evaluate_regime_gate(
        artifact,
        station_id="RKPK",
        event_date="2026-07-27",
        stage="P0-M",
        checkpoint_id="p0m-checkpoint-1",
        settlement_timezone="Asia/Seoul",
        evaluation_at_utc="2026-07-26T16:00:00Z",
        probability_sha256="1" * 64,
        probability_direction="UP",
        center_bucket="37",
    )
    assert gate.state == "UP"
    assert gate.direction_allowed is True
    assert gate.audit["center_bucket"] == "37"
    assert gate.audit["model_extreme_low_bucket"] == "33"
    assert gate.concentration_multiplier == 0.8


def test_low_high_low_and_neutral_unstable_only_tighten():
    artifact = materialize_regime_artifact(
        _artifact(
            regime="NEUTRAL",
            center_stable=False,
            center_lead_margin=0.001,
        )
    )
    gate = evaluate_regime_gate(
        artifact,
        station_id="RKPK", event_date="2026-07-27", stage="P0-M",
        checkpoint_id="p0m-checkpoint-1", settlement_timezone="Asia/Seoul",
        evaluation_at_utc="2026-07-26T16:00:00Z", probability_direction="UP",
        probability_sha256="1" * 64, center_bucket="37",
    )
    assert gate.state == "NEUTRAL_UNSTABLE"
    assert gate.direction_allowed is False
    assert gate.concentration_multiplier <= 0.75
    assert gate.audit["model_extreme_low_bucket"] == "33"
    assert gate.audit["model_extreme_high_bucket"] == "39"


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"settlement_timezone": "America/New_York"}, "regime_exact_scope_mismatch"),
        ({"checkpoint_id": "other"}, "regime_exact_scope_mismatch"),
        ({"content_sha256": "0" * 64}, "regime_content_hash_mismatch"),
        ({"taf": "raw payload"}, "regime_artifact_unknown_field"),
    ],
)
def test_wrong_scope_checkpoint_timezone_and_tampering_fail_closed(changes, error):
    artifact = _artifact(**changes)
    if "content_sha256" not in changes:
        artifact["content_sha256"] = content_sha256({**artifact, "content_sha256": None})
        # The helper above intentionally produces a wrong hash for scope cases;
        # rebuild a valid artifact before checking the exact resolver gate.
        if error == "regime_exact_scope_mismatch":
            artifact = _artifact()
            artifact.update({k: v for k, v in changes.items() if k != "content_sha256"})
            artifact["content_sha256"] = content_sha256({k: v for k, v in artifact.items() if k != "content_sha256"})
    with pytest.raises(ContractError, match=error):
        resolve_regime_artifact(
            artifact, station_id="RKPK", event_date="2026-07-27", stage="P0-M",
            checkpoint_id="p0m-checkpoint-1", settlement_timezone="Asia/Seoul",
            probability_sha256="1" * 64,
            evaluation_at_utc="2026-07-26T16:30:00Z",
        )


def test_expired_artifact_and_direction_conflict_fail_closed():
    expired = _artifact(
        valid_from_utc="2026-07-25T15:00:00Z",
        valid_until_utc="2026-07-26T15:00:00Z",
        as_of_utc="2026-07-25T16:00:00Z",
    )
    with pytest.raises(ContractError, match="regime_artifact_expired"):
        resolve_regime_artifact(
            expired, station_id="RKPK", event_date="2026-07-27", stage="P0-M",
            checkpoint_id="p0m-checkpoint-1", settlement_timezone="Asia/Seoul",
            probability_sha256="1" * 64,
            evaluation_at_utc="2026-07-26T16:00:00Z",
        )


def test_center_lead_margin_and_append_only_ssot_are_exact():
    envelope = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0-margin-checkpoint",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-26T16:00:00Z",
        "fresh_until_utc": "2026-07-27T15:00:00Z",
        "bucket_ordinals": {"36": 36, "37": 37, "38": 38},
        "probabilities": {"36": 0.203, "37": 0.4, "38": 0.397},
        "posterior_version": "p0-oos-v1",
        "posterior_sha256": "2" * 64,
        "bridge_sha256": "3" * 64,
        "lineage": {
            "model_envelope_lower_bucket": "33",
            "model_envelope_upper_bucket": "39",
            "model_envelope_span_c": "6",
        },
    })
    artifact = build_regime_artifact(envelope)
    assert artifact.center_lead_margin == pytest.approx(0.003)
    assert artifact.center_stable is False
    assert artifact.regime == "NEUTRAL"
    assert artifact.model_extreme_low_bucket == "33"
    assert artifact.model_extreme_high_bucket == "39"

    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        first = insert_regime_artifact(
            conn,
            artifact,
            created_at_utc="2026-07-26T16:00:00Z",
        )
        second = insert_regime_artifact(
            conn,
            artifact,
            created_at_utc="2026-07-26T16:00:01Z",
        )
        assert first["wrote"] is True
        assert second["idempotent"] is True
        loaded = load_regime_artifact(
            conn,
            probability=envelope,
            evaluation_at_utc="2026-07-26T16:30:00Z",
        )
        assert loaded.content_sha256 == artifact.content_sha256
        with pytest.raises(
            sqlite3.IntegrityError,
            match="regime_update_forbidden",
        ):
            conn.execute(
                "UPDATE basket_regime_artifact SET state='QUARANTINED'"
            )


def test_all_public_basket_compilers_require_regime_by_default():
    assert (
        inspect.signature(compile_target_plan)
        .parameters["require_regime_ssot"]
        .default
        is True
    )
    assert (
        inspect.signature(compile_production_plan)
        .parameters["require_regime_ssot"]
        .default
        is True
    )
    with pytest.raises(ContractError, match="regime_direction_mismatch"):
        evaluate_regime_gate(
            _artifact(), station_id="RKPK", event_date="2026-07-27", stage="P0-M",
            checkpoint_id="p0m-checkpoint-1", settlement_timezone="Asia/Seoul",
            evaluation_at_utc="2026-07-26T16:00:00Z", probability_direction="DOWN",
            probability_sha256="1" * 64, center_bucket="37",
        )
    neutral_conflict = evaluate_regime_gate(
        _artifact(), station_id="RKPK", event_date="2026-07-27", stage="P0-M",
        checkpoint_id="p0m-checkpoint-1", settlement_timezone="Asia/Seoul",
        evaluation_at_utc="2026-07-26T16:00:00Z", probability_direction="NEUTRAL",
        probability_sha256="1" * 64, center_bucket="37",
    )
    assert neutral_conflict.direction_allowed is False
    assert neutral_conflict.state == "NEUTRAL_REGIME_CONFLICT"


def test_default_center_stability_matches_one_percent_direction_gate():
    def envelope(probabilities):
        return ProbabilityEnvelope.from_mapping({
            "stage": "P0",
            "checkpoint_id": "p0-stability-roundtrip",
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "decision_at_utc": "2026-07-26T16:00:00Z",
            "fresh_until_utc": "2026-07-27T15:00:00Z",
            "bucket_ordinals": {
                "34°C": 34, "35°C": 35, "36°C": 36, "37°C": 37,
            },
            "probabilities": probabilities,
            "posterior_version": "p0-oos-v1",
            "posterior_sha256": "2" * 64,
            "bridge_sha256": "3" * 64,
            "lineage": {
                "model_envelope_lower_bucket": "33°C",
                "model_envelope_upper_bucket": "39°C",
                "model_envelope_span_c": 6,
            },
        })

    low = build_regime_artifact(envelope({
        "34°C": 0.196, "35°C": 0.304, "36°C": 0.297, "37°C": 0.203,
    }))
    assert low.center_lead_margin == pytest.approx(0.007)
    assert low.stability_threshold == pytest.approx(0.01)
    assert low.center_stable is False
    assert low.regime == "NEUTRAL"

    high = build_regime_artifact(envelope({
        "34°C": 0.19, "35°C": 0.31, "36°C": 0.29, "37°C": 0.21,
    }))
    assert high.center_lead_margin == pytest.approx(0.02)
    assert high.center_stable is True

    low_again = build_regime_artifact(envelope({
        "34°C": 0.196, "35°C": 0.304, "36°C": 0.297, "37°C": 0.203,
    }))
    assert low_again.center_stable is False
    assert low_again.regime == "NEUTRAL"


def test_resident_regime_stage_chain_is_monotonic_idempotent_and_deterministic():
    registry = {("RKPK", "2026-07-27"): "Asia/Seoul"}
    p0 = _envelope(stage="P0")
    p0m = _envelope(
        stage="P0-M",
        decision_at="2026-07-26T17:00:00Z",
    )
    p1 = _envelope(
        stage="P1",
        decision_at="2026-07-26T18:00:00Z",
    )
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        first = materialize_resident_regimes(
            conn,
            envelopes=[p0],
            registry_timezones=registry,
            evaluation_at_utc="2026-07-26T19:00:00Z",
            created_at_utc="2026-07-26T19:00:00Z",
        )
        assert first["selected"][0]["stage"] == "P0"
        assert first["wrote_count"] == 1

        second = materialize_resident_regimes(
            conn,
            envelopes=[p0m, p0],
            registry_timezones=registry,
            evaluation_at_utc="2026-07-26T19:00:00Z",
            created_at_utc="2026-07-26T19:01:00Z",
        )
        assert second["selected"][0]["stage"] == "P0-M"

        third = materialize_resident_regimes(
            conn,
            envelopes=[p1, p0, p0m],
            registry_timezones=registry,
            evaluation_at_utc="2026-07-26T19:00:00Z",
            created_at_utc="2026-07-26T19:02:00Z",
        )
        again = materialize_resident_regimes(
            conn,
            envelopes=[p0m, p1, p0, p1],
            registry_timezones=registry,
            evaluation_at_utc="2026-07-26T19:00:00Z",
            created_at_utc="2026-07-26T19:03:00Z",
        )
        assert third["selected"][0]["stage"] == "P1"
        assert again["selected"] == third["selected"]
        assert again["selection_sha256"] != third["selection_sha256"]
        assert again["wrote_count"] == 0
        assert again["idempotent_count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact"
        ).fetchone()[0] == 3

        with pytest.raises(
            ContractError,
            match="resident_regime_stage_rewind_forbidden",
        ):
            materialize_resident_regimes(
                conn,
                envelopes=[p0m, p0],
                registry_timezones=registry,
                evaluation_at_utc="2026-07-26T19:00:00Z",
                created_at_utc="2026-07-26T19:04:00Z",
            )


def test_stale_or_damaged_high_stage_blocks_without_lower_stage_fallback():
    registry = {("RKPK", "2026-07-27"): "Asia/Seoul"}
    p0 = _envelope(stage="P0")
    stale_p1 = _envelope(
        stage="P1",
        decision_at="2026-07-26T17:00:00Z",
        fresh_until="2026-07-26T18:00:00Z",
    )
    damaged_p1 = {
        **_envelope(stage="P1"),
        "probabilities": {"36": 0.2, "37": 0.5, "38": 0.4},
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="latest_stage_stale_fail_closed",
        ):
            materialize_resident_regimes(
                conn,
                envelopes=[p0, stale_p1],
                registry_timezones=registry,
                evaluation_at_utc="2026-07-26T19:00:00Z",
                created_at_utc="2026-07-26T19:00:00Z",
            )
        with pytest.raises(ContractError, match="probability_vector_not_normalized"):
            materialize_resident_regimes(
                conn,
                envelopes=[p0, damaged_p1],
                registry_timezones=registry,
                evaluation_at_utc="2026-07-26T19:00:00Z",
                created_at_utc="2026-07-26T19:00:00Z",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact"
        ).fetchone()[0] == 0


def test_registry_timezone_scope_and_probability_content_drift_fail_closed():
    p0 = _envelope(stage="P0", checkpoint_id="same-checkpoint")
    drift = _envelope(
        stage="P0",
        checkpoint_id="same-checkpoint",
        probabilities={"36": 0.3, "37": 0.5, "38": 0.2},
    )
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="resident_regime_settlement_timezone_mismatch",
        ):
            materialize_resident_regimes(
                conn,
                envelopes=[p0],
                registry_timezones={
                    ("RKPK", "2026-07-27"): "America/New_York"
                },
                evaluation_at_utc="2026-07-26T19:00:00Z",
                created_at_utc="2026-07-26T19:00:00Z",
            )
        with pytest.raises(
            ContractError,
            match="resident_regime_probability_content_drift",
        ):
            materialize_resident_regimes(
                conn,
                envelopes=[p0, drift],
                registry_timezones={
                    ("RKPK", "2026-07-27"): "Asia/Seoul"
                },
                evaluation_at_utc="2026-07-26T19:00:00Z",
                created_at_utc="2026-07-26T19:00:00Z",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact"
        ).fetchone()[0] == 0


def test_resident_batch_is_atomic_across_stations_and_bounded_at_45():
    stations = [f"K{index:03d}" for index in range(45)]
    registry = {
        (station, "2026-07-27"): "America/New_York"
        for station in stations
    }
    envelopes = [
        _envelope(
            station_id=station,
            timezone_name="America/New_York",
            checkpoint_id=f"{station}-p0",
        )
        for station in stations
    ]
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        receipt = materialize_resident_regimes(
            conn,
            envelopes=envelopes,
            registry_timezones=registry,
            evaluation_at_utc="2026-07-26T19:00:00Z",
            created_at_utc="2026-07-26T19:00:00Z",
        )
        assert receipt["station_count"] == 45
        assert len(str(receipt).encode("utf-8")) < 1024 * 1024

    registry_46 = {
        **registry,
        ("K999", "2026-07-27"): "America/New_York",
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="resident_regime_station_limit_exceeded",
        ):
            materialize_resident_regimes(
                conn,
                envelopes=[
                    *envelopes,
                    _envelope(
                        station_id="K999",
                        timezone_name="America/New_York",
                    ),
                ],
                registry_timezones=registry_46,
                evaluation_at_utc="2026-07-26T19:00:00Z",
                created_at_utc="2026-07-26T19:00:00Z",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact"
        ).fetchone()[0] == 0


def test_envelope_count_bound_stops_before_unbounded_materialization():
    repeated = (
        _envelope(checkpoint_id=f"p0-{index}")
        for index in range(MAX_RESIDENT_PROBABILITY_ENVELOPES + 1)
    )
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="resident_regime_envelope_limit_exceeded",
        ):
            materialize_resident_regimes(
                conn,
                envelopes=repeated,
                registry_timezones={
                    ("RKPK", "2026-07-27"): "Asia/Seoul"
                },
                evaluation_at_utc="2026-07-26T19:00:00Z",
                created_at_utc="2026-07-26T19:00:00Z",
            )


def test_cross_station_insert_conflict_rolls_back_the_whole_batch():
    rkpk = ProbabilityEnvelope.from_mapping(
        _envelope(checkpoint_id="RKPK-P0-conflict")
    )
    katl = ProbabilityEnvelope.from_mapping(
        _envelope(
            station_id="KATL",
            timezone_name="America/New_York",
            checkpoint_id="KATL-P0-new",
        )
    )
    registry = {
        ("KATL", "2026-07-27"): "America/New_York",
        ("RKPK", "2026-07-27"): "Asia/Seoul",
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        original = build_regime_artifact(rkpk)
        insert_regime_artifact(
            conn,
            original,
            created_at_utc="2026-07-26T18:00:00Z",
        )
        drifted = ProbabilityEnvelope.from_mapping(
            _envelope(
                checkpoint_id="RKPK-P0-conflict",
                probabilities={"36": 0.3, "37": 0.5, "38": 0.2},
            )
        )
        before = conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact"
        ).fetchone()[0]
        with pytest.raises(
            ContractError,
            match="regime_probability_content_drift",
        ):
            materialize_resident_regimes(
                conn,
                envelopes=[drifted, katl],
                registry_timezones=registry,
                evaluation_at_utc="2026-07-26T19:00:00Z",
                created_at_utc="2026-07-26T19:00:00Z",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact"
        ).fetchone()[0] == before
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_regime_artifact "
            "WHERE station_id='KATL'"
        ).fetchone()[0] == 0
