#!/usr/bin/env python3
"""Run target evidence refresh after the registry settlement day is closed.

Before the close boundary this command writes only a bounded gate receipt and
does not read the shared live database, refresh evidence, touch target SSOT,
restart a service, call a gateway, or place an order.  After close it invokes
the existing read-only evidence coordinator with one explicit UTC clock.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.et_history import event_day_window  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from scripts.refresh_bayes_basket_target_evidence import run as refresh_evidence  # noqa: E402


SCHEMA = "bayes_basket_target_post_et_refresh_v1"
DEFAULT_RECEIPT = ROOT / "output/audits/bayes_basket_target_post_et_refresh.json"
DEFAULT_LANE_MAP = (
    ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _assert_artifact_scope(path: Path, *, event_date: str, as_of_utc: str, kind: str) -> None:
    """Prevent a forward artifact from entering a closed-day refresh."""

    resolved = path.expanduser().resolve()
    payload = load_bounded_json(resolved)
    if not isinstance(payload, Mapping):
        raise ValueError(f"post_et_{kind}_artifact_mapping_required")
    dates: set[str] = set()
    if kind == "native_decode":
        curves = payload.get("curves")
        rows = curves.get("eligible_model_rows") if isinstance(curves, Mapping) else []
        dates = {str(row.get("event_date") or "").strip() for row in rows or [] if isinstance(row, Mapping)}
    elif kind == "model_arrival":
        dates = {str(row.get("target_local_date") or "").strip() for row in payload.get("arrivals") or [] if isinstance(row, Mapping)}
    if not dates or dates != {event_date}:
        raise ValueError(f"post_et_{kind}_artifact_event_date_mismatch")
    artifact_as_of = str(payload.get("as_of_utc") or "").strip()
    if artifact_as_of and artifact_as_of != as_of_utc:
        raise ValueError(f"post_et_{kind}_artifact_as_of_mismatch")


def _scope_source_lane(
    station_ids: Sequence[str],
    *,
    lane_map_path: Path,
) -> str:
    if not station_ids:
        return "UNSCOPED_TARGET_REGISTRY"
    payload = load_bounded_json(lane_map_path.expanduser().resolve())
    stations = payload.get("stations") if isinstance(payload, Mapping) else None
    if not isinstance(stations, Mapping):
        raise ValueError("post_et_station_lane_map_missing")
    lanes = {
        str(stations.get(station_id) or "").strip().lower()
        for station_id in station_ids
    }
    if "" in lanes or not lanes.issubset({"amsc", "awc"}):
        raise ValueError("post_et_station_lane_unresolved")
    if len(lanes) == 1:
        return f"{next(iter(lanes)).upper()}_ONLY"
    return "MIXED_AMSC_AWC"


def run(
    *,
    event_date: str,
    settlement_timezone: str,
    as_of_utc: str = "",
    receipt_path: Path = DEFAULT_RECEIPT,
    native_decode: Path | None = None,
    model_arrival_evidence: Path | None = None,
    historical_canonical_paths: Sequence[Path] | None = None,
    historical_reference_paths: Sequence[Path] | None = None,
    station_ids: Sequence[str] | None = None,
    lane_map_path: Path = DEFAULT_LANE_MAP,
    refresh_runner: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    decision_clock = str(as_of_utc or _now_utc()).strip()
    window = event_day_window(
        event_date,
        as_of_utc=decision_clock,
        target_timezone=settlement_timezone,
    )
    normalized_station_ids = tuple(sorted({
        str(item).strip().upper()
        for item in (station_ids or [])
        if str(item).strip()
    }))
    scope_source_lane = _scope_source_lane(
        normalized_station_ids,
        lane_map_path=lane_map_path,
    )
    boundary = {
        "reads_shared_live_database": window["state"] == "closed",
        "shared_live_read_mode": "sqlite_mode_ro_query_only" if window["state"] == "closed" else "not_opened",
        "writes_shared_live_database": False,
        "writes_target_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "restarts_services": False,
        "live_submit_authorized": False,
    }
    core: dict[str, Any] = {
        "schema": SCHEMA,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "as_of_utc": decision_clock,
        "window": window,
        "station_scope": {
            "mode": "allowlist" if normalized_station_ids else "full_lane_map",
            "station_ids": list(normalized_station_ids),
            "source_lane": scope_source_lane,
        },
        "boundary": boundary,
    }
    if window["state"] != "closed":
        result = {**core, "state": "WAITING_FOR_SETTLEMENT_CLOSE", "refresh_invoked": False}
        write_bounded_json(receipt_path.expanduser().resolve(), result)
        return result

    if native_decode is not None:
        _assert_artifact_scope(native_decode, event_date=event_date, as_of_utc=decision_clock, kind="native_decode")
    if model_arrival_evidence is not None:
        _assert_artifact_scope(model_arrival_evidence, event_date=event_date, as_of_utc=decision_clock, kind="model_arrival")

    canonical_history = tuple(
        path.expanduser().resolve()
        for path in (historical_canonical_paths or [])
    )
    reference_history = tuple(
        path.expanduser().resolve()
        for path in (historical_reference_paths or [])
    )

    runner = refresh_runner or refresh_evidence
    refresh_kwargs: dict[str, Any] = {"as_of_utc": decision_clock}
    station_scope = set(normalized_station_ids)
    if station_scope:
        refresh_kwargs["station_ids"] = station_scope
    if native_decode is not None:
        refresh_kwargs["native_decode"] = native_decode.expanduser().resolve()
    if model_arrival_evidence is not None:
        refresh_kwargs["model_arrival_evidence"] = model_arrival_evidence.expanduser().resolve()
    if canonical_history:
        refresh_kwargs["historical_canonical_paths"] = list(canonical_history)
    if reference_history:
        refresh_kwargs["historical_reference_paths"] = list(reference_history)
    refresh = dict(runner(**refresh_kwargs))
    result = {
        **core,
        "state": str(refresh.get("state") or "BLOCKED_EVIDENCE"),
        "refresh_invoked": True,
        "refresh": refresh,
    }
    write_bounded_json(receipt_path.expanduser().resolve(), result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True, help="market settlement date, YYYY-MM-DD")
    parser.add_argument(
        "--settlement-timezone",
        required=True,
        help="exact market_registry.settlement_timezone",
    )
    parser.add_argument("--as-of-utc", default="", help="Explicit UTC decision clock")
    parser.add_argument("--native-decode", type=Path, help="same-run native decode evidence for this event")
    parser.add_argument("--model-arrival-evidence", type=Path, help="same-run prospective arrival evidence for this event")
    parser.add_argument(
        "--historical-canonical-evidence",
        type=Path,
        action="append",
        default=[],
        help="bounded historical canonical packet; repeatable",
    )
    parser.add_argument(
        "--historical-reference-evidence",
        type=Path,
        action="append",
        default=[],
        help="bounded reference-only packet; repeatable",
    )
    parser.add_argument(
        "--station-id",
        action="append",
        default=[],
        help="explicit station allowlist for the closed-day refresh; repeat for AWC-only scope",
    )
    parser.add_argument("--station-lane-map", type=Path, default=DEFAULT_LANE_MAP)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    args = parser.parse_args()
    try:
        result = run(
            event_date=args.event_date,
            settlement_timezone=args.settlement_timezone,
            as_of_utc=args.as_of_utc,
            receipt_path=args.receipt,
            native_decode=args.native_decode,
            model_arrival_evidence=args.model_arrival_evidence,
            historical_canonical_paths=args.historical_canonical_evidence,
            historical_reference_paths=args.historical_reference_evidence,
            station_ids=args.station_id,
            lane_map_path=args.station_lane_map,
        )
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "boundary": {
                "writes_shared_live_database": False,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
            },
        }
        write_bounded_json(args.receipt.expanduser().resolve(), result)
    print(json.dumps({key: result.get(key) for key in ("schema", "state", "event_date", "as_of_utc", "refresh_invoked", "receipt_sha256") if key in result}, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {"READY_FOR_STRICT_ASOF", "REFRESHED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
