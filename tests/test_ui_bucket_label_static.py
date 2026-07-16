from pathlib import Path


UI_INDEX = Path(__file__).resolve().parents[1] / "ui" / "index.html"


def _function_block(name: str) -> str:
    html = UI_INDEX.read_text(encoding="utf-8")
    marker = f"function {name}("
    start = html.index(marker)
    next_function = html.find("\n    function ", start + len(marker))
    if next_function == -1:
        return html[start:]
    return html[start:next_function]


def _function_blocks(name: str) -> list[str]:
    html = UI_INDEX.read_text(encoding="utf-8")
    marker = f"function {name}("
    blocks = []
    start = 0
    while True:
        idx = html.find(marker, start)
        if idx == -1:
            return blocks
        next_function = html.find("\n    function ", idx + len(marker))
        if next_function == -1:
            blocks.append(html[idx:])
            return blocks
        blocks.append(html[idx:next_function])
        start = next_function + 1


def test_single_temperature_bucket_labels_are_not_expanded_to_ranges():
    block = _function_block("formatBucketRangeText")

    assert "parsed.lo + step" not in block
    assert "estimateBucketStep(distribution" not in block
    assert "return `${formatBucketRangeValue(parsed.lo)}${unit}`;" in block


def test_temperature_path_distribution_only_renders_top_three_posterior_rows():
    block = _function_block("renderTemperaturePathDistribution")

    assert "eventBayesPosteriorRows(null, 3, data.bucket_distribution)" in block
    assert ".slice(0, 14)" not in block


def test_dashboard_uses_local_font_stack_without_remote_google_fonts():
    html = UI_INDEX.read_text(encoding="utf-8")

    assert "fonts.googleapis.com" not in html
    assert "fonts.gstatic.com" not in html
    assert "font-family:ui-sans-serif, system-ui" in html
    assert '"PingFang SC"' in html


def test_recovered_bayes_chain_is_marked_retired_compat_in_frontend():
    html = UI_INDEX.read_text(encoding="utf-8")
    block = _function_block("bayesChainForDistributionRow")

    assert "LEGACY_BAYES_CHAIN_RETIRED_COMPAT_META" in html
    assert "legacy_bayes_chain_retired_compat_v1" in html
    assert "ui_primary_allowed: false" in html
    assert "production_probability_allowed: false" in html
    assert "replacement_posterior_runtime: 'hierarchical_posterior_pipeline_runtime.py'" in html
    assert "recoveredLegacyEvidence = true" not in block
    assert "hasLegacyEvidencePart" in block


def test_distribution_table_legacy_likelihood_fallback_is_retired_compat_helper():
    html = UI_INDEX.read_text(encoding="utf-8")
    helper = _function_block("legacyDistributionLikelihoodCompatForRow")
    table = _function_block("renderDistributionTable")

    assert "legacyDistributionLikelihoodCompatForRow" in html
    assert "return null;" in helper
    assert "schema: 'legacy_evidence_boundary_v1'" not in helper
    assert "const rawLegacyLikelihoodCompat = legacyDistributionLikelihoodCompatForRow(row);" in table
    assert "rawLegacyLikelihoodCompat && typeof rawLegacyLikelihoodCompat === 'object'" in table
    assert "const componentProbability = (componentKey, evidenceKey, fallback)" not in table


def test_physical_likelihood_panel_prefers_metar_strong_layer_before_legacy_fallback():
    metar_block = _function_block("metarStrongLikelihoodSnapshotFromItem")
    legacy_block = _function_block("legacyPhysicalLikelihoodSnapshotCompatFromItem")
    wrapper_block = _function_block("physicalLikelihoodSnapshotFromItem")

    assert "data.metar_strong_likelihood" in metar_block
    assert "data.state_probs" in metar_block
    assert "data.remaining_warming_by_state" in metar_block
    assert "data.hard_constraints" in metar_block
    assert "schema: 'metar_strong_likelihood_snapshot_v1'" in metar_block
    assert "return null;" in legacy_block
    assert "schema: 'legacy_physical_likelihood_retired_compat_v1'" not in legacy_block
    assert "metarStrongLikelihoodSnapshotFromItem(item, snapshot)" in wrapper_block
    assert "legacyPhysicalLikelihoodSnapshotCompatFromItem(item, snapshot)" in wrapper_block


def test_trajectory_peak_estimate_reads_backend_ssot_without_frontend_mixture_math():
    backend_block = _function_block("eventIntelBackendTrajectoryPeakEstimateData")
    mixture_block = _function_block("eventIntelRemainingWarmingMixtureData")
    path_block = _function_block("eventIntelPathReachProbabilityForBucket")

    assert "trajectory_peak_estimate" in backend_block
    assert "temperature_trajectory_peak_estimate" in backend_block
    assert "path_reach_probability_by_bucket" in backend_block
    assert "eventIntelBackendTrajectoryPeakEstimateData(...inputs)" in mixture_block
    assert "const variance" not in mixture_block
    assert "Math.sqrt(Math.max(0, variance))" not in mixture_block
    assert "pathReachByBucket" in path_block


def test_physical_realtime_and_timeline_use_strong_layer_not_legacy_evidence():
    realtime_block = _function_block("renderPhysicalLikelihoodRealtimeStrip")
    timeline_block = _function_block("renderTemperatureTrajectoryTimeline")
    helper_block = _function_block("physicalStrongLikelihoodLayerDisplay")

    assert "physicalStrongLikelihoodLayerDisplay" in realtime_block
    assert "physicalStrongLikelihoodLayerDisplay" in timeline_block
    assert "metarStrongLikelihoodSnapshotFromItem(item, payload)" in realtime_block
    assert "metarStrongLikelihoodSnapshotFromItem(item, payload)" in timeline_block
    assert "schema === 'metar_strong_likelihood_snapshot_v1'" in helper_block
    assert "sourceRole === 'metar_strong_likelihood'" in helper_block
    assert "physical_likelihood_evidence" not in realtime_block
    assert "physical_likelihood_evidence" not in timeline_block


