"""Monthly-precipitation markets (NYC Central Park, Seattle City Area): model vs market.

Protocol (fixed before any score was looked at, 2026-10-08):

* Events: every resolved Polymarket "Precipitation in NYC/Seattle in <Month>?"
  event (2025-12 .. 2026-09, 20 events).  Truth = ACIS threaded record the
  rules name (NYCthr, SEWthr); all 20 winning buckets reproduce from it.
* Decision points: 12:00Z each UTC date from max(event creation, month start
  - 7 d) through the month's last day.  Market = last hourly chart price of
  every bucket at or before t (none older than 12 h, else the point is
  skipped), normalised to sum 1.
* Information at t, strictly as of t:
  - observed: ACIS daily totals for climate days (local standard time) <= d-1;
  - forecast: Open-Meteo best_match (what production requests) previous-runs
    precipitation; for each valid hour the shortest offset N in 1..7 whose run
    (issued ~24*N h before the hour) plus 8 h dissemination lag is <= t;
  - climatology: ACIS years 1945 .. (decision year - 1).
* Month split at t: observed days (< d) + forecast window (climate days d..d+5
  inside the month) + remainder (later days of the month).
* Arm F (forecast): window total ~ the 40 nearest historical windows of the
  same length in log1p(forecast mm), drawn from windows fully observed before
  d (2024-01-08 onwards); remainder ~ climatology of the same calendar days in
  every past year with shifts -7..+7 d (years with a missing day skipped).
  Window and remainder independent; total = observed + window + remainder.
* Arm C (climatology only): observed + climatology of all days >= d.
* Scores: multiclass Brier and LogScore after a 1e-3 floor + renormalise
  (charter 224).  Verdict: an arm beats the market only if both mean
  differences (arm - market) are < 0 with event-block bootstrap 95% intervals
  excluding 0 (charter 240 style, 20 events resampled, 10,000 reps).
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import pathlib
import re

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
RAW = HERE / "raw"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
# climate day = local standard time midnight..midnight, all year
LST_OFFSET_H = {"SEW": -8, "NYC": -5}
ACIS_SID = {"SEW": "SEWthr", "NYC": "NYCthr"}
CITY_SITE = {"Seattle": "SEW", "NYC": "NYC"}
WINDOW_DAYS = 6
LAG_H = 8
K_NN = 40
CLIMO_SHIFT = 7
FLOOR = 1e-3
DECISION_HOUR_UTC = 12


# ---------------------------------------------------------------- data
def _json(path):
    """Raw inputs are stored gzipped next to their fetch scripts' output names."""
    gz = path.with_name(path.name + ".gz")
    if gz.exists():
        with gzip.open(gz, "rt") as f:
            return json.load(f)
    return json.load(open(path))


def acis_daily(site):
    """date -> inches, or None when missing / part of an accumulation."""
    rows = _json(RAW / f"acis_{ACIS_SID[site]}.json")["data"]
    out = {}
    for day, raw in rows:
        d = dt.date.fromisoformat(day)
        if raw in ("M", "S") or raw.endswith("A"):
            out[d] = None
        elif raw == "T":
            out[d] = 0.0
        else:
            out[d] = float(raw)
    return out


def forecast_hours(site):
    """valid hour (UTC datetime) -> {N: mm} for N in 1..7."""
    out = {}
    names = sorted({f.name.removesuffix(".gz") for f in RAW.glob(f"prevruns_{site}_*.json*")})
    for name in names:
        h = _json(RAW / name)["hourly"]
        times = h["time"]
        cols = {n: h[f"precipitation_previous_day{n}"] for n in range(1, 8)}
        for i, t in enumerate(times):
            vt = dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc)
            out[vt] = {n: cols[n][i] for n in range(1, 8) if cols[n][i] is not None}
    return out


def window_forecast_mm(fc, site, t, days):
    """Strict as-of forecast total (mm) over the climate days ``days`` at t."""
    total = 0.0
    off = LST_OFFSET_H[site]
    for d in days:
        start = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) - dt.timedelta(hours=off)
        for k in range(24):
            # Open-Meteo hourly precipitation = total of the preceding hour
            vt = start + dt.timedelta(hours=k + 1)
            vals = fc.get(vt)
            if not vals:
                return None
            pick = None
            for n in range(1, 8):
                if vt - dt.timedelta(hours=24 * n) + dt.timedelta(hours=LAG_H) <= t and n in vals:
                    pick = vals[n]
                    break
            if pick is None:
                return None
            total += pick
    return total


def obs_sum(obs, days):
    s = 0.0
    for d in days:
        v = obs.get(d)
        if v is None:
            return None
        s += v
    return s


