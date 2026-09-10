import copy
import json
import sqlite3

import weather_fact_snapshot_writer as writer
from scripts import posterior_ssot_fact_coverage_audit as coverage_audit
from weather_fact_snapshot_writer import (
    PHASE0_FACT_JSON_CONTRACT_KEYS,
    apply_fact_snapshot_write_plan,
    apply_fact_latest_repoint_plan,
    apply_fact_summary_cache_repair_plan,
    build_hierarchical_posterior_fact_payload,
    build_hierarchical_posterior_fact_snapshot_write_plan,
    build_fact_latest_repoint_plan,
    build_fact_snapshot_write_plan,
    build_fact_summary_cache_repair_plan,
    compact_fact_snapshot_writer_apply_result_for_lineage,
    compact_replaced_event_fact_history_row,
    merge_fill_fact_payload,
)
from hierarchical_posterior_pipeline_runtime import build_hierarchical_posterior_pipeline


def _source_trace(source_class="hard_observation_candidate", hard_eligible=True):
    return {
        "schema": "weather_source_acquisition_trace_v1",
        "correlation_id": "corr_1234567890abcdef",
        "source_observations": [
            {
                "source_event_id": "srcevt_1234567890abcdef",
                "source_observation_id": "srcobs_1234567890abcdef",
                "source_id": "awc_cache_metars_csv_gz",
                "source_class": source_class,
                "hard_eligible": hard_eligible,
                "valid_time": "2026-07-03T09:00:00Z",
                "temperature_c": 30.0,
            }
        ],
        "source_latency_trace": [
            {
                "source_event_id": "srcevt_1234567890abcdef",
                "source_observation_id": "srcobs_1234567890abcdef",
                "source_id": "awc_cache_metars_csv_gz",
                "valid_time": "2026-07-03T09:00:00Z",
                "response_duration_ms": 300,
            }
        ],
        "source_observation_ids": ["srcobs_1234567890abcdef"],
        "hard_observation_candidate_ids": ["srcevt_1234567890abcdef"] if hard_eligible else [],
    }


def _posterior_ssot_fields(distribution):
    """The posterior SSOT block the apply contract requires.

    Derived from the same {label: probability} mapping the prior fields
    are, so the two cannot disagree.  Spread before any key a test sets
    for itself, which is how the negative cases below still install their
    own invalid block and still get blocked.

    Added 2026-09-07: the contract grew and these payloads did not, so
    nineteen tests here were blocked on posterior_ssot_apply_contract_failed
    before their own assertion ran.
    """

    rows = [
        {"bucket": str(label), "probability": float(probability)}
        for label, probability in dict(distribution).items()
    ]
    if not rows:
        return {}
    total = sum(row["probability"] for row in rows) or 1.0
    rows = [
        {"bucket": row["bucket"], "probability": row["probability"] / total}
        for row in rows
    ]
    top = max(rows, key=lambda row: row["probability"])
    selection = {
        "target_bucket": top["bucket"],
        "target_probability": top["probability"],
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "ssot_source": "posterior_ssot_fact",
        "selection_runtime_recomputed": False,
        "fact_snapshot_id": "unit_fact_snapshot",
        "posterior_run_id": "unit_posterior_run",
    }
    return {
        "fact_snapshot_id": "unit_fact_snapshot",
        "posterior_run_id": "unit_posterior_run",
        "phase": "D0_unit_test",
        "phase_reason": "unit_test_ssot_contract",
        "beta_weak": 0.0,
        "beta_strong": 1.0,
        "hard_mask_reason": "unit_test_no_hard_mask",
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "unit_fact_snapshot",
            "posterior_run_id": "unit_posterior_run",
            "target_bucket": top["bucket"],
            "target_probability": top["probability"],
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "bucket_probabilities": rows,
            "boundaries": {
                "frontend_recompute_allowed": False,
                "decision_recompute_allowed": False,
                "hard_mask_is_constraint_not_weight": True,
            },
            "layer_chain": {"formula": "prior*likelihood/normalizer"},
        },
        "posterior_target_bucket_selection": dict(selection),
        "target_bucket_selection": dict(selection),
    }


def _prior_fields(distribution):
    return {
        **_posterior_ssot_fields(distribution),
        "prior_distribution": dict(distribution),
        "prior_na_reason": "",
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "state": "available",
            "source": "unit_test_settled_history_prior",
            "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
            "sample_count": 10,
            "shrinkage_target": "broader_settled_history",
            "date_window_days": 15,
            "prior_na_reason": "",
        },
    }


def _create_fact_tables(conn):
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            model_snapshot_id INTEGER,
            market_snapshot_id INTEGER,
            composer_version TEXT NOT NULL,
            market_updated_at TEXT,
            model_updated_at TEXT,
            market_stale_sec INTEGER,
            model_cache_age_sec INTEGER,
            best_side TEXT,
            best_outcome TEXT,
            best_edge_score REAL,
            best_signal_edge_entry REAL,
            correlation_id TEXT,
            source_event_id TEXT,
            fact_snapshot_id TEXT,
            posterior_run_id TEXT,
            signal_id TEXT,
            review_id TEXT,
            fact_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_id INTEGER NOT NULL,
            fact_snapshot_id TEXT,
            correlation_id TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (slug, event_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_summary_cache (
            fact_id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            run_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE source_observations (
            source_event_id TEXT PRIMARY KEY,
            source_observation_id TEXT,
            source_id TEXT,
            source_class TEXT,
            role TEXT,
            station_id TEXT,
            event_date TEXT,
            valid_time TEXT,
            valid_time_local TEXT,
            fetched_at TEXT,
            parsed_at TEXT,
            response_completed_at TEXT,
            response_duration_ms REAL,
            temperature_c REAL,
            dewpoint_c REAL,
            observed_today_max_c REAL,
            forecast_peak_c REAL,
            raw_ref TEXT,
            raw_sha256 TEXT,
            parser_version TEXT,
            hard_eligible TEXT,
            demotion_reason TEXT,
            quality_flags_json TEXT,
            observation_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_test_source_observations_source_observation_id "
        "ON source_observations(source_observation_id)"
    )


def test_fact_snapshot_write_plan_is_no_apply_and_has_lineage():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "distribution": [{"outcome": "30", "q_temperature_bucket_posterior": 0.8}],
            **_prior_fields({"30": 1.0}),
            "weather_likelihood_features": {"zuuu_metar_raw_awc_today_max_c": 30.0},
        },
        source_acquisition_trace=_source_trace(),
    )

    assert plan["schema"] == "weather_fact_snapshot_writer_plan_v1"
    assert plan["read_only"] is True
    assert plan["dry_run"] is True
    assert plan["apply_supported"] is False
    assert plan["db_write_guard"]["writes_event_fact_history"] is False
    assert plan["db_write_guard"]["writes_event_fact_latest"] is False
    assert plan["db_write_guard"]["writes_event_fact_summary_cache"] is False
    assert plan["fact_snapshot_id"].startswith("fact_")
    assert plan["source_event_ids"] == ["srcevt_1234567890abcdef"]
    assert plan["source_observation_ids"] == ["srcobs_1234567890abcdef"]
    assert plan["lineage_state"]["source_observation_ids"] == ["srcobs_1234567890abcdef"]
    actions = {item["action"]: item for item in plan["plan_actions"]}
    assert actions["append_event_fact_history"]["would_write"] is True
    assert actions["append_event_fact_history"]["apply_status"] == "blocked_no_apply"
    assert actions["repoint_event_fact_latest"]["would_write"] is True
    assert actions["upsert_event_fact_summary_cache"]["would_write"] is True


def test_fact_snapshot_apply_contract_blocks_without_explicit_allow(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    _create_fact_tables(conn)
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "distribution": [{"outcome": "30", "q_temperature_bucket_posterior": 0.8}],
            **_prior_fields({"30": 1.0}),
            "weather_likelihood_features": {"zuuu_metar_raw_awc_today_max_c": 30.0},
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )

    result = apply_fact_snapshot_write_plan(conn, plan)

    assert plan["apply_supported"] is True
    assert result["ok"] is False
    assert result["applied"] is False
    assert result["block_reason"] == "allow_apply_false"
    assert result["guards"]["writes_archive_db"] is False
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0
    conn.close()


