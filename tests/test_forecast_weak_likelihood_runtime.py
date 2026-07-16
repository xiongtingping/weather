import math

from forecast_weak_likelihood_runtime import (
    CONTRACT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    build_forecast_weak_likelihood,
    build_forecast_weak_likelihood_contract,
)
from weather_source_acquisition_manager import build_source_acquisition_trace, source_payloads_from_fact_payload


BUCKETS = ["30", "31", "32", "33"]


def _likelihood_map(result):
    return result["forecast_weak_likelihood_map"]


def test_builds_forecast_weak_likelihood_from_model_taf_and_twc_sources():
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        phase="D1_late",
        source_observations=[
            {
                "source_event_id": "srcevt_model",
                "source_id": "current_model_ensemble",
                "source_class": "forecast_weak_source",
                "role": "model_forecast_weak_likelihood",
                "forecast_peak_c": 32.2,
                "sigma_c": 0.9,
                "confidence": 0.7,
            },
            {
                "source_event_id": "srcevt_taf",
                "source_id": "zuuu_taf_forecast_peak",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood",
                "forecast_peak_c": 32.8,
                "sigma_c": 0.8,
                "confidence": 0.6,
            },
            {
                "source_event_id": "srcevt_twc",
                "source_id": "twc_forecast_peak",
                "source_class": "forecast_weak_source",
                "role": "twc_forecast_weak_likelihood",
                "forecast_peak_c": 32.6,
                "sigma_c": 1.0,
                "confidence": 0.5,
            },
        ],
    )

    assert result["schema"] == SCHEMA_VERSION
    assert result["read_only"] is True
    assert result["production_entry_enabled"] is False
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["places_orders"] is False
    assert math.isclose(result["normalization"]["forecast_weak_likelihood_sum"], 1.0, abs_tol=1e-12)
    assert _likelihood_map(result)["32"] > _likelihood_map(result)["30"]
    assert {row["group"] for row in result["source_group_trace"]} == {"model", "taf", "twc_forecast"}
    contract = build_forecast_weak_likelihood_contract(result)
    assert contract["schema"] == CONTRACT_SCHEMA_VERSION
    assert contract["ok"] is True
    assert contract["state"] == "ready"


def test_duplicate_model_sources_are_group_capped_and_shrunk():
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        source_observations=[
            {
                "source_event_id": f"srcevt_model_{idx}",
                "source_id": source_id,
                "source_class": "forecast_weak_source",
                "role": "model_forecast_weak_likelihood",
                "forecast_peak_c": 33.0,
                "sigma_c": 0.35,
                "confidence": 1.0,
            }
            for idx, source_id in enumerate(["ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"])
        ],
    )

    model_group = [row for row in result["source_group_trace"] if row["group"] == "model"][0]
    assert model_group["source_count"] == 3
    assert model_group["cap_hit"] is True
    assert model_group["capped_precision"] == model_group["precision_cap"]
    assert model_group["shrinkage_alpha"] < 1.0
    assert _likelihood_map(result)["33"] < 0.95


def test_diagnostic_current_hard_observation_and_market_sources_are_excluded():
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        source_observations=[
            {
                "source_event_id": "srcevt_twc_current",
                "source_id": "twc_current_icao_metric",
                "source_class": "diagnostic_current_source",
                "role": "external_current_latency_diagnostic",
                "forecast_peak_c": 33.5,
            },
            {
                "source_event_id": "srcevt_awc",
                "source_id": "awc_cache_metars_csv_gz",
                "source_class": "hard_observation_candidate",
                "role": "metar_hard_observation_cache",
                "forecast_peak_c": 33.5,
            },
            {
                "source_event_id": "srcevt_market",
                "source_id": "market_realtime_orderbook",
                "source_class": "market_source",
                "role": "market_execution_context",
                "forecast_peak_c": 33.5,
            },
            {
                "source_event_id": "srcevt_taf",
                "source_id": "zuuu_taf_forecast_peak",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood",
                "forecast_peak_c": 31.0,
            },
        ],
    )

    excluded = [row for row in result["source_trace"] if not row["included"]]
    assert len(excluded) == 3
    assert all(row["exclusion_reason"] == "source_class_not_forecast_weak" for row in excluded)
    assert result["boundary"]["diagnostic_current_sources_excluded"] is True
    assert result["boundary"]["hard_observation_sources_excluded"] is True
    assert {row["group"] for row in result["source_group_trace"]} == {"taf"}
    assert build_forecast_weak_likelihood_contract(result)["ok"] is True


def test_amsc_taf_is_weak_likelihood_and_amsc_metar_is_excluded_from_weak_layer():
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_metar",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "role": "metar_hard_observation",
                "temperature_c": 36.0,
            },
            {
                "source_event_id": "srcevt_amsc_taf",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood",
                "forecast_peak_c": 33.0,
                "sigma_c": 0.8,
            },
        ],
    )

    included = [row for row in result["source_trace"] if row["included"]]
    excluded = [row for row in result["source_trace"] if not row["included"]]
    assert [row["source_id"] for row in included] == ["zuuu_taf_forecast_amsc"]
    assert included[0]["group"] == "taf"
    assert [row["source_id"] for row in excluded] == ["zuuu_metar_raw_amsc"]
    assert excluded[0]["exclusion_reason"] == "source_class_not_forecast_weak"


