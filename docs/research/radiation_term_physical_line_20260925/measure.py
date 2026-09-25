#!/usr/bin/env python3
"""Does the physical line carry information the temperature line does not?

Two lines, per stage (user, 2026-09-25: 「taf报文的物理证据、和metar的物理证据
和温度线是两条线，检查这个是否有信息增益」):

* temperature line -- P0's calibrated centre, the TAF TX likelihood at P0-M,
  the METAR temperature path at P1;
* physical line -- P0's radiation cooling term (charter 160/210:
  0.07 x Kt z x sigma), inherited and revised at P0-M by the TAF's cloud /
  precipitation / wind over the heating window, and at P1 by the METAR's
  observed cloud / precipitation / wind over the elapsed daylight plus the
  inherited TAF forecast for the rest of it.

Every arm is fitted walk-forward: a decision on event day d is scored with
parameters fitted only on station-days settled by d-2.  The score is the log
probability of the settled bucket (1 C, or the 2 F pair) -- the production
criterion (charter 211) -- with the bucket Brier beside it.  Gains are
averaged per event day and bootstrapped over days (stations on one day share
weather, so the day is the independent unit).
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from scipy.special import ndtr

LOG_FLOOR = 1e-9
RADIATION_SHIFT_SIGMA = 0.07
BIAS_HALF_LIFE_DAYS = 15.0
SIGMA_HALF_LIFE_DAYS = 30.0
MIN_BIAS_DAYS = 10
SIGMA_FLOOR_C = 0.7
TAF_SHRINK_K = 15.0
MIN_TRAIN_ROWS = 150
SETTLE_LAG_DAYS = 2
BOOTSTRAP = 2000


# ----------------------------------------------------------------- scoring
def bucket(value_rep: float, unit: str) -> tuple[float, float]:
    """Edges, on the settlement scale, of the bucket holding ``value_rep``."""

    if unit == "F":
        low = math.floor(round(value_rep) / 2.0) * 2.0
        return low - 0.5, low + 1.5
    k = round(value_rep)
    return k - 0.5, k + 0.5


def to_unit(mu_c: float, sd_c: float, unit: str) -> tuple[float, float]:
    if unit == "F":
        return mu_c * 9.0 / 5.0 + 32.0, sd_c * 9.0 / 5.0
    return mu_c, sd_c


def _cdf(x: float, mu: float, sd: float) -> float:
    return float(ndtr((x - mu) / sd))


def score_gaussian(mu_c: float, sd_c: float, truth_rep: float, unit: str) -> tuple[float, float]:
    """(log score, bucket Brier) of the settled bucket under N(mu, sd)."""

    mu, sd = to_unit(mu_c, sd_c, unit)
    low, high = bucket(truth_rep, unit)
    width = high - low
    p_truth = _cdf(high, mu, sd) - _cdf(low, mu, sd)
    # Sum of squared bucket probabilities over +-8 sigma.
    first = math.floor((mu - 8 * sd - low) / width)
    last = math.ceil((mu + 8 * sd - low) / width)
    total = 0.0
    for k in range(first, last + 1):
        lo = low + k * width
        p = _cdf(lo + width, mu, sd) - _cdf(lo, mu, sd)
        total += p * p
    return math.log(max(p_truth, LOG_FLOOR)), total - 2.0 * p_truth + 1.0


def score_truncated(mu_c: float, sd_c: float, running_rep: float, truth_rep: float, unit: str) -> tuple[float, float]:
    """Scores when the day is max(running high, rest-of-day max ~ N(mu, sd))."""

    mu, sd = to_unit(mu_c, sd_c, unit)
    run_low, run_high = bucket(running_rep, unit)
    width = run_high - run_low

    def p_of(k: int) -> float:
        lo = run_low + k * width
        if k == 0:
            return _cdf(run_high, mu, sd)
        return _cdf(lo + width, mu, sd) - _cdf(lo, mu, sd)

    truth_low, _ = bucket(truth_rep, unit)
    k_truth = int(round((truth_low - run_low) / width))
    if k_truth < 0:
        raise ValueError("truth_below_running_high")
    last = max(k_truth, math.ceil((mu + 8 * sd - run_low) / width))
    probs = [p_of(k) for k in range(0, last + 1)]
    p_truth = probs[k_truth]
    brier = sum(p * p for p in probs) - 2.0 * p_truth + 1.0
    return math.log(max(p_truth, LOG_FLOOR)), brier


# ----------------------------------------------------------------- bootstrap
def summarise(pairs: list[tuple[str, float]], *, seed: int = 7) -> dict:
    """By-day mean of per-decision differences, day-clustered 95% interval."""

    by_day: dict[str, list[float]] = defaultdict(list)
    for day, value in pairs:
        by_day[day].append(value)
    days = sorted(by_day)
    if not days:
        return {"n": 0}
    means = np.array([np.mean(by_day[d]) for d in days])
    rng = random.Random(seed)
    boots = []
    for _ in range(BOOTSTRAP):
        sample = [means[rng.randrange(len(means))] for _ in range(len(means))]
        boots.append(float(np.mean(sample)))
    boots.sort()
    half = len(means) // 2
    return {
        "n": len(pairs),
        "days": len(days),
        "mean": float(np.mean(means)),
        "ci95": [boots[int(0.025 * BOOTSTRAP)], boots[int(0.975 * BOOTSTRAP) - 1]],
        "days_better": int(np.sum(means > 0)),
        "per_decision_mean": float(np.mean([v for _, v in pairs])),
        # Split by date: an effect that lives in one half only is not one
        # the next season can count on.
        "first_half_mean": float(np.mean(means[:half])) if half else None,
        "second_half_mean": float(np.mean(means[half:])) if half else None,
    }


# ----------------------------------------------------------------- walk-forward OLS
class WalkForward:
    """Least squares refitted per test day on rows settled by d - lag."""

    def __init__(self, dates: list[str], X: np.ndarray, y: np.ndarray, *, ridge: float = 1e-3):
        order = np.argsort(np.array(dates), kind="stable")
        self.dates = [dates[i] for i in order]
        self.X = X[order]
        self.y = y[order]
        self.order = order
        self.ridge = ridge
        p = X.shape[1]
        unique = sorted(set(self.dates))
        self.unique = unique
        self.cum = {}
        xtx = np.zeros((p, p))
        xty = np.zeros(p)
        yty = 0.0
        n = 0
        position = 0
        for day in unique:
            while position < len(self.dates) and self.dates[position] == day:
                row = self.X[position]
                xtx += np.outer(row, row)
                xty += row * self.y[position]
                yty += self.y[position] ** 2
                n += 1
                position += 1
            self.cum[day] = (xtx.copy(), xty.copy(), yty, n)

    def fit_before(self, day: str, lag: int = SETTLE_LAG_DAYS):
        cutoff = (date.fromisoformat(day) - timedelta(days=lag)).isoformat()
        eligible = [d for d in self.unique if d <= cutoff]
        if not eligible:
            return None
        xtx, xty, yty, n = self.cum[eligible[-1]]
        if n < MIN_TRAIN_ROWS:
            return None
        p = xtx.shape[0]
        penalty = self.ridge * np.trace(xtx) / p * np.eye(p)
        penalty[0, 0] = 0.0  # the intercept is not shrunk
        beta = np.linalg.solve(xtx + penalty, xty)
        rss = yty - 2.0 * beta @ xty + beta @ xtx @ beta
        sigma = math.sqrt(max(rss / n, 1e-6))
        return beta, sigma, n


def _num(value, default=None):
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


# ----------------------------------------------------------------- P0
# Which day-ahead maximum stands in for P0's centre: ``global`` is the mean of
# four global models (ECMWF IFS, GFS, ICON, GEM) -- production's P0 is a
# calibrated mixture of global ensembles; ``best_match`` is Open-Meteo's
# blend, which leans on regional high-resolution models where they exist.
P0_SOURCE = "global"


def model_max(row: dict) -> float | None:
    # ``production`` rows (build_cases_ssot.py) carry the served root's own
    # hourly curve maximum under the same key as the global proxy.
    if P0_SOURCE in ("global", "production"):
        return _num(row.get("global_max_c"))
    return _num(row.get("model_max_c"))


def production_roots(days: list[dict]) -> dict[tuple[str, str], dict]:
    """The served P0 root at the morning cutoff, as build_cases_ssot.py read it."""

    roots = {}
    for row in days:
        mu, sigma = _num(row.get("p0_mu_c")), _num(row.get("p0_sigma_c"))
        c0 = _num(row.get("p0_c0"))
        if mu is None or sigma is None or c0 is None or sigma <= 0:
            continue
        roots[(row["station"], row["date"])] = {
            "c0": c0, "sigma": max(sigma, 0.3), "mu": mu,
            "radiation_c": _num(row.get("p0_radiation_c"), 0.0),
        }
    return roots


def p0_roots(days: list[dict]) -> dict[tuple[str, str], dict]:
    """The P0 temperature line per station-day, strictly as of d - 2.

    Centre: the day-ahead model maximum plus the station's exponentially
    weighted past error (half-life 15 days); width: its weighted RMS
    (half-life 30 days, floor 0.7 C); the radiation term as served
    (0.07 x Kt z x sigma).
    """

    by_station: dict[str, list[dict]] = defaultdict(list)
    for row in days:
        by_station[row["station"]].append(row)
    roots = {}
    for station, rows in by_station.items():
        rows.sort(key=lambda r: r["date"])
        history: list[tuple[date, float]] = []
        for row in rows:
            today = date.fromisoformat(row["date"])
            base = model_max(row)
            if base is None:
                continue
            usable = [(d, e) for d, e in history if (today - d).days >= SETTLE_LAG_DAYS]
            error = row["truth_c"] - base
            if len(usable) >= MIN_BIAS_DAYS:
                wb = np.array([0.5 ** ((today - d).days / BIAS_HALF_LIFE_DAYS) for d, _ in usable])
                ws = np.array([0.5 ** ((today - d).days / SIGMA_HALF_LIFE_DAYS) for d, _ in usable])
                errors = np.array([e for _, e in usable])
                bias = float(np.sum(wb * errors) / np.sum(wb))
                sigma = max(SIGMA_FLOOR_C, float(math.sqrt(np.sum(ws * (errors - bias) ** 2) / np.sum(ws))))
                c0 = base + bias
                kt_z = _num(row.get("kt_z"))
                radiation_c = RADIATION_SHIFT_SIGMA * kt_z * sigma if kt_z is not None else 0.0
                roots[(station, row["date"])] = {
                    "c0": c0, "sigma": sigma, "radiation_c": radiation_c,
                    "mu": c0 + radiation_c,
                }
            history.append((today, error))
    return roots


def run_p0(days: list[dict], roots: dict) -> dict:
    """Replica check: does the served radiation term score on this data?"""

    pairs_log, pairs_brier = [], []
    for row in days:
        root = roots.get((row["station"], row["date"]))
        if root is None or _num(row.get("kt_z")) is None:
            continue
        with_r = score_gaussian(root["mu"], root["sigma"], row["truth_rep"], row["unit"])
        without = score_gaussian(
            root["mu"] - root["radiation_c"], root["sigma"], row["truth_rep"], row["unit"]
        )
        pairs_log.append((row["date"], with_r[0] - without[0]))
        pairs_brier.append((row["date"], without[1] - with_r[1]))
    return {
        "radiation_term_logscore_gain": summarise(pairs_log),
        "radiation_term_brier_gain": summarise(pairs_brier),
    }


# ----------------------------------------------------------------- P0-M
def kasten_czeplak(cloud: float) -> float:
    """Global radiation relative to clear sky at cloud fraction ``cloud``."""

    return 1.0 - 0.75 * max(0.0, min(1.0, cloud)) ** 3.4


def p0m_cases(days: list[dict], roots: dict, which: str) -> list[dict]:
    cases = []
    for row in days:
        root = roots.get((row["station"], row["date"]))
        taf = row.get(f"taf_{which}")
        if root is None or not taf:
            continue
        cases.append({**row, "root": root, "taf": taf})
    return cases


def fit_tx_line(train: list[dict]) -> dict | None:
    """Charter 150's line x = a + b*y (x = TX - c0, y = truth - c0), pooled and shrunk per station."""

    rows = [(c["station"], c["taf"]["tx_c"] - c["root"]["c0"], c["truth_c"] - c["root"]["c0"])
            for c in train if c["taf"].get("tx_c") is not None]
    if len(rows) < 40:
        return None

    def ols(items):
        ys = np.array([y for _, _, y in items])
        xs = np.array([x for _, x, _ in items])
        A = np.vstack([np.ones_like(ys), ys]).T
        coef, *_ = np.linalg.lstsq(A, xs, rcond=None)
        resid = xs - A @ coef
        return float(coef[0]), float(coef[1]), float(max(0.25, np.sqrt(np.mean(resid ** 2))))

    pool = ols(rows)
    stations = {}
    grouped = defaultdict(list)
    for item in rows:
        grouped[item[0]].append(item)
    for station, items in grouped.items():
        if len(items) < 5:
            continue
        own = ols(items)
        w = len(items) / (len(items) + TAF_SHRINK_K)
        stations[station] = tuple(w * o + (1 - w) * p for o, p in zip(own, pool))
    return {"pool": pool, "stations": stations}


