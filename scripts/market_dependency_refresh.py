#!/usr/bin/env python3
"""Refresh every dependency named by the latest weather-market discovery.

The market scanner owns discovery; this child owns dependency acquisition and
materialization.  It is intentionally lease-protected and idempotent so a
one-minute scan can safely submit the same signal repeatedly.  Network
collection stays outside the CLOB/AMSC/AWC hot path and every failed provider
is recorded instead of being treated as complete.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIGNAL = ROOT / "data" / "weather_discovery_new_market_signal.json"
DEFAULT_STATE = ROOT / "data" / "runtime" / "market_dependency_refresh" / "state_latest.json"
DEFAULT_STATUS = ROOT / "data" / "runtime" / "market_dependency_refresh" / "status_latest.json"
DEFAULT_MODELS = ("gfs_seamless", "icon_seamless", "ecmwf_ifs025", "nws_forecast")
# GFS remains observable as an optional research/release source, but its
# known cold bias must not gate or feed the production basket bucket set.
DEFAULT_PROVIDERS = ("ecmwf", "icon")
TARGET_TZ = ZoneInfo("Asia/Shanghai")
SCHEMA = "market_dependency_refresh_v1"
MODEL_PROVIDER = {
    "ecmwf_ifs025": "ecmwf",
    "icon_seamless": "icon",
    "gfs_seamless": "gfs",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _mapping(value)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_day(value: Any) -> date | None:
    raw = _text(value).split("T", 1)[0]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _city_from_slug(slug: str) -> str:
    lower = _text(slug).lower()
    if "-in-" not in lower or "-on-" not in lower:
        return ""
    return lower.split("-in-", 1)[1].split("-on-", 1)[0].strip("-")


def _market_records(signal: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in signal.get("markets") or []:
        row = _mapping(item)
        slug = _text(row.get("slug"))
        event_day = _parse_day(row.get("event_date"))
        if not slug or event_day is None or (slug, event_day.isoformat()) in seen:
            continue
        seen.add((slug, event_day.isoformat()))
        city = _text(row.get("city")) or _city_from_slug(slug)
        station_id = _text(row.get("station_id")) or ("ZUUU" if city == "chengdu" else "")
        models = [m for m in (_text(v) for v in row.get("required_models") or DEFAULT_MODELS) if m]
        sources = [s for s in (_text(v) for v in row.get("required_sources") or ("awc", *DEFAULT_PROVIDERS, "nws")) if s]
        # A source lane is geographic.  Preserve model sources, but never
        # attach China's AMSC lane to a non-China market or AWC to ZUUU.
        sources = [source for source in sources if source not in {"amsc", "awc"}]
        sources.append("amsc" if station_id == "ZUUU" else "awc")
        records.append({
            "slug": slug,
            "event_date": event_day.isoformat(),
            "city": city,
            "station_id": station_id,
            "market_type": _text(row.get("market_type")) or "temperature",
            "required_models": sorted(set(models)),
            "required_sources": sorted(set(sources)),
        })
    records.sort(key=lambda row: (row["event_date"], row["slug"]))
    return records


def _target_days(signal: Mapping[str, Any], markets: Sequence[Mapping[str, Any]], now: datetime) -> list[str]:
    days = {_text(row.get("event_date"))[:10] for row in markets if _parse_day(row.get("event_date")) is not None}
    window = _mapping(signal.get("window"))
    for key in ("start", "end"):
        parsed = _parse_day(window.get(key))
        if parsed is not None:
            days.add(parsed.isoformat())
    today = now.astimezone(TARGET_TZ).date()
    return sorted(day for day in days if today <= date.fromisoformat(day) <= today + timedelta(days=3))


def _stable_release_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove probe clocks; retain only an actual official-file generation."""
    models: list[dict[str, Any]] = []
    for item in payload.get("models") or []:
        row = _mapping(item)
        models.append({
            "model": _text(row.get("model")),
            "event_date": _text(row.get("event_date"))[:10],
            "status": _text(row.get("status")),
            "complete_for_chengdu_peak": row.get("complete_for_chengdu_peak"),
            "latest_file_last_modified": _text(row.get("latest_file_last_modified")),
            "available_forecast_hours": list(row.get("available_forecast_hours") or []),
            "missing_forecast_hours": list(row.get("missing_forecast_hours") or []),
        })
    models.sort(key=lambda row: (row["event_date"], row["model"]))
    return {"event_date": _text(payload.get("event_date"))[:10], "models": models}


