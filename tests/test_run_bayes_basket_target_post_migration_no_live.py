from __future__ import annotations

import sqlite3
import subprocess

import pytest

from scripts import run_bayes_basket_target_post_migration_no_live as runner


def _scope() -> tuple[str, ...]:
    return ("EGLC", "RKPK")


def _snapshot(outside: int = 0) -> dict:
    return {
        "missing_tables": [],
        "station_scope": {
            "mode": "allowlist",
            "station_ids": list(_scope()),
        },
        "outside_scope_counts": {
            table: outside
            for table in runner.PROBABILITY_ACCEPTANCE_TABLES
        },
    }


def _round(
    *,
    rowid: int,
    current_core: list[str],
    current_direction: str | None,
    core: list[str],
    direction: str,
    candidate_activations: int = 0,
) -> dict:
    station_results = {}
    for station in _scope():
        station_results[station] = {
            "state": "PASS",
            "started": True,
            "core_chain": {
                "latest": {
                    "rowid": rowid,
                    "current_core_buckets": current_core,
                    "core_buckets": core,
                    "current_direction": current_direction,
                    "direction": direction,
                }
            },
        }
    return {
        "state": "PASS",
        "station_scope": {
            "mode": "allowlist",
            "station_ids": list(_scope()),
        },
        "started_station_ids": list(_scope()),
        "not_started_station_ids": [],
        "round_sha256": "a" * 64,
        "services": {
            "com.weather.weather_basket_yes": 101,
            "com.weather.market_ws": 102,
            "com.weather.clob_order_service": 103,
        },
        "resources": {
            "database_bytes": 1024,
            "wal_bytes": 0,
            "free_bytes": 4 * 1024 * 1024 * 1024,
            "resident_service_rss_bytes": {
                "com.weather.weather_basket_yes": 1024,
                "com.weather.market_ws": 1024,
                "com.weather.clob_order_service": 1024,
            },
            "acceptance_process_rss_bytes": 1024,
        },
        "probability_evidence_runtime": {
            "candidate_activation_count_all": candidate_activations,
            "fallback_contract_failures": [],
            "fallback_proof_counts": {
                "P0_HISTORICAL_TILT": 2,
                "METAR_TRAJECTORY_UPDATE": 2,
            },
            "missing_bridge_tables": [],
        },
        "station_results": station_results,
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def test_default_scope_is_frozen_canonical_45() -> None:
    scope = runner.normalize_station_scope()
    assert scope == runner.CANONICAL_STATION_IDS
    assert len(scope) == 45


def test_preflight_contract_exposes_release_and_m9_06_stage_receipt() -> None:
    assert runner.FORWARD_OOS_BOUNDARY["blocks_production_no_live"] is False
    assert "legacy_fallback_used" in runner.FALSE_BOUNDARY_KEYS
    stage = runner._atomic_stage(
        "preflight",
        {
            "schema": "fixture",
            "state": "PREFLIGHT_READY",
            "production_no_live": True,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
            "legacy_fallback_used": False,
        },
    )
    assert stage["schema"] == "bayes_basket_target_m9_06_stage_receipt_v1"
    assert len(stage["content_sha256"]) == 64


def test_explicit_canonical_allowlist_is_exact_and_bounded() -> None:
    packet = {
        "station_ids": ["EGLC", "RKPK"],
        "calibration_artifacts": [
            {"station_id": "EGLC"},
            {"station_id": "RKPK"},
        ],
        "global_priors": [{"global_prior_id": "global"}],
    }
    manifest = {
        "station_scope": {
            "mode": "allowlist",
            "station_ids": ["RKPK", "EGLC"],
        }
    }
    assert runner._validate_exact_station_scope(
        station_ids=_scope(),
        packet=packet,
        manifest=manifest,
    ) == []


def test_nested_resident_ws_manifest_resolves_all_started_scopes() -> None:
    manifest = {
        "scope_manifests": {
            "EGLC|2026-07-30": {
                "station_scope": {
                    "mode": "allowlist",
                    "station_ids": ["EGLC"],
                },
                "event_date": "2026-07-30",
            },
            "RKPK|2026-07-31": {
                "station_scope": {
                    "mode": "allowlist",
                    "station_ids": ["RKPK"],
                },
                "target_local_date": "2026-07-31",
            },
        }
    }
    stations, event_dates = runner._manifest_scope(manifest)
    assert stations == ["EGLC", "RKPK"]
    assert event_dates == ["2026-07-30", "2026-07-31"]


def test_exact_scope_rejects_outside_rows_and_unknown_station() -> None:
    blockers = runner._validate_exact_station_scope(
        station_ids=_scope(),
        packet={
            "station_ids": ["EGLC", "RKPK"],
            "calibration_artifacts": [{"station_id": "ZUUU"}],
        },
        manifest={
            "station_scope": {
                "mode": "allowlist",
                "station_ids": ["ZUUU"],
            }
        },
    )
    assert "artifact_packet_outside_station_scope:calibration_artifacts" in blockers
    assert "market_manifest_outside_station_scope" in blockers
    with pytest.raises(RuntimeError, match="not_canonical"):
        runner.normalize_station_scope(("XXXX",))


def test_recursive_no_live_boundary_rejects_nested_escape() -> None:
    assert runner._validate_boundary_tree(
        {
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "nested": [
                {
                    "live_submit_authorized": True,
                    "orders_submitted": 1,
                }
            ],
        }
    ) == [
        "$.nested[0].live_submit_authorized_must_be_false",
        "$.nested[0].orders_submitted_must_be_zero",
    ]


def test_child_file_limit_covers_bounded_target_db_and_wal(
    monkeypatch,
) -> None:
    observed = []
    monkeypatch.setattr(
        runner.resource,
        "setrlimit",
        lambda kind, limits: observed.append((kind, limits)),
    )
    runner._child_file_limit()
    expected = runner.MAX_TARGET_DB_BYTES + runner.MAX_TARGET_WAL_BYTES
    assert observed == [
        (runner.resource.RLIMIT_FSIZE, (expected, expected))
    ]


def test_worker_timeout_is_materialized_as_fail_closed_return_code(tmp_path, monkeypatch) -> None:
    stdout = tmp_path / "worker.stdout"
    stderr = tmp_path / "worker.stderr"

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("worker", 3)

    monkeypatch.setattr(runner.subprocess, "run", timed_out)
    result = runner._default_worker_runner(
        ("worker",),
        env={},
        stdout_path=stdout,
        stderr_path=stderr,
        timeout_seconds=3,
    )
    assert result == runner.WORKER_TIMEOUT_RETURN_CODE
    assert "worker_timeout_after_seconds=3" in stderr.read_text(encoding="utf-8")


def test_runner_counts_training_facts_labels_and_version_vectors() -> None:
    with sqlite3.connect(":memory:") as conn:
        for table in (
            "basket_policy_training_fact",
            "basket_policy_training_label",
            "basket_target_execution_version_vectors",
        ):
            conn.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY)")
            conn.execute(f"INSERT INTO {table} DEFAULT VALUES")
        counts = runner._table_counts(conn)
    assert counts["basket_policy_training_fact"] == 1
    assert counts["basket_policy_training_label"] == 1
    assert counts["basket_target_execution_version_vectors"] == 1


