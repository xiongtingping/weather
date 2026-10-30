"""Per-station F (user 2026-10-31 「所有模型，按照单站点重新优化，重新拟合参数，也就是每个站点一套自己的！先从确定性预报开始」).

Read-only on the SSOT.  ``build`` reads every settled station-day's F members as
of its own listing instant (station values, as production F reads them), the
truth and the market ladder into ``dataset.json``; ``score`` walks every event
day forward with only what had settled by that day's listing instant and
compares the production chain (round 75: fleet-shrunk station bias, shrunk
inverse-MAE weights, one set of constants for the fleet) against per-station
variants whose parameters are each station's own.

    .venv/bin/python -I docs/research/station_own_f_20261031/run.py build
    .venv/bin/python -I docs/research/station_own_f_20261031/run.py score
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

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import listing_forecast_f as L  # noqa: E402
from bayes_basket_target import listing_forecast_f_mos as M  # noqa: E402
from bayes_basket_target import listing_forecast_f_terrain as terrain  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import global_roster  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
HISTORY_FROM = date(2026, 6, 1)
EVAL_FROM = date(2026, 9, 1)
BOOT, SEED = 2000, 7


def build() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    roster = global_roster(conn)
    table = terrain.read()
    until = datetime.now(timezone.utc).date()
    truths = L.formal_truths(conn, since=HISTORY_FROM, until=until)
    rows = []
    for station, _lat, _lon, zone in roster:
        day = HISTORY_FROM
        while day <= until:
            truth = truths.get((station, day.isoformat()))
            if truth is not None:
                maxima = L.system_maxima(conn, station_id=station, event_date=day, as_of_utc=L.listing_instant(day),
                                         settlement_timezone=zone, systems=M.SYSTEMS)
                xst = M.station_values(maxima, station=station, table=table)
                if xst:
                    rungs = L.station_day_rungs(conn, station_id=station, event_date=day, prefer_registry=False)
                    rows.append({"station": station, "day": day.isoformat(), "truth": truth,
                                 "settled": L._settled_at(day, zone).isoformat(), "xst": xst,
                                 "rungs": rungs})
            day += timedelta(days=1)
        print(station, sum(1 for r in rows if r["station"] == station), flush=True)
    (HERE / "dataset.json").write_text(json.dumps(rows))
    print("rows", len(rows))


# ---------------------------------------------------------------- score

def load():
    rows = json.loads((HERE / "dataset.json").read_text())
    for r in rows:
        r["day"] = date.fromisoformat(r["day"])
        r["settled"] = datetime.fromisoformat(r["settled"])
    return rows


def _wmedian(v, w):
    return M._wmedian(list(v), list(w))


def own_bias(errs, ref, half_life, stat):
    """A station's own bias for one system from its settled (day, error) pairs."""

    w = np.array([0.5 ** ((ref - d).days / half_life) for d, _e in errs])
    e = np.array([x for _d, x in errs])
    return float(_wmedian(e, w)) if stat == "median" else float((w * e).sum() / w.sum())


def per_station_centre(xst, past, ref, *, half_life, stat, weights, min_n=3):
    """Each system corrected by the station's own bias; members combined by the
    station's own inverse-MAE weights (``weights='inv_mae'``), its own least-squares
    weights (``'ls'``) or equally (``'equal'``).  ``past``: the station's settled rows."""

    members, resid = {}, {}
    for s, x in xst.items():
        errs = [(r["day"], r["truth"] - r["xst"][s]) for r in past if s in r["xst"]]
        if len(errs) < min_n:
            continue
        b = own_bias(errs, ref, half_life, stat)
        members[s] = x + b
        resid[s] = [(r["day"], r["truth"] - (r["xst"][s] + b)) for r in past if s in r["xst"]]
    if len(members) < M.MIN_MEMBERS:
        return None
    if weights == "equal":
        return statistics.median(members.values()) if stat == "median" else statistics.mean(members.values())
    if weights == "inv_mae":
        wts = {}
        for s, rs in resid.items():
            w = np.array([0.5 ** ((ref - d).days / half_life) for d, _e in rs])
            mae = float((w * np.abs([e for _d, e in rs])).sum() / w.sum())
            wts[s] = 1.0 / max(mae, 0.1)
        tot = sum(wts.values())
        return sum(members[s] * wts[s] / tot for s in members)
    raise ValueError(weights)


