"""Charter 95: the single-system correction, fitted per station, before fusion.

Charter 95 landed the regional lane as capture-only and wrote its correction
down as a plan -- "逐站、按提前小时分档的单系统校正" -- explicitly marked
待度量后落地.  Charter 99 then skipped the gate and fused the raw curve.  This
is the measurement arriving and the plan landing: the curve is debiased first,
and only the debiased curve reaches the fusion in ``regional_path_prior``.

What is actually biased is *the statistic*, not the curve.  Measured against
the observation at the same valid hour, the MSM's curve is warm at night and
cold in the afternoon by about the same 1.2C -- a diurnal shape, not an
offset -- yet its statement about the day's maximum runs 2.2C cold at every
station it covers.  A max over an hourly, smoothed curve is not the max over
the ~48 sub-hourly reports the truth is taken from, and that deficit is a
property of one model at one station: how sharp that station's peak is and how
well a 5 km cell resolves it.  So the correction is fitted on, and applied to,
the remaining-segment peak -- the quantity the layer actually serves.

Two things the measurement changed about the plan:

*The lead axis carries no signal.*  Charter 95 planned per-station cells binned
by hours from run init to the decision instant.  On the strict as-of holdout
(fit on strictly earlier days only, 9743 samples over 15 event dates) the
station-only estimator and the station-by-lead estimator are the same to within
noise for the MSM (-25.1% vs -25.4% MAE) and the NBM (-18.4% vs -18.3%), and
splitting the lead axis only thins the station estimate that is doing the work.
The lead cells are still fitted and written into the artifact, and
``PM_REGIONAL_POINT_DEBIAS_AXIS=station_lead`` serves them, but the served axis
is the station.

*The correction only exists while the model's own peak is still ahead.*  The
raw residual ``truth - remaining_peak`` reads +3.3C for the MSM pooled, and
almost none of that is bias: near local midnight the day's high is already
behind, the remaining segment is legitimately low, and the gap is mechanical
(+7.0C for HRRR in the last four hours).  Fitting on that and serving it would
lift the peak, and therefore the mass above the running high, exactly when the
day has stopped warming.  Split on whether the curve's own maximum is still
ahead of the decision instant, the ahead half reads +1.46C for the MSM and the
behind half +3.91C.  Only the ahead half is a statement about remaining
warming, so it is the only half fitted and the only half corrected.
"""
from __future__ import annotations

import json
import os
import statistics
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "bayes_basket_target_regional_point_debias_v1"
RULE_VERSION = "regional_remaining_peak_station_debias_v1"
DEFAULT_ARTIFACT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_point_debias_artifact.json"
)
DEFAULT_REPLAY_TABLE = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_point_debias_replay_table.json"
)

#: Built, fitted, wired -- and measured not to serve.  Charter 95 ruled the
#: correction on a point-forecast reading, and that reading is real: on the
#: strict as-of holdout the MSM's remaining-peak MAE falls 2.00 -> 1.50C
#: (-25%) and the NBM's 1.22 -> 0.99C (-18%).  It does not survive the gate
#: that decides, which is what the served distribution does with it.  On the
#: TOO_EARLY window where the layer actually speaks, debiasing the MSM's
#: curve costs +0.212 nat against fusing it raw (95% CI +0.024 to +0.400 over
#: 46 station-days), argmax 61.8% -> 57.9%, and the loss is monotone in the
#: strength of the correction at every blend weight tried:
#:
#:     去偏强度        0.0     0.25     0.5     0.75     1.0
#:     w=0.50       1.060    1.125   1.210    1.284   1.346   nat
#:     w=0.30       1.198      --    1.328      --    1.419
#:     w=0.15       1.359      --    1.458      --    1.516
#:
#: The reason is that the fusion downstream already carries a debias, and a
#: better one: it anchors on ``max(remaining_peak, running_high)``, so a cold
#: curve is caught by what the station has already reported -- today's
#: observation rather than a fitted constant -- and the anchor comes out at
#: -0.10C with MAE 0.621.  Adding this correction in front of that max lifts
#: an anchor that was already right: +0.554C, MAE 0.825.  The layer's shape
#: makes that asymmetric.  The whole lower tail of the normal collapses onto
#: the bucket holding the running high, so an anchor that is slightly cold
#: costs almost nothing, while a warm one moves mass into buckets the day
#: never reaches and pays log loss for every one of them.
#:
#: So the correction is right about the model and wrong about the path.  It
#: stays here, fitted and wired, because the reading is worth keeping and
#: because the one place it would pay is a fusion that does not anchor on the
#: running high -- if the curve is ever asked where the day tops out without
#: the reported high beside it, this is the offset that question needs.
#: ``PM_REGIONAL_POINT_DEBIAS=on`` is the arm.
ENABLED = (
    os.environ.get("PM_REGIONAL_POINT_DEBIAS", "off").strip().lower()
    not in {"0", "off", "false", "no"}
)

