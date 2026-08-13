from __future__ import annotations

import pytest

from bayes_basket_target import (
    ContractError,
    validate_cross_native_ensemble_gate,
    validate_cross_native_ensemble_weights,
    validate_native_ensemble_transport_gate,
)
from bayes_basket_target.open_meteo_ensemble import ENSEMBLE_SYSTEMS


def test_open_meteo_catalog_keeps_products_separate_but_counts_families_once():
    assert len(ENSEMBLE_SYSTEMS) == 18
    ifs = ENSEMBLE_SYSTEMS["ECMWF_IFS_ENS_GLOBAL"]
    aifs = ENSEMBLE_SYSTEMS["ECMWF_AIFS_ENS_GLOBAL"]
    gefs = ENSEMBLE_SYSTEMS["NOAA_GEFS_SEAMLESS"]
    icon = ENSEMBLE_SYSTEMS["DWD_ICON_EPS_SEAMLESS"]

    assert ifs["api_model"] != aifs["api_model"]
    assert ifs["expected_member_count"] == 51
    assert aifs["expected_member_count"] == 51
    assert ifs["model_scope"] == "global"
    assert aifs["model_scope"] == "global"
    assert ENSEMBLE_SYSTEMS["ECMWF_AIFS_ENS_EUROPE"]["model_scope"] == (
        "regional"
    )
    assert ifs["independence_family_id"] == "ECMWF"
    assert aifs["independence_family_id"] == "ECMWF"
    assert gefs["independence_family_id"] == "NOAA_GEFS"
    assert icon["independence_family_id"] == "DWD_ICON_EPS"
    assert ENSEMBLE_SYSTEMS["DWD_ICON_GLOBAL_EPS"]["model_scope"] == "global"
    assert ENSEMBLE_SYSTEMS["DWD_ICON_EPS_SEAMLESS"]["model_scope"] == "global"
    assert ENSEMBLE_SYSTEMS["DWD_ICON_EU_EPS"]["model_scope"] == "regional"
    assert ENSEMBLE_SYSTEMS["DWD_ICON_D2_EPS"]["model_scope"] == "regional"
    assert len({
        ifs["independence_family_id"],
        aifs["independence_family_id"],
        gefs["independence_family_id"],
        icon["independence_family_id"],
    }) == 3


def _system(
    system_id: str,
    family_id: str,
    expected_member_count: int,
) -> dict[str, object]:
    upper = f"{system_id}|{family_id}".upper()
    if "ECCC" in upper:
        provider_id = "ECCC"
    elif "DWD" in upper or "ICON" in upper:
        provider_id = "DWD"
    elif "NOAA" in upper or "GEFS" in upper:
        provider_id = "NOAA"
    else:
        provider_id = "ECMWF"
    return {
        "ensemble_system_id": system_id,
        "independence_family_id": family_id,
        "source_class": "NATIVE_ENSEMBLE_PROVIDER",
        "aggregator_or_republisher": 0,
        "expected_member_count": expected_member_count,
        "provider_id": provider_id,
    }


def _members(system_id: str, count: int) -> list[dict[str, object]]:
    provider_id = _system(system_id, system_id, count)["provider_id"]
    transport_provider_id = {
        "ECMWF": "ECMWF_OPEN_DATA_S3",
        "NOAA": "NOAA_GEFS_PDS_S3",
        "ECCC": "ECCC_DD_WEATHER",
        # The member whitelist and every DWD member row carry the opendata
        # transport; a bare "DWD" here mirrored the map defect that kept ICON
        # out of every forward packet.
        "DWD": "DWD_OPENDATA_ICON_EPS",
    }[str(provider_id)]
    return [
        {
            "ensemble_system_id": system_id,
            "member_id": f"member-{index:02d}",
            "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
            "native_timestamps_complete": 1,
            "run_init_utc": "2026-07-30T00:00:00Z",
            "published_at_utc": "2026-07-30T06:00:00Z",
            "provider_id": provider_id,
            "transport_provider_id": transport_provider_id,
        }
        for index in range(count)
    ]


def test_ecmwf_51_members_count_as_one_family_not_51_models() -> None:
    systems = {"ECMWF_ENS": _system("ECMWF_ENS", "ECMWF_ENS", 51)}
    with pytest.raises(
        ContractError,
        match="cross_native_ensemble_requires_two_families",
    ):
        validate_cross_native_ensemble_gate(
            selected_system_ids=["ECMWF_ENS"],
            systems_by_id=systems,
            member_runs=_members("ECMWF_ENS", 51),
        )


