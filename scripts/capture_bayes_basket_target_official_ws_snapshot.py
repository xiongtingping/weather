#!/usr/bin/env python3
"""Capture one bounded official CLOB WS snapshot for the isolated target lane.

The resident ``market_ws_listener`` is intentionally owned by the protected
production line and follows that line's registry.  This one-shot collector
reads only the isolated target registry and opens its own official CLOB WS
connection.  It never writes SQLite, the resident WS manifest, historical
ticks, orders, or a gateway request.

The content-addressed registry snapshot owns the exact station/date scope and
each market's settlement timezone.  Forward P0 may use an explicitly marked
``forward_p0_catalog_only`` snapshot before that market-local day starts; the
snapshot remains read-only and is never ingested as a started generation.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import sys
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import certifi
import websockets

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.market_registry import load_market_registry_snapshot  # noqa: E402
from bayes_basket_target.settlement_time import settlement_day_window  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_PROCESS_RSS_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_official_ws_snapshot_capture_v1"
MANIFEST_SCHEMA = "clob_ws_latest_quote_snapshot_v1"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_TOKENS = 600
MAX_STATIONS = 45
MAX_SHARDS = 4
MAX_FRAME_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SEC = 45.0
DEFAULT_SHARD_SIZE = 200
ONE_SHOT_CLOSE_TIMEOUT_SEC = 0.25
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_official_ws_snapshot.json"
DEFAULT_NETWORK_CONFIG = ROOT / "config" / "trading" / "bayes_basket_target_research_network_v1.json"


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _configured_proxy(explicit: str = "") -> str | None:
    value = str(explicit or os.environ.get("PM_MARKET_WS_PROXY") or "").strip()
    if not value:
        candidates: list[str] = []
        try:
            payload = json.loads(DEFAULT_NETWORK_CONFIG.read_text(encoding="utf-8"))
            if isinstance(payload, Mapping) and payload.get("enabled") is True:
                raw_candidates = payload.get("proxy_candidates")
                if isinstance(raw_candidates, list):
                    candidates.extend(str(item).strip() for item in raw_candidates if str(item).strip())
                for key in ("proxy_url", "fallback_proxy_url"):
                    candidate = str(payload.get(key) or "").strip()
                    if candidate:
                        candidates.append(candidate)
        except (OSError, TypeError, ValueError):
            candidates = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                parsed = urlsplit(candidate)
                if parsed.scheme not in {"http", "https"} or parsed.hostname != "127.0.0.1" or parsed.port is None:
                    continue
                with socket.create_connection((parsed.hostname, parsed.port), timeout=0.25):
                    return candidate
            except (OSError, TypeError, ValueError):
                continue
        env_path = ROOT / ".env"
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if line.startswith("PM_MARKET_WS_PROXY="):
                    value = line.split("=", 1)[1].strip().strip("'\"")
                    break
        except OSError:
            pass
    if not value or value.lower() in {"none", "off", "false", "0", "direct"}:
        return None
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ContractError("target_ws_proxy_invalid")
    return value


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("target_ws_as_of_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("target_ws_as_of_timezone_required")
    return parsed.astimezone(timezone.utc)


def _normalize_station_scope(station_ids: Sequence[str] | None) -> tuple[str, ...]:
    """Normalize a requested subset; the registry still owns final scope."""

    return tuple(sorted({str(item).strip().upper() for item in (station_ids or []) if str(item).strip()}))


def _settlement_day_state(*, event_date: str, target_timezone: str, as_of_utc: str) -> str:
    window = settlement_day_window(event_date, target_timezone)
    as_of = _parse_utc(as_of_utc)
    start = _parse_utc(window["target_day_start_utc"])
    end = _parse_utc(window["target_day_end_utc"])
    return "not_started" if as_of < start else "open" if as_of < end else "closed"


def _registry_snapshot_scope(
    *,
    registry_snapshot: Path,
    event_date: str,
    as_of_utc: str,
    station_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Materialize one exact, bounded registry-owned WS subscription scope.

    An omitted station subset means "derive the exact non-empty allowlist from
    this already-scoped registry snapshot"; it never means an unscoped
    consumer fallback.  Settlement timezones stay attached to each station
    and token because one canonical 45-station event date may span zones.
    """

    payload = load_market_registry_snapshot(registry_snapshot)
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ContractError("target_ws_registry_rows_missing")
    requested_event_date = str(event_date or "").strip()
    if not requested_event_date:
        raise ContractError("target_ws_registry_event_date_missing")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ContractError("target_ws_registry_row_invalid")
    # Retained registry snapshots legitimately contain multiple settlement
    # dates and unsupported markets. Scope first; only the explicitly
    # requested date's eligible open rows may define this WS manifest.
    active_rows = [
        row
        for row in rows
        if (
            str(row.get("event_date") or "").strip()
            == requested_event_date
            and int(row.get("market_active") or 0) == 1
            and int(row.get("market_accepting_orders") or 0) == 1
            and int(row.get("source_eligible") or 0) == 1
        )
    ]
    if not active_rows:
        raise ContractError("target_ws_registry_active_scope_empty")
    registry_stations: set[str] = set()
    for row in active_rows:
        station_id = str(row.get("station_id") or "").strip().upper()
        if not station_id:
            raise ContractError("target_ws_registry_station_missing")
        registry_stations.add(station_id)
    requested_stations = set(_normalize_station_scope(station_ids))
    unknown_stations = sorted(requested_stations - registry_stations)
    if unknown_stations:
        raise ContractError(
            "target_ws_station_scope_not_in_registry:" + ",".join(unknown_stations)
        )
    stations = sorted(requested_stations or registry_stations)
    if not stations:
        raise ContractError("target_ws_station_allowlist_empty")
    if len(stations) > MAX_STATIONS:
        raise ContractError("target_ws_station_scope_cap_exceeded")

    active_rows = [
        row
        for row in active_rows
        if str(row.get("station_id") or "").strip().upper() in set(stations)
    ]

    timezones_by_station: dict[str, str] = {}
    states_by_station: dict[str, str] = {}
    markets_by_token: dict[str, dict[str, str]] = {}
    market_identities: dict[tuple[str, str], tuple[str, str]] = {}
    for station_id in stations:
        station_rows = [
            row
            for row in active_rows
            if str(row.get("station_id") or "").strip().upper() == station_id
        ]
        if not station_rows:
            raise ContractError(
                f"target_ws_registry_station_active_scope_empty:{station_id}"
            )
        station_timezones = {
            str(row.get("settlement_timezone") or "").strip()
            for row in station_rows
        }
        if "" in station_timezones or len(station_timezones) != 1:
            raise ContractError(
                f"target_ws_registry_settlement_timezone_conflict:{station_id}"
            )
        settlement_timezone = next(iter(station_timezones))
        state = _settlement_day_state(
            event_date=requested_event_date,
            target_timezone=settlement_timezone,
            as_of_utc=as_of_utc,
        )
        if state == "closed":
            raise ContractError(
                f"target_ws_registry_settlement_day_closed:{station_id}"
            )
        timezones_by_station[station_id] = settlement_timezone
        states_by_station[station_id] = state

        for row in station_rows:
            token = str(row.get("yes_token") or "").strip()
            if not token:
                raise ContractError("target_ws_registry_yes_token_missing")
            outcome = str(row.get("outcome") or "").strip()
            slug = str(row.get("slug") or "").strip()
            identity = (station_id, outcome)
            market_value = (slug, token)
            previous_market = market_identities.get(identity)
            if previous_market is not None and previous_market != market_value:
                raise ContractError(
                    f"target_ws_registry_duplicate_market_conflict:{station_id}:{outcome}"
                )
            market_identities[identity] = market_value
            token_scope = {
                "station_id": station_id,
                "event_date": requested_event_date,
                "settlement_timezone": settlement_timezone,
                "slug": slug,
                "outcome": outcome,
            }
            previous_token = markets_by_token.get(token)
            if previous_token is not None and previous_token != token_scope:
                raise ContractError(f"target_ws_registry_duplicate_token_conflict:{token}")
            markets_by_token[token] = token_scope

    filters = payload.get("filters")
    if "not_started" in set(states_by_station.values()) and (
        payload.get("forward_p0_catalog_only") is not True
        or not isinstance(filters, Mapping)
        or filters.get("include_not_started_for_p0") is not True
    ):
        raise ContractError("target_ws_forward_p0_registry_boundary_invalid")
    tokens = sorted(markets_by_token)
    if len(tokens) > MAX_TOKENS:
        raise ContractError("target_ws_token_scope_cap_exceeded")
    states = set(states_by_station.values())
    timezones = set(timezones_by_station.values())
    return {
        "event_date": requested_event_date,
        "station_allowlist": stations,
        "tokens": tokens,
        "settlement_timezones_by_station": timezones_by_station,
        "settlement_states_by_station": states_by_station,
        "settlement_state": next(iter(states)) if len(states) == 1 else "mixed",
        "target_timezone": next(iter(timezones)) if len(timezones) == 1 else None,
        "markets_by_token": {
            token: markets_by_token[token]
            for token in sorted(markets_by_token)
        },
        "registry_snapshot_sha256": str(payload.get("snapshot_sha256") or ""),
    }


