"""Bounded restart-safe state for canonical NWP scope rotation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .resource_bounds import load_bounded_json, write_bounded_json_if_changed


SCHEMA = "bayes_basket_target_nwp_rotation_state_v1"
MAX_CATALOG_SCOPES = 135
MAX_SELECTED_SCOPES = 4
MAX_BYTES = 1024 * 1024


def scope_key(scope: Mapping[str, Any]) -> str:
    stations = [
        str(value or "").strip().upper()
        for value in scope.get("station_ids") or []
        if str(value or "").strip()
    ]
    event_date = str(scope.get("event_date") or "").strip()
    timezone_name = str(scope.get("settlement_timezone") or "").strip()
    if len(stations) != 1 or not event_date or not timezone_name:
        raise ContractError("nwp_rotation_scope_invalid")
    return f"{stations[0]}|{event_date}|{timezone_name}"


def _without_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): child
        for key, child in value.items()
        if key != "content_sha256"
    }


def validate_rotation_state(
    value: Mapping[str, Any],
    *,
    station_ids: Sequence[str],
) -> None:
    if value.get("schema") != SCHEMA:
        raise ContractError("nwp_rotation_schema_invalid")
    supplied = str(value.get("content_sha256") or "")
    if len(supplied) != 64 or supplied != content_sha256(
        _without_hash(value)
    ):
        raise ContractError("nwp_rotation_content_sha256_mismatch")
    expected_stations = tuple(sorted(
        str(item or "").strip().upper()
        for item in station_ids
        if str(item or "").strip()
    ))
    actual_stations = tuple(
        (value.get("station_scope") or {}).get("station_ids") or ()
    )
    if (
        (value.get("station_scope") or {}).get("mode")
        != "canonical_allowlist"
        or actual_stations != expected_stations
    ):
        raise ContractError("nwp_rotation_station_scope_mismatch")
    catalog = value.get("catalog_scope_keys")
    attempted = value.get("attempted_scope_keys")
    selected = value.get("latest_selected_scope_keys")
    cursors = value.get("cursor_by_priority")
    if (
        not isinstance(catalog, list)
        or len(catalog) > MAX_CATALOG_SCOPES
        or catalog != sorted(set(catalog))
        or not isinstance(attempted, list)
        or attempted != sorted(set(attempted))
        or not set(attempted).issubset(catalog)
        or not isinstance(selected, list)
        or len(selected) > MAX_SELECTED_SCOPES
        or not set(selected).issubset(catalog)
        or not isinstance(cursors, Mapping)
        or set(cursors) - {"0", "1", "2"}
        or any(
            not isinstance(value, int) or value < 0
            for value in cursors.values()
        )
    ):
        raise ContractError("nwp_rotation_state_shape_invalid")
    if int(value.get("generation") or 0) < 1:
        raise ContractError("nwp_rotation_generation_invalid")
    if value.get("complete_rotation") is not (
        bool(catalog) and set(attempted) == set(catalog)
    ):
        raise ContractError("nwp_rotation_completion_invalid")
    if (
        value.get("production_no_live") is not True
        or value.get("calls_order_gateway") is not False
        or value.get("places_orders") is not False
        or value.get("live_submit_authorized") is not False
        or int(value.get("orders_submitted") or 0) != 0
    ):
        raise ContractError("nwp_rotation_boundary_invalid")


def load_rotation_state(
    path: Path,
    *,
    station_ids: Sequence[str],
) -> dict[str, Any] | None:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ContractError("nwp_rotation_state_symlink_forbidden")
    if not candidate.is_file():
        return None
    raw = load_bounded_json(candidate, limit=MAX_BYTES)
    if not isinstance(raw, Mapping):
        raise ContractError("nwp_rotation_state_mapping_required")
    result = dict(raw)
    validate_rotation_state(result, station_ids=station_ids)
    return result


def build_rotation_state(
    *,
    station_ids: Sequence[str],
    scopes: Sequence[Mapping[str, Any]],
    selected_scopes: Sequence[Mapping[str, Any]],
    cursor_by_priority: Mapping[int, int],
    outcomes: Sequence[Mapping[str, Any]],
    previous: Mapping[str, Any] | None,
    updated_at_utc: str,
) -> dict[str, Any]:
    catalog = sorted({scope_key(scope) for scope in scopes})
    if not catalog or len(catalog) > MAX_CATALOG_SCOPES:
        raise ContractError("nwp_rotation_catalog_scope_invalid")
    selected = [scope_key(scope) for scope in selected_scopes]
    if len(selected) > MAX_SELECTED_SCOPES or len(set(selected)) != len(
        selected
    ):
        raise ContractError("nwp_rotation_selected_scope_invalid")
    catalog_sha = content_sha256(catalog)
    previous_valid = (
        previous
        if (
            isinstance(previous, Mapping)
            and previous.get("catalog_sha256") == catalog_sha
        )
        else None
    )
    attempted = set(
        (previous_valid or {}).get("attempted_scope_keys") or []
    )
    attempted.update(selected)
    latest_outcomes = []
    for row in outcomes:
        key = str(row.get("scope_key") or "")
        if key not in selected:
            raise ContractError("nwp_rotation_outcome_scope_invalid")
        latest_outcomes.append({
            "scope_key": key,
            "state": str(row.get("state") or ""),
            "candidate_only": bool(row.get("candidate_only")),
            "reason": str(row.get("reason") or ""),
        })
    identity = {
        "schema": SCHEMA,
        "generation": int((previous_valid or {}).get("generation") or 0) + 1,
        "updated_at_utc": str(updated_at_utc),
        "station_scope": {
            "mode": "canonical_allowlist",
            "station_ids": sorted({
                str(item or "").strip().upper()
                for item in station_ids
                if str(item or "").strip()
            }),
        },
        "catalog_sha256": catalog_sha,
        "catalog_scope_keys": catalog,
        "catalog_scope_count": len(catalog),
        "attempted_scope_keys": sorted(attempted),
        "attempted_scope_count": len(attempted),
        "complete_rotation": bool(catalog) and attempted == set(catalog),
        "latest_selected_scope_keys": selected,
        "latest_outcomes": latest_outcomes,
        "cursor_by_priority": {
            str(key): int(value)
            for key, value in sorted(cursor_by_priority.items())
            if int(key) in {0, 1, 2}
        },
        "previous_content_sha256": str(
            (previous_valid or {}).get("content_sha256") or ""
        ),
        "production_no_live": True,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    result = {
        **identity,
        "content_sha256": content_sha256(identity),
    }
    validate_rotation_state(result, station_ids=station_ids)
    return result


def persist_rotation_state(path: Path, value: Mapping[str, Any]) -> bool:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ContractError("nwp_rotation_state_symlink_forbidden")
    return write_bounded_json_if_changed(
        candidate,
        value,
        limit=MAX_BYTES,
    )


__all__ = [
    "MAX_CATALOG_SCOPES",
    "MAX_SELECTED_SCOPES",
    "SCHEMA",
    "build_rotation_state",
    "load_rotation_state",
    "persist_rotation_state",
    "scope_key",
    "validate_rotation_state",
]
