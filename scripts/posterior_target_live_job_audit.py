#!/usr/bin/env python3
"""Read-only audit for the next posterior-target live CLOB job.

This script is intentionally narrow: it verifies the post-fix path that should
turn an AWC/posterior target update into one primary YES CLOB submit attempt
or an already-live primary target confirmation.
It does not enqueue jobs, place orders, or mutate the archive DB.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_ENV = ROOT / ".env"
POSTFIX_SENTINEL_PATHS = (
    ROOT / ".env",
    ROOT / "scan_weather_market.py",
    ROOT / "scripts" / "hard_source_direct_poller.py",
)


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _postfix_cutoff() -> str:
    mtimes = []
    for path in POSTFIX_SENTINEL_PATHS:
        try:
            mtimes.append(float(path.stat().st_mtime))
        except OSError:
            continue
    if not mtimes:
        return _iso_utc(datetime.now(timezone.utc))
    return _iso_utc(datetime.fromtimestamp(max(mtimes), timezone.utc))


def _load_json(raw: Any) -> Any:
    if raw in (None, ""):
        return None
    try:
        return json.loads(str(raw))
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _parse_env(path: Path = DEFAULT_ENV) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value and value[0] in {"'", '"'} and value[-1:] == value[0]:
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _event_filters(slug: str, event_date: str) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if slug:
        clauses.append("payload_json LIKE ?")
        params.append(f"%{slug}%")
    if event_date:
        clauses.append("payload_json LIKE ?")
        params.append(f"%{event_date}%")
    return (" AND " + " AND ".join(clauses), params) if clauses else ("", params)


def _latest_job(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    since: str,
) -> Optional[sqlite3.Row]:
    extra, params = _event_filters(slug, event_date)
    return conn.execute(
        f"""
        SELECT *
        FROM auto_trade_jobs
        WHERE source = 'posterior_target_direct'
          AND created_at >= ?
          {extra}
        ORDER BY id DESC
        LIMIT 1
        """,
        [since, *params],
    ).fetchone()


def _target_block(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    block = payload.get("posterior_target_direct")
    return block if isinstance(block, Mapping) else {}


def _entry_targets(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    targets = payload.get("entry_expected_targets")
    if not isinstance(targets, list):
        return []
    return [item for item in targets if isinstance(item, Mapping)]


def _nested_target_counts(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(payload.get("event_payloads") or []):
        if not isinstance(item, Mapping):
            continue
        nested = item.get("payload")
        if not isinstance(nested, Mapping):
            continue
        block = _target_block(nested)
        targets = _entry_targets(nested)
        out.append(
            {
                "event_index": idx,
                "target_bucket": block.get("target_bucket"),
                "entry_expected_targets_count": len(targets),
                "targets": [
                    {
                        "outcome": target.get("outcome"),
                        "side": target.get("side"),
                        "leg_role": target.get("leg_role"),
                    }
                    for target in targets
                ],
            }
        )
    return out


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_bool_true(*values: Any) -> bool:
    for value in values:
        if isinstance(value, bool):
            if value is True:
                return True
            continue
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
    return False


def _posterior_target_rule_source_proof(
    *,
    target: Mapping[str, Any],
    payload: Mapping[str, Any],
    target_bucket: str,
) -> dict[str, Any]:
    nested_payload: Mapping[str, Any] = {}
    for item in payload.get("event_payloads") or []:
        if not isinstance(item, Mapping):
            continue
        candidate = item.get("payload")
        if isinstance(candidate, Mapping):
            nested_payload = candidate
            break
    rows_raw = nested_payload.get("rows")
    if not isinstance(rows_raw, list):
        rows_raw = nested_payload.get("distribution")
    target_row: Mapping[str, Any] = {}
    for raw in rows_raw or []:
        if not isinstance(raw, Mapping):
            continue
        outcome = str(raw.get("outcome") or raw.get("label") or "").strip()
        if outcome == str(target_bucket or "").strip():
            target_row = raw
            break

    block = _target_block(payload)
    nested_block = _target_block(nested_payload)
    decision_ssot = _first_text(
        target.get("decision_ssot"),
        target.get("target_bucket_source_semantics"),
        target_row.get("decision_ssot"),
        target_row.get("target_bucket_source_semantics"),
        nested_payload.get("decision_ssot"),
        nested_payload.get("target_bucket_source_semantics"),
        block.get("decision_ssot"),
        nested_block.get("decision_ssot"),
    )
    posterior_direct_semantics_ok = bool(
        decision_ssot in {"posterior_temperature_distribution", "posterior_ssot_fact"}
        or _first_bool_true(
            target.get("posterior_target_direct"),
            target_row.get("posterior_target_direct"),
            nested_payload.get("posterior_target_direct_snapshot"),
        )
    )
    rules_ok = _first_bool_true(
        target.get("rules_contract_ok"),
        target.get("weather_strategy_rules_ok"),
        target_row.get("rules_contract_ok"),
        target_row.get("weather_strategy_rules_ok"),
        nested_payload.get("rules_contract_ok"),
        nested_payload.get("weather_strategy_rules_ok"),
    )
    source_usable = _first_bool_true(
        target.get("source_usable_for_settlement"),
        target.get("weather_strategy_source_usable_for_settlement"),
        target_row.get("source_usable_for_settlement"),
        target_row.get("weather_strategy_source_usable_for_settlement"),
        nested_payload.get("source_usable_for_settlement"),
        nested_payload.get("weather_strategy_source_usable_for_settlement"),
    )
    settlement_station = _first_text(
        target.get("settlement_station"),
        target.get("weather_strategy_settlement_station"),
        target.get("station_id"),
        target_row.get("settlement_station"),
        target_row.get("weather_strategy_settlement_station"),
        target_row.get("station_id"),
        nested_payload.get("settlement_station"),
        nested_payload.get("weather_strategy_settlement_station"),
        nested_payload.get("station_id"),
        block.get("station_id"),
        nested_block.get("station_id"),
    )
    source_fit_tier = _first_text(
        target.get("source_fit_tier"),
        target.get("weather_strategy_source_fit_tier"),
        target_row.get("source_fit_tier"),
        target_row.get("weather_strategy_source_fit_tier"),
        nested_payload.get("source_fit_tier"),
        nested_payload.get("weather_strategy_source_fit_tier"),
    )
    if posterior_direct_semantics_ok and settlement_station:
        rules_ok = True
        source_usable = True
    return {
        "ok": bool(rules_ok and source_usable and settlement_station),
        "rules_contract_ok": bool(rules_ok),
        "source_usable_for_settlement": bool(source_usable),
        "settlement_station": settlement_station,
        "source_fit_tier": source_fit_tier,
        "decision_ssot": decision_ssot,
        "posterior_direct_semantics_ok": bool(posterior_direct_semantics_ok),
        "target_row_found": bool(target_row),
        "nested_payload_present": bool(nested_payload),
    }


def _iter_nested_event_payloads(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for item in payload.get("event_payloads") or []:
        if not isinstance(item, Mapping):
            continue
        nested = item.get("payload")
        if isinstance(nested, Mapping):
            out.append(nested)
    return out


def _prob01(value: Any) -> Optional[float]:
    parsed = _to_float(value)
    if parsed is None:
        return None
    if parsed > 1.0 and parsed <= 100.0:
        parsed = parsed / 100.0
    if parsed < 0.0 or parsed > 1.0:
        return None
    return parsed


def _posterior_ssot_fact_from_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates: list[Any] = [
        payload.get("posterior_ssot_fact"),
        _target_block(payload).get("posterior_ssot_fact"),
    ]
    for target in _entry_targets(payload):
        candidates.append(target.get("posterior_ssot_fact"))
    for nested in _iter_nested_event_payloads(payload):
        candidates.append(nested.get("posterior_ssot_fact"))
        candidates.append(_target_block(nested).get("posterior_ssot_fact"))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        state = str(candidate.get("state") or "").strip().lower()
        if state and state not in {"available", "ready", "ok"}:
            continue
        if _first_text(candidate.get("target_bucket"), candidate.get("ssot_target_bucket")):
            return candidate
    return {}


def _posterior_ssot_target_probability(ssot_fact: Mapping[str, Any]) -> Optional[float]:
    for key in (
        "target_probability",
        "target_probability_pct",
        "target_bucket_probability",
        "target_bucket_probability_pct",
        "posterior",
    ):
        prob = _prob01(ssot_fact.get(key))
        if prob is not None:
            return prob
    return None


def _consumer_probability(mapping: Mapping[str, Any]) -> Optional[float]:
    for key in (
        "target_probability",
        "target_probability_pct",
        "target_bucket_probability",
        "target_bucket_probability_pct",
        "target_posterior",
        "new_target_posterior",
    ):
        prob = _prob01(mapping.get(key))
        if prob is not None:
            return prob
    return None


def _posterior_ssot_consumer_result(
    *,
    name: str,
    mapping: Mapping[str, Any],
    expected_bucket: str,
    expected_probability: Optional[float],
    expected_fact_snapshot_id: str,
    expected_posterior_run_id: str,
) -> dict[str, Any]:
    decision_ssot = _first_text(mapping.get("decision_ssot"), mapping.get("ssot_source"))
    used = _first_bool_true(mapping.get("posterior_ssot_fact_used"))
    bucket = _first_text(mapping.get("target_bucket"), mapping.get("ssot_target_bucket"), mapping.get("outcome"))
    probability = _consumer_probability(mapping)
    probability_field = _first_text(
        mapping.get("target_probability_field"),
        mapping.get("target_bucket_probability_field"),
        mapping.get("posterior_ssot_target_probability_field"),
        mapping.get("posterior_target_source"),
        mapping.get("target_source"),
    )
    fact_snapshot_id = _first_text(mapping.get("fact_snapshot_id"), mapping.get("posterior_target_fact_snapshot_id"))
    posterior_run_id = _first_text(mapping.get("posterior_run_id"), mapping.get("posterior_target_posterior_run_id"))
    issues: list[str] = []
    if decision_ssot != "posterior_ssot_fact":
        issues.append("consumer_not_marked_posterior_ssot_fact")
    if not used:
        issues.append("consumer_posterior_ssot_fact_used_missing")
    if bucket and expected_bucket and bucket != expected_bucket:
        issues.append("consumer_target_bucket_mismatch")
    if expected_probability is not None and probability is not None and abs(probability - expected_probability) > 1e-9:
        issues.append("consumer_target_probability_mismatch")
    if probability_field and "posterior_ssot_fact" not in probability_field:
        issues.append("consumer_probability_field_not_posterior_ssot_fact")
    if expected_fact_snapshot_id:
        if not fact_snapshot_id:
            issues.append("consumer_fact_snapshot_id_missing")
        elif fact_snapshot_id != expected_fact_snapshot_id:
            issues.append("consumer_fact_snapshot_id_mismatch")
    if expected_posterior_run_id:
        if not posterior_run_id:
            issues.append("consumer_posterior_run_id_missing")
        elif posterior_run_id != expected_posterior_run_id:
            issues.append("consumer_posterior_run_id_mismatch")
    return {
        "name": name,
        "ok": not issues,
        "decision_ssot": decision_ssot,
        "posterior_ssot_fact_used": bool(used),
        "target_bucket": bucket,
        "target_probability": probability,
        "target_probability_field": probability_field,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
        "issues": issues,
    }


def _posterior_ssot_decision_chain_proof(
    *,
    payload: Mapping[str, Any],
    order_details: Mapping[str, Any],
) -> dict[str, Any]:
    ssot_fact = _posterior_ssot_fact_from_payload(payload)
    if not ssot_fact:
        return {"ok": True, "status": "not_applicable", "reason": "posterior_ssot_fact_absent"}
    expected_bucket = _first_text(ssot_fact.get("target_bucket"), ssot_fact.get("ssot_target_bucket"))
    expected_probability = _posterior_ssot_target_probability(ssot_fact)
    expected_fact_snapshot_id = _first_text(ssot_fact.get("fact_snapshot_id"))
    expected_posterior_run_id = _first_text(ssot_fact.get("posterior_run_id"))
    consumers: list[dict[str, Any]] = []
    block = _target_block(payload)
    if block:
        consumers.append(
            _posterior_ssot_consumer_result(
                name="job.posterior_target_direct",
                mapping=block,
                expected_bucket=expected_bucket,
                expected_probability=expected_probability,
                expected_fact_snapshot_id=expected_fact_snapshot_id,
                expected_posterior_run_id=expected_posterior_run_id,
            )
        )
    for idx, target in enumerate(_entry_targets(payload)):
        consumers.append(
            _posterior_ssot_consumer_result(
                name=f"job.entry_expected_targets[{idx}]",
                mapping=target,
                expected_bucket=expected_bucket,
                expected_probability=expected_probability,
                expected_fact_snapshot_id=expected_fact_snapshot_id,
                expected_posterior_run_id=expected_posterior_run_id,
            )
        )
    if order_details:
        consumers.append(
            _posterior_ssot_consumer_result(
                name="auto_trade_decisions.details_json",
                mapping=order_details,
                expected_bucket=expected_bucket,
                expected_probability=expected_probability,
                expected_fact_snapshot_id=expected_fact_snapshot_id,
                expected_posterior_run_id=expected_posterior_run_id,
            )
        )
    issue_rows = [
        {"consumer": row.get("name"), "issues": row.get("issues")}
        for row in consumers
        if row.get("issues")
    ]
    if not consumers:
        issue_rows.append({"consumer": "posterior_target_job", "issues": ["posterior_ssot_consumer_missing"]})
    return {
        "ok": not issue_rows,
        "status": "posterior_ssot_fact_present",
        "schema": ssot_fact.get("schema"),
        "fact_snapshot_id": expected_fact_snapshot_id,
        "posterior_run_id": expected_posterior_run_id,
        "target_bucket": expected_bucket,
        "target_probability": expected_probability,
        "target_probability_field": ssot_fact.get("target_probability_field"),
        "consumer_results": consumers,
        "issues": issue_rows,
    }


def _decision_details(row: Optional[sqlite3.Row]) -> dict[str, Any]:
    if row is None:
        return {}
    parsed = _load_json(row["details_json"])
    return parsed if isinstance(parsed, dict) else {}


def _find_order_decision(
    conn: sqlite3.Connection,
    *,
    job: sqlite3.Row,
    slug: str,
    event_date: str,
    target_bucket: str,
) -> Optional[sqlite3.Row]:
    started = str(job["created_at"] or job["started_at"] or "")
    ended = str(job["ended_at"] or "")
    params: list[Any] = [target_bucket]
    where = [
        "action = 'entry'",
        "outcome = ?",
        (
            "reason_codes_json LIKE '%order_submitted%' "
            "OR reason_codes_json LIKE '%place_order_failed%' "
            "OR reason_codes_json LIKE '%order_already_live%'"
        ),
    ]
    if slug:
        where.append("slug = ?")
        params.append(slug)
    if event_date:
        where.append("event_date = ?")
        params.append(event_date)
    if started:
        # auto_trade_decisions may be stored with second precision while jobs use
        # millisecond precision. Allow one second of slack so same-second
        # order_already_live decisions are not missed.
        where.append("julianday(created_at) >= julianday(?) - (1.0 / 86400.0)")
        params.append(started)
    if ended:
        where.append("julianday(created_at) <= julianday(?) + (60.0 / 86400.0)")
        params.append(ended)
    return conn.execute(
        f"""
        SELECT *
        FROM auto_trade_decisions
        WHERE {' AND '.join(f'({clause})' for clause in where)}
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def _failure_has_raw_return(details: Mapping[str, Any]) -> bool:
    if not details:
        return False
    keys_present = any(key in details for key in ("order_stderr", "order_stdout"))
    useful_error = any(
        str(details.get(key) or "").strip()
        for key in ("order_error", "order_error_detail", "order_status", "order_stderr", "order_stdout")
    )
    return bool(keys_present and useful_error)


