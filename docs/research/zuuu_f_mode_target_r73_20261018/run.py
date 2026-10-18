"""Round 73 (PROTOCOL.md): F's centre target, least absolute error vs bucket hit.

Read-only on the SSOT.  Every arm is rebuilt as of each event day's listing
instant (D-2 05:00Z) from the production helpers in ``listing_forecast_f``;
writes walk.jsonl (one row per evaluated station-day) and prints the scores.

    uv run python -I docs/research/zuuu_f_mode_target_r73_20261018/run.py
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import listing_forecast_f as L  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
TODAY = date(2026, 10, 18)
TRAIN_DAYS = 60
MIN_TRAIN = 10
GRID = [round(-6 + 0.05 * i, 2) for i in range(241)]
FOCUS = "ZUUU"
BOOT, SEED = 2000, 7


def box_mode(errors: list[float]) -> float:
    """The shift maximising |e - d| < 0.5 days; ties -> the tied run's midpoint nearest the median."""

    counts = [sum(abs(e - d) < 0.5 for e in errors) for d in GRID]
    best = max(counts)
    runs, start = [], None
    for i, c in enumerate(counts + [-1]):
        if c == best and start is None:
            start = i
        elif c != best and start is not None:
            runs.append((GRID[start] + GRID[i - 1]) / 2.0)
            start = None
    med = statistics.median(errors)
    return min(runs, key=lambda m: (abs(m - med), m))


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    stations = list(global_roster(conn))
    zones = {s: z for s, _a, _b, z in stations}
    since = TODAY - timedelta(days=150)
    truths = L.formal_truths(conn, since=since, until=TODAY)
    st_truths: dict[str, dict[date, tuple[float, object]]] = {s: {} for s in zones}
    for (s, d), t in truths.items():
        if s in zones:
            p = date.fromisoformat(d)
            st_truths[s][p] = (t, L._settled_at(p, zones[s]))
    history = {}
    for s, z in zones.items():
        rows = []
        for p, (t, se) in sorted(st_truths[s].items()):
            pt = L._history_point(conn, station_id=s, event_date=p, zone=z)
            if pt:
                rows.append((p, pt, t, se))
        history[s] = rows
    print("history built", sum(len(v) for v in history.values()), file=sys.stderr)

    # Production chain per station-day, as of its listing instant.
    fleet: dict[date, float] = {}
    chain = []  # (station, day, raw, base, features, truth, settled)
    for s, rows in history.items():
        for p, pt, t, se in rows:
            if len(pt) < L.MIN_SYSTEMS:
                continue
            lst = L._listing_dt(p)
            if p not in fleet:
                fleet[p] = L._fleet_shift(history, since=p - timedelta(days=2 + L.FLEET_DAYS), knowable_by=lst)[0]
            b = L._base_f(pt, rows, event_date=p, fleet_c=fleet[p], knowable_by=lst)
            obs = L._obs_before_listing(st_truths[s], p)
            chain.append((s, p, b["raw"], b["f"], L._recal_features(b, obs), t, se))
    recal: dict[date, dict | None] = {}
    out = []
    by_station = defaultdict(list)
    for row in chain:
        by_station[row[0]].append(row)
    for s, rows in by_station.items():
        for s_, p, raw, fbase, x, t, se in rows:
            lst = L._listing_dt(p)
            train = [r for r in rows if r[6] <= lst and p - timedelta(days=TRAIN_DAYS) <= r[1] < p]
            if len(train) < MIN_TRAIN:
                continue
            if p not in recal:
                recal[p] = L.fit_recalibration([
                    (r[1], r[4], r[5] - r[3]) for r in chain
                    if r[6] <= lst and p - timedelta(days=L.RECAL_TRAIN_DAYS) <= r[1] < p
                ])
            if recal[p] is None:
                continue
            e = [r[5] - r[2] for r in train]
            eb = [r[5] - r[3] for r in train]
            arms = {
                "A0": fbase + L.recal_correction(recal[p], x),
                "A_med": raw + statistics.median(e),
                "A_mode": raw + box_mode(e),
                "A_raw": raw,
                "A_Fmode": fbase + box_mode(eb),
            }
            rungs = L.station_day_rungs(conn, station_id=s, event_date=p, prefer_registry=False)
            shaped = L._rung_edges(rungs or [])
            if shaped:
                edges = shaped[0]
                tb = L._rung_of(edges, t)
                rung = {k: L._rung_of(edges, v) - tb for k, v in arms.items()}
            else:
                rung = {k: math.floor(v + 0.5) - math.floor(t + 0.5) for k, v in arms.items()}
            out.append({"station": s, "day": p.isoformat(), "truth": t, "ladder": bool(shaped), "n_train": len(train),
                        "arms": {k: round(v, 3) for k, v in arms.items()}, "rung_off": rung})
    with open(HERE / "walk.jsonl", "w") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    score(out)


def summary(rows, arm):
    n = len(rows)
    err = [r["truth"] - r["arms"][arm] for r in rows]
    return {"n": n, "hit": sum(r["rung_off"][arm] == 0 for r in rows) / n,
            "hit1": sum(abs(r["rung_off"][arm]) <= 1 for r in rows) / n,
            "bias": statistics.mean(err), "mae": statistics.mean(abs(v) for v in err)}


def paired(rows, a, b):
    days = sorted({r["day"] for r in rows})
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append((r["rung_off"][a] == 0) - (r["rung_off"][b] == 0))
    point = sum(sum(v) for v in by_day.values()) / len(rows)
    rng = random.Random(SEED)
    boots = []
    for _ in range(BOOT):
        pick = [by_day[rng.choice(days)] for _ in days]
        n = sum(len(v) for v in pick)
        boots.append(sum(sum(v) for v in pick) / n)
    boots.sort()
    wins = sum(1 for v in by_day.values() for d in v if d > 0)
    losses = sum(1 for v in by_day.values() for d in v if d < 0)
    k, m = min(wins, losses), wins + losses
    p = min(1.0, 2 * sum(math.comb(m, i) for i in range(k + 1)) / 2 ** m) if m else 1.0
    return {"diff": point, "lo": boots[int(0.025 * BOOT)], "hi": boots[int(0.975 * BOOT) - 1],
            "wins": wins, "losses": losses, "sign_p": p}


def show(title, rows):
    print(f"\n## {title}: {len(rows)} 站日, {len({r['day'] for r in rows})} 天,"
          f" 有梯子 {sum(r['ladder'] for r in rows)}")
    for arm in ("A0", "A_med", "A_mode", "A_Fmode", "A_raw"):
        s = summary(rows, arm)
        print(f"{arm:8s} 命中 {s['hit']:.1%}  ±1 {s['hit1']:.1%}  偏差 {s['bias']:+.2f}  MAE {s['mae']:.2f}")
    for a, b in (("A_mode", "A0"), ("A_mode", "A_med"), ("A_Fmode", "A0")):
        d = paired(rows, a, b)
        print(f"{a}−{b}: {d['diff']:+.1%} [{d['lo']:+.1%}, {d['hi']:+.1%}]  胜/负 {d['wins']}/{d['losses']}  符号检验 p={d['sign_p']:.3f}")


def score(out):
    z = [r for r in out if r["station"] == FOCUS]
    show(FOCUS, z)
    show("全队", out)
    days = sorted({r["day"] for r in out})
    mid = days[len(days) // 2]
    show(f"全队前半（< {mid}）", [r for r in out if r["day"] < mid])
    show(f"全队后半（≥ {mid}）", [r for r in out if r["day"] >= mid])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--score":
        score([json.loads(line) for line in open(HERE / "walk.jsonl")])
    else:
        main()
