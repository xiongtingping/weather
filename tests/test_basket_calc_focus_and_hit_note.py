"""The basket calculator (user 2026-10-18 「篮子保本计算逻辑有误，比如我新增19度，
为什么右边还是显示20度的？」): an amount typed on a rung makes that leg the
ticket's, and the losing-hit note counts what a hit really pays -- the rung's
YES and the other rungs' NO -- as the payoff column does (ZUUU 10-18: 19°C
YES 44.96 + NO 56.97 against 113.77 spent, −11.84 U)."""

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


def _node(body: str, *names: str) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    script = ("const SD = {data: {taker_fee_rate: 0}, calc: {}}; const SD_MIN_SHARE = 0.01;\n"
              "const sdCalcState = k => (SD.calc[k] = SD.calc[k] || {sel: {}, amt: {}});\n"
              "const sdSave = () => {}; const sdRefreshCalc = () => {};\n"
              + "".join(_function(n) for n in names) + "\n" + body)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_typing_an_amount_moves_the_ticket_to_that_leg() -> None:
    got = _node(
        "const st = sdCalcState('d'); st.focus = {b: '20°C', side: 'YES'}; st.tab = 'sell';\n"
        "sdOnInput({target: {value: '30', dataset: {sdAmt: 'd', b: '19°C'}}});\n"
        "const a = {...st.focus, tab: st.tab};\n"
        "sdOnInput({target: {value: '5', dataset: {sdAmtNo: 'd', b: '21°C'}}});\n"
        "const b = {...st.focus};\n"
        "sdOnInput({target: {value: '', dataset: {sdAmt: 'd', b: '18°C'}}});\n"   # clearing moves nothing
        "console.log(JSON.stringify({a, b, c: st.focus}));",
        "sdOnInput")
    assert got["a"] == {"b": "19°C", "side": "YES", "tab": "buy"}
    assert got["b"] == {"b": "21°C", "side": "NO"} and got["c"] == {"b": "21°C", "side": "NO"}


def test_the_losing_hit_note_counts_the_other_rungs_no() -> None:
    day = {"axis": [{"bucket": b} for b in ("18°C", "19°C", "20°C", "21°C")],
           "market": {"book": [{"bucket": "19°C", "ask_levels": [[0.5, 100]]},
                               {"bucket": "20°C", "ask_levels": [[0.6, 100]]}]}}
    got = _node(
        f"const day = {json.dumps(day, ensure_ascii=False)};\n"
        "const st = {mode: 'taker', fee: 0, goal: 'breakeven', floor: 0, sel: {'19°C': true}, amt: {'19°C': '10'},"
        " held: {'20°C': '50'}, heldNo: {'21°C': '30'}, heldCost: {'20°C': '30', '21°C': '15'}};\n"
        "const res = sdSolve(day, st);\n"
        "console.log(JSON.stringify({warn: res.warn, pay: res.payoff('19°C')}));",
        "sdSolve", "sdHeldCost", "sdHeld", "sdHeldNo", "sdLevels", "sdLevelsNo", "sdSharesFor", "sdDepth", "sdCostOf")
    # 20 YES from 10 U at 0.5, plus 21°C NO 30 pays on 19°C; spent 45 + 10
    assert abs(got["pay"] - (20 + 30 - 55)) < 1e-6
    assert got["warn"] == ["19°C：命中赔付 50.00 份（YES 20.00 + 其他桶 NO 30.00）< 总花费 55.00 + 地板 0，命中这桶亏 5.00 U"]