def test_daily_trade_advice_uses_strong_likelihood_not_legacy_evidence_weights():
    html = UI_INDEX.read_text(encoding="utf-8")
    start = html.index("function formatDailyTradeAdviceSummary")
    end = html.index("function isPosteriorSettlementCandidate", start)
    block = html[start:end]

    assert "metarStrongLikelihoodSnapshotFromItem(item, payload)" in block
    assert "physicalStrongLikelihoodLayerDisplay(strongTradeSnapshot)" in block
    assert "hard_constraints" in block
    assert "q_bayes_model_evidence" not in block
    assert "q_bayes_wu_forecast_evidence" not in block
    assert "q_bayes_taf_peak_evidence" not in block
    assert "q_bayes_observed_nowcast_evidence" not in block
    assert "q_bayes_physical_likelihood_evidence" not in block
    assert "q_bayes_independent_physics_residual_evidence" not in block


def test_temperature_regime_panel_does_not_render_legacy_trajectory_evidence():
    extract_block = _function_block("temperatureRegimeV1Extract")
    panel_block = _function_block("renderTemperatureTrajectoryRegimeV1Panel")

    assert "mergedDirect.evidence" in extract_block
    assert "q_bayes_temperature_trajectory_evidence" not in extract_block
    assert "temperature_trajectory_evidence" not in extract_block
    assert "路径快照" in panel_block
    assert "兼容读数" not in panel_block
    assert "q_bayes_temperature_trajectory_evidence" not in panel_block
    assert "temperature_trajectory_evidence" not in panel_block


def test_source_peak_value_rows_do_not_frontend_bucketize_without_ssot_bucket():
    value_block = _function_block("sourcePeakBucketFromValue")
    mode_block = _function_block("sourcePeakBucketModeText")

    assert "computeSettlementGap" not in value_block
    assert "bucketRepresentativeValue" not in value_block
    assert "nearest_bucket" not in value_block
    assert "value_bucket" not in value_block
    assert "value_only_no_ssot_bucket" in value_block
    assert "仅显示原值，SSOT未给落桶" in mode_block


def test_top_posterior_chart_renders_single_snapshot_as_short_line_not_points():
    block = _function_block("renderFusedBucketTrendChart")
    live_block = _function_block("renderEventBayesPosteriorLivePanel")
    snapshot_block = _function_block("buildSnapshotHistoryGroups")

    assert "itemForReplay = null" in block
    assert "replayTrendSeries(bucketSeries, itemForReplay)" in block
    assert "created_at_replay" in block
    assert "source: 'created_at_replay'" in block
    assert "const isSingleSnapshot = timestamps.length <= 1;" in block
    assert "Top 3 温度目标概率趋势" in block
    assert "实线：最终概率" in block
    assert "未结算" in block
    assert "非当前实况" in block
    assert "${latestTime} 短线快照" in block
    assert "bucket-fused-trends${isSingleSnapshot ? ' is-single-snapshot' : ''}" in block
    assert "const probabilityShapes = drawEntries.map" in block
    assert "coords.length === 1" in block
    assert "L${x2.toFixed(1)} ${y.toFixed(1)}" in block
    assert "${path ? `<path class=\"bucket-fused-prob-line\"" in block
    assert "(!isSingleSnapshot && latest)" in block
    assert "const deltaText = isSingleSnapshot ? '' : ppText(delta);" in block
    assert "eventBayesPosteriorRows(item, 3, eventIntelTemperaturePathDistributionRows(item))" not in live_block
    assert "03:02 → 03:02" not in block
    assert "圆点：当前主 posterior 概率" not in block
    assert "最终结算后验" not in UI_INDEX.read_text(encoding="utf-8")
    assert "最终最高温落桶概率" not in UI_INDEX.read_text(encoding="utf-8")
    assert "最终最高温落桶目标" not in UI_INDEX.read_text(encoding="utf-8")
    assert "预测最终最高温落桶" not in UI_INDEX.read_text(encoding="utf-8")
    assert ".bucket-fused-trends.is-single-snapshot .bucket-fused-axis-legend span::before" not in UI_INDEX.read_text(encoding="utf-8")
    assert "bayes-posterior-live-bars" in live_block
    assert "data-live-prob" in live_block
    assert "const snapshotBucketSeries = item && item.bucket_series" in snapshot_block
    assert "mergeCurrentDistributionIntoBucketSeries(snapshotBucketSeries" in snapshot_block


def test_final_posterior_distribution_does_not_fallback_to_temperature_path_distribution():
    effective_block = _function_block("eventIntelEffectiveIntradayDistribution")
    bucket_block = _function_block("eventIntelBucketProbabilitiesFromItem")
    pct_block = _function_block("eventIntelBucketProbabilityPct")

    assert "payload.bucket_probabilities" in effective_block
    assert "payload.temperature_distribution" in effective_block
    assert "itemDistribution" in effective_block
    assert "eventIntelTemperaturePathDistributionRows" not in effective_block
    assert "const rows = item && Array.isArray(item.distribution) ? item.distribution : [];" in bucket_block
    assert "eventIntelTemperaturePathDistributionRows" not in bucket_block
    assert "eventIntelProbabilityPctFromRows(pathRows" not in pct_block
    assert "eventIntelTemperaturePathDistributionRows" not in pct_block


def test_trade_copy_keeps_target_selection_path_probability_and_market_price_separate():
    html = UI_INDEX.read_text(encoding="utf-8")

    assert "目标选择看后端最终概率/资金计划" in html
    assert "路径概率只作执行跟随门控" in html
    assert "价格/盘口仅作执行检查" in html
    assert "交易建议按目标桶最终概率" in html
    assert "目标桶路径概率" not in html
    assert "路径概率目标桶" not in html
    assert "目标选择只看目标桶路径概率" not in html
    assert "交易建议只按目标桶路径概率" not in html


