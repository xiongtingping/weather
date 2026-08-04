"""Append-only promotion and provider authority for deterministic AIFS Single.

This is intentionally separate from both the legacy ``MODEL_ENSEMBLE``
activation contract and the native-member ensemble tables.  A promoted record
means only that a product-specific official AIFS deterministic *provider*
has passed its own strict OOS gate.  It cannot by itself compile P0, count as
another ECMWF family, or authorise a scheduler substitution before a future
deterministic-aware P0-role adapter binds it to an independent non-ECMWF
family.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import math
from typing import Any

from .aifs_deterministic_oos import (
    INDEPENDENCE_FAMILY_ID,
    MIN_HOLDOUT_DAYS,
    MIN_TRAIN_DAYS,
    MODEL_ID,
    PRODUCT_ID,
    PROVIDER_ID,
    PREDICTION_SCHEMA,
    STRICT_OOS_SCHEMA,
    validate_aifs_deterministic_training_batch,
)
from .contracts import ContractError, content_sha256


PROMOTION_RECEIPT_SCHEMA = (
    "ecmwf_aifs_single_official_deterministic_promotion_receipt_v1"
)
PROVIDER_AUTHORITY_SCHEMA = (
    "ecmwf_aifs_single_official_deterministic_provider_authority_v1"
)
GATE_VERSION = "aifs_deterministic_closed_awc_rolling_oos_gate_v1"
MAX_AUTHORITY_VALID_DAYS = 90


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"aifs_deterministic_authority_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(
            f"aifs_deterministic_authority_{name}_timezone_required"
        )
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(
            f"aifs_deterministic_authority_{name}_invalid_sha256"
        )
    return result


def _finite(name: str, value: Any, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise ContractError(f"aifs_deterministic_authority_{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(
            f"aifs_deterministic_authority_{name}_invalid"
        ) from exc
    if not math.isfinite(result) or (nonnegative and result < 0.0):
        raise ContractError(f"aifs_deterministic_authority_{name}_invalid")
    return result


def _hash_identity(raw: Mapping[str, Any], *, label: str) -> None:
    supplied = _sha(label, raw.get("content_sha256"))
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError(f"aifs_deterministic_authority_{label}_sha_mismatch")


def _verify_prediction(
    raw: Mapping[str, Any],
    *,
    facts_by_sha: Mapping[str, Mapping[str, Any]],
) -> None:
    if not isinstance(raw, Mapping) or raw.get("schema") != PREDICTION_SCHEMA:
        raise ContractError("aifs_deterministic_authority_prediction_schema_invalid")
    _hash_identity(raw, label="prediction")
    if (
        raw.get("product_id") != PRODUCT_ID
        or raw.get("provider_id") != PROVIDER_ID
        or raw.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
        or raw.get("strict_oos") is not True
        or raw.get("formal_p0_eligible") is not False
        or int(raw.get("train_case_count") or 0) < MIN_TRAIN_DAYS
    ):
        raise ContractError("aifs_deterministic_authority_prediction_identity_invalid")
    holdout_sha = _sha("prediction_holdout_fact", raw.get("holdout_fact_content_sha256"))
    holdout = facts_by_sha.get(holdout_sha)
    if holdout is None:
        raise ContractError("aifs_deterministic_authority_prediction_holdout_missing")
    if (
        str(raw.get("station_id") or "") != str(holdout["station_id"])
        or str(raw.get("event_date") or "") != str(holdout["event_date"])
        or str(raw.get("target_timezone") or "") != str(holdout["target_timezone"])
        or str(raw.get("run_cycle") or "") != str(holdout["run_cycle"])
    ):
        raise ContractError("aifs_deterministic_authority_prediction_scope_mismatch")
    decision = _utc("prediction_decision", raw.get("decision_at_utc"))
    if decision != _utc("holdout_curve_available", holdout["curve_available_at_utc"]):
        raise ContractError("aifs_deterministic_authority_prediction_clock_mismatch")
    train_shas = raw.get("train_fact_content_sha256s")
    if not isinstance(train_shas, list) or len(train_shas) != int(raw["train_case_count"]):
        raise ContractError("aifs_deterministic_authority_prediction_train_missing")
    train_rows = []
    seen: set[str] = set()
    for value in train_shas:
        digest = _sha("prediction_train_fact", value)
        if digest in seen or digest not in facts_by_sha:
            raise ContractError("aifs_deterministic_authority_prediction_train_invalid")
        seen.add(digest)
        row = facts_by_sha[digest]
        if (
            str(row["station_id"]) != str(holdout["station_id"])
            or str(row["target_timezone"]) != str(holdout["target_timezone"])
            or str(row["run_cycle"]) != str(holdout["run_cycle"])
            or str(row["event_date"]) >= str(holdout["event_date"])
            or _utc("train_truth_available", row["truth_available_at_utc"])
            > decision
        ):
            raise ContractError("aifs_deterministic_authority_prediction_leakage")
        train_rows.append(row)
    if str(raw.get("trained_through_event_date") or "") != str(
        train_rows[-1]["event_date"]
    ):
        raise ContractError("aifs_deterministic_authority_prediction_train_end_invalid")
    for name in (
        "bias_c",
        "predicted_tmax_c",
        "official_tmax_c",
        "error_c",
        "climatology_predicted_tmax_c",
        "climatology_error_c",
    ):
        _finite("prediction_" + name, raw.get(name))


def _metrics(raw: Any, *, name: str) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        raise ContractError(f"aifs_deterministic_authority_{name}_missing")
    return {
        "mae_c": _finite(name + "_mae", raw.get("mae_c"), nonnegative=True),
        "rmse_c": _finite(name + "_rmse", raw.get("rmse_c"), nonnegative=True),
        "mean_error_c": _finite(name + "_mean_error", raw.get("mean_error_c")),
    }


def _validate_oos_report(
    report: Mapping[str, Any],
    *,
    training_batch: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    facts = validate_aifs_deterministic_training_batch(training_batch)
    facts_by_sha = {str(row["content_sha256"]): row for row in facts}
    if len(facts_by_sha) != len(facts):
        raise ContractError("aifs_deterministic_authority_training_fact_duplicate")
    if not isinstance(report, Mapping) or report.get("schema") != STRICT_OOS_SCHEMA:
        raise ContractError("aifs_deterministic_authority_oos_schema_invalid")
    _hash_identity(report, label="oos_report")
    if (
        report.get("state") != "STRICT_OOS_CANDIDATES_NOT_BINDING"
        or report.get("product_id") != PRODUCT_ID
        or report.get("provider_id") != PROVIDER_ID
        or report.get("model_id") != MODEL_ID
        or report.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
        or report.get("same_independence_family_as_ifs") is not True
        or report.get("counts_as_distinct_independence_family") is not False
        or report.get("formal_p0_eligible") is not False
        or report.get("formal_provider_binding_generated") is not False
        or str(report.get("training_batch_content_sha256") or "")
        != str(training_batch.get("content_sha256") or "")
    ):
        raise ContractError("aifs_deterministic_authority_oos_identity_invalid")
    candidates = report.get("candidates")
    if (
        not isinstance(candidates, list)
        or not candidates
        or int(report.get("candidate_count") or -1) != len(candidates)
    ):
        raise ContractError("aifs_deterministic_authority_candidates_missing")
    result: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or candidate.get("schema") != STRICT_OOS_SCHEMA:
            raise ContractError("aifs_deterministic_authority_candidate_schema_invalid")
        _hash_identity(candidate, label="candidate")
        digest = str(candidate["content_sha256"])
        if digest in result:
            raise ContractError("aifs_deterministic_authority_candidate_duplicate")
        if (
            candidate.get("state") != "STRICT_OOS_CANDIDATE_NOT_BINDING"
            or candidate.get("product_id") != PRODUCT_ID
            or candidate.get("provider_id") != PROVIDER_ID
            or candidate.get("model_id") != MODEL_ID
            or candidate.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
            or candidate.get("same_independence_family_as_ifs") is not True
            or candidate.get("counts_as_distinct_independence_family") is not False
            or int(candidate.get("min_train_days") or 0) < MIN_TRAIN_DAYS
            or int(candidate.get("min_holdout_days") or 0) < MIN_HOLDOUT_DAYS
            or not str(candidate.get("season_bin") or "").strip()
            or not str(candidate.get("lead_bucket") or "").strip()
            or str(candidate.get("training_batch_content_sha256") or "")
            != str(training_batch.get("content_sha256") or "")
        ):
            raise ContractError("aifs_deterministic_authority_candidate_identity_invalid")
        checks = candidate.get("checks")
        boundary = candidate.get("boundary")
        if (
            not isinstance(checks, Mapping)
            or any(
                checks.get(key) is not True
                for key in (
                    "official_native_aifs_only",
                    "closed_day_awc_truth_only",
                    "rolling_origin_strict",
                    "training_truth_available_by_prediction",
                )
            )
            or checks.get("native_ensemble_tables_written") is not False
            or checks.get("formal_provider_binding_generated") is not False
            or checks.get("formal_p0_eligible") is not False
            or not isinstance(boundary, Mapping)
            or boundary.get("writes_native_ensemble_tables") is not False
            or boundary.get("creates_formal_provider_binding") is not False
        ):
            raise ContractError("aifs_deterministic_authority_candidate_boundary_invalid")
        predictions = candidate.get("predictions")
        if (
            not isinstance(predictions, list)
            or len(predictions) != int(candidate["min_holdout_days"])
        ):
            raise ContractError("aifs_deterministic_authority_holdout_count_invalid")
        for prediction in predictions:
            _verify_prediction(prediction, facts_by_sha=facts_by_sha)
        model_metrics = _metrics(candidate.get("metrics"), name="model_metrics")
        climatology_metrics = _metrics(
            candidate.get("climatology_baseline_metrics"),
            name="climatology_metrics",
        )
        expected_state = (
            "STRICT_OOS_PASS"
            if (
                model_metrics["mae_c"] <= climatology_metrics["mae_c"] + 1e-12
                and model_metrics["rmse_c"]
                <= climatology_metrics["rmse_c"] + 1e-12
            )
            else "STRICT_OOS_REJECTED"
        )
        if candidate.get("oos_state") != expected_state:
            raise ContractError("aifs_deterministic_authority_candidate_oos_state_invalid")
        result[digest] = candidate
    return result


def validate_aifs_deterministic_oos_report(
    report: Mapping[str, Any],
    *,
    training_batch: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Validate one immutable deterministic OOS report for authority use."""

    return _validate_oos_report(report, training_batch=training_batch)


