#!/usr/bin/env python3
"""TAF direction and warm/cold side against P0 and the market (PROTOCOL.md).

    python3 docs/research/taf_direction_20261026/run.py
"""

from __future__ import annotations

import glob
import json
import math
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import fit_taf_likelihood as TF  # noqa: E402
from bayes_basket_target import p0_ngr as NGR  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE = ROOT / "data" / "runtime" / "taf_raw_archive"
DRAWS = 2000
P0_SQL = (
    "SELECT probabilities_json, bucket_ordinals_json, json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$.deterministic_consensus_json')"
    " FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
    " WHERE station_id=? AND event_date=? AND stage='P0' AND state='COMMITTED' AND decision_at_utc<=?"
    " ORDER BY decision_at_utc DESC LIMIT 1"
)
MKT_SQL = (
    "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
    " AND state='COMMITTED' AND observed_at<=? ORDER BY observed_at DESC LIMIT 1"
)


def ep(v):
    return float(v) if isinstance(v, (int, float)) else datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")


def tx_series(tzs):
    """(station, day) -> sorted [(issue epoch, TX)] of bulletins issued before 10:00 local carrying that day's TX."""

    out = defaultdict(dict)
    for path in sorted(glob.glob(str(ARCHIVE / "*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            s = r.get("station_id")
            if s not in tzs:
                continue
            z = ZoneInfo(tzs[s])
            try:
                t = ep(r["issue_time_utc"])
            except (KeyError, ValueError):
                continue
            issued = datetime.fromtimestamp(t, tz=timezone.utc)
            for sign, val, dd, hh in re.findall(r"\bTX(M?)(\d{2})/(\d{2})(\d{2})Z", str(r.get("raw_taf") or "")):
                year, month = issued.year, issued.month
                if int(dd) < issued.day - 3:
                    month += 1
                    if month == 13:
                        year, month = year + 1, 1
                try:
                    valid = datetime(year, month, int(dd), int(hh) % 24, tzinfo=timezone.utc)
                except ValueError:
                    continue
                day = valid.astimezone(z).date().isoformat()
                y, m, d = (int(x) for x in day.split("-"))
                if t > datetime(y, m, d, 10, 0, tzinfo=z).timestamp():
                    continue
                out[(s, day)][t] = -float(val) if sign == "M" else float(val)
    return {k: sorted(v.items()) for k, v in out.items()}


def bucket_of(value, ordered, intervals):
    for i, k in enumerate(ordered):
        lo, hi = intervals[k]
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return i
    return None


def collect():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    tzs = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM basket_probability_envelope"
        " WHERE event_date='2026-10-20' AND stage='P0' GROUP BY station_id"))
    truth = TF.load_truth_revisions(conn)
    series = tx_series(tzs)
    cases = []
    for (s, day), seq in sorted(series.items()):
        if (s, day) not in truth or not ("2026-08-20" <= day <= "2026-10-24"):
            continue
        t_last, tx_last = seq[-1]
        _t0, tx_first = seq[0]
        row = conn.execute(P0_SQL, (s, day, iso(t_last))).fetchone()
        if row is None:
            continue
        pj, oj, ivj, cj = row
        ordinals, intervals = json.loads(oj), json.loads(ivj or "{}")
        ordered = sorted(ordinals, key=lambda k: ordinals[k])
        if set(ordered) - set(intervals):
            continue
        probs = {k: float(v) for k, v in json.loads(pj).items()}
        if cj and '"SERVED"' in cj:
            pre = json.loads(cj).get("pre_shift_probabilities")
            if pre:
                probs = {k: float(v) for k, v in pre.items()}
        geom = NGR.geometry(ordered, intervals)
        mu = NGR.vector_moments([probs[k] for k in ordered], geom)[0]
        y = truth[(s, day)][-1][2]
        mrow = conn.execute(MKT_SQL, (s, day, iso(t_last))).fetchone()
        m = mode = None
        if mrow is not None:
            ref = market_reference_v1(json.loads(mrow[0]))
            vec = ref.get("vector") or {}
            if ref.get("state") == "complete" and set(vec) == set(ordered):
                mids = [(a + b) / 2 for a, b in zip(geom["slo"], geom["shi"])]
                tot = sum(float(vec[k]) for k in ordered)
                m = sum(float(vec[k]) * mids[i] for i, k in enumerate(ordered)) / tot
                mode = max(range(len(ordered)), key=lambda i: float(vec[ordered[i]]))
        cases.append({"station": s, "day": day, "tx": tx_last, "tx_first": tx_first, "n_tafs": len(seq),
                      "mu": mu, "y": y, "m": m, "mode": mode,
                      "tx_bucket": bucket_of(tx_last, ordered, intervals), "y_bucket": bucket_of(y, ordered, intervals)})
    conn.close()
    return cases


def rate(hits_by_day):
    days = sorted(hits_by_day)
    h = np.array([sum(hits_by_day[d]) for d in days])
    n = np.array([len(hits_by_day[d]) for d in days])
    rng = np.random.default_rng(281)
    picks = rng.integers(0, len(days), (DRAWS, len(days)))
    st = h[picks].sum(1) / n[picks].sum(1)
    return {"rate": round(float(h.sum() / n.sum()), 4), "n": int(n.sum()), "days": len(days),
            "ci": [round(float(np.percentile(st, 2.5)), 4), round(float(np.percentile(st, 97.5)), 4)]}


def wilson(h, n):
    if n == 0:
        return None
    z, p = 1.96, h / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(c - w, 3), round(c + w, 3)]


