from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from bayes_basket_target import native_ensemble_oos
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.future_temperature_curve import (
    build_native_family_temperature_curve,
)
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
    family_curve = build_native_family_temperature_curve({
        "station_id": "TEST",
        "event_date": "2026-07-02",
        "settlement_timezone": "Etc/UTC",
        "ensemble_system_id": system_id,
        "independence_family_id": family_id,
        "available_at_utc": query_at_utc,
        "source_field_receipt_set_sha256": content_sha256({
            "system_id": system_id,
            "query_at_utc": query_at_utc,
        }),
        "points": [
            {"valid_time_utc": "2026-07-02T00:00:00Z", "temperature_c": 19.0},
            {"valid_time_utc": "2026-07-02T06:00:00Z", "temperature_c": 20.0},
            {"valid_time_utc": "2026-07-02T12:00:00Z", "temperature_c": 21.0},
            {"valid_time_utc": "2026-07-02T18:00:00Z", "temperature_c": 20.0},
        ],
    })
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
        "family_temperature_curve": family_curve,
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


def _insert_official_native_mixed_cycle_archive(
    conn: sqlite3.Connection,
    cycle_by_system: dict[str, str],
) -> None:
    """Add an explicit native archive for mixed-cycle compiler tests.

    The shared pipeline fixture intentionally represents Open-Meteo historical
    fallback rows.  These tests exercise the *official* mixed-cycle lineage,
    so they must provide separate native archive receipts instead of relying
    on the republisher rows and expecting the loader to infer publication.
    Test-only system IDs are registered in the local scope map below; the
    production scope contract remains unchanged.
    """

    provider_by_system = {
        "ECMWF_IFS_TEST": "ECMWF_OPEN_DATA_S3",
        "ECMWF_AIFS_TEST": "ECMWF_OPEN_DATA_S3",
        "NOAA_GEFS_TEST": "NOAA_GEFS_PDS_S3",
    }
    provider_label = {
        "ECMWF_OPEN_DATA_S3": "ECMWF",
        "NOAA_GEFS_PDS_S3": "NOAA",
    }
    rows = conn.execute(
        "SELECT ensemble_system_id,target_local_date,member_id,member_role,"
        "raw_tmax_c,settlement_definition,extraction_version,"
        "source_object_sha256,archive_query_at_utc "
        "FROM calibration_republisher_ensemble_historical_member_run "
        "WHERE station_id='TEST' ORDER BY target_local_date,ensemble_system_id,member_id"
    ).fetchall()
    for (
        system_id,
        target_day,
        member_id,
        member_role,
        raw_tmax_c,
        settlement_definition,
        extraction_version,
        source_object_sha256,
        archive_query_at_utc,
    ) in rows:
        system_id = str(system_id)
        provider = provider_by_system[system_id]
        cycle = str(cycle_by_system[system_id])
        cycle_hour = int(cycle[:2])
        target = datetime.fromisoformat(str(target_day)).replace(
            tzinfo=timezone.utc
        )
        run_init = target - timedelta(days=2) + timedelta(hours=cycle_hour)
        identity = {
            "historical_member_run_id": (
                "official-mixed-"
                + system_id
                + "-"
                + str(target_day)
                + "-"
                + str(member_id)
            ),
            "ensemble_system_id": system_id,
            "station_id": "TEST",
            "target_local_date": str(target_day),
            "archive_lead_bucket": "d2",
            "run_cycle": cycle,
            "member_id": str(member_id),
            "member_role": str(member_role),
            "raw_tmax_c": float(raw_tmax_c),
            "settlement_definition": str(settlement_definition),
            "extraction_version": str(extraction_version),
            "provider_id": provider_label[provider],
            "transport_provider_id": provider,
            "native_run_init_utc": run_init.isoformat().replace(
                "+00:00", "Z"
            ),
            "offline_oos_eligible": 1,
            "archive_query_at_utc": str(archive_query_at_utc),
            "source_url": "https://example.test/native/" + system_id,
            "raw_object_ref": "native-test/" + system_id + "/" + str(target_day),
            "source_object_sha256": str(source_object_sha256),
            "qc_status": "ACCEPTED",
            "authority_scope": "CALIBRATION_TRAIN_ONLY",
            "holdout_eligible": 0,
        }
        identity["content_sha256"] = content_sha256(identity)
        columns = tuple(identity)
        conn.execute(
            "INSERT INTO calibration_native_ensemble_historical_member_run("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(identity[column] for column in columns),
        )
    conn.commit()


