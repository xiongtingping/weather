#!/usr/bin/env python3
"""Drill current-latest posterior SSOT materialization on a temporary DB copy.

This is a production-shape rehearsal for the current-latest posterior apply
runbook. It copies archive.db with SQLite's backup API, then calls the same
single-fact repair entrypoint against the temporary copy only. It never writes
the production database, creates a production backup, fetches weather data,
restarts launchd, places orders, or changes parameters.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import apply_posterior_ssot_fact_repair as repair  # noqa: E402
from scripts import current_latest_posterior_post_apply_acceptance as exact_acceptance  # noqa: E402
from scripts import current_latest_posterior_apply_runbook as runbook  # noqa: E402
from scripts import posterior_ssot_fact_coverage_audit as coverage_audit  # noqa: E402
from scripts import posterior_ssot_rematerialization_plan as rematerialization_plan  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "current_latest_posterior_apply_temp_drill_v1"
TEMP_DIR_PREFIXES = (
    "weather_current_latest_posterior_drill_",
    "weather_current_latest_posterior_drill_scoped_",
)


def _timer() -> float:
    return time.monotonic()


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _table_count(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(row["c"] or 0)


def _db_counts(db_path: Path) -> dict[str, int | None]:
    with _connect_ro(db_path) as conn:
        return {
            "event_fact_history": _table_count(conn, "event_fact_history"),
            "event_fact_latest": _table_count(conn, "event_fact_latest"),
            "event_fact_summary_cache": _table_count(conn, "event_fact_summary_cache"),
            "source_observations": _table_count(conn, "source_observations"),
            "daily_weather_reviews": _table_count(conn, "daily_weather_reviews"),
            "auto_trade_decisions": _table_count(conn, "auto_trade_decisions"),
            "auto_orders": _table_count(conn, "auto_orders"),
        }


def _temp_maintenance_window_packet(db_path: Path) -> dict[str, Any]:
    return {
        "schema": "controlled_apply_maintenance_window_packet_v1",
        "ok": True,
        "writer_quiescent": True,
        "db": str(Path(db_path).expanduser().resolve()),
        "counts": {
            "db_holder_process_count": 0,
            "writer_quiescence_blocker_count": 0,
            "reader_warning_count": 0,
            "write_capable_handle_process_count": 0,
        },
        "required_stop_labels": [],
        "blockers": [],
        "stop_authorization": {
            "blockers": [],
            "can_pause_required_services_now": True,
        },
        "guards": {
            "writes_archive_db": False,
            "restarts_launchd": False,
            "places_orders": False,
        },
        "next_boundary": "temp_db_private_copy_ready_for_drill_apply",
    }


def _copy_sqlite_db_to_temp(db_path: Path) -> tuple[Path, Path]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="weather_current_latest_posterior_drill_"))
    tmp_db = tmp_dir / db_path.name
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        dest = sqlite3.connect(str(tmp_db), timeout=30)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return tmp_dir, tmp_db


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row["name"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]


def _copy_schema_only(source: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    rows = source.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE sql IS NOT NULL
          AND name NOT LIKE 'sqlite_%'
          AND type IN ('table', 'index', 'trigger')
        ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name
        """
    ).fetchall()
    for row in rows:
        sql = str(row["sql"] if isinstance(row, sqlite3.Row) else row[2] or "").strip()
        if sql:
            dest.execute(sql)


def _insert_rows(dest: sqlite3.Connection, table: str, rows: Sequence[sqlite3.Row]) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    quoted = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    dest.executemany(
        f'INSERT OR IGNORE INTO "{table}"({quoted}) VALUES ({placeholders})',
        [tuple(row[column] for column in columns) for row in rows],
    )
    return len(rows)


