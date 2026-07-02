from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from scripts import small_capital_validation_review


def _paper_report(ok: bool = True) -> dict:
    return {
        "ok": ok,
        "blockers": [] if ok else ["paper_order_intent_not_seen"],
        "warnings": [],
        "paper_status": {
            "bayes_samples": 60,
            "will_order_rows": 3,
            "reliability_sample_count": 40,
        },
    }


def _paper_report_reliability_fail() -> dict:
    return {
        "ok": False,
        "blockers": ["paper_reliability_brier_above_max", "paper_reliability_ece_above_max"],
        "warnings": [],
        "paper_status": {
            "bayes_samples": 60,
            "will_order_rows": 3,
            "reliability_sample_count": 40,
            "reliability_brier": 0.31,
            "reliability_ece": 0.12,
            "max_reliability_brier": 0.24,
            "max_reliability_ece": 0.08,
        },
    }


def _gap_review() -> dict:
    return {
        "ok": True,
        "gap_rows": 3,
        "by_gap_reason": {"paper_review_candidate": 2, "expected_exit_bid_below_effective_price": 1},
        "by_review_action": {
            "paper_track_d0_physical_or_settlement": 1,
            "paper_track_reverse_edge_no_side": 1,
            "exclude_until_tradeability_gap_fixed": 1,
        },
        "by_candidate_strategy": {
            "d0_physical_settlement_hybrid": 1,
            "reverse_edge_buy_no_sell_yes": 1,
            "settlement_review": 1,
        },
        "rows": [
            {
                "slug": "highest-temperature-in-chengdu-on-may-31-2026",
                "review_action": "paper_track_d0_physical_or_settlement",
                "candidate_strategy": "d0_physical_settlement_hybrid",
            },
            {
                "slug": "highest-temperature-in-chengdu-on-may-31-2026",
                "review_action": "paper_track_reverse_edge_no_side",
                "candidate_strategy": "reverse_edge_buy_no_sell_yes",
            },
            {
                "slug": "highest-temperature-in-chengdu-on-may-31-2026",
                "review_action": "exclude_until_tradeability_gap_fixed",
                "candidate_strategy": "settlement_review",
            },
        ],
    }


def test_small_capital_review_passes_for_one_secondary_city(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        small_capital_validation_review.paper_trading_validation,
        "build_report",
        return_value=_paper_report(True),
    ):
        report = small_capital_validation_review.build_report(
            db_path,
            pilot_cities=["chengdu"],
            since_days=42,
        )

    assert report["ok"] is True
    assert report["blockers"] == []
    assert report["pilot_scope"]["secondary_only"] is True
    assert report["next_mainline_task"] == "small_capital_pilot_enable_review"


def test_small_capital_review_blocks_major_city_pilot(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        small_capital_validation_review.paper_trading_validation,
        "build_report",
        return_value=_paper_report(True),
    ):
        report = small_capital_validation_review.build_report(
            db_path,
            pilot_cities=["London"],
            since_days=42,
        )

    assert report["ok"] is False
    assert "pilot_city_not_secondary" in report["blockers"]
    assert report["pilot_scope"]["major_cities"] == ["london"]


def test_small_capital_review_blocks_more_than_two_pilot_cities(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        small_capital_validation_review.paper_trading_validation,
        "build_report",
        return_value=_paper_report(True),
    ):
        report = small_capital_validation_review.build_report(
            db_path,
            pilot_cities=["chengdu", "atlanta", "cape-town"],
            since_days=42,
        )

    assert report["ok"] is False
    assert "pilot_city_above_max" in report["blockers"]


def test_small_capital_review_requires_four_week_window(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        small_capital_validation_review.paper_trading_validation,
        "build_report",
        return_value=_paper_report(True),
    ):
        report = small_capital_validation_review.build_report(
            db_path,
            pilot_cities=["chengdu"],
            since_days=21,
        )

    assert report["ok"] is False
    assert "small_capital_validation_window_below_min_days" in report["blockers"]
    assert report["validation_window"]["min_days"] == 28


def test_small_capital_review_requires_paper_pass_per_city(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        small_capital_validation_review.paper_trading_validation,
        "build_report",
        return_value=_paper_report(False),
    ):
        report = small_capital_validation_review.build_report(
            db_path,
            pilot_cities=["chengdu"],
            since_days=42,
        )

    assert report["ok"] is False
    assert "paper_validation_not_passed:chengdu" in report["blockers"]
    assert report["next_mainline_task"] == "small_capital_validation_review"


