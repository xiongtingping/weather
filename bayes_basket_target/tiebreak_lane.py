"""「秤砣线」-- the tiebreak lane (user ruling 2026-09-18: 上线这个策略，并暂停其他所有策略).

What it buys.  The market's own ladder, read as a probability vector at the
first decision of a stage, sometimes cannot choose between two adjacent
buckets: its mode holds 41-56% and the bucket beside it 40-47%, the two
together 84-98% of its mass.  When the model's mode is that second bucket,
the lane buys the model's bucket at the ask, 20 USDC, fill-or-kill, once
per station-day.  Nowhere else does the model's mode beat the market's --
its hit rate elsewhere is the market's own price on it (2,255 first
decisions, 33 event days to 2026-09-17: 0.04→0.035, 0.155→0.181,
0.253→0.286, 0.341→0.342) -- but here, on a market that is a coin toss,
the model's direction realised 54.8% against a 42.3% price (n=42, 20 days,
31 of them P1; +17.8% on 20 USDC a trade, 90% day-clustered interval
[-7.3%, +41.8%], both halves positive).  This is the posterior lane's own
prediction -- q = σ(a + b·logit p + c·logit m) leans on the model most
where logit m is near zero -- not a rule found by searching.

The four conditions, all at the stage's FIRST decision (the enumeration the
reading was taken on), read off the fact row's own envelope and WS snapshot
so the lane sees exactly what the decision saw:

1. model mode bucket ≠ market mode bucket;
2. the two are one rung apart;
3. the market's normalised mid on the model's mode bucket ≥ 0.40;
4. buy the model's mode bucket.

What it is not.  Forty-two trades is a hypothesis with a lower bound under
zero; the ruling puts it live to accumulate the thirty event days the
reading needs, not because the reading is settled.  Every first decision of
every stage is scored into ``tiebreak_lane_evaluation`` whether or not it
fires, and a fired one records why it did not send, so the forward sample
is the whole population and not the trades.

One attempt per station-day, at the first stage whose first decision fires;
a killed FOK is not retried on a later cycle, because a later cycle is a
later book and a different rule.  The first decision must be young
(``first_decision_fresh_seconds``): at lane start every open station-day
already has a first decision hours old, and buying on a stale book is not
the reading either.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from contextlib import nullcontext

from .account_equity import (
    capture_account_equity,
    load_account_equity,
    table_present as account_equity_table_present,
)
from .basket_kernel.valuation_v1 import market_reference_v1
from .certain_win_recycle import _registry_rows
from .contracts import content_sha256
from .exit_orders import Transport
from .lane_orders import lane_decision_id, send_lane_buy
from .live_station_scope import admission as live_station_admission
from .settlement_time import settlement_day_state
from .strategy_lanes import LANE_TIEBREAK, TIEBREAK_DEFAULT_SETTINGS, lane_settings

LANE = LANE_TIEBREAK
LANE_TITLE = "秤砣线"
STRATEGY_ID = "bayes_basket_target_v1"
SCHEMA = "tiebreak_lane_plan_v1"
# Two rules, selected by the lane file's ``rule`` (charter 146):
#   v1 ``adjacent_tie_v1`` -- model mode one rung from the market mode, and
#      the market's normalised mid on the model's bucket >= min share;
#   v2 ``model_mode_midband_v2`` (user ruling 2026-09-18 "升级到v2") -- the
#      model's mode bucket priced inside [min, max) by the market's
#      normalised mid, whether or not the market's own mode is that bucket.
# Measured on every snapshot 08-15..09-17 (39,849 facts), one buy per
# station-day at the ask: v2 754 station-days +5.7% [-0.2, +10.9] (halves
# +5.5/+5.7), first decisions only 488 +7.9% [+1.8, +13.7]; the same band
# without the model's mode on the bucket -7.2% [-12.3, -2.4].  v1 is the
# 148-station-day subset of v2 (+9.8% [-2.7, +22.9]).
RULE_V1 = "adjacent_tie_v1"
RULE_V2 = "model_mode_midband_v2"
RULE_VERSIONS = {
    RULE_V1: "tiebreak_adjacent_mode_market_share_v1",
    RULE_V2: "tiebreak_model_mode_market_midband_v2",
}
DEFAULT_RULE = RULE_V2
RULE_VERSION = RULE_VERSIONS[DEFAULT_RULE]


def rule_version_for(rule: Any) -> str:
    """The evaluation table's ``rule_version`` string for a lane-file rule name."""

    name = str(rule or DEFAULT_RULE).strip()
    if name not in RULE_VERSIONS:
        raise ValueError(f"tiebreak_rule_unknown:{name}")
    return RULE_VERSIONS[name]
