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
from typing import Any, Mapping, Sequence

from .band_reliability_store import (
    BAND_AXIS_CLAIM,
    BAND_AXES,
    band_table_axis,
    load_band_table,
    serving_lane_by_station_day,
    _first_decision_by_station_day,
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
# The veto window, in days and off-centre windows of evidence served AFTER
# the incumbent's own activation.  Same shape, same constants and the same
# reason as the band lane's and the station-width lane's (2026-09-10/11): a
# table held because the evidence is thin is held unconditionally, and the
# only route back to identity was a fit that cannot run while the evidence
# is thin.  A layer that can activate but never retire is a layer where one
# bad fit is permanent.  Out of sample by construction: the rows postdate
# the activation.
OFF_CENTRE_VETO_MIN_DAYS = 3
OFF_CENTRE_VETO_MIN_ROWS = 60
# Per off-centre window, the same hurdle the other two lanes retire on.  A
# table has to be measurably hurting, not merely not helping -- not helping
# is what every identity cell already is.
OFF_CENTRE_VETO_MIN_GAIN = 0.005

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


_JOURNAL_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_off_centre_claim_journal ("
    " fit_id TEXT PRIMARY KEY,"
    " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),"
    " band_axis TEXT NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " table_sha256 TEXT NOT NULL CHECK(length(table_sha256)=64),"
    " serving_day_count INTEGER NOT NULL,"
    " station_day_count INTEGER NOT NULL,"
    " fitted_cell_count INTEGER NOT NULL,"
    # Same vocabulary as the band lane's journal, for the same reasons.
    # NO_BAND_TABLE is this lane's own: it reads the band store's sample and
    # has nothing to fit when the stage serves no band table.
    " outcome TEXT NOT NULL CHECK(outcome IN"
    "  ('ACTIVATED','UNCHANGED','NO_BAND_TABLE','FIT_NOT_ACTIVATED',"
    "   'INCUMBENT_VETOED')),"
    " factors_json TEXT NOT NULL,"
    " evidence_json TEXT NOT NULL"
    ")"
)
_JOURNAL_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_off_centre_journal_stage_time"
    " ON calibration_off_centre_claim_journal(stage, fitted_at_utc)"
)
_JOURNAL_OUTCOMES = (
    "ACTIVATED", "UNCHANGED", "NO_BAND_TABLE", "FIT_NOT_ACTIVATED",
    "INCUMBENT_VETOED",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_DDL)
    conn.execute(_ACTIVE_INDEX)
    conn.execute(_JOURNAL_DDL)
    conn.execute(_JOURNAL_INDEX)
    # When a version stopped serving, as opposed to merely being superseded.
    # Nullable: every row written before this existed reads as never retired.
    columns = {
        str(row[1]) for row in conn.execute(
            "PRAGMA table_info(calibration_off_centre_claim_table)"
        )
    }
    if "retired_at_utc" not in columns:
        conn.execute(
            "ALTER TABLE calibration_off_centre_claim_table"
            " ADD COLUMN retired_at_utc TEXT"
        )
    # ``CREATE TABLE IF NOT EXISTS`` will not widen a CHECK on a journal that
    # already exists, so a new outcome would fail at insert rather than be
    # recorded.  Rebuild in place; the journal is append-only evidence, so it
    # is copied across rather than dropped.  Same migration the band and
    # centre/width journals carry.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name='calibration_off_centre_claim_journal'"
    ).fetchone()
    live = str((row or [""])[0] or "")
    if live and any(f"'{o}'" not in live for o in _JOURNAL_OUTCOMES):
        conn.execute(
            "ALTER TABLE calibration_off_centre_claim_journal"
            " RENAME TO calibration_off_centre_claim_journal_superseded"
        )
        conn.execute(_JOURNAL_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO calibration_off_centre_claim_journal"
            " SELECT * FROM calibration_off_centre_claim_journal_superseded"
        )
        conn.execute(
            "DROP TABLE calibration_off_centre_claim_journal_superseded"
        )
        conn.commit()


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


def _window_nll(claim: float, hit: float) -> float:
    """One off-centre window's negative log likelihood under its own claim.

    The proper scoring rule the band lane's veto uses, on this lane's own
    unit: a window claimed ``claim`` and either contained the truth or did
    not.  A factor that moves the claim toward what the window realised
    lowers this; one that moves it away raises it.
    """

    q = min(1.0 - 1e-6, max(1e-6, float(claim)))
    return -math.log(q) if float(hit) > 0.0 else -math.log(1.0 - q)


