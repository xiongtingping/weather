import math
from datetime import datetime, timezone

import pytest

from hierarchical_posterior_pipeline_runtime import (
    SCHEMA_VERSION,
    build_hierarchical_posterior_pipeline,
)


BUCKETS = ["29", "30", "31", "32", "33"]


@pytest.fixture(autouse=True)
def _isolate_joint_weak_live_sources(monkeypatch):
    """Keep hierarchy tests deterministic while the joint model has its own tests."""

    def fake_joint(*, temperature_grid_c, event_date, decision_as_of, source_observations):
        decision = datetime.fromisoformat(str(decision_as_of).replace("Z", "+00:00"))
        if decision.tzinfo is None:
            decision = decision.replace(tzinfo=timezone.utc)
        decision = decision.astimezone(timezone.utc)
        revisions = []
        experts = []
        for row in source_observations:
            if row.get("source_class") != "forecast_weak_source" or row.get("forecast_peak_c") is None:
                continue
            available_text = str(row.get("response_completed_at") or row.get("fetched_at") or "")
            try:
                available = datetime.fromisoformat(available_text.replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
            if available > decision:
                continue
            source_id = "taf" if "taf" in str(row.get("source_id") or "").lower() else "gfs"
            revision_id = str(row.get("source_event_id") or row.get("source_observation_id") or source_id)
            peak = float(row["forecast_peak_c"])
            revisions.append(
                {
                    "source_id": source_id,
                    "source_revision_id": revision_id,
                    "available_at": available_text,
                    "forecast_peak_c": peak,
                }
            )
            experts.append(
                {
                    "source_id": source_id,
                    "source_revision_id": revision_id,
                    "provider_family": "taf" if source_id == "taf" else "official_nwp",
                    "forecast_peak_c": peak,
                    "calibration_bias_c": 0.0,
                    "calibrated_mean_c": peak,
                    "calibrated_sigma_c": 0.9,
                    "information_weight": 1.0,
                }
            )
        raw = [
            sum(math.exp(-0.5 * ((float(temperature) - expert["forecast_peak_c"]) / 0.9) ** 2) for expert in experts)
            if experts
            else 1.0
            for temperature in temperature_grid_c
        ]
        total = sum(raw)
        available_ids = sorted({expert["source_id"] for expert in experts})
        runtime = {
            "schema": "joint_forecast_weak_likelihood_v1",
            "ok": True,
            "state": "available",
            "artifact_sha256": "joint-weak-hierarchy-test",
            "forecast_evidence_snapshot_id": "joint-weak-hierarchy-evidence",
            "weak_source_set": {
                "source_set_key": "+".join(available_ids) if available_ids else "empty",
                "available_source_ids": available_ids,
                "missing_source_ids": [],
            },
            "joint_forecast_weak_likelihood": [
                {"temperature_c": float(temperature), "likelihood": value / total}
                for temperature, value in zip(temperature_grid_c, raw)
            ],
            "source_expert_trace": experts,
            "source_trace": [],
        }
        return {
            "ok": True,
            "state": "available",
            "forecast_evidence": {"source_revisions": revisions},
            "joint_weak_likelihood": runtime,
        }

    monkeypatch.setattr("hierarchical_posterior_pipeline_runtime.build_live_joint_weak_likelihood", fake_joint)


def test_pipeline_composes_prior_weak_strong_phase_and_posterior():
    result = build_hierarchical_posterior_pipeline(
        buckets=BUCKETS,
        prior_distribution={"29": 0.10, "30": 0.20, "31": 0.30, "32": 0.25, "33": 0.15},
        event_date="2026-07-03",
        as_of="2026-07-03T12:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_model",
                "source_id": "current_model_ensemble",
                "source_class": "forecast_weak_source",
                "role": "model_forecast_weak_likelihood",
                "forecast_peak_c": 32.5,
                "sigma_c": 0.9,
                "confidence": 0.7,
                "fetched_at": "2026-07-03T03:50:00Z",
            },
            {
                "source_event_id": "srcevt_awc",
                "source_id": "awc_cache_metars_csv_gz",
                "source_class": "hard_observation_candidate",
                "temperature_c": 28.0,
                "dewpoint_c": 22.0,
                "observed_today_max_c": 28.0,
                "fetched_at": "2026-07-03T03:55:00Z",
                "valid_time": "2026-07-03T03:30:00Z",
                "payload": {
                    "visibility_m": 9999,
                    "cloud_text": "FEW011 SCT020",
                    "dewpoint_trend_c_per_hour": -1.0,
                    "momentum_rate_c_per_hour": 2.0,
                    "temperature_momentum_1h_c_per_hour": 1.4,
                    "temperature_momentum_30m_c_per_hour": 1.1,
                },
            },
        ],
    )

    assert result["schema"] == SCHEMA_VERSION
    assert result["posterior_activation_mode"] == "live_no_shadow"
    assert result["read_only"] is True
    assert result["production_entry_enabled"] is False
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_auto_trade_decisions"] is False
    assert result["guards"]["places_orders"] is False
    assert result["phase"] == "D0_midday"
    assert result["beta_weak"] == 0.2
    assert result["beta_strong_base"] == 0.85
    assert result["beta_strong"] == 0.85
    assert result["likelihood_information_quality"]["metar_strong"]["quality"] == 1.0
    assert result["likelihood_information_quality"]["weather_direction_not_reused_in_quality"] is True
    assert math.isclose(result["normalization"]["posterior_sum"], 1.0, abs_tol=1e-12)
    assert result["runtime_outputs"]["forecast_weak_likelihood"]["schema"] == "joint_forecast_weak_likelihood_v1"
    assert result["runtime_outputs"]["metar_strong_likelihood"]["schema"] == "metar_strong_likelihood_runtime_v1"
    assert result["runtime_outputs"]["hierarchical_temperature_posterior"]["schema"] == "sequential_remaining_warming_bayes_runtime_v31"
    assert result["runtime_outputs"]["sequential_remaining_warming_bayes"]["schema"] == "sequential_remaining_warming_bayes_runtime_v31"
    assert result["boundary"]["legacy_bucket_product_posterior_not_used"] is True
    assert result["boundary"]["continuous_before_bucket_integration"] is True
    trajectory = result["runtime_outputs"]["metar_strong_likelihood"]["temperature_trajectory"]
    assert trajectory["state"] in {"sustained_warming", "emerging_warming"}
    assert trajectory["gate_state"] == "confirmed"
    assert result["source_lineage"]["source_event_ids"] == ["srcevt_model", "srcevt_awc"]
    assert result["source_lineage"]["summary"]["forecast_weak_included"] == 1
    assert result["source_lineage"]["summary"]["metar_strong_included"] == 1
    assert result["posterior_temperature_distribution_map"]["32"] > result["posterior_temperature_distribution_map"]["29"]
    assert result["posterior_continuous_distribution"]["source"] == "sequential_remaining_warming_bayes_runtime_v31"
    assert result["posterior_bucket_cdf"] == result["runtime_outputs"]["hierarchical_temperature_posterior"]["posterior_bucket_cdf"]
    assert result["posterior_quantile_grid"] == result["runtime_outputs"]["hierarchical_temperature_posterior"]["posterior_quantile_grid"]
    assert result["posterior_quantiles_c"]["0.5"] is not None
    assert result["predicted_peak_time_prior"]["role"] == "weak_forecast_timing_prior"
    assert result["predicted_peak_time_prior"]["cannot_finalize_daily_peak"] is True
    assert result["predicted_peak_time_prior"]["source"]
    assert result["predicted_peak_time_posterior"]["schema"] == "predicted_peak_time_posterior_v1"
    assert result["predicted_peak_time_posterior"]["predicted_not_observed"] is True
    assert result["observed_peak_platform"]["status"] == "provisional"
    assert result["observed_peak_platform"]["observed_peak_to_date_c"] == 28.0
    assert result["observed_peak_platform"]["source_event_ids"] == ["srcevt_awc"]
    assert result["boundary"]["predicted_peak_time_is_weak_prior_only"] is True
    assert result["boundary"]["observed_peak_time_uses_full_canonical_metar_day"] is True


