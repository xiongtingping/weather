#!/usr/bin/env python3
"""Bounded, read-only audit of the resident regime SSOT."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.market_registry import SNAPSHOT_TABLE, TABLE  # noqa: E402
from bayes_basket_target.production_io import load_materialized_probability_envelopes  # noqa: E402
from bayes_basket_target.regime_artifact import resolve_regime_artifact  # noqa: E402
from bayes_basket_target.regime_materialization import (  # noqa: E402
    MAX_RESIDENT_REGIME_STATIONS,
    build_regime_artifact,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.stages import select_current_stage  # noqa: E402


MAX_RECEIPT_BYTES = 1024 * 1024


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _registry_from_resident(
    conn: sqlite3.Connection,
    scopes: list[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    if not _table_exists(conn, SNAPSHOT_TABLE) or not _table_exists(conn, TABLE):
        raise ContractError("regime_audit_registry_table_missing")
    owner = conn.execute(
        f"SELECT snapshot_sha256 FROM {SNAPSHOT_TABLE} "
        "ORDER BY ingested_at_utc DESC,snapshot_sha256 DESC LIMIT 1"
    ).fetchone()
    if owner is None:
        raise ContractError("regime_audit_registry_snapshot_not_unique")
    snapshot_sha = str(owner[0])
    result: dict[tuple[str, str], str] = {}
    for scope in scopes:
        rows = conn.execute(
            f"SELECT DISTINCT settlement_timezone FROM {TABLE} "
            "WHERE snapshot_sha256=? AND station_id=? AND event_date=? "
            "AND source_eligible=1 AND market_active=1 "
            "AND market_accepting_orders=1 LIMIT 2",
            (snapshot_sha, *scope),
        ).fetchall()
        if len(rows) != 1 or not str(rows[0][0] or "").strip():
            raise ContractError("regime_audit_registry_timezone_not_unique")
        result[scope] = str(rows[0][0]).strip()
    return result


def audit_regime_resident(
    conn: sqlite3.Connection,
    *,
    evaluation_at_utc: str,
    registry_timezones: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any]:
    """Audit selected probability stages against exact committed regimes."""

    conn.execute("PRAGMA query_only=ON")
    required = {"basket_probability_envelope", "basket_regime_artifact"}
    missing = sorted(name for name in required if not _table_exists(conn, name))
    blockers: list[str] = []
    selections: list[dict[str, Any]] = []
    if missing:
        blockers.extend(f"table_missing:{name}" for name in missing)
    else:
        scope_rows = conn.execute(
            "SELECT DISTINCT station_id,event_date "
            "FROM basket_probability_envelope WHERE state='COMMITTED' "
            "AND decision_at_utc<=? AND fresh_until_utc>? "
            "ORDER BY station_id,event_date LIMIT ?",
            (
                evaluation_at_utc,
                evaluation_at_utc,
                MAX_RESIDENT_REGIME_STATIONS + 1,
            ),
        ).fetchall()
        scopes = [(str(row[0]), str(row[1])) for row in scope_rows]
        if not scopes:
            blockers.append("committed_probability_scope_missing")
        elif len(scopes) > MAX_RESIDENT_REGIME_STATIONS:
            blockers.append("resident_regime_station_limit_exceeded")
        elif len({station for station, _ in scopes}) != len(scopes):
            blockers.append("resident_regime_station_event_date_not_unique")
        else:
            try:
                registry = (
                    dict(registry_timezones)
                    if registry_timezones is not None
                    else _registry_from_resident(conn, scopes)
                )
                if set(registry) != set(scopes):
                    raise ContractError("regime_audit_registry_scope_mismatch")
                for scope in scopes:
                    envelopes = load_materialized_probability_envelopes(
                        conn,
                        station_id=scope[0],
                        event_date=scope[1],
                        evaluation_at_utc=evaluation_at_utc,
                    )
                    selected = select_current_stage(
                        envelopes,
                        at_utc=evaluation_at_utc,
                    )
                    if selected.settlement_timezone != str(registry[scope]):
                        raise ContractError(
                            "resident_regime_settlement_timezone_mismatch"
                        )
                    expected = build_regime_artifact(selected)
                    regime_rows = conn.execute(
                        "SELECT artifact_json FROM basket_regime_artifact "
                        "WHERE station_id=? AND event_date=? AND stage=? "
                        "AND checkpoint_id=? AND probability_sha256=? "
                        "AND state='COMMITTED' LIMIT 2",
                        (
                            selected.station_id,
                            selected.event_date,
                            selected.stage,
                            selected.checkpoint_id,
                            selected.content_sha256,
                        ),
                    ).fetchall()
                    if len(regime_rows) != 1:
                        raise ContractError("regime_ssot_not_unique")
                    actual = resolve_regime_artifact(
                        json.loads(str(regime_rows[0][0])),
                        station_id=selected.station_id,
                        event_date=selected.event_date,
                        stage=selected.stage,
                        checkpoint_id=selected.checkpoint_id,
                        settlement_timezone=selected.settlement_timezone,
                        probability_sha256=selected.content_sha256,
                        evaluation_at_utc=evaluation_at_utc,
                    )
                    if actual.content_sha256 != expected.content_sha256:
                        raise ContractError("regime_resident_derivation_drift")
                    selections.append(
                        {
                            "station_id": selected.station_id,
                            "event_date": selected.event_date,
                            "stage": selected.stage,
                            "checkpoint_id": selected.checkpoint_id,
                            "settlement_timezone": selected.settlement_timezone,
                            "probability_sha256": selected.content_sha256,
                            "regime_sha256": actual.content_sha256,
                        }
                    )
            except (ContractError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
                blockers.append(f"{type(exc).__name__}:{exc}")

    identity = {
        "schema": "bayes_basket_target_regime_resident_audit_v1",
        "evaluation_at_utc": evaluation_at_utc,
        "status": "PASS" if not blockers else "BLOCKED",
        "station_count": len(selections),
        "max_station_count": MAX_RESIDENT_REGIME_STATIONS,
        "selections": selections,
        "blockers": sorted(set(blockers)),
        "boundary": {
            "read_only": True,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "receipt_max_bytes": MAX_RECEIPT_BYTES,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--evaluation-at-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    db = resolve_target_database(args.db)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        receipt = audit_regime_resident(
            conn,
            evaluation_at_utc=args.evaluation_at_utc,
        )
    write_bounded_json(args.output, receipt, limit=MAX_RECEIPT_BYTES)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
