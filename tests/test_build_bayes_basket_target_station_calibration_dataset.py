from __future__ import annotations

import inspect

import pytest

from bayes_basket_target.calibration_ingress import (
    HISTORICAL_AWC_REPLAY,
    NO_LIVE_REPLAY,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from scripts.build_bayes_basket_target_station_calibration_dataset import build


def _packet(observations, *, settlement_timezone="Asia/Shanghai"):
    identity = {
        "schema": "bayes_basket_target_historical_awc_replay_evidence_v2",
        "target_timezone": settlement_timezone,
        "historical_only": True,
        "canonical_ingress": True,
        "source_lane": "AWC",
        "source_role": HISTORICAL_AWC_REPLAY,
        "authority_scope": NO_LIVE_REPLAY,
        "truth_ready_dates": sorted({
            str(row["valid_at_utc"])[0:10] for row in observations
        }),
        "truth_blocked_dates": [],
        "checks": {"station_day_metar_envelope_explicit": True},
        "observations": observations,
        "boundary": {
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_arrival_evidence": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
        },
    }
    return {**identity, "evidence_sha256": content_sha256(identity)}


def _row(
    *,
    station="ZBAA",
    lane="awc",
    stamp,
    temp,
    report="METAR",
    settlement_timezone="Asia/Shanghai",
):
    return {
        "station_id": station,
        "source_lane": lane,
        "source_role": HISTORICAL_AWC_REPLAY,
        "authority_scope": NO_LIVE_REPLAY,
        "settlement_timezone": settlement_timezone,
        "evidence_role": "FORMAL",
        "reference_only": False,
        "report_type": report,
        "valid_at_utc": stamp,
        "reported_temperature_c": temp,
        "raw_sha256": (stamp + str(temp)).encode().hex()[:64].ljust(64, "0"),
        "qc_status": "ACCEPTED",
    }


def test_awc_uses_only_metar_and_speci_and_keeps_daily_high():
    result = build(
        payloads=[_packet([
            _row(stamp="2026-06-27T16:00:00Z", temp=20),
            _row(stamp="2026-06-28T04:30:00Z", temp=40),
            _row(stamp="2026-06-28T05:00:00Z", temp=31),
            _row(stamp="2026-06-28T06:00:00Z", temp=45, report="SPECI"),
        ])],
        station_id="ZBAA",
        settlement_timezone="Asia/Shanghai",
        source_lane="AWC",
        start_date="2026-06-28",
        end_date="2026-06-28",
        as_of_utc="2026-06-29T00:00:00Z",
    )

    assert result["checks"]["complete_date_coverage"] is True
    assert result["checks"]["formal_awc_truth_only"] is True
    assert result["checks"]["awc_hourly_metar_and_speci_only"] is True
    assert result["label_authority"] == "FORMAL_STATION_LANE_TRUTH"
    assert result["source_role"] == HISTORICAL_AWC_REPLAY
    assert result["authority_scope"] == NO_LIVE_REPLAY
    assert result["samples"][0]["truth"]["tmax_c"] == 45
    assert len(result["samples"][0]["truth"]["observations"]) == 3


def test_non_awc_source_lane_is_rejected():
    with pytest.raises(ContractError, match="scope_invalid"):
        build(
            payloads=[_packet([])],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AMSC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2026-06-29T00:00:00Z",
        )


def test_cross_lane_observation_is_fail_closed():
    with pytest.raises(ContractError, match="cross_lane_forbidden"):
        build(
            payloads=[_packet([
                _row(
                    station="ZBAA",
                    lane="amsc",
                    stamp="2026-06-28T05:00:00Z",
                    temp=31,
                ),
            ])],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AWC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2026-06-29T00:00:00Z",
        )


def test_reference_packet_is_rejected_without_compatibility_path():
    with pytest.raises(ContractError, match="input_schema_invalid"):
        build(
            payloads=[{
                "schema": "bayes_basket_target_truth_reference_evidence_v1",
                "reference_observations": [],
            }],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AWC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2026-06-29T00:00:00Z",
        )


def test_unclosed_or_missing_day_is_not_materialized():
    result = build(
        payloads=[_packet([], settlement_timezone="America/New_York")],
        station_id="KATL",
        settlement_timezone="America/New_York",
        source_lane="AWC",
        start_date="2026-06-28",
        end_date="2026-06-29",
        as_of_utc="2026-06-29T02:00:00Z",
    )

    assert result["samples"] == []
    assert result["missing_dates"] == ["2026-06-28", "2026-06-29"]


def test_reference_or_non_historical_role_is_fail_closed():
    reference = _row(stamp="2026-06-28T05:00:00Z", temp=31)
    reference["reference_only"] = True
    reference["evidence_role"] = "REFERENCE_ONLY"
    with pytest.raises(ContractError, match="observation_lineage_invalid"):
        build(
            payloads=[_packet([reference])],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AWC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2026-06-29T00:00:00Z",
        )

    spoofed = _row(stamp="2026-06-28T05:00:00Z", temp=31)
    spoofed["source_role"] = "AMSC_RETIRED_HISTORY_ONLY"
    with pytest.raises(ContractError, match="observation_lineage_invalid"):
        build(
            payloads=[_packet([spoofed])],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AWC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2026-06-29T00:00:00Z",
        )


def test_evidence_hash_and_timezone_are_exact():
    payload = _packet([_row(stamp="2026-06-28T05:00:00Z", temp=31)])
    payload["evidence_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="evidence_sha_mismatch"):
        build(
            payloads=[payload],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AWC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2026-06-29T00:00:00Z",
        )

    with pytest.raises(ContractError, match="input_lineage_invalid"):
        build(
            payloads=[_packet(
                [_row(stamp="2026-06-28T05:00:00Z", temp=31)],
                settlement_timezone="UTC",
            )],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AWC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2026-06-29T00:00:00Z",
        )


def test_future_as_of_and_non_numeric_temperature_fail_closed():
    with pytest.raises(ContractError, match="as_of_utc_in_future"):
        build(
            payloads=[_packet([])],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AWC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2999-01-01T00:00:00Z",
        )

    with pytest.raises(ContractError, match="temperature_invalid"):
        build(
            payloads=[_packet([
                _row(
                    stamp="2026-06-28T05:00:00Z",
                    temp="not-a-temperature",
                ),
            ])],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AWC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2026-06-29T00:00:00Z",
        )


def test_legacy_label_api_is_removed():
    assert set(inspect.signature(build).parameters) == {
        "payloads",
        "station_id",
        "settlement_timezone",
        "source_lane",
        "start_date",
        "end_date",
        "as_of_utc",
    }
