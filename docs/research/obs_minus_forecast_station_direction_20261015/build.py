"""Build rows.jsonl.gz: served hourly P1 + four obs-minus-forecast increments + market vectors at t, t+1h, t+3h, t+6h.

Read-only: SSOT snapshots (indexed as-of reads, one short connection per
station), the phase-0 inputs.db radiation table, and the 10-07 audited rows.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs/research/p1_forward_physics_20261015"))
import solar  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.proper_bet_lane import market_mode_bucket  # noqa: E402

AUDITED = ROOT / "docs/research/obs_increment_recheck_20261007/audited_rows.jsonl.gz"
INPUTS = ROOT / "docs/research/p1_forward_physics_20261015/inputs.db"
SSOT = ROOT / "data/bayes_basket_target_ssot.db"
HORIZONS = (0, 1, 3, 6)
MAX_AGE = timedelta(minutes=90)  # amendment: snapshots are written per decision, 30 min left 61% of rows without one


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def main():
    rows = [json.loads(l) for l in gzip.open(AUDITED, "rt")]
    inp = sqlite3.connect(INPUTS)
    rad = {}
    lag = {}
    for st, h, v, off in inp.execute("select station_id, hour_utc, swr_wm2, official from rad"):
        rad[(st, h)] = float(v)
        lag[st] = timedelta(minutes=20) if off else timedelta(minutes=75)
    elev = {r["icaoId"]: float(r["elev"]) for r in json.load(open(ROOT / "docs/research/p1_forward_physics_20261015/awc_stationinfo.json"))}
    by_station = defaultdict(list)
    for r in rows:
        by_station[r["station"]].append(r)
    out = gzip.open(HERE / "rows.jsonl.gz", "wt")
    kept = 0
    stats = defaultdict(int)
    for st, srows in sorted(by_station.items()):
        conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=5)
        for r in srows:
            t = utc(r["decided_at"])
            lh = float(r["local_hour"])
            fc = r["fc"]
            i = min(23, int(lh))
            fc_t = fc[i]["t"] + (fc[min(23, i + 1)]["t"] - fc[i]["t"]) * (lh - i)
            dT = float(r["temp_now"]) - fc_t if r.get("temp_now") is not None else None
            fc_cloud = fc[i]["cloud"]
            dC = (float(r["cloud_now"]) - fc_cloud / 100.0) if (r.get("cloud_now") is not None and fc_cloud is not None) else None
            fc_pr = fc[i]["pr"]
            dR = (float(bool(r.get("precip_now"))) - float((fc_pr or 0.0) > 0.1)) if r.get("precip_now") is not None else None
            # dSW: last two available daylight hours
            avail_end = (t - lag.get(st, timedelta(minutes=75))).replace(minute=0, second=0, microsecond=0)
            offset_h = round((lh - (t.hour + t.minute / 60.0)) % 24)  # local - utc, hours
            sw_pairs = []
            hs = avail_end - timedelta(hours=1)
            for _ in range(6):
                v = rad.get((st, hs.strftime("%Y-%m-%dT%H:00:00Z")))
                loc_h = (hs.hour + offset_h) % 24
                fsw = fc[loc_h]["sw"] if loc_h < 24 else None
                if v is not None and fsw is not None and fsw > 50.0:
                    sw_pairs.append((v, fsw))
                    if len(sw_pairs) == 2:
                        break
                hs -= timedelta(hours=1)
            dSW = None
            if sw_pairs:
                m = sum(a for a, _ in sw_pairs) / len(sw_pairs)
                f = sum(b for _, b in sw_pairs) / len(sw_pairs)
                dSW = (m - f) / max(f, 200.0)
            # market vectors
            mk = {}
            for h in HORIZONS:
                at = t + timedelta(hours=h)
                row = conn.execute(
                    "SELECT buckets_json, observed_at FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                    " AND state='COMMITTED' AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1",
                    (st, r["day"], stamp(at), stamp(at - MAX_AGE)),
                ).fetchone()
                if row is None:
                    stats[f"no_snap_h{h}"] += 1
                    continue
                buckets = json.loads(row[0] or "[]")
                ref = market_reference_v1(buckets)
                if ref.get("state") != "complete":
                    stats[f"incomplete_h{h}"] += 1
                    continue
                q = {str(k): float(v) for k, v in ref["vector"].items()}
                ords = {str(b["bucket"]): int(b["ordinal"]) for b in buckets if b.get("ordinal") is not None}
                order = sorted(q, key=lambda b: ords.get(b, 0))
                mk[str(h)] = {"q": q, "mode": market_mode_bucket(q, order), "observed_at": row[1], "age_min": (at - utc(row[1])).total_seconds() / 60.0}
            if "0" not in mk:
                stats["drop_no_market_t"] += 1
                continue
            rec = {
                "station": st, "day": r["day"], "decided_at": r["decided_at"], "local_hour": lh,
                "p1": r["p1"], "intervals": r["intervals"], "truth": float(r["truth"]),
                "running_high": float(r["running_high"]), "hard_excluded": r["hard_excluded"],
                "posterior_version": r["posterior_version"], "unit": "F" if "°F" in next(iter(r["intervals"])) else "C",
                "rad_official": int(lag.get(st) == timedelta(minutes=20)),
                "dT": dT, "dSW": dSW, "dCloud": dC, "dRain": dR, "market": mk,
            }
            out.write(json.dumps(rec) + "\n")
            kept += 1
        conn.close()
        print(st, len(srows), "kept so far", kept, flush=True)
    out.close()
    print("kept", kept, dict(stats))


if __name__ == "__main__":
    main()
