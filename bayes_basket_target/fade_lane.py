"""反向线 -- fading the model where it leans away from the market (charter 277).

User ruling 2026-10-22 「把格 A 和格 B 做成车道并开启实盘，单笔5u，前端统计数据」.

Measured on the served record 09-12..10-20 (``docs/research/p0_mode_hit_20261021``,
补记五, ``PROTOCOL_DIRECTIONAL.md``): the truth lands on the far side of the
market from the model's disagreement.  Where the P0-M mode sits warm of the
market's mode the truth runs 0.187 buckets cold of the market mean; where the
P0 mode (more than 36 h out) sits cold of the market's, 0.175 warm.  Two legs
carried that at the taker price, each on first decisions, each positive on
both halves, odd and even days, September and October:

* **反向线 A** (``fade_p0m``): the station-day's newest stage is P0-M and its
  served mode is warm of the market's mode -> buy YES on the bucket one rung
  COLD of the market's mode, at the best ask.  670 decisions, +5.3 cents a
  share [+1.4, +9.2], cost 0.17 a share; unconditionally that bucket pays
  +0.6, with the model cold of the market -4.0, the model's own side -5.2.
* **反向线 B** (``fade_p0``): the newest stage is P0, more than 36 h out, the
  model's mode is not the market's and the two means are more than
  ``min_mean_gap_rungs`` (0.76) rungs apart -> buy NO on the MODEL's mode at
  the NO ask (1 - YES best bid).  504 decisions, +3.1 cents a share
  [+0.6, +5.3] on a 0.84 cost; the threshold is the tercile cut it was read
  at (0.5 / 1.0 / 1.3 gave +1.1 / +0.8 / -0.3).

Both are FOK takes sized up to ``stake_usdc`` and capped by the depth at the
level they cross (the P1 tail lane's headroom), once a station-day, held to
settlement.  Every station-day the rule looks at is written down in
``fade_lane_evaluation`` -- sent, filled, killed or refused -- so the forward
read is the whole population, not the legs.  Nothing is read or written while
``data/live_paused.flag`` is up or the arming flag is down.

The model sets the DIRECTION only: which rung, which side.  It sets no price
and no size, and nothing here enters P0, P0-M or P1.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .basket_kernel.valuation_v1 import market_reference_v1
from .certain_win_recycle import _registry_rows
from .contracts import content_sha256
from . import lane_orders
from .exit_orders import Transport
from .lane_orders import send_lane_buy
from .live_station_scope import admission as live_station_admission
from .p1_tail_lane import _ws_manifest_root, open_buy_orders_on_station_day, taker_take
from .proper_bet_lane import ENVELOPE_EXPIRED, served_stage_vector
from . import realisation_store
from .settlement_time import hours_to_settlement, settlement_day_state
from .strategy_lanes import (
    FADE_P0_DEFAULT_SETTINGS,
    FADE_P0M_DEFAULT_SETTINGS,
    LANE_FADE_P0,
    LANE_FADE_P0M,
    lane_settings,
)
from .tail_short_lane import (
    DEFAULT_MIN_ORDER_SHARES,
    PAUSE_FLAG_PATH,
    _book,
    no_take,
    rests_on_another_bucket,
    station_days_in_window,
    taker_fee_usdc,
    yes_quote,
)
from .tiebreak_lane import _cash_refusal, settle_stuck_lane_cycles

STRATEGY_ID = "bayes_basket_target_v1"
SCHEMA = "fade_lane_plan_v1"
RULE_A = "p0m_warm_fade_cold_neighbour_yes_taker_v1"
RULE_B = "p0_far_disagreement_fade_model_mode_no_taker_v1"
RULE_VERSIONS = {RULE_A: "fade_lane_a_v1", RULE_B: "fade_lane_b_v1"}
EXECUTION_CANDIDATE = "TAKER_FOK"
#: The venue's floor on a marketable BUY's notional (USDC), learned from its
#: refusal on 2026-10-21/22 ("invalid amount for a marketable BUY order
#: ($0.4), min size: 1"); the five-share minimum is a separate rule.
MIN_MARKETABLE_NOTIONAL_USDC = 1.0
EVALUATION_TABLE = "fade_lane_evaluation"
INTENT_PREFIX = {LANE_FADE_P0M: "fadep0m", LANE_FADE_P0: "fadep0"}
TITLES = {LANE_FADE_P0M: "反向线 A", LANE_FADE_P0: "反向线 B"}
DEFAULTS = {LANE_FADE_P0M: FADE_P0M_DEFAULT_SETTINGS, LANE_FADE_P0: FADE_P0_DEFAULT_SETTINGS}
#: Refusals of the rule itself -- the station-day is looked at again next
#: cycle; a refusal written once a station-day.
SIGNAL_REFUSALS = frozenset({
    "signal:modes_agree", "signal:model_not_warm_of_market", "signal:model_not_cold_of_market",
    "signal:mean_gap_small", "signal:no_cold_neighbour",
})

Admission = Callable[[str, sqlite3.Connection], Mapping[str, Any]]

EVALUATION_DDL = (
    f"CREATE TABLE IF NOT EXISTS {EVALUATION_TABLE} ("
    " evaluation_id TEXT PRIMARY KEY,"
    " lane TEXT NOT NULL,"
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " bucket TEXT NOT NULL,"
    " side TEXT NOT NULL,"
    " evaluated_at_utc TEXT NOT NULL,"
    " rule_version TEXT NOT NULL,"
    " stage TEXT,"
    " settlement_timezone TEXT,"
    " hours_to_settlement REAL,"
    " snapshot_id TEXT,"
    " book_source TEXT,"
    " book_observed_at TEXT,"
    " book_age_seconds REAL,"
    " model_mode_bucket TEXT,"
    " market_mode_bucket TEXT,"
    " mode_gap_rungs INTEGER,"
    " mean_gap_rungs REAL,"
    " model_p REAL,"
    " market_q REAL,"
    " yes_bid REAL,"
    " yes_ask REAL,"
    " price REAL,"
    " shares REAL,"
    " cost_usdc REAL,"
    " fee_usdc_est REAL,"
    " refusal TEXT,"
    " order_intent_id TEXT,"
    " sent INTEGER NOT NULL DEFAULT 0,"
    " filled_shares REAL NOT NULL DEFAULT 0,"
    " send_json TEXT,"
    " model_envelope_id TEXT"
    ")"
)
EVALUATION_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_scope_idx"
    f" ON {EVALUATION_TABLE}(lane, station_id, event_date, bucket)"
)


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    conn.execute(EVALUATION_DDL)
    conn.execute(EVALUATION_INDEX_DDL)
    realisation_store.ensure_shadow_schema(conn)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The rule.  Pure.


def _mode_index(vector: Sequence[float]) -> int:
    """The largest mass, the lower rung on a tie -- ``market_mode_bucket``'s
    rule, the one the research measured the modes with."""

    return max(range(len(vector)), key=lambda i: (float(vector[i]), -i))


def _mean_index(vector: Sequence[float]) -> float:
    total = float(sum(vector))
    return sum(i * float(v) for i, v in enumerate(vector)) / total if total > 0 else float("nan")


def fade_reading(
    probabilities: Mapping[str, float],
    book_buckets: Sequence[Mapping[str, Any]],
    *,
    rule: str,
    min_mean_gap_rungs: float = 0.0,
) -> dict[str, Any]:
    """The rule on one ladder: the model's mode against the market's, and the
    leg it fires -- or why not.

    ``probabilities`` is the served vector (``served_stage_vector``), ``book_buckets``
    the ladder's book rows.  Both are read on the ladder's rung order; a
    ladder the vector does not cover exactly answers ``ladder_mismatch``.
    """

    market = market_reference_v1(book_buckets)
    if market.get("state") != "complete":
        return {"state": "market_incomplete", "reason": market.get("reason")}
    q_by = {str(k): float(v) for k, v in market["vector"].items()}
    order = [str(r.get("bucket")) for r in sorted(
        book_buckets, key=lambda r: (r.get("ordinal") is None, r.get("ordinal") or 0))]
    order = [b for b in order if b in q_by]
    if not order or set(order) != {str(b) for b in probabilities}:
        return {"state": "ladder_mismatch", "reason": "ladder_mismatch"}
    p = [float(probabilities[b]) for b in order]
    total = sum(p)
    if not (total > 0.0 and math.isfinite(total)):
        return {"state": "ladder_mismatch", "reason": "model_vector_empty"}
    p = [v / total for v in p]
    q = [q_by[b] for b in order]
    pm, qm = _mode_index(p), _mode_index(q)
    mean_gap = _mean_index(p) - _mean_index(q)
    out: dict[str, Any] = {
        "state": "complete", "order": order, "p": dict(zip(order, p)), "q": dict(zip(order, q)),
        "model_mode_bucket": order[pm], "market_mode_bucket": order[qm],
        "mode_gap_rungs": pm - qm, "mean_gap_rungs": round(mean_gap, 6), "leg": None, "refusal": None,
    }
    if pm == qm:
        out["refusal"] = "signal:modes_agree"
        return out
    if rule == RULE_A:
        if pm < qm:
            out["refusal"] = "signal:model_not_warm_of_market"
            return out
        if qm == 0:
            out["refusal"] = "signal:no_cold_neighbour"
            return out
        target = qm - 1
        out["leg"] = {"bucket": order[target], "side": "YES", "p": p[target], "q": q[target]}
        return out
    if rule == RULE_B:
        if abs(mean_gap) <= float(min_mean_gap_rungs):
            out["refusal"] = "signal:mean_gap_small"
            return out
        out["leg"] = {"bucket": order[pm], "side": "NO", "p": p[pm], "q": q[pm]}
        return out
    raise ValueError(f"fade_rule_unknown:{rule}")


# ---------------------------------------------------------------------------
# Reading the store.


def lane_cost_today(conn: sqlite3.Connection, *, lane: str, now_utc: str) -> float:
    """USDC this lane spent, or may have spent, on the UTC day (the far-tail
    lane's reading: a fill at what it filled, a killed FOK at nothing, a send
    whose outcome is unknown at its full cost)."""

    day = _utc(now_utc).date().isoformat()
    row = conn.execute(
        f"SELECT COALESCE(SUM(CASE"
        f"  WHEN filled_shares > 0 THEN filled_shares * price + COALESCE(fee_usdc_est, 0)"
        f"  WHEN json_extract(send_json, '$.event_type') = 'EXPIRED' THEN 0"
        f"  ELSE COALESCE(cost_usdc, 0) + COALESCE(fee_usdc_est, 0) END), 0)"
        f" FROM {EVALUATION_TABLE} WHERE lane=? AND sent=1 AND substr(evaluated_at_utc,1,10)=?",
        (lane, day),
    ).fetchone()
    return float(row[0] or 0.0)


def _history(conn: sqlite3.Connection, lane: str, station_id: str, event_date: str) -> dict[str, dict[str, Any]]:
    """``tail_short_lane.bucket_history`` on this lane's rows of the station-day."""

    out: dict[str, dict[str, Any]] = {}
    for bucket, filled, sent, refusal, intent, at in conn.execute(
        f"SELECT bucket, filled_shares, sent, refusal, order_intent_id, evaluated_at_utc"
        f" FROM {EVALUATION_TABLE} WHERE lane=? AND station_id=? AND event_date=?",
        (lane, station_id, event_date),
    ):
        entry = out.setdefault(str(bucket), {"filled": 0.0, "ledger_filled": False, "sends": 0, "orders": 0,
                                             "unsent_at": None, "refusals": set()})
        entry["filled"] += float(filled or 0.0)
        entry["sends"] += int(sent or 0)
        if intent:
            entry["orders"] += 1
            if not sent and not float(filled or 0.0) > 0.0 and str(at) > str(entry["unsent_at"] or ""):
                entry["unsent_at"] = str(at)
        if refusal:
            entry["refusals"].add(str(refusal))
    for (bucket,) in conn.execute(
        f"SELECT DISTINCT e.bucket FROM {EVALUATION_TABLE} AS e"
        " JOIN basket_target_order_intents AS i ON i.order_intent_id = e.order_intent_id"
        " WHERE e.lane=? AND e.station_id=? AND e.event_date=? AND i.state='filled'",
        (lane, station_id, event_date),
    ):
        out.setdefault(str(bucket), {"filled": 0.0, "ledger_filled": False, "sends": 0, "orders": 0,
                                     "unsent_at": None, "refusals": set()})["ledger_filled"] = True
    return out


