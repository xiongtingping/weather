"""共识线 -- D0, P1 and the market on one bucket at local noon (charter 280).

User ruling 2026-10-23 「做成车道」, on the convergence reading
(``docs/research/estimate_convergence_20261023``, RESULTS.md): the board's six
estimates -- the listing's deterministic F, member-trajectory Bayes, D0, the
observation-updated MOS, P1 and the market -- each point to a bucket; at local
12:00 on the event day, when every one of them present (at least three) points
to the same bucket, that bucket settled 80% of the time against a 0.71 ask
(n 70, +13% a leg [+6, +22], 7 of 8 days positive; 10 h +12%, 11 h +4%, 13 h
+1%).  Eight event days, a cell picked among about twenty -- a reading the
lane measures forward, not a conclusion.

Rule v2 (charter 280 补记一, user ruling 2026-10-23 「把共识线投票改成 D0+P1+市场
三家全一致，当地 12 点，单笔 5 U 不变」; ``subset_search.py`` in the same folder):
the voters are the lane's ``voters`` setting, D0, P1 and the market.  Among
every combination of the board's estimates only those with D0 agreeing with
the market paid at 12 h; D0+P1+market n120 +6.9% a leg [+3.7, +10.9], both
halves positive, 7 of 8 days; none without D0 did.  v1 counted every estimate
present, F included -- with F on record every day since 10-20 that was a
five-way rule the history has under twenty legs of.

Once a station-day: at the first cycle inside the entry window
(``entry_local_hour`` to ``+entry_window_minutes`` local) every estimate is read
as of that moment with ``estimate_hit_readings.picks_at`` -- the readers the
research and the board's confidence card use (MOS is not read here: it needs
Open-Meteo and satellite fetches the production cycle does not make) -- and
written down; the voters must all be present and agree -> FOK YES on that
bucket at the best ask, sized up to ``stake_usdc`` and capped by the depth
there, ``min_ask`` <= ask <= ``max_ask`` (asks under 0.05 were stale books in
the reading), held to settlement.  Anything else is written down once and the
station-day is done.  Every station-day judged lands in
``consensus_lane_evaluation`` with its votes, so the forward read is the whole
population.  Nothing is read or written while ``data/live_paused.flag`` is up
or the arming flag is down.

The votes set the bucket only; they set no price and no size, and nothing
here enters P0, P0-M or P1.  The member-Bayes, D0 and F inputs are display
readings (charter 267, the board's) read by a lane by this charter's explicit
exception.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import estimate_hit_readings as votes
from . import lane_orders
from .certain_win_recycle import _registry_rows
from .contracts import content_sha256
from .exit_orders import Transport
from .fade_lane import MIN_MARKETABLE_NOTIONAL_USDC
from .lane_orders import send_lane_buy
from .live_station_scope import admission as live_station_admission
from .p1_tail_lane import _ws_manifest_root, open_buy_orders_on_station_day, taker_take
from .settlement_time import hours_to_settlement, settlement_day_state
from .strategy_lanes import CONSENSUS_NOON_DEFAULT_SETTINGS, LANE_CONSENSUS_NOON, lane_settings
from .tail_short_lane import (
    DEFAULT_MIN_ORDER_SHARES,
    PAUSE_FLAG_PATH,
    _book,
    rests_on_another_bucket,
    station_days_in_window,
    taker_fee_usdc,
    yes_quote,
)
from .tiebreak_lane import _cash_refusal, settle_stuck_lane_cycles

STRATEGY_ID = "bayes_basket_target_v1"
SCHEMA = "consensus_lane_plan_v1"
RULE = "noon_d0_p1_market_agree_yes_taker_v2"
RULE_VERSION = "consensus_lane_v2"
EXECUTION_CANDIDATE = "TAKER_FOK"
EVALUATION_TABLE = "consensus_lane_evaluation"
INTENT_PREFIX = "consensus"
TITLE = "共识线"
#: Every estimate the lane reads and writes down (the board's six less MOS);
#: the ones that vote are the ``voters`` setting.
READ = ("F", "member_bayes", "D0", "P1", "market")
#: The verdicts of the vote itself; one of these ends the station-day.
SIGNAL_REFUSALS = frozenset({"signal:too_few_voters", "signal:not_all_agree"})
ALL_STAGES = frozenset({"P0", "P0-M", "P1"})

Admission = Callable[[str, sqlite3.Connection], Mapping[str, Any]]

EVALUATION_DDL = (
    f"CREATE TABLE IF NOT EXISTS {EVALUATION_TABLE} ("
    " evaluation_id TEXT PRIMARY KEY,"
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " bucket TEXT NOT NULL,"
    " evaluated_at_utc TEXT NOT NULL,"
    " local_time TEXT,"
    " rule_version TEXT NOT NULL,"
    " stage TEXT,"
    " settlement_timezone TEXT,"
    " hours_to_settlement REAL,"
    " votes_json TEXT,"
    " voters INTEGER,"
    " agree INTEGER,"
    " snapshot_id TEXT,"
    " book_source TEXT,"
    " book_observed_at TEXT,"
    " book_age_seconds REAL,"
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
    " send_json TEXT"
    ")"
)
EVALUATION_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_scope_idx"
    f" ON {EVALUATION_TABLE}(station_id, event_date, bucket)"
)


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    conn.execute(EVALUATION_DDL)
    conn.execute(EVALUATION_INDEX_DDL)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The rule.  Pure.


def consensus_reading(picks: Mapping[str, str | None], *, voters: Sequence[str], min_voters: int) -> dict[str, Any]:
    """The vote on one station-day: the bucket every voter present points to,
    or why not.  ``votes`` carries every estimate read, voting or not."""

    present = {m: picks.get(m) for m in voters if picks.get(m) is not None}
    buckets = list(present.values())
    best = max((buckets.count(b) for b in buckets), default=0)
    top = next((b for b in buckets if buckets.count(b) == best), None)
    out = {"votes": {m: picks.get(m) for m in READ}, "voters": len(buckets), "agree": best, "bucket": top,
           "refusal": None}
    if len(buckets) < int(min_voters):
        out["refusal"] = "signal:too_few_voters"
    elif best != len(buckets):
        out["refusal"] = "signal:not_all_agree"
    return out


def in_entry_window(now: datetime, zone: ZoneInfo, event_date: str, *, hour: int, minutes: float) -> bool:
    start = datetime.combine(datetime.fromisoformat(event_date).date(), datetime.min.time(), zone) + timedelta(hours=hour)
    return start <= now.astimezone(zone) < start + timedelta(minutes=float(minutes))


# ---------------------------------------------------------------------------
# Reading the store.


def lane_cost_today(conn: sqlite3.Connection, *, now_utc: str) -> float:
    """USDC the lane spent, or may have spent, on the UTC day (``fade_lane``'s reading)."""

    day = _utc(now_utc).date().isoformat()
    row = conn.execute(
        f"SELECT COALESCE(SUM(CASE"
        f"  WHEN filled_shares > 0 THEN filled_shares * price + COALESCE(fee_usdc_est, 0)"
        f"  WHEN json_extract(send_json, '$.event_type') = 'EXPIRED' THEN 0"
        f"  ELSE COALESCE(cost_usdc, 0) + COALESCE(fee_usdc_est, 0) END), 0)"
        f" FROM {EVALUATION_TABLE} WHERE sent=1 AND substr(evaluated_at_utc,1,10)=?",
        (day,),
    ).fetchone()
    return float(row[0] or 0.0)


def _history(conn: sqlite3.Connection, station_id: str, event_date: str) -> dict[str, Any]:
    """The station-day's rows: filled, sends, orders, the last unsent order, the refusals."""

    out: dict[str, Any] = {"filled": 0.0, "ledger_filled": False, "sends": 0, "orders": 0, "unsent_at": None,
                           "refusals": set(), "bucket": None}
    for bucket, filled, sent, refusal, intent, at in conn.execute(
        f"SELECT bucket, filled_shares, sent, refusal, order_intent_id, evaluated_at_utc"
        f" FROM {EVALUATION_TABLE} WHERE station_id=? AND event_date=? ORDER BY evaluated_at_utc",
        (station_id, event_date),
    ):
        out["bucket"] = out["bucket"] or str(bucket)
        out["filled"] += float(filled or 0.0)
        out["sends"] += int(sent or 0)
        if intent:
            out["orders"] += 1
            if not sent and not float(filled or 0.0) > 0.0 and str(at) > str(out["unsent_at"] or ""):
                out["unsent_at"] = str(at)
        if refusal:
            out["refusals"].add(str(refusal))
    if conn.execute(
        f"SELECT 1 FROM {EVALUATION_TABLE} AS e JOIN basket_target_order_intents AS i"
        " ON i.order_intent_id = e.order_intent_id"
        " WHERE e.station_id=? AND e.event_date=? AND i.state='filled' LIMIT 1",
        (station_id, event_date),
    ).fetchone():
        out["ledger_filled"] = True
    return out


def _record(conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(row)
    body.setdefault("sent", 0)
    body.setdefault("filled_shares", 0.0)
    body["evaluation_id"] = f"{INTENT_PREFIX}_eval_" + content_sha256({
        k: body.get(k) for k in ("station_id", "event_date", "bucket", "evaluated_at_utc", "refusal", "order_intent_id")
    })[:32]
    conn.execute(
        f"INSERT OR IGNORE INTO {EVALUATION_TABLE}({','.join(body)}) VALUES({','.join('?' for _ in body)})",
        tuple(body.values()),
    )
    return body


# ---------------------------------------------------------------------------
# Planning and sending.


def plan_consensus(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None,
    *,
    now_utc: str,
    cfg: Mapping[str, Any],
    gate: Admission,
    manifest_root: Callable[[], Mapping[str, Any] | None],
    read_votes: Callable[[sqlite3.Connection, str, str, datetime], dict[str, str | None] | None] | None = None,
) -> dict[str, Any]:
    """Every station-day inside its local entry window: read the votes once,
    and when they all agree plan the take."""

    now = _utc(now_utc)
    hour, minutes = int(cfg["entry_local_hour"]), float(cfg["entry_window_minutes"])
    stake = float(cfg["stake_usdc"])
    fee_rate = float(cfg.get("taker_fee_rate", 0.0))
    budget_left = float(cfg["max_daily_cost_usdc"]) - lane_cost_today(target_conn, now_utc=now_utc)
    read_votes = read_votes or _read_votes
    plans: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for scope in station_days_in_window(
        target_conn, now_utc=now_utc, lookback_days=int(cfg.get("lookback_days", 1)), stages=ALL_STAGES,
    ):
        station, day, tz = scope["station_id"], scope["event_date"], scope["settlement_timezone"]
        try:
            zone = ZoneInfo(tz)
            if settlement_day_state(day, as_of_utc=now_utc, settlement_timezone=tz).get("state") == "closed":
                skip("settlement_day_closed")
                continue
            hours = float(hours_to_settlement(day, as_of_utc=now_utc, settlement_timezone=tz))
        except Exception:  # noqa: BLE001 -- a day whose clock cannot be read is not traded
            skip("settlement_day_unknown")
            continue
        if not in_entry_window(now, zone, day, hour=hour, minutes=minutes):
            skip("outside_window")
            continue
        past = _history(target_conn, station, day)
        if past["filled"] > 0.0 or past["ledger_filled"]:
            skip("held")
            continue
        if past["refusals"] & SIGNAL_REFUSALS:
            # The vote is read once a station-day (the measured snapshot).
            skip("voted")
            continue
        base: dict[str, Any] = {
            "station_id": station, "event_date": day, "evaluated_at_utc": now_utc,
            "local_time": now.astimezone(zone).strftime("%H:%M"), "rule_version": RULE_VERSION,
            "stage": scope["stage"], "settlement_timezone": tz, "hours_to_settlement": round(hours, 3),
        }
        if past["bucket"] is None:
            picks = read_votes(target_conn, station, day, now.astimezone(zone))
            if picks is None:
                skip("ladder_unavailable")
                continue
            vote = consensus_reading(picks, voters=tuple(cfg["voters"]), min_voters=int(cfg["min_voters"]))
            base.update(votes_json=json.dumps(vote["votes"], ensure_ascii=False, sort_keys=True),
                        voters=vote["voters"], agree=vote["agree"])
            if vote["refusal"]:
                evaluations.append(_record(target_conn, {
                    **base, "bucket": vote["bucket"] or "", "refusal": vote["refusal"]}))
                continue
            bucket = str(vote["bucket"])
        else:
            # Agreed on an earlier cycle and not yet filled: the same bucket,
            # retried inside the window.
            bucket = past["bucket"]
        base["bucket"] = bucket

        def refuse(reason: str, **extra: Any) -> None:
            if reason in past["refusals"]:
                return
            past["refusals"].add(reason)
            evaluations.append(_record(target_conn, {**base, **extra, "refusal": reason}))

        if past["sends"] >= int(cfg["max_attempts_per_bucket"]):
            refuse("max_attempts_reached")
            continue
        if past["orders"] >= int(cfg["max_orders_per_bucket"]):
            refuse("max_orders_reached")
            continue
        if past["unsent_at"] and (now - _utc(past["unsent_at"])).total_seconds() < float(cfg["unsent_retry_seconds"]):
            skip("unsent_retry_wait")
            continue
        book, book_refusal = _book(
            target_conn, manifest_root(), station_id=station, event_date=day, now_utc=now_utc,
            fresh_seconds=float(cfg["send_book_fresh_seconds"]), live=cfg.get("live_ws_book", True) is True,
        )
        if book is None:
            refuse(str(book_refusal))
            continue
        rows = {str(r.get("bucket")): r for r in book["buckets"]}
        quote = yes_quote(rows.get(bucket))
        base.update(snapshot_id=book.get("snapshot_id"), book_source=book.get("book_source"),
                    book_observed_at=book.get("observed_at"), book_age_seconds=book.get("age_seconds"),
                    yes_bid=quote["bid"], yes_ask=quote["ask"])
        if quote["ask"] is None:
            refuse("no_ask")
            continue
        if not float(cfg["min_ask"]) - 1e-12 <= float(quote["ask"]) <= float(cfg["max_ask"]) + 1e-12:
            refuse(f"ask_outside:{float(quote['ask']):.3f}")
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
        market_row = _registry_rows(target_conn, canonical_conn, station_id=station, event_date=day).get(bucket) or {}
        token = str(market_row.get("yes_token") or "")
        if not token:
            refuse("registry_token_missing")
            continue
        if not bool(market_row.get("accepting_orders")):
            refuse("market_not_accepting_orders")
            continue
        take = taker_take(rows.get(bucket), stake, tick_size=market_row.get("tick_size"),
                          min_shares=float(market_row.get("min_size") or DEFAULT_MIN_ORDER_SHARES),
                          walk_ticks=int(cfg.get("walk_ticks", 0)))
        if take.get("refusal"):
            refuse(str(take["refusal"]))
            continue
        price, shares = float(take["limit_price"]), float(take["shares"])
        if price > float(cfg["max_ask"]) + 1e-12:
            refuse(f"ask_outside:{price:.3f}")
            continue
        cost = round(price * shares, 6)
        if cost < MIN_MARKETABLE_NOTIONAL_USDC - 1e-9:
            refuse(f"below_min_notional:{cost:.2f}<{MIN_MARKETABLE_NOTIONAL_USDC:.2f}")
            continue
        fee = taker_fee_usdc(price, shares, fee_rate)
        if cost + fee > budget_left + 1e-9:
            refuse(f"daily_budget:{max(budget_left, 0.0):.2f}")
            continue
        cash_refusal = _cash_refusal(target_conn, now_utc=now_utc, needed=cost + fee)
        if cash_refusal:
            refuse(cash_refusal)
            continue
        plan = {
            "schema": SCHEMA, "rule": RULE_VERSION, "rule_name": RULE, "lane": LANE_CONSENSUS_NOON, "title": TITLE,
            "station_id": station, "event_date": day, "settlement_timezone": tz, "stage": scope["stage"],
            "bucket": bucket, "side": "YES", "token_id": token, "execution_candidate": EXECUTION_CANDIDATE,
            "order_type": "FOK", "post_only": False, "limit_price": price, "fok_price_cap": price,
            "shares": shares, "cost_usdc": cost, "fee_usdc_est": fee, "taker_fee_rate": fee_rate,
            "tick_size": market_row.get("tick_size"), "min_size": market_row.get("min_size"),
            "stake_usdc": stake, "attempt": past["sends"] + 1,
            **{k: base.get(k) for k in ("hours_to_settlement", "local_time", "votes_json", "voters", "agree",
                                         "snapshot_id", "book_source", "book_observed_at", "book_age_seconds",
                                         "yes_bid", "yes_ask")},
        }
        plan["order_intent_id"] = f"{INTENT_PREFIX}_{content_sha256({'plan': plan, 'at': now_utc})[:32]}"
        evaluations.append(_record(target_conn, {
            **base, "price": price, "shares": shares, "cost_usdc": cost, "fee_usdc_est": fee,
            "refusal": None, "order_intent_id": plan["order_intent_id"],
        }))
        budget_left -= cost + fee
        plans.append(plan)
    return {"plans": plans, "evaluations": evaluations, "skipped": skipped}


def _read_votes(conn: sqlite3.Connection, station: str, day: str, when: datetime) -> dict[str, str | None] | None:
    """The voters' buckets as of ``when`` with the board's own readers."""

    ladder = votes._rungs(conn, station, day)
    if ladder is None:
        return None
    return votes.picks_at(conn, station, day, when, ladder, f=votes._f_ledger().get((station, day)), wrf={},
                          d0=votes._history("d0", day), mb=votes._history("member_bayes", day), mos=None)


def execute_consensus(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
    ws_manifest_path: Any = None,
    read_votes: Callable[..., dict[str, str | None] | None] | None = None,
) -> dict[str, Any]:
    """Vote on every station-day in its noon window and send the takes."""

    stopped = ("live_paused" if PAUSE_FLAG_PATH.exists()
               else "live_intent_disarmed" if not lane_orders.live_intent_armed() else None)
    if stopped:
        return {"schema": "consensus_lane_receipt_v1", "lane": LANE_CONSENSUS_NOON, "title": TITLE, "state": stopped,
                "evaluated": 0, "planned": 0, "submitted": 0, "filled": 0}
    ensure_lane_schema(target_conn)
    cfg = {**CONSENSUS_NOON_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE_CONSENSUS_NOON))}
    if str(cfg.get("rule")) != RULE:
        raise ValueError(f"consensus_rule_unknown:{cfg.get('rule')}")
    cycles_settled = settle_stuck_lane_cycles(target_conn)
    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    live = cfg.get("live_ws_book", True) is True
    manifest: dict[str, Any] = {}

    def manifest_root() -> Mapping[str, Any] | None:
        if "root" not in manifest:
            manifest["root"] = _ws_manifest_root(ws_manifest_path) if live else None
        return manifest["root"]

    planned = plan_consensus(target_conn, canonical_conn, now_utc=now_utc, cfg=cfg, gate=gate,
                             manifest_root=manifest_root, read_votes=read_votes)
    receipts: list[dict[str, Any]] = []
    submitted = filled = 0
    for plan in planned["plans"]:
        details = {
            "schema": "consensus_lane_decision_v1", "lane": LANE_CONSENSUS_NOON, "title": TITLE, "rule": plan["rule"],
            "decision_signature": {"stage": plan["stage"], "lane": LANE_CONSENSUS_NOON, "rule": plan["rule"]},
            "forward_evidence_binding": {"decision_at_utc": now_utc, "votes": plan.get("votes_json"),
                                         "ws_snapshot_id": plan["snapshot_id"]},
            "plan": plan,
        }
        entry = send_lane_buy(
            target_conn, lane=LANE_CONSENSUS_NOON, intent_id=plan["order_intent_id"],
            station_id=plan["station_id"], event_date=plan["event_date"],
            settlement_timezone=plan["settlement_timezone"], bucket=plan["bucket"],
            token_id=plan["token_id"], price=plan["limit_price"], shares=plan["shares"],
            side="YES", execution_candidate=EXECUTION_CANDIDATE, fok_price_cap=float(plan["fok_price_cap"]),
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
            f"UPDATE {EVALUATION_TABLE} SET sent=?, filled_shares=?, refusal=?, send_json=? WHERE order_intent_id=?",
            (int(sent), matched, outcome, json.dumps(send, ensure_ascii=False, sort_keys=True), plan["order_intent_id"]),
        )
        receipts.append({"station_id": plan["station_id"], "event_date": plan["event_date"], "bucket": plan["bucket"],
                         "price": plan["limit_price"], "shares": plan["shares"],
                         "order_intent_id": plan["order_intent_id"], **send})
    return {
        "schema": "consensus_lane_receipt_v1", "lane": LANE_CONSENSUS_NOON, "title": TITLE, "rule": RULE_VERSION,
        "settings": cfg, "evaluated": len(planned["evaluations"]), "planned": len(planned["plans"]),
        "submitted": submitted, "filled": filled, "skipped": planned["skipped"],
        "refusals": sorted({str(e["refusal"]) for e in planned["evaluations"] if e.get("refusal")}),
        "cycles_settled": cycles_settled, "receipts": receipts,
    }


__all__ = [
    "EVALUATION_TABLE", "READ", "RULE", "RULE_VERSION", "SIGNAL_REFUSALS", "TITLE", "consensus_reading",
    "ensure_lane_schema", "execute_consensus", "in_entry_window", "lane_cost_today", "plan_consensus",
]
