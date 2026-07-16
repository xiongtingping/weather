#!/usr/bin/env python3
"""Read-only SSOT chain replay audit.

This script reconstructs hard-source signal lineage across facts, decisions,
jobs, and the optional signal_to_order_timing ledger. It never writes to the
database and does not call external services.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return int(default)
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return int(default)

FINAL_TERMINAL_STATES = {"submitted", "blocked", "stale", "skipped", "no_action", "error"}
PRE_SIGNAL_REQUIRED_COMPONENTS = (
    "market_prefetch",
    "market_mapping",
    "orderbook",
    "open_orders",
    "positions",
    "capital_caps",
)
TERMINAL_RANK = {
    "": 0,
    "pending": 10,
    "not_applicable": 10,
    "fact_written": 20,
    "decision_written": 30,
    "job_enqueued": 40,
    "worker_started": 50,
    "blocked": 90,
    "stale": 90,
    "skipped": 90,
    "no_action": 90,
    "error": 90,
    "submitted": 100,
}
COMPACT_TIMING_KEYS = (
    "schema",
    "signal_id",
    "decision_id",
    "order_intent_id",
    "source_event_id",
    "source_observation_id",
    "fact_snapshot_id",
    "posterior_run_id",
    "posterior_target_fact_snapshot_id",
    "posterior_target_posterior_run_id",
    "source_id",
    "acceptance_state",
    "acceptance_reason",
    "signal_eligible",
    "awc_valid_time",
    "awc_hard_source_signal_at",
    "preparation_started_at",
    "pre_signal_prepare_state",
    "pre_signal_prepare_reason",
    "pre_signal_prepare_complete",
    "pre_signal_prepare_missing_components",
    "pre_signal_prepare_required_components",
    "pre_signal_prepare_components",
    "pre_signal_prepared_at",
    "awc_pre_signal_deadline_at",
    "pre_signal_prepared_before_deadline",
    "fact_written_at",
    "posterior_written_at",
    "decision_written_at",
    "job_enqueued_at",
    "worker_started_at",
    "orderbook_checked_at",
    "orderbook_refreshed_at",
    "orderbook_check_source",
    "orderbook_check_result",
    "risk_checked_at",
    "risk_check_source",
    "risk_check_result",
    "idempotency_checked_at",
    "idempotency_key",
    "submit_requested_at",
    "exchange_ack_at",
    "order_ack_at",
    "order_ack_status",
    "order_submitted_at",
    "signal_to_orderbook_ms",
    "signal_to_risk_ms",
    "signal_to_idempotency_ms",
    "signal_to_submit_request_ms",
    "signal_to_exchange_ack_ms",
    "orderbook_to_risk_check_ms",
    "risk_check_to_idempotency_ms",
    "risk_check_to_submit_ms",
    "submit_request_to_exchange_ack_ms",
    "terminal_at",
    "terminal_state",
    "terminal_reason",
    "signal_to_order_slo_ms",
    "signal_to_order_slo_met",
    "signal_to_order_slo_violation_reason",
)
COMPACT_META_KEYS = (
    "decision_id",
    "job_id",
    "job_state",
    "order_intent_id",
    "fact_id",
    "source_event_id",
    "source_observation_id",
    "fact_snapshot_id",
    "posterior_run_id",
    "posterior_target_fact_snapshot_id",
    "posterior_target_posterior_run_id",
    "posterior_target_lineage_source",
    "fact_as_of",
    "action",
    "outcome",
    "side",
    "will_order",
    "target_bucket",
    "new_target_bucket",
    "ssot_target_bucket",
    "created_at",
    "job_created_at",
    "hard_source_runtime_contract_version",
)
SIGNAL_SUMMARY_TIMING_KEYS = (
    "preparation_started_at",
    "pre_signal_prepared_at",
    "awc_pre_signal_deadline_at",
    "fact_written_at",
    "posterior_written_at",
    "decision_written_at",
    "job_enqueued_at",
    "worker_started_at",
    "orderbook_checked_at",
    "risk_checked_at",
    "idempotency_checked_at",
    "submit_requested_at",
    "exchange_ack_at",
    "order_submitted_at",
    "terminal_at",
)
ACCEPTED_SIGNAL_BASE_TIMING_KEYS = (
    "awc_valid_time",
    "awc_hard_source_signal_at",
    "fact_written_at",
    "posterior_written_at",
    "decision_written_at",
    "terminal_at",
)
PREPARATION_TIMING_KEYS = (
    "preparation_started_at",
    "pre_signal_prepared_at",
    "awc_pre_signal_deadline_at",
    "pre_signal_prepare_state",
    "pre_signal_prepare_complete",
)
SUBMITTED_EXECUTION_TIMING_KEYS = (
    "job_enqueued_at",
    "worker_started_at",
    "orderbook_checked_at",
    "risk_checked_at",
    "idempotency_checked_at",
    "submit_requested_at",
    "exchange_ack_at",
    "order_submitted_at",
)
TIMING_KEY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "orderbook_checked_at": ("orderbook_checked_at", "orderbook_refreshed_at"),
    "exchange_ack_at": ("exchange_ack_at", "order_ack_at"),
}


def ssot_chain_replay_input_contract() -> Dict[str, Any]:
    return {
        "schema": "ssot_chain_replay_input_contract_v1",
        "read_only": True,
        "owner": "scripts/ssot_chain_replay_audit.py",
        "required_anchor": "one_of_signal_id_fact_snapshot_id_posterior_run_id_decision_id_order_id_or_market_key",
        "input_keys": [
            "market_id",
            "station_id",
            "target_date_local",
            "target_bucket",
            "signal_id",
            "source_event_id",
            "source_observation_id",
            "fact_snapshot_id",
            "posterior_run_id",
            "decision_id",
            "order_intent_id",
            "order_id",
            "review_id",
        ],
        "output_sections": [
            "source_events",
            "source_observations",
            "fact_snapshots",
            "posterior_runs",
            "auto_trade_decisions",
            "signal_to_order_timing",
            "orders",
            "ui_snapshot",
            "daily_review",
            "awc_full_chain_replay",
            "calibration_candidates",
            "production_coverage",
            "lineage_consistency",
            "fork_assertions",
        ],
        "fork_assertions": [
            "ui_primary_probability_traces_to_one_posterior_run_id",
            "decision_traces_to_one_fact_snapshot_id",
            "consumer_lineage_references_same_fact_snapshot_id_and_posterior_run_id",
            "order_intent_traces_to_one_decision_id_and_signal_id",
            "blocker_source_is_auto_trade_decisions",
            "accepted_signal_execution_traces_to_auto_trade_decisions",
            "accepted_signal_has_final_terminal_state",
            "submitted_signal_has_decision_id",
            "submitted_signal_has_pre_submit_orderbook_risk_idempotency_checks",
            "accepted_signal_preparation_required_components_complete",
            "hard_fact_source_class_and_valid_time_are_replayable",
            "accepted_awc_signal_replays_source_fact_posterior_decision_execution_ui_review",
            "bias_candidate_origin_is_daily_review_not_production_worker",
            "no_second_posterior_blocker_target_realization_or_source_fit",
        ],
        "awc_signal_to_order_slo": {
            "start_field": "awc_hard_source_signal_at",
            "end_field": "order_submitted_at",
            "slo_ms": 3000,
            "terminal_states": sorted(FINAL_TERMINAL_STATES),
            "required_timing_keys": list(COMPACT_TIMING_KEYS),
        },
        "amsc_source_to_posterior_surface": {
            "field": "source_to_posterior_timing",
            "nested_field": "signal_to_order_timing.source_to_posterior_timing",
            "start_fields": ["latest_source_response_completed_at", "latest_source_fact_as_of"],
            "end_field": "posterior_apply_started_at",
            "applicability": "source_to_posterior_freshness_only_not_order_signal",
            "required_timing_keys": [
                "latest_source_fact_as_of",
                "posterior_apply_started_at",
                "source_fact_to_posterior_apply_start_ms",
                "order_signal_applicability",
            ],
        },
        "missing_policy": {
            "planned_table_missing": "explicit_blocker_not_inferred_success",
            "legacy_contract_missing": "warning_for_old_runtime_contract_only",
            "current_contract_nonaccepted_signal": "blocker",
            "terminal_state_missing": "blocker",
            "accepted_signal_missing_auto_trade_decision": "blocker",
            "job_without_auto_trade_decision": "blocker",
            "submitted_without_auto_trade_decision": "blocker",
            "execution_chain_missing_decision_id": "blocker",
            "lineage_fact_snapshot_id_conflict": "blocker",
            "lineage_posterior_run_id_conflict": "blocker",
            "accepted_signal_terminal_coverage_incomplete": "coverage_blocker",
            "accepted_signal_base_timing_incomplete": "coverage_blocker",
            "accepted_signal_preparation_coverage_incomplete": "coverage_blocker",
            "submitted_signal_execution_safety_coverage_incomplete": "coverage_blocker",
            "signal_to_order_timing_table_missing": "coverage_blocker",
            "accepted_signal_missing_signal_to_order_ledger": "coverage_blocker",
            "submitted_missing_orderbook_check": "blocker",
            "submitted_missing_risk_check": "blocker",
            "submitted_missing_idempotency_check": "blocker",
            "submitted_missing_submit_request": "blocker",
            "submitted_missing_exchange_ack": "blocker",
            "submitted_precheck_after_submit_request": "blocker",
            "pre_signal_required_component_contract_incomplete": "blocker",
        },
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_daily_weather_reviews": False,
            "changes_parameters": False,
            "fetches_external_weather_sources": False,
            "historical_backfill": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _parse_iso(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_cutoff(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=float(hours))).isoformat(timespec="seconds")


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(conn, name):
        return set()
    return {str(row["name"] or "") for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


def _where_clause(
    columns: set[str],
    *,
    slug: str = "",
    event_date: str = "",
    since: str = "",
    signal_id: str = "",
    json_column: str = "",
) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if since and "created_at" in columns:
        clauses.append("created_at >= ?")
        params.append(since)
    if slug and "slug" in columns:
        clauses.append("slug = ?")
        params.append(slug)
    elif slug and json_column in columns:
        clauses.append(f"{json_column} LIKE ?")
        params.append(f"%{slug}%")
    if event_date and "event_date" in columns:
        clauses.append("event_date = ?")
        params.append(event_date)
    elif event_date and json_column in columns:
        clauses.append(f"{json_column} LIKE ?")
        params.append(f"%{event_date}%")
    if signal_id and "signal_id" in columns:
        clauses.append("signal_id = ?")
        params.append(signal_id)
    elif signal_id and json_column in columns:
        clauses.append(f"{json_column} LIKE ?")
        params.append(f"%{signal_id}%")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _timing_from_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    timing = value.get("signal_to_order_timing")
    if isinstance(timing, Mapping):
        out = dict(timing)
    else:
        out = {}
    for key in (
        "signal_id",
        "decision_id",
        "order_intent_id",
        "source_event_id",
        "source_observation_id",
        "awc_hard_source_signal_at",
        "pre_signal_prepare_state",
        "pre_signal_prepare_reason",
        "pre_signal_prepare_components",
        "pre_signal_prepare_missing_components",
        "pre_signal_prepare_complete",
        "terminal_state",
        "terminal_reason",
        "terminal_at",
        "order_submitted_at",
        "signal_to_order_slo_met",
        "signal_to_order_slo_violation_reason",
    ):
        if key not in out and key in value:
            out[key] = value.get(key)
    acceptance = value.get("hard_source_acceptance_state")
    if isinstance(acceptance, Mapping):
        for key in ("signal_id", "source_event_id", "source_observation_id", "awc_hard_source_signal_at"):
            if key not in out and key in acceptance:
                out[key] = acceptance.get(key)
    return out


def _hard_source_posterior_override_expects_awc_signal(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    raw_override = value.get("hard_source_posterior_override")
    raw_meta = value.get("hard_source_posterior_override_meta")
    override = raw_override if isinstance(raw_override, Mapping) else raw_meta if isinstance(raw_meta, Mapping) else {}
    if not raw_override and not override:
        return False
    if not override:
        return bool(raw_override)
    source = str(
        override.get("source")
        or override.get("value_source")
        or override.get("decision_basis")
        or ""
    ).strip().lower()
    final_lock = bool(
        override.get("final_bucket_lock")
        or str(override.get("constraint_mode") or "").strip() == "final_bucket_lock"
        or str(override.get("final_lock_reason") or "").strip() == "settlement_actual_known"
    )
    if source in {"event_runs.settlement_actual", "settlement_actual"}:
        return False
    if final_lock and source.startswith("event_runs"):
        return False
    return bool(
        override.get("signal_id")
        or override.get("awc_hard_source_signal_at")
        or "awc" in source
        or "metar" in source
        or raw_override is True
    )


def _timing_marks_no_order_signal(value: Mapping[str, Any]) -> bool:
    timing = _timing_from_mapping(value)
    if not timing:
        return False
    if timing.get("signal_id") or timing.get("awc_hard_source_signal_at"):
        return False
    schema = str(timing.get("schema") or "").strip()
    state = str(timing.get("state") or timing.get("terminal_state") or "").strip()
    reason = str(timing.get("state_reason") or timing.get("terminal_reason") or "").strip()
    return bool(
        schema == "signal_to_order_timing_placeholder_v1"
        and state == "not_applicable"
        and (
            reason in {"", "fact_snapshot_write_does_not_submit_orders"}
            or "does_not_submit_orders" in reason
        )
    )


def _payload_has_hard_source_signal_expected(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if _timing_marks_no_order_signal(value):
        return False
    if (
        value.get("hard_source_direct") is True
        or value.get("awc_hard_source_signal_candidate")
        or _hard_source_posterior_override_expects_awc_signal(value)
    ):
        return True
    timing = _timing_from_mapping(value)
    if timing.get("signal_id") or timing.get("awc_hard_source_signal_at"):
        return True
    hard = value.get("hard_source_direct")
    if isinstance(hard, Mapping):
        nested_timing = _timing_from_mapping(hard)
        return bool(nested_timing.get("signal_id") or nested_timing.get("awc_hard_source_signal_at"))
    return False


def _compact_mapping(value: Mapping[str, Any], keys: Sequence[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in keys:
        if key in value:
            out[key] = value.get(key)
    return out


def _unique_ordered(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _counter_dict(values: Iterable[Any], *, missing: str = "missing") -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for value in values:
        text = str(value or "").strip() or missing
        counter[text] += 1
    return {key: counter[key] for key in sorted(counter)}


def _current_hard_source_runtime_contract_version() -> str:
    poller_path = ROOT / "scripts" / "hard_source_direct_poller.py"
    try:
        text = poller_path.read_text(encoding="utf-8")
        match = re.search(
            r"^HARD_SOURCE_RUNTIME_CONTRACT_VERSION\s*=\s*['\"]([^'\"]+)['\"]",
            text,
            re.MULTILINE,
        )
        if match:
            return str(match.group(1) or "")
    except Exception:
        pass
    try:
        from scripts import hard_source_direct_poller as poller  # type: ignore

        return str(getattr(poller, "HARD_SOURCE_RUNTIME_CONTRACT_VERSION", "") or "")
    except Exception:
        try:
            import hard_source_direct_poller as poller  # type: ignore

            return str(getattr(poller, "HARD_SOURCE_RUNTIME_CONTRACT_VERSION", "") or "")
        except Exception:
            return ""


def _runtime_contract_version_from(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    nested = value.get("hard_source_runtime_contract")
    if isinstance(nested, Mapping):
        version = str(nested.get("contract_version") or "").strip()
        if version:
            return version
    return str(value.get("hard_source_runtime_contract_version") or "").strip()


def _compact_row(row: Optional[sqlite3.Row], keys: Sequence[str]) -> Dict[str, Any]:
    if row is None:
        return {}
    out: Dict[str, Any] = {}
    row_keys = set(row.keys())
    for key in keys:
        if key in row_keys:
            out[key] = row[key]
    return out


def _signal_id_from_mapping(value: Any) -> str:
    timing = _timing_from_mapping(value)
    return str(timing.get("signal_id") or "").strip()


def _source_event_ids_from_payload(value: Any) -> List[str]:
    if not isinstance(value, Mapping):
        return []
    out: List[str] = []

    def append(raw: Any) -> None:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)

    for row in value.get("source_observations") or []:
        if isinstance(row, Mapping):
            append(row.get("source_event_id"))
    trace = value.get("source_acquisition_trace")
    if isinstance(trace, Mapping):
        for row in trace.get("source_observations") or []:
            if isinstance(row, Mapping):
                append(row.get("source_event_id"))
        for source_event_id in trace.get("source_event_ids") or []:
            append(source_event_id)
    for key in ("signal_to_order_timing", "hard_source_acceptance_state"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            append(nested.get("source_event_id"))
    append(value.get("source_event_id"))
    return out


def _source_observation_ids_from_payload(value: Any) -> List[str]:
    if not isinstance(value, Mapping):
        return []
    out: List[str] = []

    def append(raw: Any) -> None:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)

    def append_many(raw_values: Any) -> None:
        if isinstance(raw_values, list):
            for item in raw_values:
                append(item)

    for row in value.get("source_observations") or []:
        if isinstance(row, Mapping):
            append(row.get("source_observation_id"))
    for row in value.get("source_latency_trace") or []:
        if isinstance(row, Mapping):
            append(row.get("source_observation_id"))
    append_many(value.get("source_observation_ids"))

    trace = value.get("source_acquisition_trace")
    if isinstance(trace, Mapping):
        append(trace.get("source_observation_id"))
        append_many(trace.get("source_observation_ids"))
        for row in trace.get("source_observations") or []:
            if isinstance(row, Mapping):
                append(row.get("source_observation_id"))
        for row in trace.get("source_latency_trace") or []:
            if isinstance(row, Mapping):
                append(row.get("source_observation_id"))

    for key in (
        "lineage_state",
        "posterior_ssot_fact",
        "signal_to_order_timing",
        "hard_source_acceptance_state",
    ):
        nested = value.get(key)
        if not isinstance(nested, Mapping):
            continue
        append(nested.get("source_observation_id"))
        append_many(nested.get("source_observation_ids"))
        lineage = nested.get("lineage")
        if isinstance(lineage, Mapping):
            append(lineage.get("source_observation_id"))
            append_many(lineage.get("source_observation_ids"))

    append(value.get("source_observation_id"))
    return out


def _posterior_run_id_from_payload(value: Any) -> str:
    return str(_posterior_lineage_from_payload(value).get("posterior_run_id") or "")


def _fact_snapshot_id_from_payload(value: Any) -> str:
    return str(_posterior_lineage_from_payload(value).get("fact_snapshot_id") or "")


def _posterior_lineage_candidate_mappings(value: Any) -> Iterable[Tuple[str, Mapping[str, Any]]]:
    if not isinstance(value, Mapping):
        return
    yield "top_level", value
    for key in (
        "lineage_state",
        "posterior_runtime",
        "posterior_ssot_fact",
        "posterior_target_direct",
        "signal_to_order_timing",
    ):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            yield key, nested
    for list_key in ("entry_expected_targets", "exit_expected_targets", "reverse_expected_targets", "event_payloads", "include_event_keys"):
        raw_items = value.get(list_key)
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            yield list_key, item
            payload = item.get("payload")
            if isinstance(payload, Mapping):
                yield f"{list_key}.payload", payload
                for key in (
                    "lineage_state",
                    "posterior_runtime",
                    "posterior_ssot_fact",
                    "posterior_target_direct",
                    "signal_to_order_timing",
                ):
                    nested_payload = payload.get(key)
                    if isinstance(nested_payload, Mapping):
                        yield f"{list_key}.payload.{key}", nested_payload
            for key in (
                "lineage_state",
                "posterior_runtime",
                "posterior_ssot_fact",
                "posterior_target_direct",
                "signal_to_order_timing",
            ):
                nested_item = item.get(key)
                if isinstance(nested_item, Mapping):
                    yield f"{list_key}.{key}", nested_item


def _posterior_lineage_from_payload(value: Any) -> Dict[str, str]:
    fact_snapshot_id = ""
    posterior_run_id = ""
    source = ""
    fact_keys = ("fact_snapshot_id", "posterior_target_fact_snapshot_id", "primary_fact_snapshot_id")
    posterior_keys = ("posterior_run_id", "posterior_target_posterior_run_id")
    for source_name, candidate in _posterior_lineage_candidate_mappings(value):
        if not fact_snapshot_id:
            for key in fact_keys:
                text = str(candidate.get(key) or "").strip()
                if text:
                    fact_snapshot_id = text
                    source = source or source_name
                    break
        if not posterior_run_id:
            for key in posterior_keys:
                text = str(candidate.get(key) or "").strip()
                if text:
                    posterior_run_id = text
                    source = source or source_name
                    break
        if fact_snapshot_id and posterior_run_id:
            break
    out: Dict[str, str] = {}
    if fact_snapshot_id:
        out["fact_snapshot_id"] = fact_snapshot_id
        out["posterior_target_fact_snapshot_id"] = fact_snapshot_id
    if posterior_run_id:
        out["posterior_run_id"] = posterior_run_id
        out["posterior_target_posterior_run_id"] = posterior_run_id
    if source:
        out["posterior_target_lineage_source"] = source
    return out


def _first_event_key_from_payload(payload: Mapping[str, Any]) -> Dict[str, str]:
    for key in ("include_event_keys", "event_payloads"):
        raw = payload.get(key)
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                event_payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else item
                slug = str(event_payload.get("slug") or "").strip()
                event_date = str(event_payload.get("event_date") or "").strip()
                if slug or event_date:
                    return {"slug": slug, "event_date": event_date}
    for key in ("entry_expected_targets", "exit_expected_targets", "reverse_expected_targets"):
        raw_targets = payload.get(key)
        if isinstance(raw_targets, list):
            for target in raw_targets:
                if not isinstance(target, Mapping):
                    continue
                slug = str(target.get("slug") or "").strip()
                event_date = str(target.get("event_date") or "").strip()
                if slug or event_date:
                    return {"slug": slug, "event_date": event_date}
    return {
        "slug": str(payload.get("slug") or "").strip(),
        "event_date": str(payload.get("event_date") or "").strip(),
    }


def _signal_id_from_job_payload(payload: Mapping[str, Any]) -> str:
    for timing, _meta in _iter_job_timings(payload):
        signal_id = str(timing.get("signal_id") or "").strip()
        if signal_id:
            return signal_id
    return ""


def _job_payload_has_hard_source_signal(payload: Mapping[str, Any]) -> bool:
    if isinstance(payload.get("hard_source_direct"), Mapping):
        return True
    for timing, meta in _iter_job_timings(payload):
        if timing.get("signal_id") or meta.get("hard_source_direct"):
            return True
    return False


def _latest_fact_anchor(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    at_or_before: str = "",
) -> Dict[str, Any]:
    if not _table_exists(conn, "event_fact_history"):
        return {"available": False, "reason": "event_fact_history_missing"}
    where: List[str] = []
    params: List[Any] = []
    if slug:
        where.append("slug = ?")
        params.append(slug)
    if event_date:
        where.append("event_date = ?")
        params.append(event_date)
    if at_or_before:
        where.append("created_at <= ?")
        params.append(at_or_before)
    if not where:
        return {"available": False, "reason": "slug_or_event_date_missing"}
    row = conn.execute(
        f"""
        SELECT *
        FROM event_fact_history
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return {"available": False, "reason": "fact_anchor_not_found"}
    payload = _safe_json(row["fact_json"] if "fact_json" in row.keys() else "")
    payload_ref = payload if isinstance(payload, Mapping) else {}
    return {
        "available": True,
        "fact": _compact_row(row, ("id", "slug", "event_date", "fact_as_of", "created_at", "composer_version")),
        "fact_id": str(row["id"] if "id" in row.keys() else ""),
        "signal_id": _signal_id_from_mapping(payload_ref),
        "fact_snapshot_id": _fact_snapshot_id_from_payload(payload_ref),
        "posterior_run_id": _posterior_run_id_from_payload(payload_ref),
        "source_event_ids": _source_event_ids_from_payload(payload_ref),
        "source_observation_ids": _source_observation_ids_from_payload(payload_ref),
        "hard_source_signal_expected": _payload_has_hard_source_signal_expected(payload_ref),
        "posterior_present": bool(
            payload_ref.get("posterior_temperature_distribution")
            or payload_ref.get("distribution")
            or payload_ref.get("rows")
        ),
        "source_trace_present": bool(payload_ref.get("source_acquisition_trace")),
    }


