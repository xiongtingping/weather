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
    def _refuse(request, **_k):
        raise HTTPError(request.full_url, 429, "Too Many Requests", {},
                        io.BytesIO(b'{"error":true,"reason":"Minutely API request limit exceeded."}'))

    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    out = B._open_meteo_hourly(30.58, 103.95, "Asia/Shanghai", "2026-10-12")
    assert out["available"] is False and "Minutely API request limit" in out["reason"]
    assert B._DAY_REFUSED_UNTIL == []


def test_the_panel_asks_for_soil_temperature_and_the_board_shows_it(ledger, monkeypatch) -> None:
    # User 2026-10-24「增加土壤温度」: surface and 6 cm, 6 cm also from the day-ahead run.
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
    assert {"soil_temperature_0cm", "soil_temperature_6cm", "soil_temperature_6cm_previous_day1"} <= set(hourly)
    from pathlib import Path
    dash = (Path(__file__).resolve().parents[1] / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert 'sdSunCard("soil", "土壤温度 · 预报"' in dash and "    soilCard,\n" in dash
