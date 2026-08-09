"""The daily republisher sweep must shape its own request rate.

45 stations x 3 models is 135 separate calls issued back to back.  The provider
prices per model-day, so an unpaced sweep arrives as a single burst against a
per-minute allowance; the 429s that followed were being retried rather than
avoided, which spends the quota that is refusing us.
"""

from __future__ import annotations

import importlib.util
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