def _decision_anchor(
    conn: sqlite3.Connection,
    *,
    decision_id: Any = "",
    order_id: str = "",
    slug: str = "",
    event_date: str = "",
    outcome: str = "",
    side: str = "",
    action: str = "",
) -> Dict[str, Any]:
    if not _table_exists(conn, "auto_trade_decisions"):
        return {"available": False, "reason": "auto_trade_decisions_missing"}
    row: Optional[sqlite3.Row] = None
    try:
        decision_id_int = int(decision_id or 0)
    except Exception:
        decision_id_int = 0
    if decision_id_int > 0:
        row = conn.execute("SELECT * FROM auto_trade_decisions WHERE id = ?", (decision_id_int,)).fetchone()
    if row is None and slug and event_date and outcome and side and action:
        rows = conn.execute(
            """
            SELECT *
            FROM auto_trade_decisions
            WHERE slug = ? AND event_date = ? AND outcome = ? AND side = ? AND action = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (slug, event_date, outcome, side, action),
        ).fetchall()
        for candidate in rows:
            details = _safe_json(candidate["details_json"])
            if isinstance(details, Mapping) and (
                details.get("order_id")
                or details.get("auto_order_id")
                or details.get("hard_source_direct")
                or details.get("posterior_target_direct")
                or candidate["will_order"]
            ):
                row = candidate
                break
        if row is None and rows:
            row = rows[0]
    if row is None and order_id:
        row = conn.execute(
            """
            SELECT *
            FROM auto_trade_decisions
            WHERE details_json LIKE ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (f"%{order_id}%",),
        ).fetchone()
    if row is None:
        return {"available": False, "reason": "decision_anchor_not_found"}
    details = _safe_json(row["details_json"])
    details_ref = details if isinstance(details, Mapping) else {}
    lineage = _posterior_lineage_from_payload(details_ref)
    signal_id = _signal_id_from_mapping(details_ref)
    decision = _compact_row(
        row,
        ("id", "run_id", "slug", "event_date", "outcome", "side", "action", "will_order", "created_at"),
    )
    return {
        "available": True,
        "decision": decision,
        "decision_id": str(decision.get("id") or ""),
        "run_id": str(decision.get("run_id") or ""),
        "slug": str(decision.get("slug") or ""),
        "event_date": str(decision.get("event_date") or ""),
        "signal_id": signal_id,
        "order_id": str(details_ref.get("order_id") or details_ref.get("auto_order_id") or ""),
        "fact_snapshot_id": str(lineage.get("fact_snapshot_id") or ""),
        "posterior_run_id": str(lineage.get("posterior_run_id") or ""),
        "posterior_target_fact_snapshot_id": str(lineage.get("posterior_target_fact_snapshot_id") or ""),
        "posterior_target_posterior_run_id": str(lineage.get("posterior_target_posterior_run_id") or ""),
        "posterior_target_lineage_source": str(lineage.get("posterior_target_lineage_source") or ""),
        "posterior_ssot_fact_present": bool(details_ref.get("posterior_ssot_fact")),
        "hard_source_signal_expected": bool(
            signal_id
            or details_ref.get("hard_source_direct")
            or _hard_source_posterior_override_expects_awc_signal(details_ref)
            or details_ref.get("awc_hard_source_signal_at")
        ),
        "posterior_target_direct": bool(details_ref.get("posterior_target_direct")),
        "details_keys": sorted(str(key) for key in details_ref.keys())[:80],
    }


def _job_anchor(conn: sqlite3.Connection, *, job_id: Any = "") -> Dict[str, Any]:
    if not _table_exists(conn, "auto_trade_jobs"):
        return {"available": False, "reason": "auto_trade_jobs_missing"}
    try:
        job_id_int = int(job_id or 0)
    except Exception:
        job_id_int = 0
    if job_id_int <= 0:
        return {"available": False, "reason": "job_id_missing"}
    row = conn.execute("SELECT * FROM auto_trade_jobs WHERE id = ?", (job_id_int,)).fetchone()
    if row is None:
        return {"available": False, "reason": "job_anchor_not_found"}
    payload = _safe_json(row["payload_json"])
    payload_ref = payload if isinstance(payload, Mapping) else {}
    lineage = _posterior_lineage_from_payload(payload_ref)
    event_key = _first_event_key_from_payload(payload_ref)
    signal_id = _signal_id_from_job_payload(payload_ref)
    return {
        "available": True,
        "job": _compact_row(row, ("id", "source", "state", "run_id", "created_at", "started_at", "ended_at")),
        "job_id": str(row["id"] if "id" in row.keys() else ""),
        "run_id": str((row["run_id"] if "run_id" in row.keys() else "") or ""),
        "slug": event_key.get("slug", ""),
        "event_date": event_key.get("event_date", ""),
        "signal_id": signal_id,
        "fact_snapshot_id": str(lineage.get("fact_snapshot_id") or ""),
        "posterior_run_id": str(lineage.get("posterior_run_id") or ""),
        "posterior_target_fact_snapshot_id": str(lineage.get("posterior_target_fact_snapshot_id") or ""),
        "posterior_target_posterior_run_id": str(lineage.get("posterior_target_posterior_run_id") or ""),
        "posterior_target_lineage_source": str(lineage.get("posterior_target_lineage_source") or ""),
        "posterior_ssot_fact_present": bool(
            payload_ref.get("posterior_ssot_fact")
            or any(
                isinstance(candidate, Mapping) and candidate.get("posterior_ssot_fact")
                for _source_name, candidate in _posterior_lineage_candidate_mappings(payload_ref)
            )
        ),
        "hard_source_signal_expected": _job_payload_has_hard_source_signal(payload_ref),
        "payload_keys": sorted(str(key) for key in payload_ref.keys())[:80],
    }


