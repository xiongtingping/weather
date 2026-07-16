import json
import sqlite3
from pathlib import Path

from posterior_ssot_projection_runtime import (
    _merge_projection_source_observations,
    _source_trace_with_observations,
    build_fact_projection_no_apply,
)
from scripts import posterior_ssot_rematerialization_plan as plan


def test_projection_prefers_direct_metar_history_over_fact_feature_adapter_rows():
    direct = {
        "source_event_id": "direct_peak",
        "source_observation_id": "direct_peak_obs",
        "source_id": "zuuu_metar_raw_amsc",
        "source_class": "hard_observation_candidate",
        "event_date": "2026-07-10",
        "valid_time": "2026-07-10T05:00:00Z",
        "fetched_at": "2026-07-10T05:00:43Z",
        "temperature_c": 36.0,
        "parser_version": "amsc_zuuu_metar_taf_fetcher_v1",
        "payload": {"raw_text": "METAR ZUUU 100500Z ... 36/23 ..."},
    }
    adapted = {
        "source_event_id": "adapted_stale_peak_time",
        "source_observation_id": "adapted_stale_peak_time_obs",
        "source_id": "zuuu_metar_raw_amsc",
        "source_class": "hard_observation_candidate",
        "event_date": "2026-07-10",
        "valid_time": "2026-07-10T05:00:00Z",
        "fetched_at": "2026-07-10T12:01:03Z",
        "temperature_c": 31.0,
        "parser_version": "posterior_ssot_projection_fact_feature_source_adapter_v1",
    }
    rows = _merge_projection_source_observations(
        {"source_acquisition_trace": {"source_observations": [direct, adapted]}},
        [],
        station_id="ZUUU",
        event_date="2026-07-10",
        fact_as_of="2026-07-10T13:30:45Z",
    )

    assert [row["source_event_id"] for row in rows] == ["direct_peak"]
    assert rows[0]["temperature_c"] == 36.0


