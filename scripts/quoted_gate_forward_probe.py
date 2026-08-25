#!/usr/bin/env python3
"""Forward-validate the unquoted-window refusal, and promote it when it earns it.

The sign is ``quoted``: at least CONSOLIDATION_QUOTED_FRACTION of the mass in
the ideal centre+-1 window sits on buckets asked at or above the leg floor --
did the market ever make a book on what we want to buy.  The candidate rule is
its refusal: do not seat where it is dark.

Why this needs a forward lane rather than the sweep that proposed it.  The
retrospective split is stark -- on 2026-08-24, over the honest book with the
certainty gate in force, 94 quoted chains returned -8.5% at a 26.6% hit rate
while 9 unquoted ones returned -100.0% and did not hit once -- and the
mechanism is legible: the two strata seat near-identical windows (mean 2.21
vs 2.11 buckets) but the unquoted baskets cost half as much (0.115 vs 0.245),
so the cheap seat is a seat in buckets the book declined to price.  But nine
chains is not a rule.  All-miss at the quoted stratum's hit rate carries
p ~= 0.066, and the same sign measured the other way round on 2026-08-21
(-39.7% quoted against +15.6% unquoted) before that reading was voided by the
``touch`` fill assumption and the pre-charter-50 chains.  A sign that has
already inverted once under a measurement error gets held to its forward
sample, not to the one that proposed it.

So this probe judges ONLY event dates on or after ``start_event_date``, which
is stamped once and never moved backwards.  The chains that proposed the rule
are the training set and are excluded by construction; re-reading them would
be marking one's own exam.

Promotion is data, not a code change: the verdict lands in an activation file
that the ledger reads.  Demotion is the same file.  ``--freeze`` stops the
lane without deleting its history.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
ACTIVATION = ROOT / "data" / "quoted_gate_activation.json"
SCHEMA = "quoted_gate_forward_probe_v1"

# The bar.  Deliberately three separate things, because each one alone has a
# way of being cleared by an accident this book has already produced.
#
# ``MIN_UNQUOTED_CHAINS``: the sample that matters is the refused stratum, not
# the book.  At 20 chains an all-miss run reaches p ~= 0.003 against a 25% hit
# rate, which is a fact rather than a streak.
MIN_UNQUOTED_CHAINS = 20
# ``MAX_P_VALUE``: one-sided, against the quoted stratum's own hit rate as the
# null.  Not against zero -- the claim is that unquoted seats hit LESS, and a
# stratum that merely hits rarely proves nothing on its own.
MAX_P_VALUE = 0.05
# ``MIN_ROI_GAP_AFTER_TRIM``: the ROI gap must survive dropping the best
# chain from each stratum, three times over.  This book has produced a
# +47.5% headline that was one chain, twice; a gap that dies on a trim is
# that same result wearing a different hat.
MIN_ROI_GAP_AFTER_TRIM = 0.0
TRIM_DEPTH = 3


def _binom_tail_leq(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), exactly, without scipy."""

    if n <= 0:
        return 1.0
    p = min(max(p, 0.0), 1.0)
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    # pmf(0) then the standard forward recurrence; n is small here.
    pmf = (1.0 - p) ** n
    total = pmf
    for i in range(0, min(k, n)):
        pmf *= (n - i) / (i + 1) * (p / (1.0 - p))
        total += pmf
    return min(total, 1.0)


def _roi(rows: list[dict[str, Any]]) -> float | None:
    staked = sum(float(r["stake_usdc"] or 0.0) for r in rows)
    if staked <= 0:
        return None
    return sum(float(r["pnl_usdc"] or 0.0) for r in rows) / staked


def _trimmed_roi(rows: list[dict[str, Any]], drop: int) -> float | None:
    kept = sorted(
        rows, key=lambda r: float(r["pnl_usdc"] or 0.0), reverse=True
    )[drop:]
    return _roi(kept) if kept else None


def collect(conn: sqlite3.Connection, *, start_event_date: str) -> dict[str, Any]:
    """Split settled chains at or after the start date by the sign."""

    rows = conn.execute(
        "SELECT p.station_id, p.event_date,"
        " json_extract(p.consolidation_json,'$.quoted') AS quoted,"
        " s.hit, s.stake_usdc, s.pnl_usdc"
        " FROM paper_trade_position p"
        " JOIN paper_trade_settlement s"
        "   ON s.station_id=p.station_id AND s.event_date=p.event_date"
        " WHERE p.stage='P0' AND p.event_date >= ?"
        "   AND json_valid(p.consolidation_json)",
        (start_event_date,),
    ).fetchall()
    quoted: list[dict[str, Any]] = []
    unquoted: list[dict[str, Any]] = []
    for station, day, is_quoted, hit, stake, pnl in rows:
        record = {
            "station_id": station, "event_date": day,
            "hit": int(hit or 0), "stake_usdc": stake, "pnl_usdc": pnl,
        }
        (quoted if is_quoted else unquoted).append(record)
    return {"quoted": quoted, "unquoted": unquoted}


