"""同心线 (the P1 consensus lane): the four centres agree, in the afternoon, and the
bucket is still cheap.  Named 2026-09-23 (user: 「新策略叫什么？取个名字」): the
model's and the market's mode and mean -- four centres, 四心 -- on one bucket.

Charter 203 (user ruling 2026-09-23: 「上线，一个站日可以买多次！」, then rule A,
no yielding to the tiebreak lane, 5 USDC a take).

The condition, on one P1 decision (a ``basket_policy_training_fact`` row):

  * the station-day is 6 to 10 hours from its settlement clock
    (``settlement_time.hours_to_settlement`` at the decision instant);
  * the model's mode bucket, the model's mean bucket, the market's mode
    bucket and the market's mean bucket are one bucket -- both vectors
    renormalised over the buckets they share, the modes read by the tiebreak
    lane's own ``evaluate_rule``, a mean bucket the rung nearest
    ``sum(p x ordinal)`` rounded half up;
  * that bucket's best ask is in [0.50, 0.70).

Then the lane crosses FOK at the ask for ``stake_usdc``.  Every decision that
meets the condition is a take -- a station-day may be bought several times,
the same bucket again included -- up to ``max_buys_per_station_day``, and no
sooner than ``min_minutes_between_buys`` after the station-day's last take
(user ruling 2026-09-23 「加 20 分钟间隔」: the replay's successive takes sat
6-91 minutes apart; live, LTAC bought four times in four minutes).

Measured 2026-09-23, read-only, over every settled P1 decision 07-28..09-22
(10,100 decisions with 4-12 h left, priced on the first committed book after
the decision was committed, 20 USDC a take):

    every decision meeting the condition   117 takes / 90 station-days / 33 days
                                           hit 71.8%, ask 0.627, +15.3% [+4.8%, +25.3%]
    first half / second half               +34.8% / +10.2%
    event days from 09-20 (27 takes)       +10.6% [+0.8%, +20.5%]
    priced 2 / 5 / 15 minutes later        +13.1% / +12.8% / +12.2%

It is NOT a speed trade -- the edge keeps its size a quarter of an hour
late -- so the lane runs where the tiebreak lane runs, at the head and tail
of the cycle, on facts up to ``fact_fresh_seconds`` old.

Stated plainly: the cell was found by slicing (first the four-centre
agreement, then the 6-10 h bin, then the 0.50-0.70 band, then "every
decision"), and the multiple-comparison reading of its narrowest ancestor was
p=0.054.  Its forward days are three.  The lane ships small and records every
decision in the window, fired or not, so the question can be re-asked on days
nobody chose.

Against the tiebreak lane (charters 146/163/175), user ruling 2026-09-23
「照买，不避让」: no exclusion either way.  The tiebreak lane reads only stage-
first decisions (a P1 first decision sits a median 13 h from the clock; its
replay has never fired inside 6-10 h) and counts only its own intents; this
lane buys whatever the tiebreak lane holds or rests, and records both on the
row (``held_buckets``, ``open_order_*``) so the overlap is readable.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .basket_kernel.valuation_v1 import market_reference_v1
from .certain_win_recycle import _registry_rows
from .contracts import content_sha256
from .exit_orders import Transport
from .lane_orders import lane_decision_id, send_lane_buy
from .live_station_scope import admission as live_station_admission
from .p1_tail_lane import (
    _latest_snapshot,
    _ws_manifest_root,
    held_buckets,
    live_ws_snapshot,
    open_order_on_station_day,
    taker_take,
)
from .settlement_time import hours_to_settlement, settlement_day_state
from .strategy_lanes import (
    LANE_P1_CONSENSUS,
    P1_CONSENSUS_DEFAULT_SETTINGS,
    lane_settings,
)
from .supply_watch import egress_held_sql
from .tiebreak_lane import (
    _cash_refusal,
    _envelope,
    book_features,
    evaluate_rule as tiebreak_evaluate_rule,
    settle_stuck_lane_cycles,
)

LANE = LANE_P1_CONSENSUS
LANE_TITLE = "同心线"
STRATEGY_ID = "bayes_basket_target_v1"
SCHEMA = "p1_consensus_lane_plan_v1"
RULE_V1 = "p1_four_centre_consensus_taker_v1"
RULE_VERSIONS = {RULE_V1: "p1_consensus_lane_v1"}
DEFAULT_RULE = RULE_V1
RULE_VERSION = RULE_VERSIONS[DEFAULT_RULE]
EXECUTION_CANDIDATE = "TAKER_FOK"
EVALUATION_TABLE = "p1_consensus_lane_evaluation"
INTENT_PREFIX = "p1cons_"
DEFAULT_LOOKBACK_DAYS = 2
DEFAULT_MIN_ORDER_SHARES = 5.0
#: Intent states that are a position or may still become one.
OPEN_OR_FILLED_STATES = frozenset({
    "prepared", "submitting", "submitted", "reconcile_required", "filled",
})

Admission = Callable[[str, sqlite3.Connection], Mapping[str, Any]]

EVALUATION_DDL = (
    f"CREATE TABLE IF NOT EXISTS {EVALUATION_TABLE} ("
    " evaluation_id TEXT PRIMARY KEY,"
    " fact_id TEXT NOT NULL UNIQUE,"
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " settlement_timezone TEXT NOT NULL,"
    " decision_at_utc TEXT NOT NULL,"
    " envelope_id TEXT NOT NULL,"
    " snapshot_id TEXT,"
    " snapshot_observed_at TEXT,"
    " book_source TEXT,"
    " rule_version TEXT NOT NULL,"
    " remaining_hours REAL,"
    " fact_age_seconds REAL,"
    " book_age_seconds REAL,"
    " model_mode TEXT,"
    " model_mean TEXT,"
    " market_mode TEXT,"
    " market_mean TEXT,"
    " four_same INTEGER,"
    " model_share REAL,"
    " market_share REAL,"
    " best_ask REAL,"
    " best_bid REAL,"
    " held_buckets TEXT,"
    " open_order_lane TEXT,"
    " open_order_bucket TEXT,"
    " station_day_buys INTEGER,"
    " fired INTEGER NOT NULL CHECK(fired IN (0,1)),"
    " refusal TEXT,"
    " order_intent_id TEXT,"
    " plan_json TEXT,"
    " evaluated_at_utc TEXT NOT NULL,"
    " content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)"
    ")"
)
EVALUATION_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_day_idx"
    f" ON {EVALUATION_TABLE}(event_date,fired,evaluated_at_utc)",
    f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_scope_idx"
    f" ON {EVALUATION_TABLE}(station_id,event_date,decision_at_utc)",
    f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_intent_idx"
    f" ON {EVALUATION_TABLE}(order_intent_id)",
)


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    """Statement by statement, never ``executescript``: the resident calls
    this inside the writer's transaction."""

    conn.execute(EVALUATION_DDL)
    for ddl in EVALUATION_INDEX_DDL:
        conn.execute(ddl)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def rule_version_for(rule: Any) -> str:
    name = str(rule or DEFAULT_RULE)
    if name not in RULE_VERSIONS:
        raise ValueError(f"p1_consensus_rule_unknown:{name}")
    return RULE_VERSIONS[name]