def test_pipeline_d1_uses_weak_likelihood_and_neutral_missing_strong():
    result = build_hierarchical_posterior_pipeline(
        buckets=BUCKETS,
        prior_distribution={"29": 0.15, "30": 0.20, "31": 0.25, "32": 0.25, "33": 0.15},
        event_date="2026-07-03",
        as_of="2026-07-02T13:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_taf",
                "source_id": "zuuu_taf_forecast_peak",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood",
                "forecast_peak_c": 33.0,
                "sigma_c": 0.8,
                "fetched_at": "2026-07-02T04:50:00Z",
            }
        ],
    )

    assert result["phase"] == "D1_late"
    assert result["beta_weak"] == 0.85
    assert result["beta_strong"] == 0.0
    assert "current_temp_and_observed_today_max_missing" in result["na_reasons"]
    assert result["posterior_temperature_distribution_map"]["33"] > result["posterior_temperature_distribution_map"]["29"]


def test_probability_evidence_without_freshness_metadata_fails_closed():
    result = build_hierarchical_posterior_pipeline(
        buckets=BUCKETS,
        prior_distribution={bucket: 0.2 for bucket in BUCKETS},
        event_date="2026-07-03",
        as_of="2026-07-03T12:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_model_missing_time",
                "source_id": "current_model_ensemble",
                "source_class": "forecast_weak_source",
                "role": "model_forecast_weak_likelihood",
                "forecast_peak_c": 33.0,
            },
            {
                "source_event_id": "srcevt_metar_missing_time",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 31.0,
                "observed_today_max_c": 31.0,
            },
        ],
    )

    quality = result["likelihood_information_quality"]
    assert quality["forecast_weak"]["quality"] == 0.0
    assert quality["metar_strong"]["quality"] == 0.0
    assert "included_source_missing" in quality["forecast_weak"]["na_reasons"]
    assert "freshness_metadata_missing" in quality["metar_strong"]["na_reasons"]


