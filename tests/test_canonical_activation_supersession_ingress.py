from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from scripts import materialize_canonical_activation_release as release_cli

from bayes_basket_target.canonical_contract import (
    ACTIVATION_SUPERSESSION_CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    CanonicalContract,
    SCHEMA,
)
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_activation_release import (
    BLOCKED,
    CanonicalActivationReleaseError,
    build_activation_index_v2,
    build_release_artifacts,
    load_db_activation_release_snapshot,
    load_db_supersession_facts,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.station_registry import coordinate_registry_manifest


AS_OF = "2026-08-03T12:00:00Z"


def _hashed(identity: dict, field: str = "content_sha256") -> dict:
    return {**identity, field: content_sha256(identity)}


def _activation(generation: int) -> dict:
    identity = {
        "activation_id": f"activation-katl-d2-g{generation}",
        "station_id": "KATL",
        "model_family": "global_deterministic",
        "lead_bucket": "d2",
        "run_cycle": "PREVIOUS_DAY2",
        "candidate_models": ["ECMWF"],
        "selected_models": ["ECMWF"],
        "selected_weights": {"ECMWF": 1.0},
        "available_model_set_hash": "a" * 64,
        "selection_evidence_sha256": "b" * 64,
        "artifact_content_sha256": "c" * 64,
        "activation_generation": generation,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "oos_role": "historical_strict_oos",
    }
    return _hashed(identity, "activation_content_sha256")


def _model() -> dict:
    return _hashed({
        "station_id": "KATL", "model_id": "ECMWF", "ensemble_system_id": "",
        "model_family": "global_deterministic", "provider_id": "ECMWF_IFS",
        "source_class": "aggregator_republisher", "event_date": "2026-07-20",
        "run_init_utc": "2026-07-18T00:00:00Z", "lead_bucket": "d2",
        "run_cycle": "PREVIOUS_DAY2", "settlement_timezone": "America/New_York",
        "forecast_value": 31.0, "raw_object_sha256": "d" * 64,
        "oos_role": "historical_strict_oos", "closed_day": True, "strict_asof": True,
        "native_arrival_indexed": False, "p1_eligible": False,
        "formal_live_authority": False, "published_at_utc": "", "received_at_utc": "",
    })


def _truth() -> dict:
    return _hashed({
        "truth_id": "truth-katl-20260720-r1", "station_id": "KATL",
        "event_date": "2026-07-20", "truth_revision_no": 1, "truth_value": 31.5,
        "truth_content_sha256": "e" * 64,
        "canonical_committed_at_utc": "2026-07-21T05:00:00Z",
        "station_day_end_utc": "2026-07-21T04:00:00Z", "closed_day": True,
        "source_lane": "AWC", "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "America/New_York",
    })


def _model_contract(generation: int) -> dict:
    model = _model()
    truth = _truth()
    residual = _hashed({
        "residual_id": "res-katl-20260720-ecmwf", "truth_id": truth["truth_id"],
        "model_fact_content_sha256": model["content_sha256"], "station_id": "KATL",
        "model_id": "ECMWF", "event_date": "2026-07-20", "lead_bucket": "d2",
        "run_cycle": "PREVIOUS_DAY2", "residual_value": 0.5,
        "paired_asof_utc": "2026-07-21T05:00:00Z", "oos_role": "historical_strict_oos",
    })
    identity = {
        "schema": SCHEMA, "contract_version": CONTRACT_VERSION, "as_of_utc": AS_OF,
        "builder_id": f"d2.g{generation}.builder", "model_facts": [model],
        "ensemble_member_facts": [], "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [truth], "residual_facts": [residual],
        "activation_facts": [_activation(generation)],
    }
    return _hashed(identity)


def _supersession_contract(*, semantic_hash: str | None = None) -> dict:
    before = _activation(1)
    after = _activation(4)
    candidates = sorted([
        before["activation_content_sha256"], after["activation_content_sha256"]
    ])
    semantic_fields = (
        "artifact_content_sha256", "selection_evidence_sha256",
        "available_model_set_hash", "candidate_models", "selected_models",
        "selected_weights", "oos_role", "authority_scope",
    )
    semantic = {field: before[field] for field in semantic_fields}
    fact_identity = {
        "supersession_id": "supersession-katl-d2-g1-g4",
        "station_id": "KATL", "model_family": "global_deterministic",
        "lead_bucket": "d2", "run_cycle": "PREVIOUS_DAY2",
        "predecessor_activation_content_sha256": before["activation_content_sha256"],
        "predecessor_generation": 1,
        "successor_activation_content_sha256": after["activation_content_sha256"],
        "successor_generation": 4,
        "candidate_activation_content_sha256s": candidates,
        "candidate_activation_set_sha256": content_sha256(candidates),
        "semantic_equivalence_sha256": semantic_hash or content_sha256(semantic),
        "reason": "IDENTITY_DEDUPLICATION", "authority_as_of_utc": AS_OF,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    fact = _hashed(fact_identity)
    return _hashed({
        "schema": ACTIVATION_SUPERSESSION_CONTRACT_SCHEMA, "as_of_utc": AS_OF,
        "builder_id": "d2.explicit.supersession.v1", "supersession_facts": [fact],
    })


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    ingest_canonical_contract(conn, _model_contract(1), commit=True, ingested_at_utc=AS_OF)
    ingest_canonical_contract(conn, _model_contract(4), commit=True, ingested_at_utc=AS_OF)
    return conn


def test_supersession_uses_the_same_ingress_and_is_idempotent() -> None:
    conn = _db()
    contract = _supersession_contract()
    receipt = ingest_canonical_contract(conn, contract, commit=True, ingested_at_utc=AS_OF)
    assert receipt["validated_counts"] == {"activation_supersession": 1}
    assert receipt["inserted_count"] == 1
    replay = ingest_canonical_contract(conn, contract, commit=True, ingested_at_utc=AS_OF)
    assert replay["inserted_count"] == 0
    assert replay["replayed_count"] == 1
    row = conn.execute(
        "SELECT fact_type FROM canonical_ssot_fact WHERE fact_type='activation_supersession'"
    ).fetchone()
    assert row == ("activation_supersession",)


def test_supersession_rejects_semantic_guess_and_missing_candidate() -> None:
    conn = _db()
    with pytest.raises(
        ContractError, match="canonical_activation_supersession_semantic_hash_mismatch"
    ):
        ingest_canonical_contract(
            conn, _supersession_contract(semantic_hash="f" * 64), commit=False
        )
    incomplete = _supersession_contract()
    fact = deepcopy(incomplete["supersession_facts"][0])
    fact["candidate_activation_content_sha256s"] = [
        fact["successor_activation_content_sha256"]
    ]
    fact["candidate_activation_set_sha256"] = content_sha256(
        fact["candidate_activation_content_sha256s"]
    )
    fact["content_sha256"] = content_sha256(
        {key: value for key, value in fact.items() if key != "content_sha256"}
    )
    incomplete["supersession_facts"] = [fact]
    incomplete["content_sha256"] = content_sha256(
        {key: value for key, value in incomplete.items() if key != "content_sha256"}
    )
    with pytest.raises(
        ContractError, match="canonical_activation_supersession_candidates_invalid"
    ):
        ingest_canonical_contract(conn, incomplete, commit=False)


def test_supersession_never_accepts_generation_reversal() -> None:
    raw = _supersession_contract()
    fact = deepcopy(raw["supersession_facts"][0])
    fact["predecessor_generation"], fact["successor_generation"] = 4, 1
    fact["content_sha256"] = content_sha256(
        {key: value for key, value in fact.items() if key != "content_sha256"}
    )
    raw["supersession_facts"] = [fact]
    raw["content_sha256"] = content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )
    with pytest.raises(
        ContractError, match="canonical_activation_supersession_generation_order_invalid"
    ):
        ingest_canonical_contract(_db(), raw, commit=False)


def test_current_index_requires_explicit_complete_supersession_authority() -> None:
    before = _activation(1)
    after = _activation(4)
    blocked = build_activation_index_v2(
        [before, after],
        as_of_utc=AS_OF,
        canonical_projection_content_sha256="9" * 64,
    )
    assert blocked["entries"][0]["state"] == "BLOCKED"
    contract = _supersession_contract()
    authority = contract["supersession_facts"][0]
    current = build_activation_index_v2(
        [before, after],
        supersession_facts=[authority],
        as_of_utc=AS_OF,
        canonical_projection_content_sha256="9" * 64,
    )
    entry = current["entries"][0]
    assert entry["state"] == "CURRENT"
    assert entry["current_activation"]["activation_generation"] == 4
    assert entry["supersession_authority_content_sha256s"] == [
        authority["content_sha256"]
    ]

    third = _activation(5)
    incomplete = build_activation_index_v2(
        [before, after, third],
        supersession_facts=[authority],
        as_of_utc=AS_OF,
        canonical_projection_content_sha256="9" * 64,
    )
    assert incomplete["entries"][0]["state"] == "BLOCKED"


def test_append_only_three_generation_chain_selects_unique_head() -> None:
    g1 = _activation(1)
    g4 = _activation(4)
    g5 = _activation(5)
    first = _supersession_contract()["supersession_facts"][0]
    second = deepcopy(first)
    candidates = sorted(
        activation["activation_content_sha256"] for activation in (g1, g4, g5)
    )
    second.update(
        {
            "supersession_id": "supersession-katl-d2-g4-g5",
            "predecessor_activation_content_sha256": g4[
                "activation_content_sha256"
            ],
            "predecessor_generation": 4,
            "successor_activation_content_sha256": g5[
                "activation_content_sha256"
            ],
            "successor_generation": 5,
            "candidate_activation_content_sha256s": candidates,
            "candidate_activation_set_sha256": content_sha256(candidates),
        }
    )
    second["content_sha256"] = content_sha256(
        {key: value for key, value in second.items() if key != "content_sha256"}
    )
    current = build_activation_index_v2(
        [g1, g4, g5],
        supersession_facts=[first, second],
        as_of_utc=AS_OF,
        canonical_projection_content_sha256="9" * 64,
    )
    entry = current["entries"][0]
    assert entry["state"] == "CURRENT"
    assert entry["current_activation"]["activation_generation"] == 5


def _file_db(tmp_path: Path) -> Path:
    path = tmp_path / "ssot.db"
    with sqlite3.connect(path) as conn:
        apply_production_ssot_schema(conn)
        ingest_canonical_contract(
            conn, _model_contract(1), commit=True, ingested_at_utc=AS_OF
        )
        ingest_canonical_contract(
            conn, _model_contract(4), commit=True, ingested_at_utc=AS_OF
        )
        ingest_canonical_contract(
            conn, _supersession_contract(), commit=True, ingested_at_utc=AS_OF
        )
    return path


def test_one_snapshot_loader_and_multi_contract_release_select_successor(
    tmp_path: Path,
) -> None:
    path = _file_db(tmp_path)
    contracts = [
        CanonicalContract.from_mapping(_model_contract(1)),
        CanonicalContract.from_mapping(_model_contract(4)),
    ]
    (
        activation_facts,
        _models,
        _truths,
        supersessions,
        _contract_projection,
        projection,
        _supersession_projection,
    ) = load_db_activation_release_snapshot(
        path,
        as_of_utc=AS_OF,
        contracts=contracts,
    )
    inner_identity = {
        "schema": "bayes_basket_target_oos_atomic_snapshot_manifest_v1",
        "state": "FROZEN_READ_ONLY",
        "blockers": [],
        "as_of_utc": AS_OF,
        "boundary_valid": True,
        "canonical_station_count": 45,
        "canonical_station_ids": ["KATL"],
        "read_only": True,
        "production_no_live": True,
        "network_fetches": 0,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    inner = {**inner_identity, "content_sha256": content_sha256(inner_identity)}
    ledger, index, binding = build_release_artifacts(
        contracts=contracts,
        contract_evidence=[],
        activation_facts=activation_facts,
        supersession_facts=supersessions,
        canonical_projection=projection,
        inner_snapshot=inner,
        as_of_utc=AS_OF,
        train_count=0,
        holdout_count=1,
        taf_revision_complete=False,
    )
    assert index["entries"][0]["current_activation"][
        "activation_generation"
    ] == 4
    assert ledger["summary"]["record_count"] == 1
    assert ledger["records"][0]["activation_content_sha256"] == _activation(4)[
        "activation_content_sha256"
    ]
    assert binding["state"] == BLOCKED
    assert binding["blockers"] == ["TYPED_SCOPE_BLOCKED"]
    assert binding["entries"][0]["state"] == "BOUND"


def test_loader_rejects_authority_after_release_cutoff(tmp_path: Path) -> None:
    path = tmp_path / "future-authority.db"
    ingested = "2026-08-03T11:59:00Z"
    with sqlite3.connect(path) as conn:
        apply_production_ssot_schema(conn)
        ingest_canonical_contract(
            conn, _model_contract(1), commit=True, ingested_at_utc=ingested
        )
        ingest_canonical_contract(
            conn, _model_contract(4), commit=True, ingested_at_utc=ingested
        )
        ingest_canonical_contract(
            conn, _supersession_contract(), commit=True, ingested_at_utc=ingested
        )
    with pytest.raises(
        CanonicalActivationReleaseError,
        match="activation_release_db_supersession_after_as_of",
    ):
        load_db_supersession_facts(
            path,
            as_of_utc="2026-08-03T11:59:59Z",
        )


def test_activation_mode_cli_consumes_supersession_from_same_snapshot(
    tmp_path: Path,
) -> None:
    path = _file_db(tmp_path)
    contract_paths: list[Path] = []
    for generation in (1, 4):
        contract_path = tmp_path / f"contract-g{generation}.json"
        contract_path.write_text(
            json.dumps(_model_contract(generation), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        contract_paths.append(contract_path)
    stations = sorted(coordinate_registry_manifest()["station_ids"])
    inner_identity = {
        "schema": "bayes_basket_target_oos_atomic_snapshot_manifest_v1",
        "state": "FROZEN_READ_ONLY",
        "blockers": [],
        "as_of_utc": AS_OF,
        "canonical_station_count": 45,
        "canonical_station_ids": stations,
        "read_only": True,
        "production_no_live": True,
        "network_fetches": 0,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    inner = {**inner_identity, "content_sha256": content_sha256(inner_identity)}
    inner_path = tmp_path / "inner.json"
    inner_path.write_text(
        json.dumps(inner, sort_keys=True) + "\n", encoding="utf-8"
    )
    preview = release_cli.build(
        argparse.Namespace(
            db=path,
            canonical_contract=contract_paths,
            inner_oos_snapshot=inner_path,
            release_mode="activation",
            truth_gap_evidence=None,
            typed_scope_envelope=[],
            as_of_utc=AS_OF,
            train_count=0,
            holdout_count=1,
            taf_revision_complete=False,
            apply=False,
        )
    )
    assert preview["source_mode"] == "CANONICAL_CONTRACT_ACTIVATION"
    assert preview["activation_ready_count"] == 1
    assert preview["record_count"] == 1
    assert preview["state"] == BLOCKED
