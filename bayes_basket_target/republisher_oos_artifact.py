"""Build canonical no-live artifacts from frozen republisher OOS reports.

This module never writes the database.  It converts a content-addressed
deterministic Open-Meteo report into the one canonical artifact contract
accepted by :func:`canonical_ingress.ingest_canonical_contract`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from .canonical_contract import (
    CanonicalRepublisherOOSArtifactContract,
    REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA,
)
from .contracts import ContractError, content_sha256
from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .deterministic_republisher_oos import (
    CANDIDATE_SCHEMA,
    SCHEMA as OOS_REPORT_SCHEMA,
)


ARTIFACT_METHOD_VERSION = "deterministic_bias_calibration_v1"
OOS_PROTOCOL_VERSION = "strict_closed_awc_20_8_v1"
PRODUCT_REGISTRY: dict[tuple[str, str], dict[str, str]] = {
    ("AIFS", "ecmwf_aifs_single_open_meteo"): {
        "independence_family_id": "ECMWF",
        "republisher_product_id": "ecmwf_aifs025_single",
    },
    ("ICON", "icon_global_open_meteo"): {
        "independence_family_id": "DWD",
        "republisher_product_id": "dwd_icon_global",
    },
}


def _verify_hash(raw: Mapping[str, Any], *, field: str = "content_sha256") -> None:
    supplied = str(raw.get(field) or "").strip().lower()
    if len(supplied) != 64 or any(char not in "0123456789abcdef" for char in supplied):
        raise ContractError("republisher_oos_artifact_source_hash_invalid")
    identity = {key: value for key, value in raw.items() if key != field}
    if supplied != content_sha256(identity):
        raise ContractError("republisher_oos_artifact_source_hash_mismatch")


def _settlement_definition(
    database: sqlite3.Connection, station_id: str
) -> str:
    rows = database.execute(
        "SELECT DISTINCT settlement_definition FROM calibration_station_registry "
        "WHERE station_id=? AND settlement_definition<>''",
        (station_id,),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError("republisher_oos_artifact_settlement_definition_not_unique")
    return str(rows[0][0])


def _candidate_index(
    report: Mapping[str, Any], *, truth_source: str
) -> dict[str, Mapping[str, Any]]:
    if report.get("schema") != OOS_REPORT_SCHEMA:
        raise ContractError("republisher_oos_artifact_report_schema_invalid")
    _verify_hash(report)
    if (
        report.get("source_role") != "aggregator_republisher"
        or report.get("transport_provider_id") != "OPEN_METEO"
        or report.get("truth_source") != truth_source
        or report.get("legacy_truth_projection_read") is not False
        or report.get("formal_p0_eligible") is not False
    ):
        raise ContractError("republisher_oos_artifact_report_lineage_invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for candidate in report.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            raise ContractError("republisher_oos_artifact_candidate_mapping_required")
        _verify_hash(candidate)
        station = str(candidate.get("station_id") or "").strip().upper()
        if not station or station in result:
            raise ContractError("republisher_oos_artifact_candidate_scope_invalid")
        if candidate.get("schema") != CANDIDATE_SCHEMA:
            raise ContractError("republisher_oos_artifact_candidate_schema_invalid")
        if (
            str(candidate.get("model_id") or "").strip().upper()
            != str(report.get("model_id") or "").strip().upper()
            or candidate.get("provider_id") != report.get("provider_id")
            or candidate.get("transport_provider_id") != "OPEN_METEO"
            or candidate.get("source_role") != "aggregator_republisher"
            or candidate.get("authority_scope") != "PRODUCTION_NO_LIVE_CANDIDATE"
            or candidate.get("formal_p0_eligible") is not False
            or candidate.get("native_arrival_index_eligible") is not False
            or candidate.get("p1_eligible") is not False
            or candidate.get("future_event_count") != 0
            or candidate.get("run_cycle") is not None
            or candidate.get("run_cycle_scope")
            != "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE"
        ):
            raise ContractError("republisher_oos_artifact_candidate_lineage_invalid")
        if candidate.get("state") == "PRODUCTION_NO_LIVE_OOS_PASS":
            result[station] = candidate
    return result


def build_republisher_oos_artifact_contract(
    report: Mapping[str, Any],
    database: sqlite3.Connection,
    *,
    builder_id: str,
    station_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a strict canonical contract for passed station artifacts."""

    inspection = inspect_canonical_ssot_schema(database)
    state = str(inspection.get("state") or "")
    if state not in {"V7", "V8", "V9", "V10"} or inspection.get("errors"):
        raise ContractError("republisher_oos_artifact_canonical_schema_invalid")
    candidates = _candidate_index(
        report, truth_source=f"canonical_ssot_fact_{state.lower()}"
    )
    requested = (
        sorted({str(value).strip().upper() for value in station_ids})
        if station_ids is not None
        else sorted(candidates)
    )
    if not requested or any(not value for value in requested):
        raise ContractError("republisher_oos_artifact_station_scope_empty")
    missing = sorted(set(requested) - set(candidates))
    if missing:
        raise ContractError(
            "republisher_oos_artifact_candidate_missing:" + ",".join(missing)
        )
    model_id = str(report.get("model_id") or "").strip().upper()
    provider_id = str(report.get("provider_id") or "").strip()
    registry = PRODUCT_REGISTRY.get((model_id, provider_id))
    if registry is None:
        raise ContractError("republisher_oos_artifact_product_unregistered")
    artifact_facts: list[dict[str, Any]] = []
    for station in requested:
        candidate = candidates[station]
        rows = candidate.get("cohort_rows")
        if not isinstance(rows, list) or len(rows) != 28:
            raise ContractError("republisher_oos_artifact_cohort_rows_missing")
        settlement_definition = _settlement_definition(database, station)
        scope_identity = {
            "station_id": station,
            "upstream_model_id": model_id,
            "republisher_product_id": registry["republisher_product_id"],
            "independence_family_id": registry["independence_family_id"],
            "source_class": "aggregator_republisher",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "settlement_timezone": str(candidate.get("settlement_timezone") or ""),
            "settlement_definition": settlement_definition,
            "lead_bucket": str(candidate.get("lead_bucket") or ""),
            "run_cycle": candidate.get("run_cycle"),
            "run_cycle_scope": str(candidate.get("run_cycle_scope") or ""),
            "season_bin": str(candidate.get("season_bin") or ""),
            "forecast_extraction_version": str(
                candidate.get("forecast_extraction_version") or ""
            ),
            "artifact_method_version": ARTIFACT_METHOD_VERSION,
            "oos_protocol_version": OOS_PROTOCOL_VERSION,
            "cohort_rows": rows,
        }
        strict_oos_cohort_sha256 = content_sha256(scope_identity)
        train_case_set_sha256 = str(candidate.get("train_case_set_sha256") or "")
        holdout_case_set_sha256 = str(candidate.get("holdout_case_set_sha256") or "")
        natural_identity = {
            key: scope_identity[key]
            for key in (
                "station_id",
                "upstream_model_id",
                "republisher_product_id",
                "independence_family_id",
                "source_class",
                "authority_scope",
                "settlement_timezone",
                "settlement_definition",
                "lead_bucket",
                "run_cycle",
                "run_cycle_scope",
                "season_bin",
                "forecast_extraction_version",
                "artifact_method_version",
                "oos_protocol_version",
            )
        }
        natural_identity.update(
            {
                "train_case_set_sha256": train_case_set_sha256,
                "holdout_case_set_sha256": holdout_case_set_sha256,
            }
        )
        artifact_id = (
            "republisher-historical-oos-"
            + content_sha256(natural_identity)[:40]
        )
        fact_identity = {
            "artifact_id": artifact_id,
            "station_id": station,
            "upstream_model_id": model_id,
            "republisher_product_id": registry["republisher_product_id"],
            "independence_family_id": registry["independence_family_id"],
            "transport_provider_id": "OPEN_METEO",
            "model_family": "global_deterministic",
            "source_class": "aggregator_republisher",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "settlement_timezone": scope_identity["settlement_timezone"],
            "settlement_definition": settlement_definition,
            "lead_bucket": scope_identity["lead_bucket"],
            "run_cycle": scope_identity["run_cycle"],
            "run_cycle_scope": scope_identity["run_cycle_scope"],
            "season_bin": scope_identity["season_bin"],
            "forecast_extraction_version": scope_identity[
                "forecast_extraction_version"
            ],
            "artifact_method_version": ARTIFACT_METHOD_VERSION,
            "oos_protocol_version": OOS_PROTOCOL_VERSION,
            "train_n": int(candidate.get("train_n") or 0),
            "holdout_n": int(candidate.get("holdout_n") or 0),
            "cohort_rows": rows,
            "train_case_set_sha256": train_case_set_sha256,
            "holdout_case_set_sha256": holdout_case_set_sha256,
            "strict_oos_cohort_sha256": strict_oos_cohort_sha256,
            "evaluation_content_sha256": str(candidate.get("content_sha256") or ""),
            "calibration_parameters": {
                "bias_c": candidate.get("train_bias_c"),
                "train_climatology_tmax_c": candidate.get(
                    "train_climatology_tmax_c"
                ),
            },
            "model_metrics": candidate.get("model_metrics"),
            "climatology_baseline_metrics": candidate.get(
                "climatology_baseline_metrics"
            ),
            "state": "PRODUCTION_NO_LIVE_OOS_PASS",
            "native_arrival_indexed": False,
            "p1_eligible": False,
            "formal_live_authority": False,
            "future_event_count": 0,
        }
        artifact_facts.append(
            {**fact_identity, "content_sha256": content_sha256(fact_identity)}
        )
    identity = {
        "schema": REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA,
        "as_of_utc": str(report.get("as_of_utc") or ""),
        "builder_id": str(builder_id or "").strip(),
        "artifact_facts": artifact_facts,
    }
    packet = {**identity, "content_sha256": content_sha256(identity)}
    return CanonicalRepublisherOOSArtifactContract.from_mapping(packet).to_mapping()


def canonical_json(packet: Mapping[str, Any]) -> str:
    """Return deterministic JSON for a receipt or CLI output."""

    return json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "ARTIFACT_METHOD_VERSION",
    "OOS_PROTOCOL_VERSION",
    "PRODUCT_REGISTRY",
    "build_republisher_oos_artifact_contract",
    "canonical_json",
]
