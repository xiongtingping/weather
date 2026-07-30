from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import bayes_basket_target.production_worker as production_worker_module
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.production_worker import (
    MAX_RESIDENT_NWP_SCOPES_PER_CYCLE,
    TargetProductionWorker,
)


def _write_scope_inbox(
    *,
    inbox_path: Path,
    station_id: str,
    event_date: str,
    decision_at_utc: str = "2026-07-30T00:00:00Z",
    fresh_until_utc: str = "2026-07-30T12:00:00Z",
) -> None:
    packet = {
        "stage": "P0",
        "station_id": station_id,
        "event_date": event_date,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until_utc,
    }
    identity = {
        "schema": "bayes_basket_target_p0_inbox_v1",
        "generated_at_utc": decision_at_utc,
        "packets": [{
            "source_class": "official_forward_model_bundle",
            "packet": packet,
            "packet_content_sha256": content_sha256(packet),
        }],
    }
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_text(
        json.dumps({
            **identity,
            "content_sha256": content_sha256(identity),
        }),
        encoding="utf-8",
    )


def test_resident_nwp_multistation_is_bounded_isolated_and_wraps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,stage TEXT,state TEXT,"
        "decision_at_utc TEXT,fresh_until_utc TEXT)"
    )
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = None
    worker.canonical_source_db_path = tmp_path / "canonical.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "shared-p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ()
    worker.event_date_allowlist = ()
    worker._resident_nwp_scope_cursor_by_priority = {}
    worker._nwp_run_fallback_cursor_by_scope = {}

    stations = ["KAAA", "KBBB", "KCCC", "KDDD", "KEEE", "KFFF"]
    scopes = [
        {
            "event_date": "2026-07-30",
            "settlement_timezone": "America/New_York",
            "station_ids": [station],
        }
        for station in stations
    ]
    registry_path = (
        worker.nwp_runtime_root / "market_registry_forward.json"
    )

    def export(**kwargs):
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_path"].write_text("{}", encoding="utf-8")
        return {
            "state": "EXPORTED",
            "snapshot_sha256": "a" * 64,
        }

    monkeypatch.setattr(
        production_worker_module,
        "export_shared_live_registry",
        export,
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_market_registry_snapshot",
        lambda _path: {
            "schema": "bayes_basket_target_market_registry_snapshot_v1",
            "snapshot_sha256": "a" * 64,
            "rows": [],
        },
    )
    monkeypatch.setattr(
        production_worker_module,
        "registry_capture_scopes",
        lambda *args, **kwargs: scopes,
    )
    monkeypatch.setattr(
        production_worker_module,
        "candidate_run_initializations",
        lambda **kwargs: ["2026-07-29T12:00:00Z"],
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_production_artifacts",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        production_worker_module,
        "compatible_run_candidates",
        lambda **kwargs: [{
            "run_initialization_utc": "2026-07-29T12:00:00Z",
            "activation_binding": {
                "selected_models": ["ECMWF", "GFS"],
            },
            "ensemble_activation": {"activation_id": "formal-oos"},
            "ensemble_artifact": {"ensemble_artifact_id": "ensemble"},
        }],
    )

    capture_attempts: list[str] = []
    fail_kbbb_once = True

    def capture(**kwargs):
        nonlocal fail_kbbb_once
        station = kwargs["scope"]["station_ids"][0]
        capture_attempts.append(station)
        if station == "KBBB" and fail_kbbb_once:
            fail_kbbb_once = False
            raise RuntimeError("provider_unavailable")
        path = tmp_path / f"{station}-capture.json"
        path.write_text("{}", encoding="utf-8")
        return {"state": "CAPTURED", "capture_paths": [str(path)]}

    monkeypatch.setattr(
        production_worker_module,
        "capture_official_nwp_scope",
        capture,
    )

    def refresh_p0(**kwargs):
        station = kwargs["output_root"].name
        _write_scope_inbox(
            inbox_path=kwargs["inbox_path"],
            station_id=station,
            event_date=kwargs["event_date"],
        )
        return {
            "state": "updated",
            "packet_count": 1,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        }

    monkeypatch.setattr(
        production_worker_module,
        "refresh_resident_p0",
        refresh_p0,
    )

    first = worker.refresh_resident_nwp(
        as_of_utc="2026-07-30T01:00:00Z",
    )

    assert MAX_RESIDENT_NWP_SCOPES_PER_CYCLE == 4
    assert first["selected_scope_count"] == 4
    assert first["successful_scope_count"] == 3
    assert capture_attempts == ["KAAA", "KBBB", "KCCC", "KDDD"]
    assert first["scope_failures"] == [{
        "station_id": "KBBB",
        "event_date": "2026-07-30",
        "reason": (
            "resident_nwp_scope_failed:"
            "RuntimeError:provider_unavailable"
        ),
    }]
    first_inbox = json.loads(
        worker.p0_inbox_path.read_text(encoding="utf-8")
    )
    assert {
        row["packet"]["station_id"]
        for row in first_inbox["packets"]
    } == {"KAAA", "KCCC", "KDDD"}

    second = worker.refresh_resident_nwp(
        as_of_utc="2026-07-30T01:01:00Z",
    )

    assert second["selected_scope_count"] == 4
    assert second["successful_scope_count"] == 4
    assert capture_attempts[4:] == ["KEEE", "KFFF", "KAAA", "KBBB"]
    second_inbox = json.loads(
        worker.p0_inbox_path.read_text(encoding="utf-8")
    )
    assert {
        row["packet"]["station_id"]
        for row in second_inbox["packets"]
    } == set(stations)
    assert second["merged_p0_packet_count"] == 6
    assert second["calls_order_gateway"] is False
    assert second["places_orders"] is False
    assert second["live_submit_authorized"] is False
    assert second["orders_submitted"] == 0


