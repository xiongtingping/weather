#!/usr/bin/env python3
"""Bounded acceptance for canonical-45 resident NWP scheduling.

This receipt proves that the running resident discovered the full canonical
station catalog and admitted only the bounded number of network scopes.  It
does not promote a model, authorize P0, or require forward OOS.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.nwp_rotation_state import (  # noqa: E402
    validate_rotation_state,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)


SCHEMA = "bayes_basket_target_canonical_nwp_scheduler_acceptance_v1"
DEFAULT_STATUS = ROOT / "data" / "reported_trajectory_no_status.json"
MAX_STATUS_AGE_SECONDS = 120.0
MAX_SELECTED_SCOPES = 4
MAX_CANDIDATE_PREFETCH_SCOPES = 1


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("canonical_nwp_status_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("canonical_nwp_status_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _active_nwp_attempt(status: Mapping[str, Any]) -> Mapping[str, Any]:
    forecast = status.get("forecast_prefetch")
    if not isinstance(forecast, Mapping):
        raise RuntimeError("canonical_nwp_forecast_status_missing")
    capture = forecast.get("resident_nwp_capture")
    if not isinstance(capture, Mapping):
        raise RuntimeError("canonical_nwp_capture_status_missing")
    if capture.get("state") == "not_due":
        previous = capture.get("last_attempt")
        if not isinstance(previous, Mapping):
            raise RuntimeError("canonical_nwp_last_attempt_missing")
        capture = previous
    return capture


def audit_status(
    status: Mapping[str, Any],
    *,
    as_of_utc: str,
) -> dict[str, Any]:
    canonical = tuple(coordinate_registry_manifest()["station_ids"])
    if len(canonical) != 45 or len(set(canonical)) != 45:
        raise RuntimeError("canonical_station_registry_invalid")
    blockers: list[str] = []
    heartbeat = str(status.get("heartbeat_at") or "")
    try:
        age_seconds = (
            _parse_utc(as_of_utc) - _parse_utc(heartbeat)
        ).total_seconds()
    except RuntimeError as exc:
        age_seconds = float("inf")
        blockers.append(str(exc))
    if age_seconds < 0 or age_seconds > MAX_STATUS_AGE_SECONDS:
        blockers.append("canonical_nwp_status_stale")
    try:
        capture = dict(_active_nwp_attempt(status))
    except RuntimeError as exc:
        capture = {}
        blockers.append(str(exc))
    configured = tuple(sorted(
        str(value or "").strip().upper()
        for value in capture.get("configured_station_ids") or []
        if str(value or "").strip()
    ))
    discovered = tuple(sorted(
        str(value or "").strip().upper()
        for value in capture.get("discovered_station_ids") or []
        if str(value or "").strip()
    ))
    selected_count = int(capture.get("selected_scope_count") or 0)
    successful_count = int(capture.get("successful_scope_count") or 0)
    candidate_attempts = int(
        capture.get("candidate_prefetch_attempt_count") or 0
    )
    candidate_limit = int(
        capture.get("candidate_prefetch_scope_limit") or 0
    )
    rotation = capture.get("rotation_state")
    if capture.get("station_scope_mode") != "canonical_allowlist":
        blockers.append("canonical_nwp_scope_mode_invalid")
    if configured != canonical:
        blockers.append("canonical_nwp_configured_scope_mismatch")
    if discovered != canonical:
        blockers.append("canonical_nwp_discovered_scope_mismatch")
    if int(capture.get("scope_count") or 0) < len(canonical):
        blockers.append("canonical_nwp_scope_count_incomplete")
    if selected_count < 1 or selected_count > MAX_SELECTED_SCOPES:
        blockers.append("canonical_nwp_selected_scope_bound_invalid")
    if (
        candidate_limit != MAX_CANDIDATE_PREFETCH_SCOPES
        or candidate_attempts < 0
        or candidate_attempts > candidate_limit
    ):
        blockers.append("canonical_nwp_candidate_scope_bound_invalid")
    if successful_count < 1 and candidate_attempts < 1:
        blockers.append("canonical_nwp_no_formal_or_candidate_attempt")
    if not isinstance(rotation, Mapping):
        blockers.append("canonical_nwp_rotation_state_missing")
    else:
        try:
            validate_rotation_state(rotation, station_ids=canonical)
        except Exception as exc:
            blockers.append(
                "canonical_nwp_rotation_state_invalid:"
                f"{type(exc).__name__}:{exc}"
            )
        if int(rotation.get("catalog_scope_count") or 0) != int(
            capture.get("scope_count") or 0
        ):
            blockers.append("canonical_nwp_rotation_catalog_mismatch")
        if str(rotation.get("catalog_sha256") or "") != str(
            capture.get("scope_catalog_sha256") or ""
        ):
            blockers.append("canonical_nwp_rotation_catalog_hash_mismatch")
        if len(rotation.get("latest_selected_scope_keys") or []) != (
            selected_count
        ):
            blockers.append("canonical_nwp_rotation_selection_mismatch")
    for key in (
        "calls_order_gateway",
        "places_orders",
        "live_submit_authorized",
    ):
        if capture.get(key) is not False:
            blockers.append(f"canonical_nwp_{key}_invalid")
    if int(capture.get("orders_submitted") or 0) != 0:
        blockers.append("canonical_nwp_orders_submitted_nonzero")
    identity = {
        "schema": SCHEMA,
        "state": "PASS" if not blockers else "BLOCKED_FAIL_CLOSED",
        "as_of_utc": as_of_utc,
        "heartbeat_at_utc": heartbeat,
        "status_age_seconds": age_seconds,
        "station_scope": {
            "mode": "canonical_allowlist",
            "station_ids": list(canonical),
        },
        "capture_state": capture.get("state"),
        "scope_count": int(capture.get("scope_count") or 0),
        "selected_scope_count": selected_count,
        "successful_scope_count": successful_count,
        "candidate_prefetch_attempt_count": candidate_attempts,
        "candidate_prefetch_scope_limit": candidate_limit,
        "rotation_generation": int(
            (rotation or {}).get("generation") or 0
        ),
        "rotation_attempted_scope_count": int(
            (rotation or {}).get("attempted_scope_count") or 0
        ),
        "rotation_complete": bool(
            (rotation or {}).get("complete_rotation")
        ),
        "rotation_state_sha256": str(
            (rotation or {}).get("content_sha256") or ""
        ),
        "configured_station_ids": list(configured),
        "discovered_station_ids": list(discovered),
        "blockers": sorted(set(blockers)),
        "forward_oos_boundary": {
            "blocks_production_no_live": False,
            "blocks_formal_promotion_or_live": True,
        },
        "production_no_live": True,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def wait_for_acceptance(
    status_path: Path,
    *,
    wait_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    latest: dict[str, Any] | None = None
    while True:
        if status_path.is_file() and not status_path.is_symlink():
            raw = load_bounded_json(status_path, limit=4 * 1024 * 1024)
            if isinstance(raw, Mapping):
                latest = audit_status(
                    raw,
                    as_of_utc=datetime.now(timezone.utc).isoformat(
                        timespec="milliseconds"
                    ).replace("+00:00", "Z"),
                )
                if latest["state"] == "PASS":
                    return latest
        if time.monotonic() >= deadline:
            if latest is not None:
                return latest
            raise RuntimeError("canonical_nwp_status_not_available")
        time.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=float, default=120.0)
    args = parser.parse_args()
    result = wait_for_acceptance(
        args.status.expanduser().resolve(),
        wait_seconds=args.wait_seconds,
    )
    write_bounded_json(
        args.output.expanduser().resolve(),
        result,
        limit=1024 * 1024,
    )
    print({
        "state": result["state"],
        "scope_count": result["scope_count"],
        "selected_scope_count": result["selected_scope_count"],
        "candidate_prefetch_attempt_count": result[
            "candidate_prefetch_attempt_count"
        ],
        "output": str(args.output.expanduser().resolve()),
    })
    return 0 if result["state"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
