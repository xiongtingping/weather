"""DB-resident beta: how much of the model's disagreement with the market
arrives, one number per served stage.

Charter 128 (user ruling, 2026-09-12): the third cut of the claim-chain
clean-up.  Charter 68's lambda(a) table answered "what fraction of the claim
arrives" with seven free numbers per stage keyed on ``price / claim``; on
the journal's own record every one of those tables, applied to the 1-3 days
after it was fitted, admitted windows that then realised the same
``hit - price`` as the ones it refused (P0 -0.063 against -0.062 over 4,894
window-days).  The fraction of the claim that arrives is not a function of
the agreement band; it is one slope.

The identity this lane estimates, per enumerated window::

    hit - q  =  a  +  beta * (p - q)

with ``p`` the raw served claim (the probability layer's own number, no
position multiplier), ``q`` the all-in price the gate priced the window at
and ``hit`` whether the truth landed in it.  ``beta`` is the share of the
claimed disagreement that shows up in settled outcomes -- 0 means the
market's price is the better estimate wherever the two differ, 1 means the
claim is right about all of it, negative means the disagreement leans the
wrong way -- and ``a`` is what a window realises when the two agree, which
is the cost.  Trading on the disagreement at all is worth
``beta * (p - q)`` per unit; the served belief is therefore
``q + beta_lcb * (p - q)`` and the whole-basket positive-EV gate reads
``beta_lcb * (p - q) > 0``.  One parameter, one lower bound, no bands.

The fit is walk-forward by construction -- as-of a day it reads only rows
whose event date is at or before that day -- and pooled over stations, with
the event date as the independent unit: a fleet shares its weather, so the
interval is a by-day bootstrap and the served number is its lower bound.
Below the hurdles a stage serves ``beta_lcb = 0`` and says so: the belief
collapses onto the price and the gate refuses, which is the honest reading
of "no evidence yet".

The sample is bounded below at the stage's serving-arithmetic anchor, like
every claim lane: rows from before the served distribution changed shape
describe a claim nobody serves.

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

from .basket_kernel.delta_v2 import BETA_TABLE_KEY
from .contracts import content_sha256

SCHEMA = "beta_edge_table_v2"
STAGE_SCOPES: dict[str, tuple[str, ...]] = {
    "P0": ("P0",), "P0-M": ("P0-M",), "P1": ("P1",),
}
WINDOW_DAYS = 28
MIN_ROWS = 60
MIN_DAYS = 8
BOOTSTRAP_DRAWS = 400
BOOTSTRAP_SEED = 20260912
LCB_QUANTILE = 0.05
PRICE_BASIS = "ledger_all_in_price"
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
  activated_at_utc TEXT
)
"""
_ACTIVE_INDEX = (
    "CREATE INDEX IF NOT EXISTS calibration_beta_edge_active_idx"
    " ON calibration_beta_edge_table(stage_scope, activated)"
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
    conn.execute(_ACTIVE_INDEX)
    conn.execute(_JOURNAL_DDL)
    conn.execute(_JOURNAL_INDEX)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
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
    stages that a fit at ``cutoff_utc`` could have known about.

    Charter 132 (audit items 47 and 48) makes three conditions explicit that
    the event-date bound used to stand in for:

    * **The label was knowable.**  ``event_date <= as_of`` says the day is
      over; it does not say the truth had been published.  A row is read only
      when ``settled_at_utc`` is at or before the cutoff, and a decision taken
      after the cutoff is not a row the fit could have seen either.  A table
      without the column cannot state when its labels became knowable and is
      refused rather than trusted.
    * **The label is a label.**  ``hit`` must be 0 or 1.  ``hit or 0`` used to
      turn a missing label into a loss; such rows are now counted and dropped.
    * **The window is today's population.**  The same seat-shape partition
      lambda's fit applies: rows enumerated under another shape rule, or before
      the rule recorded one, are not evidence about windows shaped today.  A
      table without the column partitions on nothing, exactly as lambda's does.

    ``cutoff_utc`` defaults to now, which is what the nightly fit means.  A
    walk-forward caller passes the moment it is standing at.
    """

    if stage_scope not in STAGE_SCOPES:
        raise ValueError(f"beta_edge_scope_invalid:{stage_scope}")
    if not _has_table(conn, "paper_trade_candidate_window"):
        return [], {"error": "no_candidate_windows"}
    if not _has_column(conn, "paper_trade_candidate_window", "settled_at_utc"):
        return [], {"error": "no_label_knowable_column"}
    stages = STAGE_SCOPES[stage_scope]
    cutoff = str(cutoff_utc or _utc_now_iso())
    newest = conn.execute(
        "SELECT MAX(event_date) FROM paper_trade_candidate_window"
        " WHERE settled_bucket IS NOT NULL AND settled_at_utc IS NOT NULL"
        "   AND settled_at_utc <= ?",
        (cutoff,),
    ).fetchone()
    latest = str(as_of or (newest[0] if newest else "") or "")
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
    if _has_column(conn, "paper_trade_candidate_window", "seat_shape_rule"):
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
        "       raw_win_probability, all_in_price, hit, settled_at_utc"
        + (", seat_shape_rule" if shape is not None else ", NULL")
        + " FROM paper_trade_candidate_window" + base_where,
        base_args,
    ).fetchall()
    out = []
    counts = {
        "excluded_before_anchor": 0,
        "excluded_label_not_yet_knowable": 0,
        "excluded_decided_after_cutoff": 0,
        "rejected_invalid_label": 0,
        "excluded_other_shape": 0,
    }
    for station, day, stage, at, raw, price, hit, settled_at, row_shape in rows:
        if shape is not None and (row_shape is None or str(row_shape) != shape):
            counts["excluded_other_shape"] += 1
            continue
        if settled_at is None or str(settled_at) > cutoff:
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
        out.append({
            "station": str(station), "date": str(day), "stage": str(stage),
            "at": str(at or ""),
            "x": float(raw) - float(price),
            "y": float(hit) - float(price),
            "raw": float(raw), "price": float(price), "hit": float(hit),
        })
    return out, {
        "as_of_event_date": latest, "floor_day": floor_day,
        "cutoff_utc": cutoff, "anchor_utc": anchor_utc,
        "anchor_states": anchor_states,
        "anchors_after_cutoff": anchors_after_cutoff,
        "anchored": bool(anchored), "seat_shape_rule": shape,
        **counts,
    }


LAYER_MIN_ROWS = 12
LEAD_BUCKETS = ((0.0, 12.0, "<12h"), (12.0, 24.0, "12-24h"), (24.0, 36.0, "24-36h"), (36.0, 1e9, ">=36h"))


def _lead_hours(row: Mapping[str, Any]) -> float | None:
    """Hours from the decision to the event day's noon, UTC.  A coarse lead
    -- the ledger's rows carry no local clock -- but the same coarseness for
    every row, which is all a bucket needs."""

    try:
        from datetime import datetime, timezone  # noqa: PLC0415

        at = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
        noon = datetime.fromisoformat(str(row["date"]) + "T12:00:00+00:00")
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return (noon - at).total_seconds() / 3600.0
    except (ValueError, KeyError, TypeError):
        return None


def layered_readings(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Point-estimate betas by station and by lead bucket -- research
    readings for the shadow, never served: a layer is a candidate for its
    own beta only once it clears the hurdles on its own days."""

    by_station: dict[str, list[Mapping[str, Any]]] = {}
    by_lead: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        by_station.setdefault(str(r["station"]), []).append(r)
        lead = _lead_hours(r)
        if lead is None:
            continue
        for lo, hi, label in LEAD_BUCKETS:
            if lo <= lead < hi:
                by_lead.setdefault(label, []).append(r)
                break

    def cell(group: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        if len(group) < LAYER_MIN_ROWS:
            return None
        got = _ols([r["x"] for r in group], [r["y"] for r in group])
        if got is None:
            return None
        return {
            "rows": len(group), "days": len({r["date"] for r in group}),
            "beta": round(got[1], 4), "intercept": round(got[0], 4),
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
) -> dict[str, Any]:
    """One beta with a by-day bootstrap lower bound; below the hurdles the
    served bound is 0 and the fit says ``accumulating``.  ``anchored=False``
    is a research reading (the anchor is what keeps a served fit off rows
    from a shape nobody serves) and is never persisted.

    Charter 132 (audit items 42 and 44): the bootstrap keeps the JOINT draws
    of ``(intercept, beta)``, because the served quantity is the prediction
    ``intercept + beta * d`` and not beta alone.  The two are estimated from
    one sample and are correlated; a lower bound on beta times ``d`` drops the
    intercept (which on this lane is the spread, about -0.04) and flips its
    conservatism whenever ``d`` is negative.  ``beta_lcb`` stays on the table
    for charter 128's shadow and its record; the new shadow reads the draws.
    """

    rows, meta = population(
        conn, stage_scope=stage_scope, as_of=as_of, window_days=window_days,
        anchored=anchored, cutoff_utc=cutoff_utc,
    )
    days = sorted({r["date"] for r in rows})
    evidence: dict[str, Any] = {
        "schema": SCHEMA, "stage_scope": stage_scope, "price_basis": PRICE_BASIS,
        "window_days": int(window_days), "row_count": len(rows),
        "day_count": len(days), "min_rows": MIN_ROWS, "min_days": MIN_DAYS,
        **meta,
    }
    accumulating = len(rows) < MIN_ROWS or len(days) < MIN_DAYS
    beta = intercept = lcb = ucb = None
    intercept_lcb = intercept_ucb = None
    joint: list[tuple[float, float]] = []
    per_day: dict[str, float] = {}
    if not accumulating:
        fitted = _ols([r["x"] for r in rows], [r["y"] for r in rows])
        if fitted is None:
            accumulating = True
        else:
            intercept, beta = fitted
            by_day: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                by_day.setdefault(r["date"], []).append(r)
            rng = random.Random(BOOTSTRAP_SEED)
            keys = list(by_day)
            samples: list[float] = []
            for _ in range(int(draws)):
                picked = [by_day[k] for k in rng.choices(keys, k=len(keys))]
                xs = [r["x"] for grp in picked for r in grp]
                ys = [r["y"] for grp in picked for r in grp]
                got = _ols(xs, ys)
                if got is not None:
                    samples.append(got[1])
                    joint.append((
                        round(float(got[0]), JOINT_DRAW_DECIMALS),
                        round(float(got[1]), JOINT_DRAW_DECIMALS),
                    ))
            samples.sort()
            if samples:
                lcb = samples[min(len(samples) - 1, int(LCB_QUANTILE * len(samples)))]
                ucb = samples[max(0, int((1.0 - LCB_QUANTILE) * len(samples)) - 1)]
            if joint:
                intercepts = sorted(a for a, _b in joint)
                intercept_lcb = intercepts[
                    min(len(intercepts) - 1, int(LCB_QUANTILE * len(intercepts)))
                ]
                intercept_ucb = intercepts[
                    max(0, int((1.0 - LCB_QUANTILE) * len(intercepts)) - 1)
                ]
            for k, grp in by_day.items():
                got = _ols([r["x"] for r in grp], [r["y"] for r in grp])
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
        # Empty while accumulating: an accumulating fit serves nothing, and
        # a served lower bound read off draws from too few days would be the
        # hurdle waived by the back door.
        "joint_draws": [] if accumulating else [list(d) for d in joint],
        "per_day_beta": per_day,
        "days_beta_positive": sum(1 for v in per_day.values() if v > 0),
        "mean_x": round(mean(r["x"] for r in rows), 6) if rows else None,
        "mean_y": round(mean(r["y"] for r in rows), 6) if rows else None,
        # Research readings by layer, recorded so a layer that earns its own
        # beta can be seen coming; nothing serves them.
        "layers": layered_readings(rows),
    })
    table = {
        "schema": SCHEMA, "stage_scope": stage_scope, "price_basis": PRICE_BASIS,
        "window_days": int(window_days), "as_of_event_date": meta.get("as_of_event_date"),
        "beta": evidence["beta"], "beta_lcb": evidence["served_beta_lcb"],
        "beta_ucb": evidence["beta_ucb"], "intercept": evidence["intercept"],
        "row_count": len(rows), "day_count": len(days),
        "accumulating": bool(accumulating),
    }
    return {
        "stage_scope": stage_scope, "table": table, "evidence": evidence,
        "content_sha256": content_sha256(table),
        "accumulating": bool(accumulating),
        "row_count": len(rows), "day_count": len(days),
    }


