"""Charter 266's momentum M runs ahead of the market: how far does the market
move toward M in the N hours after a run arrives, by |M|; and can that be
traded as a swing -- buy at the arrival, sell N hours later (user 2026-10-25
「把 M 按大小分档，量一下「到货后 N 小时内市场朝 M 挪了多少」。能否根据m跑在市场
前面这一特性，吃波段，即买入卖出。」).

Events: every arrival (production's own receipt, ``run_change.family_runs``)
of a run of one of M's four ECMWF components, 6-60 h before the local day
ends; M as of that instant, computed exactly as ``direction_signal.momentum``.
Market: the committed WS snapshots of the station-day (hot SSOT).

Drift: s * (E[rung](t+N) - E[rung](t)), s = sign M, E[rung] the market
reference vector's expected rung (rungs, so C and F pool), t = the first
complete snapshot within 20 min after the arrival, t+N the first complete
snapshot at or after t+N (within 30 min).

Swing: at t buy YES on the rung next to the market mode on M's side, taking
the best ask (0.02..0.97) plus the taker fee 0.05 p(1-p); at t+N sell at the
best bid minus the same fee (no bid -> unsellable, counted at 0 and reported).
Per share P&L and per U staked; also the same buy held to settlement, and
the mirror (buy the rung on the other side) as a control.  Event-day block
bootstrap 95%.  Read-only; no network.

    .venv/bin/python -I docs/research/m_swing_20261025/swing.py
"""

from __future__ import annotations

import bisect
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target import run_change  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
SINCE, UNTIL = "2026-09-20", "2026-10-24"
LEAD_H = (6.0, 60.0)
HORIZONS = (1, 3, 6, 12)
ENTRY_WINDOW = timedelta(minutes=20)
EXIT_WINDOW = timedelta(minutes=30)
FEE = 0.05
BINS = ((0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 6.0), (6.0, 99.0))
DRAWS = 2000
SEED = 20261025


def utc(text):
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def fee(p):
    return FEE * p * (1.0 - p)


class Book:
    """One station-day's committed snapshots, parsed on demand."""

    def __init__(self, conn, station, day):
        self.conn = conn
        rows = conn.execute(
            "SELECT observed_at, rowid FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
            " AND state='COMMITTED' ORDER BY observed_at", (station, day)).fetchall()
        self.at = [utc(r[0]) for r in rows]
        self.rid = [r[1] for r in rows]
        self.cache = {}

    def _parse(self, i):
        if i not in self.cache:
            raw = self.conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE rowid=?",
                                    (self.rid[i],)).fetchone()[0]
            b = json.loads(raw)
            ref = market_reference_v1(b)
            if ref.get("state") != "complete":
                self.cache[i] = None
            else:
                order = {x["bucket"]: int(x["ordinal"]) for x in b}
                vec = {str(k): float(v) for k, v in ref["vector"].items()}
                mode = max(sorted(vec, key=lambda k: order[k]), key=lambda k: vec[k])
                self.cache[i] = {
                    "at": self.at[i], "order": order, "mode": mode,
                    "e": sum(order[k] * v for k, v in vec.items()),
                    "ask": {x["bucket"]: float(x["ask_levels"][0]["price"]) for x in b
                            if x.get("ask_available") and x.get("ask_levels")},
                    "bid": {x["bucket"]: float(x["bid_price"]) for x in b
                            if x.get("bid_available") and x.get("bid_price")},
                }
        return self.cache[i]

    def first_bid_after(self, moment, rung):
        """The first complete snapshot at or after ``moment`` with a bid on ``rung``."""
        i = bisect.bisect_left(self.at, moment)
        while i < len(self.at):
            snap = self._parse(i)
            if snap is not None and snap["bid"].get(rung):
                return snap
            i += 1
        return None

    def crossed(self, start, window, rung, level):
        """A later snapshot within ``window`` whose best bid on ``rung`` fell
        below ``level`` or whose ask reached it: the level was sold through,
        so a maker bid resting there would have filled (queue left aside)."""
        i = bisect.bisect_right(self.at, start)
        while i < len(self.at) and self.at[i] <= start + window:
            snap = self._parse(i)
            if snap is not None:
                b, a = snap["bid"].get(rung), snap["ask"].get(rung)
                if (b is not None and b < level - 1e-9) or (b is None and a is not None) or (a is not None and a <= level + 1e-9):
                    return snap["at"]
            i += 1
        return None

    def first_after(self, moment, window):
        i = bisect.bisect_left(self.at, moment)
        while i < len(self.at) and self.at[i] <= moment + window:
            snap = self._parse(i)
            if snap is not None:
                return snap
            i += 1
        return None


