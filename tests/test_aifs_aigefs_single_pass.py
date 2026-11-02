"""AIFS ENS and AIGEFS are read by the pass that seats them (user 2026-11-02
「AIFS/AIGEFS 只抓一次：去掉「先跟随、再重读」的两遍抓取」)."""

from __future__ import annotations

import argparse
import json

import pytest

from bayes_basket_target import follow_run
from scripts import capture_official_native_ensemble_forward as capture


def test_fetch_follow_reads_that_run_then_runs_the_pass(monkeypatch, tmp_path, capsys) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_follow(**kwargs):
        calls.append(("follow", kwargs))
        return {"state": "COMMITTED_NO_LIVE", "archive_paths": [], "outcomes": []}

    def fake_pass(**kwargs):
        calls.append(("pass", kwargs))
        return {"state": "COMMITTED_NO_LIVE"}

    monkeypatch.setattr(capture, "capture_follow_runs", fake_follow)
    monkeypatch.setattr(capture, "capture_official_native_forward", fake_pass)
    code = capture.main([
        "--event-date", "2026-11-03", "--market-registry", str(tmp_path / "registry.json"),
        "--lead-days", "2", "--capture-root", str(tmp_path),
        "--fetch-follow", f"{follow_run.AIFS_FAMILY}:2:18",
    ])
    assert code == 0
    assert [name for name, _ in calls] == ["follow", "pass"]
    follow = calls[0][1]
    assert follow["targets"] == [(follow_run.AIFS_FAMILY, 2, 18)] and follow["rungs"] == []
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["fetch_follow"]["state"] == "COMMITTED_NO_LIVE"


def test_only_the_follow_only_families_can_be_fetched_inline() -> None:
    assert capture._parse_fetch_follow(f"{follow_run.AIGEFS_FAMILY}:1:6") == (follow_run.AIGEFS_FAMILY, 1, 6)
    for bad in (f"{follow_run.ICON_FAMILY}:1:6", f"{follow_run.AIFS_FAMILY}:3:6", f"{follow_run.AIFS_FAMILY}:1:3"):
        with pytest.raises(argparse.ArgumentTypeError):
            capture._parse_fetch_follow(bad)
