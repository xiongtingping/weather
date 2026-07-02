#!/usr/bin/env python3
"""Materialize rejected entry decisions into a queryable Reject Ledger.

The ledger is intentionally sourced from the existing execution SSOT:
``auto_trade_decisions`` for entry decisions, ``event_runs`` for settlement
facts, and ``edge_trade_fills`` for observed fills.  It does not change the
trading policy; it makes rejected opportunities durable and easy to audit.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import entry_counterfactual_review as ecr


DEFAULT_DB = ROOT / "data" / "archive.db"
LEDGER_TABLE = "reject_ledger_v1"
LEDGER_KIND = "reject_ledger_v1"
SHADOW_PROBE_TABLE = "soft_haircut_shadow_probe_ledger_v1"
SHADOW_PROBE_KIND = "soft_haircut_shadow_probe_ledger_v1"
QUALITY_REVIEW_MIN_SETTLED = 3
QUALITY_REVIEW_FALSE_REJECT_HIT_RATE = 0.55
QUALITY_REVIEW_PROTECTIVE_HIT_RATE = 0.35
RULE_PRUNING_MIN_SETTLED = QUALITY_REVIEW_MIN_SETTLED
RULE_PRUNING_NEUTRAL_UNIT_PNL = 0.0025
SOFT_HAIRCUT_BUCKET_MIN_SETTLED = 20
SHADOW_PROBE_DEFAULT_MAX_NOTIONAL_USDC = 0.35
SHADOW_PROBE_DEFAULT_MAX_PROBES_PER_ET_DAY = 3
HARD_GATE_SUBTYPES = {
    "data_invalid",
    "risk_redline",
    "causal_or_contract_safety",
    "exchange_or_execution_exception",
}
SOFT_HAIRCUT_SUBTYPES = {
    "sample_insufficient",
    "liquidity_or_price_weak",
    "calibration_or_probability_uncertain",
    "regime_uncertain",
}
WRAPPER_REASON_CODES = {
    "entry_gate_blocked",
    "entry_blocked",
    "auto_trade_entry_blocked",
}

LEDGER_COLUMNS = [
    "source_decision_id",
    "run_id",
    "run_date",
    "slug",
    "event_date",
    "title",
    "outcome",
    "side",
    "strategy_family",
    "decision_created_at",
    "will_order",
    "decided_state",
    "rejection_reason",
    "reason_codes_json",
    "soft_reason_codes_json",
    "entry_usable_edge",
    "entry_usable_edge_base_source",
    "entry_usable_edge_na_reason",
    "entry_usable_edge_size_scale",
    "edge_tradeability_pass",
    "edge_tradeability_blocked",
    "edge_tradeability_profit_value",
    "edge_tradeability_edge_lcb",
    "q_entry",
    "q_quote",
    "ask",
    "bid",
    "spread",
    "order_price",
    "hypothetical_size",
    "hypothetical_notional",
    "low_price_bucket",
    "false_edge_type",
    "false_edge_root_cause",
    "repricing_evidence_status",
    "repricing_p_realize",
    "repricing_p_converge",
    "repricing_p_fill_entry",
    "repricing_p_fill_exit",
    "repricing_components_json",
    "settlement_eligible",
    "settlement_actual",
    "settlement_rounding_rule",
    "settlement_recorded_at",
    "side_hit",
    "subsequent_outcome",
    "buy_filled",
    "sell_filled",
    "simulated_fill_quality_json",
    "counterfactual_pnl_per_share",
    "realized_counterfactual_pnl",
    "counterfactual_pnl_source",
    "regime_tag",
    "rule_version_json",
    "details_digest",
    "details_excerpt_json",
    "ledger_updated_at",
    "ledger_created_at",
]

SHADOW_PROBE_COLUMNS = [
    "source_decision_id",
    "probe_id",
    "probe_version",
    "run_id",
    "run_date",
    "slug",
    "event_date",
    "title",
    "outcome",
    "side",
    "strategy_family",
    "decision_created_at",
    "rejection_reason",
    "price_bucket",
    "notional_bucket",
    "shadow_mode",
    "live_orders_enabled",
    "live_block_reason",
    "shadow_price",
    "shadow_size",
    "shadow_notional",
    "max_notional_per_probe",
    "max_probes_per_et_day",
    "daily_notional_cap",
    "weekly_loss_cap",
    "entry_usable_edge",
    "edge_tradeability_profit_value",
    "ask",
    "bid",
    "spread",
    "settlement_eligible",
    "settlement_actual",
    "side_hit",
    "subsequent_outcome",
    "counterfactual_pnl_per_share",
    "shadow_counterfactual_pnl",
    "probe_evidence_json",
    "probe_guards_json",
    "promotion_requirements_json",
    "kill_switches_json",
    "details_digest",
    "ledger_updated_at",
    "ledger_created_at",
]


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")


def _safe_json_dict(raw: Any) -> dict[str, Any]:
    return ecr._safe_json_dict(raw)  # type: ignore[attr-defined]


def _safe_json_list(raw: Any) -> list[str]:
    return ecr._safe_json_list(raw)  # type: ignore[attr-defined]


def _to_float(value: Any) -> float | None:
    return ecr._to_float(value)  # type: ignore[attr-defined]


def _boolish(value: Any) -> bool:
    return ecr._boolish(value)  # type: ignore[attr-defined]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _int_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    text = str(value).strip()
    if text == "":
        return None
    if text.lower() in {"true", "yes", "on", "ok", "pass"}:
        return 1
    if text.lower() in {"false", "no", "off", "fail"}:
        return 0
    try:
        return 1 if int(float(text)) != 0 else 0
    except Exception:
        return 1 if _boolish(text) else 0


def ensure_reject_ledger_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_decision_id INTEGER NOT NULL UNIQUE,
            run_id INTEGER,
            run_date TEXT,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            title TEXT,
            outcome TEXT NOT NULL,
            side TEXT NOT NULL,
            strategy_family TEXT,
            decision_created_at TEXT,
            will_order INTEGER NOT NULL DEFAULT 0,
            decided_state TEXT,
            rejection_reason TEXT,
            reason_codes_json TEXT NOT NULL DEFAULT '[]',
            soft_reason_codes_json TEXT NOT NULL DEFAULT '[]',
            entry_usable_edge REAL,
            entry_usable_edge_base_source TEXT,
            entry_usable_edge_na_reason TEXT,
            entry_usable_edge_size_scale REAL,
            edge_tradeability_pass INTEGER,
            edge_tradeability_blocked INTEGER,
            edge_tradeability_profit_value REAL,
            edge_tradeability_edge_lcb REAL,
            q_entry REAL,
            q_quote REAL,
            ask REAL,
            bid REAL,
            spread REAL,
            order_price REAL,
            hypothetical_size REAL,
            hypothetical_notional REAL,
            low_price_bucket INTEGER,
            false_edge_type TEXT,
            false_edge_root_cause TEXT,
            repricing_evidence_status TEXT,
            repricing_p_realize REAL,
            repricing_p_converge REAL,
            repricing_p_fill_entry REAL,
            repricing_p_fill_exit REAL,
            repricing_components_json TEXT NOT NULL DEFAULT '{{}}',
            settlement_eligible INTEGER NOT NULL DEFAULT 0,
            settlement_actual REAL,
            settlement_rounding_rule TEXT,
            settlement_recorded_at TEXT,
            side_hit INTEGER,
            subsequent_outcome TEXT,
            buy_filled INTEGER NOT NULL DEFAULT 0,
            sell_filled INTEGER NOT NULL DEFAULT 0,
            simulated_fill_quality_json TEXT NOT NULL DEFAULT '{{}}',
            counterfactual_pnl_per_share REAL,
            realized_counterfactual_pnl REAL,
            counterfactual_pnl_source TEXT,
            regime_tag TEXT,
            rule_version_json TEXT NOT NULL DEFAULT '{{}}',
            details_digest TEXT,
            details_excerpt_json TEXT NOT NULL DEFAULT '{{}}',
            ledger_updated_at TEXT NOT NULL,
            ledger_created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{LEDGER_TABLE}_event
        ON {LEDGER_TABLE}(slug, event_date, outcome, side)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{LEDGER_TABLE}_reason
        ON {LEDGER_TABLE}(rejection_reason, decision_created_at DESC)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{LEDGER_TABLE}_outcome
        ON {LEDGER_TABLE}(subsequent_outcome, settlement_eligible, decision_created_at DESC)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{LEDGER_TABLE}_strategy
        ON {LEDGER_TABLE}(strategy_family, decision_created_at DESC)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{LEDGER_TABLE}_reason_strategy
        ON {LEDGER_TABLE}(rejection_reason, strategy_family)
        """
    )


