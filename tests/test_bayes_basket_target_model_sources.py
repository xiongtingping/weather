import pytest

from bayes_basket_target.contracts import (
    CALIBRATION_CANDIDATE_MODEL_IDS,
    GLOBAL_FORMAL_MODEL_IDS,
    MODEL_IDS,
    REGIONAL_CALIBRATION_MODEL_IDS,
    ContractError,
    calibration_candidate_model_set_hash,
    model_set_hash,
)
from bayes_basket_target.model_sources import (
    has_one_provider_per_model_role,
    model_id_for_provider,
    provider_role,
    provider_source_class,
    regional_model_lineage,
)


def test_candidate_model_registry_does_not_widen_formal_model_ids() -> None:
    assert MODEL_IDS == GLOBAL_FORMAL_MODEL_IDS
    assert set(GLOBAL_FORMAL_MODEL_IDS).isdisjoint(
        REGIONAL_CALIBRATION_MODEL_IDS
    )
    assert set(CALIBRATION_CANDIDATE_MODEL_IDS) == (
        set(GLOBAL_FORMAL_MODEL_IDS) | set(REGIONAL_CALIBRATION_MODEL_IDS)
    )
    with pytest.raises(ContractError, match="model_set_invalid"):
        model_set_hash(("ECMWF", "HRRR"))
    assert calibration_candidate_model_set_hash(("ECMWF", "HRRR"))


def test_previous_runs_transports_are_honest_historical_aggregators() -> None:
    assert model_id_for_provider("ecmwf_ifs_open_meteo_previous_runs") == "ECMWF"
    assert provider_role("ecmwf_ifs_open_meteo_previous_runs") == (
        "ecmwf_ifs025_official"
    )
    assert model_id_for_provider("gfs_global_open_meteo_previous_runs") == "GFS"
    assert provider_role("gfs_global_open_meteo_previous_runs") == (
        "gfs_0p25_official"
    )
    assert provider_source_class("ecmwf_ifs_open_meteo_previous_runs") == (
        "aggregator_republisher"
    )
    assert provider_source_class("gfs_global_open_meteo_previous_runs") == (
        "aggregator_republisher"
    )


def test_previous_runs_transport_cannot_satisfy_formal_provider_set() -> None:
    assert not has_one_provider_per_model_role([
        "ecmwf_ifs_open_meteo_previous_runs",
        "gfs_global_open_meteo_previous_runs",
        "icon_global_open_meteo",
        "gem_global_open_meteo",
        "cma_grapes_global_open_meteo",
    ])


def test_regional_icon_and_european_candidates_keep_republisher_lineage() -> None:
    expected = {
        "icon_eu_open_meteo": "ICON-EU",
        "icon_d2_open_meteo": "ICON-D2",
        "meteofrance_arome_france0025_open_meteo": "AROME-FR",
        "knmi_harmonie_arome_netherlands_open_meteo": "HARMONIE-NL",
        "knmi_harmonie_arome_europe_open_meteo": "HARMONIE-EU",
        "ukmo_uk_deterministic_2km_open_meteo": "UKMO-2KM",
    }
    for provider, model_id in expected.items():
        lineage = regional_model_lineage(provider)
        assert lineage["model_id"] == model_id
        assert lineage["model_scope"] == "regional"
        assert lineage["source_class"] == "aggregator_republisher"
        assert lineage["native_arrival_index_eligible"] is False
        assert lineage["formal_p0_eligible"] is False
        assert provider_source_class(provider) == "aggregator_republisher"


def test_global_native_provider_is_not_accepted_as_regional_lineage() -> None:
    with pytest.raises(ContractError, match="regional_model_provider_not_registered"):
        regional_model_lineage("icon_global_official")
