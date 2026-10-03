#!/usr/bin/env python3
"""Audit of the board's truth-landing tables (2026-10-03, read-only).

The board's two tables ("模型和市场意见不一致时，谁更准" and "两把尺子按档对比") are
built by ``scripts/calibration_dashboard.collect_truth_landing`` from per
station-day records the render keeps (``data/dashboard_section_cache/
_truth_landing_records.json``: the first P0 decision's time and claim, each
bucket's entry asks -- count and median over the four hours after it -- and
the last non-zero bid).  This re-reads those records and checks:

1. the truth: the market's adjudication (last bid >= 0.80) against the
   settlement pipeline's own settled bucket (``basket_station_day_settlement_label``);
2. the market ruler: how much the best asks of one ladder sum to (overround),
   and how many truth buckets the market side drops for want of two asks;
3. the claims: do they sum to one, do their keys match the ladder's;
4. the sample: which P0 versions the first decisions were served under, and
   how far before settlement;
5. the verdicts on cleaner footing: official truth, the asks normalised to
   one ladder, the current chain only, with station-day bootstrap intervals.

Writes results.json.
"""
from __future__ import annotations

import collections
import json
import math
import sqlite3
import statistics
import sys
from bisect import bisect_right
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import serving_arithmetic as sa  # noqa: E402

RECORDS = ROOT / "data/dashboard_section_cache/_truth_landing_records.json"
BANDS = [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.25), (0.25, 0.30), (0.30, 0.35),
         (0.35, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 1.01)]
GAPS = [("model_much", 0.10, None), ("model_bit", 0.03, 0.10), ("agree", -0.03, 0.03),
        ("market_bit", -0.10, -0.03), ("market_much", None, -0.10)]
rng = np.random.default_rng(20261003)


def band_of(v):
    for lo, hi in BANDS:
        if lo <= v < hi:
            return (lo, hi)
    return None


def gap_of(g):
    for name, lo, hi in GAPS:
        if (lo is None or g >= lo) and (hi is None or g < hi):
            return name
    return None


def boot_rate(rows, draws=1000):
    """rows: (station_day, hit, value).  Station-day clusters; returns hit-rate CI and (hit - value) CI."""
    by = collections.defaultdict(list)
    for sd, hit, value in rows:
        by[sd].append((hit, value))
    keys = list(by)
    h = np.array([sum(x for x, _ in by[k]) for k in keys], float)
    v = np.array([sum(y for _, y in by[k]) for k in keys], float)
    n = np.array([len(by[k]) for k in keys], float)
    pick = rng.integers(0, len(keys), size=(draws, len(keys)))
    rates = h[pick].sum(1) / n[pick].sum(1)
    gaps = (h[pick].sum(1) - v[pick].sum(1)) / n[pick].sum(1)
    return [float(np.percentile(rates, 2.5)), float(np.percentile(rates, 97.5))], [float(np.percentile(gaps, 2.5)), float(np.percentile(gaps, 97.5))]


def version_at(decided: str) -> str:
    starts = sorted((at, v) for v, at in (sa.STAGE_VERSION_EFFECTIVE_FROM.get("P0") or {}).items())
    idx = bisect_right([a for a, _ in starts], decided) - 1
    return starts[idx][1].rsplit("_", 1)[-1] if idx >= 0 else "pre-v3"


