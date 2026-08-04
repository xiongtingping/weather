#!/usr/bin/env python3
"""Calculate and optionally execute one explicitly specified YES basket.

This is the manual execution adapter for ``weather_settlement_floor_biased_basket_v1``.
It has no selector, forecast, probability, regime, copy-follow, or NO path.  It
resolves the requested market from the local official registry, consumes the
official WS quote shards, reads the account's current YES inventory, and sends
positive additive differences through the single local CLOB gateway.  An
explicit ``--rebalance-sell`` run is a separate controlled reduction mode: it
submits only price-protected YES SELL FAK orders and stops for reconciliation
until the next position/quote/floor recomputation.

The caller's budget is an incremental top-up budget.  Historical inventory and
historical cost are used in the settlement proof, but do not consume this
round's top-up budget.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
import sqlite3
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def _load_runtime_env() -> None:
    """Load the same local runtime settings used by the production wrapper.

    The execution adapter is also invoked directly from a terminal during a
    manual top-up.  In that mode launchd has not exported ``.env`` yet, so a
    missing funder address or stale proxy used to look like a trading-path
    failure before any market work started.  Existing process environment
    values remain authoritative.
    """

    path = Path(os.environ.get("PM_SHARED_POLY_ENV_PATH") or ROOT / ".env")
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not (
            key.startswith("PM_")
            or key.startswith("POLY_")
            or key in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
        ):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_runtime_env()
DATA = ROOT / "data"
DB_PATH = DATA / "archive.db"
QUOTE_SHARDS = DATA / "market_ws_quote_shards"
WS_SNAPSHOT = DATA / "market_ws_latest_quotes.json"
GATEWAY = os.environ.get("PM_CLOB_ORDER_SERVICE_URL", "http://127.0.0.1:8765").rstrip("/")
STRATEGY_ID = "weather_settlement_floor_biased_basket_v1"
FILL_ROUNDING_TOLERANCE_SHARES = 0.01
DEFAULT_BUCKETS = ["35°C or below", "36°C", "37°C", "38°C", "39°C"]
DEFAULT_ROLES = {"35°C or below": "L", "36°C": "A", "37°C": "M", "38°C": "S", "39°C": "F"}

sys.path.insert(0, str(ROOT))
from basket_strategy.settlement_floor_allocator import (  # noqa: E402
    _quote_levels,
    build_staged_depth_slice,
    build_role_rebalance_plan,
    build_settlement_floor_plan,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _elapsed_ms(started: float) -> float:
    """Return a stable, compact duration for the execution trace."""

    return round(max(0.0, (time.perf_counter() - started) * 1000.0), 1)


_LOCAL_GATEWAY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_local(
    path: str,
    payload: Mapping[str, Any],
    *,
    timeout_seconds: float = 30.0,
    retry_attempts: int = 0,
) -> dict[str, Any]:
    """Call the local gateway without inheriting a stale external proxy.

    Read-only gateway calls may retry once.  Order submission calls keep the
    default of zero retries because a transport error after exchange POST has
    started is not safe to replay.
    """
    request = urllib.request.Request(
        GATEWAY + path,
        data=_json(dict(payload)).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    attempts = max(0, int(retry_attempts)) + 1
    for attempt in range(attempts):
        try:
            with _LOCAL_GATEWAY_OPENER.open(request, timeout=timeout_seconds) as response:
                raw = response.read()
            value = json.loads(raw.decode("utf-8")) if raw else {}
            return dict(value) if isinstance(value, Mapping) else {"ok": False, "error": "gateway_payload_invalid"}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if attempt + 1 >= attempts:
                return {"ok": False, "error": f"gateway_transport:{type(exc).__name__}"}
            time.sleep(0.05)


def _positions_proxy() -> str:
    """Select the reachable local proxy, using the single Clash Verge route."""

    configured: list[str] = []
    for key in (
        "PM_POSITIONS_PROXY",
        "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        value = str(os.environ.get(key) or "").strip()
        if value and value not in configured:
            configured.append(value)
    ports: list[int] = []
    for raw in (
        os.environ.get("PM_PROXY_PORTS", ""),
        os.environ.get("PM_PROXY_LOCAL_FALLBACK_PORTS", "1082"),
    ):
        for value in str(raw or "").replace(" ", ",").split(","):
            try:
                port = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= port <= 65535 and port not in ports:
                ports.append(port)
    ports.extend(port for port in (1082,) if port not in ports)
    for value in configured:
        try:
            parsed = urlparse(value if "://" in value else f"http://{value}")
            host = str(parsed.hostname or "").lower()
            port = int(parsed.port or 0)
        except (TypeError, ValueError):
            continue
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return value
        candidates = [port] + ports if port else ports
        for candidate in dict.fromkeys(item for item in candidates if item > 0):
            try:
                with socket.create_connection((host, candidate), timeout=0.15):
                    return f"http://{host}:{candidate}"
            except OSError:
                continue
    for candidate in dict.fromkeys(ports):
        try:
            with socket.create_connection(("127.0.0.1", candidate), timeout=0.15):
                return f"http://127.0.0.1:{candidate}"
        except OSError:
            continue
    return configured[0] if configured else ""


def _fetch_positions(token_ids: set[str]) -> dict[str, dict[str, float]]:
    address = str(os.environ.get("PM_FUNDER_ADDRESS") or os.environ.get("POLY_FUNDER_ADDRESS") or "").strip()
    if not address:
        raise RuntimeError("settlement_floor_funder_address_missing")
    template = str(os.environ.get("PM_POSITIONS_API_URL") or "https://data-api.polymarket.com/positions?user={address}&sizeThreshold=0&limit=500")
    url = template.replace("{address}", address)
    command = [
        "curl", "--silent", "--show-error", "--fail-with-body",
        "--compressed", "--connect-timeout", "3", "--max-time", "8",
        "--retry", "2", "--retry-delay", "0", "--retry-max-time", "8",
        "--retry-all-errors",
    ]
    proxy = _positions_proxy()
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(url)
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError("settlement_floor_positions_fetch_failed")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError("settlement_floor_positions_payload_invalid") from exc
    rows = payload if isinstance(payload, list) else payload.get("positions", payload.get("data", [])) if isinstance(payload, Mapping) else []
    result = {token: {"shares": 0.0, "cost_usdc": 0.0} for token in token_ids}
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        token = str(raw.get("asset") or raw.get("token_id") or raw.get("tokenId") or "").strip()
        if token not in result:
            continue
        shares = _finite(raw.get("size")) or 0.0
        average = _finite(raw.get("avgPrice", raw.get("avg_price")))
        initial_value = _finite(raw.get("initialValue", raw.get("initial_value")))
        if shares < 0 or (average is None and initial_value is None) or (average is not None and average < 0) or (initial_value is not None and initial_value < 0):
            continue
        result[token]["shares"] += shares
        # The positions API normally exposes avgPrice, but newly matched
        # inventory can briefly expose only initialValue.  Preserve the
        # actual notional instead of silently treating that position as free.
        result[token]["cost_usdc"] += (
            initial_value if initial_value is not None else shares * float(average)
        )
    return result


def _registry(slug: str, event_date: str, buckets: list[str]) -> dict[str, dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT outcome,yes_token,market_active,market_accepting_orders,registry_json "
            "FROM reported_trajectory_market_registry WHERE slug=? AND event_date=?",
            (slug, event_date),
        ).fetchall()
    finally:
        conn.close()
    by_outcome = {str(row["outcome"]): dict(row) for row in rows}
    missing = [bucket for bucket in buckets if bucket not in by_outcome]
    if missing:
        raise RuntimeError("settlement_floor_registry_bucket_missing:" + ",".join(missing))
    result: dict[str, dict[str, Any]] = {}
    for bucket in buckets:
        row = by_outcome[bucket]
        token = str(row.get("yes_token") or "").strip()
        if not token or int(row.get("market_active") or 0) != 1 or int(row.get("market_accepting_orders") or 0) != 1:
            raise RuntimeError("settlement_floor_market_not_orderable:" + bucket)
        try:
            registry_payload = json.loads(str(row.get("registry_json") or "{}"))
        except (TypeError, ValueError):
            registry_payload = {}
        condition_id = str(registry_payload.get("condition_id") or "").strip()
        if not condition_id:
            raise RuntimeError("settlement_floor_market_condition_missing:" + bucket)
        result[bucket] = {"yes_token": token, "condition_id": condition_id, "registry": row}
    return result


def _read_ws_snapshot(*, max_age_seconds: float | None = None) -> dict[str, Any]:
    try:
        parent = json.loads(WS_SNAPSHOT.read_text(encoding="utf-8"))
        observed = datetime.fromisoformat(str(parent.get("connection_observed_at", "")).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("settlement_floor_ws_snapshot_invalid") from exc
    if parent.get("schema") != "clob_ws_latest_quote_snapshot_v1" or parent.get("state") != "connected":
        raise RuntimeError("settlement_floor_ws_snapshot_not_connected")
    if parent.get("evidence_only") is not True or parent.get("authorizes_order") is not False:
        raise RuntimeError("settlement_floor_ws_boundary_invalid")
    if max_age_seconds is not None and (age < 0 or age > max_age_seconds):
        raise RuntimeError("settlement_floor_ws_snapshot_stale")
    return dict(parent)


def _quote(
    token: str,
    *,
    max_age_seconds: float,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    parent = dict(snapshot) if snapshot is not None else _read_ws_snapshot()
    try:
        observed = datetime.fromisoformat(str(parent.get("connection_observed_at", "")).replace("Z", "+00:00"))
        snapshot_age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("settlement_floor_ws_snapshot_invalid") from exc
    if snapshot_age < 0 or snapshot_age > max_age_seconds:
        raise RuntimeError("settlement_floor_ws_snapshot_stale")
    shard = QUOTE_SHARDS / (hashlib.sha256(token.encode("utf-8")).hexdigest() + ".json")
    try:
        value = json.loads(shard.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("settlement_floor_quote_shard_missing") from exc
    quote = value.get("quote") if isinstance(value, Mapping) else None
    if (
        value.get("schema") != "clob_ws_token_quote_snapshot_v1"
        or value.get("token_id") != token
        or value.get("evidence_only") is not True
        or value.get("authorizes_order") is not False
        or not isinstance(quote, Mapping)
    ):
        raise RuntimeError("settlement_floor_quote_shard_invalid")
    book_observed = datetime.fromisoformat(str(quote.get("book_observed_at", "")).replace("Z", "+00:00"))
    book_age = (datetime.now(timezone.utc) - book_observed.astimezone(timezone.utc)).total_seconds()
    if book_age < 0 or book_age > max_age_seconds:
        raise RuntimeError("settlement_floor_quote_stale")
    return dict(quote)


def _consumption(quote: Mapping[str, Any], shares: float) -> tuple[float, float]:
    book = _quote_levels(quote)
    if book is None:
        raise RuntimeError("settlement_floor_quote_missing")
    cost = book.cost_for(shares)
    if cost is None:
        raise RuntimeError("settlement_floor_order_depth_insufficient")
    remaining = shares
    limit = 0.0
    for level in book.levels:
        take = min(remaining, level.size)
        if take > 0:
            limit = level.price
            remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-8 or not 0 < limit < 1:
        raise RuntimeError("settlement_floor_order_depth_insufficient")
    # The exchange rejects prices below 0.01 even when the book contains a
    # cheaper tail quote.  Raising only the protection cap still consumes the
    # same cheaper asks and keeps the actual spend bounded by ``amount``.
    return cost, max(0.01, limit)


def _quote_tick_size(quote: Mapping[str, Any]) -> float:
    """Infer the CLOB tick from the official WS price levels.

    The local gateway may have a stale/default metadata cache.  Passing the
    precision observed in the same WS snapshot prevents it from rounding a
    0.001-tick price such as 0.016 down to 0.01 before submitting the FAK.
    """

    raw_tick = quote.get("tick_size", quote.get("tickSize"))
    try:
        parsed_tick = float(raw_tick)
        if math.isfinite(parsed_tick) and 0.0 < parsed_tick < 1.0:
            return parsed_tick
    except (TypeError, ValueError):
        pass
    prices: list[float] = []
    for key in ("asks", "bids"):
        levels = quote.get(key)
        if not isinstance(levels, list):
            continue
        for level in levels:
            if not isinstance(level, Mapping):
                continue
            price = _finite(level.get("price"))
            if price is not None and 0.0 < price < 1.0:
                prices.append(price)
    precision = 2
    for price in prices:
        try:
            exponent = abs(Decimal(str(price)).as_tuple().exponent)
        except (InvalidOperation, ValueError):
            continue
        precision = max(precision, min(4, exponent))
    return 10.0 ** (-precision)


def _rounded_submit_shares(shares: float) -> float:
    return math.ceil(float(shares) * 100.0 - 1e-9) / 100.0


def _order_identity(*, slug: str, event_date: str, city: str, bucket: str, token: str, order_type: str, price: float, shares: float, target_shares: float, side: str = "BUY") -> str:
    identity = {
        "strategy_id": STRATEGY_ID,
        "slug": slug,
        "event_date": event_date,
        "city": city,
        "bucket": bucket,
        "token_id": token,
        "side": str(side or "BUY").upper(),
        "order_type": order_type,
        "price": price,
        "shares": round(shares, 8),
        "target_shares": round(target_shares, 8),
    }
    return "settlement-floor:" + hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()


def _plan_payload(plan: Mapping[str, Any], *, slug: str, event_date: str, city: str, quotes: Mapping[str, Any], registry: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    orders: list[dict[str, Any]] = []
    # Floor first; lottery is deliberately last and never part of the proof.
    priority = {"F": 0, "S": 1, "A": 2, "M": 3, "B": 4, "L": 5}
    rows = sorted(
        [row for row in plan.get("target_rows", []) if float(row.get("new_shares") or 0.0) > 1e-8],
        key=lambda row: (priority.get(str(row.get("role") or ""), 9), str(row.get("bucket") or "")),
    )
    staged = str(plan.get("execution_mode") or "") == "staged_fak_then_gtc"
    for row in rows:
        bucket = str(row["bucket"])
        target_shares = float(row["new_shares"])
        if staged:
            split = build_staged_depth_slice(
                target_shares=target_shares,
                quote=quotes[bucket],
                minimum_order_shares=float(plan.get("minimum_order_shares") or 0.0),
            )
            if split.get("status") == "blocked":
                raise RuntimeError("settlement_floor_staged_depth_split_failed:" + ",".join(split.get("reason_codes", [])))
            immediate_shares = float(split["immediate_fak_shares"])
            pending_shares = float(split["pending_gtc_shares"])
            if immediate_shares <= 1e-8 and pending_shares <= 1e-8:
                continue
            order_type = "FAK" if immediate_shares > 1e-8 else "GTC"
            shares = immediate_shares if order_type == "FAK" else pending_shares
            if order_type == "FAK":
                cost = float(split["immediate_fak_cost_usdc"])
                limit = float(split["immediate_fak_limit_price"])
                amount = math.ceil(cost * 100.0 - 1e-10) / 100.0
            else:
                cost = float(split["pending_gtc_reserve_usdc"])
                limit = float(split["gtc_limit_price"])
                amount = None
        else:
            shares = target_shares
            # The CLOB SDK's explicit marketable-limit order format uses two
            # decimal places for BUY taker size (even when the price tick is
            # 0.001).  Round upward by at most 0.01 share so the protected
            # FAK cannot undershoot the settlement-floor target.
            cost, limit = _consumption(quotes[bucket], shares)
            amount = math.ceil(cost * 100.0 - 1e-10) / 100.0
            immediate_shares = shares
            pending_shares = 0.0
            order_type = "FAK"
        submit_shares = _rounded_submit_shares(shares)
        # The gateway's protected explicit FAK path signs a limit order whose
        # maker amount is ``price * size``; it does not use the USDC ``amount``
        # field to cap that maker amount.  Never request more shares than the
        # rounded amount can buy at the protection price, otherwise a moving
        # book can spend above the planner's all-in budget.
        if order_type == "FAK" and amount is not None and limit > 0:
            maker_cap_shares = math.floor(float(amount) / float(limit) * 100.0 + 1e-9) / 100.0
            submit_shares = min(submit_shares, maker_cap_shares)
        if submit_shares <= 1e-8:
            raise RuntimeError("settlement_floor_safe_fak_size_below_minimum")
        token = str(registry[bucket]["yes_token"])
        idem = _order_identity(
            slug=slug,
            event_date=event_date,
            city=city,
            bucket=bucket,
            token=token,
            order_type=order_type,
            price=limit,
            shares=submit_shares,
            target_shares=target_shares,
        )
        orders.append({
            "bucket": bucket,
            "role": row.get("role"),
            "token_id": token,
            "order_type": order_type,
            "target_new_shares": target_shares,
            "planned_shares": shares,
            "requested_shares": submit_shares,
            "estimated_cost_usdc": cost,
            "amount_usdc": amount,
            "limit_price": limit,
            "pending_gtc_shares": pending_shares,
            "gtc_limit_price": float(row.get("gtc_limit_price") or limit) if pending_shares > 1e-8 else None,
            "tick_size": _quote_tick_size(quotes[bucket]),
            "idempotency_key": idem,
            "order_intent_id": idem,
        })
    return {"orders": orders, "staged": staged}


def _rebalance_plan_payload(plan: Mapping[str, Any], *, slug: str, event_date: str, city: str, quotes: Mapping[str, Any], registry: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Convert a calculation-only role rebalance into SELL gateway intents.

    This path is intentionally separate from the additive BUY planner.  A
    controlled reduction is always a protected, marketable-limit FAK at the
    current best YES bid; it never creates a GTC remainder and never shares a
    BUY idempotency identity.
    """

    orders: list[dict[str, Any]] = []
    for row in plan.get("sell_orders", []):
        bucket = str(row.get("bucket") or "")
        requested = round(float(row.get("requested_sell_shares") or 0.0), 2)
        limit = _finite(row.get("best_bid"))
        if not bucket or requested <= 1e-8 or limit is None or not 0.0 < limit < 1.0:
            raise RuntimeError("settlement_floor_rebalance_sell_terms_invalid")
        token = str(registry[bucket]["yes_token"])
        idem = _order_identity(
            slug=slug,
            event_date=event_date,
            city=city,
            bucket=bucket,
            token=token,
            order_type="FAK",
            price=limit,
            shares=requested,
            target_shares=requested,
            side="SELL",
        )
        orders.append({
            "bucket": bucket,
            "role": row.get("role"),
            "token_id": token,
            "side": "SELL",
            "order_type": "FAK",
            "target_sell_shares": requested,
            "planned_shares": requested,
            "requested_shares": requested,
            "estimated_proceeds_usdc": float(row.get("estimated_proceeds_usdc") or 0.0),
            "remaining_shares": float(row.get("remaining_shares") or 0.0),
            "limit_price": limit,
            "tick_size": _quote_tick_size(quotes[bucket]),
            "idempotency_key": idem,
            "order_intent_id": idem,
        })
    return {"orders": orders, "staged": False}


