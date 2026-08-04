from production_probability_runtime_manifest import (
    AUTHORITY_SCOPE,
    PRODUCTION_RUNTIME_AUTHORIZED,
    RETIRED_NETWORK_ENTRYPOINTS,
    RUNTIME_FILES,
    audit_runtime_manifest_dependency_closure,
    build_current_retirement_receipt,
    build_current_runtime_manifest,
)


def test_probability_runtime_manifest_binds_complete_local_dependency_closure() -> None:
    audit = audit_runtime_manifest_dependency_closure()

    assert audit["ok"] is True
    assert audit["missing_runtime_files"] == []
    assert audit["parse_errors"] == []
    assert "sequential_bayes_checkpoint_reader.py" in RUNTIME_FILES
    assert "production_probability_activation_contract.py" in RUNTIME_FILES


def test_legacy_probability_manifest_is_retired_and_has_no_network_authority() -> None:
    manifest = build_current_runtime_manifest()
    retirement = build_current_retirement_receipt()

    assert AUTHORITY_SCOPE == "RETIRED_HISTORY_ONLY"
    assert PRODUCTION_RUNTIME_AUTHORIZED is False
    assert manifest["authority_scope"] == "RETIRED_HISTORY_ONLY"
    assert manifest["production_runtime_authorized"] is False
    assert manifest["network_entrypoints"] == []
    assert "scripts/fetch_zuuu_metar_taf.py" not in RUNTIME_FILES
    assert RETIRED_NETWORK_ENTRYPOINTS == ("scripts/fetch_zuuu_metar_taf.py",)
    assert retirement["production_probability_ingress_enabled_by_deployment"] is False
    assert retirement["production_runtime_authorized"] is False
