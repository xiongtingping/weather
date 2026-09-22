"""Charter 193 amendment: the ECMWF D-3 18Z history backfill lane.

User ruling 2026-09-22: backfill thirty days of ECMWF D-3 18Z for D+2 days on
the native history lane, raw fields on the external cold volume, without
competing with production (which had just lost ten hours of P0 to network
errors).  These pin the lane's new behaviour, not its download.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import backfill_ecmwf_ifs_multistation_archive as capture_script
from scripts import run_native_history_backfill as lane


def _scope(station: str, tz: str) -> dict[str, object]:
    return {"station_id": station, "settlement_timezone": tz,
            "latitude": 0.0, "longitude": 0.0, "registry_rows": []}


def test_a_d3_18z_capture_keeps_only_the_stations_plus_72h_reaches() -> None:
    kept, left_out = capture_script.split_coverable(
        [
            _scope("ZUUU", "Asia/Shanghai"),
            _scope("EDDM", "Europe/Berlin"),
            _scope("KLGA", "America/New_York"),
            _scope("KSFO", "America/Los_Angeles"),
        ],
        event_date="2026-09-10",
        lead_days=3,
        run_cycle_hour=18,
    )
    assert [scope["station_id"] for scope in kept] == ["ZUUU", "EDDM"]
    assert left_out == ["KLGA", "KSFO"]


def test_the_lane_stands_aside_while_a_production_capture_runs() -> None:
    states = iter([True, True, False])
    slept: list[float] = []
    waited = lane.stand_aside_for_production_captures(
        max_seconds=600, running=lambda: next(states), sleep=slept.append,
        poll_seconds=15,
    )
    assert waited == 30 and slept == [15, 15]
    # never past its cap
    waited = lane.stand_aside_for_production_captures(
        max_seconds=45, running=lambda: True, sleep=lambda _s: None, poll_seconds=15,
    )
    assert waited == 45


def test_a_network_failure_is_told_apart_from_a_refusal() -> None:
    assert lane.transport_failure("ContractError: ecmwf_archive_transport_failed:ConnectError")
    assert lane.transport_failure("httpx.PoolTimeout")
    assert lane.transport_failure("capture_timeout")
    assert not lane.transport_failure("ecmwf_archive_member_index_incomplete")


def test_the_raw_root_must_be_on_another_device(tmp_path: Path) -> None:
    # tmp_path sits on the internal disk with the repository: refused, so an
    # unplugged volume can never turn into raw fields on the internal disk.
    with pytest.raises(SystemExit):
        lane._external_volume_root(tmp_path / "raw")
