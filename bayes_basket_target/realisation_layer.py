"""The realisation layer: a betting belief conditioned on the market (charter 254).

User ruling 2026-10-05 「上影子」, after 「如何在模型概率向量后增加一层……你说 90% 的
时候，最好 90% 都是对的……在保证校准度的前提下，最大化清晰度」.

The weather vector ``p`` answers "the weather alone".  This layer answers
"having seen ``p`` and the market ``q``": ``b_k = P(Y = k | p, q)``, the
belief an edge is priced on.  It changes no probability the stages serve and
enters no calibration chain; it is read by the betting lanes, in shadow.

Form (``RULE_V1``, research docs/research/realisation_layer_20261005, arm M5;
``RULE`` adds charter 265's three change columns below):
a multiclass log pool over the ladder, renormalised,

    log b_k  =  const + a * log q_k                      the market, sharpened
                + g0 * [k is the market's modal bucket]
                + g1 * [k is one rung from it]
                + (w_w * D_width_k + w_s * D_shape_k) * [centres agree]
                + (v_w * D_width_k + v_s * D_shape_k) * [centres disagree]

on the rung axis of the station-day's own ladder (a C ladder's rung is 1 C, an
F ladder's 2 F; the rule is ordinal and not converted, charter 53).  The
weather vector's disagreement with the market is split into three parts by
moving ``q`` onto ``p``: ``v1`` is ``q`` shifted to ``p``'s mean, ``v2`` is
``v1`` stretched to ``p``'s spread, and ``p`` itself; ``D_width = log(v2/v1)``
and ``D_shape = log(p/v2)``.  The centre part, ``log(v1/q)``, is left out:
measured on the served record its weight was zero (10-04 acceptance).  The
centres agree when the two means are within ``CENTRE_AGREE_RUNGS``.

The seven numbers (ten under charter 265) are fitted by the multiclass log loss of the floored
ladder (``fit``), each station-day weighted one; the band around ``b`` is the
10% and 90% points over ``theta`` refitted on bootstrapped event days.
Measured walk-forward over 13 event days (5,465 P0 decisions more than 36 h
out): 0 of 24 reliability bins outside their 95% interval, 0.5-0.7 said
0.560 and landed 0.561, mean top bucket 0.440 against the market's 0.383,
LogScore 0.046 nat better than the market's own vector.

Pure: numpy and the served floor, no database.  ``realisation_store`` fits
and persists; the lanes read through ``realisation_store.shadow_legs``.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .materialization import floor_probabilities

RULE_V1 = "market_conditioned_realisation_v1"
PARAM_NAMES_V1 = (
    "market_power",
    "width_agree", "shape_agree",
    "width_disagree", "shape_disagree",
    "modal_bucket", "beside_mode",
)
START_V1 = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
# Charter 265 (user rulings 2026-10-10 「加」「作为模型的分辨率和条件性」「改」):
# three more columns, the latest-run change of AIFS ENS, AIFS single and GEFS
# (``run_change.SYSTEMS`` order), each in rungs and weighed by its freshness,
# laid on the ladder as a tilt about the market's mode -- the column for
# bucket k is x * clip(k - mode, -CHANGE_REACH_RUNGS, CHANGE_REACH_RUNGS) -- so
# a positive weight moves mass to the side the change points to and leaves
# the mode where the market has it (research: the change moves mass between
# the mode's two sides, not the mode itself).  The first seven columns are
# v1's unchanged, so a v1 artifact still reads them (the locked 10-16
# acceptance reads v1 with its frozen theta).
RULE = "market_conditioned_realisation_v2_run_change"
CHANGE_PARAM_NAMES = ("change_aifs_ens", "change_aifs_single", "change_gefs")
PARAM_NAMES = PARAM_NAMES_V1 + CHANGE_PARAM_NAMES
START = START_V1 + (0.0, 0.0, 0.0)
CHANGE_REACH_RUNGS = 2.0
CENTRE_AGREE_RUNGS = 0.25
#: The variance of a uniform density inside one rung.
WITHIN_RUNG_VARIANCE = 1.0 / 12.0
#: The band printed beside ``b``: these quantiles of the bootstrapped fits.
BAND_QUANTILES = (0.10, 0.90)
NEWTON_ITERATIONS = 60
NEWTON_TOLERANCE = 1e-9
_ROOT_ITERATIONS = 100
#: Root tolerances on the rung axis: the shift that moves ``q`` to ``p``'s
#: mean, the shift inside a stretch, and the log of the stretch.
SHIFT_TOLERANCE = 1e-6
STRETCH_SHIFT_TOLERANCE = 1e-5
LOG_SCALE_TOLERANCE = 1e-4


# ---------------------------------------------------------------------------
# Ladder geometry on the rung axis.


def floored(vector: Sequence[float]) -> np.ndarray:
    """Normalised, then the served model-zero floor (charter 224) -- the
    function serving calls, not a restatement of it."""

    values = np.asarray([float(v) for v in vector], dtype=float)
    total = float(values.sum())
    if not (total > 0.0 and math.isfinite(total)):
        raise ValueError("realisation_vector_invalid")
    out, _ = floor_probabilities({index: v / total for index, v in enumerate(values)})
    return np.asarray([out[index] for index in range(len(values))], dtype=float)


def moments(vector: np.ndarray) -> tuple[float, float]:
    """Mean and spread on the rung axis, each rung a uniform block."""

    index = np.arange(len(vector), dtype=float)
    mean = float((index * vector).sum())
    return mean, math.sqrt(float(((index - mean) ** 2 * vector).sum()) + WITHIN_RUNG_VARIANCE)


def _affine(vector: np.ndarray, shift: float, scale: float, about: float) -> np.ndarray:
    """The ladder density moved by x -> about + scale (x - about) + shift and
    re-binned; the end rungs take whatever leaves the ladder."""

    count = len(vector)
    low = about + scale * (np.arange(count) - 0.5 - about) + shift
    high = low + scale
    edges_low = np.arange(count) - 0.5
    edges_high = edges_low + 1.0
    edges_low[0], edges_high[-1] = -np.inf, np.inf
    overlap = np.clip(
        np.minimum(high[:, None], edges_high[None, :]) - np.maximum(low[:, None], edges_low[None, :]),
        0.0, None,
    )
    return (vector[:, None] * overlap / scale).sum(axis=0)


def _root(function: Any, low: float, high: float, tolerance: float) -> float:
    """The root of an increasing ``function`` on [low, high] by Brent's method,
    clamped to the ends when it has none there."""

    a, b = low, high
    fa, fb = function(a), function(b)
    if fa >= 0.0:
        return low
    if fb <= 0.0:
        return high
    c, fc = a, fa
    d = e = b - a
    for _ in range(_ROOT_ITERATIONS):
        if fb * fc > 0.0:
            c, fc = a, fa
            d = e = b - a
        if abs(fc) < abs(fb):
            a, b, c = b, c, b
            fa, fb, fc = fb, fc, fb
        tol = 2.0 * 2.2e-16 * abs(b) + 0.5 * tolerance
        half = 0.5 * (c - b)
        if abs(half) <= tol or fb == 0.0:
            return b
        if abs(e) >= tol and abs(fa) > abs(fb):
            s = fb / fa
            if a == c:
                p_, q_ = 2.0 * half * s, 1.0 - s
            else:
                q0, r0 = fa / fc, fb / fc
                p_ = s * (2.0 * half * q0 * (q0 - r0) - (b - a) * (r0 - 1.0))
                q_ = (q0 - 1.0) * (r0 - 1.0) * (s - 1.0)
            if p_ > 0.0:
                q_ = -q_
            p_ = abs(p_)
            if 2.0 * p_ < min(3.0 * half * q_ - abs(tol * q_), abs(e * q_)):
                e, d = d, p_ / q_
            else:
                d = e = half
        else:
            d = e = half
        a, fa = b, fb
        b = b + (d if abs(d) > tol else (tol if half > 0 else -tol))
        fb = function(b)
    return b


def _shifted(vector: np.ndarray, mean: float, scale: float, tolerance: float) -> np.ndarray:
    about, _ = moments(vector)
    count = len(vector)
    shift = _root(lambda s: moments(_affine(vector, s, scale, about))[0] - mean,
                  -2.0 * count, 2.0 * count, tolerance)
    return _affine(vector, shift, scale, about)


def shifted_to(vector: np.ndarray, mean: float) -> np.ndarray:
    """``vector`` moved along the ladder until its mean is ``mean``."""

    return _shifted(vector, mean, 1.0, SHIFT_TOLERANCE)


def matched_to(vector: np.ndarray, mean: float, spread: float) -> np.ndarray:
    """``vector`` moved and stretched about its mean until its mean and spread
    are the targets, as near as the ladder's ends allow."""

    log_scale = _root(
        lambda ls: moments(_shifted(vector, mean, math.exp(ls), STRETCH_SHIFT_TOLERANCE))[1] - spread,
        math.log(0.05), math.log(20.0), LOG_SCALE_TOLERANCE,
    )
    return _shifted(vector, mean, math.exp(log_scale), STRETCH_SHIFT_TOLERANCE)


