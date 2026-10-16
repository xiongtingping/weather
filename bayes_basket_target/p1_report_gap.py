"""Charter 275: how far the official report stream has fallen behind, as P1 sees it.

User ruling 2026-10-17 「上线报文缺失特征」.  Two numbers per P1 step, from the
AWC lane only and strictly by arrival (``canonical_committed_at`` at or before
the step's clock):

* ``report_gap_h`` -- hours from the newest arrived report's valid time to the
  step's clock, looking back at most ``GAP_CAP_H`` (the previous local day's
  reports and the event day's);
* ``report_missing_n`` -- reports the station should have sent since local
  midnight at its usual rate, less those that have arrived (never below 0).
  The rate is the median daily count of the station's previous
  ``RATE_DAYS`` complete local days over 24; with fewer than
  ``RATE_MIN_DAYS`` of them, the fleet's ``FLEET_RATE_PER_HOUR``.

Measured before the ruling (docs/research/p1_report_gap_features_20261017):
the training set's late-night "rises" were mostly report outages -- the
nights a report stream had stalled rose 4.8 % afterwards, ordinary nights
0.19 % -- and with these two columns P1's night LogScore improved
-0.0018 [-0.0036, -0.0005], 18 stations better and none worse.

The sample builder and the resident call the same two functions on the same
archive table, so a feature cannot differ between the fit and the serve.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

SCHEMA = "p1_report_gap_v1"
SOURCE_LANE = "awc"
GAP_CAP_H = 24.0
RATE_DAYS = 14
RATE_MIN_DAYS = 3
# The fleet's median daily AWC report count (35, 2026-08-20..10-16) over 24.
FLEET_RATE_PER_HOUR = 35.0 / 24.0


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def report_context(archive, *, station_id: str, event_date: str, timezone_name: str) -> dict[str, Any]:
    """The station's usual rate and the previous and event local days' AWC
    reports (valid, arrival), so each step can count those that had arrived
    by its own clock.  One indexed read of ``reported_trajectory_observations``
    for the ``RATE_DAYS`` local days before ``event_date`` and the event day."""

    zone = ZoneInfo(timezone_name)
    day = date.fromisoformat(event_date)
    start_local = datetime.combine(day - timedelta(days=RATE_DAYS), time(0), zone)
    end_local = datetime.combine(day, time(0), zone)
    stop_local = datetime.combine(day + timedelta(days=1), time(0), zone)
    counts: Counter[str] = Counter()
    previous_day = (day - timedelta(days=1)).isoformat()
    reports: list[tuple[str, str]] = []
    for valid, committed in archive.execute(
        "SELECT valid_at, canonical_committed_at FROM reported_trajectory_observations"
        " WHERE station_id=? AND source_lane=? AND valid_at>=? AND valid_at<?",
        (station_id, SOURCE_LANE, start_local.astimezone(timezone.utc).date().isoformat(),
         (stop_local.astimezone(timezone.utc) + timedelta(days=1)).date().isoformat()),
    ):
        v = _utc(valid)
        if not start_local <= v < stop_local:
            continue
        local_day = v.astimezone(zone).date().isoformat()
        if v < end_local:
            counts[local_day] += 1
        if local_day == previous_day or v >= end_local:
            c = _utc(committed) if committed else v
            reports.append((v.isoformat().replace("+00:00", "Z"), c.isoformat().replace("+00:00", "Z")))
    full = [counts[(day - timedelta(days=k)).isoformat()] for k in range(1, RATE_DAYS + 1)]
    full = [n for n in full if n]
    rate = statistics.median(full) / 24.0 if len(full) >= RATE_MIN_DAYS else FLEET_RATE_PER_HOUR
    return {"schema": SCHEMA, "rate_per_hour": float(rate), "rate_days": len(full),
            "reports": sorted(reports), "event_date": event_date, "timezone_name": timezone_name}


def report_gap_features(context: Mapping[str, Any] | None, *, arrived_valid_utc: Sequence[Any],
                        as_of_utc: Any) -> dict[str, float | None]:
    """``report_gap_h`` and ``report_missing_n`` at ``as_of_utc``: the
    context's reports that had arrived by then, with the caller's own arrived
    AWC reports (a report the caller holds but the context read predates);
    both None without a context."""

    if not context:
        return {"report_gap_h": None, "report_missing_n": None}
    zone = ZoneInfo(str(context["timezone_name"]))
    as_of = _utc(as_of_utc)
    day = str(context["event_date"])
    seen = {v for v in (_utc(x) for x in arrived_valid_utc) if v <= as_of} | {
        _utc(v) for v, c in context.get("reports") or () if _utc(c) <= as_of and _utc(v) <= as_of
    }
    today = [v for v in seen if v.astimezone(zone).date().isoformat() == day]
    candidates = list(seen)
    gap = min(GAP_CAP_H, (as_of - max(candidates)).total_seconds() / 3600.0) if candidates else GAP_CAP_H
    midnight = datetime.combine(date.fromisoformat(day), time(0), zone)
    elapsed = max(0.0, min(24.0, (as_of - midnight).total_seconds() / 3600.0))
    missing = max(0.0, float(context["rate_per_hour"]) * elapsed - len(today))
    return {"report_gap_h": round(max(0.0, gap), 4), "report_missing_n": round(missing, 4)}


__all__ = ["FLEET_RATE_PER_HOUR", "GAP_CAP_H", "RATE_DAYS", "SCHEMA", "report_context", "report_gap_features"]
