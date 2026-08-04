from __future__ import annotations

import ast
import json
from pathlib import Path

from basket_strategy import (
    build_basket_yes_plan,
    build_plan_from_weather_snapshot,
    classify_regime_direction,
    extract_probability_projection,
    extract_model_taf_inputs,
)


def _request(*, center=35, anchor=34.0, price=0.10, regime="clear_dry"):
    buckets = [
        {"outcome": "30°C or below", "bucket_c": 30, "kind": "lower_tail", "yes_token": "yl", "no_token": "nl"}
    ]
    for degree in range(31, 38):
        buckets.append(
            {
                "outcome": f"{degree}°C",
                "bucket_c": degree,
                "kind": "single",
                "yes_token": f"y{degree}",
                "no_token": f"n{degree}",
                "yes_ask": price,
                "yes_ask_size": 100.0,
            }
        )
    buckets.append(
        {"outcome": "38°C or higher", "bucket_c": 38, "kind": "upper_tail", "yes_token": "yu", "no_token": "nu"}
    )
    probabilities = {str(degree): 0.02 for degree in range(31, 38)}
    probabilities[str(center)] = 0.55
    remaining = 0.45 / 6.0
    for degree in range(31, 38):
        if degree != center:
            probabilities[str(degree)] = remaining
    return {
        "as_of": "2026-07-18T00:00:00Z",
        "event": {"event_id": "event-1", "market_unit": "C", "market_anchor_c": anchor},
        "probability_projection": {
            "p1": {
                "probability_stage": "P1",
                "posterior_checkpoint_id": "p1-checkpoint",
                "posterior_run_id": "p1-run",
                "market_bucket_probabilities": {
                    f"{degree}°C": probability for degree, probability in probabilities.items()
                },
            },
            "p0": {
                "probability_stage": "P0",
                "market_bucket_probabilities": {
                    f"{degree}°C": probability for degree, probability in probabilities.items()
                },
            },
        },
        "report": {
            "raw_text": "METAR ZUUU 180000Z 00000MPS CAVOK 32/20 Q1000=",
            "expected_station": "ZUUU",
            "source_lane": "awc",
            "observation_id": "obs-1",
        },
        "buckets": buckets,
        "regime": {"forecast_weather_regime": regime} if regime else {},
    }


def test_pure_basket_is_single_shot_and_uses_up_bias():
    plan = build_basket_yes_plan(_request())

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["direction"] == "UP"
    assert plan["direction_bias"]["direction_state"] == "UP"
    assert plan["direction_bias"]["direction_horizon"] == "tomorrow"
    assert plan["basket"]["selected_buckets"] == ["34°C", "35°C", "36°C"]
    assert plan["basket"]["center_bucket"] == "35°C"
    assert plan["basket"]["posterior_stage"] == "P1"
    assert plan["plan_state"] == "BASKET_READY"
    assert plan["state_machine"]["next"] == ["FROZEN"]
    assert plan["basket"]["execution"]["single_shot"] is True
    assert plan["basket"]["execution"]["staged_build"] is False
    assert plan["basket"]["execution"]["selective_pairing"] is False
    assert plan["basket"]["execution"]["merge_enabled"] is False
    assert plan["basket"]["guaranteed_profit_usdc"] > 0
    assert plan["live_submit_authorized"] is False


def test_dynamic_convergence_advances_widest_to_narrowest_and_allows_edge_center():
    request = _request(center=35, anchor=34.0)
    posterior = request["probability_projection"]["p1"]["market_bucket_probabilities"]
    for label in list(posterior):
        posterior[label] = 0.01
    posterior.update({"33°C": 0.03, "34°C": 0.10, "35°C": 0.76, "36°C": 0.06, "37°C": 0.03})
    request["config"] = {
        "dynamic_convergence_enabled": True,
        "convergence_widths": [4, 3, 2],
        "convergence_target_4": 0.60,
        "convergence_target_3": 0.72,
        "convergence_target_2": 0.84,
        "bankroll_usdc": 30,
        "tranche_budget_usdc": 10,
    }
    # b2 is always the primary core.  The window moves to [34..37] so b2's
    # posterior is not silently demoted below b3's posterior.
    expected = [([ ], 4, ["34°C", "35°C", "36°C", "37°C"]), ([4], 3, ["34°C", "35°C", "36°C"]), ([4, 3], 2, ["34°C", "35°C"])]
    for completed, width, buckets in expected:
        request["completed_tranche_widths"] = completed
        plan = build_basket_yes_plan(request)
        assert plan["status"] == "ready"
        assert plan["basket"]["tranche_width"] == width
        assert plan["basket"]["current_stage_width"] == width
        assert plan["basket"]["leg_count"] == width
        assert plan["basket"]["selected_buckets"] == buckets
        assert plan["basket"]["execution"]["staged_build"] is True


