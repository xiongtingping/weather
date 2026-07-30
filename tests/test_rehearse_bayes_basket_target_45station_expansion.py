from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from scripts.rehearse_bayes_basket_target_45station_expansion import (
    _eligible_stations,
    _model_bound_stations,
    _packet_bound_stations,
)


def test_eligible_stations_require_review_and_two_model_selection() -> None:
    rows = []
    for index in range(45):
        station = f"S{index:03d}"
        rows.append({
            "station_id": station,
            "selected_models": ["GFS", "ICON"],
            "p0_no_live_binding_review": (
                "ELIGIBLE" if index in {1, 2} else "BLOCKED"
            ),
        })
    rows[2]["selected_models"] = ["GFS"]

    assert _eligible_stations({"station_results": rows}) == ("S001",)


def test_model_bound_stations_require_exact_selection_artifact_activation_chain(
) -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(
            """
            CREATE TABLE calibration_ensemble_selection_evidence(
              station_id TEXT,
              selected_models_json TEXT,
              content_sha256 TEXT
            );
            CREATE TABLE calibration_ensemble_artifact(
              station_id TEXT,
              ensemble_artifact_id TEXT,
              selection_evidence_sha256 TEXT
            );
            CREATE TABLE calibration_activation(
              station_id TEXT,
              artifact_kind TEXT,
              artifact_id TEXT,
              authority_scope TEXT
            );
            """
        )
        for station, models, linked, activated in (
            ("GOOD", ["GFS", "ICON"], True, True),
            ("SINGLE", ["GFS"], True, True),
            ("UNLINKED", ["GFS", "ICON"], False, True),
            ("UNACTIVATED", ["GFS", "ICON"], True, False),
        ):
            selection_sha = station.lower() + "-selection"
            artifact_id = station.lower() + "-ensemble"
            conn.execute(
                "INSERT INTO calibration_ensemble_selection_evidence "
                "VALUES(?,?,?)",
                (station, json.dumps(models), selection_sha),
            )
            conn.execute(
                "INSERT INTO calibration_ensemble_artifact VALUES(?,?,?)",
                (
                    station,
                    artifact_id,
                    selection_sha if linked else "other",
                ),
            )
            if activated:
                conn.execute(
                    "INSERT INTO calibration_activation VALUES(?,?,?,?)",
                    (
                        station,
                        "MODEL_ENSEMBLE",
                        artifact_id,
                        "PRODUCTION_NO_LIVE_ONLY",
                    ),
                )

        assert _model_bound_stations(conn) == ("GOOD",)


def _packet_build_fixture(selection_path) -> tuple[dict, dict, dict]:
    selection = {"content_sha256": "a" * 64}
    states = [
        {
            "station_id": f"S{index:03d}",
            "state": (
                "ACTIVATION_PACKET_READY"
                if index == 1
                else "BLOCKED_PROBABILITY_OOS_GATE"
            ),
        }
        for index in range(45)
    ]
    identity = {
        "schema": "bayes_basket_target_45station_18z_no_live_packet_build_v1",
        "source_manifest": str(selection_path.resolve()),
        "source_manifest_content_sha256": selection["content_sha256"],
        "activation_packet_ready_station_count": 1,
        "station_states": states,
    }
    build = {**identity, "content_sha256": content_sha256(identity)}
    packet = {"station_ids": ["S001"]}
    return packet, build, selection


def test_packet_bound_stations_require_exact_build_and_selection_generation(
    tmp_path,
) -> None:
    selection_path = tmp_path / "selection.json"
    packet, build, selection = _packet_build_fixture(selection_path)

    assert _packet_bound_stations(
        packet=packet,
        build_manifest=build,
        selection_manifest=selection,
        selection_manifest_path=selection_path,
    ) == ("S001",)

    drifted = dict(selection)
    drifted["content_sha256"] = "b" * 64
    with pytest.raises(
        ContractError,
        match="packet_selection_generation_mismatch",
    ):
        _packet_bound_stations(
            packet=packet,
            build_manifest=build,
            selection_manifest=drifted,
            selection_manifest_path=selection_path,
        )


def test_packet_bound_stations_reject_old_activation_station_leakage(
    tmp_path,
) -> None:
    selection_path = tmp_path / "selection.json"
    packet, build, selection = _packet_build_fixture(selection_path)
    packet["station_ids"] = ["S001", "OLD"]

    with pytest.raises(
        ContractError,
        match="packet_station_generation_mismatch",
    ):
        _packet_bound_stations(
            packet=packet,
            build_manifest=build,
            selection_manifest=selection,
            selection_manifest_path=selection_path,
        )
