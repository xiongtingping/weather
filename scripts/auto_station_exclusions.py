#!/usr/bin/env python3
"""Bar and release stations automatically, on the two keys the operator named.

Order of judgement, per the 2026-08-30 ruling: 第一看兑现比差 (the fulfilment
ratio, realized over expected), 第二看实得差 (the money gap, realized minus
expected).  Both are read from the same rows the board reads, joined the same
way, and both were checked to reproduce the board's per-station numbers
exactly for all 45 stations before this was written -- the point of that check
is that a screen computing its own version of a metric invents its own
findings.

Two things this does NOT do, and the reasons are measured rather than
stylistic.

It never touches a station the operator has ruled on.  Auto decisions live in
their own list and the union is what binds, so a hand ruling cannot be undone
by a statistic that moved.  On this book that separation is worth a great
deal: walk-forward over the 332 settlements of 2026-08-15..29, warming on six
days and scoring on the remaining nine, the best of sixteen automatic rules
returned +2.4 points at p=0.356 against excluding the same number of stations
at random -- nothing -- while the operator's own ten-station list, held fixed
across the same test, returned +15.6 points at p=0.001.

And the fulfilment ratio is only consulted where its denominator can carry it.
Expected P&L near zero makes the ratio explode: KAUS 2026-08-30 sat at -11.33,
the worst in the fleet, on an expected value of 0.72 USDC -- and the operator
released it that day, correctly.  Below the guard the ratio says nothing and
the second key decides, which is what "第二看" is for.
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
CONFIG = ROOT / "config" / "trading" / "live_station_exclusions_v1.json"
INTENT = ROOT / "config" / "trading" / "live_intent_v1.json"

# Hysteresis: the release bar sits above the bar-entry bar on both keys, so a
# station cannot oscillate on a metric wandering across one line.  EPWA and
# LTAC went out and back inside a single day under hand rulings; a machine
# doing that every render would be worse.
BAR_RATIO = 0.50
RELEASE_RATIO = 0.80
BAR_GAP_USDC = -15.0
RELEASE_GAP_USDC = -5.0
MIN_SETTLEMENTS = 5
# Operator ruling 2026-08-30: at most fifteen stations barred at once.  The
# cap binds the automatic layer only -- operator rows are never trimmed to
# make room, because a cap is a budget for the machine's judgement and not a
# licence to overrule a person's.  When the cap is already spent by hand
# rulings the layer simply adds nothing and says so.
MAX_BARRED_STATIONS = 15
# Below this, realized/expected is a division by noise rather than a ratio.
RATIO_DENOMINATOR_FLOOR_USDC = 3.0


def station_keys(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The two keys per station, from the board's own rows and joins.

    ``expected`` is production's EV -- stake x (claim/cost - 1) on the
    *position* row, not the settlement's chain-level claim and cost.  Using
    the settlement columns instead reproduced 27 of 45 stations wrongly, some
    by more than 10 USDC, which is exactly the class of error that turns a
    screen into a fiction.
    """

    agg: dict[str, dict[str, float]] = collections.defaultdict(
        lambda: {"n": 0.0, "hit": 0.0, "claim": 0.0, "expected": 0.0, "realized": 0.0}
    )
    for station, hit, win, cost, stake, pnl in conn.execute(
        "SELECT p.station_id, s.hit, p.win_probability, p.all_in_cost,"
        " p.stake_usdc, s.pnl_usdc"
        " FROM paper_trade_settlement s"
        " JOIN paper_trade_position p USING(position_id)"
    ):
        row = agg[str(station)]
        row["n"] += 1
        row["hit"] += int(hit or 0)
        row["claim"] += float(win or 0.0)
        if cost and float(cost) > 0.0:
            row["expected"] += float(stake or 0.0) * (
                float(win or 0.0) / float(cost) - 1.0
            )
        row["realized"] += float(pnl or 0.0)

    out: dict[str, dict[str, Any]] = {}
    for station, row in agg.items():
        expected = row["expected"]
        ratio = (
            row["realized"] / expected
            if abs(expected) >= RATIO_DENOMINATOR_FLOOR_USDC else None
        )
        out[station] = {
            "settlements": int(row["n"]),
            "fulfilment_ratio": ratio,
            "realized_gap_usdc": row["realized"] - expected,
            "expected_usdc": expected,
            "realized_usdc": row["realized"],
            "claim_ratio": (
                (row["hit"] / row["n"]) / (row["claim"] / row["n"])
                if row["n"] and row["claim"] else None
            ),
        }
    return out