#: Which systems may have their curve debiased.  HRRR is out by ruling
#: (2026-09-07, "HRRR 例外——它的误差不是偏差"), and the readings say why the
#: ruling is about a kind of error rather than a size of one.  On the strict
#: as-of holdout HRRR's pooled MAE does fall, 1.47 -> 1.16 (-20.8%), but the
#: whole of that comes from per-station offsets that disagree in sign and
#: stay large -- KSFO +2.74C against KHOU -1.68C -- and shrinking them toward
#: the lane's own pooled bias destroys the gain (-8.1% at K=800 against
#: -25.9% for the MSM, which is flat in K).  A lane whose correction is only
#: ever the station's own offset, four of whose ten stations get worse
#: out-of-sample, is not a lane running cold; it is a lane whose 3 km cell
#: represents ten different places ten different ways.  The MSM (-25.1%) and
#: the NBM (-18.4%) are flat in K, which is what a genuine lane-wide cold
#: bias looks like.
#:
#: This list is about debiasing only.  Whether a debiased system then reaches
#: the served distribution is a separate admission, held by
#: ``regional_path_prior.SERVING_SYSTEMS``, and HRRR is refused there too.
DEBIAS_SYSTEMS = frozenset(
    part for part in (
        os.environ.get(
            "PM_REGIONAL_POINT_DEBIAS_SYSTEMS",
            "JMA_MSM_POINT NCEP_NBM_POINT DWD_ICON_EU_POINT"
            " CMA_GRAPES_GLOBAL_POINT",
        ).replace(",", " ").split()
    ) if part
)

#: ``station`` serves the station's shrunk offset; ``station_lead`` serves the
#: station-by-lead cell where one is fitted.  Charter 95 planned the second;
#: the measurement above chose the first.
AXIS = (os.environ.get("PM_REGIONAL_POINT_DEBIAS_AXIS", "station").strip().lower()
        or "station")

#: Hours from run init to the decision instant.  Charter 95's axis, kept in
#: the artifact so the plan's form stays auditable and re-servable.
LEAD_EDGES = (0.0, 3.0, 6.0, 9.0, 1e9)
#: A system's pool must be this deep before any of its stations is corrected.
MIN_POOL_SAMPLES = 30
#: ``b = pooled + (station_mean - pooled) * n / (n + SHRINK_K)``.  Chosen on
#: the flat part of the holdout sweep: the MSM and the NBM move by under half
#: a point of MAE anywhere in 0..800, so the constant is picked for the small
#: samples it protects rather than for the reading it maximises.
SHRINK_K = 100.0
#: A level correction larger than this is not a bias, and serving it would put
#: the layer's whole weight on a bucket no evidence reached.
MAX_CORRECTION_C = 4.0
#: How much of the fitted offset is actually served.  A knob rather than a
#: constant because the fusion downstream already carries a correction of its
#: own -- ``max(peak, running_high)`` -- and the two compose rather than
#: compete; see the sweep recorded in SERVED_SCALE's comment.
SCALE = float(os.environ.get("PM_REGIONAL_POINT_DEBIAS_SCALE", "1.0") or 1.0)

_CACHE: dict[str, Any] = {}


def lead_bin(run_age_hours: float) -> int:
    for index in range(len(LEAD_EDGES) - 1):
        if LEAD_EDGES[index] <= float(run_age_hours) < LEAD_EDGES[index + 1]:
            return index
    return len(LEAD_EDGES) - 2


def shrunk(values: list[float], pooled: float) -> float:
    """The station's own offset pulled toward its lane's pooled one."""

    if not values:
        return float(pooled)
    count = len(values)
    own = statistics.mean(values)
    return float(pooled) + (own - float(pooled)) * (count / (count + SHRINK_K))


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
    target = Path(
        path or os.environ.get("PM_REGIONAL_POINT_DEBIAS_ARTIFACT")
        or DEFAULT_ARTIFACT
    )
    return _load(target, SCHEMA, "debias_artifact::")


def load_replay_table(path: Path | None = None) -> dict[str, Any] | None:
    target = Path(
        path or os.environ.get("PM_REGIONAL_POINT_DEBIAS_REPLAY_TABLE")
        or DEFAULT_REPLAY_TABLE
    )
    return _load(target, SCHEMA + "_replay", "debias_replay::")