EXECUTION_CANDIDATE = "TAKER_FOK"
EVALUATION_TABLE = "tiebreak_lane_evaluation"
DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_MIN_ORDER_SHARES = 5.0
SHARE_PRECISION = 2

Admission = Callable[[str, sqlite3.Connection], Mapping[str, Any]]

EVALUATION_DDL = (
    f"CREATE TABLE IF NOT EXISTS {EVALUATION_TABLE} ("
    " evaluation_id TEXT PRIMARY KEY,"
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " stage TEXT NOT NULL,"
    " fact_id TEXT NOT NULL,"
    " decision_at_utc TEXT NOT NULL,"
    " envelope_id TEXT NOT NULL,"
    " snapshot_id TEXT NOT NULL,"
    " rule_version TEXT NOT NULL,"
    " model_mode TEXT,"
    " market_mode TEXT,"
    " mode_distance INTEGER,"
    " market_share_on_model_mode REAL,"
    " model_share_on_model_mode REAL,"
    " market_mode_share REAL,"
    " model_mode_share REAL,"
    " fired INTEGER NOT NULL CHECK(fired IN (0,1)),"
    " refusal TEXT,"
    " order_intent_id TEXT,"
    " plan_json TEXT,"
    " evaluated_at_utc TEXT NOT NULL,"
    " content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),"
    " UNIQUE(station_id,event_date,stage)"
    ")"
)
EVALUATION_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_day_idx"
    f" ON {EVALUATION_TABLE}(event_date,fired,evaluated_at_utc)"
)


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    """Statement by statement, never ``executescript``: the resident calls
    this inside the writer's transaction."""

    conn.execute(EVALUATION_DDL)
    conn.execute(EVALUATION_INDEX_DDL)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# The rule, pure.


def evaluate_rule(
    probabilities: Mapping[str, Any],
    ordinals: Mapping[str, Any],
    market_vector: Mapping[str, Any],
    *,
    min_market_share: float,
    max_market_share: float | None = None,
    rule: str = DEFAULT_RULE,
) -> dict[str, Any]:
    """The conditions on one decision's envelope and market vector.

    Both vectors are renormalised over the buckets they share, as the
    reading was; the market's share on the model's mode is read after
    that.  ``rule`` picks v1 (adjacent modes, floor only) or v2 (the
    model's mode inside the market's mid band).  Returns the reading and,
    when it does not fire, why.
    """

    ords = {str(b): int(o) for b, o in (ordinals or {}).items()}
    p = {str(b): float(v) for b, v in (probabilities or {}).items() if str(b) in ords and float(v) >= 0.0}
    m = {str(b): float(v) for b, v in (market_vector or {}).items() if str(b) in ords and float(v) >= 0.0}
    common = sorted(set(p) & set(m), key=lambda b: ords[b])
    rule_name = str(rule or DEFAULT_RULE)
    reading: dict[str, Any] = {
        "rule": rule_version_for(rule_name),
        "rule_name": rule_name,
        "min_market_share_on_model_mode": float(min_market_share),
        "max_market_share_on_model_mode": (
            None if max_market_share is None else float(max_market_share)
        ),
        "buckets_in_common": len(common),
        "fired": False,
        "reason": "",
    }
    if not common:
        reading["reason"] = "no_common_buckets"
        return reading
    tp = sum(p[b] for b in common)
    tm = sum(m[b] for b in common)
    if tp <= 0.0 or tm <= 0.0:
        reading["reason"] = "mass_missing"
        return reading
    pn = {b: p[b] / tp for b in common}
    mn = {b: m[b] / tm for b in common}
    model_mode = max(common, key=lambda b: (pn[b], -ords[b]))
    market_mode = max(common, key=lambda b: (mn[b], -ords[b]))
    distance = abs(ords[model_mode] - ords[market_mode])
    reading.update({
        "model_mode": model_mode,
        "market_mode": market_mode,
        "mode_distance": distance,
        "model_mode_share": pn[model_mode],
        "market_mode_share": mn[market_mode],
        "market_share_on_model_mode": mn[model_mode],
        "model_share_on_model_mode": pn[model_mode],
        "model_share_on_market_mode": pn[market_mode],
    })
    if rule_name == RULE_V1:
        if model_mode == market_mode:
            reading["reason"] = "modes_agree"
            return reading
        if distance != 1:
            reading["reason"] = f"modes_not_adjacent:{distance}"
            return reading
    if mn[model_mode] < float(min_market_share) - 1e-12:
        reading["reason"] = "market_share_below_threshold"
        return reading
    if rule_name == RULE_V2 and max_market_share is not None and mn[model_mode] >= float(max_market_share) - 1e-12:
        reading["reason"] = "market_share_above_ceiling"
        return reading
    reading["fired"] = True
    return reading