def _release_identity(root: Path, target_days: Sequence[str]) -> dict[str, Any]:
    entries = []
    for event_day in target_days:
        path = root / "data" / "runtime" / "market_dependency_refresh" / f"model_release_{event_day}.json"
        payload = _read_json(path)
        entries.append(_stable_release_identity(payload) if payload else {"event_date": event_day, "models": []})
    global_payload = _read_json(root / "data" / "model_release_watcher_latest.json")
    if global_payload:
        entries.append({"global": _stable_release_identity(global_payload)})
    return {"entries": entries, "hash": _canonical_hash(entries)}


def _curve_revision_identity(root: Path, target_days: Sequence[str]) -> dict[str, Any]:
    """Fingerprint immutable daily-peak revisions, not probe wall clocks."""

    wanted = {str(day)[:10] for day in target_days}
    rows: list[dict[str, Any]] = []
    curve_root = root / "data" / "runtime" / "official_nwp_curve_sources_v2"
    for provider in DEFAULT_PROVIDERS:
        revision_root = curve_root / provider / "daily_peak_revisions"
        for path in sorted(revision_root.glob("*.json")):
            payload = _read_json(path)
            event_day = _text(payload.get("event_date"))[:10]
            if event_day not in wanted:
                continue
            rows.append({
                "provider": provider,
                "event_date": event_day,
                "source_revision_id": _text(payload.get("source_revision_id")),
                "available_at": _text(payload.get("available_at")),
                "raw_sha256": _text(payload.get("raw_sha256")),
            })
    rows.sort(key=lambda row: (row["event_date"], row["provider"], row["available_at"], row["source_revision_id"]))
    return {"rows": rows, "hash": _canonical_hash(rows)}


def _ready_provider_day_offsets(
    *, release: Mapping[str, Any], target_days: Sequence[str], today: date,
) -> dict[str, list[str]]:
    """Return providers whose official peak-window release is complete.

    A market scan can discover a future market before a provider has published
    the relevant run. The old worker treated any release-file change as a
    green light and started the full curve collector, repeatedly downloading
    not-yet-published files. This map keeps the trigger automatic while making
    source readiness the explicit gate for payload acquisition.
    """

    by_day: dict[str, dict[str, Any]] = {}
    for item in release.get("entries") or []:
        row = (
            _mapping(item.get("global"))
            if isinstance(item, Mapping) and item.get("global")
            else _mapping(item)
        )
        event_day = _text(row.get("event_date"))[:10]
        # Event-specific release files are authoritative for their day. The
        # legacy global watcher file may be older (or for a different day)
        # and must not overwrite a complete target-day result.
        if event_day and event_day not in by_day:
            by_day[event_day] = row
    ready: dict[str, list[str]] = {}
    for event_day in target_days:
        parsed = _parse_day(event_day)
        if parsed is None:
            continue
        offset = (parsed - today).days
        if offset < 0 or offset > 3:
            continue
        providers: set[str] = set()
        for model in _mapping(by_day.get(event_day)).get("models") or []:
            model_row = _mapping(model)
            if (
                _text(model_row.get("status")) == "complete"
                or model_row.get("complete_for_chengdu_peak") is True
            ):
                provider = MODEL_PROVIDER.get(_text(model_row.get("model")))
                if provider:
                    providers.add(provider)
        ready[str(offset)] = sorted(providers)
    return ready