def persist_and_activate(
    conn: sqlite3.Connection, fit: Mapping[str, Any], *, now: str
) -> dict[str, Any]:
    """Persist content-addressed and activate; the previous active row for
    the scope is deactivated.  Shadow lane: no promotion guard -- the guard
    is the ruling that turns the mode on."""

    ensure_schema(conn)
    table = fit["table"]
    sha = str(fit["content_sha256"])
    scope = str(fit["stage_scope"])
    conn.execute(
        "UPDATE calibration_beta_edge_table SET activated=0"
        " WHERE stage_scope=? AND activated=1 AND beta_sha256<>?",
        (scope, sha),
    )
    conn.execute(
        "INSERT OR IGNORE INTO calibration_beta_edge_table("
        " beta_sha256, stage_scope, price_basis, window_days, as_of_event_date,"
        " beta, beta_lcb, beta_ucb, intercept, row_count, day_count,"
        " accumulating, evidence_json, fitted_at_utc, activated, activated_at_utc"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (
            sha, scope, str(table["price_basis"]), int(table["window_days"]),
            str(table.get("as_of_event_date") or ""), table.get("beta"),
            float(table["beta_lcb"]), table.get("beta_ucb"), table.get("intercept"),
            int(table["row_count"]), int(table["day_count"]),
            1 if table["accumulating"] else 0,
            json.dumps(fit["evidence"], ensure_ascii=False, sort_keys=True),
            str(now), str(now),
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
    draws: list[tuple[float, float]] = []
    if len(row) > 8 and row[8]:
        try:
            raw = json.loads(str(row[8])).get("joint_draws") or []
            draws = [
                (float(pair[0]), float(pair[1]))
                for pair in raw
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            ]
        except (ValueError, TypeError, AttributeError):
            draws = []
    return {
        "sha256": str(sha), "beta": beta, "beta_lcb": float(lcb),
        "beta_ucb": ucb, "intercept": intercept,
        "accumulating": bool(accumulating), "row_count": int(rows_ or 0),
        "day_count": int(days_ or 0),
        "joint_draws": tuple(draws),
    }


def load_beta_as_of(
    conn: sqlite3.Connection, *, stage_scope: str, as_of_utc: str | None = None,
) -> dict[str, Any] | None:
    """What the scope served at ``as_of_utc`` (the active row when None),
    read off the journal like lambda so a promotion never rewrites the book
    behind it.  None before the first fit."""

    if not _has_table(conn, "calibration_beta_edge_table"):
        return None
    cols = (
        "t.beta_sha256, t.beta, t.beta_lcb, t.beta_ucb, t.intercept,"
        " t.accumulating, t.row_count, t.day_count, t.evidence_json"
    )
    try:
        if as_of_utc is None:
            row = conn.execute(
                f"SELECT {cols} FROM calibration_beta_edge_table t"
                " WHERE t.stage_scope=? AND t.activated=1"
                " ORDER BY t.activated_at_utc DESC LIMIT 1",
                (str(stage_scope),),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT {cols} FROM calibration_beta_edge_fit_journal j"
                " JOIN calibration_beta_edge_table t ON t.beta_sha256=j.beta_sha256"
                " WHERE j.stage_scope=? AND j.fitted_at_utc<=?"
                " ORDER BY j.fitted_at_utc DESC, j.fit_id DESC LIMIT 1",
                (str(stage_scope), str(as_of_utc)),
            ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else _row_to_served(row)


def load_beta_tables(
    conn: sqlite3.Connection, *, as_of_utc: str | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Per served stage, what the enumeration reads under ``BETA_TABLE_KEY``."""

    return {
        scope: load_beta_as_of(conn, stage_scope=scope, as_of_utc=as_of_utc)
        for scope in STAGE_SCOPES
    }


__all__ = [
    "BETA_TABLE_KEY", "SCHEMA", "STAGE_SCOPES", "WINDOW_DAYS", "MIN_ROWS",
    "MIN_DAYS", "LEAD_BUCKETS", "ensure_schema", "population", "fit_beta",
    "layered_readings",
    "persist_and_activate", "journal_fit", "load_beta_as_of", "load_beta_tables",
]
