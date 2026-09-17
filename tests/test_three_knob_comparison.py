"""The three knobs in one row (user ruling, 2026-09-17: 把三旋钮度数放在一排对比).

A belief in (p, m) has three first-order knobs -- alpha the level, gamma the
market's own slope, beta/w the share of the disagreement -- and two lanes
estimate them in two arithmetics.  The board puts them in one row per stage,
the beta lane beside L4 under each knob, with the bucket proxy's own fit
under them.  Pinned here: the proxy fit recovers planted knobs, and the row
prints each lane's number under the right knob in each of its states.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import calibration_dashboard as C  # noqa: E402

SOURCE = C.HTML_TEMPLATE


def test_the_proxy_fit_recovers_planted_knobs():
    rng = random.Random(3)
    rows = []
    for day in range(30):
        for _ in range(60):
            m = rng.uniform(0.02, 0.6)
            p = min(0.99, max(0.01, m + rng.uniform(-0.2, 0.2) + 0.5 * (0.3 - m)))
            prob = m - 0.01 + 0.4 * (p - m) + 0.25 * (m - 0.3) + rng.gauss(0, 0.02)
            rows.append((f"2026-09-{day + 1:02d}", p, m, int(rng.random() < max(0.0, min(1.0, prob)))))
    got = C.three_knob_fit(rows, since="2026-08-15", draws=60)

    assert got["rows"] == 1800 and got["days"] == 30
    assert abs(got["market_centre"] - 0.31) < 0.03
    assert abs(got["beta"] - 0.4) < 0.15 and abs(got["gamma"] - 0.25) < 0.15
    assert abs(got["alpha"] + 0.01) < 0.03
    for name in ("alpha", "beta", "gamma"):
        lo, hi = got[f"{name}_ci"]
        assert lo <= got[name] <= hi


def test_an_empty_proxy_says_so_without_a_fit():
    assert C.three_knob_fit([], since="2026-08-15") == {"since": "2026-08-15", "rows": 0, "days": 0}


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


def _render(payload: dict) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    script = (
        "const D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        + _function("threeKnobs") + "\nconsole.log(threeKnobs());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


BETA_P0 = {"beta": 0.05, "beta_lcb": -0.02, "beta_ucb": 0.12, "intercept": 0.001,
           "intercept_lcb": -0.01, "intercept_ucb": 0.012, "gamma": 0.21,
           "gamma_lcb": 0.17, "gamma_ucb": 0.25, "market_centre": 0.09,
           "rows": 900, "days": 20, "accumulating": False}
L4_P0 = {"alpha": 0.55, "gamma": 0.27, "w": 0.08, "w_lcb": 0.02, "w_ucb": 0.14,
         "ci": {"alpha": [0.4, 0.7], "gamma": [0.2, 0.34]},
         "decision_count": 1200, "day_count": 18, "accumulating": False}
PROXY = {"since": "2026-08-15", "rows": 13337, "days": 30, "market_centre": 0.091,
         "alpha": 0.0, "alpha_ci": [-0.004, 0.004], "beta": 0.05, "beta_ci": [-0.016, 0.104],
         "gamma": 0.209, "gamma_ci": [0.17, 0.254]}


def _payload(beta=None, l4=None, proxy=None):
    return {
        "beta_shadow": {"served": ({"P0": beta} if beta else {})},
        "posterior_lane": {"stages": [{"stage": "P0", "served": l4}] if l4 is not None else []},
        "truth_landing": ({"three_knob_proxy": proxy} if proxy else {}),
    }


def test_each_stage_row_puts_the_lanes_side_by_side_under_each_knob():
    text = _text(_render(_payload(BETA_P0, L4_P0, PROXY)))

    # alpha: beta lane | L4; gamma: beta lane | L4; beta/w: beta lane | L4; sample.
    assert re.search(
        r"P0 0\.001 \[-0\.010 … 0\.012\] 0\.550 \[0\.400 … 0\.700\] "
        r"0\.210 \[0\.170 … 0\.250\] 0\.270 \[0\.200 … 0\.340\] "
        r"0\.050 \[-0\.020 … 0\.120\] 0\.080 \[0\.020 … 0\.140\] 900 / 20 1200 / 18",
        text,
    )
    assert re.search(r"桶级代理 .*? 0\.000 \[-0\.004 … 0\.004\] — 0\.209 \[0\.170 … 0\.254\] — "
                     r"0\.050 \[-0\.016 … 0\.104\] — 13337 / 30 —", text)


def test_accumulating_and_absent_lanes_say_so():
    text = _text(_render(_payload({**BETA_P0, "accumulating": True}, None, None)))

    assert re.search(r"P0 累积中 — 累积中 — 累积中 — 900 / 20 —", text)
    assert "桶级代理 · 概率空间" not in text
    text = _text(_render(_payload(None, {**L4_P0, "accumulating": True}, None)))
    assert re.search(r"P0 — 累积中 — 累积中 — 累积中 — 1200 / 18", text)
    assert re.search(r"P0-M — — — — — — — —", text)