def test_amsc_taf_suppresses_awc_reference_taf_when_both_available():
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        source_observations=[
            {
                "source_event_id": "srcevt_awc_taf",
                "source_id": "zuuu_taf_forecast_peak",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood_reference",
                "forecast_peak_c": 31.0,
            },
            {
                "source_event_id": "srcevt_amsc_taf",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood_primary",
                "forecast_peak_c": 33.0,
            },
        ],
    )

    included = [row for row in result["source_trace"] if row["included"]]
    excluded = [row for row in result["source_trace"] if not row["included"]]
    assert [row["source_id"] for row in included] == ["zuuu_taf_forecast_amsc"]
    assert excluded[0]["source_id"] == "zuuu_taf_forecast_peak"
    assert excluded[0]["exclusion_reason"] == "taf_reference_suppressed_by_amsc_primary"
    assert excluded[0]["reference_suppressed_by"] == "zuuu_taf_forecast_amsc"


def test_taf_peak_keeps_enough_contrast_against_far_lower_edge_bucket():
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
    result = build_forecast_weak_likelihood(
        buckets=buckets,
        phase="D0_pre_dawn",
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_taf",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood",
                "forecast_peak_c": 38.0,
            }
        ],
    )

    likelihood = result["forecast_weak_likelihood_map"]
    assert likelihood["38°C"] > likelihood["33°C or below"]
    assert likelihood["38°C"] / likelihood["33°C or below"] > 300.0
    taf_group = result["source_group_trace"][0]
    assert taf_group["group"] == "taf"
    assert taf_group["config"]["shrinkage_strength"] == 0.01


def test_missing_or_unusable_sources_return_neutral_likelihood_with_na_reason():
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        source_observations=[
            {
                "source_event_id": "srcevt_missing",
                "source_id": "weather_com_forecast_peak",
                "source_class": "forecast_weak_source",
                "role": "twc_forecast_weak_likelihood",
            }
        ],
    )

    assert result["na_reasons"] == ["forecast_weak_sources_missing_or_unusable"]
    assert _likelihood_map(result) == {"30": 0.25, "31": 0.25, "32": 0.25, "33": 0.25}
    assert result["source_trace"][0]["exclusion_reason"] == "forecast_peak_c_missing"


def test_group_config_override_can_lower_precision_cap():
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        group_config={"taf": {"precision_cap": 0.2, "shrinkage_strength": 0.8}},
        source_observations=[
            {
                "source_event_id": "srcevt_taf",
                "source_id": "zuuu_taf_forecast_peak",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood",
                "forecast_peak_c": 33.0,
                "sigma_c": 0.25,
                "confidence": 1.0,
            }
        ],
    )

    taf_group = result["source_group_trace"][0]
    assert taf_group["group"] == "taf"
    assert taf_group["capped_precision"] == 0.2
    assert taf_group["cap_hit"] is True
    assert taf_group["shrinkage_alpha"] == 0.2


def test_source_manager_fact_payload_sources_feed_forecast_weak_likelihood_groups():
    trace = build_source_acquisition_trace(
        sources=source_payloads_from_fact_payload(
            {
                "weather_likelihood_features": {
                    "wu_twc_observed_intraday_current_temp_c": 30.0,
                    "wu_twc_observed_intraday_current_at": "2026-07-03T09:10:00Z",
                    "wu_twc_observed_intraday_source_url": "https://www.wunderground.com/weather/cn/chengdu",
                    "wu_forecast_peak_c": 32.0,
                    "wu_forecast_peak_at": "2026-07-03T07:00:00Z",
                    "wu_forecast_peak_source": "wu:test",
                    "twc_forecast_peak_c": 33.0,
                    "twc_forecast_peak_valid_local": "2026-07-03T15:00:00+08:00",
                    "twc_forecast_peak_source_url": "https://weather.com",
                    "taf_forecast_peak_c": 31.0,
                    "taf_forecast_peak_raw": "TAF ZUUU 0300/0324 TX31/0307Z",
                },
                "model_members_decimal_raw": {
                    "gfs_seamless": [31.8, 32.0, 32.2],
                    "icon_seamless": [30.8, 31.0, 31.2],
                },
            },
            station_id="ZUUU",
            event_date="2026-07-03",
            fact_as_of="2026-07-03T09:05:36Z",
        ),
        station_id="ZUUU",
        event_date="2026-07-03",
        fetched_at="2026-07-03T09:05:36Z",
    )
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        source_observations=trace["source_observations"],
    )

    groups = {row["group"] for row in result["source_group_trace"]}
    assert groups == {"model", "taf", "twc_forecast", "wu_forecast"}
    excluded = [row for row in result["source_trace"] if not row["included"]]
    assert excluded[0]["source_id"] == "wu_twc_observed_intraday"
    assert excluded[0]["exclusion_reason"] == "source_class_not_forecast_weak"
    assert result["normalization"]["forecast_weak_likelihood_sum"] == 1.0


