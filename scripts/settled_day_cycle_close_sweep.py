#!/usr/bin/env python3
"""Release the capital reservations of cycles whose settlement day is over.

A prepared execution cycle holds an active capital reservation until a newer
decision on the same station-day supersedes it, and supersession is the only
release path production ever calls -- ``close_target_cycle`` exists, is
tested, and has no caller outside tests.  Intraday that is fine: the guard in
``_scope_cycle_guard`` is keyed on station *and event date*, so each new
decision retires the previous one for that day.  The strand happens at the
end of the day.  The last cycle of every station-day that traded never gets a
successor, because no decision for a closed day will ever be compiled again.

Measured 2026-08-26: 161.32 USDC of ``reserved_usdc`` sitting on 54 cycles
across 54 station-days from 08-14 to 08-21, against a Tier-1 breach threshold
of 180.00 (0.35 x bankroll 514.29).  Crossing it sets ``new_intents_allowed``
false, which zeroes the planning budget of all forty-five stations at once --
so a leak that only ever grows, at 20-35 USDC per trading day, is a fleet-wide
halt on a timer.  It is capital only: the guard's scope key includes the event
date, so a stranded cycle from a past day never blocks a new one, and the
dangling intents block only their own settled bucket and token.

Safe beside the resident.  The scan is all reads, which in WAL mode do not
block a writer, and each cycle is its own short transaction -- a maintenance
writer that held the lock across its own scan would not slow the resident,
it would fail-close P0 and P1 for that whole cycle.  Bounded per pass, and
idempotent: a cycle already settled returns ``already_settled`` and writes
nothing, so a doubled or early run costs nothing.

    python3 scripts/settled_day_cycle_close_sweep.py            # close and commit
    python3 scripts/settled_day_cycle_close_sweep.py --dry-run  # classify only
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.atomic_ledger import (  # noqa: E402
    AtomicLedgerError,
    close_settled_day_cycle,
)
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.settlement_time import (  # noqa: E402
    settlement_day_state,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATUS_JSON = ROOT / "data" / "settled_day_cycle_close_status.json"
SCHEMA = "settled_day_cycle_close_sweep_v1"
# A pass is bounded so it cannot run long enough to matter to the resident.
# The zero-amount backlog drains over consecutive passes; cycles are served
# richest first, so the capital that the Tier-1 gate actually counts comes
# back on the first pass and nothing that matters starves behind the tail.
DEFAULT_MAX_CYCLES = 1500
CLOSE_REASON = "settlement_day_closed_without_successor"
# Wait past the day's end before retiring anything.  Whether the day is over
# is decided from the registry's settlement timezone, and that column has a
# mislabelling history -- 33 of 45 stations once carried the wrong zone -- so
# closing on the second the window ends bets "this day can produce no further
# candidate" on one string.  Six hours puts the close after local dawn and
# absorbs any DST or half-hour-offset discrepancy.  Settling a cycle that
# could still be superseded is not a lost reservation, it is a refused
# supersession: production calls it unguarded, so the station's whole commit
# fails that round.
SETTLEMENT_CLOSE_GRACE_HOURS = 6.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _reason_of(error: Exception) -> str:
    return str(error).split(":", 1)[0].strip() or "unknown"


def _candidates(
    conn: sqlite3.Connection, *, as_of: str, limit: int
) -> tuple[list[dict[str, Any]], Counter, dict[str, float]]:
    """Cycles whose day is over, richest first, plus why the rest were held.

    The predicate deliberately stops at "the day is over and the cycle is
    inert".  Everything about *whether the reservation may be released* is
    the ledger's own business and is checked again there; duplicating those
    rules here would give the same question two answers.
    """

    rows = conn.execute(
        "SELECT c.cycle_id,c.station_id,c.event_date,c.settlement_timezone,"
        "r.amount_usdc "
        "FROM basket_target_execution_cycles c "
        "JOIN basket_target_capital_reservations r ON r.cycle_id=c.cycle_id "
        "WHERE c.state IN ('prepared','submitted','reconcile_required') "
        "AND r.state='active'"
    ).fetchall()

    held: Counter = Counter()
    held_usdc: dict[str, float] = {}
    ready: list[dict[str, Any]] = []

    for cycle_id, station, event_date, timezone_name, amount in rows:
        amount = float(amount or 0.0)
        if not timezone_name:
            held["settlement_timezone_missing"] += 1
            held_usdc["settlement_timezone_missing"] = round(
                held_usdc.get("settlement_timezone_missing", 0.0) + amount, 6
            )
            continue
        try:
            day = settlement_day_state(
                event_date, as_of_utc=as_of, settlement_timezone=timezone_name
            )
        except ContractError as exc:
            reason = _reason_of(exc)
            held[reason] += 1
            held_usdc[reason] = round(held_usdc.get(reason, 0.0) + amount, 6)
            continue
        if day["state"] != "closed":
            reason = f"settlement_day_{day['state']}"
            held[reason] += 1
            held_usdc[reason] = round(held_usdc.get(reason, 0.0) + amount, 6)
            continue
        if _instant(as_of) < _instant(
            day["target_day_end_utc"]
        ) + timedelta(hours=SETTLEMENT_CLOSE_GRACE_HOURS):
            held["settlement_day_within_grace"] += 1
            held_usdc["settlement_day_within_grace"] = round(
                held_usdc.get("settlement_day_within_grace", 0.0) + amount, 6
            )
            continue
        ready.append(
            {
                "cycle_id": str(cycle_id),
                "station_id": str(station),
                "event_date": str(event_date),
                "amount_usdc": amount,
                "settlement_day_end_utc": day["target_day_end_utc"],
            }
        )

    ready.sort(key=lambda item: (-item["amount_usdc"], item["cycle_id"]))
    return ready[:limit], held, held_usdc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-cycles", type=int, default=DEFAULT_MAX_CYCLES)
    args = parser.parse_args()

    as_of = _now()
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        pool_before = conn.execute(
            "SELECT balance_usdc,reserved_usdc FROM "
            "basket_target_capital_pool WHERE pool_id=1"
        ).fetchone()
        ready, held, held_usdc = _candidates(
            conn, as_of=as_of, limit=max(0, args.max_cycles)
        )

        closed = 0
        already = 0
        released = 0.0
        cancelled_intents = 0
        refused: Counter = Counter()
        if not args.dry_run:
            for item in ready:
                try:
                    result = close_settled_day_cycle(
                        conn,
                        cycle_id=item["cycle_id"],
                        settlement_day_end_utc=item["settlement_day_end_utc"],
                        closed_at_utc=as_of,
                        reason=CLOSE_REASON,
                    )
                except (AtomicLedgerError, ContractError) as exc:
                    refused[_reason_of(exc)] += 1
                    continue
                if result["state"] == "already_settled":
                    already += 1
                    continue
                closed += 1
                released = round(released + float(result["released_usdc"]), 6)
                cancelled_intents += int(result["cancelled_intent_count"])

        pool_after = conn.execute(
            "SELECT balance_usdc,reserved_usdc FROM "
            "basket_target_capital_pool WHERE pool_id=1"
        ).fetchone()
    finally:
        conn.close()

    receipt = {
        "schema": SCHEMA,
        "as_of_utc": as_of,
        "dry_run": bool(args.dry_run),
        "places_orders": False,
        "calls_order_gateway": False,
        "eligible_cycle_count": len(ready),
        "eligible_usdc": round(
            sum(item["amount_usdc"] for item in ready), 6
        ),
        "closed_cycle_count": closed,
        "already_settled_count": already,
        "released_usdc": released,
        "cancelled_intent_count": cancelled_intents,
        "refused_counts": dict(refused),
        "held_back_counts": dict(held),
        "held_back_usdc": held_usdc,
        "pool_reserved_before_usdc": pool_before[1] if pool_before else None,
        "pool_reserved_after_usdc": pool_after[1] if pool_after else None,
        "pool_balance_usdc": pool_after[0] if pool_after else None,
    }
    STATUS_JSON.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
