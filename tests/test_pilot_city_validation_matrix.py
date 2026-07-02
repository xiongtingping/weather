from __future__ import annotations

from pathlib import Path

from scripts import pilot_city_validation_matrix as matrix


def test_default_event_dates_are_et_today_and_tomorrow(monkeypatch) -> None:
    class FixedDatetime(matrix.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return cls(2026, 6, 2, 3, 30, tzinfo=tz)

    monkeypatch.setattr(matrix, "datetime", FixedDatetime)

    assert matrix.default_event_dates() == ["2026-06-02", "2026-06-03"]


def test_matrix_groups_paper_candidates_by_city_and_scores_thresholds(monkeypatch, tmp_path: Path) -> None:
    rows = [
        {
            "slug": "highest-temperature-in-chongqing-on-may-31-2026",
            "event_date": "2026-05-31",
            "outcome": "31°C",
            "side": "NO",
            "review_action": "paper_track_reverse_edge_no_side",
            "candidate_strategy": "reverse_edge_buy_no_sell_yes",
        },
        {
            "slug": "highest-temperature-in-chongqing-on-may-31-2026",
            "event_date": "2026-05-31",
            "outcome": "29°C",
            "side": "YES",
            "review_action": "paper_track_d0_physical_or_settlement",
            "candidate_strategy": "d0_physical_settlement_hybrid",
        },
        {
            "slug": "highest-temperature-in-seoul-on-may-31-2026",
            "event_date": "2026-05-31",
            "outcome": "25°C",
            "side": "YES",
            "review_action": "paper_track_d0_physical_or_settlement",
            "candidate_strategy": "d0_physical_settlement_hybrid",
        },
    ]

    monkeypatch.setattr(
        matrix.tradeability_gap_review,
        "build_review",
        lambda *args, **kwargs: {"ok": True, "rows": rows},
    )
    labels = {
        (
            "highest-temperature-in-chongqing-on-may-31-2026",
            "2026-05-31",
            "31°C",
            "NO",
        ): {"label": 1, "label_source": "test"},
        (
            "highest-temperature-in-chongqing-on-may-31-2026",
            "2026-05-31",
            "29°C",
            "YES",
        ): {"label": 1, "label_source": "test"},
        (
            "highest-temperature-in-seoul-on-may-31-2026",
            "2026-05-31",
            "25°C",
            "YES",
        ): {"label": 0, "label_source": "test"},
    }
    monkeypatch.setattr(matrix.paper_observation_tracking_report, "_load_calibration_labels", lambda *args, **kwargs: labels)
    monkeypatch.setattr(matrix.paper_observation_tracking_report, "_load_settlements", lambda *args, **kwargs: {})

    report = matrix.build_report(
        tmp_path / "archive.db",
        event_dates=["2026-05-31"],
        min_resolved_rows=2,
        min_strategy_resolved_rows=1,
        min_hit_rate=0.55,
    )

    assert report["city_count"] == 2
    assert report["cities"][0]["city"] == "chongqing"
    assert report["cities"][0]["ok"] is True
    assert report["cities"][0]["hit_rate"] == 1.0
    assert report["cities"][1]["city"] == "seoul"
    assert "resolved_rows_below_min" in report["cities"][1]["blockers"]


def test_matrix_filters_requested_cities(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        matrix.tradeability_gap_review,
        "build_review",
        lambda *args, **kwargs: {
            "ok": True,
            "rows": [
                {
                    "slug": "highest-temperature-in-chongqing-on-may-31-2026",
                    "event_date": "2026-05-31",
                    "outcome": "31°C",
                    "side": "NO",
                    "review_action": "paper_track_reverse_edge_no_side",
                    "candidate_strategy": "reverse_edge_buy_no_sell_yes",
                },
                {
                    "slug": "highest-temperature-in-seoul-on-may-31-2026",
                    "event_date": "2026-05-31",
                    "outcome": "25°C",
                    "side": "YES",
                    "review_action": "paper_track_d0_physical_or_settlement",
                    "candidate_strategy": "d0_physical_settlement_hybrid",
                },
            ],
        },
    )
    monkeypatch.setattr(matrix.paper_observation_tracking_report, "_load_calibration_labels", lambda *args, **kwargs: {})
    monkeypatch.setattr(matrix.paper_observation_tracking_report, "_load_settlements", lambda *args, **kwargs: {})

    report = matrix.build_report(tmp_path / "archive.db", cities=["seoul"])

    assert [row["city"] for row in report["cities"]] == ["seoul"]
