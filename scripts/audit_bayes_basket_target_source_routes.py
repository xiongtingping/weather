#!/usr/bin/env python3
"""Read-only audit for the target station truth-source route SSOT.

This audit has one deliberately narrow authority boundary:

* ``bayes_basket_target_station_lane_map_v1.json`` owns the target station to
  official truth-lane mapping.
* ``reported_trajectory_official_sources_v1.json`` contributes station keys
  only.  Its provider and fallback values belong to the hard-NO lane and are
  never interpreted here.
* :mod:`bayes_basket_target.source_policy` owns lane and reference-only
  semantics.

The module is pure/read-only apart from the optional atomic JSON receipt
written by the CLI.  A failed audit never returns usable canonical routes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import (  # noqa: E402
    ResourceBoundError,
    assert_process_memory_budget,
    bounded_json_bytes,
    write_bounded_json,
)
from bayes_basket_target.source_policy import (  # noqa: E402
    LANES,
    reference_only_reason,
)


SCHEMA = "bayes_basket_target_source_routes_audit_v1"
LANE_MAP_SCHEMA = "bayes_basket_target_station_lane_map_v1"
CANONICAL_OPERATIONAL_TIMEZONE = "America/New_York"
SETTLEMENT_TIMEZONE_AUTHORITY = "market_registry.settlement_timezone"
MAX_STATIONS = 500
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 100_000
STATION_ID_RE = re.compile(r"^[A-Z0-9]{2,8}$")

DEFAULT_LANE_MAP = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
DEFAULT_OFFICIAL_SOURCES = ROOT / "config/trading/reported_trajectory_official_sources_v1.json"


class RouteAuditError(ValueError):
    """A bounded input or contract failure with a stable reason code."""

    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(self.reason)


class _DuplicateJSONKey(Exception):
    pass


def _shape(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_JSON_NODES:
        raise RouteAuditError("json_nodes_exceeded")
    if depth > MAX_JSON_DEPTH:
        raise RouteAuditError("json_depth_exceeded")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise RouteAuditError("json_key_invalid")
            _shape(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, list):
        for child in value:
            _shape(child, depth=depth + 1, nodes=nodes)


def _load_json(path: Path) -> Any:
    """Load one small JSON config while detecting duplicate object keys."""

    resolved = Path(path).expanduser().resolve()
    try:
        size = int(resolved.stat().st_size)
    except OSError as exc:
        raise RouteAuditError("input_stat_failed") from exc
    if size > MAX_INPUT_BYTES:
        raise RouteAuditError("input_bytes_exceeded")
    assert_process_memory_budget(projected_bytes=min(size * 3, 32 * 1024 * 1024))
    try:
        raw = resolved.read_bytes()

        def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, child in pairs:
                if key in result:
                    raise _DuplicateJSONKey(key)
                result[key] = child
            return result

        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except _DuplicateJSONKey as exc:
        raise RouteAuditError("json_duplicate_key") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RouteAuditError("json_invalid") from exc
    _shape(value)
    assert_process_memory_budget()
    return value


def _text(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    return str(value).strip()


def _station(value: Any) -> str:
    station = _text(value).upper()
    if not station or not STATION_ID_RE.fullmatch(station):
        return ""
    return station


def _add_reason(reasons: set[str], reason: str) -> None:
    if reason:
        reasons.add(str(reason))


def _as_config(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if "schema" in value or "stations" in value or "operational_timezone" in value:
        return value
    return None


def _extract_lane_map(value: Any) -> tuple[dict[str, Any], Mapping[str, Any] | None, set[str]]:
    """Return raw station map, optional config, and structural reasons."""

    reasons: set[str] = set()
    if not isinstance(value, Mapping):
        return {}, None, {"lane_map_root_invalid"}
    config = _as_config(value)
    if config is not None:
        if config.get("schema") != LANE_MAP_SCHEMA:
            reasons.add("lane_map_schema_invalid")
        stations = config.get("stations")
        if not isinstance(stations, Mapping):
            reasons.add("lane_map_stations_invalid")
            return {}, config, reasons
        return dict(stations), config, reasons
    return dict(value), None, reasons


def _validate_config(
    config: Mapping[str, Any] | None,
    expected_operational_timezone: str,
) -> set[str]:
    reasons: set[str] = set()
    if config is None:
        return reasons
    if "target_timezone" in config:
        reasons.add("route_config_settlement_timezone_authority_forbidden")
    configured_timezone = _text(config.get("operational_timezone"))
    if (
        not configured_timezone
        or configured_timezone != expected_operational_timezone
    ):
        reasons.add("config_operational_timezone_mismatch")
    if (
        _text(config.get("settlement_timezone_authority"))
        != SETTLEMENT_TIMEZONE_AUTHORITY
    ):
        reasons.add("settlement_timezone_authority_mismatch")
    boundary = config.get("boundary")
    if not isinstance(boundary, Mapping):
        return reasons | {"boundary_policy_missing"}
    if boundary.get("target_only") is not True:
        reasons.add("target_only_boundary_invalid")
    if boundary.get("allows_cross_lane_fallback") is not False:
        reasons.add("cross_lane_fallback_enabled")
    if boundary.get("writes_production_db") is not False:
        reasons.add("production_db_write_boundary_invalid")
    if boundary.get("calls_order_gateway") is not False:
        reasons.add("order_gateway_boundary_invalid")
    policy = boundary.get("reference_policy")
    if not isinstance(policy, Mapping):
        return reasons | {"reference_policy_missing"}
    expected = {
        # The current SSOT is one global AWC lane.  AMSC is retained only as
        # explicitly labelled offline/audit evidence and cannot be a route.
        "awc_global_metar_speci": "formal_truth_primary_for_all_45_stations",
        "amsc_production_lane": "retired_history_only",
        "amsc_reference_evidence": "offline_audit_only",
        "reference_enters_formal_truth": False,
        "reference_enters_calibration": False,
    }
    for key, wanted in expected.items():
        if policy.get(key) != wanted:
            reasons.add(f"reference_policy_invalid:{key}")
    if policy.get("temporary_awc_calibration") != "retired_history_only":
        reasons.add("reference_policy_invalid:temporary_awc_calibration")
    return reasons


def _normalize_expected_map(raw_map: Mapping[str, Any], reasons: set[str]) -> dict[str, str]:
    if len(raw_map) > MAX_STATIONS:
        reasons.add("station_count_exceeded")
    expected: dict[str, str] = {}
    for raw_station, raw_lane in raw_map.items():
        station = _station(raw_station)
        if not station:
            reasons.add("station_key_invalid")
            continue
        if station in expected:
            reasons.add("duplicate_station_in_lane_map")
            continue
        lane = _text(raw_lane).lower()
        if lane not in LANES:
            reasons.add("illegal_lane")
            continue
        expected[station] = lane
    if not expected:
        reasons.add("station_universe_empty")
    return expected


def _extract_routes(value: Any) -> tuple[list[Mapping[str, Any]], set[str]]:
    """Accept a station map or an explicit route list for duplicate testing."""

    reasons: set[str] = set()
    if isinstance(value, Mapping):
        if any(key in value for key in ("routes", "station_routes")):
            candidates = value.get("routes", value.get("station_routes"))
        elif "stations" in value and isinstance(value.get("stations"), Mapping):
            candidates = value["stations"]
        else:
            candidates = value
        if isinstance(candidates, Mapping):
            return [
                {"station_id": key, "truth_lane": lane}
                for key, lane in candidates.items()
            ], reasons
        if isinstance(candidates, list):
            value = candidates
        else:
            return [], {"route_list_invalid"}
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return [], {"route_list_invalid"}
    rows: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            reasons.add("route_entry_invalid")
            continue
        rows.append(item)
    return rows, reasons


def _route_lane(entry: Mapping[str, Any]) -> tuple[str, str | None]:
    keys = ("truth_lane", "official_truth_lane", "lane", "source_lane")
    present = [(key, _text(entry.get(key)).lower()) for key in keys if key in entry]
    if not present:
        return "", "route_lane_missing"
    values = {value for _, value in present}
    if len(values) != 1:
        return "", "route_lane_fields_conflict"
    lane = present[0][1]
    if lane not in LANES:
        return lane, "illegal_lane"
    return lane, None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "y", "on"}


def _explicit_boundary_intrusion(entry: Mapping[str, Any]) -> set[str]:
    reasons: set[str] = set()
    for key, value in entry.items():
        name = _text(key).lower().replace("-", "_")
        if "fallback" in name:
            # A provider id such as ``global_noaa_awc_relay`` is itself
            # evidence that this route carries fallback semantics.  Only an
            # explicit false/null/empty marker is harmless; true-like values
            # are still rejected as before.
            explicit_false = value is False or value is None or _text(value).lower() in {
                "",
                "false",
                "null",
                "none",
                "0",
                "off",
            }
            if not explicit_false:
                reasons.add("cross_lane_fallback_route")
    for key in ("route_role", "source_role", "evidence_role", "truth_role"):
        value = _text(entry.get(key)).lower().replace("-", "_")
        if "fallback" in value:
            reasons.add("cross_lane_fallback_route")
        if "reference" in value or "cross_check" in value:
            reasons.add("reference_intrudes_canonical")
    for key in ("reference_only", "is_reference_only"):
        if key in entry and _truthy(entry[key]):
            reasons.add("reference_intrudes_canonical")
    for key in ("canonical_truth", "is_canonical", "canonical"):
        if key in entry and not _truthy(entry[key]):
            reasons.add("reference_intrudes_canonical")
    if _text(entry.get("reference_reason")):
        reasons.add("reference_intrudes_canonical")
    return reasons


def _parse_valid_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    raw = _text(value)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise RouteAuditError("route_valid_at_invalid") from exc


def _official_keys(value: Iterable[str] | Mapping[str, Any] | None) -> tuple[set[str], set[str]]:
    if value is None:
        return set(), set()
    if isinstance(value, Mapping):
        items = list(value.keys())
    elif isinstance(value, (str, bytes, bytearray)):
        return set(), {"official_station_universe_invalid"}
    else:
        try:
            items = list(value)
        except TypeError:
            return set(), {"official_station_universe_invalid"}
    if len(items) > MAX_STATIONS:
        return set(), {"official_station_count_exceeded"}
    result: set[str] = set()
    reasons: set[str] = set()
    for raw_station in items:
        station = _station(raw_station)
        if not station:
            reasons.add("official_station_key_invalid")
        elif station in result:
            reasons.add("duplicate_official_station_key")
        else:
            result.add(station)
    return result, reasons


def official_station_keys_from_registry(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract only station keys; provider/fallback values are intentionally ignored."""

    if not isinstance(payload, Mapping):
        raise RouteAuditError("official_sources_root_invalid")
    routes = payload.get("station_routes")
    if not isinstance(routes, Mapping):
        raise RouteAuditError("official_sources_station_routes_missing")
    keys, reasons = _official_keys(routes)
    if reasons:
        raise RouteAuditError(sorted(reasons)[0])
    return tuple(sorted(keys))


