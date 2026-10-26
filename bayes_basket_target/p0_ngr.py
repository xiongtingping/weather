"""Charter 281: P0 as one distributional regression (NGR) over the calibrated systems
and the deterministic forecasts.

User ruling 2026-10-25 「按 N2 形态重建 P0：保留单家证书，用一个分布回归层取代其后全部层和
F 平移，每日前推拟合并以 240 门为服务前提，升版重启」 on the registered experiment
``docs/research/ngr_rebuild_20261025`` (N2 against the P0 with F: LogScore -0.0127
[-0.0241, -0.0016] over 23 event days).

Inputs at a decision -- each a fact the compile already holds:

    m_f, s_f   mean and standard deviation of each seated family's calibrated
               vector (the single-system certificates, charters 216/247), the
               vector read as uniform within each bucket, an open bucket cut at
               one bucket width; several systems of one family are pooled equally
    F, Z       the ten deterministic day maxima knowable at the decision, each
               corrected by its own fleet bias, their median and log(SD + 0.2);
               at least MIN_DET of them
    L          (hours to settlement - 36) / 24
    r          the radiation standard score (charter 222's receipt; 0 when none)

and the one model, its parameters fitted daily (``scripts/fit_p0_ngr.py``):

    pi_f   = exp(beta_f), renormalised over the families seated
    mu_E   = sum pi_f m_f / sum pi_f
    S_E^2  = sum pi_f s_f^2 / sum pi_f + sum pi_f (m_f - mu_E)^2 / sum pi_f
    mu     = mu_E + a + (w0 + w1 L)(F - mu_E) + r_w max(r, 0) + r_c min(r, 0)
    log sd = c0 + c1 L + c2 log S_E + c3 Z
    P(k)   = Phi((hi_k - mu)/sd) - Phi((lo_k - mu)/sd), charter 224's floor last.

Serving needs the artifact's own walk-forward to pass charter 240 against the
vector the systems' older layers serve without it (the fallback, kept in the
receipt as ``fallback_probabilities``); a decision whose inputs are short
(no seated family, fewer than MIN_DET deterministic forecasts, no hours to
settlement) or whose artifact does not pass is served the fallback and the
receipt names why.  ``predict_batch`` is the one implementation: the fit
scores it and serving calls it with a batch of one.
"""

from __future__ import annotations

import json
import math
import os
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.special import ndtr

from .regional_point_lane import SYSTEM_IDS

RULE_VERSION = "p0_ngr_v1"
LINEAGE_KEY = "p0_ngr_json"
FIT_SCHEMA = "bayes_basket_target_p0_ngr_fit_v1"
ROOT = Path(__file__).resolve().parents[1]
FIT_ARTIFACT_PATH = Path(
    os.environ.get("PM_P0_NGR_FIT")
    or ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "p0_ngr_fit.json"
)
#: ``serve`` serves an admitted artifact; ``shadow`` records the NGR vector and serves the fallback.
MODE = os.environ.get("PM_P0_NGR_MODE", "serve").strip().lower()
FAMILIES: tuple[str, ...] = ("ECMWF", "ECMWF_AIFS", "NOAA_GEFS", "NOAA_AIGEFS", "DWD_ICON_EPS", "ECCC_GEM")
#: The deterministic roster (charter 279 补记二's ten).
DET_PRODUCTS: tuple[str, ...] = (
    "AIFS", "IFS9KM", "UKMO10", "CMA", "IFS_HRES", "GFS", "AIGFS", "GDPS", "ICON_GLOBAL", "UKMO_GLOBAL",
)
DET_SYSTEM_IDS: frozenset[str] = frozenset(SYSTEM_IDS[p] for p in DET_PRODUCTS)
_PRODUCT_OF_SYSTEM = {SYSTEM_IDS[p]: p for p in DET_PRODUCTS}
MIN_DET = 3
FLOOR = 1e-3
N_THETA = len(FAMILIES) + 9
#: Theta layout: beta per family, then these.
THETA_NAMES: tuple[str, ...] = tuple(f"beta_{f}" for f in FAMILIES) + (
    "a", "w0", "w1", "c0", "c1", "c2", "c3", "r_warm", "r_cold",
)
#: Charter 240 on the walk-forward: scored decisions from here (the current
#: ensemble chain, charter 255), at least this many event days.
SCORE_FROM_DECISION_UTC = "2026-10-01T00:00:00Z"
VERDICT_MIN_EVENT_DAYS = 20