def _official_mixed_cycle_report(
    report: dict[str, object],
) -> dict[str, object]:
    """Keep one explicit native candidate for these tests.

    The tri-system report is mixed-cycle; a two-system pair legitimately has
    the same 18Z cycle and is retained as that official pair candidate.
    """

    all_official = [
        candidate
        for candidate in report.get("cross_candidates", [])
        if isinstance(candidate, dict)
        and candidate.get("republisher_fallback_used") is False
    ]
    mixed = [
        candidate
        for candidate in all_official
        if candidate.get("run_cycle") == "MIXED_NATIVE_CYCLES"
    ]
    candidates = mixed or all_official
    assert len(candidates) == 1
    identity = {
        key: value for key, value in report.items() if key != "content_sha256"
    }
    identity["cross_candidates"] = candidates
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def _install_mixed_cycle_calibrations(
    conn: sqlite3.Connection,
) -> dict[str, object]:
    cycle_by_system = {
        "ECMWF_IFS_TEST": "18Z",
        "ECMWF_AIFS_TEST": "00Z",
        "NOAA_GEFS_TEST": "18Z",
    }
    _insert_fixture(
        conn,
        cycle_by_system=cycle_by_system,
        lead_bucket="d2",
    )
    _insert_official_native_mixed_cycle_archive(conn, cycle_by_system)
    original_scopes = {
        system_id: native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS.get(
            system_id
        )
        for system_id in cycle_by_system
    }
    try:
        for system_id, cycle in cycle_by_system.items():
            provider = {
                "ECMWF_IFS_TEST": "ECMWF_OPEN_DATA_S3",
                "ECMWF_AIFS_TEST": "ECMWF_OPEN_DATA_S3",
                "NOAA_GEFS_TEST": "NOAA_GEFS_PDS_S3",
            }[system_id]
            native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS[system_id] = {
                "transport_provider_id": provider,
                "lead_bucket": "d2",
                "run_cycle": cycle,
            }
        report = build_native_ensemble_oos(
            conn,
            station_id="TEST",
            as_of_utc="2026-07-01T00:00:00Z",
        )
    finally:
        for system_id, original in original_scopes.items():
            if original is None:
                native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS.pop(
                    system_id, None
                )
            else:
                native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS[system_id] = original
    report = _official_mixed_cycle_report(report)
    persisted = persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    assert persisted["state"] == "COMMITTED"
    return report


def test_mixed_native_cycles_use_per_system_oos_lineage_and_family_gate() -> None:
    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)

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
    root_curve = json.loads(
        lineage["conditional_future_temperature_curve_json"]
    )
    envelope = compiled["packet"]["native_probability_envelope"]
    assert root_curve["root_checkpoint_id"] == envelope["checkpoint_id"]
    assert root_curve["root_probability_sha256"] == envelope[
        "posterior_sha256"
    ]
    assert root_curve["content_sha256"] == lineage[
        "conditional_future_temperature_curve_sha256"
    ]
    assert len(root_curve["independence_family_ids"]) == 2
    assert len(root_curve["points"]) == 4
    assert compiled["forward_prediction"]["run_cycle_by_system_json"]
    assert compiled["forward_prediction"][
        "run_cycle_by_system_sha256"
    ] == content_sha256(packet["run_cycle_by_system"])
    assert persist_native_ensemble_forward_prediction(
        conn,
        compiled,
    )["state"] == "COMMITTED"
    assert conn.execute(
        "SELECT run_cycle,run_cycle_by_system_json,"
        "run_cycle_by_system_sha256 "
        "FROM calibration_cross_native_ensemble_oos_prediction "
        "WHERE event_date='2026-07-02'"
    ).fetchone() == (
        "MIXED_NATIVE_CYCLES",
        json.dumps(
            packet["run_cycle_by_system"],
            sort_keys=True,
            separators=(",", ":"),
        ),
        content_sha256(packet["run_cycle_by_system"]),
    )


