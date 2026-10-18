"""Round 75 (PROTOCOL.md): four-layer MOS for the deterministic centre, walked forward.

Read-only on the SSOT.  ``build`` reads every station-day's six members as of
its listing instant (the F read), their station values, their own run's
physics and the truth into ``dataset.json``; ``score`` walks every event day
with only what had settled by that day's listing instant and writes
``walk.jsonl`` and the verdicts.

    uv run python -I docs/research/deterministic_centre_mos_r75_20261018/run.py build
    uv run python -I docs/research/deterministic_centre_mos_r75_20261018/run.py score
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import deterministic_point_physics as PHYS  # noqa: E402
from bayes_basket_target import listing_forecast_f as L  # noqa: E402
from bayes_basket_target import listing_forecast_f_terrain as terrain  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS, global_roster  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
R73 = ROOT / "docs" / "research" / "zuuu_f_mode_target_r73_20261018" / "walk.jsonl"
HISTORY_FROM, EVAL_FROM, EVAL_TO = date(2026, 7, 25), date(2026, 9, 1), date(2026, 10, 17)
SYSTEMS = sorted(F_SYSTEM_IDS)
HALF_LIFE = 25.0
CLIM_DAYS = 30
MIN_MEMBERS = 3
MIN_NEFF = 3.0
L3_MIN_ROWS, L3_MIN_DAYS, RIDGE = 200, 10, 1e-3
N0 = 20.0
BOOT, SEED = 2000, 7
FEATURES = ("sw", "tcc", "dep", "lpr", "anom")
# Off-protocol exploration (user 10-18 「先跑一下zuuu站点，用现有的云、雨、辐射证据（Open-Meteo）」):
# the station-day's Open-Meteo forecast (aggregator; one value for every member,
# captured by the listing instant) instead of each system's own native physics.
FEATURE_SOURCE = "native"
OM_FEATURES = ("sw", "tcc", "lpr", "anom")


# ---------------------------------------------------------------- build

def build() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    roster = global_roster(conn)
    table = terrain.read()
    truths = L.formal_truths(conn, since=HISTORY_FROM, until=EVAL_TO)
    rows = []
    for station, _lat, _lon, zone in roster:
        day = HISTORY_FROM
        while day <= EVAL_TO:
            truth = truths.get((station, day.isoformat()))
            if truth is not None:
                listing = L.listing_instant(day)
                maxima = L.system_maxima(conn, station_id=station, event_date=day, as_of_utc=listing,
                                         settlement_timezone=zone, systems=SYSTEMS)
                phys = PHYS.read_daily(conn, station_id=station, event_date=day.isoformat(), as_of_utc=listing)
                members = {}
                for system, entry in maxima.items():
                    offset = terrain.offset_c(table, system_id=system, station_id=station)
                    p = phys.get(system)
                    same_run = p is not None and p.get("run_init_utc") == entry["run_init_utc"]
                    members[system] = {
                        "x": entry["tmax_c"], "off": offset, "run": entry["run_init_utc"],
                        "phys": {k: p.get(k) for k in ("sw_mj", "tcc_day_mean", "td_day_mean", "pr_mm")}
                        if same_run else None,
                    }
                om = conn.execute(
                    "SELECT shortwave_radiation_sum, cloudcover_mean, precipitation_sum"
                    " FROM calibration_station_forecast_physics_version"
                    " WHERE station_id=? AND local_date=? AND source_id='OPEN_METEO_MODEL_FORECAST'"
                    " AND capture_mode='forward' AND captured_at_utc<=? ORDER BY captured_at_utc DESC LIMIT 1",
                    (station, day.isoformat(), listing),
                ).fetchone()
                rungs = None
                if day >= EVAL_FROM:
                    shaped = L._rung_edges(L.station_day_rungs(
                        conn, station_id=station, event_date=day, prefer_registry=False) or [])
                    rungs = shaped[0] if shaped else None
                rows.append({
                    "station": station, "day": day.isoformat(), "truth": truth, "listing": listing,
                    "settled": L._settled_at(day, zone).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "members": members, "edges": rungs,
                    "om": None if om is None else {"sw_mj": om[0], "tcc_day_mean": om[1], "pr_mm": om[2]},
                })
            day += timedelta(days=1)
        print(station, len(rows), file=sys.stderr)
    json.dump(rows, open(HERE / "dataset.json", "w"))
    print(f"dataset {len(rows)} station-days")


# ---------------------------------------------------------------- helpers

def wmedian(values, weights):
    pairs = sorted(zip(values, weights))
    total = sum(weights)
    run = 0.0
    for value, weight in pairs:
        run += weight
        if run >= total / 2.0:
            return value
    return pairs[-1][0]


def decay(day: str, ref: date) -> float:
    return 0.5 ** ((ref - date.fromisoformat(day)).days / HALF_LIFE)


def lae(X: np.ndarray, y: np.ndarray):
    mu, sd = X.mean(0), X.std(0)
    sd = np.where(sd > 1e-9, sd, 1.0)
    A = np.c_[(X - mu) / sd, np.ones(len(X))]
    n, k = A.shape
    pen = np.diag([2.0 * RIDGE] * (k - 1) + [0.0])
    beta = np.linalg.lstsq(A, y, rcond=None)[0]
    for _ in range(200):
        w = 1.0 / np.maximum(np.abs(y - A @ beta), 1e-4)
        step = np.linalg.solve(A.T @ (A * w[:, None]) / n + pen, (A * w[:, None]).T @ y / n)
        done = np.max(np.abs(step - beta)) < 1e-9
        beta = step
        if done:
            break
    return mu, sd, beta


def features(member, xst, clim, system, day_ordinal, om=None):
    if FEATURE_SOURCE == "openmeteo":
        if om is None or clim is None or any(om.get(k) is None for k in ("sw_mj", "tcc_day_mean", "pr_mm")):
            return None
        return [om["sw_mj"], om["tcc_day_mean"], math.log1p(max(om["pr_mm"], 0.0)), xst - clim]
    p = member.get("phys")
    if p is None or clim is None:
        return None
    td = p.get("td_day_mean")
    out = {
        "sw": p.get("sw_mj"), "tcc": p.get("tcc_day_mean"),
        "dep": None if td is None else xst - td,
        "lpr": None if p.get("pr_mm") is None else math.log1p(max(p["pr_mm"], 0.0)),
        "anom": xst - clim,
    }
    names = ("dep", "lpr", "anom") if system == "NOAA_AIGFS_025_NATIVE_POINT" else FEATURES
    vec = [out[n] for n in names]
    return None if any(v is None for v in vec) else vec


# ---------------------------------------------------------------- the walk

class Walk:
    def __init__(self, rows):
        self.rows = rows
        for r in rows:
            r["xst"] = {s: m["x"] + m["off"] for s, m in r["members"].items() if m["off"] is not None}
        self.days = sorted({date.fromisoformat(r["day"]) for r in rows})
        self.by_day = defaultdict(list)
        self.by_station = defaultdict(list)
        self.rows_by_key = {}
        for r in rows:
            self.by_day[r["day"]].append(r)
            self.by_station[r["station"]].append(r)
            self.rows_by_key[(r["station"], r["day"])] = r
        self.l2 = {}   # (station, day) -> {system: bhat}
        self.clim = {}
        self.l3 = {}   # (station, day) -> {system: corrected}
        self.l3s = {}

    def train(self, listing: str):
        return [r for r in self.rows if r["settled"] <= listing]

    def bias(self, train, ref: date):
        """Per system: (family bias, {station: (b_s, n_eff)}, k per station)."""

        out = {}
        for system in SYSTEMS:
            errs = defaultdict(list)
            for r in train:
                if system in r["xst"]:
                    errs[r["station"]].append((r["truth"] - r["xst"][system], decay(r["day"], ref)))
            if not errs:
                continue
            allv = [e for v in errs.values() for e, _w in v]
            allw = [w for v in errs.values() for _e, w in v]
            b_fam = wmedian(allv, allw)
            stats = {}
            within = []
            for station, v in errs.items():
                e = np.array([x for x, _ in v])
                w = np.array([y for _, y in v])
                neff = w.sum() ** 2 / (w ** 2).sum()
                b_s = wmedian(list(e), list(w))
                if len(v) > 1:
                    mean = (w * e).sum() / w.sum()
                    within.append(((w * (e - mean) ** 2).sum() / w.sum(), w.sum()))
                stats[station] = (b_s, neff)
            sigma2 = (sum(a * b for a, b in within) / sum(b for _a, b in within)) if within else 0.0
            eligible = [(b, n) for b, n in stats.values() if n >= MIN_NEFF]
            if len(eligible) >= 2:
                bs = np.array([b for b, _ in eligible])
                tau2 = max(bs.var(ddof=1) - statistics.mean(sigma2 / n for _b, n in eligible), 0.0)
            else:
                tau2 = 0.0
            ks = {}
            for station, (b_s, neff) in stats.items():
                k = 0.0 if neff < MIN_NEFF or tau2 <= 0 or sigma2 <= 0 else neff / (neff + sigma2 / tau2)
                ks[station] = (b_s, k)
            out[system] = (b_fam, ks)
        return out

    def climate(self, train, station, ref: date):
        vals = [(r["truth"], decay(r["day"], ref)) for r in train
                if r["station"] == station and (ref - date.fromisoformat(r["day"])).days <= CLIM_DAYS]
        if not vals:
            return None
        return sum(t * w for t, w in vals) / sum(w for _t, w in vals)

    def run(self):
        # Pass 1: every row's own as-of L2 bias and climate.
        for day in self.days:
            rows = self.by_day[day.isoformat()]
            listing = rows[0]["listing"]
            train = self.train(listing)
            bias = self.bias(train, day)
            for r in rows:
                key = (r["station"], r["day"])
                self.clim[key] = self.climate(train, r["station"], day)
                bh = {}
                for system in r["xst"]:
                    if system not in bias:
                        bh[system] = 0.0
                        continue
                    b_fam, ks = bias[system]
                    b_s, k = ks.get(r["station"], (b_fam, 0.0))
                    bh[system] = b_fam + k * (b_s - b_fam)
                self.l2[key] = bh
        # Pass 2: every row's own as-of L3 / L3s member values.
        for day in self.days:
            rows = self.by_day[day.isoformat()]
            listing = rows[0]["listing"]
            train = self.train(listing)
            models, smodels = {}, {}
            for system in SYSTEMS:
                X, Xs, y, ys, days = [], [], [], [], set()
                for r in train:
                    key = (r["station"], r["day"])
                    if system not in r["xst"] or system not in self.l2[key]:
                        continue
                    resid = r["truth"] - r["xst"][system] - self.l2[key][system]
                    Xs.append([date.fromisoformat(r["day"]).toordinal()])
                    f = features(r["members"][system], r["xst"][system], self.clim[key], system, None, r.get("om"))
                    if f is not None:
                        X.append(f)
                        y.append(resid)
                        days.add(r["day"])
                    ys.append(resid)
                if len(y) >= L3_MIN_ROWS and len(days) >= L3_MIN_DAYS:
                    models[system] = lae(np.array(X), np.array(y))
                if len(ys) >= L3_MIN_ROWS:
                    smodels[system] = lae(np.array(Xs, dtype=float), np.array(ys))
            for r in rows:
                key = (r["station"], r["day"])
                l3, l3s = {}, {}
                for system, xst in r["xst"].items():
                    base = xst + self.l2[key][system]
                    corr = 0.0
                    if system in models:
                        f = features(r["members"][system], xst, self.clim[key], system, None, r.get("om"))
                        if f is not None:
                            mu, sd, beta = models[system]
                            corr = float(((np.array(f) - mu) / sd) @ beta[:-1] + beta[-1])
                    l3[system] = base + corr
                    scorr = 0.0
                    if system in smodels:
                        mu, sd, beta = smodels[system]
                        scorr = float(((np.array([day.toordinal()]) - mu) / sd) @ beta[:-1] + beta[-1])
                    l3s[system] = base + scorr
                self.l3[key], self.l3s[key] = l3, l3s

    def weighted(self, r, layer, train, ref: date):
        """L4 on top of ``layer``: shrunk inverse-MAE weights from the station's own training errors."""

        values = layer[(r["station"], r["day"])]
        maes, neffs = {}, {}
        own = [q for q in self.by_station[r["station"]] if q["settled"] <= r["listing"]]
        for system in values:
            errs = [(abs(q["truth"] - layer[(q["station"], q["day"])][system]), decay(q["day"], ref))
                    for q in own if system in layer.get((q["station"], q["day"]), {})]
            if errs:
                w = np.array([b for _a, b in errs])
                maes[system] = float(sum(a * b for a, b in errs) / w.sum())
                neffs[system] = float(w.sum() ** 2 / (w ** 2).sum())
        equal = 1.0 / len(values)
        inv = {s: 1.0 / max(maes[s], 0.1) for s in values if s in maes and neffs[s] >= MIN_NEFF}
        total = sum(inv.values())
        weights = {}
        for s in values:
            if s in inv:
                lam = neffs[s] / (neffs[s] + N0)
                weights[s] = lam * inv[s] / total * (len(inv) / len(values)) + (1 - lam) * equal
            else:
                weights[s] = equal
        norm = sum(weights.values())
        return sum(values[s] * weights[s] / norm for s in values)


