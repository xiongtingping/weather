"""Bind complete native daily-max manifests to exact canonical AWC roots."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any, Mapping, Sequence

from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .contracts import ContractError, content_sha256
from .native_daily_max_manifest import validate_native_daily_max_manifest
from .pr12_authority_scope_projection import validate_pr12_authority_scope_projection
from .truth_authority_supersession import resolve_effective_truth_day_root


SCHEMA = "bayes_basket_target_native_daily_max_truth_binding_bundle_v1"
BINDING_SCHEMA = "bayes_basket_target_native_daily_max_truth_binding_v1"
TERMINAL_SCHEMA = "bayes_basket_target_native_daily_max_truth_terminal_v1"
BUILDER_ID = "pr10.native_daily_max_truth_binding.v1"


def _utc(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"native_daily_max_truth_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"native_daily_max_truth_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _seal(value: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(value)
    return {**identity, "content_sha256": content_sha256(identity)}


def build_native_daily_max_truth_binding_bundle(
    conn: sqlite3.Connection, *, manifests: Sequence[Mapping[str, Any]],
    authority_scope_projection: Mapping[str, Any], as_of_utc: str,
) -> dict[str, Any]:
    inspection = inspect_canonical_ssot_schema(conn)
    if inspection.get("state") != "V15" or inspection.get("errors"):
        raise ContractError("native_daily_max_truth_v15_required")
    cutoff = _utc(as_of_utc, "as_of_utc")
    projection = validate_pr12_authority_scope_projection(authority_scope_projection)
    parsed = [validate_native_daily_max_manifest(item) for item in manifests]
    if len({item["content_sha256"] for item in parsed}) != len(parsed):
        raise ContractError("native_daily_max_truth_manifest_duplicate")
    bindings: list[dict[str, Any]] = []
    terminals: list[dict[str, Any]] = []
    for manifest in sorted(parsed, key=lambda item: item["content_sha256"]):
        common = {
            "station_id": manifest["station_id"],
            "event_date": manifest["event_date"],
            "settlement_timezone": manifest["settlement_timezone"],
            "native_daily_max_manifest_content_sha256": manifest["content_sha256"],
            "model_product_identity_content_sha256": manifest[
                "model_product_identity_content_sha256"
            ],
            "authority_scope_projection_content_sha256": projection[
                "content_sha256"
            ],
            "model_id": manifest["model_id"],
            "provider_id": manifest["provider_id"],
            "provider_product_id": manifest["provider_product_id"],
            "lead_bucket": manifest["lead_bucket"],
            "run_cycle": manifest["run_cycle"],
        }
        try:
            bounds = resolve_effective_truth_day_root(
                conn,
                station_id=manifest["station_id"],
                event_date=manifest["event_date"],
                settlement_timezone=manifest["settlement_timezone"],
                authority_scope_projection_content_sha256=projection[
                    "content_sha256"
                ],
                as_of_utc=cutoff,
            )
        except ContractError as exc:
            if str(exc) != "TRUTH_AUTHORITY_SUPERSESSION_REQUIRED":
                raise
            terminals.append(_seal({
                "schema": TERMINAL_SCHEMA,
                "builder_id": BUILDER_ID,
                "as_of_utc": cutoff,
                "state": "BLOCKED",
                "blocker": "LINEAGE_MISMATCH",
                "diagnostic": "TRUTH_AUTHORITY_SUPERSESSION_REQUIRED",
                "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                **common,
                "writes_database": False,
                "publishes_pr04_pointer": False,
                "starts_services": False,
                "releases_live_pause": False,
                "orders_submitted": 0,
                "consumable": False,
            }))
            continue
        if (
            bounds["closed_day"] is not True
            or bounds["halt_state"] != "CLEAR"
            or bounds["canonical_committed_at_utc"] > cutoff
            or not manifest["forecast_decision_at_utc"] < bounds["day_end_utc"]
            <= bounds["canonical_committed_at_utc"]
        ):
            raise ContractError("native_daily_max_truth_root_not_strict_oos")
        bindings.append(_seal({
            "schema": BINDING_SCHEMA,
            "builder_id": BUILDER_ID,
            "as_of_utc": cutoff,
            "state": "MATERIALIZED_UNPUBLISHED",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            **common,
            "truth_day_bounds_content_sha256": bounds["content_sha256"],
            "truth_observation_revision_vector_sha256": bounds[
                "observation_revision_vector_sha256"
            ],
            "truth_max_observation_fact_sha256": bounds[
                "max_observation_fact_sha256"
            ],
            "truth_coverage_receipt_sha256": bounds["coverage_receipt_sha256"],
            "truth_value_c": bounds["running_max_c"],
            "consumable": False,
        }))
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": cutoff,
        "state": "BLOCKED" if terminals else "MATERIALIZED_UNPUBLISHED",
        "release_state": "BLOCKED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "scope_count": len(parsed),
        "binding_count": len(bindings),
        "terminal_count": len(terminals),
        "blocker_counts": (
            {"LINEAGE_MISMATCH": len(terminals)} if terminals else {}
        ),
        "bindings": bindings,
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


__all__ = ["BUILDER_ID", "build_native_daily_max_truth_binding_bundle"]
