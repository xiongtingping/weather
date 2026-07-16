import json
import sqlite3
from pathlib import Path

from scripts import canonical_writer_persisted_coverage_audit as audit


def _payload(*, target_bucket: str = "33°C", strict_marker: bool = True) -> dict:
    phase0_contract = {
        "write_owner": "weather_fact_snapshot_writer",
        "historical_backfill": False,
        "missing_required_keys_after_merge_fill": [],
    }
    if strict_marker:
        phase0_contract["persisted_coverage_contract"] = {
            "schema": "fact_writer_persisted_coverage_contract_v1",
            "state": "enabled_for_future_writer_facts",
            "posterior_ssot_coverage_required": True,
            "phase_beta_hard_mask_required": True,
            "selection_must_be_posterior_ssot_fact": True,
            "historical_backfill": False,
        }
    rows = [
        {"bucket": target_bucket, "probability": 0.6},
        {"bucket": "34°C", "probability": 0.4},
    ]
    return {
        "slug": "highest-temperature-in-chengdu-on-july-5-2026",
        "event_date": "2026-07-05",
        "fact_snapshot_id": "fact_abc",
        "posterior_run_id": "post_abc",
        "phase": "D0_awc_hard_signal",
        "phase_reason": "unit_test",
        "beta_weak": 0.0,
        "beta_strong": 1.0,
        "hard_mask_reason": "observed_today_max_c",
        "weather_likelihood_features": {
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        "posterior_temperature_distribution": rows,
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_abc",
            "target_bucket": target_bucket,
            "target_probability": 0.6,
            "bucket_probabilities": rows,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "boundaries": {
                "frontend_recompute_allowed": False,
                "decision_recompute_allowed": False,
                "hard_mask_is_constraint_not_weight": True,
            },
            "layer_chain": {"schema": "bayes_layer_chain_summary_v1"},
        },
        "posterior_target_bucket_selection": {
            "ssot_source": "posterior_ssot_fact",
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "selection_runtime_recomputed": False,
            "target_bucket": target_bucket,
            "target_probability": 0.6,
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_abc",
        },
        "target_bucket_selection": {
            "ssot_source": "posterior_ssot_fact",
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "selection_runtime_recomputed": False,
            "target_bucket": target_bucket,
            "target_probability": 0.6,
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_abc",
        },
        "phase0_fact_json_contract": phase0_contract,
        "lineage_state": {"write_owner": "weather_fact_snapshot_writer"},
        "fact_layer_state": {"write_owner": "weather_fact_snapshot_writer"},
        "db_write_guard": {
            "write_owner": "weather_fact_snapshot_writer",
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_event_fact_summary_cache": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
        },
    }


def _create_fact_db(path: Path, payloads: list[dict]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY,
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
                slug TEXT,
                event_date TEXT,
                fact_id INTEGER,
                updated_at TEXT,
                PRIMARY KEY(slug, event_date)
            )
            """
        )
        for idx, payload in enumerate(payloads, start=1):
            slug = f"{payload['slug']}-{idx}"
            payload = dict(payload, slug=slug)
            conn.execute(
                """
                INSERT INTO event_fact_history(
                    id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idx,
                    payload["event_date"],
                    slug,
                    payload["event_date"],
                    "2026-07-05T03:00:00+00:00",
                    "unit_test",
                    json.dumps(payload, ensure_ascii=False),
                    "2026-07-05T03:00:01+00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (slug, payload["event_date"], idx, "2026-07-05T03:00:02+00:00"),
            )


def test_marked_canonical_writer_fact_must_pass_posterior_ssot_coverage(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_fact_db(db, [_payload(target_bucket="33°C", strict_marker=True)])

    report = audit.build_canonical_writer_persisted_coverage_audit(
        db_path=db,
        since_hours=0,
        latest_only=True,
        require_marked=True,
    )

    assert report["ok"] is True
    assert report["coverage"]["checked_fact_count"] == 1
    assert report["coverage"]["strict_marked_fact_count"] == 1
    assert report["coverage"]["strict_marked_issue_count"] == 0
    assert report["warnings"] == []


def test_marked_canonical_writer_fact_blocks_when_posterior_ssot_coverage_fails(
    tmp_path: Path,
) -> None:
    db = tmp_path / "archive.db"
    _create_fact_db(db, [_payload(target_bucket="29°C or below", strict_marker=True)])

    report = audit.build_canonical_writer_persisted_coverage_audit(
        db_path=db,
        since_hours=0,
        latest_only=True,
    )

    assert report["ok"] is False
    assert report["blockers"] == ["canonical_writer_marked_persisted_coverage_incomplete"]
    assert report["coverage"]["strict_marked_issue_count"] == 1
    assert report["issue_counts"]["posterior_ssot_fact_target_below_hard_temperature_floor"] == 1


def test_marked_canonical_writer_forward_projection_can_clear_persisted_gap(
    tmp_path: Path,
) -> None:
    db = tmp_path / "archive.db"
    _create_fact_db(db, [_payload(target_bucket="29°C or below", strict_marker=True)])

    report = audit.build_canonical_writer_persisted_coverage_audit(
        db_path=db,
        since_hours=0,
        latest_only=True,
        project_forward=True,
    )

    assert report["ok"] is True
    assert report["coverage_mode"] == "canonical_writer_forward_projection_no_apply"
    assert report["coverage"]["strict_marked_issue_count"] == 0
    assert report["items"][0]["issues"] == []
    assert "posterior_ssot_fact_target_below_hard_temperature_floor" in report["items"][0]["persisted_issues"]


def test_legacy_unmarked_canonical_writer_rows_warn_without_blocking_future_contract(
    tmp_path: Path,
) -> None:
    db = tmp_path / "archive.db"
    _create_fact_db(db, [_payload(target_bucket="29°C or below", strict_marker=False)])

    report = audit.build_canonical_writer_persisted_coverage_audit(
        db_path=db,
        since_hours=0,
        latest_only=True,
    )

    assert report["ok"] is True
    assert report["blockers"] == []
    assert report["coverage"]["strict_marked_fact_count"] == 0
    assert report["coverage"]["legacy_unmarked_canonical_count"] == 1
    assert report["coverage"]["legacy_unmarked_issue_count"] == 1
    assert "no_strict_persisted_coverage_marker_rows_in_scope" in report["warnings"]
    assert "legacy_canonical_writer_rows_without_persisted_coverage_marker_present" in report["warnings"]
    assert "legacy_canonical_writer_rows_have_non_blocking_coverage_issues" in report["warnings"]


def test_require_marked_blocks_when_scope_has_only_legacy_unmarked_rows(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_fact_db(db, [_payload(target_bucket="33°C", strict_marker=False)])

    report = audit.build_canonical_writer_persisted_coverage_audit(
        db_path=db,
        since_hours=0,
        latest_only=True,
        require_marked=True,
    )

    assert report["ok"] is False
    assert report["blockers"] == ["strict_persisted_coverage_marker_rows_missing"]
    assert report["coverage"]["legacy_unmarked_canonical_count"] == 1
