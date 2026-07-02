#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import bayesian_readiness_audit, bayesian_reliability_report, tradeability_gap_review


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "paper_trading_validation_latest.json"
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_MIN_VALIDATION_DAYS = 28
DEFAULT_MIN_LABELED_SAMPLES = 30
DEFAULT_MIN_PAPER_ORDER_INTENTS = 1
DEFAULT_MAX_RELIABILITY_BRIER = 0.24
DEFAULT_MAX_RELIABILITY_ECE = 0.08


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if out == out else None


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    if not exists:
        return 0
    row = conn.execute(f"SELECT COUNT(1) FROM {table}").fetchone()
    return _as_int(row[0] if row else 0)


def _latest_event_dates(db_path: Path, *, limit: int = 2) -> list[str]:
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30) as conn:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                """
                SELECT DISTINCT date(event_date) AS event_day
                FROM auto_trade_decisions
                WHERE action='entry'
                  AND event_date IS NOT NULL
                  AND event_day IS NOT NULL
                ORDER BY event_day DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0] or "") for row in rows if str(row[0] or "").strip()]


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    since_days: int = DEFAULT_MIN_VALIDATION_DAYS,
    city: str = "",
    market_scope: str = DEFAULT_MARKET_SCOPE,
    min_validation_days: int = DEFAULT_MIN_VALIDATION_DAYS,
    min_labeled_samples: int = DEFAULT_MIN_LABELED_SAMPLES,
    min_paper_order_intents: int = DEFAULT_MIN_PAPER_ORDER_INTENTS,
    max_reliability_brier: float = DEFAULT_MAX_RELIABILITY_BRIER,
    max_reliability_ece: float = DEFAULT_MAX_RELIABILITY_ECE,
    gap_event_dates: Sequence[str] = (),
) -> dict[str, Any]:
    readiness = bayesian_readiness_audit.build_report(
        db_path,
        since_days=max(1, int(since_days)),
        city=str(city or ""),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    blockers: list[str] = []
    warnings: list[str] = []
    warnings.extend(str(code) for code in readiness.get("warnings") or [])
    blockers.extend(str(code) for code in readiness.get("blockers") or [])

    decisions = readiness.get("decisions") if isinstance(readiness.get("decisions"), dict) else {}
    event_runs = readiness.get("event_runs") if isinstance(readiness.get("event_runs"), dict) else {}
    label_plan = (
        readiness.get("settlement_label_plan")
        if isinstance(readiness.get("settlement_label_plan"), dict)
        else {}
    )
    bayes_rows = _as_int(decisions.get("bayes_rows"))
    bayes_applicable_rows = _as_int(decisions.get("bayes_applicable_rows"))
    current_will_order_rows = _as_int(decisions.get("will_order_rows"))
    will_order_rows = _as_int(decisions.get("raw_bayesian_will_order_rows"))
    if will_order_rows <= 0:
        will_order_rows = _as_int(decisions.get("raw_will_order_rows"))
    if will_order_rows <= 0:
        will_order_rows = current_will_order_rows
    labeled_rows = _as_int(decisions.get("labeled_rows"))
    planned_labels = _as_int(label_plan.get("planned_labels"))
    settlement_missing = _as_int((label_plan.get("skipped") or {}).get("settlement_missing"))
    settlement_not_closed = _as_int(
        (label_plan.get("settlement_skipped") or {}).get("settlement_not_closed")
    )
    settlement_skipped = (
        label_plan.get("settlement_skipped")
        if isinstance(label_plan.get("settlement_skipped"), dict)
        else {}
    )
    settlement_source_issue_count = sum(
        _as_int(count)
        for key, count in settlement_skipped.items()
        if any(token in str(key) for token in ("source", "station", "resolution"))
    )
    reliability_bins = decisions.get("reliability_bins") if isinstance(decisions.get("reliability_bins"), list) else []
    reliability_report = bayesian_reliability_report.build_report(
        db_path,
        since_days=max(1, int(since_days)),
        city=str(city or ""),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    reliability_metrics = (
        reliability_report.get("metrics")
        if isinstance(reliability_report.get("metrics"), dict)
        else {}
    )
    calibration_contract = (
        reliability_report.get("calibration_contract")
        if isinstance(reliability_report.get("calibration_contract"), dict)
        else {}
    )
    contract_metrics = (
        calibration_contract.get("metrics")
        if isinstance(calibration_contract.get("metrics"), dict)
        else {}
    )
    event_cov = _as_float(event_runs.get("bayes_bucket_coverage"))
    decision_cov = _as_float(decisions.get("bayes_coverage"))

    if bayes_applicable_rows <= 0:
        blockers.append("no_bayesian_paper_samples")
    if int(since_days) < int(min_validation_days):
        blockers.append("paper_validation_window_below_min_days")
    if event_cov is not None and event_cov < 0.9:
        blockers.append("event_run_bayes_coverage_below_paper_gate")
    if decision_cov is not None and decision_cov < 0.9:
        blockers.append("decision_bayes_coverage_below_paper_gate")
    if planned_labels > 0:
        warnings.append("calibration_labels_pending_apply")
    if (
        (settlement_missing > 0 or settlement_not_closed > 0 or settlement_source_issue_count > 0)
        and planned_labels <= 0
        and labeled_rows <= 0
    ):
        warnings.append("waiting_for_settlement_labels")
    if settlement_source_issue_count > 0:
        warnings.append("settlement_resolution_source_needs_review")
    if not reliability_bins:
        warnings.append("reliability_bins_not_ready")
    warnings.extend(str(code) for code in reliability_report.get("warnings") or [])
    reliability_blockers = [str(code) for code in reliability_report.get("blockers") or []]
    blockers.extend(reliability_blockers)
    reliability_sample_count = _as_int(reliability_metrics.get("n"))
    calibrated_reliability_sample_count = _as_int(contract_metrics.get("n"))
    use_calibrated_reliability_gate = calibrated_reliability_sample_count >= int(min_labeled_samples)
    gate_metrics = contract_metrics if use_calibrated_reliability_gate else reliability_metrics
    gate_sample_count = calibrated_reliability_sample_count if use_calibrated_reliability_gate else reliability_sample_count
    if reliability_sample_count < int(min_labeled_samples):
        blockers.append("paper_labeled_samples_below_min")
    if calibrated_reliability_sample_count < int(min_labeled_samples):
        blockers.append("paper_calibrated_labeled_samples_below_min")
    reliability_brier = _as_float(gate_metrics.get("brier"))
    reliability_ece = _as_float(gate_metrics.get("ece"))
    if reliability_brier is not None and reliability_brier > float(max_reliability_brier):
        blockers.append("paper_reliability_brier_above_max")
    if reliability_ece is not None and reliability_ece > float(max_reliability_ece):
        blockers.append("paper_reliability_ece_above_max")
    if will_order_rows < int(min_paper_order_intents):
        blockers.append("paper_order_intent_not_seen")

    gap_review: dict[str, Any] = {}
    gap_dates = [str(day).strip() for day in (gap_event_dates or []) if str(day).strip()]
    if not gap_dates:
        gap_dates = _latest_event_dates(db_path, limit=2)
    if gap_dates:
        try:
            gap_review = tradeability_gap_review.build_review(
                db_path,
                gap_dates,
                limit=25,
                market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
            )
        except Exception as exc:
            warnings.append("tradeability_gap_review_error")
            gap_review = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    gap_reasons = (
        gap_review.get("by_gap_reason")
        if isinstance(gap_review.get("by_gap_reason"), dict)
        else {}
    )
    gap_paper_candidates = _as_int(gap_reasons.get("paper_review_candidate"))
    if gap_paper_candidates > 0:
        warnings.append("tradeability_gap_paper_candidates_pending_review")

    db_stats: dict[str, Any] = {}
    if db_path.exists():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30) as conn:
                conn.execute("PRAGMA query_only=ON")
                db_stats = {
                    "auto_trade_decisions": _table_count(conn, "auto_trade_decisions"),
                    "event_runs": _table_count(conn, "event_runs"),
                    "calibration_labels": _table_count(conn, "calibration_labels"),
                }
        except sqlite3.Error as exc:
            blockers.append("archive_db_read_error")
            db_stats = {"error": f"{type(exc).__name__}:{exc}"}
    else:
        blockers.append("archive_db_missing")

    next_actions = []
    if planned_labels > 0:
        next_actions.append(
            "review_settlement_label_plan_then_run_build_settlement_calibration_labels_apply_with_confirm_apply"
        )
    if settlement_missing > 0 or settlement_not_closed > 0:
        next_actions.append("wait_for_settlement_actual_then_rebuild_calibration_label_plan")
    if settlement_source_issue_count > 0:
        next_actions.append("repair_resolution_source_station_mapping_before_label_apply")
    if will_order_rows <= 0:
        next_actions.append("continue_dry_run_until_true_edge_generates_order_intent")
    if gap_paper_candidates > 0:
        next_actions.append("review_tradeability_gap_paper_candidates_before_small_capital")
    if reliability_bins:
        next_actions.append("review_reliability_bins_and_adjust_bayesian_prior_evidence")
    else:
        next_actions.append("collect_labeled_bayesian_samples_for_reliability_bins")

    return {
        "ok": not blockers,
        "read_only": True,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "since_days": max(1, int(since_days)),
        "city_filter": str(city or ""),
        "market_scope": str(market_scope or ""),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "paper_status": {
            "bayes_samples": bayes_rows,
            "bayes_applicable_samples": bayes_applicable_rows,
            "bayes_decision_coverage": decision_cov,
            "bayes_event_run_coverage": event_cov,
            "will_order_rows": will_order_rows,
            "current_will_order_rows": current_will_order_rows,
            "raw_will_order_rows": _as_int(decisions.get("raw_will_order_rows")),
            "raw_bayesian_will_order_rows": _as_int(
                decisions.get("raw_bayesian_will_order_rows")
            ),
            "labeled_rows": labeled_rows,
            "planned_labels": planned_labels,
            "settlement_missing": settlement_missing,
            "settlement_not_closed": settlement_not_closed,
            "settlement_source_issue_count": settlement_source_issue_count,
            "reliability_bins_ready": bool(reliability_bins),
            "reliability_report_ok": bool(reliability_report.get("ok")),
            "reliability_sample_count": reliability_sample_count,
            "calibrated_reliability_sample_count": calibrated_reliability_sample_count,
            "reliability_gate_scope": (
                "historical_prior_bias_corrected"
                if use_calibrated_reliability_gate
                else "all_bayesian_samples"
            ),
            "global_reliability_brier": _as_float(reliability_metrics.get("brier")),
            "global_reliability_ece": _as_float(reliability_metrics.get("ece")),
            "reliability_brier": reliability_brier,
            "reliability_ece": reliability_ece,
            "max_reliability_brier": float(max_reliability_brier),
            "max_reliability_ece": float(max_reliability_ece),
            "min_validation_days": int(min_validation_days),
            "min_labeled_samples": int(min_labeled_samples),
            "min_paper_order_intents": int(min_paper_order_intents),
            "tradeability_gap_review_dates": gap_dates,
            "tradeability_gap_rows": _as_int(gap_review.get("gap_rows")) if gap_review else 0,
            "tradeability_gap_paper_candidates": gap_paper_candidates,
            "tradeability_gap_reasons": dict(gap_reasons),
        },
        "db_stats": db_stats,
        "bayesian_readiness": {
            "ok": readiness.get("ok"),
            "readiness": readiness.get("readiness") or {},
            "settlement_label_plan": label_plan,
        },
        "bayesian_reliability": {
            "ok": reliability_report.get("ok"),
            "blockers": reliability_report.get("blockers") or [],
            "warnings": reliability_report.get("warnings") or [],
            "metrics": reliability_metrics,
            "recommendations": reliability_report.get("recommendations") or [],
        },
        "tradeability_gap_review": gap_review,
        "next_mainline_task": "paper_trading_validation" if blockers else "small_capital_validation_review",
        "next_actions": next_actions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Bayesian paper-trading validation.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-days", type=int, default=DEFAULT_MIN_VALIDATION_DAYS)
    parser.add_argument("--city", default="")
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE, help="highest_temperature|all")
    parser.add_argument("--min-validation-days", type=int, default=DEFAULT_MIN_VALIDATION_DAYS)
    parser.add_argument("--min-labeled-samples", type=int, default=DEFAULT_MIN_LABELED_SAMPLES)
    parser.add_argument("--min-paper-order-intents", type=int, default=DEFAULT_MIN_PAPER_ORDER_INTENTS)
    parser.add_argument("--max-reliability-brier", type=float, default=DEFAULT_MAX_RELIABILITY_BRIER)
    parser.add_argument("--max-reliability-ece", type=float, default=DEFAULT_MAX_RELIABILITY_ECE)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(
        Path(args.db),
        since_days=int(args.since_days),
        city=str(args.city or ""),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
        min_validation_days=int(args.min_validation_days),
        min_labeled_samples=int(args.min_labeled_samples),
        min_paper_order_intents=int(args.min_paper_order_intents),
        max_reliability_brier=float(args.max_reliability_brier),
        max_reliability_ece=float(args.max_reliability_ece),
    )
    text = json.dumps(report, ensure_ascii=False, indent=None if args.json else 2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(text)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
