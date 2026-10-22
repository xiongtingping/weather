"""The board's live sections read the live book and nothing else."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_board_counts_station_day_verdicts_from_the_live_book_only() -> None:
    """每日统计 reads one decision of a day's verdict.

    It used to be free to compute 篮子命中/持仓命中 on its own; the verdict
    lives in ``live_book.station_days`` and the caller may not restate the
    membership test.  (The go-live table's per-station hit columns left the
    board on 2026-10-05.)
    """

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    days = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "collect_live_days"
    )
    calls = {
        ast.unparse(node.func) for node in ast.walk(days)
        if isinstance(node, ast.Call)
    }
    assert "live_book.station_days" in calls
    assert "live_book.stage_summary" not in calls


def _template_function(template: str, name: str) -> str | None:
    match = re.search(
        r"\nfunction " + name + r"\(\)\{(.*?)\n\}\n", template, re.S
    )
    return match.group(1) if match else None


def test_the_board_hands_these_sections_the_live_payload_only() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_build_payload"
    )
    returned = next(
        node for node in reversed(fn.body) if isinstance(node, ast.Return)
    )
    keys = {k.value for k in returned.value.keys if isinstance(k, ast.Constant)}
    # live_selection left with the module (user 2026-10-22 「删live_selection」):
    # nothing on the page read it.
    assert "live_selection" not in keys
    # The alignment guard left the page (user ruling, 2026-09-16).
    assert "ledger_alignment" not in keys
    # The paper selection payload is no longer built for the page.
    assert "build_selection_payload" not in source
    template = source[source.index('HTML_TEMPLATE = r"""'):]
    for name in ("overviewLamps", "liveReadiness"):
        body = _template_function(template, name)
        assert body is not None, name
        assert "D.selection" not in body, name
    assert "D.live_selection" not in template
    assert not (ROOT / "scripts" / "live_selection.py").exists()
    # 逐站汇总（跨日） merged into the go-live table, 没建仓是因为什么 moved
    # into 逐站逐日明细: neither is a section of its own any more.  The
    # basket's refusal, coverage and hit-rate sections -- and the roster's
    # why-not-built rows -- left the page on 2026-09-26 with the basket's
    # selection readings (user: 「全面检查旧的内容」「整区删除」).
    for gone in ("stationSummary", "ledgerAlignment", "refusalStats",
                 "coverage", "basketHit"):
        assert _template_function(template, gone) is None, gone
    assert "D.live_selection" not in (_template_function(template, "roster") or "")
    top = template[template.index("const TOP = "):]
    top = top[: top.index("];") + 2]
    assert "refusalStats" not in top and "stationSummary" not in top
    assert "paper_roi" not in template and "paper_chains" not in template
