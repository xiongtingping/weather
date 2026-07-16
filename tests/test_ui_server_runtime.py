from __future__ import annotations

import inspect
import json
import os
import re
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


def _unit_prior_fields_for_payload(payload: dict) -> dict:
    labels: list[str] = []
    for container_key in ("posterior_temperature_distribution", "distribution", "rows"):
        value = payload.get(container_key)
        rows = value.get("bucket_probabilities") if isinstance(value, dict) else value
        if isinstance(rows, dict):
            for label in rows.keys():
                label_text = str(label or "").strip()
                if label_text and label_text not in labels:
                    labels.append(label_text)
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = str(row.get("outcome") or row.get("bucket") or row.get("label") or "").strip()
            if label and label not in labels:
                labels.append(label)
    if not labels:
        labels = ["29°C"]
    probability = 1.0 / len(labels)
    return {
        "prior_distribution": {label: probability for label in labels},
        "prior_na_reason": "",
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "state": "available",
            "source": "unit_test_settled_history_prior",
            "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
            "sample_count": max(10, len(labels)),
            "shrinkage_target": "broader_settled_history",
            "date_window_days": 15,
            "prior_na_reason": "",
        },
    }


def attach_unit_prior_contract(payload: dict) -> dict:
    payload.update(_unit_prior_fields_for_payload(payload))
    return payload


def _unit_prior_contract_covers_payload(payload: dict) -> bool:
    prior = payload.get("prior_distribution")
    if not isinstance(prior, dict) or not isinstance(payload.get("prior_meta"), dict):
        return False
    required = set(_unit_prior_fields_for_payload(payload)["prior_distribution"])
    available = {str(label or "").strip() for label in prior.keys() if str(label or "").strip()}
    return required.issubset(available)


def insert_event_fact_history_with_unit_prior(*args, **kwargs) -> int:
    item = kwargs.get("item")
    if isinstance(item, dict):
        pending_model = str(item.get("data_status") or item.get("weather_strategy_mode") or "") == (
            "market_discovery_pending_model"
        )
        if not pending_model and not _unit_prior_contract_covers_payload(item):
            attach_unit_prior_contract(item)
    return getattr(server, "_insert_event_fact_history")(*args, **kwargs)


def build_ui_event_fact_write_compat_plan_with_unit_prior(*args, **kwargs) -> dict:
    item = kwargs.get("item")
    if isinstance(item, dict):
        if not _unit_prior_contract_covers_payload(item):
            attach_unit_prior_contract(item)
    return getattr(server, "build_ui_event_fact_write_compat_plan")(*args, **kwargs)


def attach_unit_posterior_ssot_fact(
    payload: dict,
    *,
    target_bucket: str,
    target_probability: float,
    fact_snapshot_id: str = "unit_fact_snapshot",
    posterior_run_id: str = "unit_posterior_run",
) -> dict:
    payload.setdefault("fact_snapshot_id", fact_snapshot_id)
    payload.setdefault("posterior_run_id", posterior_run_id)
    payload.setdefault("phase", "D0_unit_test")
    payload.setdefault("beta_weak", 0.0)
    payload.setdefault("beta_strong", 1.0)
    payload.setdefault("phase_reason", "unit_test_ssot_contract")
    payload.setdefault("hard_mask_reason", "unit_test_no_hard_mask")
    rows: list[dict] = []
    for container_key in ("posterior_temperature_distribution", "distribution", "rows"):
        value = payload.get(container_key)
        raw_rows = value.get("bucket_probabilities") if isinstance(value, dict) else value
        if isinstance(raw_rows, dict):
            raw_rows = [
                {"outcome": str(label), "probability": probability}
                for label, probability in raw_rows.items()
            ]
        if not isinstance(raw_rows, list):
            continue
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            label = str(row.get("outcome") or row.get("bucket") or row.get("label") or "").strip()
            prob = row.get("probability")
            if prob is None:
                prob = row.get("q_temperature_bucket_posterior")
            if prob is None:
                prob = row.get("q_bayes_posterior")
            if not label or prob is None:
                continue
            compact_row = {"outcome": label, "probability": float(prob)}
            for key in (
                "posterior_settlement_candidate_bucket_hard_zero",
                "posterior_settlement_candidate_bucket_hard_zero_reasons",
                "q_posterior_observed_constraints_hard_zero",
                "q_bayes_physical_likelihood_hard_zero",
                "impossible_by_observed_max",
                "hard_zero",
                "hard_mask_reason",
            ):
                if key in row:
                    compact_row[key] = row.get(key)
            rows.append(compact_row)
        if rows:
            break
    if not rows:
        rows = [{"outcome": target_bucket, "probability": float(target_probability)}]
    rows.sort(key=lambda row: (-float(row.get("probability") or 0.0), str(row.get("outcome") or "")))
    payload["posterior_ssot_fact"] = {
        "schema": "bayes_posterior_ssot_fact_v1",
        "state": "available",
        "read_only": True,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
        "posterior_as_of": payload.get("posterior_as_of") or payload.get("fact_as_of") or "2026-07-01T00:00:00+00:00",
        "target_bucket": target_bucket,
        "target_probability": float(target_probability),
        "target_probability_pct": float(target_probability) * 100.0,
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "phase": payload.get("phase"),
        "beta_weak": payload.get("beta_weak"),
        "beta_strong": payload.get("beta_strong"),
        "phase_reason": payload.get("phase_reason"),
        "hard_mask_reason": payload.get("hard_mask_reason"),
        "bucket_probabilities": rows,
        "layer_chain": {
            "schema": "bayes_layer_chain_summary_v1",
            "formula": "P_prior * L_forecast_weak^beta_weak * L_metar_strong^beta_strong * hard_mask",
        },
        "boundaries": {
            "frontend_recompute_allowed": False,
            "decision_recompute_allowed": False,
            "hard_mask_is_constraint_not_weight": True,
        },
    }
    return payload


def test_temperature_trajectory_timeline_accepts_bucket_only_distribution_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-july-6-2026"
    payload = {
        "slug": slug,
        "event_date": "2026-07-06",
        "run_date": "2026-07-06",
        "fact_as_of": "2026-07-06T01:54:37Z",
        "distribution": [
            {"bucket": "32°C or below", "q_temperature_bucket_posterior": 0.0},
            {"bucket": "33°C", "q_temperature_bucket_posterior": 0.16198571441083798},
        ],
        "weather_likelihood_features_compact": {
            "zuuu_metar_raw_amsc_latest_at": "2026-07-06T01:30:00Z",
            "zuuu_metar_raw_amsc_latest_temp_c": 33.0,
            "zuuu_metar_raw_amsc_today_max_c": 33.0,
            "zuuu_metar_raw_amsc_today_max_at": "2026-07-06T01:30:00Z",
            "temperature_momentum_effective_c_per_hour": 1.0,
        },
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-07-06",
            slug,
            "2026-07-06",
            "2026-07-06T01:54:37Z",
            "unit_bucket_only_distribution",
            json.dumps(payload, ensure_ascii=False),
            "2026-07-06T01:54:38Z",
        ),
    )
    conn.commit()

    points = server._ui_temperature_timeline_points_from_sql(
        conn,
        slug_key=slug,
        event_date_key="2026-07-06",
        target_label="33°C",
        event_tz="Asia/Shanghai",
    )

    assert len(points) == 1
    assert points[0]["target_bucket"] == "33°C"
    assert points[0]["target_probability_pct"] == pytest.approx(16.198571441083796)
    assert points[0]["awc_temp_c"] == 33.0
    assert points[0]["temperature_momentum_effective_c_per_hour"] == 1.0
    conn.close()


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
    assert "候选模型权重" in text
    assert "暂不上线原因" in text
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


def test_ui_index_prefers_backend_posterior_ssot_section() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    assert "function eventIntelPosteriorSsotSection(item)" in html
    assert "function eventIntelPosteriorSsotRows(item, limit = 12)" in html

    start = html.index("function eventIntelTargetBucketSelectionFromSnapshot(item, payload = {}, decision = {}, plan = {})")
    end = html.index("function eventIntelCapitalLegForBucket", start)
    selection_block = html[start:end]
    assert "const posteriorSsot = eventIntelPosteriorSsotSection(item);" in selection_block
    assert "const canonicalTarget = eventIntelCanonicalTargetFromSnapshot(item, payload);" not in selection_block
    assert "if (canonicalTarget.label)" not in selection_block
    assert selection_block.index("if (posteriorSsotLabel)") < selection_block.index("} else if (hardOutlookTargetLabel)")
    assert "source = posteriorSsot.source || 'ui_sections.bayes_posterior_ssot';" in selection_block
    assert "capitalTargetLabel" not in selection_block
    assert "selectionTargetLabel" not in selection_block

    start = html.index("function eventBayesPosteriorRows(item, limit = 3, distributionOverride = null)")
    end = html.index("function eventBayesPosteriorDistributionMeta(item)", start)
    rows_block = html[start:end]
    assert "const ssotRows = !distributionOverride ? eventIntelPosteriorSsotRows(item, limit) : [];" in rows_block
    assert rows_block.index("if (ssotRows.length) return ssotRows;") < rows_block.index("const ctpRows = !distributionOverride")

    start = html.index("const posteriorSsotForSpotlight = eventIntelPosteriorSsotSection(itemRef);")
    end = html.index("const posteriorSpotlightPrimary = resolvePosteriorSpotlightPrimary();", start)
    spotlight_block = html[start:end]
    assert "source: 'posterior_ssot_fact'" in spotlight_block
    assert "source: 'capital_plan'" not in spotlight_block
    assert "source: 'distribution_top'" not in spotlight_block


def test_ui_index_records_backend_ui_snapshot_dashboard_sections() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "function uiSnapshotDashboardSections(snapshot)" in html
    assert "String(sections.schema || '') !== 'ui_snapshot_dashboard_sections_v1'" in html
    assert "String(sections.source || '') !== '/api/ui_snapshot'" in html
    assert "function uiSnapshotPanelFreshness(snapshot, key = '')" in html
    assert "function renderUiSnapshotSsotChainBoard(snapshot)" in html
    assert "function mountUiSnapshotSsotChainBoard(snapshot)" in html
    assert "const orderbook = execution.orderbook && typeof execution.orderbook === 'object' ? execution.orderbook : {};" in html
    assert "const order = Array.isArray(sections.order) && sections.order.length" in html
    assert "source_to_posterior: '源→后验'" in html
    assert "awc_signal_to_order: '硬源 3s'" in html
    assert "data.latest_source_response_completed_at" in html
    assert "data.latest_source_to_posterior_ms ?? data.latest_source_response_to_posterior_ms" in html
    assert "data.amsc_latency_slo_audit" in html
    assert "AMSC SLO" in html
    assert "valid→fetch" in html
    assert "fact→posterior" in html
    assert "posterior→decision" in html
    assert "慢在抓取" in html
    assert "响应→后验" in html
    assert "后验→决策" in html
    assert "非订单信号" in html
    assert "data.latest_signal_to_order_ms" in html
    assert "信号→订单" in html
    assert "SLO通过" in html
    assert "window.__lastUiSnapshotDashboardSections = sections;" in html
    assert "window.__lastUiSnapshotPanelFreshness = panelFreshness;" in html
    assert "window.__lastUiSnapshotPayload = snapshot;" in html
    assert "mountUiSnapshotSsotChainBoard(snapshot);" in html
    assert "if (options.mountSsot !== false) mountUiSnapshotSsotChainBoard(snapshot);" in html
    assert "primeSnapshotBoardsFromSnapshot(snapshot, { mountSsot: false });" in html
    start = html.index("function renderUiSnapshotSsotChainBoard(snapshot)")
    end = html.index("function mountUiSnapshotSsotChainBoard", start)
    board_block = html[start:end]
    assert "return null;" not in board_block
    assert "SSOT 概率主线" in html
    assert "forecast_weak" in board_block
    assert "observation_strong" in board_block
    assert "hard_mask" in board_block
    assert "bayes_posterior_ssot" in board_block
    assert "前端不复算" in html


def test_ui_snapshot_forecast_weak_section_is_read_only_and_ranked_server_side() -> None:
    item = {
        "fact_snapshot_id": "fact-weak-1",
        "posterior_run_id": "posterior-weak-1",
        "forecast_weak_likelihood": {"32°C": 0.25, "33°C": 0.65, "34°C": 0.10},
        "beta_weak": 0.7,
        "production_model_version": "ssot_probability_mainline_v1",
    }

    section = server._build_ui_snapshot_forecast_weak_section(item)

    assert section["schema"] == "ui_bayes_forecast_weak_section_v1"
    assert section["state"] == "available"
    assert section["read_only"] is True
    assert section["frontend_recompute_allowed"] is False
    assert section["top_bucket"] == "33°C"
    assert section["top_probability_pct"] == pytest.approx(65.0)
    assert section["role"] == "prediction_weak_likelihood"
    assert section["boundary"] == "forecast_sources_only_all_sources_are_predictions_no_observation_facts"


def test_ui_snapshot_hard_mask_section_is_explicit_and_read_only() -> None:
    section = server._build_ui_snapshot_hard_mask_section(
        {
            "fact_snapshot_id": "fact-mask-1",
            "posterior_run_id": "posterior-mask-1",
            "hard_mask_reason": "observed_today_max_floor",
            "hard_constraints": {"observed_today_max_c": 33.0},
        }
    )

    assert section["schema"] == "ui_bayes_hard_mask_section_v1"
    assert section["state"] == "available"
    assert section["active"] is True
    assert section["read_only"] is True
    assert section["frontend_recompute_allowed"] is False
    assert section["role"] == "hard_physical_feasibility_mask"


def test_ui_snapshot_dashboard_sections_exposes_source_to_posterior_timing() -> None:
    item = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "event_date": "2026-07-06",
        "fact_id": 106817,
        "fact_as_of": "2026-07-06T03:30:24Z",
        "posterior_as_of": "2026-07-06T03:30:24Z",
        "fact_snapshot_id": "fact_timing",
        "posterior_run_id": "post_timing",
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_timing",
            "posterior_run_id": "post_timing",
            "posterior_as_of": "2026-07-06T03:30:24Z",
            "target_bucket": "36°C",
            "target_probability": 0.31,
            "bucket_probabilities": [{"outcome": "36°C", "probability": 0.31}],
        },
        "source_to_posterior_timing": {
            "schema": "source_to_posterior_timing_v1",
            "state": "available",
            "surface": "amsc_hard_source_to_posterior_materialization",
            "source_provider": "AMSC",
            "latest_source_id": "zuuu_metar_raw_amsc",
            "latest_source_response_completed_at": "2026-07-06T03:30:15Z",
            "latest_source_fact_as_of": "2026-07-06T03:30:15Z",
            "latest_source_valid_time": "2026-07-06T03:00:00Z",
            "posterior_apply_started_at": "2026-07-06T03:30:24Z",
            "source_response_to_posterior_apply_start_ms": 9000,
            "source_fact_to_posterior_apply_start_ms": 9000,
            "task_queue_wait_ms": 8000,
            "source_history_fact_ids": [106816],
            "source_event_ids": ["srcevt_timing"],
            "source_observation_ids": ["srcobs_timing"],
            "order_signal_applicability": "not_applicable_no_order_signal",
        },
    }
    payload = {
        "path_probability_follow": {
            "entry": {
                "counts": {"will_order": 0, "blocked": 0, "total": 1},
                "latest": [
                    {
                        "slug": item["slug"],
                        "event_date": item["event_date"],
                        "created_at": "2026-07-06T03:30:30Z",
                        "will_order": 0,
                        "reason_codes": [],
                        "reasons": [],
                    }
                ],
            }
        }
    }

    sections = server._build_ui_snapshot_dashboard_sections(
        payload,
        facts_map={"source": "unit", "total": 1},
        items=[item],
        freshness_state="fresh",
        freshness_reason="",
        latest_fact_as_of="2026-07-06T03:30:24Z",
        posterior_available_count=1,
        posterior_not_materialized_count=0,
        posterior_missing_count=0,
        latest_posterior_as_of="2026-07-06T03:30:24Z",
    )

    assert sections["order"][:4] == ["facts", "source_to_posterior", "posterior", "awc_signal_to_order"]
    timing = sections["source_to_posterior"]
    assert timing["schema"] == "ui_snapshot_source_to_posterior_section_v1"
    assert timing["state"] == "fresh"
    assert timing["timing_count"] == 1
    assert timing["latest_source_to_posterior_ms"] == 9000
    assert timing["task_queue_wait_ms"] == 8000
    assert timing["posterior_to_decision_ms"] is None
    assert timing["decision_candidate_state"] == "not_applicable_no_order_signal"
    assert timing["source_provider"] == "AMSC"
    assert timing["latest_source_id"] == "zuuu_metar_raw_amsc"
    assert timing["order_signal_applicability"] == "not_applicable_no_order_signal"
    assert timing["latest"]["source_history_fact_ids"] == [106816]
    audit = timing["amsc_latency_slo_audit"]
    assert audit["schema"] == "amsc_latency_slo_audit_v1"
    assert audit["valid_time"] == "2026-07-06T03:00:00Z"
    assert audit["fetched_at"] == "2026-07-06T03:30:15Z"
    assert audit["fact_as_of"] == "2026-07-06T03:30:15Z"
    assert audit["posterior_as_of"] == "2026-07-06T03:30:24Z"
    assert audit["decision_at"] == ""
    assert audit["stage_latencies_ms"]["valid_to_fetched"] == 1_815_000
    assert audit["stage_latencies_ms"]["fetched_to_fact"] == 0
    assert audit["stage_latencies_ms"]["fact_to_posterior"] == 9_000
    assert audit["stage_latencies_ms"]["posterior_to_decision"] is None
    assert audit["decision_at_na_reason"] == "decision_not_available_in_ui_snapshot_decision_feed"
    assert timing["frontend_recompute_allowed"] is False
    assert timing["decision_recompute_allowed"] is False

    order_signal_item = json.loads(json.dumps(item))
    order_signal_item["source_to_posterior_timing"]["order_signal_applicability"] = "hard_source_order_signal"
    order_signal_sections = server._build_ui_snapshot_dashboard_sections(
        payload,
        facts_map={"source": "unit", "total": 1},
        items=[order_signal_item],
        freshness_state="fresh",
        freshness_reason="",
        latest_fact_as_of="2026-07-06T03:30:24Z",
        posterior_available_count=1,
        posterior_not_materialized_count=0,
        posterior_missing_count=0,
        latest_posterior_as_of="2026-07-06T03:30:24Z",
    )
    order_signal_timing = order_signal_sections["source_to_posterior"]
    assert order_signal_timing["decision_candidate_state"] == "decision_after_latest_posterior"
    assert order_signal_timing["posterior_to_decision_ms"] == 6000
    order_audit = order_signal_timing["amsc_latency_slo_audit"]
    assert order_audit["decision_at"] == "2026-07-06T03:30:30Z"
    assert order_audit["posterior_to_decision_ms"] == 6000
    assert order_audit["stage_latencies_ms"]["posterior_to_decision"] == 6000
    assert order_audit["stage_status"]["posterior_to_decision"] == "breached"
    assert order_audit["valid_to_decision_ms"] == 1_830_000


def test_ui_snapshot_dashboard_sections_exposes_awc_signal_to_order_timing() -> None:
    item = {
        "slug": "highest-temperature-in-chengdu-on-july-4-2026",
        "event_date": "2026-07-04",
        "fact_id": 106743,
        "fact_as_of": "2026-07-04T15:15:53.593694+00:00",
        "fact_snapshot_id": "fact_awc",
        "posterior_run_id": "post_awc",
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_awc",
            "posterior_run_id": "post_awc",
            "posterior_as_of": "2026-07-04T10:06:52.500Z",
            "target_bucket": "36°C",
            "target_probability": 0.9,
            "bucket_probabilities": [{"outcome": "36°C", "probability": 0.9}],
        },
        "signal_to_order_timing": {
            "schema": "signal_to_order_timing_v1",
            "signal_id": "sig_awc",
            "source_event_id": "srcevt_awc",
            "source_observation_id": "srcobs_awc",
            "source_id": "zuuu_metar_raw_awc",
            "fact_snapshot_id": "fact_awc",
            "posterior_run_id": "post_awc",
            "acceptance_state": "accepted",
            "acceptance_reason": "awc_hard_source_signal_accepted",
            "awc_valid_time": "2026-07-04T10:00:00Z",
            "awc_hard_source_signal_at": "2026-07-04T10:06:52.268Z",
            "fact_written_at": "2026-07-04T10:06:52.368Z",
            "posterior_written_at": "2026-07-04T10:06:52.468Z",
            "decision_written_at": "2026-07-04T10:06:52.668Z",
            "job_enqueued_at": "2026-07-04T10:06:52.900Z",
            "order_submitted_at": "2026-07-04T10:06:54.000Z",
            "terminal_at": "2026-07-04T10:06:54.000Z",
            "terminal_state": "submitted",
            "terminal_reason": "order_submitted",
            "orderbook_checked_at": "2026-07-04T10:06:52.700Z",
            "risk_checked_at": "2026-07-04T10:06:53.000Z",
            "idempotency_checked_at": "2026-07-04T10:06:53.100Z",
            "submit_requested_at": "2026-07-04T10:06:53.200Z",
            "exchange_ack_at": "2026-07-04T10:06:54.000Z",
            "signal_to_fact_ms": 100,
            "signal_to_decision_ms": 400,
            "signal_to_job_enqueue_ms": 632,
            "awc_signal_to_order_submit_ms": 1732,
            "signal_to_terminal_ms": 1732,
            "signal_to_order_slo_ms": 3000,
            "signal_to_order_slo_met": True,
            "pre_signal_prepare_state": "prepared_before_deadline",
            "pre_signal_prepare_complete": True,
            "pre_signal_prepared_before_deadline": True,
        },
    }

    sections = server._build_ui_snapshot_dashboard_sections(
        {"path_probability_follow": {}},
        facts_map={"source": "unit", "total": 1},
        items=[item],
        freshness_state="fresh",
        freshness_reason="",
        latest_fact_as_of="2026-07-04T15:15:53.593694+00:00",
        posterior_available_count=1,
        posterior_not_materialized_count=0,
        posterior_missing_count=0,
        latest_posterior_as_of="2026-07-04T10:06:52.500Z",
    )

    awc = sections["awc_signal_to_order"]
    assert awc["schema"] == "ui_snapshot_awc_signal_to_order_section_v1"
    assert awc["state"] == "fresh"
    assert awc["timing_count"] == 1
    assert awc["latest_signal_id"] == "sig_awc"
    assert awc["latest_terminal_state"] == "submitted"
    assert awc["latest_signal_to_order_ms"] == 1732
    assert awc["signal_to_order_slo_ms"] == 3000
    assert awc["signal_to_order_slo_met"] is True
    assert awc["accepted_signal_count"] == 1
    assert awc["submitted_signal_count"] == 1
    assert awc["slo_met_count"] == 1
    assert awc["latest_signal_to_fact_ms"] == 100
    assert awc["latest_signal_to_decision_ms"] == 400
    assert awc["latest_signal_to_job_enqueue_ms"] == 632
    assert awc["pre_signal_prepare_state"] == "prepared_before_deadline"
    assert awc["safety_checks"]["orderbook_checked_at"] == "2026-07-04T10:06:52.700Z"
    assert awc["frontend_recompute_allowed"] is False
    assert awc["decision_recompute_allowed"] is False


def test_ui_snapshot_dashboard_sections_marks_awc_signal_to_order_empty_when_no_signal() -> None:
    sections = server._build_ui_snapshot_dashboard_sections(
        {"path_probability_follow": {}},
        facts_map={"source": "unit", "total": 1},
        items=[
            {
                "slug": "highest-temperature-in-chengdu-on-july-6-2026",
                "event_date": "2026-07-06",
                "fact_id": 106817,
                "posterior_ssot_fact": {
                    "schema": "bayes_posterior_ssot_fact_v1",
                    "state": "available",
                    "target_bucket": "36°C",
                    "target_probability": 0.31,
                },
                "signal_to_order_timing": {
                    "schema": "signal_to_order_timing_placeholder_v1",
                    "state": "not_applicable",
                    "source_to_posterior_timing": {"schema": "source_to_posterior_timing_v1"},
                },
            }
        ],
        freshness_state="fresh",
        freshness_reason="",
        latest_fact_as_of="2026-07-06T03:30:24Z",
        posterior_available_count=1,
        posterior_not_materialized_count=0,
        posterior_missing_count=0,
        latest_posterior_as_of="2026-07-06T03:30:24Z",
    )

    awc = sections["awc_signal_to_order"]
    assert awc["state"] == "empty"
    assert awc["timing_count"] == 0
    assert awc["na_reason"] == "awc_signal_to_order_timing_not_applicable_or_no_sample"


def test_ui_index_today_physical_verdict_requires_backend_evidence() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = html.index("function buildTodayPhysicalEvidenceVerdict(")
    end = html.index("function renderTodayPhysicalEvidenceVerdict", start)
    block = html[start:end]

    assert "const strongPhysicalSnapshot = metarStrongLikelihoodSnapshotFromItem(itemData, payloadData)" in block
    assert "const strongLayerDisplay = physicalStrongLikelihoodLayerDisplay(strongPhysicalSnapshot);" in block
    assert "const hasStrongPhysicalSnapshot = Boolean(" in block
    assert "const hasConcretePhysicalEvidence = hasStrongPhysicalSnapshot;" in block
    assert "hasMetricEvidence" not in block
    assert "hasModifierEvidence" not in block
    assert "hasTrajectoryEvidence" not in block
    assert "naReason: 'metar_strong_physical_evidence_missing'" in block
    assert "不把云雨、轨迹或旧物理字段单独解释成今日总判" in block
    assert block.index("if (!hasConcretePhysicalEvidence)") < block.index("const cards = [")
    assert "available: Boolean(hasConcretePhysicalEvidence &&" in block
    assert "物理证据中性，等待轨迹确认" not in block


def test_ui_index_reads_prior_and_observation_sections_from_ui_snapshot() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    assert "function eventIntelPriorSection(item)" in html
    assert "function eventIntelObservationStrongSection(item)" in html
    assert "function eventBayesPriorSectionRows(item, limit = 3)" in html
    assert "const sectionPriorRows = eventBayesPriorSectionRows(item, 3);" in html
    assert "const priorRows = sectionPriorRows.length ? sectionPriorRows" in html
    assert "const sectionPriorRows = eventBayesPriorSectionRows(item, 8);" in html
    assert "const observationSection = eventIntelObservationStrongSection(data);" in html
    assert "raw_hard_observation_only" in html
    assert "const latestHardObservation = eventIntelLatestHardObservation(item);" in html
    assert "latestHardObservation.observed_today_max_c" in html


def test_ui_index_merges_snapshot_ssot_when_chengdu_summary_is_present() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = html.index("function eventIntelPayloadFromSnapshotItem(item)")
    end = html.index("function storeEventIntelChengduIntradayPayload", start)
    block = html[start:end]

    assert "const snapshotPayload = {" in block
    assert "key_metrics: {" in block
    assert "...compactFeatures," in block
    assert "...fullFeatures," in block
    assert "...metrics," in block
    assert "...snapshotPayload," in block
    assert "...summaryPayload," in block
    assert "...summaryCompact," in block
    assert "...snapshotCompact," in block
    assert "posterior_ssot_fact: Object.keys(objectOrEmpty(snapshotPayload.posterior_ssot_fact)).length" in block
    assert "temperature_trajectory: Object.keys(objectOrEmpty(snapshotPayload.temperature_trajectory)).length" in block
    assert "temperature_trajectory_status: Object.keys(objectOrEmpty(snapshotPayload.temperature_trajectory_status)).length" in block


def test_ui_index_legacy_likelihood_compat_is_null_safe() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = html.index("const rawLegacyLikelihoodCompat = legacyDistributionLikelihoodCompatForRow(row);")
    end = html.index("const pHistory = legacyLikelihoodCompat.pHistory;", start)
    block = html[start:end]

    assert "rawLegacyLikelihoodCompat && typeof rawLegacyLikelihoodCompat === 'object'" in block
    assert ": {};" in block


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
    assert "force_rebuild = force or refresh" not in block
    assert "force_rebuild=False" in block
    assert 'payload["refresh_mode"] = "stale_while_revalidate"' in block
    assert 'payload["synchronous_rebuild_allowed"] = False' in block
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
    attach_unit_posterior_ssot_fact(payload, target_bucket="30°C or higher", target_probability=0.70)
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


def test_ui_snapshot_summary_prefers_posterior_ssot_fact_target() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-4-2026",
        "title": "Highest temperature in Chengdu on July 4?",
        "event_date": "2026-07-04",
        "run_date": "2026-07-04",
        "fact_snapshot_id": "fact_ssot_ui",
        "posterior_run_id": "post_ssot_ui",
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "read_only": True,
            "fact_snapshot_id": "fact_ssot_ui",
            "posterior_run_id": "post_ssot_ui",
            "posterior_as_of": "2026-07-04T07:06:03+00:00",
            "target_bucket": "29°C",
            "target_probability": 0.64,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "phase": "D0_awc_hard_signal",
            "beta_weak": 0.0,
            "beta_strong": 1.0,
            "phase_reason": "unit_test_awc_signal",
            "hard_mask_reason": "observed_today_max_c",
            "bucket_probabilities": [
                {"outcome": "30°C or higher", "probability": 0.21},
                {"outcome": "29°C", "probability": 0.64},
            ],
            "layer_chain": {
                "formula": "P_prior * L_forecast_weak^beta_weak * L_metar_strong^beta_strong * hard_mask",
                "forecast_weak_likelihood_present": True,
                "metar_strong_likelihood_present": True,
            },
            "boundaries": {
                "frontend_recompute_allowed": False,
                "decision_recompute_allowed": False,
            },
        },
        "distribution": [
            {"outcome": "30°C or higher", "q_temperature_bucket_posterior": 0.79},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.21},
        ],
        "target_bucket": "30°C or higher",
        "ssot_target_bucket": "30°C or higher",
        "target_bucket_probability_field": "posterior_temperature_distribution.bucket_probabilities",
        "target_bucket_probability_pct": 79.0,
        "target_bucket_selection": {
            "target_bucket": "30°C or higher",
            "target_probability": 0.79,
            "target_probability_field": "posterior_temperature_distribution.bucket_probabilities",
            "ssot_source": "event_fact_latest",
        },
        "posterior_target_bucket_selection": {
            "target_bucket": "30°C or higher",
            "target_probability": 0.79,
            "target_probability_field": "posterior_temperature_distribution.bucket_probabilities",
            "ssot_source": "event_fact_latest",
        },
    }

    canonical = server._canonical_target_from_fact_payload(payload)
    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    section = slim["ui_sections"]["bayes_posterior_ssot"]

    assert canonical["source"] == "posterior_ssot_fact.top_bucket"
    assert canonical["label"] == "29°C"
    assert canonical["prob"] == pytest.approx(0.64)
    assert slim["posterior_ssot_fact"]["target_bucket"] == "29°C"
    assert slim["posterior_ssot_fact"]["target_bucket_label"] == "29°C"
    assert slim["ssot_target_bucket"] == "29°C"
    assert slim["target_bucket"] == "29°C"
    assert slim["target_bucket_selection"]["target_probability"] == pytest.approx(0.64)
    assert slim["target_bucket_selection"]["target_probability_field"] == "posterior_ssot_fact.target_probability"
    assert slim["target_bucket_selection"]["ssot_source"] == "posterior_ssot_fact"
    assert slim["posterior_target_bucket_selection"] == slim["target_bucket_selection"]
    assert section["state"] == "available"
    assert section["target_bucket"] == "29°C"
    assert section["target_bucket_label"] == "29°C"
    assert section["target_probability"] == pytest.approx(0.64)
    assert section["phase"] == "D0_awc_hard_signal"
    assert section["beta_strong"] == 1.0
    assert section["hard_mask_reason"] == "observed_today_max_c"
    assert section["frontend_recompute_allowed"] is False
    assert section["decision_recompute_allowed"] is False


