#!/usr/bin/env python3
"""Build TAF exact joins from persisted strict historical P0 predictions.

The resident P0 inbox is a live/current contract and intentionally contains
only the current station set.  Historical OOS predictions are already
persisted in SSOT, so using that inbox to discover scopes silently drops
valid historical P0 roots.  This read-only repair enumerates the persisted
official native scopes directly, reuses the archived bucket contract, and
reruns the existing exact-join builder.  It never fetches TAF, writes SSOT,
materializes P0-M, or calls the order gateway.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.native_ensemble_training_facts import (  # noqa: E402
    build_taf_native_exact_join,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_OUTPUT_BYTES,
    write_bounded_json_if_changed,
)


MAX_STATIONS = expected_station_count()
MAX_SCOPES = 256
OFFICIAL_SYSTEMS = frozenset({
    "ECMWF_IFS_ENS_OPEN_PF50",
    "NOAA_GEFS_05",
    "ECCC_GEM_GLOBAL_ENS",
})
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _filename(*parts: str) -> str:
    return "_".join(
        _SAFE.sub("-", str(part).strip())
        for part in parts
        if str(part).strip()
    ) + "_taf_native_exact_join.json"


def _json_value(value: Any, *, name: str) -> Any:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"historical_taf_persisted_{name}_invalid") from exc
    return parsed


def _json_object(value: Any, *, name: str) -> dict[str, Any]:
    parsed = _json_value(value, name=name)
    if not isinstance(parsed, dict):
        raise ContractError(f"historical_taf_persisted_{name}_mapping_required")
    return parsed


def _scopes(conn: sqlite3.Connection, requested: set[str]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT DISTINCT p.station_id,p.season_bin,p.lead_bucket,p.run_cycle,"
        "p.settlement_definition,p.settlement_timezone,p.ensemble_system_ids_json,"
        "p.independence_family_ids_json,p.family_weight_caps_json "
        "FROM calibration_cross_native_ensemble_oos_prediction p "
        "JOIN calibration_native_ensemble_activation a "
        "ON a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "AND a.artifact_id=p.cross_ensemble_artifact_id "
        "AND a.station_id=p.station_id AND a.season_bin=p.season_bin "
        "AND a.lead_bucket=p.lead_bucket AND a.run_cycle=p.run_cycle "
        "WHERE p.prediction_oos=1 "
        "AND p.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "ORDER BY p.station_id,p.season_bin,p.lead_bucket,p.run_cycle,"
        "p.ensemble_system_ids_json"
    ).fetchall()
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        station = str(row[0]).strip().upper()
        if requested and station not in requested:
            continue
        systems_raw = _json_value(row[6], name="systems")
        if not isinstance(systems_raw, list):
            raise ContractError("historical_taf_persisted_systems_list_required")
        systems = [str(v) for v in systems_raw]
        # This repair is for the official native historical chain.  Explicit
        # Open-Meteo contracts remain separately auditable fallbacks and are
        # not silently mixed into the native TAF calibration join.
        if not systems or not set(systems).issubset(OFFICIAL_SYSTEMS):
            continue
        scope_key = (
            station,
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[6]),
        )
        if scope_key in seen:
            continue
        seen.add(scope_key)
        families_raw = _json_value(row[7], name="families")
        if not isinstance(families_raw, list):
            raise ContractError("historical_taf_persisted_families_list_required")
        result.append({
            "station_id": station,
            "season_bin": str(row[1]),
            "lead_bucket": str(row[2]),
            "run_cycle": str(row[3]),
            "settlement_definition": str(row[4]),
            "settlement_timezone": str(row[5]),
            "ensemble_system_ids": systems,
            "independence_family_ids": [str(v) for v in families_raw],
            "family_weight_caps": {
                str(k): float(v)
                for k, v in _json_object(row[8], name="caps").items()
            },
        })
    if len({row["station_id"] for row in result}) > MAX_STATIONS:
        raise ContractError("historical_taf_persisted_station_limit_exceeded")
    if len(result) > MAX_SCOPES:
        raise ContractError("historical_taf_persisted_scope_limit_exceeded")
    # The downstream station audit accepts one exact-join document per
    # station.  Prefer the stable official 18Z pair when present; fall back
    # to a mixed-cycle/triple native scope only when that is the only
    # persisted official candidate for the station.
    rank = {
        "18Z": 0,
        "MIXED_NATIVE_CYCLES": 1,
        "00Z": 2,
        "06Z": 3,
        "12Z": 4,
    }
    selected: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    for row in result:
        station = row["station_id"]
        current = selected.get(station)
        key = (
            rank.get(row["run_cycle"], 9),
            -len(row["ensemble_system_ids"]),
            row["lead_bucket"],
            row["run_cycle"],
            row["ensemble_system_ids"],
        )
        if current is None or key < current[0]:
            selected[station] = (key, row)
    return [value[1] for value in selected.values()]


def _bucket_intervals(
    conn: sqlite3.Connection,
    *,
    station: str,
    lead_bucket: str,
    system_id: str,
) -> dict[str, tuple[float | None, float | None]]:
    row = conn.execute(
        "SELECT bucket_intervals_json,bucket_contract_sha256 "
        "FROM calibration_native_ensemble_historical_member_bucket "
        "WHERE station_id=? AND ensemble_system_id=? "
        "AND archive_lead_bucket=? ORDER BY target_local_date LIMIT 1",
        (station, system_id, lead_bucket),
    ).fetchone()
    if row is None:
        raise ContractError("historical_taf_persisted_bucket_contract_missing")
    intervals = _json_object(row[0], name="bucket_intervals")
    normalized = {
        str(bucket): (
            None if bounds[0] is None else float(bounds[0]),
            None if bounds[1] is None else float(bounds[1]),
        )
        for bucket, bounds in intervals.items()
        if isinstance(bounds, list) and len(bounds) == 2
    }
    if len(normalized) != len(intervals):
        raise ContractError("historical_taf_persisted_bucket_intervals_invalid")
    if content_sha256({
        bucket: list(bounds) for bucket, bounds in normalized.items()
    }) != str(row[1]):
        raise ContractError("historical_taf_persisted_bucket_contract_hash_mismatch")
    return normalized


def build(
    *,
    db_path: Path,
    output_dir: Path,
    requested_stations: set[str],
) -> dict[str, Any]:
    database = resolve_target_database(db_path)
    if database.is_symlink() or not database.is_file():
        raise ContractError("historical_taf_persisted_database_invalid")
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    entries: list[dict[str, Any]] = []
    try:
        scopes = _scopes(conn, requested_stations)
        for scope in scopes:
            intervals = _bucket_intervals(
                conn,
                station=scope["station_id"],
                lead_bucket=scope["lead_bucket"],
                system_id=scope["ensemble_system_ids"][0],
            )
            exact_join = build_taf_native_exact_join(
                conn,
                **scope,
                bucket_intervals=intervals,
            )
            destination = output_dir / _filename(
                scope["station_id"],
                scope["season_bin"],
                scope["lead_bucket"],
                scope["run_cycle"],
            )
            wrote = write_bounded_json_if_changed(
                destination,
                exact_join,
                limit=MAX_JSON_OUTPUT_BYTES,
            )
            entries.append({
                "station_id": scope["station_id"],
                "season_bin": scope["season_bin"],
                "lead_bucket": scope["lead_bucket"],
                "run_cycle": scope["run_cycle"],
                "ensemble_system_ids": scope["ensemble_system_ids"],
                "artifact_path": str(destination),
                "artifact_content_sha256": exact_join["content_sha256"],
                "sample_count": int(exact_join["sample_count"]),
                "excluded_correction_sample_count": int(
                    exact_join.get("excluded_correction_sample_count", 0)
                ),
                "wrote": wrote,
            })
    finally:
        conn.close()
    identity = {
        "schema": "persisted_native_taf_exact_join_rebuild_v1",
        "state": "REBUILT_READ_ONLY",
        "oos_data_policy": "HISTORICAL_CLOSED_DAY_ONLY",
        "future_event_count": 0,
        "official_native_systems": sorted(OFFICIAL_SYSTEMS),
        "station_count": len({row["station_id"] for row in entries}),
        "scope_count": len(entries),
        "entries": entries,
        "boundary": {
            "read_only": True,
            "network_fetches": 0,
            "writes_database": False,
            "production_p0m_materialized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "preserves_taf_24_sample_gate": True,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--station-id", action="append", default=[])
    args = parser.parse_args()
    result = build(
        db_path=args.db.expanduser().resolve(strict=True),
        output_dir=args.output_dir.expanduser().resolve(),
        requested_stations={
            str(value).strip().upper()
            for value in args.station_id
            if str(value).strip()
        },
    )
    manifest_path = args.output_dir.expanduser().resolve() / "manifest.json"
    wrote = write_bounded_json_if_changed(
        manifest_path,
        result,
        limit=MAX_JSON_OUTPUT_BYTES,
    )
    print(json.dumps({
        "state": result["state"],
        "station_count": result["station_count"],
        "scope_count": result["scope_count"],
        "sample_count_total": sum(
            int(row["sample_count"]) for row in result["entries"]
        ),
        "output_dir": str(args.output_dir.expanduser().resolve()),
        "manifest": str(manifest_path),
        "wrote": wrote,
        "network_fetches": 0,
        "writes_database": False,
        "production_p0m_materialized": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
