"""Official Polymarket-rule registry for the AWC/P1 YES-basket lane."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping, Sequence

from weather_basket_yes_runtime import normalize_market_buckets
from reported_trajectory_country_sources import station_source_route

try:
    import requests
except ImportError:  # pragma: no cover - stdlib fallback for minimal service images
    requests = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "trading" / "awc_truth_ingress_v1.json"
GAMMA_ENDPOINT = "https://gamma-api.polymarket.com/events"
SCHEMA_VERSION = "reported_trajectory_market_registry_v1"
MAX_GAMMA_PAGES = 4
SLUG_RE = re.compile(r"^highest-temperature-in-(?P<city>.+)-on-[a-z]+-\d{1,2}-\d{4}$")
WU_STATION_RE = re.compile(r"https?://(?:www\.)?wunderground\.com/history/daily/[^\s.]+/(?P<station>[A-Z0-9]{4})(?:[.\s]|$)", re.I)
NOAA_STATION_RE = re.compile(r"https?://(?:www\.)?weather\.gov/wrh/timeseries\?site=(?P<station>[A-Za-z0-9]{4})(?=[&.\s]|$)", re.I)
# Polymarket moved these markets off Weather Underground onto NOAA one event date at a
# time; both wordings are live at once, so both have to resolve to the same ICAO.
STATION_RES = (WU_STATION_RE, NOAA_STATION_RE)
URL_RE = re.compile(r"https?://[^\s]+")
STATION_ID_RE = re.compile(r"^[A-Z0-9]{4}$")


class GammaFetchError(RuntimeError):
    """Gamma was unavailable through every configured transport candidate."""

    def __init__(self, message: str, *, errors: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.errors = tuple(str(item) for item in errors)


def _dotenv_network_values() -> dict[str, str]:
    """Read only non-secret network settings for direct Python invocations.

    launchd entrypoints already load ``.env``.  Heartbeats and diagnostics are
    also invoked directly as ``python3 script.py`` though, and macOS can leave
    a stale 7891 proxy in the inherited environment.  Keep this parser small,
    side-effect free, and limited to transport/config keys; never load
    credentials here.
    """

    path = Path(os.environ.get("PM_SHARED_POLY_ENV_PATH") or ROOT / ".env")
    if not path.is_file():
        return {}
    allowed = {
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
        "GLOBAL_AGENT_HTTP_PROXY", "PM_PROXY_PORTS", "PM_PROXY_LOCAL_FALLBACK_PORTS",
        "PM_GAMMA_PROXY", "PM_GAMMA_FORCE_PROXY", "PM_CLOB_FORCE_PROXY", "PM_CLOB_FORCE_DIRECT",
        "PM_GAMMA_RETRIES", "PM_GAMMA_TIMEOUT_SEC", "PM_GAMMA_BACKOFF_SEC",
    }
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("export "):
            line = line[7:].lstrip() if line.startswith("export ") else line
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in allowed:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _env_or_dotenv(key: str, default: str = "") -> str:
    # The repository .env is the authoritative launch configuration.  It must
    # win over a stale launchctl/shell proxy inherited by direct invocations.
    return _dotenv_network_values().get(key, os.environ.get(key, default))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _gamma_proxy_candidates() -> list[tuple[str, str | None]]:
    """Return deterministic HTTP/direct candidates without implicit urllib env use."""

    dotenv = _dotenv_network_values()
    raw_values: list[str] = []
    for key in ("PM_GAMMA_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        # A launch wrapper may repair a stale .env localhost port before the
        # direct HTTP client starts. Prefer that live environment choice.
        value = str(os.environ.get(key) or dotenv.get(key) or "").strip()
        if value:
            raw_values.append(value)

    candidates: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for raw in raw_values:
        if "://" not in raw:
            raw = "http://" + raw
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            continue
        normalized = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append((f"proxy:{parsed.hostname}:{parsed.port}", normalized))

    port_values = [
        os.environ.get("PM_PROXY_PORTS", ""),
        os.environ.get("PM_PROXY_LOCAL_FALLBACK_PORTS", "1082"),
        dotenv.get("PM_PROXY_PORTS", ""),
        dotenv.get("PM_PROXY_LOCAL_FALLBACK_PORTS", "1082"),
    ]
    for raw_ports in port_values:
        for raw_port in re.split(r"[,\s]+", str(raw_ports or "")):
            if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
                continue
            normalized = f"http://127.0.0.1:{int(raw_port)}"
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append((f"proxy:127.0.0.1:{int(raw_port)}", normalized))

    # Gamma registry transport is independent from the CLOB transport.  A
    # stale/unavailable CLOB proxy must never make the official market
    # registry age out (which would then fail closed every AWC report via the
    # fresh-registry admission gate).  Only an explicit Gamma-specific flag
    # may force proxy-only operation.
    force_proxy = _truthy(dotenv.get("PM_GAMMA_FORCE_PROXY") or os.environ.get("PM_GAMMA_FORCE_PROXY"))
    if not force_proxy and not _truthy(dotenv.get("PM_CLOB_FORCE_DIRECT") or os.environ.get("PM_CLOB_FORCE_DIRECT")):
        candidates.append(("direct", None))
    elif _truthy(dotenv.get("PM_CLOB_FORCE_DIRECT") or os.environ.get("PM_CLOB_FORCE_DIRECT")):
        return [("direct", None)]
    return candidates or [("direct", None)]


def _gamma_json(url: str, *, timeout: float) -> Any:
    """Fetch JSON with explicit proxy selection and bounded retry/fallback."""

    try:
        retries = max(0, int(float(_env_or_dotenv("PM_GAMMA_RETRIES", "2"))))
    except (TypeError, ValueError):
        retries = 2
    try:
        backoff = max(0.0, float(_env_or_dotenv("PM_GAMMA_BACKOFF_SEC", "0.25")))
    except (TypeError, ValueError):
        backoff = 0.25
    errors: list[str] = []
    for attempt in range(retries + 1):
        for label, proxy in _gamma_proxy_candidates():
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "weather-reported-trajectory-no/1.0", "Accept": "application/json"},
            )
            try:
                if requests is not None:
                    # requests is already a project dependency and handles the
                    # local TLS MITM used by the workstation proxy correctly;
                    # trust_env=False prevents stale 7891 values from being
                    # reintroduced underneath our explicit candidate.
                    proxies = {"http": proxy, "https": proxy} if proxy else {}
                    with requests.Session() as session:
                        session.trust_env = False
                        response = session.get(
                            url,
                            headers=dict(request.headers),
                            proxies=proxies,
                            timeout=max(1.0, float(timeout)),
                        )
                    response.raise_for_status()
                    return response.json()
                if proxy is None:
                    # An empty ProxyHandler is required: urllib otherwise
                    # silently re-imports HTTP(S)_PROXY from the environment.
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    response = opener.open(request, timeout=timeout)
                else:
                    # Request.set_proxy expects host:port without a scheme.
                    parsed_proxy = urllib.parse.urlparse(proxy)
                    request.set_proxy(parsed_proxy.netloc, "http")
                    response = urllib.request.urlopen(request, timeout=timeout)
                with response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                if requests is not None and isinstance(exc, requests.HTTPError):
                    status = int(exc.response.status_code) if exc.response is not None else 0
                    errors.append(f"{label}:http_{status}")
                    continue
                if requests is not None and isinstance(exc, requests.RequestException):
                    errors.append(f"{label}:{type(exc).__name__}")
                    continue
                if isinstance(exc, HTTPError):
                    errors.append(f"{label}:http_{exc.code}")
                elif isinstance(exc, (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError)):
                    errors.append(f"{label}:{type(exc).__name__}")
                else:
                    errors.append(f"{label}:{type(exc).__name__}")
        if attempt < retries and backoff > 0:
            import time
            time.sleep(min(2.0, backoff * (2 ** attempt)))
    raise GammaFetchError("gamma_request_failed", errors=errors[-12:])


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _station_scope(policy: Mapping[str, Any], field: str) -> set[str] | None:
    """Return one explicit station scope or ``None`` for an unsafe config."""

    raw = policy.get(field)
    if not isinstance(raw, list):
        return None
    stations: set[str] = set()
    for value in raw:
        station = str(value or "").strip().upper()
        if not STATION_ID_RE.fullmatch(station) or station in stations:
            return None
        stations.add(station)
    return stations


def _provider_scope(policy: Mapping[str, Any], stations: set[str]) -> dict[str, str] | None:
    """Validate the explicit AMSC-provider binding for its station scope."""

    raw = policy.get("official_provider_ids")
    if not isinstance(raw, Mapping):
        return None
    providers: dict[str, str] = {}
    for raw_station, raw_provider in raw.items():
        station = str(raw_station or "").strip().upper()
        provider = str(raw_provider or "").strip()
        if not STATION_ID_RE.fullmatch(station) or not provider or station in providers:
            return None
        providers[station] = provider
    return providers if set(providers) == stations else None


def _city_scope(policy: Mapping[str, Any], field: str) -> set[str] | None:
    """Validate the city projection retained for the legacy polling worker."""

    raw = policy.get(field)
    if not isinstance(raw, list):
        return None
    cities: set[str] = set()
    for value in raw:
        city = str(value or "").strip().lower()
        if not city or city in cities:
            return None
        cities.add(city)
    return cities


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or value.get("schema") != "reported_trajectory_awc_ingress_config_v1":
        raise ValueError("reported_trajectory_awc_ingress_config_invalid")
    if value.get("production_source_allowlist") != ["awc"]:
        raise ValueError("reported_trajectory_awc_ingress_source_allowlist_invalid")
    source_policy = value.get("source_policy") if isinstance(value.get("source_policy"), Mapping) else {}
    asos = source_policy.get("asos_1min", {}) if isinstance(source_policy.get("asos_1min"), Mapping) else {}
    madis = source_policy.get("madis_hfmetar", {}) if isinstance(source_policy.get("madis_hfmetar"), Mapping) else {}
    awc = source_policy.get("awc", {}) if isinstance(source_policy.get("awc"), Mapping) else {}
    amsc = source_policy.get("amsc", {}) if isinstance(source_policy.get("amsc"), Mapping) else {}
    amsc_stations = _station_scope(amsc, "station_ids")
    awc_excluded_stations = _station_scope(awc, "exclude_station_ids")
    amsc_providers = _provider_scope(amsc, amsc_stations or set())
    amsc_cities = _city_scope(amsc, "city_slugs")
    awc_excluded_cities = _city_scope(awc, "exclude_city_slugs")
    guards = value.get("guards") if isinstance(value.get("guards"), Mapping) else {}
    if (
        amsc.get("enabled") is not False
        or amsc.get("production_ingest") is not False
        or amsc.get("role") != "retired_history_only"
        or amsc_stations != set()
        or amsc_providers is None
        or amsc_cities != set()
        or awc.get("enabled") is not True
        or awc.get("production_ingest") is not True
        or awc.get("global_primary") is not True
        or awc.get("replaces_amsc") is not True
        or awc_excluded_stations != set()
        or awc_excluded_cities != set()
        or asos.get("enabled") is not True
        or asos.get("production_ingest") is not True
        or int(asos.get("priority") or 0) != 220
        or asos.get("provider_id") != "IEM_ASOS1MIN"
        or asos.get("source_class") != "aggregator_republisher"
        or asos.get("upstream_authority") != "NOAA/NCEI ASOS"
        or asos.get("eligible_station_rule") != "ICAO_K_PREFIX_UNAMBIGUOUS"
        or asos.get("coverage_required") is not True
        or asos.get("fallback_lane") != "awc"
        or madis.get("enabled") is not True
        or madis.get("production_ingest") is not True
        or int(madis.get("priority") or 0) != 210
        or madis.get("provider_id") != "NOAA_MADIS_HFMETAR"
        or madis.get("source_class") != "official_republisher"
        or madis.get("measurement_semantics") != "ASOS_RUNNING_5_MINUTE_AVERAGE_DISSEMINATED_EVERY_5_MINUTES"
        or madis.get("fallback_lane") != "awc"
        or int(awc.get("priority") or 0) != 100
        or guards.get("canonical_observation_priority") != ["asos_1min", "madis_hfmetar", "awc"]
        or guards.get("cross_source_value_mixing_forbidden") is not True
        or guards.get("asos_must_not_be_labeled_metar") is not True
        or guards.get("source_lanes_disjoint") is not True
        or guards.get("awc_global_primary") is not True
        or guards.get("awc_replaces_amsc") is not True
        or guards.get("awc_fallback_for_amsc_forbidden") is not True
        or guards.get("amsc_fallback_for_awc_forbidden") is not True
    ):
        raise ValueError("reported_trajectory_awc_ingress_global_contract_invalid")
    poll = value.get("poll_policy") if isinstance(value.get("poll_policy"), Mapping) else {}
    try:
        asos_poll_sec = float(poll.get("asos_poll_interval_sec", 0))
        madis_poll_sec = float(poll.get("madis_hfmetar_poll_interval_sec", 0))
        gamma_pages = int(poll.get("market_registry_gamma_max_pages", 0))
        refresh_sec = float(poll.get("market_registry_refresh_sec", 0))
        retry_sec = float(poll.get("market_registry_retry_sec", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("reported_trajectory_awc_ingress_registry_poll_policy_invalid") from exc
    if (
        not 60.0 <= asos_poll_sec <= 3600.0
        or not 60.0 <= madis_poll_sec <= 900.0
        or not 1 <= gamma_pages <= MAX_GAMMA_PAGES
        or not 60.0 <= refresh_sec <= 3600.0
        or not 10.0 <= retry_sec <= refresh_sec
    ):
        raise ValueError("reported_trajectory_awc_ingress_registry_poll_policy_invalid")
    return dict(value)


def city_from_slug(slug: str) -> str:
    match = SLUG_RE.match(str(slug or "").strip())
    if match is None:
        raise ValueError("highest_temperature_slug_invalid")
    return match.group("city")


def fetch_gamma_event(slug: str, *, timeout: float = 8.0) -> dict[str, Any] | None:
    url = GAMMA_ENDPOINT + "?" + urllib.parse.urlencode({"slug": slug})
    value = _gamma_json(url, timeout=timeout)
    if not isinstance(value, list) or not value:
        return None
    event = value[0]
    return dict(event) if isinstance(event, Mapping) else None


def fetch_gamma_events_window(
    *, start_date: date, end_date: date, timeout: float = 10.0, max_pages: int = 4
) -> list[dict[str, Any]]:
    """Fetch the official highest-temperature tag in bounded 100-row pages."""

    page_count = max(1, min(MAX_GAMMA_PAGES, int(max_pages)))

    def page(offset: int) -> list[dict[str, Any]]:
        params = {
            "limit": "100",
            "offset": str(offset),
            "tag_id": "104596",
            "end_date_min": start_date.isoformat() + "T00:00:00Z",
            "end_date_max": end_date.isoformat() + "T23:59:59Z",
            "order": "endDate",
            "ascending": "true",
        }
        url = GAMMA_ENDPOINT + "?" + urllib.parse.urlencode(params)
        value = _gamma_json(url, timeout=timeout)
        if not isinstance(value, list):
            raise ValueError("gamma_highest_temperature_page_invalid")
        return [dict(row) for row in value if isinstance(row, Mapping)]

    pages: dict[int, list[dict[str, Any]]] = {}
    page_errors: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=page_count) as executor:
        futures = {
            executor.submit(page, offset * 100): offset
            for offset in range(page_count)
        }
        for future in as_completed(futures):
            page_index = futures[future]
            try:
                pages[page_index] = future.result()
            except Exception as exc:
                # A single Gamma page must never hide markets discovered by
                # sibling pages.  The daemon will only fail closed if every
                # page and every slug fallback is unavailable.
                page_errors[page_index] = f"page_{page_index}:{type(exc).__name__}:{str(exc)[:120]}"
    if not pages and page_errors:
        raise GammaFetchError("gamma_events_window_failed", errors=tuple(page_errors.values()))
    events: dict[str, dict[str, Any]] = {}
    for index in sorted(pages):
        for event in pages[index]:
            slug = str(event.get("slug") or "")
            if slug.startswith("highest-temperature-in-"):
                events[slug] = event
    # An empty, successful response is a valid no-market result.  It remains
    # distinguishable from a transport outage above and lets the caller keep
    # its previous registry generation without deleting anything.
    return [events[key] for key in sorted(events)]


def _positive_number(value: Any) -> float | None:
    """A strictly positive finite number, or ``None`` for anything else.

    Gamma sends ``orderMinSize``/``orderPriceMinTickSize`` as bare JSON
    numbers today and as decimal strings on some events, so parse both and
    reject everything else rather than coercing.  These two values are the
    exchange's own admission rule for an order; a wrong one is worse than a
    missing one, because a missing one fails closed downstream and a wrong
    one signs and posts an order the book will refuse.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")) or parsed <= 0:
        return None
    return parsed


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return list(parsed) if isinstance(parsed, list) else []
    return []


