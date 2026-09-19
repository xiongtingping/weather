"""P0-M as one Bayesian update: P0 times the TAF's likelihood, and nothing else.

    P0-M(k)  proportional to  P0(k) * N(x ; a_s + b_s * y_k, tau_s) * mask(k)

    x   = TX - c0   (the TAF's forecast maximum against P0's own centre)
    y_k = k - c0    (bucket k's centre against the same)

User ruling 2026-09-19 ("taf就不要再修中心，宽度了，已经变成似然了"), charter 150.
TAF is evidence about the day's maximum, so it enters as a likelihood of the
bucket -- how probable is this TX if the day settles in bucket k -- and the
root's centre and width are never touched.  Charter 133's TAF path through the
centre/width scalars (and charter 57's TAF cells) is retired with this.

The conditional is a straight line with Gaussian scatter, fitted on settled
station-days: regress x on y.  ``b`` is how much of the day's departure from
the model the TAF sees (0 = a TAF that only repeats the model, 1 = a TAF that
tracks the day one for one) and ``tau`` is its scatter.  Measured 2026-09-19:
TAF and P0 errors correlate 0.64, so most of what a TAF says the root already
said, and the likelihood is correspondingly flat -- which is exactly how a
likelihood should price overlapping evidence, without a hand-set weight.

Per station the three numbers are shrunk toward the pool with ``K = 15``
station-days of prior weight.  Whether the likelihood serves at all is decided
by the artifact's walk-forward gate (the by-day clustered interval of the log
score gain over the root must sit above zero); until then it is the identity
and says so, which is what P0-M served before -- the stage still marks the
TAF's arrival as a decision moment.

The artifact arrives from the caller for the reason every fitted table does:
a replay has to rebuild the exact envelope that was served.

Charter 158 (user ruling 2026-09-19, 「taf是人工在模型基础上加了一些看不见的
信息」→「上线」): the one line stays, but how hard it pulls depends on whether
the forecaster's own weather explains why the TAF disagrees with P0.  The
disagreement is TX against P0's served mean; a warm TAF under clear, dry skies
or a cold TAF with rain or overcast is REASONED, a cold TAF with neither is
UNREASONED_COLD, any other disagreement of 1C or more is OTHER, and the rest
(or a TAF without its weather) is SMALL.  Each group's exponent is multiplied
by its own lambda, ``L ** lambda``: 1 is the line as fitted, 0 is silence.
The lambdas are fitted daily and serve only while the refit's walk-forward
says tempering beats lambda = 1 on days it did not see; otherwise every group
reads 1 and P0-M is exactly charter 150's.  Measured on settled days: truth
realised 55-57% of a reasoned disagreement against 17% of an unreasoned cold
one.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, content_sha256

SCHEMA = "bayes_basket_target_taf_likelihood_v1"
RULE = "p0m_taf_conditional_gaussian_v1"
RECEIPT_SCHEMA = "p0m_taf_likelihood_receipt_v1"
LINEAGE_KEY = "p0m_taf_likelihood_json"
LIKELIHOOD_MODE = "P0M_PRIOR_X_TAF_LIKELIHOOD_V1"
SHRINK_K = 15.0
MIN_TAU_C = 0.25
_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = (
    _ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "taf_likelihood_artifact.json"
)
GATE_SERVE = "SERVE"
GATE_IDENTITY = "IDENTITY"
# Charter 151 (user ruling 2026-09-19, 「taf要触发概率重算」「你先打开」): the
# TAF likelihood serves without waiting for the walk-forward interval to clear
# zero.  Applied where the fitted file is READ, not where it is compiled: the
# daily refit keeps writing the gate it measured, the loader stamps the ruling
# over it (keeping the measurement beside it) and re-hashes, so the served
# arithmetic -- and the P0-M fingerprint -- is exactly the path an automatic
# gate flip takes, and a receipt's artifact_sha256 names what actually served.
# "" restores the measured gate.
GATE_RULING = GATE_SERVE
GATE_RULING_ID = "charter_151_user_ruling_2026_09_19"

# Charter 158: per-group tempering of the one line.  The group reads the
# TAF's own weather over the day's heating window (the capture's
# ``cloud_cover_fraction`` / ``precipitation_probability``).  Charter 151's
# ruling opens the line's gate, never this one: tempering serves only on its
# own walk-forward.
TEMPERING_KEY = "reason_tempering"
TEMPERING_RULE = "taf_reason_tempering_v1"
GROUP_REASONED = "REASONED"
GROUP_UNREASONED_COLD = "UNREASONED_COLD"
GROUP_OTHER = "OTHER"
GROUP_SMALL = "SMALL"
TEMPERING_GROUPS = (
    GROUP_REASONED, GROUP_UNREASONED_COLD, GROUP_OTHER, GROUP_SMALL
)
DISAGREEMENT_MIN_C = 1.0
CLEAR_CLOUD_MAX = 0.4
WET_PRECIP_MIN = 0.3
OVERCAST_CLOUD_ABOVE = 0.6
MAX_TEMPERING_LAMBDA = 3.0


def station_parameters(
    artifact: Mapping[str, Any], station_id: str
) -> tuple[float, float, float, str]:
    """(a, b, tau, source) for a station: its shrunk row, or the pool."""

    row = (artifact.get("stations") or {}).get(str(station_id))
    source = "STATION"
    if not isinstance(row, Mapping):
        row = artifact["pool"]
        source = "POOL"
    a, b, tau = float(row["a"]), float(row["b"]), float(row["tau"])
    if not all(math.isfinite(value) for value in (a, b, tau)) or tau <= 0.0:
        raise ContractError("taf_likelihood_parameters_invalid")
    return a, b, max(tau, MIN_TAU_C), source


def disagreement_group(
    x_served_c: float | None,
    cloud_cover: float | None,
    precip_prob: float | None,
) -> str:
    """Which way the TAF disagrees with P0's served mean, and whether its weather says why."""

    if x_served_c is None or cloud_cover is None or precip_prob is None:
        return GROUP_SMALL
    x = float(x_served_c)
    if not math.isfinite(x) or abs(x) < DISAGREEMENT_MIN_C:
        return GROUP_SMALL
    cloud, precip = float(cloud_cover), float(precip_prob)
    if x > 0.0 and cloud <= CLEAR_CLOUD_MAX and precip < WET_PRECIP_MIN:
        return GROUP_REASONED
    if x < 0.0 and (precip >= WET_PRECIP_MIN or cloud > OVERCAST_CLOUD_ABOVE):
        return GROUP_REASONED
    if x < 0.0:
        return GROUP_UNREASONED_COLD
    return GROUP_OTHER