def test(cases, name, pick):
    by_day, by_station, by_size = defaultdict(list), defaultdict(list), defaultdict(lambda: defaultdict(list))
    for c in cases:
        r = pick(c)
        if r is None:
            continue
        hit, size = r
        by_day[c["day"]].append(hit)
        by_station[c["station"]].append(hit)
        by_size["0.5-1.5" if size < 1.5 else ">=1.5"][c["day"]].append(hit)
    days = sorted(by_day)
    half = days[len(days) // 2]
    out = {"all": rate(by_day),
           "first_half": rate({d: v for d, v in by_day.items() if d < half}),
           "second_half": rate({d: v for d, v in by_day.items() if d >= half}),
           "by_size": {k: rate(v) for k, v in by_size.items()},
           "by_station": {s: {"hits": sum(v), "n": len(v), "rate": round(sum(v) / len(v), 3), "ci": wilson(sum(v), len(v))}
                          for s, v in sorted(by_station.items())}}
    out["verdict"] = "DIRECTION" if out["all"]["ci"][0] > 0.5 else "NO_DIRECTION"
    return out


def sgn(x):
    return (x > 0) - (x < 0)


def main() -> int:
    cases = collect()
    print("cases", len(cases), "stations", len({c["station"] for c in cases}), "days", len({c["day"] for c in cases}),
          "with market", sum(1 for c in cases if c["m"] is not None), flush=True)
    picks = {
        "D1_vs_P0": lambda c: None if abs(c["tx"] - c["mu"]) < 0.5 or c["y"] == c["mu"]
        else (sgn(c["tx"] - c["mu"]) == sgn(c["y"] - c["mu"]), abs(c["tx"] - c["mu"])),
        "D2_vs_market_mean": lambda c: None if c["m"] is None or abs(c["tx"] - c["m"]) < 0.5 or c["y"] == c["m"]
        else (sgn(c["tx"] - c["m"]) == sgn(c["y"] - c["m"]), abs(c["tx"] - c["m"])),
        "D3_vs_market_mode_bucket": lambda c: None if c["mode"] is None or c["tx_bucket"] is None or c["y_bucket"] is None
        or c["tx_bucket"] == c["mode"] or c["y_bucket"] == c["mode"]
        else (sgn(c["tx_bucket"] - c["mode"]) == sgn(c["y_bucket"] - c["mode"]), abs(c["tx_bucket"] - c["mode"])),
        "D4_tx_revision": lambda c: None if c["tx"] == c["tx_first"] or c["y"] == c["tx_first"]
        else (sgn(c["tx"] - c["tx_first"]) == sgn(c["y"] - c["tx_first"]), abs(c["tx"] - c["tx_first"])),
    }
    out = {name: test(cases, name, f) for name, f in picks.items()}
    (HERE / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    for name, r in out.items():
        a = r["all"]
        print(f"{name}: {a['rate']:.3f} [{a['ci'][0]:.3f},{a['ci'][1]:.3f}] n={a['n']} days={a['days']} {r['verdict']} |"
              f" halves {r['first_half']['rate']:.3f}/{r['second_half']['rate']:.3f} | size " +
              ", ".join(f"{k} {v['rate']:.3f}(n{v['n']})" for k, v in r["by_size"].items()))
        print("   stations:", {s: (v["rate"], v["n"]) for s, v in r["by_station"].items() if v["n"] >= 10})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
