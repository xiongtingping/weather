"""What the four layers would have served, recorded beside what was served.

A shadow is how a layer earns its way in here.  It is computed on the same
prefix, the same root and the same instant as the live P1, written into the
envelope's lineage, and never multiplied into the distribution anyone trades.
Thirty event days of that, scored by the ledger, is the evidence; until then
the served posterior is byte-identical to what it would have been.

Two documents, because the layers are at different stages of their argument:

``p1_shadow_four_layer_json`` carries the promoted set -- the observation hard
bound, the phase split and the remaining-warming likelihood -- which measured
on 2026-09-02 as worth 0.19 C of the ladder's own centre error at 15:00 local
and a collapse of its bias from +0.48 C to +0.04.

``p1_shadow_reanchor_json`` carries the translation layer on its own.  It is
recorded and not promoted: standalone it cuts the root centre's RMSE 46% in the
nine-to-twelve-hour band, and inside the chain switching it off changes the
centre error by 0.006 C, because by the time it speaks the remaining-warming
layer has already read the same day off cloud, humidity and momentum.  Keeping
the record costs a few hundred bytes and is the only way that verdict stays
falsifiable.

Absent artifacts mean no keys at all, so every caller that has not been handed
them -- historical replays, fixtures, the packet rebuild before the worker
started supplying them -- compiles exactly the lineage it compiled before.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .metar_four_layer import compile_four_layer_posterior, feature_row

SCHEMA = "bayes_basket_target_p1_four_layer_shadow_v1"
_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME = _ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
HAZARD_ARTIFACT_PATH = _RUNTIME / "metar_phase_hazard_artifact.json"
REANCHOR_ARTIFACT_PATH = _RUNTIME / "metar_reanchor_artifact.json"
# Off until switched on deliberately.  With it off the loader returns nothing,
# the compiler adds no keys, and every lineage is the one it was before.
SHADOW_ENABLED = (
    os.environ.get("PM_P1_FOUR_LAYER_SHADOW", "0").strip()
    not in {"0", "false", "FALSE", "no", ""}
)
_CACHE: dict[str, Any] = {}
SHADOW_LINEAGE_KEYS = (
    "p1_shadow_four_layer_json",
    "p1_shadow_reanchor_json",
)
# The promoted set: fact, phase, remaining warming.  The reanchor is measured
# in its own document and multiplied into nothing.
PROMOTED_LAYERS = ("fact", "phase", "remaining_warming")


def load_shadow_artifacts(
    *,
    hazard_path: Path | None = None,
    reanchor_path: Path | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """The fitted files, or an empty mapping when the shadow is off.

    Cached on the file's modification time so a daily refit is picked up
    without a restart and a cycle does not re-parse them per station.
    """

    if not (SHADOW_ENABLED if enabled is None else enabled):
        return {}
    out: dict[str, Any] = {}
    for name, path in (
        ("hazard", hazard_path or HAZARD_ARTIFACT_PATH),
        ("reanchor", reanchor_path or REANCHOR_ARTIFACT_PATH),
    ):
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        key = f"{name}::{path}::{stamp}"
        if key not in _CACHE:
            try:
                _CACHE.clear()
                _CACHE[key] = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
        out[name] = _CACHE[key]
    return out


def _centre_c(
    probabilities: Mapping[str, float], centers: Mapping[str, float]
) -> float:
    return sum(
        float(value) * float(centers[bucket])
        for bucket, value in probabilities.items()
    )


def _argmax(probabilities: Mapping[str, float]) -> str:
    return max(probabilities, key=lambda bucket: probabilities[bucket])


def compile_p1_four_layer_shadow(
    *,
    root_probabilities: Mapping[str, float],
    bucket_ordinals: Mapping[str, int],
    bucket_centers_c: Mapping[str, float],
    bucket_intervals_c: Mapping[str, Sequence[float | None]],
    hard_excluded_buckets: Sequence[str],
    running_high_c: float,
    running_high_bucket: str,
    remaining_hours: float,
    path_state: Any,
    warming_so_far_c: float,
    curve_shape_realized: float | None,
    bias_recent_c: float | None,
    bias_report_count: int,
    hazard_artifact: Mapping[str, Any] | None,
    reanchor_artifact: Mapping[str, Any] | None,
) -> dict[str, str]:
    """The shadow lineage keys, or nothing at all when no artifact is supplied.

    Never raises on the layers' own account.  A shadow that could break a P1
    compile would be a measurement lane with the power to stop trading, which
    is the one thing it must not be; a layer that refuses is recorded as
    refused and the served distribution is untouched either way.
    """

    if not hazard_artifact:
        return {}
    try:
        features = feature_row(
            remaining_hours=remaining_hours,
            drawdown_from_high_c=path_state.drawdown_from_high_c,
            momentum_c_per_hour=path_state.momentum_c_per_hour,
            cloud_cover_fraction=path_state.cloud_cover_fraction,
            precipitation_present=path_state.precipitation_present,
            dewpoint_depression_c=path_state.dewpoint_depression_c,
            wind_speed_mps=path_state.wind_speed_mps,
            warming_so_far_c=warming_so_far_c,
            curve_shape_realized=curve_shape_realized,
        )
        common: dict[str, Any] = {
            "root_probabilities": root_probabilities,
            "bucket_ordinals": bucket_ordinals,
            "bucket_centers_c": bucket_centers_c,
            "bucket_intervals_c": bucket_intervals_c,
            "hard_excluded_buckets": list(hard_excluded_buckets),
            "running_high_c": float(running_high_c),
            "running_high_bucket": str(running_high_bucket),
            "features": features,
            "bias_recent_c": bias_recent_c,
            "bias_report_count": int(bias_report_count),
        }
        promoted, receipts = compile_four_layer_posterior(
            **common,
            hazard_artifact=hazard_artifact,
            reanchor_artifact=None,
            reanchor_enabled=False,
        )
        document = {
            "schema": SCHEMA,
            "layers": list(PROMOTED_LAYERS),
            "probabilities": {
                bucket: round(float(value), 12)
                for bucket, value in sorted(promoted.items())
            },
            "centre_c": _centre_c(promoted, bucket_centers_c),
            "argmax_bucket": _argmax(promoted),
            "features": features,
            "phase": receipts["phase"],
            "remaining_warming": receipts["remaining_warming"],
            "hazard_artifact_sha256": str(
                hazard_artifact.get("content_sha256") or ""
            ),
            "hazard_trained_through": str(
                hazard_artifact.get("trained_through") or ""
            ),
        }
        out = {
            "p1_shadow_four_layer_json": json.dumps(
                document, sort_keys=True, separators=(",", ":")
            )
        }
    except (ContractError, AttributeError, TypeError, ValueError, KeyError) as exc:
        return {
            "p1_shadow_four_layer_json": json.dumps(
                {"schema": SCHEMA, "state": "REFUSED",
                 "reason": str(exc)[:120]},
                sort_keys=True, separators=(",", ":"),
            )
        }
    if not reanchor_artifact:
        return out
    try:
        with_reanchor, reanchor_receipts = compile_four_layer_posterior(
            **common,
            hazard_artifact=hazard_artifact,
            reanchor_artifact=reanchor_artifact,
            reanchor_enabled=True,
        )
        out["p1_shadow_reanchor_json"] = json.dumps(
            {
                "schema": SCHEMA,
                "state": "SHADOW_ONLY_NOT_PROMOTED",
                "reanchor": reanchor_receipts["reanchor"],
                "centre_c": _centre_c(with_reanchor, bucket_centers_c),
                "centre_delta_c": (
                    _centre_c(with_reanchor, bucket_centers_c)
                    - float(document["centre_c"])
                ),
                "argmax_bucket": _argmax(with_reanchor),
                "reanchor_artifact_sha256": str(
                    reanchor_artifact.get("content_sha256") or ""
                ),
            },
            sort_keys=True, separators=(",", ":"),
        )
    except (ContractError, AttributeError, TypeError, ValueError, KeyError) as exc:
        out["p1_shadow_reanchor_json"] = json.dumps(
            {"schema": SCHEMA, "state": "REFUSED", "reason": str(exc)[:120]},
            sort_keys=True, separators=(",", ":"),
        )
    return out


def shadow_fingerprint(shadow: Mapping[str, str]) -> str:
    """A hash over the shadow documents, for receipts that must not carry them."""

    return content_sha256({
        key: shadow[key] for key in sorted(shadow) if shadow.get(key)
    })


__all__ = [
    "HAZARD_ARTIFACT_PATH",
    "PROMOTED_LAYERS",
    "REANCHOR_ARTIFACT_PATH",
    "SHADOW_ENABLED",
    "load_shadow_artifacts",
    "SCHEMA",
    "SHADOW_LINEAGE_KEYS",
    "compile_p1_four_layer_shadow",
    "shadow_fingerprint",
]
