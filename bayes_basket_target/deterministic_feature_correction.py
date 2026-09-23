"""Charter 201 step 2: the P0 centre correction from a deterministic forecast.

The measured form (docs/research/station_regional_features_20260922):

    delta = k * ((R + b) - c),   capped at +/- CAP_C

    c  the served P0 mean on the served bucket axis
    R  the newest family run knowable at the decision that covers the local
       afternoon: its local-day maximum (``deterministic_forecast_family``)
    b  that run's own debias, resolved by the regional lane's global-form
       fit (``regional_point_debias``: cycle x lead scope, station pooled
       fallback; nothing when neither has five training days)
    k  a station x source shrink fitted on the station's own earlier settled
       cells: clip(sum((T - c) d) / sum(d^2), 0, 1), d = (R + b) - c, zero
       until ten event days

Where it would serve: after the radiation shift and the neighbour lean,
before the centre/width scalars and the band table -- a position, like the
radiation shift, so geometry still comes before claim.  The shift uses the
width layer's own transform (``apply_center_width_calibration``, width 1,
open edges as tails, charter 190).

This step records, it does not serve: every P0 compile carries
``deterministic_feature_json`` -- each source's R, b, d, k and delta, the
station's gate state, and for the station's candidate source the full
distribution it would have served (the served layers re-run on the shifted
input).  The served vector is untouched.  Which source may serve at a
station, and with which k, is the gate artifact's
(``scripts/fit_deterministic_feature_gate.py``), fitted daily on settled days
and pre-registered:

    PASS  >= 20 evaluable event days, and on event-day bootstrap intervals
          dRPS upper < 0, dRPS with the two best days removed upper < 0,
          dRPS against the random-sign control upper < 0, and the mean change
          in log score on non-zero-bucket rows not worse (<= 0).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

RULE_VERSION = "deterministic_centre_correction_v1"
LINEAGE_KEY = "deterministic_feature_json"
CAP_C = 1.5
MIN_K_EVENT_DAYS = 10
GATE_MIN_EVENT_DAYS = 20
ROOT = Path(__file__).resolve().parents[1]
GATE_ARTIFACT_PATH = Path(
    os.environ.get("PM_DETERMINISTIC_FEATURE_GATE")
    or ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "deterministic_feature_gate.json"
)
GATE_SCHEMA = "bayes_basket_target_deterministic_feature_gate_v1"
# Step 2: every station records, none serves.
MODE = os.environ.get("PM_DETERMINISTIC_FEATURE_MODE", "shadow").strip().lower()

_GATE_CACHE: dict[str, Any] = {}
_DEBIAS_CACHE: dict[str, Any] = {}


def load_debias_artifact(path: Path | None = None) -> dict[str, Any] | None:
    """The regional lane's daily debias fit, re-read when the file changes.

    ``regional_point_debias.load_artifact`` caches for the life of the
    process, which suits the P1 arm (off) but would leave a resident's P0
    feature on the fit it started with; this reads the same file and schema
    and follows the daily refit.
    """

    from .regional_point_debias import DEFAULT_ARTIFACT, SCHEMA  # noqa: PLC0415

    target = Path(path or os.environ.get("PM_REGIONAL_POINT_DEBIAS_ARTIFACT") or DEFAULT_ARTIFACT)
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return None
    cached = _DEBIAS_CACHE.get(str(target))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    _DEBIAS_CACHE.clear()
    _DEBIAS_CACHE[str(target)] = (stamp, payload)
    return payload


def load_gate_artifact(path: Path | None = None) -> dict[str, Any] | None:
    """The daily gate artifact, cached on its modification time."""

    target = Path(path or GATE_ARTIFACT_PATH)
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return None
    cached = _GATE_CACHE.get(str(target))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        payload = json.loads(target.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != GATE_SCHEMA:
        return None
    _GATE_CACHE[str(target)] = (stamp, payload)
    return payload


def served_mean_c(
    probability: Mapping[str, float], ordered: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]],
) -> float:
    from .center_width_calibration import served_bucket_axis  # noqa: PLC0415

    centres, _edges = served_bucket_axis(intervals, ordered)
    return float(sum(float(probability[key]) * c for key, c in zip(ordered, centres)))


def shift_distribution(
    probability: Mapping[str, float], ordered: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]], delta_c: float,
) -> dict[str, float]:
    """The width layer's transform at width 1: a pure shift, open edges as tails."""

    from .center_width_calibration import (  # noqa: PLC0415
        apply_center_width_calibration, served_bucket_axis,
    )

    if abs(float(delta_c)) < 1e-12:
        return {key: float(probability[key]) for key in ordered}
    centres, edges = served_bucket_axis(intervals, ordered)
    shifted, _receipt = apply_center_width_calibration(
        probability, delta_c=max(-CAP_C, min(CAP_C, float(delta_c))), width_lambda=1.0,
        ordered_keys=list(ordered), key_centers_c=centres, open_edge_bounds_c=edges,
    )
    return {key: float(shifted[key]) for key in ordered}