def ls_centre(xst, past, ref, *, half_life, ridge):
    """The station's own decay-weighted ridge regression of truth on its members
    (intercept + one weight per member, shrunk toward equal weights)."""

    systems = sorted(xst)
    use = [r for r in past if all(s in r["xst"] for s in systems)]
    if len(use) < max(10, len(systems) + 4) or len(systems) < M.MIN_MEMBERS:
        return None
    X = np.array([[r["xst"][s] for s in systems] for r in use])
    y = np.array([r["truth"] for r in use])
    w = np.array([0.5 ** ((ref - r["day"]).days / half_life) for r in use])
    k = len(systems)
    prior = np.full(k, 1.0 / k)
    xm = (w[:, None] * X).sum(0) / w.sum()
    ym = (w * y).sum() / w.sum()
    Xc, yc = X - xm, y - ym
    A = (Xc * w[:, None]).T @ Xc + ridge * w.sum() * np.eye(k)
    beta = np.linalg.solve(A, (Xc * w[:, None]).T @ yc + ridge * w.sum() * prior)
    x = np.array([xst[s] for s in systems])
    return float(ym + (x - xm) @ beta)


VARIANTS = {
    # name: fn(xst, past_station, ref) -> centre | None
    "own_median_hl25_equal": lambda x, p, d: per_station_centre(x, p, d, half_life=25, stat="median", weights="equal"),
    "own_median_hl25_invmae": lambda x, p, d: per_station_centre(x, p, d, half_life=25, stat="median", weights="inv_mae"),
    "own_median_hl15_invmae": lambda x, p, d: per_station_centre(x, p, d, half_life=15, stat="median", weights="inv_mae"),
    "own_median_hl45_invmae": lambda x, p, d: per_station_centre(x, p, d, half_life=45, stat="median", weights="inv_mae"),
    "own_mean_hl25_invmae": lambda x, p, d: per_station_centre(x, p, d, half_life=25, stat="mean", weights="inv_mae"),
    "own_ls_hl25_r1": lambda x, p, d: ls_centre(x, p, d, half_life=25, ridge=1.0),
    "own_ls_hl45_r1": lambda x, p, d: ls_centre(x, p, d, half_life=45, ridge=1.0),
    "own_ls_hl45_r5": lambda x, p, d: ls_centre(x, p, d, half_life=45, ridge=5.0),
}
# The per-station tuned variant picks, for each station and each event day, the
# candidate with the lowest MAE over that station's own previous walk-forward
# days (strictly earlier listings), at least ``TUNE_MIN`` of them; before that,
# production's chain.
TUNE_POOL = ("chain", *VARIANTS)
TUNE_MIN, TUNE_DAYS = 10, 45


def rung_hit(rungs, value, truth):
    if not rungs:
        return None
    shaped = L._rung_edges([tuple(r) for r in rungs])
    if shaped is None:
        return None
    edges, _w = shaped
    return int(L._rung_of(edges, value) == L._rung_of(edges, truth))


