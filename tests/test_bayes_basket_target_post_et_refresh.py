from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.bayes_basket_target_post_et_refresh import run


def test_post_et_gate_waits_without_invoking_refresh(tmp_path: Path) -> None:
    called = False

    def refresh(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("refresh_must_not_run_before_et_close")

    result = run(
        event_date="2026-07-24",
        settlement_timezone="America/New_York",
        as_of_utc="2026-07-24T01:00:00Z",
        receipt_path=tmp_path / "gate.json",
        refresh_runner=refresh,
    )

    assert result["state"] == "WAITING_FOR_SETTLEMENT_CLOSE"
    assert result["refresh_invoked"] is False
    assert called is False
    assert result["boundary"]["reads_shared_live_database"] is False
    assert result["window"]["state"] == "not_started"


def test_post_et_gate_invokes_only_after_close(tmp_path: Path) -> None:
    calls: list[object] = []

    def refresh(**kwargs):
        calls.append((str(kwargs["as_of_utc"]), kwargs.get("station_ids")))
        return {"state": "BLOCKED_EVIDENCE", "writes_target_database": False}

    lane_map = tmp_path / "lane.json"
    lane_map.write_text(json.dumps({"stations": {"KAAA": "awc"}}), encoding="utf-8")
    result = run(
        event_date="2026-07-24",
        settlement_timezone="America/New_York",
        as_of_utc="2026-07-25T04:00:00Z",
        receipt_path=tmp_path / "gate.json",
        station_ids=["KAAA"],
        lane_map_path=lane_map,
        refresh_runner=refresh,
    )

    assert result["state"] == "BLOCKED_EVIDENCE"
    assert result["refresh_invoked"] is True
    assert calls == [("2026-07-25T04:00:00Z", {"KAAA"})]
    assert result["boundary"]["reads_shared_live_database"] is True
    assert result["window"]["state"] == "closed"


def test_post_et_gate_forwards_historical_packets_without_touching_boundary(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    canonical = tmp_path / "canonical.json"
    reference = tmp_path / "reference.json"

    def refresh(**kwargs):
        calls.append(kwargs)
        return {"state": "READY_FOR_STRICT_ASOF", "writes_target_database": False}

    lane_map = tmp_path / "lane.json"
    lane_map.write_text(json.dumps({"stations": {"KAAA": "awc"}}), encoding="utf-8")
    result = run(
        event_date="2026-07-24",
        settlement_timezone="America/New_York",
        as_of_utc="2026-07-25T04:00:00Z",
        receipt_path=tmp_path / "gate.json",
        historical_canonical_paths=[canonical],
        historical_reference_paths=[reference],
        station_ids=["KAAA"],
        lane_map_path=lane_map,
        refresh_runner=refresh,
    )

    assert result["state"] == "READY_FOR_STRICT_ASOF"
    assert calls == [{
        "as_of_utc": "2026-07-25T04:00:00Z",
        "station_ids": {"KAAA"},
        "historical_canonical_paths": [canonical.resolve()],
        "historical_reference_paths": [reference.resolve()],
    }]
    assert result["boundary"]["writes_shared_live_database"] is False
    assert result["station_scope"]["source_lane"] == "AWC_ONLY"


def test_post_et_china_scope_remains_amsc_owned_with_awc_reference(
    tmp_path: Path,
) -> None:
    lane_map = tmp_path / "lane.json"
    lane_map.write_text(json.dumps({"stations": {"ZBAA": "amsc"}}), encoding="utf-8")
    reference = tmp_path / "awc_reference.json"
    reference.write_text("{}", encoding="utf-8")

    result = run(
        event_date="2026-07-24",
        settlement_timezone="Asia/Shanghai",
        as_of_utc="2026-07-24T16:00:00Z",
        receipt_path=tmp_path / "gate.json",
        historical_reference_paths=[reference],
        station_ids=["ZBAA"],
        lane_map_path=lane_map,
        refresh_runner=lambda **_: {"state": "BLOCKED_EVIDENCE"},
    )

    assert result["station_scope"]["source_lane"] == "AMSC_ONLY"
    assert result["state"] == "BLOCKED_EVIDENCE"


def test_post_et_gate_rejects_artifact_from_another_event_day(tmp_path: Path) -> None:
    artifact = tmp_path / "native.json"
    artifact.write_text(
        json.dumps(
            {
                "schema": "native",
                "as_of_utc": "2026-07-25T04:00:00Z",
                "curves": {
                    "eligible_model_rows": [{"event_date": "2026-07-25"}],
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="native_decode_artifact_event_date_mismatch"):
        run(
            event_date="2026-07-24",
            settlement_timezone="America/New_York",
            as_of_utc="2026-07-25T04:00:00Z",
            receipt_path=tmp_path / "gate.json",
            native_decode=artifact,
            refresh_runner=lambda **_: {"state": "BLOCKED_EVIDENCE"},
        )


def test_post_et_gate_rejects_forward_arrival_artifact_for_closed_day(tmp_path: Path) -> None:
    artifact = tmp_path / "arrival.json"
    artifact.write_text(
        json.dumps(
            {
                "schema": "arrival",
                "as_of_utc": "2026-07-25T04:00:00Z",
                "arrivals": [{"target_local_date": "2026-07-25"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="model_arrival_artifact_event_date_mismatch"):
        run(
            event_date="2026-07-24",
            settlement_timezone="America/New_York",
            as_of_utc="2026-07-25T04:00:00Z",
            receipt_path=tmp_path / "gate.json",
            model_arrival_evidence=artifact,
            refresh_runner=lambda **_: {"state": "BLOCKED_EVIDENCE"},
        )


def test_post_et_gate_rejects_artifact_with_different_asof(tmp_path: Path) -> None:
    artifact = tmp_path / "native.json"
    artifact.write_text(
        json.dumps(
            {
                "schema": "native",
                "as_of_utc": "2026-07-25T03:59:59Z",
                "curves": {
                    "eligible_model_rows": [{"event_date": "2026-07-24"}],
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="native_decode_artifact_as_of_mismatch"):
        run(
            event_date="2026-07-24",
            settlement_timezone="America/New_York",
            as_of_utc="2026-07-25T04:00:00Z",
            receipt_path=tmp_path / "gate.json",
            native_decode=artifact,
            refresh_runner=lambda **_: {"state": "BLOCKED_EVIDENCE"},
        )
