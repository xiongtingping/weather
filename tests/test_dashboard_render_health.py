"""The board's render health review (user 2026-10-21 「系统健康增加渲染的健康审查」)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import calibration_dashboard as C  # noqa: E402

NOW = datetime(2026, 10, 21, 7, 0, tzinfo=timezone.utc)


@pytest.fixture
def files(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", cache)
    monkeypatch.setattr(C, "RENDER_COMPUTE_STAMP", tmp_path / "stamp")
    monkeypatch.setattr(C, "HEALTH_SWEEP_LOG", tmp_path / "health.log")

    def set_(*, started_min: float, stamp_min: float | None = None, build_sec: float = 600.0,
             stale: dict | None = None, health_lines: list[str] | None = None) -> None:
        started = NOW - timedelta(minutes=started_min)
        (cache / C.COMPUTE_RUN_FILE).write_text(json.dumps({
            "started_at_utc": started.isoformat().replace("+00:00", "Z"), "build_sec": build_sec,
            "paged": {"stale_sections": stale or {}}}))
        stamp = NOW - timedelta(minutes=started_min if stamp_min is None else stamp_min)
        (tmp_path / "stamp").write_text("")
        os.utime(tmp_path / "stamp", (stamp.timestamp(), stamp.timestamp()))
        if health_lines is not None:
            (tmp_path / "health.log").write_text("\n".join(health_lines) + "\n")
    return set_


def test_a_board_on_time_is_green(files) -> None:
    files(started_min=10, health_lines=["[2026-10-21T06:57:00Z] page drawn (read-only)"])
    health = C._render_health(NOW)
    assert health["health"] == "green" and C._render_health_alerts(health) == []


def test_late_cadence_deadline_sections_and_a_killed_compute(files) -> None:
    files(started_min=50, stale={"members": "deadline", "regional": "deadline"})
    late = C._render_health(NOW)
    assert late["health"] == "amber"
    assert "50 分钟没开始" in late["note"] and "每轮必算的 2 个区块沿用上一轮" in late["note"]
    # a compute that stamped its start 30 min after the summary's and never wrote one
    files(started_min=60, stamp_min=27)
    assert "没写下结果" in C._render_health(NOW)["note"]
    assert C._render_health(NOW)["health"] == "red"
    assert C._render_health_alerts(C._render_health(NOW))[0]["label"] == "看板渲染"


def test_research_pages_overdue_and_the_health_refresh(files) -> None:
    files(started_min=10, stale={"scorecard": "research lane"},
          health_lines=["[2026-10-21T06:30:00Z] page drawn (read-only)",
                        "[2026-10-21T06:35:00Z] health refresh failed or timed out (exit 124)"])
    note = C._render_health(NOW)["note"]
    assert "研究区块 1 个超期" in note and "1 个从未算出" in note
    assert "快刷 30 分钟没重画" in note and "快刷上一轮失败" in note


def test_charter_ev_reads_each_serving_version_on_its_own() -> None:
    """Charter 255 (user 2026-10-21): the EV identity version by version, the
    newest marked current, untagged decisions counted rather than dropped."""

    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_policy_training_fact(station_id TEXT, event_date TEXT, stage TEXT,"
                 " decision_at_utc TEXT, posterior_version TEXT)")
    rows = []
    for i, (version, day) in enumerate([("p0_v29", "2026-10-15"), ("p0_v29", "2026-10-16"),
                                        ("p0_v30", "2026-10-17"), ("p0_v30", "2026-10-18")]):
        at = f"{day}T06:00:00Z"
        conn.execute("INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?)", ("AAAA", day, "P0", at, version))
        rows.append({"station": "AAAA", "date": day, "stage": "P0", "at": at, "label": "b",
                     "m": {"a": 0.5, "b": 0.5}, "p": {"a": 0.3 + 0.1 * i, "b": 0.7 - 0.1 * i},
                     "m_label": 0.5, "p_label": 0.7 - 0.1 * i, "weight": 1.0})
    rows.append({**rows[-1], "at": "2026-10-18T07:00:00Z"})  # no training fact: untagged
    segments = C._charter_ev_version_segments(conn, rows, stages=("P0",), floor_day="2026-10-14",
                                              through="2026-10-20")
    assert [s["short"] for s in segments] == ["v29", "v30", "未标版本"]
    assert segments[1].get("current") is True and segments[0].get("current") is None
    assert segments[0]["station_days"] == 2 and segments[2]["decisions"] == 1
