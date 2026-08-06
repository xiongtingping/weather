from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_contract import (
    CONTRACT_VERSION,
    SCHEMA,
    CanonicalContract,
)
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_single_system_oos_artifact import (
    EXPECTED_ELIGIBILITY_COUNT,
    build_single_system_oos_artifacts,
)
from scripts.apply_canonical_contract_schema import migrate as apply_contract_schema


AS_OF = "2026-08-04T00:00:00Z"


def _database(path: Path) -> Path:
    path.touch()
    assert apply_contract_schema(path, apply=True)["state"] == "APPLIED"
    return path


def _member(**overrides: object) -> dict[str, object]:
    identity: dict[str, object] = {
        "station_id": "CYYZ",
        "ensemble_system_id": "NOAA_GEFS_05",
        "model_family": "global_ensemble",
        "provider_id": "NOAA",
        "source_class": "native_official",
        "event_date": "2026-07-31",
        "run_init_utc": "2026-07-29T18:00:00Z",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "member_id": "control",
        "member_role": "CONTROL",
        "extraction_version": "test-v1",
        "settlement_timezone": "America/Toronto",
        "forecast_value": 30.0,
        "raw_receipt_set_sha256": "1" * 64,
        "source_member_fact_sha": "2" * 64,
        "archive_query_at_known": True,
        "archive_query_at_utc": "2026-08-01T00:00:00Z",
        "oos_role": "observational_candidate_not_oos",
        "closed_day": True,
        "strict_asof": False,
    }
    identity = {**identity, **overrides}
    return {**identity, "content_sha256": content_sha256(identity)}


