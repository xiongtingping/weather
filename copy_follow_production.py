"""Independent live executor for the public-wallet four-bucket copy line.

This module is deliberately separate from ``BasketProductionEngine``.  It
does not read posterior probabilities, regime, METAR/SPECI or any weather
evidence for execution.  It consumes only the wallet, market registry, and
official CLOB WebSocket evidence for order decisions.  Forecast fields, when
enabled, are display-only report context and cannot affect an order.

* the configured wallet's public BUY YES activity feed;
* the already materialized market registry for token identity; and
* the local official CLOB WebSocket quote shards for executable depth.

The source wallet is copied only while it has an exact four-bucket YES
selection with a materially funded largest bucket.  The first entry uses
protected FAK marketable orders; later synchronization uses independent GTC
YES limits at the current official bid ladder.  Repeated runs reconcile fills,
compare the source capital/share mix with the managed position, and submit
only the missing BUY/SELL delta.  Settlement-profit figures remain audit
outputs, but the active copy-follow mode does not use a settlement-floor gate.
An ambiguous gateway response stops the plan at ``reconcile_required``; no
blind retry is attempted.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from copy_follow_schema import apply_copy_follow_schema
from copy_follow_optional_forecasts import fetch_open_meteo_daily_peak
from copy_follow_official_taf import fetch_amsc_taf_peak, fetch_awc_taf_peak
from copy_follow_strategy import (
    CopyFollowCandidate,
    allocate_source_scaled_copy,
    allocate_settlement_floor,
    activity_identity,
    assess_source_building,
    compare_forecast_peak_to_bucket,
    event_slug_from_activity,
    extract_four_bucket_candidates,
    fetch_activity,
    fetch_trades,
    fetch_positions,
    parse_activity_bucket,
    parse_event_identity,
    extract_four_bucket_candidates_from_positions,
    position_rows_to_activity_rows,
    rank_recent_city_candidates,
    stable_id,
)
from dynamic_three_leg_clob_gateway_adapter import LocalDynamicThreeLegClobGateway


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "trading" / "copy_follow_v1.json"
DEFAULT_PAUSE_FILE = ROOT / "data" / "copy_follow_paused.flag"
DEFAULT_QUOTE_SNAPSHOT = ROOT / "data" / "market_ws_latest_quotes.json"
DEFAULT_QUOTE_SHARDS = ROOT / "data" / "market_ws_quote_shards"
DEFAULT_CITY_SELECTION_CACHE = ROOT / "data" / "copy_follow_city_selection_cache.json"
SCHEMA_VERSION = "weather_copy_follow_production_v1"
ACTIVE_PLAN_STATES = ("submitting", "submitted", "reconcile_required")
OPEN_ORDER_STATES = ("submitting", "open", "partially_filled", "cancel_submitting")
FILLABLE_ORDER_STATES = ("open", "partially_filled")
EXIT_LIQUIDITY_IDEMPOTENCY_PREFIX = "copy_exit_liquidity_idem_"
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _elapsed_ms(started: float) -> float:
    return round(max(0.0, time.perf_counter() - started) * 1000.0, 3)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    """Persist a non-authoritative copy-follow cache without partial writes."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _is_exit_liquidity_order(row: Mapping[str, Any]) -> bool:
    """Identify a standing exit order without changing the order schema."""

    return str(row.get("idempotency_key") or "").startswith(EXIT_LIQUIDITY_IDEMPOTENCY_PREFIX)


def _align_market_buy_quantity(price: float, quantity: float, *, minimum_notional_usdc: float = 1.0) -> float:
    """Return a four-decimal FAK size whose maker amount is cent-aligned."""

    limit = float(price)
    requested = float(quantity)
    if not 0 < limit < 1 or requested <= 0:
        raise ValueError("copy_follow_market_buy_terms_invalid")
    candidate = max(requested, float(minimum_notional_usdc) / limit)
    candidate = math.ceil(candidate * 10000.0 - 1e-12) / 10000.0
    for _ in range(100000):
        maker_cents = limit * candidate * 100.0
        if abs(maker_cents - round(maker_cents)) <= 1e-7 and maker_cents >= float(minimum_notional_usdc) * 100.0 - 1e-7:
            return round(candidate, 4)
        candidate = round(candidate + 0.0001, 4)
    raise ValueError("copy_follow_market_buy_maker_amount_alignment_unavailable")


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_seconds(value: Any) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def _local_entry_window_reason(
    event_date: str,
    timezone_name: str,
    *,
    cutoff_hour: float = 18.0,
    now: datetime | None = None,
) -> str | None:
    """Block same-day copy entry after the fixed local peak window.

    This is a clock/date safety rule only.  It deliberately does not inspect
    TAF, ECMWF, Open-Meteo, METAR, model or any other weather field.
    """

    try:
        target_date = date.fromisoformat(str(event_date).split("T", 1)[0])
        zone = ZoneInfo(str(timezone_name or "").strip())
        cutoff = float(cutoff_hour)
    except (TypeError, ValueError, KeyError):
        return "copy_follow_settlement_timezone_invalid"
    if not math.isfinite(cutoff) or not 0.0 <= cutoff <= 24.0:
        return "copy_follow_local_entry_cutoff_invalid"
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    if current.date() > target_date:
        return "copy_follow_local_event_date_expired"
    if current.date() == target_date:
        local_hour = current.hour + current.minute / 60.0 + current.second / 3600.0
        if local_hour >= cutoff:
            return "copy_follow_local_peak_window_passed"
    return None


def _forecast_notice_text(context: Mapping[str, Any], key: str) -> str:
    source = context.get(key) if isinstance(context.get(key), Mapping) else {}
    peak = _finite(source.get("peak_c"))
    comparison = source.get("comparison") if isinstance(source.get("comparison"), Mapping) else {}
    if peak is None:
        reasons = source.get("reasons") if isinstance(source.get("reasons"), list) else []
        return f"获取失败:{reasons[0]}" if reasons else "获取失败"
    market_value = comparison.get("forecast_peak_market_value")
    unit = str(comparison.get("market_unit") or "C")
    relation = str(comparison.get("relation") or "unavailable")
    label = str(source.get("source_label") or "").strip()
    prefix = f"{label}:" if label else ""
    return f"{prefix}{peak:g}°C/{market_value:g}°{unit}，{relation}" if isinstance(market_value, (int, float)) else f"{prefix}{peak:g}°C，{relation}"


def _hard_building_gate_reasons(building_gate: Mapping[str, Any]) -> list[str]:
    """Return only structural/core-bucket blockers.

    Source activity freshness is informational for copy-follow.  The exact
    four-bucket selection and materially funded core bucket remain hard
    requirements.  The source's percentage split is a sizing preference, not
    a reason to discard the source selection.
    """

    return [
        str(reason)
        for reason in building_gate.get("reason_codes") or []
        if str(reason)
        not in {
            "copy_follow_source_not_actively_building",
            "copy_follow_main_bucket_share_not_large_enough",
        }
    ]


def _copy_follow_hard_reason(reason: Any) -> bool:
    """Classify a reason without confusing missing quotes with no plan.

    Four-bucket identity and the core-bucket minimum are hard.  The settlement
    floor remains recognized only for the legacy opt-in mode.  Quote
    freshness/depth, ask-sum, budget headroom and source activity are execution
    diagnostics: they may stop an order this cycle, but the source selection
    must still be reported and retained as a plan.
    """

    code = str(reason or "")
    if code in {
        "copy_follow_four_bucket_identity_invalid",
        "copy_follow_four_bucket_selection_not_formed",
        "copy_follow_main_bucket_notional_not_large_enough",
        "copy_follow_primary_secondary_invalid",
        "copy_follow_edge_bucket_identity_invalid",
        "copy_follow_local_peak_window_passed",
        "copy_follow_local_event_date_expired",
    }:
        return True
    if code.startswith("copy_follow_settlement_floor"):
        return True
    if code.startswith("copy_follow_floor_solver"):
        return True
    if code.startswith("copy_follow_rebalance_settlement_floor"):
        return True
    if code.startswith("copy_follow_reduction_breaks_settlement_floor"):
        return True
    if code.startswith("copy_follow_all_in_yes_ask_sum"):
        # This is only a hard reason when the floor solver cannot certify the
        # basket; the sum itself is never independently used as a gate.
        return True
    if code.startswith("copy_follow_primary_secondary_order"):
        return True
    return False


def _is_settlement_floor_reason(reason: Any) -> bool:
    """Return whether a reason means the basket floor is unproven/violated."""

    code = str(reason or "")
    return (
        code.startswith("copy_follow_settlement_floor")
        or code.startswith("copy_follow_floor_solver")
        or code.startswith("copy_follow_rebalance_settlement_floor")
        or code.startswith("copy_follow_reduction_breaks_settlement_floor")
    )


