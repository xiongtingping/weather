"""DB-resident, band-axis-partitioned table for the off-centre claim
correction.

A window that excludes the model's own centre bucket is over-claimed, and by
a measurable amount -- that much has been the rule since 2026-08-14.  What
was NOT true until now is that the amount was measured more than once.  The
2026-08-14 fit left no code behind (``scripts/measure_off_center_claim.py``
was written three weeks later), its off-centre cells never recorded an n,
and it was taken the day before charter 49 made centre containment a
structural admission floor -- so it scored a population the ladder ruling
then removed.  The 2026-09-08 re-measurement corrected three cells to 1.0
and was itself taken on claim-axis band vintages, days after P0 had moved to
the distance axis.  Two hand fits, two vintages, no lane.

This is the lane.  The correction now refits on the settled window, on the
band axis that serves TODAY, and activates itself under an admission rule --
the same shape charter 68's lambda and the band table already have.

The partition key is the BAND AXIS, for the reason charter 110 gives about
lambda: the band table moves every claim, the correction is measured on
claims, and a table fitted on one axis describes a quantity the other does
not compute.  A table fitted on an axis that is no longer served is not
loaded; the scope falls back to the kernel bootstrap and says so.

Not partitioned on the band table's SHA.  Band tables are refitted nightly
and the sha moves whenever a factor shifts in its third decimal; keying on
it would end the fit rather than purify it.  Same reasoning, same words, as
``effective_edge_store._fit_band_sha``.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
from datetime import UTC, datetime
from typing import Any, Sequence

from .band_reliability_store import (
    BAND_AXIS_CLAIM,
    BAND_AXES,
    band_table_axis,
    load_band_table,
    serving_lane_by_station_day,
    _truth,
)
from .basket_kernel.delta_v2 import OFF_CENTER_CLAIM_BANDS
from .contracts import content_sha256

SCHEMA = "off_centre_claim_table_v1"
# The mass bands the correction is fitted on.  Identical to the 2026-08-14
# measurement's, because a re-fit that also re-cut the bands would be
# measuring two changes at once.
CLAIM_BANDS: tuple[tuple[float, float], ...] = (
    (0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.55), (0.55, 1.01),
)
# Four-bucket contiguous windows, the width the original fit used.
WINDOW_WIDTH = 4
# The centre definition the served correction keys on: charter 42's point
# estimate, which is what ``model_center_bucket`` carries and what
# ``_support_windows`` compares the window against.
CENTRE_DEFINITION = "argmax"
# Shrunk toward one rather than served at the point estimate, as the hand
# fits were.
SHRINK_TOWARD_ONE = 0.30
# A cell needs this much before it may move the served number.  The floor is
# the reason the 0.48 could stand for three weeks: it was fitted on a cell
# that never recorded an n at all.
MIN_CELL_ROWS = 200
MIN_CELL_DAYS = 10
BOOTSTRAP_DRAWS = 400
BOOTSTRAP_SEED = 20260909
# A correction outside this band is a fit going wrong, not a claim being
# wrong; the hand tables never left it either.
FACTOR_FLOOR = 0.50
FACTOR_CEILING = 1.50

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS calibration_off_centre_claim_table (
  table_sha256 TEXT PRIMARY KEY CHECK(length(table_sha256)=64),
  stage TEXT NOT NULL,
  band_axis TEXT NOT NULL,
  factors_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  fitted_at_utc TEXT NOT NULL,
  serving_day_count INTEGER NOT NULL,
  activated INTEGER NOT NULL CHECK(activated IN (0,1)),
  activated_at_utc TEXT
)
"""
_ACTIVE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_off_centre_claim_active"
    " ON calibration_off_centre_claim_table(stage, band_axis, activated)"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_DDL)
    conn.execute(_ACTIVE_INDEX)


def bootstrap_bands() -> tuple[tuple[float, float], ...]:
    """The kernel constant a stage serves before any fit is activated."""

    return tuple((float(b), float(f)) for b, f in OFF_CENTER_CLAIM_BANDS)


def load_off_centre_bands(
    conn: sqlite3.Connection, *, stage: str = "P0"
) -> tuple[tuple[tuple[float, float], ...], str] | None:
    """The stage's activated table for the axis it serves today, or None.

    None means "serve the bootstrap": either nothing has been fitted, or
    everything fitted was fitted on an axis this stage no longer serves.
    The second case is the one that matters and it is silent by design --
    a table from another axis is not a stale table, it is a table about a
    different quantity.
    """

    # No ensure_schema here: the serving side reads through a read-only
    # connection, and a missing table is "serve the bootstrap", not an error.
    try:
        axis = band_table_axis(conn, stage=stage)
        row = conn.execute(
            "SELECT factors_json, table_sha256 FROM"
            " calibration_off_centre_claim_table"
            " WHERE activated=1 AND stage=? AND band_axis=?",
            (str(stage), str(axis)),
        ).fetchone()
    except sqlite3.Error:
        return None
    except Exception:  # noqa: BLE001 -- no band store, no axis, no table
        return None
    if row is None:
        return None
    try:
        factors = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None
    bands = tuple(
        (float(bound), float(factor)) for bound, factor in factors
    )
    return (bands, str(row[1])) if bands else None