def _gateway_health() -> dict[str, Any]:
    """Read health from the local gateway without inheriting a proxy."""

    request = urllib.request.Request(GATEWAY + "/health", headers={"Accept": "application/json"}, method="GET")
    with _LOCAL_GATEWAY_OPENER.open(request, timeout=3.0) as response:
        raw = response.read()
    value = json.loads(raw.decode("utf-8")) if raw else {}
    return dict(value) if isinstance(value, Mapping) else {"ok": False, "error": "gateway_health_payload_invalid"}


def _response_status(response: Mapping[str, Any]) -> str:
    exchange = response.get("exchange_response")
    exchange_status = exchange.get("status") if isinstance(exchange, Mapping) else None
    return str(response.get("status") or exchange_status or "").lower()


def _response_size_matched(response: Mapping[str, Any], *, side: str = "") -> float | None:
    # Gateway size_matched is the exchange takingAmount.  For YES SELLs the
    # exchange takingAmount is USDC proceeds, while makingAmount is shares.
    # Rebalance fill checks must therefore use the nested makingAmount.
    exchange = response.get("exchange_response")
    if isinstance(exchange, Mapping):
        response_side = str(exchange.get("side") or response.get("side") or side or "").upper()
        if response_side == "SELL":
            matched = _finite(exchange.get("makingAmount"))
            if matched is not None:
                return matched
    matched = _finite(response.get("size_matched"))
    if matched is not None:
        return matched
    if isinstance(exchange, Mapping):
        return _finite(exchange.get("size_matched"))
    return None


