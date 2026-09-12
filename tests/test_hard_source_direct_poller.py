import importlib.util
import inspect
import json
import os
import plistlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import scan_weather_market as swm
from awc_hard_source_signal_ledger_writer import AWC_HARD_SOURCE_SIGNAL_COLUMNS
from signal_to_order_ledger_writer import SIGNAL_TO_ORDER_TIMING_COLUMNS
from source_observation_runtime import SOURCE_CLASSES
from weather_source_acquisition_manager import build_trace_from_refresh_result


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hard_source_direct_poller",
    ROOT / "scripts" / "hard_source_direct_poller.py",
)
poller = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(poller)
AUDIT_SPEC = importlib.util.spec_from_file_location(
    "hard_source_direct_acceptance_audit",
    ROOT / "scripts" / "hard_source_direct_acceptance_audit.py",
)
hard_source_audit = importlib.util.module_from_spec(AUDIT_SPEC)
assert AUDIT_SPEC.loader is not None
AUDIT_SPEC.loader.exec_module(hard_source_audit)
SSOT_AUDIT_SPEC = importlib.util.spec_from_file_location(
    "audit_hard_source_direct_ssot",
    ROOT / "scripts" / "audit_hard_source_direct_ssot.py",
)
ssot_audit = importlib.util.module_from_spec(SSOT_AUDIT_SPEC)
assert SSOT_AUDIT_SPEC.loader is not None
SSOT_AUDIT_SPEC.loader.exec_module(ssot_audit)


@pytest.fixture(autouse=True)
def restore_auto_trade_globals_after_test(monkeypatch):
    before = {
        "AUTO_TRADE_ENABLED": swm.AUTO_TRADE_ENABLED,
        "AUTO_TRADE_DRY_RUN": swm.AUTO_TRADE_DRY_RUN,
        "AUTO_TRADE_SYNC_TRADES": swm.AUTO_TRADE_SYNC_TRADES,
    }
    swm._TARGET_REALIZATION_FACT_PLAN_CACHE.clear()
    monkeypatch.setenv("PM_TARGET_REALIZATION_SSOT_URL", "")
    monkeypatch.setenv("PM_TARGET_REALIZATION_V3_ENABLED", "0")
    monkeypatch.setenv("PM_TARGET_REALIZATION_V3_ACTIVATION_DATE_ET", "2000-01-01")
    monkeypatch.setenv("PM_AUTO_TRADE_JOB_CLAIM_ONLY_TARGET_REALIZATION_V3", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_ENQUEUE_JOB", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DIRECT_SKIP_ENTRY_BALANCE_GUARD", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_RELEASE_WATCH_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_FINAL_WATCH_ENABLED", "0")
    try:
        yield
    finally:
        swm._TARGET_REALIZATION_FACT_PLAN_CACHE.clear()
        for key, value in before.items():
            setattr(swm, key, value)


def _utc_fixture_iso(offset_sec: float = 0.0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=float(offset_sec))
    ).replace(microsecond=0).isoformat()


def _set_primary_amsc_hard_source_features(
    features: dict,
    *,
    valid_at: str,
    fetched_at: str,
    value_c: float | None = None,
) -> None:
    value = value_c
    if value is None:
        value = (
            features.get("zuuu_metar_raw_amsc_today_max_c")
            or features.get("zuuu_metar_raw_awc_today_max_c")
            or features.get("zuuu_metar_raw_amsc_latest_temp_c")
            or features.get("zuuu_metar_raw_awc_latest_temp_c")
            or 31.0
        )
    features["zuuu_metar_raw_amsc_today_max_c"] = float(value)
    features["zuuu_metar_raw_amsc_latest_temp_c"] = float(value)
    features["zuuu_metar_raw_amsc_today_max_at"] = valid_at
    features["zuuu_metar_raw_amsc_latest_at"] = valid_at
    features["zuuu_metar_raw_amsc_fetched_at"] = fetched_at
    features["zuuu_metar_raw_amsc_fetch_duration_ms"] = features.get("zuuu_metar_raw_amsc_fetch_duration_ms") or 210.0
    features["zuuu_metar_raw_amsc_latest_raw"] = features.get("zuuu_metar_raw_amsc_latest_raw") or "ZUUU 190900Z ... 31/22"


def _attach_unit_posterior_ssot_fact(
    payload: dict,
    *,
    target_bucket: str,
    target_probability: float,
) -> dict:
    rows = []
    for raw_row in payload.get("distribution") or payload.get("rows") or []:
        if not isinstance(raw_row, dict):
            continue
        label = str(raw_row.get("outcome") or raw_row.get("bucket") or raw_row.get("label") or "").strip()
        if not label:
            continue
        prob = raw_row.get("q_temperature_bucket_posterior")
        if prob is None:
            prob = raw_row.get("q_bayes_posterior")
        if prob is None:
            prob = raw_row.get("q_entry")
        if prob is None:
            prob = raw_row.get("q_round")
        if prob is None:
            continue
        rows.append({"outcome": label, "bucket": label, "probability": float(prob)})
    if not rows:
        rows.append({"outcome": target_bucket, "bucket": target_bucket, "probability": float(target_probability)})
    non_target_rows = [row for row in rows if row["bucket"] != target_bucket]
    remaining = max(0.0, 1.0 - float(target_probability))
    non_target_sum = sum(float(row.get("probability") or 0.0) for row in non_target_rows)
    for row in rows:
        if row["bucket"] == target_bucket:
            row["probability"] = float(target_probability)
        elif non_target_sum > 0.0:
            row["probability"] = remaining * float(row.get("probability") or 0.0) / non_target_sum
        elif non_target_rows:
            row["probability"] = remaining / len(non_target_rows)
    rows.sort(key=lambda row: (-float(row.get("probability") or 0.0), str(row.get("outcome") or "")))
    payload["posterior_composer_schema"] = "sequential_remaining_warming_bayes_runtime_v31"
    payload["posterior_activation_mode"] = "live_no_shadow"
    payload.setdefault("fact_snapshot_id", "fact_unit_sequential")
    payload.setdefault("posterior_run_id", "post_unit_sequential")
    source_features = payload.get("weather_likelihood_features")
    source_features = source_features if isinstance(source_features, dict) else {}
    payload.setdefault(
        "source_to_posterior_timing",
        {
            "latest_source_first_actionable_seen_at": str(
                source_features.get("zuuu_metar_raw_amsc_fetched_at")
                or _utc_fixture_iso()
            )
        },
    )
    continuous_cdf = 0.0
    continuous_grid = []
    for index, row in enumerate(rows):
        mass = float(row.get("probability") or 0.0)
        continuous_cdf += mass
        continuous_grid.append(
            {
                "temp_c": round(20.0 + index * 0.1, 1),
                "mass": mass,
                "cdf": continuous_cdf,
            }
        )
    payload["posterior_continuous_distribution"] = {
        "schema": "sequential_remaining_warming_continuous_distribution_v31",
        "source": "sequential_remaining_warming_bayes_runtime_v31",
        "target_variable": "settlement_daily_max_c",
        "grid_step_c": 0.1,
        "grid": continuous_grid,
        "bucket_probabilities": rows,
    }
    payload["sequential_state_filter"] = {"schema": "sequential_metar_state_filter_v1"}
    payload["remaining_warming_mixture"] = {
        "schema": "zero_inflated_remaining_warming_mixture_v1"
    }
    payload["observation_information_gate"] = {
        "schema": "continuous_observation_information_gate_v1"
    }
    payload["posterior_boundary"] = {
        "legacy_bucket_product_posterior_not_used": True,
        "live_no_shadow": True,
        "shadow_posterior_emitted": False,
    }
    payload["hard_constraints"] = {
        "posterior": {"hard_constraint_violation_count": 0}
    }
    payload["posterior_ssot_fact"] = {
        "schema": "bayes_posterior_ssot_fact_v1",
        "state": "available",
        "target_bucket": target_bucket,
        "target_probability": float(target_probability),
        "target_probability_pct": float(target_probability) * 100.0,
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "fact_snapshot_id": payload["fact_snapshot_id"],
        "posterior_run_id": payload["posterior_run_id"],
        "posterior_composer_schema": payload["posterior_composer_schema"],
        "posterior_activation_mode": payload["posterior_activation_mode"],
        "layer_chain": {
            "formula": "continuous_forecast_error_prior_then_sequential_state_zero_inflated_remaining_warming_mixture_then_bucket_integral"
        },
        "boundaries": {
            "live_primary_composer": "sequential_remaining_warming_bayes_runtime_v31",
            "posterior_activation_mode": "live_no_shadow",
            "legacy_bucket_product_posterior_used": False,
            "shadow_posterior_emitted": False,
        },
        "ranked_bucket_probabilities": rows,
        "bucket_probabilities": rows,
        # The composer's physical verdict, which the three-leg plan refuses
        # to proceed without: it will not reverse-engineer weather from raw
        # METAR, so a missing verdict keeps both legs HOLD and the poller
        # returns target_realization_v3_plan_blocked instead of recording a
        # decision.  Production mints this on every SSOT block; these
        # fixtures were written before the plan demanded it.  Same minimal
        # shape the v3 integration tests use.
        "strong_physical_evidence": {
            "joint_feature_model_applied": True,
            "top_bucket": target_bucket,
            "top_bucket_joint_strong_support": True,
            "bucket_exclusions": [],
        },
    }
    payload["posterior_target_bucket_selection"] = {
        "target_bucket": target_bucket,
        "target_probability": float(target_probability),
        "target_probability_pct": float(target_probability) * 100.0,
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "ssot_source": "posterior_ssot_fact",
        "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
    }
    plan = payload.get("target_realization_capital_plan")
    if isinstance(plan, dict):
        plan.setdefault("target_bucket", target_bucket)
        plan.setdefault("target_posterior", float(target_probability))
        plan.setdefault("target_bucket_selection_source", "posterior_ssot_fact.top_bucket")
        plan.setdefault("target_probability_field", "posterior_ssot_fact.target_probability")
    return payload


def test_posterior_target_latency_snapshot_captures_enqueue_disabled():
    result = {
        "ok": True,
        "event_date": "2026-07-02",
        "timings_ms": {
            "awc_refresh_ms": 910.0,
            "posterior_after_awc_ctp_recompute_ms": 22.0,
        },
        "awc": {
            "ok": True,
            "status": "updated",
            "reason": "awc_observation_updated",
            "awc_latest_at": "2026-07-02T06:00:00+00:00",
            "awc_today_max_c": 34.0,
            "latest_fact_payload_source": "summary_cache",
        },
        "posterior_target_after_awc_ctp": {
            "ok": True,
            "stage": "after_awc_ctp_recompute",
            "status": "recorded",
            "event_date": "2026-07-02",
            "fact_id": 103638,
            "fact_as_of": "2026-07-02T06:07:00.611+00:00",
            "latest_fact_payload_source": "summary_cache",
            "target_bucket": "34°C",
            "target_posterior": 0.9999999999991778,
            "previous_bucket": "33°C",
            "previous_posterior": 0.7000640178138834,
            "bucket_changed": True,
            "probability_changed": True,
            "probability_delta": 0.2999359821852944,
            "entry_order_update_triggered": True,
            "job_id": None,
            "job_enqueued": False,
            "job_enqueue_eligible": False,
            "job_enqueue_attempted": False,
            "job_skipped_reason": "posterior_target_enqueue_disabled",
            "decision_started_at": "2026-07-02T06:07:02.000+00:00",
            "decision_created_at": "2026-07-02T06:07:02.031+00:00",
        },
    }

    snapshot = poller._posterior_target_latency_snapshot(result)

    assert snapshot["persist_event"] is True
    assert snapshot["target_update_detected"] is True
    assert snapshot["root_cause_hint"] == "posterior_target_enqueue_job_disabled"
    assert snapshot["job_skipped_reason"] == "posterior_target_enqueue_disabled"
    assert snapshot["job_enqueue_eligible"] is False
    assert snapshot["latencies_ms"]["fact_to_decision_ms"] == pytest.approx(1420.0)


def test_poll_once_source_acquisition_trace_summary_keeps_source_roles():
    awc_trace = build_trace_from_refresh_result(
        {
            "ok": True,
            "status": "updated",
            "event_date": "2026-07-03",
            "station_id": "ZUUU",
            "fact_as_of": "2026-07-03T09:05:36Z",
            "awc_latest_at": "2026-07-03T09:00:00Z",
            "awc_today_max_c": 30.0,
            "fetch_duration_ms": 431,
        },
        source_family="awc",
    )
    wu_trace = build_trace_from_refresh_result(
        {
            "ok": True,
            "status": "updated",
            "event_date": "2026-07-03",
            "station_id": "ZUUU",
            "fact_as_of": "2026-07-03T09:10:02Z",
            "source_features": {
                "wu_twc_observed_intraday_today_max_c": 30.0,
                "wu_twc_observed_intraday_today_max_at": "2026-07-03T09:10:00Z",
                "wu_twc_observed_intraday_fetched_at": "2026-07-03T09:10:02Z",
                "wu_forecast_peak_c": 32.0,
                "wu_forecast_peak_at": "2026-07-03T07:00:00Z",
                "wu_forecast_peak_source": "wu_forecast:test",
            },
        },
        source_family="wu_twc",
    )

    summary = poller._poll_once_source_acquisition_trace_summary(
        awc_result={"ok": True, "status": "updated", "source_acquisition_trace": awc_trace},
        wu_result={"ok": True, "status": "updated", "source_acquisition_trace": wu_trace},
        metar_pws_result={"ok": True, "status": "skipped", "reason": "not_due"},
    )

    assert summary["read_only"] is True
    assert summary["hard_observation_candidate_count"] == 1
    assert summary["diagnostic_current_count"] == 1
    assert summary["forecast_weak_count"] == 1
    assert summary["source_class_counts"][SOURCE_CLASSES["hard_observation"]] == 1
    assert summary["source_class_counts"][SOURCE_CLASSES["diagnostic_current"]] == 1
    assert summary["source_class_counts"][SOURCE_CLASSES["forecast_weak"]] == 1
    assert summary["missing_trace_families"] == ["metar_pws"]
    assert summary["guards"]["places_orders"] is False
    by_source = {row["source_id"]: row for row in summary["source_latency_trace"]}
    assert by_source["wu_twc_observed_intraday"]["hard_eligible"] is False


def test_write_posterior_target_latency_trace_dedupes_same_event(tmp_path):
    result = {
        "ok": True,
        "event_date": "2026-07-02",
        "timings_ms": {"posterior_fast_precheck_ms": 18.0},
        "posterior_target_fast_precheck": {
            "ok": True,
            "stage": "fast_precheck",
            "status": "recorded",
            "fact_id": 103639,
            "fact_as_of": "2026-07-02T06:07:00.611+00:00",
            "target_bucket": "34°C",
            "target_posterior": 0.9999999999987478,
            "bucket_changed": False,
            "probability_changed": False,
            "job_id": 3717,
            "job_enqueued": True,
            "job_enqueue_eligible": True,
            "job_enqueue_attempted": True,
            "job_skipped_reason": "",
            "decision_created_at": "2026-07-02T06:14:19.611+00:00",
        },
    }
    latest_path = tmp_path / "posterior_target_latency_latest.json"
    events_path = tmp_path / "posterior_target_latency_events.jsonl"

    poller._write_posterior_target_latency_trace(
        result,
        latest_path=latest_path,
        events_path=events_path,
    )
    poller._write_posterior_target_latency_trace(
        result,
        latest_path=latest_path,
        events_path=events_path,
    )

    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    event_lines = events_path.read_text(encoding="utf-8").splitlines()
    assert latest["job_id"] == 3717
    assert latest["root_cause_hint"] == "posterior_target_job_enqueued"
    assert len(event_lines) == 1


def test_canonical_target_requires_posterior_ssot_fact_not_top_level_ctp():
    payload = {
        "target_bucket": "31°C",
        "target_bucket_probability_pct": 57.8228912939907,
        "target_bucket_source": "posterior_temperature_distribution",
        "target_bucket_probability_field": "posterior_temperature_distribution.bucket_probabilities",
        "posterior_temperature_distribution_source": "continuous_temperature_posterior_v1_materialized_latest",
        "posterior_target_bucket_selection": {
            "target_bucket": "31°C",
            "target_probability": 0.5271339356156189,
            "target_probability_pct": 52.7133935615619,
        },
        "target_bucket_selection": {
            "target_bucket": "31°C",
            "target_probability": 0.5271339356156189,
            "target_probability_pct": 52.7133935615619,
        },
        "target_realization_capital_plan": {
            "target_bucket": "31°C",
            "target_posterior": 0.5271339356156189,
        },
    }

    bucket, posterior, source = poller._canonical_target_from_payload(payload)

    assert bucket == ""
    assert posterior is None
    assert source == "posterior_ssot_fact_missing"


def test_canonical_target_source_has_no_legacy_target_fallbacks():
    body = inspect.getsource(poller._canonical_target_from_payload)

    assert "_posterior_ssot_fact_target_from_payload" in body
    assert "_top_level_ctp_target_from_payload" not in body
    assert "posterior_target_bucket_selection" not in body
    assert "target_bucket_selection" not in body
    assert "target_realization_capital_plan" not in body
    assert "_top_posterior_row" not in body
    assert "distribution." not in body


def test_canonical_target_prefers_posterior_ssot_fact_over_legacy_copies():
    payload = {
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "32°C",
            "target_probability": 0.72,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            # The composer's physical verdict.  The three-leg plan refuses to
            # build without it -- it will not reverse-engineer weather from raw
            # METAR, so a missing verdict keeps both legs HOLD and the poller
            # returns target_realization_v3_plan_blocked before recording
            # anything.  Production mints this on every SSOT block.
            "strong_physical_evidence": {
                "joint_feature_model_applied": True,
                "top_bucket": "32°C",
                "top_bucket_joint_strong_support": True,
                "bucket_exclusions": [],
            },
        },
        "target_bucket": "31°C",
        "target_bucket_probability_pct": 57.8,
        "target_bucket_source": "posterior_temperature_distribution",
        "target_bucket_probability_field": "posterior_temperature_distribution.bucket_probabilities",
        "posterior_target_bucket_selection": {
            "target_bucket": "30°C",
            "target_probability": 0.51,
        },
    }

    bucket, posterior, source = poller._canonical_target_from_payload(payload)

    assert bucket == "32°C"
    assert posterior == pytest.approx(0.72)
    assert source == "posterior_ssot_fact.target_probability"


def test_decision_ssot_context_from_payload_includes_plan_switch_and_momentum():
    payload = {
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "31°C",
            "target_probability": 0.57,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "phase": "D0_breakout",
            "beta_weak": 0.4,
            "beta_strong": 0.65,
            "hard_mask_reason": "none",
            "bucket_probabilities": [
                {"bucket": "31°C", "probability": 0.57},
                {"bucket": "32°C", "probability": 0.21},
            ],
            # The composer's physical verdict.  The three-leg plan refuses to
            # build without it -- it will not reverse-engineer weather from raw
            # METAR, so a missing verdict keeps both legs HOLD and the poller
            # returns target_realization_v3_plan_blocked before recording
            # anything.  Production mints this on every SSOT block.
            "strong_physical_evidence": {
                "joint_feature_model_applied": True,
                "top_bucket": "31°C",
                "top_bucket_joint_strong_support": True,
                "bucket_exclusions": [],
            },
        },
        "target_realization_capital_plan": {
            "ok": True,
            "version": "target_realization_capital_plan_v2",
            "target_bucket": "31°C",
            "target_posterior": 0.57,
            "second_target_bucket": "32°C",
            "second_target_posterior": 0.21,
            "summary": "按 SSOT 动态目标桶执行。",
            "buy_new_leg": {
                "bucket": "31°C",
                "side": "YES",
                "execution_action": "BUY",
            },
            "sell_old_buy_new_legs": [
                {
                    "bucket": "30°C",
                    "side": "YES",
                    "execution_action": "SELL",
                },
                {
                    "bucket": "31°C",
                    "side": "YES",
                    "execution_action": "BUY",
                },
            ],
        },
        "target_switch_context": {
            "target_bucket": "31°C",
            "candidate_bucket": "32°C",
            "trajectory_momentum_impact": {
                "label": "动量支持目标",
                "trade_push": "31°C 升温兑现增强",
            },
        },
        "temperature_path_distribution": {
            "target_bucket": "31°C",
            "trade_follow": {
                "allowed": True,
                "reason": "multi_window_momentum_confirmed",
            },
        },
    }

    out = poller._decision_ssot_context_from_payload(payload)

    assert out["decision_ssot"] == "posterior_ssot_fact"
    assert out["posterior_ssot_fact"]["target_bucket"] == "31°C"
    assert out["posterior_ssot_target_probability"] == 0.57
    assert out["posterior_ssot_phase"] == "D0_breakout"
    assert out["posterior_ssot_beta_strong"] == 0.65
    assert out["target_realization_capital_plan"]["target_bucket"] == "31°C"
    assert out["target_realization_capital_plan_version"] == (
        "target_realization_capital_plan_v2"
    )
    assert out["capital_plan_target_bucket"] == "31°C"
    assert out["buy_new_leg"]["execution_action"] == "BUY"
    assert out["sell_old_buy_new_legs"][0]["execution_action"] == "SELL"
    assert out["target_switch_context"]["candidate_bucket"] == "32°C"
    assert out["trajectory_momentum_impact"]["label"] == "动量支持目标"
    assert out["temperature_path_trade_follow_allowed"] is True
    assert out["temperature_path_trade_follow_reason"] == "multi_window_momentum_confirmed"


def test_posterior_target_expected_target_marks_posterior_ssot_decision_source():
    target = poller._build_posterior_target_expected_target(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        outcome="32°C",
        row={
            "outcome": "32°C",
            "q_temperature_bucket_posterior": 0.72,
            "best_ask": 0.40,
            "best_bid": 0.39,
            "spread": 0.01,
            "yes_token": "yes-token",
        },
        tick={"best_ask": 0.40, "best_bid": 0.39, "spread": 0.01},
        token={"yes_token": "yes-token"},
        payload={
            "fact_as_of": "2026-07-03T12:05:40+08:00",
            "posterior_ssot_fact": {
                "schema": "bayes_posterior_ssot_fact_v1",
                "state": "available",
                "target_bucket": "32°C",
                "target_probability": 0.72,
                "target_probability_field": "posterior_ssot_fact.target_probability",
                # The composer's physical verdict.  The three-leg plan refuses to
                # build without it -- it will not reverse-engineer weather from raw
                # METAR, so a missing verdict keeps both legs HOLD and the poller
                # returns target_realization_v3_plan_blocked before recording
                # anything.  Production mints this on every SSOT block.
                "strong_physical_evidence": {
                    "joint_feature_model_applied": True,
                    "top_bucket": "32°C",
                    "top_bucket_joint_strong_support": True,
                    "bucket_exclusions": [],
                },
            },
            "posterior_target_bucket_selection": {
                "target_bucket": "32°C",
                "target_probability": 0.72,
                "ssot_source": "posterior_ssot_fact",
            },
        },
        target_bucket="32°C",
        target_posterior=0.72,
        target_source="posterior_ssot_fact.target_probability",
    )

    assert target["decision_ssot"] == "posterior_ssot_fact"
    assert target["posterior_ssot_fact_used"] is True
    assert target["posterior_target_source"] == "posterior_ssot_fact.target_probability"
    assert target["target_bucket"] == "32°C"
    assert target["q_temperature_bucket_posterior"] == pytest.approx(0.72)


def test_ssot_audit_matching_exits_keeps_only_hard_source_yes_old_bucket_orders():
    decisions = [
        {
            "action": "exit",
            "outcome": "31\u00b0C",
            "side": "NO",
            "reason_codes": ["order_submitted"],
            "details": {"hard_source_direct": True, "hard_source_posterior_override": True},
        },
        {
            "action": "exit",
            "outcome": "32\u00b0C",
            "side": "YES",
            "reason_codes": ["order_submitted"],
            "details": {"hard_source_direct": True, "ssot_target_bucket": "33\u00b0C"},
        },
        {
            "action": "exit",
            "outcome": "33\u00b0C",
            "side": "YES",
            "reason_codes": ["order_submitted"],
            "details": {"hard_source_direct": True, "ssot_target_bucket": "33\u00b0C"},
        },
        {
            "action": "exit",
            "outcome": "30\u00b0C",
            "side": "YES",
            "reason_codes": ["order_submitted"],
            "details": {},
        },
    ]

    exits = ssot_audit._matching_exits(decisions, "33\u00b0C")

    assert [item["outcome"] for item in exits] == ["32\u00b0C"]


def test_reap_finished_children_is_nonblocking(monkeypatch):
    calls = [(12345, 0), (0, 0)]

    def fake_waitpid(pid, options):
        assert pid == -1
        assert options == poller.os.WNOHANG
        return calls.pop(0)

    monkeypatch.setattr(poller.os, "waitpid", fake_waitpid)

    assert poller._reap_finished_children() == 1


def test_hard_source_source_merge_drops_stale_precipitation_coverage():
    payload = {
        "weather_likelihood_features": {},
        "precipitation_evidence_coverage": {
            "updated_at": "2026-06-21T18:00:00+00:00",
            "state": "covered_no_strong_precip",
        },
    }

    merged = poller._merge_source_features_into_payload(
        payload,
        {
            "zuuu_metar_raw_awc_latest_at": "2026-06-22T16:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 25.0,
        },
        source_id="zuuu_metar_raw_awc",
        refreshed_at="2026-06-22T16:00:10+00:00",
    )

    assert "precipitation_evidence_coverage" not in merged


def test_wu_twc_observed_merge_replaces_stale_generic_wu_max():
    payload = {
        "weather_likelihood_features": {
            "observed_today_max_c": 28.0,
            "observed_today_max_source": "wu_twc_observed_intraday_current_metric_max_since7am",
            "wu_twc_observed_intraday_today_max_c": 28.0,
            "wu_twc_observed_intraday_today_max_source": (
                "wu_twc_observed_intraday_current_metric_max_since7am"
            ),
        },
        "distribution": [
            {
                "outcome": "25\u00b0C",
                "observed_today_max_c": 28.0,
                "observed_today_max_source": "wu_twc_observed_intraday_current_metric_max_since7am",
            }
        ],
    }
    merged = poller._merge_source_features_into_payload(
        payload,
        {
            "wu_twc_observed_intraday_today_max_c": 25.0,
            "wu_twc_observed_intraday_today_max_source": "wu_twc_observed_intraday_history",
            "wu_twc_observed_intraday_today_max_at": "2026-06-25T00:10:00+00:00",
            "wu_twc_observed_intraday_current_metric_max_since7am_raw_value": 28.0,
        },
        source_id="wu_twc_observed_intraday",
        refreshed_at="2026-06-25T00:40:00+00:00",
    )

    features = merged["weather_likelihood_features"]
    assert features["observed_today_max_c"] == 25.0
    assert features["observed_today_max_source"] == "wu_twc_observed_intraday_history"
    assert features["observed_today_max_at"] == "2026-06-25T00:10:00+00:00"
    assert features["wu_twc_observed_intraday_current_metric_max_since7am_raw_value"] == 28.0
    assert merged["distribution"][0]["observed_today_max_c"] == 25.0
    assert merged["distribution"][0]["observed_today_max_source"] == "wu_twc_observed_intraday_history"
    compact = merged["weather_likelihood_features_compact"]
    assert compact["observed_today_max_c"] == 25.0
    assert compact["wu_twc_observed_intraday_today_max_source"] == "wu_twc_observed_intraday_history"


def test_wu_twc_merge_preserves_awc_momentum_from_compact_payload():
    payload = {
        "weather_likelihood_features": {
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_today_max_c": 33.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-29T07:00:00+00:00",
            "zuuu_metar_raw_awc_latest_at": "2026-06-29T08:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 33.0,
        },
        "weather_likelihood_features_compact": {
            "observed_temp_c": 33.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "temperature_momentum_source": "zuuu_metar_raw_awc",
            "temperature_momentum_available_horizons": "12h,6h,3h,2h,1h",
            "temperature_momentum_effective_windows": "1h",
            "temperature_momentum_12h_c_per_hour": 1.0,
            "temperature_momentum_12h_actual_hours": 12.0,
            "temperature_momentum_12h_requested_hours": 12.0,
            "temperature_momentum_6h_c_per_hour": 1.0,
            "temperature_momentum_6h_actual_hours": 6.0,
            "temperature_momentum_6h_requested_hours": 6.0,
            "temperature_momentum_3h_c_per_hour": 1.0,
            "temperature_momentum_3h_actual_hours": 3.0,
            "temperature_momentum_3h_requested_hours": 3.0,
            "temperature_momentum_2h_c_per_hour": 0.5,
            "temperature_momentum_2h_actual_hours": 2.0,
            "temperature_momentum_2h_requested_hours": 2.0,
            "temperature_momentum_1h_c_per_hour": 0.0,
            "temperature_momentum_1h_actual_hours": 1.0,
            "temperature_momentum_1h_requested_hours": 1.0,
            "temperature_momentum_posterior_gate_allowed": False,
            "temperature_momentum_posterior_gate_state": "unconfirmed",
            "temperature_momentum_posterior_gate_reason": "momentum_state_not_realized",
            "observation_path_primary_source": "zuuu_metar_raw_awc",
        },
        "distribution": [{"outcome": "33\u00b0C", "q_bayes_posterior": 0.7}],
    }

    merged = poller._merge_source_features_into_payload(
        payload,
        {
            "wu_twc_observed_intraday_today_max_c": 32.0,
            "wu_twc_observed_intraday_today_max_source": "wu_twc_observed_intraday_history",
            "wu_twc_observed_intraday_today_max_at": "2026-06-29T08:05:00+00:00",
            "wu_twc_observed_intraday_fetched_at": "2026-06-29T08:06:00+00:00",
        },
        source_id="wu_twc_observed_intraday",
        refreshed_at="2026-06-29T08:06:00+00:00",
    )

    features = merged["weather_likelihood_features"]
    compact = merged["weather_likelihood_features_compact"]
    assert features["temperature_momentum_available_horizons"] == "12h,6h,3h,2h,1h"
    assert features["temperature_momentum_1h_c_per_hour"] == 0.0
    assert "temperature_momentum_45m_c_per_hour" not in features
    assert compact["temperature_momentum_available_horizons"] == "12h,6h,3h,2h,1h"
    assert compact["temperature_momentum_posterior_gate_reason"] == "momentum_state_not_realized"
    assert "temperature_momentum_45m_c_per_hour" not in compact
    assert merged["distribution"][0]["temperature_momentum_1h_c_per_hour"] == 0.0


