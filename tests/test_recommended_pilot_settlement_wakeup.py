from __future__ import annotations

import json
from pathlib import Path

from scripts import recommended_pilot_settlement_wakeup as wakeup


def _audit_payload() -> dict:
    return {
        "event_dates": ["2026-05-31", "2026-06-01"],
        "pilot_cities": ["chongqing"],
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
                                }
                            ]
                        }
                    }
                },
                "recommended_pilot_observation_validation": {
                    "ok": False,
                    "thresholds": {"min_resolved_rows": 10},
                    "city_count": 1,
                    "by_city": [
                        {
                            "city": "buenos-aires",
                            "ok": False,
                            "observation_mode": "paper_only_not_small_capital",
                            "overall": {
                                "resolved_rows": 1,
                                "pending_rows": 0,
                                "wins": 1,
                                "hit_rate": 1.0,
                            },
                            "blockers": [
                                "recommended_pilot_resolved_rows_below_min",
                            ],
                            "next_action": (
                                "continue_paper_only_observation_until_min_rows_and_hit_rate_pass"
                            ),
                        }
                    ],
                    "blockers": [
                        "buenos-aires:recommended_pilot_resolved_rows_below_min",
                    ],
                    "sample_gap_queue": [
                        {
                            "city": "buenos-aires",
                            "needed_resolved_rows": 9,
                        }
                    ],
                    "next_action": "continue_collecting_recommended_paper_only_pilot_samples",
                }
            }
        },
    }


def test_wakeup_detects_recommended_pilot_settlement_status_change(
    monkeypatch,
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(_audit_payload()), encoding="utf-8")

    monkeypatch.setattr(
        wakeup.settlement_label_eligibility_report,
        "build_report",
        lambda *args, **kwargs: {
            "markets": [
                {
                    "slug": "highest-temperature-in-buenos-aires-on-june-1-2026",
                    "event_date": "2026-06-01",
                    "status": "label_ready",
                }
            ]
        },
    )

    report = wakeup.build_report(
        tmp_path / "archive.db",
        audit_path=audit_path,
        refresh=False,
    )

    assert report["watched_market_count"] == 1
    assert report["triggered_market_count"] == 1
    assert report["triggered_markets"][0]["current_status"] == "label_ready"
    assert report["triggered_markets"][0]["wakeup_reason"] == "paper_observation_label_ready"
    assert report["next_action"] == "review_and_apply_settlement_calibration_labels_then_refresh_validation"
    validation = report["recommended_pilot_observation_validation"]
    assert validation["by_city"][0]["city"] == "buenos-aires"
    assert validation["by_city"][0]["resolved_rows"] == 1
    assert validation["by_city"][0]["hit_rate"] == 1.0
    assert validation["sample_gap_queue"][0]["needed_resolved_rows"] == 9


def test_wakeup_waits_while_market_remains_not_closed(monkeypatch, tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(_audit_payload()), encoding="utf-8")

    monkeypatch.setattr(
        wakeup.settlement_label_eligibility_report,
        "build_report",
        lambda *args, **kwargs: {
            "markets": [
                {
                    "slug": "highest-temperature-in-buenos-aires-on-june-1-2026",
                    "event_date": "2026-06-01",
                    "status": "settlement_not_closed",
                }
            ]
        },
    )

    report = wakeup.build_report(tmp_path / "archive.db", audit_path=audit_path)

    assert report["watched_market_count"] == 1
    assert report["triggered_market_count"] == 0
    assert report["recommended_pilot_observation_validation"]["city_count"] == 1
    assert report["next_action"] == "wait_for_paper_observation_settlement_close"


def test_wakeup_watches_target_and_expansion_paper_observation_slugs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    audit = {
        "event_dates": ["2026-05-31", "2026-06-01"],
        "pilot_cities": ["chongqing", "ankara"],
        "current_strategy_batch_summary": {
            "targeted_candidate_status": {
                "target_hunt_plan": [
                    {
                        "city": "ankara",
                        "paper_observation_slugs": [
                            "highest-temperature-in-ankara-on-june-1-2026"
                        ],
                    }
                ],
                "expansion_hunt_plan": [
                    {
                        "city": "atlanta",
                        "paper_observation_rows": 1,
                    }
                ],
                "paper_observation_availability": {
                    "atlanta|secondary_or_physical_edge": {
                        "city": "atlanta",
                        "paper_observation_slugs": [
                            "highest-temperature-in-atlanta-on-june-1-2026"
                        ],
                    }
                },
            }
        },
    }
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    def fake_eligibility(*args, **kwargs):
        city = kwargs.get("city")
        if city == "ankara":
            return {
                "markets": [
                    {
                        "slug": "highest-temperature-in-ankara-on-june-1-2026",
                        "event_date": "2026-06-01",
                        "status": "settlement_not_closed",
                    }
                ]
            }
        return {
            "markets": [
                {
                    "slug": "highest-temperature-in-atlanta-on-june-1-2026",
                    "event_date": "2026-06-01",
                    "status": "label_ready",
                }
            ]
        }

    monkeypatch.setattr(
        wakeup.settlement_label_eligibility_report,
        "build_report",
        fake_eligibility,
    )

    report = wakeup.build_report(tmp_path / "archive.db", audit_path=audit_path)

    assert report["watched_market_count"] == 2
    assert report["triggered_market_count"] == 1
    assert report["triggered_markets"][0]["city"] == "atlanta"
    assert report["triggered_markets"][0]["current_status"] == "label_ready"
    assert report["triggered_markets"][0]["event_date"] == "2026-06-01"


