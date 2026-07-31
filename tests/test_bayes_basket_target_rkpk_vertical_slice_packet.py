from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.artifact_ingress import ingest_artifact_bundle
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts.ingest_bayes_basket_target_artifacts import (
    validate_packet_sha256,
)


ROOT = Path(__file__).parents[1]
PACKET = (
    ROOT
    / "output/audits/bayes_basket_target_no_live_vertical_slice_RKPK_v2.json"
)
CURRENT_PACKET = (
    ROOT
    / "output/audits/bayes_basket_target_no_live_vertical_slice_RKPK_v3.json"
)


def test_stale_rkpk_vertical_slice_packet_is_rejected_zero_write(
    tmp_path: Path,
) -> None:
    packet = json.loads(PACKET.read_text(encoding="utf-8"))
    assert packet["station_ids"] == ["RKPK"]
    assert validate_packet_sha256(packet, required=True) == packet["packet_sha256"]

    database = tmp_path / "target.sqlite3"
    with sqlite3.connect(database) as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="ensemble_selection_method_version_invalid",
        ):
            ingest_artifact_bundle(conn, packet, commit=True)
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_model_artifact"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_ensemble_artifact"
        ).fetchone()[0] == 0


def test_retired_deterministic_rkpk_packet_fails_new_formal_sample_gate(
    tmp_path: Path,
) -> None:
    packet = json.loads(CURRENT_PACKET.read_text(encoding="utf-8"))
    assert packet["station_ids"] == ["RKPK"]
    assert (
        validate_packet_sha256(packet, required=True)
        == packet["packet_sha256"]
    )
    assert {
        row["method_version"]
        for row in packet["ensemble_selection_evidence"]
    } == {
        "rolling_origin_probability_stack_v4_five_model_pool_min2"
    }

    database = tmp_path / "target.sqlite3"
    with sqlite3.connect(database) as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="formal_ensemble_holdout_sample_insufficient",
        ):
            ingest_artifact_bundle(conn, packet, commit=True)
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_model_artifact"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_ensemble_artifact"
        ).fetchone()[0] == 0
