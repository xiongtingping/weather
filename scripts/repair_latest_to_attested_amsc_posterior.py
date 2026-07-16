#!/usr/bin/env python3
"""Repair one copied latest posterior pointer to its attested AMSC fact.

This is deliberately narrower than a historical repair: it may only repoint
an existing latest row when that row is a v3.1 posterior without a matching
materialization attestation and the same event has a newer-or-equal attested
AMSC posterior.  A newer raw AMSC source fact blocks the repair instead of
allowing an old posterior to win.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_db_write_queue import connect_archive_db_rw  # noqa: E402
from posterior_materialization_attestation_runtime import TABLE_NAME  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
CONFIRMATION = "REPOINT_LATEST_TO_ATTESTED_AMSC_POSTERIOR"
V31_SCHEMA = "sequential_remaining_warming_bayes_runtime_v31"
AMSC_SOURCE_COMPOSER = "amsc_zuuu_metar_taf_fetcher_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> Mapping[str, Any]:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _posterior_schema(payload: Mapping[str, Any]) -> str:
    nested = payload.get("posterior_ssot_fact")
    return _text(
        payload.get("posterior_composer_schema")
        or (nested.get("posterior_composer_schema") if isinstance(nested, Mapping) else "")
    )


def _latest_row(conn: sqlite3.Connection, *, slug: str, event_date: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT l.fact_id,l.fact_snapshot_id,l.correlation_id,h.slug,h.event_date,h.fact_as_of,h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id=l.fact_id
        WHERE l.slug=? AND l.event_date=?
        """,
        (slug, event_date),
    ).fetchone()


def build_repair_plan(conn: sqlite3.Connection, *, slug: str, event_date: str) -> dict[str, Any]:
    if not _table_exists(conn, TABLE_NAME):
        return {"ok": False, "state": "blocked", "blockers": ["posterior_materialization_attestation_table_missing"]}
    latest = _latest_row(conn, slug=slug, event_date=event_date)
    if latest is None:
        return {"ok": False, "state": "blocked", "blockers": ["event_fact_latest_missing"]}
    current_payload = _json(latest["fact_json"])
    timing = current_payload.get("amsc_posterior_materialization_timing") or current_payload.get("source_to_posterior_timing")
    if (
        _posterior_schema(current_payload) != V31_SCHEMA
        or not isinstance(timing, Mapping)
        or _text(timing.get("schema")) != "source_to_posterior_timing_v1"
    ):
        return {"ok": True, "state": "not_applicable_latest_not_v31_amsc_posterior", "apply_required": False}
    current_fact_id = int(latest["fact_id"] or 0)
    current_attestation = conn.execute(
        f"SELECT materialized_fact_id FROM {TABLE_NAME} WHERE materialized_fact_id=?",
        (current_fact_id,),
    ).fetchone()
    if current_attestation is not None:
        return {"ok": True, "state": "already_attested_latest", "apply_required": False, "current_fact_id": current_fact_id}
    candidate = conn.execute(
        f"""
        SELECT h.id,h.fact_as_of,h.fact_json,a.posterior_committed_at,a.fact_snapshot_id,a.posterior_run_id
        FROM {TABLE_NAME} a
        JOIN event_fact_history h ON h.id=a.materialized_fact_id
        WHERE h.slug=? AND h.event_date=?
        ORDER BY a.posterior_committed_at DESC, h.id DESC
        LIMIT 1
        """,
        (slug, event_date),
    ).fetchone()
    if candidate is None:
        return {"ok": False, "state": "blocked", "blockers": ["attested_amsc_posterior_candidate_missing"]}
    candidate_payload = _json(candidate["fact_json"])
    if (
        _posterior_schema(candidate_payload) != V31_SCHEMA
        or _text(candidate_payload.get("fact_snapshot_id")) != _text(candidate["fact_snapshot_id"])
        or _text(candidate_payload.get("posterior_run_id")) != _text(candidate["posterior_run_id"])
    ):
        return {"ok": False, "state": "blocked", "blockers": ["attested_candidate_payload_binding_invalid"]}
    candidate_fact_id = int(candidate["id"] or 0)
    if candidate_fact_id <= 0 or candidate_fact_id == current_fact_id:
        return {"ok": False, "state": "blocked", "blockers": ["attested_candidate_identity_invalid"]}
    newer_source = conn.execute(
        """
        SELECT id,fact_as_of FROM event_fact_history
        WHERE slug=? AND event_date=? AND composer_version=? AND id>?
        ORDER BY id DESC LIMIT 1
        """,
        (slug, event_date, AMSC_SOURCE_COMPOSER, candidate_fact_id),
    ).fetchone()
    if newer_source is not None:
        return {
            "ok": False,
            "state": "blocked",
            "blockers": ["newer_amsc_source_fact_requires_fresh_materialization"],
            "newer_amsc_source_fact_id": int(newer_source["id"] or 0),
        }
    return {
        "ok": True,
        "state": "ready_to_repoint",
        "apply_required": True,
        "slug": slug,
        "event_date": event_date,
        "current_fact_id": current_fact_id,
        "candidate_fact_id": candidate_fact_id,
        "candidate_fact_snapshot_id": _text(candidate["fact_snapshot_id"]),
        "candidate_posterior_run_id": _text(candidate["posterior_run_id"]),
        "candidate_posterior_committed_at": _text(candidate["posterior_committed_at"]),
        "transaction_boundary": "cas_repoint_latest_to_attested_amsc_posterior",
    }


