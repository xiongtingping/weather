"""Round 43c (熊先生 10-12 「更准」): the production recalibration, replayed.

Charter 267 补记五 puts round 43b's L1 robust recalibration into production F
(``listing_forecast_f.compute``: least absolute error of truth - F_base on
the station correction, the members' skew and the change from the newest
settled observation; every training day read as of its own listing instant;
numpy IRLS instead of scipy Powell).  This replays the production code itself
at each event day's listing instant + 5 min, 09-20..10-10, strict as-of as in
round 39 (runs knowable then; truths only days at least two calendar days
before the instant), and scores F (recalibrated) against F_base on the truth:
hits on the market ladder, MAE, bias, paired, overall and in each half.
The first instant's training rows are also fitted by scipy Powell on the
same objective, to show the IRLS lands on the same coefficients.
"""
import json
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np
from scipy.optimize import minimize

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target import listing_forecast_f as F  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402

conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
ALL_TRUTHS = F.formal_truths(conn, since=date(2026, 5, 1), until=date(2026, 12, 31))
ROSTER = global_roster(conn)
real_formal_truths = F.formal_truths


def ladder_of(st, d):
    """Market ladder C edges from the registry (native-unit integers, [lower - 0.5, upper + 0.5))."""
    rows_ = conn.execute("SELECT outcome, registry_json, settlement_unit FROM basket_target_market_registry"
                         " WHERE station_id=? AND event_date=? ORDER BY refreshed_at DESC LIMIT 60",
                         (st, d.isoformat())).fetchall()
    seen = {}
    for outcome, rj, unit in rows_:
        if outcome in seen:
            continue
        try:
            r = json.loads(rj)
        except ValueError:
            continue
        lo = None if r.get("lower") is None else float(r["lower"]) - 0.5
        hi = None if r.get("upper") is None else float(r["upper"]) + 0.5
        if str(unit).upper().startswith("F"):
            lo = None if lo is None else (lo - 32.0) * 5.0 / 9.0
            hi = None if hi is None else (hi - 32.0) * 5.0 / 9.0
        seen[outcome] = (lo, hi)
    if len(seen) < 3:
        return None
    edges = sorted(seen.values(), key=lambda v: v[0] if v[0] is not None else -1e9)
    return edges


def bucket(edges, x):
    for i, (lo, hi) in enumerate(edges):
        if (lo is None or x >= lo) and (hi is None or x < hi):
            return i
    return 0 if edges[0][0] is not None and x < edges[0][0] else len(edges) - 1


captured = {}
real_fit = F.fit_recalibration


def spy_fit(rows):
    captured.setdefault("first", rows)
    return real_fit(rows)


F.fit_recalibration = spy_fit
results = []
models = []
day = date(2026, 9, 20)
while day <= date(2026, 10, 10):
    now = F._listing_dt(day) + timedelta(minutes=5)
    cut = (now.date() - timedelta(days=2)).isoformat()
    F.formal_truths = lambda c, since, until, cut=cut: {
        k: v for k, v in ALL_TRUTHS.items() if since.isoformat() <= k[1] <= min(until.isoformat(), cut)}
    payload = F.compute(conn, now=now, roster=ROSTER)
    models.append((day, payload["recalibration"]))
    for key, entry in payload["station_days"].items():
        st, ed = key.split("|")
        if ed != day.isoformat() or entry.get("state") != "READY":
            continue
        y = ALL_TRUTHS.get((st, ed))
        edges = ladder_of(st, day)
        if y is None or edges is None:
            continue
        results.append({"st": st, "d": day, "y": y, "f": entry["f_c"], "b": entry["f_base_c"],
                        "hit": bucket(edges, entry["f_c"]) == bucket(edges, y),
                        "bhit": bucket(edges, entry["f_base_c"]) == bucket(edges, y)})
    day += timedelta(days=1)
F.formal_truths = real_formal_truths

print("recalibration per instant (beta per sd: station corr, skew, obs->F change; intercept):")
for d, m in models:
    if m.get("state") == "FITTED":
        print(f"  {d}  rows {m['rows']:5d} days {m['days']:3d}  beta {m['beta_per_sd']}  a {m['intercept_c']:+.3f}"
              f"  in-sample MAE {m['in_sample_mae_before_c']:.3f} -> {m['in_sample_mae_after_c']:.3f}")
    else:
        print(f"  {d}  {m}")


def report(label, rows):
    n = len(rows)
    if not n:
        return
    w = sum(r["hit"] and not r["bhit"] for r in rows)
    l_ = sum(r["bhit"] and not r["hit"] for r in rows)
    dh, hh = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    dae = np.array([abs(r["y"] - r["f"]) - abs(r["y"] - r["b"]) for r in rows])
    hae = 1.96 * dae.std() / n ** 0.5
    print(f"{label:10s} n {n:4d}: hits {sum(r['hit'] for r in rows) / n:.1%} vs F_base {sum(r['bhit'] for r in rows) / n:.1%}"
          f" ({dh:+.3f} [{dh - hh:+.3f}, {dh + hh:+.3f}]);"
          f" MAE {np.mean([abs(r['y'] - r['f']) for r in rows]):.3f} vs {np.mean([abs(r['y'] - r['b']) for r in rows]):.3f}"
          f" ({dae.mean():+.3f} [{dae.mean() - hae:+.3f}, {dae.mean() + hae:+.3f}]);"
          f" bias {np.mean([r['y'] - r['f'] for r in rows]):+.2f} vs {np.mean([r['y'] - r['b'] for r in rows]):+.2f} C")


print()
report("all", results)
days = sorted({r["d"] for r in results})
mid = days[len(days) // 2]
report("1st half", [r for r in results if r["d"] < mid])
report("2nd half", [r for r in results if r["d"] >= mid])
report("F stations", [r for r in results if r["st"].startswith("K")])

# IRLS against scipy Powell on the same objective, the first instant's rows.
rows = captured["first"]
X = np.array([x for _d, x, _y in rows])
y = np.array([v for _d, _x, v in rows])
mu, sd = X.mean(0), X.std(0) + 1e-9
Z = (X - mu) / sd
obj = lambda b: float(np.mean(np.abs(y - (Z @ b[:-1] + b[-1])))) + F.RECAL_RIDGE * float(b[:-1] @ b[:-1])  # noqa: E731
powell = minimize(obj, np.zeros(Z.shape[1] + 1), method="Powell", options={"maxiter": 20000, "xtol": 1e-8, "ftol": 1e-12}).x
irls = real_fit(rows)
b_irls = np.r_[irls["beta_per_sd"], irls["intercept_c"]]
print(f"\nfirst instant ({len(rows)} rows): IRLS {np.round(b_irls, 4).tolist()} objective {obj(b_irls):.6f};"
      f" Powell {np.round(powell, 4).tolist()} objective {obj(powell):.6f}")