def materialize_event_registry(
    event: Mapping[str, Any], *, event_date: str, config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    slug = str(event.get("slug") or "").strip()
    city = city_from_slug(slug)
    description = str(event.get("description") or "")
    station_match = next(
        (match for match in (pattern.search(description) for pattern in STATION_RES) if match),
        None,
    )
    station = station_match.group("station").upper() if station_match else ""
    origin_route = station_source_route(station) if station else {}
    resolution_url = station_match.group(0).rstrip(". ") if station_match else ""
    timezones = config.get("settlement_timezones") if isinstance(config.get("settlement_timezones"), Mapping) else {}
    timezone_name = str(timezones.get(city) or "").strip()
    sources = config.get("source_policy") if isinstance(config.get("source_policy"), Mapping) else {}
    amsc_policy = (sources.get("amsc") or {}) if isinstance(sources.get("amsc"), Mapping) else {}
    awc_policy = (sources.get("awc") or {}) if isinstance(sources.get("awc"), Mapping) else {}
    amsc_stations = _station_scope(amsc_policy, "station_ids") or set()
    awc_excluded_stations = _station_scope(awc_policy, "exclude_station_ids") or set()
    global_awc = (
        amsc_policy.get("enabled") is False
        and amsc_policy.get("production_ingest") is False
        and amsc_policy.get("role") == "retired_history_only"
        and not amsc_stations
        and not (_city_scope(amsc_policy, "city_slugs") or set())
        and awc_policy.get("enabled") is True
        and awc_policy.get("production_ingest") is True
        and awc_policy.get("global_primary") is True
        and awc_policy.get("replaces_amsc") is True
        and not awc_excluded_stations
        and not (_city_scope(awc_policy, "exclude_city_slugs") or set())
    )
    source_lane = "awc" if global_awc else "blocked"
    blocks: list[str] = []
    if source_lane == "blocked":
        blocks.append("no_enabled_official_source_lane")
    if not str(origin_route.get("provider_id") or "").strip():
        blocks.append("official_source_route_unmapped")
    elif str(origin_route.get("adapter") or "").strip().lower() == "amsc":
        # AMSC remains a historical/offline provider only.
        blocks.append("amsc_production_route_forbidden")
    if not station:
        blocks.append("official_resolution_is_not_airport_station")
    if not timezone_name:
        blocks.append("settlement_timezone_unmapped")
    if "whole degrees" not in description.lower():
        blocks.append("official_resolution_not_whole_degree")
    markets = [dict(row) for row in event.get("markets") or [] if isinstance(row, Mapping)]
    bucket_inputs = [{"outcome": str(row.get("groupItemTitle") or "").strip()} for row in markets]
    try:
        buckets = normalize_market_buckets(bucket_inputs)
    except ValueError as exc:
        buckets = []
        blocks.append(str(exc))
    bucket_by_label = {row["outcome"]: row for row in buckets}
    rows: list[dict[str, Any]] = []
    for market in markets:
        outcome = str(market.get("groupItemTitle") or "").strip()
        outcomes, tokens = _json_list(market.get("outcomes")), _json_list(market.get("clobTokenIds"))
        token_map = {str(name).upper(): str(token) for name, token in zip(outcomes, tokens)}
        interval = bucket_by_label.get(outcome, {})
        row_blocks = list(blocks)
        if not token_map.get("YES") or not token_map.get("NO"):
            row_blocks.append("official_market_token_mapping_missing")
        if not interval.get("unit"):
            row_blocks.append("official_market_bucket_interval_missing")
        order_min_size = _positive_number(market.get("orderMinSize"))
        order_price_min_tick_size = _positive_number(market.get("orderPriceMinTickSize"))
        if order_min_size is None or order_price_min_tick_size is None:
            row_blocks.append("official_market_order_sizing_rule_missing")
        eligible = not row_blocks
        registry = {
            "schema": SCHEMA_VERSION,
            "event_id": str(event.get("id") or ""),
            "market_id": str(market.get("id") or ""),
            "condition_id": str(market.get("conditionId") or ""),
            "slug": slug,
            "event_date": event_date,
            "city_slug": city,
            "station_id": station,
            "settlement_timezone": timezone_name,
            "settlement_unit": interval.get("unit"),
            "source_lane": source_lane,
            "source_eligible": eligible,
            "source_block_reasons": sorted(set(row_blocks)),
            "resolution_url": resolution_url,
            "outcome": outcome,
            "lower": interval.get("lower"),
            "upper": interval.get("upper"),
            "kind": interval.get("kind"),
            "yes_token": token_map.get("YES", ""),
            "no_token": token_map.get("NO", ""),
            "market_active": market.get("active") is True and market.get("closed") is not True,
            "market_accepting_orders": market.get("acceptingOrders") is True,
            # The exchange's own minimum lot and price increment.  Without
            # these the only thing that knows an order is too small to exist
            # is the CLOB itself, which means every undersized order is
            # discovered by signing it and posting it.  Carried here so the
            # gateway can refuse it locally, before anything is signed.
            "order_min_size": order_min_size,
            "order_price_min_tick_size": order_price_min_tick_size,
            "rules_sha256": _sha({"description": description, "outcome": outcome, "resolution_url": resolution_url}),
            "source_disjoint": True,
            "origin_country_code": origin_route.get("country_code", ""),
            "preferred_origin_source_id": origin_route.get("provider_id", ""),
            "preferred_origin_adapter": origin_route.get("adapter", ""),
            "preferred_origin_authority": origin_route.get("authority", ""),
            "preferred_origin_access": origin_route.get("access", ""),
            "preferred_origin_url": origin_route.get("official_url", ""),
            "official_source_registry_sha256": origin_route.get("registry_sha256", ""),
        }
        rows.append(registry)
    return rows


def discovered_slugs(conn: sqlite3.Connection, *, today: date, horizon_days: int) -> list[tuple[str, str]]:
    end = today + timedelta(days=max(0, int(horizon_days)))
    return [
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            """SELECT slug,event_date FROM weather_market_discovery
               WHERE slug LIKE 'highest-temperature-in-%'
                 AND event_date>=? AND event_date<=?
               ORDER BY event_date,slug""",
            (today.isoformat(), end.isoformat()),
        )
    ]


