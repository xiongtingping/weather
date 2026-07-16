from scripts import hierarchical_probability_unit_contract_audit as audit


def test_hierarchical_probability_unit_contract_audit_passes_runtime_semantics():
    report = audit.build_hierarchical_probability_unit_contract_audit()

    assert report["schema"] == audit.SCHEMA_VERSION
    assert report["ok"] is True
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["fetches_external_weather_sources"] is False
    assert report["guards"]["places_orders"] is False
    assert report["checks"] == {"prior": True, "likelihood": True, "posterior": True}
    assert report["coverage"]["checked_fixture_count"] >= 20
    assert report["blockers"] == []

    prior = report["stage_reports"]["prior"]
    likelihood = report["stage_reports"]["likelihood"]
    posterior = report["stage_reports"]["posterior"]

    assert prior["checks"]["prior_boundary_excludes_forecast_metar_market"] is True
    assert prior["checks"]["prior_guards_exclude_today_sources"] is True
    assert likelihood["checks"]["forecast_group_precision_cap_applied"] is True
    assert likelihood["checks"]["temperature_trajectory_is_internal_deduped_feature"] is True
    assert likelihood["checks"]["short_window_momentum_not_overconfirmed"] is True
    assert posterior["checks"]["beta_zero_makes_weak_layer_neutral"] is True
    assert posterior["checks"]["hard_mask_conditions_bucket_and_continuous_distribution"] is True
    assert posterior["checks"]["legacy_bucket_product_retired_from_live_composer"] is True
    assert posterior["checks"]["d0_uses_distinct_valid_time_sequential_state"] is True
