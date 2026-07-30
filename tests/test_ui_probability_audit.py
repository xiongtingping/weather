from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ui import server


ROOT = Path(__file__).resolve().parents[1]


def test_probability_audit_frontend_renders_only_snapshot_contract() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    block = html[
        html.index("function renderProbabilityAuditDetails(audit)") :
        html.index("function renderUiSnapshotSsotChainBoard(snapshot)")
    ]
    assert "audit.weak_model" in block
    assert "audit.forecast_regime" in block
    assert "audit.peak_window" in block
    assert "audit.observations" in block
    assert "audit.posterior_history" in block
    assert "observation_bucket_evidence_rows" in block
    assert "温度动量 logLR" in block
    assert "物理字段 logLR" in block
    assert "时段调节 logLR" in block
    assert "硬约束保留" in block
    assert "基础权重" in block
    assert "家族限权后" in block
    assert "归一化占比" in block
    assert "相关性折扣" in block
    assert "最终信息指数" in block
    assert "local_responsibility_by_source" in block
    assert "弱似然使用历史冻结快照" in block
    assert "不是来源当前预报值" in block
    assert "fetch(" not in block
    assert "Math.exp" not in block


def test_probability_audit_exposes_persisted_bucket_component_trace() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE source_observations(
          source_id TEXT,source_event_id TEXT,source_observation_id TEXT,
          source_class TEXT,event_date TEXT,forecast_peak_c REAL,valid_time TEXT,
          fetched_at TEXT,response_completed_at TEXT,created_at TEXT,observation_json TEXT,raw_ref TEXT
        );
        CREATE TABLE canonical_observation_events(
          canonical_observation_event_id TEXT,report_type TEXT,valid_time TEXT,accepted_at TEXT,
          selected_source_id TEXT,selected_source_observation_id TEXT,parsed_observation_json TEXT,
          quality_flags_json TEXT,target_identity TEXT,state TEXT,revision_no INTEGER
        );
        CREATE TABLE posterior_checkpoints(
          posterior_checkpoint_id TEXT,posterior_run_id TEXT,checkpoint_kind TEXT,
          last_canonical_event_id TEXT,last_strong_update_id TEXT,
          reported_physical_x_distribution_json TEXT,latent_post_state_json TEXT,
          reported_x_lower_bound_c REAL,committed_at TEXT,target_identity TEXT,state TEXT
        );
        CREATE TABLE strong_observation_updates(
          strong_observation_update_id TEXT PRIMARY KEY,
          observation_emission_json TEXT
        );
        """
    )
    evidence_row = {
        "reported_x_c": 34,
        "probability_before": 0.4,
        "hard_allowed_probability_before": 0.3,
        "hard_allowed_fraction": 0.75,
        "hard_zero": False,
        "temperature_momentum_log_lr": 0.12,
        "physical_fields_log_lr": -0.08,
        "temporal_adjustment_log_lr": 0.03,
        "combined_log_posterior_ratio": 0.07,
        "probability_after": 0.42,
        "probability_delta": 0.02,
        "probability_delta_pct_points": 2.0,
    }
    emission = {
        "observation_feature_trace": {"observation_phase": "pre_peak"},
        "bucket_observation_evidence": {
            "schema": "v6_observation_bucket_evidence_v1",
            "frontend_recompute_allowed": False,
            "reported_x_lower_bound_before_c": 33.0,
            "reported_x_lower_bound_after_c": 33.0,
            "rows": [evidence_row],
        },
    }
    target = "physical_weather_day_v1:ZUUU:2026-07-17"
    conn.execute(
        "INSERT INTO strong_observation_updates VALUES(?,?)",
        ("strong-1", json.dumps(emission)),
    )
    conn.execute(
        "INSERT INTO posterior_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "checkpoint-1", "run-1", "observation_update", "canonical-1", "strong-1",
            json.dumps({"34": 0.42, "35": 0.58}), json.dumps({"heating": 1.0}),
            33.0, "2026-07-17T03:00:45Z", target, "committed",
        ),
    )
    payload = {
        "fact_as_of": "2026-07-17T03:00:45Z",
        "posterior_ssot_fact": {"committed_at": "2026-07-17T03:00:45Z"},
    }

    server._attach_v6_probability_audit_for_ui_snapshot(
        conn, payload, event_date="2026-07-17"
    )

    history = payload["probability_audit"]["posterior_history"]
    assert history[0]["observation_bucket_evidence_rows"] == [evidence_row]
    assert history[0]["observation_bucket_evidence"]["frontend_recompute_allowed"] is False
    assert history[0]["observation_bucket_evidence_na_reason"] == ""
    assert history[0]["observation_feature_trace"]["observation_phase"] == "pre_peak"


def test_probability_audit_marks_newer_taf_revision_unconsumed() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE source_observations(
          source_id TEXT,source_event_id TEXT,source_observation_id TEXT,
          source_class TEXT,event_date TEXT,forecast_peak_c REAL,valid_time TEXT,
          fetched_at TEXT,response_completed_at TEXT,created_at TEXT,observation_json TEXT,raw_ref TEXT
        );
        CREATE TABLE canonical_observation_events(
          canonical_observation_event_id TEXT,report_type TEXT,valid_time TEXT,accepted_at TEXT,
          selected_source_id TEXT,selected_source_observation_id TEXT,parsed_observation_json TEXT,
          quality_flags_json TEXT,target_identity TEXT,state TEXT,revision_no INTEGER
        );
        CREATE TABLE posterior_checkpoints(
          posterior_checkpoint_id TEXT,posterior_run_id TEXT,checkpoint_kind TEXT,
          last_canonical_event_id TEXT,last_strong_update_id TEXT,
          reported_physical_x_distribution_json TEXT,latent_post_state_json TEXT,
          reported_x_lower_bound_c REAL,committed_at TEXT,target_identity TEXT,state TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO source_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "zuuu_taf_forecast_amsc", "taf-new", "taf-new", "forecast_weak_source",
            "2026-07-17", 33.0, "2026-07-17T07:00:00Z", "2026-07-16T21:30:36Z",
            "2026-07-16T21:30:36Z", "2026-07-16T21:30:36Z", "{}", "",
        ),
    )
    payload = {
        "fact_as_of": "2026-07-17T03:00:45Z",
        "posterior_ssot_fact": {"committed_at": "2026-07-17T03:00:45Z"},
        "prior_meta": {"prior_context_id": "prior-1"},
        "forecast_weak_attribution": {
            "created_at": "2026-07-16T17:35:14Z",
            "source_revision_ids": ["taf-old"],
            "source_revisions": [
                {"source_id": "taf", "source_revision_id": "taf-old", "forecast_peak_c": 35.0}
            ],
            "source_expert_trace": [
                {
                    "source_id": "taf", "source_revision_id": "taf-old",
                    "forecast_peak_c": 35.0, "calibration_bias_c": 0.0,
                    "calibrated_mean_c": 35.0, "information_weight": 0.35,
                    "base_information_weight": 0.35,
                    "family_information_cap": 0.35,
                    "family_raw_information_weight": 0.35,
                    "family_cap_scale": 1.0,
                    "family_capped_information_weight": 0.35,
                    "normalized_information_share": 1.0,
                    "correlation_discount": 0.8,
                    "joint_information_scale": 0.5,
                    "final_information_index": 0.4,
                    "source_modes_schema": "forecast_source_modes_v1",
                    "source_mode_count": 1,
                    "source_modes": [{
                        "mode_id": "taf_tx_primary", "mode_probability": 1.0,
                        "forecast_peak_c": 35.0, "calibrated_mean_c": 35.0,
                    }],
                }
            ],
            "weak_model_diagnostics": {
                "source_weight_attribution": {
                    "schema": "weak_source_weight_attribution_v1",
                    "frontend_recompute_allowed": False,
                    "source_rows": [{"source_id": "taf", "final_information_index": 0.4}],
                    "bucket_rows": [
                        {
                            "reported_x_c": 35,
                            "weak_likelihood_mass": 1.0,
                            "local_responsibility_by_source": {"taf": 1.0},
                            "local_responsibility_by_mode": {"taf-old#taf_tx_primary": 1.0},
                            "dominant_source_id": "taf",
                        }
                    ],
                }
            },
        },
    }
    server._attach_v6_probability_audit_for_ui_snapshot(
        conn, payload, event_date="2026-07-17"
    )
    weak = payload["probability_audit"]["weak_model"]
    assert weak["snapshot_state"] == "stale_newer_revision_not_consumed"
    assert weak["newer_source_revisions"][0]["source_id"] == "taf"
    assert weak["newer_source_revisions"][0]["forecast_peak_c"] == 33.0
    assert weak["source_rows"][0]["forecast_peak_c"] == 35.0
    assert weak["source_rows"][0]["calibration_na_reason"] == ""
    assert weak["source_rows"][0]["base_information_weight"] == 0.35
    assert weak["source_rows"][0]["family_capped_information_weight"] == 0.35
    assert weak["source_rows"][0]["normalized_information_share"] == 1.0
    assert weak["source_rows"][0]["final_information_index"] == 0.4
    assert weak["source_rows"][0]["source_modes"][0]["mode_id"] == "taf_tx_primary"
    assert weak["source_weight_attribution"]["frontend_recompute_allowed"] is False
    assert weak["source_weight_attribution"]["bucket_rows"][0]["dominant_source_id"] == "taf"
    assert weak["source_weight_attribution"]["bucket_rows"][0]["local_responsibility_by_mode"] == {
        "taf-old#taf_tx_primary": 1.0
    }