def build_dependency_plan(
    *, signal: Mapping[str, Any], state: Mapping[str, Any] | None = None, root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    previous = _mapping(state)
    markets = _market_records(signal)
    target_days = _target_days(signal, markets, current)
    models = sorted({model for row in markets for model in row.get("required_models") or DEFAULT_MODELS}) or list(DEFAULT_MODELS)
    nwp_markets = [row for row in markets if _text(row.get("station_id")) == "ZUUU"]
    providers = [provider for provider in DEFAULT_PROVIDERS if provider in {source for row in nwp_markets for source in row.get("required_sources") or ()}]
    if nwp_markets and not providers:
        providers = list(DEFAULT_PROVIDERS)
    market_generation = _canonical_hash(markets)
    release = _release_identity(root, target_days)
    curve_revisions = _curve_revision_identity(root, target_days)
    catalog_hash = _file_hash(root / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    source_generation = _canonical_hash({"catalog_sha256": catalog_hash, "release_generation": release["hash"], "curve_revision_generation": curve_revisions["hash"]})
    market_changed = market_generation != _text(previous.get("market_generation"))
    source_changed = source_generation != _text(previous.get("source_generation"))
    has_markets = bool(markets) and bool(target_days)
    should_refresh = has_markets and (market_changed or source_changed or not previous.get("last_success_at"))
    target_offsets = []
    today = current.astimezone(TARGET_TZ).date()
    for day in target_days:
        offset = (date.fromisoformat(day) - today).days
        if 0 <= offset <= 3:
            target_offsets.append(offset)
    nwp_target_days = sorted({_text(row.get("event_date")) for row in nwp_markets})
    nwp_offsets = []
    for day in nwp_target_days:
        offset = (date.fromisoformat(day) - today).days
        if 0 <= offset <= 3:
            nwp_offsets.append(offset)
    ready_provider_day_offsets = _ready_provider_day_offsets(
        release=release,
        target_days=nwp_target_days,
        today=today,
    )
    # The production refresh path owns source acquisition and materialization.
    # Broad model_scan is retired from this lane: it is slow, can spawn its
    # own retrying network work, and previously delayed the official curve
    # collector long enough to make an already-published run look stale.
    actions = ["model_release_probe"] if should_refresh else []
    if should_refresh and nwp_target_days and any(ready_provider_day_offsets.values()):
        actions.extend(["official_nwp_daily_curve", "official_nwp_p1_materialization"])
    return {
        "schema": SCHEMA,
        "state": "planned" if should_refresh else "noop",
        "ok": True,
        "trigger": _text(signal.get("trigger")) or "market_scan",
        "market_generation": market_generation,
        "source_generation": source_generation,
        "market_count": len(markets),
        "markets": markets,
        "target_event_dates": target_days,
        "nwp_target_event_dates": nwp_target_days,
        "target_day_offsets": sorted(set(target_offsets)),
        "nwp_target_day_offsets": sorted(set(nwp_offsets)),
        "ready_provider_day_offsets": ready_provider_day_offsets,
        "required_models": models,
        "required_providers": providers,
        "market_changed": market_changed,
        "source_changed": source_changed,
        "actions": actions,
        "blockers": [] if has_markets or not markets else ["weather_market_dependency_target_dates_missing"],
        "release_generation": release,
        "curve_revision_generation": curve_revisions,
        "catalog_sha256": catalog_hash,
    }


def _command_result(
    *, command: Sequence[str], env: Mapping[str, str], timeout_sec: float, output_path: Path,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(command, cwd=str(ROOT), env=dict(env), capture_output=True, text=True, timeout=timeout_sec, check=False)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        result = {"ok": completed.returncode == 0, "returncode": completed.returncode, "command": list(command), "stderr_tail": stderr[-4000:]}
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, Mapping):
                result["payload"] = dict(parsed)
        except json.JSONDecodeError:
            result["stdout_tail"] = stdout[-4000:]
    except subprocess.TimeoutExpired as exc:
        result = {"ok": False, "returncode": 124, "command": list(command), "blockers": ["dependency_command_timeout"], "stdout_tail": str(exc.stdout or "")[-2000:]}
    except OSError as exc:
        result = {"ok": False, "returncode": 127, "command": list(command), "blockers": [f"dependency_command_spawn_failed:{type(exc).__name__}"]}
    _atomic_json(output_path, result)
    return result


def _bounded_timeout_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _run_release_probes(*, plan: Mapping[str, Any], root: Path, env: Mapping[str, str]) -> list[dict[str, Any]]:
    results = []
    for event_day in plan.get("target_event_dates") or []:
        output = root / "data" / "runtime" / "market_dependency_refresh" / f"model_release_{event_day}.json"
        model_names = [
            _text(value)
            for value in plan.get("required_models") or DEFAULT_MODELS
            if _text(value) in {"gfs_seamless", "icon_seamless", "ecmwf_ifs025"}
        ]
        command = [str(root / ".venv" / "bin" / "python"), str(root / "scripts" / "model_release_watcher.py"), "--event-date", event_day, "--models", ",".join(model_names or DEFAULT_MODELS), "--output", str(output)]
        if not Path(command[0]).is_file():
            command[0] = sys.executable
        try:
            per_model_deadline = max(
                2.0,
                min(30.0, float(env.get("PM_MODEL_RELEASE_PROBE_DEADLINE_SEC", os.environ.get("PM_MODEL_RELEASE_PROBE_DEADLINE_SEC", "12")))),
            )
        except (TypeError, ValueError):
            per_model_deadline = 12.0
        # Each provider is isolated in its own child.  Give the outer command
        # enough time for every child plus interpreter startup, while keeping
        # a bounded upper limit for a broken launchd/network environment.
        release_probe_timeout = max(30.0, min(120.0, len(model_names or DEFAULT_MODELS) * (per_model_deadline + 3.0) + 6.0))
        results.append(_command_result(command=command, env=env, timeout_sec=release_probe_timeout, output_path=output.with_suffix(".result.json")))
    return results


def execute_dependency_plan(*, plan: Mapping[str, Any], root: Path = ROOT, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    if not plan.get("actions"):
        return {"ok": True, "state": "noop", "results": []}
    runtime_env = os.environ.copy()
    runtime_env.update({
        "PM_ARCHIVE_WRITE_SCOPE": os.environ.get("PM_MARKET_DEPENDENCY_WRITE_SCOPE", "all"),
        "PM_MODEL_SCAN_SCHEDULE_ENABLED": "0",
        "PM_MODEL_SCAN_BYPASS_SCHEDULE_ON_NEW_MARKET": "1",
        "PM_MODEL_SCAN_BYPASS_YIELD_ON_NEW_MARKET": "1",
        "PM_MODEL_SCAN_YIELD_TO_AUTO_TRADE_ACTIVE": "0",
        "PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES": "0",
        "PM_SCAN_FULL_INTERVAL_MINUTES": "0",
        "PM_SCAN_NO_UPDATE_MODELS": "0",
        "PM_MODEL_SCAN_WEATHER_STALE_NO_UPDATE_MODELS": "0",
        "PM_MODEL_SCAN_DEPENDENCY_BATCH": "1",
        "PM_FACTS_MATERIALIZE_AFTER_SCAN": "1",
        "PM_MODEL_SCAN_FACTS_CURRENT_RUN_ONLY": "0",
        "PM_WEATHER_DISCOVERY_KICK_MODEL_SCAN": "0",
        "PM_OFFICIAL_NWP_CURVE_COLLECTION_ENABLED": "1",
        "PM_OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_ENABLED": "1",
        "PM_OFFICIAL_NWP_DAILY_PEAK_P1_INGRESS_ENABLED": "1",
        # Raw NWP bodies are a non-production evidence cache.  Every source
        # refresh ends with bounded cleanup after its capture child exits and
        # releases the shared capture lease.  The retention worker may remove
        # only truly unreferenced objects; referenced bodies remain strict
        # replay inputs even when their run is old.
        "PM_OFFICIAL_NWP_RAW_RETENTION_ENABLED": os.environ.get("PM_OFFICIAL_NWP_RAW_RETENTION_ENABLED", "1"),
    })
    runtime_env.update({str(k): str(v) for k, v in (env or {}).items()})
    results: list[dict[str, Any]] = []
    results.extend({"action": "model_release_probe", **item} for item in _run_release_probes(plan=plan, root=root, env=runtime_env))

    # ``nwp_target_day_offsets`` is the current explicit field.  Accept the
    # older plan shape as well so a signal queued just before an upgrade does
    # not silently skip every provider refresh.
    offsets = plan.get("nwp_target_day_offsets") or plan.get("target_day_offsets") or []
    model_offsets = plan.get("target_day_offsets") or offsets
    max_offset = max([int(item) for item in model_offsets] or [1])
    if "model_scan" in (plan.get("actions") or []) and not _official_peak_window_ready_for_model_scan(plan):
        # A newly discovered market may precede the official provider run.
        # Record the wait explicitly, but do not launch the broad scanner:
        # it can retry unpublished payloads and hold the archive write path.
        results.append({
            "action": "model_scan",
            "ok": True,
            "state": "waiting_for_official_peak_window",
            "skipped": True,
            "blockers": ["official_source_peak_window_not_ready"],
        })
    elif "model_scan" in (plan.get("actions") or []):
        model_env = dict(runtime_env)
        event_budget = max(2, min(80, int(plan.get("market_count") or 2)))
        model_env.update({"PM_SCAN_FAST_DAYS_AHEAD": str(max_offset), "PM_SCAN_FULL_DAYS_AHEAD": str(max_offset), "PM_MODEL_SCAN_DEPENDENCY_DAYS_AHEAD": str(max_offset), "PM_MODEL_SCAN_MAX_EVENTS_PER_RUN": str(event_budget), "PM_MODEL_SCAN_REFRESH_MAX_EVENTS_PER_RUN": str(event_budget), "PM_MODEL_SCAN_NEW_MARKET_MAX_EVENTS_PER_RUN": str(event_budget), "PM_MODEL_SCAN_WEATHER_STALE_MAX_EVENTS_PER_RUN": str(event_budget)})
        model_output = root / "data" / "runtime" / "market_dependency_refresh" / "model_scan.result.json"
        results.append({"action": "model_scan", **_command_result(command=[str(root / "scripts" / "model_scan.sh")], env=model_env, timeout_sec=float(os.environ.get("PM_MARKET_DEPENDENCY_MODEL_SCAN_TIMEOUT_SEC", "900")), output_path=model_output)})

    curve_root = Path(os.environ.get("PM_OFFICIAL_NWP_CURVE_SOURCE_ROOT", str(root / "data" / "runtime" / "official_nwp_curve_sources_v2"))).expanduser().resolve()
    if "official_nwp_daily_curve" in (plan.get("actions") or []):
        for offset in offsets:
            for provider in plan.get("required_providers") or []:
                ready_by_offset = plan.get("ready_provider_day_offsets")
                if isinstance(ready_by_offset, Mapping) and str(offset) in ready_by_offset:
                    ready_providers = {
                        _text(value)
                        for value in ready_by_offset.get(str(offset)) or []
                        if _text(value)
                    }
                    if provider not in ready_providers:
                        results.append({
                            "action": "official_nwp_daily_curve",
                            "provider": provider,
                            "target_day_offset": offset,
                            "ok": True,
                            "state": "not_yet_due",
                            "skipped": True,
                            "blockers": ["official_source_peak_window_not_ready"],
                        })
                        continue
                status = root / "data" / "runtime" / "market_dependency_refresh" / f"curve_d{offset}_{provider}.result.json"
                command = [str(root / ".venv" / "bin" / "python"), str(root / "scripts" / "run_official_nwp_daily_curve_monitor_job.py"), "--execute", "--provider", provider, "--target-day-offset", str(offset), "--curve-source-root", str(curve_root), "--status-path", str(status.with_suffix(".status.json"))]
                if not Path(command[0]).is_file():
                    command[0] = sys.executable
                results.append({
                    "action": "official_nwp_daily_curve",
                    "provider": provider,
                    "target_day_offset": offset,
                    **_command_result(
                        command=command,
                        env=runtime_env,
                        # The provider collector has its own request and
                        # wall-clock budgets.  Keep the supervisor bounded as
                        # well so a broken transport cannot hold the next
                        # provider/day rotation for the old 30-minute
                        # subprocess timeout.
                        timeout_sec=_bounded_timeout_env(
                            "PM_MARKET_DEPENDENCY_NWP_TIMEOUT_SEC",
                            600.0,
                            minimum=60.0,
                            maximum=900.0,
                        ),
                        output_path=status,
                    ),
                })

    if "official_nwp_p1_materialization" in (plan.get("actions") or []):
        p1_output = root / "data" / "runtime" / "market_dependency_refresh" / "p1_materialization.result.json"
        p1_command = [str(root / ".venv" / "bin" / "python"), str(root / "scripts" / "materialize_official_nwp_daily_peak_p1_ingress.py"), "--apply", "--confirm", "MATERIALIZE_OFFICIAL_NWP_DAILY_PEAK_P1", "--summary", "--curve-source-root", str(curve_root)]
        if not Path(p1_command[0]).is_file():
            p1_command[0] = sys.executable
        results.append({"action": "official_nwp_p1_materialization", **_command_result(command=p1_command, env=runtime_env, timeout_sec=120.0, output_path=p1_output)})
    if "official_nwp_daily_curve" in (plan.get("actions") or []) and _text(runtime_env.get("PM_OFFICIAL_NWP_RAW_RETENTION_ENABLED")) == "1":
        retention_output = root / "output" / "audits" / "official_nwp_raw_retention_latest.json"
        retention_command = [
            str(root / ".venv" / "bin" / "python"),
            str(root / "scripts" / "official_nwp_raw_retention.py"),
            "--apply",
            "--purge-unreferenced",
            "--retain-latest-runs",
            str(max(1, int(os.environ.get("PM_OFFICIAL_NWP_RAW_RETAIN_LATEST_RUNS", "2") or 2))),
            "--audit-output",
            str(retention_output),
        ]
        if not Path(retention_command[0]).is_file():
            retention_command[0] = sys.executable
        results.append({
            "action": "official_nwp_raw_retention",
            **_command_result(
                command=retention_command,
                env=runtime_env,
                timeout_sec=_bounded_timeout_env(
                    "PM_OFFICIAL_NWP_RAW_RETENTION_TIMEOUT_SEC",
                    900.0,
                    minimum=120.0,
                    maximum=1800.0,
                ),
                output_path=retention_output.with_suffix(".result.json"),
            ),
        })
    ok = all(item.get("ok") is True for item in results)
    return {"ok": ok, "state": "completed" if ok else "partial_or_blocked", "results": results}


def _try_lease(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"pid": os.getpid(), "started_at": _iso_now()}) + "\n")
    handle.flush()
    return handle


def _source_probe_due(state: Mapping[str, Any], *, now: datetime) -> bool:
    raw = _text(state.get("last_source_probe_at")).replace("Z", "+00:00")
    try:
        previous = datetime.fromisoformat(raw)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        age = (now - previous.astimezone(timezone.utc)).total_seconds()
    except ValueError:
        return True
    try:
        # Official cycles publish every six hours, but the release probe must
        # notice a newly complete run promptly enough to hand it to the
        # collector.  Five minutes bounds detection latency without creating
        # a per-request burst; provider payloads remain gated separately.
        interval = max(60.0, float(os.environ.get("PM_MARKET_DEPENDENCY_SOURCE_PROBE_INTERVAL_SEC", "300")))
    except ValueError:
        interval = 300.0
    return age >= interval


def _official_peak_window_ready_for_model_scan(plan: Mapping[str, Any]) -> bool:
    """Allow broad model acquisition only after official peak files exist."""
    target_offsets = [
        int(value)
        for value in plan.get("nwp_target_day_offsets") or []
        if str(value).lstrip("-").isdigit()
    ]
    required = {
        _text(value)
        for value in plan.get("required_providers") or []
        if _text(value)
    }
    ready_by_offset = plan.get("ready_provider_day_offsets")
    if not target_offsets or not required:
        return True
    if not isinstance(ready_by_offset, Mapping):
        # Preserve compatibility with hand-built plans from older callers.
        return True
    return all(
        required.issubset(
            {
                _text(value)
                for value in ready_by_offset.get(str(offset)) or []
                if _text(value)
            }
        )
        for offset in target_offsets
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal", type=Path, default=DEFAULT_SIGNAL)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    signal = _read_json(args.signal.expanduser().resolve())
    state_path = args.state.expanduser().resolve()
    status_path = args.status.expanduser().resolve()
    plan = build_dependency_plan(signal=signal, state=_read_json(state_path), root=root)
    if args.plan_only:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    lease = _try_lease(state_path.with_suffix(state_path.suffix + ".lock"))
    if lease is None:
        result = {"schema": SCHEMA, "ok": True, "state": "already_running", "blockers": ["market_dependency_refresh_already_in_progress"], "plan": plan}
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    try:
        # Re-read under the lease: a newer market scan may have replaced the
        # signal while the launcher was starting this child.
        signal = _read_json(args.signal.expanduser().resolve())
        previous = _read_json(state_path)
        plan = build_dependency_plan(signal=signal, state=previous, root=root)
        probe_results: list[dict[str, Any]] = []
        # Probe immediately for a new market and periodically thereafter.  A
        # probe is HEAD/list metadata only.  Rebuild the plan after it so a
        # newly published run is collected in this same cycle rather than
        # waiting for the next timer tick.
        probe_due = _source_probe_due(previous, now=datetime.now(timezone.utc))
        probe_requested = bool(plan.get("market_count", 0)) and (
            "model_release_probe" in (plan.get("actions") or [])
            or (not plan.get("actions") and probe_due)
        )
        if probe_requested:
            probe_results = _run_release_probes(plan=plan, root=root, env=os.environ.copy())
            plan = build_dependency_plan(signal=signal, state=previous, root=root)
            # The probe has already run in this cycle; leave only the actual
            # acquisition/materialization actions for the executor.
            plan = {
                **plan,
                "actions": [
                    action for action in plan.get("actions") or []
                    if action != "model_release_probe"
                ],
            }
        execution = execute_dependency_plan(plan=plan, root=root)
        # Release probes publish their stable file identity during execution.
        # Persist the post-probe generation, otherwise the next minute would
        # mistake the probe's own output for a new source revision.
        persisted_plan = build_dependency_plan(signal=signal, state=previous, root=root)
        updated_at = _iso_now()
        result = {"schema": SCHEMA, "updated_at": updated_at, "ok": execution.get("ok") is True, "state": execution.get("state"), "plan": persisted_plan, "source_probe_results": probe_results, "execution": execution}
        if result["ok"]:
            next_state = {"schema": SCHEMA, "updated_at": updated_at, "market_generation": persisted_plan.get("market_generation"), "source_generation": persisted_plan.get("source_generation"), "target_event_dates": persisted_plan.get("target_event_dates"), "required_models": persisted_plan.get("required_models"), "required_providers": persisted_plan.get("required_providers"), "last_source_probe_at": updated_at}
            if execution.get("state") == "completed":
                next_state["last_success_at"] = updated_at
            else:
                next_state["last_success_at"] = previous.get("last_success_at", "")
            _atomic_json(state_path, next_state)
        _atomic_json(status_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["ok"] else 2
    finally:
        lease.close()


if __name__ == "__main__":
    raise SystemExit(main())
