"""Charter 132: one valuation centre and one uncertainty set per snapshot.

The 2026-09-13 design review (docs/research/EDGE_REALIZATION_OPTIMIZATION_
2026-09-13.md) replaced "a claim times a stack of multipliers" with "two
sources, one fusion".  The market's book is not only a price to pay; it is a
second forecast, and the question worth a parameter is how much the weather
model adds on top of it.  So:

* ``p_weather`` is the stage's served probability vector, read off the
  committed envelope and never changed here.
* ``m_market`` is a reference probability vector built from the whole
  ladder's official WS book (``market_reference_v1``).  An estimate, not a
  price: normalised mids, with every one-sided or empty bucket stated.
* ``p_value = (1 - beta) * m_market + beta * p_weather`` with beta in [0, 1],
  fitted per stage on the bucket the truth landed in (``fit_valuation``).
  One vector per snapshot; every window's probability is a sum over it, so a
  window can never be worth more than a window that contains it.
* Conservatism lives on the ACTION, not on the probability: the fit keeps
  by-event-day bootstrap draws of beta, each draw is a whole vector, and an
  action's lower bound is a quantile of its value across those vectors
  (``action_value_v1``).  There is no single "more conservative" vector that
  still sums to one, and looking for one is what the multiplier stack was.
* Cost is the action's, walked down the ask ladder at the shares it would
  actually buy (``ladder_cost_v1``); it never moves a belief.

This module is the valuation lane only.  It does not write weather facts,
does not refit anything on the hot path -- the runtime reads a materialised
row -- and serves nothing on its own: ``VALUATION_MODE`` in the kernel
decides whether the gate reads it, and promotion additionally needs a
walk-forward verdict (``calibration_valuation_verdict``) for the stage.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .basket_kernel.valuation_v1 import (
    LCB_QUANTILE,
    MARKET_REFERENCE_RULE,
    MAX_BOOK_SKEW_SECONDS,
    NEAR_CERTAIN_MIN_BID,
    ONE_SIDED_TAIL_MAX_ASK,
    VALUATION_RULE,
    VALUATION_TABLE_KEY,
    action_value_v1,
    beta_draw_vectors,
    capacity_v1,
    ladder_cost_v1,
    market_reference_v1,
    valuation_vector_v1,
    window_mass,
)
from .contracts import content_sha256

SCHEMA = "valuation_table_v1"
VERDICT_SCHEMA = "valuation_verdict_v1"
STAGE_SCOPES: dict[str, tuple[str, ...]] = {
    "P0": ("P0",), "P0-M": ("P0-M",), "P1": ("P1",),
}
WINDOW_DAYS = 28
# Hurdles in station-days and event days, not rows: a station-day's decisions
# settle against one high and are weighted to count once.
MIN_STATION_DAYS = 60
MIN_DAYS = 8
BOOTSTRAP_DRAWS = 200
BOOTSTRAP_SEED = 20260913
BETA_GRID: tuple[float, ...] = tuple(i / 100.0 for i in range(101))
LOG_FLOOR = 1e-4
_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS calibration_valuation_table (
  valuation_sha256 TEXT PRIMARY KEY,
  stage_scope TEXT NOT NULL,
  rule TEXT NOT NULL,
  market_reference_rule TEXT NOT NULL,
  window_days INTEGER NOT NULL,
  as_of_event_date TEXT NOT NULL,
  fit_cutoff_utc TEXT NOT NULL,
  beta REAL,
  beta_lcb REAL,
  beta_ucb REAL,
  station_day_count INTEGER NOT NULL,
  day_count INTEGER NOT NULL,
  accumulating INTEGER NOT NULL CHECK(accumulating IN (0,1)),
  evidence_json TEXT NOT NULL,
  fitted_at_utc TEXT NOT NULL,
  activated INTEGER NOT NULL CHECK(activated IN (0,1)),
  activated_at_utc TEXT
)
"""
_TABLE_INDEX = (
    "CREATE INDEX IF NOT EXISTS calibration_valuation_active_idx"
    " ON calibration_valuation_table(stage_scope, activated)"
)
_JOURNAL_DDL = """
CREATE TABLE IF NOT EXISTS calibration_valuation_fit_journal (
  fit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  valuation_sha256 TEXT NOT NULL,
  stage_scope TEXT NOT NULL,
  fitted_at_utc TEXT NOT NULL,
  outcome TEXT NOT NULL,
  evidence_json TEXT NOT NULL
)
"""
_JOURNAL_INDEX = (
    "CREATE INDEX IF NOT EXISTS calibration_valuation_journal_idx"
    " ON calibration_valuation_fit_journal(stage_scope, fitted_at_utc, fit_id)"
)
# The walk-forward verdict a promotion reads.  Written by
# scripts/valuation_four_arm.py; one active row per stage.
_VERDICT_DDL = """
CREATE TABLE IF NOT EXISTS calibration_valuation_verdict (
  verdict_sha256 TEXT PRIMARY KEY,
  stage_scope TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN ('admitted','hold')),
  reason TEXT NOT NULL,
  first_test_day TEXT,
  last_test_day TEXT,
  evidence_json TEXT NOT NULL,
  computed_at_utc TEXT NOT NULL,
  activated INTEGER NOT NULL CHECK(activated IN (0,1))
)
"""
_VERDICT_INDEX = (
    "CREATE INDEX IF NOT EXISTS calibration_valuation_verdict_active_idx"
    " ON calibration_valuation_verdict(stage_scope, activated)"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_DDL)
    conn.execute(_TABLE_INDEX)
    conn.execute(_JOURNAL_DDL)
    conn.execute(_JOURNAL_INDEX)
    conn.execute(_VERDICT_DDL)
    conn.execute(_VERDICT_INDEX)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Fit