def _pair_where_clause(pairs: Sequence[tuple[str, str]], *, slug_column: str = "slug", event_date_column: str = "event_date") -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for slug, event_date in pairs:
        if not slug or not event_date:
            continue
        clauses.append(f"({slug_column}=? AND {event_date_column}=?)")
        params.extend([slug, event_date])
    if not clauses:
        return "1=0", []
    return " OR ".join(clauses), params


def _copy_scoped_sqlite_db_to_temp(db_path: Path, candidates: Sequence[Mapping[str, Any]]) -> tuple[Path, Path, dict[str, Any]]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="weather_current_latest_posterior_drill_scoped_"))
    tmp_db = tmp_dir / db_path.name
    pairs = sorted(
        {
            (str(candidate.get("slug") or ""), str(candidate.get("event_date") or ""))
            for candidate in candidates
            if str(candidate.get("slug") or "") and str(candidate.get("event_date") or "")
        }
    )
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    source.row_factory = sqlite3.Row
    dest = sqlite3.connect(str(tmp_db), timeout=30)
    dest.row_factory = sqlite3.Row
    copied: dict[str, int] = {}
    try:
        _copy_schema_only(source, dest)
        table_set = set(_table_names(source))
        history_fact_ids: list[int] = []
        if "event_fact_history" in table_set and pairs:
            where, params = _pair_where_clause(pairs)
            rows = source.execute(
                f"SELECT * FROM event_fact_history WHERE {where} ORDER BY id ASC",
                params,
            ).fetchall()
            copied["event_fact_history"] = _insert_rows(dest, "event_fact_history", rows)
            history_fact_ids = [int(row["id"] or 0) for row in rows if int(row["id"] or 0) > 0]
        if "event_fact_latest" in table_set and pairs:
            where, params = _pair_where_clause(pairs)
            rows = source.execute(
                f"SELECT * FROM event_fact_latest WHERE {where}",
                params,
            ).fetchall()
            copied["event_fact_latest"] = _insert_rows(dest, "event_fact_latest", rows)
        if "event_fact_summary_cache" in table_set and pairs:
            where, params = _pair_where_clause(pairs)
            rows = source.execute(
                f"SELECT * FROM event_fact_summary_cache WHERE {where}",
                params,
            ).fetchall()
            copied["event_fact_summary_cache"] = _insert_rows(dest, "event_fact_summary_cache", rows)
        if "source_observations" in table_set:
            try:
                if pairs and "event_date" in [row["name"] for row in source.execute("PRAGMA table_info(source_observations)").fetchall()]:
                    event_dates = sorted({event_date for _, event_date in pairs if event_date})
                    placeholders = ",".join("?" for _ in event_dates)
                    rows = source.execute(
                        f"SELECT * FROM source_observations WHERE event_date IN ({placeholders})",
                        event_dates,
                    ).fetchall()
                else:
                    rows = source.execute("SELECT * FROM source_observations").fetchall()
            except sqlite3.Error:
                rows = []
            copied["source_observations"] = _insert_rows(dest, "source_observations", rows)
        dest.commit()
    except Exception:
        dest.close()
        source.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    finally:
        try:
            dest.close()
        finally:
            source.close()
    return tmp_dir, tmp_db, {
        "schema": "current_latest_posterior_scoped_temp_db_copy_v1",
        "copy_mode": "scoped_same_slug_event_date",
        "source_db": str(db_path),
        "temp_db": str(tmp_db),
        "slug_event_pairs": [{"slug": slug, "event_date": event_date} for slug, event_date in pairs],
        "history_fact_ids": history_fact_ids,
        "copied_row_counts": copied,
        "boundary": "scoped_temp_copy_for_current_latest_drill_no_production_write",
    }


def _copy_sqlite_backup(source_db: Path, backup_path: Path) -> str:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.unlink(missing_ok=True)
    try:
        shutil.copy2(source_db, backup_path)
        if backup_path.exists() and backup_path.stat().st_size > 0:
            return "file_copy"
    except OSError:
        backup_path.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(backup_path), timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return "sqlite_backup"


