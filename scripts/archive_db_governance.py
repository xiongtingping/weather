#!/usr/bin/env python3
"""Govern retention and compaction for data/archive.db.

Default mode is a dry run. Use --apply only after reviewing the printed
candidate counts.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Iterable, Optional
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
ET = ZoneInfo("America/New_York")
TICK_ROLLUP_TABLE = "event_market_tick_rollups"


@dataclass(frozen=True)
class RetentionRule:
    table: str
    days_attr: str
    date_column: str
    extra_where: str = ""
    preserve_latest_by: tuple[str, ...] = ()


RULES = (
    RetentionRule(
        "event_market_ticks",
        "keep_ticks_days",
        "run_date",
        preserve_latest_by=("slug", "event_date", "outcome", "side"),
    ),
    RetentionRule(
        "auto_trade_decisions",
        "keep_decisions_days",
        "run_date",
        extra_where="AND COALESCE(will_order, 0) = 0",
    ),
    RetentionRule(
        "event_fact_history",
        "keep_facts_days",
        "run_date",
        preserve_latest_by=("slug", "event_date"),
    ),
    RetentionRule(
        "event_runs",
        "keep_event_runs_days",
        "run_date",
        preserve_latest_by=("slug", "event_date"),
    ),
    RetentionRule("market_snapshots", "keep_snapshots_days", "run_date"),
    RetentionRule("model_snapshots", "keep_snapshots_days", "run_date"),
    RetentionRule("station_observations", "keep_observations_days", "run_date"),
)


TICK_RULE = RULES[0]
CHENGDU_SLUG_LIKE = "highest-temperature-in-chengdu-%"
CHENGDU_SLUG_PREFIX = "highest-temperature-in-chengdu-"
CHENGDU_SLUG_PREFIX_UPPER = "highest-temperature-in-chengdu."


def _connect(db: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, index: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _count(conn: sqlite3.Connection, table: str, where: str = "", params: Iterable[object] = ()) -> int:
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(row["n"] or 0)


def _one(conn: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> Optional[object]:
    row = conn.execute(sql, tuple(params)).fetchone()
    if row is None:
        return None
    return row[0]


def _fmt_bytes(value: int) -> str:
    num = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0 or unit == "TiB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{value}B"


def _cutoff_date(as_of_et: date, keep_days: int) -> str:
    return (as_of_et - timedelta(days=max(0, int(keep_days)))).isoformat()


def _latest_preserve_clause(rule: RetentionRule) -> str:
    if not rule.preserve_latest_by:
        return ""
    joins = " AND ".join(
        f"(newer.{col} = {rule.table}.{col} OR (newer.{col} IS NULL AND {rule.table}.{col} IS NULL))"
        for col in rule.preserve_latest_by
    )
    return (
        "AND EXISTS ("
        f"SELECT 1 FROM {rule.table} AS newer "
        f"WHERE {joins} AND newer.id > {rule.table}.id LIMIT 1"
        ")"
    )


def _prepare_tick_latest_keep(conn: sqlite3.Connection) -> int:
    """Materialize raw tick rows that must survive retention pruning.

    The generic preserve_latest_by clause uses a correlated EXISTS, which is
    too expensive for event_market_ticks once the table grows into tens of
    millions of rows. A temp table turns the keep-set into a single indexed
    lookup per candidate row.
    """

    conn.execute("DROP TABLE IF EXISTS temp.tick_latest_keep")
    conn.execute("CREATE TEMP TABLE tick_latest_keep (id INTEGER PRIMARY KEY)")
    conn.execute(
        """
        INSERT INTO tick_latest_keep(id)
        SELECT MAX(id)
        FROM event_market_ticks
        GROUP BY slug, event_date, outcome, side
        """
    )
    row = conn.execute("SELECT COUNT(*) AS n FROM tick_latest_keep").fetchone()
    return int(row["n"] or 0)


def _tick_rule_where(rule: RetentionRule, cutoff: str) -> tuple[str, tuple[object, ...]]:
    # Keep governance deterministic by clock time rather than insertion day.
    if rule.table == "event_market_ticks":
        cutoff_dt = f"{cutoff}T00:00:00+00:00"
        parts = [f"created_at < ?"]
        params = (cutoff_dt,)
    else:
        parts = [f"{rule.date_column} < ?"]
        params = (cutoff,)
    if rule.extra_where:
        parts.append(rule.extra_where)
    if rule.preserve_latest_by:
        parts.append(
            "AND NOT EXISTS ("
            "SELECT 1 FROM tick_latest_keep AS keep "
            f"WHERE keep.id = {rule.table}.id"
            ")"
        )
    return " ".join(parts), params


def _parse_csv_dates(raw: str) -> list[str]:
    dates: list[str] = []
    for item in (raw or "").split(","):
        value = item.strip()
        if not value:
            continue
        try:
            dates.append(datetime.strptime(value, "%Y-%m-%d").date().isoformat())
        except ValueError as exc:
            raise SystemExit(f"invalid_date={value} expected=YYYY-MM-DD") from exc
    return dates


def _chengdu_active_event_dates(args: argparse.Namespace, as_of_et: date) -> list[str]:
    explicit = _parse_csv_dates(getattr(args, "chengdu_keep_event_dates", ""))
    if explicit:
        return sorted(set(explicit))
    days_ahead = max(0, int(getattr(args, "chengdu_active_days_ahead", 2)))
    return [(as_of_et + timedelta(days=i)).isoformat() for i in range(days_ahead + 1)]


def _now_utc_for_args(args: argparse.Namespace) -> datetime:
    raw = str(getattr(args, "now_utc", "") or "").strip()
    if raw:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            return datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise SystemExit(f"invalid_now_utc={raw} expected=ISO-8601") from exc
    return datetime.now(ZoneInfo("UTC"))


def _chengdu_slug_predicate(table: str = "event_market_ticks") -> str:
    # Prefix range keeps SQLite on idx_event_market_ticks_slug_*; lower()/LIKE
    # turns this into a full index scan on the live 40GiB+ archive.
    return (
        f"{table}.slug >= '{CHENGDU_SLUG_PREFIX}' "
        f"AND {table}.slug < '{CHENGDU_SLUG_PREFIX_UPPER}'"
    )


def _prepare_chengdu_tick_raw_keep(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    as_of_et: date,
) -> dict[str, object]:
    """Materialize raw tick rows that should remain after Chengdu-only pruning."""

    active_dates = _chengdu_active_event_dates(args, as_of_et)
    recent_hours = max(0, int(getattr(args, "chengdu_recent_hours", 48)))
    now_utc = _now_utc_for_args(args)
    recent_cutoff = (now_utc - timedelta(hours=recent_hours)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    preserve_latest = bool(getattr(args, "chengdu_preserve_latest_per_key", True))

    conn.execute("DROP TABLE IF EXISTS temp.chengdu_tick_raw_keep")
    conn.execute(
        """
        CREATE TEMP TABLE chengdu_tick_raw_keep (
            id INTEGER PRIMARY KEY,
            reason TEXT NOT NULL
        )
        """
    )

    chengdu = _chengdu_slug_predicate()
    if active_dates:
        placeholders = ",".join("?" for _ in active_dates)
        conn.execute(
            f"""
            INSERT OR IGNORE INTO chengdu_tick_raw_keep(id, reason)
            SELECT id, 'chengdu_active_event'
            FROM event_market_ticks
            WHERE {chengdu}
              AND event_date IN ({placeholders})
            """,
            tuple(active_dates),
        )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO chengdu_tick_raw_keep(id, reason)
        SELECT id, 'chengdu_recent_window'
        FROM event_market_ticks
        WHERE {chengdu}
          AND created_at >= ?
        """,
        (recent_cutoff,),
    )
    if preserve_latest:
        conn.execute(
            f"""
            INSERT OR IGNORE INTO chengdu_tick_raw_keep(id, reason)
            SELECT MAX(id), 'chengdu_latest_per_key'
            FROM event_market_ticks
            WHERE {chengdu}
            GROUP BY slug, event_date, outcome, side
            """,
        )

    keep_total = _count(conn, "chengdu_tick_raw_keep")
    chengdu_total = _count(conn, "event_market_ticks", chengdu)
    total = _count(conn, "event_market_ticks")
    active_count = (
        _count(
            conn,
            "event_market_ticks",
            f"{chengdu} AND event_date IN ({','.join('?' for _ in active_dates)})",
            tuple(active_dates),
        )
        if active_dates
        else 0
    )
    recent_count = _count(
        conn,
        "event_market_ticks",
        f"{chengdu} AND created_at >= ?",
        (recent_cutoff,),
    )
    latest_count = (
        int(
            _one(
                conn,
                f"""
                SELECT COUNT(*)
                FROM (
                    SELECT MAX(id)
                    FROM event_market_ticks
                    WHERE {chengdu}
                    GROUP BY slug, event_date, outcome, side
                )
                """,
            )
            or 0
        )
        if preserve_latest
        else 0
    )
    return {
        "active_dates": active_dates,
        "recent_hours": recent_hours,
        "recent_cutoff": recent_cutoff,
        "preserve_latest": preserve_latest,
        "total": total,
        "chengdu_total": chengdu_total,
        "non_chengdu_total": total - chengdu_total,
        "active_count": active_count,
        "recent_count": recent_count,
        "latest_count": latest_count,
        "keep_total": keep_total,
    }


