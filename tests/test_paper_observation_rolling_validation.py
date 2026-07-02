from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from scripts import paper_observation_rolling_validation


def _tracking_report(labels: list[int | None]) -> dict:
    rows = []
    for idx, label in enumerate(labels, start=1):
        rows.append(
            {
                "candidate_strategy": "reverse_edge_buy_no_sell_yes",
                "review_action": "paper_track_reverse_edge_no_side",
                "tracking_status": "pending_settlement_label" if label is None else "labeled",
                "label": label,
                "risk_edge": 0.12,
                "raw_lcb_edge": 0.15,
                "slug": f"highest-temperature-in-toronto-on-may-{idx}-2026",
            }
        )
    return {
        "ok": False,
        "pilot_scope": {"cities": ["toronto"], "secondary_only": True},
        "summary": {
            "candidate_rows": len(rows),
        },
        "rows": rows,
    }


def test_observation_rolling_blocks_low_resolved_sample(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        paper_observation_rolling_validation.paper_observation_tracking_report,
        "build_report",
        return_value=_tracking_report([1, None]),
    ):
        report = paper_observation_rolling_validation.build_report(
            db_path,
            pilot_cities=["toronto"],
            min_resolved_rows=3,
            min_strategy_resolved_rows=2,
        )

    assert report["ok"] is False
    assert "paper_observation_resolved_rows_below_min" in report["blockers"]
    assert "strategy_resolved_rows_below_min:reverse_edge_buy_no_sell_yes" in report["blockers"]
    assert "paper_observation_pending_labels" in report["warnings"]
    assert report["overall"]["resolved_rows"] == 1
    assert report["overall"]["pending_rows"] == 1


def test_observation_rolling_passes_when_strategy_sample_and_hit_rate_pass(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        paper_observation_rolling_validation.paper_observation_tracking_report,
        "build_report",
        return_value=_tracking_report([1, 1, 0, 1]),
    ):
        report = paper_observation_rolling_validation.build_report(
            db_path,
            pilot_cities=["toronto"],
            min_resolved_rows=4,
            min_strategy_resolved_rows=3,
            min_hit_rate=0.55,
        )

    assert report["ok"] is True
    assert report["blockers"] == []
    assert report["overall"]["hit_rate"] == 0.75
    assert report["by_candidate_strategy"]["reverse_edge_buy_no_sell_yes"]["resolved_rows"] == 4


def test_observation_rolling_blocks_low_hit_rate(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")

    with patch.object(
        paper_observation_rolling_validation.paper_observation_tracking_report,
        "build_report",
        return_value=_tracking_report([0, 0, 1, 0]),
    ):
        report = paper_observation_rolling_validation.build_report(
            db_path,
            pilot_cities=["toronto"],
            min_resolved_rows=4,
            min_strategy_resolved_rows=3,
            min_hit_rate=0.55,
        )

    assert report["ok"] is False
    assert "paper_observation_hit_rate_below_min" in report["blockers"]
    assert "strategy_hit_rate_below_min:reverse_edge_buy_no_sell_yes" in report["blockers"]


def test_observation_rolling_excludes_negative_ev_and_non_track_rows_from_reference_stats(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    tracking = _tracking_report([1, 0, 1])
    tracking["rows"][1]["risk_edge"] = -0.02
    tracking["rows"][2]["review_action"] = "exclude_until_tradeability_gap_fixed"

    with patch.object(
        paper_observation_rolling_validation.paper_observation_tracking_report,
        "build_report",
        return_value=tracking,
    ):
        report = paper_observation_rolling_validation.build_report(
            db_path,
            pilot_cities=["toronto"],
            min_resolved_rows=1,
            min_strategy_resolved_rows=1,
            min_hit_rate=0.55,
        )

    assert report["reference_filter"]["included_rows"] == 1
    assert report["reference_filter"]["excluded_rows"] == 2
    assert report["overall"]["resolved_rows"] == 1
    assert report["overall"]["wins"] == 1
    assert report["overall"]["hit_rate"] == 1.0
