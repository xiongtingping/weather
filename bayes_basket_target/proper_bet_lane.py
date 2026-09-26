"""Proper 下注线 -- log-proper betting on the whole ladder (charter 231).

User ruling 2026-09-26 (「调整下注规则」, then 「实盘上线，p＝天气模型」):
the betting rule of arXiv 2607.06166 replaces the basket's.  Not a basket,
not Kelly, not a value clamp: the pair of probability vectors ``(p, q)``
generates the position vector directly.

* ``p`` -- the weather model: the served envelope the stage's FIRST
  decision read (at P0 the calibrated NWP ensemble vector, charter 224's
  floor included).  Not the L4 posterior (charter 230): the ruling names the
  weather model.
* ``q`` -- the market: the normalised mid ladder of one fresh book
  (``market_reference_v1``, the same ``m`` L4 reads).
* The scoring rule is LogScore, so the proper position is
  ``s_k = log(p_k / q_k)`` on every bucket of the ladder; ``s > 0`` is the
  YES direction, ``s < 0`` the NO direction.  Both are traded.
* The bid/ask no-trade band decides whether a direction can be executed:
  BUY YES iff ``p_k > ask_k`` (YES ask); BUY NO iff ``p_k < bid_k`` (YES
  bid), i.e. ``1 - p_k > 1 - bid_k``, the NO ask the CLOB offers against the
  YES bid.  The pasted text had ``p_k < 1 - bid_k``; that buys NO at a
  price above its own claim and was corrected before the ruling (p = 0.30,
  bid 0.25: the text would buy NO at 0.75 for a 0.70 claim).  A bucket the
  band admits in the direction opposite to ``s`` (possible because ``q`` is
  the normalised mid and the band is the raw book) is not traded.
* Size: ``x_k = lambda * |s_k|`` shares, one ``lambda`` for the
  station-day.  The paper leaves ``lambda`` open; here it is set so the
  legs' worst-case cost ``sum |s_k| * c_k`` equals the stake
  (``stake_usdc``, capped by the free cash), ``c_k`` the leg's limit.  The
  venue's minimum order (five shares) cannot be met by every leg of a small
  stake, so legs are taken in order of ``|s|`` and the longest prefix whose
  smallest leg still clears the minimum is kept -- the ratios inside it are
  exactly the proper position's.  A leg is capped by the in-band depth.
* Execution: a limit FOK per leg at the worst in-band level (YES: the
  highest ask under ``p``; NO: one minus the lowest YES bid over ``p``),
  so every share filled is inside the band.  Held to settlement
  (fundamental, Corollary 4): one evaluation per stage first decision,
  only on the stages in ``opening_stages`` (P0, as the basket opened
  under charter 229).
* Attribution: each evaluation row keeps ``p``, ``q``, ``s`` and
  ``D = KL(q || p)``; at settlement ``Delta S = log(p_y / q_y)`` and
  ``L`` (what the fills paid over ``q``) close ``Pi = Delta S + D - L``
  (``scripts/proper_bet_attribution.py``).

What was measured before the ruling (docs/research/log_proper_betting_20260926,
sections 8, 9 and 13, 1,851 settled station-days): this protocol, two-sided,
Log shares, ROI -3.0% [-4.4, -1.7] on the served vector and -2.9% on pure P0,
the loss being about one spread.  The lane goes live on the ruling, not on
that reading; every evaluation is recorded so the forward sample is the whole
population.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any

from .account_equity import (
    capture_account_equity,
    load_account_equity,
    table_present as account_equity_table_present,
)
from .basket_kernel.valuation_v1 import market_reference_v1
from .certain_win_recycle import _registry_rows
from .contracts import content_sha256
from .exit_orders import Transport
from .lane_orders import send_lane_buy
from .live_station_scope import admission as live_station_admission
from .materialization import floor_probabilities
from .p1_tail_lane import (
    DEPTH_HEADROOM_FRACTION,
    _latest_snapshot,
    _sendable_shares,
    _ws_manifest_root,
    live_ws_snapshot,
)
from .settlement_time import settlement_day_state
from .strategy_lanes import (
    LANE_PROPER_BET,
    PROPER_BET_DEFAULT_SETTINGS,
    lane_settings,
    opening_stages,
)
from .tiebreak_lane import _budget_dial, stage_first_decisions

LANE = LANE_PROPER_BET
LANE_TITLE = "Proper 下注线"
STRATEGY_ID = "bayes_basket_target_v1"
SCHEMA = "proper_bet_lane_plan_v1"
RULE_LOG_V1 = "log_proper_bid_ask_band_two_sided_v1"
RULE_VERSIONS = {RULE_LOG_V1: "proper_bet_lane_v1"}
DEFAULT_RULE = RULE_LOG_V1
EXECUTION_CANDIDATE = "TAKER_FOK"
EVALUATION_TABLE = "proper_bet_lane_evaluation"
INTENT_PREFIX = "proper"
DEFAULT_MIN_ORDER_SHARES = 5.0
DEFAULT_LOOKBACK_DAYS = 3

#: The two directions a leg can take, as the ledger spells the outcome leg.
SIDE_YES = "YES"
SIDE_NO = "NO"

#: Refusals that leave the decision unrecorded while it is still young: the
#: next cycle asks again on its own book.  Past the freshness window the
#: last one is recorded.
WAIT_REFUSALS = ("book_unavailable", "book_stale", "market_incomplete")

EVALUATION_DDL = (
    f"CREATE TABLE IF NOT EXISTS {EVALUATION_TABLE} ("
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " stage TEXT NOT NULL,"
    " decision_at_utc TEXT NOT NULL,"
    " settlement_timezone TEXT,"
    " fact_id TEXT,"
    " envelope_id TEXT,"
    " envelope_sha256 TEXT,"
    " snapshot_id TEXT,"
    " book_observed_at TEXT,"
    " book_source TEXT,"
    " rule_version TEXT NOT NULL,"
    " p_json TEXT,"
    " q_json TEXT,"
    " s_json TEXT,"
    " divergence_kl REAL,"
    " stake_usdc REAL,"
    " budget_usdc REAL,"
    " lambda_shares REAL,"
    " buckets_json TEXT,"
    " planned_legs INTEGER NOT NULL DEFAULT 0,"
    " planned_cost_usdc REAL NOT NULL DEFAULT 0,"
    " refusal TEXT,"
    " sends_json TEXT,"
    " evaluated_at_utc TEXT NOT NULL,"
    " PRIMARY KEY (station_id, event_date, stage)"
    ")"
)

Admission = Callable[[str, sqlite3.Connection], Mapping[str, Any]]


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    conn.execute(EVALUATION_DDL)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _levels(raw: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for level in raw or ():
        try:
            price = float(level.get("price"))
            size = float(level.get("shares") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if size > 0.0 and math.isfinite(price) and 0.0 < price < 1.0:
            out.append((price, size))
    return out


# ---------------------------------------------------------------------------
# The rule: (p, q) -> s -> band -> legs.  Pure; the store is read below.


def proper_position(
    p: Mapping[str, float], q: Mapping[str, float]
) -> dict[str, float]:
    """``s_k = log(p_k / q_k)`` -- the LogScore proper position."""

    return {bucket: math.log(float(p[bucket]) / float(q[bucket])) for bucket in q}


def divergence_kl(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    """``D = KL(q || p) = sum q_k log(q_k / p_k)``, the Log rule's divergence."""

    return sum(
        float(q[b]) * math.log(float(q[b]) / float(p[b]))
        for b in q if float(q[b]) > 0.0
    )