def _chengdu_tick_delete_where() -> tuple[str, tuple[object, ...]]:
    return (
        "NOT EXISTS ("
        "SELECT 1 FROM chengdu_tick_raw_keep AS keep "
        "WHERE keep.id = event_market_ticks.id"
        ")",
        (),
    )


def _rule_where(rule: RetentionRule, cutoff: str) -> tuple[str, tuple[object, ...]]:
    parts = [f"{rule.date_column} < ?"]
    if rule.extra_where:
        parts.append(rule.extra_where)
    latest = _latest_preserve_clause(rule)
    if latest:
        parts.append(latest)
    return " ".join(parts), (cutoff,)


def _delete_batch(
    conn: sqlite3.Connection,
    table: str,
    where: str,
    params: tuple[object, ...],
    batch_size: int,
    *,
    indexed_by: str = "",
) -> int:
    table_ref = f"{table} INDEXED BY {indexed_by}" if indexed_by else table
    sql = f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table_ref} WHERE {where} LIMIT ?)"
    cur = conn.execute(sql, (*params, int(batch_size)))
    return int(cur.rowcount if cur.rowcount is not None else 0)


def _ensure_tick_rollup_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TICK_ROLLUP_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grain TEXT NOT NULL,
            bucket_start TEXT NOT NULL,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            outcome TEXT,
            side TEXT,
            tick_count INTEGER NOT NULL,
            first_tick_at TEXT,
            last_tick_at TEXT,
            first_best_bid REAL,
            last_best_bid REAL,
            min_best_bid REAL,
            max_best_bid REAL,
            avg_best_bid REAL,
            first_best_ask REAL,
            last_best_ask REAL,
            min_best_ask REAL,
            max_best_ask REAL,
            avg_best_ask REAL,
            first_mid REAL,
            last_mid REAL,
            min_mid REAL,
            max_mid REAL,
            avg_mid REAL,
            min_spread REAL,
            max_spread REAL,
            avg_spread REAL,
            max_bid_depth_topN REAL,
            avg_bid_depth_topN REAL,
            max_ask_depth_topN REAL,
            avg_ask_depth_topN REAL,
            archived_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_{TICK_ROLLUP_TABLE}_key
        ON {TICK_ROLLUP_TABLE}(grain, bucket_start, slug, event_date, outcome, side)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{TICK_ROLLUP_TABLE}_run_slug
        ON {TICK_ROLLUP_TABLE}(run_date, slug, event_date)
        """
    )


def _tick_rollup_sql(where: str, *, indexed_by: str = "") -> str:
    table_ref = (
        f"event_market_ticks INDEXED BY {indexed_by}"
        if indexed_by
        else "event_market_ticks"
    )
    return f"""
        WITH candidate AS (
            SELECT
                id,
                run_date,
                event_date,
                slug,
                outcome,
                side,
                replace(substr(created_at, 1, 13), ' ', 'T') || ':00:00+00:00' AS bucket_start,
                best_bid,
                best_ask,
                mid,
                spread,
                bid_depth_topN,
                ask_depth_topN,
                created_at
            FROM {table_ref}
            WHERE {where}
        ),
        ranked AS (
            SELECT
                *,
                row_number() OVER (
                    PARTITION BY bucket_start, slug, event_date, outcome, side
                    ORDER BY created_at ASC, id ASC
                ) AS rn_first,
                row_number() OVER (
                    PARTITION BY bucket_start, slug, event_date, outcome, side
                    ORDER BY created_at DESC, id DESC
                ) AS rn_last
            FROM candidate
        )
        INSERT OR REPLACE INTO {TICK_ROLLUP_TABLE} (
            grain,
            bucket_start,
            run_date,
            event_date,
            slug,
            outcome,
            side,
            tick_count,
            first_tick_at,
            last_tick_at,
            first_best_bid,
            last_best_bid,
            min_best_bid,
            max_best_bid,
            avg_best_bid,
            first_best_ask,
            last_best_ask,
            min_best_ask,
            max_best_ask,
            avg_best_ask,
            first_mid,
            last_mid,
            min_mid,
            max_mid,
            avg_mid,
            min_spread,
            max_spread,
            avg_spread,
            max_bid_depth_topN,
            avg_bid_depth_topN,
            max_ask_depth_topN,
            avg_ask_depth_topN,
            archived_at
        )
        SELECT
            'hour',
            bucket_start,
            MIN(run_date),
            event_date,
            slug,
            outcome,
            side,
            COUNT(*),
            MIN(created_at),
            MAX(created_at),
            MAX(CASE WHEN rn_first = 1 THEN best_bid END),
            MAX(CASE WHEN rn_last = 1 THEN best_bid END),
            MIN(best_bid),
            MAX(best_bid),
            AVG(best_bid),
            MAX(CASE WHEN rn_first = 1 THEN best_ask END),
            MAX(CASE WHEN rn_last = 1 THEN best_ask END),
            MIN(best_ask),
            MAX(best_ask),
            AVG(best_ask),
            MAX(CASE WHEN rn_first = 1 THEN mid END),
            MAX(CASE WHEN rn_last = 1 THEN mid END),
            MIN(mid),
            MAX(mid),
            AVG(mid),
            MIN(spread),
            MAX(spread),
            AVG(spread),
            MAX(bid_depth_topN),
            AVG(bid_depth_topN),
            MAX(ask_depth_topN),
            AVG(ask_depth_topN),
            strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        FROM ranked
        GROUP BY bucket_start, slug, event_date, outcome, side
    """


def _count_tick_rollup_groups(conn: sqlite3.Connection, where: str, params: tuple[object, ...]) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM (
            SELECT 1
            FROM event_market_ticks
            WHERE {where}
            GROUP BY
                replace(substr(created_at, 1, 13), ' ', 'T') || ':00:00+00:00',
                slug,
                event_date,
                outcome,
                side
        )
        """,
        params,
    ).fetchone()
    return int(row["n"] or 0)


