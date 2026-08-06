"""Builders and reference validation for canonical republisher no-live P0."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any, Mapping, Sequence

from .canonical_contract import (
    CanonicalRepublisherNoLiveActivationContract,
    CanonicalRepublisherNoLiveActivationFact,
    CanonicalRepublisherOOSArtifactFact,
    REPUBLISHER_NO_LIVE_ACTIVATION_CONTRACT_SCHEMA,
)
from .contracts import ContractError, content_sha256
from .market_bucket_bridge import CanonicalMarketBucketBridgeFact
from .republisher_cross_selection import (
    assert_republisher_cross_selection_fact_references,
    load_persisted_republisher_cross_selection,
)
from .republisher_no_live_activation import _load_activation_chain
from .republisher_no_live_p0_contract import (
    CONTRACT_SCHEMA,
    CanonicalForwardApplicabilityFact,
    CanonicalP0SnapshotFact,
    CanonicalProbabilityDistributionArtifactFact,
    CanonicalRepublisherNoLiveP0Contract,
)


QUANTUM = Decimal("0.000000000001")
DISTRIBUTION_BUILDER_ID = "republisher_train_residual_distribution_builder_v1"
DISTRIBUTION_ACTIVATION_BUILDER_ID = (
    "republisher_distribution_activation_builder_v1"
)


def _q(value: Decimal) -> str:
    if not value.is_finite():
        raise ContractError("republisher_probability_nonfinite")
    quantized = value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized == 0:
        quantized = Decimal(0)
    return format(quantized, ".12f")


def _d(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ContractError("republisher_probability_decimal_invalid") from exc
    if not result.is_finite():
        raise ContractError("republisher_probability_nonfinite")
    return result


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ContractError("republisher_probability_values_empty")
    return sum(values) / Decimal(len(values))


def _rmse(values: Sequence[Decimal]) -> Decimal:
    return (_mean([item * item for item in values])).sqrt()


def _crps(samples: Sequence[Decimal], truth: Decimal) -> Decimal:
    first = _mean([abs(item - truth) for item in samples])
    pairwise = _mean([abs(left - right) for left in samples for right in samples])
    return first - pairwise / Decimal(2)


def _load_artifact(
    conn: sqlite3.Connection, content_sha256_value: str
) -> CanonicalRepublisherOOSArtifactFact:
    rows = conn.execute(
        "SELECT content_sha256,fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='republisher_oos_artifact' AND content_sha256=?",
        (content_sha256_value,),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError("republisher_probability_parent_artifact_missing")
    stored_sha, fact_json = rows[0]
    try:
        raw = json.loads(str(fact_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("republisher_probability_parent_artifact_json_invalid") from exc
    if not isinstance(raw, Mapping):
        raise ContractError("republisher_probability_parent_artifact_invalid")
    fact = CanonicalRepublisherOOSArtifactFact.from_mapping(raw)
    if str(stored_sha) != fact.content_sha256:
        raise ContractError("republisher_probability_parent_artifact_hash_mismatch")
    return fact


def _selection_artifacts(
    conn: sqlite3.Connection, selection_sha: str
) -> tuple[Any, CanonicalRepublisherOOSArtifactFact, CanonicalRepublisherOOSArtifactFact]:
    selection, _ = load_persisted_republisher_cross_selection(conn, selection_sha)
    by_model = {
        ref.upstream_model_id: _load_artifact(conn, ref.artifact_content_sha256)
        for ref in selection.component_artifact_refs
    }
    if set(by_model) != {"AIFS", "ICON"}:
        raise ContractError("republisher_probability_parent_product_set_invalid")
    return selection, by_model["AIFS"], by_model["ICON"]


def _artifact_fact(
    conn: sqlite3.Connection,
    *,
    selection_sha: str,
    as_of_utc: str,
) -> dict[str, Any]:
    selection, aifs, icon = _selection_artifacts(conn, selection_sha)
    assert_republisher_cross_selection_fact_references(
        conn, selection, as_of_utc=as_of_utc
    )
    if (
        aifs.train_n != 20
        or icon.train_n != 20
        or aifs.holdout_n != 8
        or icon.holdout_n != 8
        or len(aifs.cohort_rows) != 28
        or len(icon.cohort_rows) != 28
    ):
        raise ContractError("republisher_probability_exact_20_8_cohort_required")
    if [row.event_date for row in aifs.cohort_rows] != [
        row.event_date for row in icon.cohort_rows
    ]:
        raise ContractError("republisher_probability_cohort_date_mismatch")
    weights = {key: _d(value) for key, value in selection.selected_weights.items()}
    if set(weights) != {"AIFS", "ICON"} or sum(weights.values()) != Decimal(1):
        raise ContractError("republisher_probability_weight_vector_invalid")
    biases = {
        "AIFS": _d(aifs.calibration_parameters["bias_c"]),
        "ICON": _d(icon.calibration_parameters["bias_c"]),
    }
    scenarios: list[dict[str, Any]] = []
    residuals: list[Decimal] = []
    train_truths: list[Decimal] = []
    with localcontext() as context:
        context.prec = 50
        for index in range(20):
            aifs_row = aifs.cohort_rows[index]
            icon_row = icon.cohort_rows[index]
            if (
                aifs_row.partition != "train"
                or icon_row.partition != "train"
                or aifs_row.canonical_truth_fact_content_sha256
                != icon_row.canonical_truth_fact_content_sha256
                or aifs_row.truth_value_c != icon_row.truth_value_c
            ):
                raise ContractError("republisher_probability_train_partition_mismatch")
            point = (
                weights["AIFS"]
                * (_d(aifs_row.forecast_daily_max_c) + biases["AIFS"])
                + weights["ICON"]
                * (_d(icon_row.forecast_daily_max_c) + biases["ICON"])
            )
            truth = _d(aifs_row.truth_value_c)
            residual = truth - point
            train_truths.append(truth)
            residuals.append(residual)
            identity = {
                "event_date": aifs_row.event_date,
                "canonical_truth_fact_content_sha256": (
                    aifs_row.canonical_truth_fact_content_sha256
                ),
                "aifs_model_input_content_sha256": (
                    aifs_row.model_input_content_sha256
                ),
                "icon_model_input_content_sha256": (
                    icon_row.model_input_content_sha256
                ),
                "selected_calibrated_point_c": _q(point),
                "residual_c": _q(residual),
            }
            scenarios.append(
                {**identity, "scenario_content_sha256": content_sha256(identity)}
            )
        selected_holdout_crps: list[Decimal] = []
        climatology_holdout_crps: list[Decimal] = []
        for index in range(20, 28):
            aifs_row = aifs.cohort_rows[index]
            icon_row = icon.cohort_rows[index]
            if aifs_row.partition != "holdout" or icon_row.partition != "holdout":
                raise ContractError("republisher_probability_holdout_partition_mismatch")
            point = (
                weights["AIFS"]
                * (_d(aifs_row.forecast_daily_max_c) + biases["AIFS"])
                + weights["ICON"]
                * (_d(icon_row.forecast_daily_max_c) + biases["ICON"])
            )
            truth = _d(aifs_row.truth_value_c)
            selected_holdout_crps.append(
                _crps([point + residual for residual in residuals], truth)
            )
            climatology_holdout_crps.append(_crps(train_truths, truth))
    scenario_set_sha = content_sha256(scenarios)
    component_shas = [
        ref.artifact_content_sha256 for ref in selection.component_artifact_refs
    ]
    fit_identity = {
        "selection_content_sha256": selection.content_sha256,
        "component_artifact_content_sha256s": component_shas,
        "method_version": "train_residual_bootstrap_v1",
        "train_scenario_set_sha256": scenario_set_sha,
    }
    artifact_seed = {
        "station_id": selection.station_id,
        "selection_content_sha256": selection.content_sha256,
        "method_version": "train_residual_bootstrap_v1",
        "train_scenario_set_sha256": scenario_set_sha,
    }
    selected_crps = _mean(selected_holdout_crps)
    climatology_crps = _mean(climatology_holdout_crps)
    identity = {
        "artifact_id": "republisher-temperature-distribution-"
        + content_sha256(artifact_seed)[:40],
        "station_id": selection.station_id,
        "settlement_timezone": selection.settlement_timezone,
        "settlement_definition": selection.settlement_definition,
        "season_bin": selection.season_bin,
        "lead_bucket": selection.lead_bucket,
        "run_cycle": None,
        "run_cycle_scope": selection.run_cycle_scope,
        "selection_id": selection.selection_id,
        "selection_content_sha256": selection.content_sha256,
        "component_artifact_content_sha256s": component_shas,
        "method_version": "train_residual_bootstrap_v1",
        "train_n": 20,
        "holdout_n": 8,
        "train_scenarios": scenarios,
        "train_scenario_set_sha256": scenario_set_sha,
        "train_residual_mean_c": _q(_mean(residuals)),
        "train_residual_mae_c": _q(_mean([abs(item) for item in residuals])),
        "train_residual_rmse_c": _q(_rmse(residuals)),
        "holdout_case_set_sha256": selection.component_artifact_refs[0].holdout_case_set_sha256,
        "selected_holdout_mean_crps_c": _q(selected_crps),
        "climatology_holdout_mean_crps_c": _q(climatology_crps),
        "holdout_evaluation_passed": selected_crps < climatology_crps,
        "fit_content_sha256": content_sha256(fit_identity),
        "source_class": "aggregator_republisher",
        "transport_provider_id": "OPEN_METEO",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "state": "PRODUCTION_NO_LIVE_TRAIN_RESIDUAL_DISTRIBUTION_READY",
        "future_event_count": 0,
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "production_no_live_only": True,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_probability_distribution_contract(
    conn: sqlite3.Connection,
    *,
    selection_content_sha256s: Sequence[str],
    as_of_utc: str,
    builder_id: str = DISTRIBUTION_BUILDER_ID,
) -> dict[str, Any]:
    selection_shas = sorted(set(selection_content_sha256s))
    if not selection_shas:
        raise ContractError("republisher_probability_selection_scope_empty")
    facts = [
        _artifact_fact(conn, selection_sha=value, as_of_utc=as_of_utc)
        for value in selection_shas
    ]
    identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": str(as_of_utc),
        "builder_id": str(builder_id),
        "forward_applicability_facts": [],
        "probability_distribution_artifact_facts": facts,
        "p0_snapshot_facts": [],
    }
    packet = {**identity, "content_sha256": content_sha256(identity)}
    return CanonicalRepublisherNoLiveP0Contract.from_mapping(packet).to_mapping()


def _load_activation(
    conn: sqlite3.Connection, content_sha256_value: str
) -> CanonicalRepublisherNoLiveActivationFact:
    rows = conn.execute(
        "SELECT content_sha256,fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='republisher_no_live_activation' AND content_sha256=?",
        (content_sha256_value,),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError("republisher_p0_activation_missing")
    stored_sha, fact_json = rows[0]
    try:
        raw = json.loads(str(fact_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("republisher_p0_activation_json_invalid") from exc
    if not isinstance(raw, Mapping):
        raise ContractError("republisher_p0_activation_invalid")
    activation = CanonicalRepublisherNoLiveActivationFact.from_mapping(raw)
    if str(stored_sha) != activation.content_sha256:
        raise ContractError("republisher_p0_activation_hash_mismatch")
    return activation


def _load_v10_fact(
    conn: sqlite3.Connection,
    *,
    fact_type: str,
    content_sha256_value: str,
    parser: Any,
) -> Any:
    rows = conn.execute(
        "SELECT content_sha256,fact_json FROM canonical_ssot_fact "
        "WHERE fact_type=? AND content_sha256=?",
        (fact_type, content_sha256_value),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError(f"republisher_p0_{fact_type}_missing")
    stored_sha, fact_json = rows[0]
    try:
        raw = json.loads(str(fact_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"republisher_p0_{fact_type}_json_invalid") from exc
    if not isinstance(raw, Mapping):
        raise ContractError(f"republisher_p0_{fact_type}_invalid")
    fact = parser.from_mapping(raw)
    if str(stored_sha) != fact.content_sha256:
        raise ContractError(f"republisher_p0_{fact_type}_hash_mismatch")
    return fact


def build_distribution_activation_contract(
    conn: sqlite3.Connection,
    *,
    distribution_content_sha256s: Sequence[str],
    as_of_utc: str,
    builder_id: str = DISTRIBUTION_ACTIVATION_BUILDER_ID,
) -> dict[str, Any]:
    facts: list[dict[str, Any]] = []
    for distribution_sha in sorted(set(distribution_content_sha256s)):
        distribution = _load_v10_fact(
            conn,
            fact_type="probability_distribution_artifact",
            content_sha256_value=distribution_sha,
            parser=CanonicalProbabilityDistributionArtifactFact,
        )
        selection, _ = load_persisted_republisher_cross_selection(
            conn, distribution.raw["selection_content_sha256"]
        )
        root = "|".join(
            (
                selection.station_id,
                selection.settlement_timezone,
                selection.settlement_definition,
                selection.season_bin,
                selection.lead_bucket,
                "null",
                selection.run_cycle_scope,
            )
        )
        chain = _load_activation_chain(conn, root)
        if (
            len(chain) == 2
            and chain[-1].temperature_distribution_artifact_content_sha256
            == distribution.content_sha256
        ):
            facts.append(chain[-1].to_mapping())
            continue
        if (
            len(chain) != 1
            or chain[0].temperature_distribution_artifact_content_sha256 is not None
        ):
            raise ContractError("republisher_distribution_activation_gen1_head_required")
        predecessor = chain[0].content_sha256
        generation = 2
        activation_identity = {
            "root": {
                "station_id": selection.station_id,
                "settlement_timezone": selection.settlement_timezone,
                "settlement_definition": selection.settlement_definition,
                "season_bin": selection.season_bin,
                "lead_bucket": selection.lead_bucket,
                "run_cycle": None,
                "run_cycle_scope": selection.run_cycle_scope,
            },
            "activation_generation": generation,
            "selection_content_sha256": selection.content_sha256,
            "predecessor_activation_content_sha256": predecessor,
            "temperature_distribution_artifact_content_sha256": distribution.content_sha256,
        }
        identity = {
            "activation_id": "republisher-no-live-activation-"
            + content_sha256(activation_identity)[:40],
            **activation_identity["root"],
            "source_class": "aggregator_republisher",
            "transport_provider_id": "OPEN_METEO",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "model_family": "cross_global_deterministic",
            "selection_id": selection.selection_id,
            "selection_content_sha256": selection.content_sha256,
            "temperature_distribution_artifact_content_sha256": distribution.content_sha256,
            "activation_generation": generation,
            "predecessor_activation_content_sha256": predecessor,
            "activated_at_utc": str(as_of_utc),
            "state": "PRODUCTION_NO_LIVE_REPUBLISHER_DISTRIBUTION_ACTIVATION_READY",
            "future_event_count": 0,
            "native_arrival_indexed": False,
            "p1_eligible": False,
            "formal_live_authority": False,
            "production_no_live_only": True,
        }
        facts.append({**identity, "content_sha256": content_sha256(identity)})
    identity = {
        "schema": REPUBLISHER_NO_LIVE_ACTIVATION_CONTRACT_SCHEMA,
        "as_of_utc": str(as_of_utc),
        "builder_id": str(builder_id),
        "activation_facts": facts,
    }
    packet = {**identity, "content_sha256": content_sha256(identity)}
    return CanonicalRepublisherNoLiveActivationContract.from_mapping(packet).to_mapping()


def build_forward_applicability_contract(
    conn: sqlite3.Connection,
    *,
    activation_content_sha256: str,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
    component_receipts: Sequence[Mapping[str, Any]],
    native_unavailability_receipts: Sequence[Mapping[str, Any]],
    evidence_manifest_content_sha256: str,
    evidence_manifest_content_address: str,
    fresh_until_utc: str,
    native_evidence_expires_at_utc: str,
    as_of_utc: str,
    builder_id: str = "republisher_forward_applicability_builder_v1",
) -> dict[str, Any]:
    activation = _load_activation(conn, activation_content_sha256)
    chain = _load_activation_chain(conn, activation.activation_root)
    if not chain or chain[-1].content_sha256 != activation.content_sha256:
        raise ContractError("republisher_forward_activation_not_current")
    station = str(station_id).strip().upper()
    if station != activation.station_id:
        raise ContractError("republisher_forward_station_mismatch")
    components = [dict(item) for item in component_receipts]
    native = [dict(item) for item in native_unavailability_receipts]
    component_set_sha = content_sha256(components)
    native_set_sha = content_sha256(native)
    seed = {
        "station_id": station,
        "event_date": str(event_date),
        "decision_at_utc": str(decision_at_utc),
        "lead_bucket": activation.lead_bucket,
        "activation_content_sha256": activation.content_sha256,
        "component_receipt_set_sha256": component_set_sha,
        "native_unavailability_receipt_set_sha256": native_set_sha,
    }
    identity = {
        "applicability_id": "forward-applicability-" + content_sha256(seed)[:40],
        "station_id": station,
        "event_date": str(event_date),
        "settlement_timezone": activation.settlement_timezone,
        "settlement_definition": activation.settlement_definition,
        "season_bin": activation.season_bin,
        "lead_bucket": activation.lead_bucket,
        "run_cycle": None,
        "run_cycle_scope": activation.run_cycle_scope,
        "decision_at_utc": str(decision_at_utc),
        "activation_id": activation.activation_id,
        "activation_generation": activation.activation_generation,
        "activation_content_sha256": activation.content_sha256,
        "selection_content_sha256": activation.selection_content_sha256,
        "component_receipts": components,
        "component_receipt_set_sha256": component_set_sha,
        "native_unavailability_receipts": native,
        "native_unavailability_receipt_set_sha256": native_set_sha,
        "evidence_manifest_content_sha256": str(evidence_manifest_content_sha256),
        "evidence_manifest_content_address": str(evidence_manifest_content_address),
        "source_class": "aggregator_republisher",
        "transport_provider_id": "OPEN_METEO",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "evidence_role": "observational_candidate_not_oos",
        "strict_oos_sample_eligible": False,
        "fresh_until_utc": str(fresh_until_utc),
        "native_evidence_expires_at_utc": str(native_evidence_expires_at_utc),
        "state": "PRODUCTION_NO_LIVE_FORWARD_APPLICABILITY_READY",
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "production_no_live_only": True,
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    contract_identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": str(as_of_utc),
        "builder_id": str(builder_id),
        "forward_applicability_facts": [fact],
        "probability_distribution_artifact_facts": [],
        "p0_snapshot_facts": [],
    }
    packet = {
        **contract_identity,
        "content_sha256": content_sha256(contract_identity),
    }
    return CanonicalRepublisherNoLiveP0Contract.from_mapping(packet).to_mapping()


def _scenario_bucket(
    value: Decimal, intervals: Sequence[Mapping[str, Any]]
) -> int:
    matches: list[int] = []
    for index, interval in enumerate(intervals):
        lower = interval.get("lower_c")
        upper = interval.get("upper_c")
        if (lower is None or value >= _d(lower)) and (
            upper is None or value < _d(upper)
        ):
            matches.append(index)
    if len(matches) != 1:
        raise ContractError("republisher_p0_scenario_bucket_ambiguous")
    return matches[0]


def build_p0_snapshot_contract(
    conn: sqlite3.Connection,
    *,
    forward_applicability_content_sha256: str,
    bucket_intervals: Sequence[Mapping[str, Any]],
    market_bucket_bridge_sha256: str,
    materialized_at_utc: str,
    as_of_utc: str,
    builder_id: str = "republisher_no_live_p0_snapshot_builder_v1",
) -> dict[str, Any]:
    bridge_rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='market_bucket_bridge' AND content_sha256=?",
        (str(market_bucket_bridge_sha256),),
    ).fetchall()
    if len(bridge_rows) != 1:
        raise ContractError("republisher_p0_market_bucket_bridge_missing")
    try:
        bridge = CanonicalMarketBucketBridgeFact.from_mapping(
            json.loads(str(bridge_rows[0][0]))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("republisher_p0_market_bucket_bridge_json_invalid") from exc
    applicability = _load_v10_fact(
        conn,
        fact_type="forward_applicability",
        content_sha256_value=forward_applicability_content_sha256,
        parser=CanonicalForwardApplicabilityFact,
    )
    if (
        bridge.raw["station_id"] != applicability.raw["station_id"]
        or bridge.raw["event_date"] != applicability.raw["event_date"]
        or bridge.raw["settlement_timezone"]
        != applicability.raw["settlement_timezone"]
    ):
        raise ContractError("republisher_p0_market_bucket_bridge_scope_mismatch")
    activation = _load_activation(
        conn, applicability.raw["activation_content_sha256"]
    )
    distribution_sha = activation.temperature_distribution_artifact_content_sha256
    if activation.activation_generation < 2 or distribution_sha is None:
        raise ContractError("republisher_p0_distribution_activation_required")
    distribution = _load_v10_fact(
        conn,
        fact_type="probability_distribution_artifact",
        content_sha256_value=distribution_sha,
        parser=CanonicalProbabilityDistributionArtifactFact,
    )
    selection, aifs, icon = _selection_artifacts(
        conn, activation.selection_content_sha256
    )
    components = {
        item["upstream_model_id"]: item
        for item in applicability.raw["component_receipts"]
    }
    weights = {key: _d(value) for key, value in selection.selected_weights.items()}
    biases = {
        "AIFS": _d(aifs.calibration_parameters["bias_c"]),
        "ICON": _d(icon.calibration_parameters["bias_c"]),
    }
    with localcontext() as context:
        context.prec = 50
        forward_point = sum(
            weights[key]
            * (_d(components[key]["forecast_daily_max_c"]) + biases[key])
            for key in ("AIFS", "ICON")
        )
        scenario_temperatures = [
            forward_point + _d(item["residual_c"])
            for item in distribution.raw["train_scenarios"]
        ]
    frozen_intervals = [dict(item) for item in bucket_intervals]
    bridge_intervals = [
        {
            "bucket_id": item["outcome"],
            "ordinal": item["ordinal"],
            "lower_c": item["lower_c"],
            "upper_c": item["upper_c"],
        }
        for item in bridge.raw["bucket_contracts"]
    ]
    if frozen_intervals != bridge_intervals:
        raise ContractError("republisher_p0_market_bucket_bridge_interval_mismatch")
    bucket_contract_sha = content_sha256(frozen_intervals)
    counts = [0 for _ in frozen_intervals]
    for temperature in scenario_temperatures:
        counts[_scenario_bucket(temperature, frozen_intervals)] += 1
    probabilities = [
        {
            "bucket_id": str(interval["bucket_id"]),
            "ordinal": int(interval["ordinal"]),
            "scenario_count": counts[index],
            "probability": _q(Decimal(counts[index]) / Decimal(20)),
        }
        for index, interval in enumerate(frozen_intervals)
    ]
    scenario_set_sha = content_sha256([_q(value) for value in scenario_temperatures])
    probability_root = content_sha256(
        {
            "bucket_contract_sha256": bucket_contract_sha,
            "market_bucket_bridge_sha256": str(market_bucket_bridge_sha256),
            "bucket_intervals": frozen_intervals,
            "bucket_probabilities": probabilities,
        }
    )
    posterior_identity = {
        "selection_content_sha256": activation.selection_content_sha256,
        "forward_applicability_content_sha256": applicability.content_sha256,
        "probability_distribution_artifact_content_sha256": distribution.content_sha256,
        "selected_forward_point_c": _q(forward_point),
        "scenario_temperature_set_sha256": scenario_set_sha,
        "probability_root_sha256": probability_root,
    }
    snapshot_seed = {
        "station_id": activation.station_id,
        "event_date": applicability.raw["event_date"],
        "decision_at_utc": applicability.raw["decision_at_utc"],
        "activation_content_sha256": activation.content_sha256,
        "forward_applicability_content_sha256": applicability.content_sha256,
        "probability_distribution_artifact_content_sha256": distribution.content_sha256,
        "bucket_contract_sha256": bucket_contract_sha,
    }
    identity = {
        "snapshot_id": "republisher-no-live-p0-" + content_sha256(snapshot_seed)[:40],
        "station_id": activation.station_id,
        "event_date": applicability.raw["event_date"],
        "settlement_timezone": activation.settlement_timezone,
        "settlement_definition": activation.settlement_definition,
        "season_bin": activation.season_bin,
        "lead_bucket": activation.lead_bucket,
        "run_cycle": None,
        "run_cycle_scope": activation.run_cycle_scope,
        "decision_at_utc": applicability.raw["decision_at_utc"],
        "materialized_at_utc": str(materialized_at_utc),
        "fresh_until_utc": applicability.raw["fresh_until_utc"],
        "activation_id": activation.activation_id,
        "activation_generation": activation.activation_generation,
        "activation_content_sha256": activation.content_sha256,
        "selection_content_sha256": activation.selection_content_sha256,
        "forward_applicability_content_sha256": applicability.content_sha256,
        "probability_distribution_artifact_content_sha256": distribution.content_sha256,
        "bucket_contract_sha256": bucket_contract_sha,
        "market_bucket_bridge_sha256": str(market_bucket_bridge_sha256),
        "bucket_intervals": frozen_intervals,
        "selected_forward_point_c": _q(forward_point),
        "scenario_temperature_set_sha256": scenario_set_sha,
        "bucket_probabilities": probabilities,
        "probability_root_sha256": probability_root,
        "posterior_version": "REPUBLISHER_NO_LIVE_TRAIN_RESIDUAL_BOOTSTRAP_V1",
        "posterior_sha256": content_sha256(posterior_identity),
        "stage": "P0",
        "source_class": "aggregator_republisher",
        "transport_provider_id": "OPEN_METEO",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "evidence_role": "observational_candidate_not_oos",
        "strict_oos_sample_eligible": False,
        "state": "PRODUCTION_NO_LIVE_REPUBLISHER_P0_READY",
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "production_no_live_only": True,
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    contract_identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": str(as_of_utc),
        "builder_id": str(builder_id),
        "forward_applicability_facts": [],
        "probability_distribution_artifact_facts": [],
        "p0_snapshot_facts": [fact],
    }
    packet = {
        **contract_identity,
        "content_sha256": content_sha256(contract_identity),
    }
    return CanonicalRepublisherNoLiveP0Contract.from_mapping(packet).to_mapping()


def _assert_scope_matches(left: Mapping[str, Any], right: Mapping[str, Any]) -> None:
    for key in (
        "station_id",
        "settlement_timezone",
        "settlement_definition",
        "season_bin",
        "lead_bucket",
        "run_cycle",
        "run_cycle_scope",
        "selection_content_sha256",
    ):
        if left.get(key) != right.get(key):
            raise ContractError(f"republisher_p0_scope_mismatch:{key}")


def assert_republisher_no_live_p0_references(
    conn: sqlite3.Connection,
    contract: CanonicalRepublisherNoLiveP0Contract,
) -> None:
    for distribution in contract.probability_distribution_artifact_facts:
        rebuilt = _artifact_fact(
            conn,
            selection_sha=distribution.raw["selection_content_sha256"],
            as_of_utc=contract.as_of_utc,
        )
        if rebuilt != distribution.to_mapping():
            raise ContractError("republisher_probability_distribution_not_reproducible")
    for applicability in contract.forward_applicability_facts:
        activation = _load_activation(
            conn, applicability.raw["activation_content_sha256"]
        )
        if (
            activation.activation_id != applicability.raw["activation_id"]
            or activation.activation_generation
            != applicability.raw["activation_generation"]
            or activation.selection_content_sha256
            != applicability.raw["selection_content_sha256"]
        ):
            raise ContractError("republisher_forward_activation_mismatch")
        _assert_scope_matches(activation.to_mapping(), applicability.raw)
        chain = _load_activation_chain(conn, activation.activation_root)
        if not chain or chain[-1].content_sha256 != activation.content_sha256:
            raise ContractError("republisher_forward_activation_not_current")
        selection, _ = load_persisted_republisher_cross_selection(
            conn, activation.selection_content_sha256
        )
        assert_republisher_cross_selection_fact_references(
            conn, selection, as_of_utc=contract.as_of_utc
        )
    for snapshot in contract.p0_snapshot_facts:
        activation = _load_activation(
            conn, snapshot.raw["activation_content_sha256"]
        )
        applicability = _load_v10_fact(
            conn,
            fact_type="forward_applicability",
            content_sha256_value=snapshot.raw[
                "forward_applicability_content_sha256"
            ],
            parser=CanonicalForwardApplicabilityFact,
        )
        distribution = _load_v10_fact(
            conn,
            fact_type="probability_distribution_artifact",
            content_sha256_value=snapshot.raw[
                "probability_distribution_artifact_content_sha256"
            ],
            parser=CanonicalProbabilityDistributionArtifactFact,
        )
        bridge = _load_v10_fact(
            conn,
            fact_type="market_bucket_bridge",
            content_sha256_value=snapshot.raw["market_bucket_bridge_sha256"],
            parser=CanonicalMarketBucketBridgeFact,
        )
        bridge_intervals = [
            {
                "bucket_id": item["outcome"],
                "ordinal": item["ordinal"],
                "lower_c": item["lower_c"],
                "upper_c": item["upper_c"],
            }
            for item in bridge.raw["bucket_contracts"]
        ]
        if (
            bridge.raw["station_id"] != snapshot.raw["station_id"]
            or bridge.raw["event_date"] != snapshot.raw["event_date"]
            or bridge.raw["settlement_timezone"]
            != snapshot.raw["settlement_timezone"]
            or bridge.raw["settlement_definition"]
            != snapshot.raw["settlement_definition"]
            or bridge_intervals != snapshot.raw["bucket_intervals"]
            or content_sha256(bridge_intervals)
            != snapshot.raw["bucket_contract_sha256"]
        ):
            raise ContractError("republisher_p0_market_bucket_bridge_binding_mismatch")
        if (
            activation.temperature_distribution_artifact_content_sha256
            != distribution.content_sha256
            or activation.activation_generation < 2
            or snapshot.raw["activation_id"] != activation.activation_id
            or snapshot.raw["activation_generation"]
            != activation.activation_generation
            or snapshot.raw["selection_content_sha256"]
            != activation.selection_content_sha256
            or snapshot.raw["decision_at_utc"]
            != applicability.raw["decision_at_utc"]
            or snapshot.raw["event_date"] != applicability.raw["event_date"]
            or snapshot.raw["fresh_until_utc"]
            != applicability.raw["fresh_until_utc"]
        ):
            raise ContractError("republisher_p0_parent_binding_mismatch")
        _assert_scope_matches(snapshot.raw, applicability.raw)
        _assert_scope_matches(snapshot.raw, distribution.raw)
        chain = _load_activation_chain(conn, activation.activation_root)
        if not chain or chain[-1].content_sha256 != activation.content_sha256:
            raise ContractError("republisher_p0_activation_not_current")


__all__ = [
    "DISTRIBUTION_ACTIVATION_BUILDER_ID",
    "DISTRIBUTION_BUILDER_ID",
    "QUANTUM",
    "assert_republisher_no_live_p0_references",
    "build_forward_applicability_contract",
    "build_p0_snapshot_contract",
    "build_distribution_activation_contract",
    "build_probability_distribution_contract",
]
