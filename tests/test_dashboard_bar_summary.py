"""The money row spells out 待观察 and 禁入 as group totals, not per station.

User ruling, 2026-09-13: 前端显式待观察和禁入.  The money row had become one
lamp per question, so the 待观察 and 禁入 cards showed a percentage and nothing
else; a line under the row now gives each group's station count, settled days,
stake and P&L in words.

The same evening the user ruled 这里不要显式站点明细: the per-station chips that
line briefly carried came back out, and so did the roster in the payload that
fed them.  A cached section written before that change may still carry a
roster, so the renderer is tested against one and must draw no station.
"""

from __future__ import annotations

import json
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


def _const_arrow(name: str) -> str:
    """One ``const NAME = () => {...};`` from the page, by brace matching."""

    start = SOURCE.index(f"const {name} = ")
    depth = 0
    for index in range(SOURCE.index("{", SOURCE.index("=>", start)), len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
        elif SOURCE[index] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start:index + 1] + ";"
    raise AssertionError(f"{name} has unbalanced braces")


PRELUDE = (
    "const h = (s) => String(s == null ? '' : s)"
    ".replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');\n"
    "const attr = (s) => h(s).replace(/\"/g,'&quot;');\n"
)


def _render(bar_split: dict) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI images all carry node
        pytest.skip("node is required to run the board's renderer")
    script = (
        "const D = " + json.dumps({"live_readiness": {"bar_split": bar_split}}, ensure_ascii=False) + ";\n"
        + PRELUDE + _const_arrow("barSummary") + "\n"
        + "console.log(barSummary());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


PROBATION = {"stations": 1, "settled_days": 3, "staked_usdc": 6.89, "pnl_usdc": -1.89, "roi": -0.274}
BARRED = {"stations": 29, "settled_days": 36, "staked_usdc": 170.54, "pnl_usdc": -77.54, "roi": -0.455}


def test_both_groups_are_named_with_their_totals():
    html = _render({"probation": PROBATION, "barred": BARRED})

    assert "⏳ 待观察" in html and "⛔ 禁入" in html
    assert "1 站 · 3 个结算站日 · 投入 6.89 U · 盈亏 -1.89 U" in html
    assert "29 站 · 36 个结算站日 · 投入 170.54 U · 盈亏 -77.54 U" in html


def test_a_roster_left_in_the_payload_draws_no_station():
    stale = [
        {"station": "EGLC", "settled_days": 5, "staked_usdc": 29.48, "pnl_usdc": -11.48, "roi": -0.39},
        {"station": "KDAL", "settled_days": 1, "staked_usdc": 11.9, "pnl_usdc": 3.30, "roi": 0.28},
        {"station": "KATL", "settled_days": 0, "staked_usdc": 0.0, "pnl_usdc": 0.0, "roi": None},
    ]

    html = _render({"barred": BARRED | {"members": stale}})

    assert "29 站" in html
    for station in ("EGLC", "KDAL", "KATL"):
        assert station not in html
    assert "pill" not in html
    assert "无结算" not in html
    assert "逐站" not in html


def test_nothing_to_show_renders_nothing():
    assert _render({}).strip() == ""


def test_the_summary_sits_directly_under_the_money_row():
    # The money row moved from the head into the money board on 2026-09-13;
    # the rendered order is asserted in tests/test_dashboard_money_board.py.
    start = SOURCE.index("function moneyEquity(")
    body = SOURCE[start:SOURCE.index("\n}\n", start)]

    assert body.index("O.money") < body.index("+ barSummary()")
