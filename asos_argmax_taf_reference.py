"""Optional official AWC TAF-TX audit references for frozen NWS argmax windows."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from amsc_taf_revision_runtime import parse_taf_revision
from asos_argmax_window import (
    CANONICAL_EVENT_TIMEZONE,
    LIVE_STATIONS,
    select_latest_contiguous_argmax_periods,
)
from bayes_basket_target.contracts import content_sha256


SCHEMA = "asos_argmax_awc_taf_tx_reference_v1"
INBOX_SCHEMA = "bayes_basket_target_taf_revision_inbox_v1"


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("taf_reference_timestamp_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _capture_valid(capture: Mapping[str, Any]) -> bool:
    unsigned = {key: value for key, value in capture.items() if key != "content_sha256"}
    raw_taf = str(capture.get("raw_taf") or "")
    return (
        capture.get("source_class") == "official_forward_native_receipt"
        and capture.get("provider") == "NOAA Aviation Weather Center"
        and (capture.get("boundary") or {}).get("native_local_receipt") is True
        and str(capture.get("content_sha256") or "") == content_sha256(unsigned)
        and hashlib.sha256(raw_taf.encode()).hexdigest()
        == str(capture.get("raw_taf_sha256") or "")
    )


def materialize_argmax_taf_references(
    conn: sqlite3.Connection,
    *,
    inbox_path: Path,
    station_ids: Sequence[str] = tuple(sorted(LIVE_STATIONS)),
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Append TX comparisons; never change NWS windows or entry eligibility."""

    path = inbox_path.expanduser().resolve()
    if not path.is_file():
        return {"ok": True, "state": "inbox_missing_optional", "inserted": 0}
    inbox = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(inbox, Mapping) or inbox.get("schema") != INBOX_SCHEMA:
        return {"ok": False, "state": "inbox_invalid", "inserted": 0}
    stations = sorted({str(value).upper() for value in station_ids} & LIVE_STATIONS)
    if not stations:
        return {"ok": True, "state": "no_live_station", "inserted": 0}
    placeholders = ",".join("?" for _ in stations)
    windows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(
        f"""SELECT window_id,station_id,event_date,argmax_window_start_utc,
                    argmax_periods_json
               FROM reported_trajectory_asos_argmax_windows
              WHERE station_id IN ({placeholders})""",
        stations,
    ):
        try:
            periods = json.loads(str(row[4]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(periods, list) or not periods:
            continue
        periods = select_latest_contiguous_argmax_periods(periods)
        if not periods:
            continue
        windows[(str(row[1]), str(row[2]))] = {
            "window_id": str(row[0]),
            "window_start": _utc(str(row[3])),
            "periods": periods,
        }
    inserted = 0
    missing_tx = 0
    rejected = 0
    for capture in inbox.get("captures") or []:
        if not isinstance(capture, Mapping) or not _capture_valid(capture):
            rejected += 1
            continue
        station = str(capture.get("station_id") or "").upper()
        event_date = str(capture.get("event_date") or "")
        window = windows.get((station, event_date))
        if window is None:
            continue
        received = _utc(str(capture.get("local_received_at_utc") or ""))
        if received >= window["window_start"]:
            rejected += 1
            continue
        revision = capture.get("forecast_revision")
        if not isinstance(revision, Mapping):
            rejected += 1
            continue
        raw_taf = str(capture.get("raw_taf") or "")
        try:
            parsed = parse_taf_revision(
                raw_taf,
                first_seen_at=str(capture.get("local_received_at_utc") or ""),
                station_id=station,
                source_id="awc_taf_forecast",
                local_timezone=CANONICAL_EVENT_TIMEZONE,
            )
        except ValueError as exc:
            if str(exc) == "amsc_taf_tx_missing":
                missing_tx += 1
                continue
            rejected += 1
            continue
        peaks = [
            item for item in parsed.get("forecast_peaks") or []
            if isinstance(item, Mapping)
            and str(item.get("event_date_local") or "") == event_date
        ]
        if not peaks:
            missing_tx += 1
            continue
        maximum = max(float(item["temperature_c"]) for item in peaks)
        for peak in peaks:
            if abs(float(peak["temperature_c"]) - maximum) > 1e-9:
                continue
            tx_start = _utc(str(peak.get("valid_time_utc") or ""))
            tx_end = tx_start + timedelta(hours=1)
            confirms = False
            for period in window["periods"]:
                try:
                    start = _utc(str(period.get("start_utc") or ""))
                    end = _utc(str(period.get("end_utc") or ""))
                except (AttributeError, TypeError, ValueError):
                    continue
                if tx_start < end and tx_end > start:
                    confirms = True
                    break
            fact = {
                "schema": SCHEMA,
                "window_id": window["window_id"],
                "station_id": station,
                "event_date": event_date,
                "source_lane": "awc_taf",
                "source_class": "official_forward_native_receipt",
                "evidence_role": "auxiliary_timing_reference_only",
                "forecast_revision_id": str(revision.get("revision_id") or ""),
                "issue_time_utc": str(revision.get("issued_at_utc") or ""),
                "local_received_at_utc": _iso(received),
                "raw_taf_sha256": str(capture.get("raw_taf_sha256") or ""),
                "tx_c": maximum,
                "tx_valid_time_utc": _iso(tx_start),
                "comparison_state": (
                    "CONFIRMS_NWS_ARGMAX" if confirms
                    else "CONFLICTS_WITH_NWS_ARGMAX"
                ),
                "modifies_nws_window": False,
                "blocks_entry": False,
            }
            sha = content_sha256(fact)
            reference_json = {
                **fact,
                "raw_taf": raw_taf,
                "capture_content_sha256": capture["content_sha256"],
            }
            cursor = conn.execute(
                """INSERT OR IGNORE INTO reported_trajectory_asos_argmax_taf_references(
                     reference_id,window_id,station_id,event_date,source_lane,source_class,
                     evidence_role,forecast_revision_id,issue_time_utc,local_received_at_utc,
                     raw_taf_sha256,tx_c,tx_valid_time_utc,comparison_state,
                     modifies_nws_window,blocks_entry,reference_json,content_sha256,created_at_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?)""",
                (
                    "asostafref_" + sha[:32], window["window_id"], station, event_date,
                    "awc_taf", "official_forward_native_receipt",
                    "auxiliary_timing_reference_only", fact["forecast_revision_id"],
                    fact["issue_time_utc"], fact["local_received_at_utc"],
                    fact["raw_taf_sha256"], maximum, fact["tx_valid_time_utc"],
                    fact["comparison_state"],
                    json.dumps(reference_json, sort_keys=True, separators=(",", ":")),
                    sha, _iso(now_utc or datetime.now(timezone.utc)),
                ),
            )
            inserted += int(cursor.rowcount or 0)
    conn.commit()
    return {
        "ok": True,
        "state": "materialized",
        "inserted": inserted,
        "missing_tx_capture_count": missing_tx,
        "rejected_capture_count": rejected,
        "modifies_nws_window": False,
        "blocks_entry": False,
    }


__all__ = ["materialize_argmax_taf_references"]
