"""The money board: five readings of what the book is worth, in one container.

User ruling, 2026-09-13: 把「组合风险指标」、入场的九个数、钱 · 这本账值多少、
实盘订单 · 数量 / ROI / PnL、实盘意向簿 这五块融合在一个大容器内，做成一个
数据看板，分类展示.  They sat in three places: the 钱 lamp row in the head,
two sections under it, and two cards inside 站点实盘上线闸门.

The same evening the paper category's three blocks became one board
(有些指标重复了，把纸面账本的所有数据看板重新整理合并).

User ruling, 2026-09-16: 纸面账本的数据同样改为实盘数据，把「实盘订单 · 数量 /
ROI / PnL」融合在一起，上面是数据看板，下方是站明细; the ledger is a rule
comparator from that day.  One category, 实盘, whose first pane draws the data
board over the order card and whose second is the intent book.

Asserted on rendered output, with every block stubbed: the board's own job is
the grouping, the tabs and the guards, and the blocks each have their own
tests (the data board's are in tests/test_entry_economics_section.py).  The
placement checks read the page's call graph, not its wording.
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
    _function("liveMoney"),
    _function("moneyBoard"),
    _function("safe"),
])

STUBS = """
const h = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const moneyDataBoard = () => { if (D.__board_throws) throw new Error('boom'); return '<div>DATA</div>'; };
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


def test_one_live_category_with_the_data_board_over_the_orders():
    html = _render(LIVE)

    # Charter 146 (6cf2a530) gave the tiebreak lane the third pane.
    assert _panes(html) == [
        ("live", "orders", False), ("live", "book", True),
        ("live", "tiebreak", True),
        # Charter 166: the tail lane's own page, beside the tiebreak lane's.
        ("live", "p1tail", True),
        # Charter 203: the consensus lane's own page, after the other two.
        ("live", "p1consensus", True),
        # Charter 231: the Proper lane's own page, last.
        ("live", "proper", True),
    ]
    assert 'data-daytabs="money-paper"' not in html
    for label in ("实盘", "实盘 · 数据看板与站明细", "实盘意向簿"):
        assert label in html
    for body in ("DATA", "ORDERS 7", "BOOK 7"):
        assert body in html
    # 上面是数据看板，下方是站明细.
    assert html.index("DATA") < html.index("ORDERS 7")


def test_every_tab_has_a_pane_and_a_single_block_draws_no_strip():
    html = _render(LIVE)

    for cat in re.finditer(r'data-daytabs="money-\w+"(.*?)(?=data-daytabs="money-|\Z)', html, re.S):
        tabs = re.findall(r'<button type="button" data-day="(\w+)"', cat.group(1))
        panes = re.findall(r'data-daypane="(\w+)"', cat.group(1))
        assert tabs == (panes + ["all"] if len(panes) > 1 else [])
        assert ('class="daytabs"' in cat.group(1)) is (len(panes) > 1)


def test_a_block_that_throws_does_not_blank_the_others():
    html = _render(LIVE | {"__board_throws": True})

    assert "渲染失败" in html and "boom" in html
    for body in ("ORDERS 7", "BOOK 7"):
        assert body in html


def test_live_blocks_say_absent_instead_of_drawing_empty_tiles():
    html = _render({"live_readiness": {"available": False}})

    assert "ORDERS" not in html and "BOOK" not in html
    assert html.count("live_readiness 不可用") == 2
    # The data board reads the live book, not the readiness payload.
    assert "DATA" in html


# ---------------------------------------------------------------- placement


def _assembly() -> str:
    start = SOURCE.index('document.getElementById("app").innerHTML =')
    return SOURCE[start:SOURCE.index("</footer>", start)]


def test_the_board_sits_between_the_head_and_live_readiness():
    page = _assembly()

    head = page.index('safe(head, "页头")')
    board = page.index('sec(moneyBoard, "资金看板", "sec-money")')
    live = page.index('sec(liveReadiness, "站点实盘上线闸门", "sec-live")')
    assert head < board < live
    assert "sec(entryEconomics" not in page and "sec(riskMetrics" not in page


@pytest.mark.parametrize("name", ["moneyDataBoard", "liveOrderCard", "orderBookCard"])
def test_each_block_is_drawn_by_the_board_and_nowhere_else(name):
    """A second call site would print the same numbers twice on one page."""

    board = [_balanced("const MONEY_BOARD = ", "[", "]")]
    rest = SOURCE
    for part in board:
        rest = rest.replace(part, "")
    rest = re.sub(rf"function {name}\(", "", rest)
    assert not re.findall(rf"(?<![\w.]){name}\(", rest), f"{name} is still called outside the board"
    assert any(re.search(rf"(?<![\w.]){name}\(", part) for part in board)


def test_the_head_no_longer_carries_the_money_row():
    head = _function("head")
    lamps = _function("overviewLamps")

    assert "O.money" not in head and "money:[]" not in head
    assert 'push("money"' not in lamps and "money:[]" not in lamps