def test_probability_scope_snapshot_allows_all_in_scope_stations_to_advance(
    tmp_path,
) -> None:
    db = tmp_path / "target.db"
    tables = runner.PROBABILITY_ACCEPTANCE_TABLES
    with sqlite3.connect(db) as conn:
        for table in tables:
            if table == "basket_probability_evidence_artifact":
                conn.execute(
                    f"CREATE TABLE {table}("
                    "artifact_id TEXT PRIMARY KEY,"
                    "station_id TEXT NOT NULL)"
                )
                conn.executemany(
                    f"INSERT INTO {table}(artifact_id,station_id) "
                    "VALUES(?,?)",
                    [
                        ("EGLC-artifact", "EGLC"),
                        ("RKPK-artifact", "RKPK"),
                        ("ZUUU-artifact", "ZUUU"),
                    ],
                )
            elif table == "basket_probability_evidence_promotion":
                conn.execute(
                    f"CREATE TABLE {table}("
                    "promotion_id TEXT PRIMARY KEY,"
                    "artifact_id TEXT NOT NULL)"
                )
                conn.executemany(
                    f"INSERT INTO {table}(promotion_id,artifact_id) "
                    "VALUES(?,?)",
                    [
                        ("EGLC-promotion", "EGLC-artifact"),
                        ("RKPK-promotion", "RKPK-artifact"),
                        ("ZUUU-promotion", "ZUUU-artifact"),
                    ],
                )
            elif table == "basket_probability_bridge_bucket_probability":
                conn.execute(
                    f"CREATE TABLE {table}(history_id TEXT NOT NULL)"
                )
                conn.executemany(
                    f"INSERT INTO {table}(history_id) VALUES(?)",
                    [
                        ("EGLC-history",),
                        ("RKPK-history",),
                        ("ZUUU-history",),
                    ],
                )
            elif table == "basket_probability_bridge_history":
                conn.execute(
                    f"CREATE TABLE {table}"
                    "(history_id TEXT PRIMARY KEY,station_id TEXT NOT NULL)"
                )
                conn.executemany(
                    f"INSERT INTO {table}(history_id,station_id) VALUES(?,?)",
                    [
                        ("EGLC-history", "EGLC"),
                        ("RKPK-history", "RKPK"),
                        ("ZUUU-history", "ZUUU"),
                    ],
                )
            else:
                conn.execute(
                    f"CREATE TABLE {table}(station_id TEXT NOT NULL)"
                )
                conn.executemany(
                    f"INSERT INTO {table}(station_id) VALUES(?)",
                    [("EGLC",), ("RKPK",), ("ZUUU",)],
                )
        conn.commit()
    snapshot = runner.probability_scope_snapshot(
        db,
        station_ids=_scope(),
    )
    assert snapshot["missing_tables"] == []
    assert all(value == 2 for value in snapshot["scoped_counts"].values())
    assert all(
        value == 1
        for value in snapshot["outside_scope_counts"].values()
    )


