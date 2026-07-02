from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from scripts import paper_trading_validation


def test_paper_validation_waiting_for_settlement_is_warning(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    readiness = {
        "ok": True,
        "warnings": ["calibration_labels_missing"],
        "blockers": [],
        "readiness": {
            "event_run_bayes_status": "ok",
            "decision_bayes_status": "ok",
        },
        "event_runs": {"bayes_bucket_coverage": 1.0},
        "decisions": {
            "bayes_rows": 10,
            "bayes_applicable_rows": 10,
            "bayes_coverage": 1.0,
            "will_order_rows": 0,
            "labeled_rows": 0,
            "reliability_bins": [],
        },
        "settlement_label_plan": {
            "planned_labels": 0,
            "settlement_skipped": {"settlement_not_closed": 1},
            "skipped": {"settlement_missing": 10},
        },
    }

    with patch.object(
        paper_trading_validation.bayesian_readiness_audit,
        "build_report",
        return_value=readiness,
    ), patch.object(
        paper_trading_validation.bayesian_reliability_report,
        "build_report",
        return_value={
            "ok": False,
            "blockers": ["no_labeled_bayesian_samples"],
            "warnings": [],
            "metrics": {"n": 0},
            "recommendations": [],
        },
    ), patch.object(paper_trading_validation, "_table_count", return_value=0), patch.object(
        paper_trading_validation, "_latest_event_dates", return_value=[]
    ):
        report = paper_trading_validation.build_report(db_path, since_days=3, city="chengdu")

    assert report["ok"] is False
    assert "paper_validation_window_below_min_days" in report["blockers"]
    assert "paper_order_intent_not_seen" in report["blockers"]
    assert "no_labeled_bayesian_samples" in report["blockers"]
    assert "waiting_for_settlement_labels" in report["warnings"]
    assert report["paper_status"]["bayes_samples"] == 10
    assert report["paper_status"]["settlement_not_closed"] == 1
    assert report["next_mainline_task"] == "paper_trading_validation"


def test_paper_validation_default_window_matches_min_validation_days(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    readiness = {
        "ok": True,
        "warnings": [],
        "blockers": [],
        "event_runs": {"bayes_bucket_coverage": 1.0},
        "decisions": {
            "bayes_rows": 10,
            "bayes_applicable_rows": 10,
            "bayes_coverage": 1.0,
            "will_order_rows": 0,
            "labeled_rows": 0,
            "reliability_bins": [],
        },
        "settlement_label_plan": {"planned_labels": 0, "skipped": {}},
    }

    with patch.object(
        paper_trading_validation.bayesian_readiness_audit,
        "build_report",
        return_value=readiness,
    ), patch.object(
        paper_trading_validation.bayesian_reliability_report,
        "build_report",
        return_value={
            "ok": False,
            "blockers": ["no_labeled_bayesian_samples"],
            "warnings": [],
            "metrics": {"n": 0},
            "recommendations": [],
        },
    ), patch.object(paper_trading_validation, "_table_count", return_value=0), patch.object(
        paper_trading_validation, "_latest_event_dates", return_value=[]
    ):
        report = paper_trading_validation.build_report(db_path, city="chengdu")

    assert report["since_days"] == paper_trading_validation.DEFAULT_MIN_VALIDATION_DAYS
    assert "paper_validation_window_below_min_days" not in report["blockers"]


def test_paper_validation_surfaces_pending_label_apply(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    readiness = {
        "ok": True,
        "warnings": [],
        "blockers": [],
        "readiness": {
            "event_run_bayes_status": "ok",
            "decision_bayes_status": "ok",
        },
        "event_runs": {"bayes_bucket_coverage": 1.0},
        "decisions": {
            "bayes_rows": 4,
            "bayes_applicable_rows": 4,
            "bayes_coverage": 1.0,
            "will_order_rows": 1,
            "labeled_rows": 0,
            "reliability_bins": [],
        },
        "settlement_label_plan": {
            "planned_labels": 2,
            "skipped": {},
        },
    }

    with patch.object(
        paper_trading_validation.bayesian_readiness_audit,
        "build_report",
        return_value=readiness,
    ), patch.object(
        paper_trading_validation.bayesian_reliability_report,
        "build_report",
        return_value={
            "ok": False,
            "blockers": ["no_labeled_bayesian_samples"],
            "warnings": [],
            "metrics": {"n": 0},
            "recommendations": [],
        },
    ), patch.object(paper_trading_validation, "_table_count", return_value=0), patch.object(
        paper_trading_validation, "_latest_event_dates", return_value=[]
    ):
        report = paper_trading_validation.build_report(db_path, since_days=3, city="chengdu")

    assert report["ok"] is False
    assert "calibration_labels_pending_apply" in report["warnings"]
    assert report["paper_status"]["planned_labels"] == 2
    assert report["next_actions"][0] == (
        "review_settlement_label_plan_then_run_build_settlement_calibration_labels_apply_with_confirm_apply"
    )


def test_paper_validation_blocks_without_bayesian_samples(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    readiness = {
        "ok": True,
        "warnings": [],
        "blockers": [],
        "event_runs": {"bayes_bucket_coverage": 1.0},
        "decisions": {
            "bayes_rows": 0,
            "bayes_applicable_rows": 0,
            "bayes_coverage": None,
            "will_order_rows": 0,
            "labeled_rows": 0,
            "reliability_bins": [],
        },
        "settlement_label_plan": {"planned_labels": 0, "skipped": {}},
    }

    with patch.object(
        paper_trading_validation.bayesian_readiness_audit,
        "build_report",
        return_value=readiness,
    ), patch.object(
        paper_trading_validation.bayesian_reliability_report,
        "build_report",
        return_value={
            "ok": False,
            "blockers": ["no_labeled_bayesian_samples"],
            "warnings": [],
            "metrics": {"n": 0},
            "recommendations": [],
        },
    ), patch.object(paper_trading_validation, "_table_count", return_value=0), patch.object(
        paper_trading_validation, "_latest_event_dates", return_value=[]
    ):
        report = paper_trading_validation.build_report(db_path, since_days=3, city="chengdu")

    assert report["ok"] is False
    assert "no_bayesian_paper_samples" in report["blockers"]


def test_paper_validation_passes_after_full_window_and_labeled_samples(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    readiness = {
        "ok": True,
        "warnings": [],
        "blockers": [],
        "event_runs": {"bayes_bucket_coverage": 1.0},
        "decisions": {
            "bayes_rows": 60,
            "bayes_applicable_rows": 60,
            "bayes_coverage": 1.0,
            "will_order_rows": 3,
            "labeled_rows": 40,
            "reliability_bins": [{"bin": "0.4-0.6"}],
        },
        "settlement_label_plan": {"planned_labels": 0, "skipped": {}},
    }

    with patch.object(
        paper_trading_validation.bayesian_readiness_audit,
        "build_report",
        return_value=readiness,
    ), patch.object(
        paper_trading_validation.bayesian_reliability_report,
        "build_report",
        return_value={
            "ok": True,
            "blockers": [],
            "warnings": [],
            "metrics": {"n": 40, "brier": 0.18, "ece": 0.04},
            "calibration_contract": {"metrics": {"n": 40, "brier": 0.18, "ece": 0.04}},
            "recommendations": [],
        },
    ), patch.object(paper_trading_validation, "_table_count", return_value=0), patch.object(
        paper_trading_validation, "_latest_event_dates", return_value=[]
    ):
        report = paper_trading_validation.build_report(db_path, since_days=42, city="chengdu")

    assert report["ok"] is True
    assert report["blockers"] == []
    assert report["next_mainline_task"] == "small_capital_validation_review"


def test_paper_validation_blocks_when_reliability_metrics_above_gate(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    readiness = {
        "ok": True,
        "warnings": [],
        "blockers": [],
        "event_runs": {"bayes_bucket_coverage": 1.0},
        "decisions": {
            "bayes_rows": 60,
            "bayes_applicable_rows": 60,
            "bayes_coverage": 1.0,
            "will_order_rows": 3,
            "labeled_rows": 40,
            "reliability_bins": [{"bin": "0.4-0.6"}],
        },
        "settlement_label_plan": {"planned_labels": 0, "skipped": {}},
    }

    with patch.object(
        paper_trading_validation.bayesian_readiness_audit,
        "build_report",
        return_value=readiness,
    ), patch.object(
        paper_trading_validation.bayesian_reliability_report,
        "build_report",
        return_value={
            "ok": True,
            "blockers": [],
            "warnings": ["bayesian_reliability_ece_high"],
            "metrics": {"n": 40, "brier": 0.31, "ece": 0.12},
            "calibration_contract": {"metrics": {"n": 40, "brier": 0.31, "ece": 0.12}},
            "recommendations": [],
        },
    ), patch.object(paper_trading_validation, "_table_count", return_value=0), patch.object(
        paper_trading_validation, "_latest_event_dates", return_value=[]
    ):
        report = paper_trading_validation.build_report(db_path, since_days=42, city="chengdu")

    assert report["ok"] is False
    assert "paper_reliability_brier_above_max" in report["blockers"]
    assert "paper_reliability_ece_above_max" in report["blockers"]
    assert report["paper_status"]["max_reliability_brier"] == paper_trading_validation.DEFAULT_MAX_RELIABILITY_BRIER
    assert report["paper_status"]["max_reliability_ece"] == paper_trading_validation.DEFAULT_MAX_RELIABILITY_ECE


def test_paper_validation_uses_calibrated_contract_gate_when_available(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    readiness = {
        "ok": True,
        "warnings": [],
        "blockers": [],
        "event_runs": {"bayes_bucket_coverage": 1.0},
        "decisions": {
            "bayes_rows": 60,
            "bayes_applicable_rows": 60,
            "bayes_coverage": 1.0,
            "will_order_rows": 3,
            "labeled_rows": 40,
            "reliability_bins": [{"bin": "0.4-0.6"}],
        },
        "settlement_label_plan": {"planned_labels": 0, "skipped": {}},
    }

    with patch.object(
        paper_trading_validation.bayesian_readiness_audit,
        "build_report",
        return_value=readiness,
    ), patch.object(
        paper_trading_validation.bayesian_reliability_report,
        "build_report",
        return_value={
            "ok": True,
            "blockers": [],
            "warnings": ["bayesian_reliability_ece_high"],
            "metrics": {"n": 80, "brier": 0.20, "ece": 0.11},
            "calibration_contract": {"metrics": {"n": 40, "brier": 0.12, "ece": 0.03}},
            "recommendations": [],
        },
    ), patch.object(paper_trading_validation, "_table_count", return_value=0), patch.object(
        paper_trading_validation, "_latest_event_dates", return_value=[]
    ):
        report = paper_trading_validation.build_report(db_path, since_days=42, city="chengdu")

    assert report["ok"] is True
    assert report["paper_status"]["reliability_gate_scope"] == "historical_prior_bias_corrected"
    assert report["paper_status"]["global_reliability_ece"] == 0.11
    assert report["paper_status"]["reliability_ece"] == 0.03


def test_paper_validation_surfaces_tradeability_gap_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    readiness = {
        "ok": True,
        "warnings": [],
        "blockers": [],
        "event_runs": {"bayes_bucket_coverage": 1.0},
        "decisions": {
            "bayes_rows": 60,
            "bayes_applicable_rows": 60,
            "bayes_coverage": 1.0,
            "will_order_rows": 0,
            "labeled_rows": 0,
            "reliability_bins": [],
        },
        "settlement_label_plan": {"planned_labels": 0, "skipped": {}},
    }

    with patch.object(
        paper_trading_validation.bayesian_readiness_audit,
        "build_report",
        return_value=readiness,
    ), patch.object(
        paper_trading_validation.bayesian_reliability_report,
        "build_report",
        return_value={
            "ok": False,
            "blockers": ["no_labeled_bayesian_samples"],
            "warnings": [],
            "metrics": {"n": 0},
            "recommendations": [],
        },
    ), patch.object(paper_trading_validation, "_table_count", return_value=0), patch.object(
        paper_trading_validation, "_latest_event_dates", return_value=["2026-05-31"]
    ), patch.object(
        paper_trading_validation.tradeability_gap_review,
        "build_review",
        return_value={
            "ok": True,
            "gap_rows": 11,
            "by_gap_reason": {
                "paper_review_candidate": 10,
                "expected_exit_bid_below_effective_price": 1,
            },
            "rows": [],
        },
    ):
        report = paper_trading_validation.build_report(db_path, since_days=42, city="")

    assert "tradeability_gap_paper_candidates_pending_review" in report["warnings"]
    assert report["paper_status"]["tradeability_gap_rows"] == 11
    assert report["paper_status"]["tradeability_gap_paper_candidates"] == 10
    assert report["paper_status"]["tradeability_gap_review_dates"] == ["2026-05-31"]
    assert report["paper_status"]["tradeability_gap_reasons"]["paper_review_candidate"] == 10
    assert "review_tradeability_gap_paper_candidates_before_small_capital" in report["next_actions"]
