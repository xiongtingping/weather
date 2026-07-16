#!/usr/bin/env python3
"""Build a compact archive.db candidate into a separate file.

This never replaces data/archive.db. The source database is attached read-only,
and the output path must be a new file unless --force is provided.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import archive_db_rebuild_dry_run as dry_run  # noqa: E402
from auto_trade_job_writer import copy_auto_trade_jobs_for_archive_rebuild  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "archive_rebuilds"
DEFAULT_REPORT_DIR = ROOT / "data" / "reports"
ACTIVE_JOB_STATES = tuple(sorted(dry_run.ACTIVE_JOB_STATES))
CANDIDATE_TABLES = set(dry_run.CANDIDATE_TABLES)
CANDIDATE_COPY_BOUNDARY_SCHEMA = "archive_db_rebuild_candidate_copy_boundary_v1"


def _utc_stamp() -> str:
    return datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def _now_utc() -> str:
    return datetime.now(ZoneInfo("UTC")).replace(microsecond=0).isoformat()


def build_archive_rebuild_candidate_copy_boundary(
    *,
    source_db: Path | str = "",
    output_db: Path | str = "",
) -> dict[str, Any]:
    return {
        "schema": CANDIDATE_COPY_BOUNDARY_SCHEMA,
        "read_only": True,
        "execution_mode": "candidate_db_table_copy_not_fact_snapshot_mutation",
        "source_db": str(source_db or ""),
        "output_db": str(output_db or ""),
        "source_db_mode": "attach_read_only",
        "output_scope": "candidate_archive_db_file",
        "write_owner": "scripts/archive_db_rebuild_build.py",
        "fact_writer_integration": {
            "status": "not_applicable_for_candidate_copy",
            "reason": "archive_rebuild_copies_tables_into_a_separate_candidate_db_and_does_not_append_or_repoint_live_fact_snapshot",
        },
        "guards": {
            "writes_archive_db": False,
            "writes_candidate_archive_db": True,
            "replaces_source_db": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "historical_backfill": False,
            "restarts_launchd": False,
            "changes_parameters": False,
            "executes_sql_against_source_db": False,
        },
        "allowed_sql_scope": [
            "CREATE/INSERT/INDEX only inside output candidate DB",
            "SELECT only from read-only attached source DB",
        ],
        "blocked_operations": [
            "replace_source_archive_db",
            "append_live_event_fact_history",
            "repoint_live_event_fact_latest",
            "write_live_event_fact_summary_cache",
            "place_order",
            "restart_launchd",
        ],
    }


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _fmt_bytes(value: int | float) -> str:
    return dry_run._fmt_bytes(value)


def _jsonable_int(value: Any) -> int:
    return dry_run._jsonable_int(value)


def _connect_dest(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _attach_source(conn: sqlite3.Connection, source: Path) -> None:
    conn.execute("ATTACH DATABASE ? AS src", (f"file:{source}?mode=ro",))


def _schema_rows(conn: sqlite3.Connection, object_type: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM src.sqlite_master
            WHERE type = ?
              AND name NOT LIKE 'sqlite_%'
              AND sql IS NOT NULL
            ORDER BY name
            """,
            (object_type,),
        )
    )


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["name"])
        for row in conn.execute(
            """
            SELECT name
            FROM src.sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA src.table_info({_q(table)})")]


def _count(conn: sqlite3.Connection, schema: str, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {_q(schema)}.{_q(table)}").fetchone()
    return _jsonable_int(row["n"] if row else 0)


def _exec_script_rows(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row], *, label: str) -> list[str]:
    errors: list[str] = []
    for row in rows:
        sql = str(row["sql"] or "").strip()
        if not sql:
            continue
        try:
            conn.execute(sql)
        except sqlite3.Error as exc:
            errors.append(f"{label}:{row['name']}:{exc}")
    return errors


def _insert_all_sql(table: str) -> str:
    return f"INSERT INTO main.{_q(table)} SELECT * FROM src.{_q(table)}"


def _copy_full_table(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    source_rows = _count(conn, "src", table)
    before = conn.total_changes
    conn.execute(_insert_all_sql(table))
    copied = conn.total_changes - before
    return {
        "table": table,
        "rows_copied": int(copied),
        "source_rows_snapshot": source_rows,
        "policy": "copy_full",
        "snapshot_count_match": int(copied) == source_rows,
    }


def _copy_by_keep_table(conn: sqlite3.Connection, table: str, keep_table: str) -> int:
    before = conn.total_changes
    conn.execute(
        f"""
        INSERT INTO main.{_q(table)}
        SELECT *
        FROM src.{_q(table)}
        WHERE id IN (SELECT id FROM temp.{_q(keep_table)})
        """
    )
    return int(conn.total_changes - before)


def _prepare_latest_keep_table(
    conn: sqlite3.Connection,
    *,
    source_table: str,
    keep_table: str,
    cutoff: str,
) -> int:
    conn.execute(f"DROP TABLE IF EXISTS temp.{_q(keep_table)}")
    conn.execute(f"CREATE TEMP TABLE {_q(keep_table)} (id INTEGER PRIMARY KEY)")
    conn.execute(
        f"""
        INSERT OR IGNORE INTO temp.{_q(keep_table)}(id)
        SELECT id
        FROM src.{_q(source_table)}
        WHERE event_date >= ?
        """,
        (cutoff,),
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO temp.{_q(keep_table)}(id)
        SELECT id
        FROM (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY slug, event_date
                    ORDER BY created_at DESC, id DESC
                ) AS rn
            FROM src.{_q(source_table)}
        )
        WHERE rn = 1
        """
    )
    row = conn.execute(f"SELECT COUNT(*) AS n FROM temp.{_q(keep_table)}").fetchone()
    return _jsonable_int(row["n"] if row else 0)


