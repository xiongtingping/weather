from weather_fact_snapshot_writer import build_posterior_ssot_fact

# ``joint_weak_physical_strong_composer_v1`` is the only version
# build_posterior_ssot_fact will mint an available block for -- anything else
# is joint_weak_physical_strong_model_unavailable and the block comes back
# blocked, with no strong_physical_evidence on it at all.  These fixtures said
# "v3.1_multimodel_allday", which is the NCEI multimodel trainer's own version
# string and never the composer's.


def test_joint_confirmed_cooling_emits_only_composer_owned_unattainable_upper_bucket() -> None:
    result = build_posterior_ssot_fact(
        {
            "production_model_version": "joint_weak_physical_strong_composer_v1",
            "production_model_ready": True,
            "posterior_temperature_distribution": [
                {"bucket": "32°C", "probability": 0.6},
                {"bucket": "33°C", "probability": 0.3},
                {"bucket": "34°C", "probability": 0.1},
            ],
            "hard_constraints": {
                "metar_strong": {
                    "observed_today_max_c": 33.0,
                    "observed_today_max_source": "zuuu_metar_raw_amsc",
                    "observed_today_max_at": "2026-07-12T05:00:00Z",
                }
            },
            "temperature_trajectory": {"state": "cooling"},
            "physical_cutoff_evidence": {
                "true_cooling_or_cutoff_confirmed": True,
                "magnitude_cooling_confirmed": True,
                "physical_cutoff_confirmed": True,
            },
        }
    )
    evidence = result["strong_physical_evidence"]
    assert evidence["safe_no_amsc_observation"] is True
    assert evidence["bucket_exclusions"] == [
        {
            "bucket": "34°C",
            "excluded": True,
            "kind": "remaining_warming_unattainable",
            "temperature_momentum": True,
            "temperature_trajectory": True,
            "cloud_rain_physics": True,
            "confidence": 1.0,
            "source": "metar_strong_joint_confirmed_cooling_cutoff",
        }
    ]


def test_upper_bucket_is_not_excluded_without_the_full_joint_cutoff_verdict() -> None:
    result = build_posterior_ssot_fact(
        {
            "production_model_version": "joint_weak_physical_strong_composer_v1",
            "production_model_ready": True,
            "posterior_temperature_distribution": [
                {"bucket": "32°C", "probability": 0.8},
                {"bucket": "34°C", "probability": 0.2},
            ],
            "hard_constraints": {
                "metar_strong": {
                    "observed_today_max_c": 33.0,
                    "observed_today_max_source": "zuuu_metar_raw_amsc",
                }
            },
            "temperature_trajectory": {"state": "cooling"},
            "physical_cutoff_evidence": {
                "true_cooling_or_cutoff_confirmed": True,
                "magnitude_cooling_confirmed": False,
                "physical_cutoff_confirmed": True,
            },
        }
    )
    assert result["strong_physical_evidence"]["bucket_exclusions"] == []


def test_joint_warming_environment_emits_top_yes_support_verdict() -> None:
    result = build_posterior_ssot_fact(
        {
            "production_model_version": "joint_weak_physical_strong_composer_v1",
            "production_model_ready": True,
            "posterior_temperature_distribution": [
                {"bucket": "33°C", "probability": 0.2},
                {"bucket": "34°C", "probability": 0.8},
            ],
            "hard_constraints": {"metar_strong": {"observed_today_max_c": 33.0}},
            "temperature_trajectory": {"state": "sustained_warming"},
            "state_probs": {"S2_open_boundary_layer": 0.8},
            "physical_cutoff_evidence": {"true_cooling_or_cutoff_confirmed": False},
        }
    )
    evidence = result["strong_physical_evidence"]
    assert evidence["top_support_mode"] == "joint_warming_environment"
    assert all(
        evidence[key] is True
        for key in (
            "top_temperature_momentum",
            "top_temperature_trajectory",
            "top_cloud_rain_physics",
        )
    )
