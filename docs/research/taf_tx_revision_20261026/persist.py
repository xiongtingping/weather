#!/usr/bin/env python3
"""PROTOCOL 补记二: the TX-revision shift kept on every hourly decision until the next revision.

    python3 docs/research/taf_tx_revision_20261026/persist.py
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import explore as X  # noqa: E402
import usage as U  # noqa: E402  (shift, bucket_of, boot, GRID)

D = X.D
STAGE_RANK = {"P1": 3, "P0-M": 2, "P0": 1}
ENV_SQL = (
    "SELECT stage, decision_at_utc, probabilities_json, json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " bucket_ordinals_json, json_extract(lineage_json,'$.deterministic_consensus_json')"
    " FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
    " WHERE station_id=? AND event_date=? AND stage IN ('P0','P0-M','P1') AND state='COMMITTED'"
    " ORDER BY decision_at_utc"
)


def collect():
    conn = sqlite3.connect(f"file:{D.DB}?mode=ro", uri=True, timeout=120)
    tzs = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM basket_probability_envelope"
        " WHERE event_date='2026-10-20' AND stage='P0' GROUP BY station_id"))
    truth = D.TF.load_truth_revisions(conn)
    rows = []
    for (s, day), seq in sorted(X.bulletins(tzs).items()):
        if (s, day) not in truth or not ("2026-08-20" <= day <= "2026-10-24"):
            continue
        revs, prev = [], None
        tx_first = seq[0][1]
        for t, tx, _k, _r in seq:
            if prev is not None and tx != prev:
                revs.append((t, tx - prev, tx))
            prev = tx
        if not revs:
            continue
        envs = conn.execute(ENV_SQL, (s, day)).fetchall()
        if not envs:
            continue
        ordinals = intervals = None
        for stage, _dec, _pj, ivj, oj, _cj in envs:
            if ivj and oj:
                ordinals, intervals = json.loads(oj), json.loads(ivj)
                break
        if not intervals:
            continue
        ordered = sorted(ordinals, key=lambda k: ordinals[k])
        if set(ordered) - set(intervals):
            continue
        geom = D.NGR.geometry(ordered, intervals)
        w = U.bucket_of(truth[(s, day)][-1][2], ordered, intervals)
        if w is None:
            continue
        stamped = []
        for stage, dec, pj, _iv, _oj, cj in envs:
            p = json.loads(pj)
            if set(p) != set(ordered):
                continue
            if stage == "P0" and cj and '"SERVED"' in cj:
                pre = json.loads(cj).get("pre_shift_probabilities")
                if pre:
                    p = pre
            stamped.append((X.D.ep(dec), stage, [float(p[k]) for k in ordered]))
        z = ZoneInfo(tzs[s])
        y_, m_, d_ = (int(x) for x in day.split("-"))
        end = datetime(y_, m_, d_, 23, 0, tzinfo=z).timestamp()
        tau = math.ceil(revs[0][0] / 3600.0) * 3600.0
        rev_times = [r[0] for r in revs]
        while tau <= end:
            best = None
            for t_env, stage, vec in stamped:
                if t_env > tau:
                    break
                if best is None or STAGE_RANK[stage] > STAGE_RANK[best[1]] or (
                        STAGE_RANK[stage] == STAGE_RANK[best[1]] and t_env >= best[0]):
                    best = (t_env, stage, vec)
            i = bisect.bisect_right(rev_times, tau) - 1
            if best is not None and i >= 0 and best[2][w] > 0:
                t_rev, step, tx_now = revs[i]
                local = datetime.fromtimestamp(tau, tz=timezone.utc).astimezone(z)
                rows.append({"station": s, "day": day, "p": best[2], "geom": geom, "winner": w, "stage": best[1],
                             "step": step, "net": tx_now - tx_first, "since_h": (tau - t_rev) / 3600.0,
                             "local_hour": local.hour if local.date().isoformat() == day else -1})
            tau += 3600.0
    conn.close()
    return rows


def score(r, delta):
    return U.score(r, delta)


def fit(train, key):
    best = (0.0, None)
    for k in U.GRID:
        ls = statistics.mean(score(r, k * r[key])["logscore"] for r in train)
        if best[1] is None or ls < best[1]:
            best = (k, ls)
    return best[0]


def main() -> int:
    rows = collect()
    days = sorted({r["day"] for r in rows})
    print("decisions", len(rows), "station-days", len({(r['station'], r['day']) for r in rows}), "days", len(days), flush=True)
    scored, path = [], []
    for day in days:
        train = [r for r in rows if r["day"] < day]
        if len({r["day"] for r in train}) < 7:
            continue
        # a subsample keeps the grid search quick; every training day still contributes
        sample = train[:: max(1, len(train) // 6000)]
        kp, kc = fit(sample, "step"), fit(sample, "net")
        path.append({"day": day, "kappa_step": kp, "kappa_net": kc})
        for r in (r for r in rows if r["day"] == day):
            out = {"day": day, "station": r["station"], "since_h": r["since_h"], "hour": r["local_hour"], "stage": r["stage"]}
            for arm, delta in (("R", 0.0), ("P", kp * r["step"]), ("C", kc * r["net"])):
                for m, v in score(r, delta).items():
                    out[f"{arm}_{m}"] = v
            scored.append(out)
    half = sorted({r["day"] for r in scored})[len({r["day"] for r in scored}) // 2]

    def cmp(arm, keep):
        res = {}
        for m in ("logscore", "crps", "brier"):
            per_day = defaultdict(list)
            for r in scored:
                if keep(r):
                    per_day[r["day"]].append(r[f"{arm}_{m}"] - r[f"R_{m}"])
            per_day = {k: v for k, v in per_day.items() if v}
            if not per_day:
                return None
            allv = [v for vs in per_day.values() for v in vs]
            ci = U.boot(per_day)
            res[m] = {"delta": round(statistics.mean(allv), 5), "ci": [round(ci[0], 5), round(ci[1], 5)]}
            if m == "logscore":
                res["n"], res["n_days"] = len(allv), len(per_day)
                res["days_better"] = sum(1 for v in per_day.values() if statistics.mean(v) < 0)
        return res

    slices = {
        "all": lambda r: True,
        "since_0_3": lambda r: r["since_h"] < 3, "since_3_6": lambda r: 3 <= r["since_h"] < 6,
        "since_6_12": lambda r: 6 <= r["since_h"] < 12, "since_12plus": lambda r: r["since_h"] >= 12,
        "local_before_day": lambda r: r["hour"] < 0, "local_00_10": lambda r: 0 <= r["hour"] < 10,
        "local_10_16": lambda r: 10 <= r["hour"] < 16, "local_16_23": lambda r: r["hour"] >= 16,
        "stage_P0": lambda r: r["stage"] == "P0", "stage_P0M": lambda r: r["stage"] == "P0-M", "stage_P1": lambda r: r["stage"] == "P1",
        "first_half": lambda r: r["day"] < half, "second_half": lambda r: r["day"] >= half,
    }
    out = {"decisions": len(rows), "kappa_path": path}
    for arm in ("P", "C"):
        out[arm] = {name: cmp(arm, f) for name, f in slices.items()}
        a = out[arm]["all"]
        out[arm]["verdict"] = ("INSUFFICIENT" if a["n_days"] < 20 else
                               "PASS" if a["logscore"]["ci"][1] < 0 and a["crps"]["ci"][0] <= 0 and a["brier"]["ci"][0] <= 0 else "REFUSED")
        by_st = defaultdict(list)
        for r in scored:
            by_st[r["station"]].append(r[f"{arm}_logscore"] - r["R_logscore"])
        out[arm]["stations"] = {s: (len(v), round(statistics.mean(v), 4)) for s, v in sorted(by_st.items())}
    (HERE / "persist_results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print("kappa:", path[0], path[len(path) // 2], path[-1])
    for arm in ("P", "C"):
        print(arm, out[arm]["verdict"])
        for name in slices:
            v = out[arm][name]
            if v is None:
                continue
            print(f"  {name:16s} LS {v['logscore']['delta']:+.4f} [{v['logscore']['ci'][0]:+.4f},{v['logscore']['ci'][1]:+.4f}]"
                  f" RPS {v['crps']['delta']:+.4f}{v['crps']['ci']} Br {v['brier']['delta']:+.4f}{v['brier']['ci']}"
                  f" n{v['n']} {v['days_better']}/{v['n_days']}d")
        print("  stations:", out[arm]["stations"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
