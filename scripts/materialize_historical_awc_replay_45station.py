#!/usr/bin/env python3
"""Materialize exact 45-station canonical historical AWC replay evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.historical_awc_replay import (  # noqa: E402
    build_historical_awc_replay_evidence,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timezones(database: Path) -> dict[str, str]:
    expected = sorted(coordinate_registry_manifest()["station_ids"])
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT station_id,settlement_timezone,market_registry_timezone_count "
            "FROM calibration_station_market_timezone_binding"
        ).fetchall()
    result: dict[str, str] = {}
    for station_id, timezone_name, count in rows:
        station = str(station_id or "").strip().upper()
        timezone_value = str(timezone_name or "").strip()
        if station in result or not station or not timezone_value or int(count or 0) != 1:
            raise ContractError("historical_awc_timezone_binding_invalid")
        result[station] = timezone_value
    if sorted(result) != expected:
        raise ContractError("historical_awc_exact_45_station_scope_required")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation-artifact", action="append", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    input_paths = sorted({path.expanduser().resolve() for path in args.observation_artifact})
    packets = [load_bounded_json(path, limit=MAX_JSON_INPUT_BYTES) for path in input_paths]
    timezones = _timezones(args.database)
    output_root = args.output_root.expanduser().resolve()
    station_rows = []
    for station, timezone_name in sorted(timezones.items()):
        evidence = build_historical_awc_replay_evidence(
            packets=packets,
            raw_root=args.raw_root,
            station_id=station,
            settlement_timezone=timezone_name,
            start_date=args.start_date,
            end_date=args.end_date,
            as_of_utc=args.as_of_utc,
        )
        path = output_root / station / "historical_awc_replay_evidence.json"
        write_bounded_json(path, evidence)
        station_rows.append({
            "station_id": station,
            "settlement_timezone": timezone_name,
            "path": str(path),
            "file_sha256": _file_sha256(path),
            "content_sha256": evidence["evidence_sha256"],
            "observation_count": len(evidence["observations"]),
            "observed_date_count": len(evidence["observed_dates"]),
            "truth_ready_date_count": len(evidence["truth_ready_dates"]),
            "truth_blocked_dates": evidence["truth_blocked_dates"],
        })
    identity = {
        "schema": "bayes_basket_target_historical_awc_replay_45station_manifest_v2",
        "as_of_utc": args.as_of_utc,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "station_count": len(station_rows),
        "input_artifacts": [{
            "path": str(path),
            "file_sha256": _file_sha256(path),
        } for path in input_paths],
        "stations": station_rows,
        "summary": {
            "observation_count": sum(row["observation_count"] for row in station_rows),
            "observed_station_day_count": sum(row["observed_date_count"] for row in station_rows),
            "truth_ready_station_day_count": sum(row["truth_ready_date_count"] for row in station_rows),
            "complete_station_count": sum(not row["truth_blocked_dates"] for row in station_rows),
            "blocked_station_count": sum(bool(row["truth_blocked_dates"]) for row in station_rows),
        },
        "production_no_live": True,
        "writes_production_db": False,
        "writes_target_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    manifest = {**identity, "content_sha256": content_sha256(identity)}
    manifest_path = output_root / "manifest.json"
    write_bounded_json(manifest_path, manifest)
    print(json.dumps({
        "manifest": str(manifest_path),
        "content_sha256": manifest["content_sha256"],
        **manifest["summary"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