def test_wakeup_surfaces_missing_settlement_actual_as_repair_action(
    monkeypatch,
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(_audit_payload()), encoding="utf-8")

    monkeypatch.setattr(
        wakeup.settlement_label_eligibility_report,
        "build_report",
        lambda *args, **kwargs: {
            "markets": [
                {
                    "slug": "highest-temperature-in-buenos-aires-on-june-1-2026",
                    "event_date": "2026-06-01",
                    "status": "closed_without_settlement_actual",
                }
            ]
        },
    )

    report = wakeup.build_report(tmp_path / "archive.db", audit_path=audit_path)

    assert report["triggered_status_counts"] == {"closed_without_settlement_actual": 1}
    assert report["triggered_markets"][0]["wakeup_reason"] == "settlement_actual_missing_after_close"
    assert report["next_action"] == "repair_missing_settlement_actual_for_closed_markets"


def test_refresh_result_surfaces_updated_recommended_pilot_validation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(_audit_payload()), encoding="utf-8")

    monkeypatch.setattr(
        wakeup.settlement_label_eligibility_report,
        "build_report",
        lambda *args, **kwargs: {
            "markets": [
                {
                    "slug": "highest-temperature-in-buenos-aires-on-june-1-2026",
                    "event_date": "2026-06-01",
                    "status": "label_ready",
                }
            ]
        },
    )
    monkeypatch.setattr(
        wakeup.paper_observation_target_package,
        "build_package",
        lambda *args, **kwargs: {"ok": False, "event_dates": ["2026-06-01"]},
    )
    monkeypatch.setattr(
        wakeup.paper_observation_target_package,
        "format_markdown",
        lambda payload: "# target",
    )
    monkeypatch.setattr(
        wakeup.paper_observation_target_package,
        "DEFAULT_OUT_JSON",
        tmp_path / "target.json",
    )
    monkeypatch.setattr(
        wakeup.paper_observation_target_package,
        "DEFAULT_OUT_MD",
        tmp_path / "target.md",
    )
    monkeypatch.setattr(
        wakeup.paper_observation_rolling_validation,
        "build_report",
        lambda *args, **kwargs: {"ok": False, "blockers": ["paper_observation_resolved_rows_below_min"]},
    )
    monkeypatch.setattr(
        wakeup.paper_observation_rolling_validation,
        "DEFAULT_OUT",
        tmp_path / "rolling.json",
    )
    monkeypatch.setattr(
        wakeup.strategy_objective_completion_audit,
        "DEFAULT_OUT",
        tmp_path / "objective.json",
    )
    monkeypatch.setattr(
        wakeup.strategy_objective_completion_audit,
        "build_report",
        lambda *args, **kwargs: {
            "ok": False,
            "current_strategy_batch_summary": {
                "targeted_candidate_status": {
                    "recommended_pilot_observation_validation": {
                        "ok": False,
                        "thresholds": {"min_resolved_rows": 10},
                        "city_count": 1,
                        "by_city": [
                            {
                                "city": "buenos-aires",
                                "ok": False,
                                "overall": {
                                    "resolved_rows": 2,
                                    "pending_rows": 0,
                                    "wins": 2,
                                    "hit_rate": 1.0,
                                },
                                "blockers": [
                                    "recommended_pilot_resolved_rows_below_min",
                                ],
                            }
                        ],
                        "blockers": [
                            "buenos-aires:recommended_pilot_resolved_rows_below_min",
                        ],
                        "next_action": (
                            "continue_collecting_recommended_paper_only_pilot_samples"
                        ),
                    }
                }
            },
        },
    )

    report = wakeup.build_report(
        tmp_path / "archive.db",
        audit_path=audit_path,
        refresh=True,
    )

    validation = report["refresh_result"]["recommended_pilot_observation_validation"]
    assert report["refresh_ran"] is True
    assert validation["by_city"][0]["resolved_rows"] == 2
    assert report["recommended_pilot_observation_validation"]["by_city"][0]["resolved_rows"] == 2