def main():
    K = json.load(open(RECORDS))
    records = K["records"]
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=10)
    zones = dict(conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"))
    labels = {}
    for st, day, rev, bucket in conn.execute("SELECT station_id, event_date, truth_revision_no, settled_bucket FROM basket_station_day_settlement_label ORDER BY truth_revision_no"):
        labels[(st, day)] = bucket          # latest revision wins
    conn.close()
    out = {"records": len(records), "kept_through": K.get("through")}

    # ---- per station-day facts
    rows = []
    for key, r in records.items():
        st, day = key.split("|")
        bids = r.get("bids") or {}
        winner = None
        if bids:
            b, top = max(bids.items(), key=lambda kv: kv[1])
            if top >= 0.80:
                winner = b
        claims = r.get("claims") or {}
        asks = {b: v for b, v in (r.get("asks") or {}).items() if int(v[0]) >= 2}
        decided = r["decided"]
        try:
            end = datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=ZoneInfo(zones[st])) + timedelta(days=1)
            hours = (end - datetime.fromisoformat(decided.replace("Z", "+00:00"))).total_seconds() / 3600
        except Exception:
            hours = None
        rows.append({"st": st, "day": day, "winner": winner, "official": labels.get((st, day)), "claims": claims,
                     "asks": {b: float(v[1]) for b, v in asks.items()}, "decided": decided, "version": version_at(decided),
                     "hours": hours})

    # 1. truth
    adj = [r for r in rows if r["winner"] is not None]
    both = [r for r in adj if r["official"] is not None]
    agree = [r for r in both if r["official"] == r["winner"]]
    disagree = [r for r in both if r["official"] != r["winner"]]
    no_adj_but_official = [r for r in rows if r["winner"] is None and r["official"] is not None]
    out["truth"] = {"station_days": len(rows), "adjudicated": len(adj), "with_official_label": len(both),
                    "agree": len(agree), "disagree": len(disagree), "agree_rate": len(agree) / len(both) if both else None,
                    "not_adjudicated_but_official_label": len(no_adj_but_official),
                    "official_label_not_in_claims": sum(1 for r in both if r["official"] not in r["claims"]),
                    "disagree_examples": [(r["st"], r["day"], r["winner"], r["official"]) for r in disagree[:15]]}

    # 2. market ruler
    overround, coverage, truth_unpriced, full_ladder = [], [], 0, 0
    for r in adj:
        ladder = set(r["claims"])
        priced = set(r["asks"]) & ladder
        coverage.append(len(priced) / len(ladder) if ladder else 0)
        if r["winner"] not in r["asks"]:
            truth_unpriced += 1
        if ladder and priced == ladder:
            full_ladder += 1
            overround.append(sum(r["asks"][b] for b in ladder))
    out["market"] = {"days_with_every_bucket_priced": full_ladder, "ask_sum_median": statistics.median(overround) if overround else None,
                     "ask_sum_p10_p90": [float(np.percentile(overround, 10)), float(np.percentile(overround, 90))] if overround else None,
                     "priced_share_of_ladder_median": statistics.median(coverage) if coverage else None,
                     "truth_bucket_unpriced": truth_unpriced, "truth_bucket_unpriced_share": truth_unpriced / len(adj) if adj else None}

    # 3. claims
    sums = [sum(r["claims"].values()) for r in rows if r["claims"]]
    out["claims"] = {"sum_min": min(sums), "sum_max": max(sums),
                     "ask_keys_outside_claims": sum(1 for r in rows for b in r["asks"] if b not in r["claims"])}

    # 4. sample
    out["versions"] = dict(collections.Counter(r["version"] for r in adj).most_common())
    hrs = [r["hours"] for r in adj if r["hours"] is not None]
    out["hours_to_settlement"] = {"p10": float(np.percentile(hrs, 10)), "median": float(np.median(hrs)), "p90": float(np.percentile(hrs, 90))}

    # 5. verdicts under each footing
    def tables(sel, truth_key, normalise):
        claim_rows, price_rows, gap_rows = [], [], collections.defaultdict(list)
        for r in sel:
            t = r[truth_key]
            if t is None or t not in r["claims"]:
                continue
            ladder = list(r["claims"])
            priced = {b: v for b, v in r["asks"].items() if b in r["claims"]}
            if normalise:
                if set(priced) != set(ladder):
                    continue
                s = sum(priced.values())
                priced = {b: v / s for b, v in priced.items()}
            sd = (r["st"], r["day"])
            for b in ladder:
                claim_rows.append((sd, 1 if b == t else 0, r["claims"][b]))
            for b, q in priced.items():
                price_rows.append((sd, 1 if b == t else 0, q))
                c = r["claims"][b]
                if c < 0.05 and q < 0.05:
                    continue
                gap_rows[gap_of(c - q)].append((sd, 1 if b == t else 0, c, q))
        def bandtab(rows):
            cells = collections.defaultdict(list)
            for sd, hit, v in rows:
                cells[band_of(v)].append((sd, hit, v))
            tab = {}
            for bd in BANDS:
                cs = cells.get(bd, [])
                if not cs:
                    continue
                ci_rate, ci_gap = boot_rate(cs)
                tab[f"{bd[0]:.2f}-{min(bd[1],1):.2f}"] = {"n": len(cs), "said": statistics.mean(v for _, _, v in cs),
                                                          "hit": statistics.mean(h for _, h, _ in cs), "hit_ci": ci_rate, "hit_minus_said_ci": ci_gap}
            return tab
        gtab = {}
        for name, *_ in GAPS:
            gs = gap_rows.get(name, [])
            if not gs:
                continue
            ci_rate, _ = boot_rate([(sd, h, c) for sd, h, c, q in gs])
            hit = statistics.mean(h for _, h, _, _ in gs); c = statistics.mean(x for _, _, x, _ in gs); q = statistics.mean(x for _, _, _, x in gs)
            # who is nearer, with an interval on |hit-model| - |hit-market| by station-day bootstrap
            by = collections.defaultdict(list)
            for sd, h, cc, qq in gs:
                by[sd].append((h, cc, qq))
            keys = list(by)
            H = np.array([sum(x[0] for x in by[k]) for k in keys], float); C = np.array([sum(x[1] for x in by[k]) for k in keys], float)
            Q = np.array([sum(x[2] for x in by[k]) for k in keys], float); N = np.array([len(by[k]) for k in keys], float)
            pick = rng.integers(0, len(keys), size=(1000, len(keys)))
            hh, cc_, qq_ = H[pick].sum(1) / N[pick].sum(1), C[pick].sum(1) / N[pick].sum(1), Q[pick].sum(1) / N[pick].sum(1)
            diff = np.abs(hh - cc_) - np.abs(hh - qq_)       # > 0: market nearer
            gtab[name] = {"n": len(gs), "model": c, "market": q, "hit": hit, "hit_ci": ci_rate,
                          "market_nearer_by": float(np.mean(diff)), "market_nearer_by_ci": [float(np.percentile(diff, 2.5)), float(np.percentile(diff, 97.5))]}
        return {"station_days": len({(r['st'], r['day']) for r in sel}), "by_claim": bandtab(claim_rows), "by_price": bandtab(price_rows), "gaps": gtab}

    current = [r for r in adj if r["decided"] >= "2026-09-25T14:00:00Z"]       # P0 v19 onward
    out["footings"] = {
        "board_as_built": tables(adj, "winner", False),
        "official_truth": tables([r for r in adj if r["official"]], "official", False),
        "official_truth_normalised_asks": tables([r for r in adj if r["official"]], "official", True),
        "current_chain_v19plus_official_normalised": tables([r for r in current if r["official"]], "official", True),
        "current_chain_v19plus_official_raw_asks": tables([r for r in current if r["official"]], "official", False),
    }
    (HERE / "results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    # ---- print
    print("records", out["records"], "kept through", out["kept_through"])
    print("TRUTH", {k: v for k, v in out["truth"].items() if k != "disagree_examples"})
    print("  disagreements:", out["truth"]["disagree_examples"])
    print("MARKET", out["market"])
    print("CLAIMS", out["claims"])
    print("VERSIONS", out["versions"])
    print("HOURS", out["hours_to_settlement"])
    for name, f in out["footings"].items():
        print(f"== {name}  station-days {f['station_days']}")
        for side in ("by_claim", "by_price"):
            print("   ", side, " | ".join(f"{b} n{v['n']} {v['said']:.3f}->{v['hit']:.3f}[{v['hit_ci'][0]:.3f},{v['hit_ci'][1]:.3f}]" for b, v in f[side].items()))
        for g, v in f["gaps"].items():
            print(f"    gap {g:12s} n {v['n']:5d} model {v['model']:.3f} market {v['market']:.3f} hit {v['hit']:.3f} [{v['hit_ci'][0]:.3f},{v['hit_ci'][1]:.3f}]  market nearer by {v['market_nearer_by']:+.3f} [{v['market_nearer_by_ci'][0]:+.3f},{v['market_nearer_by_ci'][1]:+.3f}]")


if __name__ == "__main__":
    main()