# ---------------------------------------------------------------------------
# The reading.


def mean_bucket(
    distribution: Mapping[str, float], ordinals: Mapping[str, int]
) -> tuple[str, float]:
    """The rung nearest ``sum(p x ordinal)``, rounded half up; a tie between
    two rungs (a gap in the ladder) goes to the lower one."""

    mu = sum(float(distribution[b]) * int(ordinals[b]) for b in distribution)
    target = math.floor(mu + 0.5)
    bucket = min(distribution, key=lambda b: (abs(int(ordinals[b]) - target), int(ordinals[b])))
    return bucket, mu


def evaluate_consensus(
    probabilities: Mapping[str, Any],
    ordinals: Mapping[str, Any],
    market_vector: Mapping[str, Any],
) -> dict[str, Any]:
    """The four centres of one decision, and whether they are one bucket.

    The modes are the tiebreak lane's own reading (``evaluate_rule`` with no
    floor): both vectors renormalised over the buckets they share, ties to
    the lower rung.  The means are read on the same renormalised vectors.
    """

    base = tiebreak_evaluate_rule(
        probabilities, ordinals, market_vector, min_market_share=0.0,
    )
    if not base.get("model_mode") or not base.get("market_mode"):
        return {"state": base.get("reason") or "unread", "four_same": False}
    ords = {str(b): int(o) for b, o in (ordinals or {}).items()}
    p = {str(b): float(v) for b, v in (probabilities or {}).items() if str(b) in ords and float(v) >= 0.0}
    m = {str(b): float(v) for b, v in (market_vector or {}).items() if str(b) in ords and float(v) >= 0.0}
    common = sorted(set(p) & set(m), key=lambda b: ords[b])
    tp = sum(p[b] for b in common)
    tm = sum(m[b] for b in common)
    pn = {b: p[b] / tp for b in common}
    mn = {b: m[b] / tm for b in common}
    model_mean, model_mu = mean_bucket(pn, ords)
    market_mean, market_mu = mean_bucket(mn, ords)
    model_mode = str(base["model_mode"])
    market_mode = str(base["market_mode"])
    four_same = model_mode == model_mean == market_mode == market_mean
    return {
        "state": "read",
        "model_mode": model_mode,
        "model_mean": model_mean,
        "market_mode": market_mode,
        "market_mean": market_mean,
        "model_mu": round(model_mu, 6),
        "market_mu": round(market_mu, 6),
        "four_same": bool(four_same),
        "model_share": round(pn[market_mode], 6),
        "market_share": round(mn[market_mode], 6),
        "buckets_in_common": len(common),
    }


