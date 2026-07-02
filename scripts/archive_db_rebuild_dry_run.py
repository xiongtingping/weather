#!/usr/bin/env python3
"""Plan a compact rebuild of data/archive.db without touching the source DB."""

from __future__ import annotations

import argparse
import heapq
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_REPORT_DIR = ROOT / "data" / "reports"
ET = ZoneInfo("America/New_York")


ACTIVE_JOB_STATES = {"queued", "leased", "running", "stuck"}
COPY_FULL_PRIORITY_TABLES = {
    "event_fact_latest",
    "auto_trade_decisions",
    "auto_orders",
    "edge_trade_fills",
    "trade_fill_attributions",
    "edge_trades",
    "market_tokens",
    "weather_market_discovery",
}
CANDIDATE_TABLES = {
    "event_fact_history",
    "event_fact_summary_cache",
    "event_runs",
    "model_snapshots",
    "market_snapshots",
    "auto_trade_jobs",
    "event_market_ticks",
}


@dataclass(frozen=True)
class SizeBreakdown:
    table_bytes: int = 0
    index_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.table_bytes + self.index_bytes


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _now_utc() -> str:
    return datetime.now(ZoneInfo("UTC")).replace(microsecond=0).isoformat()


def _fmt_bytes(value: float | int) -> str:
    num = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0 or unit == "TiB":
            return f"{num:.2f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{int(value or 0)}B"


def _jsonable_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _date_cutoff(as_of: date, keep_days: int) -> str:
    return (as_of - timedelta(days=max(0, int(keep_days)))).isoformat()


def _parse_date(raw: str, *, default: date) -> date:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"invalid_date={text} expected=YYYY-MM-DD") from exc


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row["name"])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]


def _index_names(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(row["name"]) for row in conn.execute(f"PRAGMA index_list({table})")]
    except sqlite3.Error:
        return []


