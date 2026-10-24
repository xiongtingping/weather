"""Do charter 266's direction (relative to the market's modal bucket) and the
board MOS's changes carry information about the truth -- prices and the book
left out (user 2026-10-25 「在不考虑市场价格和盘口的情况下：方向（宪章 266 ·
相对市场众数）和mos的变化，相对于真值，是否有信息？」).

Direction: the round-68 sampling (the first bridge decision with a complete
market ladder in each band 12-24 / 24-36 / 36-60 h before settlement, at most
8 tried), the market's modal bucket there, and the truth's side of it
y in {+1 warmer, 0 on the mode, -1 colder}.  Signals: momentum M (|M|>=1,
unfitted, no look-ahead), the four-feature model side (physics z from the
production cache -- its baseline days include days after the event day, a
small look-ahead; only days the cache covers), and the combined side (M when
it speaks, else the model).  Read: hit rate P(y=s | y!=0) against 0.5, and the
warm-rate gap P(y=+1|s=+1) - P(y=+1|s=-1) over all rows (base-rate free).
No price enters.

MOS: the board's composite estimate per local hour as the estimate-hit reading
cached it (bucket labels, out of sample from its fit window's end, 10-19 on).
Each hour-to-hour change of its bucket: did it move toward the truth (sign of
the change = sign of truth - the bucket before), and did it end nearer.
Also the change's sign against the truth's side of the market mode at that
hour (the market's mode label only -- no price).

Event-day block bootstrap, 95%.  Read-only: SSOT, the physics cache, the
estimate-hit cache; no network.

    .venv/bin/python -I docs/research/direction_mos_truth_20261025/analyze.py
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import board_readings as br  # noqa: E402
from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
SINCE = "2026-09-06"
FORWARD_FROM = "2026-10-09"   # after charter 266's fit window (09-22..10-08)
BANDS = (("12–24h", 12.0, 24.0), ("24–36h", 24.0, 36.0), ("36–60h", 36.0, 60.0))
TRIES = 8
DRAWS = 2000
SEED = 20261025
VERDICTS = ROOT / "data" / "runtime" / "estimate_hit" / "verdicts_v1.json"
MOS_FROM = "2026-10-19"


def sgn(x):
    return 0 if x is None or x == 0 else (1 if x > 0 else -1)


def boot(rows, stat, used=None):
    """Point and 95% event-day block bootstrap of stat(rows); n counts the
    rows the statistic reads (``used``), not every row passed."""
    by = defaultdict(list)
    for r in rows:
        by[r["day"]].append(r)
    days = sorted(by)
    point = stat(rows)
    n = len(rows) if used is None else sum(1 for r in rows if used(r))
    if point is None or len(days) < 3:
        return point, None, n, len(days)
    rng = random.Random(SEED)
    draws = []
    for _ in range(DRAWS):
        sample = [r for _ in days for r in by[days[rng.randrange(len(days))]]]
        v = stat(sample)
        if v is not None:
            draws.append(v)
    draws.sort()
    return point, (draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]), n, len(days)


def fmt(res, pct=True):
    m, ci, n, d = res
    if m is None:
        return f"— n={n}"
    f = (lambda x: f"{100 * x:+.1f}") if pct else (lambda x: f"{x:+.3f}")
    return f"{f(m)} [{f(ci[0])},{f(ci[1])}] n={n} d={d}" if ci else f"{f(m)} n={n} d={d}"


def hit_minus_half(key):
    def stat(rows):
        rs = [r for r in rows if r[key] and r["y"]]
        return None if not rs else sum(r[key] == r["y"] for r in rs) / len(rs) - 0.5
    return stat


def warm_gap(key):
    def stat(rows):
        w = [r for r in rows if r[key] == 1]
        c = [r for r in rows if r[key] == -1]
        if not w or not c:
            return None
        return sum(r["y"] == 1 for r in w) / len(w) - sum(r["y"] == 1 for r in c) / len(c)
    return stat


def build_direction(conn):
    labels = {k: v for k, v in knowable_labels(conn).items() if str(k[1]) >= SINCE}
    cache = json.loads((ROOT / "data" / "runtime" / "direction_physics_v1.json").read_text())["stations"]
    served, market = {}, {}
    rows = []
    for (station, day), label in sorted(labels.items()):
        station, day = str(station), str(day)
        instants = [str(r[0]) for r in conn.execute(
            "SELECT DISTINCT decision_at_utc FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY decision_at_utc", (station, day))]
        if not instants:
            continue
        tz = conn.execute("SELECT settlement_timezone FROM basket_probability_bridge_history WHERE station_id=?"
                          " AND event_date=? AND state='COMMITTED' LIMIT 1", (station, day)).fetchone()[0]
        settled = str(label["bucket"])
        for band, lo, hi in BANDS:
            cands = [a for a in instants
                     if (h := hours_to_settlement_if_known(day, as_of_utc=a, settlement_timezone=tz)) is not None
                     and lo <= h < hi][:TRIES]
            picked = None
            for at in cands:
                q = br._ladder_market_reference(conn, station, day, at, market)
                if q is None:
                    continue
                _stage, p, order, _rung = br._served_belief_vector(conn, station, day, at, served)
                if p is None or order is None or set(q) != set(order) or settled not in q:
                    continue
                picked = (at, q, order)
                break
            if picked is None:
                continue
            at, q, order = picked
            mode = br._ladder_mode(q, order)
            mom = ds.momentum(conn, station_id=station, event_date=day, at_utc=at)
            m_side = sgn(mom["M"]) if mom["M"] is not None and abs(mom["M"]) >= ds.MOMENTUM_QUIET_BELOW else 0
            feats = ds.physics_features(cache.get(station), event_date=day, at_utc=at)
            has_phys = any(feats.get(k) is not None for k in ("icon_shortwave", "dew_point_consensus", "ecmwf_wind_inverse"))
            model_side = sgn(ds.model_score(mom["M"], feats)) if has_phys else 0
            rows.append({"station": station, "day": day, "band": band, "unit": "F" if "°F" in settled else "C",
                         "y": sgn(order[settled] - order[mode]), "M": m_side, "model": model_side,
                         "combined": ds.combined_side(m_side, model_side) if has_phys else m_side,
                         "has_phys": has_phys})
    return rows


def build_mos():
    doc = json.loads(VERDICTS.read_text())
    ev = []
    for key, v in doc.items():
        if not v or v.get("truth") is None:
            continue
        station, day = key.split("|")
        if day < MOS_FROM:
            continue
        rank = v["rank"]
        t = rank.get(v["truth"])
        if t is None:
            continue
        prev = None
        for h in range(24):
            pick = (v["curve"].get(str(h)) or {})
            lab = pick.get("MOS")
            if lab is None or lab not in rank:
                continue
            if prev is not None and rank[lab] != rank[prev]:
                mk = pick.get("market")
                ev.append({"day": day, "station": station, "hour": h,
                           "d": sgn(rank[lab] - rank[prev]),
                           "toward": sgn(t - rank[prev]),
                           "nearer": abs(t - rank[lab]) < abs(t - rank[prev]),
                           "farther": abs(t - rank[lab]) > abs(t - rank[prev]),
                           "ym": sgn(t - rank[mk]) if mk in rank else None,
                           "on_mode_before": prev == mk})
            prev = lab
    return ev


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    rows = build_direction(conn)
    days = sorted({r["day"] for r in rows})
    print(f"方向样本 {len(rows)} 行，事件日 {days[0]}..{days[-1]}（{len(days)} 天）；有物理特征 {sum(r['has_phys'] for r in rows)}")
    ys = [r["y"] for r in rows]
    print(f"真值相对众数：暖侧 {ys.count(1)} / 众数上 {ys.count(0)} / 冷侧 {ys.count(-1)}")
    print("\n== 方向：命中率−50%（只数真值离开众数的行） | 暖侧率差 P(暖|信号暖)−P(暖|信号冷)（全部行）")
    for key, name in (("M", "动量 M（|M|≥1）"), ("model", "四特征模型分"), ("combined", "合成方向（M 优先）")):
        for seg, rs in (("全部", rows), ("拟合窗内 <10-09", [r for r in rows if r["day"] < FORWARD_FROM]),
                        ("前推 ≥10-09", [r for r in rows if r["day"] >= FORWARD_FROM])):
            if key != "M":
                rs = [r for r in rs if r["has_phys"]]
            print(f"{name} · {seg}: 命中 {fmt(boot(rs, hit_minus_half(key), lambda r: r[key] and r["y"]))} | 暖侧率差 {fmt(boot(rs, warm_gap(key), lambda r: r[key]))}")
        for band, _lo, _hi in BANDS:
            rs = [r for r in rows if r["band"] == band and (key == "M" or r["has_phys"])]
            print(f"   {band}: 命中 {fmt(boot(rs, hit_minus_half(key), lambda r: r[key] and r["y"]))}")
        for u in ("C", "F"):
            rs = [r for r in rows if r["unit"] == u and (key == "M" or r["has_phys"])]
            print(f"   {u} 站: 命中 {fmt(boot(rs, hit_minus_half(key), lambda r: r[key] and r["y"]))}")
        on = [r for r in rows if r[key] and (key == "M" or r["has_phys"])]
        if on:
            print(f"   信号开口时真值正落众数 {sum(r['y'] == 0 for r in on) / len(on):.0%}（n={len(on)}）")

    ev = build_mos()
    edays = sorted({e["day"] for e in ev})
    print(f"\n== MOS 档位变化（样本外 {MOS_FROM} 起）：{len(ev)} 次变化，事件日 {edays}")

    def toward(rs):
        r2 = [e for e in rs if e["toward"]]
        return None if not r2 else sum(e["d"] == e["toward"] for e in r2) / len(r2) - 0.5

    def nearer(rs):
        return None if not rs else sum(e["nearer"] for e in rs) / len(rs) - sum(e["farther"] for e in rs) / len(rs)

    def vs_mode(rs):
        r2 = [e for e in rs if e["ym"]]
        return None if not r2 else sum(e["d"] == e["ym"] for e in r2) / len(r2) - 0.5

    for seg, lo, hi in (("全部", 0, 23), ("00–09 点", 0, 9), ("10–13 点", 10, 13), ("14–17 点", 14, 17), ("18–23 点", 18, 23)):
        rs = [e for e in ev if lo <= e["hour"] <= hi]
        print(f"{seg}: 朝真值方向 {fmt(boot(rs, toward, lambda e: e["toward"]))} | 变近−变远 {fmt(boot(rs, nearer))}"
              f" | 对真值在市场众数哪侧 {fmt(boot(rs, vs_mode, lambda e: e["ym"]))}")
    rs = [e for e in ev if e["on_mode_before"]]
    print(f"变化前 MOS 正在市场众数上：对真值在众数哪侧 {fmt(boot(rs, vs_mode, lambda e: e["ym"]))}")


if __name__ == "__main__":
    main()