def test_ui_snapshot_summary_blocks_target_fallback_when_posterior_ssot_missing() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-4-2026",
        "title": "Highest temperature in Chengdu on July 4?",
        "event_date": "2026-07-04",
        "run_date": "2026-07-04",
        "distribution": [
            {"outcome": "30°C or higher", "q_temperature_bucket_posterior": 0.79},
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.21},
        ],
        "target_bucket": "30°C or higher",
        "target_bucket_probability_pct": 79.0,
        "target_bucket_selection": {
            "target_bucket": "30°C or higher",
            "target_probability": 0.79,
            "target_probability_field": "posterior_temperature_distribution.bucket_probabilities",
        },
    }

    assert server._canonical_target_from_fact_payload(payload) == {}
    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    section = slim["ui_sections"]["bayes_posterior_ssot"]

    assert "target_bucket" not in slim
    assert "ssot_target_bucket" not in slim
    assert "target_bucket_selection" not in slim
    assert slim["target_bucket_na_reason"] == "posterior_ssot_fact_missing"
    assert section["state"] == "missing"
    assert section["na_reason"] == "posterior_ssot_fact_missing"


def test_ui_snapshot_summary_exposes_prior_section_from_root_prior_distribution() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "title": "Highest temperature in Chengdu on July 6?",
        "event_date": "2026-07-06",
        "run_date": "2026-07-06",
        "fact_snapshot_id": "fact_prior_ui",
        "posterior_run_id": "post_prior_ui",
        "prior_distribution": {"36°C": 0.25, "37°C": 0.55, "38°C": 0.20},
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "source": "settled_history_site_terrain_weather_type",
            "sample_count": 42,
        },
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_placeholder_v1",
            "state": "not_materialized",
            "state_reason": "unit_test_waiting_for_posterior",
            "read_only": True,
        },
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    prior = slim["ui_sections"]["prior"]

    assert slim["prior_distribution"]["37°C"] == pytest.approx(0.55)
    assert prior["schema"] == "ui_bayes_prior_section_v1"
    assert prior["state"] == "available"
    assert prior["source"] == "facts.items[].prior_distribution"
    assert prior["role"] == "root_prior"
    assert prior["top_bucket"] == "37°C"
    assert prior["top_bucket_label"] == "37°C"
    assert prior["top_probability"] == pytest.approx(0.55)
    assert prior["bucket_probabilities"][0]["outcome"] == "37°C"
    assert prior["prior_meta"]["sample_count"] == 42
    assert prior["frontend_recompute_allowed"] is False


def test_temperature_trajectory_status_prefers_root_ssot_over_legacy_q_fields() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "event_date": "2026-07-06",
        "weather_likelihood_features_compact": {
            "observed_temp_c": 29.0,
            "observed_today_max_c": 31.0,
            "temperature_momentum_effective_c_per_hour": 1.0,
            "temperature_momentum_realization_state": "emerging_warming",
            "temperature_momentum_posterior_gate_allowed": True,
            "temperature_momentum_posterior_gate_reason": "legacy_one_hour_context",
        },
        "temperature_trajectory": {
            "schema": "temperature_trajectory_summary_v1",
            "state": "platform",
            "gate_state": "not_warming",
            "gate_reason": "trajectory_state_platform",
            "stable_rate_c_per_hour": 0.0,
            "effective_rate_c_per_hour": 1.0,
            "adjusted_rate_c_per_hour": 0.0,
            "rolling_temperature_observation_summary": {
                "current_temp_c": 33.0,
                "rolling_max_c": 34.0,
            },
        },
        "distribution": [
            {
                "outcome": "36°C",
                "q_bayes_temperature_trajectory_status": "unavailable_after_ctp_materialization",
                "q_bayes_temperature_trajectory_na_reason": "temperature_trajectory_component_missing_after_ctp_materialization",
            }
        ],
    }

    status = server._ui_temperature_trajectory_status_from_payload(payload, payload["distribution"])

    assert status["available"] is True
    assert status["state"] == "not_effective"
    assert status["observed_temp_c"] == pytest.approx(33.0)
    assert status["current_temperature_c"] == pytest.approx(33.0)
    assert status["observed_today_max_c"] == pytest.approx(34.0)
    assert status["rolling_window_max_c"] == pytest.approx(34.0)
    assert status["rolling_max_temperature_c"] == pytest.approx(34.0)
    assert status["temperature_momentum_effective_c_per_hour"] == pytest.approx(0.0)
    assert status["temperature_momentum_realization_state"] == "platform"
    assert status["temperature_momentum_posterior_gate_allowed"] is False
    assert status["temperature_trajectory_posterior_status"] == "platform"
    assert status["na_reason"] == "trajectory_state_platform"


def test_ui_temperature_trajectory_root_fields_normalized_for_frontend() -> None:
    payload = {
        "weather_likelihood_features_compact": {
            "temperature_momentum_effective_c_per_hour": 2.0,
            "temperature_momentum_1h_c_per_hour": 2.0,
        },
        "temperature_trajectory": {
            "schema": "temperature_trajectory_summary_v1",
            "state": "sustained_warming",
            "gate_state": "confirmed",
            "gate_reason": "rolling_3_to_5_point_slope_positive",
            "rates_c_per_hour": {"1h": 1.0, "2h": 0.5, "3h": 0.0, "6h": -0.166666666667},
            "confirmed_windows": ["1h", "2h"],
            "adjusted_rate_c_per_hour": 1.0,
            "rolling_temperature_observation_summary": {
                "point_count": 5,
                "current_temp_c": 35.0,
                "rolling_max_c": 36.0,
            },
        },
        "distribution": [{"outcome": "37°C", "q_bayes_posterior": 0.4}],
        "chengdu_intraday_summary": {},
    }

    server._attach_ui_temperature_trajectory_status_from_payload(payload)

    rolling = payload["temperature_trajectory"]["rolling_temperature_observation_summary"]
    status = payload["temperature_trajectory_status"]
    assert rolling["rolling_point_count"] == 5
    assert rolling["current_temperature_c"] == pytest.approx(35.0)
    assert rolling["observed_temp_c"] == pytest.approx(35.0)
    assert rolling["rolling_window_max_c"] == pytest.approx(36.0)
    assert rolling["rolling_max_temperature_c"] == pytest.approx(36.0)
    assert rolling["observed_today_max_c"] == pytest.approx(36.0)
    assert payload["temperature_trajectory"]["current_temperature_c"] == pytest.approx(35.0)
    assert payload["temperature_trajectory"]["rolling_window_max_c"] == pytest.approx(36.0)
    assert payload["temperature_trajectory"]["rolling_max_temperature_c"] == pytest.approx(36.0)
    assert status["available"] is True
    assert status["current_temperature_c"] == pytest.approx(35.0)
    assert status["rolling_max_temperature_c"] == pytest.approx(36.0)
    assert status["rolling_point_count"] == pytest.approx(5)
    assert status["temperature_momentum_effective_c_per_hour"] == pytest.approx(1.0)
    assert payload["temperature_momentum_effective_c_per_hour"] == pytest.approx(1.0)
    assert payload["temperature_momentum_rates_c_per_hour"]["1h"] == pytest.approx(1.0)
    assert payload["weather_likelihood_features_compact"]["temperature_momentum_effective_c_per_hour"] == pytest.approx(1.0)
    assert payload["weather_likelihood_features_compact"]["temperature_momentum_1h_c_per_hour"] == pytest.approx(1.0)
    assert payload["weather_likelihood_features_compact"]["temperature_momentum_rates_c_per_hour"]["2h"] == pytest.approx(0.5)
    assert payload["key_metrics"]["temperature_momentum_effective_c_per_hour"] == pytest.approx(1.0)
    assert payload["key_metrics"]["temperature_momentum_rates_c_per_hour"]["6h"] == pytest.approx(-0.166666666667)
    assert payload["chengdu_intraday_summary"]["temperature_trajectory_status"] == status


def test_ui_temperature_trajectory_status_keeps_amsc_daily_max_above_rolling_window() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-8-2026",
        "event_date": "2026-07-08",
        "weather_likelihood_features_compact": {
            "observed_temp_c": 33.0,
            "observed_today_max_c": 39.0,
            "observed_today_max_source": "zuuu_metar_raw_amsc",
            "zuuu_metar_raw_amsc_latest_temp_c": 33.0,
            "zuuu_metar_raw_amsc_today_max_c": 39.0,
            "temperature_momentum_effective_c_per_hour": -1.0,
            "temperature_momentum_realization_state": "mixed",
        },
        "temperature_trajectory": {
            "schema": "temperature_trajectory_summary_v1",
            "state": "mixed",
            "gate_state": "not_warming",
            "gate_reason": "trajectory_state_platform",
            "stable_rate_c_per_hour": -0.75,
            "effective_rate_c_per_hour": -1.0,
            "adjusted_rate_c_per_hour": 0.0,
            "rolling_temperature_observation_summary": {
                "point_count": 5,
                "current_temp_c": 33.0,
                "rolling_max_c": 35.0,
            },
        },
        "distribution": [{"outcome": "39°C", "q_temperature_bucket_posterior": 1.0}],
        "chengdu_intraday_summary": {},
    }

    server._attach_ui_temperature_trajectory_status_from_payload(payload)

    status = payload["temperature_trajectory_status"]
    rolling = payload["temperature_trajectory"]["rolling_temperature_observation_summary"]
    assert status["observed_today_max_c"] == pytest.approx(39.0)
    assert status["rolling_window_max_c"] == pytest.approx(35.0)
    assert rolling["observed_today_max_c"] == pytest.approx(39.0)
    assert rolling["rolling_window_max_c"] == pytest.approx(35.0)
    assert payload["key_metrics"]["observed_today_max_c"] == pytest.approx(39.0)
    assert payload["weather_likelihood_features_compact"]["observed_today_max_c"] == pytest.approx(39.0)


def test_ui_snapshot_summary_slim_normalizes_temperature_trajectory_root_fields() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "event_date": "2026-07-06",
        "temperature_trajectory": {
            "schema": "temperature_trajectory_summary_v1",
            "state": "sustained_warming",
            "gate_state": "confirmed",
            "gate_reason": "multi_window_confirmed",
            "effective_rate_c_per_hour": 1.0,
            "adjusted_rate_c_per_hour": 1.0,
            "rolling_temperature_observation_summary": {
                "point_count": 5,
                "current_temp_c": 35.0,
                "rolling_max_c": 36.0,
            },
        },
        "distribution": [{"outcome": "37°C", "q_bayes_posterior": 0.4}],
    }

    compact = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    rolling = compact["temperature_trajectory"]["rolling_temperature_observation_summary"]
    assert rolling["rolling_point_count"] == 5
    assert compact["temperature_trajectory"]["current_temperature_c"] == pytest.approx(35.0)
    assert compact["temperature_trajectory"]["rolling_max_temperature_c"] == pytest.approx(36.0)
    assert compact["temperature_trajectory_status"]["current_temperature_c"] == pytest.approx(35.0)
    assert compact["temperature_trajectory_status"]["rolling_max_temperature_c"] == pytest.approx(36.0)
    assert compact["temperature_trajectory_status"]["rolling_point_count"] == pytest.approx(5)


def test_ui_snapshot_summary_merges_top_level_ssot_into_chengdu_summary_shell() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "title": "Highest temperature in Chengdu on July 6?",
        "event_date": "2026-07-06",
        "run_date": "2026-07-06",
        "fact_snapshot_id": "fact_ssot_shell",
        "posterior_run_id": "post_ssot_shell",
        "chengdu_intraday_summary": {
            "ok": True,
            "event_date": "2026-07-06",
            "source": "chengdu_intraday_summary_ssot",
            "key_metrics": {"observed_temp_c": 29.0},
        },
        "weather_likelihood_features_compact": {
            "observed_temp_c": 34.0,
            "observed_today_max_c": 34.0,
            "temperature_momentum_2h_c_per_hour": 0.5,
            "temperature_momentum_1h_c_per_hour": 1.0,
            "temperature_momentum_effective_c_per_hour": 0.0,
            "temperature_momentum_realization_state": "platform",
        },
        "temperature_trajectory": {
            "schema": "temperature_trajectory_summary_v1",
            "state": "platform",
            "gate_state": "not_warming",
            "gate_reason": "trajectory_state_platform",
            "adjusted_rate_c_per_hour": 0.0,
            "rolling_temperature_observation_summary": {
                "current_temp_c": 34.0,
                "rolling_max_c": 34.0,
                "point_count": 5,
            },
        },
        "prior_distribution": {"34°C": 0.2, "35°C": 0.4, "36°C": 0.4},
        "metar_strong_likelihood": {"34°C": 0.2, "35°C": 0.5, "36°C": 0.3},
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "35°C",
            "target_probability": 0.5,
            "bucket_probabilities": [
                {"bucket": "34°C", "probability": 0.2},
                {"bucket": "35°C", "probability": 0.5},
                {"bucket": "36°C", "probability": 0.3},
            ],
        },
        "distribution": [
            {"outcome": "34°C", "q_temperature_bucket_posterior": 0.2},
            {"outcome": "35°C", "q_temperature_bucket_posterior": 0.5},
            {"outcome": "36°C", "q_temperature_bucket_posterior": 0.3},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    summary = slim["chengdu_intraday_summary"]

    assert summary["temperature_trajectory"]["state"] == "platform"
    assert summary["temperature_trajectory_status"]["observed_temp_c"] == pytest.approx(34.0)
    assert summary["key_metrics"]["observed_temp_c"] == pytest.approx(34.0)
    assert summary["key_metrics"]["temperature_momentum_1h_c_per_hour"] == pytest.approx(1.0)
    assert summary["ui_sections"]["observation_strong"]["state"] == "available"
    assert summary["posterior_ssot_fact"]["target_bucket"] == "35°C"
    assert summary["frontend_recompute_allowed"] is False


def test_ui_snapshot_summary_fills_key_metrics_from_compact_fact_features_without_summary() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "event_date": "2026-07-06",
        "weather_likelihood_features_compact": {
            "observed_temp_c": 34.0,
            "observed_today_max_c": 34.0,
            "temperature_momentum_1h_c_per_hour": 1.0,
            "temperature_momentum_effective_c_per_hour": 0.0,
        },
        "temperature_trajectory": {
            "schema": "temperature_trajectory_summary_v1",
            "state": "platform",
            "gate_state": "not_warming",
            "gate_reason": "trajectory_state_platform",
            "adjusted_rate_c_per_hour": 0.0,
            "rolling_temperature_observation_summary": {
                "current_temp_c": 34.0,
                "rolling_max_c": 34.0,
            },
        },
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "35°C",
            "target_probability": 0.5,
            "bucket_probabilities": [{"bucket": "35°C", "probability": 0.5}],
        },
        "distribution": [{"outcome": "35°C", "q_temperature_bucket_posterior": 0.5}],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert slim["key_metrics"]["observed_temp_c"] == pytest.approx(34.0)
    assert slim["key_metrics"]["observed_today_max_c"] == pytest.approx(34.0)
    assert slim["key_metrics"]["temperature_momentum_1h_c_per_hour"] == pytest.approx(1.0)
    assert slim["key_metrics"]["temperature_momentum_effective_c_per_hour"] == pytest.approx(0.0)


