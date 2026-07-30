from __future__ import annotations

from pathlib import Path
import json

import identifiable_physical_probability_worker as worker_module
from identifiable_physical_probability_worker import (
    IdentifiablePhysicalProbabilityWorker,
)
from production_probability_v6_activation_contract import (
    COMPOSER_SCHEMA,
    FORMULA_ID,
    activation_sha256,
)


ROOT = Path(__file__).resolve().parents[1]


def test_old_activation_disables_before_database_or_archive_touch(tmp_path: Path) -> None:
    missing_db = tmp_path / "must-not-be-created.db"
    raw_archive = tmp_path / "must-not-be-created"
    activation = json.loads(
        (ROOT / "config/production_probability_runtime_activation.json").read_text(
            encoding="utf-8"
        )
    )
    activation["composer_schema"] = COMPOSER_SCHEMA
    activation["bayes_formula_id"] = FORMULA_ID
    activation["transition_artifact_sha256"] = "0" * 64
    activation["observation_artifact_sha256"] = "1" * 64
    activation["activation_contract_sha256"] = activation_sha256(activation)
    activation_path = tmp_path / "inactive-legacy-activation.json"
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
    worker = IdentifiablePhysicalProbabilityWorker(
        missing_db,
        activation_path=activation_path,
        raw_archive_dir=raw_archive,
    )

    result = worker.run()

    assert result["ok"] is True
    assert result["state"] == "disabled_until_identifiable_physical_activation"
    assert result["database_opened"] is False
    assert result["raw_archive_written"] is False
    assert result["places_orders"] is False
    assert not missing_db.exists()
    assert not raw_archive.exists()


def test_retired_probability_worker_is_outside_production_closure() -> None:
    daemon = (ROOT / "scripts/reported_trajectory_no_daemon.py").read_text(
        encoding="utf-8"
    )
    installer = (
        ROOT / "scripts/install_reported_trajectory_no_system_services.sh"
    ).read_text(encoding="utf-8")

    assert "IdentifiablePhysicalProbabilityWorker" not in daemon
    assert "physical_probability_worker.run" not in daemon
    assert "ProspectivePhysicalEvidenceCaptureWorker" not in daemon
    assert "prospective_evidence_worker.run" not in daemon
    assert "TargetProductionWorker" in daemon
    assert "compile_production_cycle_from_ssot" in daemon
    assert '"taf_requested": False' in daemon
    assert "identifiable_physical_probability" not in installer
    assert "prospective_physical_evidence" not in installer
    assert "com.weather.reported_trajectory_no" in installer


def test_initial_ingress_preserves_each_reports_original_first_seen(
    tmp_path: Path, monkeypatch
) -> None:
    captured = {}

    def fake_ingress(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(
        worker_module, "apply_v6_amsc_archive_probability_ingress", fake_ingress
    )
    worker = IdentifiablePhysicalProbabilityWorker(
        tmp_path / "unused.db", raw_archive_dir=tmp_path / "raw"
    )
    rows = [
        {
            "raw_text": "METAR ZUUU 180000Z 00000MPS 9999 FEW020 25/20 Q1000 NOSIG=",
            "raw_sha256": "a",
            "first_seen_at": "2026-07-18T00:01:00Z",
            "fetch_started_at": "2026-07-18T00:00:59Z",
            "fetch_completed_at": "2026-07-18T00:01:00Z",
            "valid_at": "2026-07-18T00:00:00Z",
        },
        {
            "raw_text": "METAR ZUUU 180030Z 00000MPS 9999 FEW020 26/20 Q1000 NOSIG=",
            "raw_sha256": "b",
            "first_seen_at": "2026-07-18T00:31:00Z",
            "fetch_started_at": "2026-07-18T00:30:59Z",
            "fetch_completed_at": "2026-07-18T00:31:00Z",
            "valid_at": "2026-07-18T00:30:00Z",
        },
    ]

    result = worker._archive_and_ingress(event_date="2026-07-18", rows=rows)

    observations = captured["source_observation_payload"]["source_observations"]
    first_seen = sorted(row["payload"]["first_seen_at"] for row in observations)
    manifest_refs = [
        row["payload"]["transport"]["raw_archive_manifest_ref"]
        for row in observations
    ]
    assert result["ok"] is True
    assert first_seen == ["2026-07-18T00:01:00Z", "2026-07-18T00:31:00Z"]
    assert len(set(manifest_refs)) == 2