def test_edge_centre_can_anchor_a_contiguous_basket():
    request = _request(center=31, anchor=34.0, regime="cloudy_stable")
    request["report"] = None
    request["buckets"][0].update({"yes_ask": 0.10, "yes_ask_size": 100.0})
    request["probability_projection"]["p1"]["market_bucket_probabilities"] = {
        "30°C or below": 0.70,
        "31°C": 0.10,
        "32°C": 0.08,
        "33°C": 0.06,
        "34°C": 0.04,
        "35°C": 0.02,
    }

    plan = build_basket_yes_plan(request)

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["center_bucket"] == "30°C or below"
    assert plan["basket"]["selected_buckets"] == ["30°C or below", "31°C", "32°C"]


def test_dynamic_convergence_fails_closed_when_stage_coverage_is_below_target():
    request = _request(center=35, anchor=34.0)
    request["config"] = {
        "dynamic_convergence_enabled": True,
        "convergence_widths": [4, 3, 2],
        "convergence_target_4": 0.90,
        "convergence_target_3": 0.95,
        "convergence_target_2": 0.99,
        "bankroll_usdc": 30,
        "tranche_budget_usdc": 10,
    }
    plan = build_basket_yes_plan(request)
    assert plan["status"] == "blocked"
    assert "convergence_coverage_below_stage_target" in plan["basket"]["reason_codes"]


def test_dynamic_addition_reopens_only_narrowest_stage_after_all_stages():
    request = _request(center=35, anchor=34.0)
    posterior = request["probability_projection"]["p1"]["market_bucket_probabilities"]
    for label in list(posterior):
        posterior[label] = 0.01
    posterior.update({"33°C": 0.03, "34°C": 0.10, "35°C": 0.76, "36°C": 0.06, "37°C": 0.03})
    request["completed_tranche_widths"] = [4, 3, 2]
    request["config"] = {
        "dynamic_convergence_enabled": True,
        "allow_dynamic_additions": True,
        "convergence_widths": [4, 3, 2],
        "convergence_target_4": 0.60,
        "convergence_target_3": 0.72,
        "convergence_target_2": 0.84,
        "bankroll_usdc": 30,
        "tranche_budget_usdc": 10,
    }
    plan = build_basket_yes_plan(request)
    assert plan["status"] == "ready"
    assert plan["basket"]["tranche_width"] == 2
    assert plan["basket"]["execution"]["dynamic_additions"] is True


def test_awc_report_floor_excludes_impossible_yes_buckets_without_creating_no_orders():
    plan = build_basket_yes_plan(_request(center=33, anchor=34.0, regime="cloud_limited_or_rain_cooled"))

    assert plan["official_report_floor"]["status"] == "available"
    assert plan["official_report_floor"]["reported_floor_market_unit"] == 32
    assert plan["basket"]["center_bucket"] == "33°C"
    assert all(int(label[:-2]) >= 32 for label in plan["basket"]["selected_buckets"])
    assert "hard_no" not in plan
    assert all(order["side"] == "YES" and order["order_type"] == "FOK" for order in plan["basket"]["orders"])


def test_neutral_direction_is_blocked_instead_of_guessing():
    plan = build_basket_yes_plan(_request(center=34, anchor=34.0))

    assert plan["direction_bias"]["direction"] == "BLOCKED"
    assert plan["basket"]["status"] == "blocked"
    assert "probability_center_regime_conflict" in plan["direction_bias"]["reason_codes"]


def test_p1_is_preferred_and_p0_is_used_only_when_p1_is_absent():
    request = _request()
    del request["probability_projection"]["p1"]
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["posterior_stage"] == "P0"