def test_forecast_weak_contract_excludes_forecast_source_marked_hard_eligible():
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        source_observations=[
            {
                "source_event_id": "srcevt_bad_forecast",
                "source_id": "twc_forecast_peak",
                "source_class": "forecast_weak_source",
                "role": "twc_forecast_weak_likelihood",
                "forecast_peak_c": 33.0,
                "hard_eligible": True,
            }
        ],
    )

    assert result["source_trace"][0]["included"] is False
    assert result["source_trace"][0]["exclusion_reason"] == "forecast_source_marked_hard_eligible"
    assert result["na_reasons"] == ["forecast_weak_sources_missing_or_unusable"]
    assert build_forecast_weak_likelihood_contract(result)["ok"] is True


def test_forecast_weak_contract_marks_hard_observation_fields_ignored():
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        source_observations=[
            {
                "source_event_id": "srcevt_forecast_with_current_field",
                "source_id": "wu_forecast_high",
                "source_class": "forecast_weak_source",
                "role": "wu_forecast_weak_likelihood",
                "forecast_peak_c": 32.0,
                "observed_today_max_c": 35.0,
                "temperature_c": 35.0,
            }
        ],
    )

    trace = result["source_trace"][0]
    assert trace["included"] is True
    assert sorted(trace["hard_observation_fields_present"]) == ["observed_today_max_c", "temperature_c"]
    assert trace["hard_observation_fields_ignored"] is True
    assert build_forecast_weak_likelihood_contract(result)["ok"] is True


def test_model_and_taf_use_symmetric_group_pool_without_legacy_double_peak_special_case():
    buckets = [f"{value}°C" for value in range(33, 42)]
    result = build_forecast_weak_likelihood(
        buckets=buckets,
        event_date="2026-07-10",
        source_observations=[
            {
                "source_event_id": "srcevt_model_conflict",
                "source_id": "current_model_ensemble",
                "source_class": "forecast_weak_source",
                "event_date": "2026-07-10",
                "forecast_peak_c": 35.0,
                "sigma_c": 0.9,
                "confidence": 0.8,
            },
            {
                "source_event_id": "srcevt_taf_conflict",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "event_date": "2026-07-10",
                "forecast_peak_c": 39.0,
                "sigma_c": 0.9,
                "confidence": 0.8,
            },
        ],
    )

    likelihood = result["forecast_weak_likelihood_map"]
    assert "model_taf_conflict" not in result
    assert result["forecast_group_pooling"] == {
        "schema": "forecast_group_precision_pool_v1",
        "state": "available",
        "pooling_mode": "symmetric_group_precision_geometric_pool",
        "included_groups": ["model", "taf"],
        "source_group_count": 2,
        "legacy_model_taf_special_case_retired": True,
    }
    assert math.isclose(sum(likelihood.values()), 1.0, abs_tol=1e-9)
    assert result["source_group_trace"][0]["group"] == "model"
    assert result["source_group_trace"][1]["group"] == "taf"
    assert build_forecast_weak_likelihood_contract(result)["ok"] is True


def test_disagreement_does_not_create_a_model_taf_special_branch():
    result = build_forecast_weak_likelihood(
        buckets=[f"{value}°C" for value in range(33, 41)],
        source_observations=[
            {
                "source_event_id": "srcevt_model_moderate",
                "source_id": "current_model_ensemble",
                "source_class": "forecast_weak_source",
                "forecast_peak_c": 36.0,
                "sigma_c": 0.9,
                "confidence": 0.8,
            },
            {
                "source_event_id": "srcevt_taf_moderate",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "forecast_peak_c": 38.0,
                "sigma_c": 0.9,
                "confidence": 0.8,
            },
        ],
    )

    assert "model_taf_conflict" not in result
    assert result["formula"] == "symmetric_group_precision_geometric_pool_with_group_precision_caps"
    assert result["boundary"]["legacy_model_taf_special_case_retired"] is True


def test_forecast_event_date_mismatch_is_excluded_before_symmetric_group_pooling():
    result = build_forecast_weak_likelihood(
        buckets=BUCKETS,
        event_date="2026-07-10",
        source_observations=[
            {
                "source_event_id": "srcevt_wrong_day_taf",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "event_date": "2026-07-11",
                "forecast_peak_c": 33.0,
            }
        ],
    )

    assert result["source_trace"][0]["included"] is False
    assert result["source_trace"][0]["exclusion_reason"] == "forecast_event_date_mismatch"
    assert result["forecast_group_pooling"]["state"] == "not_available"
