"""真值离均值多远, as rendered: two questions per stage, a conclusion, a summary.

User ruling, 2026-09-15 (after the truth-landing cross-tab was replaced):
真值落在离服务均值多远：检验服务分布盖不盖得住真值：这个也是 -- the thirteen-
column width table and the three-state table were unreadable.  The section is
now one row per stage (which side of the served mean the truth lands on;
whether the seat window covers as often as it claims), a conclusion and a
summary computed from those rows, and the finer cuts folded underneath.

Asserted on rendered output from payloads shaped like the book: the words
have to follow the numbers in both directions.
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


def _row(cells, bias, claim, cover, mode, label="尖 <1.44"):
    return {"label": label, "cells": cells, "bias_rungs": bias,
            "seat_cells": cells, "seat_claim": claim, "seat_cover": cover,
            "mode_cells": cells, "mode_cover": mode}


def _stage(name, rows, days=2):
    return {"stage": name, "meaning": name + " 的意思", "cells": sum(r["cells"] for r in rows),
            "days": days, "since": "2026-09-14", "rows": rows, "states": []}


def _render(stages) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    payload = {"center_landing": {"schema": "center_landing_v2", "flat_threshold_rungs": 2.5,
                                  "seat_rule": {"skew_threshold_sigma": 0.35}, "stages": stages}}
    script = (
        "const D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        "const h = (s) => String(s == null ? '' : s);\n"
        + _function("centreLanding") + "\nconsole.log(centreLanding());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


# Shaped like 2026-09-15: every stage colder than its mean, every seat
# window covering less than it claims, the mode window covering more.
BOOK = [
    _stage("P0", [_row(187, -0.159, 0.8515, 0.7968, 0.8877), _row(16, -0.733, 0.5439, 0.3125, 0.8125, "中")]),
    _stage("P0-M", [_row(198, -0.534, 0.8733, 0.7663, 0.9040)]),
    _stage("P1", [_row(215, -0.338, 0.9138, 0.8605, 0.9302)]),
]


def _pills(html):
    return re.findall(r'<div class="tl-verdict"><span class="pill \w*">([^<]+)</span>', html)


def test_the_wide_tables_are_folded_and_the_answer_is_on_top():
    html = _render(BOOK)

    assert html.index("结论：") < html.index('<div class="tl-rows">') < html.index("<details")
    assert "席位窗声称" not in html and "均值±1实际（退役窗）" not in html


def test_the_centre_is_pooled_over_width_tiers_by_cells():
    html = _render(BOOK)
    p0 = (187 * -0.159 + 16 * -0.733) / 203

    assert f"P0 −{abs(p0):.2f} 桶" in html
    assert "都比服务均值偏冷" in html and "模型整体偏暖" in html


def test_coverage_below_the_claim_is_called_high_and_summed():
    html = _render(BOOK)

    assert _pills(html) == ["声称偏高"] * 3
    assert "实际盖住的都比模型说的少" in html
    assert "每个阶段都能多盖住" in html
    assert re.search(r"平均虚高 <b class=\"down\">\d+ 点</b>", html)
    assert "目前只有 2 个事件日" in html


def test_an_honest_warm_book_reads_the_other_way():
    stages = [
        _stage("P0", [_row(300, 0.35, 0.80, 0.81, 0.78)], days=20),
        _stage("P1", [_row(300, 0.40, 0.90, 0.91, 0.85)], days=20),
    ]
    html = _render(stages)

    assert _pills(html) == ["声称诚实"] * 2
    assert "都比服务均值偏暖" in html and "模型整体偏冷" in html
    assert "基本盖得住真值" in html
    assert "目前只有" not in html and "样本少" not in html
    assert "每个阶段都能多盖住" not in html


def test_a_centred_stage_is_called_centred():
    html = _render([_stage("P1", [_row(100, 0.05, 0.9, 0.9, 0.9)], days=10)])

    assert "基本居中" in html