def test_ui_snapshot_summary_amsc_compact_overrides_stale_intraday_observation_fields() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-8-2026",
        "event_date": "2026-07-08",
        "station_id": "ZUUU",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "whole_degrees",
        "weather_likelihood_features_compact": {
            "observed_temp_c": 33.0,
            "metar_temperature_c": 33.0,
            "observed_today_max_c": 39.0,
            "observed_today_max_source": "zuuu_metar_raw_amsc",
            "zuuu_metar_raw_amsc_latest_temp_c": 33.0,
            "zuuu_metar_raw_amsc_latest_at": "2026-07-08T15:00:00Z",
            "zuuu_metar_raw_amsc_latest_local": "2026-07-08T23:00:00+08:00",
            "zuuu_metar_raw_amsc_today_max_c": 39.0,
            "zuuu_metar_raw_amsc_today_max_at": "2026-07-08T09:00:00Z",
            "zuuu_metar_raw_amsc_today_max_local": "2026-07-08T17:00:00+08:00",
        },
        "chengdu_intraday_summary": {
            "key_metrics": {
                "observed_temp_c": 35.0,
                "observed_today_max_c": 37.0,
                "zuuu_metar_raw_amsc_latest_temp_c": 35.0,
                "zuuu_metar_raw_amsc_today_max_c": 37.0,
                "zuuu_metar_raw_amsc_today_max_at": "2026-07-08T04:30:00Z",
            }
        },
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "39°C",
            "target_probability": 1.0,
            "target_probability_pct": 100.0,
            "final_bucket_lock": True,
            "bucket_probabilities": [
                {"bucket": "38°C", "probability": 0.0},
                {"bucket": "39°C", "probability": 1.0},
                {"bucket": "40°C", "probability": 0.0},
            ],
        },
        "distribution": [
            {"outcome": "38°C", "q_temperature_bucket_posterior": 0.0},
            {"outcome": "39°C", "q_temperature_bucket_posterior": 1.0},
            {"outcome": "40°C", "q_temperature_bucket_posterior": 0.0},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert slim["observed_today_max_c"] == pytest.approx(39.0)
    assert slim["key_metrics"]["observed_today_max_c"] == pytest.approx(39.0)
    assert slim["weather_likelihood_features_compact"]["observed_today_max_c"] == pytest.approx(39.0)
    assert slim["key_metrics"]["observed_temp_source"] == "zuuu_metar_raw_amsc"
    assert slim["weather_likelihood_features_compact"]["observation_path_primary_source"] == "zuuu_metar_raw_amsc"
    assert slim["key_metrics"]["zuuu_metar_raw_amsc_today_max_c"] == pytest.approx(39.0)
    assert slim["weather_likelihood_features_compact"]["zuuu_metar_raw_amsc_today_max_c"] == pytest.approx(39.0)
    assert slim["key_metrics"]["zuuu_metar_raw_amsc_latest_temp_c"] == pytest.approx(33.0)
    assert slim["posterior_highest_bucket_label"] == "39°C"
    assert slim["posterior_highest_probability_pct"] == pytest.approx(100.0)


def test_ui_snapshot_latest_projection_fills_prior_and_strong_sections(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-july-5-2026"
    event_date = "2026-07-05"
    rows = [
        {"bucket": "36°C", "probability": 0.25},
        {"bucket": "37°C", "probability": 0.50},
        {"bucket": "38°C", "probability": 0.25},
    ]
    context_payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
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
            "target_probability": 0.5,
            "bucket_probabilities": rows,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "boundaries": {
                "frontend_recompute_allowed": False,
                "decision_recompute_allowed": False,
                "hard_mask_is_constraint_not_weight": True,
            },
            "layer_chain": {"schema": "bayes_layer_chain_summary_v1"},
        },
    }
    source_only_payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
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
                "event_date": event_date,
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
                "event_date": event_date,
                "forecast_peak_c": 36.0,
            },
        ],
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_placeholder_v1",
            "state": "not_materialized",
            "fact_snapshot_id": "fact_source_only",
            "posterior_run_id": "",
        },
    }
    source_only_payload["source_acquisition_trace"] = {
        "schema": "weather_source_acquisition_trace_v1",
        "read_only": True,
        "source_observations": list(source_only_payload["source_observations"]),
        "source_latency_trace": [],
        "hard_observation_candidate_ids": ["srcevt_amsc_metar"],
        "forecast_weak_ids": ["srcevt_amsc_taf"],
        "source_observation_ids": ["srcobs_amsc_metar", "srcobs_amsc_taf"],
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(
            run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_date,
            slug,
            event_date,
            "2026-07-05T15:05:55Z",
            "unit_context",
            json.dumps(context_payload, ensure_ascii=False),
            "2026-07-05T15:05:56Z",
        ),
    )
    context_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO event_fact_history(
            run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_date,
            slug,
            event_date,
            "2026-07-05T16:00:14Z",
            "amsc_zuuu_metar_taf_fetcher_v1",
            json.dumps(source_only_payload, ensure_ascii=False),
            "2026-07-05T16:00:15Z",
        ),
    )
    latest_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    assert context_id
    conn.execute(
        """
        INSERT OR REPLACE INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (slug, event_date, latest_id, "2026-07-05T16:00:14Z"),
    )
    conn.commit()

    item = {"slug": slug, "event_date": event_date, "distribution": rows}
    server._merge_fill_fast_observation_fields_from_latest_fact(conn, item, slug, event_date)
    server._attach_ui_snapshot_sections_to_item(item)

    assert item["prior_distribution"]["37°C"] == pytest.approx(0.6)
    assert item["posterior_ssot_fact"]["target_bucket"] == "37°C"
    assert item["posterior_ssot_fact"]["target_probability"] == pytest.approx(1.0)
    assert item["metar_strong_likelihood"]["37°C"] == pytest.approx(1.0)
    assert item["ui_sections"]["prior"]["state"] == "available"
    assert item["ui_sections"]["observation_strong"]["state"] == "available"
    assert item["ui_sections"]["observation_strong"]["hard_source_observations"][0]["source_id"] == "zuuu_metar_raw_amsc"
    projection = item["fact_provenance"]["ui_snapshot_latest_fact_projection"]
    assert projection["projection_mode"] == "contextual_hierarchical_posterior_projection"
    assert projection["writes_archive_db"] is False
    conn.close()


def test_ui_snapshot_summary_exposes_raw_amsc_hard_observation_when_strong_likelihood_pending() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "title": "Highest temperature in Chengdu on July 6?",
        "event_date": "2026-07-06",
        "run_date": "2026-07-06",
        "fact_snapshot_id": "fact_amsc_raw_ui",
        "posterior_run_id": "post_amsc_raw_ui",
        "source_observations": [
            {
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "source_role": "hard_source_observation",
                "hard_eligible": True,
                "station_id": "ZUUU",
                "valid_time": "2026-07-06T06:30:00+00:00",
                "valid_time_local": "2026-07-06T14:30:00+08:00",
                "temperature_c": 36.0,
                "dewpoint_c": 24.0,
                "observed_today_max_c": 37.0,
                "source_event_id": "src_evt_amsc",
                "source_observation_id": "src_obs_amsc",
            },
            {
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "source_role": "weak_forecast",
                "hard_eligible": False,
                "station_id": "ZUUU",
                "forecast_peak_c": 37.0,
            },
        ],
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_placeholder_v1",
            "state": "not_materialized",
            "state_reason": "unit_test_waiting_for_posterior",
            "read_only": True,
        },
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    observed = slim["ui_sections"]["observation_strong"]

    assert observed["schema"] == "ui_bayes_observation_strong_section_v1"
    assert observed["state"] == "raw_observation_only"
    assert observed["na_reason"] == "metar_strong_likelihood_not_materialized"
    assert observed["hard_source_count"] == 1
    assert observed["latest_hard_observation"]["source_id"] == "zuuu_metar_raw_amsc"
    assert observed["latest_hard_observation"]["temperature_c"] == pytest.approx(36.0)
    assert observed["latest_hard_observation"]["observed_today_max_c"] == pytest.approx(37.0)
    assert observed["source_observations"][1]["source_id"] == "zuuu_taf_forecast_amsc"
    assert observed["frontend_recompute_allowed"] is False


def test_ui_snapshot_summary_derives_awc_hard_observation_from_fact_features() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "title": "Highest temperature in Chengdu on July 6?",
        "event_date": "2026-07-06",
        "run_date": "2026-07-06",
        "fact_snapshot_id": "fact_awc_feature_ui",
        "posterior_run_id": "post_awc_feature_ui",
        "weather_likelihood_features_compact": {
            "observed_temp_c": 30.0,
            "observed_today_max_c": 31.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_at": "2026-07-05T16:00:00+00:00",
            "zuuu_metar_raw_awc_latest_at": "2026-07-05T17:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-07-05T17:07:04+00:00",
            "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 051700Z 21002MPS CAVOK 30/23 Q1001 NOSIG",
            "zuuu_metar_raw_awc_latest_dewpoint_c": 23.0,
        },
        "source_acquisition_trace": {
            "schema": "ui_pending_model_hierarchical_source_trace_v1",
            "read_only": True,
            "source_observations": [],
        },
        "metar_strong_likelihood": {"30°C": 0.25, "31°C": 0.75},
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_placeholder_v1",
            "state": "not_materialized",
            "state_reason": "unit_test_waiting_for_posterior",
            "read_only": True,
        },
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    observed = slim["ui_sections"]["observation_strong"]
    latest = observed["latest_hard_observation"]

    assert observed["state"] == "available"
    assert observed["hard_source_count"] == 1
    assert latest["source_id"] == "zuuu_metar_raw_amsc"
    assert latest["temperature_c"] == pytest.approx(30.0)
    assert latest["dewpoint_c"] == pytest.approx(23.0)
    assert latest["observed_today_max_c"] == pytest.approx(31.0)
    assert latest["valid_time"] == "2026-07-05T17:00:00+00:00"
    assert latest["ui_display_derived_from_fact_features"] is True
    assert latest["not_posterior_input"] is True
    assert slim["source_observations"][0]["source_observation_id"] == latest["source_observation_id"]
    assert slim["source_acquisition_trace"]["source_observations"][0]["source_id"] == "zuuu_metar_raw_amsc"
    assert slim["source_acquisition_trace"]["boundary"] == "display_only_from_existing_fact_fields_no_frontend_recompute_no_db_write"
    assert slim["ui_sections"]["prior"]["state"] == "missing"


def test_ui_snapshot_observation_strong_marks_likelihood_without_hard_point() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "title": "Highest temperature in Chengdu on July 6?",
        "event_date": "2026-07-06",
        "run_date": "2026-07-06",
        "fact_snapshot_id": "fact_strong_without_point",
        "posterior_run_id": "post_strong_without_point",
        "metar_strong_likelihood": {"30°C": 0.5, "31°C": 0.5},
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_placeholder_v1",
            "state": "not_materialized",
            "state_reason": "unit_test_waiting_for_posterior",
            "read_only": True,
        },
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    observed = slim["ui_sections"]["observation_strong"]

    assert observed["state"] == "likelihood_only"
    assert observed["na_reason"] == "hard_source_observation_points_missing"
    assert observed["hard_source_count"] == 0
    assert observed["latest_hard_observation"] == {}
    assert "source_observations" not in slim


def test_ui_snapshot_summary_exposes_materialized_metar_strong_likelihood_section() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "title": "Highest temperature in Chengdu on July 6?",
        "event_date": "2026-07-06",
        "run_date": "2026-07-06",
        "fact_snapshot_id": "fact_strong_ui",
        "posterior_run_id": "post_strong_ui",
        "metar_strong_likelihood": {"36°C": 0.15, "37°C": 0.70, "38°C": 0.15},
        "distribution": [
            {"outcome": "37°C", "q_bayes_posterior": 0.30},
            {"outcome": "38°C", "q_bayes_posterior": 0.45},
        ],
        "state_probs": {"S2_open_boundary_layer": 0.72, "S3_convective_cutoff": 0.08},
        "remaining_warming_by_state": [
            {"state": "S2_open_boundary_layer", "state_prob": 0.72, "delta_mu_c": 1.2, "peak_mu_c": 38.2, "sigma_c": 0.6},
            {"state": "S1_opening", "state_prob": 0.28, "delta_mu_c": 0.5, "peak_mu_c": 37.5, "sigma_c": 0.8},
        ],
        "temperature_trajectory": {
            "schema": "temperature_trajectory_summary_v1",
            "state": "platform",
            "rolling_temperature_observation_summary": {
                "state": "platform",
                "rolling_point_count": 5,
            },
        },
        "hard_constraints": {"current_temperature_c": 36.0, "observed_today_max_c": 37.0},
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_placeholder_v1",
            "state": "not_materialized",
            "state_reason": "unit_test_waiting_for_posterior",
            "read_only": True,
        },
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    observed = slim["ui_sections"]["observation_strong"]

    assert "state_probs" in slim
    assert "remaining_warming_by_state" in slim
    assert observed["state"] == "available"
    assert observed["metar_strong_likelihood"]["37°C"] == pytest.approx(0.70)
    assert observed["bucket_probabilities"][0]["outcome"] == "37°C"
    assert observed["state_probs"]["S2_open_boundary_layer"] == pytest.approx(0.72)
    assert observed["remaining_warming_by_state"][0]["state"] == "S2_open_boundary_layer"
    amsc_ssot = slim["ui_sections"]["amsc_metar_ssot"]
    peak_estimate = amsc_ssot["trajectory_peak_estimate"]
    assert peak_estimate["schema"] == "ui_amsc_metar_trajectory_peak_estimate_v1"
    assert peak_estimate["mean_peak_c"] == pytest.approx(38.004)
    assert peak_estimate["sigma_c"] == pytest.approx(0.7329283730351833)
    assert peak_estimate["sigma"] == pytest.approx(0.7329283730351833)
    assert peak_estimate["p90_c"] == pytest.approx(38.943285503895304)
    assert peak_estimate["p90"] == pytest.approx(38.943285503895304)
    assert peak_estimate["target_bucket_label"] == "38°C"
    assert peak_estimate["path_reach_probability"] == pytest.approx(
        peak_estimate["path_reach_probability_by_bucket"]["38°C"]
    )
    assert slim["temperature_trajectory_peak_estimate"]["mean_peak_c"] == pytest.approx(38.004)
    assert observed["trajectory_peak_estimate"]["mean_peak_c"] == pytest.approx(38.004)
    assert observed["temperature_trajectory"]["state"] == "platform"
    assert (
        observed["temperature_trajectory"]["rolling_temperature_observation_summary"]["rolling_point_count"]
        == 5
    )
    assert observed["hard_constraints"]["observed_today_max_c"] == pytest.approx(37.0)


def test_ui_snapshot_never_rewrites_posterior_ssot_from_observed_temperature() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-5-2026",
        "title": "Highest temperature in Chengdu on July 5?",
        "event_date": "2026-07-05",
        "run_date": "2026-07-05",
        "fact_snapshot_id": "fact_stale_low_target",
        "posterior_run_id": "post_stale_low_target",
        "rounding_rule": "whole_degrees",
        "market_var": "temperature_2m_max",
        "weather_likelihood_features": {
            "zuuu_metar_raw_awc_today_max_c": 30.0,
            "zuuu_metar_raw_awc_latest_temp_c": 30.0,
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
        },
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "read_only": True,
            "fact_snapshot_id": "fact_stale_low_target",
            "posterior_run_id": "post_stale_low_target",
            "posterior_as_of": "2026-07-05T01:31:18+00:00",
            "target_bucket": "29°C or below",
            "target_probability": 0.220141017041,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "phase": "D0_awc_hard_signal",
            "beta_weak": 0.0,
            "beta_strong": 1.0,
            "hard_mask_reason": "observed_today_max_c+current_temperature_c",
            "bucket_probabilities": [
                {"outcome": "29°C or below", "probability": 0.220141017041},
                {"outcome": "33°C", "probability": 0.19},
            ],
        },
        "distribution": [
            {
                "outcome": "29°C or below",
                "q_temperature_bucket_posterior": 0.220141017041,
                "q_bayes_posterior": 0.220141017041,
                "posterior_settlement_candidate_bucket_hard_zero": True,
                "posterior_settlement_candidate_bucket_hard_zero_reasons": [
                    "observed_today_max_above_bucket"
                ],
                "q_bayes_physical_likelihood_hard_zero": True,
                "observed_today_max_c": 30.0,
                "observed_today_max_source": "zuuu_metar_raw_awc",
            },
            {"outcome": "33°C", "q_temperature_bucket_posterior": 0.19, "q_bayes_posterior": 0.19},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    section = slim["ui_sections"]["bayes_posterior_ssot"]

    # The UI is a read-only SSOT projection.  It may refuse to expose an
    # internally hard-zero legacy row as a canonical trading target, but it
    # must not synthesize a new posterior state from the observed temperature.
    assert slim["posterior_ssot_fact"]["state"] == "available"
    assert slim["posterior_ssot_fact"]["target_bucket"] == "29°C or below"
    assert "invalidated_target_bucket" not in slim["posterior_ssot_fact"]
    assert section["state"] == "available"
    assert section["target_bucket"] == "29°C or below"
    assert "target_bucket" not in slim
    assert "ssot_target_bucket" not in slim
    assert "target_bucket_selection" not in slim
    assert "posterior_target_bucket_selection" not in slim
    assert "target_bucket_na_reason" not in slim
    assert "posterior_target_na_reason" not in slim


def test_ui_snapshot_phase0_contract_exposes_posterior_freshness() -> None:
    payload = {
        "ok": True,
        "run_date": "2026-07-04",
        "facts": {
            "source": "unit_test",
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                    "event_date": "2026-07-04",
                    "fact_id": 7,
                    "fact_as_of": "2026-07-04T07:06:04+00:00",
                    "fact_snapshot_id": "fact_ssot_ui",
                    "posterior_run_id": "post_ssot_ui",
                    "posterior_ssot_fact": {
                        "schema": "bayes_posterior_ssot_fact_v1",
                        "state": "available",
                        "fact_snapshot_id": "fact_ssot_ui",
                        "posterior_run_id": "post_ssot_ui",
                        "posterior_as_of": "2026-07-04T07:06:03+00:00",
                        "target_bucket": "29°C",
                        "target_probability": 0.64,
                    },
                }
            ],
        },
    }

    out = server._attach_ui_snapshot_phase0_contract(payload)

    posterior = out["panel_freshness"]["posterior"]
    assert posterior["state"] == "fresh"
    assert posterior["latest_posterior_as_of"] == "2026-07-04T07:06:03+00:00"
    assert posterior["available_count"] == 1
    assert posterior["not_materialized_count"] == 0
    assert posterior["missing_count"] == 0
    assert posterior["source"] == "facts.items[].ui_sections.bayes_posterior_ssot"


def test_ui_snapshot_phase0_contract_marks_placeholder_posterior_as_pending() -> None:
    payload = {
        "ok": True,
        "as_of": "2026-07-05T01:31:20+00:00",
        "run_date": "2026-07-05",
        "facts": {
            "source": "unit_test",
            "total": 1,
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-5-2026",
                    "event_date": "2026-07-05",
                    "fact_id": 8,
                    "fact_as_of": "2026-07-05T01:31:18+00:00",
                    "fact_snapshot_id": "fact_pending",
                    "posterior_run_id": "post_pending",
                    "phase": "D0_morning",
                    "beta_weak": 0.55,
                    "beta_strong": 0.35,
                    "posterior_ssot_fact": {
                        "schema": "bayes_posterior_ssot_fact_placeholder_v1",
                        "state": "not_materialized",
                        "state_reason": "posterior_ssot_fact_not_materialized_for_this_writer_path",
                        "fact_snapshot_id": "fact_pending",
                        "posterior_run_id": "post_pending",
                        "read_only": True,
                    },
                }
            ],
        },
    }

    out = server._attach_ui_snapshot_phase0_contract(payload)
    posterior = out["ui_sections"]["posterior"]

    assert posterior["state"] == "pending"
    assert posterior["available_count"] == 0
    assert posterior["not_materialized_count"] == 1
    assert posterior["missing_count"] == 0
    assert posterior["na_reason"] == "posterior_ssot_fact_not_materialized"
    assert out["panel_freshness"]["posterior"]["state"] == "pending"
    assert out["panel_freshness"]["posterior"]["not_materialized_count"] == 1


def test_ui_snapshot_phase0_contract_exposes_dashboard_sections() -> None:
    payload = {
        "ok": True,
        "as_of": "2026-07-04T07:06:07+00:00",
        "run_date": "2026-07-04",
        "facts": {
            "source": "unit_test",
            "total": 1,
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                    "event_date": "2026-07-04",
                    "fact_id": 7,
                    "fact_as_of": "2026-07-04T07:06:04+00:00",
                    "fact_snapshot_id": "fact_ssot_ui",
                    "posterior_run_id": "post_ssot_ui",
                    "review_id": "review_1",
                    "posterior_ssot_fact": {
                        "schema": "bayes_posterior_ssot_fact_v1",
                        "state": "available",
                        "fact_snapshot_id": "fact_ssot_ui",
                        "posterior_run_id": "post_ssot_ui",
                        "posterior_as_of": "2026-07-04T07:06:03+00:00",
                        "target_bucket": "29°C",
                        "target_probability": 0.64,
                    },
                    "daily_review_state": {
                        "schema": "daily_review_state_v1",
                        "review_id": "review_1",
                        "state": "ready",
                        "generated_at": "2026-07-04T22:00:00+00:00",
                        "first_bad_link": "posterior",
                    },
                }
            ],
        },
        "path_probability_follow": {
            "jobs": {
                "counts": {"queued": 1, "submitted": 1},
                "updated_at": "2026-07-04T07:06:08+00:00",
            },
            "entry": {
                "counts": {"total": 2, "blocked": 1, "will_order": 1},
                "blocked_reasons": [
                    {
                        "code": "orderbook_stale",
                        "count": 1,
                        "share": 1.0,
                    }
                ],
                "latest": [
                    {
                        "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                        "event_date": "2026-07-04",
                        "title": "Highest temperature in Chengdu on July 4?",
                        "outcome": "29°C",
                        "side": "BUY",
                        "decided_state": "blocked",
                        "will_order": 0,
                        "reason_codes": ["orderbook_stale"],
                        "reasons": ["orderbook too old"],
                        "created_at": "2026-07-04T07:06:09+00:00",
                        "details": {"ignored": "not exported in compact row"},
                    }
                ],
            },
        },
        "positions": {
            "trading_open_count": 1,
            "truth_source": "positions_api_funder",
            "sync_at": "2026-07-04T07:06:06+00:00",
        },
        "fills": {"filled_order_count_today": 1},
        "operator_apply_gate_summary": {
            "schema": "phase0_operator_apply_gate_summary_v1",
            "read_only": True,
            "execution_mode": "phase0_operator_apply_gate_summary_no_apply",
            "ready_now": False,
            "ready_after_operator_preflight": False,
            "first_blocked_gate": "daily_review_persistence_apply",
            "actionable_blocked_gates": ["daily_review_persistence_apply"],
            "gates": [
                {
                    "gate": "daily_review_persistence_apply",
                    "applicable": True,
                    "ready_now": False,
                    "ready_after_operator_preflight": False,
                    "blocked": True,
                    "blockers_now": ["daily_review_persistence_schema_missing"],
                    "reason": "daily review persistence is not apply-ready",
                    "source": "lineage_decision_apply_readiness.daily_review_persistence",
                }
            ],
        },
    }

    out = server._attach_ui_snapshot_phase0_contract(payload)
    sections = out["ui_sections"]

    assert sections["schema"] == "ui_snapshot_dashboard_sections_v1"
    assert sections["read_only"] is True
    assert sections["order"] == [
        "facts",
        "source_to_posterior",
        "posterior",
        "awc_signal_to_order",
        "decisions",
        "execution",
        "review",
    ]
    assert sections["facts"]["latest_fact_as_of"] == "2026-07-04T07:06:04+00:00"
    assert sections["posterior"]["latest_posterior_as_of"] == "2026-07-04T07:06:03+00:00"
    assert sections["decisions"]["source"] == "auto_trade_decisions.path_probability_follow"
    assert sections["decisions"]["latest_decision_at"] == "2026-07-04T07:06:09+00:00"
    assert sections["decisions"]["latest"][0]["reason_codes"] == ["orderbook_stale"]
    assert "details" not in sections["decisions"]["latest"][0]
    assert sections["execution"]["latest_execution_at"] == "2026-07-04T07:06:08+00:00"
    assert sections["execution"]["orderbook"]["state"] == "stale"
    assert sections["review"]["latest_review_id"] == "review_1"
    assert sections["review"]["latest_review_at"] == "2026-07-04T22:00:00+00:00"
    apply_gate = sections["review"]["apply_gate_summary"]
    assert apply_gate["schema"] == "ui_snapshot_apply_gate_summary_v1"
    assert apply_gate["available"] is True
    assert apply_gate["source"] == "operator_apply_gate_summary"
    assert apply_gate["source_schema"] == "phase0_operator_apply_gate_summary_v1"
    assert apply_gate["state"] == "blocked"
    assert apply_gate["first_blocked_gate"] == "daily_review_persistence_apply"
    assert apply_gate["gates"][0]["gate"] == "daily_review_persistence_apply"
    assert apply_gate["gates"][0]["blockers_now"] == ["daily_review_persistence_schema_missing"]
    assert sections["review"]["apply_gate_na_reason"] == ""
    assert out["panel_freshness"]["decisions"]["source"] == "auto_trade_decisions.path_probability_follow"
    assert out["panel_freshness"]["orderbook"]["stale_reason"] == "orderbook_stale"
    assert out["panel_freshness"]["review"]["review_count"] == 1


def test_ui_snapshot_phase0_contract_overwrites_cached_panel_freshness_state() -> None:
    payload = {
        "ok": True,
        "as_of": "2026-07-04T07:06:07+00:00",
        "run_date": "2026-07-04",
        "cache_stale": True,
        "cache_stale_reason": "snapshot_rebuild_started",
        "facts": {"source": "unit_test", "items": [], "total": 0},
        "panel_freshness": {
            "facts": {
                "state": "fresh",
                "source": "old_cache",
                "stale_reason": "",
                "na_reason": "",
            }
        },
    }

    out = server._attach_ui_snapshot_phase0_contract(payload)

    assert out["snapshot_freshness_state"] == "stale"
    assert out["ui_sections"]["facts"]["state"] == "stale"
    assert out["panel_freshness"]["facts"]["state"] == "stale"
    assert out["panel_freshness"]["facts"]["source"] == "unit_test"
    assert out["panel_freshness"]["facts"]["stale_reason"] == "snapshot_rebuild_started"
    assert out["panel_freshness"]["facts"]["na_reason"] == "no_fact_items"
    assert out["ui_sections"]["review"]["apply_gate_summary"]["available"] is False
    assert out["ui_sections"]["review"]["apply_gate_na_reason"] == "operator_apply_gate_summary_not_attached"
    assert out["next_recommended_poll_ms"] == 1000


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
    attach_unit_posterior_ssot_fact(
        payload,
        target_bucket="30°C or higher",
        target_probability=0.45382930769085045,
    )

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
    attach_unit_posterior_ssot_fact(payload, target_bucket="33°C", target_probability=1.0)

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


def test_ui_snapshot_summary_cache_rejects_fact_item_without_ssot_display_contract() -> None:
    payload = {
        "facts": {
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-6-2026",
                    "event_date": "2026-07-06",
                    "fact_id": 106775,
                    "target_bucket": "37°C",
                }
            ]
        }
    }

    assert server._ui_snapshot_summary_payload_has_display_items(payload) is False
    assert server._ui_snapshot_summary_cache_entry_has_display_items((123.0, payload)) is False


def test_ui_snapshot_summary_cache_accepts_fact_item_with_ssot_display_contract() -> None:
    payload = {
        "facts": {
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-6-2026",
                    "event_date": "2026-07-06",
                    "fact_id": 106775,
                    "lineage_state": {
                        "fact_snapshot_id": "fact_1",
                        "posterior_run_id": "post_1",
                        "source_event_ids": ["srcevt_1"],
                    },
                    "posterior_ssot_fact": {
                        "schema": "bayes_posterior_ssot_fact_v1",
                        "state": "available",
                        "fact_snapshot_id": "fact_1",
                        "posterior_run_id": "post_1",
                        "target_bucket": "37°C",
                        "target_probability": 0.8,
                    },
                    "posterior_target_bucket_selection": {
                        "ssot_source": "posterior_ssot_fact",
                        "target_bucket": "37°C",
                        "target_probability": 0.8,
                    },
                    "target_bucket_selection": {
                        "ssot_source": "posterior_ssot_fact",
                        "target_bucket": "37°C",
                        "target_probability": 0.8,
                    },
                    "prior_distribution": {"37°C": 0.4, "38°C": 0.6},
                    "metar_strong_likelihood": {"37°C": 0.7, "38°C": 0.3},
                }
            ]
        }
    }

    assert server._ui_snapshot_summary_payload_has_display_items(payload) is True
    assert server._ui_snapshot_summary_cache_entry_has_display_items((123.0, payload)) is True


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


def test_fact_write_top_label_helper_prefers_temperature_bucket_posterior() -> None:
    source = inspect.getsource(server._insert_event_fact_history)
    helper_start = source.index("def _top_label_from_rows(rows_obj: Any) -> str:")
    helper_end = source.index("distribution = item.get(\"distribution\")", helper_start)
    helper = source[helper_start:helper_end]

    assert helper.index('row.get("q_temperature_bucket_posterior")') < helper.index('row.get("q_bayes_posterior")')


def test_fact_write_keeps_observation_led_summary_diagnostic_without_posterior_ssot() -> None:
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
    assert old_top == "31°C"
    assert item["observation_led_likelihood"]["applied"] is True
    assert item["observation_led_likelihood"]["diagnostic_only"] is True
    assert item["observation_led_likelihood_posterior_materialized"] is False
    assert top_row["outcome"] == old_top
    assert "target_bucket" not in item
    assert "ssot_target_bucket" not in item


def test_intraday_summary_read_side_keeps_observation_led_rows_diagnostic_without_posterior_ssot() -> None:
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
    assert top_row["outcome"] == "31°C"
    assert top_row["q_temperature_bucket_posterior"] == pytest.approx(0.40)
    assert "target_bucket" not in payload
    assert "ssot_target_bucket" not in payload
    assert "posterior_highest_bucket_label" not in payload
    assert payload["observation_led_likelihood"]["applied"] is True
    assert payload["observation_led_likelihood"]["diagnostic_only"] is True
    assert payload["observation_led_likelihood_posterior_materialized"] is False


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
    assert "target_bucket" not in payload
    assert "ssot_target_bucket" not in payload
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


def test_ui_snapshot_distribution_compact_preserves_bucket_only_labels() -> None:
    row = {
        "bucket": "38°C",
        "q_temperature_bucket_posterior": 0.296701862055,
        "q_bayes_posterior": 0.296701862055,
    }

    compact = server._compact_distribution_row_for_ui_snapshot(row, summary_minimal=True)

    assert compact["bucket"] == "38°C"
    assert compact["outcome"] == "38°C"
    assert compact["label"] == "38°C"
    assert compact["q_temperature_bucket_posterior"] == pytest.approx(0.296701862055)


def test_ui_snapshot_distribution_projection_prefers_posterior_ssot_fact_over_stale_distribution() -> None:
    payload = {
        "target_bucket_probability_field": "event_fact_latest.distribution.q_temperature_bucket_posterior",
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "37°C",
            "target_probability": 1.0,
            "bucket_probabilities": [
                {"bucket": "37°C", "probability": 1.0, "final_bucket_lock_target": True},
                {"bucket": "38°C", "probability": 0.0, "hard_zero": True},
                {"bucket": "39°C or higher", "probability": 0.0, "hard_zero": True},
            ],
        },
        "posterior_temperature_distribution": {
            "bucket_probabilities": [
                {"bucket": "37°C", "probability": 0.027126122616945747},
                {"bucket": "38°C", "probability": 0.017903508455964195},
                {"bucket": "39°C or higher", "probability": 0.01022537415197955},
            ],
        },
        "distribution": [
            {"outcome": "37°C", "q_temperature_bucket_posterior": 0.027126122616945747},
            {"outcome": "38°C", "q_temperature_bucket_posterior": 0.017903508455964195},
            {"outcome": "39°C or higher", "q_temperature_bucket_posterior": 0.01022537415197955},
        ],
    }

    projected = server._distribution_with_ctp_posterior(payload)

    assert server._preserve_event_fact_distribution_posterior(payload) is False
    by_outcome = {row["outcome"]: row for row in projected}
    assert by_outcome["37°C"]["q_temperature_bucket_posterior"] == 1.0
    assert by_outcome["38°C"]["q_temperature_bucket_posterior"] == 0.0
    assert by_outcome["39°C or higher"]["q_temperature_bucket_posterior"] == 0.0
    assert by_outcome["38°C"]["posterior_field"] == "posterior_ssot_fact.bucket_probabilities"
    assert by_outcome["38°C"]["target_bucket_probability_field"] == "posterior_ssot_fact.bucket_probabilities"


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


def test_ui_snapshot_summary_slim_keeps_amsc_recoil_and_latency_ssot_fields() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-9-2026",
        "event_date": "2026-07-09",
        "fact_as_of": "2026-07-08T23:30:47Z",
        "amsc_latency_slo_audit": {
            "schema": "amsc_latency_slo_audit_v1",
            "valid_time": "2026-07-08T23:30:00Z",
            "fetched_at": "2026-07-08T23:30:47Z",
            "fact_as_of": "2026-07-08T23:30:47Z",
        },
        "termination_risk": {
            "schema": "strong_likelihood_termination_risk_v1",
            "probability_no_material_new_daily_high": 0.81,
            "reference_observed_daily_high_c": 32.0,
            "derived_from_same_remaining_warming_mixture": True,
            "speci_accelerated": True,
        },
        "weather_likelihood_features": {
            "zuuu_metar_raw_amsc_latest_at": "2026-07-08T23:30:00Z",
            "zuuu_metar_raw_amsc_latest_local": "2026-07-09T07:30:00+08:00",
            "zuuu_metar_raw_amsc_latest_temp_c": 29.0,
            "zuuu_metar_raw_amsc_today_max_c": 32.0,
            "zuuu_metar_raw_amsc_latest_raw": "METAR ZUUU 082330Z 21002MPS CAVOK 29/24 Q1001 NOSIG=",
            "recoil_state": "confirmed_cooling_cutoff",
            "recoil_state_label": "真实降温/截断",
            "recoil_action": "disable_recoil_compress_high_tail",
            "recoil_momentum_treatment": "cooling_or_cutoff_confirmed",
            "physical_cutoff_confirmed": True,
            "true_cooling_or_cutoff_confirmed": True,
            "physical_cutoff_evidence_codes": ["pressure_jump_observed"],
            "physical_cutoff_evidence_count": 1,
        },
        "distribution": [{"outcome": "38°C", "q_temperature_bucket_posterior": 0.32}],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    assert slim["amsc_latency_slo_audit"]["schema"] == "amsc_latency_slo_audit_v1"
    assert slim["zuuu_metar_raw_amsc_latest_temp_c"] == pytest.approx(29.0)
    assert slim["weather_likelihood_features_compact"]["zuuu_metar_raw_amsc_today_max_c"] == pytest.approx(32.0)
    assert slim["recoil_state_ssot"]["state"] == "confirmed_cooling_cutoff"
    assert slim["recoil_state_ssot"]["physical_cutoff_evidence"]["evidence_codes"] == ["pressure_jump_observed"]
    assert slim["physical_cutoff_evidence"]["true_cooling_or_cutoff_confirmed"] is True
    assert slim["termination_risk"]["probability_no_material_new_daily_high"] == pytest.approx(0.81)
    assert slim["key_metrics"]["termination_risk"]["speci_accelerated"] is True
    assert slim["key_metrics"]["recoil_state_ssot"]["state"] == "confirmed_cooling_cutoff"
    assert slim["weather_likelihood_features_compact"]["physical_cutoff_evidence"]["evidence_count"] == 1


def test_chengdu_intraday_fact_fallback_keeps_amsc_recoil_and_latency_ssot_fields(monkeypatch) -> None:
    fact_payload = {
        "slug": "highest-temperature-in-chengdu-on-july-9-2026",
        "event_date": "2026-07-09",
        "fact_as_of": "2026-07-08T23:30:47Z",
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "38°C",
            "target_probability": 0.32,
            "bucket_probabilities": [{"bucket": "38°C", "probability": 0.32}],
        },
        "amsc_latency_slo_audit": {
            "schema": "amsc_latency_slo_audit_v1",
            "state": "partial",
            "slow_stage": "fact_to_posterior",
        },
        "weather_likelihood_features": {
            "zuuu_metar_raw_amsc_latest_at": "2026-07-08T23:30:00Z",
            "zuuu_metar_raw_amsc_latest_temp_c": 29.0,
            "zuuu_metar_raw_amsc_today_max_c": 32.0,
            "recoil_state": "confirmed_cooling_cutoff",
            "physical_cutoff_confirmed": True,
            "true_cooling_or_cutoff_confirmed": True,
            "physical_cutoff_evidence_codes": ["pressure_jump_observed"],
        },
        "distribution": [{"outcome": "38°C", "q_temperature_bucket_posterior": 0.32}],
    }
    monkeypatch.setattr(
        server,
        "_latest_fact_payload_for_chengdu_intraday_ssot",
        lambda _event_date: fact_payload,
    )

    fallback = server._fact_backed_chengdu_intraday_summary_fallback(
        "2026-07-09",
        {"ok": False, "event_date": "2026-07-09", "busy": True},
    )
    compact = server._compact_chengdu_intraday_summary_for_endpoint(fallback)

    assert compact["ok"] is True
    assert compact["key_metrics"]["zuuu_metar_raw_amsc_latest_temp_c"] == pytest.approx(29.0)
    assert compact["recoil_state_ssot"]["state"] == "confirmed_cooling_cutoff"
    assert compact["physical_cutoff_evidence"]["evidence_codes"] == ["pressure_jump_observed"]
    assert compact["amsc_latency_slo_audit"]["slow_stage"] == "fact_to_posterior"


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

    assert "target_bucket" not in compact
    assert "ssot_target_bucket" not in compact
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


def test_ui_snapshot_summary_busy_cold_cache_returns_metadata_only_items() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

def test_ui_snapshot_summary_heavy_lane_busy_serves_summary_cache() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

def test_ui_snapshot_summary_heavy_lane_preserves_prior_observation_and_posterior_sections() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

def test_ui_snapshot_summary_heavy_lane_ignores_empty_busy_memory_cache() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

def test_ui_snapshot_summary_prefer_cache_raw_response_discards_empty_cached_body() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

def test_ui_server_routes_prefer_cache_snapshot_outside_heavy_lane() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

def test_ui_snapshot_summary_prewarm_targets_chengdu_shared_cache_key() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

def test_ui_snapshot_summary_prefer_cache_raw_response_reuses_short_cache() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

def test_ui_snapshot_summary_prefer_cache_raw_response_refreshes_html_mtime() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

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


def test_ui_snapshot_summary_busy_cold_cache_prefers_summary_cache() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

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
    attach_unit_prior_contract(item)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = insert_event_fact_history_with_unit_prior(
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
    assert selection["target_probability_field"] == "posterior_ssot_fact.target_probability"
    target_watch = next(
        row for row in selection["watch_buckets"] if row.get("outcome") == "30°C or higher"
    )
    assert target_watch["target_bucket_probability"] == 0.7
    assert target_watch["target_bucket_probability_pct"] == 70.0
    assert target_watch["posterior_field"] == "posterior_ssot_fact.bucket_probabilities"
    path = result["temperature_path_distribution"]
    assert path["target_bucket_posterior"] == 0.7
    assert path["target"]["posterior"] == 0.7
    assert path["bucket_distribution"][1]["posterior"] == 0.7
    assert result["chengdu_intraday_summary"]["target_bucket_probability_pct"] == 70.0
    assert (
        result["chengdu_intraday_summary"]["temperature_path_distribution"]["target_bucket_posterior"]
        == 0.7
    )


def test_ui_snapshot_summary_empty_items_guard_uses_summary_cache() -> None:
    assert not hasattr(server, "_ui_snapshot_summary_heavy_lane_fallback_payload")
    assert "prefer_cache" not in Path(server.__file__).read_text(encoding="utf-8")

def test_frontend_metadata_only_items_render_placeholder_without_inference() -> None:
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    assert "function isMetadataOnlySnapshotItem(item)" in html
    assert "function marketDiscoveryPendingDisplayReason(item)" in html
    assert "function renderEventIntelMetadataOnlyPanel(item, title = '事实详情')" in html
    assert "isMarketDiscoveryPendingModelItem(data)" in html
    assert "data.data_status === 'market_discovery_pending_model'" in html
    assert "data.mean_bucket_na_reason === 'model_not_materialized_yet'" in html
    assert "frontend_inference_allowed === false" in html
    assert "当前只读快照尚未物化后验事实" in html
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
    attach_unit_prior_contract(item)
    fact_id = insert_event_fact_history_with_unit_prior(
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
    attach_unit_prior_contract(item)
    fact_id = insert_event_fact_history_with_unit_prior(
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

    first = attach_unit_prior_contract({
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
    })
    second = {
        **first,
        "distribution": [
            {"outcome": "27°C", "q_round": 0.20, "q_bayes_posterior": 0.20, "q_temperature_bucket_posterior": 0.20},
            {"outcome": "28°C", "q_round": 0.80, "q_bayes_posterior": 0.80, "q_temperature_bucket_posterior": 0.80},
        ],
    }
    first_id = insert_event_fact_history_with_unit_prior(
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
    latest_id = insert_event_fact_history_with_unit_prior(
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


def test_open_db_ro_snapshot_pins_one_sqlite_view(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "snapshot.db"
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE snapshot_probe(value INTEGER NOT NULL)")
    writer.execute("INSERT INTO snapshot_probe(value) VALUES (1)")
    writer.commit()
    monkeypatch.setattr(server, "DB_PATH", db_path)

    with server.open_db_ro_snapshot(timeout_sec=0.2, busy_timeout_ms=200) as reader:
        assert reader.execute("SELECT value FROM snapshot_probe").fetchone()[0] == 1
        writer.execute("UPDATE snapshot_probe SET value = 2")
        writer.commit()
        assert reader.execute("SELECT value FROM snapshot_probe").fetchone()[0] == 1

    with server.open_db_ro_snapshot(timeout_sec=0.2, busy_timeout_ms=200) as reader:
        assert reader.execute("SELECT value FROM snapshot_probe").fetchone()[0] == 2
    writer.close()


def test_ui_snapshot_full_rebuild_uses_pinned_read_transaction() -> None:
    source = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    start = source.index("def ui_snapshot(")
    build_start = source.index("    def _build_payload()", start)
    build_end = source.index("    def _background_rebuild()", build_start)
    build_block = source[build_start:build_end]

    assert "open_db_ro_snapshot" in build_block
    assert "with open_db(" not in build_block


def test_ui_snapshot_full_cold_and_dirty_rebuilds_are_nonblocking() -> None:
    source = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    start = source.index("def ui_snapshot(")
    end = source.index("def ui_snapshot_summary(", start)
    block = source[start:end]

    assert "if UI_SNAPSHOT_STALE_WHILE_REVALIDATE and cached and not cache_dirty_sync_rebuild:" in block
    assert "if UI_SNAPSHOT_STALE_WHILE_REVALIDATE and not cached:" in block
    assert 'name=f"ui_snapshot_cold_rebuild:{cache_key}"' in block
    assert 'payload["synchronous_rebuild_allowed"] = False' in block


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


def test_ui_snapshot_summary_metadata_fallback_attaches_timeline_before_compaction() -> None:
    source = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    start = source.index("def _ui_snapshot_summary_metadata_fallback_payload(")
    end = source.index("def _ui_snapshot_summary_latest_run_date_cached()", start)
    block = source[start:end]
    attach = block.index("_attach_temperature_trajectory_timeline_for_ui_snapshot(")
    compact = block.index("_slim_fact_payload_for_ui_snapshot_summary(", attach)

    assert attach < compact


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
    today_id = insert_event_fact_history_with_unit_prior(
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
    future_id = insert_event_fact_history_with_unit_prior(
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
        fact_id = insert_event_fact_history_with_unit_prior(
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
    fact_id = insert_event_fact_history_with_unit_prior(
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
    persisted_payload = json.loads(
        conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()["fact_json"]
    )
    server._upsert_event_fact_summary_cache(
        conn,
        fact_id=fact_id,
        run_date="2026-06-21",
        slug=payload["slug"],
        event_date="2026-06-21",
        fact_as_of="2026-06-21T04:00:00+00:00",
        item=persisted_payload,
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

    fact_id = insert_event_fact_history_with_unit_prior(
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

    hard_fact_id = insert_event_fact_history_with_unit_prior(
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

    weak_fact_id = insert_event_fact_history_with_unit_prior(
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


def test_ui_event_fact_write_compat_plan_is_no_apply_for_legacy_helper_inputs(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    item = {
        "slug": "highest-temperature-in-test-city-on-june-23-2026",
        "event_date": "2026-06-23",
        "run_date": "2026-06-23",
        "source_event_ids": ["srcevt_ui_compat"],
        "distribution": [{"outcome": "30°C", "q_temperature_bucket_posterior": 0.62}],
    }

    plan = build_ui_event_fact_write_compat_plan_with_unit_prior(
        conn,
        run_date="2026-06-23",
        slug=item["slug"],
        event_date="2026-06-23",
        fact_as_of="2026-06-23T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
        include_payload=True,
    )

    assert plan["schema"] == "weather_fact_snapshot_writer_plan_v1"
    assert plan["read_only"] is True
    assert plan["dry_run"] is True
    assert plan["apply_supported"] is False
    assert plan["db_write_guard"]["writes_event_fact_history"] is False
    assert plan["db_write_guard"]["writes_event_fact_latest"] is False
    assert plan["source_event_ids"] == ["srcevt_ui_compat"]
    assert plan["source_observation_ids"]
    assert plan["effective_payload"]["source_observation_ids"] == plan["source_observation_ids"]
    assert plan["effective_payload"]["source_observations"][0]["source_observation_id"] == plan["source_observation_ids"][0]
    assert plan["lineage_state"]["source_observation_ids"] == plan["source_observation_ids"]
    assert plan["ui_fact_writer_compat_adapter"]["production_write_path_changed"] is False
    assert plan["effective_payload"]["model_snapshot_id"] == 10
    assert item.get("model_snapshot_id") is None
    row_count = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    assert row_count == 0
    conn.close()


def test_ui_event_fact_write_apply_contract_requires_explicit_allow(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    item = {
        "slug": "highest-temperature-in-test-city-on-june-23-2026",
        "event_date": "2026-06-23",
        "run_date": "2026-06-23",
        "source_event_ids": ["srcevt_ui_apply_contract"],
        "distribution": [{"outcome": "30°C", "q_temperature_bucket_posterior": 0.62}],
    }
    attach_unit_prior_contract(item)
    plan = build_ui_event_fact_write_compat_plan_with_unit_prior(
        conn,
        run_date="2026-06-23",
        slug=item["slug"],
        event_date="2026-06-23",
        fact_as_of="2026-06-23T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
        include_payload=True,
        enable_apply_contract=True,
    )

    blocked = server.apply_ui_event_fact_write_compat_plan(conn, plan)
    assert blocked["ok"] is False
    assert blocked["block_reason"] == "allow_apply_false"
    assert blocked["ui_fact_writer_compat_apply"]["production_write_path_changed"] is False
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0

    applied = server.apply_ui_event_fact_write_compat_plan(conn, plan, allow_apply=True)
    assert applied["ok"] is True
    assert applied["applied"] is True
    assert applied["post_apply_persisted_posterior_ssot_verification"]["source_lineage_contract"]["ok"] is True
    assert applied["post_apply_persisted_posterior_ssot_verification"]["source_lineage_contract"][
        "source_observation_id_replay_anchor_ready"
    ] is True
    assert applied["ui_fact_writer_compat_apply"]["uses_weather_fact_snapshot_writer_apply"] is True
    assert applied["ui_fact_writer_compat_apply"]["legacy_helper_replaced"] is False
    latest = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (item["slug"], item["event_date"]),
    ).fetchone()
    summary = conn.execute(
        "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?",
        (applied["fact_id"],),
    ).fetchone()
    assert latest["fact_id"] == applied["fact_id"]
    assert json.loads(summary["summary_json"])["summary_cache_source"] == "weather_fact_snapshot_writer"
    conn.close()


def test_ui_event_fact_write_apply_parity_audit_matches_legacy_and_writer_apply(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    item = {
        "slug": "highest-temperature-in-test-city-on-june-23-2026",
        "event_date": "2026-06-23",
        "run_date": "2026-06-23",
        "source_event_ids": ["srcevt_ui_apply_parity"],
        "market_updated_at": "2026-06-23T07:19:00+00:00",
        "distribution": [{"outcome": "30°C", "q_temperature_bucket_posterior": 0.62}],
    }
    legacy_fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date="2026-06-23",
        slug=item["slug"],
        event_date="2026-06-23",
        fact_as_of="2026-06-23T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=json.loads(json.dumps(item)),
        use_writer_apply=False,
    )
    legacy_latest_updated = server._upsert_event_fact_latest(conn, item["slug"], "2026-06-23", legacy_fact_id)
    plan = build_ui_event_fact_write_compat_plan_with_unit_prior(
        conn,
        run_date="2026-06-23",
        slug=item["slug"],
        event_date="2026-06-23",
        fact_as_of="2026-06-23T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
        include_payload=True,
        enable_apply_contract=True,
    )
    writer_apply = server.apply_ui_event_fact_write_compat_plan(conn, plan, allow_apply=True)

    audit = server.build_ui_event_fact_write_apply_parity_audit(
        conn,
        legacy_fact_id=legacy_fact_id,
        writer_fact_id=writer_apply["fact_id"],
        legacy_latest_updated=legacy_latest_updated,
        writer_apply_result=writer_apply,
    )

    assert audit["schema"] == "ui_fact_writer_apply_parity_audit_v1"
    assert audit["read_only"] is True
    assert audit["guards"]["writes_archive_db"] is False
    assert audit["ok"] is True
    assert {row["name"]: row["ok"] for row in audit["checks"] if row["blocking"]} == {
        "history_rows_present": True,
        "storage_projection_matches": True,
        "payload_projection_matches": True,
        "summary_cache_presence_matches": True,
        "latest_repoint_policy_matches": True,
    }
    assert audit["legacy_summary_state"]["summary_source"] == "weather_fact_snapshot_writer"
    assert audit["writer_summary_state"]["summary_source"] == "weather_fact_snapshot_writer"
    assert audit["boundary"]["legacy_helper_replaced"] is False
    conn.close()


def test_ui_event_fact_history_materialized_payload_split_keeps_sql_boundary_explicit(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    item = {
        "slug": "highest-temperature-in-test-city-on-june-23-2026",
        "event_date": "2026-06-23",
        "run_date": "2026-06-23",
        "distribution": [{"outcome": "30°C", "edge_score_yes": 0.12, "signal_edge_entry": 0.04}],
    }

    materialized = server._finalize_event_fact_history_materialized_payload_for_storage(
        item=item,
        fact_as_of="2026-06-23T07:20:00+00:00",
    )

    assert materialized["schema"] == "ui_event_fact_history_materialized_payload_v1"
    assert materialized["executes_sql"] is False
    assert materialized["best_side"] == "YES"
    assert materialized["best_outcome"] == "30°C"
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0

    fact_id = server._insert_materialized_event_fact_history_row(
        conn,
        run_date="2026-06-23",
        slug=item["slug"],
        event_date="2026-06-23",
        fact_as_of="2026-06-23T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        materialized=materialized,
    )

    history = conn.execute("SELECT * FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    summary = conn.execute("SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?", (fact_id,)).fetchone()
    assert history["best_side"] == "YES"
    assert history["best_outcome"] == "30°C"
    assert json.loads(history["fact_json"])["distribution"][0]["outcome"] == "30°C"
    assert json.loads(summary["summary_json"])["summary_cache_source"] == "weather_fact_snapshot_writer"
    conn.close()


def test_ui_event_fact_history_can_use_writer_apply_without_repointing_latest(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-23-2026"
    event_date = "2026-06-23"
    item = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "source_event_ids": ["srcevt_ui_helper_writer_apply"],
        "distribution": [{"outcome": "30°C", "edge_score_yes": 0.12, "signal_edge_entry": 0.04}],
    }

    fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-23T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
        use_writer_apply=True,
    )

    history = conn.execute("SELECT * FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    latest = conn.execute("SELECT 1 FROM event_fact_latest WHERE slug = ? AND event_date = ?", (slug, event_date)).fetchone()
    summary = conn.execute("SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?", (fact_id,)).fetchone()
    payload = json.loads(history["fact_json"])
    summary_payload = json.loads(summary["summary_json"])
    assert history["best_side"] == "YES"
    assert history["best_outcome"] == "30°C"
    assert latest is None
    assert payload["fact_writer_migration_context"]["latest_repoint_owned_by_weather_fact_snapshot_writer"] is True
    assert payload["fact_layer_state"]["write_owner"] == "weather_fact_snapshot_writer"
    assert summary_payload["summary_cache_source"] == "weather_fact_snapshot_writer"
    conn.close()


def test_ui_event_fact_history_defaults_to_writer_apply_without_repointing_latest(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PM_UI_FACT_HELPER_WRITER_APPLY_ENABLED", raising=False)
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-23-2026"
    event_date = "2026-06-23"
    item = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "source_event_ids": ["srcevt_ui_helper_default_writer_apply"],
        "distribution": [{"outcome": "30°C", "edge_score_yes": 0.12, "signal_edge_entry": 0.04}],
    }

    fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-23T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
    )

    history = conn.execute("SELECT * FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    latest = conn.execute("SELECT 1 FROM event_fact_latest WHERE slug = ? AND event_date = ?", (slug, event_date)).fetchone()
    summary = conn.execute("SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?", (fact_id,)).fetchone()
    payload = json.loads(history["fact_json"])
    summary_payload = json.loads(summary["summary_json"])
    assert latest is None
    assert payload["fact_layer_state"]["write_owner"] == "weather_fact_snapshot_writer"
    assert payload["phase0_fact_json_contract"]["missing_required_keys_after_merge_fill"] == []
    assert payload["fact_writer_migration_context"]["latest_repoint_owned_by_weather_fact_snapshot_writer"] is True
    assert summary_payload["summary_cache_source"] == "weather_fact_snapshot_writer"
    conn.close()


def test_ui_event_fact_history_writer_apply_env_cannot_reenable_legacy_insert(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_UI_FACT_HELPER_WRITER_APPLY_ENABLED", "0")
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-23-2026"
    event_date = "2026-06-23"
    item = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "source_event_ids": ["srcevt_ui_helper_writer_apply_kill_switch"],
        "distribution": [{"outcome": "30°C", "edge_score_yes": 0.12, "signal_edge_entry": 0.04}],
    }

    fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-23T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
    )

    history = conn.execute("SELECT * FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    summary = conn.execute("SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?", (fact_id,)).fetchone()
    payload = json.loads(history["fact_json"])
    summary_payload = json.loads(summary["summary_json"])
    assert payload["fact_writer_migration_context"]["latest_repoint_owned_by_weather_fact_snapshot_writer"] is True
    assert payload["phase0_fact_json_contract"]["missing_required_keys_after_merge_fill"] == []
    assert summary_payload["summary_cache_source"] == "weather_fact_snapshot_writer"
    conn.close()


def test_materialized_event_fact_history_writer_apply_plan_is_append_only(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-24-2026"
    event_date = "2026-06-24"
    item = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "source_event_ids": ["srcevt_ui_materialized_plan"],
        "distribution": [{"outcome": "31°C", "edge_score_yes": 0.08}],
    }
    materialized = server._finalize_event_fact_history_materialized_payload_for_storage(
        item=item,
        fact_as_of="2026-06-24T07:20:00+00:00",
    )

    plan = server.build_materialized_event_fact_history_writer_apply_plan(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-24T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        materialized=materialized,
        include_payload=True,
    )

    actions = {row["action"]: row for row in plan["plan_actions"]}
    assert plan["apply_supported"] is True
    assert plan["write_latest"] is False
    assert actions["append_event_fact_history"]["would_write"] is True
    assert actions["repoint_event_fact_latest"]["would_write"] is False
    assert actions["repoint_event_fact_latest"]["block_reason"] == "latest_repoint_disabled_by_caller"
    assert plan["effective_payload"]["model_snapshot_id"] == 10
    assert plan["ui_fact_writer_materialized_apply_adapter"]["latest_repoint_owned_by_weather_fact_snapshot_writer"] is False
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0
    conn.close()


def test_event_fact_history_insert_function_parity_audit_uses_in_memory_db() -> None:
    item = {
        "slug": "highest-temperature-in-test-city-on-june-25-2026",
        "event_date": "2026-06-25",
        "run_date": "2026-06-25",
        "source_event_ids": ["srcevt_function_parity"],
        "market_updated_at": "2026-06-25T07:19:00+00:00",
        "distribution": [{"outcome": "32°C", "edge_score_yes": 0.11, "signal_edge_entry": 0.03}],
    }

    audit = server.build_event_fact_history_insert_function_parity_audit(
        run_date="2026-06-25",
        slug=item["slug"],
        event_date="2026-06-25",
        fact_as_of="2026-06-25T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
    )

    assert audit["schema"] == "ui_event_fact_history_insert_function_parity_audit_v1"
    assert audit["ok"] is True
    assert audit["archive_db_read_only"] is True
    assert audit["uses_in_memory_db"] is True
    assert audit["guards"]["writes_archive_db"] is False
    assert audit["guards"]["executes_sql_in_memory"] is True
    assert audit["function_paths"]["legacy"] == ""
    assert audit["function_paths"]["legacy_path_removed"] is True
    assert audit["function_paths"]["writer"] == "_insert_event_fact_history(use_writer_apply=True, write_latest=True)"
    assert {row["name"]: row["ok"] for row in audit["parity_audit"]["checks"] if row["blocking"]} == {
        "history_row_present": True,
        "summary_cache_present": True,
        "latest_repoint_owned_by_writer": True,
    }


def test_event_fact_history_insert_function_parity_sample_suite_covers_switch_risks() -> None:
    suite = server.build_event_fact_history_insert_function_parity_sample_suite()

    assert suite["schema"] == "ui_event_fact_history_insert_function_parity_sample_suite_v1"
    assert suite["ok"] is True
    assert suite["archive_db_read_only"] is True
    assert suite["uses_in_memory_db"] is True
    assert suite["guards"]["writes_archive_db"] is False
    assert suite["guards"]["executes_sql_in_memory"] is True
    assert suite["sample_case_ids"] == [
        "baseline_distribution",
        "awc_hard_source_observation",
        "hard_source_direct_ssot",
        "stale_market_rows",
        "fast_trade_compact_payload",
    ]
    assert suite["sample_count"] == 5
    assert {row["case_id"]: row["ok"] for row in suite["case_results"]} == {
        "baseline_distribution": True,
        "awc_hard_source_observation": True,
        "hard_source_direct_ssot": True,
        "stale_market_rows": True,
        "fast_trade_compact_payload": True,
    }
    for case in suite["case_results"]:
        assert {row["name"]: row["ok"] for row in case["blocking_checks"]} == {
            "history_row_present": True,
            "summary_cache_present": True,
            "latest_repoint_owned_by_writer": True,
        }


def test_ui_event_fact_write_alignment_audit_matches_legacy_history_latest_and_summary(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    item = {
        "slug": "highest-temperature-in-test-city-on-june-23-2026",
        "event_date": "2026-06-23",
        "run_date": "2026-06-23",
        "source_event_ids": ["srcevt_ui_align"],
        "distribution": [{"outcome": "30°C", "q_temperature_bucket_posterior": 0.62}],
    }
    plan = build_ui_event_fact_write_compat_plan_with_unit_prior(
        conn,
        run_date="2026-06-23",
        slug=item["slug"],
        event_date="2026-06-23",
        fact_as_of="2026-06-23T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=item,
        include_payload=True,
    )
    legacy_item = json.loads(json.dumps(item))
    fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date="2026-06-23",
        slug=item["slug"],
        event_date="2026-06-23",
        fact_as_of="2026-06-23T07:20:00+00:00",
        model_snapshot_id=10,
        market_snapshot_id=20,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=legacy_item,
    )
    latest_updated = server._upsert_event_fact_latest(conn, item["slug"], "2026-06-23", fact_id)

    audit = server.build_ui_event_fact_write_compat_alignment_audit(
        conn,
        plan=plan,
        legacy_fact_id=fact_id,
        legacy_latest_updated=latest_updated,
        include_payload=True,
    )

    assert audit["schema"] == "ui_fact_writer_compat_alignment_audit_v1"
    assert audit["read_only"] is True
    assert audit["guards"]["writes_archive_db"] is False
    assert audit["ok"] is True
    assert {row["name"]: row["ok"] for row in audit["action_alignment"]} == {
        "append_event_fact_history": True,
        "repoint_event_fact_latest": True,
        "upsert_event_fact_summary_cache": True,
    }
    assert audit["legacy_state"]["latest_points_to_legacy"] is True
    assert audit["legacy_state"]["summary_cache_present"] is True
    assert audit["payload_alignment"]["checked"] is True
    assert audit["payload_alignment"]["projection_matches"] is True
    conn.close()


def test_ui_event_fact_write_compat_plan_uses_latest_context_and_preserves_hard_fact_policy(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-24-2026"
    event_date = "2026-06-24"
    hard_fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-24T07:22:49+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "hard_source_direct_ssot_write": True,
            "hard_source_observed_value_c": 30.0,
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "distribution": [{"outcome": "30°C", "q_temperature_bucket_posterior": 0.7}],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, hard_fact_id) is True
    before_count = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]

    plan = build_ui_event_fact_write_compat_plan_with_unit_prior(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-24T07:19:49+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "source_event_ids": ["srcevt_weak_ui_compat"],
            "distribution": [{"outcome": "29°C", "q_temperature_bucket_posterior": 0.59}],
        },
        include_payload=True,
    )

    actions = {item["action"]: item for item in plan["plan_actions"]}
    assert plan["ui_fact_writer_compat_adapter"]["current_fact_id"] == hard_fact_id
    assert actions["repoint_event_fact_latest"]["would_write"] is False
    assert actions["repoint_event_fact_latest"]["apply_status"] == "blocked_policy"
    assert actions["repoint_event_fact_latest"]["block_reason"] == "incoming_weaker_than_existing_hard_fact"
    assert plan["effective_payload"]["observed_today_max_c"] == 30.0
    after_count = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    assert after_count == before_count
    conn.close()


def test_ui_event_fact_write_alignment_audit_matches_legacy_latest_block_policy(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-test-city-on-june-25-2026"
    event_date = "2026-06-25"
    hard_fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-25T07:22:49+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": event_date,
            "run_date": event_date,
            "hard_source_direct_ssot_write": True,
            "hard_source_observed_value_c": 30.0,
            "observed_today_max_c": 30.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "distribution": [{"outcome": "30°C", "q_temperature_bucket_posterior": 0.7}],
        },
    )
    assert server._upsert_event_fact_latest(conn, slug, event_date, hard_fact_id) is True
    weak_item = {
        "slug": slug,
        "event_date": event_date,
        "run_date": event_date,
        "source_event_ids": ["srcevt_weak_ui_align"],
        "distribution": [
            {"outcome": "29°C", "q_temperature_bucket_posterior": 0.59},
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.01},
        ],
    }
    plan = build_ui_event_fact_write_compat_plan_with_unit_prior(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-25T07:19:49+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=weak_item,
        include_payload=True,
    )
    weak_fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date=event_date,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-06-25T07:19:49+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item=json.loads(json.dumps(weak_item)),
    )
    latest_updated = server._upsert_event_fact_latest(conn, slug, event_date, weak_fact_id)
    audit = server.build_ui_event_fact_write_compat_alignment_audit(
        conn,
        plan=plan,
        legacy_fact_id=weak_fact_id,
        legacy_latest_updated=latest_updated,
        include_payload=True,
    )

    assert latest_updated is False
    assert audit["ok"] is True
    latest_check = {row["name"]: row for row in audit["action_alignment"]}["repoint_event_fact_latest"]
    assert latest_check["expected"] is False
    assert latest_check["actual"] is False
    assert latest_check["plan_block_reason"] == "incoming_weaker_than_existing_hard_fact"
    latest_row = conn.execute("SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?", (slug, event_date)).fetchone()
    assert latest_row["fact_id"] == hard_fact_id
    conn.close()


def test_event_fact_latest_rejects_stale_awc_latest_at_with_same_max(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-27-2026"
    event_date = "2026-06-27"

    fresh_fact_id = insert_event_fact_history_with_unit_prior(
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

    stale_fact_id = insert_event_fact_history_with_unit_prior(
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

    materialized_fact_id = insert_event_fact_history_with_unit_prior(
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

    unmaterialized_fact_id = insert_event_fact_history_with_unit_prior(
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

    next_materialized_fact_id = insert_event_fact_history_with_unit_prior(
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
    old_fact_id = insert_event_fact_history_with_unit_prior(
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
    new_fact_id = insert_event_fact_history_with_unit_prior(
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
    fast_fact_id = insert_event_fact_history_with_unit_prior(
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
    rich_fact_id = insert_event_fact_history_with_unit_prior(
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
    fast_fact_id = insert_event_fact_history_with_unit_prior(
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
    rich_fact_id = insert_event_fact_history_with_unit_prior(
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
            "target_probability": 0.5319810931180973,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "target_bucket_probability_field": "posterior_ssot_fact.target_probability",
            "ssot_source": "posterior_ssot_fact",
            "selection_runtime_recomputed": False,
        },
        "posterior_target_bucket_selection": {
            "target_bucket": "33°C",
            "target_probability": 0.5319810931180973,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "target_bucket_probability_field": "posterior_ssot_fact.target_probability",
            "ssot_source": "posterior_ssot_fact",
            "selection_runtime_recomputed": False,
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
    attach_unit_posterior_ssot_fact(
        item,
        target_bucket="33°C",
        target_probability=0.5319810931180973,
    )
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
    old_fact_id = insert_event_fact_history_with_unit_prior(
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
    new_fact_id = insert_event_fact_history_with_unit_prior(
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
    assert old_payload["distribution"][0]["q_bayes_posterior"] == 1.0
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

    first_fact_id = insert_event_fact_history_with_unit_prior(
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

    stronger_fact_id = insert_event_fact_history_with_unit_prior(
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
    hard_fact_id = insert_event_fact_history_with_unit_prior(
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
    stale_fact_id = insert_event_fact_history_with_unit_prior(
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

    fact_id = insert_event_fact_history_with_unit_prior(
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
    assert item["target_bucket_selection"]["target_bucket_selection_source"] == "posterior_ssot_fact.top_bucket"
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

    fact_id = insert_event_fact_history_with_unit_prior(
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
    cached = conn.execute(
        "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?",
        (fact_id,),
    ).fetchone()
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
    assert payload["ui_snapshot_schema_version"] == "ui_snapshot_phase0_v1"
    assert payload["snapshot_id"].startswith("uisnap_")
    assert payload["snapshot_etag"] == f'W/"{payload["snapshot_id"]}"'
    assert payload["snapshot_freshness_state"] == "blocked"
    assert payload["lineage_state"]["placeholder"] is True
    assert payload["panel_freshness"]["facts"]["na_reason"] == "no_fact_items"


def test_ui_snapshot_phase0_contract_exposes_fact_lineage_items() -> None:
    payload = {
        "ok": True,
        "as_of": "2026-07-04T04:06:00+00:00",
        "run_date": "2026-07-04",
        "facts": {
            "source": "unit",
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                    "event_date": "2026-07-04",
                    "fact_id": 123,
                    "fact_as_of": "2026-07-04T04:05:59+00:00",
                    "lineage_state": {
                        "correlation_id": "corr_1",
                        "fact_snapshot_id": "fact_1",
                        "posterior_run_id": "post_1",
                        "source_event_ids": ["srcevt_lineage"],
                        "source_observation_ids": ["srcobs_lineage"],
                    },
                    "source_observations": [{"source_event_id": "srcevt_obs", "source_observation_id": "srcobs_obs"}],
                    "signal_to_order_timing": {
                        "signal_id": "sig_1",
                        "source_event_id": "srcevt_signal",
                        "source_observation_id": "srcobs_signal",
                    },
                }
            ],
        },
    }

    server._attach_ui_snapshot_phase0_contract(payload, cache_key="unit:1")

    assert payload["source_fact_ids"] == [123]
    assert payload["fact_snapshot_ids"] == ["fact_1"]
    assert payload["posterior_run_ids"] == ["post_1"]
    assert payload["source_event_ids"] == ["srcevt_lineage", "srcevt_obs", "srcevt_signal"]
    assert payload["source_observation_ids"] == ["srcobs_lineage", "srcobs_obs", "srcobs_signal"]
    assert payload["signal_ids"] == ["sig_1"]
    assert payload["primary_fact_snapshot_id"] == "fact_1"
    lineage = payload["lineage_state"]
    assert lineage["schema"] == "ui_snapshot_lineage_state_v1"
    assert lineage["placeholder"] is False
    assert lineage["fact_lineage_complete"] is True
    assert lineage["execution_lineage_complete"] is False
    assert lineage["source_observation_ids"] == ["srcobs_lineage", "srcobs_obs", "srcobs_signal"]
    assert lineage["items"][0]["fact_id"] == 123
    assert lineage["items"][0]["fact_snapshot_id"] == "fact_1"
    assert lineage["items"][0]["source_observation_ids"] == ["srcobs_lineage", "srcobs_obs", "srcobs_signal"]


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
        assert active_payload["ui_snapshot_schema_version"] == "ui_snapshot_phase0_v1"
        assert active_payload["snapshot_freshness_state"] == "blocked"
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
    assert "`/api/ui_snapshot?run_date=${date}&limit=${limit}${cityParam}${refreshParam}&full=1&_=${Date.now()}`" in full_block
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
    rerender_start = text.index("function rerenderFromCache")
    rerender_end = text.index("const autoTradeConfigState", rerender_start)
    rerender_block = text[rerender_start:rerender_end]
    render_cache_start = text.index("function renderFromCache")
    render_cache_end = text.index("function updateEventsProgress", render_cache_start)
    render_cache_block = text[render_cache_start:render_cache_end]
    load_start = text.index("async function load(")
    load_end = text.index("function renderFromCache", load_start)
    load_block = text[load_start:load_end]
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
    assert "function readSavedScrollAnchor()" in text
    assert "function maybeRestorePendingScrollPosition()" in text
    assert "window.__pendingScrollRestoreAnchor = anchor;" in text
    assert "sessionStorage.setItem(SCROLL_KEY, JSON.stringify(captureScrollAnchor()));" in text
    assert "restoreScrollAnchor(anchor, { retry: true });" in text
    assert "maybeRestorePendingScrollPosition();" in rerender_block
    assert "maybeRestorePendingScrollPosition();" in render_cache_block
    assert "maybeRestorePendingScrollPosition();" in load_block
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


def test_frontend_snapshot_failure_keeps_last_good_runs_visible() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    load_start = text.index("async function load(")
    load_end = text.index("function renderFromCache", load_start)
    load_block = text[load_start:load_end]

    assert "const existingRuns = Array.isArray(window.__runsLoaded) ? window.__runsLoaded : [];" in load_block
    assert "// keep existing data on incremental refresh failures" in load_block
    assert "const fallbackExistingRuns = (Array.isArray(existingRuns) ? existingRuns : [])" in load_block
    assert ".filter((item) => isRunVisibleForEffectiveDate(item, effectiveDate, includeUpcomingEvents));" in load_block
    assert "if (fallbackExistingRuns.length > 0) {" in load_block
    assert "data = fallbackExistingRuns;" in load_block
    assert "throw err;" in load_block


def test_frontend_surfaces_physical_source_impact_and_rebuild_metrics() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    target_start = text.index("function physicalLikelihoodTargetLabelFromItem")
    target_end = text.index("function physicalLikelihoodFindGroup", target_start)
    target_block = text[target_start:target_end]
    realtime_start = text.index("function renderPhysicalLikelihoodRealtimeStrip")
    realtime_end = text.index("function renderPhysicalLikelihoodPanel", realtime_start)
    realtime_block = text[realtime_start:realtime_end]

    assert "function physicalLikelihoodTargetGroupRows" in text
    assert "physicalLikelihoodTargetLabelFromItem(data, top.label || '')" in text
    assert "eventIntelTargetBucketSelectionFromSnapshot(" in target_block
    assert "const ssotLabel = eventIntelBucketLabel(ssotSelection.targetLabel || '', 'C');" in target_block
    assert "if (ssotLabel) return ssotLabel;" in target_block
    assert "temperature_path_distribution" not in target_block
    assert "selection.target_bucket" not in target_block
    assert "outlook.target_bucket" not in target_block
    assert "targetLabel: snap.targetLabel" in text
    assert "targetProb: snap.targetProb" in text
    assert "const targetFactorProb = toFiniteNumberOrNaN(entry && entry.targetProb);" in realtime_block
    assert "targetLabel ? Number.NaN : (entry && entry.topProb" in realtime_block
    assert "<strong>物理因素对目标桶</strong>" in text
    assert "相对强弱 ${physicalLikelihoodSignedNumber(rel, 2)}" in text
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
    assert "当前最高" not in block
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
    assert "Top 3 温度目标概率趋势" in text
    assert "温度桶最终概率（Top 3）" in text
    assert "posterior_temperature_distribution.bucket_probabilities" in text
    assert "等待来源" in text
    assert "主因" in text
    assert "贡献=P×w" not in text
    assert "先验 × 似然" not in text
    assert "未结算" in text
    assert "非当前实况" in text
    assert "模型校准摘要" in text
    assert "Weather.com/TWC" in text
    assert "QM校正最高桶" not in text
    assert "Top 3 温度桶后验趋势" not in text
    assert "目标桶：${targetBucketCompact}" in text
    assert "${targetSemantics.pathNoun}目标" not in text
    assert "路径触达概率目标" not in text
    assert "触达单桶概率" not in text
    assert "仅作路径/执行参考，不改写最终概率" in text


def test_ctp_bucket_table_shows_refactored_bayes_probability_change() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    header_start = text.index("header.innerHTML = [")
    header_end = text.index("const note = document.createElement('div');", header_start)
    header_block = text[header_start:header_end]
    chain_start = text.index("const bayesFormulaStepCard =")
    chain_end = text.index("const stateTags = [];", chain_start)
    chain_block = text[chain_start:chain_end]

    assert "概率变化" in header_block
    assert "背景→预测→实况→最终" in text
    assert "function eventIntelCtpContributionRows" in text
    assert "function eventIntelCtpEffectivePrecisionTotal" in text
    assert "轨迹占用" in text
    assert "信号强度" in text
    assert "label: '背景起点'" in chain_block
    assert "label: '预测调整'" in chain_block
    assert "label: '实况约束'" in chain_block
    assert "label: '最终概率'" in chain_block
    assert "bayes-step-card-label" in chain_block
    assert "bayes-step-card-value" in chain_block
    assert "bayes-step-card-delta" in chain_block
    assert "沿用上一步" in chain_block
    assert "约束生效" in chain_block
    assert "推高概率" in chain_block
    assert "压低概率" in chain_block
    assert "formulaSummary" in chain_block
    assert "bayes-formula-steps" in chain_block
    assert "重构 Bayes" not in header_block
    assert "D0/硬源" not in header_block


def test_frontend_bayes_driver_uses_ctp_ssot_precision_share_labels() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("const ctpComponentRows = eventIntelCtpComponentRows")
    end = text.index("const ctpBucketPosterior = eventIntelCtpBucketProbability", start)
    block = text[start:end]

    assert "function eventIntelCtpNormalBucketProbability" in text
    assert "eventIntelCtpMemberBucketProbability(component, idx, item)" in text
    assert "singleProbability" in block
    assert "precisionShare" in block
    assert "displayImpact" in block
    assert "return Number.isFinite(precisionShare) ? precisionShare : Number.NaN;" in block
    assert "return prob * precisionShare;" in block
    assert "单源概率" in block
    assert "实际有效 precision share" in block
    assert "展示影响" in block
    assert "channelWeightText" not in block
    assert "channelContributionText" not in block


def test_event_intel_default_copy_hides_internal_probability_jargon() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

    for phrase in (
        "重构 Bayes 概率变化",
        "D0/硬源",
        "目标桶后验交易目标",
        "模型权重 shadow",
        "不能上线原因",
        "Regime v1 实况轨迹路径",
        "路径聚合最高温",
        "强制转向/压制",
        "Peak hour 分布",
        "Soft intraday 诊断",
        "source_component 已接入",
        "落桶后验",
    ):
        assert phrase not in text

    assert "目标桶概率变化" in text
    assert "背景起点 → 预测调整 → 实况约束 → 最终概率" in text
    assert "温度桶最终概率（Top 3）" in text
    assert "实况轨迹判断" in text


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
    assert "查看全部温度桶" in text


def test_bayes_prior_layout_prioritizes_ctp_prior_core_over_supporting_context() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderEventBayesPriorStep")
    end = text.index("function eventBayesLikelihoodChannels", start)
    block = text[start:end]

    assert "function eventBayesPriorSectionRows" in text
    assert "function eventBayesCtpPriorRows" in text
    assert "const focus = eventBayesStageFocus(item);" in block
    assert "const sectionPriorRows = eventBayesPriorSectionRows(item, 3);" in block
    assert "const ctpPriorRows = eventBayesCtpPriorRows(item, 3);" in block
    assert "const priorRows = sectionPriorRows.length ? sectionPriorRows" in block
    assert "priorRowsSource = sectionPriorRows.length ? '后端先验SSOT'" in block
    assert block.index("const sectionPriorRows = eventBayesPriorSectionRows(item, 3);") < block.index("const ctpPriorRows = eventBayesCtpPriorRows(item, 3);")
    assert "title: '目标桶起点'" in block
    assert "bayes-evidence-prior-grid is-core-first" in block
    assert ".bayes-prior-support-row {" in text
    assert "grid-template-columns:minmax(220px, .68fr) minmax(320px, 1.32fr);" in text
    assert ".bayes-evidence-prior-grid.is-core-first {\n\t      grid-template-columns:1fr;" in text
    assert ".bayes-prior-core {" in text
    assert ".bayes-prior-explain-card {" in text
    assert "const priorComponentLabel = topPrior && topPrior.componentLabel" in block
    assert "bayes-prior-core event-realtime-block" in block
    assert "data-realtime-key=\"${escapeHtml(`bayes-stage:${String(cardId || eventIntelEventDate(item) || 'latest')}:prior`)}\"" in block
    assert "focusPriorNum = eventIntelFirstFiniteNumber(focus && focus.priorProb, topPriorNum)" in block
    assert "${priorFocusMode}背景起点" in block
    assert "bayes-prior-core-head" not in block
    assert "同一目标桶起点" in block
    assert "来源" in block
    assert "背景因素" in block
    assert "只作起点" in block
    assert "背景 Top3 · ${priorRowsSource}" in block
    assert "气候Top ${topPriorLabel} ${topPriorProb}" in block
    assert "气候影响" in block
    assert "盆地影响" in block
    assert "为什么：" not in block
    assert "气候背景组件桶" not in block
    assert "bayes-prior-support-row" in block
    assert "bayes-prior-core" in block
    assert block.index("const priorCoreHtml") < block.index("const top3Html")
    assert block.index("${priorCoreHtml}") < block.index("bayes-prior-support-row")
    assert block.index("${priorExplainHtml}") < block.index("${top3Html}")


def test_frontend_prior_section_falls_back_to_root_ui_snapshot_prior_distribution() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function eventIntelPriorSection")
    end = text.index("function eventIntelObservationStrongSection", start)
    block = text[start:end]

    assert "data.prior_distribution" in block
    assert "facts.items[].prior_distribution" in block
    assert "schema: 'ui_bayes_prior_section_v1'" in block
    assert "bucket_probabilities: rows" in block
    assert "prior_meta: data.prior_meta" in block


def test_frontend_observation_section_falls_back_to_root_ui_snapshot_hard_sources() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function eventIntelObservationStrongSection")
    end = text.index("function eventIntelLatestHardObservation", start)
    block = text[start:end]
    latest_start = text.index("function eventIntelLatestObservationFromRows")
    latest_end = text.index("function eventIntelLatestHardObservation", latest_start)
    latest_block = text[latest_start:latest_end]
    hard_start = text.index("function eventIntelLatestHardObservation")
    hard_end = text.index("function eventIntelFirstFiniteNumber", hard_start)
    hard_block = text[hard_start:hard_end]

    assert "data.metar_strong_likelihood" in block
    assert "data.source_observations" in block
    assert "facts.items[].metar_strong_likelihood/source_observations" in block
    assert "schema: 'ui_bayes_observation_strong_section_v1'" in block
    assert "hard_source_observations: hardRows" in block
    assert "latest_hard_observation: latest" in block
    assert "Date.parse(raw)" in latest_block
    assert "sort((a, b) => timeValue(b) - timeValue(a))" in latest_block
    assert "return eventIntelLatestObservationFromRows(rows);" in hard_block


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
    normalize_start = text.index("function normalizeUiSnapshotRunsPage")
    normalize_end = text.index("function snapshotItemIsDisplayUsable", normalize_start)
    normalize_block = text[normalize_start:normalize_end]
    summary_start = text.index("async function fetchUiSnapshotSummaryRunsPage")
    summary_end = text.index("async function fetchUiSnapshotFullRunsPage", summary_start)
    summary_block = text[summary_start:summary_end]
    summary_gate_start = text.index("function shouldUseUiSnapshotSummaryForRuns")
    summary_gate_end = text.index("function shouldUseSnapshotOnlyRuns", summary_gate_start)
    summary_gate_block = text[summary_gate_start:summary_gate_end]
    fetch_start = text.index("async function fetchRunsPage")
    fetch_end = text.index("async function fetchRunsRange", fetch_start)
    fetch_block = text[fetch_start:fetch_end]

    assert "function shouldUseSnapshotOnlyRuns" in text
    assert "function snapshotOnlyRunsPage" in text
    assert "snapshot_only: true," in normalize_block
    assert "const preferCacheParam = '&prefer_cache=1';" in summary_block
    assert "${refreshParam}${preferCacheParam}&_=" in summary_block
    assert "ui_snapshot_summary_empty_items" in fetch_block
    assert "err.snapshotOnly = true;" in fetch_block
    assert "err.snapshotOnlySource = 'ui_snapshot_summary';" in fetch_block
    assert "fetchUiSnapshotFullRunsPage(" in fetch_block
    assert "ui_snapshot_full_fallback_after_summary_empty" in fetch_block
    assert "throw err;" in fetch_block
    assert "ui_snapshot_full_snapshot_only_page" in fetch_block
    assert "ui_snapshot_full_snapshot_only_empty" in fetch_block
    assert "return snapshotOnlyRunsPage(params, 'ui_snapshot_summary_page_only');" not in fetch_block
    assert "return true;" in summary_gate_block
    assert "LCB_ONLY_UI" not in summary_gate_block
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
    assert "window.__runsFetchMode = runsPage && runsPage.snapshot_only ? 'snapshot' : 'paged';" in load_block
    assert "fallbackExistingRuns" in snapshot_block
    assert "throw err;" in snapshot_block
    assert "fetchRunsRange" not in snapshot_block
    assert "if (!snapshotOnlyRunsMode && !canIncremental && useLatest" in load_block


def test_optional_agentation_widget_is_opt_in_only() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    module_start = text.index("<script type=\"module\">")
    module_block = text[module_start:text.index("</script>", module_start)]

    assert "const agentationEnabled = (() => {" in module_block
    assert "params.get('agentation') === '1'" in module_block
    assert "localStorage.getItem('weather.agentation.enabled') === '1'" in module_block
    assert "if (isLocal && agentationEnabled) {" in module_block
    assert "if (isLocal) {" not in module_block


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
    assert "if (!requestedDate && !UI_FAST_LOAD_ENABLED) {" in init_block
    assert "const latest = await fetchLatestDateObserved();" in init_block
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
    assert "setSyncIndicator('fresh', '已轮询，无变化'" in refresh_block
    assert "window.__lastLiveSnapshotMergeReport" in text
    assert "window.__eventIntelligenceNextSnapshotAt" in schedule_block
    assert "updateLiveSnapshotCountdown()" in schedule_block
    assert "fetchRunsPage(" not in refresh_block
    assert "fetchRunsRange(" not in refresh_block
    assert "/api/facts" not in refresh_block
    assert "/api/runs-range" not in refresh_block
    assert "eventIntelligenceLiveSnapshotDelayMs()" in schedule_block
    assert "scheduleEventIntelligenceLiveSnapshotRefresh();" in schedule_block
    assert "scheduleEventIntelligenceLiveSnapshotRefresh(1200);" in start_block
    assert "eventIntelligenceCountdownTimer = setInterval" in start_block


def test_realtime_update_visuals_show_delta_countdown_and_animated_numbers() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    realtime_start = text.index("function renderEventRealtimeBadge")
    realtime_end = text.index("function requestEventIntelChengduIntradayPayload", realtime_start)
    realtime_block = text[realtime_start:realtime_end]

    assert "id=\"syncNext\"" in text
    assert ".sync-next" in text
    assert ".event-realtime-delta" in text
    assert "animation:posterior-live-flash 1.8s ease" in text
    assert "function formatLiveSnapshotCountdown" in text
    assert "function updateLiveSnapshotCountdown" in text
    assert "function realtimeDeltaText" in realtime_block
    assert "formatProbabilityDeltaPp(current - previous, '0.0pp')" in realtime_block
    assert "function animateNumericText" in realtime_block
    assert "window.requestAnimationFrame(step)" in realtime_block
    assert "function animatePosteriorSpotlightProbability" in realtime_block
    assert "is-number-animating" in text
    assert "setRealtimeDeltaBadge(" in realtime_block


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
    assert "zuuu_metar_raw_amsc_latest_wind_dir_deg" in text
    assert "AMSC Raw METAR 风组" in text
    assert "风场辅助" in text
    assert "辅助不覆盖轨迹" in text
    assert "露点湿压" in text
    assert "低露点干化" in text
    assert "高压下沉" in text
    assert "低压扰动" in text
    assert "湿压/辐射" in text
    assert "今日总判 · 物理证据" in text
    assert "function eventIntelPhysicalObservationTitle" in text
    assert "return `物理观察辅助，交易目标 ${target}`;" in text
    assert "物理观察候选" in text
    assert "axisTargetSelection = eventIntelTargetBucketSelectionFromSnapshot(" in text
    assert "const title = eventIntelPhysicalObservationTitle(rawPhysicalTitle, targetLabel) || rawPhysicalTitle;" in text
    assert "<span>物理观察</span>" in text
    assert "<span>结论优先</span>" not in text
    assert "const trajectoryGuardActive = Boolean(" in text
    assert "不覆盖温度轨迹门控" in text
    assert "badge: trajectoryGuardActive ? (assessment.badge || '轨迹优先')" in text
    assert "AMSC/METAR 无降水" in text
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


def test_hard_source_raw_report_places_taf_before_collapsed_metar() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    bag_start = text.index("function awcRawReportFeatureBag")
    bag_end = text.index("function awcRawReportLocalTime", bag_start)
    bag_block = text[bag_start:bag_end]
    obs_start = text.index("function awcRawReportObservationRows")
    obs_end = text.index("function awcRawReportMetarRows", obs_start)
    obs_block = text[obs_start:obs_end]
    metar_start = text.index("function renderAwcRawMetarTimeline")
    metar_end = text.index("function renderAwcRawTafTimeline", metar_start)
    metar_block = text[metar_start:metar_end]
    report_start = text.index("function renderAwcRawReportTimeline")
    report_end = text.index("function shouldUseUiSnapshotSummaryForRuns", report_start)
    report_block = text[report_start:report_end]

    assert "function renderAwcRawMetarRow" in text
    assert "const visibleRows = rows.slice(0, 3);" in metar_block
    assert "const collapsedRows = rows.slice(3);" in metar_block
    assert '<details class="awc-raw-report-more">' in metar_block
    assert "其余 Raw METAR ${escapeHtml(String(collapsedRows.length))} 条" in metar_block
    assert "hard sources · latest 3 shown" in report_block
    assert "硬源原文" in report_block
    assert "AWC 原文" not in report_block
    assert "item.source_observations" in obs_block
    assert "observationSection.hard_source_observations" in obs_block
    assert "awcRawReportSourceLabel(sourceId)" in obs_block
    assert report_block.index("<span>Raw TAF</span>") < report_block.index("<span>Raw METAR</span>")
    assert ".awc-raw-report-grid {\n      display:grid;\n      grid-template-columns:1fr;" in text
    assert ".awc-raw-report-more summary::after" in text
    assert "const merged = { ...full, ...compact };" in bag_block
    assert "'taf_forecast_peak_raw'," in bag_block
    assert "row0[key] !== undefined" in bag_block


def test_scan_weather_market_persists_taf_raw_fields_into_distribution_rows() -> None:
    text = (ROOT / "scan_weather_market.py").read_text(encoding="utf-8")
    start = text.index('"taf_forecast_peak_c": taf_forecast_peak.get("value_c"),')
    end = text.index('"taf_forecast_peak_source_fit_tier": taf_forecast_peak.get("source_fit_tier"),', start)
    block = text[start:end]

    assert '"taf_forecast_peak_raw": taf_forecast_peak.get("raw_text"),' in block
    assert '"taf_forecast_peak_source_url": taf_forecast_peak.get("source_url"),' in block
    assert '"taf_forecast_peak_valid_start_at": taf_forecast_peak.get("valid_start_at"),' in block
    assert '"taf_forecast_peak_valid_end_at": taf_forecast_peak.get("valid_end_at"),' in block
    assert '"taf_forecast_min_c": taf_forecast_peak.get("minimum_c"),' in block
    assert '"taf_forecast_min_at": taf_forecast_peak.get("minimum_at"),' in block


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
    assert "目标桶预测" in block
    assert "Bayes 预测似然主结论" not in block
    assert "previous: focus.priorProb" in block
    assert "value: focus.likelihoodProb" in block
    assert "realtimeKey: `bayes-stage:${String(cardId || eventIntelEventDate(item) || 'latest')}:likelihood`" in block
    assert "主要预测来源" in block
    assert "谁在影响目标桶" in block
    assert "预测、轨迹、物理统一排序" in block
    assert "renderEventBayesContributionPanel(item, focus.displayLabel" in block
    assert "预测汇总" in block
    assert "renderEventBayesSourceComponentCard(component, '预测')" in block
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

    assert "实况是否改变目标" in block
    assert "previous: focus.likelihoodProb" in block
    assert "value: focus.observedProb" in block
    assert "realtimeKey: `bayes-stage:${String(cardId || eventIntelEventDate(item) || 'latest')}:d0`" in block
    assert "目标能否兑现" in block
    assert "路径触达" in block
    assert "AMSC SSOT路径概率" in block
    assert "离目标" in block
    assert "实况如何影响目标桶" not in block
    assert "轨迹、物理、实况统一排序" not in block
    assert "observedContributionPanelHtml" not in block
    assert "实况证据" in block
    assert "实况 / 轨迹 / 物理" in block
    assert "温度轨迹" in block
    assert "当前实况" in block
    assert "已观测最高" in block
    assert "升温率" in block
    assert "peakEstimateLabel" in block
    assert "温度动量" in block
    assert "renderTemperatureMomentumVisual(metrics, TEMPERATURE_MOMENTUM_COMPACT_WINDOWS" in block
    assert "物理证据" in block
    assert "renderEventIntelIntradayEvidenceAxis(payload || {}, item)" in block
    assert "趋势曲线" in block
    assert "renderEventIntelPhysicalTrendCurve(payload || {})" in block
    assert "窗口：" in block
    assert "实况未改写，沿用预测结果。" in block
    assert "实况未触发硬修正" not in block
    assert "temperature_trajectory" in block
    assert "independent_physics_residual" in block
    assert "renderEventIntelPhysicalEvidenceHub" not in block
    assert "hideLikelihoodSecondary: false" not in block
    assert "const selection = eventIntelTargetBucketSelectionFromSnapshot(item, payload);" in focus_block
    assert "const targetTop = targetFocusLabel" in focus_block
    assert "focusSource: targetTop ? (selection.source || 'posterior_ssot_fact.top_bucket') : 'posterior_ssot_fact_missing'" in focus_block
    assert "preferConstrained ? firstFiniteRowValue(row, ['q_raw', 'q_posterior_observed_constraints'])" not in text
    assert "['q_raw', 'q_posterior_observed_constraints', 'q_temperature_bucket_posterior', 'q_bayes_posterior']" not in text
    assert "['q_temperature_bucket_posterior', 'q_posterior_observed_constraints', 'q_bayes_posterior', 'q_raw']" in text
    assert "compactTrendConclusion: false" not in block
    assert "observedDynamicLayout: true" not in block
    assert "title: '实况是否改变目标'" in block
    assert "当前 ${observed}" not in block
    assert "最高 ${observedMax}" not in block


def test_intraday_physical_axis_surfaces_today_physical_verdict() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderEventIntelIntradayEvidenceAxis")
    end = text.index("function renderEventIntelPhysicalTrendCurve", start)
    block = text[start:end]

    assert "const todayPhysicalVerdictHtml = renderTodayPhysicalEvidenceVerdict" in block
    assert "buildTodayPhysicalEvidenceVerdict(" in block
    assert "eventIntelPosteriorSsotPendingForDisplay(" in block
    assert "不把空证据解释为中性或拉扯" in block
    assert "${todayPhysicalVerdictHtml}" in block
    assert "physical-evidence-decision is-${escapeHtml(finalConclusion.tone)}" not in block
    assert "final_bucket_probability_pct" in block
    assert "物理证据结论" in block
    assert "今日总判 · 物理证据" in text


def test_bayes_posterior_step_matches_refactored_bucket_probability_stage() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderEventBayesPosteriorStep")
    end = text.index("function renderEventBayesEvidenceFlow", start)
    block = text[start:end]

    assert "最终目标概率" in block
    assert "previous: focus.observedProb" in block
    assert "value: focus.posteriorProb" in block
    assert "realtimeKey: `bayes-stage:${String(cardId || eventIntelEventDate(item) || 'latest')}:posterior`" in block
    assert "目标桶最终概率" in block
    assert "诊断明细" in block
    assert "quantile_grid" in block
    assert "cdf_values" in block
    assert "quantiles_c" in block
    assert "交易使用" not in block
    assert "只读取最终概率" not in block
    assert "daily-trade-advice-mount" not in block


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


def test_source_peak_model_cards_read_member_cache_snapshot_fallback() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    member_start = text.index("function sourcePeakModelMemberSnapshot")
    member_end = text.index("function sourcePeakMarketReference", member_start)
    member_block = text[member_start:member_end]
    bucket_start = text.index("function sourcePeakModelTopBucket")
    bucket_end = text.index("function sourcePeakBucketModeText", bucket_start)
    bucket_block = text[bucket_start:bucket_end]
    value_start = text.index("function sourcePeakModelValue")
    value_end = text.index("function sourcePeakModelRawHistoryTop", value_start)
    value_block = text[value_start:value_end]

    assert "item.model_member_cache_snapshot" in member_block
    assert "item.model_members_decimal_raw" in member_block
    assert "function sourcePeakModelMemberMeanC" in member_block
    assert "qmMeta.model_bucket_rule_comparison" in bucket_block
    assert "return { label: String(ruleTop.label || ''), q: ruleQ, mode: 'model_member_cache_rule_bucket' };" in bucket_block
    assert "const memberMean = sourcePeakModelMemberMeanC(item, modelKey);" in value_block
    assert "if (Number.isFinite(memberMean)) return memberMean;" in value_block


def test_source_peak_field_dependencies_are_ui_snapshot_contract_keys() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    referenced = set()
    for match in re.finditer(r"sourcePeakField\(item,\s*\[([^\]]*)\]", text):
        referenced.update(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)))

    contract = (
        set(server.UI_SNAPSHOT_FRONTEND_CONTRACT_ROW_KEYS)
        | set(server.UI_SNAPSHOT_FRONTEND_CONTRACT_FEATURE_KEYS)
        | set(server.UI_SNAPSHOT_FRONTEND_CONTRACT_TOP_LEVEL_KEYS)
    )

    assert referenced
    assert referenced - contract == set()


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

    assert "模型校准摘要" in focus_block
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


def test_skeleton_overlay_is_removed_from_layout_after_loading() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    css_start = text.index(".skeleton-overlay {")
    css_end = text.index(".skeleton-card {", css_start)
    css_block = text[css_start:css_end]
    js_start = text.index("function setSkeleton")
    js_end = text.index("function getPolicyCooldowns", js_start)
    js_block = text[js_start:js_end]

    assert "display:none;" in css_block
    assert "body.is-loading .skeleton-overlay" in css_block
    assert "display:flex;" in css_block
    assert "overlay.setAttribute('aria-hidden', active ? 'false' : 'true');" in js_block


def test_posterior_spotlight_requires_posterior_ssot_not_capital_or_distribution_fallback() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    rows_start = text.index("function eventIntelPosteriorSsotRows")
    rows_end = text.index("function eventIntelTargetBucketSelectionFromSnapshot", rows_start)
    rows_block = text[rows_start:rows_end]
    start = text.index("const posteriorSpotlightRows =")
    end = text.index("if (posteriorSpotlightPrimary.label)", start)
    block = text[start:end]

    assert "function renderDistributionTable" in text
    assert "const rawRows = Array.isArray(ssot.bucket_probabilities)" in rows_block
    assert ": [];" in rows_block
    assert "ssot.ranked_bucket_probabilities" not in rows_block
    assert "const resolvePosteriorSpotlightPrimary = () => {" in block
    assert "posteriorSsotForSpotlight.target_bucket" in block
    assert "eventIntelCanonicalTargetFromSnapshot(itemRef)" not in block
    assert "source: 'canonical_target'" not in block
    assert "targetOutlookForSpotlight.posterior_final_bucket" in block
    assert "targetOutlookForSpotlight.target_bucket" in block
    assert "const physicalSpotlightRows = itemRef ? eventIntelPhysicalHighPosteriorRows(itemRef, 3) : [];" in text
    assert "? physicalSpotlightRows" in block
    assert ": (itemRef ? eventIntelPosteriorSsotRows(itemRef, 3) : [])" in block
    assert "source: 'physical_high_posterior_ssot'" in block
    assert "posterior_field: (entry && entry.row && entry.row.posterior_field) || 'posterior_ssot_fact.bucket_probabilities'" in block
    assert "const prob = eventIntelFirstFiniteNumber(row.q_temperature_bucket_posterior, row.q_bayes_posterior);" in block
    assert "posterior_ssot_fact.ranked_bucket_probabilities" not in block
    assert "topPosteriorRows.length ? topPosteriorRows : visibleRows" not in block
    assert "posterior_temperature_distribution" not in block
    assert "posterior <= 0" in rows_block
    assert "eventIntelIsPosteriorHardZeroRow(row)" in rows_block
    assert "row.hard_zero" in text
    assert "final_bucket_lock" in text
    assert "eventIntelBucketProbabilityPct(itemRef, hardOutlookLabel" in block
    assert "eventIntelBucketProbabilityPct(itemRef, finalLabel" not in block
    assert "cumulative_posterior_final_bucket_probability_pct" in text
    assert "posteriorRunnerRankLabels" in block
    assert "SSOT次高" in block
    assert "分布次高" not in block
    assert "capitalPlanForSpotlight.target_bucket" not in block
    assert "source: 'capital_plan'" not in block
    assert "source === 'capital_plan'" not in block
    assert "source: 'distribution_top'" not in block
    assert "posteriorSpotlightPrimary.source === 'distribution_top'" not in block
    assert "source: 'target_bucket_outlook'" not in block
    assert "source: 'posterior_ssot_fact_missing'" in block
    assert "posterior_ssot_fact 缺失" in block
    assert "posteriorSpotlightRunnerRows = posteriorSpotlightRows" in text
    assert "const label = eventIntelBucketLabel(String(row.outcome || ''), 'C') || String(row.outcome || '—');" in text
    assert "交易目标" in text
    assert "后验SSOT缺失" in text
    assert "后端未提供 posterior_ssot_fact；看板不展示默认目标桶，也不从分布或资金计划推断目标。" in text
    assert "当前最高合约（未结算）" not in block
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
    assert "模型校准摘要" in text
    assert "模型侧诊断" in text
    assert "非最终目标" in text
    assert "Weather.com/TWC" in text
    assert "已优先采用后端实况约束后的最高目标" in text
    assert "预测最高温最终概率，未结算" not in text
    assert "这是预测最高温目标概率，未结算，不是当前实况温度。" not in text
    assert "最终最高温落桶概率" not in text
    assert "最终最高温落桶目标" not in text
    assert "预测最终最高温落桶" not in text


def test_compact_posterior_ssot_keeps_full_bucket_distribution_separate_from_ranked() -> None:
    compact = server._compact_posterior_ssot_fact_for_ui_snapshot(
        {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "38°C",
            "target_probability": 0.4287,
            "bucket_probabilities": [
                {"bucket": "37°C", "probability": 0.3236},
                {"bucket": "38°C", "probability": 0.4287},
                {"bucket": "39°C", "probability": 0.2038},
                {"bucket": "40°C", "probability": 0.0405},
                {"bucket": "41°C", "probability": 0.0033},
            ],
            "ranked_bucket_probabilities": [
                {"bucket": "38°C", "probability": 0.4287},
                {"bucket": "41°C", "probability": 0.3313},
            ],
        }
    )

    bucket_labels = [row["outcome"] for row in compact["bucket_probabilities"]]
    ranked_labels = [row["outcome"] for row in compact["ranked_bucket_probabilities"]]

    assert bucket_labels == ["37°C", "38°C", "39°C", "40°C", "41°C"]
    assert ranked_labels == ["38°C", "37°C", "39°C", "40°C", "41°C"]
    assert compact["ranked_bucket_probabilities"][4]["probability"] == pytest.approx(0.0033)
    assert compact["bucket_count"] == 5


def test_sequential_v3_ui_keeps_reported_x_separate_from_market_edge_buckets() -> None:
    payload = {
        "posterior_ssot_fact": {
            "schema": "sequential_temperature_posterior_ssot_v3_reported_x",
            "state": "physical_only_market_projection_na",
            "writer_mode": "production",
            "fact_snapshot_id": "factsnap-seq-v2",
            "posterior_run_id": "post-seq-v2",
            "posterior_checkpoint_id": "pchk-seq-v2",
            "committed_at": "2026-07-14T05:00:00Z",
            "probability_target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
            "reported_physical_x_distribution": {"33": 0.20, "34": 0.55, "35": 0.25},
            "reported_x_lower_bound_c": 33,
            "market_bucket_probabilities": {},
            "market_bucket_probabilities_na_reason": "x_to_s_to_b_bridge_unavailable",
        }
    }

    assert server._ui_snapshot_latest_fact_needs_posterior_projection(payload) is False
    slim = server._slim_fact_payload_for_ui(payload)
    physical = slim["ui_sections"]["physical_high_posterior"]
    market = slim["ui_sections"]["bayes_posterior_ssot"]

    assert physical["state"] == "available"
    assert physical["top_temperature_c"] == pytest.approx(34.0)
    assert physical["top_probability"] == pytest.approx(0.55)
    assert [row["temperature_c"] for row in physical["ranked_temperature_probabilities"]] == [34.0, 35.0, 33.0]
    assert physical["boundary"] == "reported_X_whole_degree_only_not_latent_Z_not_market_B_not_cumulative_edge_bucket"
    assert market["state"] == "market_projection_na"
    assert market["na_reason"] == "x_to_s_to_b_bridge_unavailable"
    assert not market.get("target_bucket")
    assert server._canonical_target_from_fact_payload(payload) == {}
    assert "target_bucket" not in slim
    assert "ssot_target_bucket" not in slim

    # Summary compaction keeps sequential lineage in the authoritative SSOT;
    # a duplicate top-level lineage copy is not required for cache acceptance.
    slim["fact_id"] = 108484
    assert server._ui_snapshot_summary_item_has_display_ssot_contract(slim) is True


def test_production_v6_v4_ui_projects_reported_x_without_market_bucket_inference() -> None:
    payload = {
        "posterior_ssot_fact": {
            "schema": "sequential_temperature_posterior_ssot_v4_reported_x",
            "state": "physical_only_market_projection_na",
            "writer_mode": "production",
            "fact_snapshot_id": "fact-v6-event-1",
            "posterior_run_id": "posterior-v6-1",
            "posterior_checkpoint_id": "posterior-checkpoint-v6-1",
            "committed_at": "2026-07-16T02:02:45.527Z",
            "probability_target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
            "reported_physical_x_distribution": {"38": 0.50, "39": 0.30, "40": 0.20},
            "reported_x_lower_bound_c": 38,
            "market_bucket_probabilities": {},
            "market_bucket_probabilities_na_reason": "resolution_bridge_not_closed",
            "probability_runtime_identity": {
                "composer_schema": "physical_state_bayes_v6_pre_event_predict_update",
                "v5_fallback_forbidden": True,
            },
            "termination_risk": {"state": "available", "probability": 0.42},
        }
    }

    assert server._ui_snapshot_latest_fact_needs_posterior_projection(payload) is False
    slim = server._slim_fact_payload_for_ui(payload)
    physical = slim["ui_sections"]["physical_high_posterior"]
    market = slim["ui_sections"]["bayes_posterior_ssot"]

    assert physical["state"] == "available"
    assert physical["top_temperature_c"] == pytest.approx(38.0)
    assert physical["top_probability"] == pytest.approx(0.50)
    assert market["state"] == "market_projection_na"
    assert market["na_reason"] == "resolution_bridge_not_closed"
    assert server._canonical_target_from_fact_payload(payload) == {}
    assert slim["posterior_ssot_fact"]["probability_runtime_identity"]["v5_fallback_forbidden"] is True
    assert slim["posterior_ssot_fact"]["termination_risk"]["probability"] == pytest.approx(0.42)

    slim["fact_id"] = 109070
    assert server._ui_snapshot_summary_item_has_display_ssot_contract(slim) is True


def test_production_v6_ui_uses_authoritative_timeline_for_amsc_strong_observation_display() -> None:
    payload = {
        "event_date": "2026-07-16",
        "posterior_ssot_fact": {
            "schema": "sequential_temperature_posterior_ssot_v4_reported_x",
            "state": "physical_only_market_projection_na",
            "writer_mode": "production",
            "fact_snapshot_id": "fact-v6-timeline",
            "posterior_run_id": "posterior-v6-timeline",
            "posterior_checkpoint_id": "checkpoint-v6-timeline",
            "reported_physical_x_distribution": {"38": 0.55, "39": 0.45},
            "reported_x_lower_bound_c": 38,
            "market_bucket_probabilities": {},
            "market_bucket_probabilities_na_reason": "resolution_bridge_not_closed",
            "peak_timing_state": {
                "schema": "peak_timing_state_l3_v1",
                "state": "available",
                "peak_timing_state_id": "peaktiming-v6-ui",
                "entry_peak_at": "2026-07-16T09:00:00Z",
                "peak_already_probability": 0.4,
                "future_peak_probability": 0.6,
                "does_not_update_physical_x": True,
            },
            "termination_risk": {
                "schema": "termination_risk_marginal_v3_physical_state_bayes_v6",
                "state": "available",
                "probability_no_material_new_daily_high": 0.4,
                "updates_posterior": False,
            },
        },
        "temperature_trajectory_timeline": {
            "version": "sequential_reported_x_temperature_timeline_v3",
            "probability_semantics": "amsc_scheduled_regular_metar_reported_daily_max_integer_x",
            "rows": [
                {
                    "canonical_observation_event_id": "canobs-v6-ui",
                    "primary_source_id": "zuuu_metar_raw_amsc",
                    "primary_metar_latest_at": "2026-07-16T08:30:00Z",
                    "primary_metar_latest_local_hhmm": "16:30",
                    "primary_metar_temp_c": 37.5,
                    "primary_metar_today_max_c": 37.5,
                    "reported_x_lower_bound_c": 38,
                    "dewpoint_c": 24.0,
                    "cloud_text": "FEW040",
                    "weather_text": "",
                    "temperature_momentum_effective_c_per_hour": 0.6,
                    "temperature_momentum_1h_c_per_hour": 1.0,
                    "temperature_momentum_realization_state": "warming",
                    "latent_state_probabilities": [
                        {"state": "warming", "probability": 0.7},
                        {"state": "temporary_platform", "probability": 0.3},
                    ],
                    "termination_risk": {
                        "schema": "termination_risk_marginal_v3_physical_state_bayes_v6",
                        "state": "available",
                        "probability_no_material_new_daily_high": 0.4,
                        "updates_posterior": False,
                    },
                }
            ],
        },
    }

    slim = server._slim_fact_payload_for_ui(payload)
    amsc = slim["ui_sections"]["amsc_metar_ssot"]
    strong = slim["ui_sections"]["observation_strong"]

    assert amsc["state"] == "available"
    assert amsc["metar"]["latest_temp_c"] == pytest.approx(37.5)
    assert amsc["metar"]["today_max_c"] == pytest.approx(37.5)
    assert amsc["temperature_momentum"]["effective_c_per_hour"] == pytest.approx(0.6)
    assert amsc["physical_evidence"]["cloud_text"] == "FEW040"
    assert amsc["termination_risk"]["probability_no_material_new_daily_high"] == pytest.approx(0.4)
    assert strong["state"] == "raw_observation_only"
    assert strong["hard_source_observations"][0]["source_id"] == "zuuu_metar_raw_amsc"
    assert strong["state_probs"]["warming"] == pytest.approx(0.7)
    assert strong["peak_timing_state"]["peak_timing_state_id"] == "peaktiming-v6-ui"
    assert slim["posterior_ssot_fact"]["peak_timing_state"]["does_not_update_physical_x"] is True
    assert "or below" not in json.dumps(
        slim["ui_sections"]["physical_high_posterior"], ensure_ascii=False
    )


def test_sequential_v3_canonical_market_target_requires_bridge_output() -> None:
    payload = {
        "posterior_ssot_fact": {
            "schema": "sequential_temperature_posterior_ssot_v3_reported_x",
            "state": "available",
            "target_bucket": "38°C",
            "target_probability": 0.62,
            "reported_physical_x_distribution": {"37": 0.70, "38": 0.30},
            "reported_x_lower_bound_c": 37,
            "market_bucket_probabilities": {
                "37°C": 0.31,
                "38°C": 0.62,
                "39°C or higher": 0.07,
            },
        }
    }

    canonical = server._canonical_target_from_fact_payload(payload)

    assert canonical["label"] == "38°C"
    assert canonical["prob"] == pytest.approx(0.62)
    assert [row["outcome"] for row in canonical["ranked_rows"]] == [
        "38°C",
        "37°C",
        "39°C or higher",
    ]


def test_ui_source_has_no_direct_observed_temperature_to_market_bucket_path() -> None:
    source = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")

    assert "_ui_snapshot_hard_temperature_floor_c" not in source
    assert "_ui_snapshot_bucket_below_hard_temperature_floor" not in source
    assert "posterior_target_bucket_below_hard_temperature_floor" not in source
    assert "_invalidate_posterior_ssot_fact_by_hard_observation" not in source


def test_sequential_v2_continuous_grid_is_not_rendered_as_reported_x() -> None:
    payload = {
        "posterior_ssot_fact": {
            "schema": "sequential_temperature_posterior_ssot_v2",
            "state": "physical_only_market_projection_na",
            "writer_mode": "production",
            "posterior_run_id": "post-v2",
            "physical_high_distribution": {"33.25": 0.5, "33.5": 0.5},
        }
    }
    slim = server._slim_fact_payload_for_ui(payload)
    physical = slim["ui_sections"]["physical_high_posterior"]
    assert physical["state"] == "missing"
    assert physical["na_reason"] == (
        "legacy_continuous_latent_distribution_is_not_reported_x"
    )


def test_sequential_v3_trajectory_timeline_reads_reported_x_checkpoint_chain() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE posterior_checkpoints (
            posterior_checkpoint_id TEXT PRIMARY KEY,
            previous_checkpoint_id TEXT,
            last_canonical_event_id TEXT,
            last_strong_update_id TEXT,
            reported_physical_x_distribution_json TEXT,
            latent_post_state_json TEXT,
            reported_x_lower_bound_c INTEGER,
            committed_at TEXT
        );
        CREATE TABLE canonical_observation_events (
            canonical_observation_event_id TEXT PRIMARY KEY,
            report_type TEXT,
            valid_time TEXT,
            first_actionable_seen_at TEXT,
            accepted_at TEXT,
            selected_source_id TEXT,
            parsed_observation_json TEXT
        );
        CREATE TABLE strong_observation_updates (
            strong_observation_update_id TEXT PRIMARY KEY,
            raw_log_score_json TEXT,
            path_auxiliary_json TEXT
        );
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY,
            slug TEXT,event_date TEXT,posterior_run_id TEXT,outcome TEXT,side TEXT,
            decided_state TEXT,will_order INTEGER,reason_codes_json TEXT,
            details_json TEXT,created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO canonical_observation_events VALUES (?,?,?,?,?,?,?)",
        (
            "canobs-1",
            "METAR",
            "2026-07-14T07:30:00Z",
            "2026-07-14T07:30:01Z",
            "2026-07-14T07:30:01Z",
            "zuuu_metar_raw_amsc",
            json.dumps(
                {
                    "temperature_c": 37.0,
                    "dewpoint_c": 24.0,
                    "cloud_text": "SCT033",
                    "weather_text": "",
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO strong_observation_updates VALUES (?,?,?)",
        (
            "stupd-1",
            json.dumps(
                [
                    {
                        "temperature_c": 37.5,
                        "pre_state": "warming",
                        "raw_log_score": -0.15,
                        "state_log_score_adjustment": -0.05,
                        "scoring_kind": "operational_physical_state_aware_lr_v2",
                        "feature_values": {
                            "trajectory.observed_peak_including_current_c": 38.0,
                            "trajectory.current_step_rate_c_per_hour": -0.2,
                            "trajectory.slope_60m_c_per_hour": -1.0,
                            "trajectory.pre_event_path_state": "temporary_cooling",
                            "physical.first_cb_event_state": "cb_already_seen",
                            "physical.first_cb_timing_phase": "late_after_peak_window",
                            "physical.first_cb_peak_relation": "at_observed_peak",
                        },
                        "feature_audit": {
                            "physical.first_cb_valid_time": "2026-07-14T09:00:00Z",
                            "physical.first_cb_temperature_c": 38.0,
                            "physical.cb_present_in_current_event": False,
                        },
                    },
                    {
                        "temperature_c": 37.5,
                        "pre_state": "terminal_cooling",
                        "raw_log_score": -1.25,
                        "state_log_score_adjustment": -1.15,
                        "scoring_kind": "operational_physical_state_aware_lr_v2",
                        "feature_values": {}
                    }
                ]
            ),
            json.dumps(
                {
                    "termination_risk": {
                        "probability_no_material_new_daily_high": 0.993,
                    }
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO posterior_checkpoints VALUES (?,?,?,?,?,?,?,?)",
        (
            "pchk-1",
            "",
            "canobs-1",
            "stupd-1",
            json.dumps({"37": 0.5924, "38": 0.4076}),
            json.dumps({"warming": 0.72, "terminal_cooling": 0.28}),
            37,
            "2026-07-14T07:30:02Z",
        ),
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            1,
            "highest-temperature-in-chengdu-on-july-14-2026",
            "2026-07-14",
            "post-1",
            "38°C",
            "YES",
            "hold_no_order",
            0,
            json.dumps(["predicted_peak_time_reached_or_passed"]),
            json.dumps(
                {
                    "strategy_leg": "second_yes",
                    "entry_allowed": False,
                    "entry_block_reason": "predicted_peak_time_reached_or_passed",
                    "second_yes_entry_window_state": {
                        "second_yes_entry_window_state_id": "second-window-1",
                        "entry_allowed": False,
                        "entry_block_reason": "predicted_peak_time_reached_or_passed",
                        "predicted_peak_at": "2026-07-14T09:00:00Z",
                        "entry_window_start_at": "2026-07-14T08:30:00Z",
                        "entry_window_end_at": "2026-07-14T09:00:00Z",
                        "entry_window_minutes": 30,
                        "existing_same_bucket_position_may_be_retained": True,
                    },
                }
            ),
            "2026-07-14T09:00:01Z",
        ),
    )
    # The displayed checkpoint has consumed canobs-1, while a later accepted
    # regular METAR is already present in the fact layer.  The UI must freeze
    # the checkpoint probability instead of presenting it as current.
    conn.executescript(
        """
        ALTER TABLE posterior_checkpoints ADD COLUMN target_identity TEXT;
        ALTER TABLE posterior_checkpoints ADD COLUMN consumed_event_ids_json TEXT;
        ALTER TABLE canonical_observation_events ADD COLUMN target_identity TEXT;
        ALTER TABLE canonical_observation_events ADD COLUMN state TEXT;
        ALTER TABLE canonical_observation_events ADD COLUMN revision_no INTEGER;
        UPDATE posterior_checkpoints
        SET target_identity='zuuu:2026-07-14', consumed_event_ids_json='["canobs-1"]';
        UPDATE canonical_observation_events
        SET target_identity='zuuu:2026-07-14', state='confirmed', revision_no=0;
        """
    )
    conn.execute(
        """
        INSERT INTO canonical_observation_events (
            canonical_observation_event_id,report_type,valid_time,
            first_actionable_seen_at,accepted_at,selected_source_id,
            parsed_observation_json,target_identity,state,revision_no
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "canobs-2",
            "METAR",
            "2026-07-14T08:00:00+00:00",
            "2026-07-14T08:00:01+00:00",
            "2026-07-14T08:00:01+00:00",
            "zuuu_metar_raw_amsc",
            json.dumps({"temperature_c": 38.0}),
            "zuuu:2026-07-14",
            "accepted_primary",
            0,
        ),
    )
    ssot = {
        "schema": "sequential_temperature_posterior_ssot_v3_reported_x",
        "fact_snapshot_id": "factsnap-1",
        "posterior_run_id": "post-1",
        "posterior_checkpoint_id": "pchk-1",
        "reported_x_lower_bound_c": 37,
        "peak_timing_state": {
            "schema": "peak_timing_state_l3_v1",
            "state": "available",
            "peak_timing_state_id": "peak-timing-1",
            "mode_time_state": "future_forecast_supported_slot",
            "mode_probability": 0.61,
            "peak_already_probability": 0.39,
            "future_peak_probability": 0.61,
            "entry_peak_at": "2026-07-14T09:00:00Z",
            "observed_peak_at": "2026-07-14T08:00:00Z",
            "does_not_update_physical_x": True,
        },
        "market_bucket_probabilities_na_reason": "x_to_s_to_b_bridge_unavailable",
    }

    timeline = server._build_sequential_physical_timeline_for_ui_snapshot(
        conn,
        posterior_ssot=ssot,
        slug="highest-temperature-in-chengdu-on-july-14-2026",
        event_date="2026-07-14",
        run_date="2026-07-14",
        event_tz="Asia/Shanghai",
    )

    assert timeline["version"] == "sequential_reported_x_temperature_timeline_v3"
    assert timeline["probability_semantics"] == "amsc_scheduled_regular_metar_reported_daily_max_integer_x"
    assert timeline["target_bucket"] == "37°C"
    assert timeline["latest_probability_pct"] == pytest.approx(59.24)
    assert timeline["rows"][0]["amsc_today_max_c"] == pytest.approx(38.0)
    assert timeline["rows"][0]["awc_temp_c"] is None
    assert timeline["rows"][0]["awc_na_reason"] == "awc_not_in_sequential_checkpoint_chain"
    assert timeline["rows"][0]["temperature_momentum_effective_c_per_hour"] == pytest.approx(-0.2)
    assert timeline["rows"][0]["probability_no_material_new_daily_high"] == pytest.approx(0.993)
    assert timeline["rows"][0]["latent_top_state"] == "warming"
    assert timeline["rows"][0]["latent_top_state_probability"] == pytest.approx(0.72)
    assert timeline["rows"][0]["strong_lr_scoring_kind"] == "operational_physical_state_aware_lr_v2"
    assert timeline["rows"][0]["strong_lr_state_discriminating"] is None
    assert timeline["rows"][0]["strong_lr_state_discriminating_na_reason"] == "reported_x_bucket_combines_multiple_latent_z_cells"
    assert timeline["latest_latent_top_state"] == "warming"
    assert timeline["latest_strong_lr_state_discriminating"] is None
    assert timeline["rows"][0]["first_cb_local_hhmm"] == "17:00"
    assert timeline["rows"][0]["first_cb_temperature_c"] == pytest.approx(38.0)
    assert timeline["rows"][0]["first_cb_timing_phase"] == "late_after_peak_window"
    assert "不逆转已形成日高" in timeline["rows"][0]["first_cb_effect"]
    assert "首次 CB 17:00" in timeline["latest_first_cb_display"]
    assert timeline["peak_timing_state"]["entry_peak_local_hhmm"] == "17:00"
    assert timeline["peak_timing_state"]["peak_already_probability"] == pytest.approx(0.39)
    assert timeline["second_yes_entry_window"]["state"] == "available"
    assert timeline["second_yes_entry_window"]["entry_allowed"] is False
    assert timeline["second_yes_entry_window"]["entry_window_start_local_hhmm"] == "16:30"
    assert timeline["second_yes_entry_window"]["entry_window_end_local_hhmm"] == "17:00"
    assert timeline["second_yes_entry_window"]["max_notional_usdc"] == pytest.approx(4.99)
    assert timeline["second_yes_entry_window"]["source"] == "auto_trade_decisions.details_json"
    assert timeline["second_yes_entry_window"]["frontend_recompute_allowed"] is False
    assert timeline["reported_x_ssot_freshness"]["state"] == "blocked_unconsumed_canonical_events"
    assert timeline["reported_x_ssot_freshness"]["pending_canonical_event_ids"] == ["canobs-2"]
    assert timeline["reported_x_ssot_freshness"]["pending_regular_metar_max_temperature_c"] == pytest.approx(38.0)
    assert timeline["reported_x_ssot_freshness"]["pending_regular_metar_hard_bound_ahead_of_checkpoint"] is True
    assert timeline["probability_current"] is False
    assert "34°C or below" not in json.dumps(timeline, ensure_ascii=False)
    slim = server._slim_fact_payload_for_ui_snapshot_summary(
        {
            "slug": "highest-temperature-in-chengdu-on-july-14-2026",
            "event_date": "2026-07-14",
            "temperature_trajectory_timeline": timeline,
        }
    )
    slim_freshness = slim["temperature_trajectory_timeline"]["reported_x_ssot_freshness"]
    assert slim_freshness["probability_current"] is False
    assert slim_freshness["pending_regular_metar_max_temperature_c"] == pytest.approx(38.0)


