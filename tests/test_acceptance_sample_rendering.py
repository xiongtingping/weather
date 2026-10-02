"""The acceptance sample, as the page draws it (user ruling, 2026-09-17).

The windows the model claims at >=45% while the market's mid sits under 30%
are the shape the band table cannot see and the lanes that read the market
-- beta and L4 -- must catch first.  Each lane's section draws the class
from its own realisation payload; these tests render those two blocks with
node and pin what they say in each of their states, so a payload that
changes shape cannot leave the page quietly printing the wrong cell.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

SOURCE = C.HTML_TEMPLATE


def _function(name: str) -> str:
    start = SOURCE.index(f"function {name}(")
    depth = 0
    for index in range(SOURCE.index("{", start), len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
        elif SOURCE[index] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start:index + 1]
    raise AssertionError(name)


def _run(payload: dict, call: str) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    script = (
        "const D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        "const h = (s) => String(s == null ? '' : s);\n"
        + _function("posteriorRealisation")
        + f"\nconsole.log({call});\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# The beta section and its acceptance rows left the page with the beta
# lane's board readings (2026-09-26); the L4 table keeps its own.


def _cell(n: int, hit: float) -> dict:
    return {"n": n, "station_days": n, "days": n, "price": 0.2, "hit": hit,
            "claimed_edge": 0.1, "hit_minus_price": hit - 0.2, "ci": [hit - 0.3, hit - 0.1]}


REALISATION = {
    "since": "2026-09-03", "windows": 20, "judged": 10, "no_fit": 5, "accumulating": 5,
    "no_vector": 0, "cells": {}, "by_stage": {},
    "l4_admitted": _cell(4, 0.5), "lambda_admitted": _cell(6, 0.3),
    "acceptance": {"claim_floor": 0.45, "market_ceiling": 0.30, "windows": 3, "judged": 2,
                   "all": _cell(2, 0.5), "l4_admitted": _cell(1, 1.0),
                   "lambda_admitted": _cell(2, 0.5)},
}


def test_the_l4_table_draws_the_class_whoever_bought_then_each_gate():
    text = _text(_run({}, "posteriorRealisation(" + json.dumps(REALISATION, ensure_ascii=False) + ")"))

    assert "验收样本 ：模型说 ≥45% 而市场中价 &lt;30% 的窗——已结算 3、已判 2" in text
    assert re.search(r"这类窗全部（不论谁买） 2 / 2 / 2 0\.200 0\.500 \+10\.0 点 \+30\.0 点", text)
    assert re.search(r"其中 L4 会买 1 / 1 / 1 0\.200 1\.000 \+10\.0 点 \+80\.0 点", text)
    assert re.search(r"其中实盘 λ 会买 2 / 2 / 2", text)


def test_the_l4_table_says_when_the_page_predates_the_field():
    stale = {k: v for k, v in REALISATION.items() if k != "acceptance"}
    text = _text(_run({}, "posteriorRealisation(" + json.dumps(stale, ensure_ascii=False) + ")"))

    assert "验收样本：本页数据还没带这一项" in text