def test_normalized_projection_with_lineage_uses_p0_without_recursive_fallback():
    request = _request()
    normalized = request["probability_projection"]["p0"]
    request["probability_projection"] = {
        "status": "available",
        "stage": "P0",
        "probabilities": normalized["market_bucket_probabilities"],
        "posterior_run_id": "normalized-p0-run",
    }

    plan = build_basket_yes_plan(request)

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["posterior_stage"] == "P0"


def test_cyclic_projection_mapping_fails_closed_without_recursion_error():
    cyclic = {"probability_stage": "P1", "posterior_run_id": "cyclic-p1"}
    cyclic["resolution_bridge"] = cyclic

    projection = extract_probability_projection({"probability_projection": cyclic})

    assert projection["status"] == "blocked"
    assert projection["reason_codes"] == ["p1_market_bucket_projection_missing"]


def test_p1_center_overrides_p0_and_no_mean_or_direction_shift_is_applied():
    request = _request(center=35)
    p1 = request["probability_projection"]["p1"]
    p1["market_bucket_probabilities"] = {
        "34°C": 0.05,
        "35°C": 0.10,
        "36°C": 0.60,
        "37°C": 0.15,
        "33°C": 0.10,
    }
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["posterior_stage"] == "P1"
    assert plan["direction_bias"]["center_bucket"] == "36°C"
    assert plan["basket"]["selected_buckets"] == ["35°C", "36°C", "37°C"]


def test_invalid_p1_does_not_silently_fallback_to_p0():
    request = _request()
    request["probability_projection"]["p1"]["market_bucket_probabilities"] = {"35°C": 0.4}
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "blocked"
    assert plan["direction_bias"]["posterior_stage"] == "P1"
    assert any(reason.startswith("p1_probability_bucket_distribution") for reason in plan["direction_bias"]["reason_codes"])


def test_tail_mode_remains_the_probability_centre_without_silent_exact_fallback():
    request = _request()
    p1 = request["probability_projection"]["p1"]
    p1["market_bucket_probabilities"] = {
        "30°C or below": 0.60,
        "34°C": 0.10,
        "35°C": 0.10,
        "36°C": 0.10,
        "37°C": 0.10,
    }
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "blocked"
    assert plan["direction_bias"]["center_bucket"] == "30°C or below"
    assert plan["direction_bias"]["center_bucket_c"] == 30
    assert "probability_center_regime_conflict" in plan["direction_bias"]["reason_codes"]


def test_materialized_successor_checkpoint_is_read_as_p1_projection():
    request = _request()
    p1 = request["probability_projection"]["p1"]
    request["probability_projection"] = {
        "checkpoint": {
            "checkpoint_kind": "successor",
            "posterior_run_id": "successor-p1",
            "resolution_bridge": {
                "market_bucket_probabilities": p1["market_bucket_probabilities"],
            },
        }
    }
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["posterior_stage"] == "P1"
    assert plan["direction_bias"]["posterior_run_id"] == "successor-p1"


def test_materialized_checkpoint_json_distribution_is_read_without_rebuilding_posterior():
    request = _request()
    p1 = request["probability_projection"]["p1"]
    request["probability_projection"] = {
        "checkpoint_kind": "successor",
        "posterior_run_id": "successor-p1-json",
        "market_bucket_distribution_json": json.dumps(p1["market_bucket_probabilities"]),
    }
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["posterior_stage"] == "P1"


def test_missing_p0_and_p1_blocks_without_point_forecast_fallback():
    request = _request()
    request["probability_projection"] = {}
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "blocked"
    assert "p1_and_p0_market_bucket_projection_missing" in plan["direction_bias"]["reason_codes"]


def test_regime_agreement_keeps_tomorrow_up_direction():
    request = _request()
    request["regime"] = {"forecast_weather_regime": "clear_dry", "basin_regime": "weak_ventilation_heat_buildup"}
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["direction_state"] == "UP"
    assert plan["direction_bias"]["regime_gate"] == "agree"


def test_regime_conflict_blocks_instead_of_flipping_tomorrow_direction():
    request = _request()
    request["regime"] = {"forecast_weather_regime": "convection_or_showers"}
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "blocked"
    assert plan["direction_bias"]["center_bucket"] == "35°C"
    assert plan["direction_bias"]["direction_state"] == "BLOCKED"
    assert "probability_center_regime_conflict" in plan["direction_bias"]["reason_codes"]


