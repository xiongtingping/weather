"""配对挂单线 (pair maker) -- charter 269.

User rulings 2026-10-12/13: a YES+NO pair lane, post-only only, pair cost under
1 and held at 0.95; then 「成形即挂、众数桶、允许 NO 单边、0.95 作挂价上限」.

* When: the first pass at which the station-day's book is FORMED -- every bucket
  quoted on both sides (best bid, and a best ask of at least ``ask_min_shares``)
  and the mids summing to 0.7-1.4.  Before that the ladder carries no market
  view (listing books sum to ~3.0; quoting F's or F_v3's bucket at listing lost
  in all 36 backtest cells, research round 60b).
* Where: the market's mode bucket at formation (the largest mid; the lower
  bucket on a tie), one bucket a station-day, C and F stations (user 10-13
  「全可以做」).
* What (rule v2, user 10-13 「允许先用买一+1积累单边，成交后动态计算成本，另一个必须在
  这个成本下」「尽可能的提高吞吐」): the lead side (``lead_side``, YES) rests one
  post-only rung at its own best bid + 1 tick and accumulates while its
  unmatched cost is under ``max_unmatched_usdc``; once it holds more than the
  follow side, the follow side rests one rung for the unmatched shares at
  min(its best bid + 1 tick, ``pair_cost_cap`` - the lead's average cost), so
  every completed pair costs at most the cap (0.95, a quote ceiling).  Each
  side is also capped against the other side's dearest resting rung.  Rungs
  live ``rung_minutes`` (GTD) and are re-priced by re-posting on the next
  pass -- throughput -- inside ``quote_window_hours`` from formation and
  never past the ``min_hours_to_settlement`` line.  ``stations`` limits the
  lane to an allowlist (the single-station MVP).
* Budget: filled plus resting cost a station-day at most
  ``max_station_day_usdc``; the lane's resting rungs at most
  ``max_cash_fraction`` of the free cash.
* Unmatched (amendment four, user 10-13 「0.95 封顶。未配平控制：两个都用」): the
  lead stops new rungs at ``max_unmatched_usdc`` of unmatched cost OR, once
  both sides hold more than ``ratio_min_total_shares`` together, at an
  unmatched ratio of ``max_unmatched_ratio`` (10%); ``warn_unmatched_ratio``
  (8%) is flagged in the pass status; the follow keeps pairing either way.
* Merge (amendment three, user 10-13 「做 merge」): once the matched pairs not
  yet merged reach ``min_merge_shares`` the lane asks the gateway to merge
  them back into pUSD (``/merge-positions``: proxy wallet -> v2
  NegRiskCtfCollateralAdapter); ``merge`` = ``simulate`` (default: the exact
  transaction is gas-estimated and recorded, nothing is sent) or ``live``
  (also needs the gateway's ``PM_PAIR_MERGE_LIVE``).  The transaction hash is
  recorded at broadcast and its receipt read on a later pass; a merge whose
  broadcast is unknown holds every further merge of that plan.  Merged pairs
  leave the station-day cap (capital rolls) and are taken off the ledger's
  booked position per token, so the departed-order reconciliation compares
  the account with what it should still hold.
* Books: per station-day x bucket ledger of YES/NO shares and cost, matched
  pairs, pair cost and the unmatched side, refreshed every pass from the
  order reconciliations; the plan closes when its window has passed and
  nothing rests.

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
RULE = "post_only_pair_bid_plus_one_lead_follow_v2"
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


MERGE_TABLE = "pair_maker_merges"
MERGE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {MERGE_TABLE} ("
    " merge_id TEXT PRIMARY KEY,"
    " plan_id TEXT NOT NULL,"
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " bucket TEXT NOT NULL,"
    " condition_id TEXT NOT NULL,"
    " yes_token TEXT NOT NULL,"
    " no_token TEXT NOT NULL,"
    " shares REAL NOT NULL,"
    " mode TEXT NOT NULL,"
    " state TEXT NOT NULL,"
    " tx_hash TEXT,"
    " response_json TEXT,"
    " created_at_utc TEXT NOT NULL,"
    " updated_at_utc TEXT NOT NULL)"
)
# SIMULATED: estimated only; SENT: broadcast, receipt not read yet; MERGED: receipt
# success; FAILED: refused, simulation reverted or receipt reverted; UNKNOWN:
# broadcast-or-not unknown (holds the plan's merges until resolved by hand).
MERGE_COUNTED_STATES = ("MERGED",)
MERGE_HOLDING_STATES = ("SENT", "UNKNOWN")


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    conn.execute(PLAN_DDL)
    conn.execute(MERGE_DDL)


def merged_shares_for_token(conn: sqlite3.Connection, token_id: str) -> float:
    """Shares of one token this lane has merged away (receipt success)."""

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (MERGE_TABLE,)).fetchone() is None:
        return 0.0
    row = conn.execute(
        f"SELECT COALESCE(SUM(shares),0) FROM {MERGE_TABLE} WHERE (yes_token=? OR no_token=?)"
        f" AND state IN ({','.join('?' for _ in MERGE_COUNTED_STATES)})",
        (str(token_id), str(token_id), *MERGE_COUNTED_STATES),
    ).fetchone()
    return float((row[0] if row else 0.0) or 0.0)


def _plan_merges(conn: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
    cur = conn.execute(f"SELECT * FROM {MERGE_TABLE} WHERE plan_id=? ORDER BY created_at_utc", (plan_id,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def default_merge_transport(path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    import sys  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    root = str(_Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from clob_order_service_client import ClobOrderServiceClient  # noqa: PLC0415

    if path not in ("/merge-positions", "/merge-receipt"):
        raise ValueError("pair_maker_merge_unknown_path")
    return ClobOrderServiceClient(timeout_seconds=30)._post(path, dict(payload))


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


def _intent_states(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Per intent: filled shares, filled notional, and whether it is still at the venue."""

    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    out = {i: {"filled": 0.0, "notional": 0.0, "open": False} for i in ids}
    for iid, size, notional in conn.execute(
        f"SELECT order_intent_id, COALESCE(SUM(filled_size),0), COALESCE(SUM(filled_notional_usdc),0)"
        f" FROM basket_target_order_reconciliations WHERE order_intent_id IN ({marks}) GROUP BY order_intent_id",
        tuple(ids),
    ):
        out[str(iid)].update(filled=float(size or 0.0), notional=float(notional or 0.0))
    for (iid,) in conn.execute(
        f"SELECT order_intent_id FROM basket_target_order_intents WHERE order_intent_id IN ({marks})"
        f" AND state IN ({','.join('?' for _ in OPEN_STATES)})", (*ids, *OPEN_STATES),
    ):
        out[str(iid)]["open"] = True
    return out


