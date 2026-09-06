#!/usr/bin/env python3
"""Restore intents a not-found lookup ended while the order was still live.

2026-09-06 15:06:39Z the charter-94 resting lane wrote CANCEL_ACK for two
rungs off a single gateway reply.  The gateway maps a not-found lookup to
``status: cancelled`` -- correct for a caller asking whether its own cancel
landed, wrong for a lane that cancels nothing before it syncs.  Both orders
were still resting on the exchange when it was asked again eight minutes
later: LIMC 33C at 0.131 for 8 shares and KATL 84-85F at 0.121 for 9.

The cause is fixed in the gateway (the inference now says it is one) and in
the lane (which now declines it).  This repairs what the bad reads already
wrote, because the ledger is what frees capital and the bucket for a re-post:
an intent recorded cancelled while its order works is capital the book will
commit twice.

Fail closed at every step.  An intent is repaired only when the exchange,
asked now, answers that the order is live; anything else is reported and left
alone.  The false CANCEL_ACK is removed rather than annotated because
``record_order_event`` derives intent state from these rows, so a corrected
state with the event still standing would be re-cancelled by the next
re-derivation.  Every removed row is copied verbatim into the receipt first.

Dry run unless --apply.  Reads the exchange through the local gateway only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
RECEIPT = ROOT / "output" / "audits" / "false_cancel_repair_latest.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", type=Path, default=DB)
    args = ap.parse_args(argv)

    from clob_order_service_client import ClobOrderServiceClient

    client = ClobOrderServiceClient(timeout_seconds=20.0)
    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT oi.order_intent_id, oi.state, oi.limit_price, oi.requested_size,
               oi.idempotency_key, cy.station_id, cy.event_date, oi.bucket,
               oe.event_id, oe.exchange_order_id, oe.event_json, oe.observed_at
        FROM basket_target_order_intents oi
        JOIN basket_target_execution_cycles cy ON cy.cycle_id = oi.cycle_id
        JOIN basket_target_order_events oe ON oe.order_intent_id = oi.order_intent_id
        WHERE oi.state = 'cancelled' AND oe.event_type = 'CANCEL_ACK'
          AND json_extract(oe.event_json, '$.recovered_by') = 'resting_ladder_lifecycle'
          AND COALESCE(oe.matched_size, 0) = 0
        """
    ).fetchall()

    findings: list[dict] = []
    for r in rows:
        probe = client.lookup_order({"idempotency_key": r["idempotency_key"]})
        status = str(probe.get("status") or "")
        matched = probe.get("size_matched") or probe.get("shares_matched") or ""
        live = probe.get("ok") is True and status == "live" and str(matched) in ("", "0", "0.0")
        findings.append({
            "order_intent_id": r["order_intent_id"],
            "station_id": r["station_id"],
            "event_date": r["event_date"],
            "bucket": r["bucket"],
            "limit_price": r["limit_price"],
            "requested_size": r["requested_size"],
            "exchange_order_id": r["exchange_order_id"],
            "false_event_id": r["event_id"],
            "false_event_observed_at": r["observed_at"],
            "false_event_json": r["event_json"],
            "exchange_status_now": status,
            "exchange_size_matched_now": str(matched),
            "repairable": bool(live),
        })

    repairable = [f for f in findings if f["repairable"]]
    receipt = {
        "schema": "bayes_basket_target_false_cancel_repair_v1",
        "generated_at_utc": _now(),
        "applied": bool(args.apply),
        "examined": len(findings),
        "repairable": len(repairable),
        "findings": findings,
        "calls_order_gateway": True,
        "places_orders": False,
        "orders_submitted": 0,
    }

    if args.apply and repairable:
        with conn:
            for f in repairable:
                conn.execute(
                    "DELETE FROM basket_target_order_events WHERE event_id=?",
                    (f["false_event_id"],),
                )
                cur = conn.execute(
                    "UPDATE basket_target_order_intents SET state='submitted'"
                    " WHERE order_intent_id=? AND state='cancelled'",
                    (f["order_intent_id"],),
                )
                if cur.rowcount != 1:
                    raise SystemExit(
                        "repair_state_transition_unexpected:" + f["order_intent_id"]
                    )
        receipt["repaired"] = [f["order_intent_id"] for f in repairable]

    receipt["content_sha256"] = content_sha256(
        {k: v for k, v in receipt.items() if k != "content_sha256"}
    )
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "applied": receipt["applied"],
        "examined": receipt["examined"],
        "repairable": receipt["repairable"],
        "receipt": str(RECEIPT),
    }, ensure_ascii=False, sort_keys=True))
    for f in findings:
        print(f"  {f['station_id']:6} {f['bucket']:>10} {f['limit_price']:.3f} x{f['requested_size']:.0f}"
              f"  交易所现状={f['exchange_status_now']:<10} 可修={f['repairable']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
