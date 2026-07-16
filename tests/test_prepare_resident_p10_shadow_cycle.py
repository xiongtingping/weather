from __future__ import annotations

import json
import sqlite3

from p10_shadow_runtime_schema import initialize_p10_shadow_runtime_schema
from scripts.prepare_resident_p10_shadow_cycle import CONFIRMATION, INPUT_SCHEMA, main
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from strong_observation_lr_runtime_registry import (
    activate_strong_lr_runtime_artifact,
    register_strong_lr_runtime_artifact,
)
from tests.test_resident_p10_shadow_host import (
    _prepared_cycle,
    _previous_checkpoint,
    _seed_previous_checkpoint,
)
from tests.strong_observation_artifact_fixtures import strong_activation_attestation


def _input() -> dict:
    return {
        "schema": INPUT_SCHEMA,
        "prepared_at": "2026-07-03T06:01:00Z",
        "prepared_by": "p10-preparer-test",
        "cycle_input": _prepared_cycle(_previous_checkpoint()),
    }


def _ready_db(path) -> None:
    with sqlite3.connect(path) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_p10_shadow_runtime_schema(conn)
        _seed_previous_checkpoint(conn, _previous_checkpoint())
        cycle = _prepared_cycle(_previous_checkpoint())
        artifact = cycle["posterior_context"]["approved_lr_artifact"]
        register_strong_lr_runtime_artifact(
            conn,
            artifact=artifact,
            registered_at="2026-07-03T06:00:00Z",
            registered_by="pytest",
            registration_basis="test_runtime_baseline",
            registration_ref="pytest://p10-preparer",
        )
        activate_strong_lr_runtime_artifact(
            conn,
            target_identity=cycle["target_identity"],
            artifact_sha256=artifact["model_artifact_sha256"],
            activated_at="2026-07-03T06:00:00Z",
            activated_by="pytest",
            activation_basis="approved_runtime_baseline",
            activation_ref="pytest://p10-preparer-approval",
            activation_evidence=strong_activation_attestation(
                target_identity=cycle["target_identity"],
                artifact_sha256=artifact["model_artifact_sha256"],
                activation_basis="approved_runtime_baseline",
                activation_ref="pytest://p10-preparer-approval",
            ),
        )


def test_preparer_previews_then_persists_only_to_ready_isolated_p10_db(tmp_path, capsys) -> None:
    db = tmp_path / "p10_runtime.db"
    _ready_db(db)
    input_path = tmp_path / "cycle.json"
    input_path.write_text(json.dumps(_input()), encoding="utf-8")

    assert main(["--input-json", str(input_path), "--shadow-db", str(db)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["state"] == "preview_ready"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM resident_p10_prepared_cycles_shadow").fetchone()[0] == 0

    assert main([
        "--input-json", str(input_path), "--shadow-db", str(db), "--apply", "--confirm", CONFIRMATION,
    ]) == 0
    persisted = json.loads(capsys.readouterr().out)
    assert persisted["state"] == "persisted"
    assert persisted["shadow_only"] is True
    assert persisted["places_orders"] is False
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM resident_p10_prepared_cycles_shadow").fetchone()[0] == 1


def test_preparer_refuses_apply_without_confirmation(tmp_path, capsys) -> None:
    input_path = tmp_path / "cycle.json"
    input_path.write_text(json.dumps(_input()), encoding="utf-8")

    assert main(["--input-json", str(input_path), "--shadow-db", str(tmp_path / "p10.db"), "--apply"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["blockers"] == ["resident_p10_preparer_confirmation_invalid"]
