from __future__ import annotations

from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.resource_bounds import write_bounded_json
from scripts.merge_bayes_basket_target_historical_truth_observations import _registry_timezone, merge


def _row(*, station: str, lane: str, valid: str, raw: str, temperature: float, report_type: str = "METAR", qc: str = "ACCEPTED", first_seen: str = "2026-07-24T10:01:00Z", committed: str = "2026-07-24T10:02:00Z") -> dict[str, object]:
    return {
        "station_id": station,
        "source_lane": lane,
        "report_type": report_type,
        "valid_at_utc": valid,
        "reported_temperature_c": temperature,
        "raw_sha256": raw * 64,
        "source_poll_id": f"poll-{raw}",
        "first_seen_at_utc": first_seen,
        "canonical_committed_at_utc": committed,
        "correction_suspected": False,
        "qc_status": qc,
    }


def _packet(path: Path, rows: list[dict[str, object]]) -> None:
    write_bounded_json(
        path,
        {
            "schema": "bayes_basket_target_canonical_observation_evidence_v1",
            "observations": rows,
        },
    )


def _reference_packet(path: Path, rows: list[dict[str, object]]) -> None:
    write_bounded_json(
        path,
        {
            "schema": "bayes_basket_target_truth_reference_evidence_v1",
            "reference_observations": rows,
        },
    )


def test_amsc_hourly_is_formal_and_half_or_awc_hourly_is_reference(tmp_path: Path) -> None:
    packet = tmp_path / "packet.json"
    _packet(
        packet,
        [
            _row(station="ZBAA", lane="amsc", valid="2026-07-24T10:00:00Z", raw="a", temperature=30),
            _row(station="ZBAA", lane="amsc", valid="2026-07-24T10:30:00Z", raw="b", temperature=31),
            _row(station="ZBAA", lane="awc", valid="2026-07-24T10:00:00Z", raw="c", temperature=29),
            _row(station="KATL", lane="awc", valid="2026-07-24T10:00:00Z", raw="d", temperature=28),
        ],
    )

    result = merge(
        base=packet,
        additional=[],
        event_dates=[],
        output=tmp_path / "out.json",
        station_lane_map={"ZBAA": "amsc", "KATL": "awc"},
        as_of_utc="2026-07-25T00:00:00Z",
        target_timezone="UTC",
    )

    assert result["state"] == "EXPORTED"
    payload = __import__("json").loads((tmp_path / "out.json").read_text())
    assert {(row["station_id"], row["source_lane"], row["valid_at_utc"]) for row in payload["observations"]} == {
        ("ZBAA", "amsc", "2026-07-24T10:00:00.000Z"),
        ("KATL", "awc", "2026-07-24T10:00:00.000Z"),
    }
    assert payload["reference_reason_counts"] == {
        "amsc_half_hour_reference_only": 1,
        "awc_hourly_reference_only_for_amsc_station": 1,
    }
    assert payload["truth_rows"]
    assert payload["checks"]["strict_timestamp_ordering"] is True
    assert all(row["source_lane"] == "amsc" or row["station_id"] == "KATL" for row in payload["truth_rows"])
    assert all(
        row["observation_id"].startswith("histobs_")
        and row["event_date_local"] == "2026-07-24"
        and row["qc_status"] == "ACCEPTED"
        and row["evidence_role"] == "FORMAL"
        for row in payload["observations"]
    )


def test_amsc_hourly_remains_formal_when_awc_arrives_first(tmp_path: Path) -> None:
    """Arrival order must not turn the AWC cross-check into formal truth."""

    packet = tmp_path / "packet.json"
    _packet(
        packet,
        [
            _row(
                station="ZBAA",
                lane="awc",
                valid="2026-07-24T10:00:00Z",
                raw="a",
                temperature=29,
                first_seen="2026-07-24T10:00:01Z",
                committed="2026-07-24T10:00:02Z",
            ),
            _row(
                station="ZBAA",
                lane="amsc",
                valid="2026-07-24T10:00:00Z",
                raw="b",
                temperature=30,
                first_seen="2026-07-24T10:05:00Z",
                committed="2026-07-24T10:06:00Z",
            ),
        ],
    )

    result = merge(
        base=packet,
        additional=[],
        event_dates=[],
        output=tmp_path / "out.json",
        station_lane_map={"ZBAA": "amsc"},
        as_of_utc="2026-07-25T00:00:00Z",
        target_timezone="UTC",
    )

    assert result["state"] == "EXPORTED"
    payload = __import__("json").loads((tmp_path / "out.json").read_text())
    assert [(row["source_lane"], row["official_tmax_c"]) for row in payload["truth_rows"]] == [("amsc", 30)]
    assert payload["reference_reason_counts"] == {"awc_hourly_reference_only_for_amsc_station": 1}


def test_same_slot_revision_is_quarantined_without_blocking_packet(tmp_path: Path) -> None:
    packet = tmp_path / "packet.json"
    _packet(
        packet,
        [
            _row(station="ZBAA", lane="amsc", valid="2026-07-24T10:00:00Z", raw="a", temperature=30),
            _row(station="ZBAA", lane="amsc", valid="2026-07-24T10:00:00Z", raw="b", temperature=32),
            _row(station="KATL", lane="awc", valid="2026-07-24T10:00:00Z", raw="c", temperature=28),
        ],
    )

    result = merge(
        base=packet,
        additional=[],
        event_dates=[],
        output=tmp_path / "out.json",
        station_lane_map={"ZBAA": "amsc", "KATL": "awc"},
        as_of_utc="2026-07-25T00:00:00Z",
        target_timezone="UTC",
    )

    assert result["state"] == "EXPORTED"
    assert result["quarantined_observation_count"] == 2
    payload = __import__("json").loads((tmp_path / "out.json").read_text())
    assert payload["observation_count"] == 1
    assert payload["quarantine_reason_counts"] == {"same_slot_revision": 2}


