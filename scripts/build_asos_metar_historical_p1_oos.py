#!/usr/bin/env python3
"""Download historical ASOS minute data and exact-match canonical AWC METAR."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asos_one_minute_ingress import (
    decode_asos_one_minute_temperature_payload,
    fetch_asos_one_minute_temperature,
    icao_to_iem_station,
)
from madis_hfmetar import (
    AsosTemperatureProduct,
    build_asos_p1_oos_artifact,
    match_metar_to_asos,
)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _station_timezones(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """SELECT station_id,settlement_timezone
             FROM reported_trajectory_market_registry
            WHERE source_lane='awc' AND COALESCE(station_id,'')<>''
              AND COALESCE(settlement_timezone,'')<>''
            GROUP BY station_id,settlement_timezone ORDER BY station_id"""
    ).fetchall()
    grouped: dict[str, set[str]] = defaultdict(set)
    for station, timezone_name in rows:
        if icao_to_iem_station(str(station)) is not None:
            grouped[str(station).upper()].add(str(timezone_name))
    return {station: next(iter(values)) for station, values in grouped.items() if len(values) == 1}


def _metars(
    conn: sqlite3.Connection,
    station: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, object]]:
    return [
        {
            "observation_id": str(row[0]), "station_id": str(row[1]),
            "observed_at_utc": str(row[2]), "reported_temperature_c": float(row[3]),
            "raw_sha256": str(row[4]),
        }
        for row in conn.execute(
            """SELECT observation_id,station_id,valid_at,reported_temperature_c,raw_sha256
                 FROM reported_trajectory_observations
                WHERE source_lane='awc' AND report_type='METAR' AND station_id=?
                  AND valid_at>=? AND valid_at<? ORDER BY valid_at,observation_id""",
            (station, _stamp(start), _stamp(end)),
        )
    ]


def build(
    database: Path,
    output: Path,
    *,
    start_date: date,
    end_date: date,
    station_ids: list[str],
    reuse_retained: bool = False,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    retained_index: dict[tuple[str, tuple[str, ...]], dict[str, object]] = {}
    if reuse_retained:
        manifest_path = output / "manifest.json"
        if not manifest_path.exists():
            raise ValueError("asos_reuse_manifest_missing")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        retained_index = {
            (str(row["event_date"]), tuple(sorted(str(x) for x in row["station_ids"]))): row
            for row in previous.get("downloads", [])
        }
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        registry = _station_timezones(conn)
        selected = sorted(set(station_ids) & set(registry)) if station_ids else sorted(registry)
        matches_by_station: dict[str, list[dict[str, object]]] = defaultdict(list)
        downloads: list[dict[str, object]] = []
        failures: list[dict[str, str]] = []
        current = start_date
        while current <= end_date:
            groups: dict[tuple[str, str], list[str]] = defaultdict(list)
            for station in selected:
                zone = ZoneInfo(registry[station])
                local = datetime.combine(current, datetime.min.time(), tzinfo=zone)
                groups[(_stamp(local), _stamp(local + timedelta(days=1)))].append(station)
            for (start_text, end_text), stations in sorted(groups.items()):
                start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
                try:
                    retained = retained_index.get((current.isoformat(), tuple(sorted(stations))))
                    if reuse_retained:
                        if retained is None:
                            raise ValueError("asos_retained_group_missing")
                        raw_path = Path(str(retained["raw_path"]))
                        if not raw_path.is_absolute():
                            raw_path = ROOT / raw_path
                        payload = raw_path.read_bytes()
                        if hashlib.sha256(payload).hexdigest() != retained["raw_sha256"]:
                            raise ValueError("asos_retained_raw_hash_mismatch")
                        poll = {
                            "reports": decode_asos_one_minute_temperature_payload(payload, stations),
                            "source_receipt_sha256": retained["source_receipt_sha256"],
                        }
                    else:
                        poll = fetch_asos_one_minute_temperature(
                            stations, start_utc=start, end_utc=end, timeout=30.0
                        )
                        payload = bytes(poll.pop("payload"))
                    digest = hashlib.sha256(payload).hexdigest()
                    raw_path = output / "raw" / f"iem_asos1min_{current}_{digest}.csv"
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    if not raw_path.exists():
                        raw_path.write_bytes(payload)
                    downloads.append({
                        "event_date": current.isoformat(), "station_ids": stations,
                        "raw_path": str(raw_path), "raw_sha256": digest,
                        "source_receipt_sha256": poll["source_receipt_sha256"],
                        "report_count": len(poll["reports"]),
                    })
                    by_station: dict[str, list[AsosTemperatureProduct]] = defaultdict(list)
                    for row in poll["reports"]:
                        by_station[str(row["station_id"])].append(AsosTemperatureProduct(
                            datetime.fromisoformat(str(row["valid_at"]).replace("Z", "+00:00")),
                            float(row["temperature_c"]), str(row["raw_record_sha256"]),
                        ))
                    for station in stations:
                        for metar in _metars(conn, station, start, end):
                            matches_by_station[station].append(
                                match_metar_to_asos(metar, by_station.get(station, []), [])
                            )
                except Exception as exc:
                    failures.append({
                        "event_date": current.isoformat(), "station_ids": ",".join(stations),
                        "reason": f"{type(exc).__name__}:{exc}",
                    })
            current += timedelta(days=1)
    artifacts: dict[str, dict[str, object]] = {}
    for station, matches in sorted(matches_by_station.items()):
        artifact = build_asos_p1_oos_artifact(matches)
        artifacts[station] = artifact
        _atomic_json(output / station / "asos_metar_matches.json", matches)
        _atomic_json(output / station / "asos_p1_oos_artifact.json", artifact)
    report = {
        "schema": "asos_metar_historical_p1_oos_build_v1",
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "station_ids": selected, "download_count": len(downloads),
        "reused_retained_raw_only": reuse_retained,
        "downloads": downloads, "failures": failures, "artifacts": artifacts,
        "publishes_pr04_pointer": False, "starts_services": False,
        "calls_order_gateway": False, "orders_submitted": 0,
    }
    _atomic_json(output / "manifest.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--station", action="append", default=[])
    parser.add_argument("--reuse-retained", action="store_true")
    args = parser.parse_args()
    if args.end_date < args.start_date or (args.end_date - args.start_date).days > 62:
        raise SystemExit("historical_window_invalid")
    report = build(
        args.db, args.output, start_date=args.start_date, end_date=args.end_date,
        station_ids=[str(value).upper() for value in args.station],
        reuse_retained=args.reuse_retained,
    )
    print(json.dumps({
        "download_count": report["download_count"],
        "failure_count": len(report["failures"]),
        "artifact_states": {key: value["state"] for key, value in report["artifacts"].items()},
    }, sort_keys=True))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
