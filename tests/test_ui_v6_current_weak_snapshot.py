from __future__ import annotations

import json
import sqlite3
import time

from ui import server


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_v6_ui_uses_current_weak_snapshot_but_keeps_p0_prior() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE production_initial_posterior_lineage(
            target_identity TEXT,
            posterior_checkpoint_id TEXT,
            prior_context_id TEXT,
            prior_artifact_sha256 TEXT,
            forecast_evidence_snapshot_id TEXT,
            weak_likelihood_sha256 TEXT,
            created_at TEXT
        );
        CREATE TABLE posterior_checkpoints(
            posterior_checkpoint_id TEXT PRIMARY KEY,
            joint_posterior_json TEXT,
            forecast_evidence_snapshot_id TEXT
        );
        CREATE TABLE weak_forecast_evidence_snapshots(
            forecast_evidence_snapshot_id TEXT PRIMARY KEY,
            weak_likelihood_sha256 TEXT,
            likelihood_by_x_json TEXT,
            source_set_key TEXT,
            source_revision_ids_json TEXT,
            created_at TEXT,
            source_revisions_json TEXT,
            forecast_peak_timing_prior_json TEXT,
            forecast_background_context_json TEXT,
            source_expert_trace_json TEXT,
            forecast_multimodality_trace_json TEXT,
            weak_model_diagnostics_json TEXT,
            prior_context_audit_json TEXT
        );
        """
    )
    target = "physical_weather_day_v1:ZUUU:2099-07-17"
    conn.execute(
        "INSERT INTO posterior_checkpoints VALUES(?,?,?)",
        (
            "p0",
            _json(
                [
                    {"continuous_y_c": 30.0, "state": "warming", "probability": 1.0 / 3.0},
                    {"continuous_y_c": 31.0, "state": "warming", "probability": 2.0 / 3.0},
                ]
            ),
            "weak-old",
        ),
    )
    conn.execute(
        "INSERT INTO production_initial_posterior_lineage VALUES(?,?,?,?,?,?,?)",
        (target, "p0", "prior-1", "a" * 64, "weak-old", "old-sha", "2099-07-16T00:00:00Z"),
    )
    columns = (
        "forecast_evidence_snapshot_id,weak_likelihood_sha256,likelihood_by_x_json,"
        "source_set_key,source_revision_ids_json,created_at,source_revisions_json,"
        "forecast_peak_timing_prior_json,forecast_background_context_json,source_expert_trace_json,"
        "forecast_multimodality_trace_json,weak_model_diagnostics_json,prior_context_audit_json"
    )
    conn.execute(
        f"INSERT INTO weak_forecast_evidence_snapshots({columns}) VALUES({','.join('?' for _ in range(13))})",
        (
            "weak-old", "old-sha", _json({"30": 1.0, "31": 2.0}), "gfs+taf",
            _json(["old-gfs", "old-taf"]), "2099-07-16T00:00:00Z", "[]", "{}", "{}", "[]", "{}", "{}", "{}",
        ),
    )
    current_revisions = [
        {"source_id": "gfs", "source_revision_id": "new-gfs", "forecast_peak_c": 30.0},
        {"source_id": "wu", "source_revision_id": "new-wu", "forecast_peak_c": 31.0},
    ]
    conn.execute(
        f"INSERT INTO weak_forecast_evidence_snapshots({columns}) VALUES({','.join('?' for _ in range(13))})",
        (
            "weak-current", "current-sha", _json({"30": 3.0, "31": 1.0}), "gfs+wu",
            _json(["new-gfs", "new-wu"]), "2099-07-17T01:00:00Z", _json(current_revisions),
            _json({"state": "available"}), _json({"state": "available"}), "[]",
            _json({"multimodality_state": "unimodal"}), _json({"formula": "weak-v6"}), "{}",
        ),
    )
    payload = {
        "posterior_ssot_fact": {
            "posterior_checkpoint_id": "weak-checkpoint",
            "forecast_evidence_snapshot_id": "weak-current",
            "probability_runtime_identity": {"target_identity": target},
        }
    }

    server._attach_v6_frozen_probability_layers_for_ui_snapshot(
        conn,
        payload,
        event_date="2099-07-17",
    )

    assert payload["forecast_weak_attribution"]["forecast_evidence_snapshot_id"] == "weak-current"
    assert payload["forecast_weak_attribution"]["source_set_key"] == "gfs+wu"
    assert payload["forecast_weak_attribution"]["source_revisions"] == current_revisions
    assert payload["forecast_weak_likelihood"] == {"30°C": 3.0, "31°C": 1.0}
    assert abs(payload["prior_distribution"]["30°C"] - 0.5) < 1e-12
    assert abs(payload["prior_distribution"]["31°C"] - 0.5) < 1e-12
    assert payload["prior_meta"]["p0_checkpoint_id"] == "p0"


def test_ui_cache_rejects_repointed_event_fact_latest(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE event_fact_latest(slug TEXT,event_date TEXT,fact_id INTEGER,PRIMARY KEY(slug,event_date))"
    )
    conn.execute("INSERT INTO event_fact_latest VALUES('chengdu','2099-07-17',2)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_DIRTY_STAMP_PATH", tmp_path / "missing.stamp")
    stale = (
        time.time(),
        {
            "cache_fact_pointer_guard": True,
            "facts": {
                "items": [
                    {"slug": "chengdu", "event_date": "2099-07-17", "fact_id": 1}
                ]
            }
        },
    )
    current = (
        time.time(),
        {
            "cache_fact_pointer_guard": True,
            "facts": {
                "items": [
                    {"slug": "chengdu", "event_date": "2099-07-17", "fact_id": 2}
                ]
            }
        },
    )

    assert server._ui_snapshot_cache_is_dirty(stale) is True
    assert server._ui_snapshot_cache_is_dirty(current) is False
