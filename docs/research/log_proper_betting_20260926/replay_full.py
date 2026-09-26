#!/usr/bin/env python3
"""Full-fleet, full-history replay of the paper's own protocol (arXiv 2607.06166
Sections 4, C.1, C.2, D.2, D.7) -- PROTOCOL.md, rounds two and three, unchanged,
on every settled station-day 2026-08-15..2026-09-25.

Dataset: dataset_full.jsonl.gz (build_full.py: the bayes_kelly_replay_20260924
builder over the widened window; 9 grid hours per station-day, stage-priority
served vector and its P0 root, the official WS book six levels deep on both
sides, the book never older than the belief).  Truth: truth_full.json (current
settlement labels, latest revision, plus the venue's own resolution where the
charter-207 reconciliation still disagrees).

Per bucket a binary market; YES when p > ask, NO when p < bid, nothing inside
the band; sizes Brier |p - q~|, Log |log p - log q~|, logit |logit p - logit q~|;
fills walk the book; ROI = sum P&L / sum cost; event-day bootstrap.  Both sides,
no repo constraint, for the served vector and for the pure P0 root.

Readings: pooled (fundamental, momentum, by grid hour, by stage, by period, by
unit), per station (fundamental and momentum, dS/D, first-half vs second-half),
the count of stations whose interval clears zero against what chance gives, and
walk-forward station selection.  No SSOT access.  Writes results_replay_full.json.

    PYTHONPATH=<repo> python3 replay_full.py dataset_full.jsonl.gz truth_full.json
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from levers import walk_forward  # noqa: E402
from paper_protocol import GRID, SEED, bets, load, momentum, pooled  # noqa: E402

RULES = ("brier", "log_d2", "logit_c1")
PERIODS = (("08-15..08-31", "2026-08-15", "2026-08-31"), ("09-01..09-13", "2026-09-01", "2026-09-13"),
           ("09-14..09-25", "2026-09-14", "2026-09-25"))
MIN_STATION_BETS = 40


def unit(row):
    return "F" if "°F" in row["keys"][0] else "C"


def main():
    dataset, truth = sys.argv[1], sys.argv[2]
    results = {"vectors": {}}
    for vector in ("served", "root"):
        rng = np.random.default_rng(SEED)
        rows, skipped = load(dataset, truth, vector=vector, floor="log_only")
        by_sd = defaultdict(list)
        for r in rows:
            by_sd[(r["station"], r["date"])].append(r)
        stations = sorted({r["station"] for r in rows})
        out = {"rows": len(rows), "skipped": skipped, "station_days": len(by_sd), "stations": len(stations),
               "event_days": len({r["date"] for r in rows}), "pooled": {}, "by_H": {}, "by_stage": {},
               "by_period": {}, "by_unit": {}, "per_station": {}, "significance_count": {}, "walk_forward_station": {}}
        bets_by_rule = {}
        for rule in RULES:
            items = [dict(b, date=r["date"], station=r["station"], H=r["H"], stage=r["stage"], unit=unit(r))
                     for r in rows for b in bets(r, rule)]
            bets_by_rule[rule] = items
            yes_only = [b for b in items if b["side"] == "YES"]
            out["pooled"][rule] = {
                "fundamental": pooled(items, rng),
                "fundamental_YES_legs": pooled(yes_only, rng),
                "fundamental_NO_legs": pooled([b for b in items if b["side"] == "NO"], rng),
                "momentum": pooled(momentum(by_sd, rule), rng),
            }
            out["by_H"][rule] = {str(H): pooled([b for b in items if b["H"] == H], rng) for H in GRID}
            out["by_stage"][rule] = {s: pooled([b for b in items if b["stage"] == s], rng) for s in ("P0", "P0-M", "P1")}
            out["by_period"][rule] = {lab: pooled([b for b in items if lo <= b["date"] <= hi], rng) for lab, lo, hi in PERIODS}
            out["by_unit"][rule] = {u: pooled([b for b in items if b["unit"] == u], rng) for u in ("C", "F")}
        # per station
        for st in stations:
            entry = {}
            days = sorted({d for s, d in by_sd if s == st})
            half = days[: len(days) // 2], days[len(days) // 2:]
            for rule in RULES:
                items = [b for b in bets_by_rule[rule] if b["station"] == st]
                f = pooled(items, rng)
                first = [b for b in items if b["date"] in set(half[0])]
                second = [b for b in items if b["date"] in set(half[1])]
                roi_half = [(sum(b["pnl"] for b in h) / sum(b["cost"] for b in h)) if h and sum(b["cost"] for b in h) > 0 else None
                            for h in (first, second)]
                mom = pooled(momentum({k: v for k, v in by_sd.items() if k[0] == st}, rule), rng)
                entry[rule] = {"fundamental": f, "first_half_roi": roi_half[0], "second_half_roi": roi_half[1],
                               "momentum": mom}
            entry["days"] = len(days)
            entry["unit"] = unit(next(r for r in rows if r["station"] == st))
            out["per_station"][st] = entry
        for rule in RULES:
            lows = [e[rule]["fundamental"]["roi"][1] for e in out["per_station"].values() if e[rule]["fundamental"].get("roi")]
            highs = [e[rule]["fundamental"]["roi"][2] for e in out["per_station"].values() if e[rule]["fundamental"].get("roi")]
            both_halves_pos = [st for st, e in out["per_station"].items()
                               if (e[rule]["first_half_roi"] or -1) > 0 and (e[rule]["second_half_roi"] or -1) > 0]
            out["significance_count"][rule] = {
                "stations": len(lows), "lower_bound_above_zero": sum(1 for x in lows if x > 0),
                "upper_bound_below_zero": sum(1 for x in highs if x < 0),
                "expected_above_zero_by_chance_if_all_null": round(0.025 * len(lows), 2),
                "point_positive": sum(1 for e in out["per_station"].values() if e[rule]["fundamental"].get("roi") and e[rule]["fundamental"]["roi"][0] > 0),
                "both_halves_positive": both_halves_pos,
            }
            out["walk_forward_station"][rule] = walk_forward(bets_by_rule[rule], lambda b: b["station"], MIN_STATION_BETS, rng)
        results["vectors"][vector] = out
        del rows, by_sd, bets_by_rule
    (HERE / "results_replay_full.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))

    def fmt(x):
        return "—" if not x else f"{100 * x[0]:+.1f}% [{100 * x[1]:+.1f}, {100 * x[2]:+.1f}]"

    for vector, out in results["vectors"].items():
        print(f"\n######## vector={vector}: rows {out['rows']} station-days {out['station_days']} stations {out['stations']} days {out['event_days']} skipped {out['skipped']}")
        for rule in RULES:
            p = out["pooled"][rule]
            f = p["fundamental"]
            print(f"{rule:9s} fundamental {fmt(f['roi'])} (dS {100 * f['dS_roi']:+.1f} D {100 * f['D_roi']:+.1f}, bets {f['bets']}, cost {f['cost']:.0f}) | "
                  f"YES {fmt(p['fundamental_YES_legs']['roi'])} NO {fmt(p['fundamental_NO_legs']['roi'])} | momentum {fmt(p['momentum']['roi'])}")
        for rule in ("brier", "log_d2"):
            print(f"  {rule} by H: " + " | ".join(f"H{H} {fmt(b.get('roi'))}" for H, b in out["by_H"][rule].items()))
            print(f"  {rule} by stage: " + " | ".join(f"{s} {fmt(b.get('roi'))} n={b.get('bets')}" for s, b in out["by_stage"][rule].items()))
            print(f"  {rule} by period: " + " | ".join(f"{s} {fmt(b.get('roi'))}" for s, b in out["by_period"][rule].items()))
            print(f"  {rule} by unit: " + " | ".join(f"{s} {fmt(b.get('roi'))}" for s, b in out["by_unit"][rule].items()))
        for rule in RULES:
            print(f"  significance {rule}: {out['significance_count'][rule]}")
            w = out["walk_forward_station"][rule]
            print(f"  walk-forward station pick {rule}: {fmt(w.get('roi'))} bets {w.get('bets')} tested days {w.get('tested_days')}")
        print("\n  station | unit | days | Brier ROI | Log ROI | logit ROI | Brier 1st/2nd half | Log 1st/2nd half | momentum Brier | momentum Log")
        order = sorted(out["per_station"], key=lambda s: -(out["per_station"][s]["brier"]["fundamental"].get("roi") or [0])[0])
        for st in order:
            e = out["per_station"][st]
            h = lambda r: "/".join("—" if v is None else f"{100 * v:+.0f}" for v in (e[r]["first_half_roi"], e[r]["second_half_roi"]))
            print(f"  {st} | {e['unit']} | {e['days']:2d} | {fmt(e['brier']['fundamental'].get('roi'))} | {fmt(e['log_d2']['fundamental'].get('roi'))} | "
                  f"{fmt(e['logit_c1']['fundamental'].get('roi'))} | {h('brier')} | {h('log_d2')} | {fmt(e['brier']['momentum'].get('roi'))} | {fmt(e['log_d2']['momentum'].get('roi'))}")


if __name__ == "__main__":
    main()
