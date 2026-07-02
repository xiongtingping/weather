#!/usr/bin/env python3
"""Read-only counterfactual review for entry decisions.

The report answers four production questions:
- did bought candidates later win?
- did rejected candidates later win?
- did size-down candidates deserve larger size?
- did low-price buckets have an executable exit?
- did false-edge filters block candidates that later settled as winners?

It uses candidate-level de-duplication to avoid overweighting repeated scanner
decisions for the same market/outcome/side/strategy.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "archive.db"
ET_ZONE = ZoneInfo("America/New_York") if ZoneInfo is not None else timezone.utc
PENALTY_KEYS = (
    "entry_usable_edge_price_penalty",
    "entry_usable_edge_liquidity_penalty",
    "entry_usable_edge_stale_penalty",
    "entry_usable_edge_station_penalty",
    "entry_usable_edge_exit_penalty",
    "entry_usable_edge_churn_penalty",
)
REPRICING_EVIDENCE_COMPONENTS = ("p_converge", "p_fill_entry", "p_fill_exit")
BREAKDOWN_METRICS = (
    "entry_usable_edge",
    "exec_edge_entry",
    "edge_tradeability_profit_value",
    "edge_tradeability_edge_lcb",
    "edge_realizable",
    "edge_realizable_score",
    "q_entry",
    "q_quote",
    "ask",
    "bid",
    "spread",
    "order_price",
    "q_minus_ask",
    "q_minus_order_price",
    "q_quote_minus_ask",
)
FEEDBACK_SNAPSHOT_KIND = "entry_counterfactual_review"
REPRICING_PROBABILITY_LOW_THRESHOLD = 0.05
FAST_ENTRY_ROW_WINDOW_DAYS = 2
FAST_ENTRY_JSON_KEYS = (
    "entry_strategy_family",
    "strategy_variant",
    "weather_strategy_kind",
    "strategy",
    "dry_run",
    "entry_soft_reason_codes",
    "entry_usable_edge",
    "entry_usable_edge_na_reason",
    "entry_usable_edge_size_scale_applied",
    "entry_usable_edge_size_scale",
    "entry_soft_no_order",
    "entry_usable_edge_low_price_bucket",
    "entry_edge_false_edge",
    "entry_edge_false_edge_type",
    "entry_edge_root_cause",
    "source_fit_na_reason",
    "source_na_reason",
    "weather_strategy_source_na_reason",
    "observed_nowcast_source_na_reason",
    "order_id",
    "auto_order_id",
    "entry_order_id",
)


def _configure_read_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA query_only=ON")


def _db_read_error_report(db_path: Path, days: int, exc: sqlite3.Error) -> Dict[str, Any]:
    start_utc, end_utc, start_et, end_et = _window_for_days(days)
    error = f"{type(exc).__name__}:{exc}"
    return {
        "ok": False,
        "db_path": str(db_path),
        "error": error,
        "window": {
            "days": int(days),
            "start_utc": start_utc,
            "end_utc": end_utc,
            "start_et": start_et,
            "end_et": end_et,
        },
        "scope": "archive_db_read_error",
        "overall": {},
        "by_strategy": {},
        "strategy_kpis": {"overall": {}, "by_strategy": {}, "rows": []},
        "counterfactual_answers": {"overall": {}, "by_strategy": {}},
        "actionable_summary": {
            "next_mainline_task": "database_health_check",
            "overall": {
                "primary_diagnosis": "archive_db_read_error",
                "diagnostic_flags": ["archive_db_read_error"],
            },
        },
        "current_runtime": {},
        "examples": {},
    }


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _unique_event_keys(keys: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for slug, event_date in keys:
        key = (
            str(slug or "").strip(),
            str(event_date or "").split("T", 1)[0].strip(),
        )
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(key)
    out.sort()
    return out


def _chunked(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    step = max(1, int(size))
    for idx in range(0, len(values), step):
        yield values[idx : idx + step]


def _values_cte(rows: Sequence[Sequence[Any]], arity: int) -> Tuple[str, List[Any]]:
    placeholder = "(" + ", ".join("?" for _ in range(arity)) + ")"
    values_sql = ", ".join(placeholder for _ in rows)
    params: List[Any] = []
    for row in rows:
        params.extend(list(row)[:arity])
    return values_sql, params


def _date_from_iso(value: str) -> str:
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except Exception:
        return str(value or "").split("T", 1)[0]


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
        }
    except sqlite3.OperationalError:
        return set()


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _row_int(row: Any, key: str, default: int = 0) -> int:
    try:
        return int(_row_get(row, key, default) or 0)
    except Exception:
        return int(default)


def _row_bool_override(row: Any, key: str) -> Optional[bool]:
    value = _row_get(row, key, None)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return int(value) != 0
    except Exception:
        return _boolish(value)


def _fetch_decision_rows_by_id(
    conn: sqlite3.Connection,
    ids: Iterable[int],
) -> Dict[int, sqlite3.Row]:
    unique_ids = sorted({int(row_id) for row_id in ids if int(row_id or 0) > 0})
    out: Dict[int, sqlite3.Row] = {}
    for chunk in _chunked(unique_ids, 800):
        placeholders = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                f"""
                SELECT id, slug, event_date, outcome, side, will_order,
                       decided_state, reason_codes_json, details_json, created_at
                FROM auto_trade_decisions
                WHERE id IN ({placeholders})
                """,
                list(chunk),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            out[int(row["id"] or 0)] = row
    return out


def _merged_reason_codes(raw: Any) -> List[str]:
    out: List[str] = []
    for part in str(raw or "").split("\n"):
        for code in _safe_json_list(part):
            if code and code not in out:
                out.append(code)
    return out


def _fast_entry_window_enabled(
    conn: sqlite3.Connection,
    start_et: str,
    end_et: str,
) -> bool:
    if "idx_auto_trade_decisions_run_action_created" not in _index_names(
        conn, "auto_trade_decisions"
    ):
        return False
    try:
        start_date = datetime.fromisoformat(start_et).date()
        end_date = datetime.fromisoformat(end_et).date()
    except Exception:
        return False
    return (end_date - start_date).days + 1 > FAST_ENTRY_ROW_WINDOW_DAYS


def _load_entry_decision_rows_fast(
    conn: sqlite3.Connection,
    start_utc: str,
    end_utc: str,
    start_et: str,
    end_et: str,
) -> List[Mapping[str, Any]]:
    start_date = (
        datetime.fromisoformat(start_et).date() - timedelta(days=1)
    ).isoformat()
    end_date = (
        datetime.fromisoformat(end_et).date() + timedelta(days=1)
    ).isoformat()
    groups: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    scan_rows = conn.execute(
        """
        SELECT
          id,
          slug,
          substr(event_date, 1, 10) AS event_date,
          outcome,
          upper(side) AS side,
          will_order,
          decided_state,
          reason_codes_json,
          created_at
        FROM auto_trade_decisions INDEXED BY idx_auto_trade_decisions_run_action_created
        WHERE action = 'entry'
          AND run_date >= ?
          AND run_date <= ?
          AND created_at >= ?
          AND created_at <= ?
        """,
        (start_date, end_date, start_utc, end_utc),
    )
    for row in scan_rows:
        slug = str(row["slug"] or "").strip()
        event_date = str(row["event_date"] or "").strip()
        outcome = str(row["outcome"] or "").strip()
        side = str(row["side"] or "").strip().upper()
        if not slug or not event_date or not outcome or side not in {"YES", "NO"}:
            continue
        key = (slug, event_date, outcome, side)
        item = groups.get(key)
        row_id = int(row["id"] or 0)
        created_at = str(row["created_at"] or "")
        if item is None:
            item = {
                "slug": slug,
                "event_date": event_date,
                "outcome": outcome,
                "side": side,
                "raw_decision_count": 0,
                "will_order_any": 0,
                "rejected_any": 0,
                "dry_run_state_any": 0,
                "first_created_at": created_at,
                "latest_created_at": created_at,
                "latest_id": row_id,
                "reason_codes": set(),
            }
            groups[key] = item
        item["raw_decision_count"] += 1
        will_order = int(row["will_order"] or 0) != 0
        if will_order:
            item["will_order_any"] = 1
        else:
            item["rejected_any"] = 1
        if will_order and "dry_run" in str(row["decided_state"] or "").strip().lower():
            item["dry_run_state_any"] = 1
        if created_at < str(item.get("first_created_at") or created_at):
            item["first_created_at"] = created_at
        if row_id >= int(item.get("latest_id") or 0):
            item["latest_id"] = row_id
            item["latest_created_at"] = created_at
        for code in _safe_json_list(row["reason_codes_json"]):
            item["reason_codes"].add(code)
    group_rows = sorted(groups.values(), key=lambda item: int(item.get("latest_id") or 0))
    details_by_id = _fetch_decision_rows_by_id(
        conn,
        [int(row["latest_id"] or 0) for row in group_rows],
    )
    out: List[Mapping[str, Any]] = []
    for group in group_rows:
        latest_id = int(group["latest_id"] or 0)
        detail_row = details_by_id.get(latest_id)
        if detail_row is None:
            continue
        details = _safe_json_dict(detail_row["details_json"])
        latest_will_order = int(detail_row["will_order"] or 0) != 0
        latest_decided_state = str(detail_row["decided_state"] or "")
        dry_run_intent = bool(
            int(group["dry_run_state_any"] or 0) != 0
            or (latest_will_order and "dry_run" in latest_decided_state.lower())
            or _boolish(details.get("dry_run"))
        )
        out.append(
            {
                "id": latest_id,
                "slug": str(group["slug"] or "").strip(),
                "event_date": str(group["event_date"] or "").strip(),
                "outcome": str(group["outcome"] or "").strip(),
                "side": str(group["side"] or "").strip().upper(),
                "will_order": 1 if latest_will_order else 0,
                "will_order_any": int(group["will_order_any"] or 0),
                "latest_will_order": 1 if latest_will_order else 0,
                "rejected": int(group["rejected_any"] or 0),
                "dry_run_intent": 1 if dry_run_intent else 0,
                "decided_state": latest_decided_state,
                "reason_codes_json": json.dumps(sorted(group.get("reason_codes") or []), ensure_ascii=False),
                "latest_reason_codes_json": str(detail_row["reason_codes_json"] or "[]"),
                "details_json": json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                "created_at": str(group["latest_created_at"] or detail_row["created_at"] or ""),
                "raw_decision_count": int(group["raw_decision_count"] or 0),
                "counterfactual_row_mode": "candidate_fast",
            }
        )
    out.sort(key=lambda row: int(row.get("id") or 0))
    return out


def _load_entry_decision_rows(
    conn: sqlite3.Connection,
    start_utc: str,
    end_utc: str,
    start_et: str,
    end_et: str,
) -> List[sqlite3.Row]:
    if _fast_entry_window_enabled(conn, start_et, end_et):
        return list(_load_entry_decision_rows_fast(conn, start_utc, end_utc, start_et, end_et))
    select_sql = """
            SELECT id, slug, event_date, outcome, side, will_order,
                   decided_state, reason_codes_json, details_json, created_at
            FROM auto_trade_decisions {index_hint}
            WHERE action = 'entry'
              {run_date_filter}
              AND created_at >= ?
              AND created_at <= ?
            """
    params: List[Any]
    if "idx_auto_trade_decisions_run_action_created" in _index_names(
        conn, "auto_trade_decisions"
    ):
        start_date = (
            datetime.fromisoformat(start_et).date() - timedelta(days=1)
        ).isoformat()
        end_date = (
            datetime.fromisoformat(end_et).date() + timedelta(days=1)
        ).isoformat()
        sql = select_sql.format(
            index_hint="INDEXED BY idx_auto_trade_decisions_run_action_created",
            run_date_filter="AND run_date >= ? AND run_date <= ?",
        )
        params = [start_date, end_date, start_utc, end_utc]
    else:
        sql = select_sql.format(index_hint="", run_date_filter="")
        params = [start_utc, end_utc]
    rows = list(conn.execute(sql, params).fetchall())
    rows.sort(key=lambda row: int(row["id"] or 0))
    return rows


def _safe_json_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_json_list(raw: Any) -> List[str]:
    if isinstance(raw, list):
        items = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        items = parsed if isinstance(parsed, list) else [text]
    out: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _split_counter_items(raw: Any) -> List[str]:
    out: List[str] = []
    for item in _safe_json_list(raw):
        for part in re.split(r"[,;|]", str(item or "")):
            text = part.strip()
            if text and text not in out:
                out.append(text)
    return out


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "ok", "pass"}


def _strategy_family(details: Mapping[str, Any]) -> str:
    for key in ("entry_strategy_family", "strategy_variant", "weather_strategy_kind", "strategy"):
        text = str(details.get(key) or "").strip().lower()
        if text:
            if text in {"main", "lcb_global", "lcb_global_main", "hard_source_direct"}:
                return "path_probability_follow"
            if text in {"yes_top", "consensus_yes_top"}:
                return "consensus_top_regional"
            return text
    return "unknown"


def _json_extract_expr(key: str) -> str:
    escaped = str(key).replace("'", "''")
    return f"json_extract(details_json, '$.{escaped}')"


def _strategy_family_sql_expr() -> str:
    return (
        "COALESCE("
        "NULLIF(json_extract(details_json, '$.entry_strategy_family'), ''),"
        "NULLIF(json_extract(details_json, '$.strategy_variant'), ''),"
        "NULLIF(json_extract(details_json, '$.weather_strategy_kind'), ''),"
        "NULLIF(json_extract(details_json, '$.strategy'), ''),"
        "'unknown')"
    )


def _strategy_family_from_text(value: Any) -> str:
    return _strategy_family({"entry_strategy_family": value})


def _fast_details_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    details: Dict[str, Any] = {}
    for key in FAST_ENTRY_JSON_KEYS:
        value = row[key] if key in row.keys() else None
        if value is None:
            continue
        if key == "entry_soft_reason_codes":
            parsed = _safe_json_list(value)
            if parsed:
                details[key] = parsed
        else:
            details[key] = value
    return details


STATION_MAPPING_MISSING_CODES = {
    "station_mapping_missing",
    "settlement_station_missing",
    "weather_station_missing",
}
CONTRACT_RULE_MISSING_CODES = {
    "contract_rule_missing",
    "rules_contract_missing",
    "rules_contract_incomplete",
    "weather_rules_contract_missing",
    "weather_rules_contract_incomplete",
}
RULE_HARD_INVARIANT_CODES = {
    "balance_insufficient",
    "bucket_exposure_cap",
    "clob_auth_missing",
    "clob_submit_failed",
    "contract_rule_missing",
    "duplicate_open_order",
    "event_exposure_cap",
    "invalid_token",
    "market_archived",
    "market_closed",
    "market_inactive",
    "market_not_accepting_orders",
    "market_not_ready",
    "missing_market",
    "no_wallet",
    "orderbook_missing",
    "outcome_token_missing",
    "position_cap",
    "private_key_missing",
    "risk_cap",
    "rules_contract_incomplete",
    "settlement_source_missing",
    "station_mapping_missing",
    "token_id_missing",
    "token_missing",
    "weather_adjacent_exposure_cap",
    "weather_rules_contract_incomplete",
}
RULE_OPERATIONAL_CODES = {
    "decision_persistence_failed",
    "event_worker_failed",
    "event_worker_timeout",
    "event_worker_timeout_inline_failed",
}
RULE_POSITION_RISK_CODES = {
    "already_in_entry",
    "bucket_loss_streak_blocked",
    "entry_stale_cancel_exit",
    "exit_flag",
    "other_side_active",
    "plain_lcb_reentry_cooldown",
    "recent_exit",
    "reentry_blocked",
    "residual_exit_lock",
    "residual_opposite_position",
    "retry_blocked",
}
RULE_ALPHA_POLICY_CODES = {
    "edge_lcb_below_min",
    "edge_realizable_low",
    "edge_realizable_missing",
    "edge_realizable_p_converge_below_min",
    "edge_realizable_p_realize_below_min",
    "edge_tradeability_blocked",
    "edge_tradeability_missing",
    "edge_tradeability_shadow_block",
    "entry_usable_edge_nonpositive",
    "ev_below_min",
    "plain_lcb_min_total_edge_below_min",
    "positive_ev_false",
    "probability_below_min",
    "signal_lcb_below_min",
}
RULE_STRATEGY_FILTER_CODES = {
    "legacy_entry_strategy_retired",
    "plain_lcb_pinned_tail_no_filtered",
    "skip_top",
    "strategy_disabled",
    "strategy_size_ratio_zero",
}
RULE_WRAPPER_CODES = {
    "__no_reason_code__",
    "auto_trade_entry_blocked",
    "entry_blocked",
    "entry_gate_blocked",
}
RULE_DIAGNOSTIC_OR_SORT_CODES = {
    "edge_realizable_sort_missing",
}
RULE_ORDER_INTENT_CODES = {
    "dry_run_order_submitted",
    "order_created",
    "order_submitted",
    "will_order",
}
RULE_SOFT_HAIRCUT_TERMS = (
    "ask",
    "calibration",
    "depth",
    "evidence",
    "lag",
    "liquidity",
    "low_sample",
    "market_tick_extreme_tail_skipped",
    "market_tick_stale",
    "no_by_yes",
    "overprice",
    "price",
    "q_below",
    "q_lcb",
    "q_raw",
    "sample",
    "spread",
    "stale",
    "target_exit_execution",
    "tick",
    "volume",
)
RULE_ATTRIBUTION_MIN_SETTLED_BLOCKED_TOTAL = 20
RULE_ATTRIBUTION_MIN_SETTLED_BLOCKED_PER_RULE = 5
RULE_PRUNING_REVIEW_FALSE_REJECT_RATE = 0.55
RULE_PRUNING_REVIEW_PROTECTIVE_TRUE_BLOCK_RATE = 0.65


def _source_fit_na_is_station_mapping_missing(reason: Any) -> bool:
    text = str(reason or "").strip().lower()
    return text in STATION_MAPPING_MISSING_CODES


def _contract_rule_is_missing(reason: Any) -> bool:
    text = str(reason or "").strip().lower()
    return text in CONTRACT_RULE_MISSING_CODES


def _rule_type_for_code(code: Any) -> str:
    text = str(code or "").strip().lower()
    if not text:
        return "unknown"
    if text in RULE_ORDER_INTENT_CODES:
        return "order_intent"
    if text in RULE_WRAPPER_CODES:
        return "wrapper_or_missing_reason"
    if text in RULE_HARD_INVARIANT_CODES:
        return "hard_invariant"
    if text in RULE_POSITION_RISK_CODES or "cooldown" in text or "loss_streak" in text:
        return "position_risk"
    if text in RULE_OPERATIONAL_CODES or text.startswith("event_worker_"):
        return "operational_failure"
    if text in RULE_STRATEGY_FILTER_CODES or text.startswith("plain_lcb_"):
        return "strategy_shape_filter"
    if text in RULE_DIAGNOSTIC_OR_SORT_CODES or text.endswith("_sort_missing"):
        return "diagnostic_or_sort_missing"
    if text in RULE_ALPHA_POLICY_CODES or "edge_tradeability" in text or "edge_lcb" in text:
        return "alpha_policy_gate"
    if any(term in text for term in RULE_SOFT_HAIRCUT_TERMS):
        return "soft_haircut"
    return "unknown"


def _rule_treatment(rule_type: str) -> str:
    if rule_type == "hard_invariant":
        return "keep_hard_block"
    if rule_type == "position_risk":
        return "keep_hard_or_size_down_until_position_state_is_clean"
    if rule_type == "operational_failure":
        return "fix_infrastructure_or_persistence_path"
    if rule_type == "alpha_policy_gate":
        return "review_alpha_gate_with_counterfactual_evidence"
    if rule_type == "soft_haircut":
        return "keep_as_haircut_or_tune_size_penalty"
    if rule_type == "strategy_shape_filter":
        return "keep_only_if_rule_attribution_is_protective"
    if rule_type == "wrapper_or_missing_reason":
        return "ignore_wrapper_or_add_specific_reason_code"
    if rule_type == "order_intent":
        return "ordered_or_dry_run_intent"
    if rule_type == "diagnostic_or_sort_missing":
        return "keep_out_of_hard_gate_or_repair_sort_diagnostic"
    return "needs_rule_mapping"


def _counterfactual_edge_from_breakdown(row: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    for key, source in (
        ("avg_entry_usable_edge", "entry_usable_edge"),
        ("avg_q_minus_order_price", "q_minus_order_price"),
        ("avg_q_minus_ask", "q_minus_ask"),
        ("avg_edge_tradeability_profit_value", "edge_tradeability_profit_value"),
    ):
        value = _to_float(row.get(key))
        if value is not None:
            return float(value), source
    return None, "missing"


def _rule_attribution_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    code = str(row.get("key") or "").strip() or "__no_reason_code__"
    candidate_count = int(row.get("candidate_count") or 0)
    will_order_count = int(row.get("will_order_count") or 0)
    blocked_count = max(0, candidate_count - will_order_count)
    settled_blocked_count = int(row.get("rejected_settled_count") or 0)
    later_winner_count = int(row.get("missed_winner_count") or row.get("rejected_hit_count") or 0)
    true_block_count = max(0, settled_blocked_count - later_winner_count)
    false_reject_rate = _rate(later_winner_count, settled_blocked_count)
    true_block_rate = _rate(true_block_count, settled_blocked_count)
    counterfactual_edge, edge_source = _counterfactual_edge_from_breakdown(row)
    rule_type = _rule_type_for_code(code)
    if rule_type == "order_intent":
        action = "no_reject_attribution_needed"
    elif settled_blocked_count <= 0:
        action = "collect_more_settled_counterfactual_samples"
    elif rule_type == "hard_invariant":
        action = "keep_hard_block"
    elif false_reject_rate is not None and false_reject_rate >= 0.55:
        action = "soften_or_prune_candidate"
    elif true_block_rate is not None and true_block_rate >= 0.65:
        action = "keep_rule_currently_protective"
    elif rule_type in {"soft_haircut", "strategy_shape_filter", "alpha_policy_gate"}:
        action = "review_threshold_or_haircut_strength"
    else:
        action = "collect_more_evidence"
    return {
        "rule_code": code,
        "rule_type": rule_type,
        "suggested_treatment": _rule_treatment(rule_type),
        "recommended_action": action,
        "candidate_count": candidate_count,
        "blocked_count": blocked_count,
        "settled_blocked_count": settled_blocked_count,
        "later_winner_count": later_winner_count,
        "true_block_count": true_block_count,
        "false_reject_rate": false_reject_rate,
        "true_block_rate": true_block_rate,
        "avg_counterfactual_edge": counterfactual_edge,
        "counterfactual_edge_source": edge_source,
        "avg_entry_usable_edge": row.get("avg_entry_usable_edge"),
        "avg_q_minus_order_price": row.get("avg_q_minus_order_price"),
        "avg_q_minus_ask": row.get("avg_q_minus_ask"),
    }


def _build_rule_attribution_from_stats(stats: Mapping[str, Any]) -> Dict[str, Any]:
    rows = [
        _rule_attribution_row(row)
        for row in stats.get("reason_code_breakdown") or []
        if isinstance(row, Mapping)
    ]
    rows.sort(
        key=lambda row: (
            -int(row.get("later_winner_count") or 0),
            -int(row.get("settled_blocked_count") or 0),
            -int(row.get("blocked_count") or 0),
            str(row.get("rule_code") or ""),
        )
    )
    type_counts: Dict[str, int] = {}
    for row in rows:
        rule_type = str(row.get("rule_type") or "unknown")
        type_counts[rule_type] = type_counts.get(rule_type, 0) + int(row.get("blocked_count") or 0)
    pruning_candidates = [
        row
        for row in rows
        if row.get("rule_type") != "hard_invariant"
        and int(row.get("settled_blocked_count") or 0) > 0
        and _to_float(row.get("false_reject_rate")) is not None
        and float(row["false_reject_rate"]) >= 0.55
    ]
    protective_rules = [
        row
        for row in rows
        if int(row.get("settled_blocked_count") or 0) > 0
        and _to_float(row.get("true_block_rate")) is not None
        and float(row["true_block_rate"]) >= 0.65
    ]
    settled_blocked_total = sum(int(row.get("settled_blocked_count") or 0) for row in rows)
    evaluable_rule_count = sum(
        1
        for row in rows
        if int(row.get("settled_blocked_count") or 0)
        >= RULE_ATTRIBUTION_MIN_SETTLED_BLOCKED_PER_RULE
    )
    sample_progress = {
        "settled_blocked_count": settled_blocked_total,
        "min_settled_blocked_total": RULE_ATTRIBUTION_MIN_SETTLED_BLOCKED_TOTAL,
        "min_settled_blocked_per_rule": RULE_ATTRIBUTION_MIN_SETTLED_BLOCKED_PER_RULE,
        "evaluable_rule_count": evaluable_rule_count,
        "ready_for_rule_pruning_review": bool(
            settled_blocked_total >= RULE_ATTRIBUTION_MIN_SETTLED_BLOCKED_TOTAL
            and evaluable_rule_count > 0
        ),
        "remaining_settled_blocked_needed": max(
            0,
            RULE_ATTRIBUTION_MIN_SETTLED_BLOCKED_TOTAL - settled_blocked_total,
        ),
    }
    sample_progress["next_mainline_task"] = (
        "rule_pruning_review_v1"
        if sample_progress["ready_for_rule_pruning_review"]
        else "settled_counterfactual_sample_accrual"
    )
    return {
        "rows": rows,
        "rule_type_blocked_counts": type_counts,
        "sample_progress": sample_progress,
        "pruning_candidates": pruning_candidates,
        "protective_rules": protective_rules,
        "field_contract": [
            "rule_code",
            "rule_type",
            "blocked_count",
            "settled_blocked_count",
            "later_winner_count",
            "false_reject_rate",
            "true_block_rate",
            "avg_counterfactual_edge",
            "recommended_action",
            "sample_progress",
        ],
    }


def _runtime_reason_code_counts(current_runtime: Optional[Mapping[str, Any]]) -> Dict[str, int]:
    runtime = current_runtime if isinstance(current_runtime, Mapping) else {}
    overall = runtime.get("overall") if isinstance(runtime.get("overall"), Mapping) else {}
    counts = overall.get("reason_code_counts") if isinstance(overall, Mapping) else None
    if not isinstance(counts, Mapping):
        return {}
    out: Dict[str, int] = {}
    for key, value in counts.items():
        code = str(key or "").strip()
        if not code:
            continue
        try:
            count = int(value or 0)
        except Exception:
            count = 0
        if count > 0:
            out[code] = count
    return out


def _rule_pruning_bucket(row: Mapping[str, Any], active_count: int) -> str:
    rule_type = str(row.get("rule_type") or "unknown")
    action = str(row.get("recommended_action") or "").strip()
    false_reject_rate = _to_float(row.get("false_reject_rate"))
    true_block_rate = _to_float(row.get("true_block_rate"))
    if rule_type == "order_intent":
        return "order_intent_ignore"
    if rule_type == "wrapper_or_missing_reason":
        return "wrapper_reason_cleanup" if active_count > 0 else "historical_wrapper_residual"
    if rule_type == "hard_invariant":
        return "keep_hard_invariant"
    if rule_type == "diagnostic_or_sort_missing":
        return "diagnostic_or_sort_review" if active_count > 0 else "historical_diagnostic_residual"
    if active_count <= 0 and action == "soften_or_prune_candidate":
        return "historical_false_reject_residual"
    if action == "soften_or_prune_candidate":
        return "active_prune_or_soften"
    if active_count <= 0 and action == "review_threshold_or_haircut_strength":
        return "historical_threshold_or_haircut_residual"
    if true_block_rate is not None and true_block_rate >= RULE_PRUNING_REVIEW_PROTECTIVE_TRUE_BLOCK_RATE:
        return "keep_protective"
    if false_reject_rate is not None and false_reject_rate >= RULE_PRUNING_REVIEW_FALSE_REJECT_RATE:
        return "review_prune_or_soften"
    if rule_type == "unknown":
        return "needs_rule_mapping"
    if action == "review_threshold_or_haircut_strength":
        return "threshold_or_haircut_review"
    return "monitor"


def _rule_review_item(row: Mapping[str, Any], active_count: int) -> Dict[str, Any]:
    item = dict(row)
    item["current_run_reason_count"] = int(active_count)
    item["current_run_active"] = bool(active_count > 0)
    item["review_bucket"] = _rule_pruning_bucket(row, active_count)
    return item


def _top_rule_items(rows: Iterable[Mapping[str, Any]], limit: int = 12) -> List[Dict[str, Any]]:
    out = [dict(row) for row in rows if isinstance(row, Mapping)]
    out.sort(
        key=lambda row: (
            -int(row.get("current_run_reason_count") or 0),
            -int(row.get("later_winner_count") or 0),
            -int(row.get("settled_blocked_count") or 0),
            str(row.get("rule_code") or ""),
        )
    )
    return out[: max(0, int(limit))]


def _build_rule_pruning_review(
    rule_attribution: Mapping[str, Any],
    current_runtime: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    overall = rule_attribution.get("overall") if isinstance(rule_attribution, Mapping) else {}
    if not isinstance(overall, Mapping):
        overall = {}
    progress = overall.get("sample_progress") if isinstance(overall.get("sample_progress"), Mapping) else {}
    rows_in = overall.get("rows") if isinstance(overall.get("rows"), list) else []
    runtime_counts = _runtime_reason_code_counts(current_runtime)
    rows = [
        _rule_review_item(row, runtime_counts.get(str(row.get("rule_code") or ""), 0))
        for row in rows_in
        if isinstance(row, Mapping)
    ]
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get("review_bucket") or "monitor")].append(row)
    bucket_counts = {key: len(value) for key, value in sorted(buckets.items())}
    active_prune = _top_rule_items(buckets.get("active_prune_or_soften", []))
    historical_residual = _top_rule_items(buckets.get("historical_false_reject_residual", []))
    threshold_review = _top_rule_items(buckets.get("threshold_or_haircut_review", []))
    historical_threshold_review = _top_rule_items(
        buckets.get("historical_threshold_or_haircut_residual", [])
    )
    needs_mapping = _top_rule_items(buckets.get("needs_rule_mapping", []))
    wrapper_cleanup = _top_rule_items(buckets.get("wrapper_reason_cleanup", []))
    historical_wrapper = _top_rule_items(buckets.get("historical_wrapper_residual", []))
    diagnostic_review = _top_rule_items(buckets.get("diagnostic_or_sort_review", []))
    historical_diagnostic = _top_rule_items(buckets.get("historical_diagnostic_residual", []))
    protective = _top_rule_items(buckets.get("keep_protective", []))
    hard_invariant = _top_rule_items(buckets.get("keep_hard_invariant", []))
    ready = bool(progress.get("ready_for_rule_pruning_review"))
    current_active_codes = sum(1 for count in runtime_counts.values() if count > 0)
    if not ready:
        next_task = "settled_counterfactual_sample_accrual"
        recommended_action = "collect_more_settled_counterfactual_samples"
    elif active_prune:
        next_task = "active_rule_prune_or_soften"
        recommended_action = "review_active_false_reject_rules"
    elif threshold_review:
        next_task = "threshold_and_soft_penalty_replay"
        recommended_action = "review_threshold_or_haircut_strength"
    elif needs_mapping:
        next_task = "rule_reason_mapping_cleanup"
        recommended_action = "map_unknown_rule_codes"
    elif diagnostic_review:
        next_task = "diagnostic_sort_reason_cleanup"
        recommended_action = "repair_active_diagnostic_or_sort_reason"
    elif wrapper_cleanup:
        next_task = "rule_reason_mapping_cleanup"
        recommended_action = "replace_wrapper_reason_codes_with_specific_reasons"
    elif historical_residual:
        next_task = "rolling_validation"
        recommended_action = "monitor_historical_false_reject_residuals"
    else:
        next_task = "rolling_validation"
        recommended_action = "continue_monitoring_rule_attribution"
    return {
        "ok": bool(ready),
        "recommended_action": recommended_action,
        "next_mainline_task": next_task,
        "sample_progress": dict(progress),
        "current_run": {
            "run_id": _as_int((current_runtime or {}).get("run_id") if isinstance(current_runtime, Mapping) else 0),
            "active_reason_code_count": current_active_codes,
            "reason_code_counts": runtime_counts,
        },
        "bucket_counts": bucket_counts,
        "active_prune_or_soften": active_prune,
        "historical_false_reject_residual": historical_residual,
        "threshold_or_haircut_review": threshold_review,
        "historical_threshold_or_haircut_residual": historical_threshold_review,
        "needs_rule_mapping": needs_mapping,
        "diagnostic_or_sort_review": diagnostic_review,
        "historical_diagnostic_residual": historical_diagnostic,
        "wrapper_reason_cleanup": wrapper_cleanup,
        "historical_wrapper_residual": historical_wrapper,
        "keep_protective": protective,
        "keep_hard_invariant": hard_invariant,
        "field_contract": [
            "review_bucket",
            "current_run_active",
            "current_run_reason_count",
            "rule_code",
            "rule_type",
            "settled_blocked_count",
            "later_winner_count",
            "false_reject_rate",
            "true_block_rate",
            "recommended_action",
            "next_mainline_task",
        ],
        "notes": [
            "active_prune_or_soften only includes rules still present in the latest entry run.",
            "historical_false_reject_residual means the rule had high historical false rejects but is not active in the latest run.",
        ],
    }


def _build_rule_attribution(
    overall: Mapping[str, Any],
    by_strategy: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    overall_attribution = _build_rule_attribution_from_stats(overall)
    next_mainline_task = str(
        (overall_attribution.get("sample_progress") or {}).get("next_mainline_task")
        or "settled_counterfactual_sample_accrual"
    )
    return {
        "overall": overall_attribution,
        "by_strategy": {
            str(strategy): _build_rule_attribution_from_stats(stats)
            for strategy, stats in by_strategy.items()
            if isinstance(stats, Mapping)
        },
        "next_mainline_task": next_mainline_task,
        "notes": [
            "hard_invariant rules are not pruning candidates even when a rejected candidate later wins.",
            "false_reject_rate is later_winner_count / settled_blocked_count for rejected candidates carrying the rule.",
            "avg_counterfactual_edge uses entry_usable_edge when present, then q-minus-price fallbacks.",
        ],
    }


def _window_for_days(days: int) -> Tuple[str, str, str, str]:
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET_ZONE)
    start_date = now_et.date() - timedelta(days=max(1, int(days)) - 1)
    start_et = datetime.combine(start_date, time.min, tzinfo=ET_ZONE)
    start_utc = start_et.astimezone(timezone.utc)
    return (
        start_utc.isoformat(timespec="seconds"),
        now_utc.isoformat(timespec="seconds"),
        start_et.isoformat(timespec="seconds"),
        now_et.isoformat(timespec="seconds"),
    )


def _apply_rounding(actual: float, rounding_rule: str) -> float:
    rule = str(rounding_rule or "").strip().lower()
    if "floor" in rule:
        return float(math.floor(float(actual)))
    if "ceil" in rule:
        return float(math.ceil(float(actual)))
    return float(actual)


def _bucket_hit(actual: float, label: str, rounding_rule: str = "") -> Optional[bool]:
    text = str(label or "").strip()
    if not text:
        return None
    value = _apply_rounding(float(actual), rounding_rule)
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text)]
    low = text.lower()
    if any(token in low for token in ("or higher", "or above", "above", "+")) and nums:
        return value >= nums[0]
    if any(token in low for token in ("or lower", "or below", "below")) and nums:
        return value <= nums[0]
    if len(nums) >= 2:
        lo, hi = min(nums[0], nums[1]), max(nums[0], nums[1])
        return lo <= value <= hi
    if len(nums) == 1:
        return round(value) == round(nums[0])
    return None


def _side_hit(actual: float, outcome: str, side: str, rounding_rule: str = "") -> Optional[bool]:
    hit = _bucket_hit(actual, outcome, rounding_rule)
    if hit is None:
        return None
    return bool(hit) if str(side or "").upper() != "NO" else (not bool(hit))


def _avg(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    return (sum(vals) / len(vals)) if vals else None


def _rate(num: int, den: int) -> Optional[float]:
    return (int(num) / int(den)) if int(den) else None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _as_float(value: Any) -> Optional[float]:
    return _to_float(value)


def _inc_counter(item: Dict[str, Any], field: str, key: str, by: int = 1) -> None:
    text = str(key or "").strip() or "unknown"
    bucket = item.get(field)
    if not isinstance(bucket, dict):
        bucket = {}
        item[field] = bucket
    bucket[text] = int(bucket.get(text) or 0) + int(by)


def _top_counts(counter: Any, limit: int = 20) -> List[Dict[str, Any]]:
    if not isinstance(counter, Mapping):
        return []
    rows = [
        {"key": str(key), "count": int(value or 0)}
        for key, value in counter.items()
        if str(key or "").strip()
    ]
    rows.sort(key=lambda x: (-int(x["count"]), str(x["key"])))
    return rows[: max(1, int(limit))]


def _normalize_repricing_evidence_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "default":
        return "default_only"
    return text


def _parse_repricing_probability_source(raw: Any) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for item in _split_counter_items(raw):
        if "=" not in item:
            continue
        component, raw_status = item.split("=", 1)
        component = component.strip()
        if component not in REPRICING_EVIDENCE_COMPONENTS:
            continue
        status, _, source = raw_status.partition(":")
        parsed: Dict[str, str] = {}
        status = _normalize_repricing_evidence_status(status)
        if status:
            parsed["status"] = status
        source = source.strip()
        if source:
            parsed["source"] = source
        if parsed:
            out[component] = parsed
    return out


def _repricing_component_from_details(
    details: Mapping[str, Any],
    component: str,
    source_hint: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    prefix = f"edge_realizable_{component}"
    hint = source_hint if isinstance(source_hint, Mapping) else {}
    out: Dict[str, Any] = {}
    prob = _to_float(details.get(prefix))
    if prob is not None:
        out["prob"] = float(prob)
    samples = _to_float(details.get(f"{prefix}_samples"))
    if samples is not None:
        out["samples"] = float(samples)
    status = _normalize_repricing_evidence_status(
        details.get(f"{prefix}_evidence_status") or hint.get("status")
    )
    if status:
        out["status"] = status
    source = str(details.get(f"{prefix}_source") or hint.get("source") or "").strip()
    if source:
        out["source"] = source
    bucket_key = str(details.get(f"{prefix}_bucket_key") or "").strip()
    if bucket_key:
        out["bucket_key"] = bucket_key
    bucket_used_key = str(details.get(f"{prefix}_bucket_used_key") or "").strip()
    if bucket_used_key:
        out["bucket_used_key"] = bucket_used_key
    return out


def _new_repricing_component_stats() -> Dict[str, Dict[str, Any]]:
    return {
        component: {
            "candidate_count": 0,
            "reported_count": 0,
            "missing_count": 0,
            "default_only_count": 0,
            "low_sample_count": 0,
            "bucket_evidence_count": 0,
            "status_counts": {},
            "source_counts": {},
            "bucket_key_counts": {},
            "bucket_used_key_counts": {},
            "bucket_used_breakdown_raw": {},
            "probability_too_low_count": 0,
            "limiting_probability_count": 0,
            "limiting_bucket_used_key_counts": {},
            "probability_bucket_quality_raw": {},
            "prob_values": [],
            "sample_values": [],
        }
        for component in REPRICING_EVIDENCE_COMPONENTS
    }


def _repricing_probability_limiter_component(components: Mapping[str, Any]) -> str:
    values: List[Tuple[float, str]] = []
    if not isinstance(components, Mapping):
        return ""
    for component in REPRICING_EVIDENCE_COMPONENTS:
        item = components.get(component)
        if not isinstance(item, Mapping):
            continue
        prob = _to_float(item.get("prob"))
        if prob is None:
            continue
        values.append((float(prob), component))
    if not values:
        return ""
    values.sort(key=lambda pair: (pair[0], pair[1]))
    return values[0][1]


def _inc_probability_bucket_quality(
    item: Dict[str, Any],
    bucket_key: str,
    *,
    prob: Optional[float],
    samples: Optional[float],
    p_realize: Optional[float],
    probability_too_low: bool,
    limiting_probability: bool,
) -> None:
    text = str(bucket_key or "").strip() or "unknown"
    raw = item.get("probability_bucket_quality_raw")
    if not isinstance(raw, dict):
        raw = {}
        item["probability_bucket_quality_raw"] = raw
    row = raw.get(text)
    if not isinstance(row, dict):
        row = {
            "candidate_count": 0,
            "probability_too_low_count": 0,
            "limiting_probability_count": 0,
            "prob_sum": 0.0,
            "prob_count": 0,
            "samples_sum": 0.0,
            "samples_count": 0,
            "p_realize_sum": 0.0,
            "p_realize_count": 0,
            "min_prob": None,
            "min_samples": None,
        }
        raw[text] = row
    row["candidate_count"] += 1
    if probability_too_low:
        row["probability_too_low_count"] += 1
    if limiting_probability:
        row["limiting_probability_count"] += 1
    if prob is not None:
        prob_f = float(prob)
        row["prob_sum"] += prob_f
        row["prob_count"] += 1
        prev_min_prob = _to_float(row.get("min_prob"))
        row["min_prob"] = prob_f if prev_min_prob is None else min(prev_min_prob, prob_f)
    if samples is not None:
        samples_f = float(samples)
        row["samples_sum"] += samples_f
        row["samples_count"] += 1
        prev_min_samples = _to_float(row.get("min_samples"))
        row["min_samples"] = samples_f if prev_min_samples is None else min(prev_min_samples, samples_f)
    if p_realize is not None:
        row["p_realize_sum"] += float(p_realize)
        row["p_realize_count"] += 1


def _finalize_probability_bucket_quality(raw: Any, limit: int = 12) -> List[Dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return []
    rows: List[Dict[str, Any]] = []
    for key, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        candidate_count = int(value.get("candidate_count") or 0)
        prob_count = int(value.get("prob_count") or 0)
        samples_count = int(value.get("samples_count") or 0)
        p_realize_count = int(value.get("p_realize_count") or 0)
        probability_too_low_count = int(value.get("probability_too_low_count") or 0)
        limiting_probability_count = int(value.get("limiting_probability_count") or 0)
        rows.append(
            {
                "key": str(key),
                "candidate_count": candidate_count,
                "probability_too_low_count": probability_too_low_count,
                "probability_too_low_rate": _rate(probability_too_low_count, candidate_count),
                "limiting_probability_count": limiting_probability_count,
                "limiting_probability_rate": _rate(limiting_probability_count, candidate_count),
                "avg_prob": (
                    float(value.get("prob_sum") or 0.0) / prob_count
                    if prob_count > 0
                    else None
                ),
                "min_prob": _to_float(value.get("min_prob")),
                "avg_samples": (
                    float(value.get("samples_sum") or 0.0) / samples_count
                    if samples_count > 0
                    else None
                ),
                "min_samples": _to_float(value.get("min_samples")),
                "avg_p_realize": (
                    float(value.get("p_realize_sum") or 0.0) / p_realize_count
                    if p_realize_count > 0
                    else None
                ),
            }
        )
    rows.sort(
        key=lambda row: (
            -int(row["limiting_probability_count"]),
            -int(row["probability_too_low_count"]),
            -int(row["candidate_count"]),
            float(row["avg_prob"]) if row["avg_prob"] is not None else 1.0,
            str(row["key"]),
        )
    )
    return rows[: max(1, int(limit))]


def _finalize_repricing_component_stats(raw: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, Mapping):
        raw = {}
    out: Dict[str, Dict[str, Any]] = {}
    for component in REPRICING_EVIDENCE_COMPONENTS:
        item = raw.get(component) if isinstance(raw, Mapping) else None
        if not isinstance(item, Mapping):
            item = {}
        candidate_count = int(item.get("candidate_count") or 0)
        reported_count = int(item.get("reported_count") or 0)
        missing_count = int(item.get("missing_count") or 0)
        default_only_count = int(item.get("default_only_count") or 0)
        low_sample_count = int(item.get("low_sample_count") or 0)
        bucket_evidence_count = int(item.get("bucket_evidence_count") or 0)
        probability_too_low_count = int(item.get("probability_too_low_count") or 0)
        limiting_probability_count = int(item.get("limiting_probability_count") or 0)
        sample_values = [float(x) for x in item.get("sample_values") or []]
        out[component] = {
            "candidate_count": candidate_count,
            "reported_count": reported_count,
            "reported_rate": _rate(reported_count, candidate_count),
            "unreported_count": max(0, candidate_count - reported_count),
            "missing_count": missing_count,
            "missing_rate": _rate(missing_count, candidate_count),
            "default_only_count": default_only_count,
            "default_only_rate": _rate(default_only_count, candidate_count),
            "low_sample_count": low_sample_count,
            "low_sample_rate": _rate(low_sample_count, candidate_count),
            "bucket_evidence_count": bucket_evidence_count,
            "bucket_evidence_rate": _rate(bucket_evidence_count, candidate_count),
            "probability_too_low_count": probability_too_low_count,
            "probability_too_low_rate": _rate(probability_too_low_count, candidate_count),
            "limiting_probability_count": limiting_probability_count,
            "limiting_probability_rate": _rate(limiting_probability_count, candidate_count),
            "avg_prob": _avg([float(x) for x in item.get("prob_values") or []]),
            "avg_samples": _avg(sample_values),
            "min_samples": min(sample_values) if sample_values else None,
            "status_top": _top_counts(item.get("status_counts")),
            "source_top": _top_counts(item.get("source_counts")),
            "bucket_key_top": _top_counts(item.get("bucket_key_counts")),
            "bucket_used_key_top": _top_counts(item.get("bucket_used_key_counts")),
            "limiting_bucket_used_key_top": _top_counts(
                item.get("limiting_bucket_used_key_counts")
            ),
            "probability_bucket_quality": _finalize_probability_bucket_quality(
                item.get("probability_bucket_quality_raw"),
                limit=12,
            ),
            "bucket_used_breakdown": _finalize_breakdown(
                item.get("bucket_used_breakdown_raw"),
                limit=12,
            ),
        }
    return out


def _new_breakdown_item() -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "candidate_count": 0,
        "will_order_count": 0,
        "dry_run_intent_count": 0,
        "filled_entry_count": 0,
        "settled_count": 0,
        "rejected_settled_count": 0,
        "rejected_hit_count": 0,
        "missed_winner_count": 0,
    }
    for metric in BREAKDOWN_METRICS:
        item[f"{metric}_sum"] = 0.0
        item[f"{metric}_count"] = 0
    return item


def _inc_breakdown(
    item: Dict[str, Any],
    field: str,
    key: str,
    *,
    will_order: bool,
    dry_run_intent: bool,
    buy_filled: bool,
    settlement_known: bool,
    rejected: bool,
    side_hit: Optional[bool],
    missed_winner: bool,
    metrics: Optional[Mapping[str, Any]] = None,
) -> None:
    text = str(key or "").strip() or "unknown"
    bucket = item.get(field)
    if not isinstance(bucket, dict):
        bucket = {}
        item[field] = bucket
    row = bucket.get(text)
    if not isinstance(row, dict):
        row = _new_breakdown_item()
        bucket[text] = row
    row["candidate_count"] += 1
    if will_order:
        row["will_order_count"] += 1
    if dry_run_intent:
        row["dry_run_intent_count"] += 1
    if buy_filled:
        row["filled_entry_count"] += 1
    if settlement_known:
        row["settled_count"] += 1
        if rejected:
            row["rejected_settled_count"] += 1
            if side_hit is True:
                row["rejected_hit_count"] += 1
    if missed_winner:
        row["missed_winner_count"] += 1
    if isinstance(metrics, Mapping):
        for metric in BREAKDOWN_METRICS:
            value = _to_float(metrics.get(metric))
            if value is None:
                continue
            row[f"{metric}_sum"] += float(value)
            row[f"{metric}_count"] += 1


def _finalize_breakdown(breakdown: Any, limit: int = 20) -> List[Dict[str, Any]]:
    if not isinstance(breakdown, Mapping):
        return []
    rows: List[Dict[str, Any]] = []
    for key, raw in breakdown.items():
        if not isinstance(raw, Mapping):
            continue
        rejected_settled = int(raw.get("rejected_settled_count") or 0)
        rejected_hit = int(raw.get("rejected_hit_count") or 0)
        row = {
            "key": str(key),
            "candidate_count": int(raw.get("candidate_count") or 0),
            "will_order_count": int(raw.get("will_order_count") or 0),
            "dry_run_intent_count": int(raw.get("dry_run_intent_count") or 0),
            "filled_entry_count": int(raw.get("filled_entry_count") or 0),
            "settled_count": int(raw.get("settled_count") or 0),
            "rejected_settled_count": rejected_settled,
            "rejected_hit_count": rejected_hit,
            "missed_winner_count": int(raw.get("missed_winner_count") or 0),
            "rejected_hit_rate": _rate(rejected_hit, rejected_settled),
        }
        for metric in BREAKDOWN_METRICS:
            count = int(raw.get(f"{metric}_count") or 0)
            row[f"avg_{metric}"] = (
                float(raw.get(f"{metric}_sum") or 0.0) / count
                if count > 0
                else None
            )
        rows.append(row)
    rows.sort(
        key=lambda x: (
            -int(x["missed_winner_count"]),
            -int(x["rejected_settled_count"]),
            -int(x["candidate_count"]),
            str(x["key"]),
        )
    )
    return rows[: max(1, int(limit))]


def _load_settlements(
    conn: sqlite3.Connection,
    keys: Iterable[Tuple[str, str]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    best_order: Dict[Tuple[str, str], Tuple[str, int]] = {}
    event_keys = _unique_event_keys(keys)
    if not event_keys:
        return out
    cur = conn.cursor()
    for chunk in _chunked(event_keys, 400):
        values_sql, params = _values_cte(chunk, 2)
        try:
            cur.execute(
                f"""
                WITH wanted(slug, event_date) AS (VALUES {values_sql})
                SELECT e.slug, e.event_date, e.settlement_actual, e.rounding_rule,
                       e.created_at, e.id
                FROM event_runs e
                JOIN wanted w
                  ON w.slug = e.slug
                 AND w.event_date = e.event_date
                WHERE e.settlement_actual IS NOT NULL
                  AND e.settlement_actual != ''
                """,
                params,
            )
        except sqlite3.OperationalError:
            continue
        for row in cur.fetchall():
            actual = _to_float(row["settlement_actual"])
            if actual is None:
                continue
            key = (
                str(row["slug"] or "").strip(),
                str(row["event_date"] or "").split("T", 1)[0].strip(),
            )
            if not key[0] or not key[1]:
                continue
            created_at = str(row["created_at"] or "")
            row_id = int(row["id"] or 0)
            order_key = (created_at, row_id)
            if key in best_order and order_key <= best_order[key]:
                continue
            best_order[key] = order_key
            out[key] = {
                "settlement_actual": float(actual),
                "rounding_rule": str(row["rounding_rule"] or ""),
                "created_at": created_at,
            }
    return out


def _event_date_for_settlement_check(event_date: Any) -> Optional[datetime.date]:
    text = str(event_date or "").split("T", 1)[0].strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def _settlement_eligible(event_date: Any) -> bool:
    parsed = _event_date_for_settlement_check(event_date)
    if parsed is None:
        return False
    return parsed < datetime.now(timezone.utc).astimezone(ET_ZONE).date()


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _settlement_missing_bucket(item: Mapping[str, Any]) -> str:
    event_run_count = int(item.get("event_run_count") or 0)
    if event_run_count <= 0:
        return "event_run_missing"
    if bool(item.get("market_closed")):
        return "market_closed_without_settlement_actual"
    latest_created = _parse_iso_datetime(item.get("latest_created_at"))
    event_end = _parse_iso_datetime(item.get("event_end_utc"))
    if latest_created is not None and event_end is not None:
        if latest_created < event_end:
            return "latest_event_run_before_event_end"
        return "post_event_refresh_missing_settlement_actual"
    if event_end is None:
        return "event_end_utc_missing"
    return "latest_event_run_timestamp_missing"


def _load_event_settlement_presence(
    conn: sqlite3.Connection,
    keys: Iterable[Tuple[str, str]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    event_keys = _unique_event_keys(keys)
    out: Dict[Tuple[str, str], Dict[str, Any]] = {
        key: {
            "event_run_count": 0,
            "settled_row_count": 0,
            "market_closed": False,
            "latest_created_at": "",
        }
        for key in event_keys
    }
    if not event_keys:
        return out
    cur = conn.cursor()
    try:
        columns = {
            str(row[1])
            for row in cur.execute("PRAGMA table_info(event_runs)").fetchall()
            if len(row) > 1
        }
    except sqlite3.OperationalError:
        columns = set()
    market_closed_expr = (
        "MAX(CASE WHEN e.market_closed THEN 1 ELSE 0 END)"
        if "market_closed" in columns
        else "0"
    )
    event_end_expr = "MAX(e.event_end_utc)" if "event_end_utc" in columns else "''"
    for chunk in _chunked(event_keys, 400):
        values_sql, params = _values_cte(chunk, 2)
        try:
            cur.execute(
                f"""
                WITH wanted(slug, event_date) AS (VALUES {values_sql})
                SELECT
                  e.slug AS slug,
                  e.event_date AS event_date,
                  COUNT(*) AS row_count,
                  SUM(CASE WHEN e.settlement_actual IS NOT NULL THEN 1 ELSE 0 END) AS settled_count,
                  {market_closed_expr} AS market_closed,
                  MAX(e.created_at) AS latest_created_at,
                  {event_end_expr} AS event_end_utc
                FROM event_runs e
                JOIN wanted w
                  ON w.slug = e.slug
                 AND w.event_date = e.event_date
                GROUP BY e.slug, e.event_date
                """,
                params,
            )
        except sqlite3.OperationalError:
            continue
        for row in cur.fetchall():
            key = (
                str(row["slug"] or "").strip(),
                str(row["event_date"] or "").split("T", 1)[0].strip(),
            )
            if key not in out:
                continue
            out[key] = {
                "event_run_count": int(row["row_count"] or 0),
                "settled_row_count": int(row["settled_count"] or 0),
                "market_closed": bool(row["market_closed"]),
                "latest_created_at": str(row["latest_created_at"] or ""),
                "event_end_utc": str(row["event_end_utc"] or ""),
            }
    return out


def _build_settlement_coverage(
    conn: sqlite3.Connection,
    candidates: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    candidate_rows = list(candidates)
    eligible_candidates = [
        cand
        for cand in candidate_rows
        if bool(cand.get("rejected")) and _settlement_eligible(cand.get("event_date"))
    ]
    event_keys = {(str(c["slug"]), str(c["event_date"])) for c in eligible_candidates}
    presence = _load_event_settlement_presence(conn, event_keys)
    event_run_present_count = 0
    market_closed_count = 0
    settlement_present_count = 0
    missing_bucket_counts: Dict[str, int] = defaultdict(int)
    event_run_missing_examples: List[Dict[str, Any]] = []
    settlement_missing_examples: List[Dict[str, Any]] = []
    for cand in eligible_candidates:
        key = (str(cand["slug"]), str(cand["event_date"]))
        item = presence.get(key) or {}
        event_run_count = int(item.get("event_run_count") or 0)
        settled_row_count = int(item.get("settled_row_count") or 0)
        if event_run_count > 0:
            event_run_present_count += 1
        elif len(event_run_missing_examples) < 8:
            event_run_missing_examples.append(
                {
                    "slug": cand.get("slug"),
                    "event_date": cand.get("event_date"),
                    "outcome": cand.get("outcome"),
                    "side": cand.get("side"),
                    "strategy": cand.get("strategy"),
                    "reason_codes": sorted(cand.get("reason_codes") or [])[:8],
                }
            )
        if bool(item.get("market_closed")):
            market_closed_count += 1
        if settled_row_count > 0:
            settlement_present_count += 1
        else:
            missing_bucket = _settlement_missing_bucket(item)
            missing_bucket_counts[missing_bucket] += 1
            if len(settlement_missing_examples) < 8:
                settlement_missing_examples.append(
                    {
                        "slug": cand.get("slug"),
                        "event_date": cand.get("event_date"),
                        "outcome": cand.get("outcome"),
                        "side": cand.get("side"),
                        "strategy": cand.get("strategy"),
                        "event_run_count": event_run_count,
                        "market_closed": bool(item.get("market_closed")),
                        "latest_event_run_created_at": str(item.get("latest_created_at") or ""),
                        "event_end_utc": str(item.get("event_end_utc") or ""),
                        "missing_bucket": missing_bucket,
                        "reason_codes": sorted(cand.get("reason_codes") or [])[:8],
                    }
                )
    eligible_count = len(eligible_candidates)
    settlement_missing_count = max(0, eligible_count - settlement_present_count)
    if eligible_count <= 0:
        next_task = "settled_counterfactual_sample_accrual"
    elif settlement_missing_count > 0:
        next_task = "settlement_fact_capture_gap_review"
    else:
        next_task = "settled_counterfactual_sample_accrual"
    missing_bucket_top = [
        {"key": key, "count": int(count)}
        for key, count in sorted(
            missing_bucket_counts.items(),
            key=lambda kv: (-int(kv[1]), str(kv[0])),
        )
    ]
    primary_missing_bucket = missing_bucket_top[0]["key"] if missing_bucket_top else ""
    return {
        "ok": settlement_missing_count == 0,
        "candidate_count": len(candidate_rows),
        "settlement_eligible_rejected_count": eligible_count,
        "event_key_count": len(event_keys),
        "event_run_present_count": event_run_present_count,
        "event_run_missing_count": max(0, eligible_count - event_run_present_count),
        "market_closed_count": market_closed_count,
        "settlement_present_count": settlement_present_count,
        "settlement_missing_count": settlement_missing_count,
        "settlement_missing_bucket_top": missing_bucket_top,
        "primary_missing_bucket": primary_missing_bucket,
        "event_run_present_rate": _rate(event_run_present_count, eligible_count),
        "settlement_present_rate": _rate(settlement_present_count, eligible_count),
        "next_mainline_task": next_task,
        "event_run_missing_examples": event_run_missing_examples,
        "settlement_missing_examples": settlement_missing_examples,
        "notes": [
            "This is a read-only coverage check; it does not backfill event_runs.",
            "A candidate is settlement-eligible when its event_date is before the current ET date and it has at least one rejected entry decision.",
        ],
    }


def _load_fills(
    conn: sqlite3.Connection,
    keys: Iterable[Tuple[str, str, str, str]],
) -> Dict[Tuple[str, str, str, str], Dict[str, Any]]:
    wanted = {
        (
            str(slug or "").strip(),
            str(event_date or "").split("T", 1)[0].strip(),
            str(outcome or "").strip(),
            str(outcome_side or "").strip().upper(),
        )
        for slug, event_date, outcome, outcome_side in keys
    }
    wanted = {key for key in wanted if key[0] and key[1] and key[2] and key[3]}
    if not wanted:
        return {}
    events = _unique_event_keys((k[0], k[1]) for k in wanted)
    out: Dict[Tuple[str, str, str, str], Dict[str, Any]] = defaultdict(
        lambda: {
            "buy_qty": 0.0,
            "buy_notional": 0.0,
            "sell_qty": 0.0,
            "sell_notional": 0.0,
            "order_ids": set(),
        }
    )
    cur = conn.cursor()
    for chunk in _chunked(events, 400):
        values_sql, params = _values_cte(chunk, 2)
        try:
            cur.execute(
                f"""
                WITH wanted(slug, event_date) AS (VALUES {values_sql})
                SELECT f.slug, f.event_date, f.outcome, f.outcome_side, f.trade_side,
                       f.price, f.size, f.order_id
                FROM edge_trade_fills f
                JOIN wanted w
                  ON w.slug = f.slug
                 AND w.event_date = f.event_date
                """,
                params,
            )
        except sqlite3.OperationalError:
            continue
        for row in cur.fetchall():
            key = (
                str(row["slug"] or "").strip(),
                str(row["event_date"] or "").split("T", 1)[0].strip(),
                str(row["outcome"] or "").strip(),
                str(row["outcome_side"] or "").strip().upper(),
            )
            if key not in wanted:
                continue
            trade_side = str(row["trade_side"] or "").strip().upper()
            price = _to_float(row["price"])
            size = _to_float(row["size"])
            if trade_side not in {"BUY", "SELL"} or price is None or size is None or size <= 0:
                continue
            item = out[key]
            if trade_side == "BUY":
                item["buy_qty"] += float(size)
                item["buy_notional"] += float(price) * float(size)
            else:
                item["sell_qty"] += float(size)
                item["sell_notional"] += float(price) * float(size)
            order_id = str(row["order_id"] or "").strip()
            if order_id:
                item["order_ids"].add(order_id)
    for item in out.values():
        buy_qty = float(item.get("buy_qty") or 0.0)
        sell_qty = float(item.get("sell_qty") or 0.0)
        item["buy_avg_price"] = (float(item["buy_notional"]) / buy_qty) if buy_qty > 0 else None
        item["sell_avg_price"] = (float(item["sell_notional"]) / sell_qty) if sell_qty > 0 else None
        item["order_ids"] = sorted(item.get("order_ids") or [])
    return out


def _new_stats() -> Dict[str, Any]:
    return {
        "candidate_count": 0,
        "raw_decision_count": 0,
        "will_order_count": 0,
        "live_will_order_count": 0,
        "dry_run_intent_count": 0,
        "filled_entry_count": 0,
        "exit_filled_count": 0,
        "settled_count": 0,
        "bought_settled_count": 0,
        "bought_hit_count": 0,
        "rejected_only_settled_count": 0,
        "rejected_only_hit_count": 0,
        "soft_down_count": 0,
        "soft_down_settled_count": 0,
        "soft_down_hit_count": 0,
        "soft_down_rejected_settled_count": 0,
        "soft_down_rejected_hit_count": 0,
        "soft_down_filled_count": 0,
        "soft_down_exit_filled_count": 0,
        "low_price_count": 0,
        "low_price_settled_count": 0,
        "low_price_hit_count": 0,
        "low_price_filled_count": 0,
        "low_price_exit_filled_count": 0,
        "entry_false_edge_count": 0,
        "entry_false_edge_settled_count": 0,
        "entry_false_edge_hit_count": 0,
        "entry_false_edge_missed_winner_count": 0,
        "friction_false_edge_count": 0,
        "friction_false_edge_settled_count": 0,
        "friction_false_edge_hit_count": 0,
        "friction_false_edge_missed_winner_count": 0,
        "repricing_candidate_count": 0,
        "repricing_evidence_missing_count": 0,
        "repricing_evidence_component_missing_count": 0,
        "repricing_evidence_default_only_count": 0,
        "repricing_evidence_status_missing_count": 0,
        "repricing_evidence_status_default_only_count": 0,
        "repricing_evidence_missing_table_candidate_count": 0,
        "repricing_evidence_default_table_candidate_count": 0,
        "repricing_evidence_entry_missing_flag_count": 0,
        "repricing_evidence_realize_missing_flag_count": 0,
        "repricing_evidence_realize_default_flag_count": 0,
        "repricing_evidence_missing_residual_count": 0,
        "repricing_evidence_low_sample_count": 0,
        "repricing_evidence_unreported_component_count": 0,
        "repricing_evidence_all_bucket_evidence_count": 0,
        "repricing_probability_too_low_count": 0,
        "entry_usable_edge_present_count": 0,
        "entry_usable_edge_missing_count": 0,
        "entry_usable_edge_na_reason_present_count": 0,
        "entry_usable_edge_silent_missing_count": 0,
        "entry_usable_edge_positive_count": 0,
        "entry_usable_edge_nonpositive_count": 0,
        "size_scale_zero_count": 0,
        "missed_winner_entry_usable_edge_missing_count": 0,
        "missed_winner_entry_usable_edge_positive_count": 0,
        "missed_winner_entry_usable_edge_nonpositive_count": 0,
        "missed_winner_size_scale_zero_count": 0,
        "entry_usable_edges": [],
        "size_scales": [],
        "exit_slippages": [],
        "repricing_p_realize_values": [],
        "repricing_p_converge_values": [],
        "repricing_p_fill_entry_values": [],
        "repricing_p_fill_exit_values": [],
        "repricing_evidence_scales": [],
        "repricing_target_surplus_raw_values": [],
        "repricing_target_surplus_discounted_values": [],
        "repricing_target_surplus_discounted_to_friction_values": [],
        "repricing_target_surplus_to_friction_values": [],
        "repricing_friction_floor_values": [],
        "repricing_evidence_component_stats": _new_repricing_component_stats(),
        "penalty_sums": {key: 0.0 for key in PENALTY_KEYS},
        "penalty_counts": {key: 0 for key in PENALTY_KEYS},
        "reason_code_counts": {},
        "latest_reason_code_counts": {},
        "entry_usable_edge_na_reason_counts": {},
        "reason_code_breakdown_raw": {},
        "latest_reason_code_breakdown_raw": {},
        "entry_usable_edge_na_reason_breakdown_raw": {},
        "missed_winner_reason_code_counts": {},
        "missed_winner_na_reason_counts": {},
        "entry_false_edge_type_counts": {},
        "entry_false_edge_root_cause_counts": {},
        "missed_winner_false_edge_type_counts": {},
        "missed_winner_false_edge_root_cause_counts": {},
        "repricing_evidence_status_counts": {},
        "repricing_evidence_na_reason_counts": {},
        "repricing_evidence_missing_reason_counts": {},
        "repricing_evidence_probability_source_counts": {},
        "repricing_evidence_missing_table_counts": {},
        "repricing_evidence_default_table_counts": {},
        "repricing_evidence_low_sample_table_counts": {},
        "repricing_evidence_bucket_samples_min_values": [],
        "station_missing_count": 0,
        "contract_rule_missing_count": 0,
    }


def _finalize_stats(item: Dict[str, Any]) -> Dict[str, Any]:
    bought_settled = int(item["bought_settled_count"])
    bought_hit = int(item["bought_hit_count"])
    rejected_settled = int(item["rejected_only_settled_count"])
    rejected_hit = int(item["rejected_only_hit_count"])
    filled = int(item["filled_entry_count"])
    live_will_order = int(item["live_will_order_count"])
    low_filled = int(item["low_price_filled_count"])
    out = dict(item)
    out["fill_rate"] = _rate(filled, live_will_order)
    out["exit_fill_rate"] = _rate(int(item["exit_filled_count"]), filled)
    out["bought_hit_rate"] = _rate(bought_hit, bought_settled)
    out["false_positive_rate"] = _rate(bought_settled - bought_hit, bought_settled)
    out["missed_winner_rate"] = _rate(rejected_hit, rejected_settled)
    out["soft_down_hit_rate"] = _rate(int(item["soft_down_hit_count"]), int(item["soft_down_settled_count"]))
    out["soft_down_missed_winner_rate"] = _rate(
        int(item["soft_down_rejected_hit_count"]),
        int(item["soft_down_rejected_settled_count"]),
    )
    out["soft_down_exit_fill_rate"] = _rate(
        int(item["soft_down_exit_filled_count"]),
        int(item["soft_down_filled_count"]),
    )
    out["low_price_hit_rate"] = _rate(
        int(item["low_price_hit_count"]),
        int(item["low_price_settled_count"]),
    )
    out["low_price_exit_fill_rate"] = _rate(int(item["low_price_exit_filled_count"]), low_filled)
    out["entry_false_edge_hit_rate"] = _rate(
        int(item["entry_false_edge_hit_count"]),
        int(item["entry_false_edge_settled_count"]),
    )
    out["entry_false_edge_missed_winner_rate"] = _rate(
        int(item["entry_false_edge_missed_winner_count"]),
        int(item["entry_false_edge_settled_count"]),
    )
    out["friction_false_edge_hit_rate"] = _rate(
        int(item["friction_false_edge_hit_count"]),
        int(item["friction_false_edge_settled_count"]),
    )
    out["friction_false_edge_missed_winner_rate"] = _rate(
        int(item["friction_false_edge_missed_winner_count"]),
        int(item["friction_false_edge_settled_count"]),
    )
    out["avg_entry_usable_edge"] = _avg([float(x) for x in item["entry_usable_edges"]])
    out["avg_size_scale"] = _avg([float(x) for x in item["size_scales"]])
    out["avg_exit_slippage"] = _avg([float(x) for x in item["exit_slippages"]])
    repricing_count = int(item.get("repricing_candidate_count") or 0)
    out["repricing_evidence_missing_rate"] = _rate(
        int(item.get("repricing_evidence_missing_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_component_missing_rate"] = _rate(
        int(item.get("repricing_evidence_component_missing_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_default_only_rate"] = _rate(
        int(item.get("repricing_evidence_default_only_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_status_missing_rate"] = _rate(
        int(item.get("repricing_evidence_status_missing_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_status_default_only_rate"] = _rate(
        int(item.get("repricing_evidence_status_default_only_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_missing_table_candidate_rate"] = _rate(
        int(item.get("repricing_evidence_missing_table_candidate_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_default_table_candidate_rate"] = _rate(
        int(item.get("repricing_evidence_default_table_candidate_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_entry_missing_flag_rate"] = _rate(
        int(item.get("repricing_evidence_entry_missing_flag_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_realize_missing_flag_rate"] = _rate(
        int(item.get("repricing_evidence_realize_missing_flag_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_realize_default_flag_rate"] = _rate(
        int(item.get("repricing_evidence_realize_default_flag_count") or 0),
        repricing_count,
    )
    missing_residual_count = max(
        0,
        int(item.get("repricing_evidence_missing_residual_count") or 0),
    )
    out["repricing_evidence_missing_residual_count"] = missing_residual_count
    out["repricing_evidence_missing_residual_rate"] = _rate(
        missing_residual_count,
        repricing_count,
    )
    out["repricing_evidence_low_sample_rate"] = _rate(
        int(item.get("repricing_evidence_low_sample_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_unreported_component_rate"] = _rate(
        int(item.get("repricing_evidence_unreported_component_count") or 0),
        repricing_count,
    )
    out["repricing_evidence_all_bucket_evidence_rate"] = _rate(
        int(item.get("repricing_evidence_all_bucket_evidence_count") or 0),
        repricing_count,
    )
    out["repricing_probability_too_low_rate"] = _rate(
        int(item.get("repricing_probability_too_low_count") or 0),
        repricing_count,
    )
    out["avg_repricing_p_realize"] = _avg(
        [float(x) for x in item.get("repricing_p_realize_values") or []]
    )
    out["avg_repricing_p_converge"] = _avg(
        [float(x) for x in item.get("repricing_p_converge_values") or []]
    )
    out["avg_repricing_p_fill_entry"] = _avg(
        [float(x) for x in item.get("repricing_p_fill_entry_values") or []]
    )
    out["avg_repricing_p_fill_exit"] = _avg(
        [float(x) for x in item.get("repricing_p_fill_exit_values") or []]
    )
    component_breakdown = _finalize_repricing_component_stats(
        item.get("repricing_evidence_component_stats")
    )
    out["avg_repricing_evidence_scale"] = _avg(
        [float(x) for x in item.get("repricing_evidence_scales") or []]
    )
    out["avg_repricing_target_surplus_raw"] = _avg(
        [float(x) for x in item.get("repricing_target_surplus_raw_values") or []]
    )
    out["avg_repricing_target_surplus_discounted"] = _avg(
        [float(x) for x in item.get("repricing_target_surplus_discounted_values") or []]
    )
    out["avg_repricing_target_surplus_discounted_to_friction"] = _avg(
        [
            float(x)
            for x in item.get("repricing_target_surplus_discounted_to_friction_values") or []
        ]
    )
    out["avg_repricing_target_surplus_to_friction"] = _avg(
        [float(x) for x in item.get("repricing_target_surplus_to_friction_values") or []]
    )
    out["avg_repricing_friction_floor"] = _avg(
        [float(x) for x in item.get("repricing_friction_floor_values") or []]
    )
    out["repricing_execution_evidence"] = {
        "candidate_count": repricing_count,
        "missing_count": int(item.get("repricing_evidence_missing_count") or 0),
        "missing_rate": out["repricing_evidence_missing_rate"],
        "component_missing_count": int(
            item.get("repricing_evidence_component_missing_count") or 0
        ),
        "component_missing_rate": out["repricing_evidence_component_missing_rate"],
        "default_only_count": int(
            item.get("repricing_evidence_default_only_count") or 0
        ),
        "default_only_rate": out["repricing_evidence_default_only_rate"],
        "status_missing_count": int(
            item.get("repricing_evidence_status_missing_count") or 0
        ),
        "status_missing_rate": out["repricing_evidence_status_missing_rate"],
        "status_default_only_count": int(
            item.get("repricing_evidence_status_default_only_count") or 0
        ),
        "status_default_only_rate": out[
            "repricing_evidence_status_default_only_rate"
        ],
        "missing_table_candidate_count": int(
            item.get("repricing_evidence_missing_table_candidate_count") or 0
        ),
        "missing_table_candidate_rate": out[
            "repricing_evidence_missing_table_candidate_rate"
        ],
        "default_table_candidate_count": int(
            item.get("repricing_evidence_default_table_candidate_count") or 0
        ),
        "default_table_candidate_rate": out[
            "repricing_evidence_default_table_candidate_rate"
        ],
        "entry_missing_flag_count": int(
            item.get("repricing_evidence_entry_missing_flag_count") or 0
        ),
        "entry_missing_flag_rate": out[
            "repricing_evidence_entry_missing_flag_rate"
        ],
        "realize_missing_flag_count": int(
            item.get("repricing_evidence_realize_missing_flag_count") or 0
        ),
        "realize_missing_flag_rate": out[
            "repricing_evidence_realize_missing_flag_rate"
        ],
        "realize_default_flag_count": int(
            item.get("repricing_evidence_realize_default_flag_count") or 0
        ),
        "realize_default_flag_rate": out[
            "repricing_evidence_realize_default_flag_rate"
        ],
        "missing_residual_count": out["repricing_evidence_missing_residual_count"],
        "missing_residual_rate": out["repricing_evidence_missing_residual_rate"],
        "low_sample_count": int(item.get("repricing_evidence_low_sample_count") or 0),
        "low_sample_rate": out["repricing_evidence_low_sample_rate"],
        "unreported_component_count": int(
            item.get("repricing_evidence_unreported_component_count") or 0
        ),
        "unreported_component_rate": out[
            "repricing_evidence_unreported_component_rate"
        ],
        "all_bucket_evidence_count": int(
            item.get("repricing_evidence_all_bucket_evidence_count") or 0
        ),
        "all_bucket_evidence_rate": out[
            "repricing_evidence_all_bucket_evidence_rate"
        ],
        "probability_too_low_count": int(
            item.get("repricing_probability_too_low_count") or 0
        ),
        "probability_too_low_rate": out["repricing_probability_too_low_rate"],
        "avg_p_realize": out["avg_repricing_p_realize"],
        "avg_p_converge": out["avg_repricing_p_converge"],
        "avg_p_fill_entry": out["avg_repricing_p_fill_entry"],
        "avg_p_fill_exit": out["avg_repricing_p_fill_exit"],
        "avg_evidence_scale": out["avg_repricing_evidence_scale"],
        "avg_execution_probability": out["avg_repricing_evidence_scale"],
        "avg_target_surplus_raw": out["avg_repricing_target_surplus_raw"],
        "avg_target_surplus_discounted": out[
            "avg_repricing_target_surplus_discounted"
        ],
        "avg_discounted_surplus_to_friction": out[
            "avg_repricing_target_surplus_discounted_to_friction"
        ],
        "avg_target_surplus_to_friction": out[
            "avg_repricing_target_surplus_to_friction"
        ],
        "avg_friction_floor": out["avg_repricing_friction_floor"],
        "component_breakdown": component_breakdown,
        "status_top": _top_counts(item.get("repricing_evidence_status_counts")),
        "na_reason_top": _top_counts(item.get("repricing_evidence_na_reason_counts")),
        "missing_reason_top": _top_counts(
            item.get("repricing_evidence_missing_reason_counts")
        ),
        "probability_source_top": _top_counts(
            item.get("repricing_evidence_probability_source_counts")
        ),
        "missing_table_top": _top_counts(
            item.get("repricing_evidence_missing_table_counts")
        ),
        "default_table_top": _top_counts(
            item.get("repricing_evidence_default_table_counts")
        ),
        "low_sample_table_top": _top_counts(
            item.get("repricing_evidence_low_sample_table_counts")
        ),
        "min_bucket_samples": (
            min(item.get("repricing_evidence_bucket_samples_min_values") or [])
            if item.get("repricing_evidence_bucket_samples_min_values")
            else None
        ),
        "avg_bucket_samples_min": _avg(
            [
                float(x)
                for x in item.get("repricing_evidence_bucket_samples_min_values") or []
            ]
        ),
    }
    penalty_sums = item.get("penalty_sums") if isinstance(item.get("penalty_sums"), Mapping) else {}
    penalty_counts = item.get("penalty_counts") if isinstance(item.get("penalty_counts"), Mapping) else {}
    out["avg_penalties"] = {
        key: (
            float(penalty_sums.get(key) or 0.0) / int(penalty_counts.get(key) or 0)
            if int(penalty_counts.get(key) or 0) > 0
            else None
        )
        for key in PENALTY_KEYS
    }
    out["reason_code_top"] = _top_counts(item.get("reason_code_counts"))
    out["latest_reason_code_top"] = _top_counts(item.get("latest_reason_code_counts"))
    out["entry_usable_edge_na_reason_top"] = _top_counts(
        item.get("entry_usable_edge_na_reason_counts")
    )
    out["reason_code_breakdown"] = _finalize_breakdown(
        item.get("reason_code_breakdown_raw")
    )
    out["latest_reason_code_breakdown"] = _finalize_breakdown(
        item.get("latest_reason_code_breakdown_raw")
    )
    out["entry_usable_edge_na_reason_breakdown"] = _finalize_breakdown(
        item.get("entry_usable_edge_na_reason_breakdown_raw")
    )
    out["missed_winner_reason_code_top"] = _top_counts(item.get("missed_winner_reason_code_counts"))
    out["missed_winner_na_reason_top"] = _top_counts(item.get("missed_winner_na_reason_counts"))
    out["entry_false_edge_type_top"] = _top_counts(item.get("entry_false_edge_type_counts"))
    out["entry_false_edge_root_cause_top"] = _top_counts(item.get("entry_false_edge_root_cause_counts"))
    out["missed_winner_false_edge_type_top"] = _top_counts(
        item.get("missed_winner_false_edge_type_counts")
    )
    out["missed_winner_false_edge_root_cause_top"] = _top_counts(
        item.get("missed_winner_false_edge_root_cause_counts")
    )
    for key in (
        "entry_usable_edges",
        "size_scales",
        "exit_slippages",
        "repricing_p_realize_values",
        "repricing_p_converge_values",
        "repricing_p_fill_entry_values",
        "repricing_p_fill_exit_values",
        "repricing_evidence_scales",
    ):
        out.pop(key, None)
    for key in (
        "penalty_sums",
        "penalty_counts",
        "reason_code_counts",
        "latest_reason_code_counts",
        "entry_usable_edge_na_reason_counts",
        "reason_code_breakdown_raw",
        "latest_reason_code_breakdown_raw",
        "entry_usable_edge_na_reason_breakdown_raw",
        "missed_winner_reason_code_counts",
        "missed_winner_na_reason_counts",
        "entry_false_edge_type_counts",
        "entry_false_edge_root_cause_counts",
        "missed_winner_false_edge_type_counts",
        "missed_winner_false_edge_root_cause_counts",
        "repricing_evidence_status_counts",
        "repricing_evidence_na_reason_counts",
        "repricing_evidence_missing_reason_counts",
        "repricing_evidence_probability_source_counts",
        "repricing_evidence_missing_table_counts",
        "repricing_evidence_default_table_counts",
        "repricing_evidence_low_sample_table_counts",
        "repricing_evidence_bucket_samples_min_values",
        "repricing_evidence_component_stats",
    ):
        out.pop(key, None)
    flags: List[str] = []
    missed_missing = int(item.get("missed_winner_entry_usable_edge_missing_count") or 0)
    missed_positive = int(item.get("missed_winner_entry_usable_edge_positive_count") or 0)
    missed_nonpositive = int(item.get("missed_winner_entry_usable_edge_nonpositive_count") or 0)
    missed_size_zero = int(item.get("missed_winner_size_scale_zero_count") or 0)
    missed_with_new_edge = missed_positive + missed_nonpositive
    if rejected_settled >= 5 and out["missed_winner_rate"] is not None and out["missed_winner_rate"] >= 0.55:
        if rejected_hit > 0 and missed_with_new_edge <= 0 and missed_missing >= rejected_hit:
            flags.append("legacy_data_missing_entry_usable_edge")
        elif missed_nonpositive > 0 or missed_size_zero > 0:
            flags.append("threshold_or_soft_penalty_too_strict")
        else:
            flags.append("threshold_review_insufficient_new_edge_attribution")
    if bought_settled >= 5 and out["false_positive_rate"] is not None and out["false_positive_rate"] >= 0.55:
        flags.append("strategy_or_model_quality_risk")
    if int(item["entry_usable_edge_silent_missing_count"]) > 0:
        flags.append("legacy_or_silent_entry_usable_edge_missing")
    if filled >= 3 and out["exit_fill_rate"] is not None and out["exit_fill_rate"] <= 0.30:
        flags.append("exit_liquidity_risk")
    if int(item["entry_false_edge_missed_winner_count"]) >= 3:
        flags.append("false_edge_filter_needs_review")
    if int(item["friction_false_edge_missed_winner_count"]) >= 3:
        flags.append("repricing_friction_filter_needs_exit_replay")
    if int(item.get("repricing_evidence_missing_count") or 0) >= 3:
        flags.append("repricing_exit_evidence_missing")
    if int(item.get("repricing_evidence_component_missing_count") or 0) >= 3:
        flags.append("repricing_exit_component_missing")
    if int(item.get("repricing_evidence_default_only_count") or 0) >= 3:
        flags.append("repricing_exit_default_only")
    if int(item.get("repricing_evidence_low_sample_count") or 0) >= 3:
        flags.append("repricing_exit_evidence_low_sample")
    if int(item.get("repricing_evidence_unreported_component_count") or 0) >= 3:
        flags.append("repricing_exit_component_unreported")
    if int(item.get("repricing_probability_too_low_count") or 0) >= 3:
        flags.append("repricing_exit_probability_too_low")
    if int(item["station_missing_count"]) > 0:
        flags.append("station_mapping_risk")
    out["diagnostic_flags"] = flags
    return out


def _answer_status(value: str, evidence_count: int = 0) -> Dict[str, Any]:
    if evidence_count <= 0 and value not in {"yes", "no"}:
        return {"answer": "insufficient_data", "confidence": "low"}
    if value == "yes":
        return {"answer": "yes", "confidence": "medium" if evidence_count < 10 else "high"}
    if value == "no":
        return {"answer": "no", "confidence": "medium" if evidence_count < 10 else "high"}
    if value == "mixed":
        return {"answer": "mixed", "confidence": "medium" if evidence_count >= 5 else "low"}
    return {"answer": "insufficient_data", "confidence": "low"}


def _diagnose_stats(stats: Mapping[str, Any]) -> Dict[str, Any]:
    candidate_count = _as_int(stats.get("candidate_count"))
    bought_settled = _as_int(stats.get("bought_settled_count"))
    rejected_settled = _as_int(stats.get("rejected_only_settled_count"))
    filled = _as_int(stats.get("filled_entry_count"))
    false_positive_rate = _as_float(stats.get("false_positive_rate"))
    missed_winner_rate = _as_float(stats.get("missed_winner_rate"))
    exit_fill_rate = _as_float(stats.get("exit_fill_rate"))
    avg_exit_slippage = _as_float(stats.get("avg_exit_slippage"))
    low_price_exit_fill_rate = _as_float(stats.get("low_price_exit_fill_rate"))
    station_missing = _as_int(stats.get("station_missing_count"))
    contract_missing = _as_int(stats.get("contract_rule_missing_count"))
    missed_positive = _as_int(stats.get("missed_winner_entry_usable_edge_positive_count"))
    missed_nonpositive = _as_int(stats.get("missed_winner_entry_usable_edge_nonpositive_count"))
    missed_size_zero = _as_int(stats.get("missed_winner_size_scale_zero_count"))
    false_edge_missed = _as_int(stats.get("entry_false_edge_missed_winner_count"))
    friction_false_edge_missed = _as_int(stats.get("friction_false_edge_missed_winner_count"))
    repricing_candidate_count = _as_int(stats.get("repricing_candidate_count"))
    repricing_evidence_missing_count = _as_int(
        stats.get("repricing_evidence_missing_count")
    )
    repricing_evidence_component_missing_count = _as_int(
        stats.get("repricing_evidence_component_missing_count")
    )
    repricing_evidence_default_only_count = _as_int(
        stats.get("repricing_evidence_default_only_count")
    )
    repricing_evidence_status_missing_count = _as_int(
        stats.get("repricing_evidence_status_missing_count")
    )
    repricing_evidence_status_default_only_count = _as_int(
        stats.get("repricing_evidence_status_default_only_count")
    )
    repricing_evidence_missing_table_candidate_count = _as_int(
        stats.get("repricing_evidence_missing_table_candidate_count")
    )
    repricing_evidence_default_table_candidate_count = _as_int(
        stats.get("repricing_evidence_default_table_candidate_count")
    )
    repricing_evidence_entry_missing_flag_count = _as_int(
        stats.get("repricing_evidence_entry_missing_flag_count")
    )
    repricing_evidence_realize_missing_flag_count = _as_int(
        stats.get("repricing_evidence_realize_missing_flag_count")
    )
    repricing_evidence_realize_default_flag_count = _as_int(
        stats.get("repricing_evidence_realize_default_flag_count")
    )
    repricing_evidence_missing_residual_count = _as_int(
        stats.get("repricing_evidence_missing_residual_count")
    )
    repricing_evidence_low_sample_count = _as_int(
        stats.get("repricing_evidence_low_sample_count")
    )
    repricing_evidence_unreported_component_count = _as_int(
        stats.get("repricing_evidence_unreported_component_count")
    )
    repricing_probability_too_low_count = _as_int(
        stats.get("repricing_probability_too_low_count")
    )
    repricing_evidence_missing_rate = _as_float(
        stats.get("repricing_evidence_missing_rate")
    )
    repricing_evidence_component_missing_rate = _as_float(
        stats.get("repricing_evidence_component_missing_rate")
    )
    repricing_evidence_default_only_rate = _as_float(
        stats.get("repricing_evidence_default_only_rate")
    )
    repricing_evidence_status_missing_rate = _as_float(
        stats.get("repricing_evidence_status_missing_rate")
    )
    repricing_evidence_status_default_only_rate = _as_float(
        stats.get("repricing_evidence_status_default_only_rate")
    )
    repricing_evidence_missing_table_candidate_rate = _as_float(
        stats.get("repricing_evidence_missing_table_candidate_rate")
    )
    repricing_evidence_default_table_candidate_rate = _as_float(
        stats.get("repricing_evidence_default_table_candidate_rate")
    )
    repricing_evidence_entry_missing_flag_rate = _as_float(
        stats.get("repricing_evidence_entry_missing_flag_rate")
    )
    repricing_evidence_realize_missing_flag_rate = _as_float(
        stats.get("repricing_evidence_realize_missing_flag_rate")
    )
    repricing_evidence_realize_default_flag_rate = _as_float(
        stats.get("repricing_evidence_realize_default_flag_rate")
    )
    repricing_evidence_missing_residual_rate = _as_float(
        stats.get("repricing_evidence_missing_residual_rate")
    )
    repricing_evidence_low_sample_rate = _as_float(
        stats.get("repricing_evidence_low_sample_rate")
    )
    repricing_evidence_unreported_component_rate = _as_float(
        stats.get("repricing_evidence_unreported_component_rate")
    )
    repricing_probability_too_low_rate = _as_float(
        stats.get("repricing_probability_too_low_rate")
    )
    flags = [str(x) for x in stats.get("diagnostic_flags") or []]

    strategy_wrong = "insufficient"
    if bought_settled >= 5 and false_positive_rate is not None:
        strategy_wrong = "yes" if false_positive_rate >= 0.55 else "no"

    threshold_too_strict = "insufficient"
    if rejected_settled >= 5 and missed_winner_rate is not None:
        if missed_winner_rate >= 0.55 and (missed_nonpositive > 0 or missed_size_zero > 0 or missed_positive > 0):
            threshold_too_strict = "yes"
        elif missed_winner_rate < 0.35:
            threshold_too_strict = "no"
        else:
            threshold_too_strict = "mixed"

    exit_difficult = "insufficient"
    if filled >= 3 and exit_fill_rate is not None:
        if exit_fill_rate <= 0.30:
            exit_difficult = "yes"
        elif exit_fill_rate >= 0.70:
            exit_difficult = "no"
        else:
            exit_difficult = "mixed"
    if low_price_exit_fill_rate is not None and low_price_exit_fill_rate <= 0.30:
        exit_difficult = "yes"
    if avg_exit_slippage is not None and avg_exit_slippage > 0.02:
        exit_difficult = "yes"
    if repricing_candidate_count > 0 and (
        repricing_evidence_missing_count > 0
        or repricing_evidence_component_missing_count > 0
        or repricing_evidence_default_only_count > 0
        or repricing_evidence_status_missing_count > 0
        or repricing_evidence_status_default_only_count > 0
        or repricing_evidence_missing_table_candidate_count > 0
        or repricing_evidence_default_table_candidate_count > 0
        or repricing_evidence_entry_missing_flag_count > 0
        or repricing_evidence_realize_missing_flag_count > 0
        or repricing_evidence_realize_default_flag_count > 0
        or repricing_evidence_missing_residual_count > 0
        or repricing_evidence_low_sample_count > 0
        or repricing_evidence_unreported_component_count > 0
        or repricing_probability_too_low_count > 0
    ):
        exit_difficult = "yes"

    mapping_wrong = "yes" if (station_missing > 0 or contract_missing > 0) else "no"
    if candidate_count <= 0:
        mapping_wrong = "insufficient"

    exit_notes: List[str] = []
    if friction_false_edge_missed > 0:
        exit_notes.append("repricing_friction_filter_blocked_later_winners")
    if false_edge_missed > 0:
        exit_notes.append("false_edge_filter_blocked_later_winners")
    if low_price_exit_fill_rate is not None:
        exit_notes.append("low_price_exit_fill_rate_observed")
    if repricing_evidence_missing_count > 0:
        exit_notes.append("repricing_exit_evidence_missing")
    if repricing_evidence_component_missing_count > 0:
        exit_notes.append("repricing_exit_component_missing")
    if repricing_evidence_default_only_count > 0:
        exit_notes.append("repricing_exit_default_only")
    if repricing_evidence_status_missing_count > 0:
        exit_notes.append("repricing_exit_status_missing")
    if repricing_evidence_status_default_only_count > 0:
        exit_notes.append("repricing_exit_status_default_only")
    if repricing_evidence_missing_table_candidate_count > 0:
        exit_notes.append("repricing_exit_missing_table")
    if repricing_evidence_default_table_candidate_count > 0:
        exit_notes.append("repricing_exit_default_table")
    if repricing_evidence_entry_missing_flag_count > 0:
        exit_notes.append("repricing_exit_entry_missing_flag")
    if repricing_evidence_realize_missing_flag_count > 0:
        exit_notes.append("repricing_exit_realize_missing_flag")
    if repricing_evidence_realize_default_flag_count > 0:
        exit_notes.append("repricing_exit_realize_default_flag")
    if repricing_evidence_missing_residual_count > 0:
        exit_notes.append("repricing_exit_missing_residual")
    if repricing_evidence_low_sample_count > 0:
        exit_notes.append("repricing_exit_evidence_low_sample")
    if repricing_evidence_unreported_component_count > 0:
        exit_notes.append("repricing_exit_component_unreported")
    if repricing_probability_too_low_count > 0:
        exit_notes.append("repricing_exit_probability_too_low")

    return {
        "is_strategy_wrong": {
            **_answer_status(strategy_wrong, bought_settled),
            "false_positive_rate": false_positive_rate,
            "bought_settled_count": bought_settled,
        },
        "are_thresholds_too_strict": {
            **_answer_status(threshold_too_strict, rejected_settled),
            "missed_winner_rate": missed_winner_rate,
            "rejected_only_settled_count": rejected_settled,
            "missed_winner_entry_usable_edge_positive_count": missed_positive,
            "missed_winner_entry_usable_edge_nonpositive_count": missed_nonpositive,
            "missed_winner_size_scale_zero_count": missed_size_zero,
        },
        "is_exit_difficult": {
            **_answer_status(exit_difficult, max(filled, repricing_candidate_count)),
            "exit_fill_rate": exit_fill_rate,
            "avg_exit_slippage": avg_exit_slippage,
            "low_price_exit_fill_rate": low_price_exit_fill_rate,
            "filled_entry_count": filled,
            "repricing_candidate_count": repricing_candidate_count,
            "repricing_evidence_missing_count": repricing_evidence_missing_count,
            "repricing_evidence_missing_rate": repricing_evidence_missing_rate,
            "repricing_evidence_component_missing_count": repricing_evidence_component_missing_count,
            "repricing_evidence_component_missing_rate": repricing_evidence_component_missing_rate,
            "repricing_evidence_default_only_count": repricing_evidence_default_only_count,
            "repricing_evidence_default_only_rate": repricing_evidence_default_only_rate,
            "repricing_evidence_status_missing_count": repricing_evidence_status_missing_count,
            "repricing_evidence_status_missing_rate": repricing_evidence_status_missing_rate,
            "repricing_evidence_status_default_only_count": repricing_evidence_status_default_only_count,
            "repricing_evidence_status_default_only_rate": repricing_evidence_status_default_only_rate,
            "repricing_evidence_missing_table_candidate_count": repricing_evidence_missing_table_candidate_count,
            "repricing_evidence_missing_table_candidate_rate": repricing_evidence_missing_table_candidate_rate,
            "repricing_evidence_default_table_candidate_count": repricing_evidence_default_table_candidate_count,
            "repricing_evidence_default_table_candidate_rate": repricing_evidence_default_table_candidate_rate,
            "repricing_evidence_entry_missing_flag_count": repricing_evidence_entry_missing_flag_count,
            "repricing_evidence_entry_missing_flag_rate": repricing_evidence_entry_missing_flag_rate,
            "repricing_evidence_realize_missing_flag_count": repricing_evidence_realize_missing_flag_count,
            "repricing_evidence_realize_missing_flag_rate": repricing_evidence_realize_missing_flag_rate,
            "repricing_evidence_realize_default_flag_count": repricing_evidence_realize_default_flag_count,
            "repricing_evidence_realize_default_flag_rate": repricing_evidence_realize_default_flag_rate,
            "repricing_evidence_missing_residual_count": repricing_evidence_missing_residual_count,
            "repricing_evidence_missing_residual_rate": repricing_evidence_missing_residual_rate,
            "repricing_evidence_low_sample_count": repricing_evidence_low_sample_count,
            "repricing_evidence_low_sample_rate": repricing_evidence_low_sample_rate,
            "repricing_evidence_unreported_component_count": repricing_evidence_unreported_component_count,
            "repricing_evidence_unreported_component_rate": repricing_evidence_unreported_component_rate,
            "repricing_probability_too_low_count": repricing_probability_too_low_count,
            "repricing_probability_too_low_rate": repricing_probability_too_low_rate,
            "notes": exit_notes,
        },
        "is_model_or_station_mapping_wrong": {
            **_answer_status(mapping_wrong, candidate_count),
            "station_missing_count": station_missing,
            "contract_rule_missing_count": contract_missing,
        },
        "primary_diagnostic_flags": flags,
    }


def _build_counterfactual_answers(
    overall: Mapping[str, Any],
    by_strategy: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    strategy_answers = {
        str(key): _diagnose_stats(value)
        for key, value in sorted(by_strategy.items(), key=lambda kv: str(kv[0]))
        if isinstance(value, Mapping)
    }
    return {
        "overall": _diagnose_stats(overall),
        "by_strategy": strategy_answers,
        "questions": [
            "is_strategy_wrong",
            "are_thresholds_too_strict",
            "is_exit_difficult",
            "is_model_or_station_mapping_wrong",
        ],
    }


def _strategy_kpi_row(strategy: str, stats: Mapping[str, Any]) -> Dict[str, Any]:
    bought_settled = _as_int(stats.get("bought_settled_count"))
    bought_hit = _as_int(stats.get("bought_hit_count"))
    rejected_settled = _as_int(stats.get("rejected_only_settled_count"))
    rejected_hit = _as_int(stats.get("rejected_only_hit_count"))
    soft_settled = _as_int(stats.get("soft_down_settled_count"))
    soft_hit = _as_int(stats.get("soft_down_hit_count"))
    soft_rejected_settled = _as_int(stats.get("soft_down_rejected_settled_count"))
    soft_rejected_hit = _as_int(stats.get("soft_down_rejected_hit_count"))
    soft_filled = _as_int(stats.get("soft_down_filled_count"))
    soft_exit_filled = _as_int(stats.get("soft_down_exit_filled_count"))
    low_price_settled = _as_int(stats.get("low_price_settled_count"))
    low_price_hit = _as_int(stats.get("low_price_hit_count"))
    low_price_filled = _as_int(stats.get("low_price_filled_count"))
    low_price_exit_filled = _as_int(stats.get("low_price_exit_filled_count"))
    return {
        "strategy": str(strategy or "unknown"),
        "candidate_count": _as_int(stats.get("candidate_count")),
        "settled_count": _as_int(stats.get("settled_count")),
        "will_order_count": _as_int(stats.get("will_order_count")),
        "filled_entry_count": _as_int(stats.get("filled_entry_count")),
        "missed_winner_count": rejected_hit,
        "missed_winner_rejected_settled_count": rejected_settled,
        "missed_winner_rate": _as_float(stats.get("missed_winner_rate")),
        "false_positive_count": max(0, bought_settled - bought_hit),
        "false_positive_bought_settled_count": bought_settled,
        "false_positive_rate": _as_float(stats.get("false_positive_rate")),
        "exit_fill_rate": _as_float(stats.get("exit_fill_rate")),
        "avg_exit_slippage": _as_float(stats.get("avg_exit_slippage")),
        "low_price_count": _as_int(stats.get("low_price_count")),
        "low_price_settled_count": low_price_settled,
        "low_price_hit_count": low_price_hit,
        "low_price_hit_rate": _as_float(stats.get("low_price_hit_rate")),
        "low_price_filled_count": low_price_filled,
        "low_price_exit_filled_count": low_price_exit_filled,
        "low_price_exit_success_rate": _as_float(stats.get("low_price_exit_fill_rate")),
        "soft_penalty_count": _as_int(stats.get("soft_down_count")),
        "soft_penalty_settled_count": soft_settled,
        "soft_penalty_hit_count": soft_hit,
        "soft_penalty_hit_rate": _as_float(stats.get("soft_down_hit_rate")),
        "soft_penalty_rejected_settled_count": soft_rejected_settled,
        "soft_penalty_rejected_hit_count": soft_rejected_hit,
        "soft_penalty_missed_winner_rate": _as_float(
            stats.get("soft_down_missed_winner_rate")
        ),
        "soft_penalty_filled_count": soft_filled,
        "soft_penalty_exit_filled_count": soft_exit_filled,
        "soft_penalty_exit_fill_rate": _as_float(stats.get("soft_down_exit_fill_rate")),
        "repricing_candidate_count": _as_int(stats.get("repricing_candidate_count")),
        "repricing_evidence_missing_count": _as_int(
            stats.get("repricing_evidence_missing_count")
        ),
        "repricing_evidence_missing_rate": _as_float(
            stats.get("repricing_evidence_missing_rate")
        ),
        "repricing_evidence_component_missing_count": _as_int(
            stats.get("repricing_evidence_component_missing_count")
        ),
        "repricing_evidence_component_missing_rate": _as_float(
            stats.get("repricing_evidence_component_missing_rate")
        ),
        "repricing_evidence_default_only_count": _as_int(
            stats.get("repricing_evidence_default_only_count")
        ),
        "repricing_evidence_default_only_rate": _as_float(
            stats.get("repricing_evidence_default_only_rate")
        ),
        "repricing_evidence_status_missing_count": _as_int(
            stats.get("repricing_evidence_status_missing_count")
        ),
        "repricing_evidence_status_missing_rate": _as_float(
            stats.get("repricing_evidence_status_missing_rate")
        ),
        "repricing_evidence_status_default_only_count": _as_int(
            stats.get("repricing_evidence_status_default_only_count")
        ),
        "repricing_evidence_status_default_only_rate": _as_float(
            stats.get("repricing_evidence_status_default_only_rate")
        ),
        "repricing_evidence_missing_table_candidate_count": _as_int(
            stats.get("repricing_evidence_missing_table_candidate_count")
        ),
        "repricing_evidence_missing_table_candidate_rate": _as_float(
            stats.get("repricing_evidence_missing_table_candidate_rate")
        ),
        "repricing_evidence_default_table_candidate_count": _as_int(
            stats.get("repricing_evidence_default_table_candidate_count")
        ),
        "repricing_evidence_default_table_candidate_rate": _as_float(
            stats.get("repricing_evidence_default_table_candidate_rate")
        ),
        "repricing_evidence_entry_missing_flag_count": _as_int(
            stats.get("repricing_evidence_entry_missing_flag_count")
        ),
        "repricing_evidence_entry_missing_flag_rate": _as_float(
            stats.get("repricing_evidence_entry_missing_flag_rate")
        ),
        "repricing_evidence_realize_missing_flag_count": _as_int(
            stats.get("repricing_evidence_realize_missing_flag_count")
        ),
        "repricing_evidence_realize_missing_flag_rate": _as_float(
            stats.get("repricing_evidence_realize_missing_flag_rate")
        ),
        "repricing_evidence_realize_default_flag_count": _as_int(
            stats.get("repricing_evidence_realize_default_flag_count")
        ),
        "repricing_evidence_realize_default_flag_rate": _as_float(
            stats.get("repricing_evidence_realize_default_flag_rate")
        ),
        "repricing_evidence_missing_residual_count": _as_int(
            stats.get("repricing_evidence_missing_residual_count")
        ),
        "repricing_evidence_missing_residual_rate": _as_float(
            stats.get("repricing_evidence_missing_residual_rate")
        ),
        "repricing_evidence_low_sample_count": _as_int(
            stats.get("repricing_evidence_low_sample_count")
        ),
        "repricing_evidence_low_sample_rate": _as_float(
            stats.get("repricing_evidence_low_sample_rate")
        ),
        "repricing_evidence_unreported_component_count": _as_int(
            stats.get("repricing_evidence_unreported_component_count")
        ),
        "repricing_evidence_unreported_component_rate": _as_float(
            stats.get("repricing_evidence_unreported_component_rate")
        ),
        "repricing_evidence_all_bucket_evidence_count": _as_int(
            stats.get("repricing_evidence_all_bucket_evidence_count")
        ),
        "repricing_evidence_all_bucket_evidence_rate": _as_float(
            stats.get("repricing_evidence_all_bucket_evidence_rate")
        ),
        "repricing_probability_too_low_count": _as_int(
            stats.get("repricing_probability_too_low_count")
        ),
        "repricing_probability_too_low_rate": _as_float(
            stats.get("repricing_probability_too_low_rate")
        ),
        "avg_repricing_p_realize": _as_float(stats.get("avg_repricing_p_realize")),
        "avg_repricing_p_converge": _as_float(stats.get("avg_repricing_p_converge")),
        "avg_repricing_p_fill_entry": _as_float(
            stats.get("avg_repricing_p_fill_entry")
        ),
        "avg_repricing_p_fill_exit": _as_float(
            stats.get("avg_repricing_p_fill_exit")
        ),
        "avg_repricing_evidence_scale": _as_float(
            stats.get("avg_repricing_evidence_scale")
        ),
        "avg_repricing_target_surplus_raw": _as_float(
            stats.get("avg_repricing_target_surplus_raw")
        ),
        "avg_repricing_target_surplus_discounted": _as_float(
            stats.get("avg_repricing_target_surplus_discounted")
        ),
        "avg_repricing_target_surplus_discounted_to_friction": _as_float(
            stats.get("avg_repricing_target_surplus_discounted_to_friction")
        ),
        "avg_repricing_target_surplus_to_friction": _as_float(
            stats.get("avg_repricing_target_surplus_to_friction")
        ),
        "avg_repricing_friction_floor": _as_float(
            stats.get("avg_repricing_friction_floor")
        ),
        "diagnostic_flags": [
            str(flag) for flag in (stats.get("diagnostic_flags") or []) if str(flag).strip()
        ],
    }


def _build_strategy_kpis(
    overall: Mapping[str, Any],
    by_strategy: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    rows = [
        _strategy_kpi_row(strategy, stats)
        for strategy, stats in sorted(by_strategy.items(), key=lambda kv: str(kv[0]))
        if isinstance(stats, Mapping)
    ]
    rows.sort(
        key=lambda row: (
            -int(row.get("missed_winner_count") or 0),
            -int(row.get("false_positive_count") or 0),
            -int(row.get("candidate_count") or 0),
            str(row.get("strategy") or ""),
        )
    )
    return {
        "overall": _strategy_kpi_row("overall", overall),
        "by_strategy": {str(row["strategy"]): row for row in rows},
        "rows": rows,
        "field_contract": [
            "missed_winner_rate",
            "false_positive_rate",
            "exit_fill_rate",
            "avg_exit_slippage",
            "low_price_hit_rate",
            "low_price_exit_success_rate",
            "soft_penalty_hit_rate",
            "soft_penalty_missed_winner_rate",
            "soft_penalty_exit_fill_rate",
            "repricing_evidence_missing_rate",
            "repricing_evidence_component_missing_rate",
            "repricing_evidence_default_only_rate",
            "repricing_evidence_status_missing_rate",
            "repricing_evidence_status_default_only_rate",
            "repricing_evidence_missing_table_candidate_rate",
            "repricing_evidence_default_table_candidate_rate",
            "repricing_evidence_entry_missing_flag_rate",
            "repricing_evidence_realize_missing_flag_rate",
            "repricing_evidence_realize_default_flag_rate",
            "repricing_evidence_missing_residual_rate",
            "repricing_evidence_low_sample_rate",
            "repricing_evidence_unreported_component_rate",
            "repricing_evidence_all_bucket_evidence_rate",
            "repricing_probability_too_low_rate",
            "avg_repricing_p_realize",
            "avg_repricing_p_converge",
            "avg_repricing_p_fill_entry",
            "avg_repricing_p_fill_exit",
            "avg_repricing_evidence_scale",
            "avg_repricing_target_surplus_raw",
            "avg_repricing_target_surplus_discounted",
            "avg_repricing_target_surplus_discounted_to_friction",
            "avg_repricing_target_surplus_to_friction",
            "avg_repricing_friction_floor",
        ],
    }


ACTIONABLE_DIAGNOSIS_CONFIG = {
    "model_or_station_mapping_gap": {
        "question": "is_model_or_station_mapping_wrong",
        "recommended_action": "repair_station_or_contract_mapping",
        "next_mainline_task": "station_and_contract_mapping_audit",
    },
    "exit_execution_evidence_gap": {
        "question": "is_exit_difficult",
        "recommended_action": "repair_repricing_execution_evidence",
        "next_mainline_task": "repricing_bucket_execution_evidence_chain",
    },
    "threshold_or_soft_penalty_too_strict": {
        "question": "are_thresholds_too_strict",
        "recommended_action": "review_thresholds_and_soft_penalties",
        "next_mainline_task": "threshold_and_soft_penalty_replay",
    },
    "strategy_or_model_quality_risk": {
        "question": "is_strategy_wrong",
        "recommended_action": "review_strategy_model_quality",
        "next_mainline_task": "strategy_quality_counterfactual_review",
    },
}
ACTIONABLE_DIAGNOSIS_ORDER = (
    "model_or_station_mapping_gap",
    "exit_execution_evidence_gap",
    "threshold_or_soft_penalty_too_strict",
    "strategy_or_model_quality_risk",
)
ACTIONABLE_DIAGNOSIS_RANK = {
    key: idx for idx, key in enumerate(ACTIONABLE_DIAGNOSIS_ORDER)
}
ACTIONABLE_DIAGNOSIS_RANK.update(
    {
        "needs_more_data": len(ACTIONABLE_DIAGNOSIS_ORDER),
        "repricing_probability_quality_review": len(ACTIONABLE_DIAGNOSIS_ORDER) + 1,
        "repricing_probability_low_ev_confirmed": len(ACTIONABLE_DIAGNOSIS_ORDER) + 2,
        "historical_repricing_evidence_residual": len(ACTIONABLE_DIAGNOSIS_ORDER) + 3,
        "no_actionable_issue_detected": len(ACTIONABLE_DIAGNOSIS_ORDER) + 4,
    }
)

CURRENT_RUNTIME_REPRICING_CHAIN_GAP_KEYS = (
    "repricing_evidence_missing_count",
    "repricing_evidence_defaultish_count",
    "repricing_evidence_missing_tables_count",
    "repricing_evidence_component_missing_count",
    "repricing_evidence_component_default_only_count",
    "repricing_evidence_component_unreported_count",
)
CURRENT_RUNTIME_REPRICING_QUALITY_KEYS = (
    "repricing_evidence_low_sample_count",
    "repricing_probability_too_low_count",
)
CURRENT_RUNTIME_REPRICING_MATERIAL_LOW_SAMPLE_RATE = 0.20
CURRENT_RUNTIME_REPRICING_MIN_BUCKET_EVIDENCE_RATE = 0.80


def _answer_dict(answers: Mapping[str, Any], question: str) -> Mapping[str, Any]:
    value = answers.get(question) if isinstance(answers, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _answer_value(answers: Mapping[str, Any], question: str) -> str:
    return str(_answer_dict(answers, question).get("answer") or "").strip().lower()


def _answer_confidence(answers: Mapping[str, Any], question: str) -> str:
    return str(_answer_dict(answers, question).get("confidence") or "low").strip().lower()


def _answer_snapshot(answers: Mapping[str, Any]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for question in (
        "is_strategy_wrong",
        "are_thresholds_too_strict",
        "is_exit_difficult",
        "is_model_or_station_mapping_wrong",
    ):
        answer = _answer_dict(answers, question)
        out[question] = {
            "answer": str(answer.get("answer") or "insufficient_data"),
            "confidence": str(answer.get("confidence") or "low"),
        }
    return out


def _evidence_item(metric: str, value: Any, *, source: str = "strategy_kpis") -> Dict[str, Any]:
    return {"metric": str(metric), "value": value, "source": str(source)}


def _append_metric_evidence(
    evidence: List[Dict[str, Any]],
    kpi: Mapping[str, Any],
    metric: str,
) -> None:
    if not isinstance(kpi, Mapping) or metric not in kpi:
        return
    value = kpi.get(metric)
    if value is None:
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == 0.0:
        return
    evidence.append(_evidence_item(metric, value))


def _append_answer_metric_evidence(
    evidence: List[Dict[str, Any]],
    answers: Mapping[str, Any],
    question: str,
    metric: str,
) -> None:
    answer = _answer_dict(answers, question)
    if metric not in answer:
        return
    value = answer.get(metric)
    if value is None:
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == 0.0:
        return
    evidence.append(_evidence_item(metric, value, source=f"counterfactual_answers.{question}"))


def _build_actionable_evidence(
    diagnosis: str,
    answers: Mapping[str, Any],
    kpi: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    if diagnosis == "model_or_station_mapping_gap":
        _append_answer_metric_evidence(
            evidence, answers, "is_model_or_station_mapping_wrong", "station_missing_count"
        )
        _append_answer_metric_evidence(
            evidence, answers, "is_model_or_station_mapping_wrong", "contract_rule_missing_count"
        )
        _append_metric_evidence(evidence, kpi, "candidate_count")
    elif diagnosis == "exit_execution_evidence_gap":
        for metric in (
            "repricing_candidate_count",
            "repricing_evidence_missing_count",
            "repricing_evidence_missing_rate",
            "repricing_evidence_component_missing_count",
            "repricing_evidence_component_missing_rate",
            "repricing_evidence_default_only_count",
            "repricing_evidence_default_only_rate",
            "repricing_evidence_low_sample_count",
            "repricing_evidence_low_sample_rate",
            "repricing_evidence_unreported_component_count",
            "repricing_evidence_unreported_component_rate",
            "repricing_probability_too_low_count",
            "repricing_probability_too_low_rate",
            "exit_fill_rate",
            "avg_exit_slippage",
            "low_price_exit_success_rate",
        ):
            _append_metric_evidence(evidence, kpi, metric)
        notes = _answer_dict(answers, "is_exit_difficult").get("notes")
        if isinstance(notes, list) and notes:
            evidence.append(
                _evidence_item(
                    "notes",
                    [str(note) for note in notes if str(note or "").strip()],
                    source="counterfactual_answers.is_exit_difficult",
                )
            )
    elif diagnosis == "threshold_or_soft_penalty_too_strict":
        for metric in (
            "missed_winner_rate",
            "missed_winner_count",
            "missed_winner_rejected_settled_count",
            "soft_penalty_missed_winner_rate",
            "soft_penalty_hit_rate",
        ):
            _append_metric_evidence(evidence, kpi, metric)
        for metric in (
            "missed_winner_entry_usable_edge_positive_count",
            "missed_winner_entry_usable_edge_nonpositive_count",
            "missed_winner_size_scale_zero_count",
        ):
            _append_answer_metric_evidence(
                evidence, answers, "are_thresholds_too_strict", metric
            )
    elif diagnosis == "strategy_or_model_quality_risk":
        for metric in (
            "false_positive_rate",
            "false_positive_count",
            "false_positive_bought_settled_count",
        ):
            _append_metric_evidence(evidence, kpi, metric)
    elif diagnosis == "repricing_probability_quality_review":
        for metric in (
            "repricing_candidate_count",
            "repricing_probability_too_low_count",
            "repricing_probability_too_low_rate",
            "repricing_evidence_low_sample_count",
            "repricing_evidence_low_sample_rate",
            "avg_repricing_p_realize",
            "avg_repricing_p_converge",
            "avg_repricing_p_fill_entry",
            "avg_repricing_p_fill_exit",
            "avg_repricing_evidence_scale",
        ):
            _append_metric_evidence(evidence, kpi, metric)
    elif diagnosis == "repricing_probability_low_ev_confirmed":
        for metric in (
            "repricing_candidate_count",
            "repricing_probability_too_low_count",
            "repricing_probability_too_low_rate",
            "repricing_evidence_low_sample_count",
            "repricing_evidence_low_sample_rate",
            "avg_repricing_p_realize",
            "avg_repricing_p_converge",
            "avg_repricing_p_fill_exit",
        ):
            _append_metric_evidence(evidence, kpi, metric)
    else:
        for metric in ("candidate_count", "settled_count", "will_order_count"):
            _append_metric_evidence(evidence, kpi, metric)
    flags = [str(flag) for flag in (kpi.get("diagnostic_flags") or []) if str(flag or "").strip()]
    if flags:
        evidence.append(_evidence_item("diagnostic_flags", flags))
    return evidence[:12]


def _current_runtime_row(
    current_runtime: Optional[Mapping[str, Any]],
    strategy: str,
) -> Mapping[str, Any]:
    runtime = current_runtime if isinstance(current_runtime, Mapping) else {}
    if not runtime:
        return {}
    if str(strategy or "") == "overall":
        row = runtime.get("overall")
        return row if isinstance(row, Mapping) else {}
    by_strategy = runtime.get("by_strategy")
    if not isinstance(by_strategy, Mapping):
        return {}
    row = by_strategy.get(str(strategy or ""))
    return row if isinstance(row, Mapping) else {}


def _current_runtime_repricing_clean(row: Mapping[str, Any]) -> bool:
    if not isinstance(row, Mapping):
        return False
    if _as_int(row.get("repricing_candidate_count")) <= 0:
        return False
    return all(_as_int(row.get(key)) <= 0 for key in CURRENT_RUNTIME_REPRICING_CHAIN_GAP_KEYS)


def _current_runtime_repricing_quality_issue(row: Mapping[str, Any]) -> bool:
    if not _current_runtime_repricing_clean(row):
        return False
    return any(_as_int(row.get(key)) > 0 for key in CURRENT_RUNTIME_REPRICING_QUALITY_KEYS)


def _current_runtime_repricing_probability_explained(row: Mapping[str, Any]) -> bool:
    if not _current_runtime_repricing_clean(row):
        return False
    candidate_count = _as_int(row.get("repricing_candidate_count"))
    if candidate_count <= 0:
        return False
    probability_too_low_count = _as_int(row.get("repricing_probability_too_low_count"))
    if probability_too_low_count <= 0:
        return False
    all_bucket_count = _as_int(row.get("repricing_all_bucket_evidence_count"))
    if _rate(all_bucket_count, candidate_count) < CURRENT_RUNTIME_REPRICING_MIN_BUCKET_EVIDENCE_RATE:
        return False
    if _rate(
        _as_int(row.get("repricing_evidence_low_sample_count")),
        candidate_count,
    ) >= CURRENT_RUNTIME_REPRICING_MATERIAL_LOW_SAMPLE_RATE:
        return False
    quality = row.get("repricing_probability_quality")
    if not isinstance(quality, Mapping):
        quality = _current_runtime_probability_quality(row)
    limiting_top = quality.get("limiting_component_top") if isinstance(quality, Mapping) else None
    if not isinstance(limiting_top, list) or not limiting_top:
        return False
    component_breakdown = quality.get("component_breakdown") if isinstance(quality, Mapping) else None
    if not isinstance(component_breakdown, Mapping):
        return False
    explained_count = 0
    for item in limiting_top:
        if not isinstance(item, Mapping):
            continue
        component = str(item.get("key") or "").strip()
        count = _as_int(item.get("count"))
        if not component or count <= 0:
            continue
        comp = component_breakdown.get(component)
        if not isinstance(comp, Mapping):
            return False
        if _as_int(comp.get("limiting_probability_count")) < count:
            return False
        if not comp.get("limiting_bucket_used_key_top"):
            return False
        explained_count += count
    return explained_count >= probability_too_low_count


def _current_runtime_evidence(row: Mapping[str, Any]) -> Dict[str, Any]:
    probability_quality = row.get("repricing_probability_quality") if isinstance(row, Mapping) else None
    if not isinstance(probability_quality, Mapping):
        probability_quality = _current_runtime_probability_quality(row)
    return {
        "run_id": _as_int(row.get("run_id")),
        "entry_rows": _as_int(row.get("entry_rows")),
        "repricing_candidate_count": _as_int(row.get("repricing_candidate_count")),
        "repricing_evidence_missing_count": _as_int(row.get("repricing_evidence_missing_count")),
        "repricing_evidence_defaultish_count": _as_int(row.get("repricing_evidence_defaultish_count")),
        "repricing_evidence_missing_tables_count": _as_int(row.get("repricing_evidence_missing_tables_count")),
        "repricing_evidence_low_sample_count": _as_int(row.get("repricing_evidence_low_sample_count")),
        "repricing_evidence_component_missing_count": _as_int(row.get("repricing_evidence_component_missing_count")),
        "repricing_evidence_component_default_only_count": _as_int(row.get("repricing_evidence_component_default_only_count")),
        "repricing_evidence_component_unreported_count": _as_int(row.get("repricing_evidence_component_unreported_count")),
        "repricing_probability_too_low_count": _as_int(row.get("repricing_probability_too_low_count")),
        "repricing_all_bucket_evidence_count": _as_int(row.get("repricing_all_bucket_evidence_count")),
        "repricing_probability_quality": probability_quality,
    }


def _actionable_row(
    strategy: str,
    answers: Mapping[str, Any],
    kpi: Mapping[str, Any],
    current_runtime: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    diagnosis = ""
    question = ""
    for candidate in ACTIONABLE_DIAGNOSIS_ORDER:
        config = ACTIONABLE_DIAGNOSIS_CONFIG[candidate]
        candidate_question = str(config["question"])
        if _answer_value(answers, candidate_question) == "yes":
            diagnosis = candidate
            question = candidate_question
            break
    if not diagnosis:
        answer_values = {
            _answer_value(answers, str(config["question"]))
            for config in ACTIONABLE_DIAGNOSIS_CONFIG.values()
        }
        if answer_values & {"mixed", "insufficient_data", "insufficient", ""}:
            diagnosis = "needs_more_data"
            question = ""
        else:
            diagnosis = "no_actionable_issue_detected"
            question = ""
    if diagnosis in ACTIONABLE_DIAGNOSIS_CONFIG:
        config = ACTIONABLE_DIAGNOSIS_CONFIG[diagnosis]
        recommended_action = str(config["recommended_action"])
        next_mainline_task = str(config["next_mainline_task"])
        confidence = _answer_confidence(answers, question)
        runtime_row = _current_runtime_row(current_runtime, strategy)
        if diagnosis == "exit_execution_evidence_gap" and _current_runtime_repricing_clean(runtime_row):
            if _current_runtime_repricing_probability_explained(runtime_row):
                diagnosis = "repricing_probability_low_ev_confirmed"
                recommended_action = "continue_repricing_probability_rolling_validation"
                next_mainline_task = "rolling_validation"
                confidence = "medium"
            elif _current_runtime_repricing_quality_issue(runtime_row):
                diagnosis = "repricing_probability_quality_review"
                recommended_action = "review_repricing_probability_quality"
                next_mainline_task = "repricing_probability_quality_review"
                confidence = "medium"
            else:
                diagnosis = "historical_repricing_evidence_residual"
                recommended_action = "continue_repricing_evidence_rolling_validation"
                next_mainline_task = "rolling_validation"
                confidence = "medium"
    elif diagnosis == "needs_more_data":
        recommended_action = "collect_more_settled_counterfactual_samples"
        next_mainline_task = "collect_more_settled_counterfactual_samples"
        confidence = "low"
    else:
        recommended_action = "continue_monitoring_current_policy"
        next_mainline_task = "rolling_validation"
        confidence = "medium" if _as_int(kpi.get("candidate_count")) > 0 else "low"
    evidence = _build_actionable_evidence(diagnosis, answers, kpi)
    runtime_row = _current_runtime_row(current_runtime, strategy)
    if diagnosis in {
        "historical_repricing_evidence_residual",
        "repricing_probability_quality_review",
        "repricing_probability_low_ev_confirmed",
    } and runtime_row:
        evidence.append(
            _evidence_item(
                "current_runtime_repricing_evidence",
                _current_runtime_evidence(runtime_row),
                source="current_runtime",
            )
        )
    return {
        "strategy": str(strategy or "overall"),
        "primary_diagnosis": diagnosis,
        "recommended_action": recommended_action,
        "next_mainline_task": next_mainline_task,
        "confidence": confidence or "low",
        "primary_question": question,
        "question_answers": _answer_snapshot(answers),
        "evidence": evidence[:12],
    }


def _build_actionable_summary(
    counterfactual_answers: Mapping[str, Any],
    strategy_kpis: Mapping[str, Any],
    current_runtime: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    overall_answers = (
        counterfactual_answers.get("overall")
        if isinstance(counterfactual_answers.get("overall"), Mapping)
        else {}
    )
    overall_kpi = (
        strategy_kpis.get("overall")
        if isinstance(strategy_kpis.get("overall"), Mapping)
        else {}
    )
    by_answer = (
        counterfactual_answers.get("by_strategy")
        if isinstance(counterfactual_answers.get("by_strategy"), Mapping)
        else {}
    )
    by_kpi = (
        strategy_kpis.get("by_strategy")
        if isinstance(strategy_kpis.get("by_strategy"), Mapping)
        else {}
    )
    by_strategy: Dict[str, Dict[str, Any]] = {}
    strategy_names = sorted(set(str(key) for key in by_answer.keys()) | set(str(key) for key in by_kpi.keys()))
    for strategy in strategy_names:
        answers = by_answer.get(strategy) if isinstance(by_answer.get(strategy), Mapping) else {}
        kpi = by_kpi.get(strategy) if isinstance(by_kpi.get(strategy), Mapping) else {}
        by_strategy[strategy] = _actionable_row(
            strategy,
            answers,
            kpi,
            current_runtime=current_runtime,
        )
    strategy_candidate_counts = {
        strategy: _as_int(by_kpi.get(strategy, {}).get("candidate_count"))
        if isinstance(by_kpi.get(strategy), Mapping)
        else 0
        for strategy in strategy_names
    }
    rows = list(by_strategy.values())
    rows.sort(
        key=lambda row: (
            ACTIONABLE_DIAGNOSIS_RANK.get(
                str(row.get("primary_diagnosis") or ""),
                len(ACTIONABLE_DIAGNOSIS_RANK),
            ),
            -strategy_candidate_counts.get(str(row.get("strategy") or ""), 0),
            str(row.get("strategy") or ""),
        )
    )
    overall_row = _actionable_row(
        "overall",
        overall_answers,
        overall_kpi,
        current_runtime=current_runtime,
    )
    return {
        "overall": overall_row,
        "by_strategy": by_strategy,
        "rows": rows,
        "next_mainline_task": overall_row.get("next_mainline_task"),
        "current_runtime": current_runtime if isinstance(current_runtime, Mapping) else {},
        "field_contract": [
            "primary_diagnosis",
            "recommended_action",
            "next_mainline_task",
            "confidence",
            "primary_question",
            "question_answers",
            "evidence",
            "current_runtime",
        ],
    }


def _new_current_runtime_row(run_id: int, strategy: str) -> Dict[str, Any]:
    return {
        "run_id": int(run_id or 0),
        "strategy": str(strategy or "overall"),
        "entry_rows": 0,
        "will_order_count": 0,
        "hard_reason_rows": 0,
        "exec_edge_base_count": 0,
        "repricing_candidate_count": 0,
        "repricing_evidence_missing_count": 0,
        "repricing_evidence_defaultish_count": 0,
        "repricing_evidence_missing_tables_count": 0,
        "repricing_evidence_low_sample_count": 0,
        "repricing_evidence_component_missing_count": 0,
        "repricing_evidence_component_default_only_count": 0,
        "repricing_evidence_component_unreported_count": 0,
        "repricing_probability_too_low_count": 0,
        "repricing_all_bucket_evidence_count": 0,
        "reason_code_counts": {},
        "repricing_probability_limiting_component_counts": {},
        "repricing_probability_component_stats": _new_repricing_component_stats(),
    }


def _current_runtime_repricing_candidate(details: Mapping[str, Any], strategy: str) -> bool:
    exit_plan = str(
        details.get("entry_edge_exit_plan")
        or details.get("weather_strategy_exit_plan")
        or details.get("entry_exit_plan")
        or details.get("exit_plan")
        or ""
    ).strip().lower()
    return bool(
        strategy == "repricing_bucket"
        or _boolish(details.get("entry_edge_exit_path_required"))
        or "repricing" in exit_plan
        or "target_exit" in exit_plan
        or "exit_target" in exit_plan
        or str(details.get("entry_edge_repricing_exit_evidence_status") or "").strip()
        or str(details.get("edge_realizable_p_realize_evidence_status") or "").strip()
        or "target_exit_execution" in str(details.get("entry_edge_false_edge_type") or "").strip()
    )


def _current_runtime_probability_quality(row: Mapping[str, Any]) -> Dict[str, Any]:
    stats = row.get("repricing_probability_component_stats") if isinstance(row, Mapping) else None
    return {
        "probability_low_threshold": REPRICING_PROBABILITY_LOW_THRESHOLD,
        "limiting_component_top": _top_counts(
            row.get("repricing_probability_limiting_component_counts")
            if isinstance(row, Mapping)
            else {}
        ),
        "component_breakdown": _finalize_repricing_component_stats(stats),
    }


def _finalize_current_runtime_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(row) if isinstance(row, Mapping) else {}
    out["repricing_probability_quality"] = _current_runtime_probability_quality(out)
    out["repricing_probability_limiting_component_top"] = _top_counts(
        out.get("repricing_probability_limiting_component_counts")
    )
    out["reason_code_top"] = _top_counts(out.get("reason_code_counts"))
    out.pop("repricing_probability_component_stats", None)
    out.pop("repricing_probability_limiting_component_counts", None)
    return out


def _inc_current_runtime_repricing(row: Dict[str, Any], details: Mapping[str, Any]) -> None:
    row["repricing_candidate_count"] += 1
    status = _normalize_repricing_evidence_status(
        details.get("entry_edge_repricing_exit_evidence_status")
        or details.get("edge_realizable_p_realize_evidence_status")
    )
    missing_tables = str(
        details.get("entry_edge_repricing_exit_evidence_missing_tables")
        or details.get("edge_realizable_p_realize_missing_tables")
        or ""
    ).strip()
    default_tables = str(
        details.get("entry_edge_repricing_exit_evidence_default_tables")
        or details.get("edge_realizable_p_realize_default_tables")
        or ""
    ).strip()
    low_sample_tables = str(
        details.get("entry_edge_repricing_exit_evidence_low_sample_tables")
        or details.get("edge_realizable_p_realize_low_sample_tables")
        or ""
    ).strip()
    missing_flag = bool(
        _boolish(details.get("entry_edge_repricing_exit_evidence_missing"))
        or _boolish(details.get("edge_realizable_p_realize_missing"))
        or _boolish(details.get("edge_realizable_p_realize_uses_default"))
        or status in {"missing", "default_only"}
        or missing_tables
        or default_tables
    )
    if missing_flag:
        row["repricing_evidence_missing_count"] += 1
    if status == "default_only" or default_tables or _boolish(details.get("edge_realizable_p_realize_uses_default")):
        row["repricing_evidence_defaultish_count"] += 1
    if missing_tables:
        row["repricing_evidence_missing_tables_count"] += 1
    if (
        status == "low_sample"
        or low_sample_tables
        or _boolish(details.get("entry_edge_repricing_exit_evidence_low_sample"))
        or _boolish(details.get("edge_realizable_p_realize_low_sample"))
    ):
        row["repricing_evidence_low_sample_count"] += 1
    probability_source_components = _parse_repricing_probability_source(
        details.get("entry_edge_repricing_exit_evidence_probability_source")
    )
    component_stats = row.get("repricing_probability_component_stats")
    if not isinstance(component_stats, dict):
        component_stats = _new_repricing_component_stats()
        row["repricing_probability_component_stats"] = component_stats
    component_items: Dict[str, Dict[str, Any]] = {}
    component_missing = False
    component_default_only = False
    component_unreported = False
    all_bucket = True
    for component in REPRICING_EVIDENCE_COMPONENTS:
        comp_stat = component_stats.setdefault(
            component,
            _new_repricing_component_stats()[component],
        )
        comp_stat["candidate_count"] += 1
        component_item = _repricing_component_from_details(
            details,
            component,
            probability_source_components.get(component),
        )
        if not component_item:
            component_unreported = True
            all_bucket = False
            continue
        component_items[component] = component_item
        comp_stat["reported_count"] += 1
        component_status = _normalize_repricing_evidence_status(component_item.get("status"))
        if component_status:
            _inc_counter(comp_stat, "status_counts", component_status)
        if component_status == "missing":
            comp_stat["missing_count"] += 1
            component_missing = True
            all_bucket = False
        elif component_status == "default_only":
            comp_stat["default_only_count"] += 1
            component_default_only = True
            all_bucket = False
        elif component_status == "low_sample":
            comp_stat["low_sample_count"] += 1
            all_bucket = False
        elif component_status == "bucket_evidence":
            comp_stat["bucket_evidence_count"] += 1
        else:
            all_bucket = False
        source = str(component_item.get("source") or "").strip()
        if source:
            _inc_counter(comp_stat, "source_counts", source)
        bucket_key = str(component_item.get("bucket_key") or "").strip()
        if bucket_key:
            _inc_counter(comp_stat, "bucket_key_counts", bucket_key)
        bucket_used_key = str(component_item.get("bucket_used_key") or "").strip()
        if bucket_used_key:
            _inc_counter(comp_stat, "bucket_used_key_counts", bucket_used_key)
        prob = _to_float(component_item.get("prob"))
        if prob is not None:
            comp_stat["prob_values"].append(float(prob))
        samples = _to_float(component_item.get("samples"))
        if samples is not None:
            comp_stat["sample_values"].append(float(samples))
    if component_missing:
        row["repricing_evidence_component_missing_count"] += 1
    if component_default_only:
        row["repricing_evidence_component_default_only_count"] += 1
    if component_unreported:
        row["repricing_evidence_component_unreported_count"] += 1
    if all_bucket:
        row["repricing_all_bucket_evidence_count"] += 1
    p_realize = _to_float(details.get("edge_realizable_p_realize"))
    probability_too_low = bool(
        p_realize is not None and p_realize < REPRICING_PROBABILITY_LOW_THRESHOLD
    )
    limiter = _repricing_probability_limiter_component(component_items)
    if probability_too_low:
        row["repricing_probability_too_low_count"] += 1
        if limiter:
            _inc_counter(row, "repricing_probability_limiting_component_counts", limiter)
    for component, component_item in component_items.items():
        comp_stat = component_stats.setdefault(
            component,
            _new_repricing_component_stats()[component],
        )
        limiting_probability = bool(probability_too_low and component == limiter)
        if probability_too_low:
            comp_stat["probability_too_low_count"] += 1
        if limiting_probability:
            comp_stat["limiting_probability_count"] += 1
            bucket_used_key = str(
                component_item.get("bucket_used_key") or component_item.get("bucket_key") or ""
            ).strip()
            if bucket_used_key:
                _inc_counter(comp_stat, "limiting_bucket_used_key_counts", bucket_used_key)
        _inc_probability_bucket_quality(
            comp_stat,
            str(component_item.get("bucket_used_key") or component_item.get("bucket_key") or ""),
            prob=_to_float(component_item.get("prob")),
            samples=_to_float(component_item.get("samples")),
            p_realize=p_realize,
            probability_too_low=probability_too_low,
            limiting_probability=limiting_probability,
        )


def _build_current_runtime_snapshot(conn: sqlite3.Connection) -> Dict[str, Any]:
    try:
        latest_row = conn.execute(
            "SELECT MAX(run_id) FROM auto_trade_decisions WHERE action = 'entry'"
        ).fetchone()
    except sqlite3.OperationalError:
        latest_row = None
    run_id = int(latest_row[0] or 0) if latest_row else 0
    if run_id <= 0:
        return {"ok": True, "run_id": 0, "overall": _new_current_runtime_row(0, "overall"), "by_strategy": {}}
    overall = _new_current_runtime_row(run_id, "overall")
    by_strategy: Dict[str, Dict[str, Any]] = {}
    try:
        rows = conn.execute(
            """
            SELECT will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            WHERE action = 'entry' AND run_id = ?
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        details = _safe_json_dict(row["details_json"])
        strategy = _strategy_family(details)
        strategy_row = by_strategy.setdefault(
            strategy,
            _new_current_runtime_row(run_id, strategy),
        )
        target_rows = (overall, strategy_row)
        reason_codes = _safe_json_list(row["reason_codes_json"])
        will_order = int(row["will_order"] or 0) != 0
        hard_reason = bool((not will_order) and reason_codes)
        exec_edge_base = str(details.get("entry_usable_edge_base_source") or "").strip() == "exec_edge_entry"
        repricing_candidate = _current_runtime_repricing_candidate(details, strategy)
        for target in target_rows:
            target["entry_rows"] += 1
            for code in reason_codes:
                _inc_counter(target, "reason_code_counts", code)
            if will_order:
                target["will_order_count"] += 1
            if hard_reason:
                target["hard_reason_rows"] += 1
            if exec_edge_base:
                target["exec_edge_base_count"] += 1
            if repricing_candidate:
                _inc_current_runtime_repricing(target, details)
    overall_out = _finalize_current_runtime_row(overall)
    by_strategy_out = {
        key: _finalize_current_runtime_row(value)
        for key, value in sorted(by_strategy.items(), key=lambda kv: str(kv[0]))
    }
    return {
        "ok": True,
        "run_id": int(run_id),
        "overall": overall_out,
        "by_strategy": by_strategy_out,
        "field_contract": [
            "run_id",
            "entry_rows",
            "hard_reason_rows",
            "exec_edge_base_count",
            "repricing_candidate_count",
            "repricing_evidence_missing_count",
            "repricing_evidence_defaultish_count",
            "repricing_evidence_missing_tables_count",
            "repricing_evidence_low_sample_count",
            "repricing_all_bucket_evidence_count",
            "repricing_probability_quality",
        ],
    }


