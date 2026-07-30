"""Frozen contract for the unique calibrated physical-X -> settlement-S bridge.

The artifact models only the observation-definition difference between AMSC X
and Wunderground S.  It never consumes forecasts, live market prices, orders,
positions, PnL, or trading outcomes.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any, Mapping, Sequence

from resolution_operational_projection_authority_runtime import (
    CONTRACT_ID as OPERATIONAL_PROJECTION_AUTHORITY_CONTRACT_ID,
)


SCHEMA_VERSION = "resolution_x_s_bridge_artifact_v1"
CANDIDATE_STATE = "shadow_candidate_ready"
PRODUCTION_STATE = "production_approved"
CALIBRATED_KERNEL_KIND = "calendar_season_shrunk_discrete_residual_pmf_v1"
OPERATIONAL_BASELINE_KERNEL_KIND = "same_station_sample_free_operational_residual_prior_v1"
AUTHORITY_CONTRACT_ID = "zuuu_amsc_wunderground_daily_high_v4"
PHYSICAL_SOURCE_ID = "amsc_zuuu_scheduled_metar_daily_max_v2"
SETTLEMENT_SOURCE_ID = "wunderground_zuuu_historical_observation_daily_high_final_v3"
FORBIDDEN_TOKENS = (
    "market_price",
    "orderbook",
    "position",
    "pnl",
    "edge",
    "expected_value",
    "trade_outcome",
)


class ResolutionBridgeArtifactError(ValueError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha256(value: Any) -> bool:
    text = _text(value).lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _forbidden_paths(value: Any, prefix: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            lowered = str(key).lower()
            if any(token in lowered for token in FORBIDDEN_TOKENS):
                hits.append(path)
            hits.extend(_forbidden_paths(item, path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            hits.extend(_forbidden_paths(item, f"{prefix}[{index}]"))
    return hits


def _pmf(value: Any, *, field: str) -> tuple[dict[int, float], list[str]]:
    raw = _mapping(value)
    output: dict[int, float] = {}
    issues: list[str] = []
    for key, probability in raw.items():
        try:
            residual = int(str(key))
        except ValueError:
            issues.append(f"{field}_residual_not_integer")
            continue
        parsed = _finite(probability)
        if parsed is None or parsed < 0.0:
            issues.append(f"{field}_probability_invalid")
            continue
        output[residual] = parsed
    if not output or abs(sum(output.values()) - 1.0) > 1e-8:
        issues.append(f"{field}_not_normalized")
    if output and (min(output) < -20 or max(output) > 20):
        issues.append(f"{field}_residual_support_out_of_contract")
    return output, issues


def artifact_sha256(value: Mapping[str, Any]) -> str:
    return _sha({key: item for key, item in _mapping(value).items() if key != "artifact_sha256"})


def validate_resolution_bridge_artifact(
    value: Mapping[str, Any],
    *,
    require_production: bool = False,
) -> dict[str, Any]:
    artifact = dict(_mapping(value))
    issues: list[str] = []
    if _text(artifact.get("schema")) != SCHEMA_VERSION:
        issues.append("bridge_artifact_schema_invalid")
    expected_state = PRODUCTION_STATE if require_production else _text(artifact.get("state"))
    if expected_state not in {CANDIDATE_STATE, PRODUCTION_STATE}:
        issues.append("bridge_artifact_state_invalid")
    if require_production and _text(artifact.get("state")) != PRODUCTION_STATE:
        issues.append("bridge_artifact_not_production_approved")
    identities = {
        "authority_contract_id": AUTHORITY_CONTRACT_ID,
        "physical_source_id": PHYSICAL_SOURCE_ID,
        "settlement_source_id": SETTLEMENT_SOURCE_ID,
        "target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
        "settlement_type": "wunderground_zuuu_historical_observation_daily_max_c",
    }
    for field, expected in identities.items():
        if _text(artifact.get(field)) != expected:
            issues.append(f"bridge_artifact_{field}_invalid")
    kernel_kind = _text(artifact.get("kernel_kind"))
    if kernel_kind not in {CALIBRATED_KERNEL_KIND, OPERATIONAL_BASELINE_KERNEL_KIND}:
        issues.append("bridge_artifact_kernel_kind_invalid")
    for field in ("authority_contract_sha256", "training_dataset_sha256"):
        if not _sha256(artifact.get(field)):
            issues.append(f"bridge_artifact_{field}_invalid")
    global_pmf, pmf_issues = _pmf(artifact.get("global_residual_pmf"), field="global_residual_pmf")
    issues.extend(pmf_issues)
    seasonal = _mapping(artifact.get("seasonal_residual_pmf"))
    allowed_seasons = {"djf", "mam", "jja", "son"}
    if any(_text(key) not in allowed_seasons for key in seasonal):
        issues.append("bridge_artifact_season_invalid")
    for season, raw in seasonal.items():
        _season_pmf, season_issues = _pmf(raw, field=f"seasonal_residual_pmf_{season}")
        issues.extend(season_issues)
    coverage = _mapping(artifact.get("coverage"))
    oos = _mapping(artifact.get("oos_evaluation"))
    if kernel_kind == CALIBRATED_KERNEL_KIND:
        if int(coverage.get("distinct_event_days") or 0) < 90:
            issues.append("bridge_artifact_event_day_coverage_below_minimum")
        if int(coverage.get("distinct_physical_x_values") or 0) < 12:
            issues.append("bridge_artifact_physical_x_support_below_minimum")
        if int(coverage.get("distinct_settlement_s_values") or 0) < 12:
            issues.append("bridge_artifact_settlement_s_support_below_minimum")
        if int(oos.get("evaluated_event_days") or 0) < 18:
            issues.append("bridge_artifact_oos_event_days_below_minimum")
        for field in ("mean_log_loss", "mean_absolute_error_c", "mean_crps"):
            if _finite(oos.get(field)) is None:
                issues.append(f"bridge_artifact_oos_{field}_invalid")
    elif kernel_kind == OPERATIONAL_BASELINE_KERNEL_KIND:
        baseline = _mapping(artifact.get("operational_baseline"))
        if (
            _text(artifact.get("operational_projection_authority_contract_id"))
            != OPERATIONAL_PROJECTION_AUTHORITY_CONTRACT_ID
            or not _sha256(
                artifact.get("operational_projection_authority_contract_sha256")
            )
        ):
            issues.append("bridge_artifact_operational_projection_authority_invalid")
        support = baseline.get("residual_support_c")
        expected_support = sorted(global_pmf)
        try:
            normalized_support = (
                sorted(int(value) for value in support)
                if isinstance(support, Sequence)
                and not isinstance(support, (str, bytes, bytearray))
                else []
            )
        except (TypeError, ValueError):
            normalized_support = []
        if (
            baseline.get("sample_or_oos_required_for_runtime") is not False
            or baseline.get("calibrated_artifact_replaces_baseline_when_approved") is not True
            or baseline.get("hard_exclusion_certificates_authorized") is not False
            or baseline.get("residual_support_is_probabilistic_not_authority_hard_envelope") is not True
            or _text(baseline.get("construction"))
            != "same_station_integer_maximum_soft_heavy_tail_prior_v2"
            or normalized_support != expected_support
            or expected_support != list(range(-20, 21))
            or baseline.get("soft_tail_mass_present") is not True
            or baseline.get("soft_projection_exact_zero_forbidden_without_hard_certificate") is not True
        ):
            issues.append("bridge_artifact_operational_baseline_contract_invalid")
        if seasonal:
            issues.append("bridge_artifact_operational_baseline_must_not_claim_seasonal_calibration")
        if _text(oos.get("state")) != "not_required_for_operational_baseline":
            issues.append("bridge_artifact_operational_baseline_oos_state_invalid")
    if _forbidden_paths(artifact):
        issues.append("bridge_artifact_contains_forbidden_trading_input")
    supplied_sha = _text(artifact.get("artifact_sha256"))
    if not _sha256(supplied_sha) or supplied_sha != artifact_sha256(artifact):
        issues.append("bridge_artifact_sha256_invalid")
    if require_production:
        if artifact.get("bridge_closed") is not True or artifact.get("validated") is not True:
            issues.append("bridge_artifact_production_closure_invalid")
        if (
            artifact.get("production_runtime_eligible") is not True
            or artifact.get("shadow_only") is not False
        ):
            issues.append("bridge_artifact_production_runtime_boundary_invalid")
        if not _text(artifact.get("approval_id")) or not _sha256(artifact.get("approval_receipt_sha256")):
            issues.append("bridge_artifact_approval_lineage_missing")
    else:
        if _text(artifact.get("state")) == CANDIDATE_STATE and (
            artifact.get("bridge_closed") is not False or artifact.get("validated") is not False
        ):
            issues.append("bridge_artifact_candidate_must_remain_open")
    if issues:
        raise ResolutionBridgeArtifactError(
            "resolution_bridge_artifact_invalid:" + ",".join(sorted(set(issues)))
        )
    artifact["global_residual_pmf"] = {str(key): value for key, value in sorted(global_pmf.items())}
    return artifact


def calendar_season(event_date: str) -> str:
    month = date.fromisoformat(_text(event_date)[:10]).month
    if month in (12, 1, 2):
        return "djf"
    if month in (3, 4, 5):
        return "mam"
    if month in (6, 7, 8):
        return "jja"
    return "son"


def residual_pmf_for_date(artifact: Mapping[str, Any], *, event_date: str) -> dict[int, float]:
    validated = validate_resolution_bridge_artifact(
        artifact,
        require_production=_text(artifact.get("state")) == PRODUCTION_STATE,
    )
    season = calendar_season(event_date)
    selected = _mapping(_mapping(validated.get("seasonal_residual_pmf")).get(season))
    raw = selected or _mapping(validated.get("global_residual_pmf"))
    return {int(key): float(value) for key, value in raw.items()}


def materialize_closed_resolution_contract(
    *,
    target_contract: Mapping[str, Any],
    bridge_artifact: Mapping[str, Any],
    physical_support_c: Sequence[float],
) -> dict[str, Any]:
    """Fuse one approved bridge artifact with one immutable market rule."""

    artifact = validate_resolution_bridge_artifact(bridge_artifact, require_production=True)
    target = dict(_mapping(target_contract))
    issues: list[str] = []
    if _text(target.get("schema")) != "market_resolution_target_contract_v1":
        issues.append("bridge_target_contract_schema_invalid")
    if _text(target.get("authority_contract_id")) != AUTHORITY_CONTRACT_ID:
        issues.append("bridge_target_authority_contract_invalid")
    if _text(target.get("authority_contract_sha256")) != _text(artifact.get("authority_contract_sha256")):
        issues.append("bridge_target_authority_sha_mismatch")
    if _text(target.get("resolution_source_id")) != SETTLEMENT_SOURCE_ID:
        issues.append("bridge_target_settlement_source_invalid")
    if target.get("bridge_closed") is not False or target.get("validated") is not False:
        issues.append("bridge_target_contract_must_be_raw_open_contract")
    event_date = _text(target.get("resolution_local_date"))[:10]
    try:
        pmf = residual_pmf_for_date(artifact, event_date=event_date)
    except (ValueError, ResolutionBridgeArtifactError):
        pmf = {}
        issues.append("bridge_artifact_residual_pmf_unavailable")
    support: list[int] = []
    for value in physical_support_c:
        parsed = _finite(value)
        if parsed is None or abs(parsed - round(parsed)) > 1e-8:
            issues.append("bridge_physical_support_not_integer_reported_x")
        else:
            support.append(int(round(parsed)))
    if not support:
        issues.append("bridge_physical_support_missing")
    if issues:
        raise ResolutionBridgeArtifactError(
            "closed_resolution_contract_invalid:" + ",".join(sorted(set(issues)))
        )
    kernel = {
        str(x): [
            {"settlement_value_c": float(x + residual), "probability": probability}
            for residual, probability in sorted(pmf.items())
        ]
        for x in sorted(set(support))
    }
    core = {
        "resolution_source_id": SETTLEMENT_SOURCE_ID,
        "resolution_local_date": event_date,
        "settlement_unit": "C",
        "physical_to_settlement_kernel": kernel,
        "bucket_contract": dict(_mapping(target.get("bucket_contract"))),
        "bridge_closed": True,
        "validated": True,
        "bridge_artifact_sha256": _text(artifact.get("artifact_sha256")),
        "target_contract_sha256": _text(target.get("target_contract_sha256")),
        "authority_contract_id": AUTHORITY_CONTRACT_ID,
        "authority_contract_sha256": _text(artifact.get("authority_contract_sha256")),
        "rounding_rule": _text(target.get("rounding_rule")),
        # A fitted or operational PMF support is not a proof that residual
        # tails are impossible.  Hard NO certificates require a separately
        # approved authority envelope, which no current bridge artifact has.
        "hard_exclusion_certificates_authorized": False,
        "hard_exclusion_certificate_na_reason": "authority_hard_residual_envelope_not_approved",
    }
    return {**core, "resolution_contract_id": f"xsb_{_sha(core)[:24]}"}


__all__ = [
    "AUTHORITY_CONTRACT_ID",
    "CANDIDATE_STATE",
    "CALIBRATED_KERNEL_KIND",
    "PHYSICAL_SOURCE_ID",
    "OPERATIONAL_BASELINE_KERNEL_KIND",
    "OPERATIONAL_PROJECTION_AUTHORITY_CONTRACT_ID",
    "PRODUCTION_STATE",
    "ResolutionBridgeArtifactError",
    "SCHEMA_VERSION",
    "SETTLEMENT_SOURCE_ID",
    "artifact_sha256",
    "calendar_season",
    "materialize_closed_resolution_contract",
    "residual_pmf_for_date",
    "validate_resolution_bridge_artifact",
]