def _copy_latest_by_event_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    cutoff: str,
) -> dict[str, Any]:
    keep_table = f"keep_{table}"
    keep_rows = _prepare_latest_keep_table(
        conn,
        source_table=table,
        keep_table=keep_table,
        cutoff=cutoff,
    )
    copied = _copy_by_keep_table(conn, table, keep_table)
    return {
        "table": table,
        "policy": "keep_recent_by_event_date_and_latest_per_slug_event_date",
        "cutoff": cutoff,
        "keep_rows": keep_rows,
        "rows_copied": copied,
    }


def _copy_auto_trade_jobs(conn: sqlite3.Connection, *, cutoff: str) -> dict[str, Any]:
    copied = copy_auto_trade_jobs_for_archive_rebuild(
        conn,
        active_states=ACTIVE_JOB_STATES,
        cutoff=cutoff,
    )
    return {
        "table": "auto_trade_jobs",
        "policy": "keep_recent_created_at_and_active_states",
        "cutoff": cutoff,
        "active_states": list(ACTIVE_JOB_STATES),
        "rows_copied": copied,
    }


def _copy_event_market_ticks(conn: sqlite3.Connection, *, cutoff: str) -> dict[str, Any]:
    cutoff_dt = f"{cutoff}T00:00:00+00:00"
    conn.execute("DROP TABLE IF EXISTS temp.keep_event_market_ticks")
    conn.execute("CREATE TEMP TABLE keep_event_market_ticks (id INTEGER PRIMARY KEY)")
    conn.execute(
        """
        INSERT OR IGNORE INTO temp.keep_event_market_ticks(id)
        SELECT id
        FROM src.event_market_ticks
        WHERE created_at >= ?
        """,
        (cutoff_dt,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO temp.keep_event_market_ticks(id)
        SELECT MAX(id)
        FROM src.event_market_ticks
        GROUP BY slug, event_date, outcome, side
        """
    )
    keep_rows = _jsonable_int(
        conn.execute("SELECT COUNT(*) AS n FROM temp.keep_event_market_ticks").fetchone()["n"]
    )
    copied = _copy_by_keep_table(conn, "event_market_ticks", "keep_event_market_ticks")
    return {
        "table": "event_market_ticks",
        "policy": "keep_recent_created_at_and_latest_per_slug_event_outcome_side",
        "cutoff_created_at": cutoff_dt,
        "keep_rows": keep_rows,
        "rows_copied": copied,
    }


def _compact_fact_json(payload_text: str, *, fact_as_of: str) -> tuple[str, bool]:
    try:
        import ui.server as server  # noqa: WPS433 - reuse the runtime SSOT compaction helper.

        payload = json.loads(str(payload_text or "{}"))
        compact = server._compact_fact_payload_for_history_storage(
            payload,
            fact_as_of=fact_as_of,
            compacted_at="REBUILD",
        )
        compact_text = json.dumps(compact, ensure_ascii=False)
        if compact_text and len(compact_text.encode("utf-8")) < len(str(payload_text or "").encode("utf-8")):
            return compact_text, True
    except Exception:
        pass
    return str(payload_text or ""), False


def _copy_event_fact_history(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    batch_size: int,
) -> dict[str, Any]:
    latest_ids = {
        int(row["fact_id"])
        for row in conn.execute("SELECT fact_id FROM src.event_fact_latest WHERE fact_id IS NOT NULL")
    }
    columns = _columns(conn, "event_fact_history")
    fact_json_idx = columns.index("fact_json")
    event_date_idx = columns.index("event_date")
    fact_as_of_idx = columns.index("fact_as_of")
    id_idx = columns.index("id")
    insert_sql = (
        f"INSERT INTO main.event_fact_history ({','.join(_q(col) for col in columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})"
    )
    rows_copied = latest_rows = compact_rows = drop_rows = compact_fallback_rows = 0
    compact_original_bytes = compact_new_bytes = 0
    batch: list[tuple[Any, ...]] = []

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        conn.executemany(insert_sql, batch)
        batch = []

    for row in conn.execute(f"SELECT * FROM src.event_fact_history ORDER BY id"):
        values = list(tuple(row))
        fact_id = int(values[id_idx] or 0)
        event_date = str(values[event_date_idx] or "")
        if fact_id in latest_ids:
            latest_rows += 1
        elif event_date >= cutoff:
            original = str(values[fact_json_idx] or "")
            compact_text, compacted = _compact_fact_json(original, fact_as_of=str(values[fact_as_of_idx] or ""))
            values[fact_json_idx] = compact_text
            compact_rows += 1
            compact_original_bytes += len(original.encode("utf-8"))
            compact_new_bytes += len(compact_text.encode("utf-8"))
            if not compacted:
                compact_fallback_rows += 1
        else:
            drop_rows += 1
            continue
        batch.append(tuple(values))
        rows_copied += 1
        if len(batch) >= batch_size:
            flush()
    flush()
    return {
        "table": "event_fact_history",
        "policy": "keep_latest_full_compact_recent_nonlatest_drop_older_nonlatest",
        "cutoff": cutoff,
        "rows_copied": rows_copied,
        "latest_rows": latest_rows,
        "compact_rows": compact_rows,
        "drop_rows": drop_rows,
        "compact_fallback_rows": compact_fallback_rows,
        "compact_original_bytes": compact_original_bytes,
        "compact_new_bytes": compact_new_bytes,
        "compact_ratio_actual": (
            compact_new_bytes / float(compact_original_bytes)
            if compact_original_bytes
            else None
        ),
    }


def _copy_event_fact_summary_cache(conn: sqlite3.Connection, *, cutoff: str) -> dict[str, Any]:
    conn.execute("DROP TABLE IF EXISTS temp.keep_event_fact_summary_cache")
    conn.execute("CREATE TEMP TABLE keep_event_fact_summary_cache (fact_id INTEGER PRIMARY KEY)")
    conn.execute(
        """
        INSERT OR IGNORE INTO temp.keep_event_fact_summary_cache(fact_id)
        SELECT fact_id
        FROM src.event_fact_latest
        WHERE fact_id IS NOT NULL
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO temp.keep_event_fact_summary_cache(fact_id)
        SELECT fact_id
        FROM src.event_fact_summary_cache
        WHERE event_date >= ?
        """,
        (cutoff,),
    )
    keep_rows = _jsonable_int(
        conn.execute("SELECT COUNT(*) AS n FROM temp.keep_event_fact_summary_cache").fetchone()["n"]
    )
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO main.event_fact_summary_cache
        SELECT *
        FROM src.event_fact_summary_cache
        WHERE fact_id IN (SELECT fact_id FROM temp.keep_event_fact_summary_cache)
        """
    )
    copied = int(conn.total_changes - before)
    return {
        "table": "event_fact_summary_cache",
        "policy": "keep_latest_fact_cache_and_recent_event_date",
        "cutoff": cutoff,
        "keep_rows": keep_rows,
        "rows_copied": copied,
    }


def _set_user_version(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA src.user_version").fetchone()
    user_version = _jsonable_int(row[0] if row else 0)
    conn.execute(f"PRAGMA main.user_version={user_version}")


def _create_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    table_errors = _exec_script_rows(conn, _schema_rows(conn, "table"), label="table")
    _set_user_version(conn)
    return {"table_errors": table_errors}


def _create_indexes_triggers_views(conn: sqlite3.Connection) -> dict[str, Any]:
    index_errors = _exec_script_rows(conn, _schema_rows(conn, "index"), label="index")
    trigger_errors = _exec_script_rows(conn, _schema_rows(conn, "trigger"), label="trigger")
    view_errors = _exec_script_rows(conn, _schema_rows(conn, "view"), label="view")
    return {
        "index_errors": index_errors,
        "trigger_errors": trigger_errors,
        "view_errors": view_errors,
    }


def _copy_data(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    batch_size: int,
) -> dict[str, Any]:
    settings = plan.get("settings", {}) if isinstance(plan.get("settings"), Mapping) else {}
    tables = _table_names(conn)
    results: dict[str, Any] = {}
    for table in tables:
        started = time.monotonic()
        if table == "event_fact_history":
            result = _copy_event_fact_history(
                conn,
                cutoff=str(settings.get("facts_cutoff_event_date") or ""),
                batch_size=batch_size,
            )
        elif table == "event_fact_summary_cache":
            result = _copy_event_fact_summary_cache(
                conn,
                cutoff=str(settings.get("facts_cutoff_event_date") or ""),
            )
        elif table == "event_runs":
            result = _copy_latest_by_event_table(
                conn,
                table,
                cutoff=str(settings.get("runs_cutoff_event_date") or ""),
            )
        elif table in {"model_snapshots", "market_snapshots"}:
            result = _copy_latest_by_event_table(
                conn,
                table,
                cutoff=str(settings.get("snapshots_cutoff_event_date") or ""),
            )
        elif table == "auto_trade_jobs":
            result = _copy_auto_trade_jobs(
                conn,
                cutoff=str(settings.get("jobs_cutoff_created_date") or ""),
            )
        elif table == "event_market_ticks":
            result = _copy_event_market_ticks(
                conn,
                cutoff=str(settings.get("ticks_cutoff_created_date") or ""),
            )
        else:
            result = _copy_full_table(conn, table)
        result["elapsed_sec"] = round(time.monotonic() - started, 3)
        results[table] = result
    return results


def _validate_latest_facts(conn: sqlite3.Connection) -> dict[str, Any]:
    missing = _jsonable_int(
        conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM event_fact_latest l
            LEFT JOIN event_fact_history h ON h.id = l.fact_id
            WHERE h.id IS NULL
            """
        ).fetchone()["n"]
    )
    compacted_latest = 0
    sample_errors: list[str] = []
    for row in conn.execute(
        """
        SELECT l.fact_id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        """
    ):
        try:
            payload = json.loads(str(row["fact_json"] or "{}"))
            if isinstance(payload, Mapping) and payload.get("history_compacted") is True:
                compacted_latest += 1
        except Exception as exc:
            sample_errors.append(f"fact_id={row['fact_id']}:{exc}")
    return {
        "latest_missing_fact_rows": missing,
        "latest_compacted_rows": compacted_latest,
        "latest_json_parse_errors": sample_errors[:10],
        "ok": missing == 0 and compacted_latest == 0 and not sample_errors,
    }


def _count_compare(
    conn: sqlite3.Connection,
    tables: Iterable[str],
    *,
    copy_results: Mapping[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for table in tables:
        try:
            result = copy_results.get(table, {}) if isinstance(copy_results, Mapping) else {}
            source_rows = (
                _jsonable_int(result.get("source_rows_snapshot"))
                if isinstance(result, Mapping) and "source_rows_snapshot" in result
                else None
            )
            dest_rows = _count(conn, "main", table)
            out[table] = {
                "source_rows_snapshot": source_rows,
                "dest_rows": dest_rows,
                "match": source_rows == dest_rows if source_rows is not None else False,
            }
        except sqlite3.Error as exc:
            out[table] = {"error": str(exc), "match": False}
    return out


def _run_checks(conn: sqlite3.Connection, *, copy_results: Mapping[str, Any]) -> dict[str, Any]:
    quick = str(conn.execute("PRAGMA main.quick_check").fetchone()[0] or "")
    integrity = str(conn.execute("PRAGMA main.integrity_check").fetchone()[0] or "")
    latest = _validate_latest_facts(conn)
    full_copy_counts = _count_compare(
        conn,
        [
            "event_fact_latest",
            "auto_trade_decisions",
            "auto_orders",
            "edge_trade_fills",
            "trade_fill_attributions",
            "edge_trades",
            "market_tokens",
            "weather_market_discovery",
        ],
        copy_results=copy_results,
    )
    return {
        "quick_check": quick,
        "integrity_check": integrity,
        "latest_facts": latest,
        "full_copy_counts": full_copy_counts,
        "ok": quick == "ok"
        and integrity == "ok"
        and bool(latest.get("ok"))
        and all(item.get("match") for item in full_copy_counts.values()),
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_markdown(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    plan_summary = payload.get("plan_summary", {}) if isinstance(payload.get("plan_summary"), Mapping) else {}
    build = payload.get("build", {}) if isinstance(payload.get("build"), Mapping) else {}
    checks = payload.get("checks", {}) if isinstance(payload.get("checks"), Mapping) else {}
    copy_results = payload.get("copy_results", {}) if isinstance(payload.get("copy_results"), Mapping) else {}
    lines = [
        "# archive.db 小库候选构建报告",
        "",
        f"生成时间：{payload.get('generated_at_utc')}",
        f"源库：`{payload.get('source_db')}`",
        f"候选库：`{payload.get('output_db')}`",
        "",
        "## 结果",
        "",
        f"- 构建状态：`{build.get('status')}`",
        f"- 检查通过：`{checks.get('ok')}`",
        f"- 预计小库：{_fmt_bytes(_jsonable_int(plan_summary.get('estimated_rebuilt_db_bytes')))}",
        f"- 实际文件：{_fmt_bytes(_jsonable_int(build.get('output_db_size_bytes')))}",
        f"- 预计释放：{_fmt_bytes(_jsonable_int(plan_summary.get('estimated_release_bytes')))}",
        f"- 耗时：{build.get('elapsed_sec')} 秒",
        "",
        "## 候选表复制结果",
        "",
        "| 表 | 行数 | 策略 | 耗时 |",
        "| --- | ---: | --- | ---: |",
    ]
    for table in sorted(CANDIDATE_TABLES):
        result = copy_results.get(table)
        if not isinstance(result, Mapping):
            continue
        lines.append(
            "| `{}` | {} | {} | {} |".format(
                table,
                result.get("rows_copied", ""),
                result.get("policy", ""),
                result.get("elapsed_sec", ""),
            )
        )
    lines.extend(
        [
            "",
            "## 完整性检查",
            "",
            f"- quick_check：`{checks.get('quick_check')}`",
            f"- integrity_check：`{checks.get('integrity_check')}`",
            f"- latest fact 检查：`{(checks.get('latest_facts') or {}).get('ok') if isinstance(checks.get('latest_facts'), Mapping) else None}`",
            "",
            "该脚本未替换现库。切换仍需单独确认。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _remove_output_files(output: Path) -> None:
    for path in (output, Path(str(output) + "-wal"), Path(str(output) + "-shm")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _date_cutoff(as_of: date, keep_days: int) -> str:
    return (as_of - timedelta(days=max(0, int(keep_days)))).isoformat()


def _parse_as_of_date(raw: str) -> date:
    text = str(raw or "").strip()
    if not text:
        return datetime.now(ZoneInfo("America/New_York")).date()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"invalid_date={text} expected=YYYY-MM-DD") from exc


def _fast_plan_from_build_args(args: argparse.Namespace) -> dict[str, Any]:
    as_of = _parse_as_of_date(args.as_of_date)
    settings = {
        "facts_cutoff_event_date": _date_cutoff(as_of, args.keep_facts_days),
        "runs_cutoff_event_date": _date_cutoff(as_of, args.keep_runs_days),
        "snapshots_cutoff_event_date": _date_cutoff(as_of, args.keep_snapshots_days),
        "jobs_cutoff_created_date": _date_cutoff(as_of, args.keep_jobs_days),
        "ticks_cutoff_created_date": _date_cutoff(as_of, args.keep_ticks_days),
    }
    return {
        "summary": {
            "estimated_rebuilt_db_bytes": 0,
            "estimated_release_bytes": 0,
            "fast_plan": True,
        },
        "settings": settings,
        "notes": [
            "fast_plan_skipped_slow_dbstat_estimate",
            "candidate checks remain authoritative before cutover",
        ],
    }


def _plan_args_from_build_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        db=str(Path(args.source_db).expanduser().resolve()),
        as_of_date=args.as_of_date,
        keep_facts_days=args.keep_facts_days,
        keep_runs_days=args.keep_runs_days,
        keep_snapshots_days=args.keep_snapshots_days,
        keep_jobs_days=args.keep_jobs_days,
        keep_ticks_days=args.keep_ticks_days,
        fact_sample_limit=args.fact_sample_limit,
        fact_compact_ratio=args.fact_compact_ratio,
    )


def build_candidate(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source_db).expanduser().resolve()
    output = Path(args.output).expanduser().resolve() if args.output else (
        DEFAULT_OUTPUT_DIR / f"archive.compact_{_utc_stamp()}.db"
    )
    if not source.exists():
        raise SystemExit(f"source_db_missing path={source}")
    if source == output:
        raise SystemExit("refusing_to_overwrite_source_db")
    if output.exists() and not args.force:
        raise SystemExit(f"output_exists path={output} use --force to replace candidate file")
    if not args.force:
        for sidecar in (Path(str(output) + "-wal"), Path(str(output) + "-shm")):
            if sidecar.exists():
                raise SystemExit(f"output_sidecar_exists path={sidecar} use --force to replace candidate files")
    output.parent.mkdir(parents=True, exist_ok=True)

    plan = _fast_plan_from_build_args(args) if args.fast_plan else dry_run.build_plan(_plan_args_from_build_args(args))
    estimated = _jsonable_int(plan.get("summary", {}).get("estimated_rebuilt_db_bytes"))
    free = shutil.disk_usage(output.parent).free
    if estimated > 0 and free < estimated + 1024**3:
        raise SystemExit(
            f"insufficient_free_space free={_fmt_bytes(free)} estimated={_fmt_bytes(estimated)}"
        )

    _remove_output_files(output)
    started = time.monotonic()
    copy_results: dict[str, Any] = {}
    schema_result: dict[str, Any] = {}
    index_result: dict[str, Any] = {}
    checks: dict[str, Any] = {}
    status = "started"
    try:
        with _connect_dest(output) as conn:
            _attach_source(conn, source)
            schema_result = _create_schema(conn)
            if schema_result.get("table_errors"):
                raise RuntimeError(f"schema_table_errors={schema_result['table_errors'][:3]}")
            conn.commit()
            conn.execute("BEGIN")
            copy_results = _copy_data(conn, plan=plan, batch_size=max(1, int(args.batch_size)))
            conn.commit()
            index_result = _create_indexes_triggers_views(conn)
            if index_result.get("index_errors") or index_result.get("trigger_errors") or index_result.get("view_errors"):
                raise RuntimeError(f"schema_post_errors={index_result}")
            conn.commit()
            conn.execute("PRAGMA main.journal_mode=WAL")
            conn.execute("PRAGMA main.synchronous=NORMAL")
            checks = _run_checks(conn, copy_results=copy_results)
            status = "ready" if checks.get("ok") else "checks_failed"
    except Exception:
        status = "failed"
        if not args.keep_failed_output:
            _remove_output_files(output)
        raise
    finally:
        elapsed = round(time.monotonic() - started, 3)

    output_size = output.stat().st_size if output.exists() else 0
    payload = {
        "ok": status == "ready",
        "generated_at_utc": _now_utc(),
        "source_db": str(source),
        "output_db": str(output),
        "candidate_copy_boundary": build_archive_rebuild_candidate_copy_boundary(
            source_db=source,
            output_db=output,
        ),
        "plan_summary": plan.get("summary", {}),
        "settings": plan.get("settings", {}),
        "build": {
            "status": status,
            "elapsed_sec": elapsed,
            "output_db_size_bytes": output_size,
            "output_db_size_human": _fmt_bytes(output_size),
            "source_db_size_bytes": source.stat().st_size,
            "source_db_size_human": _fmt_bytes(source.stat().st_size),
        },
        "schema": {**schema_result, **index_result},
        "copy_results": copy_results,
        "checks": checks,
        "notes": [
            "Source archive.db was attached read-only.",
            "The output DB is a candidate file only; no source replacement was performed.",
            "event_fact_latest rows are copied full and must point to non-compacted fact rows.",
        ],
    }
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=str(DEFAULT_DB), help="source archive.db path")
    parser.add_argument("--output", default="", help="candidate DB output path")
    parser.add_argument("--as-of-date", default="", help="ET date for retention math, YYYY-MM-DD")
    parser.add_argument("--keep-facts-days", type=int, default=2)
    parser.add_argument("--keep-runs-days", type=int, default=2)
    parser.add_argument("--keep-snapshots-days", type=int, default=2)
    parser.add_argument("--keep-jobs-days", type=int, default=2)
    parser.add_argument("--keep-ticks-days", type=int, default=2)
    parser.add_argument("--fact-sample-limit", type=int, default=8)
    parser.add_argument("--fact-compact-ratio", type=float, default=0.05)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--fast-plan",
        action="store_true",
        help="Skip the slow read-only dbstat/payload estimate and build from retention settings directly.",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing candidate output file")
    parser.add_argument("--keep-failed-output", action="store_true", help="leave failed candidate files for inspection")
    parser.add_argument("--report-json", default="", help="build report JSON path")
    parser.add_argument("--report-md", default="", help="build report Markdown path")
    parser.add_argument("--print-json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_candidate(args)
    stamp = _utc_stamp()
    report_json = Path(args.report_json) if args.report_json else DEFAULT_REPORT_DIR / f"archive_db_rebuild_build_{stamp}.json"
    report_md = Path(args.report_md) if args.report_md else DEFAULT_REPORT_DIR / f"archive_db_rebuild_build_{stamp}.md"
    payload["report_json"] = str(report_json)
    payload["report_md"] = str(report_md)
    _write_json(report_json, payload)
    _write_markdown(report_md, payload)
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                {
                    "ok": payload["ok"],
                    "output_db": payload["output_db"],
                    "output_db_size": payload["build"]["output_db_size_human"],
                    "report_json": str(report_json),
                    "report_md": str(report_md),
                    "checks_ok": payload["checks"].get("ok"),
                    "elapsed_sec": payload["build"]["elapsed_sec"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
