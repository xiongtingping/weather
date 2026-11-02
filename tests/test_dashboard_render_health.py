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
    monkeypatch.setattr(C, "RENDER_COMPUTE_END_STAMP", tmp_path / "end-stamp")
    monkeypatch.setattr(C, "HEALTH_SWEEP_LOG", tmp_path / "health.log")

    def set_(*, started_min: float, stamp_min: float | None = None,
             ended_min: float | None = None, build_sec: float = 600.0,
             status: str | None = None, stale: dict | None = None,
             health_lines: list[str] | None = None) -> None:
        started = NOW - timedelta(minutes=started_min)
        compute = {
            "started_at_utc": started.isoformat().replace("+00:00", "Z"),
            "build_sec": build_sec,
            "paged": {"stale_sections": stale or {}},
        }
        if status is not None:
            compute["compute_status"] = status
        (cache / C.COMPUTE_RUN_FILE).write_text(json.dumps(compute))
        stamp = NOW - timedelta(minutes=started_min if stamp_min is None else stamp_min)
        (tmp_path / "stamp").write_text("")
        os.utime(tmp_path / "stamp", (stamp.timestamp(), stamp.timestamp()))
        end_stamp = tmp_path / "end-stamp"
        if ended_min is None:
            end_stamp.unlink(missing_ok=True)
        else:
            ended = NOW - timedelta(minutes=ended_min)
            end_stamp.write_text("")
            os.utime(end_stamp, (ended.timestamp(), ended.timestamp()))
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


def test_stale_previous_summary_does_not_describe_a_new_render(files) -> None:
    files(started_min=60, stamp_min=28,
          stale={"chain": "deadline", "regional": "deadline"})
    active = C._render_health(NOW)
    assert active["health"] == "red"
    assert "没写下结果" in active["note"]
    assert "每轮必算" not in active["note"]
    assert "沿用 0" not in active["reading"]

    # The matching end stamp proves the run is no longer active.  The old
    # summary still cannot contribute its stale-section list.
    files(started_min=60, stamp_min=28, ended_min=11,
          stale={"chain": "deadline", "regional": "deadline"})
    ended = C._render_health(NOW)
    assert ended["health"] == "amber"
    assert "已结束，但没有本轮结果摘要" in ended["note"]
    assert "每轮必算" not in ended["note"]
    assert ended["compute_build_sec"] is None


def test_python_start_after_shell_stamp_is_still_the_current_summary(files) -> None:
    # Importing the dashboard can be slow under disk pressure.  Its summary
    # start may therefore trail the shell's start stamp by more than a minute.
    files(started_min=20, stamp_min=28, ended_min=1,
          stale={"regional": "deadline"})
    health = C._render_health(NOW)
    assert "已结束，但没有本轮结果摘要" not in health["note"]
    assert "每轮必算的 1 个区块沿用上一轮" in health["note"]


def test_a_yielded_compute_is_recorded_as_ended_and_uses_its_own_stale_list(files) -> None:
    files(started_min=12, ended_min=1, status="yielded",
          stale={"regional": "deadline"})
    health = C._render_health(NOW)
    assert health["health"] == "amber"
    assert "已让出给生产写者" in health["note"]
    assert "每轮必算的 1 个区块沿用上一轮" in health["note"]
    assert health["compute_status"] == "yielded"


def test_a_failed_compute_summary_is_a_current_failure(files) -> None:
    files(started_min=5, ended_min=4, status="failed")
    health = C._render_health(NOW)
    assert health["health"] == "red"
    assert "上一轮整页计算失败" in health["note"]


def test_partial_compute_summary_keeps_the_actual_yield_state(monkeypatch) -> None:
    started_at = NOW.timestamp() - 120
    monkeypatch.setattr(C, "_SECTION_CACHE_LOG", {
        "regional": {"build_sec": 31.0, "cached": False},
        "taf_board": {"reused": True},
    })
    monkeypatch.setattr(C, "_PAGED_RUN", {
        "gap_wait_sec": 8.5,
        "yields": 1,
        "stale": {"chain": "deadline"},
    })

    summary = C._compute_run_summary(
        started_at, status="yielded", detail="chain: WAL is over the line",
    )
    assert summary["compute_status"] == "yielded"
    assert summary["computed_sections"] == 1
    assert summary["reused_sections"] == 1
    assert summary["paged"]["stale_sections"] == {"chain": "deadline"}
    assert summary["compute_detail"] == "chain: WAL is over the line"


@pytest.mark.parametrize(
    ("failure", "expected"), [("yielded", "yielded"), ("failed", "failed")]
)
def test_compute_cli_persists_yield_and_failure_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected: str,
) -> None:
    db = tmp_path / "ssot.db"
    db.write_bytes(b"")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", cache)
    monkeypatch.setattr(C, "_SECTION_CACHE_LOG", {})
    monkeypatch.setattr(C, "_PAGED_RUN", {"stale": {"regional": "deadline"}})
    for name in ("SECTION_CACHE_ENABLED", "PAGED_RENDER", "PAGE_READ_ONLY"):
        monkeypatch.setattr(C, name, getattr(C, name))
    monkeypatch.setattr(
        sys, "argv", ["calibration_dashboard.py", "--db", str(db), "--compute-pages"]
    )

    def fail_build(_db: Path) -> None:
        if failure == "yielded":
            raise C.RenderYielded("regional: WAL is over the line")
        raise RuntimeError("fixture compute failure")

    monkeypatch.setattr(C, "build_payload", fail_build)
    if failure == "yielded":
        assert C.main() == 0
    else:
        with pytest.raises(RuntimeError, match="fixture compute failure"):
            C.main()

    summary = json.loads((cache / C.COMPUTE_RUN_FILE).read_text())
    assert summary["compute_status"] == expected
    assert summary["paged"]["stale_sections"] == {"regional": "deadline"}


def test_research_pages_overdue_and_the_health_refresh(files) -> None:
    files(started_min=10, stale={"scorecard": "research lane"},
          health_lines=["[2026-10-21T06:30:00Z] page drawn (read-only)",
                        "[2026-10-21T06:35:00Z] health refresh failed or timed out (exit 124)"])
    note = C._render_health(NOW)["note"]
    assert "研究区块 1 个超期" in note and "1 个从未算出" in note
    assert "快刷 30 分钟没重画" in note and "快刷上一轮失败" in note