def test_market_top_label_does_not_fallback_into_posterior_or_trajectory_reference():
    trajectory_block = _function_block("eventIntelTemperatureTrajectoryFallback")
    qm_block = _function_block("renderCorrectedTopBucketBlock")
    advice_start = UI_INDEX.read_text(encoding="utf-8").index("function formatDailyTradeAdviceSummary")
    html = UI_INDEX.read_text(encoding="utf-8")
    advice_end = html.index("function isPosteriorSettlementCandidate", advice_start)
    advice_block = html[advice_start:advice_end]

    assert "item.market_top_label" not in trajectory_block
    assert "item.market_top_label" not in qm_block
    assert "后验参考桶" not in html
    assert "后验/模型参考桶" not in html
    assert "最高后验桶" not in html
    assert "item.single_temperature_top_label || item.model_top_label" not in trajectory_block
    assert "item.single_temperature_top_q || item.model_top_q" not in trajectory_block
    assert "topLabel: top.label || ''," in trajectory_block
    assert "topProb: Number.isFinite(top.prob) ? top.prob : Number.NaN," in trajectory_block
    assert "item.single_temperature_top_label || item.model_top_label || item.market_top_label" not in advice_block
    assert "const topLabel = posteriorTop ? posteriorTop.label : '';" in advice_block
    assert "const topProb = posteriorTop ? posteriorTop.value : Number.NaN;" in advice_block
    assert "item.single_temperature_top_label || item.model_top_label" not in advice_block
    assert "item.single_temperature_top_q || item.model_top_q" not in advice_block
    assert "marketTopLabel" in advice_block
    assert "盘口" in advice_block


def test_yes_top_events_do_not_stamp_model_top_as_market_top():
    html = UI_INDEX.read_text(encoding="utf-8")
    start = html.index("const buildEdgeEntry = (item, idx) => {")
    end = html.index("const hasBestScore = Number.isFinite(bestScore);", start)
    block = html[start:end]

    assert "market_top_label: topLabel" not in block
    assert "market_top_label: entry.market_top_label || String(item && (item.market_top_label || item.market_close_label) || '')" in block
    assert "model_top_label: topLabel" in block


def test_daily_trade_advice_defines_payload_top_single_reference():
    html = UI_INDEX.read_text(encoding="utf-8")
    start = html.index("function formatDailyTradeAdviceSummary")
    end = html.index("function isPosteriorSettlementCandidate", start)
    block = html[start:end]

    assert "const payloadTopSingle = payload && payload.top_single_temperature" in block
    assert ": payloadTopSingle;" in block
    assert "payloadTopSingle.outcome" not in block


def test_bucket_probability_resolver_does_not_backfill_from_top_single_or_model_top():
    html = UI_INDEX.read_text(encoding="utf-8")
    payload_block = _function_block("eventIntelPayloadBucketProbabilityPct")
    resolver_block = _function_block("eventIntelBucketProbabilityPct")

    assert "function eventIntelItemTopBucketProbabilityPct" not in html
    assert "top_single_temperature" not in payload_block
    assert "eventIntelProbabilityPctForLabeledObject(\n        data" not in payload_block
    assert "single_temperature_top_q" not in resolver_block
    assert "model_top_q" not in resolver_block
    assert "eventIntelItemTopBucketProbabilityPct" not in resolver_block
    assert "return Number.NaN;" in payload_block


def test_auto_trade_config_bootstrap_does_not_block_initial_render():
    wire_block = _function_block("wire有效优势分Controls")
    bootstrap_block = _function_block("bootstrapAutoTradeConfig")

    assert "bootstrapAutoTradeConfig();" in wire_block
    assert "await bootstrapAutoTradeConfig()" not in wire_block
    assert "fetchJSON('/api/auto-trade-config', {" in bootstrap_block
    assert "timeoutMs: 2500" in bootstrap_block
    assert "retryCount: 0" in bootstrap_block
    assert "retryOnAbort: false" in bootstrap_block


def test_auto_trade_criteria_panel_renders_backend_ssot_sections_only():
    html = UI_INDEX.read_text(encoding="utf-8")
    block = _function_block("renderAutoTradeCriteriaHtml")

    assert "/api/auto-trade-criteria?payload=" in html
    assert "renderAutoTradeCriteriaHtml(res.criteria)" in html
    assert "AUTO_TRADE_ENTRY_ONLY_REALIZABLE" not in block
    assert "data-criteria-id=\"entry-only-realizable\"" not in block
    assert "YES：只买后验高且温度轨迹正在兑现的目标桶" not in block
    assert "const renderSection = (section, levelId) =>" in block
    assert "criteria.sections" in block


def test_frontend_auto_trade_candidates_do_not_recompute_without_ssot():
    html = UI_INDEX.read_text(encoding="utf-8")
    blocks = []
    start = 0
    while True:
        pick_start = html.find("function pick有效优势分Candidate(item)", start)
        if pick_start == -1:
            break
        reason_start = html.index("function pickReasonCandidate(item)", pick_start)
        end_candidates = [
            pos
            for marker in ("function formatTradeBadge(suggestion)", "const EXIT_REASON_MAP = {")
            for pos in [html.find(marker, reason_start)]
            if pos != -1
        ]
        assert end_candidates
        end = min(end_candidates)
        blocks.append(html[pick_start:end])
        start = end

    assert len(blocks) == 2
    for block in blocks:
        assert "auto_trade_ssot && item.auto_trade_ssot.main_candidate" in block
        assert "return ssot && typeof ssot === 'object' ? ssot : null;" in block
        assert "return pick有效优势分Candidate(item);" in block
        for old_fragment in (
            "const dist = item && Array.isArray(item.distribution)",
            "edge_score",
            "q_round",
            "marketTop",
            "resolveSignalEdgeFromRow",
            "EDGE_SCORE_MIN_MARKET_PROB",
        ):
            assert old_fragment not in block


