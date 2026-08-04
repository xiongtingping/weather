"""Pure planning contracts for the independent Polymarket copy-follow line.

The copy-follow line intentionally consumes only the public activity feed from
one configured wallet.  It does not import Bayes, regime, forecast, METAR or
TAF code.  Market execution evidence is supplied separately by the local
official CLOB WebSocket snapshot.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPSHandler, Request, ProxyHandler, build_opener, urlopen
from zoneinfo import ZoneInfo

from weather_basket_yes_runtime import parse_bucket_interval


SCHEMA_VERSION = "weather_copy_follow_v1"
ET = ZoneInfo("America/New_York")
EVENT_SLUG_RE = re.compile(
    r"^highest-temperature-in-(?P<city>.+)-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$",
    re.I,
)
MARKET_SLUG_EVENT_RE = re.compile(
    r"^(?P<event>highest-temperature-in-.+?-on-[a-z]+-\d{1,2}-\d{4})(?:-.+)?$",
    re.I,
)
BUCKET_TITLE_RE = re.compile(r"\bbe\s+(?P<bucket>.+?)\s+on\s+[A-Za-z]+\s+\d{1,2}\??$", re.I)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_id(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()[:32]


def parse_event_identity(event_slug: str) -> tuple[str, str] | None:
    match = EVENT_SLUG_RE.match(str(event_slug or "").strip())
    if match is None:
        return None
    try:
        event_date = date(
            int(match.group("year")),
            datetime.strptime(match.group("month"), "%B").month,
            int(match.group("day")),
        ).isoformat()
    except ValueError:
        return None
    return match.group("city").strip().lower(), event_date


def event_slug_from_activity(row: Mapping[str, Any]) -> str:
    """Return the canonical event slug when the API leaves eventSlug blank."""

    direct = str(row.get("eventSlug") or "").strip()
    if direct:
        return direct
    market_slug = str(row.get("slug") or "").strip()
    match = MARKET_SLUG_EVENT_RE.match(market_slug)
    return str(match.group("event") or "").strip() if match else ""


def parse_activity_bucket(title: str) -> str | None:
    match = BUCKET_TITLE_RE.search(str(title or "").strip())
    bucket = str(match.group("bucket") or "").strip() if match else ""
    bucket = re.sub(r"^between\s+", "", bucket, flags=re.I)
    return bucket or None


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _source_share_value(row: Mapping[str, Any]) -> float | None:
    """Read the source wallet's current share count from common API keys."""

    for key in ("size", "shareSize", "shares", "positionSize"):
        value = _finite(row.get(key))
        if value is not None and value > 0:
            return value
    return None


def activity_identity(row: Mapping[str, Any]) -> str:
    return stable_id(
        "copy_activity_",
        {
            "transaction_hash": row.get("transactionHash"),
            "asset": row.get("asset"),
            "timestamp": row.get("timestamp"),
            "size": row.get("size"),
            "price": row.get("price"),
            "side": row.get("side"),
            "outcome": row.get("outcome"),
        },
    )


def _default_activity_opener(request: Request, *, timeout: float):
    # The macOS Python runtime used by the local worker may not expose the
    # system CA bundle to urllib.  Use certifi when present, while retaining
    # normal certificate verification; never fall back to an unverified TLS
    # context.
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        context = ssl.create_default_context()
    # The interactive shell can retain a stale local proxy value after Clash
    # Verge changes ports. Resolve only the configured endpoint plus canonical
    # mixed port 1082 and keep normal TLS verification; this is a transport
    # repair, not a data or selection fallback. Explicit non-local proxies
    # remain untouched.
    proxy_keys = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")
    configured = next((str(os.environ.get(key) or "").strip() for key in proxy_keys if str(os.environ.get(key) or "").strip()), "")
    proxy = configured
    try:
        parsed = urlparse(configured)
        host = str(parsed.hostname or "").lower()
        port = int(parsed.port or 0)
    except (TypeError, ValueError):
        host, port = "", 0
    if host in {"127.0.0.1", "localhost", "::1"}:
        candidates = [port, 1082]
        for candidate in dict.fromkeys(item for item in candidates if item > 0):
            try:
                with socket.create_connection(("127.0.0.1", candidate), timeout=min(0.25, max(0.05, timeout / 10.0))):
                    proxy = f"http://127.0.0.1:{candidate}"
                    break
            except OSError:
                continue
    if proxy and proxy != configured:
        opener = build_opener(
            ProxyHandler({"http": proxy, "https": proxy}),
            HTTPSHandler(context=context),
        )
        return opener.open(request, timeout=timeout)
    return urlopen(request, timeout=timeout, context=context)


