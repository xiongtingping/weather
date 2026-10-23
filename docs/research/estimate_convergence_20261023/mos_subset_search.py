"""MOS in the combinations (user 2026-10-23 「评估mos组合呢」).

MOS is out of sample only after its fit window (10-19 on) and has no
satellite input at about twenty stations, so this reads the station-days with
a MOS record (``station_days.json``, written by ``convergence.py``): 10-19..22.
Every subset (>=2) of F, member Bayes, D0, MOS, P1, market, local 10..14 h,
all present and on one bucket -> YES at the WS best ask (>=0.05) + fee.  The
current lane rule (D0+P1+market) is read on the same station-days.  Four event
days: a reading, not a basis.
"""

from __future__ import annotations

import itertools
import json
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import estimate_hit_readings as E  # noqa: E402
from bayes_basket_target.board_readings import DEFAULT_DB  # noqa: E402
from bayes_basket_target.realisation_store import taker_fee_per_share  # noqa: E402

HERE = Path(__file__).resolve().parent
VOTERS = ("F", "member_bayes", "D0", "MOS", "P1", "market")
HOURS = (10, 11, 12, 13, 14)


def main() -> int:
    rows = json.loads((HERE / "station_days.json").read_text())
    days = {k: v for k, v in rows.items()
            if v.get("truth") and any((v["hours"].get(str(h)) or {}).get("MOS") for h in range(24))}
    conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True, timeout=30)
    zones = E._zones(conn)
    asks = {}

    def ask_at(key, h):
        if (key, h) not in asks:
            st, day = key.split("|")
            local0 = datetime.combine(date.fromisoformat(day), datetime.min.time(), zones[st])
            at, lo = E._iso(local0 + timedelta(hours=h)), E._iso(local0 + timedelta(hours=h, minutes=-30))
            row = conn.execute(
                "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                " AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (st, day, at, lo)).fetchone()
            asks[(key, h)] = {b["bucket"]: float(b["best_ask"]) for b in json.loads(row[0])
                              if b.get("ask_available") and b.get("best_ask")} if row and row[0] else {}
        return asks[(key, h)]

    out = []
    subsets = [s for n in range(2, len(VOTERS) + 1) for s in itertools.combinations(VOTERS, n)]
    for sub in subsets:
        for h in HOURS:
            legs = []
            for key, v in days.items():
                if key.split("|")[0] not in zones:
                    continue
                picks = v["hours"].get(str(h)) or {}
                vals = [picks.get(m) for m in sub]
                if any(x is None for x in vals) or len(set(vals)) != 1:
                    continue
                p = ask_at(key, h).get(vals[0])
                if p is None or not 0.05 <= p < 1:
                    continue
                hit = vals[0] == v["truth"]
                cost = p + taker_fee_per_share(p, 0.05)
                legs.append((key.split("|")[1], hit, p, (1 / cost if hit else 0) - 1))
            if len(legs) < 10:
                continue
            byd = {}
            for d, _, _, r in legs:
                byd.setdefault(d, []).append(r)
            rng = random.Random(7)
            boots = []
            for _ in range(1000):
                pick = [rng.choice(list(byd)) for _ in byd]
                x = [y for d in pick for y in byd[d]]
                boots.append(sum(x) / len(x))
            boots.sort()
            n = len(legs)
            out.append({"subset": "+".join(sub), "hour": h, "n": n, "has_mos": "MOS" in sub,
                        "hit": round(sum(x[1] for x in legs) / n, 3), "ask": round(sum(x[2] for x in legs) / n, 3),
                        "roi": round(sum(x[3] for x in legs) / n, 4), "ci90": [round(boots[50], 4), round(boots[949], 4)],
                        "days_pos": sum(1 for r in byd.values() if sum(r) > 0), "days": len(byd)})
    out.sort(key=lambda r: -r["roi"])
    (HERE / "mos_subset_reading.json").write_text(json.dumps({"station_days": len(days), "cells": out}, ensure_ascii=False, indent=1))
    print("station-days with MOS", len(days))
    for r in out:
        if r["hour"] == 12 and (r["has_mos"] or r["subset"] in ("D0+P1+market", "D0+market")):
            print(r)
    print("-- top 12 overall")
    for r in out[:12]:
        print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
