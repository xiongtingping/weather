"""P1 as one Bayesian update: the P0/P0-M root times one joint observation likelihood.

    P1(k)  proportional to  root(k) * 1[k >= running-high bucket] * g(d_k | s_t) ** alpha(hours)

User ruling 2026-09-19 ("p1的信息更新是最快的，应该设计为基于p0/p0m先验的似然更新
概率分布…用联合似然，不要连续乘所有 likelihood" and "按照方案全量落地"), charter 150.

What this replaces is a stack of layers each answering part of one question --
a phase split, a remaining-warming ratio, a clock ceiling, a regional path
prior, and a centre/width and band correction after them -- where every layer
re-normalised the one below it and the root was re-scaled five times over.
Here there is one question per decision: given everything the reports say at
this instant, how likely is each bucket's remaining rise?  It is asked once,
jointly, and multiplied into the root once.

``g`` is the ordered hazard chain the four-layer module already fitted, read as
a likelihood over the bucket's increment above the running high:

* ``d = 0`` (the running high's own bucket, and anything within half a degree
  of it): ``1 - rise`` -- the day does not cross into another bucket.
* ``d >= 1``: ``rise * P(delta = d | delta >= 1)`` off the continuation rungs.

``s_t`` is observation-side only: the path state (drawdown, momentum, cloud,
rain, dewpoint depression, wind, warming so far), the clock, the root curve's
*shape* (never its level) and two *innovation* terms -- how far today's
reports have run from the root's own curve (``root_bias_recent_c``) and how
much warming the day's regional run still claims above the reported high
(``regional_headroom_c``) together with how far the reports have run from that
run (``regional_live_gap_c``).  The regional curves enter here as covariates
and nowhere else: they are deterministic single curves, measured to pay as a
feature (+0.026 nat) and not as a blended distribution.

``g`` is not divided by its training marginal.  The measured alternative --
dividing out the clock base rate to remove the double count in theory -- lost
0.45 nat, because the truncated root carries no time-of-day base rate of its
own to double count against; the rate lives only in ``g``.  Instead the
likelihood is tempered by ``alpha`` per remaining-hours bin, chosen
walk-forward on a held-out day split (2026-09-19: >=10h 0.5, 6-10h 1.125,
<6h 1.5), which is how the overlap between root and evidence is priced.

Nothing here touches the root's centre or width, and P1 carries no band table:
the root is P0/P0-M's calibrated distribution and this stage only states what
the reports add to it.  The observation hard bound is the one mask, and the
zero floor after it is the materializer's, unchanged.

The artifact arrives from the caller rather than being loaded inside the
arithmetic, for the reason every fitted table does: a replay has to rebuild the
exact envelope that was served.  ``load_artifact`` is the caller's loader,
cached on the file's modification time so a daily refit is picked up without a
restart.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractError
from .metar_four_layer import _continuation_distribution, phase_rise_probability

SCHEMA = "bayes_basket_target_p1_observation_likelihood_v1"
RULE = "p1_prior_x_joint_observation_likelihood_v1"
RECEIPT_SCHEMA = "p1_observation_likelihood_receipt_v1"
LINEAGE_KEY = "p1_observation_likelihood_json"
LIKELIHOOD_MODE = "P1_PRIOR_X_JOINT_OBSERVATION_LIKELIHOOD_V1"
_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = (
    _ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "p1_observation_likelihood_artifact.json"
)
SAMPLES_PATH = (
    _ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "p1_observation_samples.db"
)

# Rung one: will the day cross into another bucket from here.  The clock
# enters as binned intercepts; these are the state and the two innovations.
PHASE_FEATURES = (
    "drawdown_from_high_c",
    "momentum_c_per_hour",
    "cloud_cover_fraction",
    "precipitation_present",
    "root_bias_recent_c",
    "regional_headroom_c",
)
# Rungs two and up: given it will, how far.
FULL_FEATURES = PHASE_FEATURES + (
    "dewpoint_depression_c",
    "wind_speed_mps",
    "warming_so_far_c",
    "curve_shape_realized",
    "remaining_hours",
    "regional_live_gap_c",
)
# The path-state features: the ones a report prefix too thin to compile a
# complete state leaves unknown.  They fall back to the fleet mean, which is
# "nothing known here", and the clock, the shape and the innovations still
# speak -- so an incomplete state degrades to a clock-led likelihood rather
# than to silence, and no ceiling is needed behind it.
STATE_FEATURES = (
    "drawdown_from_high_c",
    "momentum_c_per_hour",
    "cloud_cover_fraction",
    "precipitation_present",
    "dewpoint_depression_c",
    "wind_speed_mps",
)
# Remaining-hours edges for the tempering, descending: [>=10h, 6-10h, <6h].
ALPHA_EDGES_HOURS = (10.0, 6.0)
# A bucket whose centre sits within this of the running high is the running
# high's own outcome: the day does not settle a whole rung above it.
_NO_RISE_WITHIN_C = 0.5
_EPS = 1e-12


def _number(name: str, value: Any, *, nullable: bool) -> float | None:
    if value is None:
        if nullable:
            return None
        raise ContractError(f"p1_observation_likelihood_{name}_missing")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"p1_observation_likelihood_{name}_invalid") from exc
    if not math.isfinite(number):
        raise ContractError(f"p1_observation_likelihood_{name}_invalid")
    return number


def feature_vector(
    *,
    remaining_hours: float,
    drawdown_from_high_c: float | None,
    momentum_c_per_hour: float | None,
    cloud_cover_fraction: float | None,
    precipitation_present: bool | int | None,
    dewpoint_depression_c: float | None,
    wind_speed_mps: float | None,
    warming_so_far_c: float | None,
    curve_shape_realized: float | None,
    root_bias_recent_c: float | None,
    regional_headroom_c: float | None,
    regional_live_gap_c: float | None,
) -> dict[str, float | None]:
    """One feature vector for fit and serve, named as the sample table names it.

    Every argument is keyword-required, the nullable ones included: a caller
    that has not thought about a feature must say ``None`` rather than inherit
    a default.  ``None`` means "nothing known here" and is read at the fleet
    mean; only the clock is required.
    """

    return {
        "remaining_hours": max(
            0.0, _number("remaining_hours", remaining_hours, nullable=False)
        ),
        "drawdown_from_high_c": _number(
            "drawdown_from_high_c", drawdown_from_high_c, nullable=True
        ),
        "momentum_c_per_hour": _number(
            "momentum_c_per_hour", momentum_c_per_hour, nullable=True
        ),
        "cloud_cover_fraction": _number(
            "cloud_cover_fraction", cloud_cover_fraction, nullable=True
        ),
        "precipitation_present": _number(
            "precipitation_present", precipitation_present, nullable=True
        ),
        "dewpoint_depression_c": _number(
            "dewpoint_depression_c", dewpoint_depression_c, nullable=True
        ),
        "wind_speed_mps": _number(
            "wind_speed_mps", wind_speed_mps, nullable=True
        ),
        "warming_so_far_c": _number(
            "warming_so_far_c", warming_so_far_c, nullable=True
        ),
        "curve_shape_realized": _number(
            "curve_shape_realized", curve_shape_realized, nullable=True
        ),
        "root_bias_recent_c": _number(
            "root_bias_recent_c", root_bias_recent_c, nullable=True
        ),
        "regional_headroom_c": _number(
            "regional_headroom_c", regional_headroom_c, nullable=True
        ),
        "regional_live_gap_c": _number(
            "regional_live_gap_c", regional_live_gap_c, nullable=True
        ),
    }


def alpha_bin(remaining_hours: float) -> int:
    """0 for >=10h, 1 for 6-10h, 2 for <6h."""

    hours = float(remaining_hours)
    for index, edge in enumerate(ALPHA_EDGES_HOURS):
        if hours >= edge:
            return index
    return len(ALPHA_EDGES_HOURS)


def alpha_for(remaining_hours: float, artifact: Mapping[str, Any]) -> float:
    values = [float(value) for value in artifact["alpha_by_bin"]]
    if len(values) != len(ALPHA_EDGES_HOURS) + 1:
        raise ContractError("p1_observation_likelihood_alpha_bins_invalid")
    alpha = values[alpha_bin(remaining_hours)]
    if not math.isfinite(alpha) or alpha < 0.0:
        raise ContractError("p1_observation_likelihood_alpha_invalid")
    return alpha


def evidence_by_bucket(
    features: Mapping[str, float | None],
    artifact: Mapping[str, Any],
    *,
    bucket_ordinals: Mapping[str, int],
    bucket_centers_c: Mapping[str, float],
    running_high_c: float,
    running_high_bucket: str,
) -> tuple[dict[str, float], float]:
    """``g(d_k | s_t)`` per bucket, untempered, and the rise probability."""

    rise = phase_rise_probability(features, artifact)
    continuation = _continuation_distribution(
        features, artifact["continuation_full"], artifact["feature_fallbacks"]
    )
    rungs = sorted(continuation)
    floor = int(bucket_ordinals[str(running_high_bucket)])
    high = float(running_high_c)
    evidence: dict[str, float] = {}
    for bucket, ordinal in bucket_ordinals.items():
        increment = float(bucket_centers_c[bucket]) - high
        if int(ordinal) <= floor or increment <= _NO_RISE_WITHIN_C:
            evidence[str(bucket)] = 1.0 - rise
        else:
            rung = min(rungs, key=lambda value: abs(value - increment))
            evidence[str(bucket)] = rise * float(continuation.get(rung, 0.0))
    return evidence, rise


def compile_observation_likelihood(
    features: Mapping[str, float | None],
    artifact: Mapping[str, Any],
    *,
    bucket_ordinals: Mapping[str, int],
    bucket_centers_c: Mapping[str, float],
    running_high_c: float,
    running_high_bucket: str,
    hard_excluded_buckets: Sequence[str] = (),
) -> tuple[dict[str, float], dict[str, Any]]:
    """The tempered joint likelihood per bucket and its receipt.

    Raises ContractError when the artifact is malformed or the likelihood
    leaves no surviving bucket any mass; the caller decides what a refusal
    serves.  Excluded buckets keep their computed value -- the hard bound is
    the materializer's mask, applied once, not something this function
    decides a second time.
    """

    if str(artifact.get("schema") or "") != SCHEMA:
        raise ContractError("p1_observation_likelihood_artifact_schema_invalid")
    missing = [
        bucket for bucket in bucket_ordinals if bucket not in bucket_centers_c
    ]
    if missing:
        raise ContractError("p1_observation_likelihood_bucket_centre_missing")
    if str(running_high_bucket) not in bucket_ordinals:
        raise ContractError("p1_observation_likelihood_running_high_bucket_unknown")
    evidence, rise = evidence_by_bucket(
        features,
        artifact,
        bucket_ordinals=bucket_ordinals,
        bucket_centers_c=bucket_centers_c,
        running_high_c=running_high_c,
        running_high_bucket=running_high_bucket,
    )
    hours = float(features["remaining_hours"])
    alpha = alpha_for(hours, artifact)
    likelihoods = {
        bucket: max(0.0, value) ** alpha for bucket, value in evidence.items()
    }
    excluded = {str(bucket) for bucket in hard_excluded_buckets}
    if not any(
        value > _EPS
        for bucket, value in likelihoods.items()
        if bucket not in excluded
    ):
        raise ContractError("p1_observation_likelihood_zero_on_support")
    fallbacks = artifact.get("feature_fallbacks") or {}
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "rule": RULE,
        "state": "APPLIED",
        "artifact_sha256": str(artifact.get("content_sha256") or ""),
        "trained_through": str(artifact.get("trained_through") or ""),
        "alpha": alpha,
        "alpha_bin": alpha_bin(hours),
        "rise_probability": rise,
        "running_high_c": float(running_high_c),
        "running_high_bucket": str(running_high_bucket),
        "features": {
            name: (None if value is None else round(float(value), 4))
            for name, value in sorted(features.items())
        },
        # Which features were read at the fleet mean.  Without this a served
        # step cannot be told from one where the state was complete, short of
        # recomputing the feature -- an audit reimplementing the rule.
        "fleet_mean_features": sorted(
            name for name in FULL_FEATURES
            if features.get(name) is None and name in fallbacks
        ),
        "evidence_by_bucket": {
            bucket: round(value, 6) for bucket, value in sorted(evidence.items())
        },
    }
    return likelihoods, receipt


def silent_receipt(state: str, *, reason: str = "", artifact_sha256: str = "") -> dict[str, Any]:
    """The receipt of a step the likelihood did not speak on."""

    return {
        "schema": RECEIPT_SCHEMA,
        "rule": RULE,
        "state": str(state),
        "artifact_sha256": str(artifact_sha256),
        **({"reason": str(reason)[:160]} if reason else {}),
    }


_CACHE: dict[str, Any] = {}


def load_artifact(path: Path | None = None) -> dict[str, Any] | None:
    """The fitted file, cached on its modification time; None if absent or bad."""

    target = Path(path or ARTIFACT_PATH)
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return None
    key = f"{target}::{stamp}"
    if key not in _CACHE:
        try:
            body = json.loads(target.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(body, dict) or body.get("schema") != SCHEMA:
            return None
        _CACHE.clear()
        _CACHE[key] = body
    return _CACHE[key]


__all__ = [
    "ALPHA_EDGES_HOURS",
    "ARTIFACT_PATH",
    "FULL_FEATURES",
    "LIKELIHOOD_MODE",
    "LINEAGE_KEY",
    "PHASE_FEATURES",
    "RECEIPT_SCHEMA",
    "RULE",
    "SAMPLES_PATH",
    "SCHEMA",
    "STATE_FEATURES",
    "alpha_bin",
    "alpha_for",
    "compile_observation_likelihood",
    "evidence_by_bucket",
    "feature_vector",
    "load_artifact",
    "silent_receipt",
]