def m_at(runs, moment):
    total, n = 0.0, 0
    for _name, system, back, spread in ds.MOMENTUM_COMPONENTS:
        held = [r for r in runs.get(system) or () if r[0] <= moment]
        if len(held) >= back + 1:
            total += (float(held[-1][2]) - float(held[-1 - back][2])) / spread
            n += 1
    return round(total, 4) if n else None


def build(conn):
    labels = knowable_labels(conn)
    zones = ds.fleet_timezones(conn)
    events = []
    for (station, day), label in sorted(labels.items()):
        station, day = str(station), str(day)
        if not SINCE <= day <= UNTIL or station not in zones:
            continue
        runs = run_change.family_runs(conn, station_id=station, event_date=day, systems=ds.MOMENTUM_SYSTEMS)
        runs = {k: [(ds._instant(r[0]), r[1], r[2]) for r in v] for k, v in runs.items()}
        arrivals = sorted({r[0] for v in runs.values() for r in v})
        end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), datetime.min.time(),
                               tzinfo=ZoneInfo(zones[station]))
        arrivals = [a for a in arrivals if LEAD_H[0] <= (end - a).total_seconds() / 3600 <= LEAD_H[1]]
        if not arrivals:
            continue
        book = Book(conn, station, day)
        if not book.at:
            continue
        settled = str(label["bucket"])
        for a in arrivals:
            M = m_at(runs, a)
            if M is None or M == 0:
                continue
            s = 1 if M > 0 else -1
            entry = book.first_after(a, ENTRY_WINDOW)
            if entry is None:
                continue
            ev = {"station": station, "day": day, "M": M, "s": s, "lead": (end - a).total_seconds() / 3600,
                   "unit": "F" if "°F" in settled else "C", "drift": {}, "trade": {}, "mirror": {}}
            inv = {v: k for k, v in entry["order"].items()}
            for name, rung in (("trade", inv.get(entry["order"][entry["mode"]] + s)),
                               ("mirror", inv.get(entry["order"][entry["mode"]] - s))):
                ask = entry["ask"].get(rung) if rung else None
                if ask is None or not 0.02 <= ask <= 0.97:
                    continue
                cost = ask + fee(ask)
                bid0 = entry["bid"].get(rung)
                filled_at = book.crossed(entry["at"], timedelta(minutes=60), rung, bid0) if (bid0 := entry["bid"].get(rung)) else None
                legs = {"cost": cost, "settle": (1.0 if settled == rung else 0.0) - cost, "mk_fill": filled_at is not None,
                        "mid0": (ask + bid0) / 2 if bid0 else None,
                        "roundtrip": (ask + fee(ask)) - (bid0 - fee(bid0)) if bid0 else None}
                for n in HORIZONS:
                    out = book.first_after(entry["at"] + timedelta(hours=n), EXIT_WINDOW)
                    if out is None:
                        continue
                    bid = out["bid"].get(rung)
                    legs[n] = (bid - fee(bid) if bid else 0.0) - cost
                    legs[f"nobid{n}"] = not bid
                    legs[f"bid{n}"] = None if not bid else bid - fee(bid) - cost
                    # Executable: the first bid at or after t+N, else held to settlement.
                    sold = book.first_bid_after(entry["at"] + timedelta(hours=n), rung)
                    legs[f"exe{n}"] = (sold["bid"][rung] - fee(sold["bid"][rung]) - cost) if sold else legs["settle"]
                    # Upper bound: entered as a maker at the arrival's bid, filled at once, no fee.
                    if bid0:
                        legs[f"mk{n}"] = legs[f"exe{n}"] + cost - bid0
                        if filled_at is not None:
                            # Filled when the level was sold through; out N hours after the fill.
                            out2 = book.first_bid_after(filled_at + timedelta(hours=n), rung)
                            legs[f"mkf{n}"] = (out2["bid"][rung] - fee(out2["bid"][rung]) - bid0) if out2 else ((1.0 if settled == rung else 0.0) - bid0)
                    a_n = out["ask"].get(rung)
                    if bid and a_n is not None and legs["mid0"] is not None:
                        legs[f"mid{n}"] = (bid + a_n) / 2 - legs["mid0"]
                ev[name] = legs
            for n in HORIZONS:
                out = book.first_after(entry["at"] + timedelta(hours=n), EXIT_WINDOW)
                if out is not None:
                    ev["drift"][n] = s * (out["e"] - entry["e"])
            events.append(ev)
    return events