def _temp_drill_disk_preflight(db_path: Path) -> dict[str, Any]:
    try:
        db_size = int(db_path.stat().st_size)
    except OSError:
        db_size = 0
    usage = shutil.disk_usage(tempfile.gettempdir())
    margin = 512 * 1024 * 1024
    required = max(margin, db_size * 2 + margin)
    return {
        "schema": "current_latest_posterior_temp_drill_disk_preflight_v1",
        "ok": usage.free >= required,
        "temp_root": tempfile.gettempdir(),
        "archive_db_size_bytes": db_size,
        "free_bytes": int(usage.free),
        "required_free_bytes": int(required),
        "required_components": {
            "temp_archive_copy_bytes": db_size,
            "temp_archive_backup_bytes": db_size,
            "safety_margin_bytes": margin,
        },
        "block_reason": "" if usage.free >= required else "insufficient_temp_drill_disk_space",
        "boundary": "preflight_before_temp_db_copy_no_production_write",
    }


def _path_size_bytes(path: Path) -> int:
    total = 0
    if path.is_symlink():
        return 0
    if path.is_file():
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0
    for child in path.rglob("*"):
        if child.is_symlink():
            continue
        try:
            if child.is_file():
                total += int(child.stat().st_size)
        except OSError:
            continue
    return total


def cleanup_stale_temp_dirs(
    *,
    temp_root: Path | str | None = None,
    min_age_seconds: float = 3600.0,
    apply: bool = False,
    max_items_output: int = 20,
) -> dict[str, Any]:
    root = Path(temp_root or tempfile.gettempdir()).expanduser().resolve()
    now = time.time()
    scanned = 0
    eligible: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    if root.exists():
        for child in root.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            if not any(child.name.startswith(prefix) for prefix in TEMP_DIR_PREFIXES):
                continue
            scanned += 1
            try:
                stat = child.stat()
            except OSError:
                continue
            age_seconds = max(0.0, now - float(stat.st_mtime))
            if age_seconds < float(min_age_seconds):
                continue
            row = {
                "path": str(child),
                "age_seconds": int(age_seconds),
                "size_bytes": _path_size_bytes(child),
            }
            eligible.append(row)
            if apply:
                shutil.rmtree(child, ignore_errors=True)
                row = dict(row)
                row["removed"] = not child.exists()
                removed.append(row)
    total_eligible_bytes = sum(int(row.get("size_bytes") or 0) for row in eligible)
    total_removed_bytes = sum(int(row.get("size_bytes") or 0) for row in removed if row.get("removed"))
    return {
        "schema": "current_latest_posterior_temp_dir_cleanup_v1",
        "ok": True,
        "temp_root": str(root),
        "apply": bool(apply),
        "min_age_seconds": float(min_age_seconds),
        "prefixes": list(TEMP_DIR_PREFIXES),
        "scanned_temp_dir_count": scanned,
        "eligible_count": len(eligible),
        "removed_count": sum(1 for row in removed if row.get("removed")),
        "eligible_bytes": total_eligible_bytes,
        "removed_bytes": total_removed_bytes,
        "eligible_items": eligible[: max(0, int(max_items_output))],
        "removed_items": removed[: max(0, int(max_items_output))],
        "items_truncated": len(eligible) > max(0, int(max_items_output)),
        "boundary": "only_directories_with_current_latest_posterior_temp_drill_prefix_under_temp_root",
    }


