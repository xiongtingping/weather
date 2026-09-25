#!/usr/bin/env python3
"""Is there a subset of admitted windows that can be picked BEFORE the day?

Read-only.  Step one of the strategy plan, and the one every later step
rests on: the model's whole-domain score is worse than the market's, but
that does not settle whether some identifiable slice carries an edge.  This
asks the question the only way it can be honestly asked -- by picking the
slice on the past and scoring it on the future.

Each row is one enumerated support window at one decision, as production
recorded it (charter 140, ``basket_candidate_window_scored``): the lifted
claim the gate divided, the all-in crossing price, whether the gate admitted
it, whether the book seated it, and where the day settled.  At one share of each leg a
window costs ``all_in_price`` and pays 1 when the truth lands inside it, so
its realised edge is ``hit - all_in_price`` -- no fill model, no sizing, no
execution.  That is deliberate: this step must not be able to inherit the
maker fill bias, which is measured and unresolved.

**The rolling origin.**  For each event date t, cells are selected using
only rows from dates strictly before t, and scored only on rows from t.
Nothing about day t informs the choice of what to hold on day t.  A cell
needs ``--min-cell-n`` settled rows in the past before it may be picked,
and its past realised edge must clear ``--min-edge`` per dollar.

**What a cell is.**  Stage, agreement band (production's own lambda
partition, imported), claimed-edge band, lead-time bucket, and the band
axis the claim was computed on -- because moving that axis moves every
claim and therefore the agreement the cell is keyed on.

Three references, because a selection rule has to beat something:
``all`` every admitted window, ``seated`` what the book actually bought,
and ``selected`` the rolling rule's own picks.

    python3 scripts/measure_admission_edge_rolling.py --min-cell-n 12
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.candidate_window_evidence import (  # noqa: E402
    CANDIDATE_SOURCE,
)
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    EFFECTIVE_EDGE_LAMBDA,
)
from bayes_basket_target.settlement_time import (  # noqa: E402
    hours_to_settlement_if_known,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
OUT_DIR = ROOT / "data" / "research" / "admission_edge_rolling"
# What a dollar of basket has to beat, by execution family.  The taker
# number is the measured certain cost of a crossing basket -- 3.3 points of
# venue rake plus 3.8 of spread, both paid before the weather happens.  The
# maker family has no single number: its cost is a fill probability that
# depends on the outcome, measured and not yet resolved, so a maker
# threshold here would be an invention.  Reported against the taker bar.
TAKER_CERTAIN_COST = 0.071
# The claimed-edge partition.  Deliberately the same cuts the settled book
# has been read on all along, so a cell here can be set beside those
# readings rather than needing its own.
EDGE_BANDS = ((-9.0, 0.0), (0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 9.0))
LEAD_BANDS = ((0.0, 6.0), (6.0, 12.0), (12.0, 24.0), (24.0, 48.0), (48.0, 1e9))


def _agreement_band(agreement: float) -> str:
    """Production's own lambda partition, read off the served table."""

    lower = 0.0
    for upper, _lambda in EFFECTIVE_EDGE_LAMBDA:
        if agreement < float(upper):
            return "a[%.2f,%.2f)" % (lower, float(upper))
        lower = float(upper)
    return "a[%.2f,inf)" % lower


def _band(value: float, bands) -> str:
    for low, high in bands:
        if low <= value < high:
            return "[%g,%g)" % (low, high)
    return "[%g,inf)" % bands[-1][0]


def _rows(conn: sqlite3.Connection, since: str) -> list[dict]:
    timezones = {
        str(station): str(zone)
        for station, zone in conn.execute(
            "SELECT station_id, settlement_timezone"
            " FROM calibration_station_market_timezone_binding"
        )
    }
    out: list[dict] = []
    for (station, date, stage, at, buckets_js, claim, price, admissible,
         seated, settled, hit, axis) in conn.execute(
        "SELECT station_id, event_date, stage, decision_at_utc,"
        "       buckets_json, win_probability, all_in_price, admissible,"
        "       seated, settled_bucket, hit, band_axis"
        f"  FROM {CANDIDATE_SOURCE}"
        " WHERE settled_bucket IS NOT NULL"
        "   AND all_in_price IS NOT NULL AND all_in_price > 0"
        "   AND (?1 = '' OR event_date >= ?1)"
        " ORDER BY event_date, station_id, decision_at_utc",
        (since,),
    ):
        try:
            window = [str(b) for b in json.loads(buckets_js or "[]")]
        except (TypeError, ValueError):
            continue
        if not window:
            continue
        claim = float(claim or 0.0)
        price = float(price)
        lead = hours_to_settlement_if_known(
            str(date), as_of_utc=str(at),
            settlement_timezone=timezones.get(str(station), "UTC"),
        )
        out.append({
            "station": str(station), "date": str(date), "stage": str(stage),
            "window": window, "claim": claim, "price": price,
            "admissible": bool(admissible), "seated": bool(seated),
            # The realised payout of one share of each leg: 1 if the truth
            # landed in this window, whatever the book did about it.
            "won": str(settled) in window,
            "recorded_hit": None if hit is None else int(hit),
            "axis": str(axis or "unknown"),
            "cell": "|".join((
                str(stage),
                _agreement_band(price / claim if claim > 0 else 9.9),
                "e" + _band(claim - price, EDGE_BANDS),
                "h" + (
                    _band(float(lead), LEAD_BANDS) if lead is not None
                    else "unknown"
                ),
                str(axis or "unknown"),
            )),
        })
    return out


