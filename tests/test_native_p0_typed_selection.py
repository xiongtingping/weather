"""Formal P0 must consume only typed native ensemble OOS references."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.model_oos_selection import (
    build_historical_holdout_access_receipt,
    build_historical_holdout_reservation,
    build_model_oos_selection_contract,
)
from bayes_basket_target.native_ensemble_oos_contract import (
    CALIBRATION_METHOD_VERSION,
    COHORT_FACT_TYPE,
    ENSEMBLE_REDUCER_VERSION,
    EVENT_FACT_TYPE,
    EVENT_OOS_PROTOCOL_VERSION,
    NATIVE_ENSEMBLE_SYSTEM_REGISTRY,
    CanonicalNativeEnsembleOOSCohortFact,
    CanonicalNativeEnsembleOOSEventFact,
    derive_native_ensemble_cohort_statistics,
)
from bayes_basket_target.p0_calibrated_distribution import (
    CanonicalP0CalibratedDistributionContract,
    build_p0_calibrated_distribution_contract,
)
from bayes_basket_target.p0_activation_root import (
    CanonicalP0ActivationRootContract,
    assert_p0_activation_root_rebuild,
    build_p0_activation_root_contract,
)


AS_OF = "2026-08-06T00:00:00.000Z"
SYSTEMS = (
    (
        "ECMWF_IFS_ENS_OPEN_PF50",
        "ECMWF",
        "ECMWF",
        "ecmwf_ifs_ens_open_pf50",
        "ECMWF",
    ),
    ("NOAA_GEFS_05", "GFS", "NOAA", "noaa_gefs_0p5", "NOAA"),
)


def _seal(identity: dict[str, object]) -> dict[str, object]:
    return {**identity, "content_sha256": content_sha256(identity)}


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE canonical_ssot_fact (fact_type TEXT, content_sha256 TEXT, fact_json TEXT)"
    )
    return conn


def _store(conn: sqlite3.Connection, fact_type: str, fact: dict[str, object]) -> None:
    conn.execute(
        "INSERT INTO canonical_ssot_fact VALUES (?, ?, ?)",
        (fact_type, fact["content_sha256"], json.dumps(fact, sort_keys=True)),
    )


def _hash(*parts: object) -> str:
    return content_sha256(list(parts))


def _adapter_input(system_id: str, ordinal: int) -> str:
    return _hash("adapter-event-input", system_id, ordinal)


def _input_commitments(system: tuple[str, str, str, str, str]) -> dict[str, object]:
    system_id, model_id, provider_id, provider_product_id, family = system
    return {
        "component_identity": {
            "model_id": model_id,
            "ensemble_system_id": system_id,
            "model_family": "global_ensemble",
            "provider_id": provider_id,
            "provider_product_id": provider_product_id,
            "independence_family_id": family,
            "source_class": "native_official",
        },
        "event_fact_type": EVENT_FACT_TYPE,
        "event_input_commitments": [{
            "event_date": (date(2026, 7, 2) + timedelta(days=ordinal)).isoformat(),
            "adapter_event_input_content_sha256": _adapter_input(system_id, ordinal),
        } for ordinal in range(28)],
    }


def _event(
    *,
    system: tuple[str, str, str, str, str],
    ordinal: int,
    rejected_component: bool,
    opposed_calibration: bool = False,
) -> dict[str, object]:
    system_id, model_id, provider_id, provider_product_id, family = system
    registry = NATIVE_ENSEMBLE_SYSTEM_REGISTRY[system_id]
    event_day = date(2026, 7, 2) + timedelta(days=ordinal)
    truth = 20.0 + ordinal
    # The NOAA component is deliberately OOS_REJECTED by a post-train shift.
    forecast = truth - 0.5
    if rejected_component:
        forecast = truth - 1.0 if ordinal < 20 else truth + 10.0
    elif opposed_calibration:
        # Both component calibrations improve on the holdout, but their raw
        # errors cancel exactly in the fixed equal-weight ensemble.  A formal
        # calibrated P0 must not accept this zero-gain calibration boundary.
        error = 0.5 if ordinal < 20 else 1.0
        if system_id == "NOAA_GEFS_05":
            error = -error
        forecast = truth + error
    member_ids = (
        ["control", *[f"p{item:02d}" for item in range(30)]]
        if system_id == "NOAA_GEFS_05"
        else [f"p{item:02d}" for item in range(50)]
    )
    members = [{
        "member_id": member_id,
        "role": "CONTROL" if member_id == "control" else "PERTURBED",
        "value_c": forecast,
        "source_member_evidence_sha256": _hash("member", system_id, ordinal, member_id),
        "field_receipt_manifest_sha256": _hash("receipt", system_id, ordinal, member_id),
    } for member_id in member_ids]
    raw_messages = sorted([
        _hash("raw-message", system_id, ordinal, item)
        for item in range(8)
    ])
    object_ids = [f"{system_id.lower()}-{ordinal:02d}-object-{item}" for item in range(8)]
    identity: dict[str, object] = {
        "station_id": "CYYZ",
        "model_id": model_id,
        "ensemble_system_id": system_id,
        "model_family": "global_ensemble",
        "provider_id": provider_id,
        "provider_product_id": provider_product_id,
        "independence_family_id": family,
        "source_class": "native_official",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "event_date": event_day.isoformat(),
        "settlement_timezone": "America/New_York",
        "lead_bucket": "d2",
        "lead_bucket_scope": "PREVIOUS_DAY2",
        "run_init_utc": "2026-06-30T18:00:00.000Z",
        "run_cycle": "18Z",
        "run_cycle_scope": "NATIVE_UTC_CYCLE",
        "forecast_decision_at_utc": "2026-07-01T00:00:00.000Z",
        "official_object_available_by_utc": "2026-06-30T21:00:00.000Z",
        "availability_evidence_kind": "OFFICIAL_HTTP_LAST_MODIFIED_UPPER_BOUND",
        "native_arrival_indexed": False,
        "formal_live_authority": False,
        "p1_eligible": False,
        "member_count": registry["member_count"],
        "daily_max_extraction_version": registry["daily_max_extraction_version"],
        "ensemble_reducer_version": ENSEMBLE_REDUCER_VERSION,
        "forecast_value_c": forecast,
        "member_evidence_vector": members,
        "member_evidence_vector_sha256": content_sha256(members),
        "raw_message_sha256s": raw_messages,
        "raw_message_vector_sha256": content_sha256(raw_messages),
        "raw_object_ids": object_ids,
        "raw_object_id_vector_sha256": content_sha256(object_ids),
        "source_packet_sha256": _hash("packet", system_id, ordinal),
        "source_packet_ref": f"/tmp/{system_id}_{ordinal}.json",
        "adapter_event_input_content_sha256": _adapter_input(system_id, ordinal),
        "archive_retrieved_at_utc": "2026-08-05T00:00:00.000Z",
        "canonical_truth_fact_content_sha256": _hash("truth-fact", ordinal),
        "truth_id": f"awc-cyyz-{event_day.isoformat()}",
        "truth_revision_no": 1,
        "truth_content_sha256": _hash("truth", ordinal),
        "truth_value_c": truth,
        "station_day_end_utc": "2026-07-30T04:00:00.000Z",
        "truth_canonical_committed_at_utc": "2026-07-30T05:00:00.000Z",
        "event_oos_protocol_version": EVENT_OOS_PROTOCOL_VERSION,
        "state": "MATERIALIZED_UNPUBLISHED",
    }
    fact = _seal(identity)
    CanonicalNativeEnsembleOOSEventFact.from_mapping(fact)
    return fact


def _cohort(
    system: tuple[str, str, str, str, str], events: list[dict[str, object]]) -> dict[str, object]:
    event_facts = [CanonicalNativeEnsembleOOSEventFact.from_mapping(item) for item in events]
    parameters, train_metrics, holdout_metrics, baseline_metrics, evaluation = (
        derive_native_ensemble_cohort_statistics(event_facts)
    )
    system_id, model_id, provider_id, provider_product_id, family = system
    train = [str(item["content_sha256"]) for item in events[:20]]
    holdout = [str(item["content_sha256"]) for item in events[20:]]
    vector = [
        *({"partition": "train", "event_content_sha256": item} for item in train),
        *({"partition": "holdout", "event_content_sha256": item} for item in holdout),
    ]
    identity: dict[str, object] = {
        "station_id": "CYYZ",
        "model_id": model_id,
        "ensemble_system_id": system_id,
        "model_family": "global_ensemble",
        "provider_id": provider_id,
        "provider_product_id": provider_product_id,
        "independence_family_id": family,
        "source_class": "native_official",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "America/New_York",
        "lead_bucket": "d2",
        "lead_bucket_scope": "PREVIOUS_DAY2",
        "run_cycle": "18Z",
        "run_cycle_scope": "NATIVE_UTC_CYCLE",
        "season_bin": "ALL_YEAR",
        "event_oos_protocol_version": EVENT_OOS_PROTOCOL_VERSION,
        "calibration_method_version": CALIBRATION_METHOD_VERSION,
        "cohort_as_of_utc": AS_OF,
        "train_event_content_sha256s": train,
        "holdout_event_content_sha256s": holdout,
        "train_event_set_sha256": content_sha256(train),
        "holdout_event_set_sha256": content_sha256(holdout),
        "cohort_event_vector_sha256": content_sha256(vector),
        "calibration_parameters": parameters,
        "selection_train_metrics": train_metrics,
        "holdout_metrics": holdout_metrics,
        "baseline_holdout_metrics": baseline_metrics,
        "evaluation_state": evaluation,
        "state": "MATERIALIZED_UNPUBLISHED",
    }
    fact = _seal(identity)
    CanonicalNativeEnsembleOOSCohortFact.from_mapping(fact)
    return fact


def _native_pipeline(
    conn: sqlite3.Connection, *, rejected_component: bool = False,
    opposed_calibration: bool = False,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    # The reservation is produced solely from input commitments, before an
    # event carries truth or a cohort exists in the SSOT.
    reservation = build_historical_holdout_reservation(
        station_id="CYYZ",
        settlement_timezone="America/New_York",
        component_input_commitments=[_input_commitments(item) for item in SYSTEMS],
        registered_at_utc="2026-08-05T00:00:00.000Z",
        prior_evaluation_absence_basis_sha256=_hash("no-prior-evaluation"),
    )
    assert "truth" not in json.dumps(reservation, sort_keys=True).lower()
    event_vectors: list[list[dict[str, object]]] = []
    for system in SYSTEMS:
        events = [
            _event(
                system=system,
                ordinal=ordinal,
                rejected_component=(
                    rejected_component and system[0] == "NOAA_GEFS_05"
                ),
                opposed_calibration=opposed_calibration,
            )
            for ordinal in range(28)
        ]
        for event in events:
            _store(conn, EVENT_FACT_TYPE, event)
        event_vectors.append(events)
    cohorts = [_cohort(system, events) for system, events in zip(SYSTEMS, event_vectors)]
    expected_states = {"OOS_PASS", "OOS_REJECTED"} if rejected_component else {"OOS_PASS"}
    assert {item["evaluation_state"] for item in cohorts} == expected_states
    for cohort in cohorts:
        _store(conn, COHORT_FACT_TYPE, cohort)
    return [reservation], cohorts


def test_formal_p0_rejects_a_component_whose_calibration_lost_to_raw_baseline() -> None:
    conn = _database()
    reservations, cohorts = _native_pipeline(conn, rejected_component=True)
    reservation = reservations[0]
    access = build_historical_holdout_access_receipt(
        reservation=reservation,
        holdout_unsealed_at_utc="2026-08-05T00:00:01.000Z",
        first_evaluated_at_utc="2026-08-05T00:00:02.000Z",
        evaluator_id="test.native.typed.selection",
    )
    with pytest.raises(
        ContractError, match="component_calibration_rejected"
    ):
        build_model_oos_selection_contract(
            conn,
            component_cohort_refs=[
                {
                    "event_fact_type": EVENT_FACT_TYPE,
                    "cohort_fact_type": COHORT_FACT_TYPE,
                    "cohort_content_sha256": str(item["content_sha256"]),
                }
                for item in reversed(cohorts)
            ],
            pre_registered_mask_receipt=reservation,
            holdout_label_access_receipt=access,
            as_of_utc=AS_OF,
        )


def test_formal_p0_rejects_a_zero_gain_calibrated_ensemble() -> None:
    conn = _database()
    reservations, cohorts = _native_pipeline(conn, opposed_calibration=True)
    assert {item["evaluation_state"] for item in cohorts} == {"OOS_PASS"}
    reservation = reservations[0]
    access = build_historical_holdout_access_receipt(
        reservation=reservation,
        holdout_unsealed_at_utc="2026-08-05T00:00:01.000Z",
        first_evaluated_at_utc="2026-08-05T00:00:02.000Z",
        evaluator_id="test.native.typed.zero-gain",
    )
    with pytest.raises(
        ContractError, match="calibration_holdout_rejected"
    ):
        build_model_oos_selection_contract(
            conn,
            component_cohort_refs=[
                {
                    "event_fact_type": EVENT_FACT_TYPE,
                    "cohort_fact_type": COHORT_FACT_TYPE,
                    "cohort_content_sha256": str(item["content_sha256"]),
                }
                for item in cohorts
            ],
            pre_registered_mask_receipt=reservation,
            holdout_label_access_receipt=access,
            as_of_utc=AS_OF,
        )


def test_formal_p0_uses_typed_native_member_set_chain_after_calibration_gate() -> None:
    conn = _database()
    reservations, cohorts = _native_pipeline(conn)
    reservation = reservations[0]
    access = build_historical_holdout_access_receipt(
        reservation=reservation,
        holdout_unsealed_at_utc="2026-08-05T00:00:01.000Z",
        first_evaluated_at_utc="2026-08-05T00:00:02.000Z",
        evaluator_id="test.native.typed.selection",
    )
    selection = build_model_oos_selection_contract(
        conn,
        component_cohort_refs=[
            {
                "event_fact_type": EVENT_FACT_TYPE,
                "cohort_fact_type": COHORT_FACT_TYPE,
                "cohort_content_sha256": str(item["content_sha256"]),
            }
            for item in reversed(cohorts)
        ],
        pre_registered_mask_receipt=reservation,
        holdout_label_access_receipt=access,
        as_of_utc=AS_OF,
    )
    selection_fact = selection["selection_facts"][0]
    assert {item["event_fact_type"] for item in selection_fact["component_cohort_refs"]} == {EVENT_FACT_TYPE}
    assert {item["cohort_fact_type"] for item in selection_fact["component_cohort_refs"]} == {COHORT_FACT_TYPE}
    assert selection_fact["selection_evaluation_state"] == "OOS_PASS"
    _store(conn, "p0_model_family_selection", selection_fact)

    distribution = build_p0_calibrated_distribution_contract(
        conn,
        selection_content_sha256=str(selection_fact["content_sha256"]),
        as_of_utc=AS_OF,
    )
    parsed = CanonicalP0CalibratedDistributionContract.from_mapping(distribution)
    distribution_fact = parsed.distribution_facts[0].raw
    assert distribution_fact["component_cohort_refs"] == selection_fact["component_cohort_refs"]


def test_persisted_p0_root_rejects_a_stale_distribution_descendant() -> None:
    """A pre-gate writer must not make a stale P0 ancestry consumable."""

    conn = _database()
    reservations, cohorts = _native_pipeline(conn)
    reservation = reservations[0]
    access = build_historical_holdout_access_receipt(
        reservation=reservation,
        holdout_unsealed_at_utc="2026-08-05T00:00:01.000Z",
        first_evaluated_at_utc="2026-08-05T00:00:02.000Z",
        evaluator_id="test.native.typed.stale-descendant",
    )
    selection = build_model_oos_selection_contract(
        conn,
        component_cohort_refs=[
            {
                "event_fact_type": EVENT_FACT_TYPE,
                "cohort_fact_type": COHORT_FACT_TYPE,
                "cohort_content_sha256": str(item["content_sha256"]),
            }
            for item in cohorts
        ],
        pre_registered_mask_receipt=reservation,
        holdout_label_access_receipt=access,
        as_of_utc=AS_OF,
    )
    selection_fact = selection["selection_facts"][0]
    _store(conn, "p0_model_family_selection", selection_fact)
    distribution = build_p0_calibrated_distribution_contract(
        conn,
        selection_content_sha256=str(selection_fact["content_sha256"]),
        as_of_utc=AS_OF,
    )
    stale_distribution = deepcopy(distribution["distribution_facts"][0])
    stale_distribution["train_residual_scenarios"][0]["residual_c"] = (
        "0.100000000000"
    )
    stale_distribution["train_residual_scenario_set_sha256"] = content_sha256(
        stale_distribution["train_residual_scenarios"]
    )
    stale_distribution["content_sha256"] = content_sha256({
        key: value
        for key, value in stale_distribution.items()
        if key != "content_sha256"
    })
    # It remains structurally valid, mimicking an immutable row written by an
    # old builder; only a deterministic parent rebuild can reject it.
    CanonicalP0CalibratedDistributionContract.from_mapping({
        "schema": "bayes_basket_target_canonical_p0_calibrated_distribution_contract_v2",
        "as_of_utc": AS_OF,
        "builder_id": "test.native.typed.stale-descendant",
        "distribution_facts": [stale_distribution],
        "content_sha256": content_sha256({
            "schema": "bayes_basket_target_canonical_p0_calibrated_distribution_contract_v2",
            "as_of_utc": AS_OF,
            "builder_id": "test.native.typed.stale-descendant",
            "distribution_facts": [stale_distribution],
        }),
    })
    _store(conn, "p0_calibrated_distribution", stale_distribution)
    activation = build_p0_activation_root_contract(
        conn,
        selection_content_sha256=str(selection_fact["content_sha256"]),
        calibrated_distribution_content_sha256=str(
            stale_distribution["content_sha256"]
        ),
        activation_generation=1,
        predecessor_activation_root_content_sha256=None,
        as_of_utc=AS_OF,
    )
    activation_fact = CanonicalP0ActivationRootContract.from_mapping(
        activation
    ).activation_root_facts[0]
    with pytest.raises(
        ContractError, match="p0_activation_root_distribution_rebuild_mismatch"
    ):
        assert_p0_activation_root_rebuild(conn, activation_fact)


def test_formal_p0_refuses_legacy_or_republisher_fact_type_before_any_cohort_exists() -> None:
    with pytest.raises(ContractError, match="formal_native_event_fact_type_required"):
        build_historical_holdout_reservation(
            station_id="CYYZ",
            settlement_timezone="America/New_York",
            component_input_commitments=[
                {
                    **_input_commitments(SYSTEMS[0]),
                    "event_fact_type": "model_oos_event",
                },
                _input_commitments(SYSTEMS[1]),
            ],
            registered_at_utc="2026-08-05T00:00:00.000Z",
            prior_evaluation_absence_basis_sha256="c" * 64,
        )


def test_selection_fails_closed_when_a_native_event_uses_an_unreserved_adapter_input() -> None:
    conn = _database()
    reservations, cohorts = _native_pipeline(conn)
    reservation = deepcopy(reservations[0])
    reservation["component_input_commitments"][0]["event_input_commitments"][0][
        "adapter_event_input_content_sha256"
    ] = _hash("different-input")
    for component in reservation["component_input_commitments"]:
        component["event_input_date_vector_sha256"] = content_sha256([
            item["event_date"] for item in component["event_input_commitments"]
        ])
        component["event_input_vector_sha256"] = content_sha256(
            component["event_input_commitments"]
        )
    components = reservation["component_input_commitments"]
    reservation["component_input_commitment_set_sha256"] = content_sha256(components)
    reservation["eligible_input_universe_sha256"] = content_sha256([{
        "component_identity": item["component_identity"],
        "event_fact_type": item["event_fact_type"],
        "event_input_vector_sha256": item["event_input_vector_sha256"],
    } for item in components])
    reservation["train_input_manifest_sha256"] = content_sha256([{
        "component_identity": item["component_identity"],
        "event_fact_type": item["event_fact_type"],
        "train_event_input_set_sha256": content_sha256(
            item["event_input_commitments"][:20]
        ),
    } for item in components])
    reservation["content_sha256"] = content_sha256({
        key: value for key, value in reservation.items() if key != "content_sha256"
    })
    access = build_historical_holdout_access_receipt(
        reservation=reservation,
        holdout_unsealed_at_utc="2026-08-05T00:00:01.000Z",
        first_evaluated_at_utc="2026-08-05T00:00:02.000Z",
        evaluator_id="test.native.typed.input-mismatch",
    )
    with pytest.raises(ContractError, match="reservation_input_mismatch"):
        build_model_oos_selection_contract(
            conn,
            component_cohort_refs=[
                {
                    "event_fact_type": EVENT_FACT_TYPE,
                    "cohort_fact_type": COHORT_FACT_TYPE,
                    "cohort_content_sha256": str(item["content_sha256"]),
                }
                for item in cohorts
            ],
            pre_registered_mask_receipt=reservation,
            holdout_label_access_receipt=access,
            as_of_utc=AS_OF,
        )