def _fact_anchor(conn: sqlite3.Connection, *, fact_id: Any = "") -> Dict[str, Any]:
    if not _table_exists(conn, "event_fact_history"):
        return {"available": False, "reason": "event_fact_history_missing"}
    try:
        fact_id_int = int(fact_id or 0)
    except Exception:
        fact_id_int = 0
    if fact_id_int <= 0:
        return {"available": False, "reason": "fact_id_missing"}
    row = conn.execute("SELECT * FROM event_fact_history WHERE id = ?", (fact_id_int,)).fetchone()
    if row is None:
        return {"available": False, "reason": "fact_anchor_not_found"}
    payload = _safe_json(row["fact_json"])
    payload_ref = payload if isinstance(payload, Mapping) else {}
    return {
        "available": True,
        "fact": _compact_row(row, ("id", "slug", "event_date", "fact_as_of", "created_at", "composer_version")),
        "fact_id": str(row["id"] if "id" in row.keys() else ""),
        "slug": str(row["slug"] or payload_ref.get("slug") or ""),
        "event_date": str(row["event_date"] or payload_ref.get("event_date") or ""),
        "signal_id": _signal_id_from_mapping(payload_ref),
        "fact_snapshot_id": _fact_snapshot_id_from_payload(payload_ref),
        "posterior_run_id": _posterior_run_id_from_payload(payload_ref),
        "source_event_ids": _source_event_ids_from_payload(payload_ref),
        "source_observation_ids": _source_observation_ids_from_payload(payload_ref),
        "hard_source_signal_expected": _payload_has_hard_source_signal_expected(payload_ref),
        "posterior_present": bool(
            payload_ref.get("posterior_temperature_distribution")
            or payload_ref.get("distribution")
            or payload_ref.get("rows")
        ),
        "source_trace_present": bool(payload_ref.get("source_acquisition_trace")),
    }