def score() -> None:
    rows = load()
    chain = M.Chain([{**r, "listing": L._listing_dt(r["day"])} for r in rows])
    by_station = defaultdict(list)
    for r in rows:
        by_station[r["station"]].append(r)
    evals = sorted((r for r in rows if r["day"] >= EVAL_FROM), key=lambda r: (r["day"], r["station"]))
    walk = []
    history_err = defaultdict(lambda: defaultdict(list))  # station -> variant -> [(day, |err|, listing)]
    for r in evals:
        cutoff = L._listing_dt(r["day"])
        past = [q for q in by_station[r["station"]] if q["settled"] <= cutoff and q["day"] < r["day"]]
        c = chain.centre(station=r["station"], day=r["day"], xst=r["xst"])
        if c is None:
            continue
        out = {"chain": c["mos_c"]}
        for name, fn in VARIANTS.items():
            v = fn(r["xst"], past, r["day"])
            out[name] = c["mos_c"] if v is None else v
        # Per-station tuned: choose on the station's own earlier, already-settled walk days.
        hist = history_err[r["station"]]
        known = {k: [(d, e) for d, e, settled in v if settled <= cutoff and d >= r["day"] - timedelta(days=TUNE_DAYS)]
                 for k, v in hist.items()}
        n_known = len(known.get("chain", []))
        if n_known >= TUNE_MIN:
            best = min(TUNE_POOL, key=lambda k: statistics.mean(e for _d, e in known[k]))
        else:
            best = "chain"
        out["tuned"] = out[best]
        for k in TUNE_POOL:
            hist[k].append((r["day"], abs(r["truth"] - out[k]), r["settled"]))
        walk.append({"station": r["station"], "day": r["day"].isoformat(), "truth": r["truth"], "pick": best,
                     "centres": out, "hits": {k: rung_hit(r["rungs"], v, r["truth"]) for k, v in out.items()}})
    (HERE / "walk.jsonl").write_text("\n".join(json.dumps(w) for w in walk))
    report(walk)


def boot_diff(walk, a, b):
    days = sorted({w["day"] for w in walk})
    per = defaultdict(list)
    for w in walk:
        per[w["day"]].append(abs(w["truth"] - w["centres"][a]) - abs(w["truth"] - w["centres"][b]))
    rng = random.Random(SEED)
    stats = []
    for _ in range(BOOT):
        pick = [rng.choice(days) for _ in days]
        vals = [x for d in pick for x in per[d]]
        stats.append(statistics.mean(vals))
    stats.sort()
    mid = sorted(days)[len(days) // 2]
    halves = [statistics.mean(x for d in days if (d < mid) == h for x in per[d]) for h in (True, False)]
    return statistics.mean(x for v in per.values() for x in v), stats[int(0.025 * BOOT)], stats[int(0.975 * BOOT)], halves


def report(walk):
    names = list(walk[0]["centres"])
    days = sorted({w["day"] for w in walk})
    print(f"walk {len(walk)} station-days, {len(days)} days {days[0]}..{days[-1]}")
    print(f"{'variant':28s} {'MAE':>6s} {'bias':>7s} {'hit':>6s}  ΔMAE vs chain [95%] halves")
    for k in names:
        err = [w["truth"] - w["centres"][k] for w in walk]
        hits = [w["hits"][k] for w in walk if w["hits"][k] is not None]
        line = f"{k:28s} {statistics.mean(map(abs, err)):6.3f} {statistics.mean(err):+7.3f} {100 * statistics.mean(hits):5.1f}%"
        if k != "chain":
            d, lo, hi, hv = boot_diff(walk, k, "chain")
            line += f"  {d:+.3f} [{lo:+.3f}, {hi:+.3f}] {hv[0]:+.3f}/{hv[1]:+.3f}"
        print(line)
    picks = defaultdict(int)
    for w in walk:
        picks[w["pick"]] += 1
    print("tuned picks:", dict(sorted(picks.items(), key=lambda x: -x[1])))
    print("\nper station MAE (chain → tuned, best fixed variant):")
    st = defaultdict(list)
    for w in walk:
        st[w["station"]].append(w)
    better = 0
    for s, ws in sorted(st.items()):
        m = {k: statistics.mean(abs(w["truth"] - w["centres"][k]) for w in ws) for k in names}
        better += m["tuned"] < m["chain"]
        bestk = min(VARIANTS, key=m.get)
        print(f"  {s} n={len(ws):3d} chain {m['chain']:.3f} tuned {m['tuned']:.3f}  best {bestk} {m[bestk]:.3f}")
    print(f"tuned better than chain at {better}/{len(st)} stations")


if __name__ == "__main__":
    {"build": build, "score": score}[sys.argv[1]]()
