from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_oos import (
    build_native_ensemble_oos,
    persist_native_ensemble_oos,
)
from bayes_basket_target.native_ensemble_p0 import (
    compile_native_ensemble_p0,
    persist_native_ensemble_forward_prediction,
)
from bayes_basket_target.official_native_forward_packet import (
    ECMWF_MULTISTATION_SCHEMA,
    NOAA_GEFS_MULTISTATION_SCHEMA,
    build_official_native_forward_packet,
)

from tests.test_native_ensemble_oos_p0_pipeline import _insert_fixture


def _archive(
    *,
    system_id: str,
    family_id: str,
    schema: str,
    run_cycle: str,
    run_init_utc: str,
    query_at_utc: str,
) -> dict[str, object]:
    intervals = {
        "19°C or below": [None, 19.5],
        "20°C": [19.5, 20.5],
        "21°C": [20.5, 21.5],
        "22°C or higher": [21.5, None],
    }
    members = [
        {
            "member_id": (
                "control" if index == 0 else f"perturbed_{index:02d}"
            ),
            "member_role": "CONTROL" if index == 0 else "PERTURBED",
            "raw_tmax_c": 20.0 + (-0.2, 0.0, 0.2)[index],
        }
        for index in range(3)
    ]
    capture_identity = {
        "station_id": "TEST",
        "event_date": "2026-07-02",
        "settlement_timezone": "Etc/UTC",
        "run_cycle": run_cycle,
        "ensemble_system_id": system_id,
        "independence_family_id": family_id,
        "member_count": len(members),
        "members": members,
        "member_set_sha256": content_sha256(members),
        "bucket_intervals": intervals,
        "bucket_contract_sha256": content_sha256(intervals),
    }
    capture = {
        **capture_identity,
        "content_sha256": content_sha256(capture_identity),
    }
    archive_identity = {
        "schema": schema,
        "event_date": "2026-07-02",
        "archive_lead_bucket": "d2",
        "run_cycle": run_cycle,
        "ensemble_system_id": system_id,
        "independence_family_id": family_id,
        "member_count": len(members),
        "run_init_utc": run_init_utc,
        "archive_query_at_utc": query_at_utc,
        "captures": [capture],
        "training_only": True,
        "holdout_eligible": False,
    }
    return {
        **archive_identity,
        "content_sha256": content_sha256(archive_identity),
    }


def _mixed_packet() -> dict[str, object]:
    return build_official_native_forward_packet(
        archives=[
            _archive(
                system_id="ECMWF_IFS_TEST",
                family_id="ECMWF",
                schema=ECMWF_MULTISTATION_SCHEMA,
                run_cycle="18Z",
                run_init_utc="2026-06-30T18:00:00Z",
                query_at_utc="2026-07-01T12:00:00Z",
            ),
            _archive(
                system_id="ECMWF_AIFS_TEST",
                family_id="ECMWF",
                schema=ECMWF_MULTISTATION_SCHEMA,
                run_cycle="00Z",
                run_init_utc="2026-07-01T00:00:00Z",
                query_at_utc="2026-07-01T12:01:00Z",
            ),
            _archive(
                system_id="NOAA_GEFS_TEST",
                family_id="NOAA_GEFS",
                schema=NOAA_GEFS_MULTISTATION_SCHEMA,
                run_cycle="18Z",
                run_init_utc="2026-06-30T18:00:00Z",
                query_at_utc="2026-07-01T12:02:00Z",
            ),
        ],
        station_id="TEST",
    )


def _clone_row(
    conn: sqlite3.Connection,
    table: str,
    *,
    primary_key: str,
    original_id: str,
    changes: dict[str, object],
) -> dict[str, object]:
    columns = [
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})")
    ]
    row = dict(zip(
        columns,
        conn.execute(
            f"SELECT {','.join(columns)} FROM {table} "
            f"WHERE {primary_key}=?",
            (original_id,),
        ).fetchone(),
    ))
    row.update(changes)
    row[primary_key] = str(changes.get(primary_key) or original_id)
    row.pop("content_sha256", None)
    row["content_sha256"] = content_sha256(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})",
        tuple(row[column] for column in row),
    )
    return row