def _fact_anchor_from_fact_json_like(
    conn: sqlite3.Connection,
    *,
    needle: str,
    reason: str,
    slug: str = "",
    event_date: str = "",
) -> Dict[str, Any]:
    if not _table_exists(conn, "event_fact_history"):
        return {"available": False, "reason": "event_fact_history_missing"}
    needle_text = str(needle or "").strip()
    if not needle_text:
        return {"available": False, "reason": f"{reason}_missing"}
    where = ["fact_json LIKE ?"]
    params: List[Any] = [f"%{needle_text}%"]
    if slug:
        where.append("slug = ?")
        params.append(slug)
    if event_date:
        where.append("event_date = ?")
        params.append(event_date)
    row = conn.execute(
        f"""
        SELECT *
        FROM event_fact_history
        WHERE {' AND '.join(where)}
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return {"available": False, "reason": f"{reason}_not_found", "blocker": f"{reason}_not_found"}
    return _fact_anchor(conn, fact_id=row["id"])


def _source_event_anchor(
    conn: sqlite3.Connection,
    *,
    source_event_id: str = "",
    slug: str = "",
    event_date: str = "",
) -> Dict[str, Any]:
    source_event_id_text = str(source_event_id or "").strip()
    if not source_event_id_text:
        return {"available": False, "reason": "source_event_id_missing"}
    source_row: Dict[str, Any] = {}
    if _table_exists(conn, "source_observations"):
        columns = _table_columns(conn, "source_observations")
        if "source_event_id" in columns:
            row = conn.execute(
                "SELECT * FROM source_observations WHERE source_event_id = ? LIMIT 1",
                (source_event_id_text,),
            ).fetchone()
            if row is not None:
                source_row = _compact_row(
                    row,
                    (
                        "source_observation_id",
                        "source_event_id",
                        "source_id",
                        "source_class",
                        "role",
                        "station_id",
                        "event_date",
                        "valid_time",
                        "fetched_at",
                        "temperature_c",
                    ),
                )
                if not event_date and "event_date" in row.keys():
                    event_date = str(row["event_date"] or "")
    fact_anchor = _fact_anchor_from_fact_json_like(
        conn,
        needle=source_event_id_text,
        reason="source_event_id_anchor",
        slug=slug,
        event_date=event_date,
    )
    if not fact_anchor.get("available"):
        if source_row:
            return {
                "available": True,
                "source_observation": source_row,
                "source_event_id": source_event_id_text,
                "event_date": str(event_date or source_row.get("event_date") or ""),
                "hard_source_signal_expected": False,
                "warning": "source_event_id_has_no_fact_snapshot",
            }
        return fact_anchor
    out = dict(fact_anchor)
    out["source_event_id"] = source_event_id_text
    if source_row:
        out["source_observation"] = source_row
    return out


def _source_observation_anchor(
    conn: sqlite3.Connection,
    *,
    source_observation_id: str = "",
    slug: str = "",
    event_date: str = "",
) -> Dict[str, Any]:
    source_observation_id_text = str(source_observation_id or "").strip()
    if not source_observation_id_text:
        return {"available": False, "reason": "source_observation_id_missing"}
    source_row: Dict[str, Any] = {}
    if _table_exists(conn, "source_observations"):
        columns = _table_columns(conn, "source_observations")
        if "source_observation_id" in columns:
            row = conn.execute(
                "SELECT * FROM source_observations WHERE source_observation_id = ? LIMIT 1",
                (source_observation_id_text,),
            ).fetchone()
            if row is not None:
                source_row = _compact_row(
                    row,
                    (
                        "source_observation_id",
                        "source_event_id",
                        "source_id",
                        "source_class",
                        "role",
                        "station_id",
                        "event_date",
                        "valid_time",
                        "fetched_at",
                        "temperature_c",
                    ),
                )
                if not event_date and "event_date" in row.keys():
                    event_date = str(row["event_date"] or "")
    fact_anchor = _fact_anchor_from_fact_json_like(
        conn,
        needle=source_observation_id_text,
        reason="source_observation_id_anchor",
        slug=slug,
        event_date=event_date,
    )
    if not fact_anchor.get("available"):
        if source_row:
            return {
                "available": True,
                "source_observation": source_row,
                "source_observation_id": source_observation_id_text,
                "source_event_id": str(source_row.get("source_event_id") or ""),
                "event_date": str(event_date or source_row.get("event_date") or ""),
                "hard_source_signal_expected": False,
                "warning": "source_observation_id_has_no_fact_snapshot",
            }
        return fact_anchor
    out = dict(fact_anchor)
    out["source_observation_id"] = source_observation_id_text
    if source_row:
        out["source_observation"] = source_row
        if not str(out.get("source_event_id") or "").strip():
            out["source_event_id"] = str(source_row.get("source_event_id") or "")
    if not str(out.get("source_event_id") or "").strip():
        source_event_ids = out.get("source_event_ids")
        if isinstance(source_event_ids, list) and len(source_event_ids) == 1:
            out["source_event_id"] = str(source_event_ids[0] or "")
    return out


def _posterior_run_anchor(
    conn: sqlite3.Connection,
    *,
    posterior_run_id: str = "",
    slug: str = "",
    event_date: str = "",
) -> Dict[str, Any]:
    posterior_run_id_text = str(posterior_run_id or "").strip()
    if not posterior_run_id_text:
        return {"available": False, "reason": "posterior_run_id_missing"}
    fact_anchor = _fact_anchor_from_fact_json_like(
        conn,
        needle=posterior_run_id_text,
        reason="posterior_run_id_anchor",
        slug=slug,
        event_date=event_date,
    )
    if fact_anchor.get("available"):
        out = dict(fact_anchor)
        out["posterior_run_id"] = posterior_run_id_text
        return out
    return fact_anchor


def _review_anchor(conn: sqlite3.Connection, *, review_id: str = "") -> Dict[str, Any]:
    review_id_text = str(review_id or "").strip()
    if not review_id_text:
        return {"available": False, "reason": "review_id_missing"}
    if not _table_exists(conn, "daily_weather_reviews"):
        return {
            "available": False,
            "not_applicable": True,
            "reason": "daily_weather_reviews_missing_planned_table",
            "review_id": review_id_text,
            "hard_source_signal_expected": False,
        }
    columns = _table_columns(conn, "daily_weather_reviews")
    row: Optional[sqlite3.Row] = None
    if "review_id" in columns:
        row = conn.execute("SELECT * FROM daily_weather_reviews WHERE review_id = ? LIMIT 1", (review_id_text,)).fetchone()
    if row is None and "id" in columns:
        try:
            review_id_int = int(review_id_text)
        except Exception:
            review_id_int = 0
        if review_id_int > 0:
            row = conn.execute("SELECT * FROM daily_weather_reviews WHERE id = ? LIMIT 1", (review_id_int,)).fetchone()
    if row is None:
        for json_column in ("review_json", "payload_json"):
            if json_column in columns:
                row = conn.execute(
                    f"SELECT * FROM daily_weather_reviews WHERE {json_column} LIKE ? ORDER BY id DESC LIMIT 1",
                    (f"%{review_id_text}%",),
                ).fetchone()
                if row is not None:
                    break
    if row is None:
        return {"available": False, "reason": "review_anchor_not_found", "blocker": "review_anchor_not_found"}
    payload: Mapping[str, Any] = {}
    for json_column in ("review_json", "payload_json"):
        if json_column in columns:
            parsed = _safe_json(row[json_column])
            if isinstance(parsed, Mapping):
                payload = parsed
                break
    slug = str((row["slug"] if "slug" in columns else "") or payload.get("slug") or "")
    event_date = str((row["event_date"] if "event_date" in columns else "") or payload.get("event_date") or "")
    fact_snapshot_id = str(
        (row["fact_snapshot_id"] if "fact_snapshot_id" in columns else "")
        or payload.get("fact_snapshot_id")
        or ""
    )
    out: Dict[str, Any] = {
        "available": True,
        "review": _compact_row(row, ("id", "review_id", "slug", "event_date", "fact_snapshot_id", "created_at")),
        "review_id": review_id_text,
        "slug": slug,
        "event_date": event_date,
        "fact_snapshot_id": fact_snapshot_id,
        "hard_source_signal_expected": False,
        "payload_keys": sorted(str(key) for key in payload.keys())[:80],
    }
    if fact_snapshot_id:
        fact_anchor = _fact_anchor_from_fact_json_like(
            conn,
            needle=fact_snapshot_id,
            reason="review_fact_snapshot_anchor",
            slug=slug,
            event_date=event_date,
        )
        out["fact_anchor"] = fact_anchor
        if fact_anchor.get("available"):
            for key in ("fact_id", "slug", "event_date", "signal_id", "posterior_run_id"):
                value = str(fact_anchor.get(key) or "").strip()
                if value and not str(out.get(key) or "").strip():
                    out[key] = value
            out["source_event_ids"] = fact_anchor.get("source_event_ids") or []
            out["source_observation_ids"] = fact_anchor.get("source_observation_ids") or []
            out["hard_source_signal_expected"] = bool(fact_anchor.get("hard_source_signal_expected"))
    return out


def _order_anchor(conn: sqlite3.Connection, *, order_id: str = "", auto_order_id: Any = "") -> Dict[str, Any]:
    if not _table_exists(conn, "auto_orders"):
        return {"available": False, "reason": "auto_orders_missing"}
    row: Optional[sqlite3.Row] = None
    try:
        auto_order_id_int = int(auto_order_id or 0)
    except Exception:
        auto_order_id_int = 0
    if auto_order_id_int > 0:
        row = conn.execute("SELECT * FROM auto_orders WHERE id = ?", (auto_order_id_int,)).fetchone()
    if row is None and order_id:
        row = conn.execute("SELECT * FROM auto_orders WHERE order_id = ?", (order_id,)).fetchone()
    if row is None:
        return {"available": False, "reason": "order_anchor_not_found"}
    row_keys = set(row.keys())

    def row_text(key: str) -> str:
        if key not in row_keys:
            return ""
        return str(row[key] or "").strip()

    order = _compact_row(
        row,
        (
            "id",
            "order_id",
            "correlation_id",
            "decision_id",
            "signal_id",
            "order_intent_id",
            "slug",
            "event_date",
            "outcome",
            "side",
            "action",
            "price",
            "size",
            "strategy",
            "created_at",
            "order_status",
            "order_checked_at",
        ),
    )
    order_decision_id = row_text("decision_id")
    if order_decision_id:
        decision = _decision_anchor(conn, decision_id=order_decision_id)
    else:
        decision = _decision_anchor(
            conn,
            order_id=row_text("order_id"),
            slug=row_text("slug"),
            event_date=row_text("event_date"),
            outcome=row_text("outcome"),
            side=row_text("side"),
            action=row_text("action"),
        )
    strategy = row_text("strategy")
    order_signal_id = row_text("signal_id")
    order_intent_id = row_text("order_intent_id")
    hard_source_expected = bool(
        order_signal_id
        or
        decision.get("hard_source_signal_expected")
        or strategy == "hard_source_direct"
    )
    return {
        "available": True,
        "order": order,
        "decision_anchor": decision,
        "slug": row_text("slug"),
        "event_date": row_text("event_date"),
        "decision_id": str(order_decision_id or decision.get("decision_id") or ""),
        "run_id": str(decision.get("run_id") or ""),
        "signal_id": str(order_signal_id or decision.get("signal_id") or ""),
        "order_intent_id": order_intent_id,
        "fact_snapshot_id": str(decision.get("fact_snapshot_id") or ""),
        "posterior_run_id": str(decision.get("posterior_run_id") or ""),
        "posterior_target_fact_snapshot_id": str(decision.get("posterior_target_fact_snapshot_id") or ""),
        "posterior_target_posterior_run_id": str(decision.get("posterior_target_posterior_run_id") or ""),
        "posterior_target_lineage_source": str(decision.get("posterior_target_lineage_source") or ""),
        "hard_source_signal_expected": hard_source_expected,
    }


def _resolve_anchor_context(
    conn: sqlite3.Connection,
    *,
    order_id: str = "",
    auto_order_id: str = "",
    decision_id: str = "",
    job_id: str = "",
    fact_id: str = "",
    ui_snapshot_id: str = "",
    source_event_id: str = "",
    source_observation_id: str = "",
    posterior_run_id: str = "",
    review_id: str = "",
    slug: str = "",
    event_date: str = "",
    signal_id: str = "",
) -> Dict[str, Any]:
    requested = {
        "order_id": order_id,
        "auto_order_id": auto_order_id,
        "decision_id": decision_id,
        "job_id": job_id,
        "fact_id": fact_id,
        "ui_snapshot_id": ui_snapshot_id,
        "source_event_id": source_event_id,
        "source_observation_id": source_observation_id,
        "posterior_run_id": posterior_run_id,
        "review_id": review_id,
        "slug": slug,
        "event_date": event_date,
        "signal_id": signal_id,
    }
    anchor_provided = any(str(value or "").strip() for key, value in requested.items() if key not in {"slug", "event_date", "signal_id"})
    trace: Dict[str, Any] = {
        "schema": "ssot_chain_replay_anchor_trace_v1",
        "read_only": True,
        "anchor_provided": bool(anchor_provided),
        "requested": {key: value for key, value in requested.items() if str(value or "").strip()},
        "anchors": {},
        "resolved": {
            "slug": str(slug or ""),
            "event_date": str(event_date or ""),
            "signal_id": str(signal_id or ""),
            "decision_id": "",
            "fact_id": "",
            "job_id": "",
            "run_id": "",
            "source_event_id": str(source_event_id or ""),
            "source_observation_id": str(source_observation_id or ""),
            "source_event_ids": [],
            "source_observation_ids": [],
            "posterior_run_id": str(posterior_run_id or ""),
            "review_id": str(review_id or ""),
            "fact_snapshot_id": "",
            "anchor_not_applicable": False,
            "anchor_not_applicable_reason": "",
            "hard_source_signal_expected": None,
        },
        "warnings": [],
        "blockers": [],
    }

    def merge_anchor(name: str, anchor: Mapping[str, Any]) -> None:
        trace["anchors"][name] = dict(anchor)
        if anchor.get("warning"):
            trace["warnings"].append(str(anchor.get("warning")))
        if anchor.get("blocker"):
            trace["blockers"].append(str(anchor.get("blocker")))
        if anchor.get("not_applicable"):
            resolved = trace["resolved"]
            resolved["anchor_not_applicable"] = True
            resolved["anchor_not_applicable_reason"] = str(anchor.get("reason") or "")
            if anchor.get("hard_source_signal_expected") is not None:
                resolved["hard_source_signal_expected"] = bool(anchor.get("hard_source_signal_expected"))
            if anchor.get("reason"):
                trace["warnings"].append(str(anchor.get("reason")))
        if not anchor.get("available"):
            return
        resolved = trace["resolved"]
        for key in (
            "slug",
            "event_date",
            "signal_id",
            "decision_id",
            "fact_id",
            "job_id",
            "run_id",
            "source_event_id",
            "source_observation_id",
            "posterior_run_id",
            "review_id",
            "fact_snapshot_id",
        ):
            value = str(anchor.get(key) or "").strip()
            if value and not str(resolved.get(key) or "").strip():
                resolved[key] = value
        source_event_ids = anchor.get("source_event_ids")
        if isinstance(source_event_ids, list):
            existing = list(resolved.get("source_event_ids") or [])
            for item in source_event_ids:
                text = str(item or "").strip()
                if text and text not in existing:
                    existing.append(text)
            resolved["source_event_ids"] = existing
        source_observation_ids = anchor.get("source_observation_ids")
        if isinstance(source_observation_ids, list):
            existing = list(resolved.get("source_observation_ids") or [])
            for item in source_observation_ids:
                text = str(item or "").strip()
                if text and text not in existing:
                    existing.append(text)
            resolved["source_observation_ids"] = existing
        if anchor.get("hard_source_signal_expected") is not None:
            resolved["hard_source_signal_expected"] = bool(
                resolved.get("hard_source_signal_expected") or anchor.get("hard_source_signal_expected")
            )

    if source_observation_id:
        merge_anchor(
            "source_observation",
            _source_observation_anchor(
                conn,
                source_observation_id=source_observation_id,
                slug=slug,
                event_date=event_date,
            ),
        )
    if source_event_id:
        merge_anchor(
            "source_event",
            _source_event_anchor(conn, source_event_id=source_event_id, slug=slug, event_date=event_date),
        )
    if posterior_run_id:
        merge_anchor(
            "posterior_run",
            _posterior_run_anchor(conn, posterior_run_id=posterior_run_id, slug=slug, event_date=event_date),
        )
    if review_id:
        merge_anchor("review", _review_anchor(conn, review_id=review_id))
    if order_id or auto_order_id:
        merge_anchor("order", _order_anchor(conn, order_id=order_id, auto_order_id=auto_order_id))
    if decision_id:
        merge_anchor("decision", _decision_anchor(conn, decision_id=decision_id))
    if job_id:
        merge_anchor("job", _job_anchor(conn, job_id=job_id))
    fact_anchor_id = fact_id or ui_snapshot_id
    if fact_anchor_id:
        fact_anchor = _fact_anchor(conn, fact_id=fact_anchor_id)
        merge_anchor("fact" if fact_id else "ui_snapshot_fact", fact_anchor)
        if ui_snapshot_id:
            trace["anchors"]["ui_snapshot"] = {
                "available": bool(fact_anchor.get("available")),
                "ui_snapshot_id": str(ui_snapshot_id),
                "resolved_as": "event_fact_history.id",
                "reason": "ui_snapshot_is_rendered_from_fact_snapshot",
            }
    resolved = trace["resolved"]
    anchor_not_applicable = bool(resolved.get("anchor_not_applicable"))
    if anchor_provided and not anchor_not_applicable and not str(resolved.get("signal_id") or "").strip():
        if resolved.get("hard_source_signal_expected") is False:
            trace["warnings"].append("anchor_has_no_awc_signal_expected")
        elif resolved.get("hard_source_signal_expected") is True:
            trace["blockers"].append("anchor_signal_id_missing")
        else:
            trace["warnings"].append("anchor_signal_expectation_unknown")
    if (
        anchor_provided
        and not anchor_not_applicable
        and not str(resolved.get("slug") or "").strip()
        and not str(resolved.get("signal_id") or "").strip()
    ):
        trace["blockers"].append("anchor_slug_or_signal_missing")
    if (
        not anchor_provided
        and str(resolved.get("slug") or "").strip()
        and str(resolved.get("event_date") or "").strip()
    ):
        latest_fact = _latest_fact_anchor(
            conn,
            slug=str(resolved.get("slug") or ""),
            event_date=str(resolved.get("event_date") or ""),
        )
        trace["anchors"].setdefault("latest_fact_for_resolved_event", latest_fact)
        if latest_fact.get("available"):
            trace["anchor_provided"] = True
            merge_anchor("latest_fact_for_resolved_event", latest_fact)
    trace["warnings"] = sorted(set(trace.get("warnings") or []))
    trace["blockers"] = sorted(set(trace.get("blockers") or []))
    return trace


def _append_signal_source(
    records: Dict[str, Dict[str, Any]],
    *,
    source: str,
    timing: Mapping[str, Any],
    meta: Optional[Mapping[str, Any]] = None,
) -> None:
    signal_id = str(timing.get("signal_id") or "").strip()
    if not signal_id:
        return
    record = records.setdefault(
        signal_id,
        {
            "signal_id": signal_id,
            "sources": [],
            "timings": [],
            "terminal_states": [],
            "target_outcomes": [],
            "decision_ids": [],
            "job_ids": [],
            "order_intent_ids": [],
            "hard_source_runtime_contract_versions": [],
            "blockers": [],
            "warnings": [],
        },
    )
    timing_compact = _compact_mapping(timing, COMPACT_TIMING_KEYS)
    meta_compact = _compact_mapping(dict(meta or {}), COMPACT_META_KEYS)
    item = {
        "source": source,
        "meta": meta_compact,
        "timing": timing_compact,
    }
    record["sources"].append(source)
    record["timings"].append(item)
    lineage_observation = _lineage_observation_from_compact_source(
        source=source,
        timing=timing_compact,
        meta=meta_compact,
    )
    if lineage_observation.get("fact_snapshot_id") or lineage_observation.get("posterior_run_id"):
        observations = record.setdefault("lineage_observations", [])
        if lineage_observation not in observations:
            observations.append(lineage_observation)
    for target_key, raw in (
        ("decision_ids", (meta or {}).get("decision_id") or timing.get("decision_id")),
        ("job_ids", (meta or {}).get("job_id") or timing.get("job_id")),
        ("order_intent_ids", (meta or {}).get("order_intent_id") or timing.get("order_intent_id")),
    ):
        text = str(raw or "").strip()
        if text and text not in record[target_key]:
            record[target_key].append(text)
    awc_at = str(timing.get("awc_hard_source_signal_at") or "")
    if awc_at and not record.get("awc_hard_source_signal_at"):
        record["awc_hard_source_signal_at"] = awc_at
    if "signal_eligible" in timing and "signal_eligible" not in record:
        record["signal_eligible"] = timing.get("signal_eligible")
    if timing.get("acceptance_state") and not record.get("acceptance_state"):
        record["acceptance_state"] = str(timing.get("acceptance_state") or "")
    if timing.get("acceptance_reason") and not record.get("acceptance_reason"):
        record["acceptance_reason"] = str(timing.get("acceptance_reason") or "")
    runtime_version = _runtime_contract_version_from(timing) or _runtime_contract_version_from(meta or {})
    if runtime_version and runtime_version not in record["hard_source_runtime_contract_versions"]:
        record["hard_source_runtime_contract_versions"].append(runtime_version)
    state = str(timing.get("terminal_state") or "").strip()
    if state:
        record["terminal_states"].append(
            {
                "state": state,
                "rank": int(TERMINAL_RANK.get(state, 0)),
                "source": source,
                "reason": str(timing.get("terminal_reason") or ""),
            }
        )
    target_keys = ("target_bucket", "new_target_bucket", "ssot_target_bucket")
    for target_key in target_keys:
        target = str((meta or {}).get(target_key) or timing.get(target_key) or "").strip()
        if target and target not in record["target_outcomes"]:
            record["target_outcomes"].append(target)
    if str((meta or {}).get("action") or "").strip() == "entry":
        target = str((meta or {}).get("outcome") or "").strip()
        if target and target not in record["target_outcomes"]:
            record["target_outcomes"].append(target)


def _first_text_from_mapping(value: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return ""


def _lineage_observation_from_compact_source(
    *,
    source: str,
    timing: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> Dict[str, Any]:
    fact_snapshot_id = _first_text_from_mapping(
        meta,
        ("fact_snapshot_id", "posterior_target_fact_snapshot_id", "primary_fact_snapshot_id"),
    ) or _first_text_from_mapping(
        timing,
        ("fact_snapshot_id", "posterior_target_fact_snapshot_id", "primary_fact_snapshot_id"),
    )
    posterior_run_id = _first_text_from_mapping(
        meta,
        ("posterior_run_id", "posterior_target_posterior_run_id"),
    ) or _first_text_from_mapping(
        timing,
        ("posterior_run_id", "posterior_target_posterior_run_id"),
    )
    return {
        "source": str(source or ""),
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
        "decision_id": str(meta.get("decision_id") or timing.get("decision_id") or ""),
        "job_id": str(meta.get("job_id") or timing.get("job_id") or ""),
        "fact_id": str(meta.get("fact_id") or ""),
    }


def _signal_lineage_consistency(record: Mapping[str, Any]) -> Dict[str, Any]:
    observations = [
        dict(item)
        for item in (record.get("lineage_observations") or [])
        if isinstance(item, Mapping)
    ]
    sources = _unique_ordered(record.get("sources") or [])
    fact_snapshot_ids = _unique_ordered(
        str(item.get("fact_snapshot_id") or "").strip()
        for item in observations
        if str(item.get("fact_snapshot_id") or "").strip()
    )
    posterior_run_ids = _unique_ordered(
        str(item.get("posterior_run_id") or "").strip()
        for item in observations
        if str(item.get("posterior_run_id") or "").strip()
    )
    observed_sources = {
        str(item.get("source") or "").strip()
        for item in observations
        if str(item.get("fact_snapshot_id") or item.get("posterior_run_id") or "").strip()
    }
    expected_sources = [
        source
        for source in ("event_fact_history", "auto_trade_decisions", "auto_trade_jobs")
        if source in sources
    ]
    missing_sources = [source for source in expected_sources if source not in observed_sources]
    blockers: List[str] = []
    if len(fact_snapshot_ids) > 1:
        blockers.append("lineage_fact_snapshot_id_conflict")
    if len(posterior_run_ids) > 1:
        blockers.append("lineage_posterior_run_id_conflict")
    return {
        "schema": "ssot_signal_lineage_consistency_v1",
        "ok": not blockers,
        "fact_snapshot_ids": fact_snapshot_ids,
        "posterior_run_ids": posterior_run_ids,
        "observed_sources": sorted(observed_sources),
        "expected_sources": expected_sources,
        "missing_sources": missing_sources,
        "blockers": sorted(set(blockers)),
        "observations": observations,
    }


def _collect_from_signal_ledger(
    conn: sqlite3.Connection,
    records: Dict[str, Dict[str, Any]],
    *,
    slug: str,
    event_date: str,
    since: str,
    signal_id: str,
    source_event_id: str = "",
    source_observation_id: str = "",
    limit: int,
) -> Dict[str, Any]:
    table = "signal_to_order_timing"
    if not _table_exists(conn, table):
        return {"available": False, "reason": "table_missing"}
    columns = _table_columns(conn, table)
    where, params = _where_clause(columns, since=since, signal_id=signal_id, json_column="timing_json")
    source_event_id_text = str(source_event_id or "").strip()
    if source_event_id_text:
        if "source_event_id" in columns:
            where = f"{where} AND source_event_id = ?" if where else " WHERE source_event_id = ?"
            params.append(source_event_id_text)
        elif "timing_json" in columns:
            where = f"{where} AND timing_json LIKE ?" if where else " WHERE timing_json LIKE ?"
            params.append(f"%{source_event_id_text}%")
    source_observation_id_text = str(source_observation_id or "").strip()
    if source_observation_id_text:
        if "source_observation_id" in columns:
            where = f"{where} AND source_observation_id = ?" if where else " WHERE source_observation_id = ?"
            params.append(source_observation_id_text)
        elif "timing_json" in columns:
            where = f"{where} AND timing_json LIKE ?" if where else " WHERE timing_json LIKE ?"
            params.append(f"%{source_observation_id_text}%")
    order = "created_at DESC" if "created_at" in columns else "signal_id"
    rows = conn.execute(
        f"SELECT * FROM {table}{where} ORDER BY {order} LIMIT ?",
        tuple(params + [int(limit)]),
    ).fetchall()
    for row in rows:
        parsed = _safe_json(row["timing_json"] if "timing_json" in columns else "")
        timing = dict(parsed) if isinstance(parsed, Mapping) else {}
        if not timing:
            timing = {key: row[key] for key in row.keys()}
        if slug and slug not in json.dumps(timing, ensure_ascii=False):
            continue
        if event_date and event_date not in json.dumps(timing, ensure_ascii=False):
            # Timing rows do not always carry event keys; do not reject if absent.
            pass
        _append_signal_source(
            records,
            source="signal_to_order_timing",
            timing=timing,
            meta={"ledger_created_at": row["created_at"] if "created_at" in columns else ""},
        )
    return {"available": True, "rows": len(rows)}


def _collect_signal_ledger_for_existing_records(
    conn: sqlite3.Connection,
    records: Dict[str, Dict[str, Any]],
    *,
    since: str,
    limit: int,
) -> Dict[str, Any]:
    if not records:
        return {"available": True, "rows": 0, "scan_reason": "no_existing_signal_records"}
    if not _table_exists(conn, "signal_to_order_timing"):
        return {"available": False, "reason": "table_missing"}
    checked = 0
    appended = 0
    for signal_id, record in list(records.items())[: max(0, int(limit))]:
        if "signal_to_order_timing" in set(record.get("sources") or []):
            continue
        checked += 1
        before = len(record.get("sources") or [])
        _collect_from_signal_ledger(
            conn,
            records,
            slug="",
            event_date="",
            since=since,
            signal_id=str(signal_id or ""),
            limit=1,
        )
        after = len(record.get("sources") or [])
        if after > before and "signal_to_order_timing" in set(record.get("sources") or []):
            appended += 1
    return {
        "available": True,
        "rows": appended,
        "signals_checked": checked,
        "scan_reason": "exact_signal_id_backfill",
    }


def _collect_from_decisions(
    conn: sqlite3.Connection,
    records: Dict[str, Dict[str, Any]],
    *,
    slug: str,
    event_date: str,
    since: str,
    signal_id: str,
    decision_id: str = "",
    run_id: str = "",
    skip_unindexed_scan: bool = False,
    limit: int,
) -> Dict[str, Any]:
    table = "auto_trade_decisions"
    if not _table_exists(conn, table):
        return {"available": False, "reason": "table_missing"}
    columns = _table_columns(conn, table)
    reason = "json_or_event_scan"
    try:
        decision_id_int = int(decision_id or 0)
    except Exception:
        decision_id_int = 0
    try:
        run_id_int = int(run_id or 0)
    except Exception:
        run_id_int = 0
    if decision_id_int > 0 and "id" in columns:
        rows = conn.execute("SELECT * FROM auto_trade_decisions WHERE id = ?", (decision_id_int,)).fetchall()
        reason = "decision_id_anchor"
    elif run_id_int > 0 and "run_id" in columns:
        rows = conn.execute(
            """
            SELECT *
            FROM auto_trade_decisions
            WHERE run_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (run_id_int, int(limit)),
        ).fetchall()
        reason = "run_id_anchor"
    elif skip_unindexed_scan:
        return {
            "available": True,
            "rows": 0,
            "scan_reason": "skipped_unindexed_details_scan",
            "skipped": True,
        }
    else:
        where, params = _where_clause(
            columns,
            slug=slug,
            event_date=event_date,
            since=since,
            signal_id=signal_id,
            json_column="details_json",
        )
        rows = conn.execute(
            f"SELECT * FROM {table}{where} ORDER BY id DESC LIMIT ?",
            tuple(params + [int(limit)]),
        ).fetchall()
    for row in rows:
        details = _safe_json(row["details_json"] if "details_json" in columns else "")
        if not isinstance(details, Mapping):
            continue
        timing = _timing_from_mapping(details)
        if signal_id and str(timing.get("signal_id") or "").strip() != signal_id:
            continue
        meta = {
            "decision_id": row["id"] if "id" in columns else "",
            "action": row["action"] if "action" in columns else "",
            "outcome": row["outcome"] if "outcome" in columns else "",
            "side": row["side"] if "side" in columns else "",
            "will_order": row["will_order"] if "will_order" in columns else None,
            "created_at": row["created_at"] if "created_at" in columns else "",
            "hard_source_runtime_contract": details.get("hard_source_runtime_contract"),
            "hard_source_runtime_contract_version": details.get("hard_source_runtime_contract_version"),
        }
        meta.update(_posterior_lineage_from_payload(details))
        _append_signal_source(records, source="auto_trade_decisions", timing=timing, meta=meta)
    return {"available": True, "rows": len(rows), "scan_reason": reason}


