#!/usr/bin/env python3
"""Bounded Open-Meteo historical forecast fetcher with immutable envelopes.

Default mode only validates and prints the request plan. Historical network
fetch and research writes require ``--fetch`` plus ``--allow-research-write``
and remain bounded by ``--max-requests``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from urllib.error import HTTPError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from scripts.forecast_source_forward_snapshot import DEFAULT_RESEARCH_RAW_ROOT, write_forward_snapshot


SCHEMA_VERSION = "open_meteo_source_skill_fetch_report_v1"
CONFIRMATION = "FETCH_CONFIRMED_OPEN_METEO_SOURCE_SKILL_HISTORY"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _default_fetch(url: str, timeout_seconds: float) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "weather-source-skill-research/1.0"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return int(response.status), response.read().decode("utf-8")


def _fetch_error_evidence(exc: Exception) -> dict[str, Any]:
    evidence: dict[str, Any] = {"error_type": type(exc).__name__, "error": str(exc)}
    if isinstance(exc, HTTPError):
        evidence["http_status"] = int(exc.code)
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if body:
            evidence["response_body_excerpt"] = body[:1000]
        lowered = body.lower()
        evidence["provider_run_unavailable"] = bool(
            int(exc.code) == 400
            and "requested model run is not available" in lowered
        )
    return evidence


def execute_request_plan(
    requests: Sequence[Mapping[str, Any]],
    *,
    fetch: bool = False,
    confirmation: str = "",
    allow_research_write: bool = False,
    max_requests: int = 20,
    timeout_seconds: float = 30.0,
    output_root: Path = DEFAULT_RESEARCH_RAW_ROOT,
    fetcher: Callable[[str, float], tuple[int, str]] = _default_fetch,
    max_retries: int = 2,
    min_interval_seconds: float = 0.25,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    rows = [dict(row) for row in requests]
    issues = []
    if len(rows) > max(0, int(max_requests)):
        issues.append("request_count_exceeds_explicit_bound")
    for index, row in enumerate(rows):
        if not str(row.get("url") or "").startswith("https://"):
            issues.append(f"request_{index}_https_url_missing")
        if not str(row.get("source_id") or ""):
            issues.append(f"request_{index}_source_id_missing")
    authorized = bool(fetch and allow_research_write and not issues)
    results: list[dict[str, Any]] = []
    if authorized:
        for request_index, row in enumerate(rows):
            if request_index > 0 and min_interval_seconds > 0:
                sleep_fn(float(min_interval_seconds))
            requested_at = _now()
            started_at = _now()
            attempts = []
            status = None
            body = None
            for attempt in range(1, max(0, int(max_retries)) + 2):
                try:
                    status, body = fetcher(str(row["url"]), float(timeout_seconds))
                    attempts.append({"attempt": attempt, "ok": True, "http_status": status})
                    break
                except Exception as exc:
                    evidence = {"attempt": attempt, "ok": False, **_fetch_error_evidence(exc)}
                    attempts.append(evidence)
                    if evidence.get("provider_run_unavailable") is True:
                        break
                    if attempt <= max(0, int(max_retries)):
                        sleep_fn(min(5.0, 0.5 * (2 ** (attempt - 1))))
            try:
                if status is None or body is None:
                    raise RuntimeError("open_meteo_request_retries_exhausted")
                completed_at = _now()
                envelope = build_external_forecast_raw_envelope(
                    source_id=str(row["source_id"]),
                    provider_family=str(row.get("provider_family") or ""),
                    response_body_utf8=body,
                    requested_at=requested_at,
                    response_started_at=started_at,
                    response_completed_at=completed_at,
                    http_status=status,
                    parser_version="open_meteo_source_skill_fetch_v1",
                    request_url=str(row["url"]),
                    request_parameters={
                        key: row.get(key)
                        for key in (
                            "model",
                            "target_event_date_local",
                            "cutoff_id",
                            "cutoff_at",
                            "run",
                            "run_initialized_at",
                            "provider_available_at_conservative",
                            "availability_quality",
                            "eligible_for_exact_cutoff_pairing",
                            "evidence_tier",
                        )
                        if row.get(key) is not None
                    },
                    provider_available_at=row.get("provider_available_at_conservative"),
                    source_event_id=str(
                        row.get("source_event_id")
                        or f"open_meteo_{row['source_id']}_{row.get('target_event_date_local') or row.get('start_date')}_{row.get('run') or row.get('end_date')}"
                    ),
                    availability_mode=(
                        "historical_archive_replay"
                        if row.get("provider_available_at_conservative")
                        else "live_fetch"
                    ),
                    provider_available_at_quality=str(row.get("availability_quality") or ""),
                )
                write = write_forward_snapshot(envelope, output_root=output_root, allow_research_write=True)
                results.append({"source_id": row["source_id"], "url": row["url"], "http_status": status, "attempts": attempts, "write": write})
            except Exception as exc:
                provider_unavailable = any(item.get("provider_run_unavailable") is True for item in attempts)
                results.append(
                    {
                        "source_id": row.get("source_id"),
                        "url": row.get("url"),
                        "attempts": attempts,
                        "state": "unavailable" if provider_unavailable else "failed",
                        "state_reason": "provider_run_unavailable" if provider_unavailable else "request_retries_exhausted",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
    success_count = sum(1 for row in results if row.get("write", {}).get("ok") is True)
    return {
        "schema": SCHEMA_VERSION,
        "mode": "fetch_and_research_write" if authorized else "dry_run_no_network_no_write",
        "authorized": authorized,
        "request_count": len(rows),
        "max_requests": int(max_requests),
        "max_retries": int(max_retries),
        "min_interval_seconds": float(min_interval_seconds),
        "issues": sorted(set(issues)),
        "requests": rows,
        "results": results,
        "success_count": success_count,
        "failure_count": len(results) - success_count,
        "confirmation": {
            "fetch_flag": bool(fetch),
            "confirmation_phrase_matched": confirmation == CONFIRMATION,
            "confirmation_phrase_required": False,
            "research_write_allowed": bool(allow_research_write),
        },
        "guards": {
            "downloads_historical_forecasts": authorized,
            "writes_research_files": authorized,
            "reads_existing_local_history": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "next_boundary": "normalize_downloaded_envelopes" if authorized else "explicit_bounded_fetch_and_research_write_flags_required",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-manifest", required=True, type=Path)
    parser.add_argument("--request-set", choices=("single_runs_exact", "previous_runs"), default="single_runs_exact")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--allow-research-write", action="store_true")
    parser.add_argument("--max-requests", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--min-interval-seconds", type=float, default=0.25)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_RESEARCH_RAW_ROOT)
    args = parser.parse_args(argv)
    manifest = json.loads(args.coverage_manifest.read_text(encoding="utf-8"))
    request_key = "single_runs_exact_requests" if args.request_set == "single_runs_exact" else "previous_runs_requests"
    requests = manifest.get(request_key) if isinstance(manifest, Mapping) else None
    if not isinstance(requests, list):
        raise ValueError("coverage_manifest_previous_runs_requests_missing")
    report = execute_request_plan(
        requests,
        fetch=args.fetch,
        confirmation=args.confirmation,
        allow_research_write=args.allow_research_write,
        max_requests=args.max_requests,
        timeout_seconds=args.timeout_seconds,
        output_root=args.output_root,
        max_retries=args.max_retries,
        min_interval_seconds=args.min_interval_seconds,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not report["issues"] and (not args.fetch or report["authorized"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
