from __future__ import annotations

import re
from pathlib import Path

from scripts import check_posterior_consistency as audit


ROOT = Path(__file__).resolve().parents[1]


def _payload(*, stale_path: bool = False) -> dict:
    path_posterior = 0.2776375734997888 if stale_path else 0.3645925961706722
    path_posterior_pct = 27.76 if stale_path else 36.46
    return {
        "run_date": "2026-06-21",
        "facts": {
            "source": "ui_snapshot_summary",
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-june-22-2026",
                    "event_date": "2026-06-22",
                    "run_date": "2026-06-21",
                    "distribution": [
                        {"outcome": "30°C", "q_temperature_bucket_posterior": 0.3645925961706722},
                        {"outcome": "31°C", "q_temperature_bucket_posterior": 0.2703085257485856},
                    ],
                    "target_bucket_outlook": {
                        "target_bucket": "30°C",
                        "target_final_probability_pct": 36.46,
                        "final_bucket": "30°C",
                        "final_bucket_probability_pct": 36.46,
                    },
                    "temperature_path_distribution": {
                        "target_bucket": "30°C",
                        "target_bucket_posterior": path_posterior,
                        "target_bucket_posterior_pct": path_posterior_pct,
                        "target_bucket_path_probability": 0.245,
                        "second_target_bucket": "31°C",
                        "second_target_bucket_posterior": 0.2703085257485856,
                        "second_target_bucket_posterior_pct": 27.03,
                        "target": {
                            "bucket": "30°C",
                            "posterior": path_posterior,
                            "posterior_pct": path_posterior_pct,
                            "path_probability": 0.245,
                        },
                        "second_target": {
                            "bucket": "31°C",
                            "posterior": 0.2703085257485856,
                            "posterior_pct": 27.03,
                            "path_probability": 0.12,
                        },
                        "bucket_distribution": [
                            {"bucket": "30°C", "posterior": path_posterior, "posterior_pct": path_posterior_pct},
                            {"bucket": "31°C", "posterior": 0.2703085257485856, "posterior_pct": 27.03},
                        ],
                    },
                    "target_realization_capital_plan": {
                        "target_bucket": "30°C",
                        "target_posterior": 0.3645925961706722,
                        "second_target_bucket": "31°C",
                        "second_target_posterior": 0.2703085257485856,
                        "legs": [
                            {"role": "target_yes", "bucket": "30°C", "posterior": 0.3645925961706722},
                            {"role": "second_target_yes", "bucket": "31°C", "posterior": 0.2703085257485856},
                        ],
                        "ui_risk_explanation": {
                            "target_bucket": "30°C",
                            "target_posterior": 0.3645925961706722,
                            "second_target_bucket": "31°C",
                            "second_target_posterior": 0.2703085257485856,
                        },
                    },
                    "target_switch_context": {
                        "target_bucket": "30°C",
                        "target_probability_pct": 36.46,
                        "candidate_bucket": "31°C",
                        "candidate_probability_pct": 27.03,
                    },
                }
            ],
        },
    }


def test_posterior_consistency_audit_accepts_aligned_snapshot() -> None:
    result = audit.audit_payload(_payload(), source="test")

    assert result["ok"] is True
    assert result["checked_items"] == 1
    assert result["checked_fields"] >= 18
    assert result["mismatches"] == []


def test_posterior_consistency_audit_checks_busy_snapshot_when_items_are_present() -> None:
    payload = _payload()
    payload["busy"] = True
    payload["facts"]["source"] = "event_fact_summary_cache_fast_fallback_city_filtered"

    result = audit.audit_payload(payload, source="test")

    assert result["ok"] is True
    assert result["temporary"] is True
    assert result["checked_items"] == 1
    assert result["blockers"] == []


def test_posterior_consistency_audit_blocks_empty_busy_snapshot() -> None:
    result = audit.audit_payload(
        {
            "busy": True,
            "facts": {
                "source": "ui_snapshot_rebuild_in_flight",
                "items": [],
            },
        },
        source="test",
    )

    assert result["ok"] is False
    assert result["temporary"] is True
    assert result["blockers"] == ["ui_snapshot_rebuild_in_flight"]
    assert result["checked_items"] == 0


def test_posterior_consistency_audit_rejects_stale_path_posterior() -> None:
    result = audit.audit_payload(_payload(stale_path=True), source="test")
    mismatch_fields = {item["field"] for item in result["mismatches"]}

    assert result["ok"] is False
    assert result["blockers"] == ["posterior_mismatch"]
    assert "temperature_path_distribution.target_bucket_posterior" in mismatch_fields
    assert "temperature_path_distribution.target.posterior" in mismatch_fields
    assert "temperature_path_distribution.bucket_distribution[0].posterior" in mismatch_fields


