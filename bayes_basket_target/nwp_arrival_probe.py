"""Charter 195: when a model run is actually on the server we download from.

The capture used to learn that a run had landed by trying to download it: the
production ladder offered every rung whose init time had passed
(``_official_native_run_ladder``), the first try on a run still uploading
failed, and the event date then waited out a 30-minute retry floor inside a
10-minute NWP poll.  Measured 2026-09-19..21 (reading the upstream object
timestamps against the capture receipts), that discovery step alone cost a
14-23 minute median and a 49-55 minute p90 per system.

This module asks the servers instead, cheaply and on its own clock:

* ECMWF -- a HEAD on the ``.grib2`` and ``.index`` of the last step a covered
  event date needs, on the replica the capture downloads from (GCS).  Not the
  index alone: on GCS the index lands 2-3 minutes before its grib2.  The AWS
  bucket is asked the same question and recorded under its own source, so the
  transport choice can be measured before it is changed.  data.ecmwf.int is
  never listed (it answered a directory listing with 429).
* GEFS -- a HEAD on the ``.idx`` of that step for the control and the last
  perturbed member, then, once both answer, on all 31 members once.
* ICON -- one GET of DWD's ``t_2m`` directory for the cycle (a few kB).
* ECCC -- a HEAD on the ``TMP_TGL_2m`` file of that step on the datamart.

"Covered event date" and "the last step it needs" are the capture modules' own
answers (their step functions, over every station the forward registry lists),
so a run is ready here exactly when the capture could build it.

Charter 195 (一) records first-seen times only (shadow).  Nothing here starts a
capture, writes the SSOT or keeps a raw object.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import ContractError


STATE_SCHEMA = "nwp_arrival_probe_state_v1"
EVENT_SCHEMA = "nwp_arrival_probe_event_v1"
STATE_FILE_NAME = "nwp_arrival_probe_state.json"
LOG_FILE_NAME = "nwp_arrival_probe_log.jsonl"
PROBE_ENV = "PM_NWP_ARRIVAL_PROBE"
PROBE_INTERVAL_SEC = 60.0
REQUEST_TIMEOUT_SEC = 8.0
ROUND_DEADLINE_SEC = 45.0
MAX_REQUESTS_PER_ROUND = 120
# The furthest a run is asked about: its own day plus three.  The ladder
# serves 0..2 today; a lead-3 hold rung (charter 193 amendment, ECMWF D-3 18Z
# for a D+2 day) is asked about the moment the step functions accept it.
MAX_PROBE_LEAD_DAYS = 3
# Kept this long in the state file, then dropped.
STATE_RETENTION = timedelta(days=3)

ECMWF_FAMILY = "ECMWF_OPEN_DATA_S3"
GEFS_FAMILY = "NOAA_GEFS_PDS_S3"
ICON_FAMILY = "DWD_OPENDATA_ICON_EPS"
ECCC_FAMILY = "ECCC_DD_WEATHER"
# Charter 204: AIFS ENS, a follow-only family (``follow_run.AIFS_FAMILY``);
# a lane key, not a transport id -- its objects sit in PF50's bucket.
AIFS_FAMILY = "ECMWF_AIFS_ENS"
# Charter 212: NOAA AIGEFS (``follow_run.AIGEFS_FAMILY``), follow-only too.
AIGEFS_FAMILY = "NOAA_AIGEFS_NODD"
FAMILIES = (ECMWF_FAMILY, GEFS_FAMILY, ICON_FAMILY, ECCC_FAMILY, AIFS_FAMILY, AIGEFS_FAMILY)

ECMWF_GCS_BASE_URL = "https://storage.googleapis.com/ecmwf-open-data"
ECMWF_AWS_BASE_URL = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
GEFS_BASE_URL = "https://noaa-gefs-pds.s3.amazonaws.com"
ICON_BASE_URL = "https://opendata.dwd.de/weather/nwp/icon-eps/grib"
ECCC_BASE_URL = "https://dd.weather.gc.ca"

# Where each family is downloaded from, i.e. the source whose readiness a
# capture can act on.  ECMWF's AWS source is recorded beside it, never acted
# on until the transport itself changes.
DOWNLOAD_SOURCE = {
    ECMWF_FAMILY: "gcs",
    GEFS_FAMILY: "s3",
    ICON_FAMILY: "dwd",
    ECCC_FAMILY: "dd",
    AIFS_FAMILY: "gcs",
    AIGEFS_FAMILY: "s3",
}
SHADOW_SOURCES = {ECMWF_FAMILY: ("aws",)}


@dataclass(frozen=True)
class FamilySpec:
    family: str
    system_id: str
    cycles: tuple[int, ...]
    # First ask this long after init; measured 2026-09-14..21 on the
    # servers above, with ten minutes of margin before the earliest object.
    earliest_after_init_hours: Mapping[int, float]
    # Stop asking about a run this long after init; a run not complete by
    # then is answered by the capture's own retry.
    give_up_after_init_hours: float


SPECS: dict[str, FamilySpec] = {
    # Origin releases 00/12Z at T+7:40 and 06/18Z at T+7:04; the GCS replica
    # completes a D+2 day's steps 18-25 minutes later.
    ECMWF_FAMILY: FamilySpec(
        ECMWF_FAMILY, "ECMWF_IFS_ENS_OPEN_PF50", (0, 6, 12, 18),
        {0: 7.4, 6: 6.9, 12: 7.4, 18: 6.9}, 16.0,
    ),
    # f000 at T+3:46..4:00, the +72h step at T+4:13..4:31.
    GEFS_FAMILY: FamilySpec(
        GEFS_FAMILY, "NOAA_GEFS_05", (0, 6, 12, 18),
        {0: 3.6, 6: 3.6, 12: 3.6, 18: 3.6}, 12.0,
    ),
    # 00/12Z from T+2:37, 06/18Z from T+2:41 (DWD listing 2026-09-21).
    ICON_FAMILY: FamilySpec(
        ICON_FAMILY, "DWD_ICON_GLOBAL_EPS", (0, 6, 12, 18),
        {0: 2.4, 6: 2.4, 12: 2.4, 18: 2.4}, 10.0,
    ),
    # 00Z steps by T+5:05..5:17, 12Z by T+5:08..5:24.
    ECCC_FAMILY: FamilySpec(
        ECCC_FAMILY, "ECCC_GEM_GLOBAL_ENS", (0, 12),
        {0: 4.8, 12: 4.8}, 14.0,
    ),
    # Every step of all four cycles was on the GCS replica by init + 6h10m
    # on 2026-09-20 (00Z 05:59-06:09, 06Z 11:55-12:04, 12Z 17:58-18:07,
    # 18Z 23:51-00:00): 1h08m-1h41m before the IFS run of the same cycle.
    AIFS_FAMILY: FamilySpec(
        AIFS_FAMILY, "ECMWF_AIFS_ENS_GLOBAL", (0, 6, 12, 18),
        {0: 5.8, 6: 5.8, 12: 5.8, 18: 5.8}, 14.0,
    ),
    # Every step of a run lands in one minute, init + 6h21m..8h11m over the
    # 36 runs 2026-09-14..09-22 (35 of them within 6h58m).
    AIGEFS_FAMILY: FamilySpec(
        AIGEFS_FAMILY, "NOAA_AIGEFS_025", (0, 6, 12, 18),
        {0: 6.3, 6: 6.3, 12: 6.3, 18: 6.3}, 14.0,
    ),
}

GEFS_MEMBER_PREFIXES = tuple(
    "gec00" if index == 0 else f"gep{index:02d}" for index in range(31)
)
GEFS_SENTINEL_MEMBERS = ("gec00", "gep30")

_ICON_LISTING = re.compile(
    r'href="icon-eps_global_icosahedral_single-level_(\d{10})_(\d{3})_t_2m\.grib2\.bz2"'
    r"[^\n]*?(\d{2}-\w{3}-\d{4} \d{2}:\d{2}:\d{2})"
)


def probe_enabled() -> bool:
    return (
        os.environ.get(PROBE_ENV, "on").strip().lower()
        not in {"0", "off", "false", "no", ""}
    )


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


# --------------------------------------------------------------------------
# Which steps a covered event date needs -- the capture modules' own answer.
# --------------------------------------------------------------------------


def _station_steps(
    family: str,
    *,
    event_date: str,
    settlement_timezone: str,
    run_init: datetime,
) -> list[int] | None:
    """The steps this family's capture reads for one station, or None."""

    try:
        if family == ECMWF_FAMILY:
            from .ecmwf_ifs_archive import (  # noqa: PLC0415
                station_period_end_steps,
            )

            # The capture's own per-station read, follow-only reach included
            # (charter 263 (四)), so a run is not declared landed for a
            # station before the steps that station is read at are up.
            return list(station_period_end_steps(
                event_date=event_date,
                settlement_timezone=settlement_timezone,
                run_init_utc=run_init,
                lead_days=(
                    date.fromisoformat(str(event_date))
                    - run_init.astimezone(timezone.utc).date()
                ).days,
            ))
        if family == GEFS_FAMILY:
            from .noaa_gefs_archive import _forecast_hours  # noqa: PLC0415

            return list(_forecast_hours(
                event_date=event_date,
                settlement_timezone=settlement_timezone,
                run_init_utc=run_init,
            ))
        if family == ICON_FAMILY:
            from .dwd_icon_eps_archive import follow_forecast_hours  # noqa: PLC0415

            hours = follow_forecast_hours(
                event_date=event_date,
                settlement_timezone=settlement_timezone,
                run_init_utc=run_init,
            )
            return None if hours is None else list(hours)
        if family == ECCC_FAMILY:
            from .eccc_geps_archive import follow_forecast_hours  # noqa: PLC0415

            hours = follow_forecast_hours(
                event_date=event_date,
                settlement_timezone=settlement_timezone,
                run_init_utc=run_init,
            )
            return None if hours is None else list(hours)
        if family in (AIFS_FAMILY, AIGEFS_FAMILY):
            from .ecmwf_aifs_ens_archive import day_instants  # noqa: PLC0415

            steps = day_instants(
                event_date=event_date,
                settlement_timezone=settlement_timezone,
                run_init_utc=run_init,
            )
            return None if steps is None else list(steps)
    except (ContractError, ValueError, KeyError):
        return None
    return None


