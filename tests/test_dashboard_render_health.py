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
