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
                    ("ZZZZ", "Asia/Jerusalem"),
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
    assert set(refused) == {"ZZZZ"}
    assert "native_station_coordinates_missing" in refused["ZZZZ"]["error"]


def _quota_client(bodies):
    """An httpx client whose answers come from ``bodies`` in order."""

    import httpx

    calls = []

    def handler(request):
        calls.append(str(request.url))
        status, body = bodies[min(len(calls), len(bodies)) - 1]
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_the_daily_allowance_is_not_retried(monkeypatch) -> None:
    """A 429 that names the day is terminal for the pass, not a rate to wait out."""

    monkeypatch.setattr(sweep, "_pace_request", lambda: None)
    monkeypatch.setattr(sweep.time, "sleep", lambda seconds: None)
    client, calls = _quota_client([(429, {
        "error": True,
        "reason": "Daily API request limit exceeded. Please try again tomorrow.",
    })])

    with pytest.raises(sweep.DailyQuotaExhausted, match="daily_quota_exhausted"):
        sweep._fetch(client, "https://ensemble-api.open-meteo.com/v1/ensemble")
    assert len(calls) == 1


def test_a_per_minute_limit_is_still_retried(monkeypatch) -> None:
    monkeypatch.setattr(sweep, "_pace_request", lambda: None)
    monkeypatch.setattr(sweep.time, "sleep", lambda seconds: None)
    client, calls = _quota_client([
        (429, {"error": True, "reason": "Minutely API request limit exceeded."}),
        (200, {"ok": True}),
    ])

    payload, _digest = sweep._fetch(
        client, "https://ensemble-api.open-meteo.com/v1/ensemble"
    )
    assert json.loads(payload) == {"ok": True}
    assert len(calls) == 2


def _run_sweep(tmp_path, monkeypatch, capture, *, commit):
    """Drive main() over two fleet stations with a scripted capture."""

    database = tmp_path / "scope.db"
    database.write_text("", encoding="utf-8")
    ingested = []

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
            if "basket_target_market_registry" in sql:
                return _Cursor([
                    ("KLGA", "America/New_York"),
                    ("ZUUU", "Asia/Shanghai"),
                ])
            return _Cursor([])

        def commit(self):
            return None

        def rollback(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sweep.sqlite3, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(sweep.httpx, "Client", _Client)
    monkeypatch.setattr(sweep, "_capture_station_model", capture)
    monkeypatch.setattr(
        sweep, "_ingest",
        lambda conn, report: ingested.append(report) or {"wrote": 1},
    )
    output = tmp_path / "om_d2.json"
    argv = [
        "capture_open_meteo_d2_ensemble_daily.py",
        "--db", str(database), "--output", str(output),
        "--target-date", "2026-09-23",
    ] + (["--commit"] if commit else [])
    monkeypatch.setattr(sys, "argv", argv)
    code = sweep.main()
    return code, json.loads(output.read_text(encoding="utf-8")), ingested


def test_quota_mid_pass_keeps_what_was_captured(tmp_path, monkeypatch) -> None:
    """A d2 member not stored today is gone for good, so a partial pass lands."""

    calls = []

    def capture(client, **kw):
        calls.append((kw["station_id"], kw["model"]))
        if len(calls) > 2:
            raise sweep.DailyQuotaExhausted(
                "open_meteo_d2_daily_quota_exhausted:Daily API request limit exceeded."
            )
        return {"station_id": kw["station_id"], "open_meteo_model": kw["model"]}

    code, report, ingested = _run_sweep(tmp_path, monkeypatch, capture, commit=True)

    assert len(calls) == 3  # stopped at the refusal; ZUUU never asked
    assert report["daily_quota_exhausted"] is True
    assert report["capture_count"] == 2
    assert len(ingested) == 1
    assert code == 0


def test_quota_before_any_capture_writes_the_report_and_exits_75(
    tmp_path, monkeypatch,
) -> None:
    def capture(client, **kw):
        raise sweep.DailyQuotaExhausted(
            "open_meteo_d2_daily_quota_exhausted:Daily API request limit exceeded."
        )

    code, report, ingested = _run_sweep(tmp_path, monkeypatch, capture, commit=True)

    assert code == 75
    assert report["daily_quota_exhausted"] is True
    assert report["capture_count"] == 0
    assert report["failure_count"] == 1
    assert ingested == []
