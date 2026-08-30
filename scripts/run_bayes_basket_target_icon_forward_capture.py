#!/usr/bin/env python3
"""Run one bounded, target-only ICON prospective-capture cycle.

This is a one-shot data-ingress orchestrator for the isolated Bayes basket
target lane.  It deliberately does not reuse the resident NWP monitor and is
not a launchd service.  It can preserve a bounded canonical d2/d1 run before
the target ET day starts, downloads one official DWD ICON ``t_2m`` object per
valid hour through the existing bounded arrival collector, and only fans raw
receipts out to canonical stations after the ET day has opened.

The command never writes SQLite, production databases, probability state,
market state, order state, or any retired production-line file.  Only bounded
canonical d2/d1 targets are accepted; pre-event captures remain raw-only.  The
default mode is plan-only; ``--execute`` is required for bounded official HTTP
capture.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import TARGET_TIMEZONE, event_day_window  # noqa: E402
from bayes_basket_target.model_arrival import validate_prospective_capture_window  # noqa: E402
from bayes_basket_target.model_run_contract import (  # noqa: E402
    build_model_run_contract,
    normalize_run_initialization,
)
from bayes_basket_target.model_arrival_capture_config import (  # noqa: E402
    load_target_capture_config,
    normalize_resource_budget,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    assert_target_raw_retention_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402
from scripts.build_bayes_basket_target_global_model_fanout import fanout  # noqa: E402
from scripts.capture_bayes_basket_target_model_arrival import (  # noqa: E402
    DEFAULT_ROOT as DEFAULT_ARRIVAL_ROOT,
    _icon_availability_preflight,
    capture,
)


SCHEMA = "bayes_basket_target_icon_forward_capture_scheduler_v1"
PROVIDER_ID = "icon_global_official"
ICON_HOST = "opendata.dwd.de"
ICON_ROOT = "https://opendata.dwd.de/weather/nwp/icon/grib"
MAX_VALID_TIMES = 32
DEFAULT_TIMEOUT_SEC = 60.0
DEFAULT_OUTPUT_ROOT = ROOT / "output" / "audits" / "bayes_basket_target_icon_forward_runs"
MODEL_ARRIVAL_CONFIG = ROOT / "config" / "bayes_basket_target" / "model_arrival_capture_v1.json"


def _load_target_capture_contract(path: Path = MODEL_ARRIVAL_CONFIG) -> dict[str, Any]:
    """Bind the one-shot runner to the target capture SSOT.

    The JSON flag is deliberately checked at execution-plan construction, not
    merely reported by an audit.  This keeps a disabled or mis-scoped target
    capture from silently looking healthy while preserving the resident
    production line's independent environment gate.
    """

    return load_target_capture_config(path)


def _now_utc() -> str:
    # Preserve sub-second ordering when the fanout decision follows a fast
    # response.  Whole-second truncation can make a response completed at
    # 05:41:58.653 appear later than a fanout cutoff recorded as 05:41:58.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("icon_forward_scheduler_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("icon_forward_scheduler_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _event_date_for_now(
    as_of_utc: str,
    *,
    target_timezone: str = TARGET_TIMEZONE,
) -> str:
    return _parse_utc(as_of_utc).astimezone(ZoneInfo(target_timezone)).date().isoformat()


def _icon_url(run: str, forecast_hour: int) -> str:
    run_dt = _parse_utc(run)
    return (
        f"{ICON_ROOT}/{run_dt:%H}/t_2m/"
        f"icon_global_icosahedral_single-level_{run_dt:%Y%m%d%H}_{forecast_hour:03d}_T_2M.grib2.bz2"
    )


def _valid_hours(
    *,
    event_date: str,
    run: str,
    as_of_utc: str,
    target_timezone: str = TARGET_TIMEZONE,
    due_only: bool = True,
) -> list[dict[str, Any]]:
    window = event_day_window(
        event_date,
        as_of_utc=as_of_utc,
        target_timezone=target_timezone,
    )
    if window["state"] not in {"not_started", "open"}:
        raise ContractError("icon_forward_scheduler_target_day_closed")
    run_dt = _parse_utc(run)
    start = _parse_utc(window["target_day_start_utc"])
    end = _parse_utc(window["target_day_end_utc"])
    result: list[dict[str, Any]] = []
    current = start
    while current < end:
        if current >= run_dt:
            lead = int((current - run_dt).total_seconds() // 3600)
            result.append({
                "valid_time_utc": current.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "forecast_hour": lead,
                "url": _icon_url(run, lead),
            })
        current += timedelta(hours=1)
    if due_only and window["state"] == "open":
        decision_clock = _parse_utc(as_of_utc)
        result = [
            item
            for item in result
            if _parse_utc(str(item["valid_time_utc"])) <= decision_clock
        ]
    if not result:
        raise ContractError("icon_forward_scheduler_no_valid_hours_after_run")
    if len(result) > MAX_VALID_TIMES:
        raise ContractError("icon_forward_scheduler_valid_hour_cap_exceeded")
    return result


def _latest_cycle(as_of_utc: str) -> str:
    now = _parse_utc(as_of_utc).replace(minute=0, second=0, microsecond=0)
    cycle_hour = (now.hour // 6) * 6
    return now.replace(hour=cycle_hour).isoformat(timespec="seconds").replace("+00:00", "Z")


def _previous_icon_cycles(run_initialization_utc: str, *, limit: int = 3) -> list[str]:
    """Return older same-day/previous-day 6-hour cycles in newest-first order."""

    run = _parse_utc(run_initialization_utc)
    return [
        (run - timedelta(hours=6 * offset)).isoformat(timespec="seconds").replace("+00:00", "Z")
        for offset in range(1, max(1, int(limit)) + 1)
    ]


def _all_icon_preflight_blocked(result: Mapping[str, Any]) -> bool:
    """Identify a not-yet-published rolling ICON cycle, not a generic failure."""

    rows = result.get("capture_results")
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, Mapping) or row.get("status") != "BLOCKED":
            return False
        reasons = row.get("reason_codes")
        if not isinstance(reasons, list) or not any(
            str(reason).startswith("model_arrival_icon_preflight_") for reason in reasons
        ):
            return False
    return True


def build_plan(
    *,
    event_date: str | None = None,
    run_initialization_utc: str | None = None,
    as_of_utc: str | None = None,
    station_ids: Sequence[str] | None = None,
    target_timezone: str = TARGET_TIMEZONE,
    market_registry_snapshot_sha256: str = "",
    registry_bound: bool = False,
    source_station_id: str | None = None,
    due_only: bool = True,
    capture_config_path: Path = MODEL_ARRIVAL_CONFIG,
) -> dict[str, Any]:
    """Build a no-network plan for a bounded d2/d1 capture window."""

    capture_config = _load_target_capture_contract(capture_config_path)
    clock = as_of_utc or _now_utc()
    _parse_utc(clock)
    timezone_name = str(target_timezone or "").strip()
    if not timezone_name:
        raise ContractError("icon_forward_scheduler_target_timezone_missing")
    day = event_date or _event_date_for_now(
        clock,
        target_timezone=timezone_name,
    )
    run = normalize_run_initialization(run_initialization_utc or _latest_cycle(clock))
    window = validate_prospective_capture_window(
        target_local_date=day,
        target_timezone=timezone_name,
        run_initialization_utc=run,
        decision_at_utc=clock,
    )
    stations = sorted({str(item).strip().upper() for item in (station_ids or coordinate_registry_manifest()["station_ids"])})
    if registry_bound:
        if not stations or len(stations) > expected_station_count():
            raise ContractError("icon_forward_scheduler_registry_station_scope_invalid")
        registry_sha = str(market_registry_snapshot_sha256 or "").strip().lower()
        if len(registry_sha) != 64 or any(
            value not in "0123456789abcdef" for value in registry_sha
        ):
            raise ContractError("icon_forward_scheduler_registry_sha_invalid")
        anchor = str(source_station_id or stations[0]).strip().upper()
        if anchor not in stations:
            raise ContractError("icon_forward_scheduler_source_station_outside_scope")
    else:
        if len(stations) != expected_station_count():
            raise ContractError("icon_forward_scheduler_station_scope_not_45")
        if "ZUUU" not in stations:
            raise ContractError("icon_forward_scheduler_global_anchor_missing")
        registry_sha = ""
        anchor = "ZUUU"
    valid_hours = _valid_hours(
        event_date=day,
        run=run,
        as_of_utc=clock,
        target_timezone=timezone_name,
        due_only=bool(due_only),
    )
    contract = build_model_run_contract(
        target_local_date=day,
        run_initialization_utc=run,
        station_ids=stations,
        target_timezone=timezone_name,
        market_registry_snapshot_sha256=registry_sha,
    )
    resource_budget = dict(capture_config["resource_budget"])
    identity = {
        "schema": SCHEMA,
        "purpose": "prospective_icon_only_forward_capture",
        "provider_id": PROVIDER_ID,
        "target_timezone": timezone_name,
        "target_local_date": day,
        "event_day_window": window,
        "as_of_utc": clock,
        "run_initialization_utc": run,
        "station_ids": stations,
        "source_station_id": anchor,
        "market_registry_snapshot_sha256": registry_sha,
        "registry_bound": bool(registry_bound),
        "valid_hours": valid_hours,
        "due_only": bool(due_only),
        "capture_config_path": str(capture_config_path.expanduser().resolve()),
        "capture_config_sha256": content_sha256(capture_config),
        "capture_config_enabled": True,
        "model_run_contract_sha256": contract["contract_sha256"],
        "source": {"host": ICON_HOST, "transport": "https", "path_family": "DWD_ICON_t_2m"},
        "resource_budget": {
            "max_valid_times": MAX_VALID_TIMES,
            **resource_budget,
        },
        "boundary": {
            "target_only": True,
            "prospective_only": True,
            "execution_mode": "production_no_live",
            "formal_probability_input_role_eligible": True,
            "p0_probability_input_eligible_now": True,
            "p1_probability_input_eligible_now": window["state"] == "open",
            "raw_capture_may_precede_event": True,
            "p0_consumption_before_event": True,
            "p1_consumption_before_event": False,
            "writes_target_database": False,
            "writes_production_db": False,
            "writes_probability_state": False,
            "writes_market_state": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
            "live_submit_authorized": False,
            "unrelated_production_line_touched": False,
            "creates_service": False,
            "promotion_allowed": False,
        },
    }
    return {
        **identity,
        "model_run_contract": contract,
        "capture_config": capture_config,
        "plan_sha256": content_sha256(identity),
    }


def _safe_output_path(output_root: Path, run: str, event_date: str) -> Path:
    run_token = run.replace("-", "").replace(":", "").replace("Z", "Z")
    return output_root.expanduser().resolve() / f"{event_date}_{run_token}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_existing_capture_receipt(
    *,
    source_root: Path,
    target_local_date: str,
    run_initialization_utc: str,
    valid_time_utc: str,
    source_station_id: str = "ZUUU",
) -> Path | None:
    """Reuse one exact-key capture only when its raw body is still intact.

    The target lane is append-only and content-addressed.  Re-running a
    due-only cycle after a transient failure must not create a second receipt
    for an already accepted provider/run/valid-time key.  A receipt whose raw
    body is missing or whose digest no longer matches is deliberately ignored
    so the caller can attempt a bounded repair.
    """

    receipt_dir = source_root.expanduser().resolve() / PROVIDER_ID / "receipts"
    paths = sorted(receipt_dir.glob("*.json")) if receipt_dir.is_dir() else []
    if len(paths) > 5000:
        raise ContractError("icon_forward_receipt_scan_cap_exceeded")
    for path in paths:
        try:
            value = load_bounded_json(path)
        except (OSError, UnicodeDecodeError, ValueError, TypeError):
            continue
        if not isinstance(value, Mapping):
            continue
        if (
            value.get("schema") != "bayes_basket_target_native_model_transport_receipt_v1"
            or value.get("provider_id") != PROVIDER_ID
            or value.get("station_id") != source_station_id
            or value.get("target_local_date") != target_local_date
            or value.get("run_initialization_utc") != run_initialization_utc
            or value.get("valid_time_utc") != valid_time_utc
            or value.get("capture_mode") != "prospective"
            or value.get("publication_time_source") != "provider_http_last_modified"
        ):
            continue
        raw_receipts = value.get("raw_receipts")
        if not isinstance(raw_receipts, list) or len(raw_receipts) != 1:
            continue
        raw = raw_receipts[0]
        if not isinstance(raw, Mapping):
            continue
        raw_ref = Path(str(raw.get("raw_object_ref") or "")).expanduser()
        try:
            raw_ref = raw_ref.resolve()
        except OSError:
            continue
        expected = str(raw.get("raw_sha256") or "").strip().lower()
        if not raw_ref.is_file() or not re.fullmatch(r"[0-9a-f]{64}", expected):
            continue
        try:
            if _sha256_file(raw_ref) != expected:
                continue
        except (OSError, ValueError):
            continue
        try:
            actual_bytes = int(raw_ref.stat().st_size)
            response_bytes = int(raw.get("response_bytes") or -1)
            advertised_bytes = int(
                (raw.get("response_headers") or {}).get(
                    "content-length", -1
                )
            )
        except (OSError, TypeError, ValueError):
            continue
        if (
            response_bytes != actual_bytes
            or advertised_bytes != actual_bytes
        ):
            continue
        if not str(raw.get("response_completed_at") or "").strip():
            continue
        return path.resolve()
    return None


def execute_plan(
    plan: Mapping[str, Any],
    *,
    source_root: Path = DEFAULT_ARRIVAL_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Execute only the plan's bounded official ICON captures."""

    plan_identity = {
        key: value
        for key, value in plan.items()
        if key not in {"model_run_contract", "capture_config", "plan_sha256"}
    }
    if str(plan.get("plan_sha256") or "") != content_sha256(plan_identity):
        raise ContractError("icon_forward_scheduler_plan_sha256_mismatch")
    if (
        plan.get("provider_id") != PROVIDER_ID
        or plan.get("capture_config_enabled") is not True
        or plan.get("boundary", {}).get("execution_mode") != "production_no_live"
        or plan.get("boundary", {}).get("formal_probability_input_role_eligible") is not True
        or plan.get("boundary", {}).get("p0_consumption_before_event") is not True
        or plan.get("boundary", {}).get("p1_consumption_before_event") is not False
        or plan.get("boundary", {}).get("live_submit_authorized") is not False
        or plan.get("boundary", {}).get("unrelated_production_line_touched") is not False
    ):
        raise ContractError("icon_forward_scheduler_plan_boundary_invalid")
    capture_config = plan.get("capture_config")
    if (
        not isinstance(capture_config, Mapping)
        or content_sha256(capture_config) != str(plan.get("capture_config_sha256") or "")
    ):
        raise ContractError("icon_forward_scheduler_capture_config_hash_invalid")
    configured_budget = normalize_resource_budget(capture_config.get("resource_budget"))
    plan_budget = plan.get("resource_budget")
    if not isinstance(plan_budget, Mapping) or any(
        int(plan_budget.get(key) or 0) != expected
        for key, expected in configured_budget.items()
    ):
        raise ContractError("icon_forward_scheduler_resource_budget_mismatch")
    assert_process_memory_budget(limit=configured_budget["max_process_rss_bytes"])
    source = source_root.expanduser().resolve()
    assert_target_raw_retention_budget(
        source,
        bytes_needed=0,
        limit=configured_budget["max_raw_bytes"],
        min_free_bytes=configured_budget["min_free_bytes"],
    )
    run = str(plan["run_initialization_utc"])
    day = str(plan["target_local_date"])
    execution_window = validate_prospective_capture_window(
        target_local_date=day,
        target_timezone=str(plan["target_timezone"]),
        run_initialization_utc=run,
        decision_at_utc=_now_utc(),
    )
    destination = _safe_output_path(output_root, run, day)
    destination.mkdir(parents=True, exist_ok=True)
    contract = plan["model_run_contract"]
    source_station_id = str(plan.get("source_station_id") or "ZUUU")
    capture_results: list[dict[str, Any]] = []
    execution_decision_at = _now_utc()
    # Probe the first due object once.  A DWD cycle is published in lead-time
    # order; if its first eligible object is absent, probing every remaining
    # hour only repeats the same unavailable rolling-run condition.
    if plan.get("provider_id") == PROVIDER_ID and plan.get("valid_hours"):
        try:
            _icon_availability_preflight(
                str(plan["valid_hours"][0]["url"]),
                timeout_sec=timeout_sec,
            )
        except ContractError as exc:
            reason = str(exc)
            capture_results = [
                {
                    "valid_time_utc": str(item["valid_time_utc"]),
                    "forecast_hour": item["forecast_hour"],
                    "status": "BLOCKED",
                    "transport_receipt_path": None,
                    "reason_codes": [reason],
                }
                for item in plan["valid_hours"]
            ]
            result_core = {
                "schema": SCHEMA,
                "plan_sha256": plan["plan_sha256"],
                "target_local_date": day,
                "run_initialization_utc": run,
                "as_of_utc": plan["as_of_utc"],
                "valid_hours": list(plan["valid_hours"]),
                "execution_decision_at_utc": execution_decision_at,
                "capture_count": len(capture_results),
                "captured_count": 0,
                "reused_count": 0,
                "blocked_count": len(capture_results),
                "capture_results": capture_results,
                "fanout": None,
                "boundary": dict(plan["boundary"]),
                "output_root": str(destination),
            }
            result = {**result_core, "receipt_sha256": content_sha256(result_core)}
            write_bounded_json(destination / "run_result.json", result)
            return result
    for item in plan["valid_hours"]:
        assert_process_memory_budget(limit=configured_budget["max_process_rss_bytes"])
        assert_target_raw_retention_budget(
            source,
            bytes_needed=configured_budget["max_response_bytes"],
            limit=configured_budget["max_raw_bytes"],
            min_free_bytes=configured_budget["min_free_bytes"],
        )
        valid = str(item["valid_time_utc"])
        existing_receipt = _find_existing_capture_receipt(
            source_root=source,
            target_local_date=day,
            run_initialization_utc=run,
            valid_time_utc=valid,
            source_station_id=source_station_id,
        )
        if existing_receipt is not None:
            summary = {
                "valid_time_utc": valid,
                "forecast_hour": item["forecast_hour"],
                "status": "ALREADY_CAPTURED",
                "transport_receipt_path": str(existing_receipt),
                "reason_codes": [],
            }
            capture_results.append(summary)
            write_bounded_json(destination / f"capture_{valid.replace(':', '').replace('-', '')}.json", summary)
            continue
        # The plan clock only selects work that was due when the plan was
        # built.  Leave the strict arrival cutoff unset so the capture binds
        # decision_at_utc to the response completion observed by this
        # attempt.  Passing the pre-request clock here would reject every
        # successful live download as model_received_after_decision.
        capture_result = capture(
            provider_id=PROVIDER_ID,
            urls=[str(item["url"])],
            publication_url=None,
            station_id=source_station_id,
            target_local_date=day,
            run_initialization_utc=run,
            valid_time_utc=valid,
            decision_at_utc=None,
            target_timezone=str(plan["target_timezone"]),
            output_root=source,
            timeout_sec=timeout_sec,
            max_response_bytes=configured_budget["max_response_bytes"],
            max_capture_bytes=configured_budget["max_capture_bytes"],
            min_free_bytes=configured_budget["min_free_bytes"],
            run_contract=contract,
        )
        summary = {
            "valid_time_utc": valid,
            "forecast_hour": item["forecast_hour"],
            "status": capture_result.get("status"),
            "transport_receipt_path": capture_result.get("transport_receipt_path"),
            "reason_codes": (capture_result.get("receipt") or {}).get("reason_codes", []),
        }
        capture_results.append(summary)
        write_bounded_json(destination / f"capture_{valid.replace(':', '').replace('-', '')}.json", summary)
        execution_decision_at = _now_utc()

    captured = [item for item in capture_results if item["status"] == "CAPTURED"]
    reusable = [item for item in capture_results if item["status"] in {"CAPTURED", "ALREADY_CAPTURED"}]
    fanout_result: dict[str, Any] | None = None
    if reusable:
        fanout_result = fanout(
            provider=PROVIDER_ID,
            target_local_date=day,
            run_initialization_utc=run,
            decision_at_utc=execution_decision_at,
            target_timezone=str(plan["target_timezone"]),
            station_ids=[str(value) for value in plan["station_ids"]],
            source_station_id=source_station_id,
            source_root=source,
            output_dir=destination / "station_arrivals",
        )
    result_core = {
        "schema": SCHEMA,
        "plan_sha256": plan["plan_sha256"],
        "target_local_date": day,
        "run_initialization_utc": run,
        "as_of_utc": plan["as_of_utc"],
        "valid_hours": list(plan["valid_hours"]),
        "execution_decision_at_utc": execution_decision_at,
        "capture_count": len(capture_results),
        "captured_count": len(captured),
        "reused_count": sum(item["status"] == "ALREADY_CAPTURED" for item in capture_results),
        "blocked_count": sum(item["status"] == "BLOCKED" for item in capture_results),
        "capture_results": capture_results,
        "fanout": fanout_result,
        "boundary": dict(plan["boundary"]),
        "output_root": str(destination),
    }
    result = {**result_core, "receipt_sha256": content_sha256(result_core)}
    write_bounded_json(destination / "run_result.json", result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event-date",
        help="target settlement-local date; defaults to the date at --as-of-utc in --target-timezone",
    )
    parser.add_argument("--run-initialization-utc")
    parser.add_argument("--as-of-utc", default="")
    parser.add_argument(
        "--station-id",
        action="append",
        dest="station_ids",
        help="registry-bound station scope; repeat for multiple stations",
    )
    parser.add_argument(
        "--source-station-id",
        help="station identity recorded on the shared global-model source receipt",
    )
    parser.add_argument(
        "--target-timezone",
        default=TARGET_TIMEZONE,
        help="market_registry.settlement_timezone for the requested station scope",
    )
    parser.add_argument(
        "--market-registry-snapshot-sha256",
        default="",
        help="content hash of the registry snapshot owning the station/timezone scope",
    )
    parser.add_argument(
        "--registry-bound",
        action="store_true",
        help="allow an explicit <=45-station scope bound to a verified registry snapshot",
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_ARRIVAL_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument(
        "--include-not-yet-due-hours",
        action="store_true",
        help="plan all valid hours in the open ET day; execute mode remains bounded by the caller",
    )
    parser.add_argument("--execute", action="store_true", help="perform bounded official HTTP capture")
    parser.add_argument("--output", type=Path, help="plan/result JSON path")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(
            event_date=args.event_date,
            run_initialization_utc=args.run_initialization_utc,
            as_of_utc=args.as_of_utc or None,
            station_ids=args.station_ids,
            target_timezone=args.target_timezone,
            market_registry_snapshot_sha256=args.market_registry_snapshot_sha256,
            registry_bound=args.registry_bound,
            source_station_id=args.source_station_id,
            due_only=not args.include_not_yet_due_hours,
        )
        result: dict[str, Any]
        if args.execute:
            result = execute_plan(
                plan,
                source_root=args.source_root,
                output_root=args.output_root,
                timeout_sec=args.timeout_sec,
            )
            # DWD publishes ICON runs asynchronously.  The latest clock-aligned
            # run can therefore be entirely unavailable while the preceding
            # run is complete and still present in the rolling tree.  Retry
            # only that explicit preflight condition and only when the caller
            # did not pin a run; transport/resource failures remain blocked.
            fallback_attempts: list[dict[str, Any]] = []
            if (
                args.run_initialization_utc is None
                and result.get("captured_count", 0) == 0
                and result.get("reused_count", 0) == 0
                and _all_icon_preflight_blocked(result)
            ):
                for fallback_run in _previous_icon_cycles(str(plan["run_initialization_utc"])):
                    try:
                        fallback_plan = build_plan(
                            event_date=str(plan["target_local_date"]),
                            run_initialization_utc=fallback_run,
                            as_of_utc=str(plan["as_of_utc"]),
                            station_ids=[str(value) for value in plan["station_ids"]],
                            target_timezone=str(plan["target_timezone"]),
                            market_registry_snapshot_sha256=str(
                                plan.get("market_registry_snapshot_sha256") or ""
                            ),
                            registry_bound=bool(plan.get("registry_bound")),
                            source_station_id=str(
                                plan.get("source_station_id") or ""
                            ) or None,
                            due_only=not args.include_not_yet_due_hours,
                        )
                    except ContractError as exc:
                        fallback_attempts.append({"run_initialization_utc": fallback_run, "state": "BLOCKED", "reason": str(exc)})
                        continue
                    fallback_result = execute_plan(
                        fallback_plan,
                        source_root=args.source_root,
                        output_root=args.output_root,
                        timeout_sec=args.timeout_sec,
                    )
                    fallback_attempts.append({
                        "run_initialization_utc": fallback_run,
                        "captured_count": fallback_result.get("captured_count", 0),
                        "reused_count": fallback_result.get("reused_count", 0),
                        "blocked_count": fallback_result.get("blocked_count", 0),
                    })
                    if fallback_result.get("captured_count", 0) or fallback_result.get("reused_count", 0):
                        result = {
                            **fallback_result,
                            "fallback_from_run_initialization_utc": plan["run_initialization_utc"],
                            "fallback_attempts": fallback_attempts,
                        }
                        break
                else:
                    result = {**result, "fallback_attempts": fallback_attempts}
        else:
            result = {"schema": SCHEMA, "state": "PLAN_ONLY", **plan}
            result["receipt_sha256"] = content_sha256({k: v for k, v in result.items() if k != "receipt_sha256"})
        destination = args.output.expanduser().resolve() if args.output else (
            _safe_output_path(args.output_root, str(plan["run_initialization_utc"]), str(plan["target_local_date"])) / "plan.json"
        )
        write_bounded_json(destination, result)
        print(json.dumps({
            "schema": result["schema"],
            "state": result.get("state", "EXECUTED"),
            "target_local_date": result.get("target_local_date"),
            "run_initialization_utc": result.get("run_initialization_utc"),
            "valid_time_count": len(
                result.get("valid_hours")
                or result.get("capture_results")
                or plan.get("valid_hours")
                or []
            ),
            "captured_count": result.get("captured_count", 0),
            "blocked_count": result.get("blocked_count", 0),
            "output": str(destination),
            "writes_target_database": False,
            "calls_order_gateway": False,
            "promotion_allowed": False,
        }, ensure_ascii=False, sort_keys=True))
        return 0 if not args.execute or result.get("blocked_count", 0) == 0 else 2
    except Exception as exc:
        blocked = {
            "schema": SCHEMA,
            "state": "BLOCKED",
            "blockers": [f"{type(exc).__name__}:{exc}"],
            "boundary": {
                "target_only": True,
                "writes_target_database": False,
                "writes_production_db": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_order_authorized": False,
                "execution_mode": "production_no_live",
                "formal_probability_input_role_eligible": True,
                "p0_probability_input_eligible_now": True,
                "p1_probability_input_eligible_now": False,
                "p0_consumption_before_event": True,
                "p1_consumption_before_event": False,
                "live_submit_authorized": False,
                "unrelated_production_line_touched": False,
                "promotion_allowed": False,
            },
        }
        output = args.output.expanduser().resolve() if args.output else None
        if output is not None:
            write_bounded_json(output, blocked)
        print(json.dumps(blocked, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