def audit_source_routes(
    lane_map: Mapping[str, Any],
    route_entries: Any | None = None,
    *,
    official_station_keys: Iterable[str] | Mapping[str, Any] | None = None,
    expected_operational_timezone: str = CANONICAL_OPERATIONAL_TIMEZONE,
) -> dict[str, Any]:
    """Audit target routes and return a stable, non-promotable receipt.

    ``route_entries`` may be a station-to-lane mapping or a list of route
    records.  The list form is intentionally supported so duplicate routes
    cannot be hidden by JSON object-key overwrite.  No input is mutated.
    """

    reasons: set[str] = set()
    if (
        not isinstance(expected_operational_timezone, str)
        or not expected_operational_timezone.strip()
    ):
        expected_operational_timezone = CANONICAL_OPERATIONAL_TIMEZONE
        reasons.add("expected_operational_timezone_invalid")
    else:
        expected_operational_timezone = expected_operational_timezone.strip()
    raw_map, config, extraction_reasons = _extract_lane_map(lane_map)
    reasons.update(extraction_reasons)
    reasons.update(
        _validate_config(config, expected_operational_timezone)
    )
    expected = _normalize_expected_map(raw_map, reasons)

    if route_entries is None:
        route_entries = raw_map
    rows, route_reasons = _extract_routes(route_entries)
    reasons.update(route_reasons)
    if len(rows) > MAX_STATIONS:
        reasons.add("route_count_exceeded")

    seen: set[str] = set()
    accepted: dict[str, str] = {}
    for entry in rows[: MAX_STATIONS + 1]:
        station = _station(entry.get("station_id", entry.get("station")))
        if not station:
            reasons.add("route_station_key_invalid")
            continue
        if station in seen:
            reasons.add("duplicate_route")
            continue
        seen.add(station)
        lane, lane_reason = _route_lane(entry)
        if lane_reason:
            reasons.add(lane_reason)
        expected_lane = expected.get(station)
        if expected_lane is None:
            reasons.add("unknown_route_station")
        elif lane and lane != expected_lane:
            reasons.add("route_lane_mismatch")
        reasons.update(_explicit_boundary_intrusion(entry))
        source_lane = _text(entry.get("source_lane")).lower()
        if source_lane and source_lane not in LANES:
            reasons.add("illegal_source_lane")
        if source_lane and lane and source_lane != lane:
            reasons.add("source_lane_mismatch")
        if "valid_at" in entry:
            valid_at = _parse_valid_at(entry["valid_at"])
            report_type = _text(entry.get("report_type", "METAR"))
            semantic_lane = source_lane or lane
            reference_reason = reference_only_reason(
                source_lane=semantic_lane,
                report_type=report_type,
                valid_at=valid_at,
                expected_lane=expected_lane or "",
            )
            if reference_reason:
                reasons.add("reference_intrudes_canonical")
        if station in expected and lane == expected_lane and not reasons:
            accepted[station] = lane

    missing = sorted(set(expected) - seen)
    if missing:
        reasons.add("missing_route")
    route_unknown = sorted(seen - set(expected))
    if route_unknown:
        reasons.add("unknown_route_station")

    official, official_reasons = _official_keys(official_station_keys)
    reasons.update(official_reasons)
    if official_station_keys is not None and not official_reasons:
        missing_official = sorted(set(expected) - official)
        extra_official = sorted(official - set(expected))
        if missing_official or extra_official:
            reasons.add("official_station_universe_mismatch")
    else:
        missing_official = []
        extra_official = []

    # The receipt itself is the only output allowed to contain the route
    # result.  Never expose a partial accepted route set on failure.
    ok = not reasons and len(expected) <= MAX_STATIONS and len(seen) == len(expected)
    canonical_routes = (
        [{"station_id": station, "truth_lane": expected[station]} for station in sorted(expected)]
        if ok
        else []
    )
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "PASS" if ok else "FAIL",
        "ok": ok,
        "reason_codes": sorted(reasons),
        "checks": {
            "canonical_truth_routes_only": ok,
            "cross_lane_fallback_disabled": "cross_lane_fallback_enabled" not in reasons
            and "cross_lane_fallback_route" not in reasons,
            "official_universe_exact": official_station_keys is None
            or (not official_reasons and not (set(expected) ^ official)),
            "reference_only_excluded": "reference_intrudes_canonical" not in reasons,
            "station_count_within_bound": len(expected) <= MAX_STATIONS and len(rows) <= MAX_STATIONS,
            "operational_timezone_exact": (
                "config_operational_timezone_mismatch" not in reasons
            ),
            "settlement_timezone_from_market_registry": (
                "route_config_settlement_timezone_authority_forbidden"
                not in reasons
                and "settlement_timezone_authority_mismatch" not in reasons
            ),
        },
        "operational_timezone": expected_operational_timezone,
        "settlement_timezone_authority": SETTLEMENT_TIMEZONE_AUTHORITY,
        "counts": {
            "canonical_station_count": len(expected),
            "route_entry_count": len(rows),
            "unique_route_station_count": len(seen),
            "official_universe_station_count": len(official) if official_station_keys is not None else None,
        },
        "missing_route_station_ids": missing,
        "unknown_route_station_ids": route_unknown,
        "official_universe_missing_station_ids": missing_official,
        "official_universe_extra_station_ids": extra_official,
        "canonical_routes": canonical_routes,
        "resource_bounds": {
            "max_stations": MAX_STATIONS,
            "max_receipt_bytes": MAX_RECEIPT_BYTES,
            "writes_production_db": False,
            "calls_order_gateway": False,
        },
        "boundary": {
            "read_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "market_timezone_authority": SETTLEMENT_TIMEZONE_AUTHORITY,
            "operational_timezone": expected_operational_timezone,
        },
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    # Check the actual receipt shape before returning it.  This also prevents
    # an accidentally expanded diagnostic from bypassing the 1 MiB contract.
    try:
        bounded_json_bytes(receipt, limit=MAX_RECEIPT_BYTES)
    except ResourceBoundError as exc:
        # A resource failure must itself be represented as a small fail-closed
        # receipt, not as an unbounded exception trace.
        receipt = {
            "schema": SCHEMA,
            "status": "FAIL",
            "ok": False,
            "reason_codes": ["receipt_resource_bound_exceeded"],
            "checks": {"receipt_within_bound": False},
            "resource_bounds": {"max_stations": MAX_STATIONS, "max_receipt_bytes": MAX_RECEIPT_BYTES},
        }
    return receipt


