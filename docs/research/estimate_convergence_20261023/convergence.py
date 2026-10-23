"""Convergence timeline of the board's estimates (user 2026-10-23 「计算：确定性预报、
成员轨迹贝叶斯、D0 实况、观测更新型 MOS、P1 概率和市场的趋同时间线」).

Every settled station-day since 2026-10-15, read at each local hour 0..23 of the
event day with ``estimate_hit_readings``' own readers (same buckets, same
as-of rule).  Per method x hour: how often its bucket is the truth's, how far off
in buckets, and how often it agrees with the market.  Per station-day: the lock
hour -- the first hour from which the method stays on the truth's bucket.

Read only (SSOT ro, board history files; MOS from 10-19, after its fit window,
charges the board's Open-Meteo lane once per station-day).  Display/research only.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import board_readings  # noqa: E402
from bayes_basket_target import estimate_hit_readings as E  # noqa: E402

HOURS = tuple(range(24))
METHODS = ("F", "member_bayes", "D0", "MOS", "P1", "market")
OUT = Path(__file__).resolve().parent / "reading.json"
#: Settled station-days already read (resumable; MOS costs a network call each).
DAYS_CACHE = Path(__file__).resolve().parent / "station_days.json"


def picks_at(conn, st, day, zone, ladder, h, *, f, d0, mb, mos):
    local0 = datetime.combine(date.fromisoformat(day), datetime.min.time(), zone)
    at = E._iso(local0 + timedelta(hours=h))
    out = {"F": E._label_of(ladder, f)}
    row = conn.execute(
        "SELECT probabilities_json FROM basket_probability_envelope WHERE station_id=? AND event_date=?"
        " AND state='COMMITTED' AND +stage='P1' AND decision_at_utc<=? ORDER BY decision_at_utc DESC LIMIT 1",
        (st, day, at)).fetchone()
    out["P1"] = E._argmax(json.loads(row[0])) if row and row[0] else None
    lo = E._iso(local0 + timedelta(hours=h, minutes=-E.MARKET_WINDOW_MIN))
    row = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (st, day, at, lo)).fetchone()
    out["market"] = None
    if row and row[0]:
        from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1
        ref = market_reference_v1(json.loads(row[0]))
        if ref.get("state") == "complete":
            out["market"] = E._argmax({str(k): float(v) for k, v in ref["vector"].items()})
    out["member_bayes"] = E._latest(mb.get(st, []), at, "post_mode")
    out["D0"] = E._latest(d0.get(st, []), at, "mode")
    out["MOS"] = None
    if mos:
        past = [v for hour, v in mos if hour <= h]
        out["MOS"] = E._label_of(ladder, past[-1]) if past else None
    return out


def main() -> int:
    conn = sqlite3.connect(f"file:{board_readings.DEFAULT_DB}?mode=ro", uri=True, timeout=30)
    now = datetime.now(timezone.utc)
    zones, truths, f_ledger = E._zones(conn), E._truths(conn, E.SINCE), E._f_ledger()
    mos_from = E._mos_first_day()
    try:
        days = json.loads(DAYS_CACHE.read_text())
    except (OSError, ValueError):
        days = {}
    by_day: dict[str, list[str]] = {}
    for (st, day) in truths:
        if st in zones and datetime.combine(date.fromisoformat(day) + timedelta(days=1), datetime.min.time(), zones[st]) <= now:
            by_day.setdefault(day, []).append(st)
    for day in sorted(by_day):
        d0, mb = E._history("d0", day), E._history("member_bayes", day)
        for st in sorted(by_day[day]):
            zone, t = zones[st], truths[(st, day)]
            if f"{st}|{day}" in days:
                continue
            ladder = E._rungs(conn, st, day)
            if ladder is None:
                continue
            idx = {r[0]: i for i, r in enumerate(ladder[0])}
            mos = None
            if mos_from and date.fromisoformat(day) >= mos_from:
                try:
                    mos = E._mos_series(conn, st, day, zone)
                except Exception:  # noqa: BLE001
                    mos = None
            hours = {h: picks_at(conn, st, day, zone, ladder, h, f=f_ledger.get((st, day)), d0=d0, mb=mb, mos=mos)
                     for h in HOURS}
            days[f"{st}|{day}"] = {"truth": E._label_of(ladder, t), "idx": idx, "hours": {str(h): p for h, p in hours.items()}}
        DAYS_CACHE.write_text(json.dumps(days))
        print(day, len(days), flush=True)
    conn.close()

    table = {m: {} for m in METHODS}
    locks = {m: [] for m in METHODS}
    for v in days.values():
        tl, idx = v["truth"], v["idx"]
        for m in METHODS:
            seq = [(h, v["hours"][str(h)][m]) for h in HOURS if v["hours"][str(h)][m] is not None]
            for h, p in seq:
                c = table[m].setdefault(h, {"n": 0, "hit": 0, "dist": 0, "pair": 0, "agree": 0})
                c["n"] += 1
                c["hit"] += int(p == tl)
                c["dist"] += abs(idx.get(p, 0) - idx.get(tl, 0)) if p in idx and tl in idx else 0
                mk = v["hours"][str(h)]["market"]
                if m != "market" and mk is not None:
                    c["pair"] += 1
                    c["agree"] += int(p == mk)
            if seq:
                lock = None
                for h, p in reversed(seq):
                    if p != tl:
                        break
                    lock = h
                locks[m].append(lock if lock is not None else 24)
    # Confidence: at each hour, the bucket most methods point to and how many of
    # the methods present agree on it; how often that bucket was the truth's.
    conf: dict[int, dict[int, list[int]]] = {}
    for v in days.values():
        for h in HOURS:
            ps = [p for p in (v["hours"][str(h)][m] for m in METHODS) if p is not None]
            if len(ps) < 3:
                continue
            top = max(set(ps), key=ps.count)
            c = conf.setdefault(h, {}).setdefault(ps.count(top), [0, 0, 0])
            c[0] += 1
            c[1] += int(top == v["truth"])
            c[2] += len(ps)
    confidence = {str(h): {str(k): {"n": c[0], "hit": round(c[1] / c[0], 3), "methods_mean": round(c[2] / c[0], 1)}
                           for k, c in sorted(ks.items())} for h, ks in sorted(conf.items())}
    summary = {m: {str(h): {"n": c["n"], "hit": round(c["hit"] / c["n"], 3), "bucket_err": round(c["dist"] / c["n"], 2),
                            "agree_market": round(c["agree"] / c["pair"], 3) if c["pair"] else None}
                   for h, c in sorted(table[m].items())} for m in METHODS}
    lock = {m: {"n": len(x), "median_lock_hour": statistics.median(x) if x else None,
                "never_locked": sum(1 for y in x if y == 24)} for m, x in locks.items()}
    OUT.write_text(json.dumps({"as_of_utc": now.isoformat(), "station_days": len(days), "mos_from": str(mos_from),
                               "by_hour": summary, "lock": lock, "confidence": confidence}, ensure_ascii=False, indent=1))
    print(json.dumps({"station_days": len(days), "lock": lock}, ensure_ascii=False))
    for m in METHODS:
        print(m, " ".join(f"{h}:{s['hit']:.2f}/{s['agree_market'] if s['agree_market'] is not None else '-'}" for h, s in summary[m].items() if int(h) % 2 == 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