def needed_max_step(
    family: str,
    *,
    run_init: datetime,
    event_date: str,
    station_timezones: Mapping[str, str],
) -> int | None:
    """The last step any coverable station of this day needs from this run."""

    best: int | None = None
    for timezone_name in sorted(set(station_timezones.values())):
        steps = _station_steps(
            family,
            event_date=event_date,
            settlement_timezone=timezone_name,
            run_init=run_init,
        )
        if steps:
            top = max(int(step) for step in steps)
            best = top if best is None else max(best, top)
    return best


# --------------------------------------------------------------------------
# Object URLs, per family and source.
# --------------------------------------------------------------------------


def ecmwf_object_urls(base_url: str, run_init: datetime, step: int) -> tuple[str, str]:
    prefix = (
        f"{run_init:%Y%m%d}/{run_init:%H}z/ifs/0p25/enfo/"
        f"{run_init:%Y%m%d%H%M%S}-{int(step)}h-enfo-ef"
    )
    return f"{base_url}/{prefix}.grib2", f"{base_url}/{prefix}.index"


def aifs_object_urls(
    base_url: str, run_init: datetime, step: int, kind: str
) -> tuple[str, str]:
    prefix = (
        f"{run_init:%Y%m%d}/{run_init:%H}z/aifs-ens/0p25/enfo/"
        f"{run_init:%Y%m%d%H%M%S}-{int(step)}h-enfo-{kind}"
    )
    return f"{base_url}/{prefix}.grib2", f"{base_url}/{prefix}.index"


