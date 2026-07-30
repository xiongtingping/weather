#!/usr/bin/env python3
"""Materialize explicit official-WS market evidence into the target SSOT.

This command is intentionally not a daemon and never performs network I/O.
It validates the official WS manifest plus three explicit auxiliary artifacts;
without ``--commit`` it is a read-only preflight.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.market_ingress import materialize_market_evidence
from bayes_basket_target.activation import load_target_activation
from bayes_basket_target.quote_freshness import QuoteFreshnessPolicy
from bayes_basket_target.inventory_producer import (
    build_confirmed_inventory_ingress,
    build_no_live_inventory_ingress,
)
from bayes_basket_target.order_state_producer import build_no_live_order_state_ingress
from bayes_basket_target.ssot import production_ssot_schema_status
from bayes_basket_target.database import (
    DEFAULT_TARGET_DB,
    DEFAULT_TARGET_WS_MANIFEST,
    resolve_target_database,
    resolve_target_ws_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_TARGET_WS_MANIFEST)
    parser.add_argument("--inventory-json", type=Path)
    parser.add_argument("--order-state-json", type=Path)
    parser.add_argument("--cash-phase-json", type=Path, required=True)
    parser.add_argument("--official-positions-manifest", type=Path)
    parser.add_argument(
        "--no-live-q0",
        action="store_true",
        help="Use target-owned q=0 inventory; mutually exclusive with live positions",
    )
    parser.add_argument(
        "--observed-at-utc",
        help="Required with --no-live-q0; one explicit as-of for q=0 and order-state",
    )
    parser.add_argument("--remaining-budget-usdc", type=float)
    parser.add_argument("--activation", type=Path)
    parser.add_argument("--station-id", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    conn = None
    try:
        if args.activation is None:
            raise RuntimeError(
                "content_addressed_quote_freshness_activation_required"
            )
        activation = load_target_activation(args.activation)
        quote_freshness_policy = QuoteFreshnessPolicy.from_mapping(
            activation["quote_freshness_policy"]
        )
        db_path = resolve_target_database(args.db)
        manifest_path = resolve_target_ws_manifest(args.manifest)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        status = production_ssot_schema_status(conn)
        if not status["ok"]:
            raise RuntimeError("production_ssot_schema_incomplete")
        inventory_payload = None
        order_state_payload = None
        inventory_path = args.inventory_json
        order_state_path = args.order_state_json
        if args.no_live_q0 and (
            args.official_positions_manifest is not None
            or args.inventory_json is not None
            or args.order_state_json is not None
        ):
            raise RuntimeError("no_live_q0_and_explicit_inventory_inputs_are_mutually_exclusive")
        if args.no_live_q0:
            if args.remaining_budget_usdc is None:
                raise RuntimeError("remaining_budget_usdc_required_with_no_live_q0")
            if args.activation is None:
                raise RuntimeError("activation_required_with_no_live_q0")
            if not str(args.observed_at_utc or "").strip():
                raise RuntimeError("observed_at_utc_required_with_no_live_q0")
            # This is target-owned q=0 state.  It never reads the resident
            # positions manifest or any shared execution table.  The order
            # producer below still binds the target activation and target
            # ledger quiescence before emitting known-zero state.
            inventory_payload = build_no_live_inventory_ingress(
                conn,
                remaining_budget_usdc=args.remaining_budget_usdc,
                observed_at_utc=str(args.observed_at_utc),
                station_id=str(args.station_id or "").strip(),
                event_date=str(args.event_date or "").strip(),
            )
            order_state_payload = build_no_live_order_state_ingress(
                conn,
                activation_path=args.activation,
                observed_at_utc=str(args.observed_at_utc),
                station_id=str(args.station_id or "").strip(),
                event_date=str(args.event_date or "").strip(),
            )
            inventory_path = None
            order_state_path = None
        if args.official_positions_manifest is not None:
            if args.remaining_budget_usdc is None:
                raise RuntimeError("remaining_budget_usdc_required_with_official_positions_manifest")
            if args.activation is None:
                raise RuntimeError("activation_required_with_official_positions_manifest")
            inventory_payload = build_confirmed_inventory_ingress(
                conn,
                manifest_path=args.official_positions_manifest,
                remaining_budget_usdc=args.remaining_budget_usdc,
                station_id=str(args.station_id or "").strip(),
                event_date=str(args.event_date or "").strip(),
            )
            order_state_payload = build_no_live_order_state_ingress(
                conn,
                activation_path=args.activation,
                observed_at_utc=str(inventory_payload["observed_at"]),
                station_id=str(args.station_id or "").strip(),
                event_date=str(args.event_date or "").strip(),
            )
            inventory_path = None
            order_state_path = None
        if order_state_path is None and order_state_payload is None:
            raise RuntimeError("order_state_input_required")
        result = materialize_market_evidence(
            conn,
            manifest_path=manifest_path,
            inventory_path=inventory_path,
            order_state_path=order_state_path,
            cash_phase_path=args.cash_phase_json,
            inventory_payload=inventory_payload,
            order_state_payload=order_state_payload,
            station_id=str(args.station_id or "").strip(),
            event_date=str(args.event_date or "").strip(),
            quote_freshness_policy=quote_freshness_policy,
            policy_authority_scope=str(
                activation["policy_authority_scope"]
            ),
            commit=bool(args.commit),
        )
    except Exception as exc:
        result = {
            "schema": "bayes_basket_market_evidence_ingress_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "places_orders": False,
            "calls_order_gateway": False,
        }
    finally:
        if conn is not None:
            conn.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {"VALIDATED_NO_WRITE", "COMMITTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
