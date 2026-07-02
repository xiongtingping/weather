#!/usr/bin/env python3
"""Materialize the Bayes data-role charter into archive.db metadata.

This script only writes the contract registry table. It does not backfill
event_runs, facts, observations, trades, or model snapshots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
sys.path.insert(0, str(ROOT))

from bayesian_data_role_contract import BAYESIAN_DATA_ROLE_CONTRACT, audit_bayesian_data_role_contract


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _charter_row(entry: Any) -> Dict[str, Any]:
    charter = {
        "source_id": entry.source_id,
        "label": entry.label,
        "temporal_class": entry.temporal_class,
        "contract_role": entry.contract_role,
        "implemented_roles": list(entry.implemented_roles),
        "usage": entry.usage,
        "modules": list(entry.modules),
        "fields": list(entry.fields),
        "safeguards": list(entry.safeguards),
        "evidence_class": entry.evidence_class,
        "priority_rank": int(entry.priority_rank),
    }
    raw = _json(charter)
    return {
        **charter,
        "implemented_roles_json": _json(charter["implemented_roles"]),
        "modules_json": _json(charter["modules"]),
        "fields_json": _json(charter["fields"]),
        "safeguards_json": _json(charter["safeguards"]),
        "charter_json": raw,
        "contract_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }


def ensure_contract_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bayesian_data_role_contract (
            source_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            temporal_class TEXT NOT NULL,
            contract_role TEXT NOT NULL,
            implemented_roles_json TEXT NOT NULL,
            evidence_class TEXT NOT NULL,
            priority_rank INTEGER NOT NULL,
            usage TEXT NOT NULL,
            modules_json TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            safeguards_json TEXT NOT NULL,
            charter_json TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bayesian_data_role_contract_active_priority
        ON bayesian_data_role_contract(active, priority_rank, source_id)
        """
    )


def materialize(db_path: Path) -> Dict[str, Any]:
    audit = audit_bayesian_data_role_contract()
    if int(audit.get("mismatch_count") or 0) != 0:
        return {"ok": False, "reason": "contract_audit_failed", "audit": audit}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [_charter_row(entry) for entry in BAYESIAN_DATA_ROLE_CONTRACT]
    active_ids = [row["source_id"] for row in rows]
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        ensure_contract_table(conn)
        changed = 0
        for row in rows:
            before = conn.execute(
                "SELECT contract_hash FROM bayesian_data_role_contract WHERE source_id = ?",
                (row["source_id"],),
            ).fetchone()
            if before is None or str(before[0] or "") != row["contract_hash"]:
                changed += 1
            conn.execute(
                """
                INSERT INTO bayesian_data_role_contract (
                    source_id, label, temporal_class, contract_role,
                    implemented_roles_json, evidence_class, priority_rank, usage,
                    modules_json, fields_json, safeguards_json, charter_json,
                    contract_hash, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    label=excluded.label,
                    temporal_class=excluded.temporal_class,
                    contract_role=excluded.contract_role,
                    implemented_roles_json=excluded.implemented_roles_json,
                    evidence_class=excluded.evidence_class,
                    priority_rank=excluded.priority_rank,
                    usage=excluded.usage,
                    modules_json=excluded.modules_json,
                    fields_json=excluded.fields_json,
                    safeguards_json=excluded.safeguards_json,
                    charter_json=excluded.charter_json,
                    contract_hash=excluded.contract_hash,
                    active=1,
                    updated_at=CASE
                        WHEN bayesian_data_role_contract.contract_hash != excluded.contract_hash
                        THEN excluded.updated_at
                        ELSE bayesian_data_role_contract.updated_at
                    END
                """,
                (
                    row["source_id"],
                    row["label"],
                    row["temporal_class"],
                    row["contract_role"],
                    row["implemented_roles_json"],
                    row["evidence_class"],
                    row["priority_rank"],
                    row["usage"],
                    row["modules_json"],
                    row["fields_json"],
                    row["safeguards_json"],
                    row["charter_json"],
                    row["contract_hash"],
                    now,
                    now,
                ),
            )
        stale = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM bayesian_data_role_contract
            WHERE active = 1
              AND source_id NOT IN ({",".join("?" for _ in active_ids)})
            """,
            active_ids,
        ).fetchone()[0]
        conn.execute(
            f"""
            UPDATE bayesian_data_role_contract
               SET active = 0, updated_at = ?
             WHERE active = 1
               AND source_id NOT IN ({",".join("?" for _ in active_ids)})
            """,
            [now, *active_ids],
        )
        conn.commit()
        return {
            "ok": True,
            "db": str(db_path),
            "active_count": len(rows),
            "changed_count": changed,
            "stale_deactivated_count": int(stale or 0),
            "audit": audit,
        }
    finally:
        conn.close()


def check_materialized(db_path: Path) -> Dict[str, Any]:
    audit = audit_bayesian_data_role_contract()
    expected = {_charter_row(entry)["source_id"]: _charter_row(entry) for entry in BAYESIAN_DATA_ROLE_CONTRACT}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        rows = conn.execute(
            """
            SELECT source_id, contract_hash, active, evidence_class, priority_rank
            FROM bayesian_data_role_contract
            WHERE active = 1
            """
        ).fetchall()
    except sqlite3.Error as exc:
        return {"ok": False, "reason": f"read_failed:{type(exc).__name__}:{exc}", "audit": audit}
    finally:
        conn.close()
    got = {str(row[0]): row for row in rows}
    mismatches = []
    for source_id, row in expected.items():
        current = got.get(source_id)
        if current is None:
            mismatches.append(f"{source_id}:missing")
            continue
        if str(current[1]) != row["contract_hash"]:
            mismatches.append(f"{source_id}:hash_mismatch")
    extra = sorted(set(got).difference(expected))
    for source_id in extra:
        mismatches.append(f"{source_id}:extra_active")
    return {
        "ok": not mismatches and int(audit.get("mismatch_count") or 0) == 0,
        "active_count": len(got),
        "expected_count": len(expected),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "audit": audit,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize Bayes data-role charter metadata")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--check", action="store_true", help="Read-only check instead of writing")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db_path = Path(args.db)
    payload = check_materialized(db_path) if args.check else materialize(db_path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "ok={ok} active={active} mismatches={mismatch}".format(
                ok=int(bool(payload.get("ok"))),
                active=payload.get("active_count", payload.get("expected_count", 0)),
                mismatch=payload.get("mismatch_count", 0),
            )
        )
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
