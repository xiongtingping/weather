"""Do the morning signals add to the market's own 10:00 book?

For each WSSS/WMKK day with a complete book at local 10:00 (newest COMMITTED
snapshot within 120 minutes before): the market's vector q (normalised mids),
its mean in °C, the settled truth (formal lanes), and the three signals of
REPORT.md 二 (morning 8–9 temperature against the ten days before, rain
before 10:00, yesterday against the ten-day median), all known by 10:00.

  1. residual: truth − market mean, by signal;
  2. tilt arm: p ∝ q · exp(β·s·(bucket centre − market mean)), β fitted on
     earlier days only (walk-forward, first 15 days only accumulate), LogScore
     against the market's own on the same days, event-day bootstrap.

    uv run python docs/research/tropical_tmax_regimes_20261018/vs_market.py
"""

from __future__ import annotations

import json
import math
import random
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402

TZ = timezone(timedelta(hours=8))
STATIONS = ("WSSS", "WMKK")
BETAS = [x / 10 for x in range(0, 31)]
WARMUP = 15
rng = random.Random(20261018)


def centre(label: str) -> tuple[float, int, int]:
    """Representative °C and the whole-degree range a C bucket covers."""
    n = int(re.findall(r"-?\d+", label)[0])
    if "below" in label:
        return n - 0.5, -99, n
    if "higher" in label or "above" in label:
        return n + 0.5, n, 99
    return float(n), n, n


def main() -> None:
    days = {(r["station"], r["date"]): r for r in json.loads((HERE / "days.json").read_text())}
    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True)
    truth = {}
    for s, d, t in conn.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth"
        f" WHERE station_id IN ('WSSS','WMKK') AND source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED'"
        " AND local_date>='2026-07-01' ORDER BY revision_no"):
        truth[(s, d)] = round(t)
    rows = []
    for (s, d), y in sorted(truth.items(), key=lambda kv: kv[0][1]):
        sig = days.get((s, d))
        if sig is None:
            continue
        at = datetime.combine(date.fromisoformat(d), time(10), TZ).astimezone(timezone.utc)
        lo = at - timedelta(minutes=120)
        z = lambda x: x.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        snap = conn.execute(
            "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            " AND observed_at<=? AND observed_at>=? ORDER BY observed_at DESC LIMIT 1", (s, d, z(at), z(lo))).fetchone()
        if snap is None:
            continue
        ref = market_reference_v1(json.loads(snap[0]))
        if ref.get("state") != "complete":
            continue
        q = {str(k): float(v) for k, v in ref["vector"].items()}
        cen = {b: centre(b) for b in q}
        tb = [b for b in q if cen[b][1] <= y <= cen[b][2]]
        if len(tb) != 1:
            continue
        mean = sum(q[b] * cen[b][0] for b in q)
        mt = sig["morning_t_anom"]
        rows.append({
            "station": s, "date": d, "truth": y, "q": q, "cen": {b: c[0] for b, c in cen.items()}, "tb": tb[0],
            "mean": mean, "resid": y - mean,
            "s_morning": 0 if mt is None else (-1 if mt <= -1 else 1 if mt >= 1 else 0),
            "s_rain": -1 if sig["morning_rain"] else 0,
            "s_prev": 0 if sig["prev_anom"] is None else (-1 if sig["prev_anom"] <= -1 else 1 if sig["prev_anom"] >= 1 else 0),
        })
    print(f"有 10 点盘口与信号的站日 {len(rows)}（WSSS {sum(r['station'] == 'WSSS' for r in rows)}，"
          f"WMKK {sum(r['station'] == 'WMKK' for r in rows)}），{rows[0]['date']}..{rows[-1]['date']}")
    print(f"市场 10 点给真值桶的平均概率 {sum(r['q'][r['tb']] for r in rows) / len(rows):.3f}")

    print("\n## 1. 残差：真值 − 市场 10 点均值（°C），按信号分组")
    for key, word in (("s_morning", "早上 8–9 点"), ("s_rain", "10 点前下雨"), ("s_prev", "前一天偏离")):
        for v, lab in ((-1, "偏冷/下雨"), (0, "平常"), (1, "偏暖")):
            g = [r["resid"] for r in rows if r[key] == v]
            if len(g) < 3:
                continue
            m = sum(g) / len(g)
            bs = sorted(sum(rng.choice(g) for _ in g) / len(g) for _ in range(2000))
            print(f"  {word} {lab:<6} n={len(g):3d}  残差均值 {m:+.2f} [{bs[50]:+.2f}, {bs[1949]:+.2f}]")

    print("\n## 2. 倾斜臂（β 只用更早的日子拟合），LogScore 对市场")

    def tilt(r, key, beta):
        w = {b: r["q"][b] * math.exp(beta * r[key] * (r["cen"][b] - r["mean"])) for b in r["q"]}
        t = sum(w.values())
        return max(w[r["tb"]] / t, 1e-3)

    for key, word in (("s_morning", "早上 8–9 点冷暖"), ("s_rain", "10 点前下雨"), ("s_prev", "前一天偏离")):
        diffs, picks = [], []
        for i, r in enumerate(rows):
            past = rows[:i]
            if len({p["date"] for p in past}) < WARMUP:
                continue
            beta = min(BETAS, key=lambda b: sum(-math.log(tilt(p, key, b)) for p in past))
            picks.append(beta)
            diffs.append((r["date"], -math.log(tilt(r, key, beta)) + math.log(max(r["q"][r["tb"]], 1e-3))))
        by = {}
        for d, x in diffs:
            by.setdefault(d, []).append(x)
        ds = list(by)
        m = sum(x for _, x in diffs) / len(diffs)
        bs = sorted(sum(sum(by[rng.choice(ds)]) for _ in ds) / sum(len(by[rng.choice(ds)]) for _ in ds) for _ in range(2000))
        print(f"  {word}：前推 {len(diffs)} 站日，倾斜 − 市场 = {m:+.4f} nat [{bs[50]:+.4f}, {bs[1949]:+.4f}]"
              f"（负＝比市场好）；β 中位 {sorted(picks)[len(picks) // 2]:.1f}")


if __name__ == "__main__":
    main()
