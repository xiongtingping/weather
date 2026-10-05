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
    _function("moneyBoard"),
    _function("safe"),
])

STUBS = """
const h = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const moneyDataBoard = () => { if (D.__board_throws) throw new Error('boom'); return '<div>DATA</div>'; };
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
                           "live_orders": {"intent_count": 7}}}


def test_one_live_category_with_the_data_board_over_the_orders():
    html = _render(LIVE)

    # No tabs since 2026-10-05: the comparison leads, each lane's page folds
    # under it, in board order.
    assert "data-daytabs" not in html and 'class="daytabs"' not in html
    assert re.findall(r'<details class="mb-fold" id="([\w-]+)"', html) == [
        "sec-tiebreak", "sec-proper-bet", "sec-tail-short"]
    assert html.index("DATA") < html.index('class="mb-fold"')
    assert 'data-daytabs="money-paper"' not in html
    assert "DATA" in html
    for removed in ("实盘意向簿", "P1 尾段线", "同心线"):
        assert removed not in html
    # The 订单数量 card left on 2026-10-05.
    assert "ORDERS" not in html


def test_a_block_that_throws_does_not_blank_the_others():
    html = _render(LIVE | {"__board_throws": True})

    assert "渲染失败" in html and "boom" in html


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


@pytest.mark.parametrize("name", ["moneyDataBoard"])
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