def _create_db(path: Path, payloads: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
    for index, payload in enumerate(payloads, start=1):
        slug = payload.get("slug") or f"highest-temperature-in-chengdu-on-july-{index}-2026"
        event_date = payload.get("event_date") or f"2026-07-0{index}"
        conn.execute(
            """
            INSERT INTO event_fact_history(
                id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                index,
                payload.get("run_date") or event_date,
                slug,
                event_date,
                payload.get("fact_as_of") or f"{event_date}T03:00:00+00:00",
                "unit_test",
                json.dumps(payload, ensure_ascii=False),
                f"{event_date}T03:00:01+00:00",
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO event_fact_latest(slug, event_date, fact_id, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (slug, event_date, index, f"{event_date}T03:00:02+00:00"),
        )
    conn.commit()
    conn.close()


def test_source_trace_with_observations_filters_stale_latency_rows() -> None:
    trace = _source_trace_with_observations(
        {
            "schema": "weather_source_acquisition_trace_v1",
            "source_latency_trace": [
                {
                    "source_event_id": "srcevt_old",
                    "source_observation_id": "srcobs_old",
                    "source_id": "old_latency_only",
                },
                {
                    "source_event_id": "srcevt_keep",
                    "source_observation_id": "srcobs_keep",
                    "source_id": "kept_latency",
                    "response_duration_ms": 123,
                },
            ],
        },
        [
            {
                "source_event_id": "srcevt_keep",
                "source_observation_id": "srcobs_keep",
                "source_id": "kept_latency",
                "source_class": "hard_observation_candidate",
            },
            {
                "source_event_id": "srcevt_new",
                "source_observation_id": "srcobs_new",
                "source_id": "new_latency",
                "source_class": "forecast_weak_source",
            },
        ],
    )

    assert [row["source_observation_id"] for row in trace["source_latency_trace"]] == [
        "srcobs_keep",
        "srcobs_new",
    ]
    assert trace["source_latency_trace"][0]["response_duration_ms"] == 123
    assert trace["source_observation_ids"] == ["srcobs_keep", "srcobs_new"]


def _hard_floor_payload() -> dict:
    return {
        "slug": "highest-temperature-in-chengdu-on-july-5-2026",
        "event_date": "2026-07-05",
        "run_date": "2026-07-05",
        "fact_snapshot_id": "fact_old",
        "posterior_run_id": "post_old",
        "phase": "D0_awc_hard_signal",
        "phase_reason": "unit_test",
        "beta_weak": 0.0,
        "beta_strong": 1.0,
        "hard_mask_reason": "observed_today_max_c",
        "weather_likelihood_features": {
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        "posterior_temperature_distribution": [
            {"bucket": "29°C or below", "probability": 0.60},
            {"bucket": "33°C", "probability": 0.40},
        ],
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_old",
            "posterior_run_id": "post_old",
            "target_bucket": "29°C or below",
            "target_probability": 0.60,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "boundaries": {
                "frontend_recompute_allowed": False,
                "decision_recompute_allowed": False,
                "hard_mask_is_constraint_not_weight": True,
            },
            "layer_chain": {"schema": "bayes_layer_chain_summary_v1"},
        },
        "posterior_target_bucket_selection": {
            "target_bucket": "29°C or below",
            "target_probability": 0.60,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "ssot_source": "posterior_ssot_fact",
            "selection_runtime_recomputed": False,
        },
        "target_bucket_selection": {
            "target_bucket": "29°C or below",
            "target_probability": 0.60,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "ssot_source": "posterior_ssot_fact",
            "selection_runtime_recomputed": False,
        },
    }


def _valid_ssot_payload() -> dict:
    payload = _hard_floor_payload()
    payload["slug"] = "highest-temperature-in-chengdu-on-july-6-2026"
    payload["event_date"] = "2026-07-06"
    payload["run_date"] = "2026-07-06"
    payload["fact_snapshot_id"] = "fact_valid"
    payload["posterior_run_id"] = "post_valid"
    payload["weather_likelihood_features"]["observed_today_max_c"] = 31.0
    rows = [
        {"bucket": "31°C", "probability": 0.25},
        {"bucket": "33°C", "probability": 0.75},
    ]
    payload["posterior_temperature_distribution"] = list(rows)
    payload["posterior_ssot_fact"].update(
        {
            "fact_snapshot_id": "fact_valid",
            "posterior_run_id": "post_valid",
            "target_bucket": "33°C",
            "target_probability": 0.75,
            "bucket_probabilities": list(rows),
        }
    )
    for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
        payload[key].update(
            {
                "target_bucket": "33°C",
                "target_probability": 0.75,
                "target_probability_field": "posterior_ssot_fact.target_probability",
                "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
                "ssot_source": "posterior_ssot_fact",
                "selection_runtime_recomputed": False,
                "fact_snapshot_id": "fact_valid",
                "posterior_run_id": "post_valid",
            }
        )
    return payload


def test_rematerialization_plan_projects_hard_floor_fix_without_writing(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path, [_hard_floor_payload()])

    result = plan.build_plan(db_path, since_hours=0, latest_only=True)

    assert result["ok"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["forward_write_acceptance"]["can_forward_writer_fix_all_candidates"] is True
    assert result["historical_rematerialization_plan"]["candidate_count"] == 1
    assert result["historical_rematerialization_plan"]["current_latest_issue_count"] == 1
    assert result["historical_rematerialization_plan"]["historical_only_issue_count"] == 0
    assert result["historical_rematerialization_plan"]["current_latest_projected_ok_count"] == 1
    assert result["historical_rematerialization_plan"]["current_latest_projected_issue_count"] == 0
    assert result["historical_rematerialization_plan"]["production_latest_chain_blocked_by_candidates"] is True
    candidate = result["candidates"][0]
    assert candidate["is_latest_fact"] is True
    assert candidate["latest_fact_id"] == candidate["fact_id"]
    assert candidate["impact"]["production_latest_impact"] == "current_latest_fact_has_posterior_ssot_gap"
    assert candidate["impact"]["ssot_runtime_impact"] == (
        "current_ui_decision_review_would_need_forward_projection_or_controlled_apply"
    )
    assert candidate["issue_family"] == "hard_floor_target_invalidation"
    assert candidate["recommended_action"] == "controlled_append_or_repoint_with_fact_writer_after_operator_confirmation"
    assert candidate["projection"]["projected_ok"] is True
    assert candidate["projection"]["projected_target_bucket"] == "33°C"
    assert candidate["projection"]["writer_write_latest"] is False
    assert candidate["projection"]["writer_merge_fill_operation_count"] > 0
    assert isinstance(candidate["projection"]["writer_merge_fill_action_counts"], dict)
    assert len(candidate["projection"]["writer_merge_fill_operation_samples"]) <= 8


def test_rematerialization_plan_cli_summary_matches_documented_command(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path, [_hard_floor_payload()])

    assert plan.main(
        [
            "--db",
            str(db_path),
            "--since-hours",
            "0",
            "--limit",
            "20",
            "--max-items-output",
            "8",
            "--summary",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "posterior_ssot_rematerialization_plan_v1_summary"
    assert payload["ok"] is True
    assert payload["historical_rematerialization_plan"]["candidate_count"] == 1
    assert payload["historical_rematerialization_plan"]["current_latest_issue_count"] == 1
    assert payload["historical_rematerialization_plan"]["current_latest_projected_ok_count"] == 1
    assert payload["historical_rematerialization_plan"]["current_latest_projected_issue_count"] == 0
    assert payload["historical_rematerialization_plan"]["impact_counts"] == {
        "current_latest_fact_has_posterior_ssot_gap": 1
    }
    assert "candidates" not in payload


def test_rematerialization_plan_separates_full_rebuild_requirement(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    payload = _hard_floor_payload()
    payload.pop("posterior_temperature_distribution")
    payload.pop("posterior_target_bucket_selection")
    payload.pop("target_bucket_selection")
    _create_db(db_path, [payload])

    result = plan.build_plan(db_path, since_hours=0, latest_only=True)

    candidate = result["candidates"][0]
    assert candidate["issue_family"] == "requires_full_posterior_rebuild"
    assert candidate["recommended_action"] == "rebuild_hierarchical_posterior_before_fact_writer_apply"
    assert candidate["projection"]["projected_ok"] is False
    assert "posterior_temperature_distribution_missing" in candidate["projection"]["projected_issues"]


def test_rematerialization_plan_fact_ids_scope_and_counts_ignore_ok_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path, [_hard_floor_payload(), _valid_ssot_payload(), _hard_floor_payload()])

    result = plan.build_plan(
        db_path,
        fact_ids=[1, 2],
        since_hours=0,
        limit=10,
        max_items_output=10,
    )

    remat = result["historical_rematerialization_plan"]
    assert result["scope"]["fact_ids"] == [1, 2]
    assert result["forward_write_acceptance"]["checked_fact_count"] == 2
    assert remat["candidate_count"] == 1
    assert remat["action_counts"] == {
        "controlled_append_or_repoint_with_fact_writer_after_operator_confirmation": 1
    }
    assert "none" not in remat["action_counts"]
    assert sum(remat["action_counts"].values()) == remat["candidate_count"]
    assert result["candidates"][0]["fact_id"] == 1


def _posterior_context_payload() -> dict:
    labels = ["36°C", "37°C", "38°C"]
    rows = [
        {"bucket": label, "probability": probability}
        for label, probability in zip(labels, [0.25, 0.50, 0.25])
    ]
    return {
        "slug": "highest-temperature-in-chengdu-on-july-5-2026",
        "event_date": "2026-07-05",
        "run_date": "2026-07-05",
        "fact_snapshot_id": "fact_context",
        "posterior_run_id": "post_context",
        "phase": "D0_late",
        "phase_reason": "context",
        "beta_weak": 0.08,
        "beta_strong": 1.0,
        "hard_mask_reason": "observed_today_max_c",
        "prior_distribution": {"36°C": 0.2, "37°C": 0.6, "38°C": 0.2},
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "state": "available",
            "source": "unit_test_history_prior",
            "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
            "sample_count": 30,
            "date_window_days": 15,
            "shrinkage_target": "broader_settled_history",
            "prior_na_reason": "",
        },
        "prior_na_reason": "",
        "distribution": rows,
        "posterior_temperature_distribution": rows,
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_context",
            "posterior_run_id": "post_context",
            "target_bucket": "37°C",
            "target_probability": 0.50,
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
            "target_bucket": "37°C",
            "target_probability": 0.50,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            "ssot_source": "posterior_ssot_fact",
            "selection_runtime_recomputed": False,
            "fact_snapshot_id": "fact_context",
            "posterior_run_id": "post_context",
        },
        "target_bucket_selection": {
            "target_bucket": "37°C",
            "target_probability": 0.50,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            "ssot_source": "posterior_ssot_fact",
            "selection_runtime_recomputed": False,
            "fact_snapshot_id": "fact_context",
            "posterior_run_id": "post_context",
        },
    }


def _stale_midday_policy_payload() -> dict:
    labels = ["36°C", "37°C", "38°C", "39°C", "40°C", "41°C", "42°C"]
    old_rows = [
        {"bucket": "36°C", "probability": 0.02},
        {"bucket": "37°C", "probability": 0.12},
        {"bucket": "38°C", "probability": 0.18},
        {"bucket": "39°C", "probability": 0.20},
        {"bucket": "40°C", "probability": 0.26},
        {"bucket": "41°C", "probability": 0.16},
        {"bucket": "42°C", "probability": 0.06},
    ]
    return {
        "slug": "highest-temperature-in-chengdu-on-july-7-2026",
        "event_date": "2026-07-07",
        "run_date": "2026-07-07",
        "fact_as_of": "2026-07-07T06:00:19Z",
        "station_id": "ZUUU",
        "station_timezone": "Asia/Shanghai",
        "fact_snapshot_id": "fact_stale_policy",
        "posterior_run_id": "post_stale_policy",
        "phase": "D0_late",
        "phase_reason": "unit_stale_midday_policy",
        "beta_weak": 0.08,
        "beta_strong": 1.0,
        "hard_mask_reason": "observed_today_max_c+current_temperature_c",
        "prior_distribution": {label: 1.0 / len(labels) for label in labels},
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "state": "available",
            "source": "unit_test_history_prior",
            "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
            "sample_count": 30,
            "date_window_days": 15,
            "prior_na_reason": "",
        },
        "posterior_temperature_distribution": old_rows,
        "distribution": old_rows,
        "metar_strong_likelihood": {
            "36°C": 0.01,
            "37°C": 0.05,
            "38°C": 0.14,
            "39°C": 0.25,
            "40°C": 0.30,
            "41°C": 0.20,
            "42°C": 0.05,
        },
        "weather_likelihood_features": {
            "current_temp_c": 36.0,
            "metar_temperature_c": 36.0,
            "observed_today_max_c": 36.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW020 SCT040",
            "temperature_momentum_1h_c_per_hour": 2.0,
            "temperature_momentum_2h_c_per_hour": 2.0,
            "temperature_momentum_3h_c_per_hour": 1.6667,
            "temperature_momentum_6h_c_per_hour": 0.5,
            "temperature_trajectory_state": "sustained_warming",
            "temperature_trajectory_gate_state": "confirmed",
            "temperature_trajectory_gate_reason": "multi_window_confirmed",
            "temperature_trajectory_stable_rate_c_per_hour": 1.3,
            "temperature_trajectory_effective_rate_c_per_hour": 1.3,
            "temperature_trajectory_realization_state": "sustained_warming",
        },
        "temperature_trajectory": {
            "schema": "temperature_trajectory_summary_v1",
            "state": "sustained_warming",
            "gate_state": "confirmed",
            "gate_reason": "multi_window_confirmed",
        },
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_stale_policy",
            "posterior_run_id": "post_stale_policy",
            "target_bucket": "40°C",
            "target_probability": 0.26,
            "bucket_probabilities": old_rows,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "boundaries": {
                "frontend_recompute_allowed": False,
                "decision_recompute_allowed": False,
                "hard_mask_is_constraint_not_weight": True,
            },
            "layer_chain": {"schema": "bayes_layer_chain_summary_v1"},
        },
        "posterior_target_bucket_selection": {
            "target_bucket": "40°C",
            "target_probability": 0.26,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            "ssot_source": "posterior_ssot_fact",
            "selection_runtime_recomputed": False,
        },
        "target_bucket_selection": {
            "target_bucket": "40°C",
            "target_probability": 0.26,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            "ssot_source": "posterior_ssot_fact",
            "selection_runtime_recomputed": False,
        },
    }


def test_projection_recomputes_complete_fact_when_metar_policy_version_is_stale(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    payload = _stale_midday_policy_payload()
    _create_db(db_path, [payload])

    projection = build_fact_projection_no_apply(
        slug=payload["slug"],
        event_date=payload["event_date"],
        run_date=payload["run_date"],
        fact_as_of=payload["fact_as_of"],
        payload=payload,
        current_fact_id=1,
        current_fact_as_of=payload["fact_as_of"],
        composer_version="unit_stale_policy",
        db_path=db_path,
        write_reason="unit_runtime_policy_projection",
    )

    projected_payload = projection["projected_payload"]
    projected_map = {
        row["bucket"]: row["probability"]
        for row in projected_payload["posterior_temperature_distribution"]
    }
    strong = projected_payload["metar_strong_likelihood"]
    trajectory = projected_payload["temperature_trajectory"]
    policy_context = projection["context"]["runtime_policy_context"]

    assert projection["ok"] is True
    assert projection["projection_mode"] == "contextual_hierarchical_posterior_projection"
    assert policy_context["requires_contextual_projection"] is True
    assert policy_context["reason"] == "metar_strong_runtime_policy_changed"
    assert projected_payload["runtime_policy_versions"]["metar_strong_likelihood"]
    assert projected_payload["fact_snapshot_id"] != payload["fact_snapshot_id"]
    assert projected_payload["posterior_ssot_fact"]["fact_snapshot_id"] == projected_payload["fact_snapshot_id"]
    assert trajectory["state"] == "midday_recent_confirmation_missing"
    assert trajectory["gate_state"] == "recent_window_watch"
    assert trajectory["gate_reason"] == "peak_window_requires_30m_recent_confirmation"
    assert "temperature_momentum_midday_recent_window_confirmation_missing" in projected_payload["na_reasons"]
    assert strong["40°C"] < strong["38°C"]
    assert projected_map["40°C"] < projected_map["38°C"]
    assert projected_payload["posterior_ssot_fact"]["target_bucket"] != "40°C"
    assert projected_payload["posterior_projection_context"]["projection_recompute_reason"] == (
        "metar_strong_runtime_policy_changed"
    )


def test_rematerialization_plan_flags_runtime_policy_stale_complete_fact(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path, [_stale_midday_policy_payload()])

    result = plan.build_plan(
        db_path,
        fact_ids=[1],
        since_hours=0,
        latest_only=True,
        max_items_output=10,
    )

    remat = result["historical_rematerialization_plan"]
    candidate = result["candidates"][0]
    projection = candidate["projection"]

    assert result["ok"] is True
    assert remat["candidate_count"] == 1
    assert remat["current_latest_issue_count"] == 1
    assert remat["current_latest_projected_ok_count"] == 1
    assert candidate["current_issues"] == ["posterior_runtime_policy_stale"]
    assert candidate["issue_family"] == "runtime_policy_materialization_gap"
    assert candidate["recommended_action"] == "controlled_append_or_repoint_with_fact_writer_after_operator_confirmation"
    assert projection["writer_projection_mode"] == "contextual_hierarchical_posterior_projection"
    assert projection["writer_projection_context"]["runtime_policy_context"]["reason"] == (
        "metar_strong_runtime_policy_changed"
    )
    assert projection["projected_target_bucket"] != "40°C"


def _amsc_source_only_payload() -> dict:
    return {
        "slug": "highest-temperature-in-chengdu-on-july-5-2026",
        "event_date": "2026-07-05",
        "run_date": "2026-07-05",
        "fact_as_of": "2026-07-05T16:00:14Z",
        "station_id": "ZUUU",
        "source_observations": [
            {
                "source_event_id": "srcevt_amsc_metar",
                "source_observation_id": "srcobs_amsc_metar",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "role": "metar_hard_observation",
                "hard_eligible": True,
                "event_date": "2026-07-05",
                "valid_time": "2026-07-05T15:30:00Z",
                "temperature_c": 33.0,
                "observed_today_max_c": 37.0,
                "final_daily_max_c": 37.0,
                "official_final_result": True,
                "temperature_observation_final_daily_max": True,
            },
            {
                "source_event_id": "srcevt_amsc_taf",
                "source_observation_id": "srcobs_amsc_taf",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "role": "taf_forecast_weak_likelihood",
                "hard_eligible": False,
                "event_date": "2026-07-05",
                "forecast_peak_c": 36.0,
            },
        ],
        "source_acquisition_trace": {
            "schema": "weather_source_acquisition_trace_v1",
            "read_only": True,
            "source_observations": [
                {
                    "source_event_id": "srcevt_amsc_metar",
                    "source_observation_id": "srcobs_amsc_metar",
                    "source_id": "zuuu_metar_raw_amsc",
                    "source_class": "hard_observation_candidate",
                    "role": "metar_hard_observation",
                    "hard_eligible": True,
                    "event_date": "2026-07-05",
                    "valid_time": "2026-07-05T15:30:00Z",
                    "temperature_c": 33.0,
                    "observed_today_max_c": 37.0,
                    "final_daily_max_c": 37.0,
                    "official_final_result": True,
                    "temperature_observation_final_daily_max": True,
                },
                {
                    "source_event_id": "srcevt_amsc_taf",
                    "source_observation_id": "srcobs_amsc_taf",
                    "source_id": "zuuu_taf_forecast_amsc",
                    "source_class": "forecast_weak_source",
                    "role": "taf_forecast_weak_likelihood",
                    "hard_eligible": False,
                    "event_date": "2026-07-05",
                    "forecast_peak_c": 36.0,
                },
            ],
            "hard_observation_candidate_ids": ["srcevt_amsc_metar"],
            "forecast_weak_ids": ["srcevt_amsc_taf"],
            "source_observation_ids": ["srcobs_amsc_metar", "srcobs_amsc_taf"],
            "source_latency_trace": [],
        },
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_placeholder_v1",
            "state": "not_materialized",
            "fact_snapshot_id": "fact_source_only",
            "posterior_run_id": "",
        },
    }


def test_rematerialization_plan_contextually_rebuilds_source_only_latest_without_writing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path, [_posterior_context_payload(), _amsc_source_only_payload()])

    result = plan.build_plan(db_path, since_hours=0, latest_only=True)

    assert result["ok"] is True
    assert result["guards"]["writes_archive_db"] is False
    candidate = result["candidates"][0]
    projection = candidate["projection"]
    assert candidate["issue_family"] == "requires_full_posterior_rebuild"
    assert projection["projected_ok"] is True
    assert projection["projected_target_bucket"] == "37°C"
    assert projection["projected_target_probability"] == 1.0
    assert projection["writer_write_latest"] is False
    assert projection["writer_projection_mode"] == "contextual_hierarchical_posterior_projection"
    assert projection["writer_projection_context"]["context_fact_id"] == 1
    assert projection["writer_projection_context"]["prior_source"] == "context_payload"


def test_posterior_projection_refreshes_reusable_prior_when_runtime_history_has_stronger_coverage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    context_payload = _posterior_context_payload()
    source_only_payload = _amsc_source_only_payload()
    _create_db(db_path, [context_payload, source_only_payload])
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE event_daily_temps(
                id INTEGER PRIMARY KEY,
                event_date TEXT,
                settlement_actual REAL,
                created_at TEXT,
                station_id TEXT,
                city TEXT,
                market_var TEXT,
                market_unit TEXT
            )
            """
        )
        for day in range(1, 41):
            conn.execute(
                """
                INSERT INTO event_daily_temps(
                    event_date, settlement_actual, created_at, station_id, city,
                    market_var, market_unit
                ) VALUES (?, 38.0, '2026-07-04T00:00:00+00:00', 'ZUUU', 'chengdu', 'temperature_2m_max', 'C')
                """,
                (f"2026-05-{day:02d}" if day <= 31 else f"2026-06-{day - 31:02d}",),
            )
        conn.commit()
    finally:
        conn.close()

    projection = build_fact_projection_no_apply(
        slug=context_payload["slug"],
        event_date=context_payload["event_date"],
        run_date=context_payload["run_date"],
        fact_as_of="2026-07-05T15:05:55Z",
        payload=context_payload,
        current_fact_id=1,
        current_fact_as_of="2026-07-05T15:05:55Z",
        composer_version="unit_context_posterior",
        db_path=db_path,
        write_reason="unit_projection_prior_coverage_refresh",
    )

    projected_payload = projection["projected_payload"]

    assert projection["ok"] is True
    assert projection["context"]["prior_source"] == "temperature_prior_runtime_coverage_refresh"
    assert projected_payload["posterior_projection_context"]["prior_source"] == (
        "temperature_prior_runtime_coverage_refresh"
    )
    assert projected_payload["prior_meta"]["raw_candidate_count"] == 40
    assert projected_payload["prior_meta"]["sample_count"] == 40
    assert projected_payload["prior_distribution"]["38°C"] > projected_payload["prior_distribution"]["37°C"]


def test_rematerialization_plan_flags_prior_coverage_refresh_as_current_latest_candidate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    context_payload = _posterior_context_payload()
    _create_db(db_path, [context_payload])
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE event_daily_temps(
                id INTEGER PRIMARY KEY,
                event_date TEXT,
                settlement_actual REAL,
                created_at TEXT,
                station_id TEXT,
                city TEXT,
                market_var TEXT,
                market_unit TEXT
            )
            """
        )
        for day in range(1, 41):
            conn.execute(
                """
                INSERT INTO event_daily_temps(
                    event_date, settlement_actual, created_at, station_id, city,
                    market_var, market_unit
                ) VALUES (?, 38.0, '2026-07-04T00:00:00+00:00', 'ZUUU', 'chengdu', 'temperature_2m_max', 'C')
                """,
                (f"2026-05-{day:02d}" if day <= 31 else f"2026-06-{day - 31:02d}",),
            )
        conn.commit()
    finally:
        conn.close()

    result = plan.build_plan(db_path, fact_ids=[1], latest_only=True)

    assert result["historical_rematerialization_plan"]["candidate_count"] == 1
    assert result["historical_rematerialization_plan"]["current_latest_issue_count"] == 1
    assert result["historical_rematerialization_plan"]["issue_family_counts"] == {
        "prior_coverage_materialization_gap": 1
    }
    candidate = result["candidates"][0]
    assert candidate["current_issues"] == ["posterior_prior_coverage_refresh_needed"]
    assert candidate["issue_family"] == "prior_coverage_materialization_gap"
    assert candidate["projection"]["writer_projection_context"]["prior_source"] == (
        "temperature_prior_runtime_coverage_refresh"
    )
    assert candidate["recommended_action"] == "controlled_append_or_repoint_with_fact_writer_after_operator_confirmation"


def test_posterior_projection_merges_newer_amsc_source_history_when_posterior_remains_latest(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    context_payload = _posterior_context_payload()
    source_only_payload = _amsc_source_only_payload()
    _create_db(db_path, [context_payload, source_only_payload])
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE event_fact_latest
            SET fact_id = ?, updated_at = ?
            WHERE slug = ? AND event_date = ?
            """,
            (
                1,
                "2026-07-05T15:05:55Z",
                context_payload["slug"],
                context_payload["event_date"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    projection = build_fact_projection_no_apply(
        slug=context_payload["slug"],
        event_date=context_payload["event_date"],
        run_date=context_payload["run_date"],
        fact_as_of="2026-07-05T15:05:55Z",
        payload=context_payload,
        current_fact_id=1,
        current_fact_as_of="2026-07-05T15:05:55Z",
        composer_version="unit_context_posterior",
        db_path=db_path,
        write_reason="unit_projection_newer_amsc_history",
    )

    projected_payload = projection["projected_payload"]
    source_context = projection["context"]["source_observation_context"]
    source_ids = [row["source_id"] for row in projected_payload["source_observations"]]

    assert projection["ok"] is True
    assert projection["projection_mode"] == "contextual_hierarchical_posterior_projection"
    assert projection["context"]["source_history_observations_merged"] is True
    assert source_context["source_history_fact_ids"] == [2]
    assert source_context["source_history_merged_observation_count"] == 2
    assert "zuuu_metar_raw_amsc" in source_ids
    assert "zuuu_taf_forecast_amsc" in source_ids
    assert projected_payload["posterior_ssot_fact"]["target_bucket"] == "37°C"
    assert projected_payload["posterior_ssot_fact"]["target_probability"] == 1.0
    assert projected_payload["metar_strong_likelihood"]["37°C"] == 1.0
    assert projected_payload["forecast_weak_likelihood"]
    assert projected_payload["posterior_projection_context"]["source_history_observations_merged"] is True
    assert projected_payload["source_acquisition_trace"]["source_observation_context"]["boundary"] == (
        "same_slug_event_date_newer_source_only_history_read_only_no_db_write"
    )


def test_posterior_projection_merges_model_member_cache_as_forecast_weak_sources(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    context_payload = _posterior_context_payload()
    _create_db(db_path, [context_payload])
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE model_member_cache (
                id INTEGER PRIMARY KEY,
                model TEXT,
                loc_key TEXT,
                event_date TEXT,
                market_var TEXT,
                market_unit TEXT,
                members_json TEXT,
                created_at TEXT,
                official_updated_at TEXT,
                official_updated_source TEXT,
                members_signature TEXT
            )
            """
        )
        model_rows = [
            (1, "gfs_seamless", "[36.8, 37.0, 37.2]", "2026-07-05T02:00:00Z"),
            (2, "icon_seamless", "[37.8, 38.0, 38.2]", "2026-07-05T02:01:00Z"),
            (3, "ecmwf_ifs025", "[37.6, 37.8, 38.0]", "2026-07-05T02:02:00Z"),
        ]
        for row_id, model, members, updated_at in model_rows:
            conn.execute(
                """
                INSERT INTO model_member_cache(
                    id, model, loc_key, event_date, market_var, market_unit,
                    members_json, created_at, official_updated_at,
                    official_updated_source, members_signature
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    model,
                    "30.5583,103.9460",
                    context_payload["event_date"],
                    "temperature_2m_max",
                    "C",
                    members,
                    updated_at,
                    updated_at,
                    "unit_model_cache",
                    f"sig_{row_id}",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    projection = build_fact_projection_no_apply(
        slug=context_payload["slug"],
        event_date=context_payload["event_date"],
        run_date=context_payload["run_date"],
        fact_as_of="2026-07-05T02:05:00Z",
        payload=context_payload,
        current_fact_id=1,
        current_fact_as_of="2026-07-05T02:05:00Z",
        composer_version="unit_context_posterior",
        db_path=db_path,
        write_reason="unit_projection_model_member_cache",
    )

    projected_payload = projection["projected_payload"]
    source_context = projection["context"]["source_observation_context"]
    model_context = source_context["model_member_cache_context"]
    source_ids = [row["source_id"] for row in projected_payload["source_observations"]]

    assert projection["ok"] is True
    assert projection["projection_mode"] == "contextual_hierarchical_posterior_projection"
    assert projection["context"]["model_member_cache_observations_merged"] is True
    assert model_context["state"] == "merged"
    assert model_context["source_observation_count"] == 3
    assert {"gfs_model_peak", "icon_model_peak", "ecmwf_model_peak"}.issubset(set(source_ids))
    assert projected_payload["posterior_projection_context"]["source_observation_context_kind"] == (
        "current_payload_plus_model_member_cache"
    )
    assert projected_payload["forecast_weak_likelihood"]["38°C"] > projected_payload["forecast_weak_likelihood"]["36°C"]


def test_posterior_projection_merges_soft_forecast_event_runs_as_weak_sources(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    context_payload = _posterior_context_payload()
    _create_db(db_path, [context_payload])
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY,
                slug TEXT,
                event_date TEXT,
                payload_json TEXT,
                created_at TEXT
            )
            """
        )
        event_run_payload = {
            "weather_likelihood_features": {
                "wu_forecast_peak_c": 38.0,
                "wu_forecast_peak_at": "2026-07-05T15:00:00+08:00",
                "wu_forecast_peak_source": "wunderground_forecast_daily_peak",
                "weather_com_forecast_peak_c": 38.0,
                "weather_com_forecast_peak_valid_local": "2026-07-05T15:00:00+08:00",
                "weather_com_forecast_peak_source_url": "https://weather.com",
            },
            "wu_forecast_peak": {
                "ok": True,
                "value_c": 38.0,
                "source": "wunderground_forecast_daily_peak",
            },
            "weather_com_forecast_peak": {
                "ok": True,
                "value_c": 38.0,
                "source": "weather_com_forecast_daily_peak",
                "source_url": "https://weather.com",
            },
        }
        conn.execute(
            """
            INSERT INTO event_runs(id, slug, event_date, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                1,
                context_payload["slug"],
                context_payload["event_date"],
                json.dumps(event_run_payload, ensure_ascii=False),
                "2026-07-05T01:50:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    projection = build_fact_projection_no_apply(
        slug=context_payload["slug"],
        event_date=context_payload["event_date"],
        run_date=context_payload["run_date"],
        fact_as_of="2026-07-05T02:05:00Z",
        payload=context_payload,
        current_fact_id=1,
        current_fact_as_of="2026-07-05T02:05:00Z",
        composer_version="unit_context_posterior",
        db_path=db_path,
        write_reason="unit_projection_soft_forecast_event_runs",
    )

    projected_payload = projection["projected_payload"]
    source_context = projection["context"]["source_observation_context"]
    soft_context = source_context["soft_forecast_event_runs_context"]
    source_ids = [row["source_id"] for row in projected_payload["source_observations"]]

    assert projection["ok"] is True
    assert projection["projection_mode"] == "contextual_hierarchical_posterior_projection"
    assert projection["context"]["soft_forecast_event_runs_observations_merged"] is True
    assert soft_context["state"] == "merged"
    assert soft_context["source_observation_count"] == 2
    assert set(soft_context["channels"]) == {"weather_com", "wu"}
    assert {"wu_forecast_high", "weather_com_forecast_peak"}.issubset(set(source_ids))
    assert "twc_forecast_peak" not in source_ids
    assert projected_payload["posterior_projection_context"]["source_observation_context_kind"] == (
        "current_payload_plus_soft_forecast_event_runs"
    )
    assert projected_payload["weather_likelihood_features"]["wu_forecast_peak_c"] == 38.0
    assert projected_payload["weather_likelihood_features"]["weather_com_forecast_peak_c"] == 38.0
    assert projected_payload["forecast_weak_likelihood"]["38°C"] > projected_payload["forecast_weak_likelihood"]["36°C"]


def test_rematerialization_plan_classifies_superseded_source_history_as_non_latest_governance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    context_payload = _posterior_context_payload()
    source_only_payload = _amsc_source_only_payload()
    _create_db(db_path, [context_payload, source_only_payload])
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE event_fact_latest
            SET fact_id = ?, updated_at = ?
            WHERE slug = ? AND event_date = ?
            """,
            (
                1,
                "2026-07-05T15:05:55Z",
                context_payload["slug"],
                context_payload["event_date"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = plan.build_plan(db_path, since_hours=0, latest_only=False, max_items_output=10)

    remat = result["historical_rematerialization_plan"]
    assert remat["candidate_count"] == 1
    assert remat["current_latest_issue_count"] == 0
    assert remat["historical_only_issue_count"] == 1
    assert remat["current_latest_projected_ok_count"] == 0
    assert remat["current_latest_projected_issue_count"] == 0
    assert remat["production_latest_chain_blocked_by_candidates"] is False
    assert remat["impact_counts"] == {"historical_fact_superseded_by_latest_fact": 1}
    candidate = result["candidates"][0]
    assert candidate["fact_id"] == 2
    assert candidate["latest_fact_id"] == 1
    assert candidate["is_latest_fact"] is False
    assert candidate["impact"]["production_latest_impact"] == "historical_fact_superseded_by_latest_fact"
    assert candidate["impact"]["ssot_runtime_impact"] == "no_current_latest_ssot_impact"
    assert candidate["impact"]["daily_review_impact"] == "no_persisted_daily_review_anchor_for_candidate"
    assert candidate["impact"]["operator_priority"] == "history_governance_only_after_latest_chain_is_green"