def score() -> None:
    rows = json.load(open(HERE / "dataset.json"))
    walk = Walk(rows)
    walk.run()
    l2vals = {key: {s: walk.rows_by_key[key]["xst"][s] + b for s, b in bh.items()} for key, bh in walk.l2.items()}
    a0 = {}
    if R73.exists():
        for line in open(R73):
            q = json.loads(line)
            a0[(q["station"], q["day"])] = q["arms"]["A0"]
    out = []
    for r in rows:
        day = date.fromisoformat(r["day"])
        if not (EVAL_FROM <= day <= EVAL_TO) or len(r["xst"]) < MIN_MEMBERS:
            continue
        key = (r["station"], r["day"])
        train = walk.train(r["listing"])
        arms = {
            "L0": statistics.median(m["x"] for m in r["members"].values()),
            "L1": statistics.median(r["xst"].values()),
            "L2": statistics.median(r["xst"][s] + walk.l2[key][s] for s in r["xst"]),
            "L3": statistics.median(walk.l3[key].values()),
            "L3s": statistics.median(walk.l3s[key].values()),
            "L4_on_L3": walk.weighted(r, walk.l3, train, day),
            "L4_on_L2": walk.weighted(r, l2vals, train, day),
        }
        if key in a0:
            arms["F"] = a0[key]
        edges = r["edges"]
        if edges:
            tb = L._rung_of(edges, r["truth"])
            hit = {k: L._rung_of(edges, v) == tb for k, v in arms.items()}
        else:
            hit = {k: math.floor(v + 0.5) == math.floor(r["truth"] + 0.5) for k, v in arms.items()}
        out.append({"station": r["station"], "day": r["day"], "truth": r["truth"],
                    "arms": {k: round(v, 3) for k, v in arms.items()}, "hit": hit, "n_members": len(r["xst"])})
    name = "walk.jsonl" if FEATURE_SOURCE == "native" else "walk_openmeteo.jsonl"
    with open(HERE / name, "w") as fh:
        for q in out:
            fh.write(json.dumps(q) + "\n")
    if FOCUS_ONLY:
        focus_report([q for q in out if q["station"] == FOCUS_ONLY])
    else:
        report(out)


