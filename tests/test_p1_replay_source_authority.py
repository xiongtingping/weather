from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.p1_replay_source_authority import (
    AUTHORITY_KEY,
    MANUAL_REVIEW_ACCEPTED,
    MANUAL_REVIEW_NOT_REQUIRED,
    build_awc_official_p1_replay_authority,
    resolve_awc_official_p1_replay_authority,
)


EVIDENCE_SHA = "a" * 64
SNAPSHOT_SHA = "b" * 64
RAW_SHA = "c" * 64
REVIEW_SHA = "d" * 64
DECISION_AT = "2026-07-27T10:10:00Z"


def _observation(**overrides: object) -> dict[str, object]:
    return {
        "observation_id": "awc-rkpk-1000",
        "station_id": "RKPK",
        "event_date_local": "2026-07-27",
        "source_lane": "AWC",
        "report_type": "METAR",
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "raw_sha256": RAW_SHA,
        "correction_suspected": False,
        **overrides,
    }


def _binding(**overrides: object) -> dict[str, object]:
    return {
        "observation_id": "awc-rkpk-1000",
        "station_id": "RKPK",
        "event_date_local": "2026-07-27",
        "source_lane": "AWC",
        "report_type": "METAR",
        "raw_sha256": RAW_SHA,
        "published_at_utc": "2026-07-27T10:00:00Z",
        "received_at_utc": "2026-07-27T10:05:00Z",
        "correction_status": "NONE",
        "manual_review_status": MANUAL_REVIEW_NOT_REQUIRED,
        "manual_review_receipt_sha256": None,
        **overrides,
    }


def _canonical(*, authority: dict[str, object] | None = None, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "evidence_sha256": EVIDENCE_SHA,
        "observations": [_observation()],
    }
    if authority is not None:
        result[AUTHORITY_KEY] = authority
    result.update(overrides)
    return result


def _authority(*, bindings: list[dict[str, object]] | None = None) -> dict[str, object]:
    return build_awc_official_p1_replay_authority(
        authority_id="awc-rkpk-20260727-v1",
        station_id="RKPK",
        event_date="2026-07-27",
        canonical_evidence_sha256=EVIDENCE_SHA,
        source_snapshot_sha256=SNAPSHOT_SHA,
        observations=bindings or [_binding()],
    )


def _rehash(authority: dict[str, object]) -> dict[str, object]:
    identity = {key: value for key, value in authority.items() if key != "content_sha256"}
    authority["content_sha256"] = content_sha256(identity)
    return authority


def _resolve(canonical: dict[str, object]) -> dict[str, object]:
    return resolve_awc_official_p1_replay_authority(
        canonical,
        observations=[dict(item) for item in canonical["observations"]],
        station_id="RKPK",
        event_date="2026-07-27",
        decision_at_utc=DECISION_AT,
    )


def test_awc_authority_is_content_addressed_and_bound_to_exact_observation() -> None:
    authority = _authority()
    resolved = _resolve(_canonical(authority=authority))

    assert resolved["source_lane"] == "AWC"
    assert resolved["source_publisher"] == "AVIATION_WEATHER_CENTER"
    assert resolved["content_sha256"] == authority["content_sha256"]
    assert content_sha256(
        {key: value for key, value in resolved.items() if key != "content_sha256"}
    ) == resolved["content_sha256"]
    assert resolved["canonical_evidence_sha256"] == EVIDENCE_SHA
    assert resolved["fallback_used"] is False
    assert resolved["observation_bindings"][0]["raw_sha256"] == RAW_SHA


def test_authority_content_tampering_is_fail_closed() -> None:
    authority = _authority()
    authority["source_snapshot_sha256"] = "e" * 64

    with pytest.raises(ContractError, match="p1_replay_authority_content_hash_invalid"):
        _resolve(_canonical(authority=authority))


def test_legacy_amsc_binding_cannot_be_selected_or_relabelled() -> None:
    authority = _authority()
    authority["observation_bindings"][0]["source_lane"] = "AMSC"
    _rehash(authority)

    with pytest.raises(ContractError, match="p1_replay_authority_binding_not_awc"):
        _resolve(_canonical(authority=authority))


@pytest.mark.parametrize("fallback_lane", ["AMSC", "OPEN_METEO"])
def test_fallback_is_forbidden_even_when_the_authority_hash_is_valid(
    fallback_lane: str,
) -> None:
    authority = _authority()
    authority["fallback_used"] = True
    authority["fallback_source_lane"] = fallback_lane
    _rehash(authority)

    with pytest.raises(ContractError, match="p1_replay_authority_fallback_forbidden"):
        _resolve(_canonical(authority=authority))


@pytest.mark.parametrize(
    "binding,reason",
    [
        (
            _binding(received_at_utc="2026-07-27T10:11:00Z"),
            "p1_replay_authority_received_after_decision",
        ),
        (
            _binding(
                published_at_utc="2026-07-27T10:06:01Z",
                received_at_utc="2026-07-27T10:05:00Z",
            ),
            "p1_replay_authority_publication_after_receipt",
        ),
    ],
)
def test_publication_receipt_decision_order_is_strict(
    binding: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(ContractError, match=reason):
        _resolve(_canonical(authority=_authority(bindings=[binding])))


def test_nominal_minute_may_lead_receipt_by_transmission_jitter() -> None:
    # SBGR stamps on the hour and goes on the wire ~20s early, so its
    # published minute really does sit after the poll that carried it.
    binding = _binding(
        published_at_utc="2026-07-27T10:00:00Z",
        received_at_utc="2026-07-27T09:59:40.124Z",
    )
    resolved = _resolve(_canonical(authority=_authority(bindings=[binding])))

    assert resolved["observation_bindings"][0]["published_at_utc"] == (
        "2026-07-27T10:00:00Z"
    )


def test_cor_or_amd_is_quarantined_until_manually_accepted() -> None:
    binding = _binding(correction_status="AMD")
    with pytest.raises(ContractError, match="p1_replay_authority_correction_quarantined"):
        _resolve(_canonical(authority=_authority(bindings=[binding])))


def test_manually_accepted_correction_requires_and_retains_review_receipt() -> None:
    binding = _binding(
        correction_status="COR",
        manual_review_status=MANUAL_REVIEW_ACCEPTED,
        manual_review_receipt_sha256=REVIEW_SHA,
    )
    canonical = _canonical(
        authority=_authority(bindings=[binding]),
        observations=[_observation(correction_suspected=True)],
    )
    resolved = _resolve(canonical)

    assert resolved["observation_bindings"][0]["correction_status"] == "COR"
    assert (
        resolved["observation_bindings"][0]["manual_review_receipt_sha256"]
        == REVIEW_SHA
    )


def test_authority_does_not_accept_a_different_canonical_scope() -> None:
    authority = _authority()
    canonical = _canonical(authority=authority)
    canonical["observations"] = [_observation(observation_id="other-awc-observation")]

    with pytest.raises(ContractError, match="p1_replay_authority_binding_scope_mismatch"):
        _resolve(deepcopy(canonical))