def _append_decision_row(records: Dict[str, Dict[str, Any]], row: sqlite3.Row, columns: set[str]) -> bool:
    details = _safe_json(row["details_json"] if "details_json" in columns else "")
    if not isinstance(details, Mapping):
        return False
    timing = _timing_from_mapping(details)
    row_signal_id = str((row["signal_id"] if "signal_id" in columns else "") or "").strip()
    if row_signal_id and not timing.get("signal_id"):
        timing["signal_id"] = row_signal_id
    if not str(timing.get("signal_id") or "").strip():
        return False
    meta = {
        "decision_id": row["id"] if "id" in columns else "",
        "action": row["action"] if "action" in columns else "",
        "outcome": row["outcome"] if "outcome" in columns else "",
        "side": row["side"] if "side" in columns else "",
        "will_order": row["will_order"] if "will_order" in columns else None,
        "created_at": row["created_at"] if "created_at" in columns else "",
        "hard_source_runtime_contract": details.get("hard_source_runtime_contract"),
        "hard_source_runtime_contract_version": details.get("hard_source_runtime_contract_version"),
    }
    meta.update(_posterior_lineage_from_payload(details))
    _append_signal_source(records, source="auto_trade_decisions", timing=timing, meta=meta)
    return True


def _collect_decisions_for_existing_records(
    conn: sqlite3.Connection,
    records: Dict[str, Dict[str, Any]],
    *,
    since: str,
    limit: int,
) -> Dict[str, Any]:
    signal_ids = [
        str(signal_id or "").strip()
        for signal_id in list(records)
        if str(signal_id or "").strip()
    ]
    if not signal_ids:
        return {
            "available": True,
            "rows": 0,
            "signal_count": 0,
            "scan_reason": "existing_signal_id_decision_backfill",
            "scanned_signal_count": 0,
        }
    if not _table_exists(conn, "auto_trade_decisions"):
        return {"available": False, "reason": "table_missing"}
    columns = _table_columns(conn, "auto_trade_decisions")
    max_signal_scan = max(1, int(limit)) * max(1, _env_int("SSOT_CHAIN_REPLAY_DECISION_BACKFILL_SIGNAL_MULTIPLIER", 20))
    if len(signal_ids) > max_signal_scan:
        signal_ids = signal_ids[:max_signal_scan]
    if "signal_id" in columns:
        placeholders = ",".join("?" for _ in signal_ids)
        where_parts = [f"signal_id IN ({placeholders})"]
        params: list[Any] = list(signal_ids)
        if since and "created_at" in columns:
            where_parts.append("created_at >= ?")
            params.append(since)
        rows = conn.execute(
            f"""
            SELECT *
            FROM auto_trade_decisions
            WHERE {' AND '.join(where_parts)}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params + [max(1, int(limit)) * len(signal_ids)]),
        ).fetchall()
        appended = 0
        for row in rows:
            if _append_decision_row(records, row, columns):
                appended += 1
        return {
            "available": True,
            "rows": len(rows),
            "appended_rows": appended,
            "signal_count": len(signal_ids),
            "scan_reason": "existing_signal_id_indexed_decision_backfill",
            "scanned_signal_count": len(signal_ids),
        }
    if "details_json" in columns:
        appended = 0
        rows = 0
        query_count = 0
        chunk_size = max(1, _env_int("SSOT_CHAIN_REPLAY_DECISION_BACKFILL_CHUNK_SIZE", 50))
        per_signal_limit = max(1, int(limit))
        for start in range(0, len(signal_ids), chunk_size):
            chunk = signal_ids[start : start + chunk_size]
            like_parts = ["details_json LIKE ?" for _ in chunk]
            where_parts = ["(" + " OR ".join(like_parts) + ")"]
            params: list[Any] = [f"%{signal_id}%" for signal_id in chunk]
            if since and "created_at" in columns:
                where_parts.append("created_at >= ?")
                params.append(since)
            chunk_rows = conn.execute(
                f"""
                SELECT *
                FROM auto_trade_decisions
                WHERE {' AND '.join(where_parts)}
                ORDER BY id DESC
                LIMIT ?
                """,
                tuple(params + [per_signal_limit * len(chunk)]),
            ).fetchall()
            query_count += 1
            rows += len(chunk_rows)
            for row in chunk_rows:
                if _append_decision_row(records, row, columns):
                    appended += 1
        return {
            "available": True,
            "rows": rows,
            "appended_rows": appended,
            "signal_count": len(signal_ids),
            "scan_reason": "existing_signal_id_bounded_json_decision_backfill",
            "scanned_signal_count": len(signal_ids),
            "query_count": query_count,
            "chunk_size": chunk_size,
        }
    rows = 0
    scanned = 0
    for signal_id in signal_ids:
        before = len(records.get(signal_id, {}).get("timings") or [])
        result = _collect_from_decisions(
            conn,
            records,
            slug="",
            event_date="",
            since=since,
            signal_id=signal_id,
            limit=max(1, int(limit)),
        )
        scanned += 1
        rows += int(result.get("rows") or 0)
        after = len(records.get(signal_id, {}).get("timings") or [])
        if after <= before and result.get("available") is False:
            continue
    return {
        "available": True,
        "rows": rows,
        "signal_count": len(signal_ids),
        "scan_reason": "existing_signal_id_decision_backfill",
        "scanned_signal_count": scanned,
    }


def _iter_job_timings(payload: Mapping[str, Any]) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
    hard = payload.get("hard_source_direct")
    if isinstance(hard, Mapping):
        timing = _timing_from_mapping(hard)
        if timing:
            yield timing, dict(hard)
    for key in ("entry_expected_targets", "exit_expected_targets", "reverse_expected_targets"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, Mapping):
                continue
            timing = _timing_from_mapping(item)
            if timing:
                yield timing, dict(item)


def _collect_from_jobs(
    conn: sqlite3.Connection,
    records: Dict[str, Dict[str, Any]],
    *,
    slug: str,
    event_date: str,
    since: str,
    signal_id: str,
    run_id: str = "",
    job_id: str = "",
    skip_unindexed_scan: bool = False,
    limit: int,
) -> Dict[str, Any]:
    table = "auto_trade_jobs"
    if not _table_exists(conn, table):
        return {"available": False, "reason": "table_missing"}
    columns = _table_columns(conn, table)
    rows: Sequence[sqlite3.Row]
    reason = "json_payload_scan"
    try:
        job_id_int = int(job_id or 0)
    except Exception:
        job_id_int = 0
    try:
        run_id_int = int(run_id or 0)
    except Exception:
        run_id_int = 0
    if job_id_int > 0 and "id" in columns:
        rows = conn.execute("SELECT * FROM auto_trade_jobs WHERE id = ?", (job_id_int,)).fetchall()
        reason = "job_id_anchor"
    elif run_id_int > 0 and "run_id" in columns:
        rows = conn.execute(
            """
            SELECT *
            FROM auto_trade_jobs
            WHERE run_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (run_id_int, int(limit)),
        ).fetchall()
        reason = "run_id_anchor"
    elif skip_unindexed_scan:
        return {
            "available": True,
            "rows": 0,
            "scan_reason": "skipped_unindexed_payload_scan",
            "skipped": True,
        }
    else:
        where, params = _where_clause(
            columns,
            slug=slug,
            event_date=event_date,
            since=since,
            signal_id=signal_id,
            json_column="payload_json",
        )
        rows = conn.execute(
            f"SELECT * FROM {table}{where} ORDER BY id DESC LIMIT ?",
            tuple(params + [int(limit)]),
        ).fetchall()
    for row in rows:
        payload = _safe_json(row["payload_json"] if "payload_json" in columns else "")
        if not isinstance(payload, Mapping):
            continue
        payload_lineage = _posterior_lineage_from_payload(payload)
        for timing, meta in _iter_job_timings(payload):
            if signal_id and str(timing.get("signal_id") or "").strip() != signal_id:
                continue
            target_lineage = _posterior_lineage_from_payload(meta)
            for key, value in payload_lineage.items():
                target_lineage.setdefault(key, value)
            meta.update(
                {
                    "job_id": row["id"] if "id" in columns else "",
                    "job_state": row["state"] if "state" in columns else "",
                    "job_created_at": row["created_at"] if "created_at" in columns else "",
                }
            )
            meta.update(target_lineage)
            _append_signal_source(records, source="auto_trade_jobs", timing=timing, meta=meta)
    return {"available": True, "rows": len(rows), "scan_reason": reason}


def _collect_from_facts(
    conn: sqlite3.Connection,
    records: Dict[str, Dict[str, Any]],
    *,
    slug: str,
    event_date: str,
    since: str,
    signal_id: str,
    fact_id: str = "",
    source_event_id: str = "",
    source_observation_id: str = "",
    posterior_run_id: str = "",
    skip_unindexed_scan: bool = False,
    limit: int,
) -> Dict[str, Any]:
    table = "event_fact_history"
    if not _table_exists(conn, table):
        return {"available": False, "reason": "table_missing"}
    columns = _table_columns(conn, table)
    reason = "json_or_event_scan"
    try:
        fact_id_int = int(fact_id or 0)
    except Exception:
        fact_id_int = 0
    if fact_id_int > 0 and "id" in columns:
        rows = conn.execute("SELECT * FROM event_fact_history WHERE id = ?", (fact_id_int,)).fetchall()
        reason = "fact_id_anchor"
    elif skip_unindexed_scan:
        return {
            "available": True,
            "rows": 0,
            "scan_reason": "skipped_unindexed_fact_scan",
            "skipped": True,
        }
    else:
        where, params = _where_clause(
            columns,
            slug=slug,
            event_date=event_date,
            since=since,
            signal_id=signal_id,
            json_column="fact_json",
        )
        for json_needle in (source_event_id, source_observation_id, posterior_run_id):
            text = str(json_needle or "").strip()
            if text and "fact_json" in columns:
                where = f"{where} AND fact_json LIKE ?" if where else " WHERE fact_json LIKE ?"
                params.append(f"%{text}%")
        rows = conn.execute(
            f"SELECT * FROM {table}{where} ORDER BY id DESC LIMIT ?",
            tuple(params + [int(limit)]),
        ).fetchall()
    for row in rows:
        payload = _safe_json(row["fact_json"] if "fact_json" in columns else "")
        if not isinstance(payload, Mapping):
            continue
        timing = _timing_from_mapping(payload)
        if signal_id and str(timing.get("signal_id") or "").strip() != signal_id:
            continue
        source_event_id_text = str(source_event_id or "").strip()
        if source_event_id_text and source_event_id_text not in _source_event_ids_from_payload(payload):
            continue
        source_observation_id_text = str(source_observation_id or "").strip()
        if source_observation_id_text and source_observation_id_text not in _source_observation_ids_from_payload(payload):
            continue
        posterior_run_id_text = str(posterior_run_id or "").strip()
        if posterior_run_id_text and posterior_run_id_text != _posterior_run_id_from_payload(payload):
            continue
        meta = {
            "fact_id": row["id"] if "id" in columns else "",
            "fact_snapshot_id": _fact_snapshot_id_from_payload(payload),
            "posterior_run_id": _posterior_run_id_from_payload(payload),
            "source_event_ids": _source_event_ids_from_payload(payload),
            "source_observation_ids": _source_observation_ids_from_payload(payload),
            "fact_as_of": row["fact_as_of"] if "fact_as_of" in columns else "",
            "created_at": row["created_at"] if "created_at" in columns else "",
            "ssot_target_bucket": payload.get("ssot_target_bucket"),
            "hard_source_runtime_contract": payload.get("hard_source_runtime_contract"),
            "hard_source_runtime_contract_version": payload.get("hard_source_runtime_contract_version"),
        }
        meta.update(_posterior_lineage_from_payload(payload))
        _append_signal_source(records, source="event_fact_history", timing=timing, meta=meta)
    return {"available": True, "rows": len(rows), "scan_reason": reason}


def _best_terminal(record: Mapping[str, Any]) -> Dict[str, Any]:
    states = record.get("terminal_states")
    if not isinstance(states, list) or not states:
        return {}
    return dict(max(states, key=lambda item: int(item.get("rank") or 0)))