def test_resident_nwp_reserves_slots_across_missing_stale_and_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,stage TEXT,state TEXT,"
        "decision_at_utc TEXT,fresh_until_utc TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?)",
        (
            (
                "KEEE", "2026-07-30", "P0", "COMMITTED",
                "2026-07-29T00:00:00Z", "2026-07-30T00:30:00Z",
            ),
            (
                "KFFF", "2026-07-30", "P0", "COMMITTED",
                "2026-07-30T00:30:00Z", "2026-07-30T12:00:00Z",
            ),
        ),
    )
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = None
    worker.canonical_source_db_path = tmp_path / "canonical.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "shared-p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ()
    worker.event_date_allowlist = ()

    stations = ("KAAA", "KBBB", "KCCC", "KDDD", "KEEE", "KFFF")
    scopes = [{
        "event_date": "2026-07-30",
        "settlement_timezone": "America/New_York",
        "station_ids": [station],
    } for station in stations]

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
        lambda _path: {"snapshot_sha256": "a" * 64, "rows": []},
    )
    monkeypatch.setattr(
        production_worker_module,
        "registry_capture_scopes",
        lambda *args, **kwargs: scopes,
    )
    monkeypatch.setattr(
        production_worker_module,
        "candidate_run_initializations",
        lambda **kwargs: ["2026-07-29T12:00:00Z"],
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_production_artifacts",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        production_worker_module,
        "compatible_run_candidates",
        lambda **kwargs: [{
            "run_initialization_utc": "2026-07-29T12:00:00Z",
            "activation_binding": {
                "selected_models": ["ECMWF", "GFS"],
            },
            "ensemble_activation": {"activation_id": "formal-oos"},
            "ensemble_artifact": {"ensemble_artifact_id": "ensemble"},
        }],
    )
    attempted = []

    def capture(**kwargs):
        attempted.append(kwargs["scope"]["station_ids"][0])
        raise RuntimeError("stop_after_selection")

    monkeypatch.setattr(
        production_worker_module,
        "capture_official_nwp_scope",
        capture,
    )

    result = worker.refresh_resident_nwp(
        as_of_utc="2026-07-30T01:00:00Z",
    )

    assert result["selected_scope_count"] == 4
    assert attempted == ["KAAA", "KEEE", "KFFF", "KBBB"]
    assert result["successful_scope_count"] == 0