def test_latest_awc_overlay_restores_momentum_from_history_compact(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug = "highest-temperature-in-chengdu-on-june-29-2026"
    event_date = "2026-06-29"
    older_at = "2026-06-29T08:00:00+00:00"
    newer_at = "2026-06-29T08:05:00+00:00"
    good_compact = {
        "observed_temp_c": 33.0,
        "observed_temp_source": "zuuu_metar_raw_awc",
        "observed_today_max_c": 33.0,
        "observed_today_max_source": "zuuu_metar_raw_awc",
        "zuuu_metar_raw_awc_today_max_c": 33.0,
        "zuuu_metar_raw_awc_today_max_at": older_at,
        "zuuu_metar_raw_awc_latest_at": older_at,
        "zuuu_metar_raw_awc_latest_temp_c": 33.0,
        "zuuu_metar_raw_awc_fetched_at": older_at,
        "temperature_momentum_source": "zuuu_metar_raw_awc",
        "temperature_momentum_available_horizons": "12h,6h,3h,2h,1h",
        "temperature_momentum_effective_windows": "1h",
        "temperature_momentum_12h_c_per_hour": 1.0,
        "temperature_momentum_12h_actual_hours": 12.0,
        "temperature_momentum_12h_requested_hours": 12.0,
        "temperature_momentum_6h_c_per_hour": 1.0,
        "temperature_momentum_6h_actual_hours": 6.0,
        "temperature_momentum_6h_requested_hours": 6.0,
        "temperature_momentum_3h_c_per_hour": 1.0,
        "temperature_momentum_3h_actual_hours": 3.0,
        "temperature_momentum_3h_requested_hours": 3.0,
        "temperature_momentum_2h_c_per_hour": 0.5,
        "temperature_momentum_2h_actual_hours": 2.0,
        "temperature_momentum_2h_requested_hours": 2.0,
        "temperature_momentum_1h_c_per_hour": 0.0,
        "temperature_momentum_1h_actual_hours": 1.0,
        "temperature_momentum_1h_requested_hours": 1.0,
        "temperature_momentum_posterior_gate_reason": "momentum_state_not_realized",
    }
    good_payload = {
        "slug": slug,
        "event_date": event_date,
        "weather_likelihood_features_compact": good_compact,
        "fact_provenance": {"awc_hard_observation_refresh": {"source": "zuuu_metar_raw_awc"}},
    }
    bad_payload = {
        "slug": slug,
        "event_date": event_date,
        "weather_likelihood_features": {
            "observed_temp_c": 33.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        "weather_likelihood_features_compact": {
            "observed_temp_c": 33.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        "fact_provenance": {
            "metar_pws_multi_source_hard_observation_refresh": {
                "source": "metar_pws_multi_source"
            }
        },
    }
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-06-29', ?, ?, ?, ?, ?)
        """,
        (slug, event_date, older_at, json.dumps(good_payload), older_at),
    )
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES('2026-06-29', ?, ?, ?, ?, ?)
        """,
        (slug, event_date, newer_at, json.dumps(bad_payload), newer_at),
    )
    latest_id = conn.execute("SELECT max(id) FROM event_fact_history").fetchone()[0]
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES(?, ?, ?, ?)",
        (slug, event_date, latest_id, newer_at),
    )

    merged = poller._merge_latest_awc_hard_observation_into_payload(
        conn,
        bad_payload,
        slug=slug,
        event_date=event_date,
    )

    features = merged["weather_likelihood_features"]
    compact = merged["weather_likelihood_features_compact"]
    assert features["zuuu_metar_raw_awc_today_max_c"] == 33.0
    assert features["temperature_momentum_available_horizons"] == "12h,6h,3h,2h,1h"
    assert compact["temperature_momentum_1h_c_per_hour"] == 0.0
    assert "temperature_momentum_45m_c_per_hour" not in compact
    assert (
        merged["fact_provenance"]["awc_hard_observation_momentum_merge_fill"]["source"]
        == "hard_source_direct_poller.latest_awc_overlay"
    )


def test_wu_twc_observed_merge_demotes_deformed_intraday_hard_source():
    payload = {
        "weather_likelihood_features": {
            "market_var": "temperature_2m_max",
            "event_date": "2026-06-27",
            "forecast_peak_temp": 27.6,
            "observed_temp": 27.78,
            "observation_hour_local": 15.25,
            "observed_today_max_c": 28.0,
            "observed_today_max_source": "wu_twc_observed_intraday_history",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_today_max_c": 28.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-27T04:00:00+00:00",
            "zuuu_metar_raw_awc_target_event_date": "2026-06-27",
        },
        "market_var": "temperature_2m_max",
        "rounding_rule": "whole_degrees",
        "event_date": "2026-06-27",
        "distribution": [
            {
                "outcome": "27\u00b0C",
                "q_bayes_posterior": 0.0,
                "q_bayes_posterior_pre_hard_source_override": 0.4,
                "q_posterior_observed_constraints_hard_zero": True,
                "q_posterior_observed_constraints_reason_codes": ["observed_today_max_above_bucket"],
            },
            {
                "outcome": "28\u00b0C",
                "q_bayes_posterior": 0.0,
                "q_bayes_posterior_pre_hard_source_override": 0.3,
                "q_posterior_observed_constraints_hard_zero": True,
                "q_posterior_observed_constraints_reason_codes": ["observed_today_max_above_bucket"],
                "observed_today_max_c": 28.0,
                "observed_today_max_source": "wu_twc_observed_intraday_history",
                "observed_today_max_trusted": True,
            },
            {
                "outcome": "29\u00b0C",
                "q_bayes_posterior": 0.5,
                "q_bayes_posterior_pre_hard_source_override": 0.2,
            },
            {
                "outcome": "30\u00b0C",
                "q_bayes_posterior": 0.5,
                "q_bayes_posterior_pre_hard_source_override": 0.1,
            },
        ],
    }

    merged = poller._merge_source_features_into_payload(
        payload,
        {
            "wu_twc_observed_intraday_today_max_c": 29.0,
            "wu_twc_observed_intraday_today_max_source": (
                "wu_twc_observed_intraday_current_metric_max_since7am"
            ),
            "wu_twc_observed_intraday_today_max_at": "2026-06-27T06:36:51+00:00",
            "wu_twc_observed_intraday_current_error": "current_observation_event_date_mismatch",
            "wu_twc_observed_intraday_max_error": "current_observation_event_date_mismatch",
            "weather_source_delay_deformation_alert": {
                "alert": True,
                "codes": ["wu_nowcast_temperature_deformation"],
            },
        },
        source_id="wu_twc_observed_intraday",
        refreshed_at="2026-06-27T06:45:39+00:00",
    )

    features = merged["weather_likelihood_features"]
    assert features["observed_today_max_c"] == 28.0
    assert features["observed_today_max_source"] == "zuuu_metar_raw_awc"
    assert features["observed_today_max_trusted"] is True
    assert features["observed_today_max_hard_source_eligible"] is True
    assert features["wu_twc_observed_intraday_today_max_c"] == 29.0
    assert features["wu_twc_observed_intraday_hard_source_eligible"] is False
    assert features["wu_twc_observed_intraday_hard_source_demoted"] is True
    assert features["wu_twc_observed_intraday_hard_source_demoted_reason"] in {
        "wu_twc_intraday_deformation_alert",
        "wu_twc_intraday_event_date_mismatch",
    }
    assert features["peak_estimator_source"] == "locked_trusted_observed"
    assert features["peak_estimator_mu_c"] == 28.0
    assert features["peak_estimator_raw_observed_today_max_c"] == 28.0
    assert features["peak_estimator_raw_observed_today_max_source"] == "zuuu_metar_raw_awc"
    by_outcome = {row["outcome"]: row for row in merged["distribution"]}
    assert by_outcome["27\u00b0C"]["q_posterior_observed_constraints_hard_zero"] is True
    assert by_outcome["28\u00b0C"]["q_posterior_observed_constraints_hard_zero"] is False
    assert by_outcome["28\u00b0C"]["q_bayes_posterior"] > 0.0
    assert by_outcome["28\u00b0C"]["observed_today_max_trusted"] is True
    assert by_outcome["28\u00b0C"]["observed_today_max_hard_source_eligible"] is True


def test_wu_twc_observed_merge_replaces_stale_generic_wu_current():
    payload = {
        "weather_likelihood_features": {
            "observed_temp": 28.0,
            "observed_temp_c": 28.0,
            "observed_temp_source": "wu_twc_observed_intraday_current",
            "wu_twc_observed_intraday_current_temp_c": 28.0,
        },
        "distribution": [
            {
                "outcome": "25\u00b0C",
                "observed_temp_c": 28.0,
                "observed_temp_source": "wu_twc_observed_intraday_current",
            }
        ],
    }

    merged = poller._merge_source_features_into_payload(
        payload,
        {
            "wu_twc_observed_intraday_current_temp_c": 21.6666666667,
            "wu_twc_observed_intraday_current_at": "2026-06-24T22:59:39+00:00",
        },
        source_id="wu_twc_observed_intraday",
        refreshed_at="2026-06-25T00:45:12+00:00",
    )

    features = merged["weather_likelihood_features"]
    assert round(features["observed_temp_c"], 3) == 21.667
    assert features["observed_temp_source"] == "wu_twc_observed_intraday_current"
    assert round(merged["distribution"][0]["observed_temp_c"], 3) == 21.667


def test_wu_twc_observed_merge_does_not_override_awc_generic_max():
    payload = {
        "weather_likelihood_features": {
            "observed_today_max_c": 26.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
        },
        "distribution": [
            {
                "outcome": "26\u00b0C",
                "observed_today_max_c": 26.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
            }
        ],
    }

    merged = poller._merge_source_features_into_payload(
        payload,
        {
            "wu_twc_observed_intraday_today_max_c": 25.0,
            "wu_twc_observed_intraday_today_max_source": "wu_twc_observed_intraday_history",
        },
        source_id="wu_twc_observed_intraday",
        refreshed_at="2026-06-25T00:40:00+00:00",
    )

    features = merged["weather_likelihood_features"]
    assert features["observed_today_max_c"] == 26.0
    assert features["observed_today_max_source"] == "zuuu_metar_raw_awc"
    assert features["wu_twc_observed_intraday_today_max_c"] == 25.0
    assert merged["distribution"][0]["observed_today_max_c"] == 26.0
    assert merged["distribution"][0]["observed_today_max_source"] == "zuuu_metar_raw_awc"


def test_wu_twc_observed_merge_does_not_override_awc_generic_current():
    payload = {
        "weather_likelihood_features": {
            "observed_temp_c": 22.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
        },
        "distribution": [
            {
                "outcome": "25\u00b0C",
                "observed_temp_c": 22.0,
                "observed_temp_source": "zuuu_metar_raw_awc",
            }
        ],
    }

    merged = poller._merge_source_features_into_payload(
        payload,
        {
            "wu_twc_observed_intraday_current_temp_c": 21.6666666667,
            "wu_twc_observed_intraday_current_at": "2026-06-24T22:59:39+00:00",
        },
        source_id="wu_twc_observed_intraday",
        refreshed_at="2026-06-25T00:45:12+00:00",
    )

    features = merged["weather_likelihood_features"]
    assert features["observed_temp_c"] == 22.0
    assert features["observed_temp_source"] == "zuuu_metar_raw_awc"
    assert round(features["wu_twc_observed_intraday_current_temp_c"], 3) == 21.667
    assert merged["distribution"][0]["observed_temp_c"] == 22.0


def test_wu_twc_fahrenheit_boundary_blocks_final_settlement_signal():
    source_features = {
        "hard_source_settlement_final": True,
        "observed_today_max_c": 32.7777777778,
        "wu_twc_observed_intraday_today_max_c": 32.7777777778,
        "wu_twc_observed_intraday_today_max_source": "wu_twc_observed_intraday_current_imperial_max_since7am",
        "wu_twc_observed_intraday_current_imperial_max_since7am_raw_value": 91,
        "wu_twc_observed_intraday_current_imperial_raw_unit": "F",
        "wu_twc_observed_intraday_fetched_at": "2026-06-22T08:08:01+00:00",
    }

    signal = poller._final_settlement_signal_from_wu_twc_features(
        source_features,
        rounding_rule="whole_degrees",
        settlement_unit="C",
    )

    assert signal["settlement_signal_blocked"] is True
    assert signal["settlement_signal_block_reason"] == "boundary_ambiguous"
    assert "settlement_actual" not in signal

    merged = poller._merge_final_settlement_signal_into_payload(
        {"weather_likelihood_features": {}},
        signal,
    )
    assert "settlement_actual" not in merged
    assert not merged.get("market_closed")
    assert merged["settlement_signal_blocked"] is True
    assert merged["weather_likelihood_features"]["settlement_signal_block_reason"] == "boundary_ambiguous"


def test_wu_twc_intraday_not_used_as_hard_observation_fallback():
    payload = {
        "rounding_rule": "whole_degrees",
        "market_unit": "C",
        "weather_likelihood_features": {
            "wu_twc_observed_intraday_today_max_c": 33.0,
            "wu_twc_observed_intraday_today_max_source": "wu_twc_observed_intraday_history",
            "wu_twc_observed_intraday_stale": False,
        },
    }

    observation = poller._hard_observation_from_payload(payload, allow_wu_twc_fallback=True)

    assert observation["ok"] is False
    assert observation["reason"] == "wu_twc_intraday_auxiliary_not_hard_source"


def test_awc_hard_source_signal_candidate_from_payload_is_read_only():
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "rounding_rule": "whole_degrees",
        "market_unit": "C",
        "weather_likelihood_features": {
            "zuuu_metar_raw_amsc_today_max_c": 30.0,
            "zuuu_metar_raw_amsc_today_max_at": "2026-07-03T09:30:00Z",
            "zuuu_metar_raw_amsc_latest_temp_c": 30.0,
            "zuuu_metar_raw_amsc_latest_at": "2026-07-03T09:30:00Z",
            "zuuu_metar_raw_amsc_fetched_at": "2026-07-03T09:31:37Z",
            "zuuu_metar_raw_amsc_fetch_duration_ms": 210.0,
            "zuuu_metar_raw_amsc_latest_raw": "ZUUU 030930Z ... 30/22",
        },
    }

    candidate = poller.build_awc_hard_source_signal_candidate_from_payload(
        payload,
        candidate_seen_at="2026-07-03T09:31:38Z",
    )

    assert candidate["acceptance_state"] == "accepted"
    assert candidate["signal_eligible"] is True
    assert candidate["read_only"] is True
    assert candidate["emits_signal"] is False
    assert candidate["terminal_reason"] == "read_only_candidate_not_emitted"
    assert candidate["source_id"] == "zuuu_metar_raw_amsc"
    assert candidate["primary_hard_source"] is True
    assert candidate["accepted_signal_source"] is True