def test_d0_pre_dawn_metar_low_temp_does_not_turn_forecast_peak_into_low_edge_target():
    buckets = [
        "33°C or below",
        "34°C",
        "35°C",
        "36°C",
        "37°C",
        "38°C",
        "39°C",
        "40°C",
        "41°C",
        "42°C",
        "43°C or higher",
    ]
    prior = {"33°C or below": 0.60, "34°C": 0.142857142858}
    prior.update({bucket: 0.028571428571 for bucket in buckets[2:]})
    result = build_hierarchical_posterior_pipeline(
        buckets=buckets,
        prior_distribution=prior,
        event_date="2026-07-08",
        as_of="2026-07-07T23:30:11Z",
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_metar",
                "source_observation_id": "srcobs_amsc_metar",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "role": "metar_hard_observation",
                "temperature_c": 27.0,
                "dewpoint_c": 23.0,
                "observed_today_max_c": 27.0,
                "valid_time": "2026-07-07T23:00:00Z",
                "fetched_at": "2026-07-07T23:05:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "CAVOK"},
            },
            {
                "source_event_id": "srcevt_amsc_taf",
                "source_observation_id": "srcobs_amsc_taf",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood",
                "forecast_peak_c": 38.0,
                "fetched_at": "2026-07-07T22:30:00Z",
            },
        ],
    )

    posterior = result["posterior_temperature_distribution_map"]
    assert result["phase"] == "D0_pre_dawn"
    assert result["runtime_outputs"]["metar_strong_likelihood"]["phase_adjustment"]["state"] == "neutralized"
    assert result["daily_high_candidate_live_rollout"]["state"] == "deferred_by_phase_guard"
    assert max(posterior, key=posterior.get) == "38°C"
    assert posterior["38°C"] > posterior["33°C or below"]


