"""How far the paper ledger has drifted from the book production actually ran.

``tests/test_ledger_production_rule_binding.py`` asks whether the two sides
carry the same RULES.  It passed on 2026-09-16 while the two sides were
picking different legs on the same station-day, because a rule can be shared
and still be applied to a different book: the binding test compares source,
and this compares outcomes.  Both are needed and neither substitutes.

Four layers, measured separately and never averaged (user ruling, 2026-09-16
-- 加对齐守卫).  Any single score over them would have read as "roughly fine":

    seat choice     posted bucket sets    Jaccard 0.75, zero cells disjoint
    fill rate       completion once open  live 54.4%, ledger 43.2% (n=37)
    fill identity   WHICH legs filled     Jaccard 0.167 against 0.310 chance
    clock           when the order went   median 2.65h apart, 4/24 within 15m

The first is green, the last is amber, and the middle two have not earned a
verdict yet.  That spread is the finding: the two sides pick the same basket
and then diverge on what happens to it.

Three decisions about the measurement itself, each of which changes the
answer:

**The universe is the orders that reached the exchange, not the orders
production authorized.**  1,550 of 1,928 live orders never reached it, every
one of them refused at the station-policy door (``live_station_barred`` 836,
``live_intent_disarmed`` 609, ``live_station_held`` 80,
``live_station_probation`` 23).  Scoring the ledger against those measures
the admission list, and the ledger is *supposed* to cover barred stations --
``live_intent_v1.json`` says so in as many words, because a barred station
accumulates its evidence nowhere else.  The share that reached is reported
alongside, as context rather than as a verdict: it is a fact about the
operator's list, not about the ledger.

**The grain is (station, event_date, stage).**  One chain is one station-day
(a ``paper_trade_position`` row is a top-up, not a separate bet), and the
three stages buy from three different beliefs.

**Fill identity is scored against chance on the same pools.**  Two sides that
each fill about 44% of two or three legs will share some legs by accident;
the honest question is whether they share MORE than that.  The baseline is
exact rather than simulated -- the intersection size of two independent
uniform draws is hypergeometric -- so the reading is reproducible and the
test can pin it.

Every layer carries its own sample count and refuses a verdict below its
minimum.  On 2026-09-16 the fill-identity layer had n=14, which is a strong
hint and not a finding, and a guard that had called that red would have been
asserting more than it knew.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "paper_ledger_alignment_guard_v1"

# Bumped whenever any layer's ARITHMETIC changes, not when the code moves.
# Two readings under different definitions are not comparable and the guard's
# whole value is the history, so the version rides on every row rather than
# living in a comment: the alternative is a column that silently means one
# thing before a commit and another after, which is how a changed axis leaves
# readers double-counting.
#
# v2 (2026-09-16, same day as v1): the fill-rate layer moved from each side's
# whole population to the cells both sides opened, because the populations
# were different station sets -- and identity cells with no room to disagree
# stopped being scored.  v1 rows read 0.431/0.444 where v2 reads 0.544/0.432
# on the same window.
DEFINITION_VERSION = 2

# The four layers, their thresholds, and the sample each needs before it may
# speak.  Every bound is anchored on the 2026-09-16 baseline rather than
# chosen for roundness -- a threshold with no measurement behind it is a
# number that will be argued with the first time it fires.
GREEN, AMBER, RED, INSUFFICIENT = "green", "amber", "red", "insufficient"

# Posted bucket sets.  Baseline 0.776 mean / 0.75 median over 77 cells, with
# ZERO cells disjoint -- the two sides never once picked unrelated baskets.
SEAT_JACCARD_GREEN = 0.60
SEAT_JACCARD_AMBER = 0.45
SEAT_DISJOINT_SHARE_AMBER = 0.10
SEAT_MIN_CELLS = 20

# Leg fill rate, over the cells both sides acted on.  Baseline over the whole
# populations was live 0.431 (156/362) against ledger 0.444 (155/349), 1.3
# points apart; on the shared cells the counts are smaller and noisier, which
# is why the bands are wide -- this is a ratio of two small counts and moves
# several points on a quiet week.
FILL_RATE_GAP_GREEN = 0.08
FILL_RATE_GAP_AMBER = 0.16
# Sixty legs a side, which is high for this board and deliberately so: two
# proportions near a half carry a standard error of about 0.06 each at that
# count, so a green band of eight points is only just meaningful there and
# would be noise at the forty this started with.  The layer stays quiet
# until it can say something -- 样本不足 is not a pass.
FILL_RATE_MIN_LEGS = 60

# Fill identity, as lift over the same-pool chance baseline.  Baseline
# 0.167 observed against 0.310 expected = -0.143: below chance, i.e. the
# ledger's choice of which leg fills says nothing about production's.
IDENTITY_LIFT_GREEN = 0.05
IDENTITY_LIFT_AMBER = -0.05
IDENTITY_MIN_CELLS = 20

# The clock.  Baseline median 2.65h, with 4 of 24 cells inside a quarter
# hour.  Two orders posted hours apart into a moving book are not the same
# order, whatever rule chose them.
CLOCK_MEDIAN_HOURS_GREEN = 1.0
CLOCK_MEDIAN_HOURS_AMBER = 4.0
CLOCK_MIN_CELLS = 10

DEFAULT_WINDOW_DAYS = 14

# A reconciliation row carries one of these when the order was stopped before
# the exchange saw it.  Anything else (including NULL) means it was placed.
_NEVER_PLACED_REASONS = frozenset({
    "order_never_reached_exchange",
    "gateway_refused_submission",
})


def _iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _loads(value: Any) -> Any:
    try:
        return json.loads(value or "null")
    except (TypeError, ValueError):
        return None


def _jaccard(left: set[str], right: set[str]) -> float | None:
    union = left | right
    if not union:
        return None
    return len(left & right) / len(union)


def chance_jaccard(pool: int, left: int, right: int) -> float | None:
    """Expected Jaccard of two independent uniform draws from one pool.

    Exact, not simulated: the size of the intersection is hypergeometric, so
    the expectation is a short finite sum.  Being closed-form is the point --
    the baseline this compares against has to be the same number tomorrow, or
    a lift that moved would be unreadable.
    """

    if pool <= 0 or left <= 0 or right <= 0:
        return None
    left, right = min(left, pool), min(right, pool)
    total = math.comb(pool, right)
    if total <= 0:
        return None
    expected = 0.0
    for shared in range(max(0, left + right - pool), min(left, right) + 1):
        ways = math.comb(left, shared) * math.comb(pool - left, right - shared)
        if not ways:
            continue
        expected += (ways / total) * (shared / (left + right - shared))
    return expected


def _verdict(
    value: float | None,
    *,
    n: int,
    minimum: int,
    green: float,
    amber: float,
    higher_is_better: bool = True,
) -> str:
    if value is None or n < minimum:
        return INSUFFICIENT
    if higher_is_better:
        if value >= green:
            return GREEN
        return AMBER if value >= amber else RED
    if value <= green:
        return GREEN
    return AMBER if value <= amber else RED


def _worst(verdicts: Iterable[str]) -> str:
    order = [RED, AMBER, GREEN]
    seen = set(verdicts)
    for level in order:
        if level in seen:
            return level
    return INSUFFICIENT


def _live_cells(conn: sqlite3.Connection, *, since: str) -> dict[str, Any]:
    """What production put in front of the exchange, by station-day-stage.

    Driven off ``basket_target_order_intents`` rather than off the decision
    table: intents are three thousand rows against fifty thousand decisions,
    and every intent already joins to its decision on an indexed key.
    """

    placed: dict[str, tuple[bool, str | None]] = {}
    for intent_id, filled, blocker in conn.execute(
        "SELECT order_intent_id, filled_size, blocking_reason"
        " FROM basket_target_order_reconciliations"
    ):
        placed[str(intent_id)] = (
            float(filled or 0.0) > 0.0,
            None if blocker is None else str(blocker),
        )

    cells: dict[tuple[str, str, str], dict[str, Any]] = {}
    authorized = 0
    reached = 0
    for station, event_date, details_json, bucket, intent_id, created_at in (
        conn.execute(
            "SELECT d.station_id, d.event_date, d.details_json, i.bucket,"
            "       i.order_intent_id, i.created_at"
            " FROM basket_target_order_intents i"
            " JOIN auto_trade_decisions d ON d.decision_id = i.decision_id"
            " WHERE i.live_submit_authorized = 1 AND d.event_date >= ?",
            (since,),
        )
    ):
        authorized += 1
        state = placed.get(str(intent_id))
        if state is None or state[1] in _NEVER_PLACED_REASONS:
            continue
        reached += 1
        details = _loads(details_json)
        stage = "?"
        if isinstance(details, Mapping):
            signature = details.get("decision_signature")
            if isinstance(signature, Mapping):
                stage = str(signature.get("stage") or "?")
        cell = cells.setdefault(
            (str(station), str(event_date), stage),
            {"posted": set(), "filled": set(), "legs": 0, "fills": 0,
             "placed_at": []},
        )
        cell["posted"].add(str(bucket))
        cell["legs"] += 1
        if state[0]:
            cell["filled"].add(str(bucket))
            cell["fills"] += 1
        stamp = _iso(created_at)
        if stamp is not None:
            cell["placed_at"].append(stamp)
    for cell in cells.values():
        cell["placed_at"].sort()
    # The two counts ride with the cells because the caller has to report
    # them together: a reach rate read apart from the cells it produced
    # invites the reading that the ledger refused what the door did.
    return {"cells": cells, "authorized": authorized, "reached": reached}


def _ledger_cells(
    conn: sqlite3.Connection, *, since: str
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """What the ledger says it posted and bought, at the same grain.

    ``posted_buckets_json`` is the order that went on the book and
    ``window_buckets_json`` is what it bought -- the ledger records an
    unfilled leg rather than dropping it, which is the only reason a fill
    rate can be computed on this side at all.  Rungs come from
    ``resting_fill_json`` (``filled/posted@price`` per bucket), which counts
    ladder rungs rather than legs and so is the closer match to one live
    order per row.
    """

    cells: dict[tuple[str, str, str], dict[str, Any]] = {}
    for station, event_date, stage, posted, window, resting, decided in (
        conn.execute(
            "SELECT station_id, event_date, stage, posted_buckets_json,"
            "       window_buckets_json, resting_fill_json, decision_at_utc"
            " FROM paper_trade_position WHERE event_date >= ?",
            (since,),
        )
    ):
        cell = cells.setdefault(
            (str(station), str(event_date), str(stage)),
            {"posted": set(), "filled": set(), "legs": 0, "fills": 0,
             "decided_at": None},
        )
        for bucket in (_loads(posted) or []):
            cell["posted"].add(str(bucket))
        for bucket in (_loads(window) or []):
            cell["filled"].add(str(bucket))
        for rung in (_loads(resting) or {}).values():
            try:
                got, rest = str(rung).split("/", 1)
                total = int(str(rest).split("@", 1)[0])
            except (TypeError, ValueError):
                continue
            cell["legs"] += total
            cell["fills"] += min(int(got), total)
        stamp = _iso(decided)
        if stamp is not None and (
            cell["decided_at"] is None or stamp < cell["decided_at"]
        ):
            cell["decided_at"] = stamp
    return cells


def measure(
    conn: sqlite3.Connection,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The four layers, each with its sample, its reading and its verdict."""

    at = now or datetime.now(timezone.utc)
    since = (at - timedelta(days=int(window_days))).date().isoformat()
    live_read = _live_cells(conn, since=since)
    live: dict[tuple[str, str, str], dict[str, Any]] = live_read["cells"]
    ledger = _ledger_cells(conn, since=since)
    shared = sorted(set(live) & set(ledger))

    seat: list[float] = []
    disjoint = 0
    identity_obs: list[float] = []
    identity_chance: list[float] = []
    clock: list[float] = []
    within_quarter = 0
    examples: list[dict[str, Any]] = []

    for key in shared:
        live_cell, led_cell = live[key], ledger[key]
        overlap = _jaccard(led_cell["posted"], live_cell["posted"])
        if overlap is not None:
            seat.append(overlap)
            if overlap == 0.0:
                disjoint += 1
        if live_cell["filled"] and led_cell["filled"]:
            observed = _jaccard(led_cell["filled"], live_cell["filled"])
            pool = led_cell["posted"] | live_cell["posted"]
            baseline = chance_jaccard(
                len(pool), len(led_cell["filled"]), len(live_cell["filled"])
            )
            # A cell where chance is already 1.0 cannot disagree: both
            # sides bought the whole pool, so agreeing was the only
            # available outcome.  Scoring it would report lift 0.0 --
            # "no better than chance" -- for two books that matched
            # perfectly, which is the opposite of what happened.  A cell
            # with no room to disagree is no evidence of agreement, the
            # same reason ``chance_jaccard`` declines a side that bought
            # nothing.
            if (
                observed is not None and baseline is not None
                and baseline < 1.0
            ):
                identity_obs.append(observed)
                identity_chance.append(baseline)
                if len(examples) < 20:
                    examples.append({
                        "station_id": key[0], "event_date": key[1],
                        "stage": key[2],
                        "ledger_filled": sorted(led_cell["filled"]),
                        "live_filled": sorted(live_cell["filled"]),
                        "jaccard": round(observed, 3),
                        "chance": round(baseline, 3),
                    })
        if led_cell["decided_at"] is not None and live_cell["placed_at"]:
            gap = min(
                abs((led_cell["decided_at"] - stamp).total_seconds()) / 3600.0
                for stamp in live_cell["placed_at"]
            )
            clock.append(gap)
            if gap <= 0.25:
                within_quarter += 1

    # Both rates over the cells where BOTH sides bought something, and the
    # conditioning is the point rather than a convenience.
    #
    # Summing each side's whole population compares different station sets:
    # the ledger seats barred and probation stations by design and
    # production cannot trade them.  That reading was 0.431 against 0.444 on
    # 2026-09-16 and looked like agreement.
    #
    # Restricting to the shared cells fixes the station set and breaks
    # something else, which is the trap this comment exists for: a ledger
    # chain that never filled a leg writes a REFUSAL, not a position, so
    # every ledger cell here is already conditioned on having opened while
    # the live side is not.  The same window read that way was 0.325 against
    # 0.475, and that gap is mostly the conditioning.
    #
    # So both sides carry it.  What this layer asks is "once a basket
    # started filling, how much of it filled" -- a completion rate, on one
    # population, with one selection applied to both.  It deliberately does
    # NOT answer "how often does a chain open at all": that question needs
    # the ledger's refusal rows, and reading it off these numbers is the
    # mistake above with the sign hidden.
    opened = [
        key for key in shared
        if live[key]["fills"] and ledger[key]["fills"]
    ]
    live_legs = sum(live[key]["legs"] for key in opened)
    live_fills = sum(live[key]["fills"] for key in opened)
    led_legs = sum(ledger[key]["legs"] for key in opened)
    led_fills = sum(ledger[key]["fills"] for key in opened)
    live_rate = live_fills / live_legs if live_legs else None
    led_rate = led_fills / led_legs if led_legs else None
    rate_gap = (
        abs(live_rate - led_rate)
        if live_rate is not None and led_rate is not None else None
    )
    lift = (
        statistics.mean(identity_obs) - statistics.mean(identity_chance)
        if identity_obs else None
    )

    layers = {
        "seat_choice": {
            "label": "选哪几个桶",
            "n": len(seat),
            "minimum_n": SEAT_MIN_CELLS,
            "jaccard_mean": round(statistics.mean(seat), 4) if seat else None,
            "jaccard_median": (
                round(statistics.median(seat), 4) if seat else None
            ),
            "disjoint_cells": disjoint,
            "disjoint_share": (
                round(disjoint / len(seat), 4) if seat else None
            ),
            "verdict": _verdict(
                statistics.mean(seat) if seat else None,
                n=len(seat), minimum=SEAT_MIN_CELLS,
                green=SEAT_JACCARD_GREEN, amber=SEAT_JACCARD_AMBER,
            ),
        },
        "fill_rate": {
            "label": "开仓后成交几成",
            "n": min(live_legs, led_legs),
            "minimum_n": FILL_RATE_MIN_LEGS,
            "live_rate": round(live_rate, 4) if live_rate is not None else None,
            "ledger_rate": round(led_rate, 4) if led_rate is not None else None,
            "live_legs": live_legs, "ledger_legs": led_legs,
            "cells": len(opened),
            "gap": round(rate_gap, 4) if rate_gap is not None else None,
            "verdict": _verdict(
                rate_gap, n=min(live_legs, led_legs),
                minimum=FILL_RATE_MIN_LEGS,
                green=FILL_RATE_GAP_GREEN, amber=FILL_RATE_GAP_AMBER,
                higher_is_better=False,
            ),
        },
        "fill_identity": {
            "label": "哪一条腿成交",
            "n": len(identity_obs),
            "minimum_n": IDENTITY_MIN_CELLS,
            "jaccard_mean": (
                round(statistics.mean(identity_obs), 4)
                if identity_obs else None
            ),
            "chance_mean": (
                round(statistics.mean(identity_chance), 4)
                if identity_chance else None
            ),
            "lift": round(lift, 4) if lift is not None else None,
            "verdict": _verdict(
                lift, n=len(identity_obs), minimum=IDENTITY_MIN_CELLS,
                green=IDENTITY_LIFT_GREEN, amber=IDENTITY_LIFT_AMBER,
            ),
        },
        "clock": {
            "label": "什么时刻下的单",
            "n": len(clock),
            "minimum_n": CLOCK_MIN_CELLS,
            "median_hours": (
                round(statistics.median(clock), 3) if clock else None
            ),
            "within_quarter_hour": within_quarter,
            "within_quarter_share": (
                round(within_quarter / len(clock), 4) if clock else None
            ),
            "verdict": _verdict(
                statistics.median(clock) if clock else None,
                n=len(clock), minimum=CLOCK_MIN_CELLS,
                green=CLOCK_MEDIAN_HOURS_GREEN,
                amber=CLOCK_MEDIAN_HOURS_AMBER, higher_is_better=False,
            ),
        },
    }

    authorized = int(live_read["authorized"])
    reached = int(live_read["reached"])
    return {
        "schema": SCHEMA,
        "definition_version": DEFINITION_VERSION,
        "measured_at_utc": at.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "window_days": int(window_days),
        "window_from": since,
        "layers": layers,
        "verdict": _worst(layer["verdict"] for layer in layers.values()),
        # Context, never a verdict: this is a reading of the operator's
        # admission list, and the ledger is meant to cover what that list
        # keeps out.
        "live_orders_authorized": authorized,
        "live_orders_reached_exchange": reached,
        "reach_rate": (
            round(reached / authorized, 4) if authorized else None
        ),
        "cells_live": len(live),
        "cells_ledger": len(ledger),
        "cells_shared": len(shared),
        "examples": examples,
    }


__all__ = [
    "SCHEMA", "DEFINITION_VERSION", "GREEN", "AMBER", "RED", "INSUFFICIENT",
    "DEFAULT_WINDOW_DAYS", "chance_jaccard", "measure",
]
