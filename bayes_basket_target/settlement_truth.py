"""Resident finalization of official station-day settlement truth.

The market registry is the sole timezone authority.  This module reads the
already-canonical AMSC/AWC observation ledger, freezes a bounded daily maximum
only after the registry settlement window closes, and appends one
content-addressed truth revision to the target SSOT.  It never reads NWP,
probability, TAF, prices, orders, or a gateway.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import ContractError, canonical_json, content_sha256
from .correction_review import assert_station_day_corrections_resolved
from .resource_bounds import assert_process_memory_budget
from .settlement_time import settlement_day_window


SCHEMA = "bayes_basket_target_resident_settlement_truth_v1"
AUTHORITY_SCOPE = "FORMAL_NATIVE_ELIGIBLE"
SOURCE_ROLE = "CANONICAL_AMSC_AWC_INGRESS"
MAX_TRUTH_SCOPES = 45
MAX_OBSERVATIONS_PER_SCOPE = 64


def _utc(name: str, value: Any) -> datetime:
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


def _registry_scope(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> tuple[str, str, str]:
    rows = conn.execute(
        "SELECT snapshot_sha256,settlement_timezone,source_lane,refreshed_at "
        "FROM basket_target_market_registry "
        "WHERE station_id=? AND event_date=? AND source_eligible=1 "
        "AND julianday(refreshed_at)<=julianday(?) "
        "ORDER BY refreshed_at DESC,snapshot_sha256 DESC",
        (station_id, event_date, as_of_utc),
    ).fetchall()
    if not rows:
        raise ContractError("settlement_truth_registry_scope_not_unique")
    registry_snapshot_sha = str(rows[0][0] or "").strip().lower()
    latest_rows = [
        row for row in rows
        if str(row[0] or "").strip().lower() == registry_snapshot_sha
    ]
    scopes = {
        (
            str(row[1] or "").strip(),
            str(row[2] or "").strip().upper(),
        )
        for row in latest_rows
    }
    if len(scopes) != 1:
        raise ContractError("settlement_truth_registry_scope_not_unique")
    if (
        len(registry_snapshot_sha) != 64
        or any(
            char not in "0123456789abcdef"
            for char in registry_snapshot_sha
        )
    ):
        raise ContractError("settlement_truth_registry_snapshot_sha_invalid")
    timezone_name, source_lane = next(iter(scopes))
    if not timezone_name:
        raise ContractError("settlement_truth_registry_timezone_missing")
    if source_lane not in {"AMSC", "AWC"}:
        raise ContractError("settlement_truth_source_lane_invalid")
    settlement_day_window(event_date, timezone_name)
    return timezone_name, source_lane, registry_snapshot_sha


def materialize_settled_station_truth(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> dict[str, Any]:
    """Append one exact station-day truth revision, or deterministically skip."""

    station = str(station_id or "").strip().upper()
    day = str(event_date or "").strip()
    if not station or not day:
        raise ContractError("settlement_truth_scope_missing")
    as_of = _utc("settlement_truth_as_of_utc", as_of_utc)
    timezone_name, source_lane, registry_snapshot_sha = _registry_scope(
        target_conn,
        station_id=station,
        event_date=day,
        as_of_utc=_stamp(as_of),
    )
    window = settlement_day_window(day, timezone_name)
    start = _utc(
        "settlement_truth_window_start",
        window["target_day_start_utc"],
    )
    end = _utc(
        "settlement_truth_window_end",
        window["target_day_end_utc"],
    )
    if as_of < end:
        return {
            "schema": SCHEMA,
            "state": "skipped_open_settlement_day",
            "station_id": station,
            "event_date": day,
            "settlement_timezone": timezone_name,
            "settlement_closed_at_utc": _stamp(end),
            "wrote": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }

    try:
        correction_review = assert_station_day_corrections_resolved(
            canonical_conn,
            source_lane=source_lane.lower(),
            station_id=station,
            event_date=day,
            window_start_utc=window["target_day_start_utc"],
            window_end_utc=window["target_day_end_utc"],
            as_of_utc=_stamp(as_of),
        )
    except ContractError as exc:
        raise ContractError(
            f"settlement_truth_correction_requires_manual_review:{exc}"
        ) from exc
    rows = canonical_conn.execute(
        "SELECT observation_id,report_type,valid_at,"
        "reported_temperature_c,raw_sha256,canonical_committed_at,"
        "correction_suspected "
        "FROM reported_trajectory_observations "
        "WHERE station_id=? AND source_lane=? "
        "AND julianday(valid_at)>=julianday(?) "
        "AND julianday(valid_at)<julianday(?) "
        "AND julianday(canonical_committed_at)<=julianday(?) "
        "ORDER BY valid_at,canonical_committed_at,observation_id LIMIT ?",
        (
            station,
            source_lane.lower(),
            window["target_day_start_utc"],
            window["target_day_end_utc"],
            _stamp(as_of),
            MAX_OBSERVATIONS_PER_SCOPE + 1,
        ),
    ).fetchall()
    if len(rows) > MAX_OBSERVATIONS_PER_SCOPE:
        raise ContractError("settlement_truth_observation_scope_cap_exceeded")
    hourly: list[dict[str, Any]] = []
    hourly_by_valid_at: dict[str, dict[str, Any]] = {}
    for row in rows:
        report_type = str(row[1] or "").strip().upper()
        if report_type not in {"METAR", "SPECI"}:
            raise ContractError("settlement_truth_report_type_invalid")
        valid_at = _utc("settlement_truth_valid_at", row[2])
        committed_at = _utc("settlement_truth_committed_at", row[5])
        if not (start <= valid_at < end) or valid_at > committed_at:
            raise ContractError("settlement_truth_observation_time_invalid")
        if int(row[6] or 0) != 0:
            continue
        if valid_at.minute != 0 or valid_at.second != 0:
            continue
        try:
            temperature_c = float(row[3])
        except (TypeError, ValueError) as exc:
            raise ContractError("settlement_truth_temperature_invalid") from exc
        if not math.isfinite(temperature_c):
            raise ContractError("settlement_truth_temperature_invalid")
        raw_sha = str(row[4] or "").strip().lower()
        if (
            len(raw_sha) != 64
            or any(char not in "0123456789abcdef" for char in raw_sha)
        ):
            raise ContractError("settlement_truth_raw_sha256_invalid")
        item = {
            "observation_id": str(row[0] or "").strip(),
            "report_type": report_type,
            "valid_at_utc": _stamp(valid_at),
            "canonical_committed_at_utc": _stamp(committed_at),
            "reported_temperature_c": temperature_c,
            "raw_sha256": raw_sha,
        }
        previous = hourly_by_valid_at.get(item["valid_at_utc"])
        if previous is not None:
            if (
                previous["raw_sha256"] == item["raw_sha256"]
                and previous["reported_temperature_c"]
                == item["reported_temperature_c"]
                and previous["report_type"] == item["report_type"]
            ):
                continue
            raise ContractError(
                "settlement_truth_hourly_revision_ambiguous"
            )
        hourly_by_valid_at[item["valid_at_utc"]] = item
        hourly.append(item)
    if not hourly or any(not item["observation_id"] for item in hourly):
        raise ContractError("settlement_truth_hourly_observation_missing")

    evidence = {
        "schema": "settlement_truth_source_evidence_v1",
        "station_id": station,
        "event_date": day,
        "settlement_timezone": timezone_name,
        "registry_snapshot_sha256": registry_snapshot_sha,
        "source_lane": source_lane,
        "correction_review_ids": list(
            correction_review.get("review_ids") or []
        ),
        "settlement_window": {
            "start_utc": _stamp(start),
            "end_utc": _stamp(end),
        },
        "observations": hourly,
    }
    source_payload_sha = content_sha256(evidence)
    official_tmax_c = max(
        float(item["reported_temperature_c"]) for item in hourly
    )
    latest = target_conn.execute(
        "SELECT truth_id,revision_no,official_tmax_c,source_payload_sha256,"
        "content_sha256 FROM calibration_station_daily_high_truth "
        "WHERE station_id=? AND local_date=? "
        "AND settlement_definition='official_daily_tmax_c' "
        "ORDER BY revision_no DESC,truth_id DESC LIMIT 1",
        (station, day),
    ).fetchone()
    if (
        latest is not None
        and float(latest[2]) == official_tmax_c
        and str(latest[3]) == source_payload_sha
    ):
        return {
            "schema": SCHEMA,
            "state": "idempotent",
            "station_id": station,
            "event_date": day,
            "truth_id": str(latest[0]),
            "revision_no": int(latest[1]),
            "content_sha256": str(latest[4]),
            "wrote": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }

    revision_no = 0 if latest is None else int(latest[1]) + 1
    truth_seed = {
        "station_id": station,
        "local_date": day,
        "revision_no": revision_no,
        "source_payload_sha256": source_payload_sha,
    }
    truth_id = "truth_" + content_sha256(truth_seed)[:40]
    identity = {
        "truth_id": truth_id,
        "station_id": station,
        "local_date": day,
        "official_tmax_c": official_tmax_c,
        "source_lane": source_lane,
        "settlement_definition": "official_daily_tmax_c",
        "revision_no": revision_no,
        "published_at_utc": max(
            item["valid_at_utc"] for item in hourly
        ),
        "received_at_utc": max(
            item["canonical_committed_at_utc"] for item in hourly
        ),
        "canonical_committed_at_utc": _stamp(as_of),
        "timestamps_complete": 1,
        "qc_status": "ACCEPTED",
        "source_role": SOURCE_ROLE,
        "source_payload_sha256": source_payload_sha,
        # The legacy column name is retained to avoid inventing a new schema
        # after migration.  Its value is now a self-describing bounded
        # lineage object, so the registry timezone/snapshot and raw evidence
        # behind source_payload_sha256 remain independently auditable.
        "observation_ids_json": canonical_json(evidence),
        "authority_scope": AUTHORITY_SCOPE,
    }
    truth_sha = content_sha256(identity)
    target_conn.execute(
        "INSERT INTO calibration_station_daily_high_truth("
        "truth_id,station_id,local_date,official_tmax_c,source_lane,"
        "settlement_definition,revision_no,published_at_utc,"
        "received_at_utc,canonical_committed_at_utc,timestamps_complete,"
        "qc_status,source_role,source_payload_sha256,"
        "observation_ids_json,authority_scope,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(identity.values()) + (truth_sha,),
    )
    assert_process_memory_budget()
    return {
        "schema": SCHEMA,
        "state": "committed",
        "station_id": station,
        "event_date": day,
        "settlement_timezone": timezone_name,
        "truth_id": truth_id,
        "revision_no": revision_no,
        "official_tmax_c": official_tmax_c,
        "content_sha256": truth_sha,
        "wrote": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


def materialize_settled_training_truths(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    max_scopes: int = MAX_TRUTH_SCOPES,
    after_fact_rowid: int = 0,
    additional_scopes: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Finalize one bounded page of downstream and bootstrap truth scopes.

    A fixed ``SELECT DISTINCT ... LIMIT`` either blocks as soon as history
    exceeds the station cap or repeatedly scans the oldest scopes forever.
    The fact-rowid cursor makes the projection bounded and eventually revisits
    every historical scope, including a scope that receives a later fact.
    ``additional_scopes`` lets an upstream forward-evidence owner bootstrap
    truth before the first basket training fact exists; this module still
    receives only station/date identities and never reads model evidence.
    """

    if not 1 <= int(max_scopes) <= MAX_TRUTH_SCOPES:
        raise ContractError("settlement_truth_scope_limit_invalid")
    if (
        isinstance(after_fact_rowid, bool)
        or int(after_fact_rowid) < 0
    ):
        raise ContractError("settlement_truth_cursor_invalid")
    cursor = int(after_fact_rowid)
    supplemental: list[tuple[str, str]] = []
    supplemental_seen: set[tuple[str, str]] = set()
    if (
        not isinstance(additional_scopes, Sequence)
        or isinstance(additional_scopes, (str, bytes))
        or len(additional_scopes) > int(max_scopes)
    ):
        raise ContractError("settlement_truth_additional_scope_count_invalid")
    for raw_scope in additional_scopes:
        if (
            not isinstance(raw_scope, Sequence)
            or isinstance(raw_scope, (str, bytes))
            or len(raw_scope) != 2
        ):
            raise ContractError("settlement_truth_additional_scope_invalid")
        scope = (
            str(raw_scope[0] or "").strip().upper(),
            str(raw_scope[1] or "").strip(),
        )
        if not all(scope):
            raise ContractError("settlement_truth_additional_scope_invalid")
        if scope in supplemental_seen:
            continue
        supplemental_seen.add(scope)
        supplemental.append(scope)

    def _page(
        after_rowid: int,
        page_limit: int,
    ) -> list[tuple[Any, Any, Any]]:
        if page_limit <= 0:
            return []
        return target_conn.execute(
            "SELECT station_id,event_date,max(rowid) AS source_rowid "
            "FROM basket_policy_training_fact WHERE rowid>? "
            "GROUP BY station_id,event_date "
            "ORDER BY source_rowid,station_id,event_date LIMIT ?",
            (after_rowid, int(page_limit)),
        ).fetchall()

    remaining = int(max_scopes) - len(supplemental)
    # Read enough fact scopes to fill the residual page even when some are
    # duplicates of bootstrap scopes, plus one row to preserve has_more.
    fact_probe_limit = remaining + len(supplemental_seen) + 1
    rows = _page(cursor, fact_probe_limit)
    wrapped = False
    if not rows and cursor > 0:
        rows = _page(0, fact_probe_limit)
        wrapped = True
    selected: list[tuple[Any, Any, Any]] = []
    for row in rows:
        scope = (
            str(row[0] or "").strip().upper(),
            str(row[1] or "").strip(),
        )
        if scope in supplemental_seen:
            continue
        if len(selected) >= remaining:
            break
        selected.append(row)
    consumed_rowids = {
        int(row[2])
        for row in selected
    }
    has_more = any(
        int(row[2]) not in consumed_rowids
        and (
            str(row[0] or "").strip().upper(),
            str(row[1] or "").strip(),
        )
        not in supplemental_seen
        for row in rows
    )
    fact_scopes = [
        (
            str(row[0] or "").strip().upper(),
            str(row[1] or "").strip(),
        )
        for row in selected
    ]
    scopes = supplemental + fact_scopes
    next_source_rowid = (
        max(int(row[2]) for row in selected)
        if selected
        else cursor
    )
    results: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    for scope_index, (station_id, event_date) in enumerate(scopes):
        savepoint = f"settlement_truth_scope_{scope_index}"
        target_conn.execute(f"SAVEPOINT {savepoint}")
        try:
            result = materialize_settled_station_truth(
                target_conn,
                canonical_conn,
                station_id=str(station_id),
                event_date=str(event_date),
                as_of_utc=as_of_utc,
            )
            target_conn.execute(f"RELEASE {savepoint}")
            results.append(result)
        except Exception as exc:
            target_conn.execute(f"ROLLBACK TO {savepoint}")
            target_conn.execute(f"RELEASE {savepoint}")
            blockers.append({
                "station_id": str(station_id),
                "event_date": str(event_date),
                "reason": f"{type(exc).__name__}:{exc}",
            })
        assert_process_memory_budget()
    return {
        "schema": "bayes_basket_target_resident_settlement_truth_batch_v1",
        "state": (
            "partial_fail_closed"
            if blockers and results
            else "blocked_fail_closed"
            if blockers
            else "complete"
        ),
        "scope_count": len(scopes),
        "additional_scope_count": len(supplemental),
        "training_fact_scope_count": len(fact_scopes),
        "written_count": sum(bool(item.get("wrote")) for item in results),
        "source_cursor_before": cursor,
        "source_cursor_after": next_source_rowid,
        "cursor_wrapped": wrapped,
        "has_more": has_more,
        "max_scopes": int(max_scopes),
        "results": results,
        "blockers": blockers,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


__all__ = [
    "AUTHORITY_SCOPE",
    "MAX_OBSERVATIONS_PER_SCOPE",
    "MAX_TRUTH_SCOPES",
    "SCHEMA",
    "materialize_settled_station_truth",
    "materialize_settled_training_truths",
]