def _off_centre_incumbent_veto(
    rows: Sequence[tuple[str, str, tuple[float, float], float, float]],
    *,
    factors: Mapping[float, float],
    activated_at_utc: str | None,
    decisions: Mapping[tuple[str, str], str],
) -> dict[str, Any]:
    """Is the served table still earning its place?

    Scored on the off-centre windows decided AFTER the incumbent's own
    activation -- out of sample by construction -- against the same windows
    with the table undone.  Losing by more than the hurdle retires it.
    """

    verdict: dict[str, Any] = {
        "vetoed": False,
        "activated_at_utc": activated_at_utc,
        "day_count": 0,
        "row_count": 0,
        "undo_gain": 0.0,
        "min_days": OFF_CENTRE_VETO_MIN_DAYS,
        "min_rows": OFF_CENTRE_VETO_MIN_ROWS,
        "hurdle": OFF_CENTRE_VETO_MIN_GAIN,
    }
    if not activated_at_utc or all(
        abs(float(v) - 1.0) < 1e-12 for v in factors.values()
    ):
        return verdict
    after = [
        row for row in rows
        if str(decisions.get((str(row[0]), str(row[1])), "")) >= str(
            activated_at_utc
        )
    ]
    days = {str(row[1]) for row in after}
    verdict["day_count"] = len(days)
    verdict["row_count"] = len(after)
    if (
        len(days) < OFF_CENTRE_VETO_MIN_DAYS
        or len(after) < OFF_CENTRE_VETO_MIN_ROWS
    ):
        return verdict
    kept = undone = 0.0
    for _station, _day, band, claim, hit in after:
        factor = float(factors.get(float(band[1]), 1.0))
        kept += _window_nll(claim * factor, hit)
        undone += _window_nll(claim, hit)
    # Positive when UNDOING beats the incumbent, which is the direction this
    # question is asked in.
    verdict["nll_kept"] = kept / len(after)
    verdict["nll_undone"] = undone / len(after)
    verdict["undo_gain"] = (kept - undone) / len(after)
    verdict["vetoed"] = verdict["undo_gain"] > OFF_CENTRE_VETO_MIN_GAIN
    return verdict


def retire_off_centre_table(
    conn: sqlite3.Connection, *, stage: str, retired_at_utc: str | None = None
) -> dict[str, Any]:
    """Stop serving this stage's table on the axis it serves.

    Retired, not superseded: the unpinned read is the only reader this lane
    has, so deactivating is enough to stop it, and the stamp is the record
    of why the row is not active.
    """

    ensure_schema(conn)
    now = retired_at_utc or datetime.now(UTC).isoformat(timespec="seconds")
    axis = band_table_axis(conn, stage=stage)
    conn.execute(
        "UPDATE calibration_off_centre_claim_table"
        " SET activated=0, retired_at_utc=COALESCE(retired_at_utc, ?)"
        " WHERE activated=1 AND stage=? AND band_axis=?",
        (now, str(stage), str(axis)),
    )
    conn.commit()
    return {"state": "RETIRED", "retired_at_utc": now, "band_axis": axis}


def journal_off_centre_fit(
    conn: sqlite3.Connection,
    *,
    fit: Mapping[str, Any],
    outcome: str,
    fitted_at_utc: str | None = None,
) -> str:
    """One row per fit, whatever it decided.

    The band and centre/width lanes have journalled since they were built;
    this one stored only its latest table, so a day on which it refused, held
    or retired left no trace at all.
    """

    ensure_schema(conn)
    if str(outcome) not in _JOURNAL_OUTCOMES:
        raise ValueError(f"off_centre_journal_outcome_invalid:{outcome}")
    now = fitted_at_utc or datetime.now(UTC).isoformat(timespec="seconds")
    sha = str(fit["content_sha256"])
    fit_id = "ocfit-" + content_sha256({
        "stage": str(fit["stage"]), "sha": sha, "at": now,
        "outcome": str(outcome),
    })[:24]
    conn.execute(
        "INSERT OR REPLACE INTO calibration_off_centre_claim_journal("
        " fit_id, stage, band_axis, fitted_at_utc, table_sha256,"
        " serving_day_count, station_day_count, fitted_cell_count, outcome,"
        " factors_json, evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            fit_id, str(fit["stage"]), str(fit["band_axis"]), now, sha,
            int(fit.get("serving_day_count") or 0),
            int(fit.get("station_day_count") or 0),
            int(fit.get("fitted_cell_count") or 0),
            str(outcome),
            json.dumps(fit.get("factors") or [], ensure_ascii=False),
            json.dumps(fit.get("evidence") or [], ensure_ascii=False),
        ),
    )
    conn.commit()
    return fit_id


