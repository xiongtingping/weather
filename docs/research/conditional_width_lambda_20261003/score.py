#!/usr/bin/env python3
"""PROTOCOL.md: pass1_c -> rows_c/<station>.json.

Representative decisions (per station-day and hours-to-local-day-end bin, the
bin's last update), their features, and (LogScore, Brier, CRPS) at every
lambda of the fine grid -- production ``apply_served_table_layers`` on the
stored pre-width vector with today's P0 layer, lambda replaced, scored by the
replay's own ``_score``.  Asserts the lambda-0.75 score equals the one the
capture recorded through the full chain.

    python3 score.py CYYZ,EDDM ...
"""
import dataclasses
import gzip
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.served_shape import apply_served_table_layers  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
import replay_p0_stable_pool as replay  # noqa: E402

GRID = tuple(round(0.5 + 0.025 * i, 3) for i in range(41))
BINS = ((48, 1e9), (36, 48), (24, 36), (12, 24), (0, 12))


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main(stations):
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
    os.makedirs(os.path.join(HERE, "rows_c"), exist_ok=True)
    for station in stations:
        layer = oos.p0_gate_tables(conn, station_id=station)["center_width_layer"]
        assert abs(layer.width_lambda - 0.75) < 1e-12 and abs(layer.delta_c) < 1e-12
        recs = [json.loads(line) for line in gzip.open(os.path.join(HERE, "pass1_c", f"{station}.jsonl.gz"), "rt")]
        by_day = defaultdict(list)
        for r in recs:
            by_day[r["event_date"]].append(r)
        out, worst, excused = [], 0.0, 0
        for day, rs in sorted(by_day.items()):
            end = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(rs[0]["tz"])) + timedelta(days=1)
            for lo, hi in BINS:
                inbin = [r for r in rs if lo <= (end - utc(r["at"])).total_seconds() / 3600 < hi]
                if not inbin:
                    continue
                r = max(inbin, key=lambda x: x["at"])
                b = r["base"]
                keys = list(b["pre"])
                kc = [float(int(k)) for k in keys]
                total = sum(b["pre"].values())
                pre = {k: v / total for k, v in b["pre"].items()}
                mean = sum(c * pre[k] for k, c in zip(keys, kc))
                sigma = math.sqrt(sum((c - mean) ** 2 * pre[k] for k, c in zip(keys, kc)))
                s, sf = {}, {}
                truth = oos._temperature_bucket(r["actual"])
                for lam in GRID:
                    v, _ = apply_served_table_layers(pre, ordered_keys=keys, key_centers_c=kc,
                                                     center_width=dataclasses.replace(layer, width_lambda=lam))
                    crps, nll, brier, _b, _a = replay._score(v, r["actual"])
                    s[f"{lam:.3f}"] = [nll, brier, crps]
                    # Charter 224: the served P0 is the floored vector; a truth
                    # bucket off the integer grid joins at zero first (the
                    # market ladder's open ends always hold the truth).
                    vf, _floored = floor_probabilities({**({truth: 0.0} if truth not in v else {}), **v})
                    m = oos._metrics(vf, r["actual"])
                    sf[f"{lam:.3f}"] = [round(m["nll"], 6), round(m["brier"], 6), round(m["crps"], 6)]
                d_nll = abs(s["0.750"][0] - b["served"][1])
                if b["served"][1] > -math.log(1e-4):
                    excused += d_nll > 2e-4
                else:
                    worst = max(worst, d_nll)
                out.append({"station": station, "day": day, "known": utc(r["truth_known_at"]).isoformat(), "bin": lo,
                            "h": round((end - utc(r["at"])).total_seconds() / 3600, 3), "nf": b["nf"], "seated": b["seated"],
                            "D": b["D"], "sigma": round(sigma, 5), "state": b["fusion_state"], "s": s, "sf": sf})
        assert worst < 2e-4, (station, worst)
        with open(os.path.join(HERE, "rows_c", f"{station}.json"), "w") as handle:
            json.dump(out, handle)
        print(station, len(out), "parity max |dNLL| %.2e" % worst, "excused tail rows", excused, flush=True)
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1].split(","))