def debiased_feature(
    feature: Mapping[str, Any], *, system_id: str, station_id: str,
    debias_artifact: Mapping[str, Any] | None,
) -> tuple[float, dict[str, Any]] | None:
    """R + b for one source's feature, or None when the source has no fitted debias."""

    from .regional_point_debias import resolve_correction  # noqa: PLC0415

    correction = resolve_correction(
        debias_artifact, system_id=system_id, station_id=station_id,
        run_init_utc=str(feature["run_init_utc"]),
        lead_hours_to_local_day_start=float(feature["lead_hours_to_local_day_start"]),
        forecast_level_c=float(feature["day_max_c"]),
    )
    if correction is None:
        return None
    return float(feature["day_max_c"]) + float(correction["bias_c"]), correction


def station_gate(gate: Mapping[str, Any] | None, station_id: str) -> dict[str, Any]:
    return dict(((gate or {}).get("stations") or {}).get(str(station_id).upper()) or {})


def shadow_receipt(
    conn: Any,
    *,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
    settlement_timezone: str,
    served_probability: Mapping[str, float],
    pre_layer_probability: Mapping[str, float],
    ordered: Sequence[str],
    intervals: Mapping[str, Sequence[float | None]],
    served_layers: Any = None,
    gate: Mapping[str, Any] | None = None,
    debias_artifact: Mapping[str, Any] | None = None,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """What each family source says about this P0's centre, and what it would serve.

    Never raises: a feature that cannot be read is reported as such.
    ``served_layers(probability) -> probability`` re-runs the compile's own
    centre/width and band layers on a shifted input, so the candidate's
    shadow vector is exactly what the correction would serve.
    """

    from .deterministic_forecast_family import feature_at  # noqa: PLC0415

    receipt: dict[str, Any] = {
        "rule": RULE_VERSION, "mode": MODE, "state": "SHADOW", "cap_c": CAP_C,
        "sources": {}, "candidate": None, "served_delta_c": 0.0,
    }
    try:
        gate = gate if gate is not None else load_gate_artifact()
        debias = debias_artifact if debias_artifact is not None else load_debias_artifact()
        centre = served_mean_c(served_probability, ordered, intervals)
        receipt["centre_c"] = round(centre, 4)
        receipt["gate_fitted_at_utc"] = (gate or {}).get("fitted_at_utc")
        station = station_gate(gate, station_id)
        features = feature_at(
            conn, station_id=station_id, event_date=event_date, as_of_utc=decision_at_utc,
            settlement_timezone=settlement_timezone, rows=rows,
        )
        for system_id, feature in sorted(features.items()):
            entry: dict[str, Any] = {
                "day_max_c": round(float(feature["day_max_c"]), 4),
                "run_init_utc": feature["run_init_utc"],
                "knowable_at_utc": feature["knowable_at_utc"],
                "lead_hours_to_local_day_start": round(float(feature["lead_hours_to_local_day_start"]), 2),
            }
            resolved = debiased_feature(
                feature, system_id=system_id, station_id=station_id, debias_artifact=debias,
            )
            source_gate = dict((station.get("sources") or {}).get(system_id) or {})
            k = float(source_gate.get("k") or 0.0)
            entry["gate_state"] = str(source_gate.get("verdict") or "NO_RECORD")
            entry["k"] = round(k, 4)
            if resolved is None:
                entry["state"] = "NO_DEBIAS"
            else:
                value, correction = resolved
                gap = value - centre
                delta = max(-CAP_C, min(CAP_C, k * gap))
                entry.update(
                    state="READ", bias_c=round(float(correction["bias_c"]), 4),
                    bias_scope=f"{correction['source']}:{correction['scope']}",
                    debiased_c=round(value, 4), gap_c=round(gap, 4),
                    delta_c=round(delta, 4), clipped=abs(k * gap) > CAP_C,
                )
            receipt["sources"][system_id] = entry
        candidate = station.get("selected_system") or station.get("candidate_system")
        if candidate and receipt["sources"].get(candidate, {}).get("state") == "READ":
            delta = float(receipt["sources"][candidate]["delta_c"])
            receipt["candidate"] = candidate
            receipt["candidate_delta_c"] = delta
            if abs(delta) > 0.0 and served_layers is not None:
                shifted = shift_distribution(pre_layer_probability, ordered, intervals, delta)
                shadow = served_layers(shifted)
                receipt["shadow_probabilities"] = {
                    key: round(float(shadow[key]), 8) for key in ordered
                }
                receipt["shadow_centre_c"] = round(served_mean_c(shadow, ordered, intervals), 4)
    except Exception as exc:  # noqa: BLE001 -- a shadow never stops a compile
        receipt["state"] = "ERROR"
        receipt["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return receipt


def finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "CAP_C", "GATE_ARTIFACT_PATH", "GATE_MIN_EVENT_DAYS", "GATE_SCHEMA", "LINEAGE_KEY",
    "MIN_K_EVENT_DAYS", "MODE", "RULE_VERSION", "debiased_feature", "load_debias_artifact",
    "load_gate_artifact",
    "served_mean_c", "shadow_receipt", "shift_distribution", "station_gate",
]
