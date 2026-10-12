"""配对挂单线 (pair maker) -- charter 269.

User rulings 2026-10-12/13: a YES+NO pair lane, post-only only, pair cost under
1 and held at 0.95; then 「成形即挂、众数桶、允许 NO 单边、0.95 作挂价上限」.

* When: the first pass at which the station-day's book is FORMED -- every bucket
  quoted on both sides (best bid, and a best ask of at least ``ask_min_shares``)
  and the mids summing to 0.7-1.4.  Before that the ladder carries no market
  view (listing books sum to ~3.0; quoting F's or F_v3's bucket at listing lost
  in all 36 backtest cells, research round 60b).
* Where: the market's mode bucket at formation (the largest mid; the lower
  bucket on a tie), one bucket a station-day.  C stations only by default.
* What: two static post-only ladders against the formation mid ``m``, each
  rung ``rung_shares`` shares, live ``window_hours`` (GTD) and never past the
  ``min_hours_to_settlement`` line:
      YES rungs  x_k = m - d - o_k
      NO rungs   y_k = PC - (m - d) - o_k
  with ``d = quote_offset`` and ``o = rung_offsets``.  Every YES price is under
  ``m - d`` and every NO price under ``PC - (m - d)``, so ANY YES fill with ANY
  NO fill costs at most ``PC`` (``pair_cost_cap``, 0.95): the cap is a quote
  ceiling, not a profit assumption (the ruling).  A rung must be post-only
  legal on the standing book (YES under the YES ask, NO under 1 - the YES bid)
  or it is not posted.
* Exposure: a single filled side is held to settlement -- YES or NO on the
  mode (the ruling allows the mode's NO).  The station-day's rungs cost at most
  ``max_station_day_usdc``; the lane's open rungs at most
  ``max_cash_fraction`` of the free cash.  Deeper rungs are dropped first.
* Books: per station-day x bucket ledger of YES/NO shares and cost, matched
  pairs, pair cost and the unmatched side, refreshed every pass from the
  order reconciliations; the plan closes when its window has passed.

Research (docs/research/benchmark_account_0x8fbd_20261008, rounds 58-60;
docs/design/pair_maker_lane_design_20261012.md): over every booked
station-day, quoting the mode at formation with d 0.05 for 6 h returned
+14.1% of filled capital (+0.43 U a station-day [-0.02, +0.86], both halves
positive) under a trade-through fill model, mostly from the mode's single NO
leg; matched pairs were 5-13% of shares.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import content_sha256
from .delta_v2_runtime import CHARTER_83_MIN_NOTIONAL, EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC
from .exit_orders import Transport
from .lane_orders import send_lane_buy
from .live_station_scope import admission as live_station_admission
from .p1_tail_lane import _ws_manifest_root
from .production import GTD_SIGNING_MARGIN_SECONDS, VENUE_GTD_MINIMUM_HORIZON_SECONDS
from .proper_bet_lane import _book, _free_cash, _window_refusal, market_ids
from .strategy_lanes import LANE_PAIR_MAKER, PAIR_MAKER_DEFAULT_SETTINGS, lane_settings
from .tiebreak_lane import resting_order_fields

LANE = LANE_PAIR_MAKER
LANE_TITLE = "配对挂单线"
RULE = "post_only_pair_mode_at_formation_v1"
STRATEGY_ID = "bayes_basket_target_v1"
SCHEMA = "pair_maker_lane_plan_v1"
EXECUTION_CANDIDATE = "MAKER"
INTENT_PREFIX = "pairmk"
PLAN_TABLE = "pair_maker_plans"
SIDE_YES, SIDE_NO = "YES", "NO"
PLAN_ACTIVE, PLAN_DONE = "active", "done"
OPEN_STATES = ("prepared", "submitting", "submitted", "reconcile_required")
SHARE_PRECISION = 2

PLAN_DDL = (
    f"CREATE TABLE IF NOT EXISTS {PLAN_TABLE} ("
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " bucket TEXT NOT NULL,"
    " plan_state TEXT NOT NULL,"
    " formed_at_utc TEXT NOT NULL,"
    " window_ends_utc TEXT NOT NULL,"
    " plan_json TEXT NOT NULL,"
    " ledger_json TEXT,"
    " last_pass_at_utc TEXT,"
    " PRIMARY KEY (station_id, event_date))"
)


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    conn.execute(PLAN_DDL)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _best_ask(row: Mapping[str, Any], min_shares: float) -> float | None:
    prices = []
    for level in row.get("ask_levels") or []:
        try:
            if float(level.get("shares", 0.0)) >= float(min_shares):
                prices.append(float(level["price"]))
        except (TypeError, ValueError, KeyError):
            continue
    return min(prices) if prices else None


def _best_bid(row: Mapping[str, Any]) -> float | None:
    if not row.get("bid_available"):
        return None
    try:
        bid = float(row.get("bid_price"))
    except (TypeError, ValueError):
        return None
    return bid if bid > 0.0 else None


def formation(book_rows: list[Mapping[str, Any]], cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Is the ladder formed, and if so its mode bucket and that bucket's quotes.

    Formed = every bucket two-sided (bid, and an ask of ``ask_min_shares``) and
    the mids summing inside ``formation_mid_sum``.  The mode is the largest
    mid, the lower ordinal on a tie.
    """

    low, high = (float(v) for v in cfg["formation_mid_sum"])
    rows = sorted(book_rows, key=lambda r: int(r.get("ordinal") or 0))
    if len(rows) < 3:
        return {"formed": False, "reason": "ladder_short"}
    mids: list[tuple[float, int, Mapping[str, Any], float, float]] = []
    for row in rows:
        bid, ask = _best_bid(row), _best_ask(row, float(cfg["ask_min_shares"]))
        if bid is None or ask is None:
            return {"formed": False, "reason": "one_sided_bucket"}
        mids.append(((bid + ask) / 2.0, int(row.get("ordinal") or 0), row, bid, ask))
    total = sum(m for m, *_ in mids)
    if not low <= total <= high:
        return {"formed": False, "reason": f"mid_sum_outside:{total:.2f}"}
    mid, _ordinal, row, bid, ask = max(mids, key=lambda t: (t[0], -t[1]))
    return {"formed": True, "bucket": str(row.get("bucket")), "mid": mid, "bid": bid, "ask": ask,
            "mid_sum": round(total, 4), "tick": float(row.get("price_tick_size") or 0.001)}


