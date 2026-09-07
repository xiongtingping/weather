"""CMA GRAPES Global 2-metre temperature at a point, for the P1 lane.

Why this family exists.  Eight of the roster's forty-eight stations are in
mainland China and none of them had any regional model at all: HRRR and the
NBM are CONUS, ICON-EU stops at the Urals, and MSM's domain begins at 120E,
which reaches Shanghai and Qingdao and nothing further west.  Beijing,
Guangzhou, Shenzhen, Wuhan, Chongqing and Chengdu were served entirely by
the four global ensembles, at 25-31 km.

What is available, and what is not.  CMA's mesoscale model (CMA-MESO, 3 km)
is not published outside the CMA data service; probed 2026-09-07, the
aggregator serves no CMA regional product under any name.  What is
reachable is CMA GRAPES Global at 0.125 degrees, about 12.5 km -- coarser
than a mesoscale model and finer than every global ensemble already seated
in P0, where CMA is a declared candidate that has never actually served a
single envelope.

The honest limits of this lane, stated here because they decide what the
row may claim:

  * The aggregator does not publish the model run stamp.  ``run_init_utc``
    is therefore DERIVED -- the most recent cycle on this product's own
    published cadence whose publication lag has elapsed -- and the
    extraction version says so.  Nothing downstream reads it except run-age
    filtering and de-duplication; the as-of replay filters on
    ``knowable_at_utc``, which a forward pass stamps with its own clock and
    which is therefore exact.

  * For that reason there is NO backfill.  The historical archive is
    reachable and its accuracy is ordinary rather than suspicious -- 1.197C
    MAE against settled truth over 208 station-days, cold by 0.645C -- but
    its series is not resolved by run, so a backfilled row cannot honestly
    say when it became knowable.  Stamping it early would leak the
    afternoon into the morning; stamping it late enough to be safe would
    make it unusable for the same-day question this lane asks.  So the lane
    accumulates forward only, as the ICON-EU lane does for its own reason.

What the archive already says about the candidate, measured before any of
this was built (2026-08-10..09-05, 208 station-days): raw MAE 1.197C,
per-station debias out of sample 1.022C, against the incumbent P1
posterior's own 1.045C at the same stations.  That is a tie, and the MSM
precedent says a tie in point accuracy does not become a distributional
gain.  Two stations are the exception and are the reason to capture rather
than to decline: Chongqing debiases to 0.515C and Shanghai to 0.472C, both
basin and coastal sites of exactly the kind a 25 km cell smooths away.

Capture is not admission.  Nothing here reaches a served distribution
until scripts/measure_regional_serving_value.py says it should.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from .contracts import ContractError

TRANSPORT_PROVIDER_ID = "OPEN_METEO_CMA_GRAPES_GLOBAL"
SYSTEM_ID = "CMA_GRAPES_GLOBAL_POINT"
# Its own family: no seated system shares CMA physics.  The four global
# ensembles that do serve P0 are ECMWF, GEFS, ICON and GEM.
FAMILY_ID = "CMA_GRAPES"
PROVIDER_ID = "CMA"
_BASE = "https://api.open-meteo.com/v1/forecast"
_MODEL = "cma_grapes_global"

# The roster this lane opens at: the mainland Chinese stations, which had no
# regional coverage of any kind.  Shanghai and Qingdao also fall inside MSM's
# domain and are captured there too, deliberately -- two independent readings
# of the same station-day is what lets the admission gate tell a model apart
# from a station.
CAPTURE_STATIONS = (
    ("ZBAA", 40.082, 116.603, "Asia/Shanghai"),
    ("ZGGG", 23.392, 113.307, "Asia/Shanghai"),
    ("ZGSZ", 22.639, 113.803, "Asia/Shanghai"),
    ("ZHHH", 30.783, 114.205, "Asia/Shanghai"),
    ("ZSPD", 31.146, 121.800, "Asia/Shanghai"),
    ("ZSQD", 36.362, 120.087, "Asia/Shanghai"),
    ("ZUCK", 29.718, 106.639, "Asia/Shanghai"),
    ("ZUUU", 30.576, 103.950, "Asia/Shanghai"),
)

#: Cycles CMA GRAPES Global issues, and how long after its nominal hour the
#: aggregator has it.  Four hours is measured slack, not a published figure:
#: the aggregator republishes on its own schedule and a shorter lag would
#: name a cycle that is not there yet.
RUN_CYCLE_HOURS = (0, 6, 12, 18)
PUBLICATION_LAG_HOURS = 4.0
MAX_FORECAST_HOUR = 78

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_ATTEMPTS = 3
_client_instance: httpx.Client | None = None


def _client() -> httpx.Client:
    global _client_instance
    if _client_instance is None:
        _client_instance = httpx.Client(timeout=_TIMEOUT)
    return _client_instance


def close_client() -> None:
    global _client_instance
    if _client_instance is not None:
        _client_instance.close()
        _client_instance = None


def derived_run_init(now: datetime) -> datetime:
    """The cycle in force at ``now`` on this product's own cadence.

    Derived, not read: the aggregator publishes no run stamp.  A row's
    honesty rests on ``knowable_at_utc``, which the writer stamps with the
    fetch clock; this only labels the row and keeps one row per cycle.
    """

    stamp = now.astimezone(timezone.utc) - timedelta(hours=PUBLICATION_LAG_HOURS)
    for hour in sorted(RUN_CYCLE_HOURS, reverse=True):
        if stamp.hour >= hour:
            return stamp.replace(hour=hour, minute=0, second=0, microsecond=0)
    previous = stamp - timedelta(days=1)
    return previous.replace(
        hour=max(RUN_CYCLE_HOURS), minute=0, second=0, microsecond=0
    )


def local_day_hours(
    *, run_init: datetime, event_date: date, utc_offset_hours: int
) -> list[int]:
    """Forecast hours of this run that land inside the station's local day."""

    start_local = datetime.combine(
        event_date, datetime.min.time(), tzinfo=timezone.utc
    ) - timedelta(hours=utc_offset_hours)
    hours = []
    for step in range(0, MAX_FORECAST_HOUR + 1):
        stamp = run_init + timedelta(hours=step)
        if start_local <= stamp < start_local + timedelta(days=1):
            hours.append(step)
    return hours


