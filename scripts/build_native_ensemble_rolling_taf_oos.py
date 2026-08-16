#!/usr/bin/env python3
"""Build bounded rolling native OOS + AWC TAF exact joins, offline only."""

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
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json_if_changed  # noqa: E402
from bayes_basket_target.rolling_native_taf_oos import (  # noqa: E402
    build_rolling_origin_native_oos,
    build_rolling_taf_exact_joins,
    split_rolling_taf_exact_join,
)
from bayes_basket_target.taf_native_ensemble_oos import (  # noqa: E402
    MIN_SAMPLE_N as NATIVE_TRANSPORT_MIN_SAMPLE_N,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    stations = sorted({str(value).strip().upper() for value in args.station_id if str(value).strip()})
    if not stations or len(stations) > 45:
        raise RuntimeError("rolling_native_taf_station_scope_invalid")
    database = args.db.expanduser().resolve(strict=True)
    destination = args.output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    as_of = _now()
    reports = []
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        for station in stations:
            rolling = build_rolling_origin_native_oos(
                conn,
                station_id=station,
                as_of_utc=as_of,
            )
            skipped_scopes: list[dict] = []
            joins = (
                build_rolling_taf_exact_joins(
                    conn,
                    rolling,
                    skipped_scopes=skipped_scopes,
                )
                if rolling["state"] == "READY"
                else []
            )
            rolling_path = destination / f"{station}_rolling_native_oos.json"
            write_bounded_json_if_changed(rolling_path, rolling)
            join_paths = []
            splits = []
            for index, join in enumerate(joins):
                target = destination / f"{station}_rolling_taf_exact_join_{index:02d}.json"
                write_bounded_json_if_changed(target, join)
                join_paths.append(str(target))
                # A fitted artifact can never be qualified on its own rows, so
                # emit the strictly-later transport window alongside the fit
                # window rather than leaving the caller to cut the series.
                # Aim the transport window at the size the native ensemble
                # gate scores on, as a preference rather than a floor.  The
                # cut used to maximise the fit window against the generic
                # floor of 6, so the OOS block stayed pinned at 6 however long
                # the series ran and a gate wanting more could never be
                # satisfied by waiting.  As a preference it grows the OOS
                # block when the series can afford it and still emits a split
                # when it cannot -- sample count shapes the evidence, it does
                # not withhold the artifact.
                split = split_rolling_taf_exact_join(
                    join,
                    preferred_transport_sample_n=(
                        NATIVE_TRANSPORT_MIN_SAMPLE_N
                    ),
                )
                entry = {
                    key: value
                    for key, value in split.items()
                    if key not in {"fit", "transport"}
                }
                if split["state"] == "READY":
                    fit_path = destination / f"{station}_rolling_taf_fit_{index:02d}.json"
                    transport_path = (
                        destination / f"{station}_rolling_taf_transport_{index:02d}.json"
                    )
                    write_bounded_json_if_changed(fit_path, split["fit"])
                    write_bounded_json_if_changed(transport_path, split["transport"])
                    entry["fit_output"] = str(fit_path)
                    entry["transport_output"] = str(transport_path)
                splits.append(entry)
            reports.append({
                "station_id": station,
                "state": rolling["state"],
                "candidate_count": len(rolling["candidates"]),
                "accumulating_count": len(rolling["accumulating"]),
                "taf_exact_join_count": len(joins),
                "taf_exact_join_sample_count": sum(int(join["sample_count"]) for join in joins),
                "rolling_output": str(rolling_path),
                "exact_join_outputs": join_paths,
                "strict_split_ready_count": sum(
                    item["state"] == "READY" for item in splits
                ),
                "strict_splits": splits,
                "skipped_scope_count": len(skipped_scopes),
                "skipped_scopes": skipped_scopes,
            })
    identity = {
        "schema": "native_ensemble_rolling_taf_oos_build_manifest_v1",
        "as_of_utc": as_of,
        "station_count": len(stations),
        "reports": reports,
        "boundary": {
            "offline_only": True,
            "writes_target_database": False,
            "production_p0_materialized": False,
            "production_p0m_materialized": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    manifest = {**identity, "content_sha256": content_sha256(identity)}
    manifest_path = destination / "manifest.json"
    write_bounded_json_if_changed(manifest_path, manifest)
    print(json.dumps({
        "output": str(manifest_path),
        "station_count": len(stations),
        "ready_station_count": sum(item["state"] == "READY" for item in reports),
        "taf_exact_join_sample_count": sum(item["taf_exact_join_sample_count"] for item in reports),
        "strict_split_ready_count": sum(
            item["strict_split_ready_count"] for item in reports
        ),
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if all(item["state"] == "READY" for item in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
