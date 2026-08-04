"""Restart-safe bounded rotation for forward TAF training-fact capture."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .resource_bounds import load_bounded_json, write_bounded_json_if_changed
from .settlement_time import settlement_day_state


SCHEMA = "bayes_basket_target_taf_rotation_state_v1"
MAX_CATALOG_SCOPES = 135
MAX_SELECTED_SCOPES = 4
MAX_BYTES = 1024 * 1024
# The forward TAF physical summary uses this shared local window.  It is not a
# temperature threshold; it merely prevents a post-peak bulletin from being
# scheduled as new P0-M evidence for a daily high that has already passed its
# forecast feature window.
TAF_PHYSICAL_PEAK_WINDOW_START_HOUR_LOCAL = 6
TAF_PHYSICAL_PEAK_WINDOW_END_HOUR_LOCAL = 15


def scope_key(scope: Mapping[str, Any]) -> str:
    station = str(scope.get("station_id") or "").strip().upper()
    event_date = str(scope.get("event_date") or "").strip()
    timezone_name = str(
        scope.get("settlement_timezone") or ""
    ).strip()
    if not station or not event_date or not timezone_name:
        raise ContractError("taf_rotation_scope_invalid")
    return f"{station}|{event_date}|{timezone_name}"


def build_catalog(
    rows: Sequence[Mapping[str, Any]],
    *,
    as_of_utc: str,
    station_ids: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Deduplicate active registry rows and retain only current/future days."""

    allowed = {
        str(value or "").strip().upper()
        for value in station_ids
        if str(value or "").strip()
    }
    scopes: dict[str, dict[str, str]] = {}
    for row in rows:
        scope = {
            "station_id": str(
                row.get("station_id") or ""
            ).strip().upper(),
            "event_date": str(row.get("event_date") or "").strip(),
            "settlement_timezone": str(
                row.get("settlement_timezone") or ""
            ).strip(),
        }
        if allowed and scope["station_id"] not in allowed:
            continue
        try:
            day_state = settlement_day_state(
                scope["event_date"],
                as_of_utc=as_of_utc,
                settlement_timezone=scope["settlement_timezone"],
            )["state"]
            key = scope_key(scope)
        except Exception:
            continue
        if day_state == "closed":
            continue
        if day_state == "open":
            try:
                as_of = datetime.fromisoformat(
                    str(as_of_utc).replace("Z", "+00:00")
                )
                if as_of.tzinfo is None:
                    raise ValueError("timezone required")
                local_now = as_of.astimezone(
                    ZoneInfo(scope["settlement_timezone"])
                )
                peak_end = datetime.combine(
                    local_now.date(),
                    time(TAF_PHYSICAL_PEAK_WINDOW_END_HOUR_LOCAL),
                    tzinfo=local_now.tzinfo,
                )
            except Exception:
                continue
            if local_now >= peak_end:
                # This is an intentionally silent catalog exclusion, not a
                # failed TAF receipt.  A later capture cannot establish
                # strict-as-of physical evidence for the finished window.
                continue
        scope["day_state"] = day_state
        prior = scopes.get(key)
        if prior is not None and prior != scope:
            raise ContractError("taf_rotation_scope_collision")
        scopes[key] = scope
    if len(scopes) > MAX_CATALOG_SCOPES:
        raise ContractError("taf_rotation_catalog_scope_cap_exceeded")
    # Forward training is most valuable before the settlement day starts:
    # the bulletin still spans the full local warming window and can later
    # satisfy decision < TAF arrival < truth close.  Open scopes remain in
    # bounded rotation after the nearest future catalog.
    priority = {"not_started": 0, "open": 1}
    return sorted(
        scopes.values(),
        key=lambda item: (
            priority[item["day_state"]],
            item["event_date"],
            item["station_id"],
            item["settlement_timezone"],
        ),
    )


def _without_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): child
        for key, child in value.items()
        if key != "content_sha256"
    }


