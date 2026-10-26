#!/usr/bin/env python3
"""Per-station shrunk TX weight (PROTOCOL.md), walk-forward on old roots (08-20..) and new roots (09-29..).

    python3 docs/research/taf_station_tx_20261026/run.py
"""

from __future__ import annotations

import bisect
import glob
import json
import math
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import fit_taf_likelihood as TF  # noqa: E402
from bayes_basket_target import p0_ngr as NGR  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
REPLAY = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "p0_ngr_root_replay.db"
ARCHIVE = ROOT / "data" / "runtime" / "taf_raw_archive"
K = 15.0
FLOOR = 1e-3
DRAWS = 2000
P0_SQL = (
    "SELECT decision_at_utc, probabilities_json, bucket_ordinals_json,"
    " json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$.deterministic_consensus_json')"
    " FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
    " WHERE station_id=? AND event_date=? AND stage='P0' AND state='COMMITTED' AND decision_at_utc<=?"
    " ORDER BY decision_at_utc DESC LIMIT 1"
)


def ep(v):
    return float(v) if isinstance(v, (int, float)) else datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")


def morning_tx(tzs):
    """(station, day) -> (issue epoch, TX) of the last bulletin issued before 10:00 local carrying that day's TX."""

    best = {}
    for path in sorted(glob.glob(str(ARCHIVE / "*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            s, a = r.get("station_id"), r.get("awc_row") or {}
            if s not in tzs:
                continue
            z = ZoneInfo(tzs[s])
            try:
                t = ep(r["issue_time_utc"])
            except (KeyError, ValueError):
                continue
            # PROTOCOL 补记一: TX from the raw text -- AWC's decoded ``temp`` is empty
            # before October.
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
                value = -float(val) if sign == "M" else float(val)
                if (s, day) not in best or t > best[(s, day)][0]:
                    best[(s, day)] = (t, value)
    return best


def collect():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    replay = sqlite3.connect(f"file:{REPLAY}?mode=ro", uri=True)
    tzs = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM basket_probability_envelope"
        " WHERE event_date='2026-10-20' AND stage='P0' GROUP BY station_id"))
    truth = TF.load_truth_revisions(conn)
    tx = morning_tx(tzs)
    new_roots = defaultdict(list)
    for s, d, dec, mu, sd, pj in replay.execute(
            "SELECT station_id, event_date, decision_at_utc, mu_c, sd_c, probabilities_json FROM root WHERE theta_fitted_through!='served'"):
        new_roots[(s, d)].append((ep(dec), pj))
    for v in new_roots.values():
        v.sort(key=lambda x: x[0])
    cases = []
    for (s, day), (t, txv) in sorted(tx.items()):
        if (s, day) not in truth or day < "2026-08-20" or day > "2026-10-24":
            continue
        row = conn.execute(P0_SQL, (s, day, iso(t))).fetchone()
        if row is None:
            continue
        _dec, pj, oj, ivj, cj = row
        ordinals, intervals = json.loads(oj), json.loads(ivj or "{}")
        ordered = sorted(ordinals, key=lambda k: ordinals[k])
        if set(ordered) - set(intervals):
            continue
        probs = {k: float(v) for k, v in json.loads(pj).items()}
        if cj and '"SERVED"' in cj:
            pre = json.loads(cj).get("pre_shift_probabilities")
            if pre:
                probs = {k: float(v) for k, v in pre.items()}
        labels = TF._revision_labels(truth[(s, day)], lineage={"bucket_intervals_c_json": ivj}, ordinals=ordinals)
        if not labels:
            continue
        roots = {"old": [probs[k] for k in ordered]}
        nr = new_roots.get((s, day))
        if nr:
            i = bisect.bisect_right([x[0] for x in nr], t)
            if i:
                p = json.loads(nr[i - 1][1])
                roots["new"] = [float(p[k]) for k in ordered]
        geom = NGR.geometry(ordered, intervals)
        cases.append({"station": s, "day": day, "tx": txv, "geom": geom, "ordered": ordered,
                      "winner": ordered.index(labels[-1][3]), "y": labels[-1][2],
                      "first_known": labels[0][0], "roots": roots})
    conn.close()
    return cases


def shift(p, geom, delta):
    """The root vector moved by ``delta``: each cell uniform, re-integrated on the same bucket bounds."""

    slo, shi = np.asarray(geom["slo"]) + delta, np.asarray(geom["shi"]) + delta
    tlo, thi = np.asarray(geom["tlo"]), np.asarray(geom["thi"])
    lo = np.maximum(slo[:, None], tlo[None, :])
    hi = np.minimum(shi[:, None], thi[None, :])
    frac = np.clip(hi - lo, 0, None) / (shi - slo)[:, None]
    q = (np.asarray(p)[:, None] * frac).sum(0)
    q = np.maximum(q / q.sum(), FLOOR)
    return q / q.sum()


def mean_of(p, geom):
    return NGR.vector_moments(p, geom)[0]


def scores(q, w):
    o = np.zeros_like(q)
    o[w] = 1.0
    return {"logscore": float(-math.log(q[w])), "brier": float(((q - o) ** 2).sum()),
            "crps": float(((np.cumsum(q) - np.cumsum(o)) ** 2).sum())}


def weight(train, key=None):
    num = den = 0.0
    n = 0
    for c in train:
        if key is not None and c["station"] != key:
            continue
        x, y = c["tx"] - c["mu"], c["y"] - c["mu"]
        num += x * y
        den += x * x
        n += 1
    if den <= 0 or n == 0:
        return 0.0
    return float(min(1.0, max(0.0, n * (num / den) / (n + K))))


def boot(per_day):
    days = sorted(per_day)
    sums = np.array([sum(per_day[d]) for d in days])
    cnts = np.array([len(per_day[d]) for d in days])
    rng = np.random.default_rng(281)
    picks = rng.integers(0, len(days), (DRAWS, len(days)))
    st = sums[picks].sum(1) / cnts[picks].sum(1)
    return float(np.percentile(st, 2.5)), float(np.percentile(st, 97.5))


def run(cases, root, score_from):
    cs = [dict(c, p=c["roots"][root]) for c in cases if root in c["roots"]]
    for c in cs:
        c["mu"] = mean_of(c["p"], c["geom"])
    days = sorted({c["day"] for c in cs})
    rows, final_w = [], {}
    for day in days:
        if day < score_from:
            continue
        start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc).timestamp()
        train = [c for c in cs if c["day"] < day and c["first_known"] <= start]
        if len({c["day"] for c in train}) < 7:
            continue
        g = weight(train)
        ws = {s: weight(train, s) for s in {c["station"] for c in cs}}
        final_w = ws
        for c in (c for c in cs if c["day"] == day):
            x = c["tx"] - c["mu"]
            r = {"day": day, "station": c["station"]}
            for arm, delta in (("R", 0.0), ("S", ws.get(c["station"], 0.0) * x), ("G", g * x)):
                for k, v in scores(shift(c["p"], c["geom"], delta), c["winner"]).items():
                    r[f"{arm}_{k}"] = v
            rows.append(r)
    return rows, final_w, g


