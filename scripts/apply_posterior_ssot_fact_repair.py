#!/usr/bin/env python3
"""Apply one posterior SSOT fact repair through the canonical fact writer.

This entrypoint is intentionally narrow: it only repairs the current latest
fact for one slug/event after the no-apply confirmation and preflight packets
have shown the canonical posterior projection. It never fetches weather data,
places orders, changes parameters, or performs historical backfill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from posterior_ssot_projection_runtime import build_fact_projection_no_apply  # noqa: E402
from scripts import controlled_apply_maintenance_window_packet as maintenance_window  # noqa: E402
from weather_fact_snapshot_writer import (  # noqa: E402
    apply_fact_snapshot_write_plan,
    compact_fact_snapshot_writer_apply_result_for_lineage,
)


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
CONFIRMATION_PHRASE = "APPLY_POSTERIOR_SSOT_FACT_REPAIR"
RESULT_SCHEMA = "posterior_ssot_fact_repair_apply_result_v1"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_stamp(value: str) -> str:
    return str(value or "").replace("-", "").replace(":", "").replace(".", "").replace("+00:00", "Z")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or ""))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _guards(*, writes: bool) -> dict[str, bool]:
    return {
        "writes_archive_db": bool(writes),
        "writes_facts_sqlite": False,
        "writes_event_fact_history": bool(writes),
        "writes_event_fact_latest": bool(writes),
        "writes_event_fact_summary_cache": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "restarts_launchd": False,
        "executes_sql_writes": bool(writes),
    }


def _blocked(reason: str, *, allow_apply: bool, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "applied": False,
        "blocked": True,
        "block_reason": reason,
        "allow_apply": bool(allow_apply),
        "writer_quiescent": bool(_as_mapping(extra).get("writer_quiescent")),
        "guards": _guards(writes=False),
    }
    out.update(dict(extra or {}))
    return out


def _compact_maintenance_window(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": str(packet.get("schema") or ""),
        "ok": bool(packet.get("ok")),
        "writer_quiescent": bool(packet.get("writer_quiescent")),
        "counts": dict(_as_mapping(packet.get("counts"))),
        "required_stop_labels": list(packet.get("required_stop_labels") or []),
        "blockers": list(packet.get("blockers") or [])[:20],
        "stop_authorization": dict(_as_mapping(packet.get("stop_authorization"))),
        "guards": dict(_as_mapping(packet.get("guards"))),
        "next_boundary": packet.get("next_boundary"),
    }


def _maintenance_window_packet(
    db: Path,
    *,
    packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if packet is not None:
        return dict(packet)
    return maintenance_window.build_maintenance_window_packet(db, max_items_output=20)


def _connect_rw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _latest_fact_id(conn: sqlite3.Connection, *, slug: str, event_date: str) -> int:
    row = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?",
        (slug, event_date),
    ).fetchone()
    return int(row["fact_id"] or 0) if row else 0


def _load_candidate(
    conn: sqlite3.Connection,
    *,
    fact_id: int,
    slug: str,
    event_date: str,
) -> tuple[sqlite3.Row | None, str]:
    if fact_id > 0:
        row = conn.execute(
            """
            SELECT id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
            FROM event_fact_history
            WHERE id = ?
            """,
            (int(fact_id),),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT h.id, h.run_date, h.slug, h.event_date, h.fact_as_of, h.composer_version, h.fact_json, h.created_at
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()
    if row is None:
        return None, "candidate_fact_row_missing"
    row_slug = str(row["slug"] or "")
    row_event_date = str(row["event_date"] or "")
    if slug and row_slug != slug:
        return row, "candidate_slug_mismatch"
    if event_date and row_event_date != event_date:
        return row, "candidate_event_date_mismatch"
    latest_id = _latest_fact_id(conn, slug=row_slug, event_date=row_event_date)
    if latest_id != int(row["id"] or 0):
        return row, "candidate_not_current_latest"
    return row, ""


def _backup_sqlite(db_path: Path, backup_dir: Path, generated_at: str) -> dict[str, Any]:
    if not db_path.exists():
        return {"created": False, "reason": "archive_db_missing", "path": ""}
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"{db_path.name}.posterior-ssot-repair-apply-{_safe_stamp(generated_at)}.bak"
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        dest = sqlite3.connect(str(path), timeout=30)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return {"created": True, "reason": "", "path": str(path)}


def _reuse_existing_backup(path: Path) -> dict[str, Any]:
    if not path:
        return {"created": False, "reused": False, "reason": "backup_path_missing", "path": ""}
    if not path.exists():
        return {"created": False, "reused": False, "reason": "backup_path_not_found", "path": str(path)}
    try:
        size = int(path.stat().st_size)
    except OSError:
        size = 0
    if size <= 0:
        return {"created": False, "reused": False, "reason": "backup_path_empty", "path": str(path)}
    return {
        "created": False,
        "reused": True,
        "reason": "existing_backup_supplied",
        "path": str(path),
        "size_bytes": size,
    }


def _verify_backup_matches_candidate(backup: Mapping[str, Any], row: sqlite3.Row) -> dict[str, Any]:
    path = Path(str(backup.get("path") or "")).expanduser()
    candidate_id = int(row["id"] or 0)
    slug = str(row["slug"] or "")
    event_date = str(row["event_date"] or "")
    live_fact_json = str(row["fact_json"] or "")
    base = {
        "schema": "posterior_ssot_backup_candidate_verification_v1",
        "ok": False,
        "path": str(path),
        "candidate_fact_id": candidate_id,
        "slug": slug,
        "event_date": event_date,
        "live_fact_json_sha256": _sha256_text(live_fact_json),
    }
    if not path.exists():
        return {**base, "reason": "backup_path_not_found"}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {**base, "reason": "backup_open_failed", "sqlite_error": str(exc)}
    try:
        backup_row = conn.execute(
            """
            SELECT id, slug, event_date, fact_json
            FROM event_fact_history
            WHERE id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if backup_row is None:
            return {**base, "reason": "candidate_fact_missing_from_backup"}
        latest_row = conn.execute(
            "SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?",
            (slug, event_date),
        ).fetchone()
        backup_latest_id = int(latest_row["fact_id"] or 0) if latest_row else 0
        backup_fact_json = str(backup_row["fact_json"] or "")
        checks = {
            "slug_matches": str(backup_row["slug"] or "") == slug,
            "event_date_matches": str(backup_row["event_date"] or "") == event_date,
            "fact_json_hash_matches": _sha256_text(backup_fact_json) == _sha256_text(live_fact_json),
            "latest_pointer_matches": backup_latest_id == candidate_id,
        }
        ok = all(checks.values())
        return {
            **base,
            "ok": ok,
            "reason": "" if ok else "backup_candidate_mismatch",
            "backup_latest_fact_id": backup_latest_id,
            "backup_fact_json_sha256": _sha256_text(backup_fact_json),
            "checks": checks,
        }
    except sqlite3.Error as exc:
        return {**base, "reason": "backup_candidate_query_failed", "sqlite_error": str(exc)}
    finally:
        conn.close()


