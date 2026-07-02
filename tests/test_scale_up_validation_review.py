from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from scripts import scale_up_validation_review


def _small_cap_report(ok: bool = True, *, secondary_only: bool = True) -> dict:
    return {
        "ok": ok,
        "blockers": [] if ok else ["paper_validation_not_passed:istanbul"],
        "warnings": [],
        "since_days": 42,
        "validation_window": {
            "min_days": 28,
            "max_recommended_days": 42,
            "within_required_window": True,
        },
        "pilot_scope": {
            "cities": ["istanbul"],
            "min_cities": 1,
            "max_cities": 2,
            "major_cities": [] if secondary_only else ["miami"],
            "secondary_only": secondary_only,
        },
        "small_capital_caps": {
            "max_per_order_usdc": 5.0,
            "max_daily_usdc": 25.0,
        },
    }


def test_scale_up_review_passes_after_small_capital_validation(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        scale_up_validation_review.small_capital_validation_review,
        "build_report",
        return_value=_small_cap_report(True),
    ), patch.object(
        scale_up_validation_review.paper_observation_rolling_validation,
        "build_report",
        return_value={"ok": True, "blockers": [], "warnings": []},
    ):
        report = scale_up_validation_review.build_report(
            db_path,
            pilot_cities=["istanbul"],
            since_days=42,
            max_scale_per_order_usdc=25.0,
            max_scale_daily_usdc=100.0,
        )

    assert report["ok"] is True
    assert report["blockers"] == []
    assert report["next_mainline_task"] == "limited_scale_up_enable_review"


def test_scale_up_review_blocks_when_small_capital_not_passed(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        scale_up_validation_review.small_capital_validation_review,
        "build_report",
        return_value=_small_cap_report(False),
    ), patch.object(
        scale_up_validation_review.paper_observation_rolling_validation,
        "build_report",
        return_value={"ok": True, "blockers": [], "warnings": []},
    ):
        report = scale_up_validation_review.build_report(
            db_path,
            pilot_cities=["istanbul"],
            since_days=42,
        )

    assert report["ok"] is False
    assert "small_capital_validation_not_passed" in report["blockers"]
    assert "small_cap:paper_validation_not_passed:istanbul" in report["blockers"]


def test_scale_up_review_blocks_major_city_pilot(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        scale_up_validation_review.small_capital_validation_review,
        "build_report",
        return_value=_small_cap_report(True, secondary_only=False),
    ), patch.object(
        scale_up_validation_review.paper_observation_rolling_validation,
        "build_report",
        return_value={"ok": True, "blockers": [], "warnings": []},
    ):
        report = scale_up_validation_review.build_report(
            db_path,
            pilot_cities=["miami"],
            since_days=42,
        )

    assert report["ok"] is False
    assert "scale_up_requires_secondary_pilot_only" in report["blockers"]


def test_scale_up_review_blocks_oversized_jump(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        scale_up_validation_review.small_capital_validation_review,
        "build_report",
        return_value=_small_cap_report(True),
    ), patch.object(
        scale_up_validation_review.paper_observation_rolling_validation,
        "build_report",
        return_value={"ok": True, "blockers": [], "warnings": []},
    ):
        report = scale_up_validation_review.build_report(
            db_path,
            pilot_cities=["istanbul"],
            since_days=42,
            max_scale_per_order_usdc=200.0,
            max_scale_daily_usdc=1000.0,
        )

    assert report["ok"] is False
    assert "scale_up_per_order_jump_too_large" in report["blockers"]
    assert "scale_up_daily_jump_too_large" in report["blockers"]


def test_scale_up_review_requires_observation_rolling_validation(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        scale_up_validation_review.small_capital_validation_review,
        "build_report",
        return_value=_small_cap_report(True),
    ), patch.object(
        scale_up_validation_review.paper_observation_rolling_validation,
        "build_report",
        return_value={
            "ok": False,
            "blockers": ["paper_observation_resolved_rows_below_min"],
            "warnings": ["paper_observation_pending_labels"],
        },
    ):
        report = scale_up_validation_review.build_report(
            db_path,
            pilot_cities=["istanbul"],
            since_days=42,
        )

    assert report["ok"] is False
    assert "paper_observation_validation_not_passed" in report["blockers"]
    assert "paper_observation:paper_observation_resolved_rows_below_min" in report["blockers"]
    assert "paper_observation:paper_observation_pending_labels" in report["warnings"]
