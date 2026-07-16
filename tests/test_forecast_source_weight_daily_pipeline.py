from scripts.forecast_source_weight_daily_pipeline import build_daily_pipeline_plan


def test_daily_pipeline_plan_is_external_only_and_cannot_apply_production():
    plan = build_daily_pipeline_plan(max_incremental_requests=9)

    assert plan["source_data_policy"] == "external_only"
    assert plan["max_incremental_open_meteo_requests"] == 9
    assert "rolling_oos_and_sequential_ablation" in plan["stages"]
    assert plan["guards"]["changes_parameters"] is False
    assert plan["guards"]["applies_artifact"] is False
    assert plan["guards"]["places_orders"] is False
