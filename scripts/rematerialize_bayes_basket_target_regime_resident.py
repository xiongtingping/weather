#!/usr/bin/env python3
"""Controlled regime SSOT rematerialization; dry-run unless explicitly applied."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.production_io import load_materialized_probability_envelopes  # noqa: E402
from bayes_basket_target.regime_materialization import (  # noqa: E402
    MAX_RESIDENT_PROBABILITY_ENVELOPES,
    MAX_RESIDENT_REGIME_STATIONS,
    rematerialize_resident_regimes,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_target_db_capacity,
    write_bounded_json,
)
from scripts.audit_bayes_basket_target_regime_resident import (  # noqa: E402
    _registry_from_resident,
)


MAX_RECEIPT_BYTES = 1024 * 1024


def _active_scopes(
    conn: sqlite3.Connection,
    *,
    evaluation_at_utc: str,
) -> list[tuple[str, str]]:
    rows = conn.execute(
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
    scopes = [(str(row[0]), str(row[1])) for row in rows]
    if not scopes:
        raise ContractError("regime_rematerialization_active_scope_missing")
    if len(scopes) > MAX_RESIDENT_REGIME_STATIONS:
        raise ContractError("resident_regime_station_limit_exceeded")
    if len({station for station, _ in scopes}) != len(scopes):
        raise ContractError("resident_regime_station_event_date_not_unique")
    return scopes


def _assert_production_no_live_activation(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT state,activation_json FROM basket_strategy_activation_state "
        "WHERE singleton=1"
    ).fetchone()
    if row is None or str(row[0]) != "enabled":
        raise ContractError("regime_rematerialization_activation_missing")
    try:
        activation = json.loads(str(row[1]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(
            "regime_rematerialization_activation_invalid"
        ) from exc
    required_false = (
        "calls_order_gateway",
        "live_order_authorized",
        "live_submit_authorized",
        "places_orders",
    )
    if (
        activation.get("execution_mode") != "production_no_live"
        or activation.get("state") != "production_no_live"
        or any(activation.get(key) is not False for key in required_false)
    ):
        raise ContractError(
            "regime_rematerialization_production_no_live_required"
        )


def build_plan(
    conn: sqlite3.Connection,
    *,
    evaluation_at_utc: str,
    created_at_utc: str,
    apply_production_no_live: bool,
) -> dict[str, Any]:
    scopes = _active_scopes(
        conn,
        evaluation_at_utc=evaluation_at_utc,
    )
    registry = _registry_from_resident(conn, scopes)
    total_envelopes = 0
    for station_id, event_date in scopes:
        total_envelopes += int(
            conn.execute(
                "SELECT COUNT(*) FROM basket_probability_envelope "
                "WHERE station_id=? AND event_date=? AND state='COMMITTED' "
                "AND decision_at_utc<=?",
                (station_id, event_date, evaluation_at_utc),
            ).fetchone()[0]
        )
        if total_envelopes > MAX_RESIDENT_PROBABILITY_ENVELOPES:
            raise ContractError("resident_regime_envelope_limit_exceeded")
    envelopes = []
    for station_id, event_date in scopes:
        envelopes.extend(
            load_materialized_probability_envelopes(
                conn,
                station_id=station_id,
                event_date=event_date,
                evaluation_at_utc=evaluation_at_utc,
            )
        )
    if apply_production_no_live:
        _assert_production_no_live_activation(conn)
    return rematerialize_resident_regimes(
        conn,
        envelopes=envelopes,
        registry_timezones=registry,
        evaluation_at_utc=evaluation_at_utc,
        created_at_utc=created_at_utc,
        apply_production_no_live=apply_production_no_live,
    )


def _blocked_receipt(
    *,
    evaluation_at_utc: str,
    apply_production_no_live: bool,
    error: Exception,
) -> dict[str, Any]:
    identity = {
        "schema": "bayes_basket_target_regime_rematerialization_receipt_v1",
        "mode": (
            "APPLY_PRODUCTION_NO_LIVE"
            if apply_production_no_live
            else "DRY_RUN"
        ),
        "status": "BLOCKED",
        "evaluation_at_utc": evaluation_at_utc,
        "blockers": [f"{type(error).__name__}:{error}"],
        "boundary": {
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--evaluation-at-utc", required=True)
    parser.add_argument("--created-at-utc")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--apply-production-no-live",
        action="store_true",
        help="atomically supersede and insert; otherwise read-only dry-run",
    )
    args = parser.parse_args()
    created_at = args.created_at_utc or args.evaluation_at_utc
    db = resolve_target_database(args.db)
    if args.apply_production_no_live:
        assert_target_db_capacity(db)
    mode = "rw" if args.apply_production_no_live else "ro"
    try:
        with sqlite3.connect(f"file:{db}?mode={mode}", uri=True) as conn:
            if args.apply_production_no_live:
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("PRAGMA query_only=ON")
            try:
                receipt = build_plan(
                    conn,
                    evaluation_at_utc=args.evaluation_at_utc,
                    created_at_utc=created_at,
                    apply_production_no_live=args.apply_production_no_live,
                )
                if args.apply_production_no_live:
                    conn.commit()
            except Exception:
                if args.apply_production_no_live:
                    conn.rollback()
                raise
    except (ContractError, sqlite3.Error, ValueError, OSError) as exc:
        receipt = _blocked_receipt(
            evaluation_at_utc=args.evaluation_at_utc,
            apply_production_no_live=args.apply_production_no_live,
            error=exc,
        )
    write_bounded_json(args.output, receipt, limit=MAX_RECEIPT_BYTES)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
