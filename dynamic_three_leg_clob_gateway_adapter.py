"""Local-only CLOB adapter and independent pre-submit evidence evaluator.

The Node gateway returns raw exchange evidence.  This module alone evaluates
whether the frozen dynamic-three-leg intent is still executable.  It never
reprices, retries a POST, reads research evidence, or creates a new intent.
"""

from __future__ import annotations

import ipaddress
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from standing_exit_999_runtime import tick_supports_0999


SCHEMA_VERSION = "dynamic_three_leg_clob_gateway_adapter_v1"
GUARD_SCHEMA_VERSION = "dynamic_three_leg_execution_guard_v1"
SNAPSHOT_SCHEMA_VERSION = "dynamic_three_leg_exchange_snapshot_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _parse_exchange_time(value: Any) -> datetime | None:
    raw = _text(value)
    if raw.isdigit():
        numeric = int(raw)
        seconds = numeric / 1000.0 if numeric >= 10_000_000_000 else float(numeric)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return _parse_utc(raw)


def _base_units(value: Any, decimals: int) -> float | None:
    # The CLOB balance API returns integer strings in base units.  Reject
    # ambiguous floats instead of guessing whether they are already scaled.
    raw = _text(value)
    if not raw or not raw.isdigit() or not 0 <= decimals <= 18:
        return None
    return int(raw) / float(10**decimals)


