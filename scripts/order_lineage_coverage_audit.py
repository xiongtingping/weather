#!/usr/bin/env python3
"""Read-only coverage audit for auto order lineage back to the SSOT chain."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ssot_chain_replay_audit  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
REQUIRED_COLUMNS = (
    "order_id",
    "correlation_id",
    "decision_id",
    "signal_id",
    "order_intent_id",
)


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "executes_sql_writes": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"] or "") for row in conn.execute(f"PRAGMA table_info({table})")}


def _missing_count_expr(column: str) -> str:
    return f"SUM(CASE WHEN COALESCE({column}, '') = '' THEN 1 ELSE 0 END) AS missing_{column}"


def _utc_cutoff(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=float(hours))).isoformat(timespec="seconds")


def _cutoff_from_ignore_before(raw: str) -> tuple[str, dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return "", {"ignore_orders_before": "", "ignore_orders_before_timezone": "America/New_York"}
    tz = ZoneInfo("America/New_York")
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            local_dt = datetime.fromisoformat(text).replace(tzinfo=tz)
            source_format = "date_et_midnight"
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            local_dt = parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed
            source_format = "datetime"
    except Exception:
        return "", {
            "ignore_orders_before": text,
            "ignore_orders_before_timezone": "America/New_York",
            "ignore_orders_before_error": "invalid_iso_date_or_datetime",
        }
    cutoff = local_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    return cutoff, {
        "ignore_orders_before": text,
        "ignore_orders_before_timezone": "America/New_York",
        "ignore_orders_before_source_format": source_format,
    }


def _scope_clause(
    columns: set[str],
    *,
    ignore_legacy_orders: bool,
    order_since_hours: float,
    ignore_orders_before: str = "",
) -> tuple[str, list[Any], dict[str, Any]]:
    scope = {
        "ignore_legacy_orders": bool(ignore_legacy_orders),
        "order_since_hours": float(order_since_hours),
        "ignore_orders_before": str(ignore_orders_before or "").strip(),
        "ignore_orders_before_timezone": "America/New_York",
        "cutoff_created_at": "",
        "scope_field": "",
        "scope_filter_applied": False,
        "scope_filter_na_reason": "",
    }
    if not ignore_legacy_orders:
        scope["scope_filter_na_reason"] = "legacy_orders_not_ignored"
        return "", [], scope
    if str(ignore_orders_before or "").strip():
        cutoff, meta = _cutoff_from_ignore_before(str(ignore_orders_before or ""))
        scope.update(meta)
        if not cutoff:
            scope["scope_filter_na_reason"] = "ignore_orders_before_invalid"
            return "", [], scope
        if "created_at" not in columns:
            scope["scope_filter_na_reason"] = "auto_orders_created_at_missing"
            return "", [], scope
        scope.update(
            {
                "cutoff_created_at": cutoff,
                "scope_field": "created_at",
                "scope_filter_applied": True,
                "scope_filter_mode": "ignore_orders_before",
            }
        )
        return " WHERE created_at >= ?", [cutoff], scope
    if float(order_since_hours) <= 0:
        scope["scope_filter_na_reason"] = "order_since_hours_not_positive"
        return "", [], scope
    if "created_at" not in columns:
        scope["scope_filter_na_reason"] = "auto_orders_created_at_missing"
        return "", [], scope
    cutoff = _utc_cutoff(float(order_since_hours))
    scope.update(
        {
            "cutoff_created_at": cutoff,
            "scope_field": "created_at",
            "scope_filter_applied": True,
            "scope_filter_mode": "order_since_hours",
        }
    )
    return " WHERE created_at >= ?", [cutoff], scope


def _json_map(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _decision_payload_for_order(conn: sqlite3.Connection, order: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if not _table_exists(conn, "auto_trade_decisions"):
        return {}, "auto_trade_decisions_table_missing"
    columns = _columns(conn, "auto_trade_decisions")
    if "details_json" not in columns:
        return {}, "auto_trade_decisions_details_json_missing"
    order_id = _text(order.get("order_id"))
    if order_id:
        rows = conn.execute(
            """
            SELECT details_json
            FROM auto_trade_decisions
            WHERE json_extract(details_json, '$.order_id') = ?
            ORDER BY id DESC
            LIMIT 2
            """,
            (order_id,),
        ).fetchall()
        if len(rows) == 1:
            return _json_map(rows[0]["details_json"]), "auto_trade_decisions.details_json.order_id"
        if len(rows) > 1:
            return {}, "auto_trade_decisions.details_json.order_id_ambiguous"
    return {}, "decision_payload_missing_for_order_id"


def _missing_signal_applicability(
    conn: sqlite3.Connection,
    *,
    sample_limit: int,
    scope_where: str = "",
    scope_params: Sequence[Any] = (),
) -> dict[str, Any]:
    columns = _columns(conn, "auto_orders")
    if "signal_id" not in columns:
        return {
            "required_missing_count": 0,
            "not_applicable_missing_count": 0,
            "unknown_missing_count": 0,
            "samples": [],
        }
    select_columns = [
        column
        for column in (
            "id",
            "order_id",
            "decision_id",
            "signal_id",
            "order_intent_id",
            "slug",
            "event_date",
            "outcome",
            "side",
            "action",
            "created_at",
        )
        if column in columns
    ]
    rows = conn.execute(
        f"""
        SELECT {', '.join(select_columns)}
        FROM auto_orders
        {scope_where}
        {"AND" if scope_where else "WHERE"} (signal_id IS NULL OR trim(CAST(signal_id AS TEXT)) = '')
        ORDER BY id DESC
        """,
        tuple(scope_params),
    ).fetchall()
    required_missing = 0
    not_applicable_missing = 0
    unknown_missing = 0
    samples: list[dict[str, Any]] = []
    for row in rows:
        order = {key: row[key] for key in row.keys()}
        payload, source = _decision_payload_for_order(conn, order)
        if payload:
            signal_expected = ssot_chain_replay_audit._payload_has_hard_source_signal_expected(payload)
            strategy = _text(payload.get("strategy"))
            if signal_expected:
                required_missing += 1
                state = "required_hard_source_signal_missing"
            else:
                not_applicable_missing += 1
                state = "not_applicable_non_hard_source_order"
        else:
            strategy = ""
            if _text(order.get("decision_id")):
                unknown_missing += 1
                state = "unknown_decision_payload_missing"
            else:
                not_applicable_missing += 1
                state = "not_applicable_until_decision_anchor_exists"
        if len(samples) < max(0, int(sample_limit)):
            samples.append(
                {
                    "id": order.get("id"),
                    "order_id": order.get("order_id"),
                    "decision_id": order.get("decision_id"),
                    "order_intent_id": order.get("order_intent_id"),
                    "slug": order.get("slug"),
                    "event_date": order.get("event_date"),
                    "outcome": order.get("outcome"),
                    "side": order.get("side"),
                    "state": state,
                    "decision_payload_source": source,
                    "strategy": strategy,
                }
            )
    return {
        "required_missing_count": required_missing,
        "not_applicable_missing_count": not_applicable_missing,
        "unknown_missing_count": unknown_missing,
        "samples": samples,
    }


def _sample_orders(
    conn: sqlite3.Connection,
    *,
    limit: int,
    scope_where: str = "",
    scope_params: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    columns = _columns(conn, "auto_orders")
    select_columns = [
        column
        for column in (
            "id",
            "order_id",
            "created_at",
            "slug",
            "event_date",
            "outcome",
            "decision_id",
            "signal_id",
            "order_intent_id",
            "correlation_id",
        )
        if column in columns
    ]
    if not select_columns or limit <= 0:
        return []
    order_column = "created_at" if "created_at" in columns else "id"
    rows = conn.execute(
        f"SELECT {', '.join(select_columns)} FROM auto_orders {scope_where} ORDER BY {order_column} DESC LIMIT ?",
        tuple(scope_params) + (int(limit),),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _replay_samples(
    db: Path,
    orders: Sequence[Mapping[str, Any]],
    *,
    max_replay_samples: int,
    since_hours: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for order in list(orders)[: max(0, int(max_replay_samples))]:
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            continue
        replay = ssot_chain_replay_audit.audit(
            db,
            order_id=order_id,
            since_hours=float(since_hours),
            limit=50,
            max_signals_output=3,
        )
        out.append(
            {
                "order_id": order_id,
                "ok": bool(replay.get("ok")),
                "effective_signal_id": replay.get("effective_signal_id"),
                "effective_posterior_run_id": replay.get("effective_posterior_run_id"),
                "blockers": list(replay.get("blockers") or [])[:8],
                "warnings": list(replay.get("warnings") or [])[:8],
            }
        )
    return out


def build_audit(
    db_path: Path | str = DEFAULT_DB,
    *,
    limit: int = 20,
    max_replay_samples: int = 0,
    since_hours: float = 24.0 * 14.0,
    ignore_legacy_orders: bool = False,
    order_since_hours: float = 0.0,
    ignore_orders_before: str = "",
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    blockers: list[str] = []
    warnings: list[str] = []
    missing_by_column: dict[str, int] = {}
    total_orders = 0
    scoped_order_count = 0
    legacy_ignored_count = 0
    sample_orders: list[dict[str, Any]] = []
    replay_samples: list[dict[str, Any]] = []
    missing_columns: list[str] = []
    signal_applicability: dict[str, Any] = {}
    order_scope: dict[str, Any] = {
        "ignore_legacy_orders": bool(ignore_legacy_orders),
        "order_since_hours": float(order_since_hours),
        "ignore_orders_before": str(ignore_orders_before or "").strip(),
        "scope_filter_applied": False,
    }

    if not db.exists():
        blockers.append("archive_db_missing")
    else:
        with _connect(db) as conn:
            if not _table_exists(conn, "auto_orders"):
                blockers.append("auto_orders_table_missing")
            else:
                columns = _columns(conn, "auto_orders")
                missing_columns = [column for column in REQUIRED_COLUMNS if column not in columns]
                if missing_columns:
                    blockers.append("auto_orders_required_lineage_columns_missing")
                total_orders = int(conn.execute("SELECT COUNT(*) FROM auto_orders").fetchone()[0] or 0)
                scope_where, scope_params, order_scope = _scope_clause(
                    columns,
                    ignore_legacy_orders=bool(ignore_legacy_orders),
                    order_since_hours=float(order_since_hours),
                    ignore_orders_before=str(ignore_orders_before or ""),
                )
                scoped_order_count = int(
                    conn.execute(f"SELECT COUNT(*) FROM auto_orders {scope_where}", tuple(scope_params)).fetchone()[0]
                    or 0
                )
                legacy_ignored_count = max(0, total_orders - scoped_order_count)
                order_scope["total_order_count"] = total_orders
                order_scope["scoped_order_count"] = scoped_order_count
                order_scope["legacy_ignored_count"] = legacy_ignored_count
                if not total_orders:
                    warnings.append("auto_orders_empty")
                elif bool(ignore_legacy_orders) and scoped_order_count == 0:
                    warnings.append("order_lineage_scoped_orders_empty")
                present_required = [column for column in REQUIRED_COLUMNS if column in columns]
                if present_required:
                    row = conn.execute(
                        "SELECT "
                        + ", ".join(["COUNT(*) AS total", *[_missing_count_expr(c) for c in present_required]])
                        + f" FROM auto_orders {scope_where}",
                        tuple(scope_params),
                    ).fetchone()
                    for column in present_required:
                        missing_by_column[column] = int(row[f"missing_{column}"] or 0)
                for column in missing_columns:
                    missing_by_column[column] = scoped_order_count
                for column, count in missing_by_column.items():
                    if count > 0 and column != "signal_id":
                        blockers.append(f"auto_orders_{column}_missing")
                if missing_by_column.get("signal_id", 0) > 0:
                    signal_applicability = _missing_signal_applicability(
                        conn,
                        sample_limit=int(limit),
                        scope_where=scope_where,
                        scope_params=scope_params,
                    )
                    if int(signal_applicability.get("required_missing_count") or 0) > 0:
                        blockers.append("auto_orders_signal_id_missing_for_hard_source_signal")
                    if int(signal_applicability.get("unknown_missing_count") or 0) > 0:
                        blockers.append("auto_orders_signal_id_applicability_unknown")
                sample_orders = _sample_orders(
                    conn,
                    limit=int(limit),
                    scope_where=scope_where,
                    scope_params=scope_params,
                )
        if sample_orders and int(max_replay_samples) > 0:
            replay_samples = _replay_samples(
                db,
                sample_orders,
                max_replay_samples=int(max_replay_samples),
                since_hours=float(since_hours),
            )
            failed = [row for row in replay_samples if not bool(row.get("ok"))]
            if failed:
                blockers.append("auto_order_replay_sample_failed")

    return {
        "schema": "order_lineage_coverage_audit_v1",
        "ok": not blockers,
        "read_only": True,
        "db": str(db),
        "total_order_count": total_orders,
        "scoped_order_count": scoped_order_count,
        "legacy_ignored_count": legacy_ignored_count,
        "order_scope": order_scope,
        "required_columns": list(REQUIRED_COLUMNS),
        "missing_columns": missing_columns,
        "missing_by_column": missing_by_column,
        "signal_applicability": signal_applicability,
        "sample_orders": sample_orders[: max(0, int(limit))],
        "replay_sample_count": len(replay_samples),
        "replay_samples": replay_samples,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": _guards(),
        "next_boundary": (
            "order_lineage_coverage_ready"
            if not blockers
            else "merge_fill_auto_order_lineage_or_replay_missing_order_anchors"
        ),
    }


def compact_audit(audit: Mapping[str, Any], *, max_items_output: int = 8) -> dict[str, Any]:
    max_items = max(0, int(max_items_output))
    return {
        "schema": "order_lineage_coverage_audit_v1_summary",
        "source_schema": audit.get("schema"),
        "ok": bool(audit.get("ok")),
        "read_only": bool(audit.get("read_only")),
        "db": audit.get("db"),
        "total_order_count": int(audit.get("total_order_count") or 0),
        "scoped_order_count": int(audit.get("scoped_order_count") or 0),
        "legacy_ignored_count": int(audit.get("legacy_ignored_count") or 0),
        "order_scope": dict(audit.get("order_scope") or {}),
        "missing_by_column": dict(audit.get("missing_by_column") or {}),
        "signal_applicability": dict(audit.get("signal_applicability") or {}),
        "missing_columns": list(audit.get("missing_columns") or []),
        "sample_orders": list(audit.get("sample_orders") or [])[:max_items],
        "replay_sample_count": int(audit.get("replay_sample_count") or 0),
        "replay_samples": list(audit.get("replay_samples") or [])[:max_items],
        "blockers": list(audit.get("blockers") or [])[:max_items],
        "warnings": list(audit.get("warnings") or [])[:max_items],
        "guards": dict(audit.get("guards") or {}),
        "next_boundary": audit.get("next_boundary"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-replay-samples", type=int, default=0)
    parser.add_argument("--since-hours", type=float, default=24.0 * 14.0)
    parser.add_argument("--ignore-legacy-orders", action="store_true")
    parser.add_argument("--order-since-hours", type=float, default=0.0)
    parser.add_argument("--ignore-orders-before", default="")
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    audit = build_audit(
        args.db,
        limit=int(args.limit),
        max_replay_samples=int(args.max_replay_samples),
        since_hours=float(args.since_hours),
        ignore_legacy_orders=bool(args.ignore_legacy_orders),
        order_since_hours=float(args.order_since_hours),
        ignore_orders_before=str(args.ignore_orders_before or ""),
    )
    payload: Mapping[str, Any] = (
        compact_audit(audit, max_items_output=int(args.max_items_output)) if args.summary else audit
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if audit.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
