from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.tradeability_gap_review import build_review
from tests.test_weather_strategy_batch import _init_db


def _insert_decision(path: Path, row_id: int, slug: str, details: dict, *, side: str = "YES") -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, run_id, run_date, slug, event_date, title, outcome, side,
              action, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (?, 1, '2026-05-31', ?, '2026-05-31', 'Highest temperature?',
                    '31°C', ?, 'entry', 0, '[]', '[]', ?, ?)
            """,
            (
                row_id,
                slug,
                side,
                json.dumps(details),
                f"2026-05-31T0{row_id}:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_tradeability_gap_review_surfaces_paper_candidate(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    _insert_decision(
        db_path,
        3,
        "highest-temperature-in-istanbul-on-may-31-2026",
        {
            "q_entry": 0.62,
            "q_bayes_lcb": 0.60,
            "q_bayes_posterior": 0.66,
            "ask": 0.48,
            "bid": 0.46,
            "spread": 0.02,
            "q_bayes_z_stage": "d0_morning",
            "q_bayes_physical_likelihood_enabled": True,
            "q_bayes_physical_likelihood_evidence": 12.0,
            "q_bayes_physical_likelihood_source": "weather_physical_likelihood",
            "weather_likelihood_feature_count": 40,
            "source_fit_tier": "settlement_source",
            "station_id": "LTBA",
            "resolution_source": "https://www.wunderground.com/history/daily/tr/istanbul/LTBA",
            "source_station_id": "LTBA",
            "source_station_match": True,
        },
    )

    review = build_review(db_path, ["2026-05-31"], limit=10)

    assert review["gap_rows"] == 1
    assert review["by_gap_reason"]["paper_review_candidate"] == 1
    assert review["by_review_action"]["paper_track_d0_physical_or_settlement"] == 1
    assert review["by_candidate_strategy"]["d0_physical_settlement_hybrid"] == 1
    row = review["rows"][0]
    assert row["slug"] == "highest-temperature-in-istanbul-on-may-31-2026"
    assert row["raw_lcb_edge"] == 0.12
    assert row["candidate_strategy"] == "d0_physical_settlement_hybrid"
    assert row["execution_direction"] == "buy_yes"
    assert row["review_action"] == "paper_track_d0_physical_or_settlement"
    assert row["entry_probability_used"] == "q_bayes_lcb"
    assert row["exit_probability_reference"] == (
        "q_bayes_posterior_mean_then_hold_edge_realizable_for_open_positions"
    )
    assert row["risk_discount_positive"] is True
    assert row["counterfactual_entry_usable_edge"] > 0
    assert row["counterfactual_entry_usable_edge_size_scale"] > 0


def test_tradeability_gap_review_flags_missing_station(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    _insert_decision(
        db_path,
        3,
        "highest-temperature-in-istanbul-on-may-31-2026",
        {
            "q_entry": 0.62,
            "q_bayes_lcb": 0.60,
            "q_bayes_posterior": 0.66,
            "ask": 0.48,
            "bid": 0.46,
            "spread": 0.02,
            "q_bayes_z_stage": "d0_morning",
            "q_bayes_physical_likelihood_enabled": True,
            "weather_likelihood_feature_count": 40,
        },
    )

    review = build_review(db_path, ["2026-05-31"], limit=10)

    assert review["gap_rows"] == 1
    assert review["by_gap_reason"]["station_missing"] == 1
    assert review["by_review_action"]["exclude_until_tradeability_gap_fixed"] == 1
    assert review["rows"][0]["gap_reason"] == "station_missing"
    assert review["rows"][0]["review_action"] == "exclude_until_tradeability_gap_fixed"


def test_tradeability_gap_review_marks_tail_no_reverse_edge(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    _insert_decision(
        db_path,
        3,
        "highest-temperature-in-miami-on-may-31-2026",
        {
            "q_entry": 0.72,
            "q_bayes_lcb": 0.64,
            "q_bayes_posterior": 0.70,
            "ask": 0.49,
            "bid": 0.47,
            "spread": 0.02,
            "q_bayes_z_stage": "d0_morning",
            "q_bayes_physical_likelihood_enabled": True,
            "q_bayes_physical_likelihood_evidence": 14.0,
            "q_bayes_physical_likelihood_source": "weather_physical_likelihood",
            "weather_likelihood_feature_count": 42,
            "source_fit_tier": "settlement_source",
            "station_id": "KMIA",
            "resolution_source": "https://www.wunderground.com/history/daily/us/fl/miami/KMIA",
            "source_station_id": "KMIA",
            "source_station_match": True,
            "weather_strategy_reverse_edge": True,
            "weather_strategy_tail_no": True,
        },
        side="NO",
    )

    review = build_review(db_path, ["2026-05-31"], limit=10)

    assert review["gap_rows"] == 1
    assert review["by_review_action"]["paper_track_reverse_edge_no_side"] == 1
    assert review["by_candidate_strategy"]["reverse_edge_buy_no_sell_yes"] == 1
    row = review["rows"][0]
    assert row["candidate_strategy"] == "reverse_edge_buy_no_sell_yes"
    assert row["execution_direction"] == "buy_no_equivalent_to_sell_yes"
    assert row["review_action"] == "paper_track_reverse_edge_no_side"


def test_tradeability_gap_review_excludes_settled_or_past_from_paper_tracking(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    _init_db(db_path)
    _insert_decision(
        db_path,
        3,
        "highest-temperature-in-chongqing-on-may-31-2026",
        {
            "q_entry": 0.72,
            "q_bayes_lcb": 0.64,
            "q_bayes_posterior": 0.70,
            "ask": 0.49,
            "bid": 0.47,
            "spread": 0.02,
            "q_bayes_z_stage": "settled_or_past",
            "q_bayes_physical_likelihood_enabled": True,
            "q_bayes_physical_likelihood_evidence": 14.0,
            "weather_likelihood_feature_count": 42,
            "source_fit_tier": "settlement_source",
            "station_id": "ZUCK",
            "resolution_source": "https://www.wunderground.com/history/daily/cn/chongqing/ZUCK",
            "source_station_id": "ZUCK",
            "source_station_match": True,
        },
    )

    review = build_review(db_path, ["2026-05-31"], limit=10)

    assert review["gap_rows"] == 1
    assert review["by_gap_reason"]["market_settled_or_past_no_new_entry"] == 1
    assert review["by_review_action"]["exclude_until_tradeability_gap_fixed"] == 1
    assert review["rows"][0]["gap_reason"] == "market_settled_or_past_no_new_entry"
    assert review["rows"][0]["review_action"] == "exclude_until_tradeability_gap_fixed"
