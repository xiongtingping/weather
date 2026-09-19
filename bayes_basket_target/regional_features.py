"""The regional models' two covariates for P1's joint observation likelihood.

Charter 150 (2026-09-19): the regional high-resolution lanes (JMA MSM, HRRR,
NBM, ICON-EU, CMA GRAPES) reach P1 only here.  They are deterministic single
curves, measured to pay as a feature of the likelihood and not as a blended
distribution (the charter-99 path prior that blended them is retired and its
code deleted), so what P1 reads off a run is two numbers:

* ``regional_headroom_c`` -- "剩余小时预报最高 − 已报最高": how much more
  warming the run still claims above what the station has already reported;
* ``regional_live_gap_c`` -- the live gap, observed minus the run's value for
  the hour in progress, positive when the model is running cold today.

One definition for the sample builder and for serving, for the reason a
feature computed twice is a feature that will disagree.  Both are ``None``
when the station has no admissible run, and the live gap is ``None`` on its
own when the curve does not cover the hour in progress; the likelihood reads
``None`` at the fleet mean, which says "nothing known here", where a zero
would say "the model promises nothing more today".
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

# A curve older than this is not describing today any more.
MAX_RUN_AGE_HOURS = 12.0


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
    # honest one is +0.1 to +0.5.
    if hour in parsed:
        return parsed[hour]
    return parsed.get(hour + timedelta(hours=1))


def regional_features_at(
    rows: Sequence[Mapping[str, Any]],
    *,
    as_of_utc: str,
    running_high_c: Any,
    latest_observed_c: Any,
    systems: "frozenset[str] | set[str] | None" = None,
    station_id: str | None = None,
    calibration: Mapping[str, Any] | None = None,
    calibrate: bool | None = None,
) -> dict[str, Any]:
    """The two covariates off the freshest run this clock could have read.

    A run is admissible when it was knowable at the decision clock (the as-of
    gate is two-sided: never a run issued after the decision) and is not
    older than ``MAX_RUN_AGE_HOURS``.  Every captured lane may inform the
    likelihood; ``system_id`` comes back so the receipt can say which one did.

    Calibrated (the 2026-09-18 ruling's arm, off by measurement -- see
    ``regional_point_debias.FEATURE_CALIBRATION``): the remaining peak is
    raised by the run's own fitted bias while it is still the curve's
    maximum, and the headroom is read off that.  ``calibrate`` defaults to the
    module switch; ``calibration`` is the artifact the caller's clock may read
    -- production's latest fit, or the replay table's entry for the event
    date.  ``regional_calibration_bias_c`` says what was added, ``None`` when
    nothing was.
    """

    from .regional_point_debias import (  # noqa: PLC0415
        FEATURE_CALIBRATION, resolve_correction,
    )

    absent = {
        "regional_headroom_c": None,
        "regional_live_gap_c": None,
        "regional_system_id": None,
        "regional_run_init_utc": None,
        "regional_calibration_bias_c": None,
        "regional_calibration_source": None,
    }
    calibrating = (
        (FEATURE_CALIBRATION if calibrate is None else bool(calibrate))
        and bool(calibration) and bool(station_id)
    )
    try:
        as_of = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
        high = float(running_high_c)
    except (TypeError, ValueError):
        return absent
    for row in rows:
        if systems is not None and str(
            row.get("ensemble_system_id") or ""
        ) not in systems:
            continue
        if str(row.get("knowable_at_utc") or "") > str(as_of_utc):
            continue
        try:
            run_init = datetime.fromisoformat(
                str(row["run_init_utc"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            continue
        age_hours = (as_of - run_init).total_seconds() / 3600.0
        if age_hours < 0.0 or age_hours > MAX_RUN_AGE_HOURS:
            continue
        curve = row["hourly_c"]
        correction: Mapping[str, Any] | None = None
        peak, width = remaining_peak_from_curve(curve, as_of_utc=as_of_utc)
        if peak is None or width == 0:
            continue
        now_c = curve_value_at(curve, as_of_utc=as_of_utc)
        if calibrating:
            try:
                curve_peak = max(float(v) for v in dict(curve).values())
            except (TypeError, ValueError):
                curve_peak = None
            if curve_peak is not None and float(peak) >= curve_peak - 1e-9:
                correction = resolve_correction(
                    calibration,
                    system_id=str(row.get("ensemble_system_id") or ""),
                    station_id=str(station_id),
                    run_init_utc=str(row["run_init_utc"]),
                    lead_hours_to_local_day_start=float(
                        row.get("lead_hours_to_local_day_start") or 0.0
                    ),
                    forecast_level_c=float(
                        row.get("forecast_tmax_c") or curve_peak
                    ),
                )
                if correction is not None:
                    peak = float(peak) + float(correction["bias_c"])
        try:
            observed = float(latest_observed_c)
        except (TypeError, ValueError):
            observed = None
        return {
            "regional_headroom_c": float(peak) - high,
            "regional_live_gap_c": (
                None if (now_c is None or observed is None)
                else observed - float(now_c)
            ),
            "regional_system_id": str(row["ensemble_system_id"]),
            "regional_run_init_utc": str(row["run_init_utc"]),
            "regional_calibration_bias_c": (
                None if correction is None else float(correction["bias_c"])
            ),
            "regional_calibration_source": (
                None if correction is None
                else f"{correction['source']}:{correction['scope']}"
                f":{correction['train_days']}d"
            ),
        }
    return absent


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
    "MAX_RUN_AGE_HOURS",
    "curve_value_at",
    "load_runs_for_station_day",
    "regional_features_at",
    "remaining_peak_from_curve",
]
