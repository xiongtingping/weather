"""The money row names who 待观察 and 禁入 are made of.

User ruling, 2026-09-13: 前端显式待观察和禁入.  The money row had become one
lamp per question, so the 待观察 and 禁入 cards showed a percentage and nothing
else.  On the day of the ruling 待观察 read -27.4% and was a single station on
three settled days for 6.89 U; 禁入 read -49.7% across 29 stations, and which
stations lost what sat in a tooltip.  A reading that large on a sample that
small is exactly what a reader needs to see beside it.

The roster is asserted twice: as data (it must add up to the group's own
totals, worst money first) and as rendered output (a station with no
settlements must not be coloured as a win or a loss, and a payload without a
roster must not be drawn as an empty group).
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


# ---------------------------------------------------------------- as data


BOOK = {
    "EGLC": {"stake": 29.48, "pnl": -11.48, "days": ["d1", "d2", "d3", "d4", "d5"]},
    "RCSS": {"stake": 11.21, "pnl": -11.21, "days": ["d1", "d2"]},
    "KXYZ": {"stake": 8.00, "pnl": 3.50, "days": ["d1", "d2"]},
}


def test_the_roster_lists_every_member_worst_money_first():
    roster = C.bar_split_roster(["KXYZ", "RJTT", "EGLC", "RCSS"], BOOK)

    assert [m["station"] for m in roster] == ["EGLC", "RCSS", "RJTT", "KXYZ"]


def test_a_member_with_no_settlements_is_still_listed():
    roster = {m["station"]: m for m in C.bar_split_roster(["RJTT"], BOOK)}

    assert roster["RJTT"] == {
        "station": "RJTT", "settled_days": 0, "staked_usdc": 0.0,
        "pnl_usdc": 0.0, "roi": None,
    }


def test_the_roster_adds_up_to_the_totals_it_explains():
    members = ["EGLC", "RCSS", "KXYZ", "RJTT"]
    roster = C.bar_split_roster(members, BOOK)

    assert len(roster) == len(members)
    assert sum(m["staked_usdc"] for m in roster) == pytest.approx(sum(BOOK[s]["stake"] for s in BOOK))
    assert sum(m["pnl_usdc"] for m in roster) == pytest.approx(sum(BOOK[s]["pnl"] for s in BOOK))
    assert sum(m["settled_days"] for m in roster) == sum(len(BOOK[s]["days"]) for s in BOOK)


# ---------------------------------------------------------------- as rendered


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
        + PRELUDE + _const_arrow("barRoster") + "\n"
        + "console.log(barRoster());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _group(key: str, members: list[dict] | None) -> dict:
    g = {
        "stations": len(members or []),
        "settled_days": sum(m["settled_days"] for m in members or []),
        "staked_usdc": sum(m["staked_usdc"] for m in members or []),
        "pnl_usdc": sum(m["pnl_usdc"] for m in members or []),
    }
    if members is not None:
        g["members"] = members
    return g


def test_both_groups_are_named_with_their_sample_size():
    probation = C.bar_split_roster(["MPMG"], {"MPMG": {"stake": 6.89, "pnl": -1.89, "days": [1, 2, 3]}})
    barred = C.bar_split_roster(["EGLC", "RCSS"], BOOK)

    html = _render({"probation": _group("probation", probation), "barred": _group("barred", barred)})

    assert "⏳ 待观察" in html and "⛔ 禁入" in html
    assert "1 站 · 3 个结算站日" in html
    assert ">MPMG</span> 3日 -1.89U" in html
    assert html.index("EGLC") < html.index("RCSS")


def test_a_station_with_no_settlements_is_neither_a_win_nor_a_loss():
    barred = C.bar_split_roster(["RJTT", "KXYZ"], BOOK)

    html = _render({"barred": _group("barred", barred)})
    chip = html[html.rindex('<span class="pill', 0, html.index(">RJTT<")):html.index(">RJTT<")]

    assert "无结算" in html
    assert "green" not in chip and "red" not in chip
    assert '<span class="pill green"' in html  # KXYZ made money


def test_a_payload_without_a_roster_is_not_drawn_as_an_empty_group():
    html = _render({"barred": _group("barred", None) | {"stations": 29}})

    assert "29 站" in html
    assert "（空）" not in html


def test_a_group_that_is_genuinely_empty_says_so():
    html = _render({"probation": _group("probation", [])})

    assert "（空）" in html


def test_nothing_to_show_renders_nothing():
    assert _render({}).strip() == ""


def test_the_roster_sits_directly_under_the_money_row():
    body = SOURCE[SOURCE.index('"钱 · 这本账值多少'):]
    close = body.index('.join("")')

    assert "lampRowSafe(barRoster" in body[close:close + 120]
