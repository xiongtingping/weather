#!/usr/bin/env python3
"""Build a bounded, read-only manual-review packet for AWC COR reports.

The packet is evidence only.  It never appends a correction review, changes
the resident database, talks to a service, or submits an order.  A reviewer
must still make an explicit append-only decision through the established
correction-review flow.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, time, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.correction_review import (
    CLEAR_REFERENCE_ONLY,
    QUARANTINE_STATION_DAY,
)
from bayes_basket_target.resource_bounds import (
    ResourceBoundError,
    assert_process_memory_budget,
    write_bounded_json,
)


SCHEMA = "awc_correction_review_packet_v1"
DEFAULT_SOURCE_DB = ROOT / "data" / "archive.db"
DEFAULT_EVENT_DATE = "2026-07-31"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / "awc_correction_review_packet_20260731.json"
SOURCE_LANE = "awc"
MAX_STATIONS = 45
MAX_CORRECTIONS = 256
MAX_SAME_SLOT_ORIGINALS = 4
MAX_PACKET_BYTES = 1 * 1024 * 1024
FETCH_BATCH_ROWS = 32
COR_TOKEN = re.compile(r"(?:^|\s)COR(?:\s|$)", re.IGNORECASE)


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"awc_correction_review_{name}_missing")
    return result


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(name, value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"awc_correction_review_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"awc_correction_review_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"awc_correction_review_{name}_sha_invalid")
    return result


def _event_day(value: Any) -> str:
    result = _text("event_date", value)
    try:
        return datetime.fromisoformat(result).date().isoformat()
    except ValueError as exc:
        raise ContractError("awc_correction_review_event_date_invalid") from exc


def _resolve_output(path: Path) -> Path:
    output = path.expanduser().resolve()
    root = (ROOT / "output" / "audits").resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise ContractError("awc_correction_review_output_outside_audits") from exc
    return output


def _station_timezones(conn: sqlite3.Connection, *, event_date: str) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT station_id,settlement_timezone "
            "FROM reported_trajectory_market_registry "
            "WHERE event_date=? AND lower(source_lane)=? AND source_eligible=1 "
            "ORDER BY station_id,settlement_timezone",
            (event_date, SOURCE_LANE),
        )
    except sqlite3.Error as exc:
        raise ContractError("awc_correction_review_registry_query_failed") from exc
    values: dict[str, set[str]] = defaultdict(set)
    for station_raw, timezone_raw in rows:
        station = _text("registry_station_id", station_raw).upper()
        zone = _text("registry_timezone", timezone_raw)
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError as exc:
            raise ContractError(f"awc_correction_review_timezone_invalid:{station}") from exc
        values[station].add(zone)
        if len(values) > MAX_STATIONS:
            raise ContractError("awc_correction_review_station_scope_cap_exceeded")
        assert_process_memory_budget()
    if not values:
        raise ContractError("awc_correction_review_registry_scope_empty")
    ambiguous = sorted(station for station, zones in values.items() if len(zones) != 1)
    if ambiguous:
        raise ContractError(
            "awc_correction_review_registry_timezone_ambiguous:" + ",".join(ambiguous)
        )
    return {station: next(iter(zones)) for station, zones in sorted(values.items())}


def _bounds(event_date: str, station_timezones: Mapping[str, str]) -> tuple[str, str]:
    starts: list[datetime] = []
    ends: list[datetime] = []
    day = datetime.fromisoformat(event_date).date()
    for timezone_name in station_timezones.values():
        zone = ZoneInfo(timezone_name)
        start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
        end = datetime.combine(day.fromordinal(day.toordinal() + 1), time.min, tzinfo=zone).astimezone(timezone.utc)
        starts.append(start)
        ends.append(end)
    if not starts or not ends:
        raise ContractError("awc_correction_review_station_window_empty")
    return _stamp(min(starts)), _stamp(max(ends))


def _fetch_corrections(
    conn: sqlite3.Connection,
    *,
    start_utc: str,
    end_utc: str,
    as_of_utc: str,
) -> list[dict[str, Any]]:
    try:
        cursor = conn.execute(
            "SELECT observation_id,source_lane,station_id,report_type,valid_at,"
            "reported_temperature_c,raw_sha256,raw_text,canonical_committed_at "
            "FROM reported_trajectory_observations "
            "WHERE lower(source_lane)=? AND correction_suspected=1 "
            "AND julianday(valid_at)>=julianday(?) AND julianday(valid_at)<julianday(?) "
            "AND julianday(canonical_committed_at)<=julianday(?) "
            "ORDER BY station_id,valid_at,report_type,canonical_committed_at,observation_id "
            "LIMIT ?",
            (SOURCE_LANE, start_utc, end_utc, as_of_utc, MAX_CORRECTIONS + 1),
        )
    except sqlite3.Error as exc:
        raise ContractError("awc_correction_review_correction_query_failed") from exc
    rows: list[dict[str, Any]] = []
    while True:
        batch = cursor.fetchmany(FETCH_BATCH_ROWS)
        if not batch:
            break
        rows.extend(
            {
                "observation_id": row[0],
                "source_lane": row[1],
                "station_id": row[2],
                "report_type": row[3],
                "valid_at": row[4],
                "reported_temperature_c": row[5],
                "raw_sha256": row[6],
                "raw_text": row[7],
                "canonical_committed_at": row[8],
            }
            for row in batch
        )
        if len(rows) > MAX_CORRECTIONS:
            raise ContractError("awc_correction_review_correction_cap_exceeded")
        assert_process_memory_budget()
    return rows


def _same_slot_originals(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    valid_at_utc: str,
    report_type: str,
    correction_committed_at_utc: str,
) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT observation_id,report_type,reported_temperature_c,raw_sha256,"
            "canonical_committed_at "
            "FROM reported_trajectory_observations "
            "WHERE lower(source_lane)=? AND station_id=? AND valid_at=? "
            "AND report_type=? AND correction_suspected=0 "
            "AND julianday(canonical_committed_at)<=julianday(?) "
            "ORDER BY canonical_committed_at DESC,observation_id DESC LIMIT ?",
            (
                SOURCE_LANE,
                station_id,
                valid_at_utc,
                report_type,
                correction_committed_at_utc,
                MAX_SAME_SLOT_ORIGINALS + 1,
            ),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ContractError("awc_correction_review_same_slot_query_failed") from exc
    if len(rows) > MAX_SAME_SLOT_ORIGINALS:
        raise ContractError("awc_correction_review_same_slot_original_cap_exceeded")
    assert_process_memory_budget()
    return [
        {
            "observation_id": _text("original_observation_id", row[0]),
            "report_type": _text("original_report_type", row[1]).upper(),
            "reported_temperature_c": int(row[2]),
            "raw_sha256": _sha("original_raw", row[3]),
            "canonical_committed_at_utc": _stamp(_utc("original_committed_at", row[4])),
        }
        for row in rows
    ]


def _allowed_actions(*, whole_hour: bool) -> tuple[list[str], str | None, str]:
    if whole_hour:
        return (
            [QUARANTINE_STATION_DAY],
            QUARANTINE_STATION_DAY,
            "hourly_correction_clear_prohibited_without_replacement_contract",
        )
    return (
        [CLEAR_REFERENCE_ONLY, QUARANTINE_STATION_DAY],
        None,
        "manual_review_required_no_auto_approval",
    )


def _observation_row(
    conn: sqlite3.Connection,
    *,
    raw: Mapping[str, Any],
    event_date: str,
    station_timezones: Mapping[str, str],
) -> dict[str, Any] | None:
    if _text("correction_source_lane", raw["source_lane"]).lower() != SOURCE_LANE:
        raise ContractError("awc_correction_review_source_lane_invalid")
    station_id = _text("correction_station_id", raw["station_id"]).upper()
    timezone_name = station_timezones.get(station_id)
    if not timezone_name:
        raise ContractError(f"awc_correction_review_station_not_in_registry:{station_id}")
    report_type = _text("correction_report_type", raw["report_type"]).upper()
    if report_type not in {"METAR", "SPECI"}:
        raise ContractError("awc_correction_review_report_type_invalid")
    valid_at = _utc("correction_valid_at", raw["valid_at"])
    committed_at = _utc("correction_committed_at", raw["canonical_committed_at"])
    if valid_at > committed_at:
        raise ContractError("awc_correction_review_valid_after_commit")
    if valid_at.astimezone(ZoneInfo(timezone_name)).date().isoformat() != event_date:
        return None
    raw_text = _text("correction_raw_text", raw["raw_text"])
    if COR_TOKEN.search(raw_text) is None:
        raise ContractError("awc_correction_review_cor_token_missing")
    valid_at_utc = _stamp(valid_at)
    committed_at_utc = _stamp(committed_at)
    originals = _same_slot_originals(
        conn,
        station_id=station_id,
        valid_at_utc=str(raw["valid_at"]),
        report_type=report_type,
        correction_committed_at_utc=committed_at_utc,
    )
    if len(originals) > 1:
        raise ContractError("awc_correction_review_same_slot_original_ambiguous")
    original = originals[0] if originals else None
    temperature_c = int(raw["reported_temperature_c"])
    whole_hour = valid_at.minute == 0 and valid_at.second == 0 and valid_at.microsecond == 0
    allowed_actions, default_action, action_reason = _allowed_actions(whole_hour=whole_hour)
    temperature_changed = (
        temperature_c != int(original["reported_temperature_c"])
        if original is not None
        else None
    )
    identity = {
        "schema": "awc_correction_review_observation_v1",
        "source_lane": "AWC",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "slot": {
            "valid_at_utc": valid_at_utc,
            "report_type": report_type,
        },
        "original_same_slot": {
            "state": "PRESENT" if original is not None else "MISSING",
            "observation": original,
        },
        "correction": {
            "observation_id": _text("correction_observation_id", raw["observation_id"]),
            "reported_temperature_c": temperature_c,
            "raw_sha256": _sha("correction_raw", raw["raw_sha256"]),
            "canonical_committed_at_utc": committed_at_utc,
            "cor_token_verified": True,
        },
        "temperature_changed": temperature_changed,
        "temperature_change_state": (
            "CHANGED" if temperature_changed is True else "UNCHANGED" if temperature_changed is False else "ORIGINAL_MISSING"
        ),
        "whole_hour": whole_hour,
        "is_speci": report_type == "SPECI",
        "allowed_actions": allowed_actions,
        "default_action": default_action,
        "action_reason": action_reason,
        "auto_approved": False,
        "requires_human_append_only_review": True,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_awc_correction_review_packet(
    *,
    source_db: Path = DEFAULT_SOURCE_DB,
    event_date: str = DEFAULT_EVENT_DATE,
    as_of_utc: str = "2026-08-01T12:00:00Z",
    expected_station_count: int = 12,
) -> dict[str, Any]:
    """Build one immutable manual-review packet from AWC correction facts."""

    day = _event_day(event_date)
    as_of = _stamp(_utc("as_of", as_of_utc))
    if isinstance(expected_station_count, bool) or int(expected_station_count) < 1:
        raise ContractError("awc_correction_review_expected_station_count_invalid")
    source = source_db.expanduser().resolve()
    if not source.is_file():
        raise ContractError("awc_correction_review_source_database_missing")
    assert_process_memory_budget()
    try:
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error as exc:
        raise ContractError("awc_correction_review_source_database_open_failed") from exc
    try:
        conn.execute("PRAGMA query_only=ON")
        station_timezones = _station_timezones(conn, event_date=day)
        start_utc, end_utc = _bounds(day, station_timezones)
        corrections = _fetch_corrections(
            conn,
            start_utc=start_utc,
            end_utc=end_utc,
            as_of_utc=as_of,
        )
        rows = [
            row
            for raw in corrections
            if (row := _observation_row(
                conn,
                raw=raw,
                event_date=day,
                station_timezones=station_timezones,
            )) is not None
        ]
    finally:
        conn.close()
    if not rows:
        raise ContractError("awc_correction_review_no_corrections_in_scope")
    rows.sort(key=lambda row: (row["station_id"], row["slot"]["valid_at_utc"], row["slot"]["report_type"], row["correction"]["observation_id"]))
    slots = [
        (row["station_id"], row["slot"]["valid_at_utc"], row["slot"]["report_type"])
        for row in rows
    ]
    if len(set(slots)) != len(slots):
        raise ContractError("awc_correction_review_correction_slot_ambiguous")
    station_ids = sorted({str(row["station_id"]) for row in rows})
    if len(station_ids) != int(expected_station_count):
        raise ContractError(
            "awc_correction_review_expected_station_count_mismatch:"
            f"expected={int(expected_station_count)}:actual={len(station_ids)}"
        )
    stats = {
        "correction_observation_count": len(rows),
        "station_count": len(station_ids),
        "same_slot_original_present_count": sum(
            row["original_same_slot"]["state"] == "PRESENT" for row in rows
        ),
        "same_slot_original_missing_count": sum(
            row["original_same_slot"]["state"] == "MISSING" for row in rows
        ),
        "temperature_changed_count": sum(row["temperature_changed"] is True for row in rows),
        "temperature_unchanged_count": sum(row["temperature_changed"] is False for row in rows),
        "temperature_unknown_count": sum(row["temperature_changed"] is None for row in rows),
        "whole_hour_count": sum(row["whole_hour"] for row in rows),
        "speci_count": sum(row["is_speci"] for row in rows),
        "auto_approved_count": 0,
    }
    identity = {
        "schema": SCHEMA,
        "event_date": day,
        "as_of_utc": as_of,
        "source": {
            "source_database": str(source),
            "source_lane": "AWC",
            "source_read_only": True,
            "source_query_only": True,
            "query_window_utc": {"start": start_utc, "end": end_utc},
        },
        "station_ids": station_ids,
        "observations": rows,
        "stats": stats,
        "review_boundary": {
            "manual_review_required": True,
            "auto_approved": False,
            "writes_database": False,
            "calls_services": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "resource_bounds": {
            "max_stations": MAX_STATIONS,
            "max_corrections": MAX_CORRECTIONS,
            "max_same_slot_originals": MAX_SAME_SLOT_ORIGINALS,
            "fetch_batch_rows": FETCH_BATCH_ROWS,
            "max_packet_bytes": MAX_PACKET_BYTES,
        },
    }
    content_sha = content_sha256(identity)
    return {
        **identity,
        "packet_id": f"awc-correction-review-{day}-{content_sha[:16]}",
        "content_sha256": content_sha,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--event-date", default=DEFAULT_EVENT_DATE)
    parser.add_argument("--as-of-utc", default="2026-08-01T12:00:00Z")
    parser.add_argument("--expected-station-count", type=int, default=12)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        output = _resolve_output(args.output)
        packet = build_awc_correction_review_packet(
            source_db=args.source_db,
            event_date=args.event_date,
            as_of_utc=args.as_of_utc,
            expected_station_count=args.expected_station_count,
        )
        write_bounded_json(output, packet, limit=MAX_PACKET_BYTES)
    except (ContractError, ResourceBoundError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "writes_database": False,
            "calls_services": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps({
        "schema": packet["schema"],
        "state": "BUILT_READ_ONLY",
        "packet_id": packet["packet_id"],
        "content_sha256": packet["content_sha256"],
        "station_count": packet["stats"]["station_count"],
        "correction_observation_count": packet["stats"]["correction_observation_count"],
        "writes_database": False,
        "calls_services": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