def load_rotation_state(path: Path) -> dict[str, Any] | None:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ContractError("taf_rotation_state_symlink_forbidden")
    if not candidate.is_file():
        return None
    raw = load_bounded_json(candidate, limit=MAX_BYTES)
    if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
        raise ContractError("taf_rotation_state_invalid")
    result = dict(raw)
    supplied = str(result.get("content_sha256") or "")
    if supplied != content_sha256(_without_hash(result)):
        raise ContractError("taf_rotation_state_hash_mismatch")
    if (
        not isinstance(result.get("cursor"), int)
        or int(result["cursor"]) < 0
        or int(result.get("generation") or 0) < 1
        or not isinstance(result.get("catalog_scope_keys"), list)
        or len(result["catalog_scope_keys"]) > MAX_CATALOG_SCOPES
        or not isinstance(result.get("latest_selected_scope_keys"), list)
        or len(result["latest_selected_scope_keys"])
        > MAX_SELECTED_SCOPES
        or result.get("production_no_live") is not True
        or result.get("calls_order_gateway") is not False
        or result.get("places_orders") is not False
        or int(result.get("orders_submitted") or 0) != 0
    ):
        raise ContractError("taf_rotation_state_shape_invalid")
    return result


def select_scopes(
    catalog: Sequence[Mapping[str, Any]],
    *,
    previous: Mapping[str, Any] | None,
    limit: int = MAX_SELECTED_SCOPES,
) -> tuple[list[dict[str, Any]], int, str]:
    keys = [scope_key(scope) for scope in catalog]
    catalog_sha = content_sha256(keys)
    if not keys:
        return [], 0, catalog_sha
    if limit < 1 or limit > MAX_SELECTED_SCOPES:
        raise ContractError("taf_rotation_selection_limit_invalid")
    cursor = (
        int(previous.get("cursor") or 0)
        if isinstance(previous, Mapping)
        and previous.get("catalog_sha256") == catalog_sha
        else 0
    ) % len(keys)
    count = min(limit, len(keys))
    selected = [
        dict(catalog[(cursor + offset) % len(catalog)])
        for offset in range(count)
    ]
    return selected, (cursor + count) % len(keys), catalog_sha


def build_rotation_state(
    *,
    catalog: Sequence[Mapping[str, Any]],
    selected: Sequence[Mapping[str, Any]],
    next_cursor: int,
    catalog_sha256: str,
    outcomes: Sequence[Mapping[str, Any]],
    previous: Mapping[str, Any] | None,
    updated_at_utc: str,
) -> dict[str, Any]:
    selected_keys = [scope_key(scope) for scope in selected]
    if len(selected_keys) > MAX_SELECTED_SCOPES:
        raise ContractError("taf_rotation_selected_scope_cap_exceeded")
    outcome_rows = [
        {
            "scope_key": str(row.get("scope_key") or ""),
            "state": str(row.get("state") or ""),
            "reason": str(row.get("reason") or ""),
        }
        for row in outcomes
    ]
    if {row["scope_key"] for row in outcome_rows} != set(selected_keys):
        raise ContractError("taf_rotation_outcome_scope_mismatch")
    identity = {
        "schema": SCHEMA,
        "generation": int((previous or {}).get("generation") or 0) + 1,
        "updated_at_utc": str(updated_at_utc),
        "catalog_sha256": str(catalog_sha256),
        "catalog_scope_keys": [scope_key(scope) for scope in catalog],
        "catalog_scope_count": len(catalog),
        "cursor": int(next_cursor),
        "latest_selected_scope_keys": selected_keys,
        "latest_outcomes": outcome_rows,
        "production_no_live": True,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def persist_rotation_state(path: Path, value: Mapping[str, Any]) -> bool:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ContractError("taf_rotation_state_symlink_forbidden")
    return write_bounded_json_if_changed(
        candidate,
        value,
        limit=MAX_BYTES,
    )


__all__ = [
    "MAX_SELECTED_SCOPES",
    "TAF_PHYSICAL_PEAK_WINDOW_START_HOUR_LOCAL",
    "TAF_PHYSICAL_PEAK_WINDOW_END_HOUR_LOCAL",
    "SCHEMA",
    "build_catalog",
    "build_rotation_state",
    "load_rotation_state",
    "persist_rotation_state",
    "scope_key",
    "select_scopes",
]
