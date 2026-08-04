from __future__ import annotations

import pytest

from bayes_basket_target.canonical_contract import (
    CONTRACT_VERSION,
    SCHEMA,
    CanonicalContract,
    CanonicalEnsembleMemberFact,
    CanonicalNativeArrivalFact,
)
from bayes_basket_target.contracts import ContractError, content_sha256


def _rehash(row: dict) -> dict:
    identity = {key: value for key, value in row.items() if key != "content_sha256"}
    return {**identity, "content_sha256": content_sha256(identity)}


def _arrival(**overrides: object) -> dict:
    identity: dict[str, object] = {
        "native_arrival_id": "noaa-gefs-20260722-18z-pgrb2a",
        "ensemble_system_id": "NOAA_GEFS_05",
        "provider_id": "NOAA",
        "source_class": "native_official",
        "native_object_id": "gefs.20260722/18/atmos/pgrb2ap5",
        "run_init_utc": "2026-07-22T18:00:00Z",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "raw_object_sha256": "a" * 64,
        "published_at_known": True,
        "published_at_utc": "2026-07-22T21:00:00Z",
        "received_at_known": True,
        "received_at_utc": "2026-07-22T21:03:00Z",
        "native_arrival_indexed": True,
    }
    return _rehash({**identity, **overrides})


def _member(**overrides: object) -> dict:
    identity: dict[str, object] = {
        "station_id": "KATL",
        "ensemble_system_id": "NOAA_GEFS_05",
        "model_family": "global_ensemble",
        "provider_id": "NOAA",
        "source_class": "native_official",
        "event_date": "2026-07-24",
        "run_init_utc": "2026-07-22T18:00:00Z",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "member_id": "c00",
        "member_role": "CONTROL",
        "extraction_version": "gefs.tmax.v1",
        "settlement_timezone": "America/New_York",
        "forecast_value": 31.5,
        "raw_receipt_set_sha256": "b" * 64,
        "source_member_fact_sha": "c" * 64,
        "archive_query_at_known": True,
        "archive_query_at_utc": "2026-07-22T21:05:00Z",
        "oos_role": "historical_strict_oos",
        "closed_day": True,
        "strict_asof": True,
    }
    return _rehash({**identity, **overrides})


def _contract(
    *,
    members: list[dict] | None = None,
    arrivals: list[dict] | None = None,
    contract_version: int = CONTRACT_VERSION,
) -> dict:
    identity = {
        "schema": SCHEMA,
        "contract_version": contract_version,
        "as_of_utc": "2026-08-04T00:00:00Z",
        "builder_id": "canonical.contract.v2.test",
        "model_facts": [],
        "ensemble_member_facts": [] if members is None else members,
        "native_arrival_facts": [] if arrivals is None else arrivals,
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [],
        "residual_facts": [],
        "activation_facts": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_v2_round_trip_keeps_member_immutable_when_arrival_arrives_later() -> None:
    member = _member()
    decoded_only = CanonicalContract.from_mapping(_contract(members=[member]))
    assert decoded_only.ensemble_member_facts[0].content_sha256 == member["content_sha256"]
    assert decoded_only.native_arrival_facts == ()

    with_arrival = CanonicalContract.from_mapping(
        _contract(members=[member], arrivals=[_arrival()])
    )
    assert with_arrival.ensemble_member_facts[0].content_sha256 == member["content_sha256"]
    assert with_arrival.to_mapping() == _contract(members=[member], arrivals=[_arrival()])


def test_member_natural_key_is_unique_but_extraction_version_versions_immutable_decode() -> None:
    member = _member()
    with pytest.raises(ContractError, match="canonical_ensemble_member_fact_scope_duplicate"):
        CanonicalContract.from_mapping(_contract(members=[member, member]))

    newer = _member(extraction_version="gefs.tmax.v2", forecast_value=31.75)
    packet = CanonicalContract.from_mapping(_contract(members=[member, newer]))
    assert [fact.extraction_version for fact in packet.ensemble_member_facts] == [
        "gefs.tmax.v1",
        "gefs.tmax.v2",
    ]


def test_native_arrival_natural_key_is_lead_scoped() -> None:
    d2 = _arrival()
    d1 = _arrival(lead_bucket="d1")
    packet = CanonicalContract.from_mapping(_contract(arrivals=[d2, d1]))
    assert [fact.lead_bucket for fact in packet.native_arrival_facts] == ["d2", "d1"]


def test_member_rejects_legacy_arrival_and_raw_object_aliases() -> None:
    with pytest.raises(ContractError, match="canonical_ensemble_member_fact_unknown_field:native_arrival_content_sha256"):
        CanonicalEnsembleMemberFact.from_mapping(
            _member(native_arrival_content_sha256="d" * 64)
        )
    with pytest.raises(ContractError, match="canonical_ensemble_member_fact_unknown_field:raw_object_sha256"):
        CanonicalEnsembleMemberFact.from_mapping(_member(raw_object_sha256="a" * 64))


def test_republisher_member_is_no_live_fact_not_a_native_arrival() -> None:
    republisher = CanonicalEnsembleMemberFact.from_mapping(
        _member(source_class="aggregator_republisher")
    )
    assert republisher.source_class == "aggregator_republisher"

    with pytest.raises(ContractError, match="canonical_native_arrival_republisher_forbidden"):
        CanonicalNativeArrivalFact.from_mapping(
            _arrival(source_class="aggregator_republisher")
        )


def test_unknown_clock_requires_explicit_empty_timestamp_and_cannot_be_indexed() -> None:
    with pytest.raises(ContractError, match="canonical_native_arrival_published_at_utc_unknown_must_be_empty"):
        CanonicalNativeArrivalFact.from_mapping(
            _arrival(published_at_known=False)
        )
    with pytest.raises(ContractError, match="canonical_ensemble_member_archive_query_at_utc_unknown_must_be_empty"):
        CanonicalEnsembleMemberFact.from_mapping(
            _member(archive_query_at_known=False)
        )

    no_clock = _arrival(
        raw_object_sha256="",
        published_at_known=False,
        published_at_utc="",
        received_at_known=False,
        received_at_utc="",
        native_arrival_indexed=False,
    )
    assert CanonicalNativeArrivalFact.from_mapping(no_clock).raw_object_sha256 == ""
    with pytest.raises(ContractError, match="canonical_native_arrival_indexed_timestamps_missing"):
        CanonicalNativeArrivalFact.from_mapping(
            _rehash({**no_clock, "native_arrival_indexed": True})
        )


def test_member_archive_cannot_precede_its_exact_run() -> None:
    with pytest.raises(ContractError, match="canonical_ensemble_member_archive_before_run_init"):
        CanonicalEnsembleMemberFact.from_mapping(
            _member(archive_query_at_utc="2026-07-22T17:59:59Z")
        )


def test_v1_packet_fails_closed_before_any_legacy_compatibility_path() -> None:
    with pytest.raises(ContractError, match="canonical_contract_schema_invalid"):
        CanonicalContract.from_mapping(_contract(members=[_member()], contract_version=1))
