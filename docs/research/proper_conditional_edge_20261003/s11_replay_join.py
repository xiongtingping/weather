"""Join the walk-forward replay of the 10-02 chain (charter 247 form, 1 degC grid) to the served decisions' books.
   Celsius stations only: their market buckets ARE the 1 degC grid."""
import bisect, gzip, json, os, re
import numpy as np, pandas as pd
from load import load, floor_vec

def _ts(value):
    return pd.Timestamp(value).tz_convert("UTC") if pd.Timestamp(value).tzinfo else pd.Timestamp(value, tz="UTC")


SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downstream_after_247_20261002", "pass1_down")
df = load()
C = df[(df.unit == "C") & (df.stage == "P0")].copy()
out_p = np.full(len(C), np.nan); out_at = np.empty(len(C), dtype=object); out_seat = np.full(len(C), np.nan)
C["rp"] = np.nan; C["rat"] = None; C["rseat"] = np.nan
pos = {idx: i for i, idx in enumerate(C.index)}
stations = sorted(C.st.unique())
n_match = 0; n_dec = 0
for st in stations:
    path = os.path.join(SRC, f"{st}.jsonl.gz")
    if not os.path.exists(path):
        print("no replay for", st); continue
    by_day = {}
    with gzip.open(path, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            base = r.get("base")
            if not base or not base.get("vec"): continue
            by_day.setdefault(r["event_date"], []).append((r["at"], {int(k): float(v) for k, v in base["vec"].items()}, base.get("seated")))
    S = C[C.st == st]
    for (d, dec), g in S.groupby(["d", "dec"], sort=False):
        n_dec += 1
        rows = by_day.get(d)
        if not rows: continue
        rows.sort(key=lambda x: x[0])
        # Whole timestamps, not seconds: truncating both sides let a replay
        # update up to a second AFTER the decision through (review 2026-10-03).
        t = _ts(g.t.iloc[0])
        ats = [_ts(x[0]) for x in rows]
        i = bisect.bisect_right(ats, t) - 1
        if i < 0: continue
        at, vec, seated = rows[i]
        vals = []
        ok = True
        for name in g.k:
            m = re.match(r"^(-?\d+)°C$", name)
            if m: vals.append(vec.get(int(m.group(1)), 0.0)); continue
            m = re.match(r"^(-?\d+)°C or below$", name)
            if m: vals.append(sum(v for k, v in vec.items() if k <= int(m.group(1)))); continue
            m = re.match(r"^(-?\d+)°C or higher$", name)
            if m: vals.append(sum(v for k, v in vec.items() if k >= int(m.group(1)))); continue
            ok = False; break
        if not ok or sum(vals) < 0.98: continue
        p = floor_vec(np.array(vals) / sum(vals))
        ii = [pos[x] for x in g.index]
        out_p[ii] = p; out_at[ii] = at; out_seat[ii] = seated if seated is not None else np.nan
        n_match += 1
C["rp"] = out_p; C["rat"] = out_at; C["rseat"] = out_seat
C = C[C.rp.notna()].copy()
C["lag_h"] = (pd.to_datetime(C.t, format="ISO8601") - pd.to_datetime(C.rat, format="ISO8601")).dt.total_seconds() / 3600
C.to_pickle("replay_c.pkl")
print(f"C-station P0 decisions {n_dec}, matched to a replay vector {n_match}")
dec = C.drop_duplicates("dec")
print("lag from replay update to served decision (h):", dec.lag_h.describe()[["50%", "75%", "max"]].round(2).to_dict())
print(dec.groupby("d").size().to_string())
