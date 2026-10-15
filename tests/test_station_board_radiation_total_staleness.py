"""Both radiation totals on the station board are prominent and say how old
they are (user 2026-10-13 「把两处的辐射总量 12.74 MJ/m² ·这个数据突出，并显示
数据的陈旧度」; 「晴空指数与系数：默认展开」)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bayes_basket_target import station_day_board as board

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")


def _store(rows: list[tuple[str, float]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_station_forecast_physics_version (station_id TEXT, local_date TEXT,"
                 " source_id TEXT, captured_at_utc TEXT, shortwave_radiation_sum REAL)")
    conn.executemany("INSERT INTO calibration_station_forecast_physics_version VALUES"
                     " ('ZUUU','2026-10-14','OPEN_METEO_MODEL_FORECAST',?,?)", rows)
    return conn


ZUUU = [("2026-10-12T21:43:30Z", 13.26), ("2026-10-13T03:43:59Z", 12.74),
        ("2026-10-13T09:45:26Z", 12.74), ("2026-10-13T15:45:00Z", 13.17)]


def test_the_capture_p0_read_and_a_newer_one() -> None:
    out = board._radiation_capture(_store(ZUUU), "ZUUU", "2026-10-14", "2026-10-13T12:15:00Z", 12.74)
    assert out == {"capture_state": "matched", "captured_at_utc": "2026-10-13T09:45:26Z",
                   "first_seen_utc": "2026-10-13T03:43:59Z",
                   "latest_mj": 13.17, "latest_captured_at_utc": "2026-10-13T15:45:00Z"}


def test_a_value_matching_no_capture_is_said_not_guessed() -> None:
    out = board._radiation_capture(_store(ZUUU[:3]), "ZUUU", "2026-10-14", "2026-10-13T12:15:00Z", 11.0)
    assert out == {"capture_state": "value_not_in_record"}
    assert board._radiation_capture(_store([]), "ZUUU", "2026-10-14", "2026-10-13T12:15:00Z", 12.74) == {
        "capture_state": "no_capture_record"}


def _function(name: str) -> str:
    start = DASHBOARD.index(f"function {name}(")
    depth = 0
    for index in range(DASHBOARD.index("{", start), len(DASHBOARD)):
        depth += {"{": 1, "}": -1}.get(DASHBOARD[index], 0)
        if depth == 0:
            return DASHBOARD[start:index + 1]
    raise AssertionError(name)


def test_the_card_draws_the_total_its_age_and_the_newer_value() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the card")
    R = {"radiation_sum_mj": 12.74, "state": "RESOLVED", "capture_state": "matched",
         "captured_at_utc": "2026-10-13T09:45:26Z", "first_seen_utc": "2026-10-13T03:43:59Z",
         "latest_mj": 13.17, "latest_captured_at_utc": "2026-10-13T15:45:00Z"}
    script = (
        'const sdEsc = s => String(s == null ? "" : s);\n'
        'const sdN = (v, d) => v == null || !isFinite(v) ? "—" : Number(v).toFixed(d == null ? 2 : d);\n'
        'function sdLocal(t){ return String(t).slice(5, 16); } function sdAgeSpan(t){ return "AGE(" + t + ")"; }\n'
        + _function("sdSunStat") + "\n" + _function("sdRadTotal")
        + f"\nconsole.log(sdRadTotal({json.dumps(R)}).replace(/<[^>]+>/g, '|'));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    text = "|".join(part for part in out.stdout.strip().split("|") if part)
    assert "辐射总量（P0 用）|12.74| MJ/m²|取于 AGE(2026-10-13T09:45:26Z)|已更新 13.17" in text, text
    assert "<details open><summary>晴空指数与系数</summary>" in DASHBOARD
    assert 'sdSunCard("预报日照"' in DASHBOARD and "sdPct(H.forecast_sw_mj, H.forecast_sw_d1_mj)" in DASHBOARD
