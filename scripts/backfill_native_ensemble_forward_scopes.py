#!/usr/bin/env python3
"""Boundedly fill missing forward ensemble station-day scopes.

This is a one-time/operator accelerator for the existing three-service
production-no-live topology.  It writes only the resident native-ensemble
inbox; the sole production writer ingests that inbox into the target SSOT.
The default mode fills absent scopes.  ``--refresh-stale`` additionally
refreshes expired/missing Open-Meteo packets, but a fresh same-scope official
native packet always suppresses republisher capture.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    content_sha256,
)
from bayes_basket_target.p0_preselection import (  # noqa: E402
    load_market_registry_snapshot,
)
from bayes_basket_target.official_nwp_capture_scheduler import (  # noqa: E402
    registry_capture_scopes,
)
from bayes_basket_target.open_meteo_ensemble import (  # noqa: E402
    capture_scope,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    station_coordinates,
)


DEFAULT_RUNTIME_ROOT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
)
MAX_SCOPES_PER_RUN = 90
MAX_INBOX_PACKETS = 256
DEFAULT_FRESH_SECONDS = 6 * 60 * 60


def _parse_utc(value: Any) -> datetime | None:
    """Parse one receipt clock without allowing naive timestamps."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _packet_is_fresh(
    packet: Mapping[str, Any] | None,
    *,
    timestamp_key: str,
    as_of: datetime,
    max_age_seconds: int,
) -> bool:
    """Return true only for a bounded, non-future snapshot receipt."""

    if not isinstance(packet, Mapping):
        return False
    captured = _parse_utc(packet.get(timestamp_key))
    if captured is None:
        return False
    age = (as_of - captured).total_seconds()
    return 0 <= age < int(max_age_seconds)


def _load_packet_index(
    path: Path,
    *,
    packet_schema: str,
) -> tuple[dict[tuple[str, str], dict[str, Any]], str]:
    """Load a bounded inbox index; malformed optional native input is absent.

    The fallback collector must not fail merely because an old official inbox
    is unavailable.  A malformed packet is treated as native-unavailable and
    recorded in the returned status, while the raw file is never rewritten.
    """

    if not path.is_file():
        return {}, "missing"
    try:
        inbox = load_bounded_json(path)
        identity = {
            key: value
            for key, value in inbox.items()
            if key != "content_sha256"
        }
        if (
            not isinstance(inbox, Mapping)
            or inbox.get("schema") != "native_ensemble_forward_inbox_v1"
            or not isinstance(inbox.get("packets"), list)
            or str(inbox.get("content_sha256") or "")
            != content_sha256(identity)
        ):
            return {}, "invalid"
        indexed: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in inbox["packets"]:
            if not isinstance(raw, Mapping) or raw.get("schema") != packet_schema:
                continue
            station = str(raw.get("station_id") or "").strip().upper()
            event_date = str(raw.get("event_date") or "").strip()
            if station and event_date:
                indexed[(station, event_date)] = dict(raw)
        return indexed, "ready"
    except Exception:
        return {}, "invalid"


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _scope_key(value: dict[str, Any]) -> tuple[str, str]:
    return (
        str(value.get("station_id") or "").strip().upper(),
        str(value.get("event_date") or "").strip(),
    )


