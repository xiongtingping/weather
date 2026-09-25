"""One explicit V13 builder for one lead and one model-family scope.

The builder never discovers a cohort, chooses dates, or invents clocks.  Its
caller supplies exact model receipts, exact truth roots, and the 20+8 date
partition.  The resulting packet still enters SSOT only through
``canonical_ingress``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .canonical_contract import FORMAL_TRUTH_LANES, SETTLEMENT_SOURCE_TRUTH_ROLE
from .canonical_contract import CanonicalTruthFact
from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .contracts import ContractError, content_sha256
from .model_oos_contract import (
    AUTHORITY_SCOPE,
    CALIBRATION_METHOD_VERSION,
    CONTRACT_SCHEMA,
    EVENT_STATE,
    CanonicalModelOOSEventFact,
    CanonicalModelOOSContract,
    derive_cohort_statistics,
)
from .resource_bounds import MAX_INPUT_FILES_PER_BATCH, assert_process_memory_budget


BUILDER_ID = "canonical.model_oos.lead_family.v1"
INPUT_FIELDS = {
    "station_id", "model_id", "ensemble_system_id", "model_family",
    "provider_id", "provider_product_id", "independence_family_id",
    "source_class", "event_date", "settlement_timezone", "lead_bucket",
    "lead_bucket_scope", "run_init_utc", "run_cycle", "run_cycle_scope",
    "forecast_value_c", "canonical_truth_fact_content_sha256",
    "event_oos_protocol_version", "model_receipt",
}
COHORT_FIELDS = {
    "season_bin", "calibration_method_version", "train_event_dates",
    "holdout_event_dates",
}


def _utc_text(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("model_oos_builder_truth_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("model_oos_builder_truth_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _exact(raw: Mapping[str, Any], fields: set[str], label: str) -> dict[str, Any]:
    if set(raw) != fields:
        raise ContractError(f"model_oos_builder_{label}_fields_mismatch")
    return dict(raw)


def _truth_fact(
    conn: sqlite3.Connection, content_sha256_value: object
) -> CanonicalTruthFact:
    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth' AND content_sha256=?",
        (str(content_sha256_value),),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError("model_oos_builder_truth_root_not_unique")
    try:
        raw = json.loads(str(rows[0][0]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("model_oos_builder_truth_json_invalid") from exc
    if not isinstance(raw, Mapping):
        raise ContractError("model_oos_builder_truth_mapping_required")
    truth = CanonicalTruthFact.from_mapping(raw)
    if (
        truth.source_lane not in FORMAL_TRUTH_LANES
        or truth.source_role not in {
            "AWC_HISTORICAL_REPLAY_INGRESS",
            "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1",
            SETTLEMENT_SOURCE_TRUTH_ROLE,
        }
        or not truth.closed_day
        or truth.settlement_timezone != "America/New_York"
    ):
        raise ContractError("model_oos_builder_truth_lineage_invalid")
    return truth


def _seal(identity: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(identity)
    return {**value, "content_sha256": content_sha256(value)}


def build_model_oos_contract(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    event_inputs: Sequence[Mapping[str, Any]],
    cohort_spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one exact scope; no cross-lead/family grouping is permitted."""

    inspection = inspect_canonical_ssot_schema(conn)
    if inspection.get("state") not in {"V13", "V14", "V15"} or inspection.get("errors"):
        raise ContractError("model_oos_builder_v13_required")
    if not event_inputs:
        raise ContractError("model_oos_builder_event_input_required")
    if len(event_inputs) > MAX_INPUT_FILES_PER_BATCH:
        raise ContractError("model_oos_builder_event_input_limit_exceeded")
    assert_process_memory_budget(projected_bytes=len(event_inputs) * 16_384)

    events: list[CanonicalModelOOSEventFact] = []
    for raw_input in event_inputs:
        item = _exact(raw_input, INPUT_FIELDS, "event_input")
        receipt = item["model_receipt"]
        if not isinstance(receipt, Mapping):
            raise ContractError("model_oos_builder_model_receipt_mapping_required")
        truth = _truth_fact(conn, item["canonical_truth_fact_content_sha256"])
        if any(
            getattr(truth, field) != item[field]
            for field in ("station_id", "event_date", "settlement_timezone")
        ):
            raise ContractError("model_oos_builder_truth_scope_mismatch")
        if _utc_text(truth.canonical_committed_at_utc) > _utc_text(as_of_utc):
            raise ContractError("model_oos_builder_truth_after_contract_as_of")
        event = _seal({
            **{key: item[key] for key in INPUT_FIELDS if key not in {
                "canonical_truth_fact_content_sha256", "model_receipt"
            }},
            "authority_scope": AUTHORITY_SCOPE,
            "forecast_decision_at_utc": receipt.get("forecast_decision_at_utc"),
            "model_available_at_utc": receipt.get("model_available_at_utc"),
            "raw_object_sha256": receipt.get("raw_object_sha256"),
            "model_input_content_sha256": receipt.get("model_input_content_sha256"),
            "model_receipt": dict(receipt),
            "model_receipt_sha256": receipt.get("content_sha256"),
            "canonical_truth_fact_content_sha256": truth.content_sha256,
            "truth_id": truth.truth_id,
            "truth_revision_no": truth.truth_revision_no,
            "truth_content_sha256": truth.truth_content_sha256,
            "truth_value_c": truth.truth_value,
            "station_day_end_utc": _utc_text(truth.station_day_end_utc),
            "truth_canonical_committed_at_utc": _utc_text(
                truth.canonical_committed_at_utc
            ),
            "state": EVENT_STATE,
        })
        events.append(CanonicalModelOOSEventFact.from_mapping(event))

    scope_fields = (
        "station_id", "model_id", "ensemble_system_id", "model_family",
        "provider_id", "provider_product_id", "independence_family_id",
        "source_class", "authority_scope", "settlement_timezone", "lead_bucket",
        "lead_bucket_scope", "run_cycle", "run_cycle_scope",
        "event_oos_protocol_version",
    )
    if any(
        any(event.raw[field] != events[0].raw[field] for field in scope_fields)
        for event in events[1:]
    ):
        raise ContractError("model_oos_builder_cross_scope_input_forbidden")
    if len({event.raw["event_date"] for event in events}) != len(events):
        raise ContractError("model_oos_builder_event_date_duplicate")
    events.sort(key=lambda event: event.raw["event_date"])

    cohorts: list[dict[str, Any]] = []
    if cohort_spec is not None:
        spec = _exact(cohort_spec, COHORT_FIELDS, "cohort_spec")
        if spec["calibration_method_version"] != CALIBRATION_METHOD_VERSION:
            raise ContractError("model_oos_builder_calibration_method_invalid")
        train_dates = list(spec["train_event_dates"])
        holdout_dates = list(spec["holdout_event_dates"])
        if (
            len(train_dates) != 20 or len(holdout_dates) != 8
            or train_dates != sorted(train_dates)
            or holdout_dates != sorted(holdout_dates)
            or len(set(train_dates + holdout_dates)) != 28
            or train_dates[-1] >= holdout_dates[0]
        ):
            raise ContractError("model_oos_builder_cohort_partition_invalid")
        by_date = {event.raw["event_date"]: event for event in events}
        if set(by_date) != set(train_dates + holdout_dates):
            raise ContractError("model_oos_builder_cohort_event_set_mismatch")
        ordered = [by_date[value] for value in train_dates + holdout_dates]
        parameters, train_metrics, holdout, baseline, evaluation = derive_cohort_statistics(ordered)
        train_hashes = [event.content_sha256 for event in ordered[:20]]
        holdout_hashes = [event.content_sha256 for event in ordered[20:]]
        vector = [
            *({"partition": "train", "event_content_sha256": value} for value in train_hashes),
            *({"partition": "holdout", "event_content_sha256": value} for value in holdout_hashes),
        ]
        cohort_as_of = max(
            max(event.raw["model_available_at_utc"], event.raw["truth_canonical_committed_at_utc"])
            for event in ordered
        )
        base = events[0].raw
        cohorts.append(_seal({
            **{field: base[field] for field in scope_fields},
            "season_bin": spec["season_bin"],
            "calibration_method_version": spec["calibration_method_version"],
            "cohort_as_of_utc": cohort_as_of,
            "train_event_content_sha256s": train_hashes,
            "holdout_event_content_sha256s": holdout_hashes,
            "train_event_set_sha256": content_sha256(train_hashes),
            "holdout_event_set_sha256": content_sha256(holdout_hashes),
            "cohort_event_vector_sha256": content_sha256(vector),
            "calibration_parameters": parameters,
            "selection_train_metrics": train_metrics,
            "holdout_metrics": holdout,
            "baseline_holdout_metrics": baseline,
            "evaluation_state": evaluation,
            "state": "MATERIALIZED_UNPUBLISHED",
        }))

    contract = _seal({
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": as_of_utc,
        "builder_id": BUILDER_ID,
        "event_facts": [event.raw for event in events],
        "cohort_facts": cohorts,
    })
    result = CanonicalModelOOSContract.from_mapping(contract).to_mapping()
    assert_process_memory_budget()
    return result


__all__ = ["BUILDER_ID", "build_model_oos_contract"]