def build_aifs_deterministic_promotion_receipt(
    *,
    training_batch: Mapping[str, Any],
    strict_oos_report: Mapping[str, Any],
    candidate_content_sha256: str,
    decided_at_utc: str,
    decision_actor: str,
) -> dict[str, Any]:
    """Return one immutable promotion/rejection receipt for one exact scope.

    A rejected OOS candidate is retained as an append-only rejection receipt;
    it can never yield provider authority.  A pass promotes only the AIFS
    deterministic provider evidence, never a P0 role or native ensemble.
    """

    candidates = _validate_oos_report(
        strict_oos_report, training_batch=training_batch
    )
    candidate_sha = _sha("candidate", candidate_content_sha256)
    candidate = candidates.get(candidate_sha)
    if candidate is None:
        raise ContractError("aifs_deterministic_authority_candidate_not_found")
    actor = str(decision_actor or "").strip()
    if not actor or len(actor) > 160:
        raise ContractError("aifs_deterministic_authority_decision_actor_invalid")
    decided = _utc("promotion_decided", decided_at_utc)
    holdout_end = str(candidate["holdout_end_date"])
    facts = validate_aifs_deterministic_training_batch(training_batch)
    holdout_truth_times = [
        _utc("holdout_truth_available", row["truth_available_at_utc"])
        for row in facts
        if str(row["event_date"]) <= holdout_end
        and str(row["station_id"]) == str(candidate["station_id"])
        and str(row["target_timezone"]) == str(candidate["target_timezone"])
        and str(row["run_cycle"]) == str(candidate["run_cycle"])
    ]
    if not holdout_truth_times or decided < max(holdout_truth_times):
        raise ContractError("aifs_deterministic_authority_promotion_before_truth")
    promoted = candidate.get("oos_state") == "STRICT_OOS_PASS"
    identity = {
        "schema": PROMOTION_RECEIPT_SCHEMA,
        "promotion_id": "aifs-det-promotion-" + candidate_sha[:32],
        "decision_seq": 1,
        "decision": "PROMOTED" if promoted else "REJECTED",
        "gate_version": GATE_VERSION,
        "decision_actor": actor,
        "decided_at_utc": _stamp(decided),
        "product_id": PRODUCT_ID,
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "same_independence_family_as_ifs": True,
        "counts_as_distinct_independence_family": False,
        "strict_oos_report_content_sha256": strict_oos_report[
            "content_sha256"
        ],
        "strict_oos_candidate_content_sha256": candidate_sha,
        "training_batch_content_sha256": training_batch["content_sha256"],
        "authority_scope_if_promoted": "FORMAL_DETERMINISTIC_PROVIDER_ONLY",
        "formal_p0_role_activated": False,
        "reason_codes": (
            [] if promoted else ["strict_rolling_oos_not_passing_climatology"]
        ),
        "boundary": {
            "append_only_decision": True,
            "writes_production_database": False,
            "writes_native_ensemble_tables": False,
            "creates_p0_role_activation": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {
        **identity,
        "receipt_sha256": content_sha256(identity),
        "content_sha256": content_sha256({
            **identity,
            "receipt_sha256": content_sha256(identity),
        }),
    }


def _validate_promotion_receipt(
    receipt: Mapping[str, Any],
    *,
    strict_oos_report: Mapping[str, Any],
    training_batch: Mapping[str, Any],
) -> Mapping[str, Any]:
    candidates = _validate_oos_report(
        strict_oos_report, training_batch=training_batch
    )
    if not isinstance(receipt, Mapping) or receipt.get("schema") != PROMOTION_RECEIPT_SCHEMA:
        raise ContractError("aifs_deterministic_authority_receipt_schema_invalid")
    receipt_identity = {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_sha256", "content_sha256"}
    }
    receipt_sha = _sha("promotion_receipt", receipt.get("receipt_sha256"))
    if receipt_sha != content_sha256(receipt_identity):
        raise ContractError("aifs_deterministic_authority_receipt_sha_mismatch")
    content_identity = {**receipt_identity, "receipt_sha256": receipt_sha}
    if _sha("promotion_content", receipt.get("content_sha256")) != content_sha256(content_identity):
        raise ContractError("aifs_deterministic_authority_receipt_content_mismatch")
    candidate_sha = _sha(
        "receipt_candidate", receipt.get("strict_oos_candidate_content_sha256")
    )
    candidate = candidates.get(candidate_sha)
    if candidate is None:
        raise ContractError("aifs_deterministic_authority_receipt_candidate_missing")
    expected_decision = (
        "PROMOTED"
        if candidate.get("oos_state") == "STRICT_OOS_PASS"
        else "REJECTED"
    )
    if (
        receipt.get("promotion_id") != "aifs-det-promotion-" + candidate_sha[:32]
        or int(receipt.get("decision_seq") or 0) != 1
        or receipt.get("decision") != expected_decision
        or receipt.get("gate_version") != GATE_VERSION
        or not str(receipt.get("decision_actor") or "").strip()
        or receipt.get("product_id") != PRODUCT_ID
        or receipt.get("provider_id") != PROVIDER_ID
        or receipt.get("model_id") != MODEL_ID
        or receipt.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
        or receipt.get("same_independence_family_as_ifs") is not True
        or receipt.get("counts_as_distinct_independence_family") is not False
        or receipt.get("strict_oos_report_content_sha256")
        != strict_oos_report.get("content_sha256")
        or receipt.get("training_batch_content_sha256")
        != training_batch.get("content_sha256")
        or receipt.get("authority_scope_if_promoted")
        != "FORMAL_DETERMINISTIC_PROVIDER_ONLY"
        or receipt.get("formal_p0_role_activated") is not False
    ):
        raise ContractError("aifs_deterministic_authority_receipt_identity_invalid")
    _utc("receipt_decided", receipt.get("decided_at_utc"))
    return candidate


def validate_aifs_deterministic_promotion_receipt(
    receipt: Mapping[str, Any],
    *,
    strict_oos_report: Mapping[str, Any],
    training_batch: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate one append-only receipt and return its exact OOS candidate."""

    return _validate_promotion_receipt(
        receipt,
        strict_oos_report=strict_oos_report,
        training_batch=training_batch,
    )


def issue_aifs_deterministic_provider_authority(
    *,
    training_batch: Mapping[str, Any],
    strict_oos_report: Mapping[str, Any],
    promotion_receipt: Mapping[str, Any],
    valid_from_utc: str,
    valid_until_utc: str,
) -> dict[str, Any]:
    """Materialize authority from exactly one immutable passing receipt.

    The returned object is append-only evidence for a future deterministic P0
    role adapter.  It is intentionally incompatible with the legacy
    ``ModelEnsembleArtifact`` scheduler binding and has no order authority.
    """

    candidate = _validate_promotion_receipt(
        promotion_receipt,
        strict_oos_report=strict_oos_report,
        training_batch=training_batch,
    )
    if promotion_receipt.get("decision") != "PROMOTED":
        raise ContractError("aifs_deterministic_authority_promotion_not_passing")
    valid_from = _utc("authority_valid_from", valid_from_utc)
    valid_until = _utc("authority_valid_until", valid_until_utc)
    decided = _utc("authority_decided", promotion_receipt["decided_at_utc"])
    if (
        valid_from < decided
        or valid_until <= valid_from
        or valid_until > valid_from + timedelta(days=MAX_AUTHORITY_VALID_DAYS)
    ):
        raise ContractError("aifs_deterministic_authority_validity_invalid")
    lineage = {
        "publication_authority": "official_native",
        "forecast_role": "DETERMINISTIC_SINGLE",
        "product_id": PRODUCT_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "same_independence_family_as_ifs": True,
        "counts_as_distinct_independence_family": False,
    }
    authority_seed = {
        "promotion_receipt_content_sha256": promotion_receipt["content_sha256"],
        "strict_oos_candidate_content_sha256": candidate["content_sha256"],
        "station_id": candidate["station_id"],
        "season_bin": candidate["season_bin"],
        "lead_bucket": candidate["lead_bucket"],
        "run_cycle": candidate["run_cycle"],
        "valid_from_utc": _stamp(valid_from),
    }
    identity = {
        "schema": PROVIDER_AUTHORITY_SCHEMA,
        "authority_id": "aifs-det-authority-" + content_sha256(authority_seed)[:32],
        "authority_scope": "FORMAL_DETERMINISTIC_PROVIDER_ONLY",
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "product_id": PRODUCT_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "provider_lineage": lineage,
        "station_id": candidate["station_id"],
        "target_timezone": candidate["target_timezone"],
        "season_bin": candidate["season_bin"],
        "lead_bucket": candidate["lead_bucket"],
        "run_cycle": candidate["run_cycle"],
        "strict_oos_report_content_sha256": strict_oos_report["content_sha256"],
        "strict_oos_candidate_content_sha256": candidate["content_sha256"],
        "training_batch_content_sha256": training_batch["content_sha256"],
        "promotion_receipt_content_sha256": promotion_receipt["content_sha256"],
        "promotion_receipt_sha256": promotion_receipt["receipt_sha256"],
        "formal_provider_eligible": True,
        "native_arrival_index_eligible": True,
        "formal_p0_role_eligible": False,
        "requires_deterministic_p0_role_activation": True,
        "native_ensemble_member_tables_prohibited": True,
        "valid_from_utc": _stamp(valid_from),
        "valid_until_utc": _stamp(valid_until),
        "boundary": {
            "append_only_authority": True,
            "writes_production_database": False,
            "writes_native_ensemble_tables": False,
            "creates_p0_role_activation": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def validate_aifs_deterministic_provider_authority(
    authority: Mapping[str, Any],
    *,
    training_batch: Mapping[str, Any],
    strict_oos_report: Mapping[str, Any],
    promotion_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate a stored authority without issuing or mutating anything."""

    candidate = _validate_promotion_receipt(
        promotion_receipt,
        strict_oos_report=strict_oos_report,
        training_batch=training_batch,
    )
    if promotion_receipt.get("decision") != "PROMOTED":
        raise ContractError("aifs_deterministic_authority_promotion_not_passing")
    if not isinstance(authority, Mapping) or authority.get("schema") != PROVIDER_AUTHORITY_SCHEMA:
        raise ContractError("aifs_deterministic_authority_schema_invalid")
    _hash_identity(authority, label="provider_authority")
    valid_from = _utc("stored_authority_valid_from", authority.get("valid_from_utc"))
    valid_until = _utc("stored_authority_valid_until", authority.get("valid_until_utc"))
    decided = _utc("stored_authority_decided", promotion_receipt["decided_at_utc"])
    if (
        valid_from < decided
        or valid_until <= valid_from
        or valid_until > valid_from + timedelta(days=MAX_AUTHORITY_VALID_DAYS)
    ):
        raise ContractError("aifs_deterministic_authority_validity_invalid")
    expected_lineage = {
        "publication_authority": "official_native",
        "forecast_role": "DETERMINISTIC_SINGLE",
        "product_id": PRODUCT_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "same_independence_family_as_ifs": True,
        "counts_as_distinct_independence_family": False,
    }
    authority_seed = {
        "promotion_receipt_content_sha256": promotion_receipt["content_sha256"],
        "strict_oos_candidate_content_sha256": candidate["content_sha256"],
        "station_id": candidate["station_id"],
        "season_bin": candidate["season_bin"],
        "lead_bucket": candidate["lead_bucket"],
        "run_cycle": candidate["run_cycle"],
        "valid_from_utc": _stamp(valid_from),
    }
    if (
        authority.get("authority_id")
        != "aifs-det-authority-" + content_sha256(authority_seed)[:32]
        or authority.get("authority_scope")
        != "FORMAL_DETERMINISTIC_PROVIDER_ONLY"
        or authority.get("provider_id") != PROVIDER_ID
        or authority.get("model_id") != MODEL_ID
        or authority.get("product_id") != PRODUCT_ID
        or authority.get("independence_family_id") != INDEPENDENCE_FAMILY_ID
        or authority.get("provider_lineage") != expected_lineage
        or authority.get("station_id") != candidate.get("station_id")
        or authority.get("target_timezone") != candidate.get("target_timezone")
        or authority.get("season_bin") != candidate.get("season_bin")
        or authority.get("lead_bucket") != candidate.get("lead_bucket")
        or authority.get("run_cycle") != candidate.get("run_cycle")
        or authority.get("strict_oos_report_content_sha256")
        != strict_oos_report.get("content_sha256")
        or authority.get("strict_oos_candidate_content_sha256")
        != candidate.get("content_sha256")
        or authority.get("training_batch_content_sha256")
        != training_batch.get("content_sha256")
        or authority.get("promotion_receipt_content_sha256")
        != promotion_receipt.get("content_sha256")
        or authority.get("promotion_receipt_sha256")
        != promotion_receipt.get("receipt_sha256")
        or authority.get("formal_provider_eligible") is not True
        or authority.get("native_arrival_index_eligible") is not True
        or authority.get("formal_p0_role_eligible") is not False
        or authority.get("requires_deterministic_p0_role_activation") is not True
        or authority.get("native_ensemble_member_tables_prohibited") is not True
    ):
        raise ContractError("aifs_deterministic_authority_identity_invalid")
    boundary = authority.get("boundary")
    if (
        not isinstance(boundary, Mapping)
        or boundary.get("append_only_authority") is not True
        or boundary.get("writes_native_ensemble_tables") is not False
        or boundary.get("creates_p0_role_activation") is not False
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("places_orders") is not False
        or boundary.get("live_order_authorized") is not False
    ):
        raise ContractError("aifs_deterministic_authority_boundary_invalid")
    return candidate


__all__ = [
    "GATE_VERSION",
    "PROMOTION_RECEIPT_SCHEMA",
    "PROVIDER_AUTHORITY_SCHEMA",
    "build_aifs_deterministic_promotion_receipt",
    "issue_aifs_deterministic_provider_authority",
    "validate_aifs_deterministic_oos_report",
    "validate_aifs_deterministic_promotion_receipt",
    "validate_aifs_deterministic_provider_authority",
]