def _latest_timing(record: Mapping[str, Any]) -> Dict[str, Any]:
    timings = record.get("timings")
    if not isinstance(timings, list) or not timings:
        return {}
    ranked = sorted(
        timings,
        key=lambda item: int(TERMINAL_RANK.get(str((item.get("timing") or {}).get("terminal_state") or ""), 0)),
        reverse=True,
    )
    timing = ranked[0].get("timing") if ranked else {}
    return dict(timing) if isinstance(timing, Mapping) else {}


def _reviewed_no_action_without_decision(
    *,
    final_state: str,
    best_terminal: Mapping[str, Any],
    best_timing: Mapping[str, Any],
) -> bool:
    if str(final_state or "").strip() != "no_action":
        return False
    reason = str(
        best_terminal.get("reason")
        or best_timing.get("terminal_reason")
        or best_timing.get("signal_to_order_slo_violation_reason")
        or ""
    ).strip().lower()
    return bool(
        "review" in reason
        and (
            "no auto_trade_decision" in reason
            or "no auto trade decision" in reason
            or "no decision" in reason
        )
        and ("no order" in reason or "order evidence" in reason)
    )


def _finalize_signal(
    record: Dict[str, Any],
    *,
    current_runtime_contract_version: str = "",
    fail_legacy_artifacts: bool = False,
) -> Dict[str, Any]:
    blockers: List[str] = list(record.get("blockers") or [])
    warnings: List[str] = list(record.get("warnings") or [])
    best_terminal = _best_terminal(record)
    best_timing = _latest_timing(record)
    final_state = str(best_terminal.get("state") or "").strip()
    awc_at = str(record.get("awc_hard_source_signal_at") or best_timing.get("awc_hard_source_signal_at") or "")
    acceptance_state = str(record.get("acceptance_state") or best_timing.get("acceptance_state") or "").strip()
    signal_eligible_raw = record.get("signal_eligible", best_timing.get("signal_eligible"))
    signal_eligible = signal_eligible_raw is not False
    runtime_versions = set(_unique_ordered(record.get("hard_source_runtime_contract_versions") or []))
    is_legacy_runtime_record = bool(
        runtime_versions
        and current_runtime_contract_version
        and current_runtime_contract_version not in runtime_versions
    )
    accepted_signal = bool(awc_at and signal_eligible and acceptance_state not in {"rejected", "demoted", "conflict"})
    record["candidate_scope"] = "accepted_awc_hard_source_signal" if accepted_signal else "non_accepted_candidate"
    lineage_consistency = _signal_lineage_consistency(record)
    blockers.extend(lineage_consistency.get("blockers") or [])
    record["lineage_consistency"] = lineage_consistency
    if not accepted_signal:
        sources = set(record.get("sources") or [])
        is_current_runtime_artifact = bool(
            current_runtime_contract_version and current_runtime_contract_version in runtime_versions
        )
        if "event_fact_history" in sources:
            if is_current_runtime_artifact or fail_legacy_artifacts:
                blockers.append("non_accepted_candidate_has_fact_artifact")
            else:
                warnings.append("legacy_non_accepted_candidate_has_fact_artifact")
        if "auto_trade_jobs" in sources:
            if is_current_runtime_artifact or fail_legacy_artifacts:
                blockers.append("non_accepted_candidate_has_job_artifact")
            else:
                warnings.append("legacy_non_accepted_candidate_has_job_artifact")
        record["final_terminal_state"] = final_state
        record["best_terminal"] = best_terminal
        record["best_timing"] = best_timing
        record["pre_signal_prepare_state"] = str(best_timing.get("pre_signal_prepare_state") or "")
        record["pre_signal_prepare_missing_components"] = list(
            best_timing.get("pre_signal_prepare_missing_components") or []
        )
        record["current_runtime_nonaccepted_artifact"] = is_current_runtime_artifact
        record["lineage_consistency"] = lineage_consistency
        record["blockers"] = sorted(set(blockers))
        record["warnings"] = sorted(set(warnings))
        return record
    if not final_state:
        if is_legacy_runtime_record and not fail_legacy_artifacts:
            warnings.append("legacy_accepted_signal_terminal_missing")
        else:
            blockers.append("signal_terminal_missing")
    elif final_state not in FINAL_TERMINAL_STATES:
        if is_legacy_runtime_record and not fail_legacy_artifacts:
            warnings.append("legacy_accepted_signal_terminal_non_final")
        else:
            blockers.append("signal_terminal_non_final")
    sources = set(record.get("sources") or [])
    has_decision_source = "auto_trade_decisions" in sources
    has_job_source = "auto_trade_jobs" in sources
    has_submitted_artifact = bool(
        final_state == "submitted"
        or best_timing.get("order_submitted_at")
        or best_timing.get("exchange_ack_at")
    )
    decision_ids = [
        str(item or "").strip()
        for item in (record.get("decision_ids") or [])
        if str(item or "").strip()
    ]
    reviewed_no_action_without_decision = _reviewed_no_action_without_decision(
        final_state=final_state,
        best_terminal=best_terminal,
        best_timing=best_timing,
    )
    if final_state in FINAL_TERMINAL_STATES and not has_decision_source and not reviewed_no_action_without_decision:
        blockers.append("accepted_signal_missing_auto_trade_decision")
    if has_job_source and not has_decision_source:
        blockers.append("job_without_auto_trade_decision")
    if has_submitted_artifact and not has_decision_source:
        blockers.append("submitted_without_auto_trade_decision")
    if has_submitted_artifact and not decision_ids:
        blockers.append("execution_chain_missing_decision_id")
    if has_submitted_artifact:
        orderbook_check_at = (
            best_timing.get("orderbook_checked_at")
            or best_timing.get("orderbook_refreshed_at")
        )
        risk_check_at = best_timing.get("risk_checked_at")
        idempotency_check_at = best_timing.get("idempotency_checked_at")
        submit_requested_at = best_timing.get("submit_requested_at")
        exchange_ack_at = best_timing.get("exchange_ack_at") or best_timing.get("order_ack_at")
        if not orderbook_check_at:
            blockers.append("submitted_missing_orderbook_check")
        if not risk_check_at:
            blockers.append("submitted_missing_risk_check")
        if not idempotency_check_at:
            blockers.append("submitted_missing_idempotency_check")
        if not submit_requested_at:
            blockers.append("submitted_missing_submit_request")
        if not exchange_ack_at:
            blockers.append("submitted_missing_exchange_ack")
        submit_dt = _parse_iso(submit_requested_at)
        if submit_dt is not None:
            for precheck_at in (orderbook_check_at, risk_check_at, idempotency_check_at):
                precheck_dt = _parse_iso(precheck_at)
                if precheck_dt is not None and precheck_dt > submit_dt:
                    blockers.append("submitted_precheck_after_submit_request")
                    break
    same_rank_states = {
        str(item.get("state") or "")
        for item in record.get("terminal_states") or []
        if int(item.get("rank") or 0) == int(best_terminal.get("rank") or -1)
    }
    same_rank_states.discard("")
    if len(same_rank_states) > 1:
        blockers.append("signal_terminal_conflict")
    prep_state = str(best_timing.get("pre_signal_prepare_state") or "").strip()
    missing_components = list(best_timing.get("pre_signal_prepare_missing_components") or [])
    prep_complete = best_timing.get("pre_signal_prepare_complete")
    required_components = [
        str(item or "").strip()
        for item in best_timing.get("pre_signal_prepare_required_components") or []
        if str(item or "").strip()
    ]
    components_ref = best_timing.get("pre_signal_prepare_components")
    components = components_ref if isinstance(components_ref, Mapping) else {}
    required_component_contract_missing = [
        component
        for component in PRE_SIGNAL_REQUIRED_COMPONENTS
        if component not in required_components or component not in components
    ]
    required_component_not_ok = [
        component
        for component in PRE_SIGNAL_REQUIRED_COMPONENTS
        if isinstance(components.get(component), Mapping)
        and components[component].get("ok") is not True
    ]
    if not prep_state and reviewed_no_action_without_decision:
        pass
    elif not prep_state:
        if is_legacy_runtime_record and not fail_legacy_artifacts:
            warnings.append("legacy_pre_signal_preparation_missing")
        else:
            blockers.append("pre_signal_preparation_missing")
    elif prep_state == "preparation_partial" or missing_components or prep_complete is False:
        blockers.append("pre_signal_preparation_incomplete")
    if prep_state and (required_component_contract_missing or required_component_not_ok):
        if is_legacy_runtime_record and not fail_legacy_artifacts:
            warnings.append("legacy_pre_signal_required_component_contract_incomplete")
        else:
            blockers.append("pre_signal_required_component_contract_incomplete")
            blockers.append("pre_signal_preparation_incomplete")
    before_deadline = best_timing.get("pre_signal_prepared_before_deadline")
    if before_deadline is False:
        blockers.append("pre_signal_prepared_after_deadline")
    targets = [str(item or "").strip() for item in record.get("target_outcomes") or [] if str(item or "").strip()]
    if len(set(targets)) > 1:
        warnings.append("signal_multiple_target_outcomes_seen")
    record["final_terminal_state"] = final_state
    record["best_terminal"] = best_terminal
    record["best_timing"] = best_timing
    record["pre_signal_prepare_state"] = prep_state
    record["pre_signal_prepare_missing_components"] = missing_components
    record["pre_signal_required_component_contract_missing"] = required_component_contract_missing
    record["pre_signal_required_component_not_ok"] = required_component_not_ok
    record["legacy_runtime_record"] = is_legacy_runtime_record
    record["lineage_consistency"] = lineage_consistency
    record["blockers"] = sorted(set(blockers))
    record["warnings"] = sorted(set(warnings))
    return record


def _signal_summary(record: Mapping[str, Any]) -> Dict[str, Any]:
    best_terminal = dict(record.get("best_terminal") or {})
    best_timing = dict(record.get("best_timing") or {})
    stage_timestamps = {
        key: best_timing.get(key)
        for key in SIGNAL_SUMMARY_TIMING_KEYS
        if best_timing.get(key) not in ("", None)
    }
    return {
        "signal_id": record.get("signal_id"),
        "candidate_scope": record.get("candidate_scope"),
        "awc_hard_source_signal_at": record.get("awc_hard_source_signal_at")
        or best_timing.get("awc_hard_source_signal_at"),
        "source_event_id": best_timing.get("source_event_id"),
        "source_observation_id": best_timing.get("source_observation_id"),
        "sources": _unique_ordered(record.get("sources") or []),
        "timing_count": len(record.get("timings") or []),
        "acceptance_state": record.get("acceptance_state") or best_timing.get("acceptance_state"),
        "acceptance_reason": record.get("acceptance_reason") or best_timing.get("acceptance_reason"),
        "signal_eligible": record.get("signal_eligible", best_timing.get("signal_eligible")),
        "final_terminal_state": record.get("final_terminal_state"),
        "final_terminal_reason": best_terminal.get("reason") or best_timing.get("terminal_reason"),
        "final_terminal_source": best_terminal.get("source"),
        "pre_signal_prepare_state": record.get("pre_signal_prepare_state"),
        "pre_signal_prepare_reason": best_timing.get("pre_signal_prepare_reason"),
        "pre_signal_prepare_complete": best_timing.get("pre_signal_prepare_complete"),
        "pre_signal_prepare_missing_components": record.get("pre_signal_prepare_missing_components") or [],
        "pre_signal_required_component_contract_missing": record.get(
            "pre_signal_required_component_contract_missing"
        )
        or [],
        "pre_signal_required_component_not_ok": record.get("pre_signal_required_component_not_ok") or [],
        "pre_signal_prepared_before_deadline": best_timing.get("pre_signal_prepared_before_deadline"),
        "target_outcomes": _unique_ordered(record.get("target_outcomes") or []),
        "decision_ids": _unique_ordered(record.get("decision_ids") or []),
        "job_ids": _unique_ordered(record.get("job_ids") or []),
        "order_intent_ids": _unique_ordered(record.get("order_intent_ids") or []),
        "lineage_consistency": dict(record.get("lineage_consistency") or {}),
        "execution_chain_has_auto_trade_decision": "auto_trade_decisions"
        in set(record.get("sources") or []),
        "hard_source_runtime_contract_versions": _unique_ordered(
            record.get("hard_source_runtime_contract_versions") or []
        ),
        "legacy_runtime_record": bool(record.get("legacy_runtime_record")),
        "current_runtime_nonaccepted_artifact": bool(record.get("current_runtime_nonaccepted_artifact")),
        "stage_timestamps": stage_timestamps,
        "signal_to_order_slo_ms": best_timing.get("signal_to_order_slo_ms"),
        "signal_to_order_slo_met": best_timing.get("signal_to_order_slo_met"),
        "signal_to_order_slo_violation_reason": best_timing.get("signal_to_order_slo_violation_reason"),
        "blockers": list(record.get("blockers") or []),
        "warnings": list(record.get("warnings") or []),
    }


def _chain_coverage(conn: sqlite3.Connection) -> Dict[str, Any]:
    tables = {
        name: _table_exists(conn, name)
        for name in (
            "event_fact_history",
            "event_fact_latest",
            "auto_trade_decisions",
            "auto_trade_jobs",
            "auto_orders",
            "edge_trades",
            "signal_to_order_timing",
            "daily_weather_reviews",
        )
    }
    return {
        "tables": tables,
        "source_to_fact": bool(tables.get("event_fact_history")),
        "fact_to_decision": bool(tables.get("event_fact_history") and tables.get("auto_trade_decisions")),
        "decision_to_execution": bool(tables.get("auto_trade_decisions") and tables.get("auto_trade_jobs")),
        "execution_to_order": bool(tables.get("auto_trade_jobs") and tables.get("auto_orders")),
        "review_available": bool(tables.get("daily_weather_reviews")),
    }


def _review_anchor_for_fact_snapshot_ids(
    conn: sqlite3.Connection,
    *,
    fact_snapshot_ids: Sequence[str],
    slug: str = "",
    event_date: str = "",
) -> Dict[str, Any]:
    if not _table_exists(conn, "daily_weather_reviews"):
        return {"available": False, "reason": "daily_weather_reviews_missing_planned_table"}
    columns = _table_columns(conn, "daily_weather_reviews")
    ids = [str(item or "").strip() for item in fact_snapshot_ids if str(item or "").strip()]
    row: Optional[sqlite3.Row] = None
    order = "datetime(created_at) DESC" if "created_at" in columns else "rowid DESC"
    if ids and "fact_snapshot_id" in columns:
        placeholders = ",".join("?" for _ in ids)
        row = conn.execute(
            f"""
            SELECT *
            FROM daily_weather_reviews
            WHERE fact_snapshot_id IN ({placeholders})
            ORDER BY {order}
            LIMIT 1
            """,
            tuple(ids),
        ).fetchone()
    if row is None and slug and event_date and {"slug", "event_date"}.issubset(columns):
        row = conn.execute(
            f"""
            SELECT *
            FROM daily_weather_reviews
            WHERE slug = ? AND event_date = ?
            ORDER BY {order}
            LIMIT 1
            """,
            (slug, event_date),
        ).fetchone()
    if row is None and ids and "review_json" in columns:
        clauses = " OR ".join("review_json LIKE ?" for _ in ids)
        row = conn.execute(
            f"""
            SELECT *
            FROM daily_weather_reviews
            WHERE {clauses}
            ORDER BY {order}
            LIMIT 1
            """,
            tuple(f"%{item}%" for item in ids),
        ).fetchone()
    if row is None:
        return {"available": False, "reason": "daily_review_missing"}
    return {
        "available": True,
        "review": _compact_row(row, ("id", "review_id", "slug", "event_date", "fact_snapshot_id", "created_at")),
        "review_id": str(row["review_id"] if "review_id" in row.keys() else row["id"] if "id" in row.keys() else ""),
        "fact_snapshot_id": str(row["fact_snapshot_id"] if "fact_snapshot_id" in row.keys() else ""),
    }