# Short alias for callers that use the audit noun first.
audit_routes = audit_source_routes


def _failure_receipt(reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "FAIL",
        "ok": False,
        "reason_codes": [str(reason)],
        "checks": {"canonical_truth_routes_only": False},
        "resource_bounds": {"max_stations": MAX_STATIONS, "max_receipt_bytes": MAX_RECEIPT_BYTES},
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument("--official-sources", type=Path, default=DEFAULT_OFFICIAL_SOURCES)
    parser.add_argument(
        "--routes",
        type=Path,
        help="optional bounded route list/mapping; omitted means the canonical lane-map stations",
    )
    parser.add_argument(
        "--expected-operational-timezone",
        default=CANONICAL_OPERATIONAL_TIMEZONE,
    )
    parser.add_argument("--output", type=Path, help="optional atomic JSON receipt destination")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        lane_map = _load_json(args.lane_map)
        official_payload = _load_json(args.official_sources)
        official_keys = official_station_keys_from_registry(official_payload)
        route_entries = _load_json(args.routes) if args.routes else None
        receipt = audit_source_routes(
            lane_map,
            route_entries,
            official_station_keys=official_keys,
            expected_operational_timezone=args.expected_operational_timezone,
        )
    except (RouteAuditError, ResourceBoundError) as exc:
        receipt = _failure_receipt(getattr(exc, "reason", None) or str(exc))
    try:
        bounded = bounded_json_bytes(receipt, limit=MAX_RECEIPT_BYTES)
        if args.output:
            write_bounded_json(args.output.expanduser().resolve(), receipt, limit=MAX_RECEIPT_BYTES)
    except (ResourceBoundError, OSError) as exc:
        receipt = _failure_receipt("receipt_write_failed")
        bounded = bounded_json_bytes(receipt, limit=MAX_RECEIPT_BYTES)
    sys.stdout.buffer.write(bounded)
    return 0 if receipt.get("ok") is True else 1


if __name__ == "__main__":  # pragma: no cover - exercised by CLI tests
    raise SystemExit(main())
