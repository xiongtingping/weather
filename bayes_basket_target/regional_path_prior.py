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
from .regional_live_gap import FORM as LIVE_GAP_FORM
from .regional_point_debias import (
    apply_remaining_peak_debias,
    resolve_correction as resolve_debias_correction,
)

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
# On, but the admission list below decides which systems may actually
# reach the served distribution, and today that is one of the four lanes.
#
# History worth keeping: this layer shipped on 2026-09-06 with its
# measurement gate explicitly waived, and served every system whose run
# happened to be freshest.  Measured on 2026-09-07 that arrangement showed
# no gain -- the pooled reading was dominated by hourly HRRR, the weakest
# lane, which won the freshness race almost every time -- so the layer was
# defaulted off.  A wider backfill then let each system be scored on its
# own, and JMA's MSM turned out to be strongly positive while the two
# NOAA lanes stayed indistinguishable from zero.  The layer is on again
# with admission moved from "whichever run is newest" to "whichever
# system has been measured to help".  See SERVING_SYSTEMS for the
# readings; ``PM_REGIONAL_PATH_PRIOR=off`` is still the way out entirely.
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


#: Which regional systems may reach the served distribution.  Admission is
#: per system and by measurement, not by whether a lane exists.
#:
#: 2026-09-07, on the clean window (prefixes whose ceiling receipt read
#: TOO_EARLY, where the served vector IS this layer's input so the two
#: arms differ by exactly the layer under test), clustered by station-day
#: because a station-day contributes ~30 correlated prefixes:
#:
#:   JMA_MSM_POINT   -0.634 nat over 46 station-days, 95% CI -0.946 to
#:                   -0.322, station-day bootstrap P(worse) 0.000; argmax
#:                   35.1% -> 61.8%, within-one 71.9% -> 89.5%.  All four
#:                   stations improve (RCSS -1.28, RJTT -0.66, RKPK -0.45,
#:                   RKSI -0.08).  Its own daily maximum has MAE 0.621C
#:                   against the incumbent posterior's 1.395C at those
#:                   stations, and it is near unbiased (-0.097C).
#:   NCEP_NBM_POINT  -0.006 nat over 110 station-days, CI -0.062 to
#:                   +0.051.  Straddles zero.  Refused.
#:   NCEP_HRRR_POINT +0.045 over 90 station-days, CI -0.050 to +0.141.
#:                   Straddles zero, and its own daily maximum is 1.461C
#:                   against the incumbent's 0.798C.  Refused.
#:   DWD_ICON_EU     no comparable prefix yet -- the DWD archive keeps no
#:                   history, so this lane only accumulates forward.
#:                   Unmeasured is not admitted.
#:
#: The MSM result has a physical reading rather than only a statistical
#: one: it is largest at RCSS, the station where the global bias layer has
#: failed all month because a 25 km cell cannot resolve the Taipei basin,
#: and a 5 km cell can.  It helps most where the incumbent is weakest --
#: the East Asian stations, where P1's own point error is 1.395C against
#: 0.798C at the US stations.
#:
#: This list also settles a design fault the pooled measurement hid:
#: ``resolve_serving_input`` takes the freshest admissible run, and hourly
#: HRRR won that race against the three-hourly systems almost every time
#: while being the weakest of them.  Admission by measurement removes the
#: race rather than tuning it.
SERVING_SYSTEMS = frozenset(
    part for part in (
        os.environ.get("PM_REGIONAL_PATH_PRIOR_SYSTEMS", "JMA_MSM_POINT")
        .replace(",", " ").split()
    ) if part
)

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
    live_gap_correction_c: float = 0.0,
    live_gap_note: Mapping[str, str] | None = None,
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
    #
    # Charter 95, 2026-09-07: today's own error joins the level here.  The
    # ``anchor`` form adds it after the max, which is the quantity the slope
    # was fitted against; the ``curve`` form displaces the curve before the
    # max, which reads better physically but cannot act at all once the
    # running high dominates.  Zero when the layer is silent, and then this
    # line is what it always was.
    live = float(live_gap_correction_c or 0.0)
    if LIVE_GAP_FORM == "curve":
        peak = max(float(remaining_peak_c) + live, float(running_high_c))
        peak += float(cell["bias_c"])
    else:
        peak = max(float(remaining_peak_c), float(running_high_c))
        peak += float(cell["bias_c"]) + live
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
        **{
            str(key): str(value)
            for key, value in dict(live_gap_note or {}).items()
            if str(dict(live_gap_note or {}).get(
                "regional_live_gap_skip_reason")) != "DISABLED"
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


def curve_value_at(
    hourly_c: Mapping[str, Any], *, as_of_utc: str,
) -> float | None:
    """The curve's value for the hour this instant falls in.

    The curve is keyed on the hour it is valid for, so a report at 14:23 is
    answered by the 14:00 value; a curve that only starts later answers with
    its first hour after this instant, and one that has ended answers None.
    """

    try:
        now = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
    except ValueError:
        return None
    hour = now.replace(minute=0, second=0, microsecond=0)
    parsed: dict[datetime, float] = {}
    for stamp, value in dict(hourly_c).items():
        try:
            parsed[datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))] = float(value)
        except (TypeError, ValueError):
            continue
    # This hour, or the next one, and nothing further.  The lane stores the
    # local peak window rather than the whole day, so a wider fallback hands a
    # pre-dawn report the curve's midday value and calls the difference a live
    # gap: measured 2026-09-07, that fabricated a mean gap near -2C where the
    # honest one is +0.1 to +0.5, and drove the anchor 0.47C cold.  The fitter
    # takes exactly these two and skips the instant otherwise, and serving
    # must not be able to see a sample the fit would have thrown away.
    if hour in parsed:
        return parsed[hour]
    return parsed.get(hour + timedelta(hours=1))


