"""Readings 1-5 of PROTOCOL.md over rows.jsonl.gz.

    uv run python docs/research/market_upper_overpricing_20261017/judge.py
"""

from __future__ import annotations

import gzip
import json
import math
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
FEE = 0.05
BOOT = 2000
WARMUP_DAYS = 14
MIN_CELL_N = 100
UPPER = ("+1", "+2", "≥+3")
BANDS = (0, 12, 24, 36, 48)
BAND_WORD = {0: "0–12h", 12: "12–24h", 24: "24–36h", 36: "36–48h", 48: "≥48h"}
rng = random.Random(20261017)


def kk(k: int) -> str:
    return "≤−2" if k <= -2 else "−1" if k == -1 else "0" if k == 0 else "+1" if k == 1 else "+2" if k == 2 else "≥+3"


def load() -> list[dict]:
    rows = []
    with gzip.open(HERE / "rows.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            r["kk"] = kk(r["k"])
            if r["bid"] is not None and 0.0 < r["bid"] < 1.0:
                q = 1.0 - r["bid"]
                r["edge"] = r["bid"] - r["hit"] - FEE * q * (1.0 - q)
            else:
                r["edge"] = None
            rows.append(r)
    return rows


def boot_mean(rows: list[dict], field: str) -> tuple[float | None, float | None, float | None, int]:
    """Mean of ``field`` with an event-day block bootstrap interval."""

    vals = [r for r in rows if r[field] is not None]
    if not vals:
        return None, None, None, 0
    by = defaultdict(list)
    for r in vals:
        by[r["event_date"]].append(r[field])
    days = list(by)
    sums = {d: (sum(v), len(v)) for d, v in by.items()}
    est = sum(s for s, _ in sums.values()) / sum(n for _, n in sums.values())
    stats = []
    for _ in range(BOOT):
        s = n = 0
        for _ in days:
            a, b = sums[days[rng.randrange(len(days))]]
            s += a
            n += b
        stats.append(s / n)
    stats.sort()
    return est, stats[int(0.025 * BOOT)], stats[int(0.975 * BOOT) - 1], len(vals)


def fmt(m, lo, hi, n, scale=100.0, unit="点") -> str:
    if m is None:
        return "—"
    return f"{scale * m:+.2f}{unit} [{scale * lo:+.2f}, {scale * hi:+.2f}] n={n}"


def main() -> None:
    rows = load()
    for r in rows:
        r["bias"] = r["p"] - r["hit"]
    days = sorted({r["event_date"] for r in rows})
    mid = days[len(days) // 2]
    print(f"行 {len(rows)}，站日 {len({(r['station_id'], r['event_date']) for r in rows})}，"
          f"事件日 {len(days)}（{days[0]}..{days[-1]}），拆半于 {mid}")

    print("\n## 1. 偏差 p − hit（正＝市场高估），按相对市场众数的位置")
    for k in ("≤−2", "−1", "0", "+1", "+2", "≥+3"):
        a = [r for r in rows if r["kk"] == k]
        h1 = [r for r in a if r["event_date"] < mid]
        h2 = [r for r in a if r["event_date"] >= mid]
        mp = sum(r["p"] for r in a) / len(a)
        print(f"{k:>4}  均价 {100 * mp:5.1f}%  全期 {fmt(*boot_mean(a, 'bias'))}  前半 {fmt(*boot_mean(h1, 'bias'))}"
              f"  后半 {fmt(*boot_mean(h2, 'bias'))}")
    print("\n逐周（k=+1 / +2 / ≥+3 合并，周一起）")
    week = lambda d: (date.fromisoformat(d) - timedelta(days=date.fromisoformat(d).weekday())).isoformat()
    up = [r for r in rows if r["kk"] in UPPER]
    for w in sorted({week(r["event_date"]) for r in up}):
        a = [r for r in up if week(r["event_date"]) == w]
        m = sum(r["bias"] for r in a) / len(a)
        print(f"  {w}  {100 * m:+.2f} 点  n={len(a)}  事件日 {len({r['event_date'] for r in a})}")

    print("\n## 2. 一致性")
    print("按提前量档（偏差，点）")
    for k in UPPER:
        cells = []
        for b in BANDS:
            a = [r for r in rows if r["kk"] == k and r["band"] == b]
            m, lo, hi, n = boot_mean(a, "bias")
            cells.append(f"{BAND_WORD[b]} {100 * m:+.2f}[{100 * lo:+.1f},{100 * hi:+.1f}]" if m is not None else f"{BAND_WORD[b]} —")
        print(f"  {k:>4}  " + "  ".join(cells))
    print("按单位")
    for k in UPPER:
        print(f"  {k:>4}  " + "  ".join(
            f"{u} {fmt(*boot_mean([r for r in rows if r['kk'] == k and r['unit'] == u], 'bias'))}" for u in ("C", "F")))
    print("按站点（k=+1、+2、≥+3 合并；n≥30 的站）")
    by = defaultdict(list)
    for r in up:
        by[r["station_id"]].append(r["bias"])
    st = sorted(((s, len(v), sum(v) / len(v)) for s, v in by.items() if len(v) >= 30), key=lambda x: -x[2])
    pos = sum(1 for _s, _n, m in st if m > 0)
    n = len(st)
    # two-sided exact binomial test against 1/2
    tail = sum(math.comb(n, i) for i in range(n + 1) if math.comb(n, i) <= math.comb(n, pos)) / 2 ** n
    print(f"  {n} 站，高估（偏差>0）{pos} 站，{100 * pos / n:.0f}%，二项检验 p={tail:.4f}")
    print("  " + "  ".join(f"{s}:{100 * m:+.1f}({c})" for s, c, m in st))

    print("\n## 3. 交易口径：卖出该桶（吃单买 NO）每股利润＝买一 − hit − 手续费（美分）")
    for k in ("−1", "0") + UPPER:
        cells = []
        for b in BANDS:
            a = [r for r in rows if r["kk"] == k and r["band"] == b]
            m, lo, hi, n = boot_mean(a, "edge")
            cells.append(f"{BAND_WORD[b]} {100 * m:+.2f}[{100 * lo:+.1f},{100 * hi:+.1f}]n{n}" if m is not None else f"{BAND_WORD[b]} —")
        allm = boot_mean([r for r in rows if r["kk"] == k], "edge")
        print(f"  {k:>4}  全部 {fmt(*allm, unit='¢')}\n        " + "  ".join(cells))
    print("  中价口径（挂单上界，不付价差）＝第 1 节偏差")

    print("\n## 4. 严格 OOS：逐日前推，只用 event_date ≤ d−2 的已结算行")
    cells = [(k, b) for k in UPPER for b in BANDS]
    tradable = [r for r in rows if r["edge"] is not None]
    by_day = defaultdict(list)
    for r in tradable:
        by_day[r["event_date"]].append(r)
    start = days[WARMUP_DAYS] if len(days) > WARMUP_DAYS else None
    picked, fixed, chosen_log = [], [], []
    for d in days:
        if start is None or d < start:
            continue
        cutoff = (date.fromisoformat(d) - timedelta(days=2)).isoformat()
        prefix = defaultdict(list)
        for dd in days:
            if dd > cutoff:
                break
            for r in by_day.get(dd, ()):
                if r["kk"] in UPPER:
                    prefix[(r["kk"], r["band"])].append(r["edge"])
        live = {c for c in cells if len(prefix[c]) >= MIN_CELL_N and sum(prefix[c]) / len(prefix[c]) > 0}
        chosen_log.append((d, sorted(live)))
        for r in by_day.get(d, ()):
            if (r["kk"], r["band"]) in live:
                picked.append(r)
            if r["kk"] in ("+1", "+2"):
                fixed.append(r)
    for name, a in (("前推选格", picked), ("固定臂 k=+1、+2 全部提前量", fixed)):
        m, lo, hi, n = boot_mean(a, "edge")
        tot = sum(r["edge"] for r in a)
        hd = sorted({r["event_date"] for r in a})
        half = hd[len(hd) // 2] if hd else None
        h1 = boot_mean([r for r in a if r["event_date"] < half], "edge") if hd else (None,) * 4
        h2 = boot_mean([r for r in a if r["event_date"] >= half], "edge") if hd else (None,) * 4
        print(f"  {name}：{fmt(m, lo, hi, n, unit='¢/股')}，合计 {tot:+.2f} 美元（每笔 1 股），"
              f"事件日 {len(hd)}；前半 {fmt(*h1, unit='¢')}，后半 {fmt(*h2, unit='¢')}")
        if a:
            depth = sorted(r["bid_shares"] for r in a if r["bid_shares"] is not None)
            print(f"    买一挂量 中位 {depth[len(depth) // 2]:.0f} 股，10% 分位 {depth[len(depth) // 10]:.0f} 股；"
                  f"成交价（NO）中位 {sorted(1 - r['bid'] for r in a)[len(a) // 2]:.3f}")
    freq = defaultdict(int)
    for _d, live in chosen_log:
        for c in live:
            freq[c] += 1
    print(f"  前推期 {len(chosen_log)} 个事件日，各格被选天数：" +
          "  ".join(f"{k}@{BAND_WORD[b]}:{freq[(k, b)]}" for k, b in cells))


if __name__ == "__main__":
    main()