def test_d0_morning_current_temp_is_still_lower_bound_when_taf_peak_is_much_hotter():
    buckets = [
        "33°C or below",
        "34°C",
        "35°C",
        "36°C",
        "37°C",
        "38°C",
        "39°C",
        "40°C",
        "41°C",
        "42°C",
        "43°C or higher",
    ]
    prior = {"33°C or below": 0.60, "34°C": 0.142857142858}
    prior.update({bucket: 0.028571428571 for bucket in buckets[2:]})
    result = build_hierarchical_posterior_pipeline(
        buckets=buckets,
        prior_distribution=prior,
        event_date="2026-07-08",
        as_of="2026-07-08T00:30:19Z",
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_metar",
                "source_observation_id": "srcobs_amsc_metar",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "role": "metar_hard_observation",
                "temperature_c": 29.0,
                "dewpoint_c": 22.0,
                "observed_today_max_c": 29.0,
                "valid_time": "2026-07-08T00:00:00Z",
                "fetched_at": "2026-07-08T00:05:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW050"},
            },
            {
                "source_event_id": "srcevt_amsc_taf",
                "source_observation_id": "srcobs_amsc_taf",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood",
                "forecast_peak_c": 38.0,
                "fetched_at": "2026-07-07T23:30:00Z",
            },
        ],
    )

    posterior = result["posterior_temperature_distribution_map"]
    assert result["phase"] == "D0_morning"
    assert result["runtime_outputs"]["metar_strong_likelihood"]["phase_adjustment"]["state"] == "neutralized"
    assert result["daily_high_candidate_live_rollout"]["state"] == "deferred_by_phase_guard"
    assert max(posterior, key=posterior.get) == "38°C"
    assert posterior["38°C"] > posterior["33°C or below"]


def test_pipeline_hard_mask_blocks_impossible_bucket_even_with_high_weak_forecast():
    result = build_hierarchical_posterior_pipeline(
        buckets=BUCKETS,
        prior_distribution={"29": 0.20, "30": 0.25, "31": 0.25, "32": 0.20, "33": 0.10},
        event_date="2026-07-03",
        as_of="2026-07-03T15:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_taf",
                "source_id": "zuuu_taf_forecast_peak",
                "source_class": "forecast_weak_source",
                "forecast_peak_c": 33.0,
                "sigma_c": 0.4,
                "confidence": 1.0,
            },
            {
                "source_event_id": "srcevt_awc",
                "source_id": "awc_cache_metars_csv_gz",
                "source_class": "hard_observation_candidate",
                "temperature_c": 31.0,
                "dewpoint_c": 22.0,
                "observed_today_max_c": 31.0,
                "payload": {"visibility_m": 9999, "cloud_text": "FEW020", "momentum_rate_c_per_hour": 0.3},
            },
        ],
    )

    assert result["posterior_temperature_distribution_map"]["29"] == 0.0
    assert result["posterior_temperature_distribution_map"]["30"] == 0.0
    assert result["hard_constraints"]["combined_hard_mask"]["29"] == 0.0
    assert result["hard_constraints"]["posterior"]["hard_constraint_violation_count"] == 0


def test_pipeline_excludes_diagnostic_current_from_both_weak_and_strong_paths():
    result = build_hierarchical_posterior_pipeline(
        buckets=BUCKETS,
        prior_distribution={"29": 0.2, "30": 0.2, "31": 0.2, "32": 0.2, "33": 0.2},
        event_date="2026-07-03",
        as_of="2026-07-03T12:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_twc_current",
                "source_id": "twc_current_icao_metric",
                "source_class": "diagnostic_current_source",
                "temperature_c": 33.0,
                "observed_today_max_c": 33.0,
            }
        ],
    )

    weak_trace = result["runtime_outputs"]["forecast_weak_likelihood"]["source_trace"]
    strong_trace = result["runtime_outputs"]["metar_strong_likelihood"]["source_trace"]
    assert weak_trace == []
    assert strong_trace[0]["included"] is False
    assert result["source_lineage"]["source_event_ids"] == []
    assert result["boundary"]["old_additive_evidence_pool_not_used"] is True


