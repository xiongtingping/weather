"""DB-resident, content-addressed band reliability table.

The band factors used to be code constants gated by human review; the
review step is gone (user ruling, 2026-08-15) and this module is what
replaced it.  The daily calibration cron fits a fresh table from
accumulated truth with the same admission rule the constants were chosen
by, persists it content-addressed, and activates it.  Nothing here loosens
the rule -- automation replaced the reviewer, not the hurdles.

The invariant that shapes the design: an artifact must be served with the
exact table its holdout was scored against.  Every OOS candidate scored
against a DB table carries ``band_table_sha256=<sha>`` inside its
combination method, and P0 loads that pinned version -- never "the latest".
Artifacts whose method carries the bare tag predate the store and keep
serving the static bootstrap constants in ``cross_consensus``.

Admission rule per cell (fixed; the table changes, the rule does not):

* serving lane (market buckets, bridge history): by-day bootstrap interval
  excludes 1.0 AND chronological half-split keeps the miscalibration sign
  -> serve the interval end nearest one.
* else transfer lane: the gate-geometry archive qualifies the cell, its
  direction agrees with the serving lane's point estimate, and adding the
  cell improves NLL on the serving lane's last-40% forward window.
* else identity.

Sample growth: post-band serving rows are NOT excluded -- their receipts
record the applied factors, so the pre-band claim is reconstructed exactly
and the fitting sample keeps growing.  The gate lane cannot be inverted
(its predictions carry no receipt), so banded gate rows are excluded;
that lane only confirms transfer direction and its freeze is acceptable.
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import log
from typing import Any

from .contracts import ContractError, content_sha256

BAND_TABLE_CELLS = (
    (0.00, 0.02),
    (0.02, 0.12),
    (0.12, 0.20),
    (0.20, 0.35),
    (0.35, 1.01),
)
FINE_BANDS = (
    (0.00, 0.02),
    (0.02, 0.05),
    (0.05, 0.08),
    (0.08, 0.12),
    (0.02, 0.12),
    (0.12, 0.20),
    (0.20, 0.35),
    (0.35, 1.01),
)
# A qualified cell still serves within these bounds.  The interval end
# nearest one is modest by construction; the clamp only guards a future
# degenerate fit (a handful of days, a wild interval) from serving an
# extreme factor without anyone in the loop.
FACTOR_FLOOR = 0.5
FACTOR_CEILING = 2.0
FORWARD_WINDOW_FRACTION = 0.4

_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_band_reliability_table ("
    " band_table_sha256 TEXT PRIMARY KEY"
    "  CHECK(length(band_table_sha256)=64),"
    " factors_json TEXT NOT NULL,"
    " evidence_json TEXT NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " serving_day_count INTEGER NOT NULL,"
    " activated INTEGER NOT NULL CHECK(activated IN (0,1)),"
    " activated_at_utc TEXT"
    ")"
)


def ensure_band_table_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_DDL)


def _canonical_factors(
    factors: Sequence[tuple[tuple[float, float], float]],
) -> list[list[Any]]:
    return [
        [[float(lower), float(upper)], float(factor)]
        for (lower, upper), factor in factors
    ]


def band_table_sha256(
    factors: Sequence[tuple[tuple[float, float], float]],
) -> str:
    return content_sha256({
        "schema": "band_reliability_table_v1",
        "factors": _canonical_factors(factors),
    })


def load_band_table(
    conn: sqlite3.Connection,
    *,
    table_sha256: str | None = None,
) -> tuple[tuple[tuple[tuple[float, float], float], ...], str] | None:
    """The active table, or the pinned version an artifact names.

    A pinned sha that is missing fails closed: the artifact's gated math
    cannot be reproduced, so it must not be served an approximation.
    """

    ensure_band_table_schema(conn)
    if table_sha256 is None:
        row = conn.execute(
            "SELECT band_table_sha256, factors_json"
            " FROM calibration_band_reliability_table"
            " WHERE activated=1"
        ).fetchone()
        if row is None:
            return None
    else:
        row = conn.execute(
            "SELECT band_table_sha256, factors_json"
            " FROM calibration_band_reliability_table"
            " WHERE band_table_sha256=?",
            (str(table_sha256),),
        ).fetchone()
        if row is None:
            raise ContractError(
                "band_reliability_pinned_table_missing"
            )
    factors = tuple(
        ((float(cell[0][0]), float(cell[0][1])), float(cell[1]))
        for cell in json.loads(row[1])
    )
    return factors, str(row[0])


def persist_and_activate_band_table(
    conn: sqlite3.Connection,
    *,
    factors: Sequence[tuple[tuple[float, float], float]],
    evidence: Mapping[str, Any],
    fitted_at_utc: str | None = None,
    serving_day_count: int,
) -> dict[str, Any]:
    """Insert the fitted table and make it the active one.

    Idempotent by content: refitting the same factors re-activates the
    existing row rather than growing the table.
    """

    ensure_band_table_schema(conn)
    sha = band_table_sha256(factors)
    now = fitted_at_utc or (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    active = conn.execute(
        "SELECT band_table_sha256 FROM calibration_band_reliability_table"
        " WHERE activated=1"
    ).fetchone()
    if active is not None and str(active[0]) == sha:
        return {"band_table_sha256": sha, "state": "UNCHANGED"}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE calibration_band_reliability_table SET activated=0"
            " WHERE activated=1"
        )
        conn.execute(
            "INSERT INTO calibration_band_reliability_table"
            " (band_table_sha256, factors_json, evidence_json,"
            "  fitted_at_utc, serving_day_count, activated,"
            "  activated_at_utc)"
            " VALUES (?,?,?,?,?,1,?)"
            " ON CONFLICT(band_table_sha256) DO UPDATE SET"
            "  activated=1, activated_at_utc=excluded.activated_at_utc",
            (
                sha,
                json.dumps(
                    _canonical_factors(factors), sort_keys=True
                ),
                json.dumps(dict(evidence), sort_keys=True),
                now,
                int(serving_day_count),
                now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "band_table_sha256": sha,
        "state": "ACTIVATED",
        "previous": None if active is None else str(active[0]),
    }


def pinned_band_table_sha(combination_method: str) -> str | None:
    for part in str(combination_method).split("::"):
        if part.startswith("band_table_sha256="):
            return part.split("=", 1)[1]
    return None


def _truth(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    return {
        (str(station), str(day)): float(value)
        for station, day, value in conn.execute(
            "SELECT station_id, local_date, official_tmax_c"
            " FROM calibration_station_daily_high_truth"
        )
    }


def _invert_band_receipt(
    records: list[tuple[float, str]],
    receipt: Mapping[str, Any],
) -> list[tuple[float, str]]:
    """Reconstruct pre-band claims from a served row's receipt.

    Serving computed ``served_i = raw_i * f_i / total`` and recorded both
    ``f_i`` (applied_factors, identity omitted) and ``total``
    (pre_normalisation_mass), so the inversion is exact, not estimated.
    """

    total = float(receipt.get("pre_normalisation_mass") or 0.0)
    applied = {
        str(key): float(value)
        for key, value in (receipt.get("applied_factors") or {}).items()
    }
    if total <= 0.0:
        raise ContractError("band_reliability_receipt_mass_invalid")
    return [
        (probability * total / applied.get(key, 1.0), key)
        for probability, key in records
    ]


def serving_lane_by_day(
    conn: sqlite3.Connection,
    truth: Mapping[tuple[str, str], float],
) -> dict[str, list[list[tuple[float, int]]]]:
    """Last P0 snapshot per station-day, pre-band claims, market buckets."""

    by_day: dict[str, list[list[tuple[float, int]]]] = defaultdict(list)
    rows = conn.execute(
        "SELECT h.station_id, h.event_date, h.history_id,"
        " h.envelope_json, MAX(h.decision_at_utc)"
        " FROM basket_probability_bridge_history h WHERE h.stage='P0'"
        " GROUP BY h.station_id, h.event_date"
    ).fetchall()
    for station, event, history_id, envelope_json, _latest in rows:
        actual = truth.get((str(station), str(event)))
        if actual is None:
            continue
        receipt = None
        if "band_reliability_json" in str(envelope_json):
            lineage = (
                json.loads(envelope_json).get("lineage") or {}
            )
            raw = lineage.get("band_reliability_json")
            parsed = json.loads(raw) if raw else None
            if parsed and parsed.get("state") == "APPLIED":
                receipt = parsed
        buckets = conn.execute(
            "SELECT bucket, lower_c, upper_c, probability"
            " FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=?",
            (history_id,),
        ).fetchall()
        if not buckets:
            continue
        keyed = [
            (float(probability), str(bucket))
            for bucket, _lower, _upper, probability in buckets
        ]
        if receipt is not None:
            keyed = _invert_band_receipt(keyed, receipt)
        hits = {}
        for bucket, lower, upper, _probability in buckets:
            lower_c = -1e9 if lower is None else float(lower)
            upper_c = 1e9 if upper is None else float(upper)
            hits[str(bucket)] = 1 if lower_c <= actual < upper_c else 0
        by_day[str(event)].append([
            (probability, hits[key]) for probability, key in keyed
        ])
    return dict(by_day)


def gate_lane_by_day(
    conn: sqlite3.Connection,
    truth: Mapping[tuple[str, str], float],
) -> dict[str, list[list[tuple[float, int]]]]:
    """Latest-vintage pre-band OOS predictions, 1C model buckets."""

    by_day: dict[str, list[list[tuple[float, int]]]] = defaultdict(list)
    rows = conn.execute(
        "SELECT p.station_id, p.event_date, p.probabilities_json,"
        " p.bucket_centers_c_json, a.combination_method,"
        " MAX(p.trained_through_event_date || p.decision_at_utc)"
        " FROM calibration_cross_native_ensemble_oos_prediction p"
        " JOIN calibration_cross_native_ensemble_artifact a"
        " ON a.cross_ensemble_artifact_id = p.cross_ensemble_artifact_id"
        " GROUP BY p.station_id, p.event_date, p.lead_bucket"
    ).fetchall()
    for station, event, probabilities_json, centers_json, method, _ in rows:
        actual = truth.get((str(station), str(event)))
        if actual is None:
            continue
        if "band_reliability" in str(method):
            continue
        probabilities = json.loads(probabilities_json)
        centers = json.loads(centers_json)
        records = []
        for key in sorted(
            probabilities, key=lambda k: float(centers.get(k, k))
        ):
            center = float(centers.get(key, key))
            records.append((
                float(probabilities[key]),
                1 if (center - 0.5) <= actual < (center + 0.5) else 0,
            ))
        by_day[str(event)].append(records)
    return dict(by_day)


def _day_aggregates(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    bands: Sequence[tuple[float, float]],
) -> dict[str, dict[tuple[float, float], list[float]]]:
    aggregates: dict[str, dict[tuple[float, float], list[float]]] = {}
    for day, rows in by_day.items():
        cells = {band: [0.0, 0.0] for band in bands}
        for records in rows:
            for probability, hit in records:
                for band in bands:
                    if band[0] <= probability < band[1]:
                        cells[band][0] += probability
                        cells[band][1] += hit
        aggregates[day] = cells
    return aggregates


def band_lane_report(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    *,
    bands: Sequence[tuple[float, float]] = FINE_BANDS,
    bootstrap_samples: int = 1000,
    seed: int = 17,
) -> dict[tuple[float, float], dict[str, Any]]:
    days = sorted(by_day)
    aggregates = _day_aggregates(by_day, bands)

    def ratios(sample: Sequence[str]) -> dict[tuple[float, float], float | None]:
        out = {}
        for band in bands:
            claimed = sum(aggregates[d][band][0] for d in sample)
            realised = sum(aggregates[d][band][1] for d in sample)
            out[band] = realised / claimed if claimed > 0 else None
        return out

    point = ratios(days)
    random.seed(seed)
    bootstrap: dict[tuple[float, float], list[float]] = {
        band: [] for band in bands
    }
    for _ in range(bootstrap_samples):
        sample = [random.choice(days) for _ in days]
        sampled = ratios(sample)
        for band in bands:
            if sampled[band] is not None:
                bootstrap[band].append(sampled[band])
    first = ratios(days[: len(days) // 2])
    second = ratios(days[len(days) // 2:])
    report: dict[tuple[float, float], dict[str, Any]] = {}
    for band in bands:
        ordered = sorted(bootstrap[band])
        if not ordered or point[band] is None:
            continue
        low = ordered[int(0.025 * len(ordered))]
        high = ordered[int(0.975 * len(ordered))]
        sign_stable = (
            first[band] is not None
            and second[band] is not None
            and (first[band] - 1.0) * (second[band] - 1.0) > 0
        )
        qualified = not (low <= 1.0 <= high) and sign_stable
        conservative = (high if high < 1.0 else low) if qualified else 1.0
        report[band] = {
            "ratio": point[band],
            "interval": [low, high],
            "halves": [first[band], second[band]],
            "sign_stable": sign_stable,
            "qualified": qualified,
            "conservative_factor": min(
                FACTOR_CEILING, max(FACTOR_FLOOR, conservative)
            ),
        }
    return report


def _forward_nll_delta(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    *,
    base_factors: Mapping[tuple[float, float], float],
    candidate_factors: Mapping[tuple[float, float], float],
) -> float:
    """NLL improvement of candidate over base on the trailing window."""

    days = sorted(by_day)
    cut = max(1, int(len(days) * (1.0 - FORWARD_WINDOW_FRACTION)))
    window = days[cut:] or days[-1:]

    def nll(factors: Mapping[tuple[float, float], float]) -> float:
        total = 0.0
        for day in window:
            for records in by_day[day]:
                mass = 0.0
                corrected = []
                for probability, hit in records:
                    factor = 1.0
                    for band, value in factors.items():
                        if band[0] <= probability < band[1]:
                            factor = value
                            break
                    corrected.append((probability * factor, hit))
                    mass += probability * factor
                for probability, hit in corrected:
                    if hit:
                        total += -log(max(probability / mass, 1e-9))
        return total

    return nll(base_factors) - nll(candidate_factors)


def fit_band_reliability_table(
    conn: sqlite3.Connection,
    *,
    bootstrap_samples: int = 1000,
) -> dict[str, Any]:
    """Fit today's table from both lanes under the fixed admission rule."""

    truth = _truth(conn)
    serving = serving_lane_by_day(conn, truth)
    gate = gate_lane_by_day(conn, truth)
    if len(serving) < 10:
        raise ContractError("band_reliability_serving_sample_too_thin")
    serving_report = band_lane_report(
        serving,
        bands=BAND_TABLE_CELLS,
        bootstrap_samples=bootstrap_samples,
    )
    gate_report = band_lane_report(
        gate,
        bands=BAND_TABLE_CELLS,
        bootstrap_samples=bootstrap_samples,
    )
    factors: dict[tuple[float, float], float] = {}
    basis: dict[str, str] = {}
    for band in BAND_TABLE_CELLS:
        serving_cell = serving_report.get(band)
        if serving_cell is None:
            factors[band] = 1.0
            basis[str(list(band))] = "no_sample"
            continue
        if serving_cell["qualified"]:
            factors[band] = serving_cell["conservative_factor"]
            basis[str(list(band))] = "serving_lane"
            continue
        factors[band] = 1.0
        basis[str(list(band))] = "identity"
    for band in BAND_TABLE_CELLS:
        if factors[band] != 1.0:
            continue
        serving_cell = serving_report.get(band)
        gate_cell = gate_report.get(band)
        if serving_cell is None or gate_cell is None:
            continue
        if not gate_cell["qualified"]:
            continue
        # Transfer needs direction agreement with the serving lane's own
        # point estimate; the deep tail's geometry reversal is exactly what
        # this refuses.
        if (
            (gate_cell["ratio"] - 1.0)
            * (serving_cell["ratio"] - 1.0)
            <= 0
        ):
            continue
        candidate = dict(factors)
        candidate[band] = gate_cell["conservative_factor"]
        if _forward_nll_delta(
            serving,
            base_factors=factors,
            candidate_factors=candidate,
        ) > 0:
            factors[band] = gate_cell["conservative_factor"]
            basis[str(list(band))] = "gate_transfer"
    def _cell_json(report: Mapping[tuple[float, float], dict[str, Any]]):
        return {
            str(list(band)): {
                key: value
                for key, value in cell.items()
            }
            for band, cell in report.items()
        }
    return {
        "factors": tuple(
            (band, factors[band]) for band in BAND_TABLE_CELLS
        ),
        "evidence": {
            "schema": "band_reliability_fit_evidence_v1",
            "serving_day_count": len(serving),
            "gate_day_count": len(gate),
            "basis": basis,
            "serving_report": _cell_json(serving_report),
            "gate_report": _cell_json(gate_report),
        },
        "serving_day_count": len(serving),
    }
