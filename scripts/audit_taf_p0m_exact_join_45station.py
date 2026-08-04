#!/usr/bin/env python3
"""Audit persisted AWC TAF/P0-M exact joins for all 45 canonical stations.

This is deliberately an offline scanner: it opens the target SQLite database
in query-only mode, reads already materialized P0 and exact-join artifacts,
and writes only the requested audit receipt.  It neither fetches TAF data nor
creates a revision, truth row, P0-M envelope, or order.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.taf_exact_join_audit import (  # noqa: E402
    MAX_EXACT_JOINS,
    audit_taf_p0m_exact_join_45station,
)
from bayes_basket_target.station_source_authority import (  # noqa: E402
    resolve_global_awc_source_authority,
)


MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def _path(value: Path, *, must_exist: bool = True) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise RuntimeError("taf_exact_join_audit_symlink_path_forbidden")
    return path.resolve(strict=must_exist)


def _read_connection(path: Path) -> sqlite3.Connection:
    database = resolve_target_database(path)
    if not database.is_file():
        raise RuntimeError("taf_exact_join_audit_database_missing")
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _rows(conn: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query).fetchall()]


def _station_registry(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        "SELECT station_id,timezone_name,settlement_definition "
        "FROM calibration_station_registry ORDER BY station_id",
    )
    # The immutable registry is an identity snapshot and intentionally still
    # contains retired AMSC labels for a few stations.  The append-only AWC
    # authority projection is the current formal source, so never regress to
    # the legacy registry field merely because this scanner is read-only.
    return [{
        **row,
        "source_lane": resolve_global_awc_source_authority(
            conn, station_id=str(row["station_id"])
        )["source_lane"],
    } for row in rows]


def _taf_capture_pointers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    forward = _rows(
        conn,
        "SELECT content_sha256,station_id,event_date,settlement_timezone,"
        "revision_id,revision_kind,provider_available_at_utc,"
        "source_object_sha256,source_class "
        "FROM calibration_forward_taf_capture",
    )
    historical = _rows(
        conn,
        "SELECT content_sha256,station_id,event_date,settlement_timezone,"
        "provider_available_at_utc,raw_taf_sha256,source_class "
        "FROM calibration_historical_taf_capture",
    )
    has_historical_quarantine = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ("calibration_historical_taf_correction_quarantine",),
    ).fetchone() is not None
    historical_corrections = (
        _rows(
            conn,
            "SELECT content_sha256,station_id,event_date,settlement_timezone,"
            "historical_taf_correction_quarantine_id AS revision_id,"
            "correction_kind AS revision_kind,raw_taf_sha256 AS source_object_sha256,"
            "source_class FROM calibration_historical_taf_correction_quarantine",
        )
        if has_historical_quarantine
        else []
    )
    return [
        {
            **row,
            "kind": "FORWARD",
        }
        for row in forward
    ] + [
        {
            **row,
            "kind": "HISTORICAL",
            "source_object_sha256": row.pop("raw_taf_sha256"),
            "revision_id": None,
            "revision_kind": None,
        }
        for row in historical
    ] + [
        {
            **row,
            "kind": "HISTORICAL_QUARANTINE",
            "provider_available_at_utc": "",
        }
        for row in historical_corrections
    ]


def _truth_pointers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    # Current accepted revision only: accepting an old truth id after a later
    # official correction would silently revive an invalid exact-join sample.
    return _rows(
        conn,
        "SELECT t.truth_id,t.station_id,t.local_date AS event_date,"
        "t.settlement_definition,t.source_lane,t.qc_status,t.authority_scope,"
        "t.source_payload_sha256,1 AS latest_accepted "
        "FROM calibration_station_daily_high_truth t "
        "WHERE t.source_lane='AWC' AND t.qc_status='ACCEPTED' AND NOT EXISTS ("
        "SELECT 1 FROM calibration_station_daily_high_truth newer "
        "WHERE newer.station_id=t.station_id "
        "AND newer.local_date=t.local_date "
        "AND newer.settlement_definition=t.settlement_definition "
        "AND newer.source_lane='AWC' "
        "AND newer.qc_status='ACCEPTED' "
        "AND newer.revision_no>t.revision_no"
        ")",
    )


def _exact_joins(directory: Path) -> list[tuple[Path, Mapping[str, Any]]]:
    if not directory.is_dir():
        raise RuntimeError("taf_exact_join_audit_exact_join_dir_invalid")
    sources = [
        _path(path)
        for path in sorted(directory.glob("*_taf_native_exact_join.json"))
        if path.is_file()
    ]
    if len(sources) > MAX_EXACT_JOINS:
        raise RuntimeError("taf_exact_join_audit_exact_join_count_exceeded")
    result: list[tuple[Path, Mapping[str, Any]]] = []
    for source in sources:
        payload = load_bounded_json(source, limit=MAX_INPUT_BYTES)
        if not isinstance(payload, Mapping):
            raise RuntimeError("taf_exact_join_audit_exact_join_mapping_required")
        result.append((source, payload))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--p0-inbox", type=Path, required=True)
    parser.add_argument("--exact-join-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    db_path = _path(args.db)
    p0_inbox_path = _path(args.p0_inbox)
    exact_join_dir = _path(args.exact_join_dir)
    output_path = _path(args.output, must_exist=False)
    if output_path in {db_path, p0_inbox_path} or output_path.parent == exact_join_dir:
        raise RuntimeError("taf_exact_join_audit_output_path_collision")
    p0_inbox = load_bounded_json(p0_inbox_path, limit=MAX_INPUT_BYTES)
    if not isinstance(p0_inbox, Mapping):
        raise RuntimeError("taf_exact_join_audit_p0_inbox_mapping_required")
    conn = _read_connection(db_path)
    try:
        result = audit_taf_p0m_exact_join_45station(
            station_registry=_station_registry(conn),
            p0_inbox=p0_inbox,
            p0_inbox_path=p0_inbox_path,
            exact_joins=_exact_joins(exact_join_dir),
            taf_capture_pointers=_taf_capture_pointers(conn),
            truth_pointers=_truth_pointers(conn),
        )
    finally:
        conn.close()
    wrote = write_bounded_json_if_changed(output_path, result, limit=MAX_OUTPUT_BYTES)
    print(json.dumps({
        "state": result["state"],
        "canonical_station_count": result["canonical_station_count"],
        "exact_join_artifact_count": result["exact_join_artifact_count"],
        "current_p0_station_count": result["current_p0_station_count"],
        "minimum_taf_native_oos_sample_n": result["minimum_taf_native_oos_sample_n"],
        "blocker_counts": result["blocker_counts"],
        "output": str(output_path),
        "wrote": wrote,
        "network_fetches": 0,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "EXACT_OOS_GATE_MET_UNPROMOTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
