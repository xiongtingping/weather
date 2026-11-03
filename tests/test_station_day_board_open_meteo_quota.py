"""The station board's hourly forecast panel and the shared Open-Meteo ledger.

2026-10-11: a research pull spent the egress address's daily allowance at
09:41Z and the panel read 「逐小时云与辐射：预报读不到（HTTPError: HTTP Error 429:
Too Many Requests）」 until midnight, retrying on every refresh.  The panel now
charges its own lane of the ledger, answers a spent day without a request, and
never marks the day spent for the production lanes.
"""

from __future__ import annotations

import io
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

from bayes_basket_target import open_meteo_quota as Q
from bayes_basket_target import station_day_board as B


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    monkeypatch.setattr(Q, "LEDGER_PATH", path)
    monkeypatch.setattr(B, "_FORECAST_CACHE", {})
    monkeypatch.setattr(B, "_DAY_REFUSED_UNTIL", [])
    # A test's fake answer must not land in, or be read from, the live snapshots.
    monkeypatch.setattr(B, "BEST_MATCH_DIR", tmp_path / "best_match_hourly")
    return path


def _no_request(*_a, **_k):
    raise AssertionError("the panel asked Open-Meteo on a spent day")


def test_a_spent_day_in_the_ledger_is_answered_without_a_request(ledger, monkeypatch) -> None:
    Q.mark_exhausted(path=ledger)
    monkeypatch.setattr(urllib.request, "urlopen", _no_request)
    out = B._open_meteo_hourly(30.58, 103.95, "Asia/Shanghai", "2026-10-12")
    assert out == {"available": False, "reason": "Open-Meteo 当日额度已满，00:00Z 恢复"}


def test_each_request_is_charged_to_the_board_lane(ledger, monkeypatch) -> None:
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *_a, **_k: _Resp(json.dumps({"hourly": {"time": []}}).encode()))
    assert B._open_meteo_hourly(30.58, 103.95, "Asia/Shanghai", "2026-10-12")["available"] is True
    assert Q.spent(B.QUOTA_LANE, path=ledger) == 1


def test_a_daily_refusal_is_held_in_process_and_not_marked_for_production(ledger, monkeypatch) -> None:
    calls: list[str] = []

    def _refuse(request, **_k):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 429, "Too Many Requests", {},
                        io.BytesIO(b'{"error":true,"reason":"Daily API request limit exceeded. '
                                   b'Please try again tomorrow."}'))

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    first = B._open_meteo_hourly(30.58, 103.95, "Asia/Shanghai", "2026-10-12")
    second = B._open_meteo_hourly(30.58, 103.95, "Asia/Shanghai", "2026-10-13")
    assert first["reason"] == second["reason"] == "Open-Meteo 当日额度已满，00:00Z 恢复"
    assert len(calls) == 1
    assert Q.exhausted(path=ledger) is False
    held = datetime.fromisoformat(B._DAY_REFUSED_UNTIL[0])
    assert held - datetime.now(timezone.utc) <= timedelta(seconds=B.FORECAST_CACHE_SECONDS)


def test_a_minute_refusal_says_what_the_aggregator_said(ledger, monkeypatch) -> None:
    calls: list[str] = []

    def _refuse(request, **_k):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 429, "Too Many Requests", {},
                        io.BytesIO(b'{"error":true,"reason":"Minutely API request limit exceeded."}'))

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    monkeypatch.setattr(Q, "throttle_pause", lambda _attempt: None)
    out = B._open_meteo_hourly(30.58, 103.95, "Asia/Shanghai", "2026-10-12")
    assert out["available"] is False and "Minutely API request limit" in out["reason"]
    assert B._DAY_REFUSED_UNTIL == []
    assert len(calls) == B.MINUTE_REFUSAL_ATTEMPTS
    assert Q.spent(B.QUOTA_LANE, path=ledger) == B.MINUTE_REFUSAL_ATTEMPTS


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_CONCURRENT = b'{"reason":"Too many concurrent requests","error":true}'


def test_a_concurrency_refusal_is_waited_out(ledger, monkeypatch) -> None:
    # 2026-11-03 「逐小时实况对预报：预报读不到（HTTPError 429: Too many concurrent requests）」.
    answers = iter([HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(_CONCURRENT)),
                    _Resp(json.dumps({"hourly": {"time": ["t"]}}).encode())])

    def _open(_req, **_k):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    paused: list[int] = []
    monkeypatch.setattr(urllib.request, "urlopen", _open)
    monkeypatch.setattr(Q, "throttle_pause", paused.append)
    out = B._open_meteo_hourly(30.58, 103.95, "Asia/Shanghai", "2026-10-12")
    assert out["available"] is True and paused == [0]
    assert Q.spent(B.QUOTA_LANE, path=ledger) == 2


def test_a_refusal_that_persists_shows_the_last_run_dated(ledger, monkeypatch) -> None:
    key = ("30.580,103.950", "2026-10-12")
    B.BEST_MATCH_DIR.mkdir(parents=True)
    old = {"available": True, "hourly": {"time": ["t"]}, "fetched_at_utc": "2026-10-12T01:00:00Z"}
    B._best_match_snapshot(key).write_text(json.dumps(old), encoding="utf-8")

    def _refuse(request, **_k):
        raise HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(_CONCURRENT))

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    monkeypatch.setattr(Q, "throttle_pause", lambda _attempt: None)
    out = B._open_meteo_hourly(30.58, 103.95, "Asia/Shanghai", "2026-10-12")
    assert out["available"] is True and out["fetched_at_utc"] == old["fetched_at_utc"]
    assert "Too many concurrent" in out["reason"]
    # The hourly pull never passes an old run off as its own pull.
    forced = B._open_meteo_hourly(30.58, 103.95, "Asia/Shanghai", "2026-10-12", force=True)
    assert forced["available"] is False


def test_the_panel_asks_for_soil_temperature_and_the_board_shows_it(ledger, monkeypatch) -> None:
    # User 2026-10-24「增加土壤温度」: surface and 6 cm (latest run; no day-ahead soil archive).
    asked: list[str] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _open(req, **_k):
        asked.append(req.full_url)
        return _Resp(json.dumps({"hourly": {"time": []}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    B._open_meteo_hourly(30.58, 103.95, "Asia/Shanghai", "2026-10-12")
    from urllib.parse import parse_qs, urlsplit
    hourly = parse_qs(urlsplit(asked[0]).query)["hourly"][0].split(",")
    assert {"soil_temperature_0cm", "soil_temperature_6cm"} <= set(hourly)
    from pathlib import Path
    dash = (Path(__file__).resolve().parents[1] / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert 'sdSunCard("soil", "土壤温度 · 预报"' in dash and "toned(soilCard," in dash
