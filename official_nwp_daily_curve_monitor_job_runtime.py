"""Low-frequency monitor-child plan for prospective official NWP daily curves."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from official_nwp_daily_curve_collector_runtime import Collector, collect_official_daily_curve
from official_nwp_daily_curve_plan_runtime import load_daily_curve_run_contract
from scripts.persist_official_nwp_daily_peak_revision import _atomic_immutable_json, build_contract


SCHEMA_VERSION = "official_nwp_daily_curve_monitor_job_result_v1"
JOB_SCHEMA_VERSION = "official_nwp_daily_curve_monitor_job_v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def load_monitor_job_contract(path: Path) -> dict[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def select_latest_plannable_run(*, curve_contract: Mapping[str, Any], provider_id: str, target_event_date: str, now_utc: datetime) -> str:
    """Choose only a completed-or-earlier nominal run; transport still verifies reality."""

    value = _mapping(curve_contract)
    provider = _mapping(_mapping(value.get("providers")).get(provider_id))
    hours = [int(item) for item in provider.get("allowed_run_hours_utc") or [] if str(item).isdigit()]
    try:
        zone = ZoneInfo(_text(value.get("target_timezone")))
        target_start = datetime.fromisoformat(f"{target_event_date}T00:00:00").replace(tzinfo=zone).astimezone(timezone.utc)
    except Exception:
        return ""
    ceiling = min(now_utc.astimezone(timezone.utc), target_start)
    candidates: list[datetime] = []
    for days_back in range(0, 4):
        day = (ceiling - timedelta(days=days_back)).date()
        for hour in hours:
            candidate = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
            if candidate <= ceiling:
                candidates.append(candidate)
    return max(candidates).isoformat().replace("+00:00", "Z") if candidates else ""


def select_plannable_run_candidates(
    *, curve_contract: Mapping[str, Any], provider_id: str, target_event_date: str, now_utc: datetime, limit: int
) -> list[str]:
    """Return newest-first declared runs eligible for one transport fallback.

    Calendar eligibility is only a candidate list.  A collector must still
    verify the actual official transport; a fallback produces a complete
    curve from one older immutable run and never mixes points across runs.
    """
    value = _mapping(curve_contract)
    provider = _mapping(_mapping(value.get("providers")).get(provider_id))
    hours = [int(item) for item in provider.get("allowed_run_hours_utc") or [] if str(item).isdigit()]
    try:
        zone = ZoneInfo(_text(value.get("target_timezone")))
        target_start = datetime.fromisoformat(f"{target_event_date}T00:00:00").replace(tzinfo=zone).astimezone(timezone.utc)
    except Exception:
        return []
    ceiling = min(now_utc.astimezone(timezone.utc), target_start)
    candidates: list[datetime] = []
    for days_back in range(0, 4):
        day = (ceiling - timedelta(days=days_back)).date()
        for hour in hours:
            candidate = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
            if candidate <= ceiling:
                candidates.append(candidate)
    unique = sorted(set(candidates), reverse=True)
    return [candidate.isoformat().replace("+00:00", "Z") for candidate in unique[:max(1, int(limit))]]


def _persist_complete_daily_peak_revision(
    *, provider_id: str, run_at_utc: str, target_event_date: str, curve_source_root: Path, contract_path: Path
) -> dict[str, Any]:
    """Persist only a complete same-run curve as an immutable peak revision."""
    try:
        contract = build_contract(
            provider=provider_id,
            run_at_utc=run_at_utc,
            target_event_date=target_event_date,
            curve_root=curve_source_root,
            contract_path=contract_path,
        )
        if contract.get("ok") is not True:
            return {"ok": False, "state": "blocked", "blockers": list(contract.get("revision", {}).get("block_reasons") or contract.get("history_unreadable") or ["official_nwp_daily_peak_revision_not_ready"])}
        ok, action = _atomic_immutable_json(Path(str(contract["output_path"])), dict(contract["revision"]))
        return {"ok": ok, "state": action if ok else "blocked", "source_revision_id": contract["revision"].get("source_revision_id"), "forecast_peak_c": contract["revision"].get("forecast_peak_c"), "output_path": contract.get("output_path"), "blockers": [] if ok else [action]}
    except Exception as exc:
        return {"ok": False, "state": "blocked", "blockers": [f"official_nwp_daily_peak_revision_persist_failed:{type(exc).__name__}"]}


def build_official_nwp_daily_curve_monitor_job(
    *, job_contract: Mapping[str, Any], curve_contract: Mapping[str, Any], now_utc: datetime | None = None, execute: bool = False, curve_source_root: Path, catalog_path: Path, completed_provider_runs: Mapping[str, Any] | None = None, collectors: Mapping[str, Collector] | None = None
) -> dict[str, Any]:
    job = _mapping(job_contract)
    curve = _mapping(curve_contract)
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    blockers: list[str] = []
    if job.get("schema") != JOB_SCHEMA_VERSION:
        blockers.append("official_nwp_curve_monitor_job_contract_invalid")
    try:
        zone = ZoneInfo(_text(curve.get("target_timezone")))
        target_event_date = (now.astimezone(zone).date() + timedelta(days=int(job.get("target_day_offset") or 1))).isoformat()
    except Exception:
        target_event_date = ""
        blockers.append("official_nwp_curve_monitor_target_timezone_invalid")
    providers = [str(item) for item in job.get("providers") or [] if str(item)]
    if not providers:
        blockers.append("official_nwp_curve_monitor_providers_missing")
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": sorted(set(blockers)), "boundary": {"writes_posterior": False, "places_orders": False}}
    try:
        max_provider_runs = max(1, int(_mapping(job.get("execution_budget")).get("max_provider_runs_per_job") or 1))
    except (TypeError, ValueError):
        max_provider_runs = 1
    if len(providers) > max_provider_runs:
        start = now.hour % len(providers)
        providers = [providers[(start + offset) % len(providers)] for offset in range(max_provider_runs)]
    try:
        max_run_fallbacks = max(1, min(4, int(_mapping(job.get("execution_budget")).get("max_run_fallbacks") or 2)))
    except (TypeError, ValueError):
        max_run_fallbacks = 2
    run_candidates = {
        provider: select_plannable_run_candidates(
            curve_contract=curve,
            provider_id=provider,
            target_event_date=target_event_date,
            now_utc=now,
            limit=max_run_fallbacks,
        )
        for provider in providers
    }
    runs = {provider: (run_candidates[provider][0] if run_candidates[provider] else "") for provider in providers}
    planned = []
    completed = _mapping(completed_provider_runs)
    for provider in providers:
        candidates = run_candidates[provider]
        run_at_utc = candidates[0] if candidates else ""
        if not run_at_utc:
            planned.append({"provider_id": provider, "state": "blocked", "blockers": ["official_nwp_curve_monitor_plannable_run_missing"]})
            continue
        if execute and bool(job.get("one_run_per_provider_per_target_day")) and _text(completed.get(provider)) == run_at_utc:
            planned.append({"provider_id": provider, "run_at_utc": run_at_utc, "ok": True, "state": "already_collected", "execute": False, "blockers": []})
            continue
        attempts: list[dict[str, Any]] = []
        for candidate_run in candidates if execute else candidates[:1]:
            attempt = collect_official_daily_curve(
                contract=curve,
                provider_id=provider,
                run_at_utc=candidate_run,
                target_event_date=target_event_date,
                curve_source_root=curve_source_root,
                catalog_path=catalog_path,
                execute=execute,
                collectors=collectors,
                max_point_workers=int(_mapping(job.get("execution_budget")).get("max_parallel_point_collects") or 1),
            )
            attempts.append(attempt)
            if attempt.get("ok") is True:
                break
        final = dict(attempts[-1]) if attempts else {
            "provider_id": provider,
            "ok": False,
            "state": "blocked",
            "blockers": ["official_nwp_curve_monitor_plannable_run_missing"],
        }
        final["attempted_run_at_utc"] = [str(item.get("run_at_utc") or "") for item in attempts]
        final["fallback_run_used"] = bool(
            final.get("ok") is True
            and len(attempts) > 1
            and str(final.get("run_at_utc") or "") != run_at_utc
        )
        if execute and final.get("ok") is True:
            peak = _persist_complete_daily_peak_revision(
                provider_id=provider,
                run_at_utc=str(final.get("run_at_utc") or ""),
                target_event_date=target_event_date,
                curve_source_root=curve_source_root,
                contract_path=catalog_path.parent / "zuuu_official_daily_curve_run_contract_v1.json",
            )
            final["daily_peak_revision"] = peak
            if peak.get("ok") is not True:
                final["ok"] = False
                final["state"] = "partial_or_blocked"
                final["blockers"] = sorted(set([*list(final.get("blockers") or []), *list(peak.get("blockers") or [])]))
        planned.append(final)
    all_ok = all(item.get("ok") is True for item in planned)
    next_completed = dict(completed)
    for item in planned:
        if item.get("ok") is True and item.get("state") in {"collected", "already_collected"} and _text(item.get("provider_id")) and _text(item.get("run_at_utc")):
            next_completed[_text(item.get("provider_id"))] = _text(item.get("run_at_utc"))
    return {"schema": SCHEMA_VERSION, "ok": all_ok, "state": "executed" if execute and all_ok else "planned" if all_ok else "partial_or_blocked", "target_event_date": target_event_date, "now_utc": now.isoformat().replace("+00:00", "Z"), "execute": bool(execute), "provider_runs": runs, "provider_run_candidates": run_candidates, "provider_results": planned, "completed_provider_runs": next_completed, "boundary": {"low_frequency_monitor_child_only": True, "hot_path_network_acquisition": False, "run_fallback_uses_one_complete_older_official_run_only": True, "writes_archive_db": False, "writes_posterior": False, "places_orders": False}}


__all__ = ["JOB_SCHEMA_VERSION", "SCHEMA_VERSION", "build_official_nwp_daily_curve_monitor_job", "load_monitor_job_contract", "select_latest_plannable_run", "select_plannable_run_candidates"]
