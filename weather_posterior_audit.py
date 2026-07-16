#!/usr/bin/env python3
"""Current read-only audit for the reported-X v5 posterior SSOT.

This operator surface intentionally does not call the retired hierarchical/AWC
release gate.  It composes the active runtime readiness, unique production
mainline, event-level sequential lineage/SLO, and probability document authority.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "weather_posterior_audit_v2_reported_x"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "executes_sql": False,
    }


def _extract_json(stdout: str) -> dict[str, Any]:
    text = str(stdout or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {"ok": False, "blockers": ["subprocess_json_missing"]}
    try:
        value = json.loads(text[start : end + 1])
    except Exception as exc:
        return {"ok": False, "blockers": [f"subprocess_json_invalid:{type(exc).__name__}"]}
    return value if isinstance(value, dict) else {"ok": False, "blockers": ["subprocess_json_not_object"]}


def _run_json(command: Sequence[str], *, timeout_sec: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            list(command),
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1, int(timeout_sec)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "blockers": ["subprocess_timeout"]}
    report = _extract_json(proc.stdout)
    report.setdefault("returncode", proc.returncode)
    if proc.stderr:
        report.setdefault("stderr_preview", proc.stderr[:500])
    return report


def _codes(items: Any) -> list[str]:
    result: list[str] = []
    for item in items or []:
        if isinstance(item, Mapping):
            code = str(item.get("blocker") or item.get("reason") or "").strip()
        else:
            code = str(item or "").strip()
        if code:
            result.append(code)
    return result


def build_report(
    *,
    readiness: Mapping[str, Any],
    uniqueness: Mapping[str, Any],
    sequential: Mapping[str, Any],
    document_authority: Mapping[str, Any],
) -> dict[str, Any]:
    readiness_evidence = readiness.get("evidence") if isinstance(readiness.get("evidence"), Mapping) else {}
    closure = readiness_evidence.get("runtime_manifest_dependency_closure") if isinstance(
        readiness_evidence.get("runtime_manifest_dependency_closure"), Mapping
    ) else {}
    uniqueness_checks = uniqueness.get("checks") if isinstance(uniqueness.get("checks"), Mapping) else {}
    sequential_evidence = sequential.get("evidence") if isinstance(sequential.get("evidence"), Mapping) else {}
    strong_identity = readiness_evidence.get("strong_likelihood_update_identity") if isinstance(
        readiness_evidence.get("strong_likelihood_update_identity"), Mapping
    ) else {}
    pending = list(readiness_evidence.get("pending_canonical_event_ids") or sequential_evidence.get("pending_canonical_event_ids") or [])
    blockers = sorted(
        set(
            _codes(readiness.get("blockers"))
            + _codes(uniqueness.get("blockers"))
            + _codes(sequential.get("blockers"))
            + _codes(document_authority.get("blockers"))
        )
    )
    checks = {
        "document_authority_aligned": document_authority.get("ok") is True,
        "runtime_dependency_closure_complete": closure.get("ok") is True,
        "reported_x_single_production_mainline": uniqueness_checks.get("reported_x_single_production_mainline") is True,
        "activation_runtime_identity_aligned": uniqueness_checks.get("reported_x_runtime_identity_aligned") is True,
        "reported_x_public_ssot_ready": readiness.get("ok") is True,
        "canonical_queue_drained": not pending,
        "cumulative_replacement_identity": (
            strong_identity.get("likelihood_update_kind")
            == "cumulative_evidence_replacement_delta_log_lr_v1"
        ),
        "sequential_event_lineage_and_slo_ready": sequential.get("ok") is True,
    }
    ok = all(checks.values()) and not blockers
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "state": "ready" if ok else "blocked",
        "read_only": True,
        "checks": checks,
        "blockers": blockers,
        "pending_canonical_event_ids": pending,
        "posterior_checkpoint_id": readiness_evidence.get("posterior_checkpoint_id"),
        "reported_x_lower_bound_c": readiness_evidence.get("reported_x_lower_bound_c"),
        "forward_three_second_slo": dict(sequential.get("forward_three_second_slo") or {}),
        "guards": _guards(),
        "next_boundary": (
            "reported_x_posterior_ready"
            if ok
            else "repair_current_reported_x_identity_and_replay_without_fallback"
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--event-date",
        default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    )
    parser.add_argument("--timeout-sec", type=int, default=60)
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    timeout = max(1, int(args.timeout_sec))
    readiness = _run_json(
        [sys.executable, "scripts/production_reported_x_runtime_readiness.py", "--db", str(args.db), "--event-date", str(args.event_date)],
        timeout_sec=timeout,
    )
    uniqueness = _run_json(
        [sys.executable, "scripts/production_chain_uniqueness_audit.py", "--db", str(args.db), "--summary"],
        timeout_sec=timeout,
    )
    sequential = _run_json(
        [sys.executable, "scripts/production_sequential_bayes_chain_audit.py", "--db", str(args.db), "--event-date", str(args.event_date), "--summary"],
        timeout_sec=timeout,
    )
    documents = _run_json(
        [sys.executable, "scripts/probability_document_authority_audit.py", "--summary"],
        timeout_sec=timeout,
    )
    report = build_report(
        readiness=readiness,
        uniqueness=uniqueness,
        sequential=sequential,
        document_authority=documents,
    )
    if args.summary:
        report = {key: report[key] for key in (
            "schema", "ok", "state", "read_only", "checks", "blockers",
            "pending_canonical_event_ids", "posterior_checkpoint_id",
            "reported_x_lower_bound_c", "forward_three_second_slo", "guards", "next_boundary",
        )}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