def _band_of(mass: float) -> tuple[float, float] | None:
    for lo, hi in CLAIM_BANDS:
        if lo <= mass < hi:
            return (lo, hi)
    return None


def _ratio(rows: Sequence[tuple[float, float]]) -> tuple[float, int]:
    claimed = sum(row[0] for row in rows)
    hits = sum(row[1] for row in rows)
    return (hits / claimed if claimed > 0 else float("nan")), len(rows)


def _bootstrap(
    rows_by_day: dict[str, list[tuple[float, float]]]
) -> tuple[float, float]:
    """Resampled over DAYS: stations share a day's weather, so the day is the
    independent unit."""

    days = list(rows_by_day)
    if not days:
        return (float("nan"), float("nan"))
    rng = random.Random(BOOTSTRAP_SEED)
    out = []
    for _ in range(BOOTSTRAP_DRAWS):
        pick = [rng.choice(days) for _ in days]
        claimed = sum(r[0] for d in pick for r in rows_by_day[d])
        hits = sum(r[1] for d in pick for r in rows_by_day[d])
        out.append(hits / claimed if claimed > 0 else float("nan"))
    out = [x for x in out if not math.isnan(x)]
    if not out:
        return (float("nan"), float("nan"))
    out.sort()
    lo = out[int(0.025 * len(out))]
    hi = out[min(len(out) - 1, int(0.975 * len(out)))]
    return (lo, hi)


def fit_off_centre_claim_table(
    conn: sqlite3.Connection, *, stage: str = "P0"
) -> dict[str, Any]:
    """Measure realized containment over claimed mass, off-centre windows.

    The same statistic as the 2026-08-14 fit and
    ``scripts/measure_off_center_claim.py``: contiguous windows of
    ``WINDOW_WIDTH`` buckets, split by whether the window holds the model's
    argmax bucket, aggregated by claimed-mass band.  The sample and the
    truth come from the band store's own helpers, so nothing here
    re-implements a production rule except the window enumeration.
    """

    # Read-only is a legitimate caller here too: a dry-run fit reports and
    # persists nothing.
    try:
        ensure_schema(conn)
    except sqlite3.Error:
        pass
    if load_band_table(conn, stage=stage) is None:
        return {"stage": stage, "error": "no_activated_band_table"}
    axis = band_table_axis(conn, stage=stage)
    if axis not in BAND_AXES:
        return {"stage": stage, "error": f"unknown_band_axis:{axis}"}
    sample = serving_lane_by_station_day(
        conn, _truth(conn), stage=stage, pre_band=False
    )
    cells: dict[tuple[float, float], dict[str, list[tuple[float, float]]]] = {}
    days: set[str] = set()
    for (_station, event), snaps in sample.items():
        entries = snaps[0]
        claims = [c for c, _h, _l in entries]
        hits = [h for _c, h, _l in entries]
        n = len(claims)
        if n < WINDOW_WIDTH or sum(claims) <= 0:
            continue
        days.add(str(event))
        argmax = max(range(n), key=lambda i: (claims[i], -i))
        for start in range(0, n - WINDOW_WIDTH + 1):
            window = range(start, start + WINDOW_WIDTH)
            if argmax in window:
                continue
            claim = sum(claims[i] for i in window)
            hit = 1.0 if any(hits[i] for i in window) else 0.0
            band = _band_of(claim)
            if band is None:
                continue
            cells.setdefault(band, {}).setdefault(str(event), []).append(
                (claim, hit)
            )
    incumbent = dict(
        (load_off_centre_bands(conn, stage=stage) or (bootstrap_bands(), ""))[0]
    )
    # Windows under the lowest measured band get no correction.  The hand
    # table served 1.15 there -- a LIFT, on a cell the measurement script has
    # never reported because its own bands start at 0.05 -- so the number had
    # no evidence behind it in either direction.  Deflation-only, and no
    # evidence, means one.
    served: list[tuple[float, float]] = [(float(CLAIM_BANDS[0][0]), 1.0)]
    evidence: list[dict[str, Any]] = []
    for lo, hi in CLAIM_BANDS:
        by_day = cells.get((lo, hi), {})
        rows = [row for day in by_day for row in by_day[day]]
        ratio, count = _ratio(rows)
        ci_lo, ci_hi = _bootstrap(by_day)
        held = float(incumbent.get(hi, dict(bootstrap_bands()).get(hi, 1.0)))
        reason = ""
        value = held
        if count < MIN_CELL_ROWS or len(by_day) < MIN_CELL_DAYS:
            reason = "insufficient_evidence"
        elif math.isnan(ratio):
            reason = "no_claimed_mass"
        elif not math.isnan(ci_lo) and ci_lo <= 1.0 <= ci_hi:
            # An interval containing 1 supports "no correction", and this
            # table has never had a mandate to LIFT an off-centre claim.
            value, reason = 1.0, "interval_contains_one"
        else:
            shrunk = 1.0 - (1.0 - ratio) * (1.0 - SHRINK_TOWARD_ONE)
            if not FACTOR_FLOOR <= shrunk <= FACTOR_CEILING:
                reason = "outside_factor_bounds"
            elif shrunk > 1.0:
                # Deflation only.  Measured 2026-09-09 on the distance axis,
                # P0's 0.35-0.55 cell realises 1.13 [1.08,1.19] of its claim
                # -- the interval clears 1 and the signal is real -- and this
                # table still will not serve it.  Lifting an off-centre claim
                # raises what the gate lets us PAY, and the day's other
                # measurement is that off-centre positions lose 15.8% while
                # the centre-holding ones break even: their claims are not
                # the problem, their prices are.  Charter 81 owns the lift,
                # for centre-holding windows, where it was measured.
                value, reason = 1.0, "lift_refused_deflation_only"
            else:
                value, reason = shrunk, "fitted"
        served.append((float(hi), float(value)))
        evidence.append({
            "band": [lo, hi], "rows": count, "days": len(by_day),
            "realized_over_claimed": None if math.isnan(ratio) else round(ratio, 4),
            "ci": [None if math.isnan(ci_lo) else round(ci_lo, 4),
                   None if math.isnan(ci_hi) else round(ci_hi, 4)],
            "incumbent": round(held, 4), "served": round(float(value), 4),
            "reason": reason,
        })
    payload = {
        "schema": SCHEMA, "stage": str(stage), "band_axis": str(axis),
        "centre_definition": CENTRE_DEFINITION, "window_width": WINDOW_WIDTH,
        "shrink_toward_one": SHRINK_TOWARD_ONE,
        "factors": [[bound, factor] for bound, factor in served],
    }
    return {
        "stage": str(stage), "band_axis": str(axis),
        "factors": served, "evidence": evidence,
        "serving_day_count": len(days),
        "station_day_count": len(sample),
        "fitted_cell_count": sum(
            1 for cell in evidence if cell["reason"] == "fitted"
        ),
        "content_sha256": content_sha256(payload),
        "payload": payload,
    }


