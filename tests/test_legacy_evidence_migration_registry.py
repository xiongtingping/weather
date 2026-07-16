from legacy_evidence_migration_registry import (
    STATUS_DIAGNOSTIC_ONLY,
    STATUS_MIGRATE,
    STATUS_UNCLASSIFIED,
    classify_legacy_evidence_field,
    legacy_evidence_registry_summary,
    normalize_legacy_evidence_field,
)


def test_classifies_forecast_weak_fields_as_migrate_not_production_allowed():
    item = classify_legacy_evidence_field("q_bayes_model_evidence")

    assert item["known"] is True
    assert item["status"] == STATUS_MIGRATE
    assert item["target_layer"] == "forecast_weak_likelihood"
    assert item["target_runtime"] == "forecast_weak_likelihood_runtime.py"
    assert item["production_allowed"] is False
    assert item["ui_primary_allowed"] is False


def test_classifies_observation_and_physical_fields_inside_metar_strong():
    trajectory = classify_legacy_evidence_field("q_bayes_temperature_trajectory_evidence_scale")
    physical = classify_legacy_evidence_field("physical_likelihood_evidence")

    assert trajectory["canonical_field"] == "q_bayes_temperature_trajectory_evidence"
    assert trajectory["field_suffix"] == "scale"
    assert trajectory["target_layer"] == "metar_strong_likelihood_internal"
    assert "remaining_warming" in trajectory["replacement_field"]
    assert physical["canonical_field"] == "q_bayes_physical_likelihood_evidence"
    assert physical["target_layer"] == "metar_strong_likelihood_internal"


def test_classifies_climo_and_post_cool_boundaries():
    climo = classify_legacy_evidence_field("q_bayes_climo_regime_evidence_raw")
    rebound_prior = classify_legacy_evidence_field("q_bayes_post_cool_rebound_regime_prior_evidence")
    rebound_risk = classify_legacy_evidence_field("q_bayes_post_cool_rebound_risk_evidence")

    assert climo["target_layer"] == "root_prior_weather_type_adjustment"
    assert climo["field_suffix"] == "raw"
    assert rebound_prior["target_layer"] == "root_prior_weather_type_adjustment"
    assert rebound_risk["status"] == STATUS_DIAGNOSTIC_ONLY
    assert rebound_risk["target_layer"] == "diagnostic_only"


def test_unknown_field_is_explicitly_unclassified_and_blocked():
    item = classify_legacy_evidence_field("q_bayes_unknown_evidence")

    assert item["known"] is False
    assert item["status"] == STATUS_UNCLASSIFIED
    assert item["cleanup_action"] == "add_registry_classification_before_migration"
    assert item["production_allowed"] is False
    assert item["ui_primary_allowed"] is False


def test_registry_summary_has_no_production_or_primary_ui_allowed_fields():
    summary = legacy_evidence_registry_summary()

    assert summary["read_only"] is True
    assert summary["guards"]["writes_archive_db"] is False
    assert summary["production_allowed_fields"] == []
    assert summary["ui_primary_allowed_fields"] == []
    assert summary["unclassified_fields"] == []
    assert summary["by_status"][STATUS_MIGRATE] >= 1


def test_normalize_legacy_evidence_field_suffixes():
    assert normalize_legacy_evidence_field("q_bayes_physical_likelihood_evidence_scale_details") == (
        "q_bayes_physical_likelihood_evidence",
        "scale_details",
    )
    assert normalize_legacy_evidence_field("climo_regime_evidence_scale_value") == (
        "climo_regime_evidence",
        "scale_value",
    )
    assert normalize_legacy_evidence_field("q_bayes_model_evidence") == ("q_bayes_model_evidence", "")
