#!/usr/bin/env python3
"""Create today's production v6 P0 and consume all durable canonical events."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_db_write_queue import connect_archive_db_rw
from joint_weak_live_evidence_runtime import DEFAULT_CURVE_ROOT
from production_probability_v6_activation_contract import validate_v6_activation
from production_probability_v6_canonical_event_drain_runtime import drain_v6_canonical_events, ensure_v6_daily_p0


SCHEMA_VERSION = "bootstrap_production_probability_v6_v1"
CONFIRMATION = "BOOTSTRAP_PRODUCTION_PHYSICAL_STATE_BAYES_V6"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--activation", type=Path, default=ROOT / "config/production_probability_runtime_activation.json")
    parser.add_argument("--event-date", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    parser.add_argument("--decision-as-of", default=datetime.now(ZoneInfo("UTC")).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    parser.add_argument("--curve-root", type=Path, default=DEFAULT_CURVE_ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if not args.apply or args.confirm != CONFIRMATION:
        result = {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "confirmation_missing"}
    else:
        activation = validate_v6_activation(json.loads(args.activation.read_text(encoding="utf-8")))
        try:
            with connect_archive_db_rw(args.db, owner="physical_state_bayes_v6_cutover_bootstrap") as conn:
                p0 = ensure_v6_daily_p0(
                    conn,
                    activation=activation,
                    event_date=args.event_date,
                    decision_as_of=args.decision_as_of,
                    curve_root=args.curve_root,
                )
                drain = (
                    drain_v6_canonical_events(
                        conn,
                        activation=activation,
                        event_date=args.event_date,
                        max_events=4096,
                    )
                    if p0.get("ok") is True
                    else {"ok": False, "state": "blocked", "reason": "v6_p0_unavailable"}
                )
            result = {
                "schema": SCHEMA_VERSION,
                "ok": p0.get("ok") is True and drain.get("ok") is True,
                "state": "v6_production_bootstrap_committed" if p0.get("ok") is True and drain.get("ok") is True else "blocked",
                "daily_p0": p0,
                "canonical_drain": drain,
                "activation_contract_sha256": activation["activation_contract_sha256"],
                "places_orders": False,
            }
        except Exception as exc:
            result = {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": f"v6_bootstrap_failed:{type(exc).__name__}",
                "exception_detail": str(exc)[:500],
                "places_orders": False,
            }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
