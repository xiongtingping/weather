from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from scripts import pilot_observation_review


def _small_cap_report() -> dict:
    return {
        "ok": False,
        "blockers": ["paper_validation_not_passed:chongqing"],
        "warnings": ["paper_pilot_candidates_pending_review:chongqing"],
        "pilot_scope": {
            "cities": ["chongqing", "toronto"],
            "secondary_only": True,
        },
        "pilot_candidate_review": {
            "event_dates": ["2026-06-01", "2026-05-31"],
            "paper_observation_allowed": True,
            "small_capital_candidate_ready": False,
            "by_city": {
                "chongqing": {
                    "paper_observation_candidates": 1,
                    "small_capital_candidates": 0,
                    "by_review_action": {"paper_track_d0_physical_or_settlement": 1},
                    "by_candidate_strategy": {"d0_physical_settlement_hybrid": 1},
                    "rows": [
                        {
                            "event_date": "2026-05-31",
                            "slug": "highest-temperature-in-chongqing-on-may-31-2026",
                            "outcome": "29°C",
                            "side": "YES",
                            "candidate_strategy": "d0_physical_settlement_hybrid",
                            "execution_direction": "buy_yes",
                            "review_action": "paper_track_d0_physical_or_settlement",
                            "gap_reason": "paper_review_candidate",
                            "raw_lcb_edge": 0.292,
                            "counterfactual_entry_usable_edge": 0.29,
                            "counterfactual_entry_usable_edge_size_scale": 1.0,
                            "ask": 0.22,
                            "bid": 0.20,
                            "q_bayes_lcb": 0.512,
                            "q_bayes_posterior": 0.55,
                            "station_id": "ZUCK",
                            "source_fit_tier": "settlement_source",
                            "entry_probability_used": "q_bayes_lcb",
                            "exit_probability_reference": "q_bayes_posterior_mean_then_hold_edge_realizable_for_open_positions",
                        }
                    ],
                },
                "toronto": {
                    "paper_observation_candidates": 1,
                    "small_capital_candidates": 0,
                    "by_review_action": {"paper_track_reverse_edge_no_side": 1},
                    "by_candidate_strategy": {"reverse_edge_buy_no_sell_yes": 1},
                    "rows": [
                        {
                            "event_date": "2026-05-31",
                            "slug": "highest-temperature-in-toronto-on-may-31-2026",
                            "outcome": "24°C",
                            "side": "NO",
                            "candidate_strategy": "reverse_edge_buy_no_sell_yes",
                            "execution_direction": "buy_no_equivalent_to_sell_yes",
                            "review_action": "paper_track_reverse_edge_no_side",
                            "gap_reason": "paper_review_candidate",
                            "raw_lcb_edge": 0.213,
                            "counterfactual_entry_usable_edge": 0.183,
                            "counterfactual_entry_usable_edge_size_scale": 1.0,
                            "ask": 0.69,
                            "bid": 0.64,
                            "q_bayes_lcb": 0.903,
                            "q_bayes_posterior": 0.934,
                            "station_id": "CYYZ",
                            "source_fit_tier": "settlement_source",
                            "entry_probability_used": "q_bayes_lcb",
                            "exit_probability_reference": "q_bayes_posterior_mean_then_hold_edge_realizable_for_open_positions",
                        }
                    ],
                },
            },
        },
    }


def test_pilot_observation_review_flattens_city_candidates(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    with patch.object(
        pilot_observation_review.small_capital_validation_review,
        "build_report",
        return_value=_small_cap_report(),
    ):
        report = pilot_observation_review.build_report(
            db_path,
            pilot_cities=["chongqing", "toronto"],
        )

    assert report["ok"] is False
    assert report["paper_observation_allowed"] is True
    assert report["small_capital_candidate_ready"] is False
    assert report["city_summary"]["chongqing"]["paper_observation_candidates"] == 1
    assert report["city_summary"]["toronto"]["by_candidate_strategy"][
        "reverse_edge_buy_no_sell_yes"
    ] == 1
    assert len(report["rows"]) == 2
    reverse = [row for row in report["rows"] if row["city"] == "toronto"][0]
    assert reverse["execution_direction"] == "buy_no_equivalent_to_sell_yes"
    assert reverse["risk_edge"] == 0.183


def test_pilot_observation_markdown_includes_edges_and_blockers(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    with patch.object(
        pilot_observation_review.small_capital_validation_review,
        "build_report",
        return_value=_small_cap_report(),
    ):
        report = pilot_observation_review.build_report(
            db_path,
            pilot_cities=["chongqing", "toronto"],
        )

    text = pilot_observation_review.format_markdown(report)

    assert "paper_validation_not_passed:chongqing" in text
    assert "| toronto | 1 | 0 | 1 | 0 | 0 |" in text
    assert "buy_no_equivalent_to_sell_yes" in text
    assert "21.3%" in text
    assert "18.3%" in text