def resolve_serving_input(
    rows: Sequence[Mapping[str, Any]],
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
    settlement_timezone: str,
    artifact: Mapping[str, Any] | None = None,
    systems: "frozenset[str] | set[str] | None" = None,
    debias_artifact: Mapping[str, Any] | None = None,
    debias_systems: "frozenset[str] | set[str] | None" = None,
    debias_enabled: bool | None = None,
) -> dict[str, Any] | None:
    """The freshest regional run this decision may read, with its fitted cell.

    Charter 95, 2026-09-07: the curve is debiased before it is fused, here,
    where every caller passes through -- production, the ledger and the
    admission gate all reach the fusion by way of this one function, so the
    correction cannot reach one of them and miss another.  ``remaining_peak_c``
    comes back corrected and the raw value stays in the lineage.

    ``systems`` overrides the admission list.  Serving never passes it --
    that is the whole point of the list -- but the admission gate must, or
    a candidate could never be scored: it would be refused for not being
    admitted, which is the question being asked.

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
        admitted = SERVING_SYSTEMS if systems is None else systems
        if str(row.get("ensemble_system_id") or "") not in admitted:
            # A lane may capture without serving: admission is per system
            # and by measurement.  Refusing here rather than after the
            # curve is read also keeps a freshest-run race from handing
            # the slot to a system that is not allowed to have it.
            continue
        if str(row.get("archive_query_at_utc") or "") > str(as_of_utc):
            # Not knowable at this prefix's clock.
            continue
        run_init = datetime.fromisoformat(
            str(row["run_init_utc"]).replace("Z", "+00:00")
        )
        age_hours = (as_of - run_init).total_seconds() / 3600.0
        if age_hours < 0.0:
            # A run issued after this instant is not a forecast this decision
            # could have read.  The age test above is one-sided and lets a
            # negative age through, and ``knowable_at_utc`` -- the guard that
            # should have caught it -- is an empty string on 550 of 880 NBM
            # rows and 354 of 700 HRRR rows, backfills the column's repair
            # never reached; the empty string sorts before every timestamp, so
            # those runs read as knowable at every instant.  Measured
            # 2026-09-07 on the admission window: 74.1% of NBM and 78.0% of
            # HRRR serving decisions were reading a run from their own future,
            # some of them thirteen hours ahead.  MSM was clean at 0.0%, which
            # is why the reading that admitted it stands.  This guard is
            # structural and holds whatever the column says.
            continue
        if age_hours > MAX_RUN_AGE_HOURS:
            continue
        peak, width = remaining_peak_from_curve(
            row["hourly_c"], as_of_utc=as_of_utc
        )
        if peak is None or width == 0:
            continue
        # Charter 95's single-system correction, applied before the fusion
        # rather than folded into it: the curve states where the day tops
        # out, and every regional lane states it cold.  Silence leaves the
        # raw peak, which is exactly what the fusion served before.
        try:
            curve_peak = max(float(v) for v in dict(row["hourly_c"]).values())
        except (TypeError, ValueError):
            continue
        correction = resolve_debias_correction(
            debias_artifact, system_id=str(row["ensemble_system_id"]),
            station_id=station_id,
            run_age_hours=(as_of - run_init).total_seconds() / 3600.0,
            systems=debias_systems,
        )
        raw_peak = float(peak)
        peak, debias_note = apply_remaining_peak_debias(
            raw_peak, curve_peak_c=curve_peak, correction=correction,
            enabled=debias_enabled,
        )
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
            # Charter 95's live-gap feature needs the run's own value for the
            # hour now in progress; only this function has the curve.  The
            # other half -- what the station just reported -- belongs to the
            # trajectory, so the two are joined by the caller.
            "curve_now_c": curve_value_at(row["hourly_c"], as_of_utc=as_of_utc),
            "remaining_hours_on_curve": int(width),
            "cell": cell,
            "hours_left": round(hours_left, 4),
            "lineage": {
                "regional_path_prior_system_id": str(row["ensemble_system_id"]),
                "regional_path_prior_run_init_utc": str(row["run_init_utc"]),
                "regional_path_prior_remaining_peak_raw_c": f"{raw_peak:.4f}",
                "regional_path_prior_hours_left": f"{hours_left:.4f}",
                # A switched-off layer leaves no trace: with the debias off
                # this lineage is byte-identical to the one this function
                # produced before the layer existed, so the switch really is
                # the way back rather than a different receipt saying so.
                **(
                    {} if debias_note.get("regional_point_debias_skip_reason")
                    == "DISABLED" else debias_note
                ),
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
    debias_artifact: Mapping[str, Any] | None = None,
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
        artifact=artifact, debias_artifact=debias_artifact,
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
    "POOLED_FIT_SYSTEM_ID", "SERVING_SYSTEMS",
    "apply_regional_path_prior", "build_serving_input", "curve_value_at",
    "hours_bin",
    "load_artifact", "load_replay_table", "predictive_probabilities",
    "remaining_peak_from_curve", "replay_curve", "resolve_cell",
]
