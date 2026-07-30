"""Per-scope exact readiness resolver for the target production-no-live lane.

Counts are observability only.  This resolver reuses the production compiler's
strict readers so readiness and runtime cannot disagree about which
probability, quote, inventory, order-state, station policy, cash authority, or
strict-delta artifact is authoritative for one station/day decision.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .market_registry import load_started_registry_rows
from .production_io import compile_production_cycle_from_ssot


SCHEMA = "bayes_basket_target_exact_scope_readiness_v1"
SCOPE_SCHEMA = "bayes_basket_target_exact_scope_readiness_item_v1"
MAX_SCOPE_COUNT = 256
REQUIRED_POLICY_SCOPE_KEYS = (
    "season_bin",
    "maturity_bin",
    "liquidity_bin",
    "strategy_version",
)
EXACTLY_ONE = "EXACTLY_ONE"


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return value


def _error_status(reason: str) -> str:
    value = reason.lower()
    if "ambiguous" in value or "not_unique" in value:
        return "AMBIGUOUS"
    if "expired" in value or "stale" in value or "not_active" in value:
        return "EXPIRED"
    if "future" in value or "not_started" in value:
        return "FUTURE"
    if "authority" in value or "promotion" in value:
        return "AUTHORITY_MISMATCH"
    if any(word in value for word in ("sha", "digest", "drift")):
        return "DIGEST_MISMATCH"
    if "lineage" in value:
        return "LINEAGE_MISMATCH"
    if any(word in value for word in ("scope", "timezone", "station", "event_date")):
        return "SCOPE_MISMATCH"
    if "missing" in value or "incomplete" in value:
        return "MISSING"
    return "INVALID"


def _registry_scope_sha(rows: Sequence[Mapping[str, Any]]) -> str:
    normalized = sorted(
        (
            {
                key: row.get(key)
                for key in (
                    "snapshot_sha256",
                    "slug",
                    "event_date",
                    "outcome",
                    "station_id",
                    "settlement_timezone",
                    "yes_token",
                    "no_token",
                    "rules_sha256",
                    "market_active",
                    "market_accepting_orders",
                )
            }
            for row in rows
        ),
        key=lambda row: (
            str(row.get("outcome") or ""),
            str(row.get("yes_token") or ""),
        ),
    )
    return content_sha256(
        {
            "schema": "bayes_basket_target_registry_scope_v1",
            "rows": normalized,
        }
    )


def _resolved_item(
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    registry_rows: Sequence[Mapping[str, Any]],
    compiled: Mapping[str, Any],
    policy_scope: Mapping[str, str],
    authority_scope: str,
    decision_at_utc: str,
) -> dict[str, Any]:
    envelope = _mapping(
        "readiness_probability_envelope",
        compiled.get("probability_envelope"),
    )
    market = _mapping(
        "readiness_market_snapshot",
        compiled.get("production_market_snapshot"),
    )
    plan = _mapping("readiness_plan", compiled.get("plan"))
    lineage = _mapping("readiness_plan_lineage", plan.get("lineage"))
    probability_lineage = _mapping(
        "readiness_probability_lineage", envelope.get("lineage")
    )
    production_inputs = _mapping(
        "readiness_production_inputs",
        compiled.get("production_inputs"),
    )
    stage = _text("readiness_probability_stage", envelope.get("stage"))
    if stage != "P1":
        raise ContractError("readiness_scope_p1_required")
    if (
        _text("readiness_probability_station_id", envelope.get("station_id"))
        != station_id
        or _text("readiness_probability_event_date", envelope.get("event_date"))
        != event_date
        or _text(
            "readiness_probability_settlement_timezone",
            envelope.get("settlement_timezone"),
        )
        != settlement_timezone
    ):
        raise ContractError("readiness_probability_scope_mismatch")
    if (
        _text("readiness_market_station_id", market.get("station_id"))
        != station_id
        or _text("readiness_market_event_date", market.get("event_date"))
        != event_date
        or _text(
            "readiness_market_settlement_timezone",
            market.get("settlement_timezone"),
        )
        != settlement_timezone
    ):
        raise ContractError("readiness_market_scope_mismatch")
    if compiled.get("status") != "ready_no_live":
        reasons = ",".join(
            str(value) for value in (compiled.get("reason_codes") or [])
        )
        raise ContractError(
            "readiness_compiled_plan_not_ready:" + (reasons or "unknown")
        )
    cash_manifest = _mapping(
        "readiness_cash_phase_authority_manifest",
        production_inputs.get("cash_phase_authority_manifest"),
    )
    resolved_station_policy = _mapping(
        "readiness_resolved_station_policy",
        compiled.get("resolved_station_policy"),
    )
    vector = {
        "market_registry_sha256": _registry_scope_sha(registry_rows),
        "probability_envelope_id": _text(
            "readiness_probability_envelope_id",
            envelope.get("checkpoint_id"),
        ),
        "probability_sha256": _text(
            "readiness_probability_sha256",
            envelope.get("content_sha256"),
        ),
        "posterior_sha256": _text(
            "readiness_posterior_sha256",
            envelope.get("posterior_sha256"),
        ),
        "bridge_sha256": _text(
            "readiness_bridge_sha256",
            envelope.get("bridge_sha256"),
        ),
        "probability_lineage_sha256": content_sha256(
            {
                "schema": "bayes_basket_target_probability_lineage_v1",
                **dict(probability_lineage),
            }
        ),
        "probability_bridge_history_id": _text(
            "readiness_probability_bridge_history_id",
            production_inputs.get("probability_bridge_history_id"),
        ),
        "probability_bridge_projection_sha256": _text(
            "readiness_probability_bridge_projection_sha256",
            production_inputs.get(
                "probability_bridge_projection_sha256"
            ),
        ),
        "official_ws_snapshot_id": _text(
            "readiness_ws_snapshot_id", market.get("snapshot_id")
        ),
        "official_ws_quote_sha256": _text(
            "readiness_official_ws_quote_sha256",
            market.get("official_ws_quote_sha256"),
        ),
        "quote_freshness_policy_sha256": _text(
            "readiness_quote_freshness_policy_sha256",
            market.get("quote_freshness_policy_sha256"),
        ),
        "inventory_sha256": _text(
            "readiness_inventory_sha256", market.get("inventory_sha256")
        ),
        "order_state_sha256": _text(
            "readiness_order_state_sha256", market.get("order_state_sha256")
        ),
        "station_policy_sha256": _text(
            "readiness_station_policy_sha256",
            lineage.get("policy_resolution_sha256"),
        ),
        "station_policy_resolution_sha256": content_sha256(
            {
                "schema": "bayes_basket_target_station_policy_resolution_v1",
                **dict(resolved_station_policy),
            }
        ),
        "cash_policy_sha256": _text(
            "readiness_cash_policy_sha256",
            production_inputs.get("cash_phase_policy_sha256"),
        ),
        "cash_authority_manifest_sha256": _text(
            "readiness_cash_authority_manifest_sha256",
            cash_manifest.get("manifest_sha256"),
        ),
        "strict_delta_artifact_sha256": _text(
            "readiness_strict_delta_artifact_sha256",
            production_inputs.get("strict_delta_artifact_sha256"),
        ),
        "quote_observed_at": _text(
            "readiness_quote_observed_at", market.get("quote_observed_at")
        ),
        "quote_expires_at": _text(
            "readiness_quote_expires_at", market.get("quote_expires_at")
        ),
    }
    item_identity = {
        "schema": SCOPE_SCHEMA,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "stage": stage,
        **dict(policy_scope),
        "authority_scope": authority_scope,
        "decision_at_utc": decision_at_utc,
        "status": EXACTLY_ONE,
        "lineage_vector": vector,
        "lineage_sha256": content_sha256(
            {
                "schema": "bayes_basket_target_readiness_lineage_vector_v1",
                **vector,
            }
        ),
    }
    return {
        **item_identity,
        "content_sha256": content_sha256(item_identity),
    }


def resolve_target_readiness_scopes(
    conn: sqlite3.Connection,
    *,
    decision_at_utc: str,
    target_scope: Mapping[str, Any],
    authority_scope: str,
    station_ids: Sequence[str] = (),
    max_scope_count: int = MAX_SCOPE_COUNT,
) -> dict[str, Any]:
    """Resolve every active registry scope with the production compiler."""

    decision_at = _timestamp(
        "readiness_decision_at_utc", decision_at_utc
    )
    if not 1 <= int(max_scope_count) <= MAX_SCOPE_COUNT:
        raise ContractError("readiness_scope_limit_invalid")
    raw_policy_scope = _mapping("readiness_target_scope", target_scope)
    policy_scope = {
        key: _text(f"readiness_{key}", raw_policy_scope.get(key))
        for key in REQUIRED_POLICY_SCOPE_KEYS
    }
    required_authority = _text(
        "readiness_authority_scope", authority_scope
    ).upper()
    allowlist = {
        _text("readiness_station_id", value).upper()
        for value in station_ids
    }
    registry_rows = load_started_registry_rows(
        conn,
        as_of_utc=decision_at,
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in registry_rows:
        station_id = _text(
            "readiness_registry_station_id", raw.get("station_id")
        ).upper()
        if allowlist and station_id not in allowlist:
            continue
        event_date = _text(
            "readiness_registry_event_date", raw.get("event_date")
        )
        grouped.setdefault((station_id, event_date), []).append(dict(raw))
    if len(grouped) > int(max_scope_count):
        raise ContractError("readiness_scope_limit_exceeded")

    items: list[dict[str, Any]] = []
    for (station_id, event_date), rows in sorted(grouped.items()):
        timezones = {
            str(row.get("settlement_timezone") or "").strip()
            for row in rows
        }
        if len(timezones) != 1 or not next(iter(timezones), ""):
            reason = "readiness_registry_timezone_ambiguous"
            item = {
                "schema": SCOPE_SCHEMA,
                "station_id": station_id,
                "event_date": event_date,
                "settlement_timezone": None,
                "stage": "P1",
                **policy_scope,
                "authority_scope": required_authority,
                "decision_at_utc": decision_at,
                "status": "AMBIGUOUS",
                "reason_codes": [reason],
            }
            item["content_sha256"] = content_sha256(item)
            items.append(item)
            continue
        settlement_timezone = next(iter(timezones))
        try:
            compiled = compile_production_cycle_from_ssot(
                conn,
                station_id=station_id,
                event_date=event_date,
                evaluation_at_utc=decision_at,
                season_bin=policy_scope["season_bin"],
                maturity_bin=policy_scope["maturity_bin"],
                liquidity_bin=policy_scope["liquidity_bin"],
                strategy_version=policy_scope["strategy_version"],
                policy_authority_scope=required_authority,
            )
            item = _resolved_item(
                station_id=station_id,
                event_date=event_date,
                settlement_timezone=settlement_timezone,
                registry_rows=rows,
                compiled=compiled,
                policy_scope=policy_scope,
                authority_scope=required_authority,
                decision_at_utc=decision_at,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}:{exc}"
            item = {
                "schema": SCOPE_SCHEMA,
                "station_id": station_id,
                "event_date": event_date,
                "settlement_timezone": settlement_timezone,
                "stage": "P1",
                **policy_scope,
                "authority_scope": required_authority,
                "decision_at_utc": decision_at,
                "status": _error_status(reason),
                "reason_codes": [reason],
            }
            item["content_sha256"] = content_sha256(item)
        items.append(item)

    exact_count = sum(item["status"] == EXACTLY_ONE for item in items)
    identity = {
        "schema": SCHEMA,
        "decision_at_utc": decision_at,
        "target_scope": policy_scope,
        "authority_scope": required_authority,
        "station_ids": sorted(allowlist),
        "scope_count": len(items),
        "exact_scope_count": exact_count,
        "blocked_scope_count": len(items) - exact_count,
        "ready": bool(items) and exact_count == len(items),
        "items": items,
        "boundary": {
            "execution_mode": "production_no_live",
            "read_only": True,
            "places_orders": False,
            "orders_submitted": 0,
            "calls_order_gateway": False,
        },
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


__all__ = [
    "EXACTLY_ONE",
    "MAX_SCOPE_COUNT",
    "SCHEMA",
    "SCOPE_SCHEMA",
    "resolve_target_readiness_scopes",
]
