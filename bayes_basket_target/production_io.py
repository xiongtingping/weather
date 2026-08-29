"""Read-only production adapter for the Bayes × basket target lane.

The adapter is intentionally boring: it reads only the target SSOT tables,
validates every content address and as-of window, joins one official CLOB WS
snapshot to the exact confirmed inventory/order-state generations, and then
calls :func:`compile_production_plan`.  It never writes SQLite, calls a
gateway, refreshes a market, or falls back to the legacy posterior tables.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .cash_phase import (
    BasketCashPhasePolicyArtifact,
    BasketCashPhaseInput,
)
from .cash_phase_authority import (
    CashPhaseAuthorityManifest,
    load_cash_phase_authority_manifest,
    resolve_active_cash_phase_policy,
)
from .contracts import (
    POLICY_MATURITY_BINS,
    CalibrationActivation,
    ContractError,
    FORMAL_ENSEMBLE_METHOD_VERSION,
    MIN_FORMAL_ENSEMBLE_MODELS,
    MODEL_IDS,
    ModelCalibrationArtifact,
    ModelEnsembleArtifact,
    ModelEnsembleSelectionEvidence,
    ProbabilityEnvelope,
    content_sha256,
    model_set_hash,
    timestamp_lte,
)
from .execution_deadline import derive_target_execution_hard_deadline
from .market_registry import load_active_registry_rows
from .policy import (
    GlobalBasketPriorArtifact,
    PolicyArtifact,
    StationBasketPolicyActivation,
    resolve_station_policy,
)
from .production import compile_production_plan
from .consolidation_chain import load_chain_tilt
from .probability_bridge import load_current_probability_bridge
from .native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
    validate_cross_native_ensemble_weights,
)
from .regime_materialization import load_regime_artifact
from .ssot import PRODUCTION_TARGET_TABLES, production_ssot_schema_status
from .stages import select_current_stage
from .delta_v2_artifact import resolve_delta_v2_policy
from .delta_v2_fill_artifact import resolve_delta_v2_fill_distribution
from .basket_kernel.contracts import ContractError as DeltaV2ContractError
from .resource_bounds import (
    ResourceBoundError,
    assert_connection_target_capacity,
    assert_process_memory_budget,
    load_bounded_json,
)
from .canonical_activation_release import verify_released_p0_envelope_authority


STAGE_ZERO_EXECUTION_ANCHOR_SCHEMA = (
    "basket_target_stage_zero_execution_anchor_v1"
)


def _json(name: str, value: Any, *, mapping: bool = False) -> Any:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{name}_json_invalid") from exc
    if mapping and not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return value


def _row_mapping(row: sqlite3.Row | Sequence[Any], columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in columns}
    return {key: row[index] for index, key in enumerate(columns)}


def target_registry_source_lane(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> str:
    """Return the one formal source lane from the current target registry.

    This is a consumer-side authority gate.  It deliberately reads the
    newest complete target-local registry snapshot instead of trusting a P1
    envelope's self-described lineage.  Historical envelopes remain
    append-only evidence; a P1 whose lineage lane differs from this result is
    rejected by :func:`load_materialized_probability_envelopes`, never
    silently downgraded to P0/P0-M.
    """

    station = str(station_id or "").strip().upper()
    day = str(event_date or "").strip()
    if not station or not day:
        raise ContractError("target_registry_source_lane_scope_invalid")
    rows = load_active_registry_rows(
        conn,
        station_id=station,
        event_date=day,
    )
    lanes = {
        str(row.get("source_lane") or "").strip().lower()
        for row in rows
        if str(row.get("station_id") or "").strip().upper() == station
        and str(row.get("event_date") or "").strip() == day
    }
    if len(lanes) != 1:
        raise ContractError("target_registry_source_lane_not_unique")
    lane = next(iter(lanes))
    if lane not in {"amsc", "awc"}:
        raise ContractError("target_registry_source_lane_invalid")
    return lane


def _is_memory_connection(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return False
    return not str(row[2] or "")


def _strict_expected_source_lane(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> str | None:
    """Require registry authority on file-backed SSOT; retain memory fixtures."""

    try:
        return target_registry_source_lane(
            conn,
            station_id=station_id,
            event_date=event_date,
        )
    except ContractError:
        if _is_memory_connection(conn):
            return None
        raise


def _verify_record_sha(schema: str, raw: Mapping[str, Any]) -> None:
    supplied = str(raw.get("content_sha256") or "")
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    if supplied != content_sha256({"schema": schema, **identity}):
        raise ContractError(f"{schema}_content_sha_mismatch")


def _aware_utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _normalized_utc(name: str, value: Any) -> tuple[str, datetime]:
    parsed = _aware_utc(name, value)
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _required_sha256(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _promotion_state(conn: sqlite3.Connection, table: str, artifact_kind: str, artifact_id: str) -> bool:
    rows = conn.execute(
        f"SELECT decision FROM {table} WHERE artifact_kind=? AND artifact_id=? ORDER BY decision_seq DESC",
        (artifact_kind, artifact_id),
    ).fetchall()
    return bool(rows and str(rows[0][0]) == "PROMOTED")


@dataclass(frozen=True)
class ProductionArtifactBundle:
    """Immutable inputs resolved for one production decision timestamp."""

    calibration_artifacts: Mapping[str, ModelCalibrationArtifact]
    ensemble_artifacts: Mapping[str, ModelEnsembleArtifact]
    calibration_activations: tuple[CalibrationActivation, ...]
    global_priors: Mapping[str, GlobalBasketPriorArtifact]
    policy_artifacts: Mapping[str, PolicyArtifact]
    policy_activations: tuple[StationBasketPolicyActivation, ...]
    cash_phase_policies: tuple[BasketCashPhasePolicyArtifact, ...]
    ensemble_selection_evidence: Mapping[str, ModelEnsembleSelectionEvidence] = field(
        default_factory=dict
    )


def load_production_artifacts(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    evaluation_at_utc: str,
    require_calibration: bool = False,
    require_policy: bool = True,
) -> ProductionArtifactBundle:
    """Load and validate append-only artifacts for one station.

    ``require_calibration`` is false for the basket hot path because its
    contract consumes already materialized P0/P1.  The P0 materializer uses
    ``require_calibration=True, require_policy=False`` because policy is a
    downstream basket concern; neither flag permits an online fallback.
    """

    status = production_ssot_schema_status(conn)
    if not status["ok"]:
        missing = ",".join(str(item) for item in status["missing_tables"])
        raise ContractError(f"production_ssot_schema_incomplete:{missing}")
    station = str(station_id or "").strip()
    if not station:
        raise ContractError("production_station_id_missing")

    calibration_columns = (
        "calibration_artifact_id", "station_id", "model_id", "season_bin",
        "lead_bucket", "run_cycle", "settlement_definition",
        "trained_through_utc", "sample_n", "effective_sample_n", "bias_c",
        "residual_scale_c", "tail_parameters_json", "valid_from_utc",
        "valid_until_utc", "content_sha256",
    )
    calibration_artifacts: dict[str, ModelCalibrationArtifact] = {}
    for row in conn.execute(
        "SELECT " + ",".join(calibration_columns) +
        " FROM calibration_model_artifact WHERE station_id=?",
        (station,),
    ).fetchall():
        raw = _row_mapping(row, calibration_columns)
        raw["tail_parameters"] = _json("tail_parameters", raw.pop("tail_parameters_json"), mapping=True)
        raw["promoted"] = _promotion_state(
            conn, "calibration_artifact_promotion", "MODEL_CALIBRATION", raw["calibration_artifact_id"]
        )
        artifact = ModelCalibrationArtifact.from_mapping(raw)
        calibration_artifacts[artifact.calibration_artifact_id] = artifact

    selection_columns = (
        "selection_evidence_id","station_id","season_bin","lead_bucket",
        "run_cycle","settlement_definition","method_version",
        "train_start_date","train_end_date","recent_start_date",
        "recent_end_date","holdout_start_date","holdout_end_date",
        "candidate_scores_json","selected_models_json",
        "selected_weights_json","oos_metrics_json",
        "residual_covariance_json","residual_correlation_json",
        "effective_sample_n","previous_ensemble_artifact_sha256",
        "max_weight_delta","actual_max_weight_delta","content_sha256",
    )
    ensemble_selection_evidence: dict[str, ModelEnsembleSelectionEvidence] = {}
    for row in conn.execute(
        "SELECT " + ",".join(selection_columns) +
        " FROM calibration_ensemble_selection_evidence"
        " WHERE station_id=? AND method_version=?",
        (station, FORMAL_ENSEMBLE_METHOD_VERSION),
    ).fetchall():
        raw = _row_mapping(row, selection_columns)
        for source, target, mapping in (
            ("candidate_scores_json", "candidate_scores", False),
            ("selected_models_json", "selected_models", False),
            ("selected_weights_json", "selected_weights", True),
            ("oos_metrics_json", "oos_metrics", True),
            ("residual_covariance_json", "residual_covariance", True),
            ("residual_correlation_json", "residual_correlation", True),
        ):
            raw[target] = _json(target, raw.pop(source), mapping=mapping)
        evidence = ModelEnsembleSelectionEvidence.from_mapping(raw)
        ensemble_selection_evidence[evidence.content_sha256] = evidence

    ensemble_columns = (
        "ensemble_artifact_id", "station_id", "season_bin", "lead_bucket",
        "run_cycle", "settlement_definition", "candidate_models_json",
        "supported_masks_json", "residual_covariance_json",
        "correlation_penalty", "selection_evidence_sha256",
        "previous_ensemble_artifact_sha256", "weight_method",
        "effective_sample_n", "max_weight_delta", "train_start_date",
        "train_end_date", "recent_start_date", "recent_end_date",
        "holdout_start_date", "holdout_end_date", "oos_metrics_json",
        "valid_from_utc", "valid_until_utc", "content_sha256",
    )
    ensemble_artifacts: dict[str, ModelEnsembleArtifact] = {}
    for row in conn.execute(
        "SELECT " + ",".join(ensemble_columns) +
        " FROM calibration_ensemble_artifact"
        " WHERE station_id=? AND weight_method=?",
        (station, FORMAL_ENSEMBLE_METHOD_VERSION),
    ).fetchall():
        raw = _row_mapping(row, ensemble_columns)
        raw["candidate_models"] = _json("candidate_models", raw.pop("candidate_models_json"))
        raw["supported_masks"] = _json("supported_masks", raw.pop("supported_masks_json"), mapping=True)
        raw["residual_covariance"] = _json("residual_covariance", raw.pop("residual_covariance_json"), mapping=True)
        raw["oos_metrics"] = (
            _json("oos_metrics", raw.pop("oos_metrics_json"), mapping=True)
            if raw.get("oos_metrics_json") is not None else {}
        )
        raw["promoted"] = _promotion_state(
            conn, "calibration_artifact_promotion", "MODEL_ENSEMBLE", raw["ensemble_artifact_id"]
        )
        artifact = ModelEnsembleArtifact.from_mapping(raw)
        # Do not validate every historical row as though it were the active
        # authority.  Older databases may contain a pre-contract short-window
        # artifact that is not selected by the current generation.  The exact
        # activation resolver validates the one artifact it selects, so an
        # unrelated historical row cannot poison a newer compatible run while
        # an actually selected invalid artifact still fails closed.
        if artifact.governance_complete:
            evidence = ensemble_selection_evidence.get(str(artifact.selection_evidence_sha256))
            if evidence is None or evidence.selected_models != artifact.candidate_models:
                raise ContractError("ensemble_selection_evidence_binding_invalid")
        ensemble_artifacts[artifact.ensemble_artifact_id] = artifact

    activation_columns = (
        "activation_id", "artifact_kind", "artifact_id", "station_id",
        "model_id_or_star", "season_bin", "lead_bucket", "run_cycle",
        "settlement_definition", "available_model_set_hash_or_star",
        "generation", "valid_from_utc", "valid_until_utc", "activated_at_utc",
        "authority_scope", "content_sha256",
    )
    calibration_activations = tuple(
        CalibrationActivation.from_mapping(_row_mapping(row, activation_columns))
        for row in conn.execute(
            "SELECT " + ",".join(activation_columns) +
            " FROM calibration_activation WHERE station_id=?",
            (station,),
        ).fetchall()
    )

    prior_columns = (
        "global_prior_id", "strategy_version", "family_parameters_json",
        "default_policy_json", "valid_from_utc", "valid_until_utc",
        "content_sha256",
    )
    global_priors: dict[str, GlobalBasketPriorArtifact] = {}
    for row in conn.execute("SELECT " + ",".join(prior_columns) + " FROM basket_global_prior_artifact").fetchall():
        raw = _row_mapping(row, prior_columns)
        raw["family_parameters"] = _json("family_parameters", raw.pop("family_parameters_json"), mapping=True)
        raw["default_policy"] = _json("default_policy", raw.pop("default_policy_json"), mapping=True)
        raw["promoted"] = _promotion_state(
            conn, "basket_policy_promotion", "GLOBAL_PRIOR", raw["global_prior_id"]
        )
        artifact = GlobalBasketPriorArtifact.from_mapping(raw)
        global_priors[artifact.global_prior_id] = artifact

    policy_columns = (
        "artifact_id", "family", "station_id", "stage", "season_bin",
        "maturity_bin", "liquidity_bin", "strategy_version", "theta_local_json",
        "effective_sample_n", "kappa", "trained_through_utc", "valid_from_utc",
        "valid_until_utc", "content_sha256",
    )
    policy_artifacts: dict[str, PolicyArtifact] = {}
    for row in conn.execute(
        "SELECT " + ",".join(policy_columns) +
        " FROM basket_station_policy_artifact WHERE station_id=?",
        (station,),
    ).fetchall():
        raw = _row_mapping(row, policy_columns)
        raw["theta_local"] = _json("theta_local", raw.pop("theta_local_json"), mapping=True)
        raw["promoted"] = _promotion_state(
            conn, "basket_policy_promotion", "STATION_POLICY", raw["artifact_id"]
        )
        artifact = PolicyArtifact.from_mapping(raw)
        policy_artifacts[artifact.artifact_id] = artifact

    policy_activation_columns = (
        "activation_id", "station_id", "stage", "season_bin", "maturity_bin",
        "liquidity_bin", "strategy_version", "global_prior_id",
        "target_artifact_id", "reserve_artifact_id", "cadence_artifact_id",
        "fallback_families_json", "generation", "valid_from_utc",
        "valid_until_utc", "activated_at_utc", "content_sha256",
    )
    policy_activations = tuple(
        StationBasketPolicyActivation.from_mapping({
            **_row_mapping(row, policy_activation_columns),
            "fallback_families": _json("fallback_families", row[11]),
            "promoted": _promotion_state(
                conn, "basket_policy_promotion", "STATION_ACTIVATION", row[0]
            ),
        })
        for row in conn.execute(
            "SELECT " + ",".join(policy_activation_columns) +
            " FROM basket_policy_activation WHERE station_id=?",
            (station,),
        ).fetchall()
    )

    cash_columns = (
        "policy_version", "station_id", "station_timezone",
        "heavy_to_standard_local_time", "standard_to_tail_local_time",
        "cash_by_phase_json", "valid_from_utc", "valid_until_utc",
        "evidence_status", "promoted", "content_sha256",
    )
    cash_policies = tuple(
        BasketCashPhasePolicyArtifact.from_mapping({
            **_row_mapping(row, cash_columns),
            "cash_by_phase": _json("cash_by_phase", row[5], mapping=True),
            "promoted": bool(row[9]),
        })
        for row in conn.execute(
            "SELECT " + ",".join(cash_columns) +
            " FROM basket_cash_phase_policy_artifact WHERE station_id=?",
            (station,),
        ).fetchall()
    )
    if require_calibration and (not calibration_artifacts or not ensemble_artifacts or not calibration_activations):
        raise ContractError("production_calibration_artifacts_missing")
    if require_policy and (not global_priors or not policy_activations or not cash_policies):
        raise ContractError("production_basket_policy_artifacts_missing")
    return ProductionArtifactBundle(
        calibration_artifacts=calibration_artifacts,
        ensemble_selection_evidence=ensemble_selection_evidence,
        ensemble_artifacts=ensemble_artifacts,
        calibration_activations=calibration_activations,
        global_priors=global_priors,
        policy_artifacts=policy_artifacts,
        policy_activations=policy_activations,
        cash_phase_policies=cash_policies,
    )


def load_materialized_probability_envelopes(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    evaluation_at_utc: str,
    required_probability_pathway: str | None = P0_PROBABILITY_PATHWAY,
    expected_source_lane: str | None = None,
    canonical_current_pointer: Path | str | None = None,
    canonical_release_root: Path | str | None = None,
    include_future_decisions: bool = False,
    preloaded_validated: Sequence[ProbabilityEnvelope] | None = None,
    validated_out: list[ProbabilityEnvelope] | None = None,
    stages: Sequence[str] | None = None,
) -> list[ProbabilityEnvelope]:
    """Read and validate every committed checkpoint before as-of routing.

    The SQL query deliberately does not pre-filter on ``decision_at_utc``.
    A malformed committed P0-M/P1 timestamp or payload must be observed and
    fail closed; it must not disappear behind SQLite's lexical timestamp
    comparison and expose a lower-stage P0 fallback.  Valid future
    checkpoints are validated but remain invisible until their decision
    instant.

    ``include_future_decisions`` lifts only that visibility rule, for the
    one caller class whose output is a pure function of the committed row
    rather than an as-of routing decision: companion materializers (regime)
    that must cover every envelope any later evaluation could see.  Decision
    stamps carry deterministic +1s/+1us bumps past the wall clock, so a
    companion pass anchored to a cycle-start clock structurally trails the
    envelopes its own cycle just committed.
    """

    status = production_ssot_schema_status(conn)
    if not status["ok"]:
        raise ContractError("production_probability_ssot_table_missing")
    columns = (
        "envelope_id", "stage", "checkpoint_id", "station_id", "event_date",
        "settlement_timezone", "decision_at_utc", "fresh_until_utc", "bucket_ordinals_json",
        "probabilities_json", "posterior_version", "posterior_sha256",
        "bridge_sha256", "lineage_json", "content_sha256",
    )
    if (canonical_current_pointer is None) != (canonical_release_root is None):
        raise ContractError("probability_release_authority_paths_incomplete")
    current_pointer = None
    if canonical_current_pointer is not None:
        current_pointer = load_bounded_json(canonical_current_pointer)
        if not isinstance(current_pointer, Mapping):
            raise ContractError("probability_release_current_pointer_invalid")
    # The lane and pathway gates below are pure functions of the validated
    # list, applied in Python after every row is read and parsed.  A caller
    # that needs the same station-day under two gate settings was therefore
    # paying for the read twice -- and the read is the whole station-day
    # including lineage_json, measured at 24 to 27 MB and 375 to 647ms on
    # the largest days.  ``preloaded_validated`` lets such a caller read
    # once and gate twice, against one snapshot rather than two.
    # Restricting the stages is a restriction on which rows are *built*, not
    # on which are trusted: 80% of a load is JSON-decoding each row, hashing
    # it back to check the stored sha, and constructing the envelope, all per
    # row.  P1 is 78% of the rows on a busy station-day, so a caller that
    # only needs P0 and P0-M pays a fifth of the price.  It is the caller's
    # job to know that -- the gates below still see everything they are
    # given, and a caller that narrows past what its own decision needs gets
    # a wrong answer, not a cheap one.
    stage_filter = tuple(str(stage).strip().upper() for stage in stages or ())
    if stages is not None and not stage_filter:
        raise ContractError("probability_envelope_stage_filter_empty")
    if any(stage not in {"P0", "P0-M", "P1"} for stage in stage_filter):
        raise ContractError("probability_envelope_stage_filter_invalid")
    if preloaded_validated is not None:
        validated = list(preloaded_validated)
        validated_snapshot = validated
    else:
        validated: list[ProbabilityEnvelope] = []
        stage_clause = (
            " AND stage IN (" + ",".join("?" * len(stage_filter)) + ")"
            if stage_filter
            else ""
        )
        for row in conn.execute(
            "SELECT " + ",".join(columns) +
            " FROM basket_probability_envelope"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            + stage_clause +
            " ORDER BY created_at,envelope_id",
            (station_id, event_date, *stage_filter),
        ).fetchall():
            raw = _row_mapping(row, columns)
            envelope_id = str(raw.pop("envelope_id") or "")
            supplied_sha = str(raw.pop("content_sha256") or "")
            raw["bucket_ordinals"] = _json("bucket_ordinals", raw.pop("bucket_ordinals_json"), mapping=True)
            raw["probabilities"] = _json("probabilities", raw.pop("probabilities_json"), mapping=True)
            raw["lineage"] = _json("lineage", raw.pop("lineage_json"), mapping=True)
            lineage_timezone = str(raw["lineage"].get("settlement_timezone") or "").strip()
            storage_timezone = str(raw.get("settlement_timezone") or "").strip()
            if not lineage_timezone or lineage_timezone != storage_timezone:
                raise ContractError("probability_settlement_timezone_lineage_mismatch")
            raw["settlement_timezone"] = storage_timezone
            envelope = ProbabilityEnvelope.from_mapping(raw)
            if envelope_id != envelope.checkpoint_id:
                raise ContractError("probability_envelope_storage_identity_mismatch")
            if supplied_sha != envelope.content_sha256:
                raise ContractError("probability_envelope_content_sha_mismatch")
            if envelope.lineage.get("release_state") == "MATERIALIZED_UNPUBLISHED":
                if current_pointer is None or canonical_release_root is None:
                    continue
                verify_released_p0_envelope_authority(
                    current_pointer,
                    expected_release_root=canonical_release_root,
                    envelope=envelope.to_dict(),
                )
            if include_future_decisions or timestamp_lte(
                envelope.decision_at_utc, evaluation_at_utc
            ):
                validated.append(envelope)
        validated_snapshot = validated
    # The ungated list, for a caller that will gate the same snapshot again.
    # Handing back the *return* value instead would mean the second gating ran
    # on an already-pathway-filtered list, which happens to be idempotent
    # today and is not a property worth depending on.
    if validated_out is not None:
        validated_out.extend(validated_snapshot)
    expected_lane = (
        str(expected_source_lane or "").strip().lower()
        if expected_source_lane is not None
        else ""
    )
    if expected_source_lane is not None and expected_lane not in {"amsc", "awc"}:
        raise ContractError("probability_expected_source_lane_invalid")
    if expected_lane:
        # P1 is final authority.  A wrong-lane P1 must therefore block the
        # scope instead of being filtered out and allowing a P0/P0-M
        # fallback.  Inspect only the newest visible P1 generation: old
        # append-only AWC history remains auditable after a newer compliant
        # AMSC P1 has atomically superseded it.  Checking every historical
        # P1 would make that safe repair permanently impossible.
        visible_p1 = [
            envelope for envelope in validated if envelope.stage == "P1"
        ]
        if visible_p1:
            newest_at = max(
                _aware_utc(
                    "probability_p1_source_lane_decision_at",
                    envelope.decision_at_utc,
                )
                for envelope in visible_p1
            )
            newest_p1 = [
                envelope
                for envelope in visible_p1
                if _aware_utc(
                    "probability_p1_source_lane_decision_at",
                    envelope.decision_at_utc,
                )
                == newest_at
            ]
            # An AWC scope's newest P1 may legally be closed by an
            # OOS-governed ASOS substitute for one of the station's own
            # frozen METAR slots.  The substitute lanes are the same sensor
            # on the same slots; a retired AMSC lane still blocks the scope
            # exactly as before.
            allowed_p1_lanes = (
                {expected_lane, "asos_1min", "madis_hfmetar"}
                if expected_lane == "awc"
                else {expected_lane}
            )
            if any(
                str(envelope.lineage.get("source_lane") or "")
                .strip()
                .lower()
                not in allowed_p1_lanes
                for envelope in newest_p1
            ):
                raise ContractError("probability_p1_source_lane_mismatch")
            # P1 is the METAR/SPECI checkpoint for the immutable prediction
            # branch.  When an exact TAF/P0-M checkpoint exists it is the
            # preferred root; otherwise a valid P0-root P1 is the legal
            # no-TAF path.  Inspect only the newest visible P1 generation so
            # append-only history remains auditable while a newer branch can
            # supersede it without deletion.
            if any(
                str(envelope.lineage.get("prediction_root_stage") or "")
                .strip()
                .upper()
                not in {"P0", "P0-M"}
                for envelope in newest_p1
            ):
                raise ContractError("probability_p1_root_stage_mismatch")
    if required_probability_pathway is None:
        envelopes = validated
    else:
        native_roots = {
            envelope.checkpoint_id
            for envelope in validated
            if envelope.stage == "P0"
            and str(
                envelope.lineage.get("p0_probability_pathway")
                or envelope.lineage.get("probability_pathway")
                or ""
            )
            == required_probability_pathway
        }
        envelopes = [
            envelope
            for envelope in validated
            if (
                envelope.checkpoint_id in native_roots
                or (
                    envelope.stage in {"P0-M", "P1"}
                    and str(
                        envelope.lineage.get("p0_root_checkpoint_id") or ""
                    )
                    in native_roots
                    and str(
                        envelope.lineage.get("p0_probability_pathway")
                        or envelope.lineage.get("probability_pathway")
                        or required_probability_pathway
                    )
                    == required_probability_pathway
                )
            )
        ]
    if not envelopes:
        raise ContractError("materialized_probability_envelope_missing")
    return envelopes


def assert_current_probability_bridge(
    conn: sqlite3.Connection,
    *,
    probability: ProbabilityEnvelope,
    evaluation_at_utc: str,
) -> dict[str, Any]:
    """Require the selected basket probability to equal materialized p(B)."""

    bridge = load_current_probability_bridge(
        conn,
        station_id=probability.station_id,
        event_date=probability.event_date,
        as_of_utc=evaluation_at_utc,
    )
    if bridge["checkpoint_id"] != probability.checkpoint_id:
        raise ContractError(
            "materialized_probability_bridge_checkpoint_mismatch"
        )
    probability_rows = bridge["probability_by_bucket"]
    ordered_buckets = [
        bucket
        for bucket, _ordinal in sorted(
            probability.bucket_ordinals.items(),
            key=lambda item: (item[1], item[0]),
        )
    ]
    if (
        [str(row["bucket"]) for row in probability_rows]
        != ordered_buckets
        or any(
            abs(
                float(row["probability"])
                - float(
                    probability.probabilities[str(row["bucket"])]
                )
            )
            > 1e-12
            for row in probability_rows
        )
    ):
        raise ContractError(
            "materialized_probability_bridge_projection_mismatch"
        )
    return bridge


def assert_formal_probability_model_authority(
    envelopes: Sequence[ProbabilityEnvelope],
    *,
    probability: ProbabilityEnvelope,
) -> ProbabilityEnvelope:
    """Validate the immutable formal NWP authority behind a selected stage.

    P0 owns the model combination. P0-M and P1 may carry only a compact root
    reference, so consumers resolve that reference against the exact committed
    P0 loaded for the same station-day and validate the root instead of
    accepting copied or partial downstream lineage.
    """

    roots = {
        item.checkpoint_id: item
        for item in envelopes
        if item.stage == "P0"
    }
    if probability.stage == "P0":
        root = probability
    else:
        root_id = str(
            probability.lineage.get("p0_root_checkpoint_id") or ""
        ).strip()
        if not root_id:
            raise ContractError("probability_p0_root_checkpoint_id_missing")
        root = roots.get(root_id)
        if root is None:
            raise ContractError("probability_p0_root_checkpoint_missing")
        supplied_root_sha = str(
            probability.lineage.get("p0_root_content_sha256") or ""
        ).strip().lower()
        if supplied_root_sha and supplied_root_sha != root.content_sha256:
            raise ContractError("probability_p0_root_content_sha_mismatch")

    if (
        root.station_id != probability.station_id
        or root.event_date != probability.event_date
        or root.settlement_timezone != probability.settlement_timezone
    ):
        raise ContractError("probability_p0_root_scope_mismatch")

    lineage = root.lineage
    if lineage.get("probability_pathway") == P0_PROBABILITY_PATHWAY:
        required_native = (
            "native_cross_ensemble_artifact_sha256",
            "native_selection_evidence_sha256",
            "native_cross_activation_sha256",
            "native_probability_evidence_sha256",
            "ensemble_system_ids_json",
            "independence_family_ids_json",
            "system_family_ids_json",
            "system_weights_json",
            "family_weight_caps_json",
            "system_artifact_shas_json",
            "system_activation_shas_json",
            "taf_nwp_live_compatibility_fingerprint_sha256",
            "taf_nwp_live_compatibility_json",
        )
        if any(
            not str(lineage.get(key) or "").strip()
            for key in required_native
        ):
            raise ContractError(
                "native_probability_model_authority_incomplete"
            )
        try:
            system_ids = json.loads(
                str(lineage["ensemble_system_ids_json"])
            )
            family_ids = json.loads(
                str(lineage["independence_family_ids_json"])
            )
            system_families = json.loads(
                str(lineage["system_family_ids_json"])
            )
            system_weights = json.loads(
                str(lineage["system_weights_json"])
            )
            family_caps = json.loads(
                str(lineage["family_weight_caps_json"])
            )
            system_artifacts = json.loads(
                str(lineage["system_artifact_shas_json"])
            )
            system_activations = json.loads(
                str(lineage["system_activation_shas_json"])
            )
            fingerprint = json.loads(
                str(lineage["taf_nwp_live_compatibility_json"])
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ContractError(
                "native_probability_model_authority_json_invalid"
            ) from exc
        if (
            not isinstance(system_ids, list)
            or len(system_ids) < 2
            or len(set(map(str, system_ids))) != len(system_ids)
            or not isinstance(family_ids, list)
            or len(set(map(str, family_ids))) < 2
            or not isinstance(system_families, Mapping)
            or not isinstance(system_weights, Mapping)
            or not isinstance(family_caps, Mapping)
            or not isinstance(system_artifacts, Mapping)
            or not isinstance(system_activations, Mapping)
            or not isinstance(fingerprint, Mapping)
            or set(map(str, system_ids)) != set(map(str, system_families))
            or set(map(str, system_ids)) != set(map(str, system_weights))
            or set(map(str, system_ids)) != set(map(str, system_artifacts))
            or set(map(str, system_ids)) != set(map(str, system_activations))
        ):
            raise ContractError(
                "native_probability_model_authority_shape_invalid"
            )
        receipt = validate_cross_native_ensemble_weights(
            system_weights={
                str(key): float(value)
                for key, value in system_weights.items()
            },
            systems_by_id={
                str(system_id): {
                    "independence_family_id": str(
                        system_families[str(system_id)]
                    ),
                }
                for system_id in system_ids
            },
            family_weight_caps={
                str(key): float(value)
                for key, value in family_caps.items()
            },
        )
        if set(receipt["family_weights"]) != set(map(str, family_ids)):
            raise ContractError(
                "native_probability_family_scope_mismatch"
            )
        fingerprint_sha = str(
            lineage[
                "taf_nwp_live_compatibility_fingerprint_sha256"
            ]
        ).lower()
        fingerprint_identity = {
            str(key): value
            for key, value in fingerprint.items()
            if key != "fingerprint_sha256"
        }
        if (
            fingerprint.get("schema")
            != TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA
            or fingerprint.get("probability_pathway")
            != P0_PROBABILITY_PATHWAY
            or str(fingerprint.get("fingerprint_sha256") or "").lower()
            != fingerprint_sha
            or content_sha256(fingerprint_identity) != fingerprint_sha
            or fingerprint.get("ensemble_system_ids") != system_ids
            or fingerprint.get("independence_family_ids") != family_ids
            or fingerprint.get("system_family_ids") != system_families
            or fingerprint.get("resolved_system_weights")
            != system_weights
            or fingerprint.get("family_weight_caps") != family_caps
            or str(
                fingerprint.get("cross_ensemble_artifact_sha256")
                or ""
            ) != str(
                lineage["native_cross_ensemble_artifact_sha256"]
            )
            or str(
                fingerprint.get("cross_selection_evidence_sha256")
                or ""
            ) != str(lineage["native_selection_evidence_sha256"])
            or str(fingerprint.get("cross_activation_sha256") or "")
            != str(lineage["native_cross_activation_sha256"])
        ):
            raise ContractError(
                "native_probability_compatibility_binding_mismatch"
            )
        calibration_bindings = fingerprint.get(
            "system_calibration_artifacts"
        )
        if not isinstance(calibration_bindings, Mapping):
            raise ContractError(
                "native_probability_calibration_binding_invalid"
            )
        for system_id in system_ids:
            binding = calibration_bindings.get(str(system_id))
            if (
                not isinstance(binding, Mapping)
                or binding.get("artifact_content_sha256")
                != system_artifacts[str(system_id)]
                or binding.get("activation_content_sha256")
                != system_activations[str(system_id)]
            ):
                raise ContractError(
                    "native_probability_calibration_binding_mismatch"
                )
        for value in (
            lineage["native_cross_ensemble_artifact_sha256"],
            lineage["native_selection_evidence_sha256"],
            lineage["native_cross_activation_sha256"],
            lineage["native_probability_evidence_sha256"],
            fingerprint_sha,
            *system_artifacts.values(),
            *system_activations.values(),
        ):
            normalized = str(value).lower()
            if (
                len(normalized) != 64
                or any(char not in "0123456789abcdef" for char in normalized)
            ):
                raise ContractError(
                    "native_probability_authority_sha_invalid"
                )
        return root

    required = (
        "model_run_ids",
        "calibration_artifact_ids",
        "activation_ids",
        "ensemble_artifact_id",
        "available_model_set_hash",
        "selection_evidence_sha256",
        "ensemble_artifact_content_sha256",
        "taf_nwp_live_compatibility_fingerprint_sha256",
        "taf_nwp_live_compatibility_json",
    )
    if any(not str(lineage.get(key) or "").strip() for key in required):
        raise ContractError("formal_probability_model_authority_incomplete")

    try:
        fingerprint = json.loads(
            str(lineage["taf_nwp_live_compatibility_json"])
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(
            "formal_probability_compatibility_json_invalid"
        ) from exc
    if not isinstance(fingerprint, Mapping):
        raise ContractError("formal_probability_compatibility_json_invalid")
    if (
        fingerprint.get("schema")
        != "bayes_basket_taf_nwp_live_compatibility_fingerprint_v1"
    ):
        raise ContractError("formal_probability_compatibility_schema_invalid")

    supplied_fingerprint_sha = str(
        fingerprint.get("fingerprint_sha256") or ""
    ).strip().lower()
    lineage_fingerprint_sha = str(
        lineage["taf_nwp_live_compatibility_fingerprint_sha256"]
    ).strip().lower()
    fingerprint_identity = dict(fingerprint)
    fingerprint_identity.pop("fingerprint_sha256", None)
    if (
        not supplied_fingerprint_sha
        or supplied_fingerprint_sha != lineage_fingerprint_sha
        or supplied_fingerprint_sha != content_sha256(fingerprint_identity)
    ):
        raise ContractError("formal_probability_compatibility_sha_mismatch")

    models_raw = fingerprint.get("available_models")
    weights_raw = fingerprint.get("resolved_weights")
    calibrations_raw = fingerprint.get("model_calibration_artifacts")
    if (
        not isinstance(models_raw, list)
        or not isinstance(weights_raw, Mapping)
        or not isinstance(calibrations_raw, Mapping)
    ):
        raise ContractError("formal_probability_model_combination_invalid")
    models = tuple(str(item).strip().upper() for item in models_raw)
    if (
        len(models) < MIN_FORMAL_ENSEMBLE_MODELS
        or len(set(models)) != len(models)
        or any(model not in MODEL_IDS for model in models)
        or set(weights_raw) != set(models)
        or set(calibrations_raw) != set(models)
    ):
        raise ContractError("formal_probability_model_combination_invalid")

    weights: dict[str, float] = {}
    for model in models:
        value = weights_raw[model]
        if isinstance(value, bool):
            raise ContractError("formal_probability_weight_invalid")
        try:
            weight = float(value)
        except (TypeError, ValueError) as exc:
            raise ContractError("formal_probability_weight_invalid") from exc
        if not math.isfinite(weight) or weight < 0.0:
            raise ContractError("formal_probability_weight_invalid")
        weights[model] = weight
    if (
        sum(weight > 0.0 for weight in weights.values())
        < MIN_FORMAL_ENSEMBLE_MODELS
    ):
        raise ContractError("formal_probability_positive_weight_models_too_few")
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ContractError("formal_probability_weights_not_normalized")

    try:
        formal_members = json.loads(
            str(lineage.get("formal_model_members_json") or "")
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(
            "formal_probability_model_members_json_invalid"
        ) from exc
    if (
        not isinstance(formal_members, list)
        or len(formal_members) < MIN_FORMAL_ENSEMBLE_MODELS
    ):
        raise ContractError("formal_probability_model_members_incomplete")
    member_by_model: dict[str, Mapping[str, Any]] = {}
    for raw_member in formal_members:
        if not isinstance(raw_member, Mapping):
            raise ContractError("formal_probability_model_member_invalid")
        model = str(raw_member.get("model_id") or "").strip().upper()
        center_bucket = str(
            raw_member.get("calibrated_center_bucket") or ""
        ).strip()
        try:
            member_weight = float(raw_member.get("weight"))
            center_c = float(raw_member.get("calibrated_center_c"))
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "formal_probability_model_member_invalid"
            ) from exc
        if (
            not model
            or model in member_by_model
            or center_bucket not in root.bucket_ordinals
            or not math.isfinite(member_weight)
            or not math.isfinite(center_c)
            or member_weight <= 0.0
        ):
            raise ContractError("formal_probability_model_member_invalid")
        member_by_model[model] = raw_member
        if abs(member_weight - weights.get(model, -1.0)) > 1e-12:
            raise ContractError(
                "formal_probability_model_member_weight_mismatch"
            )
    if set(member_by_model) != set(models):
        raise ContractError("formal_probability_model_members_scope_mismatch")
    anchor_ordinals = [
        int(root.bucket_ordinals[
            str(member_by_model[model]["calibrated_center_bucket"])
        ])
        for model in models
    ]
    try:
        lineage_anchor_lower = int(
            str(lineage["formal_model_anchor_lower_ordinal"])
        )
        lineage_anchor_upper = int(
            str(lineage["formal_model_anchor_upper_ordinal"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(
            "formal_probability_model_anchor_lineage_invalid"
        ) from exc
    if (
        lineage_anchor_lower != min(anchor_ordinals)
        or lineage_anchor_upper != max(anchor_ordinals)
    ):
        raise ContractError("formal_probability_model_anchor_lineage_mismatch")

    expected_model_hash = model_set_hash(models)
    if (
        str(lineage["available_model_set_hash"]).strip().lower()
        != expected_model_hash
        or str(fingerprint.get("available_model_set_hash") or "")
        .strip()
        .lower()
        != expected_model_hash
    ):
        raise ContractError("formal_probability_model_set_hash_mismatch")
    if (
        str(fingerprint.get("ensemble_weight_method") or "").strip()
        != FORMAL_ENSEMBLE_METHOD_VERSION
    ):
        raise ContractError("formal_probability_weight_method_invalid")

    bindings = (
        (
            "ensemble_artifact_id",
            "ensemble_artifact_id",
            "formal_probability_ensemble_artifact_id_mismatch",
        ),
        (
            "ensemble_artifact_content_sha256",
            "ensemble_artifact_content_sha256",
            "formal_probability_ensemble_artifact_sha_mismatch",
        ),
        (
            "selection_evidence_sha256",
            "ensemble_selection_evidence_sha256",
            "formal_probability_selection_evidence_sha_mismatch",
        ),
    )
    for lineage_key, fingerprint_key, reason in bindings:
        if str(lineage[lineage_key]).strip() != str(
            fingerprint.get(fingerprint_key) or ""
        ).strip():
            raise ContractError(reason)

    calibration_ids: set[str] = set()
    for model in models:
        calibration = calibrations_raw[model]
        if not isinstance(calibration, Mapping):
            raise ContractError("formal_probability_calibration_binding_invalid")
        artifact_id = str(
            calibration.get("calibration_artifact_id") or ""
        ).strip()
        artifact_sha = str(
            calibration.get("calibration_artifact_content_sha256") or ""
        ).strip().lower()
        if not artifact_id or len(artifact_sha) != 64:
            raise ContractError("formal_probability_calibration_binding_invalid")
        calibration_ids.add(artifact_id)
    lineage_calibration_ids = {
        item.strip()
        for item in str(lineage["calibration_artifact_ids"]).split(",")
        if item.strip()
    }
    if lineage_calibration_ids != calibration_ids:
        raise ContractError("formal_probability_calibration_binding_mismatch")

    model_run_ids = {
        item.strip()
        for item in str(lineage["model_run_ids"]).split(",")
        if item.strip()
    }
    activation_ids = {
        item.strip()
        for item in str(lineage["activation_ids"]).split(",")
        if item.strip()
    }
    if len(model_run_ids) != len(models):
        raise ContractError("formal_probability_model_run_binding_invalid")
    if len(activation_ids) < len(models) + 1:
        raise ContractError("formal_probability_activation_binding_invalid")
    return root


def target_atomic_facts_from_cycle(compiled_cycle: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the M5 fact identities from the selected P1 lineage only.

    The basket lane is not allowed to reopen METAR/SPECI rows or the hard-NO
    bounds table.  P1 already carries the canonical event and bound identity,
    raw hash, source lane and commit timestamp as immutable lineage.  This
    adapter consumes exactly those identifiers; any P0-only or incomplete
    lineage fails closed instead of reconstructing weather facts online.
    """

    envelope = compiled_cycle.get("probability_envelope")
    if not isinstance(envelope, Mapping) or str(envelope.get("stage") or "") != "P1":
        raise ContractError("target_atomic_facts_require_p1")
    station_id = str(envelope.get("station_id") or "").strip()
    event_date = str(envelope.get("event_date") or "").strip()
    lineage = envelope.get("lineage")
    if not station_id or not event_date or not isinstance(lineage, Mapping):
        raise ContractError("target_atomic_lineage_missing")
    required = (
        "canonical_observation_event_id",
        "truth_observation_revision_content_sha256",
        # ``canonical_observation_committed_at``, not ``..._utc``.  Nothing has
        # ever written the ``_utc`` spelling -- materialization publishes the
        # bare name, and the sibling check below already uses it -- so this
        # presence test could only ever fail, which is what left every resident
        # P1 unable to become an atomic fact.
        "canonical_observation_committed_at",
        "settlement_timezone",
        "hard_bound_id",
        "source_lane",
        "report_type",
    )
    if any(not str(lineage.get(key) or "").strip() for key in required):
        raise ContractError("target_atomic_lineage_incomplete")
    source_lane = str(lineage["source_lane"]).strip().upper()
    report_type = str(lineage["report_type"]).strip().upper()
    # AWC reports root on PR12 truth revisions; scheduled ASOS substitutes
    # root on their parallel identity facts.  The lane/report pairing is
    # exact -- a lane may not borrow another lane's report type -- and
    # anything outside the formal trio (AMSC, garbage) stays refused.
    formal_report_types = {
        "AWC": {"METAR", "SPECI"},
        "MADIS_HFMETAR": {"ASOS_HF"},
        "ASOS_1MIN": {"ASOS_1MIN"},
    }
    if report_type not in formal_report_types.get(source_lane, ()):
        raise ContractError("target_atomic_lineage_source_invalid")
    settlement_timezone = str(lineage["settlement_timezone"]).strip()
    if settlement_timezone != str(envelope.get("settlement_timezone") or "").strip():
        raise ContractError("target_atomic_lineage_settlement_timezone_mismatch")
    # A contract is accepted when a caller carries one, but is no longer
    # required: the observation-identity lane stages the revision facts on its
    # own timer, so the chain can name the one it roots on and let the ledger
    # verify the store holds it.  Requiring the document meant a live P1
    # candidate had to re-state a fact that already existed, carrying its 30 KB
    # authority projection along with it -- and nothing in the compile path
    # ever built one, which is why P1 stopped reaching a commit.
    awc_contract = compiled_cycle.get("awc_observation_contract")
    if awc_contract is not None and not isinstance(awc_contract, Mapping):
        raise ContractError("target_atomic_awc_observation_contract_missing")
    bound = {
        "bound_id": str(lineage["hard_bound_id"]).strip(),
        "station_id": station_id,
        "event_date": event_date,
        "bound_generation": int(lineage.get("bound_generation", 0) or 0),
        "hard_lower_bound": None,
        "bound_json": {
            "schema": "basket_target_bound_lineage_v1",
            "hard_bound_id": str(lineage["hard_bound_id"]).strip(),
            "hard_excluded_buckets": str(lineage.get("hard_excluded_buckets") or ""),
            "probability_envelope_sha256": str(envelope.get("content_sha256") or ""),
        },
    }
    return {
        "awc_observation_contract": (
            dict(awc_contract) if isinstance(awc_contract, Mapping) else None
        ),
        "truth_observation_revision_content_sha256": str(
            lineage["truth_observation_revision_content_sha256"]
        ).strip().lower(),
        "bound": bound,
    }