def decide(
    keys: dict[str, dict[str, Any]],
    *,
    currently_auto_barred: set[str],
    operator_ruled: set[str],
) -> dict[str, Any]:
    """Bar, release, or leave alone -- per station, with the operator on top."""

    bar: list[dict[str, Any]] = []
    release: list[str] = []
    for station in sorted(keys):
        if station in operator_ruled:
            continue
        row = keys[station]
        if row["settlements"] < MIN_SETTLEMENTS:
            continue
        ratio = row["fulfilment_ratio"]
        gap = row["realized_gap_usdc"]
        # A station that is up on the book is never barred for under-delivering.
        # Both keys measure a shortfall against what was claimed, and neither
        # can see that the shortfall was still a profit -- EFHK is the case.
        if station not in currently_auto_barred and row["realized_usdc"] > 0.0:
            continue
        if station in currently_auto_barred:
            # Release needs both keys clear of the higher bar.  A ratio that
            # cannot be computed cannot clear anything, so an unreadable
            # station stays barred until its expected value grows enough to
            # say something.
            if ratio is not None and ratio > RELEASE_RATIO and gap > RELEASE_GAP_USDC:
                release.append(station)
            continue
        if ratio is not None:
            if ratio < BAR_RATIO:
                bar.append({"station": station, "on": "fulfilment_ratio",
                            "value": round(ratio, 3)})
            continue
        # 第二看: the ratio is unreadable here, so the money gap decides.
        if gap < BAR_GAP_USDC:
            bar.append({"station": station, "on": "realized_gap_usdc",
                        "value": round(gap, 2)})
    return {"bar": bar, "release": release}


