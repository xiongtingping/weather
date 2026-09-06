"""Charter 99: the regional model's curve as P1's remaining-warming prior.

Operator ruling 2026-09-06 ("跳过度量直接接进 P1", after "把区域模型移动到
P1…尝试和温度路径融合").  The cooling-side ceiling answers half the P1
question -- how much mass belongs above the bucket already holding the day's
high -- from a pooled clock rate, and leaves the other half, *how far above*,
to the shape the morning model published before dawn.  A regional run issued
during the local day answers both at once: its remaining segment states where
the day tops out, and the residual of that statement, fitted per hour of the
day, states how sure it is.

The layer therefore serves a predictive distribution of ``max(H, N(peak, s))``
-- the day's high cannot fall below what has already been reported -- blended
with the incumbent posterior so the model keeps a say, and it replaces the
ceiling at the same composition point whenever it speaks.  With no admissible
run, no fitted cell, or the switch off, it stays silent and the ceiling serves
exactly as before.

Unmeasured by ruling: the operator waived the hour-by-hour gate the ceiling
and the width tiers had to pass.  The weight and the switch are the two knobs
that take it back out.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "bayes_basket_target_regional_path_prior_v1"
RULE_VERSION = "regional_curve_remaining_peak_v2_per_system_cell"
DEFAULT_ARTIFACT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_path_prior_artifact.json"
)
DEFAULT_REPLAY_TABLE = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_path_prior_replay_table.json"
)
# On by the ruling; ``off`` is the pre-ruling arm and the way back out.
ENABLED = (
    os.environ.get("PM_REGIONAL_PATH_PRIOR", "on").strip().lower()
    not in {"0", "off", "false", "no"}
)
# How much of the served shape the curve takes.  Half is the neutral choice
# an unmeasured layer is entitled to: the model keeps an equal say, so a
# curve that is wrong on a day costs half of what a replacement would.
WEIGHT = float(os.environ.get("PM_REGIONAL_PATH_PRIOR_WEIGHT", "0.5") or 0.5)
if not 0.0 <= WEIGHT <= 1.0:
    raise RuntimeError(f"regional path prior weight out of range: {WEIGHT}")
# Hours left to local midnight, the ceiling's axis, so the two layers are
# read on one clock.  A curve is worth most in the afternoon, which is where
# the fine bins are.
HOURS_EDGES = (0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 13.0, 16.0, 20.0, 24.0, 1e9)
MIN_CELL_SAMPLES = 30
MIN_STATION_SAMPLES = 40
# A curve older than this is not describing today any more.
MAX_RUN_AGE_HOURS = float(
    os.environ.get("PM_REGIONAL_PATH_PRIOR_MAX_RUN_AGE_HOURS", "12.0") or 12.0
)
MIN_SIGMA_C = 0.35

#: What ``cell["fit_system_id"]`` says when the parameters came from the
#: cross-system pool rather than the serving system's own history.
POOLED_FIT_SYSTEM_ID = "POOLED_CROSS_SYSTEM"

_CACHE: dict[str, Any] = {}


def hours_bin(hours_left: float) -> int:
    for index in range(len(HOURS_EDGES) - 1):
        if HOURS_EDGES[index] <= float(hours_left) < HOURS_EDGES[index + 1]:
            return index
    return len(HOURS_EDGES) - 2


def _load(path: Path, schema: str, key_prefix: str) -> dict[str, Any] | None:
    key = key_prefix + str(path)
    if key not in _CACHE:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if payload is not None and payload.get("schema") != schema:
            payload = None
        _CACHE[key] = payload
    return _CACHE[key]


def load_artifact(path: Path | None = None) -> dict[str, Any] | None:
    target = Path(
        path or os.environ.get("PM_REGIONAL_PATH_PRIOR_ARTIFACT")
        or DEFAULT_ARTIFACT
    )
    return _load(target, SCHEMA, "artifact::")


def load_replay_table(path: Path | None = None) -> dict[str, Any] | None:
    target = Path(
        path or os.environ.get("PM_REGIONAL_PATH_PRIOR_REPLAY_TABLE")
        or DEFAULT_REPLAY_TABLE
    )
    return _load(target, SCHEMA + "_replay", "replay::")


def replay_curve(
    event_date: str, table: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """The artifact this event date may use: fitted only on earlier days."""

    payload = table if table is not None else load_replay_table()
    if not payload:
        return None
    entry = (payload.get("dates") or {}).get(str(event_date))
    if not entry:
        return None
    return {"schema": SCHEMA, "cells": entry.get("cells") or {},
            "stations": entry.get("stations") or {},
            "systems": entry.get("systems") or {}, "trained_through": ""}


def _cell_from_scope(
    scope: Mapping[str, Any] | None,
    *,
    hours_left: float,
    station_id: str,
    source_prefix: str,
    fit_system_id: str,
) -> dict[str, Any] | None:
    """One scope's fitted (bias, sigma) for this hour, or ``None``.

    A scope is either one system's own fit or the cross-system pool.  The
    station bias is only taken from the same scope that supplied the cell:
    a per-station number is a correction to one model's own error, and
    reading it off a different model's residuals is the borrow this
    function exists to make visible.
    """

    cells = (scope or {}).get("cells") or {}
    cell = cells.get(str(hours_bin(hours_left)))
    if not cell or cell.get("backed_off"):
        return None
    bias = float(cell["bias_c"])
    sigma = max(MIN_SIGMA_C, float(cell["sigma_c"]))
    source = f"{source_prefix}hours_cell"
    station = ((scope or {}).get("stations") or {}).get(str(station_id).upper())
    if station and int(station.get("sample_n") or 0) >= MIN_STATION_SAMPLES:
        bias = float(station["bias_c"])
        source = f"{source_prefix}hours_cell_station_bias"
    return {
        "bias_c": bias, "sigma_c": sigma, "source": source,
        "fit_system_id": fit_system_id,
        "sample_n": int(cell.get("sample_n") or 0),
        "hours_from": float(cell.get("hours_from", 0.0)),
        "hours_to": float(cell.get("hours_to", 0.0)),
    }


def resolve_cell(
    artifact: Mapping[str, Any],
    *,
    hours_left: float,
    station_id: str,
    system_id: str | None = None,
) -> dict[str, Any] | None:
    """The fitted (bias, sigma) for this hour, with the station's own bias
    where it has earned one.  ``None`` when the hour has no fitted cell.

    Charter 99, 2026-09-06: the fit carries a system dimension because the
    first artifact did not, and on 2026-09-06 it served seven European
    ICON-EU station-days from cells fitted entirely on ten US HRRR/NBM
    stations -- a sigma of 0.90C that sets how much mass the layer puts
    above the running high, borrowed from a different model on a different
    continent.  A system's own cell is preferred; the pooled cell still
    serves when that system has no history, because ICON-EU's archive
    keeps none and a strict rule would silence it forever.  Either way the
    cell now says whose errors it was fitted on, and the receipt carries
    that into the envelope, so a borrow is legible rather than silent.
    """

    scoped = ((artifact.get("systems") or {}).get(str(system_id))
              if system_id else None)
    own = _cell_from_scope(
        scoped, hours_left=hours_left, station_id=station_id,
        source_prefix="", fit_system_id=str(system_id),
    )
    if own is not None:
        return own
    return _cell_from_scope(
        artifact, hours_left=hours_left, station_id=station_id,
        source_prefix="pooled_", fit_system_id=POOLED_FIT_SYSTEM_ID,
    )


def _phi(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def predictive_probabilities(
    *,
    peak_c: float,
    sigma_c: float,
    ordinals: Mapping[str, int],
    upper_bounds_c: Mapping[str, Any],
    running_high_bucket: str,
) -> dict[str, float]:
    """P(the day's max lands in each bucket), given the corrected peak.

    The day's high cannot fall below the bucket already holding it, so the
    whole lower tail of the normal collapses onto that bucket; the open top
    bucket takes the whole upper tail.
    """

    if running_high_bucket not in ordinals:
        raise ContractError("regional_path_prior_running_high_unknown")
    floor_ordinal = int(ordinals[running_high_bucket])
    surviving = sorted(
        (bucket for bucket in ordinals if int(ordinals[bucket]) >= floor_ordinal),
        key=lambda bucket: int(ordinals[bucket]),
    )
    sigma = max(MIN_SIGMA_C, float(sigma_c))
    result: dict[str, float] = {}
    previous = 0.0
    for index, bucket in enumerate(surviving):
        upper = upper_bounds_c.get(bucket)
        if index == len(surviving) - 1 or upper is None:
            cumulative = 1.0
        else:
            cumulative = _phi((float(upper) - float(peak_c)) / sigma)
        result[bucket] = max(0.0, cumulative - previous)
        previous = cumulative
    total = sum(result.values())
    if total <= 0.0:
        raise ContractError("regional_path_prior_predictive_empty")
    return {bucket: value / total for bucket, value in result.items()}


def apply_regional_path_prior(
    posterior: Mapping[str, float],
    *,
    ordinals: Mapping[str, int],
    upper_bounds_c: Mapping[str, Any],
    running_high_bucket: str,
    running_high_c: float,
    remaining_peak_c: float,
    cell: Mapping[str, Any],
    weight: float | None = None,
    enabled: bool | None = None,
    lineage: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, str]]:
    """Blend the curve's predictive shape into the posterior.

    Returns the posterior untouched with a receipt saying why whenever the
    layer declines: switched off, or a corrected peak that says nothing the
    hard bound has not already said.
    """

    base = {str(bucket): float(value) for bucket, value in posterior.items()}
    note: dict[str, str] = {
        "regional_path_prior_applied": "false",
        "regional_path_prior_rule": RULE_VERSION,
    }
    if not (ENABLED if enabled is None else enabled):
        note["regional_path_prior_skip_reason"] = "DISABLED"
        return base, note
    share = WEIGHT if weight is None else float(weight)
    if share <= 0.0:
        note["regional_path_prior_skip_reason"] = "ZERO_WEIGHT"
        return base, note
    # The day cannot end below what it has already reached.
    peak = max(float(remaining_peak_c), float(running_high_c)) + float(cell["bias_c"])
    predictive = predictive_probabilities(
        peak_c=peak, sigma_c=float(cell["sigma_c"]), ordinals=ordinals,
        upper_bounds_c=upper_bounds_c, running_high_bucket=running_high_bucket,
    )
    floor_ordinal = int(ordinals[running_high_bucket])
    above_before = sum(
        value for bucket, value in base.items()
        if int(ordinals.get(bucket, floor_ordinal)) > floor_ordinal
    )
    blended = {
        bucket: share * predictive.get(bucket, 0.0) + (1.0 - share) * value
        for bucket, value in base.items()
    }
    total = sum(blended.values())
    if not math.isfinite(total) or total <= 0.0:
        note["regional_path_prior_skip_reason"] = "ZERO_MASS"
        return base, note
    blended = {bucket: value / total for bucket, value in blended.items()}
    above_after = sum(
        value for bucket, value in blended.items()
        if int(ordinals.get(bucket, floor_ordinal)) > floor_ordinal
    )
    note = {
        "regional_path_prior_applied": "true",
        "regional_path_prior_rule": RULE_VERSION,
        "regional_path_prior_weight": f"{share:.4f}",
        "regional_path_prior_corrected_peak_c": f"{peak:.4f}",
        "regional_path_prior_sigma_c": f"{float(cell['sigma_c']):.4f}",
        "regional_path_prior_bias_c": f"{float(cell['bias_c']):.4f}",
        "regional_path_prior_cell_source": str(cell.get("source") or ""),
        "regional_path_prior_cell_fit_system_id": str(
            cell.get("fit_system_id") or POOLED_FIT_SYSTEM_ID
        ),
        "regional_path_prior_cell_sample_n": str(cell.get("sample_n") or 0),
        "regional_path_prior_mass_above_before": f"{above_before:.6f}",
        "regional_path_prior_mass_above_after": f"{above_after:.6f}",
        **{
            str(key): str(value)
            for key, value in dict(lineage or {}).items()
        },
    }
    return blended, note


# ---------------------------------------------------------------------------
# serving input: the freshest admissible run and its remaining segment
# ---------------------------------------------------------------------------

def remaining_peak_from_curve(
    hourly_c: Mapping[str, Any], *, as_of_utc: str,
) -> tuple[float | None, int]:
    """The curve's maximum strictly after the decision clock, and its width."""

    try:
        cutoff = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
    except ValueError:
        return None, 0
    values = []
    for stamp, value in dict(hourly_c).items():
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when > cutoff:
            values.append(float(value))
    return (max(values) if values else None), len(values)


def resolve_serving_input(
    rows: Sequence[Mapping[str, Any]],
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
    settlement_timezone: str,
    artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The freshest regional run this decision may read, with its fitted cell.

    Pure over ``rows`` -- the lane's runs for this station-day, freshest
    first -- so one read serves every prefix of a replay and each prefix
    still resolves on its own decision clock.  ``None`` when the station has
    no regional coverage, no run knowable yet, a run too old to describe
    today, a curve already exhausted, no fitted cell for the hour, or an
    event date inside the artifact's own training window: every one of
    which leaves the ceiling serving alone.
    """

    payload = artifact if artifact is not None else load_artifact()
    if not payload:
        return None
    # Charter 99, 2026-09-06: the same guard the ceiling has carried since
    # it shipped.  Production serves from the single fitted artifact, and a
    # day inside that fit would be priced by a curve calibrated on its own
    # outcome; the ledger already reads the per-date as-of table, which is
    # leak-free by construction and declares an empty trained_through, so
    # this refuses exactly the production path and leaves replay untouched.
    # Harmless today -- the live artifact is trained through 2026-08-26 --
    # and it is the next refit that would otherwise open the leak.
    trained_through = str(payload.get("trained_through") or "")
    if trained_through and str(event_date) <= trained_through:
        return None
    try:
        as_of = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
        close = datetime.combine(
            datetime.fromisoformat(str(event_date)).date(),
            datetime.min.time(), tzinfo=ZoneInfo(str(settlement_timezone)),
        ) + timedelta(days=1)
    except (ValueError, KeyError):
        return None
    hours_left = (close - as_of).total_seconds() / 3600.0
    if hours_left < 0:
        return None
    for row in rows:
        if str(row.get("archive_query_at_utc") or "") > str(as_of_utc):
            # Not knowable at this prefix's clock.
            continue
        run_init = datetime.fromisoformat(
            str(row["run_init_utc"]).replace("Z", "+00:00")
        )
        if (as_of - run_init).total_seconds() / 3600.0 > MAX_RUN_AGE_HOURS:
            continue
        peak, width = remaining_peak_from_curve(
            row["hourly_c"], as_of_utc=as_of_utc
        )
        if peak is None or width == 0:
            continue
        # The cell is resolved per candidate run, not once per station-day:
        # which system is about to serve decides whose fitted bias and
        # dispersion apply, and that is only known here.
        cell = resolve_cell(
            payload, hours_left=hours_left, station_id=station_id,
            system_id=str(row["ensemble_system_id"]),
        )
        if cell is None:
            continue
        return {
            "remaining_peak_c": float(peak),
            "remaining_hours_on_curve": int(width),
            "cell": cell,
            "hours_left": round(hours_left, 4),
            "lineage": {
                "regional_path_prior_system_id": str(row["ensemble_system_id"]),
                "regional_path_prior_run_init_utc": str(row["run_init_utc"]),
                "regional_path_prior_remaining_peak_raw_c": f"{float(peak):.4f}",
                "regional_path_prior_hours_left": f"{hours_left:.4f}",
            },
        }
    return None


def build_serving_input(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
    settlement_timezone: str,
    artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """``resolve_serving_input`` over the lane's own rows."""

    from .regional_point_lane import runs_for  # noqa: PLC0415

    try:
        rows = runs_for(
            conn, station_id=station_id, event_date=event_date,
            as_of_utc=as_of_utc,
        )
    except sqlite3.Error:
        return None
    return resolve_serving_input(
        rows, station_id=station_id, event_date=event_date,
        as_of_utc=as_of_utc, settlement_timezone=settlement_timezone,
        artifact=artifact,
    )


def load_runs_for_station_day(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, as_of_utc: str,
) -> list[dict[str, Any]]:
    """One read per station-day; every prefix then resolves in memory."""

    from .regional_point_lane import runs_for  # noqa: PLC0415

    try:
        return runs_for(
            conn, station_id=station_id, event_date=event_date, as_of_utc=as_of_utc
        )
    except sqlite3.Error:
        return []


__all__ = [
    "load_runs_for_station_day", "resolve_serving_input",
    "SCHEMA", "RULE_VERSION", "ENABLED", "WEIGHT", "HOURS_EDGES",
    "MIN_CELL_SAMPLES", "MIN_STATION_SAMPLES", "MAX_RUN_AGE_HOURS",
    "POOLED_FIT_SYSTEM_ID",
    "apply_regional_path_prior", "build_serving_input", "hours_bin",
    "load_artifact", "load_replay_table", "predictive_probabilities",
    "remaining_peak_from_curve", "replay_curve", "resolve_cell",
]
