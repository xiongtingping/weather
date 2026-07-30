from __future__ import annotations

import pytest

from bayes_basket_target import ContractError
from scripts.replay_bayes_basket_target_p1 import (
    CANONICAL_SCHEMA,
    P0M_SCHEMA,
    _formal_awc_station_rows,
    _validate_p0m_canonical_boundary,
)


def _canonical_row(**overrides: object) -> dict:
    return {
        "station_id": "RKPK",
        "event_date_local": "2026-07-27",
        "source_lane": "AWC",
        "report_type": "METAR",
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        **overrides,
    }


@pytest.mark.parametrize("source_lane", ["AWC", "awc"])
def test_formal_awc_lane_is_case_normalized(source_lane: str) -> None:
    rows = _formal_awc_station_rows(
        {"observations": [_canonical_row(source_lane=source_lane)]},
        station_id="RKPK",
        event_date="2026-07-27",
    )
    assert len(rows) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_lane": "AMSC"},
        {"evidence_role": "REFERENCE_ONLY"},
        {"qc_status": "REFERENCE_ONLY"},
        {"report_type": "TAF"},
    ],
)
def test_nonformal_or_nonawc_rows_are_rejected(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(
        ContractError,
        match="p1_replay_formal_awc_scope_invalid",
    ):
        _formal_awc_station_rows(
            {"observations": [_canonical_row(**overrides)]},
            station_id="RKPK",
            event_date="2026-07-27",
        )


def test_canonical_timezone_must_match_p0m_before_replay() -> None:
    p0m = {
        "schema": P0M_SCHEMA,
        "state": "retrospective_no_live_materialized",
        "boundary": {"places_orders": False},
        "p0m_envelope": {"settlement_timezone": "Asia/Seoul"},
    }
    canonical = {
        "schema": CANONICAL_SCHEMA,
        "state": "EXPORTED",
        "target_timezone": "America/New_York",
        "blockers": [],
    }
    with pytest.raises(
        ContractError,
        match="p1_replay_canonical_evidence_invalid",
    ):
        _validate_p0m_canonical_boundary(p0m, canonical)
