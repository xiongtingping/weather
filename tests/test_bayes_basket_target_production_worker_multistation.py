from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import bayes_basket_target.production_worker as production_worker_module
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.production_worker import TargetProductionWorker
from bayes_basket_target.resource_bounds import load_bounded_json


def _worker(tmp_path: Path) -> TargetProductionWorker:
    worker = object.__new__(TargetProductionWorker)
    worker.conn = sqlite3.connect(":memory:")
    worker.commit_conn = None
    worker.canonical_source_db_path = tmp_path / "canonical.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ()
    worker.event_date_allowlist = ()
    worker._native_ensemble_scope_cursor = 0
    worker._nwp_rotation_state = None
    worker.nwp_rotation_state_path = tmp_path / "rotation-state.json"
    return worker


def _registry(stations: tuple[str, ...]) -> dict[str, object]:
    rows = [
        {
            "station_id": station,
            "event_date": "2026-07-31",
            "settlement_timezone": "Asia/Seoul",
            "outcome": "mid",
            "source_eligible": 1,
            "market_active": 1,
            "market_accepting_orders": 1,
        }
        for station in stations
    ]
    identity = {
        "schema": "bayes_basket_target_market_registry_snapshot_v1",
        "row_count": len(rows),
        "source_database_path": "/canonical.db",
        "rows": rows,
    }
    return {
        **identity,
        "snapshot_sha256": content_sha256(identity),
    }


def _packet(station_id: str) -> dict[str, object]:
    stable = {
        "station_id": station_id,
        "event_date": "2026-07-31",
        "systems": [
            {
                "ensemble_system_id": "ECMWF_IFS_ENS_GLOBAL",
                "independence_family_id": "ECMWF",
            },
            {
                "ensemble_system_id": "ECMWF_AIFS_ENS_GLOBAL",
                "independence_family_id": "ECMWF",
            },
            {
                "ensemble_system_id": "NOAA_GEFS_025",
                "independence_family_id": "NOAA_GEFS",
            },
            {
                "ensemble_system_id": "DWD_ICON_GLOBAL_EPS",
                "independence_family_id": "DWD_ICON_EPS",
            },
        ],
    }
    return {
        **stable,
        "unavailable_products": [],
        "cross_model_gate": {
            "ensemble_system_count": 4,
            "independent_family_count": 3,
            "independence_family_ids": [
                "DWD_ICON_EPS",
                "ECMWF",
                "NOAA_GEFS",
            ],
            "formal_p0_eligible": False,
        },
        "snapshot_content_sha256": content_sha256(stable),
        "packet_content_sha256": content_sha256(
            {**stable, "transport": "OPEN_METEO"}
        ),
    }


def test_native_truth_signature_changes_for_lexically_smaller_new_truth() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE calibration_native_ensemble_member_run("
        "station_id TEXT NOT NULL,target_local_date TEXT NOT NULL,"
        "authority_scope TEXT NOT NULL);"
        "CREATE TABLE calibration_station_daily_high_truth("
        "station_id TEXT NOT NULL,local_date TEXT NOT NULL,"
        "source_lane TEXT NOT NULL,qc_status TEXT NOT NULL,"
        "content_sha256 TEXT NOT NULL);"
    )
    conn.execute(
        "INSERT INTO calibration_native_ensemble_member_run VALUES(?,?,?)",
        ("TEST", "2026-07-30", "PRODUCTION_NO_LIVE_CANDIDATE"),
    )
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?)",
        ("TEST", "2026-07-30", "AWC", "ACCEPTED", "f" * 64),
    )
    before = production_worker_module._native_ensemble_truth_signatures(
        conn
    )
    conn.execute(
        "INSERT INTO calibration_native_ensemble_member_run VALUES(?,?,?)",
        ("TEST", "2026-07-31", "PRODUCTION_NO_LIVE_CANDIDATE"),
    )
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?)",
        ("TEST", "2026-07-31", "AWC", "ACCEPTED", "0" * 64),
    )

    after = production_worker_module._native_ensemble_truth_signatures(
        conn
    )

    assert before["TEST"] != after["TEST"]


