#!/usr/bin/env python3
"""Build a compact Chengdu-focused archive.db candidate.

Default mode is a dry run. Use --build to create a new candidate DB. Use
--swap only after validating the candidate and pausing writers.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SRC = DATA / "archive.db"
DST = DATA / "archive.chengdu_compact.candidate.db"
BACKUP = DATA / "archive.before-chengdu-compact-swap.db"
ET = ZoneInfo("America/New_York")

CHENGDU_SLUG_PREFIX = "highest-temperature-in-chengdu-"
CHENGDU_SLUG_PREFIX_UPPER = "highest-temperature-in-chengdu."
ZUUU_LOC_KEYS = {
    "30.5583,103.9460",
    "30.5728,104.0668",
    "30.6700,104.0700",
}

STATION_TABLES = {"station_forecasts", "station_observations"}
LOC_KEY_TABLES = {"model_member_cache", "model_request_cooldown"}
DEPENDENT_TABLES = {"auto_trade_runs", "auto_trade_jobs", "trade_fill_attributions"}
SPECIAL_TABLES = {
    "event_daily_temps",
    "event_market_ticks",
    "event_market_tick_rollups",
    *STATION_TABLES,
    *LOC_KEY_TABLES,
    *DEPENDENT_TABLES,
}


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def fmt_bytes(value: int) -> str:
    num = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0 or unit == "TiB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{value}B"


def rows(cur: sqlite3.Cursor, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return list(cur.execute(sql, args))


def columns(cur: sqlite3.Cursor, db: str, table: str) -> set[str]:
    return {str(row["name"]) for row in cur.execute(f"PRAGMA {db}.table_info({q(table)})")}


def chengdu_slug_pred(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}slug >= '{CHENGDU_SLUG_PREFIX}' "
        f"AND {prefix}slug < '{CHENGDU_SLUG_PREFIX_UPPER}'"
    )


def parse_csv_dates(raw: str) -> list[str]:
    result: list[str] = []
    for item in (raw or "").split(","):
        value = item.strip()
        if not value:
            continue
        try:
            result.append(datetime.strptime(value, "%Y-%m-%d").date().isoformat())
        except ValueError as exc:
            raise SystemExit(f"invalid_date={value} expected=YYYY-MM-DD") from exc
    return sorted(set(result))


def active_dates(args: argparse.Namespace, as_of_et: date) -> list[str]:
    explicit = parse_csv_dates(args.keep_event_dates)
    if explicit:
        return explicit
    return [
        (as_of_et + timedelta(days=i)).isoformat()
        for i in range(max(0, int(args.active_days_ahead)) + 1)
    ]


def now_utc(args: argparse.Namespace) -> datetime:
    raw = str(args.now_utc or "").strip()
    if raw:
        value = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise SystemExit(f"invalid_now_utc={raw} expected=ISO-8601") from exc
    return datetime.now(ZoneInfo("UTC"))


def connect_src(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def attach_src(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute("ATTACH DATABASE ? AS src", (f"file:{path}?mode=ro",))


def create_temp_scope(cur: sqlite3.Cursor, args: argparse.Namespace, as_of_et: date) -> dict[str, object]:
    dates = active_dates(args, as_of_et)
    cutoff = (now_utc(args) - timedelta(hours=max(0, int(args.recent_hours)))).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    cur.execute(
        """
        CREATE TEMP TABLE chengdu_slugs AS
        SELECT DISTINCT slug FROM src.event_runs
        WHERE (slug >= ? AND slug < ?)
           OR lower(coalesce(city,'')) LIKE '%chengdu%'
           OR upper(coalesce(station_id,'')) = 'ZUUU'
           OR lower(coalesce(resolution_source,'')) LIKE '%/cn/chengdu/%'
        UNION
        SELECT DISTINCT slug FROM src.event_daily_temps
        WHERE (slug >= ? AND slug < ?)
           OR lower(coalesce(city,'')) LIKE '%chengdu%'
           OR upper(coalesce(station_id,'')) = 'ZUUU'
           OR lower(coalesce(resolution_source,'')) LIKE '%/cn/chengdu/%'
        UNION
        SELECT DISTINCT slug FROM src.weather_market_discovery
        WHERE slug >= ? AND slug < ?
        """,
        (
            CHENGDU_SLUG_PREFIX,
            CHENGDU_SLUG_PREFIX_UPPER,
            CHENGDU_SLUG_PREFIX,
            CHENGDU_SLUG_PREFIX_UPPER,
            CHENGDU_SLUG_PREFIX,
            CHENGDU_SLUG_PREFIX_UPPER,
        ),
    )
    cur.execute(
        """
        CREATE TEMP TABLE chengdu_locs AS
        SELECT DISTINCT printf('%.4f,%.4f', lat, lon) AS loc_key
        FROM src.event_runs
        WHERE ((slug >= ? AND slug < ?)
           OR lower(coalesce(city,'')) LIKE '%chengdu%'
           OR upper(coalesce(station_id,'')) = 'ZUUU')
          AND lat IS NOT NULL AND lon IS NOT NULL
        """,
        (CHENGDU_SLUG_PREFIX, CHENGDU_SLUG_PREFIX_UPPER),
    )
    for loc_key in sorted(ZUUU_LOC_KEYS):
        cur.execute("INSERT OR IGNORE INTO chengdu_locs(loc_key) VALUES (?)", (loc_key,))

    cur.execute("CREATE TEMP TABLE chengdu_raw_tick_keep(id INTEGER PRIMARY KEY)")
    if dates:
        placeholders = ",".join("?" for _ in dates)
        cur.execute(
            f"""
            INSERT OR IGNORE INTO chengdu_raw_tick_keep(id)
            SELECT id
            FROM src.event_market_ticks
            WHERE {chengdu_slug_pred()}
              AND event_date IN ({placeholders})
            """,
            tuple(dates),
        )
    cur.execute(
        f"""
        INSERT OR IGNORE INTO chengdu_raw_tick_keep(id)
        SELECT id
        FROM src.event_market_ticks
        WHERE {chengdu_slug_pred()}
          AND created_at >= ?
        """,
        (cutoff,),
    )
    cur.execute(
        f"""
        INSERT OR IGNORE INTO chengdu_raw_tick_keep(id)
        SELECT MAX(id)
        FROM src.event_market_ticks
        WHERE {chengdu_slug_pred()}
        GROUP BY slug, event_date, outcome, side
        """
    )
    cur.execute("CREATE TEMP TABLE event_fact_history_keep(id INTEGER PRIMARY KEY)")
    cur.execute(
        """
        INSERT OR IGNORE INTO event_fact_history_keep(id)
        SELECT fact_id
        FROM src.event_fact_latest
        WHERE slug IN (SELECT slug FROM chengdu_slugs)
        """
    )
    per_event_limit = max(0, int(getattr(args, "fact_history_per_event_limit", 500)))
    active_pairs: list[tuple[str, str]] = []
    if dates and per_event_limit > 0:
        placeholders = ",".join("?" for _ in dates)
        active_pairs = [
            (str(row["slug"]), str(row["event_date"]))
            for row in cur.execute(
                f"""
                SELECT DISTINCT slug, event_date
                FROM src.event_runs
                WHERE slug IN (SELECT slug FROM chengdu_slugs)
                  AND event_date IN ({placeholders})
                """,
                tuple(dates),
            )
        ]
        for slug, event_date in active_pairs:
            ids = [
                int(row["id"])
                for row in cur.execute(
                    """
                    SELECT id
                    FROM src.event_fact_history
                    WHERE slug = ? AND event_date = ?
                    ORDER BY fact_as_of DESC, id DESC
                    LIMIT ?
                    """,
                    (slug, event_date, per_event_limit),
                )
            ]
            cur.executemany(
                "INSERT OR IGNORE INTO event_fact_history_keep(id) VALUES (?)",
                [(value,) for value in ids],
            )
    kept_facts = int(cur.execute("SELECT COUNT(*) FROM event_fact_history_keep").fetchone()[0])
    return {
        "active_dates": dates,
        "recent_cutoff": cutoff,
        "active_fact_pairs": active_pairs,
        "fact_history_per_event_limit": per_event_limit,
        "event_fact_history_keep": kept_facts,
    }


def copy_table_sql(table: str, cols: set[str]) -> str:
    name = q(table)
    if table == "event_market_ticks":
        return (
            f"INSERT INTO main.{name} SELECT * FROM src.{name} "
            "WHERE id IN (SELECT id FROM chengdu_raw_tick_keep)"
        )
    if table == "event_market_tick_rollups":
        return (
            f"INSERT OR IGNORE INTO main.{name} SELECT * FROM src.{name} "
            "WHERE slug IN (SELECT slug FROM chengdu_slugs)"
        )
    if table == "event_fact_history":
        return (
            f"INSERT INTO main.{name} SELECT * FROM src.{name} "
            "WHERE id IN (SELECT id FROM event_fact_history_keep)"
        )
    if table == "event_daily_temps":
        return (
            f"INSERT INTO main.{name} SELECT * FROM src.{name} "
            "WHERE (slug >= 'highest-temperature-in-chengdu-' AND slug < 'highest-temperature-in-chengdu.') "
            "OR lower(coalesce(city,'')) LIKE '%chengdu%' "
            "OR upper(coalesce(station_id,'')) = 'ZUUU' "
            "OR lower(coalesce(resolution_source,'')) LIKE '%/cn/chengdu/%'"
        )
    if table in STATION_TABLES:
        return (
            f"INSERT INTO main.{name} SELECT * FROM src.{name} "
            "WHERE upper(coalesce(station_id,'')) = 'ZUUU' "
            "OR lower(coalesce(resolution_source,'')) LIKE '%/cn/chengdu/%'"
        )
    if table in LOC_KEY_TABLES:
        return (
            f"INSERT INTO main.{name} SELECT * FROM src.{name} "
            "WHERE loc_key IN (SELECT loc_key FROM chengdu_locs)"
        )
    if table == "auto_trade_runs":
        return (
            f"INSERT INTO main.{name} SELECT * FROM src.{name} "
            "WHERE id IN (SELECT DISTINCT run_id FROM src.auto_trade_decisions "
            "WHERE slug IN (SELECT slug FROM chengdu_slugs))"
        )
    if table == "auto_trade_jobs":
        return (
            f"INSERT INTO main.{name} SELECT * FROM src.{name} "
            "WHERE lower(coalesce(payload_json,'')) LIKE '%chengdu%' "
            "OR upper(coalesce(payload_json,'')) LIKE '%ZUUU%' "
            "OR run_id IN (SELECT DISTINCT run_id FROM src.auto_trade_decisions "
            "WHERE slug IN (SELECT slug FROM chengdu_slugs))"
        )
    if table == "trade_fill_attributions":
        return (
            f"INSERT INTO main.{name} SELECT * FROM src.{name} "
            "WHERE trade_id IN (SELECT trade_id FROM src.edge_trade_fills "
            "WHERE slug IN (SELECT slug FROM chengdu_slugs)) "
            "OR decision_id IN (SELECT id FROM src.auto_trade_decisions "
            "WHERE slug IN (SELECT slug FROM chengdu_slugs))"
        )
    if "slug" in cols:
        return (
            f"INSERT INTO main.{name} SELECT * FROM src.{name} "
            "WHERE slug IN (SELECT slug FROM chengdu_slugs)"
        )
    return f"INSERT INTO main.{name} SELECT * FROM src.{name}"


def rollup_old_chengdu_ticks(cur: sqlite3.Cursor) -> None:
    cur.execute(
        """
        INSERT OR REPLACE INTO main.event_market_tick_rollups (
            grain, bucket_start, run_date, event_date, slug, outcome, side,
            tick_count, first_tick_at, last_tick_at,
            first_best_bid, last_best_bid, min_best_bid, max_best_bid, avg_best_bid,
            first_best_ask, last_best_ask, min_best_ask, max_best_ask, avg_best_ask,
            first_mid, last_mid, min_mid, max_mid, avg_mid,
            min_spread, max_spread, avg_spread,
            max_bid_depth_topN, avg_bid_depth_topN,
            max_ask_depth_topN, avg_ask_depth_topN, archived_at
        )
        WITH candidate AS (
            SELECT
                id, run_date, event_date, slug, outcome, side,
                replace(substr(created_at, 1, 13), ' ', 'T') || ':00:00+00:00' AS bucket_start,
                best_bid, best_ask, mid, spread, bid_depth_topN, ask_depth_topN, created_at
            FROM src.event_market_ticks
            WHERE slug IN (SELECT slug FROM chengdu_slugs)
              AND id NOT IN (SELECT id FROM chengdu_raw_tick_keep)
        ),
        ranked AS (
            SELECT *,
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
        SELECT
            'hour', bucket_start, MIN(run_date), event_date, slug, outcome, side,
            COUNT(*), MIN(created_at), MAX(created_at),
            MAX(CASE WHEN rn_first = 1 THEN best_bid END),
            MAX(CASE WHEN rn_last = 1 THEN best_bid END),
            MIN(best_bid), MAX(best_bid), AVG(best_bid),
            MAX(CASE WHEN rn_first = 1 THEN best_ask END),
            MAX(CASE WHEN rn_last = 1 THEN best_ask END),
            MIN(best_ask), MAX(best_ask), AVG(best_ask),
            MAX(CASE WHEN rn_first = 1 THEN mid END),
            MAX(CASE WHEN rn_last = 1 THEN mid END),
            MIN(mid), MAX(mid), AVG(mid),
            MIN(spread), MAX(spread), AVG(spread),
            MAX(bid_depth_topN), AVG(bid_depth_topN),
            MAX(ask_depth_topN), AVG(ask_depth_topN),
            strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        FROM ranked
        GROUP BY bucket_start, slug, event_date, outcome, side
        """
    )


def create_indexes(
    cur: sqlite3.Cursor,
    *,
    table: str = "",
    unique_only: bool = False,
    skip_existing: bool = True,
) -> None:
    where = ["type='index'", "sql IS NOT NULL"]
    params: list[str] = []
    if table:
        where.append("tbl_name = ?")
        params.append(table)
    if unique_only:
        where.append("sql LIKE 'CREATE UNIQUE INDEX%'")
    indexes = rows(
        cur,
        f"""
        SELECT name, sql
        FROM src.sqlite_master
        WHERE {' AND '.join(where)}
        ORDER BY name
        """,
        tuple(params),
    )
    for row in indexes:
        name = str(row["name"])
        if skip_existing:
            exists = cur.execute(
                "SELECT 1 FROM main.sqlite_master WHERE type='index' AND name=?",
                (name,),
            ).fetchone()
            if exists:
                continue
        cur.execute(row["sql"])


def print_plan(src: Path, args: argparse.Namespace, as_of_et: date) -> None:
    with connect_src(src) as conn:
        cur = conn.cursor()
        attach_src(conn, src)
        meta = create_temp_scope(cur, args, as_of_et)
        total_ticks = cur.execute("SELECT COUNT(*) FROM src.event_market_ticks").fetchone()[0]
        keep_ticks = cur.execute("SELECT COUNT(*) FROM chengdu_raw_tick_keep").fetchone()[0]
        chengdu_ticks = cur.execute(
            f"SELECT COUNT(*) FROM src.event_market_ticks WHERE {chengdu_slug_pred()}"
        ).fetchone()[0]
        slugs = cur.execute("SELECT COUNT(*) FROM chengdu_slugs").fetchone()[0]
        print("mode=DRY_RUN")
        print(f"src={src}")
        print(f"dst={args.dst}")
        print(
            f"db_size={fmt_bytes(src.stat().st_size)} "
            f"disk_free={fmt_bytes(shutil.disk_usage(src.parent).free)}"
        )
        print(
            "scope "
            f"chengdu_slugs={slugs} active_dates={','.join(meta['active_dates'])} "
            f"recent_cutoff_utc={meta['recent_cutoff']} "
            f"fact_history_keep={meta['event_fact_history_keep']} "
            f"fact_history_per_event_limit={meta['fact_history_per_event_limit']}"
        )
        print(
            "event_market_ticks "
            f"total={total_ticks} chengdu={chengdu_ticks} raw_keep={keep_ticks} "
            f"raw_drop={total_ticks - keep_ticks}"
        )
        print("dry_run_only=1")
        print("build_hint=rerun_with_--build to create compact candidate without swapping")


def build_candidate(src: Path, dst: Path, args: argparse.Namespace, as_of_et: date) -> None:
    if dst.exists():
        raise SystemExit(f"refuse_overwrite_existing_dst={dst}")
    conn = sqlite3.connect(str(dst), timeout=30, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=MEMORY")
    attach_src(conn, src)
    cur = conn.cursor()
    cur.execute("BEGIN")
    meta = create_temp_scope(cur, args, as_of_et)
    tables = rows(
        cur,
        """
        SELECT name, sql
        FROM src.sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """,
    )
    for row in tables:
        cur.execute(row["sql"])
    create_indexes(cur, table="event_market_tick_rollups", unique_only=True)
    counts: list[tuple[str, int, int]] = []
    for row in tables:
        table = str(row["name"])
        before = int(cur.execute(f"SELECT COUNT(*) FROM src.{q(table)}").fetchone()[0])
        cols = columns(cur, "src", table)
        cur.execute(copy_table_sql(table, cols))
        if table == "event_market_tick_rollups":
            rollup_old_chengdu_ticks(cur)
        after = int(cur.execute(f"SELECT COUNT(*) FROM main.{q(table)}").fetchone()[0])
        counts.append((table, before, after))
        print(f"copied table={table} before={before} after={after}")
    create_indexes(cur)
    conn.commit()
    integrity = cur.execute("PRAGMA main.integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit(f"integrity_check_failed={integrity}")
    cur.execute("DETACH DATABASE src")
    conn.execute("VACUUM")
    conn.close()
    print("build_complete=1")
    print(f"dst={dst} size={fmt_bytes(dst.stat().st_size)}")
    print(
        "scope "
        f"active_dates={','.join(meta['active_dates'])} "
        f"recent_cutoff_utc={meta['recent_cutoff']} "
        f"fact_history_keep={meta['event_fact_history_keep']} "
        f"fact_history_per_event_limit={meta['fact_history_per_event_limit']}"
    )
    for table, before, after in counts:
        if before or after:
            print(f"count table={table} before={before} after={after}")


def swap_candidate(src: Path, dst: Path, backup: Path) -> None:
    if not dst.exists():
        raise SystemExit(f"missing_candidate={dst}")
    if backup.exists():
        raise SystemExit(f"refuse_overwrite_existing_backup={backup}")
    for suffix in ("-wal", "-shm"):
        path = Path(str(src) + suffix)
        if path.exists():
            path.unlink()
    os.replace(src, backup)
    os.replace(dst, src)
    print(f"swap_complete=1 backup={backup}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a compact Chengdu-only archive candidate.")
    parser.add_argument("--src", type=Path, default=SRC)
    parser.add_argument("--dst", type=Path, default=DST)
    parser.add_argument("--backup", type=Path, default=BACKUP)
    parser.add_argument("--build", action="store_true", help="Create the candidate DB. Default is dry-run only.")
    parser.add_argument("--swap", action="store_true", help="Replace source DB with candidate after build/validation.")
    parser.add_argument("--as-of-et", default="", help="ET date YYYY-MM-DD. Default: today in America/New_York.")
    parser.add_argument("--keep-event-dates", default="", help="Comma-separated Chengdu event dates to keep raw ticks.")
    parser.add_argument("--active-days-ahead", type=int, default=2)
    parser.add_argument("--recent-hours", type=int, default=48)
    parser.add_argument("--fact-history-per-event-limit", type=int, default=500)
    parser.add_argument("--now-utc", default="")
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    args = parse_args()
    src = args.src.resolve()
    dst = args.dst.resolve()
    backup = args.backup.resolve()
    if not src.exists():
        raise SystemExit(f"missing_src={src}")
    as_of_et = (
        datetime.strptime(args.as_of_et, "%Y-%m-%d").date()
        if args.as_of_et
        else datetime.now(ET).date()
    )
    if args.swap and not args.build:
        raise SystemExit("refuse_swap_without_build=1")
    if not args.build:
        print_plan(src, args, as_of_et)
        return 0
    build_candidate(src, dst, args, as_of_et)
    if args.swap:
        swap_candidate(src, dst, backup)
    else:
        print("swap_hint=validate candidate, pause writers, then rerun with --build --swap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