def commit_registry_rows(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], *, refreshed_at: str) -> int:
    count = 0
    for raw in rows:
        row = dict(raw)
        registry_json = _json(row)
        cursor = conn.execute(
            """INSERT INTO reported_trajectory_market_registry(
                 slug,event_date,outcome,city_slug,station_id,settlement_timezone,settlement_unit,
                 source_lane,source_eligible,source_block_reason,resolution_url,rules_sha256,
                 yes_token,no_token,market_active,market_accepting_orders,registry_json,refreshed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(slug,event_date,outcome) DO UPDATE SET
                 city_slug=excluded.city_slug,station_id=excluded.station_id,
                 settlement_timezone=excluded.settlement_timezone,settlement_unit=excluded.settlement_unit,
                 source_lane=excluded.source_lane,source_eligible=excluded.source_eligible,
                 source_block_reason=excluded.source_block_reason,resolution_url=excluded.resolution_url,
                 rules_sha256=excluded.rules_sha256,yes_token=excluded.yes_token,no_token=excluded.no_token,
                 market_active=excluded.market_active,market_accepting_orders=excluded.market_accepting_orders,
                 registry_json=excluded.registry_json,refreshed_at=excluded.refreshed_at
               WHERE reported_trajectory_market_registry.registry_json
                     IS NOT excluded.registry_json""",
            (
                row["slug"], row["event_date"], row["outcome"], row["city_slug"], row.get("station_id"),
                row.get("settlement_timezone"), row.get("settlement_unit"), row["source_lane"],
                int(bool(row.get("source_eligible"))), ",".join(row.get("source_block_reasons") or []),
                row.get("resolution_url"), row["rules_sha256"], row.get("yes_token"), row.get("no_token"),
                int(bool(row.get("market_active"))), int(bool(row.get("market_accepting_orders"))),
                registry_json, refreshed_at,
            ),
        )
        # Existing market WS and UI infrastructure consume this SSOT table.
        conn.execute(
            """INSERT INTO market_tokens(
                 slug,event_date,outcome,yes_token,no_token,market_active,market_closed,
                 market_archived,market_accepting_orders,market_enable_orderbook,updated_at
               ) VALUES(?,?,?,?,?,?,0,0,?,1,?)
               ON CONFLICT(slug,event_date,outcome) DO UPDATE SET
                 yes_token=excluded.yes_token,no_token=excluded.no_token,
                 market_active=excluded.market_active,market_closed=0,market_archived=0,
                 market_accepting_orders=excluded.market_accepting_orders,
                 market_enable_orderbook=1,updated_at=excluded.updated_at
               WHERE market_tokens.yes_token IS NOT excluded.yes_token
                  OR market_tokens.no_token IS NOT excluded.no_token
                  OR market_tokens.market_active IS NOT excluded.market_active
                  OR market_tokens.market_closed IS NOT 0
                  OR market_tokens.market_archived IS NOT 0
                  OR market_tokens.market_accepting_orders
                     IS NOT excluded.market_accepting_orders
                  OR market_tokens.market_enable_orderbook IS NOT 1""",
            (
                row["slug"], row["event_date"], row["outcome"], row.get("yes_token"), row.get("no_token"),
                int(bool(row.get("market_active"))), int(bool(row.get("market_accepting_orders"))), refreshed_at,
            ),
        )
        count += max(0, int(cursor.rowcount or 0))
    return count


__all__ = [
    "DEFAULT_CONFIG", "SCHEMA_VERSION", "GammaFetchError", "city_from_slug", "commit_registry_rows",
    "discovered_slugs", "fetch_gamma_event", "fetch_gamma_events_window", "load_config",
    "materialize_event_registry",
]
