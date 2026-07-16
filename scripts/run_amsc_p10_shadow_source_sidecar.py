#!/usr/bin/env python3
"""Poll AMSC directly and publish only verified new primary bundles to P10 inbox.

This shadow source sidecar is intentionally independent of the legacy
fact-writer poller.  It has no release-delay setting, no SQLite/fact write,
no queue kick, and no curl/subprocess fallback.  A launchd calendar trigger
may run it around each half-hour; it retries only the source response and
stops when the exact expected report is raw-archived and published.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_p10_shadow_source_sidecar import (  # noqa: E402
    SCHEMA_VERSION,
    capture_amsc_p10_shadow_source_attempt,
    fetch_amsc_with_observed_transport,
)
from amsc_poll_slot_runtime import assess_zuuu_regular_metar_schedule  # noqa: E402
from scripts.fetch_zuuu_metar_taf import token_candidates  # noqa: E402


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # P10 owns its transport receipt/T1 archive.  Sharing the legacy raw
    # archive would dedupe a report first captured by the old chain and make
    # this direct source unable to publish its independently observed arrival.
    parser.add_argument("--raw-archive-dir", default=str(ROOT / "data" / "shadow" / "p10_amsc_raw"))
    parser.add_argument("--inbox-dir", default=str(ROOT / "data" / "shadow" / "p10_amsc_inbox"))
    parser.add_argument("--status-path", default=str(ROOT / "data" / "shadow" / "p10_amsc_source_status_latest.json"))
    parser.add_argument("--report-count", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=int, default=3)
    parser.add_argument("--attempts", type=int, default=60)
    parser.add_argument("--interval-ms", type=int, default=500)
    args = parser.parse_args(argv)
    raw_archive_dir = str(Path(args.raw_archive_dir).expanduser().resolve())
    if args.report_count <= 0 or args.timeout_seconds <= 0 or args.attempts <= 0 or not 50 <= args.interval_ms <= 5000:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": ["amsc_p10_sidecar_arguments_invalid"],
            "raw_archive_dir": raw_archive_dir,
            "shadow_only": True,
            "places_orders": False,
        }
        _emit(result)
        return 2
    schedule = assess_zuuu_regular_metar_schedule(_utc_now_iso())
    if schedule.get("scheduled_regular_metar_slot") is not True:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "skipped_unscheduled_regular_metar_slot",
            "blockers": [],
            "raw_archive_dir": raw_archive_dir,
            "schedule": schedule,
            "shadow_only": True,
            "places_orders": False,
        }
        _atomic_json(Path(args.status_path).expanduser().resolve(), result)
        _emit(result)
        return 0
    tokens = token_candidates()
    if not tokens:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": ["amsc_p10_sidecar_session_unavailable"],
            "raw_archive_dir": raw_archive_dir,
            "shadow_only": True,
            "places_orders": False,
        }
        _atomic_json(Path(args.status_path).expanduser().resolve(), result)
        _emit(result)
        return 1
    last: dict[str, Any] = {}
    for attempt in range(1, args.attempts + 1):
        result: dict[str, Any]
        fetch_failure_types: list[str] = []
        for token in tokens:
            try:
                payload, transport = fetch_amsc_with_observed_transport(
                    token,
                    count=args.report_count,
                    timeout_seconds=args.timeout_seconds,
                )
                result = capture_amsc_p10_shadow_source_attempt(
                    payload=payload,
                    transport=transport,
                    raw_archive_dir=raw_archive_dir,
                    inbox_dir=args.inbox_dir,
                    report_count=args.report_count,
                )
                break
            except Exception as exc:
                # Do not emit the URL, headers, token or provider response.
                # Exception classes are enough to distinguish session, TLS,
                # timeout and parser failures in the launchd status record.
                failure = type(exc).__name__
                reason = getattr(exc, "reason", None)
                if reason is not None:
                    failure = f"{failure}:{type(reason).__name__}"
                fetch_failure_types.append(failure)
                result = {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "source_fetch_failed",
                    "blockers": [f"amsc_p10_sidecar_fetch_failed:{failure}"],
                    "raw_archive_dir": raw_archive_dir,
                    "shadow_only": True,
                    "places_orders": False,
                }
        else:  # pragma: no cover - tokens loop always executes when non-empty.
            result = {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "source_fetch_failed",
                "blockers": [
                    "amsc_p10_sidecar_all_token_attempts_failed"
                    + (":" + ",".join(sorted(set(fetch_failure_types))) if fetch_failure_types else "")
                ],
                "raw_archive_dir": raw_archive_dir,
                "shadow_only": True,
                "places_orders": False,
            }
        result.update({"attempt": attempt, "attempted_at": _utc_now_iso()})
        last = result
        _atomic_json(Path(args.status_path).expanduser().resolve(), result)
        _emit(result)
        if result.get("state") in {"published", "ready_without_new_primary"}:
            return 0
        if attempt < args.attempts:
            time.sleep(args.interval_ms / 1000.0)
    return 1 if last else 2


if __name__ == "__main__":
    raise SystemExit(main())