def test_pipeline_accepts_external_hard_mask_override_as_read_only_input():
    result = build_hierarchical_posterior_pipeline(
        buckets=BUCKETS,
        prior_distribution={bucket: 0.2 for bucket in BUCKETS},
        event_date="2026-07-03",
        as_of="2026-07-03T12:00:00+08:00",
        observation_inputs={
            "current_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW020",
            "momentum_rate_c_per_hour": 1.0,
        },
        hard_mask={"33": 0.0},
        hard_mask_signals={"hard_mask_reason": "manual_read_only_test_mask"},
    )

    assert result["posterior_temperature_distribution_map"]["33"] == 0.0
    assert set(result["hard_mask_reason"].split("+")) == {
        "observed_today_max_c",
        "current_temperature_c",
        "manual_read_only_test_mask",
    }
    assert result["boundary"]["does_not_write_fact_snapshot"] is True


def test_pipeline_caps_midday_high_tail_without_recent_momentum_confirmation():
    buckets = ["35°C", "36°C", "37°C", "38°C", "39°C", "40°C", "41°C", "42°C"]
    result = build_hierarchical_posterior_pipeline(
        buckets=buckets,
        prior_distribution={bucket: 1.0 / len(buckets) for bucket in buckets},
        event_date="2026-07-07",
        as_of="2026-07-07T13:00:00+08:00",
        observation_inputs={
            "current_temp_c": 35.0,
            "observed_today_max_c": 35.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW020",
            "temperature_momentum_6h_c_per_hour": 0.5,
            "temperature_momentum_3h_c_per_hour": 1.6666666667,
            "temperature_momentum_2h_c_per_hour": 2.0,
            "temperature_momentum_1h_c_per_hour": 2.0,
        },
    )

    trajectory = result["runtime_outputs"]["metar_strong_likelihood"]["temperature_trajectory"]
    strong_map = result["runtime_outputs"]["metar_strong_likelihood"]["metar_strong_likelihood_map"]
    posterior = result["posterior_temperature_distribution_map"]
    assert trajectory["state"] == "midday_recent_confirmation_missing"
    assert trajectory["gate_state"] == "recent_window_watch"
    assert trajectory["gate_reason"] == "peak_window_requires_30m_recent_confirmation"
    assert "temperature_momentum_midday_recent_window_confirmation_missing" in result["na_reasons"]
    assert strong_map["40°C"] < strong_map["38°C"]
    # The untraced compatibility observation may still produce a diagnostic
    # strong map, but it must not alter the production posterior.
    assert posterior["40°C"] == posterior["38°C"]
    assert result["likelihood_information_quality"]["metar_strong"]["quality"] == 0.0


def test_pipeline_uses_valid_candidate_map_live_after_phase_guard_and_keeps_baseline_for_rollback():
    buckets = ["32°C", "33°C", "34°C", "35°C", "36°C", "37°C", "38°C", "39°C", "40°C", "41°C or higher"]
    result = build_hierarchical_posterior_pipeline(
        buckets=buckets,
        prior_distribution={bucket: 1.0 / len(buckets) for bucket in buckets},
        event_date="2026-07-08",
        as_of="2026-07-08T06:35:00Z",
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_0600",
                "source_observation_id": "srcobs_amsc_0600",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "station_id": "ZUUU",
                "temperature_c": 36.0,
                "observed_today_max_c": 36.0,
                "valid_time": "2026-07-08T06:00:00Z",
                "fetched_at": "2026-07-08T06:01:00Z",
                "parsed_at": "2026-07-08T06:01:00Z",
                "payload": {"raw_text": "METAR ZUUU 080600Z 00000MPS CAVOK 36/20 Q1000="},
            },
            {
                "source_event_id": "srcevt_amsc_0630",
                "source_observation_id": "srcobs_amsc_0630",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "station_id": "ZUUU",
                "temperature_c": 37.0,
                "observed_today_max_c": 37.0,
                "valid_time": "2026-07-08T06:30:00Z",
                "fetched_at": "2026-07-08T06:31:00Z",
                "parsed_at": "2026-07-08T06:31:00Z",
                "payload": {"raw_text": "METAR ZUUU 080630Z 00000MPS CAVOK 37/20 Q1000="},
            },
        ],
    )

    strong = result["runtime_outputs"]["metar_strong_likelihood"]
    candidate_map = strong["daily_high_observation_candidate"]["metar_strong_likelihood_candidate"]["likelihood_map"]
    rollout = result["daily_high_candidate_live_rollout"]
    assert result["phase"] == "D0_late"
    assert rollout["state"] == "applied"
    assert rollout["applied_to_metar_strong_likelihood"] is True
    assert rollout["candidate_contract_ok"] is True
    assert strong["metar_strong_likelihood_map"] == candidate_map
    assert strong["metar_strong_likelihood_baseline_map"]
    assert strong["daily_high_observation_candidate"]["candidate_only"] is True
    assert strong["daily_high_observation_candidate"]["production_applied"] is False
    assert result["boundary"]["daily_high_candidate_never_bypasses_auto_trade_decisions"] is True


