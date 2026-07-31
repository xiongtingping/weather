#!/usr/bin/env python3
"""Boundedly fill missing forward ensemble station-day scopes.

This is a one-time/operator accelerator for the existing three-service
production-no-live topology.  It writes only the resident native-ensemble
inbox; the sole production writer ingests that inbox into the target SSOT.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    parser.add_argument("--max-scopes", type=int, default=12)
    parser.add_argument("--refresh-station-id")
    parser.add_argument("--refresh-event-date")
    args = parser.parse_args()
    if args.max_scopes < 1 or args.max_scopes > MAX_SCOPES_PER_RUN:
        raise RuntimeError("native_ensemble_backfill_scope_limit_invalid")
    if bool(args.refresh_station_id) != bool(args.refresh_event_date):
        raise RuntimeError(
            "native_ensemble_refresh_scope_identity_incomplete"
        )
    registry_path = args.registry.expanduser().resolve(strict=True)
    inbox_path = args.inbox.expanduser().resolve()
    registry = load_market_registry_snapshot(registry_path)
    scopes = registry_capture_scopes(
        registry,
        as_of_utc=_now(),
        max_scopes=MAX_SCOPES_PER_RUN,
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
    if args.refresh_station_id:
        refresh_scope = (
            str(args.refresh_station_id).strip().upper(),
            str(args.refresh_event_date).strip(),
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
