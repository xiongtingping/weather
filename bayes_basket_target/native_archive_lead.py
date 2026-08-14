"""One reading of a native archive capture's lead and run cycle.

Each of the four official collectors used to carry its own copy of this logic.
The copies drifted, and every drift was a silent mislabelling of training data
rather than a failure: one collector kept stamping ``18Z`` on captures it had
fetched from the 00Z run, another read the module's default lead instead of the
one the capture stated.  A row mislabelled at ingest is mislabelled for good --
the historical member table is append-only -- and the OOS scope check cannot
catch it, because the label and the contract agree with each other while both
disagree with the object that was actually downloaded.

The rule these functions exist to enforce: **a capture's lead and cycle come
from the capture, never from a module constant.**  The constants describe what
this collector asks for by default, which is a different question.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError


def run_cycle_label(run_init: datetime) -> str:
    """The cycle a run belongs to, as the archives name it."""

    return f"{run_init.astimezone(timezone.utc).hour:02d}Z"


def run_init_for_lead(
    event_date: str,
    *,
    lead_days: int,
    cycle_hour_by_lead: Mapping[int, int],
    error_code: str,
) -> datetime:
    """The run this collector reads for an event date at a given lead.

    The cycle is not free to choose.  It has to leave enough forecast steps to
    cover the whole target local day, and the target day starts at a different
    UTC hour for every station: a cycle late in the day still precedes the
    event day at d2, but at d1 it starts after the day has already begun in the
    far east, where eight 3-hour periods become five and the capture is
    rejected as an incomplete grid.  Shorter leads therefore take an earlier
    cycle, which is why this is a mapping rather than one constant.
    """

    if int(lead_days) not in cycle_hour_by_lead:
        raise ContractError(error_code)
    return datetime.combine(
        date.fromisoformat(str(event_date)) - timedelta(days=int(lead_days)),
        time(hour=int(cycle_hour_by_lead[int(lead_days)])),
        tzinfo=timezone.utc,
    )


def lead_bucket_for_run(
    *,
    event_date: str,
    run_init: datetime,
    supported_lead_days: tuple[int, ...],
    error_code: str,
) -> str:
    """The lead a capture may advertise, read off the run it actually used.

    Capture sites derive the bucket here rather than restating the lead they
    were asked for, so a capture cannot claim a lead its run does not carry.
    """

    delta = (
        date.fromisoformat(str(event_date))
        - run_init.astimezone(timezone.utc).date()
    ).days
    if delta not in supported_lead_days:
        raise ContractError(error_code)
    return f"d{delta}"


def capture_lead_days(
    capture: Mapping[str, Any],
    *,
    supported_lead_days: tuple[int, ...],
    default_lead_days: int,
    error_code: str,
) -> int:
    """The lead the capture states, falling back only when it states none.

    The fallback covers archives written before any lead but d2 existed.  It is
    deliberately not a general default: a capture that names a lead this
    collector does not support is an error, not something to round to d2.
    """

    raw = str(capture.get("archive_lead_bucket") or "").strip().lower()
    if not raw:
        return int(default_lead_days)
    if not raw.startswith("d") or not raw[1:].isdigit():
        raise ContractError(error_code)
    lead_days = int(raw[1:])
    if lead_days not in supported_lead_days:
        raise ContractError(error_code)
    return lead_days


def capture_run_init(
    capture: Mapping[str, Any],
    *,
    error_code: str,
) -> datetime:
    """The run initialisation the capture was actually drawn from."""

    raw = str(capture.get("run_init_utc") or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(error_code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(error_code)
    return parsed.astimezone(timezone.utc)


def capture_lead_and_run(
    capture: Mapping[str, Any],
    *,
    supported_lead_days: tuple[int, ...],
    default_lead_days: int,
    error_code: str,
) -> tuple[int, str, datetime, str]:
    """Lead days, lead bucket, run initialisation and cycle, all from the capture.

    Returned together because they have to agree: a row whose ``run_cycle``
    says 18Z while its extraction version says ``00z_d1`` is not merely untidy,
    it is unusable evidence -- nothing downstream can tell which of the two
    describes the object that was downloaded.
    """

    run_init = capture_run_init(capture, error_code=error_code)
    # The calendar lead is derived from the run, which every capture states;
    # the label no longer encodes it.  Captures now stamp the hour bucket --
    # per station, measured to its own local day -- so parsing the label for a
    # day count reads vocabulary, not evidence.
    lead_days = (
        date.fromisoformat(str(capture["event_date"])) - run_init.date()
    ).days
    if lead_days not in supported_lead_days:
        raise ContractError(error_code)
    raw = str(capture.get("archive_lead_bucket") or "").strip()
    hour_bucket_names = {
        name for name, _upper in LEAD_HOUR_BUCKETS
    } | {LONGEST_LEAD_HOUR_BUCKET}
    if raw.lower().startswith("d") and raw[1:].isdigit():
        # Calendar-era capture: its stated label must agree with its stated
        # run.  Trusting either one alone would be a guess about which the
        # members came from.
        if int(raw[1:]) != lead_days:
            raise ContractError(error_code)
        bucket = f"d{lead_days}"
    elif raw in hour_bucket_names:
        # Hour-era capture: the same agreement, in the new vocabulary.  The
        # stated bucket must be the one this capture's own run and timezone
        # derive to; a label that disagrees with the object is unusable
        # evidence in either vocabulary.
        derived = capture_lead_bucket(
            event_date=str(capture["event_date"]),
            settlement_timezone=str(
                capture.get("settlement_timezone") or ""
            ),
            run_init=run_init,
        )
        if raw != derived:
            raise ContractError(error_code)
        bucket = raw
    elif not raw:
        # Pre-label archives: the run is the only statement, and it must match
        # the collector's default lead, exactly as before.
        if lead_days != int(default_lead_days):
            raise ContractError(error_code)
        bucket = f"d{lead_days}"
    else:
        raise ContractError(error_code)
    return lead_days, bucket, run_init, run_cycle_label(run_init)


# Training groups are keyed by how far ahead a run was issued, not by the
# calendar-day label the collector used.  Measured over the retained archive,
# the two disagree badly:
#
#   label   min     median   max     span
#   d0     -12.0h    -2.0h    7.0h    19h
#   d1      12.0h    22.0h   31.0h    19h
#   d2      18.0h    35.0h   55.0h    37h
#
# d1 and d2 overlap across 18-31h -- two samples issued the same number of
# hours ahead land in different training groups purely because of which
# calendar day their run fell on -- and d2 spans 37 hours, so its within-group
# spread is wider than the gap between groups.  Both follow from the label
# being a property of the run's UTC date while skill is a property of the lead
# to the station's own local day.
LEAD_HOUR_BUCKETS: tuple[tuple[str, float], ...] = (
    ("h00_12", 12.0),
    ("h12_24", 24.0),
    ("h24_48", 48.0),
)
# Open-ended rather than capped at 72h.  Runs are bounded by UTC calendar day
# while the lead is measured to the local day start, so a station west of the
# meridian sees more than 48h from the same run set -- 55h in the retained
# archive.  Naming a ceiling the collection window does not enforce would
# create a bucket that silently never fills.
LONGEST_LEAD_HOUR_BUCKET = "h48_plus"


def lead_hours_to_local_day(
    *,
    day_start_utc: datetime,
    run_init: datetime,
) -> float:
    """Hours between a run and the start of the target *local* day.

    Measured to the local day start rather than UTC midnight.  The event date
    is a local calendar date, so for a station at UTC-4 its day begins four
    hours after UTC midnight and every run is that much further ahead of it --
    enough to move a run across a bucket boundary, which is the distinction the
    bucket exists to draw.
    """

    return (
        day_start_utc.astimezone(timezone.utc)
        - run_init.astimezone(timezone.utc)
    ).total_seconds() / 3600.0


def lead_hour_bucket(lead_hours: float) -> str:
    """The training group a run belongs to, from its lead in hours.

    A negative lead -- a run issued after the local day had already begun --
    belongs with the freshest bucket.  That is what it is: the most recent
    information available for the day, not a separate regime.  Half the d0
    samples in the retained archive are negative for exactly this reason.
    """

    for name, upper in LEAD_HOUR_BUCKETS:
        if lead_hours <= upper:
            return name
    return LONGEST_LEAD_HOUR_BUCKET


def lead_hour_bucket_for_run(
    *,
    day_start_utc: datetime,
    run_init: datetime,
) -> str:
    """Training group for one run against one station-day."""

    return lead_hour_bucket(
        lead_hours_to_local_day(
            day_start_utc=day_start_utc, run_init=run_init
        )
    )


def capture_lead_bucket(
    *,
    event_date: str,
    settlement_timezone: str,
    run_init: datetime,
) -> str:
    """The lead bucket one station-day capture is stamped with.

    This is the hour bucket, not the calendar ``d`` label.  The calendar label
    is a property of which UTC date the run fell on; skill is a property of how
    far ahead of the station's own local day the run was issued, and the two
    disagree badly enough to corrupt training groups -- ``d1`` and ``d2``
    overlap across 18-31h and ``d2`` alone spans 37 hours, so a group's
    internal spread exceeds the gap between groups.

    It is therefore per station, not per archive.  One run reaches a station at
    UTC+8 and a station at UTC-4 with lead hours that differ by half a day, so
    a single archive-level label cannot describe both -- which is exactly what
    the ``d`` label was doing.
    """

    try:
        day_start = datetime.fromisoformat(str(event_date)).replace(
            tzinfo=ZoneInfo(str(settlement_timezone))
        ).astimezone(timezone.utc)
    except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError) as exc:
        raise ContractError(
            "native_archive_capture_lead_scope_invalid"
        ) from exc
    if run_init.tzinfo is None or run_init.utcoffset() is None:
        raise ContractError("native_archive_capture_run_init_invalid")
    return lead_hour_bucket_for_run(
        day_start_utc=day_start, run_init=run_init
    )


__all__ = [
    "LEAD_HOUR_BUCKETS",
    "LONGEST_LEAD_HOUR_BUCKET",
    "capture_lead_bucket",
    "capture_lead_and_run",
    "capture_lead_days",
    "capture_run_init",
    "lead_bucket_for_run",
    "lead_hour_bucket",
    "lead_hour_bucket_for_run",
    "lead_hours_to_local_day",
    "run_cycle_label",
    "run_init_for_lead",
]
