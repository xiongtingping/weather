from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from scripts import materialize_canonical_activation_release as release_cli

from bayes_basket_target.canonical_activation_release import (
    ACTIVATION_READY,
    BLOCKED,
    DATA_MISSING,
    EXPECTED_SCOPE_MANIFEST_SCHEMA,
    OOS_SAMPLE_SHORT,
    OOS_READY,
    CanonicalActivationReleaseError,
    build_release_artifacts,
    build_truth_gap_release,
    load_contracts,
    load_db_audit_snapshot,
    load_db_projection,
    load_inner_snapshot,
    load_expected_scope_manifest,
    load_typed_scope_blocked_receipts,
    load_typed_scope_envelopes,
    load_truth_gap_evidence,
    publish_release,
    verify_binding_v1,
    verify_consumable_current_root_v2,
    verify_current_root_pointer_v2,
    verify_projection_snapshot_consistency,
    _expected_scope_coverage,
)
from bayes_basket_target.canonical_contract import (
    CONTRACT_VERSION,
    CanonicalContract,
    CanonicalModelFact,
    SCHEMA,
)
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.station_registry import coordinate_registry_manifest


def test_typed_scope_blocked_receipt_is_terminal_release_evidence(
    tmp_path: Path,
) -> None:
    identity = {
        "schema": "bayes_basket_target_canonical_typed_scope_build_receipt_v1",
        "scope_type": "P0_SNAPSHOT",
        "as_of_utc": AS_OF,
        "state": "BLOCKED",
        "blockers": ["DATA_MISSING"],
        "missing_producer_fields": ["p0_snapshot_content_sha256"],
        "source_table": "canonical_ssot_fact+basket_probability_envelope",
        "source_row_count": 0,
        "record_count": 0,
        "source_selection_sha256": content_sha256([]),
        "envelope_content_sha256": "",
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
    }
    receipt = {**identity, "content_sha256": content_sha256(identity)}
    path = tmp_path / "p0-snapshot-blocked.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    loaded, evidence = load_typed_scope_blocked_receipts(
        [path], release_as_of_utc=AS_OF
    )
    assert loaded == [receipt]
    assert evidence[0]["scope_type"] == "P0_SNAPSHOT"
    assert evidence[0]["record_count"] == 0


AS_OF = "2026-08-03T12:00:00Z"


