from __future__ import annotations

from scripts.build_bayes_basket_target_station_calibration_dataset import build


def _packet(observations):
    return {
        "schema": "bayes_basket_target_canonical_observation_evidence_v1",
        "evidence_sha256": "a" * 64,
        "observations": observations,
    }


def _row(*, station="ZBAA", lane="amsc", stamp, temp, report="METAR"):
    return {
        "station_id": station,
        "source_lane": lane,
        "report_type": report,
        "valid_at_utc": stamp,
        "reported_temperature_c": temp,
        "raw_sha256": (stamp + str(temp)).encode().hex()[:64].ljust(64, "0"),
        "qc_status": "ACCEPTED",
    }


def test_amsc_uses_only_whole_hour_metar_and_keeps_daily_high():
    result = build(
        payloads=[_packet([
            _row(stamp="2026-06-27T16:00:00Z", temp=20),
            _row(stamp="2026-06-28T04:30:00Z", temp=40),
            _row(stamp="2026-06-28T05:00:00Z", temp=31),
            _row(
                stamp="2026-06-28T06:00:00Z",
                temp=45,
                report="SPECI",
            ),
        ])],
        station_id="ZBAA",
        settlement_timezone="Asia/Shanghai",
        source_lane="AMSC",
        start_date="2026-06-28",
        end_date="2026-06-28",
        as_of_utc="2026-06-29T00:00:00Z",
    )

    assert result["checks"]["complete_date_coverage"] is True
    assert result["samples"][0]["truth"]["tmax_c"] == 31
    assert len(result["samples"][0]["truth"]["observations"]) == 2


def test_cross_lane_is_fail_closed():
    try:
        build(
            payloads=[_packet([
                _row(
                    station="ZBAA",
                    lane="awc",
                    stamp="2026-06-28T05:00:00Z",
                    temp=31,
                ),
            ])],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AMSC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2026-06-29T00:00:00Z",
        )
    except Exception as exc:
        assert "cross_lane_forbidden" in str(exc)
    else:
        raise AssertionError("cross-lane truth must fail closed")


def test_unclosed_or_missing_day_is_not_materialized():
    result = build(
        payloads=[_packet([])],
        station_id="KATL",
        settlement_timezone="America/New_York",
        source_lane="AWC",
        start_date="2026-06-28",
        end_date="2026-06-29",
        as_of_utc="2026-06-29T02:00:00Z",
    )

    assert result["samples"] == []
    assert result["missing_dates"] == ["2026-06-28", "2026-06-29"]


def test_temporary_awc_label_requires_explicit_reference_lineage():
    reference = {
        "schema": "bayes_basket_target_truth_reference_evidence_v1",
        "evidence_sha256": "b" * 64,
        "reference_observations": [{
            **_row(
                station="ZBAA",
                lane="awc",
                stamp="2026-06-28T05:00:00Z",
                temp=31,
            ),
            "evidence_role": "REFERENCE_ONLY",
            "reference_reason":
                "awc_hourly_reference_only_for_amsc_station",
        }, {
            **_row(
                station="ZBAA",
                lane="amsc",
                stamp="2026-06-28T05:30:00Z",
                temp=45,
            ),
            "evidence_role": "REFERENCE_ONLY",
            "reference_reason": "amsc_half_hour_reference_only",
        }],
    }
    result = build(
        payloads=[reference],
        station_id="ZBAA",
        settlement_timezone="Asia/Shanghai",
        source_lane="AWC",
        start_date="2026-06-28",
        end_date="2026-06-28",
        as_of_utc="2026-06-29T00:00:00Z",
        temporary_awc_calibration_label=True,
    )

    assert result["label_authority"] == "TEMPORARY_AWC_CALIBRATION_LABEL"
    assert result["samples"][0]["truth"]["tmax_c"] == 31
    assert result["boundary"]["enters_realtime_truth"] is False
    assert result["boundary"]["enters_hard_no"] is False


def test_reference_packet_is_rejected_without_temporary_authority():
    reference = {
        "schema": "bayes_basket_target_truth_reference_evidence_v1",
        "reference_observations": [],
    }
    try:
        build(
            payloads=[reference],
            station_id="ZBAA",
            settlement_timezone="Asia/Shanghai",
            source_lane="AWC",
            start_date="2026-06-28",
            end_date="2026-06-28",
            as_of_utc="2026-06-29T00:00:00Z",
        )
    except Exception as exc:
        assert "input_schema_invalid" in str(exc)
    else:
        raise AssertionError("reference input must require explicit authority")
