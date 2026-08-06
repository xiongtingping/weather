from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import unittest

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_oos_contract import (
    AVAILABILITY_EVIDENCE_KIND,
    CALIBRATION_METHOD_VERSION,
    CONTRACT_SCHEMA,
    EVENT_OOS_PROTOCOL_VERSION,
    ENSEMBLE_REDUCER_VERSION,
    NATIVE_ENSEMBLE_SYSTEM_REGISTRY,
    CanonicalNativeEnsembleOOSContract,
    CanonicalNativeEnsembleOOSEventFact,
    derive_native_ensemble_cohort_statistics,
)


def _seal(identity: dict[str, object]) -> dict[str, object]:
    return {**identity, "content_sha256": content_sha256(identity)}


def _event(event_date: str, *, system_id: str = "NOAA_GEFS_05") -> dict[str, object]:
    day = date.fromisoformat(event_date)
    day_index = (day - date(2026, 7, 2)).days
    registry = NATIVE_ENSEMBLE_SYSTEM_REGISTRY[system_id]
    roles = [
        *("CONTROL",) * int(registry["role_counts"]["CONTROL"]),
        *("PERTURBED",) * int(registry["role_counts"]["PERTURBED"]),
    ]
    members = []
    for ordinal, role in enumerate(roles):
        member_id = "c00" if role == "CONTROL" else f"p{ordinal:02d}"
        members.append(
            {
                "member_id": member_id,
                "role": role,
                "value_c": 21.0 + day_index,
                "source_member_evidence_sha256": content_sha256(
                    [system_id, event_date, member_id, "member"]
                ),
                "field_receipt_manifest_sha256": content_sha256(
                    [system_id, event_date, member_id, "receipt"]
                ),
            }
        )
    # The values are sorted lexically so the vector itself is canonical.
    raw_messages = sorted(
        [
            content_sha256([system_id, event_date, "message-a"]),
            content_sha256([system_id, event_date, "message-b"]),
        ]
    )
    raw_object_ids = sorted(
        [
            f"official://{system_id}/{event_date}/object-a",
            f"official://{system_id}/{event_date}/object-b",
        ]
    )
    identity: dict[str, object] = {
        "station_id": "CYYZ",
        "model_id": "GEFS",
        "ensemble_system_id": system_id,
        "model_family": "global_ensemble",
        "provider_id": "NOAA",
        "provider_product_id": "gefs_0p50",
        "independence_family_id": "NOAA_GEFS",
        "source_class": "native_official",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "event_date": event_date,
        "settlement_timezone": "America/New_York",
        "lead_bucket": "d2",
        "lead_bucket_scope": "PREVIOUS_DAY2",
        "run_init_utc": f"{(day - timedelta(days=2)).isoformat()}T18:00:00.000Z",
        "run_cycle": "18Z",
        "run_cycle_scope": "NATIVE_UTC_CYCLE",
        "forecast_decision_at_utc": f"{(day - timedelta(days=1)).isoformat()}T00:00:00.000Z",
        "official_object_available_by_utc": f"{(day - timedelta(days=2)).isoformat()}T20:00:00.000Z",
        "availability_evidence_kind": AVAILABILITY_EVIDENCE_KIND,
        "native_arrival_indexed": False,
        "formal_live_authority": False,
        "p1_eligible": False,
        "member_count": len(members),
        "daily_max_extraction_version": registry["daily_max_extraction_version"],
        "ensemble_reducer_version": ENSEMBLE_REDUCER_VERSION,
        "forecast_value_c": 21.0 + day_index,
        "member_evidence_vector": members,
        "member_evidence_vector_sha256": content_sha256(members),
        "raw_message_sha256s": raw_messages,
        "raw_message_vector_sha256": content_sha256(raw_messages),
        "raw_object_ids": raw_object_ids,
        "raw_object_id_vector_sha256": content_sha256(raw_object_ids),
        "source_packet_sha256": content_sha256([system_id, event_date, "packet"]),
        "source_packet_ref": f"retained:test/{system_id}/{event_date}.json",
        "adapter_event_input_content_sha256": content_sha256(
            [system_id, event_date, "reservation_pre_truth_model_input"]
        ),
        "archive_retrieved_at_utc": "2026-08-01T00:00:00.000Z",
        "canonical_truth_fact_content_sha256": content_sha256(
            ["truth-fact", event_date]
        ),
        "truth_id": f"awc:CYYZ:{event_date}:r1",
        "truth_revision_no": 1,
        "truth_content_sha256": content_sha256(["truth", event_date]),
        "truth_value_c": 21.0 + day_index + (0.25 if day_index < 20 else -0.25),
        "station_day_end_utc": f"{(day + timedelta(days=1)).isoformat()}T04:00:00.000Z",
        "truth_canonical_committed_at_utc": f"{(day + timedelta(days=1)).isoformat()}T05:00:00.000Z",
        "event_oos_protocol_version": EVENT_OOS_PROTOCOL_VERSION,
        "state": "MATERIALIZED_UNPUBLISHED",
    }
    return _seal(identity)


