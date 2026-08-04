from __future__ import annotations

import pytest

from bayes_basket_target.canonical_contract import (
    CONTRACT_VERSION,
    SCHEMA,
    CanonicalContract,
    CanonicalEnsembleMemberArrivalBindingFact,
)
from bayes_basket_target.contracts import ContractError, content_sha256


def _rehash(row: dict) -> dict:
    identity = {key: value for key, value in row.items() if key != "content_sha256"}
    return {**identity, "content_sha256": content_sha256(identity)}


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


def _arrival(
    *,
    native_object_id: str = "gefs.test/object-a",
    raw_object_sha256: str = "a" * 64,
) -> dict:
    identity: dict[str, object] = {
        "native_arrival_id": f"noaa-{native_object_id}",
        "ensemble_system_id": "NOAA_GEFS_05",
        "provider_id": "NOAA",
        "source_class": "native_official",
        "native_object_id": native_object_id,
        "run_init_utc": "2026-07-22T18:00:00Z",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "raw_object_sha256": raw_object_sha256,
        "published_at_known": True,
        "published_at_utc": "2026-07-22T21:00:00Z",
        "received_at_known": True,
        "received_at_utc": "2026-07-22T21:03:00Z",
        "native_arrival_indexed": True,
    }
    return _rehash(identity)


def _ref(
    ordinal: int,
    *,
    native_object_id: str = "gefs.test/object-a",
    raw_object_sha256: str = "a" * 64,
    receipt_evidence_at_utc: str = "2026-07-22T21:04:00Z",
    receipt_evidence_content_sha256: str = "d" * 64,
    extraction_selector_sha256: str = "e" * 64,
) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "native_object_id": native_object_id,
        "raw_object_sha256": raw_object_sha256,
        "receipt_evidence_at_utc": receipt_evidence_at_utc,
        "receipt_evidence_content_sha256": receipt_evidence_content_sha256,
        "extraction_selector_sha256": extraction_selector_sha256,
    }


def _manifest(member: dict, **overrides: object) -> dict:
    refs = [_ref(0), _ref(1, receipt_evidence_content_sha256="f" * 64)]
    identity: dict[str, object] = {
        "ensemble_member_content_sha256": member["content_sha256"],
        "source_raw_receipt_set_sha256": member["raw_receipt_set_sha256"],
        "required_receipt_refs": refs,
        "required_receipt_ref_set_sha256": content_sha256(refs),
    }
    return _rehash({**identity, **overrides})


def _binding(
    member: dict,
    arrival: dict,
    manifest: dict,
    *,
    ordinals: list[int] | None = None,
    **overrides: object,
) -> dict:
    identity: dict[str, object] = {
        "ensemble_member_content_sha256": member["content_sha256"],
        "native_arrival_content_sha256": arrival["content_sha256"],
        "receipt_manifest_content_sha256": manifest["content_sha256"],
        "manifest_entry_ordinals": [0] if ordinals is None else ordinals,
    }
    return _rehash({**identity, **overrides})


def _contract(
    *,
    members: list[dict] | None = None,
    arrivals: list[dict] | None = None,
    manifests: list[dict] | None = None,
    bindings: list[dict] | None = None,
) -> dict:
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": "2026-08-04T00:00:00Z",
        "builder_id": "canonical.member-arrival-binding.test",
        "model_facts": [],
        "ensemble_member_facts": [] if members is None else members,
        "native_arrival_facts": [] if arrivals is None else arrivals,
        "ensemble_member_receipt_manifest_facts": [] if manifests is None else manifests,
        "ensemble_member_arrival_binding_facts": [] if bindings is None else bindings,
        "truth_facts": [],
        "residual_facts": [],
        "activation_facts": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_binding_pins_one_manifest_and_has_member_arrival_scope() -> None:
    member = _member()
    arrival = _arrival()
    manifest = _manifest(member)
    binding = _binding(member, arrival, manifest, ordinals=[0, 1])

    parsed = CanonicalEnsembleMemberArrivalBindingFact.from_mapping(binding)
    assert parsed.scope_key == "|".join(
        (member["content_sha256"], arrival["content_sha256"])
    )
    assert CanonicalContract.from_mapping(
        _contract(
            members=[member],
            arrivals=[arrival],
            manifests=[manifest],
            bindings=[binding],
        )
    ).to_mapping() == _contract(
        members=[member],
        arrivals=[arrival],
        manifests=[manifest],
        bindings=[binding],
    )

    with pytest.raises(
        ContractError,
        match="canonical_ensemble_member_arrival_binding_fact_unknown_field:legacy_alias",
    ):
        CanonicalEnsembleMemberArrivalBindingFact.from_mapping(
            _rehash({**binding, "legacy_alias": "forbidden"})
        )
    with pytest.raises(
        ContractError,
        match="canonical_ensemble_member_arrival_binding_fact_content_sha256_mismatch",
    ):
        CanonicalEnsembleMemberArrivalBindingFact.from_mapping(
            {**binding, "content_sha256": "0" * 64}
        )