# --------------------------------------------------------------------------

def _instant(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _anchor(conn: sqlite3.Connection, stage: str) -> tuple[str | None, str]:
    """The stage's serving-arithmetic anchor and how it was read.

    The same three states the beta lane reads (charter 132, audit item 48):
    ``absent`` when the store that keeps the anchor is not in this database
    (an extract, a fixture) and there is no bound; ``ok``, possibly None; and
    ``error``, a present store that failed, which refuses the fit rather than
    widening it onto a distribution nobody serves.
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


def population(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS,
    cutoff_utc: str | None = None,
    anchored: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every decision of the scope's stages a fit at ``cutoff_utc`` could use.

    One row per decision: the committed envelope's vector, the market
    reference built from the WS snapshot the decision priced against, and the
    bucket the truth landed in -- read off the ledger's settlement, with the
    moment it became knowable.  The same strict-as-of contract the beta and
    lambda lanes carry (charter 132): the label must have been knowable at the
    cutoff, and a decision taken after the cutoff is not evidence either.

    And the floor they carry (2026-09-14): a decision taken before the stage's
    serving-arithmetic anchor priced a weather vector nobody serves any more,
    so it is not evidence about how much weight today's vector earns beside
    the market.  This lane shipped without it and pooled 28 days across P0's
    v4 (2026-09-10) and charter 134's stable pool (v5, P1 v10) -- the one
    change this lane exists to judge, since only a better probability layer
    can move its beta off zero.  ``anchored=False`` is a research reading and
    is never persisted.  An anchor set after the cutoff did not exist then
    and is dropped, as on the beta and lambda lanes.
    """

    if stage_scope not in STAGE_SCOPES:
        raise ValueError(f"valuation_scope_invalid:{stage_scope}")
    from .candidate_window_evidence import (  # noqa: PLC0415
        EVIDENCE_SOURCE,
        LABEL_TABLE,
    )

    # Charter 140: the labels come from the truth lane's own table on
    # production evidence; only the ledger arm still needs the paper table.
    needed = (
        "basket_policy_training_fact", "basket_probability_envelope",
        "basket_official_ws_snapshot",
        LABEL_TABLE if EVIDENCE_SOURCE == "production"
        else "paper_trade_candidate_window",
    )
    for table in needed:
        if not _has_table(conn, table):
            return [], {"error": f"missing_table:{table}"}
    cutoff = str(cutoff_utc or _utc_now_iso())
    from .label_knowability import knowable_labels  # noqa: PLC0415

    labels: dict[tuple[str, str], tuple[str, str]] = {
        key: (label["bucket"], label["known_at"])
        for key, label in knowable_labels(conn).items()
        if str(label["known_at"]) <= cutoff
    }
    if not labels:
        return [], {"error": "no_knowable_labels", "cutoff_utc": cutoff}
    latest = str(as_of or max(day for _s, day in labels))
    floor_day = (
        date.fromisoformat(latest) - timedelta(days=int(window_days))
    ).isoformat()
    stages = STAGE_SCOPES[stage_scope]
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
                if _instant(value) > _instant(cutoff):
                    anchors_after_cutoff += 1
                else:
                    anchors.append(str(value))
    anchor_utc = max(anchors, key=_instant) if anchors else None
    placeholders = ",".join("?" for _ in stages)
    rows: list[dict[str, Any]] = []
    counts = {
        "decisions_read": 0, "no_label": 0, "decided_after_cutoff": 0,
        "excluded_before_anchor": 0,
        "market_incomplete": 0, "label_outside_contract": 0,
        "weather_vector_invalid": 0,
    }
    reasons: dict[str, int] = {}
    if anchor_utc is not None:
        # Counted, not hidden: the seek below never returns these rows, so
        # the count is taken here, over the same stages and event window.
        counts["excluded_before_anchor"] = int(conn.execute(
            "SELECT COUNT(*) FROM basket_policy_training_fact f"
            f" WHERE f.stage IN ({placeholders})"
            "   AND f.event_date > ? AND f.event_date <= ?"
            "   AND f.decision_at_utc < ?",
            (*stages, floor_day, latest, str(anchor_utc)[:19]),
        ).fetchone()[0] or 0)
    # The envelope's settlement clock rides along for the posterior lane's
    # lead bands; a fixture without the column reads None and has no band.
    try:
        has_tz = any(
            str(col[1]) == "settlement_timezone"
            for col in conn.execute("PRAGMA table_info(basket_probability_envelope)")
        )
    except sqlite3.Error:
        has_tz = False
    tz_col = "e.settlement_timezone" if has_tz else "NULL"
    cursor = conn.execute(
        "SELECT f.station_id, f.event_date, f.stage, f.decision_at_utc,"
        f"       e.probabilities_json, s.buckets_json, {tz_col}"
        " FROM basket_policy_training_fact f"
        " JOIN basket_probability_envelope e"
        "   ON e.envelope_id = f.probability_envelope_id"
        " JOIN basket_official_ws_snapshot s ON s.snapshot_id = f.ws_snapshot_id"
        f" WHERE f.stage IN ({placeholders})"
        "   AND f.event_date > ? AND f.event_date <= ?"
        # A seekable superset of the anchor (whole seconds, no zone), so the
        # exact instant test below never parses rows it would drop.
        + ("" if anchor_utc is None else "   AND f.decision_at_utc >= ?"),
        (*stages, floor_day, latest,
         *(() if anchor_utc is None else (str(anchor_utc)[:19],))),
    )
    for station, day, stage, decided_at, probs_json, book_json, tz in cursor:
        counts["decisions_read"] += 1
        labelled = labels.get((str(station), str(day)))
        if labelled is None:
            counts["no_label"] += 1
            continue
        label, label_known_at = labelled
        if str(decided_at) > cutoff:
            counts["decided_after_cutoff"] += 1
            continue
        if anchor_utc is not None and _instant(decided_at) < _instant(anchor_utc):
            counts["excluded_before_anchor"] += 1
            continue
        try:
            p_weather = {
                str(k): float(v) for k, v in json.loads(probs_json or "{}").items()
            }
            book = json.loads(book_json or "[]")
        except (TypeError, ValueError):
            counts["weather_vector_invalid"] += 1
            continue
        total_p = sum(p_weather.values())
        if not (total_p > 0.0 and math.isfinite(total_p)):
            counts["weather_vector_invalid"] += 1
            continue
        p_weather = {k: v / total_p for k, v in p_weather.items()}
        market = market_reference_v1(book if isinstance(book, list) else [])
        if market["state"] != "complete":
            counts["market_incomplete"] += 1
            key = str(market.get("reason") or "").split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
            continue
        m = market["vector"]
        if label not in m:
            counts["label_outside_contract"] += 1
            continue
        rows.append({
            "station": str(station), "date": str(day), "stage": str(stage),
            "at": str(decided_at), "label": label,
            # When the label became knowable, so a walk-forward caller that
            # cut the population once can cut each fold again in memory.
            "label_known_at": label_known_at,
            "m_label": float(m[label]),
            "p_label": float(p_weather.get(label, 0.0)),
            "brier_m": _brier(m, label),
            "brier_p": _brier_on(p_weather, m, label),
            "m": m, "p": p_weather,
            # The station's settlement clock, so the posterior lane can put
            # the decision in its lead band without a second scan.
            "settlement_timezone": None if tz is None else str(tz),
            # Per bucket, on the vector's scale: what crossing the book costs
            # from the mid.  The charter EV page prices the disagreement
            # strategy's spread cost from it; the fit does not read it.
            "half_spread": dict(market.get("half_spread") or {}),
        })
    # Each station-day counts once, however many decisions it carried.
    per_station_day: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row["station"], row["date"])
        per_station_day[key] = per_station_day.get(key, 0) + 1
    for row in rows:
        row["weight"] = 1.0 / per_station_day[(row["station"], row["date"])]
    return rows, {
        "as_of_event_date": latest, "floor_day": floor_day,
        "cutoff_utc": cutoff, "market_incomplete_reasons": reasons,
        "anchored": bool(anchored), "anchor_utc": anchor_utc,
        "anchor_states": anchor_states,
        "anchors_after_cutoff": anchors_after_cutoff, **counts,
    }