def evaluate(split: dict[str, Any]) -> dict[str, Any]:
    """Decide, and record which clause decided it."""

    quoted, unquoted = split["quoted"], split["unquoted"]
    n_unquoted = len(unquoted)
    hits_unquoted = sum(r["hit"] for r in unquoted)
    quoted_hit_rate = (
        sum(r["hit"] for r in quoted) / len(quoted) if quoted else None
    )
    verdict: dict[str, Any] = {
        "schema": SCHEMA,
        "quoted_chains": len(quoted),
        "unquoted_chains": n_unquoted,
        "quoted_hit_rate": quoted_hit_rate,
        "unquoted_hit_rate": (
            hits_unquoted / n_unquoted if n_unquoted else None
        ),
        "quoted_roi": _roi(quoted),
        "unquoted_roi": _roi(unquoted),
        "required_unquoted_chains": MIN_UNQUOTED_CHAINS,
        "max_p_value": MAX_P_VALUE,
        "trim_depth": TRIM_DEPTH,
    }
    if n_unquoted < MIN_UNQUOTED_CHAINS or quoted_hit_rate is None:
        verdict["path"] = "ACCUMULATING"
        verdict["pass"] = False
        return verdict

    p_value = _binom_tail_leq(hits_unquoted, n_unquoted, quoted_hit_rate)
    verdict["p_value"] = p_value

    # The gap, at every trim depth.  Reported in full rather than as a
    # boolean: a lane that fails at depth 3 but clears 0-2 is a different
    # story from one that never clears at all, and the file is the only
    # place a later review can read that from.
    gaps = []
    for drop in range(TRIM_DEPTH + 1):
        q_roi = _trimmed_roi(quoted, drop)
        u_roi = _trimmed_roi(unquoted, drop)
        gaps.append({
            "drop": drop,
            "quoted_roi": q_roi,
            "unquoted_roi": u_roi,
            "gap": None if q_roi is None or u_roi is None else q_roi - u_roi,
        })
    verdict["trim_ladder"] = gaps
    survives_trim = all(
        g["gap"] is not None and g["gap"] > MIN_ROI_GAP_AFTER_TRIM
        for g in gaps
    )
    verdict["survives_trim"] = survives_trim

    if p_value < MAX_P_VALUE and survives_trim:
        verdict["path"] = "PROMOTED"
        verdict["pass"] = True
    else:
        verdict["path"] = (
            "REJECTED_TRIM" if p_value < MAX_P_VALUE else "REJECTED_P"
        )
        verdict["pass"] = False
    return verdict


def load_activation() -> dict[str, Any]:
    if not ACTIVATION.exists():
        return {}
    try:
        return json.loads(ACTIVATION.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--start-event-date",
        help=(
            "first event date the forward sample admits.  Stamped on the"
            " first run and reused after; it never moves backwards, because"
            " a start date that slides is a start date that can be slid"
            " until the answer is the one wanted."
        ),
    )
    parser.add_argument(
        "--freeze", action="store_true",
        help="stop the lane where it stands without deleting its history",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    prior = load_activation()

    if args.freeze:
        prior["frozen"] = True
        prior["frozen_at_utc"] = now
        ACTIVATION.write_text(
            json.dumps(prior, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        print(json.dumps({"schema": SCHEMA, "state": "FROZEN"},
                         ensure_ascii=False))
        return 0

    if prior.get("frozen"):
        print(json.dumps(
            {"schema": SCHEMA, "state": "FROZEN", "note": "run --freeze off"},
            ensure_ascii=False))
        return 0

    start = (
        prior.get("start_event_date")
        or args.start_event_date
        or now[:10]
    )
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        split = collect(conn, start_event_date=start)
    finally:
        conn.close()
    verdict = evaluate(split)
    verdict["start_event_date"] = start
    verdict["evaluated_at_utc"] = now

    # Once promoted, stay promoted: a rule that flickers with each new
    # settlement is worse than either state, and the demotion path is a
    # human reading this file, not a threshold oscillating around itself.
    already = bool(prior.get("active"))
    verdict["active"] = already or bool(verdict["pass"])
    if verdict["active"] and not already:
        verdict["promoted_at_utc"] = now
    elif already:
        verdict["promoted_at_utc"] = prior.get("promoted_at_utc")
    verdict["start_event_date"] = start

    ACTIVATION.write_text(
        json.dumps(verdict, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({
        "schema": SCHEMA,
        "path": verdict["path"],
        "active": verdict["active"],
        "unquoted_chains": verdict["unquoted_chains"],
        "required": MIN_UNQUOTED_CHAINS,
        "p_value": verdict.get("p_value"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