def _runbook_candidates(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in _as_sequence(packet.get("single_fact_preflights")):
        row = _as_mapping(raw)
        fact_id = int(row.get("fact_id") or 0)
        if fact_id <= 0:
            continue
        rows.append(
            {
                "fact_id": fact_id,
                "ok": bool(row.get("ok")),
                "risk_level": row.get("risk_level"),
                "blockers": list(row.get("blockers") or []),
                "slug": str(row.get("slug") or ""),
                "event_date": str(row.get("event_date") or ""),
                "projected_target_bucket": row.get("projected_target_bucket"),
                "projected_target_probability": row.get("projected_target_probability"),
                "uses_shared_backup": bool(row.get("uses_shared_backup")),
            }
        )
    return rows


def _compact_apply_result(result: Mapping[str, Any]) -> dict[str, Any]:
    summary = _as_mapping(result.get("writer_apply_result_summary"))
    backup = _as_mapping(result.get("backup"))
    backup_verification = _as_mapping(result.get("backup_verification"))
    maintenance = _as_mapping(result.get("maintenance_window"))
    writer_apply = _as_mapping(result.get("writer_apply_result"))
    return {
        "ok": bool(result.get("ok")),
        "applied": bool(result.get("applied")),
        "blocked": bool(result.get("blocked")),
        "block_reason": str(result.get("block_reason") or ""),
        "error": str(result.get("error") or writer_apply.get("error") or ""),
        "sqlite_error": str(result.get("sqlite_error") or writer_apply.get("sqlite_error") or ""),
        "writer_apply_blockers": list(writer_apply.get("blockers") or [])[:20],
        "candidate_fact_id": result.get("candidate_fact_id"),
        "new_fact_id": result.get("new_fact_id"),
        "slug": result.get("slug"),
        "event_date": result.get("event_date"),
        "target_bucket": summary.get("target_bucket"),
        "target_probability": summary.get("target_probability"),
        "backup_reused": bool(backup.get("reused")),
        "backup_created": bool(backup.get("created")),
        "backup_verification_ok": bool(backup_verification.get("ok")),
        "maintenance_window_writer_quiescent": bool(maintenance.get("writer_quiescent")),
        "guards": dict(_as_mapping(result.get("guards"))),
    }


def _guards(
    *,
    writes_temp_archive_db: bool = True,
    creates_temp_backup: bool = True,
) -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_temp_archive_db": bool(writes_temp_archive_db),
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_event_fact_summary_cache": False,
        "writes_daily_weather_reviews": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_orders": False,
        "creates_production_backup": False,
        "creates_temp_backup": bool(creates_temp_backup),
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "restarts_launchd": False,
    }