def build_feedback_snapshot(report: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the compact machine-readable payload for feedback_snapshots.

    The full report is intentionally still written to data/reports for human
    drill-down. This compact snapshot keeps the hourly feedback table bounded
    while preserving the daily tuning contract and the four counterfactual
    answers.
    """
    window = report.get("window") if isinstance(report.get("window"), Mapping) else {}
    overall = report.get("overall") if isinstance(report.get("overall"), Mapping) else {}
    strategy_kpis = (
        report.get("strategy_kpis") if isinstance(report.get("strategy_kpis"), Mapping) else {}
    )
    rule_attribution = (
        report.get("rule_attribution")
        if isinstance(report.get("rule_attribution"), Mapping)
        else {}
    )
    rule_pruning_review = (
        report.get("rule_pruning_review")
        if isinstance(report.get("rule_pruning_review"), Mapping)
        else {}
    )
    settlement_coverage = (
        report.get("settlement_coverage")
        if isinstance(report.get("settlement_coverage"), Mapping)
        else {}
    )
    counterfactual_answers = (
        report.get("counterfactual_answers")
        if isinstance(report.get("counterfactual_answers"), Mapping)
        else {}
    )
    actionable_summary = (
        report.get("actionable_summary")
        if isinstance(report.get("actionable_summary"), Mapping)
        else {}
    )
    current_runtime = (
        report.get("current_runtime") if isinstance(report.get("current_runtime"), Mapping) else {}
    )
    try:
        days = int(window.get("days") or 0)
    except Exception:
        days = 0
    return {
        "ok": bool(report.get("ok")),
        "kind": FEEDBACK_SNAPSHOT_KIND,
        "days": int(days),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": dict(window),
        "scope": str(report.get("scope") or ""),
        "counterfactual_answers": counterfactual_answers,
        "actionable_summary": actionable_summary,
        "current_runtime": current_runtime,
        "strategy_kpis": {
            "overall": strategy_kpis.get("overall") if isinstance(strategy_kpis.get("overall"), Mapping) else {},
            "by_strategy": strategy_kpis.get("by_strategy") if isinstance(strategy_kpis.get("by_strategy"), Mapping) else {},
            "rows": strategy_kpis.get("rows") if isinstance(strategy_kpis.get("rows"), list) else [],
            "field_contract": strategy_kpis.get("field_contract") if isinstance(strategy_kpis.get("field_contract"), list) else [],
        },
        "rule_attribution": rule_attribution,
        "rule_pruning_review": rule_pruning_review,
        "settlement_coverage": settlement_coverage,
        "repricing_execution_evidence": (
            overall.get("repricing_execution_evidence")
            if isinstance(overall.get("repricing_execution_evidence"), Mapping)
            else {}
        ),
        "diagnostic_flags": [
            str(flag)
            for flag in (overall.get("diagnostic_flags") or [])
            if str(flag or "").strip()
        ],
        "field_contract": [
            "counterfactual_answers",
            "actionable_summary",
            "current_runtime",
            "strategy_kpis",
            "rule_attribution",
            "rule_pruning_review",
            "settlement_coverage",
            "repricing_execution_evidence",
            "diagnostic_flags",
        ],
    }


def _feedback_snapshot_end_date(payload: Mapping[str, Any]) -> str:
    window = payload.get("window") if isinstance(payload.get("window"), Mapping) else {}
    end_et = str(window.get("end_et") or "").strip()
    if "T" in end_et:
        return end_et.split("T", 1)[0]
    return end_et[:10]


def _ensure_feedback_snapshot_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            days INTEGER NOT NULL,
            end_date TEXT,
            payload_json TEXT NOT NULL,
            ok INTEGER NOT NULL DEFAULT 1,
            error TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feedback_snapshots_lookup
        ON feedback_snapshots(kind, days, created_at DESC)
        """
    )