def band_leg(
    bucket: str,
    p: float,
    s: float,
    book_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """One bucket through the bid/ask no-trade band.

    YES: the ask levels priced strictly under ``p``; the limit is the highest
    of them.  NO: the YES bid levels priced strictly over ``p`` -- each is a
    NO ask at ``1 - bid`` -- and the limit is one minus the lowest of them.
    ``depth`` is the in-band size, in shares of the leg's own token.
    """

    asks = sorted(_levels((book_row or {}).get("ask_levels")))
    if not bool((book_row or {}).get("ask_available", True)):
        asks = []
    bids = sorted(_levels((book_row or {}).get("bid_levels")), reverse=True)
    best_ask = asks[0][0] if asks else None
    best_bid = bids[0][0] if bids else None
    out: dict[str, Any] = {
        "bucket": bucket, "p": p, "s": s, "best_ask": best_ask, "best_bid": best_bid,
        "band": "none", "side": None, "limit_price": None, "depth_shares": 0.0,
    }
    yes_levels = [(price, size) for price, size in asks if price < p - 1e-12]
    no_levels = [(price, size) for price, size in bids if price > p + 1e-12]
    if yes_levels:
        out["band"] = SIDE_YES
    elif no_levels:
        out["band"] = SIDE_NO
    else:
        return out
    if (out["band"] == SIDE_YES and not s > 0.0) or (out["band"] == SIDE_NO and not s < 0.0):
        out["band"] = "sign_conflict"
        return out
    if out["band"] == SIDE_YES:
        out["side"] = SIDE_YES
        out["limit_price"] = round(max(price for price, _size in yes_levels), 6)
        out["depth_shares"] = sum(size for _price, size in yes_levels)
    else:
        out["side"] = SIDE_NO
        out["limit_price"] = round(1.0 - min(price for price, _size in no_levels), 6)
        out["depth_shares"] = sum(size for _price, size in no_levels)
    return out


def size_legs(
    legs: list[dict[str, Any]],
    *,
    budget_usdc: float,
    min_shares: Mapping[str, float],
) -> dict[str, Any]:
    """``x_k = lambda * |s_k|`` on the longest feasible prefix by ``|s|``.

    ``lambda`` makes the prefix's worst-case cost ``sum |s_k| * c_k`` equal
    the budget.  A prefix is feasible when its smallest leg is still at least
    that leg's venue minimum; adding a leg (smaller ``|s|``) only lowers
    ``lambda`` and the smallest ``|s|``, so the feasible prefixes are an
    initial run.  Each kept leg is then capped by its in-band depth (less a
    headroom, as the tail lane's takes) and rounded to what the venue takes;
    one that falls under its minimum there is dropped with its reason.
    """

    ranked = sorted(
        (leg for leg in legs if leg.get("side")),
        key=lambda leg: (-abs(float(leg["s"])), str(leg["bucket"])),
    )
    kept: list[dict[str, Any]] = []
    lam = None
    for n in range(1, len(ranked) + 1):
        prefix = ranked[:n]
        denominator = sum(abs(float(leg["s"])) * float(leg["limit_price"]) for leg in prefix)
        if not denominator > 0.0:
            break
        candidate = float(budget_usdc) / denominator
        if all(
            candidate * abs(float(leg["s"])) >= float(min_shares.get(leg["bucket"], DEFAULT_MIN_ORDER_SHARES))
            for leg in prefix
        ):
            kept = prefix
            lam = candidate
        else:
            break
    sized: list[dict[str, Any]] = []
    dropped: dict[str, str] = {}
    for leg in ranked:
        if leg not in kept:
            dropped[str(leg["bucket"])] = "below_min_size_at_budget"
            continue
        target = float(lam) * abs(float(leg["s"]))
        depth = float(leg["depth_shares"])
        usable = max(0.0, min(depth * DEPTH_HEADROOM_FRACTION, depth - 1.0))
        affordable = min(target, usable)
        shares = _sendable_shares(float(leg["limit_price"]), affordable)
        floor = float(min_shares.get(leg["bucket"], DEFAULT_MIN_ORDER_SHARES))
        if shares is None or shares < floor:
            dropped[str(leg["bucket"])] = (
                f"thin_in_band_depth:{depth:.2f}" if usable < target
                else f"below_min_size:{(shares or 0.0):.4f}<{floor:.2f}"
            )
            continue
        sized.append({
            **leg,
            "target_shares": round(target, 6),
            "shares": float(shares),
            "cost_usdc": round(float(shares) * float(leg["limit_price"]), 6),
        })
    return {"lambda_shares": lam, "legs": sized, "dropped": dropped}


def evaluate_ladder(
    probabilities: Mapping[str, float],
    book_buckets: list[Mapping[str, Any]],
    *,
    budget_usdc: float,
    min_shares: Mapping[str, float],
) -> dict[str, Any]:
    """The whole rule on one ladder: ``p``, ``q``, ``s``, ``D``, band, legs."""

    market = market_reference_v1(book_buckets)
    if market.get("state") != "complete":
        return {"refusal": "market_incomplete", "market_reason": market.get("reason")}
    q = {str(k): float(v) for k, v in market["vector"].items()}
    raw_p = {str(k): float(v) for k, v in (probabilities or {}).items()}
    if set(raw_p) != set(q):
        return {
            "refusal": "ladder_mismatch",
            "model_only": sorted(set(raw_p) - set(q)),
            "market_only": sorted(set(q) - set(raw_p)),
        }
    total = sum(raw_p.values())
    if not (total > 0.0 and math.isfinite(total)):
        return {"refusal": "model_vector_invalid"}
    p, floored = floor_probabilities({b: v / total for b, v in raw_p.items()})
    s = proper_position(p, q)
    divergence = divergence_kl(p, q)
    rows = {str(r.get("bucket")): r for r in book_buckets}
    bands = [band_leg(b, p[b], s[b], rows.get(b)) for b in sorted(q, key=lambda b: rows.get(b, {}).get("ordinal", 0))]
    sizing = size_legs(bands, budget_usdc=budget_usdc, min_shares=min_shares)
    out = {
        "refusal": None,
        "p": p, "q": q, "s": s, "divergence_kl": divergence,
        "p_floored_buckets": floored,
        "mid_sum": market.get("mid_sum"),
        "bands": bands,
        "lambda_shares": sizing["lambda_shares"],
        "legs": sizing["legs"],
        "dropped": sizing["dropped"],
    }
    if not any(b.get("side") for b in bands):
        out["refusal"] = "no_bucket_outside_band"
    elif not sizing["legs"]:
        out["refusal"] = "no_leg_at_budget"
    return out


# ---------------------------------------------------------------------------
# Reading the store.


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
        "SELECT probabilities_json, content_sha256, decision_at_utc, stage"
        " FROM basket_probability_envelope WHERE envelope_id=?",
        (envelope_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        probabilities = json.loads(row[0] or "{}") or {}
    except ValueError:
        return None
    return {
        "probabilities": probabilities, "content_sha256": str(row[1]),
        "decision_at_utc": str(row[2]), "stage": str(row[3]),
    }


def _evaluated(conn: sqlite3.Connection, *, since_event_date: str) -> set[tuple[str, str, str]]:
    return {
        (str(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            f"SELECT station_id, event_date, stage FROM {EVALUATION_TABLE} WHERE event_date >= ?",
            (since_event_date,),
        )
    }


def _book(
    conn: sqlite3.Connection,
    root: Mapping[str, Any] | None,
    *,
    station_id: str,
    event_date: str,
    now_utc: str,
    fresh_seconds: float,
    live: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """The freshest ladder: the WS publisher's own, else the committed one."""

    candidates: list[dict[str, Any]] = []
    if live:
        book = live_ws_snapshot(conn, root, station_id=station_id, event_date=event_date, now_utc=now_utc)
        if book is not None:
            candidates.append(book)
    committed = _latest_snapshot(conn, station_id, event_date)
    if committed is not None:
        candidates.append({**committed, "book_source": "committed_snapshot"})
    if not candidates:
        return None, "book_unavailable"
    now = _utc(now_utc)
    for book in candidates:
        try:
            age = (now - _utc(book["observed_at"])).total_seconds()
        except (TypeError, ValueError, KeyError):
            continue
        if age <= float(fresh_seconds):
            return {**book, "age_seconds": round(age, 1)}, None
    return None, "book_stale"


def _free_cash(conn: sqlite3.Connection, *, now_utc: str) -> tuple[float | None, str | None]:
    """The account's cash, read as the tiebreak lane reads it (charter 141)."""

    if not account_equity_table_present(conn):
        return None, None
    row = load_account_equity(conn, as_of_utc=now_utc)
    if row is None:
        try:
            capture_account_equity(conn, as_of_utc=now_utc, write=nullcontext, budget_usdc=_budget_dial())
        except Exception:  # noqa: BLE001 -- a failed read leaves the stale row
            pass
        row = load_account_equity(conn, as_of_utc=now_utc)
    if row is None:
        return None, "account_equity_stale"
    return float(row.get("cash_usdc") or 0.0), None


def _planning_refusal(
    conn: sqlite3.Connection, key: Mapping[str, Any], fact: Mapping[str, Any], *,
    gate: Admission, now_utc: str,
) -> str | None:
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


def _record(
    conn: sqlite3.Connection,
    key: Mapping[str, Any],
    fact: Mapping[str, Any] | None,
    *,
    rule_version: str,
    now_utc: str,
    refusal: str | None,
    envelope: Mapping[str, Any] | None = None,
    book: Mapping[str, Any] | None = None,
    reading: Mapping[str, Any] | None = None,
    stake: float | None = None,
    budget: float | None = None,
) -> dict[str, Any]:
    reading = dict(reading or {})
    legs = list(reading.get("legs") or [])
    row = {
        "station_id": key["station_id"], "event_date": key["event_date"], "stage": key["stage"],
        "decision_at_utc": key["decision_at_utc"],
        "settlement_timezone": (fact or {}).get("settlement_timezone"),
        "fact_id": (fact or {}).get("fact_id"),
        "envelope_id": (fact or {}).get("envelope_id"),
        "envelope_sha256": (envelope or {}).get("content_sha256"),
        "snapshot_id": (book or {}).get("snapshot_id"),
        "book_observed_at": (book or {}).get("observed_at"),
        "book_source": (book or {}).get("book_source"),
        "rule_version": rule_version,
        "p_json": json.dumps(reading.get("p"), sort_keys=True) if reading.get("p") else None,
        "q_json": json.dumps(reading.get("q"), sort_keys=True) if reading.get("q") else None,
        "s_json": json.dumps(reading.get("s"), sort_keys=True) if reading.get("s") else None,
        "divergence_kl": reading.get("divergence_kl"),
        "stake_usdc": stake,
        "budget_usdc": budget,
        "lambda_shares": reading.get("lambda_shares"),
        "buckets_json": json.dumps(
            {"bands": reading.get("bands"), "dropped": reading.get("dropped"),
             **({k: reading[k] for k in ("market_reason", "model_only", "market_only") if k in reading})},
            ensure_ascii=False, sort_keys=True,
        ) if reading else None,
        "planned_legs": len(legs),
        "planned_cost_usdc": round(sum(float(leg["cost_usdc"]) for leg in legs), 6),
        "refusal": refusal,
        "sends_json": None,
        "evaluated_at_utc": now_utc,
    }
    columns = ",".join(row)
    conn.execute(
        f"INSERT OR IGNORE INTO {EVALUATION_TABLE}({columns}) VALUES({','.join('?' for _ in row)})",
        tuple(row.values()),
    )
    return row


# ---------------------------------------------------------------------------
# Plan and send.


def plan_proper_bets(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    now_utc: str,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
    ws_manifest_path: Any = None,
) -> dict[str, Any]:
    """Judge every unevaluated first decision on an opening stage; plan its legs."""

    ensure_lane_schema(target_conn)
    cfg = {**PROPER_BET_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    rule_name = str(cfg.get("rule") or DEFAULT_RULE)
    if rule_name not in RULE_VERSIONS:
        raise ValueError(f"proper_bet_rule_unknown:{rule_name}")
    rule_version = RULE_VERSIONS[rule_name]
    stake = float(cfg.get("stake_usdc") or PROPER_BET_DEFAULT_SETTINGS["stake_usdc"])
    fresh_seconds = float(cfg.get("first_decision_fresh_seconds") or PROPER_BET_DEFAULT_SETTINGS["first_decision_fresh_seconds"])
    book_fresh = float(cfg.get("send_book_fresh_seconds") or PROPER_BET_DEFAULT_SETTINGS["send_book_fresh_seconds"])
    live = cfg.get("live_ws_book", True) is True
    stages = opening_stages(cfg)
    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    now = _utc(now_utc)
    done = _evaluated(
        target_conn,
        since_event_date=(now - timedelta(days=DEFAULT_LOOKBACK_DAYS)).date().isoformat(),
    )
    manifest: dict[str, Any] = {}

    def manifest_root() -> Mapping[str, Any] | None:
        if "root" not in manifest:
            manifest["root"] = _ws_manifest_root(ws_manifest_path) if live else None
        return manifest["root"]

    cash_state: dict[str, Any] = {}
    evaluations: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for key in stage_first_decisions(target_conn, now_utc=now_utc, lookback_days=DEFAULT_LOOKBACK_DAYS):
        if str(key["stage"]) not in stages:
            continue
        if (key["station_id"], key["event_date"], key["stage"]) in done:
            continue
        fact = _fact(target_conn, key)
        if fact is None:
            evaluations.append(_record(target_conn, key, None, rule_version=rule_version, now_utc=now_utc,
                                       refusal="fact_missing"))
            continue
        age = (now - _utc(fact["decision_at_utc"])).total_seconds()
        young = age <= fresh_seconds
        refusal = _planning_refusal(target_conn, key, fact, gate=gate, now_utc=now_utc)
        if refusal:
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal=refusal))
            continue
        if not young:
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal="first_decision_stale"))
            continue
        envelope = _envelope(target_conn, fact["envelope_id"])
        if envelope is None:
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal="envelope_missing"))
            continue
        book, book_refusal = _book(
            target_conn, manifest_root(), station_id=key["station_id"], event_date=key["event_date"],
            now_utc=now_utc, fresh_seconds=book_fresh, live=live,
        )
        if book is None:
            waiting.append({**key, "reason": book_refusal})
            continue
        registry = _registry_rows(target_conn, canonical_conn, station_id=key["station_id"], event_date=key["event_date"])
        min_shares: dict[str, float] = {}
        tokens: dict[str, dict[str, Any]] = {}
        for bucket, row in registry.items():
            try:
                raw = json.loads(row.get("registry_json") or "{}")
            except ValueError:
                raw = {}
            min_shares[bucket] = float(row.get("min_size") or DEFAULT_MIN_ORDER_SHARES)
            tokens[bucket] = {
                SIDE_YES: str(row.get("yes_token") or ""),
                SIDE_NO: str(raw.get("no_token") or ""),
                "tick_size": row.get("tick_size"),
                "accepting_orders": bool(row.get("accepting_orders")),
            }
        if "cash" not in cash_state:
            cash_state["cash"], cash_state["refusal"] = _free_cash(target_conn, now_utc=now_utc)
        if cash_state.get("refusal"):
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal=cash_state["refusal"], envelope=envelope, book=book, stake=stake))
            continue
        budget = stake if cash_state.get("cash") is None else max(0.0, min(stake, float(cash_state["cash"])))
        reading = evaluate_ladder(envelope["probabilities"], list(book["buckets"]),
                                  budget_usdc=budget, min_shares=min_shares)
        if reading.get("refusal") == "market_incomplete" and young:
            waiting.append({**key, "reason": f"market_incomplete:{reading.get('market_reason')}"})
            continue
        if reading.get("refusal"):
            refusal = reading["refusal"]
            if refusal == "no_leg_at_budget" and budget < stake:
                refusal = f"insufficient_cash:{budget:.2f}<{stake:.2f}"
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal=refusal, envelope=envelope, book=book, reading=reading,
                                       stake=stake, budget=budget))
            continue
        legs: list[dict[str, Any]] = []
        unsendable: dict[str, str] = {}
        for leg in reading["legs"]:
            token = tokens.get(str(leg["bucket"])) or {}
            token_id = str(token.get(leg["side"]) or "")
            if not token_id:
                unsendable[str(leg["bucket"])] = f"registry_{leg['side'].lower()}_token_missing"
                continue
            if not token.get("accepting_orders"):
                unsendable[str(leg["bucket"])] = "market_not_accepting_orders"
                continue
            legs.append({**leg, "token_id": token_id, "tick_size": token.get("tick_size")})
        reading = {**reading, "legs": legs, "dropped": {**reading["dropped"], **unsendable}}
        if not legs:
            evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                       refusal="no_sendable_leg", envelope=envelope, book=book, reading=reading,
                                       stake=stake, budget=budget))
            continue
        if cash_state.get("cash") is not None:
            cash_state["cash"] = float(cash_state["cash"]) - sum(float(leg["cost_usdc"]) for leg in legs)
        plan = {
            "schema": SCHEMA, "rule": rule_version, "rule_name": rule_name, "lane": LANE, "title": LANE_TITLE,
            "station_id": key["station_id"], "event_date": key["event_date"], "stage": key["stage"],
            "settlement_timezone": fact["settlement_timezone"], "fact_id": fact["fact_id"],
            "decision_at_utc": fact["decision_at_utc"], "envelope_id": fact["envelope_id"],
            "envelope_sha256": envelope["content_sha256"], "envelope_decision_at_utc": envelope["decision_at_utc"],
            "snapshot_id": book["snapshot_id"], "book_observed_at": book["observed_at"],
            "book_source": book.get("book_source"), "execution_candidate": EXECUTION_CANDIDATE,
            "stake_usdc": stake, "budget_usdc": budget, "lambda_shares": reading["lambda_shares"],
            "divergence_kl": reading["divergence_kl"],
            "legs": [
                {k: leg[k] for k in ("bucket", "side", "p", "s", "best_ask", "best_bid", "limit_price",
                                     "depth_shares", "target_shares", "shares", "cost_usdc", "token_id",
                                     "tick_size")}
                for leg in legs
            ],
        }
        plan["plan_id"] = f"{INTENT_PREFIX}_{content_sha256({'plan': plan})[:32]}"
        evaluations.append(_record(target_conn, key, fact, rule_version=rule_version, now_utc=now_utc,
                                   refusal=None, envelope=envelope, book=book, reading=reading,
                                   stake=stake, budget=budget))
        plans.append({**plan, "q": reading["q"], "p_vector": reading["p"], "s_vector": reading["s"]})
    return {"evaluations": evaluations, "plans": plans, "waiting": waiting, "settings": cfg,
            "rule_version": rule_version}