def source_url(latitude: float, longitude: float) -> str:
    return (
        f"{_BASE}?latitude={latitude}&longitude={longitude}"
        f"&hourly=temperature_2m&models={_MODEL}&timezone=UTC"
    )


def _hourly_series(latitude: float, longitude: float) -> dict[datetime, float]:
    """Every hour the aggregator currently holds for this point."""

    params = {
        "latitude": float(latitude), "longitude": float(longitude),
        "hourly": "temperature_2m", "models": _MODEL,
        "forecast_days": 4, "past_days": 1, "timezone": "UTC",
    }
    last: Exception | None = None
    for _ in range(_ATTEMPTS):
        try:
            response = _client().get(_BASE, params=params)
            if response.status_code != 200:
                raise ContractError(
                    f"cma_grapes_point_http_{response.status_code}"
                )
            payload = response.json()
            hourly = payload.get("hourly") or {}
            stamps = hourly.get("time") or []
            values = hourly.get("temperature_2m") or []
            if not stamps or len(stamps) != len(values):
                raise ContractError("cma_grapes_point_series_malformed")
            series: dict[datetime, float] = {}
            for stamp, value in zip(stamps, values):
                if value is None:
                    continue
                series[
                    datetime.fromisoformat(str(stamp)).replace(tzinfo=timezone.utc)
                ] = float(value)
            if not series:
                raise ContractError("cma_grapes_point_series_empty")
            return series
        except Exception as exc:  # noqa: BLE001 -- retried, then surfaced
            last = exc
    raise last if last is not None else ContractError("cma_grapes_point_unreachable")


def fetch_day_maximum(
    *, run_init: datetime, event_date: date, utc_offset_hours: int,
    latitude: float, longitude: float,
) -> dict[str, Any]:
    """This run's hourly curve over one station's local day.

    Returned in the shape the lane writes: forecast-hour offsets from
    ``run_init`` to degrees Celsius, beside the hours the local day wanted.
    Hours the aggregator does not hold are simply absent, and the lane's own
    peak-coverage check decides whether what is left is worth a row.
    """

    wanted = local_day_hours(
        run_init=run_init, event_date=event_date,
        utc_offset_hours=utc_offset_hours,
    )
    if not wanted:
        return {"hourly_c": {}, "hours_wanted": []}
    series = _hourly_series(latitude, longitude)
    hourly_c = {}
    for hour in wanted:
        value = series.get(run_init + timedelta(hours=hour))
        if value is not None:
            hourly_c[hour] = value
    return {"hourly_c": hourly_c, "hours_wanted": wanted}


__all__ = [
    "CAPTURE_STATIONS", "FAMILY_ID", "MAX_FORECAST_HOUR", "PROVIDER_ID",
    "PUBLICATION_LAG_HOURS", "RUN_CYCLE_HOURS", "SYSTEM_ID",
    "TRANSPORT_PROVIDER_ID", "close_client", "derived_run_init",
    "fetch_day_maximum", "local_day_hours", "source_url",
]
