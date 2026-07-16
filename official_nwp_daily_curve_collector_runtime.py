"""Controlled, isolated collector orchestration for official daily curves."""

from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from official_nwp_daily_curve_plan_runtime import build_official_nwp_daily_curve_plan


SCHEMA_VERSION = "official_nwp_daily_curve_collector_run_v1"
Collector = Callable[..., Mapping[str, Any]]

# The NOMADS filter rounds the requested area to the 0.25° GFS grid.  A
# station-centred window narrower than a cell can therefore yield a one-cell
# payload whose coordinate is *outside* ZUUU; ecCodes then correctly refuses
# its nearest-point query.  Keep a whole-cell margin on every side instead of
# silently treating that decoder failure as a missing forecast source.
_GFS_FILTER_BOUNDS = {
    "leftlon": "103.50",
    "rightlon": "104.50",
    "toplat": "31.00",
    "bottomlat": "30.00",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: Any) -> datetime:
    return datetime.fromisoformat(_text(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def build_nomads_gfs_hourly_url(*, run_at_utc: str, valid_time_utc: str) -> str:
    """Build one official NOMADS GFS 0.25° hourly-filter request."""

    run, valid = _time(run_at_utc), _time(valid_time_utc)
    lead_seconds = (valid - run).total_seconds()
    if lead_seconds < 0 or lead_seconds % 3600:
        raise ValueError("gfs_curve_run_valid_time_invalid")
    lead = int(lead_seconds // 3600)
    query = {
        "file": f"gfs.t{run:%H}z.pgrb2.0p25.f{lead:03d}",
        "lev_2_m_above_ground": "on",
        "lev_850_mb": "on",
        "lev_700_mb": "on",
        "lev_500_mb": "on",
        "var_TMP": "on",
        "var_RH": "on",
        "var_HGT": "on",
        "var_UGRD": "on",
        "var_VGRD": "on",
        "subregion": "",
        **_GFS_FILTER_BOUNDS,
        "dir": f"/gfs.{run:%Y%m%d}/{run:%H}/atmos",
    }
    return "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl?" + urlencode(query)


def _default_collectors() -> dict[str, Collector]:
    from scripts.collect_dwd_icon_zuuu import collect as collect_icon
    from scripts.collect_ecmwf_ifs_zuuu import collect as collect_ecmwf
    from scripts.collect_official_nwp_snapshot import collect as collect_gfs

    def ecmwf(**kwargs: Any) -> Mapping[str, Any]:
        kwargs.pop("timeout_sec", None)
        return collect_ecmwf(**kwargs)

    def gfs(**kwargs: Any) -> Mapping[str, Any]:
        return collect_gfs(
            provider_id="gfs",
            raw_urls=[build_nomads_gfs_hourly_url(run_at_utc=kwargs["run_at_utc"], valid_time_utc=kwargs["valid_time_utc"])],
            **kwargs,
        )

    return {"ecmwf": ecmwf, "icon": collect_icon, "gfs": gfs}


def _history_point_already_collected(*, curve_source_root: Path, provider_id: str, run_at_utc: str, valid_time_utc: str) -> bool:
    """Reuse only an immutable normalized point with exact provider/run/valid identity."""

    history = Path(curve_source_root) / provider_id / "history"
    for path in history.glob("*.json"):
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(snapshot, Mapping):
            continue
        if (
            snapshot.get("schema") == "official_nwp_normalized_snapshot_v1"
            and _text(snapshot.get("provider_id")) == provider_id
            and _text(snapshot.get("run_at_utc")) == run_at_utc
            and _text(snapshot.get("valid_time_utc")) == valid_time_utc
            and _text(snapshot.get("raw_sha256"))
            and _text(snapshot.get("snapshot_sha256"))
        ):
            return True
    return False


def collect_official_daily_curve(
    *, contract: Mapping[str, Any], provider_id: str, run_at_utc: str, target_event_date: str, curve_source_root: Path, catalog_path: Path, timeout_sec: float = 45.0, execute: bool = False, collectors: Mapping[str, Collector] | None = None, now_utc: datetime | None = None, max_point_workers: int = 1
) -> dict[str, Any]:
    """Plan and optionally collect a complete provider curve outside hot paths."""

    plan = build_official_nwp_daily_curve_plan(
        contract=contract, provider_id=provider_id, run_at_utc=run_at_utc, target_event_date=target_event_date
    )
    root = Path(curve_source_root).expanduser().resolve()
    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "provider_id": _text(provider_id),
        "run_at_utc": _text(run_at_utc),
        "target_event_date": _text(target_event_date)[:10],
        "plan": plan,
        "curve_source_root": str(root),
        "execute": bool(execute),
        "boundary": {
            "isolated_from_hot_prewarm_root": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "writes_task_queue": False,
            "places_orders": False,
        },
    }
    if plan.get("ok") is not True:
        return {**result, "ok": False, "state": "blocked", "blockers": list(plan.get("blockers") or []), "point_results": []}
    if not execute:
        return {**result, "ok": True, "state": "planned", "blockers": [], "point_results": []}
    try:
        from zoneinfo import ZoneInfo

        current_target_day = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(str(contract.get("target_timezone") or ""))).date().isoformat()
    except Exception:
        return {**result, "ok": False, "state": "blocked", "blockers": ["official_nwp_daily_curve_target_timezone_invalid_at_execute"], "point_results": []}
    if _text(target_event_date)[:10] < current_target_day:
        return {**result, "ok": False, "state": "blocked", "blockers": ["official_nwp_daily_curve_historical_target_execute_forbidden"], "point_results": []}
    adapter = (collectors or _default_collectors()).get(_text(provider_id))
    if adapter is None:
        return {**result, "ok": False, "state": "blocked", "blockers": ["official_nwp_daily_curve_provider_adapter_unavailable"], "point_results": []}
    point_by_valid: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for valid_time_utc in plan["expected_valid_times_utc"]:
        if _history_point_already_collected(
            curve_source_root=root,
            provider_id=_text(provider_id),
            run_at_utc=plan["run_at_utc"],
            valid_time_utc=valid_time_utc,
        ):
            point_by_valid[valid_time_utc] = {"ok": True, "state": "already_collected", "valid_time_utc": valid_time_utc, "blockers": []}
            continue
        pending.append(valid_time_utc)

    def collect_one(valid_time_utc: str) -> dict[str, Any]:
        try:
            item = dict(adapter(run_at_utc=plan["run_at_utc"], valid_time_utc=valid_time_utc, source_root=root, catalog_path=Path(catalog_path), timeout_sec=float(timeout_sec)))
        except Exception as exc:
            item = {"ok": False, "state": "blocked", "blockers": [f"official_nwp_daily_curve_adapter_exception:{type(exc).__name__}"]}
        item["valid_time_utc"] = valid_time_utc
        return item

    workers = max(1, min(4, int(max_point_workers)))
    if workers == 1 or len(pending) < 2:
        for valid_time_utc in pending:
            point_by_valid[valid_time_utc] = collect_one(valid_time_utc)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(collect_one, valid): valid for valid in pending}
            for future in as_completed(futures):
                valid_time_utc = futures[future]
                try:
                    point_by_valid[valid_time_utc] = future.result()
                except Exception as exc:
                    point_by_valid[valid_time_utc] = {"ok": False, "state": "blocked", "valid_time_utc": valid_time_utc, "blockers": [f"official_nwp_daily_curve_worker_exception:{type(exc).__name__}"]}
    point_results = [point_by_valid[valid] for valid in plan["expected_valid_times_utc"]]
    blockers = [reason for item in point_results if item.get("ok") is not True for reason in item.get("blockers") or ["official_nwp_daily_curve_point_collect_failed"]]
    return {**result, "ok": not blockers, "state": "collected" if not blockers else "partial_or_blocked", "blockers": sorted(set(blockers)), "point_results": point_results}


__all__ = ["SCHEMA_VERSION", "build_nomads_gfs_hourly_url", "collect_official_daily_curve"]
