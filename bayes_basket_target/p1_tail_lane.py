"""P1 尾段线: the market's own ladder times our observation likelihood.

Charter 166 (user ruling 2026-09-20: 「上线尾段车道 p1_tail 策略」).

What this lane believes is NOT P1.  P1 is ``root x mask x exp f`` and its root
loses to the market by 0.15 nat; the part that is ours alone is the likelihood
``exp f`` -- what one METAR says about the rest of the day.  So the belief here
is the market's own normalised mid ladder, masked by the hard observed bound,
multiplied by that same likelihood and renormalised:

    q = norm( norm(market_mid x 1[bucket alive]) x L )

and the lane buys a bucket at the ask when ``q - ask`` clears a hurdle.  It is
a speed trade: the report is public, the market will read it too, and the only
question is whether we act before it does.

Measured 2026-09-20 on 09-05..09-19 (9,436 reports, 648 station-days, 15 days),
buying at the ask ``k`` minutes after the report, hurdle 8c, once per
station-day-bucket, held to settlement:

    remaining 6-10h   t+2  +0.166/$   t+3 +0.139   t+5 +0.138   t+8 +0.077
                      per-share +0.096 [+0.038,+0.154], 13/15 days positive
    remaining >=10h   t+2  -0.033/$   t+8 -0.084        -- the lane refuses it
    remaining <6h     8 buys in 15 days                 -- the market is there

On the log score the same split: +0.028 nat [+0.010,+0.042] against the market
two minutes after the report in the 6-10h bin, and -0.012 against the market
fifteen minutes after -- the whole edge is gone by then.  Hence the two
windows this lane will not trade outside: the remaining-hours bin, and the age
of the envelope it acts on.

Stated plainly, because it decides how much this is worth: the 6-10h bin was
chosen after seeing the result (2026-09-19), and only 09-18 and 09-19 are days
no selection ever saw (43 buys, +0.252/$).  The bin is a hypothesis with a
mechanism -- the day's high is being decided in those hours, so one report
carries the most information it will ever carry -- not a fact with fifteen
independent days behind it.  The lane ships small and keeps every evaluation
so the question can be re-asked on days nobody chose.

One more honesty note: the study multiplied the market prior by the likelihood
the study itself fitted, at beta=1.  This lane multiplies by the SERVED
likelihood (``p1_observation_likelihood_json.likelihood_by_bucket``, the v17
direct log update, lambda already applied).  They are close but not the same
vector, so the numbers above are the shape of the edge, not a forecast of this
lane's own P&L.  Its own evaluations are the measurement that counts.

Conflicts with the tiebreak lane (charters 146/163), which is live on the same
station-days:

  1. Same bucket, never twice.  A bucket any lane already holds or has an
     order resting on is not bought again here.
  2. Never queue behind a resting order.  The tiebreak rung is a post-only
     GTD that sits on the book; this lane crosses.  A station-day with an
     order the venue has not answered for is skipped rather than raced --
     the edge is five minutes long and a queue is longer than that.  The
     refusal records which bucket was resting and whether this very report
     has just killed it, because a rule that CANCELS the stale rung first is
     the obvious next version and this is the count that would justify it.
  3. Separate books.  Own lane name, own evaluation table, own intent
     prefix, so the tiebreak lane's 09-25 review reads only its own trades.

Capital is shared, not separate: the same account-equity refusal
(charter 141) and the same station-day exposure the rest of the book obeys.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .basket_kernel.valuation_v1 import market_reference_v1
from .certain_win_recycle import _registry_rows
from .contracts import content_sha256
from .database import resolve_target_ws_manifest
from .exit_orders import Transport
from .lane_orders import send_lane_buy
from .live_station_scope import admission as live_station_admission
from .market_ingress import (
    _load_registry,
    _scope_quote_values,
    load_official_ws_quotes_payload,
)
from .resource_bounds import load_bounded_json
from .settlement_time import settlement_day_state
from .strategy_lanes import LANE_P1_TAIL, P1_TAIL_DEFAULT_SETTINGS, lane_settings
from .tiebreak_lane import (
    SEND_EGRESS_HOLD_REFUSAL,
    _cash_refusal,
    book_features,
    settle_stuck_lane_cycles,
)

LANE = LANE_P1_TAIL
LANE_TITLE = "P1 尾段线"
STRATEGY_ID = "bayes_basket_target_v1"
SCHEMA = "p1_tail_lane_plan_v1"
RULE_V1 = "market_prior_x_p1_likelihood_taker_v1"
RULE_VERSIONS = {RULE_V1: "p1_tail_lane_v1"}
DEFAULT_RULE = RULE_V1
#: Which clock the 6-10h bin is read on (2026-09-21, 「改」).  The edge was
#: measured on the hours left to the settlement clock; the receipt's
#: ``clock_hours`` is those hours plus the station's peak offset (charter
#: 157, up to 2.45 h either way), which the lane read until this change.
#: Replayed over 09-05..09-20 on books under five minutes old, the served
#: likelihood made +0.116/$ on remaining hours against +0.041 on the offset
#: clock, same execution; about a fifth of the reports fall in one bin and
#: not the other.  The axis is its own dial, not a rule name, so a running
#: resident that predates it ignores the key instead of refusing the rule.
HOURS_AXIS_REMAINING = "remaining_hours"
HOURS_AXIS_CLOCK = "clock_hours"
DEFAULT_HOURS_AXIS = HOURS_AXIS_REMAINING
AXIS_RULE_VERSIONS = {
    (RULE_V1, HOURS_AXIS_CLOCK): "p1_tail_lane_v1",
    (RULE_V1, HOURS_AXIS_REMAINING): "p1_tail_lane_v2",
}
RULE_VERSION = AXIS_RULE_VERSIONS[(DEFAULT_RULE, DEFAULT_HOURS_AXIS)]

#: The lane crosses: FOK at the ask, with the price cap the binding requires.
EXECUTION_CANDIDATE = "TAKER_FOK"
EVALUATION_TABLE = "p1_tail_lane_evaluation"
DEFAULT_LOOKBACK_DAYS = 2
DEFAULT_MIN_ORDER_SHARES = 5.0
SHARE_PRECISION = 2
#: The row a refusal that never reached a bucket is filed under.  A bucket
#: name can never be empty, so the two families cannot collide on the key.
NO_BUCKET = ""

Admission = Callable[[str, sqlite3.Connection], Mapping[str, Any]]

EVALUATION_DDL = (
    f"CREATE TABLE IF NOT EXISTS {EVALUATION_TABLE} ("
    " evaluation_id TEXT PRIMARY KEY,"
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " envelope_id TEXT NOT NULL,"
    " bucket TEXT NOT NULL,"
    " decision_at_utc TEXT NOT NULL,"
    " snapshot_id TEXT NOT NULL,"
    " snapshot_observed_at TEXT,"
    " rule_version TEXT NOT NULL,"
    " remaining_hours REAL,"
    " report_age_seconds REAL,"
    " market_mid REAL,"
    " market_ask REAL,"
    " likelihood REAL,"
    " belief REAL,"
    " gap REAL,"
    " best_gap REAL,"
    " fired INTEGER NOT NULL CHECK(fired IN (0,1)),"
    " refusal TEXT,"
    " order_intent_id TEXT,"
    " plan_json TEXT,"
    " evaluated_at_utc TEXT NOT NULL,"
    " content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),"
    " UNIQUE(station_id,event_date,envelope_id,bucket)"
    ")"
)
EVALUATION_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {EVALUATION_TABLE}_day_idx"
    f" ON {EVALUATION_TABLE}(event_date,fired,evaluated_at_utc)"
)

#: Conflict-with-tiebreak evidence, written on every row (rule 2 above).
#: ``tiebreak_open_bucket`` is the bucket an unanswered order of ANY lane is
#: resting on; ``tiebreak_open_bucket_dead`` says whether this very report
#: has since killed that bucket -- the count that decides whether a
#: cancel-then-take version is worth building.
FEATURE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("open_order_lane", "TEXT"),
    ("open_order_bucket", "TEXT"),
    ("open_order_bucket_dead", "INTEGER"),
    ("held_buckets", "TEXT"),
    # The receipt's own clock, beside ``remaining_hours``, on every row: the
    # two bin axes can then be compared on one table.  Rows written before
    # 2026-09-21 (rule version v1) carry the clock in ``remaining_hours``.
    ("clock_hours", "REAL"),
)


def ensure_lane_schema(conn: sqlite3.Connection) -> None:
    """Statement by statement, never ``executescript``: the resident calls
    this inside the writer's transaction."""

    conn.execute(EVALUATION_DDL)
    conn.execute(EVALUATION_INDEX_DDL)
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({EVALUATION_TABLE})")}
    for name, ctype in FEATURE_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE {EVALUATION_TABLE} ADD COLUMN {name} {ctype}")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def rule_version_for(rule: Any, hours_axis: Any = None) -> str:
    name = str(rule or DEFAULT_RULE)
    if name not in RULE_VERSIONS:
        raise ValueError(f"p1_tail_rule_unknown:{name}")
    axis = str(hours_axis or DEFAULT_HOURS_AXIS)
    if (name, axis) not in AXIS_RULE_VERSIONS:
        raise ValueError(f"p1_tail_hours_axis_unknown:{axis}")
    return AXIS_RULE_VERSIONS[(name, axis)]