def _held(entry: Mapping[str, Any]) -> bool:
    return float(entry["filled"]) > 0.0 or bool(entry["ledger_filled"])


def _record(conn: sqlite3.Connection, lane: str, row: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(row)
    body.setdefault("sent", 0)
    body.setdefault("filled_shares", 0.0)
    body["lane"] = lane
    body["evaluation_id"] = f"{INTENT_PREFIX[lane]}_eval_" + content_sha256({
        k: body.get(k) for k in ("lane", "station_id", "event_date", "bucket", "evaluated_at_utc",
                                 "refusal", "order_intent_id")
    })[:32]
    columns = ",".join(body)
    conn.execute(
        f"INSERT OR IGNORE INTO {EVALUATION_TABLE}({columns}) VALUES({','.join('?' for _ in body)})",
        tuple(body.values()),
    )
    return body


# ---------------------------------------------------------------------------
# Planning and sending.


def plan_fade(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    lane: str,
    now_utc: str,
    cfg: Mapping[str, Any],
    gate: Admission,
    manifest_root: Callable[[], Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Every station-day whose newest stage is the lane's: read the served
    vector and the book, fire the rule, plan the take."""

    rule = str(cfg["rule"])
    rule_version = RULE_VERSIONS[rule]
    stage = str(cfg["stage"])
    now = _utc(now_utc)
    min_hours = float(cfg["min_hours_to_settlement"])
    max_hours = cfg.get("max_hours_to_settlement")
    stake = float(cfg["stake_usdc"])
    fee_rate = float(cfg.get("taker_fee_rate", 0.0))
    budget_left = float(cfg["max_daily_cost_usdc"]) - lane_cost_today(target_conn, lane=lane, now_utc=now_utc)
    plans: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for scope in station_days_in_window(
        target_conn, now_utc=now_utc, lookback_days=int(cfg.get("lookback_days", 1)), stages=frozenset({stage}),
    ):
        station, day, tz = scope["station_id"], scope["event_date"], scope["settlement_timezone"]
        try:
            if settlement_day_state(day, as_of_utc=now_utc, settlement_timezone=tz).get("state") == "closed":
                skip("settlement_day_closed")
                continue
            hours = float(hours_to_settlement(day, as_of_utc=now_utc, settlement_timezone=tz))
        except Exception:  # noqa: BLE001 -- a day whose clock cannot be read is not traded
            skip("settlement_day_unknown")
            continue
        if not hours > min_hours or (max_hours is not None and hours > float(max_hours)):
            skip("outside_window")
            continue
        history = _history(target_conn, lane, station, day)
        if sum(1 for h in history.values() if _held(h)) >= int(cfg["max_legs_per_station_day"]):
            # One leg a station-day (the measured unit), held to settlement.
            skip("held")
            continue
        served = served_stage_vector(target_conn, station, day, stage=stage, valid_at=now)
        if served.get("state") == ENVELOPE_EXPIRED:
            skip("envelope_expired")
            continue
        if served.get("state") != "ok":
            skip(f"model_unavailable:{served.get('state')}")
            continue
        book, book_refusal = _book(
            target_conn, manifest_root(), station_id=station, event_date=day, now_utc=now_utc,
            fresh_seconds=float(cfg["send_book_fresh_seconds"]), live=cfg.get("live_ws_book", True) is True,
        )
        if book is None:
            skip(str(book_refusal))
            continue
        reading = fade_reading(
            served["probabilities"], list(book["buckets"]), rule=rule,
            min_mean_gap_rungs=float(cfg.get("min_mean_gap_rungs", 0.0)),
        )
        if reading["state"] != "complete":
            skip(f"{reading['state']}:{reading.get('reason')}")
            continue
        base = {
            "station_id": station, "event_date": day, "evaluated_at_utc": now_utc, "rule_version": rule_version,
            "stage": stage, "settlement_timezone": tz, "hours_to_settlement": round(hours, 3),
            "snapshot_id": book.get("snapshot_id"), "book_source": book.get("book_source"),
            "book_observed_at": book.get("observed_at"), "book_age_seconds": book.get("age_seconds"),
            "model_mode_bucket": reading["model_mode_bucket"], "market_mode_bucket": reading["market_mode_bucket"],
            "mode_gap_rungs": int(reading["mode_gap_rungs"]), "mean_gap_rungs": reading["mean_gap_rungs"],
            "model_envelope_id": served.get("envelope_id"),
        }
        if reading["leg"] is None:
            # The rule did not fire: written once a station-day per reason,
            # on the market's mode so the row has a bucket; looked at again.
            reason = str(reading["refusal"])
            past = history.get(reading["market_mode_bucket"]) or {"refusals": set()}
            if reason not in past["refusals"]:
                evaluations.append(_record(target_conn, lane, {
                    **base, "bucket": reading["market_mode_bucket"], "side": str(cfg["side"]), "refusal": reason,
                }))
            continue
        leg = reading["leg"]
        bucket, side = leg["bucket"], leg["side"]
        past = history.get(bucket) or {"filled": 0.0, "ledger_filled": False, "sends": 0, "orders": 0,
                                       "unsent_at": None, "refusals": set()}
        rows = {str(r.get("bucket")): r for r in book["buckets"]}
        quote = yes_quote(rows.get(bucket))
        base.update(bucket=bucket, side=side, model_p=round(float(leg["p"]), 6), market_q=round(float(leg["q"]), 6),
                    yes_bid=quote["bid"], yes_ask=quote["ask"])
        shadow_legs: list[dict[str, Any]] = []

        def refuse(reason: str, **extra: Any) -> None:
            if reason in past["refusals"]:
                return
            past["refusals"].add(reason)
            evaluations.append(_record(target_conn, lane, {**base, **extra, "refusal": reason}))
            price = quote["ask"] if side == "YES" else (
                round(1.0 - float(quote["bid"]), 6) if quote["bid"] is not None else None)
            if price is not None:
                shadow_legs.append({"bucket": bucket, "side": side, "price": float(price),
                                    "fee_per_share": realisation_store.taker_fee_per_share(price, fee_rate),
                                    "outcome": reason})

        if _held(past):
            skip("held")
            continue
        if past["sends"] >= int(cfg["max_attempts_per_bucket"]):
            refuse("max_attempts_reached")
            continue
        if past["orders"] >= int(cfg["max_orders_per_bucket"]):
            refuse("max_orders_reached")
            continue
        if past["unsent_at"] and (now - _utc(past["unsent_at"])).total_seconds() < float(cfg["unsent_retry_seconds"]):
            skip("unsent_retry_wait")
            continue
        verdict = gate(station, target_conn)
        if verdict.get("admitted") is not True:
            refuse(f"station_not_admitted:{verdict.get('reason') or 'refused'}")
            continue
        blocker = next((o for o in open_buy_orders_on_station_day(target_conn, station, day)
                        if not rests_on_another_bucket(o, bucket)), None)
        if blocker is not None:
            refuse(f"open_order:{blocker['lane'] or 'unknown'}:{blocker['state']}")
            continue
        registry = _registry_rows(target_conn, canonical_conn, station_id=station, event_date=day)
        market_row = registry.get(bucket) or {}
        try:
            raw = json.loads(market_row.get("registry_json") or "{}") or {}
        except ValueError:
            raw = {}
        token = str(market_row.get("yes_token") or "") if side == "YES" else str(raw.get("no_token") or "")
        if not token:
            refuse("registry_token_missing")
            continue
        if not bool(market_row.get("accepting_orders")):
            refuse("market_not_accepting_orders")
            continue
        min_shares = float(market_row.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
        if side == "YES":
            take = taker_take(rows.get(bucket), stake, tick_size=market_row.get("tick_size"),
                              min_shares=min_shares, walk_ticks=int(cfg.get("walk_ticks", 0)))
            price, shares = take["limit_price"], take["shares"]
        else:
            if quote["bid"] is None or float(quote["bid"]) < float(cfg.get("min_yes_bid", 0.0)) - 1e-12:
                refuse("no_bid")
                continue
            take = no_take(quote["bid"], quote["bid_size"], stake, tick_size=market_row.get("tick_size"),
                           min_shares=min_shares)
            price, shares = take["price"], take["shares"]
        if take.get("refusal"):
            refuse(str(take["refusal"]))
            continue
        cost = round(float(price) * float(shares), 6)
        if cost < MIN_MARKETABLE_NOTIONAL_USDC - 1e-9:
            # The venue refuses a marketable BUY under one dollar ("invalid
            # amount for a marketable BUY order ($0.4), min size: 1" -- LLBG
            # 2026-10-22 29°C, five sends at 0.062-0.08 on a thin ask).  A
            # take the depth cannot carry to a dollar is not sent.
            refuse(f"below_min_notional:{cost:.2f}<{MIN_MARKETABLE_NOTIONAL_USDC:.2f}")
            continue
        fee = taker_fee_usdc(float(price), float(shares), fee_rate)
        if cost + fee > budget_left + 1e-9:
            refuse(f"daily_budget:{max(budget_left, 0.0):.2f}")
            continue
        cash_refusal = _cash_refusal(target_conn, now_utc=now_utc, needed=cost + fee)
        if cash_refusal:
            refuse(cash_refusal)
            continue
        plan = {
            "schema": SCHEMA, "rule": rule_version, "rule_name": rule, "lane": lane, "title": TITLES[lane],
            "station_id": station, "event_date": day, "settlement_timezone": tz, "stage": stage,
            "bucket": bucket, "side": side, "token_id": token, "execution_candidate": EXECUTION_CANDIDATE,
            "order_type": "FOK", "post_only": False, "limit_price": float(price), "fok_price_cap": float(price),
            "shares": float(shares), "cost_usdc": cost, "fee_usdc_est": fee, "taker_fee_rate": fee_rate,
            "tick_size": market_row.get("tick_size"), "min_size": market_row.get("min_size"),
            "stake_usdc": stake, "attempt": past["sends"] + 1,
            "model_envelope_id": served.get("envelope_id"), "model_envelope_sha256": served.get("content_sha256"),
            "model_decision_at_utc": served.get("decision_at_utc"),
            **{k: base[k] for k in (
                "hours_to_settlement", "snapshot_id", "book_source", "book_observed_at", "book_age_seconds",
                "model_mode_bucket", "market_mode_bucket", "mode_gap_rungs", "mean_gap_rungs", "model_p",
                "market_q", "yes_bid", "yes_ask")},
        }
        plan["order_intent_id"] = f"{INTENT_PREFIX[lane]}_{content_sha256({'plan': plan, 'at': now_utc})[:32]}"
        evaluations.append(_record(target_conn, lane, {
            **base, "price": float(price), "shares": float(shares), "cost_usdc": cost, "fee_usdc_est": fee,
            "refusal": None, "order_intent_id": plan["order_intent_id"],
        }))
        budget_left -= cost + fee
        plans.append(plan)
        shadow_legs.append({"bucket": bucket, "side": side, "price": float(price),
                            "fee_per_share": realisation_store.taker_fee_per_share(float(price), fee_rate),
                            "outcome": "planned", "order_intent_id": plan["order_intent_id"]})
        if shadow_legs:
            realisation_store.record_legs(
                target_conn, lane=lane, kind="take", station_id=str(station), event_date=str(day),
                judged_at_utc=now_utc, hours_to_settlement=hours, book=book,
                p_raw=realisation_store.envelope_probabilities(target_conn, served.get("envelope_id")),
                envelope_id=served.get("envelope_id"), legs=shadow_legs, stage=stage,
            )
    return {"plans": plans, "evaluations": evaluations, "skipped": skipped, "rule_version": rule_version}


def execute_fade(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    lane: str,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
    ws_manifest_path: Any = None,
) -> dict[str, Any]:
    """Fire one 反向线 on every station-day in its window and send the takes."""

    if lane not in DEFAULTS:
        raise ValueError(f"fade_lane_unknown:{lane}")
    title = TITLES[lane]
    stopped = ("live_paused" if PAUSE_FLAG_PATH.exists()
               else "live_intent_disarmed" if not lane_orders.live_intent_armed() else None)
    if stopped:
        return {"schema": "fade_lane_receipt_v1", "lane": lane, "title": title, "state": stopped,
                "evaluated": 0, "planned": 0, "submitted": 0, "filled": 0}
    ensure_lane_schema(target_conn)
    cfg = {**DEFAULTS[lane], **dict(settings or lane_settings(lane))}
    if str(cfg.get("rule")) not in RULE_VERSIONS:
        raise ValueError(f"fade_rule_unknown:{cfg.get('rule')}")
    cycles_settled = settle_stuck_lane_cycles(target_conn)
    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    live = cfg.get("live_ws_book", True) is True
    manifest: dict[str, Any] = {}

    def manifest_root() -> Mapping[str, Any] | None:
        if "root" not in manifest:
            manifest["root"] = _ws_manifest_root(ws_manifest_path) if live else None
        return manifest["root"]

    planned = plan_fade(target_conn, canonical_conn, lane=lane, now_utc=now_utc, cfg=cfg, gate=gate,
                        manifest_root=manifest_root)
    receipts: list[dict[str, Any]] = []
    submitted = filled = 0
    for plan in planned["plans"]:
        details = {
            "schema": "fade_lane_decision_v1", "lane": lane, "title": title, "rule": plan["rule"],
            "decision_signature": {"stage": plan["stage"], "lane": lane, "rule": plan["rule"]},
            "forward_evidence_binding": {
                "decision_at_utc": now_utc, "probability_stage": plan["stage"],
                "probability_decision_at_utc": plan.get("model_decision_at_utc"),
                "probability_artifact_sha256": plan.get("model_envelope_sha256"),
                "probability_envelope_id": plan.get("model_envelope_id"), "ws_snapshot_id": plan["snapshot_id"],
            },
            "plan": plan,
        }
        entry = send_lane_buy(
            target_conn, lane=lane, intent_id=plan["order_intent_id"],
            station_id=plan["station_id"], event_date=plan["event_date"],
            settlement_timezone=plan["settlement_timezone"], bucket=plan["bucket"],
            token_id=plan["token_id"], price=plan["limit_price"], shares=plan["shares"],
            side=plan["side"], execution_candidate=EXECUTION_CANDIDATE,
            fok_price_cap=float(plan["fok_price_cap"]),
            tick_size=(float(plan["tick_size"]) if plan.get("tick_size") not in (None, "") else None),
            intent_body={"schema": SCHEMA, "rule": plan["rule"], "strategy_id": STRATEGY_ID,
                         "order_type": plan["order_type"], "post_only": plan["post_only"], "plan": plan},
            transport=transport, live_submit_authorized=live_submit_authorized,
            decision_details=details,
        )
        sent = bool(entry.get("exchange_post_started"))
        matched = float(entry.get("matched_size") or 0.0)
        submitted += int(sent)
        filled += int(matched > 0.0)
        outcome = None if matched > 0.0 else (
            f"send:{entry.get('reason') or entry.get('state')}" if not sent
            else f"send:{entry.get('event_type') or 'killed'}:{entry.get('reason') or ''}".rstrip(":")
        )
        send = {"state": entry.get("state"), "event_type": entry.get("event_type"), "reason": entry.get("reason"),
                "matched_size": matched, "exchange_post_started": sent, "at": now_utc}
        target_conn.execute(
            f"UPDATE {EVALUATION_TABLE} SET sent=?, filled_shares=?, refusal=?, send_json=?"
            " WHERE order_intent_id=?",
            (int(sent), matched, outcome, json.dumps(send, ensure_ascii=False, sort_keys=True),
             plan["order_intent_id"]),
        )
        receipts.append({"station_id": plan["station_id"], "event_date": plan["event_date"], "bucket": plan["bucket"],
                         "side": plan["side"], "price": plan["limit_price"], "shares": plan["shares"],
                         "order_intent_id": plan["order_intent_id"], **send})
    return {
        "schema": "fade_lane_receipt_v1", "lane": lane, "title": title, "rule": planned["rule_version"],
        "settings": cfg, "evaluated": len(planned["evaluations"]), "planned": len(planned["plans"]),
        "submitted": submitted, "filled": filled, "skipped": planned["skipped"],
        "refusals": sorted({str(e["refusal"]) for e in planned["evaluations"] if e.get("refusal")}),
        "cycles_settled": cycles_settled, "receipts": receipts,
    }


__all__ = [
    "EVALUATION_TABLE", "EXECUTION_CANDIDATE", "INTENT_PREFIX", "RULE_A", "RULE_B", "RULE_VERSIONS",
    "SIGNAL_REFUSALS", "TITLES", "ensure_lane_schema", "execute_fade", "fade_reading", "lane_cost_today",
    "plan_fade",
]