def _awc_full_chain_replay_contract(
    finalized: Sequence[Mapping[str, Any]],
    *,
    source_counts: Mapping[str, Any],
    lineage_consistency: Mapping[str, Any],
    chain_coverage: Mapping[str, Any],
    review_anchor: Mapping[str, Any],
    required: bool,
) -> Dict[str, Any]:
    accepted = [
        record
        for record in finalized
        if str(record.get("candidate_scope") or "") == "accepted_awc_hard_source_signal"
    ]
    first = accepted[0] if accepted else {}
    timing = first.get("best_timing") if isinstance(first.get("best_timing"), Mapping) else {}
    sources = set(first.get("sources") or [])
    signal_id = str(first.get("signal_id") or "").strip()
    fact_snapshot_ids = list(lineage_consistency.get("fact_snapshot_ids") or [])
    posterior_run_ids = list(lineage_consistency.get("posterior_run_ids") or [])
    terminal_state = str(first.get("final_terminal_state") or "").strip()
    nodes = {
        "source": {
            "ok": bool(timing.get("source_event_id") and timing.get("source_observation_id")),
            "source_event_id": str(timing.get("source_event_id") or ""),
            "source_observation_id": str(timing.get("source_observation_id") or ""),
        },
        "fact": {
            "ok": "event_fact_history" in sources and bool(fact_snapshot_ids),
            "fact_snapshot_ids": fact_snapshot_ids,
        },
        "posterior": {
            "ok": bool(posterior_run_ids),
            "posterior_run_ids": posterior_run_ids,
        },
        "decision": {
            "ok": "auto_trade_decisions" in sources and bool(first.get("decision_ids")),
            "decision_ids": list(first.get("decision_ids") or []),
        },
        "execution": {
            "ok": "auto_trade_jobs" in sources and terminal_state in FINAL_TERMINAL_STATES,
            "job_ids": list(first.get("job_ids") or []),
            "terminal_state": terminal_state,
        },
        "ui_snapshot": {
            "ok": "event_fact_history" in sources and bool(fact_snapshot_ids),
            "reason": "ui_snapshot_is_rendered_from_fact_snapshot",
            "ui_snapshot_id_policy": "event_fact_history.id_as_ui_snapshot_anchor",
        },
        "review": {
            "ok": bool(review_anchor.get("available")),
            "review_id": str(review_anchor.get("review_id") or ""),
            "reason": str(review_anchor.get("reason") or ""),
        },
    }
    missing_nodes = [node_id for node_id, node in nodes.items() if not bool(node.get("ok"))]
    blockers: List[str] = []
    if required and not accepted:
        blockers.append("awc_full_chain_no_accepted_signal")
    if required:
        blockers.extend(f"awc_full_chain_{node_id}_missing" for node_id in missing_nodes)
    return {
        "schema": "awc_signal_full_chain_replay_contract_v1",
        "enabled": bool(required),
        "read_only": True,
        "ok": not blockers,
        "required_chain": ["source", "fact", "posterior", "decision", "execution", "ui_snapshot", "review"],
        "accepted_signal_count": len(accepted),
        "signal_id": signal_id,
        "source_counts": dict(source_counts),
        "nodes": nodes,
        "missing_nodes": missing_nodes,
        "blockers": sorted(set(blockers)),
        "guards": {
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "historical_backfill": False,
        },
    }