def _floor_tick(price: float, tick: float) -> float:
    steps = math.floor(price / tick + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(steps * tick, decimals)


def ladder(form: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The two static post-only ladders against the formation mid.

    Rung k: YES at ``m - d - o_k``, NO at ``PC - (m - d) - o_k``, each floored
    to the tick (flooring only lowers a price, so the pair cap still holds),
    dropped when not post-only legal on the book or not a positive price.
    Ordered shallow first, alternating sides, so a budget cut drops the deepest.
    """

    pc, d = float(cfg["pair_cost_cap"]), float(cfg["quote_offset"])
    tick = float(form["tick"])
    m, bid, ask = float(form["mid"]), float(form["bid"]), float(form["ask"])
    rungs: list[dict[str, Any]] = []
    for k, off in enumerate(float(o) for o in cfg["rung_offsets"]):
        yes = _floor_tick(m - d - off, tick)
        no = _floor_tick(pc - (m - d) - off, tick)
        # A rung exactly one tick inside the book is legal: compare with a
        # tolerance below the tick, never with raw floats.
        eps = tick / 10.0
        if tick - eps <= yes <= ask - tick + eps:
            rungs.append({"side": SIDE_YES, "k": k, "price": yes})
        if tick - eps <= no <= (1.0 - bid) - tick + eps:
            rungs.append({"side": SIDE_NO, "k": k, "price": no})
    return rungs


def _shares(price: float, cfg: Mapping[str, Any], min_size: float) -> float:
    shares = max(float(cfg["rung_shares"]), float(min_size))
    floor_usdc = EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC if CHARTER_83_MIN_NOTIONAL == "on" else 0.0
    if shares * price < floor_usdc - 1e-9:
        shares = math.ceil(floor_usdc / price * 10 ** SHARE_PRECISION - 1e-9) / 10 ** SHARE_PRECISION
    return round(shares, SHARE_PRECISION)


def ledger(conn: sqlite3.Connection, plan: Mapping[str, Any]) -> dict[str, Any]:
    """The bucket's book: what each side has filled, matched pairs, pair cost, unmatched."""

    out: dict[str, Any] = {}
    for side in (SIDE_YES, SIDE_NO):
        ids = [r["intent_id"] for r in plan.get("rungs") or [] if r["side"] == side and r.get("intent_id")]
        if ids:
            marks = ",".join("?" for _ in ids)
            shares, cost = conn.execute(
                f"SELECT COALESCE(SUM(filled_size),0), COALESCE(SUM(filled_notional_usdc),0)"
                f" FROM basket_target_order_reconciliations WHERE order_intent_id IN ({marks})", tuple(ids),
            ).fetchone()
            open_row = conn.execute(
                f"SELECT 1 FROM basket_target_order_intents WHERE order_intent_id IN ({marks})"
                f" AND state IN ({','.join('?' for _ in OPEN_STATES)}) LIMIT 1", (*ids, *OPEN_STATES),
            ).fetchone()
        else:
            shares, cost, open_row = 0.0, 0.0, None
        out[side] = {"shares": round(float(shares or 0.0), 4), "cost_usdc": round(float(cost or 0.0), 6),
                     "resting": open_row is not None}
    y, n = out[SIDE_YES], out[SIDE_NO]
    matched = min(y["shares"], n["shares"])
    avg_y = y["cost_usdc"] / y["shares"] if y["shares"] else None
    avg_n = n["cost_usdc"] / n["shares"] if n["shares"] else None
    out["matched_shares"] = round(matched, 4)
    out["pair_cost"] = round(avg_y + avg_n, 4) if avg_y is not None and avg_n is not None else None
    out["unmatched_shares"] = round(abs(y["shares"] - n["shares"]), 4)
    out["unmatched_side"] = (SIDE_YES if y["shares"] > n["shares"] else SIDE_NO if n["shares"] > y["shares"] else None)
    total = y["shares"] + n["shares"]
    out["unmatched_ratio"] = round(out["unmatched_shares"] / total, 4) if total else None
    return out


def _candidates(conn: sqlite3.Connection, *, now_utc: str, cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Listed station-days still ahead (today .. today + 3 UTC) with no plan yet."""

    today = _utc(now_utc).date()
    rows = conn.execute(
        "SELECT station_id, event_date, MAX(settlement_unit), MAX(settlement_timezone)"
        " FROM basket_target_market_registry WHERE event_date BETWEEN ? AND ? AND station_id<>''"
        " GROUP BY station_id, event_date",
        (today.isoformat(), (today + timedelta(days=3)).isoformat()),
    ).fetchall()
    planned = {(str(s), str(d)) for s, d in conn.execute(f"SELECT station_id, event_date FROM {PLAN_TABLE}")}
    units = {str(u).upper() for u in cfg["units"]}
    out = []
    for station, day, unit, zone in rows:
        if (str(station), str(day)) in planned or not zone:
            continue
        if str(unit or "").upper()[:1] not in units:
            continue
        out.append({"station_id": str(station), "event_date": str(day), "settlement_timezone": str(zone)})
    return out


def _open_lane_cost(conn: sqlite3.Connection) -> float:
    """What the lane's still-resting rungs could cost if they all filled."""

    row = conn.execute(
        "SELECT COALESCE(SUM(i.limit_price * i.requested_size), 0) FROM basket_target_order_intents i"
        f" WHERE i.order_intent_id LIKE '{INTENT_PREFIX}_%' AND i.state IN ({','.join('?' for _ in OPEN_STATES)})",
        OPEN_STATES,
    ).fetchone()
    return float(row[0] or 0.0)


def execute_pair_maker(
    target_conn: sqlite3.Connection,
    *,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    admission: Callable[[str, sqlite3.Connection], Mapping[str, Any]] | None = None,
    ws_manifest_path: Any = None,
) -> dict[str, Any]:
    """Refresh every plan's books, then plan and post the station-days whose book just formed."""

    ensure_lane_schema(target_conn)
    cfg = {**PAIR_MAKER_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    if str(cfg.get("rule")) != RULE:
        raise ValueError(f"pair_maker_rule_unknown:{cfg.get('rule')}")
    if not 0.0 < float(cfg["pair_cost_cap"]) <= 1.0:
        raise ValueError(f"pair_maker_pair_cost_cap_out_of_range:{cfg['pair_cost_cap']}")
    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    live = cfg.get("live_ws_book", True) is True
    root = _ws_manifest_root(ws_manifest_path) if live else None
    now = _utc(now_utc)

    # 1. Books of the plans already made; a plan whose window has passed closes.
    refreshed = closed = 0
    for station, day, plan_json, state, ends in target_conn.execute(
        f"SELECT station_id, event_date, plan_json, plan_state, window_ends_utc FROM {PLAN_TABLE} WHERE plan_state=?",
        (PLAN_ACTIVE,),
    ).fetchall():
        plan = json.loads(plan_json)
        book = ledger(target_conn, plan)
        still_open = book[SIDE_YES]["resting"] or book[SIDE_NO]["resting"]
        final = PLAN_DONE if (now >= _utc(ends) and not still_open) else PLAN_ACTIVE
        target_conn.execute(
            f"UPDATE {PLAN_TABLE} SET ledger_json=?, plan_state=?, last_pass_at_utc=? WHERE station_id=? AND event_date=?",
            (json.dumps(book, sort_keys=True), final, now_utc, station, day),
        )
        refreshed += 1
        closed += final == PLAN_DONE

    # 2. New plans on the books that formed.
    cash, cash_refusal = _free_cash(target_conn, now_utc=now_utc)
    budget = None
    if cash is not None:
        budget = max(0.0, float(cfg["max_cash_fraction"]) * float(cash) - _open_lane_cost(target_conn))
    waiting: Counter[str] = Counter()
    receipts: list[dict[str, Any]] = []
    planned = 0
    for cand in _candidates(target_conn, now_utc=now_utc, cfg=cfg):
        if planned >= int(cfg["max_new_plans_per_pass"]):
            waiting["pass_plan_limit"] += 1
            break
        station, day, zone = cand["station_id"], cand["event_date"], cand["settlement_timezone"]
        refusal, hours = _window_refusal(target_conn, station_id=station, event_date=day, settlement_timezone=zone,
                                         now_utc=now_utc, min_hours=float(cfg["min_hours_to_settlement"]), gate=gate)
        if refusal:
            waiting[refusal.split(":")[0]] += 1
            continue
        book, book_refusal = _book(target_conn, root, station_id=station, event_date=day, now_utc=now_utc,
                                   fresh_seconds=float(cfg["send_book_fresh_seconds"]), live=live)
        if book is None:
            waiting[str(book_refusal)] += 1
            continue
        form = formation(list(book.get("buckets") or []), cfg)
        if not form["formed"]:
            waiting[f"not_formed:{form['reason'].split(':')[0]}"] += 1
            continue
        room = (float(hours) - float(cfg["min_hours_to_settlement"])) * 3600.0
        rest = min(float(cfg["window_hours"]) * 3600.0, room)
        if rest < VENUE_GTD_MINIMUM_HORIZON_SECONDS + GTD_SIGNING_MARGIN_SECONDS:
            waiting["window_closing"] += 1
            continue
        until = _iso(now + timedelta(seconds=rest))
        ids = market_ids(target_conn, station, day, form["bucket"])
        tokens = {SIDE_YES: ids["yes_token"], SIDE_NO: ids["no_token"]}
        if not tokens[SIDE_YES] or not tokens[SIDE_NO]:
            waiting["token_missing"] += 1
            continue
        min_size = float(ids.get("min_size") or 5.0)
        rungs = []
        spend = 0.0
        cap = float(cfg["max_station_day_usdc"])
        if budget is not None:
            cap = min(cap, budget)
        for rung in ladder(form, cfg):
            shares = _shares(float(rung["price"]), cfg, min_size)
            cost = round(shares * float(rung["price"]), 6)
            if spend + cost > cap + 1e-9:
                continue
            spend += cost
            rungs.append({**rung, "shares": shares, "cost_usdc": cost, "token_id": tokens[rung["side"]]})
        if not rungs:
            waiting["budget" if cash is not None else f"cash:{cash_refusal}"] += 1
            continue
        plan_id = f"{INTENT_PREFIX}_plan_" + content_sha256({"station": station, "day": day, "formed": now_utc})[:24]
        plan = {"schema": SCHEMA, "rule": RULE, "plan_id": plan_id, "station_id": station, "event_date": day,
                "settlement_timezone": zone, "bucket": form["bucket"], "formation": form, "window_ends_utc": until,
                "hours_to_settlement": hours, "settings": {k: cfg[k] for k in (
                    "pair_cost_cap", "quote_offset", "rung_offsets", "rung_shares", "window_hours",
                    "max_station_day_usdc", "max_cash_fraction")},
                "book_id": book.get("snapshot_id"), "book_source": book.get("book_source"), "rungs": []}
        for rung in rungs:
            intent_id = f"{INTENT_PREFIX}_" + content_sha256({"plan": plan_id, "side": rung["side"], "k": rung["k"]})[:32]
            venue = {**resting_order_fields(now_utc=now_utc, limit_price=float(rung["price"]), belief_fresh_until_utc=until),
                     "expiration_basis": "pair_maker_window"}
            entry = send_lane_buy(
                target_conn, lane=LANE, intent_id=intent_id, station_id=station, event_date=day,
                settlement_timezone=zone, bucket=form["bucket"], token_id=rung["token_id"], price=float(rung["price"]),
                shares=float(rung["shares"]), side=rung["side"], execution_candidate=EXECUTION_CANDIDATE,
                expiration=int(venue["expiration"]), tick_size=float(form["tick"]),
                intent_body={"schema": SCHEMA, "rule": RULE, "strategy_id": STRATEGY_ID, "plan_id": plan_id,
                             "order_type": venue["order_type"], "post_only": venue["post_only"],
                             "expiration_basis": venue["expiration_basis"],
                             # Charter 269: the rung lives its window, not charter 94's
                             # bid-anchored ladder; the resting pass syncs it and leaves it.
                             "own_horizon": True,
                             "rung": {k: rung[k] for k in ("side", "k", "price", "shares")},
                             "formation": form},
                transport=transport, live_submit_authorized=live_submit_authorized,
                decision_details={"schema": "pair_maker_lane_decision_v1", "lane": LANE, "title": LANE_TITLE,
                                  "rule": RULE, "decision_signature": {"stage": "MARKET", "lane": LANE, "rule": RULE},
                                  "plan_id": plan_id, "formation": form},
            )
            plan["rungs"].append({**rung, "intent_id": intent_id, "state": entry.get("state"),
                                  "reason": entry.get("reason")})
            receipts.append({"station_id": station, "event_date": day, "bucket": form["bucket"], "side": rung["side"],
                             "price": rung["price"], "shares": rung["shares"], "state": entry.get("state"),
                             "reason": entry.get("reason")})
        target_conn.execute(
            f"INSERT OR REPLACE INTO {PLAN_TABLE} (station_id, event_date, bucket, plan_state, formed_at_utc,"
            " window_ends_utc, plan_json, ledger_json, last_pass_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            (station, day, form["bucket"], PLAN_ACTIVE, now_utc, until, json.dumps(plan, ensure_ascii=False, sort_keys=True),
             None, now_utc),
        )
        if budget is not None:
            budget = max(0.0, budget - spend)
        planned += 1
    return {"schema": "pair_maker_lane_receipt_v1", "lane": LANE, "title": LANE_TITLE, "rule": RULE,
            "planned": planned, "rungs": len(receipts), "refreshed": refreshed, "closed": closed,
            "cash": cash, "budget_left": budget, "waiting": dict(sorted(waiting.items())), "receipts": receipts}


__all__ = ["LANE", "LANE_TITLE", "PLAN_TABLE", "RULE", "execute_pair_maker", "formation", "ladder", "ledger"]