def _registry_snapshot_tokens(
    *,
    registry_snapshot: Path,
    event_date: str,
    as_of_utc: str,
    station_ids: Sequence[str] | None = None,
) -> tuple[list[str], str | None, str]:
    """Compatibility projection for focused callers; ``run`` uses full scope."""

    scope = _registry_snapshot_scope(
        registry_snapshot=registry_snapshot,
        event_date=event_date,
        as_of_utc=as_of_utc,
        station_ids=station_ids,
    )
    return (
        list(scope["tokens"]),
        scope["target_timezone"],
        str(scope["settlement_state"]),
    )


def _side_levels(raw: Any, *, reverse: bool) -> list[dict[str, float]]:
    values: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            price = float(item.get("price"))
            size = float(item.get("size"))
        except (TypeError, ValueError):
            continue
        if not (0.0 < price <= 1.0) or size <= 0.0:
            continue
        values.append((price, size))
    values.sort(reverse=reverse)
    result: list[dict[str, float]] = []
    seen: set[float] = set()
    for price, size in values:
        if price in seen:
            continue
        seen.add(price)
        result.append({"price": price, "size": size})
    return result


def _iter_messages(raw: str | bytes) -> Iterable[Mapping[str, Any]]:
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        nested = value.get("data")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, Mapping)]
        return [value]
    return []