def _chain_lineage_consistency(
    finalized: Sequence[Mapping[str, Any]],
    *,
    anchor_trace: Mapping[str, Any],
) -> Dict[str, Any]:
    resolved = anchor_trace.get("resolved") if isinstance(anchor_trace.get("resolved"), Mapping) else {}
    anchor_fact_snapshot_id = str(resolved.get("fact_snapshot_id") or "").strip()
    anchor_posterior_run_id = str(resolved.get("posterior_run_id") or "").strip()
    signal_summaries: List[Dict[str, Any]] = []
    fact_snapshot_ids: List[str] = []
    posterior_run_ids: List[str] = []
    blockers: List[str] = []
    warnings: List[str] = []

    def append_unique(target: List[str], value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in target:
            target.append(text)

    append_unique(fact_snapshot_ids, anchor_fact_snapshot_id)
    append_unique(posterior_run_ids, anchor_posterior_run_id)
    for record in finalized:
        consistency = dict(record.get("lineage_consistency") or {})
        signal_id = str(record.get("signal_id") or "").strip()
        for value in consistency.get("fact_snapshot_ids") or []:
            append_unique(fact_snapshot_ids, value)
            if anchor_fact_snapshot_id and str(value or "").strip() != anchor_fact_snapshot_id:
                blockers.append("anchor_fact_snapshot_id_mismatch")
        for value in consistency.get("posterior_run_ids") or []:
            append_unique(posterior_run_ids, value)
            if anchor_posterior_run_id and str(value or "").strip() != anchor_posterior_run_id:
                blockers.append("anchor_posterior_run_id_mismatch")
        blockers.extend(str(item) for item in consistency.get("blockers") or [])
        if consistency.get("missing_sources"):
            warnings.append("lineage_missing_from_some_sources")
        signal_summaries.append(
            {
                "signal_id": signal_id,
                "ok": bool(consistency.get("ok", True)),
                "fact_snapshot_ids": list(consistency.get("fact_snapshot_ids") or []),
                "posterior_run_ids": list(consistency.get("posterior_run_ids") or []),
                "observed_sources": list(consistency.get("observed_sources") or []),
                "missing_sources": list(consistency.get("missing_sources") or []),
                "blockers": list(consistency.get("blockers") or []),
            }
        )

    return {
        "schema": "ssot_chain_lineage_consistency_v1",
        "read_only": True,
        "ok": not blockers,
        "anchor_fact_snapshot_id": anchor_fact_snapshot_id,
        "anchor_posterior_run_id": anchor_posterior_run_id,
        "fact_snapshot_ids": fact_snapshot_ids,
        "posterior_run_ids": posterior_run_ids,
        "signal_count": len(finalized),
        "signals": signal_summaries,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
    }


def _timing_key_present(timing: Mapping[str, Any], key: str) -> bool:
    for candidate in TIMING_KEY_ALIASES.get(key, (key,)):
        if candidate not in timing:
            continue
        value = timing.get(candidate)
        if value is None or value == "":
            continue
        return True
    return False


def _coverage_for_timing_keys(records: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> Dict[str, Any]:
    per_key: Dict[str, Dict[str, Any]] = {}
    missing_by_signal: Dict[str, List[str]] = {}
    complete_signal_ids: List[str] = []
    for key in keys:
        per_key[key] = {
            "present": 0,
            "missing": 0,
            "missing_signal_ids": [],
        }
    for record in records:
        signal_id = str(record.get("signal_id") or "").strip()
        timing = record.get("best_timing") if isinstance(record.get("best_timing"), Mapping) else {}
        missing_keys: List[str] = []
        for key in keys:
            present = isinstance(timing, Mapping) and _timing_key_present(timing, key)
            if present:
                per_key[key]["present"] += 1
            else:
                per_key[key]["missing"] += 1
                if signal_id:
                    per_key[key]["missing_signal_ids"].append(signal_id)
                    missing_keys.append(key)
        if missing_keys:
            if signal_id:
                missing_by_signal[signal_id] = missing_keys
        elif signal_id:
            complete_signal_ids.append(signal_id)
    total = len(records)
    incomplete_signal_ids = sorted(missing_by_signal)
    return {
        "required_keys": list(keys),
        "total_signals": total,
        "complete_signal_count": len(complete_signal_ids),
        "incomplete_signal_count": len(incomplete_signal_ids),
        "complete_signal_ids": complete_signal_ids,
        "incomplete_signal_ids": incomplete_signal_ids,
        "missing_by_signal": missing_by_signal,
        "per_key": per_key,
        "ok": total > 0 and not incomplete_signal_ids,
    }


def _signal_to_order_production_coverage(
    finalized: Sequence[Mapping[str, Any]],
    *,
    source_scans: Mapping[str, Any],
    chain_coverage: Mapping[str, Any],
) -> Dict[str, Any]:
    accepted = [
        record
        for record in finalized
        if str(record.get("candidate_scope") or "") == "accepted_awc_hard_source_signal"
    ]
    timing_required = [
        record
        for record in accepted
        if not bool(record.get("legacy_runtime_record"))
        and not _reviewed_no_action_without_decision(
            final_state=str(record.get("final_terminal_state") or ""),
            best_terminal=record.get("best_terminal") if isinstance(record.get("best_terminal"), Mapping) else {},
            best_timing=record.get("best_timing") if isinstance(record.get("best_timing"), Mapping) else {},
        )
    ]
    submitted = [
        record
        for record in accepted
        if str(record.get("final_terminal_state") or "") == "submitted"
        or _timing_key_present(
            record.get("best_timing") if isinstance(record.get("best_timing"), Mapping) else {},
            "order_submitted_at",
        )
        or _timing_key_present(
            record.get("best_timing") if isinstance(record.get("best_timing"), Mapping) else {},
            "exchange_ack_at",
        )
    ]
    terminal_missing_ids: List[str] = []
    terminal_non_final_ids: List[str] = []
    terminal_reason_missing_ids: List[str] = []
    for record in accepted:
        signal_id = str(record.get("signal_id") or "").strip()
        state = str(record.get("final_terminal_state") or "").strip()
        timing = record.get("best_timing") if isinstance(record.get("best_timing"), Mapping) else {}
        terminal = record.get("best_terminal") if isinstance(record.get("best_terminal"), Mapping) else {}
        reason = str(
            (terminal or {}).get("reason")
            or (timing or {}).get("terminal_reason")
            or (timing or {}).get("signal_to_order_slo_violation_reason")
            or ""
        ).strip()
        if not state:
            terminal_missing_ids.append(signal_id)
        elif state not in FINAL_TERMINAL_STATES:
            terminal_non_final_ids.append(signal_id)
        if state in FINAL_TERMINAL_STATES and not reason:
            terminal_reason_missing_ids.append(signal_id)
    tables = chain_coverage.get("tables") if isinstance(chain_coverage.get("tables"), Mapping) else {}
    ledger_table_present = bool(tables.get("signal_to_order_timing"))
    ledger_source_signal_ids = [
        str(record.get("signal_id") or "").strip()
        for record in accepted
        if "signal_to_order_timing" in set(record.get("sources") or [])
    ]
    accepted_signal_ids = [str(record.get("signal_id") or "").strip() for record in accepted]
    accepted_with_ledger = set(ledger_source_signal_ids)
    missing_ledger_signal_ids = [
        signal_id
        for signal_id in accepted_signal_ids
        if signal_id and signal_id not in accepted_with_ledger
    ]
    base_timing = _coverage_for_timing_keys(timing_required, ACCEPTED_SIGNAL_BASE_TIMING_KEYS)
    preparation = _coverage_for_timing_keys(timing_required, PREPARATION_TIMING_KEYS)
    submitted_execution = _coverage_for_timing_keys(submitted, SUBMITTED_EXECUTION_TIMING_KEYS)
    prep_incomplete_ids = [
        str(record.get("signal_id") or "").strip()
        for record in timing_required
        if str(record.get("pre_signal_prepare_state") or "").strip() == "preparation_partial"
        or bool(record.get("pre_signal_prepare_missing_components"))
        or (
            isinstance(record.get("best_timing"), Mapping)
            and record["best_timing"].get("pre_signal_prepare_complete") is False
        )
    ]
    prep_missing_ids = [
        str(record.get("signal_id") or "").strip()
        for record in timing_required
        if not str(record.get("pre_signal_prepare_state") or "").strip()
    ]
    prep_after_deadline_ids = [
        str(record.get("signal_id") or "").strip()
        for record in timing_required
        if isinstance(record.get("best_timing"), Mapping)
        and record["best_timing"].get("pre_signal_prepared_before_deadline") is False
    ]
    prep_required_component_incomplete_ids = [
        str(record.get("signal_id") or "").strip()
        for record in timing_required
        if "pre_signal_required_component_contract_incomplete" in set(record.get("blockers") or [])
    ]
    submitted_precheck_order_blocker_ids = [
        str(record.get("signal_id") or "").strip()
        for record in submitted
        if "submitted_precheck_after_submit_request" in set(record.get("blockers") or [])
    ]
    blockers: List[str] = []
    if accepted and not ledger_table_present:
        blockers.append("signal_to_order_timing_table_missing")
    if accepted and missing_ledger_signal_ids:
        blockers.append("accepted_signal_missing_signal_to_order_ledger")
    if terminal_missing_ids or terminal_non_final_ids or terminal_reason_missing_ids:
        blockers.append("accepted_signal_terminal_coverage_incomplete")
    if timing_required and not base_timing.get("ok"):
        blockers.append("accepted_signal_base_timing_incomplete")
    if timing_required and (
        prep_incomplete_ids
        or prep_missing_ids
        or prep_after_deadline_ids
        or prep_required_component_incomplete_ids
        or not preparation.get("ok")
    ):
        blockers.append("accepted_signal_preparation_coverage_incomplete")
    if submitted and (
        not submitted_execution.get("ok") or submitted_precheck_order_blocker_ids
    ):
        blockers.append("submitted_signal_execution_safety_coverage_incomplete")
    ledger_scan = source_scans.get("signal_to_order_timing") if isinstance(source_scans.get("signal_to_order_timing"), Mapping) else {}
    return {
        "schema": "signal_to_order_production_coverage_v1",
        "read_only": True,
        "ok": bool(accepted) and not blockers,
        "coverage_blockers": sorted(set(blockers)),
        "coverage_blocker_counts": _counter_dict(blockers) if blockers else {},
        "accepted_signal_count": len(accepted),
        "accepted_signal_ids": accepted_signal_ids,
        "timing_required_signal_count": len(timing_required),
        "timing_required_signal_ids": [str(record.get("signal_id") or "").strip() for record in timing_required],
        "submitted_signal_count": len(submitted),
        "submitted_signal_ids": [str(record.get("signal_id") or "").strip() for record in submitted],
        "terminal_coverage": {
            "ok": bool(accepted) and not terminal_missing_ids and not terminal_non_final_ids and not terminal_reason_missing_ids,
            "final_terminal_states": sorted(FINAL_TERMINAL_STATES),
            "terminal_state_counts": _counter_dict(record.get("final_terminal_state") for record in accepted),
            "missing_terminal_signal_ids": terminal_missing_ids,
            "non_final_terminal_signal_ids": terminal_non_final_ids,
            "missing_terminal_reason_signal_ids": terminal_reason_missing_ids,
        },
        "base_timing_coverage": base_timing,
        "preparation_coverage": {
            **preparation,
            "missing_preparation_signal_ids": prep_missing_ids,
            "incomplete_preparation_signal_ids": prep_incomplete_ids,
            "after_deadline_signal_ids": prep_after_deadline_ids,
            "required_component_incomplete_signal_ids": prep_required_component_incomplete_ids,
            "required_components": list(PRE_SIGNAL_REQUIRED_COMPONENTS),
        },
        "submitted_execution_coverage": submitted_execution,
        "safety_gate_coverage": {
            "ok": bool(submitted) and submitted_execution.get("ok") and not submitted_precheck_order_blocker_ids,
            "submitted_signal_count": len(submitted),
            "required_keys": list(SUBMITTED_EXECUTION_TIMING_KEYS),
            "precheck_after_submit_request_signal_ids": submitted_precheck_order_blocker_ids,
            "per_key": submitted_execution.get("per_key", {}),
            "missing_by_signal": submitted_execution.get("missing_by_signal", {}),
        },
        "ledger_coverage": {
            "ok": bool(accepted) and ledger_table_present and not missing_ledger_signal_ids,
            "table_present": ledger_table_present,
            "source_scan": dict(ledger_scan),
            "accepted_with_ledger_source_count": len(ledger_source_signal_ids),
            "missing_ledger_signal_ids": missing_ledger_signal_ids,
        },
    }


def audit(
    db_path: Path | str = DEFAULT_DB,
    *,
    slug: str = "",
    event_date: str = "",
    since: str = "",
    since_hours: float = 48.0,
    signal_id: str = "",
    order_id: str = "",
    auto_order_id: str = "",
    decision_id: str = "",
    job_id: str = "",
    fact_id: str = "",
    ui_snapshot_id: str = "",
    source_event_id: str = "",
    source_observation_id: str = "",
    posterior_run_id: str = "",
    review_id: str = "",
    limit: int = 500,
    max_signals_output: int = 50,
    include_details: bool = False,
    fail_legacy_artifacts: bool = False,
    require_awc_full_chain: bool = False,
) -> Dict[str, Any]:
    db = Path(db_path)
    since_value = str(since or "").strip() or _utc_cutoff(float(since_hours))
    records: Dict[str, Dict[str, Any]] = {}
    anchor_trace: Dict[str, Any] = {}
    with _connect_readonly(db) as conn:
        anchor_trace = _resolve_anchor_context(
            conn,
            order_id=order_id,
            auto_order_id=auto_order_id,
            decision_id=decision_id,
            job_id=job_id,
            fact_id=fact_id,
            ui_snapshot_id=ui_snapshot_id,
            source_event_id=source_event_id,
            source_observation_id=source_observation_id,
            posterior_run_id=posterior_run_id,
            review_id=review_id,
            slug=slug,
            event_date=event_date,
            signal_id=signal_id,
        )
        anchor_provided = bool(anchor_trace.get("anchor_provided"))
        resolved = anchor_trace.get("resolved") if isinstance(anchor_trace.get("resolved"), Mapping) else {}
        effective_slug = str(resolved.get("slug") or slug or "")
        effective_event_date = str(resolved.get("event_date") or event_date or "")
        effective_signal_id = str(resolved.get("signal_id") or signal_id or "")
        effective_decision_id = str(resolved.get("decision_id") or decision_id or "")
        effective_fact_id = str(resolved.get("fact_id") or fact_id or ui_snapshot_id or "")
        effective_job_id = str(resolved.get("job_id") or job_id or "")
        effective_run_id = str(resolved.get("run_id") or "")
        effective_source_event_id = str(resolved.get("source_event_id") or source_event_id or "")
        effective_source_observation_id = str(
            resolved.get("source_observation_id") or source_observation_id or ""
        )
        effective_posterior_run_id = str(resolved.get("posterior_run_id") or posterior_run_id or "")
        effective_review_id = str(resolved.get("review_id") or review_id or "")
        alternate_anchor_filter = bool(
            effective_source_event_id
            or effective_source_observation_id
            or effective_posterior_run_id
            or effective_fact_id
            or effective_decision_id
            or effective_job_id
            or effective_run_id
        )
        if anchor_provided and not effective_signal_id and not alternate_anchor_filter:
            effective_signal_id = "__anchor_without_awc_signal__"
        signal_ledger_filter = effective_signal_id
        if (
            anchor_provided
            and not signal_ledger_filter
            and not effective_source_event_id
            and not effective_source_observation_id
        ):
            signal_ledger_filter = "__anchor_without_awc_signal__"
        skip_unindexed_fact_scan = bool(
            anchor_provided
            and not effective_fact_id
            and not effective_source_event_id
            and not effective_source_observation_id
            and not effective_posterior_run_id
        )
        skip_unindexed_decision_scan = bool(
            anchor_provided
            and not effective_signal_id
            and not effective_decision_id
            and not effective_run_id
        )
        skip_unindexed_job_scan = bool(
            anchor_provided
            and not effective_signal_id
            and not effective_run_id
            and not effective_job_id
        )
        sources = {
            "signal_to_order_timing": _collect_from_signal_ledger(
                conn,
                records,
                slug=effective_slug,
                event_date=effective_event_date,
                since=since_value,
                signal_id=signal_ledger_filter,
                source_event_id=effective_source_event_id,
                source_observation_id=effective_source_observation_id,
                limit=limit,
            ),
            "event_fact_history": _collect_from_facts(
                conn,
                records,
                slug=effective_slug,
                event_date=effective_event_date,
                since=since_value,
                signal_id=effective_signal_id,
                fact_id=effective_fact_id,
                source_event_id=effective_source_event_id,
                source_observation_id=effective_source_observation_id,
                posterior_run_id=effective_posterior_run_id,
                skip_unindexed_scan=skip_unindexed_fact_scan,
                limit=limit,
            ),
            "auto_trade_decisions": _collect_from_decisions(
                conn,
                records,
                slug=effective_slug,
                event_date=effective_event_date,
                since=since_value,
                signal_id=effective_signal_id,
                decision_id=effective_decision_id,
                run_id=effective_run_id,
                skip_unindexed_scan=skip_unindexed_decision_scan,
                limit=limit,
            ),
            "auto_trade_jobs": _collect_from_jobs(
                conn,
                records,
                slug=effective_slug,
                event_date=effective_event_date,
                since=since_value,
                signal_id=effective_signal_id,
                run_id=effective_run_id,
                job_id=effective_job_id,
                skip_unindexed_scan=skip_unindexed_job_scan,
                limit=limit,
            ),
        }
        sources["signal_to_order_timing_signal_id_backfill"] = _collect_signal_ledger_for_existing_records(
            conn,
            records,
            since=since_value,
            limit=limit,
        )
        sources["auto_trade_decisions_signal_id_backfill"] = _collect_decisions_for_existing_records(
            conn,
            records,
            since=since_value,
            limit=limit,
        )
        coverage = _chain_coverage(conn)
    current_runtime_contract_version = _current_hard_source_runtime_contract_version()
    finalized = [
        _finalize_signal(
            record,
            current_runtime_contract_version=current_runtime_contract_version,
            fail_legacy_artifacts=fail_legacy_artifacts,
        )
        for record in records.values()
    ]
    finalized.sort(key=lambda item: str(item.get("awc_hard_source_signal_at") or item.get("signal_id") or ""))
    blockers = sorted({blocker for record in finalized for blocker in record.get("blockers", [])})
    warnings = sorted({warning for record in finalized for warning in record.get("warnings", [])})
    synthetic_blockers: List[str] = []
    synthetic_warnings: List[str] = []
    if not finalized:
        anchor_provided = bool(anchor_trace.get("anchor_provided"))
        resolved = anchor_trace.get("resolved") if isinstance(anchor_trace.get("resolved"), Mapping) else {}
        hard_source_expected = resolved.get("hard_source_signal_expected")
        anchor_not_applicable = bool(resolved.get("anchor_not_applicable"))
        if anchor_provided and anchor_not_applicable:
            pass
        elif anchor_provided and hard_source_expected is False:
            synthetic_warnings.append("anchor_has_no_awc_signal_expected")
        elif anchor_provided and hard_source_expected is True:
            synthetic_blockers.append("anchor_signal_id_missing")
        elif anchor_provided:
            synthetic_warnings.append("anchor_signal_expectation_unknown")
        else:
            synthetic_blockers.append("no_awc_hard_source_signals_found")
    blockers.extend(synthetic_blockers)
    warnings.extend(synthetic_warnings)
    blockers.extend(str(item) for item in anchor_trace.get("blockers") or [])
    warnings.extend(str(item) for item in anchor_trace.get("warnings") or [])
    blockers = sorted(set(blockers))
    warnings = sorted(set(warnings))
    blocker_counts = _counter_dict(
        blocker for record in finalized for blocker in record.get("blockers", [])
    )
    for blocker in set(synthetic_blockers + [str(item) for item in anchor_trace.get("blockers") or []]):
        blocker_counts[str(blocker)] = int(blocker_counts.get(str(blocker), 0)) + 1
    warning_counts = _counter_dict(
        warning for record in finalized for warning in record.get("warnings", [])
    )
    for warning in set(synthetic_warnings + [str(item) for item in anchor_trace.get("warnings") or []]):
        warning_counts[str(warning)] = int(warning_counts.get(str(warning), 0)) + 1
    source_counts = _counter_dict(
        source for record in finalized for source in _unique_ordered(record.get("sources") or [])
    )
    lineage_consistency = _chain_lineage_consistency(finalized, anchor_trace=anchor_trace)
    blockers.extend(str(item) for item in lineage_consistency.get("blockers") or [])
    blockers = sorted(set(blockers))
    warnings = sorted(set(warnings))
    for blocker in set(str(item) for item in lineage_consistency.get("blockers") or []):
        blocker_counts.setdefault(str(blocker), 1)
    production_coverage = _signal_to_order_production_coverage(
        finalized,
        source_scans=sources,
        chain_coverage=coverage,
    )
    with _connect_readonly(db) as conn:
        review_anchor = _review_anchor_for_fact_snapshot_ids(
            conn,
            fact_snapshot_ids=list(lineage_consistency.get("fact_snapshot_ids") or []),
            slug=str((anchor_trace.get("resolved") or {}).get("slug") or slug or ""),
            event_date=str((anchor_trace.get("resolved") or {}).get("event_date") or event_date or ""),
        )
    awc_full_chain_replay = _awc_full_chain_replay_contract(
        finalized,
        source_counts=source_counts,
        lineage_consistency=lineage_consistency,
        chain_coverage=coverage,
        review_anchor=review_anchor,
        required=bool(require_awc_full_chain),
    )
    if require_awc_full_chain and not awc_full_chain_replay.get("ok"):
        blockers.extend(str(item) for item in awc_full_chain_replay.get("blockers") or [])
        blockers = sorted(set(blockers))
        for blocker in set(str(item) for item in awc_full_chain_replay.get("blockers") or []):
            blocker_counts[str(blocker)] = int(blocker_counts.get(str(blocker), 0)) + 1
    output_limit = max(0, int(max_signals_output))
    visible_records = finalized[:output_limit] if output_limit else []
    result = {
        "ok": not blockers,
        "schema": "ssot_chain_replay_audit_v1",
        "read_only": True,
        "db": str(db),
        "current_hard_source_runtime_contract_version": current_runtime_contract_version,
        "fail_legacy_artifacts": bool(fail_legacy_artifacts),
        "slug": slug,
        "event_date": event_date,
        "effective_slug": str((anchor_trace.get("resolved") or {}).get("slug") or slug or ""),
        "effective_event_date": str((anchor_trace.get("resolved") or {}).get("event_date") or event_date or ""),
        "effective_signal_id": str((anchor_trace.get("resolved") or {}).get("signal_id") or signal_id or ""),
        "effective_source_event_id": str(
            (anchor_trace.get("resolved") or {}).get("source_event_id") or source_event_id or ""
        ),
        "effective_source_observation_id": str(
            (anchor_trace.get("resolved") or {}).get("source_observation_id") or source_observation_id or ""
        ),
        "effective_posterior_run_id": str(
            (anchor_trace.get("resolved") or {}).get("posterior_run_id") or posterior_run_id or ""
        ),
        "effective_review_id": str((anchor_trace.get("resolved") or {}).get("review_id") or review_id or ""),
        "since": since_value,
        "signal_count": len(finalized),
        "signal_output_count": len(visible_records),
        "signal_output_truncated": len(finalized) > len(visible_records),
        "blockers": blockers,
        "blocker_counts": blocker_counts,
        "warnings": warnings,
        "warning_counts": warning_counts,
        "candidate_scope_counts": _counter_dict(record.get("candidate_scope") for record in finalized),
        "terminal_state_counts": _counter_dict(record.get("final_terminal_state") for record in finalized),
        "pre_signal_prepare_state_counts": _counter_dict(
            record.get("pre_signal_prepare_state") for record in finalized
        ),
        "source_counts": source_counts,
        "lineage_consistency": lineage_consistency,
        "runtime_contract_version_counts": _counter_dict(
            version
            for record in finalized
            for version in _unique_ordered(record.get("hard_source_runtime_contract_versions") or [])
        ),
        "source_scans": sources,
        "chain_coverage": coverage,
        "production_coverage": production_coverage,
        "awc_full_chain_replay": awc_full_chain_replay,
        "anchor_trace": anchor_trace,
        "signals": [_signal_summary(record) for record in visible_records],
        "next_boundary": "fix_first_blocker_or_extend_replay_fixture",
    }
    if include_details:
        result["signal_details"] = visible_records
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--signal-id", default="")
    parser.add_argument("--order-id", default="")
    parser.add_argument("--auto-order-id", default="")
    parser.add_argument("--decision-id", default="")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--fact-id", default="")
    parser.add_argument("--ui-snapshot-id", default="")
    parser.add_argument("--source-event-id", default="")
    parser.add_argument("--source-observation-id", default="")
    parser.add_argument("--posterior-run-id", default="")
    parser.add_argument("--review-id", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--since-hours", type=float, default=48.0)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--max-signals-output", type=int, default=50)
    parser.add_argument("--include-details", action="store_true")
    parser.add_argument("--fail-legacy-artifacts", action="store_true")
    parser.add_argument("--require-awc-full-chain", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    result = audit(
        args.db,
        slug=args.slug,
        event_date=args.event_date,
        since=args.since,
        since_hours=args.since_hours,
        signal_id=args.signal_id,
        order_id=args.order_id,
        auto_order_id=args.auto_order_id,
        decision_id=args.decision_id,
        job_id=args.job_id,
        fact_id=args.fact_id,
        ui_snapshot_id=args.ui_snapshot_id,
        source_event_id=args.source_event_id,
        source_observation_id=args.source_observation_id,
        posterior_run_id=args.posterior_run_id,
        review_id=args.review_id,
        limit=args.limit,
        max_signals_output=args.max_signals_output,
        include_details=args.include_details,
        fail_legacy_artifacts=args.fail_legacy_artifacts,
        require_awc_full_chain=args.require_awc_full_chain,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
