"""Charter 95's second feature: correct the curve by today's own error.

Charter 95 listed two regional features for the fusion -- "剩余小时预报最高−已报
最高" and "当前小时预报−实况" -- and neither was built; charter 99 fused the raw
curve instead.  This is the second one.

It exists because of what the first correction taught.  A per-station offset
fitted on history (``regional_point_debias``, 2026-09-07) measured -25% on the
point forecast and +0.212 nat *worse* on the served distribution, because the
fusion already anchors on ``max(remaining_peak, running_high)`` and that max is
the same correction made out of today's observations.  A historical constant
adds nothing to it.

The live gap is not a historical constant.  It is how far this model's value
for the hour now in progress sits from what the station has just reported --
"this run is 2.3C cold *today*, right now" rather than "this lane runs cold in
general" -- and the max cannot know it, because the max only sees the running
high and never sees the model's error at the current hour.  Measured against
the anchor residual over 6840 serving instants:

    system            corr    gap<-2C -> gap>+2C, residual
    NCEP_NBM_POINT   +0.294   -0.21 -> +0.81C   (monotone across all 7 bins)
    JMA_MSM_POINT    +0.336   -0.87 -> +0.13C
    NCEP_HRRR_POINT  +0.113   not monotone

The NBM is the reason this layer is being built.  Its raw curve was refused
admission to serving (-0.006 nat, straddling zero) while its live gap carries a
full degree of monotone signal, and it covers eleven US stations against the
MSM's six in East Asia.

**Mean-zero by construction.**  Charter 99's ``cell["bias_c"]`` is already the
fitted mean of the same residual.  A layer that served ``a + b*gap`` would put
its intercept on top of that mean and correct it twice -- the exact fault that
sank the historical debias.  So this serves ``b * (gap - gap_bar)``: the
gap-dependent part only, with the level left where it already lives.
"""
from __future__ import annotations

import json
import os
import statistics
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "bayes_basket_target_regional_live_gap_v1"
RULE_VERSION = "regional_live_gap_centered_slope_v1"
DEFAULT_ARTIFACT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_live_gap_artifact.json"
)
DEFAULT_REPLAY_TABLE = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_live_gap_replay_table.json"
)

ENABLED = (
    os.environ.get("PM_REGIONAL_LIVE_GAP", "off").strip().lower()
    not in {"0", "off", "false", "no"}
)

#: Which systems may have their anchor corrected by today's gap.  Empty until
#: the serving gate rules, and separate from
#: ``regional_path_prior.SERVING_SYSTEMS``: whether a lane may speak and how
#: its statement is corrected are two admissions, and the second one is what
#: this layer is.
LIVE_GAP_SYSTEMS = frozenset(
    part for part in (
        os.environ.get("PM_REGIONAL_LIVE_GAP_SYSTEMS", "").replace(",", " ").split()
    ) if part
)

#: ``anchor`` adds the correction to ``max(peak, running_high)``, which is what
#: the residual was measured against.  ``curve`` adds it to the curve's peak
#: before the max, the form with the physical reading -- the whole curve is
#: displaced, not just its maximum -- which cannot act at all when the running
#: high already dominates.  The gate decides.
FORM = (os.environ.get("PM_REGIONAL_LIVE_GAP_FORM", "anchor").strip().lower()
        or "anchor")

#: A system's pool must be this deep before its slope is served.
MIN_POOL_SAMPLES = 200
#: ``b`` shrunk toward zero by ``n/(n+K)``: a slope is a claim about a
#: relationship, and a thin sample's claim should mostly be "no relationship".
SHRINK_K = 200.0
#: The gap itself is clipped before it is multiplied.  A 9C gap at 03:00 local
#: is a station that has not started its day, not a model running cold, and
#: the fit has almost no samples out there to say what it means.
MAX_GAP_C = 4.0
#: And the product is clipped: this layer refines a level, it does not set one.
MAX_CORRECTION_C = 1.5

_CACHE: dict[str, Any] = {}


def live_gap_c(latest_observed_c: Any, curve_now_c: Any) -> float | None:
    """Degrees the station sits above this run's value for the current hour.

    Positive means the model is running cold today.  ``None`` whenever either
    side is missing -- the curve may not cover this hour, and a prefix with no
    formal report yet has nothing to compare against.
    """

    try:
        observed = float(latest_observed_c)
        modelled = float(curve_now_c)
    except (TypeError, ValueError):
        return None
    return observed - modelled


def shrunk_slope(pairs: list[tuple[float, float]]) -> tuple[float, float, int]:
    """``(slope, gap_bar, n)`` for ``residual ~ b * (gap - gap_bar)``.

    Ordinary least squares through the centred gap, then shrunk toward zero by
    sample size.  Returns a zero slope rather than raising on a degenerate
    sample, so a thin cell is silence and not a failure.
    """

    if len(pairs) < 2:
        return 0.0, 0.0, len(pairs)
    gaps = [g for g, _ in pairs]
    gap_bar = statistics.mean(gaps)
    residuals = [r for _, r in pairs]
    res_bar = statistics.mean(residuals)
    denominator = sum((g - gap_bar) ** 2 for g in gaps)
    if denominator <= 0.0:
        return 0.0, gap_bar, len(pairs)
    slope = sum(
        (g - gap_bar) * (r - res_bar) for g, r in pairs
    ) / denominator
    count = len(pairs)
    return slope * (count / (count + SHRINK_K)), gap_bar, count


