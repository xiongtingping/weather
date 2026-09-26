"""DB-resident L4 posterior parameters: the fit, the walk-forward verdict,
the journal, the served rows, the forward skill score.  The basket's one
belief since charter 230 (user ruling 2026-09-26 「按l4做」); the
first-principles baseline is
docs/architecture/EDGE_SYSTEM_FIRST_PRINCIPLES_BASELINE_2026-09-17.md.

Charter 230 changed three things below: the fit is the multiclass log loss of
the renormalised ladder (``_fit_multiclass``, charter 211's training rule;
the per-bucket logistic ``_fit_logistic`` stays as the comparison arm the
research reads), every row carries ``walk_forward_verdict``'s reading of
both scores against the market and serves only when admitted, and the
population is the rolling window rather than the serving-arithmetic anchor
(``POPULATION_ANCHORED``).

What is fitted (§4.4, appendix A.2).  Per decision and per bucket of the
market's ladder::

    logit b = logit m + w * (logit p - logit m) + alpha + gamma * logit m

by weighted logistic regression on the truth indicator, offset ``logit m``,
features ``(1, logit m, logit p - logit m)``, coefficients ``(alpha, gamma,
w)`` with a Gaussian prior centred on 0 -- "the market is right" (E3).  The
ladder is renormalised afterwards, and the score the fit is judged on is
taken on the renormalised vector: ``S = mean[log b(truth) - log m(truth)]``
per decision, the expected growth advantage over betting the market
(appendix A.1), with a by-event-day bootstrap interval (E17).

Population (E4).  Every decision with a complete book and a knowable label,
every bucket of its ladder -- the valuation lane's ``population`` unchanged,
which is the only place in the repository that already assembles "served
vector x same-snapshot market vector x truth bucket" without conditioning on
what the enumeration offered or the gate admitted.  The stage's whole row is
fitted first; one row per lead band (the kernel's ``BETA_LEAD_BANDS``, hours
from the decision to settlement) is fitted beside it and shrunk to the stage
by a day pseudo-count, so a band with two days of evidence reads as the
stage and a band with forty reads as itself.

Served rows.  ``(alpha, gamma, w)`` point values and the joint draws; below
the hurdles a row is ``accumulating`` and the runtime reads ``b = m``.  The
lane is shadow: the runtime records the belief, its lower-bound edge and the
verdict it would give, and gates nothing.

Forward skill (E5, E15).  ``forward_skill`` re-reads the journal: each
decision is scored on the fit that was active when it was taken (the latest
journal entry before its decision time), which is the only out-of-sample
number promotion may be argued on.  The in-sample ``S`` on the fit window is
reported next to it and labelled as such.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .basket_kernel.delta_v2 import BETA_LEAD_BANDS, beta_lead_band
from .basket_kernel.posterior_v1 import (
    EPS,
    LCB_QUANTILE,
    PARAM_NAMES,
    POSTERIOR_RULE,
    POSTERIOR_TABLE_KEY,
    logit,
    posterior_vector_v1,
)
from .basket_kernel.valuation_v1 import MARKET_REFERENCE_RULE
from .contracts import content_sha256
from .settlement_time import hours_to_settlement_if_known
from .valuation_store import STAGE_SCOPES, population as _valuation_population

SCHEMA = "posterior_table_v1"
WINDOW_DAYS = 28
#: Charter 230: the lane's population is the rolling window, NOT cut at the
#: stage's serving-arithmetic anchor.  The anchor moves with every served
#: version bump (P0 went v14 -> v19 in two days), and a lane that restarts its
#: evidence at each bump never holds a verdict: on 2026-09-26 the anchored P0
#: population was 11 days, leaving its walk-forward one test day.  The
#: parameters are "how far to trust the model's disagreement with the book"
#: and the book's own recalibration, measured over recent history; a version
#: change shows up in the walk-forward verdict, which is scored day by day
#: on the vectors actually served then.  ``anchored=True`` stays a research
#: reading.
POPULATION_ANCHORED = False
MIN_STATION_DAYS = 100
MIN_DAYS = 8
BOOTSTRAP_DRAWS = 200
BOOTSTRAP_SEED = 20260917
#: Prior standard deviations, centred on 0: alpha weak, gamma and w unit.
PRIOR_SD: tuple[float, float, float] = (2.0, 1.0, 1.0)
#: A band's parameters are shrunk to the stage's by this many pseudo-days.
BAND_SHRINK_DAYS = 8.0
WHOLE_STAGE = ""
LEAD_BAND_LABELS: tuple[str, ...] = tuple(label for _lo, _hi, label in BETA_LEAD_BANDS)
DRAW_DECIMALS = 5
NEWTON_ITERATIONS = 40
NEWTON_TOLERANCE = 1e-9

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS calibration_posterior_table (
  posterior_sha256 TEXT PRIMARY KEY,
  stage_scope TEXT NOT NULL,
  lead_band TEXT NOT NULL DEFAULT '',
  rule TEXT NOT NULL,
  market_reference_rule TEXT NOT NULL,
  window_days INTEGER NOT NULL,
  as_of_event_date TEXT NOT NULL,
  fit_cutoff_utc TEXT NOT NULL,
  alpha REAL,
  gamma REAL,
  w REAL,
  w_lcb REAL,
  w_ucb REAL,
  decision_count INTEGER NOT NULL,
  station_day_count INTEGER NOT NULL,
  day_count INTEGER NOT NULL,
  accumulating INTEGER NOT NULL,
  evidence_json TEXT NOT NULL,
  fitted_at_utc TEXT NOT NULL,
  activated INTEGER NOT NULL DEFAULT 0,
  activated_at_utc TEXT
);
CREATE INDEX IF NOT EXISTS calibration_posterior_table_active_idx
  ON calibration_posterior_table(stage_scope, lead_band, activated, activated_at_utc);
CREATE TABLE IF NOT EXISTS calibration_posterior_fit_journal (
  fit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  posterior_sha256 TEXT NOT NULL,
  stage_scope TEXT NOT NULL,
  lead_band TEXT NOT NULL DEFAULT '',
  fitted_at_utc TEXT NOT NULL,
  outcome TEXT NOT NULL,
  evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calibration_posterior_fit_journal_idx
  ON calibration_posterior_fit_journal(stage_scope, lead_band, fitted_at_utc);
CREATE TABLE IF NOT EXISTS calibration_posterior_hypothesis (
  hypothesis_id TEXT PRIMARY KEY,
  registered_at_utc TEXT NOT NULL,
  mechanism TEXT NOT NULL,
  context_json TEXT NOT NULL,
  expected_sign TEXT NOT NULL,
  metric TEXT NOT NULL,
  min_event_days INTEGER NOT NULL,
  status TEXT NOT NULL
);
"""