def aigefs_object_urls(run_init: datetime, member_number: int, step: int) -> tuple[str, str]:
    from .noaa_aigefs_archive import BASE_URL, object_key  # noqa: PLC0415

    key = object_key(run_init, member_number, step)
    return f"{BASE_URL}/{key}", f"{BASE_URL}/{key}.idx"


def gefs_index_url(run_init: datetime, member: str, step: int) -> str:
    return (
        f"{GEFS_BASE_URL}/gefs.{run_init:%Y%m%d}/{run_init:%H}/atmos/pgrb2ap5/"
        f"{member}.t{run_init:%H}z.pgrb2a.0p50.f{int(step):03d}.idx"
    )


def icon_listing_url(cycle_hour: int) -> str:
    return f"{ICON_BASE_URL}/{int(cycle_hour):02d}/t_2m/"


def eccc_object_url(run_init: datetime, step: int) -> str:
    stamp = f"{run_init:%Y%m%d%H}"
    return (
        f"{ECCC_BASE_URL}/{run_init:%Y%m%d}/WXO-DD/ensemble/geps/grib2/raw/"
        f"{run_init:%H}/{int(step):03d}/CMC_geps-raw_TMP_TGL_2m_latlon0p5x0p5_"
        f"{stamp}_P{int(step):03d}_allmbrs.grib2"
    )


