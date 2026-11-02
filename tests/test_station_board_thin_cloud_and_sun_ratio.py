"""ZUUU 2026-10-19 (user 「实况云和我观测感觉不太一样」→「晴空无云」): the satellite
read 88% 积云 at optical thickness 1.5 while its sunshine matched the forecast;
the cloud cell now shows τ and fades thin cloud, the sunshine column carries
satellite ÷ forecast."""

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


def _run(body: str) -> list[str]:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    script = ("const sdEsc = s => String(s == null ? '' : s); const sdN = (v, d) => Number(v).toFixed(d); const sdLocal = s => s;\n"
              "const SD_THIN_TAU = 3.6;\n" + _function("sdSatCloudCell") + _function("sdSunRatio") + "\n" + body)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().split("\n")


def _cell(cover: float, tau: float) -> dict:
    return {"sat_cloud": {"state": "ok", "cover": cover, "cells": 25, "type_name": "积云", "optical_thickness": tau,
                          "scan_utc": "x"}, "forecast": {"cloud": 0}}


def test_thin_cloud_shows_its_thickness_faded_and_tagged() -> None:
    thin, thick = _run(f"console.log(sdSatCloudCell({json.dumps(_cell(0.88, 1.5))})); console.log(sdSatCloudCell({json.dumps(_cell(0.88, 12))}));")
    assert 'class="sd-thin"' in thin and "τ1.5" in thin and ">薄<" in thin
    assert 'class="sd-thin"' not in thick and "τ12.0" in thick and ">薄<" not in thick


def test_the_sunshine_column_carries_satellite_over_forecast() -> None:
    full, weak, low = _run("console.log(sdSunRatio(524, 528)); console.log(sdSunRatio(300, 528)); console.log(JSON.stringify(sdSunRatio(5, 10)));")
    assert ">99%<" in full and "sd-warm" in full
    assert ">57%<" in weak and "sd-cold" in weak
    assert low == '""'                                         # the sun too low to say


def test_the_report_low_cloud_counts_layers_based_at_or_below_10000_ft() -> None:
    """User 2026-10-19 「报文云只算云底 ≤ 3 km（约 10000 ft）的云层，与 Open-Meteo
    低云口径一致」."""

    from bayes_basket_target.station_day_board import _low_cloud_cover

    assert _low_cloud_cover("ZUUU 190200Z 14002MPS 4000 BR FEW008 BKN016 20/16".split()) == 0.75
    assert _low_cloud_cover("RKPK 190300Z 33005KT 9999 FEW010 SCT020 BKN040 BKN070".split()) == 0.75   # 4000/7000 ft
    assert _low_cloud_cover("X 190300Z 9999 FEW030 BKN120 OVC200".split()) == 0.19                       # high layers left out
    assert _low_cloud_cover("RJTT 190300Z 20020KT 9999 FEW030 BKN///".split()) == 0.19                    # base unknown: not counted
    assert _low_cloud_cover("X 190300Z 0100 FG VV001".split()) == 1.0
    assert _low_cloud_cover("ZSQD 190300Z VRB01MPS CAVOK 18/06".split()) == 0.0
    assert _low_cloud_cover("X 190300Z 9999 SCT110 BKN250".split()) == 0.0
    assert _low_cloud_cover("X 190300Z 9999 18/06 Q1020".split()) is None                                 # nothing said of the sky