@pytest.mark.parametrize("missing", ("member", "arrival", "manifest"))
def test_binding_rejects_dangling_member_arrival_or_manifest(
    missing: str,
) -> None:
    member = _member()
    arrival = _arrival()
    manifest = _manifest(member)
    with pytest.raises(
        ContractError,
        match="canonical_ensemble_member_(receipt_manifest|arrival_binding)",
    ):
        CanonicalContract.from_mapping(
            _contract(
                members=[] if missing == "member" else [member],
                arrivals=[] if missing == "arrival" else [arrival],
                manifests=[] if missing == "manifest" else [manifest],
                bindings=[_binding(member, arrival, manifest)],
            )
        )


@pytest.mark.parametrize("ordinals", ([0, 0], [0, 2], [1, 0]))
def test_binding_rejects_duplicate_out_of_range_or_unsorted_ordinals(
    ordinals: list[int],
) -> None:
    member = _member()
    arrival = _arrival()
    manifest = _manifest(member)
    with pytest.raises(
        ContractError,
        match="canonical_ensemble_member_(receipt_manifest|arrival_binding)",
    ):
        CanonicalContract.from_mapping(
            _contract(
                members=[member],
                arrivals=[arrival],
                manifests=[manifest],
                bindings=[_binding(member, arrival, manifest, ordinals=ordinals)],
            )
        )


def test_binding_rejects_manifest_for_a_different_member() -> None:
    member = _member()
    other_member = _member(member_id="p01", member_role="PERTURBED")
    arrival = _arrival()
    other_manifest = _manifest(other_member)
    with pytest.raises(
        ContractError,
        match="canonical_ensemble_member_(receipt_manifest|arrival_binding)",
    ):
        CanonicalContract.from_mapping(
            _contract(
                members=[member, other_member],
                arrivals=[arrival],
                manifests=[other_manifest],
                bindings=[_binding(member, arrival, other_manifest)],
            )
        )


def test_binding_rejects_manifest_entry_for_a_different_native_object() -> None:
    member = _member()
    arrival = _arrival()
    manifest = _manifest(
        member,
        required_receipt_refs=[
            _ref(0, native_object_id="gefs.test/object-b", raw_object_sha256="9" * 64)
        ],
        required_receipt_ref_set_sha256=content_sha256(
            [_ref(0, native_object_id="gefs.test/object-b", raw_object_sha256="9" * 64)]
        ),
    )
    with pytest.raises(ContractError, match="canonical_ensemble_member_arrival_binding"):
        CanonicalContract.from_mapping(
            _contract(
                members=[member],
                arrivals=[arrival],
                manifests=[manifest],
                bindings=[_binding(member, arrival, manifest)],
            )
        )


def test_binding_rejects_receipt_evidence_after_contract_asof() -> None:
    member = _member()
    arrival = _arrival()
    late_ref = _ref(0, receipt_evidence_at_utc="2026-08-04T00:00:01Z")
    manifest = _manifest(
        member,
        required_receipt_refs=[late_ref],
        required_receipt_ref_set_sha256=content_sha256([late_ref]),
    )
    with pytest.raises(
        ContractError,
        match="canonical_ensemble_member_(receipt_manifest|arrival_binding)",
    ):
        CanonicalContract.from_mapping(
            _contract(
                members=[member],
                arrivals=[arrival],
                manifests=[manifest],
                bindings=[_binding(member, arrival, manifest)],
            )
        )