def _candidates(conn: sqlite3.Connection, *, now_utc: str, cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Listed station-days still ahead (today .. today + 3 UTC) with no plan yet."""

    today = _utc(now_utc).date()
    # Two covering-index reads (query-plan guard, 2026-10-15).  On the date
    # alone this scanned all 8.5M registry rows, 25 s every cycle; with the
    # station list it still read ~270k rows back from the table (every
    # registry refresh rewrites the window) for the unit and the zone.  The
    # zone comes off the (station, day, zone, ...) index; the unit is a
    # station's own (charter 53) and comes off the (station, unit) index.
    # Every listed station is registered.
    units_by_station = dict(conn.execute(
        "SELECT station_id, MAX(settlement_unit) FROM basket_target_market_registry"
        " WHERE station_id IN (SELECT station_id FROM calibration_station_registry)"
        " GROUP BY station_id"
    ).fetchall())
    rows = [
        (station, day, units_by_station.get(station), zone)
        for station, day, zone in conn.execute(
            "SELECT station_id, event_date, MAX(settlement_timezone)"
            " FROM basket_target_market_registry"
            " WHERE station_id IN (SELECT station_id FROM calibration_station_registry)"
            " AND event_date BETWEEN ? AND ? AND station_id<>''"
            " GROUP BY station_id, event_date",
            (today.isoformat(), (today + timedelta(days=3)).isoformat()),
        ).fetchall()
    ]
    planned = {(str(s), str(d)) for s, d in conn.execute(f"SELECT station_id, event_date FROM {PLAN_TABLE}")}
    units = {str(u).upper()[:1] for u in cfg["units"]}
    # The MVP (user 10-13 「先用单站点进行mvp最小单元测试」): an empty list is every station.
    allow = {str(x).upper() for x in (cfg.get("stations") or [])}
    out = []
    for station, day, unit, zone in rows:
        if (str(station), str(day)) in planned or not zone:
            continue
        if str(unit or "").upper()[:1] not in units:
            continue
        if allow and str(station) not in allow:
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


def bucket_quote(book_rows: list[Mapping[str, Any]], bucket: str, cfg: Mapping[str, Any]) -> dict[str, Any] | None:
    """The plan bucket's standing quotes (bid, ask, mid, tick), or None when it is not two-sided."""

    row = next((r for r in book_rows if str(r.get("bucket")) == str(bucket)), None)
    if row is None:
        return None
    bid, ask = _best_bid(row), _best_ask(row, float(cfg["ask_min_shares"]))
    if bid is None or ask is None:
        return None
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2.0, "tick": float(row.get("price_tick_size") or 0.001)}