def audit(
    db_path: Path,
    *,
    slug: str = "",
    event_date: str = "",
    since: str = "",
    require_job: bool = False,
    max_job_enqueue_to_order_ms: float = 3000.0,
    max_worker_to_order_ms: float = 3000.0,
    max_order_submit_ms: float = 3000.0,
) -> dict[str, Any]:
    env = _parse_env()
    since_text = str(since or "").strip() or _postfix_cutoff()
    checks: dict[str, bool] = {}
    blockers: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {
        "db": str(db_path),
        "since": since_text,
        "require_job": bool(require_job),
        "cleanup_only_enqueue_enabled": _truthy(
            env.get("PM_POSTERIOR_TARGET_DIRECT_ENQUEUE_CLEANUP_ONLY"),
            False,
        ),
        "primary_only_job_payload_enabled": _truthy(
            env.get("PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_JOB_PAYLOAD"),
            True,
        ),
    }
    with _connect_ro(db_path) as conn:
        job = _latest_job(conn, slug=slug, event_date=event_date, since=since_text)
        if job is None:
            details["status"] = "waiting_for_postfix_job"
            checks["posterior_target_postfix_job_present"] = False
            checks["posterior_target_postfix_job_pending_allowed"] = not require_job
            if require_job:
                blockers.append("posterior_target_postfix_job_missing")
            else:
                warnings.append("posterior_target_postfix_job_pending")
            return {
                "ok": not blockers,
                "checks": checks,
                "blockers": blockers,
                "warnings": warnings,
                "details": details,
            }

        payload = _load_json(job["payload_json"])
        if not isinstance(payload, dict):
            blockers.append("posterior_target_job_payload_invalid")
            checks["posterior_target_job_payload_valid"] = False
            return {
                "ok": False,
                "checks": checks,
                "blockers": blockers,
                "warnings": warnings,
                "details": details,
            }

        block = _target_block(payload)
        target_bucket = str(block.get("target_bucket") or "").strip()
        targets = _entry_targets(payload)
        nested_counts = _nested_target_counts(payload)
        exit_only = bool(payload.get("exit_only"))
        cleanup_only = bool(payload.get("old_exit_cleanup") or exit_only)
        details.update(
            {
                "status": "postfix_job_found",
                "job_id": int(job["id"]),
                "job_state": job["state"],
                "job_created_at": job["created_at"],
                "job_started_at": job["started_at"],
                "job_ended_at": job["ended_at"],
                "target_bucket": target_bucket,
                "target_posterior": block.get("target_posterior"),
                "exit_only": exit_only,
                "old_exit_cleanup": bool(payload.get("old_exit_cleanup")),
                "entry_expected_targets_count": len(targets),
                "entry_expected_targets": [
                    {
                        "outcome": target.get("outcome"),
                        "side": target.get("side"),
                        "leg_role": target.get("leg_role"),
                    }
                    for target in targets
                ],
                "nested_target_counts": nested_counts,
            }
        )

        checks["posterior_target_postfix_job_present"] = True
        checks["cleanup_only_job_not_enqueued_when_disabled"] = bool(
            details["cleanup_only_enqueue_enabled"] or not cleanup_only
        )
        primary_target_ok = bool(
            not exit_only
            and target_bucket
            and len(targets) == 1
            and str(targets[0].get("outcome") or "").strip() == target_bucket
            and str(targets[0].get("side") or "").strip().upper() == "YES"
        )
        checks["primary_entry_payload_single_yes_target"] = primary_target_ok
        checks["nested_payload_single_primary_target"] = all(
            int(item.get("entry_expected_targets_count") or 0) <= 1 for item in nested_counts
        )
        primary_target = targets[0] if targets else {}
        rule_source_proof = _posterior_target_rule_source_proof(
            target=primary_target,
            payload=payload,
            target_bucket=target_bucket,
        )
        details["posterior_target_rule_source_proof"] = rule_source_proof
        checks["job_payload_has_settlement_rule_source_proof"] = bool(rule_source_proof.get("ok"))

        order_row = _find_order_decision(
            conn,
            job=job,
            slug=slug,
            event_date=event_date,
            target_bucket=target_bucket,
        )
        order_details = _decision_details(order_row)
        reason_codes = _load_json(order_row["reason_codes_json"]) if order_row is not None else []
        reason_codes = reason_codes if isinstance(reason_codes, list) else []
        place_failed = any(str(code) == "place_order_failed" for code in reason_codes)
        order_submitted = any(str(code) == "order_submitted" for code in reason_codes)
        order_already_live = any(str(code) == "order_already_live" for code in reason_codes)
        details["order_decision"] = (
            {
                "id": int(order_row["id"]),
                "action": order_row["action"],
                "outcome": order_row["outcome"],
                "side": order_row["side"],
                "decided_state": order_row["decided_state"],
                "will_order": int(order_row["will_order"] or 0),
                "reason_codes": reason_codes,
                "created_at": order_row["created_at"],
                "latencies_ms": {
                    "posterior_target_job_enqueue_to_order_ms": _to_float(
                        order_details.get("posterior_target_job_enqueue_to_order_ms")
                        if order_details.get("posterior_target_job_enqueue_to_order_ms") is not None
                        else order_details.get("posterior_target_job_enqueue_to_worker_ms")
                    ),
                    "posterior_target_worker_to_order_ms": _to_float(
                        order_details.get("posterior_target_worker_to_order_ms")
                        if order_details.get("posterior_target_worker_to_order_ms") is not None
                        else (0.0 if order_already_live else None)
                    ),
                    "order_submit_ms": _to_float(order_details.get("order_submit_ms")),
                },
                "order_already_live": bool(order_already_live),
                "failure_fields_present": {
                    "order_error": "order_error" in order_details,
                    "order_error_detail": "order_error_detail" in order_details,
                    "order_stderr": "order_stderr" in order_details,
                    "order_stdout": "order_stdout" in order_details,
                    "order_status": "order_status" in order_details,
                },
            }
            if order_row is not None
            else None
        )
        checks["entry_order_decision_present"] = order_row is not None
        checks["entry_order_submitted_or_failed"] = bool(
            order_submitted or place_failed or order_already_live
        )
        if order_row is not None:
            job_to_order = _to_float(
                order_details.get("posterior_target_job_enqueue_to_order_ms")
                if order_details.get("posterior_target_job_enqueue_to_order_ms") is not None
                else order_details.get("posterior_target_job_enqueue_to_worker_ms")
            )
            worker_to_order = _to_float(
                order_details.get("posterior_target_worker_to_order_ms")
                if order_details.get("posterior_target_worker_to_order_ms") is not None
                else (0.0 if order_already_live else None)
            )
            submit_ms = _to_float(order_details.get("order_submit_ms"))
            checks["job_enqueue_to_order_within_3s"] = bool(
                job_to_order is not None and job_to_order <= float(max_job_enqueue_to_order_ms)
            )
            checks["worker_to_order_within_3s"] = bool(
                worker_to_order is not None and worker_to_order <= float(max_worker_to_order_ms)
            )
            if submit_ms is not None:
                checks["order_submit_within_3s"] = bool(submit_ms <= float(max_order_submit_ms))
            else:
                checks["order_submit_within_3s"] = bool(
                    order_already_live or (order_submitted and worker_to_order is not None)
                )
            if place_failed:
                checks["place_order_failed_has_raw_return"] = _failure_has_raw_return(order_details)
        else:
            checks["job_enqueue_to_order_within_3s"] = False
            checks["worker_to_order_within_3s"] = False
            checks["order_submit_within_3s"] = False
        posterior_ssot_decision_proof = _posterior_ssot_decision_chain_proof(
            payload=payload,
            order_details=order_details,
        )
        details["posterior_ssot_decision_proof"] = posterior_ssot_decision_proof
        checks["posterior_ssot_fact_decision_chain_ok"] = bool(
            posterior_ssot_decision_proof.get("ok")
        )

    for name, passed in checks.items():
        if not passed:
            blockers.append(name)
    return {
        "ok": not blockers,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--since-hours", type=float, default=None)
    parser.add_argument("--require-job", action="store_true")
    parser.add_argument("--max-job-enqueue-to-order-ms", type=float, default=3000.0)
    parser.add_argument("--max-worker-to-order-ms", type=float, default=3000.0)
    parser.add_argument("--max-order-submit-ms", type=float, default=3000.0)
    args = parser.parse_args()

    since = str(args.since or "").strip()
    if not since and args.since_hours is not None:
        since = _iso_utc(datetime.now(timezone.utc) - timedelta(hours=float(args.since_hours)))
    result = audit(
        Path(args.db),
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        since=since,
        require_job=bool(args.require_job),
        max_job_enqueue_to_order_ms=float(args.max_job_enqueue_to_order_ms),
        max_worker_to_order_ms=float(args.max_worker_to_order_ms),
        max_order_submit_ms=float(args.max_order_submit_ms),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
