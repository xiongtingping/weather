from __future__ import annotations

import json
from pathlib import Path

from scripts import recommended_pilot_gap_scan_plan as plan


def _audit_payload() -> dict:
    return {
        "generated_at": "2026-06-01T00:00:00+00:00",
        "current_strategy_batch_summary": {
            "targeted_candidate_status": {
                "recommended_pilot_settlement_readiness": {
                    "by_city": {
                        "buenos-aires": {
                            "eligibility_markets_preview": [
                                {
                                    "slug": "highest-temperature-in-buenos-aires-on-june-1-2026",
                                    "event_date": "2026-06-01",
                                    "status": "settlement_not_closed",
                                },
                                {
                                    "slug": "highest-temperature-in-buenos-aires-on-may-31-2026",
                                    "event_date": "2026-05-31",
                                    "status": "no_unlabeled_parseable_decisions",
                                },
                            ]
                        }
                    }
                },
                "recommended_pilot_observation_validation": {
                    "ok": False,
                    "sample_gap_queue": [
                        {
                            "city": "buenos-aires",
                            "observation_mode": "paper_only_not_small_capital",
                            "resolved_rows": 1,
                            "needed_resolved_rows": 9,
                            "pending_rows": 0,
                            "wins": 1,
                            "hit_rate": 1.0,
                            "strategy_gaps": [
                                {
                                    "target_strategy": "reverse_edge_buy_no_sell_yes",
                                    "resolved_rows": 1,
                                    "needed_resolved_rows": 2,
                                    "scan_directive": "scan_open_tail_no_reverse_edge_candidates",
                                }
                            ],
                        }
                    ],
                }
            }
        }
    }


def test_gap_scan_plan_surfaces_recommended_pilot_strategy_gap(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(_audit_payload()), encoding="utf-8")

    report = plan.build_plan(audit_path, market_scope="highest_temperature")

    assert report["ok"] is False
    assert report["queue_count"] == 1
    assert report["city_count"] == 1
    assert report["strategy_gap_count"] == 1
    assert report["priority_slug_count"] == 1
    assert report["priority_slugs"] == [
        "highest-temperature-in-buenos-aires-on-june-1-2026"
    ]
    assert report["blockers"] == ["recommended_pilot_sample_gap_open"]
    assert report["audit_generated_at"] == "2026-06-01T00:00:00+00:00"
    assert report["target_package_generated_at"] == ""
    target = report["plan"][0]
    assert target["city"] == "buenos-aires"
    assert target["needed_resolved_rows"] == 9
    assert target["strategy_gaps"][0]["target_strategy"] == "reverse_edge_buy_no_sell_yes"
    assert target["strategy_gaps"][0]["needed_resolved_rows"] == 2
    assert target["scan_directives"] == ["scan_open_tail_no_reverse_edge_candidates"]
    assert target["open_market_slugs"] == [
        "highest-temperature-in-buenos-aires-on-june-1-2026"
    ]
    assert target["safety"]["paper_only"] is True
    assert report["next_action"] == "prioritize_recommended_pilot_gap_scan_candidates"


def test_gap_scan_plan_city_filter_can_exclude_non_matching_city(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(_audit_payload()), encoding="utf-8")

    report = plan.build_plan(audit_path, cities=["chongqing"])

    assert report["ok"] is True
    assert report["queue_count"] == 0
    assert report["city_filter"] == ["chongqing"]
    assert report["next_action"] == "recommended_pilot_sample_gaps_closed"