def test_chengdu_intraday_fetches_are_throttled_and_deduped():
    ttl_block = _function_block("eventIntelIntradayCacheTtlMs")
    event_request_block = _function_block("requestEventIntelChengduIntradayPayload")
    board_refresh_block = _function_block("refreshChengduIntradayDecisionBoard")
    standalone_request_block = _function_block("requestChengduDecisionJson")
    standalone_refresh_block = _function_block("refreshChengduDecisionStandalone")

    assert "EVENT_INTEL_CHENGDU_INTRADAY_CACHE_TTL_MAX_MS" in ttl_block
    assert "Math.max(" in ttl_block
    assert "Math.min(sec * 1000, EVENT_INTEL_CHENGDU_INTRADAY_CACHE_TTL_MAX_MS)" in ttl_block
    assert "timeoutMs: 8000" in event_request_block
    assert "eventIntelIntradayRequestCoolingDown(date, cached)" in event_request_block
    assert "eventIntelIntradayMarkRequestStarted(date)" in event_request_block
    assert "window.__chengduIntradayDecisionInFlightByDate[date]" in event_request_block
    assert "timeoutMs: 8000" in board_refresh_block
    assert "eventIntelIntradayRequestCoolingDown(date, cached || chengduIntradayDecisionLastPayload)" in board_refresh_block
    assert "eventIntelIntradayMarkRequestStarted(date)" in board_refresh_block
    assert "if (sec <= 30) return 3000;" not in board_refresh_block
    assert "xhr.timeout = 8000" in standalone_request_block
    assert "eventIntelIntradayPayloadIsFresh(cached)" in standalone_refresh_block
    assert "eventIntelIntradayRequestCoolingDown(date, null)" in standalone_refresh_block
    assert "eventIntelIntradayMarkRequestStarted(date)" in standalone_refresh_block
    assert "window.__chengduIntradayDecisionInFlightByDate[date] = true" in standalone_refresh_block
    assert "delete window.__chengduIntradayDecisionInFlightByDate[date]" in standalone_refresh_block


def test_model_member_snapshot_board_reuses_loaded_runs_before_fetching_summary():
    refresh_block = _function_block("refreshModelMemberSnapshotBoard")
    loaded_runs_block = _function_block("modelMemberSnapshotFromLoadedRuns")

    assert "window.__runsLoaded" in loaded_runs_block
    assert "window.__lastPayload.data" in loaded_runs_block
    assert "modelMemberSnapshotFromLoadedRuns()" in refresh_block
    assert "const hasCachedSnapshot = snapshotHasUsableRunsItems(cachedSnapshot);" in refresh_block
    assert "if (hasCachedSnapshot && !modelMemberSnapshotLastFetchAt)" in refresh_block
    assert "return;" in refresh_block
    assert "run_date=${effectiveDate}&limit=12" in refresh_block
    assert "if (!hasCachedSnapshot) {" in refresh_block
    assert "console.warn('[snapshot-boards] render skipped'" in refresh_block


def test_event_bayes_prior_step_emphasizes_temperature_bucket_probability_changes():
    html = UI_INDEX.read_text(encoding="utf-8")
    block = _function_block("renderEventBayesPriorStep")

    assert "目标桶起点" in block
    assert "bayes-prior-spotlight" in block
    assert "气候Top ${topPriorLabel} ${topPriorProb}" in block
    assert "同一目标桶起点" in block
    assert "气候分位" in block
    assert "bayes-prior-bar-row" in block
    assert "bayes-prior-bar-fill" in block
    assert "距Top ${ppDeltaText(delta)}" in block
    assert "bayes-prior-impact-grid" in block
    assert "气候影响" in block
    assert "气候/环流" in block
    assert "q_bayes_climo_regime_pp_max_delta" in block
    assert "并入气候 ${ppImpactText(ppMaxDelta)}" in block
    assert "temperature_adjustment_c" in block
    assert "bayes-prior-context-chip" in block
    assert "renderChengduClimoRegimeSummary" not in block
    assert "chengdu-context-summary-grid" not in block
    assert ".bayes-prior-spotlight" in html
    assert ".bayes-prior-bar-track" in html
    assert ".bayes-prior-impact-card" in html


def test_chengdu_decision_bucket_strip_uses_top_three_posterior_rows():
    html = UI_INDEX.read_text(encoding="utf-8")

    assert "const displayBuckets = eventBayesPosteriorRows(null, 3, buckets)" in html
    assert "var displayBuckets = typeof eventBayesPosteriorRows === 'function'" in html
    assert "buckets.map((row) => renderChengduDecisionBucket(row, topLabel))" not in html
    assert "buckets.map(function (row) { return renderChengduDecisionBucketStandalone(row, topLabel); })" not in html


def test_distribution_bayes_chain_renders_step_delta_and_reason_cards():
    html = UI_INDEX.read_text(encoding="utf-8")
    block = _function_block("renderDistributionTable")
    legacy_helper = _function_block("legacyDistributionLikelihoodCompatForRow")

    assert "bayes-step-card" in block
    assert "bayes-chain-steps" in block
    assert "grid-template-columns:repeat(6, minmax(82px, 1fr))" in html
    assert "`${deltaText} · 相对 ${previousText}`" in block
    assert "evidenceReason" in block
    assert "observedConstraintReason" in block
    assert "posteriorReason" in block
    assert "chain.likelihood_parts" not in block
    assert "chain.likelihood_parts" not in legacy_helper
    assert "part.evidence" not in legacy_helper
    assert "legacyDistributionLikelihoodCompatForRow(row)" in block
    assert "channelPrecisionShareText" in block
    assert "channelDisplayImpactText" in block
    assert "bayes-driver-weight-chip" in block
    assert "主因" in block
    assert "单源最高" in block
    assert "贡献=P×w" not in html
    assert "eventIntelMergeDistributionDiagnostics(payload.bucket_probabilities, itemDistribution)" in html
    assert "eventIntelMergeDistributionDiagnostics(itemPathRows" not in html
    assert "eventIntelProbabilityPctFromRows(pathRows" not in html


