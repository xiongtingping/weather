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
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# The ledger owns the rule set; the page only reports it.  Importing the
# label rather than restating it keeps the two from drifting apart, which
# is the whole point of naming a rule at all.
from paper_trading_ledger import SELECTION_RULE_LABEL  # noqa: E402

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
# The registry's city_label is empty for every station, so the readable name
# lives here.  The ICAO code stays the key everywhere -- it is the stable
# identifier -- and the label is only ever for the page.
STATION_LABELS = {
    "CYYZ": "多伦多", "EDDM": "慕尼黑", "EFHK": "赫尔辛基",
    "EGLC": "伦敦", "EHAM": "阿姆斯特丹", "EPWA": "华沙",
    "FACT": "开普敦", "KATL": "亚特兰大", "KAUS": "奥斯汀",
    "KBKF": "丹佛", "KDAL": "达拉斯", "KHOU": "休斯敦",
    "KLAX": "洛杉矶", "KLGA": "纽约", "KMIA": "迈阿密",
    "KORD": "芝加哥", "KSEA": "西雅图", "KSFO": "旧金山",
    "LEMD": "马德里", "LFPB": "巴黎", "LIMC": "米兰",
    "LTAC": "安卡拉", "MMMX": "墨西哥城", "MPMG": "巴拿马城",
    "NZWN": "惠灵顿", "OEJN": "吉达", "OPKC": "卡拉奇",
    "RCSS": "台北", "RJTT": "东京", "RKPK": "釜山",
    "RKSI": "首尔", "RPLL": "马尼拉", "SAEZ": "布宜诺斯艾利斯",
    "SBGR": "圣保罗", "VILK": "勒克瑙", "WMKK": "吉隆坡",
    "WSSS": "新加坡", "ZBAA": "北京", "ZGGG": "广州",
    "ZGSZ": "深圳", "ZHHH": "武汉", "ZSPD": "上海",
    "ZSQD": "青岛", "ZUCK": "重庆", "ZUUU": "成都",
}


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
        " p.prices_json, p.window_size, p.selection_rule_label,"
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
        # What the seat covers, what it cost, and what that pair is worth.
        # Coverage is the calibrated probability summed over the seated
        # buckets; the cost is what the basket was actually filled at.
        #
        # It used to be the sum of the seated buckets' *top-of-book* offers,
        # which is a different number the moment a clip outruns the level
        # quoting it -- ZGSZ 2026-08-15 showed 0.004 here beside a P&L
        # computed on the 0.01771 it really paid, so the page reported a
        # return no price on it could produce.  One number, the paid one.
        seat_top_of_book_total = sum(
            max(0.0, prices.get(b, 0.0)) for b in json.loads(r[4])
        )
        seat_ask_total = cost
        # EV per USDC staked.  A basket costing C pays 1 when the truth lands
        # inside it, so a unit of stake buys 1/C of payout and returns
        # p/C - 1 in expectation.  This is the same quantity the edge (p - C)
        # describes, restated per unit of capital, which is what makes two
        # seats at different price levels comparable.
        ev_per_usdc = (win / cost - 1.0) if cost > 0 else None
        stake = float(r[9])
        out.append({
            "seat_ask_total": seat_ask_total,
            "seat_top_of_book_total": seat_top_of_book_total,
            "ev_per_usdc": ev_per_usdc,
            "ev_usdc": (
                None if ev_per_usdc is None else stake * ev_per_usdc
            ),
            "station_id": r[0],
            "station_label": STATION_LABELS.get(str(r[0]), ""),
            "event_date": r[1],
            "stage": r[2],
            "decision_at_utc": r[3],
            "window": json.loads(r[4]),
            "window_size": int(r[12]),
            # Which rule set chose this basket.  Two days on the page can be
            # compiled under different rules -- the ledger re-derives a scope
            # when the fingerprint moves, but only inside the window it can
            # reach -- so a basket has to say which one it is rather than
            # letting the reader assume they are comparable.
            "selection_rule_label": str(r[13] or ""),
            "centre_bucket": r[5],
            "win_probability": float(r[6]),
            "all_in_cost": cost,
            "edge": float(r[8]),
            "odds": (1.0 - cost) / cost if cost > 0 else None,
            "stake_usdc": float(r[9]),
            "lean_buckets": lean,
            "side": side,
            "dust_offer": cost < DUST_COST_FLOOR,
            "settled": r[14] is not None,
            "hit": None if r[14] is None else int(r[14]),
            "settled_tmax_c": r[15],
            "settled_bucket": r[16],
            "pnl_usdc": r[17],
            "bucket_distance": r[18],
            "center_error_c": r[19],
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
    # Every registered station on every event day it has evidence for, not
    # just the latest.  A station missing from the ledger on a day is the
    # more interesting row -- the objective looked and did not sit, or never
    # got a priced book -- so absence is rendered, not skipped.
    registered = [
        str(row[0])
        for row in conn.execute(
            "SELECT station_id FROM calibration_station_registry"
            " ORDER BY station_id"
        )
    ]
    seated = {
        (r["station_id"], r["event_date"], r["stage"]): r for r in rows
    }
    taf_tx: dict[tuple[str, str], float] = {}
    for station, day, value in conn.execute(
        "SELECT station_id, event_date, tx_c"
        " FROM calibration_forward_taf_capture"
        " WHERE event_date >= ? AND tx_available=1 AND tx_c IS NOT NULL"
        " GROUP BY station_id, event_date HAVING MAX(issued_at_utc)",
        (STATS_START_EVENT_DATE,),
    ):
        taf_tx[(str(station), str(day))] = float(value)
    # Why the ledger did not seat, in its own words rather than the page's
    # guess.  Absence used to be read off two existence checks, so every scope
    # with a probability and a book was reported as "the objective looked and
    # did not sit" -- true of one refusal and wrong about the four others,
    # including the thirteen 2026-08-17 stations whose every admissible window
    # priced above the model and the three whose book never reached the table.
    refusals: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
        for station, day, stage, reason, detail, cycles in conn.execute(
            "SELECT station_id, event_date, stage, reason, detail,"
            " cycles_examined FROM paper_trade_refusal WHERE event_date >= ?",
            (STATS_START_EVENT_DATE,),
        ):
            refusals[(str(station), str(day), str(stage))] = {
                "reason": str(reason),
                "detail": str(detail or ""),
                "cycles_examined": int(cycles or 0),
            }
    except sqlite3.DatabaseError:
        # The table arrives with the ledger's next sweep; until then the page
        # falls back to the existence checks below.
        refusals = {}
    # P1 re-solves as observations arrive, so its rung moves during the day.
    p1_history: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for station, day, decided, envelope_json in conn.execute(
        "SELECT station_id, event_date, decision_at_utc, envelope_json"
        " FROM basket_probability_bridge_history"
        " WHERE stage='P1' AND event_date >= ?"
        " ORDER BY station_id, event_date, decision_at_utc",
        (STATS_START_EVENT_DATE,),
    ):
        lineage = (json.loads(envelope_json) or {}).get("lineage") or {}
        p1_history[(str(station), str(day))].append({
            "decision_at_utc": str(decided),
            "centre_bucket": lineage.get("model_center_bucket"),
            "centre_c": lineage.get("model_center_c"),
        })
    # An event day is any day the fleet has evidence for; on such a day every
    # registered station gets a row, whether or not it has evidence of its
    # own.  Keying the roster off the stations with evidence made the two
    # emptiest cases -- no probability computed, no book ever captured --
    # vanish from the page instead of reading as the gap they are: 2026-08-17
    # rendered 43 stations under a heading that promised all 45.  Pairs with
    # evidence are unioned in, so a station absent from the registry still
    # shows up rather than being silently dropped.
    evidence_days = (
        {(r["station_id"], r["event_date"]) for r in rows}
        | {(station, day) for station, day, _ in refusals}
        | {
            (str(station), str(day)) for station, day in conn.execute(
                "SELECT DISTINCT station_id, event_date FROM"
                " basket_probability_bridge_history WHERE event_date >= ?",
                (STATS_START_EVENT_DATE,),
            )
        }
    )
    scope_days: set[tuple[str, str]] = evidence_days | {
        (station, day)
        for day in {d for _, d in evidence_days}
        for station in registered
    }
    roster = []
    for station, day in sorted(
        scope_days, key=lambda kv: (kv[1], kv[0]), reverse=True
    ):
        entry: dict[str, Any] = {
            "station_id": station,
            "station_label": STATION_LABELS.get(station, ""),
            "event_date": day,
            "taf_tx_c": taf_tx.get((station, day)),
            "p1_history": p1_history.get((station, day), [])[-12:],
            "stages": {},
        }
        entry["refusals"] = {
            stage: refusals.get((station, day, stage))
            for stage in ("P0", "P0-M", "P1")
        }
        for stage in ("P0", "P0-M", "P1"):
            row = seated.get((station, day, stage))
            entry["stages"][stage] = (
                None
                if row is None
                else {
                    "window": row["window"],
                    "window_size": row["window_size"],
                    "selection_rule_label": row.get(
                        "selection_rule_label", ""
                    ),
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
                    "settled_tmax_c": row["settled_tmax_c"],
                }
            )
        entry["seated_stage_count"] = sum(
            1 for v in entry["stages"].values() if v is not None
        )
        settled = [
            v for v in entry["stages"].values()
            if v is not None and v.get("hit") is not None
        ]
        entry["settled_tmax_c"] = next(
            (v["settled_tmax_c"] for v in settled), None
        )
        entry["realized_usdc"] = (
            sum(float(v["pnl_usdc"] or 0.0) for v in settled)
            if settled else None
        )
        if entry["seated_stage_count"] == 0:
            recorded = entry["refusals"].get("P0") or next(
                (v for v in entry["refusals"].values() if v), None
            )
            if recorded is not None:
                entry["absent_reason"] = recorded["reason"]
                entry["absent_detail"] = recorded["detail"]
            else:
                has_probability = conn.execute(
                    "SELECT 1 FROM basket_probability_bridge_history"
                    " WHERE station_id=? AND event_date=? LIMIT 1",
                    (station, day),
                ).fetchone() is not None
                has_book = conn.execute(
                    "SELECT 1 FROM basket_official_ws_snapshot"
                    " WHERE station_id=? AND event_date=? LIMIT 1",
                    (station, day),
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
        "window_size_mean": _mean(
            [float(r["window_size"]) for r in credible_rows]
        ),
        # The all-in ask already carries the fee, so the basket's ask total
        # and its cost are the same number under two names.  Report it once.
        "seat_ask_total_mean": _mean(
            [r["seat_ask_total"] for r in credible_rows]
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
            "station_label": STATION_LABELS.get(station, ""),
            "position_count": len(subset),
            "settled_count": len(settled),
            "window_size_mean": _mean(
                [float(r["window_size"]) for r in subset]
            ),
            "model_coverage_mean": _mean(
                [r["win_probability"] for r in subset]
            ),
            "seat_ask_total_mean": _mean(
                [r["seat_ask_total"] for r in subset]
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
    # ``station_stats`` is deliberately not attached to the roster rows.  A
    # roster row is a station-day and these averages span the whole window,
    # so carrying them there put a number on every date that belonged to
    # none of them -- ZUUU's 08-16 fill was reported as its 08-17 ask total
    # on a day with no book.  The page reads the two collections apart.
    days = sorted({r["event_date"] for r in rows}, reverse=True)
    return {
        "schema": "selection_market_dashboard_v1",
        # Stated once so the page can flag anything compiled under a
        # different rule instead of repeating the same string on every cell.
        "current_selection_rule_label": SELECTION_RULE_LABEL,
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
        "roster_event_dates": sorted({e["event_date"] for e in roster}, reverse=True),
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