# --------------------------------------------------------------------------
# HTTP, injectable for tests.
# --------------------------------------------------------------------------

# (method, url) -> (status, headers, body_text_or_empty)
Fetch = Callable[[str, str], tuple[int, Mapping[str, str], str]]


class _DefaultFetch:
    """One keep-alive client per round: a fresh TLS handshake through the
    proxy per ask cost about two seconds each."""

    def __init__(self) -> None:
        import httpx  # noqa: PLC0415

        from official_nwp_https_transport import official_nwp_proxy_url  # noqa: PLC0415

        self._client = httpx.Client(
            proxy=official_nwp_proxy_url() or None,
            trust_env=False,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SEC,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )

    def __call__(self, method: str, url: str) -> tuple[int, Mapping[str, str], str]:
        response = self._client.request(
            method,
            url,
            headers={"User-Agent": "weather-nwp-arrival-probe/1"},
        )
        body = response.text[:262144] if method == "GET" else ""
        return int(response.status_code), dict(response.headers), body

    def close(self) -> None:
        self._client.close()


class _Round:
    """One round's request budget and per-URL memo."""

    def __init__(self, fetch: Fetch, *, deadline_mono: float, max_requests: int):
        self.fetch = fetch
        self.deadline_mono = deadline_mono
        self.max_requests = max_requests
        self.requests = 0
        self.errors = 0
        self.memo: dict[tuple[str, str], tuple[int, Mapping[str, str], str]] = {}
        self.exhausted = False

    def ask(self, method: str, url: str) -> tuple[int, Mapping[str, str], str] | None:
        answers = self.ask_many(method, [url])
        return answers[0]

    def ask_many(
        self, method: str, urls: Sequence[str]
    ) -> list[tuple[int, Mapping[str, str], str] | None]:
        """Ask several at once (GEFS's 31 members), within the budget."""

        todo = [url for url in dict.fromkeys(urls) if (method, url) not in self.memo]
        room = self.max_requests - self.requests
        if todo and (room <= 0 or time.monotonic() >= self.deadline_mono):
            self.exhausted = True
        elif todo:
            if len(todo) > room:
                self.exhausted = True
            batch = todo[: max(0, room)]
            self.requests += len(batch)

            def _one(url: str) -> tuple[str, tuple[int, Mapping[str, str], str]]:
                try:
                    return url, self.fetch(method, url)
                except Exception:  # noqa: BLE001 -- a failed ask is "not yet"
                    return url, (0, {}, "")

            if len(batch) == 1:
                results = [_one(batch[0])]
            else:
                from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

                with ThreadPoolExecutor(max_workers=min(8, len(batch))) as pool:
                    results = list(pool.map(_one, batch))
            for url, answer in results:
                if answer[0] == 0:
                    self.errors += 1
                self.memo[(method, url)] = answer
        return [self.memo.get((method, url)) for url in urls]


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return ""


