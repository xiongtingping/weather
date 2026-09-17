from __future__ import annotations

from copy import deepcopy
import json

import pytest

from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_contract import CONTRACT_VERSION, SCHEMA as CANONICAL_SCHEMA
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.model_oos_contract import CanonicalModelOOSEventFact
from bayes_basket_target.model_system_coverage import ROW_SCHEMA
from bayes_basket_target.native_daily_max_manifest import expected_et_hourly_slots
from bayes_basket_target.republisher_historical_event_builder import (
    EVIDENCE_SCHEMA,
    build_republisher_historical_event_bundle,
)
from bayes_basket_target.truth_authority_supersession import (
    build_canonical_truth_authority_supersession_contract,
)
from tests.pr12_authority_projection_test_support import authority_projection
from tests.test_truth_authority_supersession import (
    _review_audit,
    _v15_database,
)


MODELS = {
    "CMA": ("cma_grapes_global_open_meteo", "cma_grapes_global"),
    "ECMWF": ("ecmwf_ifs_open_meteo_previous_runs", "ecmwf_ifs"),
    "GEM": ("gem_global_open_meteo", "gem_global"),
    "GFS": ("gfs_global_open_meteo_previous_runs", "gfs_global"),
    "ICON": ("icon_global_open_meteo", "dwd_icon_global"),
}
FAMILIES = {
    "CMA": "CMA", "ECMWF": "ECMWF", "GEM": "ECCC",
    "GFS": "NOAA", "ICON": "DWD",
}


def _seal(value: dict) -> dict:
    return {**value, "content_sha256": content_sha256(value)}


def _coverage(model: str, lead: str = "d1") -> dict:
    provider, _ = MODELS[model]
    identity = {
        "schema": ROW_SCHEMA,
        "station_id": "KATL",
        "product_id": model,
        "provider_id": provider,
        "model_family": "global_deterministic",
        "independence_family_id": "DETERMINISTIC_" + model,
        "source_class": "aggregator_republisher",
        "lead_bucket": lead,
        "run_cycle": "PREVIOUS_DAY1" if lead == "d1" else "PREVIOUS_DAY2",
        "as_of_utc": "2026-08-05T12:00:00Z",
        "state": "DATA_READY",
        "blockers": ["OOS_SAMPLE_SHORT"],
        "evidence_role": "historical_strict_oos",
        "strict_asof": False,
        "closed_awc_truth": False,
        "train_count": 0,
        "holdout_count": 0,
        "source_evidence_sha256": content_sha256([model, lead]),
        "raw_receipt_sha256": None,
        "oos_evidence_sha256": content_sha256(["oos", model, lead]),
        "single_system_artifact_sha256": None,
        "native_arrival_indexed": False,
        "formal_live_authority": False,
        "cross_selection_count": 0,
        "cross_activation_count": 0,
        "p0_count": 0,
    }
    return _seal(identity)


def _evidence(*, lead: int = 1, missing_model: str | None = None) -> dict:
    slots = expected_et_hourly_slots("2026-08-01")
    captures = []
    for ordinal, (model, (provider, _)) in enumerate(MODELS.items()):
        if model == missing_model:
            continue
        captures.append({
            "api_model": model.lower(),
            "daily_rows": [{"event_date": "2026-08-01"}],
            "formal_lineage_eligible": False,
            "grid": {"latitude": 33.64, "longitude": -84.43},
            "hourly_temperature_rows": [
                {"valid_time_utc": slot, "temperature_2m_c": 20.0 + ordinal}
                for slot in slots
            ],
            "model_id": model,
            "previous_run_semantics": (
                f"value_predicted_{24 * lead}_hours_before_valid_time"
            ),
            "provider_id": provider,
            "response_bytes": 100,
            "response_sha256": content_sha256(["response", model, lead]),
            "source_class": "aggregator_republisher",
            "source_url": "https://previous-runs-api.open-meteo.com/v1/forecast",
        })
    identity = {
        "schema": EVIDENCE_SCHEMA,
        "station_id": "KATL",
        "lead_days": lead,
        "settlement_timezone": "America/Chicago",
        "fetched_at_utc": "2026-08-05T12:00:00Z",
        "source_contract": {
            "endpoint": "https://previous-runs-api.open-meteo.com/v1/forecast",
            "fixed_lead_semantics": (
                f"predicted_{24 * lead}_hours_before_each_valid_time"
            ),
            "native_publication_or_arrival_claimed": False,
        },
        "captures": captures,
    }
    return _seal(identity)


def _ready_database():
    conn = _v15_database()
    # The lane now asks the listing what day the station settles on, so the
    # fixture has to declare one. KATL genuinely is an Eastern station -- one
    # of the three in the fleet -- which is why this lane still admits it.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_target_market_registry ("
        " station_id TEXT, event_date TEXT, settlement_timezone TEXT,"
        " source_eligible INTEGER)"
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES (?,?,?,1)",
        ("KATL", "2026-08-01", "America/New_York"),
    )
    # Left open, this transaction is the caller's, and the ingress refuses to
    # finalize one it does not own.
    conn.commit()
    truth_identity = {
        "truth_id": "awc-historical-truth-katl-20260801",
        "station_id": "KATL",
        "event_date": "2026-08-01",
        "truth_revision_no": 0,
        "truth_value": 30.0,
        "truth_content_sha256": content_sha256(["KATL", "2026-08-01", 30.0]),
        "canonical_committed_at_utc": "2026-08-02T05:00:00Z",
        "station_day_end_utc": "2026-08-02T04:00:00Z",
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "America/New_York",
    }
    contract_identity = {
        "schema": CANONICAL_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": "2026-08-05T12:00:00Z",
        "builder_id": "tests.historical_truth.v1",
        "model_facts": [],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [_seal(truth_identity)],
        "residual_facts": [],
        "activation_facts": [],
    }
    contract = _seal(contract_identity)
    ingest_canonical_contract(conn, contract, commit=True)
    return conn


def _write_evidence(tmp_path, evidence: dict):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


def test_evidence_mapping_cannot_bind_another_file(tmp_path) -> None:
    conn = _ready_database()
    evidence = _evidence()
    other = _evidence(lead=2)
    with pytest.raises(ContractError, match="evidence_path_mismatch"):
        build_republisher_historical_event_bundle(
            conn, evidences=[evidence], evidence_paths=[_write_evidence(tmp_path, other)],
            coverage_rows=[_coverage(model) for model in MODELS],
            authority_scope_projection=authority_projection(),
            as_of_utc="2026-08-05T12:00:00Z",
        )
