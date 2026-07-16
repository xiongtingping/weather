from production_probability_runtime_manifest import (
    RUNTIME_FILES,
    audit_runtime_manifest_dependency_closure,
)


def test_probability_runtime_manifest_binds_complete_local_dependency_closure() -> None:
    audit = audit_runtime_manifest_dependency_closure()

    assert audit["ok"] is True
    assert audit["missing_runtime_files"] == []
    assert audit["parse_errors"] == []
    assert "sequential_bayes_checkpoint_reader.py" in RUNTIME_FILES
    assert "production_probability_activation_contract.py" in RUNTIME_FILES