_FIT_CACHE: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Geometry and inputs.


def geometry(ordered: Sequence[str], intervals: Mapping[str, Sequence[float | None]]) -> dict[str, list[float]]:
    """Source cells (open bucket cut at the median bucket width) and target bounds."""

    widths = [float(hi) - float(lo) for lo, hi in (intervals[k] for k in ordered) if lo is not None and hi is not None]
    w = statistics.median(widths) if widths else 1.0
    slo, shi, tlo, thi = [], [], [], []
    for key in ordered:
        lo, hi = intervals[key]
        slo.append(float(lo) if lo is not None else float(hi) - w)
        shi.append(float(hi) if hi is not None else float(lo) + w)
        tlo.append(float(lo) if lo is not None else -math.inf)
        thi.append(float(hi) if hi is not None else math.inf)
    return {"slo": slo, "shi": shi, "tlo": tlo, "thi": thi}


def vector_moments(vector: Sequence[float], geom: Mapping[str, Sequence[float]]) -> tuple[float, float]:
    """Mean and standard deviation of a bucket vector read as uniform within each cell."""

    v = np.asarray(vector, float)
    v = v / v.sum()
    slo, shi = np.asarray(geom["slo"]), np.asarray(geom["shi"])
    mid, width = (slo + shi) / 2.0, shi - slo
    mu = float((v * mid).sum())
    var = float((v * (mid - mu) ** 2).sum() + (v * width ** 2).sum() / 12.0)
    return mu, math.sqrt(max(var, 1e-4))


def family_moments(
    system_probabilities: Mapping[str, Mapping[str, float]],
    family_by_system: Mapping[str, str],
    ordered: Sequence[str],
    geom: Mapping[str, Sequence[float]],
) -> dict[str, list[float]]:
    """Per seated family [m_f, s_f]; several systems of a family are pooled equally."""

    pooled: dict[str, list[list[float]]] = {}
    for system, vec in system_probabilities.items():
        family = str(family_by_system.get(system, system))
        if family not in FAMILIES or not isinstance(vec, Mapping) or not set(ordered) <= set(vec):
            continue
        pooled.setdefault(family, []).append([float(vec[k]) for k in ordered])
    out = {}
    for family, vectors in pooled.items():
        mean_vec = np.mean(np.asarray(vectors), axis=0)
        out[family] = list(vector_moments(mean_vec, geom))
    return out


