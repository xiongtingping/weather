from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ingest_bayes_basket_target_artifacts.py"


def _packet() -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
        "calibration_promotions": [],
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [],
        "cash_phase_policies": [],
        "cash_phase_authority_manifests": [],
        "policy_promotions": [],
    }
    return {**identity, "packet_sha256": content_sha256(identity)}


def _run(
    *,
    database: Path,
    packet: Path,
    receipt: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(database),
            "--packet",
            str(packet),
            "--apply",
            "--confirm",
            "INGEST_BAYES_BASKET_TARGET_ARTIFACTS",
            "--require-packet-sha256",
            "--receipt",
            str(receipt),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_requires_content_addressed_packet_and_writes_bounded_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "target.sqlite3"
    with sqlite3.connect(database) as conn:
        apply_production_ssot_schema(conn)
    packet = tmp_path / "packet.json"
    packet.write_text(
        json.dumps(_packet(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    receipt = tmp_path / "receipt.json"

    result = _run(database=database, packet=packet, receipt=receipt)

    assert result.returncode == 0, result.stderr
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["state"] == "COMMITTED"
    assert payload["packet_sha256"] == _packet()["packet_sha256"]
    assert payload["execution_mode"] == "production_no_live"
    assert payload["live_submit_authorized"] is False
    assert payload["authorizes_order"] is False
    assert payload["calls_order_gateway"] is False
    assert payload["orders_submitted"] == 0
    assert payload["station_ids"] == []
    assert payload["packet_expected_counts"] == {
        "calibration_activations": 0,
        "calibration_artifacts": 0,
        "cash_phase_authority_manifests": 0,
        "cash_phase_policies": 0,
        "ensemble_artifacts": 0,
        "ensemble_selection_evidence": 0,
    }
    assert payload["packet_binding_counts"] == {
        "calibration_activations": 0,
        "calibration_artifacts": 0,
        "cash_phase_authority_manifests": 0,
        "cash_phase_policies": 0,
        "ensemble_artifacts": 0,
        "ensemble_selection_evidence": 0,
    }


def test_cli_rejects_tampered_packet_before_database_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "target.sqlite3"
    with sqlite3.connect(database) as conn:
        apply_production_ssot_schema(conn)
    payload = _packet()
    payload["policy_artifacts"] = [{"tampered": True}]
    packet = tmp_path / "packet.json"
    packet.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    receipt = tmp_path / "receipt.json"

    result = _run(database=database, packet=packet, receipt=receipt)

    assert result.returncode == 1
    blocked = json.loads(receipt.read_text(encoding="utf-8"))
    assert blocked["state"] == "BLOCKED_FAIL_CLOSED"
    assert "artifact_ingress_packet_sha256_mismatch" in blocked["reason"]
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_station_policy_artifact"
        ).fetchone()[0] == 0
