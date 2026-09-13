from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Mapping
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


@pytest.fixture(autouse=True)
def _map_the_fixture_station_season(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixtures compile station ``TEST``; station-local seasons (charter
    62) fail closed on an unmapped station, which turned every compile in
    this file into ``station_season_station_unmapped``.  Map it like a
    northern mid-latitude station for the duration of each test."""

    from bayes_basket_target import station_season

    monkeypatch.setitem(station_season._SEASON_ANCHOR_MONTH, "TEST", 7)


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


def _mixed_packet(
    *, aifs_query_at_utc: str = "2026-07-01T12:01:00Z"
) -> dict[str, object]:
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
                query_at_utc=aifs_query_at_utc,
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
    # The cross layer seats the best subset spanning two independence
    # families; the two ECMWF fixture systems share one, so the seated set
    # is a subset of the packet's.  Every per-system lineage map is keyed on
    # the seated systems and agrees with the packet on each of them.
    seated = set(json.loads(lineage["ensemble_system_ids_json"]))
    assert 2 <= len(seated) <= len(packet["run_cycle_by_system"])
    assert seated <= set(packet["run_cycle_by_system"])
    assert json.loads(lineage["run_cycle_by_system_json"]) == {
        system: packet["run_cycle_by_system"][system] for system in seated
    }
    assert json.loads(lineage["run_init_utc_by_system_json"]) == {
        system: packet["run_init_utc_by_system"][system] for system in seated
    }
    assert json.loads(
        lineage["archive_query_at_utc_by_system_json"]
    ) == {
        system: packet["archive_query_at_utc_by_system"][system]
        for system in seated
    }
    taf = json.loads(lineage["taf_nwp_live_compatibility_json"])
    assert taf["run_cycle"] == "MIXED_NATIVE_CYCLES"
    assert taf["run_cycle_by_system"] == {
        system: packet["run_cycle_by_system"][system] for system in seated
    }
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
    ] == content_sha256({
        system: packet["run_cycle_by_system"][system] for system in seated
    })
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
            {
                system: packet["run_cycle_by_system"][system]
                for system in seated
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        content_sha256({
            system: packet["run_cycle_by_system"][system] for system in seated
        }),
    )


def test_a_cycle_without_its_own_fit_borrows_the_systems_own_not_a_peers() -> None:
    """Charter 101: the provisional path asks the system before its peers.

    Measured 2026-09-06: the four incumbent global ensembles walk this path
    on 06Z and 12Z packets because the training lane has fitted only 00Z and
    18Z -- ECMWF holds 14,178 artifacts across all 45 stations and not one
    for those cycles -- and the peer borrow then refuses bias in 95% of
    cases for want of three certified peers, serving the raw centre of a
    family measured to run 0.8C cold.  The system's own fit at another cycle
    differs by 0.198C median, four to six times less than that zero costs.
    """

    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)

    packet = copy.deepcopy(_mixed_packet())
    # The one change: this system is served on a cycle nobody fitted.
    starved = "NOAA_GEFS_TEST"
    donor_cycle = packet["run_cycle_by_system"]["ECMWF_AIFS_TEST"]
    packet["run_cycle_by_system"][starved] = donor_cycle
    packet["run_init_utc_by_system"][starved] = packet[
        "run_init_utc_by_system"
    ]["ECMWF_AIFS_TEST"]
    packet["archive_query_at_utc_by_system"][starved] = packet[
        "archive_query_at_utc_by_system"
    ]["ECMWF_AIFS_TEST"]
    receipt = packet["availability_evidence"]["by_system"][starved]
    receipt["run_cycle"] = donor_cycle
    receipt["run_init_utc"] = packet["run_init_utc_by_system"][starved]
    receipt["archive_query_at_utc"] = packet[
        "archive_query_at_utc_by_system"
    ][starved]
    for system in packet.get("systems") or []:
        if system.get("ensemble_system_id") == starved:
            system["run_cycle"] = donor_cycle
            system["run_init_utc"] = packet["run_init_utc_by_system"][starved]
    # Every layer of this fixture attests to itself, so a mutated one has to
    # re-attest at each level or the compile refuses it long before reaching
    # what is under test.
    availability = packet["availability_evidence"]
    availability["content_sha256"] = content_sha256({
        key: value for key, value in availability.items()
        if key != "content_sha256"
    })
    packet["snapshot_content_sha256"] = content_sha256({
        key: value for key, value in packet.items()
        if key != "snapshot_content_sha256"
    })

    compiled = compile_native_ensemble_p0(
        conn,
        forward_packet=packet,
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    lineage = compiled["packet"]["native_probability_envelope"]["lineage"]
    sources = json.loads(lineage["provisional_parameter_sources_json"])
    assert starved in sources, (
        "the starved cycle should have taken the provisional path"
    )
    entry = sources[starved]
    assert entry["source"] == "OWN_FIT_OTHER_RUN_CYCLE"
    assert entry["borrowed_from_run_cycle"] == "18Z"  # its own fitted cycle
    assert entry["borrowed_artifact_sha256"]
    # Its own number, not a peer median and not the zero the peer path
    # returns when three certified peers are not available.
    assert any(
        str(note).startswith("own_fit_from_run_") for note in entry["notes"]
    )
    assert not any("bias_refused" in str(note) for note in entry["notes"])

    # The off arm is the behaviour this replaces, pinned so the difference
    # stays visible: two certified peers is one short of the three the peer
    # median needs, so the borrow refuses and the member is served at its
    # raw centre -- the 0.8C cold error the measurement above names.
    off = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(off)
    import bayes_basket_target.native_ensemble_p0 as _p0
    saved = _p0.CROSS_CYCLE_PARAMETER_BORROW
    _p0.CROSS_CYCLE_PARAMETER_BORROW = "off"
    try:
        compiled_off = compile_native_ensemble_p0(
            off, forward_packet=packet, decision_at_utc="2026-07-01T13:00:00Z",
        )
    finally:
        _p0.CROSS_CYCLE_PARAMETER_BORROW = saved
    off_sources = json.loads(
        compiled_off["packet"]["native_probability_envelope"]["lineage"][
            "provisional_parameter_sources_json"
        ]
    )
    off_entry = off_sources[starved]
    assert off_entry["source"] == "PEER_MEDIAN_STATION_LOCAL"
    assert off_entry["borrowed_from_run_cycle"] is None
    assert off_entry["bias_c"] != entry["bias_c"]


def test_native_p0_publishes_per_bucket_model_reliability() -> None:
    """The support kernel's peripheral gate reads this key; until 2026-09-02
    no native envelope carried it, so every peripheral candidate read 0.0
    against a 0.15 floor and the disagreement-expansion layer never ran."""

    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    compiled = compile_native_ensemble_p0(
        conn,
        forward_packet=_mixed_packet(),
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    envelope = compiled["packet"]["native_probability_envelope"]
    lineage = envelope["lineage"]
    reliability = json.loads(
        lineage["peripheral_model_reliability_by_bucket_json"]
    )
    # Full support, so a later stage's support check sees the same key set.
    assert set(reliability) == set(envelope["probabilities"])
    weights = json.loads(lineage["system_weights_json"])
    centers = json.loads(lineage["native_calibrated_system_centers_c_json"])
    assert set(weights) == set(centers)
    # Every system's weight lands in the bucket its calibrated centre falls
    # in; nothing else carries any.
    assert sum(reliability.values()) == pytest.approx(
        sum(float(value) for value in weights.values()), abs=1e-9
    )
    assert all(0.0 <= value <= 1.0 for value in reliability.values())
    assert any(value > 0.0 for value in reliability.values())


def test_native_p0_capture_freshness_is_not_restarted_by_compilation() -> None:
    """A late native receipt must leave room for governed OM fallback.

    A receipt one second inside the boundary remains usable; the exact TTL
    and a one-second overrun both fail before the old packet can obtain a new
    freshness window merely by recompiling.
    """

    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    packet = _mixed_packet()
    # A packet is usable while at least MIN_USABLE_FRESHNESS_SECONDS of its
    # window remain (2026-08-28, 6e9f7b59), so the boundary sits one minute
    # inside the TTL, and the TTL itself has to be longer than that minute.
    from bayes_basket_target.contracts import MIN_USABLE_FRESHNESS_SECONDS

    captured = datetime.fromisoformat(
        str(packet["received_at_utc"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    ttl = 3600
    last_usable = captured + timedelta(
        seconds=ttl - MIN_USABLE_FRESHNESS_SECONDS
    )

    still_fresh = compile_native_ensemble_p0(
        conn,
        forward_packet=packet,
        decision_at_utc=last_usable.isoformat().replace("+00:00", "Z"),
        fresh_seconds=ttl,
    )
    assert (
        still_fresh["packet"]["native_probability_envelope"]["fresh_until_utc"]
        == (captured + timedelta(seconds=ttl)).isoformat().replace(
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
            decision_at_utc=(
                last_usable + timedelta(seconds=1)
            ).isoformat().replace("+00:00", "Z"),
            fresh_seconds=ttl,
        )

    with pytest.raises(
        ContractError,
        match="native_ensemble_snapshot_stale",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=packet,
            decision_at_utc=(
                captured + timedelta(seconds=ttl + 1)
            ).isoformat().replace("+00:00", "Z"),
            fresh_seconds=ttl,
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


def _install_pair_after_tri_rejection() -> sqlite3.Connection:
    """ECMWF_IFS+GEFS pass OOS; the three-system blend is rejected."""

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

    return conn


def test_mixed_packet_falls_back_to_passing_pair_after_tri_rejection() -> None:
    conn = _install_pair_after_tri_rejection()
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


def test_an_archive_left_out_of_the_blend_does_not_renew_the_blend() -> None:
    """The window runs from the newest receipt among the systems seated.

    AIFS is in the snapshot and out of the blend (the three-system candidate
    is rejected, the ECMWF_IFS+GEFS pair serves).  Its archive is also the
    newest in the packet, so under the packet clock it renewed a probability
    it contributes nothing to -- the shape of every one of the 349 same-run
    P0 re-mints that extended freshness on 2026-09-11..12.
    """

    from bayes_basket_target.contracts import MIN_USABLE_FRESHNESS_SECONDS
    from bayes_basket_target.native_ensemble_p0 import (
        seated_evidence_received_at,
    )

    conn = _install_pair_after_tri_rejection()
    packet = _mixed_packet(aifs_query_at_utc="2026-07-01T12:30:00Z")
    assert packet["received_at_utc"] == "2026-07-01T12:30:00Z"
    ttl = 3600
    compiled = compile_native_ensemble_p0(
        conn,
        forward_packet=packet,
        decision_at_utc="2026-07-01T12:40:00Z",
        fresh_seconds=ttl,
    )
    envelope = compiled["packet"]["native_probability_envelope"]
    assert json.loads(envelope["lineage"]["ensemble_system_ids_json"]) == [
        "ECMWF_IFS_TEST", "NOAA_GEFS_TEST",
    ]
    # GEFS at 12:02 is the newest seated receipt; AIFS at 12:30 lends nothing.
    assert envelope["fresh_until_utc"] == "2026-07-01T13:02:00Z"
    assert compiled["packet"]["fresh_until_utc"] == "2026-07-01T13:02:00Z"
    # The probe reads the same clock through the same function.
    assert seated_evidence_received_at(
        packet["archive_query_at_utc_by_system"],
        seated_system_ids=["ECMWF_IFS_TEST", "NOAA_GEFS_TEST"],
    ) == datetime(2026, 7, 1, 12, 2, tzinfo=timezone.utc)
    last_usable = datetime(2026, 7, 1, 13, 2, tzinfo=timezone.utc) - timedelta(
        seconds=MIN_USABLE_FRESHNESS_SECONDS
    )
    compile_native_ensemble_p0(
        conn,
        forward_packet=packet,
        decision_at_utc=last_usable.isoformat().replace("+00:00", "Z"),
        fresh_seconds=ttl,
    )
    # Past it, the packet clock would still say 28 minutes remain; the
    # compile refuses on the evidence it would actually serve.
    with pytest.raises(
        ContractError, match="native_ensemble_seated_evidence_stale"
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=packet,
            decision_at_utc=(
                last_usable + timedelta(seconds=1)
            ).isoformat().replace("+00:00", "Z"),
            fresh_seconds=ttl,
        )


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


def test_mixed_native_cycles_serve_with_named_riders() -> None:
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
    # A split packet is no longer unresolvable on its face: captures read
    # each family at its own newest published run, so adjacent-bucket splits
    # are the ordinary daytime state.  Every bucket the packet touches is
    # tried freshest-first, the certified activation keeps its fitted
    # identity, and the shifted system serves as a named rider -- once
    # through the split (its run sits outside the served bucket), once
    # through the cycle map (its run differs from the fitted pairing), and
    # once provisionally (it lacks its own per-system calibration).  The
    # old fixture asserted fail-closed here; that premise was retired by
    # the entry doctrine, and what the lineage must now do is name every
    # borrow instead of refusing the packet.
    out = compile_native_ensemble_p0(
        conn,
        forward_packet=broken,
        decision_at_utc="2026-07-01T13:00:00Z",
    )

    def _lineage_value(obj: object, key: str) -> object:
        if isinstance(obj, Mapping):
            if key in obj:
                return obj[key]
            for value in obj.values():
                found = _lineage_value(value, key)
                if found is not None:
                    return found
        return None

    assert _lineage_value(out, "training_lead_bucket") == "h24_48"
    assert json.loads(
        str(_lineage_value(out, "activation_cycle_riders_json"))
    ) == ["ECMWF_AIFS_TEST"]
    assert json.loads(
        str(_lineage_value(out, "hour_bucket_split_riders_json"))
    ) == ["ECMWF_AIFS_TEST"]
    assert json.loads(
        str(_lineage_value(out, "provisional_uncalibrated_system_ids_json"))
    ) == ["ECMWF_AIFS_TEST"]


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


def test_taf_envelope_witness_time_precedence_and_absence() -> None:
    """The envelope may only cite a TAF issued before the decision instant."""

    from bayes_basket_target.native_ensemble_p0 import _taf_envelope_witness

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_forward_taf_capture ("
        "station_id TEXT, event_date TEXT, tx_c REAL, issued_at_utc TEXT,"
        "revision_sequence INTEGER, content_sha256 TEXT, revision_id TEXT,"
        "cloud_cover_fraction REAL, precipitation_probability REAL,"
        "wind_speed_mps REAL, received_at_utc TEXT)"
    )
    rows = [
        ("ZUUU", "2026-08-12", 34.0, "2026-08-11T09:00:00Z", 3, "a" * 64, "r3",
         0.2, 0.0, 3.0, "2026-08-11T09:05:00Z"),
        ("ZUUU", "2026-08-12", 35.0, "2026-08-11T21:02:00Z", 5, "b" * 64, "r5",
         0.4, 0.1, 4.0, "2026-08-11T21:07:00Z"),
        ("ZUUU", "2026-08-12", None, "2026-08-11T23:00:00Z", 6, "c" * 64, "r6",
         0.5, 0.2, 5.0, "2026-08-11T23:05:00Z"),
        ("KLGA", "2026-08-12", None, "2026-08-11T21:00:00Z", 1, "d" * 64, "r1",
         None, None, None, "2026-08-11T21:05:00Z"),
    ]
    conn.executemany(
        "INSERT INTO calibration_forward_taf_capture "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    late = _taf_envelope_witness(
        conn, station_id="ZUUU", event_date="2026-08-12",
        decision_at_utc="2026-08-12T01:00:00Z",
    )
    assert late is not None and late["tx_c"] == "35"
    assert late["capture_content_sha256"] == "b" * 64
    early = _taf_envelope_witness(
        conn, station_id="ZUUU", event_date="2026-08-12",
        decision_at_utc="2026-08-11T10:00:00Z",
    )
    assert early is not None and early["tx_c"] == "34"
    assert _taf_envelope_witness(
        conn, station_id="ZUUU", event_date="2026-08-12",
        decision_at_utc="2026-08-11T08:00:00Z",
    ) is None
    # Issued before the decision but not yet received: not knowable, so the
    # older bulletin is still the witness (2026-09-13 review).
    between = _taf_envelope_witness(
        conn, station_id="ZUUU", event_date="2026-08-12",
        decision_at_utc="2026-08-11T21:04:00Z",
    )
    assert between is not None and between["tx_c"] == "34"
    assert _taf_envelope_witness(
        conn, station_id="KLGA", event_date="2026-08-12",
        decision_at_utc="2026-08-12T01:00:00Z",
    ) is None
    no_table = sqlite3.connect(":memory:")
    assert _taf_envelope_witness(
        no_table, station_id="ZUUU", event_date="2026-08-12",
        decision_at_utc="2026-08-12T01:00:00Z",
    ) is None


def test_taf_point_archive_joins_packet_with_cycle_label_waiver() -> None:
    """A TAF point archive enters the packet under its scope label.

    The label is not the issuance hour -- TAF issue instants jitter across
    the 6h UTC grid -- so only the temporal ordering against the query time
    is enforced for point systems, while model archives keep the strict
    init-hour==cycle identity.
    """

    from bayes_basket_target.official_native_forward_packet import (
        OFFICIAL_TAF_TX_MULTISTATION_SCHEMA,
    )

    models = [
        _archive(
            system_id="ECMWF_IFS_TEST", family_id="ECMWF",
            schema=ECMWF_MULTISTATION_SCHEMA, run_cycle="18Z",
            run_init_utc="2026-06-30T18:00:00Z",
            query_at_utc="2026-07-01T02:00:00Z",
        ),
        _archive(
            system_id="NOAA_GEFS_TEST", family_id="NOAA_GEFS",
            schema=NOAA_GEFS_MULTISTATION_SCHEMA, run_cycle="18Z",
            run_init_utc="2026-06-30T18:00:00Z",
            query_at_utc="2026-07-01T02:00:00Z",
        ),
    ]
    member = {
        "member_id": "TX_POINT",
        "member_role": "CONTROL",
        "raw_tmax_c": 20.0,
    }
    model_intervals = models[0]["captures"][0]["bucket_intervals"]
    capture_identity = {
        "station_id": "TEST",
        "ensemble_system_id": "OFFICIAL_AIRPORT_TAF_TX",
        "independence_family_id": "HUMAN_FORECASTER",
        "event_date": "2026-07-02",
        "settlement_timezone": "Etc/UTC",
        "run_cycle": "12Z",
        "archive_lead_bucket": "d2",
        "member_count": 1,
        "members": [member],
        "member_set_sha256": content_sha256([member]),
        "bucket_intervals": model_intervals,
        "bucket_contract_sha256": content_sha256(model_intervals),
        "source_capture_content_sha256": "d" * 64,
    }
    capture = {
        **capture_identity,
        "content_sha256": content_sha256(capture_identity),
    }
    taf_identity = {
        "schema": OFFICIAL_TAF_TX_MULTISTATION_SCHEMA,
        "ensemble_system_id": "OFFICIAL_AIRPORT_TAF_TX",
        "independence_family_id": "HUMAN_FORECASTER",
        "event_date": "2026-07-02",
        "archive_lead_bucket": "d2",
        # The label (12Z) deliberately disagrees with the issuance hour
        # (21:02Z): that is the exact waiver under test.
        "run_cycle": "12Z",
        "run_init_utc": "2026-06-30T21:02:00Z",
        "archive_query_at_utc": "2026-07-01T02:00:00Z",
        "member_count": 1,
        "training_only": True,
        "holdout_eligible": False,
        "captures": [capture],
    }
    taf_archive = {
        **taf_identity,
        "content_sha256": content_sha256(taf_identity),
    }
    packet = build_official_native_forward_packet(
        archives=[*models, taf_archive],
        station_id="TEST",
    )
    assert "OFFICIAL_AIRPORT_TAF_TX" in packet["run_cycle_by_system"]
    assert packet["run_cycle_by_system"]["OFFICIAL_AIRPORT_TAF_TX"] == "12Z"
    assert (
        packet["run_init_utc_by_system"]["OFFICIAL_AIRPORT_TAF_TX"]
        == "2026-06-30T21:02:00Z"
    )
    taf_entries = [
        entry for entry in packet["systems"]
        if entry["ensemble_system_id"] == "OFFICIAL_AIRPORT_TAF_TX"
    ]
    assert len(taf_entries) == 1
    assert len(taf_entries[0]["members"]) == 1
    assert taf_entries[0]["members"][0]["raw_tmax_c"] == 20.0

    # A model archive gets no such waiver: shifting its init hour off its
    # cycle must still refuse.
    broken_model_identity = {
        key: value
        for key, value in models[0].items()
        if key != "content_sha256"
    }
    broken_model_identity["run_init_utc"] = "2026-06-30T21:02:00Z"
    broken_model = {
        **broken_model_identity,
        "content_sha256": content_sha256(broken_model_identity),
    }
    with pytest.raises(
        ContractError,
        match="official_native_forward_run_cycle_lineage_mismatch",
    ):
        build_official_native_forward_packet(
            archives=[broken_model, models[1], taf_archive],
            station_id="TEST",
        )


def test_taf_projection_archives_only_final_pre_anchor_issuance() -> None:
    """The archive keeps the FINAL pre-anchor TAF, never the first amendment.

    The member archive is append-only with one row per (station, day, lead),
    so projecting before the anchor would freeze whichever amendment arrived
    first and later revisions would be silently ignored -- training would
    then grade an issuance live serving never uses.
    """

    from bayes_basket_target.native_ensemble_training_facts import (
        project_taf_point_member_runs,
    )
    from tests.pr12_atomic_test_support import install_canonical_v12
    from bayes_basket_target.ssot import apply_production_ssot_schema

    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    install_canonical_v12(conn)
    conn.execute(
        "INSERT INTO calibration_station_registry("
        "station_id,icao_code,city_label,timezone_name,source_lane,"
        "settlement_definition,registry_version,source_role,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "ZUUU", "ZUUU", "Chengdu", "Asia/Shanghai", "AWC",
            "daily_high_local_midnight_to_midnight", "v1",
            "official", "e" * 64,
        ),
    )
    def _capture(tx: float, issued: str, seq: int) -> None:
        conn.execute(
            "INSERT INTO calibration_forward_taf_capture("
            "taf_capture_id,station_id,event_date,settlement_timezone,"
            "revision_id,revision_kind,revision_sequence,issued_at_utc,"
            "received_at_utc,provider_available_at_utc,valid_from_utc,"
            "valid_until_utc,source_object_sha256,source_capture_sha256,"
            "tx_c,tx_available,cloud_cover_fraction,"
            "precipitation_probability,wind_speed_mps,source_class,"
            "authority_scope,content_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"cap-{seq}", "ZUUU", "2026-08-20", "Asia/Shanghai",
                f"rev-{seq}", "TAF", seq, issued, issued, issued,
                "2026-08-20T00:00:00Z", "2026-08-21T00:00:00Z",
                "a" * 64, "b" * 64, tx, 1, 0.0, 0.0, 1.0,
                "official_forward_native_receipt",
                "PRODUCTION_NO_LIVE_ONLY", f"{seq:064d}",
            ),
        )
    _capture(33.0, "2026-08-19T09:00:00Z", 1)
    # 15:00Z is 23:00 local on the 19th: still the day-before (d1), the
    # later amendment of the same pre-day window.
    _capture(35.0, "2026-08-19T15:00:00Z", 2)

    # Before the d1 anchor (event-day local midnight = 16:00Z on 08-19):
    # nothing may be archived, however many amendments exist.
    early = project_taf_point_member_runs(
        conn, as_of_utc="2026-08-19T12:00:00Z"
    )
    assert early["wrote_count"] == 0
    assert early["skipped"]["anchor_not_passed"] >= 1

    # After the anchor the final amendment (TX 35, 21:00Z) is the row.
    final = project_taf_point_member_runs(
        conn, as_of_utc="2026-08-19T23:00:00Z"
    )
    assert final["wrote_count"] == 1
    row = conn.execute(
        "SELECT raw_tmax_c,native_run_init_utc "
        "FROM calibration_native_ensemble_historical_member_run "
        "WHERE ensemble_system_id='OFFICIAL_AIRPORT_TAF_TX' "
        "AND station_id='ZUUU' AND target_local_date='2026-08-20'"
    ).fetchone()
    assert row == (35.0, "2026-08-19T15:00:00Z")


def test_forward_taf_ingest_reports_fresh_scopes_only_for_new_rows() -> None:
    """The ingest names newly written scopes; replays name nothing.

    The arrival-triggered recompile keys off this: a fresh issuance must wake
    its scope's packet refresh exactly once, and a replayed inbox must not
    keep re-waking it.
    """

    from bayes_basket_target.native_ensemble_training_facts import (
        ingest_forward_taf_inbox,
    )
    from bayes_basket_target.ssot import apply_production_ssot_schema
    from tests.pr12_atomic_test_support import install_canonical_v12

    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    install_canonical_v12(conn)
    conn.execute(
        "INSERT INTO calibration_station_registry("
        "station_id,icao_code,city_label,timezone_name,source_lane,"
        "settlement_definition,registry_version,source_role,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "ZUUU", "ZUUU", "Chengdu", "Asia/Shanghai", "AWC",
            "daily_high_local_midnight_to_midnight", "v1",
            "official", "e" * 64,
        ),
    )
    revision = {
        "schema": "bayes_basket_forecast_revision_v1",
        "revision_id": "taf-zuuu-fresh-1",
        "station_id": "ZUUU",
        "event_date": "2026-08-20",
        "revision_kind": "TAF",
        "sequence": 1,
        "issued_at_utc": "2026-08-19T09:00:00Z",
        "received_at_utc": "2026-08-19T09:05:00Z",
        "valid_from_utc": "2026-08-19T09:00:00Z",
        "valid_until_utc": "2026-08-20T18:00:00Z",
        "source_object_sha256": "c" * 64,
        "supersedes_revision_id": None,
    }
    capture_identity = {
        "schema": "bayes_basket_target_forward_taf_capture_v1",
        "source_class": "official_forward_native_receipt",
        "station_id": "ZUUU",
        "event_date": "2026-08-20",
        "settlement_timezone": "Asia/Shanghai",
        "forecast_revision": revision,
        "taf_fields": {
            "tx_c": 34.0,
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
        },
        "boundary": {
            "native_local_receipt": True,
            "retrospective_replay_only": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    inbox = {
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": [{
            **capture_identity,
            "content_sha256": content_sha256(capture_identity),
        }],
    }
    first = ingest_forward_taf_inbox(conn, inbox)
    assert first["wrote_count"] == 1
    assert first["fresh_scopes"] == [
        {"station_id": "ZUUU", "event_date": "2026-08-20"}
    ]
    replay = ingest_forward_taf_inbox(conn, inbox)
    assert replay["wrote_count"] == 0
    assert replay["fresh_scopes"] == []


def test_a_native_compile_is_always_p0_and_reads_only_a_knowable_taf() -> None:
    """P0-M belongs to the resident confirmation lane alone.

    This compiler used to label a same-run recompile P0-M whenever a TAF had
    been issued past the predecessor's witness; the P0 ingress then dropped
    the packet as a non-P0 stage, and the resident P0-M rows (which carry no
    witness) made that every same-run recompile of a TAF-bearing day -- 24 of
    90 live compiled packets on 2026-09-13.  The witness still widens the
    envelope (charter 52f: once the forecaster has spoken every compile reads
    it), but only a bulletin that had been *received* by the decision.
    """

    conn = sqlite3.connect(":memory:")
    _install_mixed_cycle_calibrations(conn)
    def _taf_row(revision_id, sequence, tx, issued, received, sha):
        return {
            "taf_capture_id": f"cap-{revision_id}",
            "station_id": "TEST",
            "event_date": "2026-07-02",
            "settlement_timezone": "Etc/UTC",
            "revision_id": revision_id,
            "revision_kind": "TAF" if sequence == 1 else "AMD",
            "revision_sequence": sequence,
            "issued_at_utc": issued,
            "received_at_utc": received,
            "provider_available_at_utc": received,
            "valid_from_utc": "2026-07-01T12:00:00Z",
            "valid_until_utc": "2026-07-02T18:00:00Z",
            "source_object_sha256": sha * 64,
            "source_capture_sha256": sha * 64,
            "tx_c": tx,
            "tx_available": 1,
            "cloud_cover_fraction": 0.2,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 3.0,
            "source_class": "official_forward_native_receipt",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "content_sha256": content_sha256([revision_id, sha]),
        }

    for row in (
        _taf_row("taf-known", 1, 21.0, "2026-07-01T11:00:00Z",
                 "2026-07-01T11:05:00Z", "c"),
        # Issued before the decision, received after it.
        _taf_row("taf-not-yet-received", 2, 23.0, "2026-07-01T12:30:00Z",
                 "2026-07-01T14:00:00Z", "d"),
    ):
        conn.execute(
            "INSERT INTO calibration_forward_taf_capture("
            + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")",
            tuple(row.values()),
        )
    compiled = compile_native_ensemble_p0(
        conn,
        forward_packet=_mixed_packet(),
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    envelope = compiled["packet"]["native_probability_envelope"]
    assert compiled["packet"]["stage"] == envelope["stage"] == "P0"
    assert envelope["checkpoint_id"].startswith("p0-native-")
    witness = json.loads(envelope["lineage"]["taf_envelope_witness_json"])
    assert witness["forecast_revision_id"] == "taf-known"
    # Once it has arrived it is read, and the compile is still P0.
    later = compile_native_ensemble_p0(
        conn,
        forward_packet=_mixed_packet(),
        decision_at_utc="2026-07-01T14:30:00Z",
    )
    later_envelope = later["packet"]["native_probability_envelope"]
    assert later_envelope["stage"] == "P0"
    assert json.loads(
        later_envelope["lineage"]["taf_envelope_witness_json"]
    )["forecast_revision_id"] == "taf-not-yet-received"