def tempering_lambda(artifact: Mapping[str, Any] | None, group: str) -> float:
    """The group's lambda while tempering serves; 1 (the line as fitted) otherwise."""

    block = (artifact or {}).get(TEMPERING_KEY)
    if not isinstance(block, Mapping) or block.get("state") != GATE_SERVE:
        return 1.0
    table = block.get("lambda_by_group")
    if not isinstance(table, Mapping):
        raise ContractError("taf_tempering_table_invalid")
    raw = table.get(str(group), 1.0)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError("taf_tempering_lambda_invalid") from exc
    if not math.isfinite(value) or not 0.0 <= value <= MAX_TEMPERING_LAMBDA:
        raise ContractError("taf_tempering_lambda_invalid")
    return value


def exponent_by_bucket(
    artifact: Mapping[str, Any],
    *,
    station_id: str,
    tx_c: float,
    root_center_c: float,
    bucket_centers_c: Mapping[str, float],
) -> tuple[dict[str, float], tuple[float, float, float, str]]:
    """The untempered exponent (x - a - b*y)^2 / (2 tau^2) per bucket.

    The fitter reads this same function to score lambdas, so what it picks is
    what serves.
    """

    a, b, tau, source = station_parameters(artifact, station_id)
    x = float(tx_c) - float(root_center_c)
    return {
        str(bucket): (
            (x - a - b * (float(center) - float(root_center_c))) ** 2
            / (2.0 * tau * tau)
        )
        for bucket, center in bucket_centers_c.items()
    }, (a, b, tau, source)


