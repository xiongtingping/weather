#!/usr/bin/env python3
"""Build bounded strict-forward native-ensemble OOS readiness reports."""

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

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    DEFAULT_MIN_HOLDOUT_DAYS,
    DEFAULT_MIN_TRAIN_DAYS,
    build_native_ensemble_oos,
    persist_native_ensemble_oos,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    write_bounded_json_if_changed,
)


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
MAX_STATIONS = 45


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument(
        "--min-train-days",
        type=int,
        default=DEFAULT_MIN_TRAIN_DAYS,
    )
    parser.add_argument(
        "--min-holdout-days",
        type=int,
        default=DEFAULT_MIN_HOLDOUT_DAYS,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Atomically persist passing OOS artifacts and no-live "
            "activations after the read-only build."
        ),
    )
    args = parser.parse_args()
    db = args.db.expanduser().resolve(strict=True)
    if db.is_symlink():
        raise RuntimeError("native_ensemble_oos_db_symlink_forbidden")
    as_of_utc = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        requested = sorted({
            str(value).strip().upper()
            for value in args.station_id
            if str(value).strip()
        })
        stations = requested or [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT station_id "
                "FROM basket_target_market_registry "
                "WHERE station_id IS NOT NULL AND station_id<>'' "
                "ORDER BY station_id LIMIT ?",
                (MAX_STATIONS,),
            )
        ]
        if not stations or len(stations) > MAX_STATIONS:
            raise RuntimeError(
                "native_ensemble_oos_station_scope_invalid"
            )
        reports = [
            build_native_ensemble_oos(
                conn,
                station_id=station,
                as_of_utc=as_of_utc,
                min_train_days=args.min_train_days,
                min_holdout_days=args.min_holdout_days,
            )
            for station in stations
        ]
    identity = {
        "schema": "native_ensemble_forward_oos_manifest_v1",
        "database": str(db),
        "as_of_utc": as_of_utc,
        "station_count": len(stations),
        "min_train_days": args.min_train_days,
        "min_holdout_days": args.min_holdout_days,
        "reports": reports,
        "production_no_live": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    manifest = {
        **identity,
        "content_sha256": content_sha256(identity),
    }
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, manifest)
    persist_receipts = []
    if args.commit:
        with sqlite3.connect(db, timeout=60.0) as conn:
            conn.execute("PRAGMA busy_timeout=60000")
            assert_connection_target_capacity(
                conn,
                recover_oversized_wal=True,
            )
            if str(conn.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]).lower() != "ok":
                raise RuntimeError(
                    "native_ensemble_oos_database_quick_check_failed"
                )
            conn.execute("BEGIN IMMEDIATE")
            try:
                persist_receipts = [
                    persist_native_ensemble_oos(
                        conn,
                        report,
                        activated_at_utc=as_of_utc,
                    )
                    for report in reports
                ]
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    print(json.dumps({
        "output": str(output),
        "wrote": wrote,
        "station_count": len(stations),
        "cross_candidate_count": sum(
            len(report["cross_candidates"]) for report in reports
        ),
        "oos_pass_count": sum(
            candidate["state"] == "PRODUCTION_NO_LIVE_OOS_PASS"
            for report in reports
            for candidate in report["cross_candidates"]
        ),
        "committed": bool(args.commit),
        "persisted_counts": {
            key: sum(
                int(receipt["counts"][key])
                for receipt in persist_receipts
            )
            for key in (
                "system_artifacts",
                "selection_evidence",
                "cross_artifacts",
                "oos_predictions",
                "activations",
            )
        },
        "content_sha256": manifest["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
