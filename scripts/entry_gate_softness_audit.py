#!/usr/bin/env python3
"""Read-only audit for entry gate blockers vs soft penalty candidates."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple


CATEGORY_ORDER = (
    "live_order_intent",
    "hard_safety_or_contract",
    "churn_or_position_risk",
    "alpha_gate_current",
    "soft_penalty_candidate",
    "strategy_shape_filter",
    "infrastructure_failure",
    "gate_umbrella",
    "unknown",
)

GATE_UMBRELLA_CODES = {
    "entry_gate_blocked",
    "auto_trade_entry_blocked",
    "entry_blocked",
}

LIVE_ORDER_CODES = {
    "order_submitted",
    "dry_run_order_submitted",
    "order_created",
    "will_order",
}

HARD_SAFETY_CODES = {
    "balance_insufficient",
    "bucket_exposure_cap",
    "clob_auth_missing",
    "clob_submit_failed",
    "contract_rule_missing",
    "duplicate_open_order",
    "entry_balance_insufficient_preflight",
    "event_exposure_cap",
    "invalid_token",
    "market_archived",
    "market_closed",
    "market_inactive",
    "market_not_accepting_orders",
    "market_not_ready",
    "min_order_size",
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

CHURN_RISK_CODES = {
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

ALPHA_GATE_CODES = {
    "q_missing",
    "edge_lcb_below_min",
    "edge_realizable_low",
    "edge_realizable_missing",
    "edge_realizable_p_converge_below_min",
    "edge_realizable_p_realize_below_min",
    "edge_tradeability_blocked",
    "edge_tradeability_missing",
    "edge_tradeability_shadow_block",
    "ev_below_min",
    "plain_lcb_min_total_edge_below_min",
    "positive_ev_false",
    "probability_below_min",
    "signal_lcb_below_min",
}

STRATEGY_FILTER_CODES = {
    "legacy_entry_strategy_retired",
    "plain_lcb_pinned_tail_no_filtered",
    "skip_top",
    "strategy_disabled",
    "strategy_size_ratio_zero",
}

INFRASTRUCTURE_FAILURE_CODES = {
    "decision_persistence_failed",
    "event_worker_failed",
    "event_worker_timeout",
    "event_worker_timeout_inline_failed",
}

SOFT_PENALTY_TERMS = (
    "ask",
    "depth",
    "lag",
    "liquidity",
    "market_tick_extreme_tail_skipped",
    "market_tick_stale",
    "market_ask_missing_no_bid",
    "market_one_sided",
    "no_by_yes",
    "overprice",
    "price",
    "q_below",
    "q_lcb",
    "q_missing_soft",
    "q_raw",
    "outcome_volume",
    "spread",
    "stale",
    "tick",
    "volume",
)


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


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


def parse_reason_codes(raw: Any) -> List[str]:
    if isinstance(raw, list):
        parsed = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            return []
    if not isinstance(parsed, list):
        return []
    out: List[str] = []
    for item in parsed:
        code = str(item or "").strip()
        if code:
            out.append(code)
    return out


def classify_reason_code(code: str) -> str:
    text = str(code or "").strip().lower()
    if not text:
        return "unknown"
    if text in LIVE_ORDER_CODES:
        return "live_order_intent"
    if text in GATE_UMBRELLA_CODES:
        return "gate_umbrella"
    if text in HARD_SAFETY_CODES:
        return "hard_safety_or_contract"
    if text in CHURN_RISK_CODES or "cooldown" in text or "loss_streak" in text:
        return "churn_or_position_risk"
    if text in ALPHA_GATE_CODES or "edge_tradeability" in text or "edge_lcb" in text:
        return "alpha_gate_current"
    if text in STRATEGY_FILTER_CODES or text.startswith("plain_lcb_"):
        return "strategy_shape_filter"
    if text in INFRASTRUCTURE_FAILURE_CODES or text.startswith("event_worker_"):
        return "infrastructure_failure"
    if any(term in text for term in SOFT_PENALTY_TERMS):
        return "soft_penalty_candidate"
    return "unknown"


def classify_reason_group(codes: Sequence[str], will_order: bool = False) -> str:
    if will_order:
        return "live_order_intent"
    categories = [classify_reason_code(code) for code in codes]
    categories = [cat for cat in categories if cat != "gate_umbrella"]
    if not categories:
        return "gate_umbrella" if codes else "unknown"
    rank = {cat: idx for idx, cat in enumerate(CATEGORY_ORDER)}
    return min(categories, key=lambda cat: rank.get(cat, len(rank)))


def treatment_for_category(category: str) -> str:
    if category == "hard_safety_or_contract":
        return "keep_hard_block"
    if category == "churn_or_position_risk":
        return "keep_or_size_down_until_position_state_is_clean"
    if category == "alpha_gate_current":
        return "current_alpha_gate_review_only_edge_tradeability_should_block"
    if category == "soft_penalty_candidate":
        return "convert_to_entry_usable_edge_penalty"
    if category == "strategy_shape_filter":
        return "review_strategy_filter_keep_if_empirically_useful"
    if category == "infrastructure_failure":
        return "fix_worker_or_persistence_path"
    if category == "gate_umbrella":
        return "ignore_wrapper_use_companion_reason_codes"
    if category == "live_order_intent":
        return "ordered_or_dry_run_order_intent"
    return "needs_mapping"


def _reason_group_key(codes: Sequence[str]) -> Tuple[str, ...]:
    seen: Dict[str, None] = {}
    for code in codes:
        text = str(code or "").strip()
        if text:
            seen.setdefault(text, None)
    return tuple(seen.keys())


def _merge_reason_codes(*groups: Sequence[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for group in groups:
        for code in group:
            text = str(code or "").strip()
            if text:
                seen.setdefault(text, None)
    return list(seen.keys())


def _new_stats(category: str) -> Dict[str, Any]:
    return {
        "category": category,
        "n": 0,
        "will_order_n": 0,
        "usable_edge_values": [],
        "positive_usable_n": 0,
        "example": None,
    }


def _update_stats(item: MutableMapping[str, Any], row: Mapping[str, Any], usable_edge: Optional[float]) -> None:
    item["n"] = int(item.get("n", 0)) + 1
    will_order = bool(row.get("will_order"))
    item["will_order_n"] = int(item.get("will_order_n", 0)) + (1 if will_order else 0)
    if usable_edge is not None:
        vals = item.setdefault("usable_edge_values", [])
        vals.append(float(usable_edge))
        if usable_edge > 0:
            item["positive_usable_n"] = int(item.get("positive_usable_n", 0)) + 1
    if item.get("example") is None:
        item["example"] = {
            "id": row.get("id"),
            "run_id": row.get("run_id"),
            "created_at": row.get("created_at"),
            "slug": row.get("slug"),
            "event_date": row.get("event_date"),
            "outcome": row.get("outcome"),
            "side": row.get("side"),
        }


def _finalize_stats(item: Mapping[str, Any]) -> Dict[str, Any]:
    vals = [float(v) for v in item.get("usable_edge_values", [])]
    n = int(item.get("n", 0))
    positive_n = int(item.get("positive_usable_n", 0))
    out = {
        "category": item.get("category"),
        "n": n,
        "will_order_n": int(item.get("will_order_n", 0)),
        "positive_usable_edge_n": positive_n,
        "positive_usable_edge_rate": (positive_n / n) if n else None,
        "avg_entry_usable_edge": (sum(vals) / len(vals)) if vals else None,
        "max_entry_usable_edge": max(vals) if vals else None,
        "suggested_treatment": treatment_for_category(str(item.get("category") or "unknown")),
        "example": item.get("example"),
    }
    return out


def _row_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def build_report(db_path: Path, since_hours: float, limit: int = 40) -> Dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(since_hours)))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            _row_dict(row)
            for row in conn.execute(
                """
                SELECT
                  id,
                  run_id,
                  created_at,
                  slug,
                  event_date,
                  outcome,
                  side,
                  decided_state,
                  will_order,
                  reason_codes_json,
                  details_json
                FROM auto_trade_decisions
                WHERE action = 'entry'
                  AND datetime(created_at) >= datetime(?)
                ORDER BY id DESC
                """,
                (since.isoformat(timespec="seconds"),),
            )
        ]
    finally:
        conn.close()

    code_stats: Dict[str, Dict[str, Any]] = {}
    group_stats: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    row_category_counts: Counter[str] = Counter()
    rows_with_usable = 0
    rows_positive_usable = 0
    will_order_n = 0

    for row in rows:
        details = _safe_json_dict(row.get("details_json"))
        hard_codes = parse_reason_codes(row.get("reason_codes_json"))
        soft_codes = parse_reason_codes(details.get("entry_soft_reason_codes"))
        codes = _merge_reason_codes(hard_codes, soft_codes)
        usable_edge = _to_float(details.get("entry_usable_edge"))
        if usable_edge is not None:
            rows_with_usable += 1
            if usable_edge > 0:
                rows_positive_usable += 1
        if bool(row.get("will_order")):
            will_order_n += 1

        group_category = classify_reason_group(codes, will_order=bool(row.get("will_order")))
        row_category_counts[group_category] += 1
        group_key = _reason_group_key(codes) or ("__no_reason_code__",)
        group_item = group_stats.setdefault(group_key, _new_stats(group_category))
        group_item["category"] = group_category
        _update_stats(group_item, row, usable_edge)

        if not codes:
            codes = ["__no_reason_code__"]
        for code in codes:
            category = classify_reason_code(code)
            item = code_stats.setdefault(code, _new_stats(category))
            item["category"] = category
            _update_stats(item, row, usable_edge)

    code_rows = []
    for code, item in code_stats.items():
        finalized = _finalize_stats(item)
        finalized["code"] = code
        code_rows.append(finalized)
    code_rows.sort(key=lambda x: (-int(x["n"]), str(x["code"])))

    group_rows = []
    for group_key, item in group_stats.items():
        finalized = _finalize_stats(item)
        finalized["reason_codes"] = list(group_key)
        group_rows.append(finalized)
    group_rows.sort(key=lambda x: (-int(x["n"]), str(x["reason_codes"])))

    softening_candidates = [
        row
        for row in code_rows
        if row.get("category") == "soft_penalty_candidate"
        and int(row.get("positive_usable_edge_n") or 0) > 0
    ]
    softening_candidates.sort(
        key=lambda x: (
            -int(x.get("positive_usable_edge_n") or 0),
            -(float(x.get("max_entry_usable_edge") or 0.0)),
            str(x.get("code") or ""),
        )
    )

    non_strategy_hard_blockers = [
        row
        for row in code_rows
        if row.get("category") in {"hard_safety_or_contract", "churn_or_position_risk"}
    ]

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db": str(db_path),
        "since": since.isoformat(timespec="seconds"),
        "read_only": True,
        "scope": "auto_trade_decisions(action=entry)",
        "notes": [
            "This report reads archive.db only and does not update trading state.",
            "entry_gate_blocked is treated as a wrapper; companion reason codes determine the row category.",
            "soft_penalty_candidate means a current blocker should be considered for entry_usable_edge penalty/size-down, not automatic buying.",
        ],
        "counts": {
            "rows_total": len(rows),
            "will_order_n": will_order_n,
            "rows_with_entry_usable_edge": rows_with_usable,
            "rows_positive_entry_usable_edge": rows_positive_usable,
        },
        "row_category_counts": dict(row_category_counts),
        "top_reason_codes": code_rows[:limit],
        "top_reason_groups": group_rows[:limit],
        "softening_candidates": softening_candidates[:limit],
        "non_strategy_hard_blockers": non_strategy_hard_blockers[:limit],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of entry blockers that should stay hard vs become soft penalties."
    )
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--since-hours", type=float, default=6.0)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--out", default="data/reports/entry_gate_softness_audit_live.json")
    args = parser.parse_args()

    db_path = Path(args.db)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        payload = {
            "ok": False,
            "error": f"db_missing:{db_path}",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        return 2

    payload = build_report(db_path=db_path, since_hours=float(args.since_hours), limit=int(args.limit))
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "out": str(out_path),
                "rows_total": payload["counts"]["rows_total"],
                "will_order_n": payload["counts"]["will_order_n"],
                "row_category_counts": payload["row_category_counts"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
