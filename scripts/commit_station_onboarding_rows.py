#!/usr/bin/env python3
"""Write the two SSOT rows a new station needs before it can be captured.

``onboard_station.py`` reports; this writes.  Split because these two rows
are append-only and the tables they reference are the fleet's identity:
the registry row is what the timezone binding's foreign key points at, and
the binding is what every settlement-day window is computed from.

Order is forced by that foreign key -- registry row, then binding -- and
both are refused unless the station is already in the coordinate registry,
because a station the decoder cannot place is not a station.

The timezone written to ``calibration_station_registry.timezone_name`` is
the market's, not the seed's.  That column is wrong for 33 of the original
45 rows (EDDM, Munich, carries America/New_York) and cannot be corrected --
the table is append-only -- which is precisely why the binding table exists
and why every consumer reads the binding instead.  A new row has no reason
to reproduce the error, and the binding is written from the same source, so
the two agree for stations onboarded here.

``market_registry_timezone_count`` is CHECK-constrained to 1: a station
whose listings disagree about their own timezone has no settlement day, and
this refuses it rather than picking one.

Read-only unless ``--commit``.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)

TARGET_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE_DB = ROOT / "data" / "archive.db"
REGISTRY_VERSION = "bayes_basket_target_calibration_evidence_v1"
BINDING_VERSION = "calibration_station_market_timezone_binding_v1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"


def _market_timezone(station_id: str) -> tuple[str, int]:
    conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=60)
    try:
        rows = conn.execute(
            "SELECT DISTINCT settlement_timezone"
            " FROM reported_trajectory_market_registry"
            " WHERE station_id=? AND settlement_timezone IS NOT NULL"
            "   AND settlement_timezone <> ''",
            (station_id,),
        ).fetchall()
    finally:
        conn.close()
    zones = sorted({str(row[0]) for row in rows})
    return (zones[0] if zones else ""), len(zones)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--station", action="append", required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    known = {
        str(v).upper()
        for v in coordinate_registry_manifest()["station_ids"]
    }
    stamp = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    conn = sqlite3.connect(
        str(TARGET_DB) if args.commit
        else f"file:{TARGET_DB}?mode=ro",
        uri=not args.commit, timeout=60.0,
    )
    conn.execute("PRAGMA busy_timeout=60000")
    written = blocked = 0
    try:
        for station in [s.strip().upper() for s in args.station]:
            if station not in known:
                print(f"BLOCK {station}: not in the coordinate registry")
                blocked += 1
                continue
            zone, count = _market_timezone(station)
            if count != 1:
                print(
                    f"BLOCK {station}: market registry offers {count} "
                    f"timezones; a settlement day needs exactly one"
                )
                blocked += 1
                continue
            registry_row = {
                "station_id": station,
                "icao_code": station,
                "city_label": "",
                "timezone_name": zone,
                "source_lane": "AWC",
                "settlement_definition": SETTLEMENT_DEFINITION,
                "registry_version": REGISTRY_VERSION,
                "source_role": "CANONICAL_AMSC_AWC_INGRESS",
            }
            registry_row["content_sha256"] = content_sha256(registry_row)
            binding_row = {
                "station_id": station,
                "settlement_timezone": zone,
                "market_registry_timezone_count": 1,
                "historical_truth_evidence_sha256": None,
                "binding_version": BINDING_VERSION,
                "bound_at_utc": stamp,
            }
            binding_row["binding_id"] = "station-market-tz-" + content_sha256(
                {"station_id": station, "settlement_timezone": zone}
            )[:24]
            binding_row["content_sha256"] = content_sha256(binding_row)
            print(
                f"{'WRITE' if args.commit else 'WOULD-WRITE'} {station}"
                f" tz={zone}"
            )
            if not args.commit:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO calibration_station_registry"
                    " (station_id,icao_code,city_label,timezone_name,"
                    "  source_lane,settlement_definition,registry_version,"
                    "  source_role,content_sha256)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        registry_row["station_id"], registry_row["icao_code"],
                        registry_row["city_label"],
                        registry_row["timezone_name"],
                        registry_row["source_lane"],
                        registry_row["settlement_definition"],
                        registry_row["registry_version"],
                        registry_row["source_role"],
                        registry_row["content_sha256"],
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO"
                    " calibration_station_market_timezone_binding"
                    " (binding_id,station_id,settlement_timezone,"
                    "  market_registry_timezone_count,"
                    "  historical_truth_evidence_sha256,binding_version,"
                    "  bound_at_utc,content_sha256)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        binding_row["binding_id"], binding_row["station_id"],
                        binding_row["settlement_timezone"], 1, None,
                        binding_row["binding_version"],
                        binding_row["bound_at_utc"],
                        binding_row["content_sha256"],
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            written += 1
    finally:
        conn.close()
    print(f"\n{'written' if args.commit else 'would write'}={written} "
          f"blocked={blocked}")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