def run(*, apply_changes: bool) -> dict[str, Any]:
    doc = json.loads(CONFIG.read_text(encoding="utf-8"))
    # Guarded by the operator's explicit rulings only -- the bar list and the
    # release list.  The allowlist was in this set for one day and had to come
    # out: once it became "everything not barred" (30 of 45 on 2026-08-30) the
    # guard covered the entire fleet and this layer could never act again, so
    # the automatic bar the operator asked for was silently doing nothing.
    #
    # What the allowlist was really protecting against is handled where it
    # belongs, in the criterion.  The layer moved to bar EFHK -- profitable at
    # +4.02, on the live batch -- purely because it earned less than the 11.17
    # it expected, so the fulfilment ratio read 0.36.  The ratio cannot tell
    # "made money, less than it promised" from "lost money".  A profitability
    # floor says it directly: a station that is up on the book does not get
    # barred for under-delivering, and EFHK is then safe on its own merits
    # rather than by a guard that happened to cover it.
    intent = json.loads(INTENT.read_text(encoding="utf-8"))
    allowlist = [
        str(station).strip().upper()
        for station in (intent.get("station_allowlist") or [])
    ]
    operator_ruled = {
        str(row.get("station") or "").strip().upper()
        for row in (doc.get("excluded_stations") or [])
    } | {
        str(row.get("station") or "").strip().upper()
        for row in (doc.get("released_stations") or [])
    }
    auto_rows = {
        str(row.get("station") or "").strip().upper(): row
        for row in (doc.get("auto_excluded_stations") or [])
    }
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        keys = station_keys(conn)
    finally:
        conn.close()

    operator_ruled_barred = {
        str(row.get("station") or "").strip().upper()
        for row in (doc.get("excluded_stations") or [])
    }
    verdict = decide(
        keys,
        currently_auto_barred=set(auto_rows),
        operator_ruled=operator_ruled,
    )
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    for row in verdict["bar"]:
        station = row["station"]
        auto_rows[station] = {
            "station": station,
            "barred_on": row["on"],
            "value": row["value"],
            "settlements": keys[station]["settlements"],
            "auto_barred_utc": stamp,
        }
    for station in verdict["release"]:
        auto_rows.pop(station, None)

    # The cap, applied in the order the operator named: worst fulfilment
    # ratio first, money gap breaking ties.  A station trimmed here is not
    # cleared of anything -- it is the least bad of the ones the layer wanted,
    # and it is reported so the trim is visible rather than inferred from a
    # list that quietly stopped growing.
    room = max(0, MAX_BARRED_STATIONS - len(operator_ruled_barred))
    trimmed: list[str] = []
    if len(auto_rows) > room:
        def severity(station: str) -> tuple[float, float]:
            row = keys.get(station) or {}
            ratio = row.get("fulfilment_ratio")
            return (
                ratio if ratio is not None else 1e9,
                row.get("realized_gap_usdc", 0.0),
            )
        keep = sorted(auto_rows, key=severity)[:room]
        trimmed = sorted(set(auto_rows) - set(keep))
        auto_rows = {station: auto_rows[station] for station in sorted(keep)}

    # Barring a station means it stops trading, so it leaves the live batch in
    # the same move.  Without this the two lists would overlap and the go-live
    # migration's own disjointness precondition would refuse to run -- the
    # invariant is maintained here rather than left for someone to notice.
    dropped_from_allowlist = sorted(set(allowlist) & set(auto_rows))
    remaining_allowlist = [s for s in allowlist if s not in auto_rows]

    doc["auto_excluded_stations"] = [auto_rows[k] for k in sorted(auto_rows)]
    doc["auto_policy"] = {
        "keys": "第一看 兑现比差 (realized/expected), 第二看 实得差 (realized-expected)",
        "bar_ratio_below": BAR_RATIO,
        "release_ratio_above": RELEASE_RATIO,
        "bar_gap_below_usdc": BAR_GAP_USDC,
        "release_gap_above_usdc": RELEASE_GAP_USDC,
        "minimum_settlements": MIN_SETTLEMENTS,
        "ratio_denominator_floor_usdc": RATIO_DENOMINATOR_FLOOR_USDC,
        "operator_rows_are_never_auto_managed": True,
        "max_barred_stations": MAX_BARRED_STATIONS,
        "cap_applies_to": "the automatic layer only; operator rows are never trimmed",
        "measured": (
            "Walk-forward on 332 settlements over 2026-08-15..29, warming on six"
            " days and scoring on nine: the best of sixteen automatic rules"
            " returned +2.4 points at p=0.356 against excluding the same number"
            " of stations at random. The operator's own ten-station list held"
            " fixed returned +15.6 points at p=0.001. This layer runs because it"
            " was asked for and because barring a station only costs a skipped"
            " market -- not because it was shown to pay."
        ),
        "updated_utc": stamp,
    }
    if apply_changes:
        CONFIG.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if dropped_from_allowlist:
            intent["station_allowlist"] = remaining_allowlist
            intent["allowlist_last_auto_drop_utc"] = stamp
            INTENT.write_text(
                json.dumps(intent, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return {
        "dropped_from_allowlist": dropped_from_allowlist,
        "allowlist_size": len(remaining_allowlist),
        "cap": MAX_BARRED_STATIONS,
        "operator_barred": len(operator_ruled_barred),
        "auto_room": room,
        "trimmed_by_cap": trimmed,
        "barred_now": verdict["bar"],
        "released_now": verdict["release"],
        "auto_barred_total": sorted(auto_rows),
        "operator_ruled": sorted(operator_ruled),
        "written": apply_changes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the decision back; default is a dry run")
    args = parser.parse_args()
    print(json.dumps(run(apply_changes=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
