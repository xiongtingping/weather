"""方向信号 -- the ECMWF momentum and the four-feature direction model, one
module every lane reads (charter 266).

User rulings 2026-10-10 「落地三档」 (the copy-follow lane's stake tiers) and
「动量和四特征模型：解耦出来，其他策略比如proper线也可以使用」: the signals live
here, lane-free; a lane decides what to do with them.  Research:
``docs/research/benchmark_account_0x8fbd_20261008`` rounds 19-22 (README).

The axis is the market's own modal bucket: **+1 = the day lands warmer than
the mode, -1 = colder**.  A leg's direction is its bucket's side of the mode,
reversed for NO; a leg on the mode has none.

* **momentum M** (round 19, unfitted): the sum over four ECMWF-family
  components of (change / that component's spread over 09-22..10-08) --
  AIFS ENS newest run minus the previous (0.369 C), IFS ENS likewise
  (0.383 C), HRES likewise (0.676 C), AIFS single newest minus two runs back
  (0.473 C).  A run counts from the instant production had it
  (``run_change.family_runs``: the native ensemble's earliest
  ``archive_query_at_utc``, the point run's ``knowable_at_utc``), never a
  publication schedule.  |M| < 1 is quiet.
* **model score** (round 22): ``0.056*M/2 + 0.014*z(ICON shortwave)
  - 0.045*z(dew point, six-model mean) + 0.032*(-z(ECMWF 10 m wind))``, the
  slope of an OLS of the warm side's direction excess over the market's own
  expectation on every station-day of 09-22..10-08 (not on the cluster's
  picks); a missing feature is zero; its sign is the side, zero is none.
* **physics z**: the 09-16 local mean of an Open-Meteo previous-runs hourly
  series -- ``previous_day1`` once the event day's eve 08Z has passed,
  ``previous_day2`` once the day before that 08Z has, else none -- against the
  same station's other days in the window (at least six, population sd).
  Open-Meteo is an ``aggregator_republisher``: these features carry that
  lineage and serve only a lane's execution choice (which legs, what size),
  never a belief (P0/P0-M/P1), a certificate or a formal authority.  The
  series are read off the writer by the resident's own executor
  (``PhysicsCache.refresh``) into memory and one runtime file; a lane reads
  what is in hand and never waits on the network.

Pure reads of the SSOT; the only write is the physics runtime file.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import run_change

RULE = "direction_signal_momentum_four_feature_v1"

# ---------------------------------------------------------------------------
# Momentum.

#: (name, system, runs back, the change's spread in C over 09-22..10-08).
MOMENTUM_COMPONENTS: tuple[tuple[str, str, int, float], ...] = (
    ("aifs_ens", "ECMWF_AIFS_ENS_GLOBAL", 1, 0.369),
    ("ifs_ens", "ECMWF_IFS_ENS_OPEN_PF50", 1, 0.383),
    ("hres", "ECMWF_IFS_9KM_RELAY_POINT", 1, 0.676),
    ("aifs_single", "ECMWF_AIFS_SINGLE_POINT", 2, 0.473),
)
MOMENTUM_SYSTEMS = tuple(dict.fromkeys(system for _name, system, _back, _sd in MOMENTUM_COMPONENTS))
#: |M| under this is quiet: no side.
MOMENTUM_QUIET_BELOW = 1.0

# ---------------------------------------------------------------------------
# The four-feature model.

#: Slopes of the round-22 fit on every station-day (the intercept is the
#: market's average lean and is not part of the signal).
MODEL_COEFFICIENTS: dict[str, float] = {
    "momentum_half": 0.056,
    "icon_shortwave": 0.014,
    "dew_point_consensus": -0.045,
    "ecmwf_wind_inverse": 0.032,
}

# ---------------------------------------------------------------------------
# Physics features (Open-Meteo previous runs).

PHYSICS_SOURCE = "open_meteo_previous_runs"
PHYSICS_LINEAGE = "aggregator_republisher"
PHYSICS_API_URL = os.environ.get("PM_DIRECTION_PHYSICS_API", "https://previous-runs-api.open-meteo.com/v1/forecast")
PHYSICS_MODELS = ("icon_global", "ecmwf_ifs025", "gfs_global", "ukmo_global_deterministic_10km", "jma_gsm",
                  "meteofrance_arpege_world")
PHYSICS_VARIABLES = ("shortwave_radiation", "dew_point_2m", "wind_speed_10m")
#: Local hours 09..16, all eight required for a day's mean.
DAYTIME_HOURS = tuple(range(9, 17))
#: The window a station's series is fetched over, in local days around today.
WINDOW_PAST_DAYS = 16
WINDOW_FUTURE_DAYS = 2
#: A station's series is re-read when older than this.
REFRESH_SECONDS = 3 * 3600.0
#: The z baseline needs this many other days.
BASELINE_MIN_DAYS = 6
#: The six-model dew-point mean needs this many models.
DEW_POINT_MIN_MODELS = 3
#: The UTC hour on the eve (and the day before) after which previous_day1
#: (previous_day2) is the lag read -- the research's clock.
LAG_SWITCH_UTC_HOUR = 8
#: Resource bounds (AGENTS: every capture bounded).
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 30.0
MAX_WORKERS = 4
REFRESH_DEADLINE_SECONDS = 240.0
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "runtime" / "direction_physics_v1.json"
FILE_SCHEMA = "direction_physics_cache_v1"

Fetch = Callable[[str], Mapping[str, Any]]


def _instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sign(value: float | None, *, quiet_below: float = 0.0) -> int:
    if value is None or not math.isfinite(float(value)) or abs(float(value)) < quiet_below or float(value) == 0.0:
        return 0
    return 1 if float(value) > 0.0 else -1


# ---------------------------------------------------------------------------
# Momentum.


def momentum(conn: sqlite3.Connection, *, station_id: str, event_date: str, at_utc: Any) -> dict[str, Any]:
    """``{"M", "side", "components"}`` at ``at_utc``: M is None when no
    component has its runs in hand; ``side`` is 0 when quiet."""

    runs = run_change.family_runs(conn, station_id=station_id, event_date=event_date, systems=MOMENTUM_SYSTEMS)
    moment = _instant(at_utc)
    components: dict[str, dict[str, Any]] = {}
    for name, system, back, spread in MOMENTUM_COMPONENTS:
        held = [row for row in runs.get(system) or () if row[0] <= moment]
        if len(held) < back + 1:
            continue
        newest, older = held[-1], held[-1 - back]
        delta = float(newest[2]) - float(older[2])
        components[name] = {
            "system": system, "delta_c": round(delta, 4), "z": round(delta / spread, 4),
            "newest_run_init_utc": str(newest[1]), "older_run_init_utc": str(older[1]),
            "newest_had_at_utc": _z(newest[0]),
        }
    total = round(sum(part["z"] for part in components.values()), 4) if components else None
    return {"M": total, "side": _sign(total, quiet_below=MOMENTUM_QUIET_BELOW), "components": components}


# ---------------------------------------------------------------------------
# Physics: fetch, daily means, features.


def _window(today: date) -> tuple[str, str]:
    return ((today - timedelta(days=WINDOW_PAST_DAYS)).isoformat(),
            (today + timedelta(days=WINDOW_FUTURE_DAYS)).isoformat())


def physics_url(*, latitude: float, longitude: float, timezone_name: str, start_date: str, end_date: str) -> str:
    hourly = ",".join(f"{v}_previous_day{k}" for v in PHYSICS_VARIABLES for k in (1, 2))
    query = urllib.parse.urlencode({
        "latitude": f"{float(latitude):.4f}", "longitude": f"{float(longitude):.4f}",
        "start_date": start_date, "end_date": end_date, "hourly": hourly,
        "models": ",".join(PHYSICS_MODELS), "timezone": timezone_name,
    })
    return f"{PHYSICS_API_URL}?{query}"


def _http_fetch(url: str) -> Mapping[str, Any]:
    import httpx  # noqa: PLC0415 -- the resident's HTTP client, proxy-aware

    body = bytearray()
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, headers={"User-Agent": "weather-direction-signal/1"},
                      follow_redirects=False) as client:
        with client.stream("GET", url) as response:
            # A 400 carries the API's own JSON reason, kept as the receipt;
            # every other error status is raised.
            if response.status_code >= 400 and response.status_code != 400:
                response.raise_for_status()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("direction_physics_response_too_large")
    return json.loads(bytes(body))


def daily_means(payload: Mapping[str, Any]) -> dict[str, dict[str, list[float | None]]]:
    """``"variable|model" -> {local_date: [previous_day1 mean, previous_day2
    mean]}`` over ``DAYTIME_HOURS``, a mean only where all eight hours are
    present.  The payload's times are local (the request names the zone)."""

    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping) or not isinstance(hourly.get("time"), list):
        raise ValueError("direction_physics_hourly_missing")
    times = [str(t) for t in hourly["time"]]
    out: dict[str, dict[str, list[float | None]]] = {}
    for variable in PHYSICS_VARIABLES:
        for model in PHYSICS_MODELS:
            per_day: dict[str, list[list[float]]] = {}
            for lag in (1, 2):
                series = hourly.get(f"{variable}_previous_day{lag}_{model}")
                if not isinstance(series, list) or len(series) != len(times):
                    continue
                for stamp, value in zip(times, series):
                    if value is None or len(stamp) < 13 or int(stamp[11:13]) not in DAYTIME_HOURS:
                        continue
                    per_day.setdefault(stamp[:10], [[], []])[lag - 1].append(float(value))
            out[f"{variable}|{model}"] = {
                day: [round(statistics.mean(v), 4) if len(v) == len(DAYTIME_HOURS) else None for v in pair]
                for day, pair in sorted(per_day.items())
            }
    return out


