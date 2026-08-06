"""Canonical historical AWC replay ingress from retained official raw objects.

Legacy observation packets may locate candidate raw objects, but they do not
grant lineage.  This module assigns the historical replay role only after the
retained object independently proves the official AWC HTTPS endpoint, raw body
hash, station, report type, temperature, timestamp, and closed station day.
"""

from __future__ import annotations

from datetime import date, datetime, time as wall_time, timedelta, timezone
from collections import Counter
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_historical_awc_replay_evidence_v4"
LEGACY_OBSERVATION_SCHEMA = "bayes_basket_target_canonical_observation_evidence_v1"
SOURCE_ROLE = "AWC_HISTORICAL_REPLAY_INGRESS"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
MAX_INPUT_PACKETS = 128
MAX_CANDIDATE_ROWS = 200_000
MAX_RAW_OBJECT_BYTES = 1 * 1024 * 1024
_METAR = re.compile(r"\b(?:METAR|SPECI)\s+([A-Z0-9]{4})\s+\d{6}Z\b")
_TEMP = re.compile(r"(?<!TX)(?<!TN)\b(M?\d{2}|//)/(M?\d{2}|//)\b")
_PRECISE_TEMP = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")


def _utc(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{label}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha(value: Any, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ContractError(f"{label}_invalid")
    return result


def _signed_temperature(value: str) -> float:
    if value == "//":
        raise ContractError("historical_awc_raw_temperature_missing")
    return float(-int(value[1:]) if value.startswith("M") else int(value))


def _raw_temperature(raw_text: str) -> float:
    precise = _PRECISE_TEMP.search(raw_text.replace("\n", " "))
    if precise is not None:
        magnitude = int(precise.group(2)) / 10.0
        return -magnitude if precise.group(1) == "1" else magnitude
    coarse = _TEMP.search(raw_text.replace("\n", " "))
    if coarse is None:
        raise ContractError("historical_awc_raw_temperature_missing")
    return _signed_temperature(coarse.group(1))


def _day_window(day_text: str, timezone_name: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(day_text)
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, wall_time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), wall_time.min, tzinfo=zone).astimezone(timezone.utc)
    return start, end


def _expected_hour_slots(day_text: str, timezone_name: str) -> list[str]:
    start, end = _day_window(day_text, timezone_name)
    cursor = start.replace(minute=0, second=0, microsecond=0)
    if cursor < start:
        cursor += timedelta(hours=1)
    result: list[str] = []
    while cursor < end:
        result.append(_utc_text(cursor))
        cursor += timedelta(hours=1)
    return result


def _legacy_packet_rows(packet: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if packet.get("schema") != LEGACY_OBSERVATION_SCHEMA:
        raise ContractError("historical_awc_legacy_packet_schema_invalid")
    supplied = _sha(packet.get("evidence_sha256"), "historical_awc_legacy_evidence_sha256")
    identity = {key: value for key, value in packet.items() if key != "evidence_sha256"}
    # Some merge receipts add a second outer receipt hash after evidence_sha256.
    if "receipt_sha256" in identity:
        identity.pop("receipt_sha256")
    if supplied != content_sha256(identity):
        raise ContractError("historical_awc_legacy_evidence_sha256_mismatch")
    if packet.get("historical_only") is not True or packet.get("canonical_ingress") is not True:
        raise ContractError("historical_awc_legacy_packet_boundary_invalid")
    rows = packet.get("observations")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ContractError("historical_awc_legacy_observations_invalid")
    return rows


def _load_official_raw(raw_root: Path, raw_sha256: str) -> tuple[dict[str, Any], Path]:
    path = raw_root / "objects" / raw_sha256[:2] / f"{raw_sha256}.json.gz"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RAW_OBJECT_BYTES:
        raise ContractError("historical_awc_raw_object_missing")
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = handle.read(MAX_RAW_OBJECT_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        raise ContractError("historical_awc_raw_object_invalid") from exc
    if len(raw.encode("utf-8")) > MAX_RAW_OBJECT_BYTES:
        raise ContractError("historical_awc_raw_object_too_large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError("historical_awc_raw_object_json_invalid") from exc
    if not isinstance(value, Mapping):
        raise ContractError("historical_awc_raw_object_mapping_required")
    return dict(value), path.resolve()


def _verified_observation(
    candidate: Mapping[str, Any],
    *,
    raw_root: Path,
    station_id: str,
    settlement_timezone: str,
    first_day: date,
    last_day: date,
    as_of: datetime,
) -> dict[str, Any] | None:
    if str(candidate.get("station_id") or "").strip().upper() != station_id:
        return None
    if str(candidate.get("source_lane") or "").strip().lower() != "awc":
        # Legacy packets may contain other lanes.  They are locators only;
        # no non-AWC row is adapted or allowed to influence this exact lane.
        return None
    raw_sha = _sha(candidate.get("raw_sha256"), "historical_awc_candidate_raw_sha256")
    raw, raw_path = _load_official_raw(raw_root, raw_sha)
    raw_text = str(raw.get("raw_text") or "")
    if hashlib.sha256(raw_text.encode("utf-8")).hexdigest() != raw_sha:
        raise ContractError("historical_awc_raw_text_sha256_mismatch")
    parsed_url = urlparse(str(raw.get("source_url") or ""))
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "aviationweather.gov"
        or parsed_url.path != "/api/data/metar"
    ):
        raise ContractError("historical_awc_official_source_url_invalid")
    raw_poll = str(raw.get("source_poll_id") or "")
    candidate_poll = str(candidate.get("source_poll_id") or "")
    if not raw_poll.startswith("awc-historical-") or not candidate_poll.startswith("awc-historical-"):
        raise ContractError("historical_awc_source_poll_id_invalid")
    report_type = str(raw.get("report_type") or "").strip().upper()
    if report_type not in {"METAR", "SPECI"}:
        raise ContractError("historical_awc_report_type_invalid")
    match = _METAR.search(raw_text.replace("\n", " "))
    if match is None or match.group(1).upper() != station_id:
        raise ContractError("historical_awc_raw_station_mismatch")
    temperature = _raw_temperature(raw_text)
    valid_at = _utc(raw.get("valid_at_utc"), "historical_awc_valid_at_utc")
    if (
        str(raw.get("station_id") or "").strip().upper() != station_id
        or str(raw.get("source_lane") or "").strip().lower() != "awc"
        or str(candidate.get("report_type") or "").strip().upper() != report_type
        or _utc(candidate.get("valid_at_utc"), "historical_awc_candidate_valid_at_utc") != valid_at
    ):
        raise ContractError("historical_awc_candidate_raw_mismatch")
    try:
        raw_temperature = float(raw.get("reported_temperature_c"))
    except (TypeError, ValueError) as exc:
        raise ContractError("historical_awc_temperature_invalid") from exc
    if not all(math.isfinite(value) for value in (temperature, raw_temperature)):
        raise ContractError("historical_awc_temperature_invalid")
    # Some archived AWC JSON rows retain tenths even when the raw remarks omit
    # or truncate the precise T-group.  In that case the visible TT/DD group
    # proves the rounded integer; require the official JSON value to remain
    # within that exact rounding interval instead of silently truncating it.
    if temperature != raw_temperature and abs(temperature - raw_temperature) > 0.5:
        raise ContractError("historical_awc_temperature_mismatch")
    temperature = raw_temperature
    local_day = valid_at.astimezone(ZoneInfo(settlement_timezone)).date()
    if local_day < first_day or local_day > last_day:
        return None
    _start, day_end = _day_window(local_day.isoformat(), settlement_timezone)
    if day_end > as_of:
        raise ContractError("historical_awc_station_day_not_closed")
    identity = {
        "observation_id": "awc-hist-" + content_sha256({
            "station_id": station_id,
            "valid_at_utc": _utc_text(valid_at),
            "report_type": report_type,
            "raw_sha256": raw_sha,
        })[:32],
        "station_id": station_id,
        "source_lane": "AWC",
        "source_role": SOURCE_ROLE,
        "authority_scope": AUTHORITY_SCOPE,
        "settlement_timezone": settlement_timezone,
        "event_date": local_day.isoformat(),
        "evidence_role": "FORMAL",
        "reference_only": False,
        "qc_status": "ACCEPTED",
        "report_type": report_type,
        "valid_at_utc": _utc_text(valid_at),
        "reported_temperature_c": temperature,
        "raw_sha256": raw_sha,
        "raw_object_path": str(raw_path),
        "official_source_url": str(raw["source_url"]),
        "source_poll_id": raw_poll,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_historical_awc_replay_evidence(
    *,
    packets: Sequence[Mapping[str, Any]],
    raw_root: Path,
    station_id: str,
    settlement_timezone: str,
    start_date: str,
    end_date: str,
    as_of_utc: str,
) -> dict[str, Any]:
    if not packets or len(packets) > MAX_INPUT_PACKETS:
        raise ContractError("historical_awc_packet_count_invalid")
    station = str(station_id or "").strip().upper()
    if not station:
        raise ContractError("historical_awc_station_id_missing")
    try:
        ZoneInfo(settlement_timezone)
        first = date.fromisoformat(start_date)
        last = date.fromisoformat(end_date)
    except (ValueError, KeyError) as exc:
        raise ContractError("historical_awc_scope_invalid") from exc
    if first > last or (last - first).days >= 120:
        raise ContractError("historical_awc_date_range_invalid")
    as_of = _utc(as_of_utc, "historical_awc_as_of_utc")
    root = raw_root.expanduser().resolve()
    if root.is_symlink() or not (root / "objects").is_dir():
        raise ContractError("historical_awc_raw_root_invalid")
    candidates: list[Mapping[str, Any]] = []
    input_evidence: list[str] = []
    for packet in packets:
        candidates.extend(_legacy_packet_rows(packet))
        input_evidence.append(_sha(packet.get("evidence_sha256"), "historical_awc_input_evidence_sha256"))
        if len(candidates) > MAX_CANDIDATE_ROWS:
            raise ContractError("historical_awc_candidate_row_limit_exceeded")
    by_identity: dict[str, dict[str, Any]] = {}
    slots: dict[tuple[str, str], set[str]] = {}
    rejection_counts: Counter[str] = Counter()
    rejection_dates: dict[str, Counter[str]] = {}
    for candidate in candidates:
        try:
            observation = _verified_observation(
                candidate,
                raw_root=root,
                station_id=station,
                settlement_timezone=settlement_timezone,
                first_day=first,
                last_day=last,
                as_of=as_of,
            )
        except ContractError as exc:
            if str(exc) in {
                "historical_awc_raw_object_missing",
                "historical_awc_raw_station_mismatch",
                "historical_awc_raw_temperature_missing",
                "historical_awc_temperature_mismatch",
            }:
                reason = str(exc)
                rejection_counts[reason] += 1
                try:
                    candidate_date = _utc(
                        candidate.get("valid_at_utc"),
                        "historical_awc_candidate_valid_at_utc",
                    ).astimezone(ZoneInfo(settlement_timezone)).date().isoformat()
                except ContractError:
                    candidate_date = ""
                if first.isoformat() <= candidate_date <= last.isoformat():
                    rejection_dates.setdefault(candidate_date, Counter())[reason] += 1
                continue
            raise
        if observation is None:
            continue
        observation_hash = str(observation["content_sha256"])
        by_identity.setdefault(observation_hash, observation)
        slot = (str(observation["report_type"]), str(observation["valid_at_utc"]))
        slots.setdefault(slot, set()).add(str(observation["raw_sha256"]))
    conflicted = {slot for slot, hashes in slots.items() if len(hashes) > 1}
    observations = sorted(
        (
            row for row in by_identity.values()
            if (str(row["report_type"]), str(row["valid_at_utc"])) not in conflicted
        ),
        key=lambda row: (row["valid_at_utc"], row["report_type"], row["raw_sha256"]),
    )
    observed_dates = sorted({str(row["event_date"]) for row in observations})
    expected_dates: list[str] = []
    cursor = first
    while cursor <= last:
        expected_dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    observations_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in observations:
        observations_by_date.setdefault(str(row["event_date"]), []).append(row)
    completeness_rows: list[dict[str, Any]] = []
    truth_ready_dates: list[str] = []
    truth_blocked_dates: list[str] = []
    missing_date_diagnostics = []
    for event_date in expected_dates:
        expected_slots = _expected_hour_slots(event_date, settlement_timezone)
        date_rows = observations_by_date.get(event_date, [])
        observed_awc_report_slots = sorted({
            _utc_text(
                _utc(
                    row["valid_at_utc"],
                    "historical_awc_observation_valid_at_utc",
                ).replace(minute=0, second=0, microsecond=0)
            )
            for row in date_rows
            if row["report_type"] in {"METAR", "SPECI"}
        })
        missing_slots = sorted(set(expected_slots) - set(observed_awc_report_slots))
        conflict_slots = sorted(
            stamp
            for report_type, stamp in conflicted
            if report_type in {"METAR", "SPECI"}
            and _utc(stamp, "historical_awc_conflict_slot_utc")
            .astimezone(ZoneInfo(settlement_timezone))
            .date()
            .isoformat()
            == event_date
        )
        complete = not missing_slots and not conflict_slots
        if complete:
            truth_ready_dates.append(event_date)
        else:
            truth_blocked_dates.append(event_date)
        reasons = rejection_dates.get(event_date, Counter())
        diagnostics = [
            {"reason": reason, "candidate_count": count}
            for reason, count in sorted(reasons.items())
        ]
        if missing_slots:
            diagnostics.append({
                "reason": "historical_awc_report_hour_coverage_missing",
                "candidate_count": len(missing_slots),
            })
        if conflict_slots:
            diagnostics.append({
                "reason": "historical_awc_revision_slots_quarantined",
                "candidate_count": len(conflict_slots),
            })
        if not diagnostics and not complete:
            diagnostics = [
                {
                    "reason": "historical_awc_locator_absent_or_no_candidate",
                    "candidate_count": 0,
                }
            ]
        completeness_rows.append({
            "event_date": event_date,
            "station_day_start_utc": _utc_text(_day_window(event_date, settlement_timezone)[0]),
            "station_day_end_utc": _utc_text(_day_window(event_date, settlement_timezone)[1]),
            "expected_awc_report_hour_slots": expected_slots,
            "observed_awc_report_hour_slots": observed_awc_report_slots,
            "missing_awc_report_hour_slots": missing_slots,
            "valid_speci_count": sum(row["report_type"] == "SPECI" for row in date_rows),
            "quarantined_metar_revision_slots": conflict_slots,
            "truth_ready": complete,
        })
        if not complete:
            missing_date_diagnostics.append(
                {
                    "event_date": event_date,
                    "blockers": ["DATA_MISSING", "OOS_SAMPLE_SHORT"],
                    "diagnostics": diagnostics,
                }
            )
    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "target_timezone": settlement_timezone,
        "source_lane": "AWC",
        "source_role": SOURCE_ROLE,
        "authority_scope": AUTHORITY_SCOPE,
        "historical_only": True,
        "canonical_ingress": True,
        "as_of_utc": _utc_text(as_of),
        "start_date": start_date,
        "end_date": end_date,
        "input_evidence_sha256s": sorted(set(input_evidence)),
        "observations": observations,
        "observed_dates": observed_dates,
        "truth_ready_dates": truth_ready_dates,
        "truth_blocked_dates": truth_blocked_dates,
        "station_day_completeness": completeness_rows,
        "missing_date_diagnostics": missing_date_diagnostics,
        "quarantined_revision_slots": ["|".join(slot) for slot in sorted(conflicted)],
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "checks": {
            "official_awc_raw_reverified": True,
            "raw_text_sha256_reverified": True,
            "station_timezone_exact": True,
            "closed_days_only": True,
            "metar_any_valid_minute_allowed": True,
            "speci_allowed": True,
            "reference_only_excluded": True,
            "station_day_awc_report_hour_coverage_explicit": True,
        },
        "boundary": {
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_arrival_evidence": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
        },
    }
    return {**identity, "evidence_sha256": content_sha256(identity)}