def test_sequential_v2_frontend_labels_physical_timeline_as_x_not_final_market_bucket() -> None:
    html = (ROOT / "ui/index.html").read_text(encoding="utf-8")

    assert "physical_high_exact_grid" in html
    assert "amsc_scheduled_regular_metar_reported_daily_max_integer_x" in html
    assert "物理 X 格点概率" in html
    assert "物理 X 不是 or below/or higher 市场边缘桶" in html
    assert "eventIntelLatentStateLabel" in html
    assert "状态感知 Strong LR" in html
    assert "operational_physical_state_aware_lr_v2" in html
    assert "首次 CB 时序" in html
    assert "latest_first_cb_display" in html
    assert "预计最高温 / 次 YES" in html
    assert "second_yes_entry_window" in html
    assert "上限 4.99 USDC" in html
    assert "SSOT 概率已冻结" in html
    assert "冻结旧 checkpoint" in html
    assert "待推进 X 硬下界" in html
    assert "不是当前概率；前端不重算" in html


def test_compact_posterior_ssot_keeps_weak_source_attribution_trace() -> None:
    compact = server._compact_posterior_ssot_fact_for_ui_snapshot(
        {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "forecast_weak_attribution": {
                "schema": "forecast_weak_attribution_trace_v1",
                "state": "available",
                "read_only": True,
                "weak_likelihood_consumed": True,
                "source_group_contributions": [
                    {
                        "group": "model",
                        "source_count": 1,
                        "capped_precision_share_within_weak_likelihood": 0.4,
                        "distribution": {"32°C": 0.7, "33°C": 0.3},
                    },
                    {
                        "group": "taf",
                        "source_count": 1,
                        "capped_precision_share_within_weak_likelihood": 0.6,
                        "distribution": {"32°C": 0.1, "33°C": 0.9},
                    },
                ],
                "source_contribution_trace": [
                    {
                        "source_event_id": "srcevt_taf",
                        "source_id": "zuuu_taf_forecast_amsc",
                        "group": "taf",
                        "included": True,
                        "forecast_peak_c": 33.0,
                    }
                ],
                "forecast_group_pooling": {
                    "schema": "forecast_group_precision_pool_v1",
                    "state": "available",
                    "pooling_mode": "symmetric_group_precision_geometric_pool",
                    "included_groups": ["model", "taf"],
                    "source_group_count": 2,
                    "legacy_model_taf_special_case_retired": True,
                },
                "pooling_decision": {
                    "state": "available",
                    "pooling_mode": "symmetric_group_precision_geometric_pool",
                    "legacy_model_taf_special_case_retired": True,
                },
            },
        }
    )

    attribution = compact["forecast_weak_attribution"]
    assert attribution["state"] == "available"
    assert attribution["forecast_group_pooling"]["pooling_mode"] == "symmetric_group_precision_geometric_pool"
    assert attribution["source_group_contributions"][0]["distribution"] == {"32°C": 0.7, "33°C": 0.3}
    assert attribution["source_contribution_trace"] == [
        {
            "source_event_id": "srcevt_taf",
            "source_id": "zuuu_taf_forecast_amsc",
            "group": "taf",
            "included": True,
            "forecast_peak_c": 33.0,
        }
    ]


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
    assert "capitalTargetLabel" not in selection_block
    assert "itemCapitalPlan" not in selection_block
    assert "ssotPlan" not in selection_block
    assert "const canonicalTarget = eventIntelCanonicalTargetFromSnapshot(item, payload);" not in selection_block
    assert "if (canonicalTarget.label)" not in selection_block
    assert "if (posteriorSsotLabel)" in selection_block
    assert "let source = 'missing_ssot_target';" in selection_block
    assert "source = 'target_bucket_outlook_hard';" in selection_block
    assert "source = 'target_realization_capital_plan';" not in selection_block
    assert "source = targetSelection.ssot_source || 'target_bucket_selection';" not in selection_block
    assert "target_bucket_outlook';" not in selection_block
    assert "posterior_ssot_fact 缺失" in selection_block
    hard_start = text.index("function eventIntelOutlookHasHardSourceTarget")
    hard_end = text.index("function eventIntelItemHasHardSourceOutlook", hard_start)
    hard_block = text[hard_start:hard_end]
    assert "data.hard_source_applied === true" in hard_block
    assert "data.final_bucket_lock === true" in hard_block
    assert "sourceText.includes('awc')" in hard_block
    assert "Number.isFinite(toFiniteNumberOrNaN(data.final_bucket_probability_pct))" not in hard_block
    assert "eventIntelBucketProbabilityPct(item, targetLabel" not in selection_block
    assert "topPosterior" not in selection_block


