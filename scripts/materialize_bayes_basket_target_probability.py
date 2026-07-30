#!/usr/bin/env python3
"""Materialize one target P0-M/P1 checkpoint.

P0 is deliberately forbidden here: its only production-no-live ingress is
the resident official prefetch -> native decode -> P0 inbox -> worker chain.
The revision packet is a strict, offline-produced contract.  This command never
fetches TAF/METAR/NWP, trains a model, reads legacy posterior tables, calls a
gateway, or creates an order.  Without ``--apply`` it is a read-only preview;
database mutation requires the explicit confirmation phrase.  The target
production-no-live line has one calibration authority:
``PRODUCTION_NO_LIVE_ONLY``. Formal promotion remains an offline evaluation
concept and is not a second production materialization branch.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import ContractError  # noqa: E402
from bayes_basket_target.materialization import insert_probability_envelope  # noqa: E402
from bayes_basket_target.probability_packet import (  # noqa: E402
    ALLOWED_KEYS,
    build_probability_envelope,
)
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.ssot import production_ssot_schema_status  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json  # noqa: E402


SCHEMA_VERSION = "materialize_bayes_basket_target_probability_v1"
CONFIRMATION = "MATERIALIZE_BAYES_BASKET_TARGET_PROBABILITY"
DEFAULT_DB = DEFAULT_TARGET_DB
build_envelope = build_probability_envelope


def run(*, db: Path, packet_path: Path, apply: bool, confirm: str) -> dict[str, Any]:
    db = resolve_target_database(db)
    packet = load_bounded_json(packet_path)
    if not isinstance(packet, Mapping):
        raise ContractError("materialization_packet_not_mapping")
    if str(packet.get("stage") or "").strip().upper() == "P0":
        raise ContractError(
            "direct_p0_materialization_forbidden_use_resident_ingress"
        )
    envelope = build_envelope(packet)
    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "state": "preview_no_apply",
        "envelope": envelope.to_dict(),
        "envelope_content_sha256": envelope.content_sha256,
        "places_orders": False,
        "calls_order_gateway": False,
        "writes_orders": False,
    }
    if not apply:
        return result
    if confirm != CONFIRMATION:
        raise ContractError("materialization_confirmation_invalid")
    now_utc = datetime.now(timezone.utc)
    decision_at = datetime.fromisoformat(
        envelope.decision_at_utc.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    created_at_text = str(
        packet.get("created_at_utc") or envelope.decision_at_utc
    )
    created_at = datetime.fromisoformat(
        created_at_text.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if decision_at > now_utc or created_at > now_utc:
        raise ContractError("materialization_decision_time_in_future")
    if created_at < decision_at:
        raise ContractError("materialization_created_before_decision")
    with sqlite3.connect(db, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        status = production_ssot_schema_status(conn)
        if not status["ok"]:
            raise ContractError("production_ssot_schema_incomplete")
        conn.execute("BEGIN IMMEDIATE")
        try:
            receipt = insert_probability_envelope(
                conn,
                envelope,
                created_at_utc=created_at_text,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    result.update({"state": "materialized", "receipt": receipt, "writes_target_ssot": bool(receipt.get("wrote"))})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        print(json.dumps(run(
            db=args.db.expanduser().resolve(),
            packet_path=args.packet.expanduser().resolve(),
            apply=bool(args.apply),
            confirm=args.confirm,
        ), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({
            "schema": SCHEMA_VERSION,
            "state": "blocked_fail_closed",
            "reason": f"{type(exc).__name__}:{exc}",
            "places_orders": False,
            "calls_order_gateway": False,
            "writes_orders": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
