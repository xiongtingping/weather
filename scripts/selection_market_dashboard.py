#!/usr/bin/env python3
"""Every station's daily seat, read against what the market was charging.

Two questions, one page.  Before settlement: where did we sit relative to
the book -- above it, below it, or on it?  After settlement: did the basket
contain the truth, what did the paper book make, at what odds, and which
quotes were not real prices.

The lean is measured as the gap between the model's centre of mass and the
market's, both computed on the same buckets from the same snapshot, in
bucket widths so Fahrenheit and Celsius books read alike:

    lean = model_centre_ordinal - market_centre_ordinal

The market's centre is its own implied distribution -- each bucket's all-in
ask normalised to sum to one.  That is what "the market thinks" means when
the market speaks in prices.

Read-only, and it reads the paper ledger rather than recomputing anything,
so what it shows is exactly what was scored.

Data source only -- the single frontend is scripts/calibration_dashboard.py,
which imports ``build_payload`` from here and renders every section.

    python3 scripts/selection_market_dashboard.py --json   # inspect payload
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
INITIAL_CAPITAL_USDC = 1000.0
# Below a one-cent basket cost the fill is almost always a minimum-tick
# offer with nothing bid against it; the paper ledger records these and
# reports them apart rather than letting them set the headline.
DUST_COST_FLOOR = 0.01
# A lean smaller than this is the same view, not a disagreement.
LEAN_TOLERANCE_BUCKETS = 0.35
# Everything before this event date belongs to a different strategy: the
# ladder floor, the dispersion cap and the probability-following rebalance
# all landed on 2026-08-15, so earlier seats were chosen by rules that no
# longer run and averaging them in would describe a system that no longer
# exists.
STATS_START_EVENT_DATE = "2026-08-15"


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _centre_of_mass(
    weights: dict[str, float], ordinals: dict[str, int]
) -> float | None:
    total = sum(max(0.0, v) for v in weights.values())
    if total <= 0:
        return None
    return sum(
        ordinals[b] * max(0.0, v)
        for b, v in weights.items()
        if b in ordinals
    ) / total


def _rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    query = conn.execute(
        "SELECT p.station_id, p.event_date, p.stage, p.decision_at_utc,"
        " p.window_buckets_json, p.ladder_center_bucket, p.win_probability,"
        " p.all_in_cost, p.edge, p.stake_usdc, p.probabilities_json,"
        " p.prices_json, p.window_size,"
        " s.hit, s.settled_tmax_c, s.settled_bucket, s.pnl_usdc,"
        " s.bucket_distance, s.center_error_c"
        " FROM paper_trade_position p"
        " LEFT JOIN paper_trade_settlement s"
        " ON s.position_id = p.position_id"
        " ORDER BY p.event_date DESC, p.station_id, p.stage"
    )
    for r in query:
        if str(r[1]) < STATS_START_EVENT_DATE:
            continue
        probabilities = json.loads(r[10])
        prices = json.loads(r[11])
        ordinals = {
            bucket: index
            for index, bucket in enumerate(probabilities.keys())
        }
        model_centre = _centre_of_mass(probabilities, ordinals)
        market_centre = _centre_of_mass(prices, ordinals)
        lean = (
            None
            if model_centre is None or market_centre is None
            else model_centre - market_centre
        )
        if lean is None:
            side = "unknown"
        elif lean > LEAN_TOLERANCE_BUCKETS:
            side = "above"
        elif lean < -LEAN_TOLERANCE_BUCKETS:
            side = "below"
        else:
            side = "aligned"
        cost = float(r[7])
        win = float(r[6])
        # What the seat covers, what it costs, and what that pair is worth.
        #
        # Coverage is the model's mass inside the window against the market's
        # own implied mass over the same buckets: the market quotes a book
        # whose asks sum to more than one (the vig), so its implied coverage
        # is normalised by that sum rather than taken raw -- otherwise every
        # seat would look cheap by the width of the spread.
        book_ask_total = sum(max(0.0, v) for v in prices.values())
        seat_ask_total = sum(
            max(0.0, prices.get(b, 0.0)) for b in json.loads(r[4])
        )
        market_coverage = (
            seat_ask_total / book_ask_total if book_ask_total > 0 else None
        )
        # EV per USDC staked.  A basket costing C pays 1 when the truth lands
        # inside it, so a unit of stake buys 1/C of payout and returns
        # p/C - 1 in expectation.  This is the same quantity the edge (p - C)
        # describes, restated per unit of capital, which is what makes two
        # seats at different price levels comparable.
        ev_per_usdc = (win / cost - 1.0) if cost > 0 else None
        stake = float(r[9])
        out.append({
            "market_coverage": market_coverage,
            "book_ask_total": book_ask_total,
            "seat_ask_total": seat_ask_total,
            "coverage_gap": (
                None if market_coverage is None else win - market_coverage
            ),
            "ev_per_usdc": ev_per_usdc,
            "ev_usdc": (
                None if ev_per_usdc is None else stake * ev_per_usdc
            ),
            "station_id": r[0],
            "event_date": r[1],
            "stage": r[2],
            "decision_at_utc": r[3],
            "window": json.loads(r[4]),
            "window_size": int(r[12]),
            "centre_bucket": r[5],
            "win_probability": float(r[6]),
            "all_in_cost": cost,
            "edge": float(r[8]),
            "odds": (1.0 - cost) / cost if cost > 0 else None,
            "stake_usdc": float(r[9]),
            "lean_buckets": lean,
            "side": side,
            "dust_offer": cost < DUST_COST_FLOOR,
            "settled": r[13] is not None,
            "hit": None if r[13] is None else int(r[13]),
            "settled_tmax_c": r[14],
            "settled_bucket": r[15],
            "pnl_usdc": r[16],
            "bucket_distance": r[17],
            "center_error_c": r[18],
        })
    return out


def build_payload(db: Path) -> dict[str, Any]:
    conn = _connect(db)
    rows = _rows(conn)
    by_side: dict[str, int] = defaultdict(int)
    for row in rows:
        by_side[row["side"]] += 1

    def _slice(subset: list[dict[str, Any]]) -> dict[str, Any]:
        settled = [r for r in subset if r["settled"]]
        if not settled:
            return {"settled_count": 0, "position_count": len(subset)}
        hits = sum(int(r["hit"]) for r in settled)
        staked = sum(r["stake_usdc"] for r in settled)
        pnl = sum(float(r["pnl_usdc"] or 0.0) for r in settled)
        claimed = sum(r["win_probability"] for r in settled) / len(settled)
        odds = [r["odds"] for r in settled if r["odds"] is not None]
        return {
            "position_count": len(subset),
            "settled_count": len(settled),
            "hit_count": hits,
            "hit_rate": hits / len(settled),
            "claimed_win_probability_mean": claimed,
            "calibration_ratio": (
                (hits / len(settled)) / claimed if claimed > 0 else None
            ),
            "odds_mean": sum(odds) / len(odds) if odds else None,
            "staked_usdc": staked,
            "pnl_usdc": pnl,
            "return_on_stake": pnl / staked if staked > 0 else None,
        }

    stages: dict[str, Any] = {}
    for stage in ("P0", "P0-M", "P1"):
        subset = [r for r in rows if r["stage"] == stage]
        credible = [r for r in subset if not r["dust_offer"]]
        dust = [r for r in subset if r["dust_offer"]]
        stages[stage] = {
            "all": _slice(subset),
            "credible": _slice(credible),
            "dust_offer": _slice(dust),
            "sides": {
                side: sum(1 for r in subset if r["side"] == side)
                for side in ("above", "below", "aligned", "unknown")
            },
        }
    # Quotes that are not really prices, listed so they can be chased rather
    # than quietly carried in the headline.
    anomalies = [
        {
            "station_id": r["station_id"],
            "event_date": r["event_date"],
            "stage": r["stage"],
            "window": r["window"],
            "all_in_cost": r["all_in_cost"],
            "win_probability": r["win_probability"],
            "kind": (
                "basket_under_one_cent"
                if r["dust_offer"]
                else "claim_over_market_by_10x"
            ),
        }
        for r in rows
        if r["dust_offer"]
        or (r["all_in_cost"] > 0 and r["win_probability"] / r["all_in_cost"] > 10.0)
    ]
    # Every registered station, not only the ones that got a seat.  A station
    # missing from the ledger is the more interesting row: it means the
    # objective looked and did not sit, or never got a priced book to look
    # at, and that has to be visible rather than absent.
    latest_day = max((r["event_date"] for r in rows), default=None)
    roster = []
    registered = [
        str(row[0])
        for row in conn.execute(
            "SELECT station_id FROM calibration_station_registry"
            " ORDER BY station_id"
        )
    ]
    seated = {
        (r["station_id"], r["stage"]): r
        for r in rows
        if latest_day and r["event_date"] == latest_day
    }
    for station in registered:
        entry: dict[str, Any] = {"station_id": station, "stages": {}}
        for stage in ("P0", "P0-M", "P1"):
            row = seated.get((station, stage))
            entry["stages"][stage] = (
                None
                if row is None
                else {
                    "window": row["window"],
                    "window_size": row["window_size"],
                    "centre_bucket": row["centre_bucket"],
                    "win_probability": row["win_probability"],
                    "all_in_cost": row["all_in_cost"],
                    "odds": row["odds"],
                    "side": row["side"],
                    "lean_buckets": row["lean_buckets"],
                    "stake_usdc": row["stake_usdc"],
                    "hit": row["hit"],
                    "pnl_usdc": row["pnl_usdc"],
                    "dust_offer": row["dust_offer"],
                }
            )
        entry["seated_stage_count"] = sum(
            1 for v in entry["stages"].values() if v is not None
        )
        # The movement between stages belongs on the same row as the stages
        # themselves: kept separate it was a second table saying the same
        # thing with the columns rearranged.
        seen_windows = [
            v["window"] for v in (
                entry["stages"][s] for s in ("P0", "P0-M", "P1")
            ) if v is not None
        ]
        if len(seen_windows) >= 2:
            first, last = seen_windows[0], seen_windows[-1]
            delta = len(last) - len(first)
            entry["movement"] = (
                "unchanged" if first == last
                else ("wider" if delta > 0
                      else ("narrower" if delta < 0 else "shifted"))
            )
            entry["width_delta"] = delta
        else:
            entry["movement"] = None
            entry["width_delta"] = 0
        settled = [
            v for v in entry["stages"].values()
            if v is not None and v.get("hit") is not None
        ]
        entry["settled_tmax_c"] = next(
            (
                r["settled_tmax_c"] for r in rows
                if r["station_id"] == station
                and r["event_date"] == latest_day
                and r["settled_tmax_c"] is not None
            ),
            None,
        )
        entry["realized_usdc"] = (
            sum(float(v["pnl_usdc"] or 0.0) for v in settled)
            if settled else None
        )
        # Why a station has nothing: the ledger records a probability
        # snapshot for it or it does not, and a priced book at that moment
        # or not.  Naming which is missing is the difference between "the
        # objective declined" and "the objective never got to look".
        if entry["seated_stage_count"] == 0 and latest_day:
            has_probability = conn.execute(
                "SELECT 1 FROM basket_probability_bridge_history"
                " WHERE station_id=? AND event_date=? LIMIT 1",
                (station, latest_day),
            ).fetchone() is not None
            has_book = conn.execute(
                "SELECT 1 FROM basket_official_ws_snapshot"
                " WHERE station_id=? AND event_date=? LIMIT 1",
                (station, latest_day),
            ).fetchone() is not None
            entry["absent_reason"] = (
                "no_probability" if not has_probability
                else ("no_market_book" if not has_book
                      else "no_seat_taken")
            )
        roster.append(entry)
    # Aggregates over credible fills only; a sub-one-cent basket has an EV
    # per USDC in the hundreds and would set every average by itself.
    credible_rows = [r for r in rows if not r["dust_offer"]]

    def _mean(values: list[float]) -> float | None:
        clean = [v for v in values if v is not None]
        return sum(clean) / len(clean) if clean else None

    settled_credible = [r for r in credible_rows if r["settled"]]
    expected_usdc = sum(
        float(r["ev_usdc"] or 0.0) for r in settled_credible
    )
    realized_usdc = sum(
        float(r["pnl_usdc"] or 0.0) for r in settled_credible
    )
    coverage_stats = {
        "position_count": len(credible_rows),
        "model_coverage_mean": _mean(
            [r["win_probability"] for r in credible_rows]
        ),
        "market_coverage_mean": _mean(
            [r["market_coverage"] for r in credible_rows]
        ),
        "coverage_gap_mean": _mean(
            [r["coverage_gap"] for r in credible_rows]
        ),
        "window_size_mean": _mean(
            [float(r["window_size"]) for r in credible_rows]
        ),
        "all_in_cost_mean": _mean(
            [r["all_in_cost"] for r in credible_rows]
        ),
        "book_ask_total_mean": _mean(
            [r["book_ask_total"] for r in credible_rows]
        ),
        "ev_per_usdc_mean": _mean([r["ev_per_usdc"] for r in credible_rows]),
        "staked_usdc": sum(r["stake_usdc"] for r in settled_credible),
        "expected_usdc": expected_usdc,
        "realized_usdc": realized_usdc,
        # The gap between what the model said the book was worth and what it
        # returned.  A persistent shortfall is the calibration ratio showing
        # up in money rather than in hit rate.
        "realization_ratio": (
            realized_usdc / expected_usdc
            if abs(expected_usdc) > 1e-9 else None
        ),
        "settled_count": len(settled_credible),
    }
    # The same three statistics per station.  A fleet average hides the
    # thing worth acting on: stations do not share a book, a climate or a
    # bucket width, so a coverage gap that is real at one and an artifact at
    # another average into a number that describes neither.
    per_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in credible_rows:
        per_station[row["station_id"]].append(row)
    station_stats = []
    for station in sorted(per_station):
        subset = per_station[station]
        settled = [r for r in subset if r["settled"]]
        hits = sum(int(r["hit"]) for r in settled)
        claimed = _mean([r["win_probability"] for r in settled])
        expected = sum(float(r["ev_usdc"] or 0.0) for r in settled)
        realized = sum(float(r["pnl_usdc"] or 0.0) for r in settled)
        station_stats.append({
            "station_id": station,
            "position_count": len(subset),
            "settled_count": len(settled),
            "window_size_mean": _mean(
                [float(r["window_size"]) for r in subset]
            ),
            "model_coverage_mean": _mean(
                [r["win_probability"] for r in subset]
            ),
            "market_coverage_mean": _mean(
                [r["market_coverage"] for r in subset]
            ),
            "coverage_gap_mean": _mean(
                [r["coverage_gap"] for r in subset]
            ),
            "all_in_cost_mean": _mean([r["all_in_cost"] for r in subset]),
            "book_ask_total_mean": _mean(
                [r["book_ask_total"] for r in subset]
            ),
            "ev_per_usdc_mean": _mean([r["ev_per_usdc"] for r in subset]),
            "odds_mean": _mean([r["odds"] for r in subset]),
            "sides": {
                side: sum(1 for r in subset if r["side"] == side)
                for side in ("above", "aligned", "below")
            },
            "hit_count": hits,
            "hit_rate": hits / len(settled) if settled else None,
            "claimed_win_probability_mean": claimed,
            "calibration_ratio": (
                (hits / len(settled)) / claimed
                if settled and claimed else None
            ),
            "staked_usdc": sum(r["stake_usdc"] for r in settled),
            "expected_usdc": expected,
            "realized_usdc": realized,
            "realization_ratio": (
                realized / expected if abs(expected) > 1e-9 else None
            ),
        })
    days = sorted({r["event_date"] for r in rows}, reverse=True)
    return {
        "schema": "selection_market_dashboard_v1",
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "initial_capital_usdc": INITIAL_CAPITAL_USDC,
        "position_count": len(rows),
        "day_count": len(days),
        "latest_day": days[0] if days else None,
        "sides": dict(by_side),
        "stages": stages,
        "anomalies": anomalies[:40],
        "coverage_stats": coverage_stats,
        "station_stats": station_stats,
        "station_roster": roster,
        "roster_event_date": latest_day,
        "stats_start_event_date": STATS_START_EVENT_DATE,
        "rows": rows[:400],
    }


# This module renders nothing.  There is exactly one frontend (user ruling,
# 2026-08-15): every section lives in scripts/calibration_dashboard.py, which
# imports build_payload from here.  A second page meant reading one thing in
# two places, so the serving and templating that used to live here are gone
# rather than merely unused.


def main() -> int:
    """Print the payload.  Rendering belongs to the one dashboard."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json", action="store_true", default=True)
    args = parser.parse_args()
    print(json.dumps(
        build_payload(args.db.expanduser().resolve(strict=True)),
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
