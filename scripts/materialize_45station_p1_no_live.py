#!/usr/bin/env python3
"""Run one bounded 45-station P1/METAR no-live compilation pass.

This is orchestration only.  The resident canonical reader owns the official
AMSC/AWC source-lane and strict-as-of rules; ``compile_station_p1_packets``
owns the sequential P1 compiler and calls the existing ``materialize_p1``
implementation.  The canonical source database is opened read-only and the
target SSOT is mutated only when ``--commit`` is explicitly supplied.

The command is deliberately not a worker, scheduler, market reader, or order
submitter.  A future settlement day and a started day with no formal report
are successful no-op outcomes (``NO_ELIGIBLE_P1``), so a one-shot invocation
never waits for a future report or falls back from AMSC to AWC.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, ProbabilityEnvelope
from bayes_basket_target.database import (
    DEFAULT_TARGET_DB,
    SHARED_LIVE_DB,
    resolve_target_database,
)
from bayes_basket_target.market_registry import (
    load_active_registry_rows,
    load_started_registry_rows,
)
from bayes_basket_target.p1_compiler import (
    compile_station_p1_packets,
    select_p1_replay_base,
)
from bayes_basket_target.probability_bridge import (
    build_xsb_projection,
    materialize_probability_bridge,
    probability_bridge_schema_status,
)
from bayes_basket_target.probability_evidence_store import (
    metar_trajectory_scope_key,
    resolve_active_probability_evidence_artifact,
)
from bayes_basket_target.production_io import (
    load_materialized_probability_envelopes,
)
from bayes_basket_target.resident_canonical import (
    load_resident_formal_observations,
)
from bayes_basket_target.resource_bounds import (
    assert_process_memory_budget,
)
from bayes_basket_target.settlement_time import (
    registry_settlement_timezone,
    settlement_day_state,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest
from bayes_basket_target.metar_trajectory import MetarTrajectoryLikelihoodArtifact


SCHEMA = "bayes_basket_target_45station_p1_no_live_v1"
NO_ELIGIBLE_P1 = "NO_ELIGIBLE_P1"
VALIDATED_NO_WRITE = "VALIDATED_NO_WRITE"
COMMITTED = "COMMITTED"
BLOCKED_FAIL_CLOSED = "BLOCKED_FAIL_CLOSED"
EXECUTION_MODE = "production_no_live"
MAX_STATIONS = 45


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("p1_no_live_as_of_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("p1_no_live_as_of_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _open_canonical_read_only(path: Path) -> sqlite3.Connection:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ContractError("p1_no_live_canonical_database_missing")
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _open_target(path: Path, *, commit: bool) -> sqlite3.Connection:
    """Open an existing target DB without creating anything in preview mode."""

    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise ContractError("p1_no_live_target_database_missing")
    if commit:
        conn = sqlite3.connect(target, timeout=60.0)
    else:
        conn = sqlite3.connect(
            f"file:{target}?mode=ro",
            uri=True,
            timeout=60.0,
        )
    conn.row_factory = sqlite3.Row
    if not commit:
        conn.execute("PRAGMA query_only=ON")
    return conn


def _canonical_station_ids(station_ids: Sequence[str] | None) -> tuple[str, ...]:
    canonical = tuple(coordinate_registry_manifest()["station_ids"])
    if len(canonical) != MAX_STATIONS or len(set(canonical)) != MAX_STATIONS:
        raise ContractError("p1_no_live_canonical_station_registry_invalid")
    requested = tuple(station_ids or canonical)
    normalized = tuple(sorted({str(item or "").strip().upper() for item in requested if str(item or "").strip()}))
    unknown = set(normalized) - set(canonical)
    if unknown:
        raise ContractError("p1_no_live_station_not_in_canonical_registry:" + ",".join(sorted(unknown)))
    if not normalized:
        raise ContractError("p1_no_live_station_scope_empty")
    return normalized


def _source_lane(registry_rows: Sequence[Mapping[str, Any]], station_id: str, event_date: str) -> str:
    lanes = {
        str(row.get("source_lane") or "").strip().lower()
        for row in registry_rows
        if str(row.get("station_id") or "").strip().upper() == station_id
        and str(row.get("event_date") or "") == event_date
    }
    if lanes != {"amsc"} and lanes != {"awc"}:
        raise ContractError("p1_no_live_source_lane_not_unique")
    # This lane is the station's authority.  It is passed into the resident
    # reader, which queries exactly this lane and therefore cannot use AWC as
    # a real-time fallback for an AMSC-owned (including China) station.
    return next(iter(lanes))


def _strict_formal_scope(
    observations: Sequence[Mapping[str, Any]],
    *,
    station_id: str,
    event_date: str,
    source_lane: str,
    as_of_utc: str,
) -> list[dict[str, Any]]:
    """Recheck the reader contract at the orchestration boundary."""

    as_of = _utc(as_of_utc)
    result: list[dict[str, Any]] = []
    for raw in observations:
        row = dict(raw)
        if str(row.get("station_id") or "").strip().upper() != station_id:
            raise ContractError("p1_no_live_observation_station_mismatch")
        if str(row.get("event_date_local") or "") != event_date:
            raise ContractError("p1_no_live_observation_event_date_mismatch")
        if str(row.get("source_lane") or "").strip().lower() != source_lane:
            raise ContractError("p1_no_live_observation_source_lane_mismatch")
        if str(row.get("evidence_role") or "").strip().upper() != "FORMAL":
            raise ContractError("p1_no_live_nonformal_observation_admitted")
        if str(row.get("qc_status") or "").strip().upper() != "ACCEPTED":
            raise ContractError("p1_no_live_nonaccepted_observation_admitted")
        if str(row.get("report_type") or "").strip().upper() not in {"METAR", "SPECI"}:
            raise ContractError("p1_no_live_report_type_invalid")
        if bool(row.get("correction_suspected")):
            raise ContractError("p1_no_live_correction_observation_admitted")
        committed = _utc(str(row.get("canonical_committed_at_utc") or ""))
        valid_at = _utc(str(row.get("valid_at_utc") or ""))
        if committed > as_of:
            raise ContractError("p1_no_live_observation_not_strict_asof")
        if valid_at > committed:
            raise ContractError("p1_no_live_observation_time_order_invalid")
        result.append(row)
    result.sort(
        key=lambda row: (
            str(row.get("valid_at_utc") or ""),
            str(row.get("canonical_committed_at_utc") or ""),
            str(row.get("observation_id") or ""),
        )
    )
    return result


def _local_time_bucket(as_of_utc: str, timezone_name: str) -> str:
    hour = _utc(as_of_utc).astimezone(ZoneInfo(timezone_name)).hour
    if hour < 6:
        return "NIGHT"
    if hour < 12:
        return "MORNING"
    if hour < 18:
        return "AFTERNOON"
    return "EVENING"


def _resolve_trajectory_artifact(
    target_conn: sqlite3.Connection,
    *,
    prediction_root: ProbabilityEnvelope,
    source_lane: str,
    as_of_utc: str,
    maturity_bin: str,
) -> MetarTrajectoryLikelihoodArtifact | None:
    """Use the existing promoted artifact when one is available.

    Missing artifact is left to the existing compiler's bounded fallback,
    matching the resident production path.  No likelihood or probability is
    implemented here.
    """

    scope_key = metar_trajectory_scope_key(
        source_lane=source_lane,
        season_bin=str(prediction_root.lineage.get("season_bin") or "UNKNOWN"),
        local_time_bucket=_local_time_bucket(as_of_utc, prediction_root.settlement_timezone),
        maturity_bin=maturity_bin,
        settlement_definition=str(
            prediction_root.lineage.get("settlement_definition") or "UNKNOWN"
        ),
    )
    row = resolve_active_probability_evidence_artifact(
        target_conn,
        artifact_kind="METAR_TRAJECTORY_LIKELIHOOD",
        station_id=prediction_root.station_id,
        scope_key=scope_key,
        as_of_utc=as_of_utc,
        required_authority_scope="FORMAL_PROMOTED",
    )
    if row is None:
        return None
    return MetarTrajectoryLikelihoodArtifact.from_mapping(row["artifact"])


def _scope_result(
    *,
    station_id: str,
    event_date: str,
    state: str,
    reason: str = "",
    settlement_timezone: str = "",
    source_lane: str = "",
    observation_count: int = 0,
    packets: Sequence[Mapping[str, Any]] = (),
    writes: Sequence[Mapping[str, Any]] = (),
    p0_root_checkpoint_id: str = "",
    p0_rebase: bool = False,
) -> dict[str, Any]:
    wrote_target_ssot = any(
        bool(item.get("wrote"))
        or bool(item.get("history_wrote"))
        or bool(item.get("latest_wrote"))
        for item in writes
        if isinstance(item, Mapping)
    )
    return {
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "source_lane": source_lane.upper() if source_lane else "",
        "state": state,
        "reason": reason,
        "observation_count": int(observation_count),
        "p1_update_count": len(packets),
        "p1_checkpoint_ids": [
            str(item.get("expected_envelope", {}).get("checkpoint_id") or "")
            for item in packets
        ],
        "p0_root_checkpoint_id": p0_root_checkpoint_id,
        "p0_rebase": bool(p0_rebase),
        "writes": list(writes),
        "writes_target_ssot": wrote_target_ssot,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_order_authorized": False,
    }


def _persist_packets(
    target_conn: sqlite3.Connection,
    packets: Sequence[Mapping[str, Any]],
    *,
    commit: bool,
) -> list[dict[str, Any]]:
    """Persist one station scope through the existing atomic X-S-B seam."""

    if not packets:
        return []
    if not commit:
        # Exercise the exact bridge projection in validation mode without
        # opening a write transaction or touching the target SSOT.
        for packet in packets:
            build_xsb_projection(
                ProbabilityEnvelope.from_mapping(packet["expected_envelope"])
            )
        return []
    savepoint = "p1_no_live_scope"
    target_conn.execute(f"SAVEPOINT {savepoint}")
    writes: list[dict[str, Any]] = []
    try:
        for packet in packets:
            envelope = ProbabilityEnvelope.from_mapping(packet["expected_envelope"])
            writes.append(
                materialize_probability_bridge(
                    target_conn,
                    envelope,
                    created_at_utc=str(packet.get("created_at_utc") or envelope.decision_at_utc),
                )
            )
        target_conn.execute(f"RELEASE {savepoint}")
        target_conn.commit()
    except BaseException:
        target_conn.execute(f"ROLLBACK TO {savepoint}")
        target_conn.execute(f"RELEASE {savepoint}")
        target_conn.rollback()
        raise
    return writes


def materialize_45station_p1_no_live(
    *,
    db_path: Path = DEFAULT_TARGET_DB,
    canonical_db_path: Path = SHARED_LIVE_DB,
    event_date: str,
    as_of_utc: str,
    station_ids: Sequence[str] | None = None,
    freshness_seconds: int = 3600,
    maturity_bin: str = "D0",
    commit: bool = False,
) -> dict[str, Any]:
    """Compile one event-date P1 pass for up to the canonical 45 stations."""

    if freshness_seconds <= 0:
        raise ContractError("p1_no_live_freshness_seconds_invalid")
    decision = _stamp(_utc(as_of_utc))
    day = str(event_date or "").strip()
    if not day:
        raise ContractError("p1_no_live_event_date_missing")
    stations = _canonical_station_ids(station_ids)
    target_path = resolve_target_database(db_path)
    if target_path == Path(canonical_db_path).expanduser().resolve():
        raise ContractError("p1_no_live_target_and_canonical_database_must_differ")

    scopes: list[dict[str, Any]] = []
    with closing(_open_canonical_read_only(canonical_db_path)) as canonical_conn:
        with closing(_open_target(target_path, commit=commit)) as target_conn:
            bridge_status = probability_bridge_schema_status(target_conn)
            bridge_blocker = (
                "p1_no_live_probability_bridge_schema_missing:"
                + ",".join(bridge_status["missing_tables"])
                if not bridge_status["ok"]
                else ""
            )
            for station in stations:
                assert_process_memory_budget()
                try:
                    try:
                        active_rows = load_active_registry_rows(
                            target_conn,
                            station_id=station,
                            event_date=day,
                        )
                    except ContractError as exc:
                        if str(exc) != "target_market_registry_snapshot_empty":
                            raise
                        scopes.append(
                            _scope_result(
                                station_id=station,
                                event_date=day,
                                state=NO_ELIGIBLE_P1,
                                reason="no_active_market_scope_no_eligible_p1",
                            )
                        )
                        continue
                    if not active_rows:
                        scopes.append(
                            _scope_result(
                                station_id=station,
                                event_date=day,
                                state=NO_ELIGIBLE_P1,
                                reason="no_active_market_scope_no_eligible_p1",
                            )
                        )
                        continue
                    timezone_name = registry_settlement_timezone(
                        active_rows,
                        station_id=station,
                        event_date=day,
                    )
                    lane = _source_lane(active_rows, station, day)
                    day_state = settlement_day_state(
                        day,
                        as_of_utc=decision,
                        settlement_timezone=timezone_name,
                    )
                    if day_state["state"] == "not_started":
                        scopes.append(
                            _scope_result(
                                station_id=station,
                                event_date=day,
                                state=NO_ELIGIBLE_P1,
                                reason="future_event_day_no_eligible_p1",
                                settlement_timezone=timezone_name,
                                source_lane=lane,
                            )
                        )
                        continue
                    # Use the started view for the actual formal scope.  It
                    # intentionally cannot admit future rows.
                    started_rows = load_started_registry_rows(
                        target_conn,
                        as_of_utc=decision,
                        station_id=station,
                        event_date=day,
                    )
                    if not started_rows:
                        scopes.append(
                            _scope_result(
                                station_id=station,
                                event_date=day,
                                state=NO_ELIGIBLE_P1,
                                reason="event_day_not_started_no_eligible_p1",
                                settlement_timezone=timezone_name,
                                source_lane=lane,
                            )
                        )
                        continue
                    observations = load_resident_formal_observations(
                        canonical_conn,
                        registry_rows=started_rows,
                        station_id=station,
                        event_date=day,
                        as_of_utc=decision,
                    )
                    observations = _strict_formal_scope(
                        observations,
                        station_id=station,
                        event_date=day,
                        source_lane=lane,
                        as_of_utc=decision,
                    )
                    if not observations:
                        scopes.append(
                            _scope_result(
                                station_id=station,
                                event_date=day,
                                state=NO_ELIGIBLE_P1,
                                reason="no_formal_metar_or_speci_asof_no_eligible_p1",
                                settlement_timezone=timezone_name,
                                source_lane=lane,
                            )
                        )
                        continue
                    envelopes = load_materialized_probability_envelopes(
                        target_conn,
                        station_id=station,
                        event_date=day,
                        evaluation_at_utc=decision,
                    )
                    prediction_root, predecessor, consumed, rebasing = select_p1_replay_base(
                        envelopes,
                        as_of_utc=decision,
                    )
                    if (
                        prediction_root.settlement_timezone != timezone_name
                        or predecessor.settlement_timezone != timezone_name
                    ):
                        raise ContractError("p1_no_live_probability_registry_timezone_mismatch")
                    artifact = _resolve_trajectory_artifact(
                        target_conn,
                        prediction_root=prediction_root,
                        source_lane=lane,
                        as_of_utc=decision,
                        maturity_bin=maturity_bin,
                    )
                    packets = compile_station_p1_packets(
                        predecessor=predecessor,
                        prediction_root=prediction_root,
                        observations=observations,
                        registry_rows=started_rows,
                        compilation_at_utc=decision,
                        freshness_seconds=freshness_seconds,
                        consumed_observation_ids=consumed,
                        trajectory_artifact=artifact,
                        maturity_bin=maturity_bin,
                    )
                    if not packets:
                        scopes.append(
                            _scope_result(
                                station_id=station,
                                event_date=day,
                                state=NO_ELIGIBLE_P1,
                                reason="no_unconsumed_formal_p1_update",
                                settlement_timezone=timezone_name,
                                source_lane=lane,
                                observation_count=len(observations),
                                p0_root_checkpoint_id=prediction_root.checkpoint_id,
                                p0_rebase=rebasing,
                            )
                        )
                        continue
                    if bridge_blocker:
                        raise ContractError(bridge_blocker)
                    writes = _persist_packets(target_conn, packets, commit=commit)
                    scopes.append(
                        _scope_result(
                            station_id=station,
                            event_date=day,
                            state=COMMITTED if commit else VALIDATED_NO_WRITE,
                            settlement_timezone=timezone_name,
                            source_lane=lane,
                            observation_count=len(observations),
                            packets=packets,
                            writes=writes,
                            p0_root_checkpoint_id=prediction_root.checkpoint_id,
                            p0_rebase=rebasing,
                        )
                    )
                except (ContractError, ValueError, TypeError, sqlite3.Error) as exc:
                    scopes.append(
                        _scope_result(
                            station_id=station,
                            event_date=day,
                            state=BLOCKED_FAIL_CLOSED,
                            reason=f"{type(exc).__name__}:{exc}",
                        )
                    )
    hard_blockers = [item for item in scopes if item["state"] == BLOCKED_FAIL_CLOSED]
    updates = [item for item in scopes if item["p1_update_count"] > 0]
    actual_writes = [item for item in scopes if item["writes_target_ssot"]]
    if hard_blockers:
        state = BLOCKED_FAIL_CLOSED
    elif updates:
        state = COMMITTED if commit else VALIDATED_NO_WRITE
    else:
        state = NO_ELIGIBLE_P1
    return {
        "schema": SCHEMA,
        "execution_mode": EXECUTION_MODE,
        "state": state,
        "event_date": day,
        "as_of_utc": decision,
        "station_scope": {
            "requested_station_ids": list(stations),
            "station_count": len(stations),
            "canonical_station_count": MAX_STATIONS,
        },
        "scopes": scopes,
        "scope_count": len(scopes),
        "eligible_scope_count": len(updates),
        "blocked_scope_count": len(hard_blockers),
        "p1_update_count": sum(int(item["p1_update_count"]) for item in scopes),
        "target_ssot_write_scope_count": len(actual_writes),
        "boundary": {
            "writes_target_ssot": bool(commit and actual_writes),
            "writes_canonical_database": False,
            "reads_canonical_database_read_only": True,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_order_authorized": False,
            "live_submit_authorized": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--canonical-db", type=Path, default=SHARED_LIVE_DB)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--freshness-seconds", type=int, default=3600)
    parser.add_argument("--maturity-bin", default="D0")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = materialize_45station_p1_no_live(
            db_path=args.db,
            canonical_db_path=args.canonical_db,
            event_date=args.event_date,
            as_of_utc=args.as_of_utc,
            station_ids=args.station_id or None,
            freshness_seconds=args.freshness_seconds,
            maturity_bin=args.maturity_bin,
            commit=bool(args.commit),
        )
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "execution_mode": EXECUTION_MODE,
            "state": BLOCKED_FAIL_CLOSED,
            "reason": f"{type(exc).__name__}:{exc}",
            "boundary": {
                "writes_target_ssot": False,
                "writes_canonical_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
                "live_order_authorized": False,
                "live_submit_authorized": False,
            },
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {NO_ELIGIBLE_P1, VALIDATED_NO_WRITE, COMMITTED} else 2


if __name__ == "__main__":
    raise SystemExit(main())