# ---------------------------------------------------------------------------
# The reading.


def _norm(weights: Mapping[str, float]) -> dict[str, float] | None:
    total = sum(float(v) for v in weights.values())
    if not math.isfinite(total) or total <= 0.0:
        return None
    return {str(k): float(v) / total for k, v in weights.items()}


def evaluate_rule(
    market_vector: Mapping[str, Any],
    likelihood: Mapping[str, Any],
    excluded: Sequence[str],
    asks: Mapping[str, Any],
    *,
    hurdle: float,
) -> dict[str, Any]:
    """``q = norm(norm(mid x alive) x L)``, and the buckets whose gap clears.

    Returns the whole vector, not just the winners: a row that did not fire
    is the evidence that says the rule was asked and answered no, and the
    fit that re-asks this question later needs the same numbers the live
    lane saw.
    """

    dead = {str(b) for b in excluded}
    alive = {
        str(b): float(v)
        for b, v in market_vector.items()
        if str(b) not in dead and float(v or 0.0) > 0.0
    }
    if not alive:
        return {"state": "no_live_bucket", "fired": [], "belief": {}, "best_gap": None}
    missing = sorted(b for b in alive if b not in likelihood)
    if missing:
        return {
            "state": "likelihood_incomplete",
            "missing": missing,
            "fired": [],
            "belief": {},
            "best_gap": None,
        }
    prior = _norm(alive)
    if prior is None:
        return {"state": "market_prior_empty", "fired": [], "belief": {}, "best_gap": None}
    posterior = _norm({
        b: prior[b] * max(float(likelihood[b]), 0.0) for b in prior
    })
    if posterior is None:
        return {"state": "belief_empty", "fired": [], "belief": {}, "best_gap": None}
    gaps: dict[str, float] = {}
    for bucket, belief in posterior.items():
        ask = asks.get(bucket)
        if ask is None:
            continue
        try:
            ask_value = float(ask)
        except (TypeError, ValueError):
            continue
        if not (0.0 < ask_value < 1.0):
            continue
        gaps[bucket] = round(belief - ask_value, 6)
    best_gap = max(gaps.values()) if gaps else None
    fired = sorted(
        (b for b, g in gaps.items() if g >= float(hurdle)),
        key=lambda b: (-gaps[b], b),
    )
    return {
        "state": "read",
        "prior": {b: round(v, 6) for b, v in prior.items()},
        "belief": {b: round(v, 6) for b, v in posterior.items()},
        "gaps": gaps,
        "best_gap": best_gap,
        "hurdle": float(hurdle),
        "fired": fired,
        "excluded": sorted(dead),
    }


