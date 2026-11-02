"""The basket calculator buys what the book has instead of refusing (user
2026-10-18 「22°C：需要 79.41 份，超过卖盘深度（卖盘全部只有 79.35 份…），有多少
买多少，不要因为卖盘深度阻断下单」)."""

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


def _solve(books: dict, st: dict) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the solver")
    script = (
        "const SD = {data: {taker_fee_rate: 0}}; const SD_MIN_SHARE = 0.01;\n"
        f"const BOOKS = {json.dumps(books)};\n"
        "const sdLevels = (day, b) => BOOKS[b] || []; const sdLevelsNo = () => [];\n"
        + "\n".join(_function(n) for n in ("sdCostOf", "sdDepth", "sdSharesFor", "sdHeldNo", "sdHeld", "sdHeldCost",
                                           "sdSolve"))
        + f"\nconst day = {{axis: Object.keys(BOOKS).map(b => ({{bucket: b}})), market: {{book: []}}}};\n"
        f"const r = sdSolve(day, {json.dumps(st)});\n"
        "console.log(JSON.stringify({ok: r.ok, warn: r.warn, legs: Object.fromEntries(Object.entries(r.legs)"
        ".map(([b, l]) => [b, {shares: l.shares, cost: l.cost, capped: !!l.capped}]))}));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


BREAKEVEN = {"goal": "breakeven", "mode": "taker", "fee": 0, "floor": 0, "sel": {"21°C": True, "22°C": True},
             "amt": {}, "amtNo": {}}


def test_a_break_even_leg_past_the_book_takes_the_whole_book() -> None:
    # 21°C fixed at 10 U; break-even on 22°C would need ~18 shares, the book has 10.
    st = {**BREAKEVEN, "amt": {"21°C": "10"}}
    r = _solve({"21°C": [[0.40, 1000]], "22°C": [[0.45, 4], [0.50, 6]]}, st)
    assert r["ok"] is True
    assert r["legs"]["22°C"]["shares"] == 10 and r["legs"]["22°C"]["capped"] is True
    assert abs(r["legs"]["22°C"]["cost"] - 4.8) < 1e-9 and r["legs"]["21°C"]["shares"] == 25
    assert any("卖盘全部只有 10.00 份，按 10.00 份买" in w for w in r["warn"]), r["warn"]


def test_a_book_deep_enough_is_not_capped() -> None:
    r = _solve({"21°C": [[0.40, 1000]], "22°C": [[0.45, 1000]]}, {**BREAKEVEN, "amt": {"21°C": "10"}})
    assert r["ok"] is True and not r["legs"]["22°C"]["capped"] and r["warn"] == []
    # break-even: either rung pays the whole spend
    total = sum(l["cost"] for l in r["legs"].values())
    assert all(l["shares"] >= total - 0.01 for l in r["legs"].values())


def test_a_fixed_amount_past_the_book_buys_the_whole_book() -> None:
    st = {**BREAKEVEN, "goal": "direction", "sel": {"22°C": True}, "amt": {"22°C": "100"}}
    r = _solve({"22°C": [[0.45, 4], [0.50, 6]]}, st)
    assert r["ok"] is True
    assert r["legs"]["22°C"]["shares"] == 10 and abs(r["legs"]["22°C"]["cost"] - 4.8) < 1e-9
    assert any("按全部 10.00 份" in w for w in r["warn"]), r["warn"]