def test_regime_gate_can_be_disabled_without_changing_probability_center_direction():
    request = _request(center=33, anchor=34.0, regime="clear_dry")
    request["config"] = {"regime_gate_enabled": False}
    request["regime"] = {"forecast_weather_regime": "convection_or_showers"}

    plan = build_basket_yes_plan(request)

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["direction_state"] == "DOWN"
    assert plan["direction_bias"]["regime_gate"] == "disabled"
    assert plan["direction_bias"]["regime"]["state"] == "DISABLED"
    assert plan["strategy_contract"]["regime_gate_enabled"] is False


def test_regime_gate_disabled_still_blocks_neutral_probability_center():
    request = _request(center=34, anchor=34.0)
    request["config"] = {"regime_gate_enabled": False}

    plan = build_basket_yes_plan(request)

    assert plan["status"] == "blocked"
    assert "probability_center_direction_neutral" in plan["direction_bias"]["reason_codes"]


def test_current_official_physical_regime_labels_can_gate_up_direction():
    request = _request()
    request["regime"] = {
        "basin_regime": "moderate_ventilation",
        "cloud_regime": "forecast_partial_cloud_direct",
    }
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "blocked"
    assert plan["direction_bias"]["regime"]["state"] == "DOWN"
    assert plan["direction_bias"]["regime_gate"] == "conflict_blocked"


def test_regime_classifier_marks_conflicting_existing_labels_explicitly():
    result = classify_regime_direction(
        {
            "physical_regime": {"forecast_weather_regime": "clear_dry"},
            "climo_regime": {"basin_regime": "cloud_limited_or_rain_cooled"},
        }
    )

    assert result["state"] == "CONFLICT"


def test_regime_vector_can_supply_existing_target_day_direction_without_point_forecast():
    result = classify_regime_direction(
        {"regime_probability_vector": {"clear_dry": 0.7, "cloudy_stable": 0.2, "wet_precipitation": 0.1}}
    )

    assert result["state"] == "UP"
    assert result["vector_up_mass"] == 0.7


def test_snapshot_adapter_consumes_registry_and_quote_snapshots_without_live_side_effects():
    base = _request()
    snapshot = {
        "as_of": base["as_of"],
        "event": base["event"],
        "weather_likelihood_features": {
            "gfs_model_peak_c": 35.0,
            "icon_model_peak_c": 35.0,
            "ecmwf_model_peak_c": 34.0,
            "taf_forecast_peak_c": 35.0,
            "forecast_fetched_at": "2026-07-17T23:30:00Z",
        },
        "probability_projection": base["probability_projection"],
        "regime": {"forecast_weather_regime": "clear_dry"},
        "report": base["report"],
        "market_registry": [
            {
                "outcome": row["outcome"],
                "kind": row["kind"],
                "bucket_c": row["bucket_c"],
                "yes_token": row["yes_token"],
                "no_token": row["no_token"],
            }
            for row in base["buckets"]
        ],
        "quote_snapshots": [
            {
                "token": row["yes_token"],
                "order_book": {"asks": [{"price": row.get("yes_ask", 0.1), "size": row.get("yes_ask_size", 100.0)}]},
            }
            for row in base["buckets"]
            if row.get("kind") == "single"
        ]
        + [
            {
                "token": "n31",
                "order_book": {"asks": [{"price": 0.1, "size": 100.0}]},
            }
        ],
    }

    plan = build_plan_from_weather_snapshot(snapshot)

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["direction_state"] == "UP"
    assert plan["basket"]["selected_buckets"] == ["34°C", "35°C", "36°C"]
    assert plan["official_report_floor"]["reported_floor_market_unit"] == 32
    assert plan["guards"]["writes_database"] is False
    assert plan["guards"]["submits_orders"] is False


