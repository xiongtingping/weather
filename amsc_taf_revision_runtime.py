"""Pure parser and identity contract for AMSC TAF revisions.

The original implementation was written for ZUUU only.  The parser is now
station-agnostic; the old ``parse_zuuu_taf_revision`` name remains as a
compatibility wrapper for the existing Chengdu posterior lineage.
"""

from __future__ import annotations

import calendar
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "amsc_taf_forecast_revision_v1"
TAF_HEADER_RE = re.compile(
    r"^TAF(?:\s+(?P<modifier>AMD|COR))?\s+(?P<station>[A-Z]{4})\s+"
    r"(?P<day>\d{2})(?P<hour>\d{2})(?P<minute>\d{2})Z\b"
)
TAF_VALIDITY_RE = re.compile(r"\b(?P<from_day>\d{2})(?P<from_hour>\d{2})/(?P<to_day>\d{2})(?P<to_hour>\d{2})\b")
TAF_TX_RE = re.compile(r"\bTX(?P<temperature>M?\d{2})/(?P<day>\d{2})(?P<hour>\d{2})Z\b")
TAF_CHANGE_RE = re.compile(
    r"\b(?P<kind>BECMG|TEMPO)\s+(?P<from_day>\d{2})(?P<from_hour>\d{2})/"
    r"(?P<to_day>\d{2})(?P<to_hour>\d{2})\b"
)
REVISION_KIND_RANK = {"original": 0, "amendment": 1, "correction": 2}


