"""Charter 278: the realisation layer's signed, per-stage centre column.

User ruling 2026-10-22 「加」: the centre part v1 left out -- log(v1/q), v1 the
market moved to the weather vector's mean -- is back as the last column with a
free sign, fitted per belief stage.  A negative weight moves the belief's
centre away from the weather vector, past the market.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import realisation_layer as layer  # noqa: E402
from bayes_basket_target import realisation_store as rstore  # noqa: E402

Q = [0.02, 0.08, 0.25, 0.40, 0.18, 0.05, 0.02]          # market, mode at rung 3
P = [0.01, 0.03, 0.10, 0.30, 0.36, 0.15, 0.05]          # weather, leaning warm (mean ~ +0.8 rung)
CHANGE = [0.0, 0.0, 0.0]


def _mean(v):
    v = np.asarray(v, float); v = v / v.sum(); return float((np.arange(len(v)) * v).sum())


def test_the_centre_column_is_last_signed_and_equals_log_v1_over_q() -> None:
    assert layer.PARAM_NAMES[-1] == "centre_signed" and layer.CENTRE_COLUMN == 10
    assert layer.RULE == "market_conditioned_realisation_v3_signed_centre" and layer.RULE != layer.RULE_V2
    f = layer.features(P, Q, CHANGE, centre=True)
    assert f["terms"].shape == (7, 11)
    q = layer.floored(Q); v1 = layer.floored(layer.shifted_to(q, _mean(layer.floored(P))))
    assert np.allclose(f["terms"][:, layer.CENTRE_COLUMN], np.log(v1) - np.log(q))
    # The first ten columns are v2's, unchanged.
    assert np.allclose(f["terms"][:, :10], layer.features(P, Q, CHANGE)["terms"])
    assert layer.features(P, Q, centre=True)["terms"].shape == (7, 8)
    assert layer.features(P, Q)["terms"].shape == (7, 7)


def test_a_negative_weight_moves_the_belief_away_from_the_weather_vector() -> None:
    f = layer.features(P, Q, CHANGE, centre=True)
    theta = list(layer.START)                      # market_power 1, all else 0: b = q
    base = layer.belief(f["terms"], theta)
    assert np.allclose(base, layer.floored(Q))
    theta[layer.CENTRE_COLUMN] = -0.5
    away = layer.belief(f["terms"], theta)
    theta[layer.CENTRE_COLUMN] = +0.5
    toward = layer.belief(f["terms"], theta)
    mq, mp = _mean(layer.floored(Q)), _mean(layer.floored(P))
    assert mp > mq                                 # the weather vector leans warm
    assert _mean(away) < mq < _mean(toward) < mp   # negative: past the market, cold; positive: toward the model


def test_the_fit_always_carries_the_centre_column_and_the_gate_reads_it() -> None:
    rng = np.random.default_rng(278)
    decisions = []
    for n in range(240):
        q = rng.dirichlet(np.ones(7) * 2.0)
        lean = rng.choice([-1, 1])
        p = np.roll(q, lean) * 0.7 + q * 0.3
        f = layer.features(p / p.sum(), q, CHANGE, centre=True)
        # The truth lands on the far side of the market from the lean: the
        # measured record.  A fit must find a negative centre weight.
        mode = int(f["mode"]); far = int(np.clip(mode - lean, 0, 6))
        decisions.append({"terms": f["terms"], "y": far if rng.random() < 0.6 else mode, "weight": 1.0,
                          "event_date": f"2026-10-{1 + n % 20:02d}", "label_known_at": f"2026-10-{1 + n % 20:02d}T23:00:00Z",
                          "q": f["q"], "mode": mode, "centres_agree": f["centres_agree"], "station_id": f"S{n%9}"})
    arrays = layer.stack(decisions)
    theta, cols = rstore.fit_supported(arrays)
    assert layer.CENTRE_COLUMN in cols
    assert theta[layer.CENTRE_COLUMN] < 0.0
    forward = rstore.walk_forward(decisions)
    assert "without_centre" in forward and forward["without_centre"]
    gate = rstore.calibration_gate(decisions, forward)
    assert "layer_minus_layer_without_centre" in gate["logscore"]
    assert gate["logscore"]["layer_minus_layer_without_centre"][0] < 0.0   # the centre column helps here


def test_belief_of_reads_each_generation_with_its_own_columns() -> None:
    book = [{"bucket": f"{20 + i}°C", "ordinal": 20 + i, "ask_available": True,
             "ask_levels": [{"price": min(0.99, q + 0.01), "shares": 50}], "bid_levels": [{"price": max(0.01, q - 0.01), "shares": 50}]}
            for i, q in enumerate(Q)]
    p_raw = {f"{20 + i}°C": v for i, v in enumerate(P)}
    parts = {"AIFS_ENS": {"change_rungs": 0.0, "fresh": 1.0}}
    v1 = {"theta": list(layer.START_V1)}
    v2 = {"theta": list(layer.START_V1) + [0.0, 0.0, 0.0]}
    v3 = {"theta": list(layer.START)}
    assert rstore._belief_of(p_raw, book, v1)["state"] == "ok"
    assert rstore._belief_of(p_raw, book, v2)["state"] == "no_change_read"
    assert rstore._belief_of(p_raw, book, v2, parts)["state"] == "ok"
    assert rstore._belief_of(p_raw, book, v3, parts)["state"] == "ok"
    assert rstore.WITHOUT_CENTRE_COLUMNS == tuple(range(10))


def test_the_population_cache_key_leaves_the_layer_arithmetic_out() -> None:
    import inspect
    assert "getsource(layer)" not in inspect.getsource(rstore._inputs_code_sha)
    assert "getsource(layer)" in inspect.getsource(rstore.code_sha256)   # the artifact still refits on a layer change
