from scripts.production_v6_ui_ssot_acceptance import audit


def test_accepts_physical_only_v6_ui_projection() -> None:
    payload = {
        "facts": {
            "items": [
                {
                    "slug": "chengdu",
                    "event_date": "2026-07-16",
                    "posterior_ssot_fact": {
                        "schema": "sequential_temperature_posterior_ssot_v4_reported_x",
                        "reported_physical_x_distribution": [
                            {"temperature_c": 38, "probability": 0.6},
                            {"temperature_c": 39, "probability": 0.4},
                        ],
                    },
                    "ui_sections": {
                        "physical_high_posterior": {"state": "available"},
                        "amsc_metar_ssot": {"state": "available", "source": "timeline"},
                        "observation_strong": {
                            "state": "raw_observation_only",
                            "termination_risk": {"state": "available"},
                        },
                        "bayes_posterior_ssot": {"state": "market_projection_na"},
                    },
                }
            ]
        }
    }

    result = audit(payload)

    assert result["ok"] is True
    assert result["external_disk_required"] is False
    assert result["warnings"] == ["peak_timing_state_pending_probability_runtime_cutover"]


def test_rejects_market_edge_label_in_reported_x() -> None:
    payload = {
        "facts": {
            "items": [
                {
                    "posterior_ssot_fact": {
                        "schema": "sequential_temperature_posterior_ssot_v4_reported_x",
                        "reported_physical_x_distribution": [
                            {"temperature_c": 38, "probability": 1.0, "label": "38°C or below"}
                        ],
                    },
                    "ui_sections": {
                        "physical_high_posterior": {"state": "available"},
                        "amsc_metar_ssot": {"state": "available"},
                        "observation_strong": {
                            "state": "raw_observation_only",
                            "termination_risk": {"state": "available"},
                        },
                        "bayes_posterior_ssot": {"state": "market_projection_na"},
                    },
                }
            ]
        }
    }

    result = audit(payload)

    assert result["ok"] is False
    assert "reported_x_ui_contains_market_edge_bucket_label" in result["blockers"]
