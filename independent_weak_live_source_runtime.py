"""Read immutable TAF/WU/TWC captures for the live joint weak model.

The capture repository is shared with the strict-as-of training ingress, but
live scoring does not require Tier-A rows to have already been copied into P1.
Only proposals that pass the same immutable raw/arrival/source contract are
returned.  Training/OOS readiness remains an offline concern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import gzip
import json

from external_forecast_snapshot_runtime import validate_external_forecast_raw_envelope
from scripts.strict_asof_forecast_capture_readiness import _normalized_forecast
from strict_asof_forecast_candidate_scan import (
    DEFAULT_ROOT,
    collect_strict_asof_forecast_candidates,
)
from source_observation_runtime import build_source_observation


SCHEMA_VERSION = "independent_weak_live_source_snapshot_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _live_wu_auxiliary_observation(*, root: Path, event_date: str, station_id: str) -> dict[str, Any] | None:
    """Load current WU capture for live weak use without promoting it to Tier A.

    The public WU page can expose its hourly payload through a credential-shaped
    wrapper that cannot be persisted as a raw Tier-A response.  Its immutable
    first-seen envelope is still eligible for the approved live weak model, but
    remains explicitly excluded from strict-as-of training/OOS evidence.
    """

    pointer = root / "_latest_derived" / "wu_forecast_peak" / f"{event_date}.json"
    try:
        latest = json.loads(pointer.read_text(encoding="utf-8"))
        path = Path(_text(latest.get("snapshot_path"))).expanduser().resolve()
        if root.resolve() not in path.parents:
            return None
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            envelope = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(envelope, Mapping)
        or validate_external_forecast_raw_envelope(envelope).get("ok") is not True
        or _text(envelope.get("source_id")) != "wu_forecast_peak"
        or _text(envelope.get("availability_mode")) != "live_fetch"
    ):
        return None
    forecast = _normalized_forecast(envelope)
    if not forecast.get("forecast_peak_c"):
        try:
            body = json.loads(_text(envelope.get("response_body_utf8")))
        except (TypeError, ValueError):
            body = {}
        if isinstance(body, Mapping) and _text(body.get("schema")) == "forecast_source_forward_metrics_v1":
            forecast = dict(body)
    if _text(forecast.get("target_event_date_local"))[:10] != event_date:
        return None
    try:
        peak = float(forecast.get("forecast_peak_c"))
    except (TypeError, ValueError):
        return None
    candidates = [
        _text(row.get("valid_time_local"))
        for row in forecast.get("hourly_curve") or []
        if isinstance(row, Mapping)
        and row.get("temperature_c") is not None
        and abs(float(row.get("temperature_c")) - peak) <= 1e-9
        and _text(row.get("valid_time_local"))
    ]
    if not candidates:
        metrics = forecast.get("metrics") if isinstance(forecast.get("metrics"), Mapping) else {}
        declared_peak_at = _text(metrics.get("peak_at") or metrics.get("expected_peak_at_local"))
        if declared_peak_at:
            candidates = [declared_peak_at]
    first_seen = _text(envelope.get("response_completed_at"))
    raw_sha = _text(envelope.get("response_sha256"))
    if not candidates or not first_seen or not raw_sha:
        return None
    return build_source_observation(
        source_id="wu",
        station_id=station_id,
        event_date=event_date,
        valid_time=sorted(candidates)[0],
        valid_time_local=sorted(candidates)[0],
        fetched_at=first_seen,
        parsed_at=first_seen,
        response_completed_at=first_seen,
        forecast_peak_c=peak,
        raw_ref=f"research://forecast_source_skill/raw/{path.relative_to(root.resolve()).as_posix()}",
        raw_sha256_override=raw_sha,
        parser_version=SCHEMA_VERSION,
        quality_flags=("live_weak_auxiliary", "strict_asof_training_ineligible"),
        payload={
            "provider_source_id": "wu_forecast_peak",
            "weak_source_id": "wu",
            "available_at": first_seen,
            "forecast_snapshot_id": raw_sha,
            "raw_response_sha256": raw_sha,
            "forecast_peak_valid_time": sorted(candidates)[0],
            "trajectory_eligible": True,
            "strict_asof_training_eligible": False,
            "live_weak_model_eligible": True,
        },
    ).as_dict()


def load_live_independent_weak_source_observations(
    *,
    event_date: str,
    station_id: str = "ZUUU",
    root: Path | str = DEFAULT_ROOT,
) -> dict[str, Any]:
    """Return valid independent forecast observations without writing P1."""

    day = _text(event_date)[:10]
    if len(day) != 10:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["independent_weak_live_event_date_invalid"],
            "source_observations": [],
            "read_only": True,
        }
    scan = collect_strict_asof_forecast_candidates(
        root=Path(root),
        station_id=_text(station_id).upper(),
    )
    selected: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    for item in scan.get("eligible") or []:
        if _text(item.get("event_date")) != day:
            continue
        proposal = item.get("proposal") if isinstance(item.get("proposal"), Mapping) else {}
        observation = proposal.get("source_observation") if isinstance(proposal.get("source_observation"), Mapping) else {}
        if proposal.get("ok") is True and observation:
            selected.append(dict(observation))
            trace.append({
                "capture_id": _text(item.get("capture_id")),
                "provider_source_id": _text(item.get("source_id")),
                "weak_source_id": _text(observation.get("source_id")),
                "source_event_id": _text(observation.get("source_event_id")),
                "included": True,
            })
    if not any(_text(row.get("source_id")) == "wu" for row in selected):
        wu = _live_wu_auxiliary_observation(root=Path(root), event_date=day, station_id=_text(station_id).upper())
        if wu is not None:
            selected.append(wu)
            trace.append({
                "provider_source_id": "wu_forecast_peak",
                "weak_source_id": "wu",
                "source_event_id": _text(wu.get("source_event_id")),
                "included": True,
                "strict_asof_training_eligible": False,
                "live_weak_model_eligible": True,
            })
    selected.sort(key=lambda row: (_text(row.get("fetched_at")), _text(row.get("source_event_id"))))
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available" if selected else "empty_source_set",
        "event_date": day,
        "source_observations": selected,
        "source_observation_count": len(selected),
        "source_trace": trace,
        "quarantined": list(scan.get("blocked") or []),
        "read_only": True,
        "boundary": {
            "same_contract_as_strict_candidate_scan": True,
            "p1_materialization_not_required_for_live_scoring": True,
            "strict_asof_oos_not_live_gate": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "load_live_independent_weak_source_observations"]