# ---------------------------------------------------------------------------
# The features of one decision.


def features(
    p_raw: Sequence[float], q_raw: Sequence[float], change: Sequence[float] | None = None,
) -> dict[str, Any]:
    """The pool's terms for one ladder, buckets in ladder order.

    ``p_raw`` is the weather vector as the envelope holds it and ``q_raw`` the
    market's normalised mid ladder (``market_reference_v1``), both unfloored;
    each is floored here once.  The market's modal bucket is the largest raw
    mid, the lower rung on a tie -- the mode the betting lanes read.

    ``change`` (charter 265) is ``run_change.change_columns``' three numbers;
    without it the terms are v1's seven columns, with it v2's ten.
    """

    if len(p_raw) != len(q_raw) or len(q_raw) < 2:
        raise ValueError("realisation_ladder_invalid")
    p = floored(p_raw)
    q = floored(q_raw)
    raw_q = [float(v) for v in q_raw]
    mode = max(range(len(raw_q)), key=lambda i: (raw_q[i], -i))
    mean_p, spread_p = moments(p)
    mean_q, _ = moments(q)
    v1 = floored(shifted_to(q, mean_p))
    v2 = floored(matched_to(q, mean_p, spread_p))
    agree = 1.0 if abs(mean_p - mean_q) <= CENTRE_AGREE_RUNGS else 0.0
    width = np.log(v2) - np.log(v1)
    shape = np.log(p) - np.log(v2)
    distance = np.abs(np.arange(len(q)) - mode)
    terms = np.stack([
        np.log(q),
        width * agree, shape * agree,
        width * (1.0 - agree), shape * (1.0 - agree),
        (distance == 0).astype(float), (distance == 1).astype(float),
    ], axis=1)
    if change is not None:
        if len(change) != len(CHANGE_PARAM_NAMES):
            raise ValueError("realisation_change_columns_invalid")
        reach = np.clip(np.arange(len(q)) - mode, -CHANGE_REACH_RUNGS, CHANGE_REACH_RUNGS)
        terms = np.concatenate([terms, np.stack([float(x) * reach for x in change], axis=1)], axis=1)
    return {
        "terms": terms, "p": p, "q": q, "mode": int(mode),
        "centre_gap_rungs": abs(mean_p - mean_q), "centres_agree": bool(agree),
    }


