#!/usr/bin/env python3
"""Audit retained AWC source receipts before a historical P1 replay.

This command is deliberately read-only.  It does not mint a replay authority,
rewrite canonical evidence, materialize P1, or contact a provider.  Its only
job is to compare the exact formal canonical scope with the retained AWC raw
object and poll receipt that would have existed at the requested historical
decision time.

An old canonical ``source_lane=AWC`` label is not enough for strict-as-of
replay.  Every row must still resolve to the same retained raw hash, poll
identity, issuer report time, and successful receipt no later than the P1
decision.  If any link is absent or late, the output remains an explicit
blocked audit rather than a synthetic authority artifact.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.p1_replay_source_authority import (  # noqa: E402
    AUTHORITY_KEY,
    resolve_awc_official_p1_replay_authority,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_OUTPUT_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)


SCHEMA = "bayes_basket_target_p1_historical_replay_authority_audit_v1"
CANONICAL_SCHEMA = "bayes_basket_target_canonical_observation_evidence_v1"
SOURCE_TABLE = "reported_trajectory_observations"
POLL_TABLE = "reported_trajectory_source_polls"
MAX_SCOPE_OBSERVATIONS = 512
MAX_SOURCE_MATCHES_PER_OBSERVATION = 16
MAX_POLL_JSON_BYTES = 2 * 1024 * 1024
UTC = timezone.utc


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"p1_historical_authority_{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"p1_historical_authority_{name}_invalid")
    return result


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(name, value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"p1_historical_authority_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"p1_historical_authority_{name}_timezone_required")
    return parsed.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _canonical_receipt_sha256(canonical: Mapping[str, Any]) -> str:
    return _sha(
        "canonical_evidence_sha256",
        canonical.get("evidence_sha256") or canonical.get("receipt_sha256"),
    )


def _formal_scope(
    canonical: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
) -> list[dict[str, Any]]:
    if canonical.get("schema") != CANONICAL_SCHEMA:
        raise ContractError("p1_historical_authority_canonical_schema_invalid")
    if canonical.get("state") != "EXPORTED":
        raise ContractError("p1_historical_authority_canonical_state_invalid")
    observations = canonical.get("observations")
    if not isinstance(observations, list):
        raise ContractError("p1_historical_authority_canonical_observations_missing")
    station = _text("station_id", station_id).upper()
    day = _text("event_date", event_date)
    rows = [
        dict(item)
        for item in observations
        if isinstance(item, Mapping)
        and str(item.get("station_id") or "").strip().upper() == station
        and str(item.get("event_date_local") or "") == day
        and str(item.get("source_lane") or "").strip().upper() == "AWC"
        and str(item.get("report_type") or "").strip().upper() in {"METAR", "SPECI"}
        and str(item.get("evidence_role") or "").strip().upper() == "FORMAL"
        and str(item.get("qc_status") or "").strip().upper() == "ACCEPTED"
    ]
    if len(rows) > MAX_SCOPE_OBSERVATIONS:
        raise ContractError("p1_historical_authority_scope_observation_limit_exceeded")
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("valid_at_utc") or ""),
            str(row.get("observation_id") or ""),
        ),
    )


def _open_archive(path: Path) -> sqlite3.Connection:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("p1_historical_authority_archive_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    if not resolved.is_file():
        raise ContractError("p1_historical_authority_archive_missing")
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN (?, ?)",
            (SOURCE_TABLE, POLL_TABLE),
        )
    }
    if tables != {SOURCE_TABLE, POLL_TABLE}:
        conn.close()
        raise ContractError("p1_historical_authority_archive_schema_missing")
    return conn


def _parse_poll_json(raw: Any) -> Mapping[str, Any] | None:
    text = str(raw or "")
    if not text or len(text.encode("utf-8")) > MAX_POLL_JSON_BYTES:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _poll_report_time(
    poll: Mapping[str, Any] | None,
    *,
    raw_text: str,
    valid_at: datetime,
) -> str | None:
    if poll is None:
        return None
    reports = poll.get("reports")
    if not isinstance(reports, list):
        return None
    for report in reports:
        if not isinstance(report, Mapping) or str(report.get("raw_text") or "") != raw_text:
            continue
        value = str(report.get("source_report_time") or "").strip()
        if value:
            try:
                parsed = _utc("source_report_time", value)
            except ContractError:
                continue
            if parsed == valid_at:
                return _stamp(parsed)
            continue
        # Some retained AWC batch receipts omit the normalized metadata field
        # while preserving the official TAC.  Bind only an exact DDHHMMZ
        # token from that raw object to the canonical valid_at; receipt time is
        # never used to fabricate an issue time.
        token = re.search(r"(?<!\d)(\d{6})Z(?!\d)", raw_text.upper())
        if token:
            day, hour, minute = (
                int(token.group(1)[:2]),
                int(token.group(1)[2:4]),
                int(token.group(1)[4:6]),
            )
            if (
                (day, hour, minute) == (valid_at.day, valid_at.hour, valid_at.minute)
                and valid_at.second == 0
                and valid_at.microsecond == 0
            ):
                return _stamp(valid_at)
    return None


def _source_matches(
    conn: sqlite3.Connection,
    observation: Mapping[str, Any],
    *,
    decision_at: datetime,
) -> list[dict[str, Any]]:
    station = _text("observation_station_id", observation.get("station_id")).upper()
    report_type = _text("observation_report_type", observation.get("report_type")).upper()
    raw_sha = _sha("observation_raw_sha256", observation.get("raw_sha256"))
    valid_at = _utc("observation_valid_at_utc", observation.get("valid_at_utc"))
    rows = conn.execute(
        "SELECT o.observation_id,o.source_lane,o.station_id,o.report_type,o.valid_at,"
        "o.raw_sha256,o.raw_text,o.first_seen_at,o.canonical_committed_at,"
        "o.source_poll_id,o.correction_suspected,"
        "p.source_lane AS poll_source_lane,p.response_completed_at,p.response_status,"
        "p.error_code,p.poll_json "
        f"FROM {SOURCE_TABLE} o LEFT JOIN {POLL_TABLE} p ON p.poll_id=o.source_poll_id "
        "WHERE o.raw_sha256=? AND o.source_lane='awc' AND o.station_id=? "
        "AND o.report_type=? ORDER BY o.first_seen_at,o.observation_id LIMIT ?",
        (raw_sha, station, report_type, MAX_SOURCE_MATCHES_PER_OBSERVATION + 1),
    ).fetchall()
    if len(rows) > MAX_SOURCE_MATCHES_PER_OBSERVATION:
        raise ContractError("p1_historical_authority_source_match_limit_exceeded")
    matches: list[dict[str, Any]] = []
    for source in rows:
        assert_process_memory_budget()
        try:
            source_valid_at = _utc("source_valid_at", source["valid_at"])
            if source_valid_at != valid_at:
                continue
            receipt = _utc("source_receipt_at", source["response_completed_at"])
            poll = _parse_poll_json(source["poll_json"])
            poll_lane = str(source["poll_source_lane"] or "").strip().upper()
            transport = str((poll or {}).get("transport") or "").strip().lower()
            report_time = _poll_report_time(
                poll,
                raw_text=str(source["raw_text"] or ""),
                valid_at=valid_at,
            )
            blockers: list[str] = []
            if poll_lane != "AWC" or str((poll or {}).get("source_lane") or "").strip().upper() != "AWC":
                blockers.append("source_poll_not_official_awc")
            if "awc" not in transport:
                blockers.append("source_poll_transport_not_awc")
            if int(source["response_status"] or 0) != 200:
                blockers.append("source_poll_status_not_success")
            if str(source["error_code"] or "").strip():
                blockers.append("source_poll_error_present")
            if report_time is None:
                blockers.append("source_report_issue_time_unverified")
            if receipt > decision_at:
                blockers.append("source_receipt_after_decision")
            if _text("canonical_source_poll_id", observation.get("source_poll_id")) != str(source["source_poll_id"] or ""):
                blockers.append("canonical_source_poll_identity_mismatch")
            if bool(source["correction_suspected"]):
                blockers.append("source_observation_correction_unreviewed")
            matches.append({
                "source_observation_id": str(source["observation_id"]),
                "source_poll_id": str(source["source_poll_id"]),
                "raw_sha256": raw_sha,
                "valid_at_utc": _stamp(source_valid_at),
                "source_report_issued_at_utc": report_time,
                "received_at_utc": _stamp(receipt),
                "canonical_committed_at_utc": _stamp(
                    _utc("source_canonical_committed_at", source["canonical_committed_at"])
                ),
                "poll_content_sha256": content_sha256(poll) if poll is not None else None,
                "blockers": blockers,
                "strict_asof_bindable": not blockers,
            })
        except ContractError as exc:
            matches.append({
                "source_observation_id": str(source["observation_id"]),
                "source_poll_id": str(source["source_poll_id"] or ""),
                "raw_sha256": raw_sha,
                "blockers": [str(exc)],
                "strict_asof_bindable": False,
            })
    return matches


def _authority_validation(
    canonical: Mapping[str, Any],
    *,
    observations: Sequence[Mapping[str, Any]],
    station_id: str,
    event_date: str,
    decision_at_utc: str,
) -> dict[str, Any]:
    if not isinstance(canonical.get(AUTHORITY_KEY), Mapping):
        return {"state": "AUTHORITY_OBJECT_MISSING", "blockers": ["authority_object_missing"]}
    try:
        authority = resolve_awc_official_p1_replay_authority(
            canonical,
            observations=observations,
            station_id=station_id,
            event_date=event_date,
            decision_at_utc=decision_at_utc,
        )
    except ContractError as exc:
        return {"state": "AUTHORITY_OBJECT_INVALID", "blockers": [str(exc)]}
    return {
        "state": "AUTHORITY_OBJECT_VALID",
        "content_sha256": str(authority["content_sha256"]),
        "blockers": [],
    }


def build_audit(
    *,
    canonical_evidence_path: Path,
    archive_db_path: Path,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
) -> dict[str, Any]:
    """Return a content-addressed no-write authority preflight receipt."""

    canonical = load_bounded_json(canonical_evidence_path.expanduser().resolve())
    if not isinstance(canonical, Mapping):
        raise ContractError("p1_historical_authority_canonical_mapping_required")
    station = _text("station_id", station_id).upper()
    day = _text("event_date", event_date)
    decision_at = _utc("decision_at_utc", decision_at_utc)
    receipt_sha = _canonical_receipt_sha256(canonical)
    observations = _formal_scope(canonical, station_id=station, event_date=day)
    source_rows: list[dict[str, Any]] = []
    with _open_archive(archive_db_path) as conn:
        for observation in observations:
            source_rows.append({
                "observation_id": _text("observation_id", observation.get("observation_id")),
                "raw_sha256": _sha("observation_raw_sha256", observation.get("raw_sha256")),
                "canonical_source_poll_id": _text(
                    "canonical_source_poll_id", observation.get("source_poll_id")
                ),
                "matches": _source_matches(conn, observation, decision_at=decision_at),
            })

    source_blockers: set[str] = set()
    bindable_observation_count = 0
    for row in source_rows:
        matches = row["matches"]
        if not matches:
            row["state"] = "RAW_SOURCE_OBJECT_MISSING"
            row["blockers"] = ["raw_source_object_missing"]
            source_blockers.add("raw_source_object_missing")
            continue
        bindable = [item for item in matches if item.get("strict_asof_bindable") is True]
        row["state"] = (
            "RETAINED_AWC_SOURCE_BINDABLE" if bindable else "RETAINED_AWC_SOURCE_UNBINDABLE"
        )
        row["blockers"] = sorted({
            str(reason)
            for item in matches
            for reason in item.get("blockers", [])
            if str(reason)
        })
        if bindable:
            bindable_observation_count += 1
        else:
            source_blockers.update(row["blockers"])

    if not observations:
        source_blockers.add("formal_awc_scope_missing")
    authority = _authority_validation(
        canonical,
        observations=observations,
        station_id=station,
        event_date=day,
        decision_at_utc=_stamp(decision_at),
    )
    all_source_rows_bindable = bool(observations) and (
        bindable_observation_count == len(observations)
    )
    all_blockers = sorted(source_blockers | set(authority["blockers"]))
    eligible = all_source_rows_bindable and authority["state"] == "AUTHORITY_OBJECT_VALID"
    if eligible:
        state = "FORMAL_AWC_HISTORICAL_REPLAY_AUTHORITY_VALID"
    elif not all_source_rows_bindable:
        state = "BLOCKED_RETAINED_AWC_SOURCE_RECEIPT_UNBINDABLE"
    elif authority["state"] == "AUTHORITY_OBJECT_MISSING":
        state = "BLOCKED_AUTHORITY_OBJECT_MISSING"
    else:
        state = "BLOCKED_AUTHORITY_OBJECT_INVALID"

    identity = {
        "schema": SCHEMA,
        "state": state,
        "canonical_evidence": {
            "path": str(canonical_evidence_path.expanduser().resolve()),
            "receipt_sha256": receipt_sha,
        },
        "archive_database": str(archive_db_path.expanduser().resolve()),
        "station_id": station,
        "event_date": day,
        "decision_at_utc": _stamp(decision_at),
        "formal_observation_count": len(observations),
        "strict_asof_bindable_observation_count": bindable_observation_count,
        "all_retained_source_rows_bindable": all_source_rows_bindable,
        "authority_validation": authority,
        "p1_replay_authority_eligible": eligible,
        "blockers": all_blockers,
        "source_rows": source_rows,
        "boundary": {
            "archive_database_mode": "read_only_query_only",
            "writes_archive_database": False,
            "writes_target_database": False,
            "writes_canonical_evidence": False,
            "creates_replay_authority": False,
            "materializes_p1": False,
            "fetches_weather_data": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical-evidence", type=Path, required=True)
    parser.add_argument("--archive-db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--decision-at-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    audit = build_audit(
        canonical_evidence_path=args.canonical_evidence,
        archive_db_path=args.archive_db,
        station_id=args.station_id,
        event_date=args.event_date,
        decision_at_utc=args.decision_at_utc,
    )
    wrote = write_bounded_json_if_changed(
        args.output.expanduser().resolve(), audit, limit=MAX_JSON_OUTPUT_BYTES
    )
    print(json.dumps({
        "schema": audit["schema"],
        "state": audit["state"],
        "station_id": audit["station_id"],
        "event_date": audit["event_date"],
        "formal_observation_count": audit["formal_observation_count"],
        "strict_asof_bindable_observation_count": audit[
            "strict_asof_bindable_observation_count"
        ],
        "p1_replay_authority_eligible": audit["p1_replay_authority_eligible"],
        "blockers": audit["blockers"],
        "output": str(args.output.expanduser().resolve()),
        "wrote": wrote,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if audit["p1_replay_authority_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