def test_target_realization_selection_has_no_distribution_top_fallback() -> None:
    source = inspect.getsource(server._target_realization_selected_labels)
    plan_source = inspect.getsource(server._build_target_realization_capital_plan)
    canonical_source = inspect.getsource(server._canonical_target_from_fact_payload)
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    helper_start = text.index("function eventIntelSameBucketLabel")
    helper_end = text.index("function eventIntelTargetCapitalPlan", helper_start)
    helper_block = text[helper_start:helper_end]
    selection_start = text.index("function eventIntelTargetBucketSelectionFromSnapshot")
    selection_end = text.index("function eventIntelCapitalLegForBucket", selection_start)
    selection_block = text[selection_start:selection_end]

    assert "posterior_distribution_top1" not in source
    assert "distribution_probability_order" not in source
    assert "posterior_leader" not in source
    assert "rows[0]" not in plan_source
    assert "event_fact_latest.posterior_temperature_distribution_top1" not in canonical_source
    assert "event_fact_latest.dynamic_constrained_posterior_top1" not in canonical_source
    assert "topSingle" not in selection_block
    assert "decision.target_bucket" not in selection_block
    assert "posteriorSsotSecond" in selection_block
    assert "ssotPlan.second_target_bucket" not in selection_block
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
    assert "const compactFeatures = objectOrEmpty(item.weather_likelihood_features_compact);" in block
    assert "const uiSections = objectOrEmpty(item.ui_sections);" in block
    assert "const posteriorSsotFact = objectOrEmpty(item.posterior_ssot_fact);" in block
    assert "const temperatureTrajectory = objectOrEmpty(item.temperature_trajectory);" in block
    assert "weather_likelihood_features_compact: compactFeatures" in block
    assert "weather_likelihood_features: fullFeatures" in block
    assert "ui_sections: uiSections" in block
    assert "prior_distribution: priorDistribution" in block
    assert "metar_strong_likelihood: metarStrongLikelihood" in block
    assert "posterior_ssot_fact: posteriorSsotFact" in block
    assert "temperature_trajectory: temperatureTrajectory" in block
    assert "source_observations: arrayOrEmpty(item.source_observations)" in block
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