def persist_and_activate(
    conn: sqlite3.Connection, fit: dict[str, Any], *, now: str | None = None
) -> dict[str, Any]:
    """Store the fit and serve it for this stage on this axis."""

    ensure_schema(conn)
    stamp = now or datetime.now(UTC).isoformat(timespec="seconds")
    sha = str(fit["content_sha256"])
    stage, axis = str(fit["stage"]), str(fit["band_axis"])
    conn.execute(
        "UPDATE calibration_off_centre_claim_table SET activated=0"
        " WHERE activated=1 AND stage=? AND band_axis=?",
        (stage, axis),
    )
    conn.execute(
        "INSERT INTO calibration_off_centre_claim_table("
        " table_sha256, stage, band_axis, factors_json, evidence_json,"
        " fitted_at_utc, serving_day_count, activated, activated_at_utc)"
        " VALUES(?,?,?,?,?,?,?,1,?)"
        " ON CONFLICT(table_sha256) DO UPDATE SET"
        "  activated=1, activated_at_utc=excluded.activated_at_utc,"
        "  evidence_json=excluded.evidence_json",
        (sha, stage, axis,
         json.dumps(fit["payload"]["factors"], ensure_ascii=False),
         json.dumps(fit["evidence"], ensure_ascii=False),
         stamp, int(fit["serving_day_count"]), stamp),
    )
    conn.commit()
    return {"table_sha256": sha, "activated_at_utc": stamp}


SERVED_STAGES = ("P0", "P0-M", "P1")


def load_off_centre_tables(
    conn: sqlite3.Connection,
) -> dict[str, tuple[tuple[float, float], ...] | None]:
    """One table per served stage, as the compile reads them.

    ``None`` for a stage with nothing activated on the axis it serves today,
    which the kernel reads as "use the bootstrap".  Same contract, same
    shape and the same bootstrap-on-absence rule as
    ``effective_edge_store.load_effective_edge_tables``.
    """

    out: dict[str, tuple[tuple[float, float], ...] | None] = {}
    for stage in SERVED_STAGES:
        loaded = load_off_centre_bands(conn, stage=stage)
        out[stage] = loaded[0] if loaded else None
    return out
