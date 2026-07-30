"""Pure contracts for the AMSC/AWC reported-temperature trajectory-NO lane.

This module deliberately knows nothing about Bayes, forecasts, TAF, physical
weather fields, UI state, SQLite, or order submission.  It converts one
settlement-compatible METAR/SPECI temperature into a conservative market-unit
hard lower bound and selects the nearest already-impossible lower bucket.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "reported_trajectory_no_runtime_v1"
REPORT_RE = re.compile(
    r"\b(?P<kind>METAR|SPECI)(?:\s+(?:COR|AMD))?\s+"
    r"(?P<station>[A-Z0-9]{4})\s+(?P<day>\d{2})(?P<hour>\d{2})(?P<minute>\d{2})Z\b"
)
TEMP_RE = re.compile(r"(?<![A-Z0-9])(?P<temp>M?\d{2}|//)/(?P<dew>M?\d{2}|//)(?![A-Z0-9])")
SINGLE_BUCKET_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\s*$", re.I)
LOW_BUCKET_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\s*(?:or\s+below|or\s+lower|or\s+less|and\s+below)\s*$",
    re.I,
)
HIGH_BUCKET_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\s*(?:or\s+higher|or\s+above|or\s+more|and\s+above)\s*$",
    re.I,
)
RANGE_BUCKET_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*°?\s*([CF])\s*$",
    re.I,
)


@dataclass(frozen=True)
class ParsedReport:
    report_type: str
    station_id: str
    day: int
    hour: int
    minute: int
    reported_temperature_c: int
    raw_text: str
    raw_sha256: str


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def identity(prefix: str, value: Any, *, length: int = 32) -> str:
    return prefix + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()[:length]


def _signed_integer(value: str) -> int:
    text = str(value or "").strip().upper()
    if not text or text == "//":
        raise ValueError("reported_temperature_missing")
    sign = -1 if text.startswith("M") else 1
    digits = text[1:] if sign < 0 else text
    if not digits.isdigit():
        raise ValueError("reported_temperature_invalid")
    return sign * int(digits)


def parse_metar_temperature(raw_text: str, *, expected_station: str = "") -> ParsedReport:
    """Parse only report identity/time and the mandatory METAR temp group.

    The parser stops before remarks and does not parse dew point, clouds,
    precipitation, wind, visibility, TAF, or any physical-regime field.
    """

    raw = " ".join(str(raw_text or "").replace("\r", " ").replace("\n", " ").split()).rstrip("=")
    header = REPORT_RE.search(raw)
    if header is None:
        raise ValueError("metar_speci_header_invalid")
    station = header.group("station").upper()
    expected = str(expected_station or "").strip().upper()
    if expected and station != expected:
        raise ValueError("metar_station_mismatch")
    # Temperature precedes RMK.  Excluding remarks prevents a T-group or
    # unrelated slash token from being mistaken for the settlement reading.
    body = raw[header.end() :].split(" RMK ", 1)[0]
    match = TEMP_RE.search(body)
    if match is None:
        raise ValueError("metar_temperature_group_missing")
    temperature = _signed_integer(match.group("temp"))
    return ParsedReport(
        report_type=header.group("kind").upper(),
        station_id=station,
        day=int(header.group("day")),
        hour=int(header.group("hour")),
        minute=int(header.group("minute")),
        reported_temperature_c=temperature,
        raw_text=raw,
        raw_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def conservative_market_temperature(reported_temperature_c: float, unit: str) -> int:
    """Return a lower bound that cannot overstate the settlement-unit value.

    METAR's main temperature group is integral Celsius.  Celsius markets can
    consume it directly.  Fahrenheit markets use floor(C*9/5+32), deliberately
    one-sided so rounding differences at the resolution website cannot turn a
    supposedly impossible bucket back into a possible one.
    """

    try:
        value = float(reported_temperature_c)
    except (TypeError, ValueError) as exc:
        raise ValueError("reported_temperature_non_numeric") from exc
    if not math.isfinite(value):
        raise ValueError("reported_temperature_non_finite")
    normalized = str(unit or "").strip().upper()
    if normalized == "C":
        return math.floor(value)
    if normalized == "F":
        return math.floor(value * 9.0 / 5.0 + 32.0)
    raise ValueError("market_temperature_unit_unsupported")


def parse_bucket_interval(label: str) -> dict[str, Any]:
    text = str(label or "").strip()
    range_match = RANGE_BUCKET_RE.match(text)
    if range_match is not None:
        first, second = float(range_match.group(1)), float(range_match.group(2))
        lower, upper = sorted((first, second))
        if abs(lower - round(lower)) > 1e-9 or abs(upper - round(upper)) > 1e-9:
            raise ValueError(f"market_bucket_range_not_whole_degree:{text}")
        return {
            "label": text,
            "unit": range_match.group(3).upper(),
            "lower": lower,
            "upper": upper,
            "kind": "range",
        }
    for kind, regex in (("lower_tail", LOW_BUCKET_RE), ("upper_tail", HIGH_BUCKET_RE), ("single", SINGLE_BUCKET_RE)):
        match = regex.match(text)
        if match is None:
            continue
        value = float(match.group(1))
        unit = match.group(2).upper()
        if kind == "lower_tail":
            lower, upper = None, value
        elif kind == "upper_tail":
            lower, upper = value, None
        else:
            lower = upper = value
        return {"label": text, "unit": unit, "lower": lower, "upper": upper, "kind": kind}
    raise ValueError(f"market_bucket_label_unsupported:{text}")


def normalize_market_buckets(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    units: set[str] = set()
    for raw in rows:
        label = str(raw.get("outcome") or raw.get("label") or raw.get("bucket") or "").strip()
        if not label or label in seen:
            raise ValueError("market_bucket_identity_invalid")
        interval = parse_bucket_interval(label)
        units.add(interval["unit"])
        seen.add(label)
        normalized.append({**dict(raw), **interval})
    if not normalized or len(units) != 1:
        raise ValueError("market_bucket_unit_or_coverage_invalid")
    if sum(1 for row in normalized if row["kind"] == "lower_tail") != 1:
        raise ValueError("market_lower_tail_bucket_missing_or_duplicate")
    if sum(1 for row in normalized if row["kind"] == "upper_tail") != 1:
        raise ValueError("market_upper_tail_bucket_missing_or_duplicate")
    ordered = sorted(normalized, key=lambda row: (-math.inf if row["lower"] is None else float(row["lower"])))
    # Outcome ladders are whole-unit contiguous.  Reject silent gaps/overlaps;
    # a market rule change must be understood before it can trade.
    prior_upper: float | None = None
    for index, row in enumerate(ordered):
        if index == 0:
            prior_upper = float(row["upper"])
            continue
        lower = float(row["lower"])
        if prior_upper is None or abs(lower - (prior_upper + 1.0)) > 1e-9:
            raise ValueError("market_bucket_ladder_not_contiguous")
        prior_upper = None if row["upper"] is None else float(row["upper"])
    return ordered


def select_nearest_impossible_lower_bucket(
    rows: Sequence[Mapping[str, Any]], *, hard_lower_bound: float
) -> dict[str, Any] | None:
    buckets = normalize_market_buckets(rows)
    try:
        bound = float(hard_lower_bound)
    except (TypeError, ValueError) as exc:
        raise ValueError("hard_lower_bound_non_numeric") from exc
    if not math.isfinite(bound):
        raise ValueError("hard_lower_bound_non_finite")
    candidates = [row for row in buckets if row["upper"] is not None and float(row["upper"]) < bound]
    if not candidates:
        return None
    chosen = max(candidates, key=lambda row: float(row["upper"]))
    return {
        **chosen,
        "hard_lower_bound": bound,
        "selection_rule": "nearest_bucket_with_upper_strictly_below_hard_lower_bound",
        "mathematically_impossible": True,
    }


def unified_capital_plan(
    *,
    account_free_usdc: float,
    pending_reserved_usdc: float,
    executable_depth_usdc: float,
    max_price: float,
    minimum_shares: float = 5.0,
    maximum_order_notional_usdc: float = 50.0,
) -> dict[str, Any]:
    """Freeze one bounded order from the shared cash pool.

    Only reservations not yet reflected by the exchange balance are deducted.
    Filled positions are already absent from ``account_free_usdc`` and must not
    be charged twice.  The caller serializes reservation in SQLite.
    """

    values = (
        account_free_usdc, pending_reserved_usdc, executable_depth_usdc,
        max_price, minimum_shares, maximum_order_notional_usdc,
    )
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("capital_input_non_finite")
    if account_free_usdc < 0 or pending_reserved_usdc < 0 or executable_depth_usdc < 0:
        raise ValueError("capital_exposure_negative")
    if not 0 < max_price < 1 or minimum_shares <= 0 or maximum_order_notional_usdc <= 0:
        raise ValueError("capital_policy_invalid")
    shared_cash_available = max(0.0, account_free_usdc - pending_reserved_usdc)
    # FAK is deliberately sized to the configured per-bucket target.  Visible
    # depth is only an admission fact (there must be an executable ask); it is
    # not a sizing cap because FAK already limits the actual fill to liquidity
    # available at or below max_price.
    order_notional = maximum_order_notional_usdc
    minimum_notional = minimum_shares * max_price
    if executable_depth_usdc <= 0:
        blocker = "no_executable_no_ask_at_or_below_max_buy_price"
    elif shared_cash_available + 1e-9 < order_notional:
        blocker = "capital_below_fixed_per_bucket_notional"
    elif order_notional + 1e-9 < minimum_notional:
        blocker = "capital_below_minimum_marketable_order"
    else:
        blocker = ""
    return {
        "schema": "reported_trajectory_no_fixed_bucket_capital_plan_v3",
        "account_free_usdc": round(account_free_usdc, 6),
        "pending_reserved_usdc": round(pending_reserved_usdc, 6),
        "shared_cash_available_usdc": round(shared_cash_available, 6),
        "executable_depth_usdc": round(executable_depth_usdc, 6),
        "maximum_buy_price": max_price,
        "minimum_shares": minimum_shares,
        "maximum_order_notional_usdc": maximum_order_notional_usdc,
        "target_order_notional_usdc": maximum_order_notional_usdc,
        "frozen_order_notional_usdc": 0.0 if blocker else round(order_notional, 6),
        "capital_block_reason": blocker,
        "visible_depth_caps_requested_notional": False,
        "fak_partial_fill_allowed": True,
        "position_priority": "NO",
        "existing_yes_positions_ignored": True,
        "unified_pool": True,
        "deploy_all_available": False,
        "source_quota_applied": False,
        "market_quota_applied": False,
        "staged_sizing_used": False,
    }


__all__ = [
    "ParsedReport", "SCHEMA_VERSION", "conservative_market_temperature", "identity",
    "normalize_market_buckets", "parse_bucket_interval",
    "parse_metar_temperature", "select_nearest_impossible_lower_bucket", "unified_capital_plan",
]