def test_resident_nwp_rejects_single_model_without_blocking_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,stage TEXT,state TEXT,"
        "decision_at_utc TEXT,fresh_until_utc TEXT)"
    )
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = None
    worker.canonical_source_db_path = tmp_path / "canonical.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "shared-p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ()
    worker.event_date_allowlist = ()

    scopes = [
        {
            "event_date": "2026-07-30",
            "settlement_timezone": "America/New_York",
            "station_ids": [station],
        }
        for station in ("KAAA", "KBBB")
    ]

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
        lambda _path: {"snapshot_sha256": "a" * 64, "rows": []},
    )
    monkeypatch.setattr(
        production_worker_module,
        "registry_capture_scopes",
        lambda *args, **kwargs: scopes,
    )
    monkeypatch.setattr(
        production_worker_module,
        "candidate_run_initializations",
        lambda **kwargs: ["2026-07-29T12:00:00Z"],
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_production_artifacts",
        lambda *args, **kwargs: object(),
    )

    def compatible(**kwargs):
        models = (
            ["ECMWF"]
            if kwargs["station"] == "KAAA"
            else ["ECMWF", "GFS"]
        )
        return [{
            "run_initialization_utc": "2026-07-29T12:00:00Z",
            "activation_binding": {"selected_models": models},
            "ensemble_activation": {"activation_id": "formal-oos"},
            "ensemble_artifact": {"ensemble_artifact_id": "ensemble"},
        }]

    monkeypatch.setattr(
        production_worker_module,
        "compatible_run_candidates",
        compatible,
    )
    capture_path = tmp_path / "capture.json"
    capture_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        production_worker_module,
        "capture_official_nwp_scope",
        lambda **kwargs: {
            "state": "CAPTURED",
            "capture_paths": [str(capture_path)],
        },
    )

    def refresh_p0(**kwargs):
        _write_scope_inbox(
            inbox_path=kwargs["inbox_path"],
            station_id=kwargs["output_root"].name,
            event_date=kwargs["event_date"],
        )
        return {"state": "updated", "packet_count": 1}

    monkeypatch.setattr(
        production_worker_module,
        "refresh_resident_p0",
        refresh_p0,
    )
    candidate_prefetches = []

    def prefetch_candidate(**kwargs):
        candidate_prefetches.append(kwargs)
        return {
            "state": "CAPTURED_CANDIDATE",
            "candidate_only": True,
            "p0_materialization_authorized": False,
            "station_id": kwargs["scope"]["station_ids"][0],
            "event_date": kwargs["scope"]["event_date"],
        }

    monkeypatch.setattr(
        production_worker_module,
        "prefetch_candidate_raw_training",
        prefetch_candidate,
    )

    result = worker.refresh_resident_nwp(
        as_of_utc="2026-07-30T01:00:00Z",
    )

    assert result["successful_scope_count"] == 1
    assert result["scopes"][0]["station_ids"] == ["KBBB"]
    assert result["scope_failures"][0]["station_id"] == "KAAA"
    assert "resident_nwp_no_activated_two_model_run_scope" in (
        result["scope_failures"][0]["reason"]
    )
    assert "forward" not in result["scope_failures"][0]["reason"].lower()
    assert len(candidate_prefetches) == 1
    assert candidate_prefetches[0]["scope"]["station_ids"] == ["KAAA"]
    assert tuple(candidate_prefetches[0]["candidate_models"]) == (
        "ECMWF",
        "GFS",
        "ICON",
        "GEM",
        "CMA",
    )
    assert result["candidate_prefetch_attempt_count"] == 1
    assert result["candidate_prefetch_success_count"] == 1
    assert result["candidate_prefetch_scope_limit"] == 1
    assert result["candidate_prefetch_results"][0][
        "p0_materialization_authorized"
    ] is False