def _install_mixed_cycle_calibrations(
    conn: sqlite3.Connection,
    report: dict[str, object],
) -> None:
    base_activation = conn.execute(
        "SELECT activation_id,artifact_id FROM "
        "calibration_native_ensemble_activation "
        "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    assert base_activation is not None
    base_cross_id = str(base_activation[1])
    base_cross = conn.execute(
        "SELECT * FROM calibration_cross_native_ensemble_artifact "
        "WHERE cross_ensemble_artifact_id=?",
        (base_cross_id,),
    ).fetchone()
    assert base_cross is not None
    cross_columns = [
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(calibration_cross_native_ensemble_artifact)"
        )
    ]
    base_cross_row = dict(zip(cross_columns, base_cross))
    selection = _clone_row(
        conn,
        "calibration_cross_native_ensemble_selection_evidence",
        primary_key="selection_evidence_id",
        original_id=str(
            conn.execute(
                "SELECT selection_evidence_id "
                "FROM calibration_cross_native_ensemble_selection_evidence "
                "WHERE content_sha256=?",
                (base_cross_row["selection_evidence_sha256"],),
            ).fetchone()[0]
        ),
        changes={
            "selection_evidence_id": "mixed-selection-evidence",
            "lead_bucket": "d2",
            "run_cycle": "MIXED_NATIVE_CYCLES",
        },
    )
    mixed_cross = _clone_row(
        conn,
        "calibration_cross_native_ensemble_artifact",
        primary_key="cross_ensemble_artifact_id",
        original_id=base_cross_id,
        changes={
            "cross_ensemble_artifact_id": "mixed-cross-artifact",
            "lead_bucket": "d2",
            "run_cycle": "MIXED_NATIVE_CYCLES",
            "selection_evidence_sha256": selection["content_sha256"],
        },
    )
    _clone_row(
        conn,
        "calibration_native_ensemble_activation",
        primary_key="activation_id",
        original_id=str(base_activation[0]),
        changes={
            "activation_id": "mixed-cross-activation",
            "artifact_id": mixed_cross["cross_ensemble_artifact_id"],
            "lead_bucket": "d2",
            "run_cycle": "MIXED_NATIVE_CYCLES",
            "generation": 2,
        },
    )
    cycle_by_system = {
        "ECMWF_IFS_TEST": "18Z",
        "ECMWF_AIFS_TEST": "00Z",
        "NOAA_GEFS_TEST": "18Z",
    }
    for system_id, run_cycle in cycle_by_system.items():
        system_activation = conn.execute(
            "SELECT activation_id,artifact_id FROM "
            "calibration_native_ensemble_activation "
            "WHERE artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
            "AND ensemble_system_id_or_star=? "
            "ORDER BY generation DESC LIMIT 1",
            (system_id,),
        ).fetchone()
        assert system_activation is not None
        system_artifact = _clone_row(
            conn,
            "calibration_native_ensemble_system_artifact",
            primary_key="system_artifact_id",
            original_id=str(system_activation[1]),
            changes={
                "system_artifact_id": f"mixed-system-artifact-{system_id}",
                "lead_bucket": "d2",
                "run_cycle": run_cycle,
            },
        )
        _clone_row(
            conn,
            "calibration_native_ensemble_activation",
            primary_key="activation_id",
            original_id=str(system_activation[0]),
            changes={
                "activation_id": f"mixed-system-activation-{system_id}",
                "artifact_id": system_artifact["system_artifact_id"],
                "lead_bucket": "d2",
                "run_cycle": run_cycle,
                "generation": 2,
            },
        )
    conn.commit()


def test_mixed_native_cycles_use_per_system_oos_lineage_and_family_gate() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    _install_mixed_cycle_calibrations(conn, report)

    packet = _mixed_packet()
    compiled = compile_native_ensemble_p0(
        conn,
        forward_packet=packet,
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    lineage = compiled["packet"]["native_probability_envelope"]["lineage"]
    assert lineage["run_cycle"] == "MIXED_NATIVE_CYCLES"
    assert lineage["run_cycle_scope"] == "PER_SYSTEM_NATIVE_CYCLE"
    assert json.loads(lineage["run_cycle_by_system_json"]) == packet[
        "run_cycle_by_system"
    ]
    assert json.loads(lineage["run_init_utc_by_system_json"]) == packet[
        "run_init_utc_by_system"
    ]
    assert json.loads(
        lineage["archive_query_at_utc_by_system_json"]
    ) == packet["archive_query_at_utc_by_system"]
    taf = json.loads(lineage["taf_nwp_live_compatibility_json"])
    assert taf["run_cycle"] == "MIXED_NATIVE_CYCLES"
    assert taf["run_cycle_by_system"] == packet["run_cycle_by_system"]
    assert taf["availability_evidence"] == packet[
        "availability_evidence"
    ]
    assert len(json.loads(lineage["independence_family_ids_json"])) == 2
    assert compiled["forward_prediction"]["run_cycle_by_system_json"]
    assert persist_native_ensemble_forward_prediction(
        conn,
        compiled,
    )["state"] == "COMMITTED"


def test_mixed_native_cycles_missing_system_lineage_fail_closed() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    _install_mixed_cycle_calibrations(conn, report)
    packet = _mixed_packet()
    broken = copy.deepcopy(packet)
    del broken["run_init_utc_by_system"]["ECMWF_AIFS_TEST"]
    identity = {
        key: value
        for key, value in broken.items()
        if key != "snapshot_content_sha256"
    }
    broken["snapshot_content_sha256"] = content_sha256(identity)
    with pytest.raises(
        ContractError,
        match="native_ensemble_p0_mixed_init_lineage_missing",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=broken,
            decision_at_utc="2026-07-01T13:00:00Z",
        )


def test_mixed_native_cycles_without_one_system_oos_activation_fail_closed() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    _install_mixed_cycle_calibrations(conn, report)
    packet = _mixed_packet()
    broken = copy.deepcopy(packet)
    broken["run_cycle_by_system"]["ECMWF_AIFS_TEST"] = "06Z"
    broken["run_init_utc_by_system"]["ECMWF_AIFS_TEST"] = (
        "2026-07-01T06:00:00Z"
    )
    broken["availability_evidence"]["by_system"]["ECMWF_AIFS_TEST"][
        "run_cycle"
    ] = "06Z"
    broken["availability_evidence"]["by_system"]["ECMWF_AIFS_TEST"][
        "run_init_utc"
    ] = "2026-07-01T06:00:00Z"
    availability_identity = {
        key: value
        for key, value in broken["availability_evidence"].items()
        if key != "content_sha256"
    }
    broken["availability_evidence"]["content_sha256"] = content_sha256(
        availability_identity
    )
    identity = {
        key: value
        for key, value in broken.items()
        if key != "snapshot_content_sha256"
    }
    broken["snapshot_content_sha256"] = content_sha256(identity)
    with pytest.raises(
        ContractError,
        match="native_system_activation_not_exact",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=broken,
            decision_at_utc="2026-07-01T13:00:00Z",
        )
