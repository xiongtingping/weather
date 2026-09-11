#!/usr/bin/env python3
"""Does a resting leg fill less often when it is the leg that wins?

Read-only.  The question the paper ledger cannot answer about itself: its
own resting simulation fills the winning leg and the losing leg at nearly
the same rate, and the live book does not.  A maker order is a standing
offer to be traded against, so the market sells it to us exactly when the
market thinks we are wrong -- and if that conditioning is real, the spread
a resting order saves is paid back in the legs that would have paid.

Three readings, because three different things get called "the fill rate":

*   **per intent** -- every order that reached the exchange.  Inflated by
    re-posts: one leg can carry several intents.
*   **per leg** (station-day-bucket) -- the unit the basket is built from,
    and the one to compare against the ledger's ``resting_fill_json``.  A
    leg counts as filled when any of its intents filled.
*   **per basket** (station-day), split by what actually went wrong:

    - ``winner_filled``   the leg that won was bought.
    - ``winner_gapped``   it was posted and never filled -- execution.
    - ``winner_missing``  it was never posted at all -- the model.

    The split matters because the two failures have different owners and
    only one of them is a property of the order type.  Measured 2026-09-11
    over 55 settled baskets: 16 missing (-58.09, four fifths of the loss),
    18 gapped (-14.25), 21 filled (+41.51).

Truth is the latest revision of the official high; the bucket a value
settles in comes from the ledger's own ``_settled_bucket`` rather than a
second copy of the interval rule, and the intervals come from the
station-day's own bridge history -- the same source the decision read.

    python3 scripts/measure_live_resting_fill_by_outcome.py --since 2026-09-05
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from paper_trading_ledger import _settled_bucket  # noqa: E402

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
OUT_DIR = ROOT / "data" / "research" / "live_fill_by_outcome"
# A leg that never reached the exchange is a different fact -- the gateway,
# the cash phase or the dollar floor refused it -- and counting it as an
# unfilled maker order would blame the order type for a decision taken
# before any order existed.  Reported separately.
NOT_PLACED = "resolved_not_placed"


def _rows(conn: sqlite3.Connection, since: str) -> list[tuple]:
    return conn.execute(
        "SELECT d.station_id, d.event_date, i.bucket, i.limit_price,"
        "       i.requested_size, r.state, COALESCE(r.filled_size,0),"
        "       COALESCE(r.filled_notional_usdc,0),"
        "       json_extract(i.intent_json,'$.execution_candidate'),"
        "       i.created_at, i.order_intent_id"
        "  FROM basket_target_order_intents i"
        "  JOIN basket_target_order_reconciliations r USING(order_intent_id)"
        "  JOIN auto_trade_decisions d ON d.decision_id = i.decision_id"
        " WHERE i.live_submit_authorized = 1"
        "   AND (?1 = '' OR i.created_at >= ?1)"
        " ORDER BY i.created_at",
        (since,),
    ).fetchall()


def _bucket_ladder(
    conn: sqlite3.Connection, station: str, date: str
) -> list[tuple]:
    """The station-day's own intervals, newest history first."""

    history = conn.execute(
        "SELECT history_id FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=?"
        " ORDER BY history_id DESC LIMIT 1",
        (station, date),
    ).fetchone()
    if not history:
        return []
    return [
        (str(bucket), int(ordinal or 0), lower, upper, float(p or 0.0))
        for bucket, ordinal, lower, upper, p in conn.execute(
            "SELECT bucket, ordinal, lower_c, upper_c, probability"
            " FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=? ORDER BY ordinal",
            (history[0],),
        )
    ]


def _truth(conn: sqlite3.Connection, station: str, date: str) -> float | None:
    row = conn.execute(
        "SELECT official_tmax_c FROM calibration_station_daily_high_truth"
        " WHERE station_id=? AND local_date=?"
        " ORDER BY revision_no DESC LIMIT 1",
        (station, date),
    ).fetchone()
    return None if row is None else float(row[0])