def test_republisher_candidate_scopes_feed_closed_truth_and_oos_selection() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE calibration_republisher_ensemble_member_run("
        "station_id TEXT NOT NULL,target_local_date TEXT NOT NULL,"
        "authority_scope TEXT NOT NULL);"
        "CREATE TABLE basket_target_market_registry("
        "station_id TEXT NOT NULL,event_date TEXT NOT NULL,"
        "settlement_timezone TEXT NOT NULL,source_eligible INTEGER NOT NULL);"
        "CREATE TABLE calibration_station_daily_high_truth("
        "station_id TEXT NOT NULL,local_date TEXT NOT NULL,"
        "source_lane TEXT NOT NULL,qc_status TEXT NOT NULL,"
        "content_sha256 TEXT NOT NULL);"
    )
    conn.executemany(
        "INSERT INTO calibration_republisher_ensemble_member_run VALUES(?,?,?)",
        [
            ("CLOSED", "2026-07-30", "PRODUCTION_NO_LIVE_CANDIDATE"),
            ("LEGACY", "2026-07-30", "PRODUCTION_NO_LIVE_CANDIDATE"),
            ("PENDING", "2026-07-31", "PRODUCTION_NO_LIVE_CANDIDATE"),
        ],
    )
    conn.executemany(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?)",
        [
            ("CLOSED", "2026-07-30", "Etc/UTC", 1),
            ("LEGACY", "2026-07-30", "Etc/UTC", 1),
            ("PENDING", "2026-07-31", "Asia/Seoul", 1),
        ],
    )
    conn.executemany(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?)",
        [
            ("CLOSED", "2026-07-30", "AWC", "ACCEPTED", "a" * 64),
            ("LEGACY", "2026-07-30", "AMSC", "ACCEPTED", "b" * 64),
        ],
    )

    assert production_worker_module._candidate_ensemble_station_ids(
        conn,
        limit=45,
    ) == ["CLOSED", "LEGACY", "PENDING"]
    assert production_worker_module._native_ensemble_truth_signatures(
        conn
    ) == {
        "CLOSED": content_sha256({
            "station_id": "CLOSED",
            "accepted_truths": [{
                "local_date": "2026-07-30",
                "truth_content_sha256": "a" * 64,
            }],
        }),
    }
    assert production_worker_module._candidate_ensemble_scopes_without_truth(
        conn,
        limit=45,
    ) == [
        ("LEGACY", "2026-07-30", "Etc/UTC"),
        ("PENDING", "2026-07-31", "Asia/Seoul"),
    ]


def test_open_meteo_packet_dedup_reads_republisher_bucket_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE calibration_republisher_ensemble_member_bucket("
        "ensemble_system_id TEXT NOT NULL,station_id TEXT NOT NULL,"
        "target_local_date TEXT NOT NULL,member_set_sha256 TEXT NOT NULL,"
        "bucket_contract_sha256 TEXT NOT NULL,source_role TEXT NOT NULL,"
        "authority_scope TEXT NOT NULL);"
    )
    values = (
        "DWD_ICON_GLOBAL_EPS",
        "RKPK",
        "2026-08-01",
        "a" * 64,
        "b" * 64,
        "aggregator_republisher",
        "PRODUCTION_NO_LIVE_CANDIDATE",
    )
    conn.execute(
        "INSERT INTO calibration_republisher_ensemble_member_bucket "
        "VALUES(?,?,?,?,?,?,?)",
        values,
    )

    assert production_worker_module._open_meteo_packet_bucket_exists(
        conn,
        ensemble_system_id=values[0],
        station_id=values[1],
        event_date=values[2],
        member_set_sha256=values[3],
        bucket_contract_sha256=values[4],
    ) is True
    assert production_worker_module._open_meteo_packet_bucket_exists(
        conn,
        ensemble_system_id=values[0],
        station_id=values[1],
        event_date=values[2],
        member_set_sha256="c" * 64,
        bucket_contract_sha256=values[4],
    ) is False


