"""Shadow band reliability with station and bucket-position dimensions.

The served table (``band_reliability_store``) is pooled: one factor per
stage per claim band, identical for all forty-five stations and for every
bucket in a day's ladder.  Measured on 2026-08-28 that pooling hides two
real splits.  Within a claim band, buckets near the distribution's argmax
realise at a different rate than buckets far from it (P0 [0.10,0.15):
1.42 near vs 0.90 far, by-day bootstrap CI [+0.20,+0.81]), and stations
disagree with the fleet in a hold-out sense (P0: 29 significant
station-group deviations, P1: 40) -- ZUUU claims its core at 0.19 and its
shoulder at 1.38 in *both* stages, KLGA the mirror image at 1.99 core.

This module fits that finer table and never serves it.  Nothing in the
serving path imports it; ``basis`` values here are diagnostics, not
factors any artifact can pin.  Its purpose is to answer, on accumulating
evidence and under a stated rule, whether the finer geometry ever earns
its keep -- and to make the answer auditable before any of it reaches a
served distribution.

Geometry
--------

Two dimensions on top of the served claim band:

* ``station_id`` -- the natural per-station dimension.
* ``position_group`` -- signed offset from the snapshot's **argmax
  bucket**, coarsened to five groups (``cool_far`` d<=-2, ``cool_near``
  d=-1, ``centre`` d=0, ``warm_near`` d=+1, ``warm_far`` d>=+2).

``argmax`` is stated deliberately.  This repo has three live definitions
of "centre" (the ladder centre of charter 54, the gate centre, and the
deflation centre) and they disagree by design; the shadow uses none of
them.  The argmax of the claim itself is intrinsic to the distribution
being audited, needs no selection-layer state, and is what the 2026-08-28
probe measured -- so the shadow's numbers stay comparable to it.

Absolute temperature buckets (a factor for "ZBAA's 25-26C bucket") were
considered and are not fittable: a station's market ladder shifts with
its forecast, so over twenty settled days a station touches dozens of
distinct absolute buckets with about one station-day of evidence each,
and the cell identity is not even stable across days.  Position relative
to the claim's own peak is the bucket-identity dimension that both has
sample and means the same thing on every day.

Backoff (user ruling, 2026-08-28: "回退至池权重")
------------------------------------------------

Every cell is fitted at the finest level it can support and falls back --
hard, not shrunk -- when it cannot:

1. ``station_position``  station x position x band
2. ``station``           station x band          (position marginalised)
3. ``position``          position x band         (station marginalised)
4. ``pooled``            the live served factor for that band

A partial-pooling (empirical-Bayes) estimate is recorded alongside each
cell for comparison, because it is the obvious next question, but it is
never what the cell serves: the ruling asked for pooled fallback and a
shrunk factor is a third thing that neither lane measured.

Three gates, re-set for thin cells
----------------------------------

The pooled table's rule tests eleven cells on the whole fleet's mass.
This one tests thousands of cells, most of them thin, so the same three
hurdles need different sample rules -- not looser ones:

* **Gate A, evidence mass.**  The pooled rule gates the *table* on ten
  qualifying days and lets every cell inherit that.  Per-cell that is
  meaningless: a cell holding 1.2 expected hits cannot distinguish 0.5x
  from 2.0x however many days it spans.  A cell must carry
  ``MIN_CELL_CLAIMED_MASS`` expected hits, over ``MIN_CELL_DAYS`` distinct
  event days, in ``MIN_CELL_ROWS`` bucket-claims.  The mass floor is the
  binding one: the ratio's relative standard error is about
  1/sqrt(mass), so 8.0 expected hits is the point where a 1.7x deviation
  is separable from noise and anything smaller honestly is not.
* **Gate B, significance with multiplicity control.**  The by-day
  bootstrap is kept -- weather is shared across stations within a day, so
  the day is the resampling unit, and every cell is resampled on the
  *same* day draws so correlated cells stay correlated.  What is new is
  Benjamini-Hochberg FDR across all cells tested at a level: at eleven
  cells a bare 95% interval admits about half a false cell, at three
  thousand it admits a hundred and fifty.  The 2026-08-28 probe walked
  straight into this -- a dozen stations showed "tail realises 0.00",
  which is what a fleet of 45 stations with 1.7 expected tail hits each
  produces by arithmetic, not by any station being different.  The
  chronological half-split sign check survives unchanged in spirit but is
  only evaluable when both halves carry ``MIN_HALF_MASS``; a cell whose
  halves are too thin to disagree does not get credit for agreeing.
* **Gate C, forward window.**  Required for every level and every stage.
  The pooled rule exempts P0 because its OOS gate archive re-scores that
  exact geometry; no gate archive exists at station-by-position
  granularity, so nothing here inherits the exemption.  The candidate is
  scored against what the cell would otherwise serve -- its own backoff,
  usually the pooled factor -- so the window measures the *increment* the
  finer cell buys, never the pooled correction it already inherits.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import log, sqrt
from typing import Any

import numpy as np

from .band_reliability_store import (
    BAND_AXIS_CLAIM,
    load_band_table_with_axis,
    BAND_STAGES,
    BAND_TABLE_CELLS,
    FACTOR_CEILING,
    FACTOR_FLOOR,
    FORWARD_WINDOW_FRACTION,
    _begin_immediate,
    _check_stage,
    _truth,
    load_band_table,
    serving_lane_by_station_day,
)
from .contracts import ContractError, content_sha256

SHADOW_METHOD_TAG = "band_shadow_station_position_v1"

# Signed offset from the claim's argmax bucket, coarsened.  Signed, not
# absolute: the 2026-08-28 probe found P0's deep tail realising 0.48 on the
# cool side against 1.47 on the warm side -- one shared factor is wrong for
# both, and folding the sign away would have hidden exactly that.
POSITION_GROUPS = (
    "cool_far",
    "cool_near",
    "centre",
    "warm_near",
    "warm_far",
)
BACKOFF_LEVELS = ("station_position", "station", "position", "pooled")

# Gate A.  Mass is the binding floor (relative SE ~ 1/sqrt(mass), so 8.0
# separates a 1.7x deviation and nothing finer); the day and row floors
# stop one weather day or one fat snapshot from carrying a cell alone.
MIN_CELL_CLAIMED_MASS = 8.0
MIN_CELL_DAYS = 8
MIN_CELL_ROWS = 30
# Gate B.  A half too thin to disagree does not get credit for agreeing.
MIN_HALF_MASS = 2.0
FDR_Q = 0.10
BOOTSTRAP_SAMPLES = 1000
BOOTSTRAP_SEED = 20260828
# One fit writes about three thousand cells and the fit changes every day
# as evidence lands, so an unpruned shadow adds ~0.5 MB/day to the SSOT
# for a layer nothing serves.  This store has already stopped the whole
# pipeline once by filling (2026-08-26), and a diagnostic layer is the
# last thing that should be holding that capacity: cells are pruned to
# the most recent fits per stage, while journal rows -- small, and the
# thing that makes the accumulation readable day over day -- are kept.
CELL_RETENTION_FITS = 14

_SHADOW_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_band_shadow_cell ("
    " shadow_sha256 TEXT NOT NULL CHECK(length(shadow_sha256)=64),"
    " stage TEXT NOT NULL,"
    " level TEXT NOT NULL,"
    " station_id TEXT NOT NULL,"
    " position_group TEXT NOT NULL,"
    " band_lower REAL NOT NULL,"
    " band_upper REAL NOT NULL,"
    " factor REAL NOT NULL,"
    " basis TEXT NOT NULL,"
    " served_by_level TEXT NOT NULL,"
    " claimed_mass REAL NOT NULL,"
    " hits INTEGER NOT NULL,"
    " day_count INTEGER NOT NULL,"
    " row_count INTEGER NOT NULL,"
    " ratio REAL,"
    " ci_low REAL,"
    " ci_high REAL,"
    " p_value REAL,"
    " q_threshold REAL,"
    " forward_nll_gain REAL,"
    " shrunk_factor REAL,"
    " PRIMARY KEY(shadow_sha256, stage, station_id, position_group,"
    "  band_lower)"
    ")"
)
_SHADOW_JOURNAL_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_band_shadow_journal ("
    " fit_id TEXT PRIMARY KEY,"
    " stage TEXT NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " shadow_sha256 TEXT NOT NULL CHECK(length(shadow_sha256)=64),"
    " serving_day_count INTEGER NOT NULL,"
    " serving_station_day_count INTEGER NOT NULL,"
    " cell_count INTEGER NOT NULL,"
    " promoted_cell_count INTEGER NOT NULL,"
    " pooled_fallback_cell_count INTEGER NOT NULL,"
    " forward_nll_gain REAL NOT NULL,"
    " outcome TEXT NOT NULL,"
    " evidence_json TEXT NOT NULL"
    ")"
)
_SHADOW_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_band_shadow_cell_stage"
    " ON calibration_band_shadow_cell(stage, level)",
    "CREATE INDEX IF NOT EXISTS idx_band_shadow_journal_stage_time"
    " ON calibration_band_shadow_journal(stage, fitted_at_utc)",
)


def ensure_shadow_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SHADOW_DDL)
    conn.execute(_SHADOW_JOURNAL_DDL)
    for statement in _SHADOW_INDEXES:
        conn.execute(statement)


def position_group(offset: int) -> str:
    """Coarsen a signed argmax offset into a fitting group."""

    if offset <= -2:
        return "cool_far"
    if offset == -1:
        return "cool_near"
    if offset == 0:
        return "centre"
    if offset == 1:
        return "warm_near"
    return "warm_far"


def _band_of(probability: float) -> tuple[float, float] | None:
    for cell in BAND_TABLE_CELLS:
        if cell[0] <= probability < cell[1]:
            return cell
    return None


def collect_shadow_sample(
    conn: sqlite3.Connection,
    truth: Mapping[tuple[str, str], float],
    *,
    stage: str = "P0",
) -> dict[str, Any]:
    """Per (day, station, position, band) claimed mass and hits.

    Built from the served store's own serving lane -- first decision per
    station-day, post-band rows inverted from their receipts -- so the
    shadow and the served table are fitted on byte-identical evidence and
    a difference between them is a difference of geometry, never of
    sample.
    """

    lane = serving_lane_by_station_day(conn, truth, stage=_check_stage(stage))
    cells: dict[
        tuple[str, str, tuple[float, float]], dict[str, Any]
    ] = defaultdict(
        lambda: {"by_day": defaultdict(lambda: [0.0, 0]), "rows": 0}
    )
    # Snapshot-level records keep the forward window able to renormalise a
    # whole distribution, which is what the served layer actually does.
    snapshots: list[dict[str, Any]] = []
    stations: set[str] = set()
    days: set[str] = set()
    for (station, event), station_day in lane.items():
        stations.add(str(station))
        days.add(str(event))
        for entries in station_day:
            argmax_index = max(
                range(len(entries)), key=lambda i: entries[i][0]
            )
            records = []
            for index, (claim, hit, _lower) in enumerate(entries):
                group = position_group(index - argmax_index)
                band = _band_of(float(claim))
                records.append({
                    "claim": float(claim),
                    "hit": int(hit),
                    "position_group": group,
                    "band": band,
                })
                if band is None:
                    continue
                cell = cells[(str(station), group, band)]
                day_acc = cell["by_day"][str(event)]
                day_acc[0] += float(claim)
                day_acc[1] += int(hit)
                cell["rows"] += 1
            snapshots.append({
                "station_id": str(station),
                "event_date": str(event),
                "records": records,
            })
    return {
        "cells": {
            key: {
                "by_day": {
                    day: (value[0], value[1])
                    for day, value in cell["by_day"].items()
                },
                "rows": cell["rows"],
            }
            for key, cell in cells.items()
        },
        "snapshots": snapshots,
        "stations": sorted(stations),
        "days": sorted(days),
    }


def _marginalise(
    cells: Mapping[tuple[str, str, tuple[float, float]], Mapping[str, Any]],
    *,
    level: str,
) -> dict[tuple[str, str, tuple[float, float]], dict[str, Any]]:
    """Collapse the finest cells onto a coarser backoff level's key."""

    if level == "station_position":
        return {key: dict(value) for key, value in cells.items()}
    merged: dict[
        tuple[str, str, tuple[float, float]], dict[str, Any]
    ] = defaultdict(
        lambda: {"by_day": defaultdict(lambda: [0.0, 0]), "rows": 0}
    )
    for (station, group, band), value in cells.items():
        if level == "station":
            key = (station, "*", band)
        elif level == "position":
            key = ("*", group, band)
        else:
            raise ContractError("band_shadow_level_invalid")
        target = merged[key]
        target["rows"] += int(value["rows"])
        for day, (claimed, hits) in value["by_day"].items():
            acc = target["by_day"][day]
            acc[0] += float(claimed)
            acc[1] += int(hits)
    return {
        key: {
            "by_day": {
                day: (value[0], value[1])
                for day, value in cell["by_day"].items()
            },
            "rows": cell["rows"],
        }
        for key, cell in merged.items()
    }