def test_small_capital_review_inherits_paper_reliability_gate(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        small_capital_validation_review.paper_trading_validation,
        "build_report",
        return_value=_paper_report_reliability_fail(),
    ):
        report = small_capital_validation_review.build_report(
            db_path,
            pilot_cities=["chengdu"],
            since_days=42,
        )

    assert report["ok"] is False
    assert "paper_validation_not_passed:chengdu" in report["blockers"]
    city_report = report["city_reports"][0]
    assert "paper_reliability_brier_above_max" in city_report["blockers"]
    assert city_report["paper_status"]["reliability_brier"] == 0.31


def test_small_capital_review_surfaces_gap_paper_candidates_without_enabling_money(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        small_capital_validation_review.paper_trading_validation,
        "build_report",
        return_value=_paper_report(False),
    ), patch.object(
        small_capital_validation_review,
        "_latest_gap_event_dates",
        return_value=["2026-05-31", "2026-06-01"],
    ), patch.object(
        small_capital_validation_review.tradeability_gap_review,
        "build_review",
        return_value=_gap_review(),
    ):
        report = small_capital_validation_review.build_report(
            db_path,
            pilot_cities=["chengdu"],
            since_days=42,
        )

    assert report["ok"] is False
    assert "paper_validation_not_passed:chengdu" in report["blockers"]
    assert "paper_pilot_candidates_pending_review:chengdu" in report["warnings"]
    review = report["pilot_candidate_review"]
    assert review["paper_observation_allowed"] is True
    assert review["small_capital_candidate_ready"] is False
    city = review["by_city"]["chengdu"]
    assert city["paper_observation_candidates"] == 2
    assert city["small_capital_candidates"] == 0
    assert city["by_review_action"]["paper_track_reverse_edge_no_side"] == 1
    assert city["by_candidate_strategy"]["reverse_edge_buy_no_sell_yes"] == 1
    assert "review_tradeability_gap_paper_candidates_for_listed_pilot_cities" in report["next_actions"]


def test_small_capital_review_defaults_to_latest_objective_pilot_cities(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    objective_path = tmp_path / "objective.json"
    objective_path.write_text(
        '{"pilot_cities":["chongqing","ankara","london"]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        small_capital_validation_review,
        "DEFAULT_OBJECTIVE_AUDIT_REPORT",
        objective_path,
    )
    monkeypatch.setattr(
        small_capital_validation_review,
        "DEFAULT_PILOT_CITY_MATRIX_REPORT",
        tmp_path / "missing_matrix.json",
    )

    with patch.object(
        small_capital_validation_review.paper_trading_validation,
        "build_report",
        return_value=_paper_report(True),
    ):
        report = small_capital_validation_review.build_report(
            db_path,
            pilot_cities=[],
            since_days=42,
        )

    assert report["pilot_scope"]["cities"] == ["chongqing", "ankara"]
    assert report["pilot_scope"]["source"] == "objective_audit_latest"
    assert report["pilot_scope"]["major_cities"] == []


def test_small_capital_review_falls_back_to_city_matrix_when_objective_missing(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        """
        {
          "cities": [
            {"city": "seoul", "blockers": ["hit_rate_below_min"]},
            {"city": "buenos aires", "blockers": ["resolved_rows_below_min"]},
            {"city": "london", "blockers": []},
            {"city": "atlanta", "blockers": []}
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        small_capital_validation_review,
        "DEFAULT_OBJECTIVE_AUDIT_REPORT",
        tmp_path / "missing_objective.json",
    )
    monkeypatch.setattr(
        small_capital_validation_review,
        "DEFAULT_PILOT_CITY_MATRIX_REPORT",
        matrix_path,
    )

    with patch.object(
        small_capital_validation_review.paper_trading_validation,
        "build_report",
        return_value=_paper_report(True),
    ):
        report = small_capital_validation_review.build_report(
            db_path,
            pilot_cities=[],
            since_days=42,
        )

    assert report["pilot_scope"]["cities"] == ["buenos-aires", "atlanta"]
    assert report["pilot_scope"]["source"] == "pilot_city_validation_matrix_latest"
