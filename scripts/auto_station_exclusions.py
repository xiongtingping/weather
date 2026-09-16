#!/usr/bin/env python3
"""Bar and release stations automatically, on the two keys the operator named.

Order of judgement, per the 2026-08-30 ruling: 第一看兑现比差 (the fulfilment
ratio, realized over expected), 第二看实得差 (the money gap, realized minus
expected).  Both are read from the live book since 2026-09-16 (user ruling:
the paper ledger is a rule comparator, and this layer reads live fills with
its criterion unchanged) -- the same ``live_book`` rows the board's money
sections read, so a screen does not compute its own version of a metric and
invent its own findings.

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
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_book  # noqa: E402

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
# No ceiling on the bar (operator ruling 2026-09-02, replacing the fifteen
# of 2026-08-30).  The cap was a budget for this layer's judgement, and what
# it actually did on this book was hide the layer's own verdicts: on the day
# it was lifted the layer wanted six more stations (KDAL, KLGA, KORD, RJTT,
# ZBAA, ZSQD) and the cap was silently dropping every one of them, so the
# list a reader saw was not the list the criterion produced.  Barring a
# station costs one skipped market and nothing else, and the operator's own
# switch retracts any row in one click -- so a hidden verdict was the
# expensive half of that trade, not an extra bar.
MAX_BARRED_STATIONS = None
# Below this, realized/expected is a division by noise rather than a ratio.
RATIO_DENOMINATOR_FLOOR_USDC = 3.0


def station_keys(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The two keys per station, off the live book.

    User ruling 2026-09-16: the paper ledger is a rule comparator from that
    day, not a verdict on a station, and this layer keeps its criterion but
    reads live fills.  One settlement is one (station, event day, stage) that
    filled on the exchange and has settled -- the paper settlement row's grain
    -- from ``live_book``, the loader the board's money sections share.

    ``expected`` is the claimed EV of what was actually held: the probability
    the minting decision was bound to for each filled bucket, times the shares
    filled, less the stake.  On paper this was stake x (claim/cost - 1) on the
    position row, and it is the same quantity: the live book prices uneven
    fills on the probability-weighted share count precisely so that identity
    holds.  A settled row whose claim could not be read back contributes its
    realized money and none of the expectation, and is counted.
    """

    agg: dict[str, dict[str, float]] = collections.defaultdict(
        lambda: {"n": 0.0, "hit": 0.0, "claim": 0.0, "expected": 0.0,
                 "realized": 0.0, "claim_missing": 0.0}
    )
    for row in live_book.build(conn)["settlements"]:
        slot = agg[str(row["station_id"])]
        slot["n"] += 1
        slot["hit"] += int(row["hit"] or 0)
        if row["claimed_ev_usdc"] is None:
            slot["claim_missing"] += 1
        else:
            slot["claim"] += float(row["claimed_win_probability"] or 0.0)
            slot["expected"] += float(row["claimed_ev_usdc"])
        slot["realized"] += float(row["pnl_usdc"] or 0.0)

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
            "claim_missing": int(row["claim_missing"]),
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
    # In all_except_barred mode there is nothing to drop: the live batch is
    # the bar's complement, so barring a station removes it from trading in
    # the same act and the two can never overlap.  The drop below exists only
    # for scope=explicit, where a pinned list would otherwise keep naming a
    # station this layer just barred.
    derived_scope = (
        str(intent.get("station_scope") or "all_except_barred") != "explicit"
    )
    allowlist = [] if derived_scope else [
        str(station).strip().upper()
        for station in (intent.get("station_allowlist") or [])
    ]
    operator_ruled = {
        str(row.get("station") or "").strip().upper()
        for row in (doc.get("excluded_stations") or [])
    } | {
        str(row.get("station") or "").strip().upper()
        for row in (doc.get("released_stations") or [])
    } | {
        # The two switches that live in the intent file (2026-09-02).  Both
        # are rulings about a station, so both belong in the set this layer
        # may not touch: a waiver says "this one trades on my say-so" and a
        # hold says "this one waits on my say-so", and a machine that barred
        # either would be overruling the person who just clicked it.  Unlike
        # the allowlist -- which had to come out of this set once it became
        # every unbarred station and silenced the layer entirely -- these are
        # named lists of a handful of stations, so guarding them costs the
        # layer nothing it was otherwise doing.
        str(station).strip().upper()
        for key in ("station_probation_waivers", "station_probation_holds")
        for station in (intent.get(key) or [])
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

    # No trim.  Every station the criterion barred stays barred, and the list
    # is the verdict rather than the fifteen least-bad of it.  The ordering
    # that used to decide who gave way (worst fulfilment ratio first, money
    # gap breaking ties) is gone with it -- there is nothing left to rank.
    room = None
    trimmed: list[str] = []

    # Barring a station means it stops trading, so it leaves the live batch in
    # the same move.  Without this the two lists would overlap and the go-live
    # migration's own disjointness precondition would refuse to run -- the
    # invariant is maintained here rather than left for someone to notice.
    dropped_from_allowlist = sorted(set(allowlist) & set(auto_rows))
    remaining_allowlist = [s for s in allowlist if s not in auto_rows]

    # The watchlist defines itself as "persistently negative in both halves
    # and *not barred*", so a station this layer bars stops meeting its own
    # entry condition and leaves in the same move -- the same reasoning that
    # drops it from the live batch just above.  Without this the two lists
    # state opposite things about the same station: on 2026-09-01 KHOU was
    # barred while still sitting on the watchlist, which is what "under
    # review, no verdict yet" is supposed to exclude.  Barring is a verdict.
    watch = doc.get("watchlist_not_excluded")
    dropped_from_watchlist: list[str] = []
    if isinstance(watch, dict):
        listed = [
            str(station).strip().upper()
            for station in (watch.get("stations") or [])
        ]
        dropped_from_watchlist = sorted(set(listed) & set(auto_rows))
        if dropped_from_watchlist:
            watch["stations"] = [s for s in listed if s not in auto_rows]

    doc["auto_excluded_stations"] = [auto_rows[k] for k in sorted(auto_rows)]
    doc["auto_policy"] = {
        "keys": "第一看 兑现比差 (realized/expected), 第二看 实得差 (realized-expected)",
        "book": "live fills (live_book settlements), since the 2026-09-16 ruling that the paper ledger is a rule comparator",
        "bar_ratio_below": BAR_RATIO,
        "release_ratio_above": RELEASE_RATIO,
        "bar_gap_below_usdc": BAR_GAP_USDC,
        "release_gap_above_usdc": RELEASE_GAP_USDC,
        "minimum_settlements": MIN_SETTLEMENTS,
        "ratio_denominator_floor_usdc": RATIO_DENOMINATOR_FLOOR_USDC,
        "operator_rows_are_never_auto_managed": True,
        "max_barred_stations": MAX_BARRED_STATIONS,
        "cap_applies_to": (
            "nothing -- the cap was lifted by operator ruling 2026-09-02."
            " It bound this layer only, and what it did in practice was hide"
            " the layer's own verdicts: six stations it had barred were being"
            " dropped every pass, so the stored list was not the list the"
            " criterion produced. Barring costs one skipped market and the"
            " board's switch retracts any row in one click."
        ),
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
        "dropped_from_watchlist": dropped_from_watchlist,
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
