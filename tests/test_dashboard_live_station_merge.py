"""The live order panel: one line per station-day, and 未成交 is not 未命中.

User ruling, 2026-09-14: 把同一站点的合并在一起。为什么显示为未命中，是真的未命中
还是显示错误？

On the board that morning the 2026-09-13 date listed ZBAA four times and ZGSZ
three: a basket the disarmed flag kept from the exchange is decided again next
cycle and mints a fresh one, and each was drawn as its own line with its own
verdict.  Every one of those verdicts read 未命中, because the rule was "no
leg held is the settled bucket" -- vacuously true of baskets that held
nothing.  Five of the six stations' windows held the day's truth (ZGSZ 30-32
against 32, ZGGG 33-34 against 34, ZSPD, ZSQD, ZUCK); only ZBAA's 29-30
against 31 missed.

Asserted on rendered output from the panel's own renderer, fed rows shaped
like that morning's.
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
HELPERS_HEAD = "// Rungs read left to right, coldest first"
HELPERS_TAIL = "  .sort((x, y) => byRung(pick ? pick(x) : x, pick ? pick(y) : y));"


def _extract(name: str) -> str:
    start = SOURCE.index(f"function {name}(")
    depth = 0
    for index in range(SOURCE.index("{", start), len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
        elif SOURCE[index] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start:index + 1]
    raise AssertionError(f"{name}() has unbalanced braces")


def _render(rows: list[dict]) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI images all carry node
        pytest.skip("node is required to run the board's renderer")
    payload = {"live_readiness": {
        "available": True, "execution_mode": "production_live",
        "contracts": {"live_submit_authorized": True},
        "live_orders": {"intent_count": len(rows), "rows": rows, "plan_state": {},
                        "book": {}, "exchange": {}, "pnl": {}},
        "open_orders": {"available": True, "count": 0},
    }}
    head = SOURCE.index(HELPERS_HEAD)
    helpers = SOURCE[head:SOURCE.index(HELPERS_TAIL, head) + len(HELPERS_TAIL)]
    script = (
        "const D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        "const h = (s) => String(s == null ? '' : s);\nconst shares = (v) => String(v);\n"
        + helpers + "\n" + _extract("liveOrderCard") + "\n"
        "console.log(liveOrderCard(D.live_readiness));\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _basket(decision: str, station: str, buckets: list[str], *, truth: str = "",
            filled: tuple[str, ...] = (), at: str = "2026-09-12T07:00:00Z",
            reason: str = "live_intent_disarmed", day: str = "2026-09-13") -> list[dict]:
    return [{
        "order_intent_id": f"{decision}-{bucket}", "decision_id": decision,
        "bucket": bucket, "limit_price": 0.25, "requested_size": 8,
        "requested_notional_usdc": 2.0,
        "state": "filled" if bucket in filled else "failed",
        "reason": "" if bucket in filled else reason,
        "created_at": at, "station_id": station, "event_date": day,
        "settled_bucket": truth, "settled_bucket_source": "paper_ledger",
        "execution_window_seconds": 3600,
    } for bucket in buckets]


def _station_pills(html: str) -> list[tuple[str, str, str]]:
    """(tone, station, word) for every station verdict pill, in page order."""

    return re.findall(r'<b class="mono pill (\w*)" title="[^"]*">(\w{4}) ([^<]+)</b>', html)


MORNING = (
    _basket("zb1", "ZBAA", ["29°C", "30°C"], truth="31°C", at="2026-09-12T07:22:26Z")
    + _basket("zb2", "ZBAA", ["29°C", "30°C"], truth="31°C", at="2026-09-12T07:37:32Z")
    + _basket("zb3", "ZBAA", ["29°C", "30°C"], truth="31°C", at="2026-09-12T08:07:35Z")
    + _basket("zs1", "ZGSZ", ["30°C", "31°C", "32°C"], truth="32°C", at="2026-09-11T23:04:28Z")
    + _basket("zs2", "ZGSZ", ["30°C", "31°C", "32°C"], truth="32°C", at="2026-09-12T00:14:06Z")
    + _basket("zg1", "ZGGG", ["33°C", "34°C"], truth="34°C")
)


def test_each_station_day_is_one_line_with_one_verdict():
    html = _render(MORNING)

    assert [station for _, station, _ in _station_pills(html)] == ["ZBAA", "ZGGG", "ZGSZ"]
    assert "3 站 · 6 篮" in html
    assert "3 篮 · 6 腿 · 0 成交" in html and "2 篮 · 6 腿 · 0 成交" in html


def test_a_station_day_that_filled_nothing_is_not_a_miss():
    pills = {station: (tone, word) for tone, station, word in _station_pills(_render(MORNING))}

    assert pills == {"ZBAA": ("", "未成交"), "ZGSZ": ("", "未成交"), "ZGGG": ("", "未成交")}


def test_an_unfilled_day_says_whether_its_window_held_the_truth():
    html = _render(MORNING)

    assert '窗口不含 31°C' in html  # ZBAA 29-30
    assert '窗口含 32°C' in html and '窗口含 34°C' in html
    assert "未命中" not in html


def test_identical_unfilled_baskets_fold_into_one_line_with_a_count():
    html = _render(MORNING)
    zbaa = html[html.index(">ZBAA "):html.index(">ZGGG ")]
    zgsz = html[html.index(">ZGSZ "):]

    assert "×3 篮" in zbaa and zbaa.count("29°C 0.25×8") == 1
    assert "×2 篮" in zgsz and zgsz.count("30°C 0.25×8") == 1


def test_a_basket_that_filled_is_never_folded_and_decides_the_verdict():
    rows = (
        _basket("a", "EPWA", ["20°C", "21°C"], truth="21°C", filled=("21°C",), at="2026-09-12T01:00:00Z")
        + _basket("b", "EPWA", ["20°C", "21°C"], truth="21°C", filled=("21°C",), at="2026-09-12T02:00:00Z")
        + _basket("c", "EHAM", ["18°C", "19°C"], truth="20°C", filled=("19°C",))
    )
    html = _render(rows)
    pills = {station: (tone, word) for tone, station, word in _station_pills(html)}

    assert pills == {"EPWA": ("green", "命中"), "EHAM": ("red", "未命中")}
    epwa = html[html.index(">EPWA "):]
    assert "×2 篮" not in epwa and epwa.count("21°C 0.25×8") == 2


def test_an_unsettled_station_day_is_still_amber():
    html = _render(_basket("u", "ZUCK", ["25°C", "26°C"], day="2026-09-14"))

    assert _station_pills(html) == [("amber", "ZUCK", "未结算")]