def save_feedback_snapshot(
    db_path: Path,
    payload: Mapping[str, Any],
    *,
    kind: str = FEEDBACK_SNAPSHOT_KIND,
    days: Optional[int] = None,
    end_date: str = "",
) -> Dict[str, Any]:
    try:
        resolved_days = int(days if days is not None else payload.get("days") or 0)
    except Exception:
        resolved_days = 0
    resolved_end_date = str(end_date or _feedback_snapshot_end_date(payload) or "")
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        _ensure_feedback_snapshot_table(conn)
        conn.execute(
            """
            INSERT INTO feedback_snapshots(kind, days, end_date, payload_json, ok, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(kind or FEEDBACK_SNAPSHOT_KIND),
                int(resolved_days),
                resolved_end_date,
                json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":")),
                1 if bool(payload.get("ok")) else 0,
                "" if bool(payload.get("ok")) else str(payload.get("error") or "not_ok"),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "kind": str(kind or FEEDBACK_SNAPSHOT_KIND),
        "days": int(resolved_days),
        "end_date": resolved_end_date,
    }


def build_report(db_path: Path, days: int) -> Dict[str, Any]:
    start_utc, end_utc, start_et, end_et = _window_for_days(days)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        _configure_read_connection(conn)
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        return _db_read_error_report(db_path, int(days), exc)
    conn.row_factory = sqlite3.Row
    candidates: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    current_runtime: Dict[str, Any] = {}
    row_mode = "exact"
    try:
        current_runtime = _build_current_runtime_snapshot(conn)
        rows = _load_entry_decision_rows(conn, start_utc, end_utc, start_et, end_et)
        if rows and isinstance(rows[0], Mapping):
            row_mode = str(rows[0].get("counterfactual_row_mode") or "exact")
    except sqlite3.Error as exc:
        conn.close()
        return _db_read_error_report(db_path, int(days), exc)
    for row in rows:
        details = _safe_json_dict(row["details_json"])
        strategy = _strategy_family(details)
        slug = str(row["slug"] or "").strip()
        event_date = str(row["event_date"] or "").split("T", 1)[0].strip()
        outcome = str(row["outcome"] or "").strip()
        side = str(row["side"] or "").strip().upper()
        if not slug or not event_date or not outcome or side not in {"YES", "NO"}:
            continue
        key = (slug, event_date, outcome, side, strategy)
        item = candidates.get(key)
        if item is None:
            item = {
                "slug": slug,
                "event_date": event_date,
                "outcome": outcome,
                "side": side,
                "strategy": strategy,
                "raw_decision_count": 0,
                "will_order": False,
                "latest_will_order": False,
                "live_will_order": False,
                "dry_run_intent": False,
                "rejected": False,
                "entry_usable_edge": None,
                "entry_usable_edge_base_source": "",
                "entry_usable_edge_na_reasons": set(),
                "entry_usable_edge_size_scale": None,
                "entry_usable_edge_penalties": {},
                "exec_edge_entry": None,
                "edge_tradeability_profit_value": None,
                "edge_tradeability_edge_lcb": None,
                "edge_realizable": None,
                "edge_realizable_score": None,
                "q_entry": None,
                "q_quote": None,
                "ask": None,
                "bid": None,
                "spread": None,
                "order_price": None,
                "soft_down": False,
                "low_price": False,
                "expected_exit_bid": None,
                "entry_edge_false_edge": False,
                "entry_edge_false_edge_types": set(),
                "entry_edge_root_causes": set(),
                "latest_entry_edge_false_edge": False,
                "latest_entry_edge_false_edge_types": set(),
                "latest_entry_edge_root_causes": set(),
                "entry_edge_entry_surplus": None,
                "entry_edge_target_exit_surplus": None,
                "entry_edge_repricing_target_surplus_raw": None,
                "entry_edge_target_exit_surplus_usable": None,
                "entry_edge_repricing_target_surplus_discounted": None,
                "entry_edge_repricing_friction_floor": None,
                "entry_edge_target_surplus_to_friction": None,
                "entry_edge_repricing_target_surplus_discounted_to_friction": None,
                "latest_entry_edge_entry_surplus": None,
                "latest_entry_edge_target_exit_surplus": None,
                "latest_entry_edge_repricing_target_surplus_raw": None,
                "latest_entry_edge_target_exit_surplus_usable": None,
                "latest_entry_edge_repricing_target_surplus_discounted": None,
                "latest_entry_edge_repricing_friction_floor": None,
                "latest_entry_edge_target_surplus_to_friction": None,
                "latest_entry_edge_repricing_target_surplus_discounted_to_friction": None,
                "repricing_execution_candidate": False,
                "repricing_evidence_missing": False,
                "repricing_evidence_entry_missing_flag": False,
                "repricing_evidence_realize_missing_flag": False,
                "repricing_evidence_realize_default_flag": False,
                "repricing_evidence_low_sample": False,
                "repricing_evidence_limited": False,
                "repricing_evidence_status": "",
                "repricing_evidence_na_reason": "",
                "repricing_evidence_missing_reason": "",
                "repricing_evidence_probability_source": "",
                "repricing_evidence_missing_tables": "",
                "repricing_evidence_default_tables": "",
                "repricing_evidence_low_sample_tables": "",
                "repricing_evidence_bucket_samples_min": None,
                "repricing_evidence_scale": None,
                "repricing_p_realize": None,
                "repricing_p_converge": None,
                "repricing_p_fill_entry": None,
                "repricing_p_fill_exit": None,
                "repricing_evidence_components": {},
                "station_missing": False,
                "contract_rule_missing": False,
                "first_created_at": str(row["created_at"] or ""),
                "latest_created_at": str(row["created_at"] or ""),
                "reason_codes": set(),
                "latest_reason_codes": set(),
                "order_ids": set(),
            }
            candidates[key] = item
        row_raw_count = max(1, _row_int(row, "raw_decision_count", 1))
        item["raw_decision_count"] += row_raw_count
        item["latest_created_at"] = str(row["created_at"] or "")
        latest_will_override = _row_bool_override(row, "latest_will_order")
        will_order_any_override = _row_bool_override(row, "will_order_any")
        rejected_override = _row_bool_override(row, "rejected")
        dry_run_override = _row_bool_override(row, "dry_run_intent")
        will_order = (
            latest_will_override
            if latest_will_override is not None
            else int(row["will_order"] or 0) != 0
        )
        will_order_any = will_order_any_override if will_order_any_override is not None else will_order
        decided_state = str(row["decided_state"] or "").strip().lower()
        dry_run_intent = bool(
            dry_run_override
            if dry_run_override is not None
            else will_order and ("dry_run" in decided_state or _boolish(details.get("dry_run")))
        )
        item["will_order"] = bool(item["will_order"] or will_order_any)
        item["latest_will_order"] = bool(will_order)
        item["dry_run_intent"] = bool(item["dry_run_intent"] or dry_run_intent)
        item["live_will_order"] = bool(item["live_will_order"] or (will_order_any and not dry_run_intent))
        row_rejected = rejected_override if rejected_override is not None else not will_order
        item["rejected"] = bool(item["rejected"] or row_rejected)
        row_reason_codes = set()
        for code in _safe_json_list(row["reason_codes_json"]):
            item["reason_codes"].add(code)
            row_reason_codes.add(code)
        for code in _safe_json_list(details.get("entry_soft_reason_codes")):
            item["reason_codes"].add(code)
            row_reason_codes.add(code)
        latest_reason_raw = _row_get(row, "latest_reason_codes_json", None)
        item["latest_reason_codes"] = set(
            _safe_json_list(latest_reason_raw) if latest_reason_raw is not None else row_reason_codes
        )
        edge_val = _to_float(details.get("entry_usable_edge"))
        if edge_val is not None:
            prev = _to_float(item.get("entry_usable_edge"))
            if prev is None or edge_val > prev:
                item["entry_usable_edge"] = float(edge_val)
                item["entry_usable_edge_base_source"] = str(
                    details.get("entry_usable_edge_base_source") or ""
                ).strip()
                penalties: Dict[str, float] = {}
                for penalty_key in PENALTY_KEYS:
                    penalty_val = _to_float(details.get(penalty_key))
                    if penalty_val is not None:
                        penalties[penalty_key] = float(penalty_val)
                item["entry_usable_edge_penalties"] = penalties
        else:
            na_reason = str(details.get("entry_usable_edge_na_reason") or "").strip()
            if na_reason:
                item["entry_usable_edge_na_reasons"].add(na_reason)
        for metric_name in (
            "exec_edge_entry",
            "edge_tradeability_profit_value",
            "edge_tradeability_edge_lcb",
            "edge_realizable",
            "edge_realizable_score",
            "q_entry",
            "q_quote",
            "ask",
            "bid",
            "spread",
        ):
            metric_val = _to_float(details.get(metric_name))
            if metric_val is not None:
                item[metric_name] = float(metric_val)
        order_price_val = _to_float(details.get("order_price"))
        if order_price_val is None:
            order_price_val = _to_float(details.get("price"))
        if order_price_val is not None:
            item["order_price"] = float(order_price_val)
        scale = _to_float(details.get("entry_usable_edge_size_scale_applied"))
        if scale is None:
            scale = _to_float(details.get("entry_usable_edge_size_scale"))
        if scale is not None:
            item["entry_usable_edge_size_scale"] = float(scale)
            if 0.0 < float(scale) < 0.999:
                item["soft_down"] = True
        if _boolish(details.get("entry_soft_no_order")):
            item["soft_down"] = True
        if _boolish(details.get("entry_usable_edge_low_price_bucket")):
            item["low_price"] = True
        expected_exit_bid = _to_float(details.get("entry_usable_edge_expected_exit_bid"))
        if expected_exit_bid is None:
            expected_exit_bid = _to_float(details.get("entry_edge_current_exit_bid"))
        if expected_exit_bid is not None:
            item["expected_exit_bid"] = float(expected_exit_bid)
        false_edge_type = str(details.get("entry_edge_false_edge_type") or "").strip()
        false_edge_root = str(details.get("entry_edge_root_cause") or "").strip()
        row_false_edge = bool(_boolish(details.get("entry_edge_false_edge")) or false_edge_type)
        item["latest_entry_edge_false_edge"] = bool(row_false_edge)
        item["latest_entry_edge_false_edge_types"] = set()
        item["latest_entry_edge_root_causes"] = set()
        if row_false_edge:
            item["entry_edge_false_edge"] = True
            if false_edge_type:
                item["entry_edge_false_edge_types"].add(false_edge_type)
                item["latest_entry_edge_false_edge_types"].add(false_edge_type)
            if false_edge_root:
                item["entry_edge_root_causes"].add(false_edge_root)
                item["latest_entry_edge_root_causes"].add(false_edge_root)
        for metric_name in (
            "entry_edge_entry_surplus",
            "entry_edge_target_exit_surplus",
            "entry_edge_repricing_target_surplus_raw",
            "entry_edge_target_exit_surplus_usable",
            "entry_edge_repricing_target_surplus_discounted",
            "entry_edge_repricing_friction_floor",
            "entry_edge_target_surplus_to_friction",
            "entry_edge_repricing_target_surplus_discounted_to_friction",
        ):
            metric_val = _to_float(details.get(metric_name))
            if metric_val is None:
                continue
            item[f"latest_{metric_name}"] = float(metric_val)
            prev_metric = _to_float(item.get(metric_name))
            if prev_metric is None or float(metric_val) < float(prev_metric):
                item[metric_name] = float(metric_val)
        for target_metric, fallback_metric in (
            (
                "entry_edge_repricing_target_surplus_raw",
                "entry_edge_target_exit_surplus",
            ),
            (
                "entry_edge_repricing_target_surplus_discounted",
                "entry_edge_target_exit_surplus_usable",
            ),
        ):
            if _to_float(item.get(target_metric)) is None:
                fallback_val = _to_float(item.get(fallback_metric))
                if fallback_val is not None:
                    item[target_metric] = float(fallback_val)
            latest_key = f"latest_{target_metric}"
            latest_fallback_key = f"latest_{fallback_metric}"
            if _to_float(item.get(latest_key)) is None:
                fallback_val = _to_float(item.get(latest_fallback_key))
                if fallback_val is not None:
                    item[latest_key] = float(fallback_val)
        discounted_surplus = _to_float(
            item.get("entry_edge_repricing_target_surplus_discounted")
        )
        friction_floor = _to_float(item.get("entry_edge_repricing_friction_floor"))
        if (
            _to_float(item.get("entry_edge_repricing_target_surplus_discounted_to_friction"))
            is None
            and discounted_surplus is not None
            and friction_floor is not None
        ):
            item["entry_edge_repricing_target_surplus_discounted_to_friction"] = (
                float(discounted_surplus) - float(friction_floor)
            )
        latest_discounted_surplus = _to_float(
            item.get("latest_entry_edge_repricing_target_surplus_discounted")
        )
        latest_friction_floor = _to_float(
            item.get("latest_entry_edge_repricing_friction_floor")
        )
        if (
            _to_float(
                item.get("latest_entry_edge_repricing_target_surplus_discounted_to_friction")
            )
            is None
            and latest_discounted_surplus is not None
            and latest_friction_floor is not None
        ):
            item["latest_entry_edge_repricing_target_surplus_discounted_to_friction"] = (
                float(latest_discounted_surplus) - float(latest_friction_floor)
            )
        exit_plan = str(
            details.get("entry_edge_exit_plan")
            or details.get("weather_strategy_exit_plan")
            or details.get("entry_exit_plan")
            or details.get("exit_plan")
            or ""
        ).strip().lower()
        repricing_execution_candidate = bool(
            strategy == "repricing_bucket"
            or _boolish(details.get("entry_edge_exit_path_required"))
            or "repricing" in exit_plan
            or "target_exit" in exit_plan
            or "exit_target" in exit_plan
            or str(details.get("entry_edge_repricing_exit_evidence_status") or "").strip()
            or str(details.get("edge_realizable_p_realize_evidence_status") or "").strip()
            or "target_exit_execution" in false_edge_type
        )
        item["repricing_execution_candidate"] = bool(
            item["repricing_execution_candidate"] or repricing_execution_candidate
        )
        entry_missing_flag = _boolish(
            details.get("entry_edge_repricing_exit_evidence_missing")
        )
        realize_missing_flag = _boolish(details.get("edge_realizable_p_realize_missing"))
        realize_default_flag = _boolish(
            details.get("edge_realizable_p_realize_uses_default")
        )
        evidence_missing = bool(
            entry_missing_flag or realize_missing_flag or realize_default_flag
        )
        evidence_low_sample = bool(
            _boolish(details.get("entry_edge_repricing_exit_evidence_low_sample"))
            or _boolish(details.get("edge_realizable_p_realize_low_sample"))
        )
        evidence_limited = _boolish(
            details.get("entry_edge_repricing_exit_evidence_limited")
        )
        if evidence_missing:
            item["repricing_evidence_missing"] = True
        if entry_missing_flag:
            item["repricing_evidence_entry_missing_flag"] = True
        if realize_missing_flag:
            item["repricing_evidence_realize_missing_flag"] = True
        if realize_default_flag:
            item["repricing_evidence_realize_default_flag"] = True
        if evidence_low_sample:
            item["repricing_evidence_low_sample"] = True
        if evidence_limited:
            item["repricing_evidence_limited"] = True
        status = str(
            details.get("entry_edge_repricing_exit_evidence_status")
            or details.get("edge_realizable_p_realize_evidence_status")
            or ""
        ).strip().lower()
        if status:
            item["repricing_evidence_status"] = status
            if status in {"missing", "default_only"}:
                item["repricing_evidence_missing"] = True
            elif status == "low_sample":
                item["repricing_evidence_low_sample"] = True
        na_reason = str(
            details.get("entry_edge_repricing_exit_evidence_na_reason")
            or details.get("edge_realizable_p_realize_na_reason")
            or ""
        ).strip()
        if na_reason:
            item["repricing_evidence_na_reason"] = na_reason
        missing_reason = str(
            details.get("entry_edge_repricing_exit_evidence_missing_reason") or ""
        ).strip()
        if missing_reason:
            item["repricing_evidence_missing_reason"] = missing_reason
        probability_source = str(
            details.get("entry_edge_repricing_exit_evidence_probability_source") or ""
        ).strip()
        if probability_source:
            item["repricing_evidence_probability_source"] = probability_source
        probability_source_components = _parse_repricing_probability_source(
            probability_source
        )
        missing_tables = str(
            details.get("entry_edge_repricing_exit_evidence_missing_tables")
            or details.get("edge_realizable_p_realize_missing_tables")
            or ""
        ).strip()
        if missing_tables:
            item["repricing_evidence_missing_tables"] = missing_tables
            item["repricing_evidence_missing"] = True
        default_tables = str(
            details.get("entry_edge_repricing_exit_evidence_default_tables")
            or details.get("edge_realizable_p_realize_default_tables")
            or ""
        ).strip()
        if default_tables:
            item["repricing_evidence_default_tables"] = default_tables
            item["repricing_evidence_missing"] = True
        low_sample_tables = str(
            details.get("entry_edge_repricing_exit_evidence_low_sample_tables")
            or details.get("edge_realizable_p_realize_low_sample_tables")
            or ""
        ).strip()
        if low_sample_tables:
            item["repricing_evidence_low_sample_tables"] = low_sample_tables
            item["repricing_evidence_low_sample"] = True
        samples_min = _to_float(
            details.get("entry_edge_repricing_exit_evidence_bucket_samples_min")
        )
        if samples_min is None:
            samples_min = _to_float(details.get("edge_realizable_p_realize_samples_min"))
        if samples_min is not None:
            item["repricing_evidence_bucket_samples_min"] = float(samples_min)
        evidence_scale = _to_float(details.get("entry_edge_repricing_exit_evidence_scale"))
        if evidence_scale is not None:
            item["repricing_evidence_scale"] = float(evidence_scale)
        for source_key, item_key in (
            ("edge_realizable_p_realize", "repricing_p_realize"),
            ("edge_realizable_p_converge", "repricing_p_converge"),
            ("edge_realizable_p_fill_entry", "repricing_p_fill_entry"),
            ("edge_realizable_p_fill_exit", "repricing_p_fill_exit"),
        ):
            prob_val = _to_float(details.get(source_key))
            if prob_val is not None:
                item[item_key] = float(prob_val)
        components = dict(item.get("repricing_evidence_components") or {})
        for component in REPRICING_EVIDENCE_COMPONENTS:
            component_item = _repricing_component_from_details(
                details,
                component,
                probability_source_components.get(component),
            )
            if component_item:
                components[component] = component_item
                component_status = str(component_item.get("status") or "").strip()
                if component_status in {"missing", "default_only"}:
                    item["repricing_evidence_missing"] = True
                elif component_status == "low_sample":
                    item["repricing_evidence_low_sample"] = True
        item["repricing_evidence_components"] = components
        source_fit_na_reason = (
            details.get("source_fit_na_reason")
            or details.get("source_na_reason")
            or details.get("weather_strategy_source_na_reason")
            or details.get("observed_nowcast_source_na_reason")
        )
        if any(
            _source_fit_na_is_station_mapping_missing(code)
            for code in item["reason_codes"]
        ) or _source_fit_na_is_station_mapping_missing(source_fit_na_reason):
            item["station_missing"] = True
        if any(_contract_rule_is_missing(code) for code in item["reason_codes"]):
            item["contract_rule_missing"] = True
        for key_name in ("order_id", "auto_order_id", "entry_order_id"):
            order_id = str(details.get(key_name) or "").strip()
            if order_id:
                item["order_ids"].add(order_id)

    settlement_coverage = _build_settlement_coverage(conn, candidates.values())
    event_keys = {(c["slug"], c["event_date"]) for c in candidates.values()}
    contract_keys = {(c["slug"], c["event_date"], c["outcome"], c["side"]) for c in candidates.values()}
    settlements = _load_settlements(conn, event_keys)
    fills = _load_fills(conn, contract_keys)
    conn.close()

    all_stats = _new_stats()
    by_strategy: Dict[str, Dict[str, Any]] = defaultdict(_new_stats)
    examples: Dict[str, List[Dict[str, Any]]] = {
        "missed_winners": [],
        "false_positives": [],
        "low_price_no_exit": [],
        "false_edge_missed_winners": [],
        "friction_false_edge_missed_winners": [],
    }

    for cand in candidates.values():
        stats_items = [all_stats, by_strategy[str(cand["strategy"])]]
        fill = fills.get((cand["slug"], cand["event_date"], cand["outcome"], cand["side"]), {})
        buy_qty = _to_float(fill.get("buy_qty")) or 0.0
        sell_qty = _to_float(fill.get("sell_qty")) or 0.0
        buy_filled = buy_qty > 0
        sell_filled = sell_qty > 0
        settlement = settlements.get((cand["slug"], cand["event_date"]))
        side_hit: Optional[bool] = None
        if settlement:
            side_hit = _side_hit(
                float(settlement["settlement_actual"]),
                str(cand["outcome"]),
                str(cand["side"]),
                str(settlement.get("rounding_rule") or ""),
            )
        usable = _to_float(cand.get("entry_usable_edge"))
        scale = _to_float(cand.get("entry_usable_edge_size_scale"))
        expected_exit_bid = _to_float(cand.get("expected_exit_bid"))
        sell_avg = _to_float(fill.get("sell_avg_price"))
        na_reasons = sorted(cand.get("entry_usable_edge_na_reasons") or [])
        if not na_reasons and usable is None:
            na_reasons = ["legacy_missing_entry_usable_edge"]
        reason_codes = sorted(cand.get("reason_codes") or [])
        latest_reason_codes = sorted(cand.get("latest_reason_codes") or [])
        missed_winner = bool((not buy_filled) and cand["rejected"] and side_hit is True)
        q_entry = _to_float(cand.get("q_entry"))
        q_quote = _to_float(cand.get("q_quote"))
        ask = _to_float(cand.get("ask"))
        order_price = _to_float(cand.get("order_price"))
        breakdown_metrics = {
            "entry_usable_edge": usable,
            "exec_edge_entry": _to_float(cand.get("exec_edge_entry")),
            "edge_tradeability_profit_value": _to_float(
                cand.get("edge_tradeability_profit_value")
            ),
            "edge_tradeability_edge_lcb": _to_float(
                cand.get("edge_tradeability_edge_lcb")
            ),
            "edge_realizable": _to_float(cand.get("edge_realizable")),
            "edge_realizable_score": _to_float(cand.get("edge_realizable_score")),
            "q_entry": q_entry,
            "q_quote": q_quote,
            "ask": ask,
            "bid": _to_float(cand.get("bid")),
            "spread": _to_float(cand.get("spread")),
            "order_price": order_price,
            "q_minus_ask": (
                float(q_entry) - float(ask)
                if q_entry is not None and ask is not None
                else None
            ),
            "q_minus_order_price": (
                float(q_entry) - float(order_price)
                if q_entry is not None and order_price is not None
                else None
            ),
            "q_quote_minus_ask": (
                float(q_quote) - float(ask)
                if q_quote is not None and ask is not None
                else None
            ),
        }
        false_edge_types = sorted(cand.get("latest_entry_edge_false_edge_types") or [])
        false_edge_roots = sorted(cand.get("latest_entry_edge_root_causes") or [])
        false_edge_blocked = bool(
            cand.get("latest_entry_edge_false_edge") and not cand["latest_will_order"]
        )
        friction_false_edge = bool(
            false_edge_blocked and "target_exit_does_not_cover_friction" in false_edge_types
        )
        repricing_probability_too_low = bool(
            false_edge_blocked
            and "target_exit_execution_probability_too_low" in false_edge_types
        )
        for stat in stats_items:
            stat["candidate_count"] += 1
            stat["raw_decision_count"] += int(cand["raw_decision_count"] or 0)
            settlement_known = bool(settlement and side_hit is not None)
            for code in reason_codes:
                _inc_counter(stat, "reason_code_counts", code)
                _inc_breakdown(
                    stat,
                    "reason_code_breakdown_raw",
                    code,
                    will_order=bool(cand["will_order"]),
                    dry_run_intent=bool(cand["dry_run_intent"]),
                    buy_filled=bool(buy_filled),
                    settlement_known=settlement_known,
                    rejected=bool(cand["rejected"]),
                    side_hit=side_hit,
                    missed_winner=bool(missed_winner),
                    metrics=breakdown_metrics,
                )
            for code in latest_reason_codes:
                _inc_counter(stat, "latest_reason_code_counts", code)
                _inc_breakdown(
                    stat,
                    "latest_reason_code_breakdown_raw",
                    code,
                    will_order=bool(cand["latest_will_order"]),
                    dry_run_intent=bool(cand["dry_run_intent"]),
                    buy_filled=bool(buy_filled),
                    settlement_known=settlement_known,
                    rejected=bool(not cand["latest_will_order"]),
                    side_hit=side_hit,
                    missed_winner=bool(
                        (not buy_filled)
                        and (not cand["latest_will_order"])
                        and side_hit is True
                    ),
                    metrics=breakdown_metrics,
                )
            if cand["will_order"]:
                stat["will_order_count"] += 1
            if cand["live_will_order"]:
                stat["live_will_order_count"] += 1
            if cand["dry_run_intent"]:
                stat["dry_run_intent_count"] += 1
            if buy_filled:
                stat["filled_entry_count"] += 1
            if sell_filled:
                stat["exit_filled_count"] += 1
            if settlement and side_hit is not None:
                stat["settled_count"] += 1
                if buy_filled:
                    stat["bought_settled_count"] += 1
                    if side_hit:
                        stat["bought_hit_count"] += 1
                elif cand["rejected"]:
                    stat["rejected_only_settled_count"] += 1
                    if side_hit:
                        stat["rejected_only_hit_count"] += 1
            if cand["soft_down"]:
                stat["soft_down_count"] += 1
                if buy_filled:
                    stat["soft_down_filled_count"] += 1
                    if sell_filled:
                        stat["soft_down_exit_filled_count"] += 1
                if settlement and side_hit is not None:
                    stat["soft_down_settled_count"] += 1
                    if side_hit:
                        stat["soft_down_hit_count"] += 1
                    if cand["rejected"] and not buy_filled:
                        stat["soft_down_rejected_settled_count"] += 1
                        if side_hit:
                            stat["soft_down_rejected_hit_count"] += 1
            if cand["low_price"]:
                stat["low_price_count"] += 1
                if settlement and side_hit is not None:
                    stat["low_price_settled_count"] += 1
                    if side_hit:
                        stat["low_price_hit_count"] += 1
                if buy_filled:
                    stat["low_price_filled_count"] += 1
                    if sell_filled:
                        stat["low_price_exit_filled_count"] += 1
            if false_edge_blocked:
                stat["entry_false_edge_count"] += 1
                for false_type in false_edge_types or ["unknown"]:
                    _inc_counter(stat, "entry_false_edge_type_counts", false_type)
                for root_cause in false_edge_roots or ["unknown"]:
                    _inc_counter(stat, "entry_false_edge_root_cause_counts", root_cause)
                if settlement and side_hit is not None:
                    stat["entry_false_edge_settled_count"] += 1
                    if side_hit:
                        stat["entry_false_edge_hit_count"] += 1
                if missed_winner:
                    stat["entry_false_edge_missed_winner_count"] += 1
                    for false_type in false_edge_types or ["unknown"]:
                        _inc_counter(
                            stat,
                            "missed_winner_false_edge_type_counts",
                            false_type,
                        )
                    for root_cause in false_edge_roots or ["unknown"]:
                        _inc_counter(
                            stat,
                            "missed_winner_false_edge_root_cause_counts",
                            root_cause,
                        )
            if friction_false_edge:
                stat["friction_false_edge_count"] += 1
                if settlement and side_hit is not None:
                    stat["friction_false_edge_settled_count"] += 1
                    if side_hit:
                        stat["friction_false_edge_hit_count"] += 1
                if missed_winner:
                    stat["friction_false_edge_missed_winner_count"] += 1
            if cand["repricing_execution_candidate"]:
                stat["repricing_candidate_count"] += 1
                if cand["repricing_evidence_missing"]:
                    stat["repricing_evidence_missing_count"] += 1
                    missing_attribution_hit = False
                else:
                    missing_attribution_hit = True
                if cand["repricing_evidence_low_sample"]:
                    stat["repricing_evidence_low_sample_count"] += 1
                if repricing_probability_too_low:
                    stat["repricing_probability_too_low_count"] += 1
                evidence_status = str(cand.get("repricing_evidence_status") or "").strip()
                if evidence_status:
                    _inc_counter(stat, "repricing_evidence_status_counts", evidence_status)
                    normalized_status = _normalize_repricing_evidence_status(
                        evidence_status
                    )
                    if normalized_status == "missing":
                        stat["repricing_evidence_status_missing_count"] += 1
                        missing_attribution_hit = True
                    elif normalized_status == "default_only":
                        stat["repricing_evidence_status_default_only_count"] += 1
                        missing_attribution_hit = True
                evidence_na_reason = str(
                    cand.get("repricing_evidence_na_reason") or ""
                ).strip()
                if evidence_na_reason:
                    _inc_counter(
                        stat,
                        "repricing_evidence_na_reason_counts",
                        evidence_na_reason,
                    )
                evidence_missing_reason = str(
                    cand.get("repricing_evidence_missing_reason") or ""
                ).strip()
                if evidence_missing_reason:
                    _inc_counter(
                        stat,
                        "repricing_evidence_missing_reason_counts",
                        evidence_missing_reason,
                    )
                for probability_source in _split_counter_items(
                    cand.get("repricing_evidence_probability_source")
                ):
                    _inc_counter(
                        stat,
                        "repricing_evidence_probability_source_counts",
                        probability_source,
                    )
                for table_name in _split_counter_items(
                    cand.get("repricing_evidence_missing_tables")
                ):
                    _inc_counter(
                        stat,
                        "repricing_evidence_missing_table_counts",
                        table_name,
                    )
                if _split_counter_items(cand.get("repricing_evidence_missing_tables")):
                    stat["repricing_evidence_missing_table_candidate_count"] += 1
                    missing_attribution_hit = True
                for table_name in _split_counter_items(
                    cand.get("repricing_evidence_default_tables")
                ):
                    _inc_counter(
                        stat,
                        "repricing_evidence_default_table_counts",
                        table_name,
                    )
                if _split_counter_items(cand.get("repricing_evidence_default_tables")):
                    stat["repricing_evidence_default_table_candidate_count"] += 1
                    missing_attribution_hit = True
                if cand.get("repricing_evidence_entry_missing_flag"):
                    stat["repricing_evidence_entry_missing_flag_count"] += 1
                    missing_attribution_hit = True
                if cand.get("repricing_evidence_realize_missing_flag"):
                    stat["repricing_evidence_realize_missing_flag_count"] += 1
                    missing_attribution_hit = True
                if cand.get("repricing_evidence_realize_default_flag"):
                    stat["repricing_evidence_realize_default_flag_count"] += 1
                    missing_attribution_hit = True
                for table_name in _split_counter_items(
                    cand.get("repricing_evidence_low_sample_tables")
                ):
                    _inc_counter(
                        stat,
                        "repricing_evidence_low_sample_table_counts",
                        table_name,
                    )
                samples_min = _to_float(
                    cand.get("repricing_evidence_bucket_samples_min")
                )
                if samples_min is not None:
                    stat["repricing_evidence_bucket_samples_min_values"].append(
                        float(samples_min)
                    )
                for value_key, list_key in (
                    ("repricing_p_realize", "repricing_p_realize_values"),
                    ("repricing_p_converge", "repricing_p_converge_values"),
                    ("repricing_p_fill_entry", "repricing_p_fill_entry_values"),
                    ("repricing_p_fill_exit", "repricing_p_fill_exit_values"),
                    ("repricing_evidence_scale", "repricing_evidence_scales"),
                    (
                        "entry_edge_repricing_target_surplus_raw",
                        "repricing_target_surplus_raw_values",
                    ),
                    (
                        "entry_edge_repricing_target_surplus_discounted",
                        "repricing_target_surplus_discounted_values",
                    ),
                    (
                        "entry_edge_repricing_target_surplus_discounted_to_friction",
                        "repricing_target_surplus_discounted_to_friction_values",
                    ),
                    (
                        "entry_edge_target_surplus_to_friction",
                        "repricing_target_surplus_to_friction_values",
                    ),
                    (
                        "entry_edge_repricing_friction_floor",
                        "repricing_friction_floor_values",
                    ),
                ):
                    prob_val = _to_float(cand.get(value_key))
                    if prob_val is not None:
                        stat[list_key].append(float(prob_val))
                component_stats = stat.get("repricing_evidence_component_stats")
                if not isinstance(component_stats, dict):
                    component_stats = _new_repricing_component_stats()
                    stat["repricing_evidence_component_stats"] = component_stats
                components = cand.get("repricing_evidence_components")
                if not isinstance(components, Mapping):
                    components = {}
                component_missing = False
                component_default_only = False
                component_unreported = False
                all_components_bucket_evidence = True
                for component in REPRICING_EVIDENCE_COMPONENTS:
                    comp_stat = component_stats.setdefault(
                        component,
                        _new_repricing_component_stats()[component],
                    )
                    comp_stat["candidate_count"] += 1
                    comp = components.get(component) if isinstance(components, Mapping) else None
                    if not isinstance(comp, Mapping):
                        component_unreported = True
                        all_components_bucket_evidence = False
                        continue
                    comp_stat["reported_count"] += 1
                    status = str(comp.get("status") or "").strip().lower()
                    if status:
                        _inc_counter(comp_stat, "status_counts", status)
                    if status == "missing":
                        comp_stat["missing_count"] += 1
                        component_missing = True
                        all_components_bucket_evidence = False
                    elif status == "default_only":
                        comp_stat["default_only_count"] += 1
                        component_default_only = True
                        all_components_bucket_evidence = False
                    elif status == "low_sample":
                        comp_stat["low_sample_count"] += 1
                        all_components_bucket_evidence = False
                    elif status == "bucket_evidence":
                        comp_stat["bucket_evidence_count"] += 1
                    else:
                        all_components_bucket_evidence = False
                    source = str(comp.get("source") or "").strip()
                    if source:
                        _inc_counter(comp_stat, "source_counts", source)
                    bucket_key = str(comp.get("bucket_key") or "").strip()
                    if bucket_key:
                        _inc_counter(comp_stat, "bucket_key_counts", bucket_key)
                    bucket_used_key = str(comp.get("bucket_used_key") or "").strip()
                    if bucket_used_key:
                        _inc_counter(
                            comp_stat,
                            "bucket_used_key_counts",
                            bucket_used_key,
                        )
                        _inc_breakdown(
                            comp_stat,
                            "bucket_used_breakdown_raw",
                            bucket_used_key,
                            will_order=bool(cand["will_order"]),
                            dry_run_intent=bool(cand["dry_run_intent"]),
                            buy_filled=bool(buy_filled),
                            settlement_known=settlement_known,
                            rejected=bool(cand["rejected"]),
                            side_hit=side_hit,
                            missed_winner=bool(missed_winner),
                            metrics=breakdown_metrics,
                        )
                    prob_val = _to_float(comp.get("prob"))
                    if prob_val is not None:
                        comp_stat["prob_values"].append(float(prob_val))
                    samples_val = _to_float(comp.get("samples"))
                    if samples_val is not None:
                        comp_stat["sample_values"].append(float(samples_val))
                    limiting_probability = bool(
                        repricing_probability_too_low
                        and component == _repricing_probability_limiter_component(components)
                    )
                    if repricing_probability_too_low:
                        comp_stat["probability_too_low_count"] += 1
                    if limiting_probability:
                        comp_stat["limiting_probability_count"] += 1
                        bucket_used_key = str(
                            comp.get("bucket_used_key") or comp.get("bucket_key") or ""
                        ).strip()
                        if bucket_used_key:
                            _inc_counter(
                                comp_stat,
                                "limiting_bucket_used_key_counts",
                                bucket_used_key,
                            )
                    _inc_probability_bucket_quality(
                        comp_stat,
                        str(comp.get("bucket_used_key") or comp.get("bucket_key") or ""),
                        prob=prob_val,
                        samples=samples_val,
                        p_realize=_to_float(cand.get("repricing_p_realize")),
                        probability_too_low=repricing_probability_too_low,
                        limiting_probability=limiting_probability,
                    )
                if component_missing:
                    stat["repricing_evidence_component_missing_count"] += 1
                    missing_attribution_hit = True
                if component_default_only:
                    stat["repricing_evidence_default_only_count"] += 1
                    missing_attribution_hit = True
                if component_unreported:
                    stat["repricing_evidence_unreported_component_count"] += 1
                if all_components_bucket_evidence:
                    stat["repricing_evidence_all_bucket_evidence_count"] += 1
                if cand["repricing_evidence_missing"] and not missing_attribution_hit:
                    stat["repricing_evidence_missing_residual_count"] += 1
            if usable is not None:
                stat["entry_usable_edge_present_count"] += 1
                stat["entry_usable_edges"].append(float(usable))
                if usable > 0:
                    stat["entry_usable_edge_positive_count"] += 1
                else:
                    stat["entry_usable_edge_nonpositive_count"] += 1
            else:
                stat["entry_usable_edge_missing_count"] += 1
                if na_reasons and na_reasons != ["legacy_missing_entry_usable_edge"]:
                    stat["entry_usable_edge_na_reason_present_count"] += 1
                    for reason in na_reasons:
                        _inc_counter(stat, "entry_usable_edge_na_reason_counts", reason)
                        _inc_breakdown(
                            stat,
                            "entry_usable_edge_na_reason_breakdown_raw",
                            reason,
                            will_order=bool(cand["will_order"]),
                            dry_run_intent=bool(cand["dry_run_intent"]),
                            buy_filled=bool(buy_filled),
                            settlement_known=settlement_known,
                            rejected=bool(cand["rejected"]),
                            side_hit=side_hit,
                            missed_winner=bool(missed_winner),
                            metrics=breakdown_metrics,
                        )
                else:
                    stat["entry_usable_edge_silent_missing_count"] += 1
            if scale is not None:
                stat["size_scales"].append(float(scale))
                if scale <= 1e-9:
                    stat["size_scale_zero_count"] += 1
            penalties = cand.get("entry_usable_edge_penalties")
            if isinstance(penalties, Mapping):
                for penalty_key in PENALTY_KEYS:
                    penalty_val = _to_float(penalties.get(penalty_key))
                    if penalty_val is None:
                        continue
                    stat["penalty_sums"][penalty_key] += float(penalty_val)
                    stat["penalty_counts"][penalty_key] += 1
            if expected_exit_bid is not None and sell_avg is not None:
                stat["exit_slippages"].append(float(expected_exit_bid) - float(sell_avg))
            if cand["station_missing"]:
                stat["station_missing_count"] += 1
            if cand["contract_rule_missing"]:
                stat["contract_rule_missing_count"] += 1
            if missed_winner:
                if usable is None:
                    stat["missed_winner_entry_usable_edge_missing_count"] += 1
                    for reason in na_reasons:
                        _inc_counter(stat, "missed_winner_na_reason_counts", reason)
                elif usable > 0:
                    stat["missed_winner_entry_usable_edge_positive_count"] += 1
                else:
                    stat["missed_winner_entry_usable_edge_nonpositive_count"] += 1
                if scale is not None and scale <= 1e-9:
                    stat["missed_winner_size_scale_zero_count"] += 1
                for code in reason_codes:
                    _inc_counter(stat, "missed_winner_reason_code_counts", code)
        example_base = {
            "slug": cand["slug"],
            "event_date": cand["event_date"],
            "outcome": cand["outcome"],
            "side": cand["side"],
            "strategy": cand["strategy"],
            "entry_usable_edge": usable,
            "entry_usable_edge_base_source": str(cand.get("entry_usable_edge_base_source") or ""),
            "entry_usable_edge_na_reasons": na_reasons,
            "reason_codes": reason_codes[:10],
            "latest_reason_codes": latest_reason_codes[:10],
            "size_scale": scale,
            "exec_edge_entry": _to_float(cand.get("exec_edge_entry")),
            "edge_tradeability_profit_value": _to_float(
                cand.get("edge_tradeability_profit_value")
            ),
            "edge_tradeability_edge_lcb": _to_float(
                cand.get("edge_tradeability_edge_lcb")
            ),
            "edge_realizable": _to_float(cand.get("edge_realizable")),
            "edge_realizable_score": _to_float(cand.get("edge_realizable_score")),
            "q_entry": q_entry,
            "q_quote": q_quote,
            "ask": ask,
            "bid": _to_float(cand.get("bid")),
            "spread": _to_float(cand.get("spread")),
            "order_price": order_price,
            "q_minus_ask": breakdown_metrics["q_minus_ask"],
            "q_minus_order_price": breakdown_metrics["q_minus_order_price"],
            "q_quote_minus_ask": breakdown_metrics["q_quote_minus_ask"],
            "entry_edge_false_edge_types": false_edge_types[:10],
            "entry_edge_root_causes": false_edge_roots[:10],
            "entry_edge_entry_surplus": _to_float(
                cand.get("latest_entry_edge_entry_surplus")
            ),
            "entry_edge_target_exit_surplus": _to_float(
                cand.get("latest_entry_edge_target_exit_surplus")
            ),
            "entry_edge_repricing_target_surplus_raw": _to_float(
                cand.get("latest_entry_edge_repricing_target_surplus_raw")
            ),
            "entry_edge_repricing_target_surplus_discounted": _to_float(
                cand.get("latest_entry_edge_repricing_target_surplus_discounted")
            ),
            "entry_edge_repricing_friction_floor": _to_float(
                cand.get("latest_entry_edge_repricing_friction_floor")
            ),
            "entry_edge_target_surplus_to_friction": _to_float(
                cand.get("latest_entry_edge_target_surplus_to_friction")
            ),
            "entry_edge_repricing_target_surplus_discounted_to_friction": _to_float(
                cand.get("latest_entry_edge_repricing_target_surplus_discounted_to_friction")
            ),
            "entry_edge_repricing_exit_evidence_status": str(
                cand.get("repricing_evidence_status") or ""
            ),
            "entry_edge_repricing_exit_evidence_na_reason": str(
                cand.get("repricing_evidence_na_reason") or ""
            ),
            "entry_edge_repricing_exit_evidence_missing_reason": str(
                cand.get("repricing_evidence_missing_reason") or ""
            ),
            "entry_edge_repricing_exit_evidence_probability_source": str(
                cand.get("repricing_evidence_probability_source") or ""
            ),
            "entry_edge_repricing_exit_evidence_missing_tables": str(
                cand.get("repricing_evidence_missing_tables") or ""
            ),
            "entry_edge_repricing_exit_evidence_default_tables": str(
                cand.get("repricing_evidence_default_tables") or ""
            ),
            "entry_edge_repricing_exit_evidence_low_sample_tables": str(
                cand.get("repricing_evidence_low_sample_tables") or ""
            ),
            "entry_edge_repricing_exit_evidence_bucket_samples_min": _to_float(
                cand.get("repricing_evidence_bucket_samples_min")
            ),
            "entry_edge_repricing_exit_evidence_scale": _to_float(
                cand.get("repricing_evidence_scale")
            ),
            "entry_edge_repricing_exit_evidence_missing": bool(
                cand.get("repricing_evidence_missing")
            ),
            "entry_edge_repricing_exit_evidence_low_sample": bool(
                cand.get("repricing_evidence_low_sample")
            ),
            "edge_realizable_p_realize": _to_float(cand.get("repricing_p_realize")),
            "edge_realizable_p_converge": _to_float(cand.get("repricing_p_converge")),
            "edge_realizable_p_fill_entry": _to_float(
                cand.get("repricing_p_fill_entry")
            ),
            "edge_realizable_p_fill_exit": _to_float(
                cand.get("repricing_p_fill_exit")
            ),
            "edge_realizable_repricing_evidence_components": cand.get(
                "repricing_evidence_components"
            ),
        }
        if missed_winner and len(examples["missed_winners"]) < 8:
            examples["missed_winners"].append(example_base)
        if buy_filled and side_hit is False and len(examples["false_positives"]) < 8:
            examples["false_positives"].append(example_base)
        if cand["low_price"] and buy_filled and not sell_filled and len(examples["low_price_no_exit"]) < 8:
            examples["low_price_no_exit"].append(example_base)
        if (
            missed_winner
            and false_edge_blocked
            and len(examples["false_edge_missed_winners"]) < 8
        ):
            examples["false_edge_missed_winners"].append(example_base)
        if (
            missed_winner
            and friction_false_edge
            and len(examples["friction_false_edge_missed_winners"]) < 8
        ):
            examples["friction_false_edge_missed_winners"].append(example_base)

    by_strategy_out = {
        key: _finalize_stats(value)
        for key, value in sorted(by_strategy.items(), key=lambda kv: kv[0])
    }
    overall_out = _finalize_stats(all_stats)
    rule_attribution = _build_rule_attribution(overall_out, by_strategy_out)
    rule_pruning_review = _build_rule_pruning_review(
        rule_attribution,
        current_runtime=current_runtime,
    )
    counterfactual_answers = _build_counterfactual_answers(overall_out, by_strategy_out)
    strategy_kpis = _build_strategy_kpis(overall_out, by_strategy_out)
    actionable_summary = _build_actionable_summary(
        counterfactual_answers,
        strategy_kpis,
        current_runtime=current_runtime,
    )
    return {
        "ok": True,
        "db_path": str(db_path),
        "window": {
            "days": int(days),
            "start_utc": start_utc,
            "end_utc": end_utc,
            "start_et": start_et,
            "end_et": end_et,
        },
        "scope": "auto_trade_decisions(action=entry), candidate-deduped by slug/event/outcome/side/strategy",
        "row_mode": row_mode,
        "overall": overall_out,
        "by_strategy": by_strategy_out,
        "strategy_kpis": strategy_kpis,
        "rule_attribution": rule_attribution,
        "rule_pruning_review": rule_pruning_review,
        "settlement_coverage": settlement_coverage,
        "counterfactual_answers": counterfactual_answers,
        "actionable_summary": actionable_summary,
        "current_runtime": current_runtime,
        "examples": examples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only entry counterfactual review.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--feedback-snapshot",
        action="store_true",
        help="Also write a compact payload into feedback_snapshots(kind=entry_counterfactual_review).",
    )
    args = parser.parse_args()
    db_path = Path(args.db)
    report = build_report(db_path, int(args.days))
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    if args.feedback_snapshot:
        save_feedback_snapshot(
            db_path,
            build_feedback_snapshot(report),
            days=int(args.days),
        )
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