def side_quotes(quote: Mapping[str, Any]) -> dict[str, tuple[float, float]]:
    """(bid, ask) of each side: NO is the YES book mirrored (NO bid 1 - YES ask, NO ask 1 - YES bid)."""

    return {SIDE_YES: (float(quote["bid"]), float(quote["ask"])),
            SIDE_NO: (1.0 - float(quote["ask"]), 1.0 - float(quote["bid"]))}


def lead_follow_plan(
    plan: Mapping[str, Any], states: Mapping[str, Mapping[str, Any]], quote: Mapping[str, Any], cfg: Mapping[str, Any],
    *, min_size: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The rungs to post now (user 10-13 「允许先用买一+1积累单边，成交后动态计算成本，另一个必须在这个成本下」).

    Lead side (``lead_side``): one rung at its own best bid + 1 tick while no
    lead rung rests and the lead's unmatched cost is under
    ``max_unmatched_usdc``.  Follow side: once the lead holds more shares
    than the follow, one rung for the unmatched shares at
    min(follow's best bid + 1 tick, cap - lead's average cost), so every pair
    it completes costs at most the cap.  Both are capped against the other
    side's DEAREST RESTING rung as well (cap - that price), so a fill on
    either side next to a resting rung on the other never prices a pair over
    the cap.  Post-only legal (under the side's own ask) and floored to the
    tick, or not posted.
    """

    pc, tick = float(cfg["pair_cost_cap"]), float(quote["tick"])
    eps = tick / 10.0
    lead = str(cfg["lead_side"]).upper()
    if lead not in (SIDE_YES, SIDE_NO):
        raise ValueError(f"pair_maker_lead_side_unknown:{lead}")
    follow = SIDE_NO if lead == SIDE_YES else SIDE_YES
    held = {SIDE_YES: [0.0, 0.0], SIDE_NO: [0.0, 0.0]}
    resting = {SIDE_YES: [], SIDE_NO: []}
    for rung in plan.get("rungs") or []:
        st = states.get(rung.get("intent_id") or "", {})
        side = str(rung["side"])
        held[side][0] += float(st.get("filled", 0.0))
        held[side][1] += float(st.get("notional", 0.0))
        if st.get("open"):
            resting[side].append(float(rung["price"]))
    avg = {side: (held[side][1] / held[side][0] if held[side][0] > 0 else None) for side in held}
    books = side_quotes(quote)
    status: dict[str, Any] = {"held": {k: round(v[0], 4) for k, v in held.items()},
                              "avg": {k: (round(v, 4) if v is not None else None) for k, v in avg.items()},
                              "resting": {k: sorted(v) for k, v in resting.items()}, "lead": lead}
    out: list[dict[str, Any]] = []

    def price_for(side: str, ceiling: float) -> float | None:
        bid, ask = books[side]
        price = _floor_tick(min(bid + tick, ceiling), tick)
        if not (tick - eps <= price <= ask - tick + eps):
            return None
        return price

    other_rest = {side: max(resting[other], default=0.0) for side, other in ((lead, follow), (follow, lead))}
    unmatched_lead = held[lead][0] - held[follow][0]
    # Lead: accumulate while its unmatched cost stays under the cap.
    lead_cost = max(0.0, unmatched_lead) * (avg[lead] or 0.0)
    # Amendment four (user 10-13 「未配平控制：两个都用」): the 6 U cost cap AND the
    # unmatched ratio |lead - follow| / (lead + follow) -- warn at 8%, no new
    # lead at 10% -- the ratio judged only once the book holds more than
    # ``ratio_min_total_shares`` (a first 5-share fill is 100% by construction).
    total = held[lead][0] + held[follow][0]
    ratio = abs(unmatched_lead) / total if total > 0 else 0.0
    ratio_live = total > float(cfg["ratio_min_total_shares"]) + 1e-9
    status["unmatched_ratio"] = round(ratio, 4)
    if ratio_live and ratio >= float(cfg["warn_unmatched_ratio"]) - 1e-9:
        status["ratio_warn"] = True
    if resting[lead]:
        status["lead"] = f"{lead}:resting"
    elif lead_cost >= float(cfg["max_unmatched_usdc"]) - 1e-9:
        status["lead"] = f"{lead}:unmatched_cap:{lead_cost:.2f}"
    elif ratio_live and unmatched_lead > 0 and ratio >= float(cfg["max_unmatched_ratio"]) - 1e-9:
        status["lead"] = f"{lead}:unmatched_ratio:{ratio:.3f}"
    else:
        ceiling = pc - max(avg[follow] or 0.0, other_rest[lead])
        price = price_for(lead, ceiling)
        if price is None:
            status["lead"] = f"{lead}:crosses_or_floor"
        else:
            out.append({"side": lead, "k": 0, "price": price, "shares": float(cfg["rung_shares"])})
    # Follow: complete the pairs under the lead's cost.
    # A partial lead fill under the exchange minimum (LEMD 10-14: 4.35 shares) is
    # still completed: the rung is lifted to the minimum by ``_shares``, and the
    # overshoot (under one minimum) is the smallest pair the venue allows.
    need = round(unmatched_lead, SHARE_PRECISION)
    if need >= 10 ** -SHARE_PRECISION - 1e-9 and not resting[follow]:
        ceiling = pc - max(avg[lead] or 0.0, other_rest[follow])
        price = price_for(follow, ceiling)
        if price is None:
            status["follow"] = f"{follow}:crosses_or_floor:{ceiling:.3f}"
        else:
            out.append({"side": follow, "k": 1, "price": price, "shares": need})
    elif resting[follow]:
        status["follow"] = f"{follow}:resting"
    return out, status


def execute_pair_maker(
    target_conn: sqlite3.Connection,
    *,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    admission: Callable[[str, sqlite3.Connection], Mapping[str, Any]] | None = None,
    ws_manifest_path: Any = None,
    merge_transport: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Re-quote every active plan, merge its matched pairs, then plan the station-days whose book just formed."""

    ensure_lane_schema(target_conn)
    cfg = {**PAIR_MAKER_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    if str(cfg.get("rule")) != RULE:
        raise ValueError(f"pair_maker_rule_unknown:{cfg.get('rule')}")
    if not 0.0 < float(cfg["pair_cost_cap"]) <= 1.0:
        raise ValueError(f"pair_maker_pair_cost_cap_out_of_range:{cfg['pair_cost_cap']}")
    merge_mode = str(cfg.get("merge") or "off")
    if merge_mode not in ("off", "simulate", "live"):
        raise ValueError(f"pair_maker_merge_mode_unknown:{merge_mode}")
    merge_call = merge_transport or default_merge_transport
    merges: list[dict[str, Any]] = []
    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    live = cfg.get("live_ws_book", True) is True
    root = _ws_manifest_root(ws_manifest_path) if live else None
    now = _utc(now_utc)
    cash, cash_refusal = _free_cash(target_conn, now_utc=now_utc)
    budget = None
    if cash is not None:
        budget = max(0.0, float(cfg["max_cash_fraction"]) * float(cash) - _open_lane_cost(target_conn))
    receipts: list[dict[str, Any]] = []
    waiting: Counter[str] = Counter()

    def post(plan: dict[str, Any], rung: Mapping[str, Any], *, until: str, tick: float) -> None:
        n = sum(1 for r in plan["rungs"] if r["side"] == rung["side"] and int(r["k"]) == int(rung["k"]))
        intent_id = f"{INTENT_PREFIX}_" + content_sha256(
            {"plan": plan["plan_id"], "side": rung["side"], "k": rung["k"], "n": n})[:32]
        venue = {**resting_order_fields(now_utc=now_utc, limit_price=float(rung["price"]), belief_fresh_until_utc=until),
                 "expiration_basis": "pair_maker_rung"}
        token = plan["tokens"][rung["side"]]
        entry = send_lane_buy(
            target_conn, lane=LANE, intent_id=intent_id, station_id=plan["station_id"], event_date=plan["event_date"],
            settlement_timezone=plan["settlement_timezone"], bucket=plan["bucket"], token_id=token,
            price=float(rung["price"]), shares=float(rung["shares"]), side=rung["side"],
            execution_candidate=EXECUTION_CANDIDATE, expiration=int(venue["expiration"]), tick_size=float(tick),
            intent_body={"schema": SCHEMA, "rule": RULE, "strategy_id": STRATEGY_ID, "plan_id": plan["plan_id"],
                         "order_type": venue["order_type"], "post_only": venue["post_only"],
                         "expiration_basis": venue["expiration_basis"],
                         # Charter 269: the rung lives its own horizon, not charter 94's
                         # bid-anchored ladder; the resting pass syncs it and leaves it.
                         "own_horizon": True,
                         "rung": {k: rung[k] for k in ("side", "k", "price", "shares")}},
            transport=transport, live_submit_authorized=live_submit_authorized,
            decision_details={"schema": "pair_maker_lane_decision_v1", "lane": LANE, "title": LANE_TITLE,
                              "rule": RULE, "decision_signature": {"stage": "MARKET", "lane": LANE, "rule": RULE},
                              "plan_id": plan["plan_id"]},
        )
        plan["rungs"].append({"side": rung["side"], "k": int(rung["k"]), "price": float(rung["price"]),
                              "shares": float(rung["shares"]), "intent_id": intent_id, "posted_at_utc": now_utc,
                              "state": entry.get("state"), "reason": entry.get("reason")})
        receipts.append({"station_id": plan["station_id"], "event_date": plan["event_date"], "bucket": plan["bucket"],
                         "side": rung["side"], "k": rung["k"], "price": rung["price"], "shares": rung["shares"],
                         "state": entry.get("state"), "reason": entry.get("reason")})

    def requote(plan: dict[str, Any], quote: Mapping[str, Any], hours: float | None) -> None:
        nonlocal budget
        rest_end = min(_utc(plan["window_ends_utc"]), now + timedelta(minutes=float(cfg["rung_minutes"])))
        if hours is not None:
            rest_end = min(rest_end, now + timedelta(hours=float(hours) - float(cfg["min_hours_to_settlement"])))
        if (rest_end - now).total_seconds() < VENUE_GTD_MINIMUM_HORIZON_SECONDS + GTD_SIGNING_MARGIN_SECONDS:
            waiting["window_closing"] += 1
            return
        states = _intent_states(target_conn, [r["intent_id"] for r in plan["rungs"] if r.get("intent_id")])
        rungs, status = lead_follow_plan(plan, states, quote, cfg, min_size=float(plan.get("min_size") or 5.0))
        plan["last_status"] = status
        spent = sum(states.get(r["intent_id"], {}).get("notional", 0.0) for r in plan["rungs"] if r.get("intent_id"))
        # Merged pairs left the book as pUSD: their cost no longer counts (capital rolls).
        spent -= float(plan.get("merged_cost_usdc") or 0.0)
        spent += sum(float(r["price"]) * float(r["shares"]) for r in plan["rungs"]
                     if states.get(r.get("intent_id") or "", {}).get("open"))
        for rung in rungs:
            shares = _shares(float(rung["price"]), {**cfg, "rung_shares": rung["shares"]}, float(plan.get("min_size") or 5.0))
            cost = shares * float(rung["price"])
            if spent + cost > float(cfg["max_station_day_usdc"]) + 1e-9:
                waiting["station_day_cap"] += 1
                continue
            if budget is not None and cost > budget + 1e-9:
                waiting["cash_cap"] += 1
                continue
            post(plan, {**rung, "shares": shares}, until=_iso(rest_end), tick=float(quote["tick"]))
            spent += cost
            if budget is not None:
                budget -= cost

    def merge_step(plan: dict[str, Any], book_now: Mapping[str, Any]) -> None:
        rows = _plan_merges(target_conn, plan["plan_id"])
        for row in rows:
            if row["state"] != "SENT":
                continue
            receipt = dict(merge_call("/merge-receipt", {"tx_hash": row["tx_hash"]}) or {})
            status = str(receipt.get("status") or "")
            new = {"success": "MERGED", "reverted": "FAILED"}.get(status) if receipt.get("ok") else None
            if new:
                target_conn.execute(f"UPDATE {MERGE_TABLE} SET state=?, response_json=?, updated_at_utc=? WHERE merge_id=?",
                                    (new, json.dumps({**json.loads(row["response_json"] or "{}"), "receipt": receipt},
                                                     sort_keys=True), now_utc, row["merge_id"]))
                row["state"] = new
        done = sum(float(r["shares"]) for r in rows if r["state"] in MERGE_COUNTED_STATES)
        avg_pair = book_now.get("pair_cost")
        plan["merged_shares"] = round(done, 4)
        plan["merged_cost_usdc"] = round(done * float(avg_pair), 6) if avg_pair is not None else 0.0
        if merge_mode == "off":
            return
        if any(r["state"] in MERGE_HOLDING_STATES for r in rows):
            waiting["merge_pending"] += 1
            return
        mergeable = math.floor((float(book_now["matched_shares"]) - done) * 10 ** SHARE_PRECISION + 1e-9) / 10 ** SHARE_PRECISION
        if mergeable < float(cfg["min_merge_shares"]) - 1e-9:
            return
        if not plan.get("condition_id"):
            plan["condition_id"] = market_ids(target_conn, plan["station_id"], plan["event_date"], plan["bucket"]).get("condition_id")
        if not plan.get("condition_id"):
            waiting["merge_condition_missing"] += 1
            return
        n_done = sum(1 for r in rows if r["state"] in MERGE_COUNTED_STATES)
        merge_id = f"{INTENT_PREFIX}_merge_" + content_sha256(
            {"plan": plan["plan_id"], "shares": mergeable, "mode": merge_mode, "n": n_done})[:24]
        if merge_mode == "simulate" and any(r["merge_id"] == merge_id for r in rows):
            return
        response = dict(merge_call("/merge-positions", {
            "condition_id": plan["condition_id"], "yes_token": plan["tokens"][SIDE_YES], "no_token": plan["tokens"][SIDE_NO],
            "neg_risk": True, "shares": mergeable, "execute": merge_mode == "live"}) or {})
        if response.get("sent") is True and response.get("tx_hash"):
            state = "SENT"
        elif response.get("sent", False) is None:
            state = "UNKNOWN"
        elif response.get("ok") and response.get("simulated"):
            state = "SIMULATED"
        else:
            state = "FAILED"
        target_conn.execute(
            f"INSERT OR REPLACE INTO {MERGE_TABLE} (merge_id, plan_id, station_id, event_date, bucket, condition_id, yes_token,"
            " no_token, shares, mode, state, tx_hash, response_json, created_at_utc, updated_at_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (merge_id, plan["plan_id"], plan["station_id"], plan["event_date"], plan["bucket"], plan["condition_id"],
             plan["tokens"][SIDE_YES], plan["tokens"][SIDE_NO], mergeable, merge_mode, state, response.get("tx_hash"),
             json.dumps(response, sort_keys=True), now_utc, now_utc),
        )
        merges.append({"station_id": plan["station_id"], "event_date": plan["event_date"], "bucket": plan["bucket"],
                       "shares": mergeable, "state": state, "reason": response.get("reason") or response.get("error"),
                       "gas_cost_pol": response.get("gas_cost_pol")})

    # 1. Active plans: refresh the books, re-quote inside the window, close after it.
    refreshed = closed = 0
    for station, day, plan_json, ends in target_conn.execute(
        f"SELECT station_id, event_date, plan_json, window_ends_utc FROM {PLAN_TABLE} WHERE plan_state=?", (PLAN_ACTIVE,),
    ).fetchall():
        plan = json.loads(plan_json)
        plan.setdefault("tokens", {})
        if plan["tokens"].get(SIDE_YES) and plan["tokens"].get(SIDE_NO):
            merge_step(plan, ledger(target_conn, plan))
        if now < _utc(ends):
            refusal, hours = _window_refusal(target_conn, station_id=str(station), event_date=str(day),
                                             settlement_timezone=plan["settlement_timezone"], now_utc=now_utc,
                                             min_hours=float(cfg["min_hours_to_settlement"]), gate=gate)
            if not refusal and plan["tokens"].get(SIDE_YES) and plan["tokens"].get(SIDE_NO):
                book, book_refusal = _book(target_conn, root, station_id=str(station), event_date=str(day),
                                           now_utc=now_utc, fresh_seconds=float(cfg["send_book_fresh_seconds"]), live=live)
                quote = bucket_quote(list((book or {}).get("buckets") or []), plan["bucket"], cfg) if book else None
                if quote is not None:
                    requote(plan, quote, hours)
                else:
                    waiting[f"requote:{book_refusal or 'bucket_one_sided'}"] += 1
        book_now = ledger(target_conn, plan)
        book_now["merged_shares"] = plan.get("merged_shares", 0.0)
        still_open = (book_now[SIDE_YES]["resting"] or book_now[SIDE_NO]["resting"]
                      or any(r["state"] in MERGE_HOLDING_STATES for r in _plan_merges(target_conn, plan["plan_id"])))
        final = PLAN_DONE if (now >= _utc(ends) and not still_open) else PLAN_ACTIVE
        target_conn.execute(
            f"UPDATE {PLAN_TABLE} SET plan_json=?, ledger_json=?, plan_state=?, last_pass_at_utc=?"
            " WHERE station_id=? AND event_date=?",
            (json.dumps(plan, ensure_ascii=False, sort_keys=True), json.dumps(book_now, sort_keys=True), final,
             now_utc, station, day),
        )
        refreshed += 1
        closed += final == PLAN_DONE

    # 2. New plans on the books that formed.
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
        ids = market_ids(target_conn, station, day, form["bucket"])
        if not ids["yes_token"] or not ids["no_token"]:
            waiting["token_missing"] += 1
            continue
        window_end = now + timedelta(hours=float(cfg["quote_window_hours"]))
        if hours is not None:
            window_end = min(window_end, now + timedelta(hours=float(hours) - float(cfg["min_hours_to_settlement"])))
        plan_id = f"{INTENT_PREFIX}_plan_" + content_sha256({"station": station, "day": day, "formed": now_utc})[:24]
        plan = {"schema": SCHEMA, "rule": RULE, "plan_id": plan_id, "station_id": station, "event_date": day,
                "settlement_timezone": zone, "bucket": form["bucket"], "formation": form,
                "window_ends_utc": _iso(window_end), "hours_to_settlement": hours,
                "tokens": {SIDE_YES: ids["yes_token"], SIDE_NO: ids["no_token"]},
                "condition_id": ids.get("condition_id"),
                "min_size": float(ids.get("min_size") or 5.0),
                "settings": {k: cfg[k] for k in ("pair_cost_cap", "lead_side", "rung_shares", "rung_minutes",
                                                 "quote_window_hours", "max_unmatched_usdc", "max_unmatched_ratio",
                                                 "warn_unmatched_ratio", "ratio_min_total_shares",
                                                 "max_station_day_usdc", "max_cash_fraction", "stations")},
                "book_id": book.get("snapshot_id"), "book_source": book.get("book_source"), "rungs": []}
        requote(plan, {"bid": form["bid"], "ask": form["ask"], "mid": form["mid"], "tick": form["tick"]}, hours)
        target_conn.execute(
            f"INSERT OR REPLACE INTO {PLAN_TABLE} (station_id, event_date, bucket, plan_state, formed_at_utc,"
            " window_ends_utc, plan_json, ledger_json, last_pass_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            (station, day, form["bucket"], PLAN_ACTIVE, now_utc, plan["window_ends_utc"],
             json.dumps(plan, ensure_ascii=False, sort_keys=True), None, now_utc),
        )
        planned += 1
    return {"schema": "pair_maker_lane_receipt_v2", "lane": LANE, "title": LANE_TITLE, "rule": RULE,
            "planned": planned, "rungs": len(receipts), "refreshed": refreshed, "closed": closed,
            "merge_mode": merge_mode, "merges": merges,
            "cash": cash, "cash_refusal": cash_refusal, "budget_left": budget,
            "waiting": dict(sorted(waiting.items())), "receipts": receipts}


__all__ = ["LANE", "LANE_TITLE", "MERGE_TABLE", "PLAN_TABLE", "merged_shares_for_token", "RULE", "bucket_quote", "execute_pair_maker", "formation",
           "lead_follow_plan", "ledger", "side_quotes"]