def _dbstat_bytes(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        rows = conn.execute("SELECT name, SUM(pgsize) AS bytes FROM dbstat GROUP BY name")
    except sqlite3.Error:
        return out
    for row in rows:
        out[str(row["name"])] = _jsonable_int(row["bytes"])
    return out


def _table_size(conn: sqlite3.Connection, dbstat: Mapping[str, int], table: str) -> SizeBreakdown:
    table_bytes = int(dbstat.get(table, 0) or 0)
    index_bytes = 0
    for index in _index_names(conn, table):
        index_bytes += int(dbstat.get(index, 0) or 0)
    return SizeBreakdown(table_bytes=table_bytes, index_bytes=index_bytes)


def _clamped_ratio(value: float, *, fallback: float) -> float:
    if not math.isfinite(value) or value <= 0:
        return fallback
    return min(0.25, max(0.01, value))


def _estimate_table_bytes(
    size: SizeBreakdown,
    *,
    current_payload_bytes: int,
    new_payload_bytes: int,
    current_rows: int,
    new_rows: int,
) -> int:
    if current_payload_bytes > 0:
        table_ratio = min(1.0, max(0.0, float(new_payload_bytes) / float(current_payload_bytes)))
    elif current_rows > 0:
        table_ratio = min(1.0, max(0.0, float(new_rows) / float(current_rows)))
    else:
        table_ratio = 0.0
    index_ratio = min(1.0, max(0.0, float(new_rows) / float(current_rows))) if current_rows else 0.0
    return int(size.table_bytes * table_ratio + size.index_bytes * index_ratio)


def _sample_fact_compaction_ratio(
    conn: sqlite3.Connection,
    samples: list[tuple[int, int, str]],
    *,
    fallback_ratio: float,
) -> dict[str, Any]:
    if not samples:
        return {
            "ratio": fallback_ratio,
            "source": "fallback_no_samples",
            "samples": [],
            "error": "",
        }
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import ui.server as server  # noqa: WPS433 - reuse the SSOT compaction helper.
    except Exception as exc:
        return {
            "ratio": fallback_ratio,
            "source": "fallback_import_failed",
            "samples": [],
            "error": f"{exc.__class__.__name__}: {str(exc)[:200]}",
        }

    sample_rows: list[dict[str, Any]] = []
    original_total = 0
    compact_total = 0
    for original_bytes, fact_id, fact_as_of in samples:
        try:
            row = conn.execute(
                "SELECT event_date, fact_json FROM event_fact_history WHERE id=?",
                (fact_id,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if not row:
            continue
        try:
            payload = json.loads(str(row["fact_json"] or "{}"))
            compact = server._compact_fact_payload_for_history_storage(
                payload,
                fact_as_of=str(fact_as_of or ""),
                compacted_at="DRY_RUN",
            )
            compact_json = json.dumps(compact, ensure_ascii=False)
            compact_bytes = len(compact_json.encode("utf-8"))
        except Exception:
            continue
        original_total += int(original_bytes or 0)
        compact_total += compact_bytes
        ratio = compact_bytes / float(original_bytes) if original_bytes else 0.0
        sample_rows.append(
            {
                "fact_id": fact_id,
                "event_date": str(row["event_date"] or ""),
                "original_bytes": int(original_bytes or 0),
                "compact_bytes": compact_bytes,
                "ratio": ratio,
            }
        )
    if original_total <= 0 or compact_total <= 0:
        ratio = fallback_ratio
        source = "fallback_sample_failed"
    else:
        ratio = _clamped_ratio(compact_total / float(original_total), fallback=fallback_ratio)
        source = "sampled_existing_compaction_helper"
    return {
        "ratio": ratio,
        "source": source,
        "samples": sample_rows,
        "error": "",
    }


def _plan_event_fact_history(
    conn: sqlite3.Connection,
    size: SizeBreakdown,
    *,
    facts_cutoff: str,
    sample_limit: int,
    fallback_ratio: float,
) -> dict[str, Any]:
    latest_ids = {
        int(row["fact_id"])
        for row in conn.execute("SELECT fact_id FROM event_fact_latest WHERE fact_id IS NOT NULL")
    }
    total_rows = total_bytes = 0
    latest_rows = latest_bytes = 0
    compact_rows = compact_source_bytes = 0
    drop_rows = drop_bytes = 0
    per_event_date: dict[str, dict[str, int]] = {}
    samples_heap: list[tuple[int, int, str]] = []

    for row in conn.execute(
        """
        SELECT id, event_date, fact_as_of, length(fact_json) AS bytes
        FROM event_fact_history
        """
    ):
        fact_id = int(row["id"])
        event_date = str(row["event_date"] or "")
        fact_as_of = str(row["fact_as_of"] or "")
        payload_bytes = _jsonable_int(row["bytes"])
        total_rows += 1
        total_bytes += payload_bytes
        bucket = per_event_date.setdefault(
            event_date,
            {
                "rows": 0,
                "latest_rows": 0,
                "compact_rows": 0,
                "drop_rows": 0,
                "bytes": 0,
                "latest_bytes": 0,
                "compact_source_bytes": 0,
                "drop_bytes": 0,
            },
        )
        bucket["rows"] += 1
        bucket["bytes"] += payload_bytes

        if fact_id in latest_ids:
            latest_rows += 1
            latest_bytes += payload_bytes
            bucket["latest_rows"] += 1
            bucket["latest_bytes"] += payload_bytes
            continue

        if event_date >= facts_cutoff:
            compact_rows += 1
            compact_source_bytes += payload_bytes
            bucket["compact_rows"] += 1
            bucket["compact_source_bytes"] += payload_bytes
            if sample_limit > 0:
                item = (payload_bytes, fact_id, fact_as_of)
                if len(samples_heap) < sample_limit:
                    heapq.heappush(samples_heap, item)
                elif payload_bytes > samples_heap[0][0]:
                    heapq.heapreplace(samples_heap, item)
        else:
            drop_rows += 1
            drop_bytes += payload_bytes
            bucket["drop_rows"] += 1
            bucket["drop_bytes"] += payload_bytes

    samples = sorted(samples_heap, reverse=True)
    compaction = _sample_fact_compaction_ratio(
        conn,
        samples,
        fallback_ratio=fallback_ratio,
    )
    ratio = float(compaction["ratio"])
    compact_estimated_bytes = int(compact_source_bytes * ratio)
    new_payload_bytes = latest_bytes + compact_estimated_bytes
    keep_rows = latest_rows + compact_rows
    estimated_bytes = _estimate_table_bytes(
        size,
        current_payload_bytes=total_bytes,
        new_payload_bytes=new_payload_bytes,
        current_rows=total_rows,
        new_rows=keep_rows,
    )
    top_dates = sorted(
        (
            {
                "event_date": event_date,
                **stats,
            }
            for event_date, stats in per_event_date.items()
        ),
        key=lambda item: int(item["bytes"]),
        reverse=True,
    )[:12]
    return {
        "policy": "keep_latest_full_compact_recent_nonlatest_drop_older_nonlatest",
        "cutoff_event_date": facts_cutoff,
        "current_rows": total_rows,
        "current_payload_bytes": total_bytes,
        "current_db_bytes": size.total_bytes,
        "latest_rows_full_keep": latest_rows,
        "latest_payload_bytes": latest_bytes,
        "compact_rows": compact_rows,
        "compact_source_bytes": compact_source_bytes,
        "compact_estimated_bytes": compact_estimated_bytes,
        "drop_rows": drop_rows,
        "drop_payload_bytes": drop_bytes,
        "estimated_rows_after_rebuild": keep_rows,
        "estimated_payload_bytes_after_rebuild": new_payload_bytes,
        "estimated_db_bytes_after_rebuild": estimated_bytes,
        "estimated_db_bytes_released": max(0, size.total_bytes - estimated_bytes),
        "compaction_ratio": ratio,
        "compaction_ratio_source": str(compaction["source"]),
        "compaction_samples": compaction["samples"],
        "top_event_dates": top_dates,
    }


def _plan_event_fact_summary_cache(
    conn: sqlite3.Connection,
    size: SizeBreakdown,
    *,
    facts_cutoff: str,
) -> dict[str, Any]:
    latest_ids = {
        int(row["fact_id"])
        for row in conn.execute("SELECT fact_id FROM event_fact_latest WHERE fact_id IS NOT NULL")
    }
    total_rows = total_bytes = 0
    keep_rows = keep_bytes = latest_rows = recent_rows = 0
    drop_rows = drop_bytes = 0
    for row in conn.execute(
        """
        SELECT fact_id, event_date, length(summary_json) AS bytes
        FROM event_fact_summary_cache
        """
    ):
        fact_id = int(row["fact_id"] or 0)
        event_date = str(row["event_date"] or "")
        payload_bytes = _jsonable_int(row["bytes"])
        total_rows += 1
        total_bytes += payload_bytes
        keep = fact_id in latest_ids or event_date >= facts_cutoff
        if keep:
            keep_rows += 1
            keep_bytes += payload_bytes
            if fact_id in latest_ids:
                latest_rows += 1
            else:
                recent_rows += 1
        else:
            drop_rows += 1
            drop_bytes += payload_bytes
    estimated_bytes = _estimate_table_bytes(
        size,
        current_payload_bytes=total_bytes,
        new_payload_bytes=keep_bytes,
        current_rows=total_rows,
        new_rows=keep_rows,
    )
    return {
        "policy": "keep_latest_fact_cache_and_recent_event_date",
        "cutoff_event_date": facts_cutoff,
        "current_rows": total_rows,
        "current_payload_bytes": total_bytes,
        "current_db_bytes": size.total_bytes,
        "keep_rows": keep_rows,
        "latest_rows": latest_rows,
        "recent_nonlatest_rows": recent_rows,
        "keep_payload_bytes": keep_bytes,
        "drop_rows": drop_rows,
        "drop_payload_bytes": drop_bytes,
        "estimated_db_bytes_after_rebuild": estimated_bytes,
        "estimated_db_bytes_released": max(0, size.total_bytes - estimated_bytes),
    }


def _plan_latest_by_event_table(
    conn: sqlite3.Connection,
    table: str,
    size: SizeBreakdown,
    *,
    cutoff: str,
    payload_columns: Iterable[str],
) -> dict[str, Any]:
    rows: list[tuple[str, int, str, str, tuple[str, int]]] = []
    length_expr = " + ".join(f"COALESCE(length({col}), 0)" for col in payload_columns)
    query = f"""
        SELECT id, slug, event_date, created_at, ({length_expr}) AS bytes
        FROM {table}
    """
    for row in conn.execute(query):
        marker = (str(row["created_at"] or ""), int(row["id"] or 0))
        rows.append(
            (
                str(row["event_date"] or ""),
                _jsonable_int(row["bytes"]),
                str(row["slug"] or ""),
                str(row["event_date"] or ""),
                marker,
            )
        )
    latest: dict[tuple[str, str], tuple[tuple[str, int], int]] = {}
    for _, payload_bytes, slug, event_date, marker in rows:
        key = (slug, event_date)
        old = latest.get(key)
        if old is None or marker > old[0]:
            latest[key] = (marker, payload_bytes)

    total_rows = len(rows)
    total_bytes = sum(row[1] for row in rows)
    keep_rows = keep_bytes = latest_kept = 0
    drop_rows = drop_bytes = 0
    for event_date_key, payload_bytes, slug, event_date, marker in rows:
        latest_row = latest.get((slug, event_date))
        keep = event_date_key >= cutoff or (latest_row is not None and latest_row[0] == marker)
        if keep:
            keep_rows += 1
            keep_bytes += payload_bytes
            if latest_row is not None and latest_row[0] == marker and event_date_key < cutoff:
                latest_kept += 1
        else:
            drop_rows += 1
            drop_bytes += payload_bytes

    estimated_bytes = _estimate_table_bytes(
        size,
        current_payload_bytes=total_bytes,
        new_payload_bytes=keep_bytes,
        current_rows=total_rows,
        new_rows=keep_rows,
    )
    return {
        "policy": "keep_recent_by_event_date_and_latest_per_slug_event_date",
        "cutoff_event_date": cutoff,
        "current_rows": total_rows,
        "current_payload_bytes": total_bytes,
        "current_db_bytes": size.total_bytes,
        "keep_rows": keep_rows,
        "keep_payload_bytes": keep_bytes,
        "keep_older_latest_rows": latest_kept,
        "drop_rows": drop_rows,
        "drop_payload_bytes": drop_bytes,
        "estimated_db_bytes_after_rebuild": estimated_bytes,
        "estimated_db_bytes_released": max(0, size.total_bytes - estimated_bytes),
    }


def _plan_auto_trade_jobs(
    conn: sqlite3.Connection,
    size: SizeBreakdown,
    *,
    cutoff: str,
) -> dict[str, Any]:
    total_rows = total_bytes = 0
    keep_rows = keep_bytes = 0
    drop_rows = drop_bytes = 0
    state_counts: dict[str, int] = {}
    for row in conn.execute(
        """
        SELECT id, state, substr(created_at, 1, 10) AS created_date, length(payload_json) AS bytes
        FROM auto_trade_jobs
        """
    ):
        state = str(row["state"] or "")
        created_date = str(row["created_date"] or "")
        payload_bytes = _jsonable_int(row["bytes"])
        total_rows += 1
        total_bytes += payload_bytes
        state_counts[state] = state_counts.get(state, 0) + 1
        if state in ACTIVE_JOB_STATES or created_date >= cutoff:
            keep_rows += 1
            keep_bytes += payload_bytes
        else:
            drop_rows += 1
            drop_bytes += payload_bytes

    estimated_bytes = _estimate_table_bytes(
        size,
        current_payload_bytes=total_bytes,
        new_payload_bytes=keep_bytes,
        current_rows=total_rows,
        new_rows=keep_rows,
    )
    return {
        "policy": "keep_recent_created_at_and_active_states",
        "cutoff_created_date": cutoff,
        "active_states": sorted(ACTIVE_JOB_STATES),
        "current_rows": total_rows,
        "current_payload_bytes": total_bytes,
        "current_db_bytes": size.total_bytes,
        "keep_rows": keep_rows,
        "keep_payload_bytes": keep_bytes,
        "drop_rows": drop_rows,
        "drop_payload_bytes": drop_bytes,
        "state_counts": state_counts,
        "estimated_db_bytes_after_rebuild": estimated_bytes,
        "estimated_db_bytes_released": max(0, size.total_bytes - estimated_bytes),
    }


def _plan_event_market_ticks(
    conn: sqlite3.Connection,
    size: SizeBreakdown,
    *,
    cutoff: str,
) -> dict[str, Any]:
    cutoff_dt = f"{cutoff}T00:00:00+00:00"
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_rows,
            SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS recent_rows
        FROM event_market_ticks
        """,
        (cutoff_dt,),
    ).fetchone()
    total_rows = _jsonable_int(row["total_rows"] if row else 0)
    recent_rows = _jsonable_int(row["recent_rows"] if row else 0)
    older_latest_row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM (
            SELECT MAX(created_at) AS latest_created_at
            FROM event_market_ticks
            GROUP BY slug, event_date, outcome, side
        )
        WHERE latest_created_at < ?
        """,
        (cutoff_dt,),
    ).fetchone()
    older_latest_rows = _jsonable_int(older_latest_row["n"] if older_latest_row else 0)
    keep_rows = min(total_rows, recent_rows + older_latest_rows)
    drop_rows = max(0, total_rows - keep_rows)
    ratio = keep_rows / float(total_rows) if total_rows else 0.0
    estimated_bytes = int(size.total_bytes * ratio)
    return {
        "policy": "keep_recent_created_at_and_latest_per_slug_event_outcome_side",
        "cutoff_created_at": cutoff_dt,
        "current_rows": total_rows,
        "current_db_bytes": size.total_bytes,
        "recent_rows": recent_rows,
        "older_latest_rows": older_latest_rows,
        "keep_rows": keep_rows,
        "drop_rows": drop_rows,
        "estimated_db_bytes_after_rebuild": estimated_bytes,
        "estimated_db_bytes_released": max(0, size.total_bytes - estimated_bytes),
        "note": "tick byte estimate is proportional by row count because tick rows are narrow and indexes dominate",
    }


def _copy_full_table_plan(
    conn: sqlite3.Connection,
    table: str,
    size: SizeBreakdown,
) -> dict[str, Any]:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        rows = _jsonable_int(row["n"] if row else 0)
    except sqlite3.Error:
        rows = 0
    return {
        "policy": "copy_full",
        "current_rows": rows,
        "current_db_bytes": size.total_bytes,
        "estimated_db_bytes_after_rebuild": size.total_bytes,
        "estimated_db_bytes_released": 0,
    }


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"archive_db_missing path={db_path}")
    as_of_default = datetime.now(ET).date()
    as_of_date = _parse_date(str(args.as_of_date or ""), default=as_of_default)
    facts_cutoff = _date_cutoff(as_of_date, int(args.keep_facts_days))
    runs_cutoff = _date_cutoff(as_of_date, int(args.keep_runs_days))
    snapshots_cutoff = _date_cutoff(as_of_date, int(args.keep_snapshots_days))
    jobs_cutoff = _date_cutoff(as_of_date, int(args.keep_jobs_days))
    ticks_cutoff = _date_cutoff(as_of_date, int(args.keep_ticks_days))

    source_size = db_path.stat().st_size
    wal_path = Path(str(db_path) + "-wal")
    wal_size = wal_path.stat().st_size if wal_path.exists() else 0

    with _connect_ro(db_path) as conn:
        page_size = _jsonable_int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = _jsonable_int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = _jsonable_int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "")
        tables = _table_names(conn)
        dbstat = _dbstat_bytes(conn)
        table_sizes = {table: _table_size(conn, dbstat, table) for table in tables}

        table_plans: dict[str, dict[str, Any]] = {}
        if _table_exists(conn, "event_fact_history") and _table_exists(conn, "event_fact_latest"):
            table_plans["event_fact_history"] = _plan_event_fact_history(
                conn,
                table_sizes.get("event_fact_history", SizeBreakdown()),
                facts_cutoff=facts_cutoff,
                sample_limit=max(0, int(args.fact_sample_limit)),
                fallback_ratio=float(args.fact_compact_ratio),
            )
        if _table_exists(conn, "event_fact_summary_cache") and _table_exists(conn, "event_fact_latest"):
            table_plans["event_fact_summary_cache"] = _plan_event_fact_summary_cache(
                conn,
                table_sizes.get("event_fact_summary_cache", SizeBreakdown()),
                facts_cutoff=facts_cutoff,
            )
        for table in ("event_runs", "model_snapshots", "market_snapshots"):
            if _table_exists(conn, table):
                extra = ("suggestions_json",) if table == "event_runs" else ("meta_json",)
                table_plans[table] = _plan_latest_by_event_table(
                    conn,
                    table,
                    table_sizes.get(table, SizeBreakdown()),
                    cutoff=runs_cutoff if table == "event_runs" else snapshots_cutoff,
                    payload_columns=("payload_json", *extra),
                )
        if _table_exists(conn, "auto_trade_jobs"):
            table_plans["auto_trade_jobs"] = _plan_auto_trade_jobs(
                conn,
                table_sizes.get("auto_trade_jobs", SizeBreakdown()),
                cutoff=jobs_cutoff,
            )
        if _table_exists(conn, "event_market_ticks"):
            table_plans["event_market_ticks"] = _plan_event_market_ticks(
                conn,
                table_sizes.get("event_market_ticks", SizeBreakdown()),
                cutoff=ticks_cutoff,
            )

        for table in tables:
            if table in table_plans:
                continue
            table_plans[table] = _copy_full_table_plan(
                conn,
                table,
                table_sizes.get(table, SizeBreakdown()),
            )

    current_db_bytes = page_size * page_count if page_size and page_count else source_size
    estimated_rebuilt_bytes = sum(
        _jsonable_int(plan.get("estimated_db_bytes_after_rebuild"))
        for plan in table_plans.values()
    )
    estimated_release_bytes = max(0, current_db_bytes - estimated_rebuilt_bytes)
    candidate_release = {
        table: _jsonable_int(table_plans.get(table, {}).get("estimated_db_bytes_released"))
        for table in sorted(CANDIDATE_TABLES)
        if table in table_plans
    }
    copy_full_tables = sorted(table for table, plan in table_plans.items() if plan.get("policy") == "copy_full")
    return {
        "ok": True,
        "dry_run": True,
        "generated_at_utc": _now_utc(),
        "source_db": str(db_path),
        "source_db_size_bytes": source_size,
        "source_wal_size_bytes": wal_size,
        "sqlite": {
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "journal_mode": journal_mode,
        },
        "settings": {
            "as_of_date_et": as_of_date.isoformat(),
            "keep_facts_days": int(args.keep_facts_days),
            "keep_runs_days": int(args.keep_runs_days),
            "keep_snapshots_days": int(args.keep_snapshots_days),
            "keep_jobs_days": int(args.keep_jobs_days),
            "keep_ticks_days": int(args.keep_ticks_days),
            "facts_cutoff_event_date": facts_cutoff,
            "runs_cutoff_event_date": runs_cutoff,
            "snapshots_cutoff_event_date": snapshots_cutoff,
            "jobs_cutoff_created_date": jobs_cutoff,
            "ticks_cutoff_created_date": ticks_cutoff,
            "fact_sample_limit": int(args.fact_sample_limit),
            "fact_compact_ratio_fallback": float(args.fact_compact_ratio),
        },
        "summary": {
            "current_main_db_bytes": current_db_bytes,
            "estimated_rebuilt_db_bytes": estimated_rebuilt_bytes,
            "estimated_release_bytes": estimated_release_bytes,
            "estimated_rebuilt_db_human": _fmt_bytes(estimated_rebuilt_bytes),
            "estimated_release_human": _fmt_bytes(estimated_release_bytes),
            "candidate_release_bytes": candidate_release,
            "copy_full_table_count": len(copy_full_tables),
            "candidate_table_count": len(candidate_release),
        },
        "must_keep_tables": sorted(COPY_FULL_PRIORITY_TABLES & set(table_plans)),
        "copy_full_tables": copy_full_tables,
        "table_plans": table_plans,
        "apply_outline": [
            "Open source archive.db read-only and create a separate new SQLite file.",
            "Create schema and indexes from sqlite_master in the new file.",
            "Copy all copy_full tables without filtering.",
            "Copy event_fact_latest and full latest event_fact_history rows first.",
            "Insert retained non-latest event_fact_history rows as compact fact_json.",
            "Copy retained event_runs/model_snapshots/market_snapshots rows by retention policy.",
            "Copy retained active/recent auto_trade_jobs rows.",
            "Copy retained recent/latest event_market_ticks rows after rollup coverage is confirmed.",
            "Run integrity checks on the new file, then swap only after explicit confirmation.",
        ],
        "warnings": [
            "This is an estimate; actual rebuilt size depends on SQLite page packing and index rebuilds.",
            "No source DB writes are performed by this dry-run script.",
            "auto_trade_decisions is copied full because it is the execution-blocking SSOT.",
            "edge_trades is copied full as legacy execution ledger/compat state unless separately archived.",
        ],
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _markdown_table(rows: list[tuple[str, str, str, str, str]]) -> str:
    lines = ["| 表 | 策略 | 当前 | 预计新库 | 预计释放 |", "| --- | --- | ---: | ---: | ---: |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _write_markdown(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), Mapping) else {}
    settings = payload.get("settings", {}) if isinstance(payload.get("settings"), Mapping) else {}
    table_plans = payload.get("table_plans", {}) if isinstance(payload.get("table_plans"), Mapping) else {}
    candidate_rows: list[tuple[str, str, str, str, str]] = []
    for table in sorted(CANDIDATE_TABLES):
        plan = table_plans.get(table)
        if not isinstance(plan, Mapping):
            continue
        candidate_rows.append(
            (
                f"`{table}`",
                str(plan.get("policy") or ""),
                _fmt_bytes(_jsonable_int(plan.get("current_db_bytes"))),
                _fmt_bytes(_jsonable_int(plan.get("estimated_db_bytes_after_rebuild"))),
                _fmt_bytes(_jsonable_int(plan.get("estimated_db_bytes_released"))),
            )
        )
    fact_plan = table_plans.get("event_fact_history", {}) if isinstance(table_plans.get("event_fact_history"), Mapping) else {}
    sample_rows = fact_plan.get("compaction_samples", []) if isinstance(fact_plan.get("compaction_samples"), list) else []
    sample_lines = ["| fact_id | event_date | 原始 | compact | 比例 |", "| ---: | --- | ---: | ---: | ---: |"]
    for sample in sample_rows[:8]:
        if not isinstance(sample, Mapping):
            continue
        ratio = float(sample.get("ratio") or 0)
        sample_lines.append(
            "| {fact_id} | {event_date} | {orig} | {comp} | {ratio:.2%} |".format(
                fact_id=sample.get("fact_id"),
                event_date=sample.get("event_date"),
                orig=_fmt_bytes(_jsonable_int(sample.get("original_bytes"))),
                comp=_fmt_bytes(_jsonable_int(sample.get("compact_bytes"))),
                ratio=ratio,
            )
        )
    text = f"""# archive.db 重建小库 dry-run 计划

生成时间：{payload.get("generated_at_utc")}  
源库：`{payload.get("source_db")}`  
模式：只读 dry-run，未创建新库，未写入源库。

## 估算总览

- 当前主库估算：{_fmt_bytes(_jsonable_int(summary.get("current_main_db_bytes")))}
- 预计重建后：{_fmt_bytes(_jsonable_int(summary.get("estimated_rebuilt_db_bytes")))}
- 预计释放：{_fmt_bytes(_jsonable_int(summary.get("estimated_release_bytes")))}
- facts cutoff：`{settings.get("facts_cutoff_event_date")}`
- runs cutoff：`{settings.get("runs_cutoff_event_date")}`
- snapshots cutoff：`{settings.get("snapshots_cutoff_event_date")}`
- jobs cutoff：`{settings.get("jobs_cutoff_created_date")}`
- ticks cutoff：`{settings.get("ticks_cutoff_created_date")}`

## 候选表计划

{_markdown_table(candidate_rows)}

## event_fact_history

- latest 完整保留行：{fact_plan.get("latest_rows_full_keep", 0)}
- non-latest compact 行：{fact_plan.get("compact_rows", 0)}
- non-latest 删除行：{fact_plan.get("drop_rows", 0)}
- compact 比例：{float(fact_plan.get("compaction_ratio") or 0):.2%}（{fact_plan.get("compaction_ratio_source", "")}）

## compact 样本

{chr(10).join(sample_lines)}

## 必须全量复制

{", ".join(f"`{table}`" for table in payload.get("must_keep_tables", []))}

## 执行轮廓

""" + "\n".join(f"{idx}. {step}" for idx, step in enumerate(payload.get("apply_outline", []), start=1)) + "\n"
    path.write_text(text, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="source archive.db path")
    parser.add_argument("--as-of-date", default="", help="ET date for retention math, YYYY-MM-DD")
    parser.add_argument("--keep-facts-days", type=int, default=2)
    parser.add_argument("--keep-runs-days", type=int, default=2)
    parser.add_argument("--keep-snapshots-days", type=int, default=2)
    parser.add_argument("--keep-jobs-days", type=int, default=2)
    parser.add_argument("--keep-ticks-days", type=int, default=2)
    parser.add_argument("--fact-sample-limit", type=int, default=8)
    parser.add_argument("--fact-compact-ratio", type=float, default=0.05)
    parser.add_argument("--report-json", default="", help="output JSON path")
    parser.add_argument("--report-md", default="", help="output Markdown path")
    parser.add_argument("--print-json", action="store_true", help="print full JSON payload")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_plan(args)
    stamp = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    report_json = Path(args.report_json) if args.report_json else DEFAULT_REPORT_DIR / f"archive_db_rebuild_dry_run_{stamp}.json"
    report_md = Path(args.report_md) if args.report_md else DEFAULT_REPORT_DIR / f"archive_db_rebuild_dry_run_{stamp}.md"
    payload["report_json"] = str(report_json)
    payload["report_md"] = str(report_md)
    _write_json(report_json, payload)
    _write_markdown(report_md, payload)
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        summary = payload["summary"]
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "report_json": str(report_json),
                    "report_md": str(report_md),
                    "estimated_rebuilt_db": summary["estimated_rebuilt_db_human"],
                    "estimated_release": summary["estimated_release_human"],
                    "settings": payload["settings"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
