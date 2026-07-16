#!/usr/bin/env python3
"""Read-only evidence audit for the active production sequential-Bayes chain.

This audit is deliberately independent of the retired AWC signal/order replay
chain.  It proves the probability SSOT path:

    active activation -> P0 lineage -> one checkpoint generation
    -> canonical AMSC METAR/SPECI -> StrongObservationUpdate
    -> checkpoint head -> public posterior SSOT

Training/OOS/bridge coverage are never live probability ingress gates.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_activation_contract import (  # noqa: E402
    DEFAULT_PATH as DEFAULT_ACTIVATION_PATH,
    ProductionProbabilityActivationError,
    load_production_probability_activation,
)
from production_probability_runtime_manifest import (  # noqa: E402
    current_retirement_receipt_sha256,
    current_runtime_manifest_sha256,
)
from production_probability_deployment_attestation import (  # noqa: E402
    DEFAULT_PATH as DEFAULT_DEPLOYMENT_ATTESTATION_PATH,
    resolve_retirement_deployment_authorization,
)
from peak_timing_state_runtime import validate_peak_timing_state_contract  # noqa: E402
from production_probability_p0_runtime import build_zuuu_physical_public_slug  # noqa: E402
from production_probability_successor_activation_contract import (  # noqa: E402
    SCHEMA_VERSION as SUCCESSOR_ACTIVATION_SCHEMA,
    SuccessorActivationError,
    load_successor_activation,
)
from production_probability_successor_runtime_manifest import (  # noqa: E402
    audit_successor_runtime_manifest_dependency_closure,
    successor_runtime_manifest_sha256,
)
from reported_x_successor_checkpoint_runtime import (  # noqa: E402
    validate_successor_checkpoint_row,
)


SCHEMA_VERSION = "production_sequential_bayes_chain_audit_v1"
PUBLIC_SSOT_SCHEMA = "sequential_temperature_posterior_ssot_v2"
TARGET_PREFIX = "physical_weather_day_v1:ZUUU:"
SLO_MS = 3000.0
DEFAULT_FAST_LANE_RESULT = ROOT / "data" / "amsc_zuuu_metar_taf" / "p2_canonical_result_latest.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError):
        return default
    return parsed


def _time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _probability_distribution(value: Any) -> tuple[list[tuple[float, float]], list[str]]:
    if isinstance(value, Mapping):
        iterator: Sequence[Any] = [
            {"temperature_c": temperature, "probability": probability}
            for temperature, probability in value.items()
        ]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        iterator = value
    else:
        return [], ["public_physical_distribution_missing"]
    rows: list[tuple[float, float]] = []
    for raw in iterator:
        if not isinstance(raw, Mapping):
            return [], ["public_physical_distribution_row_invalid"]
        try:
            temperature = float(raw.get("temperature_c", raw.get("value_c", raw.get("x"))))
            probability = float(raw.get("probability", raw.get("mass", raw.get("posterior"))))
        except (TypeError, ValueError):
            return [], ["public_physical_distribution_row_invalid"]
        if probability < 0.0:
            return [], ["public_physical_distribution_probability_negative"]
        rows.append((temperature, probability))
    if not rows:
        return [], ["public_physical_distribution_empty"]
    if abs(sum(probability for _temperature, probability in rows) - 1.0) > 1e-8:
        return [], ["public_physical_distribution_not_normalized"]
    return rows, []


def _tables_present(conn: sqlite3.Connection, tables: Sequence[str]) -> list[str]:
    present = {
        _text(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return sorted(table for table in tables if table not in present)


def _load_fast_lane_durable_receipts(path: Path | str) -> dict[str, dict[str, Any]]:
    """Load the latest atomic fast-lane completion receipt by canonical ID.

    Checkpoint timestamps are logical transaction timestamps.  They are safe
    for lineage ordering but can precede the actual durable commit.  The
    supervised fast-lane receipt records completion after Transaction B has
    returned and is therefore the conservative live SLO measurement when the
    event identity matches.
    """

    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    root_payload = _mapping(payload)
    probability = _mapping(root_payload.get("probability"))
    fast_lane = _mapping(root_payload.get("fast_lane"))
    root_finished_at = _text(fast_lane.get("finished_at"))
    receipts: dict[str, dict[str, Any]] = {}
    for raw in probability.get("outcomes") or []:
        outcome = _mapping(raw)
        event_id = _text(outcome.get("canonical_observation_event_id"))
        latency = outcome.get("t1_to_ssot_commit_observed_ms")
        finished_at = _text(outcome.get("finished_at")) or root_finished_at
        try:
            parsed_latency = float(latency)
        except (TypeError, ValueError):
            parsed_latency = None
        if event_id and finished_at and (parsed_latency is None or parsed_latency >= 0.0):
            receipts[event_id] = {
                "t1_to_ssot_durable_ms": parsed_latency,
                "durable_finished_at": finished_at,
                "receipt_schema": _text(fast_lane.get("schema")) or _text(probability.get("schema")),
            }
    return receipts


def _peek_activation_schema(path: Path | str) -> str:
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return _text(_mapping(payload).get("schema"))


def _build_reported_x_audit(
    db_path: Path | str,
    *,
    event_date: str,
    activation_path: Path | str,
    root: Path | str,
    now: datetime | None,
    fast_lane_result_path: Path | str,
) -> dict[str, Any]:
    """Audit only the current reported-X v5 production probability epoch."""

    db = Path(db_path).expanduser().resolve()
    base = Path(root).expanduser().resolve()
    day = _text(event_date)[:10]
    target = f"{TARGET_PREFIX}{day}"
    blockers: list[str] = []
    warnings: list[str] = []
    try:
        activation = load_successor_activation(activation_path)
    except (OSError, SuccessorActivationError, ValueError) as exc:
        activation = {}
        blockers.append(f"activation_invalid:{exc}")
    activation_sha = _text(activation.get("activation_contract_sha256"))
    activation_effective_at = _time(activation.get("activation_effective_at"))
    current_runtime_sha = successor_runtime_manifest_sha256(base)
    current_retirement_sha = current_retirement_receipt_sha256(base)
    dependency_closure = audit_successor_runtime_manifest_dependency_closure(base)
    if dependency_closure.get("ok") is not True:
        blockers.append("successor_runtime_manifest_dependency_closure_incomplete")
    if activation:
        if _text(activation.get("runtime_manifest_sha256")) != current_runtime_sha:
            blockers.append("production_probability_runtime_manifest_drift")
        if _text(activation.get("legacy_writer_retirement_receipt_sha256")) != current_retirement_sha:
            blockers.append("production_probability_retirement_receipt_drift")

    durable_receipts = _load_fast_lane_durable_receipts(fast_lane_result_path)
    evidence: dict[str, Any] = {
        "target_identity": target,
        "runtime_manifest_dependency_closure": dependency_closure,
    }
    forward_rows: list[dict[str, Any]] = []
    pending_event_ids: list[str] = []
    physical_rows: list[tuple[float, float]] = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        conn = None
        blockers.append(f"archive_db_read_failed:{exc}")
    if conn is not None:
        try:
            missing = _tables_present(
                conn,
                (
                    "canonical_observation_events",
                    "canonical_observation_event_sources",
                    "production_initial_posterior_lineage",
                    "strong_observation_updates",
                    "posterior_checkpoints",
                    "event_fact_history",
                    "event_fact_latest",
                    "realization_states",
                    "posterior_commit_outbox",
                ),
            )
            blockers.extend(f"required_table_missing:{table}" for table in missing)
            if missing:
                raise RuntimeError("required_tables_missing")

            lineage_rows = conn.execute(
                "SELECT * FROM production_initial_posterior_lineage "
                "WHERE target_identity=? AND activation_contract_sha256=? "
                "ORDER BY created_at,rowid",
                (target, activation_sha),
            ).fetchall() if activation_sha else []
            if len(lineage_rows) != 1:
                blockers.append("active_activation_p0_lineage_not_unique")
                p0_lineage: Mapping[str, Any] = {}
            else:
                p0_lineage = dict(lineage_rows[0])
            p0_id = _text(p0_lineage.get("posterior_checkpoint_id"))
            p0_row = conn.execute(
                "SELECT * FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
                (p0_id,),
            ).fetchone() if p0_id else None
            p0 = dict(p0_row) if p0_row is not None else {}
            if not p0:
                blockers.append("active_activation_p0_checkpoint_missing")
                generation = None
            else:
                generation = int(p0.get("replay_generation") or 0)
                if _text(p0.get("checkpoint_kind")) != "initial":
                    blockers.append("active_p0_checkpoint_kind_not_initial")
                if _text(p0.get("previous_checkpoint_id")):
                    blockers.append("active_p0_previous_checkpoint_must_be_empty")
                validation = validate_successor_checkpoint_row(p0)
                blockers.extend(
                    f"active_p0_checkpoint_invalid:{issue}"
                    for issue in validation.get("issues") or []
                )

            checkpoints = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM posterior_checkpoints WHERE target_identity=? "
                    "AND replay_generation=? AND state='committed' ORDER BY rowid",
                    (target, generation),
                ).fetchall()
            ] if generation is not None else []
            if not checkpoints or _text(checkpoints[0].get("posterior_checkpoint_id")) != p0_id:
                blockers.append("active_checkpoint_generation_does_not_start_at_p0")
            checkpoint_by_event: dict[str, Mapping[str, Any]] = {}
            previous: Mapping[str, Any] = {}
            previous_consumed: list[str] = []
            for index, checkpoint in enumerate(checkpoints):
                checkpoint_id = _text(checkpoint.get("posterior_checkpoint_id"))
                validation = validate_successor_checkpoint_row(checkpoint)
                blockers.extend(
                    f"checkpoint_invalid:{checkpoint_id}:{issue}"
                    for issue in validation.get("issues") or []
                )
                consumed_raw = _json(checkpoint.get("consumed_event_ids_json"), [])
                consumed = [_text(item) for item in consumed_raw if _text(item)] if isinstance(consumed_raw, list) else []
                if index == 0:
                    if consumed:
                        blockers.append("active_p0_consumed_events_not_empty")
                else:
                    if _text(checkpoint.get("previous_checkpoint_id")) != _text(previous.get("posterior_checkpoint_id")):
                        blockers.append(f"checkpoint_chain_predecessor_mismatch:{checkpoint_id}")
                    if consumed == previous_consumed:
                        # A weak forecast replacement legitimately preserves
                        # the canonical sequence and strong-event identity.
                        pass
                    elif consumed[: len(previous_consumed)] != previous_consumed or len(consumed) != len(previous_consumed) + 1:
                        blockers.append(f"checkpoint_consumed_sequence_not_append_or_replace:{checkpoint_id}")
                    else:
                        event_id = _text(checkpoint.get("last_canonical_event_id"))
                        update_id = _text(checkpoint.get("last_strong_update_id"))
                        if not consumed or consumed[-1] != event_id:
                            blockers.append(f"checkpoint_last_event_not_consumed_tail:{checkpoint_id}")
                        update_row = conn.execute(
                            "SELECT * FROM strong_observation_updates WHERE strong_observation_update_id=?",
                            (update_id,),
                        ).fetchone() if update_id else None
                        update = dict(update_row) if update_row is not None else {}
                        if (
                            _text(update.get("canonical_observation_event_id")) != event_id
                            or _text(update.get("previous_checkpoint_id")) != _text(previous.get("posterior_checkpoint_id"))
                            or _text(update.get("target_identity")) != target
                        ):
                            blockers.append(f"strong_update_binding_mismatch:{checkpoint_id}")
                        identity = _mapping(
                            _mapping(_json(update.get("path_auxiliary_json"), {})).get(
                                "strong_likelihood_update_identity"
                            )
                        )
                        if (
                            _text(identity.get("schema")) != "strong_likelihood_cumulative_replacement_identity_v1"
                            or _text(identity.get("likelihood_update_kind")) != "cumulative_evidence_replacement_delta_log_lr_v1"
                            or _text(identity.get("current_cumulative_raw_log_score_sha256")) != _text(update.get("raw_log_score_sha256"))
                        ):
                            blockers.append(f"strong_likelihood_replacement_identity_invalid:{event_id}")
                        checkpoint_by_event[event_id] = checkpoint
                previous = checkpoint
                previous_consumed = consumed
            head = checkpoints[-1] if checkpoints else {}

            accepted_events = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM canonical_observation_events WHERE target_identity=? "
                    "AND station_id='ZUUU' AND selected_source_id='zuuu_metar_raw_amsc' "
                    "AND report_type IN ('METAR','SPECI') AND state='accepted_primary' "
                    "ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id",
                    (target,),
                ).fetchall()
            ]
            consumed_head_raw = _json(head.get("consumed_event_ids_json"), [])
            consumed_head = {
                _text(item) for item in consumed_head_raw if _text(item)
            } if isinstance(consumed_head_raw, list) else set()
            pending_event_ids = [
                _text(event.get("canonical_observation_event_id"))
                for event in accepted_events
                if _text(event.get("canonical_observation_event_id")) not in consumed_head
            ]
            current_time = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
            for event in accepted_events:
                event_id = _text(event.get("canonical_observation_event_id"))
                t1 = _time(event.get("first_actionable_seen_at"))
                if not (t1 and activation_effective_at and t1 >= activation_effective_at):
                    continue
                checkpoint = checkpoint_by_event.get(event_id) or {}
                t3_logical = _time(checkpoint.get("committed_at"))
                logical_ms = (t3_logical - t1).total_seconds() * 1000.0 if t3_logical else None
                receipt = durable_receipts.get(event_id) or {}
                durable_finished = _time(receipt.get("durable_finished_at"))
                explicit_ms = receipt.get("t1_to_ssot_durable_ms")
                if explicit_ms is not None:
                    latency_ms = float(explicit_ms)
                elif durable_finished is not None:
                    latency_ms = (durable_finished - t1).total_seconds() * 1000.0
                else:
                    latency_ms = logical_ms
                state = (
                    "passed"
                    if latency_ms is not None and 0.0 <= latency_ms <= SLO_MS
                    else "failed"
                    if latency_ms is not None
                    else "pending"
                )
                age_ms = (current_time - t1).total_seconds() * 1000.0
                if state == "failed":
                    blockers.append(f"forward_three_second_slo_failed:{event_id}")
                elif state == "pending" and age_ms > SLO_MS:
                    blockers.append(f"forward_canonical_event_uncommitted_after_three_seconds:{event_id}")
                forward_rows.append(
                    {
                        "canonical_observation_event_id": event_id,
                        "report_type": _text(event.get("report_type")),
                        "t1_first_actionable_seen_at": _text(event.get("first_actionable_seen_at")),
                        "t3_ssot_committed_at": _text(receipt.get("durable_finished_at")) or _text(checkpoint.get("committed_at")),
                        "t1_to_t3_ms": round(latency_ms, 3) if latency_ms is not None else None,
                        "db_logical_t1_to_t3_ms": round(logical_ms, 3) if logical_ms is not None else None,
                        "measurement_source": "fast_lane_durable_completion_receipt" if receipt else "db_logical_timestamp_fallback",
                        "state": state,
                    }
                )

            slug = build_zuuu_physical_public_slug(day)
            public_row = conn.execute(
                "SELECT h.* FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id "
                "WHERE l.slug=? AND l.event_date=?",
                (slug, day),
            ).fetchone()
            public_fact = dict(public_row) if public_row is not None else {}
            public_ssot = _mapping(
                _mapping(_json(public_fact.get("fact_json"), {})).get("posterior_ssot_fact")
            )
            if _text(public_ssot.get("schema")) != _text(activation.get("public_ssot_schema")):
                blockers.append("public_latest_ssot_schema_invalid")
            for left, right, label in (
                (public_ssot.get("posterior_checkpoint_id"), head.get("posterior_checkpoint_id"), "checkpoint"),
                (public_ssot.get("posterior_run_id"), head.get("posterior_run_id"), "posterior_run"),
                (public_ssot.get("last_canonical_event_id"), head.get("last_canonical_event_id"), "last_event"),
                (public_ssot.get("last_strong_update_id"), head.get("last_strong_update_id"), "last_update"),
            ):
                if _text(left) != _text(right):
                    blockers.append(f"public_latest_binding_mismatch:{label}")
            runtime_identity = _mapping(public_ssot.get("probability_runtime_identity"))
            if _text(runtime_identity.get("activation_contract_sha256")) != activation_sha:
                blockers.append("public_latest_activation_binding_mismatch")
            physical_rows, distribution_issues = _probability_distribution(
                public_ssot.get("reported_physical_x_distribution")
            )
            blockers.extend(distribution_issues)
            peak_timing, peak_timing_issues = validate_peak_timing_state_contract(
                public_ssot.get("peak_timing_state"),
                expected_event_id=_text(head.get("last_canonical_event_id")),
                expected_update_id=_text(head.get("last_strong_update_id")),
            )
            blockers.extend(peak_timing_issues)
            market_available = bool(public_ssot.get("market_bucket_probabilities"))
            market_na_reason = _text(public_ssot.get("market_bucket_probabilities_na_reason"))
            realization_count = int(conn.execute(
                "SELECT COUNT(*) FROM realization_states WHERE posterior_run_id=?",
                (_text(head.get("posterior_run_id")),),
            ).fetchone()[0] or 0) if head else 0
            outbox_count = int(conn.execute(
                "SELECT COUNT(*) FROM posterior_commit_outbox WHERE posterior_run_id=?",
                (_text(head.get("posterior_run_id")),),
            ).fetchone()[0] or 0) if head else 0
            if not market_available:
                if not market_na_reason:
                    blockers.append("physical_only_public_ssot_market_na_reason_missing")
                if realization_count or outbox_count:
                    blockers.append("physical_only_public_ssot_created_market_execution_state")
            evidence.update(
                {
                    "active_activation_p0_lineage_count": len(lineage_rows),
                    "active_p0_checkpoint_id": p0_id,
                    "active_replay_generation": generation,
                    "active_generation_checkpoint_count": len(checkpoints),
                    "active_head_checkpoint_id": _text(head.get("posterior_checkpoint_id")),
                    "active_head_posterior_run_id": _text(head.get("posterior_run_id")),
                    "accepted_amsc_event_count": len(accepted_events),
                    "active_head_consumed_event_count": len(consumed_head),
                    "pending_canonical_event_ids": pending_event_ids,
                    "public_fact_id": public_fact.get("id"),
                    "public_fact_snapshot_id": _text(public_fact.get("fact_snapshot_id")),
                    "public_ssot_state": _text(public_ssot.get("state")),
                    "physical_distribution_count": len(physical_rows),
                    "physical_distribution_probability_sum": sum(row[1] for row in physical_rows),
                    "peak_timing_state": peak_timing,
                    "market_projection_available": market_available,
                    "market_projection_na_reason": market_na_reason,
                    "realization_state_count_for_head": realization_count,
                    "outbox_count_for_head": outbox_count,
                }
            )
        except RuntimeError as exc:
            if _text(exc) != "required_tables_missing":
                blockers.append(f"chain_audit_runtime_error:{exc}")
        except (sqlite3.Error, TypeError, ValueError) as exc:
            blockers.append(f"chain_audit_runtime_error:{type(exc).__name__}:{exc}")
        finally:
            conn.close()

    if pending_event_ids and not any("uncommitted_after_three_seconds" in item for item in blockers):
        warnings.append("canonical_events_pending_within_live_processing_window")
    forward_state = (
        "pending_next_post_activation_canonical_event"
        if not forward_rows
        else "passed"
        if all(row.get("state") == "passed" for row in forward_rows)
        else "failed"
    )
    blockers = sorted(set(blockers))
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "active_chain_verified" if not blockers and forward_state == "passed" else "active_chain_verified_forward_slo_pending" if not blockers else "blocked",
        "db": str(db),
        "event_date": day,
        "activation": {
            "activation_effective_at": _text(activation.get("activation_effective_at")),
            "activation_contract_sha256": activation_sha,
            "authorized_runtime_manifest_sha256": _text(activation.get("runtime_manifest_sha256")),
            "current_runtime_manifest_sha256": current_runtime_sha,
            "authorized_retirement_receipt_sha256": _text(activation.get("legacy_writer_retirement_receipt_sha256")),
            "current_retirement_receipt_sha256": current_retirement_sha,
            "retirement_deployment_authorization": {
                "authorized": bool(activation),
                "source": "reported_x_successor_activation_contract",
                "approved_at": _text(activation.get("activation_effective_at")),
            },
        },
        "evidence": evidence,
        "forward_three_second_slo": {
            "state": forward_state,
            "threshold_ms": int(SLO_MS),
            "event_count": len(forward_rows),
            "events": forward_rows,
        },
        "current_deployment_forward_three_second_slo": {
            "evaluation_started_at": _text(activation.get("activation_effective_at")),
            "state": forward_state,
            "threshold_ms": int(SLO_MS),
            "event_count": len(forward_rows),
            "events": forward_rows,
        },
        "blockers": blockers,
        "warnings": sorted(set(warnings)),
        "boundaries": {
            "read_only": True,
            "places_orders": False,
            "training_samples_block_live": False,
            "strict_asof_oos_block_live": False,
            "bridge_blocks_physical_x": False,
            "legacy_awc_signal_replay_used": False,
            "probability_ssot_is_unique": True,
            "reported_x_successor_only": True,
            "peak_timing_is_public_ssot_even_without_market_bridge": True,
        },
    }


def build_audit(
    db_path: Path | str,
    *,
    event_date: str,
    activation_path: Path | str = DEFAULT_ACTIVATION_PATH,
    root: Path | str = ROOT,
    now: datetime | None = None,
    deployment_attestation_path: Path | str = DEFAULT_DEPLOYMENT_ATTESTATION_PATH,
    fast_lane_result_path: Path | str = DEFAULT_FAST_LANE_RESULT,
) -> dict[str, Any]:
    if _peek_activation_schema(activation_path) == SUCCESSOR_ACTIVATION_SCHEMA:
        return _build_reported_x_audit(
            db_path,
            event_date=event_date,
            activation_path=activation_path,
            root=root,
            now=now,
            fast_lane_result_path=fast_lane_result_path,
        )
    db = Path(db_path).expanduser().resolve()
    base = Path(root).expanduser().resolve()
    day = _text(event_date)[:10]
    target = f"{TARGET_PREFIX}{day}"
    blockers: list[str] = []
    warnings: list[str] = []

    try:
        activation = load_production_probability_activation(activation_path)
    except ProductionProbabilityActivationError as exc:
        activation = {}
        blockers.append(f"activation_invalid:{exc}")
    activation_sha = _text(activation.get("activation_contract_sha256"))
    activation_effective_at = _time(activation.get("activation_effective_at"))
    current_runtime_sha = current_runtime_manifest_sha256(base)
    current_retirement_sha = current_retirement_receipt_sha256(base)
    retirement_authorization = resolve_retirement_deployment_authorization(
        activation=activation,
        current_runtime_manifest_sha256=current_runtime_sha,
        current_retirement_receipt_sha256=current_retirement_sha,
        path=deployment_attestation_path,
    )
    if activation:
        if _text(activation.get("runtime_manifest_sha256")) != current_runtime_sha:
            blockers.append("production_probability_runtime_manifest_drift")
        if retirement_authorization.get("authorized") is not True:
            blockers.append("production_probability_retirement_receipt_drift")

    required_tables = (
        "canonical_observation_events",
        "canonical_observation_event_sources",
        "production_initial_posterior_lineage",
        "strong_observation_updates",
        "weak_forecast_evidence_snapshots",
        "weak_forecast_revision_updates",
        "posterior_checkpoints",
        "event_fact_history",
        "event_fact_latest",
        "realization_states",
        "posterior_commit_outbox",
        "posterior_outbox_execution_contracts",
    )
    evidence: dict[str, Any] = {}
    forward_rows: list[dict[str, Any]] = []
    all_forward_rows: list[dict[str, Any]] = []
    pending_event_ids: list[str] = []
    physical_rows: list[tuple[float, float]] = []
    active_generation: int | None = None
    head: Mapping[str, Any] = {}
    public_ssot: Mapping[str, Any] = {}
    durable_receipts = _load_fast_lane_durable_receipts(fast_lane_result_path)

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        blockers.append(f"archive_db_read_failed:{exc}")
        conn = None
    if conn is not None:
        conn.row_factory = sqlite3.Row
        try:
            missing_tables = _tables_present(conn, required_tables)
            if missing_tables:
                blockers.extend(f"required_table_missing:{table}" for table in missing_tables)
            if missing_tables:
                raise RuntimeError("required_tables_missing")

            lineage_rows = conn.execute(
                """SELECT * FROM production_initial_posterior_lineage
                   WHERE target_identity=? AND activation_contract_sha256=?
                   ORDER BY created_at,rowid""",
                (target, activation_sha),
            ).fetchall() if activation_sha else []
            if len(lineage_rows) != 1:
                blockers.append("active_activation_p0_lineage_not_unique")
                p0_lineage: Mapping[str, Any] = {}
            else:
                p0_lineage = dict(lineage_rows[0])
            p0_id = _text(p0_lineage.get("posterior_checkpoint_id"))
            p0 = conn.execute(
                "SELECT * FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
                (p0_id,),
            ).fetchone() if p0_id else None
            p0_map = dict(p0) if p0 is not None else {}
            if not p0_map:
                blockers.append("active_activation_p0_checkpoint_missing")
            else:
                active_generation = int(p0_map.get("replay_generation") or 0)
                if _text(p0_map.get("target_identity")) != target:
                    blockers.append("active_p0_target_identity_mismatch")
                if _text(p0_map.get("checkpoint_kind")) != "initial":
                    blockers.append("active_p0_checkpoint_kind_not_initial")
                if _text(p0_map.get("previous_checkpoint_id")):
                    blockers.append("active_p0_previous_checkpoint_must_be_empty")
                bindings = (
                    (p0_lineage.get("posterior_run_id"), p0_map.get("posterior_run_id"), "p0_posterior_run"),
                    (p0_lineage.get("forecast_evidence_snapshot_id"), p0_map.get("forecast_evidence_snapshot_id"), "p0_forecast"),
                    (p0_lineage.get("prior_context_id"), p0_map.get("prior_context_id"), "p0_prior"),
                    (p0_lineage.get("resolution_contract_id"), p0_map.get("resolution_contract_id"), "p0_resolution"),
                    (p0_lineage.get("strong_lr_artifact_sha256"), p0_map.get("model_artifact_sha256"), "p0_strong_artifact"),
                    (p0_lineage.get("prior_model_artifact_sha256"), activation.get("prior_model_artifact_sha256"), "activation_prior_artifact"),
                    (p0_lineage.get("weak_likelihood_artifact_sha256"), activation.get("weak_likelihood_artifact_sha256"), "activation_weak_artifact"),
                    (p0_lineage.get("strong_lr_artifact_sha256"), activation.get("strong_lr_artifact_sha256"), "activation_strong_artifact"),
                )
                blockers.extend(
                    f"active_p0_binding_mismatch:{label}"
                    for left, right, label in bindings
                    if _text(left) != _text(right)
                )

            checkpoints = [
                dict(row) for row in conn.execute(
                    """SELECT * FROM posterior_checkpoints
                       WHERE target_identity=? AND replay_generation=? AND state='committed'
                       ORDER BY rowid""",
                    (target, active_generation),
                ).fetchall()
            ] if active_generation is not None else []
            if not checkpoints or _text(checkpoints[0].get("posterior_checkpoint_id")) != p0_id:
                blockers.append("active_checkpoint_generation_does_not_start_at_p0")
            prior_checkpoint: Mapping[str, Any] = {}
            consumed_previous: list[str] = []
            checkpoint_by_event: dict[str, Mapping[str, Any]] = {}
            for index, checkpoint in enumerate(checkpoints):
                checkpoint_id = _text(checkpoint.get("posterior_checkpoint_id"))
                consumed = _json(checkpoint.get("consumed_event_ids_json"), None)
                if not isinstance(consumed, list):
                    blockers.append(f"checkpoint_consumed_events_invalid:{checkpoint_id}")
                    consumed = []
                consumed = [_text(value) for value in consumed if _text(value)]
                if len(consumed) != len(set(consumed)):
                    blockers.append(f"checkpoint_consumed_events_duplicate:{checkpoint_id}")
                if index == 0:
                    if consumed:
                        blockers.append("active_p0_consumed_events_not_empty")
                else:
                    if _text(checkpoint.get("previous_checkpoint_id")) != _text(
                        prior_checkpoint.get("posterior_checkpoint_id")
                    ):
                        blockers.append(f"checkpoint_chain_predecessor_mismatch:{checkpoint_id}")
                    weak_row = conn.execute(
                        "SELECT * FROM weak_forecast_revision_updates WHERE successor_checkpoint_id=?",
                        (checkpoint_id,),
                    ).fetchone()
                    weak_update = dict(weak_row) if weak_row is not None else {}
                    if weak_update:
                        if consumed != consumed_previous:
                            blockers.append(f"weak_revision_consumed_canonical_sequence_changed:{checkpoint_id}")
                        if (
                            _text(checkpoint.get("last_canonical_event_id")) != _text(prior_checkpoint.get("last_canonical_event_id"))
                            or _text(checkpoint.get("last_strong_update_id")) != _text(prior_checkpoint.get("last_strong_update_id"))
                        ):
                            blockers.append(f"weak_revision_strong_lineage_changed:{checkpoint_id}")
                        weak_bindings = (
                            (weak_update.get("target_identity"), target, "target"),
                            (weak_update.get("previous_checkpoint_id"), prior_checkpoint.get("posterior_checkpoint_id"), "previous_checkpoint"),
                            (weak_update.get("previous_posterior_run_id"), prior_checkpoint.get("posterior_run_id"), "previous_run"),
                            (weak_update.get("previous_posterior_sha256"), prior_checkpoint.get("joint_posterior_sha256"), "previous_posterior"),
                            (weak_update.get("previous_forecast_evidence_snapshot_id"), prior_checkpoint.get("forecast_evidence_snapshot_id"), "previous_forecast"),
                            (weak_update.get("revised_forecast_evidence_snapshot_id"), checkpoint.get("forecast_evidence_snapshot_id"), "revised_forecast"),
                            (weak_update.get("weak_likelihood_artifact_sha256"), activation.get("weak_likelihood_artifact_sha256"), "weak_artifact"),
                        )
                        blockers.extend(
                            f"weak_revision_binding_mismatch:{checkpoint_id}:{label}"
                            for left, right, label in weak_bindings if _text(left) != _text(right)
                        )
                    else:
                        if consumed[: len(consumed_previous)] != consumed_previous or len(consumed) != len(consumed_previous) + 1:
                            blockers.append(f"checkpoint_consumed_sequence_not_append_one:{checkpoint_id}")
                        last_event_id = _text(checkpoint.get("last_canonical_event_id"))
                        update_id = _text(checkpoint.get("last_strong_update_id"))
                        if not consumed or last_event_id != consumed[-1]:
                            blockers.append(f"checkpoint_last_event_not_consumed_tail:{checkpoint_id}")
                        update = conn.execute(
                            "SELECT * FROM strong_observation_updates WHERE strong_observation_update_id=?",
                            (update_id,),
                        ).fetchone() if update_id else None
                        update_map = dict(update) if update is not None else {}
                        update_bindings = (
                            (update_map.get("target_identity"), target, "target"),
                            (update_map.get("canonical_observation_event_id"), last_event_id, "event"),
                            (update_map.get("previous_checkpoint_id"), prior_checkpoint.get("posterior_checkpoint_id"), "previous_checkpoint"),
                            (update_map.get("previous_posterior_run_id"), prior_checkpoint.get("posterior_run_id"), "previous_run"),
                            (update_map.get("forecast_evidence_snapshot_id"), prior_checkpoint.get("forecast_evidence_snapshot_id"), "forecast"),
                            (update_map.get("prior_context_id"), p0_map.get("prior_context_id"), "prior"),
                            (update_map.get("model_artifact_sha256"), p0_map.get("model_artifact_sha256"), "strong_artifact"),
                        )
                        if not update_map:
                            blockers.append(f"checkpoint_strong_update_missing:{checkpoint_id}")
                        blockers.extend(
                            f"strong_update_binding_mismatch:{checkpoint_id}:{label}"
                            for left, right, label in update_bindings if _text(left) != _text(right)
                        )
                        canonical = conn.execute(
                            "SELECT * FROM canonical_observation_events WHERE canonical_observation_event_id=?",
                            (last_event_id,),
                        ).fetchone() if last_event_id else None
                        canonical_map = dict(canonical) if canonical is not None else {}
                        if (
                            _text(canonical_map.get("target_identity")) != target
                            or _text(canonical_map.get("station_id")) != "ZUUU"
                            or _text(canonical_map.get("selected_source_id")) != "zuuu_metar_raw_amsc"
                            or _text(canonical_map.get("report_type")) not in {"METAR", "SPECI"}
                            or _text(canonical_map.get("state")) != "accepted_primary"
                        ):
                            blockers.append(f"canonical_event_not_accepted_primary_amsc:{last_event_id}")
                        selected_count = int(conn.execute(
                            """SELECT COUNT(*) FROM canonical_observation_event_sources
                               WHERE canonical_observation_event_id=? AND source_id='zuuu_metar_raw_amsc'
                                 AND relation='selected'""",
                            (last_event_id,),
                        ).fetchone()[0] or 0)
                        if selected_count != 1:
                            blockers.append(f"canonical_event_selected_source_not_unique:{last_event_id}")
                        path_auxiliary = _json(update_map.get("path_auxiliary_json"), {})
                        termination = _mapping(_mapping(path_auxiliary).get("termination_risk"))
                        remaining = _mapping(path_auxiliary).get("remaining_warming_marginal")
                        if (
                            _text(termination.get("schema")) != "termination_risk_marginal_v1"
                            or _text(termination.get("state")) != "available"
                            or _text(termination.get("canonical_observation_event_id")) != last_event_id
                            or termination.get("same_strong_update_marginal") is not True
                        ):
                            blockers.append(f"termination_risk_contract_invalid:{last_event_id}")
                        if not isinstance(remaining, list) or not remaining:
                            blockers.append(f"remaining_warming_marginal_missing:{last_event_id}")
                        else:
                            remaining_values = [
                                float(row.get("remaining_warming_c"))
                                for row in remaining
                                if isinstance(row, Mapping) and row.get("remaining_warming_c") is not None
                            ]
                            if not remaining_values or min(remaining_values) >= 0.0 or max(remaining_values) <= 0.0:
                                blockers.append(f"remaining_warming_does_not_cover_warming_and_cooling:{last_event_id}")
                        checkpoint_by_event[last_event_id] = checkpoint
                prior_checkpoint = checkpoint
                consumed_previous = consumed
            head = checkpoints[-1] if checkpoints else {}
            if head:
                head_snapshot = conn.execute(
                    """SELECT target_identity,weak_likelihood_artifact_sha256,state
                       FROM weak_forecast_evidence_snapshots
                       WHERE forecast_evidence_snapshot_id=?""",
                    (_text(head.get("forecast_evidence_snapshot_id")),),
                ).fetchone()
                if (
                    head_snapshot is None
                    or _text(head_snapshot[0]) != target
                    or _text(head_snapshot[1]) != _text(activation.get("weak_likelihood_artifact_sha256"))
                    or _text(head_snapshot[2]) != "active"
                ):
                    blockers.append("active_head_weak_forecast_snapshot_invalid")

            accepted_events = [
                dict(row) for row in conn.execute(
                    """SELECT * FROM canonical_observation_events
                       WHERE target_identity=? AND station_id='ZUUU'
                         AND selected_source_id='zuuu_metar_raw_amsc'
                         AND report_type IN ('METAR','SPECI') AND state='accepted_primary'
                       ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id""",
                    (target,),
                ).fetchall()
            ]
            consumed_head = _json(head.get("consumed_event_ids_json"), [])
            consumed_head_ids = {_text(value) for value in consumed_head if _text(value)} if isinstance(consumed_head, list) else set()
            accepted_ids = [_text(row.get("canonical_observation_event_id")) for row in accepted_events]
            pending_event_ids = [event_id for event_id in accepted_ids if event_id not in consumed_head_ids]
            current_time = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
            deployment_effective_at = activation_effective_at
            if _text(retirement_authorization.get("source")) == "operational_deployment_attestation":
                deployment_effective_at = _time(retirement_authorization.get("approved_at")) or activation_effective_at
            for event in accepted_events:
                event_id = _text(event.get("canonical_observation_event_id"))
                t1 = _time(event.get("first_actionable_seen_at"))
                is_forward = bool(t1 and activation_effective_at and t1 >= activation_effective_at)
                if not is_forward:
                    continue
                checkpoint = checkpoint_by_event.get(event_id)
                t3 = _time(_mapping(checkpoint).get("committed_at"))
                logical_latency_ms = (t3 - t1).total_seconds() * 1000.0 if t1 and t3 else None
                durable_receipt = durable_receipts.get(event_id) or {}
                latency_ms = durable_receipt.get("t1_to_ssot_durable_ms", logical_latency_ms)
                state = "passed" if latency_ms is not None and 0.0 <= latency_ms <= SLO_MS else "failed" if latency_ms is not None else "pending"
                age_ms = (current_time - t1).total_seconds() * 1000.0 if t1 else None
                row = {
                    "canonical_observation_event_id": event_id,
                    "report_type": _text(event.get("report_type")),
                    "t1_first_actionable_seen_at": _text(event.get("first_actionable_seen_at")),
                    "t3_ssot_committed_at": _text(durable_receipt.get("durable_finished_at")) or _text(_mapping(checkpoint).get("committed_at")),
                    "t1_to_t3_ms": round(latency_ms, 3) if latency_ms is not None else None,
                    "db_logical_committed_at": _text(_mapping(checkpoint).get("committed_at")),
                    "db_logical_t1_to_t3_ms": round(logical_latency_ms, 3) if logical_latency_ms is not None else None,
                    "measurement_source": (
                        "fast_lane_durable_completion_receipt"
                        if durable_receipt
                        else "db_logical_timestamp_fallback"
                    ),
                    "state": state,
                }
                all_forward_rows.append(row)
                is_current_deployment = bool(t1 and deployment_effective_at and t1 >= deployment_effective_at)
                if not is_current_deployment:
                    continue
                if state == "failed":
                    blockers.append(f"forward_three_second_slo_failed:{event_id}")
                elif state == "pending" and age_ms is not None and age_ms > SLO_MS:
                    blockers.append(f"forward_canonical_event_uncommitted_after_three_seconds:{event_id}")
                forward_rows.append(row)

            latest_rows = conn.execute(
                """SELECT h.*,l.fact_snapshot_id AS latest_fact_snapshot_id
                   FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id
                   WHERE h.event_date=? AND lower(h.slug) LIKE 'highest-temperature-in-chengdu-on-%'""",
                (day,),
            ).fetchall()
            if len(latest_rows) != 1:
                blockers.append("public_latest_chengdu_fact_not_unique")
                public_fact: Mapping[str, Any] = {}
            else:
                public_fact = dict(latest_rows[0])
            public_payload = _json(public_fact.get("fact_json"), {})
            public_ssot = _mapping(_mapping(public_payload).get("posterior_ssot_fact"))
            if _text(public_ssot.get("schema")) != PUBLIC_SSOT_SCHEMA:
                blockers.append("public_latest_ssot_schema_invalid")
            if _text(public_ssot.get("writer_mode")) != "production":
                blockers.append("public_latest_not_production_writer")
            public_bindings = (
                (public_ssot.get("posterior_checkpoint_id"), head.get("posterior_checkpoint_id"), "checkpoint"),
                (public_ssot.get("posterior_run_id"), head.get("posterior_run_id"), "posterior_run"),
                (public_ssot.get("last_canonical_event_id"), head.get("last_canonical_event_id"), "last_event"),
                (public_ssot.get("last_strong_update_id"), head.get("last_strong_update_id"), "last_update"),
                (public_fact.get("posterior_run_id"), head.get("posterior_run_id"), "fact_posterior_run"),
                (public_fact.get("fact_snapshot_id"), public_ssot.get("fact_snapshot_id"), "fact_snapshot"),
            )
            blockers.extend(
                f"public_latest_binding_mismatch:{label}"
                for left, right, label in public_bindings
                if _text(left) != _text(right)
            )
            runtime_identity = _mapping(public_ssot.get("probability_runtime_identity"))
            if _text(runtime_identity.get("activation_contract_sha256")) != activation_sha:
                blockers.append("public_latest_activation_binding_mismatch")
            physical_rows, distribution_issues = _probability_distribution(
                public_ssot.get("physical_high_distribution")
            )
            blockers.extend(distribution_issues)
            peak_timing_evidence, peak_timing_issues = validate_peak_timing_state_contract(
                public_ssot.get("peak_timing_state"),
                expected_event_id=_text(head.get("last_canonical_event_id")),
                expected_update_id=_text(head.get("last_strong_update_id")),
            )
            blockers.extend(peak_timing_issues)
            market_rows = public_ssot.get("market_bucket_probabilities")
            market_available = bool(market_rows)
            market_na_reason = _text(public_ssot.get("market_bucket_probabilities_na_reason"))
            realization_count = int(conn.execute(
                "SELECT COUNT(*) FROM realization_states WHERE posterior_run_id=?",
                (_text(head.get("posterior_run_id")),),
            ).fetchone()[0] or 0) if head else 0
            outbox_count = int(conn.execute(
                "SELECT COUNT(*) FROM posterior_commit_outbox WHERE posterior_run_id=?",
                (_text(head.get("posterior_run_id")),),
            ).fetchone()[0] or 0) if head else 0
            if not market_available:
                if not market_na_reason:
                    blockers.append("physical_only_public_ssot_market_na_reason_missing")
                if realization_count or outbox_count:
                    blockers.append("physical_only_public_ssot_created_market_execution_state")

            evidence = {
                "target_identity": target,
                "active_activation_p0_lineage_count": len(lineage_rows),
                "active_p0_checkpoint_id": p0_id,
                "active_replay_generation": active_generation,
                "active_generation_checkpoint_count": len(checkpoints),
                "active_head_checkpoint_id": _text(head.get("posterior_checkpoint_id")),
                "active_head_posterior_run_id": _text(head.get("posterior_run_id")),
                "accepted_amsc_event_count": len(accepted_events),
                "active_head_consumed_event_count": len(consumed_head_ids),
                "pending_canonical_event_ids": pending_event_ids,
                "public_fact_id": public_fact.get("id"),
                "public_fact_snapshot_id": _text(public_fact.get("fact_snapshot_id")),
                "public_ssot_state": _text(public_ssot.get("state")),
                "physical_distribution_count": len(physical_rows),
                "physical_distribution_probability_sum": sum(probability for _temperature, probability in physical_rows),
                "peak_timing_state": peak_timing_evidence,
                "market_projection_available": market_available,
                "market_projection_na_reason": market_na_reason,
                "realization_state_count_for_head": realization_count,
                "outbox_count_for_head": outbox_count,
            }
        except RuntimeError as exc:
            if _text(exc) != "required_tables_missing":
                blockers.append(f"chain_audit_runtime_error:{exc}")
        except sqlite3.Error as exc:
            blockers.append(f"chain_audit_sql_error:{exc}")
        finally:
            conn.close()

    if not forward_rows:
        forward_state = "pending_next_post_activation_canonical_event"
    elif all(row.get("state") == "passed" for row in forward_rows):
        forward_state = "passed"
    else:
        forward_state = "failed"
    if pending_event_ids and not any("uncommitted_after_three_seconds" in item for item in blockers):
        warnings.append("canonical_events_pending_within_live_processing_window")
    if any(row.get("state") == "failed" for row in all_forward_rows) and not any(
        row.get("state") == "failed" for row in forward_rows
    ):
        warnings.append("historical_pre_current_deployment_slo_failures_preserved")
    blockers = sorted(set(blockers))
    warnings = sorted(set(warnings))
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "active_chain_verified" if not blockers and forward_state == "passed" else "active_chain_verified_forward_slo_pending" if not blockers else "blocked",
        "db": str(db),
        "event_date": day,
        "activation": {
            "activation_effective_at": _text(activation.get("activation_effective_at")),
            "activation_contract_sha256": activation_sha,
            "authorized_runtime_manifest_sha256": _text(activation.get("runtime_manifest_sha256")),
            "current_runtime_manifest_sha256": current_runtime_sha,
            "authorized_retirement_receipt_sha256": _text(activation.get("legacy_writer_retirement_receipt_sha256")),
            "current_retirement_receipt_sha256": current_retirement_sha,
            "retirement_deployment_authorization": retirement_authorization,
        },
        "evidence": evidence,
        "forward_three_second_slo": {
            "state": (
                "pending_next_post_activation_canonical_event" if not all_forward_rows
                else "passed" if all(row.get("state") == "passed" for row in all_forward_rows)
                else "failed"
            ),
            "threshold_ms": int(SLO_MS),
            "event_count": len(all_forward_rows),
            "events": all_forward_rows,
        },
        "current_deployment_forward_three_second_slo": {
            "evaluation_started_at": (
                _text(retirement_authorization.get("approved_at"))
                if _text(retirement_authorization.get("source")) == "operational_deployment_attestation"
                else _text(activation.get("activation_effective_at"))
            ),
            "state": forward_state,
            "threshold_ms": int(SLO_MS),
            "event_count": len(forward_rows),
            "events": forward_rows,
        },
        "blockers": blockers,
        "warnings": warnings,
        "boundaries": {
            "read_only": True,
            "places_orders": False,
            "training_samples_block_live": False,
            "strict_asof_oos_block_live": False,
            "bridge_blocks_physical_x": False,
            "legacy_awc_signal_replay_used": False,
            "probability_ssot_is_unique": True,
            "peak_timing_is_public_ssot_even_without_market_bridge": True,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--activation", default=str(DEFAULT_ACTIVATION_PATH))
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--fast-lane-result", default=str(DEFAULT_FAST_LANE_RESULT))
    args = parser.parse_args(argv)
    result = build_audit(
        args.db,
        event_date=args.event_date,
        activation_path=args.activation,
        root=args.root,
        fast_lane_result_path=args.fast_lane_result,
    )
    if args.summary:
        result = {
            key: result[key]
            for key in (
                "schema", "ok", "state", "event_date", "activation", "evidence",
                "forward_three_second_slo", "current_deployment_forward_three_second_slo",
                "blockers", "warnings", "boundaries",
            )
        }
    print(json.dumps(result, ensure_ascii=False, indent=None if args.summary else 2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