# ---------------------------------------------------------------- model pieces
def climo_samples(obs, days, before_year):
    """Totals of the same calendar days in each past year, shifted -7..+7 d."""
    if not days:
        return np.zeros(1)
    out = []
    for y in range(1945, before_year):
        for s in range(-CLIMO_SHIFT, CLIMO_SHIFT + 1):
            try:
                shifted = [dt.date(y, d.month, d.day) + dt.timedelta(days=s) for d in days]
            except ValueError:  # Feb 29 in a non-leap year
                continue
            v = obs_sum(obs, shifted)
            if v is not None:
                out.append(v)
    return np.array(out)


class WindowPairs:
    """Historical (forecast, observed) window totals, built lazily per length."""

    def __init__(self, site, obs, fc):
        self.site, self.obs, self.fc = site, obs, fc
        self.cache = {}

    def pairs(self, length):
        if length not in self.cache:
            rows = []
            d = dt.date(2024, 1, 8)
            last = max(self.obs)
            while d + dt.timedelta(days=length - 1) <= last:
                days = [d + dt.timedelta(days=i) for i in range(length)]
                t = dt.datetime(d.year, d.month, d.day, DECISION_HOUR_UTC, tzinfo=dt.timezone.utc)
                f = window_forecast_mm(self.fc, self.site, t, days)
                o = obs_sum(self.obs, days)
                if f is not None and o is not None:
                    rows.append((d, f, o))
                d += dt.timedelta(days=1)
            self.cache[length] = rows
        return self.cache[length]

    def conditional(self, length, f_mm, decision_day):
        usable = [(f, o) for d0, f, o in self.pairs(length)
                  if d0 + dt.timedelta(days=length - 1) < decision_day]
        if len(usable) < K_NN:
            return None
        arr = np.array(usable)
        dist = np.abs(np.log1p(arr[:, 0]) - math.log1p(f_mm))
        idx = np.argsort(dist, kind="stable")[:K_NN]
        return arr[idx, 1]


# ---------------------------------------------------------------- buckets & scores
def parse_bucket(label):
    s = label.replace('"', "").replace("″", "").strip()
    if s.startswith("<"):
        return (-math.inf, float(s[1:]))
    if s.startswith(">"):
        return (float(s[1:]), math.inf)
    lo, hi = re.split(r"\s*-\s*", s)
    return (float(lo), float(hi))


def bucket_probs(totals, edges):
    t = np.round(totals, 2)
    p = np.array([np.mean((t >= lo) & (t < hi)) for lo, hi in edges])
    return p


def total_distribution(base, *parts):
    acc = np.array([base])
    for part in parts:
        acc = (acc[:, None] + part[None, :]).ravel()
    return acc


def floor_norm(p):
    p = np.maximum(np.asarray(p, float), FLOOR)
    return p / p.sum()


def scores(p, k):
    p = floor_norm(p)
    y = np.zeros_like(p)
    y[k] = 1.0
    return float(np.sum((p - y) ** 2)), float(-math.log(p[k]))


