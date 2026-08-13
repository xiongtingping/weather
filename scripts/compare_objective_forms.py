"""Re-rank each retained plan's candidate set under both objectives, and diff.

Read-only.  A live A/B is impossible while the probability envelopes are
stale -- every scope refuses to compile -- so this reconstructs the candidate
grid from what the retained plan already records (per bucket: model
probability, limit price, depth cap, clip quantum) and re-runs the exact
kernel argmax over it twice.  Costs reconstruct exactly: the plans' own
``c_order`` equals ``q_order * r_limit`` to the cent.

Reported per station-day: ``sigma``, the standard deviation of the
settled-outcome PnL curve, and ``win``, the probability mass whose settled
outcome pays.  A lottery basket has a large sigma and a small win; the switch
is meant to trade the first down and the second up without giving away the
mean.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    CURVE_OBJECTIVE_FORM,
    DEFAULT_KELLY_FRACTION,
    VALUE_OBJECTIVE_FORM,
    ValueWeightsV2,
    solve_target_inventory_v2,
)
from bayes_basket_target.contracts import ContractError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data/bayes_basket_target_ssot.db"


def _candidates(rows, clip_grid, budget):
    """Rebuild the clip-grid enumeration the plan was solved over."""

    ordered = [r for r in rows if r.get("eligible")]
    if not ordered:
        return [], []
    per_bucket = []
    for row in ordered:
        limit = float(row["r_limit"])
        cap = float(row.get("depth_shares_cap") or 0.0)
        affordable = [0.0]
        for clip in clip_grid:
            clip = float(clip)
            if clip > budget or limit <= 0.0:
                continue
            if clip / limit > cap:
                continue
            affordable.append(clip)
        per_bucket.append(sorted(set(affordable)))
    candidates = []
    for clips in product(*per_bucket):
        if sum(clips) > budget:
            continue
        if any(c <= 0.0 for c in clips) and any(c > 0.0 for c in clips):
            # Basket activation is atomic: every support bucket gets a
            # positive target, or none does.
            continue
        shares = {}
        costs = {}
        for row, clip in zip(ordered, clips):
            bucket = str(row["bucket"])
            limit = float(row["r_limit"])
            shares[bucket] = clip / limit if clip > 0.0 else 0.0
            costs[bucket] = clip
        candidates.append({
            "candidate_id": "c" + "-".join(f"{c:.4f}" for c in clips),
            "shares_by_bucket": shares,
            "cost_by_bucket": costs,
        })
    return candidates, [str(r["bucket"]) for r in ordered]


def _stats(solution, probabilities):
    scenario = solution.get("target_scenario_pnl") or {}
    mean = sum(
        float(probabilities.get(b) or 0.0) * float(v)
        for b, v in scenario.items()
    )
    variance = sum(
        float(probabilities.get(b) or 0.0) * (float(v) - mean) ** 2
        for b, v in scenario.items()
    )
    win = sum(
        float(probabilities.get(b) or 0.0)
        for b, v in scenario.items()
        if float(v) > 0.0
    )
    held = {
        b: round(float(v), 1)
        for b, v in (solution.get("target_shares_by_bucket") or {}).items()
        if float(v) > 1e-9
    }
    return mean, variance ** 0.5, win, held


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--budget", type=float, default=20.0)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    artifact = json.loads(conn.execute(
        "SELECT artifact_json FROM basket_delta_v2_policy_artifact "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0])
    clip_grid = artifact.get("cash_clip_grid") or [0.5, 1.0, 2.0]
    weights = ValueWeightsV2.from_mapping(artifact["value_weights"])

    rows = conn.execute(
        "SELECT station_id,event_date,plan_json FROM ("
        "  SELECT station_id,event_date,plan_json,ROW_NUMBER() OVER ("
        "    PARTITION BY station_id,event_date ORDER BY rowid DESC) rn"
        "  FROM basket_p0_preselection_plan"
        ") WHERE rn=1 ORDER BY station_id,event_date LIMIT ?",
        (args.limit,),
    ).fetchall()

    print(f"{'站':6s}{'日期':11s}{'桶':>4}"
          f"{'旧σ':>9}{'新σ':>9}{'旧胜率':>8}{'新胜率':>8}"
          f"{'旧E':>8}{'新E':>8}  持仓")
    print("-" * 100)
    compared = changed = 0
    agg = dict.fromkeys(
        ("s_old", "s_new", "w_old", "w_new", "m_old", "m_new"), 0.0
    )
    for station, event_date, plan_json in rows:
        compiled = json.loads(plan_json).get("compiled_plan") or {}
        sizing = compiled.get("order_sizing_decisions") or []
        probabilities = (
            (compiled.get("probability_envelope") or {}).get("probabilities")
            or {}
        )
        if not sizing or not probabilities:
            continue
        candidates, support = _candidates(sizing, clip_grid, args.budget)
        if len(candidates) < 2:
            continue
        try:
            old = solve_target_inventory_v2(
                probabilities=probabilities,
                feasible_inventory_candidates=candidates,
                protected_buckets=tuple(support),
                shortfall_buckets=(), breakthrough_buckets=(),
                weights=weights,
                objective_form=VALUE_OBJECTIVE_FORM,
            )
            new = solve_target_inventory_v2(
                probabilities=probabilities,
                feasible_inventory_candidates=candidates,
                protected_buckets=tuple(support),
                shortfall_buckets=(), breakthrough_buckets=(),
                weights=weights,
                objective_form=CURVE_OBJECTIVE_FORM,
                bankroll_usdc=args.budget,
            )
        except ContractError as exc:
            print(f"{station:6s}{event_date:11s}  {exc}")
            continue
        compared += 1
        m_o, s_o, w_o, h_o = _stats(old, probabilities)
        m_n, s_n, w_n, h_n = _stats(new, probabilities)
        agg["s_old"] += s_o; agg["s_new"] += s_n
        agg["w_old"] += w_o; agg["w_new"] += w_n
        agg["m_old"] += m_o; agg["m_new"] += m_n
        same = h_o == h_n
        if not same:
            changed += 1
        note = "同" if same else f"{sum(h_o.values()):.0f}股 -> {sum(h_n.values()):.0f}股"
        print(f"{station:6s}{event_date:11s}{len(support):>4}"
              f"{s_o:>9.2f}{s_n:>9.2f}{w_o:>8.1%}{w_n:>8.1%}"
              f"{m_o:>8.2f}{m_n:>8.2f}  {note}")

    print("-" * 100)
    if compared:
        print(f"可比 {compared} 站日 | 赢家变化 {changed} "
              f"({changed/compared:.0%})")
        print(f"  平均 σ    {agg['s_old']/compared:8.2f} -> "
              f"{agg['s_new']/compared:8.2f}")
        print(f"  平均胜率  {agg['w_old']/compared:8.1%} -> "
              f"{agg['w_new']/compared:8.1%}")
        print(f"  平均 E    {agg['m_old']/compared:8.2f} -> "
              f"{agg['m_new']/compared:8.2f}")
    print(f"\nKelly 分数 {DEFAULT_KELLY_FRACTION} | "
          f"预算 {args.budget} | 新形态 {CURVE_OBJECTIVE_FORM}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
