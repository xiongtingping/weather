"""Round 74 (PROTOCOL.md): physics as-of the listing instant vs ZUUU's F cold days.

Read-only on the SSOT.  raw, e, the ladder and the production F (A0) come from
round 73's walk (``zuuu_f_mode_target_r73_20261018/walk.jsonl``, built by the
production helpers as of each listing instant); this script adds the physics
features read as of the same instant and scores PROTOCOL.md's two tests.

    uv run python -I docs/research/zuuu_f_cold_days_physics_r74_20261018/run.py
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import listing_forecast_f as L  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
R73 = ROOT / "docs" / "research" / "zuuu_f_mode_target_r73_20261018"
FOCUS = "ZUUU"
FEATURES = ("cloudcover_mean", "precipitation_sum", "shortwave_radiation_sum", "wet_hours")
COLD, OK = 2.5, 1.0
TRAIN_DAYS, MIN_TRAIN = 60, 10
MIN_ROWS, MIN_DAYS = 200, 10
BOOT, SEED = 2000, 7
BONF = 0.0125


def features(conn, station, day):
    listing = L.listing_instant(date.fromisoformat(day))
    out = {}
    row = conn.execute(
        "SELECT cloudcover_mean, precipitation_sum, shortwave_radiation_sum FROM calibration_station_forecast_physics_version"
        " WHERE station_id=? AND local_date=? AND captured_at_utc<=? ORDER BY captured_at_utc DESC LIMIT 1",
        (station, day, listing)).fetchone()
    if row:
        out.update(zip(FEATURES[:3], row))
    row = conn.execute(
        "SELECT wet_hours FROM calibration_station_forecast_precip_hourly_version"
        " WHERE station_id=? AND local_date=? AND captured_at_utc<=? ORDER BY captured_at_utc DESC LIMIT 1",
        (station, day, listing)).fetchone()
    if row:
        out["wet_hours"] = row[0]
    return {k: float(v) for k, v in out.items() if v is not None}


def auc(pos, neg):
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def boot_auc(rows, key):
    rng = random.Random(SEED)
    days = [r["day"] for r in rows]
    by = {r["day"]: r for r in rows}
    vals = []
    for _ in range(BOOT):
        pick = [by[rng.choice(days)] for _ in days]
        a = auc([r[key] for r in pick if r["cold"]], [r[key] for r in pick if not r["cold"]])
        if a is not None:
            vals.append(a)
    vals.sort()
    return vals[int(BONF / 2 * len(vals))], vals[int((1 - BONF / 2) * len(vals)) - 1]


def test_one(walk, phys):
    rows = []
    for r in walk:
        if r["station"] != FOCUS:
            continue
        e = r["truth"] - r["arms"]["A_raw"]
        if not (e >= COLD or abs(e) < OK):
            continue
        rows.append({"day": r["day"], "e": e, "cold": e >= COLD, "date_ord": date.fromisoformat(r["day"]).toordinal(),
                     **phys.get((FOCUS, r["day"]), {})})
    print(f"## 检验一：ZUUU 冷偏(e≥{COLD}) vs 准(|e|<{OK})，挂牌时刻 as-of")
    print(f"可标注日 {len(rows)}（冷偏 {sum(r['cold'] for r in rows)}、准 {sum(not r['cold'] for r in rows)}）")
    for key in (*FEATURES, "date_ord"):
        sub = [r for r in rows if key in r]
        a = auc([r[key] for r in sub if r["cold"]], [r[key] for r in sub if not r["cold"]])
        if a is None:
            print(f"{key:24s} 不可算（n={len(sub)}）")
            continue
        lo, hi = boot_auc(sub, key)
        mid = sorted(r["day"] for r in sub)[len(sub) // 2]
        halves = []
        for part in ([r for r in sub if r["day"] < mid], [r for r in sub if r["day"] >= mid]):
            halves.append(auc([r[key] for r in part if r["cold"]], [r[key] for r in part if not r["cold"]]))
        cm = [r[key] for r in sub if r["cold"]]
        om = [r[key] for r in sub if not r["cold"]]
        hs = " / ".join("—" if h is None else f"{h:.2f}" for h in halves)
        print(f"{key:24s} n={len(sub):2d} AUC {a:.2f} [{lo:.2f}, {hi:.2f}]  两半 {hs}"
              f"  中位 冷偏 {statistics.median(cm):.1f} 准 {statistics.median(om):.1f}")
    print("露点：挂牌时刻无当日值，按协议不可检验")


def lae(X, y):
    mu, sd = X.mean(0), X.std(0)
    sd = np.where(sd > 1e-9, sd, 1.0)
    A = np.c_[(X - mu) / sd, np.ones(len(X))]
    n, k = A.shape
    pen = np.diag([2e-3] * (k - 1) + [0.0])
    beta = np.linalg.lstsq(A, y, rcond=None)[0]
    for _ in range(200):
        w = 1.0 / np.maximum(np.abs(y - A @ beta), 1e-4)
        step = np.linalg.solve(A.T @ (A * w[:, None]) / n + pen, (A * w[:, None]).T @ y / n)
        done = np.max(np.abs(step - beta)) < 1e-9
        beta = step
        if done:
            break
    return mu, sd, beta


def test_two(conn, walk, phys):
    zones = {}
    for r in walk:
        zones.setdefault(r["station"], None)
    rows = []
    for r in walk:
        f = phys.get((r["station"], r["day"]), {})
        rows.append({**r, "e": r["truth"] - r["arms"]["A_raw"], "f": f,
                     "settled": L._settled_at(date.fromisoformat(r["day"]), r["zone"]),
                     "listing": L._listing_dt(date.fromisoformat(r["day"]))})
    by_station = defaultdict(list)
    for r in rows:
        by_station[r["station"]].append(r)
    full = [r for r in rows if all(k in r["f"] for k in FEATURES)]
    out = []
    cache = {}
    for r in full:
        d = date.fromisoformat(r["day"])
        # Round 73's A_med is raw + the station's as-of median e: the same median here.
        med = r["arms"]["A_med"] - r["arms"]["A_raw"]
        if r["day"] not in cache:
            tr = []
            for q in full:
                qd = date.fromisoformat(q["day"])
                if q["settled"] > r["listing"] or not (d - timedelta(days=TRAIN_DAYS) <= qd < d):
                    continue
                tr.append(([q["f"][k] for k in FEATURES], q["e"] - (q["arms"]["A_med"] - q["arms"]["A_raw"]), q["day"]))
            ok = len(tr) >= MIN_ROWS and len({t[2] for t in tr}) >= MIN_DAYS
            cache[r["day"]] = lae(np.array([t[0] for t in tr]), np.array([t[1] for t in tr])) if ok else None
        model = cache[r["day"]]
        if model is None:
            continue
        mu, sd, beta = model
        z = (np.array([r["f"][k] for k in FEATURES]) - mu) / sd
        adj = float(z @ beta[:-1] + beta[-1])
        arms = {"A0": r["arms"]["A0"], "A_med": r["arms"]["A_raw"] + med, "A_phys": r["arms"]["A_raw"] + med + adj}
        out.append({"station": r["station"], "day": r["day"], "truth": r["truth"], "arms": arms,
                    "beta": [round(float(b), 3) for b in beta], "adj": round(adj, 3)})
    rungs_cache = {}
    for r in out:
        key = (r["station"], r["day"])
        if key not in rungs_cache:
            rungs_cache[key] = L._rung_edges(L.station_day_rungs(
                conn, station_id=r["station"], event_date=date.fromisoformat(r["day"]), prefer_registry=False) or [])
        shaped = rungs_cache[key]
        if shaped:
            tb = L._rung_of(shaped[0], r["truth"])
            r["off"] = {k: L._rung_of(shaped[0], v) - tb for k, v in r["arms"].items()}
        else:
            r["off"] = {k: math.floor(v + 0.5) - math.floor(r["truth"] + 0.5) for k, v in r["arms"].items()}
    with open(HERE / "walk.jsonl", "w") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("\n## 检验二：全队合并训练的条件平移，严格前推")
    report(FOCUS, [r for r in out if r["station"] == FOCUS])
    report("全队", out)
    days = sorted({r["day"] for r in out})
    if days:
        mid = days[len(days) // 2]
        report(f"全队前半（< {mid}）", [r for r in out if r["day"] < mid])
        report(f"全队后半（≥ {mid}）", [r for r in out if r["day"] >= mid])
    if out:
        last = out[-1]["beta"]
        print("\n最后一次拟合 β（每标准差，°C）：", dict(zip((*FEATURES, "截距"), last)))


def paired(rows, a, b, metric):
    by = defaultdict(list)
    for r in rows:
        by[r["day"]].append(metric(r, a) - metric(r, b))
    days = sorted(by)
    point = sum(sum(v) for v in by.values()) / len(rows)
    rng = random.Random(SEED)
    bs = []
    for _ in range(BOOT):
        pick = [by[rng.choice(days)] for _ in days]
        bs.append(sum(sum(v) for v in pick) / sum(len(v) for v in pick))
    bs.sort()
    return point, bs[int(0.025 * BOOT)], bs[int(0.975 * BOOT) - 1]


def report(title, rows):
    if not rows:
        print(f"\n### {title}: 无可评估站日")
        return
    print(f"\n### {title}: {len(rows)} 站日, {len({r['day'] for r in rows})} 天")
    for arm in ("A0", "A_med", "A_phys"):
        err = [r["truth"] - r["arms"][arm] for r in rows]
        print(f"{arm:7s} MAE {statistics.mean(abs(v) for v in err):.2f}  偏差 {statistics.mean(err):+.2f}"
              f"  命中 {sum(r['off'][arm] == 0 for r in rows) / len(rows):.1%}"
              f"  ±1 {sum(abs(r['off'][arm]) <= 1 for r in rows) / len(rows):.1%}")
    mae = lambda r, a: abs(r["truth"] - r["arms"][a])  # noqa: E731
    hit = lambda r, a: float(r["off"][a] == 0)  # noqa: E731
    for a, b in (("A_phys", "A_med"), ("A_phys", "A0")):
        p, lo, hi = paired(rows, a, b, mae)
        q, qlo, qhi = paired(rows, a, b, hit)
        print(f"{a}−{b}: MAE {p:+.3f} [{lo:+.3f}, {hi:+.3f}]  命中 {q:+.1%} [{qlo:+.1%}, {qhi:+.1%}]")


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    zones = dict(conn.execute("SELECT station_id, timezone_name FROM calibration_station_registry"))
    walk = [json.loads(line) for line in open(R73 / "walk.jsonl")]
    for r in walk:
        r["zone"] = zones[r["station"]]
    phys = {(r["station"], r["day"]): features(conn, r["station"], r["day"]) for r in walk}
    cover = sum(all(k in v for k in FEATURES) for v in phys.values())
    print(f"第 73 轮站日 {len(walk)}，四特征挂牌前齐全 {cover}\n")
    test_one(walk, phys)
    test_two(conn, walk, phys)


if __name__ == "__main__":
    main()
