"""Charter 226: the OOS gate scores the ladder vector serving would serve -- floored.

P0 ends at the stages' one model-zero floor since charter 224; the gate's
ladder scoring (charter 213) read the unfloored vector, so a zero rung the
truth landed in cost the candidate -log(1e-12) = 27.6 nats that serving never
charges.  ``_ladder_metrics`` now floors first.  Measured as of 2026-09-25
09:00Z over 12,007 cross keys: one verdict moved (KDAL, breakage floor to
window strict), every flip on a floored holdout day.
"""
from __future__ import annotations

import ast
import inspect
import math
import textwrap
from pathlib import Path

import pytest

from bayes_basket_target import native_ensemble_oos as O
from bayes_basket_target.materialization import floor_probabilities
from tests.test_charter_213_oos_ladder_gate import _ladder

ROOT = Path(__file__).resolve().parents[1]


def test_a_zero_hit_rung_is_scored_at_the_served_floor_not_at_eps():
    ladder = _ladder()
    probability = dict(zip(ladder["ordered"], [0.0, 0.3, 0.5, 0.2, 0.0]))
    metrics = O._metrics(probability, 20.0, ladder)          # 68F settles "66°F or higher"
    served, lifted = floor_probabilities(probability)
    assert lifted == ["59°F or below", "66°F or higher"]
    assert metrics["nll"] == pytest.approx(-math.log(served["66°F or higher"]))
    assert metrics["nll"] < 8.0                               # not 27.6


def test_the_gate_scores_exactly_the_floored_vector():
    # Lifted to the floor then renormalised, a lifted rung lands a hair under
    # it, so the floor is not idempotent (P1 behaves the same): the expected
    # scores are computed on the served vector directly.
    ladder = _ladder()
    probability = dict(zip(ladder["ordered"], [0.0005, 0.2995, 0.5, 0.2, 0.0]))
    served, _ = floor_probabilities(probability)
    metrics = O._metrics(probability, 17.0, ladder)            # 62.6F -> "62-63°F"
    assert metrics["nll"] == pytest.approx(-math.log(served["62-63°F"]))
    assert metrics["brier"] == pytest.approx(
        sum((value - (1.0 if key == "62-63°F" else 0.0)) ** 2 for key, value in served.items())
    )


def test_a_vector_already_above_the_floor_scores_as_before():
    ladder = _ladder()
    probability = dict(zip(ladder["ordered"], [0.1, 0.2, 0.4, 0.2, 0.1]))
    metrics = O._metrics(probability, 17.0, ladder)
    assert metrics["nll"] == pytest.approx(-math.log(0.4))
    assert metrics["brier"] == pytest.approx(0.01 + 0.04 + 0.36 + 0.04 + 0.01)


def test_the_integer_grid_research_arm_is_untouched():
    assert O._metrics({"17": 1.0, "18": 0.0}, 18.0)["nll"] == pytest.approx(-math.log(O.EPS))


def test_the_floor_is_the_stages_one_function():
    tree = ast.parse(textwrap.dedent(inspect.getsource(O._ladder_metrics)))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "floor_probabilities"]
    assert len(calls) == 1
    imports = [node for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) and node.module == "materialization"]
    assert imports and [alias.name for alias in imports[0].names] == ["floor_probabilities"]


def test_charter_226_records_the_rule():
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter.split("\n226. ", 1)[1].split("\n", 1)[0]
    for needle in ("_ladder_metrics", "floor_probabilities", "12,007", "不升版", "裁决人：用户"):
        assert needle in clause