def test_source_observation_append_is_idempotent_but_raw_identity_drift_fails_closed(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    _create_fact_tables(conn)
    trace = _source_trace()
    row = trace["source_observations"][0]
    row["raw_sha256"] = "a" * 64
    row["raw_ref"] = "research://raw/first"
    payload = {"source_acquisition_trace": trace}

    first = writer._persist_source_observations_for_fact_payload(
        conn,
        payload,
        created_at="2026-07-03T09:05:41Z",
    )
    second = writer._persist_source_observations_for_fact_payload(
        conn,
        payload,
        created_at="2026-07-03T09:06:00Z",
    )
    drift_payload = copy.deepcopy(payload)
    drift_payload["source_acquisition_trace"]["source_observations"][0]["raw_sha256"] = "b" * 64
    drift = writer._persist_source_observations_for_fact_payload(
        conn,
        drift_payload,
        created_at="2026-07-03T09:06:01Z",
    )

    assert first["ok"] is True and first["inserted"] == 1
    assert second["ok"] is True and second["ignored"] == 1
    assert drift["ok"] is False
    assert drift["state"] == "immutable_replay_drift"
    assert "raw_sha256" in drift["replay_drift"][0]["differing_immutable_fields"]
    stored = conn.execute("SELECT raw_sha256 FROM source_observations").fetchone()[0]
    assert stored == "a" * 64
    assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 1
    conn.close()


def test_later_transport_receipt_of_same_physical_observation_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    _create_fact_tables(conn)
    trace = _source_trace()
    row = trace["source_observations"][0]
    row.update(
        {
            "raw_sha256": "a" * 64,
            "raw_ref": "research://raw/first",
            "fetched_at": "2026-07-03T09:05:30Z",
            "parsed_at": "2026-07-03T09:05:31Z",
            "response_completed_at": "2026-07-03T09:05:30Z",
            "response_duration_ms": 300,
        }
    )
    first = writer._persist_source_observations_for_fact_payload(
        conn,
        {"source_acquisition_trace": trace},
        created_at="2026-07-03T09:05:31Z",
    )
    replay = copy.deepcopy(trace)
    replay_row = replay["source_observations"][0]
    replay_row.update(
        {
            "source_event_id": "srcevt_later_transport_receipt",
            "fetched_at": "2026-07-03T09:06:30Z",
            "parsed_at": "2026-07-03T09:06:31Z",
            "response_completed_at": "2026-07-03T09:06:30Z",
            "response_duration_ms": 500,
        }
    )
    second = writer._persist_source_observations_for_fact_payload(
        conn,
        {"source_acquisition_trace": replay},
        created_at="2026-07-03T09:06:31Z",
    )

    assert first["ok"] is True and first["inserted"] == 1
    assert second["ok"] is True and second["inserted"] == 0
    assert second["ignored"] == 1
    assert second["transport_replays_ignored"] == 1
    stored = conn.execute(
        "SELECT source_event_id, fetched_at FROM source_observations"
    ).fetchone()
    assert stored == ("srcevt_1234567890abcdef", "2026-07-03T09:05:30Z")
    conn.close()


def test_same_source_event_reparse_does_not_create_false_immutable_drift(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    _create_fact_tables(conn)
    trace = _source_trace()
    row = trace["source_observations"][0]
    row.update(
        {
            "raw_sha256": "a" * 64,
            "raw_ref": "research://raw/first",
            "parsed_at": "2026-07-03T09:05:31Z",
        }
    )
    first = writer._persist_source_observations_for_fact_payload(
        conn,
        {"source_acquisition_trace": trace},
        created_at="2026-07-03T09:05:31Z",
    )
    reparse = copy.deepcopy(trace)
    reparse["source_observations"][0]["parsed_at"] = "2026-07-03T09:06:31Z"
    second = writer._persist_source_observations_for_fact_payload(
        conn,
        {"source_acquisition_trace": reparse},
        created_at="2026-07-03T09:06:31Z",
    )

    assert first["ok"] is True and first["inserted"] == 1
    assert second["ok"] is True and second["ignored"] == 1
    assert conn.execute("SELECT parsed_at FROM source_observations").fetchone()[0] == "2026-07-03T09:05:31Z"
    conn.close()


def test_same_stable_observation_id_with_different_physical_content_fails_closed(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    _create_fact_tables(conn)
    trace = _source_trace()
    row = trace["source_observations"][0]
    row.update({"raw_sha256": "a" * 64, "raw_ref": "research://raw/first"})
    writer._persist_source_observations_for_fact_payload(
        conn,
        {"source_acquisition_trace": trace},
        created_at="2026-07-03T09:05:31Z",
    )
    collision = copy.deepcopy(trace)
    collision_row = collision["source_observations"][0]
    collision_row.update(
        {
            "source_event_id": "srcevt_conflicting_transport",
            "fetched_at": "2026-07-03T09:06:30Z",
            "raw_sha256": "b" * 64,
        }
    )

    result = writer._persist_source_observations_for_fact_payload(
        conn,
        {"source_acquisition_trace": collision},
        created_at="2026-07-03T09:06:31Z",
    )

    assert result["ok"] is False
    assert result["state"] == "immutable_replay_drift"
    assert "raw_sha256" in result["replay_drift"][0]["differing_immutable_fields"]
    conn.close()


def test_fact_snapshot_apply_contract_writes_history_latest_and_summary_cache(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)
    old_dirty_path = writer.UI_SNAPSHOT_DIRTY_STAMP_PATH
    writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = tmp_path / "ui_snapshot_dirty.stamp"
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "fact_as_of": "2026-07-03T08:00:00Z",
            "market_updated_at": "2026-07-03T09:04:00Z",
            "model_snapshot_id": 10,
            "market_snapshot_id": 20,
            "distribution": [
                {"outcome": "29", "q_temperature_bucket_posterior": 0.2},
                {"outcome": "30", "q_temperature_bucket_posterior": 0.8, "edge_score_yes": 0.12},
            ],
            **_prior_fields({"29": 0.2, "30": 0.8}),
            "weather_likelihood_features": {"zuuu_metar_raw_awc_today_max_c": 30.0},
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )

    try:
        result = apply_fact_snapshot_write_plan(
            conn,
            plan,
            allow_apply=True,
            commit=True,
            created_at="2026-07-03T09:05:41Z",
        )
    finally:
        writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = old_dirty_path

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["committed_at"].endswith("Z")
    assert result["prior_layer_contract"]["ok"] is True
    assert result["prior_layer_contract"]["state"] == "ready"
    assert result["posterior_ssot_apply_contract"]["ok"] is True
    assert result["posterior_ssot_apply_contract"]["state"] == "ready"
    assert result["post_apply_persisted_posterior_ssot_verification"]["ok"] is True
    assert result["post_apply_persisted_posterior_ssot_verification"]["fact_id"] == result["fact_id"]
    assert result["source_observation_persistence"]["state"] == "available"
    assert result["source_observation_persistence"]["attempted"] == 1
    assert result["source_observation_persistence"]["inserted"] == 1
    assert result["source_observation_persistence"]["source_observation_id_column_present"] is True
    source_lineage_contract = result["post_apply_persisted_posterior_ssot_verification"]["source_lineage_contract"]
    assert source_lineage_contract["ok"] is True
    assert source_lineage_contract["persisted_source_event_ids"] == ["srcevt_1234567890abcdef"]
    assert source_lineage_contract["expected_source_event_ids"] == ["srcevt_1234567890abcdef"]
    assert source_lineage_contract["persisted_source_observation_ids"] == ["srcobs_1234567890abcdef"]
    assert source_lineage_contract["expected_source_observation_ids"] == ["srcobs_1234567890abcdef"]
    assert source_lineage_contract["source_observation_id_replay_anchor_ready"] is True
    result_summary = compact_fact_snapshot_writer_apply_result_for_lineage(result)
    assert result_summary["schema"] == "weather_fact_snapshot_writer_apply_result_summary_v1"
    assert result_summary["lineage_verification_state"] == "persisted_verified"
    assert result_summary["prior_layer_contract_ok"] is True
    assert result_summary["prior_layer_contract_state"] == "ready"
    assert result_summary["prior_distribution_bucket_count"] == 2
    assert result_summary["posterior_ssot_apply_contract_ok"] is True
    assert result_summary["post_apply_persisted_verification_ok"] is True
    assert result_summary["source_lineage_contract_ok"] is True
    assert result_summary["source_observation_persistence_state"] == "available"
    assert result_summary["source_observation_persistence_inserted"] == 1
    assert result_summary["source_observation_id_column_present"] is True
    assert result_summary["source_observation_id_replay_anchor_ready"] is True
    assert result_summary["persisted_source_observation_ids"] == ["srcobs_1234567890abcdef"]
    assert result_summary["target_bucket"] == "30"
    assert result_summary["fact_id"] == result["fact_id"]
    assert result["guards"]["writes_event_fact_history"] is True
    assert result["guards"]["writes_source_observations"] is True
    assert result["guards"]["writes_event_fact_latest"] is True
    assert result["guards"]["writes_event_fact_summary_cache"] is True
    history = conn.execute("SELECT * FROM event_fact_history").fetchone()
    latest = conn.execute("SELECT * FROM event_fact_latest").fetchone()
    summary = conn.execute("SELECT * FROM event_fact_summary_cache").fetchone()
    source_observation = conn.execute("SELECT * FROM source_observations").fetchone()
    assert history["id"] == result["fact_id"]
    assert history["best_outcome"] == "30"
    assert history["correlation_id"] == "corr_1234567890abcdef"
    assert history["fact_snapshot_id"] == result["fact_snapshot_id"]
    assert history["source_event_id"] == "srcevt_1234567890abcdef"
    assert history["posterior_run_id"]
    assert latest["fact_id"] == result["fact_id"]
    assert latest["correlation_id"] == "corr_1234567890abcdef"
    assert latest["fact_snapshot_id"] == result["fact_snapshot_id"]
    assert summary["fact_id"] == result["fact_id"]
    assert source_observation["source_event_id"] == "srcevt_1234567890abcdef"
    assert source_observation["source_observation_id"] == "srcobs_1234567890abcdef"
    assert source_observation["source_id"] == "awc_cache_metars_csv_gz"
    assert source_observation["source_class"] == "hard_observation_candidate"
    assert source_observation["temperature_c"] == 30.0
    persisted_payload = json.loads(history["fact_json"])
    persisted_audit = coverage_audit.audit_fact_payload(
        persisted_payload,
        meta={
            "id": history["id"],
            "slug": history["slug"],
            "event_date": history["event_date"],
            "fact_as_of": history["fact_as_of"],
            "composer_version": history["composer_version"],
        },
    )
    assert persisted_audit["ok"] is True
    assert persisted_audit["issues"] == []
    summary_payload = json.loads(summary["summary_json"])
    assert summary_payload["summary_cache_source"] == "weather_fact_snapshot_writer"
    assert summary_payload["fact_id"] == result["fact_id"]
    assert summary_payload["fact_as_of"] == "2026-07-03T09:05:40Z"
    dirty_payload = json.loads((tmp_path / "ui_snapshot_dirty.stamp").read_text(encoding="utf-8"))
    assert dirty_payload["source"] == "weather_fact_snapshot_writer"
    assert "event_fact_write" in dirty_payload["reason"]
    conn.close()


def test_fact_snapshot_writer_canonicalizes_event_run_date_from_slug(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-7-2026",
        event_date="2026-07-07",
        run_date="2026-07-05",
        fact_as_of="2026-07-07T05:19:25Z",
        incoming_payload={
            "run_date": "2026-07-05",
            "event_date": "2026-07-07",
            "distribution": [{"outcome": "40", "q_temperature_bucket_posterior": 1.0}],
            **_prior_fields({"40": 1.0}),
            "weather_likelihood_features": {"zuuu_metar_raw_awc_today_max_c": 40.0},
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )

    result = apply_fact_snapshot_write_plan(conn, plan, allow_apply=True, created_at="2026-07-07T05:19:26Z")

    assert plan["run_date"] == "2026-07-07"
    assert result["ok"] is True
    history = conn.execute("SELECT run_date, fact_json FROM event_fact_history").fetchone()
    summary = conn.execute("SELECT run_date, summary_json FROM event_fact_summary_cache").fetchone()
    assert history["run_date"] == "2026-07-07"
    assert summary["run_date"] == "2026-07-07"
    assert json.loads(history["fact_json"])["run_date"] == "2026-07-07"
    assert json.loads(summary["summary_json"])["run_date"] == "2026-07-07"
    conn.close()


def test_fact_latest_repoint_apply_preserves_phase0_identity_columns(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "distribution": [{"outcome": "30", "q_temperature_bucket_posterior": 1.0}],
            **_prior_fields({"30": 1.0}),
            "weather_likelihood_features": {"zuuu_metar_raw_awc_today_max_c": 30.0},
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
        write_latest=False,
    )
    result = apply_fact_snapshot_write_plan(conn, plan, allow_apply=True, created_at="2026-07-03T09:05:41Z")
    repoint_plan = build_fact_latest_repoint_plan(
        conn,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_id=result["fact_id"],
        enable_apply_contract=True,
    )

    repoint_result = apply_fact_latest_repoint_plan(
        conn,
        repoint_plan,
        allow_apply=True,
        updated_at="2026-07-03T09:05:42Z",
    )

    latest = conn.execute("SELECT * FROM event_fact_latest").fetchone()
    assert repoint_result["ok"] is True
    assert repoint_result["latest_repointed"] is True
    assert latest["fact_id"] == result["fact_id"]
    assert latest["fact_snapshot_id"] == result["fact_snapshot_id"]
    assert latest["correlation_id"] == "corr_1234567890abcdef"
    conn.close()


def test_fact_snapshot_apply_contract_rolls_back_when_source_observation_lineage_is_lost(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "distribution": [
                {"outcome": "29", "q_temperature_bucket_posterior": 0.2},
                {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
            ],
            **_prior_fields({"29": 0.2, "30": 0.8}),
            "weather_likelihood_features": {"zuuu_metar_raw_awc_today_max_c": 30.0},
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )
    tampered = copy.deepcopy(plan)
    payload = tampered["effective_payload"]
    payload["source_observation_ids"] = []
    payload["source_observations"][0].pop("source_observation_id", None)
    payload["source_latency_trace"][0].pop("source_observation_id", None)
    payload["source_acquisition_trace"]["source_observation_ids"] = []
    payload["source_acquisition_trace"]["source_observations"][0].pop("source_observation_id", None)
    payload["source_acquisition_trace"]["source_latency_trace"][0].pop("source_observation_id", None)
    payload["lineage_state"]["source_observation_ids"] = []
    payload["posterior_ssot_fact"]["lineage"]["source_observation_ids"] = []
    payload["signal_to_order_timing"]["source_observation_ids"] = []
    tampered["payload_hash_after"] = writer.sha256_json(payload)

    result = apply_fact_snapshot_write_plan(conn, tampered, allow_apply=True)

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["block_reason"] == "post_apply_persisted_posterior_ssot_verification_failed"
    verification = result["post_apply_persisted_posterior_ssot_verification"]
    assert "persisted_source_observation_ids_mismatch" in verification["issues"]
    assert verification["source_lineage_contract"]["expected_source_observation_ids"] == ["srcobs_1234567890abcdef"]
    assert verification["source_lineage_contract"]["persisted_source_observation_ids"] == []
    assert verification["source_lineage_contract"]["source_observation_id_replay_anchor_ready"] is False
    result_summary = compact_fact_snapshot_writer_apply_result_for_lineage(result)
    assert result_summary["lineage_verification_state"] == "blocked_before_persist"
    assert result_summary["source_lineage_contract_ok"] is False
    assert result_summary["persisted_source_observation_ids"] == []
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 0
    conn.close()


def test_post_apply_source_event_lineage_allows_persisted_superset(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    _create_fact_tables(conn)
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "distribution": [
                {"outcome": "29", "q_temperature_bucket_posterior": 0.2},
                {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
            ],
            **_prior_fields({"29": 0.2, "30": 0.8}),
            "weather_likelihood_features": {"zuuu_metar_raw_awc_today_max_c": 30.0},
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )
    payload = copy.deepcopy(plan["effective_payload"])
    payload.setdefault("source_event_ids", []).append("srcevt_extra_from_legacy_trace")
    payload.setdefault("lineage_state", {}).setdefault("source_event_ids", []).append(
        "srcevt_extra_from_lineage_state"
    )
    payload_hash = writer.sha256_json(payload)
    conn.execute(
        """
        INSERT INTO event_fact_history(
            run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-07-03",
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03",
            "2026-07-03T09:05:40Z",
            "unit_test",
            json.dumps(payload, ensure_ascii=False),
            "2026-07-03T09:05:41Z",
        ),
    )
    fact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    verification = writer.build_post_apply_persisted_posterior_ssot_verification(
        conn,
        fact_id=fact_id,
        expected_payload_hash=payload_hash,
        expected_source_event_ids=["srcevt_1234567890abcdef"],
        expected_source_observation_ids=["srcobs_1234567890abcdef"],
    )

    assert verification["ok"] is True
    assert verification["issues"] == []
    source_lineage_contract = verification["source_lineage_contract"]
    assert source_lineage_contract["ok"] is True
    assert source_lineage_contract["source_event_ids_contain_expected"] is True
    assert source_lineage_contract["missing_expected_source_event_ids"] == []
    assert source_lineage_contract["expected_source_event_ids"] == ["srcevt_1234567890abcdef"]
    assert source_lineage_contract["persisted_source_observation_ids"] == ["srcobs_1234567890abcdef"]
    assert set(source_lineage_contract["extra_persisted_source_event_ids"]) == {
        "srcevt_extra_from_legacy_trace",
        "srcevt_extra_from_lineage_state",
    }
    conn.close()


def test_fact_snapshot_apply_contract_blocks_invalid_posterior_ssot_payload(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    _create_fact_tables(conn)
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        run_date="2026-07-05",
        fact_as_of="2026-07-05T03:06:53Z",
        incoming_payload={
            "weather_likelihood_features": {
                "observed_today_max_c": 31.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
            },
            "distribution": [
                {"outcome": "29°C or below", "q_temperature_bucket_posterior": 0.60},
                {"outcome": "33°C", "q_temperature_bucket_posterior": 0.40},
            ],
            **_prior_fields({"29°C or below": 0.60, "33°C": 0.40}),
            "phase": "D0_morning",
            "phase_reason": "unit_test",
            "beta_weak": 0.55,
            "beta_strong": 0.35,
            "hard_mask_reason": "observed_today_max_c",
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )
    tampered = copy.deepcopy(plan)
    payload = tampered["effective_payload"]
    payload["posterior_ssot_fact"]["target_bucket"] = "29°C or below"
    payload["posterior_ssot_fact"]["target_probability"] = 0.60
    for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
        payload[key]["target_bucket"] = "29°C or below"
        payload[key]["target_probability"] = 0.60
    tampered["payload_hash_after"] = writer.sha256_json(payload)

    result = apply_fact_snapshot_write_plan(conn, tampered, allow_apply=True)

    assert plan["posterior_ssot_apply_contract"]["ok"] is True
    assert plan["effective_payload"]["posterior_ssot_fact"]["target_bucket"] == "33°C"
    assert result["ok"] is False
    assert result["applied"] is False
    assert result["block_reason"] == "posterior_ssot_apply_contract_failed"
    assert "posterior_ssot_fact_target_below_hard_temperature_floor" in result["posterior_ssot_apply_contract"]["issues"]
    assert "posterior_ssot_fact_target_not_distribution_top" in result["posterior_ssot_apply_contract"]["issues"]
    result_summary = compact_fact_snapshot_writer_apply_result_for_lineage(result)
    assert result_summary["lineage_verification_state"] == "blocked_before_persist"
    assert result_summary["post_apply_persisted_verification_present"] is False
    assert "posterior_ssot_fact_target_below_hard_temperature_floor" in result_summary[
        "posterior_ssot_apply_contract_issues"
    ]
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0
    conn.close()


def test_fact_snapshot_apply_contract_blocks_missing_prior_for_posterior_payload(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    _create_fact_tables(conn)
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        run_date="2026-07-05",
        fact_as_of="2026-07-05T03:06:53Z",
        incoming_payload={
            "distribution": [
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.7},
                {"outcome": "33°C", "q_temperature_bucket_posterior": 0.3},
            ],
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )

    result = apply_fact_snapshot_write_plan(conn, plan, allow_apply=True)

    assert plan["prior_layer_contract"]["ok"] is False
    assert result["ok"] is False
    assert result["applied"] is False
    assert result["block_reason"] == "prior_layer_contract_failed"
    assert "prior_distribution_missing" in result["prior_layer_contract"]["issues"]
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0
    conn.close()


def test_fact_snapshot_write_plan_materializes_missing_prior_from_history_db(tmp_path):
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE event_daily_temps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date TEXT,
            settlement_actual REAL,
            created_at TEXT,
            station_id TEXT,
            city TEXT,
            market_var TEXT,
            market_unit TEXT,
            rounding_rule TEXT
        )
        """
    )
    for event_date, actual in (
        ("2026-06-25", 32.0),
        ("2026-07-01", 32.0),
        ("2026-07-02", 33.0),
    ):
        conn.execute(
            """
            INSERT INTO event_daily_temps(
                event_date, settlement_actual, created_at, station_id, city,
                market_var, market_unit, rounding_rule
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_date,
                actual,
                "2026-07-05T00:00:00+00:00",
                "ZUUU",
                "Chengdu",
                "temperature_2m_max",
                "C",
                "rounded_degrees",
            ),
        )
    conn.commit()
    conn.close()

    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-6-2026",
        event_date="2026-07-06",
        run_date="2026-07-06",
        fact_as_of="2026-07-05T03:06:53Z",
        incoming_payload={
            "city": "Chengdu",
            "station_id": "ZUUU",
            "market_var": "temperature_2m_max",
            "market_unit": "C",
            "rounding_rule": "rounded_degrees",
            "distribution": [
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.7},
                {"outcome": "33°C", "q_temperature_bucket_posterior": 0.3},
            ],
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        prior_history_db_path=db,
    )

    payload = plan["effective_payload"]
    assert plan["prior_layer_contract"]["ok"] is True
    assert plan["prior_layer_contract"]["state"] == "ready"
    assert set(payload["prior_distribution"]) == {"32°C", "33°C"}
    assert abs(sum(payload["prior_distribution"].values()) - 1.0) <= 1e-9
    assert payload["prior_meta"]["schema"] == "temperature_prior_meta_v1"
    assert payload["prior_meta"]["boundary"] == (
        "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market"
    )
    assert payload["prior_meta"]["sample_count"] >= 3
    assert payload["prior_runtime"]["read_only"] is True
    assert payload["prior_runtime"]["guards"]["reads_forecast_sources"] is False
    assert payload["prior_runtime"]["guards"]["reads_current_observations"] is False


def test_fact_snapshot_apply_contract_supports_minimal_history_schema(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            composer_version TEXT,
            fact_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (slug, event_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_summary_cache (
            fact_id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            run_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={"distribution": [{"outcome": "30", "edge_score_yes": 0.12}]},
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )

    result = apply_fact_snapshot_write_plan(conn, plan, allow_apply=True)

    assert result["ok"] is True
    history = conn.execute("SELECT * FROM event_fact_history").fetchone()
    assert history["slug"] == "highest-temperature-in-chengdu-on-july-3-2026"
    assert json.loads(history["fact_json"])["fact_layer_state"]["write_owner"] == "weather_fact_snapshot_writer"
    conn.close()


def test_fact_summary_cache_repair_plan_upserts_existing_fact_without_history_append(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-07-03",
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03",
            "2026-07-03T09:05:40Z",
            "test",
            json.dumps({"distribution": [{"outcome": "30", "q_raw": 0.7}]}),
            "2026-07-03T09:05:41Z",
        ),
    )
    fact_id = conn.execute("SELECT id FROM event_fact_history").fetchone()["id"]
    plan = build_fact_summary_cache_repair_plan(
        fact_id=fact_id,
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        payload={"distribution": [{"outcome": "30", "q_raw": 0.7}]},
        enable_apply_contract=True,
    )

    result = apply_fact_summary_cache_repair_plan(conn, plan, allow_apply=True)

    assert result["ok"] is True
    assert result["guards"]["writes_event_fact_history"] is False
    assert result["guards"]["writes_event_fact_latest"] is False
    assert result["guards"]["writes_event_fact_summary_cache"] is True
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1
    summary = conn.execute("SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?", (fact_id,)).fetchone()
    summary_payload = json.loads(summary["summary_json"])
    assert summary_payload["summary_cache_source"] == "weather_fact_snapshot_writer"
    assert summary_payload["fact_id"] == fact_id
    conn.close()


def test_compacting_replaced_history_preserves_minimal_posterior_ssot(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)
    old_payload = {
        "fact_snapshot_id": "fact_old",
        "posterior_run_id": "post_old",
        "phase": "D0_midday",
        "beta_weak": 0.2,
        "beta_strong": 0.85,
        "phase_reason": "unit_test_phase",
        "hard_mask_reason": "observed_today_max_c",
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_old",
            "posterior_run_id": "post_old",
            "target_bucket": "30°C",
            "target_probability": 0.7,
        },
        "distribution": [
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.3},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.7},
        ],
        "large_debug_blob": "x" * 20000,
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-07-03",
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03",
            "2026-07-03T09:05:40Z",
            "test",
            json.dumps(old_payload),
            "2026-07-03T09:05:41Z",
        ),
    )
    old_fact_id = conn.execute("SELECT id FROM event_fact_history").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-07-03",
            "highest-temperature-in-chengdu-on-july-3-2026",
            "2026-07-03",
            "2026-07-03T10:05:40Z",
            "test",
            json.dumps({"distribution": [{"outcome": "31°C", "q_temperature_bucket_posterior": 0.8}]}),
            "2026-07-03T10:05:41Z",
        ),
    )
    new_fact_id = conn.execute("SELECT MAX(id) FROM event_fact_history").fetchone()[0]
    conn.execute(
        """
        INSERT OR REPLACE INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES(?, ?, ?, ?)
        """,
        ("highest-temperature-in-chengdu-on-july-3-2026", "2026-07-03", new_fact_id, "2026-07-03T10:05:41Z"),
    )

    compacted = compact_replaced_event_fact_history_row(conn, old_fact_id)

    assert compacted is True
    compact_payload = json.loads(
        conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (old_fact_id,)).fetchone()["fact_json"]
    )
    assert compact_payload["history_compacted"] is True
    assert compact_payload["fact_snapshot_id"] == "fact_old"
    assert compact_payload["posterior_run_id"] == "post_old"
    assert compact_payload["phase"] == "D0_midday"
    assert compact_payload["beta_weak"] == 0.2
    assert compact_payload["beta_strong"] == 0.85
    assert compact_payload["hard_mask_reason"] == "observed_today_max_c"
    assert compact_payload["posterior_ssot_fact"]["target_bucket"] == "30°C"
    assert compact_payload["posterior_ssot_fact"]["target_probability"] == 0.7
    assert "large_debug_blob" not in compact_payload
    conn.close()


def test_fact_snapshot_apply_contract_appends_weak_fact_without_repointing_over_hard_latest(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)
    hard_plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "hard_source_direct_ssot_write": True,
            "observed_today_max_c": 30.0,
            "distribution": [{"outcome": "30", "q_temperature_bucket_posterior": 0.9}],
            **_prior_fields({"30": 1.0}),
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )
    hard_result = apply_fact_snapshot_write_plan(conn, hard_plan, allow_apply=True)
    hard_payload = json.loads(
        conn.execute(
            "SELECT fact_json FROM event_fact_history WHERE id = ?",
            (hard_result["fact_id"],),
        ).fetchone()["fact_json"]
    )
    weak_trace = _source_trace(source_class="forecast_weak_source", hard_eligible=False)
    weak_trace["source_observations"][0].update(
        {
            "source_event_id": "srcevt_weak_1234567890",
            "source_observation_id": "srcobs_weak_1234567890",
            "source_id": "weather_com_forecast_peak",
        }
    )
    weak_trace["source_latency_trace"][0].update(
        {
            "source_event_id": "srcevt_weak_1234567890",
            "source_observation_id": "srcobs_weak_1234567890",
            "source_id": "weather_com_forecast_peak",
        }
    )
    weak_trace["source_observation_ids"] = ["srcobs_weak_1234567890"]
    weak_plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:04:40Z",
        current_fact_id=hard_result["fact_id"],
        current_fact_as_of="2026-07-03T09:05:40Z",
            current_payload=hard_payload,
            incoming_payload={
                "distribution": [{"outcome": "29", "q_temperature_bucket_posterior": 0.7}],
                **_prior_fields({"29": 1.0}),
            },
        source_acquisition_trace=weak_trace,
        include_payload=True,
        enable_apply_contract=True,
    )

    weak_result = apply_fact_snapshot_write_plan(conn, weak_plan, allow_apply=True)

    assert weak_result["ok"] is True
    assert weak_result["latest_repointed"] is False
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 2
    latest = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        ("highest-temperature-in-chengdu-on-july-3-2026", "2026-07-03"),
    ).fetchone()
    assert latest["fact_id"] == hard_result["fact_id"]
    conn.close()


def test_merge_fill_does_not_overwrite_incoming_metadata_or_hard_fact():
    effective, operations = merge_fill_fact_payload(
        {
            "source_observations": [{"source_event_id": "incoming"}],
            "observed_today_max_c": 31.0,
        },
        current_payload={
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        source_acquisition_trace={"source_observations": [{"source_event_id": "trace"}]},
        lineage_state={"fact_snapshot_id": "fact_x"},
        fact_layer_state={"state": "planned_no_apply"},
        db_write_guard={"writes_archive_db": False},
    )

    assert effective["observed_today_max_c"] == 31.0
    assert effective["observed_today_max_source"] == "zuuu_metar_raw_awc"
    assert effective["source_observations"] == [{"source_event_id": "incoming"}]
    assert effective["lineage_state"]["fact_snapshot_id"] == "fact_x"
    assert any(op["path"] == "observed_today_max_c" and op["action"] == "preserved_incoming_no_overwrite" for op in operations)
    assert any(op["path"] == "source_observations" and op["action"] == "preserved_incoming_metadata" for op in operations)


def test_writer_resyncs_stale_incoming_lineage_observation_ids_for_apply_contract():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)

    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "distribution": [
                {"outcome": "29", "q_temperature_bucket_posterior": 0.2},
                {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
            ],
            **_prior_fields({"29": 0.2, "30": 0.8}),
            "lineage_state": {
                "fact_snapshot_id": "fact_stale",
                "posterior_run_id": "post_stale",
                "source_event_ids": ["srcevt_stale"],
                "source_observation_ids": ["srcobs_stale"],
                "diagnostic_note": "preserve_non_identity_metadata",
            },
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )

    payload = plan["effective_payload"]
    assert payload["lineage_state"]["source_event_ids"] == plan["source_event_ids"]
    assert payload["lineage_state"]["source_observation_ids"] == plan["source_observation_ids"]
    assert payload["lineage_state"]["fact_snapshot_id"] == plan["fact_snapshot_id"]
    assert payload["lineage_state"]["posterior_run_id"] == plan["posterior_run_id"]
    assert payload["lineage_state"]["diagnostic_note"] == "preserve_non_identity_metadata"
    assert any(
        op["path"] == "lineage_state.source_observation_ids"
        and op["action"] == "synced_writer_lineage_metadata"
        for op in plan["merge_fill_operations"]
    )

    result = apply_fact_snapshot_write_plan(conn, plan, allow_apply=True)

    assert result["ok"] is True
    verification = result["post_apply_persisted_posterior_ssot_verification"]
    assert verification["source_lineage_contract"]["expected_source_observation_ids"] == [
        "srcobs_1234567890abcdef"
    ]
    assert verification["source_lineage_contract"]["persisted_source_observation_ids"] == [
        "srcobs_1234567890abcdef"
    ]
    conn.close()


def test_weak_incoming_plan_cannot_repoint_over_existing_hard_fact():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_as_of="2026-07-03T09:06:00Z",
        current_fact_id=101,
        current_fact_as_of="2026-07-03T09:05:40Z",
        current_payload={
            "hard_source_direct_ssot_write": True,
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        incoming_payload={
            "weather_com_forecast_peak_c": 33.0,
            "distribution": [{"outcome": "33", "q_temperature_bucket_posterior": 0.7}],
        },
        source_acquisition_trace={
            "source_observations": [
                {
                    "source_event_id": "srcevt_weak1234567890",
                    "source_observation_id": "srcobs_weak1234567890",
                    "source_id": "weather_com_forecast_peak",
                    "source_class": "forecast_weak_source",
                    "hard_eligible": False,
                }
            ],
            "source_latency_trace": [],
            "source_observation_ids": ["srcobs_weak1234567890"],
        },
    )

    actions = {item["action"]: item for item in plan["plan_actions"]}
    assert actions["append_event_fact_history"]["would_write"] is True
    assert actions["repoint_event_fact_latest"]["would_write"] is False
    assert actions["repoint_event_fact_latest"]["apply_status"] == "blocked_policy"
    assert actions["repoint_event_fact_latest"]["block_reason"] == "incoming_weaker_than_existing_hard_fact"
    assert any(op["path"] == "observed_today_max_c" and op["action"] == "filled_from_current_hard_fact" for op in plan["merge_fill_operations"])


def test_plan_can_return_effective_payload_when_requested():
    plan = build_fact_snapshot_write_plan(
        slug="s",
        event_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={"rows": []},
        source_acquisition_trace=_source_trace(),
        include_payload=True,
    )

    assert "effective_payload" in plan
    payload = plan["effective_payload"]
    assert payload["source_latency_trace"]
    assert payload["db_write_guard"]["writes_archive_db"] is False
    assert set(PHASE0_FACT_JSON_CONTRACT_KEYS).issubset(payload.keys())
    assert payload["phase"] == "D0_late"
    assert payload["beta_weak"] == 0.08
    assert payload["beta_strong"] == 1.0
    assert payload["phase_reason"] == "d0_late_remaining_warming_time_short_observation_sets_upper_bound"
    assert payload["hard_mask_reason"] == "none"
    assert payload["phase_weighting_state"]["state"] == "materialized"
    assert payload["phase_weighting_state"]["source_runtime_schema"] == "phase_weighting_runtime_v1"
    assert payload["phase_weighting_state"]["awc_release_window"]["window"] == "awc_hard_source_signal_window"
    assert payload["posterior_ssot_fact"]["schema"] == "bayes_posterior_ssot_fact_placeholder_v1"
    assert payload["posterior_ssot_fact"]["state"] == "not_materialized"
    assert payload["hard_source_acceptance_state"]["state"] == "not_applicable"
    assert payload["signal_to_order_timing"]["state_reason"] == "fact_snapshot_write_does_not_submit_orders"
    assert payload["daily_review_state"]["state"] == "pending"
    assert payload["phase0_fact_json_contract"]["missing_required_keys_after_merge_fill"] == []
    assert plan["phase0_fact_json_contract_schema"] == "phase0_fact_json_contract_v1"


def test_generic_fact_writer_materializes_posterior_ssot_fact_from_existing_distribution():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "distribution": [
                {"outcome": "30°C", "q_temperature_bucket_posterior": 0.35},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.65},
            ],
            "phase": "D0_midday",
            "phase_reason": "unit_test_existing_distribution",
            "beta_weak": 0.25,
            "beta_strong": 0.75,
            "hard_mask_reason": "none",
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
    )

    payload = plan["effective_payload"]
    assert plan["posterior_run_id"].startswith("post_")
    assert payload["posterior_run_id"] == plan["posterior_run_id"]
    assert payload["posterior_temperature_distribution"] == [
        {"bucket": "30°C", "probability": 0.35},
        {"bucket": "31°C", "probability": 0.65},
    ]
    assert payload["posterior_temperature_distribution_map"] == {
        "30°C": 0.35,
        "31°C": 0.65,
    }
    ssot = payload["posterior_ssot_fact"]
    assert ssot["schema"] == "bayes_posterior_ssot_fact_v1"
    assert ssot["state"] == "available"
    assert ssot["production_model_family"] == "joint_weak_physical_strong"
    assert ssot["production_model_version"] == "joint_weak_physical_strong_unavailable"
    assert ssot["production_model_ready"] is False
    assert ssot["production_probability_policy"] == "unique_joint_weak_physical_strong_only_fail_closed"
    assert ssot["legacy_model_fallback_allowed"] is False
    assert ssot["fact_snapshot_id"] == plan["fact_snapshot_id"]
    assert ssot["posterior_run_id"] == plan["posterior_run_id"]
    assert ssot["target_bucket"] == "31°C"
    assert ssot["target_probability"] == 0.65
    assert ssot["target_probability_field"] == "posterior_ssot_fact.target_probability"
    assert ssot["materialization_note"] == "wrapped_existing_distribution_without_recomputing_posterior"
    assert payload["posterior_target_bucket_selection"]["ssot_source"] == "posterior_ssot_fact"
    assert payload["target_bucket_selection"] == payload["posterior_target_bucket_selection"]
    assert any(
        op["path"] == "posterior_ssot_fact"
        and op["action"] == "materialized_from_existing_distribution"
        for op in plan["merge_fill_operations"]
    )


def test_generic_fact_writer_replaces_posterior_ssot_target_below_hard_observed_max():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        run_date="2026-07-05",
        fact_as_of="2026-07-05T01:06:03Z",
        incoming_payload={
            "fact_snapshot_id": "fact_stale_low_target",
            "posterior_run_id": "post_stale_low_target",
            "weather_likelihood_features": {
                "zuuu_metar_raw_awc_today_max_c": 30.0,
                "zuuu_metar_raw_awc_latest_temp_c": 30.0,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
            },
            "posterior_ssot_fact": {
                "schema": "bayes_posterior_ssot_fact_v1",
                "state": "available",
                "target_bucket": "29°C or below",
                "target_probability": 0.40,
                "target_probability_field": "posterior_ssot_fact.target_probability",
            },
            "distribution": [
                {
                    "outcome": "29°C or below",
                    "q_temperature_bucket_posterior": 0.40,
                    "posterior_settlement_candidate_bucket_hard_zero": True,
                    "posterior_settlement_candidate_bucket_hard_zero_reasons": [
                        "observed_today_max_above_bucket"
                    ],
                    "observed_today_max_c": 30.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                },
                {"outcome": "33°C", "q_temperature_bucket_posterior": 0.30},
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.20},
            ],
            "phase": "D0_awc_hard_signal",
            "phase_reason": "unit_test_hard_observed_floor",
            "beta_weak": 0.0,
            "beta_strong": 1.0,
            "hard_mask_reason": "observed_today_max_c+current_temperature_c",
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
    )

    payload = plan["effective_payload"]
    ssot = payload["posterior_ssot_fact"]

    assert ssot["state"] == "available"
    assert ssot["target_bucket"] == "33°C"
    assert ssot["target_probability"] == 0.60
    assert ssot["target_probability_pre_hard_mask"] == 0.30
    assert ssot["hard_mask_normalization"]["applied"] is True
    assert ssot["hard_mask_normalization"]["excluded_bucket_count"] == 1
    assert payload["posterior_target_bucket_selection"]["target_bucket"] == "33°C"
    assert payload["posterior_target_bucket_selection"]["target_probability"] == 0.60
    assert payload["target_bucket_selection"]["target_bucket"] == "33°C"
    assert any(
        op["path"] == "posterior_ssot_fact"
        and op["action"] == "replaced_invalid_existing_posterior_ssot_fact"
        for op in plan["merge_fill_operations"]
    )


def test_generic_fact_writer_resyncs_existing_ssot_to_hard_mask_conditioned_probability():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        run_date="2026-07-05",
        fact_as_of="2026-07-05T08:06:36Z",
        incoming_payload={
            "fact_snapshot_id": "fact_current_37",
            "posterior_run_id": "post_current_37",
            "posterior_ssot_fact": {
                "schema": "bayes_posterior_ssot_fact_v1",
                "state": "available",
                "target_bucket": "37°C",
                "target_probability": 0.027126122616945747,
                "target_probability_field": "posterior_ssot_fact.target_probability",
            },
            "distribution": [
                {"outcome": "29°C or below", "q_temperature_bucket_posterior": 0.22014101704055974, "q_bayes_physical_likelihood_hard_zero": True},
                {"outcome": "30°C", "q_temperature_bucket_posterior": 0.1333608779447333, "q_bayes_physical_likelihood_hard_zero": True},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.09673433259880652, "q_bayes_physical_likelihood_hard_zero": True},
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.17809040386064384, "q_bayes_physical_likelihood_hard_zero": True},
                {"outcome": "33°C", "q_temperature_bucket_posterior": 0.19908026766160183, "q_bayes_physical_likelihood_hard_zero": True},
                {"outcome": "34°C", "q_temperature_bucket_posterior": 0.04207842465491585, "q_bayes_physical_likelihood_hard_zero": True},
                {"outcome": "35°C", "q_temperature_bucket_posterior": 0.04021171988091958, "q_bayes_physical_likelihood_hard_zero": True},
                {"outcome": "36°C", "q_temperature_bucket_posterior": 0.035047951132929904, "q_bayes_physical_likelihood_hard_zero": True},
                {"outcome": "37°C", "q_temperature_bucket_posterior": 0.027126122616945747, "q_bayes_physical_likelihood_hard_zero": False},
                {"outcome": "38°C", "q_temperature_bucket_posterior": 0.017903508455964195, "q_bayes_physical_likelihood_hard_zero": False},
                {"outcome": "39°C or higher", "q_temperature_bucket_posterior": 0.01022537415197955, "q_bayes_physical_likelihood_hard_zero": False},
            ],
            "phase": "D0_morning",
            "phase_reason": "unit_test_current_37_hard_zero",
            "beta_weak": 0.55,
            "beta_strong": 0.35,
            "hard_mask_reason": "observed_today_max_c+current_temperature_c",
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
    )

    payload = plan["effective_payload"]
    ssot = payload["posterior_ssot_fact"]
    feasible_total = 0.027126122616945747 + 0.017903508455964195 + 0.01022537415197955
    expected = 0.027126122616945747 / feasible_total

    assert ssot["target_bucket"] == "37°C"
    assert ssot["target_probability"] == expected
    assert ssot["target_probability_pre_hard_mask"] == 0.027126122616945747
    assert ssot["hard_mask_normalization"]["applied"] is True
    assert ssot["hard_mask_normalization"]["excluded_bucket_count"] == 8
    assert abs(sum(row["probability"] for row in ssot["bucket_probabilities"]) - 1.0) <= 1e-12
    distribution_by_bucket = {row["outcome"]: row for row in payload["distribution"]}
    ssot_by_bucket = {row["bucket"]: row["probability"] for row in ssot["bucket_probabilities"]}
    assert distribution_by_bucket["37°C"]["q_temperature_bucket_posterior"] == ssot_by_bucket["37°C"]
    assert distribution_by_bucket["38°C"]["q_temperature_bucket_posterior"] == ssot_by_bucket["38°C"]
    assert distribution_by_bucket["39°C or higher"]["q_temperature_bucket_posterior"] == ssot_by_bucket["39°C or higher"]
    assert distribution_by_bucket["38°C"]["q_temperature_bucket_posterior_pre_posterior_ssot_fact_alignment"] == 0.017903508455964195
    assert payload["posterior_temperature_distribution_map"]["38°C"] == ssot_by_bucket["38°C"]
    assert payload["posterior_target_bucket_selection"]["target_probability"] == expected
    assert payload["target_bucket_probability_pct"] == expected * 100.0
    assert any(
        op["path"] == "posterior_ssot_fact"
        and op["action"] == "resynced_existing_posterior_ssot_fact_from_canonical_hard_mask_distribution"
        for op in plan["merge_fill_operations"]
    )


def test_generic_fact_writer_final_awc_lock_sets_posterior_ssot_probability_to_one():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        run_date="2026-07-05",
        fact_as_of="2026-07-05T08:06:36Z",
        incoming_payload={
            "fact_snapshot_id": "fact_final_37",
            "posterior_run_id": "post_final_37",
            "final_bucket_lock": True,
            "weather_likelihood_features": {
                "observed_today_max_c": 37.0,
                "observed_today_max_final": True,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "settlement_actual": 37.0,
            },
            "distribution": [
                {"outcome": "36°C", "q_temperature_bucket_posterior": 0.035, "q_bayes_physical_likelihood_hard_zero": True},
                {"outcome": "37°C", "q_temperature_bucket_posterior": 0.027},
                {"outcome": "38°C", "q_temperature_bucket_posterior": 0.018},
            ],
            "posterior_temperature_distribution": {
                "bucket_probabilities": [
                    {"bucket": "36°C", "probability": 0.035},
                    {"bucket": "37°C", "probability": 0.027},
                    {"bucket": "38°C", "probability": 0.018},
                ],
            },
            "phase": "D0_hard",
            "phase_reason": "unit_test_awc_final_lock",
            "beta_weak": 0.0,
            "beta_strong": 1.0,
            "hard_mask_reason": "final_bucket_lock",
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
    )

    payload = plan["effective_payload"]
    ssot = payload["posterior_ssot_fact"]

    assert ssot["target_bucket"] == "37°C"
    assert ssot["target_probability"] == 1.0
    assert ssot["target_probability_pre_hard_mask"] == 0.027
    assert ssot["final_bucket_lock"] is True
    assert ssot["hard_mask_normalization"]["reason"] == "final_bucket_lock"
    by_bucket = {row["bucket"]: row["probability"] for row in ssot["bucket_probabilities"]}
    assert by_bucket == {"36°C": 0.0, "37°C": 1.0, "38°C": 0.0}
    distribution_by_bucket = {row["outcome"]: row for row in payload["distribution"]}
    assert {bucket: row["q_temperature_bucket_posterior"] for bucket, row in distribution_by_bucket.items()} == by_bucket
    assert distribution_by_bucket["38°C"]["q_temperature_bucket_posterior_pre_posterior_ssot_fact_alignment"] == 0.018
    assert distribution_by_bucket["38°C"]["posterior_field"] == "posterior_ssot_fact.bucket_probabilities"
    surface_by_bucket = {
        row["bucket"]: row
        for row in payload["posterior_temperature_distribution"]["bucket_probabilities"]
    }
    assert {bucket: row["probability"] for bucket, row in surface_by_bucket.items()} == by_bucket
    assert surface_by_bucket["38°C"]["probability_pre_posterior_ssot_fact_alignment"] == 0.018
    assert surface_by_bucket["38°C"]["posterior_field"] == "posterior_ssot_fact.bucket_probabilities"
    assert payload["posterior_temperature_distribution_map"]["38°C"] == 0.0
    assert payload["posterior_target_bucket_selection"]["target_probability"] == 1.0
    assert payload["target_bucket_probability_pct"] == 100.0


def test_generic_fact_writer_hard_source_final_daily_max_flag_locks_without_settlement_actual():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        run_date="2026-07-05",
        fact_as_of="2026-07-05T08:06:36Z",
        incoming_payload={
            "fact_snapshot_id": "fact_awc_final_daily_max_37",
            "posterior_run_id": "post_awc_final_daily_max_37",
            "weather_likelihood_features": {
                "zuuu_metar_raw_awc_today_max_c": 37.0,
                "zuuu_metar_raw_awc_today_max_at": "2026-07-05T08:00:00+00:00",
                "observed_today_max_c": 37.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "hard_source_final_daily_max": True,
                "hard_source_settlement_final": True,
            },
            "distribution": [
                {"outcome": "36°C", "q_temperature_bucket_posterior": 0.50},
                {"outcome": "37°C", "q_temperature_bucket_posterior": 0.027126122616945747},
                {"outcome": "38°C", "q_temperature_bucket_posterior": 0.47287387738305425},
            ],
            "phase": "D0_hard",
            "phase_reason": "unit_test_awc_final_daily_max",
            "beta_weak": 0.0,
            "beta_strong": 1.0,
            "hard_mask_reason": "hard_source_final_daily_max",
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
    )

    ssot = plan["effective_payload"]["posterior_ssot_fact"]

    assert ssot["target_bucket"] == "37°C"
    assert ssot["target_probability"] == 1.0
    assert ssot["target_probability_pre_hard_mask"] == 0.027126122616945747
    assert ssot["final_bucket_lock"] is True
    assert ssot["hard_mask_normalization"]["reason"] == "final_bucket_lock"
    by_bucket = {row["bucket"]: row["probability"] for row in ssot["bucket_probabilities"]}
    assert by_bucket == {"36°C": 0.0, "37°C": 1.0, "38°C": 0.0}
    distribution_by_bucket = {row["outcome"]: row for row in plan["effective_payload"]["distribution"]}
    assert {bucket: row["q_temperature_bucket_posterior"] for bucket, row in distribution_by_bucket.items()} == by_bucket
    assert distribution_by_bucket["38°C"]["q_temperature_bucket_posterior_pre_posterior_ssot_fact_alignment"] == 0.47287387738305425


def test_generic_fact_writer_resyncs_selection_when_posterior_ssot_fact_already_available():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "fact_snapshot_id": "fact_existing",
            "posterior_run_id": "post_existing",
            "posterior_ssot_fact": {
                "schema": "bayes_posterior_ssot_fact_v1",
                "state": "available",
                "fact_snapshot_id": "fact_existing",
                "posterior_run_id": "post_existing",
                "target_bucket": "31°C",
                "target_probability": 0.65,
                "target_probability_field": "posterior_ssot_fact.target_probability",
            },
            "posterior_target_bucket_selection": {
                "target_bucket": "31°C",
                "target_probability": 0.65,
                "target_probability_field": "posterior_temperature_distribution.bucket_probabilities",
                "source": "awc_fast_trade_fact_write",
            },
            "target_bucket_selection": {
                "target_bucket": "31°C",
                "target_probability": 0.65,
                "target_probability_field": "posterior_temperature_distribution.bucket_probabilities",
                "source": "awc_fast_trade_fact_write",
            },
            "distribution": [
                {"outcome": "30°C", "q_temperature_bucket_posterior": 0.35},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.65},
            ],
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
    )

    payload = plan["effective_payload"]
    for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
        selection = payload[key]
        assert selection["ssot_source"] == "posterior_ssot_fact"
        assert selection["target_probability_field"] == "posterior_ssot_fact.target_probability"
        assert selection["selection_runtime_recomputed"] is False
    assert any(
        op["path"] == "posterior_target_bucket_selection"
        and op["action"] == "filled_from_posterior_ssot_fact"
        for op in plan["merge_fill_operations"]
    )
    assert any(
        op["path"] == "target_bucket_selection"
        and op["action"] == "filled_from_posterior_ssot_fact"
        for op in plan["merge_fill_operations"]
    )


def test_generic_fact_writer_resyncs_posterior_ssot_selection_mismatch():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "fact_snapshot_id": "fact_existing",
            "posterior_run_id": "post_existing",
            "posterior_ssot_fact": {
                "schema": "bayes_posterior_ssot_fact_v1",
                "state": "available",
                "fact_snapshot_id": "fact_existing",
                "posterior_run_id": "post_existing",
                "target_bucket": "31°C",
                "target_probability": 0.65,
                "target_probability_field": "posterior_ssot_fact.target_probability",
            },
            "posterior_target_bucket_selection": {
                "target_bucket": "30°C",
                "target_probability": 0.35,
                "target_probability_field": "posterior_ssot_fact.target_probability",
                "ssot_source": "posterior_ssot_fact",
                "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            },
            "target_bucket_selection": {
                "target_bucket": "30°C",
                "target_probability": 0.35,
                "target_probability_field": "posterior_ssot_fact.target_probability",
                "ssot_source": "posterior_ssot_fact",
                "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            },
            "ssot_target_bucket": "30°C",
            "target_bucket": "30°C",
            "target_bucket_source": "posterior_ssot_fact.top_bucket",
            "target_bucket_source_semantics": "bayes_posterior_ssot_fact_v1",
            "target_bucket_probability_field": "posterior_ssot_fact.target_probability",
            "target_bucket_probability": 0.35,
            "target_bucket_probability_pct": 35.0,
            "distribution": [
                {"outcome": "30°C", "q_temperature_bucket_posterior": 0.35},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.65},
            ],
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
    )

    payload = plan["effective_payload"]
    for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
        selection = payload[key]
        assert selection["target_bucket"] == "31°C"
        assert selection["target_probability"] == 0.65
        assert selection["ssot_source"] == "posterior_ssot_fact"
        assert selection["target_probability_field"] == "posterior_ssot_fact.target_probability"
    assert payload["ssot_target_bucket"] == "31°C"
    assert payload["target_bucket"] == "31°C"
    assert payload["target_bucket_probability"] == 0.65
    assert payload["target_bucket_probability_pct"] == 65.0
    assert any(
        op["path"] == "posterior_target_bucket_selection"
        and op["action"] == "resynced_from_posterior_ssot_fact"
        for op in plan["merge_fill_operations"]
    )
    assert any(
        op["path"] == "target_bucket_selection"
        and op["action"] == "resynced_from_posterior_ssot_fact"
        for op in plan["merge_fill_operations"]
    )
    assert any(
        op["path"] == "target_bucket"
        and op["action"] == "resynced_from_posterior_ssot_fact"
        for op in plan["merge_fill_operations"]
    )


def test_generic_fact_writer_materializes_missing_phase_beta_before_posterior_ssot_fact():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T04:05:40Z",
        incoming_payload={
            "distribution": [
                {"outcome": "30°C", "q_temperature_bucket_posterior": 0.35},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.65},
            ],
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
    )

    payload = plan["effective_payload"]
    ssot = payload["posterior_ssot_fact"]
    assert payload["phase"] == "D0_midday"
    assert payload["beta_weak"] == 0.2
    assert payload["beta_strong"] == 0.85
    assert payload["phase_reason"] == "d0_midday_current_temperature_state_and_momentum_constrain_daily_max"
    assert payload["phase_weighting_state"]["phase_source"] == "phase_weighting_runtime"
    assert ssot["schema"] == "bayes_posterior_ssot_fact_v1"
    assert ssot["state"] == "available"
    assert ssot["phase"] == "D0_midday"
    assert ssot["beta_weak"] == 0.2
    assert ssot["beta_strong"] == 0.85
    assert ssot["target_bucket"] == "31°C"
    assert any(
        op["path"] == "phase"
        and op["action"] == "materialized_from_phase_weighting_runtime"
        for op in plan["merge_fill_operations"]
    )


def test_phase_materialization_does_not_treat_false_string_as_hard_lock():
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T04:05:40Z",
        incoming_payload={
            "hard_source_direct_ssot_write": "false",
            "observed_today_max_c": 30.0,
            "distribution": [
                {"outcome": "30°C", "q_temperature_bucket_posterior": 0.35},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.65},
            ],
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
    )

    payload = plan["effective_payload"]
    assert payload["phase"] == "D0_midday"
    assert payload["beta_weak"] == 0.2
    assert payload["beta_strong"] == 0.85
    assert payload["hard_mask_reason"] == "observed_today_max_c"
    assert payload["phase_weighting_state"]["hard_lock_active"] is False
    assert payload["posterior_ssot_fact"]["phase"] == "D0_midday"


def test_diagnostic_current_observed_max_does_not_materialize_hard_mask():
    trace = {
        "schema": "weather_source_acquisition_trace_v1",
        "source_observations": [
            {
                "source_event_id": "srcevt_diag",
                "source_observation_id": "srcobs_diag",
                "source_id": "wu_twc_observed_intraday",
                "source_class": "diagnostic_current_source",
                "hard_eligible": False,
                "observed_today_max_c": 30.0,
            }
        ],
        "source_latency_trace": [],
        "source_observation_ids": ["srcobs_diag"],
        "diagnostic_current_ids": ["srcevt_diag"],
        "hard_observation_candidate_ids": [],
    }
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T04:05:40Z",
        incoming_payload={
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "wu_twc_observed_intraday",
            "distribution": [
                {"outcome": "29°C", "q_temperature_bucket_posterior": 0.35},
                {"outcome": "30°C", "q_temperature_bucket_posterior": 0.65},
            ],
        },
        source_acquisition_trace=trace,
        include_payload=True,
    )

    payload = plan["effective_payload"]
    gate = payload["phase_weighting_state"]["hard_mask_source_gate"]
    assert payload["hard_mask_reason"] == "none"
    assert payload["posterior_ssot_fact"]["hard_mask_reason"] == "none"
    assert gate["observed_today_max_hard_eligible"] is False
    assert gate["diagnostic_current_source_present"] is True
    assert gate["demoted_reason"] == "diagnostic_current_source_not_hard_observation"


def test_diagnostic_current_observed_max_cannot_repoint_over_existing_hard_fact():
    trace = {
        "schema": "weather_source_acquisition_trace_v1",
        "source_observations": [
            {
                "source_event_id": "srcevt_diag",
                "source_observation_id": "srcobs_diag",
                "source_id": "wu_twc_observed_intraday",
                "source_class": "diagnostic_current_source",
                "hard_eligible": False,
                "observed_today_max_c": 31.0,
            }
        ],
        "source_latency_trace": [],
        "source_observation_ids": ["srcobs_diag"],
        "diagnostic_current_ids": ["srcevt_diag"],
        "hard_observation_candidate_ids": [],
    }
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T04:06:40Z",
        current_fact_id=101,
        current_fact_as_of="2026-07-03T04:05:40Z",
        current_payload={
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        incoming_payload={
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "wu_twc_observed_intraday",
            "distribution": [
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.8},
            ],
        },
        source_acquisition_trace=trace,
        include_payload=True,
    )

    actions = {item["action"]: item for item in plan["plan_actions"]}
    policy = plan["fact_layer_state"]
    assert actions["repoint_event_fact_latest"]["would_write"] is False
    assert actions["repoint_event_fact_latest"]["block_reason"] == "incoming_without_hard_observation_cannot_repoint_over_hard_fact"
    assert policy["latest_repoint_allowed"] is False
    assert policy["latest_repoint_block_reason"] == "incoming_without_hard_observation_cannot_repoint_over_hard_fact"
    assert plan["effective_payload"]["hard_mask_reason"] == "none"


def _mark_production_model_ready(pipeline):
    """Mark the pipeline the way the live composer does.

    v3.1 was the retired predecessor; sequential_remaining_warming_bayes_runtime
    emits joint_weak_physical_strong_composer_v1 and carries the v3.1 block only
    as a retired-not-consumed marker, which is what the writer keys off.
    """
    pipeline["ok"] = True
    pipeline["production_model_ready"] = True
    pipeline["production_model_version"] = "joint_weak_physical_strong_composer_v1"
    pipeline["production_block_reason"] = ""
    pipeline["v31_multimodel_posterior"] = {
        "state": "retired_not_consumed",
        "state_reason": "unique_joint_weak_and_physical_strong_composer_replaces_v31_probability_model",
        "applied_to_metar_strong_likelihood": False,
    }
    posterior = pipeline.get("runtime_outputs", {}).get("sequential_remaining_warming_bayes", {})
    if isinstance(posterior, dict):
        posterior["ok"] = True
        posterior["production_model_ready"] = True
        posterior["production_model_version"] = "joint_weak_physical_strong_composer_v1"
        posterior["production_block_reason"] = ""
        posterior["v31_multimodel_posterior"] = dict(pipeline["v31_multimodel_posterior"])
    return pipeline


def _posterior_pipeline_output(*, hard_source=True):
    buckets = ["29", "30", "31", "32", "33"]
    sources = [
        {
            "source_event_id": "srcevt_model",
            "source_observation_id": "srcobs_model",
            "source_id": "current_model_ensemble",
            "source_class": "forecast_weak_source",
            "role": "model_forecast_weak_likelihood",
            "forecast_peak_c": 32.5,
            "sigma_c": 0.9,
            "confidence": 0.7,
            "fetched_at": "2026-07-03T03:50:00Z",
        }
    ]
    if hard_source:
        sources.append(
            {
                "source_event_id": "srcevt_awc",
                "source_observation_id": "srcobs_awc",
                "source_id": "awc_cache_metars_csv_gz",
                "source_class": "hard_observation_candidate",
                "temperature_c": 28.0,
                "dewpoint_c": 22.0,
                "observed_today_max_c": 28.0,
                "valid_time": "2026-07-03T03:30:00Z",
                "fetched_at": "2026-07-03T03:55:00Z",
                "payload": {
                    "visibility_m": 9999,
                    "cloud_text": "FEW011 SCT020",
                    "dewpoint_trend_c_per_hour": -1.0,
                    "momentum_rate_c_per_hour": 2.0,
                    "temperature_momentum_1h_c_per_hour": 1.4,
                    "temperature_momentum_30m_c_per_hour": 1.1,
                },
            }
        )
    return _mark_production_model_ready(build_hierarchical_posterior_pipeline(
        buckets=buckets,
        prior_distribution={"29": 0.10, "30": 0.20, "31": 0.30, "32": 0.25, "33": 0.15},
        event_date="2026-07-03",
        as_of="2026-07-03T12:00:00+08:00",
        source_observations=sources,
    ))


def test_hierarchical_posterior_fact_plan_materializes_no_apply_payload_and_lineage():
    pipeline = _posterior_pipeline_output()
    plan = build_hierarchical_posterior_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T04:05:40Z",
        pipeline_output=pipeline,
        include_payload=True,
    )

    assert plan["schema"] == "weather_fact_snapshot_writer_plan_v1"
    assert plan["read_only"] is True
    assert plan["dry_run"] is True
    assert plan["apply_supported"] is False
    assert plan["posterior_run_id"].startswith("post_")
    assert plan["lineage_state"]["posterior_run_id"] == plan["posterior_run_id"]
    assert plan["source_event_ids"] == ["srcevt_awc"]
    assert plan["source_observation_ids"] == ["srcobs_awc"]
    assert plan["source_acquisition_trace_summary"]["schema"] == "posterior_pipeline_source_lineage_trace_v1"
    assert plan["source_acquisition_trace_summary"]["hard_observation_candidate_count"] == 1
    assert plan["posterior_fact_snapshot_adapter"]["old_additive_evidence_pool_not_used"] is True
    assert plan["db_write_guard"]["writes_event_fact_history"] is False
    assert plan["db_write_guard"]["writes_event_fact_latest"] is False
    payload = plan["effective_payload"]
    assert payload["fact_payload_schema"] == "hierarchical_posterior_fact_payload_v1"
    assert payload["fact_snapshot_id"] == plan["fact_snapshot_id"]
    assert payload["posterior_run_id"] == plan["posterior_run_id"]
    assert payload["phase"] == "D0_midday"
    assert payload["beta_weak"] == 0.2
    assert payload["beta_strong_base"] == 0.85
    assert payload["beta_strong"] == 0.85
    assert payload["likelihood_information_quality"]["metar_strong"]["quality"] == 1.0
    assert payload["forecast_group_pooling"]["schema"] == "forecast_group_precision_pool_v1"
    assert set(PHASE0_FACT_JSON_CONTRACT_KEYS).issubset(payload.keys())
    assert payload["phase_reason"] != "phase_not_materialized_for_this_writer_path"
    assert payload["signal_to_order_timing"]["schema"] == "signal_to_order_timing_placeholder_v1"
    assert payload["daily_review_state"]["schema"] == "daily_review_state_placeholder_v1"
    assert payload["posterior_temperature_distribution"]
    assert payload["distribution"] == payload["posterior_temperature_distribution"]
    assert payload["posterior_continuous_distribution"] == pipeline["posterior_continuous_distribution"]
    assert payload["posterior_bucket_cdf"] == pipeline["posterior_bucket_cdf"]
    assert payload["posterior_quantiles_c"] == pipeline["posterior_quantiles_c"]
    assert payload["posterior_quantile_grid"] == pipeline["posterior_quantile_grid"]
    expected_target_bucket, expected_target_probability = max(
        pipeline["posterior_temperature_distribution_map"].items(),
        key=lambda item: item[1],
    )
    posterior_ssot_fact = payload["posterior_ssot_fact"]
    assert posterior_ssot_fact["schema"] == "bayes_posterior_ssot_fact_v1"
    assert posterior_ssot_fact["state"] == "available"
    assert posterior_ssot_fact["production_model_family"] == "joint_weak_physical_strong"
    assert posterior_ssot_fact["production_model_version"] == "joint_weak_physical_strong_composer_v1"
    assert posterior_ssot_fact["production_model_ready"] is True
    assert posterior_ssot_fact["production_probability_policy"] == "unique_joint_weak_physical_strong_only_fail_closed"
    assert posterior_ssot_fact["legacy_model_fallback_allowed"] is False
    assert posterior_ssot_fact["fact_snapshot_id"] == plan["fact_snapshot_id"]
    assert posterior_ssot_fact["posterior_run_id"] == plan["posterior_run_id"]
    assert posterior_ssot_fact["target_bucket"] == expected_target_bucket
    assert posterior_ssot_fact["target_probability"] == expected_target_probability
    assert posterior_ssot_fact["target_probability_field"] == "posterior_ssot_fact.target_probability"
    assert posterior_ssot_fact["phase"] == "D0_midday"
    weak_attribution = posterior_ssot_fact["forecast_weak_attribution"]
    assert weak_attribution["schema"] == "forecast_weak_attribution_trace_v1"
    assert weak_attribution["state"] == "available"
    assert weak_attribution["weak_likelihood_consumed"] is True
    assert weak_attribution["source_group_contributions"][0]["group"] == "model"
    assert weak_attribution["source_group_contributions"][0]["capped_precision_share_within_weak_likelihood"] == 1.0
    assert weak_attribution["pooling_decision"]["state"] == "available"
    assert weak_attribution["pooling_decision"]["legacy_model_taf_special_case_retired"] is True
    assert posterior_ssot_fact["layer_chain"]["formula"] == (
        "continuous_forecast_error_prior_then_sequential_state_zero_inflated_remaining_warming_mixture_then_bucket_integral"
    )


    assert posterior_ssot_fact["layer_chain"]["forecast_weak_likelihood_present"] is True
    assert posterior_ssot_fact["layer_chain"]["metar_strong_likelihood_present"] is True
    assert posterior_ssot_fact["lineage"]["source_event_ids"] == ["srcevt_model", "srcevt_awc"]
    assert posterior_ssot_fact["lineage"]["source_observation_ids"] == ["srcobs_model", "srcobs_awc"]
    assert posterior_ssot_fact["boundaries"]["frontend_recompute_allowed"] is False
    assert posterior_ssot_fact["boundaries"]["decision_recompute_allowed"] is False
    assert payload["posterior_target_bucket_selection"]["target_bucket"] == expected_target_bucket
    assert payload["posterior_target_bucket_selection"]["target_probability"] == expected_target_probability
    assert payload["posterior_target_bucket_selection"]["ssot_source"] == "posterior_ssot_fact"
    assert payload["target_bucket_selection"] == payload["posterior_target_bucket_selection"]
    assert payload["target_bucket"] == expected_target_bucket
    assert payload["target_bucket_probability_pct"] == expected_target_probability * 100.0
    assert payload["forecast_weak_likelihood"]
    assert payload["metar_strong_likelihood"]
    assert payload["state_probs"]
    assert payload["temperature_trajectory"]["schema"] == "temperature_trajectory_summary_v1"
    assert payload["temperature_trajectory"]["gate_state"] == "confirmed"
    assert payload["recoil_state_ssot"]["schema"] == "metar_recoil_three_state_ssot_v1"
    assert payload["recoil_state_ssot"]["boundary"] == "inside_metar_strong_likelihood_not_independent_likelihood"
    assert payload["physical_cutoff_evidence"]["schema"] == "metar_recoil_physical_cutoff_evidence_v1"
    assert payload["weather_likelihood_features"]["recoil_state_ssot"]["schema"] == "metar_recoil_three_state_ssot_v1"
    assert payload["weather_likelihood_features"]["physical_cutoff_evidence"]["schema"] == (
        "metar_recoil_physical_cutoff_evidence_v1"
    )
    assert payload["weather_likelihood_features"]["observed_today_max_c"] == 28.0
    assert payload["weather_likelihood_features"]["temperature_trajectory_gate_state"] == "confirmed"
    assert posterior_ssot_fact["observation_chain"]["temperature_trajectory_present"] is True
    assert posterior_ssot_fact["observation_chain"]["temperature_trajectory_gate_state"] == "confirmed"
    assert payload["predicted_peak_time_prior"]["role"] == "weak_forecast_timing_prior"
    assert payload["predicted_peak_time_prior"]["cannot_finalize_daily_peak"] is True
    assert payload["predicted_peak_time_posterior"]["predicted_not_observed"] is True
    assert payload["observed_peak_platform"]["observed_peak_to_date_c"] == 28.0
    assert posterior_ssot_fact["observation_chain"]["predicted_peak_time_prior"] == payload["predicted_peak_time_prior"]
    assert posterior_ssot_fact["observation_chain"]["predicted_peak_time_posterior"] == payload["predicted_peak_time_posterior"]
    assert posterior_ssot_fact["observation_chain"]["observed_peak_platform"] == payload["observed_peak_platform"]
    assert posterior_ssot_fact["observation_chain"]["predicted_peak_window_cannot_finalize_daily_peak"] is True


def test_posterior_ssot_fact_persists_joint_weak_expert_attribution():
    pipeline = build_hierarchical_posterior_pipeline(
        buckets=[f"{value}°C" for value in range(29, 37)],
        prior_distribution={f"{value}°C": 1.0 / 8.0 for value in range(29, 37)},
        event_date="2026-07-03",
        as_of="2026-07-03T12:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_model_conflict",
                "source_observation_id": "srcobs_model_conflict",
                "source_id": "current_model_ensemble",
                "source_class": "forecast_weak_source",
                "role": "model_forecast_weak_likelihood",
                "forecast_peak_c": 30.0,
                "sigma_c": 0.5,
                "confidence": 0.8,
            },
            {
                "source_event_id": "srcevt_taf_conflict",
                "source_observation_id": "srcobs_taf_conflict",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood_primary",
                "forecast_peak_c": 35.0,
                "sigma_c": 0.5,
                "confidence": 0.8,
            },
        ],
    )
    pipeline = _mark_production_model_ready(pipeline)
    weak_runtime = pipeline["runtime_outputs"]["forecast_weak_likelihood"]
    weak_runtime["forecast_weak_likelihood_map"] = dict(
        pipeline["layer_contributions"]["forecast_weak_likelihood"]["distribution"]
    )
    weak_runtime["source_expert_trace"] = [
        {
            "source_id": "gfs",
            "source_revision_id": "srcevt_model_conflict",
            "provider_family": "official_nwp",
            "forecast_peak_c": 30.0,
            "calibration_bias_c": 0.0,
            "calibrated_mean_c": 30.0,
            "calibrated_sigma_c": 1.2,
            "information_weight": 0.6,
        },
        {
            "source_id": "taf",
            "source_revision_id": "srcevt_taf_conflict",
            "provider_family": "taf",
            "forecast_peak_c": 35.0,
            "calibration_bias_c": 0.0,
            "calibrated_mean_c": 35.0,
            "calibrated_sigma_c": 1.2,
            "information_weight": 0.4,
        },
    ]
    weak_runtime["source_trace"] = [
        {
            "source_event_id": "srcevt_model_conflict",
            "source_observation_id": "srcobs_model_conflict",
            "source_id": "gfs",
            "source_class": "forecast_weak_source",
            "role": "joint_forecast_weak_likelihood_expert",
            "included": True,
        },
        {
            "source_event_id": "srcevt_taf_conflict",
            "source_observation_id": "srcobs_taf_conflict",
            "source_id": "taf",
            "source_class": "forecast_weak_source",
            "role": "joint_forecast_weak_likelihood_expert",
            "included": True,
        },
    ]
    payload = build_hierarchical_posterior_fact_payload(
        pipeline_output=pipeline,
        fact_snapshot_id="fact_conflict",
        posterior_run_id="post_conflict",
    )

    attribution = payload["posterior_ssot_fact"]["forecast_weak_attribution"]
    assert {row["group"] for row in attribution["source_group_contributions"]} == {"official_nwp", "taf"}
    assert {row["source_event_id"] for row in attribution["source_contribution_trace"] if row["included"]} == {
        "srcevt_model_conflict",
        "srcevt_taf_conflict",
    }
    assert attribution["forecast_group_pooling"] == {}
    assert attribution["pooling_decision"]["state"] == "joint_weak_available"
    assert attribution["pooling_decision"]["pooling_mode"] == "correlation_aware_tempered_mixture_of_calibrated_experts"
    assert attribution["boundaries"]["does_not_recompute_or_change_posterior"] is True


def test_hierarchical_posterior_fact_plan_uses_explicit_source_trace_and_preserves_hard_latest_policy():
    pipeline = _posterior_pipeline_output(hard_source=False)
    plan = build_hierarchical_posterior_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_as_of="2026-07-03T04:06:00Z",
        pipeline_output=pipeline,
        source_acquisition_trace={
            "schema": "weather_source_acquisition_trace_v1",
            "source_observations": [
                {
                    "source_event_id": "srcevt_model",
                    "source_observation_id": "srcobs_model",
                    "source_id": "current_model_ensemble",
                    "source_class": "forecast_weak_source",
                    "hard_eligible": False,
                }
            ],
            "source_latency_trace": [],
            "source_observation_ids": ["srcobs_model"],
            "forecast_weak_ids": ["srcevt_model"],
        },
        current_fact_id=101,
        current_fact_as_of="2026-07-03T04:05:00Z",
        current_payload={
            "hard_source_direct_ssot_write": True,
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        include_payload=True,
    )

    actions = {item["action"]: item for item in plan["plan_actions"]}
    assert actions["repoint_event_fact_latest"]["would_write"] is False
    assert actions["repoint_event_fact_latest"]["block_reason"] == "incoming_weaker_than_existing_hard_fact"
    assert plan["source_acquisition_trace_summary"]["schema"] == "weather_source_acquisition_trace_v1"
    assert plan["effective_payload"]["observed_today_max_c"] == 30.0
    assert plan["effective_payload"]["observed_today_max_source"] == "zuuu_metar_raw_awc"


def test_hierarchical_posterior_fact_payload_preserves_pipeline_outputs_without_recomputing():
    pipeline = _posterior_pipeline_output()
    payload = build_hierarchical_posterior_fact_payload(
        pipeline_output=pipeline,
        fact_snapshot_id="fact_1234567890abcdef",
        posterior_run_id="post_1234567890abcdef",
        extra_payload={"ui_refresh_reason": "posterior_update", "phase": "should_not_override"},
    )

    assert payload["fact_snapshot_id"] == "fact_1234567890abcdef"
    assert payload["posterior_run_id"] == "post_1234567890abcdef"
    assert payload["posterior_temperature_distribution_map"] == pipeline["posterior_temperature_distribution_map"]
    assert payload["posterior_bucket_cdf"] == pipeline["posterior_bucket_cdf"]
    assert payload["posterior_quantile_grid"] == pipeline["posterior_quantile_grid"]
    assert payload["phase"] == pipeline["phase"]
    assert payload["temperature_trajectory"]["schema"] == "temperature_trajectory_summary_v1"
    assert payload["posterior_ssot_fact"]["observation_chain"]["temperature_trajectory_present"] is True
    assert payload["posterior_ssot_fact"]["target_bucket"] == max(
        pipeline["posterior_temperature_distribution_map"].items(),
        key=lambda item: item[1],
    )[0]
    assert payload["posterior_target_bucket_selection"]["target_bucket"] == payload["posterior_ssot_fact"]["target_bucket"]
    assert payload["ui_refresh_reason"] == "posterior_update"
    assert payload["extra_payload_preserved"]["phase"] == "should_not_override"


def test_hierarchical_posterior_fact_payload_does_not_recursively_preserve_old_conflicts():
    pipeline = _posterior_pipeline_output()
    payload = build_hierarchical_posterior_fact_payload(
        pipeline_output=pipeline,
        fact_snapshot_id="fact_no_recursive_preserve",
        posterior_run_id="post_no_recursive_preserve",
        extra_payload={
            "extra_payload_preserved": {
                "phase": "older",
                "extra_payload_preserved": {"phase": "oldest"},
            },
            "phase": "previous_phase",
            "posterior_ssot_fact": {"legacy_blob": "x" * 10000},
        },
    )

    preserved = payload["extra_payload_preserved"]
    assert preserved["phase"] == "previous_phase"
    assert "extra_payload_preserved" not in preserved
    assert preserved["posterior_ssot_fact"]["schema"] == "extra_payload_conflict_digest_v1"
    assert preserved["posterior_ssot_fact"]["omitted"] is True
    assert preserved["posterior_ssot_fact"]["original_size_bytes"] > 4096
    assert "legacy_blob" not in json.dumps(preserved, ensure_ascii=False)


def test_hierarchical_posterior_fact_payload_preserves_midday_recent_confirmation_gate():
    buckets = ["35°C", "36°C", "37°C", "38°C", "39°C", "40°C", "41°C", "42°C"]
    pipeline = build_hierarchical_posterior_pipeline(
        buckets=buckets,
        prior_distribution={bucket: 1.0 / len(buckets) for bucket in buckets},
        event_date="2026-07-07",
        as_of="2026-07-07T13:00:00+08:00",
        observation_inputs={
            "current_temp_c": 35.0,
            "observed_today_max_c": 35.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW020",
            "temperature_momentum_6h_c_per_hour": 0.5,
            "temperature_momentum_3h_c_per_hour": 1.6666666667,
            "temperature_momentum_2h_c_per_hour": 2.0,
            "temperature_momentum_1h_c_per_hour": 2.0,
        },
        source_observations=[
            {
                "source_event_id": "srcevt_midday_recent_gate",
                "source_observation_id": "srcobs_midday_recent_gate",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "role": "metar_hard_observation",
                "temperature_c": 35.0,
                "observed_today_max_c": 35.0,
                "valid_time": "2026-07-07T04:30:00Z",
                "fetched_at": "2026-07-07T04:35:00Z",
                "payload": {
                    "dewpoint_c": 22.0,
                    "visibility_m": 9999,
                    "cloud_text": "FEW020",
                    "temperature_momentum_6h_c_per_hour": 0.5,
                    "temperature_momentum_3h_c_per_hour": 1.6666666667,
                    "temperature_momentum_2h_c_per_hour": 2.0,
                    "temperature_momentum_1h_c_per_hour": 2.0,
                },
            }
        ],
    )
    pipeline = _mark_production_model_ready(pipeline)
    payload = build_hierarchical_posterior_fact_payload(
        pipeline_output=pipeline,
        fact_snapshot_id="fact_midday_recent_gate",
        posterior_run_id="post_midday_recent_gate",
    )

    assert payload["temperature_trajectory"]["state"] == "midday_recent_confirmation_missing"
    assert payload["temperature_trajectory"]["gate_state"] == "recent_window_watch"
    assert payload["temperature_trajectory"]["gate_reason"] == "peak_window_requires_30m_recent_confirmation"
    assert payload["weather_likelihood_features"]["temperature_trajectory_state"] == "midday_recent_confirmation_missing"
    assert payload["weather_likelihood_features"]["temperature_trajectory_gate_state"] == "recent_window_watch"
    assert payload["posterior_ssot_fact"]["observation_chain"]["temperature_trajectory_present"] is True
    assert payload["posterior_ssot_fact"]["observation_chain"]["temperature_trajectory_state"] == "midday_recent_confirmation_missing"
    assert payload["metar_strong_likelihood"]["40°C"] < payload["metar_strong_likelihood"]["38°C"]


def test_fact_payload_persists_compact_daily_high_shadow_candidate_without_changing_production_strong_map():
    buckets = ["34°C", "35°C", "36°C", "37°C", "38°C", "39°C or higher"]
    sources = [
        {
            "source_event_id": "srcevt_amsc_0200",
            "source_observation_id": "srcobs_amsc_0200",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "station_id": "ZUUU",
            "temperature_c": 35.0,
            "valid_time": "2026-07-05T02:00:00Z",
            "fetched_at": "2026-07-05T02:01:00Z",
            "parsed_at": "2026-07-05T02:01:00Z",
            "raw_sha256": "sha0200",
            "raw_object_ref": "objects/aa/sha0200.json.gz",
            "payload": {
                "raw_text": "METAR ZUUU 050200Z 00000MPS CAVOK 35/20 Q1000=",
                "first_seen_at": "2026-07-05T02:01:00Z",
                "arrival_time_quality": "observed_first_seen",
            },
        },
        {
            "source_event_id": "srcevt_amsc_0230",
            "source_observation_id": "srcobs_amsc_0230",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "station_id": "ZUUU",
            "temperature_c": 36.0,
            "valid_time": "2026-07-05T02:30:00Z",
            "fetched_at": "2026-07-05T02:31:00Z",
            "parsed_at": "2026-07-05T02:31:00Z",
            "raw_sha256": "sha0230",
            "raw_object_ref": "objects/aa/sha0230.json.gz",
            "payload": {
                "raw_text": "METAR ZUUU 050230Z 00000MPS CAVOK 36/20 Q1000=",
                "first_seen_at": "2026-07-05T02:31:00Z",
                "arrival_time_quality": "observed_first_seen",
            },
        },
    ]
    pipeline = build_hierarchical_posterior_pipeline(
        buckets=buckets,
        prior_distribution={bucket: 1.0 / len(buckets) for bucket in buckets},
        event_date="2026-07-05",
        as_of="2026-07-05T03:05:00Z",
        source_observations=sources,
    )
    pipeline = _mark_production_model_ready(pipeline)
    production_map = dict(pipeline["runtime_outputs"]["metar_strong_likelihood"]["metar_strong_likelihood_map"])
    payload = build_hierarchical_posterior_fact_payload(pipeline_output=pipeline)

    candidate = payload["daily_high_observation_candidate"]
    assert candidate["state"] == "available"
    assert candidate["candidate_only"] is True
    assert candidate["production_applied"] is False
    assert candidate["metar_strong_likelihood_candidate"]["likelihood_map"]
    assert "METAR ZUUU" not in json.dumps(candidate)
    assert payload["metar_strong_likelihood"] == production_map
    rollout = payload["daily_high_candidate_live_rollout"]
    assert rollout["schema"] == "zuuu_daily_high_candidate_live_rollout_fact_v1"
    assert rollout["applied_to_metar_strong_likelihood"] is True
    assert rollout["candidate_contract_ok"] is True
    assert "METAR ZUUU" not in json.dumps(rollout)
    assert payload["runtime_schemas"]["daily_high_observation_candidate"] == "zuuu_daily_high_observation_candidate_shadow_fact_v1"
    assert payload["runtime_schemas"]["daily_high_candidate_live_rollout"] == "zuuu_daily_high_candidate_live_rollout_fact_v1"
    assert payload["runtime_policy_versions"]["daily_high_observation_candidate"]


def test_fact_snapshot_apply_contract_writes_phase0_fact_json_contract(tmp_path):
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.row_factory = sqlite3.Row
    _create_fact_tables(conn)
    plan = build_fact_snapshot_write_plan(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        run_date="2026-07-03",
        fact_as_of="2026-07-03T09:05:40Z",
        incoming_payload={
            "distribution": [{"outcome": "30", "q_temperature_bucket_posterior": 0.8}],
            **_prior_fields({"30": 1.0}),
        },
        source_acquisition_trace=_source_trace(),
        include_payload=True,
        enable_apply_contract=True,
    )

    result = apply_fact_snapshot_write_plan(conn, plan, allow_apply=True)

    payload = json.loads(
        conn.execute(
            "SELECT fact_json FROM event_fact_history WHERE id = ?",
            (result["fact_id"],),
        ).fetchone()["fact_json"]
    )
    assert result["ok"] is True
    assert set(PHASE0_FACT_JSON_CONTRACT_KEYS).issubset(payload.keys())
    assert payload["phase0_fact_json_contract"]["historical_backfill"] is False
    assert payload["lineage_state"]["fact_snapshot_id"] == plan["fact_snapshot_id"]
    assert payload["signal_to_order_timing"]["fact_snapshot_id"] == plan["fact_snapshot_id"]
    assert payload["daily_review_state"]["fact_snapshot_id"] == plan["fact_snapshot_id"]
    assert payload["posterior_run_id"] == plan["posterior_run_id"]
    assert payload["posterior_ssot_fact"]["schema"] == "bayes_posterior_ssot_fact_v1"
    assert payload["posterior_ssot_fact"]["state"] == "available"
    assert payload["posterior_ssot_fact"]["target_bucket"] == "30"
    assert payload["posterior_ssot_fact"]["target_probability"] == 1.0
    assert payload["source_observation_ids"] == ["srcobs_1234567890abcdef"]
    assert payload["lineage_state"]["source_observation_ids"] == ["srcobs_1234567890abcdef"]
    assert payload["posterior_ssot_fact"]["lineage"]["source_observation_ids"] == ["srcobs_1234567890abcdef"]
    conn.close()
