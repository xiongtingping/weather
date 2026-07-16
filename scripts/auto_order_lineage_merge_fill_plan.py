#!/usr/bin/env python3
"""Read-only merge-fill plan for auto_orders decision/signal lineage.

This plan is intentionally narrower than production identity merge-fill. It
does not infer lineage from market similarity, timestamps, or prices. A
candidate is safe only when an existing structured anchor points at the exact
order row.
"""

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

from lineage_identity_runtime import lineage_id_from_parts  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "auto_order_lineage_merge_fill_plan_v1"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_auto_orders": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_signal_to_order_timing": False,
        "writes_manifest_file": False,
        "executes_sql_writes": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "fetches_orderbook": False,
        "places_orders": False,
        "restarts_launchd": False,
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


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


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
        return " AND created_at >= ?", [cutoff], scope
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
    return " AND created_at >= ?", [cutoff], scope


def _is_empty(value: Any) -> bool:
    return _text(value) == ""


def _json_map(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _nested_timing(details: Mapping[str, Any]) -> dict[str, Any]:
    value = details.get("signal_to_order_timing")
    return dict(value) if isinstance(value, Mapping) else {}


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _hard_source_signal_expected(details: Mapping[str, Any]) -> bool:
    timing = _nested_timing(details)
    if timing.get("signal_id") or timing.get("awc_hard_source_signal_at"):
        return True
    if _truthy(details.get("hard_source_direct")) or details.get("awc_hard_source_signal_candidate"):
        return True
    hard = details.get("hard_source_direct")
    if isinstance(hard, Mapping):
        nested_timing = _nested_timing(hard)
        return bool(nested_timing.get("signal_id") or nested_timing.get("awc_hard_source_signal_at"))
    return False


def _first_text(*mappings: Mapping[str, Any], keys: Sequence[str]) -> str:
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            continue
        for key in keys:
            value = _text(mapping.get(key))
            if value:
                return value
    return ""


def _has_index_on_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        index_name = str(row["name"] or "")
        for info in conn.execute(f"PRAGMA index_info({index_name})").fetchall():
            if str(info["name"] or "") == column:
                return True
    return False


def _has_decision_order_id_expression_index(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "auto_trade_decisions"):
        return False
    for row in conn.execute("PRAGMA index_list(auto_trade_decisions)").fetchall():
        index_name = str(row["name"] or "")
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        sql = str(sql_row["sql"] or "") if sql_row else ""
        if "json_extract(details_json" in sql and "$.order_id" in sql:
            return True
    return False


def _order_rows(
    conn: sqlite3.Connection,
    *,
    limit: int,
    scope_clause: str = "",
    scope_params: Sequence[Any] = (),
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            id, order_id, correlation_id, decision_id, signal_id, order_intent_id,
            slug, event_date, outcome, side, action, created_at
        FROM auto_orders
        WHERE (
            decision_id IS NULL OR trim(CAST(decision_id AS TEXT)) = ''
            OR signal_id IS NULL OR trim(CAST(signal_id AS TEXT)) = ''
        )
        """
        + str(scope_clause or "")
        + """
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(scope_params) + (max(1, int(limit)),),
    ).fetchall()


def _missing_order_count(
    conn: sqlite3.Connection,
    *,
    scope_clause: str = "",
    scope_params: Sequence[Any] = (),
) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM auto_orders
            WHERE (
                decision_id IS NULL OR trim(CAST(decision_id AS TEXT)) = ''
                OR signal_id IS NULL OR trim(CAST(signal_id AS TEXT)) = ''
            )
            """
            + str(scope_clause or ""),
            tuple(scope_params),
        ).fetchone()[0]
        or 0
    )


def _total_order_count(
    conn: sqlite3.Connection,
    *,
    scope_clause: str = "",
    scope_params: Sequence[Any] = (),
) -> int:
    where = ""
    params: tuple[Any, ...] = ()
    if scope_clause:
        text = str(scope_clause).strip()
        where = " WHERE " + text[4:].strip() if text.upper().startswith("AND ") else text
        params = tuple(scope_params)
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM auto_orders
            {where}
            """
            ,
            params,
        ).fetchone()[0]
        or 0
    )


