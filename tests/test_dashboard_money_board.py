"""The money board: five readings of what the book is worth, in one container.

User ruling, 2026-09-13: 把「组合风险指标」、入场的九个数、钱 · 这本账值多少、
实盘订单 · 数量 / ROI / PnL、实盘意向簿 这五块融合在一个大容器内，做成一个
数据看板，分类展示.  They sat in three places: the 钱 lamp row in the head,
two sections under it, and two cards inside Live 上线准备.

Asserted on rendered output, with every block stubbed: the board's own job is
the grouping, the tabs and the guards, and the five blocks each have their own
tests.  The placement checks read the page's call graph, not its wording.
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


def _balanced(start: str, opener: str, closer: str) -> str:
    begin = SOURCE.index(start)
    depth = 0
    for index in range(SOURCE.index(opener, begin), len(SOURCE)):
        if SOURCE[index] == opener:
            depth += 1
        elif SOURCE[index] == closer:
            depth -= 1
            if depth == 0:
                return SOURCE[begin:index + 1]
    raise AssertionError(f"{start!r} is unbalanced")


def _function(name: str) -> str:
    return _balanced(f"function {name}(", "{", "}")


BOARD = "\n".join([
    _balanced("const MONEY_BOARD = ", "[", "]") + ";",
    _function("moneyEquity"),
    _function("liveMoney"),
    _function("moneyBoard"),
    _function("safe"),
])

STUBS = """
const h = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const lampRow = (cap, lamps) => `<div class="lampdeck">${cap}|${lamps.join('')}</div>`;
const barSummary = () => '<div class="lampdeck">BAR_SUMMARY</div>';
const overviewLamps = () => D.__ledger_absent
  ? {banner: 'BANNER', money: []}
  : {banner: '', money: ['LAMP_EQUITY', 'LAMP_ROI']};
const riskMetrics = () => { if (D.__risk_throws) throw new Error('boom'); return '<h3>RISK</h3>'; };
const entryEconomics = () => '<h2>ENTRY</h2>';
const liveOrderCard = (L) => `<h3>ORDERS ${L.live_orders.intent_count}</h3>`;
const orderBookCard = (B) => `<div>BOOK ${B.total}</div>`;
"""


def _render(payload: dict) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI images all carry node
        pytest.skip("node is required to run the board's renderer")
    script = (
        "const D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        + STUBS + BOARD + "\nconsole.log(moneyBoard());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


LIVE = {"live_readiness": {"available": True,
                           "live_orders": {"intent_count": 7, "book": {"total": 7}}}}


def _panes(html: str) -> list[tuple[str, str, bool]]:
    """(category, pane key, hidden) in page order."""

    out = []
    for cat in re.finditer(r'data-daytabs="money-(\w+)"(.*?)(?=data-daytabs="money-|\Z)', html, re.S):
        for pane in re.finditer(r'data-daypane="(\w+)" id="[\w-]+"( hidden)?', cat.group(2)):
            out.append((cat.group(1), pane.group(1), bool(pane.group(2))))
    return out


def test_five_blocks_in_two_categories_one_showing_per_category():
    html = _render(LIVE)

    assert _panes(html) == [
        ("paper", "equity", False), ("paper", "risk", True), ("paper", "entry", True),
        ("live", "orders", False), ("live", "book", True),
    ]
    for label in ("纸面账本", "实盘", "权益与三边对照", "组合风险指标", "入场的九个数",
                  "实盘订单 · 数量 / ROI / PnL", "实盘意向簿"):
        assert label in html
    for body in ("LAMP_EQUITY", "BAR_SUMMARY", "RISK", "ENTRY", "ORDERS 7", "BOOK 7"):
        assert body in html
    assert html.index("LAMP_ROI") < html.index("BAR_SUMMARY") < html.index("RISK")


def test_every_tab_has_a_pane_and_every_category_can_show_all():
    html = _render(LIVE)

    for cat in re.finditer(r'data-daytabs="money-\w+"(.*?)(?=data-daytabs="money-|\Z)', html, re.S):
        tabs = re.findall(r'<button type="button" data-day="(\w+)"', cat.group(1))
        panes = re.findall(r'data-daypane="(\w+)"', cat.group(1))
        assert tabs == panes + ["all"]


def test_the_old_anchors_still_land_on_their_blocks():
    html = _render(LIVE)

    for anchor, body in (("sec-entry", "ENTRY"), ("sec-risk", "RISK")):
        start = html.index(f'id="{anchor}"')
        assert body in html[start:html.index("</div>", start)]


def test_a_block_that_throws_does_not_blank_the_other_four():
    html = _render(LIVE | {"__risk_throws": True})

    assert "渲染失败" in html and "boom" in html
    for body in ("LAMP_EQUITY", "ENTRY", "ORDERS 7", "BOOK 7"):
        assert body in html


def test_live_blocks_say_absent_instead_of_drawing_empty_tiles():
    html = _render({"live_readiness": {"available": False}})

    assert "ORDERS" not in html and "BOOK" not in html
    assert html.count("live_readiness 不可用") == 2


def test_an_absent_ledger_is_one_line_not_a_row_of_zeros():
    html = _render(LIVE | {"__ledger_absent": True})

    assert "纸面账本此刻是空的" in html
    assert "LAMP_" not in html and "BAR_SUMMARY" not in html
    assert "BANNER" not in html  # the head already carries it


# ---------------------------------------------------------------- placement


def _assembly() -> str:
    start = SOURCE.index('document.getElementById("app").innerHTML =')
    return SOURCE[start:SOURCE.index("</footer>", start)]


def test_the_board_sits_between_the_head_and_live_readiness():
    page = _assembly()

    head = page.index('safe(head, "页头")')
    board = page.index('sec(moneyBoard, "资金看板", "sec-money")')
    live = page.index('sec(liveReadiness, "Live 上线准备", "sec-live")')
    assert head < board < live
    assert "sec(entryEconomics" not in page and "sec(riskMetrics" not in page


@pytest.mark.parametrize("name", [
    "entryEconomics", "riskMetrics", "liveOrderCard", "orderBookCard", "barSummary",
])
def test_each_block_is_drawn_by_the_board_and_nowhere_else(name):
    """A second call site would print the same numbers twice on one page."""

    board = [_balanced("const MONEY_BOARD = ", "[", "]"), _function("moneyEquity")]
    rest = SOURCE
    for part in board:
        rest = rest.replace(part, "")
    rest = re.sub(rf"function {name}\(", "", rest)
    assert not re.findall(rf"(?<![\w.]){name}\(", rest), f"{name} is still called outside the board"
    assert any(re.search(rf"(?<![\w.]){name}\(", part) for part in board)


def test_the_head_no_longer_carries_the_money_row():
    head = _function("head")

    assert "O.money" not in head
    assert "barSummary" not in head
