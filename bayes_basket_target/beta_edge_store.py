"""DB-resident beta: how much of the model's disagreement with the market
arrives, one number per served stage -- and, when a band earns it, one per
lead band inside the stage.

Charter 128 (user ruling, 2026-09-12): the third cut of the claim-chain
clean-up.  Charter 68's lambda(a) table answered "what fraction of the claim
arrives" with seven free numbers per stage keyed on ``price / claim``; on
the journal's own record every one of those tables, applied to the 1-3 days
after it was fitted, admitted windows that then realised the same
``hit - price`` as the ones it refused (P0 -0.063 against -0.062 over 4,894
window-days).  The fraction of the claim that arrives is not a function of
the agreement band; it is one slope.

The identity this lane estimates, per enumerated window (v2 basis, user
ruling 2026-09-14 "改")::

    hit - m  =  a  +  beta * (p - m)  +  gamma * (m - m0)      (v3)

with ``p`` the raw served claim (the probability layer's own number, no
position multiplier), ``m`` the MARKET'S BELIEF for the window -- the
normalised mid of the whole ladder summed over the window's buckets, the
same vector charter 132 reads -- and ``hit`` whether the truth landed in it.
``beta`` is the share of the claimed disagreement that shows up in settled
outcomes -- 0 means the market's belief is the better estimate wherever the
two differ, 1 means the claim is right about all of it, negative means the
disagreement leans the wrong way.  On the mid basis ``a`` is zero when the
market is calibrated at its centre; the spread and the fee are NOT in this
regression.

``gamma`` (v3, user ruling 2026-09-17: 把 β 车道补上 γ) is the market's own
slope -- how the market's error runs with its price, read at zero
disagreement around the population's mean mid ``m0``.  It is the third of
the three knobs a belief in ``(p, m)`` has (level, market slope,
disagreement share), and without it the market's reliability curve leaks
into beta: the model disagrees more where the book is cheap (corr(p - m, m)
-0.42 on the priced buckets since 2026-08-15), so a market that
under-realises its cheap prices and over-realises its dear ones read as the
disagreement leaning the wrong way -- beta -0.064 with two knobs, +0.050
with three, gamma +0.21 [+0.17, +0.25] by day.  The joint draws are
``(a, beta, gamma)`` triples and the v3 prediction reads all three; the
charter-128 belief ``m + beta_lcb * (p - m)`` is unchanged and still
carries neither ``a`` nor ``gamma`` (charter 132 records why).
They are charged where they belong: the gate compares the served belief
``m + beta_lcb * (p - m)`` with the window's all-in ask under charter 130's
hurdle.

Why not the ask (the v1 basis).  With ``q = m + d`` (``d`` half a spread
plus the fee) the regressor ``p - q = x - d`` and the outcome ``hit - q =
(t - m) - d`` share ``-d``; a spread that varies across windows then reads as
realised disagreement, and a model with no information at all fits a slope
of ``Var(d) / (Var(x) + Var(d))``.  Measured 2026-09-14 on identical rows:
the ask basis read beta 0.016 (P0) and 0.033 (P1) above the mid basis, its
intercept absorbing the mean spread.

The second axis (same ruling): hours from the decision to the settlement
cutoff, in the kernel's ``BETA_LEAD_BANDS``.  The disagreement realises
differently through the day -- the market's error shrinks as observations
arrive and the model reads them earlier or later than the book does -- and a
stage-wide slope pools bands that do not share one.  Every band is fitted
beside the stage's whole row under the same hurdles; a band's row serves
only when it clears them, and the whole row serves otherwise.

The fit is walk-forward by construction -- as-of a day it reads only rows
whose label was knowable by the cutoff -- and pooled over stations, with the
event date as the independent unit: a fleet shares its weather, so the
interval is a by-day bootstrap and the served number is its lower bound.
Below the hurdles a stage serves ``beta_lcb = 0`` and says so: the belief
collapses onto the market's and the gate refuses, which is the honest
reading of "no evidence yet".

The sample is bounded below at the stage's serving-arithmetic anchor, like
every claim lane: rows from before the served distribution changed shape
describe a claim nobody serves.  Rows written before the ledger carried the
market mid (the candidate source's ``market_mid``) are not evidence on
this basis and are counted out, never priced at the ask instead.

Shadow first.  ``BETA_EDGE_MODE`` in the kernel defaults to ``shadow``: the
enumeration records the beta edge and the beta verdict on every priced
window and changes nothing; ``on`` makes the gate and the solver read the
beta belief instead of lambda's.  Promotion is a separate ruling, argued on
the shadow's own record (admitted minus refused, out of sample, ten event
days or more).
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import date, timedelta
from statistics import mean
from typing import Any, Mapping, Sequence

from .basket_kernel.delta_v2 import (
    BETA_LEAD_BANDS,
    BETA_PRICE_BASIS,
    BETA_TABLE_KEY,
    beta_lead_band,
)
from .candidate_window_evidence import CANDIDATE_SOURCE
from .contracts import content_sha256

# v4: the fit carries gamma, the market's own slope, and its draws are
# triples (user ruling, 2026-09-17).  The string is in the content hash, so
# every scope refits under the new rule at the next sweep; rows fitted
# under v3 keep serving until then and read gamma = 0.
SCHEMA = "beta_edge_table_v4"
STAGE_SCOPES: dict[str, tuple[str, ...]] = {
    "P0": ("P0",), "P0-M": ("P0-M",), "P1": ("P1",),
}
WINDOW_DAYS = 28
MIN_ROWS = 60
MIN_DAYS = 8
BOOTSTRAP_DRAWS = 400
BOOTSTRAP_SEED = 20260912
LCB_QUANTILE = 0.05
PRICE_BASIS = BETA_PRICE_BASIS
#: The ``lead_band`` of a stage's whole row.  A band row carries its label.
WHOLE_STAGE = ""
LEAD_BAND_LABELS: tuple[str, ...] = tuple(label for _lo, _hi, label in BETA_LEAD_BANDS)
#: Kept under its old name for the readers that iterate it (``layered_readings``).
LEAD_BUCKETS = BETA_LEAD_BANDS
# Charter 132 (2026-09-13): the joint (alpha, beta) draws a fit keeps so the
# served lower bound is taken on the whole prediction ``alpha + beta * d``,
# not on beta's marginal.  Rounded because they ride in evidence_json and are
# read back every cycle; six places is far below the bootstrap's own noise.
JOINT_DRAW_DECIMALS = 6

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS calibration_beta_edge_table (
  beta_sha256 TEXT PRIMARY KEY,
  stage_scope TEXT NOT NULL,
  price_basis TEXT NOT NULL,
  window_days INTEGER NOT NULL,
  as_of_event_date TEXT NOT NULL,
  beta REAL,
  beta_lcb REAL NOT NULL,
  beta_ucb REAL,
  intercept REAL,
  row_count INTEGER NOT NULL,
  day_count INTEGER NOT NULL,
  accumulating INTEGER NOT NULL CHECK(accumulating IN (0,1)),
  evidence_json TEXT NOT NULL,
  fitted_at_utc TEXT NOT NULL,
  activated INTEGER NOT NULL CHECK(activated IN (0,1)),
  activated_at_utc TEXT,
  lead_band TEXT NOT NULL DEFAULT ''
)
"""
_ACTIVE_INDEX = (
    "CREATE INDEX IF NOT EXISTS calibration_beta_edge_active_band_idx"
    " ON calibration_beta_edge_table(stage_scope, lead_band, activated)"
)
_JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS calibration_beta_edge_fit_journal (
  fit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  beta_sha256 TEXT NOT NULL,
  stage_scope TEXT NOT NULL,
  fitted_at_utc TEXT NOT NULL,
  outcome TEXT NOT NULL,
  evidence_json TEXT NOT NULL
)
"""
_JOURNAL_INDEX = (
    "CREATE INDEX IF NOT EXISTS calibration_beta_edge_journal_idx"
    " ON calibration_beta_edge_fit_journal(stage_scope, fitted_at_utc, fit_id)"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_DDL)
    # A table from before the band axis existed: widened in place, every
    # row it holds being a whole-stage row (v1 basis, which the loaders
    # below no longer serve -- the basis column keeps them apart).
    if not _has_column(conn, "calibration_beta_edge_table", "lead_band"):
        conn.execute(
            "ALTER TABLE calibration_beta_edge_table"
            " ADD COLUMN lead_band TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(_ACTIVE_INDEX)
    conn.execute(_JOURNAL_DDL)
    conn.execute(_JOURNAL_INDEX)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        # Views count: the candidate source is one (charter 140).
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone() is not None


def _anchor(conn: sqlite3.Connection, stage: str) -> tuple[str | None, str]:
    """The stage's serving-arithmetic anchor and how it was read.

    ``absent`` -- the store that keeps the anchor is not in this database (a
    research extract, a fixture): no bound, recorded as such.  ``ok`` -- read,
    possibly None when nothing has been committed yet.  ``error`` -- the store
    is there and the read failed.

    The third state used to be folded into the first (charter 132, audit item
    48): any exception became "no bound", so a transient failure silently
    widened a served fit onto rows from a distribution nobody serves.  A fit
    that cannot read its own floor now refuses to fit.
    """

    if not (
        _has_table(conn, "basket_probability_bridge_history")
        and _has_table(conn, "basket_probability_envelope")
    ):
        return None, "absent"
    try:
        from .center_width_store import served_arithmetic_anchor  # noqa: PLC0415

        return served_arithmetic_anchor(conn, stage=stage), "ok"
    except Exception:  # noqa: BLE001 -- a present store that fails is a refusal
        return None, "error"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(
            str(row[1]) == column
            for row in conn.execute(f"PRAGMA table_info({table})")
        )
    except sqlite3.Error:
        return False


def _utc_now_iso() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ols(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float] | None:
    if len(xs) < 3:
        return None
    mx, my = mean(xs), mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    if vx <= 0.0:
        return None
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
    return my - beta * mx, beta


def _ols3(
    xs: Sequence[float], zs: Sequence[float], ys: Sequence[float],
) -> tuple[float, float, float] | None:
    """``y = a + beta * x + gamma * z`` by the normal equations on centred
    regressors; None when either regressor is flat or the two are collinear
    (then the market's slope and the disagreement cannot be told apart and
    the caller falls back to the two-knob fit)."""

    if len(xs) < 4:
        return None
    mx, mz, my = mean(xs), mean(zs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    szz = sum((z - mz) ** 2 for z in zs)
    sxz = sum((x - mx) * (z - mz) for x, z in zip(xs, zs))
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    szy = sum((z - mz) * (y - my) for z, y in zip(zs, ys))
    det = sxx * szz - sxz * sxz
    if sxx <= 0.0 or szz <= 0.0 or det <= 1e-12 * sxx * szz:
        return None
    beta = (sxy * szz - szy * sxz) / det
    gamma = (szy * sxx - sxy * sxz) / det
    return my - beta * mx - gamma * mz, beta, gamma


def population(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS,
    anchored: bool = True,
    cutoff_utc: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every enumerated window the ledger priced and settled for the scope's
    stages that a fit at ``cutoff_utc`` could have known about, on the mid
    basis.

    Charter 132 (audit items 47 and 48) makes three conditions explicit that
    the event-date bound used to stand in for:

    * **The label was knowable.**  ``event_date <= as_of`` says the day is
      over; it does not say the truth had been published.  A row is read only
      when the truth revision its label came from had been committed by the
      cutoff (``label_knowability``), and a decision taken after the cutoff is
      not a row the fit could have seen either.  A table without a settlement
      time cannot state when its labels became knowable and is refused.
    * **The label is a label.**  ``hit`` must be 0 or 1.  ``hit or 0`` used to
      turn a missing label into a loss; such rows are now counted and dropped.
    * **The window is today's population.**  The same seat-shape partition
      lambda's fit applies: rows enumerated under another shape rule, or before
      the rule recorded one, are not evidence about windows shaped today.  A
      table without the column partitions on nothing, exactly as lambda's does.

    And one the v2 basis adds: **the row carries the market's belief.**  A
    table without ``market_mid`` cannot state the disagreement this lane
    regresses on and is refused outright; a row without one (the book was
    incomplete at that decision, or the row predates the column) is counted
    out, never priced at the ask in its place.  ``hours_to_cutoff`` is read
    when the table has it and gives each row its lead band; without the
    column no row has a band and only the whole-stage fit is possible.

    ``cutoff_utc`` defaults to now, which is what the nightly fit means.  A
    walk-forward caller passes the moment it is standing at.
    """

    if stage_scope not in STAGE_SCOPES:
        raise ValueError(f"beta_edge_scope_invalid:{stage_scope}")
    if not _has_table(conn, CANDIDATE_SOURCE):
        return [], {"error": "no_candidate_windows"}
    if not _has_column(conn, CANDIDATE_SOURCE, "settled_at_utc"):
        return [], {"error": "no_label_knowable_column"}
    if not _has_column(conn, CANDIDATE_SOURCE, "market_mid"):
        return [], {"error": "no_market_mid_column", "price_basis": PRICE_BASIS}
    has_clock = _has_column(conn, CANDIDATE_SOURCE, "hours_to_cutoff")
    stages = STAGE_SCOPES[stage_scope]
    cutoff = str(cutoff_utc or _utc_now_iso())
    from .label_knowability import knowable_labels  # noqa: PLC0415

    knowable = knowable_labels(conn)
    knowable_days = [
        day for (_s, day), label in knowable.items() if label["known_at"] <= cutoff
    ]
    latest = str(as_of or (max(knowable_days) if knowable_days else "") or "")
    if not latest:
        return [], {"error": "no_settled_windows", "cutoff_utc": cutoff}
    floor_day = (
        date.fromisoformat(latest) - timedelta(days=int(window_days))
    ).isoformat()
    anchor_states: dict[str, str] = {}
    anchors: list[str] = []
    anchors_after_cutoff = 0
    if anchored:
        for stage in stages:
            value, state = _anchor(conn, stage)
            anchor_states[stage] = state
            if state == "error":
                return [], {
                    "error": "anchor_unreadable", "stage": stage,
                    "cutoff_utc": cutoff,
                }
            if value:
                if str(value) > cutoff:
                    # The version serving today started after the moment this
                    # fit stands at; its floor did not exist yet.  Dropped and
                    # counted -- the version that served at the cutoff is not
                    # recoverable here, so the rolling window is the bound.
                    anchors_after_cutoff += 1
                else:
                    anchors.append(str(value))
    anchor_utc = max(anchors) if anchors else None
    shape = None
    if _has_column(conn, CANDIDATE_SOURCE, "seat_shape_rule"):
        from .basket_kernel.delta_v2 import (  # noqa: PLC0415
            SEAT_SHAPE_RULE_VERSION,
        )

        shape = str(SEAT_SHAPE_RULE_VERSION)
    placeholders = ",".join("?" for _ in stages)
    base_where = (
        f" WHERE stage IN ({placeholders})"
        "   AND settled_bucket IS NOT NULL"
        "   AND raw_win_probability IS NOT NULL"
        "   AND all_in_price IS NOT NULL AND all_in_price > 0"
        "   AND event_date > ? AND event_date <= ?"
    )
    base_args: tuple[Any, ...] = (*stages, floor_day, latest)
    rows = conn.execute(
        "SELECT station_id, event_date, stage, decision_at_utc,"
        "       raw_win_probability, all_in_price, hit, settled_at_utc,"
        + (" seat_shape_rule," if shape is not None else " NULL,")
        + " market_mid,"
        + (" hours_to_cutoff" if has_clock else " NULL")
        + f" FROM {CANDIDATE_SOURCE}" + base_where,
        base_args,
    ).fetchall()
    out = []
    counts = {
        "excluded_before_anchor": 0,
        "excluded_label_not_yet_knowable": 0,
        "excluded_decided_after_cutoff": 0,
        "rejected_invalid_label": 0,
        "excluded_other_shape": 0,
        "excluded_no_market_mid": 0,
    }
    for (station, day, stage, at, raw, price, hit, _written, row_shape,
         mid, hours) in rows:
        if shape is not None and (row_shape is None or str(row_shape) != shape):
            counts["excluded_other_shape"] += 1
            continue
        label = knowable.get((str(station), str(day)))
        if label is None or str(label["known_at"]) > cutoff:
            counts["excluded_label_not_yet_knowable"] += 1
            continue
        if str(at or "") > cutoff:
            counts["excluded_decided_after_cutoff"] += 1
            continue
        if anchor_utc and str(at or "") < anchor_utc:
            counts["excluded_before_anchor"] += 1
            continue
        if hit not in (0, 1) or isinstance(hit, bool):
            counts["rejected_invalid_label"] += 1
            continue
        try:
            mid_value = float(mid)
        except (TypeError, ValueError):
            mid_value = float("nan")
        if not (0.0 < mid_value < 1.0):
            counts["excluded_no_market_mid"] += 1
            continue
        out.append({
            "station": str(station), "date": str(day), "stage": str(stage),
            "at": str(at or ""),
            "x": float(raw) - mid_value,
            "y": float(hit) - mid_value,
            "raw": float(raw), "mid": mid_value, "price": float(price),
            "hit": float(hit),
            "hours_to_cutoff": None if hours is None else float(hours),
            "lead_band": beta_lead_band(hours),
        })
    return out, {
        "as_of_event_date": latest, "floor_day": floor_day,
        "cutoff_utc": cutoff, "anchor_utc": anchor_utc,
        "anchor_states": anchor_states,
        "anchors_after_cutoff": anchors_after_cutoff,
        "anchored": bool(anchored), "seat_shape_rule": shape,
        "price_basis": PRICE_BASIS,
        "lead_clock": "hours_to_cutoff" if has_clock else "none",
        **counts,
    }