def test_temperature_regime_v1_panel_uses_ctp_source_component_fields():
    html = UI_INDEX.read_text(encoding="utf-8")
    extract_block = _function_block("temperatureRegimeV1Extract")
    panel_block = _function_block("renderTemperatureTrajectoryRegimeV1Panel")
    physical_block = _function_block("renderEventIntelPhysicalEvidenceHub")
    fallback_block = _function_block("renderEventIntelTrajectoryFallbackPanel")
    ctp_chain_block = _function_block("renderTemperaturePosteriorCtpChainPanel")

    assert "temperatureRegimeV1SourceComponentFromMeta(payloadPosterior)" in extract_block
    assert "temperatureRegimeV1SourceComponentFromMeta(itemPosterior)" in extract_block
    assert "temperatureRegimeV1SourceComponentFromMeta(qMeta)" in extract_block
    assert "observed_temperature_trajectory_regime_v1" in panel_block
    assert "path_mu_c" in panel_block
    assert "path_sigma_c" in panel_block
    assert "path_aggregation" in panel_block
    assert "path_candidates" in panel_block
    assert "kinematic_baseline_mu" in panel_block
    assert "regime_probs" in panel_block
    assert "forcing_onset_probs" in panel_block
    assert "peak_hour_distribution" in panel_block
    assert "路径快照" in panel_block
    assert "v1 后端轨迹" in panel_block
    assert "强度缩放" in panel_block
    assert "独立物理证据单独展示，不在前端二次扣减。" in panel_block
    assert "temperature-regime-v1-summary-grid" in panel_block
    assert "temperature-regime-v1-fit-strip" in panel_block
    assert "const regimeV1PanelHtml = renderTemperatureTrajectoryRegimeV1Panel(item, payload" in physical_block
    assert "${observedDynamicLayout ? '' : regimeV1PanelHtml}" in physical_block
    assert "const regimeV1PanelHtml = renderTemperatureTrajectoryRegimeV1Panel(item, item" in fallback_block
    assert "const regimeV1PanelHtml = renderTemperatureTrajectoryRegimeV1Panel(data, data" in html
    assert "const regimeV1PanelHtml = renderTemperatureTrajectoryRegimeV1Panel(item, payload || item" in html
    assert "{ key: 'temperature_trajectory', label: 'v1轨迹'" in ctp_chain_block


def test_temperature_posterior_ctp_chain_is_null_safe_for_missing_bayes_chain():
    block = _function_block("renderTemperaturePosteriorCtpChainPanel")

    assert ".map(([label, rawRow]) => {" in block
    assert "const row = rawRow && typeof rawRow === 'object' ? rawRow : {};" in block
    assert "const chain = bayesChainForDistributionRow(row) || {};" in block
    assert "chain.likelihood_prob ?? row.q_likelihood" in block


def test_temperature_trajectory_timeline_prioritizes_hourly_awc_reports():
    html = UI_INDEX.read_text(encoding="utf-8")
    block = _function_block("renderTemperatureTrajectoryTimeline")
    dedupe_block = _function_block("eventIntelTimelineDistinctAwcRows")

    assert "eventIntelTimelineDistinctAwcRows(rows)" in block
    assert "最新整点 AWC" in block
    assert "报文最高温" in block
    assert "日高变化" in block
    assert "物理证据变化" in block
    assert "历史整点报文" in block
    assert "非整点重复刷新已折叠" in block
    assert "仅保留产生新 AWC 整点报文的记录" in block
    assert "最新快照" not in block
    assert "等待 AWC 原始报文" not in block
    assert "awc_raw" not in block
    assert "METAR" not in block
    assert "indexByKey" in dedupe_block
    assert "output[indexByKey.get(key)] = row" in dedupe_block
    assert ".event-trajectory-timeline-conclusion" in html
    assert ".event-trajectory-timeline-history" in html


def test_event_bayes_likelihood_step_uses_merged_diagnostics_and_weights():
    block = _function_block("eventBayesLikelihoodChannels")
    step_block = _function_block("renderEventBayesLikelihoodStep")

    assert "eventIntelMergeDistributionDiagnostics(eventIntelEffectiveIntradayDistribution(item), diagnosticRows)" in block
    assert "eventBayesPosteriorRows(item, 1, effectiveRows)" in block
    assert "channelEvidence" in block
    assert "observedConstraintApplied ? '实况约束已应用' : ''" in block
    assert "channelWeight(channel)" in step_block
    assert "channelContribution(channel)" in step_block
    assert "if (channel.status)" in step_block
    assert "statusParts.join(' · ')" in step_block
    assert "最强来源" in step_block
    assert "按目标桶概率排序" in step_block


def test_model_likelihood_panel_exposes_qm_bias_and_model_hit_rates():
    html = UI_INDEX.read_text(encoding="utf-8")
    member_block = _function_block("buildModelMemberSummary")
    hit_block = _function_block("renderSourcePeakHitRates")
    step_block = _function_block("renderEventBayesLikelihoodStep")
    raw_change_block = _function_block("renderModelRawTopChangePanel")

    assert "raw 成员桶 Top 3" in member_block
    assert "QM 成员桶 Top 3" in member_block
    assert "q_ensemble_qm_raw_bucket，不是 q_raw 汇总列" in member_block
    assert "QM偏差校正" in member_block
    assert "formatModelMemberBiasCell" in member_block
    assert "最大校正" in html
    assert "modelOnly" in hit_block
    assert "各模型 Hit Rate" in hit_block
    assert "sourcePeakHitCoverageChips" in hit_block
    assert "sourcePeakHitCoverageProvenanceNote" in hit_block
    assert "样本来源已分层" in _function_block("sourcePeakHitCoverageProvenanceNote")
    assert "避免和原生实时采集混看" in _function_block("sourcePeakHitCoverageProvenanceNote")
    assert "renderSourcePeakModelHitMatrix" in hit_block
    assert "source-peak-hit-card-bucket" not in hit_block
    assert "renderSourcePeakHitRates(item, null, { modelOnly: true })" in step_block
    assert '<div class="bayes-evidence-model-subpanel model-hit-rate-panel">' in step_block
    assert '<details class="bayes-evidence-model-secondary model-hit-rate-panel">' not in step_block
    assert "更新时间轴：上一快照 → 当前快照" in raw_change_block
    assert "renderModelRawTopUpdateTimeline(previousSnapshotMeta, currentSnapshotMeta)" in raw_change_block
    assert "modelRawTopSnapshotMeta(item)" in raw_change_block
    assert "previousSnapshotAt" in html
    assert "currentSnapshotAt" in html
    assert ".model-raw-top-change-timeline" in html
    assert "model_bucket_rule_comparison" in html
    assert "入桶规则对照" in html
    assert "同一批模型成员按 round / floor / ceil 三种入桶规则重算" in html