def test_resident_nwp_candidate_prefetch_is_bounded_and_never_builds_p0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,stage TEXT,state TEXT,"
        "decision_at_utc TEXT,fresh_until_utc TEXT)"
    )
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = None
    worker.canonical_source_db_path = tmp_path / "canonical.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "shared-p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ()
    worker.event_date_allowlist = ()

    scopes = [{
        "event_date": "2026-07-30",
        "settlement_timezone": "America/New_York",
        "station_ids": [station],
    } for station in ("KAAA", "KBBB", "KCCC")]

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
        lambda _path: {"snapshot_sha256": "a" * 64, "rows": []},
    )
    monkeypatch.setattr(
        production_worker_module,
        "registry_capture_scopes",
        lambda *args, **kwargs: scopes,
    )
    monkeypatch.setattr(
        production_worker_module,
        "candidate_run_initializations",
        lambda **kwargs: [
            "2026-07-30T00:00:00Z",
            "2026-07-29T18:00:00Z",
        ],
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_production_artifacts",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            production_worker_module.ContractError(
                "production_calibration_artifacts_missing"
            )
        ),
    )
    formal_capture_calls = []
    p0_calls = []
    monkeypatch.setattr(
        production_worker_module,
        "capture_official_nwp_scope",
        lambda **kwargs: formal_capture_calls.append(kwargs),
    )
    monkeypatch.setattr(
        production_worker_module,
        "refresh_resident_p0",
        lambda **kwargs: p0_calls.append(kwargs),
    )
    candidate_calls = []

    def candidate_prefetch(**kwargs):
        candidate_calls.append(kwargs)
        return {
            "state": "CAPTURED_CANDIDATE",
            "candidate_only": True,
            "p0_materialization_authorized": False,
            "station_id": kwargs["scope"]["station_ids"][0],
            "event_date": kwargs["scope"]["event_date"],
            "creates_p0": False,
            "writes_target_database": False,
        }

    monkeypatch.setattr(
        production_worker_module,
        "prefetch_candidate_raw_training",
        candidate_prefetch,
    )

    result = worker.refresh_resident_nwp(
        as_of_utc="2026-07-30T01:00:00Z",
    )

    assert result["state"] == "candidate_capture_only"
    assert result["reason"] == "formal_p0_scope_not_eligible"
    assert result["selected_scope_count"] == 3
    assert result["successful_scope_count"] == 0
    assert result["candidate_prefetch_attempt_count"] == 1
    assert result["candidate_prefetch_success_count"] == 1
    assert len(candidate_calls) == 1
    assert candidate_calls[0]["scope"]["station_ids"] == ["KAAA"]
    assert candidate_calls[0]["run_initialization_utc"] == (
        "2026-07-29T18:00:00Z"
    )
    assert formal_capture_calls == []
    assert p0_calls == []
    assert not worker.p0_inbox_path.exists()
    assert result["calls_order_gateway"] is False
    assert result["places_orders"] is False
    assert result["live_submit_authorized"] is False
    assert result["orders_submitted"] == 0


