from __future__ import annotations

from pathlib import Path

from scripts import paper_observation_target_package as targets


def test_target_package_uses_dynamic_default_event_dates(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        targets.pilot_city_validation_matrix,
        "default_event_dates",
        lambda: ["2026-06-02", "2026-06-03"],
    )

    def fake_build_report(*args, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "city_count": 0, "cities": []}

    monkeypatch.setattr(targets.pilot_city_validation_matrix, "build_report", fake_build_report)

    package = targets.build_package(tmp_path / "archive.db")

    assert captured["event_dates"] == ["2026-06-02", "2026-06-03"]
    assert package["event_dates"] == ["2026-06-02", "2026-06-03"]


def test_target_package_prioritizes_strategy_deficits(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        targets.pilot_city_validation_matrix,
        "build_report",
        lambda *args, **kwargs: {
            "ok": False,
            "city_count": 1,
            "cities": [
                {
                    "city": "chongqing",
                    "ok": False,
                    "resolved_rows": 5,
                    "pending_rows": 0,
                    "wins": 3,
                    "losses": 2,
                    "hit_rate": 0.6,
                    "blockers": ["resolved_rows_below_min"],
                    "by_candidate_strategy": {
                        "d0_physical_settlement_hybrid": {
                            "resolved_rows": 4,
                            "pending_rows": 0,
                            "wins": 2,
                            "losses": 2,
                            "hit_rate": 0.5,
                        },
                        "reverse_edge_buy_no_sell_yes": {
                            "resolved_rows": 1,
                            "pending_rows": 0,
                            "wins": 1,
                            "losses": 0,
                            "hit_rate": 1.0,
                        },
                    },
                }
            ],
        },
    )

    package = targets.build_package(tmp_path / "archive.db", cities=["chongqing"])

    assert package["next_mainline_task"] == "collect_targeted_paper_observation_samples"
    assert package["city_targets"][0]["needed_resolved_rows"] == 5
    by_strategy = {item["target_strategy"]: item for item in package["strategy_targets"]}
    assert by_strategy["reverse_edge_buy_no_sell_yes"]["needed_resolved_rows"] == 2
    assert by_strategy["d0_physical_settlement_hybrid"]["needed_additional_wins_for_hit_rate"] == 1


def test_target_package_keeps_expansion_cities_separate(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_build_report(*args, **kwargs):
        calls.append(dict(kwargs))
        cities = kwargs.get("cities") or []
        if cities:
            city_rows = [
                {
                    "city": "chongqing",
                    "ok": False,
                    "resolved_rows": 5,
                    "pending_rows": 0,
                    "wins": 3,
                    "losses": 2,
                    "hit_rate": 0.6,
                    "rows": 5,
                    "blockers": ["resolved_rows_below_min"],
                    "by_candidate_strategy": {},
                }
            ]
        else:
            city_rows = [
                {
                    "city": "chongqing",
                    "ok": False,
                    "resolved_rows": 5,
                    "pending_rows": 0,
                    "wins": 3,
                    "losses": 2,
                    "hit_rate": 0.6,
                    "rows": 5,
                    "blockers": ["resolved_rows_below_min"],
                    "by_candidate_strategy": {},
                },
                {
                    "city": "buenos aires",
                    "ok": False,
                    "resolved_rows": 1,
                    "pending_rows": 1,
                    "wins": 1,
                    "losses": 0,
                    "hit_rate": 1.0,
                    "rows": 2,
                    "blockers": ["resolved_rows_below_min"],
                    "by_candidate_strategy": {},
                },
                {
                    "city": "seoul",
                    "ok": False,
                    "resolved_rows": 4,
                    "pending_rows": 0,
                    "wins": 2,
                    "losses": 2,
                    "hit_rate": 0.5,
                    "rows": 4,
                    "blockers": ["hit_rate_below_min", "resolved_rows_below_min"],
                    "by_candidate_strategy": {},
                },
            ]
        return {"ok": False, "city_count": len(city_rows), "cities": city_rows}

    monkeypatch.setattr(targets.pilot_city_validation_matrix, "build_report", fake_build_report)

    package = targets.build_package(tmp_path / "archive.db", cities=["chongqing"])

    assert [item["city"] for item in package["city_targets"]] == ["chongqing"]
    assert [item["city"] for item in package["observation_expansion_city_targets"]] == [
        "buenos aires",
        "seoul",
    ]
    assert [item["city"] for item in package["recommended_pilot_city_targets"]] == [
        "buenos aires"
    ]
    assert package["recommended_pilot_city_targets"][0]["observation_mode"] == (
        "paper_only_not_small_capital"
    )
    assert calls[0]["cities"] == ["chongqing"]
    assert calls[1]["cities"] == ()


def test_target_package_prioritizes_objective_secondary_expansion_cities(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_build_report(*args, **kwargs):
        requested = kwargs.get("cities") or []
        if requested:
            city_rows = [
                {
                    "city": "chongqing",
                    "ok": False,
                    "resolved_rows": 5,
                    "pending_rows": 0,
                    "wins": 3,
                    "losses": 2,
                    "hit_rate": 0.6,
                    "rows": 5,
                    "blockers": ["resolved_rows_below_min"],
                    "by_candidate_strategy": {},
                }
            ]
        else:
            city_rows = [
                {
                    "city": "toronto",
                    "ok": False,
                    "resolved_rows": 0,
                    "pending_rows": 4,
                    "wins": 0,
                    "losses": 0,
                    "hit_rate": None,
                    "rows": 4,
                    "blockers": ["hit_rate_missing"],
                    "by_candidate_strategy": {},
                },
                {
                    "city": "atlanta",
                    "ok": False,
                    "resolved_rows": 0,
                    "pending_rows": 1,
                    "wins": 0,
                    "losses": 0,
                    "hit_rate": None,
                    "rows": 1,
                    "blockers": ["hit_rate_missing"],
                    "by_candidate_strategy": {},
                },
            ]
        return {"ok": False, "city_count": len(city_rows), "cities": city_rows}

    monkeypatch.setattr(targets.pilot_city_validation_matrix, "build_report", fake_build_report)
    monkeypatch.setenv("PM_OBSERVATION_EXPANSION_PRIORITY_CITIES", "atlanta")

    package = targets.build_package(
        tmp_path / "archive.db",
        cities=["chongqing"],
        max_cities=1,
    )

    assert [item["city"] for item in package["observation_expansion_city_targets"]] == [
        "atlanta"
    ]


def test_markdown_contains_city_and_strategy_targets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        targets.pilot_city_validation_matrix,
        "build_report",
        lambda *args, **kwargs: {
            "ok": False,
            "city_count": 1,
            "cities": [
                {
                    "city": "chongqing",
                    "ok": False,
                    "resolved_rows": 1,
                    "pending_rows": 2,
                    "wins": 1,
                    "losses": 0,
                    "hit_rate": 1.0,
                    "blockers": ["resolved_rows_below_min"],
                    "by_candidate_strategy": {
                        "reverse_edge_buy_no_sell_yes": {
                            "resolved_rows": 1,
                            "pending_rows": 2,
                            "wins": 1,
                            "losses": 0,
                            "hit_rate": 1.0,
                        },
                    },
                }
            ],
        },
    )

    package = targets.build_package(tmp_path / "archive.db")
    md = targets.format_markdown(package)

    assert "chongqing" in md
    assert "reverse_edge_buy_no_sell_yes" in md
    assert "prioritize_reverse_no_tail_candidates" in md