def test_native_p0_capture_freshness_is_not_restarted_by_compilation() -> None:
    """A late native receipt must leave room for governed OM fallback.

    A receipt one second inside the boundary remains usable; the exact TTL
    and a one-second overrun both fail before the old packet can obtain a new
    freshness window merely by recompiling.
    """

    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    packet = _mixed_packet()
    captured = datetime.fromisoformat(
        str(packet["received_at_utc"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    still_fresh_decision = captured + timedelta(seconds=59)

    still_fresh = compile_native_ensemble_p0(
        conn,
        forward_packet=packet,
        decision_at_utc=(
            still_fresh_decision.isoformat().replace("+00:00", "Z")
        ),
        fresh_seconds=60,
    )
    assert (
        still_fresh["packet"]["native_probability_envelope"]["fresh_until_utc"]
        == (captured + timedelta(seconds=60)).isoformat().replace(
            "+00:00", "Z"
        )
    )

    with pytest.raises(
        ContractError,
        match="native_ensemble_snapshot_stale",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=packet,
            decision_at_utc=(captured + timedelta(seconds=60)).isoformat().replace(
                "+00:00", "Z"
            ),
            fresh_seconds=60,
        )

    with pytest.raises(
        ContractError,
        match="native_ensemble_snapshot_stale",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=packet,
            decision_at_utc=(captured + timedelta(seconds=61)).isoformat().replace(
                "+00:00", "Z"
            ),
            fresh_seconds=60,
        )


def test_latest_exact_oos_rejection_blocks_older_activation() -> None:
    conn = sqlite3.connect(":memory:")
    report = _install_mixed_cycle_calibrations(conn)
    rejected = copy.deepcopy(report)
    for candidate in rejected["cross_candidates"]:
        candidate["state"] = "OOS_REJECTED"
    rejected_identity = {
        key: value
        for key, value in rejected.items()
        if key != "content_sha256"
    }
    rejected["content_sha256"] = content_sha256(rejected_identity)
    persisted = persist_native_ensemble_oos(
        conn,
        rejected,
        activated_at_utc="2026-07-01T12:00:00Z",
    )
    assert persisted["state"] == "COMMITTED_EVALUATIONS_ONLY"
    assert persisted["counts"]["oos_evaluations"] == 1

    with pytest.raises(
        ContractError,
        match="native_cross_oos_evaluation_not_passing",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=_mixed_packet(),
            decision_at_utc="2026-07-01T13:00:00Z",
        )


def test_mixed_packet_falls_back_to_passing_pair_after_tri_rejection() -> None:
    conn = sqlite3.connect(":memory:")
    cycle_by_system = {
        "ECMWF_IFS_TEST": "18Z",
        "ECMWF_AIFS_TEST": "00Z",
        "NOAA_GEFS_TEST": "18Z",
    }
    _insert_fixture(
        conn,
        cycle_by_system=cycle_by_system,
        lead_bucket="d2",
    )
    _insert_official_native_mixed_cycle_archive(conn, cycle_by_system)
    original_scopes = {
        system_id: native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS.get(
            system_id
        )
        for system_id in cycle_by_system
    }
    for system_id, cycle in cycle_by_system.items():
        provider = {
            "ECMWF_IFS_TEST": "ECMWF_OPEN_DATA_S3",
            "ECMWF_AIFS_TEST": "ECMWF_OPEN_DATA_S3",
            "NOAA_GEFS_TEST": "NOAA_GEFS_PDS_S3",
        }[system_id]
        native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS[system_id] = {
            "transport_provider_id": provider,
            "lead_bucket": "d2",
            "run_cycle": cycle,
        }
    pair_report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
        system_mask=("ECMWF_IFS_TEST", "NOAA_GEFS_TEST"),
    )
    tri_report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    pair_report = _official_mixed_cycle_report(pair_report)
    tri_report = _official_mixed_cycle_report(tri_report)
    for system_id, original in original_scopes.items():
        if original is None:
            native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS.pop(
                system_id, None
            )
        else:
            native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS[system_id] = original
    persist_native_ensemble_oos(
        conn,
        pair_report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    persist_native_ensemble_oos(
        conn,
        tri_report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    rejected_tri = copy.deepcopy(tri_report)
    for candidate in rejected_tri["cross_candidates"]:
        candidate["state"] = "OOS_REJECTED"
    rejected_identity = {
        key: value
        for key, value in rejected_tri.items()
        if key != "content_sha256"
    }
    rejected_tri["content_sha256"] = content_sha256(rejected_identity)
    persist_native_ensemble_oos(
        conn,
        rejected_tri,
        activated_at_utc="2026-07-01T12:00:00Z",
    )

    compiled = compile_native_ensemble_p0(
        conn,
        forward_packet=_mixed_packet(),
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    lineage = compiled["packet"]["native_probability_envelope"]["lineage"]
    assert json.loads(lineage["ensemble_system_ids_json"]) == [
        "ECMWF_IFS_TEST",
        "NOAA_GEFS_TEST",
    ]
    assert lineage["run_cycle"] == "18Z"
    assert lineage["run_cycle_scope"] == "SAME_NATIVE_CYCLE"


def test_mixed_native_cycles_missing_system_lineage_fail_closed() -> None:
    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
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
    _install_mixed_cycle_calibrations(conn)
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
        match="native_cross_ensemble_activation_not_exact",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=broken,
            decision_at_utc="2026-07-01T13:00:00Z",
        )


def test_official_native_p0_rejects_open_meteo_transport_forgery() -> None:
    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    forged = copy.deepcopy(_mixed_packet())
    for source_archive in forged["source_archives"]:
        if source_archive["ensemble_system_id"] == "NOAA_GEFS_TEST":
            source_archive["transport_provider_id"] = "OPEN_METEO"
    identity = {
        key: value
        for key, value in forged.items()
        if key != "snapshot_content_sha256"
    }
    forged["snapshot_content_sha256"] = content_sha256(identity)

    with pytest.raises(
        ContractError,
        match="FAIL_CLOSED_NO_P0:native_ensemble_transport_not_official",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=forged,
            decision_at_utc="2026-07-01T13:00:00Z",
        )
