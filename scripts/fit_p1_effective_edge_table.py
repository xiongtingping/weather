#!/usr/bin/env python3
"""Fit P1's own effective-edge table, one row of bands per event day.

Charter 68's lambda(a) -- the realized share of a claim as a function of how
far the market agrees with it, a = price / claim -- was fitted on P0 and P0-M
opening seats and deliberately not applied to P1, because P1's edge is the
observation rather than the shape and the P0 bands would gut it: on the
2026-08-19..09-01 book, P1 earns +136% exactly in the band (a < 0.50) where
the P0 table's lambda is 0.14.

So a P1 table has to be fitted on P1's own settled rows, and it has to be
fitted the way charter 68 itself was: on the days before, never on the day
being scored.  This emits one set of bands per event date, each fitted on the
settled P1 rows with an earlier event date, on the same four agreement edges
the kernel uses so the two tables read alike.

lambda is capped at one.  A band where P1 under-claims (realized above
claimed, which the a >= 0.85 band shows at 1.10) does not get to inflate the
claim -- the exemption was always "trust it", never "trust it more than it
trusts itself".  A band with too few rows behind it is left at one, which is
the exemption standing; a table that shrank P1 on three rows would be the
thing the exemption exists to prevent.

    python3 scripts/fit_p1_effective_edge_table.py \
        --settlements /path/to/arm.db --out /path/to/p1_lambda.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

# The kernel's edges, so the two tables read alike.
EDGES = (0.50, 0.70, 0.85, float("inf"))
MIN_BAND_ROWS = 5


def band_index(agreement: float) -> int:
    for index, bound in enumerate(EDGES):
        if agreement < bound:
            return index
    return len(EDGES) - 1


def fit_bands(rows: list[dict]) -> list[list[float]]:
    """realized/claimed per band, capped at one, one where too thin."""

    tally = [[0.0, 0.0, 0] for _ in EDGES]  # claimed, realized, n
    for row in rows:
        claim = float(row["claimed_win_probability"])
        if claim <= 0.0:
            continue
        slot = tally[band_index(float(row["basket_all_in_cost"]) / claim)]
        slot[0] += claim
        slot[1] += float(row["hit"])
        slot[2] += 1
    bands: list[list[float]] = []
    for bound, (claimed, realized, count) in zip(EDGES, tally):
        if count < MIN_BAND_ROWS or claimed <= 0.0:
            lam = 1.0
        else:
            lam = min(1.0, realized / claimed)
        bands.append([bound if bound != float("inf") else 1e9, lam])
    return bands


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settlements", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.settlements}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    from paper_ledger_reader import attach_paper_ledger  # noqa: PLC0415
    attach_paper_ledger(conn)
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT event_date, claimed_win_probability, basket_all_in_cost,"
            " hit FROM paper_trade_settlement WHERE stage='P1'"
            " ORDER BY event_date"
        )
    ]
    days = sorted({str(row["event_date"]) for row in rows})
    by_day: dict[str, list[list[float]]] = {}
    for day in days:
        prior = [row for row in rows if str(row["event_date"]) < day]
        by_day[day] = fit_bands(prior)
    body = {
        "schema": "paper_ledger_p1_effective_edge_table_v1",
        "edges": [bound if bound != float("inf") else 1e9 for bound in EDGES],
        "minimum_band_rows": MIN_BAND_ROWS,
        "fitted_from_rows": len(rows),
        "by_event_date": by_day,
        "final_bands_all_rows": fit_bands(rows),
    }
    Path(args.out).write_text(json.dumps(body, indent=2))
    print(json.dumps({
        "rows": len(rows),
        "days": len(days),
        "final_bands_all_rows": body["final_bands_all_rows"],
        "out": args.out,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