#: E11: the contexts this lane may condition on, registered before they are
#: read.  ``refit_posterior.py --commit`` writes them once; a band the fit
#: reads without a row here is a bug, not a finding.
HYPOTHESES_V0: tuple[dict[str, Any], ...] = tuple(
    {
        "hypothesis_id": f"lead_band:{label}",
        "mechanism": (
            "The market's error shrinks as observations arrive through the "
            "day and the weather model reads them earlier or later than the "
            "book does, so the share of the disagreement that realises "
            "differs by hours to settlement."
        ),
        "context": {"axis": "hours_to_cutoff", "band": label},
        "expected_sign": "w > 0 near settlement if the model reads the day's observations first",
        "metric": "forward_skill_lcb",
        "min_event_days": MIN_DAYS,
    }
    for label in LEAD_BAND_LABELS
) + (
    {
        "hypothesis_id": "market_recalibration",
        "mechanism": (
            "The crowd is systematically over- or under-confident on its own "
            "ladder (favourite-longshot bias); a slope on logit m other than "
            "one and a non-zero intercept capture it without reading p."
        ),
        "context": {"axis": "logit_m", "terms": ["alpha", "gamma"]},
        "expected_sign": "gamma < 0 if tails are overpriced",
        "metric": "forward_skill_lcb",
        "min_event_days": MIN_DAYS,
    },
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_TABLE_DDL)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Population
# --------------------------------------------------------------------------