def belief(terms: np.ndarray, theta: Sequence[float]) -> np.ndarray:
    """``b`` for one ladder."""

    z = terms @ np.asarray(theta, dtype=float)
    z = z - z.max()
    weights = np.exp(z)
    return weights / weights.sum()


def belief_band(terms: np.ndarray, draws: Sequence[Sequence[float]]) -> tuple[np.ndarray, np.ndarray]:
    """The ``BAND_QUANTILES`` of ``b`` over the bootstrapped fits."""

    stacked = np.stack([belief(terms, theta) for theta in draws])
    low, high = np.quantile(stacked, BAND_QUANTILES, axis=0)
    return low, high


# ---------------------------------------------------------------------------
# The fit: the multiclass log loss of the pool, by damped Newton steps.


def stack(decisions: Sequence[Mapping[str, Any]]) -> dict[str, np.ndarray]:
    """Padded arrays of many decisions' terms: ``X`` (n, k, j), ``mask``,
    ``y`` (the truth's position) and ``w`` (the decision's weight)."""

    width = max(len(d["terms"]) for d in decisions)
    count = len(decisions)
    # v1's seven columns or v2's ten (charter 265): the rows say which.
    columns = {np.asarray(d["terms"]).shape[1] for d in decisions}
    if len(columns) != 1:
        raise ValueError("realisation_terms_width_mixed")
    terms = np.zeros((count, width, columns.pop()))
    mask = np.zeros((count, width), dtype=bool)
    for row, decision in enumerate(decisions):
        k = len(decision["terms"])
        terms[row, :k] = decision["terms"]
        mask[row, :k] = True
    return {
        "X": terms, "mask": mask,
        "y": np.asarray([int(d["y"]) for d in decisions]),
        "w": np.asarray([float(d["weight"]) for d in decisions]),
    }