def _decision_identity(row: sqlite3.Row) -> dict[str, str]:
    details = _json_map(row["details_json"] if "details_json" in row.keys() else "")
    timing = _nested_timing(details)
    decision_id = _text(row["decision_id"]) or _first_text(details, timing, keys=("decision_id",))
    if not decision_id:
        decision_id = lineage_id_from_parts(
            "decision_id",
            {"table": "auto_trade_decisions", "id": row["id"]},
        )
    return {
        "decision_id": decision_id,
        "signal_id": _text(row["signal_id"]) or _first_text(details, timing, keys=("signal_id",)),
        "order_intent_id": _text(row["order_intent_id"]) or _first_text(details, timing, keys=("order_intent_id",)),
        "correlation_id": _text(row["correlation_id"]) or _first_text(details, timing, keys=("correlation_id",)),
        "source_decision_row_id": _text(row["id"]),
        "hard_source_signal_expected": "true" if _hard_source_signal_expected(details) else "",
    }


def _resolve_from_decision_order_id(
    conn: sqlite3.Connection,
    order: sqlite3.Row,
    *,
    enabled: bool,
    lookup_enabled: bool,
) -> tuple[dict[str, str], list[str], list[dict[str, Any]]]:
    if not lookup_enabled:
        return {}, ["decision_order_id_lookup_disabled"], []
    if not enabled:
        return {}, ["decision_order_id_expression_index_missing"], []
    order_id = _text(order["order_id"])
    if not order_id:
        return {}, ["order_id_missing"], []
    rows = conn.execute(
        """
        SELECT id, decision_id, signal_id, order_intent_id, correlation_id, details_json,
               slug, event_date, outcome, side, action, created_at
        FROM auto_trade_decisions
        WHERE json_extract(details_json, '$.order_id') = ?
        ORDER BY id DESC
        LIMIT 5
        """,
        (order_id,),
    ).fetchall()
    evidence = [
        {
            "table": "auto_trade_decisions",
            "id": row["id"],
            "source": "details_json.order_id",
            "decision_id": _text(row["decision_id"]),
            "signal_id": _text(row["signal_id"]),
            "order_intent_id": _text(row["order_intent_id"]),
            "created_at": row["created_at"] if "created_at" in row.keys() else "",
            "hard_source_signal_expected": _hard_source_signal_expected(
                _json_map(row["details_json"] if "details_json" in row.keys() else "")
            ),
        }
        for row in rows
    ]
    if not rows:
        return {}, ["decision_order_id_anchor_missing"], evidence
    if len(rows) > 1:
        return {}, ["decision_order_id_anchor_ambiguous"], evidence
    identity = _decision_identity(rows[0])
    if not identity.get("decision_id"):
        return {}, ["decision_order_id_anchor_missing_decision_id"], evidence
    return identity, [], evidence


def _row_identity(row: sqlite3.Row) -> dict[str, str]:
    return {
        "decision_id": _text(row["decision_id"]) if "decision_id" in row.keys() else "",
        "signal_id": _text(row["signal_id"]) if "signal_id" in row.keys() else "",
        "order_intent_id": _text(row["order_intent_id"]) if "order_intent_id" in row.keys() else "",
        "correlation_id": _text(row["correlation_id"]) if "correlation_id" in row.keys() else "",
        "source_decision_row_id": "",
    }


