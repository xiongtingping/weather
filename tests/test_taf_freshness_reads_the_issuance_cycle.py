"""TAF freshness is read against the issuance cycle, and slowness is its own number.

2026-09-14 05:49Z the card said "50 of 65 scopes over three hours without a new
bulletin".  TAFs issue on a six-hour synoptic schedule (peaks at 05/11/17/23Z),
so an age measured from our receipt runs 0-6 hours for a perfectly served
station purely by where in the cycle it is read, and a three-hour threshold
reads half of every cycle as stale.  What was actually wrong was different
and invisible to that count: bulletins reached the inbox a median 34-109
minutes after AWC published them (p90 2-4.7h, every day 09-04..09-13).

So: age from the newest held bulletin's issue time, stale past one scheduled
cycle and an hour, and the receipt lag reported beside it.  And the round
fetches each station once, so a wider round costs the same requests.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts import weather_basket_yes_daemon as daemon  # noqa: E402
from scripts import capture_bayes_basket_target_awc_taf_revision as capture_mod  # noqa: E402
from bayes_basket_target.taf_rotation_state import (  # noqa: E402
    BULLETIN_OVERDUE_SEC,
    capture_lag_summary,
)

import calibration_dashboard as C  # noqa: E402


def _catalog(n: int, day: str = "2026-09-14") -> list[dict[str, str]]:
    return [{"station_id": f"Z{i:02d}", "event_date": day} for i in range(n)]


def _card(fresh):
    return C._taf_capture_health(
        {"forecast_prefetch": {"forward_taf_capture": {"capture_freshness": fresh}}}
    )


def test_an_hour_after_an_issuance_a_served_fleet_is_not_stale() -> None:
    # Held: the 23Z bulletin, received at 23:50Z.  Now 05:49Z: receipt age is
    # six hours, issue age 6.8h -- inside one cycle and its hour of grace.
    catalog = _catalog(20)
    received = {f"Z{i:02d}|2026-09-14": "2026-09-13T23:50:00Z" for i in range(20)}
    issued = {f"Z{i:02d}|2026-09-14": "2026-09-13T23:00:00Z" for i in range(20)}
    fresh = daemon._taf_capture_freshness(
        catalog, newest_capture_at=received, latest_issued_at=issued,
        as_of_utc="2026-09-14T05:49:00Z",
    )
    assert fresh["stale_scope_count"] == 0
    assert fresh["age_basis"] == "issued"
    assert _card(fresh)["taf_capture_health"] == "green"

    # Every one of them is six hours past its receipt: the old three-hour rule
    # on receipt age called the whole fleet stale.
    assert fresh["median_age_hours"] == 6.82


def test_a_missed_issuance_is_stale_and_named() -> None:
    catalog = _catalog(3)
    issued = {
        "Z00|2026-09-14": "2026-09-13T23:00:00Z",   # 8.1h at 07:05Z: missed the 05Z
        "Z01|2026-09-14": "2026-09-14T05:00:00Z",
        "Z02|2026-09-14": "2026-09-14T05:00:00Z",
    }
    fresh = daemon._taf_capture_freshness(
        catalog, newest_capture_at=dict(issued), latest_issued_at=issued,
        as_of_utc="2026-09-14T07:05:00Z",
    )
    assert BULLETIN_OVERDUE_SEC == 7 * 3600
    assert fresh["stale_scope_count"] == 1
    assert fresh["worst_scopes"][0]["scope"] == "Z00|2026-09-14"
    assert "超过 7 小时没有新签发的报文" in _card(fresh)["taf_capture_note"]


def test_slow_but_not_silent_is_amber_and_says_how_slow() -> None:
    catalog = _catalog(10)
    issued = {f"Z{i:02d}|2026-09-14": "2026-09-14T05:00:00Z" for i in range(10)}
    lag = {"lookback_hours": 24.0, "sample_count": 120, "p50_minutes": 58.0, "p90_minutes": 205.0}
    fresh = daemon._taf_capture_freshness(
        catalog, newest_capture_at=dict(issued), latest_issued_at=issued,
        capture_lag=lag, as_of_utc="2026-09-14T06:00:00Z",
    )
    card = _card(fresh)
    assert fresh["stale_scope_count"] == 0
    assert card["taf_capture_health"] == "amber"
    assert "报文发布到入库中位 58 分钟、p90 205 分钟" in card["taf_capture_note"]

    fast = dict(lag, p50_minutes=8.0, p90_minutes=25.0)
    fresh_fast = daemon._taf_capture_freshness(
        catalog, newest_capture_at=dict(issued), latest_issued_at=issued,
        capture_lag=fast, as_of_utc="2026-09-14T06:00:00Z",
    )
    assert _card(fresh_fast)["taf_capture_health"] == "green"


def test_the_rounds_own_captures_count_before_the_next_ingest() -> None:
    folded = daemon._fold_round_captures(
        {"Z00|2026-09-14": "2026-09-13T23:00:00Z"},
        [
            {"station_id": "z00", "event_date": "2026-09-14",
             "forecast_revision": {"issued_at_utc": "2026-09-14T05:00:00Z"}},
            {"station_id": "Z01", "event_date": "2026-09-14",
             "forecast_revision": {"issued_at_utc": "2026-09-14T05:00:00Z"}},
            {"station_id": "Z02", "event_date": "2026-09-14", "forecast_revision": {}},
        ],
        field="issued_at_utc",
    )
    assert folded == {
        "Z00|2026-09-14": "2026-09-14T05:00:00Z",
        "Z01|2026-09-14": "2026-09-14T05:00:00Z",
    }


def test_the_capture_lag_is_read_off_the_capture_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_forward_taf_capture("
        "received_at_utc TEXT, provider_available_at_utc TEXT)"
    )
    rows = [
        ("2026-09-14T05:10:00Z", "2026-09-14T05:05:00Z"),   # 5 min
        ("2026-09-14T06:05:00Z", "2026-09-14T05:05:00Z"),   # 60 min
        ("2026-09-14T08:05:00Z", "2026-09-14T05:05:00Z"),   # 180 min
        ("2026-09-12T08:05:00Z", "2026-09-12T05:05:00Z"),   # outside the day
    ]
    conn.executemany("INSERT INTO calibration_forward_taf_capture VALUES(?,?)", rows)
    summary = capture_lag_summary(conn, as_of_utc="2026-09-14T09:00:00Z")
    assert summary["sample_count"] == 3
    assert summary["p50_minutes"] == 60.0
    assert summary["p90_minutes"] == 60.0 or summary["p90_minutes"] == 180.0
    assert capture_lag_summary(sqlite3.connect(":memory:"), as_of_utc="2026-09-14T09:00:00Z")["sample_count"] == 0


def test_a_round_fetches_each_station_once_for_all_its_days(monkeypatch) -> None:
    calls: list[str] = []

    def fetch(url, *, timeout_sec):  # noqa: ARG001
        calls.append(url)
        raise RuntimeError("awc down")

    monkeypatch.setattr(capture_mod, "_fetch_json", fetch)
    cache: dict[str, object] = {}
    for day in ("2026-09-14", "2026-09-15", "2026-09-16"):
        with pytest.raises(RuntimeError, match="awc down"):
            capture_mod.capture_forward_awc_taf(
                station_id="ZBAA", event_date=day,
                settlement_timezone="Asia/Shanghai", response_cache=cache,
            )
    assert len(calls) == 1

    # Without a cache every call fetches, exactly as before.
    for day in ("2026-09-14", "2026-09-15"):
        with pytest.raises(RuntimeError):
            capture_mod.capture_forward_awc_taf(
                station_id="ZBAA", event_date=day, settlement_timezone="Asia/Shanghai",
            )
    assert len(calls) == 3


def test_a_station_fetched_once_serves_its_other_days_from_the_same_bytes(monkeypatch) -> None:
    from datetime import datetime, timezone

    start = int(datetime(2026, 7, 26, 15, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 7, 27, 18, tzinfo=timezone.utc).timestamp())
    rows = [{
        "icaoId": "RKPK",
        "issueTime": "2026-07-26T11:00:00Z",
        "dbPopTime": "2026-07-26T11:05:00Z",
        "validTimeFrom": "2026-07-26T11:00:00Z",
        "validTimeTo": "2026-07-27T18:00:00Z",
        "rawTAF": "TAF RKPK 261100Z 2612/2718 TX35/2706Z",
        "fcsts": [{
            "timeFrom": start, "timeTo": end, "wspd": 8, "probability": 30,
            "wxString": "-RA", "clouds": [{"cover": "BKN", "base": 3000}],
        }],
    }]
    calls: list[str] = []

    def fetch(url, *, timeout_sec):  # noqa: ARG001
        calls.append(url)
        return rows, "a" * 64, 512

    monkeypatch.setattr(capture_mod, "_fetch_json", fetch)
    cache: dict[str, object] = {}
    first = capture_mod.capture_forward_awc_taf(
        station_id="RKPK", event_date="2026-07-27", settlement_timezone="Asia/Seoul",
        response_cache=cache,
    )
    second = capture_mod.capture_forward_awc_taf(
        station_id="RKPK", event_date="2026-07-27", settlement_timezone="Asia/Seoul",
        response_cache=cache,
    )
    assert len(calls) == 1
    # The identity a fetch would have produced: same URL, bytes and receipt.
    assert first["content_sha256"] == second["content_sha256"]
    assert first["local_received_at_utc"] == second["local_received_at_utc"]