def test_two_round_verifier_accepts_each_started_station_advancing() -> None:
    first = _round(
        rowid=10,
        current_core=[],
        current_direction=None,
        core=["38C", "39C"],
        direction="UP",
    )
    second = _round(
        rowid=11,
        current_core=["38C", "39C"],
        current_direction="UP",
        core=["37C", "38C"],
        direction="DOWN",
    )
    result = runner.verify_two_round_resident_receipts(
        first,
        second,
        before_snapshot=_snapshot(),
        after_snapshot=_snapshot(),
        station_ids=_scope(),
    )
    assert result["state"] == "PASS"
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0
    assert result["historical_oos_boundary"] == {
        "blocks_production_no_live": False,
        "blocks_formal_promotion_or_live": False,
        "future_data_counts_as_oos": False,
        "future_data_blocks_production_no_live": False,
        "future_data_blocks_formal_promotion_or_live": False,
        "historical_strict_oos_required_for_formal_promotion_or_live": True,
    }


def test_two_round_verifier_rejects_outside_scope_write_and_candidate_activation():
    first = _round(
        rowid=1,
        current_core=[],
        current_direction=None,
        core=["38C"],
        direction="UP",
        candidate_activations=1,
    )
    second = _round(
        rowid=2,
        current_core=["38C"],
        current_direction="UP",
        core=["39C"],
        direction="DOWN",
        candidate_activations=1,
    )
    result = runner.verify_two_round_resident_receipts(
        first,
        second,
        before_snapshot=_snapshot(0),
        after_snapshot=_snapshot(1),
        station_ids=_scope(),
    )
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "resident_round_1_candidate_activation_nonzero" in result["blockers"]
    assert "outside_scope_probability_table_write_detected" in result["blockers"]


def test_two_round_verifier_accepts_future_not_started_zero_write() -> None:
    receipt = _round(
        rowid=10,
        current_core=["37C"],
        current_direction="NEUTRAL",
        core=["38C", "39C"],
        direction="UP",
    )
    receipt["started_station_ids"] = ["EGLC"]
    receipt["not_started_station_ids"] = ["RKPK"]
    receipt["station_results"]["RKPK"] = {
        "state": "NOT_STARTED_NO_MATERIALIZATION_REQUIRED",
        "started": False,
        "core_chain": None,
    }
    result = runner.verify_two_round_resident_receipts(
        receipt,
        {**receipt, "round_sha256": "b" * 64},
        before_snapshot=_snapshot(),
        after_snapshot=_snapshot(),
        station_ids=_scope(),
    )
    assert result["state"] == "PASS"


def test_two_round_verifier_blocks_when_no_canonical_station_starts() -> None:
    receipt = _round(
        rowid=10,
        current_core=[],
        current_direction=None,
        core=["38C"],
        direction="NEUTRAL",
    )
    receipt["started_station_ids"] = []
    receipt["not_started_station_ids"] = list(_scope())
    receipt["probability_evidence_runtime"]["fallback_proof_counts"] = {}
    receipt["station_results"] = {
        station: {
            "state": "NOT_STARTED_NO_MATERIALIZATION_REQUIRED",
            "started": False,
            "core_chain": None,
        }
        for station in _scope()
    }
    result = runner.verify_two_round_resident_receipts(
        receipt,
        {**receipt, "round_sha256": "b" * 64},
        before_snapshot=_snapshot(),
        after_snapshot=_snapshot(),
        station_ids=_scope(),
    )
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "resident_no_canonical_station_started_both_rounds" in result[
        "blockers"
    ]


def test_migration_verifier_allows_later_materialization_but_binds_snapshots() -> None:
    first = _round(
        rowid=10,
        current_core=[],
        current_direction=None,
        core=["38C"],
        direction="NEUTRAL",
    )
    first.update({
        "registry_started_station_ids": list(_scope()),
        "candidate_accumulation_station_ids": [],
        "outside_scope_station_counts": {"facts": 0},
        "scoped_station_counts": {"facts": 2},
        "forbidden_execution_counts": {"order_events": 0},
        "materialization_pending_blockers": [
            "RKPK:directional_core_decision_missing"
        ],
    })
    first["probability_evidence_runtime"]["missing_tables"] = []
    second = {
        **first,
        "round_sha256": "b" * 64,
    }
    result = runner.verify_migration_two_round_resident_receipts(
        first,
        second,
        before_snapshot=_snapshot(),
        after_snapshot=_snapshot(),
        station_ids=_scope(),
    )
    assert result["state"] == "PASS"
    assert result["materialization_pending_blockers"] == [
        "RKPK:directional_core_decision_missing"
    ]

    blocked = runner.verify_migration_two_round_resident_receipts(
        first,
        second,
        before_snapshot=_snapshot(0),
        after_snapshot=_snapshot(1),
        station_ids=_scope(),
    )
    assert blocked["state"] == "BLOCKED_FAIL_CLOSED"
    assert (
        "migration_outside_scope_probability_write_detected"
        in blocked["blockers"]
    )