def _resolve_from_job_intent(
    conn: sqlite3.Connection,
    order: sqlite3.Row,
    *,
    enabled: bool,
) -> tuple[dict[str, str], list[str], list[dict[str, Any]]]:
    if not enabled:
        return {}, ["job_order_intent_index_missing"], []
    intent = _text(order["order_intent_id"])
    if not intent:
        return {}, ["order_intent_id_missing_for_job_anchor"], []
    rows = conn.execute(
        """
        SELECT id, decision_id, signal_id, order_intent_id, correlation_id, source, state, created_at
        FROM auto_trade_jobs
        WHERE order_intent_id = ?
          AND order_intent_id IS NOT NULL
          AND order_intent_id <> ''
        ORDER BY id DESC
        LIMIT 5
        """,
        (intent,),
    ).fetchall()
    evidence = [
        {
            "table": "auto_trade_jobs",
            "id": row["id"],
            "source": "order_intent_id",
            "decision_id": _text(row["decision_id"]),
            "signal_id": _text(row["signal_id"]),
            "order_intent_id": _text(row["order_intent_id"]),
            "state": row["state"] if "state" in row.keys() else "",
        }
        for row in rows
    ]
    if not rows:
        return {}, ["job_order_intent_anchor_missing"], evidence
    if len(rows) > 1:
        return {}, ["job_order_intent_anchor_ambiguous"], evidence
    identity = _row_identity(rows[0])
    if _text(rows[0]["source"]) == "hard_source_direct":
        identity["hard_source_signal_expected"] = "true"
    if not identity.get("decision_id") and not identity.get("signal_id"):
        return {}, ["job_order_intent_anchor_has_no_decision_or_signal"], evidence
    return identity, [], evidence


def _resolve_from_timing_intent(
    conn: sqlite3.Connection,
    order: sqlite3.Row,
    *,
    table_small_enough: bool,
) -> tuple[dict[str, str], list[str], list[dict[str, Any]]]:
    if not table_small_enough:
        return {}, ["signal_timing_scan_not_allowed_without_index"], []
    intent = _text(order["order_intent_id"])
    if not intent:
        return {}, ["order_intent_id_missing_for_timing_anchor"], []
    rows = conn.execute(
        """
        SELECT signal_id, decision_id, order_intent_id, created_at
        FROM signal_to_order_timing
        WHERE order_intent_id = ?
        ORDER BY created_at DESC
        LIMIT 5
        """,
        (intent,),
    ).fetchall()
    evidence = [
        {
            "table": "signal_to_order_timing",
            "source": "order_intent_id",
            "signal_id": _text(row["signal_id"]),
            "decision_id": _text(row["decision_id"]),
            "order_intent_id": _text(row["order_intent_id"]),
            "created_at": row["created_at"] if "created_at" in row.keys() else "",
        }
        for row in rows
    ]
    if not rows:
        return {}, ["signal_timing_order_intent_anchor_missing"], evidence
    if len(rows) > 1:
        return {}, ["signal_timing_order_intent_anchor_ambiguous"], evidence
    identity = _row_identity(rows[0])
    identity["hard_source_signal_expected"] = "true"
    if not identity.get("decision_id") and not identity.get("signal_id"):
        return {}, ["signal_timing_anchor_has_no_decision_or_signal"], evidence
    return identity, [], evidence


