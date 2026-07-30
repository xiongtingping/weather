#!/usr/bin/env python3
"""Read-only performance receipt for the isolated Bayes basket target lane.

This audit measures bounded local work only.  It never starts a service, opens
the order gateway, writes a database, or treats the evidence-only WS cache as
an executable snapshot.  A performance receipt cannot pass until an explicit
approved budget and real target-owned WS snapshot/lock-wait evidence exist.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.performance_budget import (  # noqa: E402
    DEFAULT_PERFORMANCE_BUDGET,
    PerformanceBudgetError,
    load_performance_budget,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_INPUT_FILES_PER_BATCH,
    MAX_TARGET_PROCESS_RSS_BYTES,
    assert_process_memory_budget,
    inspect_target_resource_budget,
    write_bounded_json,
)
from scripts.bayes_basket_target_runtime_audit import audit as runtime_audit  # noqa: E402


SCHEMA = "bayes_basket_target_performance_audit_v1"
DEFAULT_RAW_ROOT = ROOT / "data/research/bayes_basket_target/model_arrivals"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_performance.json"
DEFAULT_SAMPLES = 31
MAX_SAMPLES = 64


class PerformanceAuditError(ValueError):
    """Raised when the bounded performance audit cannot produce a receipt."""


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, int((percentile / 100.0) * len(ordered) + 0.999999) - 1))
    return round(ordered[index], 3)


def _metric(values: list[float]) -> dict[str, Any]:
    return {
        "sample_count": len(values),
        "min_ms": round(min(values), 3) if values else None,
        "mean_ms": round(mean(values), 3) if values else None,
        "p95_ms": _percentile(values, 95.0),
        "p99_ms": _percentile(values, 99.0),
        "max_ms": round(max(values), 3) if values else None,
    }


def _run_timed(function: Callable[[], Any], *, samples: int) -> dict[str, Any]:
    values: list[float] = []
    errors: list[str] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        try:
            function()
        except Exception as exc:  # receipt records evidence failure; caller decides the gate
            errors.append(f"{type(exc).__name__}:{exc}")
        finally:
            values.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return {"latency": _metric(values), "errors": errors[:8], "error_count": len(errors)}


def _parse_utc(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp_missing")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _ws_snapshot_evidence(
    db_path: Path,
    *,
    now_utc: datetime,
) -> dict[str, Any]:
    """Inspect current target WS rows without decoding or mutating payloads."""

    resolved = db_path.expanduser().resolve()
    uri = f"file:{resolved}?mode=ro"
    cache_rows: list[tuple[Any, ...]] = []
    snapshot_rows: list[tuple[Any, ...]] = []
    errors: list[str] = []
    try:
        with sqlite3.connect(uri, uri=True, timeout=0.0) as conn:
            cache_rows = conn.execute(
                "SELECT observed_at, updated_at, evidence_only, authorizes_order "
                "FROM basket_official_ws_evidence_cache"
            ).fetchall()
            snapshot_rows = conn.execute(
                "SELECT quote_observed_at, created_at, state "
                "FROM basket_official_ws_snapshot"
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        errors.append(f"{type(exc).__name__}:{exc}")

    def ages(rows: list[tuple[Any, ...]], index: int) -> tuple[list[float], list[str]]:
        result: list[float] = []
        parse_errors: list[str] = []
        for row in rows:
            try:
                age = (now_utc - _parse_utc(row[index])).total_seconds()
                result.append(round(age, 3))
                if age < 0:
                    parse_errors.append("future_timestamp")
            except ValueError as exc:
                parse_errors.append(str(exc))
        return result, parse_errors

    cache_ages, cache_age_errors = ages(cache_rows, 0)
    snapshot_ages, snapshot_age_errors = ages(snapshot_rows, 0)
    errors.extend(cache_age_errors + snapshot_age_errors)
    cache_flags_ok = all(row[2] == 1 and row[3] == 0 for row in cache_rows)
    materialized_count = len(snapshot_rows)
    return {
        "state": "READY" if materialized_count > 0 and not errors else "NOT_OBSERVED",
        "materialized_snapshot_count": materialized_count,
        "oldest_materialized_snapshot_age_sec": max(snapshot_ages) if snapshot_ages else None,
        "latest_materialized_snapshot_age_sec": min(snapshot_ages) if snapshot_ages else None,
        "evidence_cache_count": len(cache_rows),
        "oldest_evidence_cache_age_sec": max(cache_ages) if cache_ages else None,
        "latest_evidence_cache_age_sec": min(cache_ages) if cache_ages else None,
        "evidence_cache_is_non_authorizing": cache_flags_ok,
        "errors": errors[:8],
        "error_count": len(errors),
        "evidence_cache_is_not_materialized_snapshot": True,
    }


def _read_transaction_acquire(db_path: Path) -> dict[str, Any]:
    """Read bounded target-owned writer telemetry and measure read acquisition."""

    resolved = db_path.expanduser().resolve()
    uri = f"file:{resolved}?mode=ro"
    values: list[float] = []
    errors: list[str] = []
    writer_telemetry: tuple[Any, ...] | None = None
    try:
        with sqlite3.connect(uri, uri=True, timeout=0.0) as conn:
            writer_telemetry = conn.execute(
                "SELECT sample_count,total_wait_ms,max_wait_ms,last_wait_ms,last_operation,"
                "last_acquired_at,updated_at FROM basket_target_writer_lock_wait_telemetry "
                "WHERE singleton=1"
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        errors.append(f"writer_telemetry:{type(exc).__name__}:{exc}")
    for _ in range(3):
        started = time.perf_counter_ns()
        try:
            with sqlite3.connect(uri, uri=True, timeout=0.0) as conn:
                conn.execute("BEGIN")
                conn.execute("SELECT 1").fetchone()
                conn.rollback()
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"{type(exc).__name__}:{exc}")
        finally:
            values.append((time.perf_counter_ns() - started) / 1_000_000.0)
    sample_count = int(writer_telemetry[0]) if writer_telemetry is not None else 0
    return {
        "state": "OBSERVED" if not errors else "ERROR",
        "read_transaction_acquire_latency": _metric(values),
        "errors": errors[:8],
        "error_count": len(errors),
        "writer_lock_wait_telemetry": "OBSERVED" if sample_count > 0 else "NOT_OBSERVED",
        "writer_lock_wait_sample_count": sample_count,
        "writer_lock_wait_total_ms": float(writer_telemetry[1]) if writer_telemetry is not None else 0.0,
        "writer_lock_wait_max_ms": float(writer_telemetry[2]) if writer_telemetry is not None else None,
        "writer_lock_wait_last_ms": float(writer_telemetry[3]) if writer_telemetry is not None else None,
        "writer_lock_wait_last_operation": str(writer_telemetry[4]) if writer_telemetry is not None else None,
        "writer_lock_wait_last_acquired_at": str(writer_telemetry[5]) if writer_telemetry is not None else None,
        "writer_lock_wait_updated_at": str(writer_telemetry[6]) if writer_telemetry is not None else None,
        "writer_lock_wait_reason": (
            "target_owned_bounded_writer_telemetry"
            if sample_count > 0
            else "target_owned_writer_has_not_acquired_a_write_lock"
        ),
    }


def _metric_within_budget(result: Mapping[str, Any], *, p95_ms: float, p99_ms: float) -> bool:
    metric = result.get("latency")
    if not isinstance(metric, Mapping):
        return False
    observed_p95 = metric.get("p95_ms")
    observed_p99 = metric.get("p99_ms")
    if observed_p95 is None or observed_p99 is None:
        return False
    try:
        return float(observed_p95) <= float(p95_ms) and float(observed_p99) <= float(p99_ms)
    except (TypeError, ValueError):
        return False


def audit(
    *,
    db_path: Path = DEFAULT_TARGET_DB,
    raw_root: Path = DEFAULT_RAW_ROOT,
    project_root: Path = ROOT,
    samples: int = DEFAULT_SAMPLES,
    now_utc: datetime | None = None,
    performance_budget_path: Path = DEFAULT_PERFORMANCE_BUDGET,
    runtime_runner: Callable[[], Mapping[str, Any]] | None = None,
    resource_runner: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if not 1 <= int(samples) <= MAX_SAMPLES:
        raise PerformanceAuditError(f"samples_out_of_bounds:{samples}:1..{MAX_SAMPLES}")
    assert_process_memory_budget(projected_bytes=MAX_SAMPLES * 4096, limit=MAX_TARGET_PROCESS_RSS_BYTES)
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise PerformanceAuditError("now_utc_timezone_required")
    runtime_fn = runtime_runner or (lambda: runtime_audit(project_root=project_root))
    # Measure the bounded resource probe used by the production hot path.
    # The retention/lineage auditor recursively verifies many durable JSON
    # receipts and is intentionally an offline governance job; repeating that
    # full scan for every latency sample made this supposedly bounded audit
    # consume minutes and tens of thousands of file reads.
    resource_fn = resource_runner or (
        lambda: inspect_target_resource_budget(
            db_path,
            raw_root=raw_root,
        )
    )
    runtime_timing = _run_timed(runtime_fn, samples=int(samples))
    resource_timing = _run_timed(resource_fn, samples=int(samples))
    resource_snapshot = inspect_target_resource_budget(db_path, raw_root=raw_root)
    ws = _ws_snapshot_evidence(db_path, now_utc=now.astimezone(timezone.utc))
    lock = _read_transaction_acquire(db_path)
    budget: dict[str, Any] | None = None
    budget_error: str | None = None
    try:
        budget = load_performance_budget(performance_budget_path)
    except (OSError, PerformanceBudgetError, ValueError) as exc:
        budget_error = f"{type(exc).__name__}:{exc}"

    limits = budget.get("limits", {}) if budget else {}
    runtime_budget_ok = bool(budget) and _metric_within_budget(
        runtime_timing,
        p95_ms=float(limits["runtime_audit_p95_ms"]),
        p99_ms=float(limits["runtime_audit_p99_ms"]),
    )
    resource_budget_ok = bool(budget) and _metric_within_budget(
        resource_timing,
        p95_ms=float(limits["resource_audit_p95_ms"]),
        p99_ms=float(limits["resource_audit_p99_ms"]),
    )
    ws_age = ws.get("oldest_materialized_snapshot_age_sec")
    ws_age_ok = bool(budget) and ws_age is not None and float(ws_age) <= float(limits["max_ws_snapshot_age_sec"])

    checks = {
        "sample_count_bounded": int(samples) <= MAX_SAMPLES,
        "input_batch_limit_declared": MAX_INPUT_FILES_PER_BATCH > 0,
        "runtime_audit_samples_completed": runtime_timing["error_count"] == 0,
        "resource_audit_samples_completed": resource_timing["error_count"] == 0,
        "target_resource_budget_within_limit": resource_snapshot.get("ok") is True,
        "materialized_ws_snapshot_present": ws["materialized_snapshot_count"] > 0,
        "evidence_cache_not_used_as_snapshot": ws["evidence_cache_is_not_materialized_snapshot"] is True,
        "evidence_cache_non_authorizing": ws["evidence_cache_is_non_authorizing"],
        "writer_lock_wait_telemetry_present": lock["writer_lock_wait_sample_count"] > 0,
        "approved_performance_budget_present": budget is not None,
        "runtime_audit_within_declared_budget": runtime_budget_ok,
        "resource_audit_within_declared_budget": resource_budget_ok,
        "materialized_ws_snapshot_within_declared_age": ws_age_ok,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": False,
    }
    blockers: list[str] = []
    if lock["writer_lock_wait_sample_count"] <= 0:
        blockers.append("target_owned_writer_lock_wait_telemetry_missing")
    if budget is None:
        blockers.append("approved_performance_budget_not_declared")
    else:
        if not runtime_budget_ok:
            blockers.append("runtime_audit_exceeds_declared_budget")
        if not resource_budget_ok:
            blockers.append("resource_audit_exceeds_declared_budget")
    if ws["materialized_snapshot_count"] == 0:
        blockers.append("official_ws_materialized_snapshot_missing")
    elif not ws_age_ok:
        blockers.append("official_ws_materialized_snapshot_stale")
    if resource_snapshot.get("ok") is not True:
        blockers.extend(str(item) for item in resource_snapshot.get("violations") or [])
    if runtime_timing["error_count"]:
        blockers.append("runtime_audit_sample_failed")
    if resource_timing["error_count"]:
        blockers.append("resource_audit_sample_failed")
    blockers = sorted(set(blockers))
    identity = {
        "schema": SCHEMA,
        "state": "BLOCKED_EVIDENCE" if blockers else "READY_FOR_BUDGET_REVIEW",
        "lane": "bayes_basket_target",
        "execution_mode": "production_no_live",
        "sample_limit": MAX_SAMPLES,
        "sample_count_requested": int(samples),
        "measured_at_utc": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "performance": {
            "runtime_audit": runtime_timing,
            "resource_audit": resource_timing,
            "read_transaction_acquire": lock,
        },
        "ws_snapshot_age": ws,
        "resource_budget": resource_snapshot,
        "approved_budget": budget or {
            "state": "MISSING",
            "source": str(performance_budget_path.expanduser().resolve()),
            "error": budget_error,
        },
        "checks": checks,
        "blockers": blockers,
        "boundary": {
            "authorizes_order": False,
            "live_submit_authorized": False,
            "evidence_only_ws": True,
            "settlement_floor_calculation_only": True,
            "writes_shared_live_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "cleanup_performed": False,
            "external_archive_dependency": False,
        },
    }
    return identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--performance-budget", type=Path, default=DEFAULT_PERFORMANCE_BUDGET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = audit(
        db_path=args.db.expanduser().resolve(),
        raw_root=args.raw_root.expanduser().resolve(),
        project_root=args.root.expanduser().resolve(),
        samples=args.samples,
        performance_budget_path=args.performance_budget.expanduser().resolve(),
    )
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "schema": result["schema"],
        "state": result["state"],
        "blockers": result["blockers"],
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["state"] == "READY_FOR_BUDGET_REVIEW" else 2


if __name__ == "__main__":
    raise SystemExit(main())
