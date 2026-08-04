from __future__ import annotations

import pytest

from bayes_basket_target.canonical_contract import (
    CanonicalEnsembleMemberReceiptManifestFact,
)
from bayes_basket_target.contracts import ContractError, content_sha256


def _rehash(row: dict) -> dict:
    identity = {key: value for key, value in row.items() if key != "content_sha256"}
    return {**identity, "content_sha256": content_sha256(identity)}


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


def _manifest(**overrides: object) -> dict:
    refs = [_ref(0), _ref(1, receipt_evidence_content_sha256="f" * 64)]
    identity: dict[str, object] = {
        "ensemble_member_content_sha256": "b" * 64,
        "source_raw_receipt_set_sha256": "c" * 64,
        "required_receipt_refs": refs,
        "required_receipt_ref_set_sha256": content_sha256(refs),
    }
    return _rehash({**identity, **overrides})


def test_manifest_has_strict_fields_hash_and_member_hash_scope() -> None:
    manifest = _manifest()
    parsed = CanonicalEnsembleMemberReceiptManifestFact.from_mapping(manifest)
    assert parsed.scope_key == manifest["ensemble_member_content_sha256"]
    assert parsed.to_mapping() == manifest

    with pytest.raises(
        ContractError,
        match="canonical_ensemble_member_receipt_manifest_fact_unknown_field:legacy_alias",
    ):
        CanonicalEnsembleMemberReceiptManifestFact.from_mapping(
            _rehash({**manifest, "legacy_alias": "forbidden"})
        )
    with pytest.raises(
        ContractError,
        match="canonical_ensemble_member_receipt_manifest_fact_content_sha256_mismatch",
    ):
        CanonicalEnsembleMemberReceiptManifestFact.from_mapping(
            {**manifest, "content_sha256": "0" * 64}
        )


@pytest.mark.parametrize(
    "refs",
    (
        [_ref(1)],
        [_ref(0), _ref(0)],
        [_ref(1), _ref(0)],
    ),
)
def test_manifest_rejects_non_contiguous_duplicate_or_unsorted_ordinals(
    refs: list[dict[str, object]],
) -> None:
    with pytest.raises(ContractError, match="canonical_ensemble_member_receipt_manifest"):
        CanonicalEnsembleMemberReceiptManifestFact.from_mapping(
            _manifest(
                required_receipt_refs=refs,
                required_receipt_ref_set_sha256=content_sha256(refs),
            )
        )


def test_manifest_rejects_required_receipt_set_hash_mismatch() -> None:
    with pytest.raises(
        ContractError,
        match="canonical_ensemble_member_receipt_manifest_ref_set_hash_mismatch",
    ):
        CanonicalEnsembleMemberReceiptManifestFact.from_mapping(
            _manifest(required_receipt_ref_set_sha256="0" * 64)
        )