def _visible(
    round_: _Round,
    family: str,
    source: str,
    run_init: datetime,
    step: int,
) -> tuple[bool | None, str]:
    """(present, last_modified).  None when the round ran out of budget."""

    if family == ECMWF_FAMILY:
        base = ECMWF_GCS_BASE_URL if source == "gcs" else ECMWF_AWS_BASE_URL
        grib_url, index_url = ecmwf_object_urls(base, run_init, step)
        grib = round_.ask("HEAD", grib_url)
        if grib is None:
            return None, ""
        if grib[0] != 200:
            return False, ""
        index = round_.ask("HEAD", index_url)
        if index is None:
            return None, ""
        return index[0] == 200, _header(grib[1], "last-modified")
    if family == GEFS_FAMILY:
        sentinels = round_.ask_many(
            "HEAD",
            [gefs_index_url(run_init, member, step) for member in GEFS_SENTINEL_MEMBERS],
        )
        if any(answer is None for answer in sentinels):
            return None, ""
        if any(answer[0] != 200 for answer in sentinels):
            return False, ""
        members = round_.ask_many(
            "HEAD",
            [gefs_index_url(run_init, member, step) for member in GEFS_MEMBER_PREFIXES],
        )
        if any(answer is None for answer in members):
            return None, ""
        if any(answer[0] != 200 for answer in members):
            return False, ""
        return True, max(_header(answer[1], "last-modified") for answer in members)
    if family == ICON_FAMILY:
        answer = round_.ask("GET", icon_listing_url(run_init.hour))
        if answer is None:
            return None, ""
        if answer[0] != 200:
            return False, ""
        stamp = f"{run_init:%Y%m%d%H}"
        listed = {
            int(match.group(2)): match.group(3)
            for match in _ICON_LISTING.finditer(answer[2])
            if match.group(1) == stamp
        }
        return int(step) in listed, listed.get(int(step), "")
    if family == ECCC_FAMILY:
        answer = round_.ask("HEAD", eccc_object_url(run_init, step))
        if answer is None:
            return None, ""
        return answer[0] == 200, _header(answer[1], "last-modified")
    if family == AIFS_FAMILY:
        # The control and the perturbed members are two objects per step;
        # the run is up for this day when both indexes of its last needed
        # step are.
        base = ECMWF_GCS_BASE_URL if source == "gcs" else ECMWF_AWS_BASE_URL
        pf_grib, pf_index = aifs_object_urls(base, run_init, step, "pf")
        _cf_grib, cf_index = aifs_object_urls(base, run_init, step, "cf")
        grib = round_.ask("HEAD", pf_grib)
        if grib is None:
            return None, ""
        if grib[0] != 200:
            return False, ""
        indexes = round_.ask_many("HEAD", [pf_index, cf_index])
        if any(answer is None for answer in indexes):
            return None, ""
        return all(answer[0] == 200 for answer in indexes), _header(
            grib[1], "last-modified"
        )
    if family == AIGEFS_FAMILY:
        # The run is up for this day when the control's and the last
        # member's indexes of the last needed step are (every member lands
        # in the same minute); the last member's object gives the stamp.
        last_grib, last_index = aigefs_object_urls(run_init, 30, step)
        _grib0, index0 = aigefs_object_urls(run_init, 0, step)
        grib = round_.ask("HEAD", last_grib)
        if grib is None:
            return None, ""
        if grib[0] != 200:
            return False, ""
        indexes = round_.ask_many("HEAD", [index0, last_index])
        if any(answer is None for answer in indexes):
            return None, ""
        return all(answer[0] == 200 for answer in indexes), _header(
            grib[1], "last-modified"
        )
    return False, ""


# --------------------------------------------------------------------------
# State.
# --------------------------------------------------------------------------


def ready_key(family: str, source: str, run_init: datetime, event_date: str) -> str:
    return f"{family}|{source}|{_stamp(run_init)}|{event_date}"


