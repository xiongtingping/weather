"""Route 3 of PROTOCOL.md: does the market lag a new ECMWF-family run?

    uv run python docs/research/skew_profit_routes_20261017/t3_run_lag.py
"""

from __future__ import annotations

import gzip
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target import run_change  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.station_day_board import bucket_of, ladder_axis  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
SINCE = "2026-08-20"
FEE = 0.05
OFFSETS = (0, 30, 60, 120, 360)   # minutes after the run was in hand
FIRST_WITHIN = timedelta(minutes=10)
rng = random.Random(20261017)


def _t(s) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _z(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def market(conn, station, day, at: datetime, after: bool):
    q = ("SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
         " AND state='COMMITTED' AND observed_at" + (">=? ORDER BY observed_at ASC" if after else "<=? ORDER BY observed_at DESC")
         + " LIMIT 1")
    row = conn.execute(q, (station, day, _z(at))).fetchone()
    if row is None:
        return None
    buckets = json.loads(row[1])
    ref = market_reference_v1(buckets)
    if ref.get("state") != "complete":
        return None
    return _t(row[0]), {str(k): float(v) for k, v in ref["vector"].items()}, buckets


def roi(rows):
    if not rows:
        return "0 笔"
    by = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        by[r["event_date"]][0] += r["pnl"]
        by[r["event_date"]][1] += r["cost"]
    days = list(by)
    sims = []
    for _ in range(2000):
        s = [by[days[rng.randrange(len(days))]] for _ in days]
        sims.append(sum(x[0] for x in s) / sum(x[1] for x in s))
    sims.sort()
    pnl, cost = sum(r["pnl"] for r in rows), sum(r["cost"] for r in rows)
    return (f"{len(rows)} 笔/{len(days)} 天  命中 {100 * sum(r['hit'] for r in rows) / len(rows):.1f}%"
            f"  均买价 {sum(r['ask'] for r in rows) / len(rows):.3f}  回报率 {100 * pnl / cost:+.1f}%"
            f" [{100 * sims[50]:+.1f}, {100 * sims[1949]:+.1f}]  合计 {pnl:+.2f}")


def buy_side(vec, buckets, axis_order, sign, truth_bucket):
    labels = sorted(vec, key=lambda b: axis_order[b])
    mode = max(labels, key=lambda b: (vec[b], -labels.index(b)))
    i = labels.index(mode) + sign
    if not 0 <= i < len(labels):
        return None
    b = next((x for x in buckets if str(x["bucket"]) == labels[i]), None)
    asks = [float(x["price"]) for x in (b or {}).get("ask_levels") or [] if float(x.get("shares") or 0) > 0]
    if not asks:
        return None
    a = min(asks)
    if not 0 < a < 1:
        return None
    cost = a + FEE * a * (1 - a)
    hit = int(labels[i] == truth_bucket)
    return {"ask": a, "cost": cost, "hit": hit, "pnl": hit - cost}


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=1")
    truth = {}
    for s, d, v in conn.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth"
        f" WHERE source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED' AND local_date>=? ORDER BY revision_no",
        (SINCE,),
    ):
        truth[(str(s), str(d))] = float(v)
    scopes = conn.execute("SELECT DISTINCT station_id, event_date, settlement_timezone FROM basket_official_ws_snapshot"
                          " WHERE state='COMMITTED' AND event_date>=?", (SINCE,)).fetchall()
    spread = {system: sd for _n, system, _b, sd in ds.MOMENTUM_COMPONENTS}
    back = {system: b for _n, system, b, _sd in ds.MOMENTUM_COMPONENTS}
    events = []
    for station, day, tz in sorted(scopes):
        if (station, day) not in truth:
            continue
        env = conn.execute(
            "SELECT bucket_ordinals_json, json_extract(lineage_json,'$.bucket_intervals_c_json')"
            " FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            " AND json_extract(lineage_json,'$.bucket_intervals_c_json') IS NOT NULL LIMIT 1", (station, day)).fetchone()
        if env is None:
            continue
        axis = ladder_axis(json.loads(env[0]), json.loads(env[1]))
        centre = {a["bucket"]: a["centre_c"] for a in axis}
        order = {a["bucket"]: a["ordinal"] for a in axis}
        if any(v is None for v in centre.values()):
            continue
        truth_bucket = bucket_of(axis, truth[(station, day)])
        end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), time(0), ZoneInfo(tz)).astimezone(timezone.utc)
        runs = run_change.family_runs(conn, station_id=station, event_date=day, systems=ds.MOMENTUM_SYSTEMS)
        for system, rs in runs.items():
            k = back[system]
            for i in range(k, len(rs)):
                had, _init, value = rs[i]
                delta = float(value) - float(rs[i - k][2])
                if abs(delta) < spread[system] or not 0 <= (end - had).total_seconds() / 3600 <= 48:
                    continue
                base = market(conn, station, day, had, after=False)
                if base is None or set(base[1]) != set(centre):
                    continue
                mean0 = sum(base[1][b] * centre[b] for b in centre)
                sign = 1 if delta > 0 else -1
                moves = {}
                first = None
                for off in OFFSETS:
                    m = market(conn, station, day, had + timedelta(minutes=off), after=True)
                    if m is None or set(m[1]) != set(centre) or m[0] > end:
                        continue
                    if off == 0:
                        if m[0] - had > FIRST_WITHIN:
                            continue
                        first = m
                    moves[off] = sign * (sum(m[1][b] * centre[b] for b in centre) - mean0)
                ev = {"station_id": station, "event_date": day, "system": system, "had": _z(had), "delta": delta,
                      "lead_h": round((end - had).total_seconds() / 3600, 1), "moves": moves}
                if first is not None:
                    ev["buy_now"] = buy_side(first[1], first[2], order, sign, truth_bucket)
                later = market(conn, station, day, had + timedelta(hours=2), after=True)
                if later is not None and set(later[1]) == set(centre) and later[0] <= end:
                    ev["buy_2h"] = buy_side(later[1], later[2], order, sign, truth_bucket)
                events.append(ev)
    with gzip.open(HERE / "t3_events.jsonl.gz", "wt", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    print(f"事件 {len(events)}（|Δ| ≥ 该分量 spread、距结算 0–48 小时）")
    print("\n## 市场均值沿 Δ 方向的移动（°C；Δ 本身的均值绝对值 "
          f"{sum(abs(e['delta']) for e in events) / max(1, len(events)):.2f}°C）")
    for off in OFFSETS:
        v = [e["moves"][off] for e in events if off in e["moves"]]
        if v:
            print(f"  到手后 {off:>3} 分钟（0＝10 分钟内第一份）  n={len(v)}  均 {sum(v) / len(v):+.3f}°C"
                  f"  同向比例 {100 * sum(x > 0 for x in v) / len(v):.0f}%")
    for system in ds.MOMENTUM_SYSTEMS:
        sub = [e for e in events if e["system"] == system]
        print(f"  {system}: n={len(sub)}  " + "  ".join(
            f"+{off}m {sum(e['moves'][off] for e in sub if off in e['moves']) / max(1, sum(off in e['moves'] for e in sub)):+.3f}"
            for off in OFFSETS))
    print("\n## 交易：按 Δ 方向买 众数±1（吃单）")
    print(f"  到手后 10 分钟内第一份盘口  {roi([dict(e['buy_now'], event_date=e['event_date']) for e in events if e.get('buy_now')])}")
    print(f"  到手后 2 小时              {roi([dict(e['buy_2h'], event_date=e['event_date']) for e in events if e.get('buy_2h')])}")


if __name__ == "__main__":
    main()
