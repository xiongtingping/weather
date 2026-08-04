#!/usr/bin/env python3
"""Materialize official AMSC TAF and NWP station snapshots.

This is a bounded weak-source materializer.  It writes only the two snapshot
tables and never invokes a production decision or order path.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weather_basket_yes_schema import apply_weather_basket_yes_schema  # noqa: E402
from reported_trajectory_snapshot_materializer import (  # noqa: E402
    materialize_amsc_taf,
    materialize_official_nwp_station_snapshots,
)
from daily_regime_snapshot_materializer import materialize_daily_regime_snapshot  # noqa: E402


BASE_STATIONS = (
    {"station_id": "ZBAA", "loc_key": "40.0801,116.5846"},
    {"station_id": "ZGGG", "loc_key": "23.3924,113.2988"},
    {"station_id": "ZGSZ", "loc_key": "22.6393,113.8107"},
    {"station_id": "ZHHH", "loc_key": "30.7838,114.2081"},
    {"station_id": "ZSPD", "loc_key": "31.1443,121.8083"},
    {"station_id": "ZSQD", "loc_key": "36.2661,120.3744"},
    {"station_id": "ZUCK", "loc_key": "29.7192,106.6419"},
    {"station_id": "ZUUU", "loc_key": "30.5583,103.9460"},
)


def _load_stations(path: Path) -> list[dict[str, Any]]:
    # The national airport catalogue is UI-only.  It must never expand the
    # source materializer into 52 stations or make catalogue rows look like
    # configured production source lanes.
    del path
    return [dict(item) for item in BASE_STATIONS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--event-date", default=datetime.now().date().isoformat())
    parser.add_argument("--catalog", type=Path, default=ROOT / "config" / "ui" / "china_airport_station_catalog_v1.json")
    parser.add_argument("--nwp-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_sources")
    parser.add_argument("--skip-taf", action="store_true")
    parser.add_argument("--skip-nwp", action="store_true")
    parser.add_argument("--max-nwp-snapshots", type=int, default=96)
    parser.add_argument("--no-decode-missing-stations", action="store_true")
    parser.add_argument("--providers", default="", help="comma-separated official NWP providers to project")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db.expanduser().resolve(), timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        apply_weather_basket_yes_schema(conn)
        stations = _load_stations(args.catalog.expanduser().resolve())
        result: dict[str, Any] = {
            "schema": "amsc_taf_nwp_snapshot_materialization_run_v1",
            "ok": True,
            "event_date": str(args.event_date)[:10],
            "station_count": len(stations),
            "read_only_orders": True,
        }
        if not args.skip_taf:
            try:
                result["taf"] = materialize_amsc_taf(
                    conn,
                    station_ids=[str(item["station_id"]) for item in stations],
                    target_event_date=str(args.event_date)[:10],
                )
            except Exception as exc:
                result["taf"] = {"ok": False, "state": "blocked", "reason": f"{type(exc).__name__}:{exc}"}
                result["ok"] = False
        if not args.skip_nwp:
            result["nwp"] = materialize_official_nwp_station_snapshots(
                conn,
                stations=stations,
                target_event_date=str(args.event_date)[:10],
                source_root=args.nwp_root.expanduser().resolve(),
                max_snapshots=max(1, int(args.max_nwp_snapshots)),
                decode_missing_station_points=not args.no_decode_missing_stations,
                providers=[item.strip() for item in str(args.providers or "").split(",") if item.strip()],
            )
        result["regime"] = [
            materialize_daily_regime_snapshot(
                conn,
                station_id=str(item["station_id"]),
                event_date=str(args.event_date)[:10],
            )
            for item in stations
        ]
        result["regime_summary"] = {
            "materialized_count": sum(item.get("state") == "materialized" for item in result["regime"]),
            "already_materialized_count": sum(item.get("state") == "already_materialized" for item in result["regime"]),
            "waiting_count": sum(item.get("state") == "waiting" for item in result["regime"]),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result.get("ok") else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
