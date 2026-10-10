"""The realisation layer's fit, its calibration gate, its artifact, and the
shadow legs the betting lanes record against it (charter 254).

User ruling 2026-10-05 「上影子」: the layer (``realisation_layer``) is fitted
every day on the settled record, judged walk-forward on whether it says what
happens -- 「你说 90% 的时候，最好 90% 都是对的」 -- and written to a file; the
Proper and far-tail lanes record, for every leg they judge, the belief ``b``,
its band and the edge it implies on the leg's own price.  Nothing reads ``b``
to trade: the lanes decide exactly as before.

* ``population`` -- every P0 decision more than ``MIN_HOURS_TO_SETTLEMENT``
  before its settlement clock, with a complete book and a knowable label, over
  the rolling ``WINDOW_DAYS``: the scope the Proper lane acts in (charter 231
  补记 2026-10-10 dropped its >36 h window, so the population is every P0
  decision before the clock).  Each station-day weighs one.
* ``refit`` -- the walk-forward gate (every day with at least
  ``MIN_PREFIX_DAYS`` of earlier evidence is predicted from the days at least
  two before it whose labels were knowable by its midnight), then the fit on
  the whole window and ``BAND_DRAWS`` refits on bootstrapped event days for
  the band.  The gate reads the reliability of the walk-forward beliefs on
  every ten-bin x position cell with at least ``MIN_BIN_BUCKETS`` buckets and
  admits when no more cells fall outside their 95% interval than a calibrated
  forecaster would put there by chance (the binomial 95% point), the
  calibration line's slope interval holds one and its intercept's holds zero.
  Beside it, what the weather vector adds: the same pool without the weather
  terms (``MARKET_ONLY_COLUMNS``) predicted on the same days.
* ``write_artifact`` / ``load_active`` -- ``data/realisation_layer/active.json``
  (and a dated copy), read by the lanes and the board; no row in the SSOT.
* ``record_legs`` -- one ``realisation_layer_shadow`` row per leg, written in
  the lane's own transaction.  It never raises into the lane: a failure is a
  row whose ``state`` says what failed.

The fit reads the store ``mode=ro`` and writes only files (the board
readings job runs it, ``scripts/refit_realisation_layer.py``); the shadow rows
are written by the resident, inside the writer turn the lanes already hold.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from . import realisation_layer as layer
from . import run_change
from .basket_kernel.valuation_v1 import market_reference_v1
from .contracts import content_sha256
from .settlement_time import hours_to_settlement_if_known

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = Path(os.environ.get("PM_REALISATION_LAYER_DIR") or (ROOT / "data" / "realisation_layer"))
ACTIVE_NAME = "active.json"
#: Charter 231 补记二 (user ruling 2026-10-10 「把 P0-M、P1 阶段也放开」): the Proper
#: lane bets every belief stage, so b is fitted and gated per stage on that
#: stage's own decisions.  P0 keeps ``active.json`` (the board and the far-tail
#: shadow read it); the others write ``active_<slug>.json``.
STAGES = ("P0", "P0-M", "P1")
_STAGE_SLUGS = {"P0": "p0", "P0-M": "p0m", "P1": "p1"}


def _stage_slug(stage: str) -> str:
    try:
        return _STAGE_SLUGS[str(stage)]
    except KeyError as exc:
        raise ValueError(f"realisation_stage_unknown:{stage}") from exc


def active_name(stage: str = "P0") -> str:
    """The artifact file of ``stage``'s fit."""

    slug = _stage_slug(stage)
    return ACTIVE_NAME if slug == "p0" else f"active_{slug}.json"
HISTORY_KEEP = 30
SCHEMA = "realisation_layer_fit_v1"
WINDOW_DAYS = 28
#: Charter 231 补记 (user ruling 2026-10-10 「也是人工定的，按照proper线本来的设计
#: 来」): the Proper lane bets P0 at every hour before the settlement clock, so b is
#: fitted and gated on that whole scope (it was > 36 h).  The locked acceptance
#: (docs/research/realisation_acceptance_20261006) keeps its own 36 h constant.
MIN_HOURS_TO_SETTLEMENT = 0.0
MIN_PREFIX_DAYS = 8
BAND_DRAWS = 60
BAND_SEED = 20261005
GATE_BOOTSTRAP_DRAWS = 2000
LINE_BOOTSTRAP_DRAWS = 200
GATE_SEED = 20261005
GATE_CONFIDENCE = 0.95
MIN_BIN_BUCKETS = 200
RELIABILITY_BINS = (0.0, 0.005, 0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0001)
POSITION_GROUPS = (("all", None), ("modal", 0), ("beside", 1), ("far", 2))
#: The pool without the weather terms: the market sharpened plus the two
#: position intercepts.  What the weather vector adds is read against it.
MARKET_ONLY_COLUMNS = (0, 5, 6)
#: Charter 265: the pool without the three change columns -- v1's seven.  What
#: the latest-run change adds is read against it.
WITHOUT_CHANGE_COLUMNS = tuple(range(len(layer.PARAM_NAMES_V1)))
#: Charter 265: a change column is fitted only when this many station-days of
#: the training rows carry it (the acceptance's own readiness bar); until then
#: its weight is held at zero.  Measured 2026-10-10: the walk-forward day whose
#: prefix held 27 rows of AIFS change fitted weights of 6.4 and 5.7 and cost
#: that day 0.82 nat a decision; the same columns fitted on 1,300+ rows sit at
#: 0.1-0.5.
MIN_CHANGE_SUPPORT_STATION_DAYS = 150.0
#: How old the artifact may be before the hourly job fits again.
REFIT_MAX_AGE_HOURS = 20.0
SHADOW_TABLE = "realisation_layer_shadow"