def test_station_scope_excludes_non_target_lane_from_materialized_packet(tmp_path: Path) -> None:
    packet = tmp_path / "packet.json"
    _packet(
        packet,
        [
            _row(station="KATL", lane="awc", valid="2026-07-24T10:00:00Z", raw="a", temperature=28),
            _row(station="ZBAA", lane="amsc", valid="2026-07-24T10:00:00Z", raw="b", temperature=30),
        ],
    )

    result = merge(
        base=packet,
        additional=[],
        event_dates=[],
        output=tmp_path / "out.json",
        station_lane_map={"ZBAA": "amsc", "KATL": "awc"},
        station_ids={"KATL"},
        as_of_utc="2026-07-25T00:00:00Z",
        target_timezone="UTC",
    )

    assert result["state"] == "EXPORTED"
    payload = __import__("json").loads((tmp_path / "out.json").read_text())
    assert {row["station_id"] for row in payload["observations"]} == {"KATL"}
    assert payload["scope"]["included_station_ids"] == ["KATL"]
    assert payload["scope"]["excluded_source_lanes"] == ["amsc"]


def test_amsc_station_scope_marks_awc_excluded(tmp_path: Path) -> None:
    packet = tmp_path / "packet.json"
    _packet(
        packet,
        [_row(station="ZBAA", lane="amsc", valid="2026-07-24T10:00:00Z", raw="a", temperature=30)],
    )

    result = merge(
        base=packet,
        additional=[],
        event_dates=[],
        output=tmp_path / "out.json",
        station_lane_map={"ZBAA": "amsc", "KATL": "awc"},
        station_ids={"ZBAA"},
        as_of_utc="2026-07-25T00:00:00Z",
        target_timezone="Asia/Shanghai",
    )

    assert result["state"] == "EXPORTED"
    payload = __import__("json").loads((tmp_path / "out.json").read_text())
    assert payload["scope"]["excluded_source_lanes"] == ["awc"]


def test_registry_timezone_carries_invariant_station_timezone_to_historical_dates(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    write_bounded_json(
        registry,
        {
            "rows": [
                {
                    "station_id": "ZBAA",
                    "event_date": "2026-07-27",
                    "settlement_timezone": "Asia/Shanghai",
                },
                {
                    "station_id": "ZBAA",
                    "event_date": "2026-07-28",
                    "settlement_timezone": "Asia/Shanghai",
                },
            ]
        },
    )

    assert _registry_timezone(
        registry,
        station_ids=["ZBAA"],
        event_dates=["2026-07-15"],
    ) == "Asia/Shanghai"


def test_registry_timezone_rejects_mixed_station_timezones(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    write_bounded_json(
        registry,
        {
            "rows": [
                {
                    "station_id": "ZBAA",
                    "event_date": "2026-07-27",
                    "settlement_timezone": "Asia/Shanghai",
                },
                {
                    "station_id": "RCSS",
                    "event_date": "2026-07-27",
                    "settlement_timezone": "Asia/Taipei",
                },
            ]
        },
    )

    with pytest.raises(ContractError, match="market_registry_mixed_settlement_timezones_forbidden"):
        _registry_timezone(
            registry,
            station_ids=["ZBAA", "RCSS"],
            event_dates=["2026-07-15"],
        )


def test_non_hourly_amsc_metar_is_blocked_not_formal(tmp_path: Path) -> None:
    packet = tmp_path / "packet.json"
    _packet(
        packet,
        [_row(station="ZBAA", lane="amsc", valid="2026-07-24T10:15:00Z", raw="a", temperature=30)],
    )

    result = merge(
        base=packet,
        additional=[],
        event_dates=[],
        output=tmp_path / "out.json",
        station_lane_map={"ZBAA": "amsc"},
        as_of_utc="2026-07-25T00:00:00Z",
        target_timezone="Asia/Shanghai",
    )

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    payload = __import__("json").loads((tmp_path / "out.json").read_text())
    assert payload["observations"] == []
    assert any("amsc_metar_timing_invalid" in item for item in payload["blockers"])


def test_reference_packet_is_bounded_to_station_and_event_date_scope(tmp_path: Path) -> None:
    packet = tmp_path / "packet.json"
    reference = tmp_path / "reference.json"
    _packet(
        packet,
        [_row(station="ZBAA", lane="amsc", valid="2026-07-24T10:00:00Z", raw="a", temperature=30)],
    )
    _reference_packet(
        reference,
        [
            _row(station="ZBAA", lane="awc", valid="2026-07-24T10:00:00Z", raw="b", temperature=29),
            _row(station="ZBAA", lane="awc", valid="2026-07-23T10:00:00Z", raw="c", temperature=28),
            _row(station="KATL", lane="amsc", valid="2026-07-24T10:30:00Z", raw="d", temperature=27),
        ],
    )

    result = merge(
        base=packet,
        additional=[],
        event_dates=["2026-07-24"],
        output=tmp_path / "out.json",
        station_lane_map={"ZBAA": "amsc", "KATL": "awc"},
        station_ids={"ZBAA"},
        reference_paths=[reference],
        as_of_utc="2026-07-25T00:00:00Z",
        target_timezone="Asia/Shanghai",
    )

    assert result["state"] == "EXPORTED"
    payload = __import__("json").loads((tmp_path / "out.json").read_text())
    assert payload["reference_observation_count"] == 1
    assert payload["reference_lineage"]["reference_inputs"][0][
        "retained_unique_reference_observation_count"
    ] == 1