def _normalize_event_date(value: Any) -> str | None:
    """Return one canonical target date, rejecting ambiguous input."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise RuntimeError("native_ensemble_event_date_invalid")
    return parsed.date().isoformat()


def _select_event_date_scopes(
    scopes: list[dict[str, Any]],
    *,
    event_date: str | None,
) -> list[dict[str, Any]]:
    """Restrict registry scopes before any packet/fallback selection."""

    if event_date is None:
        return scopes
    return [
        scope
        for scope in scopes
        if str(scope.get("event_date") or "").strip() == event_date
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT / "market_registry_forward.json",
    )
    parser.add_argument(
        "--inbox",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT
        / "native_ensemble_forward_inbox.json",
    )
    parser.add_argument(
        "--official-inbox",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT
        / "official_native_ensemble_forward_inbox.json",
        help=(
            "Existing official-native inbox used only for precedence.  A "
            "fresh same-station/day native packet suppresses republisher "
            "capture; it is never overwritten by this command."
        ),
    )
    parser.add_argument("--max-scopes", type=int, default=12)
    parser.add_argument("--refresh-station-id")
    parser.add_argument("--refresh-event-date")
    parser.add_argument(
        "--event-date",
        help=(
            "Restrict selection to one YYYY-MM-DD event date.  This is an "
            "independent date filter; --refresh-event-date remains the "
            "legacy station-scope alias."
        ),
    )
    parser.add_argument(
        "--refresh-stale",
        action="store_true",
        help=(
            "Refresh missing or stale Open-Meteo packets.  A fresh official "
            "native packet for the exact station×event-day always wins and "
            "suppresses fallback capture."
        ),
    )
    parser.add_argument(
        "--fresh-seconds",
        type=int,
        default=DEFAULT_FRESH_SECONDS,
        help="Snapshot freshness window for explicit stale refresh (default: 21600).",
    )
    parser.add_argument(
        "--as-of-utc",
        default="",
        help="Optional deterministic selection clock; defaults to current UTC.",
    )
    args = parser.parse_args()
    if args.max_scopes < 1 or args.max_scopes > MAX_SCOPES_PER_RUN:
        raise RuntimeError("native_ensemble_backfill_scope_limit_invalid")
    if args.fresh_seconds < 60 or args.fresh_seconds > 24 * 60 * 60:
        raise RuntimeError("native_ensemble_backfill_freshness_invalid")
    legacy_event_date = _normalize_event_date(args.refresh_event_date)
    requested_event_date = _normalize_event_date(args.event_date)
    if (
        legacy_event_date is not None
        and requested_event_date is not None
        and legacy_event_date != requested_event_date
    ):
        raise RuntimeError("native_ensemble_event_date_conflict")
    event_date_filter = requested_event_date or legacy_event_date
    # Preserve the old explicit station×date contract while allowing the new
    # --event-date filter to be used independently for stale refresh.
    if args.refresh_event_date and not args.refresh_station_id:
        raise RuntimeError(
            "native_ensemble_refresh_scope_identity_incomplete"
        )
    if args.refresh_station_id and event_date_filter is None:
        raise RuntimeError(
            "native_ensemble_refresh_scope_identity_incomplete"
        )
    as_of = _parse_utc(args.as_of_utc) if args.as_of_utc else datetime.now(timezone.utc)
    if as_of is None:
        raise RuntimeError("native_ensemble_backfill_as_of_utc_invalid")
    as_of_utc = as_of.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    registry_path = args.registry.expanduser().resolve(strict=True)
    inbox_path = args.inbox.expanduser().resolve()
    official_inbox_path = args.official_inbox.expanduser().resolve()
    registry = load_market_registry_snapshot(registry_path)
    scopes = registry_capture_scopes(
        registry,
        as_of_utc=as_of_utc,
        max_scopes=MAX_SCOPES_PER_RUN,
    )
    scopes = _select_event_date_scopes(
        scopes,
        event_date=event_date_filter,
    )
    existing_packets: list[dict[str, Any]] = []
    if inbox_path.is_file():
        inbox = load_bounded_json(inbox_path)
        identity = {
            key: value
            for key, value in inbox.items()
            if key != "content_sha256"
        }
        if (
            inbox.get("schema") != "native_ensemble_forward_inbox_v1"
            or not isinstance(inbox.get("packets"), list)
            or str(inbox.get("content_sha256") or "")
            != content_sha256(identity)
        ):
            raise ContractError("native_ensemble_forward_inbox_invalid")
        existing_packets = [
            dict(value)
            for value in inbox["packets"]
            if isinstance(value, dict)
        ]
    existing_keys = {_scope_key(value) for value in existing_packets}
    official_index, official_inbox_state = _load_packet_index(
        official_inbox_path,
        packet_schema="official_native_ensemble_forward_packet_v1",
    )
    republisher_index = {
        _scope_key(value): value
        for value in existing_packets
        if value.get("schema") == "open_meteo_native_ensemble_forward_packet_v1"
    }
    if args.refresh_station_id:
        refresh_scope = (
            str(args.refresh_station_id).strip().upper(),
            event_date_filter,
        )
        selected = [
            scope
            for scope in scopes
            if (
                str(scope["station_ids"][0]).strip().upper(),
                str(scope["event_date"]).strip(),
            )
            == refresh_scope
        ]
        if len(selected) != 1:
            raise RuntimeError(
                "native_ensemble_refresh_scope_not_unique"
            )
        # Explicit scope refresh is still subject to native precedence.  It
        # may repair a stale/missing republisher packet, but never creates a
        # duplicate fallback beside a fresh official packet.
        if _packet_is_fresh(
            official_index.get(refresh_scope),
            timestamp_key="received_at_utc",
            as_of=as_of,
            max_age_seconds=args.fresh_seconds,
        ):
            selected = []
    else:
        if args.refresh_stale:
            selected = []
            for scope in scopes:
                key = (
                    str(scope["station_ids"][0]).strip().upper(),
                    str(scope["event_date"]).strip(),
                )
                # Native precedence is evaluated at the same clock as the
                # stale test.  A fresh official packet means there is no
                # fallback acquisition to perform for this exact scope.
                if _packet_is_fresh(
                    official_index.get(key),
                    timestamp_key="received_at_utc",
                    as_of=as_of,
                    max_age_seconds=args.fresh_seconds,
                ):
                    continue
                if not _packet_is_fresh(
                    republisher_index.get(key),
                    timestamp_key="aggregator_snapshot_at_utc",
                    as_of=as_of,
                    max_age_seconds=args.fresh_seconds,
                ):
                    selected.append(scope)
            selected = selected[: args.max_scopes]
        else:
            selected = [
                scope
                for scope in scopes
                if (
                    str(scope["station_ids"][0]).strip().upper(),
                    str(scope["event_date"]).strip(),
                )
                not in existing_keys
            ][: args.max_scopes]
    rows = [
        value
        for value in registry.get("rows") or []
        if isinstance(value, dict)
    ]
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    packets = list(existing_packets)
    for scope in selected:
        assert_process_memory_budget()
        station = str(scope["station_ids"][0]).strip().upper()
        event_date = str(scope["event_date"]).strip()
        captured_at = _now()
        try:
            coordinates = station_coordinates(station)
            packet = capture_scope(
                station_id=station,
                event_date=event_date,
                latitude=float(coordinates["latitude"]),
                longitude=float(coordinates["longitude"]),
                settlement_timezone=str(scope["settlement_timezone"]),
                registry_rows=[
                    row
                    for row in rows
                    if str(row.get("station_id") or "").strip().upper()
                    == station
                    and str(row.get("event_date") or "") == event_date
                ],
                captured_at_utc=captured_at,
            )
            packets = [
                value
                for value in packets
                if _scope_key(value) != (station, event_date)
            ]
            packets.append(packet)
            packets.sort(
                key=lambda value: (
                    str(value.get("event_date") or ""),
                    str(value.get("station_id") or ""),
                )
            )
            if len(packets) > MAX_INBOX_PACKETS:
                raise ContractError(
                    "native_ensemble_forward_inbox_packet_limit_exceeded"
                )
            inbox_identity = {
                "schema": "native_ensemble_forward_inbox_v1",
                "packets": packets,
            }
            write_bounded_json_if_changed(
                inbox_path,
                {
                    **inbox_identity,
                    "content_sha256": content_sha256(inbox_identity),
                },
            )
            successes.append({
                "station_id": station,
                "event_date": event_date,
                "available_product_count": len(
                    packet.get("systems") or []
                ),
                "unavailable_product_count": len(
                    packet.get("unavailable_products") or []
                ),
                "independent_family_count": int(
                    (packet.get("cross_model_gate") or {}).get(
                        "independent_family_count"
                    )
                    or 0
                ),
            })
        except Exception as exc:
            failures.append({
                "station_id": station,
                "event_date": event_date,
                "reason": f"{type(exc).__name__}:{exc}",
            })
    print(json.dumps({
        "schema": "native_ensemble_forward_scope_backfill_v1",
        "as_of_utc": as_of_utc,
        "refresh_stale": bool(args.refresh_stale),
        "event_date_filter": event_date_filter,
        "fresh_seconds": int(args.fresh_seconds),
        "official_inbox": str(official_inbox_path),
        "official_inbox_state": official_inbox_state,
        "policy": {
            "official_native_first": True,
            "fallback_requires_same_station_event_day": True,
            "fallback_source_role": "aggregator_republisher",
            "forward_evidence_role": "observational_candidate_not_oos",
            "writes_p0": False,
            "writes_p0m": False,
            "writes_p1": False,
        },
        "catalog_scope_count": len(scopes),
        "existing_scope_count": len(existing_keys),
        "attempted_scope_count": len(selected),
        "captured_scope_count": len(successes),
        "failed_scope_count": len(failures),
        "remaining_scope_count": max(
            0,
            len(scopes) - len(existing_keys) - len(successes),
        ),
        "successes": successes,
        "failures": failures,
        "inbox": str(inbox_path),
        "inbox_packet_count": len(packets),
        "production_no_live": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