def physics_lag(event_date: str, at_utc: Any) -> int | None:
    """0 (previous_day1) once the event day's eve 08Z has passed, 1
    (previous_day2) once the day before that 08Z has, else None."""

    day = date.fromisoformat(str(event_date))
    moment = _instant(at_utc)
    base = datetime(day.year, day.month, day.day, LAG_SWITCH_UTC_HOUR, tzinfo=UTC)
    if moment >= base - timedelta(days=1):
        return 0
    if moment >= base - timedelta(days=2):
        return 1
    return None


def _zscore(daily: Mapping[str, Sequence[float | None]] | None, event_date: str, lag: int) -> float | None:
    if not daily or event_date not in daily or daily[event_date][lag] is None:
        return None
    others = [pair[lag] for day, pair in daily.items() if day != event_date and pair[lag] is not None]
    if len(others) < BASELINE_MIN_DAYS:
        return None
    spread = statistics.pstdev(others)
    if not spread > 0.0:
        return None
    return (float(daily[event_date][lag]) - statistics.mean(others)) / spread


def physics_features(station_doc: Mapping[str, Any] | None, *, event_date: str, at_utc: Any) -> dict[str, Any]:
    """The three physics features at ``at_utc`` (+ = warmer), each None when
    it cannot be stated, with the lag read and the doc's fetch time."""

    out: dict[str, Any] = {"icon_shortwave": None, "dew_point_consensus": None, "ecmwf_wind_inverse": None,
                           "lag": None, "fetched_at_utc": None, "state": "ok"}
    lag = physics_lag(event_date, at_utc)
    if station_doc is None:
        return {**out, "state": "no_physics"}
    out["fetched_at_utc"] = station_doc.get("fetched_at_utc")
    if lag is None:
        return {**out, "state": "before_lag_window"}
    out["lag"] = ("previous_day1", "previous_day2")[lag]
    daily = station_doc.get("daily") or {}
    shortwave = _zscore(daily.get("shortwave_radiation|icon_global"), event_date, lag)
    dew = [z for z in (_zscore(daily.get(f"dew_point_2m|{m}"), event_date, lag) for m in PHYSICS_MODELS)
           if z is not None]
    wind = _zscore(daily.get("wind_speed_10m|ecmwf_ifs025"), event_date, lag)
    out["icon_shortwave"] = round(shortwave, 4) if shortwave is not None else None
    out["dew_point_consensus"] = round(statistics.mean(dew), 4) if len(dew) >= DEW_POINT_MIN_MODELS else None
    out["ecmwf_wind_inverse"] = round(-wind, 4) if wind is not None else None
    return out