def fit_off_centre_claim_table(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
    min_decision_at_utc: str | None = None,
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
    # The serving-arithmetic floor (2026-09-11): the same bound the band
    # lane carries.  This table measures how much of a served claim is
    # realised, and a claim served before the stage's declared version start
    # -- under the 0.5 station scale retired that afternoon, say -- is a
    # claim nobody serves any more.  Short of evidence the cells hold their
    # incumbent, as they always have.
    if min_decision_at_utc is None:
        from .center_width_store import served_arithmetic_anchor  # noqa: PLC0415

        min_decision_at_utc = served_arithmetic_anchor(conn, stage=stage)
    sample = serving_lane_by_station_day(
        conn, _truth(conn), stage=stage, pre_band=False,
        min_decision_at_utc=min_decision_at_utc,
    )
    cells: dict[tuple[float, float], dict[str, list[tuple[float, float]]]] = {}
    window_rows: list[tuple[str, str, tuple[float, float], float, float]] = []
    days: set[str] = set()
    for (_station, event), snaps in sample.items():  # noqa: B007
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
            # The same windows, keyed so the veto below can ask which of
            # them were served after the incumbent went live.
            window_rows.append((str(_station), str(event), band, claim, hit))
    loaded = load_off_centre_bands(conn, stage=stage)
    incumbent = dict((loaded or (bootstrap_bands(), ""))[0])
    # The decision times are only needed to date the veto's window, so they
    # are read only when there is something to veto: a stage serving the
    # bootstrap, or an all-identity table, has no incumbent to retire and
    # the query would be work -- and, on a fixture that stubs the sample,
    # a table that need not exist.
    veto_factors = incumbent if loaded is not None else {}
    activated_at = (
        None if loaded is None
        else _active_off_centre_activated_at(conn, stage=stage)
    )
    votable = bool(activated_at) and any(
        abs(float(v) - 1.0) > 1e-12 for v in veto_factors.values()
    )
    veto = _off_centre_incumbent_veto(
        window_rows,
        factors=veto_factors,
        activated_at_utc=activated_at,
        decisions=(
            _first_decision_by_station_day(
                conn, stage=stage, min_decision_at_utc=min_decision_at_utc
            ) if votable else {}
        ),
    )
    if veto["vetoed"]:
        identity = [(float(CLAIM_BANDS[0][0]), 1.0)] + [
            (float(hi), 1.0) for _lo, hi in CLAIM_BANDS
        ]
        payload = {
            "schema": SCHEMA, "stage": str(stage), "band_axis": str(axis),
            "centre_definition": CENTRE_DEFINITION,
            "window_width": WINDOW_WIDTH,
            "shrink_toward_one": SHRINK_TOWARD_ONE,
            "factors": [[b, f] for b, f in identity],
        }
        return {
            "stage": str(stage), "band_axis": str(axis),
            "factors": identity,
            "evidence": [{"reason": "incumbent_vetoed", "veto": veto}],
            "serving_day_count": len(days),
            "station_day_count": len(sample),
            "fitted_cell_count": 0,
            "vetoed": True,
            "incumbent_veto": veto,
            "content_sha256": content_sha256(payload),
            "payload": payload,
            "min_decision_at_utc": min_decision_at_utc,
        }
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
        "min_decision_at_utc": min_decision_at_utc,
        "vetoed": False,
        "incumbent_veto": veto,
    }


def _active_off_centre_activated_at(
    conn: sqlite3.Connection, *, stage: str
) -> str | None:
    try:
        row = conn.execute(
            "SELECT activated_at_utc FROM calibration_off_centre_claim_table"
            " WHERE activated=1 AND stage=? AND band_axis=?",
            (str(stage), band_table_axis(conn, stage=stage)),
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None or row[0] is None else str(row[0])


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