def _book_levels(value: Any) -> list[tuple[float, float]]:
    rows = value if isinstance(value, list) else []
    levels: list[tuple[float, float]] = []
    for raw in rows:
        row = _mapping(raw)
        price, size = _finite(row.get("price")), _finite(row.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            levels.append((price, size))
    return levels


def _available(balance: Mapping[str, Any], decimals: int) -> tuple[float | None, float | None, float | None]:
    current = _base_units(balance.get("balance"), decimals)
    allowance = _base_units(balance.get("allowance"), decimals)
    if allowance is None and isinstance(balance.get("allowances"), Mapping):
        candidates = [_base_units(value, decimals) for value in balance["allowances"].values()]
        candidates = [value for value in candidates if value is not None]
        allowance = max(candidates) if candidates else None
    usable = min(current, allowance) if current is not None and allowance is not None else None
    return current, allowance, usable


def evaluate_execution_guard_snapshot(
    intent: Mapping[str, Any], snapshot: Mapping[str, Any], *, evaluated_at: str,
    max_snapshot_age_ms: int = 1_000, position_tolerance: float = 1e-6,
) -> dict[str, Any]:
    """Evaluate raw CLOB evidence without trusting a gateway authorization bit."""

    row = _mapping(intent)
    evidence = _mapping(snapshot)
    payload = _mapping(json.loads(_text(row.get("intent_json")) or "{}")) if row.get("intent_json") else row
    token_id = _text(row.get("token_id") or payload.get("token_id"))
    side = _text(row.get("order_action") or payload.get("side")).upper()
    limit_price = _finite(row.get("limit_price") if row.get("limit_price") is not None else payload.get("price"))
    size = _finite(row.get("size") if row.get("size") is not None else payload.get("size"))
    notional = _finite(row.get("notional") if row.get("notional") is not None else payload.get("notional"))
    expected_balance = _finite(payload.get("expected_token_balance"))
    standing_exit_coverage_order = payload.get("standing_exit_coverage_order") is True
    observed_at = _parse_utc(evidence.get("observed_at"))
    now = _parse_utc(evaluated_at)
    age_ms = (now - observed_at).total_seconds() * 1000 if now is not None and observed_at is not None else None
    decimals = int(evidence.get("amount_decimals")) if isinstance(evidence.get("amount_decimals"), int) else -1
    book = _mapping(evidence.get("order_book"))
    book_at = _parse_exchange_time(book.get("timestamp"))
    book_age_ms = (now - book_at).total_seconds() * 1000 if now is not None and book_at is not None else None
    asks, bids = _book_levels(book.get("asks")), _book_levels(book.get("bids"))
    conditional_balance, conditional_allowance, conditional_usable = _available(
        _mapping(evidence.get("conditional_balance_allowance")), decimals
    )
    collateral_balance, collateral_allowance, collateral_usable = _available(
        _mapping(evidence.get("collateral_balance_allowance")), decimals
    )
    blockers: list[str] = []
    if evidence.get("schema") != "clob_execution_guard_snapshot_v1" or evidence.get("ok") is not True:
        blockers.append("clob_execution_guard_snapshot_unavailable")
    if evidence.get("authorizes_order") is not False or evidence.get("evidence_only") is not True:
        blockers.append("clob_execution_guard_boundary_invalid")
    if _text(evidence.get("token_id")) != token_id or _text(evidence.get("side")).upper() != side:
        blockers.append("clob_execution_guard_identity_mismatch")
    if _text(book.get("asset_id")) != token_id:
        blockers.append("clob_execution_guard_order_book_token_mismatch")
    if age_ms is None or age_ms < 0 or age_ms > max(0, int(max_snapshot_age_ms)):
        blockers.append("clob_execution_guard_snapshot_stale")
    if book_age_ms is None or book_age_ms < -250 or book_age_ms > max(0, int(max_snapshot_age_ms)):
        blockers.append("clob_execution_guard_order_book_stale")
    if side not in {"BUY", "SELL"} or limit_price is None or size is None or size <= 0:
        blockers.append("clob_execution_guard_intent_terms_invalid")

    executable_depth = 0.0
    best_price = None
    if side == "BUY" and limit_price is not None:
        best_price = min((price for price, _size in asks), default=None)
        executable_depth = sum(level_size for price, level_size in asks if price <= limit_price + 1e-12)
        if best_price is None or best_price > limit_price + 1e-12 or executable_depth + 1e-9 < float(size or 0):
            blockers.append("clob_execution_guard_buy_quote_or_depth_not_executable")
    elif side == "SELL" and limit_price is not None:
        best_price = max((price for price, _size in bids), default=None)
        executable_depth = sum(level_size for price, level_size in bids if price + 1e-12 >= limit_price)
        if not standing_exit_coverage_order and (best_price is None or best_price + 1e-12 < limit_price or executable_depth + 1e-9 < float(size or 0)):
            blockers.append("clob_execution_guard_sell_quote_or_depth_not_executable")
        if standing_exit_coverage_order:
            metadata = _mapping(evidence.get("token_metadata"))
            if abs(float(limit_price) - 0.999) > 1e-12 or not tick_supports_0999(metadata.get("tick_size")):
                blockers.append("clob_execution_guard_standing_999_tick_unsupported")

    # BUY does not consume an existing conditional position.  Its duplicate
    # and cap protection is the durable intent/risk ledger; requiring a fresh
    # conditional balance here adds no safety and creates an avoidable network
    # dependency.  SELL must still reconcile the exact managed lot balance.
    position_current = (
        expected_balance is not None and expected_balance >= 0
        if side == "BUY"
        else expected_balance is not None
        and conditional_balance is not None
        and abs(conditional_balance - expected_balance) <= position_tolerance
    )
    if not position_current:
        blockers.append("clob_execution_guard_position_snapshot_drift")
    if side == "BUY":
        balance_sufficient = notional is not None and collateral_usable is not None and collateral_usable + 1e-9 >= notional
    else:
        balance_sufficient = conditional_usable is not None and size is not None and conditional_usable + 1e-9 >= size
    if not balance_sufficient:
        blockers.append("clob_execution_guard_balance_or_allowance_insufficient")
    return {
        "schema": GUARD_SCHEMA_VERSION,
        "ok": not blockers,
        "position_lot_current": position_current,
        "market_quote_current": not any("quote_or_depth" in item for item in blockers),
        "balance_sufficient": balance_sufficient,
        "block_reasons": sorted(set(blockers)),
        "token_id": token_id,
        "side": side,
        "limit_price": limit_price,
        "size": size,
        "notional": notional,
        "expected_token_balance": expected_balance,
        "standing_exit_coverage_order": standing_exit_coverage_order,
        "conditional_balance": conditional_balance,
        "conditional_allowance": conditional_allowance,
        "conditional_usable": conditional_usable,
        "collateral_balance": collateral_balance,
        "collateral_allowance": collateral_allowance,
        "collateral_usable": collateral_usable,
        "best_executable_price": best_price,
        "executable_depth": executable_depth,
        "snapshot_age_ms": age_ms,
        "order_book_age_ms": book_age_ms,
        "gateway_authorization_trusted": False,
        "ev_edge_lcb_consumed": False,
    }


def evaluate_dynamic_three_leg_exchange_snapshot(
    request: Mapping[str, Any], snapshot: Mapping[str, Any], *, evaluated_at: str,
    max_snapshot_age_ms: int = 1_000,
) -> dict[str, Any]:
    """Validate raw multi-token CLOB evidence and derive frozen quote rows."""

    requested = _mapping(request)
    evidence = _mapping(snapshot)
    now = _parse_utc(evaluated_at)
    observed = _parse_utc(evidence.get("observed_at"))
    age_ms = (now - observed).total_seconds() * 1000 if now is not None and observed is not None else None
    blockers: list[str] = []
    if evidence.get("schema") != "clob_dynamic_three_leg_snapshot_v1" or evidence.get("ok") is not True:
        blockers.append("dynamic_three_leg_exchange_snapshot_unavailable")
    if evidence.get("evidence_only") is not True or evidence.get("authorizes_order") is not False:
        blockers.append("dynamic_three_leg_exchange_snapshot_boundary_invalid")
    if _text(evidence.get("request_id")) != _text(requested.get("request_id")):
        blockers.append("dynamic_three_leg_exchange_snapshot_request_identity_mismatch")
    if age_ms is None or age_ms < 0 or age_ms > max(0, int(max_snapshot_age_ms)):
        blockers.append("dynamic_three_leg_exchange_snapshot_stale")
    amount_decimals = evidence.get("amount_decimals")
    decimals = int(amount_decimals) if isinstance(amount_decimals, int) else -1
    books = _mapping(evidence.get("order_books_by_token"))
    top_quotes = _mapping(evidence.get("top_quotes_by_token"))
    token_metadata = _mapping(evidence.get("token_metadata_by_token"))
    requested_buckets = requested.get("buckets") if isinstance(requested.get("buckets"), list) else []
    buckets: dict[str, dict[str, Any]] = {}
    quote_diagnostics: list[dict[str, str]] = []
    maximum_book_age_ms = 0.0
    for raw in requested_buckets:
        row = _mapping(raw)
        bucket = _text(row.get("bucket"))
        yes_token, no_token = _text(row.get("yes_token_id")), _text(row.get("no_token_id"))
        yes_book, no_book = _mapping(books.get(yes_token)), _mapping(books.get(no_token))
        quote: dict[str, Any] = {"yes_token_id": yes_token, "no_token_id": no_token}
        for side, token_id, book in (("yes", yes_token, yes_book), ("no", no_token, no_book)):
            top_quote = _mapping(top_quotes.get(token_id))
            evidence_row = top_quote if top_quote else book
            if not bucket or not token_id or _text(evidence_row.get("asset_id")) != token_id:
                blockers.append("dynamic_three_leg_exchange_book_identity_mismatch")
                continue
            book_time = _parse_exchange_time(evidence_row.get("timestamp"))
            book_age_ms = (now - book_time).total_seconds() * 1000 if now is not None and book_time is not None else None
            if book_age_ms is None or book_age_ms < -250 or book_age_ms > max(0, int(max_snapshot_age_ms)):
                blockers.append("dynamic_three_leg_exchange_book_stale")
            else:
                maximum_book_age_ms = max(maximum_book_age_ms, book_age_ms)
            bids, asks = _book_levels(book.get("bids")), _book_levels(book.get("asks"))
            bid = _finite(top_quote.get("best_bid")) if top_quote else max((price for price, _size in bids), default=None)
            ask = _finite(top_quote.get("best_ask")) if top_quote else min((price for price, _size in asks), default=None)
            if bid is not None and not 0 < bid < 1:
                bid = None
            if ask is not None and not 0 < ask < 1:
                ask = None
            if bid is not None and ask is not None and bid > ask:
                blockers.append("dynamic_three_leg_exchange_crossed_quote")
            if bid is None or ask is None:
                quote_diagnostics.append({
                    "bucket": bucket,
                    "side": side.upper(),
                    "state": (
                        "quote_unavailable" if bid is None and ask is None
                        else "ask_missing" if ask is None
                        else "bid_missing"
                    ),
                })
            quote[f"{side}_bid"] = bid
            quote[f"{side}_ask"] = ask
            quote[f"{side}_bid_depth"] = None if top_quote else sum(size for price, size in bids if bid is not None and abs(price - bid) <= 1e-12)
            quote[f"{side}_ask_depth"] = None if top_quote else sum(size for price, size in asks if ask is not None and abs(price - ask) <= 1e-12)
            metadata = _mapping(token_metadata.get(token_id))
            quote[f"{side}_tick_size"] = _text(metadata.get("tick_size"))
            quote[f"{side}_neg_risk"] = metadata.get("neg_risk") if isinstance(metadata.get("neg_risk"), bool) else None
        if bucket:
            buckets[bucket] = quote
    if len(buckets) != len(requested_buckets):
        blockers.append("dynamic_three_leg_exchange_bucket_coverage_incomplete")

    collateral_balance, collateral_allowance, collateral_usable = _available(
        _mapping(evidence.get("collateral_balance_allowance")), decimals
    )
    if collateral_usable is None:
        blockers.append("dynamic_three_leg_exchange_collateral_unavailable")
    conditional: dict[str, dict[str, float | None]] = {}
    raw_conditionals = _mapping(evidence.get("conditional_balance_allowance_by_token"))
    position_tokens = requested.get("position_token_ids") if isinstance(requested.get("position_token_ids"), list) else []
    for raw_token in position_tokens:
        token_id = _text(raw_token)
        balance, allowance, usable = _available(_mapping(raw_conditionals.get(token_id)), decimals)
        if not token_id or balance is None or allowance is None or usable is None:
            blockers.append("dynamic_three_leg_exchange_position_balance_unavailable")
        conditional[token_id] = {"balance": balance, "allowance": allowance, "usable": usable}
    return {
        "schema": SNAPSHOT_SCHEMA_VERSION,
        "ok": not blockers,
        "state": "available" if not blockers else "blocked",
        "block_reasons": sorted(set(blockers)),
        "upstream_evidence_errors": sorted({
            _text(reason) for reason in evidence.get("evidence_errors") or [] if _text(reason)
        }),
        "request_id": _text(requested.get("request_id")),
        "as_of": _text(evidence.get("observed_at")),
        "snapshot_age_ms": age_ms,
        "maximum_book_age_ms": maximum_book_age_ms,
        "buckets": buckets,
        "quote_diagnostics": quote_diagnostics,
        "candidate_side_quote_validation_deferred_to_materializer": True,
        "collateral_balance": collateral_balance,
        "collateral_allowance": collateral_allowance,
        "available_cash_usdc": collateral_usable,
        "conditional_by_token": conditional,
        "evidence_only": True,
        "authorizes_order": False,
        "ev_edge_lcb_consumed": False,
    }


class LocalDynamicThreeLegClobGateway:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", *, timeout_seconds: float = 1.5) -> None:
        parsed = urlparse(base_url)
        host = parsed.hostname or ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if parsed.scheme != "http" or not loopback or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("dynamic_three_leg_clob_gateway_must_be_local_http")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(0.05, float(timeout_seconds))

    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            data=json.dumps(dict(payload), separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return dict(json.loads(response.read().decode("utf-8")))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"dynamic_three_leg_local_clob_gateway_error:{type(exc).__name__}") from exc

    def validate_execution_guard(self, intent: Mapping[str, Any], *, evaluated_at: str | None = None) -> dict[str, Any]:
        payload = _mapping(json.loads(_text(intent.get("intent_json")) or "{}"))
        snapshot = self._post("/execution-guard-snapshot", {
            "token_id": intent.get("token_id") or payload.get("token_id"),
            "side": intent.get("order_action") or payload.get("side"),
            "limit_price": intent.get("limit_price") or payload.get("price"),
            "size": intent.get("size") or payload.get("size"),
            "notional": intent.get("notional") or payload.get("notional"),
        })
        evaluated = evaluated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return evaluate_execution_guard_snapshot(intent, snapshot, evaluated_at=evaluated)

    def freeze_dynamic_three_leg_snapshot(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Fetch one raw, evidence-only multi-token snapshot; never authorize."""

        return self._post("/dynamic-three-leg-snapshot", request)

    def freeze_execution_guard_snapshot(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Return raw single-token exchange evidence without evaluating it."""

        return self._post("/execution-guard-snapshot", request)

    def place_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        # Deliberately one POST and no retry.  Any transport ambiguity is handed
        # to identity-only reconciliation by the resident submitter.
        return self._post("/place-order", payload)

    def lookup_order(self, lookup: Mapping[str, Any]) -> dict[str, Any]:
        return self._post("/reconcile-order", {"idempotency_key": lookup.get("idempotency_key")})

    def cancel_order(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Cancel one exact order identity; never broad-cancel or retry."""

        return self._post("/cancel-order", {
            "order_id": request.get("order_id") or request.get("orderID"),
            "idempotency_key": request.get("idempotency_key"),
        })

    def lookup_exchange_order(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._post("/lookup-exchange-order", {
            "order_id": request.get("order_id") or request.get("orderID") or request.get("exchange_order_id"),
        })


__all__ = [
    "GUARD_SCHEMA_VERSION", "LocalDynamicThreeLegClobGateway", "SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION", "evaluate_dynamic_three_leg_exchange_snapshot",
    "evaluate_execution_guard_snapshot",
]