def test_qm_bias_flow_emphasizes_bucket_and_probability_values():
    html = UI_INDEX.read_text(encoding="utf-8")
    block = _function_block("renderTemperaturePosteriorCtpChainPanel")

    assert "qm-bias-flow-bucket" in block
    assert "qm-bias-flow-prob" in block
    assert 'class="qm-bias-flow-prob">${escapeHtml(pctText(posteriorTop && posteriorTop.value))}</strong>' in block
    assert "renderQRawToBayesChainPanel(item, distribution)" in html
    assert "return renderTemperaturePosteriorCtpChainPanel(item, distribution);" in html
    assert ".qm-bias-flow-bucket" in html
    assert "font-size:var(--font-md);" in html
    assert ".qm-bias-flow-meta" in html
    assert "opacity:.78;" in html


def test_mean_bucket_missing_reason_is_visible_for_pending_model_rows():
    html = UI_INDEX.read_text(encoding="utf-8")
    formatter_block = _function_block("formatMeanBucketNaReason")
    expected_block = _function_block("renderExpectedBucketBlock")
    summary_block = _function_block("buildMeanBucketSummary")

    assert "model_not_materialized_yet" in formatter_block
    assert "市场已发现，等待模型分布生成" in formatter_block
    assert "item && item.mean_bucket_na_reason" in expected_block
    assert "待生成" in expected_block
    assert "bucket-focus-value is-na" in expected_block
    assert "均值桶缺失" in summary_block
    assert ".bucket-focus-value.is-na" in html


def test_pending_model_blocks_frontend_physical_verdict_fallback():
    html = UI_INDEX.read_text(encoding="utf-8")
    verdict_block = _function_block("buildTodayPhysicalEvidenceVerdict")

    assert "function eventIntelPosteriorSsotMissingReason" in html
    assert "function isMarketDiscoveryPendingModelItem" in html
    assert "function eventIntelPosteriorSsotPendingForDisplay" in html
    assert "function marketDiscoveryPendingDisplayReason" in html
    assert "eventIntelPosteriorSsotPendingForDisplay(" in verdict_block
    assert "return {\n          available: false," in verdict_block
    assert "后验SSOT未物化" in verdict_block
    assert "等待后端生成 posterior_ssot_fact 后再展示物理总判。" in verdict_block
    assert verdict_block.index("pendingPosteriorReason") < verdict_block.index("targetSwitchPhysicalModifier")


def test_pending_model_blocks_distribution_top_spotlight_fallback():
    block = _function_block("renderDistributionTable")
    pending_idx = block.index("const posteriorSsotPendingReason = eventIntelPosteriorSsotPendingForDisplay(itemRef, distribution);")
    ssot_idx = block.index("const posteriorSsotForSpotlight = eventIntelPosteriorSsotSection(itemRef);")
    hard_outlook_idx = block.index("const hardOutlookLabel = targetOutlookForSpotlight")

    assert pending_idx < ssot_idx
    assert "source: 'posterior_ssot_pending'" in block
    assert block.index("if (posteriorSsotPendingReason)") < hard_outlook_idx
    assert "const capitalLabel = String(capitalPlanForSpotlight" not in block
    assert "source: 'capital_plan'" not in block
    assert "source === 'capital_plan'" not in block
    assert "source: 'distribution_top'" not in block
    assert "posteriorSpotlightPrimary.source === 'distribution_top'" not in block
    assert "posteriorSpotlightCompactText(posteriorSsotPendingReason, 104)" in block


def test_pending_model_blocks_intraday_physical_axis_neutral_fallback():
    html = UI_INDEX.read_text(encoding="utf-8")
    block = _function_block("renderEventIntelIntradayEvidenceAxis")
    pending_idx = block.index("const pendingPosteriorReason = eventIntelPosteriorSsotPendingForDisplay(")
    evidence_idx = block.index("const evidence = (Array.isArray(data.evidence_conclusions)")

    assert "function renderEventIntelIntradayEvidenceAxis(payload, item = null)" in html
    assert pending_idx < evidence_idx
    assert "后验SSOT未物化" in block
    assert "等待模型分布生成" in block
    assert "不把空证据解释为中性或拉扯" in block
    assert "renderEventIntelIntradayEvidenceAxis(payload, item)" in html
    assert "renderEventIntelIntradayEvidenceAxis(payload || {}, item)" in html