STATE_ADMITTED = "ADMITTED"
STATE_REFUSED = "REFUSED"
STATE_ACCUMULATING = "ACCUMULATING"

SHADOW_DDL = (
    f"CREATE TABLE IF NOT EXISTS {SHADOW_TABLE} ("
    " shadow_id TEXT PRIMARY KEY,"
    " lane TEXT NOT NULL,"
    " kind TEXT NOT NULL,"
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " judged_at_utc TEXT NOT NULL,"
    " hours_to_settlement REAL,"
    " bucket TEXT NOT NULL,"
    " side TEXT NOT NULL,"
    " price REAL,"
    " fee_per_share REAL,"
    " outcome TEXT,"
    " order_intent_id TEXT,"
    " p REAL,"
    " q REAL,"
    " b REAL,"
    " b_low REAL,"
    " b_high REAL,"
    " edge REAL,"
    " edge_sure REAL,"
    " centres_agree INTEGER,"
    " state TEXT NOT NULL,"
    " fit_id TEXT,"
    " fit_state TEXT,"
    " fit_age_hours REAL,"
    " envelope_id TEXT,"
    " book_id TEXT,"
    " ladder_json TEXT,"
    " created_at_utc TEXT NOT NULL"
    ")"
)
SHADOW_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS idx_{SHADOW_TABLE}_day ON {SHADOW_TABLE}(event_date, lane)",
    f"CREATE INDEX IF NOT EXISTS idx_{SHADOW_TABLE}_judged ON {SHADOW_TABLE}(judged_at_utc)",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def code_sha256() -> str:
    """The fingerprint of the arithmetic an artifact was fitted by: a change
    to the layer or its fit refits on the next run whatever the artifact's age."""

    source = inspect.getsource(layer) + inspect.getsource(run_change) + "\n".join(
        inspect.getsource(fn) for fn in (population, walk_forward, calibration_gate, refit, _ladder_inputs,
                                         supported_columns, fit_supported)
    )
    # The population's scope is a module constant, not in any function's text:
    # without it a change of scope (charter 231 补记, 36 h -> 0) left the old
    # artifact looking current.
    source += f"\nMIN_HOURS_TO_SETTLEMENT={MIN_HOURS_TO_SETTLEMENT!r}"
    return hashlib.sha256(source.encode()).hexdigest()


# ---------------------------------------------------------------------------
# One ladder's inputs, for the fit and the lanes alike.


