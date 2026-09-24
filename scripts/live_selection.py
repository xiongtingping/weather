"""The selection-layer readings, off production's decisions and the live fills.

User ruling 2026-09-16: the paper ledger is a rule comparator, and every
reading of whether a choice paid reads what production chose and what the
exchange filled.  The board's selection readings -- the top strip's 选桶 row,
逐站汇总（跨日）, 没建仓是因为什么, 覆盖 · 价格 · EV and 篮子命中率 -- read the
paper book until this module; ``build`` hands them the same shapes from the
live book, so the sections keep their layout and change their source.

Three sources, each read where it is the record:

``live_book.build``
    every live intent with its fill, bound claim and settled bucket.  A leg
    the send gate resolved as never placed (state ``failed``) is production's
    choice that no market saw; it counts toward what was chosen on a day that
    reached the exchange, and toward the gate's refusal on a day that did not.
``basket_policy_training_fact``
    the decision production actually compiled: the seated window, its all-in
    price and claim, the ladder centre and the market centre, and the plan's
    reason codes when it held.  Read by decision id inside the station-day's
    index range, never re-derived.
``basket_target_order_events``
    why a live intent never reached the exchange, in the send gate's words.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import live_book
from selection_market_dashboard import (
    STATION_LABELS,
    _odds_from_mean_cost,
    chosen_basket_hit_trend,
    chosen_miss_offsets,
)

#: The send path resolves an intent it refused to place as ``failed`` (every
#: one of them carries ``resolved_not_placed`` and a blocking reason, checked
#: 2026-09-16: 1,583 of 1,583).  Every other state reached the exchange.
NOT_PLACED_STATE = "failed"

#: Production opens a chain only at P0 (charter 74); P0-M and P1 add to one.
OPENING_STAGE = "P0"

#: The send gate's refusal reasons, in the page's words.
GATE_LABELS = {
    "live_station_barred": "站点禁入",
    "live_intent_disarmed": "实盘总闸关着",
    "live_station_held": "站点人工待观察",
    "live_station_probation": "站点待观察（证据不足）",
    "gateway_refused_submission": "网关拒单",
}

#: Why a P0 that held did not buy, from the plan's own reason codes.  First
#: match wins, so the gate that refused the basket outranks the generic
#: "the solver chose to stand still" codes every hold carries.
HOLD_REASONS: tuple[tuple[str, str, str], ...] = (
    ("complete_basket_candidate_missing", "hold_candidate_missing",
     "候选篮缺腿（挂单锚价不可用）"),
    ("missing_leg_candidate", "hold_candidate_missing",
     "候选篮缺腿（挂单锚价不可用）"),
    ("exchange_minimum_lot_unaffordable", "hold_minimum_lot",
     "买不起交易所最小单"),
    ("support_floor_after_lift", "hold_support_floor",
     "抬到一手后情景地板不过（宪章 93）"),
    ("support_edge_after_lift", "hold_edge_after_lift",
     "抬到一手后整篮边际不正（宪章 123）"),
    ("basket_net_roi_below_target", "hold_roi_below_target",
     "净回报低于经济门槛（宪章 130）"),
    ("complete_basket_delta_not_positive", "hold_delta_not_positive",
     "整篮增量不正"),
    ("no_decision_moment", "hold_no_decision_moment",
     "非决策时刻，保持不动（宪章 77）"),
)
HOLD_OTHER = ("hold_solver_stood_still", "求解器选择不动")


def reached_exchange(leg: Mapping[str, Any]) -> bool:
    return str(leg.get("state") or "") != NOT_PLACED_STATE


def _legs_by_station_day(
    legs: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    out: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for leg in legs:
        out[(str(leg["station_id"]), str(leg["event_date"]))].append(leg)
    return out


def settled_chains(book: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One record per settled station-day whose orders reached the exchange.

    ``chosen`` is every bucket production posted that day, across stages;
    ``bought`` the buckets that filled; ``claimed`` the chosen set's mass on
    one envelope, the newest decision's that posted (``live_book
    .basket_belief``).  Each bucket at the claim of the newest leg posted on
    it added several envelopes' masses together and could pass 1.  A day whose
    every intent the send gate refused is not here: no market was asked, so
    "chosen but not bought" would blame the market for a switch.
    """

    settled = book.get("settled") or {}
    chains: list[dict[str, Any]] = []
    for (station, day), legs in sorted(
        _legs_by_station_day(book.get("legs") or []).items()
    ):
        landed = settled.get((station, day))
        if landed is None or not any(reached_exchange(leg) for leg in legs):
            continue
        chosen = {str(leg["bucket"]) for leg in legs}
        belief = live_book.basket_belief(legs, strict=False)
        chains.append({
            "station_id": station,
            "event_date": day,
            "chosen": chosen,
            "bought": {
                str(leg["bucket"]) for leg in legs
                if float(leg.get("filled_size") or 0.0) > 0
            },
            "landed": str(landed[0]),
            "claimed": (
                sum(float(belief.get(bucket, 0.0)) for bucket in chosen)
                if belief is not None else 0.0
            ),
        })
    return chains


