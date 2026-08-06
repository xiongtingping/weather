"""Bayes × adaptive basket target contracts and no-live production compiler.

The package is deliberately a boundary layer, not a new production service.
It validates strict-as-of calibration inputs, materializes a versioned NWP P0
probability envelope, resolves station policy artifacts, and feeds the existing
zero-order ``AdaptiveBasketPlan`` evaluator.  It has no network, scheduler,
database connection, or order-gateway dependency in its hot path.
"""

from .calibration import (
    fit_model_calibration_artifact,
    materialize_p0,
    normal_bucket_probability,
    resolve_active_calibration_artifact,
    select_latest_complete_model_run_bundle,
)
from .calibration_evidence import SCHEMA as CALIBRATION_EVIDENCE_SCHEMA, build_calibration_evidence
from .calibration_ingress import (
    FORMAL_NATIVE as CALIBRATION_FORMAL_NATIVE,
    NO_LIVE_REPLAY as CALIBRATION_NO_LIVE_REPLAY,
)
from .canonical_contract import (
    CanonicalActivationFact,
    CanonicalContract,
    CanonicalModelFact,
    CanonicalResidualFact,
    CanonicalTruthFact,
)
from .canonical_ingress import ingest_canonical_contract
from .gap_ledger import (
    GapLedger,
    GapRecord,
    build_gap_ledger,
    gap_records_from_canonical_contract,
)
from .model_builders import (
    BuilderSpec,
    build_all_scopes,
    build_scope,
    builder_for,
    ingest_built_scope,
    registered_builders,
)
from .no_live_calibration_authority import build_no_live_calibration_artifact_packet
from .et_history import (
    SCHEMA as ET_HISTORY_SCHEMA,
    TARGET_TIMEZONE as ET_HISTORY_TIMEZONE,
    build_et_history_evidence,
    build_et_truth_rows,
    normalize_et_model_rows,
)
from .model_arrival import (
    BLOCKED_SCHEMA as MODEL_ARRIVAL_BLOCKED_SCHEMA,
    INDEX_SCHEMA as MODEL_ARRIVAL_INDEX_SCHEMA,
    SCHEMA as MODEL_ARRIVAL_SCHEMA,
    build_blocked_receipt,
    load_model_arrival_receipts,
    model_arrival_key,
    validate_model_arrival,
)
from .native_model_decode import (
    CURVE_SCHEMA as NATIVE_MODEL_CURVE_SCHEMA,
    SCHEMA as NATIVE_MODEL_DECODE_SCHEMA,
    decode_native_temperature_points,
    materialize_native_daily_curves,
    parse_grib_get_temperature_output,
)
from .native_ensemble_contract import (
    MIN_CROSS_ENSEMBLE_FAMILIES,
    P0_PROBABILITY_PATHWAY,
    native_ensemble_family_set_hash,
    validate_cross_native_ensemble_gate,
    validate_cross_native_ensemble_weights,
    validate_native_ensemble_transport_gate,
)
from .native_ensemble_oos import (
    build_native_ensemble_oos,
    persist_native_ensemble_oos,
)
from .native_ensemble_p0 import compile_native_ensemble_p0
from .contracts import (
    CalibrationActivation,
    ContractError,
    CurrentAsOfModelRun,
    FORMAL_ENSEMBLE_METHOD_VERSION,
    MIN_FORMAL_ENSEMBLE_MODELS,
    ModelCalibrationArtifact,
    ModelEnsembleArtifact,
    ModelEnsembleSelectionEvidence,
    ProbabilityEnvelope,
    ResidualObservation,
    content_sha256,
    model_set_hash,
    strict_asof,
)
from .event_contracts import (
    CanonicalObservationEvent,
    ForecastRevision,
    validate_canonical_observation_sequence,
    validate_forecast_revision_chain,
)
from .stage_bias import StageProbabilityBiasContract
from .cash_phase import (
    BasketCashPhaseDecision,
    BasketCashPhaseInput,
    BasketCashPhasePolicyArtifact,
    CashPhase,
    OrderSizingDecision,
    evaluate_cash_phase,
    resolve_cash_phase_policy,
)
from .policy import (
    GlobalBasketPriorArtifact,
    PolicyArtifact,
    ResolvedStationBasketPolicy,
    StationBasketPolicyActivation,
    resolve_station_policy,
)
from .station_policy_candidate import (
    SCHEMA as STATION_POLICY_CANDIDATE_SCHEMA,
    assert_candidate_bundle_boundary,
    build_station_policy_candidate,
)
from .station_policy_forward_outcome_binding import (
    UTILITY_CONTRACT_ARTIFACT_ID,
    UTILITY_CONTRACT_POINTER_SCHEMA,
    UTILITY_CONTRACT_SCHEMA,
    UTILITY_CONTRACT_VERSION,
    assert_station_policy_utility_binding,
    assert_station_policy_utility_contract,
    assert_station_policy_utility_contract_pointer,
    build_station_policy_utility_contract,
    build_station_policy_utility_contract_pointer,
    validate_station_policy_utility_binding,
)
from .runtime import (
    DELTA_V2_OPTIMIZER_ID,
    DELTA_V2_OPTIMIZER_SCHEMA,
    DeltaBasketScoreV2Optimizer,
    compile_target_plan,
    snapshot_from_probability_envelope,
)
from .stages import select_current_stage
from .production import (
    PRODUCTION_EXECUTION_MODE,
    PRODUCTION_PLAN_SCHEMA,
    commit_production_candidate_atomic,
    compile_production_plan,
)
from .materialization import (
    MATERIALIZATION_SCHEMA,
    insert_probability_envelope,
    materialize_p0m,
    materialize_p1,
)
from .probability_bridge import (
    SCHEMA as PROBABILITY_XSB_BRIDGE_SCHEMA,
    build_xsb_projection,
    canonical_replay_probability_bridges,
    install_probability_bridge_schema,
    load_current_probability_bridge,
    materialize_probability_bridge,
    probability_bridge_schema_status,
)
from .taf_revision import (
    TAF_ARTIFACT_SCHEMA,
    TAF_AUTHORITY_SCOPE,
    TAF_CORRELATION_METHOD,
    TAF_METHOD_VERSION,
    TAF_PROOF_SCHEMA,
    TafRevisionArtifact,
    TafRevisionProof,
    compile_taf_revision_proof,
    validate_taf_revision_proof,
)
from .taf_calibration import (
    MIN_TAF_CALIBRATION_SAMPLE_N,
    MIN_TAF_HOLDOUT_N,
    TAF_CALIBRATION_SCORECARD_SCHEMA,
    TafCalibrationSample,
    fit_taf_revision_artifact,
)
from .market_materialization import (
    MARKET_MATERIALIZATION_SCHEMA,
    build_confirmed_inventory_snapshot,
    build_official_ws_snapshot,
    build_order_state_snapshot,
    insert_market_evidence_bundle,
)
from .market_ingress import (
    INGRESS_SCHEMA,
    load_official_ws_quotes,
    materialize_market_evidence,
    materialize_official_ws_evidence_cache,
)
from .market_registry import (
    apply_target_market_registry_schema,
    export_shared_live_registry,
    ingest_target_market_registry,
    load_active_registry_rows,
    load_started_registry_rows,
)
from .inventory_producer import build_confirmed_inventory_ingress, build_no_live_inventory_ingress
from .order_state_producer import build_no_live_order_state_ingress
from .resident_market_ingress import materialize_resident_no_live_market_evidence
from .artifact_ingress import ingest_artifact_bundle
from .canonical_ingress import materialize_p1_from_canonical_observation
from .canonical_fact_ingress import (
    ingest_canonical_observation_evidence,
    stage_canonical_observation_evidence,
)
from .canonical_observation_export import export_canonical_observation_evidence
from .production_io import (
    ProductionArtifactBundle,
    compile_production_cycle_from_ssot,
    load_materialized_probability_envelopes,
    load_production_artifacts,
    load_production_market_snapshot,
)
from .atomic_ledger import (
    AtomicLedgerError,
    InjectedAtomicFault,
    apply_target_ledger_schema,
    arbitrate_capital_requests,
    build_target_atomic_bundle,
    close_target_cycle,
    commit_target_atomic_bundle,
    initialize_capital_pool,
    target_ledger_schema_status,
    transition_target_cycle_state,
)
from .execution_reconciliation import (
    ExecutionCasError,
    ReconciliationError,
    assert_basket_hold_only,
    assert_execution_version_cas,
    build_execution_version_vector,
    can_submit_next_order,
    record_order_event,
    resolve_reconciliation,
)
from .dependency_audit import audit_official_ws_dependencies
from .promotion_evidence import (
    CASH_SCORECARD_SCHEMA,
    STATION_SCORECARD_SCHEMA,
    validate_cash_phase_forward_scorecard,
    validate_station_policy_forward_scorecard,
)
from .forward_scorecard import (
    SCHEMA as FORWARD_SCORECARD_SCHEMA,
    build_forward_scorecard,
    build_forward_scorecard_receipt,
)
from .readiness_scope import resolve_target_readiness_scopes