def population(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS,
    cutoff_utc: str | None = None,
    anchored: bool = POPULATION_ANCHORED,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The valuation lane's population with each decision's lead band."""

    rows, meta = _valuation_population(
        conn, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
        cutoff_utc=cutoff_utc, anchored=anchored,
    )
    without_clock = 0
    for row in rows:
        hours = hours_to_settlement_if_known(
            str(row.get("date") or ""),
            as_of_utc=row.get("at"),
            settlement_timezone=row.get("settlement_timezone"),
        )
        row["hours_to_cutoff"] = hours
        row["lead_band"] = beta_lead_band(hours) if hours is not None else None
        if row["lead_band"] is None:
            without_clock += 1
    meta = dict(meta)
    meta["rows_without_clock"] = without_clock
    return rows, meta


# --------------------------------------------------------------------------
# The fit
# --------------------------------------------------------------------------

def _design(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Bucket-level arrays for the logistic fit, grouped by decision."""

    import numpy as np  # noqa: PLC0415

    x1: list[float] = []
    x2: list[float] = []
    y: list[float] = []
    wt: list[float] = []
    day_of_row: list[int] = []
    starts: list[int] = []
    m_label: list[float] = []
    day_of_dec: list[int] = []
    days = sorted({str(r["date"]) for r in rows})
    day_index = {d: i for i, d in enumerate(days)}
    for row in rows:
        starts.append(len(x1))
        label = str(row["label"])
        m = row["m"]
        p = row["p"]
        d = day_index[str(row["date"])]
        day_of_dec.append(d)
        m_label.append(float(m[label]))
        for bucket, mi in m.items():
            lm = logit(float(mi))
            lp = logit(float(p.get(bucket, 0.0) or 0.0))
            x1.append(lm)
            x2.append(lp - lm)
            y.append(1.0 if str(bucket) == label else 0.0)
            wt.append(float(row.get("weight", 1.0)))
            day_of_row.append(d)
    return {
        "X": np.column_stack([np.ones(len(x1)), np.asarray(x1), np.asarray(x2)])
        if x1 else np.zeros((0, 3)),
        "offset": np.asarray(x1), "y": np.asarray(y), "w": np.asarray(wt),
        "day_of_row": np.asarray(day_of_row, dtype=int),
        "starts": np.asarray(starts, dtype=int),
        "m_label": np.asarray(m_label), "day_of_dec": np.asarray(day_of_dec, dtype=int),
        "days": days, "n_days": len(days),
    }


def _fit_logistic(
    design: Mapping[str, Any], *, row_weight: Any, prior_mean: Sequence[float],
    prior_sd: Sequence[float] = PRIOR_SD, start: Sequence[float] | None = None,
) -> Any:
    """Penalised Newton–Raphson; returns the coefficient vector."""

    import numpy as np  # noqa: PLC0415

    X = design["X"]
    off = design["offset"]
    y = design["y"]
    w = design["w"] * row_weight
    mu0 = np.asarray(prior_mean, dtype=float)
    prec = 1.0 / (np.asarray(prior_sd, dtype=float) ** 2)
    theta = np.asarray(start if start is not None else mu0, dtype=float).copy()
    for _ in range(NEWTON_ITERATIONS):
        s = off + X @ theta
        pr = 1.0 / (1.0 + np.exp(-s))
        grad = X.T @ (w * (pr - y)) + prec * (theta - mu0)
        hess = (X * (w * pr * (1.0 - pr))[:, None]).T @ X + np.diag(prec)
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        theta = theta - step
        if float(np.max(np.abs(step))) < NEWTON_TOLERANCE:
            break
    return theta


def _multiclass_terms(
    design: Mapping[str, Any], theta: Any, *, dec_weight: Any,
    prior_mean: Any, prec: Any, want_hessian: bool = True,
) -> tuple[float, Any, Any]:
    """Penalised multiclass log loss of the renormalised ladder, its gradient
    and Hessian.

    ``b_i = expit(s_i) / sum_j expit(s_j)`` per decision, ``s = offset + X theta``;
    the loss is ``-sum_d w_d log b_{d,label}`` plus the Gaussian prior.  With
    ``T_d = sum_j expit(s_j)`` and ``a = expit(s)(1 - expit(s))``::

        grad = -sum_d w_d [(1 - sig_y) x_y - (sum_j a_j x_j) / T_d]
        hess =  sum_d w_d [a_y x_y x_y' + sum_j a_j (1 - 2 sig_j) x_j x_j' / T_d
                           - A_d A_d' / T_d^2],   A_d = sum_j a_j x_j
    """

    import numpy as np  # noqa: PLC0415

    X = design["X"]
    starts = design["starts"]
    y = design["y"]
    # Clipped so a far-out parameter proposal cannot overflow the sigmoid or
    # empty a decision's total; the prior keeps the optimum well inside.
    s = np.clip(design["offset"] + X @ theta, -60.0, 60.0)
    sig = 1.0 / (1.0 + np.exp(-s))
    a = sig * (1.0 - sig)
    total = np.maximum(np.add.reduceat(sig, starts), 1e-300)
    lengths = np.diff(np.append(starts, len(sig)))
    label_rows = np.flatnonzero(y > 0.5)
    sig_y = np.maximum(sig[label_rows], 1e-300)
    loss = float(-np.sum(dec_weight * (np.log(sig_y) - np.log(total))))
    diff = theta - prior_mean
    loss += 0.5 * float(np.sum(prec * diff * diff))
    xa = X * a[:, None]
    big_a = np.add.reduceat(xa, starts, axis=0)
    grad = -(X[label_rows] * (dec_weight * (1.0 - sig[label_rows]))[:, None]).sum(axis=0)
    grad += (big_a * (dec_weight / total)[:, None]).sum(axis=0)
    grad += prec * diff
    if not want_hessian:
        return loss, grad, None
    w_row = np.repeat(dec_weight / total, lengths)
    hess = (X[label_rows] * (dec_weight * a[label_rows])[:, None]).T @ X[label_rows]
    hess += (X * (w_row * a * (1.0 - 2.0 * sig))[:, None]).T @ X
    hess -= (big_a * (dec_weight / (total * total))[:, None]).T @ big_a
    hess += np.diag(prec)
    return loss, grad, hess


def _fit_multiclass(
    design: Mapping[str, Any], *, row_weight: Any, prior_mean: Sequence[float],
    prior_sd: Sequence[float] = PRIOR_SD, start: Sequence[float] | None = None,
) -> Any:
    """Charter 211's training rule on L4: the multiclass log loss of the
    RENORMALISED ladder, not a logistic regression per bucket.

    ``_fit_logistic`` fits every bucket as its own yes/no and renormalises
    afterwards, so the coefficients it returns maximise a likelihood nobody
    serves; the belief the gate reads is the renormalised vector, and this
    maximises the probability that vector puts on the bucket that settled.
    Damped Newton (Levenberg) with a backtracking line search: the loss of a
    logit-then-renormalise map is not convex everywhere, the prior makes it
    well posed.  ``row_weight`` is per row (constant within a decision) or a
    scalar, as ``_fit_logistic`` takes it.
    """

    import numpy as np  # noqa: PLC0415

    X = design["X"]
    if X.shape[0] == 0:
        return np.asarray(prior_mean, dtype=float).copy()
    starts = design["starts"]
    row_w = design["w"] * (np.ones(X.shape[0]) * row_weight)
    dec_weight = row_w[starts]
    mu0 = np.asarray(prior_mean, dtype=float)
    prec = 1.0 / (np.asarray(prior_sd, dtype=float) ** 2)
    theta = np.asarray(start if start is not None else mu0, dtype=float).copy()
    loss, grad, hess = _multiclass_terms(
        design, theta, dec_weight=dec_weight, prior_mean=mu0, prec=prec,
    )
    damping = 0.0
    for _ in range(NEWTON_ITERATIONS):
        step = None
        for _attempt in range(12):
            try:
                candidate_hess = hess + damping * np.eye(len(theta))
                np.linalg.cholesky(candidate_hess)
                step = np.linalg.solve(candidate_hess, grad)
                break
            except np.linalg.LinAlgError:
                damping = max(1e-6, damping * 10.0 if damping else 1e-4 * float(np.max(np.abs(np.diag(hess)))) + 1e-6)
        if step is None:
            break
        scale = 1.0
        improved = False
        for _ls in range(30):
            trial = theta - scale * step
            trial_loss, _g, _h = _multiclass_terms(
                design, trial, dec_weight=dec_weight, prior_mean=mu0, prec=prec,
                want_hessian=False,
            )
            if math.isfinite(trial_loss) and trial_loss <= loss + 1e-12:
                improved = True
                break
            scale *= 0.5
        if not improved:
            break
        moved = float(np.max(np.abs(trial - theta)))
        theta = trial
        loss, grad, hess = _multiclass_terms(
            design, theta, dec_weight=dec_weight, prior_mean=mu0, prec=prec,
        )
        damping = damping * 0.1 if damping > 1e-9 else 0.0
        if moved < NEWTON_TOLERANCE:
            break
    return theta


def _ladder_scores(design: Mapping[str, Any], theta: Any) -> dict[str, Any]:
    """Per-decision ``log b(truth) - log m(truth)`` on the renormalised
    ladder, and the bucket-level IC ingredients."""

    import numpy as np  # noqa: PLC0415

    X = design["X"]
    if X.shape[0] == 0:
        return {"s_dec": np.zeros(0), "ic": None}
    s = design["offset"] + X @ np.asarray(theta, dtype=float)
    b_raw = 1.0 / (1.0 + np.exp(-s))
    starts = design["starts"]
    totals = np.add.reduceat(b_raw, starts)
    lengths = np.diff(np.append(starts, len(b_raw)))
    b = b_raw / np.repeat(totals, lengths)
    y = design["y"]
    b_label = np.add.reduceat(b * y, starts)
    m_label = design["m_label"]
    s_dec = np.log(np.maximum(b_label, EPS)) - np.log(np.maximum(m_label, EPS))
    m_row = 1.0 / (1.0 + np.exp(-design["offset"]))
    a = b - m_row
    c = y - m_row
    wt = design["w"]
    va = float(np.sum(wt * a * a))
    vc = float(np.sum(wt * c * c))
    ic = float(np.sum(wt * a * c) / math.sqrt(va * vc)) if va > 0 and vc > 0 else None
    return {"s_dec": s_dec, "ic": ic}


def _by_day_ci(values: Any, day_of: Any, n_days: int, *, rng: random.Random, draws: int) -> tuple[float | None, float | None]:
    import numpy as np  # noqa: PLC0415

    if n_days == 0 or len(values) == 0:
        return None, None
    sums = np.bincount(day_of, weights=values, minlength=n_days)
    counts = np.bincount(day_of, minlength=n_days).astype(float)
    out: list[float] = []
    for _ in range(draws):
        pick = np.asarray([rng.randrange(n_days) for _ in range(n_days)])
        den = float(np.sum(counts[pick]))
        out.append(float(np.sum(sums[pick]) / den) if den > 0 else 0.0)
    out.sort()
    return out[min(len(out) - 1, int(LCB_QUANTILE * len(out)))], out[max(0, int((1.0 - LCB_QUANTILE) * len(out)) - 1)]


def fit_posterior(
    conn: sqlite3.Connection | None,
    *,
    stage_scope: str,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS,
    cutoff_utc: str | None = None,
    draws: int = BOOTSTRAP_DRAWS,
    anchored: bool = POPULATION_ANCHORED,
    lead_band: str | None = None,
    rows: Sequence[Mapping[str, Any]] | None = None,
    meta: Mapping[str, Any] | None = None,
    parent: Mapping[str, Any] | None = None,
    verdict: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One row's fit: the stage's whole population, or one lead band of it
    shrunk to ``parent`` (the stage's fit) by ``BAND_SHRINK_DAYS``.

    Charter 230: fitted by ``_fit_multiclass`` (charter 211's training rule),
    and carrying ``verdict`` -- the row's own walk-forward against the market,
    from ``walk_forward_verdict`` -- in the table, so a row whose verdict
    moved is a new row."""

    import numpy as np  # noqa: PLC0415

    if lead_band is not None and lead_band not in LEAD_BAND_LABELS:
        raise ValueError(f"posterior_lead_band_invalid:{lead_band}")
    if rows is None:
        if conn is None:
            raise ValueError("posterior_fit_needs_rows_or_connection")
        rows, meta = population(
            conn, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
            cutoff_utc=cutoff_utc, anchored=anchored,
        )
    meta = dict(meta or {})
    band = WHOLE_STAGE if lead_band is None else str(lead_band)
    if lead_band is not None:
        rows = [r for r in rows if r.get("lead_band") == lead_band]
    station_days = len({(r["station"], r["date"]) for r in rows})
    days = sorted({str(r["date"]) for r in rows})
    accumulating = station_days < MIN_STATION_DAYS or len(days) < MIN_DAYS
    evidence: dict[str, Any] = {
        "schema": SCHEMA, "rule": POSTERIOR_RULE,
        "market_reference_rule": MARKET_REFERENCE_RULE,
        "stage_scope": stage_scope, "lead_band": band,
        "window_days": int(window_days),
        "decision_count": len(rows), "station_day_count": station_days,
        "day_count": len(days), "min_station_days": MIN_STATION_DAYS,
        "min_days": MIN_DAYS, "prior_sd": list(PRIOR_SD),
        "band_shrink_days": BAND_SHRINK_DAYS,
        **{k: v for k, v in meta.items() if k != "market_incomplete_reasons"},
        "market_incomplete_reasons": meta.get("market_incomplete_reasons"),
    }
    point: dict[str, float | None] = {name: None for name in PARAM_NAMES}
    ci: dict[str, tuple[float | None, float | None]] = {name: (None, None) for name in PARAM_NAMES}
    param_draws: list[list[float]] = []
    skill: dict[str, Any] = {
        "s_in_sample": None, "s_in_sample_ci": [None, None],
        "ic_in_sample": None, "w_draw_positive_share": None,
    }
    if rows and not accumulating:
        design = _design(rows)
        ones = np.ones(design["X"].shape[0])
        theta = _fit_multiclass(design, row_weight=ones, prior_mean=(0.0, 0.0, 0.0))
        n_days = design["n_days"]
        shrink = None
        if parent is not None and lead_band is not None:
            parent_point = np.asarray(
                [float(parent.get(k) or 0.0) for k in PARAM_NAMES], dtype=float
            )
            shrink = (float(n_days), parent_point)
            theta = (n_days * theta + BAND_SHRINK_DAYS * parent_point) / (n_days + BAND_SHRINK_DAYS)
        rng = random.Random(BOOTSTRAP_SEED + (LEAD_BAND_LABELS.index(lead_band) + 1 if lead_band else 0))
        day_of_row = design["day_of_row"]
        for _ in range(int(draws)):
            counts = np.bincount(
                np.asarray([rng.randrange(n_days) for _ in range(n_days)]),
                minlength=n_days,
            ).astype(float)
            weight = counts[day_of_row]
            draw = _fit_multiclass(
                design, row_weight=weight, prior_mean=(0.0, 0.0, 0.0), start=theta,
            )
            if shrink is not None:
                draw = (shrink[0] * draw + BAND_SHRINK_DAYS * shrink[1]) / (shrink[0] + BAND_SHRINK_DAYS)
            param_draws.append([round(float(v), DRAW_DECIMALS) for v in draw])
        for i, name in enumerate(PARAM_NAMES):
            point[name] = float(theta[i])
            ordered = sorted(d[i] for d in param_draws)
            ci[name] = (
                ordered[min(len(ordered) - 1, int(LCB_QUANTILE * len(ordered)))],
                ordered[max(0, int((1.0 - LCB_QUANTILE) * len(ordered)) - 1)],
            )
        scored = _ladder_scores(design, theta)
        s_dec = scored["s_dec"]
        lo, hi = _by_day_ci(
            s_dec, design["day_of_dec"], n_days, rng=random.Random(BOOTSTRAP_SEED), draws=int(draws),
        )
        skill = {
            "s_in_sample": float(np.mean(s_dec)) if len(s_dec) else None,
            "s_in_sample_ci": [lo, hi],
            "ic_in_sample": scored["ic"],
            "w_draw_positive_share": (
                sum(1 for d in param_draws if d[2] > 0.0) / len(param_draws)
                if param_draws else None
            ),
        }
    verdict_record = dict(verdict) if verdict is not None else _no_verdict("not_evaluated")
    if accumulating:
        verdict_record = dict(verdict_record, admitted=False)
    evidence.update({
        "accumulating": bool(accumulating),
        "verdict": verdict_record,
        "point": point,
        "ci": {name: list(ci[name]) for name in PARAM_NAMES},
        "draws": [] if accumulating else param_draws,
        "skill": skill,
    })
    table = {
        "schema": SCHEMA, "rule": POSTERIOR_RULE,
        "market_reference_rule": MARKET_REFERENCE_RULE,
        "stage_scope": stage_scope, "lead_band": band,
        "window_days": int(window_days),
        "as_of_event_date": meta.get("as_of_event_date"),
        "fit_cutoff_utc": meta.get("cutoff_utc"),
        "point": point, "w_lcb": ci["w"][0], "w_ucb": ci["w"][1],
        "decision_count": len(rows), "station_day_count": station_days,
        "day_count": len(days), "accumulating": bool(accumulating),
        "draws": evidence["draws"],
        "verdict": verdict_record,
    }
    return {
        "stage_scope": stage_scope, "lead_band": band, "table": table,
        "evidence": evidence, "content_sha256": content_sha256(table),
        "accumulating": bool(accumulating), "decision_count": len(rows),
        "station_day_count": station_days, "day_count": len(days),
    }


def fit_stage_and_bands(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS,
    cutoff_utc: str | None = None,
    draws: int = BOOTSTRAP_DRAWS,
    anchored: bool = POPULATION_ANCHORED,
) -> list[dict[str, Any]]:
    """The stage's whole row first, then one row per lead band shrunk to it;
    all cut from one population."""

    rows, meta = population(
        conn, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
        cutoff_utc=cutoff_utc, anchored=anchored,
    )
    verdicts = {} if meta.get("error") else walk_forward_verdict(rows)
    whole = fit_posterior(
        None, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
        cutoff_utc=cutoff_utc, draws=draws, anchored=anchored, rows=rows, meta=meta,
        verdict=verdicts.get(WHOLE_STAGE),
    )
    fits = [whole]
    if meta.get("error"):
        return fits
    parent = None if whole["accumulating"] else whole["evidence"]["point"]
    for label in LEAD_BAND_LABELS:
        fits.append(fit_posterior(
            None, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
            cutoff_utc=cutoff_utc, draws=draws, anchored=anchored,
            lead_band=label, rows=rows, meta=meta, parent=parent,
            verdict=verdicts.get(label),
        ))
    return fits


# --------------------------------------------------------------------------
# Charter 230: the walk-forward verdict a row must hold to serve
# --------------------------------------------------------------------------

#: Test days a row's verdict needs, and training days before a test day.
VERDICT_MIN_TEST_DAYS = MIN_DAYS
VERDICT_DRAWS = 1000
VERDICT_SEED = 20260926
VERDICT_RULE = "walk_forward_brier_and_logscore_beat_market_v1"


def _no_verdict(reason: str) -> dict[str, Any]:
    return {
        "rule": VERDICT_RULE, "admitted": False, "reason": reason,
        "test_days": 0, "station_days": 0, "decisions": 0,
        "d_logscore": [None, None, None], "d_brier": [None, None, None],
    }


def _row_theta(
    rows: Sequence[Mapping[str, Any]], *, parent: Any | None = None,
) -> Any | None:
    """The multiclass point a row would serve on ``rows``, or None while the
    row is accumulating -- the same hurdles and band shrink as ``fit_posterior``."""

    import numpy as np  # noqa: PLC0415

    station_days = len({(r["station"], r["date"]) for r in rows})
    days = {str(r["date"]) for r in rows}
    if station_days < MIN_STATION_DAYS or len(days) < MIN_DAYS:
        return None
    design = _design(rows)
    theta = _fit_multiclass(
        design, row_weight=np.ones(design["X"].shape[0]), prior_mean=(0.0, 0.0, 0.0),
    )
    if parent is not None:
        n = float(design["n_days"])
        theta = (n * theta + BAND_SHRINK_DAYS * np.asarray(parent, dtype=float)) / (
            n + BAND_SHRINK_DAYS
        )
    return theta


def walk_forward_verdict(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Charter 230: per served row, does the lane beat the market out of sample?

    Every event day in the population is a test day once its training set --
    decisions whose label was knowable before that day's first decision --
    fits.  On it the stage row and the five band rows are fitted exactly as
    the nightly fit fits them, and each of the day's decisions reads the row
    the runtime would read (its band's when not accumulating, else the
    stage's; a decision with neither reads ``b = m`` and is nobody's
    evidence).  The row is admitted when, over at least
    ``VERDICT_MIN_TEST_DAYS`` test days, the one-sided 95% upper bound of the
    mean change against the market is below zero on BOTH of charter 211's
    scores -- bucket LogScore and bucket Brier -- station-days weighted
    equally, by-event-day bootstrap.  A row without that serves ``b = m``.
    """

    import numpy as np  # noqa: PLC0415

    out: dict[str, dict[str, Any]] = {
        band: _no_verdict("no_test_days") for band in (WHOLE_STAGE, *LEAD_BAND_LABELS)
    }
    if not rows:
        return out

    def _instant(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else None

    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(str(row["date"]), []).append(row)
    known = [(_instant(r.get("label_known_at")), r) for r in rows]
    # (row label, day, station) -> list of (d_logscore, d_brier)
    scored: dict[str, dict[tuple[str, str], list[tuple[float, float]]]] = {}
    for day in sorted(by_day):
        first = min(
            (t for t in (_instant(r.get("at")) for r in by_day[day]) if t is not None),
            default=None,
        )
        if first is None:
            continue
        train = [
            r for t, r in known
            if t is not None and t <= first and str(r["date"]) < day
        ]
        stage_theta = _row_theta(train)
        if stage_theta is None:
            continue
        band_theta: dict[str, Any] = {}
        for label in LEAD_BAND_LABELS:
            band_theta[label] = _row_theta(
                [r for r in train if r.get("lead_band") == label], parent=stage_theta,
            )
        for r in by_day[day]:
            label = r.get("lead_band")
            if label is not None and band_theta.get(label) is not None:
                key, theta = str(label), band_theta[label]
            else:
                key, theta = WHOLE_STAGE, stage_theta
            params = {name: float(theta[i]) for i, name in enumerate(PARAM_NAMES)}
            vector = posterior_vector_v1(r["p"], r["m"], params)
            if vector is None:
                continue
            truth = str(r["label"])
            m = r["m"]
            b_y = max(float(vector.get(truth, 0.0)), EPS)
            m_y = max(float(m[truth]), EPS)
            d_ls = -math.log(b_y) + math.log(m_y)
            d_br = sum(
                (float(vector.get(k, 0.0)) - (1.0 if str(k) == truth else 0.0)) ** 2
                - (float(v) - (1.0 if str(k) == truth else 0.0)) ** 2
                for k, v in m.items()
            )
            scored.setdefault(key, {}).setdefault(
                (day, str(r["station"])), []
            ).append((d_ls, d_br))
    for key, cells in scored.items():
        days = sorted({day for day, _station in cells})
        day_index = {d: i for i, d in enumerate(days)}
        num = np.zeros((len(days), 2))
        den = np.zeros(len(days))
        for (day, _station), values in cells.items():
            arr = np.asarray(values, dtype=float)
            num[day_index[day]] += arr.mean(axis=0)
            den[day_index[day]] += 1.0
        mean = num.sum(axis=0) / den.sum()
        rng = np.random.default_rng(VERDICT_SEED)
        picks = rng.integers(0, len(days), size=(VERDICT_DRAWS, len(days)))
        boot = num[picks].sum(axis=1) / den[picks].sum(axis=1)[:, None]
        lo = np.quantile(boot, LCB_QUANTILE, axis=0)
        hi = np.quantile(boot, 1.0 - LCB_QUANTILE, axis=0)
        enough = len(days) >= VERDICT_MIN_TEST_DAYS
        beats = bool(hi[0] < 0.0 and hi[1] < 0.0)
        out[key] = {
            "rule": VERDICT_RULE,
            "admitted": bool(enough and beats),
            "reason": (
                "admitted" if enough and beats
                else "too_few_test_days" if not enough
                else "does_not_beat_market"
            ),
            "test_days": len(days),
            "station_days": len(cells),
            "decisions": int(sum(len(v) for v in cells.values())),
            "d_logscore": [float(mean[0]), float(lo[0]), float(hi[0])],
            "d_brier": [float(mean[1]), float(lo[1]), float(hi[1])],
        }
    return out


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def persist_and_activate(
    conn: sqlite3.Connection, fit: Mapping[str, Any], *, now: str,
) -> dict[str, Any]:
    ensure_schema(conn)
    table = fit["table"]
    sha = str(fit["content_sha256"])
    scope = str(fit["stage_scope"])
    band = str(fit.get("lead_band") or WHOLE_STAGE)
    point = table["point"]
    conn.execute(
        "UPDATE calibration_posterior_table SET activated=0"
        " WHERE stage_scope=? AND lead_band=? AND activated=1 AND posterior_sha256<>?",
        (scope, band, sha),
    )
    conn.execute(
        "INSERT OR IGNORE INTO calibration_posterior_table("
        " posterior_sha256, stage_scope, lead_band, rule, market_reference_rule,"
        " window_days, as_of_event_date, fit_cutoff_utc, alpha, gamma, w, w_lcb,"
        " w_ucb, decision_count, station_day_count, day_count, accumulating,"
        " evidence_json, fitted_at_utc, activated, activated_at_utc"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (
            sha, scope, band, POSTERIOR_RULE, MARKET_REFERENCE_RULE,
            int(table["window_days"]), str(table.get("as_of_event_date") or ""),
            str(table.get("fit_cutoff_utc") or ""),
            point.get("alpha"), point.get("gamma"), point.get("w"),
            table.get("w_lcb"), table.get("w_ucb"),
            int(table["decision_count"]), int(table["station_day_count"]),
            int(table["day_count"]), 1 if table["accumulating"] else 0,
            json.dumps(fit["evidence"], ensure_ascii=False, sort_keys=True),
            str(now), str(now),
        ),
    )
    conn.execute(
        "UPDATE calibration_posterior_table SET activated=1, activated_at_utc=?"
        " WHERE posterior_sha256=?",
        (str(now), sha),
    )
    conn.commit()
    return {"posterior_sha256": sha, "activated_at_utc": str(now)}


def journal_fit(
    conn: sqlite3.Connection, *, fit: Mapping[str, Any], outcome: str,
    fitted_at_utc: str,
) -> int:
    ensure_schema(conn)
    cur = conn.execute(
        "INSERT INTO calibration_posterior_fit_journal("
        " posterior_sha256, stage_scope, lead_band, fitted_at_utc, outcome, evidence_json"
        ") VALUES(?,?,?,?,?,?)",
        (
            str(fit["content_sha256"]), str(fit["stage_scope"]),
            str(fit.get("lead_band") or WHOLE_STAGE), str(fitted_at_utc),
            str(outcome),
            json.dumps(fit["evidence"], ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def register_hypotheses(conn: sqlite3.Connection, *, now: str) -> int:
    """E11: write the v0 hypotheses once; returns how many were new."""

    ensure_schema(conn)
    new = 0
    for h in HYPOTHESES_V0:
        cur = conn.execute(
            "INSERT OR IGNORE INTO calibration_posterior_hypothesis("
            " hypothesis_id, registered_at_utc, mechanism, context_json,"
            " expected_sign, metric, min_event_days, status"
            ") VALUES(?,?,?,?,?,?,?,'shadow')",
            (
                h["hypothesis_id"], str(now), h["mechanism"],
                json.dumps(h["context"], ensure_ascii=False, sort_keys=True),
                h["expected_sign"], h["metric"], int(h["min_event_days"]),
            ),
        )
        new += int(cur.rowcount or 0)
    conn.commit()
    return new


# --------------------------------------------------------------------------
# Served rows
# --------------------------------------------------------------------------

def _row_to_served(row: Sequence[Any]) -> dict[str, Any]:
    (sha, band, alpha, gamma, w, w_lcb, w_ucb, decisions, station_days, days,
     accumulating, evidence_json, cutoff, activated_at) = row
    try:
        evidence = json.loads(str(evidence_json))
    except (TypeError, ValueError):
        evidence = {}
    draws = tuple(
        tuple(float(x) for x in d) for d in (evidence.get("draws") or ())
        if isinstance(d, (list, tuple)) and len(d) == 3
        and all(isinstance(x, (int, float)) and math.isfinite(float(x)) for x in d)
    )
    return {
        "sha256": str(sha), "lead_band": str(band or WHOLE_STAGE),
        "alpha": alpha, "gamma": gamma, "w": w, "w_lcb": w_lcb, "w_ucb": w_ucb,
        "decision_count": int(decisions or 0),
        "station_day_count": int(station_days or 0), "day_count": int(days or 0),
        "accumulating": bool(accumulating), "draws": draws,
        "fit_cutoff_utc": cutoff, "activated_at_utc": activated_at,
        "skill": evidence.get("skill") or {}, "ci": evidence.get("ci") or {},
        # Charter 230: a row serves its parameters only on its own verdict.
        "admitted": bool((evidence.get("verdict") or {}).get("admitted")),
        "verdict": evidence.get("verdict") or _no_verdict("unrecorded"),
    }


_SERVED_COLS = (
    "t.posterior_sha256, t.lead_band, t.alpha, t.gamma, t.w, t.w_lcb, t.w_ucb,"
    " t.decision_count, t.station_day_count, t.day_count, t.accumulating,"
    " t.evidence_json, t.fit_cutoff_utc, t.activated_at_utc"
)


def load_posterior_as_of(
    conn: sqlite3.Connection, *, stage_scope: str, as_of_utc: str | None = None,
) -> dict[str, Any] | None:
    """The stage's whole row with its band rows under ``lead_bands``; the
    active rows when ``as_of_utc`` is None, else the rows the journal says
    were current then.  None before the first fit."""

    if not _has_table(conn, "calibration_posterior_table"):
        return None
    served: dict[str, dict[str, Any]] = {}
    try:
        for band in (WHOLE_STAGE, *LEAD_BAND_LABELS):
            if as_of_utc is None:
                row = conn.execute(
                    f"SELECT {_SERVED_COLS} FROM calibration_posterior_table t"
                    " WHERE t.stage_scope=? AND t.lead_band=? AND t.activated=1"
                    " ORDER BY t.activated_at_utc DESC LIMIT 1",
                    (str(stage_scope), band),
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT {_SERVED_COLS} FROM calibration_posterior_fit_journal j"
                    " JOIN calibration_posterior_table t"
                    "   ON t.posterior_sha256=j.posterior_sha256"
                    " WHERE j.stage_scope=? AND j.lead_band=? AND j.fitted_at_utc<=?"
                    " ORDER BY j.fitted_at_utc DESC, j.fit_id DESC LIMIT 1",
                    (str(stage_scope), band, str(as_of_utc)),
                ).fetchone()
            if row is not None:
                served[band] = _row_to_served(row)
    except sqlite3.Error:
        return None
    whole = served.get(WHOLE_STAGE)
    if whole is None:
        return None
    whole["lead_bands"] = {
        band: served[band] for band in LEAD_BAND_LABELS if band in served
    }
    return whole


def load_posterior_tables(
    conn: sqlite3.Connection, *, as_of_utc: str | None = None,
) -> dict[str, dict[str, Any] | None]:
    return {
        scope: load_posterior_as_of(conn, stage_scope=scope, as_of_utc=as_of_utc)
        for scope in STAGE_SCOPES
    }


def serving_row(
    stage_row: Mapping[str, Any] | None, lead_band: str | None,
) -> tuple[Mapping[str, Any] | None, str | None]:
    """The row a decision reads: its band's when that band clears the
    hurdles, else the stage's whole row.  Mirrors the beta lane's rule."""

    if not isinstance(stage_row, Mapping):
        return None, None
    if lead_band is not None:
        band_row = (stage_row.get("lead_bands") or {}).get(lead_band)
        if isinstance(band_row, Mapping) and not band_row.get("accumulating"):
            return band_row, lead_band
    return stage_row, None


# --------------------------------------------------------------------------
# Forward skill: the out-of-sample number promotion is argued on
# --------------------------------------------------------------------------

def forward_skill(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
    window_days: int = WINDOW_DAYS,
    anchored: bool = POPULATION_ANCHORED,
    draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """Score every labelled decision on the fit that was active when it was
    taken.  A decision before the first journal entry has no fit and is
    counted out; one taken while its row was accumulating read ``b = m`` and
    scores exactly 0, which is what the lane served then."""

    import numpy as np  # noqa: PLC0415

    out: dict[str, Any] = {
        "stage_scope": stage_scope, "decisions": 0, "days": 0,
        "before_first_fit": 0, "served_market": 0,
        "s_forward": None, "s_forward_ci": [None, None], "ic_forward": None,
        "first_day": None, "last_day": None,
    }
    if not _has_table(conn, "calibration_posterior_fit_journal"):
        return out
    try:
        journal = conn.execute(
            "SELECT j.fitted_at_utc, j.lead_band, t.accumulating, t.alpha, t.gamma, t.w"
            " FROM calibration_posterior_fit_journal j"
            " JOIN calibration_posterior_table t ON t.posterior_sha256=j.posterior_sha256"
            " WHERE j.stage_scope=? ORDER BY j.fitted_at_utc, j.fit_id",
            (str(stage_scope),),
        ).fetchall()
    except sqlite3.Error:
        return out
    if not journal:
        return out
    # One entry per activation instant: the stage row and its band rows.
    groups: dict[str, dict[str, Any]] = {}
    for fitted_at, band, accumulating, alpha, gamma, w in journal:
        entry = groups.setdefault(str(fitted_at), {})
        entry[str(band or WHOLE_STAGE)] = {
            "accumulating": bool(accumulating), "alpha": alpha, "gamma": gamma, "w": w,
        }
    stamps = sorted(groups)
    rows, _meta = population(
        conn, stage_scope=stage_scope, window_days=window_days, anchored=anchored,
    )
    s_values: list[float] = []
    day_of: list[int] = []
    ic_a: list[float] = []
    ic_c: list[float] = []
    ic_w: list[float] = []
    days_seen: dict[str, int] = {}
    for row in rows:
        at = str(row["at"])
        idx = -1
        for i, stamp in enumerate(stamps):
            if stamp <= at:
                idx = i
            else:
                break
        if idx < 0:
            out["before_first_fit"] += 1
            continue
        group = groups[stamps[idx]]
        stage_row = group.get(WHOLE_STAGE)
        if stage_row is None:
            out["before_first_fit"] += 1
            continue
        band = row.get("lead_band")
        chosen = stage_row
        if band is not None and band in group and not group[band]["accumulating"]:
            chosen = group[band]
        if chosen["accumulating"] or chosen.get("w") is None:
            out["served_market"] += 1
            params = {"alpha": 0.0, "gamma": 0.0, "w": 0.0}
        else:
            params = chosen
        vector = posterior_vector_v1(row["p"], row["m"], params)
        if vector is None:
            continue
        label = str(row["label"])
        m = row["m"]
        s_values.append(
            math.log(max(vector.get(label, 0.0), EPS)) - math.log(max(float(m[label]), EPS))
        )
        day = str(row["date"])
        day_of.append(days_seen.setdefault(day, len(days_seen)))
        wt = float(row.get("weight", 1.0))
        for bucket, mi in m.items():
            a = vector.get(bucket, 0.0) - float(mi)
            c = (1.0 if str(bucket) == label else 0.0) - float(mi)
            ic_a.append(a)
            ic_c.append(c)
            ic_w.append(wt)
    out["decisions"] = len(s_values)
    out["days"] = len(days_seen)
    if s_values:
        values = np.asarray(s_values)
        lo, hi = _by_day_ci(
            values, np.asarray(day_of, dtype=int), len(days_seen),
            rng=random.Random(BOOTSTRAP_SEED), draws=int(draws),
        )
        out["s_forward"] = float(np.mean(values))
        out["s_forward_ci"] = [lo, hi]
        a = np.asarray(ic_a)
        c = np.asarray(ic_c)
        w = np.asarray(ic_w)
        va = float(np.sum(w * a * a))
        vc = float(np.sum(w * c * c))
        out["ic_forward"] = float(np.sum(w * a * c) / math.sqrt(va * vc)) if va > 0 and vc > 0 else None
        ordered = sorted(days_seen)
        out["first_day"], out["last_day"] = ordered[0], ordered[-1]
    return out


# --------------------------------------------------------------------------
# Realisation: did the windows L4 would buy make money once they settled
# --------------------------------------------------------------------------

REALISATION_CELLS: tuple[str, ...] = ("both_admit", "l4_only", "lambda_only", "both_refuse")


def _journal_groups(
    conn: sqlite3.Connection, stage_scope: str,
) -> tuple[list[str], dict[str, dict[str, dict[str, Any]]]]:
    """Activation instants for a stage, each with its stage row and band rows
    (point values, draws and accumulating flag), oldest first."""

    rows = conn.execute(
        "SELECT j.fitted_at_utc, j.lead_band, t.accumulating, t.alpha, t.gamma,"
        " t.w, t.evidence_json"
        " FROM calibration_posterior_fit_journal j"
        " JOIN calibration_posterior_table t ON t.posterior_sha256=j.posterior_sha256"
        " WHERE j.stage_scope=? ORDER BY j.fitted_at_utc, j.fit_id",
        (str(stage_scope),),
    ).fetchall()
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    for fitted_at, band, accumulating, alpha, gamma, w, evidence_json in rows:
        try:
            draws = tuple(
                tuple(float(x) for x in d)
                for d in (json.loads(str(evidence_json)).get("draws") or ())
                if isinstance(d, (list, tuple)) and len(d) == 3
            )
        except (TypeError, ValueError):
            draws = ()
        groups.setdefault(str(fitted_at), {})[str(band or WHOLE_STAGE)] = {
            "accumulating": bool(accumulating), "alpha": alpha, "gamma": gamma,
            "w": w, "draws": draws,
        }
    return sorted(groups), groups


def realisation(
    conn: sqlite3.Connection,
    *,
    since_event_date: str,
    draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """Every settled candidate window production enumerated since a day,
    re-judged with the L4 row that was active when the decision was taken,
    crossed with the verdict production's own gate gave it.

    The windows are the production evidence table's sampled rows (charter
    140: every priced window of the first decision per station, day, stage
    and lead band, refused ones included), so the refused cells are the
    control the admitted ones are read against.  The belief is rebuilt from
    the decision's committed envelope and the official book it priced
    against, through the same kernel the runtime's shadow calls; the edge is
    ``window mass - all-in price`` and the verdict is the runtime's own
    (lower bound across the fit's draws at least the economic hurdle).  A
    decision taken while its row was accumulating served ``b = m`` and gave
    no verdict; it is counted, not judged.  One difference from the runtime:
    the replay does not zero buckets the day's observed high has already
    ruled out, because the evidence row does not carry that set; on the
    windows the gate prices those buckets carry almost no mass either way.

    Per cell: windows, station-days, event days, mean price, hit rate, the
    L4 edge it claimed, the ``hit - price`` it realised with a by-event-day
    bootstrap interval.
    """

    from .basket_kernel.delta_v2 import economic_hurdle_usdc  # noqa: PLC0415
    from .basket_kernel.posterior_v1 import (  # noqa: PLC0415
        posterior_draw_vectors_v1,
        window_mass,
    )
    from .basket_kernel.valuation_v1 import (  # noqa: PLC0415
        action_value_v1,
        market_reference_v1,
    )
    from .candidate_window_evidence import (  # noqa: PLC0415
        ACCEPTANCE_CLAIM_FLOOR,
        ACCEPTANCE_MARKET_CEILING,
        EVIDENCE_TABLE,
        SCORED_VIEW,
        in_acceptance_class,
    )

    out: dict[str, Any] = {
        "since": str(since_event_date), "windows": 0, "judged": 0,
        "no_fit": 0, "accumulating": 0, "no_vector": 0,
        "cells": {}, "by_stage": {}, "acceptance": {},
    }
    needed = (
        SCORED_VIEW, EVIDENCE_TABLE, "calibration_posterior_fit_journal",
        "basket_policy_training_fact", "basket_probability_envelope",
        "basket_official_ws_snapshot",
    )
    if not all(_has_table(conn, name) for name in needed):
        out["error"] = "missing_table"
        return out
    rows = conn.execute(
        "SELECT v.station_id, v.event_date, v.stage, v.decision_at_utc,"
        "       v.buckets_json, v.all_in_price, v.admissible, v.hit,"
        "       v.hours_to_cutoff, f.probability_envelope_id, f.ws_snapshot_id,"
        "       v.raw_win_probability, v.market_mid"
        f" FROM {SCORED_VIEW} v"
        f" JOIN {EVIDENCE_TABLE} e ON e.evidence_id = v.candidate_id"
        " JOIN basket_policy_training_fact f ON f.fact_id = e.fact_id"
        " WHERE v.hit IS NOT NULL AND v.event_date >= ?"
        "   AND v.all_in_price IS NOT NULL",
        (str(since_event_date),),
    ).fetchall()
    journals: dict[str, tuple[list[str], dict[str, dict[str, dict[str, Any]]]]] = {}
    vectors: dict[tuple[str, str, str], tuple[list[dict[str, float]], dict[str, float]] | None] = {}
    judged: list[dict[str, Any]] = []
    acceptance_windows = 0
    for (station, day, stage, decided_at, buckets_json, price, admissible, hit,
         hours, envelope_id, snapshot_id, raw_claim, market_mid) in rows:
        out["windows"] += 1
        # The acceptance class (user ruling, 2026-09-17), on the row's own
        # raw claim and market mid, counted before any verdict so the
        # windows still waiting for a fit are visible too.
        in_class = in_acceptance_class(raw_claim, market_mid)
        acceptance_windows += int(in_class)
        stage = str(stage)
        if stage not in journals:
            journals[stage] = _journal_groups(conn, stage)
        stamps, groups = journals[stage]
        at = str(decided_at)
        active = None
        active_stamp = ""
        for stamp in stamps:
            if stamp <= at:
                active, active_stamp = groups[stamp], stamp
            else:
                break
        stage_row = None if active is None else active.get(WHOLE_STAGE)
        if stage_row is None:
            out["no_fit"] += 1
            continue
        band = beta_lead_band(hours) if hours is not None else None
        chosen, chosen_band = stage_row, WHOLE_STAGE
        if band is not None and band in active and not active[band]["accumulating"]:
            chosen, chosen_band = active[band], band
        if chosen["accumulating"] or chosen.get("w") is None or not chosen["draws"]:
            out["accumulating"] += 1
            continue
        key = (str(envelope_id), str(snapshot_id), active_stamp, chosen_band)
        if key not in vectors:
            vectors[key] = None
            env = conn.execute(
                "SELECT probabilities_json FROM basket_probability_envelope WHERE envelope_id=?",
                (str(envelope_id),),
            ).fetchone()
            book = conn.execute(
                "SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?",
                (str(snapshot_id),),
            ).fetchone()
            try:
                p = {str(k): float(v) for k, v in json.loads(env[0] or "{}").items()} if env else {}
                ladder = json.loads(book[0] or "[]") if book else []
            except (TypeError, ValueError):
                p, ladder = {}, []
            market = market_reference_v1(ladder if isinstance(ladder, list) else [])
            if p and market.get("state") == "complete":
                point = posterior_vector_v1(p, market["vector"], chosen)
                sampled = posterior_draw_vectors_v1(
                    p, market["vector"], chosen["draws"][: int(draws)],
                )
                if point is not None and sampled:
                    vectors[key] = (sampled, point)
        cached = vectors[key]
        if cached is None:
            out["no_vector"] += 1
            continue
        sampled, point = cached
        try:
            window = [str(b) for b in json.loads(buckets_json or "[]")]
        except (TypeError, ValueError):
            out["no_vector"] += 1
            continue
        cost = float(price)
        value = action_value_v1(sampled, {b: 1.0 for b in window}, cost)
        if value is None:
            out["no_vector"] += 1
            continue
        mass = window_mass(point, window)
        l4_admit = float(value["value_lcb"]) >= economic_hurdle_usdc(cost) - 1e-12
        lam_admit = bool(admissible)
        cell = (
            ("both_admit" if lam_admit else "l4_only") if l4_admit
            else ("lambda_only" if lam_admit else "both_refuse")
        )
        judged.append({
            "cell": cell, "stage": stage, "station": str(station), "date": str(day),
            "price": cost, "hit": float(hit), "edge": mass - cost,
            "acceptance": in_class,
        })
    out["judged"] = len(judged)

    def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {"n": 0, "station_days": 0, "days": 0, "price": None, "hit": None,
                    "claimed_edge": None, "hit_minus_price": None, "ci": [None, None]}
        by_day: dict[str, list[float]] = {}
        for item in items:
            by_day.setdefault(item["date"], []).append(item["hit"] - item["price"])
        keys = sorted(by_day)
        rng = random.Random(BOOTSTRAP_SEED)
        boot: list[float] = []
        for _ in range(int(draws)):
            pick = [keys[rng.randrange(len(keys))] for _ in keys]
            num = sum(sum(by_day[k]) for k in pick)
            den = sum(len(by_day[k]) for k in pick)
            boot.append(num / den if den else 0.0)
        boot.sort()
        n = len(items)
        return {
            "n": n,
            "station_days": len({(i["station"], i["date"]) for i in items}),
            "days": len(keys),
            "price": sum(i["price"] for i in items) / n,
            "hit": sum(i["hit"] for i in items) / n,
            "claimed_edge": sum(i["edge"] for i in items) / n,
            "hit_minus_price": sum(i["hit"] - i["price"] for i in items) / n,
            "ci": [boot[min(len(boot) - 1, int(LCB_QUANTILE * len(boot)))],
                   boot[max(0, int((1.0 - LCB_QUANTILE) * len(boot)) - 1)]],
        }

    out["cells"] = {
        cell: _summary([j for j in judged if j["cell"] == cell]) for cell in REALISATION_CELLS
    }
    out["l4_admitted"] = _summary([j for j in judged if j["cell"] in ("both_admit", "l4_only")])
    out["lambda_admitted"] = _summary([j for j in judged if j["cell"] in ("both_admit", "lambda_only")])
    for stage in sorted(STAGE_SCOPES):
        items = [j for j in judged if j["stage"] == stage]
        out["by_stage"][stage] = {
            "judged": len(items),
            "l4_admitted": _summary([j for j in items if j["cell"] in ("both_admit", "l4_only")]),
            "lambda_admitted": _summary([j for j in items if j["cell"] in ("both_admit", "lambda_only")]),
        }
    # The acceptance sample: the class's windows, whoever bought them, then
    # the ones L4 would buy and the ones the live gate bought.  L4's edge is
    # read here first, because this is the shape nothing upstream can see.
    accepted = [j for j in judged if j["acceptance"]]
    out["acceptance"] = {
        "claim_floor": ACCEPTANCE_CLAIM_FLOOR,
        "market_ceiling": ACCEPTANCE_MARKET_CEILING,
        "windows": acceptance_windows,
        "judged": len(accepted),
        "all": _summary(accepted),
        "l4_admitted": _summary([j for j in accepted if j["cell"] in ("both_admit", "l4_only")]),
        "lambda_admitted": _summary([j for j in accepted if j["cell"] in ("both_admit", "lambda_only")]),
    }
    return out


__all__ = [
    "SCHEMA", "WINDOW_DAYS", "MIN_STATION_DAYS", "MIN_DAYS", "BOOTSTRAP_DRAWS",
    "PRIOR_SD", "BAND_SHRINK_DAYS", "WHOLE_STAGE", "LEAD_BAND_LABELS",
    "HYPOTHESES_V0", "POSTERIOR_TABLE_KEY", "STAGE_SCOPES",
    "ensure_schema", "population", "fit_posterior", "fit_stage_and_bands",
    "persist_and_activate", "journal_fit", "register_hypotheses",
    "load_posterior_as_of", "load_posterior_tables", "serving_row",
    "forward_skill", "realisation", "REALISATION_CELLS",
    "walk_forward_verdict", "VERDICT_RULE", "VERDICT_MIN_TEST_DAYS",
]