def apply_posterior_ssot_fact_repair(
    *,
    db_path: Path | str = DEFAULT_DB,
    fact_id: int = 0,
    slug: str = "",
    event_date: str = "",
    allow_apply: bool = False,
    confirm: str = "",
    writer_quiescent: bool = False,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    backup_path: Path | str | None = None,
    generated_at: str | None = None,
    maintenance_window_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    generated = generated_at or _now_utc()
    db = Path(db_path).expanduser().resolve()
    if not allow_apply:
        return _blocked("allow_apply_false", allow_apply=allow_apply, extra={"writer_quiescent": writer_quiescent})
    if confirm != CONFIRMATION_PHRASE:
        return _blocked(
            "confirmation_phrase_missing_or_invalid",
            allow_apply=allow_apply,
            extra={"writer_quiescent": writer_quiescent},
        )
    if not writer_quiescent:
        return _blocked("writer_quiescent_false", allow_apply=allow_apply, extra={"writer_quiescent": writer_quiescent})
    if not db.exists():
        return _blocked("archive_db_missing", allow_apply=allow_apply, extra={"writer_quiescent": writer_quiescent})
    maintenance_packet = _maintenance_window_packet(db, packet=maintenance_window_packet)
    compact_maintenance = _compact_maintenance_window(maintenance_packet)
    if maintenance_packet.get("writer_quiescent") is not True:
        return _blocked(
            "maintenance_window_not_quiescent",
            allow_apply=allow_apply,
            extra={
                "writer_quiescent": writer_quiescent,
                "maintenance_window": compact_maintenance,
            },
        )

    conn = _connect_rw(db)
    try:
        row, load_error = _load_candidate(
            conn,
            fact_id=int(fact_id or 0),
            slug=str(slug or ""),
            event_date=str(event_date or ""),
        )
        if row is None or load_error:
            return _blocked(
                load_error or "candidate_fact_row_missing",
                allow_apply=allow_apply,
                extra={
                    "fact_id": int(fact_id or 0),
                    "slug": slug,
                    "event_date": event_date,
                    "writer_quiescent": writer_quiescent,
                },
            )
        payload = _safe_json(row["fact_json"])
        if not payload:
            return _blocked(
                "candidate_fact_json_invalid",
                allow_apply=allow_apply,
                extra={"writer_quiescent": writer_quiescent},
            )
        effective_slug = str(row["slug"] or payload.get("slug") or "")
        effective_event_date = str(row["event_date"] or payload.get("event_date") or "")
        projection = build_fact_projection_no_apply(
            slug=effective_slug,
            event_date=effective_event_date,
            run_date=str(payload.get("run_date") or row["run_date"] or effective_event_date),
            fact_as_of=str(row["fact_as_of"] or payload.get("fact_as_of") or generated),
            payload=payload,
            current_fact_id=int(row["id"] or 0),
            current_fact_as_of=str(row["fact_as_of"] or ""),
            composer_version=str(row["composer_version"] or ""),
            conn=conn,
            write_reason="posterior_ssot_fact_repair_apply",
            enable_apply_contract=True,
            write_latest=True,
        )
        plan = _as_mapping(projection.get("plan"))
        if projection.get("ok") is not True:
            return _blocked(
                "posterior_projection_not_ok",
                allow_apply=allow_apply,
                extra={
                    "projection_mode": projection.get("projection_mode"),
                    "projection_issues": list(projection.get("issues") or []),
                    "writer_quiescent": writer_quiescent,
                },
            )
        if plan.get("ok") is not True or plan.get("apply_supported") is not True:
            return _blocked(
                "posterior_writer_plan_not_ready",
                allow_apply=allow_apply,
                extra={
                    "writer_plan_ok": bool(plan.get("ok")),
                    "writer_plan_apply_supported": bool(plan.get("apply_supported")),
                    "posterior_ssot_apply_contract": dict(_as_mapping(plan.get("posterior_ssot_apply_contract"))),
                    "writer_quiescent": writer_quiescent,
                },
            )
        backup = (
            _reuse_existing_backup(Path(backup_path).expanduser().resolve())
            if backup_path
            else _backup_sqlite(db, Path(backup_dir).expanduser().resolve(), generated)
        )
        if not (backup.get("created") or backup.get("reused")):
            return _blocked(
                "backup_failed",
                allow_apply=allow_apply,
                extra={"backup": backup, "writer_quiescent": writer_quiescent},
            )
        backup_verification = _verify_backup_matches_candidate(backup, row)
        backup["candidate_verification"] = backup_verification
        if backup_verification.get("ok") is not True:
            return _blocked(
                "backup_verification_failed",
                allow_apply=allow_apply,
                extra={
                    "backup": backup,
                    "backup_verification": backup_verification,
                    "writer_quiescent": writer_quiescent,
                },
            )
        writer_result = apply_fact_snapshot_write_plan(
            conn,
            plan,
            allow_apply=True,
            commit=True,
            created_at=generated,
        )
        summary = compact_fact_snapshot_writer_apply_result_for_lineage(writer_result)
        return {
            "schema": RESULT_SCHEMA,
            "ok": bool(writer_result.get("ok")),
            "applied": bool(writer_result.get("applied")),
            "blocked": bool(writer_result.get("blocked")),
            "block_reason": str(writer_result.get("block_reason") or ""),
            "allow_apply": True,
            "writer_quiescent": True,
            "maintenance_window": compact_maintenance,
            "slug": effective_slug,
            "event_date": effective_event_date,
            "candidate_fact_id": int(row["id"] or 0),
            "new_fact_id": writer_result.get("fact_id"),
            "backup": backup,
            "backup_verification": backup_verification,
            "projection_mode": str(projection.get("projection_mode") or ""),
            "writer_apply_result": writer_result,
            "writer_apply_result_summary": summary,
            "guards": dict(_as_mapping(writer_result.get("guards"))) or _guards(writes=bool(writer_result.get("applied"))),
        }
    finally:
        conn.close()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--fact-id", type=int, default=0)
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--backup", type=Path, default=None, help="Reuse an existing verified SQLite backup instead of creating a new one.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--result-json", type=Path, default=None)
    args = parser.parse_args(argv)
    result = apply_posterior_ssot_fact_repair(
        db_path=args.db,
        fact_id=int(args.fact_id or 0),
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        allow_apply=bool(args.apply),
        confirm=str(args.confirm or ""),
        writer_quiescent=bool(args.writer_quiescent),
        backup_dir=args.backup_dir,
        backup_path=args.backup,
    )
    if args.result_json:
        _write_json_atomic(args.result_json, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if bool(result.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
