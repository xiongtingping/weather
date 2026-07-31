from __future__ import annotations

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
        "qc_status TEXT NOT NULL,content_sha256 TEXT NOT NULL);"
    )
    conn.execute(
        "INSERT INTO calibration_native_ensemble_member_run VALUES(?,?,?)",
        ("TEST", "2026-07-30", "PRODUCTION_NO_LIVE_CANDIDATE"),
    )
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?)",
        ("TEST", "2026-07-30", "ACCEPTED", "f" * 64),
    )
    before = production_worker_module._native_ensemble_truth_signatures(
        conn
    )
    conn.execute(
        "INSERT INTO calibration_native_ensemble_member_run VALUES(?,?,?)",
        ("TEST", "2026-07-31", "PRODUCTION_NO_LIVE_CANDIDATE"),
    )
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?)",
        ("TEST", "2026-07-31", "ACCEPTED", "0" * 64),
    )

    after = production_worker_module._native_ensemble_truth_signatures(
        conn
    )

    assert before["TEST"] != after["TEST"]


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