def _quote_from_message(message: Mapping[str, Any], *, observed_at: str) -> tuple[str, dict[str, Any]] | None:
    event_type = str(message.get("event_type") or message.get("type") or "").lower()
    if event_type not in {"book", "orderbook", "book_snapshot"} and not isinstance(message.get("asks"), list):
        return None
    token = str(message.get("asset_id") or message.get("token_id") or "").strip()
    asks = _side_levels(message.get("asks"), reverse=False)
    bids = _side_levels(message.get("bids"), reverse=True)
    # A Polymarket ``book`` is authoritative even when one side is empty.
    # Empty-side books are real evidence of one-sided liquidity, not a missing
    # websocket message.  Formal basket ingress still applies its stricter
    # two-sided bid/ask gate; this collector must not erase the source fact
    # before that gate can classify it.
    if not token or (not asks and not bids):
        return None
    if asks and bids and bids[0]["price"] >= asks[0]["price"]:
        return None
    if asks and bids:
        book_side_state = "two_sided"
    elif asks:
        book_side_state = "ask_only"
    else:
        book_side_state = "bid_only"
    quote = {
        "token_id": token,
        "side": "YES",
        "asks": asks,
        "bids": bids,
        "best_ask": asks[0]["price"] if asks else None,
        "best_bid": bids[0]["price"] if bids else None,
        "book_side_state": book_side_state,
        "book_observed_at": observed_at,
        "created_at": observed_at,
        "quote_source": "OFFICIAL_CLOB_WS",
    }
    return token, quote