LAYER_MIN_ROWS = 12


def layered_readings(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Point-estimate betas by station and by lead band -- research readings
    for the shadow.  A band is served only through its own fitted row
    (``fit_stage_and_bands``), never off this table."""

    by_station: dict[str, list[Mapping[str, Any]]] = {}
    by_lead: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        by_station.setdefault(str(r["station"]), []).append(r)
        band = r.get("lead_band")
        if band:
            by_lead.setdefault(str(band), []).append(r)

    def cell(group: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        if len(group) < LAYER_MIN_ROWS:
            return None
        centre = mean(r["mid"] for r in group)
        got3 = _ols3(
            [r["x"] for r in group], [r["mid"] - centre for r in group],
            [r["y"] for r in group],
        )
        if got3 is not None:
            intercept, beta, gamma = got3
        else:
            got = _ols([r["x"] for r in group], [r["y"] for r in group])
            if got is None:
                return None
            intercept, beta, gamma = got[0], got[1], None
        return {
            "rows": len(group), "days": len({r["date"] for r in group}),
            "beta": round(beta, 4), "intercept": round(intercept, 4),
            "gamma": None if gamma is None else round(gamma, 4),
            "mean_x": round(mean(r["x"] for r in group), 4),
        }

    return {
        "by_lead": {k: v for k, v in ((k, cell(g)) for k, g in by_lead.items()) if v},
        "by_station": {k: v for k, v in ((k, cell(g)) for k, g in by_station.items()) if v},
    }


def fit_beta(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS,
    draws: int = BOOTSTRAP_DRAWS,
    anchored: bool = True,
    cutoff_utc: str | None = None,
    lead_band: str | None = None,
    rows: Sequence[Mapping[str, Any]] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One beta with a by-day bootstrap lower bound; below the hurdles the
    served bound is 0 and the fit says ``accumulating``.  ``anchored=False``
    is a research reading (the anchor is what keeps a served fit off rows
    from a shape nobody serves) and is never persisted.

    ``lead_band`` restricts the fit to the rows decided in that band (the
    kernel's ``BETA_LEAD_BANDS`` labels); None is the stage's whole row.
    ``rows``/``meta`` let ``fit_stage_and_bands`` cut the population once
    and fit six times.

    Charter 132 (audit items 42 and 44): the bootstrap keeps the JOINT draws
    of ``(intercept, beta)``, because the served quantity is the prediction
    ``intercept + beta * d`` and not beta alone.  The two are estimated from
    one sample and are correlated; a lower bound on beta times ``d`` drops the
    intercept and flips its conservatism whenever ``d`` is negative.
    ``beta_lcb`` stays on the table for charter 128's shadow and its record;
    the new shadow reads the draws.
    """

    if lead_band is not None and lead_band not in LEAD_BAND_LABELS:
        raise ValueError(f"beta_edge_lead_band_invalid:{lead_band}")
    if rows is None:
        rows, meta = population(
            conn, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
            anchored=anchored, cutoff_utc=cutoff_utc,
        )
    meta = dict(meta or {})
    band = WHOLE_STAGE if lead_band is None else str(lead_band)
    if lead_band is not None:
        rows = [r for r in rows if r.get("lead_band") == lead_band]
    days = sorted({r["date"] for r in rows})
    evidence: dict[str, Any] = {
        "schema": SCHEMA, "stage_scope": stage_scope, "price_basis": PRICE_BASIS,
        "lead_band": band,
        "window_days": int(window_days), "row_count": len(rows),
        "day_count": len(days), "min_rows": MIN_ROWS, "min_days": MIN_DAYS,
        **meta,
    }
    accumulating = len(rows) < MIN_ROWS or len(days) < MIN_DAYS
    beta = intercept = lcb = ucb = None
    intercept_lcb = intercept_ucb = None
    gamma = gamma_lcb = gamma_ucb = None
    # v3: the market centre the fit is taken at.  The third regressor is
    # ``mid - centre`` so the intercept stays the market's level at a
    # typical price and gamma its slope; the centre travels with the row.
    centre = round(mean(r["mid"] for r in rows), 6) if rows else None
    joint: list[tuple[float, float, float]] = []
    per_day: dict[str, float] = {}

    def _fit(group: Sequence[Mapping[str, Any]]) -> tuple[float, float, float] | None:
        got3 = _ols3(
            [r["x"] for r in group], [r["mid"] - float(centre) for r in group],
            [r["y"] for r in group],
        )
        if got3 is not None:
            return got3
        got = _ols([r["x"] for r in group], [r["y"] for r in group])
        return None if got is None else (got[0], got[1], 0.0)

    if not accumulating:
        fitted = _fit(rows)
        if fitted is None:
            accumulating = True
        else:
            intercept, beta, gamma = fitted
            by_day: dict[str, list[Mapping[str, Any]]] = {}
            for r in rows:
                by_day.setdefault(r["date"], []).append(r)
            rng = random.Random(BOOTSTRAP_SEED)
            keys = list(by_day)
            samples: list[float] = []
            for _ in range(int(draws)):
                picked = [by_day[k] for k in rng.choices(keys, k=len(keys))]
                got = _fit([r for grp in picked for r in grp])
                if got is not None:
                    samples.append(got[1])
                    joint.append((
                        round(float(got[0]), JOINT_DRAW_DECIMALS),
                        round(float(got[1]), JOINT_DRAW_DECIMALS),
                        round(float(got[2]), JOINT_DRAW_DECIMALS),
                    ))
            samples.sort()
            if samples:
                lcb = samples[min(len(samples) - 1, int(LCB_QUANTILE * len(samples)))]
                ucb = samples[max(0, int((1.0 - LCB_QUANTILE) * len(samples)) - 1)]
            if joint:
                intercepts = sorted(a for a, _b, _g in joint)
                intercept_lcb = intercepts[
                    min(len(intercepts) - 1, int(LCB_QUANTILE * len(intercepts)))
                ]
                intercept_ucb = intercepts[
                    max(0, int((1.0 - LCB_QUANTILE) * len(intercepts)) - 1)
                ]
                gammas = sorted(g for _a, _b, g in joint)
                gamma_lcb = gammas[min(len(gammas) - 1, int(LCB_QUANTILE * len(gammas)))]
                gamma_ucb = gammas[max(0, int((1.0 - LCB_QUANTILE) * len(gammas)) - 1)]
            for k, grp in by_day.items():
                got = _fit(grp)
                if got is not None:
                    per_day[k] = round(got[1], 4)
    served_lcb = 0.0 if (accumulating or lcb is None) else float(lcb)
    evidence.update({
        "accumulating": bool(accumulating),
        "beta": None if beta is None else round(float(beta), 6),
        "intercept": None if intercept is None else round(float(intercept), 6),
        "beta_lcb": None if lcb is None else round(float(lcb), 6),
        "beta_ucb": None if ucb is None else round(float(ucb), 6),
        "served_beta_lcb": round(served_lcb, 6),
        "intercept_lcb": (
            None if intercept_lcb is None else round(float(intercept_lcb), 6)
        ),
        "intercept_ucb": (
            None if intercept_ucb is None else round(float(intercept_ucb), 6)
        ),
        # v3: the market's own slope around the centre the fit was taken at.
        "gamma": None if gamma is None else round(float(gamma), 6),
        "gamma_lcb": None if gamma_lcb is None else round(float(gamma_lcb), 6),
        "gamma_ucb": None if gamma_ucb is None else round(float(gamma_ucb), 6),
        "market_centre": centre,
        # Empty while accumulating: an accumulating fit serves nothing, and
        # a served lower bound read off draws from too few days would be the
        # hurdle waived by the back door.  Triples since v4: (a, beta, gamma).
        "joint_draws": [] if accumulating else [list(d) for d in joint],
        "per_day_beta": per_day,
        "days_beta_positive": sum(1 for v in per_day.values() if v > 0),
        "mean_x": round(mean(r["x"] for r in rows), 6) if rows else None,
        "mean_y": round(mean(r["y"] for r in rows), 6) if rows else None,
        # Research readings by layer, recorded so a layer that earns its own
        # beta can be seen coming; the whole row carries them, a band row
        # carries only its own.
        "layers": layered_readings(rows) if lead_band is None else {},
    })
    table = {
        "schema": SCHEMA, "stage_scope": stage_scope, "price_basis": PRICE_BASIS,
        "lead_band": band,
        "window_days": int(window_days), "as_of_event_date": meta.get("as_of_event_date"),
        "beta": evidence["beta"], "beta_lcb": evidence["served_beta_lcb"],
        "beta_ucb": evidence["beta_ucb"], "intercept": evidence["intercept"],
        "gamma": evidence["gamma"], "market_centre": centre,
        "row_count": len(rows), "day_count": len(days),
        "accumulating": bool(accumulating),
    }
    return {
        "stage_scope": stage_scope, "lead_band": band,
        "table": table, "evidence": evidence,
        "content_sha256": content_sha256(table),
        "accumulating": bool(accumulating),
        "row_count": len(rows), "day_count": len(days),
    }


def fit_stage_and_bands(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS,
    draws: int = BOOTSTRAP_DRAWS,
    anchored: bool = True,
    cutoff_utc: str | None = None,
) -> list[dict[str, Any]]:
    """The stage's whole fit first, then one fit per lead band, all cut from
    one population.  Bands with no clocked rows are still fitted (as
    accumulating) so the record shows the band exists and is empty."""

    rows, meta = population(
        conn, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
        anchored=anchored, cutoff_utc=cutoff_utc,
    )
    fits = [fit_beta(
        conn, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
        draws=draws, anchored=anchored, cutoff_utc=cutoff_utc,
        rows=rows, meta=meta,
    )]
    if meta.get("error"):
        return fits
    for label in LEAD_BAND_LABELS:
        fits.append(fit_beta(
            conn, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
            draws=draws, anchored=anchored, cutoff_utc=cutoff_utc,
            lead_band=label, rows=rows, meta=meta,
        ))
    return fits


def persist_and_activate(
    conn: sqlite3.Connection, fit: Mapping[str, Any], *, now: str
) -> dict[str, Any]:
    """Persist content-addressed and activate; the previous active row for
    the scope AND band is deactivated.  Shadow lane: no promotion guard --
    the guard is the ruling that turns the mode on."""

    ensure_schema(conn)
    table = fit["table"]
    sha = str(fit["content_sha256"])
    scope = str(fit["stage_scope"])
    band = str(table.get("lead_band") or WHOLE_STAGE)
    conn.execute(
        "UPDATE calibration_beta_edge_table SET activated=0"
        " WHERE stage_scope=? AND lead_band=? AND activated=1 AND beta_sha256<>?",
        (scope, band, sha),
    )
    conn.execute(
        "INSERT OR IGNORE INTO calibration_beta_edge_table("
        " beta_sha256, stage_scope, price_basis, window_days, as_of_event_date,"
        " beta, beta_lcb, beta_ucb, intercept, row_count, day_count,"
        " accumulating, evidence_json, fitted_at_utc, activated, activated_at_utc,"
        " lead_band"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (
            sha, scope, str(table["price_basis"]), int(table["window_days"]),
            str(table.get("as_of_event_date") or ""), table.get("beta"),
            float(table["beta_lcb"]), table.get("beta_ucb"), table.get("intercept"),
            int(table["row_count"]), int(table["day_count"]),
            1 if table["accumulating"] else 0,
            json.dumps(fit["evidence"], ensure_ascii=False, sort_keys=True),
            str(now), str(now), band,
        ),
    )
    conn.execute(
        "UPDATE calibration_beta_edge_table SET activated=1, activated_at_utc=?"
        " WHERE beta_sha256=?",
        (str(now), sha),
    )
    conn.commit()
    return {"beta_sha256": sha, "activated_at_utc": str(now)}


def journal_fit(
    conn: sqlite3.Connection, *, fit: Mapping[str, Any], outcome: str,
    fitted_at_utc: str,
) -> int:
    ensure_schema(conn)
    cur = conn.execute(
        "INSERT INTO calibration_beta_edge_fit_journal("
        " beta_sha256, stage_scope, fitted_at_utc, outcome, evidence_json"
        ") VALUES(?,?,?,?,?)",
        (
            str(fit["content_sha256"]), str(fit["stage_scope"]), str(fitted_at_utc),
            str(outcome), json.dumps(fit["evidence"], ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _row_to_served(row: Sequence[Any]) -> dict[str, Any]:
    sha, beta, lcb, ucb, intercept, accumulating, rows_, days_ = row[:8]
    # v3 reads triples; a v3-schema row's pairs read gamma = 0, which is the
    # prediction that row was fitted for.
    draws: list[tuple[float, float, float]] = []
    gamma = gamma_lcb = gamma_ucb = None
    centre = None
    if len(row) > 8 and row[8]:
        try:
            evidence = json.loads(str(row[8]))
            raw = evidence.get("joint_draws") or []
            draws = [
                (float(d[0]), float(d[1]), float(d[2]) if len(d) == 3 else 0.0)
                for d in raw
                if isinstance(d, (list, tuple)) and len(d) in (2, 3)
            ]
            gamma = evidence.get("gamma")
            gamma_lcb = evidence.get("gamma_lcb")
            gamma_ucb = evidence.get("gamma_ucb")
            centre = evidence.get("market_centre")
        except (ValueError, TypeError, AttributeError):
            draws = []
    return {
        "sha256": str(sha), "beta": beta, "beta_lcb": float(lcb),
        "beta_ucb": ucb, "intercept": intercept,
        "gamma": gamma, "gamma_lcb": gamma_lcb, "gamma_ucb": gamma_ucb,
        "market_centre": centre,
        "accumulating": bool(accumulating), "row_count": int(rows_ or 0),
        "day_count": int(days_ or 0),
        "joint_draws": tuple(draws),
        "lead_band": str(row[9]) if len(row) > 9 and row[9] is not None else WHOLE_STAGE,
        "price_basis": PRICE_BASIS,
    }


def load_beta_as_of(
    conn: sqlite3.Connection, *, stage_scope: str, as_of_utc: str | None = None,
    lead_band: str = WHOLE_STAGE,
) -> dict[str, Any] | None:
    """What the scope (and band) served at ``as_of_utc`` (the active row
    when None), read off the journal like lambda so a promotion never
    rewrites the book behind it.  None before the first fit -- and None for
    a row fitted on another price basis: a v1 (ask-basis) row is not a
    belief on this basis and is never served in its place."""

    if not _has_table(conn, "calibration_beta_edge_table"):
        return None
    if not _has_column(conn, "calibration_beta_edge_table", "lead_band"):
        return None
    cols = (
        "t.beta_sha256, t.beta, t.beta_lcb, t.beta_ucb, t.intercept,"
        " t.accumulating, t.row_count, t.day_count, t.evidence_json, t.lead_band"
    )
    try:
        if as_of_utc is None:
            row = conn.execute(
                f"SELECT {cols} FROM calibration_beta_edge_table t"
                " WHERE t.stage_scope=? AND t.lead_band=? AND t.price_basis=?"
                "   AND t.activated=1"
                " ORDER BY t.activated_at_utc DESC LIMIT 1",
                (str(stage_scope), str(lead_band), PRICE_BASIS),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT {cols} FROM calibration_beta_edge_fit_journal j"
                " JOIN calibration_beta_edge_table t ON t.beta_sha256=j.beta_sha256"
                " WHERE j.stage_scope=? AND t.lead_band=? AND t.price_basis=?"
                "   AND j.fitted_at_utc<=?"
                " ORDER BY j.fitted_at_utc DESC, j.fit_id DESC LIMIT 1",
                (str(stage_scope), str(lead_band), PRICE_BASIS, str(as_of_utc)),
            ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else _row_to_served(row)


def load_beta_tables(
    conn: sqlite3.Connection, *, as_of_utc: str | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Per served stage, what the enumeration reads under ``BETA_TABLE_KEY``:
    the stage's whole row with its band rows under ``lead_bands``.

    A stage with band rows but no whole row serves an accumulating whole
    (``beta_lcb`` 0) so the enumeration can still reach the bands; a stage
    with neither is None, which is what every day before the first fit reads.
    """

    out: dict[str, dict[str, Any] | None] = {}
    for scope in STAGE_SCOPES:
        whole = load_beta_as_of(conn, stage_scope=scope, as_of_utc=as_of_utc)
        bands: dict[str, dict[str, Any]] = {}
        for label in LEAD_BAND_LABELS:
            row = load_beta_as_of(
                conn, stage_scope=scope, as_of_utc=as_of_utc, lead_band=label,
            )
            if row is not None:
                bands[label] = row
        if whole is None and not bands:
            out[scope] = None
            continue
        served = dict(whole) if whole is not None else {
            "sha256": None, "beta": None, "beta_lcb": 0.0, "beta_ucb": None,
            "intercept": None, "gamma": None, "gamma_lcb": None,
            "gamma_ucb": None, "market_centre": None,
            "accumulating": True, "row_count": 0,
            "day_count": 0, "joint_draws": (), "lead_band": WHOLE_STAGE,
            "price_basis": PRICE_BASIS,
        }
        served["lead_bands"] = bands
        out[scope] = served
    return out


__all__ = [
    "BETA_TABLE_KEY", "SCHEMA", "STAGE_SCOPES", "WINDOW_DAYS", "MIN_ROWS",
    "MIN_DAYS", "LEAD_BUCKETS", "LEAD_BAND_LABELS", "WHOLE_STAGE", "PRICE_BASIS",
    "ensure_schema", "population", "fit_beta", "fit_stage_and_bands",
    "layered_readings",
    "persist_and_activate", "journal_fit", "load_beta_as_of", "load_beta_tables",
]
