from __future__ import annotations

import sqlite3

from scripts import run_bayes_basket_target_post_migration_no_live as runner


def test_vertical_slice_accepts_only_exact_rkpk_scope() -> None:
    packet = {
        "station_ids": ["RKPK"],
        "calibration_artifacts": [{"station_id": "RKPK"}],
        "global_priors": [{"global_prior_id": "global"}],
    }
    manifest = {
        "station_scope": {
            "mode": "allowlist",
            "station_ids": ["RKPK"],
        }
    }

    assert runner._validate_vertical_slice_scope(
        station_id="RKPK",
        packet=packet,
        manifest=manifest,
    ) == []


def test_vertical_slice_rejects_batch_and_cross_station_rows() -> None:
    blockers = runner._validate_vertical_slice_scope(
        station_id="RKPK",
        packet={
            "station_ids": ["RKPK", "EGLC"],
            "calibration_artifacts": [
                {"station_id": "RKPK"},
                {"station_id": "EGLC"},
            ],
        },
        manifest={
            "station_scope": {
                "mode": "allowlist",
                "station_ids": ["RKPK", "EGLC"],
            }
        },
    )

    assert "artifact_packet_not_single_station_exact_scope" in blockers
    assert "artifact_packet_cross_station_row:calibration_artifacts" in blockers
    assert "market_manifest_not_single_station_exact_scope" in blockers


def test_vertical_slice_station_cannot_change_before_oos_gate() -> None:
    blockers = runner._validate_vertical_slice_scope(
        station_id="EGLC",
        packet={"station_ids": ["EGLC"]},
        manifest={
            "station_scope": {
                "mode": "allowlist",
                "station_ids": ["EGLC"],
            }
        },
    )
    assert "vertical_slice_station_not_frozen_authority" in blockers


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
    expected = (
        runner.MAX_TARGET_DB_BYTES + runner.MAX_TARGET_WAL_BYTES
    )
    assert observed == [
        (runner.resource.RLIMIT_FSIZE, (expected, expected))
    ]


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


def test_probability_scope_snapshot_covers_all_probability_and_bridge_tables(
    tmp_path,
) -> None:
    db = tmp_path / "target.db"
    tables = runner.PROBABILITY_ACCEPTANCE_TABLES
    with sqlite3.connect(db) as conn:
        for table in tables:
            if table == "basket_probability_bridge_bucket_probability":
                conn.execute(
                    f"CREATE TABLE {table}(history_id TEXT NOT NULL)"
                )
                conn.executemany(
                    f"INSERT INTO {table}(history_id) VALUES(?)",
                    [("RKPK-history",), ("ZUUU-history",)],
                )
            elif table == "basket_probability_bridge_history":
                conn.execute(
                    f"CREATE TABLE {table}"
                    "(history_id TEXT PRIMARY KEY,station_id TEXT NOT NULL)"
                )
                conn.executemany(
                    f"INSERT INTO {table}(history_id,station_id) VALUES(?,?)",
                    [("RKPK-history", "RKPK"), ("ZUUU-history", "ZUUU")],
                )
            else:
                conn.execute(
                    f"CREATE TABLE {table}(station_id TEXT NOT NULL)"
                )
                conn.executemany(
                    f"INSERT INTO {table}(station_id) VALUES(?)",
                    [("RKPK",), ("ZUUU",)],
                )
        conn.commit()

    snapshot = runner.probability_scope_snapshot(db)

    assert snapshot["missing_tables"] == []
    assert set(snapshot["non_target_counts"]) == set(tables)
    assert all(value == 1 for value in snapshot["non_target_counts"].values())


