import json
import sqlite3
from pathlib import Path

from scripts import posterior_ssot_fact_coverage_audit as audit


def _available_ssot_payload(target_bucket: str = "29°C or below") -> dict:
    secondary_bucket = "34°C" if target_bucket == "33°C" else "33°C"
    bucket_rows = [
        {"bucket": target_bucket, "probability": 0.6},
        {"bucket": secondary_bucket, "probability": 0.4},
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
        "posterior_temperature_distribution": list(bucket_rows),
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_abc",
            "posterior_run_id": "post_abc",
            "production_model_family": "joint_weak_physical_strong",
            "production_model_version": "joint_weak_physical_strong_composer_v1",
            "production_model_ready": True,
            "production_probability_policy": "unique_joint_weak_physical_strong_only_fail_closed",
            "legacy_model_fallback_allowed": False,
            "target_bucket": target_bucket,
            "target_probability": 0.6,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "boundaries": {
                "frontend_recompute_allowed": False,
                "decision_recompute_allowed": False,
                "hard_mask_is_constraint_not_weight": True,
            },
            "layer_chain": {"schema": "bayes_layer_chain_summary_v1"},
            "bucket_probabilities": list(bucket_rows),
            "ranked_bucket_probabilities": list(bucket_rows),
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
    }


def _create_fact_db(path: Path, payload: dict) -> None:
    conn = sqlite3.connect(path)
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
    conn.execute(
        """
        INSERT INTO event_fact_history(
            id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            payload["event_date"],
            payload["slug"],
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
        (
            payload["slug"],
            payload["event_date"],
            1,
            "2026-07-05T03:00:02+00:00",
        ),
    )
    conn.commit()
    conn.close()


def test_coverage_audit_flags_posterior_ssot_target_below_hard_floor() -> None:
    result = audit.audit_fact_payload(_available_ssot_payload())

    assert result["ok"] is False
    assert result["hard_temperature_floor_c"] == 31.0
    assert "posterior_ssot_fact_target_below_hard_temperature_floor" in result["issues"]
    assert "d0_hard_constraint_violation" in result["issues"]
    assert result["hard_constraint_violation_count"] > 0
    assert any(row["bucket"] == "29°C or below" for row in result["hard_constraint_violations"])


def test_coverage_audit_accepts_posterior_ssot_target_at_or_above_hard_floor() -> None:
    result = audit.audit_fact_payload(_available_ssot_payload(target_bucket="33°C"))

    assert result["ok"] is True
    assert result["hard_temperature_floor_c"] == 31.0
    assert result["hard_constraint_violation_count"] == 0
    assert result["issues"] == []


def test_coverage_audit_rejects_available_fact_without_production_identity() -> None:
    payload = _available_ssot_payload(target_bucket="33°C")
    for key in (
        "production_model_family",
        "production_model_version",
        "production_model_ready",
        "production_probability_policy",
        "legacy_model_fallback_allowed",
    ):
        payload["posterior_ssot_fact"].pop(key, None)

    result = audit.audit_fact_payload(payload)

    assert result["ok"] is False
    assert "posterior_ssot_fact_production_model_family_not_production" in result["issues"]
    assert "posterior_ssot_fact_production_model_version_not_production" in result["issues"]
    assert "posterior_ssot_fact_production_probability_policy_invalid" in result["issues"]
    assert "posterior_ssot_fact_legacy_model_fallback_not_false" in result["issues"]


def test_coverage_audit_flags_any_distribution_probability_below_hard_floor() -> None:
    payload = _available_ssot_payload(target_bucket="33°C")
    payload["posterior_temperature_distribution"] = [
        {"bucket": "30°C", "probability": 0.01},
        {"bucket": "33°C", "probability": 0.59},
        {"bucket": "34°C", "probability": 0.40},
    ]
    payload["distribution"] = [
        {"outcome": "30°C", "q_temperature_bucket_posterior": 0.01},
        {"outcome": "33°C", "q_temperature_bucket_posterior": 0.59},
        {"outcome": "34°C", "q_temperature_bucket_posterior": 0.40},
    ]

    result = audit.audit_fact_payload(payload)

    assert result["ok"] is False
    assert "d0_hard_constraint_violation" in result["issues"]
    assert result["hard_constraint_violation_count"] >= 2
    assert {
        row["path"]
        for row in result["hard_constraint_violations"]
        if row["bucket"] == "30°C"
    } >= {"posterior_temperature_distribution[0]", "distribution[0]"}


def test_coverage_audit_requires_phase_beta_and_hard_mask_fields() -> None:
    payload = _available_ssot_payload(target_bucket="33°C")
    for key in ("phase", "phase_reason", "beta_weak", "beta_strong", "hard_mask_reason"):
        payload.pop(key, None)
    payload["posterior_ssot_fact"].pop("phase", None)
    payload["posterior_ssot_fact"].pop("phase_reason", None)
    payload["posterior_ssot_fact"].pop("beta_weak", None)
    payload["posterior_ssot_fact"].pop("beta_strong", None)
    payload["posterior_ssot_fact"].pop("hard_mask_reason", None)

    result = audit.audit_fact_payload(payload)

    assert result["ok"] is False
    assert "phase_missing" in result["issues"]
    assert "phase_reason_missing" in result["issues"]
    assert "beta_weak_missing" in result["issues"]
    assert "beta_strong_missing" in result["issues"]
    assert "hard_mask_reason_missing" in result["issues"]


def test_coverage_audit_handles_not_available_posterior_ssot_without_surface_mismatch_crash() -> None:
    payload = _available_ssot_payload(target_bucket="33°C")
    payload["posterior_ssot_fact"]["state"] = "not_materialized"

    result = audit.audit_fact_payload(payload)

    assert result["ok"] is False
    assert result["posterior_surface_probability_mismatches"] == []
    assert "posterior_ssot_fact_not_available" in result["issues"]
    assert "posterior_ssot_fact_not_materialized" in result["issues"]


def test_coverage_audit_flags_top_level_target_mismatch_with_posterior_ssot() -> None:
    payload = _available_ssot_payload(target_bucket="33°C")
    payload["target_bucket"] = "29°C or below"
    payload["ssot_target_bucket"] = "29°C or below"
    payload["target_bucket_source"] = "posterior_ssot_fact.top_bucket"
    payload["target_bucket_source_semantics"] = "bayes_posterior_ssot_fact_v1"
    payload["target_bucket_probability_field"] = "posterior_ssot_fact.target_probability"
    payload["target_bucket_probability"] = 0.6

    result = audit.audit_fact_payload(payload)

    assert result["ok"] is False
    assert "target_bucket_mismatch_posterior_ssot_fact" in result["issues"]
    assert "ssot_target_bucket_mismatch_posterior_ssot_fact" in result["issues"]


def test_coverage_audit_flags_distribution_tail_not_aligned_to_posterior_ssot_fact() -> None:
    payload = _available_ssot_payload(target_bucket="37°C")
    payload["weather_likelihood_features"] = {
        "observed_today_max_c": 37.0,
        "observed_today_max_source": "zuuu_metar_raw_awc",
        "observed_today_max_final": True,
    }
    payload["hard_mask_reason"] = "final_bucket_lock"
    payload["posterior_ssot_fact"]["target_bucket"] = "37°C"
    payload["posterior_ssot_fact"]["target_probability"] = 1.0
    payload["posterior_ssot_fact"]["hard_mask_reason"] = "final_bucket_lock"
    payload["posterior_ssot_fact"]["final_bucket_lock"] = True
    payload["posterior_ssot_fact"]["bucket_probabilities"] = [
        {"bucket": "37°C", "probability": 1.0, "final_bucket_lock_target": True},
        {"bucket": "38°C", "probability": 0.0, "hard_zero": True},
        {"bucket": "39°C or higher", "probability": 0.0, "hard_zero": True},
    ]
    payload["posterior_ssot_fact"]["ranked_bucket_probabilities"] = [
        {"bucket": "37°C", "probability": 1.0, "final_bucket_lock_target": True},
    ]
    for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
        payload[key]["target_bucket"] = "37°C"
        payload[key]["target_probability"] = 1.0
    payload["posterior_temperature_distribution"] = {
        "bucket_probabilities": [
            {"bucket": "37°C", "probability": 0.027126122616945747},
            {"bucket": "38°C", "probability": 0.017903508455964195},
            {"bucket": "39°C or higher", "probability": 0.01022537415197955},
        ]
    }
    payload["posterior_temperature_distribution_map"] = {
        "37°C": 0.027126122616945747,
        "38°C": 0.017903508455964195,
        "39°C or higher": 0.01022537415197955,
    }
    payload["distribution"] = [
        {"outcome": "37°C", "q_temperature_bucket_posterior": 0.027126122616945747},
        {"outcome": "38°C", "q_temperature_bucket_posterior": 0.017903508455964195},
        {"outcome": "39°C or higher", "q_temperature_bucket_posterior": 0.01022537415197955},
    ]

    result = audit.audit_fact_payload(payload)

    assert result["ok"] is False
    assert "posterior_surface_probability_mismatch_posterior_ssot_fact" in result["issues"]
    assert result["hard_constraint_violation_count"] == 0
    mismatches = result["posterior_surface_probability_mismatches"]
    assert any(
        row["bucket"] == "38°C"
        and row["surface_probability"] == 0.017903508455964195
        and row["posterior_ssot_probability"] == 0.0
        for row in mismatches
    )


def test_coverage_audit_project_forward_separates_persisted_debt_from_forward_writer(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    _create_fact_db(db_path, _available_ssot_payload())

    persisted = audit.audit(db_path, since_hours=0, limit=1)
    projected = audit.audit(db_path, since_hours=0, limit=1, project_forward=True, latest_only=True)

    assert persisted["ok"] is False
    assert persisted["blockers"] == ["posterior_ssot_fact_coverage_incomplete"]
    assert projected["ok"] is True
    assert projected["coverage_mode"] == "forward_writer_projection_no_apply"
    assert projected["scope"]["latest_only"] is True
    item = projected["items"][0]
    assert item["target_bucket"] == "33°C"
    assert item["projection_write_latest"] is False
    assert item["projection_apply_supported"] is False
    assert "posterior_ssot_fact_target_below_hard_temperature_floor" in item["persisted_issues"]
    assert "d0_hard_constraint_violation" in item["persisted_issues"]
    assert projected["coverage"]["hard_constraint_violation_count"] == 0


def test_coverage_audit_cli_summary_matches_documented_command(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "archive.db"
    _create_fact_db(db_path, _available_ssot_payload(target_bucket="33°C"))

    assert audit.main(
        [
            "--db",
            str(db_path),
            "--since-hours",
            "0",
            "--latest-only",
            "--project-forward",
            "--limit",
            "20",
            "--max-items-output",
            "8",
            "--summary",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "posterior_ssot_fact_coverage_audit_v1_summary"
    assert payload["ok"] is True
    assert payload["coverage_mode"] == "forward_writer_projection_no_apply"
    assert payload["coverage"]["hard_constraint_violation_count"] == 0
    assert "items" not in payload
