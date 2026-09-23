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

  * Runs are addressed by name (2026-09-19, v3).  Until then the lane read
    the aggregator's "current" forecast, which carries no run stamp, and
    DERIVED the cycle from the clock (four hours of assumed lag).  Checked
    2026-09-19 against the aggregator's single-run endpoint, which does
    address runs: every v2 row was the run six hours older than its label
    (labelled 06Z = the 00Z run, 12Z = 06Z, 18Z = 12Z), and 52 of 360
    consecutive labels repeated the previous row outright -- the
    aggregator holds CMA ten to eleven hours after init, not four.  So P1
    read a curve six hours staler than it believed, and the twelve-hour
    admissibility cut was measured against the wrong origin.  The lane
    now asks the single-run endpoint for a named cycle; a cycle not yet
    published answers so, and costs one request.

  * There is still NO backfill.  The historical archive is
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

Capture only.  Since charter 150 the lane's rows reach P1 as covariates of
the joint observation likelihood (``regional_features``), like every other
regional lane's.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from . import open_meteo_quota as quota
from .contracts import ContractError

TRANSPORT_PROVIDER_ID = "OPEN_METEO_CMA_GRAPES_GLOBAL"
SYSTEM_ID = "CMA_GRAPES_GLOBAL_POINT"
# Its own family: no seated system shares CMA physics.  The four global
# ensembles that do serve P0 are ECMWF, GEFS, ICON and GEM.
FAMILY_ID = "CMA_GRAPES"
PROVIDER_ID = "CMA"
_BASE = "https://single-runs-api.open-meteo.com/v1/forecast"
QUOTA_LANE = "deterministic_relay"
_MODEL = "cma_grapes_global"


class RunNotPublished(ContractError):
    """The aggregator does not hold this cycle yet."""

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
#: lane first asks for one.  Measured 2026-09-19 (single-run endpoint): the
#: aggregator holds a CMA cycle ten to eleven hours after init.  Asking from
#: six hours costs one "not published" answer per pass until it lands, which
#: is what lets a pass every twenty minutes catch a cycle within twenty.
RUN_CYCLE_HOURS = (0, 6, 12, 18)
PUBLICATION_LAG_HOURS = 6.0
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


def _run_param(run_init: datetime) -> str:
    return run_init.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def source_url(latitude: float, longitude: float, run_init: datetime) -> str:
    return (
        f"{_BASE}?latitude={latitude}&longitude={longitude}"
        f"&hourly=temperature_2m&models={_MODEL}&run={_run_param(run_init)}"
        f"&forecast_hours={MAX_FORECAST_HOUR + 1}&timezone=UTC"
    )


# Charter 201: a pass asks for one station's run once, however many of its
# target days (D-1 .. D+2) the run reaches -- the series is the same answer,
# and every call is charged to the relay budget.  Bounded: a pass touches a
# few runs x the roster.
_SERIES_CACHE: dict[tuple[float, float, str, int], dict[datetime, float]] = {}
_SERIES_CACHE_LIMIT = 512


def _hourly_series(
    latitude: float, longitude: float, run_init: datetime,
    forecast_hours: int = MAX_FORECAST_HOUR + 1,
) -> dict[datetime, float]:
    key = (float(latitude), float(longitude), _run_param(run_init), int(forecast_hours))
    cached = _SERIES_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    series = _fetch_hourly_series(latitude, longitude, run_init, forecast_hours)
    if len(_SERIES_CACHE) >= _SERIES_CACHE_LIMIT:
        _SERIES_CACHE.clear()
    _SERIES_CACHE[key] = dict(series)
    return series


def _fetch_hourly_series(
    latitude: float, longitude: float, run_init: datetime,
    forecast_hours: int = MAX_FORECAST_HOUR + 1,
) -> dict[datetime, float]:
    """This named run's hours for this point; ``RunNotPublished`` if absent.

    The endpoint caps concurrent requests and answers "Too many concurrent
    requests" rather than queueing, so that answer is waited out and
    retried; the lane only ever asks one request at a time.
    """

    params = {
        "latitude": float(latitude), "longitude": float(longitude),
        "hourly": "temperature_2m", "models": _MODEL,
        "run": _run_param(run_init), "forecast_hours": int(forecast_hours),
        "timezone": "UTC",
    }
    last: Exception | None = None
    for attempt in range(_ATTEMPTS):
        # Charter 201: every relay call is charged to the family's daily
        # Open-Meteo budget first, and none is sent once the aggregator has
        # said the day's allowance is gone (the d2 lane shares the address).
        quota.charge(QUOTA_LANE, 1)
        try:
            response = _client().get(_BASE, params=params)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            reason = str((payload or {}).get("reason") or "")
            if "not available" in reason:
                raise RunNotPublished("cma_grapes_run_not_published")
            if quota.classify(reason, response.status_code) == "day":
                quota.mark_exhausted()
                raise quota.QuotaRefused("open_meteo_day_exhausted")
            if "Too many concurrent" in reason:
                time.sleep(2.0 * (attempt + 1))
                raise ContractError("cma_grapes_point_rate_limited")
            if response.status_code != 200:
                raise ContractError(
                    f"cma_grapes_point_http_{response.status_code}"
                )
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
        except (RunNotPublished, quota.QuotaRefused):
            raise
        except Exception as exc:  # noqa: BLE001 -- retried, then surfaced
            last = exc
    raise last if last is not None else ContractError("cma_grapes_point_unreachable")


def run_published(run_init: datetime) -> bool:
    """One cheap request: does the aggregator hold this cycle yet?"""

    _station, latitude, longitude, _zone = CAPTURE_STATIONS[0]
    try:
        _hourly_series(latitude, longitude, run_init, forecast_hours=1)
    except RunNotPublished:
        return False
    return True


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
    series = _hourly_series(latitude, longitude, run_init)
    hourly_c = {}
    for hour in wanted:
        value = series.get(run_init + timedelta(hours=hour))
        if value is not None:
            hourly_c[hour] = value
    return {"hourly_c": hourly_c, "hours_wanted": wanted}


__all__ = [
    "CAPTURE_STATIONS", "FAMILY_ID", "MAX_FORECAST_HOUR", "PROVIDER_ID",
    "PUBLICATION_LAG_HOURS", "RUN_CYCLE_HOURS", "RunNotPublished", "SYSTEM_ID",
    "TRANSPORT_PROVIDER_ID", "close_client", "fetch_day_maximum",
    "local_day_hours", "run_published", "source_url",
]