def replay_curve(
    event_date: str, table: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """The artifact this event date may use: fitted only on earlier days."""

    payload = table if table is not None else load_replay_table()
    if not payload:
        return None
    entry = (payload.get("dates") or {}).get(str(event_date))
    if not entry:
        return None
    return {"schema": SCHEMA, "systems": entry.get("systems") or {},
            "trained_through": ""}


def resolve_correction(
    artifact: Mapping[str, Any] | None,
    *,
    system_id: str,
    station_id: str,
    run_age_hours: float,
    systems: "frozenset[str] | set[str] | None" = None,
) -> dict[str, Any] | None:
    """This system's fitted offset at this station, or ``None`` for silence.

    ``systems`` overrides the admission list, for the same reason
    ``regional_path_prior.resolve_serving_input`` takes one: a gate scoring a
    system that is not admitted yet would otherwise be refused the question it
    is asking.

    A system is never corrected from another system's residuals.  Charter 99
    allows that borrow for the *dispersion* because ICON-EU keeps no archive
    and a strict rule would silence it forever; a level offset is the opposite
    case -- it is the one number that is entirely about this model at this
    station, and borrowing it is how a lane inherits a bias it does not have.
    Silence here costs nothing: the fusion simply serves the raw curve.
    """

    admitted = DEBIAS_SYSTEMS if systems is None else systems
    if str(system_id) not in admitted:
        return None
    scope = ((artifact or {}).get("systems") or {}).get(str(system_id))
    if not scope:
        return None
    pool_n = int(scope.get("sample_n") or 0)
    if pool_n < MIN_POOL_SAMPLES:
        return None
    pooled = float(scope.get("pooled_bias_c") or 0.0)
    station = (scope.get("stations") or {}).get(str(station_id).upper())
    source = "pooled"
    bias = pooled
    sample_n = pool_n
    if AXIS == "station_lead":
        cell = ((station or {}).get("lead_cells") or {}).get(str(lead_bin(run_age_hours)))
        if cell:
            bias = float(cell["bias_c"])
            source = "station_lead_cell"
            sample_n = int(cell.get("sample_n") or 0)
    if source == "pooled" and station:
        bias = float(station["bias_c"])
        source = "station"
        sample_n = int(station.get("sample_n") or 0)
    if abs(bias) > MAX_CORRECTION_C:
        return None
    return {
        "bias_c": bias,
        "source": source,
        "sample_n": sample_n,
        "fit_system_id": str(system_id),
        "lead_bin": lead_bin(run_age_hours),
    }


def apply_remaining_peak_debias(
    remaining_peak_c: float,
    *,
    curve_peak_c: float,
    correction: Mapping[str, Any] | None,
    enabled: bool | None = None,
) -> tuple[float, dict[str, str]]:
    """The corrected remaining peak, and a receipt saying what was done.

    Returns the peak untouched whenever the layer declines: switched off, no
    fitted offset for this system and station, or a decision instant already
    past the curve's own maximum -- where the remaining segment is a statement
    about cooling, and a correction fitted on remaining warming has nothing to
    say about it.
    """

    raw = float(remaining_peak_c)
    note: dict[str, str] = {
        "regional_point_debias_applied": "false",
        "regional_point_debias_rule": RULE_VERSION,
    }
    if not (ENABLED if enabled is None else enabled):
        note["regional_point_debias_skip_reason"] = "DISABLED"
        return raw, note
    if correction is None:
        note["regional_point_debias_skip_reason"] = "NO_FITTED_OFFSET"
        return raw, note
    if raw < float(curve_peak_c) - 1e-9:
        note["regional_point_debias_skip_reason"] = "PEAK_BEHIND"
        return raw, note
    bias = float(correction["bias_c"]) * SCALE
    note = {
        "regional_point_debias_applied": "true",
        "regional_point_debias_rule": RULE_VERSION,
        "regional_point_debias_axis": AXIS,
        "regional_point_debias_bias_c": f"{bias:+.4f}",
        "regional_point_debias_scale": f"{SCALE:.3f}",
        "regional_point_debias_source": str(correction.get("source") or ""),
        "regional_point_debias_sample_n": str(correction.get("sample_n") or 0),
        "regional_point_debias_fit_system_id": str(
            correction.get("fit_system_id") or ""
        ),
        "regional_point_debias_raw_peak_c": f"{raw:.4f}",
    }
    return raw + bias, note


__all__ = [
    "AXIS", "DEBIAS_SYSTEMS", "ENABLED", "LEAD_EDGES", "MAX_CORRECTION_C",
    "MIN_POOL_SAMPLES", "RULE_VERSION", "SCALE", "SCHEMA", "SHRINK_K",
    "apply_remaining_peak_debias", "lead_bin", "load_artifact",
    "load_replay_table", "replay_curve", "resolve_correction", "shrunk",
]