def ensure_shadow_probe_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SHADOW_PROBE_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_decision_id INTEGER NOT NULL UNIQUE,
            probe_id TEXT NOT NULL,
            probe_version TEXT NOT NULL,
            run_id INTEGER,
            run_date TEXT,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            title TEXT,
            outcome TEXT NOT NULL,
            side TEXT NOT NULL,
            strategy_family TEXT NOT NULL,
            decision_created_at TEXT,
            rejection_reason TEXT NOT NULL,
            price_bucket TEXT NOT NULL,
            notional_bucket TEXT NOT NULL,
            shadow_mode TEXT NOT NULL,
            live_orders_enabled INTEGER NOT NULL DEFAULT 0,
            live_block_reason TEXT NOT NULL,
            shadow_price REAL,
            shadow_size REAL,
            shadow_notional REAL,
            max_notional_per_probe REAL,
            max_probes_per_et_day INTEGER,
            daily_notional_cap REAL,
            weekly_loss_cap REAL,
            entry_usable_edge REAL,
            edge_tradeability_profit_value REAL,
            ask REAL,
            bid REAL,
            spread REAL,
            settlement_eligible INTEGER NOT NULL DEFAULT 0,
            settlement_actual REAL,
            side_hit INTEGER,
            subsequent_outcome TEXT,
            counterfactual_pnl_per_share REAL,
            shadow_counterfactual_pnl REAL,
            probe_evidence_json TEXT NOT NULL DEFAULT '{{}}',
            probe_guards_json TEXT NOT NULL DEFAULT '{{}}',
            promotion_requirements_json TEXT NOT NULL DEFAULT '{{}}',
            kill_switches_json TEXT NOT NULL DEFAULT '{{}}',
            details_digest TEXT,
            ledger_updated_at TEXT NOT NULL,
            ledger_created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{SHADOW_PROBE_TABLE}_probe
        ON {SHADOW_PROBE_TABLE}(probe_id, decision_created_at DESC)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{SHADOW_PROBE_TABLE}_run
        ON {SHADOW_PROBE_TABLE}(run_id, rejection_reason, strategy_family)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{SHADOW_PROBE_TABLE}_outcome
        ON {SHADOW_PROBE_TABLE}(subsequent_outcome, settlement_eligible, decision_created_at DESC)
        """
    )


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (str(table_name),),
    ).fetchone()
    return row is not None


def _load_rejected_decisions(
    conn: sqlite3.Connection,
    *,
    start_utc: str,
    end_utc: str,
    limit: int,
) -> list[sqlite3.Row]:
    limit_clause = ""
    params: list[Any] = [start_utc, end_utc]
    if int(limit or 0) > 0:
        limit_clause = " LIMIT ?"
        params.append(int(limit))
    return list(
        conn.execute(
            f"""
            SELECT id, run_id, run_date, slug, event_date, title, outcome, side,
                   action, decided_state, will_order, reason_codes_json,
                   reasons_json, details_json, created_at
            FROM auto_trade_decisions
            WHERE action = 'entry'
              AND COALESCE(will_order, 0) = 0
              AND created_at >= ?
              AND created_at <= ?
            ORDER BY id ASC
            {limit_clause}
            """,
            params,
        ).fetchall()
    )


def _positive_float(*values: Any) -> float | None:
    for value in values:
        parsed = _to_float(value)
        if parsed is not None and parsed > 0 and math.isfinite(parsed):
            return float(parsed)
    return None


def _order_price(details: Mapping[str, Any]) -> float | None:
    return _positive_float(
        details.get("order_price"),
        details.get("price"),
        details.get("entry_usable_edge_effective_price"),
        details.get("exec_entry_price"),
        details.get("ask"),
        details.get("best_ask"),
    )


def _hypothetical_size(details: Mapping[str, Any], price: float | None) -> float | None:
    direct = _positive_float(
        details.get("order_size"),
        details.get("size"),
        details.get("size_scaled"),
        details.get("kelly_size_before"),
        details.get("size_base"),
    )
    if direct is not None:
        return direct
    notional = _positive_float(
        details.get("kelly_target_notional"),
        details.get("kelly_base_notional"),
        details.get("order_amount"),
        details.get("exec_entry_amount"),
    )
    if notional is not None and price is not None and price > 0:
        return float(notional) / float(price)
    return None


def _primary_rejection_reason(reason_codes: Sequence[str], details: Mapping[str, Any]) -> str:
    first_wrapper = ""
    for code in reason_codes:
        text = str(code or "").strip()
        if not text:
            continue
        if text.lower() in WRAPPER_REASON_CODES:
            first_wrapper = first_wrapper or text
            continue
        if text:
            return text
    for key in (
        "entry_edge_false_edge_type",
        "entry_usable_edge_na_reason",
        "edge_realizable_soft_block_reason",
        "source_fit_na_reason",
        "source_na_reason",
    ):
        text = str(details.get(key) or "").strip()
        if text:
            return text
    soft_codes = _safe_json_list(details.get("entry_soft_reason_codes"))
    if soft_codes:
        return str(soft_codes[0])
    original_codes = _safe_json_list(details.get("entry_original_reason_codes"))
    if original_codes:
        return str(original_codes[0])
    kelly_reason = str(details.get("kelly_reason") or "").strip()
    if kelly_reason and kelly_reason.lower() not in {"applied", "ok", "pass", "passed"}:
        return kelly_reason
    usable = _to_float(details.get("entry_usable_edge"))
    if usable is not None and usable <= 0:
        return "entry_usable_edge_nonpositive"
    if _boolish(details.get("edge_tradeability_blocked")):
        return "edge_tradeability_blocked"
    if _boolish(details.get("entry_soft_no_order")):
        return "entry_soft_no_order"
    if first_wrapper:
        return first_wrapper
    return "no_order_without_reason_code"


def _rule_taxonomy(reason: Any) -> dict[str, str]:
    text = str(reason or "").strip().lower()
    if not text:
        return {
            "gate_class": "diagnostic_or_legacy",
            "gate_subtype": "missing_reason",
            "recommended_treatment": "add_specific_reason_code_before_policy_changes",
        }
    hard_terms = {
        "data_invalid": (
            "data_stale",
            "facts_stale",
            "market_data_stale",
            "market_tick_stale",
            "orderbook_stale",
            "quote_stale",
            "ssot_stale",
            "invalid_snapshot",
            "market_snapshot_missing",
            "market_tick_missing",
            "price_snapshot_missing",
            "quote_missing",
            "snapshot_invalid",
            "snapshot_missing",
            "sort_missing",
            "market_price_fallback_unverified",
            "unverified_market_price",
            "unverified_snapshot",
        ),
        "risk_redline": (
            "balance_insufficient",
            "exposure_cap",
            "exit_flag",
            "other_side_active",
            "position_cap",
            "risk_cap",
            "loss_streak",
            "max_new_orders",
            "reentry_blocked",
            "reentry_cooldown",
            "cooldown",
            "entry_balance_insufficient",
        ),
        "causal_or_contract_safety": (
            "contract_rule",
            "station_mapping",
            "settlement_source",
            "token_missing",
            "token_id_missing",
            "invalid_token",
            "outcome_token_missing",
            "main_candidate_missing_snapshot",
        ),
        "exchange_or_execution_exception": (
            "market_closed",
            "market_inactive",
            "market_archived",
            "not_accepting_orders",
            "orderbook_missing",
            "clob_",
            "event_worker_failed",
            "event_worker_timeout",
            "entry_pending_state",
            "duplicate_open_order",
            "already_in_entry",
            "retry_blocked",
        ),
    }
    soft_terms = {
        "sample_insufficient": (
            "low_sample",
            "sample",
            "evidence_missing",
            "evidence_low_sample",
            "execution_evidence_missing",
            "execution_evidence_low_sample",
            "evidence_default",
        ),
        "liquidity_or_price_weak": (
            "liquidity",
            "spread",
            "depth",
            "volume",
            "price",
            "q_lcb_below_effective_entry_price",
            "q_below",
            "overprice",
            "target_exit_does_not_cover_friction",
            "exit_path_not_tradeable",
            "target_exit_not_tradeable",
            "edge_tradeability_blocked",
            "entry_usable_edge_nonpositive",
            "edge_lcb_below_min",
            "signal_lcb_below_min",
        ),
        "calibration_or_probability_uncertain": (
            "probability",
            "calibration",
            "edge_tradeability_missing",
            "edge_realizable",
            "plain_lcb_min_total_edge_below_min",
            "target_exit_execution_probability_too_low",
        ),
        "regime_uncertain": (
            "regime",
            "extreme_tail",
            "tail",
            "plain_lcb_peak_no_filtered",
            "plain_lcb_pinned_tail_no_filtered",
            "soft_shape_filter_plain_lcb_pinned_tail_no",
            "strategy_size_ratio_zero",
            "kelly_nonpositive",
        ),
    }
    if text in {*WRAPPER_REASON_CODES, "applied"}:
        subtype = "wrapper_reason" if "block" in text else "non_specific_reason"
        return {
            "gate_class": "diagnostic_or_legacy",
            "gate_subtype": subtype,
            "recommended_treatment": "replace_with_specific_underlying_reason",
        }
    # Hard invariants first: these are unsafe to dilute into a size-only penalty.
    for subtype, terms in hard_terms.items():
        if text in terms or any(term in text for term in terms):
            return {
                "gate_class": "hard_gate",
                "gate_subtype": subtype,
                "recommended_treatment": "keep_as_hard_block_and_fix_source_if_noisy",
            }
    for subtype, terms in soft_terms.items():
        if text in terms or any(term in text for term in terms):
            return {
                "gate_class": "soft_haircut",
                "gate_subtype": subtype,
                "recommended_treatment": "use_as_size_or_edge_haircut_not_standalone_hard_block",
            }
    return {
        "gate_class": "needs_mapping",
        "gate_subtype": "unknown",
        "recommended_treatment": "map_to_hard_gate_or_soft_haircut_before_policy_changes",
    }


def _taxonomy_totals(rows: Iterable[Mapping[str, Any]], *, key_fields: Sequence[str]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(str(row.get(field) or "unknown") for field in key_fields)
        item = counts.setdefault(
            key,
            {
                **{field: value for field, value in zip(key_fields, key)},
                "reason_count": 0,
                "total_count": 0,
                "active_count": 0,
                "settled_count": 0,
                "hit_count": 0,
            },
        )
        item["reason_count"] = int(item["reason_count"] or 0) + 1
        item["total_count"] = int(item["total_count"] or 0) + int(row.get("total_count") or 0)
        item["active_count"] = int(item["active_count"] or 0) + int(row.get("active_count") or 0)
        item["settled_count"] = int(item["settled_count"] or 0) + int(row.get("settled_count") or 0)
        item["hit_count"] = int(item["hit_count"] or 0) + int(row.get("hit_count") or 0)
    out = list(counts.values())
    for item in out:
        item["hit_rate"] = _rate(int(item.get("hit_count") or 0), int(item.get("settled_count") or 0))
    out.sort(
        key=lambda item: (
            -int(item.get("active_count") or 0),
            -int(item.get("total_count") or 0),
            tuple(str(item.get(field) or "") for field in key_fields),
        )
    )
    return out


def _taxonomy_top(rows: Iterable[Mapping[str, Any]], gate_class: str, *, limit: int = 10) -> list[dict[str, Any]]:
    selected = [row for row in rows if str(row.get("gate_class") or "") == gate_class]
    selected.sort(
        key=lambda row: (
            -int(row.get("active_count") or 0),
            -int(row.get("total_count") or 0),
            str(row.get("rejection_reason") or ""),
        )
    )
    return [dict(row) for row in selected[: max(1, int(limit))]]


def _build_hard_soft_taxonomy(rows: Sequence[Mapping[str, Any]], *, limit: int = 10) -> dict[str, Any]:
    class_counts = _taxonomy_totals(rows, key_fields=("gate_class",))
    subtype_counts = _taxonomy_totals(rows, key_fields=("gate_class", "gate_subtype"))
    active_needs_mapping = [
        row
        for row in rows
        if str(row.get("gate_class") or "") == "needs_mapping" and int(row.get("active_count") or 0) > 0
    ]
    active_diagnostic = [
        row
        for row in rows
        if str(row.get("gate_class") or "") == "diagnostic_or_legacy"
        and int(row.get("active_count") or 0) > 0
    ]
    active_soft = [
        row
        for row in rows
        if str(row.get("gate_class") or "") == "soft_haircut" and int(row.get("active_count") or 0) > 0
    ]
    if active_needs_mapping:
        next_task = "reject_rule_taxonomy_mapping_review"
        recommended_action = "map_active_unknown_reject_reasons_before_policy_changes"
    elif active_diagnostic:
        next_task = "reject_reason_specificity_review"
        recommended_action = "replace_active_wrapper_or_non_specific_reasons_with_underlying_codes"
    elif active_soft:
        next_task = "soft_haircut_policy_review"
        recommended_action = "review_active_soft_haircuts_for_size_or_edge_penalty_treatment"
    else:
        next_task = "rule_effectiveness_monitoring"
        recommended_action = "keep_hard_gates_hard_and_continue_reject_rule_monitoring"
    return {
        "ok": True,
        "taxonomy_version": "hard_soft_v1_2026_05_06",
        "scope": "reporting_only_no_policy_change",
        "hard_gate_subtypes": sorted(HARD_GATE_SUBTYPES),
        "soft_haircut_subtypes": sorted(SOFT_HAIRCUT_SUBTYPES),
        "definitions": {
            "hard_gate": "unsafe_to_trade_until_source_or_risk_condition_is_fixed",
            "soft_haircut": "eligible_for_size_or_edge_penalty_review_not_a_standalone_hard_block",
            "diagnostic_or_legacy": "reason_code_does_not_identify_the_underlying_rule",
            "needs_mapping": "reason_code_is_not_yet_classified",
        },
        "class_counts": class_counts,
        "subtype_counts": subtype_counts,
        "active_soft_haircut_reason_count": len(active_soft),
        "active_needs_mapping_reason_count": len(active_needs_mapping),
        "active_diagnostic_or_legacy_reason_count": len(active_diagnostic),
        "hard_gate_top": _taxonomy_top(rows, "hard_gate", limit=limit),
        "soft_haircut_top": _taxonomy_top(rows, "soft_haircut", limit=limit),
        "needs_mapping_top": _taxonomy_top(rows, "needs_mapping", limit=limit),
        "diagnostic_or_legacy_top": _taxonomy_top(rows, "diagnostic_or_legacy", limit=limit),
        "next_mainline_task": next_task,
        "recommended_action": recommended_action,
        "notes": [
            "Hard Gate keeps data invalid, risk redline, contract/causal safety, and exchange/execution exception conditions as hard blocks.",
            "Soft Haircut marks sample, liquidity/price, probability/calibration, and regime uncertainty rules for penalty treatment review.",
            "This taxonomy is report-only; trading behavior is unchanged by Reject Ledger materialization.",
        ],
    }


def _soft_haircut_policy_item(row: Mapping[str, Any]) -> dict[str, Any]:
    active_count = int(row.get("active_count") or 0)
    settled_count = int(row.get("settled_count") or 0)
    hit_rate = _to_float(row.get("hit_rate"))
    avg_unit_pnl = _to_float(row.get("avg_counterfactual_pnl_per_share"))
    realized_pnl = _to_float(row.get("realized_counterfactual_pnl_sum"))
    disposition = "historical_only"
    recommended_policy_action = "keep_for_audit_not_current_policy_work"
    evidence_label = "inactive_reason"
    release_ready = False
    if active_count > 0 and settled_count < QUALITY_REVIEW_MIN_SETTLED:
        disposition = "collect_more_evidence"
        recommended_policy_action = "wait_for_more_settled_counterfactual_samples"
        evidence_label = "active_but_insufficient_settled_sample"
    elif active_count > 0 and avg_unit_pnl is None:
        disposition = "collect_more_evidence"
        recommended_policy_action = "wait_for_counterfactual_unit_pnl"
        evidence_label = "missing_unit_pnl"
    elif active_count > 0 and avg_unit_pnl is not None and avg_unit_pnl <= 0:
        if hit_rate is not None and hit_rate >= QUALITY_REVIEW_FALSE_REJECT_HIT_RATE:
            disposition = "bucket_split_before_haircut"
            recommended_policy_action = "split_by_price_strategy_and_size_before_relaxing_zero_size"
            evidence_label = "high_hit_rate_but_negative_unit_pnl"
        else:
            disposition = "keep_zero_size"
            recommended_policy_action = "keep_zero_size_soft_haircut_until_unit_pnl_improves"
            evidence_label = "negative_unit_pnl"
    elif active_count > 0 and avg_unit_pnl is not None and avg_unit_pnl > 0:
        if hit_rate is not None and hit_rate <= QUALITY_REVIEW_PROTECTIVE_HIT_RATE:
            disposition = "bucket_split_before_haircut"
            recommended_policy_action = "identify_positive_sub_bucket_before_any_size_floor"
            evidence_label = "positive_unit_pnl_but_low_hit_rate"
        elif hit_rate is not None and hit_rate >= QUALITY_REVIEW_FALSE_REJECT_HIT_RATE:
            disposition = "candidate_continuous_haircut"
            recommended_policy_action = "evaluate_small_size_or_edge_haircut_in_shadow_first"
            evidence_label = "positive_unit_pnl_and_high_hit_rate"
            release_ready = True
        else:
            disposition = "monitor_before_policy_change"
            recommended_policy_action = "continue_monitoring_until_direction_is_stable"
            evidence_label = "positive_unit_pnl_mixed_hit_rate"
    if (
        active_count > 0
        and avg_unit_pnl is not None
        and avg_unit_pnl <= 0
        and realized_pnl is not None
        and realized_pnl > 0
    ):
        evidence_label = f"{evidence_label}_size_weighted_pnl_positive"
    return {
        "rejection_reason": str(row.get("rejection_reason") or "unknown"),
        "gate_subtype": str(row.get("gate_subtype") or "unknown"),
        "active_count": active_count,
        "total_count": int(row.get("total_count") or 0),
        "settled_count": settled_count,
        "hit_rate": hit_rate,
        "avg_counterfactual_pnl_per_share": avg_unit_pnl,
        "realized_counterfactual_pnl_sum": realized_pnl,
        "avg_order_price": _to_float(row.get("avg_order_price")),
        "avg_ask": _to_float(row.get("avg_ask")),
        "avg_spread": _to_float(row.get("avg_spread")),
        "avg_entry_usable_edge": _to_float(row.get("avg_entry_usable_edge")),
        "avg_edge_tradeability_profit_value": _to_float(
            row.get("avg_edge_tradeability_profit_value")
        ),
        "avg_edge_tradeability_edge_lcb": _to_float(row.get("avg_edge_tradeability_edge_lcb")),
        "strategy_family_top": list(row.get("strategy_family_top") or []),
        "review_bucket": str(row.get("review_bucket") or "unknown"),
        "disposition": disposition,
        "evidence_label": evidence_label,
        "release_ready": bool(release_ready),
        "recommended_policy_action": recommended_policy_action,
    }


def _build_soft_haircut_policy_review(
    rows: Sequence[Mapping[str, Any]], *, limit: int = 10
) -> dict[str, Any]:
    soft_rows = [row for row in rows if str(row.get("gate_class") or "") == "soft_haircut"]
    items = [_soft_haircut_policy_item(row) for row in soft_rows]
    items.sort(
        key=lambda item: (
            -int(item.get("active_count") or 0),
            str(item.get("disposition") or ""),
            -int(item.get("total_count") or 0),
            str(item.get("rejection_reason") or ""),
        )
    )
    active_items = [item for item in items if int(item.get("active_count") or 0) > 0]
    disposition_counts: dict[str, int] = {}
    active_disposition_counts: dict[str, int] = {}
    for item in items:
        disposition = str(item.get("disposition") or "unknown")
        disposition_counts[disposition] = disposition_counts.get(disposition, 0) + 1
        if int(item.get("active_count") or 0) > 0:
            active_disposition_counts[disposition] = active_disposition_counts.get(disposition, 0) + 1
    release_ready = [item for item in active_items if bool(item.get("release_ready"))]
    bucket_split = [
        item for item in active_items if str(item.get("disposition") or "") == "bucket_split_before_haircut"
    ]
    keep_zero = [item for item in active_items if str(item.get("disposition") or "") == "keep_zero_size"]
    collect_more = [
        item for item in active_items if str(item.get("disposition") or "") == "collect_more_evidence"
    ]
    if release_ready:
        next_task = "soft_haircut_shadow_haircut_design"
        recommended_action = "design_shadow_or_tiny_size_haircut_for_release_ready_soft_reasons"
    elif bucket_split:
        next_task = "soft_haircut_bucket_split_review"
        recommended_action = "split_ambiguous_soft_haircuts_by_price_strategy_and_size"
    elif collect_more:
        next_task = "soft_haircut_sample_accrual"
        recommended_action = "continue_collecting_counterfactual_samples_for_active_soft_haircuts"
    elif keep_zero:
        next_task = "rule_effectiveness_monitoring"
        recommended_action = "keep_active_soft_haircuts_at_zero_size_and_monitor"
    else:
        next_task = "rule_effectiveness_monitoring"
        recommended_action = "no_active_soft_haircut_policy_work"
    return {
        "ok": True,
        "scope": "reporting_only_no_policy_change",
        "active_soft_haircut_reason_count": len(active_items),
        "release_ready_reason_count": len(release_ready),
        "bucket_split_reason_count": len(bucket_split),
        "keep_zero_size_reason_count": len(keep_zero),
        "collect_more_evidence_reason_count": len(collect_more),
        "disposition_counts": [
            {"key": key, "count": count}
            for key, count in sorted(disposition_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "active_disposition_counts": [
            {"key": key, "count": count}
            for key, count in sorted(active_disposition_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "release_ready": release_ready[: max(1, int(limit))],
        "bucket_split_before_haircut": bucket_split[: max(1, int(limit))],
        "keep_zero_size": keep_zero[: max(1, int(limit))],
        "collect_more_evidence": collect_more[: max(1, int(limit))],
        "active_rows": active_items[: max(1, int(limit))],
        "rows": items[: max(1, int(limit))],
        "next_mainline_task": next_task,
        "recommended_action": recommended_action,
        "notes": [
            "avg_counterfactual_pnl_per_share is the primary unit-economics signal for policy review.",
            "Positive realized_counterfactual_pnl_sum with negative unit PnL is treated as a bucket-split question, not direct release evidence.",
            "This review is report-only; it does not alter order sizing or entry gates.",
        ],
    }


def _rule_pruning_item(row: Mapping[str, Any]) -> dict[str, Any]:
    gate_class = str(row.get("gate_class") or "needs_mapping")
    active_count = int(row.get("active_count") or 0)
    settled_count = int(row.get("settled_count") or 0)
    hit_rate = _to_float(row.get("hit_rate"))
    avg_unit_pnl = _to_float(row.get("avg_counterfactual_pnl_per_share"))
    realized_pnl = _to_float(row.get("realized_counterfactual_pnl_sum"))
    size_gap_rate = _to_float(row.get("size_missing_pnl_rate"))
    evidence_status = "sufficient" if settled_count >= RULE_PRUNING_MIN_SETTLED else "insufficient"
    protection_level = "unknown"
    false_reject_level = "unknown"
    quadrant = "needs_more_evidence"
    action = "collect_more_counterfactuals_before_pruning"
    notes: list[str] = []

    if gate_class == "hard_gate":
        quadrant = "hard_invariant_not_pruned"
        action = "keep_hard_gate_and_only_fix_noisy_source_or_specificity"
        protection_level = "not_pnl_scored"
        false_reject_level = "not_pnl_scored"
        notes.append("Hard Gate is a safety invariant, so profitable counterfactuals do not imply pruning.")
    elif gate_class in {"needs_mapping", "diagnostic_or_legacy"}:
        quadrant = "mapping_or_specificity_required"
        action = "map_to_specific_hard_or_soft_reason_before_pruning"
        notes.append("Rule pruning requires a specific underlying reason code.")
    elif settled_count < RULE_PRUNING_MIN_SETTLED:
        notes.append("Settled sample is below pruning threshold.")
    else:
        protective_like = bool(avg_unit_pnl is not None and avg_unit_pnl < -RULE_PRUNING_NEUTRAL_UNIT_PNL)
        neutral_like = bool(
            avg_unit_pnl is not None and abs(float(avg_unit_pnl)) <= RULE_PRUNING_NEUTRAL_UNIT_PNL
        )
        false_reject_like = bool(
            avg_unit_pnl is not None
            and avg_unit_pnl > RULE_PRUNING_NEUTRAL_UNIT_PNL
            and (realized_pnl is None or realized_pnl > 0)
        )
        high_hit_negative_unit = bool(
            hit_rate is not None
            and hit_rate >= QUALITY_REVIEW_FALSE_REJECT_HIT_RATE
            and avg_unit_pnl is not None
            and avg_unit_pnl <= 0
        )
        size_weighted_positive_negative_unit = bool(
            avg_unit_pnl is not None
            and avg_unit_pnl <= 0
            and realized_pnl is not None
            and realized_pnl > 0
        )
        if high_hit_negative_unit or size_weighted_positive_negative_unit:
            protection_level = "high"
            false_reject_level = "high"
            quadrant = "high_protection_high_false_reject"
            action = "segment_by_bucket_before_weighting_or_pruning"
        elif protective_like:
            protection_level = "high"
            false_reject_level = "low"
            quadrant = "high_protection_low_false_reject"
            action = "keep_rule"
        elif false_reject_like:
            protection_level = "low"
            false_reject_level = "high"
            quadrant = "low_protection_high_false_reject"
            action = "shadow_probe_before_delete_or_weight_release"
        elif neutral_like:
            protection_level = "low"
            false_reject_level = "low"
            quadrant = "low_protection_low_false_reject"
            action = "deprioritize_rule_and_keep_for_audit"
        else:
            quadrant = "mixed_or_unclear"
            action = "continue_monitoring_before_pruning"
        if active_count <= 0:
            action = "historical_audit_only_no_active_policy_change"
        if size_gap_rate is not None and size_gap_rate > 0:
            notes.append("Some settled rejects lack hypothetical size; use unit PnL more than size-weighted PnL.")

    return {
        "rejection_reason": str(row.get("rejection_reason") or "unknown"),
        "gate_class": gate_class,
        "gate_subtype": str(row.get("gate_subtype") or "unknown"),
        "active_count": active_count,
        "total_count": int(row.get("total_count") or 0),
        "settled_count": settled_count,
        "hit_rate": hit_rate,
        "avg_counterfactual_pnl_per_share": avg_unit_pnl,
        "realized_counterfactual_pnl_sum": realized_pnl,
        "size_missing_pnl_rate": size_gap_rate,
        "evidence_status": evidence_status,
        "protection_level": protection_level,
        "false_reject_level": false_reject_level,
        "pruning_quadrant": quadrant,
        "recommended_pruning_action": action,
        "strategy_family_top": list(row.get("strategy_family_top") or []),
        "notes": notes,
    }


def _build_rule_pruning_review(
    rows: Sequence[Mapping[str, Any]], *, limit: int = 10
) -> dict[str, Any]:
    items = [_rule_pruning_item(row) for row in rows]
    active_items = [item for item in items if int(item.get("active_count") or 0) > 0]
    quadrant_counts: dict[str, int] = {}
    active_quadrant_counts: dict[str, int] = {}
    for item in items:
        quadrant = str(item.get("pruning_quadrant") or "unknown")
        quadrant_counts[quadrant] = quadrant_counts.get(quadrant, 0) + 1
        if int(item.get("active_count") or 0) > 0:
            active_quadrant_counts[quadrant] = active_quadrant_counts.get(quadrant, 0) + 1

    def select(quadrant: str, *, active_only: bool = True) -> list[dict[str, Any]]:
        source = active_items if active_only else items
        out = [item for item in source if str(item.get("pruning_quadrant") or "") == quadrant]
        out.sort(
            key=lambda item: (
                -int(item.get("active_count") or 0),
                -int(item.get("settled_count") or 0),
                str(item.get("rejection_reason") or ""),
            )
        )
        return out[: max(1, int(limit))]

    shadow_before_delete = select("low_protection_high_false_reject")
    segment_or_weight = select("high_protection_high_false_reject")
    keep_rules = select("high_protection_low_false_reject")
    deprioritize = select("low_protection_low_false_reject")
    mapping_required = [
        item
        for item in active_items
        if str(item.get("pruning_quadrant") or "") == "mapping_or_specificity_required"
    ][: max(1, int(limit))]

    if mapping_required:
        next_task = "reject_rule_taxonomy_mapping_review"
        action = "map_active_unknown_or_wrapper_reasons_before_rule_pruning"
    elif shadow_before_delete:
        next_task = "rule_pruning_shadow_probe_review"
        action = "shadow_probe_low_protection_high_false_reject_rules_before_pruning"
    elif segment_or_weight:
        next_task = "rule_pruning_bucket_segmentation_review"
        action = "segment_high_protection_high_false_reject_rules_before_weighting"
    elif deprioritize:
        next_task = "rule_pruning_deprioritization_review"
        action = "deprioritize_low_impact_soft_rules_in_reporting_before_code_removal"
    else:
        next_task = "rule_effectiveness_monitoring"
        action = "continue_monitoring_rule_pruning_quadrants"

    return {
        "ok": True,
        "scope": "reporting_only_no_policy_change",
        "review_version": "rule_pruning_v1_2026_05_06",
        "min_settled_for_pruning": RULE_PRUNING_MIN_SETTLED,
        "neutral_unit_pnl_band": RULE_PRUNING_NEUTRAL_UNIT_PNL,
        "definitions": {
            "high_protection_low_false_reject": "unit PnL is materially negative; keep the rule",
            "high_protection_high_false_reject": "rule protects on average but likely contains profitable sub-buckets; segment before weighting",
            "low_protection_low_false_reject": "unit PnL is near zero; deprioritize before any code removal",
            "low_protection_high_false_reject": "unit and size-weighted PnL are positive; shadow probe before delete or release",
            "hard_invariant_not_pruned": "Hard Gate rules stay hard and are not PnL-pruned",
        },
        "quadrant_counts": [
            {"key": key, "count": count}
            for key, count in sorted(quadrant_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "active_quadrant_counts": [
            {"key": key, "count": count}
            for key, count in sorted(active_quadrant_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "keep_rules": keep_rules,
        "segment_or_weight_before_pruning": segment_or_weight,
        "deprioritize_rules": deprioritize,
        "shadow_before_delete_or_release": shadow_before_delete,
        "mapping_required": mapping_required,
        "hard_invariant_not_pruned": select("hard_invariant_not_pruned", active_only=False),
        "active_rows": active_items[: max(1, int(limit))],
        "rows": items[: max(1, int(limit))],
        "next_mainline_task": next_task,
        "recommended_action": action,
        "notes": [
            "Rule pruning is report-only; it does not delete code, modify thresholds, or submit orders.",
            "Hard Gate rules are excluded from PnL-based pruning and only support source-noise cleanup.",
            "Soft Haircut delete/release candidates must pass shadow or small-live review first.",
        ],
    }


def _bucket_case_sql(column: str, prefix: str, edges: Sequence[tuple[float, str]]) -> str:
    parts = [f"CASE WHEN {column} IS NULL THEN 'unknown'"]
    for edge, label in edges:
        parts.append(f"WHEN {column} < {float(edge)} THEN '{label}'")
    parts.append(f"ELSE '{prefix}_plus' END")
    return " ".join(parts)


def _bucket_label(value: Any, prefix: str, edges: Sequence[tuple[float, str]]) -> str:
    parsed = _to_float(value)
    if parsed is None:
        return "unknown"
    for edge, label in edges:
        if float(parsed) < float(edge):
            return str(label)
    return f"{prefix}_plus"


PRICE_BUCKET_EDGES = (
    (0.02, "p00_002"),
    (0.05, "p02_005"),
    (0.10, "p05_010"),
    (0.25, "p10_025"),
    (0.50, "p25_050"),
    (0.75, "p50_075"),
)
NOTIONAL_BUCKET_EDGES = (
    (0.5, "n000_050"),
    (1.0, "n050_100"),
    (2.0, "n100_200"),
    (5.0, "n200_500"),
)


def _price_bucket(value: Any) -> str:
    return _bucket_label(value, "p75_100", PRICE_BUCKET_EDGES)


def _notional_bucket(value: Any) -> str:
    return _bucket_label(value, "n500", NOTIONAL_BUCKET_EDGES)


def _soft_bucket_disposition(row: Mapping[str, Any]) -> dict[str, Any]:
    active_count = int(row.get("active_count") or 0)
    settled_count = int(row.get("settled_count") or 0)
    hit_count = int(row.get("hit_count") or 0)
    hit_rate = _to_float(row.get("hit_rate"))
    avg_unit_pnl = _to_float(row.get("avg_counterfactual_pnl_per_share"))
    realized_pnl = _to_float(row.get("realized_counterfactual_pnl_sum"))
    avg_price = _to_float(row.get("avg_order_price"))
    disposition = "historical_only"
    action = "keep_for_audit_not_current_policy_work"
    evidence = "inactive_bucket"
    candidate = False
    if active_count > 0 and settled_count < SOFT_HAIRCUT_BUCKET_MIN_SETTLED:
        disposition = "insufficient_exact_bucket_evidence"
        action = "collect_more_exact_bucket_counterfactuals"
        evidence = "active_bucket_below_min_settled"
    elif active_count > 0 and avg_unit_pnl is not None and avg_unit_pnl > 0 and hit_count > 0:
        if realized_pnl is None or realized_pnl <= 0:
            disposition = "monitor_before_shadow_probe"
            action = "wait_for_size_weighted_pnl_confirmation"
            evidence = "positive_unit_pnl_without_positive_size_weighted_pnl"
        else:
            disposition = "shadow_probe_candidate"
            action = "design_shadow_or_tiny_size_probe_with_independent_budget"
            evidence = "positive_unit_pnl_exact_bucket"
            candidate = True
            if hit_rate is not None and hit_rate <= QUALITY_REVIEW_PROTECTIVE_HIT_RATE:
                evidence = "positive_unit_pnl_low_hit_tail_bucket"
            if avg_price is not None and avg_price < 0.05:
                evidence = f"{evidence}_low_price"
    elif active_count > 0 and avg_unit_pnl is not None and avg_unit_pnl <= 0:
        disposition = "keep_zero_size"
        action = "keep_exact_bucket_at_zero_size"
        evidence = "negative_unit_pnl_exact_bucket"
        if hit_rate is not None and hit_rate >= QUALITY_REVIEW_FALSE_REJECT_HIT_RATE:
            evidence = "high_hit_rate_but_negative_unit_pnl_exact_bucket"
    elif active_count > 0:
        disposition = "monitor_before_policy_change"
        action = "continue_monitoring_exact_bucket"
        evidence = "mixed_or_missing_exact_bucket_evidence"
    return {
        "disposition": disposition,
        "evidence_label": evidence,
        "candidate_shadow_probe": bool(candidate),
        "recommended_policy_action": action,
    }


def _build_soft_haircut_bucket_split_review(
    conn: sqlite3.Connection,
    *,
    reasons: Sequence[str],
    current_run_id: int,
    limit: int = 40,
) -> dict[str, Any]:
    reason_values = sorted({str(reason or "").strip() for reason in reasons if str(reason or "").strip()})
    if not reason_values:
        return {
            "ok": True,
            "scope": "reporting_only_no_policy_change",
            "min_settled_per_bucket": SOFT_HAIRCUT_BUCKET_MIN_SETTLED,
            "active_bucket_count": 0,
            "shadow_probe_candidate_count": 0,
            "next_mainline_task": "rule_effectiveness_monitoring",
            "recommended_action": "no_bucket_split_reasons_active",
            "active_rows": [],
            "shadow_probe_candidates": [],
            "keep_zero_size": [],
            "insufficient_exact_bucket_evidence": [],
        }
    placeholders = ",".join("?" for _ in reason_values)
    price_bucket_sql = _bucket_case_sql(
        "order_price",
        "p75_100",
        PRICE_BUCKET_EDGES,
    )
    notional_bucket_sql = _bucket_case_sql(
        "hypothetical_notional",
        "n500",
        NOTIONAL_BUCKET_EDGES,
    )
    params: list[Any] = [*reason_values, int(current_run_id or 0), *reason_values]
    rows = conn.execute(
        f"""
        WITH base AS (
            SELECT
              rejection_reason,
              COALESCE(NULLIF(strategy_family, ''), 'unknown') AS strategy_family,
              COALESCE(NULLIF(UPPER(side), ''), 'unknown') AS side,
              {price_bucket_sql} AS price_bucket,
              {notional_bucket_sql} AS notional_bucket,
              run_id,
              side_hit,
              subsequent_outcome,
              counterfactual_pnl_per_share,
              realized_counterfactual_pnl,
              order_price,
              hypothetical_notional,
              ask,
              spread,
              entry_usable_edge,
              edge_tradeability_profit_value
            FROM {LEDGER_TABLE}
            WHERE rejection_reason IN ({placeholders})
        ), active AS (
            SELECT
              rejection_reason,
              strategy_family,
              side,
              price_bucket,
              notional_bucket,
              COUNT(*) AS active_count
            FROM base
            WHERE run_id = ?
            GROUP BY rejection_reason, strategy_family, side, price_bucket, notional_bucket
        ), agg AS (
            SELECT
              rejection_reason,
              strategy_family,
              side,
              price_bucket,
              notional_bucket,
              COUNT(*) AS total_count,
              SUM(CASE WHEN side_hit IS NOT NULL THEN 1 ELSE 0 END) AS settled_count,
              SUM(CASE WHEN side_hit = 1 THEN 1 ELSE 0 END) AS hit_count,
              SUM(CASE WHEN subsequent_outcome = 'settlement_pending' THEN 1 ELSE 0 END) AS pending_count,
              AVG(counterfactual_pnl_per_share) AS avg_counterfactual_pnl_per_share,
              SUM(realized_counterfactual_pnl) AS realized_counterfactual_pnl_sum,
              COUNT(realized_counterfactual_pnl) AS realized_counterfactual_pnl_count,
              AVG(order_price) AS avg_order_price,
              AVG(hypothetical_notional) AS avg_hypothetical_notional,
              AVG(ask) AS avg_ask,
              AVG(spread) AS avg_spread,
              AVG(entry_usable_edge) AS avg_entry_usable_edge,
              AVG(edge_tradeability_profit_value) AS avg_edge_tradeability_profit_value
            FROM base
            GROUP BY rejection_reason, strategy_family, side, price_bucket, notional_bucket
        )
        SELECT
          agg.*,
          COALESCE(active.active_count, 0) AS active_count
        FROM agg
        LEFT JOIN active USING(rejection_reason, strategy_family, side, price_bucket, notional_bucket)
        WHERE agg.rejection_reason IN ({placeholders})
        ORDER BY COALESCE(active.active_count, 0) DESC,
                 agg.settled_count DESC,
                 agg.total_count DESC,
                 agg.rejection_reason ASC,
                 agg.strategy_family ASC,
                 agg.side ASC,
                 agg.price_bucket ASC,
                 agg.notional_bucket ASC
        """,
        params,
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        settled_count = int(row["settled_count"] or 0)
        hit_count = int(row["hit_count"] or 0)
        item: dict[str, Any] = {
            "rejection_reason": str(row["rejection_reason"] or "unknown"),
            "strategy_family": str(row["strategy_family"] or "unknown"),
            "side": str(row["side"] or "unknown"),
            "price_bucket": str(row["price_bucket"] or "unknown"),
            "notional_bucket": str(row["notional_bucket"] or "unknown"),
            "active_count": int(row["active_count"] or 0),
            "total_count": int(row["total_count"] or 0),
            "settled_count": settled_count,
            "hit_count": hit_count,
            "pending_count": int(row["pending_count"] or 0),
            "hit_rate": _rate(hit_count, settled_count),
            "avg_counterfactual_pnl_per_share": _to_float(row["avg_counterfactual_pnl_per_share"]),
            "realized_counterfactual_pnl_sum": _to_float(row["realized_counterfactual_pnl_sum"]),
            "realized_counterfactual_pnl_count": int(row["realized_counterfactual_pnl_count"] or 0),
            "avg_order_price": _to_float(row["avg_order_price"]),
            "avg_hypothetical_notional": _to_float(row["avg_hypothetical_notional"]),
            "avg_ask": _to_float(row["avg_ask"]),
            "avg_spread": _to_float(row["avg_spread"]),
            "avg_entry_usable_edge": _to_float(row["avg_entry_usable_edge"]),
            "avg_edge_tradeability_profit_value": _to_float(
                row["avg_edge_tradeability_profit_value"]
            ),
        }
        item.update(_soft_bucket_disposition(item))
        items.append(item)
    active_items = [item for item in items if int(item.get("active_count") or 0) > 0]
    candidates = [item for item in active_items if bool(item.get("candidate_shadow_probe"))]
    keep_zero = [
        item for item in active_items if str(item.get("disposition") or "") == "keep_zero_size"
    ]
    insufficient = [
        item
        for item in active_items
        if str(item.get("disposition") or "") == "insufficient_exact_bucket_evidence"
    ]
    disposition_counts: dict[str, int] = {}
    for item in active_items:
        disposition = str(item.get("disposition") or "unknown")
        disposition_counts[disposition] = disposition_counts.get(disposition, 0) + 1
    if candidates:
        next_task = "soft_haircut_shadow_probe_design"
        recommended_action = "design_shadow_or_tiny_size_probe_for_positive_exact_buckets"
    elif insufficient:
        next_task = "soft_haircut_bucket_sample_accrual"
        recommended_action = "continue_collecting_exact_bucket_counterfactuals"
    else:
        next_task = "rule_effectiveness_monitoring"
        recommended_action = "keep_active_soft_haircut_buckets_at_zero_size_and_monitor"
    return {
        "ok": True,
        "scope": "reporting_only_no_policy_change",
        "min_settled_per_bucket": SOFT_HAIRCUT_BUCKET_MIN_SETTLED,
        "review_reasons": reason_values,
        "active_bucket_count": len(active_items),
        "shadow_probe_candidate_count": len(candidates),
        "keep_zero_size_bucket_count": len(keep_zero),
        "insufficient_exact_bucket_evidence_count": len(insufficient),
        "active_disposition_counts": [
            {"key": key, "count": count}
            for key, count in sorted(disposition_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "shadow_probe_candidates": candidates[: max(1, int(limit))],
        "keep_zero_size": keep_zero[: max(1, int(limit))],
        "insufficient_exact_bucket_evidence": insufficient[: max(1, int(limit))],
        "active_rows": active_items[: max(1, int(limit))],
        "top_positive_historical_buckets": [
            item
            for item in sorted(
                items,
                key=lambda item: (
                    -float(item.get("avg_counterfactual_pnl_per_share") or -999.0),
                    -int(item.get("settled_count") or 0),
                ),
            )
            if _to_float(item.get("avg_counterfactual_pnl_per_share")) is not None
            and float(item.get("avg_counterfactual_pnl_per_share") or 0.0) > 0
            and int(item.get("settled_count") or 0) >= SOFT_HAIRCUT_BUCKET_MIN_SETTLED
        ][: max(1, int(limit))],
        "rows": items[: max(1, int(limit))],
        "next_mainline_task": next_task,
        "recommended_action": recommended_action,
        "notes": [
            "Exact buckets are reason x strategy_family x side x price_bucket x notional_bucket.",
            "Positive exact buckets are candidates for shadow or tiny-size probing only, not live release.",
            "This review is report-only and does not alter entry gates or order sizing.",
        ],
    }


def _shadow_probe_design_item(bucket: Mapping[str, Any]) -> dict[str, Any]:
    avg_notional = _to_float(bucket.get("avg_hypothetical_notional"))
    if avg_notional is None or avg_notional <= 0:
        avg_notional = SHADOW_PROBE_DEFAULT_MAX_NOTIONAL_USDC
    tiny_max_notional = min(SHADOW_PROBE_DEFAULT_MAX_NOTIONAL_USDC, float(avg_notional))
    daily_cap = tiny_max_notional * SHADOW_PROBE_DEFAULT_MAX_PROBES_PER_ET_DAY
    exact_bucket = {
        "rejection_reason": str(bucket.get("rejection_reason") or "unknown"),
        "strategy_family": str(bucket.get("strategy_family") or "unknown"),
        "side": str(bucket.get("side") or "unknown"),
        "price_bucket": str(bucket.get("price_bucket") or "unknown"),
        "notional_bucket": str(bucket.get("notional_bucket") or "unknown"),
    }
    return {
        "probe_id": "soft_haircut_shadow_probe_v1:"
        + ":".join(str(exact_bucket[key]) for key in sorted(exact_bucket)),
        "mode": "shadow",
        "live_orders_enabled": False,
        "tiny_live_candidate_enabled": False,
        "exact_bucket": exact_bucket,
        "evidence": {
            "active_count": int(bucket.get("active_count") or 0),
            "settled_count": int(bucket.get("settled_count") or 0),
            "hit_count": int(bucket.get("hit_count") or 0),
            "hit_rate": _to_float(bucket.get("hit_rate")),
            "avg_counterfactual_pnl_per_share": _to_float(
                bucket.get("avg_counterfactual_pnl_per_share")
            ),
            "realized_counterfactual_pnl_sum": _to_float(
                bucket.get("realized_counterfactual_pnl_sum")
            ),
            "avg_order_price": _to_float(bucket.get("avg_order_price")),
            "avg_hypothetical_notional": _to_float(bucket.get("avg_hypothetical_notional")),
            "evidence_label": str(bucket.get("evidence_label") or ""),
        },
        "budget": {
            "budget_scope": "independent_shadow_probe_budget",
            "shadow_notional_per_probe_usdc": round(float(avg_notional), 6),
            "tiny_live_max_notional_per_probe_usdc": round(float(tiny_max_notional), 6),
            "tiny_live_max_probes_per_et_day": SHADOW_PROBE_DEFAULT_MAX_PROBES_PER_ET_DAY,
            "tiny_live_daily_notional_cap_usdc": round(float(daily_cap), 6),
            "tiny_live_weekly_loss_cap_usdc": round(float(daily_cap) * 2.0, 6),
        },
        "guards": {
            "require_hard_gate_clear": True,
            "require_exact_bucket_match": True,
            "require_shadow_or_dry_run_first": True,
            "forbid_main_strategy_size_reuse": True,
            "forbid_addon_topup": True,
            "forbid_position_scale_up": True,
            "forbid_averaging_down": True,
            "isolate_from_main_strategy_pnl": True,
            "write_probe_lineage": True,
        },
        "promotion_requirements": {
            "min_new_shadow_decisions": 50,
            "min_new_settled_counterfactuals": SOFT_HAIRCUT_BUCKET_MIN_SETTLED,
            "require_avg_counterfactual_pnl_per_share_positive": True,
            "require_realized_counterfactual_pnl_sum_positive": True,
            "require_no_active_false_reject_alert": True,
            "require_no_hard_gate_overlap": True,
            "next_mode_after_shadow": "small_live",
        },
        "kill_switches": {
            "unit_pnl_nonpositive_after_min_new_settled": True,
            "hard_gate_reason_appears": True,
            "missing_reason_or_unmapped_reason_appears": True,
            "daily_probe_budget_exhausted": True,
            "exchange_or_order_state_unknown": True,
        },
        "recommended_implementation": {
            "initial_state": "report_only_shadow_design",
            "next_step": "instrument_shadow_probe_ledger_without_live_orders",
            "policy_change_required_now": False,
        },
    }


def _build_soft_haircut_shadow_probe_design(
    bucket_split_review: Mapping[str, Any], *, limit: int = 10
) -> dict[str, Any]:
    candidates = list(bucket_split_review.get("shadow_probe_candidates") or [])
    designs = [_shadow_probe_design_item(candidate) for candidate in candidates]
    if designs:
        next_task = "soft_haircut_shadow_probe_ledger_instrumentation"
        recommended_action = "instrument_shadow_probe_ledger_without_live_orders"
    else:
        next_task = str(bucket_split_review.get("next_mainline_task") or "rule_effectiveness_monitoring")
        recommended_action = "wait_for_shadow_probe_candidate_bucket"
    return {
        "ok": True,
        "scope": "reporting_only_no_policy_change",
        "design_version": "soft_haircut_shadow_probe_v1_2026_05_06",
        "candidate_count": len(candidates),
        "design_count": len(designs),
        "live_orders_enabled": False,
        "tiny_live_candidate_enabled": False,
        "default_max_notional_per_probe_usdc": SHADOW_PROBE_DEFAULT_MAX_NOTIONAL_USDC,
        "default_max_probes_per_et_day": SHADOW_PROBE_DEFAULT_MAX_PROBES_PER_ET_DAY,
        "designs": designs[: max(1, int(limit))],
        "next_mainline_task": next_task,
        "recommended_action": recommended_action,
        "notes": [
            "Shadow probe design is an audit contract only; it does not submit orders.",
            "Tiny-live values are caps for a later small_live step and remain disabled here.",
            "Probe PnL must be attributed separately from the main strategy before any small_live step.",
        ],
    }


def _build_soft_haircut_bucket_sample_accrual(
    bucket_split_review: Mapping[str, Any], *, limit: int = 10
) -> dict[str, Any]:
    rows = [
        dict(item)
        for item in list(bucket_split_review.get("insufficient_exact_bucket_evidence") or [])
        if isinstance(item, Mapping) and int(item.get("active_count") or 0) > 0
    ]
    for item in rows:
        settled = int(item.get("settled_count") or 0)
        item["settled_gap_to_min"] = max(0, SOFT_HAIRCUT_BUCKET_MIN_SETTLED - settled)
        item["recommended_policy_action"] = "collect_more_exact_bucket_counterfactuals"
    rows.sort(
        key=lambda item: (
            -int(item.get("active_count") or 0),
            -int(item.get("pending_count") or 0),
            int(item.get("settled_gap_to_min") or 0),
            str(item.get("rejection_reason") or ""),
            str(item.get("strategy_family") or ""),
        )
    )
    if rows:
        next_task = "soft_haircut_bucket_sample_accrual"
        action = "continue_collecting_exact_bucket_counterfactuals_until_min_settled"
    else:
        next_task = str(bucket_split_review.get("next_mainline_task") or "rule_effectiveness_monitoring")
        action = "no_active_insufficient_exact_bucket"
    return {
        "ok": True,
        "scope": "reporting_only_no_policy_change",
        "min_settled_per_bucket": SOFT_HAIRCUT_BUCKET_MIN_SETTLED,
        "active_insufficient_bucket_count": len(rows),
        "total_settled_gap_to_min": sum(int(item.get("settled_gap_to_min") or 0) for item in rows),
        "rows": rows[: max(1, int(limit))],
        "next_mainline_task": next_task,
        "recommended_action": action,
        "notes": [
            "Sample accrual rows are active exact buckets with fewer than the minimum settled counterfactual samples.",
            "No live policy change is implied while exact buckets are below the minimum settled sample threshold.",
        ],
    }


def _specific_quality_next_task(
    *,
    soft_haircut_policy_review: Mapping[str, Any],
    soft_haircut_bucket_split_review: Mapping[str, Any],
    soft_haircut_bucket_sample_accrual: Mapping[str, Any],
    soft_haircut_shadow_probe_design: Mapping[str, Any],
    soft_haircut_shadow_probe_ledger: Mapping[str, Any],
) -> tuple[str, str] | None:
    ledger_status = str(soft_haircut_shadow_probe_ledger.get("status") or "")
    ledger_task = str(soft_haircut_shadow_probe_ledger.get("next_mainline_task") or "")
    ledger_action = str(soft_haircut_shadow_probe_ledger.get("recommended_action") or "")
    if ledger_status in {"shadow_probe_ledger_quality_blocked", "shadow_probe_ledger_active"}:
        return ledger_task, ledger_action

    design_task = str(soft_haircut_shadow_probe_design.get("next_mainline_task") or "")
    if int(soft_haircut_shadow_probe_design.get("design_count") or 0) > 0 and design_task:
        return design_task, str(soft_haircut_shadow_probe_design.get("recommended_action") or "")

    sample_task = str(soft_haircut_bucket_sample_accrual.get("next_mainline_task") or "")
    if int(soft_haircut_bucket_sample_accrual.get("active_insufficient_bucket_count") or 0) > 0:
        return sample_task, str(soft_haircut_bucket_sample_accrual.get("recommended_action") or "")

    bucket_task = str(soft_haircut_bucket_split_review.get("next_mainline_task") or "")
    if bucket_task and bucket_task != "rule_effectiveness_monitoring":
        return bucket_task, str(soft_haircut_bucket_split_review.get("recommended_action") or "")

    policy_task = str(soft_haircut_policy_review.get("next_mainline_task") or "")
    if policy_task and policy_task != "rule_effectiveness_monitoring":
        return policy_task, str(soft_haircut_policy_review.get("recommended_action") or "")

    return None


def _pnl_size_gap_scope(row: Mapping[str, Any]) -> str:
    gate_class = str(row.get("gate_class") or "")
    if gate_class == "hard_gate":
        return "non_tradeable_hard_gate_na"
    if gate_class == "diagnostic_or_legacy":
        return "reason_specificity_required"
    if gate_class == "needs_mapping":
        return "taxonomy_mapping_required"
    return "actionable_soft_or_tradeable_gap"


def _shadow_probe_design_index(design: Mapping[str, Any]) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
    out: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for item in list(design.get("designs") or []):
        if not isinstance(item, Mapping):
            continue
        bucket = item.get("exact_bucket")
        if not isinstance(bucket, Mapping):
            continue
        key = (
            str(bucket.get("rejection_reason") or ""),
            str(bucket.get("strategy_family") or ""),
            str(bucket.get("side") or "").upper(),
            str(bucket.get("price_bucket") or ""),
            str(bucket.get("notional_bucket") or ""),
        )
        if all(key):
            out[key] = dict(item)
    return out


def _shadow_probe_row(
    ledger_row: Mapping[str, Any],
    design: Mapping[str, Any],
    *,
    now_iso: str,
) -> dict[str, Any] | None:
    price = _to_float(ledger_row.get("order_price"))
    if price is None or price <= 0:
        return None
    budget = design.get("budget") if isinstance(design.get("budget"), Mapping) else {}
    guards = design.get("guards") if isinstance(design.get("guards"), Mapping) else {}
    promo = (
        design.get("promotion_requirements")
        if isinstance(design.get("promotion_requirements"), Mapping)
        else {}
    )
    kill = design.get("kill_switches") if isinstance(design.get("kill_switches"), Mapping) else {}
    evidence = design.get("evidence") if isinstance(design.get("evidence"), Mapping) else {}
    max_notional = _to_float(budget.get("tiny_live_max_notional_per_probe_usdc"))
    if max_notional is None or max_notional <= 0:
        max_notional = SHADOW_PROBE_DEFAULT_MAX_NOTIONAL_USDC
    source_notional = _to_float(ledger_row.get("hypothetical_notional"))
    if source_notional is None or source_notional <= 0:
        source_notional = max_notional
    shadow_notional = min(float(max_notional), float(source_notional))
    shadow_size = shadow_notional / float(price) if price > 0 else None
    pnl_per_share = _to_float(ledger_row.get("counterfactual_pnl_per_share"))
    shadow_pnl = (
        float(pnl_per_share) * float(shadow_size)
        if pnl_per_share is not None and shadow_size is not None
        else None
    )
    probe_id = str(design.get("probe_id") or "")
    probe_version = str(
        (design.get("recommended_implementation") or {}).get("initial_state")
        if isinstance(design.get("recommended_implementation"), Mapping)
        else ""
    ) or "soft_haircut_shadow_probe_v1"
    return {
        "source_decision_id": int(ledger_row.get("source_decision_id") or 0),
        "probe_id": probe_id,
        "probe_version": probe_version,
        "run_id": ledger_row.get("run_id"),
        "run_date": str(ledger_row.get("run_date") or ""),
        "slug": str(ledger_row.get("slug") or ""),
        "event_date": str(ledger_row.get("event_date") or ""),
        "title": str(ledger_row.get("title") or ""),
        "outcome": str(ledger_row.get("outcome") or ""),
        "side": str(ledger_row.get("side") or "").upper(),
        "strategy_family": str(ledger_row.get("strategy_family") or ""),
        "decision_created_at": str(ledger_row.get("decision_created_at") or ""),
        "rejection_reason": str(ledger_row.get("rejection_reason") or ""),
        "price_bucket": _price_bucket(price),
        "notional_bucket": _notional_bucket(ledger_row.get("hypothetical_notional")),
        "shadow_mode": "shadow",
        "live_orders_enabled": 0,
        "live_block_reason": "shadow_probe_report_only_no_live_order",
        "shadow_price": float(price),
        "shadow_size": float(shadow_size) if shadow_size is not None else None,
        "shadow_notional": float(shadow_notional),
        "max_notional_per_probe": float(max_notional),
        "max_probes_per_et_day": int(
            _to_float(budget.get("tiny_live_max_probes_per_et_day"))
            or SHADOW_PROBE_DEFAULT_MAX_PROBES_PER_ET_DAY
        ),
        "daily_notional_cap": _to_float(budget.get("tiny_live_daily_notional_cap_usdc")),
        "weekly_loss_cap": _to_float(budget.get("tiny_live_weekly_loss_cap_usdc")),
        "entry_usable_edge": _to_float(ledger_row.get("entry_usable_edge")),
        "edge_tradeability_profit_value": _to_float(
            ledger_row.get("edge_tradeability_profit_value")
        ),
        "ask": _to_float(ledger_row.get("ask")),
        "bid": _to_float(ledger_row.get("bid")),
        "spread": _to_float(ledger_row.get("spread")),
        "settlement_eligible": int(ledger_row.get("settlement_eligible") or 0),
        "settlement_actual": _to_float(ledger_row.get("settlement_actual")),
        "side_hit": ledger_row.get("side_hit"),
        "subsequent_outcome": str(ledger_row.get("subsequent_outcome") or ""),
        "counterfactual_pnl_per_share": pnl_per_share,
        "shadow_counterfactual_pnl": shadow_pnl,
        "probe_evidence_json": _json_dumps(evidence),
        "probe_guards_json": _json_dumps(guards),
        "promotion_requirements_json": _json_dumps(promo),
        "kill_switches_json": _json_dumps(kill),
        "details_digest": str(ledger_row.get("details_digest") or ""),
        "ledger_updated_at": now_iso,
        "ledger_created_at": now_iso,
    }


def _build_shadow_probe_rows(
    ledger_rows: Sequence[Mapping[str, Any]],
    design: Mapping[str, Any],
    *,
    now_iso: str,
) -> list[dict[str, Any]]:
    design_index = _shadow_probe_design_index(design)
    if not design_index:
        return []
    out: list[dict[str, Any]] = []
    for row in ledger_rows:
        key = (
            str(row.get("rejection_reason") or ""),
            str(row.get("strategy_family") or ""),
            str(row.get("side") or "").upper(),
            _price_bucket(row.get("order_price")),
            _notional_bucket(row.get("hypothetical_notional")),
        )
        design_item = design_index.get(key)
        if not design_item:
            continue
        probe_row = _shadow_probe_row(row, design_item, now_iso=now_iso)
        if probe_row is not None:
            out.append(probe_row)
    return out


def _upsert_shadow_probe_rows(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    columns_sql = ", ".join(SHADOW_PROBE_COLUMNS)
    placeholders = ", ".join("?" for _ in SHADOW_PROBE_COLUMNS)
    update_columns = [
        col for col in SHADOW_PROBE_COLUMNS if col not in {"source_decision_id", "ledger_created_at"}
    ]
    update_sql = ", ".join(f"{col}=excluded.{col}" for col in update_columns)
    update_sql += f", ledger_created_at={SHADOW_PROBE_TABLE}.ledger_created_at"
    sql = f"""
        INSERT INTO {SHADOW_PROBE_TABLE}({columns_sql})
        VALUES ({placeholders})
        ON CONFLICT(source_decision_id) DO UPDATE SET {update_sql}
    """
    count = 0
    for row in rows:
        conn.execute(sql, [row.get(col) for col in SHADOW_PROBE_COLUMNS])
        count += 1
    return count


def _rule_version(details: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "entry_usable_edge_version",
        "entry_edge_constitution_version",
        "entry_edge_constitution_rule",
        "entry_edge_repricing_probability_chain_version",
        "entry_usable_edge_order_guard_version",
        "edge_path_calibration_path",
        "edge_path_calibration_generated_at",
        "edge_realizable_exec_audit_source_path",
        "edge_realizable_exec_audit_source_generated_at",
        "strategy_realized_edge_source_path",
        "strategy_realized_edge_source_generated_at",
    )
    return {key: details.get(key) for key in keys if str(details.get(key) or "").strip()}


def _regime_tag(details: Mapping[str, Any], strategy_family: str) -> str:
    parts = [f"strategy:{strategy_family or 'unknown'}"]
    for label, key in (
        ("edge_mode", "edge_tradeability_mode"),
        ("path", "edge_path_bucket_key"),
        ("realizable", "edge_realizable_bucket_key"),
        ("order_type", "order_type"),
    ):
        value = str(details.get(key) or "").strip()
        if value:
            parts.append(f"{label}:{value}")
    return ";".join(parts)


def _repricing_components(details: Mapping[str, Any]) -> dict[str, Any]:
    source = ecr._parse_repricing_probability_source(  # type: ignore[attr-defined]
        details.get("entry_edge_repricing_exit_evidence_probability_source")
    )
    out: dict[str, Any] = {}
    for component in ecr.REPRICING_EVIDENCE_COMPONENTS:  # type: ignore[attr-defined]
        item = ecr._repricing_component_from_details(  # type: ignore[attr-defined]
            details,
            component,
            source.get(component),
        )
        if item:
            out[component] = item
    return out


def _details_excerpt(details: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "token_id",
        "order_type",
        "status",
        "dry_run",
        "entry_original_reason_codes",
        "entry_hard_reason_codes",
        "entry_soft_reason_codes",
        "entry_soft_no_order",
        "entry_edge_false_edge",
        "entry_edge_false_edge_type",
        "entry_edge_root_cause",
        "entry_edge_exit_plan",
        "entry_edge_repricing_exit_evidence_status",
        "entry_edge_repricing_exit_evidence_probability_source",
        "edge_realizable_p_realize_evidence_status",
        "edge_realizable_p_realize_missing_tables",
        "edge_realizable_p_realize_default_tables",
        "edge_realizable_p_realize_low_sample_tables",
    )
    return {key: details.get(key) for key in keys if details.get(key) not in (None, "")}


def _simulated_fill_quality(details: Mapping[str, Any], order_price: float | None) -> dict[str, Any]:
    out = {
        "p_fill_entry": _to_float(details.get("edge_realizable_p_fill_entry")),
        "p_fill_exit": _to_float(details.get("edge_realizable_p_fill_exit")),
        "spread": _to_float(details.get("spread")),
        "bid": _to_float(details.get("bid") or details.get("best_bid")),
        "ask": _to_float(details.get("ask") or details.get("best_ask")),
        "order_price": order_price,
        "order_type": str(details.get("order_type") or "").strip(),
        "repricing_evidence_status": str(
            details.get("entry_edge_repricing_exit_evidence_status")
            or details.get("edge_realizable_p_realize_evidence_status")
            or ""
        ).strip(),
    }
    return {key: value for key, value in out.items() if value not in (None, "")}


def _counterfactual_pnl(
    *,
    side_hit: bool | None,
    order_price: float | None,
    hypothetical_size: float | None,
) -> tuple[float | None, float | None, str]:
    if side_hit is None or order_price is None:
        return None, None, "settlement_or_price_missing"
    per_share = (1.0 - float(order_price)) if side_hit else -float(order_price)
    if hypothetical_size is None or hypothetical_size <= 0:
        return float(per_share), None, "per_share_only_size_missing"
    return float(per_share), float(per_share) * float(hypothetical_size), "binary_settlement_minus_entry_price"


def _ledger_row(
    row: sqlite3.Row,
    *,
    settlement: Mapping[str, Any] | None,
    fill: Mapping[str, Any] | None,
    now_iso: str,
) -> dict[str, Any] | None:
    details = _safe_json_dict(row["details_json"])
    slug = str(row["slug"] or "").strip()
    event_date = str(row["event_date"] or "").split("T", 1)[0].strip()
    outcome = str(row["outcome"] or "").strip()
    side = str(row["side"] or "").strip().upper()
    if not slug or not event_date or not outcome or side not in {"YES", "NO"}:
        return None
    strategy_family = ecr._strategy_family(details)  # type: ignore[attr-defined]
    reason_codes = _safe_json_list(row["reason_codes_json"])
    soft_reason_codes = _safe_json_list(details.get("entry_soft_reason_codes"))
    price = _order_price(details)
    hypothetical_size = _hypothetical_size(details, price)
    hypothetical_notional = (
        float(price) * float(hypothetical_size)
        if price is not None and hypothetical_size is not None
        else None
    )
    settlement_eligible = ecr._settlement_eligible(event_date)  # type: ignore[attr-defined]
    settlement_actual = _to_float((settlement or {}).get("settlement_actual"))
    side_hit: bool | None = None
    if settlement_actual is not None:
        side_hit = ecr._side_hit(  # type: ignore[attr-defined]
            float(settlement_actual),
            outcome,
            side,
            str((settlement or {}).get("rounding_rule") or ""),
        )
    pnl_per_share, realized_pnl, pnl_source = _counterfactual_pnl(
        side_hit=side_hit,
        order_price=price,
        hypothetical_size=hypothetical_size,
    )
    buy_qty = _to_float((fill or {}).get("buy_qty")) or 0.0
    sell_qty = _to_float((fill or {}).get("sell_qty")) or 0.0
    if side_hit is True:
        subsequent_outcome = "hit"
    elif side_hit is False:
        subsequent_outcome = "miss"
    elif settlement_eligible:
        subsequent_outcome = "settlement_missing"
    else:
        subsequent_outcome = "settlement_pending"
    details_raw = str(row["details_json"] or "")
    return {
        "source_decision_id": int(row["id"] or 0),
        "run_id": int(row["run_id"] or 0) if row["run_id"] is not None else None,
        "run_date": str(row["run_date"] or ""),
        "slug": slug,
        "event_date": event_date,
        "title": str(row["title"] or ""),
        "outcome": outcome,
        "side": side,
        "strategy_family": strategy_family,
        "decision_created_at": str(row["created_at"] or ""),
        "will_order": int(row["will_order"] or 0),
        "decided_state": str(row["decided_state"] or ""),
        "rejection_reason": _primary_rejection_reason(reason_codes, details),
        "reason_codes_json": _json_dumps(reason_codes),
        "soft_reason_codes_json": _json_dumps(soft_reason_codes),
        "entry_usable_edge": _to_float(details.get("entry_usable_edge")),
        "entry_usable_edge_base_source": str(details.get("entry_usable_edge_base_source") or ""),
        "entry_usable_edge_na_reason": str(details.get("entry_usable_edge_na_reason") or ""),
        "entry_usable_edge_size_scale": _to_float(
            details.get("entry_usable_edge_size_scale_applied")
            if details.get("entry_usable_edge_size_scale_applied") is not None
            else details.get("entry_usable_edge_size_scale")
        ),
        "edge_tradeability_pass": _int_bool(details.get("edge_tradeability_pass")),
        "edge_tradeability_blocked": _int_bool(details.get("edge_tradeability_blocked")),
        "edge_tradeability_profit_value": _to_float(details.get("edge_tradeability_profit_value")),
        "edge_tradeability_edge_lcb": _to_float(details.get("edge_tradeability_edge_lcb")),
        "q_entry": _to_float(details.get("q_entry")),
        "q_quote": _to_float(details.get("q_quote")),
        "ask": _to_float(details.get("ask") or details.get("best_ask")),
        "bid": _to_float(details.get("bid") or details.get("best_bid")),
        "spread": _to_float(details.get("spread")),
        "order_price": price,
        "hypothetical_size": hypothetical_size,
        "hypothetical_notional": hypothetical_notional,
        "low_price_bucket": _int_bool(details.get("entry_usable_edge_low_price_bucket")),
        "false_edge_type": str(details.get("entry_edge_false_edge_type") or ""),
        "false_edge_root_cause": str(details.get("entry_edge_root_cause") or ""),
        "repricing_evidence_status": str(
            details.get("entry_edge_repricing_exit_evidence_status")
            or details.get("edge_realizable_p_realize_evidence_status")
            or ""
        ),
        "repricing_p_realize": _to_float(details.get("edge_realizable_p_realize")),
        "repricing_p_converge": _to_float(details.get("edge_realizable_p_converge")),
        "repricing_p_fill_entry": _to_float(details.get("edge_realizable_p_fill_entry")),
        "repricing_p_fill_exit": _to_float(details.get("edge_realizable_p_fill_exit")),
        "repricing_components_json": _json_dumps(_repricing_components(details)),
        "settlement_eligible": 1 if settlement_eligible else 0,
        "settlement_actual": settlement_actual,
        "settlement_rounding_rule": str((settlement or {}).get("rounding_rule") or ""),
        "settlement_recorded_at": str((settlement or {}).get("created_at") or ""),
        "side_hit": None if side_hit is None else (1 if side_hit else 0),
        "subsequent_outcome": subsequent_outcome,
        "buy_filled": 1 if buy_qty > 0 else 0,
        "sell_filled": 1 if sell_qty > 0 else 0,
        "simulated_fill_quality_json": _json_dumps(_simulated_fill_quality(details, price)),
        "counterfactual_pnl_per_share": pnl_per_share,
        "realized_counterfactual_pnl": realized_pnl,
        "counterfactual_pnl_source": pnl_source,
        "regime_tag": _regime_tag(details, strategy_family),
        "rule_version_json": _json_dumps(_rule_version(details)),
        "details_digest": hashlib.sha256(details_raw.encode("utf-8")).hexdigest(),
        "details_excerpt_json": _json_dumps(_details_excerpt(details)),
        "ledger_updated_at": now_iso,
        "ledger_created_at": now_iso,
    }


def _upsert_rows(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    columns_sql = ", ".join(LEDGER_COLUMNS)
    placeholders = ", ".join("?" for _ in LEDGER_COLUMNS)
    update_columns = [col for col in LEDGER_COLUMNS if col not in {"source_decision_id", "ledger_created_at"}]
    update_sql = ", ".join(f"{col}=excluded.{col}" for col in update_columns)
    update_sql += f", ledger_created_at={LEDGER_TABLE}.ledger_created_at"
    sql = f"""
        INSERT INTO {LEDGER_TABLE}({columns_sql})
        VALUES ({placeholders})
        ON CONFLICT(source_decision_id) DO UPDATE SET {update_sql}
    """
    count = 0
    for row in rows:
        conn.execute(sql, [row.get(col) for col in LEDGER_COLUMNS])
        count += 1
    return count


def _top_counts(rows: Iterable[Mapping[str, Any]], key: str, limit: int = 8) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        text = str(row.get(key) or "").strip() or "unknown"
        counts[text] = counts.get(text, 0) + 1
    return [
        {"key": item_key, "count": count}
        for item_key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    ]


def _ledger_quality(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, LEDGER_TABLE):
        return {"ok": False, "error": "reject_ledger_table_missing"}
    metrics = {
        "total_rows": f"SELECT COUNT(*) FROM {LEDGER_TABLE}",
        "distinct_source_decision_id": f"SELECT COUNT(DISTINCT source_decision_id) FROM {LEDGER_TABLE}",
        "null_source_decision_id": f"SELECT COUNT(*) FROM {LEDGER_TABLE} WHERE source_decision_id IS NULL",
        "duplicate_source_decision_id": f"""
            SELECT COUNT(*) FROM (
                SELECT source_decision_id FROM {LEDGER_TABLE}
                GROUP BY source_decision_id HAVING COUNT(*) > 1
            )
        """,
        "missing_rejection_reason": f"""
            SELECT COUNT(*) FROM {LEDGER_TABLE}
            WHERE COALESCE(TRIM(rejection_reason), '') = ''
        """,
        "missing_strategy_family": f"""
            SELECT COUNT(*) FROM {LEDGER_TABLE}
            WHERE COALESCE(TRIM(strategy_family), '') = ''
        """,
        "settlement_eligible_missing_actual": f"""
            SELECT COUNT(*) FROM {LEDGER_TABLE}
            WHERE settlement_eligible = 1 AND settlement_actual IS NULL
        """,
        "settlement_actual_missing_side_hit": f"""
            SELECT COUNT(*) FROM {LEDGER_TABLE}
            WHERE settlement_actual IS NOT NULL AND side_hit IS NULL
        """,
        "side_hit_with_pnl_per_share": f"""
            SELECT COUNT(*) FROM {LEDGER_TABLE}
            WHERE side_hit IS NOT NULL AND counterfactual_pnl_per_share IS NOT NULL
        """,
        "side_hit_with_realized_pnl": f"""
            SELECT COUNT(*) FROM {LEDGER_TABLE}
            WHERE side_hit IS NOT NULL AND realized_counterfactual_pnl IS NOT NULL
        """,
        "side_hit_size_missing_pnl": f"""
            SELECT COUNT(*) FROM {LEDGER_TABLE}
            WHERE side_hit IS NOT NULL
              AND hypothetical_size IS NULL
              AND realized_counterfactual_pnl IS NULL
        """,
    }
    out: dict[str, Any] = {}
    for key, sql in metrics.items():
        out[key] = int(conn.execute(sql).fetchone()[0] or 0)
    total = int(out.get("total_rows") or 0)
    side_hit_rows = int(
        conn.execute(f"SELECT COUNT(*) FROM {LEDGER_TABLE} WHERE side_hit IS NOT NULL").fetchone()[0]
        or 0
    )
    out["side_hit_rows"] = side_hit_rows
    out["realized_pnl_coverage_rate"] = (
        float(out["side_hit_with_realized_pnl"]) / float(side_hit_rows)
        if side_hit_rows
        else None
    )
    blockers = []
    if out["null_source_decision_id"]:
        blockers.append("null_source_decision_id")
    if out["duplicate_source_decision_id"]:
        blockers.append("duplicate_source_decision_id")
    if out["missing_rejection_reason"]:
        blockers.append("missing_rejection_reason")
    if out["settlement_actual_missing_side_hit"]:
        blockers.append("settlement_actual_missing_side_hit")
    if out["settlement_eligible_missing_actual"]:
        blockers.append("settlement_eligible_missing_actual")
    warnings = []
    if side_hit_rows and out["side_hit_size_missing_pnl"]:
        warnings.append("historical_hypothetical_size_missing_for_some_settled_rejects")
    out["ok"] = not blockers
    out["blockers"] = blockers
    out["warnings"] = warnings
    out["table_rows_match_distinct_source"] = bool(
        total == int(out.get("distinct_source_decision_id") or 0)
    )
    return out


def _shadow_probe_quality(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, SHADOW_PROBE_TABLE):
        return {"ok": False, "error": "shadow_probe_table_missing"}
    metrics = {
        "total_rows": f"SELECT COUNT(*) FROM {SHADOW_PROBE_TABLE}",
        "distinct_source_decision_id": f"SELECT COUNT(DISTINCT source_decision_id) FROM {SHADOW_PROBE_TABLE}",
        "duplicate_source_decision_id": f"""
            SELECT COUNT(*) FROM (
                SELECT source_decision_id FROM {SHADOW_PROBE_TABLE}
                GROUP BY source_decision_id HAVING COUNT(*) > 1
            )
        """,
        "missing_probe_id": f"""
            SELECT COUNT(*) FROM {SHADOW_PROBE_TABLE}
            WHERE COALESCE(TRIM(probe_id), '') = ''
        """,
        "live_orders_enabled_rows": f"""
            SELECT COUNT(*) FROM {SHADOW_PROBE_TABLE}
            WHERE COALESCE(live_orders_enabled, 0) != 0
        """,
        "missing_live_block_reason": f"""
            SELECT COUNT(*) FROM {SHADOW_PROBE_TABLE}
            WHERE COALESCE(TRIM(live_block_reason), '') = ''
        """,
        "missing_shadow_notional": f"""
            SELECT COUNT(*) FROM {SHADOW_PROBE_TABLE}
            WHERE shadow_notional IS NULL OR shadow_notional <= 0
        """,
        "settlement_actual_missing_side_hit": f"""
            SELECT COUNT(*) FROM {SHADOW_PROBE_TABLE}
            WHERE settlement_actual IS NOT NULL AND side_hit IS NULL
        """,
        "current_run_id": f"SELECT MAX(run_id) FROM {SHADOW_PROBE_TABLE} WHERE run_id IS NOT NULL",
    }
    out: dict[str, Any] = {}
    for key, sql in metrics.items():
        value = conn.execute(sql).fetchone()[0]
        out[key] = int(value or 0) if key != "current_run_id" else (int(value or 0) if value else 0)
    current_run_id = int(out.get("current_run_id") or 0)
    if current_run_id > 0:
        out["current_run_rows"] = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {SHADOW_PROBE_TABLE} WHERE run_id = ?",
                (current_run_id,),
            ).fetchone()[0]
            or 0
        )
    else:
        out["current_run_rows"] = 0
    blockers = []
    for key in (
        "duplicate_source_decision_id",
        "missing_probe_id",
        "live_orders_enabled_rows",
        "missing_live_block_reason",
        "missing_shadow_notional",
        "settlement_actual_missing_side_hit",
    ):
        if int(out.get(key) or 0) > 0:
            blockers.append(key)
    out["ok"] = not blockers
    out["blockers"] = blockers
    out["table_rows_match_distinct_source"] = bool(
        int(out.get("total_rows") or 0) == int(out.get("distinct_source_decision_id") or 0)
    )
    out["top_probe_ids"] = [
        {"key": str(row[0] or "unknown"), "count": int(row[1] or 0)}
        for row in conn.execute(
            f"""
            SELECT probe_id, COUNT(*) AS count
            FROM {SHADOW_PROBE_TABLE}
            GROUP BY probe_id
            ORDER BY count DESC, probe_id ASC
            LIMIT 10
            """
        ).fetchall()
    ]
    out["top_subsequent_outcomes"] = [
        {"key": str(row[0] or "unknown"), "count": int(row[1] or 0)}
        for row in conn.execute(
            f"""
            SELECT subsequent_outcome, COUNT(*) AS count
            FROM {SHADOW_PROBE_TABLE}
            GROUP BY subsequent_outcome
            ORDER BY count DESC, subsequent_outcome ASC
            LIMIT 10
            """
        ).fetchall()
    ]
    return out


def _requirement_int(requirements: Mapping[str, Any], key: str, default: int) -> int:
    value = _to_float(requirements.get(key))
    if value is None or value <= 0:
        return int(default)
    return int(value)


def _shadow_probe_outcome_status(
    item: Mapping[str, Any], *, active_false_reject_count: int, active_hard_gate_reason_count: int
) -> dict[str, Any]:
    requirements = (
        item.get("promotion_requirements")
        if isinstance(item.get("promotion_requirements"), Mapping)
        else {}
    )
    min_decisions = _requirement_int(requirements, "min_new_shadow_decisions", 50)
    min_settled = _requirement_int(
        requirements,
        "min_new_settled_counterfactuals",
        SOFT_HAIRCUT_BUCKET_MIN_SETTLED,
    )
    new_decisions = int(item.get("new_shadow_decision_count") or 0)
    new_settled = int(item.get("new_settled_counterfactual_count") or 0)
    avg_unit_pnl = _to_float(item.get("new_avg_counterfactual_pnl_per_share"))
    shadow_pnl_sum = _to_float(item.get("new_shadow_counterfactual_pnl_sum"))
    blockers: list[str] = []
    if new_decisions < min_decisions:
        blockers.append("new_shadow_decisions_below_min")
    if new_settled < min_settled:
        blockers.append("new_settled_counterfactuals_below_min")
    if new_settled >= min_settled:
        if bool(requirements.get("require_avg_counterfactual_pnl_per_share_positive")) and (
            avg_unit_pnl is None or avg_unit_pnl <= 0
        ):
            blockers.append("new_avg_counterfactual_pnl_per_share_nonpositive")
        if bool(requirements.get("require_realized_counterfactual_pnl_sum_positive")) and (
            shadow_pnl_sum is None or shadow_pnl_sum <= 0
        ):
            blockers.append("new_shadow_counterfactual_pnl_sum_nonpositive")
    if bool(requirements.get("require_no_active_false_reject_alert")) and int(active_false_reject_count) > 0:
        blockers.append("active_false_reject_alert")
    if bool(requirements.get("require_no_hard_gate_overlap")) and int(active_hard_gate_reason_count) > 0:
        blockers.append("active_hard_gate_reason_present")

    if not blockers:
        status = "promotion_ready"
        next_task = "soft_haircut_shadow_probe_promotion_review"
        action = "review_shadow_probe_for_small_live_promotion_without_main_strategy_reuse"
    elif any(blocker.endswith("nonpositive") for blocker in blockers):
        status = "kill_switch_review_required"
        next_task = "soft_haircut_shadow_probe_kill_switch_review"
        action = "keep_probe_disabled_and_review_negative_post_instrument_outcomes"
    elif new_decisions <= 0:
        status = "waiting_for_post_instrument_shadow_decisions"
        next_task = "soft_haircut_shadow_probe_post_instrument_sample_accrual"
        action = "continue_collecting_new_exact_bucket_shadow_decisions"
    elif new_settled < min_settled:
        status = "waiting_for_post_instrument_settlements"
        next_task = "soft_haircut_shadow_probe_outcome_monitoring"
        action = "continue_monitoring_shadow_probe_until_new_counterfactuals_settle"
    else:
        status = "promotion_blocked_by_external_guard"
        next_task = "soft_haircut_shadow_probe_guard_review"
        action = "resolve_shadow_probe_external_guards_before_promotion_review"
    return {
        "status": status,
        "next_mainline_task": next_task,
        "recommended_action": action,
        "promotion_blockers": blockers,
        "required_new_shadow_decisions": min_decisions,
        "required_new_settled_counterfactuals": min_settled,
    }


def _build_shadow_probe_outcome_monitor(
    conn: sqlite3.Connection,
    *,
    active_false_reject_count: int = 0,
    active_hard_gate_reason_count: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    if not _table_exists(conn, SHADOW_PROBE_TABLE):
        return {
            "ok": False,
            "status": "shadow_probe_table_missing",
            "next_mainline_task": "soft_haircut_shadow_probe_ledger_instrumentation",
            "recommended_action": "materialize_shadow_probe_ledger",
        }
    rows = conn.execute(
        f"""
        WITH per_probe AS (
            SELECT
              probe_id,
              MIN(ledger_created_at) AS instrumented_at,
              MAX(ledger_updated_at) AS latest_updated_at,
              MAX(decision_created_at) AS latest_decision_at,
              MAX(run_id) AS current_run_id,
              COUNT(*) AS total_rows,
              SUM(CASE WHEN side_hit IS NOT NULL THEN 1 ELSE 0 END) AS settled_count,
              SUM(CASE WHEN side_hit = 1 THEN 1 ELSE 0 END) AS hit_count,
              SUM(CASE WHEN subsequent_outcome = 'settlement_pending' THEN 1 ELSE 0 END) AS pending_count,
              AVG(counterfactual_pnl_per_share) AS avg_counterfactual_pnl_per_share,
              SUM(shadow_counterfactual_pnl) AS shadow_counterfactual_pnl_sum,
              MAX(promotion_requirements_json) AS promotion_requirements_json,
              MAX(kill_switches_json) AS kill_switches_json
            FROM {SHADOW_PROBE_TABLE}
            GROUP BY probe_id
        ), joined AS (
            SELECT
              p.probe_id,
              SUM(CASE WHEN julianday(s.decision_created_at) > julianday(p.instrumented_at) THEN 1 ELSE 0 END)
                AS new_shadow_decision_count,
              SUM(CASE WHEN julianday(s.decision_created_at) > julianday(p.instrumented_at)
                       AND s.side_hit IS NOT NULL THEN 1 ELSE 0 END)
                AS new_settled_counterfactual_count,
              SUM(CASE WHEN julianday(s.decision_created_at) > julianday(p.instrumented_at)
                       AND s.side_hit = 1 THEN 1 ELSE 0 END)
                AS new_hit_count,
              SUM(CASE WHEN julianday(s.decision_created_at) > julianday(p.instrumented_at)
                       AND s.subsequent_outcome = 'settlement_pending' THEN 1 ELSE 0 END)
                AS new_pending_count,
              AVG(CASE WHEN julianday(s.decision_created_at) > julianday(p.instrumented_at)
                       THEN s.counterfactual_pnl_per_share ELSE NULL END)
                AS new_avg_counterfactual_pnl_per_share,
              SUM(CASE WHEN julianday(s.decision_created_at) > julianday(p.instrumented_at)
                       THEN s.shadow_counterfactual_pnl ELSE NULL END)
                AS new_shadow_counterfactual_pnl_sum
            FROM per_probe p
            JOIN {SHADOW_PROBE_TABLE} s ON s.probe_id = p.probe_id
            GROUP BY p.probe_id
        )
        SELECT p.*, j.new_shadow_decision_count, j.new_settled_counterfactual_count,
               j.new_hit_count, j.new_pending_count, j.new_avg_counterfactual_pnl_per_share,
               j.new_shadow_counterfactual_pnl_sum
        FROM per_probe p
        JOIN joined j USING(probe_id)
        ORDER BY j.new_shadow_decision_count DESC, p.total_rows DESC, p.probe_id ASC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    items: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for row in rows:
        requirements = _safe_json_dict(row["promotion_requirements_json"])
        new_settled = int(row["new_settled_counterfactual_count"] or 0)
        new_hit = int(row["new_hit_count"] or 0)
        settled = int(row["settled_count"] or 0)
        hit = int(row["hit_count"] or 0)
        item: dict[str, Any] = {
            "probe_id": str(row["probe_id"] or ""),
            "instrumented_at": str(row["instrumented_at"] or ""),
            "latest_updated_at": str(row["latest_updated_at"] or ""),
            "latest_decision_at": str(row["latest_decision_at"] or ""),
            "current_run_id": int(row["current_run_id"] or 0),
            "total_rows": int(row["total_rows"] or 0),
            "settled_count": settled,
            "hit_count": hit,
            "hit_rate": _rate(hit, settled),
            "pending_count": int(row["pending_count"] or 0),
            "avg_counterfactual_pnl_per_share": _to_float(row["avg_counterfactual_pnl_per_share"]),
            "shadow_counterfactual_pnl_sum": _to_float(row["shadow_counterfactual_pnl_sum"]),
            "new_shadow_decision_count": int(row["new_shadow_decision_count"] or 0),
            "new_settled_counterfactual_count": new_settled,
            "new_hit_count": new_hit,
            "new_hit_rate": _rate(new_hit, new_settled),
            "new_pending_count": int(row["new_pending_count"] or 0),
            "new_avg_counterfactual_pnl_per_share": _to_float(
                row["new_avg_counterfactual_pnl_per_share"]
            ),
            "new_shadow_counterfactual_pnl_sum": _to_float(
                row["new_shadow_counterfactual_pnl_sum"]
            ),
            "promotion_requirements": requirements,
            "kill_switches": _safe_json_dict(row["kill_switches_json"]),
            "external_guard_counts": {
                "active_false_reject_count": int(active_false_reject_count),
                "active_hard_gate_reason_count": int(active_hard_gate_reason_count),
            },
        }
        item.update(
            _shadow_probe_outcome_status(
                item,
                active_false_reject_count=int(active_false_reject_count),
                active_hard_gate_reason_count=int(active_hard_gate_reason_count),
            )
        )
        status_counts[str(item["status"])] = status_counts.get(str(item["status"]), 0) + 1
        items.append(item)

    if not items:
        status = "shadow_probe_outcome_empty"
        next_task = "soft_haircut_shadow_probe_candidate_accrual"
        action = "wait_for_exact_bucket_matches"
    elif any(str(item.get("status")) == "kill_switch_review_required" for item in items):
        status = "kill_switch_review_required"
        next_task = "soft_haircut_shadow_probe_kill_switch_review"
        action = "keep_probe_disabled_and_review_negative_post_instrument_outcomes"
    elif any(str(item.get("status")) == "promotion_ready" for item in items):
        status = "promotion_ready"
        next_task = "soft_haircut_shadow_probe_promotion_review"
        action = "review_shadow_probe_for_small_live_promotion_without_main_strategy_reuse"
    elif any(
        str(item.get("status")) == "waiting_for_post_instrument_shadow_decisions"
        for item in items
    ):
        status = "waiting_for_post_instrument_shadow_decisions"
        next_task = "soft_haircut_shadow_probe_post_instrument_sample_accrual"
        action = "continue_collecting_new_exact_bucket_shadow_decisions"
    elif any(str(item.get("status")) == "waiting_for_post_instrument_settlements" for item in items):
        status = "waiting_for_post_instrument_settlements"
        next_task = "soft_haircut_shadow_probe_outcome_monitoring"
        action = "continue_monitoring_shadow_probe_until_new_counterfactuals_settle"
    else:
        status = "promotion_blocked_by_external_guard"
        next_task = "soft_haircut_shadow_probe_guard_review"
        action = "resolve_shadow_probe_external_guards_before_promotion_review"
    return {
        "ok": True,
        "status": status,
        "scope": "reporting_only_no_policy_change",
        "probe_count": len(items),
        "status_counts": [
            {"key": key, "count": count}
            for key, count in sorted(status_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "rows": items,
        "next_mainline_task": next_task,
        "recommended_action": action,
        "notes": [
            "Promotion checks use post-instrument decision_created_at, so historical backfill cannot promote a probe by itself.",
            "This monitor is report-only and keeps live_orders_enabled disabled.",
        ],
    }


def _build_shadow_probe_ledger_review(
    conn: sqlite3.Connection,
    *,
    active_false_reject_count: int = 0,
    active_hard_gate_reason_count: int = 0,
) -> dict[str, Any]:
    if not _table_exists(conn, SHADOW_PROBE_TABLE):
        return {
            "ok": False,
            "status": "shadow_probe_table_missing",
            "table": SHADOW_PROBE_TABLE,
            "next_mainline_task": "soft_haircut_shadow_probe_ledger_instrumentation",
            "recommended_action": "materialize_shadow_probe_ledger",
        }
    quality = _shadow_probe_quality(conn)
    total_rows = int(quality.get("total_rows") or 0)
    current_run_rows = int(quality.get("current_run_rows") or 0)
    outcome_monitor = _build_shadow_probe_outcome_monitor(
        conn,
        active_false_reject_count=int(active_false_reject_count),
        active_hard_gate_reason_count=int(active_hard_gate_reason_count),
    )
    if not bool(quality.get("ok")):
        status = "shadow_probe_ledger_quality_blocked"
        next_task = "soft_haircut_shadow_probe_ledger_quality_fix"
        action = "fix_shadow_probe_ledger_quality_blockers"
    elif total_rows <= 0:
        status = "shadow_probe_ledger_empty"
        next_task = "soft_haircut_shadow_probe_candidate_accrual"
        action = "wait_for_exact_bucket_matches"
    elif current_run_rows <= 0:
        status = "shadow_probe_ledger_no_current_run_match"
        next_task = "soft_haircut_shadow_probe_candidate_accrual"
        action = "wait_for_current_run_exact_bucket_matches"
    else:
        status = "shadow_probe_ledger_active"
        next_task = str(
            outcome_monitor.get("next_mainline_task")
            or "soft_haircut_shadow_probe_outcome_monitoring"
        )
        action = str(
            outcome_monitor.get("recommended_action")
            or "monitor_shadow_probe_outcomes_until_promotion_requirements_are_met"
        )
    return {
        "ok": bool(quality.get("ok")),
        "status": status,
        "table": SHADOW_PROBE_TABLE,
        "quality": quality,
        "outcome_monitoring": outcome_monitor,
        "current_run_rows": current_run_rows,
        "total_rows": total_rows,
        "next_mainline_task": next_task,
        "recommended_action": action,
        "notes": [
            "Shadow probe ledger is derived from reject_ledger_v1 and remains live-order disabled.",
            "Current-run matches indicate the exact bucket is actively being observed.",
        ],
    }


def _rate(num: int, den: int) -> float | None:
    return (int(num) / int(den)) if int(den) else None


def _review_bucket(row: Mapping[str, Any]) -> str:
    active_count = int(row.get("active_count") or 0)
    settled_count = int(row.get("settled_count") or 0)
    hit_rate = _to_float(row.get("hit_rate"))
    avg_pnl = _to_float(row.get("avg_counterfactual_pnl_per_share"))
    if settled_count < QUALITY_REVIEW_MIN_SETTLED:
        return "active_insufficient_settled_sample" if active_count > 0 else "historical_insufficient_settled_sample"
    false_reject_like = bool(
        hit_rate is not None
        and hit_rate >= QUALITY_REVIEW_FALSE_REJECT_HIT_RATE
        and (avg_pnl is None or avg_pnl > 0)
    )
    protective_like = bool(
        (hit_rate is not None and hit_rate <= QUALITY_REVIEW_PROTECTIVE_HIT_RATE)
        or (avg_pnl is not None and avg_pnl <= 0)
    )
    if active_count > 0 and false_reject_like:
        return "active_false_reject_review"
    if active_count > 0 and protective_like:
        return "active_protective"
    if active_count > 0:
        return "active_monitor"
    if false_reject_like:
        return "historical_false_reject_residual"
    if protective_like:
        return "historical_protective_residual"
    return "historical_monitor"


def _table_count(conn: sqlite3.Connection, table_name: str) -> int:
    if not _table_exists(conn, table_name):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0] or 0)