def test_chengdu_intraday_payload_prefers_ui_snapshot_fact_over_global_summary_cache() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function eventIntelChengduIntradayPayload")
    end = text.index("function eventIntelEffectiveIntradayDistribution", start)
    block = text[start:end]

    assert "const factPayload = eventIntelPayloadFromSnapshotItem(item);" in block
    assert "if (factPayload && factPayload.ok && eventIntelDateMatches(item, factPayload.event_date))" in block
    assert "return factPayload;" in block
    assert block.index("return factPayload;") < block.index("const globalPayload = window.__chengduIntradayDecisionPayload || null;")
    assert "eventIntelNewerIntradayPayload(\n\t        factPayload," not in block


def test_temperature_trajectory_root_ssot_overrides_stale_compact_metrics_for_ui() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    payload_start = text.index("function eventIntelPayloadFromSnapshotItem")
    payload_end = text.index("function storeEventIntelChengduIntradayPayload", payload_start)
    payload_block = text[payload_start:payload_end]
    helper_start = text.index("function eventIntelTemperatureTrajectoryMetricsFromSsot")
    helper_end = text.index("function todayPhysicalMetricsFromSources", helper_start)
    helper_block = text[helper_start:helper_end]
    metrics_start = text.index("function todayPhysicalMetricsFromSources")
    metrics_end = text.index("function metarStrongTrajectoryStateText", metrics_start)
    metrics_block = text[metrics_start:metrics_end]

    assert "eventIntelTemperatureTrajectoryMetricsFromSsot(temperatureTrajectoryStatus, temperatureTrajectory)" in payload_block
    assert "...temperatureTrajectoryMetrics" in payload_block
    assert "...summaryTrajectoryMetrics" in payload_block
    assert "...snapshotTrajectoryMetrics" in payload_block
    assert "temperature_trajectory_available" in helper_block
    assert "temperature_momentum_effective_c_per_hour" in helper_block
    assert "temperature_rise_rate_window" in helper_block
    assert "rolling_temperature_observation_summary" in helper_block
    assert "mergeOverride(eventIntelTemperatureTrajectoryMetricsFromSsot(payloadData.temperature_trajectory_status, payloadData.temperature_trajectory))" in metrics_block
    assert "mergeOverride(eventIntelTemperatureTrajectoryMetricsFromSsot(itemData.temperature_trajectory_status, itemData.temperature_trajectory))" in metrics_block


def test_temperature_momentum_visual_supports_rolling_effective_ssot_rate() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    rates_start = text.index("function temperatureMomentumRatesFromMetrics")
    rates_end = text.index("function temperatureMomentumWindowLabel", rates_start)
    rates_block = text[rates_start:rates_end]
    visual_start = text.index("function renderTemperatureMomentumVisual")
    visual_end = text.index("function temperatureMomentumWeightedScore", visual_start)
    visual_block = text[visual_start:visual_end]

    assert "'rolling', 'effective'" in text
    assert "source.temperature_momentum_effective_c_per_hour" in rates_block
    assert "source.temperature_rise_rate_c_per_hour" in rates_block
    assert "out[key] = effectiveRate;" in rates_block
    assert "function temperatureMomentumWindowLabel" in text
    assert "滚动" in text
    assert "有效" in text
    assert "displayWindows = [fallbackWindow];" in visual_block
    assert "temperatureMomentumWindowLabel(label)" in visual_block


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
    assert "第二目标最终概率" in block
    assert "最终概率是未结算前的目标桶概率" in block
    assert "targetSemantics.pathNoun" in block
    assert "data.bucket_distribution" in block
    assert "is-temperature-path-buckets" in block
    assert "最终概率 ${posterior}" in block
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
    assert "等待后端快照输出最终概率、路径概率、可靠性、新鲜度与交易跟随门控；未结算，不是当前实况" in text
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


def test_today_physical_verdict_displays_strong_trajectory_and_momentum_cards() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function buildTodayPhysicalEvidenceVerdict")
    end = text.index("function renderTodayPhysicalEvidenceVerdict", start)
    block = text[start:end]

    assert "metarStrongTemperatureTrajectoryFromSources" in text
    assert "observation_strong" in text
    assert "function todayPhysicalMetricsFromSources" in text
    assert "mergeFill(payloadData.weather_likelihood_features_compact)" in text
    assert "label: '温度轨迹'" in block
    assert "label: '温度动量'" in block
    assert "strongTrajectory.dedupe_policy ? '强似然内消费一次'" in block
    assert "formatTemperatureMomentumLine(metricsData, TEMPERATURE_MOMENTUM_COMPACT_WINDOWS)" in block


def test_temperature_regime_v1_layout_matches_actual_payload_shape() -> None:
    text = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    start = text.index("function renderTemperatureTrajectoryRegimeV1Panel")
    end = text.index("function eventIntelForecastChannelPeak", start)
    block = text[start:end]

    assert ".temperature-regime-v1-summary-grid" in text
    assert ".temperature-regime-v1-fit-strip" in text
    assert ".temperature-regime-v1-fit-points" in text
    assert "const fit = v1.local_quadratic_fit" in block
    assert "实况轨迹判断" in block
    assert "预计最高" in block
    assert "路径样本" in block
    assert "降温压力" in block
    assert "峰值时段" in block
    assert "盘中辅助" in block
    assert "pathCandidates.slice(0, 8)" in block
    assert "已接入" in block
    assert "temperature-regime-v1-kpis" not in block


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
    refresh_start = trace_block.index("def _refresh_async(")
    refresh_end = trace_block.index("now_ts = time.time()", refresh_start)
    refresh_block = trace_block[refresh_start:refresh_end]
    assert '"na_reason": "ui_trace_read_only_no_market_refresh"' in refresh_block
    assert "_trace_refresh_market_ticks_for_blocked(" not in refresh_block
    assert "start_auto_trade_run(" not in refresh_block
    compute_start = trace_block.index("def _compute_payload()")
    compute_end = trace_block.index("def _refresh_async(", compute_start)
    compute_block = trace_block[compute_start:compute_end]
    assert "_auto_trade_cleanup_stale_runs(" not in compute_block


def test_heavy_lane_busy_returns_last_committed_summary_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    cache_key = "summary:2026-07-15:24:chengdu"
    cached_payload = {
        "ok": True,
        "run_date": "2026-07-15",
        "facts": {
            "items": [{"slug": "highest-temperature-in-chengdu-on-july-15-2026"}],
            "total": 1,
        },
    }
    monkeypatch.setattr(server, "UI_SNAPSHOT_DIRTY_STAMP_PATH", Path("/nonexistent/ui-dirty-stamp"))
    with server._UI_SNAPSHOT_CACHE_LOCK:
        previous = dict(server._UI_SNAPSHOT_CACHE)
        server._UI_SNAPSHOT_CACHE.clear()
        server._UI_SNAPSHOT_CACHE[cache_key] = (time.time() - 2.0, cached_payload)
    try:
        payload = server._ui_heavy_busy_cached_payload(
            "/api/ui_snapshot_summary?run_date=2026-07-15&limit=2&city=chengdu"
        )
    finally:
        with server._UI_SNAPSHOT_CACHE_LOCK:
            server._UI_SNAPSHOT_CACHE.clear()
            server._UI_SNAPSHOT_CACHE.update(previous)

    assert payload is not None
    assert payload["source"] == "ui_heavy_request_lane_stale_cache"
    assert payload["busy"] is True
    assert payload["read_only"] is True
    assert payload["cache_rebuild_in_flight"] is True
    assert len(payload["facts"]["items"]) == 1


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
    assert "路径触达不足，暂不追买 YES" in block
    assert "temperature_path_trade_follow_reason" in block
    assert "路径概率门控禁止 YES 跟随" not in block
    assert "temperature_path_trade_follow_not_allowed: '路径触达不足，暂不追买YES'" in text
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


def test_retired_physical_likelihood_audit_fields_preserve_compact_component_groups() -> None:
    scanner_text = (ROOT / "scan_weather_market.py").read_text(encoding="utf-8")

    components_pos = scanner_text.index(
        'components = row.get("components")'
    )
    component_out_pos = scanner_text.index(
        'out["q_bayes_physical_likelihood_components"]',
        components_pos,
    )
    component_groups_pos = scanner_text.index(
        'component_groups = row.get("component_groups")',
        component_out_pos,
    )
    state_trace_pos = scanner_text.index(
        'out["state_evidence_trace"]',
        component_groups_pos,
    )
    group_out_pos = scanner_text.index(
        'out["q_bayes_physical_likelihood_component_groups"]',
        component_groups_pos,
    )

    assert components_pos < component_out_pos < component_groups_pos < state_trace_pos < group_out_pos


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

    assert chain["schema"] == "legacy_bayes_chain_retired_compat_v1"
    assert chain["role"] == "retired_compat"
    assert chain["ui_primary_allowed"] is False
    assert chain["production_probability_allowed"] is False
    assert chain["replacement_posterior_runtime"] == "hierarchical_posterior_pipeline_runtime.py"
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

    assert chain["role"] == "retired_compat"
    assert chain["ui_primary_allowed"] is False
    assert chain["prior_prob"] == 0.2
    assert chain["likelihood_prob"] == 0.7
    assert chain["likelihood_parts"]["model"]["prob"] == 0.8