def test_target_bucket_temperature_progress_renders_from_ssot_outlook():
    html = UI_INDEX.read_text(encoding="utf-8")
    progress_block = _function_block("renderTargetBucketTrajectoryProgress")
    pace_block = _function_block("eventIntelTargetPaceProgress")
    state_block = _function_block("eventIntelTargetProgressState")
    fallback_block = _function_block("renderEventIntelTrajectoryFallbackPanel")
    hub_block = _function_block("renderEventIntelPhysicalEvidenceHub")

    assert "目标桶进度" in progress_block
    raw_target_start = progress_block.index("const rawTargetLabel =")
    raw_target_end = progress_block.index("const targetLabel =", raw_target_start)
    raw_target_block = progress_block[raw_target_start:raw_target_end]
    assert "targetSelection && targetSelection.targetLabel" in raw_target_block
    assert "outlook.target_bucket" not in raw_target_block
    assert "path.target_bucket" not in raw_target_block
    assert "targetPath.bucket" not in raw_target_block
    assert "item.target_bucket" not in raw_target_block
    assert "outlookMatchesTarget ? outlook.target_lower_edge_c : Number.NaN" in progress_block
    assert "targetPathMatches ? targetPath.threshold_c : Number.NaN" in progress_block
    assert "outlookMatchesTarget ? outlook.gap_to_target_lower_edge_c : Number.NaN" in progress_block
    assert "targetPathMatches ? targetPath.path_probability_pct : Number.NaN" in progress_block
    assert "const fallbackTargetSelection = eventIntelTargetBucketSelectionFromSnapshot(item, item, {}, {});" in fallback_block
    assert fallback_block.index("fallbackTargetSelection,") < fallback_block.index("fallbackPathDistribution,", fallback_block.index("fallbackTargetSelection,"))
    assert "领先" in state_block
    assert "正常" in state_block
    assert "落后" in state_block
    assert "target_reach_state" in state_block
    assert "gap_to_target_lower_edge_c" in progress_block
    assert "target_lower_edge_c" in progress_block
    assert "eventIntelTargetPaceProgress" in progress_block
    assert "event-target-progress-combo-head" in progress_block
    assert "event-target-progress-combo-values" in progress_block
    assert "event-target-progress-gate-badge" in progress_block
    assert "gateBadgeClass" in progress_block
    assert "门控 ${gateStatus}" in progress_block
    assert "实况" in progress_block
    assert "目标节奏" in progress_block
    assert "event-target-progress-line-row" not in progress_block
    assert "required_rate_c_per_hour" in pace_block
    assert "actual_rate_c_per_hour" in pace_block
    assert "progressTooltip" in progress_block
    assert "实况：" in progress_block
    assert "目标：" in progress_block
    assert "差值：" in progress_block
    assert "门控状态：" in progress_block
    assert "前端不重算目标桶或概率" not in progress_block
    assert "renderTargetBucketTrajectoryProgress(" in fallback_block
    assert "renderTargetBucketTrajectoryProgress(payload, item, targetSelection, temperaturePathDistribution, targetOutlookForHub)" in hub_block
    assert ".event-target-progress" in html
    assert ".event-target-progress-summary" in html


def test_model_hit_rate_ui_uses_exact_hits_only():
    hit_block = _function_block("renderSourcePeakHitRates")
    model_table_block = _function_block("formatSourcePeakModelHitTable")
    cell_block = _function_block("formatSourcePeakHitCell")
    note_block = _function_block("sourcePeakHitLowNote")
    score_block = _function_block("formatPredictionHitStats")

    combined = "\n".join([hit_block, model_table_block, cell_block, note_block, score_block])
    assert "只统计最终结算桶精准命中" in hit_block
    assert "最高概率桶" in model_table_block
    assert "原/校值" not in model_table_block
    assert "精准命中=" in cell_block
    assert "`${Math.round(hit)}/${Math.round(count)}" in cell_block
    assert "样本类型=" in cell_block
    assert "sample_mix" in _function_block("sourcePeakHitProvenanceCounts")
    assert "sourcePeakHitProvenanceDetailText" in cell_block
    assert "外部历史重建" in _function_block("sourcePeakHitProvenanceText")
    assert "external_history_reconstruction" in _function_block("sourcePeakHitCoverageProvenanceCounts")
    assert "外部历史重建 lead" in _function_block("sourcePeakHitCoverageChips")
    assert "外部历史重建/history_only 样本已单独标注" in _function_block("renderSourcePeakModelHitMatrix")
    assert "source-peak-hit-matrix" in _function_block("renderSourcePeakModelHitMatrix")
    assert "只统计最终结算桶的精准命中" in note_block
    assert "精准命中 ${exact}" in score_block
    assert "within_1_rate" not in combined
    assert "within_2_rate" not in combined
    assert "±1" not in combined
    assert "±2" not in combined
    assert "相邻桶" not in combined


def test_daily_trade_advice_is_compact_and_does_not_frontend_compute_execution_blocker():
    html = UI_INDEX.read_text(encoding="utf-8")
    block = _function_block("formatDailyTradeAdviceSummary")

    assert "daily-trade-focus-grid" in block
    assert "执行提示" in block
    assert "executionHintValue" in block
    assert "自动交易卡点" not in block
    assert "executionBlockerValue" not in block
    assert "无硬卡点" not in block
    assert "展开审计明细" in block
    assert "决策摘要" in block
    assert "主 posterior助推来源" not in block
    assert "目前没有助推或只作弱解释的通道" not in block
    assert ".daily-trade-focus-grid" in html


def test_daily_trade_advice_never_falls_back_target_to_posterior_top():
    block = _function_block("formatDailyTradeAdviceSummary")

    assert "targetLabel || topLabel" not in block
    assert "const targetBucketText = targetHasSsotLabel ? fmtBucket(targetLabel) : '目标桶缺失';" in block
    assert "const targetProbabilityLabel = targetHasSsotLabel ? eventIntelFinalBucketProbabilityLabel(targetLabel) : '目标桶概率';" in block
    assert "item.single_temperature_top_label || item.model_top_label" not in block
    assert "item.single_temperature_top_q || item.model_top_q" not in block


def test_market_context_and_decision_copy_keep_model_reference_separate():
    html = UI_INDEX.read_text(encoding="utf-8")
    block = _function_block("renderEventIntelAnomalyDynamic")

    assert "结论 · 最终概率 · 当前建议" in html
    assert "结论 · 校准后桶概率 · 当前建议" not in html
    assert "模型参考最高 ${marketFlow.model_top_label || '—'}" in block
    assert "模型 ${marketFlow.model_top_label || '—'}" in block
    assert "marketFlow.model_top_label || item.model_top_label" not in block


def test_posterior_settlement_candidate_uses_gate_wording_not_execution_blocker():
    block = _function_block("formatPosteriorSettlementAutoTradeStatus")

    assert "策略门控：${blockReason}" in block
    assert "观察/门控" in block
    assert "阻断：${blockReason}" not in block
    assert "观察/阻断" not in block


