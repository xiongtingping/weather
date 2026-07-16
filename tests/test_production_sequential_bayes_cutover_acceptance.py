from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/production_sequential_bayes_cutover_acceptance.py"
spec = importlib.util.spec_from_file_location("production_sequential_bayes_cutover_acceptance", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def _db(path: Path, *, with_event: bool) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE event_fact_history(
              id INTEGER PRIMARY KEY,slug TEXT,event_date TEXT,fact_as_of TEXT,
              composer_version TEXT,fact_json TEXT,posterior_run_id TEXT
            );
            CREATE TABLE event_fact_latest(slug TEXT,event_date TEXT,fact_id INTEGER);
            CREATE TABLE posterior_checkpoints(posterior_checkpoint_id TEXT,target_identity TEXT);
            CREATE TABLE production_initial_posterior_lineage(
              posterior_checkpoint_id TEXT,target_identity TEXT,
              activation_contract_sha256 TEXT,created_at TEXT
            );
            CREATE TABLE canonical_observation_events(
              canonical_observation_event_id TEXT,first_actionable_seen_at TEXT,report_type TEXT
            );
            CREATE TABLE strong_observation_updates(
              strong_observation_update_id TEXT,canonical_observation_event_id TEXT,completed_at TEXT
            );
            """
        )
        event_id = "event-1" if with_event else ""
        ssot = {
            "schema": "sequential_temperature_posterior_ssot_v2",
            "state": "physical_only_market_projection_na",
            "writer_mode": "production",
            "checkpoint_kind": "incremental" if with_event else "initial",
            "posterior_checkpoint_id": "pchk-current",
            "physical_high_distribution": {"34": 0.3, "35": 0.7},
            "market_bucket_probabilities": {},
            "market_bucket_probabilities_na_reason": "x_to_s_to_b_bridge_unavailable",
            "last_canonical_event_id": event_id,
            "last_strong_update_id": "update-1" if with_event else "",
            "committed_at": "2026-07-14T06:00:02Z" if with_event else "2026-07-14T06:00:00Z",
        }
        if with_event:
            ssot["peak_timing_state"] = {
                "schema": "peak_timing_state_l3_v1",
                "ok": True,
                "state": "available",
                "canonical_observation_event_id": "event-1",
                "strong_observation_update_id": "update-1",
                "peak_timing_state_id": "peaktiming-current",
                "peak_already_probability": 0.4,
                "future_peak_probability": 0.6,
                "distribution": [
                    {
                        "time_state": "observed_peak_to_date",
                        "peak_at": "2026-07-14T05:30:00Z",
                        "probability": 0.4,
                    },
                    {
                        "time_state": "future_forecast_supported_slot",
                        "peak_at": "2026-07-14T07:00:00Z",
                        "probability": 0.6,
                    },
                ],
                "entry_peak_at": "2026-07-14T07:00:00Z",
                "entry_peak_at_na_reason": "",
                "does_not_update_physical_x": True,
            }
        conn.execute(
            "INSERT INTO event_fact_history VALUES(1,?,?,?,?,?,?)",
            (
                "highest-temperature-in-chengdu-on-july-14-2026",
                "2026-07-14",
                ssot["committed_at"],
                "sequential_bayes_v2_production",
                json.dumps({"posterior_ssot_fact": ssot}),
                "post-current",
            ),
        )
        conn.execute(
            "INSERT INTO event_fact_latest VALUES(?,?,1)",
            ("highest-temperature-in-chengdu-on-july-14-2026", "2026-07-14"),
        )
        conn.execute("INSERT INTO posterior_checkpoints VALUES('pchk-current','physical_weather_day_v1:ZUUU:2026-07-14')")
        conn.execute(
            "INSERT INTO production_initial_posterior_lineage VALUES('pchk-p0','physical_weather_day_v1:ZUUU:2026-07-14',?,'2026-07-14T05:00:00Z')",
            ("a" * 64,),
        )
        if with_event:
            conn.execute("INSERT INTO canonical_observation_events VALUES('event-1','2026-07-14T06:00:00Z','SPECI')")
            conn.execute("INSERT INTO strong_observation_updates VALUES('update-1','event-1','2026-07-14T06:00:01Z')")
    return path


def _patch_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "build_readiness",
        lambda _: {"production_cutover": {"ok": True, "blockers": []}},
    )
    monkeypatch.setattr(
        module,
        "load_production_probability_activation",
        lambda: {
            "activation_contract_sha256": "a" * 64,
            "activation_effective_at": "2026-07-14T05:30:00Z",
        },
    )


def test_acceptance_separates_p0_cutover_from_next_forward_slo(tmp_path, monkeypatch) -> None:
    _patch_runtime(monkeypatch)
    result = module.build_acceptance(_db(tmp_path / "p0.db", with_event=False), event_date="2026-07-14")

    assert result["ok"] is True
    assert result["state"] == "cutover_accepted_forward_slo_pending"
    assert result["latest_fact"]["physical_distribution_probability_sum"] == 1.0
    assert result["forward_three_second_slo"]["state"] == "pending_next_canonical_event"
    assert result["forward_three_second_slo"]["event_is_post_activation"] is False


def test_acceptance_proves_real_canonical_t1_to_ssot_t3_under_three_seconds(tmp_path, monkeypatch) -> None:
    _patch_runtime(monkeypatch)
    result = module.build_acceptance(_db(tmp_path / "forward.db", with_event=True), event_date="2026-07-14")

    assert result["ok"] is True
    assert result["state"] == "cutover_and_forward_slo_accepted"
    assert result["forward_three_second_slo"]["strong_observation_update_id"] == "update-1"
    assert result["forward_three_second_slo"]["t1_to_t3_ms"] == 2000.0
    assert result["forward_three_second_slo"]["event_is_post_activation"] is True
    assert result["latest_fact"]["canonical_report_type"] == "SPECI"
    assert result["latest_fact"]["peak_timing_state"]["state"] == "available"
    assert result["latest_fact"]["peak_timing_state"]["entry_peak_at"] == "2026-07-14T07:00:00Z"


def test_acceptance_rejects_peak_timing_not_bound_to_latest_strong_update(tmp_path, monkeypatch) -> None:
    _patch_runtime(monkeypatch)
    db = _db(tmp_path / "timing-drift.db", with_event=True)
    with sqlite3.connect(db) as conn:
        payload = json.loads(conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0])
        payload["posterior_ssot_fact"]["peak_timing_state"]["strong_observation_update_id"] = "update-old"
        conn.execute(
            "UPDATE event_fact_history SET fact_json=?",
            (json.dumps(payload),),
        )

    result = module.build_acceptance(db, event_date="2026-07-14")

    assert result["ok"] is False
    assert "public_peak_timing_update_binding_mismatch" in result["blockers"]


def test_acceptance_does_not_charge_pre_activation_event_to_forward_slo(tmp_path, monkeypatch) -> None:
    _patch_runtime(monkeypatch)
    db = _db(tmp_path / "stale-event.db", with_event=True)
    monkeypatch.setattr(
        module,
        "load_production_probability_activation",
        lambda: {
            "activation_contract_sha256": "a" * 64,
            "activation_effective_at": "2026-07-14T06:00:01Z",
        },
    )

    result = module.build_acceptance(db, event_date="2026-07-14")

    assert result["ok"] is True
    assert result["state"] == "cutover_accepted_forward_slo_pending"
    assert result["forward_three_second_slo"]["t1_to_t3_ms"] is None
    assert result["forward_three_second_slo"]["t1_to_t3_ms_na_reason"] == (
        "awaiting_first_post_activation_canonical_event"
    )
    assert result["forward_three_second_slo"]["event_is_post_activation"] is False


def test_acceptance_blocks_a_measured_forward_slo_failure(tmp_path, monkeypatch) -> None:
    _patch_runtime(monkeypatch)
    db = _db(tmp_path / "slow.db", with_event=True)
    with sqlite3.connect(db) as conn:
        payload = json.loads(conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0])
        payload["posterior_ssot_fact"]["committed_at"] = "2026-07-14T06:00:04Z"
        conn.execute(
            "UPDATE event_fact_history SET fact_as_of=?,fact_json=?",
            ("2026-07-14T06:00:04Z", json.dumps(payload)),
        )

    result = module.build_acceptance(db, event_date="2026-07-14")

    assert result["ok"] is False
    assert result["state"] == "blocked"
    assert "forward_three_second_slo_failed" in result["blockers"]
    assert result["forward_three_second_slo"]["state"] == "failed"
    assert result["forward_three_second_slo"]["t1_to_t3_ms"] == 4000.0
    assert result["forward_three_second_slo"]["t1_to_t3_ms_na_reason"] == ""


def test_service_recovery_requires_one_running_monitor_and_worker(monkeypatch, tmp_path) -> None:
    root = tmp_path / "weather"

    def fake_run(command, **_kwargs):
        class Result:
            returncode = 0
            stderr = ""

        result = Result()
        if command[:2] == ["launchctl", "print"]:
            result.stdout = "state = running\npid = 123\n"
        else:
            result.stdout = (
                f"321 123 /Library/Python {root}/scan_weather_market.py "
                "--auto-trade-job-worker\n"
            )
        return result

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.build_service_recovery_acceptance(root=root)

    assert result["ok"] is True
    assert result["target_realization_worker_process_count"] == 1
    assert result["target_realization_worker_processes_na_reason"] == ""
