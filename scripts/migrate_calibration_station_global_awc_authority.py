#!/usr/bin/env python3
"""Append the current 45-station global-AWC calibration authority projection."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_target_db_capacity,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402
from bayes_basket_target.station_source_authority import (  # noqa: E402
    MAX_STATIONS,
    migrate_global_awc_source_authorities,
)


DEFAULT_LANE_MAP = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
DEFAULT_RECEIPT = ROOT / "output/audits/calibration_station_global_awc_authority_20260801.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _stations(path: Path) -> tuple[list[str], str]:
    packet = load_bounded_json(path.expanduser().resolve())
    if not isinstance(packet, Mapping) or packet.get("schema") != "bayes_basket_target_station_lane_map_v1":
        raise RuntimeError("global_awc_authority_lane_map_invalid")
    mapping = packet.get("stations")
    if not isinstance(mapping, Mapping):
        raise RuntimeError("global_awc_authority_lane_map_stations_invalid")
    stations = sorted({str(station).strip().upper() for station, lane in mapping.items() if str(lane).strip().lower() == "awc"})
    if len(stations) != MAX_STATIONS or len(mapping) != MAX_STATIONS:
        raise RuntimeError("global_awc_authority_lane_map_not_45_awc")
    return stations, content_sha256(packet)


def _resolved_activated_at(
    conn: sqlite3.Connection,
    *,
    stations: list[str],
    authority_version: str,
    requested: str | None,
) -> str:
    if requested:
        return requested
    rows = conn.execute(
        "SELECT activated_at_utc FROM calibration_station_source_authority "
        "WHERE authority_version=? AND station_id IN ("
        + ",".join("?" for _ in stations)
        + ") ORDER BY activated_at_utc",
        (authority_version, *stations),
    ).fetchall()
    values = {str(row[0]) for row in rows}
    # A completed append-only projection must rerun as an idempotent read;
    # do not synthesize a fresh activation clock and falsely conflict with it.
    if len(rows) == len(stations) and len(values) == 1:
        return next(iter(values))
    return _now()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument("--authority-version", default="global_awc_ssot_v1")
    parser.add_argument("--as-of-utc")
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    database = resolve_target_database(args.db)
    if not database.is_file():
        raise RuntimeError("global_awc_authority_target_db_missing")
    stations, lane_map_sha = _stations(args.lane_map)
    assert_target_db_capacity(database, projected_bytes=2 * 1024 * 1024)
    with sqlite3.connect(database, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        apply_production_ssot_schema(conn)
        activated_at = _resolved_activated_at(
            conn,
            stations=stations,
            authority_version=str(args.authority_version),
            requested=args.as_of_utc,
        )
        report = migrate_global_awc_source_authorities(
            conn,
            station_ids=stations,
            authority_version=str(args.authority_version),
            activated_at_utc=activated_at,
            lane_map_content_sha256=lane_map_sha,
            commit=bool(args.commit),
        )
    identity = {
        **report,
        "database": str(database),
        "station_lane_map": str(args.lane_map.expanduser().resolve()),
        "station_lane_map_content_sha256": lane_map_sha,
    }
    receipt = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(args.receipt, receipt)
    print(json.dumps({
        "receipt": str(args.receipt.expanduser().resolve()),
        "state": receipt["state"],
        "station_count": receipt["station_count"],
        "wrote_count": receipt["wrote_count"],
        "idempotent_count": receipt["idempotent_count"],
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