def test_mean_bucket_trade_context_does_not_fallback_to_generic_target_bucket():
    block = _function_block("buildMeanBucketTradeContext")
    target_start = block.index("const targetLabel =")
    target_end = block.index("const secondLabel =", target_start)
    target_block = block[target_start:target_end]

    assert "const posteriorSelection = eventIntelTargetBucketSelectionFromSnapshot(item, payload, decision, plan);" in block
    assert "const targetLabel = String(posteriorSelection.targetLabel || '');" in target_block
    assert "selection.target_bucket" not in target_block
    assert "plan.target_bucket" not in target_block
    assert "payload.target_bucket" not in target_block
    assert "decision.target_bucket" not in target_block
    assert "top.label" not in target_block


def test_awc_live_observation_marks_non_event_day_observations_as_auxiliary():
    block = _function_block("renderAwcLiveObservation")

    assert "awcNonEventObservation" in block
    assert "wuNonEventObservation" in block
    assert "usable_for_event" in block
    assert "非事件日辅助" in block
    assert "AWC 最新观测（非事件日）" in block
    assert "AWC 最新观测" in block
    assert "非事件日桶" in block
    assert "观测桶" in block
    assert "AWC 硬实况最高" in block


def test_awc_live_observation_uses_awc_metrics_when_compact_comparison_is_sparse():
    block = _function_block("renderAwcLiveObservation")
    temp_start = block.index("const awcTodayMax =")
    temp_end = block.index("const wuTemp =", temp_start)
    temp_block = block[temp_start:temp_end]

    assert "eventIntelFirstFiniteNumber(" in temp_block
    assert "awc.today_max_c" in temp_block
    assert "metrics.zuuu_metar_raw_awc_today_max_c" in temp_block
    assert "awc.current_temp_c" in temp_block
    assert "metrics.zuuu_metar_raw_awc_latest_temp_c" in temp_block
    assert "metrics.metar_temperature_c" in temp_block
    assert "const awcHasTodayMax = Number.isFinite(awcTodayMax);" in temp_block
    assert "const awcTemp = awcHasTodayMax ? awcTodayMax : awcCurrentTemp;" in temp_block
    assert "wu_twc" not in temp_block


def test_awc_live_observation_does_not_label_latest_current_as_hard_max():
    block = _function_block("renderAwcLiveObservation")
    today_max_start = block.index("const awcTodayMax =")
    today_max_end = block.index("const awcCurrentTemp =", today_max_start)
    today_max_block = block[today_max_start:today_max_end]

    assert "awc.current_temp_c" not in today_max_block
    assert "metrics.zuuu_metar_raw_awc_latest_temp_c" not in today_max_block
    assert "metrics.metar_temperature_c" not in today_max_block
    assert "const awcReadingLabel = awcNonEventObservation" in block
    assert ": (awcHasTodayMax ? 'AWC 硬实况最高' : 'AWC 最新观测');" in block
    assert "escapeHtml(awcReadingLabel)" in block


def test_corrected_top_summary_does_not_reuse_q_round_for_weighted_sources():
    strict_block = _function_block("topBucketFromDistributionStrict")
    strict_list_block = _function_block("topBucketsFromDistributionStrict")
    summary_block = _function_block("renderCorrectedTopBucketBlock")
    corrected_lines_block = _function_block("buildCorrectedTopLines")
    raw_block = _function_block("renderRawTopBucketBlock")

    assert "row.q_round" not in strict_block
    assert "row.q_round" not in strict_list_block
    assert "topBucketByFirstAvailableStrict(dist, keys)" in summary_block
    assert "topBucketFromDistributionStrict(dist, 'q_equal')" in summary_block
    assert "topBucketFromDistributionStrict(dist, 'q_auto')" in summary_block
    assert "topBucketFromDistributionStrict(dist, 'q_scenario')" in summary_block
    assert "firstFiniteRowValue(originalRow, ['q_min'])" in summary_block
    assert "firstFiniteRowValue(originalRow, ['q_max'])" in summary_block
    assert "const originalLabel = rawTop.label || '';" in summary_block
    assert "|| topLabel" not in summary_block
    assert "item && item.model_top_label" not in summary_block
    assert "topQ ||" not in summary_block
    assert "const correctedTopBlock = renderCorrectedTopBucketBlock(item, distribution);" in _function_block(
        "formatBucketConsistency"
    )
    assert "return renderCorrectedTopBucketBlock(item, distribution);" in _function_block(
        "formatCorrectedTopBlock"
    )
    assert "const topLabel = item.model_top_label" not in _function_block("formatBucketConsistency")
    assert "const topLabel = item.model_top_label" not in _function_block("formatCorrectedTopBlock")
    assert "topBucketByFirstAvailableStrict(distribution, ['q_raw', 'q_round'])" in raw_block
    assert "topSingleTemperatureBucketFromDistribution" not in raw_block
    assert "item.single_temperature_top_label" not in raw_block
    assert "item.model_top_label" not in raw_block
    assert "topBucketsFromDistributionStrict(distribution, entry.qKey, 2)" in corrected_lines_block
    assert "topBucketsFromDistribution(distribution, entry.qKey, 2)" not in corrected_lines_block


def test_corrected_top_summary_groups_qraw_methods_weights_and_qm_delta():
    html = UI_INDEX.read_text(encoding="utf-8")
    summary_block = _function_block("renderCorrectedTopBucketBlock")

    assert "模型原始最高桶" in summary_block
    assert "模型校准摘要" in summary_block
    assert "动态融合概率列" not in summary_block
    assert "模型校准增量" in summary_block
    assert "qmDeltaText" in summary_block
    assert "模型原始入桶口径" in summary_block
    assert "q_round" in summary_block
    assert "q_floor" in summary_block
    assert "q_ceil" in summary_block
    assert "q_raw_static" in summary_block
    assert "模型侧诊断" in summary_block
    assert "模型校准组件" in summary_block
    assert "只作为模型诊断" in summary_block
    assert "动态权重" in summary_block
    assert "场景权重" in summary_block
    assert "全部口径最高桶对照" not in summary_block
    assert "summaryTable" not in summary_block
    assert "${summaryTable}" not in summary_block
    assert ".bucket-summary-grid.is-qraw-redesign" in html
    assert ".bucket-qraw-method-grid" in html
    assert ".bucket-weight-mode-grid" in html
