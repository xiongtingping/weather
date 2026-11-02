"""Switching station drops the old station's order results and re-reads the
shown day's holdings on a fresh book (user 2026-10-18 「切换站点后会有遗留购买
记录」「切换站点后同步刷新盘口和持仓」)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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


def _run(body: str) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    script = (
        "const calls = []; let reply = null; let pending = [];\n"
        "const SD = {station: 'WMKK', dayTab: '2026-10-18', calc: {x: 1}, data: {}, fleet: [],"
        " orders: {'2026-10-18': {batch_id: 'old', results: [{bucket: '35°C'}]}}};\n"
        "const localStorage = {setItem(){}, getItem(){ return null; }};\n"
        "const document = {getElementById: () => null, querySelectorAll: () => []};\n"
        "const sdRender = () => {}; const sdStatus = () => {}; const sdStationPicker = () => '';\n"
        "const sdRefreshCalc = () => {}; const sdSave = () => {}; const sdLocal = s => s;\n"
        "const sdCalcState = k => (SD.calc[k] = SD.calc[k] || {});\n"
        "const SD_MIN_SHARE = 1;\n"
        "globalThis.fetch = url => new Promise(res => pending.push({url, res}));\n"
        "const answer = (i, j) => pending[i].res({ok: true, json: () => Promise.resolve(j)});\n"
        "const tick = () => new Promise(r => setTimeout(r, 10));\n"
        + _function("sdSwitchStation") + _function("sdFetch") + _function("sdLoadHoldings")
        + "\n(async () => {" + body + "})();\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_switching_clears_the_old_results_and_reads_the_new_holdings() -> None:
    got = _run(
        "sdSwitchStation('RPLL'); const orders = SD.orders;\n"
        "await tick(); answer(0, {station_id: 'RPLL', days: [{event_date: '2026-10-18', available: true}]});\n"
        "await tick();\n"
        "console.log(JSON.stringify({orders, urls: pending.map(p => p.url)}));"
    )
    assert got["orders"] == {}
    assert "station=RPLL&force=1" in got["urls"][0]
    assert "station-day-holdings?station=RPLL&event_date=2026-10-18" in got["urls"][1]


def test_a_holdings_read_for_the_old_station_neither_blocks_nor_lands() -> None:
    got = _run(
        "sdLoadHoldings('2026-10-18');\n"                 # WMKK, still reading
        "const switched = sdSwitchStation('RPLL');\n"
        "await tick(); answer(0, {rows: [{bucket: '35°C', shares: 18.75}]});\n"   # WMKK's reply, late
        "await tick();\n"
        "console.log(JSON.stringify({switched, held: (SD.calc['2026-10-18'] || {}).held || null,"
        " holding: SD.holding}));"
    )
    assert got == {"switched": True, "held": None, "holding": False}
