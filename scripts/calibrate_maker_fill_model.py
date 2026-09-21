#!/usr/bin/env python3
"""Hold the ledger's maker fill model to the shadow lane's measurement.

The paper ledger simulates a resting order by asking whether the book's ask
ever traded through the rung.  That simulation is the source of every money
number the ledger produces, and since charter 94 made the order family a
resting ladder it is the source of the ledger's whole P&L shape -- so it has
to be measured against something, and there is exactly one thing to measure
it against: ``maker_shadow_order``, four million post-only intents placed
against the real book since 2026-08-17 under five price rules and four
horizons, filled by the same ``ask <= limit`` rule the ledger uses.

The one number that matters is the asymmetry: how much less often a rung on
the bucket that WINS fills than a rung on a bucket that loses.  That ratio is
what turns a calibrated claim into a losing book, and it is the ledger's
loudest current reading -- winners 15.3%, losers 88.2%, a ratio of 0.17.

The shadow lane does not see that.  At the ladder's own price rule the ratio
is 0.96 over a sixty-minute horizon, and stratified by how far the order sits
from settlement it is ABOVE one for everything earlier than three hours out,
reaching 0.63 only inside the final hour.  Nothing in four million orders is
within a factor of three of 0.17.

The difference the two lanes have is the re-post.  A shadow order rests at a
fixed limit; the ledger re-anchors to the current bid every five minutes.
Charter 108 tested that in the ledger -- one switch, one replay window --
and the asymmetry moved from 0.76 to 0.23, so the re-post is the mechanism.
What it is NOT is measured on a real book: nothing here chased.

Charter 108's arm closes that.  ``bid+1t@chase`` is written at the same
instant, off the same book, at the same price as its fixed twin, and
re-anchors on production's own five-minute cadence; the two settle through
the same code.  It starts empty on 2026-09-07 and this receipt reports it as
accumulating until it has settled orders, which is the honest reading and
not a zero.

Usage:
    python3 scripts/calibrate_maker_fill_model.py            # print + receipt
    python3 scripts/calibrate_maker_fill_model.py --json     # receipt only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys

from bayes_basket_target.maker_shadow_store import attach_maker_shadow  # noqa: E402
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
RECEIPT = ROOT / "output" / "audits" / "maker_fill_model_calibration_latest.json"

# The ladder charter 94 posts: bid+1 tick, bid, bid-1 tick.  The shadow lane
# carries the first two of those three by name; ``bid+1t`` is the rung the
# ledger posts first and fills first, so it is the comparison rule.
LADDER_RULE = "bid+1t"
# Its chasing twin (charter 108).  Placed at the same instant off the same
# book at the same price, and re-anchored on production's own cadence -- so
# the pair isolates the one thing the ledger and the fixed rules disagree
# about.  Absent from every order written before 2026-09-07T07:27Z, so this
# reads as "accumulating" until a day of them has settled.
CHASE_RULE = "bid+1t@chase"
# Charter 109's arm: rest at bid+1t for the horizon, pay the ask if the book
# never came.  What it answers is the one assumption the paired analysis
# rested on -- that crossing later costs no more than crossing now -- so the
# reading that matters is the price its crossed half paid against what the
# plain taker paid at placement.
TAKE_AFTER_RULE = "bid+1t@then_take"
LEAD_BANDS = (
    ("D-2 (>=24h)", 24.0, 1e9),
    ("12-24h", 12.0, 24.0),
    ("6-12h", 6.0, 12.0),
    ("3-6h", 3.0, 6.0),
    ("1-3h", 1.0, 3.0),
    ("<1h", -1e9, 1.0),
)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    attach_maker_shadow(conn, ssot_path=db)
    conn.row_factory = sqlite3.Row
    from paper_ledger_reader import attach_paper_ledger  # noqa: PLC0415
    attach_paper_ledger(conn)
    return conn


def shadow_reading(conn: sqlite3.Connection) -> dict:
    """Fill rate on winning versus losing buckets, from the shadow lane."""

    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _win AS"
        " SELECT DISTINCT station_id, event_date, settled_bucket"
        " FROM maker_shadow_settlement WHERE settled_bucket IS NOT NULL"
    )
    by_rule = []
    for row in conn.execute(
        "SELECT o.price_rule rule, o.horizon_minutes horizon,"
        " o.bucket = w.settled_bucket AS won,"
        " COUNT(*) n, SUM(o.state='FILLED') filled"
        " FROM maker_shadow_order o JOIN _win w"
        "   ON w.station_id=o.station_id AND w.event_date=o.event_date"
        " WHERE o.state IN ('FILLED','EXPIRED')"
        " GROUP BY 1,2,3"
    ):
        by_rule.append({
            "price_rule": str(row["rule"]),
            "horizon_minutes": int(row["horizon"]),
            "bucket": "winner" if row["won"] else "loser",
            "orders": int(row["n"]),
            "fill_rate": round(float(row["filled"]) / max(int(row["n"]), 1), 4),
        })
    by_lead = []
    rows = conn.execute(
        "SELECT o.lead_hours lead, o.bucket = w.settled_bucket AS won,"
        " o.state='FILLED' AS filled"
        " FROM maker_shadow_order o JOIN _win w"
        "   ON w.station_id=o.station_id AND w.event_date=o.event_date"
        " WHERE o.state IN ('FILLED','EXPIRED') AND o.price_rule=?"
        "   AND o.horizon_minutes=60",
        (LADDER_RULE,),
    ).fetchall()
    for label, low, high in LEAD_BANDS:
        cell = {"winner": [0, 0], "loser": [0, 0]}
        for row in rows:
            lead = float(row["lead"] or 0.0)
            if not (low <= lead < high):
                continue
            slot = cell["winner" if row["won"] else "loser"]
            slot[0] += 1
            slot[1] += int(row["filled"])
        if not cell["winner"][0] or not cell["loser"][0]:
            continue
        win = cell["winner"][1] / cell["winner"][0]
        lose = cell["loser"][1] / cell["loser"][0]
        by_lead.append({
            "lead_band": label,
            "winner_orders": cell["winner"][0],
            "loser_orders": cell["loser"][0],
            "winner_fill_rate": round(win, 4),
            "loser_fill_rate": round(lose, 4),
            "asymmetry": round(win / lose, 4) if lose > 0 else None,
        })
    def _asymmetry(rule: str) -> float | None:
        cells = [
            cell for cell in by_rule
            if cell["price_rule"] == rule and cell["horizon_minutes"] == 60
        ]
        w = next((c for c in cells if c["bucket"] == "winner"), None)
        l = next((c for c in cells if c["bucket"] == "loser"), None)
        if not w or not l or l["fill_rate"] <= 0:
            return None
        return round(w["fill_rate"] / l["fill_rate"], 4)

    chase_orders = sum(
        cell["orders"] for cell in by_rule if cell["price_rule"] == CHASE_RULE
    )
    take_after = {"maker": [0, 0.0], "crossed": [0, 0.0], "taker": [0, 0.0]}
    try:
        take_rows = conn.execute(
            "SELECT o.price_rule rule, o.fill_kind kind,"
            " COALESCE(o.fill_limit_price, o.limit_price) paid"
            " FROM maker_shadow_order o"
            " WHERE o.state='FILLED' AND o.horizon_minutes=60"
            "   AND o.price_rule IN (?, 'ask')",
            (TAKE_AFTER_RULE,),
        ).fetchall()
    except sqlite3.Error:
        # A database older than the arm, or a replay extract that carries
        # the order table without it.  Reported as accumulating, which is
        # what it is.
        take_rows = []
    for row in take_rows:
        key = (
            "taker" if str(row["rule"]) == "ask"
            else str(row["kind"] or "maker")
        )
        if key not in take_after:
            continue
        take_after[key][0] += 1
        take_after[key][1] += float(row["paid"] or 0.0)
    take_reading = {
        key: {
            "fills": cell[0],
            "mean_price_paid": (
                round(cell[1] / cell[0], 4) if cell[0] else None
            ),
        }
        for key, cell in take_after.items()
    }
    ladder = [
        cell for cell in by_rule
        if cell["price_rule"] == LADDER_RULE and cell["horizon_minutes"] == 60
    ]
    win = next((c for c in ladder if c["bucket"] == "winner"), None)
    lose = next((c for c in ladder if c["bucket"] == "loser"), None)
    return {
        "price_rule": LADDER_RULE,
        "chase_rule": CHASE_RULE,
        # The number charter 108 said had never been measured.  ``None``
        # while the arm is still accumulating, which is the honest reading
        # and not a zero.
        "chase_asymmetry_60min": _asymmetry(CHASE_RULE),
        "chase_orders_settled": chase_orders,
        "take_after_rule": TAKE_AFTER_RULE,
        # ``crossed`` against ``taker`` is the whole question: what a cross
        # deferred by one horizon costs against a cross taken at once.
        "take_after": take_reading,
        "by_rule_and_horizon": sorted(
            by_rule, key=lambda c: (c["price_rule"], c["horizon_minutes"])
        ),
        "by_lead_hours": by_lead,
        "ladder_asymmetry_60min": (
            round(win["fill_rate"] / lose["fill_rate"], 4)
            if win and lose and lose["fill_rate"] > 0 else None
        ),
        "orders": sum(c["orders"] for c in by_rule),
    }


def level_reading(conn: sqlite3.Connection) -> dict:
    """Not the asymmetry -- the LEVEL.  How often a rung fills at all.

    The asymmetry above answers "does the simulation pick the wrong legs".
    It has been inside the shadow lane's range since charter 94.  This
    answers the other half, which nothing was checking: does the simulation
    fill too MANY legs.  Measured 2026-09-08 after the live maker family had
    posted for three days, and the two numbers were not close.

    The live column counts only legs that REACHED the exchange.  Three
    quarters of the live maker legs never did -- the station gate refused
    them as barred, held or on probation -- and those say nothing about
    whether a resting order fills, so counting them would report a
    permission fact as an execution fact.  (It did, in the first reading of
    this: 9.3% instead of 35.9%.)

    Horizons do not match and are reported rather than reconciled.  The
    shadow lane's longest is sixty minutes; production and the ledger both
    rest until the day resolves (charter 94, user ruling 2026-08-17).  So
    the shadow column bounds the level from below and only the live column
    is a like-for-like target.
    """

    intended = kept = 0
    for il, ls in conn.execute(
        "SELECT intended_legs_json, leg_shares_json FROM paper_trade_position"
        " WHERE intended_legs_json NOT IN ('', '{}', '[]')"
    ):
        try:
            legs = json.loads(il or "{}") or {}
            held = json.loads(ls or "{}") or {}
        except (TypeError, ValueError):
            continue
        if not isinstance(held, dict):
            held = {}
        for bucket in legs:
            intended += 1
            kept += 1 if float(held.get(bucket, 0.0) or 0.0) > 0 else 0

    # The live arm, keyed the way the PAPER ledger keys it -- which is the
    # correction that matters here.  A ``paper_trade_position`` row exists
    # only when at least one leg filled, so its denominator is "legs of a
    # basket that opened".  The first fit of this compared it against legs
    # that merely reached the exchange, a set that includes every basket
    # which filled nothing, and concluded the simulation was 1.7x too
    # generous when on the right denominator it is 71.4% against 67.1%.
    #
    # Both are reported.  The gap that survives the correction is not the
    # leg rate at all: live opens 27 of 73 attempted baskets and the ledger
    # opens about 85%, and that is a different mechanism.
    reached = filled = posted = 0
    gated: dict[str, int] = {}
    try:
        venue = {
            str(order): set()
            for (order,) in conn.execute(
                "SELECT DISTINCT order_intent_id FROM basket_target_order_events")
        }
        for order, kind in conn.execute(
            "SELECT order_intent_id, event_type FROM basket_target_order_events"
        ):
            venue[str(order)].add(str(kind))
        AT_VENUE = {"POST_ACK", "FILL", "PARTIAL_FILL", "CANCEL_ACK",
                    "LATE_FILL", "UNKNOWN_POST"}
        for order, payload, size in conn.execute(
            "SELECT i.order_intent_id, i.intent_json,"
            " COALESCE(r.filled_size, 0) FROM basket_target_order_intents i"
            " LEFT JOIN basket_target_order_reconciliations r"
            "   ON r.order_intent_id = i.order_intent_id"
            " WHERE i.live_submit_authorized = 1"
        ):
            try:
                candidate = json.loads(payload).get("execution_candidate")
            except (TypeError, ValueError):
                continue
            if candidate != "MAKER":
                continue
            posted += 1
            events = venue.get(str(order), set())
            if events & AT_VENUE:
                reached += 1
                filled += 1 if float(size or 0.0) > 0 else 0
        for payload, in conn.execute(
            "SELECT event_json FROM basket_target_order_events"
            " WHERE event_type='NEVER_REACHED_EXCHANGE'"
        ):
            try:
                reason = str(json.loads(payload).get("reason") or "?")
            except (TypeError, ValueError):
                reason = "?"
            gated[reason] = gated.get(reason, 0) + 1
    except sqlite3.Error:
        pass

    # Legs of live baskets that opened, de-duplicated by station-day-bucket
    # because a rung cancelled by the lifecycle and re-posted is one leg
    # wanted, not two.
    wanted: dict[tuple[str, str], set[str]] = {}
    held: dict[tuple[str, str], set[str]] = {}
    try:
        for station, day, bucket, payload, size in conn.execute(
            "SELECT d.station_id, d.event_date, i.bucket, i.intent_json,"
            " COALESCE(r.filled_size, 0)"
            " FROM basket_target_order_intents i"
            " JOIN auto_trade_decisions d ON d.decision_id = i.decision_id"
            " LEFT JOIN basket_target_order_reconciliations r"
            "   ON r.order_intent_id = i.order_intent_id"
            " WHERE i.live_submit_authorized = 1"
        ):
            try:
                if json.loads(payload).get("execution_candidate") != "MAKER":
                    continue
            except (TypeError, ValueError):
                continue
            key = (str(station), str(day))
            wanted.setdefault(key, set()).add(str(bucket))
            if float(size or 0.0) > 0:
                held.setdefault(key, set()).add(str(bucket))
    except sqlite3.Error:
        pass
    opened = [k for k in wanted if held.get(k)]
    seated_wanted = sum(len(wanted[k]) for k in opened)
    seated_held = sum(len(held[k]) for k in opened)
    seated_rate = (seated_held / seated_wanted) if seated_wanted else None

    ledger_rate = (kept / intended) if intended else None
    live_rate = (filled / reached) if reached else None
    return {
        "live_seated_fill_rate": (round(seated_rate, 4)
                                  if seated_rate is not None else None),
        "live_opened_baskets": len(opened),
        "live_attempted_baskets": len(wanted),
        "ledger_intended_legs": intended, "ledger_kept_legs": kept,
        "ledger_fill_rate": (round(ledger_rate, 4)
                             if ledger_rate is not None else None),
        "live_posted_legs": posted, "live_reached_venue": reached,
        "live_filled": filled,
        "live_fill_rate": (round(live_rate, 4)
                           if live_rate is not None else None),
        "live_never_reached_by_reason": dict(
            sorted(gated.items(), key=lambda kv: -kv[1])),
        "ledger_over_live": (
            round(ledger_rate / seated_rate, 4)
            if ledger_rate and seated_rate else None),
        "note": (
            "horizons differ: shadow 60min, ledger and live rest to"
            " resolution; only the live column is like-for-like"
        ),
    }


def ledger_reading(conn: sqlite3.Connection) -> dict:
    """The same asymmetry, as the ledger's own simulation produced it."""

    rows = conn.execute(
        "SELECT p.stage, p.intended_legs_json il, p.leg_shares_json ls,"
        " s.settled_bucket sb FROM paper_trade_settlement s"
        " JOIN paper_trade_position p USING(position_id)"
        " WHERE p.intended_legs_json NOT IN ('', '{}')"
    ).fetchall()
    tally: dict[str, dict[str, list[int]]] = {}
    for row in rows:
        try:
            legs = json.loads(row["il"] or "{}") or {}
            kept = json.loads(row["ls"] or "{}") or {}
        except (TypeError, ValueError):
            continue
        stage = tally.setdefault(
            str(row["stage"]), {"winner": [0, 0], "loser": [0, 0]}
        )
        for bucket in legs:
            slot = stage[
                "winner" if str(bucket) == str(row["sb"]) else "loser"
            ]
            slot[0] += 1
            slot[1] += 1 if float(kept.get(bucket, 0.0) or 0.0) > 0 else 0
    out = []
    total = {"winner": [0, 0], "loser": [0, 0]}
    for stage, cell in sorted(tally.items()):
        for side in ("winner", "loser"):
            total[side][0] += cell[side][0]
            total[side][1] += cell[side][1]
        win = cell["winner"][1] / cell["winner"][0] if cell["winner"][0] else None
        lose = cell["loser"][1] / cell["loser"][0] if cell["loser"][0] else None
        out.append({
            "stage": stage,
            "winner_legs": cell["winner"][0],
            "loser_legs": cell["loser"][0],
            "winner_fill_rate": None if win is None else round(win, 4),
            "loser_fill_rate": None if lose is None else round(lose, 4),
            "asymmetry": (
                round(win / lose, 4) if win is not None and lose else None
            ),
        })
    win = total["winner"][1] / total["winner"][0] if total["winner"][0] else None
    lose = total["loser"][1] / total["loser"][0] if total["loser"][0] else None
    return {
        "by_stage": out,
        "winner_legs": total["winner"][0],
        "loser_legs": total["loser"][0],
        "winner_fill_rate": None if win is None else round(win, 4),
        "loser_fill_rate": None if lose is None else round(lose, 4),
        "asymmetry": round(win / lose, 4) if win is not None and lose else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    conn = _connect(Path(args.db))
    try:
        shadow = shadow_reading(conn)
        ledger = ledger_reading(conn)
        level = level_reading(conn)
    finally:
        conn.close()

    # The shadow lane's range across every lead band is the interval the
    # simulation has to live inside.  Outside it, the ledger's money column
    # is describing a market nobody has observed.
    observed = [
        cell["asymmetry"] for cell in shadow["by_lead_hours"]
        if cell["asymmetry"] is not None
    ]
    verdict = None
    if observed and ledger["asymmetry"] is not None:
        low, high = min(observed), max(observed)
        verdict = {
            "shadow_asymmetry_range": [round(low, 4), round(high, 4)],
            "ledger_asymmetry": ledger["asymmetry"],
            "inside_observed_range": bool(low <= ledger["asymmetry"] <= high),
            "factor_outside": (
                None if low <= ledger["asymmetry"] <= high
                else round(low / ledger["asymmetry"], 2)
                if ledger["asymmetry"] > 0 else None
            ),
        }
    receipt = {
        "schema": "maker_fill_model_calibration_v1",
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "shadow": shadow,
        "ledger": ledger,
        "level": level,
        "verdict": verdict,
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False))
        return 0
    print(f"影子车道 {shadow['orders']} 条挂单，价格规则 {LADDER_RULE}")
    print(f"{'提前量':>14}{'赢桶单':>9}{'输桶单':>9}{'赢成交':>9}{'输成交':>9}{'赢/输':>8}")
    for cell in shadow["by_lead_hours"]:
        print(
            f"{cell['lead_band']:>14}{cell['winner_orders']:9d}"
            f"{cell['loser_orders']:9d}{100*cell['winner_fill_rate']:8.1f}%"
            f"{100*cell['loser_fill_rate']:8.1f}%{cell['asymmetry']:8.2f}"
        )
    print(f"\n账本模拟：赢腿 {ledger['winner_legs']} 条 成交 "
          f"{100*(ledger['winner_fill_rate'] or 0):.1f}%，输腿 "
          f"{ledger['loser_legs']} 条 成交 "
          f"{100*(ledger['loser_fill_rate'] or 0):.1f}%，"
          f"赢/输 {ledger['asymmetry']}")
    if shadow["chase_orders_settled"]:
        print(f"追价臂 {CHASE_RULE}：{shadow['chase_orders_settled']} 条已判，"
              f"赢/输 {shadow['chase_asymmetry_60min']}")
    else:
        print(f"追价臂 {CHASE_RULE}：仍在积累（0 条已判）")
    ta = shadow["take_after"]
    if ta["crossed"]["fills"] or ta["maker"]["fills"]:
        print(
            f"先挂后吃臂 {TAKE_AFTER_RULE}："
            f"挂到 {ta['maker']['fills']} 单均价 {ta['maker']['mean_price_paid']}，"
            f"改吃 {ta['crossed']['fills']} 单均价 {ta['crossed']['mean_price_paid']}，"
            f"对照直接吃单均价 {ta['taker']['mean_price_paid']}"
        )
    else:
        print(f"先挂后吃臂 {TAKE_AFTER_RULE}：仍在积累（0 单成交）")
    print(
        f"\n成交水平：账本想买 {level['ledger_intended_legs']} 腿、买到 "
        f"{level['ledger_kept_legs']} = "
        f"{100*(level['ledger_fill_rate'] or 0):.1f}%；"
        f"实盘发出 {level['live_posted_legs']} 腿、到过交易所 "
        f"{level['live_reached_venue']}、成交 {level['live_filled']} = "
        f"{100*(level['live_fill_rate'] or 0):.1f}%"
        f"\n        同口径（只看开了仓的篮子）：实盘 "
        f"{100*(level['live_seated_fill_rate'] or 0):.1f}%"
        f"（{level['live_opened_baskets']}/{level['live_attempted_baskets']} 篮开仓）"
        f" ← 这才是 position 的分母"
    )
    if level["ledger_over_live"]:
        print(f"        账本/实盘 = {level['ledger_over_live']}×"
              f"（视界不同：影子 60 分钟，账本与实盘挂到结算）")
    if level["live_never_reached_by_reason"]:
        gated = "、".join(
            f"{k} {v}" for k, v in
            list(level["live_never_reached_by_reason"].items())[:4])
        print(f"        实盘没到交易所的腿按原因：{gated}"
              f"（站级门控，按设计工作，不计入上面的分母）")
    if verdict:
        print(
            f"影子观测区间 {verdict['shadow_asymmetry_range']}；"
            f"账本 {verdict['ledger_asymmetry']}："
            + ("区间内" if verdict["inside_observed_range"]
               else f"区间外 {verdict['factor_outside']}×")
        )
    print(f"回执 {RECEIPT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