def model_score(momentum_value: float | None, features: Mapping[str, Any]) -> float:
    """The four-feature score; a missing input counts zero."""

    parts = {
        "momentum_half": (float(momentum_value) / 2.0) if momentum_value is not None else 0.0,
        "icon_shortwave": float(features.get("icon_shortwave") or 0.0),
        "dew_point_consensus": float(features.get("dew_point_consensus") or 0.0),
        "ecmwf_wind_inverse": float(features.get("ecmwf_wind_inverse") or 0.0),
    }
    return round(sum(MODEL_COEFFICIENTS[k] * v for k, v in parts.items()), 6)


class PhysicsCache:
    """Each station's daily physics means, as last fetched -- in memory, and
    in one runtime file so a restart starts from the last fetch."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._lock = threading.Lock()
        self._stations: dict[str, dict[str, Any]] | None = None

    def _loaded(self) -> dict[str, dict[str, Any]]:
        if self._stations is None:
            stations: dict[str, dict[str, Any]] = {}
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
                if document.get("schema") == FILE_SCHEMA and isinstance(document.get("stations"), Mapping):
                    stations = {str(k): dict(v) for k, v in document["stations"].items() if isinstance(v, Mapping)}
            except (OSError, ValueError):
                stations = {}
            self._stations = stations
        return self._stations

    def station_doc(self, station_id: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._loaded().get(str(station_id).upper())
            return dict(doc) if doc is not None else None

    def due(self, stations: Mapping[str, str], *, now_utc: Any) -> list[str]:
        """Stations whose series are missing, older than ``REFRESH_SECONDS``,
        in another zone, or whose window no longer reaches today's horizon."""

        moment = _instant(now_utc)
        with self._lock:
            held = self._loaded()
            out = []
            for station, zone in sorted(stations.items()):
                doc = held.get(station)
                today = moment.astimezone(ZoneInfo(zone)).date()
                if (doc is None or doc.get("timezone") != zone
                        or str(doc.get("window", ["", ""])[1]) < _window(today)[1]
                        or (moment - _instant(doc["fetched_at_utc"])).total_seconds() > REFRESH_SECONDS):
                    out.append(station)
            return out

    def refresh(
        self, stations: Mapping[str, str], *, now_utc: Any = None, fetch: Fetch | None = None,
        coordinates: Callable[[str], Mapping[str, Any]] | None = None, clock: Callable[[], float] = time.monotonic,
        deadline_seconds: float = REFRESH_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        """Fetch every due station (``station -> timezone``) in parallel and
        keep what came back; never raises.  Run off the writer."""

        if coordinates is None:
            from .station_registry import station_coordinates as coordinates  # noqa: PLC0415
        moment = _instant(now_utc) if now_utc is not None else datetime.now(UTC)
        fetcher = fetch or _http_fetch
        due = self.due(stations, now_utc=moment)
        receipt: dict[str, Any] = {"stations": len(stations), "due": len(due), "ok": 0, "failed": {}}
        if not due:
            return receipt
        started = clock()

        def one(station: str) -> tuple[str, Any]:
            if clock() - started > deadline_seconds:
                return station, "deadline"
            try:
                zone = stations[station]
                where = coordinates(station)
                start, end = _window(moment.astimezone(ZoneInfo(zone)).date())
                payload = fetcher(physics_url(latitude=float(where["latitude"]), longitude=float(where["longitude"]),
                                              timezone_name=zone, start_date=start, end_date=end))
                if isinstance(payload, Mapping) and payload.get("error"):
                    return station, f"api:{str(payload.get('reason') or '')[:100]}"
                return station, {"fetched_at_utc": _z(moment), "timezone": zone, "window": [start, end],
                                 "source": PHYSICS_SOURCE, "lineage": PHYSICS_LINEAGE,
                                 "daily": daily_means(payload)}
            except Exception as exc:  # noqa: BLE001 -- one station's failure is its own receipt
                return station, f"{type(exc).__name__}:{exc}"[:160]

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(due)), thread_name_prefix="direction-physics") as pool:
            results = list(pool.map(one, due))
        with self._lock:
            held = self._loaded()
            for station, doc in results:
                if isinstance(doc, str):
                    receipt["failed"][station] = doc
                    continue
                held[station] = doc
                receipt["ok"] += 1
            if receipt["ok"]:
                self._persist(held, moment)
        receipt["seconds"] = round(clock() - started, 2)
        return receipt

    def _persist(self, held: Mapping[str, Any], moment: datetime) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"schema": FILE_SCHEMA, "written_at_utc": _z(moment),
                                         "stations": held}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)