def test_two_round_resident_verifier_binds_core_direction_and_zero_boundaries():
    def round_receipt(rowid, current_core, current_direction, core, direction):
        return {
            "state": "PASS",
            "station_id": "RKPK",
            "round_sha256": "a" * 64,
            "services": {
                "com.weather.reported_trajectory_no": 101,
                "com.weather.market_ws": 102,
                "com.weather.clob_order_service": 103,
            },
            "resources": {
                "database_bytes": 1024,
                "wal_bytes": 0,
                "free_bytes": 4 * 1024 * 1024 * 1024,
                "resident_service_rss_bytes": {
                    "com.weather.reported_trajectory_no": 1024,
                    "com.weather.market_ws": 1024,
                    "com.weather.clob_order_service": 1024,
                },
            },
            "probability_evidence_runtime": {
                "candidate_activation_count_all": 0,
                "fallback_contract_failures": [],
                "fallback_proof_counts": {
                    "P0_HISTORICAL_TILT": 1,
                    "METAR_TRAJECTORY_UPDATE": 1,
                },
                "missing_bridge_tables": [],
            },
            "core_chain": {
                "latest": {
                    "rowid": rowid,
                    "current_core_buckets": current_core,
                    "core_buckets": core,
                    "current_direction": current_direction,
                    "direction": direction,
                }
            },
            "production_no_live": True,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }

    first = round_receipt(10, [], None, ["38C", "39C"], "UP")
    second = round_receipt(
        11,
        ["38C", "39C"],
        "UP",
        ["37C", "38C"],
        "DOWN",
    )
    before = {"missing_tables": [], "non_target_counts": {
        table: 0 for table in runner.PROBABILITY_ACCEPTANCE_TABLES
    }}
    after = {"missing_tables": [], "non_target_counts": {
        table: 0 for table in runner.PROBABILITY_ACCEPTANCE_TABLES
    }}

    result = runner.verify_two_round_resident_receipts(
        first,
        second,
        before_snapshot=before,
        after_snapshot=after,
    )

    assert result["state"] == "PASS"
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_two_round_resident_verifier_rejects_non_rkpk_delta_and_candidate_activation():
    base = {
        "state": "PASS",
        "station_id": "RKPK",
        "round_sha256": "a" * 64,
        "services": {
            "com.weather.reported_trajectory_no": 101,
            "com.weather.market_ws": 102,
            "com.weather.clob_order_service": 103,
        },
        "resources": {
            "database_bytes": 1024,
            "wal_bytes": 0,
            "free_bytes": 4 * 1024 * 1024 * 1024,
            "resident_service_rss_bytes": {
                "com.weather.reported_trajectory_no": 1024,
                "com.weather.market_ws": 1024,
                "com.weather.clob_order_service": 1024,
            },
        },
        "probability_evidence_runtime": {
            "candidate_activation_count_all": 1,
            "fallback_contract_failures": [],
            "fallback_proof_counts": {
                "P0_HISTORICAL_TILT": 1,
                "METAR_TRAJECTORY_UPDATE": 1,
            },
            "missing_bridge_tables": [],
        },
        "core_chain": {"latest": {
            "rowid": 1,
            "current_core_buckets": [],
            "core_buckets": ["38C"],
            "current_direction": None,
            "direction": "UP",
        }},
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    second = dict(base)
    second["round_sha256"] = "b" * 64
    second["core_chain"] = {"latest": {
        "rowid": 2,
        "current_core_buckets": ["38C"],
        "core_buckets": ["39C"],
        "current_direction": "UP",
        "direction": "DOWN",
    }}
    tables = runner.PROBABILITY_ACCEPTANCE_TABLES

    result = runner.verify_two_round_resident_receipts(
        base,
        second,
        before_snapshot={"missing_tables": [], "non_target_counts": {
            table: 0 for table in tables
        }},
        after_snapshot={"missing_tables": [], "non_target_counts": {
            table: 1 for table in tables
        }},
    )

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "resident_round_1_candidate_activation_nonzero" in result["blockers"]
    assert "non_rkpk_probability_table_write_detected" in result["blockers"]


def test_two_round_resident_verifier_accepts_unchanged_zero_write_round():
    services = {
        "com.weather.reported_trajectory_no": 101,
        "com.weather.market_ws": 102,
        "com.weather.clob_order_service": 103,
    }
    receipt = {
        "state": "PASS",
        "station_id": "RKPK",
        "round_sha256": "a" * 64,
        "services": services,
        "resources": {
            "database_bytes": 1024,
            "wal_bytes": 0,
            "free_bytes": 4 * 1024 * 1024 * 1024,
            "resident_service_rss_bytes": {
                label: 1024 for label in services
            },
            "acceptance_process_rss_bytes": 1024,
        },
        "probability_evidence_runtime": {
            "candidate_activation_count_all": 0,
            "fallback_contract_failures": [],
            "fallback_proof_counts": {
                "P0_HISTORICAL_TILT": 1,
                "METAR_TRAJECTORY_UPDATE": 1,
            },
            "missing_bridge_tables": [],
        },
        "core_chain": {"latest": {
            "rowid": 10,
            "current_core_buckets": ["37C"],
            "core_buckets": ["38C", "39C"],
            "current_direction": "NEUTRAL",
            "direction": "UP",
        }},
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    snapshot = {
        "missing_tables": [],
        "non_target_counts": {
            table: 0 for table in runner.PROBABILITY_ACCEPTANCE_TABLES
        },
    }

    result = runner.verify_two_round_resident_receipts(
        receipt,
        {**receipt, "round_sha256": "b" * 64},
        before_snapshot=snapshot,
        after_snapshot=snapshot,
    )

    assert result["state"] == "PASS"
