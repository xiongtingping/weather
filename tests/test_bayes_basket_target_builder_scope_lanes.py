from __future__ import annotations

from scripts.build_bayes_basket_target_calibration_evidence import (
    _output_scope,
    _scope_canonical_evidence,
)


def _canonical_payload() -> dict[str, object]:
    return {
        "schema": "bayes_basket_target_canonical_observation_evidence_v1",
        "state": "EXPORTED",
        "checks": {"strict_timestamp_ordering": True},
        "observations": [
            {"station_id": "ZBAA", "source_lane": "AMSC"},
            {"station_id": "KATL", "source_lane": "AWC"},
        ],
        "truth_rows": [
            {"station_id": "ZBAA", "source_lane": "AMSC"},
            {"station_id": "KATL", "source_lane": "AWC"},
        ],
    }


def test_amsc_allowlist_does_not_claim_amsc_was_excluded() -> None:
    scoped = _scope_canonical_evidence(
        _canonical_payload(),
        station_scope={"ZBAA"},
        source_sha256="a" * 64,
    )

    assert scoped is not None
    assert scoped["scope"]["excluded_source_lanes"] == ["awc"]
    assert _output_scope(
        station_scope={"ZBAA"},
        canonical_evidence=scoped,
    )["excluded_source_lanes"] == ["awc"]


def test_awc_allowlist_still_records_amsc_exclusion() -> None:
    scoped = _scope_canonical_evidence(
        _canonical_payload(),
        station_scope={"KATL"},
        source_sha256="b" * 64,
    )

    assert scoped is not None
    assert scoped["scope"]["excluded_source_lanes"] == ["amsc"]