def _ladder_inputs(
    p_raw: Mapping[str, float], book_buckets: Sequence[Mapping[str, Any]],
    change_parts: Mapping[str, Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    """The ladder in order, its weather and market vectors, and the layer's
    terms -- or ``state`` naming why there are none.  With ``change_parts``
    (``run_change.latest_change``, charter 265) the terms are v2's ten
    columns; without, v1's seven."""

    market = market_reference_v1(list(book_buckets))
    if market.get("state") != "complete":
        return {"state": f"market_incomplete:{market.get('reason')}"}
    q = {str(k): float(v) for k, v in market["vector"].items()}
    rows = {str(r.get("bucket")): r for r in book_buckets}
    order = sorted(q, key=lambda bucket: rows.get(bucket, {}).get("ordinal", 0))
    p = {str(k): float(v) for k, v in (p_raw or {}).items()}
    if set(p) != set(q):
        return {"state": "ladder_mismatch"}
    change = None
    if change_parts is not None:
        change = run_change.change_columns(change_parts, rung_c=run_change.ladder_rung_c(order))
    try:
        f = layer.features([p[b] for b in order], [q[b] for b in order], change)
    except ValueError as exc:
        return {"state": f"invalid:{exc}"}
    return {"state": "ok", "order": order, "features": f, "change": change}


# ---------------------------------------------------------------------------
# The population.


def population(
    conn: sqlite3.Connection,
    *,
    cutoff_utc: str,
    window_days: int = WINDOW_DAYS,
    stage: str = "P0",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every ``stage`` decision more than ``MIN_HOURS_TO_SETTLEMENT`` out a
    fit at ``cutoff_utc`` could use: its label knowable by then, its book
    complete, its envelope's ladder the book's.  A P0-M decision taken at or
    after the day's first P1 is not one (charter 260: it is no belief)."""

    from .label_knowability import knowable_labels  # noqa: PLC0415

    labels = {
        key: (str(label["bucket"]), str(label["known_at"]))
        for key, label in knowable_labels(conn).items()
        if str(label["known_at"]) <= cutoff_utc
    }
    _stage_slug(stage)
    counts: dict[str, Any] = {
        "stage": str(stage), "facts_read": 0, "no_label": 0, "inside_window": 0, "decided_after_cutoff": 0,
        "superseded_by_p1": 0, "row_missing": 0, "market_incomplete": 0, "ladder_mismatch": 0, "invalid": 0,
        "label_outside_ladder": 0,
    }
    if not labels:
        return [], {**counts, "error": "no_knowable_labels"}
    latest = max(day for _station, day in labels)
    floor_day = (date.fromisoformat(latest) - timedelta(days=int(window_days))).isoformat()
    facts = conn.execute(
        "SELECT station_id, event_date, decision_at_utc, probability_envelope_id, ws_snapshot_id,"
        " settlement_timezone FROM basket_policy_training_fact"
        " WHERE stage=? AND event_date > ? AND event_date <= ?",
        (str(stage), floor_day, latest),
    ).fetchall()
    first_p1: dict[tuple[str, str], str] = {}
    if str(stage) == "P0-M":
        from .stage_authority import first_p1_decisions  # noqa: PLC0415

        first_p1 = first_p1_decisions(conn, min_decision_at_utc=f"{floor_day}T00:00:00Z")
    decisions: list[dict[str, Any]] = []
    runs_cache: dict[tuple[str, str], dict[str, list[tuple]]] = {}
    for station, day, decided, envelope_id, snapshot_id, tz in facts:
        counts["facts_read"] += 1
        label = labels.get((str(station), str(day)))
        if label is None:
            counts["no_label"] += 1
            continue
        if str(decided) > cutoff_utc:
            counts["decided_after_cutoff"] += 1
            continue
        if first_p1:
            from .stage_authority import p0m_superseded  # noqa: PLC0415

            if p0m_superseded("P0-M", str(station), str(day), str(decided), first_p1):
                counts["superseded_by_p1"] += 1
                continue
        hours = hours_to_settlement_if_known(str(day), as_of_utc=str(decided), settlement_timezone=tz)
        if hours is None or not hours > MIN_HOURS_TO_SETTLEMENT:
            counts["inside_window"] += 1
            continue
        envelope = conn.execute(
            "SELECT probabilities_json FROM basket_probability_envelope WHERE envelope_id=?",
            (envelope_id,),
        ).fetchone()
        snapshot = conn.execute(
            "SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if envelope is None or snapshot is None:
            counts["row_missing"] += 1
            continue
        try:
            p_raw = json.loads(envelope[0] or "{}")
            buckets = json.loads(snapshot[0] or "[]")
        except (TypeError, ValueError):
            counts["invalid"] += 1
            continue
        key = (str(station), str(day))
        if key not in runs_cache:
            runs_cache[key] = run_change.family_runs(conn, station_id=key[0], event_date=key[1])
        parts = run_change.latest_change(runs_cache[key], as_of=str(decided))
        inputs = _ladder_inputs(p_raw, buckets, parts)
        state = str(inputs["state"])
        if state != "ok":
            key = state.split(":")[0]
            counts[key] = counts.get(key, 0) + 1
            continue
        bucket, known_at = label
        if bucket not in inputs["order"]:
            counts["label_outside_ladder"] += 1
            continue
        f = inputs["features"]
        decisions.append({
            "station_id": str(station), "event_date": str(day), "decision_at_utc": str(decided),
            "label_known_at": known_at, "hours_to_settlement": float(hours),
            "terms": f["terms"], "q": f["q"], "mode": f["mode"], "centres_agree": f["centres_agree"],
            "y": inputs["order"].index(bucket),
        })
    per_day: dict[tuple[str, str], int] = {}
    for d in decisions:
        per_day[(d["station_id"], d["event_date"])] = per_day.get((d["station_id"], d["event_date"]), 0) + 1
    for d in decisions:
        d["weight"] = 1.0 / per_day[(d["station_id"], d["event_date"])]
    return decisions, {**counts, "floor_day": floor_day, "latest_day": latest,
                       "decisions": len(decisions), "station_days": len(per_day)}


# ---------------------------------------------------------------------------
# The walk-forward gate.


def _columns(arrays: Mapping[str, np.ndarray], columns: Sequence[int]) -> dict[str, np.ndarray]:
    return {**arrays, "X": arrays["X"][:, :, list(columns)]}


def supported_columns(arrays: Mapping[str, np.ndarray]) -> list[int]:
    """Every v1 column, and each change column carried by at least
    ``MIN_CHANGE_SUPPORT_STATION_DAYS`` station-days of these rows."""

    columns = list(WITHOUT_CHANGE_COLUMNS)
    X, w = arrays["X"], arrays["w"]
    for column in range(len(WITHOUT_CHANGE_COLUMNS), X.shape[2]):
        carried = np.abs(X[:, :, column]).max(axis=1) > 1e-12
        if float(w[carried].sum()) >= MIN_CHANGE_SUPPORT_STATION_DAYS:
            columns.append(column)
    return columns


def fit_supported(
    arrays: Mapping[str, np.ndarray],
    *,
    weights: np.ndarray | None = None,
    columns: Sequence[int] | None = None,
    start: Sequence[float] | None = None,
) -> tuple[np.ndarray, list[int]]:
    """``layer.fit`` on the supported columns; the rest held at zero."""

    cols = list(columns) if columns is not None else supported_columns(arrays)
    origin = layer.START if start is None else start
    theta = layer.fit(_columns(arrays, cols), weights=weights, start=[origin[c] for c in cols])
    full = np.zeros(arrays["X"].shape[2])
    full[cols] = theta
    return full, cols


def walk_forward(decisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Each day with at least ``MIN_PREFIX_DAYS`` of earlier evidence,
    predicted by the layer and by the market-only pool fitted on the days at
    least two before it whose labels were knowable by its midnight."""

    days = sorted({d["event_date"] for d in decisions})
    layer_b: dict[int, np.ndarray] = {}
    market_b: dict[int, np.ndarray] = {}
    without_b: dict[int, np.ndarray] = {}
    scored: list[str] = []
    for day in days:
        cut = (date.fromisoformat(day) - timedelta(days=2)).isoformat()
        midnight = f"{day}T00:00:00Z"
        prefix = [i for i, d in enumerate(decisions)
                  if d["event_date"] <= cut and str(d["label_known_at"]) <= midnight]
        if len({decisions[i]["event_date"] for i in prefix}) < MIN_PREFIX_DAYS:
            continue
        test = [i for i, d in enumerate(decisions) if d["event_date"] == day]
        arrays = layer.stack([decisions[i] for i in prefix])
        theta, _cols = fit_supported(arrays)
        theta_m = layer.fit(_columns(arrays, MARKET_ONLY_COLUMNS),
                            start=[layer.START[c] for c in MARKET_ONLY_COLUMNS])
        theta_w = layer.fit(_columns(arrays, WITHOUT_CHANGE_COLUMNS),
                            start=[layer.START[c] for c in WITHOUT_CHANGE_COLUMNS])
        for i in test:
            terms = decisions[i]["terms"]
            layer_b[i] = layer.belief(terms, theta)
            market_b[i] = layer.belief(terms[:, list(MARKET_ONLY_COLUMNS)], theta_m)
            without_b[i] = layer.belief(terms[:, list(WITHOUT_CHANGE_COLUMNS)], theta_w)
        scored.append(day)
    return {"days": scored, "layer": layer_b, "market_only": market_b, "without_change": without_b}


def _binomial_allowance(count: int, rate: float, confidence: float) -> int:
    """The smallest c with P(Binomial(count, rate) <= c) >= confidence."""

    total = 0.0
    for c in range(count + 1):
        total += math.comb(count, c) * rate ** c * (1.0 - rate) ** (count - c)
        if total >= confidence:
            return c
    return count


def _logistic_line(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
    design = np.column_stack([np.ones(len(x)), x])
    beta = np.zeros(2)
    for _ in range(50):
        mu = 1.0 / (1.0 + np.exp(-(design @ beta)))
        gradient = design.T @ (w * (y - mu))
        hessian = (design * (w * mu * (1 - mu))[:, None]).T @ design + 1e-9 * np.eye(2)
        step = np.linalg.solve(hessian, gradient)
        beta = beta + step
        if np.abs(step).max() < 1e-9:
            break
    return beta


def calibration_gate(decisions: Sequence[Mapping[str, Any]], forward: Mapping[str, Any]) -> dict[str, Any]:
    """Charter 254's gate on the walk-forward beliefs."""

    scored = sorted(forward["layer"])
    if len(forward["days"]) < MIN_PREFIX_DAYS:
        return {"state": STATE_ACCUMULATING, "scored_days": len(forward["days"]),
                "scored_decisions": len(scored), "min_scored_days": MIN_PREFIX_DAYS}
    day_index = {day: k for k, day in enumerate(sorted({decisions[i]["event_date"] for i in scored}))}
    n_days = len(day_index)
    b_all, y_all, w_all, pos_all, day_all = [], [], [], [], []
    ls_layer, ls_market, ls_market_only, top_layer, top_market, weights, codes = [], [], [], [], [], [], []
    ls_without: list[float] = []
    for i in scored:
        d = decisions[i]
        b = forward["layer"][i]
        k = len(b)
        position = np.abs(np.arange(k) - d["mode"])
        b_all.append(b)
        y_all.append((np.arange(k) == d["y"]).astype(float))
        w_all.append(np.full(k, d["weight"]))
        pos_all.append(np.minimum(position, 2))
        day_all.append(np.full(k, day_index[d["event_date"]]))
        ls_layer.append(-math.log(b[d["y"]]))
        ls_market.append(-math.log(d["q"][d["y"]]))
        ls_market_only.append(-math.log(forward["market_only"][i][d["y"]]))
        if "without_change" in forward:
            ls_without.append(-math.log(forward["without_change"][i][d["y"]]))
        top_layer.append(float(b.max()))
        top_market.append(float(np.max(d["q"])))
        weights.append(d["weight"])
        codes.append(day_index[d["event_date"]])
    b_all, y_all, w_all = np.concatenate(b_all), np.concatenate(y_all), np.concatenate(w_all)
    pos_all, day_all = np.concatenate(pos_all), np.concatenate(day_all).astype(int)
    weights, codes = np.asarray(weights), np.asarray(codes, dtype=int)
    rng = np.random.default_rng(GATE_SEED)
    draws = rng.integers(0, n_days, size=(GATE_BOOTSTRAP_DRAWS, n_days))
    tail = 100.0 * (1.0 - GATE_CONFIDENCE) / 2.0

    def interval(values: np.ndarray, w: np.ndarray, c: np.ndarray) -> tuple[float, float, float]:
        sums = np.bincount(c, values * w, n_days)
        mass = np.bincount(c, w, n_days)
        with np.errstate(invalid="ignore", divide="ignore"):
            boot = sums[draws].sum(axis=1) / mass[draws].sum(axis=1)
        low, high = np.nanpercentile(boot, [tail, 100.0 - tail])
        return float(sums.sum() / mass.sum()), float(low), float(high)

    cells, judged, failed = [], 0, 0
    for group, position in POSITION_GROUPS:
        in_group = np.ones(len(b_all), bool) if position is None else (pos_all == position)
        for low, high in zip(RELIABILITY_BINS[:-1], RELIABILITY_BINS[1:]):
            m = in_group & (b_all >= low) & (b_all < high)
            count = int(m.sum())
            if count == 0:
                continue
            gap = interval(y_all[m] - b_all[m], w_all[m], day_all[m])
            is_judged = count >= MIN_BIN_BUCKETS
            outside = bool(is_judged and not (gap[1] <= 0.0 <= gap[2]))
            judged += int(is_judged)
            failed += int(outside)
            cells.append({
                "group": group, "bin": [low, min(high, 1.0)], "buckets": count,
                "said": float(np.average(b_all[m], weights=w_all[m])),
                "landed": float(np.average(y_all[m], weights=w_all[m])),
                "gap": list(gap), "judged": is_judged, "outside": outside,
            })
    allowed = _binomial_allowance(judged, 1.0 - GATE_CONFIDENCE, GATE_CONFIDENCE)
    x = np.log(np.clip(b_all, 1e-6, 1 - 1e-6) / np.clip(1 - b_all, 1e-6, 1))
    line = _logistic_line(x, y_all, w_all)
    line_draws = []
    for row in draws[:LINE_BOOTSTRAP_DRAWS]:
        mult = np.bincount(row, minlength=n_days).astype(float)
        line_draws.append(_logistic_line(x, y_all, w_all * mult[day_all]))
    line_low, line_high = np.percentile(np.asarray(line_draws), [tail, 100.0 - tail], axis=0)
    ls_layer, ls_market, ls_market_only = map(np.asarray, (ls_layer, ls_market, ls_market_only))
    slope_ok = bool(line_low[1] <= 1.0 <= line_high[1])
    intercept_ok = bool(line_low[0] <= 0.0 <= line_high[0])
    state = STATE_ADMITTED if (failed <= allowed and slope_ok and intercept_ok) else STATE_REFUSED
    return {
        "state": state,
        "rule": "reliability_cells_binomial_allowance_and_calibration_line_v1",
        "scored_days": len(forward["days"]), "first_scored_day": forward["days"][0],
        "last_scored_day": forward["days"][-1], "scored_decisions": len(scored),
        "cells": cells, "cells_judged": judged, "cells_outside": failed, "cells_allowed": allowed,
        "calibration_line": {"intercept": [float(line[0]), float(line_low[0]), float(line_high[0])],
                             "slope": [float(line[1]), float(line_low[1]), float(line_high[1])]},
        "logscore": {
            "layer": interval(ls_layer, weights, codes),
            "market": interval(ls_market, weights, codes),
            "market_only_pool": interval(ls_market_only, weights, codes),
            "layer_minus_market": interval(ls_layer - ls_market, weights, codes),
            # Negative: the weather vector carries something the market's own
            # pool does not.
            "layer_minus_market_only_pool": interval(ls_layer - ls_market_only, weights, codes),
            # Charter 265, negative: the latest-run change carries something
            # the v1 pool (market and weather level) does not.
            **({"layer_without_change": interval(np.asarray(ls_without), weights, codes),
                "layer_minus_layer_without_change": interval(ls_layer - np.asarray(ls_without), weights, codes)}
               if len(ls_without) == len(ls_layer) and ls_without else {}),
        },
        "top_bucket": {"layer": float(np.average(top_layer, weights=weights)),
                       "market": float(np.average(top_market, weights=weights))},
    }


# ---------------------------------------------------------------------------
# The refit and its artifact.


def refit(
    conn: sqlite3.Connection,
    *,
    cutoff_utc: str | None = None,
    window_days: int = WINDOW_DAYS,
    stage: str = "P0",
) -> dict[str, Any]:
    """The whole night's work on a read-only connection for one belief stage:
    population, gate, fit, band, and the shadow legs' realisation so far."""

    cutoff = str(cutoff_utc or _now_iso())
    decisions, counts = population(conn, cutoff_utc=cutoff, window_days=window_days, stage=stage)
    out: dict[str, Any] = {
        "schema": SCHEMA, "rule": layer.RULE, "param_names": list(layer.PARAM_NAMES), "stage": str(stage),
        "fitted_at_utc": _now_iso(), "cutoff_utc": cutoff, "window_days": window_days,
        "min_hours_to_settlement": MIN_HOURS_TO_SETTLEMENT, "code_sha256": code_sha256(),
        "population": counts,
    }
    if len({d["event_date"] for d in decisions}) < MIN_PREFIX_DAYS:
        out.update(theta=None, draws=[], gate={"state": STATE_ACCUMULATING,
                                               "event_days": len({d["event_date"] for d in decisions})})
    else:
        forward = walk_forward(decisions)
        out["gate"] = calibration_gate(decisions, forward)
        arrays = layer.stack(decisions)
        theta, fitted = fit_supported(arrays)
        days = sorted({d["event_date"] for d in decisions})
        index = {day: k for k, day in enumerate(days)}
        codes = np.asarray([index[d["event_date"]] for d in decisions])
        rng = np.random.default_rng(BAND_SEED)
        draws = []
        for _ in range(BAND_DRAWS):
            mult = np.bincount(rng.integers(0, len(days), size=len(days)), minlength=len(days)).astype(float)
            draw, _cols = fit_supported(arrays, weights=arrays["w"] * mult[codes], columns=fitted, start=theta)
            draws.append([float(v) for v in draw])
        out.update(theta=[float(v) for v in theta], draws=draws,
                   theta_named=dict(zip(layer.PARAM_NAMES, (float(v) for v in theta))),
                   fitted_columns=[layer.PARAM_NAMES[c] for c in fitted])
    out["shadow"] = shadow_realisation(conn, cutoff_utc=cutoff)
    out["fit_id"] = f"realisation_{content_sha256({k: out[k] for k in ('fitted_at_utc', 'theta', 'code_sha256', 'stage')})[:24]}"
    return out


def write_artifact(artifact: Mapping[str, Any], directory: Path | None = None) -> Path:
    """``active.json`` replaced atomically, and a dated copy beside it."""

    folder = Path(directory or ARTIFACT_DIR)
    (folder / "history").mkdir(parents=True, exist_ok=True)
    stage = str(artifact.get("stage") or "P0")
    slug = _stage_slug(stage)
    text = json.dumps(artifact, ensure_ascii=False, sort_keys=True, default=float)
    stamp = str(artifact["fitted_at_utc"]).replace(":", "").replace("-", "")[:15]
    # P0's history keeps its names; another stage's files carry its slug.
    prefix = "fit_" if slug == "p0" else f"fit-{slug}_"
    (folder / "history" / f"{prefix}{stamp}Z.json").write_text(text, encoding="utf-8")
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".active.", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(text)
    target = folder / active_name(stage)
    os.replace(temporary, target)
    kept = sorted((folder / "history").glob("fit_[0-9]*.json" if slug == "p0" else f"{prefix}*.json"))
    for old in kept[:-HISTORY_KEEP]:
        old.unlink()
    return target


_ACTIVE_CACHE: dict[str, Any] = {}


def load_active(directory: Path | None = None, *, stage: str = "P0") -> dict[str, Any] | None:
    """``stage``'s newest artifact, re-read only when its file changed."""

    path = Path(directory or ARTIFACT_DIR) / active_name(stage)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    held = _ACTIVE_CACHE.get(str(path))
    if held is not None and held["stamp"] == stamp:
        return held["artifact"]
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    _ACTIVE_CACHE[str(path)] = {"stamp": stamp, "artifact": artifact}
    return artifact


def artifact_is_current(artifact: Mapping[str, Any] | None, *, now: datetime | None = None) -> bool:
    if not artifact or artifact.get("code_sha256") != code_sha256():
        return False
    try:
        age = ((now or datetime.now(UTC)) - _instant(str(artifact["fitted_at_utc"]))).total_seconds()
    except (KeyError, ValueError):
        return False
    return age < REFIT_MAX_AGE_HOURS * 3600.0


# ---------------------------------------------------------------------------
# The shadow legs.


def ensure_shadow_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SHADOW_DDL)
    for statement in SHADOW_INDEX_DDL:
        conn.execute(statement)


def envelope_probabilities(conn: sqlite3.Connection, envelope_id: str | None) -> dict[str, float] | None:
    """An envelope's vector as it holds it, unfloored -- the layer floors once."""

    if not envelope_id:
        return None
    row = conn.execute(
        "SELECT probabilities_json FROM basket_probability_envelope WHERE envelope_id=?", (envelope_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        return {str(k): float(v) for k, v in json.loads(row[0] or "{}").items()}
    except (TypeError, ValueError):
        return None


def _belief_of(p_raw: Mapping[str, float] | None, book_buckets: Sequence[Mapping[str, Any]],
               artifact: Mapping[str, Any] | None,
               change_parts: Mapping[str, Mapping[str, float]] | None = None) -> dict[str, Any]:
    """``b`` on one ladder under ``artifact``: a v2 fit (charter 265, ten
    weights) reads the change columns from ``change_parts``, a v1 fit its
    seven columns alone."""

    if p_raw is None:
        return {"state": "no_p0"}
    theta = (artifact or {}).get("theta") or []
    v2 = len(theta) == len(layer.PARAM_NAMES)
    if v2 and change_parts is None:
        return {"state": "no_change_read"}
    inputs = _ladder_inputs(p_raw, book_buckets, change_parts if v2 else None)
    if inputs["state"] != "ok":
        return inputs
    f = inputs["features"]
    ladder = {"order": inputs["order"], "p": f["p"], "q": f["q"], "centres_agree": f["centres_agree"]}
    if not artifact or not artifact.get("theta"):
        return {**ladder, "state": "no_fit"}
    b = layer.belief(f["terms"], artifact["theta"])
    if artifact.get("draws"):
        low, high = layer.belief_band(f["terms"], artifact["draws"])
    else:
        low = high = b
    return {**ladder, "state": "ok", "b": b, "low": low, "high": high}


def record_legs(
    conn: sqlite3.Connection,
    *,
    lane: str,
    kind: str,
    station_id: str,
    event_date: str,
    judged_at_utc: str,
    hours_to_settlement: float | None,
    book: Mapping[str, Any] | None,
    p_raw: Mapping[str, float] | None,
    envelope_id: str | None,
    legs: Sequence[Mapping[str, Any]],
    artifact: Mapping[str, Any] | None = None,
    stage: str = "P0",
) -> dict[str, Any]:
    """One shadow row per leg, read under ``stage``'s fit; never raises into the lane.

    ``legs``: ``bucket``, ``side`` ("YES"/"NO"), ``price`` (per share on the
    leg's own side), ``fee_per_share``, ``outcome`` (what the lane did), and
    ``order_intent_id`` when one was made.
    """

    if not legs:
        return {"state": "no_leg", "rows": 0}
    try:
        active = artifact if artifact is not None else load_active(stage=stage)
        if book is None:
            reading: dict[str, Any] = {"state": "no_book"}
        else:
            parts = run_change.change_at(conn, station_id=station_id, event_date=event_date, as_of=judged_at_utc)
            reading = _belief_of(p_raw, list(book.get("buckets") or []), active, parts)
    except Exception as exc:  # noqa: BLE001 -- the shadow never stops the lane
        active, reading = None, {"state": f"error:{type(exc).__name__}:{str(exc)[:120]}"}
    fit_age = None
    if active and active.get("fitted_at_utc"):
        try:
            fit_age = round((_instant(judged_at_utc) - _instant(active["fitted_at_utc"])).total_seconds() / 3600.0, 2)
        except ValueError:
            fit_age = None
    order = list(reading.get("order") or [])
    ladder_json = None
    if order:
        ladder = {"buckets": order, "p": [round(float(v), 6) for v in reading["p"]],
                  "q": [round(float(v), 6) for v in reading["q"]]}
        if reading.get("state") == "ok":
            ladder.update(b=[round(float(v), 6) for v in reading["b"]],
                          b_low=[round(float(v), 6) for v in reading["low"]],
                          b_high=[round(float(v), 6) for v in reading["high"]])
        ladder_json = json.dumps(ladder, ensure_ascii=False)
    written = 0
    try:
        ensure_shadow_schema(conn)
        for leg in legs:
            bucket, side = str(leg["bucket"]), str(leg["side"])
            row: dict[str, Any] = {
                "lane": lane, "kind": kind, "station_id": station_id, "event_date": event_date,
                "judged_at_utc": judged_at_utc, "hours_to_settlement": hours_to_settlement,
                "bucket": bucket, "side": side,
                "price": None if leg.get("price") is None else float(leg["price"]),
                "fee_per_share": float(leg.get("fee_per_share") or 0.0),
                "outcome": leg.get("outcome"), "order_intent_id": leg.get("order_intent_id"),
                "p": None, "q": None, "b": None, "b_low": None, "b_high": None,
                "edge": None, "edge_sure": None,
                "centres_agree": None if "centres_agree" not in reading else int(bool(reading["centres_agree"])),
                "state": str(reading.get("state")),
                "fit_id": (active or {}).get("fit_id"),
                "fit_state": ((active or {}).get("gate") or {}).get("state"),
                "fit_age_hours": fit_age, "envelope_id": envelope_id,
                "book_id": (book or {}).get("snapshot_id"), "ladder_json": ladder_json,
                "created_at_utc": _now_iso(),
            }
            if bucket in order:
                k = order.index(bucket)
                row.update(p=float(reading["p"][k]), q=float(reading["q"][k]))
                if reading.get("state") == "ok":
                    row.update(b=float(reading["b"][k]), b_low=float(reading["low"][k]),
                               b_high=float(reading["high"][k]))
                    if row["price"] is not None:
                        row.update(layer.leg_edge(side=side, b=row["b"], b_low=row["b_low"],
                                                  b_high=row["b_high"], price=row["price"],
                                                  fee_per_share=row["fee_per_share"]))
            elif order:
                row["state"] = "bucket_outside_ladder"
            row["shadow_id"] = "shadow_" + content_sha256({
                k: row[k] for k in ("lane", "kind", "station_id", "event_date", "judged_at_utc",
                                    "bucket", "side", "order_intent_id")
            })[:32]
            columns = ",".join(row)
            conn.execute(
                f"INSERT OR IGNORE INTO {SHADOW_TABLE}({columns}) VALUES({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
            written += 1
    except Exception as exc:  # noqa: BLE001 -- the shadow never stops the lane
        return {"state": f"write_failed:{type(exc).__name__}:{str(exc)[:120]}", "rows": written}
    return {"state": str(reading.get("state")), "rows": written}


def belief_vector(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
    p_raw: Mapping[str, float] | None,
    book_buckets: Sequence[Mapping[str, Any]],
    artifact: Mapping[str, Any] | None = None,
    stage: str = "P0",
) -> dict[str, Any]:
    """Charter 265: the belief a lane bets on -- ``b`` under ``stage``'s fit
    (charter 231 补记二: one per belief stage, ``p_raw`` that stage's vector),
    with the three systems' latest-run change read at ``as_of_utc`` -- or a
    named refusal.  Only a v2 fit (the change columns) whose gate admitted it,
    fitted by this code and young enough is read; anything else fails closed
    rather than betting on a belief nobody certified."""

    active = artifact if artifact is not None else load_active(stage=stage)
    if not active or not active.get("theta"):
        return {"state": "no_fit"}
    if str(active.get("stage") or "P0") != str(stage):
        return {"state": "fit_stage_mismatch"}
    if active.get("rule") != layer.RULE or len(active["theta"]) != len(layer.PARAM_NAMES):
        return {"state": "fit_without_change_terms"}
    gate_state = str((active.get("gate") or {}).get("state") or "")
    if gate_state != STATE_ADMITTED:
        return {"state": f"fit_{gate_state.lower() or 'ungated'}"}
    if not artifact_is_current(active, now=_instant(as_of_utc)):
        return {"state": "fit_not_current"}
    parts = run_change.change_at(conn, station_id=station_id, event_date=event_date, as_of=as_of_utc)
    reading = _belief_of(p_raw, book_buckets, active, parts)
    if reading.get("state") != "ok":
        return {"state": str(reading.get("state"))}
    return {
        "state": "ok", "b": {bucket: float(v) for bucket, v in zip(reading["order"], reading["b"])},
        "fit_id": active.get("fit_id"), "change": parts, "change_rule": run_change.RULE,
    }


def taker_fee_per_share(price: float, rate: float) -> float:
    """The venue's taker fee per share: rate x p x (1 - p) (charter 234)."""

    return float(rate) * float(price) * (1.0 - float(price))


# ---------------------------------------------------------------------------
# The shadow legs against what settled.


def shadow_realisation(conn: sqlite3.Connection, *, cutoff_utc: str) -> dict[str, Any]:
    """Per lane and side, on the settled shadow legs: what ``b`` said, what
    landed, the edge it claimed and the one realised on the leg's price.

    ``decided``: each station-day's leg once, at its first judgement (the
    lane's decision).  ``filled``: the legs a fill reached, at the rung or
    take that filled.  Intervals by event-day bootstrap.
    """

    if not _has_table(conn, SHADOW_TABLE):
        return {"available": False, "reason": "no_shadow_table"}
    from .label_knowability import knowable_labels  # noqa: PLC0415

    labels = {key: label["bucket"] for key, label in knowable_labels(conn).items()
              if str(label["known_at"]) <= cutoff_utc}
    rows = conn.execute(
        f"SELECT s.lane, s.station_id, s.event_date, s.bucket, s.side, s.judged_at_utc, s.price,"
        f" s.fee_per_share, s.b, s.b_low, s.b_high, s.edge, s.edge_sure, s.order_intent_id,"
        f" COALESCE(r.filled_size, 0)"
        f" FROM {SHADOW_TABLE} s"
        " LEFT JOIN basket_target_order_reconciliations r ON r.order_intent_id = s.order_intent_id"
        " WHERE s.state='ok' AND s.price IS NOT NULL ORDER BY s.judged_at_utc"
    ).fetchall()
    decided: dict[tuple, tuple] = {}
    filled: dict[tuple, tuple] = {}
    for row in rows:
        lane, station, day, bucket, side = row[:5]
        if (station, day) not in labels:
            continue
        key = (lane, station, day, bucket, side)
        decided.setdefault(key, row)
        if float(row[14] or 0.0) > 0.0:
            filled.setdefault(key, row)
    out: dict[str, Any] = {"available": True, "settled_station_days": len({(k[1], k[2]) for k in decided})}
    for name, chosen in (("decided", decided), ("filled", filled)):
        groups: dict[tuple[str, str], list[tuple]] = {}
        for key, row in chosen.items():
            groups.setdefault((key[0], key[4]), []).append(row)
        table = []
        for (lane, side), members in sorted(groups.items()):
            hit = np.asarray([1.0 if labels[(r[1], r[2])] == r[3] else 0.0 for r in members])
            b = np.asarray([float(r[8]) for r in members])
            price = np.asarray([float(r[6]) for r in members])
            fee = np.asarray([float(r[7] or 0.0) for r in members])
            claimed = np.asarray([float(r[11]) for r in members])
            sure = np.asarray([float(r[12]) > 0.0 for r in members])
            side_hit = hit if side == "YES" else 1.0 - hit
            side_b = b if side == "YES" else 1.0 - b
            realised = side_hit - price - fee
            days = sorted({r[2] for r in members})
            index = {d: k for k, d in enumerate(days)}
            codes = np.asarray([index[r[2]] for r in members])
            boot = np.random.default_rng(GATE_SEED).integers(0, len(days), size=(GATE_BOOTSTRAP_DRAWS, len(days)))

            def interval(values: np.ndarray, keep: np.ndarray | None = None) -> list[float] | None:
                c = codes if keep is None else codes[keep]
                v = values if keep is None else values[keep]
                if len(v) == 0:
                    return None
                sums = np.bincount(c, v, len(days))
                count = np.bincount(c, minlength=len(days)).astype(float)
                with np.errstate(invalid="ignore", divide="ignore"):
                    draws = sums[boot].sum(axis=1) / count[boot].sum(axis=1)
                low, high = np.nanpercentile(draws, [2.5, 97.5])
                return [float(v.mean()), float(low), float(high)]

            table.append({
                "lane": lane, "side": side, "legs": len(members), "event_days": len(days),
                "belief_side": float(side_b.mean()), "landed_side": float(side_hit.mean()),
                "price": float(price.mean()), "claimed": float(claimed.mean()),
                "realised": interval(realised), "belief_minus_landed": interval(side_b - side_hit),
                "sure_legs": int(sure.sum()),
                "sure_realised": interval(realised, sure),
            })
        out[name] = table
    return out


__all__ = [
    "ACTIVE_NAME", "ARTIFACT_DIR", "MIN_HOURS_TO_SETTLEMENT", "SCHEMA", "SHADOW_TABLE", "STAGES", "active_name",
    "STATE_ACCUMULATING", "STATE_ADMITTED", "STATE_REFUSED", "WINDOW_DAYS",
    "artifact_is_current", "calibration_gate", "code_sha256", "ensure_shadow_schema",
    "envelope_probabilities", "load_active", "population", "record_legs", "refit",
    "shadow_realisation", "taker_fee_per_share", "walk_forward", "write_artifact",
]