def test_pipeline_shadow_mode_keeps_baseline_even_when_candidate_contract_is_valid(monkeypatch):
    monkeypatch.setenv("PM_ZUUU_DAILY_HIGH_CANDIDATE_LIVE_MODE", "shadow")
    buckets = ["32°C", "33°C", "34°C", "35°C", "36°C", "37°C", "38°C", "39°C", "40°C", "41°C or higher"]
    source_observations = [
        {
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "station_id": "ZUUU",
            "temperature_c": temperature_c,
            "observed_today_max_c": temperature_c,
            "valid_time": valid_time,
            "fetched_at": fetched_at,
            "parsed_at": fetched_at,
            "payload": {"raw_text": raw_text},
        }
        for temperature_c, valid_time, fetched_at, raw_text in (
            (36.0, "2026-07-08T06:00:00Z", "2026-07-08T06:01:00Z", "METAR ZUUU 080600Z 00000MPS CAVOK 36/20 Q1000="),
            (37.0, "2026-07-08T06:30:00Z", "2026-07-08T06:31:00Z", "METAR ZUUU 080630Z 00000MPS CAVOK 37/20 Q1000="),
        )
    ]
    result = build_hierarchical_posterior_pipeline(
        buckets=buckets,
        prior_distribution={bucket: 1.0 / len(buckets) for bucket in buckets},
        event_date="2026-07-08",
        as_of="2026-07-08T06:35:00Z",
        source_observations=source_observations,
    )

    strong = result["runtime_outputs"]["metar_strong_likelihood"]
    assert result["daily_high_candidate_live_rollout"]["state"] == "shadow_or_baseline_mode"
    assert strong["metar_strong_likelihood_map"] == strong["metar_strong_likelihood_baseline_map"]


def test_future_forecast_fetch_time_zeroes_effective_weak_beta_without_touching_hard_mask():
    result = build_hierarchical_posterior_pipeline(
        buckets=BUCKETS,
        prior_distribution={bucket: 0.2 for bucket in BUCKETS},
        event_date="2026-07-03",
        as_of="2026-07-03T12:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_future_model",
                "source_id": "current_model_ensemble",
                "source_class": "forecast_weak_source",
                "forecast_peak_c": 33.0,
                "fetched_at": "2026-07-03T13:00:00+08:00",
            },
            {
                "source_event_id": "srcevt_hard_now",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 31.0,
                "observed_today_max_c": 31.0,
                "valid_time": "2026-07-03T04:00:00Z",
                "fetched_at": "2026-07-03T04:01:00Z",
                "payload": {"raw_text": "METAR ZUUU 030400Z 00000MPS CAVOK 31/22 Q1000="},
            },
        ],
    )

    quality = result["likelihood_information_quality"]
    assert result["beta_weak_base"] == 0.2
    assert result["beta_weak"] == 0.0
    assert quality["forecast_weak"]["quality"] == 0.0
    assert "included_source_missing" in quality["forecast_weak"]["na_reasons"]
    assert result["runtime_outputs"]["forecast_weak_likelihood"]["source_trace"] == []
    assert result["beta_strong_base"] == 0.85
    assert result["beta_strong"] == 0.7225
    assert quality["metar_strong"]["quality"] == 0.85
    assert result["posterior_temperature_distribution_map"]["29"] == 0.0
    assert result["posterior_temperature_distribution_map"]["30"] == 0.0


