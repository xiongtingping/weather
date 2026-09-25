"""Charter 227: the charter 201 feature gate is judged by charter 211's two proper scores.

A source passes on >= 20 evaluable event days only when the bucket Brier and
the log score (of the vector floored at charter 224's 1e-3) both clear all
three robustness forms -- the change, the change without the two best days,
and the change against the random-sign control.  dRPS is a diagnostic.
"""
from __future__ import annotations

import ast
import inspect
import math
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_charter_201_p0_feature_shadow import ORDER, _ssot  # noqa: F401 - puts scripts/ on the path
import fit_deterministic_feature_gate as G  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SIX = [f"d_{name}{form}" for name in G.PROPER_SCORES for form in G.ROBUST_FORMS]


def _fit(tmp_path, monkeypatch, informative):
    from bayes_basket_target import regional_point_debias as D  # noqa: PLC0415

    monkeypatch.setattr(D, "MIN_TRAIN_DAYS", 3)
    db = _ssot(tmp_path, informative=informative)
    return G.fit(db, now=datetime(2026, 9, 23, 6, tzinfo=timezone.utc))


def test_an_informative_source_clears_all_six_bounds(tmp_path, monkeypatch):
    payload = _fit(tmp_path, monkeypatch, informative=True)
    assert payload["gate_rule"] == G.GATE_RULE == "proper_scores_brier_logscore_v2"
    stat = payload["stations"]["SBGR"]["sources"]["ECMWF_AIFS_SINGLE_POINT"]
    assert stat["verdict"] == "PASS"
    assert all(stat[key][2] < 0 for key in SIX)
    # RPS is still read, and decides nothing.
    assert stat["d_rps"] and stat["d_rps_drop_best2"] and stat["d_rps_vs_random_sign"]


def test_an_uninformative_source_fails(tmp_path, monkeypatch):
    payload = _fit(tmp_path, monkeypatch, informative=False)
    stat = payload["stations"]["SBGR"]["sources"]["ECMWF_AIFS_SINGLE_POINT"]
    assert stat["verdict"] == "FAIL"
    assert not all(stat[key][2] < 0 for key in SIX)
    assert payload["stations"]["SBGR"]["selected_system"] is None


def test_the_verdict_reads_the_proper_scores_not_rps():
    tree = ast.parse(textwrap.dedent(inspect.getsource(G.fit)))
    passed = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "passed" for t in node.targets))
    source = ast.unparse(passed.value)
    assert "PROPER_SCORES" in source and "ROBUST_FORMS" in source
    assert "rps" not in source and "nll" not in source
    assert G.PROPER_SCORES == ("brier", "logscore")
    assert G.ROBUST_FORMS == ("", "_drop_best2", "_vs_random_sign")


def test_the_log_score_reads_the_served_floor():
    vector = {key: 0.0 for key in ORDER}
    vector["22°C"] = 1.0
    score = G._logscore(vector, ORDER, "24°C")          # the truth sat on a zero rung
    assert score < 8.0 and math.isfinite(score)          # not -log(1e-12) = 27.6
    assert G._brier(vector, ORDER, "22°C") == pytest.approx(0.0)


def test_charter_227_records_the_rule():
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter.split("\n227. ", 1)[1].split("\n", 1)[0]
    for needle in ("proper_scores_brier_logscore_v2", "ΔRPS", "六个", "20", "裁决人：用户"):
        assert needle in clause