def _brier(vector: Mapping[str, float], label: str) -> float:
    return sum(
        (float(v) - (1.0 if str(b) == label else 0.0)) ** 2
        for b, v in vector.items()
    )


def _brier_on(
    vector: Mapping[str, float], contract: Mapping[str, float], label: str,
) -> float:
    return sum(
        (float(vector.get(b, 0.0) or 0.0) - (1.0 if str(b) == label else 0.0)) ** 2
        for b in contract
    )


def _grid_scores(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[float]]:
    """Per event day, the weighted log score at every grid beta."""

    by_day: dict[str, list[float]] = {}
    for row in rows:
        scores = by_day.setdefault(row["date"], [0.0] * len(BETA_GRID))
        m_y, p_y, w = row["m_label"], row["p_label"], row["weight"]
        for i, beta in enumerate(BETA_GRID):
            scores[i] += w * math.log(max(LOG_FLOOR, (1.0 - beta) * m_y + beta * p_y))
    return by_day


def _geometric_pool_diagnostic(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Best ``m^(1-g) * p^g`` (renormalised) on the grid, by weighted log score.

    g is not bounded to [0, 1] here in principle, but the grid is -- the
    question is only whether a log pool finds weight on the weather model
    where the linear pool finds none.
    """

    totals = [0.0] * len(BETA_GRID)
    weight = 0.0
    for row in rows:
        m, p, label, w = row["m"], row["p"], row["label"], row["weight"]
        weight += w
        logs_m = {b: math.log(max(LOG_FLOOR, float(v))) for b, v in m.items()}
        logs_p = {b: math.log(max(LOG_FLOOR, float(p.get(b, 0.0) or 0.0))) for b in m}
        for i, g in enumerate(BETA_GRID):
            exps = {b: (1.0 - g) * logs_m[b] + g * logs_p[b] for b in m}
            top = max(exps.values())
            norm = top + math.log(sum(math.exp(v - top) for v in exps.values()))
            totals[i] += w * (exps[label] - norm)
    best = _argmax(totals)
    return {
        "g": BETA_GRID[best],
        "log_score": totals[best] / weight if weight else None,
        "log_score_market": totals[0] / weight if weight else None,
    }


def _argmax(scores: Sequence[float]) -> int:
    best = 0
    for i, value in enumerate(scores):
        if value > scores[best] + 1e-12:
            best = i
    return best


def fit_valuation(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
    as_of: str | None = None,
    window_days: int = WINDOW_DAYS,
    cutoff_utc: str | None = None,
    draws: int = BOOTSTRAP_DRAWS,
    rows: Sequence[Mapping[str, Any]] | None = None,
    meta: Mapping[str, Any] | None = None,
    anchored: bool = True,
) -> dict[str, Any]:
    """beta in [0, 1] maximising the log score of the truth bucket.

    The log score is the proper score the pool is judged on; the fit reports
    the market alone (beta 0) and the weather alone (beta 1) beside it, so a
    fitted beta of 0.13 is read against what the two ends score rather than
    as a number on its own.  The bootstrap resamples whole event days: a
    fleet shares its weather.  ``rows``/``meta`` let a walk-forward caller
    hand in a population it has already cut.
    """

    if rows is None:
        rows, meta = population(
            conn, stage_scope=stage_scope, as_of=as_of,
            window_days=window_days, cutoff_utc=cutoff_utc,
            anchored=anchored,
        )
    meta = dict(meta or {})
    station_days = len({(r["station"], r["date"]) for r in rows})
    days = sorted({r["date"] for r in rows})
    accumulating = station_days < MIN_STATION_DAYS or len(days) < MIN_DAYS
    evidence: dict[str, Any] = {
        "schema": SCHEMA, "rule": VALUATION_RULE,
        "market_reference_rule": MARKET_REFERENCE_RULE,
        "stage_scope": stage_scope, "window_days": int(window_days),
        "decision_count": len(rows), "station_day_count": station_days,
        "day_count": len(days), "min_station_days": MIN_STATION_DAYS,
        "min_days": MIN_DAYS, **meta,
    }
    beta = lcb = ucb = None
    beta_draws: list[float] = []
    scores: dict[str, float | None] = {
        "log_market": None, "log_weather": None, "log_pool": None,
        "brier_market": None, "brier_weather": None,
    }
    if rows:
        total_w = sum(r["weight"] for r in rows)
        by_day = _grid_scores(rows)
        pooled = [sum(day[i] for day in by_day.values()) for i in range(len(BETA_GRID))]
        best = _argmax(pooled)
        scores["log_market"] = pooled[0] / total_w
        scores["log_weather"] = pooled[-1] / total_w
        scores["log_pool"] = pooled[best] / total_w
        scores["brier_market"] = sum(r["weight"] * r["brier_m"] for r in rows) / total_w
        scores["brier_weather"] = sum(r["weight"] * r["brier_p"] for r in rows) / total_w
        if not accumulating:
            beta = BETA_GRID[best]
            rng = random.Random(BOOTSTRAP_SEED)
            keys = list(by_day)
            for _ in range(int(draws)):
                picked = rng.choices(keys, k=len(keys))
                summed = [0.0] * len(BETA_GRID)
                for key in picked:
                    day = by_day[key]
                    for i in range(len(BETA_GRID)):
                        summed[i] += day[i]
                beta_draws.append(BETA_GRID[_argmax(summed)])
            ordered = sorted(beta_draws)
            lcb = ordered[min(len(ordered) - 1, int(LCB_QUANTILE * len(ordered)))]
            ucb = ordered[max(0, int((1.0 - LCB_QUANTILE) * len(ordered)) - 1)]
    geometric = _geometric_pool_diagnostic(rows) if rows else None
    evidence.update({
        "accumulating": bool(accumulating),
        # Diagnostic, never served: a linear pool pinned at beta 0 does not by
        # itself say the weather model carries nothing -- a logarithmic pool
        # can extract information a linear one cannot, when the two sources
        # err differently.  Reported so "beta is at the boundary" is read
        # against the other pool form before it is read as a verdict.
        "geometric_pool": geometric,
        "beta": beta, "beta_lcb": lcb, "beta_ucb": ucb,
        "beta_draws": [] if accumulating else beta_draws,
        "scores": scores,
    })
    table = {
        "schema": SCHEMA, "rule": VALUATION_RULE,
        "market_reference_rule": MARKET_REFERENCE_RULE,
        "stage_scope": stage_scope, "window_days": int(window_days),
        "as_of_event_date": meta.get("as_of_event_date"),
        "fit_cutoff_utc": meta.get("cutoff_utc"),
        "beta": beta, "beta_lcb": lcb, "beta_ucb": ucb,
        "station_day_count": station_days, "day_count": len(days),
        "accumulating": bool(accumulating),
        "beta_draws": evidence["beta_draws"],
    }
    return {
        "stage_scope": stage_scope, "table": table, "evidence": evidence,
        "content_sha256": content_sha256(table),
        "accumulating": bool(accumulating),
        "station_day_count": station_days, "day_count": len(days),
    }


def persist_and_activate(
    conn: sqlite3.Connection, fit: Mapping[str, Any], *, now: str,
) -> dict[str, Any]:
    ensure_schema(conn)
    table = fit["table"]
    sha = str(fit["content_sha256"])
    scope = str(fit["stage_scope"])
    conn.execute(
        "UPDATE calibration_valuation_table SET activated=0"
        " WHERE stage_scope=? AND activated=1 AND valuation_sha256<>?",
        (scope, sha),
    )
    conn.execute(
        "INSERT OR IGNORE INTO calibration_valuation_table("
        " valuation_sha256, stage_scope, rule, market_reference_rule,"
        " window_days, as_of_event_date, fit_cutoff_utc, beta, beta_lcb,"
        " beta_ucb, station_day_count, day_count, accumulating, evidence_json,"
        " fitted_at_utc, activated, activated_at_utc"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (
            sha, scope, VALUATION_RULE, MARKET_REFERENCE_RULE,
            int(table["window_days"]), str(table.get("as_of_event_date") or ""),
            str(table.get("fit_cutoff_utc") or ""), table.get("beta"),
            table.get("beta_lcb"), table.get("beta_ucb"),
            int(table["station_day_count"]), int(table["day_count"]),
            1 if table["accumulating"] else 0,
            json.dumps(fit["evidence"], ensure_ascii=False, sort_keys=True),
            str(now), str(now),
        ),
    )
    conn.execute(
        "UPDATE calibration_valuation_table SET activated=1, activated_at_utc=?"
        " WHERE valuation_sha256=?",
        (str(now), sha),
    )
    conn.commit()
    return {"valuation_sha256": sha, "activated_at_utc": str(now)}


def journal_fit(
    conn: sqlite3.Connection, *, fit: Mapping[str, Any], outcome: str,
    fitted_at_utc: str,
) -> int:
    ensure_schema(conn)
    cur = conn.execute(
        "INSERT INTO calibration_valuation_fit_journal("
        " valuation_sha256, stage_scope, fitted_at_utc, outcome, evidence_json"
        ") VALUES(?,?,?,?,?)",
        (
            str(fit["content_sha256"]), str(fit["stage_scope"]), str(fitted_at_utc),
            str(outcome), json.dumps(fit["evidence"], ensure_ascii=False, sort_keys=True),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def persist_verdict(
    conn: sqlite3.Connection, *, stage_scope: str, verdict: str, reason: str,
    evidence: Mapping[str, Any], computed_at_utc: str,
    first_test_day: str | None, last_test_day: str | None,
) -> str:
    """Record the walk-forward verdict a promotion reads; one active per stage."""

    if verdict not in ("admitted", "hold"):
        raise ValueError(f"valuation_verdict_invalid:{verdict}")
    ensure_schema(conn)
    body = {
        "schema": VERDICT_SCHEMA, "stage_scope": stage_scope,
        "verdict": verdict, "reason": reason,
        "first_test_day": first_test_day, "last_test_day": last_test_day,
        "evidence": dict(evidence),
    }
    sha = content_sha256(body)
    conn.execute(
        "UPDATE calibration_valuation_verdict SET activated=0"
        " WHERE stage_scope=? AND activated=1",
        (stage_scope,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO calibration_valuation_verdict("
        " verdict_sha256, stage_scope, verdict, reason, first_test_day,"
        " last_test_day, evidence_json, computed_at_utc, activated"
        ") VALUES(?,?,?,?,?,?,?,?,1)",
        (
            sha, stage_scope, verdict, reason, first_test_day, last_test_day,
            json.dumps(body, ensure_ascii=False, sort_keys=True),
            computed_at_utc,
        ),
    )
    conn.commit()
    return sha


def load_valuation_as_of(
    conn: sqlite3.Connection, *, stage_scope: str, as_of_utc: str | None = None,
) -> dict[str, Any] | None:
    """What the scope served at ``as_of_utc`` (the active row when None), with
    the verdict that stood then.  None before the first fit."""

    if not _has_table(conn, "calibration_valuation_table"):
        return None
    cols = (
        "t.valuation_sha256, t.beta, t.beta_lcb, t.beta_ucb, t.accumulating,"
        " t.station_day_count, t.day_count, t.evidence_json, t.fit_cutoff_utc"
    )
    try:
        if as_of_utc is None:
            row = conn.execute(
                f"SELECT {cols} FROM calibration_valuation_table t"
                " WHERE t.stage_scope=? AND t.activated=1"
                " ORDER BY t.activated_at_utc DESC LIMIT 1",
                (str(stage_scope),),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT {cols} FROM calibration_valuation_fit_journal j"
                " JOIN calibration_valuation_table t"
                "   ON t.valuation_sha256=j.valuation_sha256"
                " WHERE j.stage_scope=? AND j.fitted_at_utc<=?"
                " ORDER BY j.fitted_at_utc DESC, j.fit_id DESC LIMIT 1",
                (str(stage_scope), str(as_of_utc)),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    sha, beta, lcb, ucb, accumulating, station_days, days, evidence_json, cutoff = row
    try:
        evidence = json.loads(str(evidence_json))
    except (TypeError, ValueError):
        evidence = {}
    draws = tuple(
        float(x) for x in (evidence.get("beta_draws") or ())
        if isinstance(x, (int, float)) and math.isfinite(float(x))
    )
    served = {
        "sha256": str(sha), "beta": beta, "beta_lcb": lcb, "beta_ucb": ucb,
        "accumulating": bool(accumulating),
        "station_day_count": int(station_days or 0), "day_count": int(days or 0),
        "beta_draws": draws, "fit_cutoff_utc": cutoff,
        "scores": evidence.get("scores") or {},
        "verdict": None, "verdict_reason": None,
    }
    if _has_table(conn, "calibration_valuation_verdict"):
        try:
            clause = "" if as_of_utc is None else " AND computed_at_utc<=?"
            args: tuple[Any, ...] = (str(stage_scope),) if as_of_utc is None else (
                str(stage_scope), str(as_of_utc),
            )
            verdict_row = conn.execute(
                "SELECT verdict, reason FROM calibration_valuation_verdict"
                " WHERE stage_scope=?" + clause
                + (" AND activated=1" if as_of_utc is None else "")
                + " ORDER BY computed_at_utc DESC LIMIT 1",
                args,
            ).fetchone()
        except sqlite3.Error:
            verdict_row = None
        if verdict_row is not None:
            served["verdict"] = str(verdict_row[0])
            served["verdict_reason"] = str(verdict_row[1])
    return served


def load_valuation_tables(
    conn: sqlite3.Connection, *, as_of_utc: str | None = None,
) -> dict[str, dict[str, Any] | None]:
    return {
        scope: load_valuation_as_of(conn, stage_scope=scope, as_of_utc=as_of_utc)
        for scope in STAGE_SCOPES
    }


__all__ = [
    "SCHEMA", "VERDICT_SCHEMA", "MARKET_REFERENCE_RULE", "VALUATION_RULE",
    "VALUATION_TABLE_KEY", "STAGE_SCOPES", "WINDOW_DAYS", "MIN_STATION_DAYS",
    "MIN_DAYS", "LCB_QUANTILE", "BETA_GRID",
    "ensure_schema", "market_reference_v1", "valuation_vector_v1",
    "window_mass", "ladder_cost_v1", "action_value_v1", "capacity_v1",
    "beta_draw_vectors", "population", "fit_valuation",
    "persist_and_activate", "journal_fit", "persist_verdict",
    "load_valuation_as_of", "load_valuation_tables",
]
