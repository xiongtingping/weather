import gzip, json, os, re
import numpy as np, pandas as pd
from load import floor_vec, HERE

def build_open(src="decisions_open.jsonl.gz"):
    rows = []; dec_id = 10_000_000
    with gzip.open(os.path.join(HERE, src), "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line); b = r["b"]
            p_raw = [x[2] for x in b]
            if any(v is None for v in p_raw): continue
            p_raw = np.array(p_raw, float)
            if not p_raw.sum() > 0: continue
            p = floor_vec(p_raw / p_raw.sum()); q = r["q"]
            m = re.search(r"_v(\d+)$", r["pv"] or ""); ver = int(m.group(1)) if m else -1
            lin = r.get("lin") or {}; sysids = lin.get("ensemble_system_ids_json")
            for i, x in enumerate(b):
                bids, asks = x[3], x[4]
                rows.append((dec_id, r["st"], r["d"], r["stage"], r["t"], r["h"], r["first"], ver, x[1], float(p[i]),
                             (q.get(x[0]) if q else np.nan), bids[0][0] if bids else np.nan, asks[0][0] if asks else np.nan,
                             x[5], len(sysids) if isinstance(sysids, list) else np.nan))
            dec_id += 1
    return pd.DataFrame(rows, columns=["dec", "st", "d", "stage", "t", "h", "first", "ver", "ord", "p", "q", "bid", "ask", "tick", "nsys"])