def test_missing_strong_observation_zeroes_effective_strong_beta():
    result = build_hierarchical_posterior_pipeline(
        buckets=BUCKETS,
        prior_distribution={bucket: 0.2 for bucket in BUCKETS},
        event_date="2026-07-03",
        as_of="2026-07-03T12:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_model_only",
                "source_id": "current_model_ensemble",
                "source_class": "forecast_weak_source",
                "forecast_peak_c": 32.0,
            }
        ],
    )

    assert result["beta_strong_base"] == 0.85
    assert result["beta_strong"] == 0.0
    assert result["likelihood_information_quality"]["metar_strong"]["state"] == "observation_missing"


def test_strong_quality_uses_metar_valid_time_instead_of_recent_refetch_time():
    result = build_hierarchical_posterior_pipeline(
        buckets=BUCKETS,
        prior_distribution={bucket: 0.2 for bucket in BUCKETS},
        event_date="2026-07-03",
        as_of="2026-07-03T04:00:00Z",
        source_observations=[
            {
                "source_event_id": "srcevt_refetched_stale_metar",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 31.0,
                "observed_today_max_c": 31.0,
                "valid_time": "2026-07-03T00:00:00Z",
                "fetched_at": "2026-07-03T03:59:00Z",
                "payload": {"raw_text": "METAR ZUUU 030000Z 00000MPS CAVOK 31/22 Q1000="},
            }
        ],
    )

    quality = result["likelihood_information_quality"]["metar_strong"]
    assert quality["quality"] == 0.5
    assert result["beta_strong_base"] == 0.85
    assert result["beta_strong"] == 0.425
    assert quality["source_freshness"][0]["freshness_time_field"] == "valid_time"
    assert quality["source_freshness"][0]["age_hours"] == 4.0


def test_model_and_taf_use_symmetric_weak_pool_without_legacy_special_case():
    buckets = [f"{value}°C" for value in range(33, 42)]
    result = build_hierarchical_posterior_pipeline(
        buckets=buckets,
        prior_distribution={bucket: 1.0 / len(buckets) for bucket in buckets},
        event_date="2026-07-11",
        as_of="2026-07-10T13:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_model_conflict_pipeline",
                "source_id": "current_model_ensemble",
                "source_class": "forecast_weak_source",
                "event_date": "2026-07-11",
                "forecast_peak_c": 35.0,
                "sigma_c": 0.9,
                "confidence": 0.8,
                "fetched_at": "2026-07-10T04:30:00Z",
            },
            {
                "source_event_id": "srcevt_taf_conflict_pipeline",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "event_date": "2026-07-11",
                "forecast_peak_c": 39.0,
                "sigma_c": 0.9,
                "confidence": 0.8,
                "fetched_at": "2026-07-10T04:35:00Z",
            },
        ],
    )

    assert result["phase"] == "D1_late"
    assert result["beta_weak_base"] == 0.85
    assert result["beta_weak"] == 0.85
    assert "forecast_model_taf_conflict" not in result
    assert result["joint_weak_model"]["weak_source_set"]["source_set_key"] == "gfs+taf"
    assert result["boundary"]["unique_joint_weak_model_consumed"] is True
    assert result["likelihood_information_quality"]["legacy_model_taf_special_case_retired"] is True


def test_hard_lock_bypasses_quality_tempering_and_keeps_final_bucket_only():
    result = build_hierarchical_posterior_pipeline(
        buckets=BUCKETS,
        prior_distribution={bucket: 0.2 for bucket in BUCKETS},
        event_date="2026-07-03",
        as_of="2026-07-03T15:00:00+08:00",
        observation_inputs={
            "current_temp_c": 32.0,
            "observed_today_max_c": 32.0,
            "observed_today_max_final": True,
        },
    )

    assert result["phase"] == "D0_hard"
    assert result["beta_weak"] == 0.0
    assert result["beta_strong"] == 1.0
    assert result["likelihood_information_quality"]["hard_mask_not_tempered_by_quality"] is True
    assert result["posterior_temperature_distribution_map"]["32"] == 1.0