def taker_take(
    book_row: Mapping[str, Any] | None,
    stake_usdc: float,
    *,
    tick_size: Any,
    min_shares: float,
    walk_ticks: int = 0,
) -> dict[str, Any]:
    """What ``stake_usdc`` buys, and the worst price it is authorised to pay.

    The limit was the best ask and nothing above it, on the reasoning that a
    FOK priced higher authorises paying the second level and the lane's claim
    is about the price it measured.  On this book that reasoning does not
    survive its own numbers: over 283 ask levels sampled 2026-09-20T19:00Z+,
    the top level's median depth is 10 shares and its p25 is 5.7, so a 5 USDC
    take needs more than the whole top level most of the time -- 35% of the
    time at 0.72, 61% at 0.50, 75% at 0.30.  Three live sends, three kills,
    the last of them with the book 19 seconds old and 1.68 shares of headroom
    left on a 7.68-share level (KLGA 2026-09-20 68-69F).

    So the limit may walk ``walk_ticks`` above the best ask and the size is
    the depth up to it.  The caller pays for that by testing its hurdle
    against the WALKED price, not the best ask: what the lane authorises and
    what it claims an edge over are then the same number.  One tick carried
    every sampled level at 0.72 and 91% at 0.50.
    """

    out: dict[str, Any] = {
        "limit_price": None, "shares": None, "cost_usdc": None,
        "best_ask": None, "best_bid": None, "ask_depth_shares": None,
        "walked_depth_shares": None, "walk_ticks": int(walk_ticks),
        "tick_size": (float(tick_size) if tick_size not in (None, "") else None),
        "refusal": None,
    }
    book = book_features(book_row)
    out["best_ask"] = book["best_ask"]
    out["best_bid"] = book["best_bid"]
    out["ask_depth_shares"] = book["ask_depth_shares"]
    if book["best_ask"] is None:
        out["refusal"] = "no_ask"
        return out
    best = float(book["best_ask"])
    tick = out["tick_size"]
    price = best
    if int(walk_ticks) > 0 and tick:
        price = round(best + int(walk_ticks) * float(tick), 6)
        if price >= 1.0:
            price = best
    out["limit_price_authorised"] = price
    affordable = float(stake_usdc) / price
    # Every level at or under the authorised limit is fillable, so the size
    # is measured against all of them, not the top one alone.
    depth = book["ask_depth_shares"]
    if price > best + 1e-9:
        levels = []
        for level in (book_row or {}).get("ask_levels") or ():
            try:
                lp = float(level.get("price")); ls = float(level.get("shares") or 0.0)
            except (TypeError, ValueError, AttributeError):
                continue
            if ls > 0 and lp <= price + 1e-9:
                levels.append(ls)
        if levels:
            depth = sum(levels)
    out["walked_depth_shares"] = depth
    if depth is not None:
        # A FOK the top level cannot fill is a FOK the venue kills, and the
        # book we price on is always a little older than the send.  KMIA
        # 2026-09-20 88-89F: the snapshot showed exactly 8 shares at 0.59,
        # the plan asked for all 8, and by the time it arrived -- the book
        # was already 171 seconds old -- someone had taken part of it, so
        # the whole order died rather than part of it filling.  Leave room
        # for that: whichever of the fraction and the whole share is
        # smaller, so a thin level gives up a share and a deep one gives up
        # a fifth.
        usable = min(
            float(depth) * DEPTH_HEADROOM_FRACTION, float(depth) - 1.0
        )
        affordable = min(affordable, max(usable, 0.0))
    shares = _sendable_shares(price, affordable)
    if shares is None or shares < float(min_shares):
        thin = depth is not None and float(depth) < float(stake_usdc) / price
        out["refusal"] = (
            f"thin_ask:{float(depth):.2f}" if thin
            else f"below_min_size:{(shares or 0.0):.2f}<{float(min_shares):.2f}"
        )
        return out
    out["limit_price"] = price
    out["shares"] = shares
    out["cost_usdc"] = round(price * shares, 6)
    return out


#: What the venue will accept on a market BUY, learned the way these things
#: are learned -- from a refusal.  SAEZ 2026-09-20 25C, 2026-09-21T17:45Z:
#:
#:   send:invalid amounts, the market buy orders maker amount supports a max
#:   accuracy of 2 decimals, taker amount a max of 4 decimals
#:
#: The maker amount of a BUY is the USDC paid (price x shares) and the taker
#: amount is the shares received.  Flooring shares to two decimals -- which is
#: what every other lane does, and is right for a resting MAKER order whose
#: amounts the venue derives differently -- gave 6.94 shares at 0.72, a cost
#: of 4.9968, and four decimals where two are allowed.
SEND_COST_DECIMALS = 2
SEND_SHARE_DECIMALS = 4
#: How much of the top level a take may ask for.  See ``taker_take``.
DEPTH_HEADROOM_FRACTION = 0.8