# ---------------------------------------------------------------------------
# Reading the store.


def candidate_facts(
    conn: sqlite3.Connection,
    *,
    now_utc: str,
    min_remaining_hours: float,
    max_remaining_hours: float,
    fresh_seconds: float,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """The P1 decisions young enough to judge, inside the hours window.

    Two steps so neither reads a fact row's body in a scan: the keys come
    off the scope index as a covering scan, then each row is one seek.  The
    window reaches four freshness spans back, so a decision the lane missed
    (a long cycle, a restart) is recorded as stale rather than never seen.
    One decision second is one decision: when two facts share it, the one
    committed last is the one that served.
    """

    now = _utc(now_utc)
    floor_day = (now - timedelta(days=int(lookback_days))).date().isoformat()
    since = _z(now - timedelta(seconds=float(fresh_seconds) * 4))
    keys = conn.execute(
        "SELECT DISTINCT station_id, event_date, decision_at_utc FROM basket_policy_training_fact"
        " WHERE stage='P1' AND event_date>=? AND decision_at_utc>=?",
        (floor_day, since),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for station, day, decided in sorted(keys, key=lambda k: (str(k[2]), str(k[0]), str(k[1]))):
        row = conn.execute(
            "SELECT fact_id, settlement_timezone, probability_envelope_id, ws_snapshot_id,"
            " created_at_utc FROM basket_policy_training_fact"
            " WHERE station_id=? AND event_date=? AND stage='P1' AND decision_at_utc=?"
            " ORDER BY created_at_utc DESC, fact_id DESC LIMIT 1",
            (station, day, decided),
        ).fetchone()
        if row is None:
            continue
        try:
            hours = hours_to_settlement(
                str(day), as_of_utc=str(decided), settlement_timezone=str(row[1]),
            )
        except Exception:  # noqa: BLE001 -- no clock, no window
            continue
        if not (float(min_remaining_hours) <= hours < float(max_remaining_hours)):
            continue
        out.append({
            "fact_id": str(row[0]),
            "station_id": str(station),
            "event_date": str(day),
            "settlement_timezone": str(row[1]),
            "decision_at_utc": str(decided),
            "envelope_id": str(row[2]),
            "fact_snapshot_id": str(row[3]),
            "created_at_utc": str(row[4]),
            "remaining_hours": round(float(hours), 4),
            "fact_age_seconds": round((now - _utc(str(decided))).total_seconds(), 1),
        })
    return out


def _evaluated_facts(conn: sqlite3.Connection, *, since_event_date: str) -> set[str]:
    return {
        str(r[0]) for r in conn.execute(
            f"SELECT fact_id FROM {EVALUATION_TABLE} WHERE event_date>=?",
            (since_event_date,),
        )
    }


def station_day_buys(conn: sqlite3.Connection, station_id: str, event_date: str) -> int:
    """Takes of this lane on the station-day that are a position or may
    still become one.  A FOK the venue killed, or a send the egress hold
    kept in, bought nothing and does not count."""

    count = 0
    for state, filled in conn.execute(
        "SELECT i.state, COALESCE(r.filled_size, 0) FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " WHERE i.decision_id=?",
        (lane_decision_id(LANE, station_id, event_date),),
    ):
        if float(filled or 0.0) > 0.0 or str(state) in OPEN_OR_FILLED_STATES:
            count += 1
    return count


def station_day_sends(conn: sqlite3.Connection, station_id: str, event_date: str) -> int:
    """Every intent this lane minted for the station-day, filled or not,
    except the ones the egress hold never let out."""

    return int(conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_intents i"
        f" WHERE i.decision_id=? AND NOT ({egress_held_sql('i')})",
        (lane_decision_id(LANE, station_id, event_date),),
    ).fetchone()[0] or 0)


def last_buy_decision_at(
    conn: sqlite3.Connection, station_id: str, event_date: str
) -> str | None:
    """The decision instant of this lane's latest take on the station-day
    that is a position or may still become one.

    Read off the lane's own rows: a take's row carries its intent id and no
    refusal until the send says otherwise, so a take planned earlier in the
    same pass counts, and one the venue killed (``send:*``) or the egress
    hold kept in does not.
    """

    row = conn.execute(
        f"SELECT MAX(decision_at_utc) FROM {EVALUATION_TABLE}"
        " WHERE station_id=? AND event_date=? AND order_intent_id IS NOT NULL"
        " AND refusal IS NULL",
        (station_id, event_date),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _record(
    conn: sqlite3.Connection,
    candidate: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any] | None,
    book_age: float | None,
    reading: Mapping[str, Any],
    book: Mapping[str, Any],
    context: Mapping[str, Any],
    fired: bool,
    refusal: str | None,
    order_intent_id: str | None,
    plan: Mapping[str, Any] | None,
    now_utc: str,
    rule_version: str,
) -> dict[str, Any]:
    body = {
        "fact_id": candidate["fact_id"],
        "station_id": candidate["station_id"],
        "event_date": candidate["event_date"],
        "decision_at_utc": candidate["decision_at_utc"],
        "envelope_id": candidate["envelope_id"],
        "snapshot_id": (snapshot or {}).get("snapshot_id"),
        "rule_version": rule_version,
        "reading": dict(reading),
        "book": dict(book),
        "fired": bool(fired),
        "refusal": refusal,
        "order_intent_id": order_intent_id,
        "plan": dict(plan) if plan else None,
    }
    sha = content_sha256(body)
    conn.execute(
        f"INSERT OR IGNORE INTO {EVALUATION_TABLE}(evaluation_id,fact_id,station_id,event_date,"
        "settlement_timezone,decision_at_utc,envelope_id,snapshot_id,snapshot_observed_at,"
        "book_source,rule_version,remaining_hours,fact_age_seconds,book_age_seconds,model_mode,"
        "model_mean,market_mode,market_mean,four_same,model_share,market_share,best_ask,best_bid,"
        "held_buckets,open_order_lane,open_order_bucket,station_day_buys,fired,refusal,"
        "order_intent_id,plan_json,evaluated_at_utc,content_sha256)"
        " VALUES(" + ",".join("?" for _ in range(33)) + ")",
        (
            f"p1cons_eval_{sha[:32]}", candidate["fact_id"], candidate["station_id"],
            candidate["event_date"], candidate["settlement_timezone"],
            candidate["decision_at_utc"], candidate["envelope_id"],
            (snapshot or {}).get("snapshot_id"), (snapshot or {}).get("observed_at"),
            (snapshot or {}).get("book_source") or ("committed_snapshot" if snapshot else None),
            rule_version, candidate.get("remaining_hours"), candidate.get("fact_age_seconds"),
            None if book_age is None else round(float(book_age), 1),
            reading.get("model_mode"), reading.get("model_mean"),
            reading.get("market_mode"), reading.get("market_mean"),
            None if "four_same" not in reading else int(bool(reading["four_same"])),
            reading.get("model_share"), reading.get("market_share"),
            book.get("best_ask"), book.get("best_bid"),
            context.get("held_buckets"), context.get("open_order_lane"),
            context.get("open_order_bucket"), context.get("station_day_buys"),
            1 if fired else 0, refusal, order_intent_id,
            json.dumps(plan, ensure_ascii=False, sort_keys=True) if plan else None,
            now_utc, sha,
        ),
    )
    return {**body, "content_sha256": sha}


# ---------------------------------------------------------------------------
# Planning.


def _planning_refusal(
    conn: sqlite3.Connection,
    candidate: Mapping[str, Any],
    *,
    gate: Admission,
    now_utc: str,
    max_buys: int,
    max_sends: int,
    planned_this_pass: int,
    min_minutes_between_buys: float = 0.0,
) -> str | None:
    station = candidate["station_id"]
    day = candidate["event_date"]
    if station_day_buys(conn, station, day) + planned_this_pass >= int(max_buys):
        return f"station_day_buy_cap:{int(max_buys)}"
    # User ruling 2026-09-23 「加 20 分钟间隔」: a station-day may be bought
    # several times, but not on consecutive decisions a minute apart -- the
    # replay's successive takes sat 6-91 minutes apart (most 15-40), and
    # LTAC 2026-09-23 bought four times in four minutes at one price.
    last = last_buy_decision_at(conn, station, day)
    if last is not None and float(min_minutes_between_buys) > 0.0:
        gap = (_utc(candidate["decision_at_utc"]) - _utc(last)).total_seconds() / 60.0
        if gap < float(min_minutes_between_buys):
            return (
                f"station_day_buy_spacing:{gap:.1f}m<{float(min_minutes_between_buys):.0f}m"
            )
    if station_day_sends(conn, station, day) + planned_this_pass >= int(max_sends):
        return f"station_day_send_cap:{int(max_sends)}"
    try:
        verdict = gate(station, conn)
    except Exception as exc:  # noqa: BLE001 -- an unreadable gate admits nothing
        return f"station_admission_unreadable:{type(exc).__name__}"
    if verdict.get("admitted") is not True:
        return f"station_not_admitted:{verdict.get('reason') or 'refused'}"
    try:
        day_state = settlement_day_state(
            day, as_of_utc=now_utc, settlement_timezone=candidate["settlement_timezone"],
        )
    except Exception as exc:  # noqa: BLE001
        return f"settlement_day_unknown:{type(exc).__name__}"
    if day_state.get("state") == "closed":
        return "settlement_day_closed"
    return None


def plan_p1_consensus_entries(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    now_utc: str,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
    ws_manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Judge every unjudged P1 decision in the window; plan a take where the
    four centres agree on a bucket the market still sells in the band."""

    ensure_lane_schema(target_conn)
    repairs = {"cycles_settled": settle_stuck_lane_cycles(target_conn)}
    cfg = {**P1_CONSENSUS_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    rule_name = str(cfg.get("rule") or DEFAULT_RULE)
    rule_version = rule_version_for(rule_name)

    def dial(name: str) -> float:
        """A dial the operator set, falling back only when it is ABSENT
        (``or`` would read a deliberate zero as unset)."""

        value = cfg.get(name)
        if value is None:
            value = P1_CONSENSUS_DEFAULT_SETTINGS[name]
        return float(value)

    stake = dial("stake_usdc")
    min_ask = dial("min_ask")
    max_ask = dial("max_ask")
    max_paid = dial("max_paid_price")
    fresh_seconds = dial("fact_fresh_seconds")
    book_fresh = dial("book_fresh_seconds")
    send_book_fresh = dial("send_book_fresh_seconds")
    max_buys = int(dial("max_buys_per_station_day"))
    max_sends = int(dial("max_sends_per_station_day"))
    min_gap = dial("min_minutes_between_buys")
    walk_ticks = int(dial("walk_ticks"))
    live_book = cfg.get("live_ws_book")
    live_book = True if live_book is None else bool(live_book)
    manifest: dict[str, Any] = {}

    def manifest_root() -> Mapping[str, Any] | None:
        if "root" not in manifest:
            manifest["root"] = _ws_manifest_root(ws_manifest_path)
        return manifest["root"]

    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    now = _utc(now_utc)
    done = _evaluated_facts(
        target_conn,
        since_event_date=(now - timedelta(days=DEFAULT_LOOKBACK_DAYS)).date().isoformat(),
    )
    candidates = candidate_facts(
        target_conn, now_utc=now_utc,
        min_remaining_hours=dial("min_remaining_hours"),
        max_remaining_hours=dial("max_remaining_hours"),
        fresh_seconds=fresh_seconds,
    )
    evaluations: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    planned_by_day: dict[tuple[str, str], int] = {}
    registries: dict[tuple[str, str], Mapping[str, Any]] = {}
    for candidate in candidates:
        if candidate["fact_id"] in done:
            continue
        done.add(candidate["fact_id"])
        scope = (candidate["station_id"], candidate["event_date"])
        state: dict[str, Any] = {
            "snapshot": None, "book_age": None, "reading": {}, "book": {}, "context": {},
        }

        def refuse(reason: str, *, fired: bool = False) -> None:
            evaluations.append(_record(
                target_conn, candidate, snapshot=state["snapshot"], book_age=state["book_age"],
                reading=state["reading"], book=state["book"], context=state["context"],
                fired=fired, refusal=reason, order_intent_id=None, plan=None,
                now_utc=now_utc, rule_version=rule_version,
            ))

        if float(candidate["fact_age_seconds"]) > fresh_seconds:
            refuse(f"fact_stale:{candidate['fact_age_seconds']:.0f}s")
            continue
        envelope = _envelope(target_conn, candidate["envelope_id"])
        if not envelope or not envelope.get("probabilities") or not envelope.get("ordinals"):
            refuse("envelope_missing")
            continue
        snapshot = _latest_snapshot(target_conn, candidate["station_id"], candidate["event_date"])
        book_age = None if snapshot is None else (now - _utc(snapshot["observed_at"])).total_seconds()
        if live_book and (book_age is None or book_age > send_book_fresh):
            live = live_ws_snapshot(
                target_conn, manifest_root(),
                station_id=candidate["station_id"], event_date=candidate["event_date"],
                now_utc=now_utc,
            )
            if live is not None:
                live_age = (now - _utc(live["observed_at"])).total_seconds()
                if book_age is None or live_age < book_age:
                    snapshot, book_age = live, live_age
        state["snapshot"], state["book_age"] = snapshot, book_age
        if snapshot is None or book_age is None:
            refuse("snapshot_missing")
            continue
        if book_age > book_fresh:
            refuse(f"book_stale:{book_age:.0f}s")
            continue
        market = market_reference_v1(snapshot["buckets"])
        if market.get("state") != "complete":
            refuse(f"market_incomplete:{market.get('reason') or market.get('state')}")
            continue
        reading = evaluate_consensus(
            envelope["probabilities"], envelope["ordinals"], market["vector"],
        )
        state["reading"] = reading
        if reading.get("state") != "read":
            refuse(f"reading:{reading.get('state')}")
            continue
        bucket = str(reading["market_mode"])
        book_row = next(
            (r for r in snapshot["buckets"] if str(r.get("bucket")) == bucket), None
        )
        book = book_features(book_row)
        state["book"] = book
        held = held_buckets(target_conn, *scope)
        open_order = open_order_on_station_day(target_conn, *scope)
        state["context"] = {
            "held_buckets": ",".join(held) if held else None,
            "open_order_lane": (open_order or {}).get("lane"),
            "open_order_bucket": (open_order or {}).get("bucket"),
            "station_day_buys": station_day_buys(target_conn, *scope) + planned_by_day.get(scope, 0),
        }
        if not reading["four_same"]:
            refuse("rule_not_met:centres_split")
            continue
        ask = book.get("best_ask")
        if ask is None:
            refuse("rule_not_met:no_ask")
            continue
        if not (min_ask <= float(ask) < max_ask):
            refuse(f"rule_not_met:ask_outside_band:{float(ask):.3f}")
            continue
        # From here the rule has fired; what follows are the reasons a fired
        # decision may still not be bought.
        if book_age > send_book_fresh:
            refuse(f"send_book_stale:{book_age:.0f}s", fired=True)
            continue
        refusal = _planning_refusal(
            target_conn, candidate, gate=gate, now_utc=now_utc,
            max_buys=max_buys, max_sends=max_sends,
            planned_this_pass=planned_by_day.get(scope, 0),
            min_minutes_between_buys=min_gap,
        )
        if refusal:
            refuse(refusal, fired=True)
            continue
        if scope not in registries:
            registries[scope] = _registry_rows(
                target_conn, canonical_conn,
                station_id=candidate["station_id"], event_date=candidate["event_date"],
            )
        market_row = registries[scope].get(bucket)
        if market_row is None or not market_row.get("yes_token"):
            refuse("registry_token_missing", fired=True)
            continue
        if not bool(market_row.get("accepting_orders")):
            refuse("market_not_accepting_orders", fired=True)
            continue
        take = taker_take(
            book_row, stake,
            tick_size=market_row.get("tick_size"),
            min_shares=float(market_row.get("min_size") or DEFAULT_MIN_ORDER_SHARES),
            walk_ticks=walk_ticks,
        )
        if take["refusal"]:
            refuse(take["refusal"], fired=True)
            continue
        if float(take["limit_price"]) > max_paid + 1e-9:
            refuse(f"paid_price_above_cap:{float(take['limit_price']):.3f}>{max_paid:.2f}", fired=True)
            continue
        cash_refusal = _cash_refusal(target_conn, now_utc=now_utc, needed=float(take["cost_usdc"]))
        if cash_refusal:
            refuse(cash_refusal, fired=True)
            continue
        plan = {
            "schema": SCHEMA, "rule": rule_version, "rule_name": rule_name,
            "lane": LANE, "title": LANE_TITLE,
            "station_id": candidate["station_id"],
            "event_date": candidate["event_date"],
            "settlement_timezone": candidate["settlement_timezone"],
            "stage": "P1",
            "fact_id": candidate["fact_id"],
            "decision_at_utc": candidate["decision_at_utc"],
            "envelope_id": candidate["envelope_id"],
            "envelope_sha256": envelope.get("content_sha256"),
            "remaining_hours": candidate["remaining_hours"],
            "fact_age_seconds": candidate["fact_age_seconds"],
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_observed_at": snapshot["observed_at"],
            "book_source": snapshot.get("book_source") or "committed_snapshot",
            "book_age_seconds": round(float(book_age), 1),
            "bucket": bucket,
            "token_id": str(market_row["yes_token"]),
            "tick_size": take["tick_size"], "min_size": market_row.get("min_size"),
            "execution_candidate": EXECUTION_CANDIDATE,
            "order_type": "FOK", "post_only": False,
            "stake_usdc": stake, "shares": take["shares"],
            "limit_price": take["limit_price"], "fok_price_cap": take["limit_price"],
            "cost_usdc": take["cost_usdc"],
            "best_bid": take["best_bid"], "best_ask": take["best_ask"],
            "ask_depth_shares": take["ask_depth_shares"],
            "walked_depth_shares": take["walked_depth_shares"],
            "walk_ticks": take["walk_ticks"],
            "min_ask": min_ask, "max_ask": max_ask, "max_paid_price": max_paid,
            "centres": {
                k: reading.get(k) for k in (
                    "model_mode", "model_mean", "market_mode", "market_mean",
                    "model_mu", "market_mu", "model_share", "market_share",
                )
            },
            "held_buckets": held,
            "open_order": open_order,
            "station_day_buy_index": state["context"]["station_day_buys"] + 1,
        }
        plan["order_intent_id"] = f"{INTENT_PREFIX}{content_sha256({'plan': plan})[:32]}"
        evaluations.append(_record(
            target_conn, candidate, snapshot=snapshot, book_age=book_age,
            reading=reading, book=book, context=state["context"], fired=True,
            refusal=None, order_intent_id=plan["order_intent_id"], plan=plan,
            now_utc=now_utc, rule_version=rule_version,
        ))
        plans.append(plan)
        planned_by_day[scope] = planned_by_day.get(scope, 0) + 1
    return {
        "evaluations": evaluations,
        "plans": plans,
        "settings": cfg,
        "rule_version": rule_version,
        "candidates": len(candidates),
        "repairs": repairs,
    }


# ---------------------------------------------------------------------------
# Execution.


def execute_p1_consensus_entries(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    transport: Transport,
    now_utc: str,
    live_submit_authorized: bool,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
    ws_manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Judge, plan, send -- through the shared buy sender and the one ledger."""

    planned = plan_p1_consensus_entries(
        target_conn, canonical_conn, now_utc=now_utc,
        settings=settings, admission=admission, ws_manifest_path=ws_manifest_path,
    )
    receipts: list[dict[str, Any]] = []
    submitted = 0
    filled = 0
    for plan in planned["plans"]:
        intent_id = plan["order_intent_id"]
        if target_conn.execute(
            "SELECT 1 FROM basket_target_order_intents WHERE order_intent_id=?",
            (intent_id,),
        ).fetchone():
            receipts.append({"order_intent_id": intent_id, "state": "already_minted"})
            continue
        details = {
            "schema": "p1_consensus_lane_decision_v1", "lane": LANE, "title": LANE_TITLE,
            "rule": plan["rule"],
            "decision_signature": {"stage": "P1", "lane": LANE, "rule": plan["rule"]},
            "forward_evidence_binding": {
                "decision_at_utc": now_utc,
                "probability_decision_at_utc": plan["decision_at_utc"],
                "probability_artifact_sha256": plan["envelope_sha256"],
                "probability_stage": "P1",
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
            execution_candidate=EXECUTION_CANDIDATE,
            fok_price_cap=float(plan["fok_price_cap"]),
            tick_size=(float(plan["tick_size"]) if plan.get("tick_size") is not None else None),
            intent_body={
                "schema": SCHEMA, "rule": plan["rule"], "strategy_id": STRATEGY_ID,
                "order_type": plan["order_type"], "post_only": plan["post_only"],
                "plan": plan,
            },
            transport=transport, live_submit_authorized=live_submit_authorized,
            decision_details=details,
        )
        if entry.get("exchange_post_started"):
            submitted += 1
        matched = float(entry.get("matched_size") or 0.0)
        if matched > 0.0:
            filled += 1
        after = None if matched > 0.0 else (
            f"send:{entry.get('reason') or entry.get('state')}"
            if not entry.get("exchange_post_started")
            else f"send:{entry.get('event_type') or 'killed'}:{entry.get('reason') or ''}".rstrip(":")
        )
        target_conn.execute(
            f"UPDATE {EVALUATION_TABLE} SET refusal=? WHERE order_intent_id=?",
            (after, intent_id),
        )
        receipts.append(entry)
    return {
        "schema": "p1_consensus_lane_receipt_v1",
        "lane": LANE,
        "title": LANE_TITLE,
        "rule": planned["rule_version"],
        "settings": planned["settings"],
        "candidates": planned["candidates"],
        "evaluated": len(planned["evaluations"]),
        "fired": sum(1 for e in planned["evaluations"] if e["fired"]),
        "planned": len(planned["plans"]),
        "submitted": submitted,
        "filled": filled,
        "refusals": sorted({
            str(e["refusal"]) for e in planned["evaluations"] if e.get("refusal")
        }),
        "repairs": planned.get("repairs") or {},
        "receipts": receipts,
    }


__all__ = [
    "EVALUATION_TABLE",
    "EXECUTION_CANDIDATE",
    "INTENT_PREFIX",
    "LANE",
    "LANE_TITLE",
    "RULE_VERSION",
    "RULE_VERSIONS",
    "SCHEMA",
    "candidate_facts",
    "ensure_lane_schema",
    "evaluate_consensus",
    "execute_p1_consensus_entries",
    "last_buy_decision_at",
    "mean_bucket",
    "plan_p1_consensus_entries",
    "rule_version_for",
    "station_day_buys",
    "station_day_sends",
]