def _stats(rows: list[dict]) -> dict:
    cost = sum(r["price"] for r in rows)
    payout = sum(1.0 for r in rows if r["won"])
    return {
        "windows": len(rows),
        "cost": round(cost, 2),
        "wins": int(payout),
        "edge_per_dollar": round((payout - cost) / cost, 4) if cost else None,
        "roi_pct": round(100.0 * (payout - cost) / cost, 2) if cost else None,
    }


def _bootstrap(rows: list[dict], *, draws: int, seed: int) -> dict | None:
    """Clustered on the event date, because a day is one weather draw."""

    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_day[row["date"]].append(row)
    days = sorted(by_day)
    if len(days) < 3:
        return None
    generator = random.Random(seed)
    values: list[float] = []
    for _ in range(draws):
        sample: list[dict] = []
        for _ in days:
            sample.extend(by_day[generator.choice(days)])
        cost = sum(r["price"] for r in sample)
        if cost <= 0:
            continue
        payout = sum(1.0 for r in sample if r["won"])
        values.append((payout - cost) / cost)
    if not values:
        return None
    values.sort()
    return {
        "draws": len(values),
        "p2_5": round(values[int(len(values) * 0.025)], 4),
        "p97_5": round(values[int(len(values) * 0.975)], 4),
        "p_negative": round(
            sum(1 for v in values if v < 0) / len(values), 3
        ),
    }


def measure(
    conn: sqlite3.Connection,
    *,
    since: str,
    min_cell_n: int,
    min_edge: float,
    draws: int,
    seed: int,
) -> dict:
    rows = _rows(conn, since)
    admitted = [r for r in rows if r["admissible"]]
    seated = [r for r in rows if r["seated"]]
    days = sorted({r["date"] for r in rows})

    selected: list[dict] = []
    per_day: list[dict] = []
    picks_by_day: dict[str, list[str]] = {}
    for index, today in enumerate(days):
        past = [r for r in admitted if r["date"] < today]
        cells: dict[str, list[dict]] = defaultdict(list)
        for row in past:
            cells[row["cell"]].append(row)
        picks = set()
        for cell, cell_rows in cells.items():
            if len(cell_rows) < min_cell_n:
                continue
            stats = _stats(cell_rows)
            if stats["edge_per_dollar"] is None:
                continue
            if stats["edge_per_dollar"] > min_edge:
                picks.add(cell)
        picks_by_day[today] = sorted(picks)
        chosen = [
            r for r in admitted
            if r["date"] == today and r["cell"] in picks
        ]
        selected.extend(chosen)
        per_day.append({
            "date": today, "days_of_history": index,
            "cells_eligible": sum(
                1 for c in cells.values() if len(c) >= min_cell_n
            ),
            "cells_picked": len(picks),
            **{"today_" + k: v for k, v in _stats(chosen).items()},
        })

    # The whole-sample cell table, for reading only.  In-sample by
    # construction: it is what the rolling rule was choosing from, not a
    # result.
    in_sample: dict[str, list[dict]] = defaultdict(list)
    for row in admitted:
        in_sample[row["cell"]].append(row)
    table = sorted(
        (
            {"cell": cell, **_stats(cell_rows),
             "days": len({r["date"] for r in cell_rows})}
            for cell, cell_rows in in_sample.items()
            if len(cell_rows) >= min_cell_n
        ),
        key=lambda item: -(item["edge_per_dollar"] or -9),
    )

    return {
        "schema": "admission_edge_rolling_v1",
        "since": since or "all",
        "min_cell_n": min_cell_n,
        "min_edge": min_edge,
        "taker_certain_cost": TAKER_CERTAIN_COST,
        "dates": len(days),
        "references": {
            "all_admitted": {
                **_stats(admitted),
                "bootstrap": _bootstrap(admitted, draws=draws, seed=seed),
            },
            "seated": {
                **_stats(seated),
                "bootstrap": _bootstrap(seated, draws=draws, seed=seed),
            },
            "rolling_selected": {
                **_stats(selected),
                "bootstrap": _bootstrap(selected, draws=draws, seed=seed),
            },
        },
        "beats_taker_bar": (
            None if not selected or _stats(selected)["edge_per_dollar"] is None
            else _stats(selected)["edge_per_dollar"] > TAKER_CERTAIN_COST
        ),
        "per_day": per_day,
        "in_sample_cells": table[:40],
        "in_sample_cell_count": len(table),
        "picks_by_day": picks_by_day,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since", default="")
    parser.add_argument("--min-cell-n", type=int, default=12)
    parser.add_argument("--min-edge", type=float, default=0.0)
    parser.add_argument("--draws", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--write-research", action="store_true")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60.0)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA query_only=ON")
        report = measure(
            conn, since=args.since, min_cell_n=args.min_cell_n,
            min_edge=args.min_edge, draws=args.draws, seed=args.seed,
        )
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=1))
    destination = args.out
    if args.write_research and destination is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        destination = OUT_DIR / ("n%d_e%g.json" % (
            args.min_cell_n, args.min_edge
        ))
    if destination is not None:
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print("wrote %s" % destination, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
