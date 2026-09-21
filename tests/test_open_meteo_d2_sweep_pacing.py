"""The daily republisher sweep must shape its own request rate.

45 stations x 3 models is 135 separate calls issued back to back.  The provider
prices per model-day, so an unpaced sweep arrives as a single burst against a
per-minute allowance; the 429s that followed were being retried rather than
avoided, which spends the quota that is refusing us.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest


def _load():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "capture_open_meteo_d2_ensemble_daily",
        root / "scripts" / "capture_open_meteo_d2_ensemble_daily.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sweep = _load()


class _Response:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {}


class _HttpError(Exception):
    def __init__(self, response: _Response):
        self.response = response


def test_requests_are_held_apart() -> None:
    started = time.monotonic()
    for _ in range(5):
        sweep._pace_request()
    elapsed = time.monotonic() - started

    # Four gaps between five calls.
    assert elapsed >= 4 * sweep.MIN_REQUEST_INTERVAL_SECONDS - 0.02


def test_pacing_still_finishes_well_inside_the_sweep_cadence() -> None:
    # A pace that outran the 15-minute cadence would turn every sweep into a
    # permanently in-flight one.
    stations, models = 45, 3
    sweep_seconds = stations * models * sweep.MIN_REQUEST_INTERVAL_SECONDS

    assert sweep_seconds < 120


def test_rate_limit_prefers_the_servers_own_retry_after() -> None:
    exc = _HttpError(_Response(429, {"Retry-After": "7"}))

    assert sweep._retry_sleep_seconds(exc, attempt=0) == 7.0


def test_retry_after_is_capped() -> None:
    exc = _HttpError(_Response(429, {"Retry-After": "9999"}))

    assert sweep._retry_sleep_seconds(exc, attempt=0) == (
        sweep.MAX_RETRY_SLEEP_SECONDS
    )


def test_rate_limit_backs_off_harder_than_a_socket_error() -> None:
    # A refused quota needs materially longer than a dropped connection.
    rate_limited = sweep._retry_sleep_seconds(
        _HttpError(_Response(429)), attempt=1
    )
    transport = sweep._retry_sleep_seconds(OSError("reset"), attempt=1)

    assert rate_limited > transport
    assert rate_limited <= sweep.MAX_RETRY_SLEEP_SECONDS


def test_a_station_outside_the_coordinate_registry_does_not_abort_the_sweep(
    tmp_path, monkeypatch,
) -> None:
    """One unlisted station must cost its own rows, not the whole pass.

    The scope is the market registry and the model scope is the coordinate
    registry, so a newly listed station exists in one and not the other.
    Raising on it killed the sweep before a single row was written, and this
    lane is the only route UKMO, WeatherNext and AI-GEFS have -- forward-only
    members the provider drops after about five days.
    """

    database = tmp_path / "scope.db"
    database.write_text("", encoding="utf-8")

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return list(self._rows)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, *args):
            rows = []
            if "basket_target_market_registry" in sql:
                rows = [
                    ("KLGA", "America/New_York"),
                    ("LLBG", "Asia/Jerusalem"),
                ]
            return _Cursor(rows)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sweep.sqlite3, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(sweep.httpx, "Client", _Client)
    monkeypatch.setattr(
        sweep,
        "_capture_station_model",
        lambda client, **kw: {
            "station_id": kw["station_id"],
            "open_meteo_model": kw["model"],
        },
    )

    output = tmp_path / "om_d2.json"
    monkeypatch.setattr(sys, "argv", [
        "capture_open_meteo_d2_ensemble_daily.py",
        "--db", str(database),
        "--output", str(output),
        "--target-date", "2026-09-23",
    ])

    assert sweep.main() == 0

    report = json.loads(output.read_text(encoding="utf-8"))
    captured = {row["station_id"] for row in report["captures"]}
    assert captured == {"KLGA"}
    assert len(report["captures"]) == len(sweep.TARGET_MODELS)
    refused = {row["station_id"]: row for row in report["failures"]}
    assert set(refused) == {"LLBG"}
    assert "native_station_coordinates_missing" in refused["LLBG"]["error"]
