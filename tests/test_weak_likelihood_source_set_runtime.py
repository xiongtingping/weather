from weak_likelihood_source_set_runtime import build_weak_likelihood_source_set, select_source_set_surface


def test_source_set_includes_missingness_in_exact_combination_key() -> None:
    result = build_weak_likelihood_source_set(
        official_nwp_generation={
            "schema": "official_nwp_multimodel_live_bundle_v1",
            "state": "available",
            "available_provider_ids": ["ecmwf", "icon", "gfs"],
            "missing_provider_ids": ["jma"],
        },
        independent_source_availability={"available_source_ids": ["taf"], "missing_source_ids": ["wu", "twc"]},
    )

    assert result["ok"] is True
    assert result["source_set_key"] == "ecmwf+icon+gfs+taf"
    assert result["missing_source_ids"] == ["jma", "wu", "twc"]


def test_source_set_supports_gfs_taf_without_inventing_missing_models() -> None:
    result = build_weak_likelihood_source_set(
        official_nwp_generation={
            "schema": "official_nwp_multimodel_live_bundle_v1",
            "state": "available",
            "available_provider_ids": ["gfs"],
            "missing_provider_ids": ["ecmwf", "icon", "jma"],
        },
        independent_source_availability={"available_source_ids": ["taf"]},
    )

    assert result["ok"] is True
    assert result["source_set_key"] == "gfs+taf"
    assert result["missing_source_ids"] == ["ecmwf", "icon", "jma", "wu", "twc"]


def test_surface_selection_rejects_generic_fallback_when_combination_is_untrained() -> None:
    source_set = build_weak_likelihood_source_set(
        official_nwp_generation={
            "schema": "official_nwp_multimodel_live_bundle_v1",
            "state": "available",
            "available_provider_ids": ["gfs"],
            "missing_provider_ids": ["ecmwf", "icon", "jma"],
        },
        independent_source_availability={"available_source_ids": ["taf"]},
    )

    selection = select_source_set_surface(
        artifact={"source_set_surfaces": {"summer|clear|short": {}}},
        source_set=source_set,
        context_key="summer|clear|short",
    )

    assert selection["ok"] is False
    assert "weak_source_set_surface_missing_or_invalid" in selection["issues"]
