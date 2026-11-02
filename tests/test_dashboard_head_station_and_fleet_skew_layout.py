"""Layout of 2026-10-18 (user): the station board entry floats in the head row
(「把站点看板移动到head区悬浮」), the fleet skew is its own board (「把全队 48 站 ·
偏态（均值对众数）移出站点看板区，单独一个板块」), the lane switches sit with the P&L
calendar (「把head区的开关移动到“盈亏日历”区」), and a double click on a station
opens its market (「站点看板增加双击点击站点调整到对应的市场盘口」)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import calibration_dashboard as C  # noqa: E402

SRC = C.HTML_TEMPLATE


def _fn(name: str) -> str:
    start = SRC.index(f"function {name}(")
    depth = 0
    for i in range(SRC.index("{", start), len(SRC)):
        depth += {"{": 1, "}": -1}.get(SRC[i], 0)
        if depth == 0:
            return SRC[start:i + 1]
    raise AssertionError(name)


def test_the_head_row_carries_the_station_picker_and_not_the_switches() -> None:
    # 「我说的是把站点移动到head区」: the city picker, not the board's nav entry
    head = SRC[SRC.index('<div class="navtop">'):SRC.index('<nav class="jump" id="jump"')]
    assert '<div id="sd-st-pick" class="sd-st-pick sd-st-head">${sdStationPicker()}</div>' in head
    assert SRC.count('id="sd-st-pick"') == 1
    assert "liveControlStrip" not in head
    # the switches open the first top area, above the P&L calendar
    area = SRC[SRC.index("`<section class=\"toparea\""):]
    assert area.index('<div class="lc-panel">${safe(liveControlStrip, "实盘控制")}</div>') < area.index("TOP.map(")
    # the board's entry stays on the bar
    assert "nav-station" not in SRC


def test_the_fleet_skew_is_its_own_board() -> None:
    # 「移出站点看板区」(2026-10-21): filed under the model and edge areas, not the top area
    assert '[fleetShapePanel,"全队偏态"]' in SRC and '[modeAgreementPanel,"众数同桶命中"]' in SRC
    area = SRC[SRC.index("`<section class=\"toparea\""):]
    assert "fleetShapePanel" not in area and "modeAgreementPanel" not in area
    assert "fsBody.innerHTML = sdFleetShape(d.fleet_shape) + sdSkewHistory(d.skew_history);" in SRC
    assert "body.innerHTML = tabs + days.map(" in SRC
    # 「我方众数 = 市场众数时，真值命中率 也移出」
    assert "maBody.innerHTML = sdModeAgreement(d.mode_agreement)" in SRC
    assert '["全队偏态", ".sd-t", "全队"]' not in SRC


def test_a_double_click_opens_the_station_market() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    now = int(datetime(2026, 10, 18, 2, 36, tzinfo=timezone.utc).timestamp() * 1000)
    fleet = [{"station_id": "WSSS", "city_slug": "singapore", "utc_offset_h": 8},
             {"station_id": "KLGA", "city_slug": "nyc", "utc_offset_h": -4}]
    script = (f"const SD = {{station: 'ZUUU', dayTab: '2026-10-19', fleet: {json.dumps(fleet)}}};\n"
              + SRC[SRC.index("const SD_MONTHS = "):SRC.index("function sdMarketUrl(")] + _fn("sdMarketUrl")
              + f"\nconsole.log([sdMarketUrl('WSSS', {now}), sdMarketUrl('KLGA', {now}), String(sdMarketUrl('ZZZZ', {now}))].join('\\n'));"
              "\nSD.station = 'WSSS'; console.log(sdMarketUrl('WSSS'));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        "https://polymarket.com/event/highest-temperature-in-singapore-on-october-18-2026",
        "https://polymarket.com/event/highest-temperature-in-nyc-on-october-17-2026",   # still the 17th in New York
        "null",
        "https://polymarket.com/event/highest-temperature-in-singapore-on-october-19-2026",  # the day showing
    ]
    assert 'addEventListener("dblclick"' in SRC and 'window.open(url, "_blank", "noopener")' in SRC