def fetch_activity(
    *,
    user: str,
    base_url: str = "https://data-api.polymarket.com/activity",
    page_size: int = 500,
    max_pages: int = 10,
    timeout_sec: float = 10.0,
    opener=_default_activity_opener,
) -> list[dict[str, Any]]:
    """Fetch bounded public activity pages without touching market REST data."""

    wallet = str(user or "").strip().lower()
    if not wallet:
        raise ValueError("copy_follow_source_user_missing")
    if not 1 <= int(page_size) <= 500 or not 1 <= int(max_pages) <= 20:
        raise ValueError("copy_follow_activity_pagination_invalid")
    rows: list[dict[str, Any]] = []
    for page in range(int(max_pages)):
        query = urlencode({"user": wallet, "limit": int(page_size), "offset": page * int(page_size)})
        request = Request(
            f"{str(base_url).rstrip('?') }?{query}",
            headers={"Accept": "application/json", "User-Agent": "weather-copy-follow/1"},
        )
        with opener(request, timeout=float(timeout_sec)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("copy_follow_activity_payload_invalid")
        rows.extend(item for item in payload if isinstance(item, Mapping))
        if len(payload) < int(page_size):
            break
    return [dict(item) for item in rows]


def fetch_trades(
    *,
    user: str,
    base_url: str = "https://data-api.polymarket.com/trades",
    page_size: int = 500,
    max_pages: int = 20,
    timeout_sec: float = 10.0,
    since_timestamp: int | None = None,
    required_event_slugs: Iterable[str] | None = None,
    opener=_default_activity_opener,
) -> list[dict[str, Any]]:
    """Fetch public trade history, stopping after the requested time window.

    The public ``activity`` endpoint is capped at a historical offset that is
    too short for a reliable seven-day window on an active wallet.  The
    ``trades`` endpoint exposes the same public fills with a larger usable
    history.  Normalize its rows to the internal activity shape so ranking
    remains isolated from execution selection.
    """

    wallet = str(user or "").strip().lower()
    if not wallet:
        raise ValueError("copy_follow_source_user_missing")
    if not 1 <= int(page_size) <= 500 or not 1 <= int(max_pages) <= 40:
        raise ValueError("copy_follow_trades_pagination_invalid")
    required_events = {
        str(value).strip()
        for value in (required_event_slugs or ())
        if str(value).strip()
    }
    rows: list[dict[str, Any]] = []
    for page in range(int(max_pages)):
        query = urlencode({"user": wallet, "limit": int(page_size), "offset": page * int(page_size)})
        request = Request(
            f"{str(base_url).rstrip('?')}?{query}",
            headers={"Accept": "application/json", "User-Agent": "weather-copy-follow/1"},
        )
        with opener(request, timeout=float(timeout_sec)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("copy_follow_trades_payload_invalid")
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            row = dict(item)
            row["type"] = "TRADE"
            size = _finite(row.get("size"))
            price = _finite(row.get("price"))
            if row.get("usdcSize") is None and size is not None and price is not None:
                row["usdcSize"] = size * price
            rows.append(row)
        if len(payload) < int(page_size):
            break
        if required_events:
            covered_events = {
                event_slug_from_activity(row)
                for row in rows
                if (
                    event_slug_from_activity(row)
                    and (
                        since_timestamp is None
                        or (_finite(row.get("timestamp")) is not None and int(_finite(row.get("timestamp")) or 0) >= int(since_timestamp))
                    )
                )
            }
            if required_events.issubset(covered_events):
                break
        timestamps = [_finite(item.get("timestamp")) for item in payload if isinstance(item, Mapping)]
        timestamps = [int(value) for value in timestamps if value is not None]
        if since_timestamp is not None and timestamps and min(timestamps) <= int(since_timestamp):
            break
    return rows


def fetch_positions(
    *,
    user: str,
    base_url: str = "https://data-api.polymarket.com/positions",
    page_size: int = 500,
    max_pages: int = 2,
    timeout_sec: float = 6.0,
    include_redeemable: bool = False,
    opener=_default_activity_opener,
) -> list[dict[str, Any]]:
    """Fetch the configured wallet's current positions in bounded pages.

    Copy-follow only needs the source wallet's presently held YES buckets.
    Positions are therefore the hot-path source of selection identity and
    notional.  Historical activity/trade pages are deliberately not fetched.
    """

    wallet = str(user or "").strip().lower()
    if not wallet:
        raise ValueError("copy_follow_source_user_missing")
    if not 1 <= int(page_size) <= 500 or not 1 <= int(max_pages) <= 10:
        raise ValueError("copy_follow_positions_pagination_invalid")
    rows: list[dict[str, Any]] = []
    for page in range(int(max_pages)):
        query = urlencode(
            {
                "user": wallet,
                "limit": int(page_size),
                "offset": page * int(page_size),
                "sizeThreshold": 0,
                "redeemable": "true" if include_redeemable else "false",
            }
        )
        request = Request(
            f"{str(base_url).rstrip('?') }?{query}",
            headers={"Accept": "application/json", "User-Agent": "weather-copy-follow/1"},
        )
        with opener(request, timeout=float(timeout_sec)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("copy_follow_positions_payload_invalid")
        rows.extend(item for item in payload if isinstance(item, Mapping))
        if len(payload) < int(page_size):
            break
    return [dict(item) for item in rows]


@dataclass(frozen=True)
class CopyFollowCandidate:
    source_user: str
    event_slug: str
    city_slug: str
    event_date: str
    buckets: tuple[str, ...]
    source_notional_by_bucket: dict[str, float]
    allocation_usdc_by_bucket: dict[str, float]
    total_source_notional_usdc: float
    total_allocation_usdc: float
    main_bucket: str
    last_activity_timestamp: int | None = None
    # Current source shares are separate from source capital.  The former
    # controls the bucket quantity mix; the latter controls the city-wide
    # scale factor used by the copy budget.
    source_shares_by_bucket: dict[str, float] = field(default_factory=dict)

    @property
    def selection_signature(self) -> str:
        return stable_id("copy_selection_", [self.event_slug, self.buckets])

    @property
    def plan_id(self) -> str:
        return stable_id("copy_plan_", [self.source_user, self.event_slug, self.buckets])


def _round_ratio_allocations(values: Mapping[str, float], budget_usdc: float) -> dict[str, float]:
    total = sum(float(value) for value in values.values())
    budget_cents = int(round(float(budget_usdc) * 100.0))
    if total <= 0 or budget_cents <= 0:
        return {str(key): 0.0 for key in values}
    raw_cents = {str(key): float(value) / total * budget_cents for key, value in values.items()}
    rounded = {key: int(math.floor(value + 1e-9)) for key, value in raw_cents.items()}
    remaining = budget_cents - sum(rounded.values())
    order = sorted(raw_cents, key=lambda key: (raw_cents[key] - rounded[key], key), reverse=True)
    for key in order[:remaining]:
        rounded[key] += 1
    return {key: cents / 100.0 for key, cents in rounded.items()}


def extract_four_bucket_candidates(
    rows: Iterable[Mapping[str, Any]],
    *,
    source_user: str,
    city_cap_usdc: float = 30.0,
    today_et: date | None = None,
    lookahead_days: int = 2,
) -> list[CopyFollowCandidate]:
    """Aggregate exact four-bucket BUY YES selections.

    Events with fewer or more than four observed buckets are deliberately
    excluded; the executor never silently chooses four out of a five-bucket
    selection.
    """

    cap = float(city_cap_usdc)
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError("copy_follow_city_cap_invalid")
    if lookahead_days < 0 or lookahead_days > 7:
        raise ValueError("copy_follow_lookahead_invalid")
    lower = today_et or datetime.now(ET).date()
    upper = lower + timedelta(days=int(lookahead_days))
    grouped: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for raw in rows:
        row = dict(raw)
        if activity_identity(row) in seen:
            continue
        seen.add(activity_identity(row))
        if str(row.get("type") or "").upper() != "TRADE":
            continue
        if str(row.get("side") or "").upper() != "BUY":
            continue
        if str(row.get("outcome") or "").strip().lower() != "yes":
            continue
        event_slug = event_slug_from_activity(row)
        identity = parse_event_identity(event_slug)
        bucket = parse_activity_bucket(str(row.get("title") or ""))
        notional = _finite(row.get("usdcSize"))
        if identity is None or not bucket or notional is None or notional <= 0:
            continue
        city_slug, event_date = identity
        parsed_date = date.fromisoformat(event_date)
        if parsed_date < lower or parsed_date > upper:
            continue
        event = grouped.setdefault(
            event_slug,
            {
                "city_slug": city_slug,
                "event_date": event_date,
                "buckets": {},
                "shares": {},
                "last_activity_timestamp": None,
            },
        )
        event["buckets"][bucket] = float(event["buckets"].get(bucket, 0.0)) + notional
        source_shares = _source_share_value(row)
        if source_shares is not None:
            event["shares"][bucket] = float(event["shares"].get(bucket, 0.0)) + source_shares
        activity_timestamp = _finite(row.get("timestamp"))
        if activity_timestamp is not None:
            previous = _finite(event.get("last_activity_timestamp"))
            event["last_activity_timestamp"] = int(max(activity_timestamp, previous or activity_timestamp))
    candidates: list[CopyFollowCandidate] = []
    for event_slug, event in sorted(grouped.items()):
        source_values = {
            str(bucket): round(float(value), 8)
            for bucket, value in event["buckets"].items()
            if float(value) > 0
        }
        if len(source_values) != 4:
            continue
        buckets = tuple(sorted(source_values, key=lambda item: (parse_bucket_sort_key(item), item)))
        allocations = _round_ratio_allocations(source_values, cap)
        main_bucket = max(source_values, key=lambda item: (source_values[item], item))
        candidates.append(
            CopyFollowCandidate(
                source_user=str(source_user).strip().lower(),
                event_slug=event_slug,
                city_slug=str(event["city_slug"]),
                event_date=str(event["event_date"]),
                buckets=buckets,
                source_notional_by_bucket=source_values,
                allocation_usdc_by_bucket={bucket: allocations[bucket] for bucket in buckets},
                total_source_notional_usdc=round(sum(source_values.values()), 8),
                total_allocation_usdc=round(sum(allocations.values()), 2),
                main_bucket=main_bucket,
                last_activity_timestamp=event.get("last_activity_timestamp"),
                source_shares_by_bucket={
                    bucket: round(float(event.get("shares", {}).get(bucket, 0.0) or 0.0), 8)
                    for bucket in buckets
                    if float(event.get("shares", {}).get(bucket, 0.0) or 0.0) > 0
                },
            )
        )
    return candidates


def _position_timestamp(row: Mapping[str, Any]) -> int | None:
    value = _finite(row.get("timestamp") or row.get("updatedAt") or row.get("createdAt"))
    if value is not None:
        return int(value)
    for key in ("updatedAt", "createdAt", "endDate"):
        raw = str(row.get(key) or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        return int(parsed.timestamp())
    return None


def position_rows_to_activity_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize Data API position rows to the internal four-bucket shape."""

    normalized: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        try:
            size = float(row.get("size") or 0.0)
        except (TypeError, ValueError):
            size = 0.0
        if not math.isfinite(size) or size <= 0:
            continue
        event_slug = event_slug_from_activity(row)
        if not event_slug:
            continue
        bucket = str(
            row.get("outcome")
            or row.get("outcomeName")
            or row.get("bucket")
            or ""
        ).strip()
        # Some API payloads put the bucket only in the title.  This parser is
        # intentionally exact; a non-temperature position must not become a
        # copied weather bucket by accident.
        if not bucket or parse_bucket_interval_safe(bucket) is None:
            bucket = parse_activity_bucket(str(row.get("title") or "")) or ""
        if not bucket or parse_bucket_interval_safe(bucket) is None:
            continue
        initial_value = _finite(row.get("initialValue"))
        avg_price = _finite(row.get("avgPrice") or row.get("averagePrice"))
        total_bought = _finite(row.get("totalBought"))
        notional = initial_value if initial_value is not None and initial_value > 0 else total_bought
        if notional is None or notional <= 0:
            notional = size * avg_price if avg_price is not None and avg_price > 0 else None
        if notional is None or notional <= 0:
            continue
        event_date_identity = parse_event_identity(event_slug)
        if event_date_identity is None:
            continue
        city_slug, event_date = event_date_identity
        parsed_event_date = datetime.fromisoformat(event_date)
        month_day = f"{parsed_event_date.strftime('%B')} {parsed_event_date.day}"
        # The normalized title keeps the audit table and the existing parser
        # compatible while retaining the original position payload below.
        normalized.append(
            {
                **row,
                "type": "TRADE",
                "side": "BUY",
                "outcome": "Yes",
                "eventSlug": event_slug,
                "title": f"Will the highest temperature in {city_slug} be {bucket} on {month_day}?",
                "usdcSize": float(notional),
                "size": size,
                "price": avg_price if avg_price is not None else (float(notional) / size if size else None),
                "timestamp": _position_timestamp(row),
                "source_position": True,
            }
        )
    return normalized


def parse_bucket_interval_safe(bucket: str) -> dict[str, Any] | None:
    try:
        return parse_bucket_interval(str(bucket))
    except (TypeError, ValueError, KeyError):
        return None


def extract_four_bucket_candidates_from_positions(
    rows: Iterable[Mapping[str, Any]],
    *,
    source_user: str,
    city_cap_usdc: float = 30.0,
    today_et: date | None = None,
    lookahead_days: int = 2,
) -> list[CopyFollowCandidate]:
    """Extract exact four-bucket candidates directly from open positions."""

    return extract_four_bucket_candidates(
        position_rows_to_activity_rows(rows),
        source_user=source_user,
        city_cap_usdc=city_cap_usdc,
        today_et=today_et,
        lookahead_days=lookahead_days,
    )


def rank_recent_city_candidates(
    positions: Iterable[Mapping[str, Any]],
    trades: Iterable[Mapping[str, Any]],
    *,
    source_user: str,
    max_cities: int = 5,
    window_days: int = 7,
    minimum_initial_value_usdc: float = 5.0,
    today_et: date | None = None,
    lookahead_days: int = 2,
    now_timestamp: int | None = None,
) -> dict[str, Any]:
    """Rank active exact-four weather cities by recent current-position ROI.

    The ranking is deliberately separate from bucket choice: positions define
    the exact four buckets and current PnL, while recent public trades only
    establish that the city was active during the ranking window.  No
    forecast, probability, regime, or weather field is read here.
    """

    try:
        limit = int(max_cities)
        days = int(window_days)
        minimum_initial = float(minimum_initial_value_usdc)
    except (TypeError, ValueError):
        return {"status": "blocked", "reason_codes": ["copy_follow_city_selection_parameters_invalid"]}
    if not 1 <= limit <= 20 or not 1 <= days <= 30 or not math.isfinite(minimum_initial) or minimum_initial < 0:
        return {"status": "blocked", "reason_codes": ["copy_follow_city_selection_parameters_invalid"]}

    position_rows = [dict(row) for row in positions if isinstance(row, Mapping)]
    trade_rows = [dict(row) for row in trades if isinstance(row, Mapping)]
    timestamps = [_finite(row.get("timestamp")) for row in trade_rows]
    timestamps = [int(value) for value in timestamps if value is not None]
    now = int(now_timestamp) if now_timestamp is not None else int(datetime.now(ZoneInfo("UTC")).timestamp())
    window_start = now - days * 86400
    recent_events: dict[str, dict[str, Any]] = {}
    for row in trade_rows:
        timestamp = _finite(row.get("timestamp"))
        if timestamp is None or int(timestamp) < window_start or int(timestamp) > now + 60:
            continue
        event_slug = event_slug_from_activity(row)
        if parse_event_identity(event_slug) is None:
            continue
        event = recent_events.setdefault(
            event_slug,
            {"trade_count": 0, "latest_timestamp": None, "buy_yes_usdc": 0.0},
        )
        event["trade_count"] += 1
        event["latest_timestamp"] = max(
            int(timestamp), int(event["latest_timestamp"] or timestamp)
        )
        if str(row.get("side") or "").upper() == "BUY" and str(row.get("outcome") or "").lower() == "yes":
            event["buy_yes_usdc"] += max(0.0, float(_finite(row.get("usdcSize")) or 0.0))

    candidates = extract_four_bucket_candidates_from_positions(
        position_rows,
        source_user=source_user,
        today_et=today_et,
        lookahead_days=lookahead_days,
    )
    position_by_event: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for row in position_rows:
        event_slug = event_slug_from_activity(row)
        bucket = parse_activity_bucket(str(row.get("title") or ""))
        if not event_slug or not bucket or parse_bucket_interval_safe(bucket) is None:
            continue
        position_by_event.setdefault(event_slug, []).append((bucket, row))

    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        activity = recent_events.get(candidate.event_slug)
        if activity is None:
            continue
        metric_rows = position_by_event.get(candidate.event_slug, [])
        initial = sum(max(0.0, float(_finite(row.get("initialValue")) or 0.0)) for _bucket, row in metric_rows)
        current = sum(max(0.0, float(_finite(row.get("currentValue")) or 0.0)) for _bucket, row in metric_rows)
        cash_pnl = sum(float(_finite(row.get("cashPnl")) or 0.0) for _bucket, row in metric_rows)
        realized_pnl = sum(float(_finite(row.get("realizedPnl")) or 0.0) for _bucket, row in metric_rows)
        if initial < minimum_initial or initial <= 0:
            continue
        total_pnl = cash_pnl + realized_pnl
        roi = total_pnl / initial
        ranked.append(
            {
                "candidate": candidate,
                "event_slug": candidate.event_slug,
                "city_slug": candidate.city_slug,
                "event_date": candidate.event_date,
                "selected_buckets": list(candidate.buckets),
                "main_bucket": candidate.main_bucket,
                "initial_value_usdc": round(initial, 8),
                "current_value_usdc": round(current, 8),
                "cash_pnl_usdc": round(cash_pnl, 8),
                "realized_pnl_usdc": round(realized_pnl, 8),
                "total_pnl_usdc": round(total_pnl, 8),
                "roi": round(roi, 10),
                "roi_percent": round(roi * 100.0, 6),
                "recent_trade_count": int(activity["trade_count"]),
                "recent_buy_yes_usdc": round(float(activity["buy_yes_usdc"]), 8),
                "latest_trade_timestamp": activity["latest_timestamp"],
            }
        )
    ranked.sort(
        key=lambda item: (
            -float(item["roi"]),
            -float(item["total_pnl_usdc"]),
            -float(item["initial_value_usdc"]),
            str(item["city_slug"]),
            str(item["event_slug"]),
        )
    )
    selected: list[dict[str, Any]] = []
    seen_cities: set[str] = set()
    for item in ranked:
        city = str(item["city_slug"])
        if city in seen_cities:
            continue
        seen_cities.add(city)
        selected.append(item)
        if len(selected) >= limit:
            break
    coverage_oldest = min(timestamps) if timestamps else None
    coverage_complete = coverage_oldest is not None and coverage_oldest <= window_start
    ranking_rows = [
        {key: value for key, value in item.items() if key != "candidate"}
        for item in ranked
    ]
    return {
        "status": "ready" if selected else "blocked",
        "reason_codes": [] if selected else ["copy_follow_recent_city_selection_empty"],
        "window_days": days,
        "window_start_timestamp": window_start,
        "window_end_timestamp": now,
        "trade_row_count": len(trade_rows),
        "recent_trade_event_count": len(recent_events),
        "trade_oldest_timestamp": coverage_oldest,
        "trade_latest_timestamp": max(timestamps) if timestamps else None,
        "trade_window_coverage_complete": coverage_complete,
        "candidate_count": len(ranked),
        "selected_city_count": len(selected),
        "selected_cities": [str(item["city_slug"]) for item in selected],
        "selected_events": [str(item["event_slug"]) for item in selected],
        "ranking": ranking_rows,
        "selected_ranking": [
            {key: value for key, value in item.items() if key != "candidate"}
            for item in selected
        ],
        "selected_candidates": [item["candidate"] for item in selected],
        "roi_definition": "(cashPnl + realizedPnl) / initialValue from current open positions",
    }


def assess_source_building(
    candidate: CopyFollowCandidate,
    *,
    now_timestamp: float | None = None,
    max_age_sec: float = 900.0,
    minimum_main_notional_usdc: float = 5.0,
    minimum_main_share: float = 0.35,
) -> dict[str, Any]:
    """Gate on a fresh four-bucket build with a materially funded main bucket."""

    try:
        now = float(now_timestamp if now_timestamp is not None else datetime.now(ET).timestamp())
        age_limit = float(max_age_sec)
        minimum_notional = float(minimum_main_notional_usdc)
        minimum_share = float(minimum_main_share)
    except (TypeError, ValueError):
        return {"active": False, "reason_codes": ["copy_follow_building_gate_parameters_invalid"]}
    latest = _finite(candidate.last_activity_timestamp)
    total = float(candidate.total_source_notional_usdc)
    main = float(candidate.source_notional_by_bucket.get(candidate.main_bucket, 0.0) or 0.0)
    share = main / total if total > 0 else 0.0
    age = now - latest if latest is not None else None
    reasons: list[str] = []
    if latest is None or age is None or age < -30.0 or age > age_limit:
        reasons.append("copy_follow_source_not_actively_building")
    if len(candidate.buckets) != 4:
        reasons.append("copy_follow_four_bucket_selection_not_formed")
    if main < minimum_notional:
        reasons.append("copy_follow_main_bucket_notional_not_large_enough")
    if share < minimum_share:
        reasons.append("copy_follow_main_bucket_share_not_large_enough")
    return {
        "active": not reasons,
        "reason_codes": reasons,
        "latest_activity_timestamp": int(latest) if latest is not None else None,
        "activity_age_sec": round(age, 3) if age is not None else None,
        "main_bucket": candidate.main_bucket,
        "main_bucket_source_notional_usdc": round(main, 8),
        "main_bucket_source_share": round(share, 8),
        "minimum_main_notional_usdc": minimum_notional,
        "minimum_main_share": minimum_share,
        "max_age_sec": age_limit,
    }


def parse_bucket_sort_key(bucket: str) -> tuple[float, str]:
    text = str(bucket or "").strip().replace("–", "-")
    values = re.findall(r"-?\d+(?:\.\d+)?", text)
    try:
        return float(values[0]), text
    except (IndexError, ValueError):
        return float("inf"), text


def compare_forecast_peak_to_bucket(
    peak_c: Any,
    bucket: str,
) -> dict[str, Any]:
    """Compare a Celsius forecast peak with one market bucket.

    This is presentation-only.  It deliberately does not decide whether a
    copy-follow plan is eligible and does not alter the selected buckets or
    settlement-floor allocation.
    """

    value_c = _finite(peak_c)
    result: dict[str, Any] = {
        "forecast_peak_c": value_c,
        "bucket": str(bucket or ""),
        "relation": "missing" if value_c is None else "unavailable",
        "inside_main_bucket": None,
    }
    if value_c is None:
        return result
    try:
        interval = parse_bucket_interval(str(bucket or ""))
    except (TypeError, ValueError):
        result["relation"] = "unparseable_main_bucket"
        return result
    unit = str(interval["unit"])
    value_market = value_c if unit == "C" else value_c * 9.0 / 5.0 + 32.0
    lower = interval.get("lower")
    upper = interval.get("upper")
    inside = (lower is None or value_market >= float(lower)) and (upper is None or value_market <= float(upper))
    if inside:
        relation = "inside_main_bucket"
    elif lower is not None and value_market < float(lower):
        relation = "below_main_bucket"
    elif upper is not None and value_market > float(upper):
        relation = "above_main_bucket"
    else:
        relation = "outside_main_bucket"
    center = (float(lower) + float(upper)) / 2.0 if lower is not None and upper is not None else None
    result.update(
        {
            "market_unit": unit,
            "forecast_peak_market_value": round(value_market, 3),
            "bucket_lower": lower,
            "bucket_upper": upper,
            "bucket_center": center,
            "delta_to_bucket_center": round(value_market - center, 3) if center is not None else None,
            "relation": relation,
            "inside_main_bucket": inside,
        }
    )
    return result


def allocate_settlement_floor(
    *,
    buckets: Iterable[str],
    source_allocations: Mapping[str, float],
    asks_by_bucket: Mapping[str, Iterable[tuple[float, float]]],
    budget_usdc: float,
    primary_bucket: str,
    secondary_bucket: str,
    safety_floor_usdc: float = 0.01,
    minimum_shares: float = 1.0,
    minimum_notional_usdc: float = 0.0,
    strict_share_gap: float = 1.0,
    maximum_buy_price: float = 0.99,
) -> dict[str, Any]:
    """Solve a source-biased four-leg basket under the settlement floor.

    Source allocations are only the initial preference.  The returned order
    quantities are repeatedly lifted until every scenario satisfies
    ``quantity_i - total_cost >= safety_floor_usdc`` and the primary/secondary
    ordering is strict.  Cost is calculated from every real ask level, not
    from the best ask alone.  If that safe basket cannot fit below the city
    cap, the result is blocked.
    """

    labels = tuple(str(item) for item in buckets)
    if len(labels) != 4 or len(set(labels)) != 4:
        return {"status": "blocked", "reason_codes": ["copy_follow_four_bucket_identity_invalid"]}
    if primary_bucket not in labels or secondary_bucket not in labels or primary_bucket == secondary_bucket:
        return {"status": "blocked", "reason_codes": ["copy_follow_primary_secondary_invalid"]}
    edges = tuple(item for item in labels if item not in {primary_bucket, secondary_bucket})
    if len(edges) != 2:
        return {"status": "blocked", "reason_codes": ["copy_follow_edge_bucket_identity_invalid"]}
    try:
        budget = float(budget_usdc)
        floor = float(safety_floor_usdc)
        min_shares = float(minimum_shares)
        minimum_notional = float(minimum_notional_usdc)
        gap = float(strict_share_gap)
        max_price = float(maximum_buy_price)
    except (TypeError, ValueError):
        return {"status": "blocked", "reason_codes": ["copy_follow_floor_parameters_invalid"]}
    if not all(math.isfinite(item) for item in (budget, floor, min_shares, minimum_notional, gap, max_price)) or budget <= 0 or floor < 0 or min_shares <= 0 or minimum_notional < 0 or gap < 0 or not 0 < max_price < 1:
        return {"status": "blocked", "reason_codes": ["copy_follow_floor_parameters_invalid"]}

    asks: dict[str, list[tuple[float, float]]] = {}
    for bucket in labels:
        levels: list[tuple[float, float]] = []
        for raw_price, raw_size in asks_by_bucket.get(bucket, ()):
            try:
                price, size = float(raw_price), float(raw_size)
            except (TypeError, ValueError):
                continue
            if math.isfinite(price) and math.isfinite(size) and 0 < price < 1 and size > 0 and price <= max_price + 1e-12:
                levels.append((price, size))
        levels.sort()
        if not levels:
            return {"status": "blocked", "reason_codes": [f"copy_follow_depth_missing:{bucket}"]}
        asks[bucket] = levels

    # ``sum(best_yes_ask)`` is a useful quote diagnostic, not an independent
    # strategy gate.  The only hard market-side gate is the settlement floor
    # after walking the actual ask depth.  A sum at or above one can still be
    # solvable for an uneven quantity vector, while rejecting it here would
    # incorrectly erase a valid four-bucket source selection.
    best_sum = sum(levels[0][0] for levels in asks.values())

    def cost_for(bucket: str, quantity: float) -> tuple[float, float] | None:
        remaining = float(quantity)
        cost = 0.0
        limit = 0.0
        for price, depth in asks[bucket]:
            take = min(depth, remaining)
            if take <= 0:
                continue
            cost += take * price
            remaining -= take
            limit = price
            if remaining <= 1e-8:
                break
        if remaining > 1e-7 or limit <= 0:
            return None
        return cost, limit

    # The source capital split is a preference, not a lower bound on every
    # leg's share count.  Converting a tiny cheap-leg allocation directly into
    # shares can create hundreds of mandatory shares; the old solver then
    # raised the primary leg to preserve the ordering and falsely reported a
    # city-cap/depth failure.  Start from the least expensive vector that
    # satisfies the strict share ordering and solve only the settlement-floor
    # fixed point.  The city budget is an upper bound, so unused headroom is
    # intentionally retained as safety reserve.
    def ordered_quantities(edge_floor: float) -> dict[str, float]:
        return {
            edge: float(edge_floor)
            for edge in edges
        } | {
            secondary_bucket: float(edge_floor + gap),
            primary_bucket: float(edge_floor + 2.0 * gap),
        }

    def evaluate(edge_floor: float) -> tuple[dict[str, float], dict[str, float], dict[str, float], float] | None:
        quantities = ordered_quantities(edge_floor)
        costs: dict[str, float] = {}
        limits: dict[str, float] = {}
        for bucket in labels:
            priced = cost_for(bucket, quantities[bucket])
            if priced is None:
                return None
            costs[bucket], limits[bucket] = priced
        return quantities, costs, limits, sum(costs.values())

    # If the best executable asks already sum to one (or more), every
    # selected quantity being at least total_cost + floor is mathematically
    # impossible.  Keep this as a solver failure rather than an independent
    # ask-sum gate: the ask sum is only a proof of why the hard settlement
    # floor cannot be certified here.
    if best_sum >= 1.0 - 1e-12:
        return {"status": "blocked", "reason_codes": ["copy_follow_floor_solver_not_converged"], "best_ask_sum": best_sum}

    best_prices = {bucket: asks[bucket][0][0] for bucket in labels}
    edge_floor = max(
        min_shares,
        max(
            [minimum_notional / best_prices[edge] for edge in edges]
            + [
                minimum_notional / best_prices[secondary_bucket] - gap,
                minimum_notional / best_prices[primary_bucket] - 2.0 * gap,
                1e-8,
            ]
        ),
    )
    initial = evaluate(edge_floor)
    if initial is None:
        missing_bucket = next(
            (bucket for bucket in labels if cost_for(bucket, ordered_quantities(edge_floor)[bucket]) is None),
            labels[0],
        )
        return {"status": "blocked", "reason_codes": [f"copy_follow_depth_insufficient:{missing_bucket}"], "best_ask_sum": best_sum}

    quantities, costs, limits, total_cost = initial
    floor_gap = edge_floor - total_cost - floor
    lower = edge_floor
    upper: float | None = None
    if floor_gap < -1e-8:
        # Find a bracket for the first feasible point.  The walk is bounded
        # and quote depth is checked at every candidate; this avoids the
        # runaway fixed-point behavior of the former source-ratio lower bound.
        probe = edge_floor
        for _ in range(256):
            probe = max(probe + 1.0, probe * 2.0)
            candidate = evaluate(probe)
            if candidate is None:
                # The exponential probe can jump beyond one leg's available
                # ask depth even though a smaller quantity between ``lower``
                # and ``probe`` satisfies the floor.  Find the largest
                # executable point first, then test its actual walked-depth
                # profit instead of treating the first depth miss as proof
                # that the floor is unsolvable.
                feasible_lower = lower
                infeasible_upper = probe
                for _ in range(80):
                    midpoint = (feasible_lower + infeasible_upper) / 2.0
                    midpoint_candidate = evaluate(midpoint)
                    if midpoint_candidate is None:
                        infeasible_upper = midpoint
                    else:
                        feasible_lower = midpoint
                candidate = evaluate(feasible_lower)
                if candidate is None:
                    return {"status": "blocked", "reason_codes": ["copy_follow_floor_solver_not_converged"], "best_ask_sum": best_sum}
                candidate_gap = feasible_lower - candidate[3] - floor
                if candidate_gap >= -1e-8:
                    upper = feasible_lower
                    break
                return {"status": "blocked", "reason_codes": ["copy_follow_floor_solver_not_converged"], "best_ask_sum": best_sum}
            candidate_gap = probe - candidate[3] - floor
            if candidate_gap >= -1e-8:
                upper = probe
                break
            lower = probe
        if upper is None:
            return {"status": "blocked", "reason_codes": ["copy_follow_floor_solver_not_converged"], "best_ask_sum": best_sum}
        # Narrow the first feasible edge quantity.  Walking actual ask depth
        # remains inside evaluate(), so no single-price approximation enters
        # the settlement calculation.
        for _ in range(80):
            midpoint = (lower + upper) / 2.0
            candidate = evaluate(midpoint)
            if candidate is None:
                lower = midpoint
                continue
            if midpoint - candidate[3] - floor >= -1e-8:
                upper = midpoint
            else:
                lower = midpoint
        final = evaluate(upper)
        if final is None:
            return {"status": "blocked", "reason_codes": ["copy_follow_floor_solver_not_converged"], "best_ask_sum": best_sum}
        quantities, costs, limits, total_cost = final

    base_quantities = dict(quantities)
    source_total = sum(
        max(0.0, float(source_allocations.get(bucket, 0.0) or 0.0))
        for bucket in labels
    )
    reserve = max(0.02, floor * (len(labels) + 1.0))
    reference_budget = max(0.0, min(budget - reserve, source_total or budget))
    desired_quantities = {
        bucket: (
            reference_budget
            * max(0.0, float(source_allocations.get(bucket, 0.0) or 0.0))
            / source_total
            / best_prices[bucket]
            if source_total > 0 and best_prices[bucket] > 0
            else 0.0
        )
        for bucket in labels
    }

    def biased_quantities(alpha: float) -> dict[str, float]:
        result = {
            bucket: max(base_quantities[bucket], alpha * desired_quantities[bucket])
            for bucket in labels
        }
        result[secondary_bucket] = max(
            result[secondary_bucket],
            max(result[edge] for edge in edges) + gap,
        )
        result[primary_bucket] = max(
            result[primary_bucket],
            result[secondary_bucket] + gap,
        )
        return result

    def evaluate_biased(alpha: float) -> tuple[dict[str, float], dict[str, float], dict[str, float], float] | None:
        candidate_quantities = biased_quantities(alpha)
        candidate_costs: dict[str, float] = {}
        candidate_limits: dict[str, float] = {}
        for bucket in labels:
            priced = cost_for(bucket, candidate_quantities[bucket])
            if priced is None:
                return None
            candidate_costs[bucket], candidate_limits[bucket] = priced
        candidate_total = sum(candidate_costs.values())
        candidate_profits = {
            bucket: candidate_quantities[bucket] - candidate_total
            for bucket in labels
        }
        if candidate_total > budget + 1e-7 or min(candidate_profits.values()) + 1e-7 < floor:
            return None
        return candidate_quantities, candidate_costs, candidate_limits, candidate_total

    # Follow the source main/secondary capital proportions as far as the hard
    # floor, share ordering, real depth and city cap jointly allow.  Alpha is
    # a preference strength: alpha=1 is the requested source split, while a
    # lower value is the closest safe projection when that split is not
    # executable together with the settlement rules.
    biased = evaluate_biased(1.0)
    if biased is None:
        low, high = 0.0, 1.0
        for _ in range(80):
            midpoint = (low + high) / 2.0
            if evaluate_biased(midpoint) is None:
                high = midpoint
            else:
                low = midpoint
        biased = evaluate_biased(low)
    if biased is not None:
        quantities, costs, limits, total_cost = biased

    # Keep a tiny deterministic cushion above the numerical boundary.  The
    # order path later rounds quantities upward, but the plan/report itself
    # must already show a non-negative safety margin at full precision.
    margin = max(1e-6, floor * 1e-5)
    quantities = {bucket: float(quantity) + margin for bucket, quantity in quantities.items()}

    costs = {}
    limits = {}
    for bucket in labels:
        priced = cost_for(bucket, quantities[bucket])
        if priced is None:
            return {"status": "blocked", "reason_codes": [f"copy_follow_depth_insufficient:{bucket}"]}
        costs[bucket], limits[bucket] = priced
    total_cost = sum(costs.values())
    profits = {bucket: quantities[bucket] - total_cost for bucket in labels}
    if total_cost > budget + 1e-7:
        return {
            "status": "blocked",
            "reason_codes": ["copy_follow_settlement_floor_exceeds_city_cap"],
            "total_cost_usdc": total_cost,
            "profits": profits,
            "best_ask_sum": best_sum,
        }
    if min(profits.values()) + 1e-7 < floor:
        return {"status": "blocked", "reason_codes": ["copy_follow_settlement_floor_not_met"], "total_cost_usdc": total_cost, "profits": profits}
    if not quantities[primary_bucket] > quantities[secondary_bucket] + gap - 1e-7 or not quantities[secondary_bucket] > max(quantities[edge] for edge in edges) + gap - 1e-7:
        return {"status": "blocked", "reason_codes": ["copy_follow_primary_secondary_order_not_met"], "total_cost_usdc": total_cost, "profits": profits}
    return {
        "status": "ready",
        "mode": "source_ratio_preference_with_settlement_floor",
        "buckets": list(labels),
        "primary_bucket": primary_bucket,
        "secondary_bucket": secondary_bucket,
        "edge_buckets": list(edges),
        "source_allocations_usdc": {bucket: round(float(source_allocations.get(bucket, 0.0) or 0.0), 2) for bucket in labels},
        "quantities": {bucket: round(quantities[bucket], 8) for bucket in labels},
        "costs_usdc": {bucket: round(costs[bucket], 8) for bucket in labels},
        "limit_prices": {bucket: limits[bucket] for bucket in labels},
        "total_cost_usdc": round(total_cost, 8),
        "settlement_profit_by_bucket": {bucket: round(profits[bucket], 8) for bucket in labels},
        "worst_case_profit_usdc": round(min(profits.values()), 8),
        "best_case_profit_usdc": round(max(profits.values()), 8),
        "best_ask_sum": best_sum,
        "safety_floor_usdc": floor,
    }


def allocate_source_scaled_copy(
    *,
    buckets: Iterable[str],
    source_notional_by_bucket: Mapping[str, float],
    source_shares_by_bucket: Mapping[str, float],
    best_ask_by_bucket: Mapping[str, float],
    budget_usdc: float,
) -> dict[str, Any]:
    """Scale the source wallet's capital and share mix into one city budget.

    The source capital total determines the city-wide scale factor.  The
    source share counts determine each bucket's target quantity.  This keeps
    both kinds of source concentration visible: a bucket funded heavily by
    the source remains capital-heavy in the report, while its actual copied
    quantity follows the source's share-heavy mix.  No settlement-floor or
    primary/secondary profit ordering is applied here.
    """

    labels = tuple(str(item) for item in buckets)
    if len(labels) != 4 or len(set(labels)) != 4:
        return {"status": "blocked", "reason_codes": ["copy_follow_four_bucket_identity_invalid"]}
    try:
        budget = float(budget_usdc)
    except (TypeError, ValueError):
        budget = 0.0
    if not math.isfinite(budget) or budget <= 0:
        return {"status": "blocked", "reason_codes": ["copy_follow_copy_budget_invalid"]}

    capital = {
        bucket: max(0.0, float(source_notional_by_bucket.get(bucket, 0.0) or 0.0))
        for bucket in labels
    }
    shares = {
        bucket: max(0.0, float(source_shares_by_bucket.get(bucket, 0.0) or 0.0))
        for bucket in labels
    }
    capital_total = sum(capital.values())
    shares_total = sum(shares.values())
    if capital_total <= 0:
        return {"status": "blocked", "reason_codes": ["copy_follow_source_capital_weight_missing"]}
    # Historical activity rows may not carry a share field.  Positions are
    # required to carry it, but this fallback keeps old activity snapshots
    # reportable without inventing a new bucket selection.
    if shares_total <= 0:
        shares = dict(capital)
        shares_total = capital_total

    prices: dict[str, float] = {}
    for bucket in labels:
        price = _finite(best_ask_by_bucket.get(bucket))
        if price is None or not 0 < price < 1:
            return {"status": "blocked", "reason_codes": [f"copy_follow_best_ask_missing:{bucket}"]}
        prices[bucket] = price

    capital_scale_factor = budget / capital_total
    price_scale_factor = 1.0
    quantities = {bucket: shares[bucket] * capital_scale_factor for bucket in labels}
    costs = {bucket: quantities[bucket] * prices[bucket] for bucket in labels}
    expected_cost = sum(costs.values())
    # The budget is the source-capital scaling reference, not a hard ceiling
    # in the active copy-follow mode.  Keep the source share ratio intact even
    # when current prices make the resulting basket cost more than that
    # reference budget; the production executor is buy-only and never sells
    # to force the basket back under the reference amount.
    budget_exceeded = expected_cost > budget + 1e-9
    effective_scale_factor = capital_scale_factor * price_scale_factor

    capital_weights = {bucket: capital[bucket] / capital_total for bucket in labels}
    share_weights = {bucket: shares[bucket] / shares_total for bucket in labels}
    return {
        "status": "ready",
        "mode": "source_capital_scale_and_share_ratio",
        "buckets": list(labels),
        "source_capital_by_bucket": {bucket: round(capital[bucket], 8) for bucket in labels},
        "source_shares_by_bucket": {bucket: round(shares[bucket], 8) for bucket in labels},
        "source_capital_weight_by_bucket": {bucket: round(capital_weights[bucket], 8) for bucket in labels},
        "source_share_weight_by_bucket": {bucket: round(share_weights[bucket], 8) for bucket in labels},
        "copy_scale_factor": round(effective_scale_factor, 12),
        "capital_scale_factor": round(capital_scale_factor, 12),
        "price_scale_factor": round(price_scale_factor, 12),
        "quantities": {bucket: round(quantities[bucket], 8) for bucket in labels},
        "costs_usdc": {bucket: round(costs[bucket], 8) for bucket in labels},
        "limit_prices": prices,
        "total_cost_usdc": round(expected_cost, 8),
        "budget_usdc": budget,
        "budget_exceeded": budget_exceeded,
        "settlement_floor_removed": True,
    }


__all__ = [
    "CopyFollowCandidate",
    "SCHEMA_VERSION",
    "activity_identity",
    "allocate_settlement_floor",
    "allocate_source_scaled_copy",
    "compare_forecast_peak_to_bucket",
    "assess_source_building",
    "event_slug_from_activity",
    "extract_four_bucket_candidates",
    "extract_four_bucket_candidates_from_positions",
    "fetch_activity",
    "fetch_trades",
    "fetch_positions",
    "parse_activity_bucket",
    "parse_bucket_sort_key",
    "parse_event_identity",
    "position_rows_to_activity_rows",
    "rank_recent_city_candidates",
    "stable_id",
]
