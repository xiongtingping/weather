"""Read-only scan of retained strict-as-of forecast candidates.

This module deliberately has no P1 writer.  It preserves the historical/raw
candidate inspection needed by offline weak-source analysis while forecasts
remain confined to the P0/P0-M side of the canonical probability pipeline.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Mapping

from scripts.strict_asof_forecast_capture_readiness import _normalized_forecast
from strict_asof_forecast_capture_runtime import TIER_A, classify_strict_asof_forecast_capture
from strict_asof_forecast_source_observation_runtime import (
    build_tier_a_forecast_source_observation_proposal,
)


SCHEMA_VERSION = "strict_asof_forecast_candidate_scan_v1"
DEFAULT_ROOT = Path(__file__).resolve().parent / "data" / "research" / "forecast_source_skill" / "raw"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _read_gzip_json(path: Path) -> Mapping[str, Any] | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def collect_strict_asof_forecast_candidates(
    *, root: Path, station_id: str
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    unreadable: list[str] = []
    tier_counts = {"A": 0, "B": 0, "C": 0}
    for path in sorted(base.glob("**/*.json.gz")):
        envelope = _read_gzip_json(path)
        if envelope is None:
            unreadable.append(str(path))
            continue
        normalized = _normalized_forecast(envelope)
        capture = classify_strict_asof_forecast_capture(
            raw_envelope=envelope,
            normalized_forecast=normalized,
        )
        tier = _text(capture.get("evidence_tier"))
        tier_counts["A" if tier == TIER_A else "B" if tier.startswith("B_") else "C"] += 1
        if tier != TIER_A:
            continue
        try:
            raw_ref = "research://forecast_source_skill/raw/" + path.resolve().relative_to(base).as_posix()
        except ValueError:
            blocked.append({"path": str(path), "block_reasons": ["candidate_path_outside_root"]})
            continue
        proposal = build_tier_a_forecast_source_observation_proposal(
            capture=capture,
            normalized_forecast=normalized,
            station_id=station_id,
            raw_ref=raw_ref,
        )
        if proposal.get("ok") is not True:
            blocked.append(
                {
                    "path": str(path),
                    "source_id": _text(capture.get("source_id")),
                    "block_reasons": list(proposal.get("block_reasons") or []),
                }
            )
            continue
        eligible.append(
            {
                "path": str(path),
                "capture_id": _text(capture.get("forecast_capture_id")),
                "source_id": _text(capture.get("source_id")),
                "event_date": _text(capture.get("event_date")),
                "proposal": proposal,
            }
        )
    return {
        "schema": SCHEMA_VERSION,
        "root": str(base),
        "station_id": _text(station_id).upper(),
        "tier_counts": tier_counts,
        "eligible": eligible,
        "blocked": blocked,
        "unreadable": unreadable,
        "read_only": True,
        "p1_materialization": "RETIRED",
    }


__all__ = [
    "DEFAULT_ROOT",
    "collect_strict_asof_forecast_candidates",
]