def paired(rows, a, b):
    by = defaultdict(list)
    for q in rows:
        by[q["day"]].append(abs(q["truth"] - q["arms"][a]) - abs(q["truth"] - q["arms"][b]))
    days = sorted(by)
    point = sum(sum(v) for v in by.values()) / sum(len(v) for v in by.values())
    rng = random.Random(SEED)
    bs = []
    for _ in range(BOOT):
        pick = [by[rng.choice(days)] for _ in days]
        bs.append(sum(sum(v) for v in pick) / sum(len(v) for v in pick))
    bs.sort()
    mid = days[len(days) // 2]
    halves = []
    for part in ([d for d in days if d < mid], [d for d in days if d >= mid]):
        vals = [x for d in part for x in by[d]]
        halves.append(sum(vals) / len(vals))
    return point, bs[int(0.025 * BOOT)], bs[int(0.975 * BOOT) - 1], halves


def table(title, rows, arms):
    print(f"\n### {title}: {len(rows)} 站日, {len({q['day'] for q in rows})} 天")
    for arm in arms:
        err = [q["truth"] - q["arms"][arm] for q in rows]
        print(f"{arm:9s} MAE {statistics.mean(abs(e) for e in err):.3f}  偏差 {statistics.mean(err):+.3f}"
              f"  命中 {sum(q['hit'][arm] for q in rows) / len(rows):.1%}")


def report(out):
    arms = ["L0", "L1", "L2", "L3s", "L3", "L4_on_L3", "L4_on_L2"]
    table("全队", out, arms)
    print("\n逐层（MAE 差，负＝更准）：")
    for a, b in (("L1", "L0"), ("L2", "L1"), ("L3", "L2"), ("L3s", "L2"), ("L3", "L3s"),
                 ("L4_on_L3", "L3"), ("L4_on_L2", "L2")):
        p, lo, hi, h = paired(out, a, b)
        print(f"{a}−{b}: {p:+.4f} [{lo:+.4f}, {hi:+.4f}]  两半 {h[0]:+.4f} / {h[1]:+.4f}")
    inter = [q for q in out if "F" in q["arms"]]
    if inter:
        table("与现行 F 的交集", inter, arms + ["F"])
        for a in arms[1:]:
            p, lo, hi, h = paired(inter, a, "F")
            print(f"{a}−F: {p:+.4f} [{lo:+.4f}, {hi:+.4f}]  两半 {h[0]:+.4f} / {h[1]:+.4f}")
    z = [q for q in out if q["station"] == "ZUUU"]
    if z:
        table("ZUUU（只报不判）", z, arms + (["F"] if all("F" in q["arms"] for q in z) else []))


FOCUS_ONLY = ""


def focus_report(rows):
    """One station's read: the table and each layer against the one below and F."""

    arms = ["L0", "L1", "L2", "L3s", "L3", "L4_on_L3", "L4_on_L2"]
    inter = all("F" in q["arms"] for q in rows)
    table(f"{FOCUS_ONLY}（特征来源 {FEATURE_SOURCE}）", rows, arms + (["F"] if inter else []))
    print("\n逐层（MAE 差，负＝更准）：")
    pairs = [("L1", "L0"), ("L2", "L1"), ("L3", "L2"), ("L3s", "L2"), ("L3", "L3s"), ("L4_on_L3", "L3"), ("L4_on_L2", "L2")]
    if inter:
        pairs += [(a, "F") for a in ("L2", "L3", "L4_on_L3")]
    for a, b in pairs:
        p, lo, hi, h = paired(rows, a, b)
        print(f"{a}−{b}: {p:+.3f} [{lo:+.3f}, {hi:+.3f}]  两半 {h[0]:+.3f} / {h[1]:+.3f}")


if __name__ == "__main__":
    for flag in sys.argv[2:]:
        if flag == "--openmeteo":
            FEATURE_SOURCE = "openmeteo"
        elif flag.startswith("--focus="):
            FOCUS_ONLY = flag.split("=", 1)[1]
    {"build": build, "score": score}[sys.argv[1]]()