def test_forward_p0_prefers_official_and_uses_republisher_only_when_absent() -> None:
    def packet(
        schema: str,
        station_id: str,
        event_date: str,
        marker: str,
    ) -> dict[str, str]:
        return {
            "schema": schema,
            "station_id": station_id,
            "event_date": event_date,
            "marker": marker,
        }

    republisher_a = packet(
        production_worker_module.OPEN_METEO_FORWARD_PACKET_SCHEMA,
        "RKPK",
        "2026-08-02",
        "republisher-a",
    )
    official_a = packet(
        production_worker_module.OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
        "RKPK",
        "2026-08-02",
        "official-a",
    )
    republisher_b = packet(
        production_worker_module.OPEN_METEO_FORWARD_PACKET_SCHEMA,
        "ZBAA",
        "2026-08-02",
        "republisher-b",
    )

    selected, official, republisher = (
        production_worker_module._preferred_forward_packets_for_p0(
            [republisher_a, official_a, republisher_b]
        )
    )

    assert official == {("RKPK", "2026-08-02"): official_a}
    assert republisher == {
        ("RKPK", "2026-08-02"): republisher_a,
        ("ZBAA", "2026-08-02"): republisher_b,
    }
    assert selected == {
        ("RKPK", "2026-08-02"): official_a,
        ("ZBAA", "2026-08-02"): republisher_b,
    }


def test_forward_packet_cycle_scope_excludes_retained_other_dates() -> None:
    packet = {"station_id": "RKPK", "event_date": "2026-08-03"}
    assert production_worker_module._forward_packet_in_cycle_scope(
        packet,
        station_allowlist=("RKPK", "KSEA"),
        event_date_allowlist=("2026-08-03",),
    ) is True
    assert production_worker_module._forward_packet_in_cycle_scope(
        {**packet, "event_date": "2026-08-02"},
        station_allowlist=("RKPK", "KSEA"),
        event_date_allowlist=("2026-08-03",),
    ) is False
    assert production_worker_module._forward_packet_in_cycle_scope(
        {**packet, "station_id": "KLAX"},
        station_allowlist=("RKPK", "KSEA"),
        event_date_allowlist=("2026-08-03",),
    ) is False