def execute_proper_bets(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
    ws_manifest_path: Any = None,
) -> dict[str, Any]:
    """Judge, plan, and send each leg as its own limit FOK."""

    planned = plan_proper_bets(
        target_conn, canonical_conn, now_utc=now_utc, settings=settings,
        admission=admission, ws_manifest_path=ws_manifest_path,
    )
    receipts: list[dict[str, Any]] = []
    submitted = 0
    filled = 0
    for plan in planned["plans"]:
        details = {
            "schema": "proper_bet_lane_decision_v1", "lane": LANE, "title": LANE_TITLE, "rule": plan["rule"],
            "decision_signature": {"stage": plan["stage"], "lane": LANE, "rule": plan["rule"]},
            "forward_evidence_binding": {
                "decision_at_utc": now_utc,
                "probability_decision_at_utc": plan["envelope_decision_at_utc"],
                "probability_artifact_sha256": plan["envelope_sha256"],
                "probability_stage": plan["stage"],
                "probability_envelope_id": plan["envelope_id"],
                "ws_snapshot_id": plan["snapshot_id"],
            },
            "plan": {k: v for k, v in plan.items() if k not in ("q", "p_vector", "s_vector")},
            "proper_position": {"p": plan["p_vector"], "q": plan["q"], "s": plan["s_vector"],
                                "divergence_kl": plan["divergence_kl"]},
        }
        sends: list[dict[str, Any]] = []
        for leg in plan["legs"]:
            intent_id = f"{INTENT_PREFIX}_" + content_sha256({
                "plan_id": plan["plan_id"], "bucket": leg["bucket"], "side": leg["side"],
            })[:32]
            if target_conn.execute(
                "SELECT 1 FROM basket_target_order_intents WHERE order_intent_id=?", (intent_id,)
            ).fetchone():
                sends.append({"bucket": leg["bucket"], "side": leg["side"], "order_intent_id": intent_id,
                              "state": "already_minted"})
                continue
            entry = send_lane_buy(
                target_conn, lane=LANE, intent_id=intent_id,
                station_id=plan["station_id"], event_date=plan["event_date"],
                settlement_timezone=plan["settlement_timezone"], bucket=leg["bucket"],
                token_id=leg["token_id"], price=float(leg["limit_price"]), shares=float(leg["shares"]),
                side=leg["side"],
                execution_candidate=EXECUTION_CANDIDATE,
                fok_price_cap=float(leg["limit_price"]),
                tick_size=(float(leg["tick_size"]) if leg.get("tick_size") not in (None, "") else None),
                intent_body={"schema": SCHEMA, "rule": plan["rule"], "strategy_id": STRATEGY_ID,
                             "order_type": "FOK", "post_only": False, "plan_id": plan["plan_id"],
                             "leg": leg},
                transport=transport, live_submit_authorized=live_submit_authorized,
                decision_details=details,
            )
            if entry.get("exchange_post_started"):
                submitted += 1
            if float(entry.get("matched_size") or 0.0) > 0.0:
                filled += 1
            sends.append({
                "bucket": leg["bucket"], "side": leg["side"], "order_intent_id": intent_id,
                "shares": leg["shares"], "limit_price": leg["limit_price"],
                "state": entry.get("state"), "event_type": entry.get("event_type"),
                "reason": entry.get("reason"),
                "matched_size": entry.get("matched_size"),
                "matched_notional_usdc": entry.get("matched_notional_usdc"),
            })
        target_conn.execute(
            f"UPDATE {EVALUATION_TABLE} SET sends_json=? WHERE station_id=? AND event_date=? AND stage=?",
            (json.dumps(sends, ensure_ascii=False, sort_keys=True),
             plan["station_id"], plan["event_date"], plan["stage"]),
        )
        receipts.append({"station_id": plan["station_id"], "event_date": plan["event_date"],
                         "stage": plan["stage"], "plan_id": plan["plan_id"], "sends": sends})
    return {
        "schema": "proper_bet_lane_receipt_v1",
        "lane": LANE,
        "title": LANE_TITLE,
        "rule": planned["rule_version"],
        "settings": planned["settings"],
        "evaluated": len(planned["evaluations"]),
        "planned": len(planned["plans"]),
        "planned_legs": sum(len(p["legs"]) for p in planned["plans"]),
        "waiting": len(planned["waiting"]),
        "submitted": submitted,
        "filled": filled,
        "refusals": sorted({str(e["refusal"]) for e in planned["evaluations"] if e.get("refusal")}),
        "receipts": receipts,
    }


__all__ = [
    "DEFAULT_RULE",
    "EVALUATION_TABLE",
    "EXECUTION_CANDIDATE",
    "LANE",
    "LANE_TITLE",
    "RULE_LOG_V1",
    "RULE_VERSIONS",
    "SIDE_NO",
    "SIDE_YES",
    "band_leg",
    "divergence_kl",
    "ensure_lane_schema",
    "evaluate_ladder",
    "execute_proper_bets",
    "plan_proper_bets",
    "proper_position",
    "size_legs",
]