def _sendable_shares(price: float, affordable: float) -> float | None:
    """The largest share count at ``price`` the venue will take.

    Both amounts have to land on the venue's grid at once: the cost on two
    decimals, the shares on four.  Whole shares satisfy the cost side for
    any whole-cent price, which is every price this book quotes, so the
    search starts there and only walks down if a finer tick ever makes the
    first candidate illegal.  Returns None when nothing at or below
    ``affordable`` is legal.
    """

    def legal(candidate: float) -> bool:
        if candidate <= 0.0:
            return False
        cost = candidate * price
        return (
            abs(cost * 10 ** SEND_COST_DECIMALS
                - round(cost * 10 ** SEND_COST_DECIMALS)) < 1e-6
            and abs(candidate * 10 ** SEND_SHARE_DECIMALS
                    - round(candidate * 10 ** SEND_SHARE_DECIMALS)) < 1e-6
        )

    whole = float(math.floor(affordable + 1e-9))
    for step in range(0, 100):
        candidate = whole - step
        if candidate <= 0.0:
            break
        if legal(candidate):
            return candidate
    # A tick finer than a cent: walk the share grid down instead.
    grid = 10 ** SEND_SHARE_DECIMALS
    start = math.floor(affordable * grid + 1e-6)
    for offset in range(0, 10000):
        candidate = (start - offset) / grid
        if candidate <= 0.0:
            break
        if legal(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Reading the store.


def tail_envelopes(
    conn: sqlite3.Connection,
    *,
    now_utc: str,
    min_remaining_hours: float,
    max_remaining_hours: float,
    fresh_seconds: float,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    hours_axis: str = DEFAULT_HOURS_AXIS,
) -> list[dict[str, Any]]:
    """Every committed P1 envelope young enough to act on, newest per scope.

    The bin is read off the envelope's own likelihood receipt, never
    recomputed here: ``features.remaining_hours`` (hours to this station's
    settlement clock when the report was folded in, the axis the edge was
    measured on) or, on the ``clock_hours`` axis, the receipt's clock --
    those hours plus the station's peak offset (charter 157).  A receipt
    without the axis it is asked for is not a candidate.
    """

    now = _utc(now_utc)
    floor_day = (now - timedelta(days=int(lookback_days))).date().isoformat()
    rows = conn.execute(
        "SELECT envelope_id, station_id, event_date, settlement_timezone,"
        " decision_at_utc, lineage_json, content_sha256, fresh_until_utc"
        " FROM basket_probability_envelope"
        " WHERE stage='P1' AND state='COMMITTED' AND event_date>=?"
        " AND decision_at_utc>=?"
        " ORDER BY station_id, event_date, decision_at_utc DESC",
        (floor_day, (now - timedelta(seconds=float(fresh_seconds) * 4)).strftime("%Y-%m-%dT%H:%M:%S.000Z")),
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        scope = (str(row[1]), str(row[2]))
        if scope in seen:
            continue
        seen.add(scope)
        try:
            lineage = json.loads(row[5] or "{}") or {}
            receipt = json.loads(lineage.get("p1_observation_likelihood_json") or "{}") or {}
        except ValueError:
            continue
        if str(receipt.get("state") or "").upper() != "APPLIED":
            continue

        def _hours(value: Any) -> float | None:
            try:
                out = float(value)
            except (TypeError, ValueError):
                return None
            return out if math.isfinite(out) else None

        clock = _hours(receipt.get("clock_hours"))
        remaining = _hours((receipt.get("features") or {}).get("remaining_hours"))
        hours = clock if hours_axis == HOURS_AXIS_CLOCK else remaining
        if hours is None:
            continue
        if not (float(min_remaining_hours) <= hours < float(max_remaining_hours)):
            continue
        age = (now - _utc(str(row[4]))).total_seconds()
        excluded = [
            part.strip()
            for part in str(lineage.get("hard_excluded_buckets") or "").split(",")
            if part.strip()
        ]
        out.append({
            "envelope_id": str(row[0]),
            "station_id": str(row[1]),
            "event_date": str(row[2]),
            "settlement_timezone": str(row[3]),
            "decision_at_utc": str(row[4]),
            "envelope_sha256": str(row[6]),
            "fresh_until_utc": str(row[7]) if row[7] else None,
            "remaining_hours": None if remaining is None else round(remaining, 4),
            "clock_hours": None if clock is None else round(clock, 4),
            "hours_axis": hours_axis,
            "report_age_seconds": round(age, 1),
            "likelihood": {
                str(k): float(v)
                for k, v in (receipt.get("likelihood_by_bucket") or {}).items()
            },
            "excluded": excluded,
            "running_high_c": receipt.get("running_high_c"),
            "rise_probability": receipt.get("rise_probability"),
            "likelihood_rule": receipt.get("rule"),
        })
    return out


def _latest_snapshot(
    conn: sqlite3.Connection, station_id: str, event_date: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT snapshot_id, buckets_json, observed_at FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " ORDER BY sequence DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if row is None:
        return None
    try:
        buckets = json.loads(row[1] or "[]") or []
    except ValueError:
        return None
    return {
        "snapshot_id": str(row[0]),
        "buckets": buckets,
        "observed_at": str(row[2]),
    }


#: A ladder read off the publisher's manifest rather than the snapshot table.
#: The prefix is how an evaluation row and a decision's evidence binding say
#: which of the two the lane priced on -- there is no snapshot row behind it.
LIVE_BOOK_PREFIX = "wslive_"


def _ws_manifest_root(path: Path | str | None) -> Mapping[str, Any] | None:
    """The WS publisher's multi-scope manifest, or None when unreadable.

    The same target-owned file production's market ingress reads (never the
    protected resident manifest -- ``resolve_target_ws_manifest`` refuses
    it), resolved the way the worker resolves it.  Only the nested scope the
    lane asks for is verified; see ``live_ws_snapshot``.
    """

    try:
        resolved = resolve_target_ws_manifest(
            path or os.environ.get("PM_TARGET_MARKET_MANIFEST") or None
        )
        root = load_bounded_json(resolved)
    except Exception:  # noqa: BLE001 -- no manifest is no live book, not a failure
        return None
    if not isinstance(root, Mapping) or root.get("state") != "connected":
        return None
    return root


def live_ws_snapshot(
    target_conn: sqlite3.Connection,
    root: Mapping[str, Any] | None,
    *,
    station_id: str,
    event_date: str,
    now_utc: str,
) -> dict[str, Any] | None:
    """This station-day's ladder as the WS publisher holds it now, or None.

    The committed snapshot is written in batches, once per cycle for a
    station: measured 2026-09-20T18:12Z..09-21T07:40Z over 183 evaluations,
    the snapshot the lane read was a median 331 seconds old and 9 of 183
    (5%) were inside the 60-second send bound -- so 48 of the 50 envelopes
    that cleared the hurdle were refused ``send_book_stale`` and the lane sent
    nothing after its last fix.  The publisher's manifest carries every
    scope's books as the stream delivers them (median 27 s, max 34 s at
    07:49Z the same day).

    Built with the ingress's own two functions -- the nested manifest's
    verification and ``_scope_quote_values`` over the started-day registry
    rows -- so the rows are the rows a committed snapshot would have stored,
    depth fold included; ``observed_at`` is the OLDEST bucket's book time,
    as in the snapshot.  Any refusal on the way (scope absent, hash, a
    token missing, crossed book) is None: the caller falls back to the
    committed snapshot and its own bounds.
    """

    if not isinstance(root, Mapping):
        return None
    key = f"{station_id}|{event_date}"
    nested = (root.get("scope_manifests") or {}).get(key)
    if (
        not isinstance(nested, Mapping)
        or str(nested.get("scope_key") or "") != key
        or str(nested.get("target_local_date") or "") != event_date
    ):
        return None
    try:
        verified, quotes = load_official_ws_quotes_payload(nested)
        rows = _load_registry(
            target_conn, station_id=station_id, event_date=event_date,
            as_of_utc=now_utc,
        ).get(key)
        if not rows:
            return None
        buckets, observed_at = _scope_quote_values(rows, quotes, verified)
    except Exception:  # noqa: BLE001 -- see the docstring
        return None
    return {
        "snapshot_id": LIVE_BOOK_PREFIX + content_sha256({
            "schema": "p1_tail_live_ws_book_v1",
            "station_id": station_id,
            "event_date": event_date,
            "buckets": buckets,
        })[:40],
        "buckets": buckets,
        "observed_at": observed_at,
        "book_source": "ws_publisher_manifest",
    }


def _evaluated_buckets(
    conn: sqlite3.Connection, *, since_event_date: str
) -> set[tuple[str, str, str]]:
    """(station, day, envelope) already scored -- one pass per envelope."""

    return {
        (str(r[0]), str(r[1]), str(r[2]))
        for r in conn.execute(
            f"SELECT station_id, event_date, envelope_id FROM {EVALUATION_TABLE}"
            " WHERE event_date >= ?",
            (since_event_date,),
        )
    }


def station_day_bucket_attempted(
    conn: sqlite3.Connection, station_id: str, event_date: str, bucket: str
) -> bool:
    """Charter 166 rule 1: this lane buys a bucket once, ever."""

    # Charter 197 amendment: a take the egress hold kept in never left, so
    # it is not the bucket's one take.
    return conn.execute(
        f"SELECT 1 FROM {EVALUATION_TABLE}"
        " WHERE station_id=? AND event_date=? AND bucket=? AND order_intent_id IS NOT NULL"
        " AND COALESCE(refusal, '')<>?"
        " LIMIT 1",
        (station_id, event_date, bucket, SEND_EGRESS_HOLD_REFUSAL),
    ).fetchone() is not None


def station_day_sends(
    conn: sqlite3.Connection, station_id: str, event_date: str
) -> int:
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {EVALUATION_TABLE}"
        " WHERE station_id=? AND event_date=? AND order_intent_id IS NOT NULL"
        " AND COALESCE(refusal, '')<>?",
        (station_id, event_date, SEND_EGRESS_HOLD_REFUSAL),
    ).fetchone()[0] or 0)


def open_order_on_station_day(
    conn: sqlite3.Connection, station_id: str, event_date: str
) -> dict[str, Any] | None:
    """Charter 166 rule 2: an order of ANY lane the venue has not answered for.

    Not just this lane's: the tiebreak rung rests as a post-only GTD, and a
    take sent while it sits is a take queued behind our own order.
    """

    # Through the decision row, which is the only place a station-day is
    # written on an intent: the side lanes spell their decision id
    # ``{lane}_decision_{station}_{date}`` but the basket compile's is an
    # opaque hash, so reading the id would miss exactly the lane whose
    # return this rule would matter most for.  Both sides of the join are
    # indexed (scope index, then the intent's decision index).
    #
    # BUYs only.  A resting recycle SELL at 0.99 neither freezes the
    # station-day's quotes nor shuts its token to the next order (charter
    # 100), and the mis-bought exit is a sell too.
    # The lane is read off the intent, not the decision row: that table's
    # ``strategy_lane`` is fenced to 'basket' by the hard-NO trigger and says
    # nothing about which of today's lanes minted the order.
    row = conn.execute(
        "SELECT i.order_intent_id, i.bucket, i.state,"
        "       json_extract(i.intent_json,'$.lane')"
        " FROM basket_target_order_intents AS i"
        " JOIN auto_trade_decisions AS d ON d.decision_id=i.decision_id"
        " WHERE d.station_id=? AND d.event_date=? AND i.order_action='BUY'"
        "   AND i.state NOT IN ('filled','failed','cancelled')"
        " ORDER BY i.created_at DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if row is None:
        return None
    return {
        "order_intent_id": str(row[0]),
        "bucket": str(row[1] or ""),
        "state": str(row[2] or ""),
        "lane": str(row[3] or ""),
    }


def held_buckets(
    conn: sqlite3.Connection, station_id: str, event_date: str
) -> list[str]:
    """Buckets this station-day already has shares in, any lane.

    The confirmed inventory snapshot is the book's own count of what is
    held -- the same one the recycle lane sells off -- rather than a sum
    over this lane's fills, which would miss every share the other lanes
    bought.
    """

    row = conn.execute(
        "SELECT inventory_json FROM basket_confirmed_inventory_snapshot"
        " WHERE station_id=? AND event_date=? AND complete=1"
        " ORDER BY observed_at DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if row is None:
        return []
    try:
        inventory = json.loads(row[0] or "{}") or {}
    except ValueError:
        return []
    filled = inventory.get("filled_by_bucket") or inventory.get("filled_shares_by_bucket") or {}
    out = []
    for bucket, shares in filled.items():
        try:
            if float(shares or 0.0) > 1e-9:
                out.append(str(bucket))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def _record(
    conn: sqlite3.Connection,
    candidate: Mapping[str, Any],
    *,
    bucket: str,
    snapshot: Mapping[str, Any] | None,
    reading: Mapping[str, Any],
    features: Mapping[str, Any],
    fired: bool,
    refusal: str | None,
    order_intent_id: str | None,
    plan: Mapping[str, Any] | None,
    now_utc: str,
    rule_version: str = RULE_VERSION,
) -> dict[str, Any]:
    gaps = dict(reading.get("gaps") or {})
    belief = dict(reading.get("belief") or {})
    prior = dict(reading.get("prior") or {})
    body = {
        "station_id": candidate["station_id"],
        "event_date": candidate["event_date"],
        "envelope_id": candidate["envelope_id"],
        "bucket": bucket,
        "decision_at_utc": candidate["decision_at_utc"],
        "snapshot_id": (snapshot or {}).get("snapshot_id", ""),
        "rule_version": rule_version,
        "reading": dict(reading),
        "fired": bool(fired),
        "refusal": refusal,
        "order_intent_id": order_intent_id,
        "plan": dict(plan) if plan else None,
    }
    sha = content_sha256(body)
    names = [name for name, _t in FEATURE_COLUMNS]
    conn.execute(
        f"INSERT OR IGNORE INTO {EVALUATION_TABLE}(evaluation_id,station_id,event_date,"
        "envelope_id,bucket,decision_at_utc,snapshot_id,snapshot_observed_at,rule_version,"
        "remaining_hours,report_age_seconds,market_mid,market_ask,likelihood,belief,gap,"
        "best_gap,fired,refusal,order_intent_id,plan_json,evaluated_at_utc,content_sha256,"
        + ",".join(names) + ")"
        " VALUES(" + ",".join("?" for _ in range(23 + len(names))) + ")",
        (
            f"p1tail_eval_{sha[:32]}",
            body["station_id"], body["event_date"], body["envelope_id"], bucket,
            body["decision_at_utc"], body["snapshot_id"],
            (snapshot or {}).get("observed_at"),
            rule_version,
            candidate.get("remaining_hours"), candidate.get("report_age_seconds"),
            prior.get(bucket), (reading.get("asks") or {}).get(bucket),
            (candidate.get("likelihood") or {}).get(bucket),
            belief.get(bucket), gaps.get(bucket), reading.get("best_gap"),
            1 if fired else 0, refusal, order_intent_id,
            json.dumps(plan, ensure_ascii=False, sort_keys=True) if plan else None,
            now_utc, sha,
            *[features.get(name) for name in names],
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
    max_sends: int,
) -> str | None:
    station = candidate["station_id"]
    day = candidate["event_date"]
    if station_day_sends(conn, station, day) >= int(max_sends):
        return f"station_day_send_cap:{int(max_sends)}"
    try:
        verdict = gate(station, conn)
    except Exception as exc:  # noqa: BLE001 -- an unreadable gate admits nothing
        return f"station_admission_unreadable:{type(exc).__name__}"
    if verdict.get("admitted") is not True:
        return f"station_not_admitted:{verdict.get('reason') or 'refused'}"
    try:
        day_state = settlement_day_state(
            day, as_of_utc=now_utc,
            settlement_timezone=candidate["settlement_timezone"],
        )
    except Exception as exc:  # noqa: BLE001
        return f"settlement_day_unknown:{type(exc).__name__}"
    if day_state.get("state") == "closed":
        return "settlement_day_closed"
    return None


def plan_p1_tail_entries(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection | None = None,
    *,
    now_utc: str,
    settings: Mapping[str, Any] | None = None,
    admission: Admission | None = None,
    ws_manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Score every unscored tail envelope; plan a take for the buckets that fire."""

    ensure_lane_schema(target_conn)
    repairs = {"cycles_settled": settle_stuck_lane_cycles(target_conn)}
    cfg = {**P1_TAIL_DEFAULT_SETTINGS, **dict(settings or lane_settings(LANE))}
    rule_name = str(cfg.get("rule") or DEFAULT_RULE)
    hours_axis = str(cfg.get("hours_axis") or DEFAULT_HOURS_AXIS)
    rule_version = rule_version_for(rule_name, hours_axis)

    def dial(name: str) -> float:
        """A dial the operator set, falling back only when it is ABSENT.

        ``or`` would read a deliberate zero -- a closed cap, a hurdle of
        nothing -- as "unset" and serve the default instead, which is the
        ordinal-zero falsy trap this book has been caught by before.
        """

        value = cfg.get(name)
        if value is None:
            value = P1_TAIL_DEFAULT_SETTINGS[name]
        return float(value)

    stake = dial("stake_usdc")
    hurdle = dial("hurdle")
    fresh_seconds = dial("envelope_fresh_seconds")
    book_fresh = dial("book_fresh_seconds")
    send_book_fresh = dial("send_book_fresh_seconds")
    max_sends = int(dial("max_sends_per_station_day"))
    walk_ticks = int(dial("walk_ticks"))
    # Absent reads as on; only an explicit false turns the live book off.
    live_book = cfg.get("live_ws_book")
    live_book = True if live_book is None else bool(live_book)
    manifest: dict[str, Any] = {}

    def manifest_root() -> Mapping[str, Any] | None:
        """Read once per pass, and only when some envelope needs it."""

        if "root" not in manifest:
            manifest["root"] = _ws_manifest_root(ws_manifest_path)
        return manifest["root"]

    gate = admission or (lambda station, conn: live_station_admission(station, conn=conn))
    now = _utc(now_utc)
    done = _evaluated_buckets(
        target_conn,
        since_event_date=(now - timedelta(days=DEFAULT_LOOKBACK_DAYS)).date().isoformat(),
    )
    evaluations: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    candidates = tail_envelopes(
        target_conn, now_utc=now_utc,
        min_remaining_hours=dial("min_remaining_hours"),
        max_remaining_hours=dial("max_remaining_hours"),
        fresh_seconds=fresh_seconds,
        hours_axis=hours_axis,
    )
    for candidate in candidates:
        key = (candidate["station_id"], candidate["event_date"], candidate["envelope_id"])
        if key in done:
            continue
        empty = {"state": "unread", "fired": [], "belief": {}, "best_gap": None}
        no_features = {
            **{name: None for name, _t in FEATURE_COLUMNS},
            "clock_hours": candidate.get("clock_hours"),
        }

        def refuse(reason: str, reading: Mapping[str, Any] = empty,
                   snapshot: Mapping[str, Any] | None = None,
                   features: Mapping[str, Any] = no_features) -> None:
            evaluations.append(_record(
                target_conn, candidate, bucket=NO_BUCKET, snapshot=snapshot,
                reading=reading, features=features, fired=False, refusal=reason,
                order_intent_id=None, plan=None, now_utc=now_utc,
                rule_version=rule_version,
            ))

        # The edge is five minutes long: an envelope older than the window is
        # not a slower trade, it is a different trade.
        if float(candidate["report_age_seconds"]) > fresh_seconds:
            refuse(f"envelope_stale:{candidate['report_age_seconds']:.0f}s")
            continue
        snapshot = _latest_snapshot(
            target_conn, candidate["station_id"], candidate["event_date"]
        )
        book_age = (
            None if snapshot is None
            else (now - _utc(snapshot["observed_at"])).total_seconds()
        )
        # The committed snapshot is a cycle's batch, a median 331 seconds
        # old when the lane reads it; the publisher holds the same ladder
        # seconds old.  Score AND price on the newer one, so the belief's
        # prior and the ask it is tested against are the same book -- which
        # is also how the edge was measured (the market at the report plus
        # k minutes, bought at that market's ask).
        if live_book and (book_age is None or book_age > send_book_fresh):
            live = live_ws_snapshot(
                target_conn, manifest_root(),
                station_id=candidate["station_id"],
                event_date=candidate["event_date"], now_utc=now_utc,
            )
            if live is not None:
                live_age = (now - _utc(live["observed_at"])).total_seconds()
                if book_age is None or live_age < book_age:
                    snapshot, book_age = live, live_age
        if snapshot is None or book_age is None:
            refuse("snapshot_missing")
            continue
        if book_age > book_fresh:
            refuse(f"book_stale:{book_age:.0f}s", snapshot=snapshot)
            continue
        # Reading a three-minute-old book is fine; SENDING against one is
        # not.  ``book_fresh_seconds`` is the bound on what may still be
        # scored -- the evaluation row is the evidence a later fit reads --
        # and this is the much shorter bound on what may be bought.  Both
        # sends so far were priced on a book 171 and 328 seconds old while
        # the envelope was 24 and 35: the belief was current and the ladder
        # was not.
        send_book_stale = book_age > send_book_fresh
        market = market_reference_v1(snapshot["buckets"])
        if market.get("state") != "complete":
            refuse(
                f"market_incomplete:{market.get('reason') or market.get('state')}",
                snapshot=snapshot,
            )
            continue
        asks = {
            str(row.get("bucket")): book_features(row)["best_ask"]
            for row in snapshot["buckets"]
        }
        reading = {
            **evaluate_rule(
                market["vector"], candidate["likelihood"], candidate["excluded"],
                asks, hurdle=hurdle,
            ),
            "asks": {b: a for b, a in asks.items() if a is not None},
        }
        open_order = open_order_on_station_day(
            target_conn, candidate["station_id"], candidate["event_date"]
        )
        held = held_buckets(target_conn, candidate["station_id"], candidate["event_date"])
        features = {
            "open_order_lane": (open_order or {}).get("lane"),
            "open_order_bucket": (open_order or {}).get("bucket"),
            "open_order_bucket_dead": (
                None if open_order is None
                else int(str(open_order.get("bucket")) in set(candidate["excluded"]))
            ),
            "held_buckets": ",".join(held) if held else None,
            "clock_hours": candidate.get("clock_hours"),
        }
        if reading.get("state") != "read":
            refuse(f"reading:{reading.get('state')}", reading, snapshot, features)
            continue
        if not reading["fired"]:
            refuse("rule_not_met:no_bucket_over_hurdle", reading, snapshot, features)
            continue
        # Charter 166 rule 2: never queue behind a resting order of our own.
        if open_order is not None:
            refuse(
                f"open_order:{open_order['lane'] or 'unknown'}:{open_order['state']}",
                reading, snapshot, features,
            )
            continue
        if send_book_stale:
            refuse(
                f"send_book_stale:{book_age:.0f}s", reading, snapshot, features
            )
            continue
        refusal = _planning_refusal(
            target_conn, candidate, gate=gate, now_utc=now_utc, max_sends=max_sends
        )
        if refusal:
            refuse(refusal, reading, snapshot, features)
            continue
        registry = _registry_rows(
            target_conn, canonical_conn,
            station_id=candidate["station_id"], event_date=candidate["event_date"],
        )
        planned_this_pass = 0
        for bucket in reading["fired"]:
            if planned_this_pass + station_day_sends(
                target_conn, candidate["station_id"], candidate["event_date"]
            ) >= max_sends:
                break

            def bucket_refuse(reason: str) -> None:
                evaluations.append(_record(
                    target_conn, candidate, bucket=bucket, snapshot=snapshot,
                    reading=reading, features=features, fired=True, refusal=reason,
                    order_intent_id=None, plan=None, now_utc=now_utc,
                    rule_version=rule_version,
                ))

            # Charter 166 rule 1: one bucket, one take, ever.
            if station_day_bucket_attempted(
                target_conn, candidate["station_id"], candidate["event_date"], bucket
            ):
                bucket_refuse("bucket_already_attempted")
                continue
            if bucket in set(held):
                bucket_refuse("bucket_already_held")
                continue
            market_row = registry.get(bucket)
            if market_row is None or not market_row.get("yes_token"):
                bucket_refuse("registry_token_missing")
                continue
            if not bool(market_row.get("accepting_orders")):
                bucket_refuse("market_not_accepting_orders")
                continue
            book_row = next(
                (r for r in snapshot["buckets"] if str(r.get("bucket")) == bucket), None
            )
            take = taker_take(
                book_row, stake,
                tick_size=market_row.get("tick_size"),
                min_shares=float(market_row.get("min_size") or DEFAULT_MIN_ORDER_SHARES),
                walk_ticks=walk_ticks,
            )
            if take["refusal"]:
                bucket_refuse(take["refusal"])
                continue
            # The hurdle is tested against the price this order AUTHORISES,
            # not the one it was read at.  The reading stays on the ask so
            # the row is comparable to the measurement; the send has to clear
            # the worst price it may pay, which is the stricter test.
            walked_gap = round(
                float((reading.get("belief") or {}).get(bucket, 0.0))
                - float(take["limit_price"]), 6
            )
            if walked_gap < hurdle:
                bucket_refuse(f"hurdle_after_walk:{walked_gap:+.3f}<{hurdle:.2f}")
                continue
            cash_refusal = _cash_refusal(
                target_conn, now_utc=now_utc, needed=float(take["cost_usdc"])
            )
            if cash_refusal:
                bucket_refuse(cash_refusal)
                continue
            plan = {
                "schema": SCHEMA, "rule": rule_version, "rule_name": rule_name,
                "lane": LANE, "title": LANE_TITLE,
                "station_id": candidate["station_id"],
                "event_date": candidate["event_date"],
                "settlement_timezone": candidate["settlement_timezone"],
                "stage": "P1",
                "envelope_id": candidate["envelope_id"],
                "envelope_sha256": candidate["envelope_sha256"],
                "envelope_decision_at_utc": candidate["decision_at_utc"],
                "remaining_hours": candidate["remaining_hours"],
                "clock_hours": candidate.get("clock_hours"),
                "hours_axis": hours_axis,
                "report_age_seconds": candidate["report_age_seconds"],
                "running_high_c": candidate.get("running_high_c"),
                "rise_probability": candidate.get("rise_probability"),
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
                "walk_ticks": take["walk_ticks"], "walked_gap": walked_gap,
                "market_mid": (reading.get("prior") or {}).get(bucket),
                "likelihood": candidate["likelihood"].get(bucket),
                "belief": (reading.get("belief") or {}).get(bucket),
                "gap": (reading.get("gaps") or {}).get(bucket),
                "hurdle": hurdle,
                "excluded_buckets": candidate["excluded"],
            }
            plan["order_intent_id"] = f"p1tail_{content_sha256({'plan': plan})[:32]}"
            evaluations.append(_record(
                target_conn, candidate, bucket=bucket, snapshot=snapshot,
                reading=reading, features=features, fired=True, refusal=None,
                order_intent_id=plan["order_intent_id"], plan=plan,
                now_utc=now_utc, rule_version=rule_version,
            ))
            plans.append(plan)
            planned_this_pass += 1
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


def execute_p1_tail_entries(
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
    """Score, plan, send -- through the shared buy sender and the one ledger."""

    planned = plan_p1_tail_entries(
        target_conn, canonical_conn, now_utc=now_utc,
        settings=settings, admission=admission,
        ws_manifest_path=ws_manifest_path,
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
            "schema": "p1_tail_lane_decision_v1", "lane": LANE, "title": LANE_TITLE,
            "rule": plan["rule"],
            "decision_signature": {"stage": "P1", "lane": LANE, "rule": plan["rule"]},
            "forward_evidence_binding": {
                "decision_at_utc": now_utc,
                "probability_decision_at_utc": plan["envelope_decision_at_utc"],
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
        "schema": "p1_tail_lane_receipt_v1",
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
    "AXIS_RULE_VERSIONS",
    "DEFAULT_HOURS_AXIS",
    "EXECUTION_CANDIDATE",
    "HOURS_AXIS_CLOCK",
    "HOURS_AXIS_REMAINING",
    "LANE",
    "LIVE_BOOK_PREFIX",
    "LANE_TITLE",
    "RULE_VERSION",
    "RULE_VERSIONS",
    "SCHEMA",
    "ensure_lane_schema",
    "evaluate_rule",
    "execute_p1_tail_entries",
    "held_buckets",
    "live_ws_snapshot",
    "open_order_on_station_day",
    "plan_p1_tail_entries",
    "rule_version_for",
    "station_day_bucket_attempted",
    "station_day_sends",
    "tail_envelopes",
    "taker_take",
]
