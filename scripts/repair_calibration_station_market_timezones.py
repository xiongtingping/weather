#!/usr/bin/env python3
"""Repair calibration station timezone metadata from the market registry.

The historical truth rows are append-only and are not rewritten here.  This
repair only replaces stale station-registry metadata after proving that the
market registry has one timezone for the station and the historical rebuild
evidence used that same timezone.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256


REGISTRY_VERSION = (
    "calibration_station_market_timezone_binding_v1"
)


def _load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"evidence_not_mapping:{path}")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/bayes_basket_target_ssot.db"),
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path(
            "output/audits/"
            "bayes_basket_target_45station_truth_29d_direct_20260730_v1"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "output/audits/open_meteo_native_ensemble_oos/"
            "calibration_station_market_timezone_repair_latest.json"
        ),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    database = args.database.expanduser().resolve()
    evidence_root = args.evidence_root.expanduser().resolve()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS "
            "calibration_station_market_timezone_binding("
            "binding_id TEXT PRIMARY KEY,"
            "station_id TEXT NOT NULL UNIQUE REFERENCES "
            "calibration_station_registry(station_id),"
            "settlement_timezone TEXT NOT NULL,"
            "market_registry_timezone_count INTEGER NOT NULL CHECK("
            "market_registry_timezone_count=1),"
            "historical_truth_evidence_sha256 TEXT CHECK("
            "historical_truth_evidence_sha256 IS NULL OR "
            "length(historical_truth_evidence_sha256)=64),"
            "binding_version TEXT NOT NULL,"
            "bound_at_utc TEXT NOT NULL,"
            "content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64))"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS "
            "calibration_station_market_timezone_binding_no_update "
            "BEFORE UPDATE ON calibration_station_market_timezone_binding "
            "BEGIN SELECT RAISE(ABORT,'append_only'); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS "
            "calibration_station_market_timezone_binding_no_delete "
            "BEFORE DELETE ON calibration_station_market_timezone_binding "
            "BEGIN SELECT RAISE(ABORT,'append_only'); END"
        )
        rows = conn.execute(
            "WITH market_tz AS ("
            " SELECT station_id,MIN(settlement_timezone) timezone_name,"
            " COUNT(DISTINCT settlement_timezone) timezone_count"
            " FROM basket_target_market_registry "
            " WHERE settlement_timezone<>'' GROUP BY station_id"
            ") "
            "SELECT c.station_id,c.icao_code,c.city_label,c.timezone_name,"
            "c.source_lane,c.settlement_definition,c.registry_version,"
            "c.source_role,m.timezone_name,m.timezone_count,"
            "(SELECT COUNT(*) FROM calibration_station_daily_high_truth t "
            " WHERE t.station_id=c.station_id) "
            "FROM calibration_station_registry c JOIN market_tz m "
            "ON m.station_id=c.station_id "
            "WHERE c.timezone_name<>m.timezone_name "
            "ORDER BY c.station_id"
        ).fetchall()
        repairs: list[dict[str, Any]] = []
        for row in rows:
            station_id = str(row[0])
            market_timezone = str(row[8])
            if int(row[9]) != 1:
                raise RuntimeError(
                    f"market_timezone_not_unique:{station_id}"
                )
            truth_count = int(row[10])
            evidence_path = (
                evidence_root / station_id / "calibration_truth_dataset.json"
            )
            if truth_count:
                evidence = _load_json(evidence_path)
                if (
                    str(evidence.get("station_id") or "") != station_id
                    or str(evidence.get("settlement_timezone") or "")
                    != market_timezone
                    or not isinstance(evidence.get("samples"), list)
                    or not evidence["samples"]
                ):
                    raise RuntimeError(
                        f"historical_truth_timezone_evidence_invalid:"
                        f"{station_id}"
                    )
                evidence_sha = str(
                    evidence.get("content_sha256") or ""
                )
            else:
                evidence_sha = None
            bound_at_utc = "2026-07-30T17:45:00Z"
            identity = {
                "binding_id": (
                    "station-market-tz-"
                    + content_sha256({
                        "station_id": station_id,
                        "settlement_timezone": market_timezone,
                        "binding_version": REGISTRY_VERSION,
                    })[:24]
                ),
                "station_id": station_id,
                "settlement_timezone": market_timezone,
                "market_registry_timezone_count": 1,
                "historical_truth_evidence_sha256": evidence_sha,
                "binding_version": REGISTRY_VERSION,
                "bound_at_utc": bound_at_utc,
            }
            repairs.append({
                "station_id": station_id,
                "old_timezone": str(row[3]),
                "market_timezone": market_timezone,
                "truth_row_count": truth_count,
                "historical_truth_evidence_path": (
                    str(evidence_path) if truth_count else None
                ),
                "historical_truth_evidence_sha256": evidence_sha,
                "old_registry_version": str(row[6]),
                "binding_version": REGISTRY_VERSION,
                "new_content_sha256": content_sha256(identity),
                "_identity": identity,
            })

        if args.apply:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for repair in repairs:
                    identity = repair["_identity"]
                    conn.execute(
                        "INSERT OR IGNORE INTO "
                        "calibration_station_market_timezone_binding "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            identity["binding_id"],
                            identity["station_id"],
                            identity["settlement_timezone"],
                            identity["market_registry_timezone_count"],
                            identity[
                                "historical_truth_evidence_sha256"
                            ],
                            identity["binding_version"],
                            identity["bound_at_utc"],
                            repair["new_content_sha256"],
                        ),
                    )
                    existing = conn.execute(
                        "SELECT content_sha256 FROM "
                        "calibration_station_market_timezone_binding "
                        "WHERE station_id=?",
                        (identity["station_id"],),
                    ).fetchone()
                    if (
                        existing is None
                        or str(existing[0])
                        != repair["new_content_sha256"]
                    ):
                        raise RuntimeError(
                            "calibration_station_timezone_binding_conflict:"
                            + identity["station_id"]
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        remaining = conn.execute(
            "WITH market_tz AS ("
            " SELECT station_id,MIN(settlement_timezone) timezone_name,"
            " COUNT(DISTINCT settlement_timezone) timezone_count "
            " FROM basket_target_market_registry "
            " WHERE settlement_timezone<>'' GROUP BY station_id"
            ") SELECT COUNT(*) FROM calibration_station_registry c "
            "JOIN market_tz m USING(station_id) "
            "LEFT JOIN calibration_station_market_timezone_binding b "
            "USING(station_id) "
            "WHERE c.timezone_name<>m.timezone_name AND ("
            "b.station_id IS NULL OR b.settlement_timezone<>m.timezone_name "
            "OR b.market_registry_timezone_count<>1)"
        ).fetchone()[0]

    public_repairs = [
        {key: value for key, value in repair.items() if key != "_identity"}
        for repair in repairs
    ]
    receipt_identity = {
        "schema": "calibration_station_market_timezone_repair_v1",
        "database": str(database),
        "evidence_root": str(evidence_root),
        "applied": bool(args.apply),
        "repair_count": len(public_repairs),
        "remaining_mismatch_count": int(remaining),
        "repairs": public_repairs,
        "truth_rows_rewritten": 0,
        "orders_submitted": 0,
        "live_submit_authorized": False,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
    }
    receipt = {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "applied": receipt["applied"],
        "repair_count": receipt["repair_count"],
        "remaining_mismatch_count": receipt[
            "remaining_mismatch_count"
        ],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
