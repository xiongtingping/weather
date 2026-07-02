"""Auditable data-role contract for PM weather Bayes inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple


ROLE_PRIOR = "prior"
ROLE_CALIBRATION = "calibration"
ROLE_LIKELIHOOD = "likelihood/evidence"
ROLE_MODEL_EVIDENCE = "model_evidence"
ROLE_BACKGROUND = "background_prior/residual"
ROLE_DISPLAY_TRADEABILITY = "display/tradeability"

CONTRACT_ROLES = frozenset(
    {
        ROLE_PRIOR,
        ROLE_CALIBRATION,
        ROLE_LIKELIHOOD,
        ROLE_MODEL_EVIDENCE,
        ROLE_BACKGROUND,
    }
)

IMPLEMENTATION_ROLES = frozenset(
    {
        ROLE_PRIOR,
        ROLE_CALIBRATION,
        ROLE_LIKELIHOOD,
        ROLE_MODEL_EVIDENCE,
        ROLE_BACKGROUND,
        ROLE_DISPLAY_TRADEABILITY,
    }
)


@dataclass(frozen=True)
class BayesianDataRole:
    source_id: str
    label: str
    temporal_class: str
    contract_role: str
    implemented_roles: Tuple[str, ...]
    usage: str
    modules: Tuple[str, ...]
    fields: Tuple[str, ...]
    safeguards: Tuple[str, ...]
    evidence_class: str = "unspecified"
    priority_rank: int = 100


BAYESIAN_DATA_ROLE_CONTRACT: Tuple[BayesianDataRole, ...] = (
    BayesianDataRole(
        source_id="wu_settlement_page",
        label="WU settlement display page",
        temporal_class="historical",
        contract_role=ROLE_PRIOR,
        implemented_roles=(ROLE_PRIOR,),
        usage="Polymarket settlement display page, not a sensor origin; build historical bucket hit distribution only after settlement_actual is parsed from the finalized page.",
        modules=("bayesian_probability_runtime.build_historical_bayesian_context",),
        fields=(
            "event_runs.settlement_actual",
            "event_daily_temps.settlement_actual",
            "settlement_actual",
            "wu_settlement_page",
            "wu_settlement_page_url",
            "q_bayes_prior_source",
            "q_bayes_historical_samples",
            "q_bayes_historical_bucket_count",
        ),
        safeguards=(
            "historical_only",
            "event_closed_or_settlement_parsed",
            "not_d0_intraday_evidence",
            "not_sensor_origin",
            "read_only_archive",
        ),
        evidence_class="settlement_truth",
        priority_rank=0,
    ),
    BayesianDataRole(
        source_id="market_quote_history",
        label="Historical market quotes, closes, and settlement hit rates",
        temporal_class="historical",
        contract_role=ROLE_CALIBRATION,
        implemented_roles=(ROLE_CALIBRATION,),
        usage="Calibrate quote/implied-probability diagnostics outside the main Bayes posterior.",
        modules=(
            "scan_weather_market.load_q_quote_calibration_bundle",
            "scan_weather_market.predict_q_quote",
        ),
        fields=(
            "q_quote",
            "q_quote_lcb",
            "q_quote_ucb",
            "q_quote_source",
            "q_quote_calibration_version",
        ),
        safeguards=("not_main_posterior_evidence", "runtime_bundle_required"),
        evidence_class="calibration",
        priority_rank=80,
    ),
    BayesianDataRole(
        source_id="model_historical_performance",
        label="Historical model forecast vs actual performance",
        temporal_class="historical",
        contract_role=ROLE_CALIBRATION,
        implemented_roles=(ROLE_CALIBRATION,),
        usage="Adjust active model weights using historical Brier performance.",
        modules=("bayesian_probability_runtime.build_historical_bayesian_context",),
        fields=(
            "q_bayes_model_weight_multipliers",
            "q_bayes_bias_correction_enabled",
            "model_reliability_samples",
        ),
        safeguards=("historical_only", "bounded_weight_multipliers"),
        evidence_class="calibration",
        priority_rank=70,
    ),
    BayesianDataRole(
        source_id="wu_settlement_page_realtime_display",
        label="WU settlement-page realtime display / nowcast",
        temporal_class="realtime",
        contract_role=ROLE_LIKELIHOOD,
        implemented_roles=(ROLE_LIKELIHOOD,),
        usage="Enter Bayes only as P_observed_nowcast display evidence for observed lower bound and trajectory anchoring; it helps define observed temperature trajectory realization and must be checked against ZUUU METAR, never treated as the airport sensor origin.",
        modules=(
            "scan_weather_market.fetch_observed_nowcast_value",
            "scan_weather_market._build_weather_likelihood_features",
            "bayesian_probability_runtime.apply_bayesian_probability_update",
        ),
        fields=(
            "q_observed_nowcast",
            "observed_nowcast_value",
            "weather_likelihood_features.observed_temp",
            "q_bayes_nowcast_evidence",
            "wu_settlement_page",
            "twc_provider",
            "zuuu_metar_observed",
        ),
        safeguards=(
            "no_base_q_round_premix",
            "evidence_only",
            "not_sensor_origin",
            "awc_raw_metar_cross_check_required",
            "temperature_trajectory_realization_anchor",
        ),
        evidence_class="observed_realization",
        priority_rank=20,
    ),
    BayesianDataRole(
        source_id="twc_realtime_display",
        label="Weather.com / TWC realtime display nowcast",
        temporal_class="realtime",
        contract_role=ROLE_LIKELIHOOD,
        implemented_roles=(),
        usage="Target split for Weather.com/TWC realtime display fields; deduplicate with WU realtime display because both are provider display evidence, not airport sensor observations.",
        modules=(
            "scan_weather_market._zuuu_weather_source_lineage",
            "scan_weather_market.fetch_observed_nowcast_value",
        ),
        fields=(
            "twc_realtime_display",
            "twc_provider",
            "observed_nowcast_value",
            "weather_source_lineage.twc_provider",
        ),
        safeguards=(
            "provider_dedup_required",
            "not_sensor_origin",
            "not_settlement_actual",
            "awc_raw_metar_cross_check_required",
        ),
        evidence_class="display_nowcast",
        priority_rank=35,
    ),
    BayesianDataRole(
        source_id="market_realtime_orderbook",
        label="Realtime market odds, volume, bids, and asks",
        temporal_class="realtime",
        contract_role=ROLE_LIKELIHOOD,
        implemented_roles=(ROLE_DISPLAY_TRADEABILITY,),
        usage="Used for display probability, dead/invalid quote state, and trading availability; not main posterior evidence.",
        modules=(
            "ui.server._apply_display_probability_fields_in_place",
            "scan_weather_market.fetch_orderbook",
        ),
        fields=(
            "best_bid",
            "best_ask",
            "display_probability_market_dead",
            "display_probability_market_invalid",
            "display_probability_normalized",
        ),
        safeguards=("invalid_quote_no_model_fallback", "not_main_posterior_evidence"),
        evidence_class="market_display",
        priority_rank=95,
    ),
    BayesianDataRole(
        source_id="physical_model_realtime",
        label="Realtime physical / numerical weather model output",
        temporal_class="realtime",
        contract_role=ROLE_LIKELIHOOD,
        implemented_roles=(ROLE_LIKELIHOOD,),
        usage="Legacy/current physical likelihood feature bundle. It represents physical heating/cooling potential only; any high-temp posterior amplification must be realized by observed temperature trajectory, observed max, and sustained warming-rate evidence before it can outweigh D0 trajectory.",
        modules=(
            "scan_weather_market._build_weather_likelihood_features",
            "bayesian_likelihood_runtime.compute_weather_likelihood_vector",
        ),
        fields=(
            "weather_likelihood_features",
            "weather_likelihood_feature_quality",
            "q_bayes_physical_likelihood_evidence",
            "q_bayes_physical_likelihood_prob",
            "q_bayes_physical_likelihood_hard_reasons",
            "q_bayes_physical_likelihood_component_groups",
        ),
        safeguards=(
            "hard_soft_gate_reasons_required",
            "probability_evidence_enabled",
            "split_model_context_from_observed_residual_required",
            "physical_potential_only",
            "requires_temperature_trajectory_realization",
            "cannot_override_observed_trajectory",
        ),
        evidence_class="physical_potential",
        priority_rank=55,
    ),
    BayesianDataRole(
        source_id="satellite_radar_realtime",
        label="Satellite/radar/METAR realtime auxiliary observations",
        temporal_class="realtime",
        contract_role=ROLE_LIKELIHOOD,
        implemented_roles=(ROLE_LIKELIHOOD,),
        usage="Supply auxiliary physical likelihood features such as radiation, cloud, and METAR observations. Radiation/cloud are physical potential; METAR temperature trajectory fields are observed realization anchors and must be split/weighted accordingly.",
        modules=(
            "scan_weather_market.fetch_open_meteo_satellite_radiation_features",
            "scan_weather_market.fetch_metar_likelihood_features",
            "scan_weather_market.fetch_nws_latest_observation_likelihood_features",
            "scan_weather_market.fetch_mrms_qpe_metadata_features",
            "bayesian_likelihood_runtime.compute_weather_likelihood_vector",
        ),
        fields=(
            "weather_likelihood_aux_source",
            "solar_radiation_observed",
            "cti_observed",
            "metar_temperature_c",
            "precipitation_rate_mm_per_h",
            "observed_precip_rate_mm_per_h",
            "radar_precip_rate_mm_per_h",
            "qpe_precip_rate_mm_per_h",
            "radar_qpe_source_url",
            "radar_qpe_value_source",
            "radar_qpe_value_na_reason",
            "nws_latest_station_observation",
        ),
        safeguards=(
            "auxiliary_features_only",
            "physical_likelihood_only",
            "independent_physics_residual_only",
            "physical_potential_only_when_not_temperature_trajectory",
            "requires_temperature_trajectory_realization",
            "cannot_override_observed_trajectory",
        ),
        evidence_class="mixed_physical_potential_and_observed_realization",
        priority_rank=45,
    ),
    BayesianDataRole(
        source_id="model_derived_physics",
        label="Model-derived cloud, rain, wind, and radiation context",
        temporal_class="realtime",
        contract_role=ROLE_LIKELIHOOD,
        implemented_roles=(),
        usage="Target split for physical context derived from GFS/ICON/ECMWF or their temperature distribution; it may explain physical potential or weakly gate residuals but must not receive strong independent weight on top of P_model, and cannot imply realized warming without observed trajectory confirmation.",
        modules=(
            "scan_weather_market._build_weather_likelihood_features",
            "bayesian_likelihood_runtime.compute_weather_likelihood_vector",
        ),
        fields=(
            "weather_likelihood_features.temperature_distribution",
            "weather_likelihood_features.forecast_peak_temp",
            "weather_likelihood_features.cloud_cover_pct",
            "weather_likelihood_features.precipitation_rate_mm_per_h",
            "weather_likelihood_features.wind_speed_mps",
        ),
        safeguards=(
            "model_context_only",
            "dedupe_with_p_model",
            "no_strong_independent_weight",
            "quality_gate_required",
            "physical_potential_only",
            "requires_temperature_trajectory_realization",
            "cannot_override_observed_trajectory",
        ),
        evidence_class="model_context_physical_potential",
        priority_rank=65,
    ),
    BayesianDataRole(
        source_id="independent_physics_observed",
        label="Independent or semi-independent observed physical residual evidence",
        temporal_class="realtime",
        contract_role=ROLE_LIKELIHOOD,
        implemented_roles=(ROLE_LIKELIHOOD,),
        usage="Target P_independent_physics_residual channel for observed radiation, cloud, precipitation, pressure, wind, and wet-bulb constraints. These are physical potential/residual clues; actual D0 high-temp realization is governed by observed temperature trajectory, observed max, and sustained warming rate.",
        modules=(
            "scan_weather_market.fetch_open_meteo_satellite_radiation_features",
            "scan_weather_market.fetch_metar_likelihood_features",
            "scan_weather_market.fetch_mrms_qpe_metadata_features",
            "bayesian_likelihood_runtime.compute_weather_likelihood_vector",
        ),
        fields=(
            "solar_radiation_observed",
            "cti_observed",
            "metar_temperature_c",
            "observed_precip_rate_mm_per_h",
            "radar_precip_rate_mm_per_h",
            "qpe_precip_rate_mm_per_h",
            "pressure_trend_hpa_3h",
            "wet_bulb_temp_c",
        ),
        safeguards=(
            "observed_or_cross_checked_only",
            "physical_residual_only",
            "independent_physics_residual_only",
            "not_model_member_distribution",
            "quality_gate_required",
            "physical_potential_only",
            "requires_temperature_trajectory_realization",
            "cannot_override_observed_trajectory",
        ),
        evidence_class="independent_physical_potential",
        priority_rank=50,
    ),
    BayesianDataRole(
        source_id="observed_temperature_trajectory",
        label="Observed D0 temperature trajectory, momentum, and realized warming evidence",
        temporal_class="realtime",
        contract_role=ROLE_LIKELIHOOD,
        implemented_roles=(ROLE_LIKELIHOOD,),
        usage="Highest-priority D0 realization evidence: current temperature, observed same-day max, 24h/12h/6h/3h/2h/1h/45m/30m/15m/10m/5m/3m/2m/1m temperature momentum, warming rate, and distance to the local peak window decide whether physical heating potential is actually being realized. Clear sky/radiation potential cannot amplify high-temp buckets when this trajectory or momentum is weak, flat, stale, or contradicted.",
        modules=(
            "scan_weather_market.fetch_zuuu_metar_raw_awc_features",
            "scan_weather_market._build_weather_likelihood_features",
            "bayesian_likelihood_runtime.estimate_peak_temperature_from_features",
            "chengdu_intraday_summary._temperature_stages",
            "scripts.audit_d0_physical_posterior_alignment",
        ),
        fields=(
            "observed_temp_c",
            "observed_today_max_c",
            "metar_temperature_c",
            "temperature_rise_rate_c_per_hour",
            "temperature_momentum_24h_c_per_hour",
            "temperature_momentum_12h_c_per_hour",
            "temperature_momentum_6h_c_per_hour",
            "temperature_momentum_3h_c_per_hour",
            "temperature_momentum_2h_c_per_hour",
            "temperature_momentum_1h_c_per_hour",
            "temperature_momentum_45m_c_per_hour",
            "temperature_momentum_30m_c_per_hour",
            "temperature_momentum_15m_c_per_hour",
            "temperature_momentum_10m_c_per_hour",
            "temperature_momentum_5m_c_per_hour",
            "temperature_momentum_3m_c_per_hour",
            "temperature_momentum_2m_c_per_hour",
            "temperature_momentum_1m_c_per_hour",
            "temperature_momentum_realization_state",
            "temperature_momentum_sustained_warming",
            "observation_hour_local",
            "hours_to_peak_window",
            "peak_estimator_mu_c",
            "high_temp_amplification_guard",
        ),
        safeguards=(
            "observed_realization_evidence",
            "higher_priority_than_physical_potential",
            "temperature_trajectory_required_for_high_temp_amplification",
            "temperature_momentum_answers_physical_potential_realization",
            "multi_horizon_momentum_required_for_sustained_warming",
            "warming_rate_must_be_positive_and_sustained",
            "flat_or_falling_trajectory_blocks_high_temp_amplification",
            "stale_trajectory_cannot_confirm_realization",
            "current_temp_gap_guard_current_lt_soft_forecast_minus_2_and_rise_lte_0",
        ),
        evidence_class="observed_realization",
        priority_rank=10,
    ),
    BayesianDataRole(
        source_id="zuuu_metar_raw_awc",
        label="NOAA AviationWeather Raw METAR for ZUUU observed lower bound",
        temporal_class="realtime",
        contract_role=ROLE_LIKELIHOOD,
        implemented_roles=(ROLE_LIKELIHOOD,),
        usage="Fetch raw ZUUU airport METAR observations through NOAA AviationWeather and supply current temperature plus same-day observed-max lower bound; never a final daily settlement before event close.",
        modules=(
            "scan_weather_market.fetch_zuuu_metar_raw_awc_features",
            "scan_weather_market._build_weather_likelihood_features",
            "bayesian_probability_runtime.apply_bayesian_probability_update",
        ),
        fields=(
            "zuuu_metar_raw_awc_latest_raw",
            "zuuu_metar_raw_awc_latest_at",
            "zuuu_metar_raw_awc_latest_temp_c",
            "zuuu_metar_raw_awc_today_max_c",
            "observed_temp_c",
            "observed_today_max_c",
            "observed_today_max_source",
            "zuuu_metar_observed",
            "weather_source_delay_deformation_alert",
        ),
        safeguards=(
            "observed_lower_bound_only",
            "not_settlement_actual_before_close",
            "merge_fill_no_weak_overwrite",
            "station_id_must_be_zuuu",
            "temperature_trajectory_realization_anchor",
        ),
        evidence_class="observed_realization",
        priority_rank=15,
    ),
    BayesianDataRole(
        source_id="zuuu_metar_archive_iem",
        label="IEM ASOS archive for ZUUU METAR history",
        temporal_class="historical",
        contract_role=ROLE_CALIBRATION,
        implemented_roles=(),
        usage="Historical METAR archive for auditing AWC/WU delay or deformation after data becomes available; not a realtime source and not a settlement page.",
        modules=("scan_weather_market._zuuu_weather_source_lineage",),
        fields=(
            "zuuu_metar_archive_iem",
            "zuuu_metar_archive_iem_role",
            "zuuu_metar_archive_iem_realtime_enabled",
        ),
        safeguards=("historical_archive_only", "not_realtime_nowcast", "not_settlement_actual", "not_sensor_origin"),
        evidence_class="calibration",
        priority_rank=75,
    ),
    BayesianDataRole(
        source_id="twc_provider",
        label="Weather.com / TWC provider wrapper behind WU pages",
        temporal_class="realtime",
        contract_role=ROLE_MODEL_EVIDENCE,
        implemented_roles=(ROLE_MODEL_EVIDENCE,),
        usage="Identify the Weather.com/TWC API/provider layer behind WU forecast and display fields; it is a packaging/provider layer, not the airport observation origin.",
        modules=(
            "scan_weather_market.fetch_wunderground_forecast_peak_metrics",
            "scan_weather_market._zuuu_weather_source_lineage",
        ),
        fields=(
            "twc_provider",
            "twc_provider_role",
            "wu_forecast_peak_source",
            "settlement_source_forecast_peak_c",
        ),
        safeguards=("provider_wrapper_only", "not_sensor_origin", "soft_forecast_only"),
        evidence_class="provider_lineage",
        priority_rank=90,
    ),
    BayesianDataRole(
        source_id="twc_forecast_peak",
        label="Weather.com / TWC daily forecast high",
        temporal_class="realtime",
        contract_role=ROLE_MODEL_EVIDENCE,
        implemented_roles=(ROLE_MODEL_EVIDENCE,),
        usage="Independent Weather.com/TWC daily high fetch for source comparison and audit; it remains display-only until created_at-safe walk-forward and provider dedupe approve any posterior weight.",
        modules=(
            "scan_weather_market.fetch_weather_com_forecast_peak_metrics",
            "scan_weather_market.fetch_wunderground_forecast_peak_metrics",
            "scan_weather_market._zuuu_weather_source_lineage",
        ),
        fields=(
            "twc_forecast_peak",
            "twc_forecast_peak_c",
            "weather_com_forecast_peak_c",
            "weather_com_forecast_peak_source",
            "settlement_source_forecast_peak_c",
            "wu_forecast_peak_c",
            "q_bayes_wu_forecast_evidence",
            "q_bayes_settlement_forecast_evidence",
        ),
        safeguards=(
            "provider_dedup_required",
            "soft_forecast_only",
            "not_observed_nowcast",
            "not_physical_likelihood_component_until_split",
        ),
        evidence_class="soft_forecast",
        priority_rank=60,
    ),
    BayesianDataRole(
        source_id="wu_forecast_high",
        label="WU same-day forecast high",
        temporal_class="realtime",
        contract_role=ROLE_MODEL_EVIDENCE,
        implemented_roles=(ROLE_MODEL_EVIDENCE,),
        usage="Supply P_wu_forecast soft settlement-ecosystem forecast evidence; never a settlement fact, observed nowcast, or independent physical residual.",
        modules=(
            "scan_weather_market.fetch_wunderground_forecast_peak_metrics",
            "scan_weather_market._build_weather_likelihood_features",
            "bayesian_likelihood_runtime.compute_weather_likelihood_vector",
        ),
        fields=(
            "wu_forecast_peak",
            "wu_forecast_peak_c",
            "settlement_source_forecast_peak_c",
            "q_bayes_wu_forecast_evidence",
            "weather_likelihood_features.wu_forecast_peak_c",
        ),
        safeguards=(
            "not_observed_nowcast",
            "not_settlement_actual",
            "not_independent_physics_residual",
            "source_usable_for_settlement_false",
            "soft_forecast_only",
        ),
        evidence_class="soft_forecast",
        priority_rank=60,
    ),
    BayesianDataRole(
        source_id="zuuu_taf_forecast_peak",
        label="ZUUU TAF TX/TN airport short-range forecast",
        temporal_class="realtime",
        contract_role=ROLE_MODEL_EVIDENCE,
        implemented_roles=(),
        usage="Candidate independent strong-soft airport forecast peak channel P_taf_peak; keep disabled until AWC/WU quality alerts are visible and created_at walk-forward calibration approves a non-zero weight.",
        modules=(
            "scan_weather_market.fetch_zuuu_taf_forecast_peak_metrics",
            "bayesian_probability_runtime.apply_bayesian_probability_update",
            "scripts.backtest_ensemble_qm_bucket_probabilities",
        ),
        fields=(
            "taf_forecast_peak_c",
            "taf_forecast_peak_at",
            "taf_forecast_peak_raw",
            "q_bayes_taf_peak_evidence",
            "q_bayes_taf_peak_prob",
            "q_bayes_taf_peak_source",
        ),
        safeguards=(
            "strong_soft_forecast_only",
            "not_observed_nowcast",
            "not_physical_likelihood_component",
            "created_at_walk_forward_weight_required",
            "disabled_until_awc_wu_alert_gate_and_walk_forward_calibration",
            "station_id_must_be_zuuu",
        ),
        evidence_class="soft_forecast",
        priority_rank=58,
    ),
    BayesianDataRole(
        source_id="climo_regime_background",
        label="Chengdu climate and circulation background regime",
        temporal_class="realtime",
        contract_role=ROLE_BACKGROUND,
        implemented_roles=(ROLE_BACKGROUND,),
        usage="Supply P_climo_regime as a slow-variable background layer, weak prior shrinkage, regime label, calibration stratification feature, and risk gate input using visible historical Tmax baseline plus a Sichuan Basin heat-mechanism model: subtropical-high/warm-ridge proxy, low-level warm airmass, clear-sky shortwave radiation, boundary-layer growth, weak ventilation, humidity/cloud/precip suppression, southwest-vortex precip regime, and post-cool rebound setup when available. It is not a strong likelihood, does not decide the target bucket alone, is capped by single-bucket pp impact, and is zero-weighted within 2h of the local peak window.",
        modules=(
            "climo_regime_runtime.load_climo_regime_context",
            "bayesian_probability_runtime.apply_bayesian_probability_update",
            "scripts.backtest_ensemble_qm_bucket_probabilities",
        ),
        fields=(
            "climo_regime_features",
            "q_bayes_climo_regime_prob",
            "q_bayes_climo_regime_evidence",
            "q_bayes_climo_regime_evidence_raw",
            "q_bayes_climo_regime_pp_delta",
            "q_bayes_climo_regime_pp_cap",
            "q_bayes_climo_regime_pp_max_delta",
            "q_bayes_climo_regime_pp_cap_stage",
            "q_bayes_climo_regime_primary_regime",
            "q_bayes_climo_regime_high_temp_gate",
            "q_bayes_climo_regime_explanation",
            "climo_tmax_mean_c",
            "climo_tmax_p50_c",
            "climo_tmax_p90_c",
            "climo_tmax_p95_c",
            "dewpoint_anomaly_c",
            "monsoon_humidity_regime",
            "subtropical_high_regime",
            "basin_ventilation_index",
            "basin_heat_mechanism_model",
            "basin_heat_mechanism_state",
            "basin_heat_mechanism_status",
            "basin_heat_uplift_index",
            "basin_heat_mechanism_active_support_keys",
            "basin_heat_mechanism_active_suppression_keys",
            "primary_regime_label",
            "regime_source",
            "regime_updated_at",
            "regime_confidence",
            "high_temp_potential_gate",
            "temperature_trajectory_priority",
            "climo_reference_used",
            "climo_reference_meta",
            "created_at_guard",
        ),
        safeguards=(
            "slow_variable_background_layer_only",
            "weak_prior_shrinkage_only",
            "not_strong_likelihood",
            "does_not_decide_target_bucket_alone",
            "not_p_obs",
            "not_wu_metar_observed",
            "not_p_model",
            "not_p_physics",
            "created_at_safe_history_only",
            "reference_uses_only_years_before_target_event_year",
            "d0_within_6h_single_bucket_pp_cap_0_5pp",
            "d0_within_2h_direct_weight_zero",
            "cannot_override_observed_trajectory",
        ),
        evidence_class="climate_circulation_background",
        priority_rank=68,
    ),
    BayesianDataRole(
        source_id="current_model_ensemble",
        label="Current maintained GFS/ICON/ECMWF/QM model ensemble distribution",
        temporal_class="realtime",
        contract_role=ROLE_MODEL_EVIDENCE,
        implemented_roles=(ROLE_MODEL_EVIDENCE,),
        usage="Contribute the weighted per-model bucket distribution as explicit model evidence.",
        modules=("bayesian_probability_runtime.apply_bayesian_probability_update",),
        fields=(
            "per_model_q",
            "active_models",
            "q_model_round",
            "q_bayes_model_evidence",
        ),
        safeguards=("dirichlet_model_evidence_only", "no_market_quote_substitution"),
        evidence_class="model_distribution",
        priority_rank=40,
    ),
)


def audit_bayesian_data_role_contract() -> Dict[str, object]:
    ids = [entry.source_id for entry in BAYESIAN_DATA_ROLE_CONTRACT]
    mismatches: List[str] = []
    expected_count = 19
    if len(ids) != expected_count:
        mismatches.append(f"coverage_expected_{expected_count}_got_{len(ids)}")
    if len(set(ids)) != len(ids):
        mismatches.append("duplicate_source_id")
    for entry in BAYESIAN_DATA_ROLE_CONTRACT:
        if entry.contract_role not in CONTRACT_ROLES:
            mismatches.append(f"{entry.source_id}:invalid_contract_role:{entry.contract_role}")
        unknown_impl = [role for role in entry.implemented_roles if role not in IMPLEMENTATION_ROLES]
        if unknown_impl:
            mismatches.append(f"{entry.source_id}:invalid_implemented_roles:{','.join(unknown_impl)}")
        if entry.temporal_class == "historical":
            bad_roles = {
                ROLE_LIKELIHOOD,
                ROLE_MODEL_EVIDENCE,
                ROLE_DISPLAY_TRADEABILITY,
            }.intersection(entry.implemented_roles)
            if bad_roles:
                mismatches.append(f"{entry.source_id}:historical_role_mismatch:{','.join(sorted(bad_roles))}")
        if entry.temporal_class == "realtime":
            bad_roles = {ROLE_PRIOR, ROLE_CALIBRATION}.intersection(entry.implemented_roles)
            if bad_roles:
                mismatches.append(f"{entry.source_id}:realtime_role_mismatch:{','.join(sorted(bad_roles))}")
        if not entry.modules or not entry.fields:
            mismatches.append(f"{entry.source_id}:missing_mapping")
    by_id = {entry.source_id: entry for entry in BAYESIAN_DATA_ROLE_CONTRACT}
    trajectory = by_id.get("observed_temperature_trajectory")
    if trajectory is None:
        mismatches.append("observed_temperature_trajectory:missing")
    else:
        required = {
            "observed_realization_evidence",
            "higher_priority_than_physical_potential",
            "temperature_trajectory_required_for_high_temp_amplification",
            "temperature_momentum_answers_physical_potential_realization",
            "flat_or_falling_trajectory_blocks_high_temp_amplification",
        }
        missing = required.difference(trajectory.safeguards)
        if missing:
            mismatches.append(f"observed_temperature_trajectory:missing_safeguards:{','.join(sorted(missing))}")
    potential_sources = (
        "physical_model_realtime",
        "satellite_radar_realtime",
        "model_derived_physics",
        "independent_physics_observed",
    )
    potential_required = {"requires_temperature_trajectory_realization", "cannot_override_observed_trajectory"}
    for source_id in potential_sources:
        entry = by_id.get(source_id)
        if entry is None:
            mismatches.append(f"{source_id}:missing")
            continue
        missing = potential_required.difference(entry.safeguards)
        if missing:
            mismatches.append(f"{source_id}:missing_physical_potential_safeguards:{','.join(sorted(missing))}")
        if "potential" not in entry.evidence_class:
            mismatches.append(f"{source_id}:evidence_class_not_potential:{entry.evidence_class}")
        if trajectory is not None and entry.priority_rank <= trajectory.priority_rank:
            mismatches.append(f"{source_id}:priority_not_below_temperature_trajectory")
    return {
        "coverage_count": len(ids),
        "coverage_ratio": len(ids) / float(expected_count),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "roles": {entry.source_id: entry.contract_role for entry in BAYESIAN_DATA_ROLE_CONTRACT},
        "implemented_roles": {entry.source_id: entry.implemented_roles for entry in BAYESIAN_DATA_ROLE_CONTRACT},
        "charter": {
            entry.source_id: {
                "label": entry.label,
                "temporal_class": entry.temporal_class,
                "contract_role": entry.contract_role,
                "usage": entry.usage,
                "fields": entry.fields,
                "safeguards": entry.safeguards,
                "evidence_class": entry.evidence_class,
                "priority_rank": entry.priority_rank,
            }
            for entry in BAYESIAN_DATA_ROLE_CONTRACT
        },
        "physical_evidence_charter": {
            "principle": "Physical evidence expresses heating/cooling potential; observed temperature trajectory expresses D0 realization.",
            "temperature_momentum": (
                "Temperature Momentum is observed realization evidence: 24h/12h/6h/3h/2h/1h/45m/30m/15m/10m/5m/3m/2m/1m warming rates answer "
                "whether the heating/cooling potential is being realized strongly and continuously."
            ),
            "priority_order": (
                "settlement_truth_after_close",
                "observed_temperature_trajectory",
                "temperature_momentum_multi_window",
                "observed_nowcast_lower_bound",
                "model_distribution",
                "independent_physical_potential",
                "model_context_physical_potential",
                "soft_forecast",
            ),
            "hard_boundary": (
                "If current_temp < soft_forecast - 2°C and warming_rate <= 0, physical potential "
                "such as clear sky or favorable radiation may remain explanatory but must not amplify high-temperature buckets."
            ),
        },
    }


def contract_by_source_id() -> Mapping[str, BayesianDataRole]:
    return {entry.source_id: entry for entry in BAYESIAN_DATA_ROLE_CONTRACT}