# ---------------------------------------------------------------- run
def main():
    events = json.load(open(HERE / "events.json"))
    with gzip.open(RAW / "prices_hourly.json.gz", "rt") as f:
        prices = {k: np.array(v) for k, v in json.load(f).items()}
    sites = {}
    for site in ("SEW", "NYC"):
        obs = acis_daily(site)
        fc = forecast_hours(site)
        sites[site] = (obs, fc, WindowPairs(site, obs, fc))

    rows = []
    for e in events:
        if not e["closed"]:
            continue
        city = "Seattle" if "Seattle" in e["title"] else "NYC"
        site = CITY_SITE[city]
        obs, fc, wp = sites[site]
        month = MONTHS.index(e["title"].split(" in ")[-1].rstrip("?")) + 1
        year = 2025 if month == 12 else 2026
        mdays = [dt.date(year, month, 1) + dt.timedelta(days=i) for i in range(31)]
        mdays = [d for d in mdays if d.month == month]
        truth = obs_sum(obs, mdays)
        markets = e["markets"]
        edges = [parse_bucket(m["label"]) for m in markets]
        order = np.argsort([lo for lo, _ in edges])
        markets = [markets[i] for i in order]
        edges = [edges[i] for i in order]
        k_true = [i for i, (lo, hi) in enumerate(edges) if lo <= round(truth, 2) < hi][0]
        won = [i for i, m in enumerate(markets) if (m["outcome_prices"] or "").startswith('["1')]
        assert won == [k_true], (e["title"], truth, won, k_true)

        created = dt.datetime.fromisoformat(e["start"].replace("Z", "+00:00")).date()
        d = max(created + dt.timedelta(days=1), mdays[0] - dt.timedelta(days=7))
        while d <= mdays[-1]:
            t = dt.datetime(d.year, d.month, d.day, DECISION_HOUR_UTC, tzinfo=dt.timezone.utc)
            ts = t.timestamp()
            mkt = []
            for m in markets:
                ser = prices.get(m["yes_token"])
                if ser is None or len(ser) == 0:
                    mkt = None
                    break
                j = np.searchsorted(ser[:, 0], ts, side="right") - 1
                if j < 0 or ts - ser[j, 0] > 12 * 3600:
                    mkt = None
                    break
                mkt.append(ser[j, 1])
            if mkt is None or sum(mkt) <= 0:
                d += dt.timedelta(days=1)
                continue
            mkt = np.array(mkt) / sum(mkt)

            observed_days = [x for x in mdays if x < d]
            mtd = obs_sum(obs, observed_days)
            window = [x for x in mdays if d <= x <= d + dt.timedelta(days=WINDOW_DAYS - 1)]
            remainder = [x for x in mdays if x > d + dt.timedelta(days=WINDOW_DAYS - 1)]
            future = [x for x in mdays if x >= d]
            if mtd is None:
                d += dt.timedelta(days=1)
                continue

            p_c = bucket_probs(total_distribution(mtd, climo_samples(obs, future, year)), edges)
            p_f = None
            f_mm = None
            if window:
                f_mm = window_forecast_mm(fc, site, t, window)
                cond = wp.conditional(len(window), f_mm, d) if f_mm is not None else None
                if cond is not None:
                    p_f = bucket_probs(
                        total_distribution(mtd, cond, climo_samples(obs, remainder, year)), edges)
            else:
                p_f = p_c
            if p_f is None:
                d += dt.timedelta(days=1)
                continue
            row = {"event": e["title"], "city": city, "month": f"{year}-{month:02d}", "date": d.isoformat(),
                   "day_of_month": (d - mdays[0]).days + 1, "truth_in": truth, "k_true": k_true,
                   "mtd_in": mtd, "window_days": len(window), "window_fc_in": None if f_mm is None else f_mm / 25.4,
                   "p_market": mkt.round(4).tolist(), "p_clim": p_c.round(4).tolist(), "p_fc": p_f.round(4).tolist()}
            for arm, p in (("market", mkt), ("clim", p_c), ("fc", p_f)):
                row[f"brier_{arm}"], row[f"log_{arm}"] = scores(p, k_true)
            rows.append(row)
            d += dt.timedelta(days=1)

    with gzip.open(HERE / "rows.jsonl.gz", "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    # ------------------------------------------------ report
    rng = np.random.default_rng(20261008)
    ev_names = sorted({r["event"] for r in rows})
    by_ev = {n: [r for r in rows if r["event"] == n] for n in ev_names}

    def boot(arm, metric, subset=None):
        sel = {n: [r for r in rs if subset is None or subset(r)] for n, rs in by_ev.items()}
        sel = {n: rs for n, rs in sel.items() if rs}
        names = list(sel)
        diffs = {n: np.array([r[f"{metric}_{arm}"] - r[f"{metric}_market"] for r in rs]) for n, rs in sel.items()}
        allv = np.concatenate([diffs[n] for n in names])
        point = float(allv.mean())
        reps = []
        for _ in range(10000):
            pick = rng.integers(0, len(names), len(names))
            v = np.concatenate([diffs[names[i]] for i in pick])
            reps.append(v.mean())
        lo, hi = np.percentile(reps, [2.5, 97.5])
        return point, float(lo), float(hi), len(allv), len(names)

    report = {"protocol": __doc__, "n_rows": len(rows), "n_events": len(ev_names), "arms": {}}
    segs = {
        "all": None,
        "seattle": lambda r: r["city"] == "Seattle",
        "nyc": lambda r: r["city"] == "NYC",
        "before_month": lambda r: r["day_of_month"] <= 0,
        "days_1_10": lambda r: 1 <= r["day_of_month"] <= 10,
        "days_11_20": lambda r: 11 <= r["day_of_month"] <= 20,
        "days_21_end": lambda r: r["day_of_month"] >= 21,
    }
    means = {arm: {m: float(np.mean([r[f"{m}_{arm}"] for r in rows])) for m in ("brier", "log")}
             for arm in ("market", "clim", "fc")}
    report["mean_scores"] = means
    for arm in ("clim", "fc"):
        report["arms"][arm] = {}
        for seg, fn in segs.items():
            out = {}
            for metric in ("brier", "log"):
                pt, lo, hi, n, ne = boot(arm, metric, fn)
                out[metric] = {"diff_vs_market": round(pt, 4), "ci95": [round(lo, 4), round(hi, 4)], "n": n, "events": ne}
            report["arms"][arm][seg] = out
    # forecast vs climatology (does the forecast add anything at all?)
    fc_vs_clim = {}
    for metric in ("brier", "log"):
        v = np.array([r[f"{metric}_fc"] - r[f"{metric}_clim"] for r in rows])
        fc_vs_clim[metric] = round(float(v.mean()), 4)
    report["fc_minus_clim"] = fc_vs_clim
    json.dump(report, open(HERE / "report.json", "w"), indent=1, ensure_ascii=False)
    print(json.dumps({k: v for k, v in report.items() if k != "protocol"}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
