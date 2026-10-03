#!/usr/bin/env python3
"""Flatten decisions.jsonl.gz into a bucket-level frame (cached as pickle)."""
import gzip
import json
import math
import os
import re

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "decisions.jsonl.gz")
CACHE = os.path.join(HERE, "buckets.pkl")
FLOOR = 1e-3  # charter 224's floor, as floor_probabilities applies it


def floor_vec(p):
    """Mirror of materialization.floor_probabilities: lift to eps, renormalise all."""
    vals = np.array(p, dtype=float)
    if not (vals < FLOOR).any():
        return vals
    lifted = np.maximum(vals, FLOOR)
    return lifted / lifted.sum()


def build():
    rows = []
    dec_id = 0
    with gzip.open(SRC, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            b = r["b"]
            names = [x[0] for x in b]
            p_raw = [x[2] for x in b]
            if any(v is None for v in p_raw):
                continue  # ladder mismatch between envelope and book
            p_raw = np.array(p_raw, dtype=float)
            if not (p_raw.sum() > 0):
                continue
            p = floor_vec(p_raw / p_raw.sum())
            q = r["q"]
            lin = r.get("lin") or {}
            sysids = lin.get("ensemble_system_ids_json")
            nsys = len(sysids) if isinstance(sysids, list) else np.nan
            try:
                mc = float(lin.get("model_center_c"))
            except (TypeError, ValueError):
                mc = np.nan
            m = re.search(r"_v(\d+)$", r["pv"] or "")
            ver = int(m.group(1)) if m else -1
            unit = "F" if "°F" in names[0] else "C"
            nb = len(b)
            y_ord = next((x[1] for x in b if x[0] == r["y"]), None)
            if y_ord is None:
                continue
            for i, x in enumerate(b):
                bids, asks = x[3], x[4]
                bid = bids[0][0] if bids else np.nan
                bsz = bids[0][1] if bids else 0.0
                ask = asks[0][0] if asks else np.nan
                asz = asks[0][1] if asks else 0.0
                rows.append((
                    dec_id, r["st"], r["d"], r["stage"], r["t"], r["h"], r["first"], ver,
                    unit, nb, x[0], x[1], float(p[i]), (q.get(x[0]) if q else np.nan),
                    bid, bsz, ask, asz, x[5], 1 if x[0] == r["y"] else 0, y_ord,
                    r["ms"] if r["ms"] is not None else np.nan, r["qr"], nsys, mc,
                    lin.get("lead_bucket"), lin.get("run_cycle"), r["tmax"],
                ))
            dec_id += 1
    cols = ["dec", "st", "d", "stage", "t", "h", "first", "ver", "unit", "nb", "k", "ord", "p", "q",
            "bid", "bsz", "ask", "asz", "tick", "y", "y_ord", "ms", "qr", "nsys", "mc",
            "lead", "run", "tmax"]
    df = pd.DataFrame(rows, columns=cols)
    df.to_pickle(CACHE)
    return df


def load():
    if os.path.exists(CACHE) and os.path.getmtime(CACHE) > os.path.getmtime(SRC):
        return pd.read_pickle(CACHE)
    return build()


if __name__ == "__main__":
    df = build()
    print(df.shape, df["dec"].nunique(), "decisions")
    dec = df.drop_duplicates("dec")
    print("station-days:", dec.groupby(["st", "d"]).ngroups)
    print(dec.groupby("stage").size())
    print("q complete:", dec["q"].notna().mean() if False else df.groupby("dec")["q"].first().notna().mean())
    print(dec["qr"].fillna("complete").str.split(":").str[0].value_counts())
    dec = dec.assign(week=dec["d"].str.slice(0, 10))
    piv = dec.pivot_table(index="d", columns="stage", values="dec", aggfunc="count").fillna(0).astype(int)
    print(piv.to_string())