def _truth() -> dict[str, object]:
    identity: dict[str, object] = {
        "truth_id": "truth-CYYZ-2026-07-31",
        "station_id": "CYYZ",
        "event_date": "2026-07-31",
        "truth_revision_no": 0,
        "truth_value": 31.0,
        "truth_content_sha256": "3" * 64,
        "canonical_committed_at_utc": "2026-08-01T05:00:00Z",
        "station_day_end_utc": "2026-08-01T04:00:00Z",
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "America/Toronto",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _arrival() -> dict[str, object]:
    identity: dict[str, object] = {
        "native_arrival_id": "noaa-gefs-test-object",
        "ensemble_system_id": "NOAA_GEFS_05",
        "provider_id": "NOAA",
        "source_class": "native_official",
        "native_object_id": "gefs.test.native.object",
        "run_init_utc": "2026-07-29T18:00:00Z",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "raw_object_sha256": "4" * 64,
        "published_at_known": True,
        "published_at_utc": "2026-07-29T21:00:00Z",
        "received_at_known": True,
        "received_at_utc": "2026-07-29T21:02:00Z",
        "native_arrival_indexed": True,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _receipt_ref(
    ordinal: int,
    *,
    evidence_sha256: str,
    selector_sha256: str,
) -> dict[str, object]:
    identity: dict[str, object] = {
        "native_object_id": "gefs.test.native.object",
        "raw_object_sha256": "4" * 64,
        "receipt_evidence_at_utc": "2026-07-29T21:03:00Z",
        "receipt_evidence_content_sha256": evidence_sha256,
        "extraction_selector_sha256": selector_sha256,
    }
    return {"ordinal": ordinal, **identity}


def _manifest(
    member: dict[str, object], refs: list[dict[str, object]]
) -> dict[str, object]:
    identity: dict[str, object] = {
        "ensemble_member_content_sha256": member["content_sha256"],
        "source_raw_receipt_set_sha256": member["raw_receipt_set_sha256"],
        "required_receipt_refs": refs,
        "required_receipt_ref_set_sha256": content_sha256(refs),
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _binding(
    member: dict[str, object],
    arrival: dict[str, object],
    manifest: dict[str, object],
    ordinals: list[int],
) -> dict[str, object]:
    identity: dict[str, object] = {
        "ensemble_member_content_sha256": member["content_sha256"],
        "native_arrival_content_sha256": arrival["content_sha256"],
        "receipt_manifest_content_sha256": manifest["content_sha256"],
        "manifest_entry_ordinals": ordinals,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _ingest_candidate(path: Path) -> None:
    identity: dict[str, object] = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": AS_OF,
        "builder_id": "test-single-system-canonical-catalog",
        "model_facts": [],
        "ensemble_member_facts": [_member()],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [_truth()],
        "residual_facts": [],
        "activation_facts": [],
    }
    contract = CanonicalContract.from_mapping(
        {**identity, "content_sha256": content_sha256(identity)}
    )
    with sqlite3.connect(path, isolation_level=None) as conn:
        receipt = ingest_canonical_contract(
            conn, contract, commit=True, ingested_at_utc=AS_OF
        )
    assert receipt["inserted_count"] == 2


def _ingest_bound_candidate(path: Path) -> None:
    member = _member()
    arrival = _arrival()
    refs = [
        _receipt_ref(0, evidence_sha256="5" * 64, selector_sha256="6" * 64)
    ]
    manifest = _manifest(member, refs)
    identity: dict[str, object] = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": AS_OF,
        "builder_id": "test-single-system-bound-catalog",
        "model_facts": [],
        "ensemble_member_facts": [member],
        "native_arrival_facts": [arrival],
        "ensemble_member_receipt_manifest_facts": [manifest],
        "ensemble_member_arrival_binding_facts": [
            _binding(member, arrival, manifest, [0])
        ],
        "truth_facts": [_truth()],
        "residual_facts": [],
        "activation_facts": [],
    }
    with sqlite3.connect(path, isolation_level=None) as conn:
        receipt = ingest_canonical_contract(
            conn,
            {**identity, "content_sha256": content_sha256(identity)},
            commit=True,
            ingested_at_utc=AS_OF,
        )
    assert receipt["inserted_count"] == 5


def test_builder_reads_v6_member_and_truth_but_keeps_all_55_lineage_blocked(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "ssot.db")
    _ingest_candidate(database)
    before = database.read_bytes()
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        manifest = build_single_system_oos_artifacts(
            conn, compiled_at_utc=AS_OF
        )
    assert database.read_bytes() == before
    assert manifest["artifact_count"] == 0
    assert manifest["blocked_scope_count"] == 55
    assert manifest["blocked_scope_count_by_system"] == EXPECTED_ELIGIBILITY_COUNT
    assert manifest["legacy_audit_read"] is False
    assert manifest["legacy_metrics_reused"] is False
    assert manifest["member_arrival_binding_fact_type_present"] is True
    assert manifest["visible_canonical_fact_counts"] == {
        "ensemble_member": 1,
        "truth": 1,
    }
    assert all(row["blockers"] == ["LINEAGE_MISMATCH"] for row in manifest["blocked_scopes"])
    cyyz = next(
        row
        for row in manifest["blocked_scopes"]
        if row["station_id"] == "CYYZ"
        and row["ensemble_system_id"] == "NOAA_GEFS_05"
    )
    assert cyyz["canonical_diagnostics"]["candidate_member_fact_count"] == 1
    assert cyyz["canonical_diagnostics"]["strict_member_cohort_date_count"] == 0
    assert cyyz["canonical_diagnostics"]["indexed_native_arrival_fact_count"] == 0
    assert cyyz["canonical_diagnostics"]["member_arrival_binding_fact_count"] == 0
    assert cyyz["inferred_member_arrival_join"] is False


def test_empty_v6_still_returns_exact_registry_and_mutable_connection_rejected(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "ssot.db")
    with sqlite3.connect(database) as conn:
        with pytest.raises(ContractError, match="read_only_connection_required"):
            build_single_system_oos_artifacts(conn, compiled_at_utc=AS_OF)
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        first = build_single_system_oos_artifacts(conn, compiled_at_utc=AS_OF)
        second = build_single_system_oos_artifacts(conn, compiled_at_utc=AS_OF)
    assert first == second
    assert first["blocked_scope_count"] == 55
    assert first["visible_canonical_fact_counts"] == {}
    assert all(row["artifact_count"] == 0 for row in first["blocked_scopes"])


def test_builder_reads_explicit_binding_without_inferred_join(tmp_path: Path) -> None:
    database = _database(tmp_path / "bound.db")
    _ingest_bound_candidate(database)
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        manifest = build_single_system_oos_artifacts(conn, compiled_at_utc=AS_OF)
    cyyz = next(
        row
        for row in manifest["blocked_scopes"]
        if row["station_id"] == "CYYZ"
        and row["ensemble_system_id"] == "NOAA_GEFS_05"
    )
    assert cyyz["canonical_diagnostics"]["member_arrival_binding_fact_count"] == 1
    assert cyyz["canonical_diagnostics"]["indexed_native_arrival_fact_count"] == 1
    assert cyyz["inferred_member_arrival_join"] is False
    assert manifest["artifact_count"] == 0


def test_builder_rejects_non_v7_database(tmp_path: Path) -> None:
    database = tmp_path / "invalid.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE canonical_ssot_fact(fact_id TEXT)")
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        with pytest.raises(ContractError, match="canonical_ssot_v7_required"):
            build_single_system_oos_artifacts(conn, compiled_at_utc=AS_OF)


def test_reader_does_not_treat_one_binding_as_complete_receipt_coverage(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "partial-receipt-coverage.db")
    members: list[dict[str, object]] = []
    manifests: list[dict[str, object]] = []
    bindings: list[dict[str, object]] = []
    arrival = _arrival()
    for ordinal in range(31):
        member = _member(
            member_id="control" if ordinal == 0 else f"p{ordinal:02d}",
            member_role="CONTROL" if ordinal == 0 else "PERTURBED",
            source_member_fact_sha=f"{ordinal + 10:064x}",
            raw_receipt_set_sha256=f"{ordinal + 100:064x}",
            oos_role="historical_strict_oos",
            strict_asof=True,
        )
        refs = [
            _receipt_ref(
                0,
                evidence_sha256=f"{ordinal + 1000:064x}",
                selector_sha256=f"{ordinal + 2000:064x}",
            ),
            _receipt_ref(
                1,
                evidence_sha256=f"{ordinal + 3000:064x}",
                selector_sha256=f"{ordinal + 4000:064x}",
            ),
        ]
        manifest = _manifest(member, refs)
        members.append(member)
        manifests.append(manifest)
        bindings.append(_binding(member, arrival, manifest, [0]))
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": AS_OF,
        "builder_id": "test-partial-manifest-coverage",
        "model_facts": [],
        "ensemble_member_facts": members,
        "native_arrival_facts": [arrival],
        "ensemble_member_receipt_manifest_facts": manifests,
        "ensemble_member_arrival_binding_facts": bindings,
        "truth_facts": [_truth()],
        "residual_facts": [],
        "activation_facts": [],
    }
    with sqlite3.connect(database, isolation_level=None) as conn:
        receipt = ingest_canonical_contract(
            conn,
            {**identity, "content_sha256": content_sha256(identity)},
            commit=True,
            ingested_at_utc=AS_OF,
        )
    assert receipt["inserted_count"] == 95
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        result = build_single_system_oos_artifacts(conn, compiled_at_utc=AS_OF)
    cyyz = next(
        row
        for row in result["blocked_scopes"]
        if row["station_id"] == "CYYZ"
        and row["ensemble_system_id"] == "NOAA_GEFS_05"
    )
    diagnostics = cyyz["canonical_diagnostics"]
    assert diagnostics["strict_member_cohort_date_count"] == 1
    assert diagnostics["exact_member_receipt_coverage"] is False
    assert diagnostics["strict_bound_member_cohort_date_count"] == 0
