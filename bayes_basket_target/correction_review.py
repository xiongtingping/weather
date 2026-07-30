"""Append-only manual resolution of official correction station-day halts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Mapping

from .contracts import ContractError, canonical_json, content_sha256
from .resource_bounds import assert_process_memory_budget
from .settlement_time import settlement_day_window


SCHEMA = "reported_trajectory_station_halt_review_v1"
CLEAR_REFERENCE_ONLY = "CLEAR_REFERENCE_ONLY"
QUARANTINE_STATION_DAY = "QUARANTINE_STATION_DAY"
DECISIONS = frozenset({CLEAR_REFERENCE_ONLY, QUARANTINE_STATION_DAY})
MAX_CORRECTIONS_PER_SCOPE = 16
MAX_REVIEWS_PER_SCOPE = 64
MAX_EVIDENCE_JSON_BYTES = 65_536


def _instant(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if (
        len(result) != 64
        or any(char not in "0123456789abcdef" for char in result)
    ):
        raise ContractError(f"{name}_invalid")
    return result


def _review_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "source_lane": _text("correction_review_source_lane", row["source_lane"]).lower(),
        "station_id": _text("correction_review_station_id", row["station_id"]).upper(),
        "event_date": _text("correction_review_event_date", row["event_date"]),
        "triggering_observation_id": _text(
            "correction_review_triggering_observation_id",
            row["triggering_observation_id"],
        ),
        "decision": _text("correction_review_decision", row["decision"]).upper(),
        "reviewer": _text("correction_review_reviewer", row["reviewer"]),
        "rationale": _text("correction_review_rationale", row["rationale"]),
        "reviewed_at": _stamp(
            _instant("correction_review_reviewed_at", row["reviewed_at"])
        ),
        "supersedes_review_id": (
            _text(
                "correction_review_supersedes_review_id",
                row["supersedes_review_id"],
            )
            if row.get("supersedes_review_id") is not None
            else None
        ),
        "evidence_json": _text(
            "correction_review_evidence_json", row["evidence_json"]
        ),
        "evidence_sha256": _sha(
            "correction_review_evidence_sha256", row["evidence_sha256"]
        ),
    }


def _validate_review_row(row: Mapping[str, Any]) -> dict[str, Any]:
    identity = _review_identity(row)
    if identity["source_lane"] not in {"amsc", "awc"}:
        raise ContractError("correction_review_source_lane_invalid")
    if identity["decision"] not in DECISIONS:
        raise ContractError("correction_review_decision_invalid")
    try:
        evidence = json.loads(identity["evidence_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("correction_review_evidence_json_invalid") from exc
    if not isinstance(evidence, Mapping):
        raise ContractError("correction_review_evidence_mapping_required")
    canonical_evidence = canonical_json(dict(evidence))
    if canonical_evidence != identity["evidence_json"]:
        raise ContractError("correction_review_evidence_not_canonical")
    if content_sha256(dict(evidence)) != identity["evidence_sha256"]:
        raise ContractError("correction_review_evidence_sha_mismatch")
    expected_sha = content_sha256(identity)
    if _sha("correction_review_content_sha256", row["content_sha256"]) != expected_sha:
        raise ContractError("correction_review_content_sha_mismatch")
    expected_id = "rthreview_" + expected_sha[:40]
    if _text("correction_review_id", row["review_id"]) != expected_id:
        raise ContractError("correction_review_id_mismatch")
    return {**identity, "review_id": expected_id, "content_sha256": expected_sha}


def append_correction_review(
    conn: sqlite3.Connection,
    *,
    source_lane: str,
    station_id: str,
    event_date: str,
    triggering_observation_id: str,
    decision: str,
    reviewer: str,
    rationale: str,
    reviewed_at: str,
) -> dict[str, Any]:
    """Append an exact review of one correction observation."""

    lane = _text("correction_review_source_lane", source_lane).lower()
    station = _text("correction_review_station_id", station_id).upper()
    day = _text("correction_review_event_date", event_date)
    observation_id = _text(
        "correction_review_triggering_observation_id",
        triggering_observation_id,
    )
    resolved_decision = _text(
        "correction_review_decision", decision
    ).upper()
    if lane not in {"amsc", "awc"} or resolved_decision not in DECISIONS:
        raise ContractError("correction_review_scope_or_decision_invalid")
    observation = conn.execute(
        "SELECT source_lane,station_id,valid_at,raw_sha256,"
        "canonical_committed_at,correction_suspected "
        "FROM reported_trajectory_observations WHERE observation_id=?",
        (observation_id,),
    ).fetchone()
    if observation is None:
        raise ContractError("correction_review_observation_missing")
    if (
        str(observation[0]).lower() != lane
        or str(observation[1]).upper() != station
        or int(observation[5] or 0) != 1
    ):
        raise ContractError("correction_review_observation_scope_invalid")
    valid_at = _instant("correction_review_observation_valid_at", observation[2])
    registry_rows = conn.execute(
        "SELECT DISTINCT settlement_timezone,source_lane "
        "FROM reported_trajectory_market_registry "
        "WHERE station_id=? AND event_date=? AND source_eligible=1",
        (station, day),
    ).fetchall()
    registry_scopes = {
        (
            str(row[0] or "").strip(),
            str(row[1] or "").strip().lower(),
        )
        for row in registry_rows
    }
    if len(registry_scopes) != 1:
        raise ContractError("correction_review_registry_scope_not_unique")
    timezone_name, registry_lane = next(iter(registry_scopes))
    if registry_lane != lane:
        raise ContractError("correction_review_registry_source_lane_mismatch")
    window = settlement_day_window(day, timezone_name)
    window_start = _instant(
        "correction_review_window_start",
        window["target_day_start_utc"],
    )
    window_end = _instant(
        "correction_review_window_end",
        window["target_day_end_utc"],
    )
    if not window_start <= valid_at < window_end:
        raise ContractError("correction_review_observation_outside_event_day")
    committed_at = _instant(
        "correction_review_observation_committed_at", observation[4]
    )
    reviewed = _instant("correction_review_reviewed_at", reviewed_at)
    if reviewed < committed_at:
        raise ContractError("correction_review_precedes_observation")
    if (
        resolved_decision == CLEAR_REFERENCE_ONLY
        and valid_at.minute == 0
        and valid_at.second == 0
    ):
        raise ContractError(
            "correction_review_hourly_clear_requires_replacement_contract"
        )
    latest = conn.execute(
        "SELECT review_id,decision,reviewer,rationale,reviewed_at,"
        "evidence_sha256 "
        "FROM reported_trajectory_station_halt_reviews "
        "WHERE source_lane=? AND station_id=? AND event_date=? "
        "AND triggering_observation_id=? "
        "ORDER BY reviewed_at DESC,review_id DESC LIMIT 1",
        (lane, station, day, observation_id),
    ).fetchone()
    supersedes = str(latest[0]) if latest is not None else None
    evidence = {
        "schema": "reported_trajectory_correction_review_evidence_v1",
        "source_lane": lane,
        "station_id": station,
        "event_date": day,
        "triggering_observation_id": observation_id,
        "observation_valid_at_utc": _stamp(valid_at),
        "observation_canonical_committed_at_utc": _stamp(committed_at),
        "observation_raw_sha256": _sha(
            "correction_review_observation_raw_sha256", observation[3]
        ),
        "decision": resolved_decision,
        "reviewer": _text("correction_review_reviewer", reviewer),
        "rationale": _text("correction_review_rationale", rationale),
    }
    evidence_json = canonical_json(evidence)
    if len(evidence_json.encode("utf-8")) > MAX_EVIDENCE_JSON_BYTES:
        raise ContractError("correction_review_evidence_too_large")
    evidence_sha = content_sha256(evidence)
    reviewed_stamp = _stamp(reviewed)
    if (
        latest is not None
        and str(latest[1]) == resolved_decision
        and str(latest[2]) == evidence["reviewer"]
        and str(latest[3]) == evidence["rationale"]
        and str(latest[4]) == reviewed_stamp
        and str(latest[5]) == evidence_sha
    ):
        return {
            "schema": SCHEMA,
            "state": "idempotent",
            "review_id": str(latest[0]),
            "content_sha256": str(
                conn.execute(
                    "SELECT content_sha256 "
                    "FROM reported_trajectory_station_halt_reviews "
                    "WHERE review_id=?",
                    (str(latest[0]),),
                ).fetchone()[0]
            ),
            "wrote": False,
        }
    identity = {
        "schema": SCHEMA,
        "source_lane": lane,
        "station_id": station,
        "event_date": day,
        "triggering_observation_id": observation_id,
        "decision": resolved_decision,
        "reviewer": evidence["reviewer"],
        "rationale": evidence["rationale"],
        "reviewed_at": reviewed_stamp,
        "supersedes_review_id": supersedes,
        "evidence_json": evidence_json,
        "evidence_sha256": evidence_sha,
    }
    content_sha = content_sha256(identity)
    review_id = "rthreview_" + content_sha[:40]
    existing = conn.execute(
        "SELECT review_id FROM reported_trajectory_station_halt_reviews "
        "WHERE content_sha256=?",
        (content_sha,),
    ).fetchone()
    if existing is not None:
        return {
            "schema": SCHEMA,
            "state": "idempotent",
            "review_id": str(existing[0]),
            "content_sha256": content_sha,
            "wrote": False,
        }
    conn.execute(
        "INSERT INTO reported_trajectory_station_halt_reviews("
        "review_id,source_lane,station_id,event_date,"
        "triggering_observation_id,decision,reviewer,rationale,reviewed_at,"
        "supersedes_review_id,evidence_json,evidence_sha256,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            review_id, lane, station, day, observation_id,
            resolved_decision, evidence["reviewer"], evidence["rationale"],
            identity["reviewed_at"], supersedes, evidence_json,
            identity["evidence_sha256"], content_sha,
        ),
    )
    assert_process_memory_budget()
    return {
        "schema": SCHEMA,
        "state": "committed",
        "review_id": review_id,
        "content_sha256": content_sha,
        "wrote": True,
    }


def assert_station_day_corrections_resolved(
    conn: sqlite3.Connection,
    *,
    source_lane: str,
    station_id: str,
    event_date: str,
    window_start_utc: str,
    window_end_utc: str,
    as_of_utc: str,
) -> dict[str, Any]:
    """Require one latest clear receipt for every correction in scope."""

    lane = _text("correction_review_source_lane", source_lane).lower()
    station = _text("correction_review_station_id", station_id).upper()
    day = _text("correction_review_event_date", event_date)
    as_of = _stamp(_instant("correction_review_as_of_utc", as_of_utc))
    corrections = conn.execute(
        "SELECT observation_id,valid_at FROM reported_trajectory_observations "
        "WHERE source_lane=? AND station_id=? AND correction_suspected=1 "
        "AND julianday(valid_at)>=julianday(?) "
        "AND julianday(valid_at)<julianday(?) "
        "AND julianday(canonical_committed_at)<=julianday(?) "
        "ORDER BY valid_at,canonical_committed_at,observation_id LIMIT ?",
        (
            lane, station, window_start_utc, window_end_utc, as_of,
            MAX_CORRECTIONS_PER_SCOPE + 1,
        ),
    ).fetchall()
    if len(corrections) > MAX_CORRECTIONS_PER_SCOPE:
        raise ContractError("correction_review_scope_cap_exceeded")
    if not corrections:
        return {
            "schema": SCHEMA,
            "state": "NO_CORRECTIONS",
            "correction_count": 0,
            "review_ids": [],
        }
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='reported_trajectory_station_halt_reviews'"
    ).fetchone()
    if table is None:
        raise ContractError("correction_review_schema_missing")
    review_ids: list[str] = []
    for observation_id, valid_at_raw in corrections:
        rows = conn.execute(
            "SELECT review_id,source_lane,station_id,event_date,"
            "triggering_observation_id,decision,reviewer,rationale,"
            "reviewed_at,supersedes_review_id,evidence_json,evidence_sha256,"
            "content_sha256 "
            "FROM reported_trajectory_station_halt_reviews "
            "WHERE source_lane=? AND station_id=? AND event_date=? "
            "AND triggering_observation_id=? "
            "AND julianday(reviewed_at)<=julianday(?) "
            "ORDER BY reviewed_at DESC,review_id DESC LIMIT ?",
            (
                lane, station, day, str(observation_id), as_of,
                MAX_REVIEWS_PER_SCOPE + 1,
            ),
        ).fetchall()
        if len(rows) > MAX_REVIEWS_PER_SCOPE:
            raise ContractError("correction_review_revision_cap_exceeded")
        if not rows:
            raise ContractError(
                "correction_review_required:"
                f"{str(observation_id)}"
            )
        keys = (
            "review_id", "source_lane", "station_id", "event_date",
            "triggering_observation_id", "decision", "reviewer",
            "rationale", "reviewed_at", "supersedes_review_id",
            "evidence_json", "evidence_sha256", "content_sha256",
        )
        review = _validate_review_row(dict(zip(keys, rows[0])))
        valid_at = _instant(
            "correction_review_observation_valid_at", valid_at_raw
        )
        if review["decision"] == QUARANTINE_STATION_DAY:
            raise ContractError(
                "correction_review_quarantines_station_day:"
                f"{str(observation_id)}"
            )
        if valid_at.minute == 0 and valid_at.second == 0:
            raise ContractError(
                "correction_review_hourly_clear_requires_replacement_contract"
            )
        review_ids.append(review["review_id"])
        assert_process_memory_budget()
    return {
        "schema": SCHEMA,
        "state": "CLEARED_REFERENCE_ONLY",
        "correction_count": len(corrections),
        "review_ids": review_ids,
    }


__all__ = [
    "CLEAR_REFERENCE_ONLY",
    "DECISIONS",
    "MAX_CORRECTIONS_PER_SCOPE",
    "QUARANTINE_STATION_DAY",
    "SCHEMA",
    "append_correction_review",
    "assert_station_day_corrections_resolved",
]