#: The resident's one cache: its executor refreshes it, the lanes read it.
PHYSICS = PhysicsCache()


def fleet_timezones(conn: sqlite3.Connection) -> dict[str, str]:
    """``station -> settlement timezone`` for every station with a timezone
    binding and a canonical coordinate (AGENTS: zones only from the binding)."""

    from .station_registry import coordinate_registry_manifest  # noqa: PLC0415

    known = set(coordinate_registry_manifest()["station_ids"])
    return {str(s).upper(): str(z) for s, z in conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ) if str(s).upper() in known and z}


# ---------------------------------------------------------------------------
# The signal, the market's mode and a leg's side of it.


def direction_signal(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, at_utc: Any, physics: PhysicsCache | None = None,
) -> dict[str, Any]:
    """Momentum and the four-feature model at ``at_utc``: sides on the
    warm(+1)/cold(-1) axis, 0 for quiet or none, with every input stated."""

    moved = momentum(conn, station_id=station_id, event_date=event_date, at_utc=at_utc)
    cache = PHYSICS if physics is None else physics
    features = physics_features(cache.station_doc(station_id), event_date=event_date, at_utc=at_utc)
    score = model_score(moved["M"], features)
    return {
        "rule": RULE, "at_utc": _z(_instant(at_utc)),
        "momentum": moved["M"], "momentum_side": moved["side"], "momentum_components": moved["components"],
        "model_score": score, "model_side": _sign(score),
        "physics": {**features, "source": PHYSICS_SOURCE, "lineage": PHYSICS_LINEAGE},
    }


