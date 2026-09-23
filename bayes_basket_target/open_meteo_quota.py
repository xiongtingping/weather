"""One daily budget for the relay lanes' Open-Meteo calls, and one backoff.

Charter 201 (2026-09-23, user ruling 「放行转发源」): IFS 9 km, UKMO 10 km
and CMA GRAPES reach the deterministic-forecast family through the
aggregator's single-runs endpoint.  The aggregator's free tier counts calls
per egress address and per UTC day (10,000), per hour (5,000) and per minute
(600), a multi-location request counting once per location; on 2026-09-21
the day's allowance ran out at 15:51Z and the d2 member lane, which shares
the address, lost its capture for 09-23.  So the relay lanes keep a ledger:

* ``charge(lane, weight)`` before every request, refused (``QuotaRefused``)
  once the lane's own daily budget is spent -- the budget is sized so the
  relay lanes together can never take more than a fraction of the day and
  the d2 lane keeps its room;
* ``mark_exhausted()`` when the aggregator itself says the day is gone: every
  relay request is then refused until the next UTC midnight, instead of
  retrying into a wall (the d2 lane's 09-21 failure mode);
* ``note_throttled()`` on a per-minute answer ("Too many concurrent
  requests" / HTTP 429): the caller waits out an increasing pause.

The ledger is a small JSON file beside the other runtime state, locked for
each read-modify-write; a count off by a request under a race is harmless,
the refusal is what matters.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .contracts import ContractError

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = Path(
    os.environ.get("PM_OPEN_METEO_QUOTA_LEDGER")
    or ROOT / "data" / "runtime" / "open_meteo_quota_ledger.json"
)
# Weighted calls per UTC day each relay lane may spend.  The family's
# relay captures take 45 stations x 4 cycles per model (180 a model, three
# models) plus their doorbell (one call a model per 20 minutes, 216 a day):
# about 760 a day.  1,500 leaves double that for catch-up and still takes
# under a sixth of the address's 10,000.
DAILY_BUDGET = {
    "deterministic_relay": int(os.environ.get("PM_OPEN_METEO_RELAY_DAILY_BUDGET", "1500")),
}
DEFAULT_LANE_BUDGET = 500
THROTTLE_PAUSE_SEC = (2.0, 5.0, 15.0)


class QuotaRefused(ContractError):
    """This lane's share of today's aggregator calls is spent, or the day is."""


def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


@contextmanager
def _locked(path: Path) -> Iterator[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(path.read_text())
                if not isinstance(state, dict):
                    state = {}
            except (OSError, ValueError):
                state = {}
            yield state
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(state, sort_keys=True, indent=1))
            os.replace(temporary, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def exhausted(now: datetime | None = None, *, path: Path | None = None) -> bool:
    """The aggregator said today's allowance is gone, and today is not over."""

    now = now or datetime.now(timezone.utc)
    try:
        state = json.loads((path or LEDGER_PATH).read_text())
    except (OSError, ValueError):
        return False
    until = str((state or {}).get("exhausted_until_utc") or "")
    return bool(until) and now.astimezone(timezone.utc).isoformat() < until


def spent(lane: str, now: datetime | None = None, *, path: Path | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    try:
        state = json.loads((path or LEDGER_PATH).read_text())
    except (OSError, ValueError):
        return 0
    return int(((state or {}).get("days") or {}).get(_day(now), {}).get(lane, 0))


def charge(
    lane: str, weight: int, now: datetime | None = None, *, path: Path | None = None,
) -> None:
    """Account ``weight`` calls to ``lane`` before making them, or refuse."""

    now = now or datetime.now(timezone.utc)
    budget = DAILY_BUDGET.get(lane, DEFAULT_LANE_BUDGET)
    with _locked(path or LEDGER_PATH) as state:
        until = str(state.get("exhausted_until_utc") or "")
        if until and now.astimezone(timezone.utc).isoformat() < until:
            raise QuotaRefused("open_meteo_day_exhausted")
        days = state.setdefault("days", {})
        # Keep a week: enough for the board, bounded forever.
        for day in sorted(days)[:-7]:
            days.pop(day, None)
        today = days.setdefault(_day(now), {})
        used = int(today.get(lane, 0))
        if used + int(weight) > budget:
            raise QuotaRefused(f"open_meteo_lane_budget_spent:{lane}:{used}/{budget}")
        today[lane] = used + int(weight)


def mark_exhausted(now: datetime | None = None, *, path: Path | None = None) -> str:
    """Refuse every relay call until the next UTC midnight."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    until = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    stamp = until.isoformat()
    with _locked(path or LEDGER_PATH) as state:
        state["exhausted_until_utc"] = stamp
        state["exhausted_marked_at_utc"] = now.isoformat()
    return stamp


def throttle_pause(attempt: int) -> None:
    """Wait out a per-minute refusal, a little longer each time."""

    time.sleep(THROTTLE_PAUSE_SEC[min(int(attempt), len(THROTTLE_PAUSE_SEC) - 1)])


def classify(reason: str, status_code: int | None = None) -> str:
    """"day" / "minute" / "not_available" / "" from an aggregator answer."""

    text = str(reason or "")
    if "Daily API request limit" in text or "daily" in text.lower() and "limit" in text.lower():
        return "day"
    if "Too many concurrent" in text or "Minutely API request limit" in text or status_code == 429:
        return "minute"
    if "not available" in text:
        return "not_available"
    return ""


__all__ = [
    "DAILY_BUDGET", "LEDGER_PATH", "QuotaRefused", "charge", "classify",
    "exhausted", "mark_exhausted", "spent", "throttle_pause",
]