def boot(rows, value):
    pts = [(r["day"], v) for r in rows if (v := value(r)) is not None]
    if not pts:
        return None, None, 0, 0
    by = defaultdict(list)
    for d, v in pts:
        by[d].append(v)
    days = sorted(by)
    mean = sum(v for _d, v in pts) / len(pts)
    if len(days) < 3:
        return mean, None, len(pts), len(days)
    rng = random.Random(SEED)
    draws = []
    for _ in range(DRAWS):
        s = n = 0
        for _ in days:
            vs = by[days[rng.randrange(len(days))]]
            s += sum(vs)
            n += len(vs)
        draws.append(s / n)
    draws.sort()
    return mean, (draws[int(0.025 * DRAWS)], draws[int(0.975 * DRAWS) - 1]), len(pts), len(days)


def fmt(res, scale=1.0, unit=""):
    m, ci, n, d = res
    if m is None:
        return "—"
    f = lambda x: f"{scale * x:+.3f}{unit}"  # noqa: E731
    return f"{f(m)} [{f(ci[0])},{f(ci[1])}] n={n} d={d}" if ci else f"{f(m)} n={n} d={d}"


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    ev = build(conn)
    days = sorted({e["day"] for e in ev})
    print(f"事件 {len(ev)}（到货时刻 × 站日），事件日 {days[0]}..{days[-1]}（{len(days)} 天）")
    print("\n== 漂移：s·(N 小时后 − 到货时) 的市场期望档位（档；正＝朝 M 挪）")
    print("|M| 档 | " + " | ".join(f"{n}h" for n in HORIZONS))
    for lo, hi in BINS:
        rs = [e for e in ev if lo <= abs(e["M"]) < hi]
        print(f"[{lo},{hi}) n={len(rs)} | " + " | ".join(fmt(boot(rs, lambda r, n=n: r["drift"].get(n))) for n in HORIZONS))
    for name, ok in (("|M|≥1", lambda e: abs(e["M"]) >= 1), ("全部", lambda e: True)):
        rs = [e for e in ev if ok(e)]
        print(f"{name} n={len(rs)} | " + " | ".join(fmt(boot(rs, lambda r, n=n: r["drift"].get(n))) for n in HORIZONS))
    for leg, title in (("trade", "波段：到货买入 M 那侧众数邻档 YES（吃单），N 小时后按买一卖出；每股盈亏（美元）"),
                       ("mirror", "对照：买反侧邻档")):
        print(f"\n== {title}")
        print("|M| 档 | " + " | ".join(f"{n}h" for n in HORIZONS) + " | 持有到结算")
        for lo, hi in BINS + ((1.0, 99.0), (0.0, 99.0)):
            rs = [e for e in ev if lo <= abs(e["M"]) < hi and e[leg]]
            cells = [fmt(boot(rs, lambda r, n=n: r[leg].get(n))) for n in HORIZONS]
            print(f"[{lo},{hi}) n={len(rs)} | " + " | ".join(cells) + f" | {fmt(boot(rs, lambda r: r[leg]['settle']))}")
        rs = [e for e in ev if e[leg]]
        cost = sum(e[leg]["cost"] for e in rs) / max(1, len(rs))
        nob = {n: sum(1 for e in rs if e[leg].get(f"nobid{n}")) for n in HORIZONS}
        print(f"平均买入成本 {cost:.3f}；卖出时没有买一（按 0 计）: " + ", ".join(f"{n}h {nob[n]}" for n in HORIZONS))
    rt = [e for e in ev if e["trade"] and e["trade"].get("roundtrip") is not None]
    rts = sorted(e["trade"]["roundtrip"] for e in rt)
    print(f"\n== 往返成本（进场时刻立刻买入再卖出：卖一+费 − (买一−费)），M 那侧邻档：中位 {rts[len(rts) // 2]:.3f}，"
          f"平均 {sum(rts) / len(rts):.3f}，四分位 {rts[len(rts) // 4]:.3f}–{rts[3 * len(rts) // 4]:.3f}（n={len(rts)}）")
    print("\n== 拆开：M 那侧邻档的中间价移动（毛，不含点差和费用；美元/股） | 只算卖出时有买一的波段盈亏")
    for lo, hi in BINS + ((1.0, 99.0),):
        rs = [e for e in ev if lo <= abs(e["M"]) < hi and e["trade"]]
        print(f"[{lo},{hi}) | 中间价 " + " · ".join(f"{n}h {fmt(boot(rs, lambda r, n=n: r['trade'].get(f'mid{n}')))}" for n in (3, 6, 12))
              + " | 有买一 6h " + fmt(boot(rs, lambda r: r["trade"].get("bid6"))))
    print("\n== 可执行退出：N 小时后第一个有买一的快照卖出，一直没有就持到结算（每股，美元）")
    halves = (("前半 <10-09", lambda e: e["day"] < "2026-10-09"), ("后半 ≥10-09", lambda e: e["day"] >= "2026-10-09"))
    for lo, hi in BINS + ((3.0, 99.0), (4.0, 99.0), (1.0, 99.0)):
        rs = [e for e in ev if lo <= abs(e["M"]) < hi and e["trade"]]
        cost = sum(e["trade"]["cost"] for e in rs) / max(1, len(rs))
        print(f"[{lo},{hi}) 成本 {cost:.3f} | " + " · ".join(f"{n}h {fmt(boot(rs, lambda r, n=n: r['trade'].get(f'exe{n}')))}" for n in (3, 6, 12)))
        if lo >= 3.0:
            for name, ok in halves:
                h = [e for e in rs if ok(e)]
                print(f"     {name}: " + " · ".join(f"{n}h {fmt(boot(h, lambda r, n=n: r['trade'].get(f'exe{n}')))}" for n in (3, 6, 12)))
    print("\n== 上限：挂单进场（到货时按买一挂、假设立刻成交、免费），吃单卖出；每股")
    for lo, hi in ((1.0, 99.0), (3.0, 99.0), (4.0, 99.0)):
        rs = [e for e in ev if lo <= abs(e["M"]) < hi and e["trade"]]
        print(f"[{lo},{hi}) | " + " · ".join(f"{n}h {fmt(boot(rs, lambda r, n=n: r['trade'].get(f'mk{n}')))}" for n in (3, 6, 12)))
        for name, ok in halves:
            h = [e for e in rs if ok(e)]
            print(f"     {name}: " + " · ".join(f"{n}h {fmt(boot(h, lambda r, n=n: r['trade'].get(f'mk{n}')))}" for n in (3, 6, 12)))
    print("\n== 挂单进场 + 成交代理（60 分钟内该价位被卖穿才算成交；成交后 N 小时吃单卖出）；每股")
    for lo, hi in ((1.0, 99.0), (3.0, 99.0), (4.0, 99.0)):
        rs = [e for e in ev if lo <= abs(e["M"]) < hi and e["trade"] and e["trade"].get("mid0") is not None]
        fr = sum(e["trade"]["mk_fill"] for e in rs) / max(1, len(rs))
        print(f"[{lo},{hi}) 成交率 {fr:.0%} | " + " · ".join(f"{n}h {fmt(boot(rs, lambda r, n=n: r['trade'].get(f'mkf{n}')))}" for n in (3, 6, 12)))
        for name, ok in halves:
            h = [e for e in rs if ok(e)]
            print(f"     {name}: " + " · ".join(f"{n}h {fmt(boot(h, lambda r, n=n: r['trade'].get(f'mkf{n}')))}" for n in (3, 6, 12)))
    print("\n== 按提前量（|M|≥1，M 那侧，6h 卖出）")
    for lo, hi in ((6, 24), (24, 36), (36, 60)):
        rs = [e for e in ev if abs(e["M"]) >= 1 and e["trade"] and lo <= e["lead"] < hi]
        print(f"{lo}–{hi}h: 漂移 6h {fmt(boot(rs, lambda r: r['drift'].get(6)))} | 波段 6h {fmt(boot(rs, lambda r: r['trade'].get(6)))}")


if __name__ == "__main__":
    main()