def walk_ask_book(
    levels: Iterable[Any],
    stake_usdc: float,
    *,
    min_shares: float = DEFAULT_MIN_ORDER_SHARES,
) -> dict[str, Any] | None:
    """How many shares ``stake_usdc`` buys walking the asks from the best.

    Returns the size, the price of the deepest level touched (the FOK's limit
    and its cap) and what the walk costs, or ``None`` when the book cannot
    fill the venue minimum inside the stake.  The last level is taken only as
    far as the stake reaches; the size is floored to the venue's precision so
    the walk never costs more than the stake.
    """

    parsed: list[tuple[float, float]] = []
    for level in levels or ():
        try:
            price = float(level.get("price") if isinstance(level, Mapping) else level[0])
            shares = float((level.get("shares") if isinstance(level, Mapping) else level[1]) or 0.0)
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        if math.isfinite(price) and 0.0 < price < 1.0 and shares > 0.0:
            parsed.append((price, shares))
    if not parsed:
        return None
    parsed.sort()
    remaining = float(stake_usdc)
    size = 0.0
    cost = 0.0
    limit = parsed[0][0]
    for price, shares in parsed:
        if remaining <= 1e-9:
            break
        take = min(shares, remaining / price)
        size += take
        cost += take * price
        remaining -= take * price
        limit = price
    # The FOK is sent at the deepest level touched, and the exchange may fill
    # every share there if the shallower levels are gone by the time it
    # lands.  So the size is capped at what the stake buys AT THE LIMIT: the
    # worst case costs the stake, the walked case costs less, and a moved
    # book can never spend more than the twenty dollars the ruling named.
    size = min(size, float(stake_usdc) / limit)
    size = math.floor(size * (10 ** SHARE_PRECISION)) / (10 ** SHARE_PRECISION)
    if size < float(min_shares) or size <= 0.0:
        return None
    # Repriced at the walked levels for the floored size, never above the stake.
    cost = 0.0
    left = size
    for price, shares in parsed:
        if left <= 1e-12:
            break
        take = min(shares, left)
        cost += take * price
        left -= take
    return {
        "shares": size,
        "limit_price": float(limit),
        "walked_cost_usdc": round(cost, 6),
        "levels_touched": sum(1 for price, _s in parsed if price <= limit),
        "best_ask": parsed[0][0],
    }


# ---------------------------------------------------------------------------
# Reading the store.


def stage_first_decisions(
    conn: sqlite3.Connection, *, now_utc: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """The earliest fact of every (station, day, stage) on recent event days."""

    if not _has_table(conn, "basket_policy_training_fact"):
        return []
    floor = (_utc(now_utc) - timedelta(days=int(lookback_days))).date().isoformat()
    rows = conn.execute(
        "SELECT station_id, event_date, stage, MIN(decision_at_utc)"
        " FROM basket_policy_training_fact"
        " WHERE event_date >= ?"
        " GROUP BY station_id, event_date, stage",
        (floor,),
    ).fetchall()
    return [
        {"station_id": str(r[0]), "event_date": str(r[1]), "stage": str(r[2]), "decision_at_utc": str(r[3])}
        for r in rows
    ]


def _fact(conn: sqlite3.Connection, key: Mapping[str, Any]) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT fact_id, settlement_timezone, probability_envelope_id, ws_snapshot_id, decision_at_utc"
        " FROM basket_policy_training_fact"
        " WHERE station_id=? AND event_date=? AND stage=? AND decision_at_utc=?"
        " ORDER BY created_at_utc, fact_id LIMIT 1",
        (key["station_id"], key["event_date"], key["stage"], key["decision_at_utc"]),
    ).fetchone()
    if row is None:
        return None
    return {
        "fact_id": str(row[0]), "settlement_timezone": str(row[1]),
        "envelope_id": str(row[2]), "snapshot_id": str(row[3]), "decision_at_utc": str(row[4]),
    }