def _verify_fresh_backup(source: Path, backup_path: Path | str | None) -> str:
    """Require a distinct, readable snapshot before a latest-pointer repair."""

    if backup_path is None:
        return "backup_path_required_before_attested_latest_repair"
    backup = Path(backup_path).expanduser().resolve()
    if backup == source or not backup.exists():
        return "backup_missing_or_same_as_source"
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_conn, sqlite3.connect(
            f"file:{backup}?mode=ro", uri=True
        ) as backup_conn:
            if str(source_conn.execute("PRAGMA quick_check").fetchone()[0]) != "ok" or str(backup_conn.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
                return "backup_or_source_integrity_check_failed"
            if int(source_conn.execute("PRAGMA page_count").fetchone()[0]) != int(backup_conn.execute("PRAGMA page_count").fetchone()[0]):
                return "backup_page_count_mismatch"
    except sqlite3.Error:
        return "backup_verification_failed"
    return ""


def apply_repair(
    db_path: Path | str,
    *,
    slug: str,
    event_date: str,
    confirm: str = "",
    writer_quiescent: bool = False,
    backup_path: Path | str | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    if _text(confirm) != CONFIRMATION:
        return {"ok": False, "state": "blocked", "blockers": ["confirmation_phrase_required"]}
    if not writer_quiescent:
        return {"ok": False, "state": "blocked", "blockers": ["writer_quiescent_assertion_required"]}
    backup_blocker = _verify_fresh_backup(db, backup_path)
    if backup_blocker:
        return {"ok": False, "state": "blocked", "blockers": [backup_blocker]}
    with connect_archive_db_rw(
        db,
        owner="repair_latest_to_attested_amsc_posterior",
        timeout_sec=30.0,
        busy_timeout_ms=5000,
        row_factory=True,
    ) as conn:
        plan = build_repair_plan(conn, slug=slug, event_date=event_date)
        if plan.get("state") != "ready_to_repoint":
            return plan
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(
                """
                UPDATE event_fact_latest
                SET fact_id=?,fact_snapshot_id=?,updated_at=?
                WHERE slug=? AND event_date=? AND fact_id=?
                """,
                (
                    int(plan["candidate_fact_id"]),
                    _text(plan["candidate_fact_snapshot_id"]),
                    _now(),
                    slug,
                    event_date,
                    int(plan["current_fact_id"]),
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("latest_pointer_compare_and_swap_conflict")
            conn.commit()
        except Exception as exc:
            conn.rollback()
            return {**plan, "ok": False, "state": "rolled_back", "blockers": [f"repair_apply_failed:{type(exc).__name__}"]}
    return {**plan, "ok": True, "state": "repointed", "repointed": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--slug", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--backup")
    args = parser.parse_args()
    db = Path(args.db).expanduser().resolve()
    if args.apply:
        result = apply_repair(
            db, slug=args.slug, event_date=args.event_date, confirm=args.confirm,
            writer_quiescent=args.writer_quiescent, backup_path=args.backup,
        )
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            result = build_repair_plan(conn, slug=args.slug, event_date=args.event_date)
        finally:
            conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