def _compatible(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    for key in ("decision_id", "signal_id", "order_intent_id"):
        if left.get(key) and right.get(key) and left.get(key) != right.get(key):
            return False
    return True


def _merge_identity(base: dict[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    out = dict(base)
    for key, value in extra.items():
        if value and not out.get(key):
            out[key] = value
    return out


def _operation_for_order(
    conn: sqlite3.Connection,
    order: sqlite3.Row,
    *,
    decision_order_id_index: bool,
    enable_decision_order_id_lookup: bool,
    job_intent_index: bool,
    timing_small_enough: bool,
) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    blockers: list[str] = []
    source_diagnostics: list[str] = []
    identity: dict[str, str] = {}
    source_chain: list[str] = []

    resolvers = (
        (
            "auto_trade_decisions.details_json.order_id",
            _resolve_from_decision_order_id(
                conn,
                order,
                enabled=decision_order_id_index,
                lookup_enabled=enable_decision_order_id_lookup,
            ),
        ),
        ("auto_trade_jobs.order_intent_id", _resolve_from_job_intent(conn, order, enabled=job_intent_index)),
        ("signal_to_order_timing.order_intent_id", _resolve_from_timing_intent(conn, order, table_small_enough=timing_small_enough)),
    )
    for source, (candidate, source_blockers, source_evidence) in resolvers:
        evidence.extend(source_evidence)
        if candidate:
            if identity and not _compatible(identity, candidate):
                blockers.append(f"conflicting_identity_source:{source}")
                continue
            identity = _merge_identity(identity, candidate)
            source_chain.append(source)
        else:
            source_diagnostics.extend(f"{source}:{item}" for item in source_blockers)
            blockers.extend(
                f"{source}:{item}"
                for item in source_blockers
                if "ambiguous" in str(item)
            )

    set_fields: dict[str, str] = {}
    if _is_empty(order["decision_id"]) and identity.get("decision_id"):
        set_fields["decision_id"] = identity["decision_id"]
    if _is_empty(order["signal_id"]) and identity.get("signal_id"):
        set_fields["signal_id"] = identity["signal_id"]

    required_missing: list[str] = []
    if _is_empty(order["decision_id"]) and "decision_id" not in set_fields:
        required_missing.append("decision_id")
    hard_source_signal_expected = identity.get("hard_source_signal_expected") == "true"
    if _is_empty(order["signal_id"]) and "signal_id" not in set_fields and hard_source_signal_expected:
        required_missing.append("signal_id")
    if required_missing:
        blockers.append("strong_lineage_source_missing_required_fields:" + ",".join(required_missing))
    if not source_chain:
        blockers.append("no_strong_lineage_source")

    return {
        "table": "auto_orders",
        "primary_key": {"id": order["id"]},
        "natural_key": {
            "order_id": order["order_id"],
            "order_intent_id": order["order_intent_id"],
            "correlation_id": order["correlation_id"],
            "slug": order["slug"],
            "event_date": order["event_date"],
            "outcome": order["outcome"],
            "side": order["side"],
            "action": order["action"],
            "created_at": order["created_at"],
        },
        "set_fields": set_fields,
        "source": " / ".join(source_chain) if source_chain else "",
        "evidence": evidence,
        "source_diagnostics": sorted(set(source_diagnostics)),
        "blockers": sorted(set(blockers)),
    }


def build_plan(
    db_path: Path | str = DEFAULT_DB,
    *,
    limit: int = 1000000,
    max_items_output: int = 20,
    max_small_table_scan_rows: int = 5000,
    resolve_candidates: bool = False,
    enable_decision_order_id_lookup: bool = False,
    ignore_legacy_orders: bool = False,
    order_since_hours: float = 0.0,
    ignore_orders_before: str = "",
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    blockers: list[str] = []
    warnings: list[str] = []
    operations: list[dict[str, Any]] = []
    missing_order_count = 0
    total_order_count = 0
    scoped_order_count = 0
    legacy_ignored_count = 0
    candidate_scope_truncated = False
    index_contract: dict[str, Any] = {}
    order_scope: dict[str, Any] = {
        "ignore_legacy_orders": bool(ignore_legacy_orders),
        "order_since_hours": float(order_since_hours),
        "ignore_orders_before": str(ignore_orders_before or "").strip(),
        "scope_filter_applied": False,
    }
    timing_count = 0

    if not db.exists():
        blockers.append("archive_db_missing")
    else:
        with _connect(db) as conn:
            for table in ("auto_orders", "auto_trade_decisions", "auto_trade_jobs"):
                if not _table_exists(conn, table):
                    blockers.append(f"{table}_table_missing")
            if not blockers:
                order_columns = _columns(conn, "auto_orders")
                required_order_columns = {"id", "order_id", "decision_id", "signal_id", "order_intent_id", "correlation_id"}
                missing_columns = sorted(required_order_columns - order_columns)
                if missing_columns:
                    blockers.append("auto_orders_required_columns_missing:" + ",".join(missing_columns))
                scope_clause, scope_params, order_scope = _scope_clause(
                    order_columns,
                    ignore_legacy_orders=bool(ignore_legacy_orders),
                    order_since_hours=float(order_since_hours),
                    ignore_orders_before=str(ignore_orders_before or ""),
                )
                total_order_count = _total_order_count(conn)
                scoped_order_count = _total_order_count(
                    conn,
                    scope_clause=scope_clause,
                    scope_params=scope_params,
                )
                legacy_ignored_count = max(0, total_order_count - scoped_order_count)
                order_scope["total_order_count"] = total_order_count
                order_scope["scoped_order_count"] = scoped_order_count
                order_scope["legacy_ignored_count"] = legacy_ignored_count
                decision_order_id_index = _has_decision_order_id_expression_index(conn)
                job_intent_index = _has_index_on_column(conn, "auto_trade_jobs", "order_intent_id")
                timing_exists = _table_exists(conn, "signal_to_order_timing")
                timing_count = (
                    int(conn.execute("SELECT COUNT(*) FROM signal_to_order_timing").fetchone()[0] or 0)
                    if timing_exists
                    else 0
                )
                timing_small_enough = timing_exists and timing_count <= max(0, int(max_small_table_scan_rows))
                index_contract = {
                    "decision_order_id_expression_index": decision_order_id_index,
                    "decision_order_id_lookup_enabled": bool(enable_decision_order_id_lookup),
                    "job_order_intent_id_index": job_intent_index,
                    "signal_to_order_timing_exists": timing_exists,
                    "signal_to_order_timing_row_count": timing_count,
                    "signal_to_order_timing_small_scan_allowed": timing_small_enough,
                    "max_small_table_scan_rows": int(max_small_table_scan_rows),
                }
                if not decision_order_id_index:
                    warnings.append("decision_order_id_expression_index_missing_limits_repair")
                if not job_intent_index:
                    warnings.append("job_order_intent_id_index_missing_limits_repair")
                if timing_exists and not timing_small_enough:
                    warnings.append("signal_to_order_timing_too_large_without_order_intent_index")
                if not missing_columns:
                    missing_order_count = _missing_order_count(
                        conn,
                        scope_clause=scope_clause,
                        scope_params=scope_params,
                    )
                    if not resolve_candidates and missing_order_count:
                        blockers.append("candidate_resolution_disabled")
                    if resolve_candidates:
                        orders = _order_rows(
                            conn,
                            limit=int(limit),
                            scope_clause=scope_clause,
                            scope_params=scope_params,
                        )
                        candidate_scope_truncated = len(orders) < missing_order_count
                        if candidate_scope_truncated:
                            blockers.append("candidate_scope_truncated")
                        for order in orders:
                            operations.append(
                                _operation_for_order(
                                    conn,
                                    order,
                                    decision_order_id_index=decision_order_id_index,
                                    enable_decision_order_id_lookup=bool(enable_decision_order_id_lookup),
                                    job_intent_index=job_intent_index,
                                    timing_small_enough=timing_small_enough,
                                )
                            )

    safe_operations = [
        item
        for item in operations
        if not [blocker for blocker in item.get("blockers") or [] if str(blocker).strip()]
        and item.get("set_fields")
    ]
    blocked_operations = [
        item for item in operations if [blocker for blocker in item.get("blockers") or [] if str(blocker).strip()]
    ]
    if blocked_operations:
        blockers.append("blocked_auto_order_lineage_candidates_present")
    if missing_order_count and not safe_operations and not blocked_operations:
        blockers.append("auto_order_lineage_candidates_missing")
    needs_apply = missing_order_count > 0
    safe_to_apply_all = bool(needs_apply and not blockers and len(safe_operations) == missing_order_count)

    return {
        "schema": SCHEMA_VERSION,
        "ok": bool((not needs_apply) or safe_to_apply_all),
        "read_only": True,
        "dry_run": True,
        "db": str(db),
        "total_order_count": total_order_count,
        "scoped_order_count": scoped_order_count,
        "legacy_ignored_count": legacy_ignored_count,
        "order_scope": order_scope,
        "missing_order_count": missing_order_count,
        "candidate_scope_truncated": candidate_scope_truncated,
        "candidate_count": len(operations),
        "safe_candidate_count": len(safe_operations),
        "blocked_candidate_count": len(blocked_operations),
        "needs_apply": needs_apply,
        "candidate_resolution_enabled": bool(resolve_candidates),
        "safe_to_apply_all_candidates": safe_to_apply_all,
        "index_contract": index_contract,
        "candidate_samples": operations[: max(0, int(max_items_output))],
        "candidate_operations": operations,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "guards": _guards(),
        "next_boundary": (
            "auto_order_lineage_merge_fill_not_required"
            if not needs_apply
            else "run_controlled_auto_order_lineage_merge_fill_apply_after_contract"
            if safe_to_apply_all
            else "review_or_extend_strong_order_lineage_sources_before_apply"
        ),
    }


def compact_plan(plan: Mapping[str, Any], *, max_items_output: int = 8) -> dict[str, Any]:
    max_items = max(0, int(max_items_output))
    return {
        "schema": SCHEMA_VERSION + "_summary",
        "source_schema": plan.get("schema"),
        "ok": bool(plan.get("ok")),
        "read_only": bool(plan.get("read_only")),
        "dry_run": bool(plan.get("dry_run")),
        "db": plan.get("db"),
        "total_order_count": int(plan.get("total_order_count") or 0),
        "scoped_order_count": int(plan.get("scoped_order_count") or 0),
        "legacy_ignored_count": int(plan.get("legacy_ignored_count") or 0),
        "order_scope": dict(_as_mapping(plan.get("order_scope"))),
        "missing_order_count": int(plan.get("missing_order_count") or 0),
        "candidate_scope_truncated": bool(plan.get("candidate_scope_truncated")),
        "candidate_count": int(plan.get("candidate_count") or 0),
        "safe_candidate_count": int(plan.get("safe_candidate_count") or 0),
        "blocked_candidate_count": int(plan.get("blocked_candidate_count") or 0),
        "needs_apply": bool(plan.get("needs_apply")),
        "candidate_resolution_enabled": bool(plan.get("candidate_resolution_enabled")),
        "safe_to_apply_all_candidates": bool(plan.get("safe_to_apply_all_candidates")),
        "index_contract": dict(plan.get("index_contract") or {}),
        "candidate_samples": list(plan.get("candidate_samples") or [])[:max_items],
        "blockers": list(plan.get("blockers") or [])[:max_items],
        "warnings": list(plan.get("warnings") or [])[:max_items],
        "guards": dict(plan.get("guards") or {}),
        "next_boundary": plan.get("next_boundary"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=1000000)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--max-small-table-scan-rows", type=int, default=5000)
    parser.add_argument(
        "--resolve-candidates",
        action="store_true",
        help="Resolve row-level candidates. Default reports coverage/index contract only for live DB safety.",
    )
    parser.add_argument(
        "--enable-decision-order-id-lookup",
        action="store_true",
        help="Opt in to indexed auto_trade_decisions details_json.order_id lookups; default stays off for live archive safety.",
    )
    parser.add_argument("--ignore-legacy-orders", action="store_true")
    parser.add_argument("--order-since-hours", type=float, default=0.0)
    parser.add_argument("--ignore-orders-before", default="")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    plan = build_plan(
        args.db,
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        max_small_table_scan_rows=int(args.max_small_table_scan_rows),
        resolve_candidates=bool(args.resolve_candidates),
        enable_decision_order_id_lookup=bool(args.enable_decision_order_id_lookup),
        ignore_legacy_orders=bool(args.ignore_legacy_orders),
        order_since_hours=float(args.order_since_hours),
        ignore_orders_before=str(args.ignore_orders_before or ""),
    )
    payload: Mapping[str, Any] = compact_plan(plan, max_items_output=int(args.max_items_output)) if args.summary else plan
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if plan.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