def run_drill(
    *,
    db_path: Path | str = DEFAULT_DB,
    fact_ids: str | Sequence[int] | None = None,
    since_hours: float = 168.0,
    limit: int = 100,
    max_items_output: int = 20,
    keep_temp: bool = False,
) -> dict[str, Any]:
    source_db = Path(db_path).expanduser().resolve()
    stage_durations_ms: dict[str, int] = {}
    started = _timer()
    production_counts_before = _db_counts(source_db)

    stage_start = _timer()
    production_runbook = runbook.build_runbook(
        source_db,
        fact_ids=runbook._parse_fact_ids(fact_ids),
        since_hours=float(since_hours),
        limit=int(limit),
        max_items_output=int(max_items_output),
    )
    stage_durations_ms["build_production_runbook_no_apply"] = _elapsed_ms(stage_start)
    candidates = _runbook_candidates(production_runbook)
    disk_preflight = _temp_drill_disk_preflight(source_db)
    scoped_copy_required = disk_preflight.get("ok") is not True and bool(candidates)
    if disk_preflight.get("ok") is not True and not scoped_copy_required:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "execution_mode": "current_latest_posterior_apply_temp_drill_blocked",
            "db": str(source_db),
            "read_only_for_production": True,
            "production_counts_before": production_counts_before,
            "source_runbook": {
                "ok": bool(production_runbook.get("ok")),
                "apply_required": bool(_as_mapping(production_runbook.get("scope")).get("apply_required")),
                "ready_for_controlled_apply_now": bool(production_runbook.get("ready_for_controlled_apply_now")),
                "candidate_fact_ids": list(_as_mapping(production_runbook.get("scope")).get("candidate_fact_ids") or []),
                "start_blockers": list(production_runbook.get("start_blockers") or []),
                "risk_blockers": list(production_runbook.get("risk_blockers") or []),
            },
            "candidate_count": len(candidates),
            "candidate_samples": candidates[: max(0, int(max_items_output))],
            "disk_preflight": disk_preflight,
            "blockers": [str(disk_preflight.get("block_reason") or "temp_drill_disk_preflight_failed")],
            "stage_durations_ms": stage_durations_ms,
            "elapsed_ms": _elapsed_ms(started),
            "guards": _guards(),
            "next_boundary": "free_disk_space_or_use_smaller_temp_drill_before_production_apply",
        }

    stage_start = _timer()
    if scoped_copy_required:
        tmp_dir, tmp_db, temp_copy_context = _copy_scoped_sqlite_db_to_temp(source_db, candidates)
        backup_mode_label = "scoped_sqlite_backup"
    else:
        tmp_dir, tmp_db = _copy_sqlite_db_to_temp(source_db)
        temp_copy_context = {
            "schema": "current_latest_posterior_full_temp_db_copy_v1",
            "copy_mode": "full_sqlite_backup_copy",
            "source_db": str(source_db),
            "temp_db": str(tmp_db),
            "boundary": "full_temp_copy_for_production_shape_drill_no_production_write",
        }
        backup_mode_label = "full_sqlite_backup"
    stage_durations_ms["copy_source_to_temp"] = _elapsed_ms(stage_start)
    retained = bool(keep_temp)
    backup_path = tmp_dir / f"{tmp_db.name}.current-latest-posterior-drill.bak"
    try:
        stage_start = _timer()
        backup_mode = _copy_sqlite_backup(tmp_db, backup_path)
        stage_durations_ms["create_temp_shared_backup"] = _elapsed_ms(stage_start)

        apply_results: list[dict[str, Any]] = []
        stage_start = _timer()
        for candidate in candidates:
            if candidate["ok"] is not True:
                apply_results.append(
                    {
                        "ok": False,
                        "applied": False,
                        "blocked": True,
                        "block_reason": "preflight_candidate_not_ok",
                        "candidate_fact_id": candidate["fact_id"],
                        "slug": candidate["slug"],
                        "event_date": candidate["event_date"],
                        "preflight_blockers": list(candidate.get("blockers") or []),
                    }
                )
                continue
            result = repair.apply_posterior_ssot_fact_repair(
                db_path=tmp_db,
                fact_id=int(candidate["fact_id"]),
                slug=str(candidate["slug"]),
                event_date=str(candidate["event_date"]),
                allow_apply=True,
                confirm=repair.CONFIRMATION_PHRASE,
                writer_quiescent=True,
                backup_dir=tmp_dir,
                backup_path=backup_path,
                maintenance_window_packet=_temp_maintenance_window_packet(tmp_db),
            )
            apply_results.append(_compact_apply_result(result))
        stage_durations_ms["apply_current_latest_repairs_on_temp"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        latest_coverage = coverage_audit.audit(
            tmp_db,
            since_hours=float(since_hours),
            limit=int(limit),
            max_items_output=int(max_items_output),
            project_forward=True,
            latest_only=True,
        )
        stage_durations_ms["post_apply_latest_coverage_audit"] = _elapsed_ms(stage_start)

        candidate_fact_ids = [int(candidate["fact_id"]) for candidate in candidates]
        expected_projections = [
            {
                "fact_id": int(candidate["fact_id"]),
                "target_bucket": candidate.get("projected_target_bucket"),
                "target_probability": candidate.get("projected_target_probability"),
            }
            for candidate in candidates
        ]
        stage_start = _timer()
        if candidate_fact_ids:
            post_apply_exact_acceptance = exact_acceptance.build_acceptance(
                tmp_db,
                candidate_fact_ids=candidate_fact_ids,
                expected_projections=expected_projections,
                max_items_output=int(max_items_output),
            )
        else:
            post_apply_exact_acceptance = {
                "schema": exact_acceptance.SCHEMA_VERSION,
                "ok": True,
                "skipped": True,
                "skip_reason": "no_current_latest_candidates",
                "candidate_fact_ids": [],
                "candidate_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
                "blockers": [],
                "guards": exact_acceptance._guards(),
                "next_boundary": "no_current_latest_posterior_apply_required",
            }
        stage_durations_ms["post_apply_exact_candidate_acceptance"] = _elapsed_ms(stage_start)

        stage_start = _timer()
        remat = rematerialization_plan.build_plan(
            tmp_db,
            since_hours=float(since_hours),
            limit=int(limit),
            max_items_output=int(max_items_output),
        )
        stage_durations_ms["post_apply_rematerialization_plan"] = _elapsed_ms(stage_start)

        production_counts_after = _db_counts(source_db)
        production_unchanged = production_counts_before == production_counts_after
        remat_scope = _as_mapping(remat.get("historical_rematerialization_plan"))
        current_latest_issue_count = int(remat_scope.get("current_latest_issue_count") or 0)
        apply_required = bool(_as_mapping(production_runbook.get("scope")).get("apply_required"))
        candidate_count = len(candidates)
        all_candidates_applied = all(bool(row.get("ok")) and bool(row.get("applied")) for row in apply_results)
        no_candidates = candidate_count == 0 and not apply_required
        latest_coverage_ok = bool(latest_coverage.get("ok"))
        exact_acceptance_ok = bool(post_apply_exact_acceptance.get("ok"))
        remat_ok = bool(remat.get("ok"))
        ok = bool(
            production_unchanged
            and remat_ok
            and (no_candidates or all_candidates_applied)
            and current_latest_issue_count == 0
            and latest_coverage_ok
            and exact_acceptance_ok
        )
        return {
            "schema": SCHEMA_VERSION,
            "ok": ok,
            "execution_mode": "current_latest_posterior_apply_temp_drill",
            "db": str(source_db),
            "temp_db": str(tmp_db),
            "temp_db_retained": retained,
            "backup_path": str(backup_path),
            "backup_exists": backup_path.exists(),
            "temp_backup_mode": backup_mode,
            "temp_copy_mode": temp_copy_context.get("copy_mode"),
            "temp_copy_context": temp_copy_context,
            "temp_copy_disk_preflight": disk_preflight,
            "temp_copy_fallback_used": bool(scoped_copy_required),
            "temp_copy_fallback_reason": (
                "full_temp_copy_insufficient_disk_space"
                if scoped_copy_required
                else ""
            ),
            "temp_copy_backup_mode_label": backup_mode_label,
            "read_only_for_production": True,
            "production_counts_before": production_counts_before,
            "production_counts_after": production_counts_after,
            "production_counts_unchanged": production_unchanged,
            "source_runbook": {
                "ok": bool(production_runbook.get("ok")),
                "apply_required": apply_required,
                "ready_for_controlled_apply_now": bool(production_runbook.get("ready_for_controlled_apply_now")),
                "candidate_fact_ids": list(_as_mapping(production_runbook.get("scope")).get("candidate_fact_ids") or []),
                "start_blockers": list(production_runbook.get("start_blockers") or []),
                "risk_blockers": list(production_runbook.get("risk_blockers") or []),
            },
            "candidate_count": candidate_count,
            "candidate_samples": candidates[: max(0, int(max_items_output))],
            "apply_results": apply_results[: max(0, int(max_items_output))],
            "apply_result_count": len(apply_results),
            "all_candidates_applied_on_temp": all_candidates_applied,
            "latest_coverage": {
                "ok": latest_coverage_ok,
                "blockers": list(latest_coverage.get("blockers") or [])[:20],
                "issue_counts": dict(_as_mapping(latest_coverage.get("issue_counts"))),
            },
            "post_apply_exact_acceptance": {
                "ok": exact_acceptance_ok,
                "skipped": bool(post_apply_exact_acceptance.get("skipped")),
                "skip_reason": post_apply_exact_acceptance.get("skip_reason"),
                "candidate_count": int(post_apply_exact_acceptance.get("candidate_count") or 0),
                "expected_projection_count": int(
                    post_apply_exact_acceptance.get("expected_projection_count") or 0
                ),
                "expected_projection_source": post_apply_exact_acceptance.get("expected_projection_source"),
                "accepted_count": int(post_apply_exact_acceptance.get("accepted_count") or 0),
                "rejected_count": int(post_apply_exact_acceptance.get("rejected_count") or 0),
                "blockers": list(post_apply_exact_acceptance.get("blockers") or [])[:20],
                "items": list(post_apply_exact_acceptance.get("items") or [])[: max(0, int(max_items_output))],
            },
            "post_apply_rematerialization": {
                "ok": remat_ok,
                "candidate_count": int(remat_scope.get("candidate_count") or 0),
                "current_latest_issue_count": current_latest_issue_count,
                "historical_only_issue_count": int(remat_scope.get("historical_only_issue_count") or 0),
                "current_latest_projected_issue_count": int(
                    remat_scope.get("current_latest_projected_issue_count") or 0
                ),
            },
            "stage_durations_ms": stage_durations_ms,
            "elapsed_ms": _elapsed_ms(started),
            "guards": _guards(),
            "next_boundary": (
                "operator_may_use_runbook_in_writer_quiescent_window_after_reviewing_temp_drill"
                if ok and apply_required
                else "no_current_latest_posterior_apply_required"
                if ok
                else "fix_temp_drill_blockers_before_production_apply"
            ),
        }
    finally:
        if not retained:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--fact-ids", default="", help="Optional comma-separated exact current-latest fact ids.")
    parser.add_argument("--since-hours", type=float, default=168.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument(
        "--cleanup-stale-temp-dirs",
        action="store_true",
        help="Remove stale temp drill directories under the system temp root before running.",
    )
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only run stale temp directory cleanup and exit.",
    )
    parser.add_argument(
        "--stale-temp-min-age-minutes",
        type=float,
        default=60.0,
        help="Minimum temp directory age for --cleanup-stale-temp-dirs.",
    )
    args = parser.parse_args(argv)
    cleanup_report = None
    if args.cleanup_stale_temp_dirs or args.cleanup_only:
        cleanup_report = cleanup_stale_temp_dirs(
            min_age_seconds=max(0.0, float(args.stale_temp_min_age_minutes) * 60.0),
            apply=bool(args.cleanup_stale_temp_dirs),
            max_items_output=int(args.max_items_output),
        )
    if args.cleanup_only:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": bool(_as_mapping(cleanup_report).get("ok")),
            "execution_mode": "current_latest_posterior_temp_drill_cleanup_only",
            "read_only_for_production": True,
            "cleanup_stale_temp_dirs": cleanup_report,
            "guards": _guards(writes_temp_archive_db=False, creates_temp_backup=False),
            "next_boundary": "temp_drill_stale_temp_dir_cleanup_complete",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return 0 if result.get("ok") else 1
    try:
        result = run_drill(
            db_path=args.db,
            fact_ids=runbook._parse_fact_ids(args.fact_ids),
            since_hours=float(args.since_hours),
            limit=int(args.limit),
            max_items_output=int(args.max_items_output),
            keep_temp=bool(args.keep_temp),
        )
        if cleanup_report is not None:
            result["cleanup_stale_temp_dirs"] = cleanup_report
    except Exception as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "execution_mode": "current_latest_posterior_apply_temp_drill_exception",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "read_only_for_production": True,
            "cleanup_stale_temp_dirs": cleanup_report,
            "guards": _guards(),
            "next_boundary": "fix_temp_drill_exception_before_production_apply",
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