def _copy_follow_plan_summary(
    candidate: CopyFollowCandidate,
    *,
    primary_bucket: str,
    secondary_bucket: str,
    source_values: Mapping[str, float],
    forecast_context: Mapping[str, Any],
    building_gate: Mapping[str, Any],
    config: Mapping[str, Any],
    reason_codes: Sequence[str],
    execution_status: str,
    quote_status_by_bucket: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a reportable four-bucket plan even when execution evidence waits."""

    execution = config.get("execution") if isinstance(config.get("execution"), Mapping) else {}
    return {
        "state": "planned",
        "execution_ready": False,
        "execution_status": execution_status,
        "strategy_id": "weather_copy_follow_v1",
        "plan_id": candidate.plan_id,
        "event_slug": candidate.event_slug,
        "event_date": candidate.event_date,
        "city_slug": candidate.city_slug,
        "main_bucket": primary_bucket,
        "secondary_bucket": secondary_bucket,
        "selected_buckets": list(candidate.buckets),
        "source_notional_by_bucket": dict(source_values),
        "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket,
        "source_reference_total_usdc": candidate.total_allocation_usdc,
        "building_gate": dict(building_gate),
        "forecast_context": dict(forecast_context),
        "reason_codes": list(reason_codes),
        "quote_status_by_bucket": dict(quote_status_by_bucket or {}),
        "settlement_floor_hard_gate": bool(execution.get("settlement_floor_hard_gate", False)),
        "safety_floor_usdc": float(execution.get("safety_floor_usdc", 0.01) or 0.01),
        "minimum_settlement_profit_usdc": None,
        "maximum_settlement_profit_usdc": None,
        "best_ask_sum": None,
        "execution": {
            "order_type": "GTC",
            "all_legs_required": False,
            "buy_price_mode": "best_bid",
            "sell_price_mode": "best_bid",
            "initial_entry_order_type": "FAK",
            "initial_entry_price_mode": "market",
            "source": "official_polymarket_clob_websocket",
            "uses_bayes": False,
            "uses_regime": False,
            "uses_weather_fields": False,
            "forecast_context_display_only": True,
        },
    }


def _copy_follow_report_visible(result: Mapping[str, Any]) -> bool:
    """Hide cities whose local peak/entry window has already passed."""

    reasons = {str(item) for item in result.get("reason_codes") or []}
    return not reasons.intersection(
        {
            "copy_follow_local_peak_window_passed",
            "copy_follow_local_event_date_expired",
        }
    )


def _city_budget_cap(config: Mapping[str, Any], candidate: CopyFollowCandidate) -> float:
    """Return the configured cap for one city without widening other cities."""

    try:
        base = float(config.get("city_cap_usdc") or 30.0)
    except (TypeError, ValueError):
        base = 30.0
    overrides = config.get("city_budget_overrides_usdc") if isinstance(config.get("city_budget_overrides_usdc"), Mapping) else {}
    raw = overrides.get(candidate.city_slug)
    try:
        override = float(raw)
    except (TypeError, ValueError):
        override = None
    if override is None or not math.isfinite(override) or override <= 0:
        return base
    return override


def _config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    value = _read_json(path)
    if value.get("schema") != "weather_copy_follow_config_v1":
        raise ValueError("copy_follow_config_schema_invalid")
    if value.get("strategy_id") != "weather_copy_follow_v1":
        raise ValueError("copy_follow_config_strategy_invalid")
    if value.get("state") not in {"disabled", "enabled"}:
        raise ValueError("copy_follow_config_state_invalid")
    if not isinstance(value.get("source"), Mapping):
        raise ValueError("copy_follow_config_source_missing")
    source = value["source"]
    if not str(source.get("user") or "").strip():
        raise ValueError("copy_follow_config_source_user_missing")
    if int(value.get("required_bucket_count") or 0) != 4:
        raise ValueError("copy_follow_config_bucket_count_invalid")
    if float(value.get("city_cap_usdc") or 0) <= 0:
        raise ValueError("copy_follow_config_city_cap_invalid")
    city_selection = value.get("city_selection") if isinstance(value.get("city_selection"), Mapping) else {}
    if city_selection.get("enabled", False) not in {True, False}:
        raise ValueError("copy_follow_config_city_selection_policy_invalid")
    if city_selection.get("enabled") is True:
        try:
            max_cities = int(city_selection.get("max_cities", 5) or 5)
            window_days = int(city_selection.get("window_days", 7) or 7)
            refresh_sec = float(city_selection.get("refresh_sec", 300.0) or 300.0)
        except (TypeError, ValueError):
            raise ValueError("copy_follow_config_city_selection_parameters_invalid")
        if not 1 <= max_cities <= 20 or not 1 <= window_days <= 30 or not math.isfinite(refresh_sec) or refresh_sec < 30.0:
            raise ValueError("copy_follow_config_city_selection_parameters_invalid")
        selected_cities = city_selection.get("selected_cities", [])
        if not isinstance(selected_cities, list) or any(not str(city).strip() for city in selected_cities):
            raise ValueError("copy_follow_config_city_selection_cities_invalid")
        pinned_cities = city_selection.get("pinned_cities", [])
        if not isinstance(pinned_cities, list) or any(not str(city).strip() for city in pinned_cities):
            raise ValueError("copy_follow_config_city_selection_pinned_cities_invalid")
    execution = value.get("execution") if isinstance(value.get("execution"), Mapping) else {}
    if execution.get("order_type") != "GTC" or execution.get("all_legs_required") is not False:
        raise ValueError("copy_follow_config_dynamic_order_policy_invalid")
    if str(execution.get("buy_price_mode") or "best_bid") != "best_bid":
        raise ValueError("copy_follow_config_buy_price_mode_invalid")
    if str(execution.get("sell_price_mode") or "best_bid") != "best_bid":
        raise ValueError("copy_follow_config_sell_price_mode_invalid")
    if str(execution.get("initial_entry_order_type") or "FAK") != "FAK":
        raise ValueError("copy_follow_config_initial_entry_order_type_invalid")
    if str(execution.get("initial_entry_price_mode") or "market") != "market":
        raise ValueError("copy_follow_config_initial_entry_price_mode_invalid")
    if execution.get("settlement_floor_hard_gate", False) not in {True, False}:
        raise ValueError("copy_follow_config_settlement_floor_hard_gate_invalid")
    copy_weight_mode = str(
        execution.get("copy_weight_mode")
        or "source_capital_scale_and_share_ratio"
    )
    if copy_weight_mode not in {
        "source_capital_scale_and_share_ratio",
        "source_ratio_preference_with_settlement_floor",
    }:
        raise ValueError("copy_follow_config_copy_weight_mode_invalid")
    if execution.get("allow_reductions", True) not in {True, False}:
        raise ValueError("copy_follow_config_reductions_policy_invalid")
    if execution.get("enforce_city_cap", False) not in {True, False}:
        raise ValueError("copy_follow_config_city_cap_policy_invalid")
    exit_liquidity = execution.get("exit_liquidity") if isinstance(execution.get("exit_liquidity"), Mapping) else {}
    if exit_liquidity.get("enabled", False) not in {True, False}:
        raise ValueError("copy_follow_config_exit_liquidity_policy_invalid")
    if exit_liquidity.get("enabled") is True:
        exit_price = _finite(exit_liquidity.get("price", 0.997))
        if exit_price is None or not 0 < exit_price < 1:
            raise ValueError("copy_follow_config_exit_liquidity_price_invalid")
        if str(exit_liquidity.get("order_type") or "GTC") != "GTC":
            raise ValueError("copy_follow_config_exit_liquidity_order_type_invalid")
    buy_ladder = execution.get("buy_ladder") if isinstance(execution.get("buy_ladder"), Mapping) else {}
    if buy_ladder.get("enabled") is True:
        try:
            ladder_levels = int(buy_ladder.get("max_levels", 3) or 3)
        except (TypeError, ValueError):
            raise ValueError("copy_follow_config_buy_ladder_levels_invalid")
        if not 1 <= ladder_levels <= 5:
            raise ValueError("copy_follow_config_buy_ladder_levels_invalid")
        weights = buy_ladder.get("allocation_weights")
        if not isinstance(weights, (list, tuple)) or not weights:
            raise ValueError("copy_follow_config_buy_ladder_weights_invalid")
        parsed_weights = [_finite(value) for value in weights[:ladder_levels]]
        if not parsed_weights or any(value is None or value <= 0 for value in parsed_weights):
            raise ValueError("copy_follow_config_buy_ladder_weights_invalid")
    heavy_bucket_fallback = (
        execution.get("heavy_bucket_best_bid_fallback")
        if isinstance(execution.get("heavy_bucket_best_bid_fallback"), Mapping)
        else {}
    )
    if heavy_bucket_fallback.get("enabled", True) not in {True, False}:
        raise ValueError("copy_follow_config_heavy_bucket_best_bid_fallback_invalid")
    fallback_after_sec = _finite(heavy_bucket_fallback.get("after_sec", 6.0))
    if fallback_after_sec is None or fallback_after_sec < 1.0:
        raise ValueError("copy_follow_config_heavy_bucket_best_bid_fallback_delay_invalid")
    cutoff_hour = _finite(execution.get("local_entry_cutoff_hour", 18.0))
    if cutoff_hour is None or not 0.0 <= cutoff_hour <= 24.0:
        raise ValueError("copy_follow_local_entry_cutoff_invalid")
    forecast = value.get("forecast") if isinstance(value.get("forecast"), Mapping) else {}
    open_meteo = forecast.get("open_meteo") if isinstance(forecast.get("open_meteo"), Mapping) else {}
    if open_meteo.get("enabled") is True and open_meteo.get("display_only") is not True:
        raise ValueError("copy_follow_open_meteo_must_be_display_only")
    awc_taf = forecast.get("awc_taf") if isinstance(forecast.get("awc_taf"), Mapping) else {}
    if awc_taf.get("enabled") is True and awc_taf.get("display_only") is not True:
        raise ValueError("copy_follow_awc_taf_must_be_display_only")
    return value


def _quote_paths() -> tuple[Path, Path]:
    snapshot = Path(os.environ.get("PM_MARKET_WS_LATEST_QUOTES_PATH", str(DEFAULT_QUOTE_SNAPSHOT)))
    shards = Path(os.environ.get("PM_MARKET_WS_QUOTE_SHARD_DIR", str(DEFAULT_QUOTE_SHARDS)))
    return snapshot, shards


def _quote_book(token_id: str, *, max_age_sec: float) -> dict[str, Any] | None:
    token = str(token_id or "").strip()
    if not token:
        return None
    snapshot_path, shard_dir = _quote_paths()
    shard_path = shard_dir / (hashlib.sha256(token.encode("utf-8")).hexdigest() + ".json")
    value = _read_json(shard_path) if shard_path.exists() else {}
    root_snapshot = _read_json(snapshot_path)
    if value.get("schema") != "clob_ws_token_quote_snapshot_v1":
        value = root_snapshot.get("quotes_by_token", {}).get(token, {}) if isinstance(root_snapshot.get("quotes_by_token"), Mapping) else {}
        # The global manifest has a different schema; it is acceptable as a
        # fallback only when the token payload still carries official fields.
    quote = value.get("quote") if isinstance(value.get("quote"), Mapping) else value
    if not isinstance(quote, Mapping):
        return None
    if str(value.get("source") or "official_polymarket_clob_websocket") != "official_polymarket_clob_websocket":
        return None
    if value.get("evidence_only", True) is not True or value.get("authorizes_order", False) is not False:
        return None
    if str(value.get("token_id") or token) != token:
        return None
    # A reconnect stages a new token file before its root manifest is
    # committed.  The active flag and generation fence prevent readers from
    # mixing that provisional file with the previous committed generation.
    if value.get("schema") == "clob_ws_token_quote_snapshot_v1" and value.get("shard_active") is not True:
        return None
    observed = quote.get("book_observed_at") or quote.get("created_at")
    # An unchanged book remains valid while its official WS shard is active.
    # The prior check only used the last price/book event, so quiet markets
    # were incorrectly reported as missing after five seconds.  The manifest
    # is evidence-only and must prove both the official source and active
    # shard before its connection timestamp can refresh quote freshness.
    active_shards = root_snapshot.get("active_shards") if isinstance(root_snapshot.get("active_shards"), list) else []
    shard_index = value.get("shard_index")
    if (
        root_snapshot.get("schema") == "clob_ws_latest_quote_snapshot_v1"
        and root_snapshot.get("source") == "official_polymarket_clob_websocket"
        and root_snapshot.get("evidence_only", True) is True
        and root_snapshot.get("authorizes_order", False) is False
        and isinstance(shard_index, int)
        and shard_index in {int(item) for item in active_shards if str(item).lstrip("-").isdigit()}
    ):
        active_generations = root_snapshot.get("active_shard_generations")
        if isinstance(active_generations, Mapping) and shard_index in {
            int(key) for key in active_generations if str(key).lstrip("-").isdigit()
        }:
            try:
                expected_generation = int(
                    active_generations.get(str(shard_index), active_generations.get(shard_index))
                )
                actual_generation = value.get("shard_generation")
                if not (
                    actual_generation is None and expected_generation == 0
                ) and int(actual_generation) != expected_generation:
                    return None
            except (TypeError, ValueError):
                return None
        connection_observed = root_snapshot.get("connection_observed_at")
        per_shard_observed = root_snapshot.get("active_shard_connection_observed_at")
        if isinstance(per_shard_observed, Mapping):
            connection_observed = per_shard_observed.get(
                str(shard_index), per_shard_observed.get(shard_index, connection_observed)
            )
        if _parse_time(connection_observed) is not None and (
            _parse_time(observed) is None
            or _parse_time(connection_observed) > _parse_time(observed)
        ):
            observed = connection_observed
    age = _age_seconds(observed)
    if age is None or age < -0.5 or age > float(max_age_sec):
        return None
    bids: list[tuple[float, float]] = []
    for raw in quote.get("bids") or []:
        if not isinstance(raw, Mapping):
            continue
        price, size = _finite(raw.get("price")), _finite(raw.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            bids.append((price, size))
    bids.sort(key=lambda item: item[0], reverse=True)
    asks: list[tuple[float, float]] = []
    for raw in quote.get("asks") or []:
        if not isinstance(raw, Mapping):
            continue
        price, size = _finite(raw.get("price")), _finite(raw.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            asks.append((price, size))
    asks.sort(key=lambda item: item[0])
    if not asks and not bids:
        return None
    return {
        "token_id": token,
        "bids": bids,
        "asks": asks,
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "observed_at": observed,
        "tick_size": quote.get("tick_size") or value.get("tick_size"),
        "neg_risk": quote.get("neg_risk") if isinstance(quote.get("neg_risk"), bool) else value.get("neg_risk"),
    }


def _ws_messages(raw: Any) -> list[Mapping[str, Any]]:
    """Decode the small set of official CLOB WS envelope shapes."""

    values: list[Any]
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            return []
    else:
        values = raw
    if isinstance(values, Mapping):
        values = [values]
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, Mapping)]


def _ws_levels(value: Any, *, reverse: bool) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    if isinstance(value, Mapping):
        source = [{"price": price, "size": size} for price, size in value.items()]
    else:
        source = value if isinstance(value, list) else []
    for raw in source:
        if isinstance(raw, Mapping):
            price, size = _finite(raw.get("price")), _finite(raw.get("size"))
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            price, size = _finite(raw[0]), _finite(raw[1])
        else:
            continue
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            levels.append((price, size))
    return sorted(levels, key=lambda item: item[0], reverse=reverse)


def _target_quote_from_book(token_id: str, book: Mapping[str, Any], observed_at: str) -> dict[str, Any] | None:
    asks = _ws_levels(book.get("asks"), reverse=False)
    bids = _ws_levels(book.get("bids"), reverse=True)
    if not asks and not bids:
        return None
    return {
        "token_id": token_id,
        "bids": bids,
        "asks": asks,
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "observed_at": observed_at,
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
        "tick_size": book.get("tick_size"),
        "neg_risk": book.get("neg_risk") if isinstance(book.get("neg_risk"), bool) else None,
    }


async def _fetch_target_quote_books_async(tokens: Sequence[str], *, timeout_sec: float) -> dict[str, dict[str, Any]]:
    """Read only the missing target books from the official market WS.

    This is a bounded one-shot prewarm, not a fourth service.  It deliberately
    subscribes to the requested four-token set only, so a cold cache never
    triggers the multi-thousand-asset listener refresh.
    """

    import websockets

    token_set = {str(token).strip() for token in tokens if str(token).strip()}
    if not token_set:
        return {}
    ws_url = str(os.environ.get("PM_MARKET_WS_URL") or "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    ssl_module = __import__("ssl")
    try:
        import certifi

        ssl_context = ssl_module.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        # The resident /usr/bin Python on macOS may not ship certifi.  The
        # system trust store is still valid; missing an optional CA helper
        # must not collapse the official WS quote prewarm into an empty book.
        ssl_context = ssl_module.create_default_context()
    books: dict[str, dict[str, Any]] = {}
    order_books: dict[str, dict[str, dict[float, float]]] = {
        token: {"bids": {}, "asks": {}} for token in token_set
    }
    connect_kwargs: dict[str, Any] = {
        "ssl": ssl_context,
        "ping_interval": 20,
        "ping_timeout": 20,
        "open_timeout": min(20.0, max(2.0, float(timeout_sec))),
        "close_timeout": 2,
        "max_size": 8 * 1024 * 1024,
    }
    proxy_url = str(
        os.environ.get("PM_MARKET_WS_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or ""
    ).strip()
    if proxy_url:
        # Make the selected local proxy explicit. Relying on the websockets
        # library's ambient proxy discovery allowed a stale launchd 7891 value
        # to survive after the machine moved to 7890.
        connect_kwargs["proxy"] = proxy_url
    started = time.monotonic()
    async with websockets.connect(ws_url, **connect_kwargs) as ws:
        await ws.send(json.dumps({
            "assets_ids": sorted(token_set),
            "type": "market",
            "custom_feature_enabled": True,
        }))
        while time.monotonic() - started < max(0.5, float(timeout_sec)):
            remaining = max(0.05, float(timeout_sec) - (time.monotonic() - started))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(0.75, remaining))
            except asyncio.TimeoutError:
                continue
            observed_at = _now()
            for message in _ws_messages(raw):
                event_type = str(message.get("event_type") or message.get("eventType") or "").lower()
                token_hint = str(message.get("asset_id") or message.get("assetId") or "").strip()
                is_initial_book = (
                    not event_type
                    and token_hint in token_set
                    and ("bids" in message or "asks" in message)
                )
                if event_type == "book" or is_initial_book:
                    token = str(message.get("asset_id") or message.get("assetId") or "").strip()
                    if token not in token_set:
                        continue
                    order_books[token] = {
                        "bids": {price: size for price, size in _ws_levels(message.get("bids"), reverse=True)},
                        "asks": {price: size for price, size in _ws_levels(message.get("asks"), reverse=False)},
                    }
                    quote = _target_quote_from_book(token, order_books[token], observed_at)
                    if quote is not None:
                        books[token] = quote
                elif event_type == "price_change":
                    for change in message.get("price_changes") or []:
                        if not isinstance(change, Mapping):
                            continue
                        token = str(change.get("asset_id") or change.get("assetId") or "").strip()
                        if token not in token_set:
                            continue
                        price, size = _finite(change.get("price")), _finite(change.get("size"))
                        if price is None or size is None or not 0 < price < 1:
                            continue
                        side = str(change.get("side") or "").upper()
                        key = "bids" if side == "BUY" else "asks" if side == "SELL" else ""
                        if not key:
                            continue
                        if size <= 0:
                            order_books[token][key].pop(price, None)
                        else:
                            order_books[token][key][price] = size
                        quote = _target_quote_from_book(token, {
                            "bids": [{"price": p, "size": s} for p, s in order_books[token]["bids"].items()],
                            "asks": [{"price": p, "size": s} for p, s in order_books[token]["asks"].items()],
                        }, observed_at)
                        if quote is not None:
                            books[token] = quote
            if len(books) == len(token_set) and all(books[token].get("asks") for token in token_set):
                break
    return books


def _fetch_target_quote_books(tokens: Sequence[str], *, timeout_sec: float) -> dict[str, dict[str, Any]]:
    """Synchronous boundary for the copy-follow worker thread."""

    if not tokens:
        return {}
    try:
        return asyncio.run(_fetch_target_quote_books_async(tokens, timeout_sec=timeout_sec))
    except Exception:
        # The caller retains the per-bucket quote-missing evidence.  A network
        # prewarm failure never becomes a REST quote fallback or an order.
        return {}


def _walk_ask_depth(asks: Sequence[tuple[float, float]], notional: float, max_price: float) -> dict[str, float] | None:
    remaining = float(notional)
    shares = 0.0
    cost = 0.0
    limit_price = 0.0
    for price, level_size in asks:
        if price > float(max_price) + 1e-12:
            break
        take = min(float(level_size), remaining / price)
        if take <= 0:
            continue
        shares += take
        cost += take * price
        remaining -= take * price
        limit_price = price
        if remaining <= 1e-7:
            break
    if remaining > 1e-7 or shares <= 0 or limit_price <= 0:
        return None
    return {"size": shares, "cost": cost, "limit_price": limit_price}


def _response_mapping(value: Any) -> dict[str, Any]:
    """Flatten the gateway's common nested order response shapes.

    ``/lookup-exchange-order`` returns the exchange payload under
    ``response`` while the idempotency endpoint historically used ``order``.
    Reconciliation needs the same normalized status and fill fields from both
    paths; otherwise a confirmed zero-fill cancellation is misclassified as
    an unknown fill and unnecessarily leaves a plan at ``reconcile_required``.
    """

    if not isinstance(value, Mapping):
        return {}
    result = dict(value)
    for nested_key in ("order", "response"):
        nested = value.get(nested_key)
        if isinstance(nested, Mapping):
            for key, item in nested.items():
                result.setdefault(str(key), item)
    return result


def _matched_size(response: Mapping[str, Any]) -> float | None:
    for key in ("size_matched", "sizeMatched", "matched_size", "matchedSize", "filled_size", "filledSize"):
        value = _finite(response.get(key))
        if value is not None:
            return value
    return None


def _fill_price(response: Mapping[str, Any], fallback: float) -> float:
    for key in ("average_price", "averagePrice", "avg_price", "avgPrice", "fill_price", "fillPrice", "price"):
        value = _finite(response.get(key))
        if value is not None and 0 < value < 1:
            return value
    return float(fallback)


def _copy_follow_ladder_levels(
    quote: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> list[dict[str, float]]:
    """Return passive BUY levels from the current official WS bid ladder.

    The ladder is deliberately derived from observed bid prices rather than
    synthesizing prices from a guessed tick size.  That keeps every posted
    level aligned with the exchange quote evidence and naturally collapses to
    one level when the book exposes only one bid.  The configured weights are
    renormalized when fewer levels are available.
    """

    ladder = execution.get("buy_ladder") if isinstance(execution.get("buy_ladder"), Mapping) else {}
    if ladder.get("enabled") is not True:
        best_bid = _finite(quote.get("best_bid"))
        return [{"price": best_bid, "weight": 1.0}] if best_bid is not None and 0 < best_bid < 1 else []

    try:
        max_levels = max(1, min(5, int(ladder.get("max_levels", 3) or 3)))
    except (TypeError, ValueError):
        max_levels = 3
    raw_weights = ladder.get("allocation_weights")
    weights: list[float] = []
    if isinstance(raw_weights, (list, tuple)):
        for value in raw_weights[:max_levels]:
            parsed = _finite(value)
            if parsed is not None and parsed > 0:
                weights.append(parsed)
    if not weights:
        weights = [0.5, 0.3, 0.2][:max_levels]

    raw_bids = quote.get("bids") if isinstance(quote.get("bids"), (list, tuple)) else []
    bids: list[float] = []
    for raw in raw_bids:
        if isinstance(raw, Mapping):
            price = _finite(raw.get("price"))
        elif isinstance(raw, (list, tuple)) and raw:
            price = _finite(raw[0])
        else:
            price = None
        if price is not None and 0 < price < 1 and all(abs(price - prior) > 1e-9 for prior in bids):
            bids.append(price)
    best_bid = _finite(quote.get("best_bid"))
    if best_bid is not None and 0 < best_bid < 1 and all(abs(best_bid - prior) > 1e-9 for prior in bids):
        bids.insert(0, best_bid)
    bids.sort(reverse=True)
    bids = bids[:max_levels]
    if not bids:
        return []
    weights = weights[: len(bids)]
    if len(weights) < len(bids):
        weights.extend([weights[-1] if weights else 1.0] * (len(bids) - len(weights)))
    total = sum(weights)
    if total <= 0:
        weights = [1.0] * len(bids)
        total = float(len(bids))
    return [
        {"price": price, "weight": weight / total, "level": float(index)}
        for index, (price, weight) in enumerate(zip(bids, weights))
    ]


def _order_status(response: Mapping[str, Any]) -> str:
    return str(response.get("status") or response.get("state") or "").strip().lower().replace("-", "_")


class CopyFollowProductionEngine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        gateway: LocalDynamicThreeLegClobGateway | None = None,
        config_path: Path = DEFAULT_CONFIG_PATH,
        pause_file: Path = DEFAULT_PAUSE_FILE,
        activity_fetcher=None,
        positions_fetcher=fetch_positions,
        quote_reader=_quote_book,
        quote_batch_reader=_fetch_target_quote_books,
    ) -> None:
        self.conn = conn
        self.gateway = gateway or LocalDynamicThreeLegClobGateway(timeout_seconds=1.5)
        self.config_path = Path(config_path)
        self.pause_file = Path(pause_file)
        # Tests and explicit callers may still inject the old activity reader;
        # production defaults to the much smaller current-position endpoint.
        self.activity_fetcher = activity_fetcher
        self.positions_fetcher = positions_fetcher
        self.quote_reader = quote_reader
        self.quote_batch_reader = quote_batch_reader
        self._default_quote_reader = quote_reader is _quote_book
        self._last_quote_timing: dict[str, Any] = {}
        self._run_quote_cache: dict[str, dict[str, Any]] | None = None
        self.city_selection_cache_path = Path(
            os.environ.get("PM_COPY_FOLLOW_CITY_SELECTION_CACHE_PATH", str(DEFAULT_CITY_SELECTION_CACHE))
        )
        self._city_selection_cache = _read_json(self.city_selection_cache_path) or None
        self._city_selection_cache_at = self._cache_age_monotonic(self._city_selection_cache)
        # Production schema is installed by the controlled cutover.  Avoid
        # replaying DDL on every bounded check because a concurrent resident
        # writer can otherwise turn a read/reconcile pass into SQLITE_BUSY.
        # Isolated tests/tools may still start from an empty connection.
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='copy_follow_plans' LIMIT 1"
        ).fetchone()
        if table_row is None:
            apply_copy_follow_schema(conn)

    @staticmethod
    def _cache_age_monotonic(value: Mapping[str, Any] | None) -> float:
        """Convert a persisted UTC refresh time into this process's clock."""

        if not isinstance(value, Mapping):
            return 0.0
        refreshed_at = _parse_time(value.get("refreshed_at"))
        if refreshed_at is None:
            return 0.0
        age = max(0.0, (datetime.now(timezone.utc) - refreshed_at).total_seconds())
        return max(0.0, time.monotonic() - age)

    def _enabled(self, config: Mapping[str, Any]) -> bool:
        return config.get("state") == "enabled" and not self.pause_file.exists()

    def _select_city_candidates(
        self,
        candidates: Sequence[CopyFollowCandidate],
        source_rows: Sequence[Mapping[str, Any]],
        config: Mapping[str, Any],
        *,
        prefetched_trades: Sequence[Mapping[str, Any]] | None = None,
        prefetched_trades_error: BaseException | None = None,
    ) -> tuple[list[CopyFollowCandidate], dict[str, Any]]:
        """Apply the cached top-city selector without touching bucket identity."""

        policy = config.get("city_selection") if isinstance(config.get("city_selection"), Mapping) else {}
        if policy.get("enabled") is not True:
            return list(candidates), {"state": "disabled", "selected_cities": None}
        now = time.monotonic()
        try:
            refresh_sec = max(30.0, float(policy.get("refresh_sec", 300.0) or 300.0))
        except (TypeError, ValueError):
            refresh_sec = 300.0
        cached = self._city_selection_cache
        if cached is None or now - self._city_selection_cache_at >= refresh_sec:
            try:
                if prefetched_trades_error is not None:
                    raise prefetched_trades_error
                trades = list(prefetched_trades) if prefetched_trades is not None else fetch_trades(
                    user=str((config.get("source") or {}).get("user") or ""),
                    base_url=str((config.get("source") or {}).get("trades_url") or "https://data-api.polymarket.com/trades"),
                    page_size=int((config.get("source") or {}).get("trades_page_size", 500) or 500),
                    max_pages=int((config.get("source") or {}).get("trades_max_pages", 20) or 20),
                    timeout_sec=float((config.get("source") or {}).get("trades_timeout_sec", 10.0) or 10.0),
                    since_timestamp=int(time.time()) - int(float(policy.get("window_days", 7) or 7)) * 86400,
                )
                ranked = rank_recent_city_candidates(
                    source_rows,
                    trades,
                    source_user=str((config.get("source") or {}).get("user") or ""),
                    max_cities=int(policy.get("max_cities", 5) or 5),
                    window_days=int(policy.get("window_days", 7) or 7),
                    minimum_initial_value_usdc=float(policy.get("minimum_initial_value_usdc", 5.0) or 5.0),
                    lookahead_days=int(config.get("lookahead_days", 2) or 2),
                )
                if ranked.get("status") == "ready":
                    public_ranked = {
                        key: value
                        for key, value in ranked.items()
                        if key != "selected_candidates"
                    }
                    cached = {
                        **public_ranked,
                        "state": "ready",
                        "selection_mode": str(policy.get("mode") or "top_recent_7d_roi"),
                        "refreshed_at": _now(),
                    }
                    self._city_selection_cache = cached
                    self._city_selection_cache_at = now
                    try:
                        _write_json_atomic(self.city_selection_cache_path, cached)
                    except OSError:
                        # Selection caching is only a latency optimization. A
                        # failed cache write must not change the source-city
                        # decision or open a new fallback path.
                        pass
                elif cached is None:
                    cached = {
                        **ranked,
                        "state": "blocked",
                        "selected_cities": [],
                        "selected_candidates": [],
                    }
            except Exception as exc:
                if cached is None:
                    cached = {
                        "state": "stale_previous_selection_unavailable",
                        "selected_cities": [],
                        "selected_candidates": [],
                        "reason_codes": [f"copy_follow_city_selection_fetch_failed:{type(exc).__name__}"],
                    }
                else:
                    cached = {
                        **cached,
                        "state": "stale_previous_selection",
                        "reason_codes": [f"copy_follow_city_selection_refresh_failed:{type(exc).__name__}"],
                    }
        selected_cities = {
            str(city).strip().lower()
            for city in cached.get("selected_cities") or []
            if str(city).strip()
        }
        pinned_cities = {
            str(city).strip().lower()
            for city in policy.get("pinned_cities") or []
            if str(city).strip()
        }
        # A config snapshot is intentionally a last-known selection only.  It
        # may be reused on a transient ranking API failure, but never invents
        # a new city or bucket.
        if not selected_cities:
            selected_cities = {
                str(city).strip().lower()
                for city in policy.get("selected_cities") or []
                if str(city).strip()
            }
            if selected_cities and cached.get("state") != "ready":
                cached = {
                    **cached,
                    "state": "stale_configured_selection",
                    "selected_cities": sorted(selected_cities),
                }
        selected_cities.update(pinned_cities)
        filtered = [candidate for candidate in candidates if candidate.city_slug in selected_cities]
        return filtered, {
            **dict(cached),
            "selected_cities": sorted(selected_cities),
            "pinned_cities": sorted(pinned_cities),
            "filtered_candidate_count": len(filtered),
            "unfiltered_candidate_count": len(candidates),
        }

    def _city_selection_refresh_due(self, config: Mapping[str, Any]) -> bool:
        """Return whether the ranking feed should be refreshed this run.

        This is deliberately a local decision.  It lets ``run_once`` start
        the optional trades request alongside the source positions request,
        while ``_select_city_candidates`` remains the only authority allowed
        to apply the cached ranking result.
        """

        policy = config.get("city_selection") if isinstance(config.get("city_selection"), Mapping) else {}
        if policy.get("enabled") is not True:
            return False
        try:
            refresh_sec = max(30.0, float(policy.get("refresh_sec", 300.0) or 300.0))
        except (TypeError, ValueError):
            refresh_sec = 300.0
        return self._city_selection_cache is None or time.monotonic() - self._city_selection_cache_at >= refresh_sec

    @staticmethod
    def _fetch_city_selection_trades(
        config: Mapping[str, Any],
        *,
        required_event_slugs: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch only the optional ranking input for the current source wallet."""

        source = config.get("source") if isinstance(config.get("source"), Mapping) else {}
        policy = config.get("city_selection") if isinstance(config.get("city_selection"), Mapping) else {}
        return fetch_trades(
            user=str(source.get("user") or ""),
            base_url=str(source.get("trades_url") or "https://data-api.polymarket.com/trades"),
            page_size=int(source.get("trades_page_size", 500) or 500),
            max_pages=int(source.get("trades_max_pages", 20) or 20),
            timeout_sec=float(source.get("trades_timeout_sec", 10.0) or 10.0),
            since_timestamp=int(time.time()) - int(float(policy.get("window_days", 7) or 7)) * 86400,
            required_event_slugs=required_event_slugs,
        )

    def _existing_plan(self, candidate: CopyFollowCandidate) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM copy_follow_plans
               WHERE source_user=? AND event_slug=? AND selection_signature=?
               ORDER BY created_at DESC LIMIT 1""",
            (candidate.source_user, candidate.event_slug, candidate.selection_signature),
        ).fetchone()

    def _city_cap_available(self, candidate: CopyFollowCandidate, cap: float) -> bool:
        row = self.conn.execute(
            """SELECT COALESCE(SUM(total_budget_usdc),0) FROM copy_follow_plans
               WHERE city_slug=? AND state IN ('submitting','submitted','reconcile_required')""",
            (candidate.city_slug,),
        ).fetchone()
        return float(row[0] or 0.0) + candidate.total_allocation_usdc <= cap + 1e-9

    def _registry_rows(self, candidate: CopyFollowCandidate) -> dict[str, dict[str, Any]]:
        placeholders = ",".join("?" for _ in candidate.buckets)
        registry_columns = {
            str(row[1])
            for row in self.conn.execute(
                "PRAGMA table_info(reported_trajectory_market_registry)"
            )
        }
        timezone_select = ",settlement_timezone" if "settlement_timezone" in registry_columns else ""
        rows = self.conn.execute(
            f"""SELECT outcome,city_slug,station_id,yes_token,source_eligible,market_active,market_accepting_orders{timezone_select}
                  FROM reported_trajectory_market_registry
                 WHERE slug=? AND event_date=? AND outcome IN ({placeholders})""",
            (candidate.event_slug, candidate.event_date, *candidate.buckets),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            outcome = str(row[0] or "")
            result[outcome] = {
                "bucket": outcome,
                "city_slug": str(row[1] or ""),
                "station_id": str(row[2] or ""),
                "yes_token": str(row[3] or ""),
                "source_eligible": int(row[4] or 0),
                "market_active": int(row[5] or 0),
                "market_accepting_orders": int(row[6] or 0),
                "settlement_timezone": str(row[7] or "") if timezone_select else "",
            }
        return result

    def _forecast_context(
        self,
        candidate: CopyFollowCandidate,
        registry_rows: Mapping[str, Mapping[str, Any]],
        *,
        main_bucket: str | None = None,
        forecast_config: Mapping[str, Any] | None = None,
        include_remote: bool = True,
    ) -> dict[str, Any]:
        """Read-only display context; never used as a copy-follow gate.

        The execution hot path reads only already materialized local forecast
        snapshots.  Remote AWC/AMSC/Open-Meteo fetches are enabled for this
        reporting helper by default and never delay a quote solve or order
        decision; the execution hot path explicitly passes ``False``.
        """

        station_ids = sorted({str(row.get("station_id") or "") for row in registry_rows.values() if str(row.get("station_id") or "")})
        if not station_ids:
            return {
                "taf": {"peak_c": None, "state": "missing"},
                "ecmwf": {"peak_c": None, "state": "missing"},
                "open_meteo": {"peak_c": None, "state": "missing"},
                "amsc_taf": {"peak_c": None, "state": "missing"},
                "awc_taf": {"peak_c": None, "state": "missing"},
                "taf_peak_c": None,
                "ecmwf_peak_c": None,
                "open_meteo_peak_c": None,
                "main_bucket": main_bucket or candidate.main_bucket,
                "comparison": {"taf": "missing", "ecmwf": "missing", "open_meteo": "missing"},
                "comparison_state": "station_identity_missing",
                "display_only": True,
            }
        placeholders = ",".join("?" for _ in station_ids)
        params = (*station_ids, candidate.event_date)
        primary_bucket = str(main_bucket or candidate.main_bucket)

        # A TAF peak is tied to a revision.  Select the newest available
        # revision per station; taking MAX(peak_c) would incorrectly preserve
        # an old, warmer forecast after a correction.
        taf_rows: list[dict[str, Any]] = []
        try:
            rows = self.conn.execute(
                f"""SELECT station_id,peak_c,issue_time_utc,source_revision_id,peak_valid_time_utc
                    FROM amsc_taf_snapshots
                    WHERE station_id IN ({placeholders}) AND event_date=? AND state='available'
                    ORDER BY station_id,issue_time_utc DESC,created_at DESC""",
                params,
            ).fetchall()
            seen_stations: set[str] = set()
            for row in rows:
                station = str(row[0] or "")
                if not station or station in seen_stations:
                    continue
                seen_stations.add(station)
                taf_rows.append(
                    {
                        "station_id": station,
                        "peak_c": _finite(row[1]),
                        "issue_time_utc": str(row[2] or ""),
                        "source_revision_id": str(row[3] or ""),
                        "peak_valid_time_utc": str(row[4] or ""),
                    }
                )
        except sqlite3.Error:
            taf_rows = []

        # NWP snapshots contain one row per valid time.  First select the
        # newest model run per station, then take that run's daily maximum.
        ecmwf_rows: list[dict[str, Any]] = []
        try:
            rows = self.conn.execute(
                f"""SELECT station_id,run_at_utc,valid_time_utc,temperature_2m_c,snapshot_id
                    FROM official_nwp_station_snapshots
                    WHERE station_id IN ({placeholders}) AND event_date=? AND state='available'
                      AND LOWER(provider_id) LIKE '%ecmwf%'
                    ORDER BY station_id,run_at_utc DESC,valid_time_utc ASC""",
                params,
            ).fetchall()
            latest_run_by_station: dict[str, str] = {}
            grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
            for row in rows:
                station, run_at = str(row[0] or ""), str(row[1] or "")
                if not station or not run_at:
                    continue
                latest_run_by_station.setdefault(station, run_at)
                grouped.setdefault((station, run_at), []).append(row)
            for station, run_at in latest_run_by_station.items():
                candidates = [row for row in grouped.get((station, run_at), []) if _finite(row[3]) is not None]
                if not candidates:
                    continue
                peak_row = max(candidates, key=lambda row: float(row[3]))
                ecmwf_rows.append(
                    {
                        "station_id": station,
                        "peak_c": _finite(peak_row[3]),
                        "run_at_utc": run_at,
                        "peak_valid_time_utc": str(peak_row[2] or ""),
                        "snapshot_id": str(peak_row[4] or ""),
                    }
                )
        except sqlite3.Error:
            ecmwf_rows = []

        def source_context(rows: list[dict[str, Any]], *, source_id: str) -> dict[str, Any]:
            valid = [row for row in rows if row.get("peak_c") is not None]
            if not valid:
                return {"peak_c": None, "state": "missing", "source_id": source_id, "rows": []}
            # One registry event normally maps to one station.  If a malformed
            # registry maps several stations, retain all evidence and use the
            # warmest selected station only for the compact summary.
            selected = max(valid, key=lambda row: float(row["peak_c"]))
            comparison = compare_forecast_peak_to_bucket(selected["peak_c"], primary_bucket)
            return {
                "peak_c": selected["peak_c"],
                "state": "available",
                "source_id": source_id,
                "station_id": selected.get("station_id"),
                "comparison": comparison,
                "rows": rows,
            }

        forecast = forecast_config if isinstance(forecast_config, Mapping) else {}
        open_meteo_config = forecast.get("open_meteo") if isinstance(forecast.get("open_meteo"), Mapping) else {}
        awc_taf_config = forecast.get("awc_taf") if isinstance(forecast.get("awc_taf"), Mapping) else {}
        amsc_taf_config = forecast.get("amsc_taf") if isinstance(forecast.get("amsc_taf"), Mapping) else {}
        coordinates = open_meteo_config.get("station_coordinates") if isinstance(open_meteo_config.get("station_coordinates"), Mapping) else {}
        awc_taf_rows: list[dict[str, Any]] = []
        awc_taf_reasons: list[str] = []
        awc_success_stations: set[str] = set()
        if include_remote and awc_taf_config.get("enabled") is True:
            for station in station_ids:
                # AWC is deliberately excluded for China; China remains on
                # the AMSC lane with no cross-source fallback.
                if station.startswith("Z"):
                    continue
                point = coordinates.get(station) if isinstance(coordinates.get(station), Mapping) else {}
                result = fetch_awc_taf_peak(
                    station_id=station,
                    event_date=candidate.event_date,
                    api_url=str(awc_taf_config.get("api_url") or "https://aviationweather.gov/api/data/taf"),
                    timeout_sec=float(awc_taf_config.get("timeout_sec", 4.0) or 4.0),
                    timezone_name=str(point.get("timezone") or "") or None,
                )
                if result.get("peak_c") is not None:
                    awc_taf_rows.extend(result.get("rows") or [])
                    awc_success_stations.add(station)
                else:
                    awc_taf_reasons.append(f"{station}:{result.get('reason') or result.get('state') or 'unavailable'}")

        # AMSC is the official supplemental TAF lane.  Use it for stations
        # that do not already have a persisted AMSC peak and where AWC did not
        # provide TX.  This keeps the hot path read-only and avoids treating
        # two providers as a weather vote; the freshest single revision is
        # still selected by source_context below.
        amsc_taf_rows: list[dict[str, Any]] = []
        amsc_taf_reasons: list[str] = []
        persisted_amsc_stations = {str(row.get("station_id") or "") for row in taf_rows if row.get("station_id")}
        if include_remote and amsc_taf_config.get("enabled") is True:
            for station in station_ids:
                if station in persisted_amsc_stations or station in awc_success_stations:
                    continue
                point = coordinates.get(station) if isinstance(coordinates.get(station), Mapping) else {}
                result = fetch_amsc_taf_peak(
                    station_id=station,
                    event_date=candidate.event_date,
                    timezone_name=str(point.get("timezone") or "") or None,
                    count=int(amsc_taf_config.get("count", 12) or 12),
                    timeout_sec=float(amsc_taf_config.get("timeout_sec", 8.0) or 8.0),
                )
                if result.get("peak_c") is not None:
                    amsc_taf_rows.extend(result.get("rows") or [])
                else:
                    amsc_taf_reasons.append(f"{station}:{result.get('reason') or result.get('state') or 'unavailable'}")
        taf_rows.extend(amsc_taf_rows)
        taf_context = source_context(taf_rows, source_id="amsc_taf_snapshots")
        awc_taf_context = source_context(awc_taf_rows, source_id="awc_taf_snapshots")
        awc_taf_context["display_only"] = True
        if awc_taf_reasons:
            awc_taf_context["reasons"] = awc_taf_reasons
        taf_context["display_only"] = True
        if amsc_taf_reasons:
            taf_context["reasons"] = amsc_taf_reasons
        # Keep the source-specific contexts visible.  This is not
        # cross-source voting: one newest revision is selected for the compact
        # TAF field, while AWC and AMSC remain auditable separately.
        ecmwf_context = source_context(ecmwf_rows, source_id="official_nwp_station_snapshots")
        open_meteo_rows: list[dict[str, Any]] = []
        open_meteo_reasons: list[str] = []
        if include_remote and open_meteo_config.get("enabled") is True:
            coordinates = open_meteo_config.get("station_coordinates") if isinstance(open_meteo_config.get("station_coordinates"), Mapping) else {}
            for station in station_ids:
                point = coordinates.get(station) if isinstance(coordinates.get(station), Mapping) else {}
                if not point:
                    open_meteo_reasons.append(f"station_coordinates_missing:{station}")
                    continue
                result = fetch_open_meteo_daily_peak(
                    latitude=point.get("latitude"),
                    longitude=point.get("longitude"),
                    event_date=candidate.event_date,
                    timezone_name=str(point.get("timezone") or "UTC"),
                    models=None,
                    api_url=str(open_meteo_config.get("api_url") or "https://api.open-meteo.com/v1/forecast"),
                    timeout_sec=float(open_meteo_config.get("timeout_sec", 4.0) or 4.0),
                )
                if result.get("peak_c") is not None:
                    open_meteo_rows.append(
                        {
                            "station_id": station,
                            "peak_c": result.get("peak_c"),
                            "event_date": candidate.event_date,
                            "timezone": result.get("timezone") or point.get("timezone") or "UTC",
                            "request_url": result.get("request_url") or "",
                            "source_id": result.get("source_id") or "open_meteo_daily_temperature_2m_max",
                        }
                    )
                else:
                    open_meteo_reasons.append(f"{station}:{result.get('reason') or result.get('state') or 'unavailable'}")
        if open_meteo_rows:
            open_meteo_selected = max(open_meteo_rows, key=lambda row: float(row["peak_c"]))
            open_meteo_context = {
                "peak_c": open_meteo_selected["peak_c"],
                "state": "available",
                "source_id": "open_meteo_daily_temperature_2m_max",
                "station_id": open_meteo_selected.get("station_id"),
                "comparison": compare_forecast_peak_to_bucket(open_meteo_selected["peak_c"], primary_bucket),
                "rows": open_meteo_rows,
                "display_only": True,
            }

        # If the official ECMWF snapshot lane has not materialized a run for
        # this station/date yet, fetch the ECMWF IFS model exposed by
        # Open-Meteo as a clearly labelled display-only replacement.  It is
        # never consumed by bucket selection or order sizing.
        ecmwf_open_meteo_rows: list[dict[str, Any]] = []
        ecmwf_open_meteo_reasons: list[str] = []
        if include_remote and ecmwf_context.get("peak_c") is None and open_meteo_config.get("enabled") is True:
            ecmwf_model = str(open_meteo_config.get("ecmwf_model") or "ecmwf_ifs025")
            coordinates = open_meteo_config.get("station_coordinates") if isinstance(open_meteo_config.get("station_coordinates"), Mapping) else {}
            for station in station_ids:
                point = coordinates.get(station) if isinstance(coordinates.get(station), Mapping) else {}
                if not point:
                    ecmwf_open_meteo_reasons.append(f"station_coordinates_missing:{station}")
                    continue
                result = fetch_open_meteo_daily_peak(
                    latitude=point.get("latitude"),
                    longitude=point.get("longitude"),
                    event_date=candidate.event_date,
                    timezone_name=str(point.get("timezone") or "UTC"),
                    models=ecmwf_model,
                    api_url=str(open_meteo_config.get("api_url") or "https://api.open-meteo.com/v1/forecast"),
                    timeout_sec=float(open_meteo_config.get("timeout_sec", 4.0) or 4.0),
                )
                if result.get("peak_c") is not None:
                    ecmwf_open_meteo_rows.append(
                        {
                            "station_id": station,
                            "peak_c": result.get("peak_c"),
                            "event_date": candidate.event_date,
                            "timezone": result.get("timezone") or point.get("timezone") or "UTC",
                            "request_url": result.get("request_url") or "",
                            "source_id": "open_meteo_ecmwf_ifs025",
                            "model": ecmwf_model,
                        }
                    )
                else:
                    ecmwf_open_meteo_reasons.append(f"{station}:{result.get('reason') or result.get('state') or 'unavailable'}")
        if ecmwf_context.get("peak_c") is None and ecmwf_open_meteo_rows:
            selected = max(ecmwf_open_meteo_rows, key=lambda row: float(row["peak_c"]))
            ecmwf_context = {
                "peak_c": selected["peak_c"],
                "state": "available",
                "source_id": "open_meteo_ecmwf_ifs025",
                "source_label": "Open-Meteo ECMWF IFS 0.25°替代",
                "station_id": selected.get("station_id"),
                "comparison": compare_forecast_peak_to_bucket(selected["peak_c"], primary_bucket),
                "rows": ecmwf_open_meteo_rows,
                "display_only": True,
                "replacement_for": "official_ecmwf_snapshot",
            }
        elif ecmwf_context.get("peak_c") is None and ecmwf_open_meteo_reasons:
            ecmwf_context["reasons"] = ecmwf_open_meteo_reasons

        # AWC/AMSC TAFs often omit an explicit TX group.  Preserve both raw
        # TAF contexts, but use the fetched Open-Meteo daily peak as the
        # compact TAF display replacement when no TAF TX is available.
        selected_taf_context = taf_context if taf_context["peak_c"] is not None else awc_taf_context
        if selected_taf_context.get("peak_c") is None and open_meteo_rows:
            selected = max(open_meteo_rows, key=lambda row: float(row["peak_c"]))
            selected_taf_context = {
                "peak_c": selected["peak_c"],
                "state": "available",
                "source_id": "open_meteo_taf_replacement",
                "source_label": "Open-Meteo替代TAF",
                "station_id": selected.get("station_id"),
                "comparison": compare_forecast_peak_to_bucket(selected["peak_c"], primary_bucket),
                "rows": open_meteo_rows,
                "display_only": True,
                "replacement_for": "taf_tx_peak",
            }
        else:
            open_meteo_context = {
                "peak_c": None,
                "state": "disabled" if open_meteo_config.get("enabled") is not True else "unavailable",
                "source_id": "open_meteo_daily_temperature_2m_max",
                "rows": [],
                "reasons": open_meteo_reasons,
                "display_only": True,
            }
        return {
            # Keep scalar aliases for existing consumers while exposing the
            # complete, source-specific comparison objects.
            "taf_peak_c": selected_taf_context["peak_c"],
            "ecmwf_peak_c": ecmwf_context["peak_c"],
            "open_meteo_peak_c": open_meteo_context["peak_c"],
            "taf": selected_taf_context,
            "amsc_taf": taf_context,
            "awc_taf": awc_taf_context,
            "ecmwf": ecmwf_context,
            "open_meteo": open_meteo_context,
            "main_bucket": primary_bucket,
            "comparison": {
                "taf": (selected_taf_context.get("comparison") or {}).get("relation", "missing"),
                "ecmwf": (ecmwf_context.get("comparison") or {}).get("relation", "missing"),
                "open_meteo": (open_meteo_context.get("comparison") or {}).get("relation", "missing"),
            },
            "comparison_state": "available" if selected_taf_context["peak_c"] is not None or ecmwf_context["peak_c"] is not None or open_meteo_context["peak_c"] is not None else "forecast_context_missing",
            "station_ids": station_ids,
            "display_only": True,
        }

    def _sync_open_orders(self, plan_id: str) -> dict[str, Any]:
        """Reconcile every live GTC order before making a new decision."""

        started = time.perf_counter()
        rows = self.conn.execute(
            "SELECT * FROM copy_follow_orders WHERE plan_id=? AND state IN (?,?,?,?) ORDER BY created_at",
            (plan_id, *OPEN_ORDER_STATES),
        ).fetchall()
        blockers: list[str] = []
        lookup_timings_ms: list[float] = []
        for row in rows:
            state = str(row["state"] or "")
            if state in {"submitting", "cancel_submitting"}:
                blockers.append(f"copy_follow_order_state_ambiguous:{row['local_order_id']}")
                continue
            try:
                lookup_started = time.perf_counter()
                raw = self.gateway.lookup_order(
                    {
                        "idempotency_key": row["idempotency_key"],
                        "order_id": row["exchange_order_id"],
                    }
                )
                response = _response_mapping(raw)
            except Exception as exc:
                blockers.append(f"copy_follow_order_lookup_failed:{row['local_order_id']}:{type(exc).__name__}")
                lookup_timings_ms.append(_elapsed_ms(lookup_started))
                continue
            lookup_timings_ms.append(_elapsed_ms(lookup_started))
            expected_exchange_id = str(row["exchange_order_id"] or "")
            # The gateway idempotency record can deliberately remain in
            # ``reconcile_required`` after it has recovered an exchange order
            # id.  In that state its lightweight lookup has no fill fields;
            # use the exact exchange id once more before declaring the fill
            # unknown.  This is an identity-preserving read, never a retry.
            response_status = _order_status(response)
            # An idempotency record may be evicted from the local gateway
            # cache (``found=false``) even though the durable row still has
            # the exact exchange order identity.  In that case the response
            # has no authoritative fill field; query that exact identity
            # before blocking.  This is a read-only reconciliation, never a
            # new order or an idempotency retry.  It also covers an ``open``
            # exchange order whose endpoint omits ``size_matched``: the
            # existing durable matched amount is then a safe lower bound.
            needs_exact_exchange_lookup = (
                bool(expected_exchange_id)
                and (
                    response.get("reconcile_required") is True
                    or _matched_size(response) is None
                    or response_status in {"submitted_unconfirmed", "submitted_unknown"}
                )
            )
            if needs_exact_exchange_lookup and expected_exchange_id:
                try:
                    exchange = _response_mapping(
                        self.gateway.lookup_exchange_order({"order_id": expected_exchange_id})
                    )
                    exchange_id_check = str(
                        exchange.get("orderID") or exchange.get("order_id") or exchange.get("id") or ""
                    )
                    exchange_status = _order_status(exchange)
                    if exchange_id_check == expected_exchange_id and exchange_status in {
                        "open", "live", "posted", "active", "submitted", "partially_filled", "partial",
                        "matched", "filled", "cancelled", "canceled", "expired",
                    }:
                        response = exchange
                except Exception:
                    # Keep the original idempotency response so the normal
                    # fail-closed blocker below remains authoritative.
                    pass
            exchange_id = str(response.get("orderID") or response.get("order_id") or response.get("id") or "")
            if expected_exchange_id and exchange_id and exchange_id != expected_exchange_id:
                blockers.append(f"copy_follow_order_identity_mismatch:{row['local_order_id']}")
                continue
            matched = _matched_size(response)
            if matched is None:
                status = _order_status(response)
                matched = float(row["size_matched"] or 0.0) if status in {"open", "live", "active", "posted", "submitted"} else None
            if matched is None:
                blockers.append(f"copy_follow_order_fill_state_unknown:{row['local_order_id']}")
                continue
            requested = float(row["requested_size"] or 0.0)
            previous = float(row["size_matched"] or 0.0)
            if matched < previous - 1e-7 or matched > requested + 1e-7:
                blockers.append(f"copy_follow_order_fill_size_invalid:{row['local_order_id']}")
                continue
            status = _order_status(response)
            if matched + 0.005 >= requested:
                next_state = "filled"
            elif (
                status in {"cancelled", "canceled", "expired", "rejected", "not_found"}
                or (str(row["order_type"] or "") == "FAK" and status in {"matched", "filled"})
            ):
                next_state = "cancelled"
            elif status in {"open", "live", "active", "posted", "submitted", "partially_filled", "partial", "matched"}:
                next_state = "partially_filled" if matched > 1e-9 else "open"
            else:
                blockers.append(f"copy_follow_order_status_unknown:{row['local_order_id']}")
                continue
            self.conn.execute(
                """UPDATE copy_follow_orders
                   SET state=?,exchange_order_id=COALESCE(NULLIF(?,''),exchange_order_id),
                       size_matched=?,gateway_response_json=?,last_error=NULL,updated_at=?
                 WHERE local_order_id=?""",
                (next_state, exchange_id, matched, _json(response), _now(), row["local_order_id"]),
            )
        self.conn.commit()
        return {
            "ok": not blockers,
            "reason_codes": blockers,
            "timing_ms": {
                "total_ms": _elapsed_ms(started),
                "lookup_count": len(lookup_timings_ms),
                "lookup_ms": lookup_timings_ms,
            },
        }

    def _reconcile_unknown_orders(self, plan_id: str) -> dict[str, Any]:
        """Resolve exact timeout identities without creating a new order.

        The gateway's idempotency record can remain ``reconcile_required``
        even when the exchange accepted the order and the exchange-order
        lookup is already authoritative.  Only that exact identity is
        inspected here; an unresolved identity remains a hard stop.
        """

        blockers: list[str] = []
        resolved: list[dict[str, Any]] = []
        rows = self.conn.execute(
            "SELECT * FROM copy_follow_orders WHERE plan_id=? AND state='reconcile_required' ORDER BY created_at",
            (plan_id,),
        ).fetchall()
        for row in rows:
            local_id = str(row["local_order_id"] or "")
            try:
                outcome = _response_mapping(
                    self.gateway.lookup_order({"idempotency_key": row["idempotency_key"], "order_id": row["exchange_order_id"]})
                )
            except Exception as exc:
                blockers.append(f"copy_follow_order_reconcile_failed:{local_id}:{type(exc).__name__}")
                continue
            if outcome.get("found") is True and outcome.get("pending") is not True and outcome.get("exchange_post_started") is False:
                self.conn.execute(
                    "UPDATE copy_follow_orders SET state='submit_failed',gateway_response_json=?,last_error=?,updated_at=? WHERE local_order_id=?",
                    (_json(outcome), str(outcome.get("error") or "copy_follow_known_local_rejection"), _now(), local_id),
                )
                resolved.append({"local_order_id": local_id, "state": "submit_failed", "gateway": outcome})
                continue
            expected_id = str(
                row["exchange_order_id"]
                or outcome.get("exchange_order_id")
                or outcome.get("expected_order_id")
                or ""
            )
            if not expected_id:
                blockers.append(f"copy_follow_order_reconcile_identity_missing:{local_id}")
                continue
            try:
                exchange = _response_mapping(self.gateway.lookup_exchange_order({"order_id": expected_id}))
            except Exception as exc:
                blockers.append(f"copy_follow_exchange_lookup_failed:{local_id}:{type(exc).__name__}")
                continue
            exchange_id = str(exchange.get("orderID") or exchange.get("order_id") or exchange.get("id") or "")
            status = _order_status(exchange)
            if exchange_id != expected_id or status not in {"open", "live", "posted", "active", "submitted", "partially_filled", "partial", "matched", "filled", "cancelled", "canceled", "expired"}:
                blockers.append(f"copy_follow_order_reconcile_exchange_state_unknown:{local_id}")
                continue
            matched = _matched_size(exchange)
            if matched is None:
                matched = float(row["size_matched"] or 0.0) if status in {"open", "live", "posted", "active", "submitted"} else None
            if matched is None or matched < float(row["size_matched"] or 0.0) - 1e-7 or matched > float(row["requested_size"] or 0.0) + 1e-7:
                blockers.append(f"copy_follow_order_reconcile_fill_unknown:{local_id}")
                continue
            requested = float(row["requested_size"] or 0.0)
            next_state = "filled" if matched + 0.005 >= requested else "partially_filled" if matched > 1e-9 else "cancelled" if status in {"cancelled", "canceled", "expired"} else "open"
            self.conn.execute(
                """UPDATE copy_follow_orders
                   SET state=?,exchange_order_id=?,size_matched=?,gateway_response_json=?,last_error=NULL,updated_at=?
                 WHERE local_order_id=?""",
                (next_state, exchange_id, matched, _json({"idempotency": outcome, "exchange": exchange}), _now(), local_id),
            )
            resolved.append({"local_order_id": local_id, "state": next_state, "exchange_order_id": exchange_id})
        self.conn.commit()
        return {"ok": not blockers, "reason_codes": blockers, "resolved": resolved}

    def _managed_state(self, plan_id: str) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT * FROM copy_follow_orders WHERE plan_id=? ORDER BY created_at",
            (plan_id,),
        ).fetchall()
        holdings: dict[str, float] = {}
        net_cost = 0.0
        pending_by_bucket_side: dict[tuple[str, str], float] = {}
        open_rows: list[dict[str, Any]] = []
        for row in rows:
            bucket = str(row["bucket"] or "")
            side = str(row["side"] or "").upper()
            matched = max(0.0, float(row["size_matched"] or 0.0))
            try:
                response = _response_mapping(json.loads(str(row["gateway_response_json"] or "{}")))
            except (TypeError, ValueError):
                response = {}
            fill_price = _fill_price(response, float(row["limit_price"] or 0.0))
            signed_size = matched if side == "BUY" else -matched
            holdings[bucket] = holdings.get(bucket, 0.0) + signed_size
            net_cost += matched * fill_price if side == "BUY" else -matched * fill_price
            if str(row["state"] or "") in FILLABLE_ORDER_STATES:
                remaining = max(0.0, float(row["requested_size"] or 0.0) - matched)
                if remaining > 1e-8:
                    key = (bucket, side)
                    pending_by_bucket_side[key] = pending_by_bucket_side.get(key, 0.0) + remaining
                    open_rows.append(dict(row))
        projected_holdings = dict(holdings)
        projected_net_cost = net_cost
        for (bucket, side), remaining in pending_by_bucket_side.items():
            price_rows = [item for item in open_rows if item["bucket"] == bucket and item["side"] == side]
            price = (
                sum(float(item["limit_price"]) * max(0.0, float(item["requested_size"]) - float(item["size_matched"])) for item in price_rows) / remaining
                if remaining > 0 else 0.0
            )
            if side == "BUY":
                projected_holdings[bucket] = projected_holdings.get(bucket, 0.0) + remaining
                projected_net_cost += remaining * price
            else:
                projected_holdings[bucket] = projected_holdings.get(bucket, 0.0) - remaining
                projected_net_cost -= remaining * price
        return {
            "rows": rows,
            "holdings_by_bucket": holdings,
            "net_cost_usdc": net_cost,
            "pending_by_bucket_side": pending_by_bucket_side,
            "open_rows": open_rows,
            "projected_holdings_by_bucket": projected_holdings,
            "projected_net_cost_usdc": projected_net_cost,
            "profits_by_bucket": {bucket: quantity - net_cost for bucket, quantity in holdings.items()},
        }

    def _legacy_fok_plan_requires_reconcile(self, plan_id: str) -> bool:
        """Never place a second basket on top of an archived FOK plan."""

        dynamic_count = self.conn.execute(
            "SELECT COUNT(*) FROM copy_follow_orders WHERE plan_id=?",
            (plan_id,),
        ).fetchone()[0]
        if int(dynamic_count or 0) > 0:
            return False
        legacy_count = self.conn.execute(
            """SELECT COUNT(*) FROM copy_follow_legs
                WHERE plan_id=? AND state IN ('exchange_accepted','reconcile_required','submitting')""",
            (plan_id,),
        ).fetchone()[0]
        return int(legacy_count or 0) > 0

    def _cancel_order(self, row: Mapping[str, Any]) -> dict[str, Any]:
        local_id = str(row.get("local_order_id") or "")
        self.conn.execute(
            "UPDATE copy_follow_orders SET state='cancel_submitting',updated_at=? WHERE local_order_id=? AND state IN ('open','partially_filled')",
            (_now(), local_id),
        )
        self.conn.commit()
        try:
            response = _response_mapping(
                self.gateway.cancel_order(
                    {"order_id": row.get("exchange_order_id"), "idempotency_key": row.get("idempotency_key")}
                )
            )
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
        status = _order_status(response)
        confirmed = response.get("cancelled") is True or response.get("canceled") is True or status in {"cancelled", "canceled", "expired"}
        if response.get("ok") is True and confirmed:
            matched = _matched_size(response)
            self.conn.execute(
                """UPDATE copy_follow_orders SET state='cancelled',size_matched=COALESCE(?,size_matched),
                   gateway_response_json=?,last_error=NULL,updated_at=? WHERE local_order_id=?""",
                (matched, _json(response), _now(), local_id),
            )
            self.conn.commit()
            return {"state": "cancelled", "local_order_id": local_id, "gateway": response}
        self.conn.execute(
            "UPDATE copy_follow_orders SET state='reconcile_required',gateway_response_json=?,last_error=?,updated_at=? WHERE local_order_id=?",
            (_json(response), "copy_follow_cancel_confirmation_unknown", _now(), local_id),
        )
        self.conn.commit()
        return {"state": "reconcile_required", "local_order_id": local_id, "gateway": response}

    def _cancel_open_orders(self, managed: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Cancel every outstanding order while freezing the filled basket."""

        results: list[dict[str, Any]] = []
        for row in managed.get("open_rows") or []:
            result = self._cancel_order(row)
            results.append(result)
            if result.get("state") == "reconcile_required":
                break
        return results

    def _desired_actions(
        self,
        candidate: CopyFollowCandidate,
        prepared: Mapping[str, Any],
        managed: Mapping[str, Any],
        config: Mapping[str, Any],
        *,
        source_changed: bool = False,
    ) -> dict[str, Any]:
        execution = config.get("execution") if isinstance(config.get("execution"), Mapping) else {}
        minimum_shares = float(execution.get("minimum_order_shares", 1.0) or 1.0)
        minimum_notional = float(execution.get("minimum_rebalance_notional_usdc", 0.25) or 0.25)
        allow_reductions = execution.get("allow_reductions", True) is True
        enforce_city_cap = execution.get("enforce_city_cap", False) is True
        cap = _city_budget_cap(config, candidate) if enforce_city_cap else float("inf")
        if enforce_city_cap:
            reserved_elsewhere = self.conn.execute(
                """SELECT COALESCE(SUM(total_budget_usdc),0)
                     FROM copy_follow_plans
                    WHERE city_slug=? AND plan_id<>? AND state IN ('submitting','submitted','reconcile_required')""",
                (candidate.city_slug, candidate.plan_id),
            ).fetchone()[0]
            cap = max(0.0, cap - float(reserved_elsewhere or 0.0))
        target = {str(key): float(value) for key, value in (prepared.get("target_quantities") or {}).items()}
        actions: list[dict[str, Any]] = []
        cancellations: list[dict[str, Any]] = []
        reasons: list[str] = []
        # Standing exit-liquidity sells are reconciled separately.  They are
        # not reductions, do not reserve pending copy quantity, and must not
        # be cancelled by the buy-only rebalance policy.
        open_rows = [
            dict(row)
            for row in (managed.get("open_rows") or [])
            if not _is_exit_liquidity_order(row)
        ]
        actual_holdings = {
            bucket: float(managed.get("holdings_by_bucket", {}).get(bucket, 0.0) or 0.0)
            for bucket in candidate.buckets
        }
        # Only durable matched size and its fill price are settlement
        # evidence.  An open/partially-filled order is intent, not inventory
        # or cost, and must never authorize the next replenishment cycle.
        actual_net_cost = float(
            managed.get(
                "net_cost_usdc",
                managed.get("projected_net_cost_usdc", 0.0),
            )
            or 0.0
        )
        actual_settlement_profit = {
            bucket: actual_holdings[bucket] - actual_net_cost
            for bucket in candidate.buckets
        }
        actual_minimum_profit = min(actual_settlement_profit.values()) if actual_settlement_profit else None
        actual_maximum_profit = max(actual_settlement_profit.values()) if actual_settlement_profit else None
        safety_floor = float(prepared.get("safety_floor_usdc") or 0.01)
        floor_gate_enabled = execution.get("settlement_floor_hard_gate") is True
        primary = str(prepared.get("main_bucket") or candidate.main_bucket)
        secondary = str(prepared.get("secondary_bucket") or "")
        edges = [bucket for bucket in candidate.buckets if bucket not in {primary, secondary}]
        strict_gap = float(execution.get("strict_share_gap", 1.0) or 1.0)
        initial_entry = prepared.get("initial_entry") is True
        buy_ladder = execution.get("buy_ladder") if isinstance(execution.get("buy_ladder"), Mapping) else {}
        ladder_enabled = buy_ladder.get("enabled") is True and not initial_entry
        fallback_policy = (
            execution.get("heavy_bucket_best_bid_fallback")
            if isinstance(execution.get("heavy_bucket_best_bid_fallback"), Mapping)
            else {}
        )
        fallback_enabled = fallback_policy.get("enabled", True) is True
        fallback_after_sec = _finite(fallback_policy.get("after_sec", 6.0)) or 6.0
        fallback_buckets = {
            str(bucket)
            for bucket in prepared.get("best_bid_fallback_buckets") or []
            if str(bucket) in candidate.buckets
        } if not source_changed else set()
        # A copy basket is not allowed to become temporarily incomplete just
        # because the source allocation moved.  Pending BUY orders are only
        # intent; they do not make a missing leg part of the held basket.
        # While any selected leg is actually missing, pending SELLs are
        # cancelled and no replenishment is authorized in that same pass.
        basket_incomplete = any(quantity <= 1e-7 for quantity in actual_holdings.values())
        protected_reduction_rows = [
            dict(row)
            for row in open_rows
            if basket_incomplete and str(row.get("side") or "").upper() == "SELL"
        ] if allow_reductions else []
        if protected_reduction_rows:
            reasons.append("copy_follow_preserve_incomplete_basket")
        if not allow_reductions:
            sell_rows = [
                dict(row)
                for row in open_rows
                if str(row.get("side") or "").upper() == "SELL"
            ]
            if sell_rows:
                cancellations.extend(sell_rows)
                reasons.append("copy_follow_sell_disabled")
                open_rows = [
                    row
                    for row in open_rows
                    if str(row.get("side") or "").upper() != "SELL"
                ]

        pending_fill_rows = [
            dict(row)
            for row in open_rows
            if max(
                0.0,
                float(row.get("requested_size") or 0.0)
                - float(row.get("size_matched") or 0.0),
            ) > 1e-8
        ]
        pending_primary_buy_rows = [
            row
            for row in pending_fill_rows
            if str(row.get("bucket") or "") == primary
            and str(row.get("side") or "").upper() == "BUY"
        ]
        aged_primary_buy_rows = [
            row
            for row in pending_primary_buy_rows
            if (_age_seconds(row.get("created_at")) or 0.0) >= fallback_after_sec
        ]
        if (
            fallback_enabled
            and ladder_enabled
            and not source_changed
            and primary not in fallback_buckets
            and aged_primary_buy_rows
        ):
            # A passive ladder that has sat on the heavy bucket long enough is
            # no longer useful as the entry path.  Cancel every outstanding
            # child first; the next cycle will confirm those cancellations and
            # then post the entire remaining heavy-bucket delta at the latest
            # official best bid.  Never cancel and replace in one cycle.
            fallback_buckets.add(primary)
            cancellations.extend(aged_primary_buy_rows)
            reasons.append("copy_follow_heavy_bucket_best_bid_fallback")
            return {
                "actions": [],
                "cancellations": [*cancellations, *protected_reduction_rows],
                "reason_codes": [*reasons, "copy_follow_cancel_stale_heavy_bucket_ladder"],
                "best_bid_fallback_active": True,
                "best_bid_fallback_buckets": sorted(fallback_buckets),
                "actual_holdings_by_bucket": actual_holdings,
                "actual_net_cost_usdc": actual_net_cost,
                "actual_settlement_profit_by_bucket": actual_settlement_profit,
                "actual_minimum_settlement_profit_usdc": actual_minimum_profit,
                "actual_maximum_settlement_profit_usdc": actual_maximum_profit,
                "actual_settlement_floor_passed": bool(
                    actual_minimum_profit is not None
                    and actual_minimum_profit + 1e-7 >= safety_floor
                ),
                "post_fill_settlement_floor_required": floor_gate_enabled,
                "pending_fill_count": len(pending_fill_rows),
            }
        if pending_fill_rows and not source_changed and not fallback_buckets:
            return {
                "actions": [],
                "cancellations": [*cancellations, *protected_reduction_rows],
                "reason_codes": [
                    *reasons,
                    "copy_follow_waiting_for_fill_reconciliation",
                ],
                "actual_holdings_by_bucket": actual_holdings,
                "actual_net_cost_usdc": actual_net_cost,
                "actual_settlement_profit_by_bucket": actual_settlement_profit,
                "actual_minimum_settlement_profit_usdc": actual_minimum_profit,
                "actual_maximum_settlement_profit_usdc": actual_maximum_profit,
                "actual_settlement_floor_passed": bool(
                    actual_minimum_profit is not None
                    and actual_minimum_profit + 1e-7 >= safety_floor
                ),
                "post_fill_settlement_floor_required": floor_gate_enabled,
                "pending_fill_count": len(pending_fill_rows),
                "best_bid_fallback_active": False,
                "best_bid_fallback_buckets": [],
            }

        # Pending orders are deliberately excluded from the projected state.
        # For a source change, the caller may cancel them in this pass; any
        # newly emitted action is still checked against actual inventory and
        # actual cost, never against an assumed future fill.
        projected = dict(actual_holdings)
        projected_net_cost = actual_net_cost
        def restore_role_order() -> None:
            if not floor_gate_enabled or not secondary:
                return
            target[secondary] = max(
                target[secondary],
                max(target[edge] for edge in edges) + strict_gap,
            )
            target[primary] = max(target[primary], target[secondary] + strict_gap)

        # A protected FAK market entry is still subject to the exchange's
        # minimum maker notional.  Raising each leg independently to that
        # minimum can collapse the required main > secondary > edge ordering
        # (all four legs become the same 1 USDC lot).  Establish the minimum
        # baseline first, then restore the role gaps so the simulated order
        # check and submitted quantities agree.
        if initial_entry and floor_gate_enabled:
            for bucket in candidate.buckets:
                market_price = _finite((prepared.get("market_price_cap_by_bucket") or {}).get(bucket))
                if market_price is None or not 0 < market_price < 1:
                    continue
                target[bucket] = max(target.get(bucket, 0.0), 1.0 / market_price)
            restore_role_order()

        restore_role_order()

        if basket_incomplete:
            # Keep every currently held leg in the target until the missing
            # legs have actually filled.  This deliberately permits a larger
            # temporary position than the source ratio, but never a naked
            # reduction of an existing basket leg.
            for bucket in candidate.buckets:
                target[bucket] = max(target.get(bucket, 0.0), actual_holdings[bucket])
            restore_role_order()

        # The CLOB rejects orders below its configured minimum lot.  A simple
        # ``held + minimum`` lift is not enough: an existing lot also carries
        # cost into every settlement scenario.  For example, holding only the
        # secondary bucket can make a freshly solved four-leg target lose its
        # floor after the three missing legs are added.  Re-solve the dynamic
        # target against the projected inventory and best-bid execution cost,
        # then add a common share tail.  Since the same total cost is deducted
        # from every scenario, the tail is safe exactly when
        # ``tail * (1 - sum(best_bid))`` covers the weakest scenario deficit.
        if floor_gate_enabled:
            for bucket in candidate.buckets:
                held = projected.get(bucket, 0.0)
                delta = target[bucket] - held
                if 1e-7 < delta < minimum_shares - 1e-8:
                    target[bucket] = held + minimum_shares
        restore_role_order()

        def target_cost_and_profits() -> tuple[float, dict[str, float]] | None:
            cost = projected_net_cost
            for bucket in candidate.buckets:
                quote = (prepared.get("quotes_by_bucket") or {}).get(bucket) or {}
                price = _finite(quote.get("best_bid"))
                if price is None or not 0 < price < 1:
                    return None
                delta = target[bucket] - projected.get(bucket, 0.0)
                if delta > 1e-7:
                    # Reserve the cent-rounded maker amount used by the
                    # signed GTC payload, not an optimistic floating value.
                    cost += math.ceil(delta * price * 100.0 - 1e-12) / 100.0
                elif delta < -1e-7:
                    cost += delta * price
            return cost, {
                bucket: target[bucket] - cost for bucket in candidate.buckets
            }

        floor = safety_floor
        if floor_gate_enabled:
            for _ in range(256):
                evaluated = target_cost_and_profits()
                if evaluated is None:
                    break
                simulated_cost, simulated_profits = evaluated
                deficit = floor - min(simulated_profits.values())
                if deficit <= 1e-7:
                    break
                bid_sum = sum(
                    float((prepared.get("quotes_by_bucket") or {}).get(bucket, {}).get("best_bid") or 0.0)
                    for bucket in candidate.buckets
                )
                gain_per_common_share = 1.0 - bid_sum
                if gain_per_common_share <= 1e-7:
                    break
                increment = max(
                    minimum_shares if deficit >= minimum_shares else 0.01,
                    deficit / gain_per_common_share + 0.0001,
                )
                for bucket in candidate.buckets:
                    target[bucket] += increment
                restore_role_order()

        dynamic_sizing_block = False
        dynamic_evaluation = target_cost_and_profits()
        if dynamic_evaluation is not None:
            dynamic_cost, dynamic_profits = dynamic_evaluation
            if floor_gate_enabled and min(dynamic_profits.values()) + 1e-7 < floor:
                reasons.append("copy_follow_rebalance_settlement_floor_not_met")
                dynamic_sizing_block = True
            if enforce_city_cap and dynamic_cost > cap + 1e-7:
                reasons.append("copy_follow_city_cap_would_be_exceeded")
                dynamic_sizing_block = True
        if dynamic_sizing_block:
            return {
                "actions": [],
                "cancellations": [],
                "reason_codes": reasons,
                "best_bid_fallback_active": bool(fallback_buckets),
                "best_bid_fallback_buckets": sorted(fallback_buckets),
            }

        protected_reduction_ids = {
            str(row.get("local_order_id") or "") for row in protected_reduction_rows
        }
        open_rows = [
            row
            for row in open_rows
            if str(row.get("local_order_id") or "") not in protected_reduction_ids
        ]
        cancellations.extend(protected_reduction_rows)
        for bucket in candidate.buckets:
            desired_delta = target[bucket] - projected.get(bucket, 0.0)
            side = "BUY" if desired_delta > 1e-7 else "SELL" if desired_delta < -1e-7 else ""
            if side == "SELL" and not allow_reductions:
                reasons.append(f"copy_follow_sell_disabled:{bucket}")
                continue
            opposite = "SELL" if side == "BUY" else "BUY" if side == "SELL" else ""
            held_quantity = float(managed.get("holdings_by_bucket", {}).get(bucket, 0.0) or 0.0)
            desired_from_holdings = target[bucket] - held_quantity
            desired_side_from_holdings = (
                "BUY" if desired_from_holdings > 1e-7
                else "SELL" if desired_from_holdings < -1e-7
                else ""
            )
            same_direction_rows = [
                row for row in open_rows
                if str(row["bucket"]) == bucket
                and str(row["side"]).upper() == desired_side_from_holdings
            ]
            pending_same_direction = sum(
                max(0.0, float(row["requested_size"] or 0.0) - float(row["size_matched"] or 0.0))
                for row in same_direction_rows
            )
            if same_direction_rows:
                desired_abs = abs(desired_from_holdings)
                if desired_abs + 1e-6 < pending_same_direction:
                    cancellations.extend(dict(row) for row in same_direction_rows)
                    reasons.append(f"copy_follow_cancel_stale_order:{bucket}")
                    continue
                # If the target is larger than the existing pending amount,
                # retain that order and let ``desired_delta`` below emit only
                # the missing tail.  This avoids duplicate notional while
                # still allowing a four-leg cycle to finish serially.
            opposite_rows = [row for row in open_rows if str(row["bucket"]) == bucket and str(row["side"]).upper() == opposite]
            if opposite_rows:
                cancellations.extend(dict(row) for row in opposite_rows)
                reasons.append(f"copy_follow_cancel_opposite_order:{bucket}")
                continue
            if not side and not (ladder_enabled and desired_side_from_holdings == "BUY"):
                continue
            quote = (prepared.get("quotes_by_bucket") or {}).get(bucket) or {}
            best_bid = _finite(quote.get("best_bid"))
            initial_market_buy = initial_entry and side == "BUY"
            market_cap = _finite((prepared.get("market_price_cap_by_bucket") or {}).get(bucket))
            order_price = market_cap if initial_market_buy else best_bid
            if order_price is None or not 0 < order_price < 1:
                reasons.append(f"copy_follow_best_bid_missing:{bucket}")
                continue
            # The live CLOB gateway rejects prices below 0.01 even when the
            # quote shard exposes a dust ask/bid.  Do not turn that quote
            # anomaly into a partial initial basket: wait for a legal quote
            # and keep all first-entry legs out of the cycle.
            if order_price < 0.01:
                reasons.append(f"copy_follow_price_below_exchange_minimum:{bucket}")
                continue

            use_best_bid_fallback = (
                bucket in fallback_buckets
                and desired_side_from_holdings == "BUY"
                and not initial_market_buy
            )
            if use_best_bid_fallback:
                stale_fallback_rows = [
                    dict(row)
                    for row in same_direction_rows
                    if abs(float(row.get("limit_price") or 0.0) - order_price) > 1e-9
                ]
                if stale_fallback_rows:
                    cancellations.extend(stale_fallback_rows)
                    reasons.append(f"copy_follow_reprice_heavy_bucket_best_bid:{bucket}")
                    continue
                desired_abs = max(0.0, target[bucket] - held_quantity)
                pending_at_best_bid = sum(
                    max(
                        0.0,
                        float(row.get("requested_size") or 0.0)
                        - float(row.get("size_matched") or 0.0),
                    )
                    for row in same_direction_rows
                )
                quantity = desired_abs - pending_at_best_bid
                if quantity <= 1e-7:
                    continue
                estimated_notional = math.ceil(quantity * order_price * 100.0 - 1e-12) / 100.0
                has_existing_filled_lot = held_quantity > 1e-7 or any(
                    str(row["bucket"]) == bucket and float(row["size_matched"] or 0.0) > 1e-7
                    for row in managed.get("rows") or []
                )
                if (
                    (
                        quantity < minimum_shares - 1e-8
                        and not has_existing_filled_lot
                    )
                    or (
                        estimated_notional < minimum_notional - 1e-8
                        and not has_existing_filled_lot
                    )
                ):
                    reasons.append(f"copy_follow_rebalance_below_minimum:{bucket}:best_bid_fallback")
                    continue
                actions.append(
                    {
                        "bucket": bucket,
                        "token": (prepared.get("token_by_bucket") or {}).get(bucket),
                        "side": "BUY",
                        "quantity": round(quantity, 8),
                        "limit_price": order_price,
                        "estimated_notional_usdc": round(estimated_notional, 8),
                        "order_type": "GTC",
                        "price_mode": "best_bid",
                        "best_bid_fallback": True,
                        "allow_market": False,
                        "current_projected_quantity": round(projected.get(bucket, 0.0), 8),
                        "target_quantity": round(target[bucket], 8),
                        "quote_observed_at": quote.get("observed_at"),
                    }
                )
                continue

            # After the protected first entry, split a BUY shortfall across
            # the current official bid ladder.  Existing pending orders are
            # part of the target and are matched by price before emitting a
            # new tail, so a reprice never duplicates an in-flight quantity.
            if ladder_enabled and desired_side_from_holdings == "BUY":
                ladder_levels = _copy_follow_ladder_levels(quote, execution)
                if not ladder_levels:
                    reasons.append(f"copy_follow_best_bid_ladder_missing:{bucket}")
                    continue
                desired_abs = max(0.0, target[bucket] - held_quantity)
                level_defs = [dict(level) for level in ladder_levels]

                def renormalize_levels(levels: list[dict[str, float]]) -> list[dict[str, float]]:
                    total_weight = sum(float(level.get("weight") or 0.0) for level in levels)
                    if total_weight <= 0:
                        total_weight = float(len(levels))
                        for level in levels:
                            level["weight"] = 1.0
                    return [
                        {**level, "weight": float(level.get("weight") or 0.0) / total_weight}
                        for level in levels
                    ]

                # Do not create sub-minimum child orders.  Collapse the
                # deepest levels until every newly created child is legal.
                while len(level_defs) > 1:
                    candidate_weight = float(level_defs[-1].get("weight") or 0.0)
                    candidate_price = float(level_defs[-1]["price"])
                    candidate_size = desired_abs * candidate_weight
                    if candidate_size + 1e-8 >= minimum_shares and candidate_size * candidate_price + 1e-8 >= minimum_notional:
                        break
                    level_defs.pop()
                    level_defs = renormalize_levels(level_defs)
                level_defs = renormalize_levels(level_defs)
                desired_by_price: dict[float, float] = {}
                remaining_target = desired_abs
                for index, level in enumerate(level_defs):
                    price = float(level["price"])
                    quantity = desired_abs * float(level["weight"])
                    if index == len(level_defs) - 1:
                        quantity = remaining_target
                    quantity = max(0.0, quantity)
                    remaining_target = max(0.0, remaining_target - quantity)
                    desired_by_price[round(price, 8)] = quantity

                price_rows: dict[float, list[Mapping[str, Any]]] = {}
                for row in same_direction_rows:
                    row_price = _finite(row.get("limit_price"))
                    if row_price is None:
                        cancellations.append(dict(row))
                        continue
                    price_rows.setdefault(round(row_price, 8), []).append(row)
                for row_price, rows_at_price in price_rows.items():
                    desired_at_price = desired_by_price.get(row_price)
                    pending_at_price = sum(
                        max(0.0, float(row["requested_size"] or 0.0) - float(row["size_matched"] or 0.0))
                        for row in rows_at_price
                    )
                    if desired_at_price is None or pending_at_price > desired_at_price + 1e-6:
                        cancellations.extend(dict(row) for row in rows_at_price)
                        reasons.append(f"copy_follow_reprice_stale_ladder_order:{bucket}")

                for index, level in enumerate(level_defs):
                    price = float(level["price"])
                    desired_at_price = desired_by_price.get(round(price, 8), 0.0)
                    pending_at_price = sum(
                        max(0.0, float(row["requested_size"] or 0.0) - float(row["size_matched"] or 0.0))
                        for row in price_rows.get(round(price, 8), [])
                    )
                    quantity = desired_at_price - pending_at_price
                    if quantity <= 1e-7:
                        continue
                    estimated_notional = math.ceil(quantity * price * 100.0 - 1e-12) / 100.0
                    has_existing_filled_lot = held_quantity > 1e-7 or any(
                        str(row["bucket"]) == bucket and float(row["size_matched"] or 0.0) > 1e-7
                        for row in managed.get("rows") or []
                    )
                    if (
                        (quantity < minimum_shares - 1e-8 or estimated_notional < minimum_notional - 1e-8)
                        and not has_existing_filled_lot
                    ):
                        reasons.append(f"copy_follow_rebalance_below_minimum:{bucket}:ladder_{index}")
                        continue
                    actions.append(
                        {
                            "bucket": bucket,
                            "token": (prepared.get("token_by_bucket") or {}).get(bucket),
                            "side": "BUY",
                            "quantity": round(quantity, 8),
                            "limit_price": price,
                            "estimated_notional_usdc": round(estimated_notional, 8),
                            "order_type": "GTC",
                            "price_mode": "best_bid",
                            "ladder_level": index,
                            "ladder_weight": float(level.get("weight") or 0.0),
                            "allow_market": False,
                            "current_projected_quantity": round(projected.get(bucket, 0.0), 8),
                            "target_quantity": round(target[bucket], 8),
                            "quote_observed_at": quote.get("observed_at"),
                        }
                    )
                continue
            quantity = abs(desired_delta)
            if initial_market_buy:
                try:
                    aligned_quantity = _align_market_buy_quantity(order_price, quantity)
                except ValueError:
                    reasons.append(f"copy_follow_market_buy_precision_invalid:{bucket}")
                    continue
                if aligned_quantity > quantity + 1e-8:
                    target[bucket] = projected.get(bucket, 0.0) + aligned_quantity
                    quantity = aligned_quantity
            estimated_notional = quantity * order_price
            # Polymarket CLOB maker amounts are cent-precision while taker
            # share amounts are four-decimal precision.  Round the notional
            # upward so the signed order is accepted without understating the
            # capital reservation or weakening the settlement floor.
            estimated_notional = math.ceil(float(estimated_notional) * 100.0 - 1e-12) / 100.0
            has_existing_filled_lot = held_quantity > 1e-7 or any(
                str(row["bucket"]) == bucket and float(row["size_matched"] or 0.0) > 1e-7
                for row in managed.get("rows") or []
            )
            reentry_without_open_orders = (
                not (managed.get("open_rows") or [])
                and not any(
                    float(value or 0.0) > 1e-7
                    for value in (managed.get("holdings_by_bucket") or {}).values()
                )
            )
            if (
                (
                    quantity < minimum_shares - 1e-8
                    and not has_existing_filled_lot
                    and not reentry_without_open_orders
                )
                or (
                    estimated_notional < minimum_notional - 1e-8
                    and not has_existing_filled_lot
                    and not reentry_without_open_orders
                )
            ):
                reasons.append(f"copy_follow_rebalance_below_minimum:{bucket}")
                continue
            projected_cost = actual_net_cost
            if side == "SELL":
                held = float(managed.get("holdings_by_bucket", {}).get(bucket, 0.0) or 0.0)
                if held + 1e-7 < quantity:
                    reasons.append(f"copy_follow_sell_position_unavailable:{bucket}")
                    continue
                floor = safety_floor
                # A sell changes only this scenario's holdings while lowering
                # the common basket cost by bid * quantity.  Cap the sell to
                # the largest quantity that keeps this leg above the floor on
                # its own; waiting for other sell legs to execute would make a
                # partially filled multi-leg rebalance unsafe.
                safe_sell_capacity = (
                    held
                    if not floor_gate_enabled
                    else max(
                        0.0,
                        (held - projected_cost - floor) / max(1e-9, 1.0 - order_price),
                    )
                )
                if quantity > safe_sell_capacity + 1e-7:
                    quantity = safe_sell_capacity
                if quantity <= 1e-7:
                    reasons.append(f"copy_follow_reduction_breaks_settlement_floor:{bucket}")
                    continue
                estimated_notional = math.ceil(float(quantity * order_price) * 100.0 - 1e-12) / 100.0
            if enforce_city_cap and side == "BUY" and projected_cost + estimated_notional > cap + 1e-7:
                reasons.append("copy_follow_city_cap_would_be_exceeded")
                continue
            actions.append(
                {
                    "bucket": bucket,
                    "token": (prepared.get("token_by_bucket") or {}).get(bucket),
                    "side": side,
                    "quantity": round(quantity, 8),
                    "limit_price": order_price,
                    "estimated_notional_usdc": round(estimated_notional, 8),
                    "order_type": "FAK" if initial_market_buy else "GTC",
                    "price_mode": "market" if initial_market_buy else "best_bid",
                    "allow_market": bool(initial_market_buy),
                    "current_projected_quantity": round(projected.get(bucket, 0.0), 8),
                    "target_quantity": round(target[bucket], 8),
                    "quote_observed_at": quote.get("observed_at"),
                }
            )
        if initial_entry and any(
            reason.startswith("copy_follow_price_below_exchange_minimum:")
            for reason in reasons
        ):
            actions = []
        if actions:
            simulated_holdings = dict(projected)
            simulated_cost = projected_net_cost
            for action in actions:
                quantity = float(action["quantity"])
                bucket = str(action["bucket"])
                if action["side"] == "BUY":
                    simulated_holdings[bucket] = simulated_holdings.get(bucket, 0.0) + quantity
                    simulated_cost += float(action.get("estimated_notional_usdc") or 0.0)
                else:
                    simulated_holdings[bucket] = simulated_holdings.get(bucket, 0.0) - quantity
                    simulated_cost -= quantity * float(action["limit_price"])
            floor = safety_floor
            if (
                floor_gate_enabled
                and min((simulated_holdings.get(bucket, 0.0) - simulated_cost) for bucket in candidate.buckets) + 1e-7 < floor
            ):
                reasons.append("copy_follow_rebalance_settlement_floor_not_met")
                actions = []
            elif enforce_city_cap and simulated_cost > cap + 1e-7:
                reasons.append("copy_follow_city_cap_would_be_exceeded")
                actions = []
            else:
                primary = str(prepared.get("main_bucket") or candidate.main_bucket)
                secondary = str(prepared.get("secondary_bucket") or "")
                edges = [bucket for bucket in candidate.buckets if bucket not in {primary, secondary}]
                if floor_gate_enabled and (
                    simulated_holdings.get(primary, 0.0) <= simulated_holdings.get(secondary, 0.0) + float(execution.get("strict_share_gap", 1.0) or 1.0) - 1e-7
                    or any(simulated_holdings.get(secondary, 0.0) <= simulated_holdings.get(edge, 0.0) + float(execution.get("strict_share_gap", 1.0) or 1.0) - 1e-7 for edge in edges)
                ):
                    reasons.append("copy_follow_rebalance_primary_secondary_order_not_met")
                    actions = []
        final_holdings = dict(projected)
        final_cost = float(projected_net_cost)
        for action in actions:
            bucket = str(action.get("bucket") or "")
            quantity = float(action.get("quantity") or 0.0)
            if action.get("side") == "BUY":
                final_holdings[bucket] = final_holdings.get(bucket, 0.0) + quantity
                final_cost += float(action.get("estimated_notional_usdc") or 0.0)
            elif action.get("side") == "SELL":
                final_holdings[bucket] = final_holdings.get(bucket, 0.0) - quantity
                final_cost -= quantity * float(action.get("limit_price") or 0.0)
        post_fill_profits = {
            bucket: final_holdings.get(bucket, 0.0) - final_cost
            for bucket in candidate.buckets
        }
        post_fill_minimum = min(post_fill_profits.values()) if post_fill_profits else None
        post_fill_maximum = max(post_fill_profits.values()) if post_fill_profits else None
        active_fallback_buckets = sorted(
            bucket
            for bucket in fallback_buckets
            if target.get(bucket, 0.0) > actual_holdings.get(bucket, 0.0) + 1e-7
        )
        return {
            "actions": actions,
            "cancellations": cancellations,
            "reason_codes": reasons,
            "actual_holdings_by_bucket": actual_holdings,
            "actual_net_cost_usdc": actual_net_cost,
            "actual_settlement_profit_by_bucket": actual_settlement_profit,
            "actual_minimum_settlement_profit_usdc": actual_minimum_profit,
            "actual_maximum_settlement_profit_usdc": actual_maximum_profit,
            "actual_settlement_floor_passed": bool(
                actual_minimum_profit is not None
                and actual_minimum_profit + 1e-7 >= safety_floor
            ),
            "post_fill_holdings_by_bucket": final_holdings,
            "post_fill_net_cost_usdc": final_cost,
            "post_fill_settlement_profit_by_bucket": post_fill_profits,
            "post_fill_minimum_settlement_profit_usdc": post_fill_minimum,
            "post_fill_maximum_settlement_profit_usdc": post_fill_maximum,
            "post_fill_settlement_floor_passed": bool(
                post_fill_minimum is not None
                and post_fill_minimum + 1e-7 >= safety_floor
            ),
            "post_fill_settlement_floor_required": floor_gate_enabled,
            "best_bid_fallback_active": bool(active_fallback_buckets),
            "best_bid_fallback_buckets": active_fallback_buckets,
        }

    def _desired_exit_liquidity(
        self,
        candidate: CopyFollowCandidate,
        prepared: Mapping[str, Any],
        managed: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Keep fixed-price GTC exits over actually held YES shares.

        These sells are capital-release liquidity, not copy-ratio reductions.
        They are therefore deliberately independent from ``allow_reductions``
        and from the nominal city budget.  Pending buys are excluded: only
        durable matched holdings may authorize an exit order.
        """

        execution = config.get("execution") if isinstance(config.get("execution"), Mapping) else {}
        policy = execution.get("exit_liquidity") if isinstance(execution.get("exit_liquidity"), Mapping) else {}
        if policy.get("enabled") is not True:
            return {"enabled": False, "actions": [], "cancellations": [], "reason_codes": []}
        price = _finite(policy.get("price", 0.997))
        if price is None or not 0 < price < 1:
            return {
                "enabled": True,
                "actions": [],
                "cancellations": [],
                "reason_codes": ["copy_follow_exit_liquidity_price_invalid"],
            }
        minimum_shares = float(execution.get("minimum_order_shares", 1.0) or 1.0)
        minimum_notional = float(execution.get("minimum_rebalance_notional_usdc", 0.25) or 0.25)
        holdings = {
            bucket: max(0.0, float(managed.get("holdings_by_bucket", {}).get(bucket, 0.0) or 0.0))
            for bucket in candidate.buckets
        }
        open_rows = [
            dict(row)
            for row in managed.get("open_rows") or []
            if _is_exit_liquidity_order(row) and str(row.get("side") or "").upper() == "SELL"
        ]
        actions: list[dict[str, Any]] = []
        cancellations: list[dict[str, Any]] = []
        reasons: list[str] = []
        for bucket in candidate.buckets:
            held = holdings[bucket]
            rows = [row for row in open_rows if str(row.get("bucket") or "") == bucket]
            stale_rows = [
                row
                for row in rows
                if abs(float(row.get("limit_price") or 0.0) - price) > 1e-9
            ]
            if stale_rows:
                cancellations.extend(stale_rows)
                reasons.append(f"copy_follow_exit_liquidity_reprice:{bucket}")
                continue
            pending = sum(
                max(0.0, float(row.get("requested_size") or 0.0) - float(row.get("size_matched") or 0.0))
                for row in rows
            )
            if held <= 1e-7:
                if rows:
                    cancellations.extend(rows)
                    reasons.append(f"copy_follow_exit_liquidity_no_inventory:{bucket}")
                continue
            if pending > held + 1e-7:
                cancellations.extend(rows)
                reasons.append(f"copy_follow_exit_liquidity_oversized:{bucket}")
                continue
            quantity = held - pending
            if quantity <= 1e-7:
                continue
            estimated_notional = math.ceil(quantity * price * 100.0 - 1e-12) / 100.0
            if quantity < minimum_shares - 1e-8 or estimated_notional < minimum_notional - 1e-8:
                reasons.append(f"copy_follow_exit_liquidity_below_minimum:{bucket}")
                continue
            actions.append(
                {
                    "bucket": bucket,
                    "token": (prepared.get("token_by_bucket") or {}).get(bucket),
                    "side": "SELL",
                    "quantity": round(quantity, 8),
                    "limit_price": price,
                    "estimated_notional_usdc": round(estimated_notional, 8),
                    "order_type": "GTC",
                    "price_mode": "fixed_exit",
                    "allow_market": False,
                    "role": "exit_liquidity",
                    "current_projected_quantity": round(held, 8),
                    "target_quantity": round(held, 8),
                    "quote_observed_at": None,
                }
            )
        return {
            "enabled": True,
            "price": price,
            "order_type": "GTC",
            "actions": actions,
            "cancellations": cancellations,
            "reason_codes": reasons,
            "held_yes_by_bucket": holdings,
        }

    def _read_quote_batch(
        self,
        registry_rows: Mapping[str, Mapping[str, Any]],
        buckets: Sequence[str],
        *,
        max_age_sec: float,
        target_timeout_sec: float,
    ) -> dict[str, dict[str, Any]]:
        """Read local WS shards, then prewarm only missing target books."""

        started = time.perf_counter()
        local_started = time.perf_counter()
        quotes: dict[str, dict[str, Any]] = {}
        target_tokens: list[str] = []
        for bucket in buckets:
            registry = registry_rows[bucket]
            if (
                not registry.get("yes_token")
                or registry.get("source_eligible") != 1
                or registry.get("market_active") != 1
                or registry.get("market_accepting_orders") != 1
            ):
                continue
            token = str(registry.get("yes_token") or "").strip()
            if not token:
                continue
            quote = (
                self._run_quote_cache.get(token)
                if self._run_quote_cache is not None
                else self.quote_reader(token, max_age_sec=max_age_sec)
            )
            if quote is not None:
                quotes[bucket] = dict(quote)
            if self._run_quote_cache is None and self._default_quote_reader and (quote is None or not quote.get("asks")):
                target_tokens.append(token)
        local_ms = _elapsed_ms(local_started)
        target_started = time.perf_counter()
        target_ms = 0.0
        if target_tokens and self._run_quote_cache is None and self._default_quote_reader:
            target_quotes = self.quote_batch_reader(
                target_tokens,
                timeout_sec=max(0.5, float(target_timeout_sec)),
            ) or {}
            target_ms = _elapsed_ms(target_started)
            by_token = {
                str(token): value
                for token, value in target_quotes.items()
                if isinstance(value, Mapping)
            }
            for bucket in buckets:
                token = str(registry_rows[bucket].get("yes_token") or "").strip()
                target_quote = by_token.get(token)
                if target_quote is not None and target_quote.get("asks"):
                    quotes[bucket] = dict(target_quote)
        self._last_quote_timing = {
            "total_ms": _elapsed_ms(started),
            "local_cache_ms": local_ms,
            "target_prewarm_ms": target_ms,
            "target_prewarm_requested": bool(target_tokens),
            "target_prewarm_token_count": len(set(target_tokens)),
            "resolved_bucket_count": len(quotes),
            "requested_bucket_count": len(buckets),
            "run_level_cache": self._run_quote_cache is not None,
        }
        return quotes

    def _prime_run_quotes(
        self,
        candidates: Sequence[CopyFollowCandidate],
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Prime every selected token once for this run.

        A source wallet can hold many four-bucket cities.  Opening one WS per
        city serializes identical cold-start work.  This run-level cache keeps
        the subscription bounded to the selected candidate tokens while using
        one official CLOB WS connection for the whole cycle.
        """

        started = time.perf_counter()
        if not self._default_quote_reader or not candidates:
            # Keep injected readers (tests and isolated callers) on their
            # existing per-call path. An empty cache would shadow them.
            self._run_quote_cache = None
            return {
                "total_ms": _elapsed_ms(started),
                "requested_token_count": 0,
                "local_token_count": 0,
                "target_token_count": 0,
                "resolved_token_count": 0,
                "connection_count": 0,
                "skipped": True,
            }
        self._run_quote_cache = {}
        execution = config.get("execution") if isinstance(config.get("execution"), Mapping) else {}
        max_age_sec = float(execution.get("max_quote_age_sec", 5.0) or 5.0)
        target_timeout_sec = float(execution.get("target_quote_timeout_sec", 4.0) or 4.0)
        registry_by_token: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            rows = self._registry_rows(candidate)
            for bucket in candidate.buckets:
                row = rows.get(bucket)
                if not row or not row.get("yes_token"):
                    continue
                if (
                    row.get("source_eligible") != 1
                    or row.get("market_active") != 1
                    or row.get("market_accepting_orders") != 1
                ):
                    continue
                registry_by_token[str(row["yes_token"])] = row
        target_tokens: list[str] = []
        local_token_count = 0
        for token in sorted(registry_by_token):
            quote = self.quote_reader(token, max_age_sec=max_age_sec)
            if quote is not None:
                self._run_quote_cache[token] = dict(quote)
                local_token_count += 1
            if quote is None or not quote.get("asks"):
                target_tokens.append(token)
        target_started = time.perf_counter()
        error = ""
        if target_tokens:
            try:
                target_quotes = self.quote_batch_reader(
                    target_tokens,
                    timeout_sec=max(0.5, target_timeout_sec),
                ) or {}
                for token, quote in target_quotes.items():
                    if isinstance(quote, Mapping) and quote.get("asks"):
                        self._run_quote_cache[str(token)] = dict(quote)
            except Exception as exc:
                error = f"{type(exc).__name__}:{exc}"
        target_ms = _elapsed_ms(target_started) if target_tokens else 0.0
        return {
            "total_ms": _elapsed_ms(started),
            "local_cache_ms": round(max(0.0, target_started - started) * 1000.0, 3),
            "target_prewarm_ms": target_ms,
            "requested_token_count": len(registry_by_token),
            "local_token_count": local_token_count,
            "target_token_count": len(target_tokens),
            "resolved_token_count": len(self._run_quote_cache),
            "connection_count": 1 if target_tokens else 0,
            "error": error,
        }

    def _prepare(
        self,
        candidate: CopyFollowCandidate,
        config: Mapping[str, Any],
        *,
        managed: Mapping[str, Any] | None = None,
        source_changed: bool = False,
        previous_plan: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        execution = config.get("execution") if isinstance(config.get("execution"), Mapping) else {}
        buy_ladder = execution.get("buy_ladder") if isinstance(execution.get("buy_ladder"), Mapping) else {}
        building_gate = assess_source_building(
            candidate,
            max_age_sec=float(execution.get("active_building_max_age_sec", 900.0) or 900.0),
            minimum_main_notional_usdc=float(execution.get("main_bucket_min_source_notional_usdc", 5.0) or 5.0),
            minimum_main_share=float(execution.get("main_bucket_min_source_share", 0.35) or 0.35),
        )
        source_values = {bucket: float(candidate.source_notional_by_bucket.get(bucket, 0.0) or 0.0) for bucket in candidate.buckets}
        ranked = sorted(candidate.buckets, key=lambda bucket: (source_values[bucket], bucket), reverse=True)
        primary_bucket, secondary_bucket = ranked[0], ranked[1]
        rows = self._registry_rows(candidate)
        forecast_context = self._forecast_context(
            candidate,
            rows,
            main_bucket=primary_bucket,
            forecast_config=config.get("forecast") if isinstance(config.get("forecast"), Mapping) else None,
            include_remote=False,
        )
        if set(rows) != set(candidate.buckets):
            return _copy_follow_plan_summary(
                candidate,
                primary_bucket=primary_bucket,
                secondary_bucket=secondary_bucket,
                source_values=source_values,
                forecast_context=forecast_context,
                building_gate=building_gate,
                config=config,
                reason_codes=["copy_follow_registry_bucket_coverage_incomplete"],
                execution_status="waiting_for_market_registry",
                quote_status_by_bucket={
                    bucket: {"state": "registry_missing" if bucket not in rows else "available"}
                    for bucket in candidate.buckets
                },
            )
        hard_building_reasons = _hard_building_gate_reasons(building_gate)
        cutoff_hour = float(execution.get("local_entry_cutoff_hour", 18.0) or 18.0)
        local_window_reasons = sorted(
            {
                reason
                for timezone_name in {
                    str(row.get("settlement_timezone") or "").strip()
                    for row in rows.values()
                }
                if timezone_name
                for reason in [
                    _local_entry_window_reason(
                        candidate.event_date,
                        timezone_name,
                        cutoff_hour=cutoff_hour,
                    )
                ]
                if reason
            }
        )
        # A passed local peak window hides the city and prevents entry, but it
        # must not short-circuit the quote read.  The four-leg CLOB evidence
        # and settlement-floor calculation are still needed for the persisted
        # plan/audit and for a correct minimum/maximum settlement-profit
        # result.  Only structural/core-bucket reasons return before quotes.
        if hard_building_reasons:
            return {
                "state": "blocked",
                "execution_ready": False,
                "execution_status": "blocked_by_hard_core_gate",
                "strategy_id": "weather_copy_follow_v1",
                "plan_id": candidate.plan_id,
                "event_slug": candidate.event_slug,
                "event_date": candidate.event_date,
                "city_slug": candidate.city_slug,
                "reason_codes": hard_building_reasons,
                "building_gate": building_gate,
                "main_bucket": primary_bucket,
                "secondary_bucket": secondary_bucket,
                "selected_buckets": list(candidate.buckets),
                "source_notional_by_bucket": source_values,
                "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket,
                "forecast_context": forecast_context,
                "execution": {"order_type": "GTC", "all_legs_required": False, "uses_bayes": False, "uses_regime": False, "uses_weather_fields": False, "forecast_context_display_only": True},
                "settlement_floor_hard_gate": bool(execution.get("settlement_floor_hard_gate", False)),
            }
        quote_rows: list[dict[str, Any]] = []
        quote_status_by_bucket: dict[str, dict[str, Any]] = {}
        quote_wait_reasons: list[str] = []
        quotes_by_bucket = self._read_quote_batch(
            rows,
            candidate.buckets,
            max_age_sec=float(execution.get("max_quote_age_sec", 5.0) or 5.0),
            target_timeout_sec=float(execution.get("target_quote_timeout_sec", 4.0) or 4.0),
        )
        for bucket in candidate.buckets:
            registry = rows[bucket]
            if not registry["yes_token"] or registry["source_eligible"] != 1 or registry["market_active"] != 1 or registry["market_accepting_orders"] != 1:
                reason = f"copy_follow_market_not_tradeable:{bucket}"
                quote_wait_reasons.append(reason)
                quote_status_by_bucket[bucket] = {"state": "market_not_tradeable"}
                continue
            quote = quotes_by_bucket.get(bucket)
            if quote is None:
                reason = f"copy_follow_clob_quote_missing:{bucket}"
                quote_wait_reasons.append(reason)
                quote_status_by_bucket[bucket] = {"state": "quote_missing"}
                continue
            if not quote.get("asks"):
                # A live bid-only book is different from missing WS evidence:
                # the market is observable, but there is no executable YES
                # ask for the protected entry/floor calculation.
                reason = f"copy_follow_clob_ask_side_empty:{bucket}"
                quote_wait_reasons.append(reason)
                quote_status_by_bucket[bucket] = {"state": "ask_side_empty", "best_bid": quote.get("best_bid")}
                continue
            quote_status_by_bucket[bucket] = {
                "state": "available",
                "best_bid": quote.get("best_bid"),
                "best_ask": quote.get("best_ask"),
                "observed_at": quote.get("observed_at"),
            }
            quote_rows.append({**registry, **quote})
        if quote_wait_reasons:
            return _copy_follow_plan_summary(
                candidate,
                primary_bucket=primary_bucket,
                secondary_bucket=secondary_bucket,
                source_values=source_values,
                forecast_context=forecast_context,
                building_gate=building_gate,
                config=config,
                reason_codes=[*local_window_reasons, *quote_wait_reasons],
                execution_status="waiting_for_clob_evidence",
                quote_status_by_bucket=quote_status_by_bucket,
            )
        floor_gate_enabled = execution.get("settlement_floor_hard_gate") is True
        enforce_city_cap = execution.get("enforce_city_cap", False) is True
        if floor_gate_enabled:
            floor_plan = allocate_settlement_floor(
                buckets=candidate.buckets,
                source_allocations=candidate.allocation_usdc_by_bucket,
                asks_by_bucket={row["bucket"]: row["asks"] for row in quote_rows},
                budget_usdc=_city_budget_cap(config, candidate),
                primary_bucket=primary_bucket,
                secondary_bucket=secondary_bucket,
                safety_floor_usdc=float(execution.get("safety_floor_usdc", 0.01) or 0.01),
                minimum_shares=float(execution.get("minimum_order_shares", 1.0) or 1.0),
                minimum_notional_usdc=float(execution.get("minimum_rebalance_notional_usdc", 0.0) or 0.0),
                strict_share_gap=float(execution.get("strict_share_gap", 1.0) or 1.0),
                maximum_buy_price=float(execution.get("maximum_buy_price", 0.99) or 0.99),
            )
        else:
            source_scaled = allocate_source_scaled_copy(
                buckets=candidate.buckets,
                source_notional_by_bucket=candidate.source_notional_by_bucket,
                source_shares_by_bucket=candidate.source_shares_by_bucket,
                best_ask_by_bucket={
                    row["bucket"]: row.get("best_ask")
                    for row in quote_rows
                },
                budget_usdc=_city_budget_cap(config, candidate),
            )
            if source_scaled.get("status") != "ready":
                return _copy_follow_plan_summary(
                    candidate,
                    primary_bucket=primary_bucket,
                    secondary_bucket=secondary_bucket,
                    source_values=source_values,
                    forecast_context=forecast_context,
                    building_gate=building_gate,
                    config=config,
                    reason_codes=list(source_scaled.get("reason_codes") or ["copy_follow_source_weight_missing"]),
                    execution_status="waiting_for_source_weight_evidence",
                    quote_status_by_bucket=quote_status_by_bucket,
                )
            source_scaled_cost = float(source_scaled.get("total_cost_usdc") or 0.0)
            source_scaled_profits = {
                bucket: float(source_scaled["quantities"][bucket]) - source_scaled_cost
                for bucket in candidate.buckets
            }
            floor_plan = {
                **source_scaled,
                "primary_bucket": primary_bucket,
                "secondary_bucket": secondary_bucket,
                "source_allocations_usdc": candidate.allocation_usdc_by_bucket,
                "best_ask_sum": sum(float(row["best_ask"]) for row in quote_rows),
                "quantities": source_scaled["quantities"],
                "costs_usdc": source_scaled["costs_usdc"],
                "limit_prices": source_scaled["limit_prices"],
                "total_cost_usdc": source_scaled["total_cost_usdc"],
                "settlement_profit_by_bucket": source_scaled_profits,
                "worst_case_profit_usdc": min(source_scaled_profits.values()),
                "best_case_profit_usdc": max(source_scaled_profits.values()),
                # Retain the field for reporting compatibility; it is not a
                # gate in source-capital/share copy mode.
                "safety_floor_usdc": 0.0,
            }
        floor_proof_price_mode = "ask_depth_fak"
        floor_proof_best_bid_sum: float | None = None
        if (
            floor_gate_enabled
            and
            floor_plan.get("status") != "ready"
            and bool(managed and managed.get("rows"))
            and all(_finite(row.get("best_bid")) is not None for row in quote_rows)
            and any(
                reason == "copy_follow_floor_solver_not_converged"
                for reason in floor_plan.get("reason_codes") or []
            )
        ):
            # Existing plans are already past protected FAK entry.  A
            # best-bid GTC repair can therefore be proved against the posted
            # limit price even when the current ask ladder cannot certify a
            # new marketable basket.  Use a conservative unlimited depth at
            # each best bid: actual fills are never worse than that limit,
            # while the order ladder may split the missing quantity across
            # lower bids.  This keeps the settlement floor hard and preserves
            # all four legs without pretending that asks are executable.
            best_bid_depth = {
                row["bucket"]: [
                    (float(row["best_bid"]), 1_000_000_000.0)
                ]
                for row in quote_rows
            }
            bid_floor_plan = allocate_settlement_floor(
                buckets=candidate.buckets,
                source_allocations=candidate.allocation_usdc_by_bucket,
                asks_by_bucket=best_bid_depth,
                budget_usdc=_city_budget_cap(config, candidate),
                primary_bucket=primary_bucket,
                secondary_bucket=secondary_bucket,
                safety_floor_usdc=float(execution.get("safety_floor_usdc", 0.01) or 0.01),
                minimum_shares=float(execution.get("minimum_order_shares", 1.0) or 1.0),
                minimum_notional_usdc=float(execution.get("minimum_rebalance_notional_usdc", 0.0) or 0.0),
                strict_share_gap=float(execution.get("strict_share_gap", 1.0) or 1.0),
                maximum_buy_price=float(execution.get("maximum_buy_price", 0.99) or 0.99),
            )
            if bid_floor_plan.get("status") == "ready":
                actual_ask_sum = sum(
                    float(row["best_ask"])
                    for row in quote_rows
                    if _finite(row.get("best_ask")) is not None
                )
                floor_proof_best_bid_sum = sum(
                    float(row["best_bid"]) for row in quote_rows
                )
                floor_plan = {
                    **bid_floor_plan,
                    "best_ask_sum": actual_ask_sum,
                    "floor_proof_price_mode": "best_bid_gtc",
                    "floor_proof_best_bid_sum": floor_proof_best_bid_sum,
                    "ask_floor_reason_codes": list(floor_plan.get("reason_codes") or []),
                }
                floor_proof_price_mode = "best_bid_gtc"
        if floor_plan.get("status") != "ready":
            floor_reason_codes = [
                *local_window_reasons,
                *(
                    floor_plan.get("reason_codes")
                    or [
                        "copy_follow_settlement_floor_not_verified"
                        if floor_gate_enabled
                        else "copy_follow_source_weight_not_verified"
                    ]
                ),
            ]
            floor_hard_block = (
                floor_gate_enabled
                and any(_is_settlement_floor_reason(reason) for reason in floor_reason_codes)
            )
            floor_profits = floor_plan.get("profits") if isinstance(floor_plan.get("profits"), Mapping) else {}
            floor_profit_values = [float(value) for value in floor_profits.values() if _finite(value) is not None]
            return {
                "state": "blocked" if floor_hard_block else "planned",
                "execution_ready": False,
                "execution_status": "blocked_by_settlement_floor" if floor_hard_block else "waiting_for_source_or_clob_evidence",
                "strategy_id": "weather_copy_follow_v1",
                "plan_id": candidate.plan_id,
                "event_slug": candidate.event_slug,
                "event_date": candidate.event_date,
                "city_slug": candidate.city_slug,
                "settlement_floor_hard_gate": floor_gate_enabled,
                "selected_buckets": list(candidate.buckets),
                "main_bucket": primary_bucket,
                "secondary_bucket": secondary_bucket,
                "source_notional_by_bucket": source_values,
                "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket,
                "forecast_context": forecast_context,
                "building_gate": building_gate,
                "quote_status_by_bucket": quote_status_by_bucket,
                "reason_codes": floor_reason_codes,
                "settlement_profit_by_bucket": dict(floor_profits) if floor_profits else None,
                "minimum_settlement_profit_usdc": min(floor_profit_values) if floor_profit_values else None,
                "maximum_settlement_profit_usdc": max(floor_profit_values) if floor_profit_values else None,
                **floor_plan,
            }
        # The CLOB explicit FAK market-buy path accepts taker amounts to at
        # most four decimal places.  Freeze the floor target at that precision
        # before building the order rows, rounding upward and repricing only
        # the tiny added tail so the settlement floor remains conservative.
        execution_quantities = {
            bucket: math.ceil(float(floor_plan["quantities"][bucket]) * 10000.0 - 1e-12) / 10000.0
            for bucket in candidate.buckets
        }
        exact_execution_costs = {
            bucket: float(floor_plan["costs_usdc"][bucket])
            + (execution_quantities[bucket] - float(floor_plan["quantities"][bucket]))
            * float(floor_plan["limit_prices"][bucket])
            for bucket in candidate.buckets
        }
        # The exchange maker amount is cent-precision.  Recompute the
        # settlement proof against the same rounded amounts that the order
        # payload will reserve, then add a common share tail when the cent
        # rounding would otherwise consume the safety floor.
        for _ in range(256):
            execution_costs = {
                bucket: math.ceil(float(exact_execution_costs[bucket]) * 100.0 - 1e-12) / 100.0
                for bucket in candidate.buckets
            }
            execution_total_cost = sum(execution_costs.values())
            execution_profits = {
                bucket: execution_quantities[bucket] - execution_total_cost
                for bucket in candidate.buckets
            }
            if (
                not floor_gate_enabled
                or min(execution_profits.values()) + 1e-7 >= float(floor_plan["safety_floor_usdc"])
            ):
                break
            deficit = float(floor_plan["safety_floor_usdc"]) - min(execution_profits.values())
            increment = max(0.01, math.ceil((deficit + 0.0001) * 10000.0 - 1e-12) / 10000.0)
            for bucket in candidate.buckets:
                execution_quantities[bucket] += increment
                exact_execution_costs[bucket] += increment * float(floor_plan["limit_prices"][bucket])
        else:
            return {
                "state": "blocked",
                "execution_ready": False,
                "execution_status": "blocked_by_settlement_floor",
                "strategy_id": "weather_copy_follow_v1",
                "plan_id": candidate.plan_id,
                "event_slug": candidate.event_slug,
                "event_date": candidate.event_date,
                "city_slug": candidate.city_slug,
                "selected_buckets": list(candidate.buckets),
                "main_bucket": primary_bucket,
                "secondary_bucket": secondary_bucket,
                "allocation_usdc_by_bucket": source_values,
                "forecast_context": forecast_context,
                "building_gate": building_gate,
                "reason_codes": ["copy_follow_settlement_floor_not_met_after_amount_precision"],
                "minimum_settlement_profit_usdc": min(execution_profits.values()),
                "maximum_settlement_profit_usdc": max(execution_profits.values()),
                "settlement_floor_hard_gate": floor_gate_enabled,
            }
        if enforce_city_cap and execution_total_cost > float(_city_budget_cap(config, candidate)) + 1e-7:
            return {
                "state": "blocked",
                "execution_ready": False,
                "execution_status": "blocked_by_settlement_floor" if floor_gate_enabled else "blocked_by_copy_budget",
                "strategy_id": "weather_copy_follow_v1",
                "plan_id": candidate.plan_id,
                "event_slug": candidate.event_slug,
                "event_date": candidate.event_date,
                "city_slug": candidate.city_slug,
                "selected_buckets": list(candidate.buckets),
                "main_bucket": primary_bucket,
                "secondary_bucket": secondary_bucket,
                "source_notional_by_bucket": source_values,
                "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket,
                "forecast_context": forecast_context,
                "building_gate": building_gate,
                "reason_codes": [
                    "copy_follow_settlement_floor_exceeds_city_cap_after_precision_rounding"
                    if floor_gate_enabled
                    else "copy_follow_copy_budget_exceeded_after_precision_rounding"
                ],
                "settlement_floor_hard_gate": floor_gate_enabled,
                **floor_plan,
            }
        if floor_gate_enabled and min(execution_profits.values()) + 1e-7 < float(floor_plan["safety_floor_usdc"]):
            return {
                "state": "blocked",
                "execution_ready": False,
                "execution_status": "blocked_by_settlement_floor",
                "strategy_id": "weather_copy_follow_v1",
                "plan_id": candidate.plan_id,
                "event_slug": candidate.event_slug,
                "event_date": candidate.event_date,
                "city_slug": candidate.city_slug,
                "selected_buckets": list(candidate.buckets),
                "main_bucket": primary_bucket,
                "secondary_bucket": secondary_bucket,
                "source_notional_by_bucket": source_values,
                "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket,
                "forecast_context": forecast_context,
                "building_gate": building_gate,
                "reason_codes": ["copy_follow_settlement_floor_not_met_after_precision_rounding"],
                **floor_plan,
            }
        floor_plan = {
            **floor_plan,
            "quantities": execution_quantities,
            "costs_usdc": execution_costs,
            "total_cost_usdc": execution_total_cost,
            "settlement_profit_by_bucket": execution_profits,
            "worst_case_profit_usdc": min(execution_profits.values()),
            "best_case_profit_usdc": max(execution_profits.values()),
        }
        managed = managed or {
            "holdings_by_bucket": {},
            "net_cost_usdc": 0.0,
            "pending_by_bucket_side": {},
            "open_rows": [],
            "projected_holdings_by_bucket": {},
            "projected_net_cost_usdc": 0.0,
        }
        previous_rebalance = (
            previous_plan.get("rebalance")
            if isinstance(previous_plan, Mapping)
            and isinstance(previous_plan.get("rebalance"), Mapping)
            else {}
        )
        previous_fallback_buckets = []
        if not source_changed and isinstance(previous_rebalance, Mapping):
            previous_fallback_buckets = [
                str(bucket)
                for bucket in previous_rebalance.get("best_bid_fallback_buckets") or []
                if str(bucket) in candidate.buckets
            ]
        prepared: dict[str, Any] = {
            "state": "ready",
            "execution_ready": True,
            "execution_status": (
                "ready_with_settlement_floor"
                if floor_gate_enabled
                else "ready_with_source_capital_share_ratio"
            ),
            "strategy_id": "weather_copy_follow_v1",
            "plan_id": candidate.plan_id,
            "event_slug": candidate.event_slug,
            "event_date": candidate.event_date,
            "city_slug": candidate.city_slug,
            "main_bucket": primary_bucket,
            "secondary_bucket": secondary_bucket,
            "selected_buckets": list(candidate.buckets),
            "source_notional_by_bucket": source_values,
            "source_shares_by_bucket": candidate.source_shares_by_bucket,
            "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket,
            "source_capital_weight_by_bucket": floor_plan.get("source_capital_weight_by_bucket"),
            "source_share_weight_by_bucket": floor_plan.get("source_share_weight_by_bucket"),
            "copy_scale_factor": floor_plan.get("copy_scale_factor"),
            "floor_order_notional_by_bucket": floor_plan["costs_usdc"],
            "total_budget_usdc": float(floor_plan["total_cost_usdc"]),
            "source_reference_total_usdc": candidate.total_allocation_usdc,
            "target_quantities": floor_plan["quantities"],
            "market_price_cap_by_bucket": floor_plan["limit_prices"],
            "best_bid_fallback_buckets": previous_fallback_buckets,
            # Once a plan has submitted any order, later re-entry/rebalance
            # cycles are dynamic best-bid GTC.  A cancelled zero-fill FAK is
            # still evidence that this is no longer the first entry.
            "initial_entry": not bool(managed.get("rows") or []),
            # These are informational projections in the active copy mode;
            # they must never be interpreted as an eligibility gate.
            "settlement_profit_by_bucket": floor_plan["settlement_profit_by_bucket"],
            "worst_case_profit_usdc": floor_plan["worst_case_profit_usdc"],
            "best_case_profit_usdc": floor_plan["best_case_profit_usdc"],
            "minimum_settlement_profit_usdc": floor_plan["worst_case_profit_usdc"],
            "maximum_settlement_profit_usdc": floor_plan["best_case_profit_usdc"],
            "safety_floor_usdc": floor_plan["safety_floor_usdc"],
            "settlement_floor_hard_gate": floor_gate_enabled,
            "best_ask_sum": floor_plan["best_ask_sum"],
            "floor_proof_price_mode": floor_proof_price_mode,
            "floor_proof_best_bid_sum": floor_proof_best_bid_sum,
            "building_gate": building_gate,
            "forecast_context": forecast_context,
            "token_by_bucket": {row["bucket"]: row["yes_token"] for row in quote_rows},
            "quotes_by_bucket": {
                row["bucket"]: {
                    "bids": [
                        [float(price), float(size)]
                        for price, size in (row.get("bids") or [])
                        if _finite(price) is not None and _finite(size) is not None
                    ],
                    "best_bid": row.get("best_bid"),
                    "best_ask": row.get("best_ask"),
                    "observed_at": row.get("observed_at"),
                    "tick_size": row.get("tick_size"),
                    "neg_risk": row.get("neg_risk"),
                }
                for row in quote_rows
            },
            "execution": {
                "order_type": "GTC",
                "all_legs_required": False,
                "buy_price_mode": "best_bid",
                "sell_price_mode": "best_bid",
                "copy_weight_mode": str(
                    execution.get("copy_weight_mode")
                    or "source_capital_scale_and_share_ratio"
                ),
                "exit_liquidity": dict(
                    execution.get("exit_liquidity")
                    if isinstance(execution.get("exit_liquidity"), Mapping)
                    else {}
                ),
                "buy_ladder": dict(buy_ladder) if isinstance(buy_ladder, Mapping) else {},
                "initial_entry_order_type": "FAK" if not managed.get("rows") else "GTC",
                "initial_entry_price_mode": "market" if not managed.get("rows") else "best_bid",
                "source": "official_polymarket_clob_websocket",
                "uses_bayes": False,
                "uses_regime": False,
                "uses_weather_fields": False,
                "forecast_context_display_only": True,
            },
        }
        if local_window_reasons:
            # Keep the solved floor and quote evidence in the plan, while
            # making the window rule a no-entry state.  The report layer hides
            # this city using the reason code; it is intentionally not treated
            # as a reason to skip market/floor computation.
            prepared.update(
                {
                    "state": "blocked",
                    "execution_ready": False,
                    "execution_status": "blocked_by_local_peak_window",
                    "reason_codes": list(local_window_reasons),
                    "local_entry_window_passed": True,
                    "rebalance": {"actions": [], "cancellations": [], "reason_codes": list(local_window_reasons)},
                }
            )
            return prepared
        prepared["rebalance"] = self._desired_actions(
            candidate,
            prepared,
            managed,
            config,
            source_changed=source_changed,
        )
        prepared["exit_liquidity"] = self._desired_exit_liquidity(
            candidate,
            prepared,
            managed,
            config,
        )
        for key in (
            "actual_holdings_by_bucket",
            "actual_net_cost_usdc",
            "actual_settlement_profit_by_bucket",
            "actual_minimum_settlement_profit_usdc",
            "actual_maximum_settlement_profit_usdc",
            "actual_settlement_floor_passed",
            "post_fill_holdings_by_bucket",
            "post_fill_net_cost_usdc",
            "post_fill_settlement_profit_by_bucket",
            "post_fill_minimum_settlement_profit_usdc",
            "post_fill_maximum_settlement_profit_usdc",
            "post_fill_settlement_floor_passed",
            "post_fill_settlement_floor_required",
            "pending_fill_count",
        ):
            if key in prepared["rebalance"]:
                prepared[key] = prepared["rebalance"][key]
        return prepared

    def _decision(self, *, candidate: CopyFollowCandidate, order: Mapping[str, Any], intent_id: str, will_order: bool, reason_codes: list[str], details: Mapping[str, Any]) -> None:
        table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_trade_decisions'"
        ).fetchone()
        if table is None:
            return
        columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(auto_trade_decisions)")}
        side = str(order.get("side") or "BUY").upper()
        decision_id = stable_id("copy_decision_", [candidate.plan_id, order.get("bucket"), side, intent_id])
        values: dict[str, Any] = {
            "run_id": 0,
            "run_date": candidate.event_date,
            "slug": candidate.event_slug,
            "event_date": candidate.event_date,
            "title": "Independent wallet copy-follow YES",
            "outcome": str(order.get("bucket") or ""),
            "side": "YES",
            "action": "entry" if side == "BUY" else "exit",
            "decided_state": "will_order" if will_order else "blocked",
            "will_order": int(will_order),
            "reason_codes_json": _json(reason_codes),
            "reasons_json": _json(reason_codes),
            "details_json": _json({**dict(details), "strategy_id": "weather_copy_follow_v1", "plan_id": candidate.plan_id}),
            "created_at": _now(),
            "correlation_id": stable_id("copy_corr_", candidate.plan_id),
            "fact_snapshot_id": "",
            "posterior_run_id": "",
            "decision_id": decision_id,
            "signal_id": stable_id("copy_signal_", [candidate.plan_id, order.get("bucket"), side, intent_id]),
            "order_intent_id": intent_id,
        }
        selected = {key: value for key, value in values.items() if key in columns}
        if "id" in columns:
            selected.pop("id", None)
        if not selected:
            return
        names = ",".join(selected)
        placeholders = ",".join("?" for _ in selected)
        try:
            self.conn.execute(
                f"INSERT OR IGNORE INTO auto_trade_decisions({names}) VALUES({placeholders})",
                tuple(selected.values()),
            )
        except sqlite3.OperationalError:
            # Some historical test databases carry only the original compact
            # decision surface.  Preserve the audit row there as well.
            compact = {key: values[key] for key in ("run_date", "slug", "event_date", "title", "outcome", "side", "action", "will_order", "details_json", "created_at") if key in columns}
            names = ",".join(compact)
            placeholders = ",".join("?" for _ in compact)
            if compact:
                self.conn.execute(
                    f"INSERT INTO auto_trade_decisions({names}) VALUES({placeholders})",
                    tuple(compact.values()),
                )

    def _enqueue_notification(self, *, plan_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        revision = payload.get("notification_revision")
        if revision is None:
            revision = stable_id("copy_notice_revision_", dict(payload))
        key = stable_id("copy_notice_", [plan_id, event_type, revision])
        now = _now()
        self.conn.execute(
            """INSERT OR IGNORE INTO copy_follow_notifications(
               notification_id,idempotency_key,plan_id,event_type,channel,payload_json,state,last_error,created_at,updated_at)
               VALUES(?,?,?,?,? ,?,'pending',NULL,?,?)""",
            (key, key, plan_id, event_type, "codex_thread", _json(dict(payload)), now, now),
        )

    def _deliver_notifications(self, config: Mapping[str, Any]) -> None:
        notifications = config.get("notifications") if isinstance(config.get("notifications"), Mapping) else {}
        if notifications.get("enabled", True) is not True:
            return
        channel = str(notifications.get("channel") or "codex_thread")
        if channel != "codex_thread":
            raise ValueError("copy_follow_notification_channel_must_be_codex_thread")
        output_path = Path(notifications.get("event_file") or (ROOT / "data" / "copy_follow_codex_notifications.jsonl"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.conn.execute(
            "SELECT notification_id,payload_json FROM copy_follow_notifications WHERE state='pending' ORDER BY created_at LIMIT 16"
        ).fetchall()
        for row in rows:
            notification_id = str(row[0])
            try:
                payload = json.loads(str(row[1] or "{}"))
            except ValueError:
                payload = {}
            try:
                envelope = {"schema": "codex_thread_notification_v1", "created_at": _now(), **payload}
                with output_path.open("a", encoding="utf-8") as handle:
                    handle.write(_json(envelope) + "\n")
                state, error = "sent", ""
            except OSError as exc:
                state, error = "failed", f"notification_file:{type(exc).__name__}:{exc}"
            self.conn.execute(
                "UPDATE copy_follow_notifications SET state=?,last_error=?,updated_at=? WHERE notification_id=? AND state='pending'",
                (state, error or None, _now(), notification_id),
            )
        self.conn.commit()

    def _persist_source_rows(self, rows: Sequence[Mapping[str, Any]], source_user: str, now: str) -> None:
        today = datetime.now().astimezone().date()
        for raw in rows:
            row = dict(raw)
            event_slug = event_slug_from_activity(row)
            identity = parse_event_identity(event_slug)
            bucket = parse_activity_bucket(str(row.get("title") or ""))
            try:
                notional = float(row.get("usdcSize"))
            except (TypeError, ValueError):
                notional = 0.0
            if identity is None or not bucket or notional <= 0:
                continue
            city_slug, event_date = identity
            if str(row.get("type") or "").upper() != "TRADE" or str(row.get("side") or "").upper() != "BUY" or str(row.get("outcome") or "").lower() != "yes":
                continue
            self.conn.execute(
                """INSERT OR IGNORE INTO copy_follow_source_activities(
                   activity_id,source_user,event_slug,event_date,city_slug,bucket,usdc_size,share_size,price,activity_timestamp,payload_json,fetched_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    activity_identity(row), source_user, event_slug, event_date, city_slug, bucket,
                    notional, _finite(row.get("size")), _finite(row.get("price")), int(row.get("timestamp") or 0) or None,
                    _json(row), now,
                ),
            )

    def _insert_plan(self, candidate: CopyFollowCandidate, prepared: Mapping[str, Any], now: str) -> None:
        plan_json = dict(prepared)
        plan_json.setdefault("candidate", {
            "source_user": candidate.source_user,
            "event_slug": candidate.event_slug,
            "event_date": candidate.event_date,
            "city_slug": candidate.city_slug,
            "buckets": list(candidate.buckets),
            "source_notional_by_bucket": candidate.source_notional_by_bucket,
            "source_shares_by_bucket": candidate.source_shares_by_bucket,
            "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket,
        })
        state = "planned" if prepared.get("state") != "blocked" else "planned"
        self.conn.execute(
            """INSERT OR IGNORE INTO copy_follow_plans(
               plan_id,source_user,event_slug,event_date,city_slug,selection_signature,buckets_json,
               source_notional_json,allocation_json,main_bucket,source_total_usdc,total_budget_usdc,state,plan_json,last_error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate.plan_id, candidate.source_user, candidate.event_slug, candidate.event_date, candidate.city_slug,
                candidate.selection_signature, _json(list(candidate.buckets)), _json(candidate.source_notional_by_bucket),
                _json(candidate.allocation_usdc_by_bucket), candidate.main_bucket, candidate.total_source_notional_usdc,
                candidate.total_allocation_usdc, state, _json(plan_json), None, now, now,
            ),
        )

    def _update_plan(self, plan_id: str, *, state: str, plan: Mapping[str, Any], error: str | None = None) -> None:
        self.conn.execute(
            """UPDATE copy_follow_plans
                  SET state=?,plan_json=?,last_error=?,
                      main_bucket=COALESCE(NULLIF(?,''),main_bucket),
                      source_notional_json=COALESCE(NULLIF(?,''),source_notional_json),
                      allocation_json=COALESCE(NULLIF(?,''),allocation_json),
                      total_budget_usdc=CASE WHEN ? > 0 THEN ? ELSE total_budget_usdc END,
                      updated_at=?
                WHERE plan_id=?""",
            (
                state,
                _json(dict(plan)),
                error,
                str(plan.get("main_bucket") or ""),
                _json(plan.get("source_notional_by_bucket") or {}) if plan.get("source_notional_by_bucket") else "",
                _json(plan.get("allocation_usdc_by_bucket") or {}) if plan.get("allocation_usdc_by_bucket") else "",
                float(plan.get("total_budget_usdc") or 0.0),
                float(plan.get("total_budget_usdc") or 0.0),
                _now(),
                plan_id,
            ),
        )

    def _prepare_dynamic_order_rows(self, candidate: CopyFollowCandidate, prepared: Mapping[str, Any], now: str) -> list[tuple[str, dict[str, Any]]]:
        rows: list[tuple[str, dict[str, Any]]] = []
        rebalance = prepared.get("rebalance") if isinstance(prepared.get("rebalance"), Mapping) else {}
        exit_liquidity = prepared.get("exit_liquidity") if isinstance(prepared.get("exit_liquidity"), Mapping) else {}
        action_rows = [
            (raw, "rebalance") for raw in rebalance.get("actions") or []
        ] + [
            (raw, "exit_liquidity") for raw in exit_liquidity.get("actions") or []
        ]
        for raw, role in action_rows:
            item = dict(raw)
            item["role"] = role
            side = str(item.get("side") or "").upper()
            target_key = [candidate.plan_id, item.get("bucket"), side, item.get("quantity"), item.get("limit_price"), item.get("target_quantity"), item.get("current_projected_quantity")]
            idempotency_prefix = (
                EXIT_LIQUIDITY_IDEMPOTENCY_PREFIX
                if role == "exit_liquidity"
                else "copy_dynamic_idem_"
            )
            idempotency_key = stable_id(idempotency_prefix, target_key)
            existing_row = self.conn.execute(
                "SELECT local_order_id,state FROM copy_follow_orders WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing_row is not None and str(existing_row["state"] or "") == "prepared":
                item.update({"order_intent_id": stable_id("copy_dynamic_intent_", [candidate.plan_id, item.get("bucket"), side, idempotency_key]), "idempotency_key": idempotency_key, "order_type": str(item.get("order_type") or "GTC")})
                rows.append((str(existing_row["local_order_id"]), item))
                continue
            if existing_row is not None and str(existing_row["state"] or "") not in {"submit_failed", "cancelled"}:
                continue
            if existing_row is not None:
                retry = self.conn.execute(
                    "SELECT COUNT(*) FROM copy_follow_orders WHERE plan_id=? AND bucket=? AND side=?",
                    (candidate.plan_id, str(item.get("bucket") or ""), side),
                ).fetchone()[0]
                idempotency_key = stable_id(idempotency_prefix, [*target_key, "retry", int(retry or 0)])
            intent_id = stable_id("copy_dynamic_intent_", [candidate.plan_id, item.get("bucket"), side, idempotency_key])
            item.update({"order_intent_id": intent_id, "idempotency_key": idempotency_key, "order_type": str(item.get("order_type") or "GTC")})
            local_order_id = stable_id("copy_order_", [intent_id])
            self.conn.execute(
                """INSERT OR IGNORE INTO copy_follow_orders(
                   local_order_id,plan_id,bucket,token_id,side,limit_price,requested_size,
                   requested_notional_usdc,order_type,idempotency_key,state,exchange_order_id,
                   size_matched,gateway_response_json,last_error,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'prepared',NULL,0,'{}',NULL,?,?)""",
                (
                    local_order_id,
                    candidate.plan_id,
                    str(item.get("bucket") or ""),
                    str(item.get("token") or ""),
                    side,
                    float(item.get("limit_price") or 0.0),
                    float(item.get("quantity") or 0.0),
                    float(item.get("estimated_notional_usdc") or 0.0),
                    str(item["order_type"]),
                    idempotency_key,
                    now,
                    now,
                ),
            )
            self._decision(
                candidate=candidate,
                order=item,
                intent_id=intent_id,
                will_order=True,
                reason_codes=[
                    "copy_follow_exit_liquidity"
                    if role == "exit_liquidity"
                    else "copy_follow_source_four_bucket_selection",
                    (
                        "copy_follow_source_capital_share_ratio"
                        if prepared.get("settlement_floor_hard_gate") is not True
                        else "copy_follow_settlement_floor_target"
                    )
                    if role != "exit_liquidity"
                    else "copy_follow_exit_price_0_997",
                    "copy_follow_best_bid_ladder"
                    if item.get("ladder_level") is not None
                    else "copy_follow_best_bid_limit"
                    if role != "exit_liquidity"
                    else "copy_follow_fixed_gtc_exit",
                ],
                details=prepared,
            )
            rows.append((local_order_id, item))
        return rows

    def _submit_dynamic(
        self,
        candidate: CopyFollowCandidate,
        order_rows: Sequence[tuple[str, dict[str, Any]]],
        *,
        max_parallel_orders: int = 4,
    ) -> dict[str, Any]:
        """Submit independent copy-follow orders concurrently.

        Each order keeps its own idempotency key and the durable row is moved to
        ``submitting`` before any network call.  A timeout/unknown response on
        one leg still produces ``reconcile_required``; concurrency only removes
        avoidable proxy round-trip serialization for the other independent
        legs.  Basket FOK cycles do not use this method.
        """

        started = time.perf_counter()
        rows = list(order_rows)
        if not rows:
            return {"state": "submitted", "orders": [], "timing_ms": {"total_ms": 0.0, "order_count": 0, "order_ms": [], "parallelism": 0}}
        parallelism = max(1, min(8, int(max_parallel_orders or 1)))
        for local_order_id, _order in rows:
            self.conn.execute(
                "UPDATE copy_follow_orders SET state='submitting',updated_at=? WHERE local_order_id=? AND state='prepared'",
                (_now(), local_order_id),
            )
        self.conn.commit()

        def submit_one(index: int, local_order_id: str, order: Mapping[str, Any]) -> dict[str, Any]:
            side = str(order.get("side") or "").upper()
            payload = {
                "schema": SCHEMA_VERSION,
                "strategy_id": "weather_copy_follow_v1",
                "order_intent_id": order["order_intent_id"],
                "idempotency_key": order["idempotency_key"],
                "token_id": str(order["token"]),
                "side": side,
                "outcome_side": "YES",
                "price": float(order["limit_price"]),
                "size": float(order["quantity"]),
                "amount": float(order["quantity"]),
                "notional": float(order["estimated_notional_usdc"]),
                "order_type": str(order.get("order_type") or "GTC"),
                "allow_market": bool(order.get("allow_market")),
                "post_only": False,
                "order_role": str(order.get("role") or "rebalance"),
                "direct_target_order_context": False,
                "copy_follow_parallel_submission": True,
                "copy_follow_plan_id": candidate.plan_id,
                "copy_follow_city": candidate.city_slug,
                "copy_follow_event_date": candidate.event_date,
            }
            order_started = time.perf_counter()
            try:
                response = _response_mapping(self.gateway.place_order(payload))
            except Exception as exc:
                response = {"ok": False, "reconcile_required": True, "error": f"{type(exc).__name__}:{exc}"}
                # A local transport exception can occur after the gateway has
                # durably recorded the idempotency key. Resolve only that exact
                # key; never retry a possibly submitted order.
                try:
                    lookup = _response_mapping(
                        self.gateway.lookup_order(
                            {"idempotency_key": order["idempotency_key"], "order_id": ""}
                        )
                    )
                    if (
                        lookup.get("found") is True
                        and lookup.get("pending") is not True
                        and lookup.get("reconcile_required") is not True
                        and lookup.get("exchange_post_started") is False
                    ):
                        response = {
                            **response,
                            "lookup": lookup,
                            "reconcile_required": False,
                            "exchange_post_started": False,
                            "known_local_rejection": True,
                        }
                except Exception:
                    pass
            return {
                "index": index,
                "local_order_id": local_order_id,
                "order": dict(order),
                "side": side,
                "response": response,
                "timing_ms": _elapsed_ms(order_started),
            }

        completed: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(parallelism, len(rows)), thread_name_prefix="copy-order") as executor:
            futures = {
                executor.submit(submit_one, index, local_order_id, order): index
                for index, (local_order_id, order) in enumerate(rows)
            }
            for future in as_completed(futures):
                try:
                    completed.append(future.result())
                except Exception as exc:
                    index = futures[future]
                    local_order_id, order = rows[index]
                    completed.append({
                        "index": index,
                        "local_order_id": local_order_id,
                        "order": dict(order),
                        "side": str(order.get("side") or "").upper(),
                        "response": {"ok": False, "reconcile_required": True, "error": f"{type(exc).__name__}:{exc}"},
                        "timing_ms": None,
                    })

        results: list[dict[str, Any]] = []
        ambiguous = False
        for item in sorted(completed, key=lambda value: int(value["index"])):
            local_order_id = str(item["local_order_id"])
            order = item["order"]
            response = _response_mapping(item["response"])
            side = str(item["side"] or "").upper()
            matched = _matched_size(response) or 0.0
            exchange_id = str(response.get("orderID") or response.get("order_id") or response.get("id") or "")
            requested_size = float(order["quantity"])
            order_type = str(order.get("order_type") or "GTC").upper()
            ambiguous_response = (
                response.get("pending") is True
                or response.get("reconcile_required") is True
                or (
                    response.get("exchange_post_started") is True
                    and not (
                        order_type == "GTC"
                        and response.get("ok") is True
                        and exchange_id
                        and _order_status(response) in {
                            "live", "open", "posted", "active", "submitted",
                            "matched", "filled", "partially_filled", "partial",
                        }
                    )
                )
            )
            status = _order_status(response)
            state = "reconcile_required" if ambiguous_response else (
                "filled" if response.get("ok") is True and exchange_id and matched + 0.005 >= requested_size
                else "partially_filled" if response.get("ok") is True and exchange_id and matched > 1e-9
                else "open" if response.get("ok") is True and exchange_id
                else "submit_failed"
            )
            ambiguous = ambiguous or ambiguous_response
            self.conn.execute(
                """UPDATE copy_follow_orders
                      SET state=?,exchange_order_id=?,size_matched=?,gateway_response_json=?,last_error=?,updated_at=?
                    WHERE local_order_id=?""",
                (state, exchange_id or None, matched, _json(response), str(response.get("error") or "") or None, _now(), local_order_id),
            )
            results.append({
                "bucket": order.get("bucket"),
                "side": side,
                "state": state,
                "gateway": response,
                "status": status,
                "timing_ms": item.get("timing_ms"),
            })
        self.conn.commit()
        order_timings_ms = [item.get("timing_ms") for item in results if item.get("timing_ms") is not None]
        return {
            "state": "reconcile_required" if ambiguous else "submitted",
            "orders": results,
            "timing_ms": {
                "total_ms": _elapsed_ms(started),
                "order_count": len(results),
                "order_ms": order_timings_ms,
                "parallelism": min(parallelism, len(rows)),
            },
        }

    def run_once(self) -> dict[str, Any]:
        run_started = time.perf_counter()
        timings_ms: dict[str, Any] = {}
        # Quote evidence is scoped to one real run and must never leak into
        # the next source snapshot.
        self._run_quote_cache = None

        def finish(payload: dict[str, Any]) -> dict[str, Any]:
            payload["timings_ms"] = {
                **timings_ms,
                "total_ms": _elapsed_ms(run_started),
            }
            self._run_quote_cache = None
            return payload

        config_started = time.perf_counter()
        try:
            config = _config(self.config_path)
        except Exception as exc:
            timings_ms["config_ms"] = _elapsed_ms(config_started)
            return finish({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": f"config:{type(exc).__name__}:{exc}", "places_orders": False})
        timings_ms["config_ms"] = _elapsed_ms(config_started)
        if not self._enabled(config):
            return finish({"schema": SCHEMA_VERSION, "ok": True, "state": "disabled", "places_orders": False, "reason": "copy_follow_disabled_or_paused"})
        source = config["source"]
        source_user = str(source.get("user") or "").strip().lower()
        source_fetch_started = time.perf_counter()
        trades_executor: ThreadPoolExecutor | None = None
        trades_future = None
        trades_started: float | None = None
        trades_result: list[dict[str, Any]] | None = None
        trades_error: BaseException | None = None
        try:
            if self.activity_fetcher is not None:
                # Compatibility seam for historical tests/manual callers.  It
                # is not used by the production constructor.
                source_rows = self.activity_fetcher(
                    user=source_user,
                    base_url=str(source.get("activity_url") or "https://data-api.polymarket.com/activity"),
                    page_size=int(source.get("page_size", 500) or 500),
                    max_pages=int(source.get("max_pages", 10) or 10),
                    timeout_sec=float(source.get("timeout_sec", 10.0) or 10.0),
                )
                audit_rows = source_rows
                candidates = extract_four_bucket_candidates(
                    source_rows,
                    source_user=source_user,
                    city_cap_usdc=float(config.get("city_cap_usdc") or 30.0),
                    lookahead_days=int(config.get("lookahead_days", 2) or 2),
                )
                source_mode = "activity_compatibility"
            else:
                source_rows = self.positions_fetcher(
                    user=source_user,
                    base_url=str(source.get("positions_url") or "https://data-api.polymarket.com/positions"),
                    page_size=int(source.get("page_size", 500) or 500),
                    max_pages=int(source.get("positions_max_pages", 2) or 2),
                    timeout_sec=float(source.get("positions_timeout_sec", source.get("timeout_sec", 6.0)) or 6.0),
                    include_redeemable=source.get("include_redeemable") is True,
                )
                audit_rows = position_rows_to_activity_rows(source_rows)
                candidates = extract_four_bucket_candidates_from_positions(
                    source_rows,
                    source_user=source_user,
                    city_cap_usdc=float(config.get("city_cap_usdc") or 30.0),
                    lookahead_days=int(config.get("lookahead_days", 2) or 2),
                )
                source_mode = "positions"
        except Exception as exc:
            timings_ms["source_fetch_and_selection_ms"] = _elapsed_ms(source_fetch_started)
            return finish({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": f"source:{type(exc).__name__}:{exc}", "places_orders": False})
        positions_elapsed_ms = _elapsed_ms(source_fetch_started)
        timings_ms["source_positions_ms"] = positions_elapsed_ms
        # The selector only needs recent activity for the exact events found
        # in the current position snapshot.  Start the targeted pagination
        # after those identities are known; the fetcher stops as soon as all
        # required events are covered instead of walking unrelated history.
        if self._city_selection_refresh_due(config):
            required_event_slugs = {candidate.event_slug for candidate in candidates}
            trades_started = time.perf_counter()
            trades_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="copy-city-selection")
            trades_future = trades_executor.submit(
                self._fetch_city_selection_trades,
                config,
                required_event_slugs=required_event_slugs,
            )
            timings_ms["city_selection_trades_started_after_positions"] = True
            try:
                trades_result = list(trades_future.result())
            except BaseException as exc:
                trades_error = exc
            finally:
                trades_executor.shutdown(wait=True)
            timings_ms["city_selection_trades_ms"] = _elapsed_ms(trades_started)
            timings_ms["city_selection_trades_ok"] = trades_error is None
        else:
            timings_ms["city_selection_trades_started_after_positions"] = False
        timings_ms["source_fetch_and_selection_ms"] = _elapsed_ms(source_fetch_started)
        city_selection_started = time.perf_counter()
        candidates, city_selection = self._select_city_candidates(
            candidates,
            source_rows,
            config,
            prefetched_trades=trades_result,
            prefetched_trades_error=trades_error,
        )
        timings_ms["city_selection_ms"] = _elapsed_ms(city_selection_started)
        if city_selection.get("state") in {"blocked", "stale_previous_selection_unavailable"}:
            return finish({
                "schema": SCHEMA_VERSION,
                "ok": True,
                "state": "blocked",
                "places_orders": False,
                "source_user": source_user,
                "source_mode": source_mode,
                "candidate_count": 0,
                "city_selection": city_selection,
                "reason_codes": list(city_selection.get("reason_codes") or ["copy_follow_city_selection_unavailable"]),
                "checked_at": _now(),
            })
        # The positions endpoint can return hundreds of unrelated/open rows.
        # Persist only rows belonging to an exact four-bucket candidate; the
        # rest cannot affect this copy-follow decision and made the cold-run
        # SQLite phase unnecessarily expensive.
        candidate_keys = {
            (candidate.event_slug, bucket)
            for candidate in candidates
            for bucket in candidate.buckets
        }
        audit_rows = [
            row
            for row in audit_rows
            if (
                event_slug_from_activity(row),
                parse_activity_bucket(str(row.get("title") or "")),
            ) in candidate_keys
        ]
        now = _now()
        persist_started = time.perf_counter()
        # Use a savepoint instead of starting a top-level transaction.  The
        # production connection is normally idle here, while isolated callers
        # (and the cutover/bootstrap path) may have an outer transaction open
        # after materialising the registry.  ``BEGIN IMMEDIATE`` is invalid
        # inside that outer transaction and made an otherwise valid run fail
        # before quote preparation.  A savepoint preserves the atomic source
        # persistence boundary in both cases without committing a caller's
        # surrounding work.
        source_savepoint = "copy_follow_source_persist"
        self.conn.execute(f"SAVEPOINT {source_savepoint}")
        try:
            self._persist_source_rows(audit_rows, source_user, now)
            self.conn.execute(f"RELEASE SAVEPOINT {source_savepoint}")
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {source_savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {source_savepoint}")
            raise
        timings_ms["source_persist_ms"] = _elapsed_ms(persist_started)
        results: list[dict[str, Any]] = []
        mutations = 0
        seen_plan_ids: set[str] = set()
        timings_ms["candidate_count"] = len(candidates)
        timings_ms["order_reconcile_ms"] = 0.0
        timings_ms["prepare_ms"] = 0.0
        timings_ms["cancel_ms"] = 0.0
        timings_ms["submit_ms"] = 0.0
        timings_ms["quote_timing_ms"] = []
        enforce_city_cap = (
            isinstance(config.get("execution"), Mapping)
            and config["execution"].get("enforce_city_cap", False) is True
        )
        quote_prime_started = time.perf_counter()
        try:
            quote_prime = self._prime_run_quotes(candidates, config)
        except Exception as exc:
            # Priming is an optimization, never an authorization path. Do
            # not fall back to serialized per-city WS connections after a
            # failure; retain fail-closed quote evidence for this run.
            self._run_quote_cache = {} if self._default_quote_reader else None
            quote_prime = {
                "total_ms": _elapsed_ms(quote_prime_started),
                "requested_token_count": 0,
                "local_token_count": 0,
                "target_token_count": 0,
                "resolved_token_count": 0,
                "connection_count": 0,
                "error": f"{type(exc).__name__}:{exc}",
            }
        timings_ms["quote_prime"] = quote_prime
        for candidate in candidates:
            existing = self._existing_plan(candidate)
            if enforce_city_cap and existing is None and not self._city_cap_available(candidate, _city_budget_cap(config, candidate)):
                results.append({"plan_id": candidate.plan_id, "state": "blocked", "reason_codes": ["copy_follow_city_cap_reserved"]})
                continue
            if existing is None:
                # Publish the exact four-bucket identity before the first
                # quote precheck.  The WS listener uses planned copy-follow
                # rows to prioritize these tokens under its bounded asset
                # subscription; waiting until _prepare() completes makes a
                # first-seen candidate invisible to that priority path.
                self._insert_plan(
                    candidate,
                    {
                        "state": "planned",
                        "strategy_id": "weather_copy_follow_v1",
                        "plan_id": candidate.plan_id,
                        "event_slug": candidate.event_slug,
                        "event_date": candidate.event_date,
                        "city_slug": candidate.city_slug,
                        "selected_buckets": list(candidate.buckets),
                        "main_bucket": candidate.main_bucket,
                        "source_notional_by_bucket": candidate.source_notional_by_bucket,
                        "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket,
                        "total_budget_usdc": candidate.total_allocation_usdc,
                        "quote_precheck": "pending",
                    },
                    now,
                )
                self.conn.commit()
            if existing is not None and str(existing["state"] or "") == "reconcile_required":
                reconcile_started = time.perf_counter()
                reconciliation = self._reconcile_unknown_orders(candidate.plan_id)
                timings_ms["order_reconcile_ms"] += _elapsed_ms(reconcile_started)
                if not reconciliation.get("ok"):
                    seen_plan_ids.add(candidate.plan_id)
                    results.append({"plan_id": candidate.plan_id, "state": "reconcile_required", "reason_codes": reconciliation.get("reason_codes") or ["copy_follow_manual_reconcile_required"], "reconciliation": reconciliation})
                    continue
                recovered_plan = json.loads(str(existing["plan_json"] or "{}")) if existing["plan_json"] else {}
                recovered_plan["reconciliation"] = reconciliation
                self._update_plan(candidate.plan_id, state="submitted", plan=recovered_plan, error=None)
                existing = self._existing_plan(candidate)
                self._enqueue_notification(
                    plan_id=candidate.plan_id,
                    event_type="reconcile_resolved",
                    payload={"event_type": "reconcile_resolved", "notification_revision": stable_id("copy_reconcile_resolved_", reconciliation), "message": f"[跟单幂等核对完成] {candidate.city_slug} {candidate.event_date} 已确认原订单身份，恢复后续差异计算", "reconciliation": reconciliation},
                )
            seen_plan_ids.add(candidate.plan_id)
            existing_state = str(existing["state"] or "") if existing is not None else "planned"
            source_changed = False
            if existing is not None:
                try:
                    previous_source = json.loads(str(existing["source_notional_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    previous_source = {}
                source_changed = any(
                    abs(float(previous_source.get(bucket, 0.0) or 0.0) - float(candidate.source_notional_by_bucket.get(bucket, 0.0) or 0.0)) > 1e-8
                    for bucket in candidate.buckets
                )
            if existing is not None and existing_state in {"submitted", "submitting"} and self._legacy_fok_plan_requires_reconcile(candidate.plan_id):
                legacy_plan = json.loads(str(existing["plan_json"] or "{}")) if existing["plan_json"] else {}
                reason = "copy_follow_legacy_fok_plan_requires_manual_reconcile"
                legacy_plan["migration_guard"] = reason
                self._update_plan(candidate.plan_id, state="reconcile_required", plan=legacy_plan, error=reason)
                self._enqueue_notification(
                    plan_id=candidate.plan_id,
                    event_type="reconcile_required",
                    payload={"event_type": "reconcile_required", "notification_revision": reason, "message": f"[跟单迁移阻断] {candidate.city_slug} 已发现旧 FOK 跟单记录，停止新 GTC 动作，需先人工核对旧仓位", "reason": reason},
                )
                results.append({"plan_id": candidate.plan_id, "state": "reconcile_required", "reason_codes": [reason]})
                continue
            if existing is not None and existing_state in {"submitted", "submitting"}:
                sync_started = time.perf_counter()
                sync = self._sync_open_orders(candidate.plan_id)
                timings_ms["order_reconcile_ms"] += _elapsed_ms(sync_started)
                if not sync.get("ok"):
                    plan = json.loads(str(existing["plan_json"] or "{}")) if existing["plan_json"] else {}
                    plan["order_sync"] = sync
                    self._update_plan(candidate.plan_id, state="reconcile_required", plan=plan, error=",".join(sync["reason_codes"]))
                    self._enqueue_notification(
                        plan_id=candidate.plan_id,
                        event_type="reconcile_required",
                        payload={"event_type": "reconcile_required", "notification_revision": stable_id("copy_reconcile_", sync["reason_codes"]), "message": f"[跟单阻断] {candidate.city_slug} {candidate.event_date} 订单状态不明确，已停止新动作：{sync['reason_codes']}", "sync": sync},
                    )
                    results.append({"plan_id": candidate.plan_id, "state": "reconcile_required", "reason_codes": sync["reason_codes"]})
                    continue
            managed = self._managed_state(candidate.plan_id)
            prepare_started = time.perf_counter()
            self._last_quote_timing = {}
            previous_plan: dict[str, Any] = {}
            if existing is not None and existing["plan_json"]:
                try:
                    decoded_previous_plan = json.loads(str(existing["plan_json"] or "{}"))
                    if isinstance(decoded_previous_plan, Mapping):
                        previous_plan = dict(decoded_previous_plan)
                except (TypeError, ValueError, json.JSONDecodeError):
                    previous_plan = {}
            prepared = self._prepare(
                candidate,
                config,
                managed=managed,
                source_changed=source_changed,
                previous_plan=previous_plan,
            )
            # Preserve any in-flight copy order while the source allocation
            # is unchanged.  The floor solver may legitimately produce a
            # different *minimum* target as quotes move, but that is not a
            # source-ratio change and must not cancel a live GTC/FAK order or
            # create a replacement.  A real source-ratio change is the
            # explicit exception: it may cancel the old direction and rebuild
            # the copied allocation.
            pending_copy_orders = [
                row for row in managed.get("open_rows") or []
                if str(row.get("side") or "").upper() in {"BUY", "SELL"}
                and not _is_exit_liquidity_order(row)
            ]
            existing_rebalance = prepared.get("rebalance") if isinstance(prepared.get("rebalance"), Mapping) else {}
            existing_rebalance_reasons = list(existing_rebalance.get("reason_codes") or [])
            existing_floor_block = any(_is_settlement_floor_reason(reason) for reason in existing_rebalance_reasons)
            best_bid_fallback_active = bool(
                existing_rebalance.get("best_bid_fallback_active")
                or existing_rebalance.get("best_bid_fallback_buckets")
            )
            if (
                prepared.get("state") == "ready"
                and pending_copy_orders
                and not source_changed
                and not existing_floor_block
                and not best_bid_fallback_active
            ):
                remaining_actions = [
                    dict(action)
                    for action in existing_rebalance.get("actions") or []
                ]
                prepared = {
                    **prepared,
                    "rebalance": {
                        "actions": remaining_actions,
                        "cancellations": [],
                        "reason_codes": ["copy_follow_waiting_for_fill_reconciliation"],
                    },
                }
            timings_ms["prepare_ms"] += _elapsed_ms(prepare_started)
            quote_timing = dict(self._last_quote_timing)
            if quote_timing:
                timings_ms["quote_timing_ms"].append({"plan_id": candidate.plan_id, **quote_timing})
            if existing is None:
                self._insert_plan(candidate, prepared, now)
            if prepared.get("state") != "ready":
                reason_codes = list(prepared.get("reason_codes") or ["copy_follow_plan_blocked"])
                settlement_floor_block = (
                    prepared.get("settlement_floor_hard_gate") is True
                    and any(_is_settlement_floor_reason(reason) for reason in reason_codes)
                )
                # A stale source stops outstanding BUY orders but does not
                # liquidate an already-held basket merely because the source
                # paused.
                cancel_results: list[dict[str, Any]] = []
                if (
                    existing is not None
                    and (
                        settlement_floor_block
                        or str(existing["state"] or "") in {"submitted", "submitting"}
                    )
                    and (
                        settlement_floor_block
                        or "copy_follow_source_not_actively_building" in reason_codes
                        or "copy_follow_local_peak_window_passed" in reason_codes
                        or "copy_follow_local_event_date_expired" in reason_codes
                    )
                ):
                    for row in managed.get("open_rows") or []:
                        if not settlement_floor_block and str(row.get("side") or "").upper() != "BUY":
                            continue
                        cancel_result = self._cancel_order(row)
                        cancel_results.append(cancel_result)
                        mutations += 1
                        if cancel_result.get("state") == "reconcile_required":
                            reason_codes.append("copy_follow_cancel_reconcile_required")
                            break
                hard_block = any(_copy_follow_hard_reason(reason) for reason in reason_codes)
                plan_state = (
                    "reconcile_required"
                    if any(item.get("state") == "reconcile_required" for item in cancel_results)
                    else "blocked"
                    if settlement_floor_block
                    else "submitted"
                    if existing is not None and str(existing["state"] or "") in {"submitted", "submitting"}
                    else "planned"
                )
                prepared = {**prepared, "cancel_results": cancel_results}
                self._update_plan(candidate.plan_id, state=plan_state, plan=prepared, error=",".join(sorted(set(reason_codes))))
                event_type = "settlement_floor_gate" if settlement_floor_block else "source_gate" if hard_block else "execution_wait"
                label = "结算地板硬门" if settlement_floor_block else "保本/核心硬门" if hard_block else "盘口执行提示"
                self._enqueue_notification(
                    plan_id=candidate.plan_id,
                    event_type=event_type,
                    payload={
                        "event_type": event_type,
                        "notification_revision": stable_id("copy_gate_", [reason_codes, prepared.get("building_gate")]),
                        "message": f"[跟单{label}] {candidate.city_slug} {candidate.event_date} 四桶方案={prepared.get('selected_buckets')} 主桶={prepared.get('main_bucket')}：{reason_codes}",
                        "source_notional_by_bucket": candidate.source_notional_by_bucket,
                        "main_bucket": prepared.get("main_bucket") or candidate.main_bucket,
                        "secondary_bucket": prepared.get("secondary_bucket"),
                        "building_gate": prepared.get("building_gate"),
                        "cancel_results": cancel_results,
                        "plan": prepared,
                    },
                )
                for bucket in candidate.buckets:
                    self._decision(candidate=candidate, order={"bucket": bucket}, intent_id="", will_order=False, reason_codes=reason_codes, details=prepared)
                self.conn.commit()
                result_state = "blocked" if hard_block and plan_state == "planned" else plan_state
                results.append({"plan_id": candidate.plan_id, "state": result_state, "execution_status": prepared.get("execution_status"), "reason_codes": reason_codes, "selected_buckets": list(candidate.buckets), "main_bucket": prepared.get("main_bucket") or candidate.main_bucket, "secondary_bucket": prepared.get("secondary_bucket"), "source_notional_by_bucket": candidate.source_notional_by_bucket, "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket, "minimum_settlement_profit_usdc": prepared.get("minimum_settlement_profit_usdc"), "maximum_settlement_profit_usdc": prepared.get("maximum_settlement_profit_usdc"), "cancel_results": cancel_results})
                continue
            rebalance = prepared.get("rebalance") if isinstance(prepared.get("rebalance"), Mapping) else {}
            rebalance_reason_codes = list(rebalance.get("reason_codes") or [])
            settlement_floor_block = (
                prepared.get("settlement_floor_hard_gate") is True
                and any(_is_settlement_floor_reason(reason) for reason in rebalance_reason_codes)
            )
            if settlement_floor_block:
                cancel_started = time.perf_counter()
                cancel_results = self._cancel_open_orders(managed)
                mutations += len(cancel_results)
                timings_ms["cancel_ms"] += _elapsed_ms(cancel_started)
                cancel_unknown = any(item.get("state") == "reconcile_required" for item in cancel_results)
                prepared = {
                    **prepared,
                    "state": "reconcile_required" if cancel_unknown else "blocked",
                    "execution_ready": False,
                    "execution_status": "reconcile_required" if cancel_unknown else "blocked_by_settlement_floor",
                    "reason_codes": sorted(set(rebalance_reason_codes)),
                    "cancel_results": cancel_results,
                    "rebalance": {
                        **dict(rebalance),
                        "actions": [],
                        "cancellations": [],
                    },
                }
                plan_state = "reconcile_required" if cancel_unknown else "blocked"
                self._update_plan(
                    candidate.plan_id,
                    state=plan_state,
                    plan=prepared,
                    error=",".join(sorted(set(rebalance_reason_codes))),
                )
                self._enqueue_notification(
                    plan_id=candidate.plan_id,
                    event_type="settlement_floor_gate",
                    payload={
                        "event_type": "settlement_floor_gate",
                        "notification_revision": stable_id("copy_floor_gate_", [rebalance_reason_codes, cancel_results]),
                        "message": f"[跟单结算地板硬门] {candidate.city_slug} {candidate.event_date} 已停止新增订单并撤销未成交挂单：{sorted(set(rebalance_reason_codes))}",
                        "selected_buckets": list(candidate.buckets),
                        "main_bucket": prepared.get("main_bucket") or candidate.main_bucket,
                        "secondary_bucket": prepared.get("secondary_bucket"),
                        "cancel_results": cancel_results,
                        "plan": prepared,
                    },
                )
                for bucket in candidate.buckets:
                    self._decision(
                        candidate=candidate,
                        order={"bucket": bucket},
                        intent_id="",
                        will_order=False,
                        reason_codes=sorted(set(rebalance_reason_codes)),
                        details=prepared,
                    )
                self.conn.commit()
                results.append({
                    "plan_id": candidate.plan_id,
                    "state": plan_state,
                    "execution_status": prepared["execution_status"],
                    "reason_codes": sorted(set(rebalance_reason_codes)),
                    "selected_buckets": list(candidate.buckets),
                    "main_bucket": prepared.get("main_bucket") or candidate.main_bucket,
                    "secondary_bucket": prepared.get("secondary_bucket"),
                    "minimum_settlement_profit_usdc": prepared.get("minimum_settlement_profit_usdc"),
                    "maximum_settlement_profit_usdc": prepared.get("maximum_settlement_profit_usdc"),
                    "cancel_results": cancel_results,
                })
                continue
            exit_liquidity = prepared.get("exit_liquidity") if isinstance(prepared.get("exit_liquidity"), Mapping) else {}
            pending_cancellations = [
                *(rebalance.get("cancellations") or []),
                *(exit_liquidity.get("cancellations") or []),
            ]
            cancel_results: list[dict[str, Any]] = []
            cancel_started = time.perf_counter()
            for row in pending_cancellations:
                cancel_result = self._cancel_order(row)
                cancel_results.append(cancel_result)
                mutations += 1
                if cancel_result.get("state") == "reconcile_required":
                    break
            timings_ms["cancel_ms"] += _elapsed_ms(cancel_started)
            if any(item.get("state") == "reconcile_required" for item in cancel_results):
                prepared = {**prepared, "cancel_results": cancel_results}
                self._update_plan(candidate.plan_id, state="reconcile_required", plan=prepared, error="copy_follow_cancel_confirmation_unknown")
                results.append({"plan_id": candidate.plan_id, "state": "reconcile_required", "cancel_results": cancel_results})
                continue
            # After an opposite order is cancelled, wait one cycle and
            # re-read its fill state before posting the new direction.
            if cancel_results:
                prepared = {
                    **prepared,
                    "cancel_results": cancel_results,
                    "rebalance": {**dict(rebalance), "actions": []},
                    "exit_liquidity": {**dict(exit_liquidity), "actions": []},
                }
                self._update_plan(candidate.plan_id, state="submitted", plan=prepared)
                self._enqueue_notification(
                    plan_id=candidate.plan_id,
                    event_type="rebalance",
                    payload={"event_type": "rebalance", "notification_revision": stable_id("copy_cancel_", cancel_results), "message": f"[跟单调整] {candidate.city_slug} {candidate.event_date} 先撤销反向挂单，下一轮重新计算", "source_notional_by_bucket": candidate.source_notional_by_bucket, "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket, "rebalance": prepared.get("rebalance"), "cancel_results": cancel_results, "plan": prepared},
                )
                self.conn.commit()
                results.append({"plan_id": candidate.plan_id, "state": "submitted", "cancel_results": cancel_results})
                continue
            order_rows = self._prepare_dynamic_order_rows(candidate, prepared, now)
            prepared = {**prepared, "order_rows": [{"local_order_id": local_id, **item} for local_id, item in order_rows]}
            self._update_plan(candidate.plan_id, state="submitted", plan=prepared)
            self.conn.commit()
            submit_started = time.perf_counter()
            execution_config = config.get("execution") if isinstance(config.get("execution"), Mapping) else {}
            try:
                parallel_order_submits = max(
                    1,
                    min(8, int(execution_config.get("max_parallel_order_submits", 4) or 4)),
                )
            except (TypeError, ValueError):
                parallel_order_submits = 4
            submit_result = (
                self._submit_dynamic(
                    candidate,
                    order_rows,
                    max_parallel_orders=parallel_order_submits,
                )
                if order_rows
                else {"state": "submitted", "orders": []}
            )
            timings_ms["submit_ms"] += _elapsed_ms(submit_started)
            self._update_plan(candidate.plan_id, state="reconcile_required" if submit_result.get("state") == "reconcile_required" else "submitted", plan={**prepared, "submit_result": submit_result}, error="copy_follow_order_cycle_requires_reconcile" if submit_result.get("state") == "reconcile_required" else None)
            if order_rows:
                mutations += len(order_rows)
                notification_revision = stable_id("copy_rebalance_", [prepared.get("target_quantities"), prepared.get("rebalance"), submit_result])
                self._enqueue_notification(
                    plan_id=candidate.plan_id,
                    event_type="rebalance",
                    payload={
                        "event_type": "rebalance",
                        "notification_revision": notification_revision,
                        "message": (
                            f"[跟单调整] {candidate.city_slug} {candidate.event_date} "
                            f"主桶={prepared.get('main_bucket')} "
                            f"动作={[(item.get('side'), item.get('bucket'), item.get('quantity'), item.get('limit_price')) for _local, item in order_rows]} "
                            f"当前实际最低/最高结算利润={prepared.get('actual_minimum_settlement_profit_usdc')}/{prepared.get('actual_maximum_settlement_profit_usdc')}；"
                            f"本批全部成交后最低/最高={prepared.get('post_fill_minimum_settlement_profit_usdc')}/{prepared.get('post_fill_maximum_settlement_profit_usdc')} "
                            f"TAF={_forecast_notice_text(prepared.get('forecast_context') or {}, 'taf')} "
                            f"ECMWF={_forecast_notice_text(prepared.get('forecast_context') or {}, 'ecmwf')} "
                            f"Open-Meteo={_forecast_notice_text(prepared.get('forecast_context') or {}, 'open_meteo')}"
                        ),
                        "source_notional_by_bucket": candidate.source_notional_by_bucket,
                        "allocation_usdc_by_bucket": candidate.allocation_usdc_by_bucket,
                        "current_position": prepared.get(
                            "actual_holdings_by_bucket",
                            managed.get("holdings_by_bucket"),
                        ),
                        # This is an intent-only view of outstanding orders;
                        # it is explicitly not settlement evidence.
                        "pending_intent_position": managed.get("projected_holdings_by_bucket"),
                        "post_fill_position": prepared.get("post_fill_holdings_by_bucket"),
                        "target_position": prepared.get("target_quantities"),
                        "rebalance": prepared.get("rebalance"),
                        "exit_liquidity": prepared.get("exit_liquidity"),
                        "submit_result": submit_result,
                        "plan": prepared,
                    },
                )
            self.conn.commit()
            results.append(
                {
                    "plan_id": candidate.plan_id,
                    **submit_result,
                    "selected_buckets": list(candidate.buckets),
                    "main_bucket": prepared.get("main_bucket") or candidate.main_bucket,
                    "secondary_bucket": prepared.get("secondary_bucket"),
                    "source_notional_by_bucket": candidate.source_notional_by_bucket,
                    "source_shares_by_bucket": candidate.source_shares_by_bucket,
                    "source_capital_weight_by_bucket": prepared.get("source_capital_weight_by_bucket"),
                    "source_share_weight_by_bucket": prepared.get("source_share_weight_by_bucket"),
                    "copy_scale_factor": prepared.get("copy_scale_factor"),
                    "target_quantities": prepared.get("target_quantities"),
                    "minimum_settlement_profit_usdc": prepared.get("minimum_settlement_profit_usdc"),
                    "maximum_settlement_profit_usdc": prepared.get("maximum_settlement_profit_usdc"),
                    "settlement_floor_hard_gate": prepared.get("settlement_floor_hard_gate") is True,
                    "rebalance": prepared.get("rebalance"),
                }
            )
            if submit_result.get("state") == "reconcile_required":
                break
        # If the source no longer presents an exact four-bucket active
        # selection, stop outstanding BUY orders for its old plans.  Held
        # positions remain untouched for manual or later strategy handling.
        stale_rows = self.conn.execute(
            """SELECT * FROM copy_follow_plans
               WHERE source_user=? AND state='submitted'""",
            (source_user,),
        ).fetchall()
        for plan in stale_rows:
            plan_id = str(plan["plan_id"])
            if plan_id in seen_plan_ids:
                continue
            sync = self._sync_open_orders(plan_id)
            if not sync.get("ok"):
                self._update_plan(plan_id, state="reconcile_required", plan={"order_sync": sync}, error=",".join(sync["reason_codes"]))
                continue
            managed = self._managed_state(plan_id)
            cancel_results = []
            for row in managed.get("open_rows") or []:
                if str(row.get("side") or "").upper() != "BUY":
                    continue
                cancel_result = self._cancel_order(row)
                cancel_results.append(cancel_result)
                mutations += 1
            if cancel_results:
                self._enqueue_notification(
                    plan_id=plan_id,
                    event_type="source_gate",
                    payload={"event_type": "source_gate", "notification_revision": stable_id("copy_stale_", cancel_results), "message": f"[跟单停止建仓] 源钱包不再保持四桶选择，已停止城市计划 {plan['city_slug']} 的未成交买单", "cancel_results": cancel_results},
                )
                self.conn.commit()
        self._deliver_notifications(config)
        report_results = [item for item in results[-20:] if _copy_follow_report_visible(item)]
        return finish({
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "active",
            "places_orders": bool(mutations),
            "source_user": source_user,
            "source_mode": source_mode,
            "candidate_count": len(candidates),
            "city_selection": city_selection,
            "new_plan_results": report_results,
            "expired_plan_count": len(results[-20:]) - len(report_results),
            "checked_at": _now(),
        })


__all__ = ["CopyFollowProductionEngine", "SCHEMA_VERSION"]
