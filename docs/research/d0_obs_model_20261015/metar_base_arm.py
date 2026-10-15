"""D0 with a METAR starting point instead of the official forecast (user
2026-10-15 「测换成metar作为起点」).

Same rows, features, ridge, spread model and walk-forward as fit_d0.py; only
the anchor B the model shrinks toward changes (y = truth - B, and the one
feature built on B, high_minus_base, follows it):

  official  median official deterministic Tmax by local midnight (served)
  metar_now the newest METAR temperature at the instant
  metar_rise max(running high, METAR now + forecast warming left today)

Each arm full-features and anchor-only (station-bias corrected); paired
against the official arm per station-day, event-day block bootstrap.
"""

from __future__ import annotations

import copy
import json
import random
from collections import defaultdict

import numpy as np

import fit_d0 as F

ANCHORS = {
    "official": lambda r, raw: r["base"],
    "metar_now": lambda r, raw: raw["t"],
    "metar_rise": lambda r, raw: max(raw["high"], raw["t"] + max(0.0, raw["fc_rise"])),
}


def main() -> None:
    import gzip
    raw = {(x["station"], x["day"], x["hour"]): x for x in map(json.loads, gzip.open(F.DATA / "rows_d0.jsonl.gz", "rt"))}
    base_rows = F.load()
    arms = {}
    for name, fn in ANCHORS.items():
        rows = []
        for r in base_rows:
            x = raw[(r["station"], r["day"], r["hour"])]
            if x.get("t") is None:
                continue
            b = float(fn(r, x))
            q = copy.copy(r); q["feats"] = dict(r["feats"])
            q["base"] = b; q["y"] = r["truth"] - b; q["feats"]["high_minus_base"] = r["high"] - b
            rows.append(q)
        arms[name] = rows
    keyset = set.intersection(*({(r["station"], r["day"], r["hour"]) for r in v} for v in arms.values()))
    for k in arms:
        arms[k] = [r for r in arms[k] if (r["station"], r["day"], r["hour"]) in keyset]
    stations = sorted({r["station"] for r in base_rows})
    days = sorted({r["day"] for r in base_rows})
    acc = defaultdict(lambda: defaultdict(list))
    for band, (h0, h1) in F.BANDS.items():
        for i, day in enumerate(days):
            if i < F.MIN_TRAIN_DAYS:
                continue
            per = defaultdict(lambda: defaultdict(list))
            for name, rows in arms.items():
                rb = [r for r in rows if h0 <= r["hour"] < h1]
                train = [r for r in rb if r["day"] < day]; test = [r for r in rb if r["day"] == day]
                if not test or len(train) < 200:
                    continue
                m = F.fit_band(train, stations, True)
                bias = defaultdict(list)
                for r in train:
                    bias[r["station"]].append(r["y"])
                allb = float(np.mean([r["y"] for r in train])); sig = float(np.std([r["y"] for r in train]))
                sb = {s: (np.sum(v) + 3 * allb) / (len(v) + 3) for s, v in bias.items()}
                for r in test:
                    key = (r["day"], r["station"])
                    for arm, (c, s) in ((f"{name}|full", F.predict(m, r, stations, True)),
                                        (f"{name}|anchor", (r["base"] + sb.get(r["station"], allb), sig))):
                        v = F.vector(r, c, s)
                        lg, br = F.scores(v, r["k"])
                        per[(arm, "mae")][key].append(abs(c - r["truth"]))
                        per[(arm, "log")][key].append(lg)
                        per[(arm, "brier")][key].append(br)
                        if r["market"] is not None:
                            mk = F.to_vec(r, r["market"]["q"])
                            if mk is not None:
                                lgm, _ = F.scores(mk, r["k"])
                                per[(arm, "dlog_mkt")][key].append(lg - lgm)
            for (arm, metric), by in per.items():
                for key, vals in by.items():
                    acc[(band, metric, key)][arm] = float(np.mean(vals))
        print("band", band, "done", flush=True)
    rng = random.Random(20261015)
    out = {}
    for band in F.BANDS:
        for metric in ("mae", "log", "brier", "dlog_mkt"):
            for arm in [f"{n}|{k}" for n in ANCHORS for k in ("full", "anchor")]:
                by_day = defaultdict(list); diff = defaultdict(list)
                for (b, mt, key), d in acc.items():
                    if b != band or mt != metric or arm not in d:
                        continue
                    by_day[key[0]].append(d[arm])
                    if "official|full" in d:
                        diff[key[0]].append(d[arm] - d["official|full"])
                if not by_day:
                    continue
                pt = F.boot(by_day, rng)[0]
                dd = F.boot(diff, rng) if arm != "official|full" else (0, 0, 0)
                out[f"{band}|{metric}|{arm}"] = {"point": pt, "d_vs_official_full": dd}
                print(f"{band} {metric:8s} {arm:18s} {pt:+.4f}  vs official|full {dd[0]:+.4f} [{dd[1]:+.4f},{dd[2]:+.4f}]")
    (F.DATA / "metar_base_arm.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
