"""The fast TAF round (user, 2026-09-19: 「极限压低信息到来到bayes概率更新的延迟」).

The rotation round waits for the prefetch lane's writer turn, behind a ~200s
production cycle.  The fast round needs no writer turn -- SSOT read-only plus
the inbox file -- so it runs every twenty seconds on its own thread: one bulk
request, captures only for the stations it names, inbox under a shared lock.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location("_fast_taf_daemon", ROOT / "scripts" / "weather_basket_yes_daemon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ssot(tmp_path):
    path = tmp_path / "ssot.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE calibration_forward_taf_capture(station_id TEXT, event_date TEXT, issued_at_utc TEXT)")
    conn.execute("INSERT INTO calibration_forward_taf_capture VALUES ('LTAC','2026-09-19','2026-09-18T16:40:00Z')")
    conn.commit(); conn.close()
    return path


def _worker(tmp_path, catalog):
    return SimpleNamespace(
        _taf_catalog_cache=catalog, _taf_bulk_forced={},
        taf_inbox_path=tmp_path / "inbox.json",
    )


CATALOG = [
    {"station_id": "LTAC", "event_date": "2026-09-19", "settlement_timezone": "Europe/Istanbul"},
    {"station_id": "LTAC", "event_date": "2026-09-20", "settlement_timezone": "Europe/Istanbul"},
    {"station_id": "ZUUU", "event_date": "2026-09-19", "settlement_timezone": "Asia/Shanghai"},
]


def test_no_catalog_means_no_request(daemon, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(daemon, "awc_newer_taf_stations", lambda *a, **k: pytest.fail("no probe"))
    out = daemon.run_fast_taf_round(_worker(tmp_path, []), ssot_path=_ssot(tmp_path))
    assert out["state"] == "no_catalog_yet"


def test_nothing_new_writes_nothing(daemon, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(daemon, "awc_newer_taf_stations", lambda *a, **k: ({}, {"state": "ok"}))
    monkeypatch.setattr(daemon, "refresh_forward_taf_inbox", lambda **k: pytest.fail("no write"))
    out = daemon.run_fast_taf_round(_worker(tmp_path, CATALOG), ssot_path=_ssot(tmp_path))
    assert out["state"] == "nothing_new"


def test_a_newer_bulletin_is_captured_for_every_scope_of_its_station(daemon, tmp_path, monkeypatch) -> None:
    seen = {}

    def probe(stations, *, held_issued_at, already_forced):
        seen["held"] = dict(held_issued_at)
        return {"LTAC": "2026-09-18T22:40:00Z"}, {"state": "ok", "newer": ["LTAC"]}

    captured = []
    monkeypatch.setattr(daemon, "awc_newer_taf_stations", probe)
    monkeypatch.setattr(daemon, "capture_forward_awc_taf",
                        lambda **k: captured.append((k["station_id"], k["event_date"])) or {"id": k["event_date"]})
    writes = []

    def refresh(*, captures, inbox_path):
        assert daemon._TAF_INBOX_LOCK.locked()          # always under the shared lock
        writes.append(len(captures))
        return {}

    monkeypatch.setattr(daemon, "refresh_forward_taf_inbox", refresh)
    worker = _worker(tmp_path, CATALOG)
    out = daemon.run_fast_taf_round(worker, ssot_path=_ssot(tmp_path))
    assert seen["held"] == {"LTAC": "2026-09-18T16:40:00Z"}
    assert captured == [("LTAC", "2026-09-19"), ("LTAC", "2026-09-20")]
    assert writes == [2] and out["state"] == "captured" and out["stations"] == ["LTAC"]
    assert worker._taf_bulk_forced == {"LTAC": "2026-09-18T22:40:00Z"}


def test_one_failing_scope_does_not_lose_the_others(daemon, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(daemon, "awc_newer_taf_stations", lambda *a, **k: ({"LTAC": "x"}, {"state": "ok"}))

    def capture(**k):
        if k["event_date"] == "2026-09-20":
            raise RuntimeError("no bulletin for that day")
        return {"id": k["event_date"]}

    monkeypatch.setattr(daemon, "capture_forward_awc_taf", capture)
    monkeypatch.setattr(daemon, "refresh_forward_taf_inbox", lambda **k: {})
    out = daemon.run_fast_taf_round(_worker(tmp_path, CATALOG), ssot_path=_ssot(tmp_path))
    assert out["capture_count"] == 1 and len(out["failures"]) == 1


def test_the_fast_round_runs_often_and_off_the_writer_turn(daemon) -> None:
    assert daemon.FAST_TAF_ROUND_INTERVAL_SEC <= 30.0
    assert isinstance(daemon._TAF_INBOX_LOCK, type(threading.Lock()))
