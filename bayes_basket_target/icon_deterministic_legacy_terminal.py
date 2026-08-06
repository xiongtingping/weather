"""Bind legacy ICON republisher OOS claims to canonical blocked terminals.

The v1 report was built from retrospectively reconstructed daily curves and
uses the reconstruction time as the holdout decision time.  It therefore
cannot be converted into V13 strict-as-of events, even where the legacy
report says PASS.  This module preserves those claims as evidence only.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .model_product_identity import PRODUCT_BY_ID, REGISTRY_CONTENT_SHA256
from .resource_bounds import load_bounded_json


SCHEMA = "bayes_basket_target_pr05_pr09_icon_legacy_terminal_bundle_v1"
TERMINAL_SCHEMA = "bayes_basket_target_icon_legacy_scope_terminal_v1"
REPORT_SCHEMA = "deterministic_republisher_strict_oos_v1"
BINDING_SCHEMA = "bayes_basket_target_republisher_no_live_fallback_binding_v1"
COVERAGE_SCHEMA = "bayes_basket_target_pr05_pr09_v13_model_oos_scope_bundle_v1"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
BUILDER_ID = "pr05-pr09.icon_legacy_oos_terminal.v1"


class ICONLegacyTerminalError(ContractError):
    """Legacy ICON evidence cannot be bound without semantic guessing."""


def _verified(raw: Mapping[str, Any], label: str) -> str:
    supplied = str(raw.get("content_sha256") or "").lower()
    if supplied != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise ICONLegacyTerminalError(f"icon_legacy_{label}_hash_mismatch")
    return supplied


def _load(path: Path | str, label: str) -> dict[str, Any]:
    value = load_bounded_json(Path(path).expanduser().resolve(strict=True))
    if not isinstance(value, Mapping):
        raise ICONLegacyTerminalError(f"icon_legacy_{label}_mapping_required")
    return dict(value)


def _post_event_decision(prediction: Mapping[str, Any]) -> bool:
    try:
        event_end = datetime.combine(
            date.fromisoformat(str(prediction["event_date"])),
            time.max,
            tzinfo=timezone.utc,
        )
        decision = datetime.fromisoformat(
            str(prediction["decision_at_utc"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise ICONLegacyTerminalError("icon_legacy_prediction_time_invalid") from exc
    return decision > event_end


def build_icon_legacy_terminal_bundle(
    *, as_of_utc: str, report_path: Path | str, binding_path: Path | str,
    coverage_scope_path: Path | str,
) -> dict[str, Any]:
    report = _load(report_path, "report")
    binding = _load(binding_path, "binding")
    coverage = _load(coverage_scope_path, "coverage")
    report_sha = _verified(report, "report")
    binding_sha = _verified(binding, "binding")
    coverage_sha = _verified(coverage, "coverage")
    if (
        report.get("schema") != REPORT_SCHEMA
        or report.get("model_id") != "ICON"
        or report.get("provider_id") != "icon_global_open_meteo"
        or report.get("source_role") != "aggregator_republisher"
        or report.get("formal_p0_eligible") is not False
        or binding.get("schema") != BINDING_SCHEMA
        or binding.get("formal_live_authority_written") is not False
        or binding.get("p1_authority_written") is not False
        or coverage.get("schema") != COVERAGE_SCHEMA
        or coverage.get("canonical_product_registry_content_sha256")
        != REGISTRY_CONTENT_SHA256
    ):
        raise ICONLegacyTerminalError("icon_legacy_parent_semantics_invalid")

    expected = {
        str(row["station_id"]): dict(row)
        for row in coverage.get("v13_expected_scopes") or []
        if row.get("product_id") == "ICON" and row.get("lead_bucket") == "d0"
    }
    product = PRODUCT_BY_ID["ICON"]
    if len(expected) != 45 or any(
        row.get("provider_id") != product["provider_id"]
        or row.get("provider_product_id") != product["provider_product_id"]
        for row in expected.values()
    ):
        raise ICONLegacyTerminalError("icon_legacy_expected_scope_invalid")

    icon_bindings: dict[str, dict[str, Any]] = {}
    for item in binding.get("bindings") or []:
        if item.get("model_id") != "ICON":
            continue
        station = str(item.get("station_id") or "")
        if station in icon_bindings:
            raise ICONLegacyTerminalError("icon_legacy_binding_duplicate")
        if (
            item.get("activation_state") != "BOUND_NO_LIVE_FALLBACK"
            or item.get("production_no_live_only") is not True
            or item.get("p1_eligible") is not False
            or item.get("native_arrival_index_eligible") is not False
        ):
            raise ICONLegacyTerminalError("icon_legacy_binding_semantics_invalid")
        icon_bindings[station] = dict(item)

    terminals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in report.get("candidates") or []:
        candidate_sha = _verified(candidate, "candidate")
        station = str(candidate.get("station_id") or "")
        if station in seen or station not in expected:
            raise ICONLegacyTerminalError("icon_legacy_candidate_scope_invalid")
        seen.add(station)
        predictions = candidate.get("predictions")
        if (
            candidate.get("model_id") != "ICON"
            or candidate.get("provider_id") != product["provider_id"]
            or candidate.get("lead_bucket") != "d0"
            or candidate.get("source_role") != "aggregator_republisher"
            or candidate.get("run_cycle") != "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
            or candidate.get("p1_eligible") is not False
            or not isinstance(predictions, list)
            or len(predictions) != 8
            or not all(_post_event_decision(item) for item in predictions)
        ):
            raise ICONLegacyTerminalError("icon_legacy_candidate_semantics_invalid")
        legacy_state = str(candidate.get("state") or "")
        bound = icon_bindings.get(station)
        if legacy_state == "PRODUCTION_NO_LIVE_OOS_PASS":
            if bound is None or bound.get("candidate_content_sha256") != candidate_sha:
                raise ICONLegacyTerminalError("icon_legacy_pass_binding_missing")
        elif legacy_state == "OOS_REJECTED":
            if bound is not None:
                raise ICONLegacyTerminalError("icon_legacy_rejected_bound")
        else:
            raise ICONLegacyTerminalError("icon_legacy_candidate_state_invalid")

        scope = expected[station]
        identity = {
            "schema": TERMINAL_SCHEMA,
            **{
                field: scope[field]
                for field in (
                    "station_id", "model_id", "ensemble_system_id", "model_family",
                    "provider_id", "provider_product_id", "independence_family_id",
                    "source_class", "authority_scope", "settlement_timezone",
                    "lead_bucket", "lead_bucket_scope", "run_cycle",
                    "run_cycle_scope", "event_oos_protocol_version",
                )
            },
            "product_id": "ICON",
            "model_product_identity_content_sha256": scope[
                "model_product_identity_content_sha256"
            ],
            "legacy_candidate_content_sha256": candidate_sha,
            "legacy_binding_id": None if bound is None else bound["binding_id"],
            "legacy_claimed_state": legacy_state,
            "legacy_holdout_decision_after_event_count": len(predictions),
            "blocker": "LINEAGE_MISMATCH",
            "diagnostic": (
                "legacy_retroactive_reconstruction_time_is_not_strict_historical_"
                "forecast_decision_or_availability"
            ),
            "state": "BLOCKED",
            "release_state": "BLOCKED",
            "consumable": False,
        }
        terminals.append({**identity, "content_sha256": content_sha256(identity)})

    if len(terminals) != report.get("candidate_count") or len(icon_bindings) != 43:
        raise ICONLegacyTerminalError("icon_legacy_count_mismatch")
    terminals.sort(key=lambda item: item["station_id"])
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": as_of_utc,
        "state": "MATERIALIZED_UNPUBLISHED",
        "release_state": "BLOCKED",
        "authority_scope": AUTHORITY_SCOPE,
        "report_content_sha256": report_sha,
        "binding_content_sha256": binding_sha,
        "coverage_scope_content_sha256": coverage_sha,
        "terminal_count": len(terminals),
        "legacy_pass_count": sum(
            item["legacy_claimed_state"] == "PRODUCTION_NO_LIVE_OOS_PASS"
            for item in terminals
        ),
        "legacy_rejected_count": sum(
            item["legacy_claimed_state"] == "OOS_REJECTED" for item in terminals
        ),
        "terminals": terminals,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "future_data_counts_as_oos": False,
        "consumable": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = ["ICONLegacyTerminalError", "SCHEMA", "build_icon_legacy_terminal_bundle"]