def _dates() -> list[str]:
    first = date(2026, 7, 2)
    return [(first + timedelta(days=offset)).isoformat() for offset in range(28)]


def _cohort(events: list[dict[str, object]]) -> dict[str, object]:
    parsed = [CanonicalNativeEnsembleOOSEventFact.from_mapping(event) for event in events]
    parameters, train, holdout, baseline, evaluation = (
        derive_native_ensemble_cohort_statistics(parsed)
    )
    train_hashes = [str(event["content_sha256"]) for event in events[:20]]
    holdout_hashes = [str(event["content_sha256"]) for event in events[20:]]
    vector = [
        *(
            {"partition": "train", "event_content_sha256": item}
            for item in train_hashes
        ),
        *(
            {"partition": "holdout", "event_content_sha256": item}
            for item in holdout_hashes
        ),
    ]
    identity: dict[str, object] = {
        "station_id": "CYYZ",
        "model_id": "GEFS",
        "ensemble_system_id": "NOAA_GEFS_05",
        "model_family": "global_ensemble",
        "provider_id": "NOAA",
        "provider_product_id": "gefs_0p50",
        "independence_family_id": "NOAA_GEFS",
        "source_class": "native_official",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "America/New_York",
        "lead_bucket": "d2",
        "lead_bucket_scope": "PREVIOUS_DAY2",
        "run_cycle": "18Z",
        "run_cycle_scope": "NATIVE_UTC_CYCLE",
        "season_bin": "JJA",
        "event_oos_protocol_version": EVENT_OOS_PROTOCOL_VERSION,
        "calibration_method_version": CALIBRATION_METHOD_VERSION,
        "cohort_as_of_utc": "2026-08-01T00:00:00.000Z",
        "train_event_content_sha256s": train_hashes,
        "holdout_event_content_sha256s": holdout_hashes,
        "train_event_set_sha256": content_sha256(train_hashes),
        "holdout_event_set_sha256": content_sha256(holdout_hashes),
        "cohort_event_vector_sha256": content_sha256(vector),
        "calibration_parameters": parameters,
        "selection_train_metrics": train,
        "holdout_metrics": holdout,
        "baseline_holdout_metrics": baseline,
        "evaluation_state": evaluation,
        "state": "MATERIALIZED_UNPUBLISHED",
    }
    return _seal(identity)


def _contract(
    events: list[dict[str, object]], cohort: dict[str, object]
) -> dict[str, object]:
    return _seal(
        {
            "schema": CONTRACT_SCHEMA,
            "as_of_utc": "2026-08-02T00:00:00.000Z",
            "builder_id": "tests.native_ensemble_oos_contract.v1",
            "event_facts": events,
            "cohort_facts": [cohort],
        }
    )


def _rehash_event(event: dict[str, object]) -> None:
    event["content_sha256"] = content_sha256(
        {key: value for key, value in event.items() if key != "content_sha256"}
    )


def test_native_ensemble_contract_accepts_member_set_and_exact_20_plus_8() -> None:
    events = [_event(item) for item in _dates()]
    contract = CanonicalNativeEnsembleOOSContract.from_mapping(
        _contract(events, _cohort(events))
    )

    assert len(contract.event_facts) == 28
    assert len(contract.cohort_facts) == 1
    event = contract.event_facts[0].raw
    assert event["official_object_available_by_utc"] < event["forecast_decision_at_utc"]
    assert event["availability_evidence_kind"] == AVAILABILITY_EVIDENCE_KIND
    assert event["native_arrival_indexed"] is False
    assert event["formal_live_authority"] is False
    assert event["p1_eligible"] is False
    assert "raw_object_sha256" not in event
    assert event["raw_object_ids"] == sorted(event["raw_object_ids"])
    assert "raw_object_sha256s" not in event
    assert len(event["adapter_event_input_content_sha256"]) == 64
    assert "model_input_content_sha256" not in event
    assert event["member_count"] == 31

    pf50 = CanonicalNativeEnsembleOOSEventFact.from_mapping(
        _event("2026-07-02", system_id="ECMWF_IFS_ENS_OPEN_PF50")
    )
    assert len(pf50.raw["member_evidence_vector"]) == 50
    assert {item["role"] for item in pf50.raw["member_evidence_vector"]} == {
        "PERTURBED"
    }