def compile_taf_likelihood(
    artifact: Mapping[str, Any] | None,
    *,
    station_id: str,
    tx_c: float | None,
    root_center_c: float | None,
    bucket_centers_c: Mapping[str, float] | None,
    buckets: Any,
    root_mean_c: float | None = None,
    taf_cloud_cover: float | None = None,
    taf_precip_prob: float | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """The likelihood per bucket and its receipt; identity whenever it cannot speak.

    ``root_mean_c`` is P0's served mean (the disagreement charter 158 groups
    on); with the TAF's cloud and precipitation it picks the group's lambda.
    """

    identity = {str(bucket): 1.0 for bucket in buckets}

    def _silent(state: str) -> tuple[dict[str, float], dict[str, Any]]:
        return identity, {
            "schema": RECEIPT_SCHEMA,
            "rule": RULE,
            "state": state,
            "artifact_sha256": str((artifact or {}).get("content_sha256") or ""),
            "tx_c": None if tx_c is None else float(tx_c),
            "root_center_c": (
                None if root_center_c is None else float(root_center_c)
            ),
        }

    if artifact is None or artifact.get("schema") != SCHEMA:
        return _silent("IDENTITY_NO_ARTIFACT")
    gate = str((artifact.get("gate") or {}).get("state") or "")
    if gate != GATE_SERVE:
        return _silent("IDENTITY_GATE_CLOSED")
    if tx_c is None:
        return _silent("IDENTITY_NO_TX")
    if root_center_c is None or not bucket_centers_c:
        return _silent("IDENTITY_NO_ROOT_GEOMETRY")
    if set(bucket_centers_c) != set(identity):
        return _silent("IDENTITY_NO_ROOT_GEOMETRY")
    exponents, (a, b, tau, source) = exponent_by_bucket(
        artifact, station_id=station_id, tx_c=float(tx_c),
        root_center_c=float(root_center_c), bucket_centers_c=bucket_centers_c,
    )
    x = float(tx_c) - float(root_center_c)
    x_served = (
        None if root_mean_c is None else float(tx_c) - float(root_mean_c)
    )
    group = disagreement_group(x_served, taf_cloud_cover, taf_precip_prob)
    lam = tempering_lambda(artifact, group)
    likelihoods = {
        bucket: math.exp(-lam * value) for bucket, value in exponents.items()
    }
    if max(likelihoods.values()) <= 1e-300:
        return _silent("IDENTITY_UNDERFLOW")
    _, receipt = _silent("APPLIED")
    tempering_block = (artifact or {}).get(TEMPERING_KEY)
    receipt.update({
        "parameters_source": source,
        "a": a,
        "b": b,
        "tau": tau,
        "x_c": x,
        "tempering": {
            "rule": TEMPERING_RULE,
            "state": (
                str(tempering_block.get("state") or "")
                if isinstance(tempering_block, Mapping) else "ABSENT"
            ),
            "group": group,
            "lambda": lam,
            "x_served_c": x_served,
            "cloud_cover": (
                None if taf_cloud_cover is None else float(taf_cloud_cover)
            ),
            "precip_prob": (
                None if taf_precip_prob is None else float(taf_precip_prob)
            ),
        },
        "likelihood_by_bucket": {
            bucket: round(value, 6) for bucket, value in sorted(likelihoods.items())
        },
    })
    return likelihoods, receipt


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
        body = apply_gate_ruling(body)
        _CACHE.clear()
        _CACHE[key] = body
    return _CACHE[key]


def apply_gate_ruling(
    body: Mapping[str, Any], ruling: str | None = None
) -> dict[str, Any]:
    """The artifact as it serves under charter 151's ruling.

    The measured gate stays in the file and beside the ruling
    (``measured_state``); the content hash is recomputed over what is served,
    so an envelope's receipt never names the measured file for an artifact
    that served differently.
    """

    ruling = GATE_RULING if ruling is None else ruling
    gate = dict(body.get("gate") or {})
    if not ruling or gate.get("state") == ruling:
        return dict(body)
    served = {key: value for key, value in body.items() if key != "content_sha256"}
    served["gate"] = {
        **gate,
        "measured_state": gate.get("state"),
        "state": ruling,
        "ruling": GATE_RULING_ID,
    }
    served["measured_content_sha256"] = body.get("content_sha256")
    served["content_sha256"] = content_sha256(served)
    return served


__all__ = [
    "ARTIFACT_PATH",
    "GROUP_OTHER",
    "GROUP_REASONED",
    "GROUP_SMALL",
    "GROUP_UNREASONED_COLD",
    "TEMPERING_GROUPS",
    "TEMPERING_KEY",
    "TEMPERING_RULE",
    "disagreement_group",
    "exponent_by_bucket",
    "tempering_lambda",
    "GATE_IDENTITY",
    "GATE_SERVE",
    "LIKELIHOOD_MODE",
    "LINEAGE_KEY",
    "RECEIPT_SCHEMA",
    "RULE",
    "SCHEMA",
    "SHRINK_K",
    "GATE_RULING",
    "apply_gate_ruling",
    "compile_taf_likelihood",
    "load_artifact",
    "station_parameters",
]
