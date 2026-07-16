#!/usr/bin/env python3
"""Reconcile a planned Open-Meteo replay with immutable rows and explicit NA."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "open_meteo_realized_coverage_audit_v1"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"
REQUIRED_SOURCES = {"ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"}


def build_realized_coverage_audit(
    *,
    coverage: Mapping[str, Any],
    fetch_report: Mapping[str, Any],
    forecast_snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    requests = [dict(row) for row in coverage.get("single_runs_exact_requests") or [] if isinstance(row, Mapping)]
    results = [dict(row) for row in fetch_report.get("results") or [] if isinstance(row, Mapping)]
    request_by_url = {str(row.get("url") or ""): row for row in requests}
    available_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in forecast_snapshots:
        if row.get("state") != "available":
            continue
        key = (str(row.get("target_event_date_local") or ""), str(row.get("cutoff_id") or ""))
        available_by_key[key].add(str(row.get("source_id") or ""))

    unavailable: list[dict[str, Any]] = []
    unmatched_result_count = 0
    for result in results:
        if result.get("write", {}).get("ok") is True:
            continue
        request = request_by_url.get(str(result.get("url") or ""))
        if request is None:
            unmatched_result_count += 1
            continue
        errors = [str(row.get("error") or "") for row in result.get("attempts") or [] if isinstance(row, Mapping)]
        all_http_400 = bool(errors) and all("HTTP Error 400" in value for value in errors)
        provider_unavailable = result.get("state_reason") == "provider_run_unavailable" or all_http_400
        unavailable.append(
            {
                "state": "unavailable",
                "state_reason": "provider_run_unavailable_http_400" if provider_unavailable else "provider_request_failed",
                "source_id": request.get("source_id"),
                "provider_family": request.get("provider_family"),
                "model": request.get("model"),
                "target_event_date_local": request.get("target_event_date_local"),
                "cutoff_id": request.get("cutoff_id"),
                "run": request.get("run"),
                "attempt_count": len(result.get("attempts") or []),
                "failure_evidence": errors,
            }
        )
    realized_triplets = sum(REQUIRED_SOURCES.issubset(sources) for sources in available_by_key.values())
    result_count = len(results)
    request_count = len(requests)
    success_count = sum(1 for row in results if row.get("write", {}).get("ok") is True)
    return {
        "schema": SCHEMA_VERSION,
        "source_data_policy": "external_only",
        "authorized": fetch_report.get("authorized") is True,
        "planned_request_count": request_count,
        "executed_result_count": result_count,
        "successful_immutable_envelope_count": success_count,
        "explicit_unavailable_count": len(unavailable),
        "request_accounting_complete": result_count == request_count and success_count + len(unavailable) == request_count,
        "unmatched_result_count": unmatched_result_count,
        "realized_available_event_cutoff_count": len(available_by_key),
        "realized_three_model_triplet_count": realized_triplets,
        "explicit_unavailable": unavailable,
        "availability_policy": {
            "provider_unavailable_is_not_model_error": True,
            "provider_unavailable_is_not_imputed": True,
            "provider_unavailable_never_receives_zero_score": True,
            "date_specific_exceptions": False,
        },
        "guards": {
            "reads_existing_local_history": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("realized_coverage_output_outside_research_root")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--fetch-report", type=Path, required=True)
    parser.add_argument("--forecast-snapshots", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    snapshots_payload = _load(args.forecast_snapshots)
    snapshots = snapshots_payload.get("forecast_snapshots") if isinstance(snapshots_payload, Mapping) else snapshots_payload
    if not isinstance(snapshots, list):
        raise ValueError("forecast_snapshots_missing")
    report = build_realized_coverage_audit(
        coverage=_load(args.coverage),
        fetch_report=_load(args.fetch_report),
        forecast_snapshots=snapshots,
    )
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = _safe_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["request_accounting_complete"] and not report["unmatched_result_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
