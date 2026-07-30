from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from official_nwp_request_control import parse_retry_after, record_provider_response, request_control_path, reserve_request_slot


def test_retry_after_accepts_delta_and_http_date() -> None:
    assert parse_retry_after("12", now_epoch=100.0) == 12.0
    stamp = datetime.fromtimestamp(112.0, tz=timezone.utc)
    assert parse_retry_after(stamp.strftime("%a, %d %b %Y %H:%M:%S GMT"), now_epoch=100.0) == 12.0


def test_reservation_serializes_bursts_across_calls(tmp_path: Path) -> None:
    state = tmp_path / "ecmwf" / "request_control.json"
    sleeps: list[float] = []
    first = reserve_request_slot(path=state, provider_id="ecmwf", min_interval_sec=2.0, now_epoch=100.0, sleep_fn=sleeps.append)
    second = reserve_request_slot(path=state, provider_id="ecmwf", min_interval_sec=2.0, now_epoch=100.0, sleep_fn=sleeps.append)
    assert first["wait_sec"] == 0.0
    assert second["wait_sec"] == 2.0
    assert sleeps == [2.0]


def test_429_retry_after_becomes_persistent_cooldown(tmp_path: Path) -> None:
    state = tmp_path / "request_control.json"
    result = record_provider_response(
        path=state,
        provider_id="ecmwf",
        status_code=429,
        headers={"Retry-After": "30"},
        now_epoch=100.0,
    )
    sleeps: list[float] = []
    slot = reserve_request_slot(path=state, provider_id="ecmwf", min_interval_sec=2.0, now_epoch=100.0, sleep_fn=sleeps.append)
    assert result["throttled"] is True
    assert result["retry_after_sec"] == 30.0
    assert slot["wait_sec"] == 30.0
    assert sleeps == [30.0]


def test_curve_and_live_roots_share_provider_gate(tmp_path: Path) -> None:
    curve = tmp_path / "official_nwp_curve_sources_v2"
    live = tmp_path / "official_nwp_sources"
    assert request_control_path(curve, "ecmwf") == request_control_path(live, "ecmwf")