def tx_posterior(case: dict, line: dict | None) -> tuple[float, float]:
    root = case["root"]
    mu0, s0, c0 = root["mu"], root["sigma"], root["c0"]
    tx = case["taf"].get("tx_c")
    if tx is None or line is None:
        return mu0, s0
    a, b, tau = line["stations"].get(case["station"], line["pool"])
    x = tx - c0
    precision = 1.0 / s0 ** 2 + b * b / tau ** 2
    mean = (mu0 / s0 ** 2 + b * (x - a + b * c0) / tau ** 2) / precision
    return float(mean), float(math.sqrt(1.0 / precision))


DEBIAS_HALF_LIFE_DAYS = 30.0
DEBIAS_MIN_ROWS = 5


def station_debiased(items: list[dict], feature, *, group=lambda item: None) -> list[list[float]]:
    """``feature(item)`` minus the station's own exponentially weighted past mean.

    A forecaster's cloud (oktas of the thickest layer) and a model's (total
    cover in percent) or a METAR's (FEW/SCT/BKN/OVC) are different scales,
    and how far apart they sit is a property of the station; the pooled fit
    cannot take that offset out.  Only rows settled by d - 2 inform the mean,
    as everywhere else here; with fewer than five the raw value is centred on
    the pooled past mean instead.
    """

    order = sorted(range(len(items)), key=lambda i: items[i]["date"])
    history: dict = defaultdict(list)
    pooled: list[tuple[date, list[float]]] = []
    out: list[list[float] | None] = [None] * len(items)
    for index in order:
        item = items[index]
        today = date.fromisoformat(item["date"])
        values = feature(item)
        key = (item["station"], group(item))
        usable = [(d, v) for d, v in history[key] if (today - d).days >= SETTLE_LAG_DAYS]
        if len(usable) < DEBIAS_MIN_ROWS:
            usable = [(d, v) for d, v in pooled if (today - d).days >= SETTLE_LAG_DAYS]
        if usable:
            weights = np.array([0.5 ** ((today - d).days / DEBIAS_HALF_LIFE_DAYS) for d, _ in usable])
            means = np.average(np.array([v for _, v in usable]), axis=0, weights=weights)
            out[index] = [float(v - m) for v, m in zip(values, means)]
        else:
            out[index] = [0.0 for _ in values]
        history[key].append((today, values))
        pooled.append((today, values))
    return out  # type: ignore[return-value]


