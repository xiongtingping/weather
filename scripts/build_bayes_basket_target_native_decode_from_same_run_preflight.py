#!/usr/bin/env python3
"""Adapt same-run retained-raw preflight into native decode evidence.

This is a bounded evidence adapter.  It copies no raw payload and never writes
SQLite, changes an activation, or authorizes calibration/promotion.  The
source preflight must already prove one official run, complete curves, raw
SHA lineage, and a non-authorizing boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import event_day_window  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.station_registry import coordinate_registry_manifest, station_coordinates  # noqa: E402


SCHEMA = "bayes_basket_target_native_model_arrival_decode_evidence_v1"
PREFLIGHT_SCHEMA = "bayes_basket_target_same_run_retained_raw_preflight_v1"
# A same-run preflight is an explicitly scoped diagnostic.  Do not bake a
# calendar date into its defaults: an accidental default must never create or
# promote a future target day.
DEFAULT_INPUT = ROOT / "output/audits/bayes_basket_target_same_run_retained_raw_preflight.json"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_native_model_arrival_decode.json"
MAX_INPUT_BYTES = 64 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _date_boundary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep future/open-day decode evidence visibly outside formal history."""

    target_local_date = str(payload.get("target_local_date") or "").strip()
    target_timezone = str(payload.get("target_timezone") or "").strip()
    decision_at_utc = str(payload.get("decision_at_utc") or "").strip()
    if not target_local_date or not target_timezone or not decision_at_utc:
        raise ContractError("same_run_preflight_date_boundary_missing")
    try:
        window = event_day_window(
            target_local_date,
            as_of_utc=decision_at_utc,
            target_timezone=target_timezone,
        )
    except Exception as exc:
        raise ContractError("same_run_preflight_date_boundary_invalid") from exc
    state = str(window.get("state") or "")
    if state not in {"not_started", "open", "closed"}:
        raise ContractError("same_run_preflight_date_boundary_invalid")
    forward_only = state == "not_started"
    return {
        "target_local_date": target_local_date,
        "target_timezone": target_timezone,
        "decision_at_utc": decision_at_utc,
        "event_day_state": state,
        "forward_only": forward_only,
        "scope": "FORWARD_DIAGNOSTIC_ONLY" if forward_only else "PROSPECTIVE_EVIDENCE_ONLY",
        "historical_ssot_allowed": False if forward_only else state == "closed",
        "formal_calibration_allowed": False,
        "formal_decision_allowed": False,
        "reason": "target_settlement_day_not_started" if forward_only else "evidence_adapter_never_authorizes_formal_use",
    }


def build(*, input_path: Path, output_path: Path) -> dict[str, Any]:
    source = input_path.expanduser().resolve()
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise ContractError("same_run_preflight_input_too_large")
    payload = load_bounded_json(source, limit=MAX_INPUT_BYTES)
    if not isinstance(payload, Mapping) or payload.get("schema") != PREFLIGHT_SCHEMA:
        raise ContractError("same_run_preflight_schema_invalid")
    checks = payload.get("checks")
    boundary = payload.get("boundary")
    curves = payload.get("curves")
    points = payload.get("model_points")
    if not isinstance(checks, Mapping) or not isinstance(boundary, Mapping) or not isinstance(curves, Mapping) or not isinstance(points, list):
        raise ContractError("same_run_preflight_shape_invalid")
    date_boundary = _date_boundary(payload)
    registry_sha = str(
        payload.get("market_registry_snapshot_sha256") or ""
    ).strip().lower()
    if (
        len(registry_sha) != 64
        or any(ch not in "0123456789abcdef" for ch in registry_sha)
    ):
        raise ContractError("same_run_preflight_market_registry_hash_missing")
    required_checks = {
        "one_run_only": True,
        "official_provider_only": True,
        "raw_sha256_verified": True,
        "complete_curve_count_positive": True,
        "exact_three_model_join_positive": True,
        "model_arrival_index_complete": True,
        "all_model_arrivals_prospective": True,
        "all_model_arrival_publication_sources_native": True,
    }
    if any(checks.get(key) is not value for key, value in required_checks.items()):
        raise ContractError("same_run_preflight_required_check_failed")
    if boundary.get("writes_target_database") is not False or boundary.get("writes_shared_live_database") is not False or boundary.get("calls_order_gateway") is not False:
        raise ContractError("same_run_preflight_boundary_invalid")
    eligible = curves.get("eligible_model_rows")
    if not isinstance(eligible, list) or int(curves.get("complete_curve_count") or 0) != len(eligible):
        raise ContractError("same_run_preflight_curve_eligibility_invalid")
    decoded_points: list[dict[str, Any]] = []
    for raw_point in points:
        if not isinstance(raw_point, Mapping):
            raise ContractError("same_run_preflight_model_point_invalid")
        point = dict(raw_point)
        station_id = str(point.get("station_id") or "").strip().upper()
        if not station_id:
            raise ContractError("same_run_preflight_model_point_station_missing")
        # The preflight is allowed to fan out one official global object to
        # the canonical station set, but the resulting point must carry the
        # exact station-coordinate contract used by the decoder.  Do not
        # infer coordinates from the weather payload or from a market label.
        point["station_id"] = station_id
        point["coordinate_lineage"] = station_coordinates(station_id)
        decoded_points.append(point)
    identity = {
        "schema": SCHEMA,
        "as_of_utc": payload.get("decision_at_utc"),
        "target_local_date": date_boundary["target_local_date"],
        "date_boundary": date_boundary,
        "target_timezone": payload.get("target_timezone"),
        "market_registry_snapshot_sha256": registry_sha,
        "station_coordinate_registry": coordinate_registry_manifest(),
        "input_artifacts": [{"path": str(source), "sha256": _sha256(source)}],
        "decoded_point_count": len(points),
        "decoded_points": decoded_points,
        "blocked_captures": payload.get("blockers") or [],
        "curves": curves,
        "checks": {
            "native_provider_only": True,
            "official_ecmwf_gfs_icon_only": True,
            "decoded_points_have_raw_lineage": bool(points) and all(len(str(item.get("raw_message_sha256") or "")) == 64 for item in points if isinstance(item, Mapping)),
            "decoded_points_have_coordinate_lineage": bool(decoded_points) and all(isinstance(item.get("coordinate_lineage"), Mapping) for item in decoded_points),
            "complete_curve_count_positive": int(curves.get("complete_curve_count") or 0) > 0,
            "promotion_allowed": False,
            "production_authority": False,
            "writes_production_db": False,
            "writes_target_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "production_calibration_eligible": False,
        },
        "boundary": {
            "target_only": True,
            "read_only_decode": True,
            "derived_from_same_run_preflight": True,
            "writes_production_db": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
            "protected_production_line_touched": False,
        },
    }
    result = {**identity, "evidence_sha256": content_sha256(identity)}
    result["receipt_sha256"] = content_sha256(result)
    write_bounded_json(output_path.expanduser().resolve(), result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = build(input_path=args.input, output_path=args.output)
    print(json.dumps({
        "schema": result["schema"],
        "output": str(args.output.expanduser().resolve()),
        "decoded_point_count": result["decoded_point_count"],
        "complete_curve_count": result["curves"]["complete_curve_count"],
        "promotion_allowed": result["checks"]["promotion_allowed"],
        "receipt_sha256": result["receipt_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
