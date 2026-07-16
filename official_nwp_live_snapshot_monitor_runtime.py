"""Low-frequency official NWP live-snapshot collector and prewarm publisher.

This is deliberately separate from the immutable daily-curve collector.  It
captures one declared peak-anchor valid time per provider into the live source
root, then lets the existing no-network prewarm publisher freeze the available
source mask.  It is never an AMSC/posterior/order hot-path dependency.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from official_nwp_daily_curve_collector_runtime import _default_collectors
from official_nwp_daily_curve_monitor_job_runtime import select_plannable_run_candidates
from official_nwp_multimodel_prewarm import build_official_nwp_prewarm_plan, persist_official_nwp_prewarm


SCHEMA_VERSION = "official_nwp_live_snapshot_monitor_result_v1"
JOB_SCHEMA_VERSION = "official_nwp_live_snapshot_monitor_job_v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _target_anchor_valid_time(*, target_event_date: str, timezone_name: str, local_hour: int) -> str:
    zone = ZoneInfo(timezone_name)
    local = datetime.fromisoformat(f"{target_event_date}T00:00:00").replace(tzinfo=zone)
    return local.replace(hour=int(local_hour), minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_official_nwp_live_snapshot_monitor_job(
    *, job_contract: Mapping[str, Any], curve_contract: Mapping[str, Any], now_utc: datetime | None,
    source_root: Path, catalog_path: Path, prewarm_cache: Path, execute: bool,
) -> dict[str, Any]:
    """Collect at most one source revision, then atomically publish its mask."""
    job, curve = _mapping(job_contract), _mapping(curve_contract)
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if job.get("schema") != JOB_SCHEMA_VERSION:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["official_nwp_live_snapshot_job_contract_invalid"]}
    timezone_name = _text(job.get("target_timezone") or curve.get("target_timezone"))
    try:
        zone = ZoneInfo(timezone_name)
        target_date = (now.astimezone(zone).date() + timedelta(days=int(job.get("target_day_offset") or 1))).isoformat()
        valid_time = _target_anchor_valid_time(target_event_date=target_date, timezone_name=timezone_name, local_hour=int(job.get("target_peak_anchor_local_hour")))
    except Exception:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["official_nwp_live_snapshot_target_anchor_invalid"]}
    providers = [str(item) for item in job.get("providers") or [] if str(item)]
    if not providers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["official_nwp_live_snapshot_providers_missing"]}
    max_provider_runs = max(1, min(len(providers), int(_mapping(job.get("execution_budget")).get("max_provider_runs_per_job") or 1)))
    # A low-frequency job may run several times in the same UTC hour. Hour
    # modulo would choose the same provider every time while the freshness
    # window expires the others. Rotate on the declared collection slot.
    collection_interval_sec = max(60, int(job.get("collection_interval_sec") or 900))
    snapshot_max_age_sec = max(collection_interval_sec, int(job.get("snapshot_max_age_sec") or 3600))
    start = int(now.timestamp() // collection_interval_sec) % len(providers)
    selected = [providers[(start + offset) % len(providers)] for offset in range(max_provider_runs)]
    max_fallbacks = max(1, min(4, int(_mapping(job.get("execution_budget")).get("max_run_fallbacks") or 2)))
    collectors = _default_collectors()
    results: list[dict[str, Any]] = []
    for provider in selected:
        candidates = select_plannable_run_candidates(
            curve_contract=curve, provider_id=provider, target_event_date=target_date, now_utc=now, limit=max_fallbacks
        )
        attempts: list[dict[str, Any]] = []
        for run_at in candidates[:1] if not execute else candidates:
            if not execute:
                attempts.append({"ok": True, "state": "planned", "run_at_utc": run_at})
                break
            adapter = collectors.get(provider)
            if adapter is None:
                attempts.append({"ok": False, "state": "blocked", "run_at_utc": run_at, "blockers": ["official_nwp_live_snapshot_adapter_missing"]})
                break
            try:
                result = dict(adapter(run_at_utc=run_at, valid_time_utc=valid_time, source_root=source_root, catalog_path=catalog_path, timeout_sec=45.0))
            except Exception as exc:
                result = {"ok": False, "state": "blocked", "blockers": [f"official_nwp_live_snapshot_adapter_exception:{type(exc).__name__}"]}
            result["run_at_utc"] = run_at
            attempts.append(result)
            if result.get("ok") is True:
                break
        final = dict(attempts[-1]) if attempts else {"ok": False, "state": "blocked", "blockers": ["official_nwp_live_snapshot_plannable_run_missing"]}
        final.update({"provider_id": provider, "valid_time_utc": valid_time, "attempted_run_at_utc": [str(item.get("run_at_utc") or "") for item in attempts]})
        results.append(final)
    # `as_of` belongs to the prewarm publication, not the monitor planning
    # instant.  A snapshot captured in this run must be visible to the
    # subsequent publisher; using the earlier planning clock falsely labels
    # its completed receipt as future data and emits an empty source mask.
    prewarm_as_of = datetime.now(timezone.utc) if execute else now
    prewarm_plan = build_official_nwp_prewarm_plan(
        catalog_path=catalog_path,
        source_root=source_root,
        cache_path=prewarm_cache,
        as_of=prewarm_as_of,
        max_snapshot_age_sec=snapshot_max_age_sec,
    )
    prewarm = persist_official_nwp_prewarm(plan=prewarm_plan) if execute and prewarm_plan.get("ok") is True else {"ok": prewarm_plan.get("ok") is True, "state": "planned" if not execute else "blocked", "blockers": list(prewarm_plan.get("blockers") or [])}
    return {
        "schema": SCHEMA_VERSION, "ok": bool(prewarm.get("ok")), "state": "executed" if execute and prewarm.get("ok") else "planned" if not execute else "partial_or_blocked",
        "target_event_date": target_date, "target_peak_anchor_valid_time_utc": valid_time,
        "collection_interval_sec": collection_interval_sec, "snapshot_max_age_sec": snapshot_max_age_sec,
        "provider_results": results, "prewarm": prewarm,
        "boundary": {"low_frequency_monitor_child_only": True, "curve_root_unused": True, "hot_path_network_acquisition": False, "writes_archive_db": False, "writes_posterior": False, "places_orders": False},
    }


__all__ = ["JOB_SCHEMA_VERSION", "SCHEMA_VERSION", "build_official_nwp_live_snapshot_monitor_job"]