def load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"schema": STATE_SCHEMA, "ready": {}}
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        return {"schema": STATE_SCHEMA, "ready": {}}
    if not isinstance(payload.get("ready"), dict):
        payload["ready"] = {}
    return payload


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, sort_keys=True, ensure_ascii=False))
    os.replace(temporary, path)


def _append_log(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def registry_scope(registry_path: Path) -> tuple[dict[str, str], list[str]]:
    """(station -> settlement timezone, listed event dates) from the forward
    market registry the capture itself reads."""

    try:
        payload = json.loads(Path(registry_path).read_text())
    except (OSError, ValueError):
        return {}, []
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    zones: dict[str, str] = {}
    dates: set[str] = set()
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        zone = str(row.get("settlement_timezone") or "").strip()
        event_date = str(row.get("event_date") or "").strip()
        if not station or not zone:
            try:
                registry = json.loads(str(row.get("registry_json") or "{}"))
            except ValueError:
                registry = {}
            station = station or str(registry.get("station_id") or "").strip().upper()
            zone = zone or str(registry.get("settlement_timezone") or "").strip()
        if station and zone:
            zones[station] = zone
        if event_date:
            dates.add(event_date)
    return zones, sorted(dates)


def candidate_runs(family: str, now: datetime) -> list[datetime]:
    """Runs of this family worth asking about now, newest first."""

    spec = SPECS[family]
    runs: list[datetime] = []
    day = now.date()
    for back in range(0, 2):
        for hour in spec.cycles:
            run_init = datetime.combine(
                day - timedelta(days=back), dt_time(hour=int(hour)), tzinfo=timezone.utc
            )
            age_h = (now - run_init).total_seconds() / 3600.0
            if spec.earliest_after_init_hours[int(hour)] <= age_h <= spec.give_up_after_init_hours:
                runs.append(run_init)
    runs.sort(reverse=True)
    return runs


def probe_round(
    *,
    runtime_root: Path,
    now: datetime | None = None,
    fetch: Fetch | None = None,
    registry_path: Path | None = None,
    families: Sequence[str] = FAMILIES,
) -> dict[str, Any]:
    """Ask once about every run that may have landed; record first sightings.

    Returns the round receipt.  Never raises on network failures -- a failed
    ask is "not yet" -- and never downloads a data object.
    """

    owned = None
    if fetch is None:
        owned = _DefaultFetch()
        fetch = owned
    try:
        return _probe_round(
            runtime_root=Path(runtime_root),
            now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc),
            fetch=fetch,
            registry_path=registry_path,
            families=families,
        )
    finally:
        if owned is not None:
            owned.close()


