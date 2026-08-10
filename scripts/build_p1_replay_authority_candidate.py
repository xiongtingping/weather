#!/usr/bin/env python3
"""Build candidate-only AWC P1 replay envelopes from retained source rows.

This is a narrow, read-only repair tool for historical P1 replay.  It does
not relabel a blocked canonical receipt, rewrite ``archive.db``, or promote a
trajectory artifact.  For each station it binds the exact P0/P0-M root in the
target SSOT, then maps canonical raw hashes to the retained archive's real
``rtpoll_*`` receipt.  Synthetic historical poll pointers may be replaced in
the candidate envelope only when the raw hash, report issue time and official
AWC poll receipt all agree.  A receipt later than the root decision remains a
hard blocker.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.p1_replay_source_authority import (  # noqa: E402
    build_awc_official_p1_replay_authority,
    resolve_awc_official_p1_replay_authority,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)


SCHEMA = "bayes_basket_target_p1_replay_authority_candidate_v1"
MANIFEST_SCHEMA = "bayes_basket_target_p1_replay_authority_candidate_manifest_v1"
CANONICAL_SCHEMA = "bayes_basket_target_canonical_observation_evidence_v1"
AWC = "AWC"
MAX_OBSERVATIONS = 512
MAX_MATCHES = 16
UTC = timezone.utc


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"p1_candidate_{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"p1_candidate_{name}_invalid")
    return result


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(name, value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"p1_candidate_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"p1_candidate_{name}_timezone_required")
    return parsed.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _load_mapping(path: Path) -> dict[str, Any]:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("p1_candidate_input_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    payload = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(payload, Mapping):
        raise ContractError("p1_candidate_input_mapping_required")
    return dict(payload)


def _open_archive(path: Path) -> sqlite3.Connection:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("p1_candidate_archive_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('reported_trajectory_observations','reported_trajectory_source_polls')"
        )
    }
    if tables != {
        "reported_trajectory_observations",
        "reported_trajectory_source_polls",
    }:
        conn.close()
        raise ContractError("p1_candidate_archive_schema_missing")
    return conn


def _open_target(path: Path) -> sqlite3.Connection:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("p1_candidate_target_symlink_forbidden")
    resolved = requested.resolve(strict=True)
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _parse_json(value: Any) -> Mapping[str, Any] | None:
    text = str(value or "")
    if not text or len(text.encode("utf-8")) > 2 * 1024 * 1024:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _poll_report_time(
    poll: Mapping[str, Any] | None,
    *,
    raw_text: str,
    valid_at: datetime,
) -> datetime | None:
    if poll is None or not isinstance(poll.get("reports"), list):
        return None
    for report in poll["reports"]:
        if not isinstance(report, Mapping) or str(report.get("raw_text") or "") != raw_text:
            continue
        value = str(report.get("source_report_time") or "").strip()
        if value:
            try:
                parsed = _utc("source_report_time", value)
            except ContractError:
                continue
            if parsed == valid_at:
                return parsed
            continue
        # The retained AWC batch keeps the authoritative TAC in raw_text,
        # including the DDHHMMZ issue/validity token, but older poll receipts
        # left source_report_time empty.  Recover only that explicit token and
        # require an exact match to the already-bound valid_at; never infer a
        # date or time from receipt metadata.
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
                return valid_at
    return None


def _root_row(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
) -> dict[str, Any]:
    decision = _utc("root_decision_at_utc", decision_at_utc)
    rows = conn.execute(
        "SELECT stage,checkpoint_id,station_id,event_date,settlement_timezone,"
        "decision_at_utc,fresh_until_utc,lineage_json,content_sha256,state "
        "FROM basket_probability_envelope WHERE station_id=? AND event_date=? "
        "AND stage IN ('P0','P0-M') AND state='COMMITTED'",
        (station_id, event_date),
    ).fetchall()
    selected = [
        row
        for row in rows
        if _utc("root_row_decision_at_utc", row["decision_at_utc"]) == decision
    ]
    if len(selected) != 1:
        raise ContractError(
            "p1_candidate_root_not_unique:" + station_id + ":" + str(len(selected))
        )
    row = selected[0]
    lineage = _parse_json(row["lineage_json"])
    if lineage is None:
        raise ContractError("p1_candidate_root_lineage_invalid")
    return {
        "stage": str(row["stage"]),
        "checkpoint_id": _text("root_checkpoint_id", row["checkpoint_id"]),
        "station_id": _text("root_station_id", row["station_id"]).upper(),
        "event_date": _text("root_event_date", row["event_date"]),
        "settlement_timezone": _text(
            "root_settlement_timezone", row["settlement_timezone"]
        ),
        "decision_at_utc": _stamp(decision),
        "fresh_until_utc": _stamp(_utc("root_fresh_until_utc", row["fresh_until_utc"])),
        "lineage": dict(lineage),
        "content_sha256": _sha("root_content_sha256", row["content_sha256"]),
        "state": str(row["state"]),
    }


def _root_binding(root: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "root_stage": str(root["stage"]),
        "root_checkpoint_id": str(root["checkpoint_id"]),
        "root_content_sha256": str(root["content_sha256"]),
        "root_decision_at_utc": str(root["decision_at_utc"]),
        "root_fresh_until_utc": str(root["fresh_until_utc"]),
        "root_lineage_sha256": content_sha256(root["lineage"]),
    }


def _canonical_rows(
    canonical: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if canonical.get("schema") != CANONICAL_SCHEMA:
        raise ContractError("p1_candidate_canonical_schema_invalid")
    observations = canonical.get("observations")
    if not isinstance(observations, list):
        raise ContractError("p1_candidate_canonical_observations_missing")
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for raw in observations:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        if str(row.get("station_id") or "").strip().upper() != station_id:
            continue
        if str(row.get("event_date_local") or "") != event_date:
            continue
        if str(row.get("source_lane") or "").strip().upper() != AWC:
            excluded.append({"observation_id": row.get("observation_id"), "reason": "source_lane_not_awc"})
            continue
        if str(row.get("evidence_role") or "").strip().upper() != "FORMAL":
            excluded.append({"observation_id": row.get("observation_id"), "reason": "evidence_role_not_formal"})
            continue
        if str(row.get("qc_status") or "").strip().upper() != "ACCEPTED":
            excluded.append({"observation_id": row.get("observation_id"), "reason": "qc_not_accepted"})
            continue
        report_type = str(row.get("report_type") or "").strip().upper()
        try:
            valid_at = _utc("canonical_valid_at_utc", row.get("valid_at_utc"))
            raw_sha = _sha("canonical_raw_sha256", row.get("raw_sha256"))
            observation_id = _text("canonical_observation_id", row.get("observation_id"))
        except ContractError as exc:
            excluded.append({"observation_id": row.get("observation_id"), "reason": str(exc)})
            continue
        if report_type not in {"METAR", "SPECI"}:
            excluded.append({"observation_id": observation_id, "reason": "report_type_invalid"})
            continue
        # Routine METARs are formal at the provider's own official minute
        # (:00, :20/:50, :51-:58 ...); replay must admit the same set the
        # live P1 reader and settlement admit.
        row["source_lane"] = AWC
        row["report_type"] = report_type
        row["raw_sha256"] = raw_sha
        row["observation_id"] = observation_id
        row["valid_at_utc"] = _stamp(valid_at)
        selected.append(row)
        # The input may be a retained multi-station export.  Apply the
        # resource bound after narrowing to the requested station/event day;
        # the bounded JSON loader and process-memory guard still cap the
        # complete input object.
        if len(selected) > MAX_OBSERVATIONS:
            raise ContractError("p1_candidate_observation_limit_exceeded")
    selected.sort(key=lambda item: (str(item.get("valid_at_utc")), str(item.get("observation_id"))))
    return selected, excluded


def _archive_matches(
    conn: sqlite3.Connection,
    observation: Mapping[str, Any],
    *,
    decision_at: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    station = _text("observation_station_id", observation.get("station_id")).upper()
    report_type = _text("observation_report_type", observation.get("report_type")).upper()
    raw_sha = _sha("observation_raw_sha256", observation.get("raw_sha256"))
    valid_at = _utc("observation_valid_at_utc", observation.get("valid_at_utc"))
    rows = conn.execute(
        "SELECT o.observation_id,o.source_lane,o.station_id,o.report_type,o.valid_at,"
        "o.raw_sha256,o.raw_text,o.first_seen_at,o.canonical_committed_at,"
        "o.source_poll_id,o.correction_suspected,p.source_lane AS poll_source_lane,"
        "p.response_completed_at,p.response_status,p.error_code,p.poll_json "
        "FROM reported_trajectory_observations o LEFT JOIN "
        "reported_trajectory_source_polls p ON p.poll_id=o.source_poll_id "
        "WHERE o.raw_sha256=? AND o.source_lane='awc' AND o.station_id=? "
        "AND o.report_type=? ORDER BY o.first_seen_at,o.observation_id LIMIT ?",
        (raw_sha, station, report_type, MAX_MATCHES + 1),
    ).fetchall()
    if len(rows) > MAX_MATCHES:
        raise ContractError("p1_candidate_source_match_limit_exceeded")
    matches: list[dict[str, Any]] = []
    for source in rows:
        assert_process_memory_budget()
        blockers: list[str] = []
        try:
            source_valid_at = _utc("source_valid_at", source["valid_at"])
            if source_valid_at != valid_at:
                continue
            receipt = _utc("source_receipt_at", source["response_completed_at"])
            poll = _parse_json(source["poll_json"])
            poll_lane = str(source["poll_source_lane"] or "").strip().upper()
            declared_lane = str((poll or {}).get("source_lane") or "").strip().upper()
            transport = str((poll or {}).get("transport") or "").strip().lower()
            published = _poll_report_time(
                poll, raw_text=str(source["raw_text"] or ""), valid_at=valid_at
            )
            if poll_lane != AWC or declared_lane != AWC:
                blockers.append("source_poll_not_official_awc")
            if "awc" not in transport:
                blockers.append("source_poll_transport_not_awc")
            if int(source["response_status"] or 0) != 200:
                blockers.append("source_poll_status_not_success")
            if str(source["error_code"] or "").strip():
                blockers.append("source_poll_error_present")
            if published is None:
                blockers.append("source_report_issue_time_unverified")
            if published is not None and published > receipt:
                blockers.append("source_publication_after_receipt")
            if receipt > decision_at:
                blockers.append("source_receipt_after_root_decision")
            if bool(source["correction_suspected"]):
                blockers.append("source_observation_correction_unreviewed")
            matches.append({
                "source_observation_id": str(source["observation_id"]),
                "source_poll_id": str(source["source_poll_id"] or ""),
                "raw_sha256": raw_sha,
                "valid_at_utc": _stamp(source_valid_at),
                "source_report_issued_at_utc": None if published is None else _stamp(published),
                "received_at_utc": _stamp(receipt),
                "canonical_committed_at_utc": _stamp(_utc(
                    "source_canonical_committed_at", source["canonical_committed_at"]
                )),
                "poll_content_sha256": None if poll is None else content_sha256(poll),
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
    return matches, sorted({str(reason) for item in matches for reason in item.get("blockers", [])})


def _build_station_candidate(
    *,
    canonical: Mapping[str, Any],
    canonical_path: Path,
    archive_conn: sqlite3.Connection,
    target_conn: sqlite3.Connection,
    archive_path: Path,
    target_path: Path,
    station_id: str,
    event_date: str,
    root_decision_at_utc: str,
) -> dict[str, Any]:
    station = _text("station_id", station_id).upper()
    day = _text("event_date", event_date)
    root = _root_row(
        target_conn,
        station_id=station,
        event_date=day,
        decision_at_utc=root_decision_at_utc,
    )
    if root["station_id"] != station or root["event_date"] != day:
        raise ContractError("p1_candidate_root_scope_mismatch")
    decision = _utc("root_decision_at_utc", root["decision_at_utc"])
    rows, excluded = _canonical_rows(canonical, station_id=station, event_date=day)
    accepted: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    pointer_repairs: list[dict[str, Any]] = []
    blocked_rows: list[dict[str, Any]] = []
    source_match_rows: list[dict[str, Any]] = []
    for row in rows:
        matches, match_blockers = _archive_matches(
            archive_conn, row, decision_at=decision
        )
        strict = [item for item in matches if item.get("strict_asof_bindable") is True]
        source_match_rows.append({
            "observation_id": row["observation_id"],
            "canonical_source_poll_id": str(row.get("source_poll_id") or ""),
            "matches": matches,
        })
        if not strict:
            if any(_utc("canonical_valid_at_utc", row["valid_at_utc"]) > decision for _ in [0]):
                excluded.append({"observation_id": row["observation_id"], "reason": "observation_valid_after_root_decision"})
            else:
                blocked_rows.append({
                    "observation_id": row["observation_id"],
                    "raw_sha256": row["raw_sha256"],
                    "blockers": match_blockers or ["retained_source_unbindable"],
                    "matches": matches,
                })
            continue
        match = strict[0]
        actual_poll = str(match["source_poll_id"])
        canonical_poll = str(row.get("source_poll_id") or "")
        if canonical_poll != actual_poll:
            pointer_repairs.append({
                "observation_id": row["observation_id"],
                "canonical_source_poll_id": canonical_poll,
                "archive_source_poll_id": actual_poll,
                "repair_reason": "canonical_pointer_replaced_by_archive_receipt_identity",
            })
        candidate_row = dict(row)
        candidate_row["source_poll_id"] = actual_poll
        candidate_row["source_lane"] = AWC
        accepted.append(candidate_row)
        bindings.append({
            "observation_id": row["observation_id"],
            "station_id": station,
            "event_date_local": day,
            "source_lane": AWC,
            "report_type": row["report_type"],
            "raw_sha256": row["raw_sha256"],
            "source_poll_id": actual_poll,
            "published_at_utc": match["source_report_issued_at_utc"],
            "received_at_utc": match["received_at_utc"],
            "poll_content_sha256": match.get("poll_content_sha256"),
            "correction_status": "NONE",
            "manual_review_status": "NOT_REQUIRED",
            "manual_review_receipt_sha256": None,
        })
    # Every retained formal source row that was available by the root must be
    # represented.  Late rows are intentionally excluded; they are not OOS.
    blockers = sorted({
        reason
        for item in blocked_rows
        for reason in item.get("blockers", [])
        if reason
    })
    blockers.extend(
        "formal_source_row_unbindable:" + str(item["observation_id"])
        for item in blocked_rows
    )
    if not accepted:
        blockers.append("no_strict_asof_observation_bindings")
    if len(accepted) > MAX_OBSERVATIONS:
        raise ContractError("p1_candidate_accepted_observation_limit_exceeded")
    accepted.sort(key=lambda item: (str(item["valid_at_utc"]), str(item["observation_id"])))
    bindings.sort(key=lambda item: (str(item["published_at_utc"]), str(item["observation_id"])))
    root_binding = _root_binding(root)
    input_receipt = _sha(
        "canonical_input_receipt_sha256",
        canonical.get("receipt_sha256") or canonical.get("evidence_sha256"),
    )
    source_snapshot_identity = {
        "archive_database": str(archive_path.expanduser().resolve()),
        "station_id": station,
        "event_date": day,
        "root_binding": root_binding,
        "archive_bindings": [
            {
                "observation_id": item["observation_id"],
                "raw_sha256": item["raw_sha256"],
                "source_poll_id": item["source_poll_id"],
                "received_at_utc": item["received_at_utc"],
                "poll_content_sha256": item.get("poll_content_sha256"),
            }
            for item in bindings
        ],
    }
    source_snapshot_sha = content_sha256(source_snapshot_identity)
    evidence_identity = {
        "schema": CANONICAL_SCHEMA,
        "candidate_only": True,
        "source_input_receipt_sha256": input_receipt,
        "station_id": station,
        "event_date": day,
        "root_binding": root_binding,
        "observation_ids": [item["observation_id"] for item in accepted],
        "raw_sha256": [item["raw_sha256"] for item in accepted],
        "archive_source_poll_ids": [item["source_poll_id"] for item in bindings],
    }
    candidate_evidence_sha = content_sha256(evidence_identity)
    candidate_body: dict[str, Any] = {
        "schema": CANONICAL_SCHEMA,
        "state": "BLOCKED_FAIL_CLOSED" if blockers else "EXPORTED",
        "candidate_only": True,
        "evidence_sha256": candidate_evidence_sha,
        "receipt_sha256": None,
        "observations": accepted,
        "truth_rows": [],
        "blockers": sorted(set(blockers)),
        "source_input": {
            "path": str(canonical_path.expanduser().resolve()),
            "receipt_sha256": input_receipt,
            "source_state": str(canonical.get("state") or ""),
        },
        "p1_root_binding": root_binding,
        "source_snapshot_sha256": source_snapshot_sha,
        "excluded_rows": excluded,
        "blocked_rows": blocked_rows,
        "source_match_rows": source_match_rows,
        "pointer_repairs": pointer_repairs,
        "boundary": {
            "candidate_only": True,
            "calls_network": False,
            "fetches_weather_data": False,
            "writes_archive_database": False,
            "writes_target_database": False,
            "formal_promoted_created": False,
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "settlement_truth_final_frozen": False,
        },
    }
    if not blockers:
        authority = build_awc_official_p1_replay_authority(
            authority_id=(
                "awc-p1-replay-" + station.lower() + "-" + day.replace("-", "")
                + "-" + str(root["checkpoint_id"])[-12:]
            ),
            station_id=station,
            event_date=day,
            canonical_evidence_sha256=candidate_evidence_sha,
            source_snapshot_sha256=source_snapshot_sha,
            observations=bindings,
        )
        candidate_body["p1_replay_source_authority"] = authority
        try:
            resolved = resolve_awc_official_p1_replay_authority(
                candidate_body,
                observations=accepted,
                station_id=station,
                event_date=day,
                decision_at_utc=root["decision_at_utc"],
            )
        except ContractError as exc:
            candidate_body.pop("p1_replay_source_authority", None)
            candidate_body["state"] = "BLOCKED_FAIL_CLOSED"
            candidate_body["blockers"] = sorted(
                set(candidate_body["blockers"]) | {"authority_validation:" + str(exc)}
            )
        else:
            candidate_body["authority_validation"] = {
                "state": "AUTHORITY_OBJECT_VALID",
                "content_sha256": resolved["content_sha256"],
            }
    candidate_body["formal_observation_count"] = len(accepted)
    candidate_body["strict_asof_bindable_observation_count"] = len(bindings)
    candidate_body["root_decision_at_utc"] = root["decision_at_utc"]
    identity = {key: value for key, value in candidate_body.items() if key != "receipt_sha256"}
    candidate_body["receipt_sha256"] = content_sha256(identity)
    return candidate_body


def build_candidates(
    *,
    canonical_path: Path,
    archive_path: Path,
    target_path: Path,
    station_ids: Sequence[str],
    event_date: str,
    root_decision_at_utc: str,
    output_dir: Path,
) -> dict[str, Any]:
    canonical = _load_mapping(canonical_path)
    stations = tuple(sorted({str(item).strip().upper() for item in station_ids if str(item).strip()}))
    if not stations:
        raise ContractError("p1_candidate_station_scope_missing")
    output_root = output_dir.expanduser().resolve()
    if output_root.is_symlink():
        raise ContractError("p1_candidate_output_symlink_forbidden")
    output_root.mkdir(parents=True, exist_ok=True)
    archive_conn = _open_archive(archive_path)
    target_conn = _open_target(target_path)
    reports: list[dict[str, Any]] = []
    try:
        for station in stations:
            assert_process_memory_budget()
            candidate = _build_station_candidate(
                canonical=canonical,
                canonical_path=canonical_path,
                archive_conn=archive_conn,
                target_conn=target_conn,
                archive_path=archive_path,
                target_path=target_path,
                station_id=station,
                event_date=event_date,
                root_decision_at_utc=root_decision_at_utc,
            )
            destination = output_root / f"{station}_{event_date}_p1_replay_authority_candidate.json"
            write_bounded_json_if_changed(destination, candidate, limit=MAX_JSON_OUTPUT_BYTES)
            reports.append({
                "station_id": station,
                "event_date": event_date,
                "state": candidate["state"],
                "formal_observation_count": candidate["formal_observation_count"],
                "strict_asof_bindable_observation_count": candidate[
                    "strict_asof_bindable_observation_count"
                ],
                "pointer_repair_count": len(candidate.get("pointer_repairs") or []),
                "blockers": candidate["blockers"],
                "output": str(destination),
                "content_sha256": candidate["receipt_sha256"],
            })
    finally:
        archive_conn.close()
        target_conn.close()
    identity = {
        "schema": MANIFEST_SCHEMA,
        "state": "CANDIDATE_ONLY",
        "canonical_input": str(canonical_path.expanduser().resolve()),
        "archive_database": str(archive_path.expanduser().resolve()),
        "target_database": str(target_path.expanduser().resolve()),
        "event_date": event_date,
        "root_decision_at_utc": _stamp(_utc("root_decision_at_utc", root_decision_at_utc)),
        "station_count": len(stations),
        "reports": reports,
        "summary": {
            "candidate_ready_count": sum(item["state"] == "EXPORTED" for item in reports),
            "blocked_count": sum(item["state"] != "EXPORTED" for item in reports),
            "pointer_repairs": sum(int(item["pointer_repair_count"]) for item in reports),
            "formal_observations": sum(int(item["formal_observation_count"]) for item in reports),
            "strict_asof_bindings": sum(
                int(item["strict_asof_bindable_observation_count"]) for item in reports
            ),
        },
        "boundary": {
            "candidate_only": True,
            "calls_network": False,
            "writes_archive_database": False,
            "writes_target_database": False,
            "formal_promoted_created": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical-evidence", type=Path, required=True)
    parser.add_argument("--archive-db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument(
        "--target-db",
        type=Path,
        default=ROOT / "data" / "bayes_basket_target_ssot.db",
    )
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--root-decision-at-utc", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_candidates(
            canonical_path=args.canonical_evidence,
            archive_path=args.archive_db,
            target_path=args.target_db,
            station_ids=args.station_id,
            event_date=args.event_date,
            root_decision_at_utc=args.root_decision_at_utc,
            output_dir=args.output_dir,
        )
        write_bounded_json_if_changed(
            args.manifest.expanduser().resolve(), manifest, limit=MAX_JSON_OUTPUT_BYTES
        )
    except Exception as exc:
        print(json.dumps({"state": "BLOCKED", "error": f"{type(exc).__name__}:{exc}"}, ensure_ascii=False))
        return 2
    print(json.dumps({
        "schema": manifest["schema"],
        "state": manifest["state"],
        "station_count": manifest["station_count"],
        "summary": manifest["summary"],
        "manifest": str(args.manifest.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if manifest["summary"]["candidate_ready_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