def market_mode_at(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, at_utc: Any, max_age_seconds: float = 1800.0,
) -> dict[str, Any]:
    """The market's modal bucket on the newest committed WS snapshot at or
    before ``at_utc`` (no older than ``max_age_seconds``): the canonical
    market vector (``market_reference_v1``), the largest normalised mid, the
    lower rung on a tie (charter 234/249).  ``{"state": "ok", "mode",
    "ordinals", "observed_at", "snapshot_id"}`` or a named state."""

    from .basket_kernel.valuation_v1 import market_reference_v1  # noqa: PLC0415
    from .proper_bet_lane import market_mode_bucket  # noqa: PLC0415

    moment = _instant(at_utc)
    high = moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    low_moment = moment - timedelta(seconds=float(max_age_seconds))
    low = low_moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{low_moment.microsecond // 1000:03d}Z"
    row = conn.execute(
        "SELECT snapshot_id, buckets_json, observed_at FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=? AND observed_at>=?"
        " ORDER BY observed_at DESC LIMIT 1",
        (station_id, event_date, high, low),
    ).fetchone()
    if row is None:
        return {"state": "no_snapshot"}
    try:
        buckets = json.loads(row[1] or "[]") or []
    except ValueError:
        return {"state": "snapshot_unreadable"}
    market = market_reference_v1(buckets)
    if market.get("state") != "complete":
        return {"state": f"market_incomplete:{market.get('reason')}"}
    q = {str(k): float(v) for k, v in market["vector"].items()}
    ordinals = {str(b.get("bucket")): int(b.get("ordinal")) for b in buckets if b.get("ordinal") is not None}
    order = sorted(q, key=lambda b: ordinals.get(b, 0))
    mode = market_mode_bucket(q, order)
    if mode is None or mode not in ordinals:
        return {"state": "no_mode"}
    return {"state": "ok", "mode": mode, "mode_q": round(q[mode], 4), "ordinals": ordinals,
            "observed_at": str(row[2]), "snapshot_id": str(row[0])}


def leg_direction(*, bucket: str, side: str, mode: str, ordinals: Mapping[str, int]) -> int | None:
    """+1 when the leg wins on a day warmer than the mode, -1 colder, 0 on the
    mode; None when the bucket is not on the ladder."""

    if bucket not in ordinals or mode not in ordinals:
        return None
    offset = int(ordinals[bucket]) - int(ordinals[mode])
    if offset == 0:
        return 0
    warm = 1 if offset > 0 else -1
    return warm if str(side).upper() == "YES" else -warm


def agreement(signal_side: int, leg: int | None) -> str:
    """``agree`` / ``disagree`` with a leg's direction, ``quiet`` when the
    signal has no side, ``no_direction`` when the leg has none."""

    if not leg:
        return "no_direction"
    if not signal_side:
        return "quiet"
    return "agree" if int(signal_side) == int(leg) else "disagree"


def leg_reading(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, bucket: str, side: str, at_utc: Any,
    physics: PhysicsCache | None = None,
) -> dict[str, Any]:
    """Everything a lane asks of the signal about one leg at ``at_utc``: the
    market's mode then, the leg's side of it, and whether momentum and the
    model agree with that side (``agree`` / ``disagree`` / ``quiet`` /
    ``no_direction``), with the signal's inputs for the record."""

    mode = market_mode_at(conn, station_id=station_id, event_date=event_date, at_utc=at_utc)
    signal = direction_signal(conn, station_id=station_id, event_date=event_date, at_utc=at_utc, physics=physics)
    leg = (leg_direction(bucket=str(bucket), side=str(side), mode=str(mode["mode"]), ordinals=mode["ordinals"])
           if mode.get("state") == "ok" else None)
    return {
        "rule": RULE,
        "mode": {k: mode.get(k) for k in ("state", "mode", "mode_q", "observed_at", "snapshot_id")},
        "leg_direction": leg,
        "momentum": agreement(signal["momentum_side"], leg),
        "model": agreement(signal["model_side"], leg),
        "signal": signal,
    }


def refresh_fleet_physics(db_path: Path | str, *, cache: PhysicsCache | None = None) -> dict[str, Any]:
    """The resident's executor job: the fleet's zones off a read-only
    connection (closed at once), then the cache refresh.  Never raises."""

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
        try:
            conn.execute("PRAGMA query_only=1")
            zones = fleet_timezones(conn)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- a receipt, never a raise
        return {"state": "zones_unreadable", "reason": f"{type(exc).__name__}:{exc}"[:160]}
    return {"state": "ran", **(PHYSICS if cache is None else cache).refresh(zones)}


__all__ = [
    "DEFAULT_PATH", "MODEL_COEFFICIENTS", "MOMENTUM_COMPONENTS", "MOMENTUM_QUIET_BELOW", "PHYSICS", "PHYSICS_LINEAGE",
    "PHYSICS_SOURCE", "RULE", "PhysicsCache", "agreement", "daily_means", "direction_signal", "fleet_timezones",
    "leg_direction", "leg_reading", "market_mode_at", "model_score", "momentum", "physics_features", "physics_lag",
    "refresh_fleet_physics",
]