def _quote_side_state(quote: Mapping[str, Any]) -> str:
    """Classify a quote, including older test/fixture payloads without a marker."""

    state = str(quote.get("book_side_state") or "").strip().lower()
    if state in {"two_sided", "ask_only", "bid_only"}:
        return state
    asks = quote.get("asks")
    bids = quote.get("bids")
    has_asks = isinstance(asks, list) and bool(asks)
    has_bids = isinstance(bids, list) and bool(bids)
    if has_asks and has_bids:
        return "two_sided"
    if has_asks:
        return "ask_only"
    if has_bids:
        return "bid_only"
    return "unknown"


def _build_manifest(
    *,
    event_date: str,
    as_of_utc: str,
    tokens: Sequence[str],
    quotes: Mapping[str, Mapping[str, Any]],
    shard_size: int,
    started_at_utc: str,
    finished_at_utc: str,
    errors: Sequence[str],
    timeout_sec: float,
    target_timezone: str | None,
    station_scope: Sequence[str],
    registry_scope: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_stations = _normalize_station_scope(station_scope)
    if not normalized_stations:
        raise ContractError("target_ws_manifest_station_allowlist_empty")
    if len(normalized_stations) > MAX_STATIONS:
        raise ContractError("target_ws_manifest_station_scope_cap_exceeded")
    if str(registry_scope.get("event_date") or "") != event_date:
        raise ContractError("target_ws_manifest_registry_event_date_mismatch")
    registry_stations = tuple(registry_scope.get("station_allowlist") or ())
    if _normalize_station_scope(registry_stations) != normalized_stations:
        raise ContractError("target_ws_manifest_registry_station_scope_mismatch")
    timezones_by_station = registry_scope.get("settlement_timezones_by_station")
    markets_by_token = registry_scope.get("markets_by_token")
    if not isinstance(timezones_by_station, Mapping) or not isinstance(markets_by_token, Mapping):
        raise ContractError("target_ws_manifest_registry_scope_invalid")
    if set(timezones_by_station) != set(normalized_stations):
        raise ContractError("target_ws_manifest_registry_timezone_scope_mismatch")
    registry_snapshot_sha256 = str(
        registry_scope.get("registry_snapshot_sha256") or ""
    )
    if (
        len(registry_snapshot_sha256) != 64
        or any(character not in "0123456789abcdef" for character in registry_snapshot_sha256)
    ):
        raise ContractError("target_ws_manifest_registry_snapshot_sha256_invalid")
    token_set = sorted({str(token).strip() for token in tokens if str(token).strip()})
    if set(markets_by_token) != set(token_set):
        raise ContractError("target_ws_manifest_registry_token_scope_mismatch")
    for token in token_set:
        market = markets_by_token.get(token)
        if not isinstance(market, Mapping):
            raise ContractError("target_ws_manifest_registry_market_invalid")
        station_id = str(market.get("station_id") or "").strip().upper()
        if (
            station_id not in normalized_stations
            or str(market.get("event_date") or "") != event_date
            or str(market.get("settlement_timezone") or "")
            != str(timezones_by_station.get(station_id) or "")
        ):
            raise ContractError("target_ws_manifest_registry_market_scope_mismatch")
    quote_map = {token: dict(quotes[token]) for token in sorted(quotes) if token in token_set}
    shard_count = max(1, (len(token_set) + shard_size - 1) // shard_size)
    complete = len(quote_map) == len(token_set) and not errors
    side_state_by_token = {
        token: _quote_side_state(quote_map[token])
        for token in sorted(quote_map)
    }
    side_state_counts = {
        state: sum(1 for value in side_state_by_token.values() if value == state)
        for state in ("two_sided", "ask_only", "bid_only", "unknown")
    }
    # A one-sided official book is a materialized source fact.  Side
    # availability is carried independently so consumers can suppress BUY or
    # SELL candidates without deleting the token from exact-scope evidence.
    buy_incomplete_tokens = sorted(
        token for token in token_set
        if side_state_by_token.get(token) not in {"two_sided", "ask_only"}
    )
    sell_incomplete_tokens = sorted(
        token for token in token_set
        if side_state_by_token.get(token) != "two_sided"
    )
    core: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "state": "connected" if complete else "blocked",
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
        "quote_storage": "inline_v1",
        "quotes_by_token": quote_map,
        "unmaterialized_tokens": sorted(
            token for token in token_set
            if side_state_by_token.get(token) == "unknown"
        ),
        "buy_unavailable_tokens": buy_incomplete_tokens,
        "sell_unmaterialized_tokens": sell_incomplete_tokens,
        "subscribed_token_count": len(token_set),
        "received_book_token_count": len(quote_map),
        "materialized_token_count": (
            side_state_counts["two_sided"] + side_state_counts["ask_only"]
        ),
        "formal_incomplete_token_count": len(buy_incomplete_tokens),
        "sell_incomplete_token_count": len(sell_incomplete_tokens),
        "quote_side_state_counts": side_state_counts,
        "quote_side_state_by_token": side_state_by_token,
        "expected_shards": shard_count,
        "active_shards": list(range(shard_count)) if complete else [],
        "active_shard_generations": {str(i): 1 for i in range(shard_count)} if complete else {},
        "event_date": event_date,
        "target_local_date": event_date,
        "target_timezone": target_timezone,
        "settlement_timezone_authority": "market_registry.settlement_timezone",
        "station_scope": {
            "mode": "allowlist",
            "station_ids": list(normalized_stations),
            "source_lane": "REGISTRY_EXACT_SCOPE",
        },
        "market_registry_scope": {
            "event_date": event_date,
            "station_allowlist": list(normalized_stations),
            "settlement_timezones_by_station": {
                station_id: str(timezones_by_station[station_id])
                for station_id in normalized_stations
            },
            "markets_by_token": {
                token: dict(markets_by_token[token])
                for token in token_set
            },
            "registry_snapshot_sha256": str(
                registry_snapshot_sha256
            ),
        },
        "as_of_utc": as_of_utc,
        "connection_observed_at": finished_at_utc,
        "capture_window": {
            "started_at_utc": started_at_utc,
            "finished_at_utc": finished_at_utc,
            "timeout_sec": float(timeout_sec),
        },
        "capture_errors": sorted(str(item) for item in errors if str(item)),
    }
    core["snapshot_sha256"] = hashlib.sha256(_canonical(core)).hexdigest()
    return core


async def _capture_shard(tokens: Sequence[str], *, timeout_sec: float, shard_index: int, proxy: str | None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    quotes: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    frames_received = 0
    book_like_messages = 0
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    try:
        async with websockets.connect(
            WS_URL,
            ssl=ssl_context,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=20,
            # This is a bounded one-shot evidence collector. Once every
            # requested token has an authoritative book, waiting the default
            # five seconds for the peer's close handshake makes the already
            # captured quotes expire before the no-live worker can consume
            # them. Keep graceful close bounded without weakening quote
            # freshness or the overall capture deadline.
            close_timeout=ONE_SHOT_CLOSE_TIMEOUT_SEC,
            max_size=MAX_FRAME_BYTES,
            proxy=proxy,
        ) as ws:
            await ws.send(json.dumps({"assets_ids": list(tokens), "type": "market", "custom_feature_enabled": True}))
            while len(quotes) < len(tokens) and time.monotonic() < deadline:
                remaining = max(0.25, deadline - time.monotonic())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                frames_received += 1
                observed_at = _now_utc()
                for message in _iter_messages(raw):
                    if (
                        isinstance(message.get("asks"), list)
                        or isinstance(message.get("bids"), list)
                        or str(message.get("event_type") or message.get("type") or "").lower()
                        in {"book", "orderbook", "book_snapshot"}
                    ):
                        book_like_messages += 1
                    parsed = _quote_from_message(message, observed_at=observed_at)
                    if parsed is None:
                        continue
                    token, quote = parsed
                    if token in tokens:
                        quotes[token] = quote
                assert_process_memory_budget(limit=MAX_TARGET_PROCESS_RSS_BYTES)
    except Exception as exc:
        errors.append(f"shard_{shard_index}:{type(exc).__name__}:{str(exc)[:180]}")
    if frames_received == 0:
        errors.append(f"shard_{shard_index}:no_server_messages")
    elif book_like_messages == 0:
        errors.append(f"shard_{shard_index}:no_book_messages")
    missing = sorted(set(tokens) - set(quotes))
    if missing:
        errors.append(f"shard_{shard_index}:missing_tokens={len(missing)}")
    return quotes, errors


async def capture(
    *,
    tokens: Sequence[str],
    event_date: str,
    as_of_utc: str,
    timeout_sec: float,
    shard_size: int,
    proxy: str | None,
    target_timezone: str | None,
    station_scope: Sequence[str],
    registry_scope: Mapping[str, Any],
) -> dict[str, Any]:
    if not tokens:
        raise ContractError("target_ws_tokens_empty")
    shard_size = max(1, min(int(shard_size), MAX_TOKENS))
    shards = [list(tokens[i : i + shard_size]) for i in range(0, len(tokens), shard_size)]
    if len(shards) > MAX_SHARDS:
        raise ContractError("target_ws_shard_cap_exceeded")
    started = _now_utc()
    results = await asyncio.gather(
        *(_capture_shard(shard, timeout_sec=timeout_sec, shard_index=index, proxy=proxy) for index, shard in enumerate(shards)),
    )
    quotes: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for shard_quotes, shard_errors in results:
        quotes.update(shard_quotes)
        errors.extend(shard_errors)
    finished = _now_utc()
    manifest = _build_manifest(
        event_date=event_date,
        target_timezone=target_timezone,
        as_of_utc=as_of_utc,
        tokens=tokens,
        quotes=quotes,
        shard_size=shard_size,
        started_at_utc=started,
        finished_at_utc=finished,
        errors=errors,
        timeout_sec=timeout_sec,
        station_scope=station_scope,
        registry_scope=registry_scope,
    )
    return {
        "schema": SCHEMA,
        "state": "CAPTURED" if manifest["state"] == "connected" else "BLOCKED",
        "target_local_date": event_date,
        "as_of_utc": as_of_utc,
        "manifest": manifest,
        "captured_token_count": len(quotes),
        "subscribed_token_count": len(tokens),
        "errors": sorted(errors),
        "boundary": {
            "target_only": True,
            "official_ws_only": True,
            "writes_target_database": False,
            "writes_production_database": False,
            "writes_resident_ws_manifest": False,
            "writes_historical_ticks": False,
            "writes_event_market_ticks": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
            "protected_production_line_touched": False,
        },
    }


def _assert_output_disk_budget(path: Path) -> None:
    destination = path.expanduser().resolve()
    free_bytes = int(shutil.disk_usage(destination.parent).free)
    if free_bytes < MIN_TARGET_FREE_BYTES + MAX_JSON_OUTPUT_BYTES:
        raise ContractError("target_ws_output_min_free_space_guard")


def run(
    *,
    event_date: str,
    as_of_utc: str,
    output: Path,
    timeout_sec: float,
    shard_size: int,
    proxy: str = "",
    manifest_output: Path | None = None,
    station_ids: Sequence[str] | None = None,
    market_registry_snapshot: Path,
) -> dict[str, Any]:
    _parse_utc(as_of_utc)
    if timeout_sec <= 0.0 or timeout_sec > 120.0:
        raise ContractError("target_ws_timeout_out_of_bounds")
    output_path = output.expanduser().resolve()
    manifest_path = manifest_output.expanduser().resolve() if manifest_output is not None else None
    if manifest_path is not None and manifest_path == output_path:
        raise ContractError("target_ws_manifest_output_must_differ")
    registry_scope = _registry_snapshot_scope(
        registry_snapshot=market_registry_snapshot,
        event_date=event_date,
        as_of_utc=as_of_utc,
        station_ids=station_ids,
    )
    station_scope = tuple(registry_scope["station_allowlist"])
    tokens = list(registry_scope["tokens"])
    target_timezone = registry_scope["target_timezone"]
    settlement_state = str(registry_scope["settlement_state"])
    registry_mode = "verified_market_registry_snapshot"
    proxy_value = _configured_proxy(proxy)
    result = asyncio.run(
        capture(
            tokens=tokens,
            event_date=event_date,
            as_of_utc=as_of_utc,
            target_timezone=target_timezone,
            timeout_sec=timeout_sec,
            shard_size=shard_size,
            proxy=proxy_value,
            station_scope=station_scope,
            registry_scope=registry_scope,
        )
    )
    result["station_scope"] = {
        "mode": "allowlist",
        "station_ids": list(station_scope),
        "source_lane": "REGISTRY_EXACT_SCOPE",
        "registry_mode": registry_mode,
    }
    result["forward_p0_boundary"] = {
        "enabled": (
            settlement_state == "not_started"
            or "not_started" in set(
                registry_scope["settlement_states_by_station"].values()
            )
        ),
        "registry_snapshot_ingested": False,
        "started_registry_written": False,
        "orders_authorized": False,
    }
    result["transport"] = {"proxy_configured": bool(proxy_value), "proxy_value_retained": False}
    if manifest_path is not None:
        result["manifest_output"] = str(manifest_path)
    # A blocked reconnect may be recorded in the audit wrapper, but must not
    # replace the last consumer-facing good generation.
    if manifest_path is not None and result.get("state") == "CAPTURED":
        manifest = result.get("manifest")
        if not isinstance(manifest, Mapping):
            raise ContractError("target_ws_manifest_missing_from_capture")
        _assert_output_disk_budget(manifest_path)
        # Keep the consumer-facing root separate from the audit wrapper.  The
        # ingress loader verifies this exact content-addressed schema and must
        # never have to understand the collector's diagnostic envelope.
        write_bounded_json(manifest_path, manifest)
        result["manifest_written"] = True
    elif manifest_path is not None:
        result["manifest_written"] = False
        result["manifest_preserved_after_block"] = True
    result["receipt_sha256"] = hashlib.sha256(
        _canonical({k: v for k, v in result.items() if k != "receipt_sha256"})
    ).hexdigest()
    _assert_output_disk_budget(output_path)
    write_bounded_json(output_path, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--shard-size", type=int, default=DEFAULT_SHARD_SIZE)
    parser.add_argument("--proxy", default="", help="explicit HTTP(S) proxy; omitted uses PM_MARKET_WS_PROXY or local .env")
    parser.add_argument(
        "--station-id",
        action="append",
        default=[],
        help="explicit station allowlist; repeatable. Use this to isolate one source lane.",
    )
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=None,
        help="optional raw clob_ws_latest_quote_snapshot_v1 path for target evidence-cache ingress",
    )
    parser.add_argument(
        "--market-registry-snapshot",
        type=Path,
        required=True,
        help=(
            "verified market registry snapshot; settlement_timezone owns the day boundary"
        ),
    )
    args = parser.parse_args(argv)
    try:
        result = run(
            event_date=str(args.event_date).strip(),
            as_of_utc=str(args.as_of_utc).strip(),
            output=args.output,
            timeout_sec=float(args.timeout_sec),
            shard_size=int(args.shard_size),
            proxy=str(args.proxy or ""),
            manifest_output=args.manifest_output,
            station_ids=args.station_id,
            market_registry_snapshot=args.market_registry_snapshot,
        )
        print(json.dumps({
            "schema": result["schema"],
            "state": result["state"],
            "event_date": result["target_local_date"],
            "captured_token_count": result["captured_token_count"],
            "subscribed_token_count": result["subscribed_token_count"],
            "output": str(args.output.expanduser().resolve()),
            "manifest_output": str(args.manifest_output.expanduser().resolve()) if args.manifest_output is not None else "",
            "manifest_written": bool(result.get("manifest_written", False)),
            "manifest_preserved_after_block": bool(result.get("manifest_preserved_after_block", False)),
            "station_scope": result.get("station_scope", {}),
            "calls_order_gateway": False,
            "writes_target_database": False,
            "protected_production_line_touched": False,
        }, ensure_ascii=False, sort_keys=True))
        return 0 if result["state"] == "CAPTURED" else 2
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "state": "BLOCKED",
            "errors": [f"{type(exc).__name__}:{exc}"],
            "boundary": {
                "target_only": True,
                "writes_target_database": False,
                "writes_production_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_order_authorized": False,
                "protected_production_line_touched": False,
            },
        }
        try:
            _assert_output_disk_budget(args.output)
            write_bounded_json(args.output.expanduser().resolve(), result)
        except Exception:
            pass
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