def compare(rows, hi, lo, keep=lambda r: True):
    out = {}
    for sc in ("logscore", "crps", "brier"):
        per_day = defaultdict(list)
        for r in rows:
            if keep(r):
                per_day[r["day"]].append(r[f"{hi}_{sc}"] - r[f"{lo}_{sc}"])
        allv = [v for vs in per_day.values() for v in vs]
        ci = boot(per_day)
        out[sc] = {"delta": round(statistics.mean(allv), 5), "ci": [round(ci[0], 5), round(ci[1], 5)]}
        if sc == "logscore":
            out["n_days"], out["n"] = len(per_day), len(allv)
            out["days_better"] = sum(1 for v in per_day.values() if statistics.mean(v) < 0)
    ls = out["logscore"]
    out["verdict"] = ("INSUFFICIENT" if out["n_days"] < 20 else
                      "PASS" if ls["ci"][1] < 0 and out["crps"]["ci"][0] <= 0 and out["brier"]["ci"][0] <= 0 else "REFUSED")
    return out


def main() -> int:
    cases = collect()
    print("cases", len(cases), "stations", len({c["station"] for c in cases}),
          "days", len({c["day"] for c in cases}), "with new root", sum(1 for c in cases if "new" in c["roots"]), flush=True)
    out = {}
    for root, score_from in (("old", "2026-08-27"), ("new", "2026-10-06")):
        rows, ws, g = run(cases, root, score_from)
        days = sorted({r["day"] for r in rows})
        half = days[len(days) // 2]
        by_st = defaultdict(list)
        for r in rows:
            by_st[r["station"]].append(r["S_logscore"] - r["R_logscore"])
        out[root] = {
            "days": [days[0], days[-1]], "split": half,
            "S-R": compare(rows, "S", "R"), "G-R": compare(rows, "G", "R"), "S-G": compare(rows, "S", "G"),
            "S-R_first_half": compare(rows, "S", "R", lambda r: r["day"] < half),
            "S-R_second_half": compare(rows, "S", "R", lambda r: r["day"] >= half),
            "final_station_w": {s: round(w, 3) for s, w in sorted(ws.items()) if w > 0},
            "final_fleet_w": round(g, 3),
            "station_delta": {s: (len(v), round(statistics.mean(v), 4)) for s, v in sorted(by_st.items())},
        }
    (HERE / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    for root, r in out.items():
        print("==", root, r["days"], "fleet w", r["final_fleet_w"])
        for k in ("S-R", "G-R", "S-G", "S-R_first_half", "S-R_second_half"):
            v = r[k]
            print(f"  {k:16s} {v['logscore']['delta']:+.4f} [{v['logscore']['ci'][0]:+.4f},{v['logscore']['ci'][1]:+.4f}]"
                  f" rps{v['crps']['delta']:+.4f}{v['crps']['ci']} br{v['brier']['delta']:+.4f}{v['brier']['ci']}"
                  f" {v['n_days']}d/{v['days_better']} {v['verdict']}")
        print("  final w>0:", r["final_station_w"])
        print("  per-station S-R:", {s: v for s, v in r["station_delta"].items() if v[1] != 0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