def test_ecmwf_ens_plus_gefs_passes_two_family_gate() -> None:
    systems = {
        "ECMWF_ENS": _system("ECMWF_ENS", "ECMWF_ENS", 51),
        "GEFS": _system("GEFS", "GEFS", 31),
    }
    result = validate_cross_native_ensemble_gate(
        selected_system_ids=["ECMWF_ENS", "GEFS"],
        systems_by_id=systems,
        member_runs=_members("ECMWF_ENS", 51) + _members("GEFS", 31),
    )
    assert result["ensemble_system_count"] == 2
    assert result["independent_family_count"] == 2
    assert result["member_count_by_system"] == {"ECMWF_ENS": 51, "GEFS": 31}


def test_two_products_from_same_family_still_count_as_one_family() -> None:
    systems = {
        "ECMWF_ENS_A": _system("ECMWF_ENS_A", "ECMWF_ENS", 2),
        "ECMWF_ENS_B": _system("ECMWF_ENS_B", "ECMWF_ENS", 2),
    }
    with pytest.raises(
        ContractError,
        match="cross_native_ensemble_requires_two_families",
    ):
        validate_cross_native_ensemble_gate(
            selected_system_ids=["ECMWF_ENS_A", "ECMWF_ENS_B"],
            systems_by_id=systems,
            member_runs=(
                _members("ECMWF_ENS_A", 2) + _members("ECMWF_ENS_B", 2)
            ),
        )


def test_product_catalog_registration_does_not_upgrade_republisher_members() -> None:
    systems = {
        "AIFS": _system("AIFS", "ECMWF", 2),
        "GEFS": _system("GEFS", "NOAA_GEFS", 2),
    }
    republisher_members = _members("AIFS", 2) + _members("GEFS", 2)
    republisher_members[0] = {
        **republisher_members[0],
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "native_timestamps_complete": 0,
        "run_init_utc": None,
        "published_at_utc": None,
    }
    with pytest.raises(
        ContractError,
        match="native_ensemble_member_not_formal_native",
    ):
        validate_cross_native_ensemble_gate(
            selected_system_ids=["AIFS", "GEFS"],
            systems_by_id=systems,
            member_runs=republisher_members,
        )


def test_ifs_aifs_and_gefs_are_three_systems_but_two_families() -> None:
    systems = {
        "ECMWF_IFS_ENS": _system("ECMWF_IFS_ENS", "ECMWF", 2),
        "ECMWF_AIFS_ENS": _system("ECMWF_AIFS_ENS", "ECMWF", 2),
        "GEFS": _system("GEFS", "NOAA_GEFS", 2),
    }
    result = validate_cross_native_ensemble_gate(
        selected_system_ids=list(systems),
        systems_by_id=systems,
        member_runs=sum(
            (_members(system_id, 2) for system_id in systems),
            [],
        ),
    )
    assert result["ensemble_system_count"] == 3
    assert result["independent_family_count"] == 2


def test_incomplete_member_set_fails_closed() -> None:
    systems = {
        "ECMWF_ENS": _system("ECMWF_ENS", "ECMWF_ENS", 51),
        "ICON_EPS": _system("ICON_EPS", "ICON_EPS", 40),
    }
    with pytest.raises(
        ContractError,
        match="native_ensemble_member_set_incomplete",
    ):
        validate_cross_native_ensemble_gate(
            selected_system_ids=["ECMWF_ENS", "ICON_EPS"],
            systems_by_id=systems,
            member_runs=_members("ECMWF_ENS", 50) + _members("ICON_EPS", 40),
        )


def test_republisher_cannot_claim_native_family() -> None:
    systems = {
        "ECMWF_ENS": _system("ECMWF_ENS", "ECMWF_ENS", 51),
        "OPEN_METEO_GEFS": {
            **_system("OPEN_METEO_GEFS", "GEFS", 31),
            "aggregator_or_republisher": 1,
        },
    }
    with pytest.raises(
        ContractError,
        match="native_ensemble_republisher_forbidden",
    ):
        validate_cross_native_ensemble_gate(
            selected_system_ids=["ECMWF_ENS", "OPEN_METEO_GEFS"],
            systems_by_id=systems,
            member_runs=(
                _members("ECMWF_ENS", 51)
                + _members("OPEN_METEO_GEFS", 31)
            ),
        )


