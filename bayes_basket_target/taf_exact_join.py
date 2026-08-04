"""Fail-closed exact join for conditional TAF calibration.

TAF may only be calibrated against a P0 prediction that was produced by a
strict OOS/cross-fit, minimum-two-model ensemble at the same decision anchor.
An explicit ``historical_republisher_replay`` lineage is accepted only for the
separate no-live candidate lane; it never grants native arrival or live
authority.  This module deliberately performs no network or database I/O.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .taf_oos_contract import (
    HISTORICAL_OOS_TEMPORAL_SCOPE,
    assert_historical_taf_oos,
)
from .settlement_time import settlement_day_window


TAF_EXACT_JOIN_SCHEMA = "bayes_basket_taf_increment_calibration_dataset_v2"
NWP_CROSSFIT_SCHEMA = "bayes_basket_nwp_crossfit_predictions_v1"
MAX_EXACT_JOIN_ROWS = 366
MAX_EXACT_JOIN_OUTPUT_BYTES = 16 * 1024 * 1024


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_required")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


def _utc(name: str, value: Any) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _model_lineage(row: Mapping[str, Any], *, decision_at: datetime) -> tuple[str, ...]:
    selected = row.get("selected_models")
    weights = row.get("selected_weights")
    model_rows = row.get("model_rows")
    if not isinstance(selected, list) or len(selected) < 2:
        raise ContractError("taf_exact_join_minimum_two_models_required")
    models = tuple(sorted({_text("selected_model", item).upper() for item in selected}))
    if len(models) != len(selected) or len(models) < 2:
        raise ContractError("taf_exact_join_selected_models_invalid")
    if not isinstance(weights, Mapping) or set(str(key).upper() for key in weights) != set(models):
        raise ContractError("taf_exact_join_selected_weights_scope_mismatch")
    parsed_weights = {str(key).upper(): float(value) for key, value in weights.items()}
    if any(value <= 0.0 for value in parsed_weights.values()):
        raise ContractError("taf_exact_join_selected_weight_nonpositive")
    if abs(sum(parsed_weights.values()) - 1.0) > 1e-9:
        raise ContractError("taf_exact_join_selected_weights_not_normalized")
    if not isinstance(model_rows, list) or len(model_rows) != len(models):
        raise ContractError("taf_exact_join_model_lineage_incomplete")
    by_model: dict[str, Mapping[str, Any]] = {}
    for item in model_rows:
        if not isinstance(item, Mapping):
            raise ContractError("taf_exact_join_model_lineage_invalid")
        model_id = _text("model_id", item.get("model_id")).upper()
        if model_id in by_model:
            raise ContractError("taf_exact_join_model_lineage_duplicate")
        by_model[model_id] = item
    if set(by_model) != set(models):
        raise ContractError("taf_exact_join_model_lineage_scope_mismatch")
    for model_id in models:
        item = by_model[model_id]
        _text("model_run_id", item.get("model_run_id"))
        _sha("source_object_sha256", item.get("source_object_sha256"))
        _text("raw_object_ref", item.get("raw_object_ref"))
        if item.get("raw_object_verified") is not True:
            raise ContractError("taf_exact_join_raw_object_verification_required")
        availability_class = _text(
            "availability_class", item.get("availability_class")
        )
        if availability_class == "native_receipt":
            if _utc("model_published_at_utc", item.get("published_at_utc")) > decision_at:
                raise ContractError("taf_exact_join_model_published_after_decision")
            if _utc("model_available_at_utc", item.get("available_at_utc")) > decision_at:
                raise ContractError("taf_exact_join_model_available_after_decision")
        elif availability_class == "official_publication_schedule_historical_replay":
            if _utc("model_published_at_utc", item.get("published_at_utc")) > decision_at:
                raise ContractError("taf_exact_join_model_published_after_decision")
            if item.get("available_at_utc") not in {None, ""}:
                raise ContractError(
                    "taf_exact_join_historical_native_arrival_forbidden"
                )
        elif availability_class == "historical_republisher_replay":
            # Open-Meteo is an explicit retrospective fallback.  It has no
            # native publication/arrival timestamp, so the immutable model
            # run/replay anchor is checked instead.  This branch is candidate
            # only and never grants native arrival, P1, or live authority.
            replay_anchor = _utc(
                "historical_replay_anchor_utc",
                item.get("historical_replay_anchor_utc")
                or item.get("published_at_utc"),
            )
            if replay_anchor > decision_at:
                raise ContractError("taf_exact_join_replay_anchor_after_decision")
            if item.get("available_at_utc") not in {None, ""}:
                raise ContractError(
                    "taf_exact_join_republisher_arrival_forbidden"
                )
            if item.get("receipt_class") != "HISTORICAL_ASOF_REPUBLISHER_RECEIPT":
                raise ContractError(
                    "taf_exact_join_republisher_receipt_class_required"
                )
            _text(
                "historical_oos_contract_id",
                item.get("historical_oos_contract_id"),
            )
            if item.get("historical_oos_eligible") is not True:
                raise ContractError(
                    "taf_exact_join_republisher_historical_oos_required"
                )
        else:
            raise ContractError("taf_exact_join_availability_class_invalid")
    return models


def build_taf_exact_join(
    taf_dataset: Mapping[str, Any],
    nwp_crossfit_dataset: Mapping[str, Any],
    *,
    max_rows: int = MAX_EXACT_JOIN_ROWS,
) -> dict[str, Any]:
    """Return only formally joined rows plus explicit per-day blockers."""

    if taf_dataset.get("schema") != "bayes_basket_awc_taf_calibration_dataset_v1":
        raise ContractError("taf_exact_join_taf_schema_invalid")
    if nwp_crossfit_dataset.get("schema") != NWP_CROSSFIT_SCHEMA:
        raise ContractError("taf_exact_join_nwp_schema_invalid")
    if max_rows < 1 or max_rows > MAX_EXACT_JOIN_ROWS:
        raise ContractError("taf_exact_join_max_rows_invalid")
    taf_rows = taf_dataset.get("samples")
    nwp_rows = nwp_crossfit_dataset.get("predictions")
    if not isinstance(taf_rows, list) or not isinstance(nwp_rows, list):
        raise ContractError("taf_exact_join_rows_invalid")
    if len(taf_rows) > max_rows or len(nwp_rows) > max_rows:
        raise ContractError("taf_exact_join_resource_bound_exceeded")
    crossfit_sha = _sha(
        "crossfit_prediction_artifact_sha256",
        nwp_crossfit_dataset.get("crossfit_prediction_artifact_sha256"),
    )
    nwp_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in nwp_rows:
        if not isinstance(row, Mapping):
            raise ContractError("taf_exact_join_nwp_row_invalid")
        key = (
            _text("nwp_station_id", row.get("station_id")).upper(),
            _text("nwp_event_date", row.get("event_date")),
        )
        if key in nwp_by_key:
            raise ContractError("taf_exact_join_nwp_key_duplicate")
        nwp_by_key[key] = row

    joined: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    excluded_samples: list[dict[str, str]] = []
    burn_in_dates = {
        str(value)
        for value in nwp_crossfit_dataset.get("excluded_burn_in_dates", [])
    }
    seen_taf: set[tuple[str, str]] = set()
    for taf_row in taf_rows:
        if not isinstance(taf_row, Mapping):
            raise ContractError("taf_exact_join_taf_row_invalid")
        station = _text("taf_station_id", taf_row.get("station_id")).upper()
        event_date = _text("taf_event_date", taf_row.get("event_date"))
        key = (station, event_date)
        if key in seen_taf:
            raise ContractError("taf_exact_join_taf_key_duplicate")
        seen_taf.add(key)
        nwp = nwp_by_key.get(key)
        if nwp is None:
            if event_date in burn_in_dates:
                excluded_samples.append({
                    "station_id": station,
                    "event_date": event_date,
                    "reason": "rolling_origin_training_burn_in",
                })
                continue
            blockers.append({"station_id": station, "event_date": event_date, "reason": "nwp_crossfit_prediction_missing"})
            continue
        try:
            anchor_text = _text("decision_anchor_utc", taf_row.get("decision_anchor_utc"))
            _utc("decision_anchor_utc", anchor_text)
            nwp_decision_text = _text(
                "nwp_decision_at_utc", nwp.get("decision_at_utc")
            )
            nwp_decision = _utc(
                "nwp_decision_at_utc", nwp_decision_text
            )
            timezone_name = _text("settlement_timezone", taf_row.get("settlement_timezone"))
            if _text("nwp_settlement_timezone", nwp.get("settlement_timezone")) != timezone_name:
                raise ContractError("taf_exact_join_settlement_timezone_mismatch")
            if nwp.get("prediction_oos") is not True:
                raise ContractError("taf_exact_join_nwp_oos_required")
            if _sha("nwp_crossfit_artifact_sha256", nwp.get("crossfit_prediction_artifact_sha256")) != crossfit_sha:
                raise ContractError("taf_exact_join_crossfit_artifact_mismatch")
            models = _model_lineage(nwp, decision_at=nwp_decision)
            taf = taf_row.get("taf")
            truth = taf_row.get("truth")
            window = taf_row.get("settlement_window")
            if not isinstance(taf, Mapping) or not isinstance(truth, Mapping) or not isinstance(window, Mapping):
                raise ContractError("taf_exact_join_taf_truth_window_incomplete")
            truth_closed = _utc(
                "taf_exact_join_truth_closed_at",
                window.get("end_utc")
                or settlement_day_window(
                    event_date, taf_row.get("settlement_timezone")
                )["target_day_end_utc"],
            )
            temporal = assert_historical_taf_oos(
                taf_row,
                event_date=event_date,
                truth_closed_at=truth_closed,
                transport_values=tuple(
                    str(item.get("provider_id") or item.get("transport_provider_id") or "")
                    for item in (nwp.get("model_rows") or [])
                    if isinstance(item, Mapping)
                ),
                name="taf_exact_join",
            )
            if str(taf.get("source_class") or "") == "official_forward_native_receipt":
                raise ContractError("taf_exact_join_forward_taf_not_oos")
            if nwp_decision > _utc(
                "taf_available_time_utc", taf.get("available_time_utc")
            ):
                raise ContractError(
                    "taf_exact_join_nwp_combination_after_taf_arrival"
                )
            selection_sha = _sha("selection_evidence_sha256", nwp.get("selection_evidence_sha256"))
            receipts = taf_row.get("receipts")
            if not isinstance(receipts, Mapping):
                receipts = {}
            joined.append({
                "sample_id": _text("sample_id", taf_row.get("sample_id")),
                "station_id": station,
                "event_date": event_date,
                "decision_anchor_utc": anchor_text,
                "settlement_timezone": timezone_name,
                "settlement_window": dict(window),
                "taf": {
                    key: taf[key]
                    for key in (
                        "issue_time_utc", "available_time_utc", "tx_c",
                        "cloud_cover_fraction", "precipitation_probability",
                        "wind_speed_mps",
                    )
                },
                "truth": {
                    "source_lane": _text("truth_source_lane", truth.get("source_lane")),
                    "tmax_c": float(truth["tmax_c"]),
                    "observation_count": int(truth.get("observation_count") or 0),
                },
                "lineage": {
                    "raw_taf_sha256": _sha(
                        "raw_taf_sha256", taf.get("raw_taf_sha256")
                    ),
                    "taf_response_sha256": _sha(
                        "taf_response_sha256", receipts.get("taf_response_sha256")
                    ),
                    "truth_response_sha256": _sha(
                        "truth_response_sha256", receipts.get("metar_response_sha256")
                    ),
                },
                "calibration_scope": {
                    "season_bin": _text("season_bin", nwp.get("season_bin")),
                    "lead_bucket": _text("lead_bucket", nwp.get("lead_bucket")),
                    "run_cycle": _text("run_cycle", nwp.get("run_cycle")),
                    "settlement_definition": _text(
                        "settlement_definition", nwp.get("settlement_definition")
                    ),
                },
                "nwp_combination": {
                    "artifact_sha256": crossfit_sha,
                    "selection_evidence_sha256": selection_sha,
                    "decision_at_utc": nwp_decision_text,
                    "prediction_oos": True,
                    "selected_models": list(models),
                    "combined_tmax_c": float(nwp["combined_tmax_c"]),
                    "model_rows": [dict(item) for item in nwp["model_rows"]],
                    **temporal,
                },
                **temporal,
            })
        except (ContractError, KeyError, TypeError, ValueError) as exc:
            blockers.append({
                "station_id": station,
                "event_date": event_date,
                "reason": str(exc),
            })

    identity = {
        "schema": TAF_EXACT_JOIN_SCHEMA,
        "source_class": "strict_oos_crossfit_exact_join",
        "source_taf_dataset_sha256": content_sha256(taf_dataset),
        "nwp_crossfit_prediction_artifact_sha256": crossfit_sha,
        "sample_count": len(joined),
        "blocker_count": len(blockers),
        "excluded_sample_count": len(excluded_samples),
        "samples": joined,
        "blockers": blockers,
        "excluded_samples": excluded_samples,
        "boundary": {
            "historical_replay_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "minimum_selected_models": 2,
            "strict_oos_crossfit_required": True,
            "oos_event_temporal_scope": HISTORICAL_OOS_TEMPORAL_SCOPE,
        },
        "resource_bounds": {
            "max_rows": max_rows,
            "max_output_bytes": MAX_EXACT_JOIN_OUTPUT_BYTES,
            "input_taf_rows": len(taf_rows),
            "input_nwp_rows": len(nwp_rows),
        },
    }
    if len(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")) > MAX_EXACT_JOIN_OUTPUT_BYTES:
        raise ContractError("taf_exact_join_output_resource_bound_exceeded")
    return {**identity, "content_sha256": content_sha256(identity)}
