"""DB-resident L4 posterior parameters: the fit, the journal, the served
rows, the forward skill score.  Shadow lane for the first-principles baseline
(docs/architecture/EDGE_SYSTEM_FIRST_PRINCIPLES_BASELINE_2026-09-17.md).

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
    anchored: bool = True,
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
    anchored: bool = True,
    lead_band: str | None = None,
    rows: Sequence[Mapping[str, Any]] | None = None,
    meta: Mapping[str, Any] | None = None,
    parent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One row's fit: the stage's whole population, or one lead band of it
    shrunk to ``parent`` (the stage's fit) by ``BAND_SHRINK_DAYS``."""

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
        theta = _fit_logistic(design, row_weight=ones, prior_mean=(0.0, 0.0, 0.0))
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
            draw = _fit_logistic(
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
    evidence.update({
        "accumulating": bool(accumulating),
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
    anchored: bool = True,
) -> list[dict[str, Any]]:
    """The stage's whole row first, then one row per lead band shrunk to it;
    all cut from one population."""

    rows, meta = population(
        conn, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
        cutoff_utc=cutoff_utc, anchored=anchored,
    )
    whole = fit_posterior(
        None, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
        cutoff_utc=cutoff_utc, draws=draws, anchored=anchored, rows=rows, meta=meta,
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
        ))
    return fits


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
    anchored: bool = True,
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
]
