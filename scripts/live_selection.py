"""The selection-layer readings, off production's decisions and the live fills.

User ruling 2026-09-16: the paper ledger is a rule comparator, and every
reading of whether a choice paid reads what production chose and what the
exchange filled.  The board's selection readings -- the top strip's 选桶 row,
逐站汇总（跨日）, 覆盖 · 价格 · EV and 篮子命中率 -- read the
paper book until this module; ``build`` hands them the same shapes from the
live book, so the sections keep their layout and change their source.

Two sources, each read where it is the record:

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
    _odds_from_mean_cost,
    chosen_basket_hit_trend,
    chosen_miss_offsets,
)

#: The send path resolves an intent it refused to place as ``failed`` (every
#: one of them carries ``resolved_not_placed`` and a blocking reason, checked
#: 2026-09-16: 1,583 of 1,583).  Every other state reached the exchange.
NOT_PLACED_STATE = "failed"


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
        "basket_hit_by_size": basket_hit_by_width(book),
        "chosen_basket_hit": chosen_basket_hit_trend(chains),
        "chosen_miss_offsets": chosen_miss_offsets(chains),
    }
