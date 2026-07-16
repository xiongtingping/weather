from __future__ import annotations

from resolution_final_label_proposal_runtime import (
    PHYSICAL_FINAL_SOURCE_ROLE,
    SETTLEMENT_FINAL_SOURCE_ROLE,
    build_final_resolution_label_proposal,
)
from source_observation_runtime import build_source_observation


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"
DAY = "2026-07-03"


def _observation(
    *,
    source_id: str,
    role: str,
    payload: dict,
    raw: str,
    event_date: str = DAY,
) -> dict:
    return build_source_observation(
        source_id=source_id,
        station_id="ZUUU",
        event_date=event_date,
        valid_time=f"{event_date}T19:00:00Z",
        fetched_at=f"{event_date}T19:01:00Z",
        parsed_at=f"{event_date}T19:01:01Z",
        response_completed_at=f"{event_date}T19:00:59Z",
        raw_ref=f"research://final-labels/{source_id}/{event_date}.json",
        raw_payload=raw,
        payload=payload,
        overrides={
            "source_class": "settlement_source",
            "role": role,
            "hard_eligible": False,
            "demotion_reason": "final_label_only_not_realtime_weather_evidence",
        },
    ).as_dict()


def _contract() -> dict:
    return {
        "resolution_contract_id": "zuuu_resolution_20260703_v1",
        "target_identity": TARGET,
        "target_station_id": "ZUUU",
        "resolution_source_id": "zuuu_resolution_final",
        "resolution_local_date": DAY,
        "settlement_unit": "C",
        "physical_to_settlement_kernel": {"30": [{"settlement_value_c": 31.0, "probability": 1.0}]},
        "bucket_contract": {"31C": {"lower_inclusive": 30.5, "upper_exclusive": 31.5}},
        "bridge_closed": True,
        "validated": True,
    }


def test_final_source_observation_proposal_builds_strict_x_s_b_label() -> None:
    proposal = build_final_resolution_label_proposal(
        target_identity=TARGET,
        event_date=DAY,
        physical_final_observation=_observation(
            source_id="zuuu_physical_final",
            role=PHYSICAL_FINAL_SOURCE_ROLE,
            payload={"final_physical_high_c": 30.0, "label_finality": "final"},
            raw="physical-final-raw",
        ),
        settlement_final_observation=_observation(
            source_id="zuuu_resolution_final",
            role=SETTLEMENT_FINAL_SOURCE_ROLE,
            payload={"settlement_scalar_c": 31.0, "label_finality": "final"},
            raw="settlement-final-raw",
        ),
        resolution_contract=_contract(),
        label_available_at="2026-07-03T19:02:00Z",
        asserted_market_bucket="31C",
    )

    assert proposal["ok"] is True
    assert proposal["state"] == "proposal_ready"
    assert proposal["resolution_final_label"]["final_physical_high_c"] == 30.0
    assert proposal["resolution_final_label"]["settlement_scalar_c"] == 31.0
    assert proposal["resolution_final_label"]["market_settlement_bucket"] == "31C"


def test_intraday_or_nonfinal_or_mismatched_final_sources_fail_closed() -> None:
    physical = _observation(
        source_id="zuuu_physical_final",
        role=PHYSICAL_FINAL_SOURCE_ROLE,
        payload={"final_physical_high_c": 30.0, "label_finality": "final"},
        raw="physical-final-raw",
    )
    intraday = _observation(
        source_id="zuuu_resolution_final",
        role="metar_hard_observation_primary",
        payload={"settlement_scalar_c": 31.0, "label_finality": "final"},
        raw="settlement-final-raw",
    )
    nonfinal = _observation(
        source_id="zuuu_resolution_final",
        role=SETTLEMENT_FINAL_SOURCE_ROLE,
        payload={"settlement_scalar_c": 31.0, "label_finality": "provisional"},
        raw="settlement-final-raw",
    )
    wrong_day = _observation(
        source_id="zuuu_resolution_final",
        role=SETTLEMENT_FINAL_SOURCE_ROLE,
        payload={"settlement_scalar_c": 31.0, "label_finality": "final"},
        raw="settlement-final-raw",
        event_date="2026-07-04",
    )

    for settlement, expected in (
        (intraday, "settlement_source_role_invalid"),
        (nonfinal, "settlement_payload_finality_not_final"),
        (wrong_day, "settlement_event_date_mismatch"),
    ):
        proposal = build_final_resolution_label_proposal(
            target_identity=TARGET,
            event_date=DAY,
            physical_final_observation=physical,
            settlement_final_observation=settlement,
            resolution_contract=_contract(),
            label_available_at="2026-07-03T19:02:00Z",
        )
        assert proposal["ok"] is False
        assert expected in proposal["block_reasons"]
