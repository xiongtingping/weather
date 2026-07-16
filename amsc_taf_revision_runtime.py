"""Pure parser and identity contract for original, AMD, and COR ZUUU TAF revisions."""

from __future__ import annotations

import calendar
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "amsc_taf_forecast_revision_v1"
TAF_HEADER_RE = re.compile(
    r"^TAF(?:\s+(?P<modifier>AMD|COR))?\s+ZUUU\s+(?P<day>\d{2})(?P<hour>\d{2})(?P<minute>\d{2})Z\b"
)
TAF_VALIDITY_RE = re.compile(r"\b(?P<from_day>\d{2})(?P<from_hour>\d{2})/(?P<to_day>\d{2})(?P<to_hour>\d{2})\b")
TAF_TX_RE = re.compile(r"\bTX(?P<temperature>M?\d{2})/(?P<day>\d{2})(?P<hour>\d{2})Z\b")
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
        if 1 <= day <= calendar.monthrange(month_ref.year, month_ref.month)[1]:
            candidates.append(datetime(month_ref.year, month_ref.month, day, hour, minute, tzinfo=timezone.utc))
    if not candidates:
        raise ValueError("taf_calendar_time_invalid")
    return min(candidates, key=lambda item: abs((item - reference).total_seconds()))


def _temperature(value: str) -> float:
    return float(-int(value[1:]) if value.startswith("M") else int(value))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


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


def parse_zuuu_taf_revision(raw_text: str, *, first_seen_at: str) -> dict[str, Any]:
    """Parse one immutable provider TAF revision without assigning observation authority."""

    raw = str(raw_text or "").strip()
    header = TAF_HEADER_RE.search(raw)
    if not header:
        raise ValueError("zuuu_taf_header_invalid_or_unsupported")
    seen = _parse_reference(first_seen_at)
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
                "event_date_local": valid.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            }
        )
    if not peaks:
        raise ValueError("zuuu_taf_tx_missing")
    modifier = str(header.group("modifier") or "")
    kind = "amendment" if modifier == "AMD" else "correction" if modifier == "COR" else "original"
    raw_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {
        "schema": SCHEMA_VERSION,
        "source_id": "zuuu_taf_forecast_amsc",
        "station_id": "ZUUU",
        "forecast_revision_kind": kind,
        "taf_modifier": modifier,
        "issue_time_utc": _iso(issue),
        "validity_token": validity.group(0) if validity else "",
        "forecast_peaks": peaks,
        "raw_sha256": raw_sha,
        "source_revision_id": "tafrev_" + raw_sha[:24],
        "first_system_seen_at": _iso(seen),
        "weak_forecast_only": True,
        "hard_observation_eligible": False,
    }


__all__ = ["REVISION_KIND_RANK", "SCHEMA_VERSION", "parse_zuuu_taf_revision", "taf_revision_order_key"]
