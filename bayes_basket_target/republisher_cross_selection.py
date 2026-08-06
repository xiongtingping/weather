"""Build strict no-live cross-selections from persisted republisher artifacts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, localcontext
from typing import Any

from .canonical_contract import (
    CanonicalRepublisherCrossSelectionContract,
    CanonicalRepublisherCrossSelectionFact,
    CanonicalRepublisherOOSArtifactFact,
    CanonicalTruthFact,
    REPUBLISHER_CROSS_SELECTION_CONTRACT_SCHEMA,
)
from .contracts import ContractError, content_sha256


SELECTION_METHOD_VERSION = "inverse_calibrated_train_rmse_floor_v1"
WEIGHT_RMSE_FLOOR_C = Decimal("0.100000000000")
QUANTUM = Decimal("0.000000000001")
PRODUCT_ORDER = ("AIFS", "ICON")


def _d(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ContractError("republisher_cross_selection_numeric_input_invalid")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ContractError("republisher_cross_selection_numeric_input_invalid") from exc
    if not result.is_finite():
        raise ContractError("republisher_cross_selection_numeric_input_invalid")
    return result


def _q(value: Decimal) -> str:
    if not value.is_finite():
        raise ContractError("republisher_cross_selection_numeric_result_invalid")
    result = value.quantize(QUANTUM)
    if result == 0:
        result = abs(result)
    return format(result, ".12f")


def _rmse(errors: Sequence[Decimal]) -> Decimal:
    if not errors:
        raise ContractError("republisher_cross_selection_metric_rows_empty")
    with localcontext() as context:
        context.prec = 50
        return (sum(value * value for value in errors) / Decimal(len(errors))).sqrt()


def _metrics(errors: Sequence[Decimal]) -> dict[str, str]:
    if not errors:
        raise ContractError("republisher_cross_selection_metric_rows_empty")
    size = Decimal(len(errors))
    return {
        "mae_c": _q(sum(abs(value) for value in errors) / size),
        "mean_error_c": _q(sum(errors) / size),
        "rmse_c": _q(_rmse(errors)),
    }


def _load_artifacts(
    conn: sqlite3.Connection, station_id: str
) -> tuple[CanonicalRepublisherOOSArtifactFact, ...]:
    facts: list[CanonicalRepublisherOOSArtifactFact] = []
    for (fact_json,) in conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='republisher_oos_artifact' ORDER BY fact_id"
    ):
        try:
            raw = json.loads(str(fact_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "republisher_cross_selection_parent_json_invalid"
            ) from exc
        if not isinstance(raw, Mapping):
            raise ContractError(
                "republisher_cross_selection_parent_mapping_required"
            )
        fact = CanonicalRepublisherOOSArtifactFact.from_mapping(raw)
        if fact.station_id == station_id:
            facts.append(fact)
    facts.sort(key=lambda item: item.upstream_model_id)
    if [item.upstream_model_id for item in facts] != list(PRODUCT_ORDER):
        raise ContractError(
            "republisher_cross_selection_exact_parent_set_required"
        )
    return tuple(facts)


def _truth_ref(row: Any) -> dict[str, Any]:
    return {
        "event_date": row.event_date,
        "canonical_truth_fact_content_sha256": row.canonical_truth_fact_content_sha256,
        "awc_truth_id": row.awc_truth_id,
        "awc_truth_revision_no": row.awc_truth_revision_no,
        "awc_truth_content_sha256": row.awc_truth_content_sha256,
        "awc_truth_asof_utc": row.awc_truth_asof_utc,
        "station_day_end_utc": row.station_day_end_utc,
    }


def _component_ref(fact: CanonicalRepublisherOOSArtifactFact) -> dict[str, Any]:
    return {
        "upstream_model_id": fact.upstream_model_id,
        "republisher_product_id": fact.republisher_product_id,
        "independence_family_id": fact.independence_family_id,
        "artifact_id": fact.artifact_id,
        "artifact_content_sha256": fact.content_sha256,
        "train_case_set_sha256": fact.train_case_set_sha256,
        "holdout_case_set_sha256": fact.holdout_case_set_sha256,
        "strict_oos_cohort_sha256": fact.strict_oos_cohort_sha256,
        "evaluation_content_sha256": fact.evaluation_content_sha256,
        "run_cycle": None,
        "run_cycle_scope": fact.run_cycle_scope,
    }


def _selection_fact(
    artifacts: tuple[CanonicalRepublisherOOSArtifactFact, ...]
) -> dict[str, Any]:
    aifs, icon = artifacts
    shared_fields = (
        "station_id",
        "settlement_timezone",
        "settlement_definition",
        "season_bin",
        "lead_bucket",
        "run_cycle",
        "run_cycle_scope",
        "source_class",
        "transport_provider_id",
        "model_family",
        "authority_scope",
        "artifact_method_version",
        "oos_protocol_version",
        "train_n",
        "holdout_n",
        "state",
        "future_event_count",
        "native_arrival_indexed",
        "p1_eligible",
        "formal_live_authority",
    )
    for field in shared_fields:
        if getattr(aifs, field) != getattr(icon, field):
            raise ContractError(
                f"republisher_cross_selection_parent_scope_mismatch:{field}"
            )
    if (
        aifs.run_cycle is not None
        or aifs.train_n != 20
        or aifs.holdout_n != 8
        or aifs.source_class != "aggregator_republisher"
        or aifs.authority_scope != "PRODUCTION_NO_LIVE_ONLY"
        or aifs.future_event_count != 0
        or aifs.native_arrival_indexed
        or aifs.p1_eligible
        or aifs.formal_live_authority
    ):
        raise ContractError("republisher_cross_selection_parent_authority_invalid")
    if aifs.independence_family_id == icon.independence_family_id:
        raise ContractError("republisher_cross_selection_family_not_independent")

    for index, (left, right) in enumerate(zip(aifs.cohort_rows, icon.cohort_rows)):
        if (
            left.event_date,
            left.partition,
            left.truth_value_c,
            left.canonical_truth_fact_content_sha256,
            left.awc_truth_id,
            left.awc_truth_revision_no,
            left.awc_truth_content_sha256,
            left.awc_truth_asof_utc,
            left.station_day_end_utc,
        ) != (
            right.event_date,
            right.partition,
            right.truth_value_c,
            right.canonical_truth_fact_content_sha256,
            right.awc_truth_id,
            right.awc_truth_revision_no,
            right.awc_truth_content_sha256,
            right.awc_truth_asof_utc,
            right.station_day_end_utc,
        ):
            raise ContractError(
                f"republisher_cross_selection_truth_vector_mismatch:{index}"
            )

    calibrated_rows: dict[str, list[Decimal]] = {}
    train_rmse_exact: dict[str, Decimal] = {}
    for artifact in artifacts:
        bias = _d(artifact.calibration_parameters["bias_c"])
        calibrated = [
            _d(row.forecast_daily_max_c) + bias for row in artifact.cohort_rows
        ]
        calibrated_rows[artifact.upstream_model_id] = calibrated
        train_errors = [
            calibrated[index] - _d(artifact.cohort_rows[index].truth_value_c)
            for index in range(20)
        ]
        train_rmse_exact[artifact.upstream_model_id] = _rmse(train_errors)
    with localcontext() as context:
        context.prec = 50
        raw_weights = {
            key: Decimal(1) / max(value, WEIGHT_RMSE_FLOOR_C)
            for key, value in train_rmse_exact.items()
        }
        denominator = sum(raw_weights.values())
        aifs_weight = (raw_weights["AIFS"] / denominator).quantize(QUANTUM)
        icon_weight = Decimal("1.000000000000") - aifs_weight
    if aifs_weight <= 0 or icon_weight <= 0:
        raise ContractError("republisher_cross_selection_weight_result_invalid")
    weights = {"AIFS": aifs_weight, "ICON": icon_weight}

    selected_errors: list[Decimal] = []
    uniform_errors: list[Decimal] = []
    truth_values = [_d(row.truth_value_c) for row in aifs.cohort_rows]
    for index in range(20, 28):
        selected = sum(
            weights[key] * calibrated_rows[key][index] for key in PRODUCT_ORDER
        )
        uniform = sum(calibrated_rows[key][index] for key in PRODUCT_ORDER) / Decimal(2)
        selected_errors.append(selected - truth_values[index])
        uniform_errors.append(uniform - truth_values[index])
    climatology = _d(aifs.calibration_parameters["train_climatology_tmax_c"])
    if climatology != _d(icon.calibration_parameters["train_climatology_tmax_c"]):
        raise ContractError("republisher_cross_selection_climatology_mismatch")
    climatology_errors = [climatology - truth_values[index] for index in range(20, 28)]

    truth_refs = [_truth_ref(row) for row in aifs.cohort_rows[20:]]
    truth_set_sha = content_sha256(truth_refs)
    component_refs = [_component_ref(item) for item in artifacts]
    natural_identity = {
        "station_id": aifs.station_id,
        "settlement_timezone": aifs.settlement_timezone,
        "settlement_definition": aifs.settlement_definition,
        "season_bin": aifs.season_bin,
        "lead_bucket": aifs.lead_bucket,
        "run_cycle": None,
        "run_cycle_scope": aifs.run_cycle_scope,
        "component_artifact_content_sha256s": [
            item.content_sha256 for item in artifacts
        ],
        "holdout_truth_ref_set_sha256": truth_set_sha,
        "selection_method_version": SELECTION_METHOD_VERSION,
        "weight_rmse_floor_c": _q(WEIGHT_RMSE_FLOOR_C),
    }
    selection_id = (
        "republisher-cross-selection-"
        + content_sha256(natural_identity)[:40]
    )
    identity = {
        "selection_id": selection_id,
        "station_id": aifs.station_id,
        "settlement_timezone": aifs.settlement_timezone,
        "settlement_definition": aifs.settlement_definition,
        "season_bin": aifs.season_bin,
        "lead_bucket": aifs.lead_bucket,
        "run_cycle": None,
        "run_cycle_scope": aifs.run_cycle_scope,
        "source_class": "aggregator_republisher",
        "transport_provider_id": "OPEN_METEO",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "model_family": "cross_global_deterministic",
        "selection_method_version": SELECTION_METHOD_VERSION,
        "weight_rmse_floor_c": _q(WEIGHT_RMSE_FLOOR_C),
        "component_artifact_refs": component_refs,
        "holdout_truth_refs": truth_refs,
        "holdout_truth_ref_set_sha256": truth_set_sha,
        "train_calibrated_rmse_c": {
            key: _q(train_rmse_exact[key]) for key in PRODUCT_ORDER
        },
        "selected_weights": {key: _q(weights[key]) for key in PRODUCT_ORDER},
        "selected_holdout_metrics": _metrics(selected_errors),
        "uniform_holdout_metrics": _metrics(uniform_errors),
        "climatology_holdout_metrics": _metrics(climatology_errors),
        "state": "PRODUCTION_NO_LIVE_CROSS_SELECTION_READY",
        "future_event_count": 0,
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "production_no_live_only": True,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_republisher_cross_selection_contract(
    conn: sqlite3.Connection,
    *,
    station_ids: Iterable[str],
    as_of_utc: str,
    builder_id: str,
) -> dict[str, Any]:
    stations = sorted({str(value).strip().upper() for value in station_ids})
    if not stations or any(not value for value in stations):
        raise ContractError("republisher_cross_selection_station_scope_empty")
    facts = [_selection_fact(_load_artifacts(conn, station)) for station in stations]
    identity = {
        "schema": REPUBLISHER_CROSS_SELECTION_CONTRACT_SCHEMA,
        "as_of_utc": str(as_of_utc),
        "builder_id": str(builder_id).strip(),
        "selection_facts": facts,
    }
    packet = {**identity, "content_sha256": content_sha256(identity)}
    return CanonicalRepublisherCrossSelectionContract.from_mapping(packet).to_mapping()


def assert_republisher_cross_selection_references(
    conn: sqlite3.Connection,
    contract: CanonicalRepublisherCrossSelectionContract,
) -> None:
    rebuilt = build_republisher_cross_selection_contract(
        conn,
        station_ids=[item.station_id for item in contract.selection_facts],
        as_of_utc=contract.as_of_utc,
        builder_id=contract.builder_id,
    )
    if rebuilt != contract.to_mapping():
        raise ContractError("republisher_cross_selection_not_reproducible")
    for selection in contract.selection_facts:
        assert_republisher_cross_selection_fact_references(
            conn, selection, as_of_utc=contract.as_of_utc
        )


def load_persisted_republisher_cross_selection(
    conn: sqlite3.Connection, content_sha256_value: str
) -> tuple[CanonicalRepublisherCrossSelectionFact, str]:
    rows = conn.execute(
        "SELECT content_sha256,fact_json,ingested_at_utc FROM canonical_ssot_fact "
        "WHERE fact_type='republisher_cross_selection' AND content_sha256=?",
        (content_sha256_value,),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError("republisher_cross_selection_persisted_fact_missing")
    stored_sha, fact_json, ingested_at_utc = rows[0]
    try:
        raw = json.loads(str(fact_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("republisher_cross_selection_persisted_json_invalid") from exc
    if not isinstance(raw, Mapping):
        raise ContractError("republisher_cross_selection_persisted_mapping_required")
    fact = CanonicalRepublisherCrossSelectionFact.from_mapping(raw)
    if str(stored_sha) != fact.content_sha256:
        raise ContractError("republisher_cross_selection_persisted_hash_mismatch")
    return fact, str(ingested_at_utc)


def assert_republisher_cross_selection_fact_references(
    conn: sqlite3.Connection,
    selection: CanonicalRepublisherCrossSelectionFact,
    *,
    as_of_utc: str,
) -> None:
    rebuilt = _selection_fact(_load_artifacts(conn, selection.station_id))
    if rebuilt != selection.to_mapping():
        raise ContractError("republisher_cross_selection_not_reproducible")
    for truth_ref in selection.holdout_truth_refs:
        row = conn.execute(
            "SELECT fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='truth' AND content_sha256=?",
            (truth_ref.canonical_truth_fact_content_sha256,),
        ).fetchone()
        if row is None:
            raise ContractError("republisher_cross_selection_truth_fact_missing")
        raw = json.loads(str(row[0]))
        if not isinstance(raw, Mapping):
            raise ContractError("republisher_cross_selection_truth_fact_invalid")
        truth = CanonicalTruthFact.from_mapping(raw)
        if (
            truth.station_id != selection.station_id
            or truth.event_date != truth_ref.event_date
            or truth.truth_id != truth_ref.awc_truth_id
            or truth.truth_revision_no != truth_ref.awc_truth_revision_no
            or truth.truth_content_sha256 != truth_ref.awc_truth_content_sha256
            or truth.canonical_committed_at_utc != truth_ref.awc_truth_asof_utc
            or truth.station_day_end_utc != truth_ref.station_day_end_utc
            or truth.settlement_timezone != selection.settlement_timezone
            or truth.source_lane != "AWC"
            or truth.closed_day is not True
            or truth.canonical_committed_at_utc > as_of_utc
        ):
            raise ContractError("republisher_cross_selection_truth_lineage_mismatch")
        latest = max(
            (
                CanonicalTruthFact.from_mapping(json.loads(str(candidate_json))).truth_revision_no
                for (candidate_json,) in conn.execute(
                    "SELECT fact_json FROM canonical_ssot_fact "
                    "WHERE fact_type='truth' AND scope_key LIKE ?",
                    (f"{selection.station_id}|{truth_ref.event_date}|%",),
                )
            ),
            default=-1,
        )
        if latest != truth_ref.awc_truth_revision_no:
            raise ContractError("republisher_cross_selection_truth_revision_stale")


__all__ = [
    "PRODUCT_ORDER",
    "QUANTUM",
    "SELECTION_METHOD_VERSION",
    "WEIGHT_RMSE_FLOOR_C",
    "assert_republisher_cross_selection_references",
    "assert_republisher_cross_selection_fact_references",
    "build_republisher_cross_selection_contract",
    "load_persisted_republisher_cross_selection",
]