def test_real_multiday_discovery_round_robin_reaches_beyond_first_45(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,stage TEXT,state TEXT,"
        "decision_at_utc TEXT,fresh_until_utc TEXT)"
    )
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = None
    worker.canonical_source_db_path = tmp_path / "canonical.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "shared-p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ()
    worker.event_date_allowlist = ()

    stations = [f"S{index:03d}" for index in range(45)]
    rows = []
    for day in (
        "2026-07-29",  # closed
        "2026-07-30",  # open
        "2026-07-31",  # not started and must remain discoverable
        "2026-08-03",  # beyond the 72-hour discovery horizon
    ):
        for station in stations:
            rows.append({
                "station_id": station,
                "event_date": day,
                "settlement_timezone": "America/New_York",
                "outcome": "mid",
                "source_eligible": 1,
                "market_active": 1,
                "market_accepting_orders": 1,
            })
    registry = {
        "schema": "bayes_basket_target_market_registry_snapshot_v1",
        "snapshot_sha256": "a" * 64,
        "row_count": len(rows),
        "source_database_path": str(worker.canonical_source_db_path),
        "rows": rows,
    }

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
    # Deliberately do not mock registry_capture_scopes: this is the real
    # discovery boundary that previously truncated the catalog to four.
    monkeypatch.setattr(
        production_worker_module,
        "candidate_run_initializations",
        lambda **kwargs: ["2026-07-29T12:00:00Z"],
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_production_artifacts",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        production_worker_module,
        "compatible_run_candidates",
        lambda **kwargs: [{
            "run_initialization_utc": "2026-07-29T12:00:00Z",
            "activation_binding": {
                "selected_models": ["ECMWF", "GFS"],
            },
            "ensemble_activation": {"activation_id": "formal-oos"},
            "ensemble_artifact": {"ensemble_artifact_id": "ensemble"},
        }],
    )
    captured: list[tuple[str, str]] = []

    def capture(**kwargs):
        station = kwargs["scope"]["station_ids"][0]
        day = kwargs["scope"]["event_date"]
        captured.append((station, day))
        path = tmp_path / f"{station}-{day}-capture.json"
        path.write_text("{}", encoding="utf-8")
        return {"state": "CAPTURED", "capture_paths": [str(path)]}

    monkeypatch.setattr(
        production_worker_module,
        "capture_official_nwp_scope",
        capture,
    )

    def refresh_p0(**kwargs):
        _write_scope_inbox(
            inbox_path=kwargs["inbox_path"],
            station_id=kwargs["output_root"].name,
            event_date=kwargs["event_date"],
            decision_at_utc="2026-07-30T15:00:00Z",
            fresh_until_utc="2026-07-30T22:00:00Z",
        )
        return {"state": "updated", "packet_count": 1}

    monkeypatch.setattr(
        production_worker_module,
        "refresh_resident_p0",
        refresh_p0,
    )

    cycles = [
        worker.refresh_resident_nwp(
            as_of_utc="2026-07-30T16:00:00Z",
        )
        for _ in range(12)
    ]

    assert all(item["scope_count"] == 90 for item in cycles)
    assert all(item["selected_scope_count"] == 4 for item in cycles)
    assert captured[:4] != captured[4:8]
    assert len(set(captured)) == 48
    assert {
        station
        for station, day in captured
        if day == "2026-07-30"
    } == set(stations)
    assert any(day == "2026-07-31" for _station, day in captured)
    assert all(
        day not in {"2026-07-29", "2026-08-03"}
        for _station, day in captured
    )


def test_scope_inbox_is_removed_after_p0_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,stage TEXT,state TEXT,"
        "decision_at_utc TEXT,fresh_until_utc TEXT)"
    )
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = None
    worker.canonical_source_db_path = tmp_path / "canonical.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "shared-p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ()
    worker.event_date_allowlist = ()
    scope = {
        "event_date": "2026-07-30",
        "settlement_timezone": "America/New_York",
        "station_ids": ["KAAA"],
    }

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
        lambda _path: {"snapshot_sha256": "a" * 64, "rows": []},
    )
    monkeypatch.setattr(
        production_worker_module,
        "registry_capture_scopes",
        lambda *args, **kwargs: [scope],
    )
    monkeypatch.setattr(
        production_worker_module,
        "candidate_run_initializations",
        lambda **kwargs: ["2026-07-29T12:00:00Z"],
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_production_artifacts",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        production_worker_module,
        "compatible_run_candidates",
        lambda **kwargs: [{
            "run_initialization_utc": "2026-07-29T12:00:00Z",
            "activation_binding": {
                "selected_models": ["ECMWF", "GFS"],
            },
            "ensemble_activation": {"activation_id": "formal-oos"},
            "ensemble_artifact": {"ensemble_artifact_id": "ensemble"},
        }],
    )
    capture_path = tmp_path / "capture.json"
    capture_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        production_worker_module,
        "capture_official_nwp_scope",
        lambda **kwargs: {
            "state": "CAPTURED",
            "capture_paths": [str(capture_path)],
        },
    )

    def fail_after_candidate_write(**kwargs):
        _write_scope_inbox(
            inbox_path=kwargs["inbox_path"],
            station_id="KAAA",
            event_date=kwargs["event_date"],
        )
        raise RuntimeError("compile_failed_after_candidate")

    monkeypatch.setattr(
        production_worker_module,
        "refresh_resident_p0",
        fail_after_candidate_write,
    )

    result = worker.refresh_resident_nwp(
        as_of_utc="2026-07-30T01:00:00Z",
    )

    assert result["state"] == "blocked_fail_closed"
    assert result["successful_scope_count"] == 0
    assert not worker.p0_inbox_path.exists()
    assert not list(
        worker.nwp_runtime_root.glob(
            ".resident-nwp-scope-inbox-*"
        )
    )
    assert not list(
        worker.nwp_runtime_root.glob("**/p0-inbox.json")
    )