def _rev(c: dict) -> list[float]:
    return [
        c["taf"]["cloud"] - c["model_cloud"],
        c["taf"]["precip"] - c["model_wet"],
        c["taf"]["wind"] - c["model_wind"],
    ]


P0M_ARMS = {
    # name: feature builder (list of floats) -- the intercept is always in.
    "T": lambda c: [],
    "T+TAF": lambda c: [c["taf"]["cloud"], c["taf"]["precip"], c["taf"]["wind"]],
    "T+rev": lambda c: [
        c["taf"]["cloud"] - c["model_cloud"],
        c["taf"]["precip"] - c["model_wet"],
        c["taf"]["wind"] - c["model_wind"],
    ],
    "T+M": lambda c: [c["kt_z_or0"], c["model_cloud"], c["model_wet"], c["model_wind"]],
    "T+M+TAF": lambda c: [
        c["kt_z_or0"], c["model_cloud"], c["model_wet"], c["model_wind"],
        c["taf"]["cloud"], c["taf"]["precip"], c["taf"]["wind"],
    ],
    # The revision with each station's own TAF-versus-model offset removed.
    "T+M+revd": lambda c: [
        c["kt_z_or0"], c["model_cloud"], c["model_wet"], c["model_wind"],
    ] + c["rev_debiased"],
    # CEILING, not an arm anyone could serve: the heating window's cloud and
    # rain as the METARs later reported them (06-16 local, solar-weighted),
    # minus the model's.  What a perfect TAF could have added at most.
    "T+M+oracle": lambda c: [
        c["kt_z_or0"], c["model_cloud"], c["model_wet"], c["model_wind"],
    ] + c["oracle"],
}
P0M_COMPARISONS = (
    ("T+TAF", "T"), ("T+rev", "T"), ("T+M+TAF", "T+M"), ("T+M", "T"),
    ("T+M+revd", "T+M"), ("T+M+oracle", "T+M"),
)


