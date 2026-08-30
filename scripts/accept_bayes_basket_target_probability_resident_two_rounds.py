#!/usr/bin/env python3
"""Two-round read-only acceptance for canonical probability-evidence residents."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    content_sha256,
)
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.market_registry import (  # noqa: E402
    load_started_registry_rows,
)
from bayes_basket_target.official_nwp_capture_scheduler import (  # noqa: E402
    candidate_run_initializations,
)
from bayes_basket_target.p0_producer import (  # noqa: E402
    compatible_run_candidates,
)
from bayes_basket_target.production_io import (  # noqa: E402
    load_production_artifacts,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)
from bayes_basket_target.target_ssot_replay import (  # noqa: E402
    replay_durable_target_chain,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_DB_BYTES,
    MAX_TARGET_WAL_BYTES,
    write_bounded_json,
)
from scripts.bayes_basket_target_production_readiness import (  # noqa: E402
    _probability_evidence_runtime_status,
)


SCHEMA = "bayes_basket_target_probability_resident_two_round_acceptance_v2"
ROUND_SCHEMA = "bayes_basket_target_probability_resident_round_v2"
MIGRATION_SCHEMA = (
    "bayes_basket_target_probability_migration_resident_acceptance_v1"
)
CORE_SERVICES = (
    "com.weather.weather_basket_yes",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)
STATION_TABLES = (
    "basket_probability_evidence_artifact",
    "basket_probability_evidence_activation",
    "basket_probability_update_proof",
    "basket_probability_bridge_history",
    "basket_probability_bridge_latest",
    "basket_probability_envelope",
    "auto_trade_decisions",
    "basket_policy_training_fact",
)
MAX_CORE_ROWS = 512
MAX_JSON_BYTES = 1024 * 1024
MAX_DB_BYTES = MAX_TARGET_DB_BYTES
MAX_WAL_BYTES = MAX_TARGET_WAL_BYTES
MIN_FREE_BYTES = 1024 * 1024 * 1024
CANONICAL_STATION_IDS = tuple(
    coordinate_registry_manifest()["station_ids"]
)
MAX_STATION_SCOPE = expected_station_count()
MATERIALIZATION_PENDING_BLOCKER_SUFFIXES = (
    "formal_two_model_scope_missing_p0",
    "candidate_artifact_missing",
    "candidate_rejection_incomplete",
    "directional_core_decision_missing",
    "identity_likelihood_fallback_not_consumed",
    "theta_zero_fallback_not_consumed",
)


def normalize_station_scope(
    station_ids: Sequence[str] | None = None,
) -> tuple[str, ...]:
    canonical = tuple(CANONICAL_STATION_IDS)
    if len(canonical) != MAX_STATION_SCOPE or len(set(canonical)) != len(
        canonical
    ):
        raise RuntimeError("canonical_station_registry_invalid")
    raw = canonical if station_ids is None else tuple(station_ids)
    normalized = tuple(
        sorted(
            {
                str(station_id or "").strip().upper()
                for station_id in raw
                if str(station_id or "").strip()
            }
        )
    )
    if not normalized:
        raise RuntimeError("resident_acceptance_station_scope_empty")
    if len(normalized) > MAX_STATION_SCOPE:
        raise RuntimeError("resident_acceptance_station_scope_exceeds_limit")
    if len(normalized) != len(raw):
        raise RuntimeError("resident_acceptance_station_scope_duplicate_or_empty")
    unknown = sorted(set(normalized) - set(canonical))
    if unknown:
        raise RuntimeError(
            "resident_acceptance_station_scope_not_canonical:"
            + ",".join(unknown)
        )
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _formal_p0_scope_eligibility(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    registry_rows: Sequence[Mapping[str, Any]],
    as_of_utc: str,
) -> dict[str, Any]:
    """Classify whether an open scope already owns a strict 2+ OOS combo.

    Candidate raw acquisition must be allowed before formal calibration
    exists, while a station that already owns an exact compatible activation
    must not hide a missing P0 behind the candidate-only state.
    """

    station = str(station_id or "").strip().upper()
    scopes = sorted({
        (
            str(row.get("event_date") or "").strip(),
            str(row.get("settlement_timezone") or "").strip(),
        )
        for row in registry_rows
        if (
            str(row.get("station_id") or "").strip().upper() == station
            and str(row.get("event_date") or "").strip()
            and str(row.get("settlement_timezone") or "").strip()
        )
    })
    try:
        artifacts = load_production_artifacts(
            conn,
            station_id=station,
            evaluation_at_utc=as_of_utc,
            require_calibration=True,
            require_policy=False,
        )
    except Exception as exc:
        return {
            "eligible": False,
            "compatible_scopes": [],
            "reason": (
                "strict_oos_artifact_not_available:"
                f"{type(exc).__name__}:{exc}"
            ),
        }
    compatible_scopes: list[dict[str, Any]] = []
    for event_date, timezone_name in scopes:
        try:
            compatible = compatible_run_candidates(
                artifacts=artifacts,
                station=station,
                event_date=event_date,
                settlement_timezone=timezone_name,
                decision_at_utc=as_of_utc,
                run_initializations=candidate_run_initializations(
                    as_of_utc=as_of_utc,
                    event_date=event_date,
                    settlement_timezone=timezone_name,
                ),
            )
        except Exception:
            compatible = []
        for candidate in compatible:
            selected_models = {
                str(value or "").strip().upper()
                for value in (
                    candidate.get("activation_binding") or {}
                ).get("selected_models", [])
                if str(value or "").strip()
            }
            if len(selected_models) >= 2:
                compatible_scopes.append({
                    "event_date": event_date,
                    "settlement_timezone": timezone_name,
                    "run_initialization_utc": candidate.get(
                        "run_initialization_utc"
                    ),
                    "selected_models": sorted(selected_models),
                })
    return {
        "eligible": bool(compatible_scopes),
        "compatible_scopes": compatible_scopes,
        "reason": (
            "strict_two_model_scope_available"
            if compatible_scopes
            else "strict_two_model_scope_not_available"
        ),
    }


def _service_pids() -> dict[str, int]:
    result: dict[str, int] = {}
    domain = f"gui/{os.getuid()}"
    for label in CORE_SERVICES:
        completed = subprocess.run(
            ["/bin/launchctl", "print", f"{domain}/{label}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"resident_acceptance_service_missing:{label}")
        pids = []
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("pid = "):
                value = stripped.removeprefix("pid = ").strip()
                if value.isdigit():
                    pids.append(int(value))
        if len(pids) != 1:
            raise RuntimeError(
                f"resident_acceptance_service_pid_invalid:{label}"
            )
        result[label] = pids[0]
    return result


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _station_counts(
    conn: sqlite3.Connection,
    *,
    station_ids: Sequence[str],
) -> tuple[dict[str, int], dict[str, int]]:
    scope = normalize_station_scope(station_ids)
    placeholders = ",".join("?" for _ in scope)
    present = _table_names(conn)
    scoped: dict[str, int] = {}
    outside: dict[str, int] = {}
    for table in STATION_TABLES:
        if table not in present:
            scoped[table] = -1
            outside[table] = -1
            continue
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if "station_id" not in columns:
            scoped[table] = -1
            outside[table] = -1
            continue
        scoped[table] = int(conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE UPPER(COALESCE(station_id,'')) IN ({placeholders})",
            scope,
        ).fetchone()[0])
        outside[table] = int(conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE UPPER(COALESCE(station_id,'')) NOT IN ({placeholders})",
            scope,
        ).fetchone()[0])
    return scoped, outside


def _validate_core_chain_rows(
    rows: Sequence[tuple[int, int, str]],
) -> dict[str, Any]:
    validated: list[dict[str, Any]] = []
    malformed = 0
    legacy = 0
    violations: list[str] = []
    for rowid, byte_length, raw_json in rows:
        try:
            if int(byte_length) <= 0 or int(byte_length) > MAX_JSON_BYTES:
                raise ValueError
            details = json.loads(str(raw_json))
            core = details.get("core_buckets")
            if core is None:
                continue
            if (
                not isinstance(core, list)
                or not core
                or any(
                    not isinstance(bucket, str) or not bucket.strip()
                    for bucket in core
                )
                or len(core) != len(set(core))
            ):
                raise ValueError
            direction = str(details.get("direction") or "").upper()
            current_direction_raw = details.get("current_direction")
            current_direction = (
                None
                if current_direction_raw in (None, "")
                else str(current_direction_raw).upper()
            )
            transition = details.get("core_transition")
            if (
                direction not in {"UP", "DOWN", "NEUTRAL"}
                or not isinstance(transition, Mapping)
                or str(
                    transition.get("direction_transition") or ""
                ).upper()
                not in {
                    "INITIALIZE",
                    "MIGRATE",
                    "NEUTRALIZE",
                    "KEEP",
                    "HOLD",
                }
            ):
                legacy += 1
                continue
            current_core = details.get("current_core_buckets") or []
            if not isinstance(current_core, list):
                raise ValueError
            validated.append({
                "rowid": int(rowid),
                "core_buckets": list(core),
                "current_core_buckets": list(current_core),
                "direction": direction,
                "current_direction": current_direction,
                "direction_transition": str(
                    transition["direction_transition"]
                ).upper(),
            })
        except (TypeError, ValueError, json.JSONDecodeError):
            malformed += 1
    for previous, current in zip(validated, validated[1:]):
        if current["current_core_buckets"] != previous["core_buckets"]:
            violations.append(
                f"core_predecessor_mismatch:{previous['rowid']}:{current['rowid']}"
            )
        if current["current_direction"] != previous["direction"]:
            violations.append(
                "direction_predecessor_mismatch:"
                f"{previous['rowid']}:{current['rowid']}"
            )
    return {
        "validated_count": len(validated),
        "legacy_count": legacy,
        "malformed_count": malformed,
        "violations": violations,
        "latest": validated[-1] if validated else None,
        "content_sha256": content_sha256(validated),
    }


def _latest_core_chain_rows(
    conn: sqlite3.Connection,
    *,
    station_id: str,
) -> tuple[list[tuple[int, int, str]], bool]:
    """Read only the newest bounded window, returned in causal order."""

    newest_first = conn.execute(
        "SELECT rowid,length(CAST(details_json AS BLOB)),details_json "
        "FROM auto_trade_decisions "
        "WHERE station_id=? AND strategy_lane='basket' "
        "ORDER BY rowid DESC LIMIT ?",
        (station_id, MAX_CORE_ROWS + 1),
    ).fetchall()
    truncated = len(newest_first) > MAX_CORE_ROWS
    bounded = newest_first[:MAX_CORE_ROWS]
    bounded.reverse()
    return bounded, truncated


def _forbidden_execution_counts(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    present = _table_names(conn)
    queries = {
        "will_order": (
            "auto_trade_decisions",
            "SELECT COUNT(*) FROM auto_trade_decisions WHERE will_order<>0",
        ),
        "live_submit_authorized": (
            "basket_target_order_intents",
            "SELECT COUNT(*) FROM basket_target_order_intents "
            "WHERE live_submit_authorized<>0",
        ),
        "order_events": (
            "basket_target_order_events",
            "SELECT COUNT(*) FROM basket_target_order_events",
        ),
        "execution_receipts": (
            "basket_target_execution_receipts",
            "SELECT COUNT(*) FROM basket_target_execution_receipts",
        ),
    }
    return {
        name: (
            int(conn.execute(sql).fetchone()[0])
            if table in present
            else -1
        )
        for name, (table, sql) in queries.items()
    }


def _probability_authority(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_dates: Sequence[str],
) -> dict[str, Any]:
    dates = tuple(sorted(set(event_dates)))
    if not dates:
        return {"state": "NO_STARTED_EVENT_DATE", "items": []}
    present = _table_names(conn)
    if not {
        "basket_probability_bridge_history",
        "basket_probability_bridge_latest",
    }.issubset(present):
        return {"state": "SCHEMA_MISSING", "items": []}
    rank = {"P0": 0, "P0-M": 1, "P1": 2}
    items: list[dict[str, Any]] = []
    blockers: list[str] = []
    for event_date in dates:
        latest = conn.execute(
            "SELECT checkpoint_id,stage FROM "
            "basket_probability_bridge_latest "
            "WHERE station_id=? AND event_date=?",
            (station_id, event_date),
        ).fetchall()
        history_stages = {
            str(row[0])
            for row in conn.execute(
                "SELECT stage FROM basket_probability_bridge_history "
                "WHERE station_id=? AND event_date=?",
                (station_id, event_date),
            )
        }
        latest_stage = str(latest[0][1]) if len(latest) == 1 else ""
        if len(latest) != 1:
            blockers.append(
                f"probability_latest_authority_cardinality:{event_date}"
            )
        elif latest_stage not in rank:
            blockers.append(
                f"probability_latest_authority_stage_invalid:{event_date}"
            )
        if not history_stages or not history_stages.issubset(rank):
            blockers.append(
                f"probability_history_stage_invalid:{event_date}"
            )
        elif latest_stage in rank and rank[latest_stage] != max(
            rank[stage] for stage in history_stages
        ):
            blockers.append(
                f"probability_latest_authority_downgraded:{event_date}"
            )
        items.append({
            "event_date": event_date,
            "checkpoint_id": (
                str(latest[0][0]) if len(latest) == 1 else None
            ),
            "authoritative_stage": latest_stage or None,
            "materialized_stages": sorted(history_stages),
        })
    return {
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
        "items": items,
        "blockers": sorted(set(blockers)),
    }


def capture_round(
    db: Path,
    *,
    station_ids: Sequence[str] | None = None,
    station_id: str | None = None,
    as_of_utc: str | None = None,
    service_probe: Any = _service_pids,
) -> dict[str, Any]:
    if station_id is not None:
        if station_ids is not None:
            raise RuntimeError("resident_acceptance_station_scope_ambiguous")
        station_ids = (station_id,)
    scope = normalize_station_scope(station_ids)
    decision_clock = str(as_of_utc or _now())
    database = db.expanduser().resolve()
    if not database.is_file() or database.is_symlink():
        raise RuntimeError("resident_acceptance_database_invalid")
    wal = Path(str(database) + "-wal")
    resources = {
        "database_bytes": int(database.stat().st_size),
        "wal_bytes": int(wal.stat().st_size) if wal.is_file() else 0,
        "free_bytes": int(shutil.disk_usage(database.parent).free),
    }
    blockers: list[str] = []
    if resources["database_bytes"] > MAX_DB_BYTES:
        blockers.append("database_size_exceeded")
    if resources["wal_bytes"] > MAX_WAL_BYTES:
        blockers.append("wal_size_exceeded")
    if resources["free_bytes"] < MIN_FREE_BYTES:
        blockers.append("minimum_free_disk_not_met")
    with sqlite3.connect(
        f"file:{database}?mode=ro",
        uri=True,
        timeout=30,
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            blockers.append("quick_check_failed")
        runtime = _probability_evidence_runtime_status(
            conn,
            station_ids=scope,
        )
        scoped_counts, outside_counts = _station_counts(
            conn,
            station_ids=scope,
        )
        present = _table_names(conn)
        try:
            started_rows = load_started_registry_rows(
                conn,
                as_of_utc=decision_clock,
            )
        except ContractError as exc:
            started_rows = []
            blockers.append(f"started_registry_invalid:{exc}")
        registry_started = tuple(sorted({
            str(row.get("station_id") or "").strip().upper()
            for row in started_rows
            if str(row.get("station_id") or "").strip().upper() in scope
        }))
        not_started = tuple(sorted(set(scope) - set(registry_started)))
        started_event_dates: dict[str, tuple[str, ...]] = {
            station: tuple(sorted({
                str(row.get("event_date") or "").strip()
                for row in started_rows
                if (
                    str(row.get("station_id") or "").strip().upper()
                    == station
                    and str(row.get("event_date") or "").strip()
                )
            }))
            for station in registry_started
        }
        forbidden = _forbidden_execution_counts(conn)
        station_results: dict[str, dict[str, Any]] = {}
        materialized_started: list[str] = []
        candidate_accumulation: list[str] = []
        for station in scope:
            if station not in registry_started:
                station_results[station] = {
                    "state": "NOT_STARTED_NO_MATERIALIZATION_REQUIRED",
                    "started": False,
                    "registry_started": False,
                    "core_chain": None,
                    "durable_ssot_replay": None,
                    "durable_ssot_replay_error": None,
                }
                continue
            event_dates = started_event_dates.get(station, ())
            if not event_dates:
                blockers.append(f"{station}:started_event_date_missing")
                station_results[station] = {
                    "state": "BLOCKED_FAIL_CLOSED",
                    "started": False,
                    "registry_started": True,
                    "blockers": ["started_event_date_missing"],
                }
                continue
            placeholders = ",".join("?" for _ in event_dates)
            p0_materialized = conn.execute(
                "SELECT 1 FROM basket_probability_envelope "
                "WHERE station_id=? AND event_date IN ("
                + placeholders
                + ") AND stage='P0' AND state='COMMITTED' LIMIT 1",
                (station, *event_dates),
            ).fetchone() is not None
            if not p0_materialized:
                eligibility = _formal_p0_scope_eligibility(
                    conn,
                    station_id=station,
                    registry_rows=started_rows,
                    as_of_utc=decision_clock,
                )
                if eligibility["eligible"] is True:
                    blocker = "formal_two_model_scope_missing_p0"
                    blockers.append(f"{station}:{blocker}")
                    station_results[station] = {
                        "state": "BLOCKED_FAIL_CLOSED",
                        "started": False,
                        "registry_started": True,
                        "candidate_only": False,
                        "formal_p0_eligibility": eligibility,
                        "blockers": [blocker],
                    }
                else:
                    candidate_accumulation.append(station)
                    station_results[station] = {
                        "state": "CANDIDATE_ACCUMULATION_PENDING",
                        "started": False,
                        "registry_started": True,
                        "candidate_only": True,
                        "formal_p0_eligibility": eligibility,
                        "core_chain": None,
                        "durable_ssot_replay": None,
                        "durable_ssot_replay_error": None,
                        "production_no_live": True,
                        "calls_order_gateway": False,
                        "live_submit_authorized": False,
                        "orders_submitted": 0,
                    }
                continue
            materialized_started.append(station)
            station_runtime = _probability_evidence_runtime_status(
                conn,
                station_ids=(station,),
            )
            probability_authority = _probability_authority(
                conn,
                station_id=station,
                event_dates=started_event_dates.get(station, ()),
            )
            core_rows, core_rows_truncated = (
                _latest_core_chain_rows(conn, station_id=station)
                if "auto_trade_decisions" in present
                else ([], False)
            )
            core_chain = _validate_core_chain_rows(core_rows)
            core_chain["history_truncated"] = core_rows_truncated
            latest_fact_day = conn.execute(
                "SELECT event_date FROM basket_policy_training_fact "
                "WHERE station_id=? ORDER BY event_date DESC,rowid DESC LIMIT 1",
                (station,),
            ).fetchone()
            durable_replay: dict[str, Any] | None = None
            durable_replay_error: str | None = None
            if latest_fact_day is None:
                durable_replay_error = (
                    "durable_replay_training_facts_missing"
                )
            else:
                try:
                    durable_replay = replay_durable_target_chain(
                        conn,
                        station_id=station,
                        event_date=str(latest_fact_day[0]),
                    )
                except ContractError as exc:
                    durable_replay_error = str(exc)
            station_blockers: list[str] = []
            candidate_total = sum(
                int(value)
                for value in station_runtime.get(
                    "candidate_artifact_counts", {}
                ).values()
            )
            fallback = station_runtime.get("fallback_proof_counts") or {}
            if station_runtime.get("candidate_activation_count") != 0:
                station_blockers.append("candidate_activation_not_zero")
            if candidate_total < 1:
                station_blockers.append("candidate_artifact_missing")
            if (
                int(station_runtime.get("candidate_rejected_count") or 0)
                != candidate_total
            ):
                station_blockers.append("candidate_rejection_incomplete")
            if int(fallback.get("P0_HISTORICAL_TILT") or 0) < 1:
                station_blockers.append("theta_zero_fallback_not_consumed")
            if int(fallback.get("METAR_TRAJECTORY_UPDATE") or 0) < 1:
                station_blockers.append(
                    "identity_likelihood_fallback_not_consumed"
                )
            if station_runtime.get("fallback_contract_failures"):
                station_blockers.append("fallback_contract_invalid")
            if probability_authority.get("state") != "PASS":
                station_blockers.extend(
                    probability_authority.get("blockers") or [
                        "probability_authority_missing"
                    ]
                )
            if core_chain["validated_count"] < 1:
                station_blockers.append("directional_core_decision_missing")
            station_blockers.extend(core_chain["violations"])
            if (
                durable_replay is None
                or durable_replay.get("state") != "PASS"
            ):
                station_blockers.append(
                    "durable_ssot_replay_failed:"
                    + str(durable_replay_error or "unknown")
                )
            blockers.extend(
                f"{station}:{reason}" for reason in station_blockers
            )
            station_results[station] = {
                "state": (
                    "PASS"
                    if not station_blockers
                    else "BLOCKED_FAIL_CLOSED"
                ),
                "started": True,
                "registry_started": True,
                "candidate_only": False,
                "probability_evidence_runtime": station_runtime,
                "probability_authority": probability_authority,
                "core_chain": core_chain,
                "durable_ssot_replay": durable_replay,
                "durable_ssot_replay_error": durable_replay_error,
                "blockers": sorted(set(station_blockers)),
            }
    if runtime.get("missing_tables") or runtime.get(
        "missing_bridge_tables"
    ):
        blockers.append("probability_evidence_schema_incomplete")
    if int(runtime.get("candidate_activation_count_all") or 0) != 0:
        blockers.append("candidate_activation_not_zero_all_scope")
    if any(value != 0 for value in forbidden.values()):
        blockers.append("forbidden_execution_fact_present")
    identity = {
        "schema": ROUND_SCHEMA,
        "station_scope": {
            "mode": "allowlist",
            "station_ids": list(scope),
            "canonical_default": station_ids is None and station_id is None,
            "max_station_count": MAX_STATION_SCOPE,
        },
        "as_of_utc": decision_clock,
        "registry_started_station_ids": list(registry_started),
        "started_station_ids": sorted(materialized_started),
        "candidate_accumulation_station_ids": sorted(
            candidate_accumulation
        ),
        "not_started_station_ids": list(not_started),
        "database": str(database),
        "services": service_probe(),
        "resources": resources,
        "probability_evidence_runtime": runtime,
        "scoped_station_counts": scoped_counts,
        "outside_scope_station_counts": outside_counts,
        "station_results": station_results,
        "forbidden_execution_counts": forbidden,
        "blockers": sorted(set(blockers)),
        "historical_oos_boundary": {
            "future_data_counts_as_oos": False,
            "future_data_blocks_production_no_live": False,
            "future_data_blocks_formal_promotion_or_live": False,
            "historical_strict_oos_required_for_formal_promotion_or_live": True,
        },
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return {
        **identity,
        "captured_at_utc": _now(),
        "round_sha256": content_sha256(identity),
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
    }


def compare_rounds(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    if first.get("state") != "PASS":
        blockers.append("round_one_not_ready")
    if second.get("state") != "PASS":
        blockers.append("round_two_not_ready")
    if first.get("services") != second.get("services"):
        blockers.append("service_pid_drift")
    first_scope = (first.get("station_scope") or {}).get("station_ids")
    second_scope = (second.get("station_scope") or {}).get("station_ids")
    if first_scope != second_scope or not first_scope:
        blockers.append("station_scope_drift")
    if (
        first.get("outside_scope_station_counts")
        != second.get("outside_scope_station_counts")
    ):
        blockers.append("outside_scope_station_write_detected")
    for round_value in (first, second):
        round_scope = set(
            (round_value.get("station_scope") or {}).get("station_ids") or []
        )
        registry_started = set(
            round_value.get("registry_started_station_ids") or []
        )
        not_started = set(round_value.get("not_started_station_ids") or [])
        materialized = set(round_value.get("started_station_ids") or [])
        candidate = set(
            round_value.get("candidate_accumulation_station_ids") or []
        )
        if (
            registry_started | not_started != round_scope
            or registry_started & not_started
            or not materialized.issubset(registry_started)
            or not candidate.issubset(registry_started)
            or materialized & candidate
        ):
            blockers.append("resident_station_state_partition_invalid")
    first_counts = first.get("scoped_station_counts") or {}
    second_counts = second.get("scoped_station_counts") or {}
    for table, before in first_counts.items():
        after = second_counts.get(table)
        if (
            isinstance(before, int)
            and isinstance(after, int)
            and before >= 0
            and after < before
        ):
            blockers.append(f"scoped_append_only_count_decreased:{table}")
    first_results = first.get("station_results") or {}
    second_results = second.get("station_results") or {}
    for station in sorted(
        set(first.get("started_station_ids") or [])
        & set(second.get("started_station_ids") or [])
    ):
        first_replay = (
            first_results.get(station) or {}
        ).get("durable_ssot_replay")
        second_replay = (
            second_results.get(station) or {}
        ).get("durable_ssot_replay")
        if not isinstance(first_replay, Mapping) or not isinstance(
            second_replay, Mapping
        ):
            blockers.append(f"{station}:durable_ssot_replay_missing")
            continue
        first_rows = first_replay.get("rows")
        second_rows = second_replay.get("rows")
        if (
            first_replay.get("station_id") != second_replay.get("station_id")
            or first_replay.get("event_date") != second_replay.get("event_date")
            or first_replay.get("settlement_timezone")
            != second_replay.get("settlement_timezone")
            or not isinstance(first_rows, list)
            or not isinstance(second_rows, list)
            or len(second_rows) < len(first_rows)
            or second_rows[:len(first_rows)] != first_rows
        ):
            blockers.append(
                f"{station}:durable_ssot_replay_not_append_only_prefix"
            )
        elif len(second_rows) == len(first_rows) and (
            first_replay.get("continuation")
            != second_replay.get("continuation")
        ):
            blockers.append(
                f"{station}:durable_ssot_replay_root_drift_without_append"
            )
    for round_value in (first, second):
        if (
            round_value.get("production_no_live") is not True
            or round_value.get("calls_order_gateway") is not False
            or round_value.get("live_submit_authorized") is not False
            or int(round_value.get("orders_submitted") or 0) != 0
        ):
            blockers.append("no_live_boundary_invalid")
    identity = {
        "schema": SCHEMA,
        "round_one_sha256": first.get("round_sha256"),
        "round_two_sha256": second.get("round_sha256"),
        "station_scope": second.get("station_scope"),
        "registry_started_station_ids": second.get(
            "registry_started_station_ids"
        ),
        "started_station_ids": second.get("started_station_ids"),
        "candidate_accumulation_station_ids": second.get(
            "candidate_accumulation_station_ids"
        ),
        "not_started_station_ids": second.get("not_started_station_ids"),
        "services": second.get("services"),
        "outside_scope_station_counts": second.get(
            "outside_scope_station_counts"
        ),
        "scoped_station_counts": second_counts,
        "station_results": second_results,
        "blockers": sorted(set(blockers)),
        "historical_oos_boundary": {
            "future_data_counts_as_oos": False,
            "future_data_blocks_production_no_live": False,
            "future_data_blocks_formal_promotion_or_live": False,
            "historical_strict_oos_required_for_formal_promotion_or_live": True,
        },
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return {
        **identity,
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
        "content_sha256": content_sha256(identity),
        "rounds": [dict(first), dict(second)],
    }


def migration_round_from_strict(
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Separate M9-06 safety failures from later M9-08 materialization."""

    strict_blockers = [
        str(value) for value in receipt.get("blockers") or ()
    ]
    pending = [
        value
        for value in strict_blockers
        if value.endswith(MATERIALIZATION_PENDING_BLOCKER_SUFFIXES)
    ]
    safety = [
        value for value in strict_blockers if value not in pending
    ]
    identity = {
        key: value
        for key, value in dict(receipt).items()
        if key not in {"round_sha256", "state", "blockers"}
    }
    identity.update({
        "schema": "bayes_basket_target_probability_migration_round_v1",
        "strict_round_schema": receipt.get("schema"),
        "strict_round_sha256": receipt.get("round_sha256"),
        "strict_state": receipt.get("state"),
        "materialization_pending_blockers": sorted(set(pending)),
        "blockers": sorted(set(safety)),
        "historical_oos_boundary": {
            "future_data_counts_as_oos": False,
            "future_data_blocks_production_no_live": False,
            "future_data_blocks_formal_promotion_or_live": False,
            "historical_strict_oos_required_for_formal_promotion_or_live": True,
        },
    })
    return {
        **identity,
        "round_sha256": content_sha256(identity),
        "state": "PASS" if not safety else "BLOCKED_FAIL_CLOSED",
    }


