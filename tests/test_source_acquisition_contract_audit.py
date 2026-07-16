from pathlib import Path

from scripts import source_acquisition_contract_audit as audit


def test_source_acquisition_contract_audit_validates_catalog_samples_and_guards(tmp_path: Path) -> None:
    report = audit.build_source_acquisition_contract_audit(
        root=tmp_path,
        max_items_output=20,
    )

    assert report["schema"] == "source_acquisition_contract_audit_v1"
    assert report["ok"] is True
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["fetches_external_weather_sources"] is False
    assert report["catalog_audit"]["ok"] is True
    assert report["source_observation_sample_audit"]["ok"] is True
    assert report["data_quality_scenario_audit"]["ok"] is True
    assert report["coverage"]["sample_source_observation_count"] == 9
    assert report["coverage"]["data_quality_scenario_count"] == 3

    source_classes = report["source_observation_sample_audit"]["source_class_counts"]
    assert source_classes["hard_observation_candidate"] == 2
    assert source_classes["diagnostic_current_source"] == 2
    assert source_classes["forecast_weak_source"] == 3
    quality_checks = report["data_quality_scenario_audit"]["checks"]
    assert quality_checks["missing_source_quality_flags_detected"] is True
    assert quality_checks["missing_source_quality_summary_counts"] is True
    assert quality_checks["conflicting_hard_observation_detected"] is True
    assert quality_checks["conflict_threshold_uses_metar_platform_policy"] is True
    assert quality_checks["delayed_source_detected_from_catalog_timeout"] is True
    assert quality_checks["delayed_source_uses_catalog_timeout"] is True
    assert set(report["data_quality_scenario_audit"]["missing_source"]["quality_flags"]) >= {
        "source_not_ok",
        "valid_time_missing",
        "raw_ref_missing",
        "weather_value_missing",
    }
    assert report["data_quality_scenario_audit"]["conflicting_source"]["conflict_count"] == 1
    assert report["data_quality_scenario_audit"]["delayed_source"]["delayed_count"] == 1
    assert report["network_ingress_inventory"]["network_file_count"] == 0


def test_network_inventory_blocks_production_source_ingress_without_manager_adapter(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "fetch_zuuu_metar_taf.py").write_text(
        "import urllib.request\nurllib.request.urlopen('https://example.test')\n",
        encoding="utf-8",
    )

    report = audit.build_source_acquisition_contract_audit(
        root=tmp_path,
        max_items_output=20,
    )

    assert report["ok"] is False
    assert "network:scripts/fetch_zuuu_metar_taf.py:production_source_ingress_missing_manager_adapter" in report["blockers"]
    inventory = report["network_ingress_inventory"]
    assert inventory["production_ingress_count"] == 1
    assert inventory["ok"] is False


def test_network_inventory_accepts_production_source_ingress_with_manager_adapter(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "fetch_zuuu_metar_taf.py").write_text(
        "\n".join(
            [
                "import urllib.request",
                "from weather_source_acquisition_manager import build_source_acquisition_trace",
                "from weather_fact_snapshot_writer import build_fact_snapshot_write_plan",
                "urllib.request.urlopen('https://example.test')",
            ]
        ),
        encoding="utf-8",
    )

    report = audit.build_source_acquisition_contract_audit(
        root=tmp_path,
        max_items_output=20,
    )

    assert report["ok"] is True
    inventory = report["network_ingress_inventory"]
    assert inventory["production_ingress_count"] == 1
    assert inventory["blockers"] == []


def test_compact_source_acquisition_contract_audit_keeps_key_counts(tmp_path: Path) -> None:
    report = audit.build_source_acquisition_contract_audit(
        root=tmp_path,
        max_items_output=5,
    )
    compact = audit.compact_audit(report)

    assert compact["schema"] == "source_acquisition_contract_audit_v1_summary"
    assert compact["ok"] is True
    assert compact["catalog"]["required_source_count"] == 20
    assert compact["source_observation_samples"]["observation_count"] == 9
    assert compact["network_ingress"]["network_file_count"] == 0
    assert compact["data_quality"]["ok"] is True
    assert compact["data_quality"]["conflict_count"] == 1
    assert compact["data_quality"]["delayed_count"] == 1
    assert "weather_value_missing" in compact["data_quality"]["missing_quality_flags"]


def test_source_acquisition_contract_audit_cli_accepts_limit_alias(
    tmp_path: Path,
    capsys,
) -> None:
    assert audit.main(
        [
            "--root",
            str(tmp_path),
            "--summary",
            "--limit",
            "20",
            "--max-items-output",
            "5",
        ]
    ) == 0
    payload = capsys.readouterr().out
    assert "source_acquisition_contract_audit_v1_summary" in payload
