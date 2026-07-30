#!/usr/bin/env python3
"""Run one bounded final AWC backfill and post-ET evidence refresh.

This is a research/evidence job, not a production service.  It is safe to
invoke frequently: outside the first six ET hours it exits without touching
the network or any database, and a completed event day is not rebuilt twice.
The only persistent state is one bounded scheduler receipt plus the event-day
lineage packet required by the next refresh.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.et_history import TARGET_TIMEZONE  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


ET = ZoneInfo(TARGET_TIMEZONE)
SCHEMA = "bayes_basket_target_post_et_refresh_scheduler_v1"
DEFAULT_LANE_MAP = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
DEFAULT_PROXY_CONFIG = ROOT / "config/trading/bayes_basket_target_research_network_v1.json"
DEFAULT_RAW_ROOT = ROOT / "data/research/bayes_basket_target/historical_truth_raw"
DEFAULT_OUTPUT_ROOT = ROOT / "output/audits/bayes_basket_target_awc_post_et"
DEFAULT_STATE = ROOT / "output/audits/bayes_basket_target_post_et_scheduler.json"
DEFAULT_GATE_RECEIPT = ROOT / "output/audits/bayes_basket_target_post_et_refresh.json"
DEFAULT_HISTORICAL_CANONICAL = ROOT / "output/audits/bayes_basket_target_historical_canonical_observation_evidence.json"
DEFAULT_REFERENCE = ROOT / "output/audits/bayes_basket_target_truth_reference_evidence.json"
LOCK_PATH = ROOT / "output/audits/.bayes_basket_target_post_et_refresh_scheduler.lock"
MAX_COMMAND_OUTPUT = 16 * 1024


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_date_for_run(now_utc: datetime) -> tuple[str, datetime]:
    current_et = now_utc.astimezone(ET)
    event_date = current_et.date() - timedelta(days=1)
    return event_date.isoformat(), current_et


def _load_awc_station_ids(path: Path) -> list[str]:
    payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    stations = payload.get("stations") if isinstance(payload, Mapping) else None
    if not isinstance(stations, Mapping):
        raise ValueError("station_lane_map_invalid")
    result = sorted(
        str(station).strip().upper()
        for station, lane in stations.items()
        if str(lane).strip().lower() == "awc" and str(station).strip()
    )
    if not result:
        raise ValueError("awc_station_scope_empty")
    return result


def _bounded_text(value: str) -> str:
    return str(value or "")[-MAX_COMMAND_OUTPUT:]


def _run(command: list[str], *, timeout_sec: int = 900) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "timed_out": True,
            "stdout": _bounded_text(exc.stdout or ""),
            "stderr": _bounded_text(exc.stderr or ""),
        }
    return {
        "returncode": int(completed.returncode),
        "timed_out": False,
        "stdout": _bounded_text(completed.stdout),
        "stderr": _bounded_text(completed.stderr),
    }


def _read_state(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _write(path: Path, value: Mapping[str, Any]) -> None:
    write_bounded_json(path.expanduser().resolve(), value)


def _load_rebuild_receipt(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("final_awc_rebuild_receipt_unreadable") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError("final_awc_rebuild_receipt_invalid")
    if str(value.get("state") or "") != "CLOSED_HISTORICAL":
        raise RuntimeError("final_awc_rebuild_not_closed")
    if list(value.get("pending_event_dates") or []):
        raise RuntimeError("final_awc_rebuild_pending_event_date")
    if list(value.get("blockers") or []):
        raise RuntimeError("final_awc_rebuild_has_blockers")
    return value


def run(
    *,
    now_utc: datetime | None = None,
    lane_map: Path = DEFAULT_LANE_MAP,
    proxy_config: Path = DEFAULT_PROXY_CONFIG,
    raw_root: Path = DEFAULT_RAW_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    state_path: Path = DEFAULT_STATE,
    gate_receipt: Path = DEFAULT_GATE_RECEIPT,
    historical_canonical: Path = DEFAULT_HISTORICAL_CANONICAL,
    reference_evidence: Path = DEFAULT_REFERENCE,
) -> dict[str, Any]:
    clock = (now_utc or _now_utc()).astimezone(timezone.utc).replace(microsecond=0)
    event_date, current_et = _event_date_for_run(clock)
    base = {
        "schema": SCHEMA,
        "as_of_utc": _iso(clock),
        "event_date": event_date,
        "current_et": current_et.isoformat(),
        "target_timezone": TARGET_TIMEZONE,
        "boundary": {
            "research_only": True,
            "writes_shared_live_database": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "restarts_services": False,
            "live_submit_authorized": False,
        },
    }

    # launchd runs this job every few minutes.  The first six ET hours are a
    # bounded catch-up window after midnight; outside it, no source or DB is
    # opened and no receipt is rewritten.
    if current_et.hour >= 6:
        result = {**base, "state": "NOOP_OUTSIDE_POST_ET_WINDOW", "refresh_invoked": False}
        _write(state_path, result)
        return result

    previous = _read_state(state_path)
    if (
        previous
        and str(previous.get("event_date") or "") == event_date
        and str(previous.get("state") or "") in {
            "REFRESHED",
            "READY_FOR_STRICT_ASOF",
            "BLOCKED_EVIDENCE",
        }
    ):
        result = {**base, "state": "ALREADY_PROCESSED", "refresh_invoked": False}
        _write(state_path, result)
        return result

    station_ids = _load_awc_station_ids(lane_map)
    event_root = output_root.expanduser().resolve() / event_date
    event_root.mkdir(parents=True, exist_ok=True)
    rebuild_receipt = event_root / "rebuild.json"
    observations = event_root / "observations.json"
    reference = event_root / "reference.json"

    with LOCK_PATH.expanduser().resolve().open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result = {**base, "state": "NOOP_ALREADY_RUNNING", "refresh_invoked": False}
            _write(state_path, result)
            return result

        rebuild_command = [
            sys.executable,
            str(ROOT / "scripts/rebuild_bayes_basket_target_historical_truth.py"),
            "--station-lane-map",
            str(lane_map.expanduser().resolve()),
            "--event-date",
            event_date,
            "--output",
            str(rebuild_receipt),
            "--observations-output",
            str(observations),
            "--reference-output",
            str(reference),
            "--raw-root",
            str(raw_root.expanduser().resolve()),
            "--proxy-config",
            str(proxy_config.expanduser().resolve()),
            "--skip-amsc-fetch",
        ]
        for station in station_ids:
            rebuild_command.extend(("--station-id", station))
        rebuild = _run(rebuild_command)
        if rebuild.get("returncode") != 0:
            result = {**base, "state": "BLOCKED_REBUILD", "refresh_invoked": False, "rebuild": rebuild}
            _write(state_path, result)
            return result
        try:
            rebuild_receipt_value = _load_rebuild_receipt(rebuild_receipt)
        except RuntimeError as exc:
            result = {
                **base,
                "state": "BLOCKED_REBUILD_EVIDENCE",
                "refresh_invoked": False,
                "rebuild": rebuild,
                "reason": str(exc),
            }
            _write(state_path, result)
            return result
        if int(rebuild_receipt_value.get("station_day_count") or 0) != len(station_ids):
            result = {
                **base,
                "state": "BLOCKED_REBUILD_EVIDENCE",
                "refresh_invoked": False,
                "rebuild": rebuild,
                "reason": "final_awc_station_day_coverage_incomplete",
                "expected_station_day_count": len(station_ids),
                "actual_station_day_count": int(rebuild_receipt_value.get("station_day_count") or 0),
            }
            _write(state_path, result)
            return result

        historical_inputs = [
            path
            for path in (historical_canonical.expanduser().resolve(), observations)
            if path.is_file()
        ]
        if not historical_inputs:
            result = {
                **base,
                "state": "BLOCKED_REBUILD_EVIDENCE",
                "refresh_invoked": False,
                "rebuild": rebuild,
                "reason": "historical_canonical_inputs_missing",
            }
            _write(state_path, result)
            return result

        refresh_command = [
            sys.executable,
            str(ROOT / "scripts/bayes_basket_target_post_et_refresh.py"),
            "--event-date",
            event_date,
            "--as-of-utc",
            _iso(clock),
            "--receipt",
            str(gate_receipt.expanduser().resolve()),
        ]
        for path in historical_inputs:
            refresh_command.extend(("--historical-canonical-evidence", str(path)))
        refresh_command.extend([
            "--historical-reference-evidence",
            str(reference),
        ])
        for station in station_ids:
            refresh_command.extend(("--station-id", station))
        refresh = _run(refresh_command)
        state = "REFRESHED" if refresh.get("returncode") == 0 else "BLOCKED_EVIDENCE"
        result = {
            **base,
            "state": state,
            "refresh_invoked": True,
            "station_count": len(station_ids),
            "rebuild": rebuild,
            "refresh": refresh,
            "lineage": {
                "rebuild_receipt": str(rebuild_receipt),
                "formal_observations": str(observations),
                "reference_evidence": str(reference),
                "historical_canonical_inputs": [str(path) for path in historical_inputs],
            },
            "rebuild_receipt_summary": {
                "state": str(rebuild_receipt_value.get("state") or ""),
                "station_day_count": int(rebuild_receipt_value.get("station_day_count") or 0),
                "observation_count": int(rebuild_receipt_value.get("observation_count") or 0),
                "source_counts": dict(rebuild_receipt_value.get("source_counts") or {}),
            },
        }
        _write(state_path, result)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now-utc", default="", help="test clock, ISO-8601 UTC")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    try:
        clock = datetime.fromisoformat(args.now_utc.replace("Z", "+00:00")) if args.now_utc else None
        result = run(now_utc=clock, state_path=args.state)
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
        _write(args.state, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {"REFRESHED", "READY_FOR_STRICT_ASOF", "BLOCKED_EVIDENCE", "ALREADY_PROCESSED", "NOOP_OUTSIDE_POST_ET_WINDOW"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