def _bootstrap_draws(days: Sequence[str]) -> np.ndarray:
    """One shared multinomial day-count matrix for every cell.

    Shared on purpose: cells drawn on different day samples would be
    independent by construction, which is the opposite of the truth --
    one weather day moves many stations at once, and the whole reason the
    day is the resampling unit is to keep that dependence in the interval.
    """

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    return rng.multinomial(
        len(days), np.full(len(days), 1.0 / len(days)), size=BOOTSTRAP_SAMPLES
    ).astype(np.float64)


def _cell_statistics(
    cell: Mapping[str, Any],
    *,
    days: Sequence[str],
    draws: np.ndarray,
) -> dict[str, Any]:
    """Point ratio, shared-draw bootstrap interval and two-sided p-value."""

    index = {day: position for position, day in enumerate(days)}
    claimed = np.zeros(len(days))
    hits = np.zeros(len(days))
    for day, (day_claimed, day_hits) in cell["by_day"].items():
        position = index.get(day)
        if position is None:
            continue
        claimed[position] = day_claimed
        hits[position] = day_hits
    total_claimed = float(claimed.sum())
    total_hits = float(hits.sum())
    stats: dict[str, Any] = {
        "claimed_mass": total_claimed,
        "hits": int(round(total_hits)),
        "day_count": int((claimed > 0).sum()),
        "row_count": int(cell["rows"]),
        "ratio": (total_hits / total_claimed) if total_claimed > 0 else None,
    }
    if total_claimed <= 0:
        stats.update({
            "ci_low": None,
            "ci_high": None,
            "p_value": 1.0,
            "halves_sign_stable": False,
            "half_masses": [0.0, 0.0],
        })
        return stats
    sampled_claimed = draws @ claimed
    sampled_hits = draws @ hits
    usable = sampled_claimed > 0
    ratios = sampled_hits[usable] / sampled_claimed[usable]
    if ratios.size < BOOTSTRAP_SAMPLES // 2:
        stats.update({
            "ci_low": None,
            "ci_high": None,
            "p_value": 1.0,
            "halves_sign_stable": False,
            "half_masses": [0.0, 0.0],
        })
        return stats
    low, high = np.quantile(ratios, [0.025, 0.975])
    # Two-sided bootstrap p-value: the smallest alpha at which the
    # interval would still exclude 1.0, which is what FDR needs to rank.
    below = float((ratios < 1.0).mean())
    p_value = min(1.0, 2.0 * min(below, 1.0 - below))
    cut = max(1, len(days) // 2)
    first_claimed = float(claimed[:cut].sum())
    second_claimed = float(claimed[cut:].sum())
    halves_stable = False
    if first_claimed >= MIN_HALF_MASS and second_claimed >= MIN_HALF_MASS:
        first = float(hits[:cut].sum()) / first_claimed
        second = float(hits[cut:].sum()) / second_claimed
        halves_stable = (first - 1.0) * (second - 1.0) > 0
    stats.update({
        "ci_low": float(low),
        "ci_high": float(high),
        "p_value": p_value,
        "halves_sign_stable": halves_stable,
        "half_masses": [first_claimed, second_claimed],
    })
    return stats


def _benjamini_hochberg(
    p_values: Mapping[Any, float], *, q: float = FDR_Q
) -> dict[Any, float]:
    """Per-key BH threshold; a key passes when its p is at or below it.

    Returned as thresholds rather than a boolean so the journal records
    how close a rejected cell came, which is what makes the accumulation
    legible day over day.
    """

    if not p_values:
        return {}
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    total = len(ordered)
    largest_pass = 0
    for rank, (_key, value) in enumerate(ordered, start=1):
        if value <= q * rank / total:
            largest_pass = rank
    cutoff = (
        q * largest_pass / total if largest_pass else 0.0
    )
    return {key: cutoff for key, _value in ordered}


def _promoted_key(
    station: str,
    group: str,
    band: tuple[float, float],
    served_by: str,
) -> tuple[str, str, tuple[float, float]] | None:
    """The backoff key whose promotion a fine cell is inheriting."""

    if served_by == "station_position":
        return (station, group, band)
    if served_by == "station":
        return (station, "*", band)
    if served_by == "position":
        return ("*", group, band)
    return None


def _conservative_factor(stats: Mapping[str, Any]) -> float:
    high = stats["ci_high"]
    low = stats["ci_low"]
    if high is None or low is None:
        return 1.0
    conservative = high if high < 1.0 else low
    return min(FACTOR_CEILING, max(FACTOR_FLOOR, float(conservative)))


def _shrunk_factor(
    stats: Mapping[str, Any], *, pooled_factor: float
) -> float:
    """Empirical-Bayes style shrink toward the pooled factor.

    Diagnostic only.  The ruling is pooled *fallback*, so a cell either
    earns its own factor or serves the pooled one; this column exists so
    the question "would partial pooling have done better" is answerable
    from the journal without refitting anything.
    """

    ratio = stats.get("ratio")
    mass = float(stats.get("claimed_mass") or 0.0)
    if ratio is None or mass <= 0:
        return float(pooled_factor)
    weight = mass / (mass + MIN_CELL_CLAIMED_MASS)
    value = weight * float(ratio) + (1.0 - weight) * float(pooled_factor)
    return min(FACTOR_CEILING, max(FACTOR_FLOOR, value))


def _forward_window_gain(
    snapshots: Sequence[Mapping[str, Any]],
    days: Sequence[str],
    *,
    base: Mapping[tuple[str, str, tuple[float, float]], float],
    candidate: Mapping[tuple[str, str, tuple[float, float]], float],
) -> float:
    """NLL improvement of candidate over base on the trailing window.

    Whole-snapshot renormalisation, because that is what the served layer
    does: a factor that lifts one bucket takes mass from its neighbours,
    and scoring the bucket alone would credit a change the distribution
    never makes.
    """

    if not days:
        return 0.0
    cut = max(1, int(len(days) * (1.0 - FORWARD_WINDOW_FRACTION)))
    window = set(days[cut:] or days[-1:])

    def factor_for(
        table: Mapping[tuple[str, str, tuple[float, float]], float],
        station: str,
        group: str,
        band: tuple[float, float] | None,
    ) -> float:
        if band is None:
            return 1.0
        for key in (
            (station, group, band),
            (station, "*", band),
            ("*", group, band),
            ("*", "*", band),
        ):
            if key in table:
                return float(table[key])
        return 1.0

    def nll(
        table: Mapping[tuple[str, str, tuple[float, float]], float],
    ) -> float:
        total = 0.0
        for snapshot in snapshots:
            if snapshot["event_date"] not in window:
                continue
            station = snapshot["station_id"]
            mass = 0.0
            corrected = []
            for record in snapshot["records"]:
                factor = factor_for(
                    table, station, record["position_group"], record["band"]
                )
                value = record["claim"] * factor
                corrected.append((value, record["hit"]))
                mass += value
            if mass <= 0:
                continue
            for value, hit in corrected:
                if hit:
                    total += -log(max(value / mass, 1e-9))
        return total

    return nll(base) - nll(candidate)


def fit_band_shadow_table(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
) -> dict[str, Any]:
    """Fit one stage's shadow table under the re-set admission rule.

    Walks the backoff hierarchy from finest to coarsest.  A cell is
    promoted at the first level where it clears all three gates against
    what it would otherwise serve; anything unpromoted at every level
    serves the pooled factor from the live served table.
    """

    stage_id = _check_stage(stage)
    truth = _truth(conn)
    sample = collect_shadow_sample(conn, truth, stage=stage_id)
    days = sample["days"]
    finest = sample["cells"]
    # Charter 105: the pooled table is this lane's BASE, and this lane's
    # cells are (position, claim band) -- so it can only read a pooled table
    # whose own cells are claim bands.  A distance-axis table's bounds are
    # distances; read as claims they all fall in the first cell, which would
    # hand every bucket that cell's factor and never reach the other four.
    # Measured on a real serving vector the day P0 moved: 0.00929 per bucket,
    # quietly wrong rather than loudly.  So the axis is checked, and a
    # mismatch stops the fit by name instead of scoring against a base the
    # lane misread.
    pooled_loaded = load_band_table_with_axis(conn, stage=stage_id)
    pooled_axis = (
        BAND_AXIS_CLAIM if pooled_loaded is None else pooled_loaded[2]
    )
    if pooled_axis != BAND_AXIS_CLAIM:
        return {
            "stage": stage_id,
            "cells": [],
            "evidence": {
                "schema": "band_shadow_fit_evidence_v1",
                "stage": stage_id,
                "serving_day_count": 0,
                "serving_station_day_count": 0,
                "accumulating": True,
                "blocked_by": "pooled_table_axis_not_claim",
                "pooled_table_axis": pooled_axis,
                "pooled_table_sha256": (
                    None if pooled_loaded is None else pooled_loaded[1]
                ),
            },
        }
    pooled = (
        {band: float(factor) for band, factor in pooled_loaded[0]}
        if pooled_loaded is not None
        else {band: 1.0 for band in BAND_TABLE_CELLS}
    )
    pooled_sha = None if pooled_loaded is None else pooled_loaded[1]
    # The base every candidate is scored against: what the shadow would
    # serve with no finer cell at all.  Scoring against identity instead
    # would credit each cell with the pooled correction it inherits.
    base_table: dict[tuple[str, str, tuple[float, float]], float] = {
        ("*", "*", band): factor for band, factor in pooled.items()
    }
    promoted: dict[tuple[str, str, tuple[float, float]], float] = {}
    decisions: dict[tuple[str, str, tuple[float, float]], dict[str, Any]] = {}
    level_reports: dict[str, dict[str, Any]] = {}
    if not days:
        return {
            "stage": stage_id,
            "cells": [],
            "evidence": {
                "schema": "band_shadow_fit_evidence_v1",
                "stage": stage_id,
                "serving_day_count": 0,
                "serving_station_day_count": 0,
                "accumulating": True,
                "pooled_table_sha256": pooled_sha,
                "levels": {},
            },
            "serving_day_count": 0,
            "serving_station_day_count": 0,
            "accumulating": True,
            "forward_nll_gain": 0.0,
        }
    draws = _bootstrap_draws(days)
    for level in BACKOFF_LEVELS[:-1]:
        level_cells = _marginalise(finest, level=level)
        statistics: dict[Any, dict[str, Any]] = {}
        eligible: dict[Any, float] = {}
        blocked: dict[str, int] = defaultdict(int)
        for key, cell in level_cells.items():
            if key in promoted:
                # Already earned at a finer level; a coarser cell must not
                # re-decide a station the fine level already answered.
                continue
            stats = _cell_statistics(cell, days=days, draws=draws)
            statistics[key] = stats
            if (
                stats["claimed_mass"] < MIN_CELL_CLAIMED_MASS
                or stats["day_count"] < MIN_CELL_DAYS
                or stats["row_count"] < MIN_CELL_ROWS
            ):
                stats["blocked_by"] = "gate_a_insufficient_mass"
                blocked["gate_a_insufficient_mass"] += 1
                continue
            if not stats["halves_sign_stable"]:
                stats["blocked_by"] = "gate_b_halves_not_stable"
                blocked["gate_b_halves_not_stable"] += 1
                continue
            eligible[key] = float(stats["p_value"])
        thresholds = _benjamini_hochberg(eligible)
        candidates: list[tuple[Any, float]] = []
        for key, p_value in eligible.items():
            stats = statistics[key]
            cutoff = thresholds.get(key, 0.0)
            stats["q_threshold"] = cutoff
            if p_value > cutoff or cutoff <= 0.0:
                stats["blocked_by"] = "gate_b_fdr_not_significant"
                blocked["gate_b_fdr_not_significant"] += 1
                continue
            candidates.append((key, _conservative_factor(stats)))
        # Gate C last: it is the only gate that costs a pass over the
        # forward window, so it runs on the handful of cells that already
        # cleared mass and significance rather than on every cell.
        accepted = 0
        for key, factor in sorted(candidates, key=lambda item: str(item[0])):
            stats = statistics[key]
            candidate_table = dict(base_table)
            candidate_table.update(promoted)
            candidate_table[key] = factor
            gain = _forward_window_gain(
                sample["snapshots"],
                days,
                base={**base_table, **promoted},
                candidate=candidate_table,
            )
            stats["forward_nll_gain"] = gain
            if gain <= 0:
                stats["blocked_by"] = "gate_c_forward_window_not_improved"
                blocked["gate_c_forward_window_not_improved"] += 1
                continue
            promoted[key] = factor
            stats["blocked_by"] = None
            decisions[key] = {
                "level": level,
                "factor": factor,
                "stats": stats,
            }
            accepted += 1
        level_reports[level] = {
            "tested": len(level_cells),
            "eligible": len(eligible),
            "promoted": accepted,
            "blocked": dict(blocked),
        }
    served_table = {**base_table, **promoted}
    cells: list[dict[str, Any]] = []
    for (station, group, band), cell in sorted(
        finest.items(), key=lambda item: str(item[0])
    ):
        stats = _cell_statistics(cell, days=days, draws=draws)
        served_by = "pooled"
        factor = pooled.get(band, 1.0)
        for key, level in (
            ((station, group, band), "station_position"),
            ((station, "*", band), "station"),
            (("*", group, band), "position"),
        ):
            if key in promoted:
                served_by = level
                factor = promoted[key]
                break
        # Two different cells describe one row and conflating them tells a
        # false story: the statistics columns are this fine cell's own --
        # its mass, its ratio, why it could not qualify -- while the factor
        # comes from whichever level actually earned it.  Read together
        # without the distinction, a row reading "ratio 0.00, factor 0.686"
        # says this station's cool tail was measured at zero and shrunk for
        # it, when what happened is that the station cell was too thin to
        # test at all and inherited the fleet's cool-tail factor.
        decision = decisions.get(_promoted_key(station, group, band, served_by))
        serving_stats = None if decision is None else decision["stats"]
        cells.append({
            "stage": stage_id,
            "level": "station_position",
            "station_id": station,
            "position_group": group,
            "band": band,
            "factor": float(factor),
            "served_by_level": served_by,
            "serving_ratio": (
                None if serving_stats is None else serving_stats["ratio"]
            ),
            "serving_claimed_mass": (
                None
                if serving_stats is None
                else serving_stats["claimed_mass"]
            ),
            "basis": (
                f"promoted:{served_by}"
                if served_by != "pooled"
                else "pooled_fallback:"
                + str(stats.get("blocked_by") or "not_tested")
            ),
            "claimed_mass": stats["claimed_mass"],
            "hits": stats["hits"],
            "day_count": stats["day_count"],
            "row_count": stats["row_count"],
            "ratio": stats["ratio"],
            "ci_low": stats["ci_low"],
            "ci_high": stats["ci_high"],
            "p_value": stats["p_value"],
            "q_threshold": stats.get("q_threshold"),
            "forward_nll_gain": (
                None
                if serving_stats is None
                else serving_stats.get("forward_nll_gain")
            ),
            "shrunk_factor": _shrunk_factor(
                stats, pooled_factor=pooled.get(band, 1.0)
            ),
        })
    total_gain = _forward_window_gain(
        sample["snapshots"], days, base=base_table, candidate=served_table
    )
    station_days = len({
        (snapshot["station_id"], snapshot["event_date"])
        for snapshot in sample["snapshots"]
    })
    return {
        "stage": stage_id,
        "cells": cells,
        "evidence": {
            "schema": "band_shadow_fit_evidence_v1",
            "stage": stage_id,
            "serving_day_count": len(days),
            "serving_station_day_count": station_days,
            "station_count": len(sample["stations"]),
            "accumulating": False,
            "pooled_table_sha256": pooled_sha,
            "gate_a": {
                "min_claimed_mass": MIN_CELL_CLAIMED_MASS,
                "min_days": MIN_CELL_DAYS,
                "min_rows": MIN_CELL_ROWS,
            },
            "gate_b": {"fdr_q": FDR_Q, "min_half_mass": MIN_HALF_MASS},
            "levels": level_reports,
            "promoted": {
                str(list(key)): value for key, value in promoted.items()
            },
            "forward_nll_gain": total_gain,
        },
        "serving_day_count": len(days),
        "serving_station_day_count": station_days,
        "accumulating": False,
        "forward_nll_gain": total_gain,
    }


def shadow_sha256(cells: Sequence[Mapping[str, Any]], *, stage: str) -> str:
    return content_sha256({
        "schema": "band_shadow_table_v1",
        "stage": _check_stage(stage),
        "cells": [
            [
                cell["station_id"],
                cell["position_group"],
                [cell["band"][0], cell["band"][1]],
                round(float(cell["factor"]), 12),
                cell["served_by_level"],
            ]
            for cell in sorted(
                cells,
                key=lambda item: (
                    item["station_id"],
                    item["position_group"],
                    item["band"][0],
                ),
            )
        ],
    })


def persist_shadow_fit(
    conn: sqlite3.Connection,
    *,
    stage: str,
    cells: Sequence[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    serving_day_count: int,
    serving_station_day_count: int,
    forward_nll_gain: float,
) -> dict[str, Any]:
    """Write one shadow fit and its journal row.  Nothing is activated.

    There is no ``activated`` column anywhere in this module: a shadow
    table with an activation flag is one config change away from being
    served, and the whole point is that reaching serving has to be a code
    change somebody reviews.
    """

    stage_id = _check_stage(stage)
    ensure_shadow_schema(conn)
    sha = shadow_sha256(cells, stage=stage_id)
    fitted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    promoted_count = sum(
        1 for cell in cells if cell["served_by_level"] != "pooled"
    )
    fit_id = "band-shadow-" + content_sha256({
        "stage": stage_id,
        "shadow_sha256": sha,
        "fitted_at_utc": fitted_at,
    })[:24]
    _begin_immediate(conn)
    try:
        existing = conn.execute(
            "SELECT COUNT(*) FROM calibration_band_shadow_cell"
            " WHERE shadow_sha256=? AND stage=?",
            (sha, stage_id),
        ).fetchone()[0]
        if not existing:
            conn.executemany(
                "INSERT INTO calibration_band_shadow_cell"
                " (shadow_sha256, stage, level, station_id, position_group,"
                "  band_lower, band_upper, factor, basis, served_by_level,"
                "  claimed_mass, hits, day_count, row_count, ratio,"
                "  ci_low, ci_high, p_value, q_threshold, forward_nll_gain,"
                "  shrunk_factor)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        sha,
                        stage_id,
                        cell["level"],
                        cell["station_id"],
                        cell["position_group"],
                        float(cell["band"][0]),
                        float(cell["band"][1]),
                        float(cell["factor"]),
                        cell["basis"],
                        cell["served_by_level"],
                        float(cell["claimed_mass"]),
                        int(cell["hits"]),
                        int(cell["day_count"]),
                        int(cell["row_count"]),
                        cell["ratio"],
                        cell["ci_low"],
                        cell["ci_high"],
                        cell["p_value"],
                        cell["q_threshold"],
                        cell["forward_nll_gain"],
                        cell["shrunk_factor"],
                    )
                    for cell in cells
                ],
            )
        conn.execute(
            "INSERT OR REPLACE INTO calibration_band_shadow_journal"
            " (fit_id, stage, fitted_at_utc, shadow_sha256,"
            "  serving_day_count, serving_station_day_count, cell_count,"
            "  promoted_cell_count, pooled_fallback_cell_count,"
            "  forward_nll_gain, outcome, evidence_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fit_id,
                stage_id,
                fitted_at,
                sha,
                int(serving_day_count),
                int(serving_station_day_count),
                len(cells),
                promoted_count,
                len(cells) - promoted_count,
                float(forward_nll_gain),
                "PROMOTED_CELLS" if promoted_count else "ALL_POOLED",
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            ),
        )
        pruned = _prune_old_cells(conn, stage=stage_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "fit_id": fit_id,
        "shadow_sha256": sha,
        "stage": stage_id,
        "cell_count": len(cells),
        "promoted_cell_count": promoted_count,
        "pruned_cell_count": pruned,
        "state": "RECORDED" if existing else "WRITTEN",
    }