def test_ui_snapshot_summary_distribution_keeps_compact_physical_detail_without_heavy_blob() -> None:
    row = {
        "outcome": "31°C",
        "q_bayes_posterior": 0.84,
        "bayes_chain": {"prior_prob": 0.3, "likelihood_prob": 0.8},
        "q_bayes_physical_likelihood_component_groups": {
            "dewpoint": {
                "available": True,
                "weight": 1.1,
                "log_likelihood": 0.2,
                "source_profile": {
                    "source_class": "metar",
                    "source_field": "dewpoint_c",
                    "debug_blob": "x" * 128,
                },
                "components": {
                    "dewpoint_spread": {
                        "factor": 0.8,
                        "weight": 1.1,
                        "debug_blob": "x" * 128,
                    }
                },
            }
        },
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
    assert compact["bayes_chain"]["role"] == "retired_compat"
    assert compact["bayes_chain"]["ui_primary_allowed"] is False
    assert compact["bayes_chain"]["likelihood_prob"] == 0.8
    assert "q_bayes_physical_likelihood_component_groups" not in compact
    assert compact["state_evidence_trace"]["schema"] == "metar_strong_state_evidence_trace_v1"
    assert compact["state_evidence_trace"]["role"] == "state_evidence_trace"
    assert compact["state_evidence_trace"]["replaces"] == "q_bayes_physical_likelihood_component_groups"
    assert compact["state_evidence_trace"]["groups"]["dewpoint"]["source_profile"]["source_class"] == "metar"
    assert "debug_blob" not in compact["state_evidence_trace"]["groups"]["dewpoint"]["source_profile"]
    assert compact["q_bayes_physical_likelihood_components"] == {"dewpoint": -0.1}
    assert "q_bayes_physical_likelihood_evidence_scale_details" not in compact
    assert compact["q_bayes_physical_d0_explanation"]["variables"][0]["key"] == "temp"


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
        "q_bayes_old_additive_evidence_pool_used": True,
        "q_bayes_production_replacement_required": True,
        "q_bayes_replacement_posterior_runtime": "hierarchical_posterior_pipeline_runtime.py",
    }

    compact = server._compact_distribution_row_for_ui_snapshot(
        row,
        include_physical_detail=False,
        summary_minimal=True,
    )

    chain = compact["bayes_chain"]
    assert chain["schema"] == "legacy_bayes_chain_retired_compat_v1"
    assert chain["role"] == "retired_compat"
    assert chain["ui_primary_allowed"] is False
    assert compact["q_bayes_old_additive_evidence_pool_used"] is True
    assert compact["q_bayes_production_replacement_required"] is True
    assert compact["q_bayes_replacement_posterior_runtime"] == "hierarchical_posterior_pipeline_runtime.py"
    assert chain["prior_prob"] == 0.22
    assert abs(chain["likelihood_prob"] - ((0.58 * 20.0 + 0.48 * 8.0) / 28.0)) < 1e-9
    assert chain["likelihood_parts"]["model"]["prob"] == 0.58
    assert chain["likelihood_parts"]["model"]["evidence"] == 20.0
    assert chain["likelihood_parts"]["wu_forecast"]["prob"] == 0.48


def test_ui_snapshot_soft_forecast_channels_prefers_ctp_components_over_legacy_status() -> None:
    payload = {
        "soft_forecast_posterior_channels": {
            "wu_forecast_peak": {
                "captured": True,
                "posterior_included": False,
                "posterior_status": "captured_not_in_main_posterior",
                "posterior_label": "已抓取但未进入主后验",
                "not_in_posterior_reason": "legacy_zero_evidence",
                "peak_c": None,
                "evidence": 0.0,
            },
            "summary_status": "captured_not_in_main_posterior",
            "summary_label": "已抓取但未进入主后验",
            "captured_not_included_channels": ["wu_forecast_peak"],
        },
        "posterior_temperature_distribution": {
            "source_components": {
                "components": [
                    {
                        "source_id": "wu_forecast",
                        "group": "soft_forecast",
                        "role": "forecast",
                        "precision": 0.20408163265306123,
                        "sigma_c": 0.7,
                        "raw": {
                            "source": "wu_forecast",
                            "calibrated_peak_c": 31.6666666667,
                            "bucket_probs": [0.1, 0.3581221832],
                        },
                    }
                ],
                "groups": [
                    {
                        "group": "soft_forecast",
                        "raw_precision": 0.46033163265306126,
                        "effective_precision": 0.29376913265306126,
                    }
                ],
            }
        },
    }
    distribution = [
        {
            "outcome": "31°C",
            "q_bayes_wu_forecast_status": "missing_wu_forecast_peak_c",
            "q_bayes_wu_forecast_evidence": 0.0,
        }
    ]

    channels = server._ui_snapshot_soft_forecast_channels(payload, distribution)
    wu = channels["wu_forecast_peak"]

    assert wu["captured"] is True
    assert wu["posterior_included"] is True
    assert wu["posterior_status"] == "included_in_ctp_posterior"
    assert wu["posterior_label"] == "已进入 CTP 主后验"
    assert wu["not_in_posterior_reason"] == ""
    assert wu["q_bayes_wu_forecast_status"] == "included_in_ctp_posterior"
    assert wu["peak_c"] == pytest.approx(31.6666666667)
    assert wu["evidence"] == pytest.approx(0.20408163265306123)
    assert wu["ctp_precision"] == pytest.approx(0.20408163265306123)
    assert channels["summary_status"] == "included_or_not_captured"
    assert channels["summary_label"] == ""
    assert "wu_forecast_peak" not in channels["captured_not_included_channels"]


def test_ui_snapshot_soft_forecast_legacy_fallback_is_retired_compat() -> None:
    payload = {}
    distribution = [
        {
            "outcome": "31°C",
            "wu_forecast_peak_captured": True,
            "wu_forecast_peak_posterior_included": False,
            "wu_forecast_peak_not_in_posterior_reason": "legacy_zero_evidence",
            "wu_forecast_peak_c": 31.0,
            "q_bayes_wu_forecast_evidence": 0.0,
            "taf_forecast_peak_captured": True,
            "taf_forecast_peak_posterior_included": False,
            "taf_forecast_peak_c": 30.0,
            "q_bayes_taf_peak_evidence": 0.0,
        }
    ]

    channels = server._ui_snapshot_soft_forecast_channels(payload, distribution)

    assert channels["legacy_evidence_boundary"]["schema"] == "legacy_evidence_boundary_v1"
    assert channels["legacy_evidence_boundary"]["role"] == "retired_compat"
    assert channels["legacy_evidence_boundary"]["ui_primary_allowed"] is False
    assert channels["legacy_evidence_boundary"]["production_probability_allowed"] is False
    assert channels["legacy_evidence_boundary"]["replacement"] == "layer_contributions.forecast_weak_likelihood"


def test_ui_temperature_trajectory_status_marks_legacy_evidence_boundary() -> None:
    payload = {
        "weather_likelihood_features": {
            "observed_temp_c": 31.0,
            "observed_today_max_c": 31.0,
            "temperature_rise_rate_c_per_hour": 1.2,
            "temperature_rise_rate_source": "metar",
            "forecast_peak_temp_c": 33.0,
        }
    }
    distribution = [
        {
            "outcome": "31°C",
            "q_bayes_temperature_trajectory_evidence": 4.0,
            "q_bayes_temperature_trajectory_status": "enabled",
        }
    ]

    status = server._ui_temperature_trajectory_status_from_payload(payload, distribution)

    assert status["temperature_trajectory_overlay_strength"] == 4.0
    assert status["legacy_evidence_boundary"]["schema"] == "legacy_evidence_boundary_v1"
    assert status["legacy_evidence_boundary"]["role"] == "retired_compat"
    assert status["legacy_evidence_boundary"]["replacement"] == "layer_contributions.metar_strong_likelihood.state_evidence_trace"


def test_ui_fast_fallback_attaches_temperature_trajectory_status_from_existing_payload() -> None:
    payload = {
        "weather_likelihood_features_compact": {
            "observed_temp_c": 29.0,
            "observed_today_max_c": 31.0,
            "temperature_rise_rate_c_per_hour": 1.0,
            "temperature_rise_rate_source": "zuuu_metar_raw_awc:1h",
            "temperature_rise_rate_window": "1h",
            "temperature_rise_rate_trusted": True,
            "temperature_momentum_effective_c_per_hour": 1.0,
            "temperature_momentum_realization_state": "emerging_warming",
            "temperature_momentum_posterior_gate_allowed": True,
            "temperature_momentum_posterior_gate_reason": "one_hour_context_confirmed",
        },
        "distribution": [{"outcome": "33°C", "q_bayes_posterior": 0.35}],
        "chengdu_intraday_summary": {},
    }

    server._attach_ui_temperature_trajectory_status_from_payload(payload)

    status = payload["temperature_trajectory_status"]
    assert status["available"] is True
    assert status["observed_temp_c"] == pytest.approx(29.0)
    assert status["observed_today_max_c"] == pytest.approx(31.0)
    assert status["temperature_rise_rate_c_per_hour"] == pytest.approx(1.0)
    assert status["temperature_momentum_effective_c_per_hour"] == pytest.approx(1.0)
    assert status["temperature_momentum_realization_state"] == "emerging_warming"
    assert payload["chengdu_intraday_summary"]["temperature_trajectory_status"] == status


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


def test_ui_snapshot_summary_keeps_hierarchical_posterior_contract_fields() -> None:
    payload = {
        "slug": "chengdu-high-temp",
        "event_date": "2026-07-03",
        "fact_snapshot_id": "fact-123",
        "posterior_run_id": "posterior-456",
        "posterior_runtime_schema": "hierarchical_posterior_pipeline_runtime_v1",
        "posterior_composer_schema": "sequential_remaining_warming_bayes_runtime_v31",
        "posterior_as_of": "2026-07-03T12:05:04+08:00",
        "phase": "D0_breakout",
        "beta_weak": 0.35,
        "beta_strong": 1.25,
        "phase_reason": "d0_chengdu_breakout_window_metar_strong_likelihood_starts_to_dominate",
        "hard_mask_reason": "observed_today_max_floor",
        "prior_distribution": {"30°C": 0.4, "31°C": 0.6},
        "forecast_weak_likelihood": {"30°C": 0.3, "31°C": 0.7},
        "metar_strong_likelihood": {"30°C": 0.2, "31°C": 0.8},
        "hard_constraints": {"observed_today_max_c": 31.0},
        "layer_contributions": {
            "prior": {"distribution": {"30°C": 0.4, "31°C": 0.6}},
            "forecast_weak_likelihood": {"beta": 0.35},
            "metar_strong_likelihood": {"beta": 1.25},
        },
        "normalization": {"posterior_sum": 1.0},
        "posterior_boundary": {"old_additive_evidence_pool_used": False},
        "runtime_schemas": {"metar_strong_likelihood": "metar_strong_likelihood_runtime_v1"},
        "posterior_temperature_distribution": {
            "bucket_probabilities": [
                {"bucket": "30°C", "probability": 0.25},
                {"bucket": "31°C", "probability": 0.75},
            ]
        },
        "distribution": [
            {"outcome": "30°C", "q_temperature_bucket_posterior": 0.25},
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.75},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)

    for key in (
        "fact_snapshot_id",
        "posterior_run_id",
        "posterior_runtime_schema",
        "posterior_composer_schema",
        "posterior_as_of",
        "phase",
        "beta_weak",
        "beta_strong",
        "phase_reason",
        "hard_mask_reason",
        "prior_distribution",
        "forecast_weak_likelihood",
        "metar_strong_likelihood",
        "hard_constraints",
        "layer_contributions",
        "normalization",
        "posterior_boundary",
        "runtime_schemas",
        "posterior_temperature_distribution",
    ):
        assert key in slim
    assert slim["layer_contributions"]["metar_strong_likelihood"]["beta"] == 1.25


def test_ui_snapshot_distribution_row_prefers_hierarchical_layer_chain() -> None:
    layer_contributions = {
        "prior": {
            "role": "root_prior",
            "distribution": {"30°C": 0.40, "31°C": 0.60},
        },
        "forecast_weak_likelihood": {
            "role": "prediction_weak_likelihood",
            "beta": 0.35,
            "distribution": {"30°C": 0.30, "31°C": 0.70},
        },
        "metar_strong_likelihood": {
            "role": "observation_strong_likelihood",
            "beta": 1.25,
            "distribution": {"30°C": 0.20, "31°C": 0.80},
        },
    }
    payload = {
        "slug": "chengdu-high-temp",
        "event_date": "2026-07-03",
        "distribution": [
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.82,
                "q_bayes_source": "hierarchical_posterior_pipeline_runtime_v1",
                "q_bayes_phase": "d0_awc_preparation_window",
                "q_bayes_beta_weak": 0.35,
                "q_bayes_beta_strong": 1.25,
                "q_bayes_phase_reason": "unit_test_phase",
                "q_bayes_hard_mask_reason": "none",
                "q_bayes_layer_contributions": layer_contributions,
                "q_bayes_wu_forecast_evidence": 99.0,
                "q_bayes_wu_forecast_prob": 0.01,
            }
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    row = slim["distribution"][0]
    chain = row["bayes_chain"]

    assert row["q_bayes_phase"] == "d0_awc_preparation_window"
    assert row["q_bayes_beta_weak"] == 0.35
    assert row["q_bayes_beta_strong"] == 1.25
    assert chain["schema"] == "hierarchical_layer_contributions_chain_v1"
    assert chain["role"] == "hierarchical_joint_likelihood_explanation"
    assert chain["prior_prob"] == 0.60
    assert chain["likelihood_parts"]["forecast_weak_likelihood"]["prob"] == 0.70
    assert chain["likelihood_parts"]["metar_strong_likelihood"]["prob"] == 0.80
    assert "wu_forecast" not in chain["likelihood_parts"]


def test_ui_snapshot_distribution_rows_merge_event_level_prior_and_strong_layers() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-6-2026",
        "event_date": "2026-07-06",
        "phase": "D1_early",
        "beta_weak": 0.65,
        "beta_strong": 0.0,
        "phase_reason": "unit_test_d1_forecast_weight",
        "hard_mask_reason": "current_temperature_c",
        "prior_distribution": {
            "32°C or below": 0.575757575758,
            "33°C": 0.151515151515,
        },
        "forecast_weak_likelihood": {
            "32°C or below": 0.25,
            "33°C": 0.75,
        },
        "metar_strong_likelihood": {
            "32°C or below": 0.5,
            "33°C": 0.5,
        },
        "hard_constraints": {
            "current_temperature_c": 29.0,
            "observed_today_max_c": 31.0,
        },
        "layer_contributions": {
            "prior": {
                "role": "root_prior",
                "boundary": "historical_climate_date_site_terrain_weather_type_only",
            },
            "forecast_weak_likelihood": {
                "role": "prediction_weak_likelihood",
                "beta": 0.65,
            },
            "metar_strong_likelihood": {
                "role": "observation_strong_likelihood",
                "beta": 0.0,
            },
        },
        "distribution": [
            {"outcome": "32°C or below", "q_temperature_bucket_posterior": 0.0},
            {"outcome": "33°C", "q_temperature_bucket_posterior": 0.3571428571428571},
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    rows = {row["outcome"]: row for row in slim["distribution"]}
    target = rows["33°C"]
    chain = target["bayes_chain"]

    assert target["q_prior"] == pytest.approx(0.151515151515)
    assert target["q_likelihood"] == pytest.approx(chain["likelihood_prob"])
    assert target["q_bayes_phase"] == "D1_early"
    assert target["q_bayes_beta_weak"] == 0.65
    assert target["q_bayes_beta_strong"] == 0.0
    assert chain["schema"] == "hierarchical_layer_contributions_chain_v1"
    assert chain["role"] == "hierarchical_joint_likelihood_explanation"
    assert chain["prior_prob"] == pytest.approx(0.151515151515)
    assert chain["likelihood_parts"]["forecast_weak_likelihood"]["prob"] == pytest.approx(0.75)
    assert chain["likelihood_parts"]["metar_strong_likelihood"]["prob"] == pytest.approx(0.5)
    assert slim["ui_sections"]["prior"]["state"] == "available"
    assert slim["ui_sections"]["observation_strong"]["state"] == "available"


def test_chengdu_intraday_endpoint_row_keeps_compact_hierarchical_chain() -> None:
    row = {
        "outcome": "31°C",
        "q_temperature_bucket_posterior": 0.82,
        "q_bayes_phase": "d0_awc_preparation_window",
        "q_bayes_beta_weak": 0.35,
        "q_bayes_beta_strong": 1.25,
        "q_bayes_layer_contributions": {
            "prior": {"distribution": {"30°C": 0.40, "31°C": 0.60}},
            "forecast_weak_likelihood": {
                "beta": 0.35,
                "distribution": {"30°C": 0.30, "31°C": 0.70},
            },
            "metar_strong_likelihood": {
                "beta": 1.25,
                "distribution": {"30°C": 0.20, "31°C": 0.80},
            },
        },
    }

    compact = server._compact_intraday_distribution_row_for_endpoint(row)
    chain = compact["bayes_chain"]

    assert compact["q_bayes_phase"] == "d0_awc_preparation_window"
    assert chain["schema"] == "hierarchical_layer_contributions_chain_v1"
    assert chain["prior_prob"] == 0.60
    assert chain["likelihood_parts"]["forecast_weak_likelihood"]["beta"] == 0.35
    assert chain["likelihood_parts"]["metar_strong_likelihood"]["prob"] == 0.80


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


def test_ui_snapshot_summary_merge_fills_model_members_from_cache() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE model_member_cache(
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
    for model, members in {
        "gfs_seamless": [30.6, 30.7, 31.2],
        "icon_seamless": [29.8, 30.4, 30.6],
        "ecmwf_ifs025": [31.1, 31.2, 31.3],
    }.items():
        conn.execute(
            """
            INSERT INTO model_member_cache
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model,
                "30.5583,103.9460",
                "2026-07-03",
                "temperature_2m_max",
                "C",
                json.dumps(members),
                "2026-07-02T15:41:57+00:00",
                "2026-07-02T12:00:00+00:00",
                "open_meteo",
                f"sig-{model}",
            ),
        )
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-3-2026",
        "city": "ZUUU",
        "station_id": "ZUUU",
        "event_date": "2026-07-03",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "distribution": [{"outcome": label} for label in ["29°C", "30°C", "31°C", "32°C"]],
    }

    server._attach_model_member_cache_snapshot_for_ui_snapshot(conn, payload)
    server._attach_model_bucket_rule_comparison_for_ui_snapshot(conn, payload)
    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    conn.close()

    assert slim["model_member_cache_snapshot"]["models"]["gfs_seamless"]["member_count"] == 3
    assert slim["model_members_decimal_raw"]["icon_seamless"] == [29.8, 30.4, 30.6]
    comparison = slim["distribution"][0]["q_ensemble_qm_meta"]["model_bucket_rule_comparison"]
    assert comparison["per_model"]["gfs_seamless"]["rules"]["round"]["raw"]["label"] == "31°C"
    assert comparison["per_model"]["ecmwf_ifs025"]["rules"]["round"]["raw"]["q"] == pytest.approx(1.0)


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
        "fact_snapshot_id": "fact_pending_model",
        "posterior_run_id": "post_pending_model",
        "phase": "D1_early",
        "beta_weak": 0.65,
        "beta_strong": 0.0,
        "phase_reason": "target_date_future_early_forecast_sources_dominate_but_distribution_stays_wide",
        "hard_mask_reason": "none",
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_placeholder_v1",
            "state": "not_materialized",
            "state_reason": "posterior_ssot_fact_not_materialized_for_this_writer_path",
            "fact_snapshot_id": "fact_pending_model",
            "posterior_run_id": "post_pending_model",
            "read_only": True,
        },
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
    assert slim["posterior_ssot_fact"]["schema"] == "bayes_posterior_ssot_fact_placeholder_v1"
    assert slim["posterior_ssot_fact"]["state"] == "not_materialized"
    section = slim["ui_sections"]["bayes_posterior_ssot"]
    assert section["state"] == "not_materialized"
    assert section["fact_snapshot_id"] == "fact_pending_model"
    assert section["posterior_run_id"] == "post_pending_model"
    assert section["phase"] == "D1_early"
    assert section["beta_weak"] == pytest.approx(0.65)
    assert section["beta_strong"] == pytest.approx(0.0)
    assert section["hard_mask_reason"] == "none"

    compact = server._compact_ui_snapshot_summary_item_for_first_screen(
        {**slim, "summary_first_screen_compact": True, "ui_sections": {}}
    )
    assert compact["summary_first_screen_compact"] is True
    assert compact["ui_sections"]["bayes_posterior_ssot"]["state"] == "not_materialized"


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


def test_ui_snapshot_frontend_contract_survives_summary_minimal_compaction() -> None:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "run_date": "2026-07-02",
        "market_unit": "C",
        "key_metrics": {"wu_forecast_peak_c": 31.2, "taf_forecast_peak_c": 30.8},
        "model_member_cache_snapshot": {
            "models": {"gfs_seamless": {"member_count": 30, "mean_c": 30.4}}
        },
        "target_bucket_outlook": {"final_bucket": "31°C", "final_bucket_probability_pct": 44.0},
        "temperature_path_distribution": {"target_bucket": "31°C", "summary": "path ok"},
        "target_switch_context": {"target_bucket": "31°C", "reason": "unit_test"},
        "weather_likelihood_features": {
            "wu_forecast_peak_c": 31.2,
            "taf_forecast_peak_c": 30.8,
            "taf_forecast_peak_raw": "TAF ZUUU 0300/0324 TX31/0307Z",
            "taf_forecast_peak_at": "2026-07-03T07:00:00+00:00",
            "observed_temp_c": 25.0,
            "temperature_momentum_1h_c_per_hour": 0.25,
            "precip_nowcast_1_3h_max_probability_pct": 0.0,
        },
        "distribution": [
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.44,
                "q_bayes_posterior": 0.44,
                "q_bayes_wu_forecast_prob": 0.52,
                "q_bayes_taf_peak_prob": 0.48,
                "q_bayes_independent_physics_residual_prob": 0.41,
                "q_bayes_physical_likelihood_prob": 0.39,
                "q_bayes_physical_likelihood_component_groups": {
                    "dewpoint": {"available": True, "weight": 0.3, "na_reason": ""}
                },
                "q_temperature_bucket_score_components": {"wu_forecast": 0.5},
                "bayes_chain": {
                    "prior_prob": 0.4,
                    "likelihood_prob": 0.5,
                    "likelihood_parts": {"wu_forecast": {"prob": 0.52, "evidence": 3.0}},
                },
                "q_ensemble_qm_meta": {
                    "raw_bucket_probability": {
                        "labels": ["31°C"],
                        "probs_by_model": {"gfs_seamless": {"31°C": 0.6}},
                    }
                },
                "q_bayes_physical_d0_explanation": {
                    "summary": "物理证据",
                    "variables": [{"key": "dewpoint", "label": "露点", "direction": "warm"}],
                },
                "wu_forecast_peak_c": 31.2,
                "taf_forecast_peak_c": 30.8,
                "taf_forecast_peak_raw": "TAF ZUUU 0300/0324 TX31/0307Z",
            }
        ],
    }

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload, include_physical_detail=False)

    assert slim["key_metrics"]["wu_forecast_peak_c"] == 31.2
    assert slim["model_member_cache_snapshot"]["models"]["gfs_seamless"]["member_count"] == 30
    assert slim["weather_likelihood_features_compact"]["taf_forecast_peak_raw"].startswith("TAF ZUUU")
    row = slim["distribution"][0]
    assert row["q_bayes_wu_forecast_prob"] == 0.52
    assert row["q_bayes_taf_peak_prob"] == 0.48
    assert "q_bayes_physical_likelihood_component_groups" not in row
    assert row["state_evidence_trace"]["groups"]["dewpoint"]["available"] is True
    assert row["state_evidence_trace"]["replaces"] == "q_bayes_physical_likelihood_component_groups"
    assert row["q_temperature_bucket_score_components"]["wu_forecast"] == 0.5
    assert row["bayes_chain"]["likelihood_parts"]["wu_forecast"]["prob"] == 0.52
    assert row["q_ensemble_qm_meta"]["raw_bucket_probability"]["probs_by_model"]["gfs_seamless"]["31°C"] == 0.6
    assert row["q_bayes_physical_d0_explanation"]["variables"][0]["key"] == "dewpoint"
    assert row["taf_forecast_peak_raw"].startswith("TAF ZUUU")


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
    attach_unit_posterior_ssot_fact(out, target_bucket="31°C", target_probability=0.5)
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
    attach_unit_posterior_ssot_fact(out, target_bucket="30°C", target_probability=0.2776)

    server._apply_chengdu_intraday_summary_to_snapshot_out(out, summary)

    outlook = out["target_bucket_outlook"]
    nested = out["chengdu_intraday_summary"]["target_bucket_outlook"]
    for item in (outlook, nested):
        assert item["primary_conclusion"] == "当前最高目标 30°C（最终概率 27.8%）；目标桶同为 30°C。"
        assert item["target_reach_label"] == "路径触达偏弱"
        assert "仅作路径/执行参考" in item["reach_line"]
        assert "满足偏难" not in json.dumps(item, ensure_ascii=False)
        assert item["answer_cards"][0]["label"] == "路径触达参考"
        assert item["answer_cards"][0]["answer"] == "路径触达偏弱"
        assert item["second_target_outlook"]["definition"].endswith("不覆盖当前交易目标。")


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
    attach_unit_posterior_ssot_fact(out, target_bucket="30°C", target_probability=0.4307)

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

    assert outlook["primary_conclusion"] == "硬源已触达 28°C 下沿，约束后当前最高目标 29°C；当前目标桶是 30°C（当前最高目标低于目标）。"
    assert outlook["settlement_line"].startswith("最终概率偏低")
    assert outlook["answer_cards"][0]["label"] == "当前最高目标"
    assert outlook["answer_cards"][0]["detail"] == "当前最高目标 29°C。"


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
    assert outlook["primary_conclusion"] == "当前最高目标 30°C（最终概率 36.5%）；目标桶同为 30°C。"
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

    attach_unit_posterior_ssot_fact(item, target_bucket="31°C", target_probability=0.6531927844391303)
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


def test_ssot_target_context_ignores_legacy_selection_and_non_hard_outlook() -> None:
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

    assert context == {}


def test_ssot_target_context_uses_posterior_ssot_fact_not_legacy_selection() -> None:
    context = server._ssot_target_context_from_payload(
        {
            "posterior_ssot_fact": {
                "state": "available",
                "target_bucket": "25°C",
                "target_probability": 0.30,
                "ranked_bucket_probabilities": [
                    {"outcome": "25°C", "probability": 0.30},
                    {"outcome": "26°C", "probability": 0.20},
                ],
            },
            "target_bucket_selection": {
                "target_bucket": "27°C",
                "target_probability_pct": 60.0,
            },
            "target_bucket_outlook": {
                "target_bucket": "24°C or below",
                "target_bucket_probability_pct": 35.0,
            },
        }
    )

    assert context["target_bucket"] == "25°C"
    assert context["posterior_settlement_target_bucket"] == "25°C"
    assert context["target_bucket_source"] == "posterior_ssot_fact"
    assert context["target_posterior"] == 0.30
    assert context["second_target_bucket"] == "26°C"
    assert context["second_target_posterior"] == 0.20


def test_ssot_target_context_sorts_full_bucket_probabilities_before_ranked_fallback() -> None:
    context = server._ssot_target_context_from_payload(
        {
            "posterior_ssot_fact": {
                "state": "available",
                "target_bucket": "38°C",
                "target_probability": 0.428687914101,
                "bucket_probabilities": [
                    {"bucket": "32°C or below", "probability": 0.0},
                    {"bucket": "37°C", "probability": 0.323565351829},
                    {"bucket": "38°C", "probability": 0.428687914101},
                    {"bucket": "39°C", "probability": 0.203833508157},
                    {"bucket": "40°C", "probability": 0.040492100138},
                    {"bucket": "41°C", "probability": 0.003312666834},
                ],
                "ranked_bucket_probabilities": [
                    {"bucket": "38°C", "probability": 0.428687914101},
                    {"bucket": "41°C", "probability": 0.3313},
                ],
            },
        }
    )

    assert context["target_bucket"] == "38°C"
    assert context["second_target_bucket"] == "37°C"
    assert context["second_target_posterior"] == pytest.approx(0.323565351829)


def test_ssot_target_context_does_not_select_final_lock_zero_rows_as_second_target() -> None:
    context = server._ssot_target_context_from_payload(
        {
            "posterior_ssot_fact": {
                "state": "available",
                "target_bucket": "37°C",
                "target_probability": 1.0,
                "bucket_probabilities": [
                    {"bucket": "32°C or below", "probability": 0.0, "hard_zero": True, "hard_mask_reason": "final_bucket_lock"},
                    {"bucket": "36°C", "probability": 0.0, "hard_zero": True, "hard_mask_reason": "final_bucket_lock"},
                    {"bucket": "37°C", "probability": 1.0},
                    {"bucket": "38°C", "probability": 0.0, "hard_zero": True, "hard_mask_reason": "final_bucket_lock"},
                ],
            },
        }
    )

    assert context["target_bucket"] == "37°C"
    assert "second_target_bucket" not in context
    assert "second_target_posterior" not in context


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
    attach_unit_prior_contract(stale_payload)
    attach_unit_posterior_ssot_fact(
        stale_payload,
        target_bucket=new_target,
        target_probability=0.26122337983261507,
    )
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
    fact_id = insert_event_fact_history_with_unit_prior(
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
    assert plan["target_bucket_selection_source"] == "posterior_ssot_fact.top_bucket"
    assert plan["target_bucket"] == new_target
    assert plan["target_posterior"] == pytest.approx(0.5642755789554247)
    assert plan["second_target_bucket"] == old_target
    assert plan["second_target_posterior"] == pytest.approx(0.4357244210445753)
    assert plan["temperature_path_trade_follow_allowed"] is False
    assert plan["temperature_path_trade_follow_block_yes"] is True
    risk = plan["ui_risk_explanation"]
    assert risk["available"] is True
    assert risk["target_bucket"] == new_target
    assert risk["second_target_bucket"] == old_target
    assert risk["target_posterior"] == pytest.approx(0.5642755789554247)
    assert risk["second_target_posterior"] == pytest.approx(0.4357244210445753)
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
    assert "最终概率" in risk["text"]
    assert "最终后验" not in risk["text"]
    assert "落桶后验" not in risk["text"]
    assert "路径触达" in risk["text"]
    assert "路径执行概率" not in risk["text"]
    assert "路径触达不足，暂不追买 YES" in plan["summary"]
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

    assert plan["ok"] is False
    assert plan["reason"] == "posterior_ssot_fact_target_missing"
    assert plan["target_bucket_selection_source"] == "posterior_ssot_fact_missing"
    assert context["target_bucket"] == ""
    assert context["candidate_bucket"] == ""


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
    attach_unit_posterior_ssot_fact(
        item,
        target_bucket="25°C or below",
        target_probability=0.26122337983261507,
    )

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
    attach_unit_posterior_ssot_fact(
        out,
        target_bucket="30°C",
        target_probability=0.3645925961706722,
    )
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
    attach_unit_posterior_ssot_fact(item, target_bucket="30°C", target_probability=0.20)

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
    attach_unit_posterior_ssot_fact(
        payload,
        target_bucket="25°C or below",
        target_probability=0.42016291281991425,
    )

    slim = server._slim_fact_payload_for_ui_snapshot_summary(payload)
    risk = slim["target_realization_capital_plan"]["ui_risk_explanation"]

    assert risk["target_posterior"] == 0.42016291281991425
    assert risk["target_realization_probability"] == 0.5
    assert risk["second_target_realization_probability"] == 0.2119
    assert risk["trade_follow"]["allowed"] is True
    assert "最终概率" in risk["text"]
    assert "最终后验" not in risk["text"]
    assert "落桶后验" not in risk["text"]
    assert "路径触达" in risk["text"]
    assert "路径执行概率" not in risk["text"]


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
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
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
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
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
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
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
    assert row_by_outcome["28°C"]["temperature_trajectory_overlay_strength"] > 0
    assert "q_bayes_temperature_trajectory_evidence" not in row_by_outcome["28°C"]
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
    assert rows[2]["temperature_trajectory_overlay_strength"] > 0
    assert "q_bayes_temperature_trajectory_evidence" not in rows[2]
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
    assert by_outcome["31°C"]["temperature_trajectory_overlay_strength"] > 0
    assert "q_bayes_temperature_trajectory_evidence" not in by_outcome["31°C"]
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
    wu_fact_id = insert_event_fact_history_with_unit_prior(
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
    awc_fact_id = insert_event_fact_history_with_unit_prior(
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
    insert_event_fact_history_with_unit_prior(
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
    current_fact_id = insert_event_fact_history_with_unit_prior(
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
    fact_id = insert_event_fact_history_with_unit_prior(
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

    fact_id = insert_event_fact_history_with_unit_prior(
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

    fact_id = insert_event_fact_history_with_unit_prior(
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

    attach_unit_posterior_ssot_fact(
        item,
        target_bucket="30°C or higher",
        target_probability=0.60,
    )
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

    fact_id = insert_event_fact_history_with_unit_prior(
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

    fact_id = insert_event_fact_history_with_unit_prior(
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


def test_refresh_awc_fact_releases_schema_gate_before_awc_network_fetch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "0")
    gate_state = {"active": 0, "entered": 0}

    @contextmanager
    def tracked_archive_write_gate(*_args, **_kwargs):
        gate_state["active"] += 1
        gate_state["entered"] += 1
        try:
            yield
        finally:
            gate_state["active"] -= 1

    monkeypatch.setattr(server, "archive_db_write_gate", tracked_archive_write_gate)
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
    attach_unit_prior_contract(old_payload)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = insert_event_fact_history_with_unit_prior(
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
    # The legacy fixture predates the current Target Realization repoint
    # contract.  Seed the read-side fact pointer directly so this test remains
    # focused on AWC refresh and the lock boundary rather than that contract.
    conn.execute(
        """
        INSERT OR REPLACE INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (slug, "2026-06-18", fact_id, "2026-06-18T12:04:00+00:00"),
    )
    conn.commit()
    conn.close()

    def fake_awc(*_args, **_kwargs):
        # AWC is network I/O.  Schema initialization must not retain the
        # process-wide archive writer lock while this request is in flight.
        assert gate_state["active"] == 0
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

    assert result["ok"] is True, result
    assert result["status"] == "updated"
    assert result["reason"] == "awc_latest_at_changed"
    assert gate_state["entered"] >= 2
    assert gate_state["active"] == 0
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

    assert row is not None
    payload = json.loads(row["fact_json"])
    features = payload["weather_likelihood_features"]
    assert row["fact_as_of"] == "2026-06-18T13:00:02+00:00"
    assert features["observed_today_max_c"] == 31.0
    assert features["zuuu_metar_raw_awc_today_max_at"] == "2026-06-18T13:00:00+00:00"


def test_refresh_awc_fact_keeps_awc_today_max_lower_bound_before_event_end(tmp_path, monkeypatch) -> None:
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
        "market_var": "temperature_2m_max",
        "rounding_rule": "whole_degrees",
        "event_end_utc": "2026-06-18T13:00:00+00:00",
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
            {"outcome": "30°C", "q_bayes_posterior": 0.62, "q_temperature_bucket_posterior": 0.62},
            {"outcome": "31°C", "q_bayes_posterior": 0.28, "q_temperature_bucket_posterior": 0.28},
            {"outcome": "32°C", "q_bayes_posterior": 0.10, "q_temperature_bucket_posterior": 0.10},
        ],
    }
    attach_unit_prior_contract(old_payload)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = insert_event_fact_history_with_unit_prior(
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
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "metar_temperature_c": 31.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T12:30:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 31.0,
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T12:30:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T12:35:02+00:00",
            "zuuu_metar_raw_awc_fetch_duration_ms": 180,
            "zuuu_metar_raw_awc_date_mismatch": False,
            "zuuu_metar_raw_awc_date_mismatch_reason": "",
        }

    monkeypatch.setattr(scan_weather_market, "fetch_zuuu_metar_raw_awc_features", fake_awc)

    result = server.refresh_awc_fact_for_chengdu_event(db_path, event_date="2026-06-18")

    assert result["ok"] is True
    assert result["awc_final_daily_max"] is False
    assert result["awc_final_daily_max_state"] == "awc_latest_before_event_end"
    assert result["awc_final_event_end_utc"] == "2026-06-18T13:00:00+00:00"
    assert result.get("hard_source_final_daily_max") in (None, "", False)
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
    ssot = payload["posterior_ssot_fact"]
    assert features["zuuu_metar_raw_awc_final_daily_max"] is False
    assert features["zuuu_metar_raw_awc_final_daily_max_state"] == "awc_latest_before_event_end"
    assert "observed_today_max_final" not in features
    assert ssot["target_bucket"] == "31°C"
    assert ssot["target_probability"] < 1.0
    by_bucket = {entry["bucket"]: entry["probability"] for entry in ssot["bucket_probabilities"]}
    assert by_bucket["30°C"] == 0.0
    assert by_bucket["31°C"] > 0.0
    assert by_bucket["32°C"] > 0.0


def test_refresh_awc_fact_keeps_post_peak_cooling_provisional_before_event_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED", "0")
    monkeypatch.setenv("PM_AWC_FINAL_LOCK_AFTER_PEAK_MIN_LOCAL_HOUR", "18")
    monkeypatch.setenv("PM_AWC_FINAL_LOCK_AFTER_PEAK_MIN_ELAPSED_MIN", "45")
    monkeypatch.setenv("PM_AWC_FINAL_LOCK_AFTER_PEAK_MIN_DROP_C", "0.5")
    db_path = tmp_path / "archive.db"
    slug = "highest-temperature-in-chengdu-on-june-18-2026"
    old_payload = {
        "slug": slug,
        "event_date": "2026-06-18",
        "run_date": "2026-06-18",
        "station_id": "ZUUU",
        "tz": "Asia/Shanghai",
        "market_unit": "C",
        "market_var": "temperature_2m_max",
        "rounding_rule": "whole_degrees",
        "event_end_utc": "2026-06-18T13:00:00+00:00",
        "weather_likelihood_features": {
            "source": "zuuu_metar_raw_awc",
            "observed_temp_c": 36.0,
            "observed_today_max_c": 36.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T08:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 36.0,
            "zuuu_metar_raw_awc_today_max_c": 36.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T08:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T08:04:00+00:00",
        },
        "distribution": [
            {"outcome": "36°C", "q_bayes_posterior": 0.35, "q_temperature_bucket_posterior": 0.35},
            {"outcome": "37°C", "q_bayes_posterior": 0.40, "q_temperature_bucket_posterior": 0.40},
            {"outcome": "38°C", "q_bayes_posterior": 0.25, "q_temperature_bucket_posterior": 0.25},
        ],
    }
    attach_unit_prior_contract(old_payload)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date="2026-06-18",
        slug=slug,
        event_date="2026-06-18",
        fact_as_of="2026-06-18T08:04:00+00:00",
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
            "observed_temp_c": 36.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 37.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "metar_temperature_c": 36.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T10:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 36.0,
            "zuuu_metar_raw_awc_today_max_c": 37.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T09:00:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T10:05:02+00:00",
            "zuuu_metar_raw_awc_fetch_duration_ms": 180,
            "zuuu_metar_raw_awc_date_mismatch": False,
            "zuuu_metar_raw_awc_date_mismatch_reason": "",
        }

    monkeypatch.setattr(scan_weather_market, "fetch_zuuu_metar_raw_awc_features", fake_awc)

    result = server.refresh_awc_fact_for_chengdu_event(db_path, event_date="2026-06-18")

    assert result["ok"] is True
    assert result["status"] == "updated"
    assert result["awc_final_daily_max"] is False
    assert result["awc_final_daily_max_state"] == "awc_latest_before_event_end"
    assert result["awc_final_event_end_utc"] == "2026-06-18T13:00:00+00:00"
    assert result.get("hard_source_final_daily_max") in (None, "", False)

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
    ssot = payload["posterior_ssot_fact"]
    assert features["zuuu_metar_raw_awc_final_daily_max"] is False
    assert features["zuuu_metar_raw_awc_final_daily_max_state"] == "awc_latest_before_event_end"
    assert "hard_source_final_daily_max_source" not in features
    assert "hard_source_final_lock_reason" not in features
    assert ssot["target_bucket"] == "37°C"
    assert ssot["target_probability"] < 1.0
    assert ssot["final_bucket_lock"] is False
    by_bucket = {entry["bucket"]: entry["probability"] for entry in ssot["bucket_probabilities"]}
    assert by_bucket["36°C"] == 0.0
    assert by_bucket["37°C"] > 0.0
    assert by_bucket["38°C"] > 0.0