def run_p0m(days: list[dict], roots: dict, which: str, oracle: dict | None = None) -> dict:
    cases = p0m_cases(days, roots, which)
    oracle = oracle or {}
    cases = [c for c in cases if (c["station"], c["date"]) in oracle]
    for c in cases:
        c["kt_z_or0"] = _num(c.get("kt_z"), 0.0)
        cloud, wet = oracle[(c["station"], c["date"])]
        c["oracle"] = [cloud - c["model_cloud"], wet - c["model_wet"]]
    for c, values in zip(cases, station_debiased(cases, _rev)):
        c["rev_debiased"] = values
    dates = sorted({c["date"] for c in cases})
    # TX line, refitted per test day on settled days.
    by_date = defaultdict(list)
    for c in cases:
        by_date[c["date"]].append(c)
    for day in dates:
        cutoff = (date.fromisoformat(day) - timedelta(days=SETTLE_LAG_DAYS)).isoformat()
        train = [c for c in cases if c["date"] <= cutoff]
        line = fit_tx_line(train)
        for c in by_date[day]:
            c["mu_t"], c["sd_t"] = tx_posterior(c, line)
            c["r"] = (c["truth_c"] - c["mu_t"]) / c["sd_t"]
    # The literal inheritance: the P0 term revised by the TAF's cloud with
    # the served coefficient and no fitting (Kasten-Czeplak cloud-to-radiation).
    literal = []
    for c in cases:
        sd_hist = _num(c.get("kt_hist_sd"))
        kt = _num(c.get("kt"))
        if sd_hist is None or kt is None or sd_hist <= 0:
            continue
        revised_kt = kt * kasten_czeplak(c["taf"]["cloud"]) / kasten_czeplak(c["model_cloud"])
        dz = (revised_kt - kt) / sd_hist
        shifted = c["mu_t"] + RADIATION_SHIFT_SIGMA * dz * c["root"]["sigma"]
        a = score_gaussian(shifted, c["sd_t"], c["truth_rep"], c["unit"])
        b = score_gaussian(c["mu_t"], c["sd_t"], c["truth_rep"], c["unit"])
        literal.append((c["date"], a[0] - b[0], b[1] - a[1], dz))
    sources: dict[str, int] = defaultdict(int)
    for c in cases:
        sources[c["taf"]["source"]] += 1
    case_by_key = {(c["station"], c["date"]): c for c in cases}
    results: dict = {
        "cases": len(cases),
        "with_tx": sum(1 for c in cases if c["taf"].get("tx_c") is not None),
        "sources": dict(sources),
        "literal_inheritance_0.07": {
            "logscore_gain": summarise([(d, g) for d, g, _, _ in literal]),
            "brier_gain": summarise([(d, g) for d, _, g, _ in literal]),
            "mean_abs_dz": float(np.mean([abs(z) for *_, z in literal])) if literal else None,
        },
    }
    arm_scores: dict[str, dict] = {}
    for arm, build in P0M_ARMS.items():
        X = np.array([[1.0] + build(c) for c in cases])
        y = np.array([c["r"] for c in cases])
        wf = WalkForward([c["date"] for c in cases], X, y)
        scores = {}
        coefs = {}
        for day in dates:
            fit = wf.fit_before(day)
            if fit is None:
                continue
            beta, _, n = fit
            coefs[day] = [round(float(v), 4) for v in beta]
            for c in by_date[day]:
                shift = float(np.dot(beta, [1.0] + build(c))) * c["sd_t"]
                scores[(c["station"], day)] = score_gaussian(c["mu_t"] + shift, c["sd_t"], c["truth_rep"], c["unit"])
        arm_scores[arm] = scores
        results.setdefault("last_coefficients", {})[arm] = coefs[max(coefs)] if coefs else None
    for new, base in P0M_COMPARISONS:
        keys = sorted(set(arm_scores[new]) & set(arm_scores[base]))
        for label, subset in (
            ("all", keys),
            ("with_tx", [k for k in keys if case_by_key[k]["taf"].get("tx_c") is not None]),
            ("no_tx", [k for k in keys if case_by_key[k]["taf"].get("tx_c") is None]),
            ("awc", [k for k in keys if case_by_key[k]["taf"]["source"] == "AWC"]),
            ("iem", [k for k in keys if case_by_key[k]["taf"]["source"] == "IEM"]),
        ):
            if not subset:
                continue
            results[f"{new} vs {base} [{label}] logscore"] = summarise(
                [(k[1], arm_scores[new][k][0] - arm_scores[base][k][0]) for k in subset]
            )
            results[f"{new} vs {base} [{label}] brier"] = summarise(
                [(k[1], arm_scores[base][k][1] - arm_scores[new][k][1]) for k in subset]
            )
    return results


