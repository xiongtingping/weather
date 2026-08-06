"""PR07 unpublished ICON Global/EPS member facts bound to canonical PR12 truth."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from .awc_observation_contract import CanonicalAWCObservationContract
from .contracts import ContractError, content_sha256
from .icon_eps_training_facts import validate_icon_eps_training_archive
from .model_product_identity import (
    PRODUCT_BY_ID,
    REGISTRY_CONTENT_SHA256,
    REGISTRY_VERSION,
)


SCHEMA = "bayes_basket_target_pr07_icon_eps_basis_bundle_v1"
FACT_SCHEMA = "bayes_basket_target_pr07_icon_eps_historical_member_fact_v1"
TERMINAL_SCHEMA = "bayes_basket_target_pr07_icon_eps_terminal_v1"
BUILDER_ID = "pr07.icon_eps.et_historical_member.v1"
SETTLEMENT_TIMEZONE = "America/New_York"


def _utc(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"pr07_icon_eps_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"pr07_icon_eps_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _seal(value: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(value)
    return {**identity, "content_sha256": content_sha256(identity)}


def build_pr07_icon_eps_basis(
    *, bound_archive: Mapping[str, Any],
    awc_contract: Mapping[str, Any], as_of_utc: str,
) -> dict[str, Any]:
    """Build exact ET historical member facts; missing PR12 roots stay terminal."""

    cutoff = _utc(as_of_utc, "as_of_utc")
    archive, captures = validate_icon_eps_training_archive(bound_archive)
    truth = CanonicalAWCObservationContract.from_mapping(awc_contract)
    product = PRODUCT_BY_ID["DWD_ICON_GLOBAL_EPS"]
    if (
        archive.get("native_arrival_index_eligible") is not True
        or archive.get("product_id") != "DWD_ICON_GLOBAL_EPS"
        or archive.get("provider_id") != "icon_global_eps_official"
        or truth.as_of_utc > cutoff
        or product != {
            "product_id": "DWD_ICON_GLOBAL_EPS",
            "model_id": "ICON",
            "ensemble_system_id": "DWD_ICON_GLOBAL_EPS",
            "model_family": "global_ensemble",
            "provider_id": "DWD",
            "provider_product_id": "dwd_icon_global_eps",
            "independence_family_id": "DWD",
            "source_class": "native_official",
            "ingress_kind": "COVERAGE_MATRIX_ROW",
        }
    ):
        raise ContractError("pr07_icon_eps_parent_authority_invalid")
    index_pointer = archive.get("native_arrival_index")
    if not isinstance(index_pointer, Mapping):
        raise ContractError("pr07_icon_eps_native_arrival_index_missing")
    bounds_by_scope = {
        (
            item.raw["station_id"], item.raw["event_date"],
            item.raw["settlement_timezone"],
        ): item.raw
        for item in truth.day_bounds_facts
        if item.raw["closed_day"] is True and item.raw["halt_state"] == "CLEAR"
    }
    facts: list[dict[str, Any]] = []
    terminals: list[dict[str, Any]] = []
    for station, capture in sorted(captures.items()):
        common = {
            "station_id": station,
            "event_date": capture["event_date"],
            "settlement_timezone": capture["settlement_timezone"],
            "model_id": "ICON",
            "ensemble_system_id": "DWD_ICON_GLOBAL_EPS",
            "model_family": "global_ensemble",
            "provider_id": "DWD",
            "provider_product_id": "dwd_icon_global_eps",
            "independence_family_id": "DWD",
            "source_class": "native_official",
            "model_product_id": product["product_id"],
            "model_product_registry_version": REGISTRY_VERSION,
            "model_product_registry_content_sha256": REGISTRY_CONTENT_SHA256,
            "lead_bucket": "d1",
            "lead_bucket_scope": "PREVIOUS_DAY1",
            "run_init_utc": archive["run_init_utc"],
            "run_cycle": archive["run_cycle"],
            "run_cycle_scope": "OFFICIAL_RUN_CYCLE",
            "event_oos_protocol_version": "STRICT_HISTORICAL_AWC_NATIVE_ENSEMBLE_V1",
            "daily_archive_content_sha256": archive["content_sha256"],
            "daily_curve_capture_sha256": capture["content_sha256"],
            "native_arrival_index_content_sha256": index_pointer[
                "content_sha256"
            ],
            "awc_contract_content_sha256": truth.content_sha256,
        }
        if capture["settlement_timezone"] != SETTLEMENT_TIMEZONE:
            raise ContractError("pr07_icon_eps_settlement_timezone_mismatch")
        bounds = bounds_by_scope.get(
            (station, capture["event_date"], capture["settlement_timezone"])
        )
        if bounds is None:
            terminals.append(_seal({
                "schema": TERMINAL_SCHEMA,
                "builder_id": BUILDER_ID,
                "as_of_utc": cutoff,
                "state": "BLOCKED",
                "blocker": "DATA_MISSING",
                "diagnostic": "PR12_CLOSED_AWC_DAY_ROOT_MISSING",
                "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                **common,
                "consumable": False,
            }))
            continue
        forecast_available = _utc(archive["archive_query_at_utc"], "forecast_available")
        if not (
            forecast_available < bounds["day_end_utc"]
            <= bounds["canonical_committed_at_utc"] <= cutoff
        ):
            raise ContractError("pr07_icon_eps_strict_asof_order_invalid")
        members = [
            {
                "member_id": item["member_id"],
                "member_role": item["member_role"],
                "daily_max_c": item["daily_max_c"],
                "peak_valid_time_utc": item["peak_valid_time_utc"],
                "curve_sha256": item["curve_sha256"],
                "component_cohort_sha256": item["component_cohort_sha256"],
            }
            for item in capture["members"]
        ]
        facts.append(_seal({
            "schema": FACT_SCHEMA,
            "builder_id": BUILDER_ID,
            "as_of_utc": cutoff,
            "state": "MATERIALIZED_UNPUBLISHED",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            **common,
            "forecast_available_at_utc": forecast_available,
            "member_count": len(members),
            "member_daily_max_c": members,
            "member_set_sha256": capture["member_set_sha256"],
            "valid_time_vector_sha256": content_sha256(
                list(capture["valid_times_utc"])
            ),
            "truth_day_bounds_content_sha256": bounds["content_sha256"],
            "truth_observation_revision_vector_sha256": bounds[
                "observation_revision_vector_sha256"
            ],
            "truth_max_observation_fact_sha256": bounds[
                "max_observation_fact_sha256"
            ],
            "truth_coverage_receipt_sha256": bounds[
                "coverage_receipt_sha256"
            ],
            "truth_value_c": bounds["running_max_c"],
            "station_day_end_utc": bounds["day_end_utc"],
            "truth_canonical_committed_at_utc": bounds[
                "canonical_committed_at_utc"
            ],
            "consumable": False,
        }))
    blockers = Counter(item["blocker"] for item in terminals)
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": cutoff,
        "state": "BLOCKED" if terminals else "MATERIALIZED_UNPUBLISHED",
        "release_state": "BLOCKED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "expected_scope_count": len(captures),
        "fact_count": len(facts),
        "terminal_count": len(terminals),
        "blocker_counts": dict(sorted(blockers.items())),
        "facts": facts,
        "terminals": terminals,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
        "production_no_live": True,
        "consumable": False,
    }
    return _seal(identity)


__all__ = ["BUILDER_ID", "build_pr07_icon_eps_basis"]