__all__ = [
    "CalibrationActivation",
    "CALIBRATION_EVIDENCE_SCHEMA",
    "ET_HISTORY_SCHEMA",
    "ET_HISTORY_TIMEZONE",
    "BasketCashPhaseDecision",
    "BasketCashPhaseInput",
    "BasketCashPhasePolicyArtifact",
    "MODEL_ARRIVAL_BLOCKED_SCHEMA",
    "MODEL_ARRIVAL_SCHEMA",
    "NATIVE_MODEL_CURVE_SCHEMA",
    "NATIVE_MODEL_DECODE_SCHEMA",
    "CashPhase",
    "ContractError",
    "MIN_CROSS_ENSEMBLE_FAMILIES",
    "P0_PROBABILITY_PATHWAY",
    "native_ensemble_family_set_hash",
    "validate_cross_native_ensemble_gate",
    "validate_cross_native_ensemble_weights",
    "validate_native_ensemble_transport_gate",
    "build_native_ensemble_oos",
    "persist_native_ensemble_oos",
    "compile_native_ensemble_p0",
    "CurrentAsOfModelRun",
    "FORMAL_ENSEMBLE_METHOD_VERSION",
    "MIN_FORMAL_ENSEMBLE_MODELS",
    "GlobalBasketPriorArtifact",
    "ModelCalibrationArtifact",
    "ModelEnsembleArtifact",
    "ModelEnsembleSelectionEvidence",
    "MATERIALIZATION_SCHEMA",
    "TAF_ARTIFACT_SCHEMA",
    "TAF_AUTHORITY_SCOPE",
    "TAF_CORRELATION_METHOD",
    "TAF_METHOD_VERSION",
    "TAF_PROOF_SCHEMA",
    "MARKET_MATERIALIZATION_SCHEMA",
    "OrderSizingDecision",
    "PolicyArtifact",
    "PRODUCTION_EXECUTION_MODE",
    "PRODUCTION_PLAN_SCHEMA",
    "ProbabilityEnvelope",
    "TafRevisionArtifact",
    "TafRevisionProof",
    "TafCalibrationSample",
    "TAF_CALIBRATION_SCORECARD_SCHEMA",
    "MIN_TAF_CALIBRATION_SAMPLE_N",
    "MIN_TAF_HOLDOUT_N",
    "CanonicalObservationEvent",
    "ForecastRevision",
    "StageProbabilityBiasContract",
    "validate_canonical_observation_sequence",
    "validate_forecast_revision_chain",
    "ProductionArtifactBundle",
    "ResidualObservation",
    "ResolvedStationBasketPolicy",
    "StationBasketPolicyActivation",
    "STATION_POLICY_CANDIDATE_SCHEMA",
    "UTILITY_CONTRACT_ARTIFACT_ID",
    "UTILITY_CONTRACT_POINTER_SCHEMA",
    "UTILITY_CONTRACT_SCHEMA",
    "UTILITY_CONTRACT_VERSION",
    "assert_station_policy_utility_binding",
    "assert_station_policy_utility_contract",
    "assert_station_policy_utility_contract_pointer",
    "build_station_policy_utility_contract",
    "build_station_policy_utility_contract_pointer",
    "validate_station_policy_utility_binding",
    "compile_target_plan",
    "DELTA_V2_OPTIMIZER_ID",
    "DELTA_V2_OPTIMIZER_SCHEMA",
    "DeltaBasketScoreV2Optimizer",
    "build_calibration_evidence",
    "build_et_history_evidence",
    "build_et_truth_rows",
    "build_blocked_receipt",
    "compile_production_plan",
    "commit_production_candidate_atomic",
    "compile_production_cycle_from_ssot",
    "content_sha256",
    "fit_model_calibration_artifact",
    "fit_taf_revision_artifact",
    "insert_probability_envelope",
    "build_confirmed_inventory_snapshot",
    "build_official_ws_snapshot",
    "build_order_state_snapshot",
    "insert_market_evidence_bundle",
    "INGRESS_SCHEMA",
    "load_official_ws_quotes",
    "materialize_market_evidence",
    "materialize_official_ws_evidence_cache",
    "build_confirmed_inventory_ingress",
    "build_no_live_inventory_ingress",
    "build_no_live_order_state_ingress",
    "materialize_resident_no_live_market_evidence",
    "ingest_artifact_bundle",
    "materialize_p1_from_canonical_observation",
    "ingest_canonical_observation_evidence",
    "stage_canonical_observation_evidence",
    "export_canonical_observation_evidence",
    "evaluate_cash_phase",
    "materialize_p0",
    "materialize_p0m",
    "compile_taf_revision_proof",
    "validate_taf_revision_proof",
    "materialize_p1",
    "model_set_hash",
    "normal_bucket_probability",
    "normalize_et_model_rows",
    "load_materialized_probability_envelopes",
    "load_production_artifacts",
    "load_production_market_snapshot",
    "load_model_arrival_receipts",
    "model_arrival_key",
    "MODEL_ARRIVAL_INDEX_SCHEMA",
    "decode_native_temperature_points",
    "materialize_native_daily_curves",
    "parse_grib_get_temperature_output",
    "AtomicLedgerError",
    "InjectedAtomicFault",
    "apply_target_ledger_schema",
    "arbitrate_capital_requests",
    "build_target_atomic_bundle",
    "close_target_cycle",
    "commit_target_atomic_bundle",
    "initialize_capital_pool",
    "target_ledger_schema_status",
    "transition_target_cycle_state",
    "ExecutionCasError",
    "ReconciliationError",
    "assert_basket_hold_only",
    "assert_execution_version_cas",
    "build_execution_version_vector",
    "can_submit_next_order",
    "record_order_event",
    "resolve_reconciliation",
    "audit_official_ws_dependencies",
    "resolve_active_calibration_artifact",
    "select_latest_complete_model_run_bundle",
    "resolve_station_policy",
    "assert_candidate_bundle_boundary",
    "build_station_policy_candidate",
    "resolve_cash_phase_policy",
    "select_current_stage",
    "snapshot_from_probability_envelope",
    "strict_asof",
    "validate_model_arrival",
    "CASH_SCORECARD_SCHEMA",
    "STATION_SCORECARD_SCHEMA",
    "validate_cash_phase_forward_scorecard",
    "validate_station_policy_forward_scorecard",
    "FORWARD_SCORECARD_SCHEMA",
    "build_forward_scorecard",
    "build_forward_scorecard_receipt",
    "resolve_target_readiness_scopes",
]