# ----------------------------------------------------------------- P1
def p1_features(row: dict, day: dict, root: tuple[float, float]) -> dict | None:
    """The P1 decision's features; the model curve is moved to the root's level."""

    mu_root, _ = root
    suffix = "_g" if P0_SOURCE == "global" else ""
    base = model_max(row)
    rem = _num(row.get(f"rem_max_raw{suffix}"))
    now_gap = _num(row.get(f"now_gap_raw{suffix}"))
    if base is None or rem is None or now_gap is None:
        return None
    shift = mu_root - base
    f_rem = rem + shift
    innov_raw = _num(row.get(f"innov_raw{suffix}"))
    innov = None if innov_raw is None else innov_raw - shift
    taf_rem = row.get("taf_rem")
    return {
        "f_rem": f_rem,
        "temp": [
            _num(innov, 0.0),
            now_gap - shift,
            f_rem - row["running_c"],
        ],
        "state": [
            _num(row.get("state_cloud"), 0.4),
            _num(row.get("state_wet"), 0.0),
            _num(row.get("state_wind"), 3.5),
            _num(row.get("state_dd"), 8.0),
        ],
        # The METAR's revision: what the elapsed daylight actually was
        # against what the model said it would be (solar-weighted).
        "el": [
            _num(row.get("obs_cloud_el"), 0.0) - _num(row.get("mod_cloud_el"), 0.0),
            _num(row.get("obs_wet_el"), 0.0) - _num(row.get("mod_wet_el"), 0.0),
            _num(row.get("obs_wind_el"), 0.0) - _num(row.get("mod_wind_el"), 0.0),
        ],
        # The TAF's revision inherited for the rest of the heating window.
        "taf": [
            (taf_rem[0] - row["mod_cloud_rem"]) if taf_rem else 0.0,
            taf_rem[1] if taf_rem else 0.0,
        ],
        # P0's own term, free to be re-weighted at P1.
        "rad": [_num(day.get("kt_z"), 0.0)],
    }