def _probe_round(
    *,
    runtime_root: Path,
    now: datetime,
    fetch: Fetch,
    registry_path: Path | None,
    families: Sequence[str],
) -> dict[str, Any]:
    state_path = runtime_root / STATE_FILE_NAME
    log_path = runtime_root / LOG_FILE_NAME
    state = load_state(state_path)
    ready: dict[str, Any] = state["ready"]
    zones, listed_dates = registry_scope(
        registry_path or runtime_root / "market_registry_forward.json"
    )
    round_ = _Round(
        fetch,
        deadline_mono=time.monotonic() + ROUND_DEADLINE_SEC,
        max_requests=MAX_REQUESTS_PER_ROUND,
    )
    new_rows: list[dict[str, Any]] = []
    asked = 0
    pending = 0
    for family in families:
        sources = (DOWNLOAD_SOURCE[family], *SHADOW_SOURCES.get(family, ()))
        for run_init in candidate_runs(family, now):
            for event_date in listed_dates:
                lead = (date.fromisoformat(event_date) - run_init.date()).days
                if not 0 <= lead <= MAX_PROBE_LEAD_DAYS:
                    continue
                step = needed_max_step(
                    family,
                    run_init=run_init,
                    event_date=event_date,
                    station_timezones=zones,
                )
                if step is None:
                    continue
                for source in sources:
                    key = ready_key(family, source, run_init, event_date)
                    if key in ready:
                        continue
                    asked += 1
                    present, last_modified = _visible(
                        round_, family, source, run_init, step
                    )
                    if present is None:
                        pending += 1
                        continue
                    if not present:
                        pending += 1
                        continue
                    row = {
                        "schema": EVENT_SCHEMA,
                        "family": family,
                        "system_id": SPECS[family].system_id,
                        "source": source,
                        "acts_on_capture": source == DOWNLOAD_SOURCE[family],
                        "run_init_utc": _stamp(run_init),
                        "event_date": event_date,
                        "lead_days": lead,
                        "needed_max_step": int(step),
                        "first_seen_at_utc": _stamp(now),
                        "last_modified": last_modified,
                    }
                    ready[key] = row
                    new_rows.append(row)
    floor = now - STATE_RETENTION
    for key in [
        key for key, row in ready.items()
        if not isinstance(row, Mapping)
        or _utc(str(row.get("run_init_utc") or "1970-01-01T00:00:00Z")) < floor
    ]:
        ready.pop(key, None)
    receipt = {
        "at_utc": _stamp(now),
        "asked": asked,
        "pending": pending,
        "new_ready": len(new_rows),
        "requests": round_.requests,
        "request_errors": round_.errors,
        "budget_exhausted": round_.exhausted,
        "listed_dates": listed_dates,
        "station_count": len(zones),
    }
    state["schema"] = STATE_SCHEMA
    state["updated_at_utc"] = _stamp(now)
    state["last_round"] = receipt
    _write_state(state_path, state)
    _append_log(log_path, new_rows)
    return {**receipt, "new_rows": new_rows}


def run_landed(
    state: Mapping[str, Any] | None,
    *,
    family: str,
    run_init: datetime,
    event_date: str | None,
    as_of: datetime,
    fresh_sec: float = 600.0,
) -> bool | None:
    """Whether the probe has seen this run land for this day.

    None when the probe cannot speak for it -- no fresh state, a day it does
    not list, a family it does not know, or a run outside the window it asks
    about -- so the caller keeps its own rule.
    """

    if not isinstance(state, Mapping) or event_date is None or family not in SPECS:
        return None
    try:
        updated = _utc(str(state.get("updated_at_utc") or ""))
    except ValueError:
        return None
    if abs((as_of - updated).total_seconds()) > fresh_sec:
        return None
    listed = (state.get("last_round") or {}).get("listed_dates") or ()
    if str(event_date) not in {str(day) for day in listed}:
        return None
    spec = SPECS[family]
    age_h = (as_of - run_init).total_seconds() / 3600.0
    if int(run_init.hour) not in spec.cycles or age_h > spec.give_up_after_init_hours:
        return None
    key = ready_key(family, DOWNLOAD_SOURCE[family], run_init, str(event_date))
    return key in (state.get("ready") or {})


def newest_ready_run(
    state: Mapping[str, Any],
    *,
    family: str,
    event_date: str,
    source: str | None = None,
) -> datetime | None:
    """The newest run of ``family`` the probe has seen complete for this day."""

    wanted = source or DOWNLOAD_SOURCE[family]
    best: datetime | None = None
    for row in (state.get("ready") or {}).values():
        if not isinstance(row, Mapping):
            continue
        if (
            row.get("family") != family
            or row.get("source") != wanted
            or row.get("event_date") != event_date
        ):
            continue
        run_init = _utc(str(row.get("run_init_utc")))
        if best is None or run_init > best:
            best = run_init
    return best


__all__ = [
    "DOWNLOAD_SOURCE",
    "FAMILIES",
    "LOG_FILE_NAME",
    "PROBE_INTERVAL_SEC",
    "SPECS",
    "STATE_FILE_NAME",
    "candidate_runs",
    "load_state",
    "needed_max_step",
    "newest_ready_run",
    "probe_enabled",
    "probe_round",
    "ready_key",
    "registry_scope",
    "run_landed",
]
