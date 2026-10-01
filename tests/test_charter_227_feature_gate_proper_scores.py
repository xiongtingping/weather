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
    assert payload["gate_rule"] == G.GATE_RULE == "logscore_primary_rps_brier_guardrail_v3"
    stat = payload["stations"]["SBGR"]["sources"]["ECMWF_AIFS_SINGLE_POINT"]
    assert stat["verdict"] == "PASS"
    # Charter 240: the primary (LogScore, all three robustness forms) is
    # significantly lower and both guardrails (RPS, Brier) are not worse.
    assert stat["primary_pass"] is True
    assert stat["guardrails_pass"] == {"rps": True, "brier": True}
    assert all(stat[f"d_logscore{form}"][2] < 0 for form in G.ROBUST_FORMS)
    assert stat["d_rps"][1] <= 0 and stat["d_brier"][1] <= 0


def test_an_uninformative_source_fails(tmp_path, monkeypatch):
    payload = _fit(tmp_path, monkeypatch, informative=False)
    stat = payload["stations"]["SBGR"]["sources"]["ECMWF_AIFS_SINGLE_POINT"]
    assert stat["verdict"] == "FAIL"
    assert not all(stat[key][2] < 0 for key in SIX)
    assert payload["stations"]["SBGR"]["selected_system"] is None


def test_the_verdict_ranks_the_scores():
    """Charter 240: one primary, two guardrails -- not equal vetoes."""
    assert G.PRIMARY_SCORE == "logscore"
    assert G.GUARDRAIL_SCORES == ("rps", "brier")
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