def _tick_run_dates(conn: sqlite3.Connection, where: str, params: tuple[object, ...]) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT DISTINCT run_date
        FROM event_market_ticks
        WHERE {where}
        ORDER BY run_date
        """,
        params,
    ).fetchall()
    dates: list[str] = []
    for (run_date,) in rows:
        if run_date:
            dates.append(str(run_date))
    return dates


def _tick_hour_prefixes(conn: sqlite3.Connection, where: str, params: tuple[object, ...]) -> list[str]:
    row = conn.execute(
        f"""
        SELECT
            MIN(substr(created_at, 1, 13)) AS min_hour,
            MAX(substr(created_at, 1, 13)) AS max_hour
        FROM event_market_ticks
        WHERE {where}
        """,
        params,
    ).fetchone()
    min_hour = str(row["min_hour"] or "")
    max_hour = str(row["max_hour"] or "")
    if not min_hour or not max_hour:
        return []
    try:
        cur = datetime.strptime(min_hour, "%Y-%m-%dT%H")
        end = datetime.strptime(max_hour, "%Y-%m-%dT%H")
    except ValueError:
        rows = conn.execute(
            f"""
            SELECT DISTINCT substr(created_at, 1, 13) AS hour_prefix
            FROM event_market_ticks
            WHERE {where}
            ORDER BY hour_prefix
            """,
            params,
        ).fetchall()
        return [str(row["hour_prefix"]) for row in rows if row["hour_prefix"]]
    prefixes: list[str] = []
    while cur <= end:
        prefixes.append(cur.strftime("%Y-%m-%dT%H"))
        cur += timedelta(hours=1)
    return prefixes


def _run_date_hour_prefixes(run_date: str) -> list[str]:
    try:
        cur = datetime.strptime(run_date, "%Y-%m-%d")
    except ValueError:
        return []
    # run_date is ET-facing while created_at is UTC. Two UTC days are a small,
    # bounded superset and avoid expensive DISTINCT/MIN/MAX scans on raw ticks.
    return [(cur + timedelta(hours=i)).strftime("%Y-%m-%dT%H") for i in range(48)]


def _archive_and_delete_tick_run_date(
    conn: sqlite3.Connection,
    run_date: str,
    where: str,
    params: tuple[object, ...],
    batch_size: int,
    max_delete_rows: int = 0,
) -> tuple[int, int]:
    per_day_where = f"{where} AND run_date = ?"
    per_day_params = (*params, run_date)
    rollup_groups = -1
    deleted_total = 0
    created_at_index = (
        "idx_event_market_ticks_created_at"
        if _index_exists(conn, "idx_event_market_ticks_created_at")
        else ""
    )
    hour_prefixes = _run_date_hour_prefixes(run_date)
    if not hour_prefixes:
        hour_prefixes = _tick_hour_prefixes(conn, per_day_where, per_day_params)
    if not hour_prefixes:
        return rollup_groups, deleted_total
    for hour_prefix in hour_prefixes:
        if max_delete_rows > 0 and deleted_total >= max_delete_rows:
            break
        try:
            hour_start_dt = datetime.strptime(hour_prefix, "%Y-%m-%dT%H")
        except ValueError:
            continue
        hour_end_dt = hour_start_dt + timedelta(hours=1)
        hour_start = hour_start_dt.strftime("%Y-%m-%dT%H:00:00+00:00")
        hour_end = hour_end_dt.strftime("%Y-%m-%dT%H:00:00+00:00")
        hour_where = f"{per_day_where} AND created_at >= ? AND created_at < ?"
        hour_params = (*per_day_params, hour_start, hour_end)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_tick_rollup_table(conn)
            conn.execute(
                _tick_rollup_sql(hour_where, indexed_by=created_at_index),
                hour_params,
            )
            hour_deleted_total = 0
            while True:
                deleted = _delete_batch(
                    conn,
                    "event_market_ticks",
                    hour_where,
                    hour_params,
                    batch_size,
                    indexed_by=created_at_index,
                )
                hour_deleted_total += deleted
                deleted_total += deleted
                if deleted <= 0:
                    break
            conn.commit()
            if hour_deleted_total > 0:
                try:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error:
                    pass
                print(
                    f"event_market_ticks: run_date={run_date} "
                    f"hour={hour_prefix} deleted={hour_deleted_total} "
                    f"day_deleted={deleted_total}"
                )
        except Exception:
            conn.rollback()
            raise
    return rollup_groups, deleted_total


def _print_db_summary(conn: sqlite3.Connection, db: Path) -> None:
    page_size = int(_one(conn, "PRAGMA page_size") or 0)
    page_count = int(_one(conn, "PRAGMA page_count") or 0)
    freelist_count = int(_one(conn, "PRAGMA freelist_count") or 0)
    wal = db.with_name(db.name + "-wal")
    print("== archive.db summary ==")
    print(f"db_path={db}")
    print(f"db_file={_fmt_bytes(db.stat().st_size)} wal={_fmt_bytes(wal.stat().st_size) if wal.exists() else '0B'}")
    print(
        "sqlite_pages="
        f"{page_count} page_size={page_size} free_pages={freelist_count} "
        f"free_bytes={_fmt_bytes(page_size * freelist_count)}"
    )
    print(f"disk_free={_fmt_bytes(shutil.disk_usage(db.parent).free)}")


def _print_table_summary(conn: sqlite3.Connection, table: str, *, exact_count: bool) -> None:
    if not _table_exists(conn, table):
        return
    cols = _columns(conn, table)
    total = str(_count(conn, table)) if exact_count else "skipped_fast_mode"
    if "run_date" in cols:
        min_v = _one(conn, f"SELECT MIN(run_date) FROM {table}")
        max_v = _one(conn, f"SELECT MAX(run_date) FROM {table}")
        print(f"{table}: rows={total} run_date={min_v}..{max_v}")
    elif "created_at" in cols:
        min_v = _one(conn, f"SELECT MIN(created_at) FROM {table}")
        max_v = _one(conn, f"SELECT MAX(created_at) FROM {table}")
        print(f"{table}: rows={total} created_at={min_v}..{max_v}")
    else:
        print(f"{table}: rows={total}")


def _run_retention(conn: sqlite3.Connection, args: argparse.Namespace, as_of_et: date) -> None:
    print("== retention candidates ==")
    for rule in RULES:
        if not _table_exists(conn, rule.table):
            print(f"{rule.table}: skipped_missing_table")
            continue
        cols = _columns(conn, rule.table)
        if rule.date_column not in cols:
            print(f"{rule.table}: skipped_missing_{rule.date_column}")
            continue
        keep_days = int(getattr(args, rule.days_attr))
        cutoff = _cutoff_date(as_of_et, keep_days)
        cutoff_label = (
            f"{cutoff}T00:00:00+00:00" if rule.table == "event_market_ticks" else cutoff
        )
        if rule.table == "event_market_ticks":
            keep_ids = _prepare_tick_latest_keep(conn) if rule.preserve_latest_by else 0
            if rule.preserve_latest_by:
                print(f"event_market_ticks: preserve_latest_keep_ids={keep_ids}")
            where, params = _tick_rule_where(rule, cutoff)
        else:
            where, params = _rule_where(rule, cutoff)
        candidates = (
            -1
            if rule.table == "event_market_ticks" and args.apply
            else _count(conn, rule.table, where, params)
        )
        candidates_text = str(candidates) if candidates >= 0 else "not_counted_apply_mode"
        print(
            f"{rule.table}: keep_days={keep_days} cutoff_run_date_lt={cutoff_label} "
            f"candidates={candidates_text}"
        )
        if rule.table == "event_market_ticks":
            if args.apply:
                print(f"{TICK_ROLLUP_TABLE}: projected_hourly_groups=skipped_apply_mode")
            else:
                rollup_groups = _count_tick_rollup_groups(conn, where, params) if candidates > 0 else 0
                print(f"{TICK_ROLLUP_TABLE}: projected_hourly_groups={rollup_groups}")
            if args.apply:
                _ensure_tick_rollup_table(conn)
                conn.commit()
                if candidates != 0:
                    deleted_total = 0
                    archived_groups = 0
                    max_tick_delete_rows = max(0, int(getattr(args, "max_tick_delete_rows", 0) or 0))
                    for run_date in _tick_run_dates(conn, where, params):
                        remaining_limit = max_tick_delete_rows - deleted_total if max_tick_delete_rows > 0 else 0
                        if max_tick_delete_rows > 0 and remaining_limit <= 0:
                            break
                        day_groups, day_deleted = _archive_and_delete_tick_run_date(
                            conn,
                            run_date,
                            where,
                            params,
                            args.batch_size,
                            remaining_limit,
                        )
                        if archived_groups >= 0 and day_groups >= 0:
                            archived_groups += day_groups
                        else:
                            archived_groups = -1
                        deleted_total += day_deleted
                        day_groups_text = str(day_groups) if day_groups >= 0 else "not_counted"
                        print(
                            f"event_market_ticks: run_date={run_date} "
                            f"archived_hourly_groups={day_groups_text} "
                            f"deleted={day_deleted} deleted_total={deleted_total}"
                        )
                        if max_tick_delete_rows > 0 and deleted_total >= max_tick_delete_rows:
                            print(f"event_market_ticks: stopped_at_max_tick_delete_rows={max_tick_delete_rows}")
                            break
                    archived_groups_text = (
                        str(archived_groups) if archived_groups >= 0 else "not_counted"
                    )
                    print(
                        f"{TICK_ROLLUP_TABLE}: archived_hourly_groups={archived_groups_text} "
                        f"raw_deleted={deleted_total}"
                    )
            continue
        if not args.apply or candidates <= 0:
            continue
        deleted_total = 0
        while True:
            try:
                deleted = _delete_batch(
                    conn, rule.table, where, params, args.batch_size
                )
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                # The resident writer holds this database continuously, so a
                # contended batch is the normal case for a pass that runs while
                # the service is up -- not a failure.  Everything committed so
                # far stands, this table resumes on the next run, and the
                # remaining tables still get their turn.  Aborting instead left
                # the later rules unrun every time the writer was busy.
                conn.rollback()
                print(
                    f"{rule.table}: deferred_locked deleted_total={deleted_total}"
                )
                break
            conn.commit()
            deleted_total += deleted
            if deleted <= 0:
                break
            print(f"{rule.table}: deleted_batch={deleted} deleted_total={deleted_total}")


def _print_chengdu_delete_breakdown(
    conn: sqlite3.Connection,
    where: str,
    params: tuple[object, ...],
) -> None:
    rows = conn.execute(
        f"""
        SELECT
            event_date,
            CASE
                WHEN {_chengdu_slug_predicate()} THEN 'chengdu'
                ELSE 'non_chengdu'
            END AS scope,
            COUNT(*) AS n,
            COUNT(DISTINCT slug) AS slugs,
            MIN(created_at) AS first_created_at,
            MAX(created_at) AS last_created_at
        FROM event_market_ticks
        WHERE {where}
        GROUP BY event_date, scope
        ORDER BY event_date, scope
        """,
        params,
    ).fetchall()
    if not rows:
        print("event_market_ticks_chengdu_retention_delete_breakdown=none")
        return
    print("== chengdu tick retention delete breakdown ==")
    for row in rows:
        print(
            "delete_candidate "
            f"event_date={row['event_date']} scope={row['scope']} "
            f"rows={int(row['n'] or 0)} slugs={int(row['slugs'] or 0)} "
            f"created_at={row['first_created_at']}..{row['last_created_at']}"
        )


def _run_chengdu_tick_retention(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    as_of_et: date,
) -> None:
    if not _table_exists(conn, "event_market_ticks"):
        print("event_market_ticks: skipped_missing_table")
        return
    meta = _prepare_chengdu_tick_raw_keep(conn, args, as_of_et)
    where, params = _chengdu_tick_delete_where()
    candidates = -1 if args.apply else _count(conn, "event_market_ticks", where, params)
    non_chengdu_candidates = (
        -1
        if args.apply
        else _count(
            conn,
            "event_market_ticks",
            f"{where} AND NOT ({_chengdu_slug_predicate()})",
            params,
        )
    )
    old_chengdu_candidates = (
        -1
        if args.apply
        else _count(
            conn,
            "event_market_ticks",
            f"{where} AND {_chengdu_slug_predicate()}",
            params,
        )
    )
    keep_ratio = (
        (float(meta["keep_total"]) / float(meta["total"])) if int(meta["total"] or 0) > 0 else 0.0
    )
    print("== chengdu-only event_market_ticks retention ==")
    keep_policy = (
        "chengdu_active_event_dates+chengdu_recent_window+chengdu_latest_per_key"
        if meta["preserve_latest"]
        else "chengdu_active_event_dates+chengdu_recent_window"
    )
    print(f"raw_keep_policy={keep_policy}")
    print(
        f"active_event_dates={','.join(str(v) for v in meta['active_dates'])} "
        f"recent_hours={meta['recent_hours']} recent_cutoff_utc={meta['recent_cutoff']}"
    )
    print(
        "raw_rows "
        f"total={meta['total']} chengdu={meta['chengdu_total']} "
        f"non_chengdu={meta['non_chengdu_total']} keep_unique={meta['keep_total']} "
        f"keep_ratio={keep_ratio:.4f}"
    )
    print(
        "raw_keep_components "
        f"active_event_rows={meta['active_count']} "
        f"recent_rows={meta['recent_count']} "
        f"latest_per_key_rows={meta['latest_count']}"
    )
    candidates_text = str(candidates) if candidates >= 0 else "not_counted_apply_mode"
    non_chengdu_text = (
        str(non_chengdu_candidates)
        if non_chengdu_candidates >= 0
        else "not_counted_apply_mode"
    )
    old_chengdu_text = (
        str(old_chengdu_candidates)
        if old_chengdu_candidates >= 0
        else "not_counted_apply_mode"
    )
    print(
        "raw_delete_candidates "
        f"total={candidates_text} non_chengdu={non_chengdu_text} "
        f"old_chengdu={old_chengdu_text}"
    )
    estimate_groups = bool(getattr(args, "estimate_tick_rollup_groups", False))
    if args.apply:
        print(f"{TICK_ROLLUP_TABLE}: projected_hourly_groups=skipped_apply_mode")
    elif not estimate_groups:
        print(f"{TICK_ROLLUP_TABLE}: projected_hourly_groups=skipped_use_--estimate-tick-rollup-groups")
        _print_chengdu_delete_breakdown(conn, where, params)
        db_path = Path(getattr(args, "db", DEFAULT_DB)).resolve()
        db_size = db_path.stat().st_size if db_path.exists() else 0
        estimated_reclaim = (
            int(db_size * (float(candidates) / float(meta["total"])))
            if int(meta["total"] or 0) > 0
            else 0
        )
        print(
            "vacuum_note="
            "delete_creates_freelist_only_compact_requires_--compact "
            f"rough_reclaim_upper_bound={_fmt_bytes(estimated_reclaim)}"
        )
        return
    else:
        rollup_groups = _count_tick_rollup_groups(conn, where, params) if candidates > 0 else 0
        print(f"{TICK_ROLLUP_TABLE}: projected_hourly_groups={rollup_groups}")
        _print_chengdu_delete_breakdown(conn, where, params)
        db_path = Path(getattr(args, "db", DEFAULT_DB)).resolve()
        db_size = db_path.stat().st_size if db_path.exists() else 0
        estimated_reclaim = (
            int(db_size * (float(candidates) / float(meta["total"])))
            if int(meta["total"] or 0) > 0
            else 0
        )
        print(
            "vacuum_note="
            "delete_creates_freelist_only_compact_requires_--compact "
            f"rough_reclaim_upper_bound={_fmt_bytes(estimated_reclaim)}"
        )
        return

    _ensure_tick_rollup_table(conn)
    conn.commit()
    deleted_total = 0
    max_tick_delete_rows = max(0, int(getattr(args, "max_tick_delete_rows", 0) or 0))
    for run_date in _tick_run_dates(conn, where, params):
        remaining_limit = max_tick_delete_rows - deleted_total if max_tick_delete_rows > 0 else 0
        if max_tick_delete_rows > 0 and remaining_limit <= 0:
            break
        day_groups, day_deleted = _archive_and_delete_tick_run_date(
            conn,
            run_date,
            where,
            params,
            args.batch_size,
            remaining_limit,
        )
        deleted_total += day_deleted
        day_groups_text = str(day_groups) if day_groups >= 0 else "not_counted"
        print(
            f"event_market_ticks: run_date={run_date} "
            f"archived_hourly_groups={day_groups_text} "
            f"deleted={day_deleted} deleted_total={deleted_total}"
        )
        if max_tick_delete_rows > 0 and deleted_total >= max_tick_delete_rows:
            print(f"event_market_ticks: stopped_at_max_tick_delete_rows={max_tick_delete_rows}")
            break
    print(f"{TICK_ROLLUP_TABLE}: raw_deleted={deleted_total}")


def _checkpoint(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    print(f"wal_checkpoint_truncate={tuple(row) if row is not None else 'ok'}")


#: Slack over the computed peak.  VACUUM is not the only writer on this
#: machine -- the resident's WAL, the render, and the NWP lanes all take
#: bytes while it runs -- and running the volume to zero costs far more
#: than refusing a compaction that could have just fit.
VACUUM_HEADROOM_BYTES = 2 * 1024 ** 3


def _vacuum_space(conn: sqlite3.Connection, db: Path) -> dict[str, int]:
    """What a plain VACUUM of this database will actually need on disk.

    Not a multiple of the file.  ``VACUUM`` builds the compacted database
    beside the original and only replaces it at the end, so the peak is
    ``current + compacted`` -- and ``compacted`` is knowable exactly, from
    the pages that are not on the freelist.

    The rule this replaces was ``file * 1.25``, which is below the peak for
    any database whose freelist is under 20% and was therefore not a
    safety margin at all but a coin flip.  Measured on this archive
    2026-09-11: 19.54 GiB on disk, 28.4% freelist, so the compacted copy is
    14.00 GiB and the peak is 33.54 GiB -- 1.72x, while the old guard
    would have admitted it at 24.42 GiB free and run the volume out
    somewhere past the halfway mark.  That admission became reachable for
    the first time the same day, when free space crossed 24.42 GiB.
    """

    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    db_size = db.stat().st_size
    compacted = max(0, page_count - freelist) * page_size
    return {
        "db_size": db_size,
        "compacted": compacted,
        "reclaims": max(0, db_size - compacted),
        "required": db_size + compacted + VACUUM_HEADROOM_BYTES,
        "free": shutil.disk_usage(db.parent).free,
    }


def _vacuum(conn: sqlite3.Connection, db: Path) -> None:
    space = _vacuum_space(conn, db)
    print(
        "vacuum_plan"
        f" db={_fmt_bytes(space['db_size'])}"
        f" compacted={_fmt_bytes(space['compacted'])}"
        f" reclaims={_fmt_bytes(space['reclaims'])}"
        f" peak_required={_fmt_bytes(space['required'])}"
        f" free={_fmt_bytes(space['free'])}"
    )
    if space["free"] < space["required"]:
        raise SystemExit(
            "refuse_vacuum_insufficient_free_space "
            f"db={_fmt_bytes(space['db_size'])}"
            f" free={_fmt_bytes(space['free'])}"
            f" required={_fmt_bytes(space['required'])}"
            f" short={_fmt_bytes(space['required'] - space['free'])}"
        )
    print("vacuum_start")
    conn.execute("VACUUM")
    print("vacuum_done")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run/apply archive.db retention governance.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="Apply retention deletes. Default is dry-run.")
    parser.add_argument("--as-of-et", default="", help="ET date YYYY-MM-DD. Default: today in America/New_York.")
    parser.add_argument("--keep-ticks-days", type=int, default=int(os.environ.get("PM_DB_KEEP_TICKS_DAYS", "7")))
    parser.add_argument("--keep-decisions-days", type=int, default=int(os.environ.get("PM_DB_KEEP_DECISIONS_DAYS", "60")))
    parser.add_argument("--keep-facts-days", type=int, default=int(os.environ.get("PM_DB_KEEP_FACTS_DAYS", "30")))
    parser.add_argument("--keep-event-runs-days", type=int, default=int(os.environ.get("PM_DB_KEEP_EVENT_RUNS_DAYS", "60")))
    parser.add_argument("--keep-snapshots-days", type=int, default=int(os.environ.get("PM_DB_KEEP_SNAPSHOTS_DAYS", "30")))
    parser.add_argument("--keep-observations-days", type=int, default=int(os.environ.get("PM_DB_KEEP_OBSERVATIONS_DAYS", "60")))
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("PM_DB_GOVERNANCE_BATCH_SIZE", "50000")))
    parser.add_argument("--exact-table-counts", action="store_true", help="Count all rows per table; slower on large DBs.")
    parser.add_argument("--ensure-schema", action="store_true", help="Create governance schema only; no retention deletes.")
    parser.add_argument(
        "--chengdu-tick-retention",
        action="store_true",
        help="Govern event_market_ticks with Chengdu-only raw retention instead of generic keep-days.",
    )
    parser.add_argument(
        "--chengdu-keep-event-dates",
        default=os.environ.get("PM_DB_CHENGDU_KEEP_EVENT_DATES", ""),
        help="Comma-separated Chengdu event dates whose raw ticks must remain. Default: as-of ET through active-days-ahead.",
    )
    parser.add_argument(
        "--chengdu-active-days-ahead",
        type=int,
        default=int(os.environ.get("PM_DB_CHENGDU_ACTIVE_DAYS_AHEAD", "2")),
        help="When keep-event-dates is empty, keep Chengdu raw ticks from as-of ET through this many future days.",
    )
    parser.add_argument(
        "--chengdu-recent-hours",
        type=int,
        default=int(os.environ.get("PM_DB_CHENGDU_RECENT_HOURS", "48")),
        help="Keep Chengdu raw ticks newer than this UTC window.",
    )
    parser.add_argument(
        "--no-chengdu-preserve-latest-per-key",
        dest="chengdu_preserve_latest_per_key",
        action="store_false",
        help="Do not preserve the latest Chengdu raw tick per slug/event_date/outcome/side.",
    )
    parser.set_defaults(chengdu_preserve_latest_per_key=True)
    parser.add_argument(
        "--now-utc",
        default="",
        help="Fixed UTC timestamp for deterministic dry-runs/tests. Default: current UTC time.",
    )
    parser.add_argument(
        "--estimate-tick-rollup-groups",
        action="store_true",
        help="In Chengdu tick dry-run, run the heavier exact hourly rollup group estimate.",
    )
    parser.add_argument(
        "--max-tick-delete-rows",
        type=int,
        default=int(os.environ.get("PM_DB_MAX_TICK_DELETE_ROWS", "0")),
        help="Apply-mode soft cap for raw event_market_ticks deletes; stops on hour boundaries. Default: unlimited.",
    )
    parser.add_argument("--checkpoint", action="store_true", help="Run WAL checkpoint after apply.")
    parser.add_argument("--compact", action="store_true", help="Run VACUUM after apply if enough free space exists.")
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    args = parse_args()
    db = args.db.resolve()
    if not db.exists():
        print(f"missing_db={db}", file=sys.stderr)
        return 2
    as_of_et = (
        datetime.strptime(args.as_of_et, "%Y-%m-%d").date()
        if args.as_of_et
        else datetime.now(ET).date()
    )
    if args.batch_size < 1:
        args.batch_size = 50000
    print(f"mode={'APPLY' if args.apply else 'DRY_RUN'} as_of_et={as_of_et.isoformat()}")
    readonly = not (args.apply or args.ensure_schema)
    with _connect(db, readonly=readonly) as conn:
        if args.ensure_schema:
            _ensure_tick_rollup_table(conn)
            conn.commit()
            print(f"ensure_schema=1 table={TICK_ROLLUP_TABLE}")
            if not args.apply:
                return 0
        _print_db_summary(conn, db)
        print("== table row spans ==")
        for table in (
            "event_market_ticks",
            "auto_trade_decisions",
            "event_fact_history",
            "event_runs",
            "market_snapshots",
            "model_snapshots",
            "station_observations",
            "auto_orders",
            "edge_trades",
        ):
            _print_table_summary(conn, table, exact_count=bool(args.exact_table_counts))
        if args.chengdu_tick_retention:
            _run_chengdu_tick_retention(conn, args, as_of_et)
        else:
            _run_retention(conn, args, as_of_et)
        if args.apply and args.checkpoint:
            _checkpoint(conn)
        if args.apply and args.compact:
            _vacuum(conn, db)
        if not args.apply:
            print("dry_run_only=1")
            print("apply_hint=rerun_with_--apply after reviewing candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