def test_production_worker_infers_single_open_event_date_from_resident_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "resident-ws.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": (
                    "bayes_basket_target_resident_multi_scope_ws_manifest_v1"
                ),
                "scopes": [
                    {
                        "station_id": "RKPK",
                        "event_date": "2026-08-03",
                        "event_day_state": "open",
                    },
                    {
                        "station_id": "KSEA",
                        "event_date": "2026-08-03",
                        "event_day_state": "open",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PM_TARGET_PRODUCTION_WRITER", "1")
    monkeypatch.setenv("PM_TARGET_EXECUTION_MODE", "production_no_live")
    monkeypatch.delenv("PM_TARGET_EVENT_DATE_ALLOWLIST", raising=False)

    worker = TargetProductionWorker(
        tmp_path / "target.db",
        market_manifest_path=manifest,
        native_oos_snapshot_path=tmp_path / "missing-oos.json",
    )
    try:
        assert worker.event_date_allowlist == ("2026-08-03",)
        assert worker.event_date_scope_source == (
            "resident_ws_manifest_open_scope"
        )
    finally:
        worker.close()


def test_resident_open_event_date_inference_rejects_mixed_dates(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "resident-ws.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": (
                    "bayes_basket_target_resident_multi_scope_ws_manifest_v1"
                ),
                "scopes": [
                    {
                        "event_date": "2026-08-02",
                        "event_day_state": "open",
                    },
                    {
                        "event_date": "2026-08-03",
                        "event_day_state": "open",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    assert production_worker_module._resident_open_event_dates(manifest) == ()


def test_forward_p0_retries_republisher_when_official_compile_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = ("RKPK", "2026-08-02")
    official = {
        "schema": production_worker_module.OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
        "station_id": scope[0],
        "event_date": scope[1],
    }
    republisher = {
        "schema": production_worker_module.OPEN_METEO_FORWARD_PACKET_SCHEMA,
        "station_id": scope[0],
        "event_date": scope[1],
    }
    calls: list[str] = []

    def fake_compile(_conn, *, forward_packet, decision_at_utc):
        del decision_at_utc
        schema = str(forward_packet["schema"])
        calls.append(schema)
        if schema == production_worker_module.OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA:
            raise ContractError("native_packet_incomplete")
        return {
            "schema": "calibrated_native_cross_ensemble_p0_packet_v1",
            "packet": {"station_id": scope[0], "event_date": scope[1]},
        }

    monkeypatch.setattr(
        production_worker_module,
        "compile_native_ensemble_p0",
        fake_compile,
    )
    compiled, failures, source_class = (
        production_worker_module._compile_native_p0_with_republisher_fallback(
            sqlite3.connect(":memory:"),
            scope=scope,
            preferred_packet=official,
            official_packet=official,
            republisher_packet=republisher,
            decision_at_utc="2026-08-02T00:00:00Z",
        )
    )

    assert calls == [
        production_worker_module.OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
        production_worker_module.OPEN_METEO_FORWARD_PACKET_SCHEMA,
    ]
    assert compiled is not None
    assert source_class == "aggregator_republisher"
    assert failures[0]["source_class"] == "official_native"


def test_forward_p0_routes_fresh_official_before_republisher_even_if_preselected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit official index wins over a tentative republisher choice."""

    scope = ("RKPK", "2026-08-02")
    official = {
        "schema": production_worker_module.OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
        "station_id": scope[0],
        "event_date": scope[1],
        "marker": "official-fresh",
    }
    republisher = {
        "schema": production_worker_module.OPEN_METEO_FORWARD_PACKET_SCHEMA,
        "station_id": scope[0],
        "event_date": scope[1],
        "marker": "republisher-fresh",
    }
    calls: list[str] = []

    def fake_compile(_conn, *, forward_packet, decision_at_utc):
        del decision_at_utc
        marker = str(forward_packet["marker"])
        calls.append(marker)
        return {
            "schema": "calibrated_native_cross_ensemble_p0_packet_v1",
            "packet": {
                "station_id": scope[0],
                "event_date": scope[1],
                "marker": marker,
            },
        }

    monkeypatch.setattr(
        production_worker_module,
        "compile_native_ensemble_p0",
        fake_compile,
    )
    compiled, failures, source_class = (
        production_worker_module._compile_native_p0_with_republisher_fallback(
            sqlite3.connect(":memory:"),
            scope=scope,
            # Simulate a stale selection snapshot.  The authoritative map
            # still has a fresh official packet and must win.
            preferred_packet=republisher,
            official_packet=official,
            republisher_packet=republisher,
            decision_at_utc="2026-08-02T00:00:00Z",
        )
    )

    assert calls == ["official-fresh"]
    assert failures == []
    assert source_class == "official_native"
    assert compiled is not None
    assert compiled["packet"]["marker"] == "official-fresh"


def test_forward_p0_routes_stale_official_to_fresh_republisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired native capture must not suppress the eligible OM retry."""

    scope = ("RKPK", "2026-08-02")
    official = {
        "schema": production_worker_module.OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
        "station_id": scope[0],
        "event_date": scope[1],
        "marker": "official-stale",
    }
    republisher = {
        "schema": production_worker_module.OPEN_METEO_FORWARD_PACKET_SCHEMA,
        "station_id": scope[0],
        "event_date": scope[1],
        "marker": "republisher-fresh",
    }
    calls: list[str] = []

    def fake_compile(_conn, *, forward_packet, decision_at_utc):
        del decision_at_utc
        marker = str(forward_packet["marker"])
        calls.append(marker)
        if marker == "official-stale":
            raise ContractError("FAIL_CLOSED_NO_P0:native_ensemble_snapshot_stale")
        return {
            "schema": "calibrated_native_cross_ensemble_p0_packet_v1",
            "packet": {
                "station_id": scope[0],
                "event_date": scope[1],
                "marker": marker,
            },
        }

    monkeypatch.setattr(
        production_worker_module,
        "compile_native_ensemble_p0",
        fake_compile,
    )
    compiled, failures, source_class = (
        production_worker_module._compile_native_p0_with_republisher_fallback(
            sqlite3.connect(":memory:"),
            scope=scope,
            preferred_packet=official,
            official_packet=official,
            republisher_packet=republisher,
            decision_at_utc="2026-08-02T00:00:00Z",
        )
    )

    assert calls == ["official-stale", "republisher-fresh"]
    assert source_class == "aggregator_republisher"
    assert compiled is not None
    assert compiled["packet"]["marker"] == "republisher-fresh"
    assert failures == [{
        "station_id": scope[0],
        "event_date": scope[1],
        "source_class": "official_native",
        "schema": production_worker_module.OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
        "reason": "ContractError:FAIL_CLOSED_NO_P0:native_ensemble_snapshot_stale",
    }]


def test_forward_p0_fails_closed_when_official_and_republisher_are_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither source can regain freshness by being retried or recompiled."""

    scope = ("RKPK", "2026-08-02")
    official = {
        "schema": production_worker_module.OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
        "station_id": scope[0],
        "event_date": scope[1],
        "marker": "official-stale",
    }
    republisher = {
        "schema": production_worker_module.OPEN_METEO_FORWARD_PACKET_SCHEMA,
        "station_id": scope[0],
        "event_date": scope[1],
        "marker": "republisher-stale",
    }
    calls: list[str] = []

    def fake_compile(_conn, *, forward_packet, decision_at_utc):
        del decision_at_utc
        calls.append(str(forward_packet["marker"]))
        raise ContractError("FAIL_CLOSED_NO_P0:native_ensemble_snapshot_stale")

    monkeypatch.setattr(
        production_worker_module,
        "compile_native_ensemble_p0",
        fake_compile,
    )
    compiled, failures, source_class = (
        production_worker_module._compile_native_p0_with_republisher_fallback(
            sqlite3.connect(":memory:"),
            scope=scope,
            preferred_packet=official,
            official_packet=official,
            republisher_packet=republisher,
            decision_at_utc="2026-08-02T00:00:00Z",
        )
    )

    assert calls == ["official-stale", "republisher-stale"]
    assert compiled is None
    assert source_class == ""
    assert [failure["source_class"] for failure in failures] == [
        "official_native",
        "aggregator_republisher",
    ]
    assert all(
        failure["reason"]
        == "ContractError:FAIL_CLOSED_NO_P0:native_ensemble_snapshot_stale"
        for failure in failures
    )


def test_native_p0_inbox_upsert_replays_activated_scope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "p0-inbox.json"
    packet = {
        "station_id": "OEJN",
        "event_date": "2026-07-31",
        "probabilities": {"41": 0.4, "42": 0.6},
    }
    compiled = {
        "schema": "calibrated_native_cross_ensemble_p0_packet_v1",
        "source_class": "calibrated_native_cross_ensemble",
        "packet_content_sha256": content_sha256(packet),
        "packet": packet,
    }

    first = production_worker_module._upsert_native_p0_inbox(
        path,
        compiled_packets=[compiled],
        generated_at_utc="2026-07-30T21:10:00Z",
    )
    second = production_worker_module._upsert_native_p0_inbox(
        path,
        compiled_packets=[compiled],
        generated_at_utc="2026-07-30T21:10:00Z",
    )
    inbox = load_bounded_json(path)

    assert first["accepted_count"] == 1
    assert first["packet_count"] == 1
    assert second["wrote"] is False
    assert inbox["packets"] == [{
        "source_class": "calibrated_native_cross_ensemble",
        "packet_content_sha256": content_sha256(packet),
        "packet": packet,
    }]


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry: dict[str, object],
) -> None:
    def export(**kwargs):
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_path"].write_text("{}", encoding="utf-8")
        return {"state": "EXPORTED", "snapshot_sha256": "a" * 64}

    monkeypatch.setattr(
        production_worker_module,
        "export_shared_live_registry",
        export,
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_market_registry_snapshot",
        lambda _path: registry,
    )
    monkeypatch.setattr(
        production_worker_module,
        "registry_capture_scopes",
        lambda *_args, **_kwargs: [
            {
                "event_date": "2026-07-31",
                "settlement_timezone": "Asia/Seoul",
                "station_ids": [row["station_id"]],
            }
            for row in registry["rows"]
        ],
    )
    monkeypatch.setattr(
        production_worker_module,
        "station_coordinates",
        lambda _station: {"latitude": 1.0, "longitude": 2.0},
    )
    monkeypatch.setattr(
        production_worker_module,
        "capture_open_meteo_ensemble_scope",
        lambda **kwargs: _packet(str(kwargs["station_id"])),
    )
    monkeypatch.setattr(
        production_worker_module,
        "compile_native_ensemble_p0",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ContractError("strict_oos_activation_missing")
        ),
    )


def test_native_ensemble_scope_rotation_wraps_and_never_emits_legacy_p0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path)
    registry = _registry(("KAAA", "KBBB", "KCCC"))
    _install_fakes(monkeypatch, registry=registry)

    results = [
        worker.refresh_resident_nwp(
            as_of_utc=f"2026-07-30T0{index}:00:00Z"
        )
        for index in range(4)
    ]

    assert [row["station_id"] for row in results] == [
        "KAAA",
        "KBBB",
        "KCCC",
        "KAAA",
    ]
    assert all(
        row["schema"]
        == "bayes_basket_target_resident_native_ensemble_cycle_v1"
        and row["state"] == "candidate_captured_no_p0"
        and row["calls_order_gateway"] is False
        and row["orders_submitted"] == 0
        for row in results
    )
    inbox = load_bounded_json(
        worker.nwp_runtime_root
        / "native_ensemble_forward_inbox.json"
    )
    assert {
        row["station_id"] for row in inbox["packets"]
    } == {"KAAA", "KBBB", "KCCC"}
    p0 = load_bounded_json(worker.p0_inbox_path)
    assert p0["packets"] == []


def test_native_ensemble_duplicate_snapshot_is_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path)
    worker.station_allowlist = ("KAAA",)
    registry = _registry(("KAAA",))
    _install_fakes(monkeypatch, registry=registry)

    first = worker.refresh_resident_nwp(
        as_of_utc="2026-07-30T00:00:00Z"
    )
    second = worker.refresh_resident_nwp(
        as_of_utc="2026-07-30T01:00:00Z"
    )

    assert first["inbox_wrote"] is True
    assert second["inbox_wrote"] is False
    assert second["snapshot_content_sha256"] == first[
        "snapshot_content_sha256"
    ]


def test_native_ensemble_exact_allowlist_without_scope_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path)
    worker.station_allowlist = ("KZZZ",)
    registry = _registry(("KAAA",))
    _install_fakes(monkeypatch, registry=registry)

    result = worker.refresh_resident_nwp(
        as_of_utc="2026-07-30T00:00:00Z"
    )

    assert result["state"] == "blocked_fail_closed"
    assert result["reason"] == "resident_nwp_exact_station_scope_missing"
    assert result["orders_submitted"] == 0


def test_refresh_resident_nwp_open_meteo_cannot_replace_official_p0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(tmp_path)
    worker.station_allowlist = ("KAAA",)
    registry = _registry(("KAAA",))
    _install_fakes(monkeypatch, registry=registry)

    def compiled(provider: str, *, fallback_eligible: bool) -> dict[str, object]:
        packet = {
            "station_id": "KAAA",
            "event_date": "2026-07-31",
            "native_probability_envelope": {
                "lineage": {
                    "native_transport_provider": provider,
                    "aggregator_fallback_p0_eligible": (
                        "true" if fallback_eligible else "false"
                    ),
                    "republisher_fallback_p0_eligible": (
                        "true" if fallback_eligible else "false"
                    ),
                    "transport_lineage_class": (
                        "aggregator_republisher"
                        if provider == "OPEN_METEO"
                        else "official_native"
                    ),
                    "native_arrival_index_eligible": (
                        "false" if provider == "OPEN_METEO" else "true"
                    ),
                    "native_formal_p0_eligible": "false",
                },
            },
        }
        return {
            "schema": "calibrated_native_cross_ensemble_p0_packet_v1",
            "source_class": "calibrated_native_cross_ensemble",
            "packet": packet,
            "packet_content_sha256": content_sha256(packet),
        }

    official = compiled(
        "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT",
        fallback_eligible=False,
    )
    production_worker_module._upsert_native_p0_inbox(
        worker.p0_inbox_path,
        compiled_packets=[official],
        generated_at_utc="2026-07-30T00:00:00Z",
    )
    monkeypatch.setattr(
        production_worker_module,
        "compile_native_ensemble_p0",
        lambda *_args, **_kwargs: compiled(
            "OPEN_METEO",
            fallback_eligible=True,
        ),
    )

    result = worker.refresh_resident_nwp(
        as_of_utc="2026-07-30T01:00:00Z",
    )
    inbox = load_bounded_json(worker.p0_inbox_path)
    lineage = inbox["packets"][0]["packet"][
        "native_probability_envelope"
    ]["lineage"]

    assert result["p0_inbox_packet_count"] == 1
    assert lineage["native_transport_provider"] == (
        "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT"
    )
