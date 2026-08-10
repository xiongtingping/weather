from __future__ import annotations

import copy
import json

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_authority import (
    NativeOosSnapshot,
    pointer_activation_mismatch_reasons,
    pointer_for_scope,
    station_has_pointer,
    validate_native_oos_snapshot_manifest,
)


def _pointer_for(name: str) -> dict:
    identity = {
        "schema": "native_ensemble_artifact_pointer_v1",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "activation_id": f"activation-{name}",
        "cross_ensemble_artifact_id": f"artifact-{name}",
        "cross_artifact_content_sha256": "a" * 64,
        "activation_identity_sha256": "b" * 64,
        "selection_evidence_sha256": "c" * 64,
        "generation": 3,
        "source_candidate_sha256": "d" * 64,
        "source_manifest_sha256": "e" * 64,
        "source_report_sha256": "f" * 64,
        "valid_from_utc": "2026-08-02T16:19:44.680Z",
        "valid_until_utc": "2026-09-01T16:19:44.680000Z",
        "write_policy": "existing_append_only_no_live_only_no_write",
    }
    return {**identity, "pointer_sha256": content_sha256(identity)}


def _manifest(*, extra_bindings: list[dict] | None = None) -> dict:
    cycle = {"ECMWF_IFS_ENS_OPEN_PF50": "18Z", "NOAA_GEFS_05": "18Z"}
    pointer_identity = {
        "schema": "native_ensemble_artifact_pointer_v1",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "activation_id": "activation-eglc",
        "cross_ensemble_artifact_id": "artifact-eglc",
        "cross_artifact_content_sha256": "a" * 64,
        "activation_identity_sha256": "b" * 64,
        "selection_evidence_sha256": "c" * 64,
        "generation": 3,
        "source_candidate_sha256": "d" * 64,
        "source_manifest_sha256": "e" * 64,
        "source_report_sha256": "f" * 64,
        "valid_from_utc": "2026-08-02T16:19:44.680Z",
        "valid_until_utc": "2026-09-01T16:19:44.680000Z",
        "write_policy": "existing_append_only_no_live_only_no_write",
    }
    pointer = {
        **pointer_identity,
        "pointer_sha256": content_sha256(pointer_identity),
    }
    binding_identity = {
        "schema": "native_p0_consumable_binding_v1",
        "as_of_utc": "2026-08-02T16:19:44.680Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "source_linkage_content_sha256": "1" * 64,
        "source_repair_content_sha256": "2" * 64,
        "binding_count": 1,
        "station_ids": ["EGLC"],
        "bindings": [{
            "station_id": "EGLC",
            "season_bin": "JJA",
            "lead_bucket": "d2",
            "run_cycle": "18Z",
            "run_cycle_by_system_json": json.dumps(
                cycle, sort_keys=True, separators=(",", ":")
            ),
            "run_cycle_by_system_sha256": content_sha256(cycle),
            "selection_evidence_sha256": "c" * 64,
            "artifact_pointer": pointer,
        }] + list(extra_bindings or []),
    }
    binding_identity["binding_count"] = len(binding_identity["bindings"])
    binding_identity["station_ids"] = sorted({
        str(row["station_id"]) for row in binding_identity["bindings"]
    })
    binding = {
        **binding_identity,
        "content_sha256": content_sha256(binding_identity),
    }
    identity = {
        "schema": "bayes_basket_target_oos_atomic_snapshot_manifest_v1",
        "generated_at_utc": "2026-08-02T18:44:34.897Z",
        "as_of_utc": "2026-08-02T16:55:16.656Z",
        "read_only": True,
        "network_fetches": 0,
        "writes_target_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "max_retries": 3,
        "canonical_station_count": 1,
        "canonical_station_ids": ["EGLC"],
        "scope_station_ids": {"canonical": ["EGLC"], "native_linkage": ["EGLC"]},
        "scope_fingerprint": "3" * 64,
        "input_artifacts": {},
        "native_p0_binding": binding,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _scope(**overrides) -> dict:
    cycle = {"ECMWF_IFS_ENS_OPEN_PF50": "00Z", "NOAA_GEFS_05": "00Z"}
    row = {
        "station_id": "EGLC",
        "season_bin": "JJA",
        "lead_bucket": "h12_24",
        "run_cycle": "00Z",
        "run_cycle_by_system_json": json.dumps(
            cycle, sort_keys=True, separators=(",", ":")
        ),
        "run_cycle_by_system_sha256": content_sha256(cycle),
        "selection_evidence_sha256": "c" * 64,
        "artifact_pointer": _pointer_for("eglc-h12"),
    }
    row.update(overrides)
    return row


def test_snapshot_validates_and_resolves_exact_scope_pointer() -> None:
    snapshot = validate_native_oos_snapshot_manifest(_manifest())
    assert isinstance(snapshot, NativeOosSnapshot)
    assert snapshot.station_ids == ("EGLC",)
    assert station_has_pointer(snapshot, "eglc")
    assert not station_has_pointer(snapshot, "RKPK")
    assert pointer_for_scope(
        snapshot, "eglc", season_bin="JJA", lead_bucket="d2", run_cycle="18Z"
    )["artifact_pointer"]["activation_id"] == "activation-eglc"


def test_one_station_may_be_attested_at_several_leads() -> None:
    """The ceiling this removes: one binding per station meant one lead.

    A station pinned at d2/18Z could never price its own d1 packet however
    well calibrated, and every artifact trained at another lead was
    unreachable -- P0 was frozen to whatever scope happened to be attested.
    """

    snapshot = validate_native_oos_snapshot_manifest(
        _manifest(extra_bindings=[_scope()])
    )
    assert snapshot.station_ids == ("EGLC",)
    assert len(snapshot.bindings_by_scope) == 2
    assert pointer_for_scope(
        snapshot, "EGLC", season_bin="JJA", lead_bucket="d2", run_cycle="18Z"
    )["artifact_pointer"]["activation_id"] == "activation-eglc"
    assert pointer_for_scope(
        snapshot, "EGLC", season_bin="JJA", lead_bucket="h12_24", run_cycle="00Z"
    )["artifact_pointer"]["activation_id"] == "activation-eglc-h12"


def test_a_scope_with_no_binding_resolves_to_nothing() -> None:
    """Attested elsewhere is not attested here -- the caller must fail closed."""

    snapshot = validate_native_oos_snapshot_manifest(_manifest())
    assert station_has_pointer(snapshot, "EGLC")
    assert pointer_for_scope(
        snapshot, "EGLC", season_bin="JJA", lead_bucket="h12_24", run_cycle="00Z"
    ) is None


def test_two_bindings_for_one_scope_remain_ambiguous_and_fail_closed() -> None:
    """Splitting by scope must not let one scope name two activations."""

    duplicate = _scope(
        lead_bucket="d2",
        run_cycle="18Z",
        artifact_pointer=_pointer_for("eglc-rival"),
    )
    with pytest.raises(ContractError) as excinfo:
        validate_native_oos_snapshot_manifest(
            _manifest(extra_bindings=[duplicate])
        )
    assert "duplicate_scope" in str(excinfo.value)


def test_snapshot_hash_tamper_fails_closed() -> None:
    tampered = copy.deepcopy(_manifest())
    tampered["native_p0_binding"]["bindings"][0]["lead_bucket"] = "d3"
    with pytest.raises(ContractError, match="manifest_hash_invalid"):
        validate_native_oos_snapshot_manifest(tampered)


def test_activation_pointer_reports_identity_mismatch() -> None:
    snapshot = validate_native_oos_snapshot_manifest(_manifest())
    binding = pointer_for_scope(
        snapshot, "EGLC", season_bin="JJA", lead_bucket="d2", run_cycle="18Z"
    )
    reasons = pointer_activation_mismatch_reasons(
        binding,
        activation_id="wrong",
        artifact_id="artifact-eglc",
        activation_sha256="b" * 64,
        artifact_sha256="a" * 64,
        selection_evidence_sha256="c" * 64,
        generation=3,
        season_bin="JJA",
        lead_bucket="d2",
        run_cycle="18Z",
        run_cycle_by_system_json='{"ECMWF_IFS_ENS_OPEN_PF50":"18Z","NOAA_GEFS_05":"18Z"}',
        authority_scope="PRODUCTION_NO_LIVE_ONLY",
    )
    assert reasons == ["activation_id_mismatch"]