def _model() -> dict:
    identity = {
        "station_id": "ZUUU",
        "model_id": "ECMWF",
        "ensemble_system_id": "",
        "model_family": "global_deterministic",
        "provider_id": "ECMWF_IFS",
        "source_class": "aggregator_republisher",
        "event_date": "2026-07-23",
        "run_init_utc": "2026-07-22T00:00:00Z",
        "lead_bucket": "d1",
        "run_cycle": "PREVIOUS_DAY1",
        "settlement_timezone": "Asia/Shanghai",
        "forecast_value": 31.5,
        "raw_object_sha256": "a" * 64,
        "oos_role": "historical_strict_oos",
        "closed_day": True,
        "strict_asof": True,
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "published_at_utc": "",
        "received_at_utc": "",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _truth() -> dict:
    identity = {
        "truth_id": "truth-zuuu-20260723-r1",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "truth_revision_no": 1,
        "truth_value": 32.0,
        "truth_content_sha256": "b" * 64,
        "canonical_committed_at_utc": "2026-07-24T08:00:00Z",
        "station_day_end_utc": "2026-07-24T06:00:00Z",
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "Asia/Shanghai",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _activation(generation: int) -> dict:
    identity = {
        "activation_id": f"activation-zuuu-g{generation}",
        "station_id": "ZUUU",
        "model_family": "global_deterministic",
        "lead_bucket": "d1",
        "run_cycle": "PREVIOUS_DAY1",
        "candidate_models": ["ECMWF"],
        "selected_models": ["ECMWF"],
        "selected_weights": {"ECMWF": 1.0},
        "available_model_set_hash": "c" * 64,
        "selection_evidence_sha256": "d" * 64,
        "artifact_content_sha256": "e" * 64,
        "activation_generation": generation,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "oos_role": "historical_strict_oos",
    }
    return {**identity, "activation_content_sha256": content_sha256(identity)}


def _contract(generation: int = 1) -> dict:
    model = _model()
    truth = _truth()
    residual_identity = {
        "residual_id": "res-zuuu-20260723-ecmwf",
        "truth_id": truth["truth_id"],
        "model_fact_content_sha256": model["content_sha256"],
        "station_id": "ZUUU",
        "model_id": "ECMWF",
        "event_date": "2026-07-23",
        "lead_bucket": "d1",
        "run_cycle": "PREVIOUS_DAY1",
        "residual_value": 0.5,
        "paired_asof_utc": "2026-07-22T00:00:00Z",
        "oos_role": "historical_strict_oos",
    }
    residual = {**residual_identity, "content_sha256": content_sha256(residual_identity)}
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": AS_OF,
        "builder_id": "d1.global_deterministic.test.v1",
        "model_facts": [model],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [truth],
        "residual_facts": [residual],
        "activation_facts": [_activation(generation)],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _write_hashed(path: Path, identity: dict) -> dict:
    value = {**identity, "content_sha256": content_sha256(identity)}
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return value


def _truth_gap_receipt(
    path: Path,
    *,
    canonical_contract_sha256: str,
    model_date_ready: bool = True,
    as_of_utc: str = AS_OF,
) -> Path:
    stations = sorted(coordinate_registry_manifest()["station_ids"])
    ready = ["2026-07-23"] if model_date_ready else []
    blocked = ["2026-07-24"] if model_date_ready else [
        "2026-07-23",
        "2026-07-24",
    ]
    rows = [
        {
            "station_id": station,
            "state": BLOCKED,
            "blockers": [DATA_MISSING, OOS_SAMPLE_SHORT],
            "truth_ready_dates": ready,
            "truth_blocked_dates": blocked,
            "truth_fact_count": len(ready),
            "missing_date_diagnostics": [
                {"event_date": date, "blockers": [DATA_MISSING, OOS_SAMPLE_SHORT]}
                for date in blocked
            ],
        }
        for station in stations
    ]
    _write_hashed(
        path,
        {
            "schema": "bayes_basket_target_historical_awc_truth_contract_build_v1",
            "as_of_utc": as_of_utc,
            "builder_id": "truth-gap-test.v1",
            "canonical_contract_sha256": canonical_contract_sha256,
            "station_count": len(stations),
            "truth_fact_count": len(ready) * len(stations),
            "complete_station_count": 0,
            "blocked_station_count": len(stations),
            "stations": rows,
            "production_no_live": True,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    )
    return path


def _inner(
    path: Path,
    *,
    as_of: str = AS_OF,
    blockers: list[str] | None = None,
) -> Path:
    stations = sorted(coordinate_registry_manifest()["station_ids"])
    blockers = list(blockers or [])
    _write_hashed(
        path,
        {
            "schema": "bayes_basket_target_oos_atomic_snapshot_manifest_v1",
            "state": BLOCKED if blockers else "FROZEN_READ_ONLY",
            "blockers": blockers,
            "as_of_utc": as_of,
            "canonical_station_count": len(stations),
            "canonical_station_ids": stations,
            "read_only": True,
            "production_no_live": True,
            "network_fetches": 0,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    )
    return path


def _typed_envelope(
    path: Path,
    *,
    scope_type: str,
    scope_kind: str,
    natural_key: dict,
) -> Path:
    version = (
        {"activation_generation": 1}
        if scope_type in {"P0_ACTIVATION", "P0_SNAPSHOT"}
        else None
    )
    fact_identity = {
        "scope_type": scope_type,
        "scope_kind": scope_kind,
        "natural_key": natural_key,
        "version": version,
        "as_of_utc": AS_OF,
        "state": BLOCKED,
        "blockers": ["ACTIVATION_ROOT_MISSING"],
        "evidence_content_sha256": "7" * 64,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    fact = {**fact_identity, "content_sha256": content_sha256(fact_identity)}
    identity = {
        "schema": "bayes_basket_target_canonical_typed_scope_envelope_v1",
        "as_of_utc": AS_OF,
        "builder_id": f"{scope_type.lower()}.builder.v1",
        "scope_type": scope_type,
        "records": [fact],
        "production_no_live": True,
        "future_data_counts_as_oos": False,
    }
    _write_hashed(path, identity)
    return path


def _setup(tmp_path: Path) -> tuple[Path, Path, list[CanonicalContract], list[dict], dict, list[dict]]:
    contract_path = tmp_path / "contract.json"
    contract_value = _contract()
    contract_path.write_text(json.dumps(contract_value, sort_keys=True) + "\n", encoding="utf-8")
    db = tmp_path / "ssot.db"
    with sqlite3.connect(db) as conn:
        apply_production_ssot_schema(conn)
        ingest_canonical_contract(conn, contract_value, commit=True, ingested_at_utc=AS_OF)
    contracts, contract_evidence = load_contracts([contract_path])
    activations, projection = load_db_projection(db, as_of_utc=AS_OF, contracts=contracts)
    return db, contract_path, contracts, contract_evidence, projection, activations


def test_exact_contract_db_activation_binding_builds_activation_ready(tmp_path: Path) -> None:
    _db, _path, contracts, evidence, projection, activations = _setup(tmp_path)
    inner = load_inner_snapshot(_inner(tmp_path / "inner.json"))
    ledger, index, binding = build_release_artifacts(
        contracts=contracts,
        contract_evidence=evidence,
        activation_facts=activations,
        canonical_projection=projection,
        inner_snapshot=inner,
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )
    assert index["state"] == "CURRENT"
    assert ledger["records"][0]["state"] == ACTIVATION_READY
    assert ledger["summary"] == {
        "record_count": 1,
        "activation_ready_count": 1,
        "oos_ready_count": 0,
        "blocked_count": 0,
        "data_ready_count": 0,
        "candidate_only_count": 0,
        "builder_scope_count": 1,
        "builder_blocked_count": 0,
    }
    # A valid MODEL_OOS activation does not make the global release
    # consumable while the six non-model canonical scope envelopes are absent.
    assert binding["state"] == BLOCKED
    assert binding["blockers"] == ["TYPED_SCOPE_BLOCKED"]
    assert binding["entries"][0]["state"] == "BOUND"


def test_contract_and_canonical_projection_must_share_one_asof_fact_set(
    tmp_path: Path,
) -> None:
    db, _path, contracts, _evidence, contract_projection, _activations = _setup(tmp_path)
    _models, _all_activations, _truths, canonical_projection = load_db_audit_snapshot(
        db, as_of_utc=AS_OF
    )
    verify_projection_snapshot_consistency(contract_projection, canonical_projection)
    drifted = deepcopy(contract_projection)
    drifted["rows"][0]["ingested_at_utc"] = "2026-08-03T11:59:59Z"
    drifted["content_sha256"] = content_sha256(
        {key: value for key, value in drifted.items() if key != "content_sha256"}
    )
    with pytest.raises(
        CanonicalActivationReleaseError,
        match="activation_release_projection_snapshot_mismatch",
    ):
        verify_projection_snapshot_consistency(drifted, canonical_projection)


def test_ambiguous_generation_stays_oos_ready_and_blocks_binding(tmp_path: Path) -> None:
    db, _path, contracts, evidence, _projection, _activations = _setup(tmp_path)
    g2_packet = _contract(generation=2)
    # Only append the new activation fact; all other facts are exact replays.
    with sqlite3.connect(db) as conn:
        ingest_canonical_contract(conn, g2_packet, commit=True, ingested_at_utc=AS_OF)
    activations, projection = load_db_projection(db, as_of_utc=AS_OF, contracts=contracts)
    ledger, index, binding = build_release_artifacts(
        contracts=contracts,
        contract_evidence=evidence,
        activation_facts=activations,
        canonical_projection=projection,
        inner_snapshot=load_inner_snapshot(_inner(tmp_path / "inner.json")),
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )
    assert index["state"] == BLOCKED
    assert ledger["records"][0]["state"] == OOS_READY
    assert ledger["summary"]["builder_blocked_count"] == 1
    assert binding["state"] == BLOCKED
    assert binding["entries"][0]["blocker"] == "ACTIVATION_CURRENT_AMBIGUOUS"


def test_inner_as_of_mismatch_is_bound_but_release_remains_blocked(tmp_path: Path) -> None:
    _db, _path, contracts, evidence, projection, activations = _setup(tmp_path)
    ledger, _index, binding = build_release_artifacts(
        contracts=contracts,
        contract_evidence=evidence,
        activation_facts=activations,
        canonical_projection=projection,
        inner_snapshot=load_inner_snapshot(
            _inner(tmp_path / "inner.json", as_of="2026-08-03T11:59:59Z")
        ),
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )
    assert ledger["records"][0]["state"] == ACTIVATION_READY
    assert binding["state"] == BLOCKED
    assert binding["blockers"] == [
        "INNER_OOS_SNAPSHOT_AS_OF_MISMATCH",
        "TYPED_SCOPE_BLOCKED",
    ]


def test_blocked_inner_scope_is_preserved_in_release_binding(tmp_path: Path) -> None:
    _db, _path, contracts, evidence, projection, activations = _setup(tmp_path)
    ledger, _index, binding = build_release_artifacts(
        contracts=contracts,
        contract_evidence=evidence,
        activation_facts=activations,
        canonical_projection=projection,
        inner_snapshot=load_inner_snapshot(
            _inner(
                tmp_path / "inner.json",
                blockers=["SNAPSHOT_SCOPE_MISMATCH:p0_inbox"],
            )
        ),
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )

    assert ledger["records"][0]["state"] == ACTIVATION_READY
    assert binding["state"] == BLOCKED
    assert binding["blockers"] == [
        "INNER_OOS_SNAPSHOT_SCOPE_MISMATCH:p0_inbox",
        "TYPED_SCOPE_BLOCKED",
    ]


def test_truth_gap_receipt_loads_exact_45_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    _db, _path, _contracts, evidence, _projection, _activations = _setup(tmp_path)
    receipt_path = _truth_gap_receipt(
        tmp_path / "truth-gap.json",
        canonical_contract_sha256=evidence[0]["content_sha256"],
    )
    stations = sorted(coordinate_registry_manifest()["station_ids"])

    loaded = load_truth_gap_evidence(
        receipt_path,
        release_as_of_utc=AS_OF,
        canonical_station_ids=stations,
        contract_evidence=evidence,
    )

    assert sorted(loaded["stations_by_id"]) == stations
    assert loaded["source_evidence"]["station_count"] == len(stations)
    tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
    tampered["stations"][0]["truth_fact_count"] = 0
    receipt_path.write_text(json.dumps(tampered, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(
        CanonicalActivationReleaseError,
        match="truth_gap_evidence_content_sha256_mismatch",
    ):
        load_truth_gap_evidence(
            receipt_path,
            release_as_of_utc=AS_OF,
            canonical_station_ids=stations,
            contract_evidence=evidence,
        )


def test_truth_gap_release_projects_ready_missing_and_never_activates(
    tmp_path: Path,
) -> None:
    db, _path, contracts, evidence, _projection, _activations = _setup(tmp_path)
    models, activations, truths, projection = load_db_audit_snapshot(
        db, as_of_utc=AS_OF
    )
    receipt = load_truth_gap_evidence(
        _truth_gap_receipt(
            tmp_path / "truth-gap.json",
            canonical_contract_sha256=evidence[0]["content_sha256"],
        ),
        release_as_of_utc=AS_OF,
        canonical_station_ids=sorted(coordinate_registry_manifest()["station_ids"]),
        contract_evidence=evidence,
    )

    ledger, index, binding = build_truth_gap_release(
        contracts=contracts,
        models=models,
        activation_facts=activations,
        truths=truths,
        canonical_projection=projection,
        contract_evidence=evidence,
        truth_gap_evidence=receipt,
        inner_snapshot=load_inner_snapshot(_inner(tmp_path / "inner.json")),
        as_of_utc=AS_OF,
        train_count=1,
        holdout_count=1,
        taf_revision_complete=False,
    )

    assert ledger["records"][0]["scope_type"] == "MODEL_OOS"
    assert ledger["records"][0]["state"] == OOS_READY
    assert ledger["builder_scope_statuses"][0]["blockers"] == [OOS_SAMPLE_SHORT]
    assert ledger["summary"]["activation_ready_count"] == 0
    assert ledger["required_scope_types"] == [
        "MODEL_OOS", "P0_ACTIVATION", "P0_SNAPSHOT", "TRUTH_OBSERVATION",
        "TRUTH_DAY", "TAF_P0M", "TRAJECTORY_P1", "STRATEGY_POLICY",
        "STRATEGY_UTILITY", "MARKET_WS", "RUNTIME",
    ]
    assert len(ledger["scope_type_statuses"]) == 11
    assert ledger["typed_scope_coverage_complete"] is False
    assert index["entry_count"] == 0
    assert binding["state"] == BLOCKED
    assert "ACTIVATION_ROOT_MISSING" in binding["blockers"]

    missing_receipt = load_truth_gap_evidence(
        _truth_gap_receipt(
            tmp_path / "truth-gap-missing.json",
            canonical_contract_sha256=evidence[0]["content_sha256"],
            model_date_ready=False,
        ),
        release_as_of_utc=AS_OF,
        canonical_station_ids=sorted(coordinate_registry_manifest()["station_ids"]),
        contract_evidence=evidence,
    )
    missing_ledger, missing_index, _missing_binding = build_truth_gap_release(
        contracts=contracts,
        models=models,
        activation_facts=activations,
        truths=truths,
        canonical_projection=projection,
        contract_evidence=evidence,
        truth_gap_evidence=missing_receipt,
        inner_snapshot=load_inner_snapshot(_inner(tmp_path / "inner-missing.json")),
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )
    assert missing_ledger["records"][0]["state"] == BLOCKED
    assert missing_ledger["records"][0]["blockers"] == [DATA_MISSING]
    assert missing_index["entry_count"] == 0


def test_truth_gap_release_embeds_all_ten_typed_natural_key_scopes(
    tmp_path: Path,
) -> None:
    db, _path, contracts, evidence, _projection, _activations = _setup(tmp_path)
    models, activations, truths, projection = load_db_audit_snapshot(
        db, as_of_utc=AS_OF
    )
    typed_paths = [
        _typed_envelope(
            tmp_path / "p0-activation.json", scope_type="P0_ACTIVATION", scope_kind="ACTIVATION",
            natural_key={
                "station_id": "ZUUU", "lead_bucket": "d0", "run_cycle": None,
                "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE",
                "run_cycle_vector_sha256": "1" * 64,
                "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                "model_family": "cross_global_deterministic",
                "available_system_set_sha256": "2" * 64,
                "independence_family_set_sha256": "3" * 64,
            },
        ),
        _typed_envelope(
            tmp_path / "p0-snapshot.json", scope_type="P0_SNAPSHOT", scope_kind="SNAPSHOT",
            natural_key={
                "station_id": "ZUUU", "event_date": "2026-07-23",
                "decision_at_utc": "2026-07-23T08:00:00Z", "lead_bucket": "d0",
                "run_cycle": None,
                "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE",
                "run_cycle_vector_sha256": "1" * 64,
                "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                "model_family": "cross_global_deterministic",
                "available_system_set_sha256": "2" * 64,
                "independence_family_set_sha256": "3" * 64,
                "probability_root_sha256": "6" * 64,
                "activation_content_sha256": "5" * 64,
                "forward_applicability_content_sha256": "7" * 64,
            },
        ),
        _typed_envelope(
            tmp_path / "truth-observation.json", scope_type="TRUTH_OBSERVATION", scope_kind="EVENT",
            natural_key={"station_id": "ZUUU", "event_date": "2026-07-23", "observation_id": "obs-1", "observed_at_utc": "2026-07-23T08:00:00Z", "source_lane": "AWC"},
        ),
        _typed_envelope(
            tmp_path / "truth.json", scope_type="TRUTH_DAY", scope_kind="REVISION",
            natural_key={"station_id": "ZUUU", "event_date": "2026-07-23", "truth_revision_no": 1},
        ),
        _typed_envelope(
            tmp_path / "taf.json", scope_type="TAF_P0M", scope_kind="ARTIFACT",
            natural_key={"station_id": "ZUUU", "settlement_timezone": "Asia/Shanghai", "season": "JJA", "lead_bucket": "d1", "run_cycle": "PREVIOUS_DAY1"},
        ),
        _typed_envelope(
            tmp_path / "p1.json", scope_type="TRAJECTORY_P1", scope_kind="ARTIFACT",
            natural_key={"station_id": "ZUUU", "source_lane": "AWC", "season": "JJA", "intraday_bucket": "MORNING", "maturity": "D0", "settlement_definition": "daily_high_et_v1"},
        ),
        _typed_envelope(
            tmp_path / "utility.json", scope_type="STRATEGY_UTILITY", scope_kind="EVALUATION",
            natural_key={"station_id": "ZUUU", "event_date": "2026-07-23", "decision_at_utc": "2026-07-23T08:00:00Z", "settlement_timezone": "Asia/Shanghai", "policy_stage": "P1", "strategy_version": "pure_yes_v1", "season": "JJA", "maturity": "D0", "liquidity_regime": "STANDARD", "candidate_inventory_sha256": "1" * 64, "baseline_inventory_sha256": "2" * 64, "ws_snapshot_sha256": "3" * 64},
        ),
        _typed_envelope(
            tmp_path / "strategy-policy.json", scope_type="STRATEGY_POLICY", scope_kind="ACTIVATION",
            natural_key={"station_id": "ZUUU", "policy_stage": "P1", "strategy_version": "pure_yes_v1", "season": "JJA", "maturity": "D0", "liquidity_regime": "STANDARD", "activation_content_sha256": "8" * 64},
        ),
        _typed_envelope(
            tmp_path / "ws.json", scope_type="MARKET_WS", scope_kind="REGISTRY",
            natural_key={"station_id": "ZUUU", "event_date": "2026-07-23", "market_id": "market-1", "yes_token_id": "yes-1", "bucket_contract_sha256": "4" * 64},
        ),
        _typed_envelope(
            tmp_path / "runtime.json", scope_type="RUNTIME", scope_kind="SERVICE_RELEASE",
            natural_key={"service_label": "com.weather.weather_basket_yes", "launch_domain": "gui/501", "runtime_release_id": "runtime-1"},
        ),
    ]
    typed, typed_evidence = load_typed_scope_envelopes(
        typed_paths, release_as_of_utc=AS_OF
    )
    receipt = load_truth_gap_evidence(
        _truth_gap_receipt(
            tmp_path / "truth-gap.json",
            canonical_contract_sha256=evidence[0]["content_sha256"],
        ),
        release_as_of_utc=AS_OF,
        canonical_station_ids=sorted(coordinate_registry_manifest()["station_ids"]),
        contract_evidence=evidence,
    )
    ledger, _index, binding = build_truth_gap_release(
        contracts=contracts,
        models=models,
        activation_facts=activations,
        truths=truths,
        canonical_projection=projection,
        contract_evidence=evidence,
        truth_gap_evidence=receipt,
        inner_snapshot=load_inner_snapshot(_inner(tmp_path / "inner.json")),
        as_of_utc=AS_OF,
        train_count=1,
        holdout_count=1,
        taf_revision_complete=False,
        typed_scope_envelopes=typed,
        typed_scope_evidence=typed_evidence,
    )
    assert ledger["typed_scope_coverage_complete"] is False
    assert all(
        item["blockers"] == ["EXPECTED_SCOPE_MANIFEST_MISSING"]
        for item in ledger["expected_scope_coverage"]
    )
    assert [item["scope_type"] for item in ledger["scope_type_statuses"]] == [
        "MODEL_OOS", "P0_ACTIVATION", "P0_SNAPSHOT", "TRUTH_OBSERVATION",
        "TRUTH_DAY", "TAF_P0M", "TRAJECTORY_P1", "STRATEGY_POLICY",
        "STRATEGY_UTILITY", "MARKET_WS", "RUNTIME",
    ]
    assert all(item["scope_count"] == 1 for item in ledger["scope_type_statuses"][1:])
    typed_records = [item for item in ledger["records"] if item["scope_type"] != "MODEL_OOS"]
    assert len(typed_records) == 10
    assert all("model_id" not in item and item["natural_key"] for item in typed_records)
    assert binding["state"] == BLOCKED
    assert "TYPED_SCOPE_BLOCKED" in binding["blockers"]


def _expected_key_for(scope_type: str, station_id: str) -> dict:
    keys = {
        "MODEL_OOS": ("BUILDER", {
            "station_id": station_id, "model_family": "global_deterministic",
            "lead_bucket": "d1", "run_cycle": "PREVIOUS_DAY1",
        }),
        "P0_ACTIVATION": ("ACTIVATION", {
            "station_id": station_id, "lead_bucket": "d0", "run_cycle": None,
            "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE",
            "run_cycle_vector_sha256": "1" * 64,
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "model_family": "cross_global_deterministic",
            "available_system_set_sha256": "2" * 64,
            "independence_family_set_sha256": "3" * 64,
        }),
        "P0_SNAPSHOT": ("SNAPSHOT", {
            "station_id": station_id, "event_date": "2026-07-23",
            "decision_at_utc": "2026-07-23T08:00:00Z", "lead_bucket": "d0",
            "run_cycle": None,
            "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE",
            "run_cycle_vector_sha256": "1" * 64,
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "model_family": "cross_global_deterministic",
            "available_system_set_sha256": "2" * 64,
            "independence_family_set_sha256": "3" * 64,
            "probability_root_sha256": "6" * 64,
            "activation_content_sha256": "5" * 64,
            "forward_applicability_content_sha256": "7" * 64,
        }),
        "TRUTH_OBSERVATION": ("EVENT", {
            "station_id": station_id, "event_date": "2026-07-23",
            "observation_id": f"obs-{station_id}",
            "observed_at_utc": "2026-07-23T08:00:00Z", "source_lane": "AWC",
        }),
        "TRUTH_DAY": ("REVISION", {
            "station_id": station_id, "event_date": "2026-07-23",
            "truth_revision_no": 1,
        }),
        "TAF_P0M": ("ARTIFACT", {
            "station_id": station_id, "settlement_timezone": "Asia/Shanghai",
            "season": "JJA", "lead_bucket": "d1", "run_cycle": "PREVIOUS_DAY1",
        }),
        "TRAJECTORY_P1": ("ARTIFACT", {
            "station_id": station_id, "source_lane": "AWC", "season": "JJA",
            "intraday_bucket": "MORNING", "maturity": "D0",
            "settlement_definition": "daily_high_et_v1",
        }),
        "STRATEGY_POLICY": ("ACTIVATION", {
            "station_id": station_id, "policy_stage": "P1",
            "strategy_version": "pure_yes_v1", "season": "JJA",
            "maturity": "D0", "liquidity_regime": "STANDARD",
            "activation_content_sha256": "8" * 64,
        }),
        "STRATEGY_UTILITY": ("EVALUATION", {
            "station_id": station_id, "event_date": "2026-07-23",
            "decision_at_utc": "2026-07-23T08:00:00Z",
            "settlement_timezone": "Asia/Shanghai", "policy_stage": "P1",
            "strategy_version": "pure_yes_v1", "season": "JJA",
            "maturity": "D0", "liquidity_regime": "STANDARD",
            "candidate_inventory_sha256": "1" * 64,
            "baseline_inventory_sha256": "2" * 64,
            "ws_snapshot_sha256": "3" * 64,
        }),
        "MARKET_WS": ("REGISTRY", {
            "station_id": station_id, "event_date": "2026-07-23",
            "market_id": f"market-{station_id}", "yes_token_id": f"yes-{station_id}",
            "bucket_contract_sha256": "4" * 64,
        }),
    }
    scope_kind, natural_key = keys[scope_type]
    return {"scope_kind": scope_kind, "natural_key": natural_key}


def test_expected_scope_manifest_requires_all_45_stations_and_blocks_one_station(
    tmp_path: Path,
) -> None:
    registry = coordinate_registry_manifest()
    stations = sorted(registry["station_ids"])
    scope_types = [
        "MODEL_OOS", "P0_ACTIVATION", "P0_SNAPSHOT", "TRUTH_OBSERVATION",
        "TRUTH_DAY", "TAF_P0M", "TRAJECTORY_P1", "STRATEGY_POLICY",
        "STRATEGY_UTILITY", "MARKET_WS", "RUNTIME",
    ]
    expectations = []
    for scope_type in scope_types:
        if scope_type == "RUNTIME":
            expected_keys = [
                {
                    "scope_kind": "SERVICE_RELEASE",
                    "natural_key": {
                        "service_label": label,
                        "launch_domain": "gui/501",
                        "runtime_release_id": "runtime-1",
                    },
                }
                for label in (
                    "com.weather.clob_order_service",
                    "com.weather.market_ws",
                    "com.weather.weather_basket_yes",
                )
            ]
        else:
            expected_keys = [_expected_key_for(scope_type, station) for station in stations]
        expected_keys.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        station_counts = [] if scope_type == "RUNTIME" else [
            {
                "station_id": station,
                "expected_key_count": sum(
                    item["natural_key"].get("station_id") == station
                    for item in expected_keys
                ),
            }
            for station in stations
        ]
        expectations.append({
            "scope_type": scope_type,
            "basis_content_sha256": content_sha256(
                {"scope_type": scope_type, "stations": stations}
            ),
            "expected_key_count": len(expected_keys),
            "expected_key_set_sha256": content_sha256(expected_keys),
            "expected_keys": expected_keys,
            "expected_key_evidence": (
                [
                    {
                        "expected_key": item,
                        "evidence_content_sha256": content_sha256(
                            {"expected_key": item, "basis": "test"}
                        ),
                    }
                    for item in expected_keys
                ]
                if scope_type == "MODEL_OOS" else []
            ),
            "station_expected_key_counts": station_counts,
            "unresolved_input_receipts": [],
        })
    identity = {
        "schema": EXPECTED_SCOPE_MANIFEST_SCHEMA,
        "as_of_utc": AS_OF,
        "canonical_registry_sha256": registry["registry_sha256"],
        "station_ids": stations,
        "scope_expectations": expectations,
        "production_no_live": True,
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    path = tmp_path / "expected.json"
    path.write_text(json.dumps({**identity, "content_sha256": content_sha256(identity)}))
    manifest, _evidence = load_expected_scope_manifest(
        path,
        release_as_of_utc=AS_OF,
        canonical_station_ids=stations,
        canonical_registry_sha256=registry["registry_sha256"],
    )
    typed_records = []
    for scope_type in scope_types[1:-1]:
        expected = _expected_key_for(scope_type, "ZUUU")
        typed_records.append({"scope_type": scope_type, **expected})
    typed_records.append({
        "scope_type": "RUNTIME",
        "scope_kind": "SERVICE_RELEASE",
        "natural_key": {
            "service_label": "com.weather.weather_basket_yes",
            "launch_domain": "gui/501",
            "runtime_release_id": "runtime-1",
        },
    })
    coverage, complete = _expected_scope_coverage(
        expected_scope_manifest=manifest,
        model_statuses=[{
            "station_id": "ZUUU", "model_family": "global_deterministic",
            "lead_bucket": "d1", "run_cycle": "PREVIOUS_DAY1",
        }],
        typed_records=typed_records,
    )
    assert complete is False
    assert coverage[0]["expected_key_count"] == expected_station_count()
    assert coverage[0]["missing_count"] == 44
    assert coverage[-1]["expected_key_count"] == 3
    assert coverage[-1]["missing_count"] == 2


def test_expected_scope_coverage_matches_only_exact_sets() -> None:
    station = "ZUUU"
    model_key = _expected_key_for("MODEL_OOS", station)
    typed_key = _expected_key_for("TRUTH_DAY", station)
    normalized = {scope: [] for scope in (
        "MODEL_OOS", "P0_ACTIVATION", "P0_SNAPSHOT", "TRUTH_OBSERVATION",
        "TRUTH_DAY", "TAF_P0M", "TRAJECTORY_P1", "STRATEGY_POLICY",
        "STRATEGY_UTILITY", "MARKET_WS", "RUNTIME",
    )}
    normalized["MODEL_OOS"] = [model_key]
    normalized["TRUTH_DAY"] = [typed_key]
    model_token = json.dumps(
        model_key, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    model_evidence = content_sha256({"model": model_key})
    metadata = {
        scope: {"basis_content_sha256": "", "unresolved_input_receipts": []}
        for scope in normalized
    }
    metadata["MODEL_OOS"]["expected_key_evidence"] = {
        model_token: model_evidence
    }
    coverage, complete = _expected_scope_coverage(
        expected_scope_manifest={
            "normalized_expected_keys": normalized,
            "normalized_scope_metadata": metadata,
        },
        model_statuses=[{
            **model_key["natural_key"],
            "domain_basis_content_sha256": model_evidence,
        }],
        typed_records=[{"scope_type": "TRUTH_DAY", **typed_key}],
    )
    assert complete is True
    assert all(row["state"] == "MATCHED" for row in coverage)
    coverage, complete = _expected_scope_coverage(
        expected_scope_manifest={
            "normalized_expected_keys": normalized,
            "normalized_scope_metadata": metadata,
        },
        model_statuses=[{
            **model_key["natural_key"],
            "domain_basis_content_sha256": "f" * 64,
        }],
        typed_records=[{"scope_type": "TRUTH_DAY", **typed_key}],
    )
    assert complete is False
    assert coverage[0]["evidence_mismatch_count"] == 1
    wrong_model = dict(model_key["natural_key"])
    wrong_model["lead_bucket"] = "d2"
    coverage, complete = _expected_scope_coverage(
        expected_scope_manifest={
            "normalized_expected_keys": normalized,
            "normalized_scope_metadata": metadata,
        },
        model_statuses=[{
            **wrong_model,
            "domain_basis_content_sha256": model_evidence,
        }],
        typed_records=[{"scope_type": "TRUTH_DAY", **typed_key}],
    )
    assert complete is False
    assert coverage[0]["missing_count"] == 1
    assert coverage[0]["excess_count"] == 1


def test_truth_gap_common_cohort_includes_models_with_zero_ready_dates(
    tmp_path: Path,
) -> None:
    db, _path, contracts, evidence, _projection, _activations = _setup(tmp_path)
    models, activations, truths, projection = load_db_audit_snapshot(
        db, as_of_utc=AS_OF
    )
    second_raw = dict(_model())
    second_raw.update({
        "model_id": "GFS",
        "provider_id": "NOAA_GFS",
        "event_date": "2026-07-24",
        "raw_object_sha256": "9" * 64,
    })
    second_identity = {
        key: value for key, value in second_raw.items() if key != "content_sha256"
    }
    second_identity["content_sha256"] = content_sha256(second_identity)
    two_models = [*models, CanonicalModelFact.from_mapping(second_identity)]
    receipt = load_truth_gap_evidence(
        _truth_gap_receipt(
            tmp_path / "truth-gap.json",
            canonical_contract_sha256=evidence[0]["content_sha256"],
        ),
        release_as_of_utc=AS_OF,
        canonical_station_ids=sorted(coordinate_registry_manifest()["station_ids"]),
        contract_evidence=evidence,
    )

    ledger, _index, _binding = build_truth_gap_release(
        contracts=contracts,
        models=two_models,
        activation_facts=activations,
        truths=truths,
        canonical_projection=projection,
        contract_evidence=evidence,
        truth_gap_evidence=receipt,
        inner_snapshot=load_inner_snapshot(_inner(tmp_path / "inner.json")),
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )

    assert ledger["builder_scope_statuses"][0]["record_count"] == 2
    assert ledger["builder_scope_statuses"][0]["blockers"] == [OOS_SAMPLE_SHORT]


def test_publish_is_single_pointer_cutover_and_recovery_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _db, _path, contracts, evidence, projection, activations = _setup(tmp_path)
    ledger, index, binding = build_release_artifacts(
        contracts=contracts,
        contract_evidence=evidence,
        activation_facts=activations,
        canonical_projection=projection,
        inner_snapshot=load_inner_snapshot(_inner(tmp_path / "inner.json")),
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )
    release_root = tmp_path / "releases"
    current = tmp_path / "current.json"
    import bayes_basket_target.canonical_activation_release as release

    real_replace = release.os.replace
    failed = False

    def fail_current_replace(source: Path | str, destination: Path | str) -> None:
        nonlocal failed
        if Path(destination) == current and not failed:
            failed = True
            raise OSError("injected-current-replace-fault")
        real_replace(source, destination)

    monkeypatch.setattr(release.os, "replace", fail_current_replace)
    with pytest.raises(OSError, match="injected-current-replace-fault"):
        publish_release(
            ledger=ledger,
            activation_index=index,
            binding=binding,
            canonical_projection=projection,
            contract_evidence=evidence,
            release_root=release_root,
            current_pointer=current,
            expected_current_content_sha256=None,
        )
    assert not current.exists()
    monkeypatch.setattr(release.os, "replace", real_replace)
    pointer = publish_release(
        ledger=ledger,
        activation_index=index,
        binding=binding,
        canonical_projection=projection,
        contract_evidence=evidence,
        release_root=release_root,
        current_pointer=current,
        expected_current_content_sha256=None,
    )
    assert verify_current_root_pointer_v2(json.loads(current.read_text())) == pointer
    before = current.read_bytes()
    with pytest.raises(
        CanonicalActivationReleaseError, match="current_root_compare_and_swap_mismatch"
    ):
        publish_release(
            ledger=ledger,
            activation_index=index,
            binding=binding,
            canonical_projection=projection,
            contract_evidence=evidence,
            release_root=release_root,
            current_pointer=current,
            expected_current_content_sha256="f" * 64,
        )
    assert current.read_bytes() == before


def test_preview_apply_drift_and_publish_paths_fail_closed(tmp_path: Path) -> None:
    preview = {"content_sha256": "a" * 64}
    release_cli._assert_preview_identity(preview, "a" * 64)
    with pytest.raises(ContractError, match="activation_release_preview_apply_drift"):
        release_cli._assert_preview_identity(preview, "b" * 64)
    with pytest.raises(
        ContractError, match="activation_release_expected_scope_coverage_incomplete"
    ):
        release_cli._assert_expected_scope_coverage(
            {"typed_scope_coverage_complete": False}
        )
    release_cli._assert_expected_scope_coverage(
        {"typed_scope_coverage_complete": True}
    )
    with pytest.raises(
        ContractError,
        match="activation_release_release_root_outside_canonical_audit_path",
    ):
        release_cli._validate_publish_paths(
            release_root=tmp_path / "releases",
            current_pointer=release_cli.CANONICAL_CURRENT_POINTER,
        )
    with pytest.raises(
        ContractError,
        match="activation_release_current_pointer_outside_canonical_audit_path",
    ):
        release_cli._validate_publish_paths(
            release_root=release_cli.CANONICAL_RELEASE_ROOT,
            current_pointer=tmp_path / "current.json",
        )
    with pytest.raises(
        ContractError,
        match="activation_release_output_outside_canonical_audit_path",
    ):
        release_cli._validate_output_path(
            output=tmp_path / "receipt.json",
            apply=False,
        )
    release_cli._validate_output_path(
        output=release_cli.CANONICAL_PREVIEW_RECEIPT,
        apply=False,
    )
    release_cli._validate_output_path(
        output=release_cli.CANONICAL_APPLY_RECEIPT,
        apply=True,
    )


def test_blocked_preview_is_explicitly_non_consumable(tmp_path: Path) -> None:
    db, contract_path, _contracts, evidence, _projection, _activations = _setup(tmp_path)
    args = argparse.Namespace(
        db=db,
        canonical_contract=[contract_path],
        inner_oos_snapshot=_inner(tmp_path / "inner.json"),
        truth_gap_evidence=_truth_gap_receipt(
            tmp_path / "truth-gap.json",
            canonical_contract_sha256=evidence[0]["content_sha256"],
        ),
        as_of_utc=AS_OF,
        train_count=1,
        holdout_count=1,
        taf_revision_complete=False,
        release_root=None,
        current_pointer=None,
        expect_current_sha=None,
        expect_preview_sha=None,
        apply=False,
        confirm=None,
    )
    preview = release_cli.build(args)
    assert preview["state"] == BLOCKED
    assert preview["consumable"] is False
    assert preview["filesystem_release_applied"] is False
    assert preview["activation_scope_count"] == 0


def test_apply_receipt_rehashes_changed_preview_fields(tmp_path: Path) -> None:
    preview_identity = {
        "schema": "bayes_basket_target_activation_release_preview_v1",
        "state": BLOCKED,
        "consumable": False,
        "filesystem_release_applied": False,
    }
    preview = {
        **preview_identity,
        "content_sha256": content_sha256(preview_identity),
    }
    pointer = {"content_sha256": "b" * 64, "release_id": "c" * 64}
    receipt = release_cli._build_apply_receipt(
        preview=preview,
        pointer=pointer,
        current_pointer=tmp_path / "current.json",
    )
    assert receipt["preview_content_sha256"] == preview["content_sha256"]
    assert receipt["filesystem_release_applied"] is True
    assert receipt["consumable"] is False
    assert receipt["content_sha256"] == content_sha256(
        {key: value for key, value in receipt.items() if key != "content_sha256"}
    )


def test_binding_state_is_iff_and_blocked_root_is_never_consumable(tmp_path: Path) -> None:
    _db, _path, contracts, evidence, projection, activations = _setup(tmp_path)
    ledger, index, binding = build_release_artifacts(
        contracts=contracts,
        contract_evidence=evidence,
        activation_facts=activations,
        canonical_projection=projection,
        inner_snapshot=load_inner_snapshot(
            _inner(
                tmp_path / "inner.json",
                blockers=["SNAPSHOT_SCOPE_MISMATCH:p0_inbox"],
            )
        ),
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )
    invalid = deepcopy(binding)
    invalid["state"] = "NOT_A_BOUND_STATE"
    invalid["content_sha256"] = content_sha256(
        {key: value for key, value in invalid.items() if key != "content_sha256"}
    )
    with pytest.raises(
        CanonicalActivationReleaseError,
        match="binding_global_state_invalid",
    ):
        verify_binding_v1(invalid)

    current = tmp_path / "current.json"
    pointer = publish_release(
        ledger=ledger,
        activation_index=index,
        binding=binding,
        canonical_projection=projection,
        contract_evidence=evidence,
        release_root=tmp_path / "releases",
        current_pointer=current,
        expected_current_content_sha256=None,
    )
    with pytest.raises(
        CanonicalActivationReleaseError,
        match="current_root_not_consumable",
    ):
        verify_consumable_current_root_v2(
            pointer,
            expected_release_root=tmp_path / "releases",
        )


def test_pointer_directory_fsync_failure_is_uncertain_and_never_blindly_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _db, _path, contracts, evidence, projection, activations = _setup(tmp_path)
    ledger, index, binding = build_release_artifacts(
        contracts=contracts,
        contract_evidence=evidence,
        activation_facts=activations,
        canonical_projection=projection,
        inner_snapshot=load_inner_snapshot(_inner(tmp_path / "inner.json")),
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )
    release_root = tmp_path / "releases"
    current = tmp_path / "current.json"
    import bayes_basket_target.canonical_activation_release as release

    real_fsync_directory = release._fsync_directory

    def fail_after_pointer_replace(path: Path) -> None:
        if path == current.parent and current.exists():
            raise OSError("injected-pointer-directory-fsync-fault")
        real_fsync_directory(path)

    monkeypatch.setattr(release, "_fsync_directory", fail_after_pointer_replace)
    with pytest.raises(
        CanonicalActivationReleaseError,
        match="current_root_publish_outcome_uncertain",
    ):
        publish_release(
            ledger=ledger,
            activation_index=index,
            binding=binding,
            canonical_projection=projection,
            contract_evidence=evidence,
            release_root=release_root,
            current_pointer=current,
            expected_current_content_sha256=None,
        )
    pointer = verify_current_root_pointer_v2(json.loads(current.read_text()))
    monkeypatch.setattr(release, "_fsync_directory", real_fsync_directory)
    with pytest.raises(
        CanonicalActivationReleaseError,
        match="current_root_compare_and_swap_mismatch",
    ):
        publish_release(
            ledger=ledger,
            activation_index=index,
            binding=binding,
            canonical_projection=projection,
            contract_evidence=evidence,
            release_root=release_root,
            current_pointer=current,
            expected_current_content_sha256=None,
        )
    assert json.loads(current.read_text())["content_sha256"] == pointer["content_sha256"]


def test_root_verifier_detects_immutable_leaf_tampering(tmp_path: Path) -> None:
    _db, _path, contracts, evidence, projection, activations = _setup(tmp_path)
    ledger, index, binding = build_release_artifacts(
        contracts=contracts,
        contract_evidence=evidence,
        activation_facts=activations,
        canonical_projection=projection,
        inner_snapshot=load_inner_snapshot(_inner(tmp_path / "inner.json")),
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )
    current = tmp_path / "current.json"
    pointer = publish_release(
        ledger=ledger,
        activation_index=index,
        binding=binding,
        canonical_projection=projection,
        contract_evidence=evidence,
        release_root=tmp_path / "releases",
        current_pointer=current,
        expected_current_content_sha256=None,
    )
    ledger_path = Path(pointer["ledger_path"])
    ledger_path.write_bytes(ledger_path.read_bytes() + b" ")
    with pytest.raises(
        CanonicalActivationReleaseError,
        match="release_manifest_leaf_file_sha256_mismatch",
    ):
        verify_current_root_pointer_v2(json.loads(current.read_text()))