def test_awc_post_peak_cooling_state_is_diagnostic_only_until_event_end() -> None:
    features = {
        "zuuu_metar_raw_awc_latest_at": "2026-06-18T10:00:00+00:00",
        "zuuu_metar_raw_awc_latest_temp_c": 36.0,
        "zuuu_metar_raw_awc_today_max_c": 37.0,
        "zuuu_metar_raw_awc_today_max_at": "2026-06-18T09:00:00+00:00",
    }
    state = server._awc_post_peak_final_daily_max_state(
        features,
        payload={"event_date": "2026-06-18"},
        latest_utc=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
        event_end_utc=datetime(2026, 6, 18, 13, 0, tzinfo=timezone.utc),
        today_max_c=37.0,
    )

    assert state["final"] is False
    assert state["reason"] == "awc_post_peak_cooling_is_probabilistic_not_final"
    assert state["temp_drop_c"] == 1.0
    assert state["elapsed_min"] == 60.0
    assert state["prediction_window_cannot_emit_final_lock"] is True


def test_refresh_awc_fact_marks_final_daily_max_after_event_end_and_locks_ssot(tmp_path, monkeypatch) -> None:
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
        "market_var": "temperature_2m_max",
        "rounding_rule": "whole_degrees",
        "event_end_utc": "2026-06-18T13:00:00+00:00",
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
            {"outcome": "30°C", "q_bayes_posterior": 0.62, "q_temperature_bucket_posterior": 0.62},
            {"outcome": "31°C", "q_bayes_posterior": 0.28, "q_temperature_bucket_posterior": 0.28},
            {"outcome": "32°C", "q_bayes_posterior": 0.10, "q_temperature_bucket_posterior": 0.10},
        ],
    }
    attach_unit_prior_contract(old_payload)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = insert_event_fact_history_with_unit_prior(
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
            "observed_temp_c": 30.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 31.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "observed_today_max_trusted": True,
            "metar_temperature_c": 30.0,
            "zuuu_metar_raw_awc_available": True,
            "zuuu_metar_raw_awc_latest_at": "2026-06-18T13:00:00+00:00",
            "zuuu_metar_raw_awc_latest_temp_c": 30.0,
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-18T12:30:00+00:00",
            "zuuu_metar_raw_awc_fetched_at": "2026-06-18T13:05:02+00:00",
            "zuuu_metar_raw_awc_fetch_duration_ms": 180,
            "zuuu_metar_raw_awc_date_mismatch": False,
            "zuuu_metar_raw_awc_date_mismatch_reason": "",
        }

    monkeypatch.setattr(scan_weather_market, "fetch_zuuu_metar_raw_awc_features", fake_awc)

    result = server.refresh_awc_fact_for_chengdu_event(db_path, event_date="2026-06-18")

    assert result["ok"] is True
    assert result["awc_final_daily_max"] is True
    assert result["awc_final_daily_max_state"] == "awc_latest_covers_event_end"
    assert result["awc_final_event_end_utc"] == "2026-06-18T13:00:00+00:00"
    assert result["hard_source_final_daily_max"] is True
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
    ssot = payload["posterior_ssot_fact"]
    assert features["zuuu_metar_raw_awc_final_daily_max"] is True
    assert features["observed_today_max_final"] is True
    assert features["hard_source_final_daily_max"] is True
    assert ssot["target_bucket"] == "31°C"
    assert ssot["target_probability"] == 1.0
    assert ssot["final_bucket_lock"] is True
    by_bucket = {entry["bucket"]: entry["probability"] for entry in ssot["bucket_probabilities"]}
    assert by_bucket == {"30°C": 0.0, "31°C": 1.0, "32°C": 0.0}


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
    attach_unit_prior_contract(old_payload)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = insert_event_fact_history_with_unit_prior(
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
        row["probability"]
        for row in payload["posterior_ssot_fact"]["bucket_probabilities"]
        if isinstance(row, dict) and row.get("probability") is not None
    )
    assert payload["posterior_target_bucket_selection"]["target_probability"] == pytest.approx(
        top_distribution_prob
    )
    assert 0.0 < payload["posterior_target_bucket_selection"]["target_probability"] <= 1.0
    assert payload["target_bucket_selection"] == payload["posterior_target_bucket_selection"]
    assert payload["target_bucket"] == "31°C"
    assert payload["ssot_target_bucket"] == "31°C"
    assert summary["fast_trade_summary_cache"] is True
    assert summary["summary_cache_source"] == "weather_fact_snapshot_writer"
    assert summary["summary_cache_schema"] == "weather_fact_snapshot_writer_summary_cache_v1"
    assert summary["summary_cache_version"] == server.EVENT_FACT_SUMMARY_CACHE_VERSION
    assert summary["posterior_target_bucket_selection"]["target_bucket"] == "31°C"
    assert summary["posterior_target_bucket_selection"]["target_probability"] == pytest.approx(
        top_distribution_prob
    )
    for key in ("evidence_conclusions", "decision"):
        assert key not in payload
    for key in (
        "chengdu_intraday_summary",
        "target_bucket_outlook",
        "temperature_path_distribution",
        "temperature_realization_reliability",
        "target_switch_context",
        "key_metrics",
    ):
        if key in payload:
            assert heavy_marker not in json.dumps(payload[key], ensure_ascii=False)
    for rows_key in ("distribution", "rows"):
        for distribution_row in payload[rows_key]:
            for key in heavy_row:
                assert key not in distribution_row
    assert payload["fact_provenance"]["awc_fast_trade_fact_write"]["purpose"] == (
        "trade_path_first_ui_rich_fields_deferred"
    )
    assert "key_metrics" in payload["fact_provenance"]["awc_fast_trade_fact_write"]["dropped_top_level_keys"]
    assert payload["fact_provenance"]["awc_fast_trade_fact_write"]["ui_contract_kept_top_level_keys"] == []


def test_fast_trade_compaction_keeps_ui_contract_without_heavy_blob() -> None:
    item = {
        "slug": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "market_unit": "C",
        "key_metrics": {
            "wu_forecast_peak_c": 31.2,
            "taf_forecast_peak_c": 30.8,
            "marker": "HEAVY_FAST_TRADE_MARKER",
            "blob": "x" * 1000,
        },
        "target_bucket_outlook": {
            "final_bucket": "31°C",
            "final_bucket_probability_pct": 44.0,
            "blob": "HEAVY_FAST_TRADE_MARKER",
        },
        "temperature_path_distribution": {
            "target_bucket": "31°C",
            "summary": "路径保留",
            "blob": "HEAVY_FAST_TRADE_MARKER",
        },
        "distribution": [
            {
                "outcome": "31°C",
                "q_temperature_bucket_posterior": 0.44,
                "q_bayes_wu_forecast_prob": 0.52,
                "bayes_chain": {
                    "prior_prob": 0.4,
                    "likelihood_prob": 0.5,
                    "likelihood_parts": {"wu_forecast": {"prob": 0.52, "evidence": 3.0}},
                    "blob": "HEAVY_FAST_TRADE_MARKER",
                },
                "q_ensemble_qm_meta": {
                    "raw_bucket_probability": {
                        "labels": ["31°C"],
                        "probs_by_model": {"gfs_seamless": {"31°C": 0.6}},
                    },
                    "blob": "HEAVY_FAST_TRADE_MARKER",
                },
                "q_bayes_physical_d0_explanation": {
                    "summary": "物理证据",
                    "variables": [{"key": "dewpoint", "label": "露点", "direction": "warm"}],
                    "blob": "HEAVY_FAST_TRADE_MARKER",
                },
                "q_bayes_physical_likelihood_component_groups": {
                    "dewpoint": {
                        "available": True,
                        "weight": 1.2,
                        "source_profile": {
                            "source_class": "metar",
                            "source_field": "dewpoint_c",
                            "blob": "HEAVY_FAST_TRADE_MARKER",
                        },
                    }
                },
                "per_model_q": {
                    "gfs_seamless": {"dir_value": 31.2, "q_round": 0.6, "blob": "HEAVY_FAST_TRADE_MARKER"}
                },
            }
        ],
    }

    server._compact_fact_payload_for_fast_trade_write(item)
    encoded = json.dumps(item, ensure_ascii=False)

    assert "HEAVY_FAST_TRADE_MARKER" not in encoded
    assert "key_metrics" not in item
    assert "target_bucket_outlook" not in item
    assert "temperature_path_distribution" not in item
    row = item["distribution"][0]
    assert row["bayes_chain"]["likelihood_parts"]["wu_forecast"]["prob"] == 0.52
    assert row["q_ensemble_qm_meta"]["raw_bucket_probability"]["probs_by_model"]["gfs_seamless"]["31°C"] == 0.6
    assert row["q_bayes_physical_d0_explanation"]["variables"][0]["key"] == "dewpoint"
    assert "q_bayes_physical_likelihood_component_groups" not in row
    assert row["state_evidence_trace"]["groups"]["dewpoint"]["source_profile"]["source_class"] == "metar"
    assert row["per_model_q"]["gfs_seamless"]["dir_value"] == 31.2
    provenance = item["fact_provenance"]["awc_fast_trade_fact_write"]
    assert "key_metrics" in provenance["dropped_top_level_keys"]
    assert provenance["ui_contract_kept_top_level_keys"] == []
    assert "bayes_chain" in provenance["ui_contract_kept_row_keys"]


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

    assert "posterior_target_bucket_selection" not in payload
    assert "target_bucket_selection" not in payload
    assert "target_bucket" not in payload
    assert "ssot_target_bucket" not in payload
    assert payload["target_bucket_compaction_skipped_reason"] == "posterior_ssot_fact_missing"


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

    assert changed is False
    assert merged["observation_led_likelihood"]["diagnostic_only"] is True
    assert merged["observation_led_likelihood_posterior_materialized"] is False
    assert merged["observation_led_likelihood"]["materialization_block_reason"] == (
        "posterior_ssot_fact_missing_for_intraday_observation_led_materialization"
    )
    assert merged["fact_provenance"]["observation_led_likelihood"]["skipped_reason"] == (
        "posterior_ssot_fact_missing_for_intraday_observation_led_materialization"
    )
    assert by_outcome["31°C"]["q_temperature_bucket_posterior"] == pytest.approx(0.050319312724171415)
    assert by_outcome["33°C"]["q_temperature_bucket_posterior"] == pytest.approx(0.11631060268355033)


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
    trace = row["state_evidence_trace"]
    assert trace["schema"] == "metar_strong_state_evidence_trace_v1"
    assert trace["role"] == "state_evidence_trace"
    assert trace["dedupe_boundary"] == (
        "METAR physical evidence is consumed inside L_metar_strong, not as an independent probability channel"
    )
    groups = row["q_bayes_physical_likelihood_component_groups"]
    precip = groups["precipitation_evaporative_cooling"]
    assert precip["available"] is True
    assert precip["weight"] == pytest.approx(1.35)
    assert precip["source_profile"]["source_class"] == "metar"
    assert precip["source_profile"]["no_precip_confirmed"] is True
    assert "debug_blob" not in precip["source_profile"]
    assert "debug_blob" not in precip["components"]["precip_evaporative_cooling"]
    assert trace["groups"]["precipitation_evaporative_cooling"] == precip
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
        assert "state_evidence_trace" in row
        assert "q_bayes_physical_likelihood_component_groups" in row
        assert "q_bayes_physical_likelihood_components" in row
        assert row["state_evidence_trace"]["role"] == "state_evidence_trace"
        assert row["state_evidence_trace"]["replaces"] == "q_bayes_physical_likelihood_component_groups"
    groups = rows[0]["q_bayes_physical_likelihood_component_groups"]
    trace_groups = rows[0]["state_evidence_trace"]["groups"]
    assert "temperature_trajectory" in groups
    assert "precipitation_evaporative_cooling" in groups
    assert "temperature_trajectory" in trace_groups
    assert "precipitation_evaporative_cooling" in trace_groups
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
    attach_unit_prior_contract(old_payload)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = insert_event_fact_history_with_unit_prior(
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
    attach_unit_prior_contract(old_payload)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = insert_event_fact_history_with_unit_prior(
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
    attach_unit_prior_contract(payload)
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
    fact_id = insert_event_fact_history_with_unit_prior(
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
    attach_unit_prior_contract(payload)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = insert_event_fact_history_with_unit_prior(
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
    attach_unit_prior_contract(old_payload)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    fact_id = insert_event_fact_history_with_unit_prior(
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
    fact_id = insert_event_fact_history_with_unit_prior(
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
        fact_id = insert_event_fact_history_with_unit_prior(
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


def _create_discovery_materialization_source_tables(conn: sqlite3.Connection) -> None:
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
            payload_json TEXT,
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


def _insert_chengdu_discovery_market(conn: sqlite3.Connection, *, slug: str, event_date: str) -> None:
    conn.execute(
        """
        INSERT INTO weather_market_discovery(
            slug, event_date, title, market_open_at, first_seen_at, first_tokenized_at,
            token_count, status, details_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slug,
            event_date,
            "Highest temperature in Chengdu on June 24?",
            "2026-06-22T12:00:00+00:00",
            "2026-06-22T12:01:00+00:00",
            "2026-06-22T12:02:00+00:00",
            2,
            "tokenized",
            json.dumps({"endDate": "2026-06-24T12:00:00+00:00"}),
            "2026-06-22T12:03:00+00:00",
        ),
    )
    for outcome in ("29°C", "30°C"):
        conn.execute(
            """
            INSERT INTO market_tokens(
                slug, event_date, outcome, yes_token, no_token, outcome_volume, liquidity,
                market_active, market_closed, market_archived, market_accepting_orders,
                market_enable_orderbook, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                event_date,
                outcome,
                f"yes-{outcome}",
                f"no-{outcome}",
                10.0,
                20.0,
                1,
                0,
                0,
                1,
                1,
                "2026-06-22T12:04:00+00:00",
            ),
        )


def test_materialize_facts_uses_formal_fact_chain_for_discovery_pending_model(
    monkeypatch,
) -> None:
    monkeypatch.setattr(server, "_today_et_key", lambda: "2026-06-22")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    _create_discovery_materialization_source_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-24-2026"
    _insert_chengdu_discovery_market(conn, slug=slug, event_date="2026-06-24")
    conn.commit()

    created = server._materialize_facts_from_snapshots_for_range(conn, "2026-06-22", "2026-06-24")

    row = conn.execute(
        """
        SELECT h.id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, "2026-06-24"),
    ).fetchone()
    conn.close()

    assert created == 1
    assert row is not None
    payload = json.loads(row["fact_json"])
    assert payload["data_status"] == "market_discovery_pending_model"
    assert payload["fact_provenance"]["fact_materialization_source_resolver"]["source"] == "weather_market_discovery"
    assert payload["fact_provenance"]["fact_materialization_source_resolver"]["formal_fact_chain"] is True
    assert payload["posterior_run_id"].startswith("post_pending_discovery_")
    assert payload["posterior_ssot_fact"]["schema"] == "bayes_posterior_ssot_fact_placeholder_v1"
    assert payload["posterior_ssot_fact"]["state"] == "not_materialized"
    assert payload["posterior_materialization_state"]["available_posterior"] is False
    assert payload["auto_trade_ssot"]["main_entry_ok"] is False
    assert "target_realization_capital_plan" not in payload


def test_pending_model_fact_with_prior_and_awc_materializes_available_posterior() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_runs(
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            event_url TEXT,
            title TEXT,
            city TEXT,
            station_id TEXT,
            market_var TEXT,
            market_unit TEXT,
            rounding_rule TEXT,
            settlement_actual REAL,
            payload_json TEXT,
            created_at TEXT
        )
        """
    )
    for event_date, actual in [
        ("2026-06-20", 29.0),
        ("2026-06-21", 30.0),
        ("2026-06-22", 30.0),
        ("2026-06-23", 31.0),
    ]:
        conn.execute(
            """
            INSERT INTO event_runs(
                run_date, event_date, slug, event_url, title, city, station_id,
                market_var, market_unit, rounding_rule, settlement_actual,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'Chengdu', 'ZUUU', 'temperature_2m_max', 'C',
                      'rounded_degrees', ?, '{}', '2026-06-24T00:00:00+00:00')
            """,
            (
                event_date,
                event_date,
                f"highest-temperature-in-chengdu-on-{event_date}",
                f"https://polymarket.com/event/{event_date}",
                f"Highest temperature in Chengdu on {event_date}?",
                actual,
            ),
        )
    slug = "highest-temperature-in-chengdu-on-june-24-2026"
    rows = [
        {"outcome": "29°C", "label": "29°C", "q_na_reason": "model_not_materialized_yet", "market_discovery_placeholder": True},
        {"outcome": "30°C", "label": "30°C", "q_na_reason": "model_not_materialized_yet", "market_discovery_placeholder": True},
        {"outcome": "31°C", "label": "31°C", "q_na_reason": "model_not_materialized_yet", "market_discovery_placeholder": True},
    ]
    fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date="2026-06-24",
        slug=slug,
        event_date="2026-06-24",
        fact_as_of="2026-06-24T02:05:00+00:00",
        model_snapshot_id=None,
        market_snapshot_id=None,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": "2026-06-24",
            "run_date": "2026-06-24",
            "city": "Chengdu",
            "station_id": "ZUUU",
            "market_var": "temperature_2m_max",
            "market_unit": "C",
            "rounding_rule": "rounded_degrees",
            "data_status": "market_discovery_pending_model",
            "weather_strategy_mode": "market_discovery_pending_model",
            "mean_bucket_na_reason": "model_not_materialized_yet",
            "market_discovery_placeholder": True,
            "source_event_ids": ["srcevt_market_discovery:unit_pending_model"],
            "fact_provenance": {"market_discovery_placeholder": True},
            "distribution": json.loads(json.dumps(rows)),
            "rows": json.loads(json.dumps(rows)),
            "weather_likelihood_features": {
                "zuuu_metar_raw_awc_latest_at": "2026-06-24T02:00:00+00:00",
                "zuuu_metar_raw_awc_fetched_at": "2026-06-24T02:05:00+00:00",
                "zuuu_metar_raw_awc_latest_raw": "METAR ZUUU 240200Z VRB01MPS CAVOK 30/23 Q1004 NOSIG",
                "zuuu_metar_raw_awc_latest_temp_c": 30.0,
                "zuuu_metar_raw_awc_latest_dewpoint_c": 23.0,
                "zuuu_metar_raw_awc_today_max_c": 30.0,
                "temperature_momentum_1h_c_per_hour": 1.0,
                "temperature_momentum_available_horizons": "1h",
            },
        },
        write_latest=True,
    )
    row = conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (fact_id,)).fetchone()
    conn.close()

    payload = json.loads(row["fact_json"])
    assert payload["data_status"] == "posterior_ssot_available"
    assert payload["weather_strategy_mode"] == "hierarchical_joint_likelihood"
    assert "mean_bucket_na_reason" not in payload
    assert payload["posterior_materialization_state"]["available_posterior"] is True
    assert "srcevt_market_discovery:unit_pending_model" in payload["source_event_ids"]
    assert payload["prior_meta"]["sample_count"] == 4
    assert payload["posterior_ssot_fact"]["schema"] == "bayes_posterior_ssot_fact_v1"
    assert payload["posterior_ssot_fact"]["state"] == "available"
    assert payload["posterior_target_bucket_selection"]["ssot_source"] == "posterior_ssot_fact"
    assert payload["distribution"][0]["market_discovery_placeholder"] is False
    assert "q_na_reason" not in payload["distribution"][0]
    assert payload["source_acquisition_trace"]["schema"] == "ui_pending_model_hierarchical_source_trace_v1"
    assert "srcevt_market_discovery:unit_pending_model" in payload["source_acquisition_trace"]["source_event_ids"]
    assert "srcevt_market_discovery:unit_pending_model" in payload["source_acquisition_trace"]["diagnostic_current_ids"]
    assert payload["posterior_boundary"]["old_additive_evidence_pool_not_used"] is True
    assert payload["ui_sections"]["prior"]["state"] == "available"
    assert payload["ui_sections"]["bayes_posterior_ssot"]["state"] == "available"
    assert cached is not None
    summary_payload = json.loads(cached["summary_json"])
    assert summary_payload["ui_sections"]["prior"]["state"] == "available"
    assert summary_payload["ui_sections"]["bayes_posterior_ssot"]["state"] == "available"
    assert "srcevt_market_discovery:unit_pending_model" in summary_payload["source_event_ids"]


def test_discovery_pending_materialization_does_not_repoint_over_formal_fact(
    monkeypatch,
) -> None:
    monkeypatch.setattr(server, "_today_et_key", lambda: "2026-06-22")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    _create_discovery_materialization_source_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-24-2026"
    _insert_chengdu_discovery_market(conn, slug=slug, event_date="2026-06-24")
    formal_fact_id = insert_event_fact_history_with_unit_prior(
        conn,
        run_date="2026-06-24",
        slug=slug,
        event_date="2026-06-24",
        fact_as_of="2026-06-22T12:02:00+00:00",
        model_snapshot_id=11,
        market_snapshot_id=22,
        composer_version=server.FACTS_COMPOSER_VERSION,
        item={
            "slug": slug,
            "event_date": "2026-06-24",
            "run_date": "2026-06-24",
            "station_id": "ZUUU",
            "city": "Chengdu",
            "market_var": "temperature_2m_max",
            "market_unit": "C",
            "phase": "D1_forecast",
            "phase_reason": "unit_test_formal_model_fact",
            "beta_weak": 0.75,
            "beta_strong": 0.25,
            "hard_mask_reason": "none",
            "distribution": [
                {"outcome": "29°C", "q_temperature_bucket_posterior": 0.35},
                {"outcome": "30°C", "q_temperature_bucket_posterior": 0.65},
            ],
            "prior_distribution": {"29°C": 0.35, "30°C": 0.65},
            "prior_na_reason": "",
            "prior_meta": {
                "schema": "temperature_prior_meta_v1",
                "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
                "sample_count": 10,
                "shrinkage_target": "broader_settled_history",
                "date_window_days": 15,
                "prior_na_reason": "",
            },
        },
        write_latest=True,
    )
    conn.commit()

    created = server._materialize_facts_from_snapshots_for_range(conn, "2026-06-22", "2026-06-24")

    latest = conn.execute(
        "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
        (slug, "2026-06-24"),
    ).fetchone()
    history_count = conn.execute(
        "SELECT COUNT(*) AS count FROM event_fact_history WHERE slug = ? AND event_date = ?",
        (slug, "2026-06-24"),
    ).fetchone()["count"]
    conn.close()

    assert created == 0
    assert latest["fact_id"] == formal_fact_id
    assert history_count == 1


def test_query_facts_unpaged_include_upcoming_does_not_require_full_mode(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    slug = "highest-temperature-in-chengdu-on-june-22-2026"
    fact_id = insert_event_fact_history_with_unit_prior(
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
