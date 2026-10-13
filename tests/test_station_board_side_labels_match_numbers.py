"""The station board's side words match the numbers beside them (user
2026-10-13 「都是负数，为什么都是偏暖？」 on ZUUU 10-14: radiation 偏暖侧 beside
平移 −0.03°C, direction 偏暖 beside M −0.65).

Direction: a quiet M (|M| < 1) does not decide; the four-feature model does,
so the card names it (按模型) and shows the model's score.  Radiation: the
receipt's side is the day's sunshine against the station's usual; the
centred shift can be negative on a slightly sunnier day, so the label names
the sunshine and the colour follows the shift.
"""

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


def _direction_main(D: dict, R: dict) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the card")
    start = DASHBOARD.index("const SD_DIR_COMPONENTS")
    script = (
        'const SD = {data: {timezone: "Asia/Shanghai"}}; const SD_COL = {P0: "#58a6ff"};\n'
        'const sdEsc = s => String(s == null ? "" : s);\n'
        'const sdPc = (v, d) => v == null ? "—" : (100 * v).toFixed(d == null ? 1 : d) + "%";\n'
        'const sdN = (v, d) => v == null || !isFinite(v) ? "—" : Number(v).toFixed(d == null ? 2 : d);\n'
        'const sdSg = (v, d) => v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d == null ? 2 : d);\n'
        'function sdLocal(t){ return String(t).slice(5, 16); } function sdAgeSpan(t){ return "AGE"; }\n'
        + DASHBOARD[start:DASHBOARD.index("function sdDirection(")] + _function("sdDirection")
        + f"\nconst r = sdDirection({json.dumps(D, ensure_ascii=False)}, {json.dumps(R)});\n"
        + "console.log(r.main.replace(/<[^>]+>/g, '|') + '##' + r.body.replace(/<[^>]+>/g, '|'));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


ZUUU_1014 = {"side": 1, "momentum": -0.65, "momentum_side": 0, "model_score": 0.034, "model_side": 1,
             "momentum_quiet_below": 1.0, "mode": {"state": "ok", "mode": "26°C", "mode_q": 0.35},
             "momentum_components": {}, "physics": {}, "history": [], "computed_at_utc": "2026-10-13T12:26:06Z"}


def test_a_quiet_momentum_names_the_model_and_shows_its_score() -> None:
    out = _direction_main(ZUUU_1014, {"side": "WARM", "shift_c": -0.026})
    main, body = out.split("##")
    assert "偏暖|按模型" in main and "模型 +0.034" in main and "M −0.65" in main
    # Agreement is read against the shift's sign: −0.03 °C is the cold way.
    assert "与辐射平移反向" in body


def test_a_speaking_momentum_names_itself_and_no_model_score() -> None:
    main = _direction_main({**ZUUU_1014, "side": -1, "momentum": -6.02, "momentum_side": -1}, {"shift_c": 0.1})
    main = main.split("##")[0]
    assert "偏冷|按动量" in main and "模型 " not in main


def test_the_radiation_label_names_the_sunshine_and_the_colour_follows_the_shift() -> None:
    body = _function("sdEvidence")
    assert '"日照多于常年"' in body and '"日照少于常年"' in body and "偏暖侧" not in body
    assert 'const radColor = R && R.shift_c > 0 ? "var(--amber)" : R && R.shift_c < 0 ? SD_COL.P0' in body