def _conditional_balance_shares(guard: Mapping[str, Any]) -> float | None:
    """Decode the gateway's 6-decimal YES balance evidence into shares."""

    evidence = guard.get("conditional_balance_allowance")
    if not isinstance(evidence, Mapping):
        return None
    raw_balance = evidence.get("balance")
    try:
        integer_balance = int(str(raw_balance))
    except (TypeError, ValueError):
        return None
    return integer_balance / 1_000_000.0 if integer_balance >= 0 else None


def _execute_rebalance(
    *,
    result: dict[str, Any],
    orders: list[Mapping[str, Any]],
    slug: str,
    event_date: str,
    city: str,
    submit_started: float,
    overall_started: float,
) -> int:
    """Submit only controlled SELLs and stop until the next recomputation."""

    if not orders:
        result.update({
            "fills": [],
            "pending_orders": [],
            "completed": True,
            "recompute_required": False,
            "status": "rebalance_not_required",
        })
        result["timings_ms"]["submit_ms"] = 0.0
        result["timings_ms"]["total_ms"] = _elapsed_ms(overall_started)
        print(_json(result))
        return 0

    try:
        health = _gateway_health()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result.update({"blocked": "settlement_floor_gateway_health_unknown", "gateway_health_error": type(exc).__name__})
        result["timings_ms"]["submit_ms"] = _elapsed_ms(submit_started)
        result["timings_ms"]["total_ms"] = _elapsed_ms(overall_started)
        print(_json(result))
        return 3

    result["gateway_health"] = {
        key: health.get(key)
        for key in ("ready", "order_submission_enabled", "live_pause_present", "read_only", "proxy")
    }
    if health.get("ready") is not True or health.get("order_submission_enabled") is not True:
        result["blocked"] = "settlement_floor_gateway_not_authorized"
        result["timings_ms"]["submit_ms"] = _elapsed_ms(submit_started)
        result["timings_ms"]["total_ms"] = _elapsed_ms(overall_started)
        print(_json(result))
        return 3

    def guard(order: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        token = str(order["token_id"])
        payload = {
            "token_id": token,
            "side": "SELL",
            "price": float(order["limit_price"]),
            "size": float(order["requested_shares"]),
            "notional": float(order["requested_shares"]) * float(order["limit_price"]),
        }
        return token, _post_local(
            "/execution-guard-snapshot",
            payload,
            timeout_seconds=3.0,
            retry_attempts=1,
        )

    with ThreadPoolExecutor(max_workers=min(2, len(orders)), thread_name_prefix="floor-rebalance-guard") as pool:
        guard_results = dict(pool.map(guard, orders))
    guard_by_token = {}
    for order in orders:
        token = str(order["token_id"])
        value = guard_results.get(token, {})
        balance_shares = _conditional_balance_shares(value)
        guard_by_token[token] = {
            key: value.get(key)
            for key in ("ok", "side", "evidence_only", "authorizes_order", "evidence_errors", "conditional_balance_allowance")
        }
        guard_by_token[token]["balance_shares"] = balance_shares
        guard_by_token[token]["requested_shares"] = float(order["requested_shares"])
    result["guard_evidence"] = {
        "side": "SELL",
        "all_ok": all(
            value.get("ok") is True
            and value.get("side") == "SELL"
            and value.get("evidence_only") is True
            and value.get("authorizes_order") is False
            and not value.get("evidence_errors")
            and _conditional_balance_shares(value) is not None
            and _conditional_balance_shares(value) + FILL_ROUNDING_TOLERANCE_SHARES >= float(order["requested_shares"])
            for order in orders
            for value in (guard_results.get(str(order["token_id"]), {}),)
        ),
        "by_token": guard_by_token,
    }
    if not result["guard_evidence"]["all_ok"]:
        result["blocked"] = "settlement_floor_rebalance_guard_blocked"
        result["timings_ms"]["submit_ms"] = _elapsed_ms(submit_started)
        result["timings_ms"]["total_ms"] = _elapsed_ms(overall_started)
        print(_json(result))
        return 3

    fills: list[dict[str, Any]] = []
    terminal_fak_statuses = {
        "matched", "filled", "executed", "partial", "partially_filled",
        "cancelled", "canceled", "expired",
    }
    for order in orders:
        idem = str(order["idempotency_key"])
        payload = {
            "schema": "settlement_floor_rebalance_sell_intent_v1",
            "strategy_id": STRATEGY_ID,
            "strategy_source": STRATEGY_ID,
            "order_execution_domain": "settlement_floor_basket",
            "order_execution_source": "manual_settlement_floor_rebalance",
            "order_execution_reason": "controlled_role_rebalance",
            "order_intent_action": "controlled_rebalance",
            "slug": slug,
            "event_date": event_date,
            "city": city,
            "bucket": order["bucket"],
            "outcome": order["bucket"],
            "outcome_side": "YES",
            "token_id": order["token_id"],
            "side": "SELL",
            "price": order["limit_price"],
            "size": order["requested_shares"],
            "tick_size": order["tick_size"],
            "order_type": "FAK",
            "post_only": False,
            "allow_market": False,
            "direct_target_order_context": False,
            "idempotency_key": idem,
            "order_intent_id": str(order["order_intent_id"]),
        }
        response = _post_local("/place-order", payload, timeout_seconds=30.0, retry_attempts=0)
        row = {**dict(order), "response": response}
        fills.append(row)
        if response.get("reconcile_required") is True:
            result.update({"blocked": "settlement_floor_rebalance_order_reconcile_required", "reconcile_required": True})
            break
        if response.get("ok") is not True:
            result["blocked"] = "settlement_floor_rebalance_order_submit_failed"
            break
        status = _response_status(response)
        matched = _response_size_matched(response, side="SELL")
        requested = float(order["requested_shares"])
        if status not in terminal_fak_statuses or matched is None:
            result.update({"blocked": "settlement_floor_rebalance_fill_state_unknown", "reconcile_required": True})
            break
        if matched < requested - FILL_ROUNDING_TOLERANCE_SHARES:
            result.update({"blocked": "settlement_floor_rebalance_partial_fill_reconcile_required", "reconcile_required": True})
            break
        if matched > requested + FILL_ROUNDING_TOLERANCE_SHARES:
            result.update({"blocked": "settlement_floor_rebalance_fill_exceeds_plan", "reconcile_required": True})
            break

    result["fills"] = fills
    result["pending_orders"] = []
    result["staged_pending"] = False
    result["timings_ms"]["submit_ms"] = _elapsed_ms(submit_started)
    result["timings_ms"]["total_ms"] = _elapsed_ms(overall_started)
    if result.get("blocked"):
        result["completed"] = False
        result["status"] = "reconcile_required_stop" if result.get("reconcile_required") else "blocked"
        print(_json(result))
        return 4
    result.update({
        "completed": True,
        "recompute_required": True,
        "next_action": "重新读取持仓、YES bid/ask 和结算地板后再决定是否补仓或继续减仓",
        "status": "rebalance_completed_recompute_required",
    })
    print(_json(result))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-date", default="2026-07-24")
    parser.add_argument("--city", default="chengdu")
    parser.add_argument("--budget", type=float, default=200.0)
    parser.add_argument("--buckets", default=",".join(DEFAULT_BUCKETS))
    parser.add_argument("--roles-json", default=json.dumps(DEFAULT_ROLES, ensure_ascii=False))
    parser.add_argument("--max-quote-age-sec", type=float, default=300.0)
    staged_group = parser.add_mutually_exclusive_group()
    staged_group.add_argument(
        "--staged",
        dest="staged",
        action="store_true",
        help="FAK 吃现有深度后，将未成交差额挂 GTC（默认）",
    )
    staged_group.add_argument(
        "--no-staged",
        dest="staged",
        action="store_false",
        help="严格模式：可见深度不足时不挂剩余 GTC，直接阻断",
    )
    parser.set_defaults(staged=True)
    parser.add_argument(
        "--rebalance-sell",
        action="store_true",
        help="按角色调仓计划只提交受控 YES SELL FAK；不在同一轮提交 BUY",
    )
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    buckets = [item.strip() for item in str(args.buckets).split(",") if item.strip()]
    roles = json.loads(args.roles_json)
    if not isinstance(roles, Mapping):
        raise RuntimeError("settlement_floor_roles_invalid")
    try:
        event_day = datetime.strptime(args.event_date, "%Y-%m-%d").strftime("%B-%-d-%Y").lower()
    except ValueError as exc:
        raise RuntimeError("settlement_floor_event_date_invalid") from exc
    slug = f"highest-temperature-in-{args.city}-on-{event_day}"
    rebalance_mode = bool(args.rebalance_sell)
    timings: dict[str, float] = {}
    overall_started = time.perf_counter()

    phase_started = time.perf_counter()
    registry = _registry(slug, args.event_date, buckets)
    timings["registry_ms"] = _elapsed_ms(phase_started)

    # The manifest is common to every token.  Read and validate it once, then
    # load the individual immutable shards concurrently.  This removes four
    # repeated manifest parses from the normal basket path without weakening
    # the WS freshness/boundary checks.
    phase_started = time.perf_counter()
    snapshot_max_age = max(
        float(args.max_quote_age_sec),
        900.0 if any(str(roles.get(bucket) or "") == "L" for bucket in buckets) else 0.0,
    )
    snapshot = _read_ws_snapshot(max_age_seconds=snapshot_max_age)
    quote_workers = max(1, min(8, len(buckets)))
    with ThreadPoolExecutor(max_workers=quote_workers, thread_name_prefix="floor-quote") as pool:
        quote_futures = {
            bucket: pool.submit(
                _quote,
                str(registry[bucket]["yes_token"]),
                max_age_seconds=(
                    max(float(args.max_quote_age_sec), 900.0)
                    if str(roles.get(bucket) or "") == "L"
                    else float(args.max_quote_age_sec)
                ),
                snapshot=snapshot,
            )
            for bucket in buckets
        }
        quotes = {bucket: quote_futures[bucket].result() for bucket in buckets}
    timings["quote_ms"] = _elapsed_ms(phase_started)

    # These are independent, read-only evidence calls.  Running them in
    # parallel removes the previous positions + open-orders serial wait.
    phase_started = time.perf_counter()
    token_ids = {str(item["yes_token"]) for item in registry.values()}

    def timed_call(function: Any, *call_args: Any, **call_kwargs: Any) -> tuple[Any, float]:
        started = time.perf_counter()
        value = function(*call_args, **call_kwargs)
        return value, _elapsed_ms(started)

    open_order_markets = sorted({
        str(item.get("condition_id") or "")
        for item in registry.values()
        if str(item.get("condition_id") or "").strip()
    })
    # Small offline/test registries and older local snapshots may not carry
    # condition_id. Keep their evidence semantics intact with one bounded
    # account-wide probe; production registries always take the scoped path.
    if not open_order_markets:
        open_order_markets = [""]
    # SakuraCat serializes authenticated CLOB requests under load.  Five
    # simultaneous market probes can make one probe time out even though the
    # same market succeeds alone.  Keep the evidence parallelism bounded to
    # two: positions remain concurrent with the probe lane, while target
    # market reconciles are paced and retried instead of producing a false
    # open-orders-unknown block.
    with ThreadPoolExecutor(max_workers=min(2, 1 + len(open_order_markets)), thread_name_prefix="floor-evidence") as pool:
        positions_future = pool.submit(timed_call, _fetch_positions, token_ids)
        open_order_futures = {
            market: pool.submit(
                timed_call,
                _post_local,
                "/lookup-open-orders",
                ({"max_pages": 1, "market": market} if market else {"max_pages": 3}),
                timeout_seconds=4.0,
                retry_attempts=1,
            )
            for market in open_order_markets
        }
        positions_by_token, positions_ms = positions_future.result()
        open_order_results = {market: open_order_futures[market].result() for market in open_order_markets}
    open_orders = {
        "ok": all(bool(value[0].get("ok") is True) for value in open_order_results.values()),
        "open_orders": [
            row
            for value, _elapsed in open_order_results.values()
            for row in (value.get("open_orders") or [])
        ],
        "market_results": {
            (market or "__account__"): value
            for market, (value, _elapsed) in open_order_results.items()
        },
    }
    open_orders_ms = round(max((_elapsed for _value, _elapsed in open_order_results.values()), default=0.0), 1)
    timings["positions_ms"] = positions_ms
    timings["open_orders_ms"] = open_orders_ms
    timings["evidence_parallel_ms"] = _elapsed_ms(phase_started)
    existing = {
        bucket: positions_by_token[str(registry[bucket]["yes_token"])]
        for bucket in buckets
    }
    if open_orders.get("ok") is not True:
        raise RuntimeError("settlement_floor_open_orders_unknown")
    target_tokens = {str(item["yes_token"]) for item in registry.values()}
    relevant_open = [
        row for row in open_orders.get("open_orders", [])
        if str(row.get("asset_id") or row.get("token_id") or row.get("tokenId") or "") in target_tokens
    ]
    if relevant_open and (rebalance_mode or not args.staged):
        raise RuntimeError(
            "settlement_floor_rebalance_relevant_open_orders_present"
            if rebalance_mode
            else "settlement_floor_relevant_open_orders_present"
        )
    phase_started = time.perf_counter()
    if rebalance_mode:
        plan = build_role_rebalance_plan(
            buckets=buckets,
            roles=roles,
            existing_positions=existing,
            sell_quotes=quotes,
            safety_pad_usdc=0.01,
            minimum_strict_share_gap=1.0,
            minimum_order_shares=0.01,
        )
    else:
        plan = build_settlement_floor_plan(
            budget_usdc=float(args.budget),
            budget_is_incremental=True,
            buckets=buckets,
            roles=roles,
            quotes=quotes,
            existing_positions=existing,
            safety_pad_usdc=0.01,
            lottery_budget_usdc=0.0,
            minimum_order_shares=1.0,
            profit_order_step_shares=1.0,
            minimum_order_notional_usdc=1.0,
            allow_staged_depth=bool(args.staged),
            allow_pending_only_when_ask_sum_ge_one=bool(args.staged),
        )
    order_plan = None
    if plan.get("status") == "ready":
        order_plan = (
            _rebalance_plan_payload(plan, slug=slug, event_date=args.event_date, city=args.city, quotes=quotes, registry=registry)
            if rebalance_mode
            else _plan_payload(plan, slug=slug, event_date=args.event_date, city=args.city, quotes=quotes, registry=registry)
        )
    timings["planning_ms"] = _elapsed_ms(phase_started)
    result: dict[str, Any] = {
        "schema": "settlement_floor_execution_report_v1",
        "strategy_id": STRATEGY_ID,
        "event_date": args.event_date,
        "city": args.city,
        "slug": slug,
        "generated_at": _now(),
        "execution_requested": bool(args.execute),
        "execution_mode_requested": (
            "controlled_rebalance_sell_fak"
            if rebalance_mode
            else "staged_fak_then_gtc" if args.staged else "fak_complete"
        ),
        "rebalance_sell_requested": rebalance_mode,
        "open_orders": relevant_open,
        "existing_positions": existing,
        "plan": plan,
        "timings_ms": timings,
    }
    if plan.get("status") != "ready":
        result["timings_ms"]["submit_ms"] = 0.0
        result["timings_ms"]["total_ms"] = _elapsed_ms(overall_started)
        print(_json(result))
        return 2
    assert order_plan is not None
    result["orders"] = order_plan["orders"]
    result["staged"] = order_plan["staged"]
    result["pending_gtc_reserve_usdc"] = float(plan.get("pending_gtc_reserve_usdc") or 0.0)
    result["settlement_floor_proof_status"] = plan.get("settlement_floor_proof_status")
    if rebalance_mode:
        result["settlement_floor_proof_status"] = "rebalance_post_sale_floor_recomputed"
    if not args.execute:
        result["timings_ms"]["submit_ms"] = 0.0
        result["timings_ms"]["total_ms"] = _elapsed_ms(overall_started)
        print(_json(result))
        return 0
    if rebalance_mode:
        return _execute_rebalance(
            result=result,
            orders=list(order_plan["orders"]),
            slug=slug,
            event_date=args.event_date,
            city=args.city,
            submit_started=time.perf_counter(),
            overall_started=overall_started,
        )
    existing_open_tokens = {
        str(row.get("asset_id") or row.get("token_id") or row.get("tokenId") or "")
        for row in relevant_open
    }
    executable_orders = [
        order for order in order_plan["orders"]
        if str(order.get("token_id") or "") not in existing_open_tokens
    ]
    result["skipped_existing_open_orders"] = [
        order for order in order_plan["orders"]
        if str(order.get("token_id") or "") in existing_open_tokens
    ]
    submit_started = time.perf_counter()
    if executable_orders:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="floor-submit-gate") as pool:
            guard_future = pool.submit(
                _post_local,
                "/execution-guard-snapshot",
                {"token_id": executable_orders[0]["token_id"], "side": "BUY", "price": executable_orders[0]["limit_price"], "size": 1.0, "notional": 1.0},
                timeout_seconds=3.0,
                retry_attempts=1,
            )
            health_future = pool.submit(lambda: json.loads(urllib.request.urlopen(GATEWAY + "/health", timeout=3.0).read().decode("utf-8")))
            health = guard_future.result()
            health_payload = health_future.result()
        # BUY guard evidence intentionally does not authorize orders; the
        # actual submission gate is the local gateway health + pause boundary.
        result["gateway_health"] = {
            key: health_payload.get(key)
            for key in ("ready", "order_submission_enabled", "live_pause_present", "read_only", "proxy")
        }
        result["guard_evidence"] = {key: health.get(key) for key in ("ok", "evidence_only", "authorizes_order", "evidence_errors")}
        if health_payload.get("ready") is not True or health_payload.get("order_submission_enabled") is not True:
            result["blocked"] = "settlement_floor_gateway_not_authorized"
            result["timings_ms"]["submit_ms"] = _elapsed_ms(submit_started)
            result["timings_ms"]["total_ms"] = _elapsed_ms(overall_started)
            print(_json(result))
            return 3
        if health.get("evidence_only") is not True or health.get("authorizes_order") is not False:
            result["blocked"] = "settlement_floor_quote_boundary_invalid"
            result["timings_ms"]["submit_ms"] = _elapsed_ms(submit_started)
            result["timings_ms"]["total_ms"] = _elapsed_ms(overall_started)
            print(_json(result))
            return 3
    else:
        result["gateway_health"] = {"skipped": "no_new_orders"}
        result["guard_evidence"] = {"ok": True, "evidence_only": True, "authorizes_order": False, "evidence_errors": []}
    fills: list[dict[str, Any]] = []
    pending_specs: list[dict[str, Any]] = []
    pending_order_rows: list[dict[str, Any]] = []
    blocked = False

    def submit_order(order: Mapping[str, Any]) -> dict[str, Any]:
        order_type = str(order.get("order_type") or "FAK").upper()
        payload: dict[str, Any] = {
            "schema": "settlement_floor_buy_intent_v1",
            "strategy_id": STRATEGY_ID,
            "strategy_source": STRATEGY_ID,
            "order_execution_domain": "settlement_floor_basket",
            "order_execution_source": "manual_settlement_floor_topup",
            "order_execution_reason": "explicit_user_topup_request",
            "order_intent_action": "entry",
            "slug": slug,
            "event_date": args.event_date,
            "city": args.city,
            "bucket": order["bucket"],
            "outcome": order["bucket"],
            "outcome_side": "YES",
            "token_id": order["token_id"],
            "side": "BUY",
            "price": order["limit_price"],
            "tick_size": order["tick_size"],
            "order_type": order_type,
            "post_only": False,
            "direct_target_order_context": False,
            "idempotency_key": order["idempotency_key"],
            "order_intent_id": order["order_intent_id"],
        }
        if order_type == "FAK":
            payload.update({
                # Include the planned share size so the gateway can use the
                # price-protected limit-FAK path.  The old amount-only FAK
                # path calls the SDK market-info HTTP request, which is
                # unreliable through the local proxy.  The limit price is
                # taken from the same official WS depth slice, so this still
                # has protected market semantics and cannot cross above the
                # planned last ask.
                "amount": order["amount_usdc"],
                "size": order["requested_shares"],
                "allow_market": True,
            })
        else:
            payload.update({
                "size": order["requested_shares"],
                "allow_market": False,
            })
        return _post_local("/place-order", payload)

    terminal_fak_statuses = {
        "matched", "filled", "executed", "partial", "partially_filled",
        "cancelled", "canceled", "expired",
    }
    accepted_gtc_statuses = {
        "matched", "filled", "executed", "partial", "partially_filled",
        "live", "open", "submitted", "accepted",
    }

    for order in executable_orders:
        order_type = str(order.get("order_type") or "FAK").upper()
        response = submit_order(order)
        row = {**order, "response": response}
        fills.append(row)
        if response.get("reconcile_required") is True:
            result["blocked"] = "settlement_floor_order_reconcile_required"
            blocked = True
            break
        if response.get("ok") is not True:
            result["blocked"] = "settlement_floor_order_submit_failed"
            blocked = True
            break
        matched = _finite(response.get("size_matched"))
        status = str(response.get("status") or response.get("exchange_response", {}).get("status") or "").lower()
        if order_type == "FAK":
            if matched is None or matched < -FILL_ROUNDING_TOLERANCE_SHARES or status not in terminal_fak_statuses:
                result["blocked"] = "settlement_floor_order_fill_state_unknown"
                blocked = True
                break
            if matched > float(order["requested_shares"]) + FILL_ROUNDING_TOLERANCE_SHARES:
                # A protected USDC-denominated FAK can legitimately return a
                # slightly larger takingAmount after the exchange converts
                # the rounded amount into shares.  The spend cap, not the
                # rounded share request, is the safety boundary.  Accept the
                # fill only when the exchange reports the actual makingAmount
                # and it remains within the submitted amount; otherwise the
                # fill is not safely reconcilable.
                exchange_response = response.get("exchange_response")
                actual_cost = _finite(
                    response.get("makingAmount")
                    if isinstance(response, Mapping)
                    else None
                )
                if actual_cost is None and isinstance(exchange_response, Mapping):
                    actual_cost = _finite(exchange_response.get("makingAmount"))
                amount_cap = _finite(order.get("amount_usdc"))
                if (
                    actual_cost is None
                    or amount_cap is None
                    or actual_cost > amount_cap + 0.011
                ):
                    result["blocked"] = "settlement_floor_order_fill_exceeds_plan"
                    blocked = True
                    break
                row["actual_size_matched"] = matched
                row["actual_cost_usdc"] = actual_cost
            residual = max(0.0, float(order["target_new_shares"]) - max(0.0, matched))
            if residual > FILL_ROUNDING_TOLERANCE_SHARES:
                # The visible book may disappear between planning and the
                # exchange match.  In the default staged mode, an unexpected
                # FAK partial fill must therefore fall through to the same
                # protected GTC remainder path even when the plan originally
                # estimated enough visible depth.
                if not args.staged:
                    result["blocked"] = "settlement_floor_order_partial_fill"
                    blocked = True
                    break
                limit = _finite(order.get("gtc_limit_price")) or _finite(order.get("limit_price"))
                if limit is None or not 0.0 < limit < 1.0:
                    result["blocked"] = "settlement_floor_staged_gtc_limit_missing"
                    blocked = True
                    break
                requested = _rounded_submit_shares(residual)
                token = str(order["token_id"])
                idem = _order_identity(
                    slug=slug,
                    event_date=args.event_date,
                    city=args.city,
                    bucket=str(order["bucket"]),
                    token=token,
                    order_type="GTC",
                    price=limit,
                    shares=requested,
                    target_shares=float(order["target_new_shares"]),
                )
                pending_specs.append({
                    **order,
                    "order_type": "GTC",
                    "planned_shares": residual,
                    "requested_shares": requested,
                    "estimated_cost_usdc": residual * limit,
                    "amount_usdc": None,
                    "limit_price": limit,
                    "idempotency_key": idem,
                    "order_intent_id": idem,
                })
        else:
            if status not in accepted_gtc_statuses:
                result["blocked"] = "settlement_floor_gtc_acceptance_unknown"
                blocked = True
                break
            if status in {"matched", "filled", "executed"}:
                if matched is None:
                    result["blocked"] = "settlement_floor_gtc_fill_state_unknown"
                    blocked = True
                    break
                if matched + FILL_ROUNDING_TOLERANCE_SHARES < float(order["requested_shares"]):
                    pending_order_rows.append(row)
            else:
                pending_order_rows.append(row)

    if not blocked and pending_specs:
        result["planned_gtc_orders"] = pending_specs
        for order in pending_specs:
            if str(order.get("token_id") or "") in existing_open_tokens:
                continue
            response = submit_order(order)
            row = {**order, "response": response}
            fills.append(row)
            if response.get("reconcile_required") is True:
                result["blocked"] = "settlement_floor_order_reconcile_required"
                blocked = True
                break
            if response.get("ok") is not True:
                result["blocked"] = "settlement_floor_gtc_submit_failed"
                blocked = True
                break
            status = str(response.get("status") or response.get("exchange_response", {}).get("status") or "").lower()
            if status not in accepted_gtc_statuses:
                result["blocked"] = "settlement_floor_gtc_acceptance_unknown"
                blocked = True
                break
            matched = _finite(response.get("size_matched"))
            if status in {"matched", "filled", "executed"}:
                if matched is None:
                    result["blocked"] = "settlement_floor_gtc_fill_state_unknown"
                    blocked = True
                    break
                if matched + FILL_ROUNDING_TOLERANCE_SHARES < float(order["requested_shares"]):
                    pending_order_rows.append(row)
            else:
                pending_order_rows.append(row)

    result["fills"] = fills
    result["pending_orders"] = pending_order_rows
    result["staged_waiting_existing_orders"] = bool(result["skipped_existing_open_orders"] and not blocked)
    result["staged_pending"] = bool(
        args.staged
        and (result["pending_orders"] or result["staged_waiting_existing_orders"])
        and not blocked
    )
    result["completed"] = bool(not blocked and not result["staged_pending"])
    result["timings_ms"]["submit_ms"] = _elapsed_ms(submit_started)
    result["timings_ms"]["total_ms"] = _elapsed_ms(overall_started)
    if result["staged_pending"]:
        result["status"] = "staged_pending_recompute_after_fill"
    elif result["completed"]:
        result["status"] = "completed"
    else:
        result["status"] = "blocked"
    print(_json(result))
    return 0 if not blocked else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(_json({"schema": "settlement_floor_execution_report_v1", "strategy_id": STRATEGY_ID, "status": "blocked", "reason": str(exc)}))
        raise SystemExit(5)
