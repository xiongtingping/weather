"""The nav bar's 站点看板 chip names the station showing (user 2026-10-18
「当选择了对应站点的时候，导航栏里的站点看板同步显示所选的站点名字」)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth = 0
    for i in range(src.index("{", start), len(src)):
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start:i + 1]
    raise AssertionError(name)


def test_the_chip_follows_the_selected_station() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415

    src = C.HTML_TEMPLATE
    script = (
        "const chip = {textContent: '站点看板', title: ''};\n"
        "const document = {querySelectorAll: s => s === '[data-goto=\"sec-station-day\"]' ? [chip] : []};\n"
        "const SD = {station: 'WSSS', fleet: [{station_id: 'WSSS', name: '新加坡'}, {station_id: 'WMKK', name: '吉隆坡'}]};\n"
        + _fn(src, "sdStationName") + _fn(src, "sdNavStation")
        + "\nsdNavStation(); const a = chip.textContent;"
        "\nSD.station = 'WMKK'; sdNavStation(); const b = chip.textContent + '|' + chip.title;"
        "\nSD.station = 'ZZZZ'; sdNavStation(); console.log([a, b, chip.textContent].join('\\n'));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["站点看板 · 新加坡", "站点看板 · 吉隆坡|站点看板：吉隆坡（WMKK）", "站点看板 · ZZZZ"]
    # every redraw keeps it in step, and the chip is built with the name
    assert "function sdRender(){\n  sdNavStation();" in src
    assert 'node.id === "sec-station-day" && typeof sdStationName === "function"' in src
