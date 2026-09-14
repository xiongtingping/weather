#!/usr/bin/env python3
"""Charter 132 stage C: four fixed arms, walk-forward, one verdict per stage.

The question the design review put before any promotion: does the pooled
valuation predict outcomes better than the market alone, and does that
improvement survive the price the book charges?  Four arms, scored on the
same decisions, the same windows and the same days:

* ``lambda``  -- the claim chain as it served then: the lifted claim times the
  lambda table the journal says was active at the decision, through
  production's own resolver.  Its table's seat-shape vintage is reported.
* ``weather`` -- the served probability vector alone (beta = 1).
* ``market``  -- the whole ladder's normalised book alone (beta = 0).
* ``pool``    -- ``(1 - beta) m + beta p`` with beta fitted on the 28 days
  before the test day, labels knowable before the test day began, and the
  admission read on the action's lower bound across the fold's draws.

Two tests, both walk-forward by event day:

1. **Information**, on every production decision (``valuation_store.
   population``): the log score and multi-bucket Brier of the truth bucket,
   station-days weighted to count once.  The lambda arm has no vector and
   sits this one out.
2. **Strategy**, on every settled candidate window the ledger enumerated,
   refused or not: each arm admits at the charter-130 hurdle (the pool on its
   lower bound, the others on their point belief), buys one share per leg at
   the recorded all-in ask, and is paid by the recorded hit.  Returns are
   per share of cost; intervals are bootstrapped over station-days.

Verdict per stage, written with ``--commit`` for the kernel's ``on`` gate:
``admitted`` only when the pool's log-score gain over the market has a
positive 90% lower bound over at least ``MIN_TEST_DAYS`` test days and
``MIN_TEST_STATION_DAYS`` station-days, AND its per-share return has a
positive 90% lower bound over at least ``MIN_ADMITTED_STATION_DAYS``
admitted station-days.  Anything short of that is ``hold`` with the reasons.

    python3 scripts/valuation_four_arm.py --stage P0 --report docs/research/...
    python3 scripts/valuation_four_arm.py --stage all --commit --as-of ...

Read-only unless ``--commit``; one stage at a time stays inside a slot.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import valuation_store as V  # noqa: E402
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    ECONOMIC_HURDLE_V1,
    effective_edge_lambda_resolved,
)
from bayes_basket_target.basket_kernel.valuation_v1 import (  # noqa: E402
    action_value_v1,
    beta_draw_vectors,
    market_reference_v1,
    valuation_vector_v1,
    window_mass,
)
from bayes_basket_target.effective_edge_store import (  # noqa: E402
    load_effective_edge_tables,
    load_lambda_as_of_strict,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
ARMS = ("lambda", "weather", "market", "pool")
TRAIN_DAYS = 28
MIN_TEST_DAYS = 10
MIN_TEST_STATION_DAYS = 100
MIN_ADMITTED_STATION_DAYS = 30
BOOTSTRAP = 400
SEED = 20260913
BOOK_SCAN_LIMIT = 30


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _cluster_bootstrap(
    groups: Mapping[Any, Sequence[float]] | Mapping[Any, tuple[float, float]],
    stat: Any,
    *,
    reps: int = BOOTSTRAP,
    seed: int = SEED,
) -> tuple[float | None, float | None]:
    keys = list(groups)
    if len(keys) < 5:
        return None, None
    rng = random.Random(seed)
    draws = []
    for _ in range(reps):
        picked = [groups[keys[rng.randrange(len(keys))]] for _ in keys]
        value = stat(picked)
        if value is not None and math.isfinite(value):
            draws.append(value)
    if len(draws) < reps // 2:
        return None, None
    draws.sort()
    return draws[int(0.05 * len(draws))], draws[int(0.95 * len(draws)) - 1]


def fold_fits(
    rows: Sequence[Mapping[str, Any]], days: Sequence[str], *, stage: str,
) -> dict[str, dict[str, Any]]:
    """Per test day: the pool fitted on the 28 days before it, on labels that
    were knowable before it began, re-weighted so each training station-day
    counts once.  Shared by both tests so they judge the same fold."""

    out: dict[str, dict[str, Any]] = {}
    for day in days:
        cutoff = f"{day}T00:00:00Z"
        floor = (date.fromisoformat(day) - timedelta(days=TRAIN_DAYS)).isoformat()
        train = [
            r for r in rows
            if floor < r["date"] < day and str(r["label_known_at"]) <= cutoff
            and str(r["at"]) <= cutoff
        ]
        tally: dict[tuple[str, str], int] = {}
        for r in train:
            tally[(r["station"], r["date"])] = tally.get((r["station"], r["date"]), 0) + 1
        train = [dict(r, weight=1.0 / tally[(r["station"], r["date"])]) for r in train]
        fit = V.fit_valuation(
            None, stage_scope=stage, rows=train,  # type: ignore[arg-type]
            meta={"cutoff_utc": cutoff, "as_of_event_date": day}, draws=100,
        )
        out[day] = {
            "beta": None if fit["accumulating"] else fit["evidence"]["beta"],
            "draws": fit["evidence"]["beta_draws"],
            "train_station_days": fit["station_day_count"],
        }
    return out


# --------------------------------------------------------------------------
# Test 1: information
# --------------------------------------------------------------------------

def information_test(
    rows: Sequence[Mapping[str, Any]], days: Sequence[str],
    fits: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(row["date"], []).append(row)
    per_station_day: dict[tuple[str, str], dict[str, float]] = {}
    fold_betas: dict[str, float | None] = {}
    folds_scored = 0
    for day in days:
        test = by_day.get(day) or []
        if not test:
            continue
        beta = fits[day]["beta"]
        fold_betas[day] = beta
        if beta is None:
            continue
        folds_scored += 1
        for r in test:
            key = (r["station"], r["date"])
            cell = per_station_day.setdefault(key, {
                "w": 0.0, "market": 0.0, "weather": 0.0, "pool": 0.0,
                "brier_market": 0.0, "brier_weather": 0.0, "brier_pool": 0.0,
            })
            vector = valuation_vector_v1(r["p"], r["m"], beta)
            w = r["weight"]
            cell["w"] += w
            cell["market"] += w * math.log(max(V.LOG_FLOOR, r["m_label"]))
            cell["weather"] += w * math.log(max(V.LOG_FLOOR, r["p_label"]))
            cell["pool"] += w * math.log(max(V.LOG_FLOOR, vector[r["label"]]))
            cell["brier_market"] += w * r["brier_m"]
            cell["brier_weather"] += w * r["brier_p"]
            cell["brier_pool"] += w * sum(
                (vector[b] - (1.0 if b == r["label"] else 0.0)) ** 2 for b in r["m"]
            )
    n = len(per_station_day)
    if n == 0:
        return {"station_days": 0, "folds_scored": folds_scored, "fold_betas": fold_betas}

    def mean_of(key: str, cells: Sequence[Mapping[str, float]]) -> float:
        return sum(c[key] for c in cells) / sum(c["w"] for c in cells)

    cells = list(per_station_day.values())
    gain_lo, gain_hi = _cluster_bootstrap(
        dict(per_station_day),
        lambda picked: mean_of("pool", picked) - mean_of("market", picked),
    )
    weather_lo, weather_hi = _cluster_bootstrap(
        dict(per_station_day),
        lambda picked: mean_of("weather", picked) - mean_of("market", picked),
    )
    return {
        "station_days": n,
        "test_days": len({k[1] for k in per_station_day}),
        "folds_scored": folds_scored,
        "fold_betas": fold_betas,
        "log_score": {arm: mean_of(arm, cells) for arm in ("market", "weather", "pool")},
        "brier": {
            arm: mean_of(f"brier_{arm}", cells) for arm in ("market", "weather", "pool")
        },
        "pool_minus_market_log": mean_of("pool", cells) - mean_of("market", cells),
        "pool_minus_market_log_ci": [gain_lo, gain_hi],
        "weather_minus_market_log": mean_of("weather", cells) - mean_of("market", cells),
        "weather_minus_market_log_ci": [weather_lo, weather_hi],
    }


# --------------------------------------------------------------------------
# Test 2: strategy on the enumerated candidate windows
# --------------------------------------------------------------------------

def _matched_book(
    conn: sqlite3.Connection, station: str, day: str, decided_at: str,
    window: Sequence[str], price: float,
) -> tuple[list[Mapping[str, Any]] | None, bool]:
    """The book the enumeration priced this window on.

    Production compiles a checkpoint at its next cycle on the quote that
    cycle holds (the ledger's ``_checkpoint_books``), so the candidate is one
    of the committed books at or after the decision.  The recorded all-in
    price identifies which: the first book whose asks for these legs sum to
    it.  Fees are zero on this venue, so the sum is exact.  When none matches
    within the scan, the first book is used and the row says so.
    """

    first = None
    for (buckets_json,) in conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND observed_at >= ? ORDER BY observed_at ASC LIMIT ?",
        (station, day, decided_at, BOOK_SCAN_LIMIT),
    ):
        try:
            book = json.loads(buckets_json)
        except (TypeError, ValueError):
            continue
        if first is None:
            first = book
        asks = {}
        for row in book:
            levels = [
                float(level["price"]) for level in (row.get("ask_levels") or [])
                if float(level.get("shares") or 0) > 0
            ]
            if levels and row.get("ask_available", True):
                asks[str(row.get("bucket"))] = min(levels)
        if all(bucket in asks for bucket in window) and abs(
            sum(asks[bucket] for bucket in window) - price
        ) < 1e-6:
            return book, True
    return first, False


def strategy_test(
    conn: sqlite3.Connection,
    stage: str,
    rows: Sequence[Mapping[str, Any]],
    days: Sequence[str],
    fits: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(row["date"], []).append(row)
    windows = conn.execute(
        "SELECT w.station_id, w.event_date, w.decision_at_utc, w.buckets_json,"
        "       w.win_probability, w.raw_win_probability, w.all_in_price, w.hit,"
        "       w.settled_at_utc, e.probabilities_json"
        " FROM paper_trade_candidate_window w"
        " JOIN basket_probability_envelope e"
        "   ON e.station_id=w.station_id AND e.event_date=w.event_date"
        "  AND e.stage=w.stage AND e.decision_at_utc=w.decision_at_utc"
        " WHERE w.stage=? AND w.hit IN (0,1) AND w.settled_at_utc IS NOT NULL"
        "   AND w.all_in_price > 0",
        (stage,),
    ).fetchall()
    by_test_day: dict[str, list[tuple]] = {}
    for record in windows:
        by_test_day.setdefault(str(record[1]), []).append(record)
    table_cache: dict[str, Mapping[str, Any]] = {}
    strict_cache: dict[str, str] = {}
    book_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    ledger: dict[str, dict[tuple[str, str], list[tuple[float, float]]]] = {
        arm: {} for arm in ARMS
    }
    brier: dict[str, list[float]] = {arm: [] for arm in ARMS}
    counts = {
        "windows": 0, "no_fold_fit": 0, "market_incomplete": 0,
        "book_unmatched": 0, "lambda_vintage_current": 0,
        "lambda_vintage_other": 0,
    }
    for day in days:
        todays = by_test_day.get(day) or []
        if not todays:
            continue
        beta = fits[day]["beta"]
        draws = fits[day]["draws"]
        for (station, event_day, decided, buckets_json, win, raw_win, price,
             hit, _settled, probs_json) in todays:
            counts["windows"] += 1
            window = [str(b) for b in json.loads(buckets_json)]
            price = float(price)
            hit = float(hit)
            key = (str(station), str(event_day))
            hour = str(decided)[:13]
            if hour not in table_cache:
                table_cache[hour] = load_effective_edge_tables(conn, as_of_utc=str(decided))
                strict_cache[hour] = load_lambda_as_of_strict(
                    conn, stage_scope=stage, as_of_utc=str(decided),
                )[1]
            counts[
                "lambda_vintage_current" if strict_cache[hour] == "current"
                else "lambda_vintage_other"
            ] += 1
            lam, _source = effective_edge_lambda_resolved(
                stage, claim=float(win), price=price, tables=table_cache[hour],
            )
            beliefs: dict[str, float | None] = {
                "lambda": min(1.0, float(win) * float(lam)),
                "weather": float(raw_win) if raw_win is not None else None,
                "market": None, "pool": None,
            }
            admit_pool = None
            book_key = (str(station), str(event_day), str(decided))
            if book_key not in book_cache:
                book, matched = _matched_book(
                    conn, str(station), str(event_day), str(decided), window, price,
                )
                reference = market_reference_v1(book or [])
                book_cache[book_key] = {"reference": reference, "matched": matched}
            cached = book_cache[book_key]
            if not cached["matched"]:
                counts["book_unmatched"] += 1
            reference = cached["reference"]
            if reference["state"] == "complete":
                m = reference["vector"]
                beliefs["market"] = window_mass(m, window)
                if beta is not None:
                    p = {k: float(v) for k, v in json.loads(probs_json).items()}
                    total = sum(p.values())
                    p = {k: v / total for k, v in p.items()} if total > 0 else p
                    vector = valuation_vector_v1(p, m, beta)
                    vectors = beta_draw_vectors(p, m, draws)
                    if vector is not None and vectors:
                        beliefs["pool"] = window_mass(vector, window)
                        bound = action_value_v1(vectors, {b: 1.0 for b in window}, price)
                        admit_pool = (
                            bound is not None
                            and bound["value_lcb"] >= ECONOMIC_HURDLE_V1 * price - 1e-12
                        )
                else:
                    counts["no_fold_fit"] += 1
            else:
                counts["market_incomplete"] += 1
            for arm in ARMS:
                belief = beliefs[arm]
                if belief is None:
                    continue
                brier[arm].append((belief - hit) ** 2)
                admitted = (
                    admit_pool if arm == "pool"
                    else belief >= (1.0 + ECONOMIC_HURDLE_V1) * price - 1e-12
                )
                if admitted:
                    ledger[arm].setdefault(key, []).append((hit, price))
    out: dict[str, Any] = {"counts": counts, "arms": {}}
    for arm in ARMS:
        cells = ledger[arm]
        paid = sum(h for group in cells.values() for h, _c in group)
        cost = sum(c for group in cells.values() for _h, c in group)

        def roi(picked: Sequence[Sequence[tuple[float, float]]]) -> float | None:
            c = sum(x[1] for group in picked for x in group)
            return None if c <= 0 else sum(x[0] for group in picked for x in group) / c - 1.0

        lo, hi = _cluster_bootstrap(cells, roi)
        out["arms"][arm] = {
            "admitted_windows": sum(len(g) for g in cells.values()),
            "admitted_station_days": len(cells),
            "roi_per_share": None if cost <= 0 else paid / cost - 1.0,
            "roi_ci": [lo, hi],
            "window_brier": (sum(brier[arm]) / len(brier[arm])) if brier[arm] else None,
            "scored_windows": len(brier[arm]),
        }
    return out


def verdict(information: Mapping[str, Any], strategy: Mapping[str, Any]) -> tuple[str, list[str]]:
    reasons = []
    if int(information.get("test_days") or 0) < MIN_TEST_DAYS:
        reasons.append(f"test_days<{MIN_TEST_DAYS}")
    if int(information.get("station_days") or 0) < MIN_TEST_STATION_DAYS:
        reasons.append(f"test_station_days<{MIN_TEST_STATION_DAYS}")
    gain_lo = (information.get("pool_minus_market_log_ci") or [None])[0]
    if gain_lo is None or gain_lo <= 0.0:
        reasons.append("pool_log_gain_over_market_lcb<=0")
    pool = (strategy.get("arms") or {}).get("pool") or {}
    if int(pool.get("admitted_station_days") or 0) < MIN_ADMITTED_STATION_DAYS:
        reasons.append(f"admitted_station_days<{MIN_ADMITTED_STATION_DAYS}")
    roi_lo = (pool.get("roi_ci") or [None])[0]
    if roi_lo is None or roi_lo <= 0.0:
        reasons.append("pool_roi_lcb<=0")
    return ("admitted" if not reasons else "hold"), reasons


def run_stage(
    conn: sqlite3.Connection, stage: str, *, cutoff_utc: str, anchored: bool = True,
) -> dict[str, Any]:
    # Anchored by default (2026-09-14): a verdict judged on a weather vector
    # nobody serves any more would admit, or hold, the one that replaced it.
    rows, meta = V.population(
        conn, stage_scope=stage, cutoff_utc=cutoff_utc, window_days=400,
        anchored=anchored,
    )
    days = sorted({r["date"] for r in rows})
    test_days = days[min(len(days), V.MIN_DAYS):]
    fits = fold_fits(rows, test_days, stage=stage)
    information = information_test(rows, test_days, fits)
    strategy = strategy_test(conn, stage, rows, test_days, fits)
    decided, reasons = verdict(information, strategy)
    return {
        "stage": stage,
        "population": {k: v for k, v in meta.items()},
        "first_test_day": test_days[0] if test_days else None,
        "last_test_day": test_days[-1] if test_days else None,
        "information": information,
        "strategy": strategy,
        "verdict": decided,
        "reasons": reasons,
    }


def _fmt(value: Any, spec: str = ".4f") -> str:
    return "—" if value is None else format(value, spec)


def markdown(results: Sequence[Mapping[str, Any]], *, cutoff_utc: str) -> str:
    lines = [
        "# Charter 132 四臂前瞻对照",
        "",
        f"标签截止 `{cutoff_utc}`；每个测试日只用其前 {TRAIN_DAYS} 天、在测试日开始前已可知的标签拟合；"
        f"区间为站日块自助 90%。晋升条件：测试日 ≥ {MIN_TEST_DAYS}、站日 ≥ {MIN_TEST_STATION_DAYS}、"
        f"池化对市场的对数得分增益下界 > 0、池化按股收益下界 > 0 且入座站日 ≥ {MIN_ADMITTED_STATION_DAYS}。",
        "",
    ]
    for result in results:
        info = result["information"]
        strat = result["strategy"]
        lines += [
            f"## {result['stage']} — 裁决 **{result['verdict']}**"
            + ("" if not result["reasons"] else f"（{', '.join(result['reasons'])}）"),
            "",
            f"测试日 {result['first_test_day']} … {result['last_test_day']}；"
            f"信息检验 {info.get('station_days', 0)} 站日 / {info.get('test_days', 0)} 天。",
            "",
            "| 臂 | 对数得分 | 多桶 Brier |",
            "|---|---|---|",
        ]
        for arm in ("market", "weather", "pool"):
            lines.append(
                f"| {arm} | {_fmt((info.get('log_score') or {}).get(arm))} |"
                f" {_fmt((info.get('brier') or {}).get(arm))} |"
            )
        ci = info.get("pool_minus_market_log_ci") or [None, None]
        wci = info.get("weather_minus_market_log_ci") or [None, None]
        lines += [
            "",
            f"池化 − 市场：{_fmt(info.get('pool_minus_market_log'))} nat"
            f" [{_fmt(ci[0])}, {_fmt(ci[1])}]；天气 − 市场：{_fmt(info.get('weather_minus_market_log'))}"
            f" [{_fmt(wci[0])}, {_fmt(wci[1])}]。",
            "",
            "| 臂 | 入座窗 | 入座站日 | 按股收益 | 90% 区间 | 窗口 Brier |",
            "|---|---|---|---|---|---|",
        ]
        for arm in ARMS:
            a = strat["arms"][arm]
            rci = a.get("roi_ci") or [None, None]
            lines.append(
                f"| {arm} | {a['admitted_windows']} | {a['admitted_station_days']} |"
                f" {_fmt(a['roi_per_share'], '+.2%')} |"
                f" [{_fmt(rci[0], '+.2%')}, {_fmt(rci[1], '+.2%')}] |"
                f" {_fmt(a['window_brier'])} |"
            )
        lines += ["", f"计数：`{json.dumps(strat['counts'], ensure_ascii=False)}`", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--stage", choices=sorted(V.STAGE_SCOPES) + ["all"], default="all")
    parser.add_argument("--as-of", default=None, help="label cutoff; required to commit")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--unanchored", action="store_true",
        help="research reading only: include decisions from before the stage's"
             " serving-arithmetic anchor; refused together with --commit",
    )
    args = parser.parse_args()
    if args.commit and args.unanchored:
        print("--unanchored is a research reading and may not be committed")
        return 2
    if args.commit and not args.as_of:
        print("--commit needs --as-of (scripts may not read the clock)")
        return 2
    database = args.db.expanduser().resolve(strict=True)
    cutoff = str(args.as_of or "9999-12-31T00:00:00Z")
    read = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=180.0)
    read.execute("PRAGMA busy_timeout=180000")
    stages = sorted(V.STAGE_SCOPES) if args.stage == "all" else [args.stage]
    results = []
    try:
        for stage in stages:
            result = run_stage(
                read, stage, cutoff_utc=cutoff, anchored=not args.unanchored,
            )
            results.append(result)
            info, pool = result["information"], result["strategy"]["arms"]["pool"]
            print(
                f"{stage}: {result['verdict']} {result['reasons']} | "
                f"pool−market log {_fmt(info.get('pool_minus_market_log'))} "
                f"{info.get('pool_minus_market_log_ci')} | pool ROI "
                f"{_fmt(pool['roi_per_share'], '+.2%')} {pool['roi_ci']} "
                f"on {pool['admitted_station_days']} station-days"
            )
    finally:
        read.close()
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(markdown(results, cutoff_utc=cutoff), encoding="utf-8")
    if args.commit:
        write = sqlite3.connect(str(database), timeout=180.0)
        write.execute("PRAGMA busy_timeout=180000")
        try:
            for result in results:
                V.persist_verdict(
                    write, stage_scope=result["stage"], verdict=result["verdict"],
                    reason=";".join(result["reasons"]) or "all_conditions_met",
                    evidence={
                        "information": {
                            k: v for k, v in result["information"].items()
                            if k != "fold_betas"
                        },
                        "strategy": result["strategy"],
                        "population": result["population"],
                    },
                    computed_at_utc=str(args.as_of),
                    first_test_day=result["first_test_day"],
                    last_test_day=result["last_test_day"],
                )
        finally:
            write.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