def basket_hit_by_width(book: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Settled live baskets by how many buckets actually filled.

    One row per settled (station, day, stage) that filled, the grain the
    money board reads; ``hit`` is the truth landing in a filled bucket.
    """

    groups: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in book.get("settlements") or []:
        groups[len(row.get("held_buckets") or [])].append(row)
    out = []
    for width in sorted(groups):
        rows = groups[width]
        costed = [r for r in rows if r.get("basket_all_in_cost") is not None]
        claimed = [
            r for r in rows if r.get("claimed_win_probability") is not None
        ]
        out.append({
            "window_size": width,
            "settled_count": len(rows),
            "hit_count": sum(int(r["hit"]) for r in rows),
            "hit_rate": sum(int(r["hit"]) for r in rows) / len(rows),
            "all_in_cost_mean": (
                sum(float(r["basket_all_in_cost"]) for r in costed)
                / len(costed) if costed else None
            ),
            "claimed_win_probability_mean": (
                sum(float(r["claimed_win_probability"]) for r in claimed)
                / len(claimed) if claimed else None
            ),
            "staked_usdc": sum(float(r["stake_usdc"]) for r in rows),
            "pnl_usdc": sum(float(r["pnl_usdc"]) for r in rows),
        })
    return out


def decision_seats(
    conn: sqlite3.Connection, book: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """The seat production compiled for every live decision that was placed.

    Keyed by decision id.  The side is production's own two centres: the
    seat's ladder centre bucket against the plan's market centre bucket, on
    the decision's envelope ordinals -- above when the seat stands warm of
    the market.
    """

    wanted: dict[tuple[str, str], set[str]] = defaultdict(set)
    for leg in book.get("legs") or []:
        if reached_exchange(leg):
            wanted[(str(leg["station_id"]), str(leg["event_date"]))].add(
                str(leg["decision_id"])
            )
    seats: dict[str, dict[str, Any]] = {}
    for (station, day), decisions in sorted(wanted.items()):
        # Only the placed decisions' rows.  Without the decision filter this
        # read every fact of the station-day and ran json_extract over each
        # row's feature_json (~63 KB, into its overflow pages) to keep a few:
        # 246 s for the board's section on 2026-09-24.  decision_id sits
        # ahead of feature_json in the row, so the filter reads no overflow
        # page for the rows it drops; the rows kept, and their order off
        # basket_policy_training_fact_scope_idx, are the ones the loop below
        # always kept.
        wanted_ids = sorted(decisions)
        try:
            rows = conn.execute(
                "SELECT decision_id, stage, probability_envelope_id,"
                " json_extract(feature_json,"
                "   '$.plan.basket_support_selection.seated'),"
                " json_extract(feature_json,"
                "   '$.plan.basket_support_selection.ladder_center_bucket'),"
                " json_extract(feature_json, '$.plan.market_center_ordinal')"
                " FROM basket_policy_training_fact"
                " WHERE station_id=? AND event_date=?"
                f" AND decision_id IN ({','.join('?' for _ in wanted_ids)})",
                (station, day, *wanted_ids),
            ).fetchall()
        except sqlite3.Error:
            continue
        for decision, stage, envelope, seated_json, ladder, market in rows:
            if str(decision) not in decisions or str(decision) in seats:
                continue
            try:
                seated = json.loads(seated_json) if seated_json else None
            except (TypeError, ValueError):
                seated = None
            if not isinstance(seated, Mapping):
                continue
            side = "unknown"
            if ladder and market is not None and envelope:
                found = conn.execute(
                    "SELECT bucket_ordinals_json FROM basket_probability_envelope"
                    " WHERE envelope_id=?",
                    (str(envelope),),
                ).fetchone()
                try:
                    ordinals = json.loads(found[0]) if found else {}
                except (TypeError, ValueError):
                    ordinals = {}
                ladder_ordinal = ordinals.get(str(ladder))
                if ladder_ordinal is not None:
                    gap = int(ladder_ordinal) - int(market)
                    side = (
                        "above" if gap > 0 else "below" if gap < 0
                        else "aligned"
                    )
            price = seated.get("all_in_price")
            claim = seated.get("win_probability")
            seats[str(decision)] = {
                "station_id": station,
                "event_date": day,
                "stage": str(stage),
                "buckets": list(seated.get("buckets") or []),
                "all_in_price": None if price is None else float(price),
                "win_probability": None if claim is None else float(claim),
                "side": side,
            }
    return seats


def _mean(values: Sequence[float | None]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def _wilson_90(hits: int, n: int) -> list[float] | None:
    """Two-sided 90% Wilson interval on a hit rate, ``None`` with no sample.

    The go-live table reads a station's hit rate off a handful of 笔; the
    interval is what says whether a claim outside it is the station or luck.
    """

    if n <= 0:
        return None
    z = 1.6449
    p = hits / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / d
    return [max(0.0, centre - half), min(1.0, centre + half)]


def _seat_account(seats: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    priced = [
        s for s in seats
        if s.get("all_in_price") and s.get("win_probability") is not None
    ]
    return {
        "decision_count": len(seats),
        "model_coverage_mean": _mean([s["win_probability"] for s in seats]),
        "window_size_mean": _mean([float(len(s["buckets"])) for s in seats]),
        "seat_ask_total_mean": _mean([s["all_in_price"] for s in seats]),
        "ev_per_usdc_mean": _mean([
            float(s["win_probability"]) / float(s["all_in_price"]) - 1.0
            for s in priced
        ]),
        "sides": {
            side: sum(1 for s in seats if s["side"] == side)
            for side in ("above", "aligned", "below", "unknown")
        },
    }


def _money_account(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    staked = sum(float(r["stake_usdc"]) for r in rows)
    realized = sum(float(r["pnl_usdc"]) for r in rows)
    claimed = [r for r in rows if r.get("claimed_win_probability") is not None]
    expected = sum(
        float(r["claimed_ev_usdc"]) for r in claimed
        if r.get("claimed_ev_usdc") is not None
    )
    realized_claimed = sum(float(r["pnl_usdc"]) for r in claimed)
    hits = sum(int(r["hit"]) for r in claimed)
    claim_mean = _mean([r["claimed_win_probability"] for r in claimed])
    costed = [r for r in rows if r.get("basket_all_in_cost") is not None]
    cost_stake = sum(float(r["stake_usdc"]) for r in costed)
    cost_mean = (
        sum(float(r["stake_usdc"]) * float(r["basket_all_in_cost"])
            for r in costed) / cost_stake
        if cost_stake > 0 else None
    )
    return {
        "settled_count": len(rows),
        "staked_usdc": staked,
        "realized_usdc": realized,
        "expected_usdc": expected,
        "realization_ratio": (
            realized_claimed / expected if abs(expected) > 1e-9 else None
        ),
        "return_on_stake": realized / staked if staked > 0 else None,
        "hit_count": hits,
        # The hit rate's denominator: rows whose claim is known, which is
        # not always every settled row.
        "claimed_count": len(claimed),
        "hit_rate": hits / len(claimed) if claimed else None,
        "hit_rate_interval_90": _wilson_90(hits, len(claimed)),
        "claimed_win_probability_mean": claim_mean,
        "calibration_ratio": (
            (hits / len(claimed)) / claim_mean
            if claimed and claim_mean else None
        ),
        # Stake-weighted price, inverted once (see ``_odds_from_mean_cost``
        # for why the mean of per-row odds is the wrong number).
        "cost_mean": cost_mean,
        "odds_mean": _odds_from_mean_cost([cost_mean]),
        "ev_per_usdc": expected / staked if staked > 0 else None,
    }


def _baskets(legs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What each basket meant to buy against what it bought.

    A basket is one (station, day, stage): the stage's decisions posted one
    ladder between them, and it is the grain the settlement rows and the
    go-live table read.  ``intended`` is the distinct buckets posted to the
    exchange, ``bought`` the distinct buckets that filled.  Legs the send
    gate refused are not intended here -- no market was asked to fill them;
    they are counted apart as refused orders.
    """

    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for leg in legs:
        if reached_exchange(leg):
            groups[(str(leg["station_id"]), str(leg["event_date"]),
                    str(leg["stage"]))].append(leg)
    out = []
    for (station, day, stage), group in sorted(groups.items()):
        intended = {str(leg["bucket"]) for leg in group}
        bought = {
            str(leg["bucket"]) for leg in group
            if float(leg.get("filled_size") or 0.0) > 0
        }
        out.append({
            "station_id": station, "event_date": day, "stage": stage,
            "intended": len(intended), "bought": len(bought),
        })
    return out


def _completeness(baskets: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Baskets that bought every bucket they posted, and how close the rest got.

    ``mean`` averages bought/intended per basket, so a three-bucket basket
    that bought two is 67% whatever its money; ``unfilled`` counts the ones
    that bought nothing.
    """

    if not baskets:
        return {"baskets": 0, "complete": 0, "complete_share": None,
                "mean": None, "unfilled": 0}
    complete = sum(1 for b in baskets if b["bought"] == b["intended"])
    return {
        "baskets": len(baskets),
        "complete": complete,
        "complete_share": complete / len(baskets),
        "mean": sum(b["bought"] / b["intended"] for b in baskets) / len(baskets),
        "unfilled": sum(1 for b in baskets if b["bought"] == 0),
    }


def _station_day_hits(days: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The two station-day verdicts, counted: 每日统计's columns per station.

    ``days`` are ``live_book.station_days`` records.  篮子命中 is over every
    settled station-day, filled or not; 持仓命中 only over the ones that
    filled -- the gap between the two is the legs that did not fill, which
    is an execution number, not a model one.
    """

    settled = [d for d in days if d["settled_bucket"] is not None]
    held = [d for d in settled if d["filled_legs"]]
    return {
        "settled_station_days": len(settled),
        "basket_hit_station_days": sum(1 for d in settled if d["basket_hit"]),
        "held_station_days": len(held),
        "held_hit_station_days": sum(1 for d in held if d["held_hit"]),
    }


def station_stats(
    book: Mapping[str, Any], seats: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """One row per station that minted a live intent, across the window.

    Every live intent counts toward the order columns -- refused by the send
    gate, posted and unfilled, or filled -- so a barred station's orders are
    on its row rather than missing from it.
    """

    legs_by_station: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for leg in book.get("legs") or []:
        legs_by_station[str(leg["station_id"])].append(leg)
    rows_by_station: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in book.get("settlements") or []:
        rows_by_station[str(row["station_id"])].append(row)
    seats_by_station: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for seat in seats.values():
        seats_by_station[str(seat["station_id"])].append(seat)
    days_by_station: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for day in live_book.station_days(book):
        days_by_station[str(day["station_id"])].append(day)
    out = []
    for station in sorted(legs_by_station):
        legs = legs_by_station[station]
        reached = [leg for leg in legs if reached_exchange(leg)]
        filled = [leg for leg in legs if float(leg["filled_size"] or 0) > 0]
        seat = _seat_account(seats_by_station.get(station, []))
        out.append({
            "station_id": station,
            "station_label": STATION_LABELS.get(station, ""),
            # 笔: a (day, stage) that filled anything, settled or not -- the
            # settlement row's grain before the truth lands.
            "position_count": len({
                (leg["event_date"], leg["stage"]) for leg in filled
            }),
            "intent_leg_total": len(legs),
            "reached_leg_total": len(reached),
            "refused_leg_total": len(legs) - len(reached),
            "filled_leg_total": len(filled),
            "intent_notional_usdc": sum(
                float(leg.get("requested_notional_usdc") or 0.0) for leg in legs
            ),
            "reached_notional_usdc": sum(
                float(leg.get("requested_notional_usdc") or 0.0)
                for leg in reached
            ),
            "filled_notional_usdc": sum(
                float(leg.get("filled_notional_usdc") or 0.0) for leg in filled
            ),
            "event_days": len({leg["event_date"] for leg in legs}),
            "completeness": _completeness(_baskets(legs)),
            "sides": seat["sides"],
            "model_coverage_mean": seat["model_coverage_mean"],
            "seat_ask_total_mean": seat["seat_ask_total_mean"],
            **_station_day_hits(days_by_station.get(station, [])),
            **_money_account(rows_by_station.get(station, [])),
        })
    return out


def _gate_refusals(
    conn: sqlite3.Connection, intents: Sequence[str]
) -> dict[str, str]:
    """The send gate's reason per refused intent id."""

    out: dict[str, str] = {}
    chunk = 400
    for start in range(0, len(intents), chunk):
        part = list(intents[start:start + chunk])
        try:
            rows = conn.execute(
                "SELECT order_intent_id, event_type, event_json"
                " FROM basket_target_order_events"
                " WHERE event_type IN ('NEVER_REACHED_EXCHANGE','SUBMIT_REFUSED')"
                " AND order_intent_id IN (%s)" % ",".join("?" * len(part)),
                part,
            ).fetchall()
        except sqlite3.Error:
            return out
        for intent, event_type, event_json in rows:
            try:
                reason = str((json.loads(event_json or "{}") or {}).get("reason") or "")
            except (TypeError, ValueError):
                reason = ""
            if event_type == "SUBMIT_REFUSED" and not reason:
                reason = "gateway_refused_submission"
            out[str(intent)] = reason or str(event_type).lower()
    return out


def refusal_funnel(
    conn: sqlite3.Connection, book: Mapping[str, Any]
) -> dict[str, Any]:
    """Why a station-day production decided on ended with no live position.

    Station-day grain at the opening stage: only P0 opens a chain (charter
    74), so a P0-M or P1 decision on an unopened day is not a second refusal.
    The window starts at the first event day the live book has a leg for.
    First match wins:

    1. a live leg filled (any stage) -- 建仓;
    2. an intent reached the exchange and nothing filled -- 挂单未成交;
    3. live intents existed and the send gate refused every one -- the gate's
       reason (禁入 / 总闸 / 待观察 / 网关);
    4. a P0 decision traded but minted no live intent;
    5. every P0 decision held -- the newest P0 plan's reason codes, the
       refusing gate before the generic hold (``HOLD_REASONS``).
    """

    legs = book.get("legs") or []
    if not legs:
        return {"stages": []}
    first_day = min(str(leg["event_date"]) for leg in legs)
    by_day = _legs_by_station_day(legs)
    refused_intents = [
        str(leg["order_intent_id"]) for leg in legs
        if not reached_exchange(leg)
    ]
    gate = _gate_refusals(conn, refused_intents)
    try:
        facts = conn.execute(
            "SELECT station_id, event_date, decision_at_utc,"
            " json_extract(feature_json, '$.plan.state'),"
            " json_extract(feature_json, '$.plan.reason_codes')"
            " FROM basket_policy_training_fact"
            " WHERE stage=? AND event_date>=?",
            (OPENING_STAGE, first_day),
        ).fetchall()
    except sqlite3.Error:
        return {"stages": []}
    decided: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
    for station, day, at, state, codes in facts:
        decided[(str(station), str(day))].append(
            (str(at or ""), str(state or ""), str(codes or "[]"))
        )
    tally: dict[str, list[Any]] = {}

    def count(reason: str, label: str) -> None:
        slot = tally.setdefault(reason, [label, 0])
        slot[1] += 1

    seated = 0
    station_days: list[dict[str, str]] = []

    def record(key: tuple[str, str], reason: str, label: str) -> None:
        count(reason, label)
        station_days.append({
            "station_id": key[0], "event_date": key[1],
            "reason": reason, "label": label,
        })

    for key in sorted(set(decided) | set(by_day)):
        if key not in decided:
            continue
        day_legs = by_day.get(key, [])
        if any(float(leg["filled_size"] or 0) > 0 for leg in day_legs):
            seated += 1
            continue
        if any(reached_exchange(leg) for leg in day_legs):
            record(key, "live_unfilled", "挂到交易所未成交")
            continue
        if day_legs:
            reasons = [
                gate.get(str(leg["order_intent_id"]), "") for leg in day_legs
            ]
            named = next((r for r in reasons if r), "not_placed")
            record(key, named, GATE_LABELS.get(named, "实盘门拒单（%s）" % named))
            continue
        plans = sorted(decided[key])
        if any(state == "TRADE" for _, state, _ in plans):
            record(key, "traded_without_live_intent", "出了交易计划、没铸实盘意向")
            continue
        try:
            codes = {
                str(code).split(":")[0]
                for code in json.loads(plans[-1][2]) or []
            }
        except (TypeError, ValueError):
            codes = set()
        match = next(
            ((key_, label) for code, key_, label in HOLD_REASONS
             if code in codes),
            HOLD_OTHER,
        )
        record(key, match[0], match[1])
    refused = sum(slot[1] for slot in tally.values())
    scopes = refused + seated
    if not scopes:
        return {"stages": []}
    reasons = sorted(
        (
            {"reason": reason, "label": label, "count": n, "share": n / scopes}
            for reason, (label, n) in tally.items()
        ),
        key=lambda row: -row["count"],
    )
    return {
        "since_event_date": first_day,
        # Every station-day that ended without a live position, with its
        # reason, so the station-day listing can say why beside each day.
        "station_days": station_days,
        "stages": [{
            "stage": OPENING_STAGE,
            "seated_count": seated,
            "refused_count": refused,
            "scope_count": scopes,
            "seat_rate": seated / scopes,
            "reasons": reasons,
        }],
    }


def build(
    conn: sqlite3.Connection, book: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Every selection reading the board draws, off one live book.

    ``book``: the live book the caller already built -- the board's paged
    render builds it once per run and hands it to every section that reads
    it.  Built here when not given.
    """

    if book is None:
        book = live_book.build(conn)
    seats = decision_seats(conn, book)
    chains = settled_chains(book)
    rows = book.get("settlements") or []
    stages = {
        stage: _money_account([r for r in rows if r["stage"] == stage])
        for stage in live_book.STAGES
    }
    seat_list = list(seats.values())
    return {
        "available": bool(book.get("legs")),
        "since_event_date": min(
            (str(leg["event_date"]) for leg in book.get("legs") or []),
            default=None,
        ),
        "stages": stages,
        "coverage": {
            **_seat_account(seat_list),
            "day_count": len({s["event_date"] for s in seat_list}),
            **_money_account(rows),
        },
        "station_stats": station_stats(book, seats),
        "basket_completeness": _completeness(_baskets(book.get("legs") or [])),
        "basket_hit_by_size": basket_hit_by_width(book),
        "chosen_basket_hit": chosen_basket_hit_trend(chains),
        "chosen_miss_offsets": chosen_miss_offsets(chains),
        "refusal_stats": refusal_funnel(conn, book),
    }