def _parse_reference(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("taf_reference_time_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _infer(day: int, hour: int, minute: int, reference: datetime) -> datetime:
    center = reference.replace(day=15, hour=0, minute=0, second=0, microsecond=0)
    month_refs = {center, center - timedelta(days=32), center + timedelta(days=32)}
    candidates: list[datetime] = []
    for month_ref in month_refs:
        if 1 <= day <= calendar.monthrange(month_ref.year, month_ref.month)[1] and 0 <= hour <= 24:
            # Aviation validity groups use 24 for the next day's midnight.
            base = datetime(month_ref.year, month_ref.month, day, min(hour, 23), minute, tzinfo=timezone.utc)
            if hour == 24:
                base += timedelta(hours=1)
            candidates.append(base)
    if not candidates:
        raise ValueError("taf_calendar_time_invalid")
    return min(candidates, key=lambda item: abs((item - reference).total_seconds()))


def _temperature(value: str) -> float:
    return float(-int(value[1:]) if value.startswith("M") else int(value))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _condition_regime(text: str) -> str:
    value = str(text or "").upper()
    if re.search(r"(?:^|\s)[+-]?TS|\bCB\b|\bTCU\b|\b(?:FEW|SCT|BKN|OVC)\d{3}(?:CB|TCU)\b", value):
        return "convection_or_showers"
    if re.search(r"(?:^|\s)[+-]?(?:SH)?(?:RA|DZ|SN|SG|PL|GR|GS)\b", value):
        return "wet_precipitation"
    if re.search(r"(?:^|\s)(?:FG|BR|MIFG|BCFG|PRFG|FZFG)\b|\bVV\d{3}\b", value):
        return "fog_low_cloud"
    if re.search(r"\b(?:BKN|OVC)\d{3}\b", value):
        return "cloudy_stable"
    if re.search(r"\b(?:NSW|CAVOK|NSC|SKC)\b|\b(?:FEW|SCT)\d{3}\b", value):
        return "clear_dry"
    return "unknown_forecast_regime"


def _peak_window_regime(raw: str, *, issue: datetime, peak: datetime, validity_end: int) -> str:
    changes = list(TAF_CHANGE_RE.finditer(raw))
    base = raw[validity_end : changes[0].start() if changes else len(raw)]
    prevailing = _condition_regime(base)
    temporary: list[str] = []
    for index, match in enumerate(changes):
        body = raw[match.end() : changes[index + 1].start() if index + 1 < len(changes) else len(raw)]
        regime = _condition_regime(body)
        start = _infer(int(match.group("from_day")), int(match.group("from_hour")), 0, issue)
        end = _infer(int(match.group("to_day")), int(match.group("to_hour")), 0, issue)
        if match.group("kind") == "BECMG" and end <= peak and regime != "unknown_forecast_regime":
            prevailing = regime
        elif match.group("kind") == "TEMPO" and start <= peak < end and regime != "unknown_forecast_regime":
            temporary.append(regime)
    severity = {
        "unknown_forecast_regime": 0,
        "clear_dry": 1,
        "cloudy_stable": 2,
        "fog_low_cloud": 3,
        "wet_precipitation": 4,
        "convection_or_showers": 5,
    }
    return max([prevailing, *temporary], key=lambda item: severity[item])


def taf_revision_order_key(
    *, issue_time_utc: str, revision_kind: str, first_seen_at: str, revision_id: str,
) -> tuple[float, int, float, str]:
    """Order TAF revisions by meteorological issue, never retrieval recency alone."""

    try:
        seen = _parse_reference(first_seen_at)
    except (TypeError, ValueError):
        seen = None
    try:
        issue = _parse_reference(issue_time_utc)
    except (TypeError, ValueError):
        issue = seen
    seen_epoch = seen.timestamp() if seen is not None else float("-inf")
    issue_epoch = issue.timestamp() if issue is not None else seen_epoch
    return (
        issue_epoch,
        REVISION_KIND_RANK.get(str(revision_kind or "").strip().lower(), 0),
        seen_epoch,
        str(revision_id or "").strip(),
    )


def parse_taf_revision(
    raw_text: str,
    *,
    first_seen_at: str,
    station_id: str = "",
    source_id: str = "amsc_taf_forecast",
    local_timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Parse one immutable provider TAF revision without assigning observation authority."""

    raw = str(raw_text or "").strip()
    header = TAF_HEADER_RE.search(raw)
    expected_station = str(station_id or "").strip().upper()
    if not header or (expected_station and str(header.group("station") or "").upper() != expected_station):
        raise ValueError("amsc_taf_header_invalid_or_unsupported")
    parsed_station = str(header.group("station") or "").strip().upper()
    seen = _parse_reference(first_seen_at)
    try:
        local_zone = ZoneInfo(str(local_timezone or "Asia/Shanghai").strip())
    except Exception as exc:
        raise ValueError("taf_local_timezone_invalid") from exc
    issue = _infer(
        int(header.group("day")),
        int(header.group("hour")),
        int(header.group("minute")),
        seen,
    )
    validity = TAF_VALIDITY_RE.search(raw)
    peaks = []
    for match in TAF_TX_RE.finditer(raw):
        valid = _infer(int(match.group("day")), int(match.group("hour")), 0, issue)
        peaks.append(
            {
                "temperature_c": _temperature(match.group("temperature")),
                "valid_time_utc": _iso(valid),
                "event_date_local": valid.astimezone(local_zone).date().isoformat(),
            }
        )
    if not peaks:
        raise ValueError("amsc_taf_tx_missing")
    for peak in peaks:
        peak_time = _parse_reference(str(peak["valid_time_utc"]))
        peak["forecast_weather_regime"] = _peak_window_regime(
            raw,
            issue=issue,
            peak=peak_time,
            validity_end=validity.end() if validity else header.end(),
        )
    modifier = str(header.group("modifier") or "")
    kind = "amendment" if modifier == "AMD" else "correction" if modifier == "COR" else "original"
    raw_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {
        "schema": SCHEMA_VERSION,
        "source_id": str(source_id or "amsc_taf_forecast"),
        "station_id": parsed_station,
        "forecast_revision_kind": kind,
        "taf_modifier": modifier,
        "issue_time_utc": _iso(issue),
        "validity_token": validity.group(0) if validity else "",
        "forecast_peaks": peaks,
        "forecast_weather_regime": max(
            peaks, key=lambda item: float(item["temperature_c"])
        )["forecast_weather_regime"],
        "raw_sha256": raw_sha,
        "source_revision_id": "tafrev_" + raw_sha[:24],
        "first_system_seen_at": _iso(seen),
        "weak_forecast_only": True,
        "hard_observation_eligible": False,
    }


def parse_zuuu_taf_revision(raw_text: str, *, first_seen_at: str) -> dict[str, Any]:
    """Compatibility wrapper for the historical Chengdu parser contract."""

    try:
        return parse_taf_revision(
            raw_text,
            first_seen_at=first_seen_at,
            station_id="ZUUU",
            source_id="zuuu_taf_forecast_amsc",
        )
    except ValueError as exc:
        # Preserve the historical public error contract for callers/tests
        # while the generic AMSC parser uses station-neutral diagnostics.
        if str(exc) == "amsc_taf_tx_missing":
            raise ValueError("zuuu_taf_tx_missing") from exc
        raise


__all__ = [
    "REVISION_KIND_RANK",
    "SCHEMA_VERSION",
    "parse_taf_revision",
    "parse_zuuu_taf_revision",
    "taf_revision_order_key",
]
