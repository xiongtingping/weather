"""Relayed deterministic forecasts, one named run at a time, many stations a call.

Charter 201 (2026-09-23, user ruling 「放行转发源」): ECMWF IFS at 9 km and the
Met Office global model at 10 km join the deterministic-forecast family
through the Open-Meteo single-runs endpoint, the transport the CMA lane has
used since charter 152.  What the relay is, and what a row may claim:

* **Identity.**  The request names the model and the run (``run=``); the
  answer is that run or "not available" (``RunNotPublished``).  A row's run
  label is the run, never one derived from the clock.
* **Cell.**  The aggregator chooses the grid cell itself; ``cell_selection=
  land`` (its default, stated explicitly) takes a land cell near the
  coordinates at a similar elevation -- the relay's own form of charter 200's
  land rule.  The values carry the aggregator's elevation adjustment.
* **As-of.**  A row is knowable when WE fetched it (``archive_query_at_utc``
  = ``knowable_at_utc`` on a live pass), not at the model's init time: the
  relay holds a run hours after the originating centre publishes it (CMA
  10-11 h, measured 2026-09-19), and the feature may only ever read what the
  relay actually had.
* **Budget.**  Every request is charged to ``open_meteo_quota`` first (one
  call per location), refused when the lane's day is spent, and a "daily
  limit" answer stops every relay call until the next UTC midnight.

One request per (model, run) for every station the pass wants: the answer is
a list, one series per location, in the order asked.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import httpx

from . import open_meteo_quota as quota
from .contracts import ContractError
from .pass_deadline import PassDeadlineExceeded, deadline_spent

BASE_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
QUOTA_LANE = "deterministic_relay"
# product -> (aggregator model name, forecast hours asked for).  Ninety-six
# hours reaches the afternoon of D+2 at every roster station from any run
# issued the day before D+1.
MODELS: Mapping[str, tuple[str, int]] = {
    "IFS9KM": ("ecmwf_ifs", 96),
    "UKMO10": ("ukmo_global_deterministic_10km", 96),
}
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_ATTEMPTS = 3
_client_instance: httpx.Client | None = None


class RunNotPublished(ContractError):
    """The relay does not hold this run yet."""


class RelayPassDeadlineExceeded(PassDeadlineExceeded):
    """The pass ran out of wall clock inside a relay request."""


def _client() -> httpx.Client:
    global _client_instance
    if _client_instance is None:
        # The relay is reached through the production egress like every
        # other transport; no proxy override here.
        _client_instance = httpx.Client(timeout=_TIMEOUT)
    return _client_instance


def close_client() -> None:
    global _client_instance
    if _client_instance is not None:
        _client_instance.close()
        _client_instance = None


def _run_param(run_init: datetime) -> str:
    return run_init.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def source_url(product: str, run_init: datetime) -> str:
    model, hours = MODELS[product]
    return (
        f"{BASE_URL}?models={model}&run={_run_param(run_init)}"
        f"&hourly=temperature_2m&forecast_hours={hours}&cell_selection=land&timezone=UTC"
    )


def fetch_run_series(
    product: str, run_init: datetime, points: Sequence[tuple[float, float]],
    *, forecast_hours: int | None = None, now: datetime | None = None,
) -> list[dict[datetime, float]]:
    """This run's hourly 2 m temperature at every point, in the order asked.

    ``RunNotPublished`` when the relay does not hold the run; ``QuotaRefused``
    when the budget says no (nothing is sent); ``ContractError`` otherwise.
    """

    if not points:
        return []
    model, default_hours = MODELS[product]
    hours = int(forecast_hours or default_hours)
    params = {
        "latitude": ",".join(f"{float(lat):.5f}" for lat, _lon in points),
        "longitude": ",".join(f"{float(lon):.5f}" for _lat, lon in points),
        "hourly": "temperature_2m", "models": model, "run": _run_param(run_init),
        "forecast_hours": hours, "cell_selection": "land", "timezone": "UTC",
    }
    last: Exception | None = None
    for attempt in range(_ATTEMPTS):
        if deadline_spent():
            raise RelayPassDeadlineExceeded("open_meteo_relay_pass_deadline_exceeded")
        # Charged per attempt: a retried request is a request the relay counts.
        quota.charge(QUOTA_LANE, len(points), now)
        try:
            response = _client().get(BASE_URL, params=params)
        except httpx.HTTPError as exc:
            last = exc
            continue
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        reason = str(payload.get("reason") or "") if isinstance(payload, dict) else ""
        kind = quota.classify(reason, response.status_code)
        if kind == "not_available":
            raise RunNotPublished(f"open_meteo_relay_run_not_published:{product}")
        if kind == "day":
            quota.mark_exhausted(now)
            raise quota.QuotaRefused("open_meteo_day_exhausted")
        if kind == "minute":
            quota.throttle_pause(attempt)
            last = ContractError("open_meteo_relay_rate_limited")
            continue
        if response.status_code != 200:
            last = ContractError(f"open_meteo_relay_http_{response.status_code}")
            continue
        items = payload if isinstance(payload, list) else [payload]
        if len(items) != len(points):
            raise ContractError("open_meteo_relay_location_count_mismatch")
        out: list[dict[datetime, float]] = []
        for item in items:
            hourly = (item or {}).get("hourly") or {}
            stamps = hourly.get("time") or []
            values = hourly.get("temperature_2m") or []
            if len(stamps) != len(values):
                raise ContractError("open_meteo_relay_series_malformed")
            series: dict[datetime, float] = {}
            for stamp, value in zip(stamps, values):
                if value is None:
                    continue
                series[datetime.fromisoformat(str(stamp)).replace(tzinfo=timezone.utc)] = float(value)
            out.append(series)
        return out
    raise last if last is not None else ContractError("open_meteo_relay_unreachable")


def run_published(product: str, run_init: datetime, point: tuple[float, float]) -> bool:
    """One one-location, one-hour call: does the relay hold this run yet?"""

    try:
        fetch_run_series(product, run_init, [point], forecast_hours=1)
    except RunNotPublished:
        return False
    return True


def local_day_hours(
    *, run_init: datetime, event_date: date, utc_offset_hours: int, max_hour: int,
) -> list[int]:
    start = datetime.combine(event_date, datetime.min.time(), tzinfo=timezone.utc) - timedelta(
        hours=utc_offset_hours
    )
    return [
        step for step in range(0, max_hour + 1)
        if start <= run_init + timedelta(hours=step) < start + timedelta(days=1)
    ]


def fetch_relay_run_for_targets(
    *, product: str, run_init: datetime, targets: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Every target's local-day curve from ONE request for the run."""

    _model, hours = MODELS[product]
    stations: dict[str, tuple[float, float]] = {}
    for target in targets:
        stations.setdefault(
            str(target["station_id"]), (float(target["latitude"]), float(target["longitude"]))
        )
    order = sorted(stations)
    # One location, one hour first: the relay charges a "not available"
    # answer per location like a real one, and the capture asks for the
    # newest run on every pass its doorbell rings (any of the family's
    # products landing a run).  Asked roster-wide, an unpublished IFS 9 km /
    # UKMO 10 km run cost 49 a time and spent the lane's 1,500 by 03Z on
    # 2026-11-01, leaving both relays (and momentum M's hres) a day behind.
    if len(order) > 1 and not run_published(product, run_init, stations[order[0]]):
        raise RunNotPublished(f"open_meteo_relay_run_not_published:{product}")
    series = fetch_run_series(product, run_init, [stations[s] for s in order])
    by_station = dict(zip(order, series))
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for target in targets:
        station = str(target["station_id"])
        wanted = local_day_hours(
            run_init=run_init, event_date=target["event_date"],
            utc_offset_hours=int(target["utc_offset_hours"]), max_hour=hours - 1,
        )
        curve = by_station.get(station) or {}
        values = {
            hour: curve[run_init + timedelta(hours=hour)]
            for hour in wanted if (run_init + timedelta(hours=hour)) in curve
        }
        results[(station, target["event_date"].isoformat())] = {
            "hourly_c": values, "hours_wanted": len(wanted),
            "source_url": source_url(product, run_init),
        }
    return results


__all__ = [
    "BASE_URL", "MODELS", "QUOTA_LANE", "RunNotPublished", "close_client",
    "fetch_relay_run_for_targets", "fetch_run_series", "run_published", "source_url",
]
