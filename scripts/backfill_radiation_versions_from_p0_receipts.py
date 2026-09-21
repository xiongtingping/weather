#!/usr/bin/env python3
"""Recover the radiation P0 was served, as versions, from its own receipts.

Every served P0 envelope since 2026-08-16 records in ``synoptic_cooling_json``
the forecast radiation it read from ``calibration_station_forecast_physics``
at that moment.  That table has since been overwritten row by row until each
day's own evening, so the receipts are the only record of what a decision
could have known.  This writes them into the append-only version table as
source ``P0_ENVELOPE_RECEIPT``: per station-day, one version per change of
value, stamped with the commit time of the first envelope that carried it
(the value was current no later than that, so the stamp never looks ahead).

Idempotent: a version already present (same key and stamp) is skipped, so a
rerun adds only what is new.  Reads envelopes through the station/stage
recency index, one station at a time.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import forecast_physics_asof as ASOF  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

DEFAULT_SINCE = "2026-08-16"


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def receipt_versions(
    rows: list[tuple[str, str, str | None]],
) -> dict[str, list[tuple[str, float]]]:
    """``event_date -> [(created_at, radiation)]``, one entry per change of value.

    ``rows`` are ``(event_date, created_at, synoptic_cooling_json)``.
    """

    by_day: dict[str, list[tuple[datetime, str, float]]] = {}
    for event_date, created_at, raw in rows:
        if not raw or str(raw).startswith("@ref/"):
            continue
        try:
            receipt = json.loads(raw)
        except (TypeError, ValueError):
            continue
        value = receipt.get("radiation_sum") if isinstance(receipt, dict) else None
        if value is None:
            continue
        by_day.setdefault(str(event_date), []).append(
            (_instant(created_at), str(created_at), float(value))
        )
    out: dict[str, list[tuple[str, float]]] = {}
    for day, entries in by_day.items():
        entries.sort()
        versions: list[tuple[str, float]] = []
        for _instant_value, stamp, value in entries:
            if not versions or versions[-1][1] != value:
                versions.append((stamp, value))
        out[day] = versions
    return out


def backfill_station(
    conn: sqlite3.Connection, *, station_id: str, timezone_name: str, since: str,
) -> int:
    rows = conn.execute(
        "SELECT event_date,created_at,"
        "json_extract(lineage_json,'$.synoptic_cooling_json') "
        "FROM basket_probability_envelope "
        "INDEXED BY basket_probability_envelope_station_stage_recency_idx "
        "WHERE station_id=? AND stage='P0' AND state='COMMITTED' "
        "AND decision_at_utc>=?",
        (station_id, since),
    ).fetchall()
    by_day = receipt_versions(rows)
    if not by_day:
        return 0
    scope = station_coordinates(station_id)
    added = 0
    for day, versions in by_day.items():
        for stamp, value in versions:
            identity = {
                "schema": "station_forecast_physics_receipt_version_v1",
                "station_id": station_id,
                "local_date": day,
                "source_id": ASOF.RECEIPT_SOURCE_ID,
                "shortwave_radiation_sum": value,
                "captured_at_utc": stamp,
            }
            added += conn.execute(
                f"INSERT OR IGNORE INTO {ASOF.VERSION_TABLE}"
                "(station_id,local_date,source_id,shortwave_radiation_sum,"
                "windspeed_10m_max,cloudcover_mean,precipitation_sum,"
                "latitude,longitude,settlement_timezone,capture_mode,"
                "captured_at_utc,reference_only,content_sha256) "
                "VALUES(?,?,?,?,NULL,NULL,NULL,?,?,?,?,?,1,?)",
                (
                    station_id, day, ASOF.RECEIPT_SOURCE_ID, value,
                    float(scope["latitude"]), float(scope["longitude"]),
                    timezone_name, ASOF.RECEIPT_CAPTURE_MODE, stamp,
                    content_sha256(identity),
                ),
            ).rowcount
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "bayes_basket_target_ssot.db")
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--station", action="append", default=None)
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ASOF.VERSION_TABLE,)
    ).fetchone()
    if not exists:
        print(json.dumps({"state": "REFUSED_NO_VERSION_TABLE"}))
        return 3
    zones = {
        str(s): str(z)
        for s, z in conn.execute(
            "SELECT station_id,settlement_timezone FROM calibration_station_market_timezone_binding"
        )
        if len(str(s)) == 4
    }
    stations = args.station or sorted(zones)
    receipts = []
    for station_id in stations:
        added = backfill_station(
            conn, station_id=station_id, timezone_name=zones[station_id], since=args.since,
        )
        conn.commit()
        receipts.append({"station_id": station_id, "added": added})
    total = conn.execute(
        f"SELECT COUNT(*) FROM {ASOF.VERSION_TABLE} WHERE source_id=?", (ASOF.RECEIPT_SOURCE_ID,)
    ).fetchone()[0]
    conn.close()
    print(json.dumps({
        "schema": "radiation_receipt_version_backfill_v1",
        "since": args.since,
        "stations": len(stations),
        "added": sum(r["added"] for r in receipts),
        "receipt_version_rows": total,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