def _load(path: Path, schema: str, key_prefix: str) -> dict[str, Any] | None:
    key = key_prefix + str(path)
    if key not in _CACHE:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if payload is not None and payload.get("schema") != schema:
            payload = None
        _CACHE[key] = payload
    return _CACHE[key]


def load_artifact(path: Path | None = None) -> dict[str, Any] | None:
    return _load(
        Path(path or os.environ.get("PM_REGIONAL_LIVE_GAP_ARTIFACT")
             or DEFAULT_ARTIFACT),
        SCHEMA, "live_gap_artifact::",
    )


def load_replay_table(path: Path | None = None) -> dict[str, Any] | None:
    return _load(
        Path(path or os.environ.get("PM_REGIONAL_LIVE_GAP_REPLAY_TABLE")
             or DEFAULT_REPLAY_TABLE),
        SCHEMA + "_replay", "live_gap_replay::",
    )


def replay_curve(
    event_date: str, table: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    payload = table if table is not None else load_replay_table()
    if not payload:
        return None
    entry = (payload.get("dates") or {}).get(str(event_date))
    if not entry:
        return None
    return {"schema": SCHEMA, "systems": entry.get("systems") or {},
            "trained_through": ""}


def resolve_slope(
    artifact: Mapping[str, Any] | None,
    *,
    system_id: str,
    systems: "frozenset[str] | set[str] | None" = None,
) -> dict[str, Any] | None:
    """This system's fitted slope and gap centre, or ``None`` for silence."""

    admitted = LIVE_GAP_SYSTEMS if systems is None else systems
    if str(system_id) not in admitted:
        return None
    scope = ((artifact or {}).get("systems") or {}).get(str(system_id))
    if not scope:
        return None
    if int(scope.get("sample_n") or 0) < MIN_POOL_SAMPLES:
        return None
    return {
        "slope_c_per_c": float(scope["slope_c_per_c"]),
        "gap_bar_c": float(scope["gap_bar_c"]),
        "sample_n": int(scope["sample_n"]),
        "fit_system_id": str(system_id),
    }


def correction_c(
    gap_c: float | None, slope: Mapping[str, Any] | None,
    *, enabled: bool | None = None,
) -> tuple[float, dict[str, str]]:
    """The degrees to add, and a receipt saying why it is what it is."""

    note: dict[str, str] = {
        "regional_live_gap_applied": "false",
        "regional_live_gap_rule": RULE_VERSION,
    }
    if not (ENABLED if enabled is None else enabled):
        note["regional_live_gap_skip_reason"] = "DISABLED"
        return 0.0, note
    if slope is None:
        note["regional_live_gap_skip_reason"] = "NO_FITTED_SLOPE"
        return 0.0, note
    if gap_c is None:
        # No formal report yet, or the curve does not cover this hour.
        note["regional_live_gap_skip_reason"] = "NO_LIVE_GAP"
        return 0.0, note
    clipped = max(-MAX_GAP_C, min(MAX_GAP_C, float(gap_c)))
    raw = float(slope["slope_c_per_c"]) * (clipped - float(slope["gap_bar_c"]))
    correction = max(-MAX_CORRECTION_C, min(MAX_CORRECTION_C, raw))
    return correction, {
        "regional_live_gap_applied": "true",
        "regional_live_gap_rule": RULE_VERSION,
        "regional_live_gap_form": FORM,
        "regional_live_gap_c": f"{float(gap_c):+.4f}",
        "regional_live_gap_clipped_c": f"{clipped:+.4f}",
        "regional_live_gap_slope": f"{float(slope['slope_c_per_c']):+.4f}",
        "regional_live_gap_centre_c": f"{float(slope['gap_bar_c']):+.4f}",
        "regional_live_gap_correction_c": f"{correction:+.4f}",
        "regional_live_gap_sample_n": str(slope.get("sample_n") or 0),
        "regional_live_gap_fit_system_id": str(slope.get("fit_system_id") or ""),
    }


def resolve_correction_for(
    artifact: Mapping[str, Any] | None,
    *,
    system_id: str,
    latest_observed_c: Any,
    curve_now_c: Any,
    systems: "frozenset[str] | set[str] | None" = None,
    enabled: bool | None = None,
) -> tuple[float, dict[str, str]]:
    """The whole layer in one call: gap, slope, correction, receipt.

    Production, the paper ledger and the admission gate each hold the two
    halves in different places -- the newest formal report comes from the
    trajectory, the curve's value for this hour from the lane's run -- so the
    arithmetic lives here rather than three times over.
    """

    return correction_c(
        live_gap_c(latest_observed_c, curve_now_c),
        resolve_slope(artifact, system_id=system_id, systems=systems),
        enabled=enabled,
    )


__all__ = [
    "ENABLED", "FORM", "LIVE_GAP_SYSTEMS", "MAX_CORRECTION_C", "MAX_GAP_C",
    "MIN_POOL_SAMPLES", "RULE_VERSION", "SCHEMA", "SHRINK_K",
    "correction_c", "live_gap_c", "load_artifact", "load_replay_table",
    "replay_curve", "resolve_correction_for", "resolve_slope", "shrunk_slope",
]
