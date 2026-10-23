"""Neutral METAR and market-bucket contracts for the pure-YES basket.

This module is deliberately limited to parsing official observation identity,
converting temperature units conservatively, and validating the market bucket
ladder.  It does not create a NO position, calculate capital, or submit orders.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bayes_basket_target.metar_precision import official_temperature_c


SCHEMA_VERSION = "weather_basket_yes_runtime_v1"
REPORT_RE = re.compile(
    r"\b(?P<kind>METAR|SPECI)(?:\s+(?:COR|AMD))?\s+"
    r"(?P<station>[A-Z0-9]{4})\s+(?P<day>\d{2})(?P<hour>\d{2})(?P<minute>\d{2})Z\b"
)
# The temperature group is a whole token, TT/TdTd.  Matched inside a token,
# an automatic station's missing cloud group "//////CB" or "//////TCU" read
# as "//" over "//" -- temperature missing -- and came before the real group:
# EGLC 2026-10-21 lost ten METARs, its 13C high among them (09:50Z), and its
# truth settled a bucket low.
TEMP_RE = re.compile(r"^(?P<temp>M?\d{2}|//)/(?P<dew>M?\d{2}|//)$")
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
    # The official temperature at the finest resolution this report states:
    # tenths when it carries the ``Tsnnnsnnn`` remark group, whole degrees
    # otherwise.  ``whole_degree_temperature_c`` keeps the body group on its
    # own so a consumer that is contracted to whole degrees -- the P1 running
    # maximum, which reads the reported value as an exact integer with a
    # half-degree conservative margin around it -- is not silently handed a
    # different number.
    reported_temperature_c: float
    whole_degree_temperature_c: int
    temperature_resolution_c: float
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
    raw = " ".join(str(raw_text or "").replace("\r", " ").replace("\n", " ").split()).rstrip("=")
    header = REPORT_RE.search(raw)
    if header is None:
        raise ValueError("metar_speci_header_invalid")
    station = header.group("station").upper()
    expected = str(expected_station or "").strip().upper()
    if expected and station != expected:
        raise ValueError("metar_station_mismatch")
    body = raw[header.end() :].split(" RMK ", 1)[0]
    match = next(
        (found for found in map(TEMP_RE.match, body.split()) if found is not None),
        None,
    )
    if match is None:
        raise ValueError("metar_temperature_group_missing")
    temperature = _signed_integer(match.group("temp"))
    # The body group is the reading rounded to a whole degree.  Where the
    # station also publishes the remark group it states the same reading in
    # tenths, and a market that settles in 2F buckets needs that tenth: one
    # whole Celsius degree spans nearly two of those buckets.
    precise, resolution = official_temperature_c(
        raw, whole_degree_c=float(temperature)
    )
    return ParsedReport(
        report_type=header.group("kind").upper(),
        station_id=station,
        day=int(header.group("day")),
        hour=int(header.group("hour")),
        minute=int(header.group("minute")),
        reported_temperature_c=precise,
        whole_degree_temperature_c=temperature,
        temperature_resolution_c=resolution,
        raw_text=raw,
        raw_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def conservative_market_temperature(reported_temperature_c: float, unit: str) -> int:
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
        return {"label": text, "unit": range_match.group(3).upper(), "lower": lower, "upper": upper, "kind": "range"}
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


__all__ = [
    "ParsedReport",
    "SCHEMA_VERSION",
    "conservative_market_temperature",
    "identity",
    "normalize_market_buckets",
    "parse_bucket_interval",
    "parse_metar_temperature",
]
