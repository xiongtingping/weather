#!/usr/bin/env python3
"""Capture bounded official AWC TAF/truth rows for offline TAF calibration.

The AWC ``date`` parameter is the historical address.  Each event day uses the
latest TAF available at a fixed settlement-relative decision anchor and the
official AWC METAR/SPECI rows inside that market's settlement window.  The
script writes one compact research artifact only; it never opens production
SQLite, starts a service, or calls an order gateway.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.taf_rotation_state import (  # noqa: E402
    TAF_PHYSICAL_PEAK_WINDOW_END_HOUR_LOCAL,
    TAF_PHYSICAL_PEAK_WINDOW_START_HOUR_LOCAL,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MIB,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_awc_taf_calibration_dataset_v1"
FORMAL_STATION_LANE_TRUTH = "FORMAL_STATION_LANE_TRUTH"
TAF_ENDPOINT = "https://aviationweather.gov/api/data/taf"
METAR_ENDPOINT = "https://aviationweather.gov/api/data/metar"
USER_AGENT = "weather-target-research/1.0 (bounded official AWC TAF OOS capture)"
MAX_DAYS = 90
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 256 * MIB
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MIN_REQUEST_INTERVAL_SEC = 0.25
MAX_FETCH_ATTEMPTS = 3
FETCH_RETRY_BACKOFF_BASE_SEC = 0.25
FETCH_RETRY_BACKOFF_CAP_SEC = 0.5
# AWC's date-addressed endpoint intermittently resets TLS connections or
# closes a bounded response while several station workers are active.  These
# transport failures are safe to retry because the request is read-only and
# the resulting artifact is content-addressed; treating them as permanent
# blockers needlessly removes otherwise valid closed-day TAF samples.
RETRYABLE_CURL_RETURN_CODES = frozenset({6, 7, 28, 35, 55})
CURL_HTTP_ERROR_RETURN_CODE = 22
HTTP_STATUS_ERROR_RE = re.compile(
    r"(?:returned error:\s*|HTTP(?:/\d(?:\.\d+)?)?\s+)(\d{3})(?:\b|$)",
    re.IGNORECASE,
)
TAF_TX_RE = re.compile(r"\bTX(M?\d{2})/(\d{2})(\d{2})Z\b")
PRECIP_RE = re.compile(r"(?:^|\s)(?:[-+]?DZ|[-+]?RA|[-+]?SN|SG|PL|GR|GS|SH|TS)")
CORRECTION_RE = re.compile(r"\bCOR\b", re.IGNORECASE)
SKY_FRACTION = {
    "SKC": 0.0, "CLR": 0.0, "NSC": 0.0, "NCD": 0.0,
    "FEW": 0.125, "SCT": 0.375, "BKN": 0.75, "OVC": 1.0,
    "VV": 1.0,
}


def _truth_authority_for_source_lane(source_lane: str) -> dict[str, Any]:
    lane = str(source_lane or "").strip().upper()
    # Historical calibration is now on the same global AWC lane as the
    # current 45-station truth.  Retired AMSC history remains audit-only and
    # cannot create an input that could later be joined into P0-M.
    if lane != "AWC":
        raise ContractError("awc_taf_calibration_source_lane_invalid")
    return {
        "station_source_lane": "AWC",
        "evidence_source_lane": "AWC",
        "truth_label_authority": FORMAL_STATION_LANE_TRUTH,
        "reference_only": False,
        "enters_realtime_truth": False,
        "enters_p1": False,
        "enters_hard_no": False,
        "replacement_required_when_canonical_history_available": False,
    }


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def _event_window(event_date: str, timezone_name: str) -> tuple[datetime, datetime]:
    local = ZoneInfo(timezone_name)
    day = date.fromisoformat(event_date)
    start = datetime.combine(day, wall_time.min, tzinfo=local).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), wall_time.min, tzinfo=local).astimezone(timezone.utc)
    return start, end


def _fetch_retry_delay(attempt: int) -> float:
    """Return a short, hard-bounded delay after a transient curl failure."""

    return min(
        FETCH_RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1)),
        FETCH_RETRY_BACKOFF_CAP_SEC,
    )


def _is_retryable_curl_failure(returncode: int, detail: str) -> bool:
    if returncode in RETRYABLE_CURL_RETURN_CODES:
        return True
    if returncode != CURL_HTTP_ERROR_RETURN_CODE:
        return False
    match = HTTP_STATUS_ERROR_RE.search(detail)
    if not match:
        return False
    status = int(match.group(1))
    return status == 429 or 500 <= status <= 599


def _awc_proxy() -> str:
    """Return the explicitly selected AWC transport proxy, if any.

    Historical AWC requests used to force ``--noproxy aviationweather.gov``.
    That made a transient direct-TLS failure look like a missing TAF even when
    the user had supplied the bounded 1082 proxy.  Keep the old direct path as
    the default for reproducibility, but allow the caller to opt into the
    explicit research proxy without inheriting an arbitrary ambient proxy.
    ``AWC_HTTP_PROXY`` is intentionally preferred over generic proxy
    variables so a shell-wide proxy cannot silently change the evidence lane.
    """

    for name in ("AWC_HTTP_PROXY", "HTTPS_PROXY", "https_proxy"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _fetch_json(url: str, *, timeout_sec: int) -> tuple[list[dict[str, Any]], str, int]:
    proxy = _awc_proxy()
    command = [
        "/usr/bin/curl",
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--location",
        "--proto",
        "=https",
        "--tlsv1.2",
        "--max-time",
        str(timeout_sec),
        "--max-filesize",
        str(MAX_RESPONSE_BYTES),
        "--header",
        f"User-Agent: {USER_AGENT}",
        "--header",
        "Accept: application/json",
        url,
    ]
    if proxy:
        command[1:1] = ["--proxy", proxy]
    else:
        # Direct mode remains explicit; do not let a stale shell proxy alter
        # official AWC historical lineage when no AWC proxy was requested.
        command[1:1] = ["--noproxy", "aviationweather.gov"]
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
            )
        except subprocess.TimeoutExpired as exc:
            detail = str(exc)[:240].strip()
            error = ContractError(
                f"awc_taf_calibration_curl_failed_timeout:{detail}"
            )
            if attempt >= MAX_FETCH_ATTEMPTS:
                raise error from exc
            time.sleep(_fetch_retry_delay(attempt))
            continue

        raw = bytes(completed.stdout)
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace")[:240].strip()
            error = ContractError(
                f"awc_taf_calibration_curl_failed_rc_{completed.returncode}:{detail}"
            )
            if (
                not _is_retryable_curl_failure(completed.returncode, detail)
                or attempt >= MAX_FETCH_ATTEMPTS
            ):
                raise error
            time.sleep(_fetch_retry_delay(attempt))
            continue
        break
    else:  # pragma: no cover - the bounded loop always returns or breaks
        raise ContractError("awc_taf_calibration_curl_fetch_exhausted")

    if len(raw) > MAX_RESPONSE_BYTES:
        raise ContractError("awc_taf_calibration_response_too_large")
    if not raw.strip():
        return [], hashlib.sha256(raw).hexdigest(), len(raw)
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ContractError("awc_taf_calibration_response_shape_invalid")
    return (
        [dict(row) for row in payload if isinstance(row, Mapping)],
        hashlib.sha256(raw).hexdigest(),
        len(raw),
    )


def _tx(value: str, *, issue_time: datetime, start: datetime, end: datetime) -> tuple[float, str]:
    candidates: list[tuple[datetime, float]] = []
    for raw_temperature, raw_day, raw_hour in TAF_TX_RE.findall(value):
        temperature = -float(raw_temperature[1:]) if raw_temperature.startswith("M") else float(raw_temperature)
        wanted_day = int(raw_day)
        wanted_hour = int(raw_hour)
        cursor = start - timedelta(days=35)
        stop = end + timedelta(days=35)
        while cursor.date() <= stop.date():
            if cursor.day == wanted_day:
                candidate = datetime(
                    cursor.year, cursor.month, cursor.day, wanted_hour,
                    tzinfo=timezone.utc,
                )
                if issue_time <= candidate <= end + timedelta(days=1):
                    candidates.append((candidate, temperature))
            cursor += timedelta(days=1)
    in_window = [item for item in candidates if start <= item[0] < end]
    if not in_window:
        raise ContractError("awc_taf_tx_for_settlement_day_missing")
    valid_at, temperature = min(in_window, key=lambda item: abs((item[0] - issue_time).total_seconds()))
    return temperature, _iso(valid_at)


def _forecast_features(
    taf: Mapping[str, Any],
    *,
    event_date: str,
    timezone_name: str,
) -> tuple[float, float, float]:
    local = ZoneInfo(timezone_name)
    day = date.fromisoformat(event_date)
    # Daytime warming window; TX itself remains the main calibrated signal.
    peak_start = datetime.combine(
        day,
        wall_time(TAF_PHYSICAL_PEAK_WINDOW_START_HOUR_LOCAL, 0),
        tzinfo=local,
    ).astimezone(timezone.utc)
    peak_end = datetime.combine(
        day,
        wall_time(TAF_PHYSICAL_PEAK_WINDOW_END_HOUR_LOCAL, 0),
        tzinfo=local,
    ).astimezone(timezone.utc)
    weighted_cloud = 0.0
    weighted_precip = 0.0
    weighted_wind = 0.0
    total_weight = 0.0
    for row in taf.get("fcsts", []):
        if not isinstance(row, Mapping):
            continue
        try:
            start = _parse_utc(row.get("timeFrom"))
            end = _parse_utc(row.get("timeTo"))
        except (TypeError, ValueError, OverflowError):
            continue
        overlap = max(
            0.0,
            (min(end, peak_end) - max(start, peak_start)).total_seconds(),
        )
        if overlap <= 0.0:
            continue
        covers = [
            SKY_FRACTION.get(str(item.get("cover") or "").upper(), 0.0)
            for item in row.get("clouds", [])
            if isinstance(item, Mapping)
        ]
        cloud = max(covers, default=0.0)
        wx = str(row.get("wxString") or "")
        probability_raw = row.get("probability")
        if probability_raw is not None:
            probability = max(0.0, min(1.0, float(probability_raw) / 100.0))
        else:
            probability = 1.0 if PRECIP_RE.search(wx) else 0.0
        wind_kt = float(row.get("wspd") or 0.0)
        weighted_cloud += overlap * cloud
        weighted_precip += overlap * probability
        weighted_wind += overlap * max(0.0, wind_kt * 0.514444)
        total_weight += overlap
    if total_weight <= 0.0:
        raise ContractError("awc_taf_peak_window_features_missing")
    return (
        weighted_cloud / total_weight,
        weighted_precip / total_weight,
        weighted_wind / total_weight,
    )


def _taf_row(
    rows: list[dict[str, Any]],
    *,
    station_id: str,
    start: datetime,
    end: datetime,
    event_date: str,
    timezone_name: str,
    require_tx: bool = True,
    decision_anchor: datetime | None = None,
    require_features: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = [
        row for row in rows
        if str(row.get("icaoId") or "").strip().upper() == station_id
    ]
    if not candidates:
        raise ContractError("awc_taf_history_row_missing")
    # Resolve the latest bulletin that was actually available at the fixed
    # decision anchor.  The AWC date-addressed endpoint may return a bulletin
    # issued exactly at the anchor whose ``dbPopTime`` is a few seconds later;
    # selecting the newest row before checking availability would incorrectly
    # discard the preceding valid bulletin.
    parsed_candidates: list[tuple[datetime, datetime, dict[str, Any]]] = []
    saw_issue_after_available = False
    saw_after_anchor = False
    for candidate in candidates:
        try:
            candidate_issue = _parse_utc(candidate.get("issueTime"))
            candidate_available = _parse_utc(
                candidate.get("dbPopTime") or candidate.get("issueTime")
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if candidate_available < candidate_issue:
            saw_issue_after_available = True
            continue
        if candidate_available >= end:
            continue
        if decision_anchor is not None and candidate_available > decision_anchor:
            saw_after_anchor = True
            continue
        parsed_candidates.append((candidate_available, candidate_issue, candidate))
    if not parsed_candidates:
        if saw_issue_after_available:
            raise ContractError("awc_taf_historical_available_before_issue")
        if saw_after_anchor and decision_anchor is not None:
            raise ContractError("awc_taf_historical_available_after_decision_anchor")
        raise ContractError("awc_taf_not_available_before_truth_close")
    available_time, issue_time, taf = max(
        parsed_candidates,
        key=lambda item: (item[0], item[1], str(item[2].get("rawTAF") or "")),
    )
    raw_taf = str(taf.get("rawTAF") or "").strip()
    try:
        tx_c, tx_valid_at = _tx(
            raw_taf,
            issue_time=issue_time,
            start=start,
            end=end,
        )
    except ContractError as exc:
        if require_tx or str(exc) != (
            "awc_taf_tx_for_settlement_day_missing"
        ):
            raise
        tx_c, tx_valid_at = None, None
    try:
        cloud, precip, wind = _forecast_features(
            taf,
            event_date=event_date,
            timezone_name=timezone_name,
        )
    except ContractError as exc:
        # A no-TX sidecar is still useful as a raw/receipt arrival record when
        # an upstream historical response omitted decoded forecast segments.
        # It remains ineligible for TAF/P0-M and therefore must not turn a
        # malformed feature payload into a calibration row.
        if require_features or str(exc) != "awc_taf_peak_window_features_missing":
            raise
        cloud = precip = wind = None
    compact = {
        "issue_time_utc": _iso(issue_time),
        "available_time_utc": _iso(available_time),
        "tx_c": tx_c,
        "tx_valid_at_utc": tx_valid_at,
        "cloud_cover_fraction": cloud,
        "precipitation_probability": precip,
        "wind_speed_mps": wind,
        "raw_taf": raw_taf,
        "raw_taf_sha256": hashlib.sha256(raw_taf.encode("utf-8")).hexdigest(),
    }
    return compact, taf


def _truth_rows(
    rows: list[dict[str, Any]],
    *,
    station_id: str,
    start: datetime,
    end: datetime,
) -> tuple[float, list[dict[str, Any]]]:
    compact: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("icaoId") or "").strip().upper() != station_id:
            continue
        if row.get("temp") is None:
            continue
        try:
            observed = _parse_utc(row.get("obsTime") or row.get("reportTime"))
            temperature = float(row.get("temp"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(temperature) or not start <= observed < end:
            continue
        kind = str(row.get("metarType") or "METAR").upper()
        if kind not in {"METAR", "SPECI"}:
            continue
        compact.append({
            "observed_at_utc": _iso(observed),
            "received_at_utc": str(row.get("receiptTime") or ""),
            "report_type": kind,
            "temperature_c": temperature,
            "raw_report": str(row.get("rawOb") or "").strip(),
        })
    if not compact:
        raise ContractError("awc_taf_calibration_truth_missing")
    compact.sort(key=lambda row: (row["observed_at_utc"], row["report_type"], row["raw_report"]))
    return max(float(row["temperature_c"]) for row in compact), compact


def _truth_only_sample(
    *,
    station_id: str,
    event_date: str,
    timezone_name: str,
    authority: Mapping[str, Any],
    start: datetime,
    end: datetime,
    truth_tmax_c: float,
    observations: list[dict[str, Any]],
    taf_observation: Mapping[str, Any],
    taf_source_url: str,
    taf_response_sha256: str,
    metar_source_url: str,
    metar_response_sha256: str,
    taf_exclusion_reason: str,
) -> dict[str, Any]:
    """Preserve formal AWC truth when only the TAF side is structurally absent.

    This is deliberately a separate artifact lane.  A truth-only record has
    no *TAF calibration* fields and is never a P0-M input; it retains the
    bounded official AWC evidence needed for a later, independently reviewed
    truth recovery.  The selected no-TX bulletin is retained as an
    ``taf_observation`` sidecar (including its response receipt) so a
    structural gap is distinguishable from a missing raw object without
    allowing the bulletin to enter the historical TAF table or exact join.
    Historical replay is not a native production arrival, so this helper also
    cannot authorize a direct SSOT write.
    """

    normalized_observations: list[dict[str, Any]] = []
    correction_suspected = False
    for row in observations:
        raw_report = str(row.get("raw_report") or "").strip()
        raw_sha256 = hashlib.sha256(raw_report.encode("utf-8")).hexdigest()
        correction = bool(CORRECTION_RE.search(raw_report))
        correction_suspected = correction_suspected or correction
        normalized_observations.append({
            "observed_at_utc": str(row["observed_at_utc"]),
            "received_at_utc": str(row.get("received_at_utc") or ""),
            "report_type": str(row["report_type"]),
            "temperature_c": float(row["temperature_c"]),
            "raw_report": raw_report,
            "raw_sha256": raw_sha256,
            "correction_suspected": correction,
        })
    evidence_identity = {
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "source_lane": "AWC",
        "truth_tmax_c": truth_tmax_c,
        "observations": normalized_observations,
        "metar_response_sha256": metar_response_sha256,
    }
    evidence_sha = content_sha256(evidence_identity)
    # Keep only the immutable, non-calibration portion of the selected AWC
    # bulletin.  In particular, ``tx_c`` remains explicitly absent/None and
    # this object is never accepted as the ``taf`` feature map used by P0-M.
    retained_taf = {
        key: taf_observation[key]
        for key in (
            "issue_time_utc",
            "available_time_utc",
            "cloud_cover_fraction",
            "precipitation_probability",
            "wind_speed_mps",
            "raw_taf",
            "raw_taf_sha256",
        )
        if key in taf_observation
    }
    retained_taf["tx_c"] = None
    retained_taf["tx_available"] = False
    retained_taf["source_class"] = "official_date_addressed_historical_replay"
    retained_taf["taf_response_sha256"] = taf_response_sha256
    retained_taf["taf_source_url"] = taf_source_url
    retained_taf["taf_calibration_eligible"] = False
    sample_identity = {
        "truth_evidence_sha256": evidence_sha,
        "taf_exclusion_reason": taf_exclusion_reason,
        "taf_raw_sha256": str(retained_taf.get("raw_taf_sha256") or ""),
        "taf_response_sha256": taf_response_sha256,
    }
    return {
        "truth_sample_id": "awc-truth-only-" + content_sha256(sample_identity)[:32],
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "source_lane": "AWC",
        "truth_label_authority": str(authority["truth_label_authority"]),
        "truth_authority": dict(authority),
        "reference_only": bool(authority["reference_only"]),
        "truth_only": True,
        # Keep this an explicit negative contract rather than relying on the
        # absence of a TAF object.  Downstream P0-M preflight validates it.
        "taf_p0m_eligible": False,
        "taf_observation": retained_taf,
        "settlement_window": {
            "start_utc": _iso(start),
            "end_utc": _iso(end),
        },
        "truth": {
            "source_lane": "AWC",
            "truth_label_authority": str(authority["truth_label_authority"]),
            "tmax_c": truth_tmax_c,
            "observation_count": len(normalized_observations),
            "observations": normalized_observations,
            "evidence_sha256": evidence_sha,
        },
        "taf_exclusion": {
            "reason": taf_exclusion_reason,
            "classification": "official_taf_structural_missing_field",
        },
        "truth_recovery": {
            "historical_replay_only": True,
            "automatic_ssot_commit_eligible": False,
            "requires_authoritative_review": True,
            "requires_manual_correction_review": correction_suspected,
        },
        "receipts": {
            "taf_source_url": taf_source_url,
            "taf_response_sha256": taf_response_sha256,
            "metar_source_url": metar_source_url,
            "metar_response_sha256": metar_response_sha256,
        },
    }


def capture(
    *,
    station_id: str,
    timezone_name: str,
    start_date: str,
    end_date: str,
    timeout_sec: int,
    request_interval_sec: float,
    source_lane: str = "AWC",
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    first = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    if first > last:
        raise ContractError("awc_taf_calibration_date_range_invalid")
    count = (last - first).days + 1
    if count > MAX_DAYS:
        raise ContractError("awc_taf_calibration_date_range_too_large")
    station = station_id.strip().upper()
    authority = _truth_authority_for_source_lane(source_lane)
    station_lane = str(authority["station_source_lane"])
    truth_label_authority = str(authority["truth_label_authority"])
    now = datetime.now(timezone.utc)
    total_bytes = int(
        ((existing or {}).get("resource_bounds") or {}).get(
            "actual_response_bytes", 0
        )
    )
    if existing is not None:
        existing_end_text = str(existing.get("end_date") or "")
        try:
            existing_end = date.fromisoformat(existing_end_text)
        except ValueError as exc:
            raise ContractError(
                "awc_taf_calibration_resume_scope_mismatch"
            ) from exc
        if (
            existing.get("schema") != SCHEMA
            or existing.get("station_id") != station
            or existing.get("settlement_timezone") != timezone_name
            or existing.get("start_date") != start_date
            or existing_end < first
            or existing_end > last
        ):
            raise ContractError("awc_taf_calibration_resume_scope_mismatch")
        if (
            str(existing.get("source_lane") or "").strip().upper()
            != station_lane
            or str(existing.get("truth_label_authority") or "")
            != truth_label_authority
            or existing.get("truth_authority") != authority
        ):
            raise ContractError("awc_taf_calibration_resume_authority_mismatch")
    samples: list[dict[str, Any]] = [
        dict(item)
        for item in (existing or {}).get("samples", [])
        if isinstance(item, Mapping)
    ]
    for item in samples:
        if (
            str(item.get("source_lane") or "").strip().upper()
            != station_lane
            or str(item.get("truth_label_authority") or "")
            != truth_label_authority
            or item.get("truth_authority") != authority
        ):
            raise ContractError("awc_taf_calibration_resume_sample_authority_missing")
    truth_only_samples: list[dict[str, Any]] = [
        dict(item)
        for item in (existing or {}).get("truth_only_samples", [])
        if isinstance(item, Mapping)
    ]
    declared_truth_only_count = (existing or {}).get(
        "truth_only_sample_count",
        0,
    )
    try:
        if int(declared_truth_only_count) != len(truth_only_samples):
            raise ContractError("awc_taf_truth_only_resume_count_invalid")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError("awc_taf_truth_only_resume_count_invalid") from exc
    for item in truth_only_samples:
        if (
            str(item.get("station_id") or "").strip().upper() != station
            or str(item.get("settlement_timezone") or "") != timezone_name
            or str(item.get("source_lane") or "").strip().upper()
            != station_lane
            or str(item.get("truth_label_authority") or "")
            != truth_label_authority
            or item.get("truth_authority") != authority
            or item.get("truth_only") is not True
            or item.get("taf_p0m_eligible") is not False
            or "taf" in item
        ):
            raise ContractError("awc_taf_truth_only_resume_contract_invalid")
    excluded_samples: list[dict[str, str]] = [
        dict(item)
        for item in (existing or {}).get("excluded_samples", [])
        if isinstance(item, Mapping)
    ]
    # Older captures admitted rare AWC database timestamps that predate their
    # own TAF issue time.  Do not retain those rows on resume: they could never
    # be committed by the strict historical ingress.  Quarantine only this
    # demonstrably invalid temporal ordering and leave all valid completed
    # days untouched, avoiding both a source fallback and needless replay.
    resumed_samples: list[dict[str, Any]] = []
    # Decision-anchor exclusions are retryable.  The old exact-anchor query
    # could return a bulletin whose provider timestamp was just after the
    # anchor even though the preceding bulletin was valid.  Structural TX
    # absence and invalid provider timestamp ordering remain completed gaps.
    excluded_dates = {
        str(item.get("event_date") or "")
        for item in excluded_samples
        if str(item.get("classification") or "")
        != "official_taf_not_available_at_decision_anchor"
    }
    for item in samples:
        taf = item.get("taf")
        event_date = str(item.get("event_date") or "")
        try:
            issue_time = _parse_utc(
                taf.get("issue_time_utc")
                if isinstance(taf, Mapping)
                else None
            )
            available_time = _parse_utc(
                taf.get("available_time_utc")
                if isinstance(taf, Mapping)
                else None
            )
        except (TypeError, ValueError, OverflowError):
            resumed_samples.append(item)
            continue
        if issue_time <= available_time:
            resumed_samples.append(item)
            continue
        if event_date and event_date not in excluded_dates:
            excluded_samples.append({
                "event_date": event_date,
                "reason": (
                    "ContractError:"
                    "awc_taf_historical_available_before_issue"
                ),
                "classification": "official_taf_arrival_timestamp_invalid",
            })
            excluded_dates.add(event_date)
    samples = resumed_samples
    blockers: list[dict[str, str]] = []
    completed_dates = {str(item.get("event_date") or "") for item in samples}
    completed_dates.update(
        str(item.get("event_date") or "")
        for item in truth_only_samples
    )
    completed_dates.update(
        str(item.get("event_date") or "")
        for item in excluded_samples
        if str(item.get("classification") or "")
        != "official_taf_not_available_at_decision_anchor"
    )
    cursor = first
    last_request_at = 0.0
    while cursor <= last:
        assert_process_memory_budget()
        event_date = cursor.isoformat()
        if event_date in completed_dates:
            cursor += timedelta(days=1)
            continue
        start, end = _event_window(event_date, timezone_name)
        if end > now:
            raise ContractError("awc_taf_calibration_open_truth_day_forbidden")
        # Fixed decision anchor prevents choosing a later TAF with hindsight.
        anchor = start + timedelta(hours=1)
        # AWC's historical date filter is minute addressed.  Query one minute
        # before the anchor so a TAF issued at the boundary (but arriving a
        # few seconds later) cannot hide the prior valid bulletin.
        query_anchor = anchor - timedelta(minutes=1)
        taf_url = TAF_ENDPOINT + "?" + urllib.parse.urlencode({
            "ids": station,
            "format": "json",
            "time": "issue",
            "date": query_anchor.strftime("%Y%m%d_%H%M"),
        })
        metar_url = METAR_ENDPOINT + "?" + urllib.parse.urlencode({
            "ids": station,
            "format": "json",
            "taf": "false",
            "hours": "24",
            "date": end.strftime("%Y%m%d_%H%M"),
        })
        try:
            wait = request_interval_sec - (time.monotonic() - last_request_at)
            if wait > 0:
                time.sleep(wait)
            taf_rows, taf_response_sha, taf_bytes = _fetch_json(
                taf_url, timeout_sec=timeout_sec
            )
            last_request_at = time.monotonic()
            wait = request_interval_sec - (time.monotonic() - last_request_at)
            if wait > 0:
                time.sleep(wait)
            metar_rows, metar_response_sha, metar_bytes = _fetch_json(
                metar_url, timeout_sec=timeout_sec
            )
            last_request_at = time.monotonic()
            total_bytes += taf_bytes + metar_bytes
            if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                raise ContractError("awc_taf_calibration_total_response_cap_exceeded")
            # Truth must be evaluated before the optional TAF-TX branch.
            # Previously a missing TX raised first and discarded a complete
            # official AWC closed-day truth along with the unusable P0-M leg.
            truth_tmax, observations = _truth_rows(
                metar_rows,
                station_id=station,
                start=start,
                end=end,
            )
            try:
                taf, _ = _taf_row(
                    taf_rows,
                    station_id=station,
                    start=start,
                    end=end,
                    event_date=event_date,
                    timezone_name=timezone_name,
                    decision_anchor=anchor,
                )
            except ContractError as exc:
                if str(exc) != "awc_taf_tx_for_settlement_day_missing":
                    raise
                # Re-select the same bulletin without requiring TX so the
                # official raw TAF and response receipt are retained as an
                # explicit non-calibration observation.  This is a local
                # parse-only retry; it performs no network call and still
                # cannot produce a historical TAF/P0-M sample.
                taf_observation, _ = _taf_row(
                    taf_rows,
                    station_id=station,
                    start=start,
                    end=end,
                    event_date=event_date,
                    timezone_name=timezone_name,
                    require_tx=False,
                    require_features=False,
                    decision_anchor=anchor,
                )
                # No TX means no TAF/P0-M row, but it cannot erase the
                # independent, fully closed official AWC truth evidence.
                truth_only_samples.append(_truth_only_sample(
                    station_id=station,
                    event_date=event_date,
                    timezone_name=timezone_name,
                    authority=authority,
                    start=start,
                    end=end,
                    truth_tmax_c=truth_tmax,
                    observations=observations,
                    taf_observation=taf_observation,
                    taf_source_url=taf_url,
                    taf_response_sha256=taf_response_sha,
                    metar_source_url=metar_url,
                    metar_response_sha256=metar_response_sha,
                    taf_exclusion_reason=str(exc),
                ))
                excluded_samples = [
                    item
                    for item in excluded_samples
                    if str(item.get("event_date") or "") != event_date
                ]
                raise
            if _parse_utc(taf["available_time_utc"]) > anchor:
                raise ContractError(
                    "awc_taf_historical_available_after_decision_anchor"
                )
            # A successful retry supersedes any stale exclusion retained by a
            # prior capture made with the exact-anchor query.
            excluded_samples = [
                item
                for item in excluded_samples
                if str(item.get("event_date") or "") != event_date
            ]
            sample_identity = {
                "station_id": station,
                "event_date": event_date,
                "settlement_timezone": timezone_name,
                "taf_issue_time_utc": taf["issue_time_utc"],
                "taf_raw_sha256": taf["raw_taf_sha256"],
                "truth_tmax_c": truth_tmax,
                "truth_rows_sha256": content_sha256(observations),
            }
            samples.append({
                "sample_id": "taf-sample-" + content_sha256(sample_identity)[:32],
                "station_id": station,
                "event_date": event_date,
                "settlement_timezone": timezone_name,
                "source_lane": station_lane,
                "truth_label_authority": truth_label_authority,
                "truth_authority": authority,
                "reference_only": bool(authority["reference_only"]),
                "settlement_window": {"start_utc": _iso(start), "end_utc": _iso(end)},
                "decision_anchor_utc": _iso(anchor),
                "taf": taf,
                "truth": {
                    "source_lane": "AWC",
                    "truth_label_authority": truth_label_authority,
                    "tmax_c": truth_tmax,
                    "observation_count": len(observations),
                    "observations": observations,
                },
                "receipts": {
                    "taf_source_url": taf_url,
                    "taf_response_sha256": taf_response_sha,
                    "metar_source_url": metar_url,
                    "metar_response_sha256": metar_response_sha,
                },
            })
        except Exception as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:160]}"
            if str(exc) in {
                "awc_taf_tx_for_settlement_day_missing",
                "awc_taf_peak_window_features_missing",
                "awc_taf_historical_available_after_decision_anchor",
                "awc_taf_historical_available_before_issue",
            }:
                excluded_samples.append({
                    "event_date": event_date,
                    "reason": reason,
                    "classification": (
                        "official_taf_not_available_at_decision_anchor"
                        if str(exc)
                        == "awc_taf_historical_available_after_decision_anchor"
                        else "official_taf_arrival_timestamp_invalid"
                        if str(exc)
                        == "awc_taf_historical_available_before_issue"
                        else "official_taf_structural_missing_field"
                    ),
                })
            else:
                blockers.append({"event_date": event_date, "reason": reason})
        cursor += timedelta(days=1)
    samples.sort(key=lambda item: str(item.get("event_date") or ""))
    truth_only_samples.sort(
        key=lambda item: str(item.get("event_date") or "")
    )
    excluded_samples.sort(key=lambda item: str(item.get("event_date") or ""))
    identity = {
        "schema": SCHEMA,
        "provider": "NOAA Aviation Weather Center",
        "source_class": "official_date_addressed_historical_replay",
        "station_id": station,
        "settlement_timezone": timezone_name,
        "source_lane": station_lane,
        "truth_label_authority": truth_label_authority,
        "truth_authority": authority,
        "start_date": start_date,
        "end_date": end_date,
        "sample_count": len(samples),
        "samples": samples,
        "truth_only_sample_count": len(truth_only_samples),
        "truth_only_samples": truth_only_samples,
        "excluded_sample_count": len(excluded_samples),
        "excluded_samples": excluded_samples,
        "blockers": blockers,
        "resource_bounds": {
            "max_days": MAX_DAYS,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "max_total_response_bytes": MAX_TOTAL_RESPONSE_BYTES,
            "actual_response_bytes": total_bytes,
            "max_output_bytes": MAX_OUTPUT_BYTES,
        },
        "boundary": {
            "native_local_receipt": False,
            "official_awc_database_availability_preserved": True,
            "historical_replay_only": True,
            "truth_days_closed": True,
            "truth_only_samples_are_not_taf_samples": True,
            "truth_only_enters_p0m": False,
            "truth_only_writes_production_db": False,
            "reference_only": bool(authority["reference_only"]),
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
            "replacement_required_when_canonical_history_available": bool(
                authority["replacement_required_when_canonical_history_available"]
            ),
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--settlement-timezone", default="Asia/Seoul")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--source-lane", choices=("AWC",), default="AWC")
    parser.add_argument("--timeout-sec", type=int, default=20)
    parser.add_argument("--request-interval-sec", type=float, default=MIN_REQUEST_INTERVAL_SEC)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse completed rows in the existing output and retry only transport blockers.",
    )
    args = parser.parse_args()
    if not 5 <= args.timeout_sec <= 60:
        raise SystemExit("timeout-sec must be in [5,60]")
    if not MIN_REQUEST_INTERVAL_SEC <= args.request_interval_sec <= 5.0:
        raise SystemExit(f"request-interval-sec must be in [{MIN_REQUEST_INTERVAL_SEC},5]")
    output = args.output.expanduser().resolve()
    free_bytes = shutil.disk_usage(output.parent if output.parent.exists() else ROOT).free
    if free_bytes < MIN_TARGET_FREE_BYTES + MAX_OUTPUT_BYTES:
        raise SystemExit("insufficient free disk for bounded TAF calibration capture")
    existing = None
    if args.resume and output.exists():
        loaded = load_bounded_json(output)
        if not isinstance(loaded, Mapping):
            raise SystemExit("resume artifact must be a JSON object")
        existing = loaded
    result = capture(
        station_id=args.station_id,
        timezone_name=args.settlement_timezone,
        start_date=args.start_date,
        end_date=args.end_date,
        timeout_sec=args.timeout_sec,
        request_interval_sec=args.request_interval_sec,
        source_lane=args.source_lane,
        existing=existing,
    )
    write_bounded_json(output, result, limit=MAX_OUTPUT_BYTES)
    print(json.dumps({
        "schema": result["schema"],
        "sample_count": result["sample_count"],
        "excluded_sample_count": result["excluded_sample_count"],
        "blocker_count": len(result["blockers"]),
        "actual_response_bytes": result["resource_bounds"]["actual_response_bytes"],
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["sample_count"] >= 24 and not result["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