def test_aggregator_member_rows_cannot_pass_formal_native_gate() -> None:
    systems = {
        "ECMWF_ENS": _system("ECMWF_ENS", "ECMWF", 2),
        "GEFS": _system("GEFS", "NOAA_GEFS", 2),
    }
    members = _members("ECMWF_ENS", 2) + _members("GEFS", 2)
    members[0]["authority_scope"] = "PRODUCTION_NO_LIVE_CANDIDATE"
    members[0]["native_timestamps_complete"] = 0
    members[0]["run_init_utc"] = ""
    members[0]["published_at_utc"] = ""
    with pytest.raises(
        ContractError,
        match="native_ensemble_member_not_formal_native",
    ):
        validate_cross_native_ensemble_gate(
            selected_system_ids=list(systems),
            systems_by_id=systems,
            member_runs=members,
        )


def test_open_meteo_transport_cannot_forge_formal_native_member() -> None:
    systems = {
        "ECMWF_ENS": _system("ECMWF_ENS", "ECMWF", 2),
        "GEFS": _system("GEFS", "NOAA_GEFS", 2),
    }
    members = _members("ECMWF_ENS", 2) + _members("GEFS", 2)
    members[0]["transport_provider_id"] = "OPEN_METEO"
    with pytest.raises(
        ContractError,
        match="native_ensemble_transport_not_official",
    ):
        validate_cross_native_ensemble_gate(
            selected_system_ids=list(systems),
            systems_by_id=systems,
            member_runs=members,
        )


@pytest.mark.parametrize(
    ("provider_id", "transport_provider_id"),
    [
        ("ECMWF", "ECMWF_OPEN_DATA_S3"),
        ("NOAA", "NOAA_GEFS_PDS_S3"),
        ("ECCC", "ECCC_DD_WEATHER"),
        ("DWD", "DWD_OPENDATA_ICON_EPS"),
        ("ICAO_MET_OFFICE", "AWC_TAF_DATA_API"),
        ("AWC_METAR_PHYSICAL_PATH", "AWC_METAR_PHYSICAL_PATH"),
    ],
)
def test_official_transport_gate_accepts_each_registered_native_provider(
    provider_id: str,
    transport_provider_id: str,
) -> None:
    systems = {
        "SYSTEM": {
            **_system("SYSTEM", "FAMILY", 2),
            "provider_id": provider_id,
        },
    }
    assert validate_native_ensemble_transport_gate(
        selected_system_ids=["SYSTEM"],
        systems_by_id=systems,
        transport_provider_by_system={
            "SYSTEM": transport_provider_id,
        },
    ) == {"SYSTEM": transport_provider_id}


def test_ecmwf_ifs_and_aifs_share_one_family_weight_cap() -> None:
    systems = {
        "ECMWF_IFS_ENS": _system("ECMWF_IFS_ENS", "ECMWF", 2),
        "ECMWF_AIFS_ENS": _system("ECMWF_AIFS_ENS", "ECMWF", 2),
        "GEFS": _system("GEFS", "NOAA_GEFS", 2),
    }
    result = validate_cross_native_ensemble_weights(
        system_weights={
            "ECMWF_IFS_ENS": 0.25,
            "ECMWF_AIFS_ENS": 0.20,
            "GEFS": 0.55,
        },
        systems_by_id=systems,
        family_weight_caps={"ECMWF": 0.45, "NOAA_GEFS": 0.75},
    )
    assert result["family_weights"] == {
        "ECMWF": pytest.approx(0.45),
        "NOAA_GEFS": pytest.approx(0.55),
    }
    with pytest.raises(
        ContractError,
        match="cross_native_ensemble_family_cap_exceeded",
    ):
        validate_cross_native_ensemble_weights(
            system_weights={
                "ECMWF_IFS_ENS": 0.35,
                "ECMWF_AIFS_ENS": 0.30,
                "GEFS": 0.35,
            },
            systems_by_id=systems,
            family_weight_caps={
                "ECMWF": 0.60,
                "NOAA_GEFS": 0.75,
            },
        )
