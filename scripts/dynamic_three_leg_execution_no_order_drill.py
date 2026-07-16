#!/usr/bin/env python3
"""Run one resident cycle against an explicit isolated DB/local gateway.

The caller owns fixture preparation.  This script never creates facts, never
accepts archive.db, and never contacts a non-loopback gateway.  Its timing is
structural evidence only and cannot satisfy production T1->T5 acceptance.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dynamic_three_leg_clob_gateway_adapter import LocalDynamicThreeLegClobGateway  # noqa: E402
from resident_dynamic_three_leg_execution_loop import run_resident_dynamic_three_leg_execution_once  # noqa: E402


CONFIRMATION = "RUN_ISOLATED_DYNAMIC_THREE_LEG_NO_ORDER_DRILL"


def _utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _isolated_t1_to_t5(db: Path, *, order_intent_id: str) -> dict:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            """SELECT x.first_actionable_seen_at,x.observation_to_order_hard_deadline_at,
                      i.exchange_post_started_at,i.posterior_run_id
                 FROM posterior_execution_intents i
                 JOIN posterior_outbox_execution_contracts x
                   ON x.posterior_run_id=i.posterior_run_id
                WHERE i.order_intent_id=?""",
            (str(order_intent_id or "").strip(),),
        ).fetchone()
    if row is None:
        return {
            "ok": False,
            "state": "unavailable",
            "reason": "isolated_t1_to_t5_lineage_missing",
            "counts_as_production_slo_evidence": False,
        }
    t1, deadline, t5 = _utc(row[0]), _utc(row[1]), _utc(row[2])
    elapsed = (t5 - t1).total_seconds() * 1000.0 if t1 is not None and t5 is not None else None
    deadline_matches = bool(
        t1 is not None and deadline is not None
        and abs((deadline - t1).total_seconds() * 1000.0 - 3000.0) <= 1.0
    )
    passed = bool(
        elapsed is not None and 0.0 <= elapsed <= 3000.0
        and deadline_matches and t5 is not None and deadline is not None and t5 <= deadline
    )
    return {
        "ok": passed,
        "state": "passed" if passed else "failed",
        "posterior_run_id": str(row[3] or ""),
        "first_actionable_seen_at": str(row[0] or ""),
        "observation_to_order_hard_deadline_at": str(row[1] or ""),
        "exchange_post_started_at": str(row[2] or ""),
        "t1_to_t5_ms": round(elapsed, 3) if elapsed is not None else None,
        "threshold_ms": 3000,
        "deadline_policy_verified": deadline_matches,
        "t5_source": "posterior_execution_intents.exchange_post_started_at",
        "counts_as_production_slo_evidence": False,
    }


def run_drill(*, db_path: Path, gateway_url: str) -> dict:
    db = db_path.expanduser().resolve()
    production = (ROOT / "data" / "archive.db").resolve()
    if db == production or db.name == "archive.db" or not db.exists():
        raise ValueError("no_order_drill_requires_existing_non_archive_isolated_db")
    gateway = LocalDynamicThreeLegClobGateway(gateway_url)
    old = {name: os.environ.get(name) for name in (
        "PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED",
        "PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED",
        "PM_LIVE_PAUSE_FILE",
    )}
    started = time.perf_counter()
    try:
        os.environ["PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED"] = "1"
        os.environ["PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED"] = "1"
        os.environ["PM_LIVE_PAUSE_FILE"] = str(db.parent / "absent-no-order-drill-pause-file")
        result = run_resident_dynamic_three_leg_execution_once(db_path=db, gateway=gateway)
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    order_intent_id = str(result.get("order_intent_id") or "").strip()
    latency = _isolated_t1_to_t5(db, order_intent_id=order_intent_id) if order_intent_id else {
        "ok": False,
        "state": "unavailable",
        "reason": "isolated_t1_to_t5_order_intent_missing",
        "counts_as_production_slo_evidence": False,
    }
    return {
        "schema": "dynamic_three_leg_execution_no_order_drill_v1",
        "ok": bool(
            result.get("ok") is True
            and result.get("state") == "submitted"
            and isinstance(result.get("result"), dict)
            and result["result"].get("state") == "gateway_accepted"
            and latency.get("ok") is True
        ),
        "state": "isolated_local_gateway_completed",
        "synthetic_no_order": True,
        "gateway_loopback_only": True,
        "counts_as_production_slo_evidence": False,
        "wall_ms": round((time.perf_counter() - started) * 1000, 3),
        "isolated_t1_to_t5": latency,
        "resident_result": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        raise SystemExit("exact_confirmation_required")
    output = run_drill(db_path=args.db, gateway_url=args.gateway_url)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if output["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
