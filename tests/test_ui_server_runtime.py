from __future__ import annotations

import inspect
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import time

import pytest
import scan_weather_market
from ui import server

ROOT = Path(__file__).resolve().parents[1]


def mark_ui_snapshot_rebuilding(cache_key: str) -> None:
    server._UI_SNAPSHOT_REBUILDING[cache_key] = {
        "cache_key": cache_key,
        "started_at": server.now_text(),
        "started_monotonic": time.monotonic(),
        "mode": "summary" if str(cache_key).startswith("summary:") else "full",
    }


def attach_ctp_distribution(payload: dict) -> dict:
    rows = []
    for row in payload.get("distribution") or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("outcome") or row.get("label") or row.get("bucket") or "").strip()
        prob = row.get("q_temperature_bucket_posterior")
        if prob is None:
            prob = row.get("q_bayes_posterior")
        if not label or prob is None:
            continue
        rows.append({"outcome": label, "probability": float(prob)})
    payload["posterior_temperature_distribution"] = {
        "version": "continuous_temperature_posterior_v1",
        "target_variable": "settlement_daily_max_c",
        "status": "ok",
        "grid": [],
        "cdf_values": [],
        "quantiles_c": {},
        "quantile_grid": [],
        "bucket_probabilities": rows,
        "source_components": {},
        "calibration": {},
        "observation_constraints": {"d0_violation_count": 0},
    }
    return payload


@pytest.fixture(autouse=True)
def restore_auto_trade_env_after_test():
    keys = (
        *server.AUTO_TRADE_CONTROL_ENV_KEYS,
        *server.AUTO_TRADE_SHARED_ENV_KEYS,
        *server.AUTO_TRADE_LAUNCHCTL_ENV_KEYS,
        "PRIVATE_KEY",
        "PM_SHARED_POLY_ENV_PATH",
        "PM_ALLOW_LAUNCHCTL_POLY_CREDENTIALS",
    )
    original = {key: os.environ.get(key) for key in keys}
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_ui_http_accept_path_is_bounded_for_refresh_bursts() -> None:
    assert server.UI_REQUEST_QUEUE_SIZE >= server.UI_MAX_CONCURRENT_REQUESTS * 4
    assert server.UI_REQUEST_PEEK_TIMEOUT_SEC <= 0.08
    assert server.UI_BUSY_ACQUIRE_TIMEOUT_SEC <= 0.5


def test_ui_snapshot_summary_attaches_chengdu_model_weight_shadow(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "chengdu_model_hit_rate_audit_latest.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-01T00:00:00+00:00",
                "recommendation_guardrails": {"current_shadow_output_available": True},
                "model_weight_shadow": {
                    "version": "model_weight_shadow_v1",
                    "applied_to_runtime": False,
                    "runtime_promotion_blockers": ["live_market_days_below_runtime_gate"],
                    "segments": [
                        {
                            "segment_key": "d0_pre_peak|season=all|basin_regime=all",
                            "sample_market_days": 24,
                            "model_weights": [{"model": "gfs_seamless", "shadow_weight": 0.34}],
                        }
                    ],
                },
                "basin_regime_feature_set": {
                    "version": "basin_regime_feature_set_v1",
                    "applied_to_runtime": False,
                    "promotion_blockers": ["basin_regime_feature_not_materialized_in_model_member_cache"],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "CHENGDU_MODEL_HIT_RATE_AUDIT_REPORT_PATH", report_path)
    with server._CHENGDU_MODEL_WEIGHT_SHADOW_CACHE_LOCK:
        server._CHENGDU_MODEL_WEIGHT_SHADOW_CACHE.clear()
        server._CHENGDU_MODEL_WEIGHT_SHADOW_CACHE.update({"data": None, "mtime_ns": None})

    slim = server._slim_fact_payload_for_ui_snapshot_summary(
        {
            "slug": "highest-temperature-in-chengdu-on-july-1-2026",
            "title": "Highest temperature in Chengdu on July 1?",
            "city": "Chengdu",
            "station_id": "ZUUU",
            "event_date": "2026-07-01",
        }
    )

    assert slim["model_weight_shadow"]["version"] == "model_weight_shadow_v1"
    assert slim["model_weight_shadow"]["read_only_fact_layer"] is True
    assert slim["model_weight_shadow"]["source"] == str(report_path)
    assert slim["basin_regime_feature_set"]["version"] == "basin_regime_feature_set_v1"
    assert slim["model_weight_shadow_guardrails"]["current_shadow_output_available"] is True


def test_ui_snapshot_summary_does_not_attach_chengdu_shadow_to_other_cities(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "chengdu_model_hit_rate_audit_latest.json"
    report_path.write_text(
        json.dumps({"model_weight_shadow": {"version": "model_weight_shadow_v1"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "CHENGDU_MODEL_HIT_RATE_AUDIT_REPORT_PATH", report_path)
    with server._CHENGDU_MODEL_WEIGHT_SHADOW_CACHE_LOCK:
        server._CHENGDU_MODEL_WEIGHT_SHADOW_CACHE.clear()
        server._CHENGDU_MODEL_WEIGHT_SHADOW_CACHE.update({"data": None, "mtime_ns": None})

    slim = server._slim_fact_payload_for_ui_snapshot_summary(
        {
            "slug": "highest-temperature-in-new-york-on-july-1-2026",
            "title": "Highest temperature in New York on July 1?",
            "city": "New York",
            "station_id": "KNYC",
            "event_date": "2026-07-01",
        }
    )

    assert "model_weight_shadow" not in slim
    assert "basin_regime_feature_set" not in slim


def test_ui_index_renders_model_weight_shadow_and_basin_feature_set() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "renderEventBayesModelWeightShadowPanel" in text
    assert "eventBayesBasinRegimeFeatureSetPayload" in text
    assert "模型权重 shadow" in text
    assert "不能上线原因" in text
    assert server.BoundedThreadingHTTPServer.daemon_threads is True
    assert server.BoundedThreadingHTTPServer.block_on_close is False
    assert server.UI_STATIC_MAX_CONCURRENT_REQUESTS <= 8
    assert (
        server.UI_MAX_CONCURRENT_REQUESTS
        + server.UI_PRIORITY_MAX_CONCURRENT_REQUESTS
        + server.UI_STATIC_MAX_CONCURRENT_REQUESTS
    ) <= 20
    assert server.UI_MAX_CONCURRENT_REQUESTS <= server.UI_MAX_CONCURRENT_REQUESTS_HARD_CAP
    assert "/index.html" in server.UI_STATIC_REQUEST_PATHS
    assert "/" in server.UI_STATIC_REQUEST_PATHS
    assert "/lcb.html" in server.UI_STATIC_REQUEST_PATHS
    assert "/api/auto-trade-status" in server.UI_PRIORITY_REQUEST_PATHS
    assert "/api/model-update-stats" in server.UI_PRIORITY_REQUEST_PATHS
    server_text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    assert 'if request_path == "/api/reload-ts":' in server_text
    assert 'self._send_raw_json_and_close(request, {"mtime": html_mtime()})' in server_text
    assert 'static_request = request_path in UI_STATIC_REQUEST_PATHS' in server_text
    assert 'priority_request = request_path in UI_PRIORITY_REQUEST_PATHS' in server_text
    assert 'self._bypass_slot_requests[id(request)] = "static"' in server_text


def test_chengdu_intraday_frontend_fetches_nonblocking_summary() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = html.index("function eventIntelChengduIntradaySummaryUrl(date, options = {})")
    end = html.index("function eventIntelIntradayLastRequestMap", start)
    block = html[start:end]

    assert "const params = new URLSearchParams();" in block
    assert "if (dateKey) params.set('date', dateKey);" in block
    assert "params.set('nonblocking', '1');" in block
    assert "return `/api/chengdu-intraday-summary?${params.toString()}`;" in block
    assert "fetchJSON(eventIntelChengduIntradaySummaryUrl(date, { force })," in html
    assert "'?date=' + encodeURIComponent(date) + '&nonblocking=1'" in html
    assert "'?nonblocking=1'" in html
    assert "/api/chengdu-intraday-summary?date=${encodeURIComponent(date)}`" not in html


def test_frontend_intraday_payload_cannot_override_snapshot_posterior_distribution() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    assert "function eventIntelDistributionHasSettlementPosterior(rows)" in html
    start = html.index("function eventIntelEffectiveIntradayDistribution(item)")
    end = html.index("function eventIntelItemWithIntradayPayload", start)
    block = html[start:end]

    assert "const itemDistribution = item && Array.isArray(item.distribution) ? item.distribution : [];" in block
    assert "if (ctpRows.length) return eventIntelMergeDistributionDiagnostics(ctpRows, itemDistribution);" in block
    assert "if (eventIntelDistributionHasSettlementPosterior(itemDistribution))" in block
    assert block.index("if (eventIntelDistributionHasSettlementPosterior(itemDistribution))") < block.index("payload.bucket_probabilities")
    assert "eventIntelMergeDistributionDiagnostics(itemDistribution, payloadRows)" in block

    start = html.index("function eventIntelItemWithIntradayPayload(item, payloadArg = null)")
    end = html.index("function refreshEventIntelDistributionSlot", start)
    payload_block = html[start:end]
    assert "const baseRows = Array.isArray(data.distribution) ? data.distribution : [];" in payload_block
    assert "eventIntelDistributionHasSettlementPosterior(baseRows)" in payload_block
    assert "eventIntelMergeDistributionDiagnostics(baseRows, payloadBucketRows.length ? payloadBucketRows : payloadTemperatureRows)" in payload_block
    assert "posterior_temperature_distribution: objectOrKeep(data.posterior_temperature_distribution, payload.posterior_temperature_distribution)" in payload_block
    assert "const liveTopLabel = (() => {" in payload_block
    assert "const canCarryDataTargetContract = Boolean(" in payload_block
    assert "target_bucket_outlook: objectOrKeep(payload.target_bucket_outlook, canCarryDataTargetContract ? data.target_bucket_outlook : {})" in payload_block
    assert "target_realization_capital_plan: objectOrKeep(payload.target_realization_capital_plan, canCarryDataTargetContract ? data.target_realization_capital_plan : {})" in payload_block
    assert "target_bucket: payload.target_bucket || (canCarryDataTargetContract ? data.target_bucket : '')" in payload_block

    start = html.index("function eventIntelNewerIntradayPayload(a, b)")
    end = html.index("function eventIntelFactMetricValue", start)
    newer_block = html[start:end]
    assert "if (Number.isFinite(aMs) && Number.isFinite(bMs) && aMs !== bMs) return bMs > aMs ? b : a;" in newer_block
    assert newer_block.index("const aMs = eventIntelIntradayPayloadMs(a);") < newer_block.index("const aRank = eventIntelIntradayPayloadRank(a);")


def test_ui_snapshot_handler_uses_page_size_as_summary_limit() -> None:
    server_text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    block = server_text[
        server_text.index('if path == "/api/ui_snapshot":') : server_text.index('if path == "/api/london-follow-sim":')
    ]
    assert 'if "limit" not in params and params.get("page_size", [""])[0]:' in block
    assert 'limit = int(params.get("page_size", [""])[0])' in block
    assert "force_rebuild=force" in block
    assert "refresh_requested" in block
    assert "refresh = str(params.get(\"refresh\"" in block
    assert "force = str(params.get(\"force\"" in block


def test_ui_snapshot_summary_keeps_ctp_ssot_and_aligns_target_plan() -> None:
    payload = attach_ctp_distribution(
        {
            "slug": "highest-temperature-in-chengdu-on-june-30-2026",
            "title": "Highest temperature in Chengdu on June 30?",
            "event_date": "2026-06-30",
            "run_date": "2026-06-29",
            "market_var": "temperature_2m_max",
            "market_unit": "C",
            "rounding_rule": "whole_degrees",
            "target_bucket": "29°C",
            "target_bucket_probability_pct": 30.0,
            "target_bucket_selection": {
                "target_bucket": "29°C",
                "target_probability_pct": 30.0,
            },
            "target_realization_capital_plan": {
                "ok": True,
                "target_bucket": "29°C",
                "target_posterior": 0.30,
            },
            "distribution": [
                {
                    "outcome": "30°C or higher",
                    "q_temperature_bucket_posterior": 0.70,
                    "q_bayes_posterior": 0.20,
                    "q_round": 0.65,
                },
                {
                    "outcome": "29°C",
                    "q_temperature_bucket_posterior": 0.30,
                    "q_bayes_posterior": 0.80,
                    "q_round": 0.10,
                },
            ],
        }
    )
    payload["posterior_temperature_distribution"]["source_components"] = {
        "components": [{"source_id": "wu", "bucket": "30°C or higher", "probability": 0.7}],
        "groups": [{"group": "forecast", "effective_precision": 8.0}],
    }
    payload["posterior_temperature_distribution"]["bucket_probabilities"][0]["probability"] = 0.20
    payload["posterior_temperature_distribution"]["bucket_probabilities"][1]["probability"] = 0.80

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    ptd = slim["posterior_temperature_distribution"]
    assert ptd["ui_compact"] is True
    assert ptd["ssot_source"] == "posterior_temperature_distribution.bucket_probabilities"
    assert ptd["bucket_probabilities"][0]["outcome"] == "30°C or higher"
    assert ptd["bucket_probabilities"][0]["probability"] == pytest.approx(0.70)
    assert ptd["bucket_probabilities"][0]["pre_distribution_probability"] == pytest.approx(0.20)
    assert ptd["aligned_to_distribution"] is True
    assert ptd["alignment_source"] == "event_fact_latest.distribution.q_temperature_bucket_posterior"
    assert "source_components" in ptd
    assert "grid" not in ptd
    assert slim["target_bucket"] == "30°C or higher"
    assert slim["target_bucket_probability_pct"] == pytest.approx(70.0)
    assert slim["target_bucket_selection"]["target_bucket"] == "30°C or higher"
    assert slim["target_bucket_selection"]["target_probability_pct"] == pytest.approx(70.0)
    assert slim["target_realization_capital_plan"]["target_bucket"] == "30°C or higher"
    assert slim["target_realization_capital_plan"]["target_posterior"] == pytest.approx(0.70)


def test_ui_snapshot_summary_aligns_all_page_probability_copies_to_distribution() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-30-2026",
        "title": "Highest temperature in Chengdu on June 30?",
        "event_date": "2026-06-30",
        "run_date": "2026-06-29",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "target_bucket": "30°C or higher",
        "target_bucket_probability_pct": 33.17,
        "posterior_highest_bucket_label": "30°C or higher",
        "posterior_highest_probability_pct": 33.17,
        "distribution": [
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.2876082723775563},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.25248316104552937},
            {"outcome": "30°C or higher", "q_temperature_bucket_posterior": 0.45382930769085045},
        ],
        "target_bucket_selection": {
            "target_bucket": "30°C or higher",
            "target_probability_pct": 33.17,
            "posterior_highest_bucket_label": "30°C or higher",
            "posterior_highest_probability_pct": 33.17,
        },
        "target_bucket_outlook": {
            "target_bucket": "30°C or higher",
            "target_final_probability_pct": 33.17,
            "final_bucket": "30°C or higher",
            "final_bucket_probability_pct": 33.17,
            "final_bucket_posterior_probability_pct": 33.17,
            "posterior_final_bucket": "30°C or higher",
            "posterior_final_bucket_probability_pct": 33.17,
            "cumulative_posterior_final_bucket": "30°C or higher",
            "cumulative_posterior_final_bucket_probability_pct": 33.17,
        },
        "temperature_path_distribution": {
            "target_bucket": "30°C or higher",
            "target_bucket_posterior": 0.3317,
            "target_bucket_posterior_pct": 33.17,
            "target": {"bucket": "30°C or higher", "posterior": 0.3317, "posterior_pct": 33.17},
            "bucket_distribution": [
                {"bucket": "28°C", "posterior": 0.2102, "posterior_pct": 21.02},
                {"bucket": "29°C", "posterior": 0.1845, "posterior_pct": 18.45},
                {"bucket": "30°C or higher", "posterior": 0.3317, "posterior_pct": 33.17},
            ],
        },
        "target_switch_context": {
            "target_bucket": "30°C or higher",
            "target_probability_pct": 33.17,
            "candidate_bucket": "29°C",
            "candidate_probability_pct": 18.45,
        },
        "chengdu_intraday_summary": {
            "target_bucket": "30°C or higher",
            "target_bucket_probability_pct": 33.17,
            "target_bucket_selection": {
                "target_bucket": "30°C or higher",
                "target_probability_pct": 33.17,
                "posterior_highest_bucket_label": "30°C or higher",
                "posterior_highest_probability_pct": 33.17,
            },
            "target_bucket_outlook": {
                "target_bucket": "30°C or higher",
                "target_final_probability_pct": 33.17,
                "final_bucket": "30°C or higher",
                "final_bucket_probability_pct": 33.17,
                "final_bucket_posterior_probability_pct": 33.17,
                "posterior_final_bucket": "30°C or higher",
                "posterior_final_bucket_probability_pct": 33.17,
                "cumulative_posterior_final_bucket": "30°C or higher",
                "cumulative_posterior_final_bucket_probability_pct": 33.17,
            },
            "temperature_path_distribution": {
                "target_bucket": "30°C or higher",
                "target_bucket_posterior": 0.3317,
                "target_bucket_posterior_pct": 33.17,
                "target": {"bucket": "30°C or higher", "posterior": 0.3317, "posterior_pct": 33.17},
                "bucket_distribution": [
                    {"bucket": "28°C", "posterior": 0.2102, "posterior_pct": 21.02},
                    {"bucket": "29°C", "posterior": 0.1845, "posterior_pct": 18.45},
                    {"bucket": "30°C or higher", "posterior": 0.3317, "posterior_pct": 33.17},
                ],
            },
            "target_switch_context": {
                "target_bucket": "30°C or higher",
                "target_probability_pct": 33.17,
                "candidate_bucket": "29°C",
                "candidate_probability_pct": 18.45,
            },
        },
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    summary = slim["chengdu_intraday_summary"]

    assert slim["target_bucket_probability_pct"] == pytest.approx(45.38293076908504)
    assert slim["target_bucket_selection"]["posterior_highest_probability_pct"] == pytest.approx(45.38293076908504)
    assert slim["target_bucket_outlook"]["final_bucket_posterior_probability_pct"] == pytest.approx(45.38293076908504)
    assert slim["temperature_path_distribution"]["target"]["posterior"] == pytest.approx(0.45382930769085045)
    assert slim["temperature_path_distribution"]["bucket_distribution"][0]["posterior"] == pytest.approx(0.2876082723775563)
    assert slim["target_switch_context"]["target_probability_pct"] == pytest.approx(45.38293076908504)
    assert summary["target_bucket_selection"]["posterior_highest_probability_pct"] == pytest.approx(45.38293076908504)
    assert summary["target_bucket_outlook"]["final_bucket_posterior_probability_pct"] == pytest.approx(45.38293076908504)
    assert summary["temperature_path_distribution"]["target"]["posterior"] == pytest.approx(0.45382930769085045)
    assert summary["target_switch_context"]["target_probability_pct"] == pytest.approx(45.38293076908504)


def test_ui_snapshot_summary_clears_hard_zero_second_target_after_final_lock() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-29-2026",
        "event_date": "2026-06-29",
        "run_date": "2026-06-29",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "distribution": [
            {
                "outcome": "33°C",
                "q_temperature_bucket_posterior": 1.0,
                "posterior_settlement_candidate_bucket_hard_zero": False,
            },
            {
                "outcome": "34°C",
                "q_temperature_bucket_posterior": 0.0,
                "posterior_settlement_candidate_bucket_hard_zero": True,
                "posterior_settlement_candidate_bucket_hard_zero_reasons": [
                    "final_bucket_lock",
                    "hard_source_final_bucket_lock",
                ],
            },
        ],
        "target_bucket_selection": {
            "target_bucket": "33°C",
            "target_probability_pct": 100.0,
            "second_target_bucket": "34°C",
            "second_target_probability_pct": 0.0,
        },
        "target_bucket_outlook": {
            "target_bucket": "33°C",
            "target_final_probability_pct": 100.0,
            "final_bucket": "33°C",
            "final_bucket_probability_pct": 100.0,
            "second_target_bucket": "34°C",
            "second_target_final_probability_pct": 0.0,
        },
        "temperature_path_distribution": {
            "target_bucket": "33°C",
            "target_bucket_posterior": 1.0,
            "second_target_bucket": "34°C",
            "second_target_bucket_posterior": 0.0,
        },
        "target_realization_capital_plan": {
            "ok": True,
            "target_bucket": "33°C",
            "target_posterior": 1.0,
            "second_target_bucket": "34°C",
            "second_target_posterior": 0.0,
        },
        "target_switch_context": {
            "target_bucket": "33°C",
            "target_probability_pct": 100.0,
            "candidate_bucket": "34°C",
            "candidate_probability_pct": 0.0,
        },
        "chengdu_intraday_summary": {
            "target_bucket": "33°C",
            "target_bucket_selection": {
                "target_bucket": "33°C",
                "second_target_bucket": "34°C",
                "second_target_probability_pct": 0.0,
            },
            "target_bucket_outlook": {
                "target_bucket": "33°C",
                "second_target_bucket": "34°C",
                "second_target_final_probability_pct": 0.0,
            },
            "temperature_path_distribution": {
                "target_bucket": "33°C",
                "second_target_bucket": "34°C",
                "second_target_bucket_posterior": 0.0,
            },
        },
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    summary = slim["chengdu_intraday_summary"]

    assert slim["target_bucket_selection"].get("second_target_bucket") in (None, "")
    assert slim["target_bucket_outlook"].get("second_target_bucket") in (None, "")
    assert slim["temperature_path_distribution"].get("second_target_bucket") in (None, "")
    assert slim["target_realization_capital_plan"].get("second_target_bucket") in (None, "")
    assert slim["target_switch_context"].get("candidate_bucket") in (None, "")
    assert summary["target_bucket_selection"].get("second_target_bucket") in (None, "")
    assert summary["target_bucket_outlook"].get("second_target_bucket") in (None, "")
    assert summary["temperature_path_distribution"].get("second_target_bucket") in (None, "")


def test_ui_snapshot_summary_common_limits_share_cache_bucket() -> None:
    assert server._ui_snapshot_summary_cache_limit(2) == 24
    assert server._ui_snapshot_summary_cache_limit(3) == 24
    assert server._ui_snapshot_summary_cache_limit(5) == 24
    assert server._ui_snapshot_summary_cache_limit(12) == 24
    assert server._ui_snapshot_summary_cache_limit(20) == 24
    assert server._ui_snapshot_summary_cache_limit(24) == 24
    assert server._ui_snapshot_summary_cache_limit(25) == 25
    assert server._ui_snapshot_summary_cache_limit(40) == 40


def test_auto_trade_criteria_runtime_snapshot_cache_serves_fresh_without_db_scan(monkeypatch) -> None:
    monkeypatch.setenv("PM_AUTO_TRADE_CRITERIA_RUNTIME_SNAPSHOT_CACHE_SEC", "60")
    monkeypatch.setattr(
        server,
        "_latest_lcb_tradeability_runtime_snapshot",
        lambda _limit_rows=600: (_ for _ in ()).throw(AssertionError("fresh cache should not rescan decisions")),
    )
    with server._AUTO_TRADE_CRITERIA_RUNTIME_SNAPSHOT_LOCK:
        server._AUTO_TRADE_CRITERIA_RUNTIME_SNAPSHOT_CACHE = (
            time.time(),
            {"path_probability_follow_edge_lcb_min": 0.012},
        )
        server._AUTO_TRADE_CRITERIA_RUNTIME_SNAPSHOT_REFRESHING = False

    payload = server._latest_lcb_tradeability_runtime_snapshot_cached()

    assert payload["path_probability_follow_edge_lcb_min"] == 0.012
    assert "lcb_runtime_tradeability_cache_stale" not in payload


def test_auto_trade_criteria_routes_use_cached_runtime_snapshot() -> None:
    text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    criteria_start = text.index('if path == "/api/auto-trade-criteria":')
    criteria_end = text.index('if path == "/api/auto-trade-presets":', criteria_start)
    criteria_block = text[criteria_start:criteria_end]
    config_start = text.index('if path == "/api/auto-trade-config":')
    config_end = text.index('if path == "/api/auto-trade-criteria":', config_start)
    config_block = text[config_start:config_end]

    assert "_latest_lcb_tradeability_runtime_snapshot_cached()" in criteria_block
    assert "_latest_lcb_tradeability_runtime_snapshot()" not in criteria_block
    assert "_latest_lcb_tradeability_runtime_snapshot_cached()" in config_block
    assert "_latest_lcb_tradeability_runtime_snapshot()" not in config_block


def test_distribution_top_label_prefers_temperature_bucket_posterior() -> None:
    rows = [
        {"outcome": "28°C", "q_temperature_bucket_posterior": 0.20, "q_bayes_posterior": 0.90},
        {"outcome": "29°C", "q_temperature_bucket_posterior": 0.60, "q_bayes_posterior": 0.10},
    ]

    assert server._top_label_from_distribution_rows(rows) == "29°C"


def test_fact_write_top_label_helper_prefers_temperature_bucket_posterior() -> None:
    source = inspect.getsource(server._insert_event_fact_history)
    helper_start = source.index("def _top_label_from_rows(rows_obj: Any) -> str:")
    helper_end = source.index("distribution = item.get(\"distribution\")", helper_start)
    helper = source[helper_start:helper_end]

    assert helper.index('row.get("q_temperature_bucket_posterior")') < helper.index('row.get("q_bayes_posterior")')


def test_fact_write_merges_observation_led_summary_back_into_distribution() -> None:
    fact_as_of = "2026-06-29T06:00:00+08:00"
    slug = "highest-temperature-in-chengdu-on-june-29-2026"
    rows = [
        ("25°C or below", 0.02),
        ("26°C", 0.04),
        ("27°C", 0.06),
        ("28°C", 0.08),
        ("29°C", 0.16),
        ("30°C", 0.24),
        ("31°C", 0.40),
    ]
    item = {
        "slug": slug,
        "event_date": "2026-06-29",
        "run_date": "2026-06-29",
        "station_id": "ZUUU",
        "event_tz": "Asia/Shanghai",
        "rounding_rule": "whole_degrees",
        "market_var": "temperature_2m_max",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_latest_at": fact_as_of,
            "zuuu_metar_raw_awc_latest_temp_c": 25.0,
            "zuuu_metar_raw_awc_today_max_c": 25.0,
            "zuuu_metar_raw_awc_today_max_at": fact_as_of,
            "observed_today_max_c": 25.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "observed_temp_c": 25.0,
            "peak_estimator_mu_c": 25.6,
            "peak_estimator_sigma_c": 1.0,
            "peak_estimator_source": "test_peak",
        },
        "distribution": [
            {
                "outcome": label,
                "q_temperature_bucket_posterior": prob,
                "q_bayes_posterior": prob,
                "q_raw": prob,
                "best_ask": 0.50,
                "best_ask_no": 0.50,
            }
            for label, prob in rows
        ],
    }
    old_top = max(item["distribution"], key=lambda row: row["q_temperature_bucket_posterior"])["outcome"]

    server._attach_chengdu_intraday_summary_fields_for_fact_write(
        item,
        slug=slug,
        event_date="2026-06-29",
        fact_as_of=fact_as_of,
        snapshot_id=1,
    )
    server._attach_target_realization_capital_plan(item)

    top_row = max(item["distribution"], key=lambda row: row["q_temperature_bucket_posterior"])
    plan = item["target_realization_capital_plan"]
    assert old_top == "31°C"
    assert item["observation_led_likelihood"]["applied"] is True
    assert top_row["outcome"] == item["target_bucket"] == item["ssot_target_bucket"] == plan["target_bucket"]
    assert top_row["outcome"] != old_top
    assert plan["target_posterior"] == pytest.approx(top_row["q_temperature_bucket_posterior"])


def test_intraday_summary_read_side_merges_observation_led_rows_into_distribution() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-29-2026",
        "event_date": "2026-06-29",
        "run_date": "2026-06-29",
        "station_id": "ZUUU",
        "distribution": [
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.16, "q_bayes_posterior": 0.16},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.24, "q_bayes_posterior": 0.24},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.40, "q_bayes_posterior": 0.40},
        ],
    }
    summary = {
        "ok": True,
        "target_bucket": "29°C",
        "ssot_target_bucket": "29°C",
        "target_bucket_probability_pct": 28.75,
        "posterior_highest_bucket_label": "29°C",
        "posterior_highest_probability_pct": 28.75,
        "observation_led_likelihood": {
            "applied": True,
            "target_bucket_before": "31°C",
            "target_bucket_after": "29°C",
            "source": "observation_led_bayes",
        },
        "bucket_probabilities": [
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.2875, "q_bayes_posterior": 0.2875},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.2371, "q_bayes_posterior": 0.2371},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.1214, "q_bayes_posterior": 0.1214},
        ],
    }

    server._apply_chengdu_intraday_summary_to_snapshot_out(
        payload,
        summary,
        force_target_fields=True,
    )

    top_row = max(payload["distribution"], key=lambda row: row["q_temperature_bucket_posterior"])
    assert top_row["outcome"] == "29°C"
    assert top_row["q_temperature_bucket_posterior"] == pytest.approx(0.2875)
    assert payload["target_bucket"] == "29°C"
    assert payload["ssot_target_bucket"] == "29°C"
    assert payload["posterior_highest_bucket_label"] == "29°C"
    assert payload["observation_led_likelihood"]["applied"] is True
    assert payload["observation_led_likelihood_posterior_materialized"] is True


def test_intraday_summary_keeps_morning_not_final_observation_diagnostic_only() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-1-2026",
        "event_date": "2026-07-01",
        "run_date": "2026-07-01",
        "station_id": "ZUUU",
        "event_tz": "Asia/Shanghai",
        "fact_provenance": {
            "d0_observed_feasibility_guard": {
                "applied": False,
                "skipped_reason": "intraday_observed_max_not_final",
                "today_key": "2026-07-01",
            }
        },
        "distribution": [
            {"outcome": "25°C or below", "q_temperature_bucket_posterior": 0.05, "q_bayes_posterior": 0.05},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.55, "q_bayes_posterior": 0.55},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.40, "q_bayes_posterior": 0.40},
        ],
    }
    summary = {
        "ok": True,
        "target_bucket": "25°C or below",
        "target_bucket_probability_pct": 36.35,
        "observation_led_likelihood": {
            "applied": True,
            "target_bucket_before": "31°C",
            "target_bucket_after": "25°C or below",
        },
        "bucket_probabilities": [
            {"outcome": "25°C or below", "q_temperature_bucket_posterior": 0.3635, "q_bayes_posterior": 0.3635},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.12, "q_bayes_posterior": 0.12},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.08, "q_bayes_posterior": 0.08},
        ],
    }

    server._apply_chengdu_intraday_summary_to_snapshot_out(
        payload,
        summary,
        force_target_fields=True,
        event_date="2026-07-01",
        fact_as_of="2026-06-30T23:42:00+00:00",
        event_tz="Asia/Shanghai",
    )

    top_row = max(payload["distribution"], key=lambda row: row["q_temperature_bucket_posterior"])
    assert top_row["outcome"] == "31°C"
    assert payload["target_bucket"] == "31°C"
    assert payload["observation_led_likelihood"]["diagnostic_only"] is True
    assert payload["observation_led_likelihood_posterior_materialized"] is False
    assert payload["fact_provenance"]["observation_led_likelihood"]["posterior_materialized"] is False


def test_model_raw_top_history_uses_latest_snapshot_before_fact_as_of() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        )
        """
    )

    def snapshot_payload(top_label: str, q: float) -> str:
        return json.dumps(
            {
                "distribution": [
                    {
                        "q_ensemble_qm_meta": {
                            "raw_bucket_probability": {
                                "labels": ["29°C", "30°C", "31°C"],
                                "probs_by_model": {
                                    "gfs_seamless": {"29°C": 0.1, "30°C": q, "31°C": 0.0},
                                    "icon_seamless": {"29°C": 0.0, "30°C": 0.2, "31°C": q},
                                    "ecmwf_ifs025": {"29°C": q, "30°C": 0.1, "31°C": 0.0},
                                },
                            }
                        }
                    }
                ],
                "score_composer": {"model_snapshot_id": top_label},
            }
        )

    slug = "highest-temperature-in-chengdu-on-june-29-2026"
    conn.execute(
        "INSERT INTO model_snapshots VALUES (1, ?, ?, ?, ?, ?, ?)",
        ("2026-06-29", "2026-06-29", slug, snapshot_payload("old", 0.6), None, "2026-06-28T21:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO model_snapshots VALUES (2, ?, ?, ?, ?, ?, ?)",
        ("2026-06-29", "2026-06-29", slug, snapshot_payload("new", 0.8), None, "2026-06-28T23:00:00+00:00"),
    )
    conn.commit()

    history = server._model_raw_top_history_for_ui_snapshot(
        conn,
        {"slug": slug, "event_date": "2026-06-29"},
        {"model_snapshot_id": 1, "fact_as_of": "2026-06-28T23:30:00+00:00"},
    )
    conn.close()

    assert history
    assert history["referenced_model_snapshot_id"] == 1
    assert history["current_snapshot_id"] == 2
    assert history["selected_snapshot_id"] == 2
    assert history["selection_reason"] == "latest_model_snapshot_before_fact_as_of"
    assert history["points"][-1]["models"]["gfs_seamless"]["label"] == "30°C"


def test_slim_ui_snapshot_keeps_model_raw_top_history_with_model_probability_maps() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-29-2026",
        "event_date": "2026-06-29",
        "model_raw_top_history": {
            "version": "model_raw_top_history_v1",
            "points": [{"snapshot_id": 1, "models": {"gfs_seamless": {"label": "30°C", "q": 0.8}}}],
        },
        "distribution": [
            {
                "outcome": "30°C",
                "q_temperature_bucket_posterior": 0.4,
                "q_ensemble_qm_meta": {
                    "raw_bucket_probability": {
                        "labels": ["30°C"],
                        "probs_by_model": {"gfs_seamless": {"30°C": 0.8}},
                    }
                },
            }
        ],
    }

    compact = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert "model_raw_top_history" in compact
    assert compact["model_raw_top_history"]["points"][0]["models"]["gfs_seamless"]["label"] == "30°C"


def test_chengdu_anomaly_alerts_serves_stale_cache_while_rebuilding(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "WEATHER_ANOMALY_REPORT_DIR", tmp_path)
    monkeypatch.setattr(server, "CHENGDU_ANOMALY_ALERT_CACHE_SEC", 1)
    monkeypatch.setattr(server, "CHENGDU_ANOMALY_ALERT_STALE_CACHE_SEC", 300)
    monkeypatch.setattr(server, "_start_chengdu_anomaly_alert_rebuild", lambda _cache_key: False)
    cache_key = str(tmp_path)
    with server._CHENGDU_ANOMALY_ALERT_CACHE_LOCK:
        server._CHENGDU_ANOMALY_ALERT_CACHE.clear()
        server._CHENGDU_ANOMALY_ALERT_CACHE[cache_key] = (
            time.time() - 30.0,
            {"ok": True, "grouped_alerts": [{"alert_type": "market_context"}]},
        )

    payload = server.latest_chengdu_anomaly_alerts()

    assert payload["ok"] is True
    assert payload["cache_hit"] is True
    assert payload["cache_stale"] is True
    assert payload["cache_rebuild_in_flight"] is True
    assert payload["grouped_alerts"][0]["alert_type"] == "market_context"


def test_chengdu_intraday_nonblocking_throttles_repeated_stale_revalidate(monkeypatch) -> None:
    key = "2099-01-04"
    monkeypatch.setattr(server, "CHENGDU_INTRADAY_SUMMARY_CACHE_SEC", 1)
    monkeypatch.setattr(server, "CHENGDU_INTRADAY_SUMMARY_STALE_CACHE_SEC", 60)
    monkeypatch.setattr(server, "CHENGDU_INTRADAY_SUMMARY_REVALIDATE_MIN_INTERVAL_SEC", 60)
    monkeypatch.setattr(
        server,
        "_start_chengdu_intraday_summary_rebuild",
        lambda _key: (_ for _ in ()).throw(AssertionError("rebuild should be throttled")),
    )
    with server._CHENGDU_INTRADAY_SUMMARY_CACHE_LOCK:
        server._CHENGDU_INTRADAY_SUMMARY_CACHE[key] = (
            time.time() - 10.0,
            {"ok": True, "event_date": key, "target_bucket": "29°C"},
        )
    with server._CHENGDU_INTRADAY_SUMMARY_REVALIDATE_LOCK:
        server._CHENGDU_INTRADAY_SUMMARY_REVALIDATE_LAST[key] = time.time()
    try:
        payload = server.latest_chengdu_intraday_summary_nonblocking(key)
    finally:
        with server._CHENGDU_INTRADAY_SUMMARY_CACHE_LOCK:
            server._CHENGDU_INTRADAY_SUMMARY_CACHE.pop(key, None)
        with server._CHENGDU_INTRADAY_SUMMARY_REVALIDATE_LOCK:
            server._CHENGDU_INTRADAY_SUMMARY_REVALIDATE_LAST.pop(key, None)

    assert payload["ok"] is True
    assert payload["cache_hit"] is True
    assert payload["cache_stale"] is True
    assert payload["cache_rebuild_started"] is False
    assert payload["cache_stale_reason"] == "summary_revalidate_throttled"
    assert payload["target_bucket"] == "29°C"


def test_chengdu_intraday_endpoint_compact_keeps_ssot_temperature_fields() -> None:
    payload = {
        "ok": True,
        "event_date": "2099-01-05",
        "target_bucket": "29°C",
        "key_metrics": {
            "observed_temp_c": 24.4,
            "observed_path_temp_c": 28.0,
            "observed_path_temp_source": "observed_today_max_floor",
            "temperature_momentum_rates_c_per_hour": {"1h": 0.2},
        },
        "bucket_probabilities": [
            {
                "outcome": "29°C",
                "q_bayes_posterior_pct": 26.8,
                "q_posterior_observed_constraints_reason_codes": ["observed_floor"],
                "bayes_chain": {"large": "detail"},
                "q_temperature_bucket_score_components": {"large": "detail"},
            }
        ],
        "target_bucket_selection": {
            "target_bucket": "29°C",
            "target_probability_pct": 26.8,
            "target_row": {
                "outcome": "29°C",
                "q_bayes_posterior_pct": 26.8,
                "bayes_chain": {"large": "detail"},
            },
        },
        "source_observation_comparison": {
            "awc": {
                "source_id": "zuuu_metar_raw_awc",
                "today_max_c": 27.0,
                "current_temp_c": 27.0,
                "settlement_proxy_bucket": "27°C",
                "visual_nearest_bucket": "27°C",
                "raw_observations": {"latest_metar": {"raw_text": "large detail"}},
            },
            "wu_twc": {
                "source_id": "wu_twc_observed_intraday",
                "today_max_c": 25.0,
                "settlement_proxy_bucket": "25°C",
            },
        },
    }

    compact = server._compact_chengdu_intraday_summary_for_endpoint(payload)

    assert compact["summary_compact"] is True
    assert compact["key_metrics"]["observed_temp_c"] == 24.4
    assert compact["key_metrics"]["observed_path_temp_c"] == 28.0
    assert compact["key_metrics"]["observed_path_temp_source"] == "observed_today_max_floor"
    assert "temperature_momentum_rates_c_per_hour" not in compact["key_metrics"]
    row = compact["bucket_probabilities"][0]
    assert row["outcome"] == "29°C"
    assert row["q_bayes_posterior_pct"] == 26.8
    assert row["q_posterior_observed_constraints_reason_codes"] == ["observed_floor"]
    assert "bayes_chain" not in row
    assert "q_temperature_bucket_score_components" not in row
    assert compact["target_bucket_selection"]["target_bucket"] == "29°C"
    assert compact["target_bucket_selection"]["target_probability_pct"] == 26.8
    assert "target_row" not in compact["target_bucket_selection"]
    comparison = compact["source_observation_comparison"]
    assert comparison["awc"]["today_max_c"] == 27.0
    assert comparison["awc"]["settlement_proxy_bucket"] == "27°C"
    assert "raw_observations" not in comparison["awc"]
    assert comparison["wu_twc"]["today_max_c"] == 25.0


def test_chengdu_intraday_endpoint_compact_keeps_temperature_regime_v1_payload() -> None:
    path_candidates = [
        {
            "draw": idx,
            "hour_local": 15.0 + (idx % 3),
            "regime": "accelerating_warming",
            "forcing_turn_probability": 0.25,
            "temp_c": 34.0 + idx * 0.01,
        }
        for idx in range(12)
    ]
    payload = {
        "ok": True,
        "event_date": "2099-01-05",
        "key_metrics": {
            "observed_temp_c": 31.0,
            "peak_estimator": {
                "enabled": True,
                "source": "energy_balance_nowcast",
                "observed_temperature_trajectory_regime_v1": {
                    "enabled": True,
                    "available": True,
                    "source": "observed_temperature_trajectory_regime_v1",
                    "group": "temperature_trajectory",
                    "role": "observed_realization",
                    "regime_model_status": "heuristic_live_no_bundle",
                    "path_aggregation": "deterministic_path_monte_carlo_max",
                    "path_draw_count": 512,
                    "path_mu_c": 34.2,
                    "path_sigma_c": 0.42,
                    "path_p90_c": 34.7,
                    "kinematic_baseline_mu": 33.8,
                    "regime_probs": {"accelerating_warming": 0.62, "forced_turn_risk": 0.18},
                    "peak_hour_distribution": [{"hour_local": 16, "prob": 0.7}],
                    "forcing_onset_probs": {"forced_turn_probability": 0.22},
                    "path_candidates": path_candidates,
                },
            },
        },
        "posterior_temperature_distribution": {
            "version": "continuous_temperature_posterior_v1",
            "bucket_probabilities": [{"outcome": "34°C", "probability": 0.44}],
            "source_components": {
                "precision_cap": 8.0,
                "groups": [
                    {
                        "group": "temperature_trajectory",
                        "effective_precision": 5.67,
                        "members": [
                            {
                                "source_id": "temperature_trajectory",
                                "source": "observed_temperature_trajectory_regime_v1",
                                "precision": 5.67,
                                "raw": {"source": "observed_temperature_trajectory_regime_v1"},
                            }
                        ],
                    }
                ]
            },
            "likelihood_updates": {
                "applied": True,
                "updates": [
                    {
                        "source_id": "independent_physics_residual",
                        "group": "physical_residual",
                        "role": "physical_residual_likelihood_update",
                        "effective_precision": 0.75,
                    }
                ],
            },
        },
    }

    compact = server._compact_chengdu_intraday_summary_for_endpoint(payload)

    v1 = compact["key_metrics"]["peak_estimator"]["observed_temperature_trajectory_regime_v1"]
    assert v1["source"] == "observed_temperature_trajectory_regime_v1"
    assert v1["path_aggregation"] == "deterministic_path_monte_carlo_max"
    assert v1["path_mu_c"] == pytest.approx(34.2)
    assert v1["path_sigma_c"] == pytest.approx(0.42)
    assert v1["kinematic_baseline_mu"] == pytest.approx(33.8)
    assert v1["regime_probs"]["accelerating_warming"] == pytest.approx(0.62)
    assert len(v1["path_candidates"]) == 8
    ptd = compact["posterior_temperature_distribution"]
    assert ptd["source_components"]["groups"][0]["group"] == "temperature_trajectory"
    assert ptd["source_components"]["groups"][0]["members"][0]["source"] == "observed_temperature_trajectory_regime_v1"
    assert ptd["source_components"]["precision_cap"] == pytest.approx(8.0)
    assert ptd["likelihood_updates"]["updates"][0]["source_id"] == "independent_physics_residual"


def test_compact_target_selection_syncs_display_bayes_to_temperature_ssot() -> None:
    selection = {
        "target_bucket": "30°C",
        "target_probability_pct": 100.0,
        "target_row": {
            "outcome": "30°C",
            "q_temperature_bucket_posterior": 1.0,
            "q_bayes_posterior": 0.5456630872628443,
        },
        "watch_buckets": [
            {
                "outcome": "30°C",
                "q_temperature_bucket_posterior": 1.0,
                "q_bayes_posterior": 0.5456630872628443,
            },
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.0,
                "q_bayes_posterior": 0.039,
            },
        ],
    }

    compact = server._compact_ui_snapshot_target_selection(selection)

    target_row = compact["target_row"]
    assert target_row["q_temperature_bucket_posterior"] == 1.0
    assert target_row["q_bayes_posterior"] == 1.0
    assert target_row["q_bayes_posterior_pct"] == 100.0
    assert target_row["q_bayes_posterior_pre_ssot_display_alignment"] == pytest.approx(0.5456630872628443)
    watch = {row["outcome"]: row for row in compact["watch_buckets"]}
    assert watch["30°C"]["q_bayes_posterior"] == 1.0
    assert watch["31°C"]["q_bayes_posterior"] == 0.0
    assert watch["31°C"]["q_bayes_posterior_pre_ssot_display_alignment"] == pytest.approx(0.039)


def test_ui_snapshot_distribution_hard_zero_syncs_q_bayes_posterior() -> None:
    row = {
        "outcome": "27°C or below",
        "q_raw": 0.0,
        "q_round": 0.0,
        "q_temperature_bucket_posterior": 0.0,
        "q_bayes_posterior": 0.057996519731710196,
        "posterior_settlement_candidate_bucket_hard_zero": True,
        "posterior_settlement_candidate_bucket_hard_zero_reasons": [
            "observed_today_max_above_bucket",
            "hard_source_lower_bound_constraint",
        ],
    }

    compact = server._compact_distribution_row_for_ui_snapshot(row, summary_minimal=True)

    assert compact["q_raw"] == 0.0
    assert compact["q_temperature_bucket_posterior"] == 0.0
    assert compact["q_bayes_posterior"] == 0.0
    assert compact["q_bayes_posterior_no"] == 1.0
    assert compact["q_bayes_posterior_pre_hard_source_override"] == 0.057996519731710196
    assert compact["q_bayes_posterior_hard_zero_synced"] is True


def test_ui_snapshot_distribution_syncs_display_bayes_to_temperature_ssot() -> None:
    row = {
        "outcome": "30°C",
        "q_temperature_bucket_posterior": 1.0,
        "q_bayes_posterior": 0.5456630872628443,
        "q_contract_payout_posterior": 1.0,
    }

    compact = server._compact_distribution_row_for_ui_snapshot(row, summary_minimal=True)

    assert compact["q_temperature_bucket_posterior"] == 1.0
    assert compact["q_temperature_bucket_posterior_pct"] == 100.0
    assert compact["q_bayes_posterior"] == 1.0
    assert compact["q_bayes_posterior_pct"] == 100.0
    assert compact["q_bayes_posterior_pre_ssot_display_alignment"] == pytest.approx(0.5456630872628443)
    assert compact["posterior_ssot_display_aligned"] is True


def test_ui_snapshot_summary_slim_keeps_awc_hard_observation_fields() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-25-2026",
        "event_date": "2026-06-25",
        "key_metrics": {
            "observed_today_max_c": 27.0,
            "zuuu_metar_raw_awc_latest_temp_c": 25.0,
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 250800Z VRB02MPS 9999 SCT026 25/20 Q1010 NOSIG",
            "zuuu_metar_raw_awc_today_max_c": 27.0,
            "zuuu_metar_raw_awc_today_max_raw": "METAR ZUUU 250400Z VRB02MPS 9999 SCT026 27/20 Q1010 NOSIG",
        },
        "weather_likelihood_features": {
            "observed_today_max_c": 27.0,
            "zuuu_metar_raw_awc_latest_temp_c": 25.0,
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 250800Z VRB02MPS 9999 SCT026 25/20 Q1010 NOSIG",
            "zuuu_metar_raw_awc_today_max_c": 27.0,
            "zuuu_metar_raw_awc_today_max_raw": "METAR ZUUU 250400Z VRB02MPS 9999 SCT026 27/20 Q1010 NOSIG",
        },
        "source_observation_comparison": {
            "awc": {
                "today_max_c": 27.0,
                "settlement_proxy_bucket": "27°C",
            },
            "wu_twc": {
                "today_max_c": 25.0,
                "settlement_proxy_bucket": "25°C",
            },
        },
        "target_bucket_observation_state": {
            "source": "zuuu_metar_raw_awc_today_max",
            "observed_max_c": 27.0,
            "settlement_proxy_bucket": "27°C",
        },
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert slim["key_metrics"]["zuuu_metar_raw_awc_today_max_c"] == 27.0
    assert (
        slim["weather_likelihood_features_compact"]["zuuu_metar_raw_awc_latest_raw"]
        == "METAR ZUUU 250800Z VRB02MPS 9999 SCT026 25/20 Q1010 NOSIG"
    )
    assert (
        slim["weather_likelihood_features_compact"]["zuuu_metar_raw_awc_today_max_raw"]
        == "METAR ZUUU 250400Z VRB02MPS 9999 SCT026 27/20 Q1010 NOSIG"
    )
    assert slim["source_observation_comparison"]["awc"]["today_max_c"] == 27.0
    assert slim["target_bucket_observation_state"]["observed_max_c"] == 27.0


def test_ui_snapshot_summary_slim_promotes_nested_chengdu_awc_observation_fields() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-25-2026",
        "title": "Highest temperature in Chengdu on June 25?",
        "city": "ZUUU",
        "station_id": "ZUUU",
        "event_date": "2026-06-25",
        "weather_likelihood_features_compact": {
            "observed_today_max_c": 27.0,
            "zuuu_metar_raw_awc_today_max_c": 27.0,
        },
        "chengdu_intraday_summary": {
            "ok": True,
            "event_date": "2026-06-25",
            "source_observation_comparison": {
                "awc": {
                    "today_max_c": 27.0,
                    "current_temp_c": 25.0,
                    "settlement_proxy_bucket": "27°C",
                }
            },
            "target_bucket_observation_state": {
                "source": "zuuu_metar_raw_awc_today_max",
                "observed_max_c": 27.0,
                "settlement_proxy_bucket": "27°C",
            },
            "key_metrics": {
                "observed_today_max_c": 27.0,
                "zuuu_metar_raw_awc_latest_temp_c": 25.0,
                "zuuu_metar_raw_awc_today_max_c": 27.0,
            },
        },
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert slim["chengdu_intraday_summary"]["source_observation_comparison"]["awc"]["today_max_c"] == 27.0
    assert slim["source_observation_comparison"]["awc"]["today_max_c"] == 27.0
    assert slim["key_metrics"]["zuuu_metar_raw_awc_today_max_c"] == 27.0
    assert slim["target_bucket_observation_state"]["observed_max_c"] == 27.0


def test_event_fact_summary_cache_promotes_nested_chengdu_awc_observation(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    monkeypatch.setattr(
        server,
        "_attach_chengdu_intraday_summary_fields_for_fact_write",
        lambda *_args, **_kwargs: None,
    )
    item = {
        "slug": "highest-temperature-in-chengdu-on-june-25-2026",
        "title": "Highest temperature in Chengdu on June 25?",
        "city": "ZUUU",
        "station_id": "ZUUU",
        "event_date": "2026-06-25",
        "run_date": "2026-06-25",
        "market_var": "temperature_2m_max",
        "rounding_rule": "whole_degrees",
        "chengdu_intraday_summary": {
            "ok": True,
            "event_date": "2026-06-25",
            "target_bucket": "27°C",
            "source_observation_comparison": {
                "awc": {
                    "today_max_c": 27.0,
                    "settlement_proxy_bucket": "27°C",
                }
            },
            "target_bucket_observation_state": {
                "source": "zuuu_metar_raw_awc_today_max",
                "observed_max_c": 27.0,
                "settlement_proxy_bucket": "27°C",
            },
            "key_metrics": {
                "observed_today_max_c": 27.0,
                "zuuu_metar_raw_awc_today_max_c": 27.0,
            },
        },
    }

    ok = server._upsert_event_fact_summary_cache(
        conn,
        fact_id=1,
        run_date="2026-06-25",
        slug="highest-temperature-in-chengdu-on-june-25-2026",
        event_date="2026-06-25",
        fact_as_of="2026-06-25T12:00:00+00:00",
        item=item,
    )

    assert ok is True
    row = conn.execute(
        "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = 1"
    ).fetchone()
    summary = json.loads(row["summary_json"])
    assert summary["chengdu_intraday_summary"]["source_observation_comparison"]["awc"]["today_max_c"] == 27.0
    assert summary["source_observation_comparison"]["awc"]["today_max_c"] == 27.0
    assert summary["target_bucket_observation_state"]["observed_max_c"] == 27.0
    assert summary["key_metrics"]["zuuu_metar_raw_awc_today_max_c"] == 27.0


def test_chengdu_intraday_nonblocking_returns_compact_cached_payload_by_default(monkeypatch) -> None:
    key = "2099-01-06"
    monkeypatch.setattr(server, "CHENGDU_INTRADAY_SUMMARY_CACHE_SEC", 60)
    with server._CHENGDU_INTRADAY_SUMMARY_CACHE_LOCK:
        server._CHENGDU_INTRADAY_SUMMARY_CACHE[key] = (
            time.time(),
            {
                "ok": True,
                "event_date": key,
                "target_bucket": "30°C",
                "bucket_probabilities": [
                    {
                        "outcome": "30°C",
                        "q_bayes_posterior_pct": 31.0,
                        "bayes_chain": {"large": "detail"},
                    }
                ],
            },
        )
    try:
        compact = server.latest_chengdu_intraday_summary_nonblocking(key)
        full = server.latest_chengdu_intraday_summary_nonblocking(key, compact=False)
    finally:
        with server._CHENGDU_INTRADAY_SUMMARY_CACHE_LOCK:
            server._CHENGDU_INTRADAY_SUMMARY_CACHE.pop(key, None)

    assert compact["summary_compact"] is True
    assert "bayes_chain" not in compact["bucket_probabilities"][0]
    assert full["bucket_probabilities"][0]["bayes_chain"] == {"large": "detail"}


def test_chengdu_intraday_fast_fallback_overlays_latest_fact_observation(monkeypatch) -> None:
    event_date = "2099-01-07"
    latest_fact = {
        "event_date": event_date,
        "weather_likelihood_features_compact": {
            "observed_temp_c": 33.0,
            "observed_today_max_c": 33.0,
            "peak_estimator_mu_c": 33.0,
            "peak_estimator_sigma_c": 0.3,
        },
        "key_metrics": {
            "observed_temp_c": 33.0,
            "observed_today_max_c": 33.0,
        },
        "temperature_trajectory_status": {
            "available": True,
            "observed_temp_c": 33.0,
            "observed_today_max_c": 33.0,
            "temperature_rise_rate_c_per_hour": 0.0,
        },
        "temperature_path_distribution": {
            "observed_temp_c": 33.0,
            "observed_today_max_c": 33.0,
            "peak_estimator_mu_c": 33.1,
            "peak_estimator_sigma_c": 0.3,
        },
        "distribution": [
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.04},
            {"outcome": "33°C", "q_temperature_bucket_posterior": 0.94},
        ],
    }
    monkeypatch.setattr(server, "_latest_fact_payload_for_chengdu_intraday_ssot", lambda _date: latest_fact)

    payload = server._fact_backed_chengdu_intraday_summary_fallback(
        event_date,
        {"event_date": event_date, "busy": True, "error": "summary_build_started"},
    )
    compact = server._compact_chengdu_intraday_summary_for_endpoint(payload)

    assert compact["target_bucket"] == "33°C"
    assert compact["temperature_trajectory_status"]["observed_temp_c"] == 33.0
    assert compact["temperature_trajectory_status"]["observed_today_max_c"] == 33.0
    assert compact["temperature_path_distribution"]["peak_estimator_mu_c"] == 33.1
    assert payload["weather_likelihood_features_compact"]["observed_today_max_c"] == 33.0


def test_source_peak_model_rows_read_from_single_distribution_helper() -> None:
    html = (ROOT / "ui" / "index.html").read_text()
    assert "function sourcePeakDistributionRows(item)" in html
    assert "if (Array.isArray(item.rows)) return item.rows;" in html
    assert "if (payload && Array.isArray(payload.rows)) return payload.rows;" in html
    source_peak_block = html[
        html.index("function sourcePeakDistributionRows(item)") : html.index("async function loadStationTemp(item, force)")
    ]
    assert "const dist = item && Array.isArray(item.distribution) ? item.distribution : [];" not in source_peak_block
    assert "function sourcePeakModelValue(item, modelKey) {\n      const dist = sourcePeakDistributionRows(item);" in html
    assert "function sourcePeakModelTopBucket(item, modelKey) {\n      const dist = sourcePeakDistributionRows(item);" in html
    assert "function renderSourcePeakCurveComparison(item, stationData, sourceRows = null)" in html
    assert "最高温曲线对比" in source_peak_block
    assert "const visibleGroupText = Array.from(new Set(coords.map((point) => point.group).filter(Boolean))).join(' / ');" in source_peak_block
    assert "renderSourcePeakCurveComparison(item, stationData, rows)" in source_peak_block


def test_event_header_highlights_today_max_and_twc_wu_peak_hours() -> None:
    html = (ROOT / "ui" / "index.html").read_text()
    assert "function resolveEventPeakHourSchedule(item, nowMs = Date.now())" in html
    assert "function eventPeakWholeHourEntries(rawHour)" in html
    assert "function eventPeakWindowHourEntries(startHour, endHour)" in html
    assert "function eventPeakWindowEntriesAroundHour(rawHour)" in html
    assert "'peak_window_start_hour_local', 'expected_peak_window_start_hour_local'" in html
    assert "'peak_window_end_hour_local', 'expected_peak_window_end_hour_local'" in html
    assert "eventPeakWindowEntriesAroundHour(rawHour)" in html
    assert "eventPeakWindowEntriesAroundHour(fallbackHour)" in html
    assert "'weather_com_forecast_peak_at', 'twc_forecast_peak_at'" in html
    assert "'wu_forecast_peak_at', 'wu_forecast_peak_valid_local'" in html
    assert "最高温按多整点窗口展示，窗口结束前不判定已过" in html
    assert "按结算源 TWC/WU 每日预估整点，不压成单一时点" in html
    assert "class=\"event-info-hero-block is-max\"" in html
    assert "class=\"event-info-hero-block is-window\"" in html
    assert "class=\"event-info-hero-block is-countdown\"" in html
    assert "id=\"hero-countdown-value-${cardId}\"" in html
    assert "id=\"peak-hour-chips-${cardId}\"" in html
    assert "bindEventCountdown(heroCountdownValueEl, heroCountdownTargetEl, null, peakHourChipsEl)" in html
    assert "renderEventPeakHourChips(entry.item)" in html
    countdown_block = html[
        html.index("function formatCountdownParts(item)") : html.index("function formatCountdownLabel(item)")
    ]
    assert countdown_block.index("resolveEventPeakHourSchedule(item, nowMs)") < countdown_block.index("resolvePeakTimeIso(item)")


def test_event_display_order_sinks_past_peak_window_from_compact_schedule() -> None:
    html = (ROOT / "ui" / "index.html").read_text()
    start = html.index("function eventPeakWindowEndMs")
    end = html.index("function countdownSortKey", start)
    block = html[start:end]
    countdown_start = html.index("function countdownSortKey", start)
    countdown_end = html.index("function parseDateOnlyLocal", countdown_start)
    countdown_block = html[countdown_start:countdown_end]

    assert "function eventPeakWindowEndMs(item, nowMs = Date.now())" in block
    assert "const schedule = resolveEventPeakHourSchedule(item, nowMs);" in block
    assert "return Math.max(...candidateTimes);" in block
    assert "const endMs = eventPeakWindowEndMs(item, nowMs);" in block
    assert "if (aPastPeak !== bPastPeak) return aPastPeak - bPastPeak;" in block
    assert "return aPastPeak && bPastPeak ? -dateOrder : dateOrder;" in block
    assert "const schedule = resolveEventPeakHourSchedule(item, nowMs);" in countdown_block
    assert "schedule.next && Number.isFinite(schedule.next.targetMs)" in countdown_block
    assert "return Math.max(0, schedule.next.targetMs - nowMs);" in countdown_block
    assert countdown_block.index("resolveEventPeakHourSchedule(item, nowMs)") < countdown_block.index("resolvePeakTimeIso(item)")


def test_target_switch_explanation_includes_physical_component_direction() -> None:
    html = (ROOT / "ui" / "index.html").read_text()
    assert "function buildTargetSwitchPhysicalImpactData(payload = {}, item = {})" in html
    assert "trajectory_momentum_impact" in html
    assert "backendImpactSummary" in html
    assert "const badgeLabel = backendImpactLabel || fallbackBadgeLabel;" in html
    assert "const displayImpactSummary = impact.ssotImpactSummary || impact.tradePush || displayImpactTitle;" in html
    assert "const displayTradePush = data.ssotImpactSummary || data.tradePush;" in html
    assert "const ssotSwitchSummary = String(switchImpact.summary || '').trim();" in html
    assert "物理证据推动目标" in html
    assert "推高目标 / 支持上切" in html
    assert "压低目标 / 压制上切" in html
    assert "renderTargetSwitchPhysicalImpact(impact)" in html


def test_chengdu_anomaly_alert_groups_include_bucket_labels(tmp_path, monkeypatch) -> None:
    report_dir = tmp_path / "weather_anomaly"
    report_dir.mkdir()
    alerts_path = report_dir / "chengdu_weather_market_alerts_20990101T000000Z.json"
    alerts_path.write_text(
        json.dumps(
            {
                "generated_at": "2099-01-01T00:00:00Z",
                "alerts": [
                    {
                        "alert_type": "wallet_concentration",
                        "label": "26°C",
                        "severity": "high",
                        "risk_score": 70,
                        "market_mid": 0.42,
                        "market_model_gap": 0.08,
                        "recent_trade_windows": {"30m": {"total_notional": 120}},
                        "wallet_concentration": {"recent_30m_top3_wallet_share": 0.66},
                    },
                    {
                        "alert_type": "pressure_accumulation",
                        "label": "27°C",
                        "severity": "medium",
                        "risk_score": 50,
                    },
                    {
                        "alert_type": "pump_or_inducement",
                        "label": "25°C or below",
                        "severity": "medium",
                        "risk_score": 48,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "WEATHER_ANOMALY_REPORT_DIR", report_dir)

    payload = server.latest_chengdu_anomaly_alerts()
    groups = {group["alert_type"]: group for group in payload["grouped_alerts"]}

    assert groups["wallet_concentration"]["count"] == 1
    assert groups["wallet_concentration"]["buckets"] == ["26°C"]
    assert groups["wallet_concentration"]["alerts"][0]["compact_line"] == (
        "26°C · 钱包集中成交 · risk 70.0 · mid 42.0% · gap +8.0pp · 30m成交 120 · top3 66.0%"
    )
    assert groups["wallet_concentration"]["compact_summary"].startswith("26°C · 钱包集中成交")
    assert groups["pressure_accumulation"]["bucket_summary"] == "27°C"
    assert groups["pump_or_inducement"]["label_with_buckets"] == "诱多拉价:25°C or below"


def test_chengdu_anomaly_frontend_group_summary_uses_group_buckets() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    assert "function anomalyGroupBrief(group)" in html
    assert "function anomalyGroupBuckets(group, limit = 4)" in html
    assert "compact_line" in html
    assert "chengdu-anomaly-compact-line" in html
    assert '<details class="chengdu-anomaly-detail">' in html
    assert "const groupBrief = groups.slice(0, 3).map(anomalyGroupBrief).join('；');" in html
    assert "topAlert.outcome || topAlert.bucket" not in html


def test_ui_snapshot_summary_serves_stale_cache_during_rebuild(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "UI_SNAPSHOT_DIRTY_STAMP_PATH", tmp_path / "dirty.stamp")
    cache_key = "summary:2099-01-01:24"
    cached_payload = {
        "ok": True,
        "summary_mode": True,
        "run_date": "2099-01-01",
        "facts": {"items": [{"slug": "cached"}], "total": 1},
    }
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE[cache_key] = (time.time() - 999.0, cached_payload)
    with server._UI_SNAPSHOT_REBUILD_LOCK:
        mark_ui_snapshot_rebuilding(cache_key)
    try:
        payload = server.ui_snapshot_summary("2099-01-01", 5)
    finally:
        with server._UI_SNAPSHOT_REBUILD_LOCK:
            server._UI_SNAPSHOT_REBUILDING.pop(cache_key, None)
        with server._UI_SNAPSHOT_CACHE_LOCK:
            server._UI_SNAPSHOT_CACHE.pop(cache_key, None)

    assert payload["ok"] is True
    assert payload["cache_hit"] is True
    assert payload["cache_stale"] is True
    assert payload["cache_rebuild_in_flight"] is True
    assert payload["cache_rebuild_status"]["active"] is True
    assert payload["cache_rebuild_elapsed_ms"] >= 0
    assert payload["facts"]["items"][0]["slug"] == "cached"


def test_ui_snapshot_summary_dirty_cache_uses_fallback_during_rebuild(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(server, "DB_PATH", db_path)
    dirty_stamp = tmp_path / "dirty.stamp"
    monkeypatch.setattr(server, "UI_SNAPSHOT_DIRTY_STAMP_PATH", dirty_stamp)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 30)
    monkeypatch.setattr(server, "UI_SNAPSHOT_STALE_WHILE_REVALIDATE", True)
    monkeypatch.setattr(
        server,
        "_ui_snapshot_summary_metadata_fallback_payload",
        lambda **_kwargs: {
            "ok": True,
            "busy": True,
            "summary_cache_fallback": True,
            "facts": {"items": [{"slug": "fallback_latest"}], "total": 1, "page_size": 5},
        },
    )
    cache_key = "summary:2099-01-04:24"
    cached_payload = {
        "ok": True,
        "summary_mode": True,
        "run_date": "2099-01-04",
        "facts": {"items": [{"slug": "cached_old"}], "total": 1, "page_size": 5},
    }
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE[cache_key] = (time.time() - 20.0, cached_payload)
    dirty_stamp.write_text("new fact", encoding="utf-8")
    with server._UI_SNAPSHOT_REBUILD_LOCK:
        mark_ui_snapshot_rebuilding(cache_key)
    try:
        payload = server.ui_snapshot_summary("2099-01-04", 5)
    finally:
        with server._UI_SNAPSHOT_REBUILD_LOCK:
            server._UI_SNAPSHOT_REBUILDING.pop(cache_key, None)
        with server._UI_SNAPSHOT_CACHE_LOCK:
            server._UI_SNAPSHOT_CACHE.pop(cache_key, None)

    assert payload["ok"] is True
    assert payload["summary_cache_fallback"] is True
    assert payload["facts"]["items"][0]["slug"] == "fallback_latest"


def test_ui_snapshot_summary_throttles_repeated_stale_revalidate(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_DIRTY_STAMP_PATH", tmp_path / "dirty.stamp")
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 1)
    monkeypatch.setattr(server, "UI_SNAPSHOT_STALE_WHILE_REVALIDATE", True)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_REVALIDATE_MIN_INTERVAL_SEC", 60)
    monkeypatch.setattr(
        server,
        "_ui_snapshot_rebuild_start",
        lambda _cache_key: (_ for _ in ()).throw(AssertionError("rebuild should be throttled")),
    )
    cache_key = "summary:2099-01-03:24"
    cached_payload = {
        "ok": True,
        "summary_mode": True,
        "run_date": "2099-01-03",
        "facts": {"items": [{"slug": "cached"}], "total": 1, "page_size": 5},
    }
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE[cache_key] = (time.time() - 10.0, cached_payload)
    with server._UI_SNAPSHOT_REVALIDATE_LOCK:
        server._UI_SNAPSHOT_REVALIDATE_LAST[cache_key] = time.time()
    try:
        payload = server.ui_snapshot_summary("2099-01-03", 5)
    finally:
        with server._UI_SNAPSHOT_CACHE_LOCK:
            server._UI_SNAPSHOT_CACHE.pop(cache_key, None)
        with server._UI_SNAPSHOT_REVALIDATE_LOCK:
            server._UI_SNAPSHOT_REVALIDATE_LAST.pop(cache_key, None)

    assert payload["ok"] is True
    assert payload["cache_hit"] is True
    assert payload["cache_stale"] is True
    assert payload["cache_stale_reason"] == "summary_revalidate_throttled"
    assert payload["facts"]["items"][0]["slug"] == "cached"


def test_hard_source_final_lock_is_promoted_to_fact_fields(monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED", "1")
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "0")
    item = {
        "distribution": [
            {"outcome": "32°C", "q_bayes_posterior": 0.25, "q_round": 0.25},
            {"outcome": "33°C", "q_bayes_posterior": 0.50, "q_round": 0.50},
            {"outcome": "34°C", "q_bayes_posterior": 0.25, "q_round": 0.25},
        ],
        "weather_likelihood_features": {},
    }
    features = {
        "settlement_actual": 33.0,
        "settlement_actual_source": "event_runs.settlement_actual",
        "market_closed": True,
    }

    meta = server._apply_hard_source_final_lock_to_item(
        item,
        features,
        event_date="2026-06-22",
        market_var="temperature_2m_max",
        rounding_rule="whole_degrees",
        event_tz="Asia/Shanghai",
    )

    assert meta["final_bucket_lock"] is True
    assert item["final_bucket_lock"] is True
    assert item["final_bucket"] == "33°C"
    assert item["hard_source_final_bucket_lock"] is True
    assert item["settlement_actual"] == 33.0
    assert item["market_closed"] == 1
    assert item["weather_likelihood_features"]["final_bucket_lock"] is True
    assert item["weather_likelihood_features"]["final_bucket"] == "33°C"
    probs = {row["outcome"]: row["q_bayes_posterior"] for row in item["distribution"]}
    assert probs == {"32°C": 0.0, "33°C": 1.0, "34°C": 0.0}


def test_ui_snapshot_summary_cold_cache_rebuilds_in_background(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    sqlite3.connect(db_path).close()
    cache_key = "summary:2099-01-02:24"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 30)
    monkeypatch.setattr(server, "UI_SNAPSHOT_STALE_WHILE_REVALIDATE", True)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_SYNC_COLD_EXPLICIT_DATE", False)
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.pop(cache_key, None)
    with server._UI_SNAPSHOT_REBUILD_LOCK:
        server._UI_SNAPSHOT_REBUILDING.pop(cache_key, None)
    try:
        payload = server.ui_snapshot_summary("2099-01-02", 5)
        assert payload["ok"] is True
        assert payload["busy"] is True
        assert payload["cache_rebuild_in_flight"] is True
        assert payload["facts"]["items"] == []
    finally:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with server._UI_SNAPSHOT_REBUILD_LOCK:
                active = cache_key in server._UI_SNAPSHOT_REBUILDING
            if not active:
                break
            time.sleep(0.02)
        with server._UI_SNAPSHOT_REBUILD_LOCK:
            server._UI_SNAPSHOT_REBUILDING.pop(cache_key, None)
        with server._UI_SNAPSHOT_CACHE_LOCK:
            server._UI_SNAPSHOT_CACHE.pop(cache_key, None)


def test_ui_snapshot_summary_busy_cold_cache_returns_metadata_only_items(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 30)
    monkeypatch.setattr(server, "UI_SNAPSHOT_STALE_WHILE_REVALIDATE", True)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_SYNC_COLD_EXPLICIT_DATE", False)
    monkeypatch.setattr(server, "UI_SNAPSHOT_DIRTY_STAMP_PATH", tmp_path / "dirty.stamp")
    monkeypatch.setattr(server, "_safe_model_update_stats", lambda limit=48: {"models": []})
    cache_key = "summary:2026-06-21:24"
    slug = "highest-temperature-in-chengdu-on-june-21-2026"
    payload = {
        "slug": slug,
        "title": "Highest temperature in Chengdu on June 21?",
        "event_date": "2026-06-21",
        "run_date": "2026-06-21",
        "market_var": "temperature_2m_max",
        "distribution": [{"outcome": "25°C", "q_round": 0.4}],
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    conn.execute(
        """
        CREATE TABLE weather_market_discovery (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            title TEXT,
            market_open_at TEXT,
            first_seen_at TEXT NOT NULL,
            first_tokenized_at TEXT,
            token_count INTEGER NOT NULL DEFAULT 0,
            status TEXT,
            details_json TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(slug, event_date)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO weather_market_discovery(
            slug, event_date, title, first_seen_at, first_tokenized_at, token_count, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slug,
            "2026-06-21",
            "Highest temperature in Chengdu on June 21?",
            "2026-06-20T00:00:00+00:00",
            "2026-06-20T01:00:00+00:00",
            11,
            "tokenized",
            "2026-06-20T01:00:00+00:00",
        ),
    )
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-21",
        slug=slug,
        event_date="2026-06-21",
        fact_as_of="2026-06-21T04:00:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-21", fact_id)
    conn.execute("DELETE FROM event_fact_summary_cache WHERE fact_id = ?", (fact_id,))
    conn.commit()
    conn.close()
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.pop(cache_key, None)
    with server._UI_SNAPSHOT_REBUILD_LOCK:
        mark_ui_snapshot_rebuilding(cache_key)
    try:
        result = server.ui_snapshot_summary("2026-06-21", 5)
    finally:
        with server._UI_SNAPSHOT_REBUILD_LOCK:
            server._UI_SNAPSHOT_REBUILDING.pop(cache_key, None)
        with server._UI_SNAPSHOT_CACHE_LOCK:
            server._UI_SNAPSHOT_CACHE.pop(cache_key, None)

    assert result["ok"] is True
    assert result["busy"] is True
    assert result["summary_metadata_only"] is True
    assert result["facts"]["source"] == "event_fact_latest_summary_metadata_only"
    item = result["facts"]["items"][0]
    assert item["slug"] == slug
    assert item["title"] == "Highest temperature in Chengdu on June 21?"
    assert item["summary_metadata_only"] is True
    assert item["snapshot_source"] == "event_fact_latest_metadata_only"
    assert item["detail_available"] is False
    assert item["frontend_inference_allowed"] is False
    assert item["detail_na_reason"] == "ui_snapshot_rebuild_in_flight"
    assert item["ssot_detail_state"]["state"] == "metadata_only"
    assert item["ssot_detail_state"]["frontend_behavior"] == "placeholder_only"
    assert item["ssot_detail_state"]["frontend_inference_allowed"] is False
    assert item["data_status"]["status"] == "metadata_only"
    assert item["data_status"]["frontend_inference_allowed"] is False
    assert item["market_discovery"]["token_count"] == 11
    assert "distribution" not in item
    assert "target_bucket" not in item


def test_ui_snapshot_summary_heavy_lane_busy_serves_summary_cache(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "_event_fact_metadata_only_summary_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(server, "_discovery_placeholder_payloads", lambda *args, **kwargs: [])
    slug = "highest-temperature-in-chengdu-on-june-22-2026"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-22",
        slug=slug,
        event_date="2026-06-22",
        fact_as_of="2026-06-22T08:08:01+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "title": "Highest temperature in Chengdu on June 22?",
            "event_date": "2026-06-22",
            "run_date": "2026-06-22",
            "market_var": "temperature_2m_max",
            "distribution": [{"outcome": "33°C", "q_bayes_posterior": 0.65, "q_round": 0.65}],
        },
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-22", fact_id)
    conn.commit()
    conn.close()

    payload = server._ui_snapshot_summary_heavy_lane_fallback_payload(
        "/api/ui_snapshot_summary?run_date=2026-06-22&limit=5&city=chengdu"
    )

    assert payload is not None
    assert payload["ok"] is True
    assert payload["busy"] is True
    assert payload["heavy_lane_busy"] is True
    assert payload["source"] == "ui_heavy_request_lane_busy_summary_cache_fallback"
    assert payload["summary_cache_fallback"] is True
    assert payload["summary_cache_fallback_reason"] == "ui_heavy_request_lane_busy"
    assert payload["facts"]["items"][0]["slug"] == slug
    assert payload["facts"]["items"][0]["distribution"][0]["outcome"] == "33°C"
    cached_payload = server._ui_snapshot_summary_heavy_lane_fallback_payload(
        "/api/ui_snapshot_summary?run_date=2026-06-22&limit=5&city=chengdu"
    )
    assert cached_payload is not None
    assert cached_payload["source"] == "ui_snapshot_summary_prefer_cache_memory"
    assert cached_payload["prefer_cache_memory"] is True
    assert server._ui_snapshot_summary_prefer_cache_request_target(
        "/api/ui_snapshot_summary?run_date=2026-06-22&prefer_cache=1"
    )
    assert server._ui_snapshot_summary_prefer_cache_request_target(
        "/api/ui_snapshot?run_date=2026-06-22&prefer_cache=true"
    )
    assert not server._ui_snapshot_summary_prefer_cache_request_target(
        "/api/ui_snapshot_summary?run_date=2026-06-22"
    )
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.pop("summary:2026-06-22:24:chengdu", None)


def test_ui_server_routes_prefer_cache_snapshot_outside_heavy_lane() -> None:
    source = inspect.getsource(server.BoundedThreadingHTTPServer.process_request)
    thread_source = inspect.getsource(server.BoundedThreadingHTTPServer.process_request_thread)
    busy_source = inspect.getsource(server.BoundedThreadingHTTPServer._send_api_busy_and_close)

    assert "prefer_cache_request = _ui_snapshot_summary_prefer_cache_request_target(request_target)" in source
    assert "_ui_snapshot_summary_prefer_cache_raw_response(request_target)" in source
    assert 'self._bypass_slot_requests[id(request)] = "prefer_cache"' in source
    assert 'elif bypass_slot == "prefer_cache":' in thread_source
    assert "priority_request = request_path in UI_PRIORITY_REQUEST_PATHS or prefer_cache_request" in source
    assert "heavy_request = request_path in UI_HEAVY_REQUEST_PATHS and not prefer_cache_request" in source
    assert "_ui_snapshot_summary_heavy_lane_fallback_payload" in busy_source
    assert "_send_api_busy_and_close(request, request_target or request_path)" in source


def test_ui_snapshot_summary_prewarm_targets_chengdu_shared_cache_key(monkeypatch) -> None:
    monkeypatch.setattr(server, "_chengdu_today_date_key", lambda: "2026-07-02")

    targets = server._ui_snapshot_summary_prewarm_request_targets("2026-07-02")

    assert targets
    assert targets[0] == "/api/ui_snapshot_summary?run_date=2026-07-02&limit=24&city=chengdu&prefer_cache=1"
    assert all("limit=24" in target for target in targets)
    assert all("city=chengdu" in target for target in targets)
    assert any("run_date=2026-07-01" in target for target in targets)
    assert any("run_date=2026-07-03" in target for target in targets)


def test_ui_snapshot_summary_prefer_cache_raw_response_reuses_short_cache(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_fallback(_target: str) -> dict:
        calls["n"] += 1
        return {"ok": True, "source": "unit", "facts": {"items": []}}

    monkeypatch.setattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload", fake_fallback)
    monkeypatch.setattr(server, "UI_SNAPSHOT_PREFER_CACHE_RAW_SEC", 30.0)
    with server._UI_SNAPSHOT_PREFER_CACHE_RAW_LOCK:
        server._UI_SNAPSHOT_PREFER_CACHE_RAW.clear()

    first = server._ui_snapshot_summary_prefer_cache_raw_response(
        "/api/ui_snapshot_summary?run_date=2026-07-02&limit=2&city=chengdu&prefer_cache=1&_=1"
    )
    second = server._ui_snapshot_summary_prefer_cache_raw_response(
        "/api/ui_snapshot_summary?run_date=2026-07-02&limit=2&city=chengdu&prefer_cache=1&_=2"
    )

    assert first == second
    assert b'"prefer_cache_requested":true' in first
    assert calls["n"] == 1
    with server._UI_SNAPSHOT_PREFER_CACHE_RAW_LOCK:
        server._UI_SNAPSHOT_PREFER_CACHE_RAW.clear()


def test_ui_snapshot_route_defines_city_filter_for_summary_fallback() -> None:
    source = inspect.getsource(server.Handler.do_GET)
    start = source.index('if path == "/api/ui_snapshot":')
    end = source.index('if path == "/api/london-follow-sim":', start)
    block = source[start:end]

    city_filter_idx = block.index("city_filter = str(")
    summary_arg_idx = block.index("city_filter=city_filter")
    assert city_filter_idx < summary_arg_idx
    assert 'params.get("city", [""])[0]' in block
    assert 'params.get("scope", [""])[0]' in block


def test_ui_server_launch_script_uses_module_entrypoint() -> None:
    script = (ROOT / "scripts" / "ui_server_forever.sh").read_text(encoding="utf-8")

    assert "-m ui.server" in script
    assert '"$ROOT/ui/server.py"' not in script
    assert 'load_env_file_vars_safe "$ROOT/.env" 1' in script
    assert 'PM_UI_ENV_PROXY_REACHABILITY_GUARD:-0' in script
    assert 'PM_UI_LOAD_POLY_ENV:-0' in script
    assert 'load_repo_env_file "$ROOT/.env"' not in script


def test_ui_snapshot_summary_busy_cold_cache_prefers_summary_cache(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 30)
    monkeypatch.setattr(server, "UI_SNAPSHOT_STALE_WHILE_REVALIDATE", True)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_SYNC_COLD_EXPLICIT_DATE", False)
    monkeypatch.setattr(server, "UI_SNAPSHOT_DIRTY_STAMP_PATH", tmp_path / "dirty.stamp")
    monkeypatch.setattr(server, "_ui_upcoming_days", lambda default=2: 2)
    cache_key = "summary:2026-06-22:24"
    slug = "highest-temperature-in-chengdu-on-june-22-2026"
    fact_payload = {
        "slug": slug,
        "title": "Highest temperature in Chengdu on June 22?",
        "event_date": "2026-06-22",
        "run_date": "2026-06-22",
        "market_var": "temperature_2m_max",
        "weather_likelihood_features": {
            "observed_temp_c": 33.0,
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_latest_temp_c": 33.0,
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 220800Z VRB02MPS 9999 SCT026 33/17 Q1008 NOSIG",
            "zuuu_metar_raw_awc_today_max_c": 33.0,
            "zuuu_metar_raw_awc_today_max_raw": "METAR ZUUU 220800Z VRB02MPS 9999 SCT026 33/17 Q1008 NOSIG",
        },
        "temperature_trajectory_status": {
            "available": True,
            "observed_temp_c": 33.0,
            "observed_today_max_c": 33.0,
            "temperature_rise_rate_c_per_hour": 0.0,
        },
        "temperature_path_distribution": {
            "observed_temp_c": 33.0,
            "observed_today_max_c": 33.0,
            "peak_estimator_mu_c": 33.1,
            "peak_estimator_sigma_c": 0.3,
        },
        "distribution": [
            {"outcome": "32°C", "q_bayes_posterior": 0.25, "q_round": 0.25},
            {"outcome": "33°C", "q_bayes_posterior": 0.65, "q_round": 0.65},
        ],
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    conn.execute(
        """
        CREATE TABLE weather_market_discovery (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            title TEXT,
            market_open_at TEXT,
            first_seen_at TEXT NOT NULL,
            first_tokenized_at TEXT,
            token_count INTEGER NOT NULL DEFAULT 0,
            status TEXT,
            details_json TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(slug, event_date)
        )
        """
    )
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-22",
        slug=slug,
        event_date="2026-06-22",
        fact_as_of="2026-06-22T08:08:01+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=fact_payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-22", fact_id)
    cur = conn.execute(
        "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?",
        (fact_id,),
    )
    cached_summary = json.loads(cur.fetchone()["summary_json"])
    cached_summary.pop("source_observation_comparison", None)
    cached_summary.pop("target_bucket_observation_state", None)
    cached_summary.pop("key_metrics", None)
    cached_summary["chengdu_intraday_summary"] = {
        "ok": True,
        "event_date": "2026-06-22",
        "source_observation_comparison": {
            "awc": {
                "today_max_c": 33.0,
                "settlement_proxy_bucket": "33°C",
            }
        },
        "target_bucket_observation_state": {
            "source": "zuuu_metar_raw_awc_today_max",
            "observed_max_c": 33.0,
            "settlement_proxy_bucket": "33°C",
        },
        "key_metrics": {
            "observed_temp_c": 31.0,
            "observed_today_max_c": 33.0,
            "zuuu_metar_raw_awc_today_max_c": 33.0,
        },
        "temperature_trajectory_status": {
            "available": True,
            "observed_temp_c": 31.0,
            "observed_today_max_c": 31.0,
            "temperature_rise_rate_c_per_hour": 0.0,
        },
        "temperature_path_distribution": {
            "observed_temp_c": 31.0,
            "observed_today_max_c": 31.0,
            "peak_estimator_mu_c": 31.3,
            "peak_estimator_sigma_c": 0.6,
        },
    }
    conn.execute(
        "UPDATE event_fact_summary_cache SET summary_json = ? WHERE fact_id = ?",
        (json.dumps(cached_summary, ensure_ascii=False), fact_id),
    )
    future_slug = "highest-temperature-in-chengdu-on-june-23-2026"
    future_payload = {
        "slug": future_slug,
        "title": "Highest temperature in Chengdu on June 23?",
        "event_date": "2026-06-23",
        "run_date": "2026-06-22",
        "market_var": "temperature_2m_max",
        "distribution": [{"outcome": "31°C", "q_round": 0.5}],
    }
    future_fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-22",
        slug=future_slug,
        event_date="2026-06-23",
        fact_as_of="2026-06-22T08:09:01+00:00",
        model_snapshot_id=11,
        market_snapshot_id=21,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=future_payload,
    )
    server._upsert_event_fact_latest(conn, future_slug, "2026-06-23", future_fact_id)
    conn.execute("DELETE FROM event_fact_summary_cache WHERE fact_id = ?", (future_fact_id,))
    conn.execute(
        """
        INSERT INTO weather_market_discovery(
            slug, event_date, title, first_seen_at, first_tokenized_at, token_count, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            future_slug,
            "2026-06-23",
            "Highest temperature in Chengdu on June 23?",
            "2026-06-22T00:00:00+00:00",
            "2026-06-22T01:00:00+00:00",
            11,
            "tokenized",
            "2026-06-22T01:00:00+00:00",
        ),
    )
    discovery_only_slug = "highest-temperature-in-chengdu-on-june-24-2026"
    conn.execute(
        """
        INSERT INTO weather_market_discovery(
            slug, event_date, title, first_seen_at, first_tokenized_at, token_count, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            discovery_only_slug,
            "2026-06-24",
            "Highest temperature in Chengdu on June 24?",
            "2026-06-22T00:00:00+00:00",
            "2026-06-22T01:05:00+00:00",
            11,
            "tokenized",
            "2026-06-22T01:05:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.pop(cache_key, None)
    with server._UI_SNAPSHOT_REBUILD_LOCK:
        mark_ui_snapshot_rebuilding(cache_key)
    try:
        result = server.ui_snapshot_summary("2026-06-22", 5)
    finally:
        with server._UI_SNAPSHOT_REBUILD_LOCK:
            server._UI_SNAPSHOT_REBUILDING.pop(cache_key, None)
        with server._UI_SNAPSHOT_CACHE_LOCK:
            server._UI_SNAPSHOT_CACHE.pop(cache_key, None)

    assert result["ok"] is True
    assert result["busy"] is True
    assert result["summary_cache_fallback"] is True
    assert result["facts"]["source"] == "event_fact_summary_cache_fast_fallback_with_metadata_and_discovery"
    assert result["summary_cache_fallback_fast"] is True
    assert result["summary_metadata_only_supplement"] is True
    assert result["summary_discovery_placeholder_supplement"] is True
    assert result.get("summary_metadata_only") is not True
    assert [entry["slug"] for entry in result["facts"]["items"]] == [slug, future_slug, discovery_only_slug]
    item = result["facts"]["items"][0]
    assert item["slug"] == slug
    assert item.get("summary_metadata_only") is not True
    assert item.get("detail_na_reason") != "ui_snapshot_rebuild_in_flight"
    assert item["distribution"][0]["outcome"] == "32°C"
    assert item["distribution"][1]["outcome"] == "33°C"
    assert item["chengdu_intraday_summary"]["source_observation_comparison"]["awc"]["today_max_c"] == 33.0
    assert item["source_observation_comparison"]["awc"]["today_max_c"] == 33.0
    assert item["target_bucket_observation_state"]["observed_max_c"] == 33.0
    assert item["key_metrics"]["zuuu_metar_raw_awc_today_max_c"] == 33.0
    assert item["summary_cache_dynamic_overlays_skipped"] is True
    assert item["temperature_trajectory_status"]["observed_temp_c"] == 31.0
    assert item["temperature_trajectory_status"]["observed_today_max_c"] == 31.0
    assert item["temperature_path_distribution"]["observed_temp_c"] == 31.0
    assert item["temperature_path_distribution"]["observed_today_max_c"] == 31.0
    assert item["chengdu_intraday_summary"]["temperature_trajectory_status"]["observed_temp_c"] == 31.0
    assert item["chengdu_intraday_summary"]["temperature_path_distribution"]["observed_temp_c"] == 31.0
    assert (
        item["weather_likelihood_features_compact"]["zuuu_metar_raw_awc_latest_raw"]
        == "METAR ZUUU 220800Z VRB02MPS 9999 SCT026 33/17 Q1008 NOSIG"
    )
    assert "fast_observation_overlay_source" not in item
    future_item = result["facts"]["items"][1]
    assert future_item["slug"] == future_slug
    assert future_item["event_date"] == "2026-06-23"
    assert future_item["summary_metadata_only"] is True
    assert future_item["snapshot_source"] == "event_fact_latest_metadata_only"
    assert future_item["frontend_inference_allowed"] is False
    discovery_item = result["facts"]["items"][2]
    assert discovery_item["slug"] == discovery_only_slug
    assert discovery_item["event_date"] == "2026-06-24"
    assert discovery_item["fact_provenance"]["market_discovery_placeholder"] is True
    assert discovery_item["data_status"] == "market_discovery_pending_model"


def test_ui_snapshot_summary_fast_fallback_aligns_stale_probability_copies(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    slug = "highest-temperature-in-chengdu-on-june-30-2026"
    item = {
        "slug": slug,
        "event_date": "2026-06-30",
        "run_date": "2026-06-29",
        "title": "Highest temperature in Chengdu on June 30?",
        "market_var": "temperature_2m_max",
        "distribution": [
            {
                "outcome": "29°C",
                "q_bayes_posterior": 0.3,
                "q_temperature_bucket_posterior": 0.3,
            },
            {
                "outcome": "30°C or higher",
                "q_bayes_posterior": 0.7,
                "q_temperature_bucket_posterior": 0.7,
            },
        ],
        "posterior_highest_bucket_label": "30°C or higher",
        "posterior_highest_probability_pct": 70.0,
        "target_bucket_selection": {
            "target_bucket": "30°C or higher",
            "target_bucket_probability_pct": 33.0,
            "target_row": {
                "outcome": "30°C or higher",
                "target_bucket_probability": 0.33,
            },
            "watch_buckets": [
                {"outcome": "30°C or higher", "target_bucket_probability": 0.33},
                {"outcome": "29°C", "target_bucket_probability": 0.67},
            ],
        },
        "temperature_path_distribution": {
            "target_bucket": "30°C or higher",
            "target_bucket_posterior": 0.33,
            "target": {"bucket": "30°C or higher", "posterior": 0.33},
            "bucket_distribution": [
                {"bucket": "29°C", "posterior": 0.67},
                {"bucket": "30°C or higher", "posterior": 0.33},
            ],
        },
        "chengdu_intraday_summary": {
            "target_bucket": "30°C or higher",
            "target_bucket_probability_pct": 33.0,
        },
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-29",
        slug=slug,
        event_date="2026-06-30",
        fact_as_of="2026-06-29T13:37:18+00:00",
        model_snapshot_id=1,
        market_snapshot_id=2,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-30", fact_id)
    conn.commit()
    cached_row = conn.execute(
        "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?",
        (fact_id,),
    ).fetchone()
    cached_summary = json.loads(cached_row["summary_json"])
    cached_summary["target_bucket_probability_pct"] = 33.0
    cached_summary["posterior_highest_probability_pct"] = 33.0
    cached_summary["target_bucket_selection"] = dict(item["target_bucket_selection"])
    cached_summary["posterior_target_bucket_selection"] = dict(item["target_bucket_selection"])
    cached_summary["temperature_path_distribution"] = dict(item["temperature_path_distribution"])
    cached_summary["chengdu_intraday_summary"] = dict(item["chengdu_intraday_summary"])
    conn.execute(
        "UPDATE event_fact_summary_cache SET summary_json = ? WHERE fact_id = ?",
        (json.dumps(cached_summary, ensure_ascii=False), fact_id),
    )
    conn.commit()

    out = server._event_fact_summary_cache_fast_fallback_items(
        conn,
        "2026-06-29",
        limit_val=5,
        upcoming_days=2,
    )
    conn.close()

    assert len(out) == 1
    result = out[0]
    assert result["target_bucket"] == "30°C or higher"
    assert result["target_bucket_probability_pct"] == 70.0
    assert result["distribution"][1]["target_bucket_probability"] == 0.7
    assert result["distribution"][1]["target_bucket_probability_pct"] == 70.0
    selection = result["target_bucket_selection"]
    assert selection["target_bucket_probability_pct"] == 70.0
    assert selection["target_row"]["target_bucket_probability"] == 0.7
    assert selection["target_row"]["target_bucket_probability_pct"] == 70.0
    assert selection["target_row"]["q_temperature_bucket_posterior"] == 0.7
    assert selection["watch_buckets"][0]["target_bucket_probability"] == 0.7
    assert selection["watch_buckets"][0]["target_bucket_probability_pct"] == 70.0
    assert selection["watch_buckets"][0]["q_temperature_bucket_posterior"] == 0.7
    path = result["temperature_path_distribution"]
    assert path["target_bucket_posterior"] == 0.7
    assert path["target"]["posterior"] == 0.7
    assert path["bucket_distribution"][1]["posterior"] == 0.7
    assert result["chengdu_intraday_summary"]["target_bucket_probability_pct"] == 70.0
    assert (
        result["chengdu_intraday_summary"]["temperature_path_distribution"]["target_bucket_posterior"]
        == 0.7
    )


def test_ui_snapshot_summary_empty_items_guard_uses_summary_cache(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 0)
    monkeypatch.setattr(server, "UI_SNAPSHOT_STALE_WHILE_REVALIDATE", False)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_FAST", True)
    monkeypatch.setattr(server, "_safe_model_update_stats", lambda limit=48: {"models": []})
    monkeypatch.setattr(server, "_event_fact_summary_cache_json_current", lambda _value: False)
    monkeypatch.setattr(server, "select_event_fact_rows_paged", lambda *args, **kwargs: [])
    slug = "highest-temperature-in-chengdu-on-june-29-2026"
    item = {
        "slug": slug,
        "event_date": "2026-06-29",
        "run_date": "2026-06-28",
        "title": "Highest temperature in Chengdu on June 29?",
        "market_var": "temperature_2m_max",
        "distribution": [
            {"outcome": "29°C", "q_bayes_posterior": 0.26, "q_round": 0.26},
            {"outcome": "30°C", "q_bayes_posterior": 0.31, "q_round": 0.31},
        ],
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-28",
        slug=slug,
        event_date="2026-06-29",
        fact_as_of="2026-06-28T08:40:57+00:00",
        model_snapshot_id=2087,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-29", fact_id)
    conn.commit()
    conn.close()
    cache_key = "summary:2026-06-28:24"
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.pop(cache_key, None)

    payload = server.ui_snapshot_summary("2026-06-28", 5)

    assert payload["ok"] is True
    assert payload["facts"]["source"] == "event_fact_summary_cache_empty_result_guard"
    assert payload["facts"]["items"][0]["slug"] == slug
    assert payload["facts"]["items"][0]["event_date"] == "2026-06-29"
    assert payload["facts"]["items"][0]["distribution"][1]["outcome"] == "30°C"


def test_frontend_metadata_only_items_render_placeholder_without_inference() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    assert "function isMetadataOnlySnapshotItem(item)" in html
    assert "function renderEventIntelMetadataOnlyPanel(item, title = '事实详情')" in html
    assert "frontend_inference_allowed === false" in html
    assert "placeholder_only" in html
    assert "if (isMetadataOnlySnapshotItem(item)) return renderEventIntelMetadataOnlyPanel(item, '证据流');" in html
    assert "if (isMetadataOnlySnapshotItem(item)) return '';" in html
    assert "if (isMetadataOnlySnapshotItem(entry.item))" in html
    assert "requestEventIntelChengduIntradayPayload(entry, {" in html
    request_pos = html.index("requestEventIntelChengduIntradayPayload(entry, {")
    metadata_block = html[
        html.index("if (isMetadataOnlySnapshotItem(entry.item))") : request_pos
    ]
    assert "renderEventIntelMetadataOnlyPanel(entry.item, '证据流')" in metadata_block
    assert "tradeCard.style.display = 'none'" in metadata_block
    assert "continue;" in metadata_block


def test_ui_snapshot_summary_metadata_only_path_does_not_select_fact_json() -> None:
    source = inspect.getsource(server._event_fact_metadata_only_summary_items)

    assert "fact_json" not in source
    assert "event_fact_latest" in source
    assert "event_fact_history" in source


def test_event_fact_summary_cache_written_and_used_without_fact_json(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-paris-on-june-20-2026"
    item = {
        "slug": slug,
        "event_date": "2026-06-20",
        "run_date": "2026-06-20",
        "title": "Highest temperature in Paris on June 20?",
        "market_var": "temperature_2m_max",
        "weather_likelihood_features_compact": {
            "observed_temp_c": 24.4,
            "observed_today_max_c": 28.0,
        },
        "distribution": [
            {"outcome": "25°C", "q_round": 0.70, "q_bayes_posterior": 0.70},
            {"outcome": "26°C", "q_round": 0.30, "q_bayes_posterior": 0.30},
        ],
    }
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-20",
        slug=slug,
        event_date="2026-06-20",
        fact_as_of="2026-06-20T05:00:00+00:00",
        model_snapshot_id=1,
        market_snapshot_id=2,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-20", fact_id)
    row = conn.execute(
        "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?",
        (fact_id,),
    ).fetchone()
    assert row is not None
    cached = json.loads(row["summary_json"])
    assert cached["slug"] == slug
    assert cached["summary_cache_version"] == server.EVENT_FACT_SUMMARY_CACHE_VERSION
    assert cached["weather_likelihood_features_compact"]["observed_temp_c"] == 24.4
    assert cached["distribution"][0]["outcome"] == "25°C"
    conn.commit()
    conn.close()

    def fail_full_fact_rows(*_args, **_kwargs):
        raise AssertionError("summary cache path should not select event_fact_history.fact_json")

    def fail_legacy_bucket_series(*_args, **_kwargs):
        raise AssertionError("bucket series should use event_fact_summary_cache when available")

    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 0)
    monkeypatch.setattr(server, "UI_SNAPSHOT_STALE_WHILE_REVALIDATE", False)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_FAST", True)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_BUCKET_SERIES_ENABLED", True)
    monkeypatch.setattr(server, "select_event_fact_rows_paged", fail_full_fact_rows)
    monkeypatch.setattr(server, "_summary_bucket_series_history_from_event_fact_history", fail_legacy_bucket_series)
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.pop("summary:2026-06-20:24", None)

    summary = server.ui_snapshot_summary("2026-06-20", 24)

    assert summary["ok"] is True
    assert summary["facts"]["source"] == "event_fact_summary_cache_with_upcoming"
    out = summary["facts"]["items"][0]
    assert out["slug"] == slug
    assert out["weather_likelihood_features_compact"]["observed_temp_c"] == 24.4
    assert out["distribution"][0]["outcome"] == "25°C"
    assert out["bucket_series_source"] == "event_fact_summary_cache_distribution_summary"
    assert out["bucket_series"]["25°C"][0]["q_posterior"] == 0.70
    assert server._event_fact_summary_cache_json_current(row["summary_json"])


def test_event_fact_summary_cache_rejects_unversioned_payload() -> None:
    assert not server._event_fact_summary_cache_json_current(
        json.dumps({"summary_cache_source": "event_fact_summary_cache"})
    )
    assert not server._event_fact_summary_cache_json_current(
        json.dumps(
            {
                "summary_cache_source": "event_fact_summary_cache",
                "summary_cache_version": "old_version",
            }
        )
    )
    assert server._event_fact_summary_cache_json_current(
        json.dumps(
            {
                "summary_cache_source": "event_fact_summary_cache",
                "summary_cache_version": server.EVENT_FACT_SUMMARY_CACHE_VERSION,
            }
        )
    )


def test_chengdu_intraday_summary_rejects_today_max_floor_as_current_observation() -> None:
    assert server._chengdu_intraday_summary_has_stale_current_observation(
        {"high_temp_amplification_guard": {"current_temp_source": "observed_today_max_floor"}}
    )
    assert server._chengdu_intraday_summary_has_stale_current_observation(
        {"warming_pace_assessment": {"observed_temp_source": "observed_today_max"}}
    )
    assert not server._chengdu_intraday_summary_has_stale_current_observation(
        {"high_temp_amplification_guard": {"current_temp_source": "zuuu_metar_raw_awc"}}
    )
    source = inspect.getsource(server._attach_chengdu_intraday_summary_fields_for_ui_snapshot)
    assert "existing_has_stale_current" in source
    assert "build_chengdu_intraday_summary_from_payload" in source
    assert "ui_snapshot.stale_current_refresh" in source


def test_ui_snapshot_summary_explicit_date_cold_cache_does_not_duplicate_active_rebuild(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-24-2026"
    item = {
        "slug": slug,
        "event_date": "2026-06-24",
        "run_date": "2026-06-23",
        "title": "Highest temperature in Chengdu on June 24?",
        "city": "Chengdu",
        "distribution": [{"outcome": "27°C", "q_bayes_posterior": 0.42, "q_round": 0.42}],
    }
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-23",
        slug=slug,
        event_date="2026-06-24",
        fact_as_of="2026-06-23T13:20:09+00:00",
        model_snapshot_id=1069,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-24", fact_id)
    conn.commit()
    conn.close()

    cache_key = "summary:2026-06-24:24"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 30)
    monkeypatch.setattr(server, "UI_SNAPSHOT_STALE_WHILE_REVALIDATE", True)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_SYNC_COLD_EXPLICIT_DATE", True)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_FAST", True)
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.pop(cache_key, None)
    with server._UI_SNAPSHOT_REBUILD_LOCK:
        mark_ui_snapshot_rebuilding(cache_key)
    try:
        payload = server.ui_snapshot_summary("2026-06-24", 5)
        assert payload["ok"] is True
        assert payload.get("busy") is True
        assert payload.get("cache_rebuild_in_flight") is True
        assert payload["summary_cache_fallback"] is True
        assert payload["facts"]["source"] == "event_fact_summary_cache_fast_fallback"
        assert payload["summary_cache_fallback_fast"] is True
        assert payload["facts"]["items"][0]["slug"] == slug
        assert payload["facts"]["items"][0]["distribution"][0]["outcome"] == "27°C"
    finally:
        with server._UI_SNAPSHOT_REBUILD_LOCK:
            server._UI_SNAPSHOT_REBUILDING.pop(cache_key, None)
        with server._UI_SNAPSHOT_CACHE_LOCK:
            server._UI_SNAPSHOT_CACHE.pop(cache_key, None)


def test_ui_snapshot_summary_attaches_bucket_series_history_for_posterior_lines(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-24-2026"
    event_date = "2026-06-24"
    run_date = "2026-06-24"

    first = {
        "slug": slug,
        "event_date": event_date,
        "run_date": run_date,
        "title": "Highest temperature in Chengdu on June 24?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "distribution": [
            {"outcome": "27°C", "q_round": 0.60, "q_bayes_posterior": 0.60, "q_temperature_bucket_posterior": 0.60},
            {"outcome": "28°C", "q_round": 0.40, "q_bayes_posterior": 0.40, "q_temperature_bucket_posterior": 0.40},
        ],
    }
    second = {
        **first,
        "distribution": [
            {"outcome": "27°C", "q_round": 0.20, "q_bayes_posterior": 0.20, "q_temperature_bucket_posterior": 0.20},
            {"outcome": "28°C", "q_round": 0.80, "q_bayes_posterior": 0.80, "q_temperature_bucket_posterior": 0.80},
        ],
    }
    first_id = server._insert_event_fact_history(
        conn,
        run_date=run_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-24T02:00:00+00:00",
        model_snapshot_id=1,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=first,
    )
    server._upsert_event_fact_latest(conn, slug, event_date, first_id)
    latest_id = server._insert_event_fact_history(
        conn,
        run_date=run_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-24T03:00:00+00:00",
        model_snapshot_id=2,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=second,
    )
    server._upsert_event_fact_latest(conn, slug, event_date, latest_id)
    conn.commit()
    conn.close()

    cache_key = "summary:2026-06-24:24"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 0)
    monkeypatch.setattr(server, "UI_SNAPSHOT_STALE_WHILE_REVALIDATE", False)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_FAST", True)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_BUCKET_SERIES_ENABLED", True)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_BUCKET_SERIES_POINTS", 48)
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.pop(cache_key, None)

    summary = server.ui_snapshot_summary("2026-06-24", 5)

    assert summary["ok"] is True
    item = summary["facts"]["items"][0]
    assert item["bucket_series_source"] == "event_fact_summary_cache_distribution_summary"
    assert item["bucket_series_snapshot_count"] == 2
    assert item["bucket_series"]["28°C"][0]["q_posterior"] == 0.40
    assert item["bucket_series"]["28°C"][1]["q_posterior"] == 0.80
    assert item["bucket_series"]["28°C"][1]["ts"] == "2026-06-24T03:00:00+00:00"


def test_ui_snapshot_summary_rebuild_stays_read_only() -> None:
    source = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    start = source.index("def ui_snapshot_summary(")
    build_start = source.index("    def _build_payload()", start)
    build_end = source.index("    def _background_rebuild()", build_start)
    build_block = source[build_start:build_end]

    assert "open_db_ro_fast" in build_block
    assert "with open_db(" not in build_block
    assert "UI_SNAPSHOT_SUMMARY_REBUILD_DB_BUSY_TIMEOUT_MS" in build_block


def test_trade_sync_health_names_unified_driver_not_legacy_loop_only() -> None:
    source = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    assert "成交同步状态长时间未成功更新（unified trade_engine/成交同步链路可能卡住或网络失败）" in source
    assert "交易同步驱动未运行（unified trade_engine 与 legacy trade_sync_loop 均无新鲜心跳）" in source


def test_ui_snapshot_summary_latest_run_date_uses_short_memory_cache(monkeypatch) -> None:
    calls = {"open": 0}

    @contextmanager
    def fake_open_db_ro_fast(*_args, **_kwargs):
        calls["open"] += 1
        yield object()

    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_RUN_DATE_CACHE_SEC", 30.0)
    monkeypatch.setattr(server, "_UI_SNAPSHOT_SUMMARY_RUN_DATE_CACHE", (0.0, ""))
    monkeypatch.setattr(server, "open_db_ro_fast", fake_open_db_ro_fast)
    monkeypatch.setattr(server, "latest_ui_default_run_date", lambda _conn: "2099-01-03")

    first = server._ui_snapshot_summary_latest_run_date_cached()
    second = server._ui_snapshot_summary_latest_run_date_cached()

    assert first == "2099-01-03"
    assert second == "2099-01-03"
    assert calls["open"] == 1


def test_latest_ui_default_run_date_prefers_chengdu_today_over_future() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    today_slug = "highest-temperature-in-chengdu-on-june-23-2026"
    future_slug = "highest-temperature-in-chengdu-on-june-24-2026"
    payload_today = {
        "slug": today_slug,
        "event_date": "2026-06-23",
        "run_date": "2026-06-21",
        "title": "Highest temperature in Chengdu on June 23?",
        "city": "Chengdu",
        "distribution": [{"outcome": "29°C", "q_round": 1.0}],
    }
    payload_future = {
        "slug": future_slug,
        "event_date": "2026-06-24",
        "run_date": "2026-06-24",
        "title": "Highest temperature in Chengdu on June 24?",
        "city": "Chengdu",
        "distribution": [{"outcome": "29°C", "q_round": 1.0}],
    }
    today_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-21",
        slug=today_slug,
        event_date="2026-06-23",
        fact_as_of="2026-06-22T23:00:00+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload_today,
    )
    future_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-24",
        slug=future_slug,
        event_date="2026-06-24",
        fact_as_of="2026-06-23T01:00:00+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload_future,
    )
    server._upsert_event_fact_latest(conn, today_slug, "2026-06-23", today_id)
    server._upsert_event_fact_latest(conn, future_slug, "2026-06-24", future_id)
    conn.commit()

    old_today = server._chengdu_today_date_key
    try:
        server._chengdu_today_date_key = lambda: "2026-06-23"
        assert server.latest_fact_run_date_with_rows(conn, max_scan=10) == "2026-06-24"
        assert server.latest_ui_default_run_date(conn, max_scan=10) == "2026-06-23"
    finally:
        server._chengdu_today_date_key = old_today


def test_ui_snapshot_upcoming_rows_filter_past_run_date_events() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)

    def insert_fact(event_date: str, fact_as_of: str) -> int:
        slug = f"highest-temperature-in-chengdu-on-june-{int(event_date[-2:])}-2026"
        payload = {
            "slug": slug,
            "event_date": event_date,
            "run_date": "2026-06-27",
            "title": f"Highest temperature in Chengdu on {event_date}?",
            "city": "Chengdu",
            "distribution": [{"outcome": "29°C", "q_round": 1.0}],
        }
        fact_id = server._insert_event_fact_history(
            conn,
            run_date="2026-06-27",
            slug=slug,
            event_date=event_date,
            fact_as_of=fact_as_of,
            model_snapshot_id=None,
            market_snapshot_id=None,
            composer_version=server.FACTS_COMPOSER_VERSION,
            item=payload,
        )
        server._upsert_event_fact_latest(conn, slug, event_date, fact_id)
        return fact_id

    insert_fact("2026-06-26", "2026-06-27T06:00:00+00:00")
    insert_fact("2026-06-27", "2026-06-27T05:00:00+00:00")
    insert_fact("2026-06-28", "2026-06-27T04:00:00+00:00")
    conn.commit()

    latest_rows = server.select_event_fact_rows_latest_with_upcoming(
        conn,
        "2026-06-27",
        upcoming_days=2,
    )
    summary_rows = server.select_event_fact_summary_cache_rows_paged(
        conn,
        "2026-06-27",
        1,
        5,
        include_upcoming=True,
        upcoming_days=2,
    )

    assert [row["event_date"] for row in latest_rows] == ["2026-06-27", "2026-06-28"]
    assert [row["event_date"] for row in summary_rows] == ["2026-06-27", "2026-06-28"]


def test_first_prediction_lead_day_windows_are_chengdu_local_days() -> None:
    start_ts, end_ts = server._prediction_lead_day_utc_window("2026-06-20", "D0")
    assert start_ts == "2026-06-19T16:00:00+00:00"
    assert end_ts == "2026-06-20T16:00:00+00:00"

    start_ts, end_ts = server._prediction_lead_day_utc_window("2026-06-20", "D2")
    assert start_ts == "2026-06-17T16:00:00+00:00"
    assert end_ts == "2026-06-18T16:00:00+00:00"


def test_first_prediction_lead_day_materialization_window_caps_at_event_end() -> None:
    start_ts, end_ts = server._lead_day_window_for_materialization(
        "2026-06-09",
        "D0",
        "2026-06-09T23:04:07+00:00",
        "2026-06-09T12:00:00Z",
    )
    assert start_ts == "2026-06-08T16:00:00+00:00"
    assert end_ts == "2026-06-09T12:00:00+00:00"


def test_first_prediction_history_supplement_counts_snapshot_backfill_sources() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE event_fact_history(slug TEXT, event_date TEXT, fact_as_of TEXT);
        CREATE TABLE model_snapshots(slug TEXT, event_date TEXT, created_at TEXT);
        CREATE TABLE market_snapshots(slug TEXT, event_date TEXT, created_at TEXT);
        CREATE TABLE event_market_ticks(slug TEXT, event_date TEXT, created_at TEXT);
        """
    )
    key = ("highest-temperature-in-chengdu-on-june-20-2026", "2026-06-20")
    conn.execute("INSERT INTO event_fact_history VALUES(?,?,?)", (*key, "2026-06-19T18:00:00+00:00"))
    conn.execute("INSERT INTO model_snapshots VALUES(?,?,?)", (*key, "2026-06-18T01:00:00+00:00"))
    conn.execute("INSERT INTO market_snapshots VALUES(?,?,?)", (*key, "2026-06-19T01:00:00+00:00"))
    conn.execute("INSERT INTO event_market_ticks VALUES(?,?,?)", (*key, "2026-06-20T01:00:00+00:00"))

    supplement = server._first_prediction_history_supplement(conn, [key])

    assert supplement["settled_events"] == 1
    assert supplement["fact_history_events"] == 1
    assert supplement["model_snapshot_events"] == 1
    assert supplement["market_snapshot_events"] == 1
    assert supplement["market_tick_events"] == 1
    assert supplement["lead_day_slots"]["D2"]["model_snapshot_events"] == 1
    assert supplement["lead_day_slots"]["D1"]["market_snapshot_events"] == 1
    assert supplement["lead_day_slots"]["D0"]["fact_history_events"] == 1
    assert supplement["lead_day_slots"]["D0"]["market_tick_events"] == 1
    assert supplement["gap_reason"] == "event_fact_history_missing_lead_day_materialization"


def test_first_prediction_hit_rate_uses_indexed_lead_day_loader() -> None:
    source = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    start = source.index("def first_prediction_hit_rate_stats(")
    end = source.index("    fact_history_max_id = 0", start)
    block = source[start:end]

    assert "_load_first_prediction_fact_rows(conn, settlement_meta.keys())" in block
    assert "WHERE event_date BETWEEN ? AND ?" not in block
    assert "slug LIKE '%chengdu%'" not in block


def test_hit_rate_provenance_normalizer_enriches_legacy_snapshot() -> None:
    legacy = {
        "ok": True,
        "coverage": {
            "eligible_events": 3,
            "sample_provenance_counts": {
                "external_history_reconstruction": {
                    "class": "external_history_reconstruction",
                    "label": "外部历史重建",
                    "lead_day_samples": 2,
                    "history_only": True,
                    "external_archive": True,
                },
            },
        },
        "by_source": {
            "gfs_seamless": {
                "prediction_modes": {
                    "probability_bucket": {
                        "lead_days": {
                            "D1": {
                                "events": 3,
                                "count": 2,
                                "hit": 1,
                                "missing": 1,
                                "rate": 0.5,
                                "hit_rate": 0.5,
                                "external_archive_count": 2,
                                "history_only_count": 2,
                                "provenance_counts": {
                                    "external_history_reconstruction": {
                                        "class": "external_history_reconstruction",
                                        "label": "外部历史重建",
                                        "events": 2,
                                        "count": 2,
                                        "history_only": True,
                                        "external_archive": True,
                                        "sources": {"open_meteo_single_runs": 2},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }

    normalized = server._normalize_prediction_hit_rate_provenance_payload(legacy)
    coverage_mix = normalized["coverage"]["sample_provenance_mix"]
    cell = normalized["by_source"]["gfs_seamless"]["prediction_modes"]["probability_bucket"]["lead_days"]["D1"]

    assert coverage_mix["external_history_count"] == 2
    assert coverage_mix["has_external_history"] is True
    assert "sample_provenance_layered" in normalized["coverage"]["provenance_diagnostic"]["codes"]
    assert "external_archive+history_only n=2" in normalized["coverage"]["provenance_diagnostic"]["summary"]
    assert cell["sample_mix"]["external_history_count"] == 2
    assert cell["sample_mix"]["items"][0]["role"] == "external_history_reconstruction"
    assert "contains_external_history_reconstruction" in cell["diagnostic"]["codes"]
    assert "sample_provenance_layered" in cell["diagnostic"]["provenance_diagnostic"]["codes"]
    assert "external history reconstruction samples n=2" in cell["diagnostic"]["summary"]


def test_hit_rate_provenance_normalizer_recovers_legacy_count_only_snapshot() -> None:
    legacy = {
        "ok": True,
        "coverage": {
            "eligible_events": 2,
            "external_archive_count": 2,
            "history_only_count": 2,
        },
        "by_source": {
            "gfs_seamless": {
                "prediction_modes": {
                    "probability_bucket": {
                        "lead_days": {
                            "D1": {
                                "events": 2,
                                "count": 2,
                                "hit": 1,
                                "missing": 0,
                                "rate": 0.5,
                                "hit_rate": 0.5,
                                "external_archive_count": 2,
                                "history_only_count": 2,
                            },
                        },
                    },
                },
            },
        },
    }

    normalized = server._normalize_prediction_hit_rate_provenance_payload(legacy)
    coverage = normalized["coverage"]
    cell = normalized["by_source"]["gfs_seamless"]["prediction_modes"]["probability_bucket"]["lead_days"]["D1"]

    assert coverage["sample_provenance_counts"]["external_history_reconstruction"]["lead_day_samples"] == 2
    assert coverage["sample_provenance_mix"]["external_history_count"] == 2
    assert "external_archive+history_only n=2" in coverage["provenance_diagnostic"]["summary"]
    assert cell["provenance_counts"]["external_history_reconstruction"]["count"] == 2
    assert cell["sample_mix"]["items"][0]["label"] == "外部历史重建"
    assert "contains_external_history_reconstruction" in cell["diagnostic"]["codes"]


def test_hit_rate_ui_labels_external_history_provenance_explicitly() -> None:
    source = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "external_archive+history_only" in source
    assert "history_only" in source
    assert "样本来源已分层" in source
    assert "避免和原生实时采集混看" in source


def test_auto_trade_ssot_remote_no_candidate_uses_payload_target_context() -> None:
    payload = {
        "target_bucket": "29°C",
        "target_realization_capital_plan": {
            "ok": True,
            "target_bucket": "29°C",
            "target_posterior": 0.42,
            "target_realization_reliability": 0.72,
        },
        "temperature_path_distribution": {
            "target_bucket": "29°C",
            "target_bucket_posterior": 0.42,
        },
    }
    row = {
        "outcome": "28°C",
        "best_ask": 0.34,
        "best_bid": 0.33,
        "best_ask_no": 0.67,
        "best_bid_no": 0.66,
        "q_round": 0.285,
        "q_round_no": 0.715,
        "q_entry": 0.285,
        "q_entry_no": 0.715,
        "q_bayes_posterior": 0.285,
        "q_bayes_posterior_no": 0.715,
        "edge_score_no": 0.72,
        "exec_edge_entry_no": 0.21,
        "exec_edge_exit_no": 0.22,
        "exec_edge_score_no": 5.3,
        "edge_realizable_no": 0.21,
        "edge_realizable_score_no": 5.3,
        "exec_fee_cost_no": 0.0,
        "exec_fee_bps": 100.0,
        "no_token": "no-token",
        "yes_token": "yes-token",
        "rules_contract_ok": True,
        "market_var": "temperature_2m_max",
    }

    candidate = server._ssot_candidate_from_row(
        row,
        "NO",
        "28°C",
        {},
        strategy_context=server._ssot_target_context_from_payload(payload),
    )

    assert candidate["target_bucket"] == "29°C"
    assert candidate["posterior_settlement_bucket_distance"] == 1
    assert candidate["posterior_settlement_entry_block_reason"] != "target_bucket_missing_for_remote_no"
    assert candidate["posterior_settlement_entry_block_reason"] == "remote_bucket_realization_prob_above_max"
    reasons = server._ssot_entry_reasons_main(
        candidate,
        server._ssot_entry_policy_snapshot(),
        market_closed=False,
        temp_event_blocked=False,
        days_to_event=0,
    )
    assert any("remote_bucket_realization_prob_above_max" in reason for reason in reasons)


def test_materialize_lead_day_hit_rates_writes_success_after_failed_same_signature(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    signature = {
        "schema": server.SOURCE_LEAD_DAY_HIT_RATE_MATERIALIZATION_SCHEMA,
        "settlement_signature_hash": "same-settlement",
    }
    server._save_feedback_snapshot_to_db(
        conn,
        kind=server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_KIND,
        days=server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_DAYS,
        end_date="2026-06-20",
        payload={
            "ok": False,
            "error": "no_eligible_first_prediction_events",
            "materialization_signature": signature,
        },
        ok=False,
        error="no_eligible_first_prediction_events",
    )

    monkeypatch.setattr(server, "_today_et_key", lambda: "2026-06-21")
    monkeypatch.setattr(
        server,
        "first_prediction_hit_rate_stats",
        lambda _conn, days=0, scope="chengdu_zuuu": {
            "ok": True,
            "version": "source_lead_day_hit_rate_v2",
            "days": int(days),
            "scope": scope,
            "end_date": "2026-06-20",
            "materialization_signature": signature,
            "coverage": {"eligible_events": 1},
            "sources": [],
            "by_source": {},
        },
    )

    payload = server.materialize_source_lead_day_hit_rates(conn, days=0, scope="chengdu_zuuu")
    rows = conn.execute(
        """
        SELECT ok, error, payload_json
        FROM feedback_snapshots
        WHERE kind = ? AND days = ?
        ORDER BY id
        """,
        (server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_KIND, server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_DAYS),
    ).fetchall()

    assert payload["ok"] is True
    assert not payload.get("materialized_write_skipped")
    assert len(rows) == 2
    assert rows[-1][0] == 1
    assert json.loads(rows[-1][2])["coverage"]["eligible_events"] == 1


def test_materialize_lead_day_hit_rates_does_not_publish_no_eligible_failure(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(server, "FIRST_PREDICTION_HIT_RATE_EMPTY_RETRY_MAX", 0)
    server._save_feedback_snapshot_to_db(
        conn,
        kind=server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_KIND,
        days=server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_DAYS,
        end_date="2026-06-20",
        payload={
            "ok": True,
            "version": "source_lead_day_hit_rate_v2",
            "coverage": {"eligible_events": 20},
            "sources": [],
            "by_source": {},
        },
        ok=True,
        error="",
    )

    monkeypatch.setattr(server, "_today_et_key", lambda: "2026-06-21")
    monkeypatch.setattr(
        server,
        "first_prediction_hit_rate_stats",
        lambda _conn, days=0, scope="chengdu_zuuu": {
            "ok": False,
            "version": "source_lead_day_hit_rate_v2",
            "days": int(days),
            "scope": scope,
            "end_date": "2026-06-21",
            "na_reason": "no_eligible_first_prediction_events",
            "coverage": {"eligible_events": 0, "excluded": {"no_fact": 21}},
            "sources": [],
            "by_source": {},
        },
    )

    payload = server.materialize_source_lead_day_hit_rates(conn, days=0, scope="chengdu_zuuu")
    rows = conn.execute(
        """
        SELECT ok, error
        FROM feedback_snapshots
        WHERE kind = ? AND days = ?
        ORDER BY id
        """,
        (server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_KIND, server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_DAYS),
    ).fetchall()

    assert payload["ok"] is False
    assert payload["materialized_write_ok"] is True
    assert payload["materialized_write_skipped"] is True
    assert payload["materialized_write_skip_reason"] == "no_eligible_first_prediction_events"
    assert payload["materialized_existing_snapshot_ok"] is True
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == ""


def test_materialize_lead_day_hit_rates_retries_no_eligible_and_writes_recovered_success(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    calls = {"count": 0}

    monkeypatch.setattr(server, "_today_et_key", lambda: "2026-06-21")
    monkeypatch.setattr(server, "FIRST_PREDICTION_HIT_RATE_EMPTY_RETRY_MAX", 2)
    monkeypatch.setattr(server, "FIRST_PREDICTION_HIT_RATE_EMPTY_RETRY_SLEEP_SEC", 0.0)

    def fake_stats(_conn, days=0, scope="chengdu_zuuu"):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "ok": False,
                "version": "source_lead_day_hit_rate_v2",
                "days": int(days),
                "scope": scope,
                "end_date": "2026-06-21",
                "na_reason": "no_eligible_first_prediction_events",
                "coverage": {"eligible_events": 0, "excluded": {"no_fact": 21}},
                "sources": [],
                "by_source": {},
            }
        return {
            "ok": True,
            "version": "source_lead_day_hit_rate_v2",
            "days": int(days),
            "scope": scope,
            "end_date": "2026-06-21",
            "coverage": {"eligible_events": 20},
            "materialization_signature": {
                "schema": server.SOURCE_LEAD_DAY_HIT_RATE_MATERIALIZATION_SCHEMA,
                "settlement_signature_hash": "recovered",
            },
            "sources": [],
            "by_source": {},
        }

    monkeypatch.setattr(server, "first_prediction_hit_rate_stats", fake_stats)

    payload = server.materialize_source_lead_day_hit_rates(conn, days=0, scope="chengdu_zuuu")
    rows = conn.execute(
        """
        SELECT ok, error, payload_json
        FROM feedback_snapshots
        WHERE kind = ? AND days = ?
        ORDER BY id
        """,
        (server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_KIND, server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_DAYS),
    ).fetchall()

    assert calls["count"] == 2
    assert payload["ok"] is True
    assert payload["materialized_empty_retry_count"] == 1
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert json.loads(rows[0][2])["coverage"]["eligible_events"] == 20


def test_first_prediction_hit_rate_does_not_cache_no_eligible_payload(monkeypatch) -> None:
    monkeypatch.setattr(server, "FIRST_PREDICTION_HIT_RATE_CACHE_SEC", 300)
    monkeypatch.setattr(server, "_FIRST_PREDICTION_HIT_RATE_CACHE", {})
    monkeypatch.setattr(
        server,
        "resolve_prediction_hit_stats_range",
        lambda _conn, _days: (datetime(2026, 6, 20).date(), datetime(2026, 6, 20).date()),
    )
    monkeypatch.setattr(
        server,
        "_load_first_prediction_settlement_meta_map",
        lambda _conn, _start, _end, scope="chengdu_zuuu": {
            ("highest-temperature-in-chengdu-on-june-20-2026", "2026-06-20"): {
                "settlement_actual": 30.0,
                "settlement_known_at": "2026-06-20T18:45:25+00:00",
                "settlement_source": "test",
            }
        },
    )
    monkeypatch.setattr(server, "_load_first_prediction_fact_rows", lambda _conn, _keys: [])

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    payload = server.first_prediction_hit_rate_stats(conn, days=0, scope="chengdu_zuuu")

    assert payload["ok"] is False
    assert payload["na_reason"] == "no_eligible_first_prediction_events"
    assert "chengdu_zuuu:0" not in server._FIRST_PREDICTION_HIT_RATE_CACHE


def test_ui_snapshot_summary_fast_avoids_heavy_count_and_market_hydrate(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-21-2026",
        "title": "Highest temperature in Chengdu on June 21?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "event_date": "2026-06-21",
        "run_date": "2026-06-21",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "distribution": [
            {"outcome": "25°C or below", "q_round": 0.58, "yes_price": 0.58},
            {"outcome": "26°C", "q_round": 0.20, "yes_price": 0.20},
        ],
    }

    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 0)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_FAST", True)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_LIVE_MARKET_HYDRATE", False)
    monkeypatch.setattr(server, "_safe_model_update_stats", lambda limit=48: {"models": []})
    monkeypatch.setattr(
        server,
        "count_event_fact_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("count should be skipped")),
    )
    monkeypatch.setattr(
        server,
        "_hydrate_distribution_market_fields_in_place",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("hydrate should be skipped")),
    )
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.clear()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-21",
        slug=payload["slug"],
        event_date="2026-06-21",
        fact_as_of="2026-06-21T04:00:00+00:00",
        model_snapshot_id=1,
        market_snapshot_id=2,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload,
    )
    server._upsert_event_fact_latest(conn, payload["slug"], "2026-06-21", fact_id)
    server._upsert_event_fact_summary_cache(
        conn,
        fact_id=fact_id,
        run_date="2026-06-21",
        slug=payload["slug"],
        event_date="2026-06-21",
        fact_as_of="2026-06-21T04:00:00+00:00",
        item=payload,
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        server,
        "query_market_tick_latest_for_keys",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("summary cache path should not query ticks")),
    )
    summary = server.ui_snapshot_summary("2026-06-21", 24)

    assert summary["ok"] is True
    assert summary["facts"]["source"] == "event_fact_summary_cache_with_upcoming"
    assert summary["facts"]["total"] == 1
    item = summary["facts"]["items"][0]
    assert item["slug"] == payload["slug"]
    assert item["single_temperature_top_label"] == "25°C or below"
    assert item["distribution"][0]["best_bid"] == 0.58


def test_event_fact_write_merge_fills_top_level_settlement_actual_into_final_lock(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-22-2026"
    event_date = "2026-06-22"

    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-23",
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-23T11:19:59+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": "2026-06-23",
            "market_var": "temperature_2m_max",
            "market_unit": "C",
            "rounding_rule": "whole_degrees",
            "settlement_actual": 33.0,
            "market_closed": 1,
            "distribution": [
                {"outcome": "32°C", "q_bayes_posterior": 0.40},
                {"outcome": "33°C", "q_bayes_posterior": 0.30},
                {"outcome": "34°C", "q_bayes_posterior": 0.30},
            ],
        },
    )

    payload = json.loads(
        conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()["fact_json"]
    )
    features = payload["weather_likelihood_features"]
    by_outcome = {row["outcome"]: row for row in payload["distribution"]}
    assert features["settlement_actual"] == 33.0
    assert features["settlement_actual_c"] == 33.0
    assert features["market_closed"] == 1
    assert payload["final_bucket_lock"] is True
    assert payload["final_bucket"] == "33°C"
    assert payload["weather_likelihood_features"]["final_bucket_lock"] is True
    assert payload["hard_source_posterior_override"]["final_bucket_lock"] is True
    assert payload["hard_source_posterior_override"]["final_lock_reason"] == "settlement_actual_known"
    assert by_outcome["33°C"]["q_bayes_posterior"] == 1.0
    assert by_outcome["32°C"]["q_bayes_posterior"] == 0.0
    assert by_outcome["34°C"]["q_bayes_posterior"] == 0.0


def test_event_fact_latest_rejects_older_weak_fact_over_hard_source_snapshot(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-23-2026"
    event_date = "2026-06-23"

    hard_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-23T07:22:49+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "target_bucket": "30°C",
            "ssot_target_bucket": "30°C",
            "hard_source_direct_ssot_write": True,
            "hard_source_observed_value_c": 30.0,
            "hard_source_observed_bucket": "30°C",
            "distribution": [
                {"outcome": "29°C", "q_bayes_posterior": 0.0, "zuuu_metar_raw_awc_today_max_c": 30.0},
                {"outcome": "30°C", "q_bayes_posterior": 0.70, "zuuu_metar_raw_awc_today_max_c": 30.0},
            ],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, hard_fact_id) is True

    weak_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-23T07:19:49+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "target_bucket": "29°C",
            "distribution": [
                {"outcome": "29°C", "q_bayes_posterior": 0.59, "zuuu_metar_raw_awc_today_max_c": 29.0},
                {"outcome": "30°C", "q_bayes_posterior": 0.29, "zuuu_metar_raw_awc_today_max_c": 29.0},
            ],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, weak_fact_id) is False

    row = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?",
        (slug, event_date),
    ).fetchone()
    assert row["fact_id"] == hard_fact_id
    hard_payload = json.loads(
        conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (hard_fact_id,)).fetchone()["fact_json"]
    )
    assert hard_payload.get("history_compacted") is not True
    conn.close()


def test_event_fact_latest_rejects_stale_awc_latest_at_with_same_max(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-27-2026"
    event_date = "2026-06-27"

    fresh_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-27T06:20:50+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "weather_likelihood_features": {
                "zuuu_metar_raw_awc_latest_at": "2026-06-27T06:00:00+00:00",
                "zuuu_metar_raw_awc_latest_temp_c": 27.0,
                "zuuu_metar_raw_awc_today_max_c": 28.0,
                "zuuu_metar_raw_awc_today_max_at": "2026-06-27T04:00:00+00:00",
                "observed_today_max_c": 28.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
            },
            "distribution": [{"outcome": "28°C", "q_bayes_posterior": 1.0}],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, fresh_fact_id) is True

    stale_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-27T06:21:32+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "weather_likelihood_features": {
                "zuuu_metar_raw_awc_latest_at": "2026-06-27T04:00:00+00:00",
                "zuuu_metar_raw_awc_latest_temp_c": 28.0,
                "zuuu_metar_raw_awc_today_max_c": 28.0,
                "zuuu_metar_raw_awc_today_max_at": "2026-06-27T04:00:00+00:00",
                "observed_today_max_c": 28.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
            },
            "distribution": [{"outcome": "28°C", "q_bayes_posterior": 1.0}],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, stale_fact_id) is True

    row = conn.execute(
        """
        SELECT l.fact_id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    conn.close()
    assert row["fact_id"] == stale_fact_id
    payload = json.loads(row["fact_json"])
    assert payload["weather_likelihood_features"]["zuuu_metar_raw_awc_latest_at"] == "2026-06-27T06:00:00+00:00"
    assert payload["weather_likelihood_features"]["zuuu_metar_raw_awc_latest_temp_c"] == 27.0


def test_event_fact_latest_rejects_unmaterialized_observation_path_over_materialized_fact(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-29-2026"
    event_date = "2026-06-29"

    materialized_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-28T18:15:00+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "target_bucket": "31°C",
            "ssot_target_bucket": "31°C",
            "hard_source_posterior_override_applied": True,
            "observation_path_likelihood_posterior_materialized": True,
            "fact_provenance": {
                "observation_path_likelihood": {
                    "applied": True,
                    "posterior_materialized": True,
                    "evidence": 3.5,
                }
            },
            "distribution": [
                {
                    "outcome": "31°C",
                    "q_temperature_bucket_posterior": 0.235,
                    "q_temperature_bucket_posterior_pre_observation_path_likelihood": 0.2364,
                },
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.212},
            ],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, materialized_fact_id) is True

    unmaterialized_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-28T18:16:00+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "target_bucket": "31°C",
            "ssot_target_bucket": "31°C",
            "hard_source_posterior_override_applied": True,
            "fact_provenance": {
                "observation_path_likelihood": {
                    "applied": True,
                    "evidence": 3.5,
                }
            },
            "distribution": [
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.2364},
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.2144},
            ],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, unmaterialized_fact_id) is False

    row = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, event_date),
    ).fetchone()
    assert row["fact_id"] == materialized_fact_id

    next_materialized_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-28T18:17:00+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "target_bucket": "31°C",
            "ssot_target_bucket": "31°C",
            "hard_source_posterior_override_applied": True,
            "observation_path_likelihood_posterior_materialized": True,
            "fact_provenance": {
                "observation_path_likelihood": {
                    "applied": True,
                    "posterior_materialized": True,
                    "evidence": 3.6,
                }
            },
            "distribution": [
                {
                    "outcome": "31°C",
                    "q_temperature_bucket_posterior": 0.234,
                    "q_temperature_bucket_posterior_pre_observation_path_likelihood": 0.2364,
                },
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.211},
            ],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, next_materialized_fact_id) is True
    row = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, event_date),
    ).fetchone()
    conn.close()
    assert row["fact_id"] == next_materialized_fact_id


def test_event_fact_latest_allows_newer_model_snapshot_with_same_hard_observation(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-29-2026"
    event_date = "2026-06-29"
    base_item = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "target_bucket": "31°C",
        "hard_source_posterior_override_applied": True,
        "observation_path_likelihood_posterior_materialized": True,
        "weather_likelihood_features": {
            "zuuu_metar_raw_awc_latest_at": "2026-06-29T05:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        "fact_provenance": {
            "observation_path_likelihood": {
                "applied": True,
                "posterior_materialized": True,
                "evidence": 20.0,
            }
        },
        "distribution": [
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.45,
                "q_temperature_bucket_posterior_pre_observation_path_likelihood": 0.28,
            },
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.27},
        ],
    }
    old_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-29T05:49:41+00:00",
        model_snapshot_id=2261,
        market_snapshot_id=12340,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=base_item,
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, old_fact_id) is True

    new_item = {
        **base_item,
        "fact_provenance": {
            **base_item["fact_provenance"],
            "model_snapshot_id": 2367,
        },
        "soft_forecast_posterior_channels": {
            "taf_forecast_peak": {
                "posterior_included": False,
                "posterior_status": "captured_not_in_main_posterior",
            }
        },
        "distribution": [
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.43,
                "q_temperature_bucket_posterior_pre_observation_path_likelihood": 0.29,
                "q_bayes_taf_peak_evidence": 0.0,
                "q_bayes_taf_peak_status": "disabled",
                "taf_forecast_peak_posterior_included": False,
            },
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.29},
        ],
    }
    new_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-29T05:49:35+00:00",
        model_snapshot_id=2367,
        market_snapshot_id=12462,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=new_item,
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, new_fact_id) is True
    row = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, event_date),
    ).fetchone()
    conn.close()
    assert row["fact_id"] == new_fact_id


def test_event_fact_latest_preserves_fast_trade_latest_when_rich_payload_target_unchanged(
    tmp_path,
) -> None:
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    event_date = "2026-06-18"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fast_payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "station_id": "ZUUU",
        "posterior_target_direct_fast_trade_write": True,
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 32.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T07:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 32.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T07:00:00+00:00",
        },
        "posterior_temperature_distribution": {
            "bucket_probabilities": {"30°C or higher": 1.0, "29°C": 0.0},
        },
        "distribution": [
            {"outcome": "30°C or higher", "q_temperature_bucket_posterior": 1.0},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.0},
        ],
    }
    fast_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-18T07:00:02+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=fast_payload,
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, fast_fact_id) is True
    rich_payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "station_id": "ZUUU",
        "weather_likelihood_features": dict(fast_payload["weather_likelihood_features"]),
        "posterior_temperature_distribution": {
            "bucket_probabilities": {"30°C or higher": 1.0, "29°C": 0.0},
        },
        "posterior_target_bucket_selection": {
            "target_bucket": "30°C or higher",
            "target_probability": 1.0,
        },
        "distribution": [
            {"outcome": "30°C or higher", "q_temperature_bucket_posterior": 1.0},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.0},
        ],
        "chengdu_intraday_summary": {"large": "x" * 1000},
    }
    rich_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-18T07:06:00+00:00",
        model_snapshot_id=11,
        market_snapshot_id=21,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=rich_payload,
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, rich_fact_id) is False
    latest = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, event_date),
    ).fetchone()
    conn.close()

    assert latest["fact_id"] == fast_fact_id


def test_event_fact_latest_allows_rich_payload_when_fast_trade_target_changes(
    tmp_path,
) -> None:
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    event_date = "2026-06-18"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fast_payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "station_id": "ZUUU",
        "posterior_target_direct_fast_trade_write": True,
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T07:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T07:00:00+00:00",
        },
        "posterior_temperature_distribution": {
            "bucket_probabilities": {"31°C": 0.6, "30°C": 0.4},
        },
        "distribution": [
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.6},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.4},
        ],
    }
    fast_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-18T07:00:02+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=fast_payload,
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, fast_fact_id) is True
    rich_payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "station_id": "ZUUU",
        "weather_likelihood_features": dict(fast_payload["weather_likelihood_features"]),
        "posterior_temperature_distribution": {
            "bucket_probabilities": {"32°C": 0.7, "31°C": 0.3},
        },
        "posterior_target_bucket_selection": {
            "target_bucket": "32°C",
            "target_probability": 0.7,
        },
        "distribution": [
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.7},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.3},
        ],
        "chengdu_intraday_summary": {"large": "x" * 1000},
    }
    rich_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-18T07:06:00+00:00",
        model_snapshot_id=11,
        market_snapshot_id=21,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=rich_payload,
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, rich_fact_id) is True
    latest = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, event_date),
    ).fetchone()
    conn.close()

    assert latest["fact_id"] == rich_fact_id


def test_chengdu_intraday_summary_merge_preserves_existing_soft_forecast_components() -> None:
    item = {
        "distribution": [
            {
                "outcome": "29°C",
                "q_bayes_posterior": 0.10,
                "q_bayes_taf_peak_evidence": 0.0,
                "q_bayes_taf_peak_status": "disabled",
                "taf_forecast_peak_posterior_included": False,
                "q_temperature_bucket_score_components": {"taf_peak": 0.0, "model": 1.0},
            }
        ]
    }
    summary = {
        "bucket_probabilities": [
            {
                "outcome": "29°C",
                "q_bayes_posterior": 0.12,
                "q_bayes_taf_peak_evidence": 5.2,
                "q_bayes_taf_peak_status": "enabled_manual_override",
                "taf_forecast_peak_posterior_included": True,
                "q_temperature_bucket_score_components": {"taf_peak": 3.3, "model": 1.0},
            }
        ]
    }

    assert server._merge_chengdu_intraday_summary_distribution_into_fact_item(item, summary) is True
    row = item["distribution"][0]
    assert row["q_bayes_posterior"] == 0.12
    assert row["q_bayes_taf_peak_evidence"] == 0.0
    assert row["q_bayes_taf_peak_status"] == "disabled"
    assert row["taf_forecast_peak_posterior_included"] is False
    assert row["q_temperature_bucket_score_components"]["taf_peak"] == 0.0


def test_chengdu_intraday_summary_merge_preserves_event_fact_posterior_ssot() -> None:
    item = {
        "target_bucket_selection": {
            "target_bucket": "33°C",
            "target_probability_field": "event_fact_latest.distribution.q_temperature_bucket_posterior",
            "target_bucket_probability_field": "event_fact_latest.distribution.q_temperature_bucket_posterior",
        },
        "distribution": [
            {
                "outcome": "33°C",
                "q_temperature_bucket_posterior": 0.5319810931180973,
                "q_temperature_bucket_posterior_pct": 53.19810931180973,
                "q_bayes_posterior": 0.5319810931180973,
                "target_bucket_probability": 0.5319810931180973,
                "target_bucket_probability_pct": 53.19810931180973,
                "q_round": 0.11631060268355033,
            },
            {
                "outcome": "34°C",
                "q_temperature_bucket_posterior": 0.12931613826785035,
                "q_temperature_bucket_posterior_pct": 12.931613826785035,
                "q_bayes_posterior": 0.12931613826785035,
                "target_bucket_probability": 0.12931613826785035,
                "target_bucket_probability_pct": 12.931613826785035,
                "q_round": 0.24164792965234136,
            },
        ],
    }
    summary = {
        "bucket_probabilities": [
            {
                "outcome": "33°C",
                "q_temperature_bucket_posterior": 0.41316995846205146,
                "q_temperature_bucket_posterior_pct": 41.316995846205145,
                "q_bayes_posterior": 0.41316995846205146,
                "target_bucket_probability": 0.41316995846205146,
                "target_bucket_probability_pct": 41.316995846205145,
                "q_round": 0.11631060268355033,
                "observation_led_likelihood_probability": 0.251422,
            },
            {
                "outcome": "34°C",
                "q_temperature_bucket_posterior": 0.3445675327892925,
                "q_temperature_bucket_posterior_pct": 34.45675327892925,
                "q_bayes_posterior": 0.3445675327892925,
                "target_bucket_probability": 0.3445675327892925,
                "target_bucket_probability_pct": 34.45675327892925,
                "q_round": 0.24164792965234136,
                "observation_led_likelihood_probability": 0.296863,
            },
        ],
        "observation_led_likelihood": {"applied": True},
    }

    assert server._merge_chengdu_intraday_summary_distribution_into_fact_item(item, summary) is True
    by_label = {row["outcome"]: row for row in item["distribution"]}
    assert by_label["33°C"]["q_temperature_bucket_posterior"] == pytest.approx(0.5319810931180973)
    assert by_label["33°C"]["q_bayes_posterior"] == pytest.approx(0.5319810931180973)
    assert by_label["33°C"]["target_bucket_probability_pct"] == pytest.approx(53.19810931180973)
    assert by_label["34°C"]["q_temperature_bucket_posterior"] == pytest.approx(0.12931613826785035)
    assert by_label["34°C"]["target_bucket_probability"] == pytest.approx(0.12931613826785035)
    assert by_label["34°C"]["observation_led_likelihood_probability"] == pytest.approx(0.296863)


def test_event_fact_latest_compacts_replaced_history_but_keeps_latest_full(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-24-2026"
    event_date = "2026-06-24"
    old_item = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "title": "Highest temperature in Test City on June 24?",
        "city": "Test City",
        "market_unit": "C",
        "model_top_label": "28°C",
        "target_bucket_selection": {"target_bucket": "28°C", "debug_blob": "x" * 4000},
        "chengdu_intraday_summary": {"temperature_distribution": {"rows": ["x" * 4000]}},
        "distribution": [
            {
                "outcome": "28°C",
                "q_round": 0.60,
                "q_bayes_posterior": 0.60,
                "q_temperature_bucket_posterior": 0.60,
                "q_bayes_model_evidence": 2.0,
                "q_bayes_model_prob": 0.60,
                "q_ensemble_qm_meta": {"seasonal_bias_correction": {"raw": "x" * 4000}},
                "raw_large_debug_blob": "x" * 8000,
            }
        ],
        "rows": [
            {
                "outcome": "28°C",
                "q_round": 0.60,
                "q_bayes_posterior": 0.60,
                "raw_large_debug_blob": "x" * 8000,
            }
        ],
    }
    old_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-24T07:10:00+00:00",
        model_snapshot_id=1,
        market_snapshot_id=1,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=old_item,
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, old_fact_id) is True

    new_item = {
        **old_item,
        "distribution": [
            {
                "outcome": "29°C",
                "q_round": 0.70,
                "q_bayes_posterior": 0.70,
                "q_temperature_bucket_posterior": 0.70,
                "raw_large_debug_blob": "y" * 8000,
            }
        ],
        "rows": [{"outcome": "29°C", "q_round": 0.70, "raw_large_debug_blob": "y" * 8000}],
    }
    new_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-24T07:20:00+00:00",
        model_snapshot_id=2,
        market_snapshot_id=2,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=new_item,
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, new_fact_id) is True

    old_payload = json.loads(
        conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (old_fact_id,)).fetchone()["fact_json"]
    )
    new_payload = json.loads(
        conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (new_fact_id,)).fetchone()["fact_json"]
    )
    latest = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, event_date),
    ).fetchone()
    conn.close()

    assert latest["fact_id"] == new_fact_id
    assert old_payload["history_compacted"] is True
    assert old_payload["history_compaction_version"] == server.HISTORY_FACT_COMPACTION_VERSION
    assert old_payload["distribution"][0]["outcome"] == "28°C"
    assert old_payload["distribution"][0]["q_bayes_posterior"] == 0.60
    assert "rows" not in old_payload
    assert "chengdu_intraday_summary" not in old_payload
    assert "raw_large_debug_blob" not in old_payload["distribution"][0]
    assert new_payload.get("history_compacted") is not True
    assert "rows" in new_payload
    assert "chengdu_intraday_summary" in new_payload
    assert new_payload["distribution"][0]["raw_large_debug_blob"]


def test_event_fact_latest_allows_stronger_hard_source_snapshot(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-23-2026"
    event_date = "2026-06-23"

    first_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-23T07:10:00+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "target_bucket": "29°C",
            "hard_source_direct_ssot_write": True,
            "hard_source_observed_value_c": 29.0,
            "distribution": [{"outcome": "29°C", "q_bayes_posterior": 0.60}],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, first_fact_id) is True

    stronger_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-23T07:17:00+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "target_bucket": "30°C",
            "hard_source_direct_ssot_write": True,
            "hard_source_observed_value_c": 30.0,
            "distribution": [{"outcome": "30°C", "q_bayes_posterior": 0.70}],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, stronger_fact_id) is True
    row = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?",
        (slug, event_date),
    ).fetchone()
    assert row["fact_id"] == stronger_fact_id
    conn.close()


def test_event_fact_write_recovers_latest_awc_from_hard_source_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "0")
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-27-2026"
    event_date = "2026-06-27"

    hard_source_item = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 27.0,
            "observed_today_max_c": 28.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-27T05:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 27.0,
            "zuuu_metar_raw_awc_today_max_c": 28.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-27T04:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-27T05:26:27+00:00",
        },
        "distribution": [
            {"outcome": "27°C", "q_bayes_posterior": 0.20, "q_temperature_bucket_posterior": 0.20},
            {"outcome": "28°C", "q_bayes_posterior": 0.40, "q_temperature_bucket_posterior": 0.40},
            {"outcome": "29°C", "q_bayes_posterior": 0.30, "q_temperature_bucket_posterior": 0.30},
        ],
    }
    hard_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-27T05:26:27+00:00",
        model_snapshot_id=1,
        market_snapshot_id=1,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=hard_source_item,
    )
    assert hard_fact_id

    stale_compact_item = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "weather_likelihood_features_compact": {
            "zuuu_metar_raw_awc_latest_at": "2026-06-27T04:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 28.0,
            "zuuu_metar_raw_awc_today_max_c": 28.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-27T04:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-27T04:21:30+00:00",
        },
        "distribution": [
            {"outcome": "27°C", "q_bayes_posterior": 0.24},
            {"outcome": "28°C", "q_bayes_posterior": 0.36},
            {"outcome": "29°C", "q_bayes_posterior": 0.26},
        ],
    }
    stale_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-27T05:30:00+00:00",
        model_snapshot_id=2,
        market_snapshot_id=2,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=stale_compact_item,
    )
    conn.execute(
        """
        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES(?, ?, ?, ?)
        """,
        (slug, event_date, stale_fact_id, "2026-06-27 13:30:00"),
    )

    fact_id = server.upsert_event_fact_from_run_item(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        run_created_at="2026-06-27T05:31:00+00:00",
        run_payload={"score_composer": {"model_snapshot_id": 3, "market_snapshot_id": 3}},
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "weather_likelihood_features_compact": {
                "zuuu_metar_raw_awc_latest_at": "2026-06-27T04:00:00+00:00",
                "zuuu_metar_raw_awc_latest_temp_c": 28.0,
                "zuuu_metar_raw_awc_today_max_c": 28.0,
                "zuuu_metar_raw_awc_today_max_at": "2026-06-27T04:00:00+00:00",
                "zuuu_metar_raw_awc_fetched_at": "2026-06-27T04:21:30+00:00",
            },
            "distribution": [
                {"outcome": "27°C", "q_bayes_posterior": 0.25},
                {"outcome": "28°C", "q_bayes_posterior": 0.35},
                {"outcome": "29°C", "q_bayes_posterior": 0.25},
            ],
        },
    )
    assert fact_id

    payload = json.loads(
        conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()["fact_json"]
    )
    latest = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, event_date),
    ).fetchone()
    conn.close()

    features = payload["weather_likelihood_features"]
    assert latest["fact_id"] == fact_id
    assert features["zuuu_metar_raw_awc_latest_at"] == "2026-06-27T05:00:00+00:00"
    assert features["zuuu_metar_raw_awc_latest_temp_c"] == 27.0
    assert features["zuuu_metar_raw_awc_fetched_at"] == "2026-06-27T05:26:27+00:00"
    assert payload["fact_provenance"]["awc_hard_observation_preserved_from_latest_fact"]["latest_at"] == "2026-06-27T05:00:00+00:00"


def test_fact_write_materializes_hard_source_temperature_path_and_target_chain(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-23-2026"
    payload = {
        "slug": slug,
        "event_date": "2026-06-23",
        "run_date": "2026-06-23",
        "title": "Highest temperature in Chengdu on June 23?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "event_tz": "Asia/Shanghai",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_refreshed_at": "2026-06-23T03:08:01+00:00",
            "weather_likelihood_features_compact": {
                "observed_temp_c": 28.0,
                "observed_today_max_c": 28.0,
                "observed_temp_source": "zuuu_metar_raw_awc",
                "observed_today_max_source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_latest_at": "2026-06-23T03:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-23T03:08:01+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 28.0,
            "zuuu_metar_raw_awc_today_max_c": 28.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-23T03:00:00+00:00",
            "weather_com_forecast_peak_c": 29.0,
            "twc_forecast_peak_c": 29.0,
            "weather_com_forecast_peak_at": "2026-06-23T16:00:00+0800",
            "temperature_rise_rate_c_per_hour": 1.25,
            "temperature_rise_rate_source": "wu_twc_observed_intraday:30m",
            "temperature_rise_rate_window": "30m",
            "temperature_momentum_30m_c_per_hour": 1.25,
            "temperature_momentum_45m_c_per_hour": 1.15,
            "temperature_momentum_1h_c_per_hour": 1.05,
            "temperature_momentum_2h_c_per_hour": 0.7,
            "temperature_momentum_3h_c_per_hour": 0.5,
            "temperature_momentum_6h_c_per_hour": 0.3,
            "temperature_momentum_realization_state": "sustained_warming",
            "temperature_momentum_posterior_gate_allowed": True,
            "temperature_momentum_posterior_confirmed_windows": "30m,45m,1h,2h,3h,6h",
        },
        "distribution": [
            {
                "outcome": "27°C",
                "q_bayes_posterior": 0.2,
                "q_temperature_bucket_posterior": 0.2,
                "q_raw": 0.2,
                "best_bid": 0.10,
                "best_ask": 0.12,
                "yes_token": "yes-27",
                "no_token": "no-27",
            },
            {
                "outcome": "28°C",
                "q_bayes_posterior": 0.2,
                "q_temperature_bucket_posterior": 0.2,
                "q_raw": 0.2,
                "best_bid": 0.25,
                "best_ask": 0.27,
                "yes_token": "yes-28",
                "no_token": "no-28",
            },
            {
                "outcome": "29°C",
                "q_bayes_posterior": 0.4,
                "q_temperature_bucket_posterior": 0.4,
                "q_raw": 0.4,
                "best_bid": 0.34,
                "best_ask": 0.36,
                "yes_token": "yes-29",
                "no_token": "no-29",
            },
            {
                "outcome": "30°C",
                "q_bayes_posterior": 0.2,
                "q_temperature_bucket_posterior": 0.2,
                "q_raw": 0.2,
                "best_bid": 0.15,
                "best_ask": 0.17,
                "yes_token": "yes-30",
                "no_token": "no-30",
            },
        ],
    }

    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-23",
        slug=slug,
        event_date="2026-06-23",
        fact_as_of="2026-06-23T03:08:01+00:00",
        model_snapshot_id=11,
        market_snapshot_id=22,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload,
    )
    row = conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    assert row is not None
    item = json.loads(row["fact_json"])
    rows = {entry["outcome"]: entry for entry in item["distribution"]}

    assert item["hard_source_posterior_override_applied"] is True
    assert item["hard_source_posterior_override"]["window_ignored"] is True
    assert rows["27°C"]["q_bayes_posterior"] == 0.0
    assert rows["27°C"]["q_posterior_observed_constraints_hard_zero"] is True
    assert rows["28°C"]["q_bayes_posterior"] >= 0.0
    assert rows["29°C"]["q_bayes_posterior"] >= 0.0
    assert rows["30°C"]["q_bayes_posterior"] > rows["29°C"]["q_bayes_posterior"]
    assert (
        rows["28°C"]["q_bayes_posterior"]
        + rows["29°C"]["q_bayes_posterior"]
        + rows["30°C"]["q_bayes_posterior"]
    ) == pytest.approx(1.0)
    assert rows["30°C"]["temperature_momentum_likelihood_applied"] is True
    assert rows["30°C"]["q_bayes_posterior_pre_temperature_momentum_likelihood"] == pytest.approx(0.25)
    assert item["fact_provenance"]["temperature_momentum_likelihood"]["candidate_bucket"] == "30°C"
    assert item["fact_provenance"]["temperature_momentum_likelihood"]["previous_top_bucket"] == "29°C"
    assert item["model_top_label"] == "30°C"
    assert item["target_bucket"] == "30°C"
    assert item["target_bucket_selection"]["target_bucket"] == "30°C"
    assert item["posterior_target_bucket_selection"]["target_bucket"] == "30°C"
    assert item["target_bucket_selection"]["target_bucket_selection_source"] == (
        "event_fact_latest.hard_source_constrained_posterior_top1"
    )
    assert item["temperature_trajectory_status"]["available"] is True
    assert item["temperature_path_distribution"]["available"] is True
    assert item["target_bucket_outlook"]["target_bucket"] == "30°C"
    assert item["target_realization_capital_plan"]["target_bucket"] == "30°C"
    assert item["auto_trade_ssot"]["main_candidate"]["outcome"] == "30°C"


def test_fact_write_materializes_d0_observed_feasibility_into_distribution(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-23-2026"
    payload = {
        "slug": slug,
        "event_date": "2026-06-23",
        "run_date": "2026-06-23",
        "title": "Highest temperature in Chengdu on June 23?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "event_tz": "Asia/Shanghai",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_refreshed_at": "2026-06-23T04:08:01+00:00",
        "weather_likelihood_features_compact": {
            "observed_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "observed_today_max_trusted": True,
            "observed_temp_source": "official_daily_max",
            "observed_today_max_source": "official_daily_max",
            "zuuu_metar_raw_awc_latest_at": "2026-06-23T04:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 28.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-23T04:00:00+00:00",
        },
        "distribution": [
            {
                "outcome": "28°C",
                "q_bayes_posterior": 0.50,
                "q_temperature_bucket_posterior": 0.50,
                "q_raw": 0.50,
                "q_bayes_physical_likelihood_observed_prob": 0.20,
                "best_bid": 0.25,
                "best_ask": 0.27,
            },
            {
                "outcome": "29°C",
                "q_bayes_posterior": 0.30,
                "q_temperature_bucket_posterior": 0.30,
                "q_raw": 0.30,
                "q_bayes_physical_likelihood_observed_prob": 0.80,
                "best_bid": 0.30,
                "best_ask": 0.33,
            },
            {
                "outcome": "30°C",
                "q_bayes_posterior": 0.20,
                "q_temperature_bucket_posterior": 0.20,
                "q_raw": 0.20,
                "q_bayes_physical_likelihood_observed_prob": 0.10,
                "best_bid": 0.16,
                "best_ask": 0.20,
            },
        ],
    }

    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-23",
        slug=slug,
        event_date="2026-06-23",
        fact_as_of="2026-06-23T04:08:01+00:00",
        model_snapshot_id=12,
        market_snapshot_id=23,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload,
    )
    row = conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    conn.close()
    item = json.loads(row["fact_json"])
    rows = {entry["outcome"]: entry for entry in item["distribution"]}

    assert item["d0_observed_feasibility_guard_applied"] is True
    assert item["fact_provenance"]["d0_observed_feasibility_guard"]["today_key"] == "2026-06-23"
    assert rows["28°C"]["observed_feasibility_applied"] is True
    assert rows["28°C"]["q_bayes_posterior"] == pytest.approx(0.2777777778)
    assert rows["29°C"]["q_bayes_posterior"] == pytest.approx(0.6666666667)
    assert rows["30°C"]["q_bayes_posterior"] == pytest.approx(0.0555555556)
    assert rows["29°C"]["q_temperature_bucket_posterior"] == pytest.approx(0.6666666667)
    assert item["target_bucket"] == "29°C"
    assert item["target_bucket_selection"]["target_bucket"] == "29°C"
    assert item["auto_trade_ssot"]["main_candidate"]["outcome"] == "29°C"


def test_safe_model_update_stats_uses_short_ttl_cache(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_model_update_stats(limit: int = 120) -> dict:
        calls["n"] += 1
        return {"models": [{"limit": limit, "call": calls["n"]}]}

    monkeypatch.setattr(server, "MODEL_UPDATE_STATS_CACHE_SEC", 30)
    monkeypatch.setattr(server, "model_update_stats", fake_model_update_stats)
    with server._MODEL_UPDATE_STATS_CACHE_LOCK:
        server._MODEL_UPDATE_STATS_CACHE.clear()

    first = server._safe_model_update_stats(limit=48)
    second = server._safe_model_update_stats(limit=48)
    third = server._safe_model_update_stats(limit=120)

    assert calls["n"] == 2
    assert first["models"][0]["call"] == 1
    assert second["models"][0]["call"] == 1
    assert second["cache_hit"] is True
    assert third["models"][0]["limit"] == 120


def test_model_update_stats_route_uses_cached_priority_payload() -> None:
    server_text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    route_start = server_text.index('if path == "/api/model-update-stats":')
    route_end = server_text.index('if path == "/api/data-source-health":', route_start)
    route_block = server_text[route_start:route_end]

    assert '"/api/model-update-stats",' in server_text
    assert "_safe_model_update_stats(limit=240)" in route_block
    assert "model_update_stats()" not in route_block


def test_model_update_release_window_waiting_source_copy_is_not_conflicting() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderModelUpdateBoard")
    end = text.index("function hasModelUpdateStatsPayload", start)
    block = text[start:end]

    assert "const releaseWindowDue = Number.isFinite(nextUpdateTs) && nextUpdateTs <= Date.now();" in block
    assert "const releaseAwaitingSource = releaseWindowDue && ['not_yet_due', 'no_files', 'partial'].includes(releaseStatus);" in block
    assert "releaseStatus === 'partial' ? '窗内补齐中' : '窗内等待源'" in block
    assert "statusLabel = releaseWindowDue ? '窗内等待源' : '未到发布时间';" in block
    assert "statusLabel = releaseWindowDue ? '窗内等待源' : '源未发布';" in block
    assert "<span>${escapeHtml(releaseWindowMetricLabel)}</span><strong>${escapeHtml(releaseWindowLabel)}</strong>" in block
    assert "reasonText = releaseStatusReasonLabel(" in block
    assert "official_run_prefix_empty" in block
    assert "官方run目录为空" in block
    assert "official_peak_window_forecast_hours_not_available" in block
    assert "官方run存在，峰值小时未出齐" in block
    assert "gfs_official_prefix_key_count" in block


def test_ui_snapshot_returns_busy_shape_when_cold_rebuild_inflight() -> None:
    cache_key = "2099-01-01:10"
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.pop(cache_key, None)
    with server._UI_SNAPSHOT_REBUILD_LOCK:
        mark_ui_snapshot_rebuilding(cache_key)
    try:
        payload = server.ui_snapshot("2099-01-01", 10)
    finally:
        with server._UI_SNAPSHOT_REBUILD_LOCK:
            server._UI_SNAPSHOT_REBUILDING.pop(cache_key, None)

    assert payload["ok"] is True
    assert payload["busy"] is True
    assert payload["cache_rebuild_in_flight"] is True
    assert payload["cache_rebuild_status"]["active"] is True
    assert payload["cache_rebuild_elapsed_ms"] >= 0
    assert payload["facts"]["items"] == []
    assert payload["facts"]["source"] == "ui_snapshot_rebuild_in_flight"


def test_ui_snapshot_rebuild_status_records_last_duration() -> None:
    cache_key = "summary:unit-rebuild-status:5"
    with server._UI_SNAPSHOT_REBUILD_LOCK:
        server._UI_SNAPSHOT_REBUILDING.pop(cache_key, None)
        server._UI_SNAPSHOT_REBUILD_LAST.pop(cache_key, None)
    assert server._ui_snapshot_rebuild_start(cache_key) is True
    try:
        active_payload = server._ui_snapshot_busy_payload(
            run_date_key="2099-01-01",
            limit_val=5,
            summary_mode=True,
            cache_key=cache_key,
        )
        assert active_payload["cache_rebuild_status"]["active"] is True
        assert active_payload["cache_rebuild_elapsed_ms"] >= 0
    finally:
        server._ui_snapshot_rebuild_done(cache_key)

    done_payload: dict = {}
    server._attach_ui_snapshot_rebuild_status(done_payload, cache_key)
    assert done_payload["cache_rebuild_status"]["active"] is False
    assert done_payload["cache_rebuild_last_duration_ms"] >= 0


def test_facts_materialize_lock_state_distinguishes_active_and_stale(tmp_path, monkeypatch) -> None:
    lock_dir = tmp_path / "facts_materialize.lock"
    monkeypatch.setattr(server, "FACTS_MATERIALIZE_LOCK_DIR", lock_dir)
    monkeypatch.setattr(server, "FACTS_MATERIALIZE_LOCK_META_PATH", lock_dir / "meta.json")
    monkeypatch.setattr(
        server,
        "_process_command_for_pid",
        lambda _pid: "/usr/bin/python /Users/xiong/weather/ui/server.py --materialize-facts",
    )
    with server._UI_MATERIALIZE_STATUS_CACHE_LOCK:
        server._UI_MATERIALIZE_STATUS_CACHE = (0.0, {})
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    (lock_dir / "meta.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "mode": "facts_materialize_cli",
                "started_at": server.now_text(),
                "facts_date": "2026-07-01",
                "facts_days": 4,
                "write_scope": "chengdu_zuuu",
            }
        ),
        encoding="utf-8",
    )

    active = server._facts_materialize_lock_state()

    assert active["exists"] is True
    assert active["active"] is True
    assert active["stale"] is False
    assert active["facts_date"] == "2026-07-01"
    assert active["write_scope"] == "chengdu_zuuu"

    monkeypatch.setattr(server, "_process_command_for_pid", lambda _pid: "/usr/bin/python other.py")
    with server._UI_MATERIALIZE_STATUS_CACHE_LOCK:
        server._UI_MATERIALIZE_STATUS_CACHE = (0.0, {})

    stale = server._facts_materialize_lock_state()

    assert stale["active"] is False
    assert stale["stale"] is True
    assert stale["stale_reason"] == "pid_command_not_materialize"


def test_ui_endpoint_metrics_record_slow_paths(monkeypatch) -> None:
    monkeypatch.setattr(server, "UI_SLOW_REQUEST_LOG_SEC", 0.1)
    with server._UI_ENDPOINT_METRICS_LOCK:
        server._UI_ENDPOINT_METRICS.clear()

    server._record_ui_endpoint_observation("/api/latest?x=1", method="GET", elapsed_sec=0.25)

    metrics = server._ui_endpoint_metrics_snapshot()
    latest = next(item for item in metrics if item["path"] == "/api/latest")
    assert latest["count"] == 1
    assert latest["slow_count"] == 1
    assert latest["last_elapsed_ms"] >= 250


def test_ui_snapshot_summary_materialize_active_returns_cached_snapshot(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    sqlite3.connect(db_path).close()
    dirty_stamp = tmp_path / "dirty.stamp"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_DIRTY_STAMP_PATH", dirty_stamp)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 30)
    monkeypatch.setattr(server, "_facts_materialize_lock_state", lambda: {"exists": True, "active": True})
    monkeypatch.setattr(
        server,
        "_ui_snapshot_rebuild_start",
        lambda _cache_key: (_ for _ in ()).throw(AssertionError("materialize guard should not rebuild")),
    )
    cache_key = "summary:2099-02-01:24"
    cached_payload = {
        "ok": True,
        "summary_mode": True,
        "run_date": "2099-02-01",
        "facts": {"items": [{"slug": "cached_under_materialize"}], "total": 1, "page_size": 5},
    }
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE[cache_key] = (time.time() - 20.0, cached_payload)
    dirty_stamp.write_text("new fact", encoding="utf-8")
    try:
        payload = server.ui_snapshot_summary("2099-02-01", 5)
    finally:
        with server._UI_SNAPSHOT_CACHE_LOCK:
            server._UI_SNAPSHOT_CACHE.pop(cache_key, None)

    assert payload["ok"] is True
    assert payload["cache_hit"] is True
    assert payload["cache_stale"] is True
    assert payload["cache_stale_reason"] == "facts_materialize_active"
    assert payload["cache_rebuild_skipped_reason"] == "facts_materialize_active"
    assert payload["facts"]["items"][0]["slug"] == "cached_under_materialize"
    assert payload["runtime_health"]["facts_materialize"]["active"] is True


def test_frontend_uses_stale_ui_snapshot_items_during_rebuild() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    summary_start = text.index("async function fetchUiSnapshotSummaryRunsPage")
    summary_end = text.index("async function fetchUiSnapshotFullRunsPage", summary_start)
    summary_block = text[summary_start:summary_end]
    full_start = summary_end
    full_end = text.index("async function fetchRunsPage", full_start)
    full_block = text[full_start:full_end]
    model_start = text.index("function refreshModelMemberSnapshotBoard")
    model_end = text.index("function buildMeanBucketBarChart", model_start)
    model_block = text[model_start:model_end]

    assert "function snapshotHasUsableRunsItems" in text
    assert "function snapshotItemIsDisplayUsable" in text
    assert "return !isWeatherMarketExpiredForDisplay(item, nowMs);" in text
    assert "return items.some((item) => snapshotItemIsDisplayUsable(item, nowMs));" in text
    assert "(snapshot.busy || snapshot.cache_rebuild_in_flight) && !snapshotHasUsableRunsItems(snapshot)" in summary_block
    assert "ui_snapshot_summary_rebuild_in_flight" in summary_block
    assert "(snapshot.busy || snapshot.cache_rebuild_in_flight) && !snapshotHasUsableRunsItems(snapshot)" in full_block
    assert "ui_snapshot_full_rebuild_in_flight" in full_block
    assert "if (snapshot && (snapshot.busy || snapshot.cache_rebuild_in_flight)) return;" in model_block


def test_ui_snapshot_summary_trim_scopes_to_chengdu_and_compacts_items() -> None:
    payload = {
        "ok": True,
        "summary_mode": True,
        "facts": {
            "source": "unit",
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-2-2026",
                    "city": "Chengdu",
                    "station_id": "ZUUU",
                    "event_date": "2026-07-02",
                    "distribution": [
                        {
                            "outcome": "31C",
                            "q_temperature_bucket_posterior": 0.7,
                            "yes_token": "heavy-token",
                            "q_ensemble_qm_meta": {"large": "x" * 1000},
                        }
                    ],
                    "bucket_probabilities": [{"outcome": "31C", "yes_token": "heavy-token"}],
                },
                {
                    "slug": "highest-temperature-in-london-on-july-2-2026",
                    "city": "London",
                    "station_id": "EGLL",
                    "event_date": "2026-07-02",
                    "distribution": [{"outcome": "24C", "q_temperature_bucket_posterior": 0.5}],
                },
            ],
        },
    }

    trimmed = server._trim_ui_snapshot_summary_payload(payload, 24, city_filter="chengdu")

    items = trimmed["facts"]["items"]
    assert trimmed["ui_snapshot_city_filter"] == "chengdu"
    assert trimmed["facts"]["city_filter"] == "chengdu"
    assert trimmed["facts"]["total"] == 1
    assert len(items) == 1
    assert items[0]["station_id"] == "ZUUU"
    assert "bucket_probabilities" not in items[0]
    assert "yes_token" not in items[0]["distribution"][0]
    assert "q_ensemble_qm_meta" not in items[0]["distribution"][0]


def test_frontend_ui_snapshot_summary_requests_scope_to_chengdu() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    summary_start = text.index("async function fetchUiSnapshotSummaryRunsPage")
    summary_end = text.index("async function fetchUiSnapshotFullRunsPage", summary_start)
    summary_block = text[summary_start:summary_end]
    full_start = summary_end
    full_end = text.index("async function fetchRunsPage", full_start)
    full_block = text[full_start:full_end]
    model_start = text.index("function refreshModelMemberSnapshotBoard")
    model_end = text.index("function buildMeanBucketBarChart", model_start)
    model_block = text[model_start:model_end]

    assert "const UI_SNAPSHOT_CITY_SCOPE = 'chengdu';" in text
    assert "function uiSnapshotCityScopeParam()" in text
    assert "const cityParam = uiSnapshotCityScopeParam();" in summary_block
    assert "limit=${limit}${cityParam}${refreshParam}" in summary_block
    assert "const cityParam = uiSnapshotCityScopeParam();" in full_block
    assert "limit=${limit}${cityParam}${refreshParam}" in full_block
    assert "const cityParam = uiSnapshotCityScopeParam();" in model_block
    assert "limit=12${cityParam}" in model_block


def test_frontend_regular_polling_does_not_force_summary_rebuild_or_reset_viewport() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    summary_start = text.index("async function fetchUiSnapshotSummaryRunsPage")
    summary_end = text.index("async function fetchUiSnapshotFullRunsPage", summary_start)
    summary_block = text[summary_start:summary_end]
    full_start = summary_end
    full_end = text.index("async function fetchRunsPage", full_start)
    full_block = text[full_start:full_end]
    live_start = text.index("async function refreshLiveUiSnapshotCoreNumbers")
    live_end = text.index("function scheduleEventIntelligenceLiveSnapshotRefresh", live_start)
    live_block = text[live_start:live_end]
    model_start = text.index("function refreshModelMemberSnapshotBoard")
    model_end = text.index("function buildMeanBucketBarChart", model_start)
    model_block = text[model_start:model_end]
    auto_start = text.index("function startAutoRefresh")
    auto_end = text.index("async function refreshScanStatus", auto_start)
    auto_block = text[auto_start:auto_end]

    assert "const USER_VIEWPORT_REFRESH_DEFER_MS = 45000;" in text
    assert "function shouldDeferViewportRerender()" in text
    assert "window.addEventListener('scroll', markUserViewportInteraction, { passive: true });" in text
    assert "markProgrammaticScroll();" in text
    assert "const refreshParam = params && params.refresh === true ? '&refresh=1' : '';" in summary_block
    assert "const refreshParam = params && params.refresh === true ? '&refresh=1' : '';" in full_block
    assert "params.refresh === false ? '' : '&refresh=1'" not in summary_block
    assert "params.refresh === false ? '' : '&refresh=1'" not in full_block
    assert "if (shouldDeferViewportRerender()) return false;" in live_block
    assert "setSyncIndicator('idle', '阅读中暂存更新'" in live_block
    assert "refresh: false" in live_block
    assert "refresh: eventIntelligenceInAwcHourlyUpdateWindow()" not in live_block
    assert "refresh=1" not in model_block
    assert "if (shouldDeferViewportRerender()) {" in auto_block
    assert "setSyncIndicator('idle', '阅读中暂停刷新'" in auto_block


def test_frontend_surfaces_physical_source_impact_and_rebuild_metrics() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "function physicalLikelihoodTargetGroupRows" in text
    assert "physicalLikelihoodTargetLabelFromItem(item || {}, top.label || '')" in text
    assert "<strong>来源 → 似然贡献 → 对目标桶影响</strong>" in text
    assert "相对其他桶 ${physicalLikelihoodSignedNumber(rel, 3)} LL" in text
    assert "function summarizeUiSnapshotRebuildStatus" in text
    assert "function recordUiSnapshotRebuildStatus" in text
    assert "<div class=\"update-combo-summary-label\">Summary缓存</div>" in text


def test_ui_snapshot_dirty_stamp_invalidates_cached_snapshot(tmp_path, monkeypatch) -> None:
    stamp = tmp_path / "ui_snapshot_dirty.stamp"
    monkeypatch.setattr(server, "UI_SNAPSHOT_DIRTY_STAMP_PATH", stamp)

    assert server._ui_snapshot_cache_is_dirty((time.time(), {"ok": True})) is False
    server._touch_ui_snapshot_dirty_stamp("unit_test")
    old_cache_entry = (stamp.stat().st_mtime - 10.0, {"ok": True})
    fresh_cache_entry = (stamp.stat().st_mtime + 10.0, {"ok": True})

    assert server._ui_snapshot_cache_is_dirty(old_cache_entry) is True
    assert server._ui_snapshot_cache_is_dirty(fresh_cache_entry) is False


def test_ui_server_pidfile_clear_only_removes_owned_pid(tmp_path) -> None:
    pid_path = tmp_path / "server.pid"

    server._write_ui_server_pidfile(pid_path, 12345)
    assert pid_path.read_text(encoding="utf-8").strip() == "12345"

    server._clear_ui_server_pidfile(pid_path, 54321)
    assert pid_path.exists()

    server._clear_ui_server_pidfile(pid_path, 12345)
    assert not pid_path.exists()


def test_start_command_delegates_ui_server_to_launchd() -> None:
    text = (ROOT / "start.command").read_text(encoding="utf-8")

    assert 'nohup "$PY" "$ROOT/ui/server.py"' not in text
    assert 'echo "$SERVER_PID" > "$PIDFILE"' not in text
    assert "launchctl kickstart -k" in text
    assert "com.xiong.weather.ui" in text


def test_services_health_uses_index_friendly_market_tick_latest_query() -> None:
    text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")

    assert "SELECT MAX(created_at) AS ts FROM event_market_ticks" not in text
    assert "FROM event_market_ticks\n                    WHERE created_at IS NOT NULL" in text
    assert "ORDER BY id DESC\n                    LIMIT 1" in text
    assert 'PM_AUTO_TRADE_HEALTH_DECISION_ROW_LIMIT", "300"' in text
    assert 'PM_AUTO_TRADE_JOB_BLOCKER_MAX_ROWS' in text
    assert "PM_AUTO_TRADE_HEALTH_INCLUDE_RECENT_JOB_FAILURES" in text
    assert 'PM_AUTO_TRADE_BLOCKER_MAX_ROWS", "300"' in text
    assert "limit=1200" not in text


def test_chengdu_target_outlook_ui_uses_single_backend_outlook() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    server_text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")

    assert "answer_cards" in text
    assert "probability_bands" in text
    assert "primary_conclusion" in text
    assert "second_target_outlook" in text
    assert "eventIntelSecondTargetOutlookFromPayload" in text
    assert "renderSecondTargetOutlook" in text
    assert "targetSemantics.pathNoun" in text
    assert "达到目标下沿" in text
    assert "第二目标桶" in text
    assert "posteriorSecond" not in text
    assert "候选桶 ${candidateBucket}" not in text
    assert "候选ask" not in text
    assert "盘口确认候选" not in text
    assert "模型确认候选" not in text
    assert "候选过贵" not in text
    assert "const targetOutlookHtml = renderTargetBucketOutlook(targetOutlookForHub);" in text
    assert "targetOutlookHtml || renderTemperatureRealizationReliability" in text
    assert "target_bucket_outlook: objectOrEmpty(item.target_bucket_outlook)" in text
    assert "eventIntelHasSummarySsot" in text
    assert "source.includes('ui_snapshot_ssot')" in text
    assert "target_bucket_outlook.second_target_outlook" in server_text
    assert "candidate_label = posterior_label or market_label" not in server_text
    assert "候选 ask" not in server_text


def test_target_progress_omits_observation_metrics_already_shown_in_trajectory_header() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderTargetBucketTrajectoryProgress")
    end = text.index("function renderTemperatureRealizationReliability", start)
    block = text[start:end]
    pace_start = text.index("function eventIntelTargetPaceProgress")
    pace_end = text.index("function renderTargetBucketTrajectoryProgress", pace_start)
    pace_block = text[pace_start:pace_end]
    peak_start = text.index("function resolveChengduExpectedPeakHourLocal")
    peak_end = text.index("function isWeatherMarketExpiredForDisplay", peak_start)
    peak_block = text[peak_start:peak_end]

    assert "metric('当前实况', observedNow)" not in block
    assert "metric('已观测最高', observedMax)" not in block
    assert "metric('目标下沿', targetEdge)" in block
    assert "<span>节奏差</span>" in block
    assert "实线=实际升温节奏" in block
    assert "实线=当前实况升温节奏" not in block
    assert 'aria-label="实际升温率与目标节奏融合进度"' in block
    assert "Number.isFinite(observedNow) ? targetEdge - observedNow" in block
    observed_max_block = block[
        block.index("const observedMax = eventIntelFirstFiniteNumber(") : block.index("const observedNow = eventIntelFirstFiniteNumber(")
    ]
    assert observed_max_block.index("outlook.observed_today_max_c") < observed_max_block.index("metrics.observed_today_max_c")
    assert observed_max_block.index("metrics.observed_today_max_c") < observed_max_block.index("status.observed_today_max_c")
    assert "observed_path_temp_c" not in observed_max_block
    assert "observed_temp_c" not in observed_max_block
    observed_now_block = block[
        block.index("const observedNow = eventIntelFirstFiniteNumber(") : block.index("const forecastPeak", block.index("const observedNow = eventIntelFirstFiniteNumber("))
    ]
    assert observed_now_block.index("outlook.observed_temp_c") < observed_now_block.index("metrics.observed_temp_c")
    assert observed_now_block.index("metrics.observed_temp_c") < observed_now_block.index("status.observed_temp_c")
    assert "observed_path_temp_c" not in observed_now_block
    peak_hour_block = pace_block[
        pace_block.index("const peakHour = eventIntelFirstFiniteNumber(") : pace_block.index("const hoursToPeak", pace_block.index("const peakHour = eventIntelFirstFiniteNumber("))
    ]
    assert peak_hour_block.index("warming.peak_hour_local") < peak_hour_block.index("metrics.local_peak_hour")
    assert peak_hour_block.index("metrics.local_peak_hour") < peak_hour_block.index("metrics.expected_peak_hour_local")
    assert peak_hour_block.index("metrics.expected_peak_hour_local") < peak_hour_block.index("status.local_peak_hour")
    assert peak_hour_block.index("status.local_peak_hour") < peak_hour_block.index("status.expected_peak_hour_local")
    assert "16" not in pace_block
    assert "const table =" not in peak_block
    assert "return table[month]" not in peak_block
    assert "observed_path_temp_c ?? metrics.observed_temp_c" not in text
    assert "metrics.observed_temp_c ?? metrics.observed_path_temp_c" not in text
    assert "row.observed_path_temp_c ?? row.observed_temp_c" not in text
    assert "row.observed_temp_c ?? row.observed_path_temp_c" not in text
    assert "stage.observed_path_temp_c ?? stage.observed_temp_c" not in text
    assert "stage.observed_temp_c ?? stage.observed_path_temp_c" not in text
    assert "当前最高" not in text
    assert "当前实况/最高" not in text
    assert "function eventIntelCurrentObservedMetricValue" in text
    current_helper = text[
        text.index("function eventIntelCurrentObservedMetricValue") : text.index("function eventIntelStageObservedMetricValue")
    ]
    assert "observed_path_temp_c" not in current_helper
    stage_helper = text[
        text.index("function eventIntelStageObservedMetricValue") : text.index("function eventIntelTemperatureRiseRateValue")
    ]
    assert "observed_current_temp_c" in stage_helper
    assert "observed_today_max_floor" in stage_helper
    assert "return Number.NaN" in stage_helper


def test_latest_fact_allows_wu_twc_intraday_correction_but_preserves_awc() -> None:
    existing_wu = {
        "weather_likelihood_features": {
            "observed_today_max_c": 28.0,
            "observed_today_max_source": "wu_twc_observed_intraday_current_metric_max_since7am",
            "wu_twc_observed_intraday_today_max_c": 25.0,
            "wu_twc_observed_intraday_today_max_source": "wu_twc_observed_intraday_history",
        }
    }
    incoming_wu = {
        "weather_likelihood_features": {
            "observed_today_max_c": 25.0,
            "observed_today_max_source": "wu_twc_observed_intraday_history",
            "wu_twc_observed_intraday_today_max_c": 25.0,
            "wu_twc_observed_intraday_today_max_source": "wu_twc_observed_intraday_history",
        }
    }

    assert server._event_fact_latest_update_allowed(
        existing_fact_as_of="2026-06-25T00:50:38+00:00",
        existing_payload=existing_wu,
        incoming_fact_as_of="2026-06-25T00:50:47+00:00",
        incoming_payload=incoming_wu,
    )

    existing_awc = {
        "weather_likelihood_features": {
            "observed_today_max_c": 28.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_today_max_c": 28.0,
        }
    }
    assert not server._event_fact_latest_update_allowed(
        existing_fact_as_of="2026-06-25T00:50:38+00:00",
        existing_payload=existing_awc,
        incoming_fact_as_of="2026-06-25T00:50:47+00:00",
        incoming_payload=incoming_wu,
    )


def test_trade_explanation_keeps_posterior_target_separate_from_path_probability() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "目标桶</span>" in text
    assert "eventIntelFinalBucketProbabilityLabel(targetLabel)" in text
    assert "targetLabel || topLabel" not in text[text.index("function formatDailyTradeAdviceSummary"):text.index("function isPosteriorSettlementCandidate", text.index("function formatDailyTradeAdviceSummary"))]
    assert "Top 3 最高温落桶后验趋势" in text
    assert "重构 Bayes 概率变化" in text
    assert "posterior_temperature_distribution.bucket_probabilities" in text
    assert "CTP源组件" in text
    assert "source_components" in text
    assert "group shrinkage" in text
    assert "贡献=P×w" not in text
    assert "先验 × 似然" not in text
    assert "未结算" in text
    assert "非当前实况" in text
    assert "模型校准组件最高桶" in text
    assert "Weather.com/TWC 校准软源" in text
    assert "QM校正最高桶" not in text
    assert "Top 3 温度桶后验趋势" not in text
    assert "目标桶：${targetBucketCompact}" in text
    assert "${targetSemantics.pathNoun}目标" not in text
    assert "路径触达概率目标" not in text
    assert "触达单桶概率" not in text
    assert "仅作路径/执行参考，不改写最高温落桶后验" in text


def test_ctp_bucket_table_shows_refactored_bayes_probability_change() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    header_start = text.index("header.innerHTML = [")
    header_end = text.index("const note = document.createElement('div');", header_start)
    header_block = text[header_start:header_end]
    chain_start = text.index("const bayesFormulaStepCard =")
    chain_end = text.index("const stateTags = [];", chain_start)
    chain_block = text[chain_start:chain_end]

    assert "重构 Bayes 概率变化" in header_block
    assert "p(T|D) ∝ prior(T) × likelihood(T) × D0/硬源约束" in text
    assert "先验→似然→约束→后验" in text
    assert "function eventIntelCtpContributionRows" in text
    assert "function eventIntelCtpEffectivePrecisionTotal" in text
    assert "regime_v1 cap占比" in text
    assert "精度占比 / 目标桶支持 / regime_v1 cap" in text
    assert "label: '先验/背景'" in chain_block
    assert "label: '预测似然'" in chain_block
    assert "label: 'D0/硬源'" in chain_block
    assert "label: '落桶后验'" in chain_block
    assert "bayes-step-card-label" in chain_block
    assert "bayes-step-card-value" in chain_block
    assert "bayes-step-card-delta" in chain_block
    assert "沿用上一步" in chain_block
    assert "约束生效" in chain_block
    assert "推高概率" in chain_block
    assert "压低概率" in chain_block
    assert "formulaSummary" in chain_block
    assert "bayes-formula-steps" in chain_block
    assert "组件分布 → group shrinkage → D0/硬源约束 → bucket_probabilities" not in header_block


def test_probability_change_track_is_inside_top3_distribution_container() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    distribution_start = text.index('id="distribution-section-${cardId}"')
    distribution_end = text.index("${renderEventBayesIntelligencePanel(item, cardId)}", distribution_start)
    distribution_block = text[distribution_start:distribution_end]
    posterior_start = text.index("function renderEventBayesPosteriorStep")
    posterior_end = text.index("function renderEventBayesEvidenceFlow", posterior_start)
    posterior_block = text[posterior_start:posterior_end]
    live_start = text.index("function renderEventBayesPosteriorLivePanel")
    live_end = text.index("function eventBayesPriorRows", live_start)
    live_block = text[live_start:live_end]

    assert "概率变化轨迹（相对上一快照）" in distribution_block
    assert "bucket-qstats-history-${cardId}" in distribution_block
    assert distribution_block.index('id="distribution-slot-${cardId}"') < distribution_block.index("概率变化轨迹（相对上一快照）")
    assert "bucket-qstats-history-${escapeHtml(String(cardId || ''))}" not in posterior_block
    assert "renderFusedBucketTrendChart" not in live_block
    assert "renderPosteriorMiniTimeline" not in live_block
    assert "查看各温度桶 CTP 落桶后验概率表" in text


def test_bayes_prior_layout_prioritizes_ctp_prior_core_over_supporting_context() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderEventBayesPriorStep")
    end = text.index("function eventBayesLikelihoodChannels", start)
    block = text[start:end]

    assert "function eventBayesCtpPriorRows" in text
    assert "const ctpPriorRows = eventBayesCtpPriorRows(item, 3);" in block
    assert "const priorRows = ctpPriorRows.length ? ctpPriorRows : eventBayesPriorRows(item, 3);" in block
    assert "priorRowsSource = ctpPriorRows.length ? 'CTP source_components' : '旧 distribution 先验兜底'" in block
    assert "title: '对应上方：先验/背景'" in block
    assert "bayes-evidence-prior-grid is-core-first" in block
    assert ".bayes-prior-support-row {" in text
    assert "grid-template-columns:minmax(220px, .68fr) minmax(320px, 1.32fr);" in text
    assert ".bayes-evidence-prior-grid.is-core-first {\n\t      grid-template-columns:1fr;" in text
    assert ".bayes-prior-core {" in text
    assert ".bayes-prior-explain-card {" in text
    assert "const priorComponentLabel = topPrior && topPrior.componentLabel" in block
    assert "bayes-prior-core event-realtime-block" in block
    assert "data-realtime-key=\"${escapeHtml(`bayes-stage:${String(cardId || eventIntelEventDate(item) || 'latest')}:prior`)}\"" in block
    assert "CTP 先验/背景主结论" in block
    assert "来源组件" in block
    assert "先验解释因子" in block
    assert "慢变量，不覆盖实况" in block
    assert "辅助 Top3 · ${priorRowsSource}" in block
    assert "气候说明增量" in block
    assert "盆地说明增量" in block
    assert "为什么：" not in block
    assert "气候背景组件桶" in block
    assert "bayes-prior-support-row" in block
    assert "bayes-prior-core" in block
    assert block.index("const priorCoreHtml") < block.index("const top3Html")
    assert block.index("${priorCoreHtml}") < block.index("bayes-prior-support-row")
    assert block.index("${priorExplainHtml}") < block.index("${top3Html}")


def test_bucket_history_merges_snapshot_and_fetched_series() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    history_start = text.index("async function updateMostLikelyHistory")
    history_end = text.index("function settlementErrorLabel", history_start)
    history_block = text[history_start:history_end]

    assert "function snapshotBucketSeriesForItem" in text
    assert "function mergeBucketSeriesRows" in text
    assert "const snapshotSeries = snapshotBucketSeriesForItem(item);" in history_block
    assert "const combinedSeries = mergeBucketSeriesRows(snapshotSeries, fetchedSeries);" in history_block
    assert "bucketSeries: mergeCurrentDistributionIntoBucketSeries(combinedSeries, item, weightings[idx].key)" in history_block


def test_initial_load_does_not_fetch_facts_range() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    load_start = text.index("async function load(")
    load_end = text.index("function renderFromCache", load_start)
    load_block = text[load_start:load_end]

    assert "/api/facts-range" not in load_block
    assert "let reasonHistoryRuns = Array.isArray(cachedSnapshot.reasonHistoryRuns)" in load_block


def test_lcb_current_runs_page_uses_snapshot_only_fast_path() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    summary_start = text.index("async function fetchUiSnapshotSummaryRunsPage")
    summary_end = text.index("async function fetchUiSnapshotFullRunsPage", summary_start)
    summary_block = text[summary_start:summary_end]
    fetch_start = text.index("async function fetchRunsPage")
    fetch_end = text.index("async function fetchRunsRange", fetch_start)
    fetch_block = text[fetch_start:fetch_end]

    assert "function shouldUseSnapshotOnlyRuns" in text
    assert "function snapshotOnlyRunsPage" in text
    assert "const preferCacheParam = '&prefer_cache=1';" in summary_block
    assert "${refreshParam}${preferCacheParam}&_=" in summary_block
    assert "ui_snapshot_summary_empty_items" in fetch_block
    assert "err.snapshotOnly = true;" in fetch_block
    assert "err.snapshotOnlySource = 'ui_snapshot_summary';" in fetch_block
    assert "throw err;" in fetch_block
    assert "return snapshotOnlyRunsPage(params, 'ui_snapshot_summary_page_only');" in fetch_block
    assert fetch_block.index("throw err;") < fetch_block.index("if (shouldUseSnapshotOnlyRuns(params))")
    assert fetch_block.index("if (shouldUseSnapshotOnlyRuns(params))") < fetch_block.index(
        "if (typeof LCB_ONLY_UI !== 'undefined' && LCB_ONLY_UI)"
    )


def test_lcb_initial_load_does_not_fall_back_to_runs_range() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    load_start = text.index("async function load(")
    load_end = text.index("function renderFromCache", load_start)
    load_block = text[load_start:load_end]
    snapshot_start = load_block.index("if (snapshotOnlyRunsMode) {")
    snapshot_end = load_block.index("} else {\n              window.__runsFetchMode = 'range';", snapshot_start)
    snapshot_block = load_block[snapshot_start:snapshot_end]

    assert "const snapshotOnlyRunsMode = typeof LCB_ONLY_UI !== 'undefined' && LCB_ONLY_UI && !showHistory;" in load_block
    assert "window.__runsFetchMode = 'snapshot';" in snapshot_block
    assert "fallbackExistingRuns" in snapshot_block
    assert "throw err;" in snapshot_block
    assert "fetchRunsRange" not in snapshot_block
    assert "if (!snapshotOnlyRunsMode && !canIncremental && useLatest" in load_block


def test_initial_load_fast_path_defers_heavy_frontend_endpoints() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    load_start = text.index("async function load(")
    load_end = text.index("function renderFromCache", load_start)
    load_block = text[load_start:load_end]
    init_start = text.index("async function init()")
    init_end = text.index("let autoRefreshTimer", init_start)
    init_block = text[init_start:init_end]

    assert "const UI_FAST_LOAD_ENABLED = true;" in text
    assert "const UI_DEFERRED_DIAGNOSTIC_DELAY_MS = 45000;" in text
    assert "const AUTO_REFRESH_MS = 60 * 1000;" in text
    assert "const EVENT_INTEL_CHENGDU_INTRADAY_CACHE_TTL_MS = 120000;" in text
    assert "const EVENT_INTEL_CHENGDU_INTRADAY_REQUEST_COOLDOWN_MS = 120000;" in text
    assert "const EVENT_INTEL_CHENGDU_KEY_REFRESH_MS = 180000;" in text
    assert "const EVENT_INTELLIGENCE_AUTO_REFRESH_MS = 120000;" in text
    assert "const EVENT_INTELLIGENCE_LIVE_SSOT_REFRESH_MS = 5000;" in text
    assert "const EVENT_INTELLIGENCE_AWC_UPDATE_REFRESH_MS = 2000;" in text
    assert "const EVENT_INTELLIGENCE_AWC_UPDATE_WINDOW_MINUTES = 20;" in text
    assert "const DATA_SOURCE_HEALTH_MIN_INTERVAL_MS = 300000;" in text
    assert "const UI_AUXILIARY_LATEST_TIMEOUT_MS = 3000;" in text
    assert "const UI_AUXILIARY_LATEST_INITIAL_DELAY_MS = UI_DEFERRED_DIAGNOSTIC_DELAY_MS;" in text
    assert "const UI_AUXILIARY_LATEST_REFRESH_MS = 5 * 60 * 1000;" in text
    assert "const UI_AUXILIARY_AUTO_TRADE_TRACE_TIMEOUT_MS = 6500;" in text
    assert "const UI_AUXILIARY_STATION_TEMP_TIMEOUT_MS = 4500;" in text
    assert "const AUTO_TRADE_REFRESH_MS = 120000;" in text
    assert "const AUTO_TRADE_TRACE_REFRESH_MS = 180000;" in text
    assert "const AUTO_TRADE_BALANCE_REFRESH_MS = 10 * 60 * 1000;" in text
    assert "const MODEL_UPDATE_STATS_MIN_INTERVAL_MS = 30000;" in text
    assert "const MODEL_UPDATE_STATS_FRONTEND_TIMEOUT_MS = 5000;" in text
    assert "function refreshModelUpdateStatsBoard(options = {})" in text
    assert "fetchJSON('/api/model-update-stats', {" in text
    assert "timeoutMs: MODEL_UPDATE_STATS_FRONTEND_TIMEOUT_MS," in text
    assert "saveDashboardCache(window.__lastPayload);" in text
    assert "setTimeout(() => refreshModelUpdateStatsBoard(), 2000);" in load_block
    assert "stationTemp: 10 * 60 * 1000," in text
    assert "const STATION_TEMP_UI_MAX_CONCURRENT = 1;" in text
    assert "fetchJSON('/api/services', { timeoutMs: 5000, retryCount: 0" in text
    assert "let stationTempDeferredTimer = null;" in text
    assert "clearTimeout(stationTempDeferredTimer);" in text
    assert "stationTempDeferredTimer = setTimeout(() => {" in text
    assert "refreshStationTemps(false).catch(() => {});" in text
    assert "stationTempTimer = setInterval(() => refreshStationTemps(false).catch(() => {}), 10 * 60 * 1000);" in text
    assert "fetchJSON(`/api/station-temp?${qs.toString()}`, {" in text
    assert "timeoutMs: UI_AUXILIARY_STATION_TEMP_TIMEOUT_MS,\n            retryCount: 0,\n            retryOnAbort: false," in text
    assert "if (!UI_FAST_LOAD_ENABLED && !force && data && data.ok && !data.forecast_time" in text
    assert "if (!UI_FAST_LOAD_ENABLED && !force && data && data.ok && data.is_recent === false)" in text
    assert "const forceKeyRefresh = !UI_FAST_LOAD_ENABLED;" in text
    assert "refreshEventIntelligencePanels({ forceIntraday: forceKeyRefresh });" in text
    assert "fetchJSONWithAuxiliaryStatus('latest', '/api/latest', {" in text
    assert "startLatestDateRefreshTicker(date);" in init_block
    assert "if (!UI_FAST_LOAD_ENABLED) {\n        try {\n          const latest = await fetchLatestDateObserved();" in init_block
    assert "fetchJSON('/api/latest'" not in init_block
    assert "const skipLatestFetch = useProvidedDate || (UI_FAST_LOAD_ENABLED && silent && incremental);" in load_block
    assert "latest = await fetchLatestDateObserved();" in load_block
    assert "function summarizeUiRuntimeHealth(source)" in text
    assert "window.__lastUiSnapshotRuntimeHealth = snapshot.runtime_health;" in text
    assert '<div class="update-combo-summary-label">UI快路径</div>' in text
    assert '<div class="update-combo-summary-label">辅助接口</div>' in text
    assert "recordAuxiliaryFetchStatus('station_temp', {" in text
    assert "recordAuxiliaryFetchStatus('auto_trade_trace', {" in text
    assert "fetchJSONWithAuxiliaryStatus(\n          'auto_trade_trace'," in text
    assert "timeoutMs: UI_AUXILIARY_AUTO_TRADE_TRACE_TIMEOUT_MS,\n            retryCount: 0," in text
    assert "setTimeout(() => refreshAutoTradeMonitor(), UI_DEFERRED_DIAGNOSTIC_DELAY_MS);" in text

    for endpoint in (
        "/api/scoreboard?days=30",
        "/api/auto-trade-daily-stats",
        "/api/model-update-stats",
        "/api/data-source-health",
        "/api/edge-bias?days=30",
        "/api/model-bias?days=30",
        "/api/place-bias?days=30",
        "/api/optimizer-status",
    ):
        assert endpoint not in load_block

    assert "safeFetch('/api/auto-trade-status', null, 4500)" in load_block
    assert "safeFetch(`/api/skips?date=${effectiveDate}&mode=${mode}`, cachedSnapshot.skips || [], 3500)" in load_block
    assert "safeFetch('/api/update-policy', updatePolicyFallback, 3500)" in load_block
    assert "const fastLoadDefersAuxiliaryFetches = UI_FAST_LOAD_ENABLED && !silent;" in load_block
    assert "useCachedAuxiliaryForFirstPaint();" in load_block
    assert "Promise.allSettled([" in load_block
    assert "safeFetch(`/api/skips?date=${effectiveDate}&mode=${mode}`, null, 3500)" in load_block
    assert "rerenderFromCache({ preserveScroll: true });" in load_block
    assert "if (shouldAutoLoadRuns && !UI_FAST_LOAD_ENABLED)" in load_block
    assert "if (!UI_FAST_LOAD_ENABLED && (!edgeBiasFinal || !edgeBiasFinal.ok))" in load_block
    assert "if (!UI_FAST_LOAD_ENABLED && (healthDateNeedsRefresh || !window.__autoTradeHealth))" in load_block
    assert "if (!skipTradeSync && !UI_FAST_LOAD_ENABLED)" in load_block

    daily_prime_pos = init_block.index("fetchJSON('/api/auto-trade-daily-stats'")
    assert init_block.rfind("if (!UI_FAST_LOAD_ENABLED)", 0, daily_prime_pos) >= 0


def test_event_core_numbers_live_poll_snapshot_ssot_without_slow_fallback() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    refresh_start = text.index("async function refreshLiveUiSnapshotCoreNumbers")
    refresh_end = text.index("function scheduleEventIntelligenceLiveSnapshotRefresh", refresh_start)
    refresh_block = text[refresh_start:refresh_end]
    schedule_start = text.index("function scheduleEventIntelligenceLiveSnapshotRefresh")
    schedule_end = text.index("function refreshEventIntelligenceKeyInfo", schedule_start)
    schedule_block = text[schedule_start:schedule_end]
    start_start = text.index("function startEventIntelligenceAutoRefresh")
    start_end = text.index("function getChengduUnifiedDecisionMount", start_start)
    start_block = text[start_start:start_end]

    assert "function eventIntelligenceInAwcHourlyUpdateWindow" in text
    assert "minute >= 58 || minute <= EVENT_INTELLIGENCE_AWC_UPDATE_WINDOW_MINUTES" in text
    assert "function eventIntelligenceLiveSnapshotDelayMs" in text
    assert "? EVENT_INTELLIGENCE_AWC_UPDATE_REFRESH_MS" in text
    assert ": EVENT_INTELLIGENCE_LIVE_SSOT_REFRESH_MS" in text
    assert "fetchUiSnapshotSummaryRunsPage({" in refresh_block
    assert "includeHistory: false" in refresh_block
    assert "includeUpcoming: true" in refresh_block
    assert "refresh: false" in refresh_block
    assert "refresh: eventIntelligenceInAwcHourlyUpdateWindow()" not in refresh_block
    assert "mergeLiveUiSnapshotItemsIntoRunsLoaded(page && page.items)" in refresh_block
    assert "rerenderFromCache({ preserveScroll: true })" in refresh_block
    assert "refreshEventIntelligencePanels({ skipIntradayRequest: true })" in refresh_block
    assert "setSyncIndicator('fresh', '实时更新'" in refresh_block
    assert "fetchRunsPage(" not in refresh_block
    assert "fetchRunsRange(" not in refresh_block
    assert "/api/facts" not in refresh_block
    assert "/api/runs-range" not in refresh_block
    assert "eventIntelligenceLiveSnapshotDelayMs()" in schedule_block
    assert "scheduleEventIntelligenceLiveSnapshotRefresh();" in schedule_block
    assert "scheduleEventIntelligenceLiveSnapshotRefresh(1200);" in start_block


def test_event_card_deep_links_are_stable_and_legacy_hashes_resolve() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "function eventCardStableId" in text
    assert "const cardId = eventCardStableId(item, cardSeq, usedCardIds);" in text
    assert "Math.random().toString(36)" not in text
    assert "function legacyRandomSuffixAnchorCandidates" in text
    assert "scrollToCurrentHashAnchor({ replaceHash: true, behavior: 'auto' });" in text
    assert "if (window.location && window.location.hash) return;" in text


def test_target_outlook_card_css_prevents_horizontal_overflow() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert ".daily-target-outlook {" in text
    assert "max-width:100%;" in text
    assert "overflow-x:hidden;" in text
    assert "grid-template-columns:minmax(0, .42fr) minmax(64px, 1fr) minmax(42px, auto);" in text
    assert ".daily-target-outlook-row > span,\n    .daily-target-outlook-row > strong" in text
    assert ".daily-target-outlook-track {\n      height:8px;" in text
    assert "      min-width:0;\n    }\n    .daily-target-outlook-fill" in text
    assert ".daily-trade-chip {\n      display:inline-flex;" in text
    assert "white-space:normal;" in text


def test_physical_trend_uses_full_temperature_stage_trajectory() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "trend.stages.slice(-6)" not in text
    assert "trend.stages.slice(-4)" not in text
    assert "const stages = Array.isArray(trend.stages) ? trend.stages : [];" in text
    assert "const trendPointSortKey = (value, fallbackIndex = 0) => {" in text
    assert ".sort((a, b) => (a.sortKey - b.sortKey));" in text
    assert "currentSortKey > latestSortKey + 0.01" in text
    assert "points.push({ label: currentLabel, temp: currentTemp" not in text
    assert "const width = Math.max(360, points.length > 1 ? 44 + (points.length - 1) * 58 : 360);" in text
    assert "const pointTempLabels = coords.map((point, index) => {" in text
    assert "${escapeHtml(`${point.temp.toFixed(1)}°C`)}" in text
    assert 'style="width:${width}px;min-width:${width}px"' in text
    assert ".event-physical-trend-meta {\n      display:grid;\n      grid-template-columns:1fr;" in text


def test_physical_evidence_hub_layout_prioritizes_summary_then_details() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert ".event-physical-priority {" in text
    assert "grid-template-columns:minmax(0, 1.18fr) minmax(260px, .82fr);" in text
    assert ".event-physical-hub.is-warm {" in text
    assert ".event-today-physical-verdict {" in text
    assert ".event-today-physical-verdict.is-warm {" in text
    assert ".event-today-physical-verdict.is-cool {" in text
    assert ".event-today-physical-grid {" in text
    assert ".event-physical-priority.is-compact-summary {" in text
    assert "grid-template-columns:repeat(3, minmax(0, 1fr));" in text
    assert ".event-physical-hub-layout.is-observed-dynamic .event-physical-priority" in text
    assert ".event-observed-dynamic-grid {" in text
    assert "event-physical-priority${compactTrendConclusion ? ' is-compact-summary' : ''}${observedDynamicLayout ? ' is-observed-dynamic' : ''}" in text
    layout_start = text.index('<div class="event-physical-hub-layout${observedDynamicLayout ?')
    layout_end = text.index("${physicalLikelihoodSnapshotHtml}", layout_start)
    layout_block = text[layout_start:layout_end]
    assert "${observedDynamicLayout ? sourceConstraintsHtml : ''}" in layout_block
    assert "${observedDynamicLayout ? '' : sourceConstraintsHtml}" in layout_block
    assert layout_block.index("${observedDynamicLayout ? sourceConstraintsHtml : ''}") < layout_block.index('<div class="event-physical-hub-verdict">')
    assert "${todayPhysicalVerdictHtml}" in layout_block
    assert ".event-trajectory-readings {\n      display:grid;\n      grid-template-columns:repeat(auto-fit, minmax(150px, 1fr));" in text
    assert "function buildTodayPhysicalEvidenceVerdict" in text
    assert "function renderTodayPhysicalEvidenceVerdict" in text
    assert "function todayPhysicalWindCard" in text
    assert "awc_wind_dir_deg" in text
    assert "AWC Raw METAR 风组" in text
    assert "风场辅助" in text
    assert "辅助不覆盖轨迹" in text
    assert "露点湿压" in text
    assert "低露点干化" in text
    assert "高压下沉" in text
    assert "低压扰动" in text
    assert "湿压/辐射" in text
    assert "今日总判 · 物理证据" in text
    assert "const trajectoryGuardActive = Boolean(" in text
    assert "不覆盖温度轨迹门控" in text
    assert "badge: trajectoryGuardActive ? (assessment.badge || '轨迹优先')" in text
    assert "AWC/METAR 无降水" in text
    assert "<strong>实况源约束</strong>" in text
    assert "<strong>完整温度轨迹</strong>" in text
    assert "横向滚动查看每次/整点轨迹" in text
    assert "<span>升降温证据明细</span>" in text
    assert ".event-physical-secondary-grid {" in text
    assert ".event-physical-hub-stages {\n      display:grid;\n      grid-auto-flow:column;" in text
    hub_start = text.index("const hubKpisHtml = observedDynamicLayout")
    hub_end = text.index("const trajectoryDetailsHtml", hub_start)
    hub_block = text[hub_start:hub_end]
    assert "<span>结论</span><strong>${escapeHtml(assessment.title)}</strong>" not in hub_block
    assert "<span>趋势</span>" in hub_block
    assert "<span>目标</span>" in hub_block
    assert "<span>观测源</span>" in hub_block
    assert "<span>实况</span>" not in hub_block
    assert "${escapeHtml(`${currentTemp} / ${observedMax}`)}" not in hub_block


def test_bucket_summary_table_removed_from_corrected_top_summary() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderCorrectedTopBucketBlock")
    end = text.index("function renderRawTopBucketBlock", start)
    block = text[start:end]

    assert "const sourceProbability = (row, sourceKey) => {" not in block
    assert "firstFiniteRowValue(row, [entry.sourceKey, 'q_raw', 'q_round'])" not in text
    assert "['口径', '最高桶', '该口径概率']" not in block
    assert "summaryTable" not in block
    assert "全部口径最高桶对照" not in block


def test_bayes_likelihood_step_matches_refactored_likelihood_stage() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderEventBayesLikelihoodStep")
    end = text.index("function renderEventBayesObservedStep", start)
    block = text[start:end]
    body_start = block.index("const body =")
    body_block = block[body_start:]

    assert "function eventBayesStageFocus" in text
    assert "liveEvidenceChannelKeys" in block
    assert "'observed_nowcast'" in block
    assert "'temperature_trajectory'" in block
    assert "'independent_physics_residual'" in block
    assert "'post_cool_rebound_intraday_likelihood'" in block
    assert "Bayes 预测似然主结论" in block
    assert "previous: focus.priorProb" in block
    assert "value: focus.likelihoodProb" in block
    assert "realtimeKey: `bayes-stage:${String(cardId || eventIntelEventDate(item) || 'latest')}:likelihood`" in block
    assert "参与预测似然的源组件" in block
    assert "CTP posterior贡献占比" in block
    assert "预测 / regime_v1 / 物理证据同源占比" in block
    assert "renderEventBayesContributionPanel(item, focus.displayLabel" in block
    assert "source_components → group shrinkage → continuous density" in block
    assert "renderEventBayesSourceComponentCard(component, '预测似然')" in block
    assert "${modelPanel}" not in body_block
    assert "renderEventIntelAnomalyDynamic(item)" not in body_block
    assert "预测似然明细" not in block
    assert "bayes-likelihood-factor-board is-weak-only" not in body_block
    assert "physicalComponentSectionHtml()" not in body_block


def test_bayes_observed_step_matches_refactored_d0_stage() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderEventBayesObservedStep")
    end = text.index("function renderEventBayesPosteriorStep", start)
    block = text[start:end]
    focus_start = text.index("function eventBayesStageFocus")
    focus_end = text.index("function renderEventBayesStageTransition", focus_start)
    focus_block = text[focus_start:focus_end]

    assert "目标桶观测阶段" in block
    assert "previous: focus.likelihoodProb" in block
    assert "value: focus.observedProb" in block
    assert "realtimeKey: `bayes-stage:${String(cardId || eventIntelEventDate(item) || 'latest')}:d0`" in block
    assert "目标桶后验 vs 路径峰值" in block
    assert "路径触达目标" in block
    assert "只回答当前轨迹能否摸到目标下沿，不等于落桶后验" in block
    assert "峰值到目标下沿" in block
    assert "D0/轨迹/物理贡献占比" in block
    assert "regime_v1 cap / 物理 residual / 硬实况" in block
    assert "观测约束输入" in block
    assert "实况 / 温度轨迹 / 物理证据" in block
    assert "温度轨迹" in block
    assert "当前实况" in block
    assert "已观测最高" in block
    assert "升温率" in block
    assert "轨迹估计最高" in block
    assert "温度动量" in block
    assert "renderTemperatureMomentumVisual(metrics, TEMPERATURE_MOMENTUM_COMPACT_WINDOWS" in block
    assert "物理证据" in block
    assert "renderEventIntelIntradayEvidenceAxis(payload || {})" in block
    assert "温度轨迹趋势曲线" in block
    assert "renderEventIntelPhysicalTrendCurve(payload || {})" in block
    assert "动量只参与 D0/硬源观察层，不回写预测似然" in block
    assert "observation_constraints 未触发硬修正" in block
    assert "temperature_trajectory" in block
    assert "independent_physics_residual" in block
    assert "renderEventIntelPhysicalEvidenceHub" not in block
    assert "hideLikelihoodSecondary: false" not in block
    assert "const selection = eventIntelTargetBucketSelectionFromSnapshot(item, payload);" in focus_block
    assert "const targetTop = targetFocusLabel" in focus_block
    assert "focusSource: targetTop ? (selection.source || 'target_bucket_selection') : 'posterior_top_fallback'" in focus_block
    assert "preferConstrained ? firstFiniteRowValue(row, ['q_raw', 'q_posterior_observed_constraints'])" not in text
    assert "['q_raw', 'q_posterior_observed_constraints', 'q_temperature_bucket_posterior', 'q_bayes_posterior']" not in text
    assert "['q_temperature_bucket_posterior', 'q_posterior_observed_constraints', 'q_bayes_posterior', 'q_raw']" in text
    assert "compactTrendConclusion: false" not in block
    assert "observedDynamicLayout: true" not in block
    assert "对应上方：目标桶观测阶段" in block
    assert "当前 ${observed}" not in block
    assert "最高 ${observedMax}" not in block


def test_intraday_physical_axis_surfaces_today_physical_verdict() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderEventIntelIntradayEvidenceAxis")
    end = text.index("function renderEventIntelPhysicalTrendCurve", start)
    block = text[start:end]

    assert "const todayPhysicalVerdictHtml = renderTodayPhysicalEvidenceVerdict" in block
    assert "buildTodayPhysicalEvidenceVerdict(" in block
    assert "${todayPhysicalVerdictHtml}" in block
    assert "final_bucket_probability_pct" in block
    assert "物理证据结论" in block
    assert "今日总判 · 物理证据" in text


def test_bayes_posterior_step_matches_refactored_bucket_probability_stage() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderEventBayesPosteriorStep")
    end = text.index("function renderEventBayesEvidenceFlow", start)
    block = text[start:end]

    assert "落桶后验主结论" in block
    assert "previous: focus.observedProb" in block
    assert "value: focus.posteriorProb" in block
    assert "realtimeKey: `bayes-stage:${String(cardId || eventIntelEventDate(item) || 'latest')}:posterior`" in block
    assert "posterior_temperature_distribution.bucket_probabilities" in block
    assert "bucket_probabilities" in block
    assert "quantile_grid" in block
    assert "cdf_values" in block
    assert "quantiles_c" in block
    assert "下游使用" in block
    assert "交易层读取落桶后验，不反向改写 Bayes" in block
    assert "daily-trade-advice-mount" in block


def test_source_peak_station_forecast_uses_snapshot_open_meteo_fallback() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function sourcePeakStationForecastData")
    end = text.index("function sourcePeakHost", start)
    block = text[start:end]
    rows_start = text.index("function buildSourcePeakRows")
    rows_end = text.index("function sourcePeakCurveRoleClass", rows_start)
    rows_block = text[rows_start:rows_end]

    assert "data.observed_max_c ?? data.forecast_max_c" in block
    assert "'open_meteo_peak_temp_c'" in block
    assert "'open_meteo_peak_temp'" in block
    assert "sourceLabel: '只读快照 station forecast'" in block
    assert "return sourcePeakStationForecastData(item, data).valueC;" in block
    assert "const stationForecast = sourcePeakStationForecastData(item, stationData);" in rows_block
    assert "const stationC = stationForecast.valueC;" in rows_block
    assert "stationForecast.source || buildSettlementDetailUrl(item)" in rows_block


def test_source_peak_field_reads_compact_feature_fallbacks() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function sourcePeakFeatureBag")
    end = text.index("function sourcePeakStationForecastData", start)
    block = text[start:end]

    assert "weather_likelihood_features_compact" in block
    assert "const features = sourcePeakFeatureBag(item);" in block
    assert "if (value === undefined && features) value = features[key];" in block


def test_source_peak_model_curve_uses_top_bucket_value_fallback() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function sourcePeakModelValue")
    end = text.index("function sourcePeakModelRawHistoryTop", start)
    block = text[start:end]

    assert "const bucket = sourcePeakModelTopBucket(item, modelKey);" in block
    assert "const rep = bucket && bucket.label ? bucketRepresentativeValue(bucket.label) : null;" in block
    assert "if (rep && Number.isFinite(rep.value)) return rep.value;" in block


def test_source_peak_curve_omits_missing_points_from_plot() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderSourcePeakCurveComparison")
    end = text.index("function renderSourcePeakComparison", start)
    block = text[start:end]

    assert "const plottedPoints = points.filter((point) => Number.isFinite(toFiniteNumberOrNaN(point && point.valueC)));" in block
    assert "const coords = plottedPoints.map((point, index) => ({" in block
    assert "source-peak-curve-missing" not in block
    assert "const visibleGroupText = Array.from(new Set(coords.map((point) => point.group).filter(Boolean))).join(' / ');" in block
    assert "const noteText = Number.isFinite(observedValue)" in block


def test_probability_summary_hides_per_bucket_hard_zero_alerts() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    focus_start = text.index("function renderCorrectedTopBucketBlock")
    focus_end = text.index("function renderRawTopBucketBlock", focus_start)
    focus_block = text[focus_start:focus_end]
    qstats_start = text.index("function renderBucketQStatsBlock")
    qstats_end = text.index("function renderDistributionTable", qstats_start)
    qstats_block = text[qstats_start:qstats_end]

    assert "CTP 源组件校准摘要" in focus_block
    assert "renderObservedHardZeroAlerts(item, dist)" not in focus_block
    assert "hardZeroAlertsHtml" not in focus_block
    assert "renderObservedHardZeroAlerts(item, dist)" not in qstats_block
    assert "hardZeroAlertsHtml" not in qstats_block


def test_history_current_distribution_fallback_keeps_bayes_chain_alias() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function mergeCurrentDistributionIntoBucketSeries")
    end = text.index("function bucketSeriesPointCount", start)
    block = text[start:end]

    assert "const chain = bayesChainForDistributionRow(row);" in block
    assert "bayes_chain: chain" in block
    assert "currentPoint.chain = currentPoint.bayes_chain;" in block
    assert "row && row.bayes_chain && row.bayes_chain.prior_prob" not in block


def test_ui_defers_and_throttles_most_likely_history_fetches() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("async function loadMostLikelyHistory")
    end = text.index("function mergeCurrentDistributionIntoBucketSeries", start)
    load_block = text[start:end]

    assert "const MOST_LIKELY_HISTORY_MAX_CONCURRENT = 2;" in text
    assert "const mostLikelyHistoryInFlight = new Map();" in text
    assert "function runMostLikelyHistoryQueued(task)" in text
    assert "mostLikelyHistoryInFlight.get(key)" in load_block
    assert "runMostLikelyHistoryQueued(async () =>" in load_block
    assert "retryCount: 0" in load_block
    assert "observeMostLikelyHistoryWhenVisible" in text
    assert "{ deferFetch: true, observeFetch: true }" in text


def test_posterior_spotlight_uses_capital_plan_before_target_outlook_and_distribution_top() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("const posteriorSpotlightRows =")
    end = text.index("if (posteriorSpotlightPrimary.label)", start)
    block = text[start:end]

    assert "function renderDistributionTable" in text
    assert "const resolvePosteriorSpotlightPrimary = () => {" in block
    assert block.index("capitalPlanForSpotlight.target_bucket") < block.index("targetOutlookForSpotlight.final_bucket")
    assert "targetOutlookForSpotlight.final_bucket" in block
    assert "targetOutlookForSpotlight.posterior_final_bucket" in block
    assert "targetOutlookForSpotlight.target_bucket" in block
    assert "eventIntelBucketProbabilityPct(itemRef, finalLabel" in block
    assert "cumulative_posterior_final_bucket_probability_pct" in text
    assert "posteriorRunnerRankLabels" in block
    assert "分布次高" in block
    assert "capitalPlanForSpotlight.target_bucket" in block
    assert "source: 'capital_plan'" in block
    assert "source: 'distribution_top'" in block
    assert "posteriorSpotlightRunnerRows = posteriorSpotlightRows" in text
    assert "目标桶后验交易目标" in text
    assert "当前后验最高合约（未结算）" in text
    assert "posteriorSpotlightReasonFromSsot" in block
    assert "posteriorSpotlightDeltaPp" in text
    assert "概率变化" in text
    assert "上一快照" in text
    assert "当前快照" in text
    assert "变化原因" in text
    assert "spotlight.dataset.liveReason" in text
    assert "spotlight.dataset.liveSnapshotAt" in text
    assert "spotlight.dataset.liveFactAt" in text
    assert "const posteriorSpotlightCurrentAt = Date.now();" in text
    assert "posteriorSpotlightCurrentTimelineText" in text
    assert "`同步 ${posteriorSpotlightCurrentTimeText}`" in text
    assert "`fact ${posteriorSpotlightFactTimeText}`" in text
    assert "factAt" in text
    assert "snapshotAt" in text
    assert "模型校准组件最高桶" in text
    assert "source_components 模型侧诊断" in text
    assert "非落桶后验" in text
    assert "Weather.com/TWC 校准软源" in text
    assert "只有 final_bucket_lock 才是终局锁定" in text
    assert "预测最高温落桶后验，未结算" in text
    assert "未结算，不是当前实况温度；不同于模型校准组件或 Weather.com/TWC 校准软源" in text
    assert "最终最高温落桶概率" not in text
    assert "最终最高温落桶目标" not in text
    assert "预测最终最高温落桶" not in text


def test_boundary_bucket_probability_uses_exact_bucket_label() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    helper_start = text.index("function eventIntelSameBucketLabel")
    helper_end = text.index("function eventIntelTargetCapitalPlan", helper_start)
    helper_block = text[helper_start:helper_end]
    selection_start = text.index("function eventIntelTargetBucketSelectionFromSnapshot")
    selection_end = text.index("function eventIntelCapitalLegForBucket", selection_start)
    selection_block = text[selection_start:selection_end]

    assert "function eventIntelSameBucketLabel" in text
    assert "function eventIntelCapitalPlanBucketProbabilityPct" in text
    assert "function eventIntelSecondTargetDisplayText" in text
    assert "second_target_posterior" in helper_block
    assert "cumulative_posterior_final_bucket_probability_pct" in helper_block
    assert "eventIntelProbabilityPctFromRows(Array.isArray(data.distribution)" in helper_block
    assert "const capitalTargetLabel = eventIntelBucketLabel(ssotPlan.target_bucket || '', 'C') || '';" in selection_block
    assert "const itemCapitalPlan = capitalPlan && typeof capitalPlan === 'object' ? capitalPlan : {};" in selection_block
    assert "const ssotPlan = itemCapitalPlan.target_bucket || itemCapitalPlan.ok" in selection_block
    assert "let source = 'missing_ssot_target';" in selection_block
    assert "source = 'target_realization_capital_plan';" in selection_block
    assert "source = 'target_bucket_outlook_hard';" in selection_block
    assert selection_block.index("if (capitalTargetLabel)") < selection_block.index("} else if (hardOutlookTargetLabel)")
    assert "source = targetSelection.ssot_source || 'target_bucket_selection';" in selection_block
    hard_start = text.index("function eventIntelOutlookHasHardSourceTarget")
    hard_end = text.index("function eventIntelItemHasHardSourceOutlook", hard_start)
    hard_block = text[hard_start:hard_end]
    assert "data.hard_source_applied === true" in hard_block
    assert "data.final_bucket_lock === true" in hard_block
    assert "sourceText.includes('awc')" in hard_block
    assert "Number.isFinite(toFiniteNumberOrNaN(data.final_bucket_probability_pct))" not in hard_block
    assert "eventIntelBucketProbabilityPct(item, targetLabel" not in selection_block
    assert "topPosterior" not in selection_block
    assert "topSingle" not in selection_block
    assert "decision.target_bucket" not in selection_block
    assert "ssotPlan.second_target_bucket" in selection_block
    assert "eventIntelSecondTargetProbabilityFromOutlook(secondOutlook)" not in selection_block
    assert "item && item.single_temperature_top_q" not in selection_block
    assert "function eventIntelOutlookBucketProbabilityPct(outlook, bucketLabel, seen = new WeakSet())" in helper_block
    assert "if (!data) return Number.NaN;" in helper_block
    assert "if (seen.has(data)) return Number.NaN;" in helper_block
    assert "eventIntelOutlookBucketProbabilityPct(secondOutlook, bucketLabel, seen)" in helper_block
    assert "const secondOutlook = data.second_target_outlook && typeof data.second_target_outlook === 'object'\n\t        ? data.second_target_outlook\n\t        : {};" not in helper_block


def test_event_intel_snapshot_fallback_only_transports_backend_ssot_fields() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function eventIntelPayloadFromSnapshotItem")
    end = text.index("function storeEventIntelChengduIntradayPayload", start)
    block = text[start:end]

    assert "source: 'ui_snapshot_ssot'" in block
    assert "const metrics = objectOrEmpty(item.key_metrics);" in block
    assert "target_bucket_selection: objectOrEmpty(item.target_bucket_selection)" in block
    assert "source_observation_comparison: objectOrEmpty(item.source_observation_comparison)" in block
    assert "target_bucket_observation_state: objectOrEmpty(item.target_bucket_observation_state)" in block
    assert "decision: objectOrEmpty(item.decision)" in block
    assert "const metrics = {" not in block
    assert "eventIntelFactMetricValue" not in block
    assert "eventIntelEvidenceFromFactMetrics" not in block
    assert "const ssotSelection" not in block
    assert "fallbackReliability" not in block
    assert "target_realization_trade_plan: {" not in block
    assert "item.model_top_label" not in block
    assert "item.single_temperature_top_label" not in block
    assert "item.market_top_label" not in block
    assert "eventIntelBucketProbabilityPct(item, topLabel" not in block
    assert "target_probability_pct: Number.isFinite(topProb) ? topProb * 100 : null" not in block
    assert "当前事实层最高" not in block


def test_intraday_decision_target_rendering_does_not_fallback_to_top_single() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    dynamic_start = text.index("function renderEventIntelDecisionDynamic")
    dynamic_end = text.index("function renderEventIntelAnomalyDynamic", dynamic_start)
    dynamic_block = text[dynamic_start:dynamic_end]
    board_start = text.index("function renderChengduIntradayDecisionBoard")
    board_end = text.index("let chengduIntradayDecisionRequestId", board_start)
    board_block = text[board_start:board_end]
    standalone_start = text.index("function renderChengduDecisionStandalone")
    standalone_end = text.index("function refreshChengduDecisionStandalone", standalone_start)
    standalone_block = text[standalone_start:standalone_end]

    for block in (dynamic_block, board_block, standalone_block):
        assert "topSingle.outcome" not in block
        assert "top.outcome" not in block
        assert "top_single_temperature" not in block
        assert "data.target_bucket || decision.target_bucket" not in block
        assert "decision.target_bucket ||" not in block
        assert "targetTradePlan.target_bucket ||" not in block
        assert "chengduDecisionBucketProbability(topSingle)" not in block
    assert "const topLabel = String(posteriorSelection.targetLabel || '');" in dynamic_block
    assert "const topLabel = String(topSelection.targetLabel || '');" in board_block
    assert "var topLabel = String(topSelection.targetLabel || '');" in standalone_block


def test_target_bucket_display_does_not_fallback_to_generic_payload_target_bucket() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    display_start = text.index("function eventIntelPosteriorTradePlanForDisplay")
    display_end = text.index("function eventIntelTargetReliabilityForDisplay", display_start)
    display_block = text[display_start:display_end]
    assessment_start = text.index("function eventIntelPhysicalHubAssessment")
    assessment_end = text.index("function renderEventIntelPhysicalEvidenceHub", assessment_start)
    assessment_block = text[assessment_start:assessment_end]
    hub_start = text.index("function renderEventIntelPhysicalEvidenceHub")
    hub_end = text.index("function renderEventIntelMetadataOnlyPanel", hub_start)
    hub_block = text[hub_start:hub_end]

    assert "target_bucket: data.target_bucket || selection.targetLabel || ''," in display_block
    assert "selection.targetLabel || data.target_bucket" not in display_block
    assert "snapshotSelection.targetLabel || targetSelection.target_bucket" not in assessment_block
    assert "targetSelection.target_bucket || data.target_bucket" not in assessment_block
    assert "const targetLabel = String(snapshotSelection.targetLabel || '').trim();" in assessment_block
    assert "const targetBucket = eventIntelBucketLabel(targetSelection.targetLabel || '—', 'C');" in hub_block
    assert "targetSelection.targetLabel || targetTradePlan.target_bucket" not in hub_block


def test_event_intel_detail_target_label_is_ssot_only() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("const trajectoryJudgementPending = Boolean(trajectoryD0Wait.isFutureLocalD0);")
    end = text.index("const secondTargetLabel = String(posteriorTargetSelection.secondLabel || '').trim();", start)
    block = text[start:end]

    assert "const targetLabel = String(posteriorTargetSelection.targetLabel || '');" in block
    assert "posteriorTargetSelection.targetLabel || topLabel" not in block
    assert "payloadTopSingle.outcome" not in block
    assert "decision.target_bucket" not in block
    assert ": Number.NaN" in block


def test_daily_trade_advice_labels_capital_plan_target_and_distribution_reference() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function formatDailyTradeAdviceSummary")
    end = text.index("function isPosteriorSettlementCandidate", start)
    block = text[start:end]

    assert "目标是${targetSemantics.shortLabel}" in block
    assert "边界桶按累计边界桶概率参与目标排序" in block
    assert "分布最高参考" in block
    assert "targetSemantics.pathNoun" in block
    assert "路径解释：${capitalRiskText}" in block
    assert "升温节奏：${trajectoryPaceText}" in block
    assert "当前后验最高" not in block
    assert "动态交易目标/D0 门控后验最高" not in block


def test_temperature_path_distribution_card_renders_ssot_path_gate() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    func_start = text.index("function renderTemperaturePathDistribution")
    func_end = text.index("function renderTemperatureRealizationReliability", func_start)
    block = text[func_start:func_end]
    hub_start = text.index("const targetOutlookForHub = eventIntelTargetOutlookFromPayload(payload);")
    hub_end = text.index("const secondTargetBucket = targetSelection.secondLabel", hub_start)
    hub_block = text[hub_start:hub_end]
    fallback_start = text.index("function eventIntelPayloadFromSnapshotItem")
    fallback_end = text.index("function storeEventIntelChengduIntradayPayload", fallback_start)
    fallback_block = text[fallback_start:fallback_end]

    assert "temperature_path_distribution: objectOrEmpty(item.temperature_path_distribution)" in fallback_block
    assert "temperature_realization_reliability: objectOrEmpty(item.temperature_realization_reliability)" in fallback_block
    assert "const pathReliability = pathDistribution.reliability" not in fallback_block
    assert "const snapshotReliability = item.temperature_realization_reliability" not in fallback_block
    assert "Object.keys(snapshotReliability).length" not in fallback_block
    assert "temperature_realization_reliability: reliability" not in fallback_block
    assert "function renderTemperaturePathDistribution" in text
    assert "路径概率与交易跟随" in block
    assert "eventIntelFinalBucketProbabilityLabel(targetBucket)" in block
    assert "第二目标落桶后验" in block
    assert "落桶后验是未结算前的最高温目标桶概率" in block
    assert "targetSemantics.pathNoun" in block
    assert "data.bucket_distribution" in block
    assert "is-temperature-path-buckets" in block
    assert "落桶后验 ${posterior}" in block
    assert "${semantics.pathNoun} ${pathProb} · 路径下界 ${lcb}" in block
    assert "1 路径概率" in block
    assert "2 路径下界" in block
    assert "3 可靠性分" in block
    assert "可靠性质量分" in block
    assert "可靠性 ${escapeHtml" in block
    assert "硬源新鲜" in block
    assert "const pathState = data.path_state && typeof data.path_state === 'object' ? data.path_state : {};" in block
    assert "仍在升温窗口" in block
    assert "硬上限否" in block
    assert "交易跟随" in block
    assert "误差带 ${fmtTempValue(errorBand.sigma_c)}" in block
    assert "risk_explanation" in block
    assert "trade_follow" in block
    assert "renderTemperaturePathDistribution(temperaturePathDistribution)" in hub_block
    assert "targetOutlookForHub.temperature_path_distribution" in hub_block
    assert "等待后端 SSOT 输出最高温落桶后验、守住/触达/突破路径概率、可靠性、新鲜度与交易跟随门控；未结算，不是当前实况" in text
    assert "event-temperature-path-distribution" in text


def test_temperature_trajectory_displays_warming_pace_assessment() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function eventIntelTargetPaceProgress")
    end = text.index("function renderTemperatureRealizationReliability", start)
    block = text[start:end]

    assert "warming_pace_assessment" in block
    assert "实际升温节奏" in block
    assert "const actualRate" in block
    assert "const requiredRate" in block


def test_auto_trade_heavy_ui_endpoints_are_nonblocking_by_default() -> None:
    text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    health_start = text.index("def auto_trade_health(")
    health_end = text.index("def auto_trade_daily_stats_status", health_start)
    health_block = text[health_start:health_end]
    trace_start = text.index("def auto_trade_trace(")
    trace_end = text.index("def compute_global_order_list", trace_start)
    trace_block = text[trace_start:trace_end]
    route_start = text.index('if path == "/api/trade-sync":')
    route_end = text.index('if path == "/api/open-orders":', route_start)
    route_block = text[route_start:route_end]

    assert "def trade_sync_process_active" in text
    assert "allow_async_refresh: bool = True" in health_block
    assert '"refresh_mode": "async"' in health_block
    assert 'out["refresh_mode"] = "stale_while_revalidate"' in health_block
    assert "threading.Thread(target=_refresh_async" in health_block
    assert "allow_async_refresh: bool = False" in text
    assert "auto_trade_balance_refresh" in text
    assert "allow_async_refresh=not force" in text
    assert "readonly_trace_build_queued" in trace_block
    assert "_trace_refresh_spawn(now_ts)" in trace_block
    assert "external_running" in route_block
    assert "sync_inline" in route_block
    assert "threading.Thread(target=_trade_sync_async" in route_block
    assert '"mode": "async"' in route_block
    assert "queue_trade_sync_exit_recalc" in text
    assert "TRADE_SYNC_EXIT_RECALC_LOCK.acquire(blocking=False)" in text
    assert 'name="trade_sync_exit_recalc"' in text
    assert 'os.environ.get("PM_AUTO_TRADE_TRACE_MARKET_REFRESH_TIME_BUDGET_SEC", "3")' in text
    assert 'os.environ.get("PM_AUTO_TRADE_TRACE_MARKET_REFRESH_CLOB_TIMEOUT_SEC", "1")' in text


def test_auto_trade_health_facts_freshness_uses_event_fact_latest() -> None:
    text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    start = text.index("# Facts staleness (per event: latest fact_as_of per slug+event_date).")
    end = text.index("def _enrich_oldest_with_ticks", start)
    block = text[start:end]

    assert "FROM event_fact_latest l" in block
    assert "JOIN event_fact_history h ON h.id = l.fact_id" in block
    assert "WHERE run_date = ?" not in block


def test_auto_trade_health_no_orderbook_skips_are_scoped_to_active_fact_events() -> None:
    text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    start = text.index("# Market tick age distribution (per event: latest tick per slug+event_date).")
    end = text.index("# Facts staleness (per event: latest fact_as_of per slug+event_date).", start)
    block = text[start:end]

    assert "active_health_slugs = {slug for slug, _event_date in tick_keys}" in block
    assert "if slug not in active_health_slugs:" in block
    assert "continue" in block[block.index("if slug not in active_health_slugs:") :]


def test_intraday_final_conclusion_uses_clean_decision_summary() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function eventIntelIntradayFinalConclusion")
    end = text.index("function renderEventIntelIntradayEvidenceCard", start)
    block = text[start:end]

    assert "cleanDecisionSummary" in block
    assert "finalConclusion.caveat" in text
    assert "高温放大禁用" not in block
    assert "<small>${escapeHtml(finalConclusion.detail)}</small>" in text
    assert "finalConclusion.detail} · ${finalConclusion.caveat}" not in text


def test_trade_plan_display_respects_temperature_path_follow_gate() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    display_start = text.index("function eventIntelPosteriorTradePlanForDisplay")
    display_end = text.index("function eventIntelTargetReliabilityForDisplay", display_start)
    display_block = text[display_start:display_end]
    start = text.index("function renderTargetRealizationTradePlan")
    end = text.index("function renderCreatedAtReplayPanel", start)
    block = text[start:end]

    assert "capitalPlan && capitalPlan.ok && Array.isArray(capitalPlan.legs)" in display_block
    assert "function eventIntelCapitalLegPathProbability" in text
    assert "leg.target_realization_path_probability" in text
    assert "leg.target_realization_path_p_reach_yes" in text
    assert "eventIntelPathProbabilityText(leg.bucket, pathProbability)" in display_block
    assert "路径兑现 ${eventIntelFormatProb(reliability, 1)}" not in display_block
    assert "primary_action: capitalPlan.temperature_path_trade_follow_block_yes" in display_block
    assert "eventBayesPosteriorRows(item, 3)" not in display_block
    assert "约束后Top3" not in display_block
    assert "按计划" not in display_block
    assert "前端不合成交易候选" in display_block
    assert "ui_plan_available: hasBackendPlan" in display_block
    assert "门控 ${leg.block_reason}" in display_block
    assert "阻断 ${leg.block_reason}" not in display_block
    assert "const pathTradeBlocked = data.temperature_path_trade_follow_allowed === false || data.temperature_path_trade_follow_block_yes === true;" in block
    assert "const planAvailable = data.ui_plan_available !== false;" in block
    assert "交易候选缺失：前端不合成候选。" in block
    assert "<div>腿</div><div>概率</div><div>目标仓位</div><div>差额</div><div>动作</div>" in text
    assert "<div>腿</div><div>后验/达到下沿</div>" not in text
    assert "路径门控禁止 YES 跟随" in block
    assert "temperature_path_trade_follow_reason" in block
    assert "路径概率门控禁止 YES 跟随" in block
    assert "temperature_path_trade_follow_not_allowed: '路径门控禁止YES跟随'" in text
    assert "data.sell_old_buy_new_legs" in text
    assert "data.position_legs_resolved" in text
    assert "无旧仓位" in text
    assert "&& !explicitResolved" in text


def test_ui_strategy_labels_use_path_follow_not_legacy_lcb_primary() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    server_text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")

    assert 'hard_source_direct: "路径概率跟随"' in text
    assert 'lcb_global: "路径概率跟随"' not in text
    assert 'const strategyOrder = ["path_probability_follow"]' in text
    assert "autoTradeStats.path_probability_follow_summary" in text
    assert "autoTradeStats.lcb_global_summary" not in text
    assert "stats.strategy_stats.path_probability_follow" in text
    assert "stats.strategy_stats.lcb_global" not in text
    assert 'strategyOrder = ["lcb_global"]' not in text
    assert "path_probability_follow_enabled" in text
    assert "lcb_global_enabled" not in text
    assert "{ key: 'path_probability_follow', altFallbackKey: 'hard_source_direct', label: '路径概率跟随', flag: 'path_probability_follow_enabled' }" in text
    assert "marketParts.push(`路径概率跟随${comboPctText}`)" in text
    assert "marketParts.push(`目标桶兑现${comboPctText}`)" not in text
    assert "旧策略兼容" not in text
    assert ".replace(/SSOT第二目标/g, '资金第二腿')" in text
    assert "SSOT第二目标" not in server_text


def test_trace_blocks_prefer_target_realization_ui_risk_explanation() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    scanner_text = (ROOT / "scan_weather_market.py").read_text(encoding="utf-8")

    assert "function targetRealizationRiskTextFromItem" in text
    assert "target_realization_ui_risk_explanation_text" in text
    assert "data.target_realization_path_probability" in text
    assert "data.target_realization_path_p_reach_yes" in text
    assert "compactTargetLine" in text
    assert "Number.isFinite(targetPathProbability) ? `${targetSemantics.pathNoun}" in text
    assert "兑现可靠性 ${Number.isFinite(targetReliability)" not in text
    assert "target_realization_error_band_sigma_c" in scanner_text
    assert "target_realization_hard_source_observed_max_c" in scanner_text
    assert "pushObject(decision && decision.details)" in text
    assert "const bottleneck = targetRiskText || simplifyTraceErrorText" in text
    assert "const decisionDetail = targetRiskText || simplifyTraceErrorText" in text
    assert "const bottleneckText = targetRiskText || simplifyTraceErrorText" in text
    assert "路径概率跟随门禁未通过" in text
    assert "LCB门禁未通过" not in text


def test_auto_trade_candidate_enrichment_preserves_physical_component_groups() -> None:
    scanner_text = (ROOT / "scan_weather_market.py").read_text(encoding="utf-8")

    components_pos = scanner_text.index(
        'components = row_ref.get("q_bayes_physical_likelihood_components")'
    )
    component_groups_pos = scanner_text.index(
        'component_groups = row_ref.get("q_bayes_physical_likelihood_component_groups")',
        components_pos,
    )
    bimodal_pos = scanner_text.index(
        'if row_ref.get("q_bayes_bimodal_risk") is not None:',
        component_groups_pos,
    )
    candidate_pos = scanner_text.index(
        'candidate["q_bayes_physical_likelihood_component_groups"]',
        component_groups_pos,
        bimodal_pos,
    )

    assert components_pos < component_groups_pos < candidate_pos < bimodal_pos


def test_bayes_chain_recovers_from_fact_layer_probability_fields_without_components() -> None:
    row = {
        "q_prior": 0.25,
        "q_likelihood": 0.4,
        "q_ensemble_qm_bucket": 0.3,
        "q_bayes_model_evidence": 10.0,
        "q_bayes_wu_forecast_prob": 0.5,
        "q_bayes_wu_forecast_evidence": 10.0,
    }

    chain = server._bayes_chain_for_distribution_row(row)

    assert chain["prior_prob"] == 0.25
    assert abs(chain["likelihood_prob"] - 0.4) < 1e-9
    assert chain["likelihood_parts"]["model"]["prob"] == 0.3
    assert chain["likelihood_parts"]["wu_forecast"]["prob"] == 0.5


def test_bayes_chain_uses_existing_compact_chain_when_raw_components_missing() -> None:
    row = {
        "bayes_chain": {
            "prior_prob": 0.2,
            "likelihood_prob": 0.7,
            "likelihood_parts": {
                "model": {"prob": 0.8, "evidence": 2.0, "component": 1.6}
            },
        }
    }

    chain = server._bayes_chain_for_distribution_row(row)

    assert chain["prior_prob"] == 0.2
    assert chain["likelihood_prob"] == 0.7
    assert chain["likelihood_parts"]["model"]["prob"] == 0.8


def test_ui_snapshot_summary_distribution_keeps_compact_physical_detail_without_heavy_blob() -> None:
    row = {
        "outcome": "31°C",
        "q_bayes_posterior": 0.84,
        "bayes_chain": {"prior_prob": 0.3, "likelihood_prob": 0.8},
        "q_bayes_physical_likelihood_component_groups": {"large": {"items": list(range(20))}},
        "q_bayes_physical_likelihood_components": {"dewpoint": -0.1},
        "q_bayes_physical_likelihood_evidence_scale_details": {"debug": "x" * 128},
        "q_bayes_physical_d0_explanation": {"variables": [{"key": "temp", "value": 31}]},
    }

    compact = server._compact_distribution_row_for_ui_snapshot(
        row,
        include_physical_detail=False,
    )

    assert compact["outcome"] == "31°C"
    assert compact["q_bayes_posterior"] == 0.84
    assert compact["bayes_chain"]["likelihood_prob"] == 0.8
    assert "q_bayes_physical_likelihood_component_groups" not in compact
    assert compact["q_bayes_physical_likelihood_components"] == {"dewpoint": -0.1}
    assert "q_bayes_physical_likelihood_evidence_scale_details" not in compact
    assert "q_bayes_physical_d0_explanation" not in compact


def test_ui_snapshot_summary_minimal_keeps_bayes_likelihood_parts_for_step_explain() -> None:
    row = {
        "outcome": "31°C",
        "q_prior": 0.22,
        "q_likelihood": 0.62,
        "q_bayes_posterior": 0.42,
        "q_bayes_model_prob": 0.58,
        "q_bayes_model_evidence": 20.0,
        "q_bayes_wu_forecast_prob": 0.48,
        "q_bayes_wu_forecast_evidence": 8.0,
    }

    compact = server._compact_distribution_row_for_ui_snapshot(
        row,
        include_physical_detail=False,
        summary_minimal=True,
    )

    chain = compact["bayes_chain"]
    assert chain["prior_prob"] == 0.22
    assert abs(chain["likelihood_prob"] - ((0.58 * 20.0 + 0.48 * 8.0) / 28.0)) < 1e-9
    assert chain["likelihood_parts"]["model"]["prob"] == 0.58
    assert chain["likelihood_parts"]["model"]["evidence"] == 20.0
    assert chain["likelihood_parts"]["wu_forecast"]["prob"] == 0.48


def test_ui_snapshot_summary_keeps_rebound_regime_signal_for_badges() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-24-2026",
        "event_date": "2026-06-24",
        "run_date": "2026-06-24",
        "market_var": "temperature_high",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "distribution": [
            {
                "outcome": "30°C",
                "q_bayes_climo_regime_explanation": {
                    "summary": {
                        "basin_heat_mechanism_model": {
                            "state": "active",
                            "status": "net_heating_regime",
                            "heat_uplift_index": 0.72,
                            "support_score": 1.4,
                            "suppression_score": 0.2,
                            "temperature_adjustment_c": 0.35,
                            "active_support_keys": ["clear_sky_shortwave_radiation"],
                            "active_suppression_keys": [],
                        }
                    }
                },
                "q_bayes_post_cool_rebound_regime_prior_enabled": True,
                "q_bayes_post_cool_rebound_regime_prior_state": "prior",
                "q_bayes_post_cool_rebound_regime_prior_status": "regime_prior",
                "q_bayes_post_cool_rebound_regime_prior_evidence": 0.8,
                "q_bayes_post_cool_rebound_regime_prior_prob": 0.64,
                "q_bayes_post_cool_rebound_regime_prior_pp_delta": 0.012,
                "q_bayes_post_cool_rebound_intraday_likelihood_enabled": True,
                "q_bayes_post_cool_rebound_intraday_likelihood_state": "lift",
                "q_bayes_post_cool_rebound_intraday_likelihood_status": "lift",
                "q_bayes_post_cool_rebound_intraday_likelihood_evidence": 2.4,
                "q_bayes_post_cool_rebound_intraday_likelihood_prob": 0.71,
                "q_bayes_post_cool_rebound_intraday_likelihood_pp_delta": 0.045,
            }
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    signal = slim["rebound_regime_signal"]

    assert slim["basin_heat_mechanism_state"]["state"] == "active"
    assert slim["basin_heat_mechanism_state"]["role"] == "气候/地形 regime 先验"
    assert signal["basin_heat_mechanism"]["state"] == "active"
    assert signal["cold_pool_regime_prior"]["state"] == "active"
    assert signal["cold_pool_regime_prior"]["role"] == "气候/地形 regime 先验"
    assert signal["cold_pool_intraday_likelihood"]["state"] == "active"
    assert signal["cold_pool_intraday_likelihood"]["role"] == "intraday likelihood"
    assert len(signal["channels"]) == 3


def test_ui_snapshot_summary_minimal_keeps_calibration_probability_fields() -> None:
    row = {
        "outcome": "30°C",
        "q_raw": 0.206,
        "q_round": 0.206,
        "q_floor": 0.188,
        "q_ceil": 0.221,
        "q_nearest": 0.204,
        "q_raw_static": 0.212,
        "q_model_temperature_bucket_round": 0.231,
        "q_model_temperature_bucket_floor": 0.219,
        "q_model_temperature_bucket_ceil": 0.244,
        "q_ensemble_qm_raw_bucket": 0.215,
        "q_ensemble_qm_bucket": 0.231,
        "q_equal": 0.231,
        "q_auto": 0.247,
        "q_scenario": 0.283,
        "q_min": 0.1,
        "q_max": 0.44,
        "q_bayes_physical_likelihood_component_groups": {"large": {"items": list(range(20))}},
    }

    compact = server._compact_distribution_row_for_ui_snapshot(
        row,
        summary_minimal=True,
        include_physical_detail=False,
    )

    for key in (
        "q_model_temperature_bucket_round",
        "q_ensemble_qm_raw_bucket",
        "q_ensemble_qm_bucket",
        "q_floor",
        "q_ceil",
        "q_nearest",
        "q_raw_static",
        "q_model_temperature_bucket_floor",
        "q_model_temperature_bucket_ceil",
        "q_equal",
        "q_auto",
        "q_scenario",
        "q_min",
        "q_max",
    ):
        assert compact[key] == row[key]
    assert "q_bayes_physical_likelihood_component_groups" not in compact


def test_model_bucket_rule_comparison_uses_rows_and_member_values() -> None:
    payload = {
        "market_unit": "C",
        "rows": [
            {
                "outcome": "27°C or below",
                "q_ensemble_qm_meta": {
                    "raw_bucket_probability": {
                        "labels": ["27°C or below", "28°C", "29°C", "30°C or higher"]
                    },
                    "bucket_probability": {
                        "labels": ["27°C or below", "28°C", "29°C", "30°C or higher"]
                    },
                },
            }
        ],
        "model_members_decimal_raw": {
            "gfs_seamless": [27.8, 27.9, 28.6],
        },
        "model_members": {
            "gfs_seamless": [28.8, 28.9, 29.6],
        },
    }

    comparison = server._model_bucket_rule_comparison_for_ui_snapshot(payload)

    assert comparison is not None
    assert comparison["rules"] == ["round", "floor", "ceil"]
    rules = comparison["per_model"]["gfs_seamless"]["rules"]
    assert rules["round"]["raw"]["label"] == "28°C"
    assert rules["round"]["qm"]["label"] == "29°C"
    assert rules["floor"]["raw"]["label"] == "27°C or below"
    assert rules["ceil"]["raw"]["label"] == "28°C"
    assert rules["ceil"]["qm"]["label"] == "29°C"


def test_model_bucket_rule_comparison_can_hydrate_from_model_snapshot(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "archive.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE model_snapshots(
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            payload_json TEXT,
            created_at TEXT
        )
        """
    )
    snapshot_payload = {
        "slug": "highest-temperature-in-chengdu-on-june-27-2026",
        "event_date": "2026-06-27",
        "market_unit": "C",
        "rows": [
            {
                "outcome": "27°C or below",
                "q_ensemble_qm_meta": {
                    "raw_bucket_probability": {
                        "labels": ["27°C or below", "28°C", "29°C", "30°C or higher"]
                    },
                    "bucket_probability": {
                        "labels": ["27°C or below", "28°C", "29°C", "30°C or higher"]
                    },
                },
            }
        ],
        "model_members_decimal_raw": {"gfs_seamless": [27.8, 27.9, 28.6]},
        "model_members": {"gfs_seamless": [28.8, 28.9, 29.6]},
    }
    conn.execute(
        "INSERT INTO model_snapshots(id, slug, event_date, payload_json, created_at) VALUES(?, ?, ?, ?, ?)",
        (
            7,
            snapshot_payload["slug"],
            snapshot_payload["event_date"],
            json.dumps(snapshot_payload),
            "2026-06-27T00:00:00+00:00",
        ),
    )
    fact_payload = {
        "slug": snapshot_payload["slug"],
        "event_date": snapshot_payload["event_date"],
        "model_snapshot_id": 7,
        "distribution": [
            {
                "outcome": "27°C or below",
                "q_ensemble_qm_meta": {
                    "raw_bucket_probability": {
                        "labels": ["27°C or below", "28°C", "29°C", "30°C or higher"]
                    }
                },
            }
        ],
    }

    server._attach_model_bucket_rule_comparison_for_ui_snapshot(conn, fact_payload)

    comparison = fact_payload["distribution"][0]["q_ensemble_qm_meta"]["model_bucket_rule_comparison"]
    assert comparison["per_model"]["gfs_seamless"]["rules"]["round"]["raw"]["label"] == "28°C"
    assert comparison["per_model"]["gfs_seamless"]["rules"]["round"]["qm"]["label"] == "29°C"


def test_frontend_post_cool_badges_read_event_level_rebound_signal() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "item.basin_heat_mechanism_state" in html
    assert "item.rebound_regime_signal" in html
    assert "signal.channels" in html
    assert "盆地机制" in html
    assert "冷池先验" in html
    assert "反弹盘中组件" in html


def test_ui_snapshot_summary_derives_mean_bucket_from_distribution_q_fields() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-23-2026",
        "event_date": "2026-06-23",
        "run_date": "2026-06-23",
        "market_var": "temperature_high",
        "market_unit": "C",
        "rounding_rule": "nearest_whole",
        "distribution": [
            {"outcome": "27°C", "q_round": 0.1, "q_equal": 1.0, "q_auto": 0.0, "q_scenario": 0.0},
            {"outcome": "28°C", "q_round": 0.2, "q_equal": 0.0, "q_auto": 1.0, "q_scenario": 0.0},
            {"outcome": "29°C", "q_round": 0.7, "q_equal": 0.0, "q_auto": 0.0, "q_scenario": 1.0},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert slim["market_var"] == "temperature_high"
    assert slim["mean_bucket"]["equal"]["label"] == "27°C"
    assert slim["mean_bucket"]["auto"]["label"] == "28°C"
    assert slim["mean_bucket"]["scenario"]["label"] == "29°C"
    assert slim["mean_bucket"]["auto"]["source"] == "distribution_q_fields"


def test_ui_snapshot_summary_marks_mean_bucket_missing_for_pending_model_distribution() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-24-2026",
        "event_date": "2026-06-24",
        "run_date": "2026-06-23",
        "market_var": "temperature_high",
        "market_unit": "C",
        "distribution": [
            {
                "outcome": "28°C",
                "q_na_reason": "model_not_materialized_yet",
                "market_discovery_placeholder": True,
            },
            {
                "outcome": "29°C",
                "q_na_reason": "model_not_materialized_yet",
                "market_discovery_placeholder": True,
            },
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert "mean_bucket" not in slim
    assert slim["mean_bucket_na_reason"] == "model_not_materialized_yet"


def test_ui_snapshot_summary_minimal_keeps_compact_qm_member_meta() -> None:
    qm_meta = {
        "calibration_mode": "ensemble_qm",
        "raw_bucket_probability": {
            "labels": ["30°C", "31°C"],
            "probs_by_model": {"gfs_seamless": {"30°C": 0.4, "31°C": 0.2}},
            "counts_by_model": {"gfs_seamless": {"30°C": 10, "31°C": 8}},
        },
        "bucket_probability": {
            "labels": ["30°C", "31°C"],
            "probs_by_model": {"gfs_seamless": {"30°C": 0.32, "31°C": 0.28}},
            "counts_by_model": {"gfs_seamless": {"30°C": 10, "31°C": 8}},
        },
    }
    row = {
        "outcome": "30°C",
        "q_raw": 0.4,
        "q_ensemble_qm_bucket": 0.32,
        "q_ensemble_qm_meta": qm_meta,
    }

    compact = server._compact_distribution_row_for_ui_snapshot(
        row,
        include_qm_meta=True,
        summary_minimal=True,
        include_physical_detail=False,
    )

    assert compact["q_ensemble_qm_meta"]["calibration_mode"] == "ensemble_qm"
    assert compact["q_ensemble_qm_meta"]["raw_bucket_probability"]["probs_by_model"]["gfs_seamless"]["30°C"] == 0.4
    assert compact["q_ensemble_qm_meta"]["bucket_probability"]["probs_by_model"]["gfs_seamless"]["30°C"] == 0.32


def test_ui_snapshot_summary_keeps_lead_day_hit_rates() -> None:
    payload = {
        "slug": "x",
        "event_date": "2026-06-21",
        "run_date": "2026-06-21",
        "lead_day_hit_rates": {
            "ok": True,
            "by_source": {
                "gfs_seamless": {
                    "prediction_modes": {
                        "probability_bucket": {
                            "lead_days": {"D0": {"count": 3, "hit_rate": 0.67}},
                        },
                    },
                },
            },
        },
        "distribution": [{"outcome": "30°C", "q_raw": 0.4}],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert slim["lead_day_hit_rates"]["by_source"]["gfs_seamless"]["prediction_modes"]["probability_bucket"]["lead_days"]["D0"]["hit_rate"] == 0.67


def test_ui_snapshot_summary_keeps_weather_multi_source_lineage() -> None:
    payload = {
        "slug": "chengdu",
        "event_date": "2026-06-22",
        "run_date": "2026-06-22",
        "weather_source_lineage": {
            "zuuu_metar_iem_noaaport": {"available": False, "error": "observations_missing"},
            "zuuu_metar_avwx": {"available": False, "error": "token_missing"},
            "zuuu_metar_checkwx": {"available": False, "error": "token_missing"},
            "twc_pws_rapid_history_aux": {"available": False, "error": "pws_station_id_missing"},
        },
        "metar_cross_check_summary": {"status": "missing", "available_count": 0, "source_count": 3},
        "metar_cross_check_sources": [
            {"source_id": "zuuu_metar_iem_noaaport", "available": False, "error": "observations_missing"},
            {"source_id": "zuuu_metar_avwx", "available": False, "error": "token_missing"},
            {"source_id": "zuuu_metar_checkwx", "available": False, "error": "token_missing"},
        ],
        "twc_pws_rapid_history_aux": {
            "available": False,
            "role": "auxiliary_intraday_warning_not_final_settlement",
            "error": "pws_station_id_missing",
        },
        "distribution": [{"outcome": "33°C", "q_raw": 0.9}],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert slim["weather_source_lineage"]["zuuu_metar_avwx"]["error"] == "token_missing"
    assert slim["metar_cross_check_summary"]["source_count"] == 3
    assert slim["metar_cross_check_sources"][0]["source_id"] == "zuuu_metar_iem_noaaport"
    assert slim["twc_pws_rapid_history_aux"]["role"] == "auxiliary_intraday_warning_not_final_settlement"


def test_lead_day_hit_rates_snapshot_only_falls_back_to_latest_success() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            """
            CREATE TABLE feedback_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                days INTEGER NOT NULL,
                end_date TEXT,
                payload_json TEXT NOT NULL,
                ok INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        successful_payload = {
            "ok": True,
            "version": "source_lead_day_hit_rate_v2",
            "sources": [],
            "by_source": {
                "gfs_seamless": {
                    "prediction_modes": {
                        "probability_bucket": {
                            "lead_days": {"D0": {"count": 19, "hit_rate": 0.42}},
                        },
                    },
                },
            },
        }
        failed_payload = {
            "ok": False,
            "version": "source_lead_day_hit_rate_v2",
            "error": "no_eligible_first_prediction_events",
            "coverage": {"eligible_events": 0, "excluded": {"no_fact": 21}},
            "sources": [],
            "by_source": {},
        }
        conn.execute(
            """
            INSERT INTO feedback_snapshots(kind, days, end_date, payload_json, ok, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_KIND,
                server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_DAYS,
                "2026-06-19",
                json.dumps(successful_payload),
                1,
                "",
                "2026-06-20 08:45:20",
            ),
        )
        conn.execute(
            """
            INSERT INTO feedback_snapshots(kind, days, end_date, payload_json, ok, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_KIND,
                server.SOURCE_LEAD_DAY_HIT_RATE_SNAPSHOT_DAYS,
                "2026-06-20",
                json.dumps(failed_payload),
                0,
                "no_eligible_first_prediction_events",
                "2026-06-21 03:55:21",
            ),
        )

        payload = server._safe_first_prediction_hit_rates_snapshot_only(conn)

        assert payload["ok"] is True
        assert payload["fallback_from_latest_failed_snapshot"] is True
        assert payload["fallback_latest_snapshot_meta"]["error"] == "no_eligible_first_prediction_events"
        assert payload["snapshot_created_at"] == "2026-06-20 08:45:20"
        assert (
            payload["by_source"]["gfs_seamless"]["prediction_modes"]["probability_bucket"]["lead_days"]["D0"]["count"]
            == 19
        )
    finally:
        conn.close()


def test_chengdu_intraday_attach_reuses_existing_fact_summary(monkeypatch) -> None:
    def fail_latest_summary(event_date: str = "") -> dict:
        raise AssertionError(f"unexpected rebuild for {event_date}")

    monkeypatch.setattr(server, "latest_chengdu_intraday_summary", fail_latest_summary)
    payload = {
        "station_id": "ZUUU",
        "event_date": "2026-06-18",
        "chengdu_intraday_summary": {
            "ok": True,
            "event_date": "2026-06-18",
            "target_bucket_outlook": {
                "available": True,
                "target_bucket": "31°C",
                "final_bucket": "31°C",
            },
            "forecast_reference_bayes": {"available": True},
            "observation_led_bayes": {"available": True},
            "settlement_lock_bayes": {"available": True},
            "physical_weather_modifier": {"components": []},
            "key_metrics": {"physical_weather_state": "neutral"},
            "temperature_realization_reliability": {
                "target_bucket": "31°C",
                "label": "已达到下沿",
            },
            "temperature_trajectory_status": {
                "temperature_rise_rate_c_per_hour": -0.76,
                "temperature_rise_rate_source": "wu_twc_observed_intraday:30m",
                "temperature_rise_rate_window": "30m",
                "temperature_rise_rate_trusted": True,
            },
        },
    }
    out = {
        "station_id": "ZUUU",
        "weather_likelihood_features_compact": {
            "temperature_rise_rate_na_reason": "unstable_temperature_rise_rate_suppressed",
            "temperature_rise_rate_trusted": False,
        },
    }
    summary_cache = {}

    server._attach_chengdu_intraday_summary_fields_for_ui_snapshot(
        out,
        {"event_date": "2026-06-18", "station_id": "ZUUU"},
        payload,
        summary_cache,
        "2026-06-18",
    )

    assert out["chengdu_intraday_summary"]["event_date"] == "2026-06-18"
    assert out["target_bucket_outlook"]["target_bucket"] == "31°C"
    assert out["temperature_realization_reliability"]["label"] == "已达到下沿"
    assert out["temperature_trajectory_status"]["temperature_rise_rate_c_per_hour"] == -0.76
    assert out["weather_likelihood_features_compact"]["temperature_rise_rate_c_per_hour"] == -0.76
    assert out["weather_likelihood_features_compact"]["temperature_rise_rate_trusted"] is True
    assert out["weather_likelihood_features_compact"]["temperature_rise_rate_window"] == "30m"
    assert summary_cache["2026-06-18"]["target_bucket_outlook"]["final_bucket"] == "31°C"


def test_chengdu_intraday_outlook_display_contract_normalizes_legacy_path_wording() -> None:
    out = {
        "target_bucket_outlook": {
            "available": True,
            "target_bucket": "30°C",
            "final_bucket": "30°C",
            "final_relation_to_target": "target",
            "final_bucket_probability_pct": 27.76,
            "target_reach_label": "满足偏难",
            "primary_conclusion": "当前后验最可能落在 30°C（后验 27.8%）；当前目标桶是 30°C，满足偏难。",
            "reach_line": "轨迹估计达到 30°C 下沿概率 24.5%；这不是最终落桶概率。",
            "answer_cards": [
                {
                    "key": "target_reach",
                    "label": "能否达到目标",
                    "answer": "满足偏难",
                    "detail": "轨迹估计达到 30°C 下沿概率 24.5%；这不是最终落桶概率。",
                },
            ],
            "second_target_outlook": {
                "available": True,
                "definition": "第二目标来自同一后验排序/资金计划，不覆盖当前路径概率目标桶。",
            },
        }
    }
    summary = {
        "target_bucket_outlook": dict(out["target_bucket_outlook"]),
    }

    server._apply_chengdu_intraday_summary_to_snapshot_out(out, summary)

    outlook = out["target_bucket_outlook"]
    nested = out["chengdu_intraday_summary"]["target_bucket_outlook"]
    for item in (outlook, nested):
        assert item["primary_conclusion"] == "当前后验最可能落在 30°C（后验 27.8%）；目标桶同为 30°C。"
        assert item["target_reach_label"] == "路径触达偏弱"
        assert "仅作路径/执行参考" in item["reach_line"]
        assert "满足偏难" not in json.dumps(item, ensure_ascii=False)
        assert item["answer_cards"][0]["label"] == "路径触达参考"
        assert item["answer_cards"][0]["answer"] == "路径触达偏弱"
        assert item["second_target_outlook"]["definition"].endswith("不覆盖当前落桶后验目标桶。")


def test_chengdu_intraday_outlook_display_contract_uses_canonical_path_probability() -> None:
    out = {
        "temperature_path_distribution": {
            "available": True,
            "target_bucket": "30°C",
            "target": {
                "bucket": "30°C",
                "path_probability_pct": 0.64,
                "path_probability_lcb_pct": 0.0,
                "path_probability_source": "peak_estimator_error_band",
                "path_probability_mode": "reach_at_or_above_lower_edge",
            },
            "trade_follow": {"allowed": False, "reason": "blocked_target_path_lcb_low"},
        },
    }
    summary = {
        "temperature_path_distribution": out["temperature_path_distribution"],
        "target_bucket_outlook": {
            "available": True,
            "target_bucket": "30°C",
            "final_bucket": "30°C",
            "final_relation_to_target": "target",
            "final_bucket_probability_pct": 43.07,
            "target_reach_label": "路径仍有机会",
            "reach_line": "轨迹估计达到 30°C 下沿概率 62.3%；仅作路径/执行参考，不改写最高温落桶后验。",
            "target_reach_lower_edge_probability_pct": 62.33,
            "answer_cards": [
                {
                    "key": "target_reach",
                    "label": "路径触达参考",
                    "answer": "路径仍有机会",
                    "detail": "轨迹估计达到 30°C 下沿概率 62.3%；仅作路径/执行参考，不改写最高温落桶后验。",
                    "tone": "warn",
                },
            ],
        },
    }

    server._apply_chengdu_intraday_summary_to_snapshot_out(out, summary)

    outlook = out["target_bucket_outlook"]
    nested = out["chengdu_intraday_summary"]["target_bucket_outlook"]
    for item in (outlook, nested):
        assert item["target_reach_lower_edge_probability_pct"] == pytest.approx(0.64)
        assert item["target_path_probability_lcb_pct"] == pytest.approx(0.0)
        assert item["target_reach_label"] == "路径触达偏弱"
        assert "0.6%" in item["reach_line"]
        assert "62.3%" not in item["reach_line"]
        reach_card = next(card for card in item["answer_cards"] if card["key"] == "target_reach")
        assert reach_card["answer"] == "路径触达偏弱"
        assert reach_card["tone"] == "bad"
        assert "0.6%" in reach_card["detail"]
        assert item["temperature_path_trade_follow_reason"] == "blocked_target_path_lcb_low"


def test_chengdu_intraday_legacy_observation_led_target_source_is_rebuilt() -> None:
    legacy = {
        "target_bucket_source": "observation_led_bayes",
        "target_bucket_selection": {
            "selection_priority": "observation_led_bayes",
            "dynamic_target_source": "observation_led_bayes",
            "target_switch_context": {
                "candidate_bucket": "30°C",
            },
        },
        "target_bucket_outlook": {
            "target_bucket_source": "observation_led_bayes",
        },
    }
    explicit_switch = {
        "target_bucket_source": "observation_led_bayes",
        "target_bucket_selection": {
            "selection_priority": "observation_led_bayes",
            "target_switch_context": {
                "observation_led_candidate_applied": True,
            },
        },
    }

    assert server._chengdu_intraday_summary_has_legacy_observation_led_target_override(legacy)
    assert not server._chengdu_intraday_summary_has_legacy_observation_led_target_override(explicit_switch)


def test_target_outlook_display_contract_cleans_non_target_final_wording() -> None:
    outlook = server._normalize_target_bucket_outlook_display_contract(
        {
            "available": True,
            "target_bucket": "30°C",
            "final_bucket": "29°C",
            "final_relation_to_target": "below_target",
            "primary_conclusion": "硬源已触达 28°C 下沿，约束后最终最可能 29°C；当前目标桶是 30°C（最终最可能低于目标）。",
            "settlement_line": "最终分布偏低：低于目标 22.1%，目标桶 23.1%，高于目标 54.7%。",
            "answer_cards": [
                {
                    "key": "final_bucket",
                    "label": "最终最可能",
                    "answer": "29°C",
                    "detail": "最终最可能落在 29°C。",
                }
            ],
        }
    )

    assert outlook["primary_conclusion"] == "硬源已触达 28°C 下沿，约束后当前后验最可能 29°C；当前目标桶是 30°C（当前后验最可能低于目标）。"
    assert outlook["settlement_line"].startswith("未结算后验偏低")
    assert outlook["answer_cards"][0]["label"] == "当前后验最可能"
    assert outlook["answer_cards"][0]["detail"] == "当前后验最可能落在 29°C。"


def test_chengdu_intraday_summary_apply_exposes_awc_hard_observation_for_ui() -> None:
    out = {
        "weather_likelihood_features_compact": {
            "observed_today_max_c": 25.0,
        }
    }
    summary = {
        "event_date": "2026-06-25",
        "source_observation_comparison": {
            "awc": {
                "today_max_c": 27.0,
                "settlement_proxy_bucket": "27°C",
                "observed_at_utc": "2026-06-25T09:00:00+00:00",
            },
            "wu_twc": {
                "today_max_c": 25.0,
                "settlement_proxy_bucket": "25°C",
            },
        },
        "target_bucket_observation_state": {
            "source": "zuuu_metar_raw_awc_today_max",
            "observed_max_c": 27.0,
            "settlement_proxy_bucket": "27°C",
        },
        "key_metrics": {
            "observed_today_max_c": 27.0,
            "zuuu_metar_raw_awc_today_max_c": 27.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-25T09:00:00+00:00",
            "wu_twc_observed_intraday_today_max_c": 25.0,
        },
    }

    server._apply_chengdu_intraday_summary_to_snapshot_out(out, summary)

    assert out["chengdu_intraday_summary"]["source_observation_comparison"]["awc"]["today_max_c"] == 27.0
    assert out["source_observation_comparison"]["awc"]["today_max_c"] == 27.0
    assert out["target_bucket_observation_state"]["observed_max_c"] == 27.0
    assert out["key_metrics"]["zuuu_metar_raw_awc_today_max_c"] == 27.0
    assert out["weather_likelihood_features_compact"]["observed_today_max_c"] == 27.0
    assert out["weather_likelihood_features_compact"]["zuuu_metar_raw_awc_today_max_c"] == 27.0


def test_target_outlook_rebuilds_when_distribution_probability_is_newer() -> None:
    out = {
        "event_date": "2026-06-22",
        "rounding_rule": "whole_degrees",
        "market_unit": "C",
        "distribution": [
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.3110},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.3645925961706722},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.2110},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.1134074038293278},
        ],
        "temperature_realization_reliability": {
            "target_bucket_warming_realization_pct": 24.54,
            "target_bucket_blocked_realization_pct": 24.54,
            "neutral_or_uncertain_pct": 50.92,
            "target_observation_state": {"state": "not_observed"},
        },
        "weather_likelihood_features_compact": {
            "forecast_peak_temp_c": 30.36,
            "peak_estimator_mu_c": 30.36,
            "peak_estimator_sigma_c": 0.55,
        },
        "target_bucket_outlook": {
            "available": True,
            "target_bucket": "30°C",
            "final_bucket": "30°C",
            "final_bucket_probability_pct": 27.76,
            "target_final_probability_pct": 27.76,
            "primary_conclusion": "当前后验最可能落在 30°C（后验 27.8%）；目标桶同为 30°C。",
        },
        "chengdu_intraday_summary": {
            "target_bucket_outlook": {
                "available": True,
                "target_bucket": "30°C",
                "final_bucket": "30°C",
                "final_bucket_probability_pct": 27.76,
            }
        },
    }

    attach_ctp_distribution(out)
    server._refresh_target_bucket_outlook_from_current_distribution(out)

    outlook = out["target_bucket_outlook"]
    assert outlook["final_bucket"] == "30°C"
    assert outlook["target_bucket"] == "30°C"
    assert outlook["primary_conclusion"] == "当前后验最可能落在 30°C（后验 36.5%）；目标桶同为 30°C。"
    assert outlook["final_bucket_probability_pct"] == 36.46
    assert outlook["target_final_probability_pct"] == 36.46
    assert out["chengdu_intraday_summary"]["target_bucket_outlook"]["final_bucket_probability_pct"] == 36.46
    assert "仅作路径/执行参考" in outlook["reach_line"]


def test_canonical_target_alignment_repairs_stale_target_outlook_summary() -> None:
    item = {
        "distribution": [
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.3337},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.6531927844391303},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.1166},
        ],
        "target_bucket_outlook": {
            "available": True,
            "target_bucket": "30°C",
            "input_target_bucket": "30°C",
            "final_bucket": "30°C",
            "target_final_probability_pct": 33.37,
            "final_bucket_probability_pct": 33.37,
            "summary": "当前已观测最高 27.00°C，目标 30°C：距目标下沿还差 3.00°C；约束后后验最高 30°C（33.7%）。",
        },
        "chengdu_intraday_summary": {
            "target_bucket_outlook": {
                "available": True,
                "target_bucket": "30°C",
            }
        },
    }

    server._align_item_target_fields_to_canonical_posterior_top(item)

    outlook = item["target_bucket_outlook"]
    assert outlook["target_bucket"] == "31°C"
    assert outlook["input_target_bucket"] == "31°C"
    assert outlook["final_bucket"] == "31°C"
    assert outlook["final_bucket_probability_pct"] == pytest.approx(65.31927844391303)
    assert "交易目标 31°C" in outlook["summary"]
    assert "目标 30°C" not in outlook["summary"]
    assert "约束后后验最高 30°C" not in outlook["summary"]
    assert outlook["summary_repaired_to_canonical_target"] is True
    assert item["chengdu_intraday_summary"]["target_bucket_outlook"]["target_bucket"] == "31°C"


def test_target_outlook_rebuild_prefers_dynamic_target_selection_over_posterior() -> None:
    out = {
        "event_date": "2026-06-17",
        "rounding_rule": "whole_degrees",
        "market_unit": "C",
        "distribution": [
            {"outcome": "24°C or below", "q_temperature_bucket_posterior": 0.35},
            {"outcome": "25°C", "q_temperature_bucket_posterior": 0.30},
            {"outcome": "26°C", "q_temperature_bucket_posterior": 0.20},
        ],
        "target_bucket_selection": {
            "target_bucket": "25°C",
            "target_probability_pct": 30.0,
            "dynamic_target_state": "temperature_trajectory_switch",
            "dynamic_target_source": "temperature_trajectory_switch_pressure",
            "watch_buckets": [
                {"outcome": "25°C", "q_temperature_bucket_posterior": 0.30},
                {"outcome": "24°C or below", "q_temperature_bucket_posterior": 0.35},
            ],
        },
        "posterior_target_bucket_selection": {
            "target_bucket": "24°C or below",
            "target_probability_pct": 35.0,
        },
        "temperature_realization_reliability": {
            "target_bucket_warming_realization_pct": 72.0,
            "target_bucket_blocked_realization_pct": 8.0,
            "target_observation_state": {"state": "observed_below_target_bucket"},
        },
        "weather_likelihood_features_compact": {
            "observed_today_max_c": 23.0,
            "observed_temp_c": 23.0,
            "peak_estimator_mu_c": 25.2,
            "peak_estimator_sigma_c": 0.55,
        },
        "target_bucket_outlook": {
            "available": True,
            "target_bucket": "24°C or below",
            "final_bucket": "24°C or below",
            "final_bucket_probability_pct": 10.0,
            "target_final_probability_pct": 10.0,
        },
        "chengdu_intraday_summary": {},
    }

    attach_ctp_distribution(out)
    server._refresh_target_bucket_outlook_from_current_distribution(out)

    outlook = out["target_bucket_outlook"]
    assert outlook["target_bucket"] == "25°C"
    assert outlook["input_target_bucket"] == "25°C"
    assert outlook["target_bucket_source"] == "temperature_trajectory_switch_pressure"
    assert outlook["final_bucket"] == "24°C or below"
    assert outlook["final_relation_to_target"] == "below_target"
    assert out["chengdu_intraday_summary"]["target_bucket_outlook"]["target_bucket"] == "25°C"


def test_ssot_target_context_prefers_dynamic_target_selection_before_outlook() -> None:
    context = server._ssot_target_context_from_payload(
        {
            "target_bucket_selection": {
                "target_bucket": "25°C",
                "target_probability_pct": 30.0,
                "second_target_bucket": "26°C",
                "second_target_probability_pct": 20.0,
            },
            "target_bucket_outlook": {
                "target_bucket": "24°C or below",
                "target_bucket_probability_pct": 35.0,
                "second_target_bucket": "25°C",
                "second_target_probability_pct": 30.0,
            },
        }
    )

    assert context["target_bucket"] == "25°C"
    assert context["posterior_settlement_target_bucket"] == "25°C"
    assert context["target_posterior"] == 0.30
    assert context["second_target_bucket"] == "26°C"
    assert context["second_target_posterior"] == 0.20


def test_ui_snapshot_summary_rebuilds_capital_plan_after_chengdu_overlay(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-21-2026"
    old_target = "26°C"
    new_target = "25°C or below"
    distribution = [
        {
            "outcome": new_target,
            "q_bayes_posterior": 0.26122337983261507,
            "q_temperature_bucket_posterior": 0.26122337983261507,
            "best_bid": 0.54,
            "best_ask": 0.55,
        },
        {
            "outcome": old_target,
            "q_bayes_posterior": 0.2017124436814672,
            "q_temperature_bucket_posterior": 0.2017124436814672,
            "best_bid": 0.29,
            "best_ask": 0.30,
        },
    ]
    stale_payload = {
        "slug": slug,
        "title": "Highest temperature in Chengdu on June 21?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "event_date": "2026-06-21",
        "run_date": "2026-06-20",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "distribution": distribution,
        "chengdu_intraday_summary": {
            "ok": True,
            "event_date": "2026-06-21",
            "target_bucket_selection": {
                "definition": "单一温度桶中选择最高桶",
                "target_bucket": old_target,
                "second_target_bucket": new_target,
            },
            "target_bucket_outlook": {
                "target_bucket": old_target,
                "final_bucket": old_target,
                "target_bucket_source": "atomic_posterior_highest_bucket",
            },
        },
        "target_realization_capital_plan": {
            "ok": True,
            "target_bucket_selection_source": "chengdu_intraday_summary.target_bucket_selection",
            "target_bucket": old_target,
            "target_posterior": 0.2017124436814672,
            "second_target_bucket": new_target,
            "second_target_posterior": 0.26122337983261507,
        },
    }
    latest_summary = {
        "ok": True,
        "event_date": "2026-06-21",
        "target_bucket": new_target,
        "target_bucket_probability_pct": 26.12,
        "target_bucket_selection": {
            "definition": "边界桶与单温度桶同权参与目标排序",
            "target_bucket": new_target,
            "target_probability_pct": 26.12,
            "watch_buckets": [{"outcome": old_target}],
        },
        "posterior_target_bucket_selection": {
            "target_bucket": new_target,
            "target_probability_pct": 26.12,
        },
        "target_bucket_outlook": {
            "available": True,
            "target_bucket": new_target,
            "final_bucket": new_target,
            "final_bucket_probability_pct": 26.12,
            "target_bucket_source": "posterior_highest_bucket",
            "second_target_bucket": old_target,
            "second_target_outlook": {
                "available": True,
                "second_target_bucket": old_target,
                "candidate_bucket": old_target,
            },
        },
        "temperature_path_distribution": {
            "available": True,
            "target_bucket": new_target,
            "second_target_bucket": old_target,
            "risk_codes": ["second_target_risk_high"],
            "risk_explanation": "目标 25°C or below posterior 26.12%，路径可兑现 42.0%。第二目标 26°C posterior 20.17%，路径可兑现 28.2%。风险：second_target_risk_high。",
            "error_band": {
                "sigma_c": 1.35,
                "source": "historical_error_calibration",
                "match_scope": "chengdu_d0",
            },
            "hard_source_state": {
                "state": "fresh_observed_lower_bound",
                "source": "zuuu_metar_raw_awc",
                "observed_max_c": 24.8,
                "latest_observation_time_local": "2026-06-21 14:12",
            },
            "freshness": {
                "source_stale": False,
                "observation_age_minutes": 4.5,
                "freshness_score": 0.92,
            },
            "reliability": {
                "state": "medium",
                "label": "Medium",
                "score": 0.64,
                "confidence": 0.70,
            },
            "target": {
                "bucket": new_target,
                "posterior": 0.26122337983261507,
                "path_probability": 0.4202,
            },
            "second_target": {
                "bucket": old_target,
                "posterior": 0.2017124436814672,
                "path_probability": 0.2818,
            },
            "trade_follow": {
                "allowed": False,
                "reason": "blocked_path_reliability_low",
            },
        },
    }

    monkeypatch.setattr(server, "DB_PATH", db_path)
    monkeypatch.setattr(server, "UI_SNAPSHOT_CACHE_SEC", 0)
    monkeypatch.setattr(server, "UI_SNAPSHOT_SUMMARY_FAST", True)
    with server._UI_SNAPSHOT_CACHE_LOCK:
        server._UI_SNAPSHOT_CACHE.clear()
    with server._CHENGDU_INTRADAY_SUMMARY_CACHE_LOCK:
        server._CHENGDU_INTRADAY_SUMMARY_CACHE.clear()
        server._CHENGDU_INTRADAY_SUMMARY_CACHE["2026-06-21"] = (time.time(), dict(latest_summary))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-20",
        slug=slug,
        event_date="2026-06-21",
        fact_as_of="2026-06-20T14:00:00+00:00",
        model_snapshot_id=1,
        market_snapshot_id=2,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=stale_payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-21", fact_id)
    conn.commit()
    conn.close()

    payload = server.ui_snapshot_summary("2026-06-20", 24)
    item = next(row for row in payload["facts"]["items"] if row["event_date"] == "2026-06-21")
    plan = item["target_realization_capital_plan"]

    assert item["target_bucket_outlook"]["target_bucket"] == new_target
    assert plan["target_bucket_selection_source"] == "target_bucket_selection"
    assert plan["target_bucket"] == new_target
    assert plan["target_posterior"] == 0.26122337983261507
    assert plan["second_target_bucket"] == old_target
    assert plan["second_target_posterior"] == 0.2017124436814672
    assert plan["temperature_path_trade_follow_allowed"] is False
    assert plan["temperature_path_trade_follow_block_yes"] is True
    risk = plan["ui_risk_explanation"]
    assert risk["available"] is True
    assert risk["target_bucket"] == new_target
    assert risk["second_target_bucket"] == old_target
    assert risk["target_posterior"] == 0.26122337983261507
    assert risk["second_target_posterior"] == 0.2017124436814672
    assert risk["freshness"]["source_stale"] is False
    assert "observation_age_minutes" in risk["freshness"]
    assert risk["error_band"]["sigma_c"] is not None
    assert risk["error_band"]["source"]
    assert "observed_max_c" in risk["hard_source_state"]
    assert risk["hard_source_state"]["state"]
    assert risk["reliability"]["state"]
    assert risk["trade_follow"]["allowed"] is False
    assert risk["trade_follow"]["reason"] == "blocked_path_reliability_low"
    assert risk["risk_codes"]
    assert "落桶后验" in risk["text"]
    assert "最终后验" not in risk["text"]
    assert "路径执行概率" in risk["text"]
    assert "路径概率门控禁止 YES 跟随" in plan["summary"]
    assert all(
        leg["action"] != "add"
        for leg in plan["legs"]
        if leg["role"] in {"target_yes", "second_target_yes"}
    )
    assert any(
        leg["block_reason"] == "temperature_path_trade_follow_not_allowed"
        for leg in plan["legs"]
        if leg["role"] in {"target_yes", "second_target_yes"}
    )
    assert any(
        leg["role"] == "second_target_yes" and leg["role_label"] == "资金第二腿买入 YES"
        for leg in plan["legs"]
    )
    assert item["target_switch_context"]["target_bucket"] == new_target
    assert item["target_switch_context"]["candidate_bucket"] == old_target
    assert "temperature_path_trade_follow_not_allowed" in item["target_switch_context"]["no_add_reasons"]


def test_target_realization_plan_ignores_stale_single_bucket_selection() -> None:
    item = {
        "slug": "highest-temperature-in-chengdu-on-june-21-2026",
        "event_date": "2026-06-21",
        "run_date": "2026-06-20",
        "rounding_rule": "whole_degrees",
        "distribution": [
            {
                "outcome": "25°C or below",
                "q_temperature_bucket_posterior": 0.26122337983261507,
                "best_bid": 0.52,
                "best_ask": 0.53,
            },
            {
                "outcome": "26°C",
                "q_temperature_bucket_posterior": 0.2017124436814672,
                "best_bid": 0.28,
                "best_ask": 0.29,
            },
            {
                "outcome": "28°C",
                "q_temperature_bucket_posterior": 0.19966992988052046,
                "best_bid": 0.04,
                "best_ask": 0.05,
                "best_bid_no": 0.94,
                "best_ask_no": 0.95,
            },
        ],
        "target_bucket_outlook": {
            "available": True,
            "target_bucket": "26°C",
            "second_target_bucket": "28°C",
        },
    }

    plan = server._build_target_realization_capital_plan(item)
    item["target_realization_capital_plan"] = plan
    context = server.build_target_switch_context(item)

    assert plan["target_bucket_selection_source"] == "event_fact_latest.canonical_target"
    assert plan["target_bucket"] == "25°C or below"
    assert plan["target_posterior"] == 0.26122337983261507
    assert plan["second_target_bucket"] == "26°C"
    assert plan["ui_risk_explanation"]["target_bucket"] == "25°C or below"
    assert plan["ui_risk_explanation"]["second_target_bucket"] == "26°C"
    assert plan["ui_risk_explanation"]["target_posterior"] == 0.26122337983261507
    assert "目标 25°C or below 最终守住低边界后验" in plan["ui_risk_explanation"]["text"]
    assert context["target_bucket"] == "25°C or below"
    assert context["candidate_bucket"] == "26°C"


def test_target_realization_plan_uses_below_and_above_remote_no_ring(monkeypatch) -> None:
    monkeypatch.delenv("PM_TARGET_REALIZATION_REMOTE_NO_MIN_BUCKET_DISTANCE", raising=False)
    monkeypatch.delenv("PM_TARGET_REALIZATION_REMOTE_NO_MAX_BUCKET_DISTANCE", raising=False)
    monkeypatch.setenv("PM_TARGET_REALIZATION_REMOTE_NO_MAX_PRICE", "1")
    item = {
        "slug": "highest-temperature-in-chengdu-on-july-1-2026",
        "event_date": "2026-07-01",
        "run_date": "2026-07-01",
        "rounding_rule": "whole_degrees",
        "distribution": [
            {
                "outcome": f"{bucket}\u00b0C" if isinstance(bucket, int) else bucket,
                "q_temperature_bucket_posterior": prob,
                "best_bid": max(0.01, yes_ask - 0.02),
                "best_ask": yes_ask,
                "best_bid_no": max(0.01, no_ask - 0.02),
                "best_ask_no": no_ask,
            }
            for bucket, prob, yes_ask, no_ask in [
                (28, 0.02, 0.03, 0.99),
                (29, 0.03, 0.04, 0.97),
                (30, 0.08, 0.09, 0.91),
                (31, 0.37, 0.24, 0.76),
                (32, 0.20, 0.33, 0.68),
                (33, 0.07, 0.24, 0.77),
                (34, 0.02, 0.12, 0.91),
                ("35\u00b0C or higher", 0.01, 0.01, 0.995),
            ]
        ],
        "posterior_target_bucket_selection": {
            "target_bucket": "31\u00b0C",
            "target_probability": 0.37,
        },
    }

    plan = server._build_target_realization_capital_plan(item)

    remote = [
        leg
        for leg in plan["legs"]
        if leg.get("role") == "remote_no" and leg.get("side") == "NO"
    ]
    assert [(leg["bucket"], leg["remote_no_bucket_direction"]) for leg in remote] == [
        ("29\u00b0C", "below_target"),
        ("28\u00b0C", "below_target"),
        ("34\u00b0C", "above_target"),
        ("35\u00b0C or higher", "above_target"),
    ]
    assert {leg["bucket"] for leg in remote}.isdisjoint({"32\u00b0C", "33\u00b0C"})
    assert plan["remote_no_bucket_direction"] == "below_and_above_target"


def _clear_target_realization_remote_no_env(monkeypatch) -> None:
    for key in (
        "PM_TARGET_REALIZATION_REMOTE_NO_MIN_BUCKET_DISTANCE",
        "PM_TARGET_REALIZATION_REMOTE_NO_MAX_BUCKET_DISTANCE",
        "PM_TARGET_REALIZATION_REMOTE_NO_LOWER_MIN_BUCKET_DISTANCE",
        "PM_TARGET_REALIZATION_REMOTE_NO_LOWER_MAX_BUCKET_DISTANCE",
        "PM_TARGET_REALIZATION_REMOTE_NO_UPPER_MIN_BUCKET_DISTANCE",
        "PM_TARGET_REALIZATION_REMOTE_NO_UPPER_MAX_BUCKET_DISTANCE",
        "PM_TARGET_REALIZATION_REMOTE_NO_BUCKETS_PER_SIDE",
        "PM_TARGET_REALIZATION_REMOTE_NO_MAX_PRICE",
        "PM_TARGET_REALIZATION_REMOTE_NO_MAX_NOTIONAL_USDC",
        "PM_TARGET_REALIZATION_YES_MAX_NOTIONAL_USDC",
    ):
        monkeypatch.delenv(key, raising=False)


def _target_32_directional_ring_item(momentum_rate: float) -> dict:
    rows = [
        (27, 0.03, 0.04, 0.96),
        (28, 0.04, 0.05, 0.96),
        (29, 0.05, 0.06, 0.96),
        (30, 0.10, 0.11, 0.96),
        (31, 0.13, 0.14, 0.86),
        (32, 0.45, 0.44, 0.56),
        (33, 0.09, 0.10, 0.96),
        (34, 0.09, 0.10, 0.96),
        ("35\u00b0C or higher", 0.02, 0.03, 0.96),
    ]
    return {
        "slug": "highest-temperature-in-chengdu-on-july-1-2026",
        "event_date": "2026-07-01",
        "run_date": "2026-07-01",
        "rounding_rule": "whole_degrees",
        "distribution": [
            {
                "outcome": f"{bucket}\u00b0C" if isinstance(bucket, int) else bucket,
                "q_temperature_bucket_posterior": prob,
                "best_bid": max(0.01, yes_ask - 0.02),
                "best_ask": yes_ask,
                "best_bid_no": max(0.01, no_ask - 0.02),
                "best_ask_no": no_ask,
            }
            for bucket, prob, yes_ask, no_ask in rows
        ],
        "posterior_target_bucket_selection": {
            "target_bucket": "32\u00b0C",
            "target_probability": 0.45,
        },
        "temperature_path_distribution": {
            "available": True,
            "temperature_momentum_effective_c_per_hour": momentum_rate,
            "temperature_momentum_realization_state": (
                "emerging_warming" if momentum_rate > 0 else "cooling"
            ),
            "trade_follow": {
                "allowed": True,
                "reason": "posterior_path_reliability_and_freshness_pass",
            },
        },
    }


def test_target_realization_plan_uses_warming_directional_remote_no_ring(monkeypatch) -> None:
    _clear_target_realization_remote_no_env(monkeypatch)
    monkeypatch.setenv("PM_TARGET_REALIZATION_BANKROLL_USDC", "200")
    monkeypatch.setenv("PM_TARGET_REALIZATION_RESERVE_RATIO", "0")
    monkeypatch.setenv("PM_TARGET_REALIZATION_FRACTION", "1")
    monkeypatch.setenv("PM_TARGET_REALIZATION_TODAY_RATIO", "1")
    monkeypatch.setenv("PM_TARGET_REALIZATION_TOMORROW_RATIO", "0")
    monkeypatch.setenv("PM_TARGET_REALIZATION_DAY_AFTER_RATIO", "0")
    monkeypatch.setenv("PM_TARGET_REALIZATION_YES_RATIO", "0.2")
    monkeypatch.setenv("PM_TARGET_REALIZATION_NO_RATIO", "0.8")

    plan = server._build_target_realization_capital_plan(_target_32_directional_ring_item(0.5))
    remote = [
        leg
        for leg in plan["legs"]
        if leg.get("role") == "remote_no" and leg.get("side") == "NO"
    ]

    assert plan["remote_no_momentum_direction"] == "warming"
    assert plan["remote_no_directional_window_policy"] == "warming_lower_near_broad_upper_far"
    assert [(leg["bucket"], leg["remote_no_bucket_direction"]) for leg in remote] == [
        ("30\u00b0C", "below_target"),
        ("29\u00b0C", "below_target"),
        ("28\u00b0C", "below_target"),
        ("27\u00b0C", "below_target"),
        ("35\u00b0C or higher", "above_target"),
    ]
    assert {leg["bucket"] for leg in remote}.isdisjoint({"33\u00b0C", "34\u00b0C"})
    target_yes = next(leg for leg in plan["legs"] if leg["role"] == "target_yes")
    assert target_yes["target_notional"] <= 20.0
    assert all(leg["target_notional"] <= 100.0 for leg in remote)
    assert all(leg["target_realization_order_price_cap"] == 0.98 for leg in remote)
    by_bucket = {leg["bucket"]: leg["target_notional"] for leg in remote}
    assert by_bucket["35\u00b0C or higher"] > by_bucket["27\u00b0C"]
    assert by_bucket["27\u00b0C"] > by_bucket["28\u00b0C"]
    assert by_bucket["28\u00b0C"] > by_bucket["29\u00b0C"]
    assert by_bucket["29\u00b0C"] > by_bucket["30\u00b0C"]


def test_target_realization_plan_warming_ring_is_relative_not_boundary_hardcoded(monkeypatch) -> None:
    _clear_target_realization_remote_no_env(monkeypatch)
    item = _target_32_directional_ring_item(0.5)
    for row in item["distribution"]:
        outcome = row["outcome"]
        row["q_temperature_bucket_posterior"] = {
            "31\u00b0C": 0.45,
            "32\u00b0C": 0.13,
            "30\u00b0C": 0.10,
            "34\u00b0C": 0.04,
            "35\u00b0C or higher": 0.03,
        }.get(outcome, 0.02)
    item["posterior_target_bucket_selection"] = {
        "target_bucket": "31\u00b0C",
        "target_probability": 0.45,
    }

    plan = server._build_target_realization_capital_plan(item)
    remote = [
        leg
        for leg in plan["legs"]
        if leg.get("role") == "remote_no" and leg.get("side") == "NO"
    ]

    assert plan["target_bucket"] == "31\u00b0C"
    assert ("34\u00b0C", "above_target", 3) in [
        (
            leg["bucket"],
            leg["remote_no_bucket_direction"],
            leg["remote_no_bucket_distance"],
        )
        for leg in remote
    ]


def test_target_realization_plan_uses_cooling_directional_remote_no_ring(monkeypatch) -> None:
    _clear_target_realization_remote_no_env(monkeypatch)

    plan = server._build_target_realization_capital_plan(_target_32_directional_ring_item(-0.5))
    remote = [
        leg
        for leg in plan["legs"]
        if leg.get("role") == "remote_no" and leg.get("side") == "NO"
    ]

    assert plan["remote_no_momentum_direction"] == "cooling"
    assert plan["remote_no_directional_window_policy"] == "cooling_upper_near_broad_lower_far"
    assert [(leg["bucket"], leg["remote_no_bucket_direction"]) for leg in remote] == [
        ("29\u00b0C", "below_target"),
        ("28\u00b0C", "below_target"),
        ("27\u00b0C", "below_target"),
        ("34\u00b0C", "above_target"),
        ("35\u00b0C or higher", "above_target"),
    ]
    assert {leg["bucket"] for leg in remote}.isdisjoint({"30\u00b0C", "33\u00b0C"})


def test_target_realization_plan_prefers_temperature_path_distribution_probabilities() -> None:
    item = {
        "slug": "highest-temperature-in-chengdu-on-june-21-2026",
        "event_date": "2026-06-21",
        "run_date": "2026-06-21",
        "rounding_rule": "whole_degrees",
        "distribution": [
            {
                "outcome": "25°C or below",
                "q_temperature_bucket_posterior": 0.26122337983261507,
                "best_bid": 0.52,
                "best_ask": 0.53,
            },
            {
                "outcome": "26°C",
                "q_temperature_bucket_posterior": 0.2017124436814672,
                "best_bid": 0.28,
                "best_ask": 0.29,
            },
            {
                "outcome": "28°C",
                "q_temperature_bucket_posterior": 0.19966992988052046,
                "best_bid": 0.04,
                "best_ask": 0.05,
                "best_bid_no": 0.94,
                "best_ask_no": 0.95,
            },
        ],
        "temperature_path_distribution": {
            "available": True,
            "target_bucket": "25°C or below",
            "target_bucket_posterior": 0.42016291281991425,
            "target_bucket_path_probability": 0.5,
            "second_target_bucket": "26°C",
            "second_target_bucket_posterior": 0.2817846528381569,
            "second_target_bucket_path_probability": 0.2119,
            "target": {
                "bucket": "25°C or below",
                "posterior": 0.42016291281991425,
                "path_probability": 0.5,
                "path_probability_source": "temperature_path_distribution",
            },
            "second_target": {
                "bucket": "26°C",
                "posterior": 0.2817846528381569,
                "path_probability": 0.2119,
                "path_probability_source": "temperature_path_distribution",
            },
            "freshness": {"source_stale": False, "observation_age_minutes": 29.2},
            "reliability": {"state": "high", "label": "High", "score": 0.8115},
            "trade_follow": {
                "allowed": True,
                "reason": "posterior_path_reliability_and_freshness_pass",
            },
            "risk_explanation": (
                "目标 25°C or below posterior 42.02%，路径可兑现 50.0%。"
                " 第二目标 26°C posterior 28.18%，路径可兑现 21.19%。"
            ),
        },
    }

    plan = server._build_target_realization_capital_plan(item)
    legs = {(leg["role"], leg["bucket"], leg["side"]): leg for leg in plan["legs"]}

    assert plan["target_bucket"] == "25°C or below"
    assert plan["target_posterior"] == 0.26122337983261507
    assert plan["second_target_bucket"] == "26°C"
    assert plan["second_target_posterior"] == 0.2017124436814672
    assert plan["ui_risk_explanation"]["target_posterior"] == 0.26122337983261507
    assert plan["ui_risk_explanation"]["second_target_posterior"] == 0.2017124436814672
    assert plan["ui_risk_explanation"]["target_realization_probability"] == 0.5
    assert plan["ui_risk_explanation"]["second_target_realization_probability"] == 0.2119
    assert legs[("target_yes", "25°C or below", "YES")]["target_realization_path_probability"] == 0.5
    assert legs[("second_target_yes", "26°C", "YES")]["target_realization_path_probability"] == 0.2119


def test_temperature_path_and_capital_plan_posterior_align_to_current_distribution() -> None:
    out = {
        "slug": "highest-temperature-in-chengdu-on-june-22-2026",
        "event_date": "2026-06-22",
        "run_date": "2026-06-21",
        "distribution": [
            {
                "outcome": "30°C",
                "q_temperature_bucket_posterior": 0.3645925961706722,
                "best_bid": 0.52,
                "best_ask": 0.53,
            },
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.2703085257485856,
                "best_bid": 0.28,
                "best_ask": 0.29,
            },
        ],
        "target_bucket_outlook": {
            "available": True,
            "target_bucket": "30°C",
            "final_bucket": "30°C",
            "final_bucket_probability_pct": 36.46,
            "target_final_probability_pct": 36.46,
        },
        "temperature_path_distribution": {
            "available": True,
            "target_bucket": "30°C",
            "target_bucket_posterior": 0.2776375734997888,
            "target_bucket_posterior_pct": 27.76,
            "target_bucket_path_probability": 0.245,
            "second_target_bucket": "31°C",
            "second_target_bucket_posterior": 0.1547,
            "second_target_bucket_posterior_pct": 15.47,
            "second_target_bucket_path_probability": 0.12,
            "target": {
                "bucket": "30°C",
                "posterior": 0.2776375734997888,
                "posterior_pct": 27.76,
                "path_probability": 0.245,
            },
            "second_target": {
                "bucket": "31°C",
                "posterior": 0.1547,
                "posterior_pct": 15.47,
                "path_probability": 0.12,
            },
            "bucket_distribution": [
                {"bucket": "30°C", "posterior": 0.2776375734997888, "posterior_pct": 27.76, "path_probability": 0.245},
                {"bucket": "31°C", "posterior": 0.1547, "posterior_pct": 15.47, "path_probability": 0.12},
            ],
            "trade_follow": {"allowed": True, "reason": "posterior_path_reliability_and_freshness_pass"},
        },
        "chengdu_intraday_summary": {
            "temperature_path_distribution": {
                "target_bucket": "30°C",
                "target_bucket_posterior_pct": 27.76,
            }
        },
    }

    attach_ctp_distribution(out)
    server._sync_temperature_path_distribution_posterior_from_current_distribution(out)
    server._attach_target_realization_capital_plan(out)

    path = out["temperature_path_distribution"]
    assert path["target_bucket_posterior"] == 0.3645925961706722
    assert path["target_bucket_posterior_pct"] == 36.46
    assert path["target"]["posterior"] == 0.3645925961706722
    assert path["bucket_distribution"][0]["posterior"] == 0.3645925961706722
    assert path["target"]["path_probability"] == 0.245
    assert out["chengdu_intraday_summary"]["temperature_path_distribution"]["target_bucket_posterior_pct"] == 36.46

    plan = out["target_realization_capital_plan"]
    assert plan["target_bucket"] == "30°C"
    assert plan["target_posterior"] == 0.3645925961706722
    assert plan["second_target_bucket"] == "31°C"
    assert plan["second_target_posterior"] == 0.2703085257485856
    legs = {(leg["role"], leg["bucket"], leg["side"]): leg for leg in plan["legs"]}
    assert legs[("target_yes", "30°C", "YES")]["posterior"] == 0.3645925961706722
    assert legs[("target_yes", "30°C", "YES")]["target_realization_path_probability"] == 0.245


def test_temperature_path_posterior_sync_uses_hard_source_constrained_outlook_rows() -> None:
    out = {
        "slug": "highest-temperature-in-chengdu-on-june-22-2026",
        "event_date": "2026-06-22",
        "run_date": "2026-06-22",
        "rounding_rule": "whole_degrees",
        "market_unit": "C",
        "distribution": [
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.7278, "best_ask": 0.01},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.2508, "best_ask": 0.01},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.0205, "best_ask": 0.01},
            {"outcome": "33°C", "q_temperature_bucket_posterior": 0.0006, "best_ask": 0.017},
            {"outcome": "34°C", "q_temperature_bucket_posterior": 0.0002, "best_ask": 0.009},
        ],
        "target_bucket_outlook": {
            "available": True,
            "target_bucket_source": "hard_source_constrained_posterior_highest_bucket",
            "final_bucket_source": "hard_source_constrained_posterior",
            "target_bucket": "33°C",
            "final_bucket": "33°C",
            "target_final_probability_pct": 65.72,
            "final_bucket_probability_pct": 65.72,
            "second_target_bucket": "34°C",
            "second_target_final_probability_pct": 21.10,
            "hard_source_direct": {
                "available": True,
                "source": "hard_source_direct",
                "posterior_floor_constraint_applied": True,
                "bucket": "33°C",
                "observed_max_c": 33.0,
            },
            "rows": [
                {
                    "outcome": "33°C",
                    "probability": 0.657155398570265,
                    "probability_pct": 65.72,
                    "probability_field": "hard_source_constrained_q_temperature_bucket_posterior",
                },
                {
                    "outcome": "34°C",
                    "probability": 0.21099351914720627,
                    "probability_pct": 21.10,
                    "probability_field": "hard_source_constrained_q_temperature_bucket_posterior",
                },
                {
                    "outcome": "30°C",
                    "probability": 0.0,
                    "probability_pct": 0.0,
                    "probability_field": "q_temperature_bucket_posterior",
                },
            ],
        },
        "temperature_path_distribution": {
            "available": True,
            "target_bucket": "33°C",
            "target_bucket_posterior": 0.0006,
            "target_bucket_posterior_pct": 0.06,
            "target_bucket_path_probability": 1.0,
            "second_target_bucket": "34°C",
            "second_target_bucket_posterior": 0.0002,
            "second_target_bucket_posterior_pct": 0.02,
            "second_target_bucket_path_probability": 0.0496,
            "target": {
                "bucket": "33°C",
                "posterior": 0.0006,
                "posterior_pct": 0.06,
                "path_probability": 1.0,
            },
            "second_target": {
                "bucket": "34°C",
                "posterior": 0.0002,
                "posterior_pct": 0.02,
                "path_probability": 0.0496,
            },
            "bucket_distribution": [
                {"bucket": "30°C", "posterior": 0.7278, "posterior_pct": 72.78, "path_probability": 1.0},
                {"bucket": "33°C", "posterior": 0.0006, "posterior_pct": 0.06, "path_probability": 1.0},
                {"bucket": "34°C", "posterior": 0.0002, "posterior_pct": 0.02, "path_probability": 0.0496},
            ],
            "trade_follow": {"allowed": True, "reason": "posterior_path_reliability_and_freshness_pass"},
        },
        "chengdu_intraday_summary": {
            "temperature_path_distribution": {
                "target_bucket": "33°C",
                "target_bucket_posterior_pct": 0.06,
            }
        },
    }

    out["posterior_temperature_distribution"] = {
        "version": "continuous_temperature_posterior_v1",
        "target_variable": "settlement_daily_max_c",
        "status": "ok",
        "grid": [],
        "cdf_values": [],
        "quantiles_c": {},
        "quantile_grid": [],
        "bucket_probabilities": [
            {"outcome": "30°C", "probability": 0.0},
            {"outcome": "31°C", "probability": 0.0},
            {"outcome": "32°C", "probability": 0.0},
            {"outcome": "33°C", "probability": 0.657155398570265},
            {"outcome": "34°C", "probability": 0.21099351914720627},
        ],
        "source_components": {},
        "calibration": {},
        "observation_constraints": {"d0_violation_count": 0},
    }
    server._sync_temperature_path_distribution_posterior_from_current_distribution(out)
    server._attach_target_realization_capital_plan(out)

    path = out["temperature_path_distribution"]
    assert path["posterior_source"] == "posterior_temperature_distribution.bucket_probabilities"
    assert path["target_bucket_posterior"] == 0.657155398570265
    assert path["target_bucket_posterior_pct"] == 65.72
    assert path["target_bucket_posterior_field"] == "posterior_temperature_distribution.bucket_probabilities"
    assert path["second_target_bucket_posterior"] == 0.21099351914720627
    buckets = {row["bucket"]: row for row in path["bucket_distribution"]}
    assert buckets["30°C"]["posterior"] == 0.0
    assert buckets["33°C"]["posterior"] == 0.657155398570265
    assert out["chengdu_intraday_summary"]["temperature_path_distribution"]["target_bucket_posterior_pct"] == 65.72
    assert out["target_realization_capital_plan"]["target_bucket"] == "33°C"
    assert out["target_realization_capital_plan"]["target_posterior"] == 0.657155398570265


def test_target_realization_plan_does_not_fallback_when_path_distribution_probability_missing() -> None:
    item = {
        "slug": "highest-temperature-in-chengdu-on-june-22-2026",
        "event_date": "2026-06-22",
        "run_date": "2026-06-21",
        "distribution": [
            {
                "outcome": "30°C",
                "q_temperature_bucket_posterior": 0.20,
                "peak_estimator_mu_c": 28.9,
                "peak_estimator_sigma_c": 1.25,
                "best_bid": 0.20,
                "best_ask": 0.21,
            },
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.18,
                "peak_estimator_mu_c": 28.9,
                "peak_estimator_sigma_c": 1.25,
                "best_bid": 0.18,
                "best_ask": 0.19,
            },
        ],
        "temperature_path_distribution": {
            "available": True,
            "target_bucket": "30°C",
            "target_bucket_posterior": 0.24598122644339906,
            "target_bucket_path_probability": None,
            "second_target_bucket": "31°C",
            "second_target_bucket_posterior": 0.19461077408328195,
            "second_target_bucket_path_probability": None,
            "freshness": {"source_stale": False, "observation_age_minutes": None},
            "reliability": {"state": "low", "label": "Low", "score": 0.3146},
            "error_band": {"sigma_c": 1.25, "source": "default_error_band"},
            "trade_follow": {"allowed": False, "reason": "blocked_path_reliability_low"},
            "risk_explanation": "路径可兑现缺失，禁止跟随。",
        },
    }

    plan = server._build_target_realization_capital_plan(item)
    legs = {(leg["role"], leg["bucket"], leg["side"]): leg for leg in plan["legs"]}
    target_leg = legs[("target_yes", "30°C", "YES")]
    second_leg = legs[("second_target_yes", "31°C", "YES")]

    assert plan["target_posterior"] == 0.20
    assert plan["second_target_posterior"] == 0.18
    assert target_leg["target_realization_path_probability"] is None
    assert second_leg["target_realization_path_probability"] is None
    assert target_leg["target_realization_reliability"] == 0.0
    assert second_leg["target_realization_reliability"] == 0.0
    assert target_leg["target_realization_reliability_source"] == "temperature_path_distribution_path_probability_missing"
    assert second_leg["target_realization_reliability_source"] == "temperature_path_distribution_path_probability_missing"
    assert plan["ui_risk_explanation"]["target_realization_probability"] is None
    assert plan["ui_risk_explanation"]["second_target_realization_probability"] is None
    assert plan["temperature_path_trade_follow_block_yes"] is True


def test_ui_snapshot_summary_slim_keeps_target_realization_risk_explanation() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-21-2026",
        "event_date": "2026-06-21",
        "target_realization_capital_plan": {
            "ok": True,
            "target_bucket": "25°C or below",
            "target_posterior": 0.42016291281991425,
            "second_target_bucket": "26°C",
            "second_target_posterior": 0.2817846528381569,
            "ui_risk_explanation": {
                "available": True,
                "target_bucket": "25°C or below",
                "target_posterior": 0.42016291281991425,
                "target_realization_probability": 0.5,
                "second_target_bucket": "26°C",
                "second_target_posterior": 0.2817846528381569,
                "second_target_realization_probability": 0.2119,
                "freshness": {"source_stale": False, "observation_age_minutes": 29.2},
                "reliability": {"state": "high", "label": "High", "score": 0.8115},
                "trade_follow": {
                    "allowed": True,
                    "reason": "posterior_path_reliability_and_freshness_pass",
                },
                "text": "目标 25°C or below posterior 42.02%，路径可兑现 50.0%。",
            },
            "legs": [
                {
                    "role": "target_yes",
                    "bucket": "25°C or below",
                    "side": "YES",
                    "posterior": 0.42016291281991425,
                    "target_realization_path_probability": 0.5,
                    "target_realization_reliability": 0.5,
                }
            ],
        },
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    risk = slim["target_realization_capital_plan"]["ui_risk_explanation"]

    assert risk["target_posterior"] == 0.42016291281991425
    assert risk["target_realization_probability"] == 0.5
    assert risk["second_target_realization_probability"] == 0.2119
    assert risk["trade_follow"]["allowed"] is True
    assert "落桶后验" in risk["text"]
    assert "最终后验" not in risk["text"]
    assert "路径执行概率" in risk["text"]


def test_target_switch_context_respects_capital_plan_target_even_when_distribution_top_differs() -> None:
    item = {
        "slug": "highest-temperature-in-chengdu-on-june-21-2026",
        "event_date": "2026-06-21",
        "run_date": "2026-06-20",
        "rounding_rule": "whole_degrees",
        "distribution": [
            {
                "outcome": "25°C or below",
                "q_bayes_posterior": 0.31,
                "q_temperature_bucket_posterior": 0.31,
                "best_bid": 0.54,
                "best_ask": 0.55,
            },
            {
                "outcome": "26°C",
                "q_bayes_posterior": 0.24,
                "q_temperature_bucket_posterior": 0.24,
                "best_bid": 0.29,
                "best_ask": 0.30,
                "edge_tradeability_pass_yes": True,
            },
            {
                "outcome": "27°C",
                "q_bayes_posterior": 0.18,
                "q_temperature_bucket_posterior": 0.18,
                "best_bid": 0.18,
                "best_ask": 0.19,
                "edge_tradeability_pass_yes": True,
            },
        ],
        "target_bucket_outlook": {
            "available": True,
            "target_bucket": "25°C or below",
            "second_target_bucket": "27°C",
            "second_target_outlook": {
                "available": True,
                "second_target_bucket": "27°C",
            },
        },
        "target_realization_capital_plan": {
            "ok": True,
            "source": "ui_snapshot_ssot",
            "target_bucket": "26°C",
            "target_posterior": 0.24,
            "second_target_bucket": "27°C",
            "second_target_posterior": 0.18,
            "legs": [
                {"bucket": "26°C", "side": "YES", "action": "hold"},
                {"bucket": "27°C", "side": "YES", "action": "hold"},
            ],
        },
    }

    context = server.build_target_switch_context(item)

    assert context["target_bucket"] == "26°C"
    assert context["target_probability_pct"] == 24.0
    assert context["candidate_bucket"] == "27°C"


def test_target_switch_context_exposes_ssot_trajectory_momentum_impact() -> None:
    item = {
        "slug": "highest-temperature-in-chengdu-on-june-29-2026",
        "event_date": "2026-06-29",
        "run_date": "2026-06-29",
        "rounding_rule": "whole_degrees",
        "market_var": "temperature_2m_max",
        "distribution": [
            {
                "outcome": "29°C",
                "q_temperature_bucket_posterior": 0.2517,
                "best_bid": 0.24,
                "best_ask": 0.27,
                "edge_tradeability_pass_yes": True,
            },
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.2426,
                "best_bid": 0.21,
                "best_ask": 0.25,
                "edge_tradeability_pass_yes": True,
            },
        ],
        "weather_likelihood_features_compact": {
            "observed_today_max_c": 25.0,
            "observed_temp_c": 23.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "temperature_momentum_effective_c_per_hour": -1.0,
            "temperature_momentum_realization_state": "cooling",
            "temperature_trajectory_stable_rate_source": "zuuu_metar_raw_awc:1h",
            "physical_weather_adjusted_peak_c": 28.7,
            "physical_weather_state": "cooling",
            "physical_weather_peak_mu_adjustment_c": -0.8,
            "physical_weather_path_probability_scale": 0.824,
        },
        "temperature_path_distribution": {
            "available": True,
            "target_bucket": "29°C",
            "target_bucket_path_probability_pct": 35.84,
            "target_bucket_posterior_pct": 25.17,
            "second_target_bucket": "31°C",
            "second_target_bucket_path_probability_pct": 8.6,
            "second_target_bucket_posterior_pct": 24.26,
            "peak_estimator_mu_c": 28.7,
            "peak_estimator_sigma_c": 1.83,
            "physical_weather_state": "cooling",
            "physical_weather_peak_mu_adjustment_c": -0.8,
            "physical_weather_path_probability_scale": 0.824,
            "observed_path_temp_source": "observed_today_max_floor",
            "trade_follow": {"allowed": False, "reason": "blocked_target_path_lcb_low"},
        },
        "target_realization_capital_plan": {
            "ok": True,
            "target_bucket": "29°C",
            "target_posterior": 0.2517,
            "second_target_bucket": "31°C",
            "second_target_posterior": 0.2426,
            "legs": [
                {"bucket": "29°C", "side": "YES", "action": "hold"},
                {"bucket": "31°C", "side": "YES", "action": "hold"},
            ],
        },
    }

    context = server.build_target_switch_context(item)
    impact = context["trajectory_momentum_impact"]

    assert context["target_bucket"] == "29°C"
    assert context["candidate_bucket"] == "31°C"
    assert impact["target_bucket"] == "29°C"
    assert impact["candidate_bucket"] == "31°C"
    assert impact["direction"] == "target_supported_higher_candidate_pressed"
    assert impact["momentum_rate_c_per_hour"] == -1.0
    assert impact["target_path_probability_pct"] == pytest.approx(35.84)
    assert impact["candidate_path_probability_pct"] == pytest.approx(8.6)
    assert impact["physical_peak_adjustment_c"] == pytest.approx(-0.8)
    assert "支持维持 29°C，压制上切 31°C" in impact["summary"]
    assert "动量 -1.00°C/h" in impact["summary"]


def test_target_switch_context_keeps_sub_one_percent_path_probability_as_pct() -> None:
    item = {
        "slug": "highest-temperature-in-chengdu-on-june-29-2026",
        "event_date": "2026-06-29",
        "run_date": "2026-06-29",
        "rounding_rule": "whole_degrees",
        "market_var": "temperature_2m_max",
        "distribution": [
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.293731,
                "best_bid": 0.30,
                "best_ask": 0.31,
                "edge_tradeability_pass_yes": True,
            },
            {
                "outcome": "30°C",
                "q_temperature_bucket_posterior": 0.273432,
                "best_bid": 0.22,
                "best_ask": 0.23,
                "edge_tradeability_pass_yes": True,
            },
        ],
        "weather_likelihood_features_compact": {
            "observed_today_max_c": 25.0,
            "observed_temp_c": 23.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "temperature_momentum_effective_c_per_hour": 1.0,
            "temperature_momentum_realization_state": "sustained_warming",
            "physical_weather_adjusted_peak_c": 25.59,
            "physical_weather_state": "cooling",
            "physical_weather_peak_mu_adjustment_c": -0.8,
            "physical_weather_path_probability_scale": 0.824,
        },
        "temperature_path_distribution": {
            "available": True,
            "target_bucket": "31°C",
            "target_bucket_path_probability_pct": 2.12,
            "second_target_bucket": "30°C",
            "second_target_bucket_path_probability_pct": 0.87,
            "target": {"bucket": "31°C", "path_probability_pct": 2.12},
            "second_target": {"bucket": "32°C", "path_probability_pct": 0.87},
            "bucket_distribution": [
                {"bucket": "31°C", "path_probability_pct": 2.12},
                {"bucket": "30°C", "path_probability_pct": 0.87},
            ],
            "peak_estimator_mu_c": 25.59,
            "peak_estimator_sigma_c": 2.78,
            "physical_weather_state": "cooling",
            "physical_weather_peak_mu_adjustment_c": -0.8,
            "physical_weather_path_probability_scale": 0.824,
            "trade_follow": {"allowed": False, "reason": "blocked_path_reliability_low"},
        },
        "target_realization_capital_plan": {
            "ok": True,
            "target_bucket": "31°C",
            "target_posterior": 0.293731,
            "second_target_bucket": "30°C",
            "second_target_posterior": 0.273432,
            "legs": [
                {"bucket": "31°C", "side": "YES", "action": "hold"},
                {"bucket": "30°C", "side": "YES", "action": "hold"},
            ],
        },
    }

    context = server.build_target_switch_context(item)
    impact = context["trajectory_momentum_impact"]

    assert context["target_bucket"] == "31°C"
    assert context["candidate_bucket"] == "30°C"
    assert impact["target_path_probability_pct"] == pytest.approx(2.12)
    assert impact["candidate_path_probability_pct"] == pytest.approx(0.87)
    assert "30°C 路径 0.9%" in impact["summary"]
    assert "30°C 路径 87.0%" not in impact["summary"]


def test_temperature_path_distribution_synthesizes_bucket_distribution_from_ssot_distribution() -> None:
    payload = {
        "distribution": [
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.2517},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.2426},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.2325},
        ],
        "temperature_path_distribution": {
            "target_bucket": "29°C",
            "target_bucket_path_probability": 0.3584,
            "second_target_bucket": "31°C",
            "second_target_bucket_path_probability": 0.086,
        },
    }

    attach_ctp_distribution(payload)
    server._sync_temperature_path_distribution_posterior_from_current_distribution(payload)

    path_distribution = payload["temperature_path_distribution"]
    bucket_distribution = path_distribution["bucket_distribution"]
    by_bucket = {row["bucket"]: row for row in bucket_distribution}

    assert path_distribution["bucket_distribution_source"] == "posterior_temperature_distribution.bucket_probabilities"
    assert path_distribution["bucket_distribution_count"] == 3
    assert by_bucket["29°C"]["posterior"] == pytest.approx(0.2517)
    assert by_bucket["29°C"]["path_probability"] == pytest.approx(0.3584)
    assert by_bucket["29°C"]["path_probability_source"] == "temperature_path_distribution.target_bucket_path_probability"
    assert by_bucket["31°C"]["posterior"] == pytest.approx(0.2426)
    assert by_bucket["31°C"]["path_probability"] == pytest.approx(0.086)
    assert by_bucket["30°C"]["path_probability_available"] is False


def test_temperature_path_distribution_rebuilds_truncated_bucket_distribution() -> None:
    payload = {
        "distribution": [
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.21},
            {"outcome": "30°C or higher", "q_temperature_bucket_posterior": 0.32},
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.24},
        ],
        "temperature_path_distribution": {
            "target_bucket": "30°C or higher",
            "target_bucket_path_probability": 0.5264,
            "second_target_bucket": "28°C",
            "bucket_distribution": [
                {"bucket": "20°C or below", "posterior": 0.01},
                {"bucket": "21°C", "posterior": 0.02},
            ],
        },
    }

    attach_ctp_distribution(payload)
    server._sync_temperature_path_distribution_posterior_from_current_distribution(payload)

    path_distribution = payload["temperature_path_distribution"]
    by_bucket = {row["bucket"]: row for row in path_distribution["bucket_distribution"]}

    assert path_distribution["bucket_distribution_count"] == 3
    assert by_bucket["30°C or higher"]["posterior"] == pytest.approx(0.32)
    assert by_bucket["30°C or higher"]["path_probability"] == pytest.approx(0.5264)
    assert by_bucket["28°C"]["posterior"] == pytest.approx(0.24)


def test_temperature_path_distribution_fills_second_path_probability_from_bucket_row() -> None:
    payload = {
        "distribution": [
            {"outcome": "30°C or higher", "q_temperature_bucket_posterior": 0.32},
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.24},
        ],
        "temperature_path_distribution": {
            "target_bucket": "30°C or higher",
            "target_bucket_path_probability": 0.5264,
            "second_target_bucket": "28°C",
            "bucket_distribution": [
                {
                    "bucket": "30°C or higher",
                    "posterior": 0.32,
                    "path_probability": 0.5264,
                    "path_probability_source": "soft_forecast_error_band",
                },
                {
                    "bucket": "28°C",
                    "posterior": 0.24,
                    "path_probability": 0.9176,
                    "path_probability_source": "soft_forecast_error_band",
                },
            ],
        },
    }

    attach_ctp_distribution(payload)
    server._sync_temperature_path_distribution_posterior_from_current_distribution(payload)

    path_distribution = payload["temperature_path_distribution"]
    assert path_distribution["second_target_bucket_path_probability"] == pytest.approx(0.9176)
    assert path_distribution["second_target_bucket_path_probability_pct"] == pytest.approx(91.76)
    assert path_distribution["second_target_bucket_path_probability_source"] == "soft_forecast_error_band"


def test_temperature_path_distribution_relabels_stale_nested_second_target_to_ssot_bucket() -> None:
    payload = {
        "distribution": [
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.2937},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.2734},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.0981},
        ],
        "temperature_path_distribution": {
            "target_bucket": "31°C",
            "target_bucket_path_probability": 0.0212,
            "second_target_bucket": "30°C",
            "second_target_bucket_path_probability": 0.0087,
            "target": {"bucket": "31°C", "path_probability": 0.0212},
            "second_target": {"bucket": "32°C", "path_probability": 0.0087},
            "bucket_distribution": [
                {"bucket": "31°C", "posterior": 0.2937, "path_probability": 0.0212},
                {"bucket": "30°C", "posterior": 0.2734, "path_probability": 0.0087},
                {"bucket": "32°C", "posterior": 0.0981, "path_probability": 0.0087},
            ],
        },
    }

    attach_ctp_distribution(payload)
    server._sync_temperature_path_distribution_posterior_from_current_distribution(payload)

    path_distribution = payload["temperature_path_distribution"]
    assert path_distribution["second_target_bucket"] == "30°C"
    assert path_distribution["second_target"]["bucket"] == "30°C"
    assert path_distribution["second_target"]["posterior"] == pytest.approx(0.2734)
    assert path_distribution["second_target"]["path_probability"] == pytest.approx(0.0087)


def test_temperature_path_distribution_overwrites_stale_bucket_row_path_probability() -> None:
    payload = {
        "distribution": [
            {"outcome": "28°C", "q_temperature_bucket_posterior": 0.26},
            {"outcome": "30°C or higher", "q_temperature_bucket_posterior": 0.22},
        ],
        "temperature_path_distribution": {
            "target_bucket": "28°C",
            "target_bucket_path_probability": 0.9176,
            "second_target_bucket": "30°C or higher",
            "second_target_bucket_path_probability": 0.7665,
            "second_target_bucket_path_probability_source": "soft_forecast_error_band",
            "bucket_distribution": [
                {"bucket": "28°C", "posterior": 0.26, "path_probability": 0.9176},
                {
                    "bucket": "30°C or higher",
                    "posterior": 0.22,
                    "path_probability": 0.5264,
                    "path_probability_source": "stale_previous_target_probability",
                },
            ],
        },
    }

    attach_ctp_distribution(payload)
    server._sync_temperature_path_distribution_posterior_from_current_distribution(payload)

    by_bucket = {
        row["bucket"]: row
        for row in payload["temperature_path_distribution"]["bucket_distribution"]
    }
    assert by_bucket["30°C or higher"]["path_probability"] == pytest.approx(0.7665)
    assert by_bucket["30°C or higher"]["path_probability_source"] == "soft_forecast_error_band"


def test_most_likely_history_bucket_series_keeps_bayes_chain(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            fact_json TEXT NOT NULL
        )
        """
    )
    fact = {
        "distribution": [
            {
                "outcome": "31°C",
                "q_round": 0.37,
                "q_ensemble_qm_bucket": 0.30,
                "q_temperature_bucket_posterior": 0.84,
                "q_bayes_posterior": 0.84,
                "q_bayes_prior_strength": 12.0,
                "q_temperature_bucket_score_components": {
                    "history": 3.0,
                    "model": 9.0,
                    "wu_forecast": 1.5,
                    "taf_peak": 2.0,
                    "temperature_trajectory": 4.5,
                    "independent_physics_residual": 0.8,
                },
                "q_bayes_model_evidence": 30.0,
                "q_bayes_wu_forecast_evidence": 6.0,
                "q_bayes_wu_forecast_prob": 0.25,
                "q_bayes_taf_peak_evidence": 8.0,
                "q_bayes_taf_peak_prob": 0.25,
                "q_bayes_temperature_trajectory_evidence": 9.0,
                "q_bayes_temperature_trajectory_prob": 0.5,
                "q_bayes_temperature_trajectory_role": "intraday_temperature_trajectory_likelihood",
                "q_bayes_temperature_trajectory_dedupe_policy": "primary intraday temperature signal",
                "q_bayes_independent_physics_residual_evidence": 1.0,
                "q_bayes_independent_physics_residual_prob": 0.8,
            },
            {
                "outcome": "30°C",
                "q_round": 0.63,
                "q_ensemble_qm_bucket": 0.70,
                "q_temperature_bucket_posterior": 0.16,
                "q_bayes_posterior": 0.16,
            },
        ]
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "2026-06-18",
            "highest-temperature-in-chengdu-on-june-18-2026",
            "2026-06-18",
            "2026-06-18T12:00:00+00:00",
            json.dumps(fact),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(server, "DB_PATH", db_path)

    data = server.query_most_likely_history(
        "highest-temperature-in-chengdu-on-june-18-2026",
        "2026-06-18",
        "equal",
        "2026-06-18",
    )

    latest = data["bucket_series"]["31°C"][-1]
    chain = latest["bayes_chain"]
    assert latest["chain"] == chain
    assert latest["q_prior"] == 0.25
    assert latest["q_likelihood"] is not None
    assert latest["q_posterior"] == 0.84
    assert chain["likelihood_parts"]["model"]["prob"] == 0.30
    assert chain["likelihood_parts"]["wu_forecast"]["prob"] == 0.25
    assert chain["likelihood_parts"]["temperature_trajectory"]["prob"] == 0.5
    assert chain["likelihood_parts"]["temperature_trajectory"]["evidence"] == 9.0


def test_event_run_composer_overlay_recomputes_temperature_trajectory_posterior(monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_TEMPERATURE_TRAJECTORY_EVIDENCE", "12")
    rows = [
        {
            "outcome": "26°C",
            "q_bayes_posterior": 1 / 3,
            "q_temperature_bucket_posterior": 1 / 3,
            "q_bayes_posterior_pre_observed_constraints": 1 / 3,
            "q_temperature_bucket_posterior_pre_observed_constraints": 1 / 3,
            "q_posterior_observed_constraints_applied": True,
            "q_temperature_bucket_score_components": {"history": 5.0, "model": 5.0},
        },
        {
            "outcome": "27°C",
            "q_bayes_posterior": 1 / 3,
            "q_temperature_bucket_posterior": 1 / 3,
            "q_bayes_posterior_pre_observed_constraints": 1 / 3,
            "q_temperature_bucket_posterior_pre_observed_constraints": 1 / 3,
            "q_posterior_observed_constraints_applied": True,
            "q_temperature_bucket_score_components": {"history": 5.0, "model": 5.0},
        },
        {
            "outcome": "28°C",
            "q_bayes_posterior": 1 / 3,
            "q_temperature_bucket_posterior": 1 / 3,
            "q_bayes_posterior_pre_observed_constraints": 1 / 3,
            "q_temperature_bucket_posterior_pre_observed_constraints": 1 / 3,
            "q_posterior_observed_constraints_applied": True,
            "q_temperature_bucket_score_components": {"history": 5.0, "model": 5.0},
        },
    ]
    meta = server._overlay_temperature_trajectory_posterior_for_composed_rows(
        rows,
        {
            "market_var": "temperature_2m_max",
            "observed_temp_c": 25.0,
            "observation_hour_local": 12.0,
            "temperature_momentum_posterior_effective_c_per_hour": 0.8,
            "temperature_momentum_realization_state": "sustained_warming",
            "temperature_momentum_posterior_gate_allowed": True,
            "peak_estimator": {
                "enabled": True,
                "source": "test_peak_estimator",
                "mu_c": 28.2,
                "extrapolation_c": 28.2,
                "sigma_c": 0.35,
                "extrapolation_sigma_c": 0.35,
                "hours_to_peak_window": 2.0,
                "temperature_momentum_realization_state": "sustained_warming",
            },
        },
        rounding_rule="rounded_degrees",
    )

    row_by_outcome = {row["outcome"]: row for row in rows}
    assert meta["applied"] is True
    assert meta["role"] == "intraday_temperature_trajectory_likelihood"
    assert row_by_outcome["28°C"]["q_bayes_posterior"] > row_by_outcome["27°C"]["q_bayes_posterior"]
    assert row_by_outcome["28°C"]["q_temperature_bucket_posterior"] == row_by_outcome["28°C"]["q_bayes_posterior"]
    assert (
        row_by_outcome["28°C"]["q_bayes_posterior_pre_observed_constraints"]
        == row_by_outcome["28°C"]["q_bayes_posterior"]
    )
    assert row_by_outcome["28°C"]["q_posterior_observed_constraints_rebased_by_temperature_trajectory"] is True
    assert row_by_outcome["28°C"]["q_bayes_temperature_trajectory_evidence"] > 0
    assert row_by_outcome["28°C"]["q_bayes_temperature_trajectory_enabled"] is True
    assert row_by_outcome["28°C"]["q_bayes_temperature_trajectory_role"] == "intraday_temperature_trajectory_likelihood"
    assert row_by_outcome["28°C"]["q_bayes_temperature_trajectory_source_class"] == "airport_temperature_path"
    assert row_by_outcome["28°C"]["q_bayes_temperature_trajectory_weaker_than_hard_source"] is True
    assert (
        row_by_outcome["28°C"]["q_temperature_bucket_score_components"]["temperature_trajectory"]
        > row_by_outcome["26°C"]["q_temperature_bucket_score_components"]["temperature_trajectory"]
    )


def test_event_run_composer_rebuilds_peak_hour_for_temperature_trajectory(monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_TEMPERATURE_TRAJECTORY_EVIDENCE", "12")
    rows = [
        {
            "outcome": "26°C",
            "q_bayes_posterior": 1 / 3,
            "q_temperature_bucket_score_components": {"history": 5.0, "model": 5.0},
        },
        {
            "outcome": "27°C",
            "q_bayes_posterior": 1 / 3,
            "q_temperature_bucket_score_components": {"history": 5.0, "model": 5.0},
        },
        {
            "outcome": "28°C",
            "q_bayes_posterior": 1 / 3,
            "q_temperature_bucket_score_components": {"history": 5.0, "model": 5.0},
        },
    ]
    meta = server._overlay_temperature_trajectory_posterior_for_composed_rows(
        rows,
        {
            "market_var": "temperature_2m_max",
            "observed_temp_c": 25.0,
            "forecast_peak_temp": 28.0,
            "expected_peak_hour_local": 16.0,
            "temperature_momentum_latest_observation_at": "2026-06-24T03:00:00+00:00",
            "temperature_momentum_posterior_effective_c_per_hour": 0.8,
            "temperature_momentum_realization_state": "sustained_warming",
            "temperature_momentum_posterior_gate_allowed": True,
            "peak_estimator": {
                "enabled": False,
                "source": "insufficient_peak_estimator_features",
                "has_observed_temp": True,
                "has_forecast_peak": True,
                "has_hour_local": False,
            },
        },
        rounding_rule="rounded_degrees",
        event_tz="Asia/Shanghai",
    )

    assert meta["applied"] is True
    assert meta["status"] == "enabled"
    assert rows[2]["q_bayes_temperature_trajectory_evidence"] > 0
    assert rows[2]["q_bayes_temperature_trajectory_source"]


def test_weather_likelihood_compact_downgrades_one_hour_awc_temperature_momentum_gate() -> None:
    compact = server._compact_weather_likelihood_features_for_ui(
        {
            "observed_temp_c": 33.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "temperature_rise_rate_c_per_hour": 2.0,
            "temperature_rise_rate_source": "zuuu_metar_raw_awc:1h",
            "temperature_rise_rate_window": "1h",
            "temperature_momentum_source": "zuuu_metar_raw_awc",
            "temperature_momentum_available_horizons": "12h,6h,3h,2h,1h",
            "temperature_momentum_effective_windows": "1h",
            "temperature_momentum_12h_c_per_hour": 0.83,
            "temperature_momentum_6h_c_per_hour": 1.33,
            "temperature_momentum_3h_c_per_hour": 0.67,
            "temperature_momentum_2h_c_per_hour": 1.0,
            "temperature_momentum_1h_c_per_hour": 2.0,
            "temperature_momentum_1h_actual_hours": 1.0,
            "temperature_momentum_1h_requested_hours": 1.0,
            "temperature_momentum_1h_window_quality": "exact_or_near_requested_window",
            "temperature_momentum_posterior_gate_allowed": True,
            "temperature_momentum_posterior_gate_state": "confirmed",
            "temperature_momentum_posterior_gate_reason": "one_hour_context_confirmed",
            "temperature_momentum_posterior_confirmed_windows": "1h",
            "temperature_momentum_posterior_effective_c_per_hour": 2.0,
        }
    )

    assert compact["temperature_momentum_source"] == "zuuu_metar_raw_awc"
    assert compact["temperature_momentum_available_horizons"] == "12h,6h,3h,2h,1h"
    assert compact["temperature_momentum_effective_windows"] == "1h"
    assert compact["temperature_momentum_1h_c_per_hour"] == 2.0
    assert compact["temperature_momentum_posterior_gate_allowed"] is False
    assert compact["temperature_momentum_posterior_gate_state"] == "one_hour_watch"
    assert compact["temperature_momentum_posterior_gate_reason"] == "one_hour_requires_30m_45m_confirmation"
    assert compact["temperature_momentum_posterior_context_windows"] == "2h,3h,6h"


def test_weather_likelihood_compact_preserves_context_momentum_peak_window() -> None:
    compact = server._compact_weather_likelihood_features_for_ui(
        {
            "observed_temp_c": 33.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "temperature_momentum_source": "zuuu_metar_raw_awc",
            "temperature_momentum_available_horizons": "12h,6h,3h,2h,1h",
            "temperature_momentum_12h_c_per_hour": 0.65,
            "temperature_momentum_6h_c_per_hour": 1.1,
            "temperature_momentum_3h_c_per_hour": 0.9,
            "temperature_momentum_2h_c_per_hour": 0.8,
            "temperature_momentum_1h_c_per_hour": 0.0,
            "temperature_momentum_realization_state": "context_warming_watch",
            "temperature_momentum_context_effective_c_per_hour": 0.88,
            "temperature_momentum_context_positive_window_count": 4,
            "temperature_momentum_context_warming_watch": True,
            "expected_peak_hour_local": 15.0,
            "peak_window_start_hour_local": 14.0,
            "peak_window_end_hour_local": 17.0,
            "hours_to_peak_window": 2.5,
            "lock_threshold_hour_local": 17.0,
            "peak_estimator": {
                "enabled": True,
                "source": "kalman_observed_forecast_peak",
                "mu_c": 33.0,
                "sigma_c": 0.58,
                "hours_to_peak_window": 2.5,
                "temperature_momentum_realization_state": "context_warming_watch",
                "temperature_momentum_context_effective_c_per_hour": 0.88,
            },
        }
    )

    assert compact["temperature_momentum_realization_state"] == "context_warming_watch"
    assert compact["temperature_momentum_context_effective_c_per_hour"] == 0.88
    assert compact["temperature_momentum_context_warming_watch"] is True
    assert compact["peak_window_start_hour_local"] == 14.0
    assert compact["peak_window_end_hour_local"] == 17.0
    assert compact["hours_to_peak_window"] == 2.5
    assert compact["peak_estimator"]["hours_to_peak_window"] == 2.5


def test_weather_likelihood_compact_filters_stale_awc_momentum_window_metadata() -> None:
    compact = server._compact_weather_likelihood_features_for_ui(
        {
            "observed_temp_c": 33.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "temperature_rise_rate_c_per_hour": 2.0,
            "temperature_rise_rate_source": "zuuu_metar_raw_awc:1h",
            "temperature_momentum_source": "zuuu_metar_raw_awc",
            "temperature_momentum_available_horizons": "12h,6h,3h,2h,1h,45m,30m",
            "temperature_momentum_effective_windows": "1h,45m,30m",
            "temperature_momentum_1h_c_per_hour": 2.0,
            "temperature_momentum_posterior_gate_allowed": True,
            "temperature_momentum_posterior_gate_state": "confirmed",
            "temperature_momentum_posterior_gate_reason": "stable_multi_window_confirmed",
            "temperature_momentum_posterior_confirmed_windows": "30m,45m,1h",
            "temperature_momentum_posterior_confirmed_window_count": 3,
            "temperature_momentum_realization_state": "sustained_warming",
            "temperature_momentum_sustained_warming": True,
        }
    )

    assert compact["temperature_momentum_available_horizons"] == "1h"
    assert compact["temperature_momentum_effective_windows"] == "1h"
    assert compact["temperature_momentum_posterior_confirmed_windows"] == "1h"
    assert compact["temperature_momentum_posterior_confirmed_window_count"] == 1
    assert compact["temperature_momentum_posterior_gate_allowed"] is False
    assert compact["temperature_momentum_posterior_gate_reason"] == "one_hour_requires_30m_45m_confirmation"
    assert compact["temperature_momentum_realization_state"] == "emerging_warming"
    assert compact["temperature_momentum_sustained_warming"] is False


def test_awc_momentum_metadata_normalizer_drops_subhour_rate_without_requested_hours() -> None:
    normalized = server._normalize_awc_primary_temperature_momentum_metadata(
        {
            "observed_temp_source": "zuuu_metar_raw_awc",
            "temperature_momentum_source": "zuuu_metar_raw_awc",
            "temperature_momentum_available_horizons": "1h,30m",
            "temperature_momentum_effective_windows": "1h,30m",
            "temperature_momentum_1h_c_per_hour": 2.0,
            "temperature_momentum_1h_actual_hours": 1.0,
            "temperature_momentum_30m_c_per_hour": 0.0,
            "temperature_momentum_30m_actual_hours": 1.0,
            "temperature_momentum_posterior_confirmed_windows": "30m,1h",
        }
    )

    assert normalized["temperature_momentum_available_horizons"] == "1h"
    assert normalized["temperature_momentum_effective_windows"] == "1h"
    assert normalized["temperature_momentum_posterior_confirmed_windows"] == "1h"
    assert "temperature_momentum_30m_c_per_hour" not in normalized


def test_final_temperature_trajectory_overlay_keeps_observed_hard_zero(monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_TEMPERATURE_TRAJECTORY_EVIDENCE", "12")
    monkeypatch.setenv("PM_TEMPERATURE_MOMENTUM_CONTEXT_POSTERIOR_GATE_MIN_C_PER_HOUR", "0.45")
    rows = [
        {
            "outcome": "31°C",
            "q_bayes_posterior": 0.25,
            "q_temperature_bucket_posterior": 0.25,
            "q_temperature_bucket_score_components": {"history": 4.0, "model": 4.0},
        },
        {
            "outcome": "32°C",
            "q_bayes_posterior": 0.25,
            "q_temperature_bucket_posterior": 0.25,
            "q_temperature_bucket_score_components": {"history": 4.0, "model": 4.0},
        },
        {
            "outcome": "33°C",
            "q_bayes_posterior": 0.25,
            "q_temperature_bucket_posterior": 0.25,
            "q_temperature_bucket_score_components": {"history": 4.0, "model": 4.0},
        },
        {
            "outcome": "34°C",
            "q_bayes_posterior": 0.25,
            "q_temperature_bucket_posterior": 0.25,
            "q_temperature_bucket_score_components": {"history": 4.0, "model": 4.0},
        },
    ]
    features = {
        "market_var": "temperature_2m_max",
        "observed_temp_c": 33.0,
        "observed_temp_source": "zuuu_metar_raw_awc",
        "observed_today_max_c": 33.0,
        "observed_today_max_source": "zuuu_metar_raw_awc",
        "observed_today_max_trusted": True,
        "observation_hour_local": 14.5,
        "zuuu_metar_raw_awc_today_max_c": 33.0,
        "zuuu_metar_raw_awc_today_max_at": "2026-06-29T08:00:00+00:00",
        "zuuu_metar_raw_awc_latest_at": "2026-06-29T08:00:00+00:00",
        "zuuu_metar_raw_awc_fetched_at": "2026-06-29T08:45:00+00:00",
        "temperature_momentum_source": "zuuu_metar_raw_awc",
        "temperature_momentum_available_horizons": "12h,6h,3h,2h,1h",
        "temperature_momentum_12h_c_per_hour": 0.65,
        "temperature_momentum_6h_c_per_hour": 1.1,
        "temperature_momentum_3h_c_per_hour": 0.9,
        "temperature_momentum_2h_c_per_hour": 0.8,
        "temperature_momentum_1h_c_per_hour": 0.0,
        "temperature_momentum_realization_state": "context_warming_watch",
        "temperature_momentum_context_effective_c_per_hour": 0.88,
        "temperature_momentum_context_warming_watch": True,
        "peak_estimator": {
            "enabled": True,
            "source": "kalman_observed_forecast_peak",
            "mu_c": 33.0,
            "sigma_c": 0.58,
            "hours_to_peak_window": 2.5,
            "temperature_momentum_realization_state": "context_warming_watch",
            "temperature_momentum_context_effective_c_per_hour": 0.88,
        },
    }
    item = {
        "slug": "chengdu-high-temperature-on-june-29",
        "station_id": "ZUUU",
        "city": "Chengdu",
        "market_var": "temperature_2m_max",
        "rounding_rule": "rounded_degrees",
        "distribution": rows,
        "weather_likelihood_features": features,
    }

    changed = server._materialize_final_temperature_trajectory_overlay_for_fact_write(
        item,
        rows,
        features,
        slug=item["slug"],
        event_date="2026-06-29",
        fact_as_of="2026-06-29T08:45:00+00:00",
        market_var="temperature_2m_max",
        rounding_rule="rounded_degrees",
        event_tz="Asia/Shanghai",
        source="test.final_distribution",
    )

    by_outcome = {row["outcome"]: row for row in rows}
    assert changed is True
    assert by_outcome["31°C"]["q_bayes_temperature_trajectory_evidence"] > 0
    assert by_outcome["31°C"]["q_bayes_temperature_trajectory_realization_state"] == "context_warming_watch"
    assert by_outcome["31°C"]["q_temperature_bucket_posterior"] == 0.0
    assert by_outcome["32°C"]["q_temperature_bucket_posterior"] == 0.0
    assert by_outcome["31°C"]["q_posterior_observed_constraints_hard_zero"] is True
    assert by_outcome["32°C"]["q_posterior_observed_constraints_hard_zero"] is True
    assert by_outcome["33°C"]["q_temperature_bucket_posterior"] > 0
    assert by_outcome["34°C"]["q_temperature_bucket_posterior"] > 0
    assert item["hard_source_posterior_override_applied"] is True


def test_awc_fact_refresh_drops_stale_subhour_temperature_momentum() -> None:
    merged = server._merge_awc_features_into_fact_payload(
        {
            "weather_likelihood_features": {
                "observed_temp_c": 32.0,
                "observed_temp_source": "zuuu_metar_raw_awc",
                "zuuu_metar_raw_awc_today_max_c": 32.0,
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_available_horizons": "12h,6h,3h,2h,1h,45m,30m",
                "temperature_momentum_effective_windows": "1h,45m,30m",
                "temperature_momentum_1h_c_per_hour": 1.0,
                "temperature_momentum_45m_c_per_hour": 1.0,
                "temperature_momentum_30m_c_per_hour": 1.0,
                "zuuu_metar_raw_awc_temperature_momentum_45m_c_per_hour": 1.0,
                "zuuu_metar_raw_awc_temperature_momentum_30m_c_per_hour": 1.0,
            },
            "distribution": [{"outcome": "33°C", "q_temperature_bucket_posterior": 1.0}],
        },
        {
            "observed_temp_c": 33.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_latest_at": "2026-06-29T07:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 33.0,
            "zuuu_metar_raw_awc_today_max_c": 33.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-29T07:00:00+00:00",
            "temperature_momentum_source": "zuuu_metar_raw_awc",
            "temperature_momentum_available_horizons": "12h,6h,3h,2h,1h",
            "temperature_momentum_effective_windows": "1h",
            "temperature_momentum_1h_c_per_hour": 2.0,
            "temperature_momentum_posterior_gate_allowed": True,
            "temperature_momentum_posterior_gate_reason": "one_hour_hard_momentum_confirmed",
            "temperature_momentum_posterior_confirmed_windows": "1h",
            "zuuu_metar_raw_awc_temperature_momentum_1h_c_per_hour": 2.0,
            "zuuu_metar_raw_awc_temperature_momentum_available_horizons": "12h,6h,3h,2h,1h",
            "zuuu_metar_raw_awc_temperature_momentum_effective_windows": "1h",
            "zuuu_metar_raw_awc_temperature_momentum_posterior_gate_allowed": True,
            "zuuu_metar_raw_awc_temperature_momentum_posterior_gate_reason": "one_hour_hard_momentum_confirmed",
            "zuuu_metar_raw_awc_temperature_momentum_posterior_confirmed_windows": "1h",
        },
        refreshed_at="2026-06-29T07:57:27+00:00",
    )

    features = merged["weather_likelihood_features"]
    compact = merged["weather_likelihood_features_compact"]
    row = merged["distribution"][0]
    assert features["temperature_momentum_available_horizons"] == "1h"
    assert features["temperature_momentum_effective_windows"] == "1h"
    assert "temperature_momentum_45m_c_per_hour" not in features
    assert "temperature_momentum_30m_c_per_hour" not in features
    assert "zuuu_metar_raw_awc_temperature_momentum_45m_c_per_hour" not in features
    assert "zuuu_metar_raw_awc_temperature_momentum_30m_c_per_hour" not in features
    assert compact["temperature_momentum_posterior_gate_allowed"] is False
    assert compact["temperature_momentum_posterior_gate_reason"] == "one_hour_requires_30m_45m_confirmation"
    assert "temperature_momentum_45m_c_per_hour" not in compact
    assert "temperature_momentum_30m_c_per_hour" not in compact
    assert "temperature_momentum_45m_c_per_hour" not in row
    assert "temperature_momentum_30m_c_per_hour" not in row


def test_awc_merge_fill_preserves_compact_temperature_momentum() -> None:
    merged = server._merge_awc_hard_observation_from_latest_fact(
        {
            "weather_likelihood_features_compact": {
                "observed_temp_c": 32.0,
                "observed_temp_source": "wu_twc_observed_intraday_current",
            },
            "distribution": [{"outcome": "33°C", "q_temperature_bucket_posterior": 1.0}],
        },
        {
            "weather_likelihood_features_compact": {
                "observed_temp_c": 33.0,
                "observed_temp_source": "zuuu_metar_raw_awc",
                "observed_today_max_c": 33.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
                "zuuu_metar_raw_awc_latest_at": "2026-06-29T08:00:00+00:00",
                "zuuu_metar_raw_awc_latest_temp_c": 33.0,
                "zuuu_metar_raw_awc_today_max_c": 33.0,
                "zuuu_metar_raw_awc_today_max_at": "2026-06-29T08:00:00+00:00",
                "temperature_momentum_source": "zuuu_metar_raw_awc",
                "temperature_momentum_available_horizons": "12h,6h,3h,2h,1h",
                "temperature_momentum_effective_windows": "1h",
                "temperature_momentum_1h_c_per_hour": 0.0,
                "temperature_momentum_posterior_gate_reason": "momentum_state_not_realized",
                "temperature_rise_rate_c_per_hour": 0.0,
                "temperature_rise_rate_source": "zuuu_metar_raw_awc:1h",
            }
        },
        existing_fact_as_of="2026-06-29T08:08:55+00:00",
    )

    features = merged["weather_likelihood_features"]
    compact = merged["weather_likelihood_features_compact"]
    assert features["observed_temp_source"] == "zuuu_metar_raw_awc"
    assert features["temperature_momentum_source"] == "zuuu_metar_raw_awc"
    assert features["temperature_momentum_available_horizons"] == "1h"
    assert compact["temperature_momentum_effective_windows"] == "1h"
    assert compact["temperature_momentum_1h_c_per_hour"] == 0.0


def test_weather_likelihood_compact_keeps_source_health_timestamps() -> None:
    compact = server._compact_weather_likelihood_features_for_ui(
        {
            "observed_temp_c": 28.9,
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T11:01:02+00:00",
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T09:00:00+00:00",
            "wu_twc_observed_intraday_fetched_at": "2026-06-18T11:03:44+00:00",
            "wu_twc_observed_intraday_current_at": "2026-06-18T11:03:43+00:00",
            "wu_twc_observed_intraday_today_max_at": "2026-06-18T09:00:00+00:00",
            "observation_hour_local": 12.25,
            "expected_peak_hour_local": 14.0,
            "peak_hour_source": "weather_com_forecast_peak_at",
            "weather_com_forecast_peak_at": "2026-06-18T14:00:00+08:00",
            "precip_nowcast_1_3h_available": True,
            "precip_nowcast_1_3h_checked_at": "2026-06-18T11:12:52+00:00",
            "precip_nowcast_1_3h_reference_time": "2026-06-18T11:00:00+00:00",
            "precip_nowcast_1_3h_source": "open_meteo_forecast_hourly",
            "precip_nowcast_1_3h_role": "future_precipitation_risk_not_observed_precip",
            "precip_nowcast_1_3h_na_reason": "open_meteo_future_window_empty",
            "precip_nowcast_1_3h_response_date": "2026-06-18",
            "precip_nowcast_1_3h_window_hours": 3,
            "precip_nowcast_1_3h_window_rows": 0,
            "precip_nowcast_1_3h_max_rate_mm_per_h": 0.0,
            "precip_nowcast_1_3h_max_probability_pct": 0.0,
            "forecast_precip_nowcast_1_3h_na_reason": "open_meteo_future_window_empty",
        }
    )

    assert compact["zuuu_metar_raw_awc_fetched_at"] == "2026-06-18T11:01:02+00:00"
    assert compact["wu_twc_observed_intraday_current_at"] == "2026-06-18T11:03:43+00:00"
    assert compact["observation_hour_local"] == 12.25
    assert compact["expected_peak_hour_local"] == 14.0
    assert compact["peak_hour_source"] == "weather_com_forecast_peak_at"
    assert compact["weather_com_forecast_peak_at"] == "2026-06-18T14:00:00+08:00"
    assert compact["precip_nowcast_1_3h_checked_at"] == "2026-06-18T11:12:52+00:00"
    assert compact["precip_nowcast_1_3h_source"] == "open_meteo_forecast_hourly"
    assert compact["precip_nowcast_1_3h_role"] == "future_precipitation_risk_not_observed_precip"
    assert compact["precip_nowcast_1_3h_na_reason"] == "open_meteo_future_window_empty"
    assert compact["precip_nowcast_1_3h_window_rows"] == 0
    assert compact["precip_nowcast_1_3h_max_rate_mm_per_h"] == 0.0
    assert compact["precip_nowcast_1_3h_max_probability_pct"] == 0.0
    assert compact["forecast_precip_nowcast_1_3h_na_reason"] == "open_meteo_future_window_empty"

    server_text = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    assert "weather_likelihood_features_compact" in server_text
    assert "_merge_fill_health_mapping" in server_text


def test_index_renders_explicit_metar_pws_multi_source_panel() -> None:
    index_text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "renderEventIntelMultiSourceObservationPanel" in index_text
    assert "${renderEventIntelMultiSourceObservationPanel(item, payload)}" in index_text
    assert "IEM/NOAAPort" in index_text
    assert "AVWX" in index_text
    assert "CheckWX" in index_text
    assert "TWC PWS rapid" in index_text
    assert "辅助预警，不作最终硬结算" in index_text


def test_data_source_health_reads_wu_twc_from_recent_fact_history(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    slug = "chengdu-source-health-test"
    event_date = datetime.now(server.ZoneInfo("Asia/Shanghai")).date().isoformat()
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    wu_fact_as_of = (now_utc - timedelta(seconds=90)).isoformat()
    awc_fact_as_of = now_utc.isoformat()
    wu_current_at = (now_utc - timedelta(seconds=120)).isoformat()
    wu_max_at = (now_utc - timedelta(hours=1)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    wu_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of=wu_fact_as_of,
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "station_id": "ZUUU",
            "weather_likelihood_features": {
                "wu_twc_observed_intraday_fetched_at": wu_fact_as_of,
                "wu_twc_observed_intraday_current_at": wu_current_at,
                "wu_twc_observed_intraday_today_max_at": wu_max_at,
                "wu_twc_observed_intraday_current_temp_c": 24.6,
                "wu_twc_observed_intraday_today_max_c": 31.0,
                "wu_twc_observed_intraday_source_url": "https://example.test/wu",
                "wu_twc_observed_intraday_primary_current": True,
            },
            "distribution": [],
        },
    )
    assert wu_fact_id
    awc_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of=awc_fact_as_of,
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "station_id": "ZUUU",
            "weather_likelihood_features": {
                "source": "zuuu_metar_raw_awc",
                "zuuu_metar_raw_awc_latest_at": awc_fact_as_of,
                "zuuu_metar_raw_awc_latest_temp_c": 24.0,
            },
            "distribution": [],
        },
    )
    server._upsert_event_fact_latest(conn, slug, event_date, awc_fact_id)
    conn.commit()
    conn.close()

    monkeypatch.setattr(server, "DB_PATH", db_path)
    with server._DATA_SOURCE_HEALTH_LOCK:
        server._DATA_SOURCE_HEALTH_CACHE.clear()

    payload = server.data_source_health()
    items = {
        item["key"]: item
        for group in payload.get("groups") or []
        for item in group.get("items") or []
        if isinstance(item, dict)
    }
    wu_item = items["wu_twc_observed_intraday"]

    assert wu_item["status"] == "ok"
    assert wu_item["last_update"] == wu_current_at
    assert wu_item["checked_at"] == wu_fact_as_of
    assert "当前 24.6°C" in wu_item["detail"]
    assert "已观测最高 31.0°C" in wu_item["detail"]


def test_data_source_health_reads_precip_nowcast_from_recent_station_fact(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    event_date = datetime.now(server.ZoneInfo("Asia/Shanghai")).date().isoformat()
    event_dt = datetime.fromisoformat(event_date)
    slug = f"highest-temperature-in-chengdu-on-{event_dt.strftime('%B').lower()}-{event_dt.day}-{event_dt.year}"
    previous_slug = "highest-temperature-in-chengdu-on-june-22-2026"
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    current_fact_as_of = now_utc.isoformat()
    nowcast_fact_as_of = (now_utc - timedelta(seconds=45)).isoformat()
    nowcast_reference = (now_utc - timedelta(seconds=90)).isoformat()
    nowcast_checked = (now_utc - timedelta(seconds=40)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    server._insert_event_fact_history(
        conn,
        run_date="2026-06-22",
        slug=previous_slug,
        event_date="2026-06-22",
        fact_as_of=nowcast_fact_as_of,
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": previous_slug,
            "event_date": "2026-06-22",
            "run_date": "2026-06-22",
            "station_id": "ZUUU",
            "weather_likelihood_features": {
                "precip_nowcast_1_3h_available": False,
                "precip_nowcast_1_3h_checked_at": nowcast_checked,
                "precip_nowcast_1_3h_reference_time": nowcast_reference,
                "precip_nowcast_1_3h_source": "open_meteo_forecast_hourly",
                "precip_nowcast_1_3h_role": "future_precipitation_risk_not_observed_precip",
                "precip_nowcast_1_3h_na_reason": "open_meteo_future_window_empty",
                "precip_nowcast_1_3h_window_hours": 3,
                "precip_nowcast_1_3h_window_rows": 0,
            },
            "distribution": [],
        },
    )
    current_fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of=current_fact_as_of,
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "station_id": "ZUUU",
            "weather_likelihood_features": {
                "zuuu_metar_raw_awc_latest_at": current_fact_as_of,
                "zuuu_metar_raw_awc_latest_temp_c": 25.0,
            },
            "distribution": [],
        },
    )
    server._upsert_event_fact_latest(conn, slug, event_date, current_fact_id)
    conn.commit()
    conn.close()

    monkeypatch.setattr(server, "DB_PATH", db_path)
    with server._DATA_SOURCE_HEALTH_LOCK:
        server._DATA_SOURCE_HEALTH_CACHE.clear()

    payload = server.data_source_health()
    items = {
        item["key"]: item
        for group in payload.get("groups") or []
        for item in group.get("items") or []
        if isinstance(item, dict)
    }
    nowcast_item = items["precip_nowcast_1_3h"]

    assert nowcast_item["status"] == "ok"
    assert nowcast_item["last_update"] == nowcast_reference
    assert nowcast_item["checked_at"] == nowcast_checked
    assert nowcast_item["source"] == "open_meteo_forecast_hourly"
    assert nowcast_item["na_reason"] == ""
    assert "open_meteo_future_window_empty" in nowcast_item["detail"]


def test_data_source_health_rebuilds_stale_precip_awc_from_latest_metar(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    event_date = datetime.now(server.ZoneInfo("Asia/Shanghai")).date().isoformat()
    event_dt = datetime.fromisoformat(event_date)
    slug = f"highest-temperature-in-chengdu-on-{event_dt.strftime('%B').lower()}-{event_dt.day}-{event_dt.year}"
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    fresh_awc_at = (now_utc - timedelta(seconds=60)).isoformat()
    stale_precip_at = (now_utc - timedelta(hours=12)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of=now_utc.isoformat(),
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "station_id": "ZUUU",
            "weather_likelihood_refreshed_at": fresh_awc_at,
            "weather_likelihood_features": {
                "source": "zuuu_metar_raw_awc",
                "weather_likelihood_refreshed_at": fresh_awc_at,
                "zuuu_metar_raw_awc_available": True,
                "zuuu_metar_raw_awc_latest_at": fresh_awc_at,
                "zuuu_metar_raw_awc_latest_temp_c": 25.0,
            },
            "precipitation_evidence_coverage": {
                "updated_at": stale_precip_at,
                "source_covered_count": 1,
                "strong_source_count": 0,
                "source_total": 3,
                "state": "covered_no_strong_precip",
                "sources": [
                    {
                        "key": "awc_metar",
                        "label": "AWC/METAR",
                        "source_available": True,
                        "strong_available": False,
                        "updated_at": stale_precip_at,
                        "state": "covered",
                    }
                ],
            },
            "distribution": [],
        },
    )
    server._upsert_event_fact_latest(conn, slug, event_date, fact_id)
    conn.commit()
    conn.close()

    monkeypatch.setattr(server, "DB_PATH", db_path)
    with server._DATA_SOURCE_HEALTH_LOCK:
        server._DATA_SOURCE_HEALTH_CACHE.clear()

    payload = server.data_source_health()
    items = {
        item["key"]: item
        for group in payload.get("groups") or []
        for item in group.get("items") or []
        if isinstance(item, dict)
    }

    assert items["zuuu_metar_raw_awc"]["status"] == "ok"
    assert items["precip_awc_metar"]["label"] == "AWC/METAR 降水证据"
    assert items["precip_awc_metar"]["status"] == "ok"
    assert items["precip_awc_metar"]["last_update"] == fresh_awc_at
    assert items["precipitation_evidence"]["status"] == "ok"


def test_source_health_display_source_collapses_cached_event_run_suffix() -> None:
    item = server._source_health_item(
        "wu_forecast_peak",
        "WU forecast high",
        last_update=datetime.now(timezone.utc).isoformat(),
        ttl_sec=3600,
        source="wunderground_forecast_daily_peak_cached_previous_event_run_cached_previous_event_run",
    )

    assert item["source"] == "wunderground_forecast_daily_peak_cached_previous_event_run"


def test_source_health_weather_events_cache_state_reads_latest_cache(tmp_path, monkeypatch) -> None:
    old_cache = tmp_path / "weather_events_cache.json"
    new_cache = tmp_path / "weather_events_cache.good.json"
    old_cache.write_text(
        json.dumps({"updated_at": "2026-06-22T10:00:00+00:00", "events": [{"id": "old"}]}),
        encoding="utf-8",
    )
    new_cache.write_text(
        json.dumps(
            {
                "updated_at": "2026-06-22T10:05:00+00:00",
                "events": [{"id": "new-1"}, {"id": "new-2"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_WEATHER_EVENTS_CACHE_FILES", (old_cache, new_cache), raising=False)

    count, updated_at, checked_at, source = server._source_health_weather_events_cache_state()

    assert count == 2
    assert updated_at == "2026-06-22T10:05:00+00:00"
    assert checked_at
    assert source == "weather_events_cache.good.json"


def test_fact_write_applies_hard_source_posterior_outside_trade_window(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_ENABLED", "1")
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "1")
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_MODE", "fixed")
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_START_LOCAL_HOUR", "14")
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_END_LOCAL_HOUR", "17")
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_LOCAL_DATE_OVERRIDE", "2026-06-18")
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_LOCAL_HOUR_OVERRIDE", "10")
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-18-2026",
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 31.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_target_event_date": "2026-06-18",
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T02:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T02:08:00+00:00",
        },
        "distribution": [
            {"outcome": "30°C", "q_bayes_posterior": 0.7, "q_temperature_bucket_posterior": 0.7},
            {"outcome": "31°C", "q_bayes_posterior": 0.3, "q_temperature_bucket_posterior": 0.3},
        ],
    }

    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-18",
        slug=payload["slug"],
        event_date="2026-06-18",
        fact_as_of="2026-06-18T02:08:00+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload,
    )
    row = conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    conn.close()

    written = json.loads(row["fact_json"])
    by_outcome = {item["outcome"]: item for item in written["distribution"]}
    assert written["hard_source_posterior_override_applied"] is True
    assert written["hard_source_posterior_override"]["window_ignored"] is True
    assert written["hard_source_posterior_override"]["window"]["allowed"] is False
    assert by_outcome["30°C"]["q_bayes_posterior"] == 0.0
    assert by_outcome["30°C"]["q_posterior_observed_constraints_hard_zero"] is True
    assert by_outcome["31°C"]["q_bayes_posterior"] == 1.0
    assert written["model_top_label"] == "31°C"


def test_fact_write_normalizes_posterior_fields_to_temperature_ssot(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-30-2026",
        "event_date": "2026-06-30",
        "run_date": "2026-06-28",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_features": {},
        "distribution": [
            {
                "outcome": "30°C or higher",
                "q_temperature_bucket_posterior": 0.32225584763361015,
                "q_bayes_posterior": 0.6357425682611088,
                "q_contract_payout_posterior": 0.6357425682611088,
                "q_entry": 0.6357425682611088,
                "q_raw": 0.40,
            },
            {
                "outcome": "28°C",
                "q_temperature_bucket_posterior": 0.24013399278030345,
                "q_bayes_posterior": 0.11839842487500746,
                "q_contract_payout_posterior": 0.11839842487500746,
                "q_entry": 0.11839842487500746,
                "q_raw": 0.20,
            },
            {
                "outcome": "29°C",
                "q_temperature_bucket_posterior": 0.13885439845350797,
                "q_bayes_posterior": 0.06333813203030896,
                "q_contract_payout_posterior": 0.06333813203030896,
                "q_entry": 0.06333813203030896,
                "q_raw": 0.10,
            },
        ],
    }

    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-28",
        slug=payload["slug"],
        event_date="2026-06-30",
        fact_as_of="2026-06-28T14:48:35+00:00",
        model_snapshot_id=2122,
        market_snapshot_id=12285,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload,
    )
    row = conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    conn.close()

    written = json.loads(row["fact_json"])
    by_outcome = {item["outcome"]: item for item in written["distribution"]}
    top = by_outcome["30°C or higher"]
    assert top["q_bayes_posterior"] == pytest.approx(top["q_temperature_bucket_posterior"])
    assert top["q_contract_payout_posterior"] == pytest.approx(top["q_temperature_bucket_posterior"])
    assert top["q_entry"] == pytest.approx(top["q_temperature_bucket_posterior"])
    assert top["posterior_ssot_write_aligned"] is True
    assert top["q_raw"] == 0.40
    assert all(
        row["q_bayes_posterior"] == pytest.approx(row["q_temperature_bucket_posterior"])
        for row in by_outcome.values()
    )
    assert written["target_bucket_selection"]["target_bucket"] == "30°C or higher"
    assert written["target_realization_capital_plan"]["target_bucket"] == "30°C or higher"
    assert written["temperature_path_distribution"]["target_bucket"] == "30°C or higher"
    assert written["fact_provenance"]["posterior_ssot_write_alignment"]["applied"] is True


def test_fact_trade_derivative_refresh_rebuilds_plan_legs_after_canonical_target_alignment() -> None:
    item = {
        "slug": "highest-temperature-in-chengdu-on-june-30-2026",
        "event_date": "2026-06-30",
        "run_date": "2026-06-28",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "target_bucket_selection": {
            "target_bucket": "28°C",
            "target_probability_pct": 40.0,
            "second_target_bucket": "30°C or higher",
            "second_target_probability_pct": 60.0,
        },
        "target_bucket_outlook": {
            "available": True,
            "target_bucket": "28°C",
            "final_bucket": "28°C",
            "target_final_probability_pct": 40.0,
        },
        "temperature_path_distribution": {
            "available": True,
            "target_bucket": "28°C",
            "target": {
                "bucket": "28°C",
                "path_probability": 0.8,
                "path_probability_pct": 80.0,
                "path_probability_present": True,
            },
            "second_target_bucket": "30°C or higher",
            "second_target": {
                "bucket": "30°C or higher",
                "path_probability": 0.5,
                "path_probability_pct": 50.0,
                "path_probability_present": True,
            },
            "trade_follow": {"allowed": True, "reason": "test_allowed"},
        },
        "distribution": [
            {
                "outcome": "30°C or higher",
                "q_temperature_bucket_posterior": 0.60,
                "q_bayes_posterior": 0.60,
                "best_bid": 0.29,
                "best_ask": 0.31,
            },
            {
                "outcome": "28°C",
                "q_temperature_bucket_posterior": 0.40,
                "q_bayes_posterior": 0.40,
                "best_bid": 0.17,
                "best_ask": 0.19,
            },
        ],
    }

    server._refresh_fact_trade_derivatives_after_target_update(item, item["distribution"])

    plan = item["target_realization_capital_plan"]
    assert item["target_bucket"] == "30°C or higher"
    assert item["ssot_target_bucket"] == "30°C or higher"
    assert item["target_bucket_selection"]["target_bucket"] == "30°C or higher"
    assert item["target_bucket_outlook"]["target_bucket"] == "30°C or higher"
    assert item["temperature_path_distribution"]["target_bucket"] == "30°C or higher"
    assert plan["target_bucket"] == "30°C or higher"
    assert plan["target_posterior"] == pytest.approx(0.60)
    assert plan["buy_new_leg"]["bucket"] == "30°C or higher"
    assert plan["buy_new_leg"]["side"] == "YES"
    target_yes = next(leg for leg in plan["legs"] if leg["role"] == "target_yes")
    assert target_yes["bucket"] == "30°C or higher"


def test_temperature_momentum_candidate_uses_explicit_observation_led_switch() -> None:
    label, candidate, source = server._temperature_momentum_candidate_from_summary(
        {
            "ok": True,
            "target_bucket_selection": {
                "target_bucket": "31°C",
                "observation_led_bayes": {
                    "available": True,
                    "target_bucket": "29°C",
                    "switch_from_bucket": "31°C",
                    "switch_direction": "down",
                    "switch_support_state": "cooling",
                    "target_probability_pct": 42.0,
                    "temperature_momentum_effective_rate_c_per_hour": -0.6,
                    "peak_estimator_mu_c": 29.2,
                    "peak_estimator_sigma_c": 0.8,
                },
            },
        }
    )

    assert label == "29°C"
    assert source == "observation_led_bayes"
    assert candidate["early_switch_context"]["candidate_bucket"] == "29°C"
    assert candidate["early_switch_context"]["trajectory_actual_rate_c_per_hour"] == pytest.approx(-0.6)


def test_temperature_momentum_likelihood_materializes_observation_led_switch_into_main_posterior(monkeypatch) -> None:
    def fake_summary(*_args, **_kwargs):
        return {
            "ok": True,
            "target_bucket_selection": {
                "target_bucket": "31°C",
                "observation_led_bayes": {
                    "available": True,
                    "target_bucket": "29°C",
                    "switch_from_bucket": "31°C",
                    "switch_direction": "down",
                    "switch_support_state": "cooling",
                    "target_probability_pct": 42.0,
                    "temperature_momentum_effective_rate_c_per_hour": -0.6,
                    "peak_estimator_mu_c": 29.2,
                    "peak_estimator_sigma_c": 0.8,
                },
            },
        }

    monkeypatch.setattr(server, "build_chengdu_intraday_summary_from_payload", fake_summary)
    item = {
        "slug": "highest-temperature-in-chengdu-on-june-29-2026",
        "event_date": "2026-06-29",
        "run_date": "2026-06-29",
        "station_id": "ZUUU",
        "distribution": [
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.50, "q_bayes_posterior": 0.50, "q_entry": 0.50},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.20, "q_bayes_posterior": 0.20, "q_entry": 0.20},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.30, "q_bayes_posterior": 0.30, "q_entry": 0.30},
        ],
    }

    changed = server._materialize_temperature_momentum_likelihood(
        item,
        item["distribution"],
        slug=item["slug"],
        event_date=item["event_date"],
        fact_as_of="2026-06-29T02:00:00+00:00",
        snapshot_id=123,
    )

    by_outcome = {row["outcome"]: row for row in item["distribution"]}
    assert changed is True
    assert item["temperature_momentum_likelihood_source"] == "observation_led_bayes"
    assert item["temperature_momentum_likelihood_target_bucket"] == "29°C"
    assert by_outcome["29°C"]["q_temperature_bucket_posterior"] > by_outcome["31°C"]["q_temperature_bucket_posterior"]
    assert by_outcome["29°C"]["q_bayes_posterior"] == pytest.approx(
        by_outcome["29°C"]["q_temperature_bucket_posterior"]
    )
    assert by_outcome["29°C"]["q_entry"] == pytest.approx(
        by_outcome["29°C"]["q_temperature_bucket_posterior"]
    )
    assert item["fact_provenance"]["temperature_momentum_likelihood"]["candidate_bucket"] == "29°C"


def test_fact_write_keeps_morning_not_final_observation_out_of_posterior(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_OBSERVATION_PATH_LIKELIHOOD_EVIDENCE", "80")

    def fake_summary(*_args, **_kwargs):
        return {
            "ok": True,
            "target_bucket": "25°C or below",
            "target_bucket_probability_pct": 36.35,
            "target_bucket_selection": {
                "target_bucket": "25°C or below",
                "observation_led_bayes": {
                    "available": True,
                    "target_bucket": "25°C or below",
                    "reference_bucket": "31°C",
                    "observed_temp_c": 25.0,
                    "observed_today_max_c": 25.0,
                    "peak_estimator_mu_c": 25.6,
                    "peak_estimator_sigma_c": 0.8,
                    "intraday_window": {"peak_hour_local": 14.0},
                },
            },
            "observation_led_likelihood": {
                "applied": True,
                "target_bucket_before": "31°C",
                "target_bucket_after": "25°C or below",
            },
            "temperature_path_distribution": {
                "available": True,
                "peak_estimator_mu_c": 25.6,
                "peak_estimator_sigma_c": 0.8,
                "observed_today_max_c": 25.0,
                "physical_weather_path_probability_scale": 1.0,
                "reliability": {"state": "high", "score": 0.9},
            },
            "bucket_probabilities": [
                {"outcome": "25°C or below", "q_temperature_bucket_posterior": 0.3635, "q_bayes_posterior": 0.3635},
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.12, "q_bayes_posterior": 0.12},
                {"outcome": "32°C", "q_temperature_bucket_posterior": 0.08, "q_bayes_posterior": 0.08},
            ],
        }

    monkeypatch.setattr(server, "build_chengdu_intraday_summary_from_payload", fake_summary)
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-july-1-2026"
    payload = {
        "slug": slug,
        "event_date": "2026-07-01",
        "run_date": "2026-07-01",
        "title": "Highest temperature in Chengdu on July 1?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "event_tz": "Asia/Shanghai",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_features_compact": {
            "observed_temp_c": 25.0,
            "observed_today_max_c": 25.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_latest_at": "2026-06-30T23:42:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 25.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-30T23:42:00+00:00",
        },
        "distribution": [
            {
                "outcome": "25°C or below",
                "q_temperature_bucket_posterior": 0.05,
                "q_bayes_posterior": 0.05,
                "q_entry": 0.05,
                "q_raw": 0.05,
                "q_temperature_bucket_score_components": {"model": 1.0, "history": 1.0},
            },
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.55,
                "q_bayes_posterior": 0.55,
                "q_entry": 0.55,
                "q_raw": 0.55,
                "q_temperature_bucket_score_components": {"model": 11.0, "history": 1.0},
            },
            {
                "outcome": "32°C",
                "q_temperature_bucket_posterior": 0.40,
                "q_bayes_posterior": 0.40,
                "q_entry": 0.40,
                "q_raw": 0.40,
                "q_temperature_bucket_score_components": {"model": 8.0, "history": 1.0},
            },
        ],
    }

    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-07-01",
        slug=slug,
        event_date="2026-07-01",
        fact_as_of="2026-06-30T23:42:00+00:00",
        model_snapshot_id=2127,
        market_snapshot_id=12284,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload,
    )
    row = conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    conn.close()
    item = json.loads(row["fact_json"])
    by_outcome = {entry["outcome"]: entry for entry in item["distribution"]}

    assert item["d0_observed_feasibility_guard_applied"] is False
    assert item["fact_provenance"]["d0_observed_feasibility_guard"]["skipped_reason"] == (
        "intraday_observed_max_not_final"
    )
    assert item["observation_path_likelihood_posterior_materialized"] is False
    assert item["observation_led_likelihood_posterior_materialized"] is False
    assert by_outcome["25°C or below"]["q_temperature_bucket_posterior"] == pytest.approx(0.05)
    assert by_outcome["31°C"]["q_temperature_bucket_posterior"] == pytest.approx(0.55)
    assert item["target_bucket"] == "31°C"
    assert item["auto_trade_ssot"]["main_candidate"]["outcome"] == "31°C"


def test_fact_write_materializes_observation_path_likelihood_into_target_and_plan(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_BAYES_OBSERVATION_PATH_LIKELIHOOD_EVIDENCE", "80")

    def fake_summary(*_args, **_kwargs):
        return {
            "ok": True,
            "target_bucket_selection": {
                "target_bucket": "31°C",
                "observation_led_bayes": {
                    "available": True,
                    "target_bucket": "29°C",
                    "reference_bucket": "31°C",
                    "switch_from_bucket": "31°C",
                    "switch_direction": "hold",
                    "switch_support_state": "hold",
                    "observed_temp_c": 28.0,
                    "observed_today_max_c": 28.0,
                    "observed_today_max_source": "zuuu_metar_raw_awc",
                    "peak_estimator_mu_c": 29.1,
                    "peak_estimator_sigma_c": 0.45,
                    "temperature_momentum_effective_rate_c_per_hour": -0.2,
                    "temperature_momentum_gate": {
                        "state": "cooling",
                        "supports_warming": False,
                        "posterior_gate_allowed": False,
                    },
                    "physical_weather_state": "cooling",
                    "physical_weather_peak_mu_adjustment_c": -0.8,
                    "intraday_window": {"peak_hour_local": 14.0},
                },
            },
            "temperature_path_distribution": {
                "available": True,
                "peak_estimator_mu_c": 29.1,
                "peak_estimator_sigma_c": 0.45,
                "observed_today_max_c": 28.0,
                "physical_weather_state": "cooling",
                "physical_weather_path_probability_scale": 1.0,
                "physical_weather_peak_mu_adjustment_c": -0.8,
                "reliability": {"state": "high", "score": 0.9},
            },
        }

    monkeypatch.setattr(server, "build_chengdu_intraday_summary_from_payload", fake_summary)
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-29-2026"
    payload = {
        "slug": slug,
        "event_date": "2026-06-29",
        "run_date": "2026-06-29",
        "title": "Highest temperature in Chengdu on June 29?",
        "city": "Chengdu",
        "station_id": "ZUUU",
        "event_tz": "Asia/Shanghai",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_features_compact": {
            "observed_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_latest_at": "2026-06-29T06:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 28.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-29T04:00:00+00:00",
        },
        "distribution": [
            {
                "outcome": "28°C",
                "q_temperature_bucket_posterior": 0.10,
                "q_bayes_posterior": 0.10,
                "q_entry": 0.10,
                "q_temperature_bucket_score_components": {"model": 4.0, "history": 2.0},
                "best_bid": 0.20,
                "best_ask": 0.22,
            },
            {
                "outcome": "29°C",
                "q_temperature_bucket_posterior": 0.20,
                "q_bayes_posterior": 0.20,
                "q_entry": 0.20,
                "q_temperature_bucket_score_components": {"model": 5.0, "history": 2.0},
                "best_bid": 0.30,
                "best_ask": 0.33,
            },
            {
                "outcome": "30°C",
                "q_temperature_bucket_posterior": 0.25,
                "q_bayes_posterior": 0.25,
                "q_entry": 0.25,
                "q_temperature_bucket_score_components": {"model": 8.0, "history": 2.0},
                "best_bid": 0.20,
                "best_ask": 0.24,
            },
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.45,
                "q_bayes_posterior": 0.45,
                "q_entry": 0.45,
                "q_temperature_bucket_score_components": {"model": 18.0, "history": 2.0},
                "best_bid": 0.12,
                "best_ask": 0.16,
            },
        ],
    }

    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-29",
        slug=slug,
        event_date="2026-06-29",
        fact_as_of="2026-06-29T06:30:00+00:00",
        model_snapshot_id=2127,
        market_snapshot_id=12284,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload,
    )
    row = conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    conn.close()
    item = json.loads(row["fact_json"])
    by_outcome = {entry["outcome"]: entry for entry in item["distribution"]}

    assert item["observation_path_likelihood_applied"] is True
    assert by_outcome["29°C"]["observation_path_likelihood_applied"] is True
    assert by_outcome["29°C"]["q_temperature_bucket_posterior"] > by_outcome["31°C"]["q_temperature_bucket_posterior"]
    assert by_outcome["29°C"]["q_bayes_posterior"] == pytest.approx(
        by_outcome["29°C"]["q_temperature_bucket_posterior"]
    )
    assert by_outcome["29°C"]["q_entry"] == pytest.approx(by_outcome["29°C"]["q_temperature_bucket_posterior"])
    assert item["target_bucket"] == "29°C"
    assert item["ssot_target_bucket"] == "29°C"
    assert item["target_bucket_selection"]["target_bucket"] == "29°C"
    assert item["target_realization_capital_plan"]["target_bucket"] == "29°C"
    assert item["target_realization_capital_plan"]["buy_new_leg"]["bucket"] == "29°C"
    impact = item["target_switch_context"]["trajectory_momentum_impact"]
    assert impact["source"] == "target_switch_context.ssot"
    assert impact["target_bucket"] == "29°C"
    assert impact["summary"]
    assert "路径" in impact["summary"]
    assert item["auto_trade_ssot"]["main_candidate"]["outcome"] == "29°C"
    assert item["fact_provenance"]["observation_path_likelihood"]["posterior_top_after"] == "29°C"


def test_refresh_awc_fact_updates_latest_fact_without_model_scan(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "0")
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    old_payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_refreshed_at": "2026-06-18T12:04:00+00:00",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 30.0,
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T12:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 30.0,
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T12:04:00+00:00",
            "zuuu_metar_raw_awc_date_mismatch": True,
            "zuuu_metar_raw_awc_date_mismatch_reason": "latest_raw_metar_local_date_not_target_event_date",
        },
        "distribution": [
            {"outcome": "30°C", "q_bayes_posterior": 0.62, "q_temperature_bucket_posterior": 0.62},
            {"outcome": "31°C", "q_bayes_posterior": 0.28, "q_temperature_bucket_posterior": 0.28},
        ],
        "chengdu_intraday_summary": {"event_date": "2026-06-18", "stale": True},
        "target_bucket_outlook": {"stale": True},
        "temperature_realization_reliability": {"stale": True},
        "temperature_trajectory_status": {
            "observed_today_max_c": 30.0,
            "weather_likelihood_refreshed_at": "2026-06-18T12:04:00+00:00",
            "stale": True,
        },
        "target_switch_context": {"observed_today_max_c": 30.0, "stale": True},
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date="2026-06-18",
        fact_as_of="2026-06-18T12:04:00+00:00",
        model_snapshot_id=11,
        market_snapshot_id=22,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=old_payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-18", fact_id)
    conn.commit()
    conn.close()

    def fake_awc(*_args, **_kwargs):
        return {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 31.0,
            "observed_temp": 31.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "metar_temperature_c": 31.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 31.0,
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T13:00:02+00:00",
            "zuuu_metar_raw_awc_fetch_duration_ms": 180,
            "zuuu_metar_raw_awc_date_mismatch": False,
            "zuuu_metar_raw_awc_date_mismatch_reason": "",
            "weather_source_alert_status": "aligned",
            "weather_source_alert_level": "ok",
            "weather_source_alert_codes": "",
            "weather_source_delay_deformation_alert": {"alert": False, "status": "aligned"},
        }

    monkeypatch.setattr(scan_weather_market, "fetch_zuuu_metar_raw_awc_features", fake_awc)

    result = server.refresh_awc_fact_for_chengdu_event(db_path, event_date="2026-06-18")

    assert result["ok"] is True
    assert result["status"] == "updated"
    assert result["reason"] == "awc_latest_at_changed"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT h.fact_as_of, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, "2026-06-18"),
    ).fetchone()
    conn.close()

    payload = json.loads(row["fact_json"])
    features = payload["weather_likelihood_features"]
    assert row["fact_as_of"] == "2026-06-18T13:00:02+00:00"
    assert payload["weather_likelihood_refreshed_at"] == "2026-06-18T13:00:02+00:00"
    assert features["observed_today_max_c"] == 31.0
    assert features["zuuu_metar_raw_awc_today_max_at"] == "2026-06-18T13:00:00+00:00"
    assert features["zuuu_metar_raw_awc_date_mismatch"] is False
    assert features["zuuu_metar_raw_awc_date_mismatch_reason"] == ""
    distribution_by_outcome = {
        row["outcome"]: row
        for row in payload["distribution"]
        if isinstance(row, dict) and row.get("outcome")
    }
    assert distribution_by_outcome["30°C"]["observed_today_max_c"] == 31.0
    assert distribution_by_outcome["31°C"]["zuuu_metar_raw_awc_latest_at"] == "2026-06-18T13:00:00+00:00"
    assert distribution_by_outcome["30°C"]["q_bayes_posterior"] == 0.0
    assert distribution_by_outcome["31°C"]["q_bayes_posterior"] == pytest.approx(
        max(row["q_bayes_posterior"] for row in payload["distribution"])
    )
    assert distribution_by_outcome["31°C"]["hard_source_posterior_override_applied"] is True
    assert payload["model_top_label"] == "31°C"
    assert not payload.get("chengdu_intraday_summary", {}).get("stale")
    assert not payload.get("target_bucket_outlook", {}).get("stale")
    assert not payload.get("temperature_trajectory_status", {}).get("stale")
    assert not payload.get("target_switch_context", {}).get("stale")
    assert payload["fact_provenance"]["awc_hard_observation_refresh"]["today_max_c"] == 31.0


def test_refresh_awc_fact_fast_trade_write_compacts_heavy_ui_payload(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AWC_FACT_REFRESH_FAST_TRADE_WRITE", "1")
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    heavy_marker = "HEAVY_FAST_TRADE_MARKER"
    heavy_blob = {"marker": heavy_marker, "blob": "x" * 2000}
    heavy_row = {
        "q_ensemble_qm_meta": dict(heavy_blob),
        "q_bayes_physical_d0_explanation": dict(heavy_blob),
        "peak_estimator": dict(heavy_blob),
        "q_bayes_climo_regime_explanation": dict(heavy_blob),
        "q_bayes_physical_likelihood_component_groups": dict(heavy_blob),
        "bayes_chain": dict(heavy_blob),
        "metar_cross_check_summary": dict(heavy_blob),
        "metar_cross_check_sources": [dict(heavy_blob)],
        "per_model_q": dict(heavy_blob),
        "q_bayes_physical_likelihood_evidence_scale_details": dict(heavy_blob),
        "q_model_member_bucket_rule_meta": dict(heavy_blob),
        "q_bayes_physical_likelihood_components": [dict(heavy_blob)],
        "hard_source_posterior_override_window": dict(heavy_blob),
        "zuuu_metar_raw_awc_today_temperature_points": [dict(heavy_blob)],
    }
    old_payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_refreshed_at": "2026-06-18T12:04:00+00:00",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 30.0,
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T12:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 30.0,
            "zuuu_metar_raw_awc_today_max_c": 30.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T12:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T12:04:00+00:00",
        },
        "distribution": [
            {
                "outcome": "30°C",
                "q_bayes_posterior": 0.62,
                "q_temperature_bucket_posterior": 0.62,
                **heavy_row,
            },
            {
                "outcome": "31°C",
                "q_bayes_posterior": 0.28,
                "q_temperature_bucket_posterior": 0.28,
                **heavy_row,
            },
        ],
        "rows": [
            {"outcome": "30°C", "q_bayes_posterior": 0.62, **heavy_row},
            {"outcome": "31°C", "q_bayes_posterior": 0.28, **heavy_row},
        ],
        "chengdu_intraday_summary": dict(heavy_blob),
        "target_bucket_outlook": dict(heavy_blob),
        "temperature_path_distribution": dict(heavy_blob),
        "temperature_realization_reliability": dict(heavy_blob),
        "target_switch_context": dict(heavy_blob),
        "evidence_conclusions": dict(heavy_blob),
        "decision": dict(heavy_blob),
        "key_metrics": dict(heavy_blob),
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date="2026-06-18",
        fact_as_of="2026-06-18T12:04:00+00:00",
        model_snapshot_id=11,
        market_snapshot_id=22,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=old_payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-18", fact_id)
    conn.commit()
    conn.close()

    def fake_awc(*_args, **_kwargs):
        return {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 31.0,
            "observed_temp": 31.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "metar_temperature_c": 31.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 31.0,
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T13:00:02+00:00",
            "zuuu_metar_raw_awc_fetch_duration_ms": 180,
            "zuuu_metar_raw_awc_date_mismatch": False,
            "zuuu_metar_raw_awc_date_mismatch_reason": "",
        }

    monkeypatch.setattr(scan_weather_market, "fetch_zuuu_metar_raw_awc_features", fake_awc)

    result = server.refresh_awc_fact_for_chengdu_event(db_path, event_date="2026-06-18")

    assert result["ok"] is True
    assert result["status"] == "updated"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT h.id, h.fact_json, c.summary_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        LEFT JOIN event_fact_summary_cache c ON c.fact_id = h.id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, "2026-06-18"),
    ).fetchone()
    conn.close()

    assert row is not None
    assert heavy_marker not in row["fact_json"]
    assert heavy_marker not in row["summary_json"]
    payload = json.loads(row["fact_json"])
    summary = json.loads(row["summary_json"])
    assert payload["awc_hard_observation_fast_trade_write"] is True
    assert payload["posterior_target_bucket_selection"]["fast_trade_compacted"] is True
    assert payload["posterior_target_bucket_selection"]["target_bucket"] == "31°C"
    top_distribution_prob = max(
        row["q_temperature_bucket_posterior"]
        for row in payload["distribution"]
        if isinstance(row, dict) and row.get("q_temperature_bucket_posterior") is not None
    )
    assert payload["posterior_target_bucket_selection"]["target_probability"] == pytest.approx(
        top_distribution_prob
    )
    assert 0.0 < payload["posterior_target_bucket_selection"]["target_probability"] < 1.0
    assert payload["target_bucket_selection"] == payload["posterior_target_bucket_selection"]
    assert payload["target_bucket"] == "31°C"
    assert payload["ssot_target_bucket"] == "31°C"
    assert summary["fast_trade_summary_cache"] is True
    assert summary["summary_cache_source"] == "event_fact_summary_cache_fast_trade"
    assert summary["summary_cache_version"] == server.EVENT_FACT_SUMMARY_CACHE_VERSION
    assert summary["posterior_target_bucket_selection"]["target_bucket"] == "31°C"
    assert summary["posterior_target_bucket_selection"]["target_probability"] == pytest.approx(
        top_distribution_prob
    )
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
    for rows_key in ("distribution", "rows"):
        for distribution_row in payload[rows_key]:
            for key in heavy_row:
                assert key not in distribution_row
    assert payload["fact_provenance"]["awc_fast_trade_fact_write"]["purpose"] == (
        "trade_path_first_ui_rich_fields_deferred"
    )


def test_fast_trade_target_selection_prefers_fact_rows_over_stale_ctp() -> None:
    payload = {
        "posterior_temperature_distribution": {
            "bucket_probabilities": {
                "31°C": 0.469255504884996,
                "32°C": 0.4335266,
            },
        },
        "distribution": [
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.0761791},
            {"outcome": "25°C or below", "q_temperature_bucket_posterior": 0.4776612011202925},
            {"outcome": "27°C", "q_temperature_bucket_posterior": 0.0857838},
        ],
    }

    server._compact_target_selection_for_fast_trade_fact(payload)

    selection = payload["posterior_target_bucket_selection"]
    assert selection["target_bucket"] == "25°C or below"
    assert selection["target_probability"] == pytest.approx(0.4776612011202925)
    assert selection["target_probability_field"] == "event_fact_latest.distribution.q_temperature_bucket_posterior"
    assert payload["target_bucket_selection"] == selection
    assert payload["target_bucket"] == "25°C or below"
    assert payload["ssot_target_bucket"] == "25°C or below"
    assert payload["target_bucket_probability_field"] == (
        "event_fact_latest.distribution.q_temperature_bucket_posterior"
    )


def test_awc_refresh_clears_stale_observation_target_markers_before_recompute() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-1-2026",
        "event_date": "2026-07-01",
        "run_date": "2026-07-01",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "target_bucket_selection": {
            "target_bucket": "33°C",
            "target_probability": 0.5319810931180973,
            "target_probability_field": "event_fact_latest.distribution.q_temperature_bucket_posterior",
        },
        "posterior_target_bucket_selection": {
            "target_bucket": "33°C",
            "target_probability": 0.5319810931180973,
            "target_probability_field": "event_fact_latest.distribution.q_temperature_bucket_posterior",
        },
        "target_bucket_probability_field": "event_fact_latest.distribution.q_temperature_bucket_posterior",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 30.0,
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-07-01T03:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 30.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-07-01T03:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-07-01T03:08:00+00:00",
        },
        "distribution": [
            {
                "outcome": "31°C",
                "q_raw": 0.050319312724171415,
                "q_temperature_bucket_posterior_pre_observed_constraints": 0.050319312724171415,
                "q_temperature_bucket_posterior": 0.024739575373228403,
                "q_bayes_posterior": 0.024739575373228403,
                "q_temperature_bucket_posterior_pre_observation_led_likelihood": 0.12786100876988987,
                "q_bayes_temperature_trajectory_mu_c": 34.86711537423766,
                "temperature_momentum_likelihood_applied": True,
                "observation_led_likelihood_applied": True,
                "q_temperature_bucket_score_components": {
                    "model": 0.47,
                    "temperature_trajectory": 0.18,
                    "independent_physics_residual": 0.22,
                },
            },
            {
                "outcome": "33°C",
                "q_raw": 0.11631060268355033,
                "q_temperature_bucket_posterior_pre_observed_constraints": 0.11631060268355033,
                "q_temperature_bucket_posterior": 0.5319810931180973,
                "q_bayes_posterior": 0.5319810931180973,
                "q_temperature_bucket_posterior_pre_observation_led_likelihood": 0.3597780835038157,
                "q_bayes_temperature_trajectory_mu_c": 34.86711537423766,
                "q_bayes_physical_likelihood_prob": 0.31101871488031196,
                "temperature_momentum_likelihood_applied": True,
                "observation_led_likelihood_applied": True,
                "posterior_product_repaired": True,
                "posterior_product_repair_source": "observation_led_prior_times_likelihood",
                "q_temperature_bucket_score_components": {
                    "model": 0.04,
                    "temperature_trajectory": 4.65,
                    "independent_physics_residual": 0.02,
                },
            },
        ],
    }
    awc_features = {
        "source": "zuuu_metar_raw_awc",
        "observed_temp_c": 31.0,
        "observed_temp": 31.0,
        "observed_temp_source": "zuuu_metar_raw_awc",
        "observed_today_max_c": 31.0,
        "observed_today_max_source": "zuuu_metar_raw_awc",
        "observed_today_max_trusted": True,
        "metar_temperature_c": 31.0,
        "zuuu_metar_raw_awc_available": True,
        "zuuu_metar_raw_awc_latest_at": "2026-07-01T07:00:00+00:00",
        "zuuu_metar_raw_awc_latest_temp_c": 31.0,
        "zuuu_metar_raw_awc_today_max_c": 31.0,
        "zuuu_metar_raw_awc_today_max_at": "2026-07-01T07:00:00+00:00",
        "zuuu_metar_raw_awc_fetched_at": "2026-07-01T07:18:48+00:00",
        "peak_estimator_enabled": True,
        "peak_estimator_mu_c": 31.34,
        "peak_estimator_sigma_c": 0.60,
        "peak_estimator_source": "energy_balance_nowcast",
    }

    merged = server._merge_awc_features_into_fact_payload(
        payload,
        awc_features,
        refreshed_at="2026-07-01T07:18:48+00:00",
    )

    assert server._preserve_event_fact_distribution_posterior(merged) is False
    by_outcome = {row["outcome"]: row for row in merged["distribution"]}
    assert by_outcome["33°C"]["q_temperature_bucket_posterior"] == pytest.approx(0.11631060268355033)
    assert by_outcome["33°C"]["q_bayes_posterior"] == pytest.approx(0.11631060268355033)
    assert "target_bucket_selection" not in merged
    assert "posterior_target_bucket_selection" not in merged
    assert "q_bayes_temperature_trajectory_mu_c" not in by_outcome["33°C"]
    assert "q_bayes_physical_likelihood_prob" not in by_outcome["33°C"]
    assert "observation_led_likelihood_applied" not in by_outcome["33°C"]
    assert "temperature_momentum_likelihood_applied" not in by_outcome["33°C"]
    assert "posterior_product_repaired" not in by_outcome["33°C"]
    assert "temperature_trajectory" not in by_outcome["33°C"]["q_temperature_bucket_score_components"]
    assert "independent_physics_residual" not in by_outcome["33°C"]["q_temperature_bucket_score_components"]

    changed = server._merge_chengdu_intraday_summary_distribution_into_fact_item(
        merged,
        {
            "ok": True,
            "observation_led_likelihood": {
                "applied": True,
                "target_bucket_before": "33°C",
                "target_bucket_after": "31°C",
            },
            "bucket_probabilities": [
                {"outcome": "31°C", "q_temperature_bucket_posterior": 0.5698, "q_bayes_posterior": 0.5698},
                {"outcome": "33°C", "q_temperature_bucket_posterior": 0.2514, "q_bayes_posterior": 0.2514},
            ],
        },
        event_date="2026-07-01",
        fact_as_of="2026-07-01T07:18:48+00:00",
        event_tz="Asia/Shanghai",
    )

    assert changed is True
    assert by_outcome["31°C"]["q_temperature_bucket_posterior"] == pytest.approx(0.5698)
    assert by_outcome["33°C"]["q_temperature_bucket_posterior"] == pytest.approx(0.2514)


def test_fast_trade_compaction_preserves_compact_physical_likelihood_summary() -> None:
    payload = {
        "distribution": [
            {
                "outcome": "31°C",
                "q_bayes_physical_likelihood_component_groups": {
                    "precipitation_evaporative_cooling": {
                        "available": True,
                        "weight": 1.35,
                        "log_likelihood": 0.0,
                        "source_profile": {
                            "evidence_state": "strong_no_precip",
                            "source_class": "metar",
                            "source_field": "awc_metar_no_precip_observed",
                            "no_precip_confirmed": True,
                            "is_real_radar_or_metar": True,
                            "rate_mm_per_h": 0.0,
                            "debug_blob": "x" * 500,
                        },
                        "components": {
                            "precip_evaporative_cooling": {
                                "factor": 1.0,
                                "weight": 1.35,
                                "log_likelihood": 0.0,
                                "debug_blob": "x" * 500,
                            }
                        },
                    }
                },
                "q_bayes_physical_likelihood_components": {
                    "precip_evaporative_cooling": 0.0,
                    "debug_blob": "not_numeric",
                },
                "q_bayes_physical_likelihood_evidence_scale_details": {"debug": "x" * 500},
            }
        ]
    }

    server._compact_rows_for_fast_trade_fact(payload)

    row = payload["distribution"][0]
    groups = row["q_bayes_physical_likelihood_component_groups"]
    precip = groups["precipitation_evaporative_cooling"]
    assert precip["available"] is True
    assert precip["weight"] == pytest.approx(1.35)
    assert precip["source_profile"]["source_class"] == "metar"
    assert precip["source_profile"]["no_precip_confirmed"] is True
    assert "debug_blob" not in precip["source_profile"]
    assert "debug_blob" not in precip["components"]["precip_evaporative_cooling"]
    assert row["q_bayes_physical_likelihood_components"] == {"precip_evaporative_cooling": 0.0}
    assert "q_bayes_physical_likelihood_evidence_scale_details" not in row


def test_fact_write_physical_audit_fields_materialize_from_awc_features() -> None:
    rows = [
        {
            "outcome": "29°C",
            "q_bayes_posterior": 0.2,
            "q_bayes_physical_likelihood_evidence": 0.35,
        },
        {
            "outcome": "30°C or higher",
            "q_bayes_posterior": 0.8,
            "q_bayes_physical_likelihood_evidence": 0.35,
        },
    ]
    features = {
        "source": "zuuu_metar_raw_awc",
        "auxiliary_source": "zuuu_metar_raw_awc",
        "observed_temp_c": 31.0,
        "observed_today_max_c": 32.0,
        "observed_today_max_source": "zuuu_metar_raw_awc",
        "observed_today_max_trusted": True,
        "forecast_peak_temp_c": 31.0,
        "hours_to_peak_window": 1.0,
        "temperature_momentum_1h_c_per_hour": -1.0,
        "temperature_momentum_effective_c_per_hour": -1.0,
        "temperature_momentum_realization_state": "cooling",
        "awc_metar_no_precip_observed": True,
        "metar_cavok_observed": True,
        "dewpoint_c": 22.0,
        "peak_estimator_mu_c": 32.0,
        "peak_estimator_sigma_c": 0.55,
        "peak_estimator_source": "energy_balance_nowcast",
    }

    changed = server._attach_physical_likelihood_audit_fields_from_features(
        rows,
        features,
        fact_as_of="2026-06-30T12:00:00+00:00",
        source="test",
    )

    assert changed is True
    for row in rows:
        assert row["q_bayes_physical_likelihood_source"] == "weather_physical_likelihood"
        assert row["q_bayes_physical_likelihood_audit_source"] == "test"
        assert "q_bayes_physical_likelihood_component_groups" in row
        assert "q_bayes_physical_likelihood_components" in row
    groups = rows[0]["q_bayes_physical_likelihood_component_groups"]
    assert "temperature_trajectory" in groups
    assert "precipitation_evaporative_cooling" in groups
    precip_profile = groups["precipitation_evaporative_cooling"]["source_profile"]
    assert precip_profile["source_class"] == "metar"
    assert precip_profile["no_precip_confirmed"] is True


def test_refresh_awc_fact_preserves_higher_daily_max_when_latest_observation_regresses(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "0")
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    old_payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_refreshed_at": "2026-06-18T14:08:00+00:00",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 31.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T14:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 31.0,
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T14:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_raw": "ZUUU 181400Z ... 31/17 ...",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T14:08:00+00:00",
        },
        "distribution": [
            {"outcome": "30°C", "q_bayes_posterior": 0.34, "q_temperature_bucket_posterior": 0.34},
            {"outcome": "31°C", "q_bayes_posterior": 0.52, "q_temperature_bucket_posterior": 0.52},
        ],
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date="2026-06-18",
        fact_as_of="2026-06-18T14:08:00+00:00",
        model_snapshot_id=11,
        market_snapshot_id=22,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=old_payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-18", fact_id)
    conn.commit()
    conn.close()

    def fake_awc(*_args, **_kwargs):
        return {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 27.0,
            "observed_temp": 27.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "metar_temperature_c": 27.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T15:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 27.0,
            "zuuu_metar_raw_awc_today_max_c": 30.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_raw": "ZUUU 181300Z ... 30/17 ...",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T15:08:00+00:00",
            "zuuu_metar_raw_awc_fetch_duration_ms": 160,
            "zuuu_metar_raw_awc_date_mismatch": False,
            "zuuu_metar_raw_awc_date_mismatch_reason": "",
        }

    monkeypatch.setattr(scan_weather_market, "fetch_zuuu_metar_raw_awc_features", fake_awc)

    result = server.refresh_awc_fact_for_chengdu_event(db_path, event_date="2026-06-18")

    assert result["ok"] is True
    assert result["status"] == "updated"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, "2026-06-18"),
    ).fetchone()
    conn.close()

    payload = json.loads(row["fact_json"])
    features = payload["weather_likelihood_features"]
    by_outcome = {item["outcome"]: item for item in payload["distribution"]}
    assert features["zuuu_metar_raw_awc_latest_at"] == "2026-06-18T15:00:00+00:00"
    assert features["observed_temp_c"] == 27.0
    assert features["zuuu_metar_raw_awc_today_max_c"] == 31.0
    assert features["zuuu_metar_raw_awc_today_max_at"] == "2026-06-18T14:00:00+00:00"
    assert features["observed_today_max_c"] == 31.0
    assert by_outcome["31°C"]["q_bayes_posterior"] == 1.0
    assert by_outcome["30°C"]["q_bayes_posterior"] == 0.0
    assert payload["model_top_label"] == "31°C"
    assert payload["fact_provenance"]["awc_hard_observation_monotonic_preserved"] == {
        "source": "zuuu_metar_raw_awc",
        "refreshed_at": "2026-06-18T15:08:00+00:00",
        "existing_today_max_c": 31.0,
        "incoming_today_max_c": 30.0,
        "today_max_at": "2026-06-18T14:00:00+00:00",
    }


def test_refresh_awc_fact_skips_regressed_latest_without_stronger_daily_max(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PM_AWC_FACT_REFRESH_HEARTBEAT_SEC", "0")
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    old_payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_refreshed_at": "2026-06-18T14:06:00+00:00",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 29.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 29.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T14:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 29.0,
            "zuuu_metar_raw_awc_today_max_c": 29.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T14:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T14:06:00+00:00",
        },
        "distribution": [
            {"outcome": "29°C", "q_bayes_posterior": 0.74, "q_temperature_bucket_posterior": 0.74},
            {"outcome": "30°C", "q_bayes_posterior": 0.26, "q_temperature_bucket_posterior": 0.26},
        ],
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date="2026-06-18",
        fact_as_of="2026-06-18T14:06:00+00:00",
        model_snapshot_id=11,
        market_snapshot_id=22,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=old_payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-18", fact_id)
    conn.commit()
    conn.close()

    def fake_awc(*_args, **_kwargs):
        return {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 28.0,
            "observed_temp": 28.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 28.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "metar_temperature_c": 28.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 28.0,
            "zuuu_metar_raw_awc_today_max_c": 28.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T14:07:00+00:00",
            "zuuu_metar_raw_awc_fetch_duration_ms": 150,
            "zuuu_metar_raw_awc_date_mismatch": False,
            "zuuu_metar_raw_awc_date_mismatch_reason": "",
        }

    monkeypatch.setattr(scan_weather_market, "fetch_zuuu_metar_raw_awc_features", fake_awc)

    result = server.refresh_awc_fact_for_chengdu_event(db_path, event_date="2026-06-18")

    assert result["ok"] is True
    assert result["status"] == "skipped"
    assert result["reason"] == "awc_latest_at_regressed"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT h.id, h.fact_as_of, h.fact_json
        FROM event_fact_history h
        WHERE h.slug = ? AND h.event_date = ?
        ORDER BY h.id
        """,
        (slug, "2026-06-18"),
    ).fetchall()
    latest = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, "2026-06-18"),
    ).fetchone()
    conn.close()

    assert len(rows) == 1
    assert latest["fact_id"] == fact_id
    assert rows[0]["fact_as_of"] == "2026-06-18T14:06:00+00:00"
    payload = json.loads(rows[0]["fact_json"])
    features = payload["weather_likelihood_features"]
    assert features["zuuu_metar_raw_awc_latest_at"] == "2026-06-18T14:00:00+00:00"
    assert features["zuuu_metar_raw_awc_today_max_c"] == 29.0


def test_refresh_awc_fact_skip_uses_summary_cache_without_full_fact_json(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PM_AWC_FACT_REFRESH_HEARTBEAT_SEC", "0")
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    features = {
        "source": "zuuu_metar_raw_awc",
        "observed_temp_c": 29.0,
        "observed_temp_source": "zuuu_metar_raw_awc",
        "observed_today_max_c": 29.0,
        "observed_today_max_source": "zuuu_metar_raw_awc",
        "observed_today_max_trusted": True,
        "zuuu_metar_raw_awc_latest_at": "2026-06-18T14:00:00+00:00",
        "zuuu_metar_raw_awc_latest_temp_c": 29.0,
        "zuuu_metar_raw_awc_today_max_c": 29.0,
        "zuuu_metar_raw_awc_today_max_at": "2026-06-18T14:00:00+00:00",
        "zuuu_metar_raw_awc_fetched_at": "2026-06-18T14:06:00+00:00",
    }
    payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "event_tz": "Asia/Shanghai",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_features": dict(features),
        "weather_likelihood_features_compact": dict(features),
        "distribution": [
            {"outcome": "29°C", "q_bayes_posterior": 0.74, "q_temperature_bucket_posterior": 0.74},
            {"outcome": "30°C", "q_bayes_posterior": 0.26, "q_temperature_bucket_posterior": 0.26},
        ],
    }
    summary_payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "event_tz": "Asia/Shanghai",
        "market_unit": "C",
        "weather_likelihood_features_compact": dict(features),
        "summary_cache_version": server.EVENT_FACT_SUMMARY_CACHE_VERSION,
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date="2026-06-18",
        fact_as_of="2026-06-18T14:06:00+00:00",
        model_snapshot_id=11,
        market_snapshot_id=22,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-18", fact_id)
    conn.execute(
        """
        INSERT OR REPLACE INTO event_fact_summary_cache(
            fact_id, slug, event_date, run_date, fact_as_of, summary_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fact_id,
            slug,
            "2026-06-18",
            "2026-06-18",
            "2026-06-18T14:06:00+00:00",
            json.dumps(summary_payload),
            "2026-06-18T14:06:00+00:00",
        ),
    )
    conn.execute("UPDATE event_fact_history SET fact_json = ? WHERE id = ?", ("not-json", fact_id))
    conn.commit()
    conn.close()

    def fake_awc(*_args, **_kwargs):
        return {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 28.0,
            "observed_temp": 28.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 28.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "metar_temperature_c": 28.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 28.0,
            "zuuu_metar_raw_awc_today_max_c": 28.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T14:07:00+00:00",
            "zuuu_metar_raw_awc_fetch_duration_ms": 150,
            "zuuu_metar_raw_awc_date_mismatch": False,
            "zuuu_metar_raw_awc_date_mismatch_reason": "",
        }

    monkeypatch.setattr(scan_weather_market, "fetch_zuuu_metar_raw_awc_features", fake_awc)

    result = server.refresh_awc_fact_for_chengdu_event(db_path, event_date="2026-06-18")

    assert result["ok"] is True
    assert result["status"] == "skipped"
    assert result["reason"] == "awc_latest_at_regressed"
    assert result["latest_fact_payload_source"] == "summary_cache"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1
    finally:
        conn.close()


def test_awc_recent_window_fetch_hours_uses_existing_same_day_awc(monkeypatch) -> None:
    monkeypatch.setenv("PM_AWC_FACT_REFRESH_RECENT_WINDOW_ENABLED", "1")
    monkeypatch.setenv("PM_AWC_FACT_REFRESH_RECENT_WINDOW_HOURS", "4")
    monkeypatch.setenv("PM_AWC_FACT_REFRESH_RECENT_WINDOW_PRESERVE_LOCAL_DAY_MAX", "0")

    hours, reason = server._awc_recent_window_fetch_hours(
        {
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T04:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_date_mismatch": False,
        },
        event_date="2026-06-18",
        requested_hours=15,
        now_utc=datetime(2026, 6, 18, 5, 30, tzinfo=timezone.utc),
    )

    assert hours == 4
    assert reason == "existing_awc_fact_recent_window"


def test_refresh_awc_fact_passes_recent_window_hours_to_awc_fetch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_AWC_FACT_REFRESH_HEARTBEAT_SEC", "0")
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_unit": "C",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 31.0,
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T04:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 31.0,
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T04:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T04:00:02+00:00",
        },
        "distribution": [{"outcome": "31°C", "q_bayes_posterior": 1.0}],
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date="2026-06-18",
        fact_as_of="2026-06-18T04:00:02+00:00",
        model_snapshot_id=11,
        market_snapshot_id=22,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-18", fact_id)
    conn.commit()
    conn.close()

    captured = {}

    def fake_recent_window(*_args, **_kwargs):
        return 4, "unit_recent_window"

    def fake_awc(*_args, **kwargs):
        captured["hours"] = kwargs.get("hours")
        return {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 31.0,
            "observed_temp": 31.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "metar_temperature_c": 31.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T04:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 31.0,
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T04:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T04:00:03+00:00",
            "zuuu_metar_raw_awc_fetch_duration_ms": 90,
            "zuuu_metar_raw_awc_date_mismatch": False,
            "zuuu_metar_raw_awc_date_mismatch_reason": "",
        }

    monkeypatch.setattr(server, "_awc_recent_window_fetch_hours", fake_recent_window)
    monkeypatch.setattr(scan_weather_market, "fetch_zuuu_metar_raw_awc_features", fake_awc)

    result = server.refresh_awc_fact_for_chengdu_event(
        db_path,
        event_date="2026-06-18",
        hours=15,
    )

    assert captured["hours"] == 4
    assert result["status"] == "skipped"
    assert result["awc_fetch_hours"] == 4
    assert result["awc_fetch_hours_reason"] == "unit_recent_window"


def test_merge_awc_features_preserves_newer_latest_when_regressed_fetch_has_higher_max() -> None:
    payload = {
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 29.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T14:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 29.0,
            "zuuu_metar_raw_awc_latest_raw": "ZUUU 181400Z ... 29/18 ...",
            "zuuu_metar_raw_awc_today_max_c": 30.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T12:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T14:06:00+00:00",
        },
        "distribution": [{"outcome": "31°C", "q_bayes_posterior": 0.1}],
    }
    incoming = {
        "source": "zuuu_metar_raw_awc",
        "observed_temp_c": 31.0,
        "observed_temp": 31.0,
        "observed_temp_source": "zuuu_metar_raw_awc",
        "observed_today_max_c": 31.0,
        "observed_today_max_source": "zuuu_metar_raw_awc",
        "observed_today_max_trusted": True,
        "metar_temperature_c": 31.0,
        "zuuu_metar_raw_awc_latest_at": "2026-06-18T13:00:00+00:00",
        "zuuu_metar_raw_awc_latest_temp_c": 31.0,
        "zuuu_metar_raw_awc_latest_raw": "ZUUU 181300Z ... 31/18 ...",
        "zuuu_metar_raw_awc_today_max_c": 31.0,
        "zuuu_metar_raw_awc_today_max_at": "2026-06-18T13:00:00+00:00",
        "zuuu_metar_raw_awc_today_max_raw": "ZUUU 181300Z ... 31/18 ...",
        "zuuu_metar_raw_awc_fetched_at": "2026-06-18T14:07:00+00:00",
    }

    merged = server._merge_awc_features_into_fact_payload(
        payload,
        incoming,
        refreshed_at="2026-06-18T14:07:00+00:00",
    )

    features = merged["weather_likelihood_features"]
    assert features["zuuu_metar_raw_awc_latest_at"] == "2026-06-18T14:00:00+00:00"
    assert features["zuuu_metar_raw_awc_latest_temp_c"] == 29.0
    assert features["observed_temp_c"] == 29.0
    assert features["zuuu_metar_raw_awc_today_max_c"] == 31.0
    assert features["zuuu_metar_raw_awc_today_max_at"] == "2026-06-18T13:00:00+00:00"
    assert features["observed_today_max_c"] == 31.0
    assert (
        merged["fact_provenance"]["awc_latest_observation_monotonic_preserved"]["existing_latest_at"]
        == "2026-06-18T14:00:00+00:00"
    )


def test_refresh_awc_fact_advances_today_max_time_when_same_max_repeats(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "0")
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    old_payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_refreshed_at": "2026-06-18T13:08:00+00:00",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 31.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 31.0,
            "zuuu_metar_raw_awc_latest_raw": "ZUUU 181300Z ... 31/18 ...",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_raw": "ZUUU 181300Z ... 31/18 ...",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T13:08:00+00:00",
        },
        "distribution": [
            {"outcome": "30°C", "q_bayes_posterior": 0.34, "q_temperature_bucket_posterior": 0.34},
            {"outcome": "31°C", "q_bayes_posterior": 0.52, "q_temperature_bucket_posterior": 0.52},
            {"outcome": "32°C", "q_bayes_posterior": 0.14, "q_temperature_bucket_posterior": 0.14},
        ],
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date="2026-06-18",
        fact_as_of="2026-06-18T13:08:00+00:00",
        model_snapshot_id=11,
        market_snapshot_id=22,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=old_payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-18", fact_id)
    conn.commit()
    conn.close()

    def fake_awc(*_args, **_kwargs):
        return {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 31.0,
            "observed_temp": 31.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "metar_temperature_c": 31.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T14:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 31.0,
            "zuuu_metar_raw_awc_latest_raw": "ZUUU 181400Z ... 31/17 ...",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_raw": "ZUUU 181300Z ... 31/18 ...",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T14:08:00+00:00",
            "zuuu_metar_raw_awc_fetch_duration_ms": 160,
            "zuuu_metar_raw_awc_date_mismatch": False,
            "zuuu_metar_raw_awc_date_mismatch_reason": "",
        }

    monkeypatch.setattr(scan_weather_market, "fetch_zuuu_metar_raw_awc_features", fake_awc)

    result = server.refresh_awc_fact_for_chengdu_event(db_path, event_date="2026-06-18")

    assert result["ok"] is True
    assert result["status"] == "updated"
    assert result["reason"] == "awc_latest_at_changed"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, "2026-06-18"),
    ).fetchone()
    conn.close()

    payload = json.loads(row["fact_json"])
    features = payload["weather_likelihood_features"]
    assert features["zuuu_metar_raw_awc_today_max_at"] == "2026-06-18T14:00:00+00:00"
    assert features["observed_today_max_at"] == "2026-06-18T14:00:00+00:00"
    assert features["zuuu_metar_raw_awc_today_max_raw"] == "ZUUU 181400Z ... 31/17 ..."
    assert payload["hard_source_posterior_override"]["value_at"] == "2026-06-18T14:00:00+00:00"
    assert payload["hard_source_posterior_override"]["latest_at"] == "2026-06-18T14:00:00+00:00"
    assert payload["source_observation_comparison"]["awc"]["observed_at_utc"] == "2026-06-18T14:00:00+00:00"
    reliability = payload["temperature_realization_reliability"]
    observation_state = reliability.get("target_observation_state") or {}
    if observation_state:
        assert observation_state["observed_at_utc"] == "2026-06-18T14:00:00+00:00"
    else:
        assert payload["source_observation_comparison"]["awc"]["observed_at_utc"] == "2026-06-18T14:00:00+00:00"


def test_composer_fact_write_preserves_latest_awc_hard_observation(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    awc_payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "weather_likelihood_refreshed_at": "2026-06-18T14:08:58+00:00",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 26.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T14:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 26.0,
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T09:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T14:08:58+00:00",
        },
        "weather_source_lineage": {
            "zuuu_metar_raw_awc": {
                "source_id": "zuuu_metar_raw_awc",
                "latest_at": "2026-06-18T14:00:00+00:00",
                "today_max_c": 31.0,
            }
        },
        "distribution": [
            {"outcome": "31°C", "q_bayes_posterior": 0.84, "q_temperature_bucket_posterior": 0.84},
            {"outcome": "32°C", "q_bayes_posterior": 0.14, "q_temperature_bucket_posterior": 0.14},
        ],
    }
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date="2026-06-18",
        fact_as_of="2026-06-18T14:08:58+00:00",
        model_snapshot_id=11,
        market_snapshot_id=22,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=awc_payload,
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-18", fact_id)
    conn.commit()

    later_composer_payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "weather_likelihood_refreshed_at": "2026-06-18T14:29:39+00:00",
        "weather_likelihood_features": {
            "source": "wu_twc_observed_intraday",
            "observed_temp_c": 25.5,
        },
        "distribution": [
            {"outcome": "31°C", "q_bayes_posterior": 0.91, "q_temperature_bucket_posterior": 0.91},
            {"outcome": "32°C", "q_bayes_posterior": 0.08, "q_temperature_bucket_posterior": 0.08},
        ],
        "target_bucket_outlook": {"stale": True},
        "chengdu_intraday_summary": {"stale": True},
    }

    new_fact_id = server.upsert_event_fact_from_run_item(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date="2026-06-18",
        run_created_at="2026-06-18T14:29:57+00:00",
        run_payload={"score_composer": {"model_snapshot_id": 33, "market_snapshot_id": 44}},
        item=later_composer_payload,
    )
    assert new_fact_id
    row = conn.execute(
        """
        SELECT h.fact_as_of, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, "2026-06-18"),
    ).fetchone()
    conn.close()

    payload = json.loads(row["fact_json"])
    features = payload["weather_likelihood_features"]
    assert row["fact_as_of"] == "2026-06-18T14:29:57+00:00"
    assert features["observed_today_max_c"] == 31.0
    assert features["observed_today_max_source"] == "zuuu_metar_raw_awc"
    assert features["zuuu_metar_raw_awc_latest_at"] == "2026-06-18T14:00:00+00:00"
    assert payload["distribution"][0]["observed_today_max_c"] == 31.0
    assert payload["distribution"][0]["zuuu_metar_raw_awc_today_max_c"] == 31.0
    assert not payload.get("target_bucket_outlook", {}).get("stale")
    assert not payload.get("chengdu_intraday_summary", {}).get("stale")
    assert payload["fact_provenance"]["awc_hard_observation_preserved_from_latest_fact"]["today_max_c"] == 31.0


def test_upcoming_fact_rows_only_add_chengdu_future_markets(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    entries = [
        ("highest-temperature-in-chengdu-on-june-23-2026", "2026-06-23", "Highest temperature in Chengdu on June 23?"),
        ("highest-temperature-in-london-on-june-23-2026", "2026-06-23", "Highest temperature in London on June 23?"),
    ]
    for idx, (slug, event_date, title) in enumerate(entries, start=1):
        payload = {
            "slug": slug,
            "event_date": event_date,
            "run_date": "2026-06-22",
            "title": title,
            "distribution": [{"outcome": "33°C", "q_bayes_posterior": 0.4}],
        }
        fact_id = server._insert_event_fact_history(
            conn,
            run_date="2026-06-22",
            slug=slug,
            event_date=event_date,
            fact_as_of=f"2026-06-22T0{idx}:00:00+00:00",
            model_snapshot_id=idx,
            market_snapshot_id=idx,
            composer_version=server.FACTS_COMPOSER_VERSION,
            item=payload,
        )
        server._upsert_event_fact_latest(conn, slug, event_date, fact_id)
    conn.commit()

    rows = server.select_event_fact_rows_latest_with_upcoming(conn, "2026-06-22", upcoming_days=2)
    slugs = [str(row["slug"] or "") for row in rows]
    conn.close()

    assert "highest-temperature-in-chengdu-on-june-23-2026" in slugs
    assert "highest-temperature-in-london-on-june-23-2026" not in slugs


def test_market_discovery_placeholders_only_add_chengdu_pending_model_events() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE weather_market_discovery (
            slug TEXT,
            event_date TEXT,
            title TEXT,
            market_open_at TEXT,
            first_seen_at TEXT,
            first_tokenized_at TEXT,
            token_count INTEGER,
            status TEXT,
            details_json TEXT,
            updated_at TEXT
        );
        CREATE TABLE market_tokens (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            yes_token TEXT,
            no_token TEXT,
            outcome_volume REAL,
            liquidity REAL,
            market_active INTEGER,
            market_closed INTEGER,
            market_archived INTEGER,
            market_accepting_orders INTEGER,
            market_enable_orderbook INTEGER,
            updated_at TEXT
        );
        """
    )
    rows = [
        (
            "highest-temperature-in-chengdu-on-june-24-2026",
            "2026-06-24",
            "Highest temperature in Chengdu on June 24?",
        ),
        (
            "highest-temperature-in-london-on-june-24-2026",
            "2026-06-24",
            "Highest temperature in London on June 24?",
        ),
    ]
    for slug, event_date, title in rows:
        conn.execute(
            """
            INSERT INTO weather_market_discovery
              (slug, event_date, title, market_open_at, first_seen_at, first_tokenized_at,
               token_count, status, details_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                event_date,
                title,
                "2026-06-22T12:00:00Z",
                "2026-06-22T12:01:00Z",
                "2026-06-22T12:02:00Z",
                2,
                "tokenized",
                json.dumps({"endDate": "2026-06-24T12:00:00Z"}),
                "2026-06-22T12:03:00Z",
            ),
        )
        for outcome in ("32°C", "33°C"):
            conn.execute(
                """
                INSERT INTO market_tokens
                  (slug, event_date, outcome, yes_token, no_token, outcome_volume, liquidity,
                   market_active, market_closed, market_archived, market_accepting_orders,
                   market_enable_orderbook, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slug,
                    event_date,
                    outcome,
                    f"yes-{slug}-{outcome}",
                    f"no-{slug}-{outcome}",
                    10.0,
                    20.0,
                    1,
                    0,
                    0,
                    1,
                    1,
                    "2026-06-22T12:04:00Z",
                ),
            )
    conn.commit()

    placeholders = server._discovery_placeholder_payloads(
        conn,
        "2026-06-22",
        upcoming_days=2,
        exclude_keys=set(),
        limit=10,
    )
    conn.close()

    assert [item["slug"] for item in placeholders] == [
        "highest-temperature-in-chengdu-on-june-24-2026"
    ]
    placeholder = placeholders[0]
    assert placeholder["data_status"] == "market_discovery_pending_model"
    assert placeholder["station_id"] == "ZUUU"
    assert placeholder["market_discovery"]["source"] == "weather_market_discovery"
    assert placeholder["mean_bucket_na_reason"] == "model_not_materialized_yet"
    assert placeholder["distribution"][0]["q_na_reason"] == "model_not_materialized_yet"


def test_snapshot_key_rows_include_tokenized_chengdu_discovery_for_materialization() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            created_at TEXT
        );
        CREATE TABLE weather_market_discovery (
            slug TEXT,
            event_date TEXT,
            title TEXT,
            market_open_at TEXT,
            first_seen_at TEXT,
            first_tokenized_at TEXT,
            token_count INTEGER,
            status TEXT,
            details_json TEXT,
            updated_at TEXT
        );
        CREATE TABLE market_tokens (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            yes_token TEXT,
            no_token TEXT,
            outcome_volume REAL,
            liquidity REAL,
            market_active INTEGER,
            market_closed INTEGER,
            market_archived INTEGER,
            market_accepting_orders INTEGER,
            market_enable_orderbook INTEGER,
            updated_at TEXT
        );
        """
    )
    slug = "highest-temperature-in-chengdu-on-june-24-2026"
    conn.execute(
        """
        INSERT INTO weather_market_discovery(
            slug, event_date, title, first_seen_at, first_tokenized_at,
            token_count, status, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slug,
            "2026-06-24",
            "Highest temperature in Chengdu on June 24?",
            "2026-06-22T12:44:45+00:00",
            "2026-06-22T12:44:45+00:00",
            2,
            "tokenized",
            "2026-06-22T12:44:45+00:00",
        ),
    )
    for outcome in ("29°C", "30°C"):
        conn.execute(
            """
            INSERT INTO market_tokens(
                slug, event_date, outcome, yes_token, no_token,
                market_active, market_closed, market_archived, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                "2026-06-24",
                outcome,
                f"yes-{outcome}",
                f"no-{outcome}",
                1,
                0,
                0,
                "2026-06-22T12:45:00+00:00",
            ),
        )
    conn.commit()

    rows = server._snapshot_key_rows_for_run_date_range(conn, "2026-06-22", "2026-06-24")
    conn.close()

    assert (slug, "2026-06-24", "2026-06-24") in rows


def test_query_facts_unpaged_include_upcoming_does_not_require_full_mode(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-22-2026"
    fact_id = server._insert_event_fact_history(
        conn,
        run_date="2026-06-22",
        slug=slug,
        event_date="2026-06-22",
        fact_as_of="2026-06-22T08:08:01+00:00",
        model_snapshot_id=1,
        market_snapshot_id=1,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": "2026-06-22",
            "run_date": "2026-06-22",
            "title": "Highest temperature in Chengdu on June 22?",
            "distribution": [{"outcome": "33°C", "q_bayes_posterior": 1.0}],
        },
    )
    server._upsert_event_fact_latest(conn, slug, "2026-06-22", fact_id)
    conn.commit()
    conn.close()
    monkeypatch.setattr(server, "DB_PATH", db_path)

    items = server.query_facts("2026-06-22", include_history=False, include_upcoming=True)

    assert [item["slug"] for item in items] == [slug]


def test_ui_realtime_cache_defaults_are_endpoint_specific() -> None:
    assert server.FACTS_PAGE_CACHE_SEC <= 1
    assert server.CHENGDU_INTRADAY_SUMMARY_CACHE_SEC <= 1
    assert server.DATA_SOURCE_HEALTH_CACHE_SEC >= 30