def test_posterior_consistency_audit_uses_hard_source_outlook_rows_for_path_distribution() -> None:
    payload = {
        "run_date": "2026-06-22",
        "facts": {
            "source": "ui_snapshot_summary",
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-june-22-2026",
                    "event_date": "2026-06-22",
                    "run_date": "2026-06-22",
                    "distribution": [
                        {"outcome": "30°C", "q_temperature_bucket_posterior": 0.7278},
                        {"outcome": "33°C", "q_temperature_bucket_posterior": 0.0006},
                        {"outcome": "34°C", "q_temperature_bucket_posterior": 0.0002},
                    ],
                    "target_bucket_outlook": {
                        "target_bucket_source": "hard_source_constrained_posterior_highest_bucket",
                        "final_bucket_source": "hard_source_constrained_posterior",
                        "target_bucket": "33°C",
                        "final_bucket": "33°C",
                        "target_final_probability_pct": 65.72,
                        "final_bucket_probability_pct": 65.72,
                        "second_target_bucket": "34°C",
                        "second_target_final_probability_pct": 21.10,
                        "hard_source_direct": {
                            "source": "hard_source_direct",
                            "posterior_floor_constraint_applied": True,
                            "bucket": "33°C",
                        },
                        "rows": [
                            {"outcome": "33°C", "probability": 0.6572, "probability_pct": 65.72},
                            {"outcome": "34°C", "probability": 0.2110, "probability_pct": 21.10},
                            {"outcome": "30°C", "probability": 0.0, "probability_pct": 0.0},
                        ],
                    },
                    "temperature_path_distribution": {
                        "target_bucket": "33°C",
                        "target_bucket_posterior": 0.6572,
                        "target_bucket_posterior_pct": 65.72,
                        "second_target_bucket": "34°C",
                        "second_target_bucket_posterior": 0.2110,
                        "second_target_bucket_posterior_pct": 21.10,
                        "target": {"bucket": "33°C", "posterior": 0.6572, "posterior_pct": 65.72},
                        "second_target": {"bucket": "34°C", "posterior": 0.2110, "posterior_pct": 21.10},
                        "bucket_distribution": [
                            {"bucket": "30°C", "posterior": 0.0, "posterior_pct": 0.0},
                            {"bucket": "33°C", "posterior": 0.6572, "posterior_pct": 65.72},
                            {"bucket": "34°C", "posterior": 0.2110, "posterior_pct": 21.10},
                        ],
                    },
                    "target_realization_capital_plan": {
                        "target_bucket": "33°C",
                        "target_posterior": 0.6572,
                        "second_target_bucket": "34°C",
                        "second_target_posterior": 0.2110,
                        "legs": [
                            {"role": "target_yes", "bucket": "33°C", "posterior": 0.6572},
                            {"role": "second_target_yes", "bucket": "34°C", "posterior": 0.2110},
                        ],
                    },
                }
            ],
        },
    }

    result = audit.audit_payload(payload, source="test")

    assert result["ok"] is True
    assert result["mismatches"] == []


def _top_level_function_body(source: str, name: str) -> str:
    match = re.search(rf"^def {re.escape(name)}\(", source, re.MULTILINE)
    assert match is not None, f"{name} missing"
    next_match = re.search(r"^def [A-Za-z_][A-Za-z0-9_]*\(", source[match.end() :], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(source)
    return source[match.start() : end]


def test_public_ui_snapshot_entrypoints_use_posterior_alignment_before_derived_outputs() -> None:
    source = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    entrypoints = (
        "query_facts",
        "query_facts_paged",
        "query_facts_range",
        "ui_snapshot",
        "ui_snapshot_summary",
    )

    for name in entrypoints:
        body = _top_level_function_body(source, name)
        align_at = body.find("_align_snapshot_posterior_derived_fields(payload)")
        if align_at < 0:
            align_at = body.find("_align_snapshot_posterior_derived_fields(out)")
        assert align_at >= 0, f"{name} must call _align_snapshot_posterior_derived_fields"
        plan_at = body.find("_attach_target_realization_capital_plan(")
        switch_at = body.find("_attach_target_switch_context(")
        if plan_at >= 0:
            assert align_at < plan_at, f"{name} must align posterior before capital plan"
        if switch_at >= 0:
            assert align_at < switch_at, f"{name} must align posterior before switch context"
