#!/usr/bin/env python3
"""Read-only readiness audit for tiered strict-as-of forecast capture files."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strict_asof_forecast_capture_runtime import (
    TIER_A,
    build_strict_asof_forecast_capture_manifest,
    classify_strict_asof_forecast_capture,
)


SCHEMA_VERSION = "strict_asof_forecast_capture_readiness_audit_v1"
DEFAULT_ROOT = ROOT / "data" / "research" / "forecast_source_skill" / "raw"
_EVENT_DATE_RE = re.compile(r"(?:^|_)(\d{4}-\d{2}-\d{2})(?:_|$)")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _event_date_from_source_event_id(value: Any) -> str:
    match = _EVENT_DATE_RE.search(_text(value))
    return match.group(1) if match else ""


def _legacy_open_meteo_normalization(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only enough to classify a historical raw response as Tier B."""

    try:
        payload = json.loads(_text(envelope.get("response_body_utf8")))
    except (TypeError, ValueError):
        payload = {}
    hourly = _mapping(_mapping(payload).get("hourly"))
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    values = hourly.get("temperature_2m") if isinstance(hourly.get("temperature_2m"), list) else []
    event_date = _event_date_from_source_event_id(envelope.get("source_event_id"))
    temperatures: list[float] = []
    for time_value, temperature in zip(times, values):
        try:
            parsed = float(temperature)
        except (TypeError, ValueError):
            continue
        if _text(time_value)[:10] == event_date:
            temperatures.append(parsed)
    return {
        "source_id": _text(envelope.get("source_id")),
        "target_event_date_local": event_date,
        "forecast_peak_c": max(temperatures) if temperatures else None,
        "forecast_snapshot_id": _text(envelope.get("response_sha256")),
        "source_event_id": _text(envelope.get("source_event_id")),
        "raw_provider_response_present": bool(_text(envelope.get("response_body_utf8"))),
        "response_headers_observed": envelope.get("response_headers_observed") is True,
    }


def _normalized_forecast(envelope: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(_text(envelope.get("response_body_utf8")))
    except (TypeError, ValueError):
        payload = {}
    wrapper = _mapping(payload)
    if wrapper.get("schema") == "forecast_source_forward_capture_payload_v2":
        derived = dict(_mapping(wrapper.get("derived_forecast")))
        metrics = _mapping(derived.get("metrics"))
        for key in ("forecast_value_kind", "forecast_record_valid_time", "forecast_peak_valid_time", "trajectory_eligible"):
            if key not in derived and key in metrics:
                derived[key] = metrics[key]
        if (
            not _text(derived.get("forecast_value_kind"))
            and _text(metrics.get("source")) == "weather_com_forecast_daily_peak"
        ):
            # Legacy immutable wrappers already preserve this provider's
            # daily record time.  Classifying it as a day-window scalar does
            # not fabricate an hourly peak or rewrite the envelope.
            derived["forecast_value_kind"] = "daily_max_event_window"
            derived["forecast_record_valid_time"] = _text(metrics.get("forecast_valid_local"))
            derived["trajectory_eligible"] = False
        if _text(derived.get("forecast_value_kind")) == "daily_max_event_window":
            # A separate hourly product is diagnostic auxiliary evidence, not
            # the trajectory of this daily-max forecast revision.
            derived["auxiliary_hourly_curve"] = list(derived.get("hourly_curve") or [])
            derived["hourly_curve"] = []
        derived["forecast_snapshot_id"] = _text(envelope.get("response_sha256"))
        derived["source_event_id"] = _text(envelope.get("source_event_id"))
        derived["raw_provider_response_present"] = bool(_text(wrapper.get("raw_provider_response_utf8")))
        # The envelope, not this derived wrapper, is the immutable transport
        # authority for whether headers had actually arrived.
        derived["response_headers_observed"] = envelope.get("response_headers_observed") is True
        return derived
    return _legacy_open_meteo_normalization(envelope)


def build_strict_asof_forecast_capture_readiness(
    root: Path,
    *,
    required_source_ids: Sequence[str],
    minimum_complete_event_days: int = 365,
) -> dict[str, Any]:
    """Audit local immutable envelopes without treating their file mtime as evidence."""

    captures: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    for path in sorted(Path(root).expanduser().glob("**/*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                envelope = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            unreadable.append({"path": str(path), "error": type(exc).__name__})
            continue
        if not isinstance(envelope, Mapping):
            unreadable.append({"path": str(path), "error": "payload_not_mapping"})
            continue
        capture = classify_strict_asof_forecast_capture(
            raw_envelope=envelope,
            normalized_forecast=_normalized_forecast(envelope),
        )
        captures.append({**capture, "raw_path": str(path)})
    manifest = build_strict_asof_forecast_capture_manifest(
        captures,
        required_source_ids=required_source_ids,
        minimum_complete_event_days=minimum_complete_event_days,
    )
    blockers: list[str] = []
    if unreadable:
        blockers.append("strict_capture_unreadable_envelopes_present")
    if not manifest["strict_training_ready"]:
        blockers.append("strict_capture_tier_a_coverage_not_ready")
    return {
        "schema": SCHEMA_VERSION,
        "state": "ready" if not blockers else "blocked",
        "ok": not blockers,
        "root": str(Path(root).expanduser().resolve()),
        "capture_count": len(captures),
        "unreadable": unreadable,
        "manifest": manifest,
        "blockers": blockers,
        "guards": {
            "writes_archive_db": False,
            "writes_research_files": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "read_only": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--required-source", action="append", default=["ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"])
    parser.add_argument(
        "--minimum-complete-event-days",
        type=int,
        default=365,
        help="Strict training threshold; pass 1 explicitly only for collector-contract health.",
    )
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args(argv)
    report = build_strict_asof_forecast_capture_readiness(
        args.root,
        required_source_ids=args.required_source,
        minimum_complete_event_days=max(1, int(args.minimum_complete_event_days)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not args.require_ready or report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
