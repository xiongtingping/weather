"""L4 posterior layer, v1 -- pure arithmetic, shadow only.

The first-principles baseline (docs/architecture/
EDGE_SYSTEM_FIRST_PRINCIPLES_BASELINE_2026-09-17.md, §4.4 and appendix A)
states the belief the gate should read as the posterior given BOTH forecasts,
in log-odds, with the market as the prior::

    logit b_i = logit m_i + w * (logit p_i - logit m_i) + c(m_i)
    c(x)      = alpha + gamma * logit x

``m`` is the market's normalised mid vector (charter 132's reference), ``p``
the served weather vector, ``w`` the share of the log-odds disagreement that
realises, and ``c`` the market's own recalibration -- ``gamma`` is the
market's over/under-confidence, ``alpha`` its bias.  The ladder is
renormalised after the per-bucket transform so a window's belief is a sum
over one vector, exactly as the valuation lane's is.

Three parameters, all with prior 0: at ``(alpha, gamma, w) = (0, 0, 0)`` the
belief IS the market's vector, which is the lane's failure direction and the
answer "no evidence yet" gives.  ``w = 1`` with ``c = 0`` reproduces the
served claim (renormalised).  Values above 1 are legal in log-odds -- two
sources with independent error extremise -- which is one reason the lane
works in this coordinate and not in the valuation lane's linear one.

No store import here: this module is what the runtime and the estimator
both read, and the runtime must never reach the database through it.
``PM_POSTERIOR_LANE`` is ``shadow`` (record on every priced window, gate
nothing) or ``off``; ``on`` is refused until the baseline's promotion rule
(forward skill score lower bound > 0, E15) has a reader.
"""
from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

POSTERIOR_RULE = "logit_market_prior_v1"
POSTERIOR_TABLE_KEY = "posterior"
PARAM_NAMES: tuple[str, str, str] = ("alpha", "gamma", "w")
LCB_QUANTILE = 0.05
EPS = 1e-6

POSTERIOR_MODE = (
    os.environ.get("PM_POSTERIOR_LANE", "").strip().lower() or "shadow"
)
if POSTERIOR_MODE not in {"off", "shadow"}:
    raise RuntimeError(
        f"unknown posterior lane mode: {POSTERIOR_MODE} (on is not implemented;"
        " promotion needs the forward skill reader first)"
    )


def logit(x: float) -> float:
    v = min(max(float(x), EPS), 1.0 - EPS)
    return math.log(v / (1.0 - v))


def expit(s: float) -> float:
    if s >= 0.0:
        z = math.exp(-s)
        return 1.0 / (1.0 + z)
    z = math.exp(s)
    return z / (1.0 + z)


def _params(params: Mapping[str, Any] | Sequence[float]) -> tuple[float, float, float]:
    if isinstance(params, Mapping):
        return (
            float(params.get("alpha", 0.0) or 0.0),
            float(params.get("gamma", 0.0) or 0.0),
            float(params.get("w", 0.0) or 0.0),
        )
    alpha, gamma, w = (float(x) for x in params)
    return alpha, gamma, w


def posterior_score_v1(p: float, m: float, params: Mapping[str, Any] | Sequence[float]) -> float:
    """The per-bucket log-odds before renormalisation."""

    alpha, gamma, w = _params(params)
    lm = logit(m)
    return lm + w * (logit(p) - lm) + alpha + gamma * lm


def posterior_vector_v1(
    p_weather: Mapping[str, float],
    m_market: Mapping[str, float],
    params: Mapping[str, Any] | Sequence[float],
    *,
    excluded: Iterable[str] = (),
) -> dict[str, float] | None:
    """The belief over the market's contract, renormalised; None when empty.

    The contract is the market's bucket set.  A bucket a hard fact has
    excluded is zeroed before the renormalisation, as on the valuation lane,
    so a lagging book cannot hand a settled loss any probability.  A bucket
    the weather vector does not name is read at ``EPS``: the claim there is
    "nothing", and in log-odds that is a large negative disagreement, which is
    what the fit saw for such buckets too.
    """

    dead = {str(x) for x in excluded}
    raw: dict[str, float] = {}
    for bucket, m in m_market.items():
        key = str(bucket)
        if key in dead:
            raw[key] = 0.0
            continue
        p = float(p_weather.get(key, 0.0) or 0.0)
        raw[key] = expit(posterior_score_v1(p, float(m), params))
    total = sum(raw.values())
    if not (total > 0.0 and math.isfinite(total)):
        return None
    return {bucket: value / total for bucket, value in raw.items()}


def posterior_draw_vectors_v1(
    p_weather: Mapping[str, float],
    m_market: Mapping[str, float],
    draws: Sequence[Sequence[float]],
    *,
    excluded: Iterable[str] = (),
) -> list[dict[str, float]]:
    """One whole vector per parameter draw -- the uncertainty set an action
    is valued on (a quantile across whole vectors, never across buckets)."""

    dead = tuple(excluded)
    out: list[dict[str, float]] = []
    for draw in draws:
        try:
            vector = posterior_vector_v1(p_weather, m_market, draw, excluded=dead)
        except (TypeError, ValueError):
            continue
        if vector is not None:
            out.append(vector)
    return out


def window_mass(vector: Mapping[str, float], window: Iterable[str]) -> float:
    return float(sum(float(vector.get(str(b), 0.0) or 0.0) for b in window))


__all__ = [
    "EPS", "LCB_QUANTILE", "PARAM_NAMES", "POSTERIOR_MODE", "POSTERIOR_RULE",
    "POSTERIOR_TABLE_KEY", "expit", "logit", "posterior_draw_vectors_v1",
    "posterior_score_v1", "posterior_vector_v1", "window_mass",
]
