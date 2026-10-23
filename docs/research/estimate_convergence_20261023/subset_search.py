"""Which estimates agreeing pays (user 2026-10-23 「不一定是五家，你去看哪几家配合比较好」).

Every subset (>=2) of the lane's voters (F, member Bayes, D0, P1, market): at a
local hour, all members present and on one bucket -> buy that bucket's YES at
the WS best ask (>=0.05) plus the taker fee, 1 U, held.  One leg per
station-day per (subset, hour).  Reported with day-clustered intervals and the
two halves of the window (10-15..18 / 10-19..22).  26 subsets x 5 hours =
130 cells on 8 event days: the top of this table is mostly selection.
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

VOTERS = ("F", "member_bayes", "D0", "P1", "market")
HOURS = (10, 11, 12, 13, 14)
SPLIT = "2026-10-19"


def main() -> int:
    cache = {k: v for k, v in json.loads(E.CACHE_PATH.read_text()).items() if v and v.get("curve") and v.get("truth")}
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
    for size in range(2, len(VOTERS) + 1):
        for sub in itertools.combinations(VOTERS, size):
            for h in HOURS:
                legs = []
                for key, v in cache.items():
                    if key.split("|")[0] not in zones:
                        continue
                    picks = v["curve"][str(h)]
                    vals = [picks.get(m) for m in sub]
                    if any(x is None for x in vals) or len(set(vals)) != 1:
                        continue
                    b = vals[0]
                    p = ask_at(key, h).get(b)
                    if p is None or not 0.05 <= p < 1:
                        continue
                    hit = b == v["truth"]
                    cost = p + taker_fee_per_share(p, 0.05)
                    legs.append((key.split("|")[1], hit, p, (1 / cost if hit else 0) - 1,
                                 picks.get("market") == b))
                if len(legs) < 20:
                    continue
                byd = {}
                for d, _, _, r, _ in legs:
                    byd.setdefault(d, []).append(r)
                rng = random.Random(7)
                boots = []
                for _ in range(1000):
                    pick = [rng.choice(list(byd)) for _ in byd]
                    x = [y for d in pick for y in byd[d]]
                    boots.append(sum(x) / len(x))
                boots.sort()
                a = [r for d, _, _, r, _ in legs if d < SPLIT]
                b2 = [r for d, _, _, r, _ in legs if d >= SPLIT]
                n = len(legs)
                out.append({"subset": "+".join(sub), "hour": h, "n": n,
                            "hit": round(sum(x[1] for x in legs) / n, 3), "ask": round(sum(x[2] for x in legs) / n, 3),
                            "roi": round(sum(x[3] for x in legs) / n, 4), "ci90": [round(boots[50], 4), round(boots[949], 4)],
                            "first": round(sum(a) / len(a), 4) if a else None, "second": round(sum(b2) / len(b2), 4) if b2 else None,
                            "n_first": len(a), "n_second": len(b2),
                            "days_pos": sum(1 for r in byd.values() if sum(r) > 0), "days": len(byd),
                            "market_agrees": round(sum(x[4] for x in legs) / n, 2)})
    out.sort(key=lambda r: -r["roi"])
    (Path(__file__).resolve().parent / "subset_reading.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(len(out), "cells")
    for r in out[:20]:
        print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
