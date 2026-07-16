"""Pure evaluator for a fresh, executable $0.90 top-YES sell quote."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "market_alpha_take_profit_quote_v1"
TARGET_PRICE = 0.90


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _dt(value: Any) -> datetime | None:
    raw = _text(value)
    if raw.isdigit():
        number = int(raw)
        try:
            return datetime.fromtimestamp(number / 1000.0 if number >= 10_000_000_000 else number, timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _base(value: Any, decimals: int) -> float | None:
    raw = _text(value)
    return int(raw) / float(10**decimals) if raw.isdigit() and 0 <= decimals <= 18 else None


def _allowance(value: Mapping[str, Any], decimals: int) -> tuple[float | None, float | None]:
    balance = _base(value.get("balance"), decimals)
    allowance = _base(value.get("allowance"), decimals)
    if allowance is None and isinstance(value.get("allowances"), Mapping):
        candidates = [_base(item, decimals) for item in value["allowances"].values()]
        candidates = [item for item in candidates if item is not None]
        allowance = max(candidates) if candidates else None
    return balance, allowance


def build_market_alpha_take_profit_quote(
    *, token_id: str, requested_size: float, raw_snapshot: Mapping[str, Any], evaluated_at: str,
    max_age_ms: int = 1_000,
) -> dict[str, Any]:
    evidence = dict(raw_snapshot) if isinstance(raw_snapshot, Mapping) else {}
    token, size = _text(token_id), _number(requested_size)
    now, observed = _dt(evaluated_at), _dt(evidence.get("observed_at"))
    book = evidence.get("order_book") if isinstance(evidence.get("order_book"), Mapping) else {}
    book_at = _dt(book.get("timestamp"))
    age = (now - observed).total_seconds() * 1000 if now and observed else None
    book_age = (now - book_at).total_seconds() * 1000 if now and book_at else None
    blockers = []
    if evidence.get("schema") != "clob_execution_guard_snapshot_v1" or evidence.get("ok") is not True:
        blockers.append("market_alpha_quote_snapshot_unavailable")
    if evidence.get("evidence_only") is not True or evidence.get("authorizes_order") is not False:
        blockers.append("market_alpha_quote_snapshot_boundary_invalid")
    if not token or _text(evidence.get("token_id")) != token or _text(evidence.get("side")).upper() != "SELL" or _text(book.get("asset_id")) != token:
        blockers.append("market_alpha_quote_token_or_side_mismatch")
    if size is None or size <= 0:
        blockers.append("market_alpha_quote_size_invalid")
    if age is None or age < 0 or age > max_age_ms or book_age is None or book_age < -250 or book_age > max_age_ms:
        blockers.append("market_alpha_quote_stale")
    levels = []
    for raw in book.get("bids") if isinstance(book.get("bids"), list) else []:
        if not isinstance(raw, Mapping):
            continue
        price, level_size = _number(raw.get("price")), _number(raw.get("size"))
        if price is not None and level_size is not None and 0 < price < 1 and level_size > 0:
            levels.append((price, level_size))
    levels.sort(reverse=True)
    eligible = [(price, level_size) for price, level_size in levels if price + 1e-12 >= TARGET_PRICE]
    executable = sum(level_size for _price, level_size in eligible)
    remaining = float(size or 0.0)
    marginal = None
    for price, level_size in eligible:
        if remaining <= level_size + 1e-12:
            marginal = price
            remaining = 0.0
            break
        remaining -= level_size
    if marginal is None or executable + 1e-9 < float(size or 0.0):
        blockers.append("market_alpha_take_profit_depth_not_executable")
    decimals = evidence.get("amount_decimals") if isinstance(evidence.get("amount_decimals"), int) else -1
    balance, allowance = _allowance(
        evidence.get("conditional_balance_allowance") if isinstance(evidence.get("conditional_balance_allowance"), Mapping) else {},
        int(decimals),
    )
    if balance is None or allowance is None or min(balance, allowance) + 1e-9 < float(size or 0.0):
        blockers.append("market_alpha_take_profit_balance_or_allowance_insufficient")
    core = {
        "token_id": token,
        "requested_size": size,
        "price_for_requested_size": marginal,
        "as_of": _text(evidence.get("observed_at")),
        "book_timestamp": _text(book.get("timestamp")),
    }
    snapshot_id = "marketalphaquote_" + hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    expiry = observed + timedelta(milliseconds=900) if observed else None
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "available" if not blockers else "blocked",
        "block_reasons": sorted(set(blockers)),
        "market_snapshot_id": snapshot_id,
        **core,
        "expires_at": expiry.isoformat(timespec="milliseconds").replace("+00:00", "Z") if expiry else "",
        "frozen_limit_price": TARGET_PRICE,
        "executable_depth_at_or_above_target": executable,
        "conditional_balance": balance,
        "conditional_allowance": allowance,
        "evidence_only": True,
        "authorizes_order": False,
        "ev_edge_lcb_consumed": False,
    }


__all__ = ["SCHEMA_VERSION", "TARGET_PRICE", "build_market_alpha_take_profit_quote"]
