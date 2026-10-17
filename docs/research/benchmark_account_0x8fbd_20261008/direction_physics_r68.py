"""Round 68 (README, protocol committed 53a2f5f3 before scoring): momentum and
physics evidence -- radiation, cloud, rain, dew point -- as direction against
the market's own ladder, in-sample (event days <= 10-08) and forward (10-09 on).

Read-only on the SSOT and the production physics cache; no network.

    python3 -I docs/research/benchmark_account_0x8fbd_20261008/direction_physics_r68.py > .../direction_physics_r68.out
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import board_readings as br  # noqa: E402
from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
SINCE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-06"
FORWARD_FROM = "2026-10-09"
BANDS = (("12–24h", 12.0, 24.0), ("24–36h", 24.0, 36.0), ("36–60h", 36.0, 60.0))
TRIES = 8
BASE_DAYS = 14
BASE_MIN = 6
CHANGE_GAP_H = 18.0
DRAWS = 2000
SEED = 20261018


def utc(text: str) -> datetime:
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def z_of(value, others):
    others = [o for o in others if o is not None]
    if value is None or len(others) < BASE_MIN:
        return None
    sd = statistics.pstdev(others)
    return None if not sd > 0 else (value - statistics.mean(others)) / sd


class Physics208:
    """Charter 208 lane: per station, every forward capture of every local date."""

    def __init__(self, conn):
        self.conn = conn
        self.cache: dict[str, dict[str, list]] = {}
        self.hourly: dict[str, dict[str, list]] = {}

    def _rows(self, station):
        if station not in self.cache:
            by_day = defaultdict(list)
            for day, at, sw, cloud, rain in self.conn.execute(
                "SELECT local_date, captured_at_utc, shortwave_radiation_sum, cloudcover_mean, precipitation_sum"
                " FROM calibration_station_forecast_physics_version WHERE station_id=?"
                " AND source_id='OPEN_METEO_MODEL_FORECAST' AND capture_mode='forward'", (station,)):
                by_day[str(day)].append((utc(at), sw, cloud, rain))
            for v in by_day.values():
                v.sort(key=lambda r: r[0])
            self.cache[station] = by_day
            hourly = defaultdict(list)
            for day, at, wet in self.conn.execute(
                "SELECT local_date, captured_at_utc, wet_hours FROM calibration_station_forecast_precip_hourly_version"
                " WHERE station_id=? AND source_id='OPEN_METEO_MODEL_FORECAST' AND capture_mode='forward'", (station,)):
                hourly[str(day)].append((utc(at), wet))
            for v in hourly.values():
                v.sort(key=lambda r: r[0])
            self.hourly[station] = hourly
        return self.cache[station], self.hourly[station]

    @staticmethod
    def _asof(rows, moment):
        last = None
        for r in rows:
            if r[0] <= moment:
                last = r
            else:
                break
        return last

    def features(self, station, day, moment):
        by_day, hourly = self._rows(station)
        d0 = date.fromisoformat(day)
        out = {}
        for name, col, sign, src in (("sw", 1, 1, by_day), ("cloud", 2, -1, by_day), ("rain", 3, -1, by_day),
                                     ("wet", 1, -1, hourly)):
            def value(d, t):
                r = self._asof(src.get(d, []), t)
                return None if r is None or r[col] is None else float(r[col])
            v = value(day, moment)
            # The same lead on the earlier days: day d read (D - d) days earlier.
            base = [value((d0 - timedelta(days=k)).isoformat(), moment - timedelta(days=k)) for k in range(1, BASE_DAYS + 1)]
            z = z_of(v, base)
            out[f"{name}_anom"] = None if z is None else sign * z
            if name != "wet":
                gap = timedelta(hours=CHANGE_GAP_H)
                old = value(day, moment - gap)
                ch = None if v is None or old is None else v - old
                prior = []
                for k in range(1, BASE_DAYS + 1):
                    a = value((d0 - timedelta(days=k)).isoformat(), moment - timedelta(days=k))
                    b = value((d0 - timedelta(days=k)).isoformat(), moment - timedelta(days=k) - gap)
                    prior.append(None if a is None or b is None else a - b)
                prior = [p for p in prior if p is not None]
                sd = statistics.pstdev(prior) if len(prior) >= BASE_MIN else 0.0
                out[f"{name}_change"] = None if ch is None or not sd > 0 else sign * ch / sd
        return out


def cache_features(doc, day, moment):
    """Dew point (six-model mean of per-model z) and ICON shortwave from the
    production physics cache, baseline strictly on earlier days."""

    out = {"dew_anom": None, "icon_sw_anom": None}
    if not doc:
        return out
    lag = ds.physics_lag(day, moment)
    if lag is None:
        return out
    daily = doc.get("daily") or {}

    def z(series):
        if not series or day not in series or series[day][lag] is None:
            return None
        prior = [series[d][lag] for d in sorted(series) if d < day][-BASE_DAYS:]
        return z_of(float(series[day][lag]), [p for p in prior if p is not None])
    dews = [z(daily.get(f"dew_point_2m|{m}")) for m in ds.PHYSICS_MODELS]
    dews = [x for x in dews if x is not None]
    out["dew_anom"] = -statistics.mean(dews) if len(dews) >= ds.DEW_POINT_MIN_MODELS else None
    sw = z(daily.get("shortwave_radiation|icon_global"))
    out["icon_sw_anom"] = sw
    return out


def build(conn):
    labels = {k: v for k, v in knowable_labels(conn).items() if str(k[1]) >= SINCE}
    phys = Physics208(conn)
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
            mo, yo = order[mode], order[settled]
            e_q = sum(v for b, v in q.items() if order[b] > mo) - sum(v for b, v in q.items() if order[b] < mo)
            moment = utc(at)
            mom = ds.momentum(conn, station_id=station, event_date=day, at_utc=at)
            rows.append({
                "station": station, "day": day, "band": band, "at": at,
                "unit": "F" if "°F" in settled else "C",
                "e": ((yo > mo) - (yo < mo)) - e_q,
                "M": mom["M"],
                **phys.features(station, day, moment),
                **cache_features(cache.get(station), day, moment),
            })
    return rows


def sgn(x):
    return 0 if x is None or x == 0 else (1 if x > 0 else -1)


FEATURES = {
    "1 M（|M|≥1）": lambda r: sgn(r["M"]) if r["M"] is not None and abs(r["M"]) >= 1 else 0,
    "2 日照距平": lambda r: sgn(r["sw_anom"]),
    "3 云量距平": lambda r: sgn(r["cloud_anom"]),
    "4 降水距平": lambda r: sgn(r["rain_anom"]),
    "5 湿时数距平": lambda r: sgn(r["wet_anom"]),
    "6 日照改动": lambda r: sgn(r["sw_change"]),
    "7 云量改动": lambda r: sgn(r["cloud_change"]),
    "8 降水改动": lambda r: sgn(r["rain_change"]),
    "9 露点距平": lambda r: sgn(r["dew_anom"]),
    "10 M 且日照同号": lambda r: (sgn(r["M"]) if r["M"] is not None and abs(r["M"]) >= 1
                                  and sgn(r["sw_anom"]) == sgn(r["M"]) else 0),
}
DESCRIPTIVE = {
    "· ICON 日照距平（缓存，未登记）": lambda r: sgn(r["icon_sw_anom"]),
    "· M 且 ICON 日照同号（21.8 登记规则的方向部分）": lambda r: (
        sgn(r["M"]) if r["M"] is not None and abs(r["M"]) >= 1 and sgn(r["icon_sw_anom"]) == sgn(r["M"]) else 0),
}


def score(rows, f):
    pts = [(r["day"], f(r) * r["e"]) for r in rows if f(r)]
    return pts


def mean_ci(pts, level):
    if not pts:
        return None, None, 0, 0
    by = defaultdict(list)
    for d, v in pts:
        by[d].append(v)
    blocks = [(sum(v), len(v)) for v in by.values()]
    m = sum(v for _d, v in pts) / len(pts)
    if len(blocks) < 3:
        return m, None, len(pts), len(blocks)
    rng = random.Random(SEED)
    draws = []
    for _ in range(DRAWS):
        s = n = 0
        for _ in blocks:
            a, b = blocks[rng.randrange(len(blocks))]
            s += a
            n += b
        draws.append(s / n)
    draws.sort()
    lo = draws[int((1 - level) / 2 * DRAWS)]
    hi = draws[int((1 + level) / 2 * DRAWS) - 1]
    return m, (lo, hi), len(pts), len(blocks)


def fmt(m, ci, n, d):
    if m is None:
        return "—"
    return f"{m:+.3f} [{ci[0]:+.3f},{ci[1]:+.3f}] n={n} d={d}" if ci else f"{m:+.3f} n={n} d={d}"


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    rows = build(conn)
    ins = [r for r in rows if r["day"] < FORWARD_FROM]
    fwd = [r for r in rows if r["day"] >= FORWARD_FROM]
    days = sorted({r["day"] for r in rows})
    print(f"rows {len(rows)} (in-sample {len(ins)}, forward {len(fwd)}); event days {days[0]}..{days[-1]} "
          f"({len(days)}); forward days {len(sorted({r['day'] for r in fwd}))}")
    for k in ("M", "sw_anom", "cloud_anom", "rain_anom", "wet_anom", "sw_change", "cloud_change", "rain_change",
              "dew_anom", "icon_sw_anom"):
        print(f"  coverage {k}: {sum(r[k] is not None for r in rows)}")
    print("\n== 主检验：方向分数 mean(s·e)；合并 99.5%（Bonferroni 10），样本内 / 前推 95%")
    verdicts = {}
    for name, f in {**FEATURES, **DESCRIPTIVE}.items():
        a = mean_ci(score(rows, f), 0.995)
        b = mean_ci(score(ins, f), 0.95)
        c = mean_ci(score(fwd, f), 0.95)
        ok = (a[1] and a[1][0] > 0 and b[0] is not None and c[0] is not None and b[0] * c[0] > 0)
        rep = c[1] is not None and c[1][0] > 0
        verdicts[name] = (ok, rep)
        print(f"{name}\n   合并 {fmt(*a)}\n   样本内 {fmt(*b)}\n   前推 {fmt(*c)}"
              f"\n   判：{'成立' if ok else '不成立'}{'；前推独立复现' if rep else ''}")
    print("\n== 分段（描述）：合并 95%")
    for name, f in FEATURES.items():
        parts = []
        for band, _lo, _hi in BANDS:
            m, ci, n, d = mean_ci(score([r for r in rows if r["band"] == band], f), 0.95)
            parts.append(f"{band} {fmt(m, ci, n, d)}")
        print(f"{name}: " + " | ".join(parts))
    print("\n== C/F（描述）：合并 95%")
    for name, f in FEATURES.items():
        parts = [f"{u} {fmt(*mean_ci(score([r for r in rows if r['unit'] == u], f), 0.95))}" for u in ("C", "F")]
        print(f"{name}: " + " | ".join(parts))
    print("\n== 对 M 的增量（描述）：M 静默时该特征的方向分数；该特征与 M 同号 / 反号时 M 的方向分数")
    fm = FEATURES["1 M（|M|≥1）"]
    for name, f in list(FEATURES.items())[1:9] + list(DESCRIPTIVE.items())[:1]:
        quiet = [r for r in rows if not fm(r)]
        agree = [r for r in rows if fm(r) and f(r) == fm(r)]
        oppose = [r for r in rows if fm(r) and f(r) and f(r) != fm(r)]
        diff = agree_minus_oppose(agree, oppose, fm)
        print(f"{name}: M 静默 {fmt(*mean_ci(score(quiet, f), 0.95))}"
              f"\n   同号时 M {fmt(*mean_ci(score(agree, fm), 0.95))}"
              f"\n   反号时 M {fmt(*mean_ci(score(oppose, fm), 0.95))}"
              f"\n   同号−反号 {diff}")


def agree_minus_oppose(agree, oppose, fm):
    """Difference of M's direction score between the two groups, event days
    resampled together (both groups drawn from the same day blocks)."""

    def by_day(rows):
        out = defaultdict(lambda: [0.0, 0])
        for r in rows:
            out[r["day"]][0] += fm(r) * r["e"]
            out[r["day"]][1] += 1
        return out
    a, b = by_day(agree), by_day(oppose)
    days = sorted(set(a) | set(b))
    if len(days) < 3:
        return "—"

    def stat(sample):
        sa = sum(a[d][0] for d in sample if d in a); na = sum(a[d][1] for d in sample if d in a)
        sb = sum(b[d][0] for d in sample if d in b); nb = sum(b[d][1] for d in sample if d in b)
        return None if not na or not nb else sa / na - sb / nb
    point = stat(days)
    rng = random.Random(SEED)
    draws = sorted(x for x in (stat([days[rng.randrange(len(days))] for _ in days]) for _ in range(DRAWS)) if x is not None)
    return f"{point:+.3f} [{draws[int(0.025 * len(draws))]:+.3f},{draws[int(0.975 * len(draws)) - 1]:+.3f}]"


if __name__ == "__main__":
    main()
