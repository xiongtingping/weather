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

    lead_days = capture_lead_days(
        capture,
        supported_lead_days=supported_lead_days,
        default_lead_days=default_lead_days,
        error_code=error_code,
    )
    run_init = capture_run_init(capture, error_code=error_code)
    if (
        date.fromisoformat(str(capture["event_date"]))
        - run_init.date()
    ).days != lead_days:
        # The stated lead and the stated run disagree.  Trusting either one
        # would be a guess about which the members came from.
        raise ContractError(error_code)
    return lead_days, f"d{lead_days}", run_init, run_cycle_label(run_init)


__all__ = [
    "capture_lead_and_run",
    "capture_lead_days",
    "capture_run_init",
    "lead_bucket_for_run",
    "run_cycle_label",
    "run_init_for_lead",
]
