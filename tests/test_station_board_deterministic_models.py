"""The station board shows every connected deterministic model's newest run
(user 2026-10-13 「增加目前已接入的"确定性预报模型"的数据」): the stored point
runs and charter 267's listing-time F, read, never computed."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bayes_basket_target import deterministic_forecast_family as fam
from bayes_basket_target import station_day_board as board

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")


def _store(rows: list[tuple]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_regional_point_run (station_id TEXT, target_local_date TEXT,"
        " ensemble_system_id TEXT, run_init_utc TEXT, knowable_at_utc TEXT, forecast_tmax_c REAL)"
    )
    conn.executemany("INSERT INTO calibration_regional_point_run VALUES ('ZUUU','2026-10-13',?,?,?,?)", rows)
    return conn


def test_newest_and_previous_run_per_model_and_the_f_entry(tmp_path, monkeypatch) -> None:
    icon, arpege = fam.SYSTEM_IDS["ICON_GLOBAL"], fam.SYSTEM_IDS["ARPEGE"]
    conn = _store([
        (icon, "2026-10-12T12:00:00Z", "2026-10-12T16:00:00Z", 25.05),
        (icon, "2026-10-13T00:00:00Z", "2026-10-13T04:05:00Z", 26.19),
        # A re-capture of the same run keeps the first one.
        (icon, "2026-10-13T00:00:00Z", "2026-10-13T05:00:00Z", 30.0),
        (arpege, "2026-10-13T00:00:00Z", "2026-10-13T04:28:00Z", 28.09),
        ("SOME_ENSEMBLE", "2026-10-13T00:00:00Z", "2026-10-13T04:00:00Z", 20.0),
    ])
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"computed_at_utc": "2026-10-13T04:53:29Z", "station_days": {
        "ZUUU|2026-10-13": {"f_c": 27.537, "raw_median_c": 26.273, "n_systems": 6, "state": "READY",
                            "v3": {"f_c": 27.738}}}}), encoding="utf-8")
    monkeypatch.setattr(board, "LISTING_F_PATH", path)
    monkeypatch.setattr(board, "_LISTING_F_CACHE", {})
    out = board._deterministic(conn, "ZUUU", "2026-10-13")
    assert [r["product"] for r in out["systems"]] == ["ICON_GLOBAL", "ARPEGE"]   # F's first
    icon_row, arpege_row = out["systems"]
    assert icon_row["in_f"] and icon_row["tmax_c"] == 26.19 and icon_row["prev_tmax_c"] == 25.05
    assert not arpege_row["in_f"] and arpege_row["prev_tmax_c"] is None
    assert out["f"] == {"f_c": 27.537, "raw_median_c": 26.273, "n_systems": 6, "state": "READY",
                        "computed_at_utc": "2026-10-13T04:53:29Z", "f_v3_c": 27.738,
                        "pick": None}   # charter 267 补记七's pick, absent in this file


def _function(name: str) -> str:
    start = DASHBOARD.index(f"function {name}(")
    depth = 0
    for index in range(DASHBOARD.index("{", start), len(DASHBOARD)):
        depth += {"{": 1, "}": -1}.get(DASHBOARD[index], 0)
        if depth == 0:
            return DASHBOARD[start:index + 1]
    raise AssertionError(name)


def test_the_card_draws_f_and_one_chip_per_model() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the card")
    day = {"stages": {"P0": {"model_center_c": 27.55}}, "deterministic": {
        "f": {"f_c": 27.537, "raw_median_c": 26.273, "n_systems": 6, "computed_at_utc": "2026-10-13T04:53:29Z"},
        "systems": [
            {"product": "ICON_GLOBAL", "system_id": "X", "in_f": True, "relay": False, "tmax_c": 26.19,
             "run_init_utc": "2026-10-13T00:00:00Z", "knowable_at_utc": "2026-10-13T04:05:00Z",
             "prev_run_init_utc": "2026-10-12T12:00:00Z", "prev_tmax_c": 25.05},
            {"product": "ARPEGE", "system_id": "Y", "in_f": False, "relay": False, "tmax_c": 28.09,
             "run_init_utc": "2026-10-13T00:00:00Z", "knowable_at_utc": "2026-10-13T04:28:00Z",
             "prev_run_init_utc": None, "prev_tmax_c": None}]}}
    start = DASHBOARD.index("const SD_DET_LABEL")
    script = (
        'const SD = {data: {timezone: "Asia/Shanghai"}}; const SD_COL = {P0: "#58a6ff"};\n'
        'const sdEsc = s => String(s == null ? "" : s);\n'
        'const sdN = (v, d) => v == null || !isFinite(v) ? "—" : Number(v).toFixed(d == null ? 2 : d);\n'
        'const sdSg = (v, d) => v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d == null ? 2 : d);\n'
        + _function("sdLocal") + "\n"
        + DASHBOARD[start:DASHBOARD.index("function sdDeterministic(")] + _function("sdDeterministic")
        + f"\nconsole.log(sdDeterministic({json.dumps(day, ensure_ascii=False)}).replace(/<[^>]+>/g, '|'));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    text = out.stdout
    assert "F 27.54°C" in text and "比 P0 −0.01°C" in text and "中位 26.27°C" in text
    assert "ICON 全球" in text and "26.2" in text and "↑+1.1" in text and "进 F" in text
    assert "ARPEGE" in text and "28.1" in text and "首轮" in text and "只采集" in text
    assert "${sdDeterministic(day)}" in DASHBOARD


def test_p0_change_cause_names_its_ngr_inputs() -> None:
    """User 2026-11-02 「模型没有run更新，为什么p0变化了呢」「加上」: charter 281's
    P0 also moves on a deterministic run, the lead hour and the radiation score;
    the cause names them instead of 「同一批 run 重算」."""

    runs = json.dumps({"NOAA_GEFS_05": "2026-11-01T18:00:00Z"})
    det = lambda **kw: json.dumps({k: {"day_max_c": 20.0, "run_init_utc": v} for k, v in kw.items()})  # noqa: E731
    head = (runs, None, None, None, "root-1")
    before = head + (det(GFS="2026-11-01T12:00:00Z", CMA="2026-10-31T18:00:00Z"), -0.8333, -0.68)
    now = head + (det(GFS="2026-11-01T18:00:00Z", CMA="2026-11-01T12:00:00Z"), -0.875, -0.48)
    assert board._timeline_cause("P0", now, before) == [
        "确定性 CMA 10-31 18Z→11-01 12Z、GFS 12Z→18Z", "提前量 16→15 小时", "辐射分 -0.68→-0.48"]
    assert board._timeline_cause("P0", before, before) == ["同一批 run 重算"]
    assert board._timeline_cause("P0", head, head) == ["同一批 run 重算"]
