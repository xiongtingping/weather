from pathlib import Path

from scripts.production_storage_boundary_audit import audit


ROOT = Path(__file__).resolve().parents[1]


def test_production_storage_boundary_is_local_and_never_probes_external_disk() -> None:
    result = audit(
        ROOT,
        archive_db=Path("data/archive.db"),
        cutover_script=Path(
            "scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh"
        ),
    )

    assert result["ok"] is True
    assert result["archive_db_is_regular_non_symlink"] is True
    assert result["archive_db_same_device_as_workspace"] is True
    assert result["archive_db_same_device_as_private_tmp"] is True
    assert result["local_apfs_cow_rollback_clone"] is True
    assert result["external_disk_probed"] is False
    assert result["external_disk_required"] is False
    assert result["external_disk_role"] == "discardable_rebuildable_cold_archive_only"
    assert result["external_disk_may_contain_production_database"] is False
    assert result["external_disk_may_supply_backup_or_rollback"] is False
    assert result["external_disk_may_affect_readiness_or_runtime"] is False
    assert result["external_archive_failure_effect"] == "none"
    assert result["retired_external_cutovers_fail_closed_before_probe"]
    assert all(result["retired_external_cutovers_fail_closed_before_probe"].values())
    assert result["blocks_amsc_or_ssot"] is False
