"""Country-authority-first raw METAR routing for the AWC execution lane.

The execution lane remains ``awc`` so market ownership and capital are still
disjoint from AMSC.  ``origin_*`` fields identify the actual national source;
NOAA/AWC is an explicit worldwide relay fallback, never a second vote.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from reported_trajectory_sources import fetch_awc_temperature_reports, infer_ddhhmm_time, utc_now


ROOT = Path(__file__).resolve().parent
OFFICIAL_SOURCE_CONFIG = ROOT / "config" / "trading" / "reported_trajectory_official_sources_v1.json"
KMA_METAR_ENDPOINT = "https://apihub.kma.go.kr/api/typ02/openApi/AmmIwxxmService/getMetar"
REDEMET_METAR_ENDPOINT = "https://api-redemet.decea.mil.br/mensagens/metar"
METSERVICE_METAR_BASE = "https://api.metservice.com/aviation/metar"
ECCC_DATAMART_BASE = "https://dd.weather.gc.ca/today/bulletins/alphanumeric"
FMI_METAR_ENDPOINT = "https://ilmailusaa.fi/backend.php"
IMGW_METAR_ENDPOINT = "https://aviation-api.imgw.pl/data/last"
NOAA_STATION_METAR_BASE = "https://tgftp.nws.noaa.gov/data/observations/metar/stations"
SCHEMA_VERSION = "reported_trajectory_country_sources_v1"

_CONFIG_CACHE: tuple[float, dict[str, Any]] = (0.0, {})
_FETCH_CACHE: dict[tuple[str, tuple[str, ...]], tuple[float, dict[str, Any]]] = {}
_NOAA_STATION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RAW_METAR_RE_TEMPLATE = (
    r"\b(?:METAR|SPECI)(?:\s+(?:COR|AMD))?\s+{station}\s+\d{{6}}Z\b.*?="
)
_REPORT_TIME_RE = re.compile(
    r"\b(?:METAR|SPECI)(?:\s+(?:COR|AMD))?\s+[A-Z0-9]{4}\s+(\d{2})(\d{2})(\d{2})Z\b",
    re.I,
)


def _release_clock(now: datetime | None = None) -> tuple[str, float, str]:
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    halfhour = 0 if instant.minute < 30 else 30
    phase = (instant.minute - halfhour) * 60 + instant.second + instant.microsecond / 1_000_000
    return f"{instant:%Y%m%d%H}{halfhour:02d}", phase, f"{instant:%Y%m%d%H%M}"


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "pre", "li", "div", "br", "h1", "h2", "h3", "h4", "h5"}:
            self.parts.append("\n")

    def text(self) -> str:
        return html.unescape(" ".join(self.parts))


def _canonical_sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _report_valid_at(report: Mapping[str, Any], *, reference: datetime) -> datetime | None:
    """Return the observation clock used only to arbitrate direct vs relay freshness."""

    source_time = str(report.get("source_report_time") or "").strip()
    if source_time:
        try:
            parsed = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    match = _REPORT_TIME_RE.search(str(report.get("raw_text") or ""))
    if not match:
        return None
    return infer_ddhhmm_time(
        day=int(match.group(1)),
        hour=int(match.group(2)),
        minute=int(match.group(3)),
        reference=reference,
    )


def load_official_source_config() -> dict[str, Any]:
    global _CONFIG_CACHE
    mtime = OFFICIAL_SOURCE_CONFIG.stat().st_mtime
    if _CONFIG_CACHE[1] and _CONFIG_CACHE[0] == mtime:
        return copy.deepcopy(_CONFIG_CACHE[1])
    value = json.loads(OFFICIAL_SOURCE_CONFIG.read_text(encoding="utf-8"))
    if value.get("schema") != "reported_trajectory_official_sources_v1":
        raise ValueError("official_source_config_schema_invalid")
    if not isinstance(value.get("providers"), Mapping) or not isinstance(value.get("station_routes"), Mapping):
        raise ValueError("official_source_config_shape_invalid")
    _CONFIG_CACHE = (mtime, dict(value))
    return copy.deepcopy(value)


def official_source_config_sha256() -> str:
    return _canonical_sha(load_official_source_config())


def station_source_route(station_id: str) -> dict[str, Any]:
    station = str(station_id or "").strip().upper()
    config = load_official_source_config()
    provider_id = str(config["station_routes"].get(station) or "")
    provider = dict(config["providers"].get(provider_id) or {})
    return {
        "station_id": station,
        "provider_id": provider_id,
        "authority": str(provider.get("authority") or ""),
        "country_code": str(provider.get("country_code") or ""),
        "adapter": str(provider.get("adapter") or ""),
        "access": str(provider.get("access") or ""),
        "official_url": str(provider.get("official_url") or ""),
        "credential_env": str(provider.get("credential_env") or ""),
        "registry_sha256": _canonical_sha(config),
    }


def _direct_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_text(url: str, *, timeout: float, headers: Mapping[str, str] | None = None) -> tuple[str, int, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "weather-country-raw-metar/1.0",
            "Accept": "text/html,application/json,text/plain,*/*",
            **dict(headers or {}),
        },
    )
    with _direct_opener().open(request, timeout=max(1.0, float(timeout))) as response:
        started_at = utc_now()
        body = response.read().decode("utf-8", "replace")
        return body, int(response.status), started_at


def _extract_raw_metars(text: str, station: str, *, allow_missing_kind: bool = False) -> list[dict[str, str]]:
    station = station.upper()
    normalized = " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())
    pattern = re.compile(_RAW_METAR_RE_TEMPLATE.format(station=re.escape(station)), re.I)
    reports = [
        {
            "station_id": station,
            "raw_text": " ".join(match.group(0).split()),
            "source_report_time": "",
        }
        for match in pattern.finditer(normalized)
    ]
    if reports or not allow_missing_kind:
        return reports
    bare = re.compile(rf"\b{re.escape(station)}\s+\d{{6}}Z\b.*?=", re.I)
    return [
        {
            "station_id": station,
            "raw_text": "METAR " + " ".join(match.group(0).split()),
            "source_report_time": "",
            "origin_raw_text": " ".join(match.group(0).split()),
            "normalization": "restore_metar_kind_literal",
        }
        for match in bare.finditer(normalized)
    ]


def _extract_pagasa_metars(body: str, station: str) -> list[dict[str, str]]:
    """Extract one PAGASA report per HTML block without crossing into the next TAC.

    PAGASA omits the leading ``METAR`` literal and does not append ``=`` to
    every displayed line.  Parsing the whole visible page would therefore
    concatenate successive observations.  Block/line boundaries are the
    authority's message boundary and are preserved here.
    """

    station = station.upper()
    candidates: list[str] = []
    for fragment in re.findall(r"<(?:p|pre|div|td|li)\b[^>]*>(.*?)</(?:p|pre|div|td|li)>", body, re.I | re.S):
        parser = _VisibleText()
        parser.feed(fragment)
        candidates.extend(parser.text().splitlines())
    if not candidates:
        parser = _VisibleText()
        parser.feed(body)
        candidates.extend(parser.text().splitlines())
    pattern = re.compile(
        rf"^\s*{re.escape(station)}\s+\d{{6}}Z\b.*?(?=\s+(?:[A-Z]{{4}})\s+\d{{6}}Z\b|$)",
        re.I,
    )
    reports: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = " ".join(candidate.replace("\r", " ").replace("\n", " ").split()).rstrip("=")
        match = pattern.match(normalized)
        if not match:
            continue
        origin_raw = " ".join(match.group(0).split()).rstrip("=")
        raw_text = f"METAR {origin_raw}="
        if raw_text in seen:
            continue
        seen.add(raw_text)
        reports.append(
            {
                "station_id": station,
                "raw_text": raw_text,
                "source_report_time": "",
                "origin_raw_text": origin_raw,
                "normalization": "restore_pagasa_metar_kind_and_terminator",
            }
        )
    # Keep the same bounded reconstruction horizon as the AWC relay.  The
    # PAGASA page contains multiple days and production must not replay the
    # entire page on every poll.
    return reports[:3]


def _origin_report(report: Mapping[str, Any], *, provider_id: str, provider: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(report)
    value.update(
        {
            "origin_source_id": provider_id,
            "origin_country_code": str(provider.get("country_code") or ""),
            "origin_authority": str(provider.get("authority") or ""),
            "origin_url": str(provider.get("official_url") or ""),
            "origin_direct_country_official": True,
            "origin_fallback_used": False,
            "origin_fallback_reason": "",
        }
    )
    return value


def _fetch_official_html(provider_id: str, provider: Mapping[str, Any], stations: Sequence[str], *, timeout: float) -> dict[str, Any]:
    body, status, started_at = _http_text(str(provider["official_url"]), timeout=timeout)
    reports: list[dict[str, Any]] = []
    pagasa = provider_id == "ph_pagasa_aviation"
    parser = _VisibleText()
    parser.feed(body)
    visible = parser.text()
    for station in stations:
        rows = _extract_pagasa_metars(body, station) if pagasa else _extract_raw_metars(visible, station)
        for row in rows:
            reports.append(_origin_report(row, provider_id=provider_id, provider=provider))
    return {
        "status": status,
        "response_started_at": started_at,
        "reports": reports,
        "transport": f"country_official_html:{provider_id}",
    }


def _fetch_fmi(provider_id: str, provider: Mapping[str, Any], stations: Sequence[str], *, timeout: float) -> dict[str, Any]:
    query = json.dumps(
        {
            "mode": "metar",
            "radius": "100",
            "points": [{"_locationName": station} for station in stations],
        },
        separators=(",", ":"),
    )
    url = FMI_METAR_ENDPOINT + "?" + urllib.parse.quote(query, safe="")
    body, status, started_at = _http_text(
        url,
        timeout=timeout,
        headers={"Accept": "application/json", "Referer": str(provider.get("official_url") or "")},
    )
    payload = json.loads(body)
    reports: list[dict[str, Any]] = []
    for station in stations:
        item = payload.get(f"METAR{station}") if isinstance(payload, Mapping) else None
        raw = str(item.get("p1") or "") if isinstance(item, Mapping) else ""
        for row in _extract_raw_metars(raw, station):
            enriched = _origin_report(row, provider_id=provider_id, provider=provider)
            enriched["origin_url"] = url
            reports.append(enriched)
    return {
        "status": status,
        "response_started_at": started_at,
        "reports": reports,
        "transport": "country_official_fmi_backend",
    }


def _fetch_imgw(provider_id: str, provider: Mapping[str, Any], stations: Sequence[str], *, timeout: float) -> dict[str, Any]:
    url = IMGW_METAR_ENDPOINT + "?" + urllib.parse.urlencode(
        {
            "params": "metar",
            "airports": ",".join(stations),
            "format": "json",
            "count": 1,
        }
    )
    body, status, started_at = _http_text(
        url,
        timeout=timeout,
        headers={
            "Accept": "application/json",
            "Origin": "https://awiacja.imgw.pl",
            "Referer": str(provider.get("official_url") or ""),
        },
    )
    payload = json.loads(body)
    reports: list[dict[str, Any]] = []
    for station in stations:
        station_payload = payload.get(station) if isinstance(payload, Mapping) else None
        for raw in _walk_values(station_payload, "message"):
            for row in _extract_raw_metars(str(raw), station):
                enriched = _origin_report(row, provider_id=provider_id, provider=provider)
                enriched["origin_url"] = url
                reports.append(enriched)
    return {
        "status": status,
        "response_started_at": started_at,
        "reports": reports,
        "transport": "country_official_imgw_aviation_api",
    }


def _directory_hrefs(text: str) -> list[str]:
    return [html.unescape(value) for value in re.findall(r'href="([^"?#]+)"', text, re.I)]


def _fetch_eccc_station(station: str, *, timeout: float) -> tuple[list[dict[str, str]], list[str]]:
    now = datetime.now(timezone.utc)
    reports: list[dict[str, str]] = []
    used_urls: list[str] = []
    for bulletin_kind in ("SA", "SP"):
        found_kind = False
        for offset in range(0, 3):
            instant = now - timedelta(hours=offset)
            directory = f"{ECCC_DATAMART_BASE}/{instant:%Y%m%d}/{bulletin_kind}/{station}/{instant:%H}/"
            try:
                listing, _status, _started_at = _http_text(directory, timeout=timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
            filenames = sorted(
                value for value in _directory_hrefs(listing)
                if not value.endswith("/") and value not in {"../", ".."}
            )
            if not filenames:
                continue
            report_url = urllib.parse.urljoin(directory, filenames[-1])
            body, _status, _started_at = _http_text(report_url, timeout=timeout)
            rows = _extract_raw_metars(body, station)
            if rows:
                reports.extend(rows)
                used_urls.append(report_url)
                found_kind = True
                break
        if bulletin_kind == "SA" and not found_kind:
            raise RuntimeError(f"eccc_sa_report_missing:{station}")
    return reports, used_urls


def _fetch_eccc(provider_id: str, provider: Mapping[str, Any], stations: Sequence[str], *, timeout: float) -> dict[str, Any]:
    response_started_at = utc_now()
    reports: list[dict[str, Any]] = []
    used_urls: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(stations)))) as executor:
        futures = {executor.submit(_fetch_eccc_station, station, timeout=timeout): station for station in stations}
        for future in as_completed(futures):
            rows, urls = future.result()
            used_urls.extend(urls)
            for row in rows:
                enriched = _origin_report(row, provider_id=provider_id, provider=provider)
                enriched["origin_url"] = next((url for url in urls if f"/{row['station_id']}/" in url), provider["official_url"])
                reports.append(enriched)
    return {
        "status": 200,
        "response_started_at": response_started_at,
        "reports": reports,
        "transport": "country_official_eccc_raw_bulletin",
        "origin_urls": sorted(set(used_urls)),
    }


def _walk_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, Mapping):
        for name, item in value.items():
            if str(name) == key:
                found.append(item)
            found.extend(_walk_values(item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_values(item, key))
    return found


def _fetch_kma(provider_id: str, provider: Mapping[str, Any], stations: Sequence[str], *, timeout: float, key: str) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    response_started_at = utc_now()
    for station in stations:
        url = KMA_METAR_ENDPOINT + "?" + urllib.parse.urlencode(
            {"pageNo": 1, "numOfRows": 10, "dataType": "JSON", "icao": station, "authKey": key}
        )
        body, _status, started_at = _http_text(url, timeout=timeout, headers={"Accept": "application/json"})
        response_started_at = min(response_started_at, started_at)
        payload = json.loads(body)
        for raw in _walk_values(payload, "msgText"):
            for row in _extract_raw_metars(str(raw), station):
                reports.append(_origin_report(row, provider_id=provider_id, provider=provider))
    return {"status": 200, "response_started_at": response_started_at, "reports": reports, "transport": "country_official_kma_apihub"}


def _fetch_redemet(provider_id: str, provider: Mapping[str, Any], stations: Sequence[str], *, timeout: float, key: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    url = REDEMET_METAR_ENDPOINT + "/" + ",".join(stations) + "?" + urllib.parse.urlencode(
        {"api_key": key, "data_ini": (now - timedelta(hours=2)).strftime("%Y%m%d%H"), "data_fim": now.strftime("%Y%m%d%H")}
    )
    body, status, started_at = _http_text(url, timeout=timeout, headers={"Accept": "application/json"})
    payload = json.loads(body)
    reports: list[dict[str, Any]] = []
    for item in _walk_values(payload, "mens"):
        raw = str(item or "")
        for station in stations:
            for row in _extract_raw_metars(raw, station):
                reports.append(_origin_report(row, provider_id=provider_id, provider=provider))
    return {"status": status, "response_started_at": started_at, "reports": reports, "transport": "country_official_redemet"}


def _fetch_metservice(provider_id: str, provider: Mapping[str, Any], stations: Sequence[str], *, timeout: float, key: str) -> dict[str, Any]:
    base = os.environ.get("METSERVICE_METAR_BASE_URL", METSERVICE_METAR_BASE).rstrip("/")
    response_started_at = utc_now()
    reports: list[dict[str, Any]] = []
    for station in stations:
        body, _status, started_at = _http_text(
            f"{base}/{station}/latest", timeout=timeout, headers={"Accept": "application/json", "apikey": key}
        )
        response_started_at = min(response_started_at, started_at)
        payload = json.loads(body)
        for raw in _walk_values(payload, "met:data"):
            for row in _extract_raw_metars(str(raw), station):
                reports.append(_origin_report(row, provider_id=provider_id, provider=provider))
    return {"status": 200, "response_started_at": response_started_at, "reports": reports, "transport": "country_official_metservice"}


def _fetch_awc_origin(provider_id: str, provider: Mapping[str, Any], stations: Sequence[str], *, timeout: float) -> dict[str, Any]:
    # All current stations fit safely in one ids query.  This avoids both
    # multi-batch tail latency and multiple same-endpoint requests per minute.
    if provider.get("_request_hours") is None:
        raise ValueError("awc_request_hours_missing")
    request_hours = max(2.0, min(30.0, float(provider["_request_hours"])))
    value = fetch_awc_temperature_reports(
        stations,
        hours=request_hours,
        timeout=timeout,
        batch_size=100,
        max_workers=1,
    )
    value["requested_hours"] = request_hours
    value["reports"] = [
        _origin_report(row, provider_id=provider_id, provider=provider)
        for row in value.get("reports") or []
    ]
    return value


def _fetch_noaa_station_metars(
    provider_id: str, provider: Mapping[str, Any], stations: Sequence[str], *, timeout: float,
) -> dict[str, Any]:
    """Fetch NOAA/NWS original per-station METAR files concurrently.

    Each official file contains its observation timestamp followed by the
    latest raw TAC. The station files commonly omit the leading ``METAR``
    literal, so restore only that framing token; all weather content remains
    byte-for-byte authoritative text.
    """

    def fetch_one(station: str) -> tuple[str, dict[str, Any] | None, str, str]:
        url = f"{NOAA_STATION_METAR_BASE}/{station}.TXT"
        try:
            body, _status, response_started_at = _http_text(
                url, timeout=min(max(1.0, float(timeout)), 4.0),
                headers={"Accept": "text/plain", "Cache-Control": "no-cache"},
            )
            lines = [line.strip() for line in body.replace("\r", "\n").splitlines() if line.strip()]
            if len(lines) < 2:
                return station, None, response_started_at, "station_file_shape_invalid"
            raw = " ".join(lines[-1].split()).rstrip("=")
            restored_kind = not raw.startswith(("METAR ", "SPECI "))
            if restored_kind:
                raw = "METAR " + raw
            if not re.search(rf"\b{re.escape(station)}\s+\d{{6}}Z\b", raw):
                return station, None, response_started_at, "station_file_identity_invalid"
            origin_file_header_time = ""
            try:
                origin_file_header_time = datetime.strptime(lines[0], "%Y/%m/%d %H:%M").replace(
                    tzinfo=timezone.utc
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
            except ValueError:
                pass
            report = _origin_report(
                {
                    "station_id": station,
                    "raw_text": raw,
                    # Canonical valid_at always comes from the raw TAC header.
                    "source_report_time": "",
                    # NOAA's first line repeats the observation timestamp; it
                    # is not an HTTP publication timestamp.
                    "origin_file_header_time": origin_file_header_time,
                    "origin_raw_text": lines[-1],
                    "normalization": "restore_metar_kind_literal" if restored_kind else "",
                },
                provider_id=provider_id,
                provider=provider,
            )
            report["origin_url"] = url
            return station, report, response_started_at, ""
        except Exception as exc:
            return station, None, "", f"{type(exc).__name__}"

    reports: list[dict[str, Any]] = []
    response_started_values: list[str] = []
    errors: list[str] = []
    station_set = sorted({str(value).strip().upper() for value in stations if str(value).strip()})
    now = time.monotonic()
    fetch_stations: list[str] = []
    for station in station_set:
        cached = _NOAA_STATION_CACHE.get(station)
        if cached and now - cached[0] < 15.0:
            reports.append(copy.deepcopy(cached[1]))
        else:
            fetch_stations.append(station)
    # Eight concurrent files proved stable against the official host; eleven
    # simultaneous cold connections produced partial timeouts. Failed
    # stations are deliberately not cached and retry on the next router pass.
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(fetch_stations)))) as executor:
        futures = {executor.submit(fetch_one, station): station for station in fetch_stations}
        for future in as_completed(futures):
            station, report, response_started_at, error = future.result()
            if response_started_at:
                response_started_values.append(response_started_at)
            if report is not None:
                reports.append(report)
                _NOAA_STATION_CACHE[station] = (time.monotonic(), copy.deepcopy(report))
            elif error:
                errors.append(f"{station}:{error}")
    return {
        "status": 200 if not errors else 206,
        "response_started_at": min(response_started_values, default=""),
        "reports": reports,
        "station_errors": errors,
        "transport": "country_official_noaa_nws_station_metar",
        "cache_hit": bool(station_set) and not fetch_stations,
        "station_cache_hits": len(station_set) - len(fetch_stations),
    }


def _provider_fetch(provider_id: str, provider: Mapping[str, Any], stations: Sequence[str], *, timeout: float) -> dict[str, Any]:
    normalized = tuple(sorted({str(value).upper() for value in stations}))
    adapter = str(provider.get("adapter") or "")
    release_slot, release_phase, minute_slot = _release_clock()
    second_in_minute = release_phase % 60.0
    # National authorities are polled at most once a second during the first
    # 90 seconds after :00/:30.  A cache from the preceding half-hour is never
    # allowed to cross the release boundary.
    ttl = 1.0 if release_phase <= 90.0 else 5.0
    if adapter == "official_html_metar" and release_phase > 90.0:
        ttl = 10.0
    cache_key = (provider_id, normalized)
    cached = _FETCH_CACHE.get(cache_key)
    cached_value = cached[1] if cached else {}
    if adapter == "awc":
        if provider.get("_request_hours") is None:
            raise ValueError("awc_request_hours_missing")
        requested_hours = max(2.0, min(30.0, float(provider["_request_hours"])))
        cached_hours = float(cached_value.get("requested_hours") or 0.0)
        # NOAA/AWC documents a one-minute cache/update cadence.  Align the
        # single relay request to +30 seconds of each UTC minute so a request
        # just before :00/:30 cannot hide the newly issued report for 60 sec.
        if (
            cached
            and cached_hours >= requested_hours
            and str(cached_value.get("awc_request_minute_slot") or "") == minute_slot
        ):
            value = copy.deepcopy(cached_value)
            value["cache_hit"] = True
            value["transport"] = str(value.get("transport") or "") + ":aligned_cache"
            return value
        if second_in_minute < 20.0:
            if cached and cached_hours >= requested_hours:
                value = copy.deepcopy(cached_value)
                value["cache_hit"] = True
                value["transport"] = str(value.get("transport") or "") + ":pre_release_aligned_cache"
                return value
            return {
                "status": 204,
                "response_started_at": utc_now(),
                "reports": [],
                "station_errors": [],
                "transport": "official_awc_release_gate_wait_until_plus_30s",
                "source_deferred": True,
                "awc_request_minute_slot": minute_slot,
            }
        if second_in_minute < 30.0:
            # Reserve one worker from :MM:20 and fire the sole relay request at
            # :MM:30 even when another national endpoint is slow.  Other
            # provider futures continue and can emit independently meanwhile.
            time.sleep(30.0 - second_in_minute)
    elif adapter != "noaa_station_metar" and cached and time.monotonic() - cached[0] < ttl and str(cached_value.get("release_slot") or "") == release_slot:
        value = copy.deepcopy(cached[1])
        value["cache_hit"] = True
        value["transport"] = str(value.get("transport") or "") + ":cache"
        return value
    credential_env = str(provider.get("credential_env") or "")
    credential = os.environ.get(credential_env, "").strip() if credential_env else ""
    if credential_env and not credential:
        raise RuntimeError(f"country_source_credential_missing:{credential_env}")
    if adapter == "official_html_metar":
        value = _fetch_official_html(provider_id, provider, normalized, timeout=timeout)
    elif adapter == "fmi_backend":
        value = _fetch_fmi(provider_id, provider, normalized, timeout=timeout)
    elif adapter == "imgw_aviation_api":
        value = _fetch_imgw(provider_id, provider, normalized, timeout=timeout)
    elif adapter == "eccc_datamart":
        value = _fetch_eccc(provider_id, provider, normalized, timeout=timeout)
    elif adapter == "kma_apihub":
        value = _fetch_kma(provider_id, provider, normalized, timeout=timeout, key=credential)
    elif adapter == "redemet":
        value = _fetch_redemet(provider_id, provider, normalized, timeout=timeout, key=credential)
    elif adapter == "metservice":
        value = _fetch_metservice(provider_id, provider, normalized, timeout=timeout, key=credential)
    elif adapter == "noaa_station_metar":
        value = _fetch_noaa_station_metars(provider_id, provider, normalized, timeout=timeout)
    elif adapter == "awc":
        value = _fetch_awc_origin(provider_id, provider, normalized, timeout=timeout)
        value.setdefault(
            "requested_hours",
            max(2.0, min(30.0, float(provider["_request_hours"]))),
        )
    else:
        raise RuntimeError(f"country_source_adapter_not_direct:{adapter or 'missing'}")
    value["release_slot"] = release_slot
    if adapter == "awc":
        value["awc_request_minute_slot"] = minute_slot
        value["awc_aligned_at_plus_30s"] = True
    if adapter != "noaa_station_metar":
        _FETCH_CACHE[cache_key] = (time.monotonic(), copy.deepcopy(value))
    return value


def fetch_country_routed_temperature_reports(
    station_ids: Sequence[str], *, timeout: float = 8.0,
    awc_hours: float,
    on_provider_batch: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Fetch station observations and emit each provider as soon as it completes.

    A single aligned AWC request is launched as a shadow for the whole station
    set.  Its rows are consumed for US/AWC-authoritative stations, when a
    national provider is unavailable, or when the relay has a strictly newer
    observation.  Equal/newer national observations retain source priority.
    """

    router_started = time.monotonic()
    stations = sorted({str(value).strip().upper() for value in station_ids if str(value).strip()})
    config = load_official_source_config()
    providers = config["providers"]
    routes = config["station_routes"]
    registry_sha = _canonical_sha(config)

    grouped: dict[str, list[str]] = {}
    fallback_reasons: dict[str, str] = {}
    awc_authoritative: set[str] = set()
    for station in stations:
        provider_id = str(routes.get(station) or "")
        provider = providers.get(provider_id) if provider_id else None
        if not isinstance(provider, Mapping):
            fallback_reasons[station] = "country_source_route_unmapped"
            continue
        adapter = str(provider.get("adapter") or "")
        if adapter in {"awc_fallback_only", "amsc"}:
            fallback_reasons[station] = f"preferred_provider_{provider.get('access') or 'not_machine_accessible'}"
            continue
        if adapter == "awc":
            awc_authoritative.add(station)
            continue
        credential_env = str(provider.get("credential_env") or "")
        if credential_env and not os.environ.get(credential_env, "").strip():
            fallback_reasons[station] = f"preferred_provider_credential_missing:{credential_env}"
            continue
        grouped.setdefault(provider_id, []).append(station)

    provider_states: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    response_started_values: list[str] = []
    station_errors: list[str] = []
    emitted_relay_stations: set[str] = set()
    awc_rows_by_station: dict[str, list[dict[str, Any]]] = {}
    awc_result: dict[str, Any] = {}
    direct_latest_by_station: dict[str, datetime] = {}
    arbitration_reference = datetime.now(timezone.utc)

    def latest_valid_at(rows: Sequence[Mapping[str, Any]]) -> datetime | None:
        values = [
            value for value in (
                _report_valid_at(row, reference=arbitration_reference) for row in rows
            ) if value is not None
        ]
        return max(values, default=None)

    def relay_is_newer(station: str) -> bool:
        direct_latest = direct_latest_by_station.get(station)
        relay_latest = latest_valid_at(awc_rows_by_station.get(station, []))
        return bool(direct_latest is not None and relay_latest is not None and relay_latest > direct_latest)

    def emit(rows: Sequence[Mapping[str, Any]], state: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        selected = [dict(row) for row in rows]
        if not selected or on_provider_batch is None:
            return
        batch_stations = sorted({str(row.get("station_id") or "").upper() for row in selected})
        on_provider_batch(
            {
                "schema": SCHEMA_VERSION,
                "source_lane": "awc",
                "station_ids": batch_stations,
                "response_started_at": str(result.get("response_started_at") or ""),
                "status": int(result.get("status") or 200),
                "reports": selected,
                "station_errors": [],
                "taf_requested": False,
                "transport": "country_official_router_with_explicit_awc_fallback",
                "provider_transport": str(result.get("transport") or ""),
                "official_source_registry_sha256": registry_sha,
                "provider_states": [dict(state)],
                "fallback_reasons": {
                    station: fallback_reasons[station]
                    for station in batch_stations if station in fallback_reasons
                },
                "provider_completed_after_router_start_ms": round(
                    (time.monotonic() - router_started) * 1000.0, 3
                ),
            }
        )

    def decorate_relay(station_set: set[str], *, state_name: str) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for station in sorted(station_set - emitted_relay_stations):
            preferred_id = str(routes.get(station) or "")
            preferred = providers.get(preferred_id) or {}
            for raw in awc_rows_by_station.get(station, []):
                row = dict(raw)
                if station in awc_authoritative:
                    row.update(
                        {
                            "origin_source_id": preferred_id,
                            "origin_country_code": str(preferred.get("country_code") or ""),
                            "origin_authority": str(preferred.get("authority") or ""),
                            "origin_url": str(preferred.get("official_url") or ""),
                            "origin_direct_country_official": True,
                            "origin_fallback_used": False,
                            "origin_fallback_reason": "",
                        }
                    )
                else:
                    row.update(
                        {
                            "origin_country_code": str(preferred.get("country_code") or ""),
                            "origin_direct_country_official": False,
                            "origin_fallback_used": True,
                            "origin_fallback_reason": fallback_reasons.get(station, "country_source_fallback"),
                            "preferred_origin_source_id": preferred_id,
                            "preferred_origin_authority": str(preferred.get("authority") or ""),
                        }
                    )
                row["origin_transport"] = str(awc_result.get("transport") or "")
                row["official_source_registry_sha256"] = registry_sha
                selected.append(row)
            if awc_rows_by_station.get(station):
                emitted_relay_stations.add(station)
        if selected:
            state = {
                "provider_id": str(config["fallback_provider_id"]),
                "stations": sorted(station_set),
                "state": state_name,
                "report_count": len(selected),
                "transport": awc_result.get("transport"),
                "cache_hit": bool(awc_result.get("cache_hit")),
                "aligned_at_plus_30s": bool(awc_result.get("awc_aligned_at_plus_30s")),
            }
            provider_states.append(state)
            reports.extend(selected)
            emit(selected, state, awc_result)
        return selected

    fallback_id = str(config["fallback_provider_id"])
    bounded_awc_hours = max(2.0, min(30.0, float(awc_hours)))
    fallback_provider = dict(providers[fallback_id])
    fallback_provider["_request_hours"] = bounded_awc_hours
    futures: dict[Any, tuple[str, str, list[str]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(grouped) + 1))) as executor:
        # One stable AWC request covers direct US stations plus failover shadow
        # for every other station; shadow rows are never consumed when the
        # national source succeeds.
        if stations:
            future = executor.submit(
                _provider_fetch,
                fallback_id,
                fallback_provider,
                stations,
                timeout=timeout,
            )
            futures[future] = ("relay", fallback_id, stations)
        for provider_id, group in grouped.items():
            future = executor.submit(_provider_fetch, provider_id, providers[provider_id], group, timeout=timeout)
            futures[future] = ("direct", provider_id, group)
        for future in as_completed(futures):
            kind, provider_id, group = futures[future]
            try:
                result = dict(future.result())
                if result.get("response_started_at"):
                    response_started_values.append(str(result["response_started_at"]))
                if kind == "relay":
                    awc_result = result
                    for raw in result.get("reports") or []:
                        station = str(raw.get("station_id") or "").upper()
                        awc_rows_by_station.setdefault(station, []).append(dict(raw))
                    decorate_relay(
                        awc_authoritative | set(fallback_reasons),
                        state_name="awc_authoritative_and_known_fallback",
                    )
                    newer_shadow = {station for station in direct_latest_by_station if relay_is_newer(station)}
                    for station in newer_shadow:
                        fallback_reasons[station] = "preferred_provider_stale_vs_awc_relay"
                    decorate_relay(newer_shadow, state_name="awc_shadow_newer_than_country")
                    continue
                found = {str(row.get("station_id") or "").upper() for row in result.get("reports") or []}
                direct_rows: list[dict[str, Any]] = []
                for raw in result.get("reports") or []:
                    row = dict(raw)
                    row["origin_transport"] = str(result.get("transport") or "")
                    row["official_source_registry_sha256"] = registry_sha
                    direct_rows.append(row)
                state = {
                    "provider_id": provider_id,
                    "stations": sorted(group),
                    "state": "direct_country_official",
                    "report_count": len(direct_rows),
                    "transport": result.get("transport"),
                    "cache_hit": bool(result.get("cache_hit")),
                    "completed_after_router_start_ms": round((time.monotonic() - router_started) * 1000.0, 3),
                }
                provider_states.append(state)
                reports.extend(direct_rows)
                emit(direct_rows, state, result)
                for station in found:
                    direct_latest = latest_valid_at(
                        [row for row in direct_rows if str(row.get("station_id") or "").upper() == station]
                    )
                    if direct_latest is not None:
                        direct_latest_by_station[station] = direct_latest
                for station in group:
                    if station not in found:
                        fallback_reasons[station] = "preferred_provider_no_current_raw_metar"
                decorate_relay(set(group) - found, state_name="awc_shadow_after_country_no_report")
                newer_shadow = {station for station in found if relay_is_newer(station)}
                for station in newer_shadow:
                    fallback_reasons[station] = "preferred_provider_stale_vs_awc_relay"
                decorate_relay(newer_shadow, state_name="awc_shadow_newer_than_country")
            except Exception as exc:
                reason = f"preferred_provider_fetch_failed:{type(exc).__name__}"
                fallback_reasons.update({station: reason for station in group})
                provider_states.append(
                    {"provider_id": provider_id, "stations": sorted(group), "state": "fallback_required", "reason": reason}
                )
                decorate_relay(set(group), state_name="awc_shadow_after_country_failure")

    # If AWC completed before a later national failure, its shadow becomes
    # usable here without a second relay request.
    decorate_relay(set(fallback_reasons) | awc_authoritative, state_name="awc_shadow_finalized")
    source_deferred = bool(awc_result.get("source_deferred"))
    unresolved = sorted(set(stations) - {str(row.get("station_id") or "").upper() for row in reports})
    if unresolved and not source_deferred:
        station_errors.extend(f"country_source_no_report:{station}" for station in unresolved)

    # Same station/report can occur in SA and SP collections.  Preserve one
    # canonical copy so no source path can create duplicate trading evidence.
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for report in reports:
        key = (str(report.get("station_id") or "").upper(), str(report.get("raw_text") or ""))
        deduped[key] = report
    return {
        "schema": SCHEMA_VERSION,
        "source_lane": "awc",
        "station_ids": stations,
        "response_started_at": min(response_started_values, default=""),
        "status": 200 if not station_errors else 206,
        "reports": list(deduped.values()),
        "station_errors": station_errors,
        "taf_requested": False,
        "transport": "country_official_router_with_explicit_awc_fallback",
        "official_source_registry_sha256": _canonical_sha(config),
        "provider_states": sorted(provider_states, key=lambda row: str(row.get("provider_id") or "")),
        "fallback_reasons": fallback_reasons,
        "source_deferred": source_deferred,
        "deferred_stations": unresolved if source_deferred else [],
        "router_duration_ms": round((time.monotonic() - router_started) * 1000.0, 3),
    }


__all__ = [
    "OFFICIAL_SOURCE_CONFIG", "SCHEMA_VERSION", "fetch_country_routed_temperature_reports",
    "load_official_source_config", "official_source_config_sha256", "station_source_route",
]