def measure(conn: sqlite3.Connection, *, since: str) -> dict:
    rows = _rows(conn, since)
    ladders: dict[tuple[str, str], list[tuple]] = {}
    truths: dict[tuple[str, str], float | None] = {}
    settled_bucket: dict[tuple[str, str], str | None] = {}
    for station, date, *_ in rows:
        scope = (str(station), str(date))
        if scope in truths:
            continue
        ladders[scope] = _bucket_ladder(conn, *scope)
        truths[scope] = _truth(conn, *scope)
        landed = None
        if truths[scope] is not None and ladders[scope]:
            hit = _settled_bucket(ladders[scope], truths[scope])
            landed = None if hit is None else hit[0]
        settled_bucket[scope] = landed

    per_intent: dict[tuple[str, bool], list] = defaultdict(
        lambda: [0, 0, 0.0, 0.0]
    )
    legs: dict[tuple[str, str, str], dict] = {}
    not_placed = [0, 0.0]
    unsettled = [0, 0.0]
    unpriceable = [0, 0.0]

    for (station, date, bucket, limit, size, state, filled_size,
         filled_cash, family, _created, _intent) in rows:
        scope = (str(station), str(date))
        bucket = str(bucket)
        requested = float(limit or 0.0) * float(size or 0.0)
        if str(state) == NOT_PLACED:
            not_placed[0] += 1
            not_placed[1] += requested
            continue
        landed = settled_bucket.get(scope)
        if landed is None:
            unsettled[0] += 1
            unsettled[1] += float(filled_cash or 0.0)
            continue
        if bucket not in {row[0] for row in ladders.get(scope, ())}:
            # The newest ladder for this station-day cannot price this
            # bucket -- it was relabelled or left the domain after the
            # order was posted.  ``bucket == landed`` would read False and
            # file it as a loss, which is a guess wearing a measurement's
            # clothes; it is counted here instead and adjudicated nowhere.
            unpriceable[0] += 1
            unpriceable[1] += float(filled_cash or 0.0)
            continue
        wins = bucket == landed
        slot = per_intent[(str(family or "?"), wins)]
        slot[0] += 1
        slot[1] += 1 if float(filled_size or 0.0) > 0 else 0
        slot[2] += requested
        slot[3] += float(filled_cash or 0.0)

        leg = legs.setdefault((scope[0], scope[1], bucket), {
            "families": set(), "intents": 0, "filled_size": 0.0,
            "filled_cash": 0.0, "last_requested": 0.0, "wins": wins,
        })
        leg["families"].add(str(family or "?"))
        leg["intents"] += 1
        leg["filled_size"] += float(filled_size or 0.0)
        leg["filled_cash"] += float(filled_cash or 0.0)
        # The last posting is the one whose price stood when the day ended.
        leg["last_requested"] = requested

    def _family(names: set[str]) -> str:
        return next(iter(names)) if len(names) == 1 else "MIXED"

    per_leg: dict[tuple[str, bool], list] = defaultdict(
        lambda: [0, 0, 0.0, 0.0, 0]
    )
    baskets: dict[tuple[str, str], dict] = {}
    for (station, date, bucket), leg in legs.items():
        family = _family(leg["families"])
        slot = per_leg[(family, leg["wins"])]
        slot[0] += 1
        slot[1] += 1 if leg["filled_size"] > 0 else 0
        slot[2] += leg["last_requested"]
        slot[3] += leg["filled_cash"]
        slot[4] += leg["intents"]
        basket = baskets.setdefault((station, date), {
            "cost": 0.0, "payout": 0.0, "families": set(),
            "winner_posted": False, "winner_filled": False, "legs": 0,
        })
        basket["families"].add(family)
        basket["legs"] += 1
        basket["cost"] += leg["filled_cash"]
        if leg["wins"]:
            basket["winner_posted"] = True
            basket["payout"] += leg["filled_size"]
            basket["winner_filled"] = (
                basket["winner_filled"] or leg["filled_size"] > 0
            )

    def _bucketed(rows_: dict, keys: int) -> list[dict]:
        out = []
        for (family, wins), slot in sorted(rows_.items()):
            row = {
                "family": family, "outcome": "WIN" if wins else "LOSE",
                "legs" if keys == 5 else "intents": slot[0],
                "filled": slot[1],
                "fill_rate_pct": round(100.0 * slot[1] / slot[0], 1)
                if slot[0] else None,
                "requested_usdc": round(slot[2], 2),
                "filled_usdc": round(slot[3], 2),
            }
            if keys == 5:
                row["intents_per_leg"] = round(slot[4] / slot[0], 2)
            out.append(row)
        return out

    def _split(predicate) -> dict:
        chosen = [b for b in baskets.values() if predicate(b)]
        cost = sum(b["cost"] for b in chosen)
        payout = sum(b["payout"] for b in chosen)
        return {
            "baskets": len(chosen), "cost_usdc": round(cost, 2),
            "pnl_usdc": round(payout - cost, 2),
            "roi_pct": round(100.0 * (payout - cost) / cost, 1)
            if cost else None,
        }

    cost = sum(b["cost"] for b in baskets.values())
    payout = sum(b["payout"] for b in baskets.values())
    by_family: dict[str, list] = defaultdict(lambda: [0, 0.0, 0.0])
    by_day: dict[str, list] = defaultdict(lambda: [0, 0.0, 0.0])
    for (station, date), basket in baskets.items():
        key = "|".join(sorted(basket["families"]))
        for target, slot in ((by_family, key), (by_day, date)):
            row = target[slot]
            row[0] += 1
            row[1] += basket["cost"]
            row[2] += basket["payout"] - basket["cost"]

    return {
        "schema": "live_resting_fill_by_outcome_v1",
        "since": since or "all",
        "intents_not_placed": {
            "intents": not_placed[0],
            "requested_usdc": round(not_placed[1], 2),
        },
        "legs_unsettled": {
            "intents": unsettled[0], "filled_usdc": round(unsettled[1], 2),
        },
        "legs_unpriceable_by_current_ladder": {
            "intents": unpriceable[0],
            "filled_usdc": round(unpriceable[1], 2),
        },
        "per_intent": _bucketed(per_intent, 4),
        "per_leg": _bucketed(per_leg, 5),
        "baskets": {
            "settled": len(baskets),
            "legs": sum(b["legs"] for b in baskets.values()),
            "cost_usdc": round(cost, 2),
            "payout_usdc": round(payout, 2),
            "pnl_usdc": round(payout - cost, 2),
            "roi_pct": round(100.0 * (payout - cost) / cost, 1)
            if cost else None,
            "hit_baskets": sum(
                1 for b in baskets.values() if b["winner_filled"]
            ),
            "winner_filled": _split(lambda b: b["winner_filled"]),
            "winner_gapped": _split(
                lambda b: b["winner_posted"] and not b["winner_filled"]
            ),
            "winner_missing": _split(lambda b: not b["winner_posted"]),
            "by_family": {
                k: {"baskets": v[0], "cost_usdc": round(v[1], 2),
                    "pnl_usdc": round(v[2], 2),
                    "roi_pct": round(100.0 * v[2] / v[1], 1) if v[1] else None}
                for k, v in sorted(by_family.items())
            },
            "by_event_date": {
                k: {"baskets": v[0], "cost_usdc": round(v[1], 2),
                    "pnl_usdc": round(v[2], 2),
                    "roi_pct": round(100.0 * v[2] / v[1], 1) if v[1] else None}
                for k, v in sorted(by_day.items())
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--since", default="",
        help="ISO instant floor on intent creation; 2026-09-05 is when the"
             " resting family went live",
    )
    parser.add_argument("--out", type=Path, help="write the JSON here")
    parser.add_argument("--write-research", action="store_true",
                        help="write under data/research/live_fill_by_outcome/")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60.0)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA query_only=ON")
        report = measure(conn, since=args.since)
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=1))
    destination = args.out
    if args.write_research and destination is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        destination = OUT_DIR / (
            "%s.json" % (args.since.replace(":", "").replace("-", "")[:8]
                         or "all")
        )
    if destination is not None:
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print("wrote %s" % destination, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
