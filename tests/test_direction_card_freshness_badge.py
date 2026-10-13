"""The direction card's 「最新」 badge (user 2026-10-13 「增加一个数据是否最新的徽标」).

停更 when the snapshot is past 25 minutes old, 迟到 when a component's run is
past the hour by which it normally lands and not in hand, else 最新.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bayes_basket_target import station_day_board as board

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = DASHBOARD.index(f"function {name}(")
    depth = 0
    for index in range(DASHBOARD.index("{", start), len(DASHBOARD)):
        depth += {"{": 1, "}": -1}.get(DASHBOARD[index], 0)
        if depth == 0:
            return DASHBOARD[start:index + 1]
    raise AssertionError(name)


def _badges(cases: list[dict]) -> list[str]:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the card")
    script = (
        'const SD = {data: {timezone: "Asia/Shanghai"}};\n'
        'const SD_DIR_COMPONENTS = {aifs_ens:"AIFS 集合", ifs_ens:"IFS 集合", hres:"HRES 点预报", aifs_single:"AIFS 单成员（两轮）"};\n'
        'const sdEsc = s => String(s == null ? "" : s);\n'
        + "\n".join(_function(n) for n in ("sdLocal", "sdSpan", "sdDirRun", "sdDirFresh"))
        + "\nconst H = 3600e3, now = Date.now(), iso = t => new Date(t).toISOString();\n"
        + "const cycle = c => Math.floor(c / (6 * H)) * 6 * H;\n"
        + f"const cases = {json.dumps(cases)};\n"
        + "console.log(JSON.stringify(cases.map(c => {\n"
        + "  const D = {computed_at_utc: iso(now - c.age_min * 60e3), snapshot_stale_sec: 1500,\n"
        + f"    run_due_hours: {json.dumps(board.DIRECTION_RUN_DUE_HOURS)},\n"
        + "    momentum_components: Object.fromEntries(Object.entries(c.runs_back_h).map(([k, h]) =>\n"
        + "      [k, {newest_run_init_utc: iso(cycle(now - h * H))}]))};\n"
        + "  return sdDirFresh(D).replace(/<[^>]+>/g, '');\n"
        + "})));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_on_time_late_and_stalled() -> None:
    fresh, late, stalled, none = _badges([
        # Every component on the newest cycle that is past its landing hour.
        {"age_min": 3, "runs_back_h": {k: h for k, h in board.DIRECTION_RUN_DUE_HOURS.items()}},
        # IFS ENS still on the cycle before: its run is overdue.
        {"age_min": 3, "runs_back_h": {"aifs_ens": 7.5, "ifs_ens": 8.5 + 6}},
        {"age_min": 40, "runs_back_h": {"aifs_ens": 7.5}},
        {"age_min": 3, "runs_back_h": {}},
    ])
    assert fresh == "最新" and late == "迟到" and stalled.startswith("停更 ") and none == "无可比轮次"


def test_the_board_hands_the_card_its_schedule(tmp_path, monkeypatch) -> None:
    path = tmp_path / "board.json"
    path.write_text(json.dumps({"computed_at_utc": "2026-10-13T04:43:29Z", "momentum_quiet_below": 1.0,
                                "stations": {"ZUUU": {"2026-10-13": {"side": 1}}}}), encoding="utf-8")
    monkeypatch.setattr(board, "DIRECTION_BOARD_PATH", path)
    monkeypatch.setattr(board, "_DIRECTION_CACHE", {})
    read = board._direction("ZUUU", "2026-10-13")
    assert read["run_due_hours"] == board.DIRECTION_RUN_DUE_HOURS and read["snapshot_stale_sec"] == 1500.0
    assert "sdDirFresh(day.direction)" in DASHBOARD