def test_hard_source_direct_decision_writes_awc_signal_timing_to_decision_and_job(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    fixed_valid = "2026-06-19T09:00:00Z"
    fixed_seen = "2026-06-19T09:05:38Z"
    fixed_fetch = "2026-06-19T09:05:37Z"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT h.id, h.fact_as_of, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    payload = json.loads(row["fact_json"])
    features = dict(payload["weather_likelihood_features"])
    features["zuuu_metar_raw_awc_today_max_at"] = fixed_valid
    features["zuuu_metar_raw_awc_latest_at"] = fixed_valid
    features["zuuu_metar_raw_awc_fetched_at"] = fixed_fetch
    _set_primary_amsc_hard_source_features(features, valid_at=fixed_valid, fetched_at=fixed_fetch)
    _set_primary_amsc_hard_source_features(features, valid_at=fixed_valid, fetched_at=fixed_fetch)
    payload["weather_likelihood_features"] = features
    conn.execute(
        "UPDATE event_fact_history SET fact_as_of = ?, fact_json = ? WHERE id = ?",
        (fixed_fetch, json.dumps(payload, ensure_ascii=True), row["id"]),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(poller, "_utc_now_iso", lambda: fixed_seen)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_WILL_ORDER", "1")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["status"] == "recorded"
    assert result["job_id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    decision = conn.execute(
        "SELECT details_json FROM auto_trade_decisions WHERE action = 'entry' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    details = json.loads(decision["details_json"])
    assert details["hard_source_acceptance_state"]["acceptance_state"] == "accepted"
    assert details["signal_id"].startswith("sig_")
    assert details["awc_hard_source_signal_at"] == fixed_seen
    assert details["signal_to_order_timing"]["terminal_state"] == "decision_written"
    assert details["signal_to_order_timing"]["signal_to_decision_ms"] == 0.0
    assert (
        details["hard_source_runtime_contract_version"]
        == poller.HARD_SOURCE_RUNTIME_CONTRACT_VERSION
    )
    assert (
        details["hard_source_runtime_contract"]["contract_version"]
        == poller.HARD_SOURCE_RUNTIME_CONTRACT_VERSION
    )

    job = conn.execute(
        "SELECT payload_json FROM auto_trade_jobs WHERE id = ?",
        (int(result["job_id"]),),
    ).fetchone()
    job_payload = json.loads(job["payload_json"])
    target = job_payload["entry_expected_targets"][0]
    assert target["hard_source_acceptance_state"]["acceptance_state"] == "accepted"
    assert target["hard_source_runtime_contract_version"] == poller.HARD_SOURCE_RUNTIME_CONTRACT_VERSION
    assert target["signal_to_order_timing"]["terminal_state"] == "job_enqueued"
    assert target["signal_to_order_timing"]["signal_to_job_enqueue_ms"] == 0.0
    assert job_payload["hard_source_direct"]["signal_id"] == details["signal_id"]
    assert (
        job_payload["hard_source_direct"]["hard_source_runtime_contract_version"]
        == poller.HARD_SOURCE_RUNTIME_CONTRACT_VERSION
    )
    assert job_payload["hard_source_direct"]["signal_to_order_timing"]["terminal_state"] == "job_enqueued"
    conn.close()


def test_hard_source_direct_decision_upserts_signal_to_order_ledger_when_schema_exists(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    _create_signal_to_order_timing_table(db_path)
    _create_awc_hard_source_signals_table(db_path)
    slug, event_date = _insert_fact(db_path)
    fixed_valid = "2026-06-19T09:00:00Z"
    fixed_seen = "2026-06-19T09:05:38Z"
    fixed_fetch = "2026-06-19T09:05:37Z"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT h.id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    payload = json.loads(row["fact_json"])
    features = dict(payload["weather_likelihood_features"])
    features["zuuu_metar_raw_awc_today_max_at"] = fixed_valid
    features["zuuu_metar_raw_awc_latest_at"] = fixed_valid
    features["zuuu_metar_raw_awc_fetched_at"] = fixed_fetch
    _set_primary_amsc_hard_source_features(features, valid_at=fixed_valid, fetched_at=fixed_fetch)
    payload["weather_likelihood_features"] = features
    conn.execute(
        "UPDATE event_fact_history SET fact_as_of = ?, fact_json = ? WHERE id = ?",
        (fixed_fetch, json.dumps(payload, ensure_ascii=True), row["id"]),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(poller, "_utc_now_iso", lambda: fixed_seen)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_WILL_ORDER", "1")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["signal_to_order_ledger_result"]["status"] == "upserted"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ledger = conn.execute("SELECT * FROM signal_to_order_timing").fetchone()
    assert ledger is not None
    assert ledger["signal_id"] == result["signal_to_order_ledger_result"]["signal_id"]
    assert ledger["decision_id"] == str(result["decision_ids"][0])
    assert ledger["awc_hard_source_signal_at"] == fixed_seen
    assert ledger["auto_trade_decision_written_at"] == fixed_seen
    assert ledger["order_job_enqueued_at"] == fixed_seen
    timing = json.loads(ledger["timing_json"])
    assert timing["terminal_state"] == "job_enqueued"
    assert timing["signal_to_job_enqueue_ms"] == 0.0
    signal = conn.execute("SELECT * FROM awc_hard_source_signals").fetchone()
    assert signal is not None
    assert signal["signal_id"] == ledger["signal_id"]
    assert signal["source_event_id"]
    assert signal["slug"] == slug
    assert signal["event_date"] == event_date
    assert signal["awc_valid_time"] == fixed_valid
    assert signal["awc_hard_source_signal_at"] == fixed_seen
    assert signal["acceptance_state"] == "accepted"
    assert signal["terminal_state"] == "job_enqueued"
    assert signal["state_reason"] == "awaiting_order_submit"
    assert result["awc_hard_source_signal_ledger_result"]["status"] == "upserted"
    conn.close()


def test_hard_source_direct_enqueue_disabled_marks_signal_ledger_skipped(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    _create_signal_to_order_timing_table(db_path)
    slug, event_date = _insert_fact(db_path)
    fixed_valid = "2026-06-19T09:00:00Z"
    fixed_seen = "2026-06-19T09:05:38Z"
    fixed_fetch = "2026-06-19T09:05:37Z"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT h.id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    payload = json.loads(row["fact_json"])
    features = dict(payload["weather_likelihood_features"])
    features["zuuu_metar_raw_awc_today_max_at"] = fixed_valid
    features["zuuu_metar_raw_awc_latest_at"] = fixed_valid
    features["zuuu_metar_raw_awc_fetched_at"] = fixed_fetch
    _set_primary_amsc_hard_source_features(features, valid_at=fixed_valid, fetched_at=fixed_fetch)
    payload["weather_likelihood_features"] = features
    conn.execute(
        "UPDATE event_fact_history SET fact_as_of = ?, fact_json = ? WHERE id = ?",
        (fixed_fetch, json.dumps(payload, ensure_ascii=True), row["id"]),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(poller, "_utc_now_iso", lambda: fixed_seen)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_WILL_ORDER", "1")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=False,
    )

    assert result["ok"] is True
    assert result["job_id"] is None
    assert result["signal_to_order_terminal_state"] == "skipped"
    assert result["signal_to_order_terminal_reason"] == "hard_source_direct_enqueue_disabled"
    assert result["signal_to_order_ledger_result"]["status"] == "upserted"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    decision = conn.execute(
        "SELECT details_json FROM auto_trade_decisions WHERE action = 'entry' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    details = json.loads(decision["details_json"])
    assert details["signal_to_order_timing"]["terminal_state"] == "skipped"
    assert details["signal_to_order_slo_met"] is False
    assert details["signal_to_order_slo_violation_reason"] == "hard_source_direct_enqueue_disabled"
    ledger = conn.execute("SELECT * FROM signal_to_order_timing").fetchone()
    timing = json.loads(ledger["timing_json"])
    assert timing["terminal_state"] == "skipped"
    assert timing["terminal_reason"] == "hard_source_direct_enqueue_disabled"
    assert ledger["signal_to_order_slo_violation_reason"] == "hard_source_direct_enqueue_disabled"
    conn.close()


def test_hard_source_direct_will_order_disabled_marks_signal_ledger_no_action(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    _create_signal_to_order_timing_table(db_path)
    slug, event_date = _insert_fact(db_path)
    fixed_valid = "2026-06-19T09:00:00Z"
    fixed_seen = "2026-06-19T09:05:38Z"
    fixed_fetch = "2026-06-19T09:05:37Z"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT h.id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    payload = json.loads(row["fact_json"])
    features = dict(payload["weather_likelihood_features"])
    features["zuuu_metar_raw_awc_today_max_at"] = fixed_valid
    features["zuuu_metar_raw_awc_latest_at"] = fixed_valid
    features["zuuu_metar_raw_awc_fetched_at"] = fixed_fetch
    _set_primary_amsc_hard_source_features(features, valid_at=fixed_valid, fetched_at=fixed_fetch)
    payload["weather_likelihood_features"] = features
    conn.execute(
        "UPDATE event_fact_history SET fact_as_of = ?, fact_json = ? WHERE id = ?",
        (fixed_fetch, json.dumps(payload, ensure_ascii=True), row["id"]),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(poller, "_utc_now_iso", lambda: fixed_seen)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_WILL_ORDER", "0")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["job_id"] is None
    assert result["signal_to_order_terminal_state"] == "no_action"
    assert result["signal_to_order_terminal_reason"] == "hard_source_direct_will_order_disabled"
    assert result["signal_to_order_ledger_result"]["status"] == "upserted"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 0
    decision = conn.execute(
        "SELECT will_order, details_json FROM auto_trade_decisions WHERE action = 'entry' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert int(decision["will_order"]) == 0
    details = json.loads(decision["details_json"])
    assert details["signal_to_order_timing"]["terminal_state"] == "no_action"
    assert details["signal_to_order_slo_met"] is False
    assert details["signal_to_order_slo_violation_reason"] == "hard_source_direct_will_order_disabled"
    ledger = conn.execute("SELECT * FROM signal_to_order_timing").fetchone()
    timing = json.loads(ledger["timing_json"])
    assert timing["terminal_state"] == "no_action"
    assert timing["terminal_reason"] == "hard_source_direct_will_order_disabled"
    assert ledger["signal_to_order_slo_violation_reason"] == "hard_source_direct_will_order_disabled"
    conn.close()


def test_hard_source_direct_rejected_stale_awc_signal_does_not_enqueue_job(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    _create_signal_to_order_timing_table(db_path)
    _create_awc_hard_source_signals_table(db_path)
    slug, event_date = _insert_fact(db_path)
    fixed_valid = "2026-06-19T09:00:00Z"
    fixed_seen = "2026-06-19T09:35:38Z"
    fixed_fetch = "2026-06-19T09:06:37Z"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT h.id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    payload = json.loads(row["fact_json"])
    features = dict(payload["weather_likelihood_features"])
    features["zuuu_metar_raw_awc_today_max_at"] = fixed_valid
    features["zuuu_metar_raw_awc_latest_at"] = fixed_valid
    features["zuuu_metar_raw_awc_fetched_at"] = fixed_fetch
    _set_primary_amsc_hard_source_features(features, valid_at=fixed_valid, fetched_at=fixed_fetch)
    payload["weather_likelihood_features"] = features
    conn.execute(
        "UPDATE event_fact_history SET fact_as_of = ?, fact_json = ? WHERE id = ?",
        (fixed_fetch, json.dumps(payload, ensure_ascii=True), row["id"]),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(poller, "_utc_now_iso", lambda: fixed_seen)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_WILL_ORDER", "1")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["status"] == "hard_source_awc_signal_not_accepted"
    assert result["job_id"] is None
    assert result["decision_ids"] == []
    assert result["hard_source_acceptance_state"]["acceptance_state"] == "rejected"
    assert result["hard_source_acceptance_state"]["acceptance_reason"] == "signal_seen_after_stale_deadline"
    assert result["hard_source_runtime_contract_version"] == poller.HARD_SOURCE_RUNTIME_CONTRACT_VERSION
    assert (
        result["hard_source_runtime_contract"]["contract_version"]
        == poller.HARD_SOURCE_RUNTIME_CONTRACT_VERSION
    )
    assert result["signal_to_order_terminal_state"] == "stale"
    assert result["signal_to_order_terminal_reason"] == "signal_seen_after_stale_deadline"
    assert result["signal_to_order_ledger_result"]["status"] == "not_applicable_no_awc_hard_source_signal"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_to_order_timing").fetchone()[0] == 0
    signal = conn.execute("SELECT * FROM awc_hard_source_signals").fetchone()
    assert signal is not None
    assert signal["slug"] == slug
    assert signal["event_date"] == event_date
    assert signal["awc_valid_time"] == fixed_valid
    assert signal["awc_hard_source_signal_at"] == ""
    assert signal["acceptance_state"] == "rejected"
    assert signal["terminal_state"] == "stale"
    assert signal["state_reason"] == "signal_seen_after_stale_deadline"
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1
    conn.close()


def test_add_hard_source_order_sla_computes_awc_signal_to_order_submit():
    details = {
        "hard_source_direct": True,
        "signal_id": "sig_submit_segments",
        "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
        "signal_to_order_slo_ms": 3000,
        "order_submit_started_at": "2026-07-03T09:05:39.100Z",
        "order_submit_completed_at": "2026-07-03T09:05:39.700Z",
        "order_submit_ms": 600.0,
        "idempotency_key": "clobsvc:test-submit-segments",
        "status": "submitted",
        "exchange_post_started": True,
        "exchange_post_started_at": "2026-07-03T09:05:40Z",
        "signal_to_order_timing": {
            "schema": "signal_to_order_timing_v1",
            "signal_id": "sig_submit_segments",
            "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
            "terminal_state": "worker_started",
        },
    }
    swm._apply_signal_to_order_execution_segments(
        details,
        orderbook_checked_at="2026-07-03T09:05:38.300Z",
        orderbook_check_source="unit_test_orderbook",
        orderbook_check_result="pass",
        risk_checked_at="2026-07-03T09:05:38.900Z",
        risk_check_source="unit_test_risk",
        risk_check_result="pass",
    )

    out = swm._add_hard_source_order_sla(details, "2026-07-03T09:05:40Z")

    assert out["awc_signal_to_order_submit_ms"] == 2000.0
    assert out["signal_to_order_slo_met"] is True
    assert out["signal_to_order_slo_violation_reason"] == ""
    assert out["signal_to_order_timing"]["terminal_state"] == "submitted"
    assert out["signal_to_order_timing"]["order_submitted_at"] == "2026-07-03T09:05:40Z"
    assert out["signal_to_order_timing"]["orderbook_checked_at"] == "2026-07-03T09:05:38.300Z"
    assert out["signal_to_order_timing"]["risk_checked_at"] == "2026-07-03T09:05:38.900Z"
    assert out["signal_to_order_timing"]["idempotency_checked_at"] == "2026-07-03T09:05:39.100Z"
    assert out["signal_to_order_timing"]["submit_requested_at"] == "2026-07-03T09:05:39.100Z"
    assert out["signal_to_order_timing"]["exchange_ack_at"] == "2026-07-03T09:05:39.700Z"
    assert out["signal_to_order_timing"]["order_ack_at"] == "2026-07-03T09:05:39.700Z"
    assert out["signal_to_order_timing"]["idempotency_key"] == "clobsvc:test-submit-segments"
    assert out["signal_to_order_timing"]["order_ack_status"] == "submitted"
    assert out["signal_to_order_timing"]["signal_to_orderbook_ms"] == 300.0
    assert out["signal_to_order_timing"]["orderbook_to_risk_check_ms"] == 600.0
    assert out["signal_to_order_timing"]["risk_check_to_submit_ms"] == 200.0
    assert out["signal_to_order_timing"]["submit_request_to_exchange_ack_ms"] == 600.0


def test_fallback_decision_marks_signal_terminal_blocked_and_upserts_ledger(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    _create_signal_to_order_timing_table(db_path)
    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)
    signal_id = "sig_orderbook_blocked"
    signal_at = "2026-06-19T09:05:38Z"

    written = swm._record_auto_trade_fallback_decisions(
        run_id=None,
        run_date="2026-06-19",
        slug="highest-temperature-in-chengdu-on-june-19-2026",
        event_date="2026-06-19",
        title="Highest temperature in Chengdu on June 19, 2026",
        targets=[
            {
                "strategy": "hard_source_direct",
                "outcome": "31\u00b0C",
                "side": "YES",
                "hard_source_direct": True,
                "signal_id": signal_id,
                "awc_hard_source_signal_at": signal_at,
                "signal_to_order_slo_ms": 3000,
                "signal_to_order_timing": {
                    "schema": "signal_to_order_timing_v1",
                    "signal_id": signal_id,
                    "awc_hard_source_signal_at": signal_at,
                    "terminal_state": "worker_started",
                    "worker_started_at": "2026-06-19T09:05:39Z",
                },
            }
        ],
        reason_code="orderbook_missing",
        reason_text="CLOB orderbook unavailable",
        details={"source": "unit_test", "hard_source_direct": True},
        decided_state="blocked",
        will_order=0,
    )

    assert written == 1
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    decision = conn.execute(
        "SELECT will_order, reason_codes_json, details_json FROM auto_trade_decisions"
    ).fetchone()
    details = json.loads(decision["details_json"])
    assert decision["will_order"] == 0
    assert "orderbook_missing" in json.loads(decision["reason_codes_json"])
    assert details["signal_to_order_terminal_state"] == "blocked"
    assert details["signal_to_order_terminal_reason"] == "orderbook_missing"
    assert details["signal_to_order_slo_met"] is False
    assert details["signal_to_order_slo_violation_reason"] == "orderbook_missing"

    ledger = conn.execute(
        "SELECT * FROM signal_to_order_timing WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    assert ledger is not None
    assert ledger["signal_to_order_slo_violation_reason"] == "orderbook_missing"
    timing = json.loads(ledger["timing_json"])
    assert timing["terminal_state"] == "blocked"
    assert timing["terminal_reason"] == "orderbook_missing"
    assert timing["signal_to_terminal_ms"] is not None


def test_payload_terminal_upsert_marks_runtime_lock_blocked(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    _create_signal_to_order_timing_table(db_path)
    monkeypatch.setattr(swm, "DB_PATH", db_path)
    signal_id = "sig_runtime_lock_busy"
    signal_at = "2026-06-19T09:05:38Z"
    payload = {
        "hard_source_direct": {
            "hard_source_direct": True,
            "signal_id": signal_id,
            "awc_hard_source_signal_at": signal_at,
            "signal_to_order_timing": {
                "schema": "signal_to_order_timing_v1",
                "signal_id": signal_id,
                "awc_hard_source_signal_at": signal_at,
                "terminal_state": "job_enqueued",
            },
        }
    }

    result = swm._upsert_signal_to_order_terminal_for_payload(
        payload,
        terminal_state="blocked",
        terminal_reason="runtime_lock_busy",
        terminal_at="2026-06-19T09:05:39Z",
    )

    assert result[0]["status"] == "upserted"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ledger = conn.execute(
        "SELECT * FROM signal_to_order_timing WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    assert ledger is not None
    timing = json.loads(ledger["timing_json"])
    assert timing["terminal_state"] == "blocked"
    assert timing["terminal_reason"] == "runtime_lock_busy"
    assert timing["signal_to_order_slo_met"] is False


def test_signal_terminal_classifier_covers_stale_and_worker_error():
    assert (
        swm._classify_signal_to_order_terminal_state(
            "decision_stale",
            "posterior freshness expired",
        )
        == "stale"
    )
    assert (
        swm._classify_signal_to_order_terminal_state(
            "event_worker_failed",
            "worker_no_result",
            will_order=True,
        )
        == "error"
    )

    details = {
        "hard_source_direct": True,
        "signal_id": "sig_worker_error",
        "awc_hard_source_signal_at": "2026-06-19T09:05:38Z",
        "signal_to_order_timing": {
            "schema": "signal_to_order_timing_v1",
            "signal_id": "sig_worker_error",
            "awc_hard_source_signal_at": "2026-06-19T09:05:38Z",
            "terminal_state": "worker_started",
        },
    }
    out = swm._apply_signal_to_order_terminal(
        details,
        terminal_state="error",
        terminal_reason="event_worker_failed",
        terminal_at="2026-06-19T09:05:41Z",
    )

    assert out["signal_to_order_terminal_state"] == "error"
    assert out["signal_to_order_slo_met"] is False
    assert out["signal_to_order_slo_violation_reason"] == "event_worker_failed"
    assert out["signal_to_order_timing"]["terminal_state"] == "error"
    assert out["signal_to_terminal_ms"] == 3000.0


def test_signal_to_order_timing_records_pre_signal_preparation_before_deadline():
    timing = poller._signal_to_order_timing_from_candidate(
        {
            "signal_id": "sig_pre_signal_ready",
            "source_event_id": "src_awc_20260703_0900",
            "source_id": "zuuu_metar_raw_awc",
            "acceptance_state": "accepted",
            "acceptance_reason": "awc_hourly_signal_accepted",
            "signal_eligible": True,
            "awc_valid_time": "2026-07-03T09:00:00Z",
            "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
            "window": {
                "hour_boundary_at": "2026-07-03T09:00:00Z",
                "preparation_started_at": "2026-07-03T09:00:00Z",
                "awc_pre_signal_deadline_at": "2026-07-03T09:05:00Z",
                "awc_poll_window_started_at": "2026-07-03T09:05:00Z",
                "awc_poll_window_ended_at": "2026-07-03T09:06:00Z",
                "detection_lag_ms": 38000.0,
            },
        },
        preparation_context={
            "schema": "pre_signal_preparation_context_v1",
            "source": "hard_source_direct_market_prefetch",
            "pre_signal_prepare_due": True,
            "pre_signal_prepare_inline": True,
            "pre_signal_prepare_state": "prepared",
            "pre_signal_prepare_reason": "market_prefetch_refreshed",
            "pre_signal_prepare_started_at": "2026-07-03T09:04:40Z",
            "pre_signal_prepared_at": "2026-07-03T09:04:50Z",
            "pre_signal_prepare_completed_at": "2026-07-03T09:04:50Z",
        },
        decision_started_at="2026-07-03T09:05:38Z",
        decision_written_at="2026-07-03T09:05:38Z",
        terminal_state="decision_written",
        terminal_reason="awaiting_job_enqueue_or_execution",
        terminal_at="2026-07-03T09:05:38Z",
    )

    assert timing["hour_boundary_at"] == "2026-07-03T09:00:00Z"
    assert timing["pre_signal_prepare_state"] == "prepared_before_deadline"
    assert timing["pre_signal_prepare_reason"] == "market_prefetch_refreshed"
    assert timing["pre_signal_prepare_due"] is True
    assert timing["pre_signal_prepare_inline"] is True
    assert timing["pre_signal_prepared_before_deadline"] is True
    assert timing["hour_to_pre_signal_prepared_ms"] == 290000.0
    assert timing["guards"]["pre_signal_places_orders"] is False
    assert timing["guards"]["pre_signal_bypasses_awc_acceptance"] is False


def test_signal_to_order_timing_keeps_partial_preparation_state():
    timing = poller._signal_to_order_timing_from_candidate(
        {
            "signal_id": "sig_pre_signal_partial",
            "source_event_id": "src_awc_20260703_0900",
            "source_id": "zuuu_metar_raw_awc",
            "acceptance_state": "accepted",
            "acceptance_reason": "awc_hourly_signal_accepted",
            "signal_eligible": True,
            "awc_valid_time": "2026-07-03T09:00:00Z",
            "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
            "window": {
                "hour_boundary_at": "2026-07-03T09:00:00Z",
                "preparation_started_at": "2026-07-03T09:00:00Z",
                "awc_pre_signal_deadline_at": "2026-07-03T09:05:00Z",
                "awc_poll_window_started_at": "2026-07-03T09:05:00Z",
                "awc_poll_window_ended_at": "2026-07-03T09:06:00Z",
            },
        },
        preparation_context={
            "schema": "pre_signal_preparation_context_v1",
            "source": "hard_source_direct_market_prefetch",
            "pre_signal_prepare_due": True,
            "pre_signal_prepare_inline": True,
            "pre_signal_prepare_state": "preparation_partial",
            "pre_signal_prepare_reason": "missing_required_components:orderbook",
            "pre_signal_prepare_started_at": "2026-07-03T09:04:40Z",
            "pre_signal_prepared_at": "2026-07-03T09:04:50Z",
            "pre_signal_prepare_completed_at": "2026-07-03T09:04:50Z",
            "pre_signal_prepare_required_components": ["market_prefetch", "orderbook"],
            "pre_signal_prepare_missing_components": ["orderbook"],
            "pre_signal_prepare_components": {
                "market_prefetch": {"ok": True, "state": "prepared"},
                "orderbook": {"ok": False, "state": "missing"},
            },
            "pre_signal_prepare_complete": False,
        },
        decision_started_at="2026-07-03T09:05:38Z",
        decision_written_at="2026-07-03T09:05:38Z",
        terminal_state="decision_written",
        terminal_reason="awaiting_job_enqueue_or_execution",
        terminal_at="2026-07-03T09:05:38Z",
    )

    assert timing["pre_signal_prepare_state"] == "preparation_partial"
    assert timing["pre_signal_prepared_before_deadline"] is True
    assert timing["pre_signal_prepare_complete"] is False
    assert timing["pre_signal_prepare_missing_components"] == ["orderbook"]
    assert timing["pre_signal_prepare_components"]["orderbook"]["state"] == "missing"


def test_pre_signal_component_snapshot_marks_market_components_ready(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE market_tokens(
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            yes_token TEXT,
            no_token TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_market_ticks(
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO market_tokens(slug, event_date, outcome, yes_token, no_token, updated_at)
        VALUES('highest-temperature-in-chengdu-on-july-3-2026', '2026-07-03',
               '32°C', 'yes32', 'no32', '2026-07-03T09:04:50Z')
        """
    )
    conn.execute(
        """
        INSERT INTO event_market_ticks(slug, event_date, outcome, side, created_at)
        VALUES('highest-temperature-in-chengdu-on-july-3-2026', '2026-07-03',
               '32°C', 'YES', '2026-07-03T09:04:50Z')
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv(
        "PM_HARD_SOURCE_DIRECT_PRE_SIGNAL_REQUIRED_COMPONENTS",
        "market_prefetch,market_mapping,orderbook,open_orders,positions,capital_caps",
    )
    monkeypatch.setattr(
        poller.swm,
        "_auto_trade_cached_open_orders_by_token",
        lambda allow_fetch, force_refresh=False: poller.swm._AUTO_TRADE_OPEN_ORDERS_CACHE.update(
            {
                "ts": 1.0,
                "by_token": {},
                "last_source": "unit_test",
                "last_age_sec": 0.0,
            }
        )
        or {},
    )
    monkeypatch.setattr(
        poller.swm,
        "fetch_positions_by_token",
        lambda force=False: poller.swm.POSITIONS_CACHE.update(
            {
                "ts": 1.0,
                "data": {},
                "error": "",
                "last_source": "unit_test",
                "last_age_sec": 0.0,
            }
        )
        or ({}, ""),
    )

    snapshot = poller._pre_signal_preparation_component_snapshot(
        db_path,
        event_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        payload={
            "target_realization_capital_plan": {
                "version": "test-plan-v1",
                "target_bucket": "32°C",
            }
        },
        market_prefetch_result={
            "ok": True,
            "status": "market_prefetch_refreshed",
            "count": 1,
            "created_at": "2026-07-03T09:04:50Z",
        },
    )
    context = poller._pre_signal_preparation_context_from_market_prefetch(
        {
            "ok": True,
            "status": "market_prefetch_refreshed",
            "count": 1,
            "created_at": "2026-07-03T09:04:50Z",
        },
        due=True,
        inline=True,
        started_at="2026-07-03T09:04:40Z",
        completed_at="2026-07-03T09:04:50Z",
        component_snapshot=snapshot,
    )

    assert snapshot["complete"] is True
    assert snapshot["missing_required_components"] == []
    assert snapshot["components"]["market_mapping"]["token_count"] == 1
    assert snapshot["components"]["orderbook"]["tick_count"] == 1
    assert snapshot["components"]["open_orders"]["ok"] is True
    assert snapshot["components"]["positions"]["ok"] is True
    assert context["pre_signal_prepare_state"] == "prepared"
    assert context["pre_signal_prepare_complete"] is True


def test_settlement_actual_used_as_final_hard_observation():
    payload = {
        "settlement_actual": 33.0,
        "settlement_signal_source": "wu_twc_final_settlement",
        "settlement_signal_created_at": "2026-06-22T10:00:00+00:00",
        "weather_likelihood_features": {
            "settlement_actual": 33.0,
            "settlement_actual_source": "wu_twc_final_settlement",
            "market_closed": True,
        },
    }

    observation = poller._hard_observation_from_payload(payload, allow_wu_twc_fallback=False)

    assert observation["ok"] is True
    assert observation["source_id"] == "wu_twc_final_settlement"
    assert observation["value_c"] == 33.0
    assert observation["final_bucket_lock"] is True


def test_event_runs_reverse_fahrenheit_boundary_blocks_final_settlement_signal(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug = "highest-temperature-in-chengdu-on-june-22-2026"
    event_date = "2026-06-22"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO event_runs(
            slug, event_date, settlement_actual, market_closed, rounding_rule,
            market_unit, resolution_source, station_id, created_at
        )
        VALUES(?, ?, ?, 1, 'whole_degrees', 'C',
               'https://www.wunderground.com/history/daily/cn/chengdu/ZUUU',
               'ZUUU', ?)
        """,
        (slug, event_date, 32.7777777778, _utc_fixture_iso(120)),
    )
    conn.commit()

    signal = poller._latest_settlement_signal_from_event_runs(
        conn,
        slug=slug,
        event_date=event_date,
    )

    assert signal["settlement_signal_blocked"] is True
    assert signal["settlement_signal_block_reason"] == "boundary_ambiguous"
    assert signal["settlement_precision_profile"]["boundary_ambiguous"] is True
    assert "settlement_actual" not in signal

    merged = poller._merge_final_settlement_signal_into_payload(
        {"weather_likelihood_features": {}},
        signal,
    )
    assert "settlement_actual" not in merged
    assert not merged.get("market_closed")
    assert merged["weather_likelihood_features"]["settlement_signal_block_reason"] == "boundary_ambiguous"
    conn.close()


def test_hard_source_expected_no_target_uses_no_quote_and_probability():
    target = poller._build_expected_target(
        slug="highest-temperature-in-chengdu-on-june-19-2026",
        event_date="2026-06-19",
        outcome="30\u00b0C",
        observation={"source_id": "zuuu_metar_raw_awc", "value_c": 31.0},
        row={
            "ask": 0.02,
            "best_ask": 0.02,
            "best_bid": 0.01,
            "ask_no": 0.88,
            "best_ask_no": 0.88,
            "best_bid_no": 0.86,
            "q_bayes_posterior": 0.20,
            "q_bayes_posterior_no": 0.77,
            "yes_token": "yes-token",
            "no_token": "no-token",
        },
        tick={},
        token={},
        side="NO",
        leg_role="old_bucket_reverse",
        held_target_bucket="30\u00b0C",
        held_target_side="YES",
    )

    assert target["side"] == "NO"
    assert target["ask"] == 0.88
    assert target["bid"] == 0.86
    assert target["q_bayes_posterior"] == 0.77
    assert target["token_id"] == "no-token"
    assert target["target_realization_leg_role"] == "old_bucket_reverse"
    assert target["target_realization_execution_action"] == "BUY"
    assert target["target_realization_position_effect"] == "open_or_add_old_bucket_opposite_side"
    assert target["sell_old_buy_new_semantics"] == "buy_old_bucket_opposite_side"
    assert target["paired_exit_side"] == "YES"


def test_hard_source_old_zero_position_entry_is_not_active(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        now_iso = _utc_fixture_iso()
        conn.execute(
            """
            INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size, updated_at)
            VALUES(?, ?, ?, 'NO', 'entry', 0.0, ?)
            """,
            (slug, event_date, "30\u00b0C", now_iso),
        )
        conn.commit()
        active = poller._active_old_yes_trades(
            conn,
            slug=slug,
            event_date=event_date,
            target_outcome="31\u00b0C",
        )
    finally:
        conn.close()

    assert [(item["outcome"], item["side"]) for item in active] == [("30\u00b0C", "YES")]


def test_same_bucket_opposite_side_is_active_old_position(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        now_iso = _utc_fixture_iso()
        conn.execute(
            """
            INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size, updated_at)
            VALUES(?, ?, ?, 'NO', 'entry', 4.0, ?)
            """,
            (slug, event_date, "31\u00b0C", now_iso),
        )
        conn.commit()
        active = poller._active_old_yes_trades(
            conn,
            slug=slug,
            event_date=event_date,
            target_outcome="31\u00b0C",
            target_side="YES",
        )
    finally:
        conn.close()

    assert ("31\u00b0C", "NO") in [(item["outcome"], item["side"]) for item in active]


def test_all_non_target_positions_are_old_after_remote_no_retirement(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        now_iso = _utc_fixture_iso()
        conn.executemany(
            """
            INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size, updated_at)
            VALUES(?, ?, ?, ?, 'entry', ?, ?)
            """,
            [
                (slug, event_date, "31\u00b0C", "NO", 4.0, now_iso),
                (slug, event_date, "31\u00b0C", "YES", 4.0, now_iso),
                (slug, event_date, "32\u00b0C", "NO", 5.0, now_iso),
                (slug, event_date, "33\u00b0C", "NO", 6.0, now_iso),
                (slug, event_date, "34\u00b0C", "NO", 7.0, now_iso),
            ],
        )
        conn.commit()
        active = poller._active_old_yes_trades(
            conn,
            slug=slug,
            event_date=event_date,
            target_outcome="31\u00b0C",
            target_side="YES",
        )
    finally:
        conn.close()

    active_pairs = {(item["outcome"], item["side"]) for item in active}
    assert ("30\u00b0C", "YES") in active_pairs
    assert ("31\u00b0C", "NO") in active_pairs
    assert ("31\u00b0C", "YES") not in active_pairs
    assert ("32\u00b0C", "NO") in active_pairs
    assert ("33\u00b0C", "NO") in active_pairs
    assert ("34\u00b0C", "NO") in active_pairs


def _create_schema(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            model_snapshot_id INTEGER,
            market_snapshot_id INTEGER,
            composer_version TEXT,
            market_updated_at TEXT,
            model_updated_at TEXT,
            market_stale_sec REAL,
            model_cache_age_sec REAL,
            best_side TEXT,
            best_outcome TEXT,
            best_edge_score REAL,
            best_signal_edge_entry REAL,
            fact_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_fact_latest (
            slug TEXT,
            event_date TEXT,
            fact_id INTEGER,
            updated_at TEXT,
            PRIMARY KEY(slug, event_date)
        );
        CREATE TABLE event_fact_summary_cache (
            fact_id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            run_date TEXT,
            fact_as_of TEXT,
            summary_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            event_url TEXT,
            title TEXT,
            city TEXT,
            lat REAL,
            lon REAL,
            tz TEXT,
            location_basis TEXT,
            resolution_source TEXT,
            station_id TEXT,
            rounding_rule TEXT,
            market_var TEXT,
            market_unit TEXT,
            market_open_at TEXT,
            settlement_actual REAL,
            city_most_likely_label TEXT,
            city_most_likely_q REAL,
            market_closed INTEGER,
            payload_json TEXT,
            suggestions_json TEXT,
            created_at TEXT
        );
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            title TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            decided_state TEXT,
            will_order INTEGER,
            reason_codes_json TEXT,
            reasons_json TEXT,
            details_json TEXT,
            created_at TEXT
        );
        CREATE TABLE auto_trade_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            source TEXT,
            state TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT,
            lease_until TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retry INTEGER NOT NULL DEFAULT 3,
            next_retry_at TEXT,
            last_error TEXT,
            started_at TEXT,
            ended_at TEXT,
            run_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE auto_trade_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            started_at TEXT,
            ended_at TEXT,
            ok INTEGER,
            error TEXT,
            processed INTEGER,
            skipped INTEGER,
            created_at TEXT
        );
        CREATE TABLE edge_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            state TEXT,
            entry_at TEXT,
            last_order_id TEXT,
            entry_price REAL,
            entry_size REAL,
            last_action TEXT,
            last_action_price REAL,
            last_action_size REAL,
            last_action_order_type TEXT,
            last_action_at TEXT,
            last_status TEXT,
            last_error TEXT,
            exit_reasons TEXT,
            exit_reason_codes TEXT,
            exit_reason_family_codes TEXT,
            exit_triggered_at TEXT,
            stop_loss_probe_at TEXT,
            exit_skip_reason TEXT,
            exit_skip_detail TEXT,
            exit_skip_at TEXT,
            entry_retry_count INTEGER,
            exit_retry_count INTEGER,
            position_size REAL,
            avg_price REAL,
            realized_pnl REAL,
            unrealized_pnl REAL,
            mark_price REAL,
            last_fill_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE market_tokens (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            outcome TEXT NOT NULL,
            yes_token TEXT,
            no_token TEXT,
            outcome_volume REAL,
            liquidity REAL,
            market_active INTEGER,
            market_closed INTEGER,
            market_archived INTEGER,
            market_accepting_orders INTEGER,
            market_enable_orderbook INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(slug, event_date, outcome)
        );
        CREATE TABLE event_market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            mid REAL,
            spread REAL,
            bid_depth_topN REAL,
            ask_depth_topN REAL,
            source TEXT,
            source_detail TEXT,
            created_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _create_signal_to_order_timing_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        columns = []
        for column in SIGNAL_TO_ORDER_TIMING_COLUMNS:
            suffix = " PRIMARY KEY" if column == "signal_id" else ""
            columns.append(f"{column} TEXT{suffix}")
        conn.execute(f"CREATE TABLE signal_to_order_timing ({', '.join(columns)})")
        conn.commit()
    finally:
        conn.close()


def _create_awc_hard_source_signals_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        columns = []
        for column in AWC_HARD_SOURCE_SIGNAL_COLUMNS:
            suffix = " PRIMARY KEY" if column == "signal_id" else ""
            columns.append(f"{column} TEXT{suffix}")
        conn.execute(f"CREATE TABLE awc_hard_source_signals ({', '.join(columns)})")
        conn.commit()
    finally:
        conn.close()


def _insert_fact(db_path: Path) -> tuple[str, str]:
    slug = "highest-temperature-in-chengdu-on-june-19-2026"
    event_date = "2026-06-19"
    now_iso = _utc_fixture_iso()
    tick_iso = _utc_fixture_iso(1)
    payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": "2026-06-18",
        "title": "Highest temperature in Chengdu on June 19, 2026",
        "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "station_id": "ZUUU",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "rules_contract_ok": True,
        "rules_missing_reasons": [],
        "weather_likelihood_features": {
            "zuuu_metar_raw_amsc_today_max_c": 31.0,
            "zuuu_metar_raw_amsc_today_max_at": now_iso,
            "zuuu_metar_raw_amsc_latest_temp_c": 31.0,
            "zuuu_metar_raw_amsc_latest_at": now_iso,
            "zuuu_metar_raw_amsc_fetched_at": now_iso,
            "zuuu_metar_raw_amsc_fetch_duration_ms": 42,
            "zuuu_metar_raw_amsc_latest_raw": "ZUUU 190900Z ... 31/22",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": now_iso,
            "zuuu_metar_raw_awc_latest_at": now_iso,
            "zuuu_metar_raw_awc_fetched_at": now_iso,
            "zuuu_metar_raw_awc_fetch_duration_ms": 42,
            "wu_forecast_peak_at": "2026-06-19T07:15:00+00:00",
            "wu_forecast_peak_time_source": "wunderground_hourly_forecast",
        },
        "distribution": [
            {
                "outcome": "30\u00b0C",
                "q_entry": 0.7,
                "q_round": 0.7,
                "q_bayes_posterior": 0.7,
                "q_temperature_bucket_posterior": 0.7,
                "best_bid": 0.4,
                "best_ask": 0.43,
                "spread": 0.03,
                "yes_token": "yes30-token",
                "no_token": "no30-token",
                "market_active": True,
                "market_closed": False,
                "market_archived": False,
                "market_accepting_orders": True,
                "market_enable_orderbook": True,
            },
            {
                "outcome": "31\u00b0C",
                "q_entry": 0.2,
                "q_round": 0.2,
                "q_bayes_posterior": 0.2,
                "q_temperature_bucket_posterior": 0.2,
                "best_bid": 0.2,
                "best_ask": 0.23,
                "spread": 0.03,
                "yes_token": "yes31-token",
                "no_token": "no31-token",
                "market_active": True,
                "market_closed": False,
                "market_archived": False,
                "market_accepting_orders": True,
                "market_enable_orderbook": True,
            },
        ],
    }
    _attach_unit_posterior_ssot_fact(payload, target_bucket="30\u00b0C", target_probability=0.7)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-18",
            slug,
            event_date,
            now_iso,
            json.dumps(payload),
            now_iso,
        ),
    )
    fact_id = cur.lastrowid
    cur.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES(?, ?, ?, ?)",
        (slug, event_date, fact_id, now_iso),
    )
    cur.execute(
        """
        INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size, last_order_id, updated_at)
        VALUES(?, ?, ?, 'YES', 'entry', 8.0, 'old-order-30', ?)
        """,
        (slug, event_date, "30\u00b0C", now_iso),
    )
    cur.execute(
        """
        INSERT INTO market_tokens(slug, event_date, outcome, yes_token, no_token, market_active,
                                  market_closed, market_archived, market_accepting_orders,
                                  market_enable_orderbook, updated_at)
        VALUES(?, ?, ?, ?, ?, 1, 0, 0, 1, 1, ?)
        """,
        (slug, event_date, "30\u00b0C", "yes30-token", "no30-token", now_iso),
    )
    cur.execute(
        """
        INSERT INTO market_tokens(slug, event_date, outcome, yes_token, no_token, market_active,
                                  market_closed, market_archived, market_accepting_orders,
                                  market_enable_orderbook, updated_at)
        VALUES(?, ?, ?, ?, ?, 1, 0, 0, 1, 1, ?)
        """,
        (slug, event_date, "31\u00b0C", "yes31-token", "no31-token", now_iso),
    )
    cur.execute(
        """
        INSERT INTO event_market_ticks(run_date, event_date, slug, outcome, side,
                                       best_bid, best_ask, mid, spread,
                                       bid_depth_topN, ask_depth_topN, source, created_at)
        VALUES(?, ?, ?, ?, 'YES', 0.21, 0.24, 0.225, 0.03, 100, 120, 'clob_rest_refresh', ?)
        """,
        ("2026-06-18", event_date, slug, "31\u00b0C", tick_iso),
    )
    cur.execute(
        """
        INSERT INTO event_market_ticks(run_date, event_date, slug, outcome, side,
                                       best_bid, best_ask, mid, spread,
                                       bid_depth_topN, ask_depth_topN, source, created_at)
        VALUES(?, ?, ?, ?, 'YES', 0.39, 0.42, 0.405, 0.03, 100, 120, 'clob_rest_refresh', ?)
        """,
        ("2026-06-18", event_date, slug, "30\u00b0C", tick_iso),
    )
    conn.commit()
    conn.close()
    return slug, event_date


def test_posterior_target_direct_freezes_existing_positions_before_v3_activation(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_TARGET_REALIZATION_V3_ACTIVATION_DATE_ET", "2099-01-01")

    first = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        enqueue_job=True,
    )
    second = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        enqueue_job=True,
    )

    assert first["status"] == "d1_existing_positions_frozen"
    assert first["existing_positions_adjusted"] is False
    assert first["job_enqueued"] is False
    assert second["decision_written"] is False
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 0
        row = conn.execute(
            "SELECT action, decided_state, will_order, reason_codes_json, details_json FROM auto_trade_decisions"
        ).fetchone()
        assert row[0] == "hold"
        assert row[1] == "frozen"
        assert row[2] == 0
        assert "target_realization_d1_existing_positions_frozen" in row[3]
        assert json.loads(row[4])["existing_positions_adjusted"] is False
        trade = conn.execute(
            "SELECT exit_skip_reason, exit_skip_detail, exit_skip_at FROM edge_trades WHERE slug=? AND event_date=?",
            (slug, event_date),
        ).fetchone()
        assert trade[0] == "target_realization_d1_existing_positions_frozen"
        assert json.loads(trade[1])["no_position_or_order_mutation"] is True
        assert trade[2]
    finally:
        conn.close()


def test_dynamic_three_leg_reselection_enqueues_managed_old_position_exit_before_any_buy(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_outcome="31°C",
        target_probability=0.62,
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO edge_trades(
            slug, event_date, outcome, side, state,
            entry_price, entry_size, position_size, avg_price, last_order_id, updated_at
        ) VALUES(?, ?, '31°C', 'NO', 'entry', 0.70, 5.0, 5.0, 0.70, 'managed-old-no', ?)
        """,
        (slug, event_date, _utc_fixture_iso()),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_orders(
            order_id TEXT, slug TEXT, event_date TEXT, outcome TEXT, side TEXT,
            action TEXT, strategy TEXT, created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO auto_orders(order_id, slug, event_date, outcome, side, action, strategy, created_at)
        VALUES('managed-old-no', ?, ?, '31°C', 'NO', 'entry', 'posterior_settlement', ?)
        """,
        (slug, event_date, _utc_fixture_iso()),
    )
    conn.execute(
        """
        INSERT INTO event_market_ticks(
            run_date, event_date, slug, outcome, side,
            best_bid, best_ask, bid_depth_topN, ask_depth_topN, source, created_at
        ) VALUES('2026-06-18', ?, ?, '31°C', 'NO', 0.74, 0.76, 100, 100, 'test', ?)
        """,
        (event_date, slug, _utc_fixture_iso(2)),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PM_TARGET_REALIZATION_V3_ENABLED", "1")
    monkeypatch.setenv("PM_TARGET_REALIZATION_V3_ACTIVATION_DATE_ET", "2000-01-01")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        enqueue_job=True,
    )

    assert result["target_realization_capital_plan_version"] == "strong_posterior_three_leg_strategy_v2"
    assert result["target_realization_v3_execution_phase"] == "SELL_OLD"
    assert result["sell_target_count"] >= 1
    assert result["job_enqueued"] is True
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute("SELECT payload_json FROM auto_trade_jobs WHERE id=?", (result["job_id"],)).fetchone()
        payload = json.loads(job["payload_json"])
        assert payload["target_realization_v3"]["execution_phase"] == "SELL_OLD"
        assert payload["target_realization_v3"]["sell_old_before_buy_new"] is True
        assert payload["entry_expected_targets"]
        assert all(
            target["target_realization_execution_action"] == "SELL"
            for target in payload["entry_expected_targets"]
        )
        assert all(target["order_type"] == "GTC" for target in payload["entry_expected_targets"])
        assert all(target["post_only"] is True for target in payload["entry_expected_targets"])
        no_exit = next(
            target
            for target in payload["entry_expected_targets"]
            if target["outcome"] == "31°C" and target["side"] == "NO"
        )
        assert no_exit["limit_price"] == 0.76
        assert no_exit["target_realization_price_source"] == "best_ask"
        decisions = conn.execute(
            "SELECT action, reason_codes_json FROM auto_trade_decisions ORDER BY id"
        ).fetchall()
        assert any(
            row["action"] == "exit"
            and "target_realization_no_invalidated" in row["reason_codes_json"]
            for row in decisions
        )
    finally:
        conn.close()




def test_removed_recycle_plan_fields_have_no_target_builder() -> None:
    assert not hasattr(poller, "_target_realization_v3_recycle_target")


def test_target_realization_v3_buy_job_preserves_all_ready_leg_deltas(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    now_iso = _utc_fixture_iso()
    conn.execute(
        """
        INSERT INTO event_market_ticks(
            run_date, event_date, slug, outcome, side,
            best_bid, best_ask, bid_depth_topN, ask_depth_topN, source, created_at
        ) VALUES('2026-06-18', ?, ?, '31°C', 'NO', 0.74, 0.76, 100, 100, 'test', ?)
        """,
        (event_date, slug, now_iso),
    )
    conn.commit()
    plan = {
        "ok": True,
        "version": "strong_posterior_three_leg_strategy_v2",
        "plan_id": "trv3-buy-two",
        "target_bucket": "30°C",
        "target_posterior": 0.70,
        "legs": [
            {
                "role": "target_yes_trading_leg",
                "bucket": "30°C",
                "side": "YES",
                "posterior": 0.70,
                "target_notional": 12.0,
                "delta_notional": 7.0,
                "action": "add",
                "execution_state": "ready",
                "block_reason": "",
            },
            {
                "role": "strongly_excluded_no",
                "bucket": "31°C",
                "side": "NO",
                "posterior": 0.20,
                "target_notional": 20.0,
                "delta_notional": 11.0,
                "action": "add",
                "execution_state": "ready",
                "block_reason": "",
            },
        ],
        "sell_old_legs": [],
    }
    snapshot = {
        "slug": slug,
        "event_date": event_date,
        "run_date": "2026-06-18",
        "title": "Chengdu high",
        "target_realization_capital_plan": plan,
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "30°C",
            "target_probability": 0.70,
            "fact_snapshot_id": "fact-buy-two",
            "posterior_run_id": "run-buy-two",
            # The composer's physical verdict.  The three-leg plan refuses to
            # build without it -- it will not reverse-engineer weather from raw
            # METAR, so a missing verdict keeps both legs HOLD and the poller
            # returns target_realization_v3_plan_blocked before recording
            # anything.  Production mints this on every SSOT block.
            "strong_physical_evidence": {
                "joint_feature_model_applied": True,
                "top_bucket": "30°C",
                "top_bucket_joint_strong_support": True,
                "bucket_exclusions": [],
            },
        },
        "rows": [
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.70},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.20},
        ],
    }
    result = poller._record_target_realization_v3_plan_decisions(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date=event_date,
        snapshot_payload=snapshot,
        plan=plan,
        fact_id=1,
        fact_as_of=now_iso,
        enqueue_job=True,
    )

    assert result["target_realization_v3_execution_phase"] == "BUY_NEW"
    assert result["buy_target_count"] == 2
    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM auto_trade_jobs WHERE id=?", (result["job_id"],)
        ).fetchone()[0]
    )
    targets = {
        (target["outcome"], target["side"]): target
        for target in payload["entry_expected_targets"]
    }
    assert len(targets) == 2
    assert targets[("30°C", "YES")]["target_realization_plan_notional"] == 7.0
    assert targets[("31°C", "NO")]["target_realization_plan_notional"] == 11.0
    assert targets[("30°C", "YES")]["q_entry"] == 0.70
    assert targets[("31°C", "NO")]["q_entry"] == 0.80
    assert targets[("30°C", "YES")]["limit_price"] == 0.39
    assert targets[("31°C", "NO")]["limit_price"] == 0.74
    assert all(target["post_only"] is True for target in targets.values())
    assert all(target["order_type"] == "GTC" for target in targets.values())
    assert payload["posterior_target_direct"]["primary_only_fast_path_allowed"] is False
    conn.close()


def test_posterior_target_direct_v3_fill_transition_cannot_reset_stale_observation_timer(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE event_fact_history SET fact_as_of='2000-01-01T00:00:00+00:00'"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PM_TARGET_REALIZATION_V3_ENABLED", "1")
    monkeypatch.setenv("PM_TARGET_REALIZATION_V3_ACTIVATION_DATE_ET", "2000-01-01")
    transition_at = _utc_fixture_iso()

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        enqueue_job=True,
        transition_signal_at=transition_at,
    )

    # A fill may get its own execution timer only after the originating
    # observation passed T1-based freshness.  It cannot turn a 26-year-old
    # physical report into a fresh BUY signal by resetting T3 alone.
    assert result["status"] == "sequential_bayes_execution_contract_blocked"


def _rewrite_posterior_target_probability(
    db_path: Path,
    *,
    slug: str,
    event_date: str,
    target_outcome: str = "31\u00b0C",
    target_probability: float = 0.62,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT h.id, h.fact_as_of, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    payload = json.loads(latest["fact_json"])
    for row in payload["distribution"]:
        if row["outcome"] == target_outcome:
            row["q_temperature_bucket_posterior"] = target_probability
            row["q_bayes_posterior"] = target_probability
            row["best_bid"] = 0.21
            row["best_ask"] = 0.24
        else:
            row["q_temperature_bucket_posterior"] = 1.0 - target_probability
            row["q_bayes_posterior"] = 1.0 - target_probability
    payload["posterior_target_bucket_selection"] = {
        "target_bucket": target_outcome,
        "target_probability": target_probability,
        "target_probability_pct": target_probability * 100.0,
    }
    payload["target_realization_capital_plan"] = {
        "ok": True,
        "version": "target_realization_capital_plan_v2",
        "target_bucket": target_outcome,
        "target_posterior": target_probability,
        "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "temperature_path_trade_follow_allowed": True,
        "temperature_path_trade_follow_reason": (
            "posterior_path_reliability_and_freshness_pass"
        ),
        "buy_new_leg": {
            "outcome": target_outcome,
            "side": "YES",
            "target_realization_execution_action": "BUY",
            "leg_enabled": True,
        },
    }
    _attach_unit_posterior_ssot_fact(
        payload,
        target_bucket=target_outcome,
        target_probability=target_probability,
    )
    conn.execute(
        "UPDATE event_fact_history SET fact_json = ? WHERE id = ?",
        (json.dumps(payload), latest["id"]),
    )
    conn.commit()
    conn.close()


def _set_target_market_price(
    db_path: Path,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    best_bid: float,
    best_ask: float,
    local_peak_hour: float = 16.0,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT h.id, h.fact_as_of, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    payload = json.loads(latest["fact_json"])
    for row in payload["distribution"]:
        if row["outcome"] != outcome:
            continue
        row["best_bid"] = best_bid
        row["best_ask"] = best_ask
        row["ask"] = best_ask
        row["spread"] = max(0.0, best_ask - best_bid)
        row["local_peak_hour"] = local_peak_hour
    conn.execute(
        "UPDATE event_fact_history SET fact_json = ? WHERE id = ?",
        (json.dumps(payload), latest["id"]),
    )
    conn.execute(
        """
        UPDATE event_market_ticks
        SET best_bid = ?, best_ask = ?, mid = ?, spread = ?
        WHERE slug = ? AND event_date = ? AND outcome = ? AND side = 'YES'
        """,
        (
            best_bid,
            best_ask,
            (best_bid + best_ask) / 2.0,
            max(0.0, best_ask - best_bid),
            slug,
            event_date,
            outcome,
        ),
    )
    conn.commit()
    conn.close()


def _insert_nonfinal_switch_fact(db_path: Path) -> tuple[str, str]:
    slug = "highest-temperature-in-chengdu-on-june-19-2026"
    event_date = "2026-06-19"
    now_iso = _utc_fixture_iso()
    tick_iso = _utc_fixture_iso(1)
    payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": "2026-06-18",
        "title": "Highest temperature in Chengdu on June 19, 2026",
        "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        "station_id": "ZUUU",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "rules_contract_ok": True,
        "rules_missing_reasons": [],
        "weather_likelihood_features": {
            "zuuu_metar_raw_awc_today_max_c": 32.0,
            "zuuu_metar_raw_awc_today_max_at": now_iso,
            "zuuu_metar_raw_awc_latest_at": now_iso,
            "zuuu_metar_raw_awc_fetched_at": now_iso,
            "zuuu_metar_raw_awc_fetch_duration_ms": 42,
            "wu_forecast_peak_at": _utc_fixture_iso(1800),
            "wu_forecast_peak_time_source": "wunderground_hourly_forecast",
        },
        "distribution": [
            {
                "outcome": "31\u00b0C",
                "q_entry": 0.05,
                "q_round": 0.05,
                "best_bid": 0.05,
                "best_ask": 0.08,
                "spread": 0.03,
                "yes_token": "yes31-token",
                "no_token": "no31-token",
                "market_active": True,
                "market_closed": False,
                "market_archived": False,
                "market_accepting_orders": True,
                "market_enable_orderbook": True,
            },
            {
                "outcome": "32\u00b0C",
                "q_entry": 0.47,
                "q_round": 0.47,
                "best_bid": 0.18,
                "best_ask": 0.22,
                "spread": 0.04,
                "yes_token": "yes32-token",
                "no_token": "no32-token",
                "market_active": True,
                "market_closed": False,
                "market_archived": False,
                "market_accepting_orders": True,
                "market_enable_orderbook": True,
            },
            {
                "outcome": "33\u00b0C",
                "q_entry": 0.48,
                "q_round": 0.48,
                "best_bid": 0.45,
                "best_ask": 0.52,
                "spread": 0.07,
                "yes_token": "yes33-token",
                "no_token": "no33-token",
                "market_active": True,
                "market_closed": False,
                "market_archived": False,
                "market_accepting_orders": True,
                "market_enable_orderbook": True,
            },
        ],
    }
    _attach_unit_posterior_ssot_fact(
        payload,
        target_bucket="31\u00b0C",
        target_probability=0.62,
    )
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-18",
            slug,
            event_date,
            now_iso,
            json.dumps(payload),
            now_iso,
        ),
    )
    fact_id = cur.lastrowid
    cur.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES(?, ?, ?, ?)",
        (slug, event_date, fact_id, now_iso),
    )
    cur.execute(
        """
        INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size,
                                last_order_id, avg_price, updated_at)
        VALUES(?, ?, ?, 'YES', 'entry', 8.0, 'old-order-32', 0.25, ?)
        """,
        (slug, event_date, "32\u00b0C", now_iso),
    )
    for outcome, yes_token, no_token in (
        ("31\u00b0C", "yes31-token", "no31-token"),
        ("32\u00b0C", "yes32-token", "no32-token"),
        ("33\u00b0C", "yes33-token", "no33-token"),
    ):
        cur.execute(
            """
            INSERT INTO market_tokens(slug, event_date, outcome, yes_token, no_token, market_active,
                                      market_closed, market_archived, market_accepting_orders,
                                      market_enable_orderbook, updated_at)
            VALUES(?, ?, ?, ?, ?, 1, 0, 0, 1, 1, ?)
            """,
            (slug, event_date, outcome, yes_token, no_token, now_iso),
        )
    for outcome, bid, ask in (
        ("32\u00b0C", 0.18, 0.22),
        ("33\u00b0C", 0.45, 0.52),
    ):
        cur.execute(
            """
            INSERT INTO event_market_ticks(run_date, event_date, slug, outcome, side,
                                           best_bid, best_ask, mid, spread,
                                           bid_depth_topN, ask_depth_topN, source, created_at)
            VALUES(?, ?, ?, ?, 'YES', ?, ?, ?, ?, 100, 120, 'clob_rest_refresh', ?)
            """,
            (
                "2026-06-18",
                event_date,
                slug,
                outcome,
                bid,
                ask,
                (bid + ask) / 2,
                ask - bid,
                tick_iso,
            ),
        )
    conn.commit()
    conn.close()
    return slug, event_date


def test_hard_source_direct_records_target_entry_exit_and_job(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn_seed = sqlite3.connect(db_path)
    conn_seed.row_factory = sqlite3.Row
    latest_seed = conn_seed.execute("SELECT * FROM event_fact_history ORDER BY id DESC LIMIT 1").fetchone()
    stale_payload = json.loads(latest_seed["fact_json"])
    stale_payload["temperature_path_distribution"] = {
        "target_bucket": "30\u00b0C",
        "trade_follow": {"allowed": True, "reason": "stale_pre_hard_source"},
    }
    stale_payload["target_realization_capital_plan"] = {
        "ok": True,
        "target_bucket": "30\u00b0C",
        "legs": [
            {
                "role": "target_yes",
                "bucket": "30\u00b0C",
                "side": "YES",
                "action": "add",
                "target_notional": 99.0,
            }
        ],
    }
    stale_payload["target_switch_context"] = {"target_bucket": "30\u00b0C"}
    stale_payload["auto_trade_ssot"] = {"main_candidate": {"outcome": "30\u00b0C"}}
    conn_seed.execute(
        "UPDATE event_fact_history SET fact_json = ? WHERE id = ?",
        (json.dumps(stale_payload), latest_seed["id"]),
    )
    conn_seed.commit()
    conn_seed.close()
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["target_bucket"] == "31\u00b0C"
    assert result["old_exit_count"] == 1
    assert result["ssot_fact_id"]
    assert result["ssot_write_ms"] is not None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest_fact = conn.execute(
        """
        SELECT h.*
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    assert latest_fact["id"] == result["ssot_fact_id"]
    latest_payload = json.loads(latest_fact["fact_json"])
    latest_by_outcome = {row["outcome"]: row for row in latest_payload["rows"]}
    assert latest_payload["hard_source_direct_ssot_write"] is True
    assert (
        latest_payload["hard_source_posterior_override_meta"]["window_source"]
        == "hard_source_direct_poller_decision_window"
    )
    assert latest_payload["ssot_target_bucket"] == "31\u00b0C"
    latest_path_distribution = latest_payload.get("temperature_path_distribution") or {}
    assert latest_path_distribution.get("target_bucket") == "31\u00b0C"
    assert (
        (latest_path_distribution.get("trade_follow") or {}).get("reason")
        != "stale_pre_hard_source"
    )
    assert "target_switch_context" not in latest_payload
    latest_ssot = latest_payload.get("auto_trade_ssot") or {}
    latest_main = latest_ssot.get("main_candidate") if isinstance(latest_ssot, dict) else {}
    assert str((latest_main or {}).get("outcome") or "") != "30\u00b0C"
    assert latest_payload.get("target_realization_capital_plan", {}).get("target_bucket") != "30\u00b0C"
    assert latest_payload["hard_source_direct_timing"]["ssot_written_at"]
    assert latest_payload["hard_source_direct_timing"]["fetch_to_ssot_ms"] is not None
    assert latest_by_outcome["30\u00b0C"]["q_posterior_observed_constraints_hard_zero"] is True
    assert latest_by_outcome["30\u00b0C"]["q_bayes_posterior"] == 0.0
    assert latest_by_outcome["30\u00b0C"]["hard_source_direct"] is True
    assert latest_by_outcome["30\u00b0C"]["hard_source_ssot_written_at"]
    assert latest_by_outcome["31\u00b0C"]["q_bayes_posterior"] > 0.99
    decisions = conn.execute(
        "SELECT * FROM auto_trade_decisions ORDER BY id"
    ).fetchall()
    assert [(row["action"], row["outcome"], row["side"]) for row in decisions] == [
        ("entry", "31\u00b0C", "YES"),
        ("exit", "30\u00b0C", "YES"),
    ]
    entry_details = json.loads(decisions[0]["details_json"])
    assert entry_details["bayes_recomputed_async"] is True
    assert entry_details["decision_basis"] == "hard_source_lower_bound_constraint"
    assert entry_details["hard_source_posterior_override"] is True
    assert entry_details["posterior_override_q"] > 0.99
    assert entry_details["q_bayes_posterior"] > 0.99
    assert entry_details["target_bucket"] == "31\u00b0C"
    assert entry_details["hard_source_observed_bucket"] == "31\u00b0C"
    assert entry_details["ssot_target_bucket"] == "31\u00b0C"
    assert entry_details["hard_source_ssot_written_at"]
    assert entry_details["hard_source_ssot_write_ms"] is not None
    assert entry_details["hard_source_fetch_to_ssot_ms"] is not None
    assert entry_details["entry_usable_edge"] == 1.0
    assert entry_details["entry_usable_edge_size_scale"] == 1.0
    assert entry_details["entry_usable_edge_base_source"] == "hard_source_direct_ssot_target"
    expected = entry_details["expected_target"]
    assert expected["hard_source_direct"] is True
    assert expected["hard_source_posterior_override"] is True
    assert expected["posterior_override"] is True
    assert expected["q_bayes_posterior"] > 0.99
    assert expected["q_temperature_bucket_posterior"] > 0.99
    assert expected["target_adjustment_action"] == "HARD_SOURCE_BUCKET_OVERRIDE"
    assert expected["hard_source_ssot_written_at"] == entry_details["hard_source_ssot_written_at"]
    assert expected["hard_source_ssot_write_ms"] == entry_details["hard_source_ssot_write_ms"]
    assert expected["q_entry"] > 0.99
    assert expected["ask"] == 0.24
    assert expected["target_realization_execution_action"] == "BUY"
    assert expected["sell_old_buy_new_semantics"] == "buy_new_bucket_side"
    assert expected["entry_usable_edge"] == 1.0
    assert expected["entry_usable_edge_size_scale"] == 1.0
    exit_details = json.loads(decisions[1]["details_json"])
    assert json.loads(decisions[1]["reason_codes_json"]) == [
        "target_realization_switch_buy_exit_old"
    ]
    assert exit_details["target_realization_execution_action"] == "SELL"
    assert exit_details["target_realization_position_effect"] == "close_old_bucket_side"
    assert exit_details["exit_policy_version"]
    assert exit_details["exit_policy_primary_source"] == "target_realization_binary_settlement"
    assert exit_details["exit_policy_trigger_reason_codes"] == [
        "target_realization_switch_buy_exit_old"
    ]
    assert exit_details["exit_policy_family_codes"] == ["target_realization_exit"]
    transition = exit_details["sell_old_buy_new_transition"]
    assert transition["exit_leg"] == {
        "outcome": "30\u00b0C",
        "side": "YES",
        "execution_action": "SELL",
        "position_effect": "close_old_bucket_side",
    }
    assert transition["new_entry_leg"]["outcome"] == "31\u00b0C"
    assert transition["new_entry_leg"]["side"] == "YES"
    assert transition["new_entry_leg"]["execution_action"] == "BUY"
    assert transition["old_bucket_reverse_entry_leg"]["outcome"] == "30\u00b0C"
    assert transition["old_bucket_reverse_entry_leg"]["side"] == "NO"
    assert transition["old_bucket_reverse_entry_leg"]["execution_action"] == "BUY"
    job = conn.execute("SELECT * FROM auto_trade_jobs").fetchone()
    payload = json.loads(job["payload_json"])
    assert payload["include_event_keys"] == [{"slug": slug, "event_date": event_date}]
    assert payload["entry_expected_targets"][0]["outcome"] == "31\u00b0C"
    assert payload["entry_expected_targets"][0]["posterior_override"] is True
    assert payload["entry_expected_targets"][0]["hard_source_job_enqueued_at"]
    assert payload["entry_expected_targets"][0]["hard_source_ssot_to_job_enqueue_ms"] is not None
    assert payload["hard_source_direct"]["decision_semantics"] == "ssot_posterior_constraint"
    assert payload["hard_source_direct"]["ssot_written_at"] == entry_details["hard_source_ssot_written_at"]
    assert payload["hard_source_direct"]["job_enqueued_at"]
    assert "target_realization_order_intent" in payload["entry_expected_targets"][0]["entry_reasons"]
    event_payloads = payload.get("event_payloads") or []
    assert len(event_payloads) == 1
    snapshot_rows = event_payloads[0]["payload"]["rows"]
    by_outcome = {row["outcome"]: row for row in snapshot_rows}
    assert by_outcome["30\u00b0C"]["q_posterior_observed_constraints_hard_zero"] is True
    assert by_outcome["30\u00b0C"]["q_bayes_posterior"] == 0.0
    assert by_outcome["30\u00b0C"]["hard_source_direct"] is True
    assert by_outcome["31\u00b0C"]["q_bayes_posterior"] > 0.99


def test_phase0_lineage_forward_schema_missing_does_not_block_trade_path(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        decision_cols = {row["name"] for row in conn.execute("PRAGMA table_info(auto_trade_decisions)")}
        job_cols = {row["name"] for row in conn.execute("PRAGMA table_info(auto_trade_jobs)")}
        assert "signal_id" not in decision_cols
        assert "fact_snapshot_id" not in decision_cols
        assert "order_intent_id" not in job_cols
        for planned_table in (
            "source_observations",
            "awc_hard_source_signals",
            "signal_to_order_timing",
        ):
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (planned_table,),
            ).fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 1
        job_payload = json.loads(
            conn.execute("SELECT payload_json FROM auto_trade_jobs ORDER BY id DESC LIMIT 1").fetchone()[0]
        )
        assert job_payload["hard_source_direct"]["decision_semantics"] == "ssot_posterior_constraint"
        assert job_payload["entry_expected_targets"][0]["hard_source_direct"] is True
    finally:
        conn.close()


def test_hard_source_fact_snapshot_writer_plan_has_lineage_and_repoint_policy(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            """
            SELECT h.*
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()
        payload = json.loads(latest["fact_json"])
        payload["hard_source_fact_write_reason"] = "unit_test_hard_source_writer_plan"
        next_fact_as_of = _utc_fixture_iso(2)

        plan = poller.build_hard_source_fact_snapshot_writer_apply_plan(
            conn,
            latest_row=latest,
            item=payload,
            fact_as_of=next_fact_as_of,
        )

        actions = {row["action"]: row for row in plan["plan_actions"]}
        assert plan["caller"]["lane"] == "hard_source_realtime"
        assert plan["caller"]["kill_switch_env"] == "PM_HARD_SOURCE_FACT_WRITER_APPLY_ENABLED=0"
        assert plan["apply_supported"] is True
        assert actions["append_event_fact_history"]["would_write"] is True
        assert actions["repoint_event_fact_latest"]["would_write"] is True
        assert actions["upsert_event_fact_summary_cache"]["would_write"] is True
        assert plan["source_event_ids"]
        assert plan["source_observation_ids"]
        assert plan["effective_payload"]["source_observation_ids"] == plan["source_observation_ids"]
        assert plan["effective_payload"]["source_observations"][0]["source_observation_id"] == plan["source_observation_ids"][0]
        assert plan["effective_payload"]["source_latency_trace"][0]["source_observation_id"] == plan["source_observation_ids"][0]
        assert plan["source_acquisition_trace_summary"]["hard_observation_candidate_count"] >= 1
        assert plan["effective_payload"]["fact_layer_state"]["write_owner"] == "weather_fact_snapshot_writer"
        assert plan["effective_payload"]["phase0_fact_json_contract"]["historical_backfill"] is False
    finally:
        conn.close()


def test_hard_source_fact_snapshot_writer_plan_fallback_trace_keeps_source_observation_id(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            """
            SELECT h.*
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()
        payload = json.loads(latest["fact_json"])
        monkeypatch.setattr(
            poller,
            "_source_payloads_from_fact_payload",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unit trace failure")),
        )

        plan = poller.build_hard_source_fact_snapshot_writer_apply_plan(
            conn,
            latest_row=latest,
            item=payload,
            fact_as_of=_utc_fixture_iso(2),
        )

        assert plan["source_observation_ids"]
        trace = plan["effective_payload"]["source_acquisition_trace"]
        assert trace["trace_error"] == "RuntimeError: unit trace failure"
        assert trace["source_observations"][0]["source_observation_id"] == plan["source_observation_ids"][0]
        assert trace["source_latency_trace"][0]["source_observation_id"] == plan["source_observation_ids"][0]
    finally:
        conn.close()


def test_insert_fact_snapshot_defaults_to_weather_fact_writer(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_HARD_SOURCE_FACT_WRITER_APPLY_ENABLED", raising=False)
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            """
            SELECT h.*
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()
        payload = json.loads(latest["fact_json"])
        payload["awc_hard_observation_fast_trade_write"] = True
        payload["hard_source_fact_write_reason"] = "unit_test_default_writer_apply"
        next_fact_as_of = _utc_fixture_iso(2)

        fact_id = poller._insert_fact_snapshot(
            conn,
            latest_row=latest,
            item=payload,
            fact_as_of=next_fact_as_of,
        )

        assert fact_id
        latest_fact_id = conn.execute(
            "SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?",
            (slug, event_date),
        ).fetchone()["fact_id"]
        assert latest_fact_id == fact_id
        stored = json.loads(
            conn.execute("SELECT fact_json FROM event_fact_history WHERE id=?", (fact_id,)).fetchone()[
                "fact_json"
            ]
        )
        assert stored["fact_layer_state"]["write_owner"] == "weather_fact_snapshot_writer"
        assert stored["db_write_guard"]["historical_backfill"] is False
        assert stored["source_acquisition_trace"]["hard_observation_candidate_ids"]
        assert stored["source_observation_ids"]
        assert stored["source_acquisition_trace"]["source_observation_ids"] == stored["source_observation_ids"]
        assert stored["phase0_fact_json_contract"]["write_owner"] == "weather_fact_snapshot_writer"
        summary = json.loads(
            conn.execute(
                "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id=?",
                (fact_id,),
            ).fetchone()["summary_json"]
        )
        assert summary["summary_cache_source"] == "weather_fact_snapshot_writer"
        assert summary["fast_trade_summary_cache"] is True
    finally:
        conn.close()


def test_insert_fact_snapshot_kill_switch_fails_closed_without_ui_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_HARD_SOURCE_FACT_WRITER_APPLY_ENABLED", "0")
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            """
            SELECT h.* FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()
        assert poller._insert_fact_snapshot(
            conn, latest_row=latest, item=json.loads(latest["fact_json"]),
            fact_as_of="2026-06-19T09:05:42Z",
        ) is None
    finally:
        conn.close()

def test_posterior_target_direct_records_probability_change_same_bucket(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")

    def rewrite_top_probability(prob: float) -> None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        latest = conn.execute(
            """
            SELECT h.id, h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()
        payload = json.loads(latest["fact_json"])
        for row in payload["distribution"]:
            if row["outcome"] == "31\u00b0C":
                row["q_temperature_bucket_posterior"] = prob
                row["q_bayes_posterior"] = prob
                row["best_bid"] = 0.21
                row["best_ask"] = 0.24
            elif row["outcome"] == "30\u00b0C":
                row["q_temperature_bucket_posterior"] = 1.0 - prob
                row["q_bayes_posterior"] = 1.0 - prob
        payload["posterior_target_bucket_selection"] = {
            "target_bucket": "31\u00b0C",
            "target_probability": prob,
            "target_probability_pct": prob * 100.0,
        }
        payload["posterior_temperature_distribution"] = {
            "debug_blob": "x" * 250_000,
            "bucket_probabilities": {"31\u00b0C": prob},
        }
        payload["distribution"][0]["debug_blob"] = "y" * 100_000
        payload["target_realization_capital_plan"] = {
            "ok": True,
            "version": "target_realization_capital_plan_v2",
            "target_bucket": "31\u00b0C",
            "target_posterior": prob,
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "temperature_path_trade_follow_allowed": True,
            "temperature_path_trade_follow_reason": "posterior_path_reliability_and_freshness_pass",
            "buy_new_leg": {
                "outcome": "31\u00b0C",
                "side": "YES",
                "target_realization_execution_action": "BUY",
                "leg_enabled": True,
            },
        }
        _attach_unit_posterior_ssot_fact(payload, target_bucket="31\u00b0C", target_probability=prob)
        conn.execute(
            "UPDATE event_fact_history SET fact_json = ? WHERE id = ?",
            (json.dumps(payload), latest["id"]),
        )
        conn.commit()
        conn.close()

    rewrite_top_probability(0.62)
    first = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=3600,
        enqueue_job=True,
    )
    assert first["ok"] is True
    assert first["status"] == "recorded"
    assert first["target_bucket"] == "31\u00b0C"
    assert first["target_posterior"] == 0.62
    assert first["job_id"]
    # The posterior-target recorder speaks the three-leg plan's own vocabulary:
    # the positions it will exit are its sell targets, and the entries it will
    # place are its buy targets.  "old_exit_count" and
    # "entry_order_update_triggered" were the pre-v3 names and are not on this
    # result at all.  record_hard_source_direct_decisions, a different entry
    # point, still returns the older pair.
    assert first["sell_target_count"] == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    decisions = conn.execute(
        "SELECT action, outcome, side, reason_codes_json, details_json FROM auto_trade_decisions ORDER BY id"
    ).fetchall()
    assert len(decisions) == 1
    entry = decisions[0]
    assert entry["action"] == "entry"
    assert entry["outcome"] == "31\u00b0C"
    assert "posterior_target_direct" in json.loads(entry["reason_codes_json"])
    entry_details = json.loads(entry["details_json"])
    assert entry_details["posterior_target_direct"] is True
    assert entry_details.get("hard_source_direct") is not True
    assert entry_details["target_posterior"] == 0.62
    assert entry_details["q_temperature_bucket_posterior"] == 0.62
    assert entry_details["posterior_target_direct_primary_only_precheck"] is True
    assert entry_details["posterior_target_direct_precheck_suppressed_old_exit_count"] == 1
    assert entry_details["old_exit_decision_rows_suppressed"] is True
    job_payload_raw = conn.execute(
        "SELECT payload_json FROM auto_trade_jobs ORDER BY id DESC LIMIT 1"
    ).fetchone()["payload_json"]
    assert len(job_payload_raw) < 50_000
    job_payload = json.loads(job_payload_raw)
    assert job_payload["posterior_target_direct"]["decision_semantics"] == (
        "ssot_posterior_target_probability_change"
    )
    assert job_payload["entry_expected_targets"][0]["posterior_target_direct"] is True
    assert job_payload["entry_expected_targets"][0].get("hard_source_direct") is not True
    event_payload = job_payload["event_payloads"][0]["payload"]
    assert event_payload["job_payload_compact"] is True
    assert "posterior_temperature_distribution" not in event_payload
    assert "debug_blob" not in json.dumps(event_payload)
    conn.close()

    rewrite_top_probability(0.64)
    second = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=3600,
        enqueue_job=False,
    )
    assert second["status"] == "recorded"
    assert second["target_bucket"] == "31\u00b0C"
    assert second["probability_changed"] is True
    assert round(second["probability_delta"], 2) == 0.02

    third = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=3600,
        enqueue_job=False,
    )
    assert third["status"] == "recorded"
    assert third["buy_target_count"] >= 1


def test_posterior_target_direct_buys_new_target_when_path_follow_is_diagnostic(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_PRECHECK", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_JOB_PAYLOAD", "0")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_probability=0.62,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT h.id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    payload = json.loads(latest["fact_json"])
    payload["target_realization_capital_plan"]["temperature_path_trade_follow_allowed"] = False
    payload["target_realization_capital_plan"]["temperature_path_trade_follow_block_yes"] = True
    payload["target_realization_capital_plan"]["temperature_path_trade_follow_reason"] = (
        "blocked_target_path_lcb_low"
    )
    conn.execute(
        "UPDATE event_fact_history SET fact_json = ? WHERE id = ?",
        (json.dumps(payload), latest["id"]),
    )
    conn.commit()
    conn.close()

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=3600,
        enqueue_job=True,
    )

    assert result["status"] == "recorded"
    assert result["job_enqueued"] is True
    assert result["job_skipped_reason"] == ""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 1
        job_payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM auto_trade_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()["payload_json"]
        )
        rows = conn.execute(
            """
            SELECT action, will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()
    assert job_payload.get("exit_only") is not True
    assert job_payload.get("old_exit_cleanup") is not True
    assert job_payload["entry_expected_targets"][0]["outcome"] == "31\u00b0C"
    assert job_payload["entry_expected_targets"][0]["side"] == "YES"
    assert job_payload["entry_expected_targets"][0]["posterior_settlement_entry_allowed"] is True
    assert (
        job_payload["entry_expected_targets"][0][
            "posterior_target_direct_path_follow_diagnostic_only"
        ]
        is True
    )
    assert job_payload["entry_expected_targets"][0]["strategy_lane"] == "yes_realized_target"
    assert (
        job_payload["entry_expected_targets"][0][
            "temperature_path_trade_follow_reason_before_posterior_target_direct"
        ]
        == "blocked_target_path_lcb_low"
    )
    assert [row["action"] for row in rows] == ["entry", "exit"]
    assert [row["will_order"] for row in rows] == [1, 1]
    entry_codes = json.loads(rows[0]["reason_codes_json"])
    exit_codes = json.loads(rows[1]["reason_codes_json"])
    assert "blocked_target_path_lcb_low" not in entry_codes
    assert exit_codes == ["target_realization_switch_buy_exit_old"]
    entry_details = json.loads(rows[0]["details_json"])
    exit_details = json.loads(rows[1]["details_json"])
    assert entry_details["job_enqueue_eligible"] is True
    assert entry_details["job_enqueue_attempted"] is True
    assert entry_details["job_enqueued"] is True
    assert entry_details["job_id"] == result["job_id"]
    assert entry_details["job_skipped_reason"] == ""
    assert entry_details["target_execution_blocked"] is False
    assert entry_details["target_execution_block_reason"] == ""
    assert entry_details["posterior_target_direct_path_follow_diagnostic_only"] is True
    assert entry_details["strategy_lane"] == "yes_realized_target"
    assert (
        entry_details["temperature_path_trade_follow_reason_before_posterior_target_direct"]
        == "blocked_target_path_lcb_low"
    )
    assert entry_details["temperature_path_trade_follow_allowed"] is True
    assert entry_details["temperature_path_trade_follow_reason"] == (
        "posterior_target_direct_target_snapshot"
    )
    assert "entry_execution_blocked" not in exit_details
    assert "entry_execution_block_reason" not in exit_details
    assert "exit_decoupled_from_entry_execution_block" not in exit_details
    assert "exit_suppressed_by_target_execution_block" not in exit_details


def test_posterior_target_direct_records_decision_from_posterior_ssot_fact(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_PRECHECK", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_JOB_PAYLOAD", "0")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_outcome="31\u00b0C",
        target_probability=0.62,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT h.id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    payload = json.loads(latest["fact_json"])
    payload["posterior_ssot_fact"] = {
        "schema": "bayes_posterior_ssot_fact_v1",
        "state": "available",
        "fact_snapshot_id": "fact_unit",
        "posterior_run_id": "post_unit",
        "target_bucket": "31\u00b0C",
        "target_probability": 0.66,
        "target_probability_pct": 66.0,
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "phase": "D0_breakout",
        "beta_weak": 0.4,
        "beta_strong": 0.65,
        "hard_mask_reason": "none",
        "ranked_bucket_probabilities": [
            {"bucket": "31\u00b0C", "probability": 0.66},
            {"bucket": "30\u00b0C", "probability": 0.34},
        ],
        "bucket_probabilities": [
            {"bucket": "31\u00b0C", "probability": 0.66},
            {"bucket": "30\u00b0C", "probability": 0.34},
        ],
        "layer_chain": {
            "formula": "continuous_forecast_error_prior_then_sequential_state_zero_inflated_remaining_warming_mixture_then_bucket_integral"
        },
        "boundaries": {
            "live_primary_composer": "sequential_remaining_warming_bayes_runtime_v31",
            "posterior_activation_mode": "live_no_shadow",
            "legacy_bucket_product_posterior_used": False,
            "shadow_posterior_emitted": False,
        },
        # The composer's physical verdict.  The three-leg plan refuses to
        # build without it -- it will not reverse-engineer weather from raw
        # METAR, so a missing verdict keeps both legs HOLD and the poller
        # returns target_realization_v3_plan_blocked before recording
        # anything.  Production mints this on every SSOT block.
        "strong_physical_evidence": {
            "joint_feature_model_applied": True,
            "top_bucket": "31\u00b0C",
            "top_bucket_joint_strong_support": True,
            "bucket_exclusions": [],
        },
    }
    payload["fact_snapshot_id"] = "fact_unit"
    payload["posterior_run_id"] = "post_unit"
    ssot_rows = payload["posterior_ssot_fact"]["ranked_bucket_probabilities"]
    running_cdf = 0.0
    continuous_grid = []
    for index, row in enumerate(ssot_rows):
        mass = float(row["probability"])
        running_cdf += mass
        continuous_grid.append(
            {
                "temp_c": round(20.0 + index * 0.1, 1),
                "mass": mass,
                "cdf": running_cdf,
            }
        )
    payload["posterior_continuous_distribution"] = {
        "schema": "sequential_remaining_warming_continuous_distribution_v31",
        "source": "sequential_remaining_warming_bayes_runtime_v31",
        "target_variable": "settlement_daily_max_c",
        "grid_step_c": 0.1,
        "grid": continuous_grid,
        "bucket_probabilities": ssot_rows,
    }
    payload["posterior_target_bucket_selection"]["target_probability"] = 0.62
    payload["posterior_target_bucket_selection"]["ssot_source"] = "legacy_selection_fixture"
    conn.execute(
        "UPDATE event_fact_history SET fact_json = ? WHERE id = ?",
        (json.dumps(payload), latest["id"]),
    )
    conn.commit()
    conn.close()

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=3600,
        enqueue_job=True,
    )

    assert result["status"] == "recorded"
    assert result["target_bucket"] == "31\u00b0C"
    assert result["target_posterior"] == pytest.approx(0.66)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        decision = conn.execute(
            """
            SELECT details_json
            FROM auto_trade_decisions
            WHERE action = 'entry'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        details = json.loads(decision["details_json"])
        job_payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM auto_trade_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()["payload_json"]
        )
    finally:
        conn.close()

    assert details["decision_ssot"] == "posterior_ssot_fact"
    assert details["posterior_ssot_fact_used"] is True
    assert details["target_posterior"] == pytest.approx(0.66)
    assert details["fact_snapshot_id"] == "fact_unit"
    assert details["posterior_run_id"] == "post_unit"
    assert details["posterior_target_fact_snapshot_id"] == "fact_unit"
    assert details["posterior_target_posterior_run_id"] == "post_unit"
    assert details["posterior_ssot_fact"]["target_probability"] == 0.66
    assert details["posterior_ssot_phase"] == "D0_breakout"
    assert job_payload["fact_snapshot_id"] == "fact_unit"
    assert job_payload["posterior_run_id"] == "post_unit"
    assert job_payload["posterior_target_direct"]["decision_ssot"] == "posterior_ssot_fact"
    assert job_payload["posterior_target_direct"]["posterior_ssot_fact_used"] is True
    assert job_payload["posterior_target_direct"]["fact_snapshot_id"] == "fact_unit"
    assert job_payload["posterior_target_direct"]["posterior_run_id"] == "post_unit"
    assert job_payload["entry_expected_targets"][0]["decision_ssot"] == "posterior_ssot_fact"
    assert job_payload["entry_expected_targets"][0]["fact_snapshot_id"] == "fact_unit"
    assert job_payload["entry_expected_targets"][0]["posterior_run_id"] == "post_unit"
    event_payload = job_payload["event_payloads"][0]["payload"]
    assert event_payload["fact_snapshot_id"] == "fact_unit"
    assert event_payload["posterior_run_id"] == "post_unit"
    assert event_payload["posterior_ssot_fact"]["target_probability"] == 0.66
    assert event_payload["posterior_ssot_fact"]["ranked_bucket_probabilities"][0]["bucket"] == "31\u00b0C"
    assert "posterior_temperature_distribution" not in event_payload


def test_posterior_target_direct_dedupes_same_fact_job(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_OLD_EXIT_CLEANUP_ON_UNCHANGED", "0")

    first = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert first["status"] == "recorded"
    assert first["job_id"]
    assert first["job_enqueued"] is True

    second = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert second["status"] == "unchanged"
    assert second["job_skipped_reason"] == "posterior_target_unchanged_no_job"

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 1
    finally:
        conn.close()


def test_posterior_target_direct_records_enqueue_error_details(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_probability=0.62,
    )

    def raise_enqueue_error(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(poller, "_enqueue_posterior_target_job", raise_enqueue_error)

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["status"] == "recorded"
    assert result["job_enqueued"] is False
    assert result["job_skipped_reason"] == "posterior_target_enqueue_error"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 0
        decision = conn.execute(
            """
            SELECT decided_state, will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            WHERE action = 'entry'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert decision["decided_state"] == "blocked"
    assert decision["will_order"] == 0
    reason_codes = json.loads(decision["reason_codes_json"])
    assert "posterior_target_enqueue_error" in reason_codes
    details = json.loads(decision["details_json"])
    assert details["job_enqueue_eligible"] is True
    assert details["job_enqueue_attempted"] is True
    assert details["job_enqueued"] is False
    assert details["job_id"] is None
    assert details["job_skipped_reason"] == "posterior_target_enqueue_error"
    assert "OperationalError: database is locked" in details["job_enqueue_error"]
    assert details["posterior_target_enqueue_error"] == details["job_enqueue_error"]


def test_posterior_target_direct_skips_job_after_previous_place_order_failure(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    now = _utc_fixture_iso()
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_RETRY_MAX", "3")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_RETRY_COOLDOWN_SEC", "120")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_outcome="31\u00b0C",
        target_probability=0.62,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO edge_trades(
                slug, event_date, outcome, side, state, last_action, last_action_at,
                last_status, last_error, entry_retry_count, updated_at
            )
            VALUES(?, ?, '31\u00b0C', 'YES', 'entry_failed', 'entry', ?,
                   'exec_failed', 'exec_failed: place-order timed out after 58.75 seconds',
                   3, ?)
            """,
            (slug, event_date, now, now),
        )
        conn.commit()

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["status"] == "recorded"
    assert result["job_enqueued"] is False
    assert result["job_skipped_reason"] == (
        "posterior_target_previous_place_order_failure_retry_exhausted"
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 0
        decision = conn.execute(
            """
            SELECT decided_state, will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert decision["decided_state"] == "blocked"
    assert decision["will_order"] == 0
    reason_codes = json.loads(decision["reason_codes_json"])
    assert "posterior_target_previous_place_order_failure_retry_exhausted" in reason_codes
    details = json.loads(decision["details_json"])
    assert details["target_execution_blocked"] is True
    assert details["previous_place_order_retry_block"]["blocked"] is True


def test_posterior_target_direct_retries_after_local_place_order_failure(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    now = _utc_fixture_iso(-300)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_RETRY_MAX", "3")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_RETRY_COOLDOWN_SEC", "120")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_outcome="31\u00b0C",
        target_probability=0.62,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO edge_trades(
                slug, event_date, outcome, side, state, last_action, last_action_at,
                last_status, last_error, entry_retry_count, updated_at
            )
            VALUES(?, ?, '31\u00b0C', 'YES', 'entry_failed', 'entry', ?,
                   'clob_order_service_unavailable',
                   'HTTPConnectionPool(host=''127.0.0.1'', port=8765): Max retries exceeded with url: /place-order (Caused by NewConnectionError(\"HTTPConnection(host=''127.0.0.1'', port=8765): Failed to establish a new connection: [Errno 61] Connection refused\"))',
                   275, ?)
            """,
            (slug, event_date, now, now),
        )
        conn.commit()

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["status"] == "recorded"
    assert result["job_enqueued"] is True
    assert result["job_skipped_reason"] == ""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 1
        decision = conn.execute(
            """
            SELECT decided_state, will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert decision["decided_state"] == "entry"
    assert decision["will_order"] == 1
    reason_codes = json.loads(decision["reason_codes_json"])
    assert "posterior_target_previous_place_order_failure_retry_exhausted" not in reason_codes
    details = json.loads(decision["details_json"])
    retry_block = details["previous_place_order_retry_block"]
    assert retry_block["blocked"] is False
    assert retry_block["retry_allowed_after_local_failure"] is True
    assert retry_block["retryable_local_failure_reason"] == (
        "local_clob_order_service_connection_failure"
    )


def test_posterior_target_direct_blocks_after_remote_place_order_502_failure(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    now = _utc_fixture_iso(-300)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_RETRY_MAX", "3")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_RETRY_COOLDOWN_SEC", "120")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_outcome="31\u00b0C",
        target_probability=0.62,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO edge_trades(
                slug, event_date, outcome, side, state, last_action, last_action_at,
                last_status, last_error, entry_retry_count, updated_at
            )
            VALUES(?, ?, '31\u00b0C', 'YES', 'entry_failed', 'entry', ?,
                   'Request failed with status code 502',
                   'Request failed with status code 502',
                   3, ?)
            """,
            (slug, event_date, now, now),
        )
        conn.commit()

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["status"] == "recorded"
    assert result["job_enqueued"] is False
    assert result["job_skipped_reason"] == (
        "posterior_target_previous_place_order_failure_retry_exhausted"
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 0
        decision = conn.execute(
            """
            SELECT decided_state, will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert decision["decided_state"] == "blocked"
    assert decision["will_order"] == 0
    reason_codes = json.loads(decision["reason_codes_json"])
    assert "posterior_target_previous_place_order_failure_retry_exhausted" in reason_codes
    details = json.loads(decision["details_json"])
    assert details["previous_place_order_retry_block"]["blocked"] is True
    assert details["previous_place_order_retry_block"]["last_status"] == (
        "Request failed with status code 502"
    )


def test_posterior_target_direct_blocks_immediately_after_geoblocked_failure(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    now = _utc_fixture_iso(-300)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_RETRY_MAX", "5")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_RETRY_COOLDOWN_SEC", "120")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_outcome="31\u00b0C",
        target_probability=0.62,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO edge_trades(
                slug, event_date, outcome, side, state, last_action, last_action_at,
                last_status, last_error, entry_retry_count, updated_at
            )
            VALUES(?, ?, '31\u00b0C', 'YES', 'entry_failed', 'entry', ?,
                   'clob_order_geoblocked',
                   'Trading restricted in your region, see geoblock docs',
                   1, ?)
            """,
            (slug, event_date, now, now),
        )
        conn.commit()

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["status"] == "recorded"
    assert result["job_enqueued"] is False
    assert result["job_skipped_reason"] == (
        "posterior_target_previous_place_order_failure_geoblocked"
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 0
        decision = conn.execute(
            """
            SELECT decided_state, will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert decision["decided_state"] == "blocked"
    assert decision["will_order"] == 0
    reason_codes = json.loads(decision["reason_codes_json"])
    assert "posterior_target_previous_place_order_failure_geoblocked" in reason_codes
    details = json.loads(decision["details_json"])
    retry_block = details["previous_place_order_retry_block"]
    assert retry_block["blocked"] is True
    assert retry_block["blocked_by_geoblock"] is True
    assert retry_block["blocked_by_max_retry"] is False


def test_posterior_target_local_place_order_retryable_reason_matches_sdk_build_error():
    assert poller._posterior_target_local_place_order_retryable_reason(
        "entry_failed exec_failed Cannot read properties of undefined (reading 'toString')"
    ) == "local_sdk_order_build_metadata_error"


def test_posterior_target_direct_existing_position_does_not_block_additional_buy_when_unchanged(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_OLD_EXIT_CLEANUP_COOLDOWN_SEC", "300")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_probability=0.62,
    )

    first = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert first["status"] == "recorded"
    assert first["job_enqueued"] is True
    assert first["sell_target_count"] == 1

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE auto_trade_jobs
        SET state = 'success', updated_at = ?, ended_at = ?
        WHERE id = ?
        """,
        (_utc_fixture_iso(), _utc_fixture_iso(), first["job_id"]),
    )
    conn.execute(
        """
        INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size, last_order_id, updated_at)
        VALUES(?, ?, ?, 'YES', 'entry', 5.0, 'target-order-31', ?)
        """,
        (slug, event_date, "31\u00b0C", _utc_fixture_iso()),
    )
    conn.commit()
    conn.close()

    second = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert second["status"] == "recorded"
    assert second["buy_target_count"] >= 1
    assert second["old_exit_cleanup_triggered"] is False
    assert second["old_exit_cleanup_deferred_for_primary_entry"] is True
    assert second["job_enqueued"] is True
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 2
        job_payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM auto_trade_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()["payload_json"]
        )
        assert job_payload.get("exit_only") is not True
        assert job_payload.get("old_exit_cleanup") is not True
        assert job_payload["posterior_target_direct"].get("old_exit_cleanup_triggered") is not True
        assert job_payload["posterior_target_direct"]["old_exit_cleanup_deferred_for_primary_entry"] is True
        assert job_payload["posterior_target_direct"]["decision_semantics"] == (
            "ssot_posterior_target_probability_change"
        )
        assert job_payload["entry_expected_targets"][0]["outcome"] == "31\u00b0C"
        assert job_payload["entry_expected_targets"][0]["side"] == "YES"
        rows = conn.execute(
            """
            SELECT action, outcome, side, will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            ORDER BY id DESC
            LIMIT 4
            """
        ).fetchall()
    finally:
        conn.close()
    latest_by_action = {}
    for row in rows:
        latest_by_action.setdefault(row["action"], row)
    entry_codes = json.loads(latest_by_action["entry"]["reason_codes_json"])
    entry_details = json.loads(latest_by_action["entry"]["details_json"])
    assert latest_by_action["entry"]["will_order"] == 1
    assert "posterior_target_active_entry_missing" in entry_codes
    assert entry_details["primary_target_entry_needed"] is True
    assert entry_details["target_entry_already_active"] is True
    assert entry_details["target_entry_pending_order_active"] is False
    assert entry_details["target_entry_active_position_does_not_block_additional_buy"] is True
    assert entry_details["old_exit_cleanup_triggered"] is False
    assert entry_details["old_exit_cleanup_deferred_for_primary_entry"] is True
    if "exit" in latest_by_action:
        assert latest_by_action["exit"]["will_order"] == 0

    third = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert third["status"] == "recorded"
    assert third["buy_target_count"] >= 1
    assert third["job_enqueued"] is False
    assert third["job_skipped_reason"] == "posterior_target_job_already_enqueued_for_fact"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 2
    finally:
        conn.close()


def test_posterior_target_direct_old_exit_cleanup_buys_missing_primary_target(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_OLD_EXIT_CLEANUP_COOLDOWN_SEC", "300")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_probability=0.62,
    )

    first = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert first["status"] == "recorded"
    assert first["job_enqueued"] is True
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE auto_trade_jobs
        SET state = 'superseded', updated_at = ?, ended_at = ?, last_error = 'test_superseded'
        WHERE id = ?
        """,
        (_utc_fixture_iso(), _utc_fixture_iso(), first["job_id"]),
    )
    conn.commit()
    conn.close()

    second = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert second["status"] == "recorded"
    assert second["buy_target_count"] >= 1
    assert second["old_exit_cleanup_triggered"] is False
    assert second["old_exit_cleanup_deferred_for_primary_entry"] is True
    assert second["job_enqueued"] is True
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 2
        job_payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM auto_trade_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()["payload_json"]
        )
        rows = conn.execute(
            """
            SELECT action, outcome, side, will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            ORDER BY id DESC
            LIMIT 2
            """
        ).fetchall()
    finally:
        conn.close()

    assert job_payload.get("exit_only") is not True
    assert job_payload.get("old_exit_cleanup") is not True
    assert job_payload["posterior_target_direct"].get("old_exit_cleanup_triggered") is not True
    assert job_payload["entry_expected_targets"][0]["outcome"] == "31\u00b0C"
    assert job_payload["entry_expected_targets"][0]["side"] == "YES"
    latest_by_action = {}
    for row in rows:
        latest_by_action.setdefault(row["action"], row)
    entry_codes = json.loads(latest_by_action["entry"]["reason_codes_json"])
    entry_details = json.loads(latest_by_action["entry"]["details_json"])
    assert latest_by_action["entry"]["will_order"] == 1
    assert "target_switch_non_target_entry_suppressed" not in entry_codes
    assert "posterior_target_active_entry_missing" in entry_codes
    assert entry_details["primary_target_entry_needed"] is True
    assert entry_details["target_entry_already_active"] is False
    assert entry_details["old_exit_cleanup_triggered"] is False
    assert entry_details["old_exit_cleanup_deferred_for_primary_entry"] is True
    if "exit" in latest_by_action:
        assert latest_by_action["exit"]["will_order"] == 0


def test_posterior_target_direct_duplicate_fact_suppresses_repeat_order_intent(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_OLD_EXIT_CLEANUP_COOLDOWN_SEC", "300")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_probability=0.62,
    )

    first = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert first["status"] == "recorded"
    assert first["job_enqueued"] is True

    second = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert second["status"] == "recorded"
    assert second["buy_target_count"] >= 1
    assert second["job_enqueued"] is False
    assert second["job_skipped_reason"] == "posterior_target_job_already_enqueued_for_fact"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 1
        rows = conn.execute(
            """
            SELECT action, outcome, side, will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            ORDER BY id DESC
            LIMIT 2
            """
        ).fetchall()
    finally:
        conn.close()

    latest_by_action = {}
    for row in rows:
        latest_by_action.setdefault(row["action"], row)
    entry_codes = json.loads(latest_by_action["entry"]["reason_codes_json"])
    entry_details = json.loads(latest_by_action["entry"]["details_json"])
    assert latest_by_action["entry"]["will_order"] == 0
    assert "posterior_target_job_already_enqueued_for_fact" in entry_codes
    assert entry_details["primary_target_entry_needed"] is True
    assert entry_details["duplicate_job_for_fact"] is True
    assert entry_details["job_skipped_reason"] == (
        "posterior_target_job_already_enqueued_for_fact"
    )
    if "exit" in latest_by_action:
        exit_details = json.loads(latest_by_action["exit"]["details_json"])
        assert latest_by_action["exit"]["will_order"] == 0
        assert exit_details["duplicate_job_for_fact"] is True


def test_posterior_target_direct_success_job_does_not_suppress_missing_primary_retry(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_OLD_EXIT_CLEANUP_COOLDOWN_SEC", "300")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_probability=0.62,
    )

    first = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert first["status"] == "recorded"
    assert first["job_enqueued"] is True

    now_iso = _utc_fixture_iso()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE auto_trade_jobs
        SET state = 'success', updated_at = ?, ended_at = ?
        WHERE id = ?
        """,
        (now_iso, now_iso, first["job_id"]),
    )
    conn.commit()
    conn.close()

    second = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert second["status"] == "recorded"
    assert second["buy_target_count"] >= 1
    assert second["job_enqueued"] is True
    assert second["job_skipped_reason"] == ""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 2
        rows = conn.execute(
            """
            SELECT action, will_order, reason_codes_json, details_json
            FROM auto_trade_decisions
            ORDER BY id DESC
            LIMIT 2
            """
        ).fetchall()
    finally:
        conn.close()

    latest_by_action = {row["action"]: row for row in rows}
    entry_codes = json.loads(latest_by_action["entry"]["reason_codes_json"])
    entry_details = json.loads(latest_by_action["entry"]["details_json"])
    assert latest_by_action["entry"]["will_order"] == 1
    assert "posterior_target_job_already_enqueued_for_fact" not in entry_codes
    assert entry_details["primary_target_entry_needed"] is True
    assert entry_details["duplicate_job_for_fact"] is False


def test_posterior_target_direct_expired_running_job_does_not_suppress_retry(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_OLD_EXIT_CLEANUP_COOLDOWN_SEC", "300")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_probability=0.62,
    )

    first = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert first["status"] == "recorded"
    assert first["job_enqueued"] is True

    stale_iso = "2000-01-01T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE auto_trade_jobs
        SET state = 'running',
            lease_until = ?,
            started_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (stale_iso, stale_iso, stale_iso, first["job_id"]),
    )
    conn.commit()
    conn.close()

    second = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert second["status"] == "recorded"
    assert second["buy_target_count"] >= 1
    assert second["job_enqueued"] is True
    assert second["job_skipped_reason"] == ""
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 2
    finally:
        conn.close()


def test_claimable_direct_target_job_includes_expired_running_job(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")

    first = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert first["job_enqueued"] is True

    stale_iso = "2000-01-01T00:00:00+00:00"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE auto_trade_jobs
        SET state = 'running',
            lease_until = ?,
            started_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (stale_iso, stale_iso, stale_iso, first["job_id"]),
    )
    conn.commit()
    conn.close()

    claimable = poller._claimable_direct_target_job_id(
        db_path,
        slug=slug,
        event_date=event_date,
    )

    assert claimable == first["job_id"]


def test_posterior_target_direct_does_not_enqueue_new_fact_when_probability_unchanged(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_OLD_EXIT_CLEANUP_ON_UNCHANGED", "0")

    first = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert first["status"] == "recorded"
    assert first["job_enqueued"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    first_decision_count = conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0]
    latest = conn.execute(
        """
        SELECT h.run_date, h.slug, h.event_date, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    next_as_of = _utc_fixture_iso(120)
    cur = conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            latest["run_date"],
            latest["slug"],
            latest["event_date"],
            next_as_of,
            latest["fact_json"],
            next_as_of,
        ),
    )
    next_fact_id = int(cur.lastrowid or 0)
    conn.execute(
        "UPDATE event_fact_latest SET fact_id = ?, updated_at = ? WHERE slug = ? AND event_date = ?",
        (next_fact_id, next_as_of, slug, event_date),
    )
    conn.commit()
    conn.close()

    second = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert second["status"] == "unchanged"
    assert second["fact_id"] == next_fact_id
    assert second["job_skipped_reason"] == "posterior_target_unchanged_no_job"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == first_decision_count
    finally:
        conn.close()


@pytest.mark.parametrize(
    (
        "old_outcome",
        "old_side",
        "old_token",
        "old_order_id",
        "old_sell_order_id",
        "old_sell_price",
    ),
    [
        ("30\u00b0C", "YES", "yes30-token", "old-order-30", "posterior-sell-30", 0.39),
        ("31\u00b0C", "NO", "no31-token", "old-order-31-no", "posterior-sell-no31", 0.57),
    ],
)
def test_posterior_target_direct_worker_executes_new_entry_and_old_exit_fake_live(
    tmp_path,
    monkeypatch,
    old_outcome,
    old_side,
    old_token,
    old_order_id,
    old_sell_order_id,
    old_sell_price,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    if old_side == "NO":
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        latest = conn.execute(
            """
            SELECT h.id, h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()
        payload = json.loads(latest["fact_json"])
        for row in payload["distribution"]:
            if row["outcome"] == old_outcome:
                row["best_bid_no"] = old_sell_price
                row["best_ask_no"] = 0.60
            elif row["outcome"] == "31\u00b0C":
                row["best_bid_no"] = 0.001
                row["best_ask_no"] = 0.003
        conn.execute(
            "UPDATE event_fact_history SET fact_json = ? WHERE id = ?",
            (json.dumps(payload), latest["id"]),
        )
        conn.execute(
            """
            UPDATE edge_trades
            SET outcome = ?, side = 'NO', last_order_id = ?, avg_price = 0.40
            WHERE slug = ? AND event_date = ? AND outcome = ? AND side = 'YES'
            """,
            (old_outcome, old_order_id, slug, event_date, "30\u00b0C"),
        )
        conn.execute(
            """
            INSERT INTO event_market_ticks(run_date, event_date, slug, outcome, side,
                                           best_bid, best_ask, mid, spread,
                                           bid_depth_topN, ask_depth_topN, source, created_at)
            VALUES(?, ?, ?, ?, 'NO', ?, 0.60, ?, ?, 100, 120, 'clob_rest_refresh', ?)
            """,
            (
                    "2026-06-18",
                    event_date,
                    slug,
                    old_outcome,
                    old_sell_price,
                    (old_sell_price + 0.60) / 2.0,
                    0.60 - old_sell_price,
                    _utc_fixture_iso(2),
            ),
        )
        conn.commit()
        conn.close()
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_outcome="31\u00b0C",
        target_probability=1.0,
    )
    _set_target_market_price(
        db_path,
        slug=slug,
        event_date=event_date,
        outcome="31\u00b0C",
        best_bid=0.997,
        best_ask=0.999,
        local_peak_hour=16.0,
    )
    monkeypatch.setenv("PM_AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "0")
    monkeypatch.setenv("PM_TARGET_REALIZATION_DIRECT_PRIMARY_ONLY_FAST_PATH_REQUIRE_ALLOWED_FLAG", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_BLOCK_POST_PEAK_NEAR_CERTAIN_ENTRY", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_POST_PEAK_NEAR_CERTAIN_PRICE_MIN", "0.99")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_TIMEOUT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_BACKOFF_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_FACTS_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DB_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_POSITIONS_SYNC", "0")
    monkeypatch.setenv("PM_POSITIONS_ASSUME_ZERO", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_ENTRY_MIN_AGE_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_RULES_LIVE_HYDRATE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_MARKET_EXIT_PROBE_REFRESH_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_NOT_FOUND_RECHECK_DELAY_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_NOT_FOUND_REFRESH_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_MIN_EXIT_SIZE", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_DUST_MIN", "0.01")
    monkeypatch.setenv("PM_MIN_ORDER_SIZE", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_ALLOW_SMALL", "1")
    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(swm, "POSITIONS_SYNC_ENABLED", False)
    monkeypatch.setattr(swm, "POSITIONS_ASSUME_ZERO", False)

    placed = []
    balance_calls = []

    def fake_run_node_order(cmd, token_id="", side="", *, price=None, size=None, order_type=None, amount=None, order_context=None):
        placed.append(
            {
                "cmd": cmd,
                "token_id": token_id,
                "side": side,
                "price": price,
                "size": size,
                "order_type": order_type,
                "amount": amount,
            }
        )
        if cmd == "cancel-orders":
            return True, {"status": "cancelled", "count": 1}
        if token_id == "yes31-token" and side == "BUY":
            return True, {"orderID": "posterior-buy-31", "status": "submitted"}
        if token_id == old_token and side == "SELL":
            return True, {"orderID": old_sell_order_id, "status": "submitted"}
        return True, {"orderID": f"posterior-{side.lower()}-{token_id}", "status": "submitted"}

    def fake_run_node_get_order(order_id):
        submitted_buy = next(
            (
                call for call in placed
                if call["cmd"] == "place-order"
                and call["side"] == "BUY"
                and call["token_id"] == "yes31-token"
            ),
            {},
        )
        orders = {
            old_order_id: {"status": "confirmed", "price": "0.40", "side": "BUY"},
            "posterior-buy-31": {
                "status": "confirmed",
                "size_matched": str(submitted_buy.get("size") or "43.47826087"),
                "price": str(submitted_buy.get("price") or "0.23"),
                "side": "BUY",
            },
            old_sell_order_id: {
                "status": "confirmed",
                "size_matched": "8.0",
                "price": str(old_sell_price),
                "side": "SELL",
            },
        }
        order = orders.get(order_id)
        if order is None:
            return True, {"order": {"status": "confirmed", "price": "0.50", "side": "BUY"}}
        return True, {"order": order}

    def fake_run_node_balance(token_id="", refresh=True, asset_type="CONDITIONAL"):
        balance_calls.append(
            {"token_id": token_id, "refresh": refresh, "asset_type": asset_type}
        )
        if str(asset_type or "").upper() == "COLLATERAL":
            return True, {
                "balance": "100000000",
                "allowance": "100000000",
                "balance_allowance": "100000000",
            }
        return False, {"error": "conditional_balance_timeout"}

    monkeypatch.setattr(swm, "_run_node_order", fake_run_node_order)
    monkeypatch.setattr(swm, "_run_node_get_order", fake_run_node_get_order)
    monkeypatch.setattr(swm, "_run_node_balance", fake_run_node_balance)

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert result["ok"] is True
    assert result["job_id"]
    assert result["sell_target_count"] == 1

    worker = swm.run_auto_trade_job_worker_once()

    assert worker["ok"] is True
    assert worker["picked"] is True
    assert worker["result"]["processed"] == 1

    buy_new_calls = [
        call for call in placed
        if call["cmd"] == "place-order"
        and call["side"] == "BUY"
        and call["token_id"] == "yes31-token"
    ]
    sell_old_calls = [
        call for call in placed
        if call["cmd"] == "place-order"
        and call["side"] == "SELL"
        and call["token_id"] == old_token
    ]
    assert buy_new_calls
    assert buy_new_calls[0]["price"] is not None
    assert buy_new_calls[0]["price"] >= 0.99
    assert sell_old_calls == [
        {
            "cmd": "place-order",
            "token_id": old_token,
            "side": "SELL",
            "price": old_sell_price,
            "size": 8.0,
            "order_type": "FAK",
            "amount": None,
        }
    ]
    conditional_balance_calls = [
        call for call in balance_calls
        if str(call["asset_type"] or "").upper() != "COLLATERAL"
    ]
    assert balance_calls == []
    assert conditional_balance_calls == []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    decisions = conn.execute(
        "SELECT * FROM auto_trade_decisions ORDER BY id"
    ).fetchall()
    entry = [
        row for row in decisions
        if row["action"] == "entry"
        and row["outcome"] == "31\u00b0C"
        and row["side"] == "YES"
        and "order_submitted" in json.loads(row["reason_codes_json"] or "[]")
    ]
    assert len(entry) == 1
    entry_details = json.loads(entry[0]["details_json"] or "{}")
    assert entry_details["posterior_target_direct"] is True
    assert entry_details["target_bucket"] == "31\u00b0C"
    assert entry_details["order_id"] == "posterior-buy-31"
    assert entry_details["posterior_target_worker_started_at"]
    assert entry_details["posterior_target_job_enqueue_to_order_ms"] is not None
    assert entry_details["posterior_target_job_enqueue_to_order_ms"] <= 3000
    assert entry_details["posterior_target_worker_to_order_ms"] <= 3000
    assert entry_details["posterior_target_fact_to_order_ms"] <= 3000

    exit_rows = [
        row for row in decisions
        if row["action"] == "exit"
        and row["outcome"] == old_outcome
        and row["side"] == old_side
        and "exit_marketable_immediate_confirmed" in json.loads(row["reason_codes_json"] or "[]")
    ]
    assert len(exit_rows) == 1
    exit_details = json.loads(exit_rows[0]["details_json"] or "{}")
    assert exit_details["posterior_target_direct"] is True
    assert exit_details["order_id"] == old_sell_order_id
    assert exit_details["exit_balance_guard_bypassed"] is True
    assert (
        exit_details["exit_balance_guard_bypass_reason"]
        == "target_realization_switch_buy_exit_old"
    )
    assert exit_details["posterior_target_worker_started_at"]
    assert exit_details["posterior_target_job_enqueue_to_order_ms"] is not None
    assert exit_details["posterior_target_job_enqueue_to_order_ms"] <= 3000
    assert exit_details["posterior_target_worker_to_order_ms"] <= 3000
    assert exit_details["posterior_target_fact_to_order_ms"] <= 3000
    if old_side == "YES":
        acceptance = hard_source_audit.audit(
            db_path,
            mode="posterior_target",
            slug=slug,
            event_date=event_date,
            max_job_enqueue_to_worker_ms=3000,
            max_job_enqueue_to_order_ms=3000,
            max_fact_to_order_ms=3000,
        )
        assert acceptance["ok"] is True, acceptance
        assert acceptance["checks"]["target_entry_order_decision_present"] is True
        assert acceptance["checks"]["old_bucket_exit_order_decision_present"] is True
        assert acceptance["checks"]["job_enqueue_to_order_within_sla"] is True
        assert acceptance["checks"]["fact_to_order_within_sla"] is True


def test_posterior_target_direct_exit_submit_failure_records_exit_skip_audit(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_outcome="31\u00b0C",
        target_probability=1.0,
    )
    _set_target_market_price(
        db_path,
        slug=slug,
        event_date=event_date,
        outcome="31\u00b0C",
        best_bid=0.997,
        best_ask=0.999,
        local_peak_hour=16.0,
    )
    monkeypatch.setenv("PM_AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "0")
    monkeypatch.setenv("PM_TARGET_REALIZATION_DIRECT_PRIMARY_ONLY_FAST_PATH_REQUIRE_ALLOWED_FLAG", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_BLOCK_POST_PEAK_NEAR_CERTAIN_ENTRY", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_POST_PEAK_NEAR_CERTAIN_PRICE_MIN", "0.99")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_TIMEOUT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_BACKOFF_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_FACTS_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DB_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_POSITIONS_SYNC", "0")
    monkeypatch.setenv("PM_POSITIONS_ASSUME_ZERO", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_ENTRY_MIN_AGE_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_RULES_LIVE_HYDRATE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_MARKET_EXIT_PROBE_REFRESH_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_NOT_FOUND_RECHECK_DELAY_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_NOT_FOUND_REFRESH_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_MIN_EXIT_SIZE", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_DUST_MIN", "0.01")
    monkeypatch.setenv("PM_MIN_ORDER_SIZE", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_ALLOW_SMALL", "1")
    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(swm, "POSITIONS_SYNC_ENABLED", False)
    monkeypatch.setattr(swm, "POSITIONS_ASSUME_ZERO", False)

    def fake_run_node_order(cmd, token_id="", side="", *, price=None, size=None, order_type=None, amount=None, order_context=None):
        if cmd == "cancel-orders":
            return True, {"status": "cancelled", "count": 0}
        if token_id == "yes31-token" and side == "BUY":
            return True, {"orderID": "posterior-buy-31", "status": "submitted"}
        if token_id == "yes30-token" and side == "SELL":
            return False, {"error": "exec_failed", "stderr": "timed out after 15.0 seconds"}
        return True, {"orderID": f"posterior-{side.lower()}-{token_id}", "status": "submitted"}

    def fake_run_node_get_order(order_id):
        if order_id == "old-order-30":
            return True, {"order": {"status": "confirmed", "price": "0.40", "side": "BUY"}}
        if order_id == "posterior-buy-31":
            return True, {
                "order": {
                    "status": "confirmed",
                    "size_matched": "10",
                    "price": "0.99",
                    "side": "BUY",
                }
            }
        return False, {"error": "not_found"}

    def fake_run_node_balance(token_id="", refresh=True, asset_type="CONDITIONAL"):
        if str(asset_type or "").upper() == "COLLATERAL":
            return True, {
                "balance": "100000000",
                "allowance": "100000000",
                "balance_allowance": "100000000",
            }
        return False, {"error": "conditional_balance_timeout"}

    monkeypatch.setattr(swm, "_run_node_order", fake_run_node_order)
    monkeypatch.setattr(swm, "_run_node_get_order", fake_run_node_get_order)
    monkeypatch.setattr(swm, "_run_node_balance", fake_run_node_balance)

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert result["ok"] is True
    assert result["job_id"]

    worker = swm.run_auto_trade_job_worker_once()

    assert worker["ok"] is True
    assert worker["picked"] is True
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        old_trade = conn.execute(
            """
            SELECT state, last_action, last_status, last_error, position_size,
                   exit_skip_reason, exit_skip_detail, exit_skip_at, exit_reason_codes
            FROM edge_trades
            WHERE slug = ? AND event_date = ? AND outcome = ? AND side = 'YES'
            """,
            (slug, event_date, "30\u00b0C"),
        ).fetchone()
        exit_decision = conn.execute(
            """
            SELECT reason_codes_json, details_json
            FROM auto_trade_decisions
            WHERE action = 'exit' AND outcome = ? AND side = 'YES'
            ORDER BY id DESC
            LIMIT 1
            """,
            ("30\u00b0C",),
        ).fetchone()
    finally:
        conn.close()
    assert old_trade["state"] == "entry"
    assert old_trade["last_action"] == "exit"
    assert old_trade["position_size"] == 8.0
    assert old_trade["exit_skip_reason"] == "exit_submit_failed"
    assert "exec_failed" in old_trade["exit_skip_detail"]
    assert old_trade["exit_skip_at"]
    assert "target_realization_switch_buy_exit_old" in old_trade["exit_reason_codes"]
    assert "place_order_failed" in json.loads(exit_decision["reason_codes_json"] or "[]")
    exit_details = json.loads(exit_decision["details_json"] or "{}")
    assert exit_details["exit_skip_reason"] == "exit_submit_failed"
    assert "exec_failed" in exit_details["exit_skip_detail"]


def test_posterior_target_direct_defers_old_exit_when_primary_entry_submit_fails(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_outcome="31\u00b0C",
        target_probability=1.0,
    )
    _set_target_market_price(
        db_path,
        slug=slug,
        event_date=event_date,
        outcome="31\u00b0C",
        best_bid=0.997,
        best_ask=0.999,
        local_peak_hour=16.0,
    )
    monkeypatch.setenv("PM_AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "0")
    monkeypatch.setenv("PM_TARGET_REALIZATION_DIRECT_PRIMARY_ONLY_FAST_PATH_REQUIRE_ALLOWED_FLAG", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_BLOCK_POST_PEAK_NEAR_CERTAIN_ENTRY", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_POST_PEAK_NEAR_CERTAIN_PRICE_MIN", "0.99")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", "0.001")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_TIMEOUT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_BACKOFF_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_FACTS_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DB_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_POSITIONS_SYNC", "0")
    monkeypatch.setenv("PM_POSITIONS_ASSUME_ZERO", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_ENTRY_MIN_AGE_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_RULES_LIVE_HYDRATE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_MARKET_EXIT_PROBE_REFRESH_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_NOT_FOUND_RECHECK_DELAY_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_NOT_FOUND_REFRESH_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_MIN_EXIT_SIZE", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_DUST_MIN", "0.01")
    monkeypatch.setenv("PM_MIN_ORDER_SIZE", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_ALLOW_SMALL", "1")
    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(swm, "POSITIONS_SYNC_ENABLED", False)
    monkeypatch.setattr(swm, "POSITIONS_ASSUME_ZERO", False)

    placed = []

    def fake_run_node_order(cmd, token_id="", side="", *, price=None, size=None, order_type=None, amount=None, order_context=None):
        placed.append(
            {
                "cmd": cmd,
                "token_id": token_id,
                "side": side,
                "price": price,
                "size": size,
                "order_type": order_type,
                "amount": amount,
            }
        )
        if cmd == "cancel-orders":
            return True, {"status": "cancelled", "count": 0}
        if token_id == "yes31-token" and side == "BUY":
            return False, {"error": "exec_failed", "stderr": "timed out after 60.0 seconds"}
        if token_id == "yes30-token" and side == "SELL":
            return True, {"orderID": "unexpected-old-sell", "status": "submitted"}
        return True, {"orderID": f"posterior-{side.lower()}-{token_id}", "status": "submitted"}

    def fake_run_node_get_order(order_id):
        if order_id == "old-order-30":
            return True, {"order": {"status": "confirmed", "price": "0.40", "side": "BUY"}}
        return False, {"error": "not_found"}

    def fake_run_node_balance(token_id="", refresh=True, asset_type="CONDITIONAL"):
        if str(asset_type or "").upper() == "COLLATERAL":
            return True, {
                "balance": "100000000",
                "allowance": "100000000",
                "balance_allowance": "100000000",
            }
        return False, {"error": "conditional_balance_timeout"}

    monkeypatch.setattr(swm, "_run_node_order", fake_run_node_order)
    monkeypatch.setattr(swm, "_run_node_get_order", fake_run_node_get_order)
    monkeypatch.setattr(swm, "_run_node_balance", fake_run_node_balance)

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert result["ok"] is True
    assert result["job_id"]

    worker = swm.run_auto_trade_job_worker_once()

    assert worker["ok"] is True
    assert worker["picked"] is True
    assert [
        call for call in placed
        if call["cmd"] == "place-order"
        and call["side"] == "SELL"
        and call["token_id"] == "yes30-token"
    ] == []
    assert [
        call for call in placed
        if call["cmd"] == "place-order"
        and call["side"] == "BUY"
        and call["token_id"] == "yes31-token"
    ]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        old_trade = conn.execute(
            """
            SELECT state, last_action, last_status, position_size,
                   exit_skip_reason, exit_skip_detail
            FROM edge_trades
            WHERE slug = ? AND event_date = ? AND outcome = ? AND side = 'YES'
            """,
            (slug, event_date, "30\u00b0C"),
        ).fetchone()
        exit_decision = conn.execute(
            """
            SELECT reason_codes_json, details_json
            FROM auto_trade_decisions
            WHERE action = 'exit' AND outcome = ? AND side = 'YES'
            ORDER BY id DESC
            LIMIT 1
            """,
            ("30\u00b0C",),
        ).fetchone()
    finally:
        conn.close()
    assert old_trade["state"] == "entry"
    assert old_trade["last_action"] == "exit_skip"
    assert old_trade["last_status"] == "target_switch_entry_priority"
    assert old_trade["position_size"] == 8.0
    assert old_trade["exit_skip_reason"] == "target_switch_entry_priority"
    assert "entry_job_deferred" in old_trade["exit_skip_detail"]
    assert "target_realization_exit_deferred_entry_priority" in json.loads(
        exit_decision["reason_codes_json"] or "[]"
    )
    exit_details = json.loads(exit_decision["details_json"] or "{}")
    assert exit_details["target_switch_entry_priority"] is True
    assert exit_details["target_switch_primary_entry_keys"] == [
        {"outcome": "31\u00b0C", "side": "YES"}
    ]


def test_posterior_target_direct_defaults_to_ignore_decision_window(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.delenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", raising=False)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", event_date)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "13.0")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_probability=0.62,
    )

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=3600,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["status"] == "recorded"
    assert result["target_bucket"] == "31\u00b0C"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 1
    job_payload = json.loads(conn.execute("SELECT payload_json FROM auto_trade_jobs").fetchone()[0])
    assert job_payload["posterior_target_direct"]["decision_semantics"] == (
        "ssot_posterior_target_probability_change"
    )
    conn.close()


def test_posterior_target_direct_can_still_enforce_decision_window(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", event_date)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "13.0")
    _rewrite_posterior_target_probability(
        db_path,
        slug=slug,
        event_date=event_date,
        target_probability=0.62,
    )

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=3600,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["status"] == "outside_decision_window"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 0
    conn.close()


@pytest.mark.skip(reason="retired: non-AMSC sources must never rewrite posterior SSOT")
def test_inline_ctp_recompute_repoints_latest_fact(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_RECOMPUTE_CTP_INLINE", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_RECOMPUTE_FAST_TRADE_WRITE", "1")
    heavy_marker = "HEAVY_INLINE_CTP_FAST_TRADE_MARKER"
    heavy_blob = {"marker": heavy_marker, "blob": "x" * 2000}

    def fake_materialize(payload):
        out = dict(payload)
        out["chengdu_intraday_summary"] = dict(heavy_blob)
        out["target_bucket_outlook"] = dict(heavy_blob)
        out["temperature_path_distribution"] = dict(heavy_blob)
        out["temperature_realization_reliability"] = dict(heavy_blob)
        out["target_switch_context"] = dict(heavy_blob)
        out["evidence_conclusions"] = dict(heavy_blob)
        out["decision"] = dict(heavy_blob)
        out["key_metrics"] = dict(heavy_blob)
        out["posterior_temperature_distribution"] = {
            "version": "continuous_temperature_posterior_v1_test",
            "grid": [30.0, 31.0],
            "cdf_values": [0.4, 1.0],
            "quantiles_c": {"0.5": 31.0},
            "quantile_grid": [{"q": 0.5, "value_c": 31.0}],
            "bucket_probabilities": {"30\u00b0C": 0.4, "31\u00b0C": 0.6},
        }
        for row in out["distribution"]:
            row["bayes_chain"] = dict(heavy_blob)
            row["peak_estimator"] = dict(heavy_blob)
            row["per_model_q"] = dict(heavy_blob)
            if row["outcome"] == "31\u00b0C":
                row["q_bayes_posterior"] = 0.6
                row["q_temperature_bucket_posterior"] = 0.6
            elif row["outcome"] == "30\u00b0C":
                row["q_bayes_posterior"] = 0.4
                row["q_temperature_bucket_posterior"] = 0.4
        return out, {"ok": True, "top_label": "31\u00b0C", "top_probability": 0.6}

    monkeypatch.setattr(poller, "_materialize_ctp_payload", fake_materialize)

    result = poller._recompute_ctp_latest_fact(
        db_path,
        event_date=event_date,
        slug=slug,
        reason="awc",
    )

    assert result["ok"] is True
    assert result["status"] == "recomputed"
    assert result["reason"] == "awc"
    assert result["probability_changed"] is True
    assert result["source_fact_to_ctp_recompute_ms"] is not None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT h.id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    fact_json = latest["fact_json"]
    assert heavy_marker not in fact_json
    payload = json.loads(latest["fact_json"])
    assert latest["id"] == result["fact_id"]
    assert payload["posterior_temperature_distribution"]["bucket_probabilities"]["31\u00b0C"] == 0.6
    assert payload["posterior_target_direct_inline_ctp_recompute_reason"] == "awc"
    assert payload["posterior_target_direct_source_fact_id"] == result["source_fact_id"]
    assert payload["posterior_target_direct_fast_trade_write"] is True
    assert payload["posterior_target_bucket_selection"]["target_bucket"] == "31\u00b0C"
    assert payload["posterior_target_bucket_selection"]["target_probability"] == pytest.approx(0.6)
    for key in (
        "chengdu_intraday_summary",
        "target_bucket_outlook",
        "temperature_path_distribution",
        "temperature_realization_reliability",
        "target_switch_context",
        "evidence_conclusions",
        "decision",
        "key_metrics",
    ):
        assert key not in payload
    for row in payload["distribution"]:
        assert "bayes_chain" not in row
        assert "peak_estimator" not in row
        assert "per_model_q" not in row
    cache = conn.execute(
        "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?",
        (result["fact_id"],),
    ).fetchone()
    assert cache is not None
    assert heavy_marker not in cache["summary_json"]
    summary = json.loads(cache["summary_json"])
    assert summary["fast_trade_summary_cache"] is True
    assert summary["posterior_target_bucket_selection"]["target_bucket"] == "31\u00b0C"
    assert summary["posterior_target_bucket_selection"]["target_probability"] == pytest.approx(0.6)
    conn.close()


def test_load_latest_fact_payload_prefers_summary_cache_over_full_fact_json(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    summary_payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": "2026-06-18",
        "summary_cache_source": "event_fact_summary_cache",
        "weather_likelihood_features_compact": {
            "zuuu_metar_raw_awc_today_max_c": 32.0,
            "zuuu_metar_raw_awc_latest_at": _utc_fixture_iso(),
        },
        "posterior_temperature_distribution": {
            "bucket_probabilities": [
                {"outcome": "31\u00b0C", "probability": 0.42},
                {"outcome": "32\u00b0C", "probability": 0.58},
            ]
        },
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT h.id, h.fact_as_of
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    assert latest is not None
    conn.execute(
        "UPDATE event_fact_history SET fact_json = ? WHERE id = ?",
        ("not-json", latest["id"]),
    )
    conn.execute(
        """
        INSERT INTO event_fact_summary_cache(
            fact_id, slug, event_date, run_date, fact_as_of, summary_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            latest["id"],
            slug,
            event_date,
            "2026-06-18",
            latest["fact_as_of"],
            json.dumps(summary_payload),
            _utc_fixture_iso(),
        ),
    )
    conn.commit()
    conn.close()

    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_LATEST_PAYLOAD_SUMMARY_CACHE", raising=False)

    payload = poller._load_latest_fact_payload(db_path, event_date=event_date, slug=slug)

    assert payload["summary_cache_source"] == "event_fact_summary_cache"
    assert payload["weather_likelihood_features_compact"]["zuuu_metar_raw_awc_today_max_c"] == 32.0
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_LATEST_PAYLOAD_SUMMARY_CACHE", "0")
    assert poller._load_latest_fact_payload(db_path, event_date=event_date, slug=slug) == {}


def test_posterior_target_direct_records_from_summary_cache_without_full_fact_json(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    summary_payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": "2026-06-18",
        "title": "Highest temperature in Chengdu on June 19, 2026",
        "rounding_rule": "whole_degrees",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "summary_cache_source": "event_fact_summary_cache",
        "posterior_target_bucket_selection": {
            "target_bucket": "31\u00b0C",
            "target_probability": 0.62,
            "target_probability_field": "q_temperature_bucket_posterior",
        },
        "target_realization_capital_plan": {
            "ok": True,
            "target_bucket": "31\u00b0C",
            "target_posterior": 0.62,
        },
        "distribution": [
            {
                "outcome": "30\u00b0C",
                "q_entry": 0.38,
                "q_bayes_posterior": 0.38,
                "q_temperature_bucket_posterior": 0.38,
                "best_bid": 0.39,
                "best_ask": 0.42,
            },
            {
                "outcome": "31\u00b0C",
                "q_entry": 0.62,
                "q_bayes_posterior": 0.62,
                "q_temperature_bucket_posterior": 0.62,
                "best_bid": 0.21,
                "best_ask": 0.24,
            },
        ],
    }
    _attach_unit_posterior_ssot_fact(
        summary_payload,
        target_bucket="31\u00b0C",
        target_probability=0.62,
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT h.id, h.fact_as_of
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    assert latest is not None
    conn.execute(
        "UPDATE event_fact_history SET fact_json = ? WHERE id = ?",
        ("not-json", latest["id"]),
    )
    conn.execute(
        """
        INSERT INTO event_fact_summary_cache(
            fact_id, slug, event_date, run_date, fact_as_of, summary_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            latest["id"],
            slug,
            event_date,
            "2026-06-18",
            latest["fact_as_of"],
            json.dumps(summary_payload),
            _utc_fixture_iso(),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.delenv("PM_POSTERIOR_TARGET_DIRECT_LATEST_PAYLOAD_SUMMARY_CACHE", raising=False)

    result = poller.record_posterior_target_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        enqueue_job=False,
    )

    assert result["ok"] is True
    assert result["status"] == "recorded"
    assert result["target_bucket"] == "31\u00b0C"
    assert result["latest_fact_payload_source"] == "summary_cache"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT action, outcome, details_json FROM auto_trade_decisions ORDER BY id"
    ).fetchall()
    conn.close()
    assert [(row["action"], row["outcome"]) for row in rows] == [
        ("entry", "31\u00b0C"),
        ("exit", "30\u00b0C"),
    ]
    details = json.loads(rows[0]["details_json"])
    assert details["expected_target"]["outcome"] == "31\u00b0C"
    assert details["expected_target"]["posterior_target_direct"] is True


def test_poll_once_does_not_recompute_posterior_after_reference_source_update(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    calls = []

    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_PWS_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_RUN_WORKER_ONCE", "0")
    monkeypatch.setattr(
        poller,
        "_refresh_awc_fact",
        lambda *args, **kwargs: {"ok": True, "status": "updated", "fact_id": 22},
    )

    def fake_hard_decision(*args, **kwargs):
        calls.append("hard_decision")
        return {"ok": True, "status": "hard_skipped"}

    def fake_posterior_decision(*args, **kwargs):
        calls.append("posterior_decision")
        return {"ok": True, "status": "posterior_checked"}

    monkeypatch.setattr(poller, "record_hard_source_direct_decisions", fake_hard_decision)
    monkeypatch.setattr(poller, "record_posterior_target_direct_decisions", fake_posterior_decision)

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert calls == ["hard_decision"]
    assert result["ctp_recompute"]["status"] == "retired"
    assert result["ctp_recompute"]["reason"] == "non_amsc_source_cannot_recompute_posterior"
    assert result["posterior_target_fast_precheck"]["status"] == "retired"
    assert result["posterior_target"]["reason"] == "posterior_target_direct_runtime_removed"
    assert result["posterior_target_after_awc_ctp"] == {}
    assert result["posterior_target_after_ctp"] == {}


def test_poll_once_never_runs_retired_posterior_precheck_before_awc(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    calls = []

    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_PWS_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_RUN_WORKER_ONCE", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_FAST_PRECHECK_ENABLED", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_FAST_PRECHECK_BEFORE_AWC_ENABLED", "1")

    def fake_posterior(*args, **kwargs):
        calls.append(f"posterior:{kwargs.get('emit_unchanged_sec')}")
        return {"ok": True, "status": "unchanged"}

    def fake_awc(*args, **kwargs):
        calls.append("awc")
        return {"ok": True, "status": "skipped", "reason": "same_awc_latest_at"}

    monkeypatch.setattr(poller, "record_posterior_target_direct_decisions", fake_posterior)
    monkeypatch.setattr(poller, "_refresh_awc_fact", fake_awc)
    monkeypatch.setattr(
        poller,
        "record_hard_source_direct_decisions",
        lambda *args, **kwargs: calls.append("hard") or {"ok": True, "status": "unchanged"},
    )

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert calls == ["awc", "hard"]
    assert result["posterior_target_fast_precheck"]["status"] == "retired"
    assert result["posterior_target_fast_precheck"]["reason"] == (
        "sequential_bayes_posterior_outbox_is_the_only_runtime_consumer"
    )


def test_poll_once_source_has_no_retired_posterior_target_runtime_call():
    source = Path(poller.__file__).read_text(encoding="utf-8")
    poll_once_body = source[
        source.index("def poll_once(") : source.index("def _refresh_stamp_due(")
    ]

    assert "record_posterior_target_direct_decisions(" not in poll_once_body
    assert "_claimable_direct_target_job_id(" not in poll_once_body
    assert "PM_POSTERIOR_TARGET_DIRECT_FAST_PRECHECK" not in poll_once_body
    assert "sequential_bayes_posterior_outbox_is_the_only_runtime_consumer" in poll_once_body


def test_poll_once_does_not_recompute_posterior_from_awc_or_pws(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    calls = []

    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_FAST_PRECHECK_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_PWS_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_PWS_BLOCKING", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_RUN_WORKER_ONCE", "0")

    def fake_awc(*args, **kwargs):
        calls.append("awc")
        return {"ok": True, "status": "updated", "fact_id": 22}

    def fake_metar_pws(*args, **kwargs):
        calls.append("metar_pws")
        return {"ok": True, "status": "updated", "fact_id": 23}

    def fake_posterior_decision(*args, **kwargs):
        calls.append("posterior_decision")
        return {"ok": True, "status": "posterior_checked"}

    def fake_hard_decision(*args, **kwargs):
        calls.append("hard_decision")
        return {"ok": True, "status": "hard_skipped"}

    monkeypatch.setattr(poller, "_refresh_awc_fact", fake_awc)
    monkeypatch.setattr(poller, "_refresh_metar_pws_multi_source_fact", fake_metar_pws)
    monkeypatch.setattr(poller, "record_posterior_target_direct_decisions", fake_posterior_decision)
    monkeypatch.setattr(poller, "record_hard_source_direct_decisions", fake_hard_decision)

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert calls == ["awc", "metar_pws", "hard_decision"]
    assert result["awc_ctp_recompute"]["status"] == "retired"
    assert result["ctp_recompute"]["status"] == "retired"
    assert result["posterior_target_after_awc_ctp"] == {}
    assert result["posterior_target_after_ctp"] == {}


def test_hard_source_job_worker_uses_constrained_snapshot_before_fact_lag(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_TIMEOUT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_BACKOFF_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_FACTS_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DB_LOCK_WAIT_SEC", "0")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert result["ok"] is True
    assert result["job_id"]
    assert result["ssot_fact_id"]

    captured = []

    def capture_execute(run_date, event_date_key, payload, **kwargs):
        captured.append(
            {
                "run_date": run_date,
                "event_date": event_date_key,
                "payload": payload,
                "kwargs": kwargs,
            }
        )

    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(swm, "execute_auto_trades", capture_execute)

    worker = swm.run_auto_trade_job_worker_once()

    assert worker["ok"] is True
    assert worker["picked"] is True
    assert worker["result"]["processed"] == 1
    assert len(captured) == 1
    payload = captured[0]["payload"]
    targets = payload.get("entry_expected_targets") or []
    assert [(item.get("outcome"), item.get("side")) for item in targets] == [
        ("31\u00b0C", "YES"),
        ("30\u00b0C", "NO"),
    ]
    assert "target_realization_order_intent" in (targets[0].get("entry_reasons") or [])
    assert targets[0]["hard_source_worker_started_at"]
    assert targets[0]["hard_source_job_id"] == result["job_id"]
    assert targets[0]["hard_source_job_enqueue_to_worker_ms"] is not None
    assert targets[1]["target_realization_leg_role"] == "old_bucket_reverse"
    assert targets[0]["target_realization_execution_action"] == "BUY"
    assert targets[1]["target_realization_execution_action"] == "BUY"
    assert "target_realization_switch_old_bucket_reverse" in (
        targets[1].get("entry_reasons") or []
    )
    assert targets[1]["held_target_bucket"] == "30\u00b0C"
    assert targets[1]["held_target_side"] == "YES"
    assert targets[1]["paired_exit_side"] == "YES"
    by_outcome = {row["outcome"]: row for row in payload["rows"]}
    old_row = by_outcome["30\u00b0C"]
    assert old_row["q_posterior_observed_constraints_hard_zero"] is True
    assert old_row["q_bayes_posterior"] == 0.0
    assert swm._should_defer_target_switch_exit_for_entry_priority(
        exit_only=False,
        target_switch_expected_entry_keys={("31\u00b0C", "YES")},
        outcome_label="30\u00b0C",
        side_label="YES",
        row_ref=old_row,
        exit_codes=["target_realization_switch_buy_exit_old"],
    ) is False
    assert old_row["target_switch_entry_priority_override"] == "hard_zero_old_bucket_exit"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    job = conn.execute("SELECT state, run_id FROM auto_trade_jobs WHERE id = ?", (result["job_id"],)).fetchone()
    assert job["state"] == "success"
    assert job["run_id"]


def test_market_prefetch_once_uses_short_budget_and_supplied_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    observed = {}
    monkeypatch.setattr(
        poller,
        "_background_lock_path",
        lambda name, event_date: lock_dir / f"{name}.{event_date}.pid",
    )

    def fake_refresh(slug, event_date, args, event_detail=None, *, run_deadline_monotonic=None):
        observed["slug"] = slug
        observed["event_date"] = event_date
        observed["args"] = args
        observed["db_path"] = swm.DB_PATH
        observed["data_dir"] = swm.DATA_DIR
        observed["workers_env"] = __import__("os").environ.get("PM_SCAN_MARKET_REFRESH_QUOTE_PREFETCH_WORKERS")
        observed["cache_only_env"] = __import__("os").environ.get("PM_MARKET_REFRESH_CACHE_ONLY")
        return True, None, 3, [{"outcome": "31°C"}], "2026-06-19T08:00:00+00:00"

    monkeypatch.setattr(swm, "refresh_market_ticks_for_event", fake_refresh)

    result = poller._market_prefetch_once_with_lock(
        db_path,
        event_date="2026-06-19",
        slug="highest-temperature-in-chengdu-on-june-19-2026",
    )

    assert result["ok"] is True
    assert result["status"] == "market_prefetch_refreshed"
    assert result["count"] == 3
    assert observed["slug"] == "highest-temperature-in-chengdu-on-june-19-2026"
    assert observed["event_date"] == "2026-06-19"
    assert observed["db_path"] == db_path
    assert observed["data_dir"] == tmp_path
    assert observed["args"].market_refresh_only is True
    assert observed["args"].market_refresh_max_outcomes == 6
    assert observed["args"].market_refresh_time_budget_sec == 1.5
    assert observed["args"].market_refresh_clob_timeout_sec == 0.75
    assert observed["args"].market_refresh_clob_retries == 1
    assert observed["args"].depth_levels == 5
    assert observed["args"].tz == "America/New_York"
    assert observed["workers_env"] == "4"
    assert observed["cache_only_env"] == "1"


def test_poll_once_starts_market_prefetch_inside_wu_peak_window(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-19-2026"
    payload = {
        "slug": slug,
        "event_date": "2026-06-19",
        "event_tz": "Asia/Shanghai",
        "weather_likelihood_features": {
            "wu_forecast_peak_at": "2026-06-19T17:00:00+08:00",
        },
    }
    started = []
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", "2026-06-19")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "16.25")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setattr(poller, "_refresh_awc_fact", lambda *args, **kwargs: {"ok": True, "status": "skipped_test"})
    monkeypatch.setattr(poller, "_load_latest_fact_payload", lambda *args, **kwargs: payload)
    monkeypatch.setattr(
        poller,
        "_refresh_stamp_due",
        lambda name, event_date, *, interval_sec: name == "market_prefetch",
    )
    monkeypatch.setattr(
        poller,
        "_spawn_market_prefetch",
        lambda db, event_date, slug="": started.append((db, event_date, slug))
        or {"ok": True, "status": "market_prefetch_background_started", "pid": 1234},
    )
    record_kwargs = {}

    def fake_record_hard_source_direct_decisions(*args, **kwargs):
        record_kwargs.update(kwargs)
        return {"ok": False, "status": "hard_observation_missing"}

    monkeypatch.setattr(
        poller,
        "record_hard_source_direct_decisions",
        fake_record_hard_source_direct_decisions,
    )

    result = poller.poll_once(db_path, event_date="2026-06-19")

    assert started == [(db_path, "2026-06-19", slug)]
    assert result["market_prefetch"]["status"] == "market_prefetch_background_started"
    assert result["pre_signal_preparation"]["pre_signal_prepare_state"] == "preparation_started_unconfirmed"
    assert record_kwargs["preparation_timing"]["pre_signal_prepare_state"] == "preparation_started_unconfirmed"
    assert result["decision"]["status"] == "hard_observation_missing"


def test_poll_once_can_run_market_prefetch_inline_inside_wu_peak_window(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-19-2026"
    payload = {
        "slug": slug,
        "event_date": "2026-06-19",
        "event_tz": "Asia/Shanghai",
        "weather_likelihood_features": {
            "wu_forecast_peak_at": "2026-06-19T17:00:00+08:00",
        },
    }
    called = []
    sequence = []
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", "2026-06-19")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "16.25")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_INLINE", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_PREPARE_OPEN_ORDERS_FETCH", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_PREPARE_POSITIONS_FETCH", "0")
    monkeypatch.setattr(
        poller.swm,
        "_AUTO_TRADE_OPEN_ORDERS_CACHE",
        {"ts": 0.0, "by_token": {}, "last_source": "unit_test_empty"},
        raising=False,
    )
    monkeypatch.setattr(
        poller.swm,
        "POSITIONS_CACHE",
        {"ts": 0.0, "data": {}, "error": "", "last_source": "unit_test_empty"},
        raising=False,
    )
    monkeypatch.setattr(poller, "_refresh_awc_fact", lambda *args, **kwargs: {"ok": True, "status": "skipped_test"})
    monkeypatch.setattr(poller, "_load_latest_fact_payload", lambda *args, **kwargs: payload)
    monkeypatch.setattr(
        poller,
        "_refresh_stamp_due",
        lambda name, event_date, *, interval_sec: name == "market_prefetch",
    )
    monkeypatch.setattr(
        poller,
        "_market_prefetch_once_with_lock",
        lambda db, event_date, slug="": sequence.append("prefetch")
        or called.append((db, event_date, slug))
        or {"ok": True, "status": "market_prefetch_refreshed", "count": 3},
    )
    record_kwargs = {}

    def fake_record_hard_source_direct_decisions(*args, **kwargs):
        sequence.append("decision")
        record_kwargs.update(kwargs)
        return {"ok": False, "status": "hard_observation_missing"}

    monkeypatch.setattr(
        poller,
        "record_hard_source_direct_decisions",
        fake_record_hard_source_direct_decisions,
    )

    result = poller.poll_once(db_path, event_date="2026-06-19")

    assert sequence == ["prefetch", "decision"]
    assert called == [(db_path, "2026-06-19", slug)]
    assert result["market_prefetch"]["status"] == "market_prefetch_refreshed"
    assert result["market_prefetch"]["count"] == 3
    assert result["pre_signal_preparation"]["pre_signal_prepare_state"] == "preparation_partial"
    assert result["pre_signal_preparation"]["pre_signal_prepared_at"]
    assert result["pre_signal_preparation"]["pre_signal_prepare_inline"] is True
    assert "market_mapping" in result["pre_signal_preparation"]["pre_signal_prepare_missing_components"]
    assert "orderbook" in result["pre_signal_preparation"]["pre_signal_prepare_missing_components"]
    assert "open_orders" in result["pre_signal_preparation"]["pre_signal_prepare_missing_components"]
    assert "positions" in result["pre_signal_preparation"]["pre_signal_prepare_missing_components"]
    assert result["pre_signal_preparation"]["pre_signal_prepare_components"]["market_prefetch"]["ok"] is True
    assert record_kwargs["preparation_timing"]["pre_signal_prepare_state"] == "preparation_partial"
    assert record_kwargs["preparation_timing"]["pre_signal_prepared_at"]


def test_poll_once_does_not_market_prefetch_when_decision_window_disabled(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-19-2026"
    payload = {
        "slug": slug,
        "event_date": "2026-06-19",
        "event_tz": "Asia/Shanghai",
        "weather_likelihood_features": {
            "wu_forecast_peak_at": "2026-06-19T17:00:00+08:00",
        },
    }
    started = []
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setattr(poller, "_refresh_awc_fact", lambda *args, **kwargs: {"ok": True, "status": "skipped_test"})
    monkeypatch.setattr(poller, "_load_latest_fact_payload", lambda *args, **kwargs: payload)
    monkeypatch.setattr(
        poller,
        "_refresh_stamp_due",
        lambda name, event_date, *, interval_sec: True,
    )
    monkeypatch.setattr(
        poller,
        "_spawn_market_prefetch",
        lambda *args, **kwargs: started.append((args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        poller,
        "record_hard_source_direct_decisions",
        lambda *args, **kwargs: {"ok": False, "status": "hard_observation_missing"},
    )

    result = poller.poll_once(db_path, event_date="2026-06-19", slug=slug)

    assert started == []
    assert result["market_prefetch"] == {}
    assert result["decision"]["status"] == "hard_observation_missing"


def test_metar_pws_due_sources_use_configured_peak_intervals(monkeypatch):
    for name in (
        "PM_HARD_SOURCE_METAR_IEM_POLL_SEC",
        "PM_HARD_SOURCE_METAR_AVWX_POLL_SEC",
        "PM_HARD_SOURCE_METAR_CHECKWX_POLL_SEC",
        "PM_HARD_SOURCE_TWC_PWS_RAPID_POLL_SEC",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PM_METAR_CROSS_CHECK_IEM_ENABLED", "1")
    monkeypatch.setenv("PM_METAR_CROSS_CHECK_AVWX_ENABLED", "1")
    monkeypatch.setenv("PM_METAR_CROSS_CHECK_CHECKWX_ENABLED", "1")
    monkeypatch.setenv("PM_TWC_PWS_RAPID_HISTORY_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_IEM_PEAK_POLL_SEC", "11")
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_AVWX_PEAK_POLL_SEC", "12")
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_CHECKWX_PEAK_POLL_SEC", "13")
    monkeypatch.setenv("PM_HARD_SOURCE_TWC_PWS_RAPID_PEAK_POLL_SEC", "14")
    captured = []

    def fake_due(name, event_date, *, interval_sec):
        captured.append((name, event_date, interval_sec))
        return True

    monkeypatch.setattr(poller, "_refresh_stamp_due", fake_due)

    due, statuses = poller._metar_pws_due_sources("2026-06-19", window_allowed=True)

    assert [item["key"] for item in due] == ["iem", "avwx", "checkwx", "twc_pws_rapid"]
    assert captured == [
        ("metar_pws_metar_iem_noaaport", "2026-06-19", 11),
        ("metar_pws_metar_avwx", "2026-06-19", 12),
        ("metar_pws_metar_checkwx", "2026-06-19", 13),
        ("metar_pws_twc_pws_rapid", "2026-06-19", 14),
    ]
    assert {item["key"]: item["interval_sec"] for item in statuses} == {
        "iem": 11,
        "avwx": 12,
        "checkwx": 13,
        "twc_pws_rapid": 14,
    }


def test_awc_release_watch_interval_only_during_acquire_window(monkeypatch):
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_RELEASE_WATCH_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_RELEASE_WATCH_POLL_SEC", "0.7")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_ACQUIRE_WINDOW_START_MIN", "5")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_ACQUIRE_WINDOW_DURATION_SEC", "120")

    assert poller._awc_release_watch_interval(
        datetime(2026, 6, 30, 4, 3, 30, tzinfo=timezone.utc)
    ) is None
    assert poller._awc_release_watch_target_at(
        datetime(2026, 6, 30, 4, 3, 30, tzinfo=timezone.utc)
    ) is None
    assert poller._awc_release_watch_interval(
        datetime(2026, 6, 30, 4, 5, 15, tzinfo=timezone.utc)
    ) == pytest.approx(0.7)
    assert poller._awc_release_watch_interval(
        datetime(2026, 6, 30, 4, 6, 59, tzinfo=timezone.utc)
    ) == pytest.approx(0.7)
    assert poller._awc_release_watch_target_at(
        datetime(2026, 6, 30, 4, 5, 15, tzinfo=timezone.utc)
    ) == datetime(2026, 6, 30, 4, 0, tzinfo=timezone.utc)
    assert poller._awc_release_watch_interval(
        datetime(2026, 6, 30, 4, 7, 0, tzinfo=timezone.utc)
    ) is None


def test_awc_event_end_final_watch_context_requires_unlocked_event_end(monkeypatch):
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_FINAL_WATCH_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_FINAL_WATCH_POLL_SEC", "0.6")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_FINAL_WATCH_DURATION_SEC", "900")
    payload = {
        "event_end_utc": "2026-07-05T12:00:00Z",
        "weather_likelihood_features": {
            "zuuu_metar_raw_awc_today_max_c": 37.0,
            "zuuu_metar_raw_awc_final_daily_max": False,
        },
    }

    before = poller._awc_event_end_final_watch_context(
        payload,
        now_utc=datetime(2026, 7, 5, 11, 59, tzinfo=timezone.utc),
    )
    active = poller._awc_event_end_final_watch_context(
        payload,
        now_utc=datetime(2026, 7, 5, 12, 5, tzinfo=timezone.utc),
    )
    locked = poller._awc_event_end_final_watch_context(
        {
            **payload,
            "weather_likelihood_features": {
                **payload["weather_likelihood_features"],
                "hard_source_final_daily_max": True,
            },
        },
        now_utc=datetime(2026, 7, 5, 12, 5, tzinfo=timezone.utc),
    )

    assert before["active"] is False
    assert before["reason"] == "before_awc_final_watch_window"
    assert active["active"] is True
    assert active["reason"] == "awc_final_daily_max_event_end_watch"
    assert active["target_at"] == datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    assert active["poll_sec"] == pytest.approx(0.6)
    assert locked == {"active": False, "reason": "final_daily_max_already_locked"}


def test_awc_release_result_reached_target_rejects_probabilistic_post_peak_state_before_event_end():
    target_at = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)

    assert poller._awc_release_result_reached_target(
        {
            "awc_latest_at": "2026-07-05T10:00:00+00:00",
            "awc_final_daily_max": True,
            "awc_final_daily_max_state": "awc_latest_after_today_max_and_cooling_post_peak",
        },
        target_at,
    ) is False


def test_near_peak_interval_uses_awc_event_end_final_watch(monkeypatch, tmp_path):
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_FINAL_WATCH_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_FINAL_WATCH_POLL_SEC", "0.7")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_POLL_SEC", "5")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_NEAR_PEAK_POLL_SEC", "3")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_MAX_POLL_SEC", "5")
    monkeypatch.setattr(poller, "_awc_release_watch_interval", lambda: None)
    monkeypatch.setattr(
        poller,
        "_load_latest_fact_payload",
        lambda *args, **kwargs: {"event_end_utc": "2026-07-05T12:00:00Z"},
    )
    monkeypatch.setattr(
        poller,
        "_awc_event_end_final_watch_context",
        lambda *args, **kwargs: {"active": True, "poll_sec": 0.7},
    )
    monkeypatch.setattr(
        poller,
        "_decision_window_state",
        lambda *args, **kwargs: {"allowed": False},
    )

    assert poller._near_peak_interval(db_path, event_date="2026-07-05") == pytest.approx(0.7)


def test_near_peak_interval_uses_awc_release_watch_when_outside_peak(monkeypatch, tmp_path):
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_POLL_SEC", "5")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_NEAR_PEAK_POLL_SEC", "1")
    monkeypatch.setattr(poller, "_awc_release_watch_interval", lambda: 0.8)
    monkeypatch.setattr(poller, "_load_latest_fact_payload", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        poller,
        "_decision_window_state",
        lambda *args, **kwargs: {"allowed": False},
    )

    assert poller._near_peak_interval(db_path, event_date="2026-06-30") == pytest.approx(0.8)


def test_near_peak_interval_caps_normal_poll_for_fast_direct_reaction(monkeypatch, tmp_path):
    db_path = tmp_path / "archive.db"
    db_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_POLL_SEC", "5")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_NEAR_PEAK_POLL_SEC", "3")
    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_MAX_POLL_SEC", raising=False)
    monkeypatch.setattr(poller, "_awc_release_watch_interval", lambda: None)
    monkeypatch.setattr(poller, "_load_latest_fact_payload", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        poller,
        "_decision_window_state",
        lambda *args, **kwargs: {"allowed": False},
    )

    assert poller._near_peak_interval(db_path, event_date="2026-06-30") == pytest.approx(1.0)


def test_awc_refresh_hours_tracks_local_day_with_margin(monkeypatch):
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_DYNAMIC_HOURS_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_HOURS", "48")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_DYNAMIC_MIN_HOURS", "8")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_DYNAMIC_MARGIN_HOURS", "2")

    assert poller._awc_refresh_hours(
        "2026-06-30",
        datetime(2026, 6, 29, 16, 5, tzinfo=timezone.utc),
    ) == 8
    assert poller._awc_refresh_hours(
        "2026-06-30",
        datetime(2026, 6, 30, 4, 3, tzinfo=timezone.utc),
    ) == 15
    assert poller._awc_refresh_hours(
        "2026-06-30",
        datetime(2026, 6, 30, 15, 30, tzinfo=timezone.utc),
    ) == 26


def test_awc_refresh_hours_uses_static_when_dynamic_disabled(monkeypatch):
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_DYNAMIC_HOURS_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_HOURS", "36")

    assert poller._awc_refresh_hours(
        "2026-06-30",
        datetime(2026, 6, 30, 4, 3, tzinfo=timezone.utc),
    ) == 36


def test_poll_once_refreshes_metar_pws_lane_before_decision_when_enabled(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    order = []
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_PWS_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_PWS_BLOCKING", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setattr(
        poller,
        "_refresh_awc_fact",
        lambda *args, **kwargs: order.append("awc") or {"ok": True, "status": "awc_test"},
    )
    monkeypatch.setattr(
        poller,
        "_refresh_metar_pws_multi_source_fact",
        lambda *args, **kwargs: order.append("metar_pws")
        or {"ok": True, "status": "updated", "sources": []},
    )
    monkeypatch.setattr(
        poller,
        "record_hard_source_direct_decisions",
        lambda *args, **kwargs: order.append("decision")
        or {"ok": False, "status": "hard_observation_missing"},
    )

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert order == ["awc", "metar_pws", "decision"]
    assert result["metar_pws"]["status"] == "updated"


def test_poll_once_spawns_metar_pws_background_without_blocking_decision(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    order = []
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_PWS_ENABLED", "1")
    monkeypatch.delenv("PM_HARD_SOURCE_METAR_PWS_BLOCKING", raising=False)
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_PWS_BACKGROUND_REFRESH", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_ENABLED", "0")
    monkeypatch.setattr(
        poller,
        "_refresh_awc_fact",
        lambda *args, **kwargs: order.append("awc") or {"ok": True, "status": "awc_test"},
    )
    monkeypatch.setattr(
        poller,
        "_refresh_metar_pws_multi_source_fact",
        lambda *args, **kwargs: order.append("inline_metar_pws") or {"ok": True, "status": "updated"},
    )
    monkeypatch.setattr(
        poller,
        "_spawn_metar_pws_refresh",
        lambda *args, **kwargs: order.append("metar_pws_background")
        or {"ok": True, "status": "metar_pws_background_started", "pid": 123},
    )
    monkeypatch.setattr(
        poller,
        "record_hard_source_direct_decisions",
        lambda *args, **kwargs: order.append("decision")
        or {"ok": False, "status": "hard_observation_missing"},
    )

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert order == ["awc", "metar_pws_background", "decision"]
    assert result["metar_pws"]["status"] == "metar_pws_background_started"


def test_poll_once_skips_metar_pws_by_default_for_order_fast_path(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    order = []
    monkeypatch.setenv("PM_HARD_SOURCE_METAR_PWS_ENABLED", "1")
    monkeypatch.delenv("PM_HARD_SOURCE_METAR_PWS_BLOCKING", raising=False)
    monkeypatch.delenv("PM_HARD_SOURCE_METAR_PWS_BACKGROUND_REFRESH", raising=False)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_ENABLED", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_ENABLED", "0")
    monkeypatch.setattr(
        poller,
        "_refresh_awc_fact",
        lambda *args, **kwargs: order.append("awc") or {"ok": True, "status": "awc_test"},
    )
    monkeypatch.setattr(
        poller,
        "_refresh_metar_pws_multi_source_fact",
        lambda *args, **kwargs: order.append("inline_metar_pws") or {"ok": True, "status": "updated"},
    )
    monkeypatch.setattr(
        poller,
        "_spawn_metar_pws_refresh",
        lambda *args, **kwargs: order.append("metar_pws_background")
        or {"ok": True, "status": "metar_pws_background_started"},
    )
    monkeypatch.setattr(
        poller,
        "record_hard_source_direct_decisions",
        lambda *args, **kwargs: order.append("decision")
        or {"ok": False, "status": "hard_observation_missing"},
    )

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert order == ["awc", "decision"]
    assert result["metar_pws"]["reason"] == "metar_pws_non_blocking_fast_path"


def test_monitor_launchd_plist_declares_metar_pws_poll_env():
    with (ROOT / "launchd" / "com.weather.monitor.daemon.plist").open("rb") as handle:
        plist = plistlib.load(handle)

    env = plist["EnvironmentVariables"]

    assert env["PM_HARD_SOURCE_AWC_RELEASE_WATCH_ENABLED"] == "1"
    assert env["PM_HARD_SOURCE_AWC_RELEASE_WATCH_WINDOW_MIN"] == "8"
    assert env["PM_HARD_SOURCE_AWC_RELEASE_WATCH_POLL_SEC"] == "1"
    assert env["PM_HARD_SOURCE_AWC_DYNAMIC_HOURS_ENABLED"] == "1"
    assert env["PM_HARD_SOURCE_AWC_DYNAMIC_MIN_HOURS"] == "8"
    assert env["PM_HARD_SOURCE_AWC_DYNAMIC_MARGIN_HOURS"] == "2"
    assert env["PM_HARD_SOURCE_METAR_PWS_ENABLED"] == "1"
    assert env["PM_HARD_SOURCE_METAR_PWS_BLOCKING"] == "0"
    assert env["PM_HARD_SOURCE_METAR_PWS_BACKGROUND_REFRESH"] == "0"
    assert env["PM_HARD_SOURCE_METAR_IEM_PEAK_POLL_SEC"] == "30"
    assert env["PM_HARD_SOURCE_METAR_IEM_NORMAL_POLL_SEC"] == "300"
    assert env["PM_HARD_SOURCE_METAR_AVWX_PEAK_POLL_SEC"] == "15"
    assert env["PM_HARD_SOURCE_METAR_AVWX_NORMAL_POLL_SEC"] == "60"
    assert env["PM_HARD_SOURCE_METAR_CHECKWX_PEAK_POLL_SEC"] == "15"
    assert env["PM_HARD_SOURCE_METAR_CHECKWX_NORMAL_POLL_SEC"] == "60"
    assert env["PM_HARD_SOURCE_TWC_PWS_RAPID_PEAK_POLL_SEC"] == "10"
    assert env["PM_HARD_SOURCE_TWC_PWS_RAPID_NORMAL_POLL_SEC"] == "60"


def test_poll_once_inline_worker_uses_supplied_db_path(tmp_path, monkeypatch):
    default_db = tmp_path / "default.db"
    supplied_db = tmp_path / "supplied.db"
    _create_schema(default_db)
    _create_schema(supplied_db)
    slug, event_date = _insert_fact(supplied_db)
    monkeypatch.setenv("PM_AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_AWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_HEARTBEAT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_TIMEOUT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_BACKOFF_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_FACTS_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DB_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_POSITIONS_SYNC", "0")
    monkeypatch.setenv("PM_POSITIONS_ASSUME_ZERO", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_RULES_LIVE_HYDRATE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_REFRESH_ON_STALE", "0")
    monkeypatch.setattr(swm, "DB_PATH", default_db)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)

    result = poller.poll_once(supplied_db, event_date=event_date, slug=slug)

    assert result["ok"] is True
    assert result["posterior_target_fast_precheck"]["job_id"] or result["decision"].get("job_id")
    assert result["worker"]["ok"] is True
    supplied_conn = sqlite3.connect(supplied_db)
    default_conn = sqlite3.connect(default_db)
    supplied_jobs = supplied_conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0]
    default_jobs = default_conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0]
    assert supplied_jobs == 1
    assert default_jobs == 0


def test_hard_source_inline_worker_defaults_to_cached_prefetch(monkeypatch):
    captured = {}
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS", "16")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC", "24")
    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_WORKER_PREFETCH_MARKET_TICKS", raising=False)
    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_PREFETCH_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_PREFETCH_BUDGET_SEC", raising=False)

    def fake_worker_once():
        captured.update(
            {
                "event_prefetch": os.environ.get("PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS"),
                "entry_attempts": os.environ.get("PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS"),
                "entry_budget": os.environ.get("PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC"),
            }
        )
        return {"ok": True, "picked": False}

    monkeypatch.setattr(poller.swm, "run_auto_trade_job_worker_once", fake_worker_once)

    result = poller._run_worker_once_inline()

    assert result["ok"] is True
    assert captured == {
        "event_prefetch": "0",
        "entry_attempts": "0",
        "entry_budget": "0",
    }
    assert os.environ["PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS"] == "1"
    assert os.environ["PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS"] == "16"
    assert os.environ["PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC"] == "24"


def test_hard_source_inline_worker_sets_direct_claim_lock_budget(monkeypatch):
    captured = {}

    def fake_worker_once():
        captured.update(
            {
                "claim_retry": os.environ.get("PM_AUTO_TRADE_DIRECT_JOB_CLAIM_RETRY"),
                "claim_backoff": os.environ.get("PM_AUTO_TRADE_DIRECT_JOB_CLAIM_BACKOFF_SEC"),
                "claim_cap": os.environ.get("PM_AUTO_TRADE_DIRECT_JOB_CLAIM_BACKOFF_CAP_SEC"),
                "db_timeout": os.environ.get("PM_AUTO_TRADE_DIRECT_JOB_DB_CONNECT_TIMEOUT_SEC"),
                "busy_timeout": os.environ.get("PM_AUTO_TRADE_DIRECT_JOB_DB_BUSY_TIMEOUT_MS"),
                "runtime_lock_wait": os.environ.get("PM_AUTO_TRADE_DIRECT_RUNTIME_LOCK_WAIT_SEC"),
                "bookkeeping_connect": os.environ.get(
                    "PM_AUTO_TRADE_JOB_BOOKKEEPING_CONNECT_TIMEOUT_SEC"
                ),
                "bookkeeping_busy": os.environ.get(
                    "PM_AUTO_TRADE_JOB_BOOKKEEPING_BUSY_TIMEOUT_MS"
                ),
                "skip_run_row": os.environ.get("PM_AUTO_TRADE_DIRECT_JOB_SKIP_RUN_ROW"),
                "skip_entry_balance_guard": os.environ.get(
                    "PM_AUTO_TRADE_DIRECT_SKIP_ENTRY_BALANCE_GUARD"
                ),
            }
        )
        return {"ok": True, "picked": False}

    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_WORKER_CLAIM_RETRY", raising=False)
    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_WORKER_CLAIM_BACKOFF_SEC", raising=False)
    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_WORKER_CLAIM_BACKOFF_CAP_SEC", raising=False)
    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_WORKER_DB_CONNECT_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_WORKER_DB_BUSY_TIMEOUT_MS", raising=False)
    monkeypatch.setattr(poller.swm, "run_auto_trade_job_worker_once", fake_worker_once)

    result = poller._run_worker_once_inline()

    assert result["ok"] is True
    assert captured == {
        "claim_retry": "8",
        "claim_backoff": "0.100",
        "claim_cap": "0.500",
        "db_timeout": "1.000",
        "busy_timeout": "200",
        "runtime_lock_wait": "0",
        "bookkeeping_connect": "0.150",
        "bookkeeping_busy": "200",
        "skip_run_row": "1",
        "skip_entry_balance_guard": "1",
    }


def test_hard_source_job_worker_executes_new_entry_and_old_exit_dry_run(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_TIMEOUT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_BACKOFF_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_FACTS_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DB_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_POSITIONS_SYNC", "0")
    monkeypatch.setenv("PM_POSITIONS_ASSUME_ZERO", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_ENTRY_MIN_AGE_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_RULES_LIVE_HYDRATE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_REFRESH_ON_STALE", "0")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert result["ok"] is True
    assert result["job_id"]
    assert result["ssot_fact_id"]
    assert result["old_exit_count"] == 1

    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)

    worker = swm.run_auto_trade_job_worker_once()

    assert worker["ok"] is True
    assert worker["picked"] is True
    assert worker["result"]["processed"] == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    decisions = conn.execute(
        "SELECT * FROM auto_trade_decisions ORDER BY id"
    ).fetchall()

    actual_entry = [
        row for row in decisions
        if row["action"] == "entry"
        and row["outcome"] == "31\u00b0C"
        and row["side"] == "YES"
        and row["decided_state"] == "entry_dry_run"
        and "order_submitted" in json.loads(row["reason_codes_json"] or "[]")
    ]
    assert len(actual_entry) == 1
    entry_details = json.loads(actual_entry[0]["details_json"] or "{}")
    assert entry_details["dry_run"] is True
    assert entry_details["status"] == "dry_run"
    assert entry_details["token_id"] == "yes31-token"
    assert entry_details["order_type"] == "GTC"
    assert entry_details["price"] == 0.24
    assert entry_details["hard_source_direct"] is True
    assert entry_details["hard_source_ssot_written_at"]
    assert entry_details["hard_source_job_enqueued_at"]
    assert entry_details["hard_source_worker_started_at"]
    assert "hard_source_order_submitted_at" not in entry_details
    assert entry_details.get("order_submission_time_quality") != "exchange_post_started_at"
    assert "hard_source_ssot_to_order_ms" not in entry_details
    assert "hard_source_worker_to_order_ms" not in entry_details
    assert entry_details["ssot_target_bucket"] == "31°C"
    assert entry_details["decision_ssot"] == "auto_trade_target_bucket"
    assert entry_details["decision_basis"] == "hard_source_lower_bound_constraint"
    assert entry_details["bayes_recomputed_async"] is True
    assert entry_details["fact_recompute_async"] is True
    assert entry_details["ssot_target_bucket"] == "31°C"
    assert entry_details["decision_ssot"] == "auto_trade_target_bucket"
    assert entry_details["decision_basis"] == "hard_source_lower_bound_constraint"
    assert entry_details["bayes_recomputed_async"] is True
    assert entry_details["fact_recompute_async"] is True

    actual_exit = [
        row for row in decisions
        if row["action"] == "exit"
        and row["outcome"] == "30\u00b0C"
        and row["side"] == "YES"
        and row["decided_state"] == "exit_dry_run"
        and "order_submitted" in json.loads(row["reason_codes_json"] or "[]")
    ]
    assert len(actual_exit) == 1
    exit_details = json.loads(actual_exit[0]["details_json"] or "{}")
    assert exit_details["hard_source_direct"] is True
    assert exit_details["hard_source_ssot_written_at"]
    assert exit_details["hard_source_worker_started_at"]
    assert "hard_source_order_submitted_at" not in exit_details
    assert exit_details["order_submission_time_quality"] == "exchange_post_t5_missing"
    assert "hard_source_ssot_to_order_ms" not in exit_details
    assert "hard_source_worker_to_order_ms" not in exit_details
    assert exit_details["ssot_target_bucket"] == "31°C"
    assert exit_details["decision_ssot"] == "auto_trade_target_bucket"
    assert exit_details["decision_basis"] == "hard_source_lower_bound_constraint"
    assert exit_details["bayes_recomputed_async"] is True
    assert exit_details["fact_recompute_async"] is True
    assert exit_details["ssot_target_bucket"] == "31°C"
    assert exit_details["decision_ssot"] == "auto_trade_target_bucket"
    assert exit_details["decision_basis"] == "hard_source_lower_bound_constraint"
    assert exit_details["bayes_recomputed_async"] is True
    assert exit_details["fact_recompute_async"] is True

    old_trade = conn.execute(
        """
        SELECT state, last_action, last_status, last_error, position_size,
               last_action_price, last_action_size, last_action_order_type,
               exit_skip_reason, exit_skip_detail, exit_reason_codes
        FROM edge_trades
        WHERE slug = ? AND event_date = ? AND outcome = ? AND side = 'YES'
        """,
        (slug, event_date, "30\u00b0C"),
    ).fetchone()
    assert old_trade["state"] == "entry"
    assert old_trade["last_action"] == "exit_skip"
    assert old_trade["last_status"] == "dry_run_exit_no_state_mutation"
    assert old_trade["last_error"] == ""
    assert old_trade["position_size"] == 8.0
    assert old_trade["last_action_price"] == 0.39
    audit = hard_source_audit.audit(
        db_path,
        slug=slug,
        event_date=event_date,
        max_fetch_to_ssot_ms=2000,
        max_ssot_to_job_enqueue_ms=2000,
        max_job_enqueue_to_worker_ms=2000,
        max_ssot_to_order_ms=2000,
        max_job_enqueue_to_order_ms=2000,
        max_fetch_to_order_ms=2000,
    )
    assert audit["ok"] is True
    assert audit["checks"]["latest_fact_is_hard_source_ssot"] is True
    assert audit["checks"]["target_entry_order_decision_present"] is True
    assert audit["checks"]["old_bucket_exit_order_decision_present"] is True
    assert audit["checks"]["job_has_constrained_snapshot"] is True
    assert audit["checks"]["job_snapshot_matches_ssot_target"] is True
    assert audit["checks"]["job_snapshot_target_is_top_posterior"] is True
    assert audit["checks"]["latest_fact_target_is_top_posterior"] is True
    assert audit["checks"]["job_snapshot_final_lock_strict_if_applicable"] is True
    assert audit["checks"]["latest_fact_final_lock_strict_if_applicable"] is True
    assert audit["checks"]["order_decisions_follow_ssot_target"] is True
    assert audit["checks"]["order_decisions_mark_async_recompute"] is True
    assert audit["checks"]["order_decisions_no_legacy_bypass_semantics"] is True
    assert audit["checks"]["hard_source_fast_path_not_waiting_full_fact_bayes"] is True
    assert audit["checks"]["hard_source_runtime_contract_present"] is True
    assert (
        audit["details"]["hard_source_runtime_contract_version"]
        == poller.HARD_SOURCE_RUNTIME_CONTRACT_VERSION
    )
    assert audit["checks"]["fetch_to_ssot_within_sla"] is True
    assert audit["checks"]["ssot_to_job_enqueue_within_sla"] is True
    assert audit["checks"]["job_enqueue_to_worker_within_sla"] is True
    assert audit["checks"]["ssot_to_order_within_sla"] is True
    assert audit["checks"]["job_enqueue_to_order_within_sla"] is True
    assert audit["checks"]["fetch_to_order_within_sla"] is True
    assert audit["details"]["target_bucket"] == "31\u00b0C"
    assert audit["details"]["exit_outcome"] == "30\u00b0C"
    assert old_trade["last_action_size"] == 8.0
    assert old_trade["last_action_order_type"] == "FAK"
    assert old_trade["exit_skip_reason"] == "dry_run_exit_no_state_mutation"
    assert "dry_run_sell=8.0000@0.3900" in old_trade["exit_skip_detail"]
    assert "target_realization_switch_buy_exit_old" in old_trade["exit_reason_codes"]
    assert "target_realization_exit_clear" in old_trade["exit_reason_codes"]


def test_hard_source_nonfinal_old_bucket_exits_after_target_entry_submitted(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_nonfinal_switch_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_TIMEOUT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_BACKOFF_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_FACTS_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DB_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_POSITIONS_SYNC", "0")
    monkeypatch.setenv("PM_POSITIONS_ASSUME_ZERO", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_ENTRY_MIN_AGE_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_RULES_LIVE_HYDRATE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_REFRESH_ON_STALE", "0")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert result["ok"] is True
    assert result["target_bucket"] == "33\u00b0C"
    assert result["observed_bucket"] == "32\u00b0C"
    assert result["old_exit_count"] == 1

    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)
    worker = swm.run_auto_trade_job_worker_once()
    assert worker["ok"] is True
    assert worker["picked"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    decisions = conn.execute(
        "SELECT * FROM auto_trade_decisions ORDER BY id"
    ).fetchall()
    actual_entry = [
        row for row in decisions
        if row["action"] == "entry"
        and row["outcome"] == "33\u00b0C"
        and row["side"] == "YES"
        and row["decided_state"] == "entry_dry_run"
        and "order_submitted" in json.loads(row["reason_codes_json"] or "[]")
    ]
    assert len(actual_entry) == 1
    entry_details = json.loads(actual_entry[0]["details_json"] or "{}")
    assert entry_details["hard_source_direct"] is True
    assert entry_details["ssot_target_bucket"] == "33\u00b0C"

    actual_exit = [
        row for row in decisions
        if row["action"] == "exit"
        and row["outcome"] == "32\u00b0C"
        and row["side"] == "YES"
        and row["decided_state"] == "exit_dry_run"
        and "order_submitted" in json.loads(row["reason_codes_json"] or "[]")
    ]
    assert len(actual_exit) == 1
    exit_details = json.loads(actual_exit[0]["details_json"] or "{}")
    assert exit_details["hard_source_direct"] is True
    assert exit_details["ssot_target_bucket"] == "33\u00b0C"

    old_trade = conn.execute(
        """
        SELECT state, last_action, last_status, position_size, exit_skip_reason,
               exit_skip_detail, exit_reason_codes
        FROM edge_trades
        WHERE slug = ? AND event_date = ? AND outcome = ? AND side = 'YES'
        """,
        (slug, event_date, "32\u00b0C"),
    ).fetchone()
    assert old_trade["state"] == "entry"
    assert old_trade["last_action"] == "exit_skip"
    assert old_trade["last_status"] == "dry_run_exit_no_state_mutation"
    assert old_trade["position_size"] == 8.0
    assert old_trade["exit_skip_reason"] == "dry_run_exit_no_state_mutation"
    assert "dry_run_sell=8.0000@0.1800" in old_trade["exit_skip_detail"]
    assert "target_realization_switch_buy_exit_old" in old_trade["exit_reason_codes"]


def test_hard_source_job_worker_executes_new_entry_and_old_exit_fake_live(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    _create_signal_to_order_timing_table(db_path)
    _create_awc_hard_source_signals_table(db_path)
    slug, event_date = _insert_fact(db_path)
    fixed_valid = "2026-06-19T09:00:00Z"
    fixed_seen = "2026-06-19T09:05:38Z"
    fixed_fetch = "2026-06-19T09:05:37Z"
    fixed_submit = "2026-06-19T09:05:40Z"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT h.id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    fact_payload = json.loads(latest["fact_json"])
    features = dict(fact_payload["weather_likelihood_features"])
    features["zuuu_metar_raw_awc_today_max_at"] = fixed_valid
    features["zuuu_metar_raw_awc_latest_at"] = fixed_valid
    features["zuuu_metar_raw_awc_fetched_at"] = fixed_fetch
    fact_payload["weather_likelihood_features"] = features
    conn.execute(
        "UPDATE event_fact_history SET fact_as_of = ?, fact_json = ? WHERE id = ?",
        (fixed_fetch, json.dumps(fact_payload, ensure_ascii=True), latest["id"]),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PM_AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_TIMEOUT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_BACKOFF_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_FACTS_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_DB_LOCK_WAIT_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_TRADES", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_SYNC_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_POSITIONS_SYNC", "0")
    monkeypatch.setenv("PM_POSITIONS_ASSUME_ZERO", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_ENTRY_MIN_AGE_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_RULES_LIVE_HYDRATE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_ENTRY_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_REFRESH_ON_STALE", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_MARKET_EXIT_PROBE_REFRESH_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_NOT_FOUND_RECHECK_DELAY_SEC", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_CONFIRM_NOT_FOUND_REFRESH_OPEN_ORDERS", "0")
    monkeypatch.setenv("PM_AUTO_TRADE_MIN_EXIT_SIZE", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_DUST_MIN", "0.01")
    monkeypatch.setenv("PM_MIN_ORDER_SIZE", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_EXIT_ALLOW_SMALL", "1")
    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(swm, "POSITIONS_SYNC_ENABLED", False)
    monkeypatch.setattr(swm, "POSITIONS_ASSUME_ZERO", False)
    monkeypatch.setattr(poller, "_utc_now_iso", lambda: fixed_seen)
    monkeypatch.setattr(swm, "utc_now_iso", lambda: fixed_submit)
    monkeypatch.setattr(swm, "utc_now_iso_ms", lambda: fixed_submit)

    placed = []

    def fake_run_node_order(cmd, token_id="", side="", *, price=None, size=None, order_type=None, amount=None, order_context=None):
        placed.append(
            {
                "cmd": cmd,
                "token_id": token_id,
                "side": side,
                "price": price,
                "size": size,
                "order_type": order_type,
                "amount": amount,
            }
        )
        if cmd == "cancel-orders":
            return True, {"status": "cancelled", "count": 1}
        common = {
            "status": "submitted",
            "exchange_post_started": True,
            "exchange_post_started_at": fixed_submit,
            "order_submitted_at": fixed_submit,
        }
        if side == "BUY":
            return True, {**common, "orderID": "fake-buy-31"}
        if side == "SELL":
            return True, {**common, "orderID": "fake-sell-30"}
        return True, {**common, "orderID": "fake-order"}

    def fake_run_node_get_order(order_id):
        submitted_buy = next(
            (
                call for call in placed
                if call["cmd"] == "place-order"
                and call["side"] == "BUY"
                and call["token_id"] == "yes31-token"
            ),
            {},
        )
        orders = {
            "old-order-30": {"status": "confirmed", "price": "0.40", "side": "BUY"},
            "fake-buy-31": {
                "status": "confirmed",
                "size_matched": str(submitted_buy.get("size") or "43.47826087"),
                "price": str(submitted_buy.get("price") or "0.23"),
                "side": "BUY",
            },
            "fake-sell-30": {
                "status": "confirmed",
                "size_matched": "8.0",
                "price": "0.39",
                "side": "SELL",
            },
        }
        order = orders.get(order_id)
        if order is None:
            return False, {"error": "not found"}
        return True, {"order": order}

    def fake_run_node_balance(token_id="", refresh=True, asset_type="CONDITIONAL"):
        if str(asset_type or "").upper() == "COLLATERAL":
            return True, {
                "balance": "100000000",
                "allowance": "100000000",
                "balance_allowance": "100000000",
            }
        return True, {
            "balance": "8000000",
            "allowance": "8000000",
            "balance_allowance": "8000000",
        }

    monkeypatch.setattr(swm, "_run_node_order", fake_run_node_order)
    monkeypatch.setattr(swm, "_run_node_get_order", fake_run_node_get_order)
    monkeypatch.setattr(swm, "_run_node_balance", fake_run_node_balance)

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )
    assert result["ok"] is True
    assert result["job_id"]
    assert result["ssot_fact_id"]
    assert result["old_exit_count"] == 1

    worker = swm.run_auto_trade_job_worker_once()

    assert worker["ok"] is True
    assert worker["picked"] is True
    assert worker["result"]["processed"] == 1

    buy_calls = [
        call for call in placed
        if call["cmd"] == "place-order" and call["side"] == "BUY"
    ]
    sell_calls = [
        call for call in placed
        if call["cmd"] == "place-order" and call["side"] == "SELL"
    ]
    expected_buy_price = buy_calls[0]["price"] if buy_calls else None
    expected_buy_size = buy_calls[0]["size"] if buy_calls else None
    assert buy_calls == [
        {
            "cmd": "place-order",
            "token_id": "yes31-token",
            "side": "BUY",
            "price": expected_buy_price,
            "size": expected_buy_size,
            "order_type": "GTC",
            "amount": None,
        }
    ]
    assert sell_calls == [
        {
            "cmd": "place-order",
            "token_id": "yes30-token",
            "side": "SELL",
            "price": 0.39,
            "size": 8.0,
            "order_type": "FAK",
            "amount": None,
        }
    ]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    decisions = conn.execute(
        "SELECT * FROM auto_trade_decisions ORDER BY id"
    ).fetchall()
    actual_entry = [
        row for row in decisions
        if row["action"] == "entry"
        and row["outcome"] == "31\u00b0C"
        and row["side"] == "YES"
        and row["decided_state"] == "entry"
        and "order_submitted" in json.loads(row["reason_codes_json"] or "[]")
    ]
    assert len(actual_entry) == 1
    entry_details = json.loads(actual_entry[0]["details_json"] or "{}")
    assert entry_details["dry_run"] is False
    assert entry_details["status"] == "confirmed"
    assert entry_details["order_id"] == "fake-buy-31"
    assert entry_details["token_id"] == "yes31-token"
    assert entry_details["order_type"] == "GTC"
    confirmed_buy_price = entry_details["price"]
    confirmed_buy_size = entry_details["size"]
    assert confirmed_buy_price is not None
    assert confirmed_buy_size is not None
    assert entry_details["hard_source_direct"] is True
    assert entry_details["hard_source_ssot_written_at"]
    assert entry_details["hard_source_job_enqueued_at"]
    assert entry_details["hard_source_worker_started_at"]
    assert entry_details["hard_source_order_submitted_at"]
    assert entry_details["hard_source_ssot_to_order_ms"] is not None
    assert entry_details["hard_source_worker_to_order_ms"] is not None
    assert entry_details["awc_hard_source_signal_at"]
    assert entry_details["signal_to_order_ledger_status"] == "pending_upsert"
    assert entry_details["signal_to_order_slo_met"] is True

    actual_exit = [
        row for row in decisions
        if row["action"] == "exit"
        and row["outcome"] == "30\u00b0C"
        and row["side"] == "YES"
        and row["decided_state"] == "exit"
        and "exit_marketable_immediate_confirmed" in json.loads(row["reason_codes_json"] or "[]")
    ]
    assert len(actual_exit) == 1
    exit_details = json.loads(actual_exit[0]["details_json"] or "{}")
    assert exit_details["order_id"] == "fake-sell-30"
    assert exit_details["hard_source_direct"] is True
    assert exit_details["hard_source_ssot_written_at"]
    assert exit_details["hard_source_worker_started_at"]
    assert exit_details["hard_source_order_submitted_at"]
    assert exit_details["hard_source_ssot_to_order_ms"] is not None
    assert exit_details["hard_source_worker_to_order_ms"] is not None

    ledger = conn.execute(
        "SELECT * FROM signal_to_order_timing WHERE signal_id = ?",
        (entry_details["signal_id"],),
    ).fetchone()
    assert ledger is not None
    assert ledger["decision_id"] == str(actual_entry[0]["id"])
    assert ledger["awc_hard_source_signal_at"] == entry_details["awc_hard_source_signal_at"]
    assert ledger["order_submitted_at"] == entry_details["hard_source_order_submitted_at"]
    ledger_timing = json.loads(ledger["timing_json"])
    assert ledger_timing["terminal_state"] == "submitted"
    assert ledger_timing["order_submitted_at"] == entry_details["hard_source_order_submitted_at"]
    assert ledger_timing["signal_to_order_slo_met"] is True
    signal = conn.execute(
        "SELECT * FROM awc_hard_source_signals WHERE signal_id = ?",
        (entry_details["signal_id"],),
    ).fetchone()
    assert signal is not None
    assert signal["slug"] == slug
    assert signal["event_date"] == event_date
    assert signal["awc_hard_source_signal_at"] == entry_details["awc_hard_source_signal_at"]
    assert signal["acceptance_state"] == "accepted"
    assert signal["terminal_state"] == "submitted"
    assert signal["state_reason"] == "order_submitted"

    orders = conn.execute(
        """
        SELECT order_id, outcome, side, action, price, size, order_type, strategy,
               decision_id, signal_id, order_intent_id
        FROM auto_orders
        ORDER BY created_at, order_id
        """
    ).fetchall()
    orders_payload = [dict(row) for row in orders]
    entry_order = next(row for row in orders_payload if row["action"] == "entry")
    exit_order = next(row for row in orders_payload if row["action"] == "exit")
    assert entry_order["decision_id"] == str(actual_entry[0]["id"])
    assert entry_order["signal_id"] == entry_details["signal_id"]
    assert entry_order["order_intent_id"]
    assert {
        key: entry_order[key]
        for key in ("order_id", "outcome", "side", "action", "price", "size", "order_type", "strategy")
    } == {
        "order_id": "fake-buy-31",
        "outcome": "31\u00b0C",
        "side": "YES",
        "action": "entry",
        "price": entry_order["price"],
        "size": entry_order["size"],
        "order_type": "GTC",
        "strategy": "posterior_settlement",
    }
    assert round(float(entry_order["price"]), 6) == round(float(expected_buy_price), 6)
    assert round(float(entry_order["size"]), 4) == round(float(expected_buy_size), 4)
    assert {
        key: exit_order[key]
        for key in ("order_id", "outcome", "side", "action", "price", "size", "order_type", "strategy")
    } == {
        "order_id": "fake-sell-30",
        "outcome": "30\u00b0C",
        "side": "YES",
        "action": "exit",
        "price": 0.39,
        "size": 8.0,
        "order_type": "FAK",
        "strategy": "exit",
    }

    old_trade = conn.execute(
        """
        SELECT state, last_action, last_status, last_error, position_size,
               last_order_id, last_action_price, last_action_size,
               last_action_order_type, exit_reason_codes
        FROM edge_trades
        WHERE slug = ? AND event_date = ? AND outcome = ? AND side = 'YES'
        """,
        (slug, event_date, "30\u00b0C"),
    ).fetchone()
    assert old_trade["state"] == "exit"
    assert old_trade["last_action"] == "confirm_exit"
    assert old_trade["last_status"] == "confirmed"
    assert old_trade["last_error"] == ""
    assert old_trade["position_size"] == 0.0
    assert old_trade["last_order_id"] == "fake-sell-30"
    audit = hard_source_audit.audit(
        db_path,
        slug=slug,
        event_date=event_date,
        max_fetch_to_ssot_ms=2000,
        max_ssot_to_job_enqueue_ms=2000,
        max_job_enqueue_to_worker_ms=2000,
        max_ssot_to_order_ms=2000,
        max_job_enqueue_to_order_ms=2000,
        max_fetch_to_order_ms=2000,
    )
    assert audit["ok"] is True
    assert audit["checks"]["latest_fact_is_hard_source_ssot"] is True
    assert audit["checks"]["target_entry_order_decision_present"] is True
    assert audit["checks"]["old_bucket_exit_order_decision_present"] is True
    assert audit["checks"]["job_has_constrained_snapshot"] is True
    assert audit["checks"]["job_snapshot_matches_ssot_target"] is True
    assert audit["checks"]["job_snapshot_target_is_top_posterior"] is True
    assert audit["checks"]["latest_fact_target_is_top_posterior"] is True
    assert audit["checks"]["job_snapshot_final_lock_strict_if_applicable"] is True
    assert audit["checks"]["latest_fact_final_lock_strict_if_applicable"] is True
    assert audit["checks"]["order_decisions_follow_ssot_target"] is True
    assert audit["checks"]["order_decisions_mark_async_recompute"] is True
    assert audit["checks"]["order_decisions_no_legacy_bypass_semantics"] is True
    assert audit["checks"]["hard_source_fast_path_not_waiting_full_fact_bayes"] is True
    assert audit["checks"]["hard_source_runtime_contract_present"] is True
    assert (
        audit["details"]["hard_source_runtime_contract_version"]
        == poller.HARD_SOURCE_RUNTIME_CONTRACT_VERSION
    )
    assert audit["checks"]["fetch_to_ssot_within_sla"] is True
    assert audit["checks"]["ssot_to_job_enqueue_within_sla"] is True
    assert audit["checks"]["job_enqueue_to_worker_within_sla"] is True
    assert audit["checks"]["ssot_to_order_within_sla"] is True
    assert audit["checks"]["job_enqueue_to_order_within_sla"] is True
    assert audit["checks"]["fetch_to_order_within_sla"] is True
    assert audit["details"]["target_bucket"] == "31\u00b0C"
    assert audit["details"]["exit_outcome"] == "30\u00b0C"
    assert old_trade["last_action_price"] == 0.39
    assert old_trade["last_action_size"] == 8.0
    assert old_trade["last_action_order_type"] == "FAK"
    assert "target_realization_switch_buy_exit_old" in old_trade["exit_reason_codes"]
    assert "target_realization_exit_clear" in old_trade["exit_reason_codes"]


def test_hard_source_constraint_keeps_higher_bucket_competing(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute("SELECT * FROM event_fact_history ORDER BY id DESC LIMIT 1").fetchone()
    payload = json.loads(latest["fact_json"])
    payload["distribution"].append(
        {
            "outcome": "32\u00b0C",
            "q_entry": 0.6,
            "q_round": 0.6,
            "q_bayes_posterior": 0.6,
            "best_bid": 0.31,
            "best_ask": 0.34,
            "spread": 0.03,
            "yes_token": "yes32",
        }
    )
    for item in payload["distribution"]:
        if item["outcome"] == "30\u00b0C":
            item["q_bayes_posterior"] = 0.3
        elif item["outcome"] == "31\u00b0C":
            item["q_bayes_posterior"] = 0.1
    conn.execute("UPDATE event_fact_history SET fact_json = ? WHERE id = ?", (json.dumps(payload), latest["id"]))
    conn.execute(
        """
        INSERT INTO market_tokens(slug, event_date, outcome, yes_token, no_token, market_active,
                                  market_closed, market_archived, market_accepting_orders,
                                  market_enable_orderbook, updated_at)
        VALUES(?, ?, '32\u00b0C', 'yes32-token', 'no32-token', 1, 0, 0, 1, 1, ?)
        """,
        (slug, event_date, "2026-06-19T07:00:00+00:00"),
    )
    conn.execute(
        """
        INSERT INTO event_market_ticks(run_date, event_date, slug, outcome, side,
                                       best_bid, best_ask, mid, spread,
                                       bid_depth_topN, ask_depth_topN, source, created_at)
        VALUES(?, ?, ?, '32\u00b0C', 'YES', 0.31, 0.34, 0.325, 0.03, 100, 120, 'clob_rest_refresh', ?)
        """,
        ("2026-06-18", event_date, slug, "2026-06-19T07:00:02+00:00"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["observed_bucket"] == "31\u00b0C"
    assert result["target_bucket"] == "32\u00b0C"
    decisions = sqlite3.connect(db_path).execute(
        "SELECT action, outcome, details_json FROM auto_trade_decisions ORDER BY id"
    ).fetchall()
    assert decisions[0][0:2] == ("entry", "32\u00b0C")
    details = json.loads(decisions[0][2])
    assert details["hard_source_observed_bucket"] == "31\u00b0C"
    assert details["ssot_target_bucket"] == "32\u00b0C"
    assert 0.85 < float(details["q_bayes_posterior"]) < 1.0


def test_hard_source_event_runs_settlement_triggers_final_bucket_lock(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_nonfinal_switch_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO event_runs(slug, event_date, settlement_actual, market_closed, created_at)
        VALUES(?, ?, 32.0, 1, ?)
        """,
        (slug, event_date, _utc_fixture_iso(120)),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["observed_bucket"] == "32\u00b0C"
    assert result["target_bucket"] == "32\u00b0C"
    assert result["constraint_mode"] == "final_bucket_lock"
    assert result["final_bucket_lock"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest_payload = json.loads(
        conn.execute(
            """
            SELECT h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()["fact_json"]
    )
    assert latest_payload["settlement_actual"] == 32.0
    assert latest_payload["market_closed"] == 1
    assert latest_payload["weather_likelihood_features"]["settlement_actual"] == 32.0
    by_outcome = {row["outcome"]: row for row in latest_payload["distribution"]}
    assert by_outcome["32\u00b0C"]["q_bayes_posterior"] == 1.0
    assert by_outcome["32\u00b0C"]["q_temperature_bucket_posterior"] == 1.0
    # The row-level lock flag went with the old hard-source override.  The
    # canonical projection states the same thing as a mask, and names it: a
    # bucket ABOVE the locked one is hard-zeroed too, which a lower-bound
    # constraint would never do, and the reason on those rows says so.
    assert by_outcome["32\u00b0C"].get("hard_zero") in (None, False)
    assert by_outcome["33\u00b0C"]["q_bayes_posterior"] == 0.0
    # The decision below names the basis; here the lock is the shape of the
    # distribution itself.
    assert sum(
        float(row.get("q_bayes_posterior") or 0.0) for row in latest_payload["distribution"]
    ) == 1.0

    decisions = conn.execute(
        "SELECT action, outcome, details_json FROM auto_trade_decisions ORDER BY id"
    ).fetchall()
    assert decisions[0][0:2] == ("entry", "32\u00b0C")
    entry_details = json.loads(decisions[0][2])
    assert entry_details["decision_basis"] == "final_bucket_lock"
    assert entry_details["posterior_override_q"] == 1.0
    assert entry_details["q_bayes_posterior"] == 1.0
    assert entry_details["q_temperature_bucket_posterior"] == 1.0


def test_hard_source_wu_twc_final_settlement_updates_latest_and_locks_bucket(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    fetched_at = _utc_fixture_iso(180)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_FORECAST", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_TWC_FORECAST", "0")

    def fake_wu_twc_observed(*args, **kwargs):
        return {
            "wu_twc_observed_intraday_today_max_c": 31.0,
            "wu_twc_observed_intraday_today_max_at": fetched_at,
            "wu_twc_observed_intraday_fetched_at": fetched_at,
            "wu_twc_observed_intraday_primary_max": True,
            "wu_twc_observed_intraday_stale": False,
            "observed_today_max_final": True,
            "hard_source_final_daily_max": True,
            "temperature_observation_final_daily_max": True,
            "observed_today_max_source": "wu_twc_observed_intraday",
            "observed_today_max_trusted": True,
        }

    monkeypatch.setattr(swm, "fetch_wu_twc_observed_intraday_features", fake_wu_twc_observed)

    refresh = poller._refresh_wu_twc_fact(
        db_path,
        event_date=event_date,
        slug=slug,
        heartbeat_sec=0,
    )

    assert refresh["ok"] is True
    assert refresh["status"] == "updated"
    trace = refresh["source_acquisition_trace"]
    assert trace["read_only"] is True
    assert trace["refresh_source_family"] == "wu_twc"
    assert trace["hard_observation_candidate_ids"] == []
    assert trace["source_class_counts"][SOURCE_CLASSES["diagnostic_current"]] == 1
    assert trace["source_latency_trace"][0]["source_id"] == "wu_twc_observed_intraday"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest_payload = json.loads(
        conn.execute(
            """
            SELECT h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()["fact_json"]
    )
    assert latest_payload["settlement_actual"] == 31.0
    assert latest_payload["market_closed"] == 1
    features = latest_payload["weather_likelihood_features"]
    assert features["settlement_actual"] == 31.0
    assert features["observed_today_max_c"] == 31.0
    assert features["settlement_signal_source"] == "wu_twc_final_settlement"

    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["observed_bucket"] == "31\u00b0C"
    assert result["target_bucket"] == "31\u00b0C"
    assert result["constraint_mode"] == "final_bucket_lock"
    assert result["final_bucket_lock"] is True

    latest_payload = json.loads(
        conn.execute(
            """
            SELECT h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()["fact_json"]
    )
    by_outcome = {row["outcome"]: row for row in latest_payload["distribution"]}
    assert by_outcome["31\u00b0C"]["q_bayes_posterior"] == 1.0
    assert by_outcome["31\u00b0C"]["q_temperature_bucket_posterior"] == 1.0
    assert by_outcome["30\u00b0C"]["q_bayes_posterior"] == 0.0

    decisions = conn.execute(
        "SELECT action, outcome, details_json FROM auto_trade_decisions ORDER BY id"
    ).fetchall()
    entries = [row for row in decisions if row[0:2] == ("entry", "31\u00b0C")]
    assert len(entries) == 1
    entry_details = json.loads(entries[0][2])
    assert entry_details["decision_basis"] == "final_bucket_lock"
    assert entry_details["q_bayes_posterior"] == 1.0
    assert entry_details["q_temperature_bucket_posterior"] == 1.0


def test_hard_source_final_lock_writes_single_bucket_ssot_and_orders(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute("SELECT * FROM event_fact_history ORDER BY id DESC LIMIT 1").fetchone()
    payload = json.loads(latest["fact_json"])
    payload["weather_likelihood_features"]["hard_source_final_daily_max"] = True
    payload["distribution"][0]["q_bayes_posterior"] = 0.8
    payload["distribution"][1]["q_bayes_posterior"] = 0.2
    _attach_unit_posterior_ssot_fact(payload, target_bucket="31\u00b0C", target_probability=0.2)
    conn.execute("UPDATE event_fact_history SET fact_json = ? WHERE id = ?", (json.dumps(payload), latest["id"]))
    conn.commit()
    conn.close()
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["observed_bucket"] == "31\u00b0C"
    assert result["target_bucket"] == "31\u00b0C"
    assert result["constraint_mode"] == "final_bucket_lock"
    assert result["final_bucket_lock"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest_payload = json.loads(
        conn.execute(
            """
            SELECT h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            """,
            (slug, event_date),
        ).fetchone()["fact_json"]
    )
    by_outcome = {row["outcome"]: row for row in latest_payload["distribution"]}
    assert by_outcome["30\u00b0C"]["q_bayes_posterior"] == 0.0
    assert by_outcome["30\u00b0C"]["q_posterior_observed_constraints_hard_zero"] is True
    assert by_outcome["31\u00b0C"]["q_bayes_posterior"] == 1.0
    assert by_outcome["31\u00b0C"]["hard_source_posterior_override_final_bucket_lock"] is True

    decisions = conn.execute(
        "SELECT action, outcome, details_json FROM auto_trade_decisions ORDER BY id"
    ).fetchall()
    assert decisions[0][0:2] == ("entry", "31\u00b0C")
    entry_details = json.loads(decisions[0][2])
    assert entry_details["decision_basis"] == "final_bucket_lock"
    assert entry_details["hard_source_final_bucket_lock"] is True
    assert entry_details["posterior_override_q"] == 1.0
    assert decisions[1][0:2] == ("exit", "30\u00b0C")
    exit_details = json.loads(decisions[1][2])
    assert exit_details["decision_basis"] == "final_bucket_lock"
    assert exit_details["hard_source_final_bucket_lock"] is True


def test_hard_source_expected_entry_override_keeps_ssot_target_when_context_disagrees():
    details = {
        "hard_source_direct": True,
        "hard_source_posterior_override": True,
        "forced_expected_target": True,
        "side": "YES",
        "outcome": "32\u00b0C",
        "ssot_target_bucket": "32\u00b0C",
        "target_bucket": "33\u00b0C",
        "posterior_settlement_target_bucket": "33\u00b0C",
        "posterior_settlement_entry_allowed": False,
        "posterior_settlement_entry_block_reason": "yes_candidate_not_target_bucket",
        "posterior_settlement_size_scale": 0.0,
        "entry_usable_edge_size_scale": 0.0,
        "entry_usable_edge_na_reason": "yes_candidate_not_target_bucket",
        "q_bayes_posterior": 1.0,
    }

    out = swm._apply_hard_source_direct_expected_entry_override(
        details,
        outcome="32\u00b0C",
        side="YES",
    )

    assert out["target_bucket"] == "32\u00b0C"
    assert out["posterior_settlement_target_bucket"] == "32\u00b0C"
    assert out["posterior_settlement_entry_allowed"] is True
    assert out["posterior_settlement_entry_block_reason"] == ""
    assert out["posterior_settlement_size_scale"] == 1.0
    assert out["entry_usable_edge_size_scale"] == 1.0
    assert out["entry_usable_edge_na_reason"] == ""
    assert out["hard_source_direct_target_entry_override"] is True


def test_hard_source_expected_entry_override_uses_ssot_target_without_transient_markers():
    details = {
        "hard_source_direct": True,
        "hard_source_posterior_override": True,
        "side": "YES",
        "outcome": "33\u00b0C",
        "ssot_target_bucket": "33\u00b0C",
        "target_bucket": "33\u00b0C",
        "posterior_settlement_target_bucket": "33\u00b0C",
        "posterior_settlement_entry_allowed": False,
        "posterior_settlement_entry_block_reason": "target_realization_reliability_below_min",
        "posterior_settlement_size_scale": 0.0,
        "entry_usable_edge_size_scale": 0.0,
        "entry_usable_edge_na_reason": "target_realization_reliability_below_min",
        "q_bayes_posterior": 1.0,
        "ask": 0.42,
    }

    out = swm._apply_hard_source_direct_expected_entry_override(details)
    audit = swm._build_entry_usable_edge_audit(out)

    assert out["posterior_settlement_entry_allowed"] is True
    assert out["posterior_settlement_entry_block_reason"] == ""
    assert out["posterior_settlement_size_scale"] == 1.0
    assert out["entry_usable_edge_size_scale"] == 1.0
    assert out["hard_source_direct_target_entry_override"] is True
    assert swm._entry_order_policy_block_reason(details) == ""
    assert audit["entry_usable_edge_size_scale"] == 1.0
    assert audit["hard_source_direct_entry_usable_edge_override"] is True

    non_target = dict(details)
    non_target["outcome"] = "32\u00b0C"
    blocked = swm._apply_hard_source_direct_expected_entry_override(non_target)
    assert blocked.get("hard_source_direct_target_entry_override") is not True
    assert blocked["posterior_settlement_entry_allowed"] is False


def test_temperature_path_trade_follow_gate_blocks_non_hard_source_yes_targets():
    payload = {
        "temperature_path_distribution": {
            "trade_follow": {
                "allowed": False,
                "reason": "blocked_path_reliability_low",
            }
        },
        "entry_expected_targets": [
            {
                "outcome": "25\u00b0C or below",
                "side": "YES",
                "strategy": "lcb_global",
                "entry_reasons": ["target_realization_order_intent"],
            },
            {
                "outcome": "26\u00b0C",
                "side": "YES",
                "strategy": "lcb_global",
                "entry_reasons": ["target_realization_order_intent"],
            },
            {
                "outcome": "30\u00b0C",
                "side": "NO",
                "strategy": "lcb_global",
                "entry_reasons": ["strongly_excluded_no"],
            },
            {
                "outcome": "25\u00b0C or below",
                "side": "YES",
                "strategy": "lcb_global",
                "hard_source_direct": True,
                "hard_source_posterior_override": True,
                "entry_reasons": ["hard_source_direct"],
            },
        ],
    }

    changed = swm._apply_temperature_path_trade_follow_gate_to_targets(payload)

    assert changed == 2
    first, second, no_leg, hard_source = payload["entry_expected_targets"]
    for target in (first, second):
        assert target["posterior_settlement_entry_allowed"] is False
        assert (
            target["posterior_settlement_entry_block_reason"]
            == swm.TEMPERATURE_PATH_TRADE_FOLLOW_BLOCK_REASON
        )
        assert target["entry_usable_edge_size_scale"] == 0.0
        assert target["entry_usable_edge_na_reason"] == swm.TEMPERATURE_PATH_TRADE_FOLLOW_BLOCK_REASON
        assert target["target_realization_trade_follow_allowed"] is False
        assert target["target_realization_trade_follow_reason"] == "blocked_path_reliability_low"
        assert target["temperature_path_trade_follow_allowed"] is False
        assert target["temperature_path_trade_follow_block_yes"] is True
        assert target["temperature_path_trade_follow_reason"] == "blocked_path_reliability_low"
        assert (
            target["temperature_path_trade_follow_gate_source"]
            == "temperature_path_distribution.trade_follow"
        )
    assert "posterior_settlement_entry_allowed" not in no_leg
    assert "posterior_settlement_entry_allowed" not in hard_source


def test_hard_source_direct_suppresses_recent_unchanged_decision(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")

    first = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=3600,
        enqueue_job=False,
    )
    second = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=3600,
        enqueue_job=False,
    )

    assert first["status"] == "recorded"
    assert second["status"] == "unchanged"
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0]
    assert count == 2
    conn.close()


def test_poll_once_refreshes_wu_twc_before_decision_when_due(tmp_path, monkeypatch):
    calls = []

    def fake_awc(*args, **kwargs):
        calls.append("awc")
        return {"ok": True, "status": "updated"}

    def fake_wu(*args, **kwargs):
        calls.append("wu")
        return {"ok": True, "status": "updated"}

    def fake_decision(*args, **kwargs):
        calls.append("decision")
        return {"ok": True, "status": "recorded", "job_id": None}

    monkeypatch.setattr(poller, "_refresh_awc_fact", fake_awc)
    monkeypatch.setattr(poller, "_refresh_wu_twc_fact", fake_wu)
    monkeypatch.setattr(poller, "_refresh_stamp_due", lambda *args, **kwargs: True)
    monkeypatch.setattr(poller, "record_hard_source_direct_decisions", fake_decision)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_ALLOW_WU_TWC_FALLBACK", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "1")

    result = poller.poll_once(tmp_path / "archive.db", event_date="2026-06-19")

    assert result["ok"] is True
    assert calls == ["awc", "wu", "decision"]


def test_refresh_awc_fact_is_read_only_when_reference_fetch_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    before = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]

    def raise_fetch(*args, **kwargs):
        raise RuntimeError("AWC unavailable")

    monkeypatch.setattr(poller.swm, "fetch_zuuu_metar_raw_awc_features", raise_fetch)

    result = poller._refresh_awc_fact(
        db_path,
        event_date=event_date,
        hours=48,
        heartbeat_sec=1,
    )

    assert result["ok"] is False
    assert result["status"] == "awc_reference_fetch_failed"
    assert result["writes_facts"] is False
    assert result["writes_posterior"] is False
    assert "AWC unavailable" in result["error"]
    trace = result["source_acquisition_trace"]
    assert trace["read_only"] is True
    assert trace["refresh_source_family"] == "awc"
    assert trace["source_latency_trace"][0]["source_id"] == "zuuu_metar_raw_awc"
    assert "source_not_ok" in trace["source_latency_trace"][0]["quality_flags"]
    after = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    assert after == before


def test_refresh_awc_fact_success_is_reference_only(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    conn = sqlite3.connect(db_path)
    before = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    before_latest = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, event_date),
    ).fetchone()[0]
    conn.close()

    monkeypatch.setattr(
        poller.swm,
        "fetch_zuuu_metar_raw_awc_features",
        lambda *args, **kwargs: {
            "zuuu_metar_raw_awc_latest_at": "2026-06-19T04:00:00Z",
            "zuuu_metar_raw_awc_today_max_at": "2026-06-19T04:00:00Z",
            "zuuu_metar_raw_awc_today_max_c": 33.0,
            "zuuu_metar_raw_awc_latest_temp_c": 33.0,
            "zuuu_metar_raw_awc_fetched_at": "2026-06-19T04:00:01Z",
        },
    )

    result = poller._refresh_awc_fact(db_path, event_date=event_date, hours=48, heartbeat_sec=1)

    assert result["ok"] is True
    assert result["status"] == "reference_observed"
    assert result["writes_facts"] is False
    assert result["writes_posterior"] is False
    conn = sqlite3.connect(db_path)
    after = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    after_latest = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, event_date),
    ).fetchone()[0]
    conn.close()
    assert after == before
    assert after_latest == before_latest


def test_main_once_returns_structured_failure_without_traceback(monkeypatch, capsys):
    def raise_poll(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(poller, "poll_once", raise_poll)

    code = poller.main(["--once", "--event-date", "2026-06-19"])

    captured = json.loads(capsys.readouterr().out)
    assert code == 1
    assert captured["status"] == "poll_once_failed"
    assert captured["error"] == "RuntimeError: boom"


def test_poll_once_defers_wu_twc_until_after_decision_when_awc_observation_ready(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    calls = []
    decision_counts_before_wu = []

    def fake_awc(*args, **kwargs):
        calls.append("awc")
        return {"ok": True, "status": "updated"}

    def fake_wu(db_path_arg, *args, **kwargs):
        calls.append("wu")
        conn = sqlite3.connect(db_path_arg)
        decision_counts_before_wu.append(
            conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0]
        )
        conn.close()
        return {"ok": True, "status": "updated"}

    monkeypatch.setattr(poller, "_refresh_awc_fact", fake_awc)
    monkeypatch.setattr(poller, "_refresh_wu_twc_fact", fake_wu)
    monkeypatch.setattr(poller, "_refresh_stamp_due", lambda *args, **kwargs: True)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_ALLOW_WU_TWC_FALLBACK", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_WU_TWC_INLINE_REFRESH", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_RUN_WORKER_ONCE", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_HEARTBEAT_SEC", "0")

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert result["ok"] is True
    assert result["decision"]["status"] == "recorded"
    assert calls == ["awc", "wu"]
    assert len(decision_counts_before_wu) == 1
    assert decision_counts_before_wu[0] >= 2


def test_poll_once_backgrounds_wu_twc_after_decision_in_loop_mode(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    calls = []

    def fake_awc(*args, **kwargs):
        calls.append("awc")
        return {"ok": True, "status": "updated"}

    def fail_sync_wu(*args, **kwargs):
        raise AssertionError("wu_twc_refresh_should_not_block_core_loop")

    def fake_spawn(db_arg, *args, **kwargs):
        calls.append("wu_background")
        assert Path(db_arg) == db_path
        assert kwargs["event_date"] == event_date
        assert kwargs["slug"] == slug
        return {"ok": True, "status": "background_started", "pid": 12345}

    monkeypatch.setattr(poller, "_refresh_awc_fact", fake_awc)
    monkeypatch.setattr(poller, "_refresh_wu_twc_fact", fail_sync_wu)
    monkeypatch.setattr(poller, "_spawn_wu_twc_refresh", fake_spawn)
    monkeypatch.setattr(poller, "_refresh_stamp_due", lambda *args, **kwargs: True)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_ALLOW_WU_TWC_FALLBACK", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_HEARTBEAT_SEC", "0")

    result = poller.poll_once(db_path, event_date=event_date, slug=slug, background_wu_twc=True)

    assert result["ok"] is True
    assert result["decision"]["status"] == "recorded"
    assert result["wu_twc"]["status"] == "background_started"
    assert calls == ["awc", "wu_background"]


def test_wu_twc_background_refresh_is_opt_in_for_loop(monkeypatch):
    monkeypatch.delenv("PM_HARD_SOURCE_WU_TWC_BACKGROUND_REFRESH", raising=False)
    assert poller._background_wu_twc_refresh_enabled() is False

    monkeypatch.setenv("PM_HARD_SOURCE_WU_TWC_BACKGROUND_REFRESH", "1")
    assert poller._background_wu_twc_refresh_enabled() is True

    monkeypatch.setenv("PM_HARD_SOURCE_WU_TWC_BACKGROUND_REFRESH", "0")
    assert poller._background_wu_twc_refresh_enabled() is False


def test_post_decision_wu_twc_inline_refresh_is_opt_in(monkeypatch):
    monkeypatch.delenv("PM_HARD_SOURCE_WU_TWC_INLINE_REFRESH", raising=False)
    assert poller._inline_wu_twc_refresh_after_decision_enabled() is False

    monkeypatch.setenv("PM_HARD_SOURCE_WU_TWC_INLINE_REFRESH", "1")
    assert poller._inline_wu_twc_refresh_after_decision_enabled() is True

    monkeypatch.setenv("PM_HARD_SOURCE_WU_TWC_INLINE_REFRESH", "0")
    assert poller._inline_wu_twc_refresh_after_decision_enabled() is False


def test_auto_trade_worker_claim_can_exclude_direct_target_jobs(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    now = "2026-06-30T01:00:00+00:00"
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO auto_trade_jobs(
                run_date, source, state, priority, payload_json,
                lease_until, retry_count, max_retry, next_retry_at, last_error,
                started_at, ended_at, run_id, created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?, '', 0, 1, '', '', '', '', NULL, ?, ?)
            """,
            (
                "2026-06-30",
                "posterior_target_direct",
                20000,
                '{"posterior_target_direct":{"target_bucket":"30°C or higher"}}',
                now,
                now,
            ),
        )
        direct_id = int(cur.lastrowid or 0)
        cur.execute(
            """
            INSERT INTO auto_trade_jobs(
                run_date, source, state, priority, payload_json,
                lease_until, retry_count, max_retry, next_retry_at, last_error,
                started_at, ended_at, run_id, created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?, '', 0, 1, '', '', '', '', NULL, ?, ?)
            """,
            (
                "2026-06-30",
                "entry_1m_strategy_batch",
                100,
                '{"entry_expected_targets":[]}',
                now,
                now,
            ),
        )
        regular_id = int(cur.lastrowid or 0)

    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "DATA_DIR", db_path.parent)
    monkeypatch.setenv("PM_AUTO_TRADE_JOB_CLAIM_EXCLUDE_DIRECT_TARGET", "1")
    monkeypatch.setenv("PM_AUTO_TRADE_JOB_SKIP_CLEANUP_ON_CLAIM", "1")

    picked = swm._auto_trade_job_claim_one()

    assert picked is not None
    assert int(picked["id"]) == regular_id
    with sqlite3.connect(db_path) as conn:
        states = {
            int(row[0]): row[1]
            for row in conn.execute("SELECT id, state FROM auto_trade_jobs")
        }
    assert states[direct_id] == "queued"
    assert states[regular_id] == "running"


def test_auto_trade_worker_claim_requested_job_id_bypasses_queue_order(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    now = "2026-06-30T01:00:00+00:00"
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO auto_trade_jobs(
                run_date, source, state, priority, payload_json,
                lease_until, retry_count, max_retry, next_retry_at, last_error,
                started_at, ended_at, run_id, created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?, '', 0, 1, '', '', '', '', NULL, ?, ?)
            """,
            (
                "2026-06-30",
                "entry_1m_strategy_batch",
                30000,
                '{"entry_expected_targets":[]}',
                now,
                now,
            ),
        )
        regular_id = int(cur.lastrowid or 0)
        cur.execute(
            """
            INSERT INTO auto_trade_jobs(
                run_date, source, state, priority, payload_json,
                lease_until, retry_count, max_retry, next_retry_at, last_error,
                started_at, ended_at, run_id, created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?, '', 0, 1, ?, '', '', '', NULL, ?, ?)
            """,
            (
                "2026-06-30",
                "posterior_target_direct",
                15000,
                '{"posterior_target_direct":{"target_bucket":"30°C or higher"}}',
                "2099-01-01T00:00:00.000+00:00",
                now,
                now,
            ),
        )
        direct_id = int(cur.lastrowid or 0)

    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "DATA_DIR", db_path.parent)
    monkeypatch.setenv("PM_AUTO_TRADE_JOB_SKIP_CLEANUP_ON_CLAIM", "1")

    picked = swm._auto_trade_job_claim_one(requested_job_id=direct_id)

    assert picked is not None
    assert int(picked["id"]) == direct_id
    with sqlite3.connect(db_path) as conn:
        states = {
            int(row[0]): row[1]
            for row in conn.execute("SELECT id, state FROM auto_trade_jobs")
        }
    assert states[regular_id] == "queued"
    assert states[direct_id] == "running"


def test_poll_once_drains_claimable_direct_retry_job_after_awc_watch_refresh(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug = "highest-temperature-in-chengdu-on-july-1-2026"
    event_date = "2026-07-01"
    past = _utc_fixture_iso(-60)
    payload = {
        "include_event_keys": [{"slug": slug, "event_date": event_date}],
        "posterior_target_direct": {
            "target_bucket": "33\u00b0C",
            "old_exit_cleanup_triggered": True,
        },
    }
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO auto_trade_jobs(
                run_date, source, state, priority, payload_json,
                lease_until, retry_count, max_retry, next_retry_at, last_error,
                started_at, ended_at, run_id, created_at, updated_at
            ) VALUES (?, ?, 'retrying', ?, ?, '', 1, 1, ?, 'worker_terminated_sigterm', ?, ?, NULL, ?, ?)
            """,
            (
                event_date,
                "posterior_target_direct",
                15000,
                json.dumps(payload),
                past,
                past,
                past,
                past,
                past,
            ),
        )
        job_id = int(cur.lastrowid or 0)

    captured = {}
    events = []
    awc_target_at = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)

    def fake_awc_refresh(*args, **kwargs):
        events.append("awc")
        return {
            "ok": True,
            "status": "skipped",
            "reason": "same_awc_latest_at",
            "awc_latest_at": awc_target_at.isoformat(),
        }

    def fake_worker_once(*, db_path=None, requested_job_id=None):
        events.append("worker")
        captured["db_path"] = Path(db_path)
        captured["requested_job_id"] = int(requested_job_id or 0)
        return {"ok": True, "picked": True, "job_id": int(requested_job_id or 0)}

    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_AWC", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DRAIN_CLAIMABLE_JOB", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DRAIN_BEFORE_AWC", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_RETURN_AFTER_PRE_AWC_DRAIN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DRAIN_BEFORE_DECISION", "1")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_ENABLED", "0")
    monkeypatch.setattr(poller, "_awc_release_watch_target_at", lambda: awc_target_at)
    monkeypatch.setattr(poller, "_refresh_awc_fact", fake_awc_refresh)
    monkeypatch.setattr(
        poller,
        "_load_latest_fact_payload",
        lambda *args, **kwargs: {"slug": slug, "event_date": event_date},
    )
    monkeypatch.setattr(
        poller,
        "_decision_window_state",
        lambda *args, **kwargs: {"allowed": False, "disabled": False},
    )
    monkeypatch.setattr(
        poller,
        "record_hard_source_direct_decisions",
        lambda *args, **kwargs: {"ok": True, "status": "unchanged"},
    )
    monkeypatch.setattr(poller, "_run_worker_once_inline", fake_worker_once)

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert events == ["awc", "worker"]
    assert captured == {"db_path": db_path, "requested_job_id": job_id}
    assert result["worker"]["ok"] is True
    assert result["worker"]["drain_claimable_direct_job"] is True
    assert result["worker"]["drain_claimable_direct_job_id"] == job_id
    assert result["worker"]["drain_claimable_direct_stage"] == "before_decision"
    assert result["decision"]["status"] == "unchanged"


def test_poll_once_holds_non_awc_work_until_awc_release_target(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-july-1-2026"
    event_date = "2026-07-01"
    target_at = datetime(2026, 6, 30, 23, 0, tzinfo=timezone.utc)
    events = []

    def fake_awc_refresh(*args, **kwargs):
        events.append("awc")
        return {
            "ok": True,
            "status": "skipped",
            "reason": "same_awc_latest_at",
            "event_date": event_date,
            "awc_latest_at": "2026-06-30T22:00:00+00:00",
        }

    def fail_non_awc_work(*args, **kwargs):
        pytest.fail("non-AWC work must wait until the release target hour is fetched")

    monkeypatch.setenv("PM_HARD_SOURCE_LIVE_SSOT_WRITER_LOCK_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_AWC", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DRAIN_CLAIMABLE_JOB", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DRAIN_BEFORE_AWC", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_RETURN_AFTER_PRE_AWC_DRAIN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_ACQUIRE_HOLD_NON_AWC_UNTIL_TARGET", "1")
    monkeypatch.setattr(poller, "_awc_release_watch_target_at", lambda: target_at)
    monkeypatch.setattr(poller, "_refresh_awc_fact", fake_awc_refresh)
    monkeypatch.setattr(poller, "_load_latest_fact_payload", fail_non_awc_work)
    monkeypatch.setattr(poller, "record_hard_source_direct_decisions", fail_non_awc_work)
    monkeypatch.setattr(poller, "_run_worker_once_inline", fail_non_awc_work)

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert events == ["awc"]
    assert result["decision"]["reason"] == "awc_acquire_window_waiting_for_target_hour"
    assert result["decision"]["awc_release_watch_target_at"] == target_at.isoformat()
    assert result["decision"]["awc_latest_at"] == "2026-06-30T22:00:00+00:00"
    assert result["worker"] == {}


def test_poll_once_holds_non_awc_work_until_awc_event_end_final_report(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-july-5-2026"
    event_date = "2026-07-05"
    target_at = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    events = []

    def fake_load(*args, **kwargs):
        events.append("load")
        return {
            "slug": slug,
            "event_date": event_date,
            "event_end_utc": target_at.isoformat(),
            "weather_likelihood_features": {
                "zuuu_metar_raw_awc_today_max_c": 37.0,
                "zuuu_metar_raw_awc_final_daily_max": False,
            },
        }

    def fake_awc_refresh(*args, **kwargs):
        events.append("awc")
        return {
            "ok": True,
            "status": "skipped",
            "reason": "same_awc_latest_at",
            "event_date": event_date,
            "awc_latest_at": "2026-07-05T10:00:00+00:00",
            "awc_final_daily_max": False,
            "awc_final_daily_max_state": "awc_latest_before_event_end",
        }

    def fail_non_awc_work(*args, **kwargs):
        pytest.fail("non-AWC work must wait for AWC event-end final report")

    monkeypatch.setenv("PM_HARD_SOURCE_LIVE_SSOT_WRITER_LOCK_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_RELEASE_WATCH_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_FINAL_WATCH_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_AWC_FINAL_WATCH_DURATION_SEC", "900")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_AWC", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DRAIN_CLAIMABLE_JOB", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DRAIN_BEFORE_AWC", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_RETURN_AFTER_PRE_AWC_DRAIN", "1")
    monkeypatch.setattr(poller, "_load_latest_fact_payload", fake_load)
    monkeypatch.setattr(
        poller,
        "_awc_event_end_final_watch_context",
        lambda payload: {
            "active": True,
            "reason": "awc_final_daily_max_event_end_watch",
            "target_at": target_at,
            "target_at_utc": target_at.isoformat(),
            "event_end_utc": target_at.isoformat(),
            "poll_sec": 1.0,
        },
    )
    monkeypatch.setattr(poller, "_refresh_awc_fact", fake_awc_refresh)
    monkeypatch.setattr(poller, "record_hard_source_direct_decisions", fail_non_awc_work)
    monkeypatch.setattr(poller, "_run_worker_once_inline", fail_non_awc_work)

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert events == ["load", "awc"]
    assert result["decision"]["reason"] == "awc_final_daily_max_waiting_for_event_end_report"
    assert result["decision"]["awc_release_watch_target_at"] == target_at.isoformat()
    assert result["decision"]["awc_watch_context"]["reason"] == "awc_final_daily_max_event_end_watch"
    assert result["decision"]["awc_latest_at"] == "2026-07-05T10:00:00+00:00"
    assert result["worker"] == {}


def test_poll_once_does_not_run_or_drain_retired_posterior_target_jobs(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug = "highest-temperature-in-chengdu-on-july-1-2026"
    event_date = "2026-07-01"
    past = _utc_fixture_iso(-60)
    payload = {
        "include_event_keys": [{"slug": slug, "event_date": event_date}],
        "posterior_target_direct": {
            "target_bucket": "33\u00b0C",
            "old_exit_cleanup_triggered": True,
        },
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO auto_trade_jobs(
                run_date, source, state, priority, payload_json,
                lease_until, retry_count, max_retry, next_retry_at, last_error,
                started_at, ended_at, run_id, created_at, updated_at
            ) VALUES (?, ?, 'retrying', ?, ?, '', 1, 1, ?, 'worker_terminated_sigterm', ?, ?, NULL, ?, ?)
            """,
            (
                event_date,
                "posterior_target_direct",
                15000,
                json.dumps(payload),
                past,
                past,
                past,
                past,
                past,
            ),
        )

    captured = {}

    def fake_worker_once(*, db_path=None, requested_job_id=None):
        captured["db_path"] = Path(db_path)
        captured["requested_job_id"] = int(requested_job_id or 0)
        return {"ok": True, "picked": True, "job_id": int(requested_job_id or 0)}

    def fake_posterior_record(*args, **kwargs):
        return {"ok": True, "status": "recorded", "job_id": 222}

    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_AWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DRAIN_CLAIMABLE_JOB", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DRAIN_BEFORE_AWC", "0")
    monkeypatch.delenv("PM_HARD_SOURCE_DIRECT_DRAIN_BEFORE_DECISION", raising=False)
    monkeypatch.setattr(
        poller,
        "_load_latest_fact_payload",
        lambda *args, **kwargs: {"slug": slug, "event_date": event_date},
    )
    monkeypatch.setattr(
        poller,
        "_decision_window_state",
        lambda *args, **kwargs: {"allowed": False, "disabled": False},
    )
    monkeypatch.setattr(
        poller,
        "record_posterior_target_direct_decisions",
        fake_posterior_record,
    )
    monkeypatch.setattr(
        poller,
        "record_hard_source_direct_decisions",
        lambda *args, **kwargs: {"ok": True, "status": "unchanged"},
    )
    monkeypatch.setattr(poller, "_run_worker_once_inline", fake_worker_once)

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert captured == {}
    assert result["posterior_target_fast_precheck"]["status"] == "retired"
    assert result["posterior_target"]["status"] == "retired"
    assert result["posterior_target_fast_worker"] == {}
    assert result["worker"] == {}


def test_recent_posterior_old_exit_cleanup_ignores_stuck_job(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug = "highest-temperature-in-chengdu-on-july-1-2026"
    event_date = "2026-07-01"
    now = _utc_fixture_iso()
    payload = {
        "include_event_keys": [{"slug": slug, "event_date": event_date}],
        "posterior_target_direct": {
            "target_bucket": "33\u00b0C",
            "old_exit_cleanup_triggered": True,
        },
    }
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO auto_trade_jobs(
                run_date, source, state, priority, payload_json,
                lease_until, retry_count, max_retry, next_retry_at, last_error,
                started_at, ended_at, run_id, created_at, updated_at
            ) VALUES (?, ?, 'stuck', 15000, ?, '', 1, 1, '', 'worker_stuck_timeout', ?, ?, NULL, ?, ?)
            """,
            (event_date, "posterior_target_direct", json.dumps(payload), now, now, now, now),
        )
        assert (
            poller._recent_posterior_target_old_exit_cleanup_job_exists(
                conn,
                slug=slug,
                event_date=event_date,
                target_bucket="33\u00b0C",
                within_sec=600,
            )
            is False
        )
        conn.execute("UPDATE auto_trade_jobs SET state='retrying'")
        assert (
            poller._recent_posterior_target_old_exit_cleanup_job_exists(
                conn,
                slug=slug,
                event_date=event_date,
                target_bucket="33\u00b0C",
                within_sec=600,
            )
            is True
        )


def test_recent_posterior_old_exit_cleanup_ignores_failed_exit_decisions(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug = "highest-temperature-in-chengdu-on-july-1-2026"
    event_date = "2026-07-01"
    target_bucket = "33\u00b0C"
    now = _utc_fixture_iso()
    payload = {
        "include_event_keys": [{"slug": slug, "event_date": event_date}],
        "posterior_target_direct": {
            "target_bucket": target_bucket,
            "old_exit_cleanup_triggered": True,
        },
    }
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            INSERT INTO auto_trade_jobs(
                run_date, source, state, priority, payload_json,
                lease_until, retry_count, max_retry, next_retry_at, last_error,
                started_at, ended_at, run_id, created_at, updated_at
            ) VALUES (?, ?, 'success', 15000, ?, '', 1, 1, '', '', ?, ?, NULL, ?, ?)
            """,
            (event_date, "posterior_target_direct", json.dumps(payload), now, now, now, now),
        )
        job_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
                run_date, slug, event_date, title, outcome, side, action,
                decided_state, will_order, reason_codes_json, reasons_json, details_json, created_at
            ) VALUES (?, ?, ?, '', '34\u00b0C', 'NO', 'exit',
                      'exit_failed', 1, ?, '[]', ?, ?)
            """,
            (
                event_date,
                slug,
                event_date,
                json.dumps(["place_order_failed"]),
                json.dumps(
                    {
                        "posterior_target_job_id": job_id,
                        "target_bucket": target_bucket,
                        "exit_skip_reason": "exit_submit_failed",
                    }
                ),
                now,
            ),
        )
        assert (
            poller._recent_posterior_target_old_exit_cleanup_job_exists(
                conn,
                slug=slug,
                event_date=event_date,
                target_bucket=target_bucket,
                within_sec=600,
            )
            is False
        )


def test_poll_once_skips_post_decision_wu_twc_refresh_by_default(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)

    def fake_awc(*args, **kwargs):
        return {"ok": True, "status": "skipped", "reason": "not_due"}

    def fail_sync_wu(*args, **kwargs):
        raise AssertionError("post_decision_wu_twc_refresh_should_be_opt_in")

    def fail_spawn_wu(*args, **kwargs):
        raise AssertionError("post_decision_wu_twc_background_should_be_opt_in")

    monkeypatch.setattr(poller, "_refresh_awc_fact", fake_awc)
    monkeypatch.setattr(poller, "_refresh_wu_twc_fact", fail_sync_wu)
    monkeypatch.setattr(poller, "_spawn_wu_twc_refresh", fail_spawn_wu)
    monkeypatch.setattr(poller, "_refresh_stamp_due", lambda *args, **kwargs: True)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_ALLOW_WU_TWC_FALLBACK", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE", "0")
    monkeypatch.setenv("PM_POSTERIOR_TARGET_DIRECT_RUN_WORKER_ONCE", "0")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_HEARTBEAT_SEC", "0")
    monkeypatch.delenv("PM_HARD_SOURCE_WU_TWC_BACKGROUND_REFRESH", raising=False)
    monkeypatch.delenv("PM_HARD_SOURCE_WU_TWC_INLINE_REFRESH", raising=False)

    result = poller.poll_once(db_path, event_date=event_date, slug=slug)

    assert result["ok"] is True
    assert result["wu_twc"]["status"] == "skipped"
    assert result["wu_twc"]["reason"] == "post_decision_wu_twc_refresh_opt_in"


def test_hard_source_direct_outside_decision_window_does_not_enqueue(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", event_date)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "13.9")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=True,
    )

    assert result["ok"] is True
    assert result["status"] == "outside_decision_window"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0] == 0


def test_hard_source_direct_window_follows_wu_forecast_peak(monkeypatch):
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", "2026-06-18")
    payload = {
        "event_date": "2026-06-18",
        "event_tz": "Asia/Shanghai",
        "weather_likelihood_features": {
            "wu_forecast_peak_at": "2026-06-18T07:00:00+00:00",
        },
    }

    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "13.99")
    before = poller._decision_window_state(payload, event_date="2026-06-18")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "14.0")
    start = poller._decision_window_state(payload, event_date="2026-06-18")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "16.5")
    end = poller._decision_window_state(payload, event_date="2026-06-18")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "16.51")
    after = poller._decision_window_state(payload, event_date="2026-06-18")

    assert before["allowed"] is False
    assert start["allowed"] is True
    assert end["allowed"] is True
    assert after["allowed"] is False
    assert start["mode"] == "wu_forecast"
    assert start["peak_hour_local"] == 15.0
    assert start["peak_hour_source"] == "wu_forecast_peak_at"
    assert start["start_local_hour"] == 14.0
    assert start["end_local_hour"] == 16.5


def test_hard_source_direct_window_prefers_peak_hour_distribution(monkeypatch):
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", "2026-06-18")
    payload = {
        "event_date": "2026-06-18",
        "event_tz": "Asia/Shanghai",
        "weather_likelihood_features": {
            "wu_forecast_peak_at": "2026-06-18T07:00:00+00:00",
            "expected_peak_hour_distribution": [
                {"hour_local": 15.0, "prob": 0.30, "valid_time_local": "2026-06-18T15:00:00+08:00"},
                {"hour_local": 16.0, "prob": 0.40, "valid_time_local": "2026-06-18T16:00:00+08:00"},
                {"hour_local": 17.0, "prob": 0.30, "valid_time_local": "2026-06-18T17:00:00+08:00"},
            ],
            "expected_peak_window_start_hour_local": 15.0,
            "expected_peak_window_end_hour_local": 17.0,
            "expected_peak_hour_source": "weather_com_forecast_hourly_peak_plateau:hourly_near_peak_plateau",
        },
    }

    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "18.49")
    inside = poller._decision_window_state(payload, event_date="2026-06-18")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "18.51")
    outside = poller._decision_window_state(payload, event_date="2026-06-18")

    assert inside["allowed"] is True
    assert outside["allowed"] is False
    assert inside["peak_hour_local"] == 16.0
    assert inside["peak_hour_source"] == "weather_com_forecast_hourly_peak_plateau:hourly_near_peak_plateau"
    assert inside["peak_window_start_hour_local"] == 15.0
    assert inside["peak_window_end_hour_local"] == 17.0
    assert inside["start_local_hour"] == 14.0
    assert inside["end_local_hour"] == 18.5


def test_peak_plateau_copy_rejects_hourly_max_far_from_daily_peak():
    source_features = {}
    poller._copy_peak_plateau_features(
        source_features,
        prefix="weather_com_forecast",
        metrics={
            "source": "weather_com_forecast_hourly_peak",
            "peak_plateau_max_temp_c": 26.0,
            "expected_peak_hour_distribution": [
                {"hour_local": 21.0, "prob": 0.5, "temperature_c": 26.0},
                {"hour_local": 22.0, "prob": 0.5, "temperature_c": 26.0},
            ],
            "expected_peak_window_start_hour_local": 21.0,
            "expected_peak_window_end_hour_local": 22.0,
        },
        canonical=True,
        reference_peak_c=34.0,
    )

    assert source_features["_clear_expected_peak_hour_distribution"] is True
    assert source_features["expected_peak_hour_distribution"] == []
    assert source_features["weather_com_forecast_peak_hour_distribution_na_reason"] == "hourly_plateau_max_far_from_daily_peak"


def test_hard_source_direct_peak_window_opens_one_hour_before_peak(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    slug, event_date = _insert_fact(db_path)
    monkeypatch.setenv("PM_AUTO_TRADE_DRY_RUN", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED", "1")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", event_date)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "15.0")

    result = poller.record_hard_source_direct_decisions(
        db_path,
        event_date=event_date,
        slug=slug,
        emit_unchanged_sec=0,
        enqueue_job=False,
    )

    assert result["status"] == "recorded"
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 2


def test_peak_decision_window_reports_wu_forecast_bounds(monkeypatch):
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", "2026-06-18")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "15.25")
    state = poller._decision_window_state(
        {
            "event_date": "2026-06-18",
            "event_tz": "Asia/Shanghai",
            "station_id": "ZUUU",
            "weather_likelihood_features": {"wu_forecast_peak_at": "2026-06-18T08:00:00+00:00"},
        },
        event_date="2026-06-18",
    )

    assert state["allowed"] is True
    assert state["mode"] == "wu_forecast"
    assert state["start_local_hour"] == 15.0
    assert state["end_local_hour"] == 17.5
    assert state["peak_hour_local"] == 16.0
    assert state["peak_hour_source"] == "wu_forecast_peak_at"


def test_peak_decision_window_blocks_without_wu_forecast_time(monkeypatch):
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", "2026-06-18")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "15.25")
    state = poller._decision_window_state(
        {
            "event_date": "2026-06-18",
            "event_tz": "Asia/Shanghai",
            "station_id": "ZUUU",
            "weather_likelihood_features": {"expected_peak_hour_local": 16.0},
        },
        event_date="2026-06-18",
    )

    assert state["allowed"] is False
    assert state["reason"] == "wu_forecast_peak_time_missing"
    assert state["peak_hour_local"] is None
    assert state["start_local_hour"] is None
    assert state["end_local_hour"] is None


def test_poll_once_uses_one_second_wu_twc_due_inside_wu_forecast_window(tmp_path, monkeypatch):
    intervals = []
    db_path = tmp_path / "archive.db"
    _create_schema(db_path)
    _insert_fact(db_path)

    monkeypatch.setattr(poller, "_refresh_awc_fact", lambda *args, **kwargs: {"ok": True, "status": "skipped"})
    monkeypatch.setattr(poller, "_refresh_wu_twc_fact", lambda *args, **kwargs: {"ok": True, "status": "updated"})
    monkeypatch.setattr(poller, "record_hard_source_direct_decisions", lambda *args, **kwargs: {"ok": True, "status": "recorded"})

    def fake_due(*args, **kwargs):
        intervals.append(kwargs.get("interval_sec"))
        return False

    monkeypatch.setattr(poller, "_refresh_stamp_due", fake_due)
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE", "2026-06-19")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE", "14.25")
    monkeypatch.setenv("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_ENABLED", "0")
    monkeypatch.delenv("PM_HARD_SOURCE_WU_TWC_POLL_SEC", raising=False)
    monkeypatch.delenv("PM_HARD_SOURCE_WU_TWC_PEAK_POLL_SEC", raising=False)

    result = poller.poll_once(db_path, event_date="2026-06-19")

    assert result["ok"] is True
    assert intervals == [1]
def test_forward_forecast_archive_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FORECAST_SOURCE_FORWARD_ARCHIVE_ENABLED", raising=False)

    result = poller._archive_forecast_source_forward_metrics(
        source_id="weather_com_forecast_peak",
        event_date="2026-07-12",
        station_id="ZUUU",
        metrics={"value_c": 36.0},
    )

    assert result == {"ok": True, "state": "disabled", "writes_research_files": False}
