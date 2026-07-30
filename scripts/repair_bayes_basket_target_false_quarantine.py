#!/usr/bin/env python3
"""Revalidate and repair the one explicitly allowlisted forward-store defect."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.forward_observation_store import (  # noqa: E402
    DEFAULT_STORE_PATH,
    DEFAULT_STORE_ROOT,
    FALSE_QUARANTINE_REPAIRABLE_ERRORS,
    MAX_FALSE_QUARANTINE_REPAIR_ROWS,
    read_forward_store_state,
    repair_false_quarantine_batch,
)
from bayes_basket_target.production_worker import TargetProductionWorker  # noqa: E402


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _repairable_identities(store_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(
        f"file:{store_path.resolve(strict=True)}?mode=ro",
        uri=True,
        timeout=30,
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        has_repair = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='forward_ingest_repair'"
        ).fetchone() is not None
        anti_join = (
            "AND NOT EXISTS(SELECT 1 FROM forward_ingest_repair r "
            "WHERE r.decision_id=s.decision_id)"
            if has_repair
            else ""
        )
        placeholders = ",".join(
            "?" for _ in FALSE_QUARANTINE_REPAIRABLE_ERRORS
        )
        rows = connection.execute(
            "SELECT s.decision_id,s.source_decision_sha256,s.source_rowid,"
            "s.error_code FROM forward_ingest_state s "
            "WHERE s.state='QUARANTINED' "
            f"AND s.error_code IN ({placeholders}) {anti_join} "
            "ORDER BY s.source_rowid LIMIT ?",
            (
                *sorted(FALSE_QUARANTINE_REPAIRABLE_ERRORS),
                MAX_FALSE_QUARANTINE_REPAIR_ROWS + 1,
            ),
        ).fetchall()
    if len(rows) > MAX_FALSE_QUARANTINE_REPAIR_ROWS:
        raise RuntimeError("false_quarantine_repair_scope_cap_exceeded")
    return [
        {
            "decision_id": str(row[0]),
            "decision_sha256": str(row[1]),
            "source_rowid": int(row[2]),
            "prior_error_code": str(row[3]),
        }
        for row in rows
    ]


def repair(
    *,
    db_path: Path,
    store_path: Path,
    apply: bool,
    confirmed_stopped: bool,
) -> dict[str, Any]:
    database = db_path.expanduser().resolve(strict=True)
    store = store_path.expanduser().resolve(strict=True)
    identities = _repairable_identities(store)
    before = read_forward_store_state(store, allowed_root=DEFAULT_STORE_ROOT)
    if not identities:
        return {
            "schema": "bayes_basket_target_false_quarantine_repair_run_v1",
            "state": "already_current",
            "candidate_count": 0,
            "generation_before": int(before["generation"]),
            "generation_after": int(before["generation"]),
            "last_source_rowid": int(before["last_source_rowid"]),
            "observation_count": int(before["observation_count"]),
            "quarantine_count": int(before["quarantine_count"]),
            "orders_submitted": 0,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
        }
    if apply and not confirmed_stopped:
        raise RuntimeError(
            "false_quarantine_repair_requires_reported_trajectory_no_stopped"
        )
    with closing(
        sqlite3.connect(
            f"file:{database}?mode=ro",
            uri=True,
            timeout=30,
            isolation_level=None,
        )
    ) as source:
        source.execute("PRAGMA busy_timeout=30000")
        source.execute("PRAGMA query_only=ON")
        worker = TargetProductionWorker(
            database,
            commit_conn=source,
            cash_phase_forward_store_path=store,
            station_allowlist=("RKPK",),
        )
        records: list[dict[str, Any]] = []
        try:
            for identity in identities:
                worker._forward_flush_probe = True
                worker._forward_flush_probe_rowid = identity["source_rowid"]
                worker._forward_flush_source_cursor_override = (
                    identity["source_rowid"] - 1
                )
                worker._forward_flush_scan_limit = 1
                probe = worker._flush_cash_phase_forward_observations_probe()
                probe_records = probe.get("records")
                if (
                    probe.get("state") != "validated"
                    or not isinstance(probe_records, list)
                    or len(probe_records) != 1
                ):
                    raise RuntimeError(
                        "false_quarantine_revalidation_result_invalid"
                    )
                record = dict(probe_records[0])
                if (
                    record.get("decision_id") != identity["decision_id"]
                    or record.get("decision_sha256")
                    != identity["decision_sha256"]
                    or int(record.get("source_rowid") or 0)
                    != identity["source_rowid"]
                ):
                    raise RuntimeError(
                        "false_quarantine_revalidation_identity_drift"
                    )
                record["prior_error_code"] = identity["prior_error_code"]
                records.append(record)
        finally:
            if worker.conn is not None:
                worker.conn.close()
            if worker.canonical_source_conn is not None:
                worker.canonical_source_conn.close()
    summary = {
        "schema": "bayes_basket_target_false_quarantine_repair_run_v1",
        "state": "validated_no_write",
        "candidate_count": len(records),
        "decision_ids": [record["decision_id"] for record in records],
        "source_rowids": [record["source_rowid"] for record in records],
        "generation_before": int(before["generation"]),
        "last_source_rowid": int(before["last_source_rowid"]),
        "orders_submitted": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }
    if not apply:
        return summary
    result = repair_false_quarantine_batch(
        store,
        records=records,
        expected_generation=int(before["generation"]),
        repaired_at_utc=_utc_now(),
        allowed_root=DEFAULT_STORE_ROOT,
    )
    return {
        **summary,
        **result,
        "state": str(result["state"]),
        "candidate_count": len(records),
        "generation_before": int(before["generation"]),
        "generation_after": int(result["generation"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-reported-trajectory-no-stopped",
        action="store_true",
    )
    args = parser.parse_args()
    result = repair(
        db_path=args.db,
        store_path=args.store,
        apply=bool(args.apply),
        confirmed_stopped=bool(
            args.confirm_reported_trajectory_no_stopped
        ),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
