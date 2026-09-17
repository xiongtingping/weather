"""真值落位's "who was right" table, as rendered.

User ruling, 2026-09-15: the price x claim cross-tab (交叉：行＝模型概率档，列＝市场
价格档) was unreadable -- replace it with something simple, with its
conclusion and a summary.  The table groups priced buckets by how far the
model's claim sat above or below the market's entry ask and names, per
group, the side whose number the actual hit rate landed nearer.

Asserted on rendered output: the verdicts and the summary are arithmetic on
the payload, and a sentence that says 市场更准 over numbers that say the
opposite is the failure this pins.
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


def _group(key, buckets, claim, price, hit):
    label = {name: label for name, label, _, _ in C.TRUTH_LANDING_GAP_GROUPS}[key]
    return {"key": key, "label": label, "buckets": buckets, "hits": round(hit * buckets),
            "claim_mean": claim, "price_mean": price, "hit_rate": hit}


def _render(groups, by_price=None, by_claim=None) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    payload = {"truth_landing": {
        "since": "2026-08-15", "closed_station_days": 10, "adjudicated_station_days": 9,
        "capture_truncated_days": 1,
        "by_price": by_price if by_price is not None else [
            {"band": [0, 0.05], "truth_count": 1, "bucket_count": 5,
             "truth_share": 1, "hit_rate": 0.2, "value_mean": 0.02}],
        "by_claim": by_claim if by_claim is not None else [],
        "disagreement": {"unlikely_below": 0.05, "unlikely_buckets": 50,
                         "unlikely_hits": 1, "groups": groups},
    }}
    script = (
        "const D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        "const h = (s) => String(s == null ? '' : s);\n"
        + _function("truthLanding") + "\nconsole.log(truthLanding());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


# Shaped like the book on 2026-09-15.
BOOK = [
    _group("model_much", 900, 0.277, 0.106, 0.070),
    _group("model_bit", 1452, 0.140, 0.081, 0.063),
    _group("agree", 1300, 0.136, 0.136, 0.112),
    _group("market_bit", 1621, 0.097, 0.161, 0.131),
    _group("market_much", 1944, 0.145, 0.342, 0.311),
]


def _verdicts(html: str) -> list[str]:
    return re.findall(r'<div class="tl-verdict"><span class="pill \w*">([^<]+)</span>', html)


def test_the_cross_tab_is_gone():
    html = _render(BOOK)

    assert "交叉：行＝模型概率档" not in html
    assert "zone-trap" not in html and "本列合计" not in html


def test_each_group_names_the_side_the_hit_rate_landed_nearer():
    html = _render(BOOK)

    # model_much: |7.0-27.7| vs |7.0-10.6|; market_bit: 3.4 vs 3.0, inside a point.
    assert _verdicts(html) == ["市场更准", "市场更准", "一样准", "一样准", "市场更准"]


def test_the_conclusion_counts_the_groups_it_summarises():
    html = _render(BOOK)

    assert "3 组市场更准" in html and "1 组一样准" in html and "0 组模型更准" in html
    assert "分歧时该信市场" in html


def test_the_summary_prices_buying_where_the_model_is_higher():
    html = _render(BOOK)
    expected = 100 * (900 * (0.070 - 0.106) + 1452 * (0.063 - 0.081)) / (900 + 1452)

    assert f"{expected:.1f}¢" in html
    assert "模型的分歧不是赚钱机会" in html


def test_a_model_that_is_right_is_said_to_be_right():
    groups = [
        _group("model_much", 900, 0.30, 0.10, 0.28),
        _group("model_bit", 900, 0.15, 0.08, 0.14),
        _group("agree", 900, 0.13, 0.13, 0.13),
        _group("market_bit", 900, 0.09, 0.16, 0.10),
        _group("market_much", 900, 0.12, 0.34, 0.13),
    ]
    html = _render(groups)

    assert _verdicts(html) == ["模型更准"] * 2 + ["一样准"] + ["模型更准"] * 2
    assert "分歧时该信模型" in html
    assert "这部分分歧是模型的真信息" in html


def test_a_thin_group_is_not_judged():
    groups = [_group("model_much", 12, 0.30, 0.10, 0.28)] + BOOK[1:]
    html = _render(groups)

    assert _verdicts(html)[0] == "样本少"
    assert "3 组里" in html


# The two rulers, fused (user ruling, 2026-09-17: 按市场价格 and 按模型概率
# 两张表融合在一起对比).  Shaped like the book on 2026-09-17.
def _band(low, high, truths, buckets, hit, mean):
    return {"band": [low, high], "truth_count": truths, "bucket_count": buckets,
            "truth_share": 0.1, "hit_rate": hit, "value_mean": mean}


BY_PRICE = [
    _band(0.0, 0.05, 27, 6814, 0.004, 0.015),
    _band(0.05, 0.1, 62, 1840, 0.034, 0.068),
    _band(0.4, 0.45, 135, 329, 0.410, 0.418),
    _band(0.5, 1.01, 183, 307, 0.596, 0.560),
]
BY_CLAIM = [
    _band(0.0, 0.05, 124, 7669, 0.016, 0.013),
    _band(0.05, 0.1, 149, 2045, 0.073, 0.071),
    _band(0.4, 0.45, 59, 153, 0.386, 0.423),
    _band(0.5, 1.01, 33, 100, 0.330, 0.574),
]


def _band_verdicts(html: str) -> list[tuple[str, str]]:
    rows = re.findall(r'<td class="mono">(\d\.\d\d–\d\.\d\d)</td>.*?'
                      r'<span class="pill \w*">([^<]+)</span></td></tr>', html, re.S)
    return rows


def test_the_two_band_tables_are_one():
    html = _render(BOOK, BY_PRICE, BY_CLAIM)

    assert "<h3 style=\"margin-top:14px\">按市场价格（入场后 4h 中位卖一）</h3>" not in html
    assert "<h3 style=\"margin-top:14px\">按模型概率（P0 建仓时刻声称）</h3>" not in html
    assert html.count("<table>") == 1
    # Market beside model under every metric, not market's half then model's.
    pair = '<th style="border-left:1px solid var(--line)">市场</th><th>模型</th>'
    assert re.search(r"</tr><tr>(.*?)</tr></thead>", html, re.S).group(1) == pair * 5


def test_each_band_names_the_ruler_that_landed_nearer_its_own_number():
    html = _render(BOOK, BY_PRICE, BY_CLAIM)

    # 0.00: |0.4-1.5| vs |1.6-1.3| inside a point; 0.05: 3.4 vs 0.2;
    # 0.40: 0.8 vs 3.7; 0.50: 3.6 vs 24.4.
    assert _band_verdicts(html) == [
        ("0.00–0.05", "一样准"), ("0.05–0.10", "模型更准"),
        ("0.40–0.45", "市场更准"), ("0.50–1.00", "市场更准"),
    ]
    assert "市场更准 2 档</b>（0.40–0.45、0.50–1.00）" in html
    assert "模型更准 1 档</b>（0.05–0.10）" in html


def test_the_ratio_is_each_rulers_own():
    html = _render(BOOK, BY_PRICE, BY_CLAIM)
    row = re.search(r'0\.50–1\.00</td>(.*?)</tr>', html, re.S).group(1)

    assert f"{0.596 / 0.560:.2f}×" in row and f"{0.330 / 0.574:.2f}×" in row


def test_a_band_one_ruler_never_reached_is_not_judged():
    by_claim = [b for b in BY_CLAIM if b["band"][0] != 0.5]
    thin = [_band(0.4, 0.45, 3, 12, 0.25, 0.42)] + [b for b in by_claim if b["band"][0] != 0.4]
    html = _render(BOOK, BY_PRICE, thin)
    verdicts = dict(_band_verdicts(html))

    assert verdicts["0.50–1.00"] == "样本少" and verdicts["0.40–0.45"] == "样本少"
    row = re.search(r'0\.50–1\.00</td>(.*?)</tr>', html, re.S).group(1)
    assert row.count(">—</td>") == 5