def test_gap_scan_plan_falls_back_to_open_market_tokens_when_readiness_missing(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"
    audit = {
        "event_dates": ["2026-06-01"],
        "current_strategy_batch_summary": {
            "targeted_candidate_status": {
                "recommended_pilot_observation_validation": {
                    "ok": False,
                    "sample_gap_queue": [
                        {
                            "city": "ankara",
                            "resolved_rows": 0,
                            "needed_resolved_rows": 10,
                            "pending_rows": 3,
                            "wins": 0,
                            "hit_rate": None,
                            "strategy_gaps": [
                                {
                                    "target_strategy": "reverse_edge_buy_no_sell_yes",
                                    "resolved_rows": 0,
                                    "needed_resolved_rows": 3,
                                    "scan_directive": "scan_open_tail_no_reverse_edge_candidates",
                                }
                            ],
                        }
                    ],
                },
                "recommended_pilot_settlement_readiness": {"by_city": {}},
            }
        },
    }
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    db_path = tmp_path / "archive.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE market_tokens (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            market_active INTEGER,
            market_closed INTEGER,
            market_archived INTEGER,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO market_tokens(
            slug, event_date, outcome, market_active, market_closed, market_archived, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "highest-temperature-in-ankara-on-june-1-2026",
            "2026-06-01",
            "18C",
            1,
            0,
            0,
            "2026-06-01T02:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(
        audit_path,
        db_path=db_path,
        include_expansion_candidates=True,
    )

    assert report["priority_slugs"] == [
        "highest-temperature-in-ankara-on-june-1-2026"
    ]
    assert report["plan"][0]["open_markets"][0]["status"] == "open_market_fallback"


def test_gap_scan_plan_includes_target_pilot_gaps_with_matrix_stats(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"
    audit = {
        "event_dates": ["2026-06-01"],
        "pilot_city_validation_matrix_summary": {
            "top_cities": [
                {
                    "city": "ankara",
                    "resolved_rows": 3,
                    "pending_rows": 0,
                    "wins": 2,
                    "hit_rate": 2 / 3,
                }
            ]
        },
        "paper_observation_target_package_summary": {
            "city_targets": [
                {
                    "city": "ankara",
                    "needed_resolved_rows": 7,
                }
            ],
            "strategy_targets": [
                {
                    "city": "ankara",
                    "target_strategy": "secondary_or_physical_edge",
                    "needed_resolved_rows": 2,
                }
            ],
        },
    }
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    report = plan.build_plan(audit_path, db_path=tmp_path / "missing.db")

    assert report["queue_count"] == 1
    target = report["plan"][0]
    assert target["city"] == "ankara"
    assert target["observation_mode"] == "paper_target_pilot"
    assert target["needed_resolved_rows"] == 7
    assert target["resolved_rows"] == 3
    assert target["wins"] == 2
    assert target["hit_rate"] == 2 / 3
    assert target["strategy_gaps"] == [
        {
            "target_strategy": "secondary_or_physical_edge",
            "needed_resolved_rows": 2,
            "resolved_rows": 0,
            "scan_directive": "scan_open_secondary_physical_edge_candidates",
        }
    ]


def test_gap_scan_plan_prefers_latest_target_package_over_stale_audit_summary(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "event_dates": ["2026-06-01"],
                "paper_observation_target_package_summary": {
                    "city_targets": [
                        {"city": "chongqing", "needed_resolved_rows": 5},
                        {"city": "nyc", "needed_resolved_rows": 10},
                    ],
                    "strategy_targets": [],
                },
            }
        ),
        encoding="utf-8",
    )
    target_package_path = tmp_path / "paper_observation_target_package_latest.json"
    target_package_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-01T01:00:00+00:00",
                "city_targets": [
                    {"city": "ankara", "needed_resolved_rows": 7},
                    {"city": "atlanta", "needed_resolved_rows": 10},
                ],
                "strategy_targets": [
                    {
                        "city": "ankara",
                        "target_strategy": "reverse_edge_buy_no_sell_yes",
                        "needed_resolved_rows": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = plan.build_plan(
        audit_path,
        db_path=tmp_path / "missing.db",
        target_package_path=target_package_path,
    )

    assert [item["city"] for item in report["plan"]] == ["ankara", "atlanta"]
    assert report["target_package_loaded"] is True
    assert report["target_package_generated_at"] == "2026-06-01T01:00:00+00:00"
    assert report["plan"][0]["strategy_gaps"][0]["target_strategy"] == (
        "reverse_edge_buy_no_sell_yes"
    )


def test_gap_scan_plan_includes_expansion_candidates_and_round_robins_slugs(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"
    audit = {
        "event_dates": ["2026-06-01"],
        "paper_observation_target_package_summary": {
            "city_targets": [{"city": "ankara", "needed_resolved_rows": 7}],
            "observation_expansion_city_targets": [
                {"city": "atlanta", "needed_resolved_rows": 10}
            ],
        },
    }
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    db_path = tmp_path / "archive.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE market_tokens (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            market_active INTEGER,
            market_closed INTEGER,
            market_archived INTEGER,
            updated_at TEXT
        )
        """
    )
    for slug, event_date in [
        ("highest-temperature-in-ankara-on-june-1-2026", "2026-06-01"),
        ("highest-temperature-in-ankara-on-june-2-2026", "2026-06-02"),
        ("highest-temperature-in-atlanta-on-june-1-2026", "2026-06-01"),
    ]:
        conn.execute(
            """
            INSERT INTO market_tokens(
                slug, event_date, outcome, market_active, market_closed, market_archived, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (slug, event_date, "18C", 1, 0, 0, "2026-06-01T02:00:00+00:00"),
        )
    conn.commit()
    conn.close()

    report = plan.build_plan(
        audit_path,
        db_path=db_path,
        include_expansion_candidates=True,
    )

    assert [item["city"] for item in report["plan"]] == ["ankara", "atlanta"]
    assert report["plan"][1]["observation_mode"] == "paper_expansion_candidate"
    assert report["priority_slugs"][:3] == [
        "highest-temperature-in-ankara-on-june-1-2026",
        "highest-temperature-in-atlanta-on-june-1-2026",
        "highest-temperature-in-ankara-on-june-2-2026",
    ]