def _pool(arrays: Mapping[str, np.ndarray], theta: np.ndarray) -> np.ndarray:
    z = arrays["X"] @ theta
    z = np.where(arrays["mask"], z, -np.inf)
    z = z - z.max(axis=1, keepdims=True)
    weights = np.exp(z)
    return weights / weights.sum(axis=1, keepdims=True)


def log_loss(arrays: Mapping[str, np.ndarray], theta: Sequence[float], weights: np.ndarray | None = None) -> float:
    b = _pool(arrays, np.asarray(theta, dtype=float))
    w = arrays["w"] if weights is None else weights
    picked = b[np.arange(len(arrays["y"])), arrays["y"]]
    return float(-(w * np.log(picked)).sum() / w.sum())


def fit(
    arrays: Mapping[str, np.ndarray],
    *,
    weights: np.ndarray | None = None,
    start: Sequence[float] | None = None,
) -> np.ndarray:
    """``theta`` minimising the weighted multiclass log loss.  Without
    ``start``, the market itself (``START`` cut to the rows' columns: v1's
    seven or v2's ten)."""

    X, y = arrays["X"], arrays["y"]
    w = arrays["w"] if weights is None else weights
    rows = np.arange(len(y))
    theta = np.asarray(START[: X.shape[2]] if start is None else start, dtype=float).copy()

    def objective(t: np.ndarray) -> float:
        b = _pool(arrays, t)
        return float(-(w * np.log(b[rows, y])).sum())

    current = objective(theta)
    for _ in range(NEWTON_ITERATIONS):
        b = _pool(arrays, theta)
        mean_x = np.einsum("nk,nkj->nj", b, X)
        gradient = -((w[:, None] * (X[rows, y] - mean_x)).sum(axis=0))
        second = np.einsum("n,nk,nkj,nkl->jl", w, b, X, X) - np.einsum("n,nj,nl->jl", w, mean_x, mean_x)
        step = np.linalg.solve(second + 1e-9 * np.eye(len(theta)), gradient)
        scale = 1.0
        while scale > 1e-6:
            trial = theta - scale * step
            value = objective(trial)
            if value <= current:
                break
            scale *= 0.5
        else:
            break
        moved = float(np.abs(trial - theta).max())
        theta, current = trial, value
        if moved < NEWTON_TOLERANCE:
            break
    return theta


# ---------------------------------------------------------------------------
# One leg's edge on the belief.


def leg_edge(
    *, side: str, b: float, b_low: float, b_high: float, price: float, fee_per_share: float,
) -> dict[str, float]:
    """What the belief says one share of the leg is worth over its price.

    ``price`` is what the leg pays per share on its own side (a NO leg's price
    is the NO price); ``fee_per_share`` the venue's fee on it (zero for a
    post-only rung).  ``edge`` reads ``b`` itself, ``edge_sure`` the band's
    unfavourable end: the low end for YES, the high end for NO.
    """

    if side == "YES":
        return {"edge": b - price - fee_per_share, "edge_sure": b_low - price - fee_per_share}
    if side == "NO":
        return {"edge": (1.0 - b) - price - fee_per_share,
                "edge_sure": (1.0 - b_high) - price - fee_per_share}
    raise ValueError(f"realisation_side_unknown:{side}")


__all__ = [
    "BAND_QUANTILES", "CENTRE_AGREE_RUNGS", "CHANGE_PARAM_NAMES", "CHANGE_REACH_RUNGS", "PARAM_NAMES",
    "PARAM_NAMES_V1", "RULE", "RULE_V1", "START", "START_V1",
    "belief", "belief_band", "features", "fit", "floored", "leg_edge", "log_loss",
    "matched_to", "moments", "shifted_to", "stack",
]
