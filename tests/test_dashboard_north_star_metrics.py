"""The north star and its three constraints, as the page scores them.

User, 2026-09-23: 前端增加 RMSE、CRPS、NLL 三个核心指标的显式并突出; 北极星指标
是离散温度桶概率的 Reliability 和 Resolution.  The served numbers are meant to
sit on the same scale as the fusion-weight study's candidates, so these tests
pin the definitions to independent formulas rather than to the helpers'
own arithmetic.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

KEYS = [f"b{index}" for index in range(6)]


def _cell(station: str, day: str, probabilities: list[float], hit: int,
          rung_c: float = 1.0, mean_c: float = 20.0, truth: float = 20.0):
    return {
        "station": station, "event_date": day,
        "core": C._core_cell(
            dict(zip(KEYS, probabilities)), KEYS, KEYS[hit], mean_c, truth, rung_c
        ),
    }


def test_crps_matches_the_energy_form_on_the_rung_grid():
    rng = np.random.default_rng(3)
    for _ in range(50):
        p = rng.dirichlet(np.ones(6))
        hit = int(rng.integers(0, 6))
        rung = float(rng.choice([1.0, 10.0 / 9.0]))
        z = np.arange(6) * rung
        energy = p @ np.abs(z - z[hit]) - 0.5 * p @ np.abs(z[:, None] - z[None, :]) @ p
        core = C._core_cell(dict(zip(KEYS, p)), KEYS, KEYS[hit], 0.0, 0.0, rung)
        assert core["crps_c"] == pytest.approx(float(energy), abs=1e-12)


def test_murphy_matches_the_study_decomposition():
    """Same bins, same one-vs-rest weights as pareto_metrics/evaluate.py."""

    rng = np.random.default_rng(11)
    cells = [_cell(f"S{i % 7}", f"2026-09-{10 + i // 7:02d}",
                   list(rng.dirichlet(np.full(6, 0.6))), int(rng.integers(0, 6)))
             for i in range(70)]
    ours = C._core_murphy([(1.0, row["core"]) for row in cells])
    edges = np.array(C.CORE_RELIABILITY_EDGES)
    stats = np.zeros((len(edges) - 1, 5))
    for row in cells:
        p = np.array(row["core"]["probabilities"])
        y = np.zeros(len(p)); y[row["core"]["hit"]] = 1
        b = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
        w = np.full(len(p), 1 / len(p))
        for column, values in enumerate([w, w * p, w * y, w * p * p, w * p * y]):
            stats[:, column] += np.bincount(b, weights=values, minlength=len(edges) - 1)
    W, P, Y, P2, PY = stats.T
    n = W.sum(); mask = W > 0
    base = Y.sum() / n
    rel = np.sum((P[mask] - Y[mask]) ** 2 / W[mask]) / n
    res = np.sum(W[mask] * (Y[mask] / W[mask] - base) ** 2) / n
    raw = (P2.sum() - 2 * PY.sum() + Y.sum()) / n
    assert ours["reliability"] == pytest.approx(rel, abs=1e-12)
    assert ours["resolution"] == pytest.approx(res, abs=1e-12)
    assert ours["mean_bucket_brier"] == pytest.approx(raw, abs=1e-12)
    assert ours["reliability"] - ours["resolution"] + ours["uncertainty"] \
        + ours["binning_correction"] == pytest.approx(raw, abs=1e-12)


def test_a_station_day_counts_once_however_many_cells_it_served():
    one = _cell("A", "2026-09-20", [0, 0, 1, 0, 0, 0], 2)
    wrong = _cell("B", "2026-09-20", [0, 0, 0, 0, 1, 0], 2)
    many = [dict(wrong) for _ in range(9)]
    metrics = C._core_metrics([one] + many)
    assert metrics["station_days"] == 2
    # One perfect station-day and one that put everything two rungs off:
    # the busy one must not outvote the quiet one nine to one.
    assert metrics["crps_c"] == pytest.approx((0.0 + 2.0) / 2)


def test_a_zero_probability_hit_is_floored_and_counted_not_dropped():
    row = _cell("A", "2026-09-20", [0.5, 0.5, 0, 0, 0, 0], 3)
    assert row["core"]["zero_hit"]
    assert row["core"]["nll"] == pytest.approx(-math.log(C.CORE_NLL_FLOOR))
    assert C._core_metrics([row])["zero_hits"] == 1


def test_the_page_renders_both_blocks_from_a_payload():
    source = C.HTML_TEMPLATE
    assert "function northStarBlock(" in source
    assert "function fusionCandidates(" in source
    assert "[fusionCandidates," in source
    # The version comparison waits for the scorecard's event-day bar.
    assert "min_event_days" in source[source.index("function northStarBlock("):]
    # Charter 240: LogScore primary, CRPS structure, Brier guardrail; REL,
    # RES and RMSE are diagnostics and decide nothing.
    block = source[source.index("const GUARDRAIL_MAX_RELATIVE_LOSS"):source.index("function fusionCandidates(")]
    assert "GUARDRAIL_MAX_RELATIVE_LOSS = 0.02" in block and "function northStarBlock(" in block
    assert '{key:"nll", label:"LogScore（NLL）", unit:"nat", role:"primary", decide:true' in block
    for key in ("reliability", "resolution", "rmse_c"):
        assert f'{{key:"{key}"' in block and 'role:"diag"' in block
    assert "三降" not in block and "四项" not in block


def test_forward_shadow_scores_seat_and_shadow_on_the_same_days(tmp_path, monkeypatch):
    """Charter 199's forward record: one sample, two weight vectors."""

    import json as _json
    import sqlite3 as _sqlite3

    from bayes_basket_target import fusion_blend as FB

    path = tmp_path / "training.db"
    conn = _sqlite3.connect(path)
    for statement in FB.TRAINING_DDL:
        conn.execute(statement)
    base = {
        "truth_revision": 1, "settlement_timezone": "UTC",
        "root_decision_at_utc": "2026-09-23T12:00:00Z", "root_checkpoint_id": "c",
        "root_envelope_sha256": "e", "root_posterior_version": "v", "truth_c": 20.0,
        "truth_known_at_utc": "2026-09-25T00:00:00Z", "truth_id": "t",
        "systems_json": '["A","B"]', "lead_by_system_json": "{}",
        "center_error_json": _json.dumps({"A": 0.2, "B": -2.0}),
        "crps_systems_json": '["A","B"]',
        "crps_a_json": _json.dumps({"A": 0.5, "B": 1.5}),
        "crps_b_json": _json.dumps({"A": {"A": 0.4, "B": 1.2}, "B": {"A": 1.2, "B": 0.6}}),
        "probability_source": "ENVELOPE",
        "seat_weights_json": _json.dumps({"A": 0.5, "B": 0.5}),
        "family_by_system_json": "{}", "family_caps_json": "{}",
        "bucket_keys_json": '["x","y","z"]', "truth_bucket_index": 1,
        "probabilities_json": _json.dumps({"A": [0.1, 0.8, 0.1], "B": [0.6, 0.3, 0.1]}),
        "fusion_blend_state": "SHADOW_APPLIED",
        "shadow_weights_json": _json.dumps({"A": 0.7, "B": 0.3}),
        "schema": FB.FUSION_BLEND_TRAINING_SCHEMA, "created_at_utc": "2026-09-25T01:00:00Z",
    }
    for day in ("2026-09-24", "2026-09-25"):
        row = {**base, "station_id": "S", "event_date": day, "content_sha256": day}
        conn.execute(
            "INSERT INTO fusion_blend_training_fact (" + ",".join(row) + ") VALUES ("
            + ",".join("?" * len(row)) + ")", tuple(row.values()),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(FB, "FUSION_BLEND_TRAINING_DB", path)
    out = C._fusion_forward_shadow()
    assert out["station_days"] == 2 and out["event_days"] == 2
    seat, shadow = out["arms"]["seat"], out["arms"]["shadow"]
    assert seat["rmse_c"] == pytest.approx(abs(0.5 * 0.2 + 0.5 * -2.0))
    assert shadow["rmse_c"] == pytest.approx(abs(0.7 * 0.2 + 0.3 * -2.0))
    assert shadow["nll"] == pytest.approx(-math.log(0.7 * 0.8 + 0.3 * 0.3))
    assert out["better"]["rmse_c"] and out["better"]["nll"]
    # Charter 211 (amended): the switch reads the two proper scores; REL/RES
    # and RMSE/CRPS are diagnostics.
    assert out["panel"] == ["bucket_brier", "nll"]
    assert set(out["panel_better"]) == set(out["panel"])
    assert shadow["bucket_brier"] == pytest.approx(
        (0.7 * 0.1 + 0.3 * 0.6) ** 2 + (0.7 * 0.8 + 0.3 * 0.3 - 1.0) ** 2 + (0.1) ** 2)
    assert out["switch_ready"] is False     # two days, not twenty
