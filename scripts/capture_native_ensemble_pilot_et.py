#!/usr/bin/env python3
"""Resume-only official ensemble ET member projection for a bounded scope.

This collector owns only capture/normalize.  It never reserves a selection,
opens truth, evaluates OOS, writes the SSOT, publishes PR04, starts services,
or releases the live pause.  Existing valid day packets are replayed and are
never downloaded again.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.eccc_geps_archive import (  # noqa: E402
    MULTISTATION_SCHEMA as GEPS_SCHEMA,
    capture_geps_station_days,
)
from bayes_basket_target.ecmwf_ifs_archive import (  # noqa: E402
    MULTISTATION_SCHEMA as ECMWF_SCHEMA,
    capture_ecmwf_station_days,
)
from bayes_basket_target.market_registry import load_active_registry_rows  # noqa: E402
from bayes_basket_target.noaa_gefs_archive import (  # noqa: E402
    MULTISTATION_SCHEMA as GEFS_SCHEMA,
    capture_gefs_station_days,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402


SETTLEMENT_TIMEZONE = "America/New_York"
PROVIDERS = {
    "ECMWF_PF50": (ECMWF_SCHEMA, capture_ecmwf_station_days),
    "NOAA_GEFS": (GEFS_SCHEMA, capture_gefs_station_days),
    "ECCC_GEPS": (GEPS_SCHEMA, capture_geps_station_days),
}


def _dates(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < first or (last - first).days >= 32:
        raise ContractError("native_pilot_et_date_range_invalid")
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def _scopes(
    conn: sqlite3.Connection,
    stations: Sequence[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for station in sorted(set(stations)):
        rows = load_active_registry_rows(conn, station_id=station)
        if not rows:
            raise ContractError("native_pilot_et_registry_station_missing")
        reference_date = max(str(row.get("event_date") or "") for row in rows)
        rows = [
            row for row in rows
            if str(row.get("event_date") or "") == reference_date
        ]
        coordinates = station_coordinates(station)
        result.append({
            "station_id": station,
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "latitude": float(coordinates["latitude"]),
            "longitude": float(coordinates["longitude"]),
            "registry_rows": rows,
        })
    if not result:
        raise ContractError("native_pilot_et_station_required")
    return result


def _valid_existing(
    path: Path,
    *,
    schema: str,
    event_date: str,
    stations: Sequence[str],
) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    raw = load_bounded_json(path)
    if not isinstance(raw, Mapping):
        raise ContractError("native_pilot_et_existing_packet_invalid")
    supplied = str(raw.get("content_sha256") or "")
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    expected_stations = sorted(set(stations))
    if (
        raw.get("schema") != schema
        or raw.get("event_date") != event_date
        or raw.get("station_ids") != expected_stations
        or int(raw.get("station_count") or 0) != len(expected_stations)
        or supplied != content_sha256(identity)
    ):
        raise ContractError("native_pilot_et_existing_packet_conflict")
    captures = raw.get("captures")
    if (
        not isinstance(captures, list)
        or len(captures) != len(expected_stations)
        or any(
            not isinstance(item, Mapping)
            or item.get("settlement_timezone") != SETTLEMENT_TIMEZONE
            for item in captures
        )
    ):
        raise ContractError("native_pilot_et_existing_packet_timezone_invalid")
    return raw


def capture_range(
    *,
    database: Path,
    provider: str,
    stations: Sequence[str],
    start_date: str,
    end_date: str,
    output_dir: Path,
) -> dict[str, Any]:
    provider_key = provider.strip().upper()
    if provider_key not in PROVIDERS:
        raise ContractError("native_pilot_et_provider_invalid")
    station_ids = sorted({str(value).strip().upper() for value in stations if str(value).strip()})
    if not station_ids:
        raise ContractError("native_pilot_et_station_required")
    schema, collector = PROVIDERS[provider_key]
    with sqlite3.connect(f"file:{database.resolve(strict=True)}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        scopes = _scopes(conn, station_ids)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for event_date in _dates(start_date, end_date):
        path = output_dir / f"{provider_key}_{event_date.replace('-', '')}_ET.json"
        existing = _valid_existing(
            path,
            schema=schema,
            event_date=event_date,
            stations=station_ids,
        )
        if existing is not None:
            rows.append({
                "event_date": event_date,
                "state": "REPLAYED_EXISTING",
                "path": str(path.resolve()),
                "content_sha256": existing["content_sha256"],
            })
            continue
        queried_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        packet = collector(
            event_date=event_date,
            station_scopes=scopes,
            archive_query_at_utc=queried_at,
        )
        write_bounded_json_if_changed(path.resolve(), packet)
        rows.append({
            "event_date": event_date,
            "state": "CAPTURED",
            "path": str(path.resolve()),
            "content_sha256": packet["content_sha256"],
        })
    identity = {
        "schema": "official_native_ensemble_pilot_et_capture_receipt_v1",
        "provider": provider_key,
        "station_ids": station_ids,
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "start_date": start_date,
        "end_date": end_date,
        "packet_count": len(rows),
        "captured_count": sum(row["state"] == "CAPTURED" for row in rows),
        "replayed_count": sum(row["state"] == "REPLAYED_EXISTING" for row in rows),
        "packets": rows,
        "writes_ssot": False,
        "reserves_selection": False,
        "evaluates_oos": False,
        "publishes_pr04": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    args = parser.parse_args()
    receipt = capture_range(
        database=args.database.expanduser(),
        provider=args.provider,
        stations=args.station_id,
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=args.output_dir.expanduser(),
    )
    write_bounded_json_if_changed(args.receipt_output.expanduser().resolve(), receipt)
    print(json.dumps({
        key: receipt[key]
        for key in (
            "provider", "station_ids", "settlement_timezone", "packet_count",
            "captured_count", "replayed_count", "content_sha256",
            "writes_ssot", "publishes_pr04", "starts_services",
            "releases_live_pause", "orders_submitted",
        )
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