P1_ARMS = {
    "T": ("temp",),
    "T+S": ("temp", "state"),
    "T+el": ("temp", "el"),
    "T+S+el": ("temp", "state", "el"),
    "T+S+eld": ("temp", "state", "eld"),
    "T+S+el+taf": ("temp", "state", "el", "taf"),
    "T+S+el+taf+rad": ("temp", "state", "el", "taf", "rad"),
}
P1_COMPARISONS = (
    ("T+el", "T"),
    ("T+S", "T"),
    ("T+S+el", "T+S"),
    ("T+S+eld", "T+S"),
    ("T+S+el+taf", "T+S+el"),
    ("T+S+el+taf+rad", "T+S+el+taf"),
    ("T+S+el+taf+rad", "T+S"),
)


def run_p1(p1_rows: list[dict], days: list[dict], roots: dict) -> dict:
    day_index = {(d["station"], d["date"]): d for d in days}
    # P1's root: P0-M's temperature line where a morning TAF stood, else P0.
    morning = {}
    for c in p0m_cases(days, roots, "morning"):
        morning[(c["station"], c["date"])] = c
    dates_all = sorted({c["date"] for c in morning.values()})
    by_date = defaultdict(list)
    for c in morning.values():
        by_date[c["date"]].append(c)
    for day in dates_all:
        cutoff = (date.fromisoformat(day) - timedelta(days=SETTLE_LAG_DAYS)).isoformat()
        line = fit_tx_line([c for c in morning.values() if c["date"] <= cutoff])
        for c in by_date[day]:
            c["mu_t"], c["sd_t"] = tx_posterior(c, line)
    results: dict = {}
    for hour in sorted({r["hour"] for r in p1_rows}):
        rows = []
        for r in p1_rows:
            if r["hour"] != hour:
                continue
            key = (r["station"], r["date"])
            day = day_index.get(key)
            root_row = roots.get(key)
            if day is None or root_row is None:
                continue
            m = morning.get(key)
            root = (m["mu_t"], m["sd_t"]) if m is not None and "mu_t" in m else (root_row["mu"], root_row["sigma"])
            features = p1_features(r, day, root)
            if features is None:
                continue
            rows.append((r, features))
        if not rows:
            continue
        # The elapsed surprise with each station's own METAR-versus-model
        # cloud / wet / wind offset at this hour removed.
        debiased = station_debiased(
            [{"station": r["station"], "date": r["date"], "el": f["el"]} for r, f in rows],
            lambda item: item["el"],
        )
        for (_, f), values in zip(rows, debiased):
            f["eld"] = values
        dates = [r["date"] for r, _ in rows]
        index_by_day: dict[str, list[int]] = defaultdict(list)
        for index, day in enumerate(dates):
            index_by_day[day].append(index)
        y = np.array([r["rest_c"] - f["f_rem"] for r, f in rows])
        arm_scores = {}
        for arm, blocks in P1_ARMS.items():
            X = np.array([[1.0] + sum((f[b] for b in blocks), []) for _, f in rows])
            wf = WalkForward(dates, X, y)
            scores = {}
            last_beta = None
            for day in sorted(index_by_day):
                fit = wf.fit_before(day)
                if fit is None:
                    continue
                beta, sigma, _ = fit
                last_beta = beta
                for index in index_by_day[day]:
                    r, f = rows[index]
                    mu = f["f_rem"] + float(np.dot(beta, X[index]))
                    try:
                        scores[(r["station"], day)] = score_truncated(
                            mu, sigma, r["running_rep"], r["truth_rep"], r["unit"]
                        )
                    except ValueError:
                        continue
            arm_scores[arm] = scores
            if last_beta is not None:
                results.setdefault(f"h{hour:02d} last_coefficients", {})[arm] = [
                    round(float(v), 4) for v in last_beta
                ]
        for new, base in P1_COMPARISONS:
            keys = sorted(set(arm_scores[new]) & set(arm_scores[base]))
            results[f"h{hour:02d} {new} vs {base} logscore"] = summarise(
                [(k[1], arm_scores[new][k][0] - arm_scores[base][k][0]) for k in keys]
            )
            results[f"h{hour:02d} {new} vs {base} brier"] = summarise(
                [(k[1], arm_scores[base][k][1] - arm_scores[new][k][1]) for k in keys]
            )
        results[f"h{hour:02d} rows"] = len(rows)
    return results


