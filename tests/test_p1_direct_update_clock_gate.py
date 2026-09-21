"""P1 b2's clock regimes as ramps (schema v2), and the candidate that never serves.

2026-09-21 review: v1 switches its two state-by-clock blocks on and off with
steps at 6h and 10h, so the served P1 steps too.  On the real states within
half an hour of 10h, moving only the clock across it moved the belief by a
median 8.3 points (26 at most), 12 of 12 toward more rise above the edge.
v2 carries ``clock_gate`` and is continuous; v1 is read exactly as before;
the fitter walks the v2 candidate on the served block's days only when asked
(``--candidate-out``), after the served file is written, into a file nothing
serves.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from bayes_basket_target import p1_direct_update as DU
from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target.contracts import ContractError
from tests.test_charter_150_p1_joint_likelihood import _artifact, _compile
from tests.test_charter_161_p1_direct_update import _block, _fitter, _with_block

GATE = {"shape": DU.CLOCK_GATE_SHAPE, "half_width_hours": 1.0}


def _v2(block: dict, gate: dict = GATE) -> dict:
    return {**block, "schema": DU.SCHEMA_V2, "rule": DU.RULE_V2, "clock_gate": dict(gate)}


def _state(block: dict, hours: float) -> np.ndarray:
    features = {name: 0.3 for name in DU.FEATURES}
    root = {"root_rise_mass": 0.4, "root_mean_minus_high_c": 1.1}
    return np.array(DU.state_vector(features, root, hours, block))


def test_the_arrays_gates_are_the_served_gates() -> None:
    fitter = _fitter()
    hours = np.concatenate([np.linspace(-1.0, 30.0, 3101), [5.0, 6.0, 7.0, 9.0, 10.0, 11.0]])
    for width in (0.0, 0.5, 1.0, 2.0):
        late, mid = fitter.clock_gates_np(hours, width)
        for i, h in enumerate(hours):
            assert (late[i], mid[i]) == pytest.approx(DU.clock_gates(float(h), width), abs=1e-12)


def test_the_fitters_x_is_the_served_state_vector_under_a_gate() -> None:
    fitter = _fitter()
    rng = np.random.default_rng(5)
    rows = 120
    raw = rng.normal(0.0, 2.0, (rows, len(DU.STATE_COLUMNS)))
    hours = np.concatenate([rng.uniform(0.0, 26.0, rows - 6), [5.0, 6.0, 7.0, 9.0, 10.0, 11.0]])
    scaling = {
        "fallbacks": {name: 0.0 for name in DU.FEATURES},
        "mean": [float(v) for v in rng.normal(0.0, 1.0, len(DU.STATE_COLUMNS))],
        "scale": [float(v) for v in rng.uniform(0.5, 2.0, len(DU.STATE_COLUMNS))],
        "clock_gate": dict(GATE),
    }
    x = fitter.direct_x({"raw": raw, "hours": hours}, np.arange(rows), scaling)
    for i in range(rows):
        features = {name: float(raw[i, k]) for k, name in enumerate(DU.FEATURES)}
        root = {name: float(raw[i, len(DU.FEATURES) + k]) for k, name in enumerate(DU.ROOT_FEATURES)}
        assert np.allclose(x[i], DU.state_vector(features, root, float(hours[i]), scaling), atol=1e-12)


def test_v1_steps_at_both_edges_and_v2_does_not() -> None:
    v1 = _block(scale=0.3)
    v2 = _v2(v1)
    bases = {"a": (0, 0.0, 0.0, 0.0), "b": (1, 1.0, 0.0, 0.0), "c": (2, 1.0, 1.0, 0.0)}
    for edge in (DU.LATE_BELOW_HOURS, DU.MID_BELOW_HOURS):
        for block, continuous in ((v1, False), (v2, True)):
            below = DU.log_update(block, _state(block, edge - 1e-9), bases)
            above = DU.log_update(block, _state(block, edge + 1e-9), bases)
            jump = max(abs(below[k] - above[k]) for k in bases)
            assert (jump < 1e-6) is continuous, (edge, block["schema"], jump)


def test_away_from_the_ramps_v2_is_v1() -> None:
    v1 = _block(scale=0.3)
    v2 = _v2(v1)
    for hours in (0.0, 3.0, 4.9, 7.1, 8.0, 8.9, 11.1, 14.0, 26.0):
        assert np.allclose(_state(v1, hours), _state(v2, hours), atol=1e-12), hours


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: {**b, "clock_gate": dict(GATE)},                       # v1 with a gate
        lambda b: {**_v2(b), "clock_gate": None} | {"clock_gate": None},  # v2 with a null gate
        lambda b: {k: v for k, v in _v2(b).items() if k != "clock_gate"},  # v2 without one
        lambda b: _v2(b, {"shape": DU.CLOCK_GATE_SHAPE, "half_width_hours": 2.5}),
        lambda b: _v2(b, {"shape": DU.CLOCK_GATE_SHAPE, "half_width_hours": 0.0}),
        lambda b: _v2(b, {"shape": "logistic", "half_width_hours": 1.0}),
        lambda b: {**b, "schema": "p1_direct_log_update_v9"},
    ],
)
def test_a_block_whose_gate_does_not_match_its_schema_is_refused(mutate) -> None:
    with pytest.raises(ContractError):
        DU.validate_block(mutate(_block()))


def test_a_served_v2_block_names_its_rule_and_gate_and_v1_receipts_are_unchanged() -> None:
    _, v1_packets = _compile(_with_block(_block()))
    v1_receipt = json.loads(v1_packets[-1]["expected_envelope"]["lineage"][OL.LINEAGE_KEY])
    assert v1_receipt["rule"] == DU.RULE and "clock_gate" not in v1_receipt
    _, v2_packets = _compile(_with_block(_v2(_block())))
    v2_receipt = json.loads(v2_packets[-1]["expected_envelope"]["lineage"][OL.LINEAGE_KEY])
    assert v2_receipt["state"] == "APPLIED"
    assert v2_receipt["rule"] == DU.RULE_V2 and v2_receipt["clock_gate"] == GATE


def test_the_candidate_block_never_serves() -> None:
    served = _with_block(_block())
    with_candidate = {**served, DU.CANDIDATE_BLOCK_KEY: _v2(_block(seed=99), GATE) | {"state": DU.GATE_SHADOW}}
    _, plain = _compile(served)
    _, shadowed = _compile(with_candidate)
    a, b = plain[-1]["expected_envelope"], shadowed[-1]["expected_envelope"]
    assert a["probabilities"] == b["probabilities"]
    assert json.loads(a["lineage"][OL.LINEAGE_KEY])["rule"] == json.loads(
        b["lineage"][OL.LINEAGE_KEY]
    )["rule"] == DU.RULE
    # And a candidate alone, with no served block, is charter 150's g**alpha.
    _, alone = _compile({**_artifact(), DU.CANDIDATE_BLOCK_KEY: with_candidate[DU.CANDIDATE_BLOCK_KEY]})
    _, base = _compile(_artifact())
    assert alone[-1]["expected_envelope"]["probabilities"] == base[-1]["expected_envelope"]["probabilities"]


def _synthetic_walk(fitter, rows: int = 480, width: int = 6, days: int = 8):
    rng = np.random.default_rng(21)
    day = np.array([f"2026-09-{10 + i // (rows // days):02d}" for i in range(rows)], dtype=object)
    order = sorted(set(day.tolist()))
    as_of = np.array([order.index(d) * 86400.0 + 3600.0 for d in day])
    live = np.ones((rows, width), dtype=bool)
    ranks = np.arange(width)[None, :].repeat(rows, 0)
    design = {
        "prior": rng.dirichlet(np.full(width, 1.0), rows), "live": live,
        "truth": rng.integers(0, width, rows),
        "rise": (ranks >= 1).astype(float), "slope": np.maximum(ranks - 1, 0).astype(float),
        "top": (ranks == width - 1).astype(float),
        "raw": rng.normal(0.0, 1.0, (rows, len(DU.STATE_COLUMNS))),
        "hours": rng.uniform(0.0, 20.0, rows), "weight": np.ones(rows),
        "day": day, "as_of": as_of, "known_at": as_of + 43200.0,
    }
    usable = np.arange(rows)
    walk_days, scored, theta = [], {}, None
    for d in order[4:]:
        rows_d = np.where(day == d)[0]
        train = usable[design["known_at"] <= as_of[rows_d].min()]
        lam, scaling, theta = fitter.direct_choose(design, train, start=theta)
        scored[d] = (float(fitter.direct_scores(design, rows_d, scaling, theta).mean()), 0.0)
        walk_days.append((d, train, rows_d, lam, theta.copy()))
    lam, scaling, theta = fitter.direct_choose(design, usable, start=theta)
    return design, {
        "samples": [{"event_date": d} for d in order], "design": design,
        "walk_days": walk_days, "scored_by_day": scored,
        "curve": {"forward_root_nat_by_day": {d: -2.0 for d in scored}},
        "margin": 0.01, "final_design": design, "final_rows": usable,
        "final_lambda": lam, "final_theta": theta,
    }


def test_the_candidate_walk_rescoring_reproduces_the_served_walk() -> None:
    fitter = _fitter()
    design, walk = _synthetic_walk(fitter)
    candidate = fitter.fit_direct_candidate(walk, fitter.CANDIDATE_CLOCK_GATE)
    assert candidate["schema"] == DU.SCHEMA_V2 and candidate["state"] == DU.GATE_SHADOW
    assert candidate["clock_gate"] == fitter.CANDIDATE_CLOCK_GATE
    DU.validate_block(candidate)
    record = candidate["walk_forward"]
    bins = record["by_clock_bin"]
    walked = sum(len(rows_d) for _, _, rows_d, _, _ in walk["walk_days"])
    assert sum(b["rows"] for b in bins) == walked
    served_total = sum(b["served_nat"] * b["rows"] for b in bins if b["rows"])
    walk_total = sum(walk["scored_by_day"][d][0] * len(r) for d, _, r, _, _ in walk["walk_days"])
    assert served_total == pytest.approx(walk_total, abs=1e-9)
    mine_total = sum(b["candidate_nat"] * b["rows"] for b in bins if b["rows"])
    by_day_total = sum(record["direct_nat_by_day"][d] * len(r) for d, _, r, _, _ in walk["walk_days"])
    assert mine_total == pytest.approx(by_day_total, abs=1e-9)