def test_snapshot_adapter_reads_regime_from_existing_feature_surface():
    base = _request()
    snapshot = {
        "as_of": base["as_of"],
        "event": base["event"],
        "features": {
            "regime_probability_vector": {
                "clear_dry": 0.70,
                "cloudy_stable": 0.15,
                "wet_precipitation": 0.10,
                "convection_or_showers": 0.05,
            }
        },
        "probability_projection": base["probability_projection"],
        "report": base["report"],
        "market_registry": base["buckets"],
        "quote_snapshots": [
            {
                "token": row["yes_token"],
                "order_book": {"asks": [{"price": 0.1, "size": 100.0}]},
            }
            for row in base["buckets"]
            if row.get("kind") == "single"
        ]
        + [{"token": "n31", "order_book": {"asks": [{"price": 0.1, "size": 100.0}]}}],
    }
    plan = build_plan_from_weather_snapshot(snapshot)

    assert plan["status"] == "ready"
    assert plan["direction_bias"]["regime"]["state"] == "UP"


def test_sum_of_yes_asks_at_or_above_one_blocks_worst_case_basket():
    plan = build_basket_yes_plan(_request(price=0.34))

    assert plan["status"] == "blocked"
    assert plan["basket"]["status"] == "blocked"
    assert "basket_worst_case_floor_not_profitable" in plan["basket"]["reason_codes"]


def test_existing_yes_inventory_neither_creates_no_orders_nor_blocks_planning():
    request = _request()
    plan = build_basket_yes_plan(request)

    assert plan["status"] == "ready"
    assert "hard_no" not in plan


def test_existing_feature_adapter_reads_three_models_and_taf():
    extracted = extract_model_taf_inputs(
        {
            "model_members_decimal_raw": {
                "gfs_seamless": [34.0, 35.0],
                "icon_seamless": [35.0, 35.0],
                "ecmwf_ifs025": [33.0, 34.0],
            },
            "taf_forecast_peak_c": 35.0,
            "taf_forecast_peak_source": "zuuu_taf_forecast_amsc",
        }
    )

    assert extracted["gfs_seamless"]["forecast_peak_c"] == 34.5
    assert extracted["icon_seamless"]["forecast_peak_c"] == 35.0
    assert extracted["ecmwf_ifs025"]["forecast_peak_c"] == 33.5
    assert extracted["taf"]["source_id"] == "zuuu_taf_forecast_amsc"


def test_nested_registry_json_is_read_without_live_side_effects():
    base = _request()
    row = base["buckets"][3]
    snapshot = {
        "as_of": base["as_of"],
        "event": base["event"],
        "features": {
            "gfs_model_peak_c": 35.0,
            "icon_model_peak_c": 35.0,
            "ecmwf_model_peak_c": 34.0,
            "taf_forecast_peak_c": 35.0,
            "forecast_fetched_at": "2026-07-17T23:30:00Z",
        },
        "probability_projection": base["probability_projection"],
        "regime": base["regime"],
        "report": base["report"],
        "market_registry": [
            {
                "registry_json": {
                    "outcome": row["outcome"],
                    "kind": row["kind"],
                    "bucket_c": row["bucket_c"],
                },
                "yes_token": row["yes_token"],
                "no_token": row["no_token"],
            }
        ],
    }
    # This intentionally lacks a complete ladder; the adapter must still
    # parse the nested registry row rather than treating the JSON field as the
    # outcome string.
    plan = build_plan_from_weather_snapshot(snapshot)
    assert plan["basket"]["status"] == "blocked"
    assert "insufficient_exact_buckets_after_reported_floor" in plan["basket"]["reason_codes"]


def test_plan_package_has_no_live_or_persistence_imports():
    forbidden_roots = {"sqlite3", "urllib", "requests", "subprocess", "httpx"}
    repo_root = Path(__file__).resolve().parents[1]
    source_paths = list((repo_root / "basket_strategy").glob("*.py"))
    # The production adapter is intentionally the only live/persistence
    # boundary; the planner modules remain side-effect free.
    source_paths = [path for path in source_paths if path.name not in {"production.py", "schema.py"}]
    source_paths.append(repo_root / "scripts" / "prepare_basket_yes_plan.py")
    for source_path in source_paths:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module.split(".")[0]] if node.module else []
            else:
                continue
            assert not forbidden_roots.intersection(imported), (source_path, imported)


def test_plan_contract_always_exposes_zero_live_guards():
    plan = build_basket_yes_plan(_request())
    assert plan["execution_mode"] == "plan_only"
    assert plan["live_submit_authorized"] is False
    assert plan["guards"]["writes_database"] is False
    assert plan["guards"]["reads_order_gateway"] is False
    assert plan["guards"]["submits_orders"] is False
