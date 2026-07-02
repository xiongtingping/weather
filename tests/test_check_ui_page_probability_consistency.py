from __future__ import annotations

from scripts import check_ui_page_probability_consistency as audit


def _payload(*, stale_top_level: bool = False, stale_ctp: bool = False) -> dict:
    target_prob = 0.5298257112574187
    target_pct = target_prob * 100.0
    stale_pct = 54.496973508289415
    ctp_target_prob = 0.5449697350828941 if stale_ctp else target_prob
    return {
        "run_date": "2026-06-29",
        "facts": {
            "source": "ui_snapshot_summary",
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-june-30-2026",
                    "event_date": "2026-06-30",
                    "run_date": "2026-06-29",
                    "target_bucket": "30°C or higher",
                    "target_bucket_probability_pct": stale_pct if stale_top_level else target_pct,
                    "posterior_highest_bucket_label": "30°C or higher",
                    "posterior_highest_probability_pct": target_pct,
                    "distribution": [
                        {
                            "outcome": "28°C",
                            "q_temperature_bucket_posterior": 0.13581924517927813,
                            "q_bayes_posterior": 0.13581924517927813,
                        },
                        {
                            "outcome": "29°C",
                            "q_temperature_bucket_posterior": 0.3021231348784663,
                            "q_bayes_posterior": 0.3021231348784663,
                        },
                        {
                            "outcome": "30°C or higher",
                            "q_temperature_bucket_posterior": target_prob,
                            "q_bayes_posterior": target_prob,
                        },
                    ],
                    "posterior_temperature_distribution": {
                        "ssot_source": "posterior_temperature_distribution.bucket_probabilities",
                        "bucket_probabilities": [
                            {"outcome": "28°C", "probability": 0.13581924517927813},
                            {"outcome": "29°C", "probability": 0.3021231348784663},
                            {"outcome": "30°C or higher", "probability": ctp_target_prob},
                        ],
                    },
                    "target_bucket_selection": {
                        "target_bucket": "30°C or higher",
                        "target_probability_pct": target_pct,
                        "target_bucket_probability_pct": target_pct,
                        "posterior_highest_bucket_label": "30°C or higher",
                        "posterior_highest_probability_pct": target_pct,
                        "second_target_bucket": "29°C",
                        "second_target_probability_pct": 30.21231348784663,
                    },
                    "posterior_target_bucket_selection": {
                        "target_bucket": "30°C or higher",
                        "target_probability_pct": target_pct,
                        "target_bucket_probability_pct": target_pct,
                        "posterior_highest_bucket_label": "30°C or higher",
                        "posterior_highest_probability_pct": target_pct,
                    },
                    "target_bucket_outlook": {
                        "target_bucket": "30°C or higher",
                        "target_bucket_probability_pct": target_pct,
                        "target_final_probability_pct": target_pct,
                        "final_bucket": "30°C or higher",
                        "final_bucket_probability_pct": target_pct,
                        "posterior_final_bucket": "30°C or higher",
                        "posterior_final_bucket_probability_pct": target_pct,
                        "cumulative_posterior_final_bucket": "30°C or higher",
                        "cumulative_posterior_final_bucket_probability_pct": target_pct,
                    },
                    "temperature_path_distribution": {
                        "target_bucket": "30°C or higher",
                        "target_bucket_posterior": target_prob,
                        "target_bucket_posterior_pct": target_pct,
                        "second_target_bucket": "29°C",
                        "second_target_bucket_posterior": 0.3021231348784663,
                        "second_target_bucket_posterior_pct": 30.21231348784663,
                        "target": {
                            "bucket": "30°C or higher",
                            "posterior": target_prob,
                            "posterior_pct": target_pct,
                        },
                        "bucket_distribution": [
                            {"bucket": "28°C", "posterior": 0.13581924517927813},
                            {"bucket": "29°C", "posterior": 0.3021231348784663},
                            {"bucket": "30°C or higher", "posterior": target_prob},
                        ],
                    },
                    "target_realization_capital_plan": {
                        "ok": True,
                        "target_bucket": "30°C or higher",
                        "target_posterior": target_prob,
                        "second_target_bucket": "29°C",
                        "second_target_posterior": 0.3021231348784663,
                        "legs": [
                            {"role": "target_yes", "bucket": "30°C or higher", "posterior": target_prob},
                            {"role": "second_target_yes", "bucket": "29°C", "posterior": 0.3021231348784663},
                        ],
                    },
                }
            ],
        },
    }


def test_page_probability_consistency_accepts_aligned_page_snapshot() -> None:
    result = audit.audit_payload(_payload(), source="test")

    assert result["ok"] is True
    assert result["checked_items"] == 1
    assert result["mismatches"] == []
    assert result["items"][0]["top_buckets"][0]["bucket"] == "30°C or higher"


def test_page_probability_consistency_rejects_stale_top_level_target_probability() -> None:
    result = audit.audit_payload(_payload(stale_top_level=True), source="test")
    fields = {item["field"] for item in result["mismatches"]}

    assert result["ok"] is False
    assert result["blockers"] == ["page_probability_mismatch"]
    assert "item.target_bucket_probability_pct" in fields


def test_page_probability_consistency_rejects_stale_ctp_bucket_probability() -> None:
    result = audit.audit_payload(_payload(stale_ctp=True), source="test")
    codes = {item["code"] for item in result["mismatches"]}

    assert result["ok"] is False
    assert "ctp_probability_mismatch" in codes
