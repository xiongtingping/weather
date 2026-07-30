from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import capture_open_meteo_aux_nwp as capture


class _Response:
    def __init__(self, payload, url: str):
        self._raw = json.dumps(payload).encode()
        self.url = url
        self.headers = {"Content-Length": str(len(self._raw))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield self._raw


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.calls = []

    def get(self, endpoint, *, params, timeout, stream):
        self.calls.append((endpoint, params, timeout, stream))
        return _Response(self.payload, endpoint + "?bounded=true")


def _point_payload(latitude: float, longitude: float):
    times = [
        f"2026-07-28T{hour:02d}:00" for hour in range(24)
    ]
    return {
        "latitude": latitude,
        "longitude": longitude,
        "elevation": 10.0,
        "hourly_units": {
            "temperature_2m": "°C",
            "relative_humidity_2m": "%",
            "precipitation": "mm",
            "cloud_cover": "%",
            "wind_speed_10m": "m/s",
        },
        "hourly": {
            "time": times,
            "temperature_2m": [20.0 + hour / 10 for hour in range(24)],
            "relative_humidity_2m": [50.0] * 24,
            "precipitation": [0.0] * 24,
            "cloud_cover": [10.0] * 24,
            "wind_speed_10m": [2.0] * 24,
        },
    }


def test_gem_and_cma_contracts_use_dedicated_current_endpoints() -> None:
    assert capture.PROVIDERS["gem_global_open_meteo"]["endpoint"].endswith(
        "/v1/gem"
    )
    assert capture.PROVIDERS["gem_global_open_meteo"]["model"] == "gem_global"
    assert capture.PROVIDERS[
        "cma_grapes_global_open_meteo"
    ]["endpoint"].endswith("/v1/cma")
    assert capture.PROVIDERS[
        "cma_grapes_global_open_meteo"
    ]["model"] == "cma_grapes_global"


def test_capture_is_native_three_hour_curve_and_never_claims_arrival(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        _point_payload(35.2, 128.9),
        _point_payload(37.5, 126.5),
    ]
    session = _Session(payload)
    monkeypatch.setattr(
        capture,
        "assert_process_memory_budget",
        lambda **_: None,
    )
    receipt = capture.capture_provider(
        provider_id="gem_global_open_meteo",
        output_dir=tmp_path,
        station_ids=["RKPK", "RKSI"],
        forecast_days=2,
        batch_size=2,
        timeout_seconds=1.0,
        max_response_bytes=1024 * 1024,
        max_capture_bytes=2 * 1024 * 1024,
        min_free_bytes=capture.MIN_FREE_BYTES_HARD,
        retention_count=2,
        session=session,
    )
    artifact = json.loads(Path(receipt["artifact_path"]).read_text())

    assert artifact["station_count"] == 2
    assert artifact["run_initialization_utc"] is None
    assert artifact["native_publication_at_utc"] is None
    assert artifact["arrival_at_utc"] is None
    assert artifact["authority"]["source_class"] == "aggregator_republisher"
    assert artifact["authority"]["calibration_candidate_eligible"] is True
    assert artifact["authority"]["native_arrival_index_eligible"] is False
    assert artifact["authority"]["formal_calibration_eligible"] is False
    assert len(artifact["stations"][0]["points"]) == 8
    assert {
        int(row["valid_time_utc"][11:13])
        for row in artifact["stations"][0]["points"]
    } == set(range(0, 24, 3))
    assert session.calls[0][1]["models"] == "gem_global"


def test_response_and_capture_limits_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        capture,
        "assert_process_memory_budget",
        lambda **_: None,
    )
    with pytest.raises(ValueError, match="response_budget_invalid"):
        capture.capture_provider(
            provider_id="cma_grapes_global_open_meteo",
            output_dir=tmp_path,
            station_ids=["RKPK"],
            forecast_days=2,
            batch_size=1,
            timeout_seconds=1.0,
            max_response_bytes=capture.MAX_RESPONSE_BYTES_HARD + 1,
            max_capture_bytes=capture.MAX_CAPTURE_BYTES_HARD,
            min_free_bytes=capture.MIN_FREE_BYTES_HARD,
            retention_count=1,
            session=_Session([_point_payload(35.2, 128.9)]),
        )


def test_incomplete_trailing_native_points_are_omitted() -> None:
    payload = _point_payload(35.2, 128.9)
    for field in capture.HOURLY_FIELDS:
        payload["hourly"][field][21] = None

    rows = capture._normalize_batch(
        payload,
        station_ids=["RKPK"],
        temporal_resolution_hours=3,
    )

    assert len(rows[0]["points"]) == 7
    assert rows[0]["availability"]["incomplete_point_count_omitted"] == 1
    assert all(
        point["temperature_2m"] is not None
        for point in rows[0]["points"]
    )


def test_retention_deletes_only_old_provider_capture_files(
    tmp_path: Path,
) -> None:
    old = tmp_path / "gem_global_open_meteo_1.json"
    middle = tmp_path / "gem_global_open_meteo_2.json"
    latest = tmp_path / "gem_global_open_meteo_3.json"
    other = tmp_path / "cma_grapes_global_open_meteo_1.json"
    for path in (old, middle, latest, other):
        path.write_text("{}")
    old.touch()
    middle.touch()
    latest.touch()

    capture._retain_latest(tmp_path, "gem_global_open_meteo", 2)

    assert len(list(tmp_path.glob("gem_global_open_meteo_*.json"))) == 2
    assert other.exists()