def _prune_old_cells(conn: sqlite3.Connection, *, stage: str) -> int:
    """Drop cells older than the retention window; keep every journal row.

    Retention is by fit, not by age: a stage that stops being fitted keeps
    its last fits intact rather than emptying itself over a quiet week,
    and a stage fitted daily holds a fortnight of comparable tables.
    """

    keep = [
        row[0]
        for row in conn.execute(
            "SELECT shadow_sha256 FROM calibration_band_shadow_journal"
            " WHERE stage=? GROUP BY shadow_sha256"
            " ORDER BY MAX(fitted_at_utc) DESC LIMIT ?",
            (stage, CELL_RETENTION_FITS),
        )
    ]
    if not keep:
        return 0
    placeholders = ",".join("?" for _ in keep)
    cursor = conn.execute(
        "DELETE FROM calibration_band_shadow_cell"
        f" WHERE stage=? AND shadow_sha256 NOT IN ({placeholders})",
        (stage, *keep),
    )
    return int(cursor.rowcount or 0)


def refit_shadow_all_stages(
    conn: sqlite3.Connection, *, commit: bool = False
) -> dict[str, Any]:
    """Fit every stage's shadow table, isolated the way the served one is.

    A stage that throws records its error and the others still land: the
    trail of what was known on a day has to survive the day's accidents.
    """

    results: dict[str, Any] = {}
    for stage in BAND_STAGES:
        entry: dict[str, Any] = {"stage": stage}
        results[stage] = entry
        try:
            fitted = fit_band_shadow_table(conn, stage=stage)
            promoted = [
                cell
                for cell in fitted["cells"]
                if cell["served_by_level"] != "pooled"
            ]
            entry.update({
                "shadow_sha256": shadow_sha256(
                    fitted["cells"], stage=stage
                ),
                "cell_count": len(fitted["cells"]),
                "promoted_cell_count": len(promoted),
                "serving_day_count": fitted["serving_day_count"],
                "serving_station_day_count": fitted[
                    "serving_station_day_count"
                ],
                "forward_nll_gain": fitted["forward_nll_gain"],
                # Per level, not just a total: a total counts every fine
                # cell that inherits a coarse promotion, so "209 of 1091"
                # reads as 209 station-specific factors when the fit in
                # fact promoted five fleet-wide position cells and let 209
                # fine cells inherit them.
                "promoted_cells_by_level": {
                    level: sum(
                        1
                        for cell in promoted
                        if cell["served_by_level"] == level
                    )
                    for level in BACKOFF_LEVELS[:-1]
                },
                "promoted_keys_by_level": {
                    level: report["promoted"]
                    for level, report in fitted["evidence"]["levels"].items()
                },
                "levels": fitted["evidence"]["levels"],
                "promoted": [
                    {
                        "station_id": cell["station_id"],
                        "position_group": cell["position_group"],
                        "band": list(cell["band"]),
                        "factor": cell["factor"],
                        "level": cell["served_by_level"],
                        "ratio": cell["ratio"],
                        "claimed_mass": cell["claimed_mass"],
                    }
                    for cell in promoted
                ],
            })
            if commit:
                entry["commit"] = persist_shadow_fit(
                    conn,
                    stage=stage,
                    cells=fitted["cells"],
                    evidence=fitted["evidence"],
                    serving_day_count=fitted["serving_day_count"],
                    serving_station_day_count=fitted[
                        "serving_station_day_count"
                    ],
                    forward_nll_gain=fitted["forward_nll_gain"],
                )
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
    return results