def load_jsonl(path: Path) -> list[dict]:
    with path.open() as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--p0-source", choices=("global", "best_match", "production"), default="global",
        help="which day-ahead maximum stands in for P0's centre "
        "(production: the served roots build_cases_ssot.py wrote)",
    )
    args = parser.parse_args()
    global P0_SOURCE
    P0_SOURCE = args.p0_source
    derived = Path(args.cache_dir) / "derived"
    days = load_jsonl(derived / "days.jsonl")
    p1_rows = load_jsonl(derived / "p1.jsonl")
    roots = production_roots(days) if P0_SOURCE == "production" else p0_roots(days)
    # The day's heating window as the METARs reported it, for the P0-M
    # ceiling arm only (hindsight; never a served input).
    oracle = {
        (r["station"], r["date"]): (_num(r.get("obs_cloud_el")), _num(r.get("obs_wet_el")))
        for r in p1_rows
        if r["hour"] == 16 and _num(r.get("obs_cloud_el")) is not None
        and _num(r.get("obs_wet_el")) is not None
    }
    out = {
        "inputs": {
            "station_days": len(days), "p1_rows": len(p1_rows),
            "p0_source": P0_SOURCE,
            "first_date": min(d["date"] for d in days),
            "last_date": max(d["date"] for d in days),
        },
        "p0_radiation_term": run_p0(days, roots),
        "p0m_morning": run_p0m(days, roots, "morning", oracle),
        "p0m_eve": run_p0m(days, roots, "eve", oracle),
        "p1": run_p1(p1_rows, days, roots),
    }
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=True))
    print(json.dumps(out, indent=1, sort_keys=True)[:6000])


if __name__ == "__main__":
    main()