def _envelope(conn: sqlite3.Connection, envelope_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT probabilities_json, bucket_ordinals_json, content_sha256, decision_at_utc, state, stage"
        " FROM basket_probability_envelope WHERE envelope_id=?",
        (envelope_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        probabilities = json.loads(row[0] or "{}") or {}
        ordinals = json.loads(row[1] or "{}") or {}
    except ValueError:
        return None
    return {
        "probabilities": probabilities, "ordinals": ordinals, "content_sha256": str(row[2]),
        "decision_at_utc": str(row[3]), "state": str(row[4]), "stage": str(row[5]),
    }


def _snapshot(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT buckets_json, observed_at, state FROM basket_official_ws_snapshot WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        buckets = json.loads(row[0] or "[]") or []
    except ValueError:
        return None
    return {"buckets": buckets, "observed_at": str(row[1]), "state": str(row[2])}


def _evaluated(conn: sqlite3.Connection, *, since_event_date: str) -> set[tuple[str, str, str]]:
    return {
        (str(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            f"SELECT station_id, event_date, stage FROM {EVALUATION_TABLE} WHERE event_date >= ?",
            (since_event_date,),
        )
    }


def station_day_attempted(conn: sqlite3.Connection, station_id: str, event_date: str) -> bool:
    """One attempt per station-day: any intent this lane minted for it."""

    return conn.execute(
        "SELECT 1 FROM basket_target_order_intents WHERE decision_id=? LIMIT 1",
        (lane_decision_id(LANE, station_id, event_date),),
    ).fetchone() is not None


def _record(conn: sqlite3.Connection, key: Mapping[str, Any], fact: Mapping[str, Any] | None,
            reading: Mapping[str, Any], *, fired: bool, refusal: str | None,
            order_intent_id: str | None, plan: Mapping[str, Any] | None, now_utc: str,
            rule_version: str = RULE_VERSION) -> dict[str, Any]:
    body = {
        "station_id": key["station_id"], "event_date": key["event_date"], "stage": key["stage"],
        "fact_id": (fact or {}).get("fact_id", ""), "decision_at_utc": key["decision_at_utc"],
        "envelope_id": (fact or {}).get("envelope_id", ""), "snapshot_id": (fact or {}).get("snapshot_id", ""),
        "rule_version": rule_version, "reading": dict(reading), "fired": bool(fired),
        "refusal": refusal, "order_intent_id": order_intent_id, "plan": dict(plan) if plan else None,
    }
    sha = content_sha256(body)
    conn.execute(
        f"INSERT OR IGNORE INTO {EVALUATION_TABLE}(evaluation_id,station_id,event_date,stage,fact_id,"
        "decision_at_utc,envelope_id,snapshot_id,rule_version,model_mode,market_mode,mode_distance,"
        "market_share_on_model_mode,model_share_on_model_mode,market_mode_share,model_mode_share,"
        "fired,refusal,order_intent_id,plan_json,evaluated_at_utc,content_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"tiebreak_eval_{sha[:32]}", body["station_id"], body["event_date"], body["stage"],
            body["fact_id"], body["decision_at_utc"], body["envelope_id"], body["snapshot_id"],
            rule_version, reading.get("model_mode"), reading.get("market_mode"), reading.get("mode_distance"),
            reading.get("market_share_on_model_mode"), reading.get("model_share_on_model_mode"),
            reading.get("market_mode_share"), reading.get("model_mode_share"),
            1 if fired else 0, refusal, order_intent_id,
            json.dumps(plan, ensure_ascii=False, sort_keys=True) if plan else None,
            now_utc, sha,
        ),
    )
    return {**body, "content_sha256": sha}


# ---------------------------------------------------------------------------
# Planning.


def plan_tiebreak_entries(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    now_utc: str,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
) -> dict[str, Any]:
    """Score every unevaluated first decision; plan a buy for the ones that fire.

    Every scored decision is written to ``tiebreak_lane_evaluation`` here,
    fired or not, with the reason a fired one could not be planned; the
    executor records the send on the same row.
    """

    ensure_lane_schema(target_conn)
    cfg = {**TIEBREAK_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    stake = float(cfg.get("stake_usdc") or TIEBREAK_DEFAULT_SETTINGS["stake_usdc"])
    min_share = float(cfg.get("min_market_share_on_model_mode") or TIEBREAK_DEFAULT_SETTINGS["min_market_share_on_model_mode"])
    rule_name = str(cfg.get("rule") or DEFAULT_RULE)
    rule_version = rule_version_for(rule_name)
    max_share_raw = cfg.get("max_market_share_on_model_mode")
    max_share = None if max_share_raw is None else float(max_share_raw)
    fresh_seconds = float(cfg.get("first_decision_fresh_seconds") or TIEBREAK_DEFAULT_SETTINGS["first_decision_fresh_seconds"])
    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    now = _utc(now_utc)
    done = _evaluated(
        target_conn,
        since_event_date=(now - timedelta(days=DEFAULT_LOOKBACK_DAYS)).date().isoformat(),
    )
    evaluations: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    for key in stage_first_decisions(target_conn, now_utc=now_utc):
        if (key["station_id"], key["event_date"], key["stage"]) in done:
            continue
        fact = _fact(target_conn, key)
        if fact is None:
            evaluations.append(_record(target_conn, key, None, {}, fired=False, refusal="fact_missing",
                                       order_intent_id=None, plan=None, now_utc=now_utc, rule_version=rule_version))
            continue
        age = (now - _utc(fact["decision_at_utc"])).total_seconds()
        if age > fresh_seconds:
            evaluations.append(_record(target_conn, key, fact, {"age_seconds": round(age, 1)}, fired=False,
                                       refusal="first_decision_stale", order_intent_id=None, plan=None, now_utc=now_utc, rule_version=rule_version))
            continue
        envelope = _envelope(target_conn, fact["envelope_id"])
        snapshot = _snapshot(target_conn, fact["snapshot_id"])
        if envelope is None or snapshot is None:
            evaluations.append(_record(target_conn, key, fact, {}, fired=False,
                                       refusal="envelope_or_snapshot_missing", order_intent_id=None, plan=None, now_utc=now_utc, rule_version=rule_version))
            continue
        market = market_reference_v1(snapshot["buckets"])
        if market.get("state") != "complete":
            evaluations.append(_record(target_conn, key, fact, {"market_state": market.get("state"), "market_reason": market.get("reason")},
                                       fired=False, refusal=f"market_incomplete:{market.get('reason') or market.get('state')}",
                                       order_intent_id=None, plan=None, now_utc=now_utc, rule_version=rule_version))
            continue
        reading = evaluate_rule(
            envelope["probabilities"], envelope["ordinals"], market["vector"],
            min_market_share=min_share, max_market_share=max_share, rule=rule_name,
        )
        if not reading["fired"]:
            evaluations.append(_record(target_conn, key, fact, reading, fired=False,
                                       refusal=f"rule_not_met:{reading['reason']}", order_intent_id=None, plan=None, now_utc=now_utc, rule_version=rule_version))
            continue
        refusal = _planning_refusal(target_conn, key, fact, gate=gate, now_utc=now_utc)
        if refusal:
            evaluations.append(_record(target_conn, key, fact, reading, fired=True, refusal=refusal,
                                       order_intent_id=None, plan=None, now_utc=now_utc, rule_version=rule_version))
            continue
        bucket = str(reading["model_mode"])
        registry = _registry_rows(target_conn, canonical_conn, station_id=key["station_id"], event_date=key["event_date"])
        market_row = registry.get(bucket)
        if market_row is None or not market_row.get("yes_token"):
            evaluations.append(_record(target_conn, key, fact, reading, fired=True, refusal="registry_token_missing",
                                       order_intent_id=None, plan=None, now_utc=now_utc, rule_version=rule_version))
            continue
        if not bool(market_row.get("accepting_orders")):
            evaluations.append(_record(target_conn, key, fact, reading, fired=True, refusal="market_not_accepting_orders",
                                       order_intent_id=None, plan=None, now_utc=now_utc, rule_version=rule_version))
            continue
        book_row = next((r for r in snapshot["buckets"] if str(r.get("bucket")) == bucket), None)
        min_shares = float(market_row.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
        walk = walk_ask_book((book_row or {}).get("ask_levels") or (), stake, min_shares=min_shares) if book_row and book_row.get("ask_available", True) else None
        if walk is None:
            evaluations.append(_record(target_conn, key, fact, reading, fired=True, refusal="ask_book_too_thin",
                                       order_intent_id=None, plan=None, now_utc=now_utc, rule_version=rule_version))
            continue
        cash_refusal = _cash_refusal(target_conn, now_utc=now_utc, needed=walk["walked_cost_usdc"])
        if cash_refusal:
            evaluations.append(_record(target_conn, key, fact, reading, fired=True, refusal=cash_refusal,
                                       order_intent_id=None, plan=None, now_utc=now_utc, rule_version=rule_version))
            continue
        plan = {
            "schema": SCHEMA, "rule": rule_version, "rule_name": rule_name, "lane": LANE, "title": LANE_TITLE,
            "station_id": key["station_id"], "event_date": key["event_date"], "stage": key["stage"],
            "settlement_timezone": fact["settlement_timezone"], "fact_id": fact["fact_id"],
            "decision_at_utc": fact["decision_at_utc"], "envelope_id": fact["envelope_id"],
            "envelope_sha256": envelope["content_sha256"], "envelope_decision_at_utc": envelope["decision_at_utc"],
            "snapshot_id": fact["snapshot_id"], "snapshot_observed_at": snapshot["observed_at"],
            "bucket": bucket, "token_id": str(market_row["yes_token"]),
            "tick_size": market_row.get("tick_size"), "min_size": market_row.get("min_size"),
            "stake_usdc": stake, "shares": walk["shares"], "limit_price": walk["limit_price"],
            "walked_cost_usdc": walk["walked_cost_usdc"], "best_ask": walk["best_ask"],
            "levels_touched": walk["levels_touched"], "reading": reading,
        }
        plan["order_intent_id"] = f"tiebreak_{content_sha256({'plan': plan})[:32]}"
        evaluation = _record(target_conn, key, fact, reading, fired=True, refusal=None,
                             order_intent_id=plan["order_intent_id"], plan=plan, now_utc=now_utc, rule_version=rule_version)
        evaluations.append(evaluation)
        plans.append(plan)
    return {"evaluations": evaluations, "plans": plans, "settings": cfg, "rule_version": rule_version}


def _planning_refusal(conn: sqlite3.Connection, key: Mapping[str, Any], fact: Mapping[str, Any], *,
                      gate: Admission, now_utc: str) -> str | None:
    if station_day_attempted(conn, key["station_id"], key["event_date"]):
        return "station_day_already_attempted"
    try:
        verdict = gate(key["station_id"], conn)
    except Exception as exc:  # noqa: BLE001 -- an unreadable gate admits nothing
        return f"station_admission_unreadable:{type(exc).__name__}"
    if verdict.get("admitted") is not True:
        return f"station_not_admitted:{verdict.get('reason') or 'refused'}"
    try:
        day = settlement_day_state(key["event_date"], as_of_utc=now_utc, settlement_timezone=fact["settlement_timezone"])
    except Exception as exc:  # noqa: BLE001
        return f"settlement_day_unknown:{type(exc).__name__}"
    if day.get("state") == "closed":
        return "settlement_day_closed"
    return None


def _cash_refusal(conn: sqlite3.Connection, *, now_utc: str, needed: float) -> str | None:
    """Charter 141's reading: a store with the account table but no fresh
    read refuses; a store without the table (a fixture) does not."""

    if not account_equity_table_present(conn):
        return None
    row = load_account_equity(conn, as_of_utc=now_utc)
    if row is None:
        # The resident reads the account at the head of its cycle; this lane
        # runs at the tail, and a slow cycle can put the head read past the
        # thirty-minute freshness bound.  Read once more before refusing --
        # the same capture the head uses -- and refuse only if that too
        # leaves nothing fresh.  A failed capture is a receipt, never a raise.
        try:
            capture_account_equity(
                conn, as_of_utc=now_utc, write=nullcontext,
                budget_usdc=_budget_dial(),
            )
        except Exception:  # noqa: BLE001 -- a failed read leaves the stale row
            pass
        row = load_account_equity(conn, as_of_utc=now_utc)
    if row is None:
        return "account_equity_stale"
    cash = float(row.get("cash_usdc") or 0.0)
    if cash < float(needed):
        return f"insufficient_cash:{cash:.2f}<{float(needed):.2f}"
    return None


def _budget_dial() -> float | None:
    try:
        from .production import PRODUCTION_EXECUTION_MODE  # noqa: PLC0415
        from .runtime_config import resolve_target_remaining_budget  # noqa: PLC0415

        return float(resolve_target_remaining_budget(execution_mode=PRODUCTION_EXECUTION_MODE))
    except Exception:  # noqa: BLE001 -- no dial means no cap
        return None


# ---------------------------------------------------------------------------
# Execution.


def execute_tiebreak_entries(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
) -> dict[str, Any]:
    """Score, plan, send -- through the shared buy sender and the one ledger."""

    planned = plan_tiebreak_entries(target_conn, canonical_conn, now_utc=now_utc, settings=settings, admission=admission)
    receipts: list[dict[str, Any]] = []
    submitted = 0
    filled = 0
    for plan in planned["plans"]:
        intent_id = plan["order_intent_id"]
        if target_conn.execute(
            "SELECT 1 FROM basket_target_order_intents WHERE order_intent_id=?", (intent_id,)
        ).fetchone():
            receipts.append({"order_intent_id": intent_id, "state": "already_minted"})
            continue
        details = {
            "schema": "tiebreak_lane_decision_v1", "lane": LANE, "title": LANE_TITLE, "rule": plan["rule"],
            "decision_signature": {"stage": plan["stage"], "lane": LANE, "rule": plan["rule"]},
            "forward_evidence_binding": {
                "decision_at_utc": now_utc,
                "probability_decision_at_utc": plan["envelope_decision_at_utc"],
                "probability_artifact_sha256": plan["envelope_sha256"],
                "probability_stage": plan["stage"],
                "probability_envelope_id": plan["envelope_id"],
                "ws_snapshot_id": plan["snapshot_id"],
            },
            "plan": plan,
        }
        entry = send_lane_buy(
            target_conn, lane=LANE, intent_id=intent_id,
            station_id=plan["station_id"], event_date=plan["event_date"],
            settlement_timezone=plan["settlement_timezone"], bucket=plan["bucket"],
            token_id=plan["token_id"], price=plan["limit_price"], shares=plan["shares"],
            execution_candidate=EXECUTION_CANDIDATE, fok_price_cap=plan["limit_price"],
            tick_size=(float(plan["tick_size"]) if plan.get("tick_size") is not None else None),
            intent_body={"schema": SCHEMA, "rule": plan["rule"], "strategy_id": STRATEGY_ID,
                         "order_type": "FOK", "post_only": False, "plan": plan},
            transport=transport, live_submit_authorized=live_submit_authorized,
            decision_details=details,
        )
        if entry.get("exchange_post_started"):
            submitted += 1
        if float(entry.get("matched_size") or 0.0) > 0.0:
            filled += 1
        target_conn.execute(
            f"UPDATE {EVALUATION_TABLE} SET refusal=? WHERE order_intent_id=?",
            (None if entry.get("exchange_post_started") else f"send:{entry.get('reason') or entry.get('state')}", intent_id),
        )
        receipts.append(entry)
    return {
        "schema": "tiebreak_lane_receipt_v1",
        "lane": LANE,
        "title": LANE_TITLE,
        "rule": planned["rule_version"],
        "settings": planned["settings"],
        "evaluated": len(planned["evaluations"]),
        "fired": sum(1 for e in planned["evaluations"] if e["fired"]),
        "planned": len(planned["plans"]),
        "submitted": submitted,
        "filled": filled,
        "refusals": sorted({str(e["refusal"]) for e in planned["evaluations"] if e.get("refusal")}),
        "receipts": receipts,
    }


__all__ = [
    "DEFAULT_RULE",
    "EVALUATION_TABLE",
    "LANE",
    "LANE_TITLE",
    "RULE_V1",
    "RULE_V2",
    "RULE_VERSION",
    "RULE_VERSIONS",
    "rule_version_for",
    "ensure_lane_schema",
    "evaluate_rule",
    "execute_tiebreak_entries",
    "plan_tiebreak_entries",
    "stage_first_decisions",
    "station_day_attempted",
    "walk_ask_book",
]