def test_event_rejects_authority_escalation_and_noncanonical_member_order() -> None:
    event = _event("2026-07-02")
    event["native_arrival_indexed"] = True
    _rehash_event(event)
    with pytest.raises(ContractError, match="historical_authority_escalation"):
        CanonicalNativeEnsembleOOSEventFact.from_mapping(event)

    event = _event("2026-07-02")
    members = event["member_evidence_vector"]
    assert isinstance(members, list)
    event["member_evidence_vector"] = list(reversed(members))
    event["member_evidence_vector_sha256"] = content_sha256(
        event["member_evidence_vector"]
    )
    _rehash_event(event)
    with pytest.raises(ContractError, match="member_evidence_ordering_invalid"):
        CanonicalNativeEnsembleOOSEventFact.from_mapping(event)

    event = _event("2026-07-02")
    event["member_count"] = 30
    _rehash_event(event)
    with pytest.raises(ContractError, match="member_count_vector_mismatch"):
        CanonicalNativeEnsembleOOSEventFact.from_mapping(event)

    event = _event("2026-07-02")
    members = event["member_evidence_vector"]
    assert isinstance(members, list)
    members[0]["role"] = "PERTURBED"
    event["member_evidence_vector_sha256"] = content_sha256(members)
    _rehash_event(event)
    with pytest.raises(ContractError, match="member_role_roster_mismatch"):
        CanonicalNativeEnsembleOOSEventFact.from_mapping(event)

    event = _event("2026-07-02")
    object_ids = event["raw_object_ids"]
    assert isinstance(object_ids, list)
    event["raw_object_ids"] = list(reversed(object_ids))
    event["raw_object_id_vector_sha256"] = content_sha256(
        event["raw_object_ids"]
    )
    _rehash_event(event)
    with pytest.raises(ContractError, match="raw_object_ids_ordering_invalid"):
        CanonicalNativeEnsembleOOSEventFact.from_mapping(event)

    event = _event("2026-07-02")
    event["adapter_event_input_content_sha256"] = "not-a-hash"
    _rehash_event(event)
    with pytest.raises(
        ContractError, match="adapter_event_input_content_sha256_invalid"
    ):
        CanonicalNativeEnsembleOOSEventFact.from_mapping(event)


def test_event_treats_http_last_modified_only_as_availability_upper_bound() -> None:
    event = _event("2026-07-02")
    event["official_object_available_by_utc"] = "2026-07-01T00:01:00.000Z"
    _rehash_event(event)
    with pytest.raises(ContractError, match="event_temporal_order_invalid"):
        CanonicalNativeEnsembleOOSEventFact.from_mapping(event)

    event = _event("2026-07-02")
    event["published_at_utc"] = "2026-06-30T20:00:00.000Z"
    with pytest.raises(ContractError, match="event_fields_mismatch"):
        CanonicalNativeEnsembleOOSEventFact.from_mapping(event)

    event = _event("2026-07-02")
    event["archive_retrieved_at_utc"] = "2026-08-03T00:00:00.000Z"
    _rehash_event(event)
    contract = _seal(
        {
            "schema": CONTRACT_SCHEMA,
            "as_of_utc": "2026-08-02T00:00:00.000Z",
            "builder_id": "tests.native_ensemble_oos_contract.v1",
            "event_facts": [event],
            "cohort_facts": [],
        }
    )
    with pytest.raises(ContractError, match="contract_fact_after_asof"):
        CanonicalNativeEnsembleOOSContract.from_mapping(contract)


def test_cohort_statistics_and_hashes_are_recomputed_from_member_set_events() -> None:
    events = [_event(item) for item in _dates()]
    cohort = _cohort(events)
    mutated = deepcopy(cohort)
    metrics = mutated["holdout_metrics"]
    assert isinstance(metrics, dict)
    metrics["mae_c"] = 99.0
    mutated["content_sha256"] = content_sha256(
        {key: value for key, value in mutated.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="cohort_statistics_mismatch"):
        CanonicalNativeEnsembleOOSContract.from_mapping(_contract(events, mutated))


class NativeEnsembleOOSContractTest(unittest.TestCase):
    """Keep a direct stdlib-unittest entry point for constrained environments."""

    def test_valid_member_set_contract(self) -> None:
        test_native_ensemble_contract_accepts_member_set_and_exact_20_plus_8()

    def test_authority_and_member_vector_guards(self) -> None:
        test_event_rejects_authority_escalation_and_noncanonical_member_order()

    def test_availability_upper_bound_guard(self) -> None:
        test_event_treats_http_last_modified_only_as_availability_upper_bound()

    def test_cohort_recomputation_guard(self) -> None:
        test_cohort_statistics_and_hashes_are_recomputed_from_member_set_events()