def capture_migration_round(
    db: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    return migration_round_from_strict(capture_round(db, **kwargs))


def compare_migration_rounds(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify resident safety/continuity without requiring M9-08 completion."""

    blockers: list[str] = []
    if first.get("state") != "PASS":
        blockers.append("migration_round_one_not_ready")
    if second.get("state") != "PASS":
        blockers.append("migration_round_two_not_ready")
    if first.get("services") != second.get("services"):
        blockers.append("service_pid_drift")
    first_scope = (first.get("station_scope") or {}).get("station_ids")
    second_scope = (second.get("station_scope") or {}).get("station_ids")
    if first_scope != second_scope or not first_scope:
        blockers.append("station_scope_drift")
    if (
        first.get("outside_scope_station_counts")
        != second.get("outside_scope_station_counts")
    ):
        blockers.append("outside_scope_station_write_detected")
    for index, round_value in enumerate((first, second), start=1):
        round_scope = set(
            (round_value.get("station_scope") or {}).get(
                "station_ids"
            ) or []
        )
        registry_started = set(
            round_value.get("registry_started_station_ids") or []
        )
        not_started = set(
            round_value.get("not_started_station_ids") or []
        )
        materialized = set(
            round_value.get("started_station_ids") or []
        )
        candidate = set(
            round_value.get("candidate_accumulation_station_ids") or []
        )
        if (
            registry_started | not_started != round_scope
            or registry_started & not_started
            or not materialized.issubset(registry_started)
            or not candidate.issubset(registry_started)
            or materialized & candidate
        ):
            blockers.append(
                f"migration_round_{index}_station_partition_invalid"
            )
        runtime = round_value.get("probability_evidence_runtime") or {}
        if runtime.get("missing_tables") or runtime.get(
            "missing_bridge_tables"
        ):
            blockers.append(
                f"migration_round_{index}_probability_schema_incomplete"
            )
        if int(
            runtime.get("candidate_activation_count_all") or 0
        ) != 0:
            blockers.append(
                f"migration_round_{index}_candidate_activation_nonzero"
            )
        forbidden = round_value.get("forbidden_execution_counts") or {}
        if any(int(value or 0) != 0 for value in forbidden.values()):
            blockers.append(
                f"migration_round_{index}_forbidden_execution_present"
            )
        resources = round_value.get("resources") or {}
        if (
            int(resources.get("database_bytes") or 0) > MAX_DB_BYTES
            or int(resources.get("wal_bytes") or 0) > MAX_WAL_BYTES
            or int(resources.get("free_bytes") or 0) < MIN_FREE_BYTES
        ):
            blockers.append(
                f"migration_round_{index}_resource_gate_invalid"
            )
        if (
            round_value.get("production_no_live") is not True
            or round_value.get("calls_order_gateway") is not False
            or round_value.get("live_submit_authorized") is not False
            or int(round_value.get("orders_submitted") or 0) != 0
        ):
            blockers.append(
                f"migration_round_{index}_no_live_boundary_invalid"
            )
    started_both = sorted(
        set(first.get("started_station_ids") or [])
        & set(second.get("started_station_ids") or [])
    )
    if not started_both:
        blockers.append("migration_no_canonical_station_consumed")
    first_counts = first.get("scoped_station_counts") or {}
    second_counts = second.get("scoped_station_counts") or {}
    for table, before in first_counts.items():
        after = second_counts.get(table)
        if (
            isinstance(before, int)
            and isinstance(after, int)
            and before >= 0
            and after < before
        ):
            blockers.append(
                f"migration_scoped_count_decreased:{table}"
            )
    pending = sorted(set(
        str(value)
        for round_value in (first, second)
        for value in (
            round_value.get("materialization_pending_blockers") or []
        )
    ))
    identity = {
        "schema": MIGRATION_SCHEMA,
        "round_one_sha256": first.get("round_sha256"),
        "round_two_sha256": second.get("round_sha256"),
        "station_scope": second.get("station_scope"),
        "started_station_ids": second.get("started_station_ids"),
        "candidate_accumulation_station_ids": second.get(
            "candidate_accumulation_station_ids"
        ),
        "not_started_station_ids": second.get(
            "not_started_station_ids"
        ),
        "services": second.get("services"),
        "outside_scope_station_counts": second.get(
            "outside_scope_station_counts"
        ),
        "scoped_station_counts": second_counts,
        "materialization_pending_blockers": pending,
        "blockers": sorted(set(blockers)),
        "historical_oos_boundary": {
            "future_data_counts_as_oos": False,
            "future_data_blocks_production_no_live": False,
            "future_data_blocks_formal_promotion_or_live": False,
            "historical_strict_oos_required_for_formal_promotion_or_live": True,
        },
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return {
        **identity,
        "state": (
            "PASS" if not blockers else "BLOCKED_FAIL_CLOSED"
        ),
        "content_sha256": content_sha256(identity),
        "rounds": [dict(first), dict(second)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--station-allowlist",
        help=(
            "comma-separated canonical station allowlist; omitted means "
            "the frozen canonical 45-station registry"
        ),
    )
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0.0 <= args.interval_seconds <= 60.0:
        raise SystemExit("interval-seconds must be in [0,60]")
    scope = (
        normalize_station_scope(args.station_allowlist.split(","))
        if args.station_allowlist
        else normalize_station_scope()
    )
    first = capture_round(args.db, station_ids=scope)
    if args.interval_seconds:
        time.sleep(args.interval_seconds)
    second = capture_round(args.db, station_ids=scope)
    result = compare_rounds(first, second)
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "state": result["state"],
        "content_sha256": result["content_sha256"],
        "output": str(args.output.expanduser().resolve()),
    }, sort_keys=True))
    return 0 if result["state"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