def target_stage_zero_execution_anchor_from_cycle(
    compiled_cycle: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the no-execution anchor from one complete SSOT compile.

    This is deliberately *not* a weakened variant of
    :func:`target_atomic_facts_from_cycle`.  It records the complete
    probability/market/economic input set as a content-addressed blocker
    instead of materializing observation, bound, intent, or reservation
    facts.  The ledger re-reads all of the referenced objects inside its own
    write transaction before it writes the zero-execution decision.
    """

    if not isinstance(compiled_cycle, Mapping):
        raise ContractError("stage_zero_execution_compiled_cycle_mapping_required")
    if "stage_zero_execution_anchor" in compiled_cycle:
        # An anchor is an output of this adapter.  Allowing it back into the
        # input would make the required full-plan identity recursive.
        raise ContractError("stage_zero_execution_anchor_recursive_input")

    raw_envelope = compiled_cycle.get("probability_envelope")
    if not isinstance(raw_envelope, Mapping):
        raise ContractError("stage_zero_execution_probability_envelope_missing")
    envelope_payload = dict(raw_envelope)
    supplied_envelope_sha = envelope_payload.pop("content_sha256", None)
    supplied_envelope_id = envelope_payload.pop("envelope_id", None)
    probability = ProbabilityEnvelope.from_mapping(envelope_payload)
    probability_sha = probability.content_sha256
    if supplied_envelope_sha not in (None, "") and _required_sha256(
        "stage_zero_execution_probability_envelope",
        supplied_envelope_sha,
    ) != probability_sha:
        raise ContractError("stage_zero_execution_probability_envelope_sha_mismatch")
    if supplied_envelope_id not in (None, "") and str(
        supplied_envelope_id
    ).strip() != probability.checkpoint_id:
        raise ContractError("stage_zero_execution_probability_envelope_id_mismatch")
    if probability.stage not in {"P0", "P0-M", "P1"}:
        raise ContractError("stage_zero_execution_probability_stage_invalid")
    if (
        compiled_cycle.get("stage") not in (None, "")
        and str(compiled_cycle.get("stage") or "").strip()
        != probability.stage
    ):
        raise ContractError("stage_zero_execution_probability_stage_mismatch")

    canonical_fact_keys = (
        "canonical_observation_event_id",
        "canonical_observation_sha256",
        "canonical_observation_committed_at",
        "hard_bound_id",
    )
    p1_canonical_fact_keys = (
        *canonical_fact_keys,
        "settlement_timezone",
        "source_lane",
        "report_type",
    )
    if probability.stage in {"P0", "P0-M"} and any(
        key in probability.lineage for key in canonical_fact_keys
    ):
        raise ContractError("stage_zero_execution_canonical_facts_forbidden")
    if probability.stage == "P1" and any(
        not str(probability.lineage.get(key) or "").strip()
        for key in p1_canonical_fact_keys
    ):
        raise ContractError("stage_zero_execution_p1_canonical_facts_missing")
    if probability.stage == "P1":
        trajectory_lineage = probability.lineage
        activation_id = str(
            trajectory_lineage.get("metar_trajectory_activation_id") or ""
        ).strip()
        artifact_id = str(
            trajectory_lineage.get("metar_trajectory_artifact_id") or ""
        ).strip()
        likelihood_mode = str(
            trajectory_lineage.get("observation_likelihood_mode") or ""
        ).strip()
        try:
            applied_strength = float(
                trajectory_lineage.get("metar_trajectory_applied_strength")
            )
        except (TypeError, ValueError):
            applied_strength = 0.0
        proof_sha = str(
            trajectory_lineage.get("metar_trajectory_proof_sha256") or ""
        ).strip().lower()
        path_state_sha = str(
            trajectory_lineage.get("metar_conditional_path_state_sha256")
            or ""
        ).strip().lower()
        future_curve_sha = str(
            trajectory_lineage.get("metar_future_temperature_curve_sha256")
            or ""
        ).strip().lower()
        # The physical method reads no root curve by construction, so the
        # curve sha it must carry is exactly "none".  Demanding a 64-hex
        # digest there would readmit the coupling the method exists to
        # exclude; demanding "NONE" from the curve method would admit a P1
        # whose evidence lineage lost its curve.
        if (
            not activation_id
            or activation_id.upper() == "NONE"
            or not artifact_id
            or likelihood_mode
            not in {
                "CONDITIONAL_HOURLY_FUTURE_PATH_SOFT_LR_PLUS_HARD_EXCLUSION_V4",
                "PHYSICAL_ABSOLUTE_TMAX_SOFT_LR_PLUS_HARD_EXCLUSION_V5",
            }
            or not 0.0 < applied_strength <= 1.0
            or len(proof_sha) != 64
            or any(character not in "0123456789abcdef" for character in proof_sha)
            or len(path_state_sha) != 64
            or any(
                character not in "0123456789abcdef"
                for character in path_state_sha
            )
            or (
                likelihood_mode
                == "PHYSICAL_ABSOLUTE_TMAX_SOFT_LR_PLUS_HARD_EXCLUSION_V5"
                and future_curve_sha != "none"
            )
            or (
                likelihood_mode
                == "CONDITIONAL_HOURLY_FUTURE_PATH_SOFT_LR_PLUS_HARD_EXCLUSION_V4"
                and (
                    len(future_curve_sha) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in future_curve_sha
                    )
                )
            )
        ):
            raise ContractError(
                "stage_zero_execution_p1_strong_likelihood_missing"
            )

    evaluation_at_utc, evaluation_at = _normalized_utc(
        "stage_zero_execution_evaluation_at_utc",
        compiled_cycle.get("evaluation_at_utc"),
    )
    probability_at_utc, probability_at = _normalized_utc(
        "stage_zero_execution_probability_decision_at_utc",
        probability.decision_at_utc,
    )
    fresh_until_utc, fresh_until = _normalized_utc(
        "stage_zero_execution_probability_fresh_until_utc",
        probability.fresh_until_utc,
    )
    if probability_at > evaluation_at:
        raise ContractError("stage_zero_execution_probability_after_evaluation")
    if evaluation_at >= fresh_until:
        raise ContractError("stage_zero_execution_probability_stale")

    plan = compiled_cycle.get("plan")
    if not isinstance(plan, Mapping):
        raise ContractError("stage_zero_execution_compiled_plan_missing")
    plan_lineage = plan.get("lineage")
    if not isinstance(plan_lineage, Mapping):
        raise ContractError("stage_zero_execution_plan_lineage_missing")
    if probability.stage in {"P0", "P0-M"} and any(
        key in plan_lineage for key in canonical_fact_keys
    ):
        raise ContractError("stage_zero_execution_plan_canonical_facts_forbidden")
    if probability.stage == "P1":
        if any(
            str(plan_lineage.get(key) or "").strip()
            != str(probability.lineage.get(key) or "").strip()
            for key in p1_canonical_fact_keys
        ):
            raise ContractError(
                "stage_zero_execution_p1_plan_canonical_facts_mismatch"
            )
    if (
        str(plan.get("station_id") or "").strip() != probability.station_id
        or str(plan.get("event_date") or "").strip() != probability.event_date
        or str(plan.get("settlement_timezone") or "").strip()
        != probability.settlement_timezone
    ):
        raise ContractError("stage_zero_execution_plan_probability_scope_mismatch")
    if str(plan_lineage.get("probability_stage") or "").strip() != probability.stage:
        raise ContractError("stage_zero_execution_plan_probability_stage_mismatch")
    if _required_sha256(
        "stage_zero_execution_plan_probability",
        plan_lineage.get("probability_envelope_sha256"),
    ) != probability_sha:
        raise ContractError("stage_zero_execution_plan_probability_sha_mismatch")
    if str(plan_lineage.get("probability_decision_at_utc") or "").strip() != probability.decision_at_utc:
        raise ContractError("stage_zero_execution_plan_probability_time_mismatch")

    market = compiled_cycle.get("production_market_snapshot")
    if not isinstance(market, Mapping):
        raise ContractError("stage_zero_execution_market_snapshot_missing")
    if (
        str(market.get("station_id") or "").strip() != probability.station_id
        or str(market.get("event_date") or "").strip() != probability.event_date
        or str(market.get("settlement_timezone") or "").strip()
        != probability.settlement_timezone
    ):
        raise ContractError("stage_zero_execution_market_probability_scope_mismatch")
    if str(market.get("quote_source") or "").strip().upper() != "OFFICIAL_CLOB_WS":
        raise ContractError("stage_zero_execution_quote_source_not_official_clob_ws")
    if any(
        market.get(key) is not True
        for key in (
            "evidence_fresh",
            "contract_matches",
            "order_state_known",
            "risk_inputs_complete",
        )
    ):
        raise ContractError("stage_zero_execution_market_evidence_incomplete")
    quote_observed_at_utc, quote_observed_at = _normalized_utc(
        "stage_zero_execution_quote_observed_at",
        market.get("quote_observed_at"),
    )
    quote_expires_at_utc, quote_expires_at = _normalized_utc(
        "stage_zero_execution_quote_expires_at",
        market.get("quote_expires_at"),
    )
    if quote_observed_at > evaluation_at:
        raise ContractError("stage_zero_execution_quote_after_evaluation")
    if quote_expires_at <= quote_observed_at or evaluation_at >= quote_expires_at:
        raise ContractError("stage_zero_execution_quote_stale")
    inventory_observed_at_utc, inventory_observed_at = _normalized_utc(
        "stage_zero_execution_inventory_observed_at",
        market.get("inventory_observed_at"),
    )
    order_state_observed_at_utc, order_state_observed_at = _normalized_utc(
        "stage_zero_execution_order_state_observed_at",
        market.get("order_state_observed_at"),
    )
    if inventory_observed_at > evaluation_at:
        raise ContractError("stage_zero_execution_inventory_after_evaluation")
    if order_state_observed_at > evaluation_at:
        raise ContractError("stage_zero_execution_order_state_after_evaluation")

    production_inputs = compiled_cycle.get("production_inputs")
    if not isinstance(production_inputs, Mapping):
        raise ContractError("stage_zero_execution_production_inputs_missing")
    regime_sha = _required_sha256(
        "stage_zero_execution_regime_artifact",
        production_inputs.get("regime_artifact_sha256"),
    )
    if _required_sha256(
        "stage_zero_execution_regime_probability",
        production_inputs.get("regime_probability_sha256"),
    ) != probability_sha:
        raise ContractError("stage_zero_execution_regime_probability_sha_mismatch")
    station_policy_sha = _required_sha256(
        "stage_zero_execution_station_policy",
        plan_lineage.get("policy_resolution_sha256"),
    )
    cash_policy_decision_sha = _required_sha256(
        "stage_zero_execution_cash_policy_decision",
        plan_lineage.get("cash_phase_decision_sha256"),
    )
    cash_policy_sha = _required_sha256(
        "stage_zero_execution_cash_policy",
        plan_lineage.get("cash_phase_policy_sha256"),
    )
    cash_observed_at_utc, cash_observed_at = _normalized_utc(
        "stage_zero_execution_cash_observed_at",
        plan_lineage.get("cash_phase_observed_at_utc"),
    )
    if cash_observed_at > evaluation_at:
        raise ContractError("stage_zero_execution_cash_after_evaluation")
    cash_phase = str(plan_lineage.get("cash_phase") or "").strip().upper()
    if cash_phase not in {"HEAVY", "STANDARD", "TAIL"}:
        raise ContractError("stage_zero_execution_cash_phase_invalid")
    cash_checkpoint_id = str(
        plan_lineage.get("cash_phase_checkpoint_id") or ""
    ).strip()
    if not cash_checkpoint_id:
        raise ContractError("stage_zero_execution_cash_checkpoint_missing")

    delta_v2_audit = compiled_cycle.get("delta_v2_audit")
    if not isinstance(delta_v2_audit, Mapping):
        raise ContractError("stage_zero_execution_delta_v2_evaluation_missing")
    plan_delta_v2_audit = plan.get("delta_v2_audit")
    if (
        isinstance(plan_delta_v2_audit, Mapping)
        and _required_sha256(
            "stage_zero_execution_plan_delta_v2_evaluation",
            plan_delta_v2_audit.get("content_sha256"),
        )
        != _required_sha256(
            "stage_zero_execution_delta_v2_evaluation",
            delta_v2_audit.get("content_sha256"),
        )
    ):
        raise ContractError("stage_zero_execution_delta_v2_evaluation_mismatch")
    delta_v2_artifact_sha = _required_sha256(
        "stage_zero_execution_delta_v2_artifact",
        production_inputs.get("delta_v2_artifact_sha256"),
    )
    delta_v2_evaluation_sha = _required_sha256(
        "stage_zero_execution_delta_v2_evaluation",
        delta_v2_audit.get("content_sha256"),
    )

    supplied_cycle_sha = _required_sha256(
        "stage_zero_execution_production_cycle",
        compiled_cycle.get("production_cycle_sha256"),
    )
    expected_cycle_sha = content_sha256({
        key: value
        for key, value in compiled_cycle.items()
        if key not in {"production_cycle_sha256", "compiled_plan_sha256"}
    })
    if supplied_cycle_sha != expected_cycle_sha:
        raise ContractError("stage_zero_execution_production_cycle_sha_mismatch")
    full_compiled_plan = dict(compiled_cycle)
    supplied_full_plan_sha = full_compiled_plan.pop("compiled_plan_sha256", None)
    compiled_plan_sha = content_sha256(full_compiled_plan)
    if supplied_full_plan_sha not in (None, "") and _required_sha256(
        "stage_zero_execution_compiled_plan",
        supplied_full_plan_sha,
    ) != compiled_plan_sha:
        raise ContractError("stage_zero_execution_compiled_plan_sha_mismatch")

    anchor = {
        "schema": STAGE_ZERO_EXECUTION_ANCHOR_SCHEMA,
        "station_id": probability.station_id,
        "event_date": probability.event_date,
        "settlement_timezone": probability.settlement_timezone,
        "evaluation_at_utc": evaluation_at_utc,
        "probability_evidence_fresh": True,
        "official_ws_fresh": True,
        "inventory_complete": True,
        "order_state_known": True,
        "probability_envelope": {
            **probability.to_dict(),
            "envelope_id": probability.checkpoint_id,
            "content_sha256": probability_sha,
            "decision_at_utc": probability_at_utc,
            "fresh_until_utc": fresh_until_utc,
        },
        "regime_artifact_sha256": regime_sha,
        "official_ws_snapshot": {
            "snapshot_id": str(market.get("snapshot_id") or "").strip(),
            "content_sha256": _required_sha256(
                "stage_zero_execution_official_ws_snapshot",
                market.get("official_ws_quote_sha256")
                or market.get("content_sha256"),
            ),
            "quote_observed_at": quote_observed_at_utc,
            "quote_expires_at": quote_expires_at_utc,
        },
        "inventory_snapshot": {
            "inventory_snapshot_id": str(
                market.get("inventory_snapshot_id") or ""
            ).strip(),
            "content_sha256": _required_sha256(
                "stage_zero_execution_inventory",
                market.get("inventory_sha256"),
            ),
            "observed_at": inventory_observed_at_utc,
        },
        "order_state_snapshot": {
            "order_state_snapshot_id": str(
                market.get("order_state_snapshot_id") or ""
            ).strip(),
            "content_sha256": _required_sha256(
                "stage_zero_execution_order_state",
                market.get("order_state_sha256"),
            ),
            "observed_at": order_state_observed_at_utc,
        },
        "station_policy_sha256": station_policy_sha,
        "cash_policy_decision_sha256": cash_policy_decision_sha,
        "cash_phase": {
            "phase": cash_phase,
            "checkpoint_id": cash_checkpoint_id,
            "observed_at_utc": cash_observed_at_utc,
            "policy_sha256": cash_policy_sha,
        },
        "delta_v2_artifact_sha256": delta_v2_artifact_sha,
        "delta_v2_evaluation_sha256": delta_v2_evaluation_sha,
        "compiled_plan": full_compiled_plan,
        "compiled_plan_sha256": compiled_plan_sha,
        "final_authority": "P1",
        "capital_authorized": False,
        "intents_written": 0,
        "reservations_written": 0,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    if (
        not anchor["official_ws_snapshot"]["snapshot_id"]
        or not anchor["inventory_snapshot"]["inventory_snapshot_id"]
        or not anchor["order_state_snapshot"]["order_state_snapshot_id"]
    ):
        raise ContractError("stage_zero_execution_snapshot_identity_missing")
    anchor["content_sha256"] = content_sha256(anchor)
    return anchor


def _latest_snapshot_row(conn: sqlite3.Connection, table: str, station_id: str, event_date: str, evaluation_at_utc: str) -> dict[str, Any]:
    cursor = conn.execute(
        f"SELECT * FROM {table} WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=? ORDER BY observed_at DESC, rowid DESC LIMIT 1",
        (station_id, event_date, evaluation_at_utc),
    ).fetchone()
    if cursor is None:
        raise ContractError(f"{table}_missing")
    if isinstance(cursor, sqlite3.Row):
        return {key: cursor[key] for key in cursor.keys()}
    description = conn.execute(f"SELECT * FROM {table} LIMIT 0").description or ()
    return {item[0]: cursor[index] for index, item in enumerate(description)}


def load_production_market_snapshot(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    evaluation_at_utc: str,
) -> dict[str, Any]:
    """Join quote, confirmed inventory and own order-state generations."""

    status = production_ssot_schema_status(conn)
    if not status["target_tables_present"]:
        raise ContractError("production_market_ssot_table_missing")
    quote = _latest_snapshot_row(conn, "basket_official_ws_snapshot", station_id, event_date, evaluation_at_utc)
    inventory = _latest_snapshot_row(conn, "basket_confirmed_inventory_snapshot", station_id, event_date, evaluation_at_utc)
    order_state = _latest_snapshot_row(conn, "basket_order_state_snapshot", station_id, event_date, evaluation_at_utc)
    quote_version = str(quote["quote_version"] or "")
    inventory_version = str(quote["inventory_version"] or "")
    order_version = str(quote["order_state_version"] or "")
    if not quote_version or not inventory_version or not order_version:
        raise ContractError("production_snapshot_version_missing")
    if str(inventory["inventory_version"]) != inventory_version:
        raise ContractError("production_inventory_version_drift")
    if str(order_state["order_state_version"]) != order_version:
        raise ContractError("production_order_state_version_drift")
    quote_observed_at = _aware_utc(
        "production_quote_observed_at",
        quote["quote_observed_at"],
    )
    quote_expires_at = _aware_utc(
        "production_quote_expires_at",
        quote["quote_expires_at"],
    )
    evaluation_at = _aware_utc(
        "production_evaluation_at_utc",
        evaluation_at_utc,
    )
    if quote_expires_at <= quote_observed_at:
        raise ContractError(
            "production_quote_expiry_must_follow_observation"
        )
    if evaluation_at >= quote_expires_at:
        raise ContractError("production_quote_expired")
    if int(inventory["complete"] or 0) != 1:
        raise ContractError("production_inventory_incomplete")
    if int(order_state["known"] or 0) != 1:
        raise ContractError("production_order_state_unknown")
    buckets = _json("buckets", quote["buckets_json"])
    if not isinstance(buckets, list) or not buckets:
        raise ContractError("production_ws_buckets_missing")
    inventory_json = _json("inventory", inventory["inventory_json"], mapping=True)
    order_json = _json("order_state", order_state["order_state_json"], mapping=True)
    _verify_record_sha("basket_confirmed_inventory_snapshot_v1", {
        **inventory,
        "inventory_json": inventory_json,
    })
    _verify_record_sha("basket_order_state_snapshot_v1", {
        **order_state,
        "order_state_json": order_json,
    })
    filled = inventory_json.get("filled_by_bucket") or inventory_json.get("filled_quantities") or {}
    pending = order_json.get("pending_by_bucket") or order_json.get("pending_quantities") or {}
    if not isinstance(filled, Mapping) or not isinstance(pending, Mapping):
        raise ContractError("production_inventory_order_state_shape_invalid")
    current_core_buckets: tuple[str, ...] = ()
    current_direction: str | None = None
    decision_table_present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='auto_trade_decisions'"
    ).fetchone() is not None
    if decision_table_present:
        prior_decision = conn.execute(
            "SELECT details_json FROM auto_trade_decisions "
            "WHERE station_id=? AND event_date=? AND strategy_lane='basket' "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (station_id, event_date),
        ).fetchone()
        if prior_decision is not None:
            prior_decision_fields = _row_mapping(
                prior_decision,
                ("details_json",),
            )
            prior_details = _json(
                "prior_basket_decision_details",
                prior_decision_fields["details_json"],
                mapping=True,
            )
            raw_core = prior_details.get("core_buckets")
            if raw_core is not None:
                if (
                    not isinstance(raw_core, list)
                    or any(
                        not isinstance(item, str) or not item.strip()
                        for item in raw_core
                    )
                    or len(raw_core) != len(set(raw_core))
                ):
                    raise ContractError(
                        "production_prior_core_buckets_invalid"
                    )
                current_core_buckets = tuple(raw_core)
            raw_direction = prior_details.get("direction")
            if raw_direction is not None:
                current_direction = str(raw_direction).strip().upper()
                if current_direction not in {"UP", "DOWN", "NEUTRAL"}:
                    raise ContractError(
                        "production_prior_direction_invalid"
                    )
    # Verify the immutable quote record before adding the joined inventory and
    # order-state projections.  The latter are derived fields and must not be
    # fed back into the quote content hash.
    cash_input = _json("cash_phase_input", quote["cash_phase_input_json"], mapping=True)
    _verify_record_sha(
        "basket_official_ws_snapshot_v1",
        {**quote, "buckets_json": buckets, "cash_phase_input_json": cash_input},
    )
    filled_cost = inventory_json.get("filled_cost_by_bucket") or {}
    pending_notional = order_json.get("pending_notional_by_bucket") or {}
    if not isinstance(filled_cost, Mapping) or not isinstance(pending_notional, Mapping):
        raise ContractError("production_inventory_order_state_cost_shape_invalid")
    normalized_buckets: list[dict[str, Any]] = []
    for raw in buckets:
        if not isinstance(raw, Mapping):
            raise ContractError("production_ws_bucket_invalid")
        bucket = str(raw.get("bucket") or "")
        asks = raw.get("ask_levels")
        if not bucket or not isinstance(asks, list):
            raise ContractError("production_ws_ask_depth_invalid")
        ask_available = bool(raw.get("ask_available", bool(asks)))
        if ask_available != bool(asks):
            raise ContractError("production_ws_ask_availability_mismatch")
        bid_levels = raw.get("bid_levels")
        if not isinstance(bid_levels, list):
            raise ContractError("production_ws_bid_depth_invalid")
        bid_available = bool(raw.get("bid_available", bool(bid_levels)))
        if bid_available != bool(bid_levels):
            raise ContractError("production_ws_bid_availability_mismatch")
        if not ask_available and not bid_available:
            # Same admission the materializer makes (charter 48): a
            # listing-day bucket nobody has quoted yet is a legal market
            # state when it names itself; an unnamed empty book is still a
            # defect.
            if not bool(raw.get("unmaterialized_book")):
                raise ContractError("production_ws_book_empty")
        bid_price = raw.get("bid_price")
        if bid_price is None and bid_levels:
            bid_price = bid_levels[0].get("price") if isinstance(bid_levels[0], Mapping) else None
        if bid_price is None and not bid_levels:
            bid_price = 0.0
        try:
            filled_shares = float(filled.get(bucket, 0.0) or 0.0)
            filled_cost_usdc = float(filled_cost.get(bucket, 0.0) or 0.0)
            pending_shares = float(pending.get(bucket, 0.0) or 0.0)
            pending_notional_usdc = float(pending_notional.get(bucket, 0.0) or 0.0)
            bid_price_value = float(bid_price)
        except (TypeError, ValueError) as exc:
            raise ContractError("production_market_numeric_projection_invalid") from exc
        if min(filled_shares, filled_cost_usdc, pending_shares, pending_notional_usdc) < 0.0:
            raise ContractError("production_market_numeric_projection_negative")
        normalized_buckets.append({
            **dict(raw),
            "ask_available": ask_available,
            "bid_price": bid_price_value,
            "bid_available": bid_available,
            "maker_queue_ahead_shares": float(raw.get("maker_queue_ahead_shares", 0.0) or 0.0),
            # Missing bid depth cannot anchor formal maker pricing.
            "maker_queue_known": bool(
                raw.get("maker_queue_known", False)
            ) and bid_available,
            "filled_shares": filled_shares,
            "filled_cost_usdc": filled_cost_usdc,
            "pending_shares": pending_shares,
            "pending_notional_usdc": pending_notional_usdc,
            "active_before": bool(raw.get("active_before", False) or filled_shares > 0.0 or pending_shares > 0.0),
            "ask_levels": [dict(level) for level in asks],
        })
    if current_core_buckets:
        canonical_buckets = tuple(
            str(item["bucket"]) for item in normalized_buckets
        )
        ordinal_by_bucket = {
            bucket: ordinal
            for ordinal, bucket in enumerate(canonical_buckets)
        }
        core_ordinals = tuple(
            ordinal_by_bucket.get(bucket)
            for bucket in current_core_buckets
        )
        if (
            len(ordinal_by_bucket) != len(canonical_buckets)
            or any(ordinal is None for ordinal in core_ordinals)
            or tuple(sorted(core_ordinals)) != core_ordinals
            or core_ordinals
            != tuple(
                range(
                    core_ordinals[0],
                    core_ordinals[0] + len(core_ordinals),
                )
            )
        ):
            raise ContractError("production_prior_core_buckets_invalid")
    if str(cash_input.get("station_id") or "") != station_id or str(cash_input.get("event_date_local") or "") != event_date:
        raise ContractError("production_cash_phase_scope_mismatch")
    return {
        "schema": "bayes_basket_production_market_snapshot_v1",
        "snapshot_id": str(quote["snapshot_id"]),
        "sequence": int(quote["sequence"]),
        "observed_at": str(quote["observed_at"]),
        "market_id": str(quote["market_id"]),
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": str(quote["settlement_timezone"] or ""),
        "quote_version": quote_version,
        "quote_observed_at": str(quote["quote_observed_at"]),
        "quote_expires_at": str(quote["quote_expires_at"]),
        "target_execution_hard_deadline_at": derive_target_execution_hard_deadline(
            # The complete multi-bucket snapshot first becomes actionable at
            # this compile evaluation.  Quote age remains anchored to the
            # oldest official book observation above; do not conflate the two
            # clocks or a valid snapshot can expire before it is joinable.
            evaluation_at_utc,
            quote_expires_at=quote["quote_expires_at"],
        ),
        "quote_freshness_policy_sha256": str(
            quote["quote_freshness_policy_sha256"]
        ),
        "quote_source": str(quote["quote_source"]),
        "official_ws_quote_sha256": str(quote["content_sha256"]),
        "inventory_snapshot_id": str(inventory["inventory_snapshot_id"]),
        "inventory_version": inventory_version,
        "inventory_sha256": str(inventory["content_sha256"]),
        "inventory_observed_at": str(inventory["observed_at"]),
        "order_state_snapshot_id": str(
            order_state["order_state_snapshot_id"]
        ),
        "order_state_version": order_version,
        "order_state_sha256": str(order_state["content_sha256"]),
        "order_state_observed_at": str(order_state["observed_at"]),
        "remaining_budget_usdc": float(quote["remaining_budget_usdc"]),
        "current_core_buckets": list(current_core_buckets),
        "current_direction": current_direction,
        "buckets": normalized_buckets,
        "cash_phase_input": cash_input,
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
    }


def derive_policy_maturity_bin(
    lead_label: str,
    *,
    event_date: str,
    settlement_timezone: str,
    evaluation_at_utc: str,
) -> str:
    """Policy maturity from the calendar distance to the event.

    The run-lead vocabulary moved to hour buckets (charter 47), but policy
    maturity was never really the run's lead -- it is how far the *event*
    is, which is what the d-keyed policy scopes, cash phases and staging
    were fitted against.  Copying an artifact's hour label into a policy
    lookup makes every migrated envelope unmatchable (station policy and
    delta-v2 policy both fail closed on it, at two different call sites).
    The label chooses a vocabulary; it never supplies the answer.

    That pass-through is the whole hazard, so it is written down rather than
    left to be rediscovered.  ``d0``/``d1``/``d2``/``d2_fallback`` label two
    different measurements: a run's lead -- how old the forecast is -- and an
    event's maturity, how far the event still is.  No table carries both
    columns, so nothing anywhere shows a reader that they differ, and a join
    written ``ON a.lead_bucket = b.maturity_bin`` returns rows and looks
    right.  This function is the only place that converts between them.

    The pass-through has already been wrong, at scale.  4,499 committed
    envelopes carry a calendar label, and twelve of twelve sampled disagree
    with the distance their own decision time implies -- every one says d1 for
    an event that was that same local day.  They reach here because the
    republisher fallback lane derives its label from the *capture* time
    (``native_ensemble_p0._lead_bucket``), and ``lead_bucket`` is a frozen
    lineage key, so P0-M and P1 repeat it verbatim.  Those decisions were
    priced against a station policy one day off.

    So the calendar label is no longer taken at its word either: both branches
    now compute the distance from the decision's own clock.  A label is a
    property of the moment it was stamped, and this function is asked at a
    different moment -- a capture that was correctly ``d1`` yesterday is
    ``d0`` for a decision taken on the event day, and it was the frozen label
    that got used.  Refusing a disagreeing label was tried first and cost
    twenty tests; computing through it costs none, because those tests need
    the lookup to succeed, not the label to survive.
    """

    label = str(lead_label or "").strip()
    if not label:
        raise ContractError("probability_policy_maturity_bin_missing")
    event_day = date.fromisoformat(str(event_date))
    decision_local_day = (
        datetime.fromisoformat(evaluation_at_utc.replace("Z", "+00:00"))
        .astimezone(ZoneInfo(str(settlement_timezone)))
        .date()
    )
    days_out = (event_day - decision_local_day).days
    if days_out <= 0:
        maturity = "d0"
    elif days_out == 1:
        maturity = "d1"
    elif days_out == 2:
        maturity = "d2"
    else:
        maturity = "d2_fallback"
    if label.startswith("h"):
        return maturity
    if label not in POLICY_MATURITY_BINS:
        raise ContractError(
            "probability_policy_maturity_bin_vocabulary_invalid:" + label
        )
    return maturity


def resolve_delta_v2_runtime_authority(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    stage: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    strategy_version: str,
    evaluation_at_utc: str,
) -> tuple[Any | None, Callable[..., Mapping[str, Any]] | None, str | None]:
    """Resolve the one v2 policy/fill authority used by every basket stage."""

    artifact = None
    blocker = None
    try:
        artifact = resolve_delta_v2_policy(
            conn,
            station_id=station_id,
            stage=stage,
            season_bin=season_bin,
            maturity_bin=maturity_bin,
            liquidity_bin=liquidity_bin,
            strategy_version=strategy_version,
            evaluation_at_utc=evaluation_at_utc,
            required_authority_scope="PRODUCTION_NO_LIVE",
        )
    except (ContractError, DeltaV2ContractError) as exc:
        blocker = str(exc)

    def resolve_fill(**kwargs: Any) -> Mapping[str, Any]:
        if artifact is None:
            raise ContractError("delta_v2_policy_unresolved")
        mode = str(kwargs.get("order_mode") or "").upper()
        lookup_timeout = (
            float(kwargs["timeout_seconds"])
            if mode == "MAKER"
            else float(artifact.timeout_grid_seconds[0])
        )
        fill_artifact = resolve_delta_v2_fill_distribution(
            conn,
            station_id=station_id,
            stage=stage,
            season_bin=season_bin,
            maturity_bin=maturity_bin,
            liquidity_bin=liquidity_bin,
            bucket=str(kwargs["bucket"]),
            price=float(kwargs["limit_price"]),
            timeout_seconds=lookup_timeout,
            queue_ahead_shares=float(kwargs["queue_ahead_shares"]),
            spread=float(kwargs["spread"]),
            depth=float(kwargs["depth"]),
            quote_version=str(kwargs["quote_version"]),
            evaluation_at_utc=evaluation_at_utc,
            required_authority_scope="PRODUCTION_NO_LIVE",
            policy_fill_distribution_artifact_sha256=(
                artifact.fill_distribution_artifact_sha256
            ),
        )
        return {
            "fill_distribution": fill_artifact.to_kernel_fill_distribution(mode),
            "execution_costs": fill_artifact.to_kernel_execution_costs(mode),
            "fill_artifact_id": fill_artifact.artifact_id,
            "fill_artifact_content_sha256": fill_artifact.content_sha256,
            "fill_distribution_artifact_sha256": (
                fill_artifact.fill_distribution_artifact_sha256
            ),
        }

    return artifact, (resolve_fill if artifact is not None else None), blocker


def compile_production_cycle_from_ssot(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    evaluation_at_utc: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    strategy_version: str,
    policy_authority_scope: str,
    cash_phase_authority_manifest: CashPhaseAuthorityManifest | Mapping[str, Any] | None = None,
    base_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile one real-SSOT candidate while preserving the no-live boundary."""

    evaluation_at_utc, _evaluation_at = _normalized_utc(
        "production_evaluation_at_utc",
        evaluation_at_utc,
    )

    try:
        assert_process_memory_budget()
        # The target SSOT owns this connection.  Permit exactly one bounded
        # TRUNCATE checkpoint when its WAL is oversized; the resource guard
        # verifies writer/reader contention and still fails closed if the WAL
        # cannot be safely reduced below the hard limit.  This never applies
        # to the protected shared-live database and does not relax OOS/live
        # authority gates.
        assert_connection_target_capacity(
            conn,
            recover_oversized_wal=True,
        )
    except ResourceBoundError as exc:
        raise ContractError("delta_v2_resource_probe_failed:" + str(exc)) from exc
    expected_source_lane = _strict_expected_source_lane(
        conn,
        station_id=station_id,
        event_date=event_date,
    )
    envelopes = load_materialized_probability_envelopes(
        conn,
        station_id=station_id,
        event_date=event_date,
        evaluation_at_utc=evaluation_at_utc,
        required_probability_pathway=(
            P0_PROBABILITY_PATHWAY
            if strategy_version == "target-v2"
            else None
        ),
        expected_source_lane=expected_source_lane,
    )
    current_envelope = select_current_stage(
        envelopes,
        at_utc=evaluation_at_utc,
    )
    probability_bridge = assert_current_probability_bridge(
        conn,
        probability=current_envelope,
        evaluation_at_utc=evaluation_at_utc,
    )
    assert_formal_probability_model_authority(
        envelopes,
        probability=current_envelope,
    )
    regime_artifact = load_regime_artifact(
        conn,
        probability=current_envelope,
        evaluation_at_utc=evaluation_at_utc,
    )
    probability_maturity_bin = derive_policy_maturity_bin(
        str(current_envelope.lineage.get("lead_bucket") or ""),
        event_date=str(current_envelope.event_date),
        settlement_timezone=str(current_envelope.settlement_timezone),
        evaluation_at_utc=evaluation_at_utc,
    )
    market_snapshot = load_production_market_snapshot(
        conn, station_id=station_id, event_date=event_date, evaluation_at_utc=evaluation_at_utc
    )
    bundle = load_production_artifacts(
        conn, station_id=station_id, evaluation_at_utc=evaluation_at_utc, require_calibration=False
    )
    # Validate the formal station-policy activation before the strict-delta
    # blocked-result path.  A missing strict policy must fail closed, but it
    # must not mask an independently invalid/unpromoted station policy.
    resolve_station_policy(
        activations=bundle.policy_activations,
        global_priors=bundle.global_priors,
        artifacts=bundle.policy_artifacts,
        station_id=current_envelope.station_id,
        stage=current_envelope.stage,
        season_bin=season_bin,
        maturity_bin=probability_maturity_bin,
        liquidity_bin=liquidity_bin,
        strategy_version=strategy_version,
        # The probability checkpoint owns weather evidence as-of time; the
        # execution policy owns the current action decision time.  Binding
        # policy activation to the older probability timestamp would make a
        # valid policy upgrade invisible until another weather update.
        decision_at_utc=evaluation_at_utc,
        required_authority_scope=policy_authority_scope,
    )
    cash_input = BasketCashPhaseInput.from_mapping(market_snapshot.pop("cash_phase_input"))
    authority_manifest = cash_phase_authority_manifest
    if authority_manifest is None:
        authority_manifest = load_cash_phase_authority_manifest(
            conn,
            station_id=station_id,
            observed_at_utc=cash_input.observed_at_utc,
            required_authority_scope=policy_authority_scope,
        )
    cash_policy = resolve_active_cash_phase_policy(
        bundle.cash_phase_policies,
        authority_manifest=authority_manifest,
        station_id=station_id,
        observed_at_utc=cash_input.observed_at_utc,
        required_authority_scope=policy_authority_scope,
    )
    if cash_input.policy_version != cash_policy.policy_version:
        raise ContractError("production_cash_phase_authority_policy_version_mismatch")
    if cash_input.station_timezone != cash_policy.station_timezone:
        raise ContractError("production_cash_phase_authority_timezone_mismatch")
    market_timezone = str(
        market_snapshot.get("settlement_timezone") or ""
    ).strip()
    if cash_policy.station_timezone != market_timezone:
        raise ContractError(
            "production_cash_phase_market_registry_timezone_mismatch"
        )
    (
        delta_v2_artifact,
        delta_v2_fill_resolver,
        delta_v2_resolution_blocker,
    ) = resolve_delta_v2_runtime_authority(
        conn,
        station_id=station_id,
        stage=current_envelope.stage,
        season_bin=season_bin,
        maturity_bin=probability_maturity_bin,
        liquidity_bin=liquidity_bin,
        strategy_version=strategy_version,
        evaluation_at_utc=evaluation_at_utc,
    )
    plan = compile_production_plan(
        # The opener's recorded stake factor, read-only; None until a P0
        # decision authorizes and the worker records it, which keeps this
        # module inside its never-writes contract.
        chain_consolidation_tilt=load_chain_tilt(
            conn, station_id=station_id, event_date=event_date,
        ),
        probability_envelopes=envelopes,
        market_snapshot=market_snapshot,
        policy_activations=bundle.policy_activations,
        global_priors=bundle.global_priors,
        policy_artifacts=bundle.policy_artifacts,
        evaluation_at_utc=evaluation_at_utc,
        season_bin=season_bin,
        # The probability artifact owns lead/maturity.  A static worker
        # configuration cannot silently force a d2/d2_fallback envelope
        # through a d1 policy activation.
        maturity_bin=probability_maturity_bin,
        liquidity_bin=liquidity_bin,
        strategy_version=strategy_version,
        base_policy=base_policy,
        delta_v2_policy=delta_v2_artifact,
        delta_v2_fill_resolver=delta_v2_fill_resolver,
        cash_phase_policy=cash_policy,
        cash_phase_input=cash_input,
        policy_authority_scope=policy_authority_scope,
        regime_artifact=regime_artifact,
        require_regime_ssot=True,
        resource_probe_ok=True,
        delta_v2_owns_cadence=True,
        expected_source_lane=expected_source_lane,
    )
    plan["production_inputs"] = {
        "probability_envelope_count": len(envelopes),
        "probability_envelope_ids": [item.checkpoint_id for item in envelopes],
        "probability_bridge_history_id": probability_bridge["history_id"],
        "probability_bridge_projection_sha256": (
            probability_bridge["projection_sha256"]
        ),
        "probability_bridge_checkpoint_id": (
            probability_bridge["checkpoint_id"]
        ),
        "probability_bridge_envelope_content_sha256": (
            probability_bridge["envelope_content_sha256"]
        ),
        "quote_snapshot_id": market_snapshot["snapshot_id"],
        "quote_source": market_snapshot["quote_source"],
        "inventory_version": market_snapshot["inventory_version"],
        "order_state_version": market_snapshot["order_state_version"],
        "cash_phase_policy_version": cash_policy.policy_version,
        "cash_phase_policy_sha256": cash_policy.content_sha256,
        "cash_phase_authority_manifest": (
            authority_manifest.to_dict()
            if isinstance(authority_manifest, CashPhaseAuthorityManifest)
            else dict(authority_manifest or {})
        ),
        "probability_maturity_bin": probability_maturity_bin,
        "regime_artifact_id": regime_artifact.artifact_id,
        "regime_artifact_sha256": regime_artifact.content_sha256,
        "regime_probability_sha256": regime_artifact.probability_sha256,
        "regime_state": regime_artifact.regime,
        "configured_maturity_bin": maturity_bin,
        "season_bin": season_bin,
        "liquidity_bin": liquidity_bin,
        "strategy_version": strategy_version,
        "delta_v2_artifact_id": (
            delta_v2_artifact.artifact_id
            if delta_v2_artifact is not None
            else None
        ),
        "delta_v2_artifact_sha256": (
            delta_v2_artifact.content_sha256
            if delta_v2_artifact is not None
            else None
        ),
        "delta_v2_fill_distribution_artifact_sha256": (
            delta_v2_artifact.fill_distribution_artifact_sha256
            if delta_v2_artifact is not None
            else None
        ),
        "delta_v2_resolution_blocker": delta_v2_resolution_blocker,
        "read_only": True,
    }
    # The writer boundary receives the exact quote snapshot that was used by
    # the compiler.  It never refetches market data or consults tick history.
    plan["production_market_snapshot"] = market_snapshot
    # The compiler's evaluation timestamp is an immutable decision clock.
    # P0/P0-M stage-zero anchors bind to this exact clock and the ledger
    # rejects a later write clock rather than silently extending freshness.
    plan["evaluation_at_utc"] = evaluation_at_utc
    plan["production_cycle_sha256"] = content_sha256(
        {key: value for key, value in plan.items() if key != "production_cycle_sha256"}
    )
    return plan


def apply_no_live_execution_receipt(
    conn: sqlite3.Connection,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Formal production-IO receipt ingress for the target no-live lane.

    The compiler remains a pure SSOT read/compile operation.  Receipt
    materialization is an explicit second boundary and is delegated to the
    single transition runtime, which performs the atomic receipt, order-state,
    reconciliation-version and post-fill request transaction.  No network or
    gateway object is accepted by this API.
    """

    from .execution_transition_runtime import apply_execution_receipt

    return apply_execution_receipt(conn, receipt)


def build_no_live_execution_receipt(**kwargs: Any) -> dict[str, Any]:
    """Build a deterministic simulated receipt for isolated no-live replay."""

    from .execution_transition_runtime import build_no_live_execution_receipt as _build

    return _build(**kwargs)


def load_post_fill_recompute_requests(
    conn: sqlite3.Connection,
    *,
    order_intent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Expose pending fill-triggered recompute requests to the worker."""

    from .execution_transition_runtime import load_post_fill_recompute_requests as _load

    return _load(conn, order_intent_id=order_intent_id)


__all__ = [
    "ProductionArtifactBundle",
    "STAGE_ZERO_EXECUTION_ANCHOR_SCHEMA",
    "apply_no_live_execution_receipt",
    "build_no_live_execution_receipt",
    "compile_production_cycle_from_ssot",
    "assert_formal_probability_model_authority",
    "load_materialized_probability_envelopes",
    "load_production_artifacts",
    "load_production_market_snapshot",
    "load_post_fill_recompute_requests",
    "resolve_delta_v2_runtime_authority",
    "target_registry_source_lane",
    "target_atomic_facts_from_cycle",
    "target_stage_zero_execution_anchor_from_cycle",
]