def _current_active_reason_counts(conn: sqlite3.Connection, current_run_id: int) -> dict[str, int]:
    if not _table_exists(conn, "auto_trade_decisions") or int(current_run_id or 0) <= 0:
        return {}
    out: dict[str, int] = {}
    try:
        rows = conn.execute(
            """
            SELECT reason_codes_json, details_json
            FROM auto_trade_decisions
            WHERE action = 'entry'
              AND COALESCE(will_order, 0) = 0
              AND run_id = ?
            """,
            (int(current_run_id),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    for row in rows:
        details = _safe_json_dict(row["details_json"])
        reason = _primary_rejection_reason(_safe_json_list(row["reason_codes_json"]), details)
        out[reason] = out.get(reason, 0) + 1
    return out


def _strategy_top_by_reason(conn: sqlite3.Connection, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    try:
        rows = conn.execute(
            f"""
            WITH counts AS (
                SELECT
                  rejection_reason,
                  COALESCE(NULLIF(strategy_family, ''), 'unknown') AS strategy_family,
                  COUNT(*) AS count
                FROM {LEDGER_TABLE}
                GROUP BY rejection_reason, COALESCE(NULLIF(strategy_family, ''), 'unknown')
            ), ranked AS (
                SELECT
                  rejection_reason,
                  strategy_family,
                  count,
                  ROW_NUMBER() OVER (
                    PARTITION BY rejection_reason
                    ORDER BY count DESC, strategy_family ASC
                  ) AS rn
                FROM counts
            )
            SELECT rejection_reason, strategy_family, count
            FROM ranked
            WHERE rn <= ?
            ORDER BY rejection_reason ASC, count DESC, strategy_family ASC
            """,
            (int(limit),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        reason = str(row["rejection_reason"] or "unknown")
        out.setdefault(reason, []).append(
            {"key": str(row["strategy_family"] or "unknown"), "count": int(row["count"] or 0)}
        )
    return out


def build_quality_review(conn: sqlite3.Connection, *, limit: int = 40) -> dict[str, Any]:
    if not _table_exists(conn, LEDGER_TABLE):
        return {
            "ok": False,
            "error": "reject_ledger_table_missing",
            "next_mainline_task": "reject_ledger_v1_materialization",
        }
    total_rows = _table_count(conn, LEDGER_TABLE)
    latest_run_row = conn.execute(
        f"SELECT MAX(run_id) FROM {LEDGER_TABLE} WHERE run_id IS NOT NULL"
    ).fetchone()
    current_run_id = int(latest_run_row[0] or 0) if latest_run_row else 0
    active_counts = _current_active_reason_counts(conn, current_run_id)
    strategy_top_by_reason = _strategy_top_by_reason(conn)
    reason_rows = conn.execute(
        f"""
        SELECT
          rejection_reason,
          COUNT(*) AS total_count,
          SUM(CASE WHEN side_hit IS NOT NULL THEN 1 ELSE 0 END) AS settled_count,
          SUM(CASE WHEN side_hit = 1 THEN 1 ELSE 0 END) AS hit_count,
          SUM(CASE WHEN side_hit = 0 THEN 1 ELSE 0 END) AS miss_count,
          SUM(CASE WHEN subsequent_outcome = 'settlement_pending' THEN 1 ELSE 0 END) AS pending_count,
          SUM(CASE WHEN side_hit IS NOT NULL AND hypothetical_size IS NULL THEN 1 ELSE 0 END) AS size_missing_pnl_count,
          AVG(counterfactual_pnl_per_share) AS avg_counterfactual_pnl_per_share,
          SUM(realized_counterfactual_pnl) AS realized_counterfactual_pnl_sum,
          COUNT(realized_counterfactual_pnl) AS realized_counterfactual_pnl_count,
          AVG(order_price) AS avg_order_price,
          AVG(ask) AS avg_ask,
          AVG(bid) AS avg_bid,
          AVG(spread) AS avg_spread,
          AVG(entry_usable_edge) AS avg_entry_usable_edge,
          AVG(edge_tradeability_profit_value) AS avg_edge_tradeability_profit_value,
          AVG(edge_tradeability_edge_lcb) AS avg_edge_tradeability_edge_lcb,
          MAX(decision_created_at) AS latest_decision_at
        FROM {LEDGER_TABLE}
        GROUP BY rejection_reason
        """
    ).fetchall()
    rows: list[dict[str, Any]] = []
    for raw in reason_rows:
        reason = str(raw["rejection_reason"] or "unknown")
        settled_count = int(raw["settled_count"] or 0)
        hit_count = int(raw["hit_count"] or 0)
        size_missing = int(raw["size_missing_pnl_count"] or 0)
        item: dict[str, Any] = {
            "rejection_reason": reason,
            "total_count": int(raw["total_count"] or 0),
            "active_count": int(active_counts.get(reason, 0)),
            "settled_count": settled_count,
            "hit_count": hit_count,
            "miss_count": int(raw["miss_count"] or 0),
            "pending_count": int(raw["pending_count"] or 0),
            "hit_rate": _rate(hit_count, settled_count),
            "size_missing_pnl_count": size_missing,
            "size_missing_pnl_rate": _rate(size_missing, settled_count),
            "avg_counterfactual_pnl_per_share": _to_float(raw["avg_counterfactual_pnl_per_share"]),
            "realized_counterfactual_pnl_sum": _to_float(raw["realized_counterfactual_pnl_sum"]),
            "realized_counterfactual_pnl_count": int(raw["realized_counterfactual_pnl_count"] or 0),
            "avg_order_price": _to_float(raw["avg_order_price"]),
            "avg_ask": _to_float(raw["avg_ask"]),
            "avg_bid": _to_float(raw["avg_bid"]),
            "avg_spread": _to_float(raw["avg_spread"]),
            "avg_entry_usable_edge": _to_float(raw["avg_entry_usable_edge"]),
            "avg_edge_tradeability_profit_value": _to_float(
                raw["avg_edge_tradeability_profit_value"]
            ),
            "avg_edge_tradeability_edge_lcb": _to_float(raw["avg_edge_tradeability_edge_lcb"]),
            "latest_decision_at": str(raw["latest_decision_at"] or ""),
            "strategy_family_top": strategy_top_by_reason.get(reason, []),
        }
        item.update(_rule_taxonomy(reason))
        item["review_bucket"] = _review_bucket(item)
        rows.append(item)
    rows.sort(
        key=lambda row: (
            0 if row["review_bucket"] == "active_false_reject_review" else 1,
            0 if int(row.get("active_count") or 0) > 0 else 1,
            -int(row.get("active_count") or 0),
            -int(row.get("settled_count") or 0),
            -float(row.get("hit_rate") or 0.0),
            str(row.get("rejection_reason") or ""),
        )
    )
    bucket_counts: dict[str, int] = {}
    for row in rows:
        bucket = str(row.get("review_bucket") or "unknown")
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    hard_soft_taxonomy = _build_hard_soft_taxonomy(rows, limit=limit)
    rule_pruning_review = _build_rule_pruning_review(rows, limit=limit)
    soft_haircut_policy_review = _build_soft_haircut_policy_review(rows, limit=limit)
    bucket_split_reasons = [
        str(item.get("rejection_reason") or "")
        for item in soft_haircut_policy_review.get("bucket_split_before_haircut", [])
        if int(item.get("active_count") or 0) > 0
    ]
    soft_haircut_bucket_split_review = _build_soft_haircut_bucket_split_review(
        conn,
        reasons=bucket_split_reasons,
        current_run_id=current_run_id,
        limit=limit,
    )
    soft_haircut_shadow_probe_design = _build_soft_haircut_shadow_probe_design(
        soft_haircut_bucket_split_review,
        limit=limit,
    )
    soft_haircut_bucket_sample_accrual = _build_soft_haircut_bucket_sample_accrual(
        soft_haircut_bucket_split_review,
        limit=limit,
    )
    active_false = [row for row in rows if row["review_bucket"] == "active_false_reject_review"]
    active_protective = [row for row in rows if row["review_bucket"] == "active_protective"]
    active_monitor = [row for row in rows if row["review_bucket"] == "active_monitor"]
    active_insufficient = [row for row in rows if row["review_bucket"] == "active_insufficient_settled_sample"]
    active_hard_gate_reason_count = len(
        [
            row
            for row in rows
            if str(row.get("gate_class") or "") == "hard_gate"
            and int(row.get("active_count") or 0) > 0
        ]
    )
    soft_haircut_shadow_probe_ledger = _build_shadow_probe_ledger_review(
        conn,
        active_false_reject_count=len(active_false),
        active_hard_gate_reason_count=active_hard_gate_reason_count,
    )
    historical_false = [row for row in rows if row["review_bucket"] == "historical_false_reject_residual"]
    size_gaps = [
        row
        for row in rows
        if int(row.get("size_missing_pnl_count") or 0) > 0
        and int(row.get("settled_count") or 0) >= QUALITY_REVIEW_MIN_SETTLED
    ]
    for row in size_gaps:
        row["pnl_size_gap_scope"] = _pnl_size_gap_scope(row)
    active_size_gaps = [
        row
        for row in size_gaps
        if int(row.get("active_count") or 0) > 0
        and str(row.get("pnl_size_gap_scope") or "") == "actionable_soft_or_tradeable_gap"
    ]
    active_non_actionable_size_gaps = [
        row
        for row in size_gaps
        if int(row.get("active_count") or 0) > 0
        and str(row.get("pnl_size_gap_scope") or "") != "actionable_soft_or_tradeable_gap"
    ]
    historical_size_gaps = [row for row in size_gaps if int(row.get("active_count") or 0) <= 0]
    size_gaps.sort(
        key=lambda row: (
            -float(row.get("size_missing_pnl_rate") or 0.0),
            -int(row.get("size_missing_pnl_count") or 0),
            str(row.get("rejection_reason") or ""),
        )
    )
    if active_false:
        next_task = "active_reject_rule_review"
        recommended_action = "review_active_reject_reasons_with_positive_counterfactual_evidence"
    elif active_insufficient:
        next_task = "reject_ledger_active_sample_accrual"
        recommended_action = "continue_collecting_settled_samples_for_active_reject_reasons"
    elif active_size_gaps:
        next_task = "reject_ledger_size_coverage_roll_forward"
        recommended_action = "fix_active_reject_ledger_hypothetical_size_coverage"
    else:
        specific_task = _specific_quality_next_task(
            soft_haircut_policy_review=soft_haircut_policy_review,
            soft_haircut_bucket_split_review=soft_haircut_bucket_split_review,
            soft_haircut_bucket_sample_accrual=soft_haircut_bucket_sample_accrual,
            soft_haircut_shadow_probe_design=soft_haircut_shadow_probe_design,
            soft_haircut_shadow_probe_ledger=soft_haircut_shadow_probe_ledger,
        )
        next_task = specific_task[0] if specific_task else "rule_effectiveness_monitoring"
        recommended_action = specific_task[1] if specific_task else (
            "continue_monitoring_reject_rule_effectiveness_with_historical_size_gap_note"
            if historical_size_gaps or active_non_actionable_size_gaps
            else "continue_monitoring_reject_rule_effectiveness"
        )
    return {
        "ok": True,
        "table": LEDGER_TABLE,
        "total_rows": total_rows,
        "current_run_id": current_run_id,
        "min_settled_for_review": QUALITY_REVIEW_MIN_SETTLED,
        "false_reject_hit_rate_threshold": QUALITY_REVIEW_FALSE_REJECT_HIT_RATE,
        "protective_hit_rate_threshold": QUALITY_REVIEW_PROTECTIVE_HIT_RATE,
        "bucket_counts": [
            {"key": key, "count": count}
            for key, count in sorted(bucket_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "hard_soft_taxonomy": hard_soft_taxonomy,
        "rule_pruning_review": rule_pruning_review,
        "soft_haircut_policy_review": soft_haircut_policy_review,
        "soft_haircut_bucket_split_review": soft_haircut_bucket_split_review,
        "soft_haircut_bucket_sample_accrual": soft_haircut_bucket_sample_accrual,
        "soft_haircut_shadow_probe_design": soft_haircut_shadow_probe_design,
        "soft_haircut_shadow_probe_ledger": soft_haircut_shadow_probe_ledger,
        "active_false_reject_review": active_false[:10],
        "active_protective": active_protective[:10],
        "active_monitor": active_monitor[:10],
        "active_insufficient_settled_sample": active_insufficient[:10],
        "historical_false_reject_residual": historical_false[:10],
        "active_pnl_size_coverage_gap": active_size_gaps[:10],
        "active_non_actionable_pnl_size_coverage_gap": active_non_actionable_size_gaps[:10],
        "historical_pnl_size_coverage_gap": historical_size_gaps[:10],
        "pnl_size_coverage_gap": size_gaps[:10],
        "rows": rows[: max(1, int(limit))],
        "next_mainline_task": next_task,
        "recommended_action": recommended_action,
        "field_contract": [
            "rejection_reason",
            "review_bucket",
            "active_count",
            "settled_count",
            "hit_rate",
            "avg_counterfactual_pnl_per_share",
            "realized_counterfactual_pnl_sum",
            "size_missing_pnl_rate",
            "strategy_family_top",
            "gate_class",
            "gate_subtype",
            "recommended_treatment",
            "avg_order_price",
            "avg_spread",
            "avg_entry_usable_edge",
            "avg_edge_tradeability_profit_value",
        ],
        "notes": [
            "active_count is computed from the latest entry run in auto_trade_decisions using the same rejection_reason derivation.",
            "active_false_reject_review means a reason is active now and historical settled rejects for that reason later hit often enough to deserve review.",
            "historical_false_reject_residual is retained for audit but is not active in the latest run.",
        ],
    }


def _rule_effectiveness_status(review: Mapping[str, Any]) -> dict[str, Any]:
    active_false = list(review.get("active_false_reject_review") or [])
    active_protective = list(review.get("active_protective") or [])
    active_monitor = list(review.get("active_monitor") or [])
    active_insufficient = list(review.get("active_insufficient_settled_sample") or [])
    active_size_gap = list(review.get("active_pnl_size_coverage_gap") or [])
    active_non_actionable_size_gap = list(
        review.get("active_non_actionable_pnl_size_coverage_gap") or []
    )
    historical_false = list(review.get("historical_false_reject_residual") or [])
    historical_size_gap = list(review.get("historical_pnl_size_coverage_gap") or [])
    review_next_task = str(review.get("next_mainline_task") or "")
    review_action = str(review.get("recommended_action") or "")
    if active_false:
        status = "active_false_reject_alert"
        next_task = "active_reject_rule_review"
        action = "review_active_reject_reasons_with_positive_counterfactual_evidence"
    elif active_size_gap:
        status = "active_size_coverage_gap"
        next_task = "reject_ledger_size_coverage_roll_forward"
        action = "fix_active_reject_ledger_hypothetical_size_coverage"
    elif active_insufficient:
        status = "active_sample_accrual"
        next_task = "reject_ledger_active_sample_accrual"
        action = "continue_collecting_settled_samples_for_active_reject_reasons"
    elif review_next_task and review_next_task != "rule_effectiveness_monitoring":
        status = "mainline_followup"
        next_task = review_next_task
        action = review_action or "continue_specific_reject_ledger_followup"
    elif active_monitor:
        status = "active_monitoring"
        next_task = "rule_effectiveness_monitoring"
        action = "continue_monitoring_active_reject_reasons"
    else:
        status = "healthy"
        next_task = "rule_effectiveness_monitoring"
        action = "continue_monitoring_reject_rule_effectiveness"
    active_total = sum(int(row.get("active_count") or 0) for row in [*active_false, *active_protective, *active_monitor, *active_insufficient])
    settled_active_total = sum(int(row.get("settled_count") or 0) for row in [*active_false, *active_protective, *active_monitor, *active_insufficient])
    return {
        "ok": status not in {"active_false_reject_alert", "active_size_coverage_gap"},
        "status": status,
        "next_mainline_task": next_task,
        "recommended_action": action,
        "current_run_id": int(review.get("current_run_id") or 0),
        "active_reject_reason_count": len([*active_false, *active_protective, *active_monitor, *active_insufficient]),
        "active_reject_decision_count": int(active_total),
        "active_settled_sample_count": int(settled_active_total),
        "active_false_reject_review_count": len(active_false),
        "active_protective_count": len(active_protective),
        "active_monitor_count": len(active_monitor),
        "active_insufficient_settled_sample_count": len(active_insufficient),
        "active_pnl_size_coverage_gap_count": len(active_size_gap),
        "active_non_actionable_pnl_size_coverage_gap_count": len(active_non_actionable_size_gap),
        "historical_false_reject_residual_count": len(historical_false),
        "historical_pnl_size_coverage_gap_count": len(historical_size_gap),
        "top_active_false_reject_review": active_false[:3],
        "top_active_protective": active_protective[:5],
        "top_active_non_actionable_pnl_size_coverage_gap": active_non_actionable_size_gap[:5],
        "top_historical_false_reject_residual": historical_false[:3],
        "notes": [
            "Only active_false_reject_alert and active_size_coverage_gap block the monitoring loop.",
            "Hard Gate rows without hypothetical size are treated as non-actionable PnL-size NA, not a soft-haircut size coverage blocker.",
            "Historical residuals are retained for audit and do not override the active status.",
        ],
    }


def materialize_reject_ledger(
    *,
    db_path: Path = DEFAULT_DB,
    days: int = 7,
    limit: int = 0,
    dry_run: bool = False,
) -> dict[str, Any]:
    start_utc, end_utc, start_et, end_et = ecr._window_for_days(int(days))  # type: ignore[attr-defined]
    if not db_path.exists():
        return {
            "ok": False,
            "error": "archive_db_missing",
            "db": str(db_path),
            "kind": LEDGER_KIND,
        }
    if dry_run:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        _configure_connection(conn)
        if not dry_run:
            ensure_reject_ledger_table(conn)
        rows = _load_rejected_decisions(
            conn,
            start_utc=start_utc,
            end_utc=end_utc,
            limit=int(limit or 0),
        )
        event_keys = {
            (str(row["slug"] or "").strip(), str(row["event_date"] or "").split("T", 1)[0].strip())
            for row in rows
        }
        contract_keys = {
            (
                str(row["slug"] or "").strip(),
                str(row["event_date"] or "").split("T", 1)[0].strip(),
                str(row["outcome"] or "").strip(),
                str(row["side"] or "").strip().upper(),
            )
            for row in rows
        }
        settlements = ecr._load_settlements(conn, event_keys)  # type: ignore[attr-defined]
        fills = ecr._load_fills(conn, contract_keys)  # type: ignore[attr-defined]
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ledger_rows: list[dict[str, Any]] = []
        for row in rows:
            event_key = (
                str(row["slug"] or "").strip(),
                str(row["event_date"] or "").split("T", 1)[0].strip(),
            )
            contract_key = (
                event_key[0],
                event_key[1],
                str(row["outcome"] or "").strip(),
                str(row["side"] or "").strip().upper(),
            )
            ledger_row = _ledger_row(
                row,
                settlement=settlements.get(event_key),
                fill=fills.get(contract_key),
                now_iso=now_iso,
            )
            if ledger_row is not None:
                ledger_rows.append(ledger_row)
        upserted = 0
        if not dry_run:
            upserted = _upsert_rows(conn, ledger_rows)
            conn.commit()
        quality: dict[str, Any] = {"ok": True, "skipped": True}
        table_rows = 0
        if _table_exists(conn, LEDGER_TABLE):
            table_rows = int(
                conn.execute(f"SELECT COUNT(*) FROM {LEDGER_TABLE}").fetchone()[0] or 0
            )
            quality = _ledger_quality(conn)
        quality_review = (
            build_quality_review(conn)
            if _table_exists(conn, LEDGER_TABLE)
            else {"ok": False, "error": "reject_ledger_table_missing"}
        )
        shadow_probe_rows: list[dict[str, Any]] = []
        shadow_probe_upserted = 0
        shadow_probe_quality: dict[str, Any] = {"ok": True, "skipped": True}
        if isinstance(quality_review, Mapping) and quality_review.get("ok"):
            shadow_design = (
                (quality_review.get("soft_haircut_shadow_probe_design") or {})
                if isinstance(quality_review.get("soft_haircut_shadow_probe_design"), Mapping)
                else {}
            )
            shadow_probe_rows = _build_shadow_probe_rows(
                ledger_rows,
                shadow_design,
                now_iso=now_iso,
            )
            if not dry_run:
                ensure_shadow_probe_table(conn)
                shadow_probe_upserted = _upsert_shadow_probe_rows(conn, shadow_probe_rows)
                conn.commit()
                shadow_probe_quality = _shadow_probe_quality(conn)
                quality_review = build_quality_review(conn)
            elif shadow_probe_rows:
                shadow_probe_quality = {
                    "ok": True,
                    "dry_run": True,
                    "rows_built": len(shadow_probe_rows),
                }
        rule_effectiveness_monitor = (
            _rule_effectiveness_status(quality_review)
            if isinstance(quality_review, Mapping) and quality_review.get("ok")
            else {
                "ok": False,
                "status": "reject_ledger_quality_review_missing",
                "next_mainline_task": "reject_ledger_quality_review",
            }
        )
        settled_rows = sum(1 for row in ledger_rows if row.get("side_hit") is not None)
        hit_rows = sum(1 for row in ledger_rows if row.get("side_hit") == 1)
        pending_rows = sum(1 for row in ledger_rows if row.get("subsequent_outcome") == "settlement_pending")
        missing_rows = sum(1 for row in ledger_rows if row.get("subsequent_outcome") == "settlement_missing")
        pnl_values = [
            float(row["realized_counterfactual_pnl"])
            for row in ledger_rows
            if _to_float(row.get("realized_counterfactual_pnl")) is not None
        ]
        return {
            "ok": True,
            "kind": LEDGER_KIND,
            "read_only": bool(dry_run),
            "dry_run": bool(dry_run),
            "db": str(db_path),
            "table": LEDGER_TABLE,
            "window": {
                "days": int(days),
                "start_utc": start_utc,
                "end_utc": end_utc,
                "start_et": start_et,
                "end_et": end_et,
            },
            "rows_scanned": len(rows),
            "ledger_rows_built": len(ledger_rows),
            "rows_upserted": int(upserted),
            "table_rows": int(table_rows),
            "quality": quality,
            "quality_review": quality_review,
            "shadow_probe": {
                "ok": bool(shadow_probe_quality.get("ok")),
                "kind": SHADOW_PROBE_KIND,
                "table": SHADOW_PROBE_TABLE,
                "rows_built": len(shadow_probe_rows),
                "rows_upserted": int(shadow_probe_upserted),
                "quality": shadow_probe_quality,
                "field_contract": SHADOW_PROBE_COLUMNS,
                "notes": [
                    "Shadow probe rows are derived from reject_ledger_v1 exact-bucket matches.",
                    "live_orders_enabled is always false in this instrumentation step.",
                    "This ledger does not submit orders or alter entry sizing.",
                ],
            },
            "rule_effectiveness_monitor": rule_effectiveness_monitor,
            "settled_rows": int(settled_rows),
            "hit_rows": int(hit_rows),
            "pending_rows": int(pending_rows),
            "settlement_missing_rows": int(missing_rows),
            "counterfactual_pnl_sum": sum(pnl_values) if pnl_values else None,
            "rejection_reason_top": _top_counts(ledger_rows, "rejection_reason"),
            "strategy_family_top": _top_counts(ledger_rows, "strategy_family"),
            "subsequent_outcome_top": _top_counts(ledger_rows, "subsequent_outcome"),
            "field_contract": LEDGER_COLUMNS,
            "next_mainline_task": str(
                rule_effectiveness_monitor.get("next_mainline_task")
                if isinstance(rule_effectiveness_monitor, Mapping)
                else "reject_ledger_quality_review"
            ),
            "notes": [
                "source_decision_id is unique, so repeated materialization is idempotent.",
                "realized_counterfactual_pnl is binary settlement PnL from hypothetical buy price and size when both are available.",
                "The ledger is derived from auto_trade_decisions, event_runs, and edge_trade_fills; it does not change trading policy.",
            ],
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize rejected entry decisions into reject_ledger_v1.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    payload = materialize_reject_ledger(
        db_path=Path(args.db),
        days=int(args.days),
        limit=int(args.limit),
        dry_run=bool(args.dry_run),
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