def read_deterministic(
    conn: Any, *, station_id: str, event_date: str, as_of_utc: str, settlement_timezone: str,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ten deterministic day maxima knowable at ``as_of_utc`` (charter 267's reader).  Never raises."""

    from .deterministic_forecast_family import feature_at  # noqa: PLC0415

    try:
        features = feature_at(
            conn, station_id=station_id, event_date=event_date, as_of_utc=as_of_utc,
            settlement_timezone=settlement_timezone, systems=sorted(DET_SYSTEM_IDS), rows=rows,
        )
    except Exception as exc:  # noqa: BLE001
        return {"state": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:160]}", "systems": {}}
    systems = {}
    for system_id, feature in sorted(features.items()):
        product = _PRODUCT_OF_SYSTEM.get(str(system_id))
        if product is not None:
            systems[product] = {"day_max_c": round(float(feature["day_max_c"]), 3),
                                "run_init_utc": str(feature.get("run_init_utc") or "")}
    return {"state": "ok", "systems": systems}


def det_features(values: Mapping[str, float], bias: Mapping[str, float]) -> tuple[float, float] | None:
    """F (median of the bias-corrected values) and Z = log(SD + 0.2); None under MIN_DET."""

    corrected = [float(v) + float(bias.get(k, 0.0)) for k, v in values.items() if k in DET_PRODUCTS]
    if len(corrected) < MIN_DET:
        return None
    return statistics.median(corrected), math.log(float(np.std(corrected)) + 0.2)


# ---------------------------------------------------------------------------
# The model: one implementation, batched.


def centre_terms(
    theta: Sequence[float], *, m: np.ndarray, has: np.ndarray, F: np.ndarray, L: np.ndarray, rad: np.ndarray,
) -> dict[str, np.ndarray]:
    """The centre's parts: mu = mu_e + a + f_pull + rad_warm + rad_cold.

    ``mu_e`` is the seated families' weighted centre, ``f_weight`` the share of
    the gap F - mu_e the regression takes at this lead, ``f_pull`` that share of
    the gap -- how far the deterministic forecasts move P0.  ``predict_batch``
    sums them in this order, so the receipt's parts add up to the served mu."""

    theta = np.asarray(theta, float)
    nf = len(FAMILIES)
    a, w0, w1, _c0, _c1, _c2, _c3, rw, rc = theta[nf:nf + 9]
    pi = np.where(has, np.exp(np.clip(theta[:nf], -8.0, 8.0))[None, :], 0.0)
    tot = pi.sum(1)
    mu_e = (pi * m).sum(1) / tot
    f_weight = w0 + w1 * L
    return {"pi": pi, "tot": tot, "mu_e": mu_e, "a": a + 0.0 * mu_e, "f_weight": f_weight,
            "f_pull": f_weight * (F - mu_e), "rad_warm": rw * np.maximum(rad, 0.0),
            "rad_cold": rc * np.minimum(rad, 0.0)}


def predict_batch(
    theta: Sequence[float], *, m: np.ndarray, s: np.ndarray, has: np.ndarray, F: np.ndarray, Z: np.ndarray,
    L: np.ndarray, rad: np.ndarray, tlo: np.ndarray, thi: np.ndarray, mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bucket probabilities (floored), mu and sd for n decisions (arrays n x families / n x buckets)."""

    theta = np.asarray(theta, float)
    nf = len(FAMILIES)
    _a, _w0, _w1, c0, c1, c2, c3, _rw, _rc = theta[nf:nf + 9]
    t = centre_terms(theta, m=m, has=has, F=F, L=L, rad=rad)
    pi, tot, mu_e = t["pi"], t["tot"], t["mu_e"]
    within = (pi * s ** 2).sum(1) / tot
    between = (pi * (m - mu_e[:, None]) ** 2).sum(1) / tot
    s_e = np.sqrt(within + between)
    mu = mu_e + t["a"] + t["f_pull"] + t["rad_warm"] + t["rad_cold"]
    sd = np.exp(np.clip(c0 + c1 * L + c2 * np.log(s_e) + c3 * Z, -3.0, 2.5))
    lo = np.where(np.isfinite(tlo), (tlo - mu[:, None]) / sd[:, None], -np.inf)
    hi = np.where(np.isfinite(thi), (thi - mu[:, None]) / sd[:, None], np.inf)
    q = np.where(mask, np.clip(ndtr(hi) - ndtr(lo), 0.0, None), 0.0)
    q = np.where(mask, np.maximum(q, FLOOR), 0.0)
    return q / q.sum(1, keepdims=True), mu, sd


def initial_theta() -> np.ndarray:
    theta = np.zeros(N_THETA)
    theta[len(FAMILIES) + 5] = 1.0  # c2: sd follows the ensemble spread
    return theta


# ---------------------------------------------------------------------------
# The artifact and serving.


def load_fit_artifact(path: Path | None = None) -> dict[str, Any] | None:
    """The daily fit, cached on its modification time."""

    target = Path(path or FIT_ARTIFACT_PATH)
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return None
    cached = _FIT_CACHE.get(str(target))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != FIT_SCHEMA:
        return None
    _FIT_CACHE.clear()
    _FIT_CACHE[str(target)] = (stamp, payload)
    return payload


def serve(
    *,
    fallback: Mapping[str, float],
    ordered: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]],
    system_probabilities: Mapping[str, Mapping[str, float]],
    family_by_system: Mapping[str, str],
    deterministic: Mapping[str, Any],
    hours_to_settlement: float | None,
    radiation_standardised: Any,
    fit: Mapping[str, Any] | None,
    mode: str | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """The vector P0 serves and its receipt.  Never raises: anything short serves the fallback."""

    mode = MODE if mode is None else mode
    base = {k: float(fallback[k]) for k in ordered}
    receipt: dict[str, Any] = {
        "rule": RULE_VERSION, "mode": mode, "state": "FALLBACK",
        "fit_fitted_at_utc": (fit or {}).get("fitted_at_utc"),
        "fit_verdict": ((fit or {}).get("walk_forward") or {}).get("verdict"),
        "fallback_probabilities": {k: round(v, 10) for k, v in base.items()},
        "deterministic": dict((deterministic or {}).get("systems") or {}),
    }
    try:
        geom = geometry(ordered, intervals)
        moments = family_moments(system_probabilities, family_by_system, ordered, geom)
        values = {k: float(v["day_max_c"]) for k, v in receipt["deterministic"].items()}
        rad = float(radiation_standardised) if isinstance(radiation_standardised, (int, float)) else 0.0
        receipt.update(family_moments=moments, radiation_standardised=rad, hours_to_settlement=hours_to_settlement)
        if fit is None or not fit.get("theta"):
            receipt["reason"] = "NO_FIT"
            return base, receipt
        features = det_features(values, fit.get("bias") or {})
        if not moments:
            receipt["reason"] = "NO_SEATED_FAMILY"
            return base, receipt
        if features is None:
            receipt["reason"] = "TOO_FEW_DETERMINISTIC"
            return base, receipt
        if hours_to_settlement is None:
            receipt["reason"] = "NO_HOURS_TO_SETTLEMENT"
            return base, receipt
        F, Z = features
        L = (float(hours_to_settlement) - 36.0) / 24.0
        m = np.array([[moments.get(f, [0.0, 1.0])[0] for f in FAMILIES]])
        s = np.array([[moments.get(f, [0.0, 1.0])[1] for f in FAMILIES]])
        has = np.array([[f in moments for f in FAMILIES]])
        q, mu, sd = predict_batch(
            fit["theta"], m=m, s=s, has=has, F=np.array([F]), Z=np.array([Z]), L=np.array([L]),
            rad=np.array([rad]), tlo=np.array([geom["tlo"]]), thi=np.array([geom["thi"]]),
            mask=np.ones((1, len(ordered)), bool),
        )
        # predict_batch already applied charter 224's floor (lift, renormalise --
        # floor_probabilities' rule); the vector served is the one the fit scored.
        ngr = {k: float(q[0, i]) for i, k in enumerate(ordered)}
        terms = centre_terms(fit["theta"], m=m, has=has, F=np.array([F]), L=np.array([L]), rad=np.array([rad]))
        receipt.update(f_c=round(F, 4), z=round(Z, 4), L=round(L, 4), mu_c=round(float(mu[0]), 4),
                       sd_c=round(float(sd[0]), 4),
                       # How the centre was put together (user 2026-10-26 「我看不出确定性预报和p0的关系」).
                       centre_terms={
                           "ensemble_c": round(float(terms["mu_e"][0]), 4),
                           "f_weight": round(float(terms["f_weight"][0]), 4),
                           "f_pull_c": round(float(terms["f_pull"][0]), 4),
                           "radiation_c": round(float(terms["rad_warm"][0] + terms["rad_cold"][0]), 4),
                           "constant_c": round(float(terms["a"][0]), 4),
                       },
                       ngr_probabilities={k: round(float(ngr[k]), 10) for k in ordered})
        if receipt["fit_verdict"] != "PASS":
            receipt["reason"] = "GATE_NOT_PASSED"
            return base, receipt
        if mode != "serve":
            receipt["reason"] = "MODE_SHADOW"
            return base, receipt
        receipt["state"] = "SERVED"
        return {k: float(ngr[k]) for k in ordered}, receipt
    except Exception as exc:  # noqa: BLE001 -- the layer never stops a compile
        receipt["state"] = "FALLBACK"
        receipt["reason"] = "ERROR"
        receipt["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return base, receipt
