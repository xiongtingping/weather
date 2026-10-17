"""The four-feature score taken apart on the board (user 2026-10-13
「把模型分拆解显式」): the resident writes each feature's input and share,
they sum to the score, and the card draws them without recomputing."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bayes_basket_target import direction_signal as ds

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
ZUUU_1014 = {"icon_shortwave": 0.717, "dew_point_consensus": -0.0985, "ecmwf_wind_inverse": 1.1608}


def test_the_parts_are_coefficient_times_input_and_sum_to_the_score() -> None:
    parts = ds.model_parts(-0.1132, ZUUU_1014)
    assert parts == {"momentum_half": -0.00317, "icon_shortwave": 0.010038,
                     "dew_point_consensus": 0.004432, "ecmwf_wind_inverse": 0.037146}
    assert ds.model_score(-0.1132, ZUUU_1014) == 0.048447
    assert abs(sum(parts.values()) - ds.model_score(-0.1132, ZUUU_1014)) < 1e-5
    assert ds.model_inputs(None, {}) == dict.fromkeys(ds.MODEL_COEFFICIENTS, 0.0)


def test_the_snapshot_carries_them() -> None:
    source = (ROOT / "bayes_basket_target" / "direction_signal.py").read_text(encoding="utf-8")
    assert '"model_inputs": signal["model_inputs"], "model_parts": signal["model_parts"],' in source


def _function(name: str) -> str:
    start = DASHBOARD.index(f"function {name}(")
    depth = 0
    for index in range(DASHBOARD.index("{", start), len(DASHBOARD)):
        depth += {"{": 1, "}": -1}.get(DASHBOARD[index], 0)
        if depth == 0:
            return DASHBOARD[start:index + 1]
    raise AssertionError(name)


def test_the_card_draws_four_rows_and_the_total() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the card")
    D = {"model_score": 0.048447, "model_side": 1, "model_parts": ds.model_parts(-0.1132, ZUUU_1014),
         "model_inputs": ds.model_inputs(-0.1132, ZUUU_1014)}
    start = DASHBOARD.index("const SD_DIR_PARTS")
    script = (
        'const SD_COL = {P0: "#58a6ff"}; const sdEsc = s => String(s == null ? "" : s);\n'
        'const sdSg = (v, d) => v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d == null ? 2 : d);\n'
        + _function("sdDirColor") + "\n"
        + DASHBOARD[start:DASHBOARD.index("function sdDirParts(")] + _function("sdDirParts")
        + f"\nconsole.log(sdDirParts({json.dumps(D)}).replace(/<[^>]+>/g, '|'));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    text = "|".join(part for part in out.stdout.strip().split("|") if part)
    for piece in ("ECMWF 风（取反）|+1.16|+0.037", "ICON 日照|+0.72|+0.010", "六家露点|−0.10|+0.004",
                  "动量 M/2|−0.06|−0.003", "合计＝模型分|+0.048"):
        assert piece in text, (piece, text)
    assert "sdDirParts(D)" in DASHBOARD[DASHBOARD.index("function sdDirection("):]
