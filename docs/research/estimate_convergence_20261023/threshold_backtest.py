"""Entry threshold on the convergence confidence (user 2026-10-23 「用这个信心指数去查过去
入场的阈值和门槛多少，有多少真值命中收益率」).

Per settled station-day: walk the local hours; the first hour whose score >= T and
whose majority bucket has a YES ask in the official WS snapshot (within 30 min) is
the entry -- buy that bucket's YES at the best ask plus the taker fee
(rate x p x (1-p), rate 0.05), 1 U stake, hold to settlement.

The score at a day is read from a table built WITHOUT that event day (leave one
day out), so no day is scored with its own truth.  Snapshot best ask is not the
fillable price for a size (memory 10-11); 1 U is small.  Read only; research only.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import estimate_hit_readings as E  # noqa: E402
from bayes_basket_target.board_readings import DEFAULT_DB  # noqa: E402
from bayes_basket_target.realisation_store import taker_fee_per_share  # noqa: E402

FEE = 0.05
THRESHOLDS = (0, 40, 50, 60, 65, 70, 75, 80, 85, 90, 95)
MIN_HOUR = 0


def main() -> int:
    cache = {k: v for k, v in json.loads(E.CACHE_PATH.read_text()).items() if v and v.get("curve") and v.get("truth")}
    days = sorted({k.split("|")[1] for k in cache})
    tables = {d: E._convergence({k: v for k, v in cache.items() if k.split("|")[1] != d})["confidence"] for d in days}
    zones = None
    conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True, timeout=30)
    zones = E._zones(conn)
    from datetime import date, datetime, timedelta
    asks: dict[tuple[str, int], dict[str, float]] = {}

    def ask_at(key: str, h: int) -> dict[str, float]:
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

    # Every (station-day, hour) scored once.
    scored = {}
    for key, v in cache.items():
        if key.split("|")[0] not in zones:
            continue
        t = tables[key.split("|")[1]]
        scored[key] = [(h, E.confidence_score(t, h, v["curve"][str(h)])) for h in E.CURVE_HOURS if h >= MIN_HOUR]

    out = {}
    for T in THRESHOLDS:
        legs = []
        for key, seq in scored.items():
            for h, s in seq:
                if not s or s["score"] < T:
                    continue
                p = ask_at(key, h).get(s["bucket"])
                if p is None or not 0 < p < 1:
                    continue
                cost = p + taker_fee_per_share(p, FEE)
                hit = s["bucket"] == cache[key]["truth"]
                legs.append({"day": key.split("|")[1], "hour": h, "p": p, "hit": hit,
                             "pnl": (1.0 / cost if hit else 0.0) - 1.0, "score": s["score"]})
                break
        if not legs:
            continue
        n = len(legs)
        roi = sum(x["pnl"] for x in legs) / n
        by_day = {}
        for x in legs:
            by_day.setdefault(x["day"], []).append(x["pnl"])
        rng = random.Random(7)
        boots = []
        dl = list(by_day)
        for _ in range(2000):
            pick = [rng.choice(dl) for _ in dl]
            pnl = [y for d in pick for y in by_day[d]]
            boots.append(sum(pnl) / len(pnl))
        boots.sort()
        hours = sorted(x["hour"] for x in legs)
        out[T] = {"legs": n, "hit": round(sum(x["hit"] for x in legs) / n, 3), "mean_ask": round(sum(x["p"] for x in legs) / n, 3),
                  "roi": round(roi, 4), "ci90": [round(boots[100], 4), round(boots[1899], 4)],
                  "median_hour": hours[n // 2], "days_positive": sum(1 for d in by_day.values() if sum(d) > 0), "days": len(by_day)}
        print(T, out[T])
    (Path(__file__).resolve().parent / "threshold_reading.json").write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
