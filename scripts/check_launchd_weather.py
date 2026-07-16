#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
ARCHIVE_DB = ROOT / "data" / "archive.db"
HOME = Path.home()
LAUNCH_AGENTS = HOME / "Library" / "LaunchAgents"
LAUNCH_DAEMONS = Path("/Library/LaunchDaemons")
MONITOR_DAEMON_LABEL = os.environ.get("PM_WEATHER_MONITOR_LABEL", "com.weather.monitor.daemon").strip()
MONITOR_DAEMON_DOMAIN_LABEL = (
    MONITOR_DAEMON_LABEL if MONITOR_DAEMON_LABEL.startswith("system/") else f"system/{MONITOR_DAEMON_LABEL}"
)
MONITOR_DAEMON_NAME = MONITOR_DAEMON_DOMAIN_LABEL.rsplit("/", 1)[-1]
MONITOR_DAEMON_PLIST = LAUNCH_DAEMONS / f"{MONITOR_DAEMON_NAME}.plist"
SOURCE_MONITOR_DAEMON_PLIST = ROOT / "launchd" / f"{MONITOR_DAEMON_NAME}.plist"
AWC_SOURCE_HEALTH_AUDIT = ROOT / "scripts" / "awc_source_health_audit.py"
POSTERIOR_TARGET_LIVE_JOB_AUDIT = ROOT / "scripts" / "posterior_target_live_job_audit.py"
ORDER_FAILURE_PAYLOAD_TRACE_AUDIT = ROOT / "scripts" / "audit_order_failure_payload_trace.py"
CLOB_ORDER_SERVICE_LABEL = "com.weather.clob_order_service"

WEATHER_REQUIRED = {
    "com.xiong.weather.ui": "scripts/ui_server_forever.sh",
    "com.weather.market_ws": "scripts/market_ws_listener.sh",
    "com.weather.auto_trade_entry_1m": "scripts/auto_trade_entry_1m.sh",
    "com.weather.trade_engine": "scripts/trade_engine_loop.sh",
    "com.weather.task_queue_worker": "scripts/task_queue_worker.py",
}

WEATHER_MONITOR_REQUIRED = {
    MONITOR_DAEMON_DOMAIN_LABEL: (
        "scripts/polymarket_monitor_daemon.sh",
        "scripts/auto_trade_watcher.py",
    ),
}

WEATHER_LEGACY_MONITORS = {
    "com.weather.auto_trade_watcher",
    "system/com.weather.auto_trade_watcher",
    "system/com.weather.monitor_daemon",
    "system/com.weather.target_rule_shadow_capture",
    "system/com.weather.market_finality_shadow_capture",
    "system/com.weather.physical_final_source_shadow_capture",
    "system/com.weather.settlement_final_source_shadow_capture",
    "com.weather.amsc_p10_shadow_source_sidecar",
    "gui/501/com.weather.amsc_p10_shadow_source_sidecar",
}
WEATHER_LEGACY_MONITOR_PLISTS = {
    LAUNCH_DAEMONS / "com.weather.auto_trade_watcher.plist",
    LAUNCH_DAEMONS / "com.weather.monitor_daemon.plist",
    LAUNCH_AGENTS / "com.weather.auto_trade_watcher.plist",
    LAUNCH_AGENTS / "com.weather.monitor_daemon.plist",
    LAUNCH_DAEMONS / "com.weather.target_rule_shadow_capture.plist",
    LAUNCH_DAEMONS / "com.weather.market_finality_shadow_capture.plist",
    LAUNCH_DAEMONS / "com.weather.physical_final_source_shadow_capture.plist",
    LAUNCH_DAEMONS / "com.weather.settlement_final_source_shadow_capture.plist",
    LAUNCH_AGENTS / "com.weather.amsc_p10_shadow_source_sidecar.plist",
}

WEATHER_SCHEDULED = {
    "com.weather.market_refresh_1m",
    "com.weather.model_scan",
    "com.weather.daily_scan",
    "com.weather.q_calibrated_writeback_hourly",
    "com.weather.feedback_loop_hourly",
    "com.weather.cleanup_weekly",
    "com.weather.archive_db_auto_guard",
}

WEATHER_LEGACY_CONFLICTS = {
    "com.weather.auto_trade_worker",
    "com.weather.trade_sync_loop",
}


def _run(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            check=False,
        )
        return proc.stdout
    except Exception:
        return ""


def _run_json(args: list[str], *, timeout: float = 8.0) -> tuple[int, dict[str, Any], str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return 124, {}, f"{exc.__class__.__name__}: {exc}"
    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception as exc:
        return proc.returncode, {}, f"json_parse_failed:{exc.__class__.__name__}: {exc}"
    return proc.returncode, payload if isinstance(payload, dict) else {}, proc.stderr[-1000:]


def _http_json(url: str, *, timeout: float = 2.0) -> tuple[bool, dict[str, Any], str, float]:
    started = time.monotonic()
    try:
        with urlopen(url, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
    except Exception as exc:
        return False, {}, f"{exc.__class__.__name__}: {exc}", (time.monotonic() - started) * 1000.0
    elapsed_ms = (time.monotonic() - started) * 1000.0
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except Exception as exc:
        return False, {}, f"json_parse_failed:{exc.__class__.__name__}: {exc}", elapsed_ms
    return True, payload if isinstance(payload, dict) else {}, "", elapsed_ms


def _load_dotenv() -> None:
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        if key in os.environ:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


def _launchctl_list() -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for raw in _run(["launchctl", "list"]).splitlines():
        parts = raw.split()
        if len(parts) < 3 or parts[0] == "PID":
            continue
        label = parts[2]
        if not (
            label.startswith("com.weather.")
            or label == "com.xiong.weather.ui"
            or label.startswith("com.polymarket.monitor")
        ):
            continue
        rows[label] = {"pid": parts[0], "status": parts[1], "label": label}
    return rows


def _plist(label: str) -> dict[str, Any]:
    path = LAUNCH_AGENTS / f"{label}.plist"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return plistlib.load(handle) or {}
    except Exception:
        return {}


def _load_plist_path(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return plistlib.load(handle) or {}
    except Exception:
        return {}


def _plist_labels() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted(LAUNCH_AGENTS.glob("com.weather*.plist")) + sorted(LAUNCH_AGENTS.glob("com.xiong.weather*.plist")):
        try:
            with path.open("rb") as handle:
                payload = plistlib.load(handle) or {}
        except Exception:
            continue
        label = str(payload.get("Label") or "").strip()
        if not label:
            continue
        out.setdefault(label, []).append(str(path))
    return out


def _launchctl_print(label: str) -> tuple[bool, dict[str, Any]]:
    proc = subprocess.run(
        ["launchctl", "print", label],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=8,
        check=False,
    )
    if proc.returncode != 0:
        return False, {}
    state = ""
    pid = ""
    for raw in proc.stdout.splitlines():
        text = raw.strip()
        if text.startswith("state = "):
            state = text.split("=", 1)[1].strip()
        elif text.startswith("pid = "):
            pid = text.split("=", 1)[1].strip()
    return True, {"pid": pid, "status": state, "label": label}


def _program_text(plist: dict[str, Any]) -> str:
    args = plist.get("ProgramArguments")
    if isinstance(args, list):
        return " ".join(str(item) for item in args)
    program = plist.get("Program")
    return str(program or "")


def _program_contains_expected(text: str, expected: Any) -> bool:
    if isinstance(expected, (list, tuple, set)):
        return any(str(fragment) in text for fragment in expected if fragment)
    return bool(expected and str(expected) in text)


def _is_keepalive(plist: dict[str, Any]) -> bool:
    value = plist.get("KeepAlive")
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value)
    return False


def _is_interval_job(plist: dict[str, Any]) -> bool:
    return "StartInterval" in plist or "StartCalendarInterval" in plist


def _ps_weather() -> str:
    return _run(["ps", "axo", "pid=,ppid=,command="])


def _ps_weather_rows() -> list[dict[str, Any]]:
    def _parse_etime_seconds(text: str) -> int:
        raw = str(text or "").strip()
        if not raw:
            return 0
        days = 0
        if "-" in raw:
            day_text, raw = raw.split("-", 1)
            try:
                days = int(day_text)
            except Exception:
                days = 0
        pieces = raw.split(":")
        try:
            nums = [int(part) for part in pieces]
        except Exception:
            return 0
        if len(nums) == 3:
            hours, minutes, seconds = nums
        elif len(nums) == 2:
            hours, minutes, seconds = 0, nums[0], nums[1]
        elif len(nums) == 1:
            hours, minutes, seconds = 0, 0, nums[0]
        else:
            return 0
        return int(days * 86400 + hours * 3600 + minutes * 60 + seconds)

    rows: list[dict[str, Any]] = []
    output = _run(["ps", "axo", "pid=,ppid=,etime=,command="])
    for raw in output.splitlines():
        parts = raw.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except Exception:
            continue
        rows.append(
            {
                "pid": pid,
                "ppid": ppid,
                "etime": parts[2],
                "etimes": _parse_etime_seconds(parts[2]),
                "command": parts[3],
            }
        )
    return rows


def _process_started_at(row: dict[str, Any]) -> float:
    try:
        etimes = float(row.get("etimes") or 0)
    except Exception:
        return 0.0
    if etimes <= 0:
        return 0.0
    return time.time() - etimes


def _process_by_pid(rows: list[dict[str, Any]], pid_raw: Any) -> dict[str, Any] | None:
    try:
        pid = int(str(pid_raw or "").strip())
    except Exception:
        return None
    for row in rows:
        try:
            if int(row.get("pid") or -1) == pid:
                return row
        except Exception:
            continue
    return None


def _latest_mtime(paths: list[Path] | tuple[Path, ...]) -> float:
    latest = 0.0
    for path in paths:
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def _parse_iso_utc(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except Exception:
        return float(default)
    return value if value > 0 else float(default)


def _env_float_zero_ok(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except Exception:
        return float(default)
    return value if value >= 0 else float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    text = str(raw).strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _audit_direct_scheduler_config() -> dict[str, Any]:
    poll_sec = _env_float(
        "PM_TRADE_ENGINE_POLL_SEC",
        _env_float("PM_AUTO_TRADE_WORKER_POLL_SEC", 2.0),
    )
    cfg = {
        "posterior_inline_worker_once": _env_bool(
            "PM_POSTERIOR_TARGET_DIRECT_RUN_WORKER_ONCE",
            True,
        ),
        "hard_source_inline_worker_once": _env_bool(
            "PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE",
            True,
        ),
        "trade_engine_claim_direct_target_jobs": _env_bool(
            "PM_TRADE_ENGINE_CLAIM_DIRECT_TARGET_JOBS",
            False,
        ),
        "direct_primary_fast_submit": _env_bool(
            "PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ENTRY_FAST_SUBMIT",
            True,
        ),
        "direct_primary_fast_slice_early": _env_bool(
            "PM_AUTO_TRADE_DIRECT_JOB_PRIMARY_FAST_SLICE_EARLY",
            True,
        ),
        "direct_job_skip_run_row": _env_bool(
            "PM_AUTO_TRADE_DIRECT_JOB_SKIP_RUN_ROW",
            False,
        ),
        "direct_entry_defer_confirm": _env_bool(
            "PM_AUTO_TRADE_DIRECT_ENTRY_DEFER_CONFIRM",
            True,
        ),
        "direct_proxy_fallback_enabled": _env_bool(
            "PM_AUTO_TRADE_DIRECT_PROXY_FALLBACK_ENABLED",
            False,
        ),
        "direct_force_proxy_on_fallback": _env_bool(
            "PM_AUTO_TRADE_DIRECT_FORCE_PROXY_ON_FALLBACK",
            False,
        ),
        "direct_entry_clob_timeout_cap_ms": _env_float_zero_ok(
            "PM_AUTO_TRADE_DIRECT_ENTRY_CLOB_TIMEOUT_CAP_MS",
            2500.0,
        ),
        "direct_entry_operation_timeout_ms": _env_float_zero_ok(
            "PM_AUTO_TRADE_DIRECT_ENTRY_OPERATION_TIMEOUT_MS",
            2800.0,
        ),
        "direct_order_timeout_sec": _env_float(
            "PM_AUTO_TRADE_DIRECT_ORDER_TIMEOUT_SEC",
            3.0,
        ),
        "clob_order_service_enabled": _env_bool(
            "PM_CLOB_ORDER_SERVICE_ENABLED",
            False,
        ),
        "clob_order_service_request_timeout_sec": _env_float(
            "PM_CLOB_ORDER_SERVICE_REQUEST_TIMEOUT_SEC",
            8.0,
        ),
        "clob_order_service_idempotency_ttl_sec": _env_float(
            "PM_CLOB_ORDER_SERVICE_IDEMPOTENCY_TTL_SEC",
            7200.0,
        ),
        "trade_engine_poll_sec": poll_sec,
        "worker_claim_retry": _env_float("PM_HARD_SOURCE_DIRECT_WORKER_CLAIM_RETRY", 4.0),
        "worker_claim_backoff_sec": _env_float(
            "PM_HARD_SOURCE_DIRECT_WORKER_CLAIM_BACKOFF_SEC",
            0.025,
        ),
        "worker_claim_backoff_cap_sec": _env_float(
            "PM_HARD_SOURCE_DIRECT_WORKER_CLAIM_BACKOFF_CAP_SEC",
            0.050,
        ),
        "worker_db_connect_timeout_sec": _env_float(
            "PM_HARD_SOURCE_DIRECT_WORKER_DB_CONNECT_TIMEOUT_SEC",
            0.150,
        ),
        "worker_db_busy_timeout_ms": _env_float(
            "PM_HARD_SOURCE_DIRECT_WORKER_DB_BUSY_TIMEOUT_MS",
            200.0,
        ),
        "worker_runtime_lock_wait_sec": _env_float_zero_ok(
            "PM_HARD_SOURCE_DIRECT_WORKER_RUNTIME_LOCK_WAIT_SEC",
            0.0,
        ),
        "worker_facts_lock_wait_sec": _env_float_zero_ok(
            "PM_HARD_SOURCE_DIRECT_FACTS_LOCK_WAIT_SEC",
            0.0,
        ),
        "worker_bookkeeping_connect_timeout_sec": _env_float(
            "PM_HARD_SOURCE_DIRECT_WORKER_BOOKKEEPING_CONNECT_TIMEOUT_SEC",
            0.150,
        ),
        "worker_bookkeeping_busy_timeout_ms": _env_float(
            "PM_HARD_SOURCE_DIRECT_WORKER_BOOKKEEPING_BUSY_TIMEOUT_MS",
            200.0,
        ),
    }
    checks = {
        "posterior_inline_worker_once": cfg["posterior_inline_worker_once"] is True,
        "hard_source_inline_worker_once": cfg["hard_source_inline_worker_once"] is True,
        "trade_engine_claim_direct_target_jobs": cfg["trade_engine_claim_direct_target_jobs"] is True,
        "direct_primary_fast_submit": cfg["direct_primary_fast_submit"] is True,
        "direct_primary_fast_slice_early": cfg["direct_primary_fast_slice_early"] is True,
        "direct_job_skip_run_row": cfg["direct_job_skip_run_row"] is True,
        "direct_entry_defer_confirm": cfg["direct_entry_defer_confirm"] is True,
        "direct_proxy_fallback_disabled": cfg["direct_proxy_fallback_enabled"] is False,
        "direct_force_proxy_on_fallback_disabled": cfg["direct_force_proxy_on_fallback"] is False,
        "direct_entry_clob_timeout_cap_ms_lte_2500": (
            float(cfg["direct_entry_clob_timeout_cap_ms"]) <= 2500.0
        ),
        "direct_entry_operation_timeout_ms_lte_2800": (
            float(cfg["direct_entry_operation_timeout_ms"]) <= 2800.0
        ),
        "direct_order_timeout_sec_lte_3": float(cfg["direct_order_timeout_sec"]) <= 3.0,
        "trade_engine_poll_sec_lte_2": float(cfg["trade_engine_poll_sec"]) <= 2.0,
        "worker_claim_retry_lte_4": float(cfg["worker_claim_retry"]) <= 4.0,
        "worker_claim_backoff_sec_lte_0_05": float(cfg["worker_claim_backoff_sec"]) <= 0.05,
        "worker_claim_backoff_cap_sec_lte_0_10": float(cfg["worker_claim_backoff_cap_sec"]) <= 0.10,
        "worker_db_connect_timeout_sec_lte_0_25": float(cfg["worker_db_connect_timeout_sec"]) <= 0.25,
        "worker_db_busy_timeout_ms_lte_250": float(cfg["worker_db_busy_timeout_ms"]) <= 250.0,
        "worker_runtime_lock_wait_zero": float(cfg["worker_runtime_lock_wait_sec"]) <= 0.0,
        "worker_facts_lock_wait_zero": float(cfg["worker_facts_lock_wait_sec"]) <= 0.0,
        "worker_bookkeeping_connect_timeout_sec_lte_0_25": (
            float(cfg["worker_bookkeeping_connect_timeout_sec"]) <= 0.25
        ),
        "worker_bookkeeping_busy_timeout_ms_lte_250": (
            float(cfg["worker_bookkeeping_busy_timeout_ms"]) <= 250.0
        ),
    }
    if cfg["clob_order_service_enabled"] is True:
        for legacy_key in (
            "direct_proxy_fallback_disabled",
            "direct_force_proxy_on_fallback_disabled",
            "direct_entry_clob_timeout_cap_ms_lte_2500",
            "direct_entry_operation_timeout_ms_lte_2800",
            "direct_order_timeout_sec_lte_3",
        ):
            checks.pop(legacy_key, None)
        checks.update(
            {
                "clob_order_service_enabled": True,
                "clob_order_service_request_timeout_sec_lte_8": (
                    float(cfg["clob_order_service_request_timeout_sec"]) <= 8.0
                ),
                "clob_order_service_idempotency_ttl_positive": (
                    float(cfg["clob_order_service_idempotency_ttl_sec"]) > 0.0
                ),
            }
        )
    return {
        "ok": all(checks.values()),
        "config": cfg,
        "checks": checks,
        "failed_checks": [key for key, passed in checks.items() if not passed],
    }
def _launchctl_state(domain_label: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["launchctl", "print", domain_label],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=4,
            check=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "loaded": False,
            "running": False,
            "pid": "",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    text = (proc.stdout or "") + (proc.stderr or "")
    pid = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid ="):
            pid = stripped.split("=", 1)[1].strip()
            break
    return {
        "ok": proc.returncode == 0,
        "loaded": proc.returncode == 0,
        "running": "state = running" in text,
        "pid": pid,
        "error": "" if proc.returncode == 0 else text.strip()[:500],
    }


def _audit_clob_order_service() -> dict[str, Any]:
    enabled = _env_bool("PM_CLOB_ORDER_SERVICE_ENABLED", False)
    host = str(os.environ.get("PM_CLOB_ORDER_SERVICE_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = str(os.environ.get("PM_CLOB_ORDER_SERVICE_PORT") or "8765").strip() or "8765"
    url = str(os.environ.get("PM_CLOB_ORDER_SERVICE_URL") or f"http://{host}:{port}").strip().rstrip("/")
    gui_state = _launchctl_state(f"gui/{os.getuid()}/{CLOB_ORDER_SERVICE_LABEL}")
    system_state = _launchctl_state(f"system/{CLOB_ORDER_SERVICE_LABEL}")
    health_timeout = _env_float("PM_CLOB_ORDER_SERVICE_HEALTH_TIMEOUT_SEC", 3.0)
    if health_timeout is None or health_timeout <= 0:
        health_timeout = 3.0
    health_timeout = max(1.5, min(float(health_timeout), 8.0))
    health_ok, health_payload, health_error, health_ms = _http_json(
        f"{url}/health",
        timeout=health_timeout,
    )
    checks = {
        "enabled": enabled,
        "launchd_running": bool(gui_state.get("running") or system_state.get("running")),
        "health_ok": bool(health_ok and health_payload.get("ok") is True),
        "health_ready": bool(health_payload.get("ready") is True),
    }
    if enabled:
        try:
            metadata_cache_size = int(float(health_payload.get("metadata_cache_size") or 0))
        except Exception:
            metadata_cache_size = 0
        checks["metadata_cache_prewarmed"] = metadata_cache_size > 0
    return {
        "ok": (not enabled) or all(checks.values()),
        "enabled": enabled,
        "url": url,
        "checks": checks,
        "gui": gui_state,
        "system": system_state,
        "health": health_payload,
        "health_error": health_error,
        "health_ms": round(float(health_ms), 3),
        "health_timeout_sec": round(float(health_timeout), 3),
        "failed_checks": [key for key, passed in checks.items() if not passed],
    }


def _audit_refresh_coordinator_config() -> dict[str, Any]:
    coordinator_lane_name = str(os.environ.get("PM_REFRESH_COORDINATOR_LANE_NAME") or "").strip()
    default_lane_name = coordinator_lane_name or "weather_refresh_lane"
    preempt_owners = {
        item.strip()
        for item in str(os.environ.get("PM_HARD_SOURCE_PREEMPT_NONCRITICAL_OWNERS") or "").split(",")
        if item.strip()
    }
    live_owners = {
        item.strip()
        for item in str(os.environ.get("PM_REFRESH_COORDINATOR_LIVE_OWNERS") or "").split(",")
        if item.strip()
    }
    model_owners = {
        item.strip()
        for item in str(os.environ.get("PM_REFRESH_COORDINATOR_MODEL_OWNERS") or "").split(",")
        if item.strip()
    }
    market_owners = {
        item.strip()
        for item in str(os.environ.get("PM_REFRESH_COORDINATOR_MARKET_OWNERS") or "").split(",")
        if item.strip()
    }
    data_source_owners = {
        item.strip()
        for item in str(os.environ.get("PM_REFRESH_COORDINATOR_DATA_SOURCE_OWNERS") or "").split(",")
        if item.strip()
    }
    facts_write_owners = {
        item.strip()
        for item in str(os.environ.get("PM_REFRESH_COORDINATOR_FACTS_WRITE_OWNERS") or "").split(",")
        if item.strip()
    }
    required_preempt_owners = {
        "model_scan",
        "model_scan_hourly",
        "market_refresh",
        "market_refresh_frequent",
        "market_refresh_urgent",
        "weather_discovery",
        "model_release_watcher",
        "daily_scan",
        "weatherctl_facts",
    }
    required_live_owners = {
        "live_ssot",
        "hard_source",
        "awc_release",
        "hard_source_direct_poller",
        "posterior_target_direct",
        "auto_trade_order",
        "auto_worker",
        "auto_trade_worker",
        "auto_trade_job_worker",
    }
    required_model_owners = {"model_scan", "model_scan_hourly", "model_release_watcher"}
    required_market_owners = {
        "market_refresh",
        "market_refresh_frequent",
        "market_refresh_urgent",
        "weather_discovery",
    }
    required_data_source_owners = {"weatherctl_facts", "daily_scan"}
    required_facts_write_owners = {"weatherctl_facts", "daily_scan"}
    cfg = {
        "coordinator_enabled": _env_bool("PM_REFRESH_COORDINATOR_ENABLED", True),
        "live_protect_enabled": _env_bool("PM_REFRESH_COORDINATOR_LIVE_PROTECT_ENABLED", True),
        "unified_lane_enabled": _env_bool("PM_REFRESH_COORDINATOR_UNIFIED_LANE_ENABLED", True),
        "lane_name": coordinator_lane_name,
        "role_contract_version": str(
            os.environ.get("PM_REFRESH_COORDINATOR_ROLE_CONTRACT_VERSION")
            or "refresh_coordinator_v3"
        ).strip(),
        "model_scan_lane_name": str(os.environ.get("PM_MODEL_SCAN_LANE_NAME") or default_lane_name).strip(),
        "market_refresh_lane_name": str(os.environ.get("PM_MARKET_REFRESH_LANE_NAME") or default_lane_name).strip(),
        "daily_scan_lane_name": str(os.environ.get("PM_DAILY_SCAN_LANE_NAME") or default_lane_name).strip(),
        "suppress_legacy_peer_guards": _env_bool(
            "PM_REFRESH_COORDINATOR_SUPPRESS_LEGACY_PEER_GUARDS",
            True,
        ),
        "market_refresh_db_lock_enabled": _env_bool("PM_MARKET_REFRESH_DB_LOCK_ENABLED", True),
        "model_scan_db_lock_enabled": _env_bool("PM_MODEL_SCAN_DB_LOCK_ENABLED", True),
        "weather_discovery_lane_enabled": _env_bool("PM_WEATHER_DISCOVERY_COORDINATOR_LANE_ENABLED", True),
        "weather_discovery_lane_wait_sec": _env_float_zero_ok(
            "PM_WEATHER_DISCOVERY_LANE_WAIT_SEC",
            0.0,
        ),
        "weather_discovery_kick_model_scan": _env_bool("PM_WEATHER_DISCOVERY_KICK_MODEL_SCAN", False),
        "model_scan_ignore_market_refresh_active": _env_bool(
            "PM_MODEL_SCAN_IGNORE_MARKET_REFRESH_ACTIVE",
            False,
        ),
        "model_scan_yield_to_market_refresh_when_tick_stale": _env_bool(
            "PM_MODEL_SCAN_YIELD_TO_MARKET_REFRESH_WHEN_TICK_STALE",
            True,
        ),
        "market_refresh_ignore_model_scan_active": _env_bool(
            "PM_MARKET_REFRESH_IGNORE_MODEL_SCAN_ACTIVE",
            False,
        ),
        "market_refresh_yield_to_model_scan_active": _env_bool(
            "PM_MARKET_REFRESH_YIELD_TO_MODEL_SCAN_ACTIVE",
            True,
        ),
        "model_scan_awc_fact_refresh_enabled": _env_bool(
            "PM_MODEL_SCAN_AWC_FACT_REFRESH_ENABLED",
            False,
        ),
        "model_scan_awc_fact_refresh_pre_lane_enabled": _env_bool(
            "PM_MODEL_SCAN_AWC_FACT_REFRESH_PRE_LANE_ENABLED",
            False,
        ),
        "model_scan_awc_fact_refresh_lane_wait_sec": _env_float_zero_ok(
            "PM_MODEL_SCAN_AWC_FACT_REFRESH_LANE_WAIT_SEC",
            0.0,
        ),
        "model_scan_awc_fact_refresh_db_lock_wait_sec": _env_float_zero_ok(
            "PM_MODEL_SCAN_AWC_FACT_REFRESH_DB_LOCK_WAIT_SEC",
            0.0,
        ),
        "model_scan_precip_nowcast_fact_refresh_enabled": _env_bool(
            "PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_ENABLED",
            False,
        ),
        "model_scan_precip_nowcast_fact_refresh_lane_wait_sec": _env_float_zero_ok(
            "PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_LANE_WAIT_SEC",
            0.0,
        ),
        "model_scan_precip_nowcast_fact_refresh_db_lock_wait_sec": _env_float_zero_ok(
            "PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_DB_LOCK_WAIT_SEC",
            0.0,
        ),
        "model_scan_facts_materialize_on_yield_enabled": _env_bool(
            "PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD",
            False,
        ),
        "model_scan_facts_materialize_on_yield_lane_wait_sec": _env_float_zero_ok(
            "PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_LANE_WAIT_SEC",
            0.0,
        ),
        "model_scan_facts_materialize_on_yield_db_lock_wait_sec": _env_float_zero_ok(
            "PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_DB_LOCK_WAIT_SEC",
            0.0,
        ),
        "facts_materialize_after_scan": _env_bool("PM_FACTS_MATERIALIZE_AFTER_SCAN", False),
        "model_scan_hourly_facts_materialize_enabled": _env_bool(
            "PM_MODEL_SCAN_HOURLY_FACTS_MATERIALIZE_ENABLED",
            False,
        ),
        "awc_recent_window_preserve_local_day_max": _env_bool(
            "PM_AWC_FACT_REFRESH_RECENT_WINDOW_PRESERVE_LOCAL_DAY_MAX",
            True,
        ),
        "awc_local_day_margin_hours": _env_float_zero_ok(
            "PM_AWC_FACT_REFRESH_LOCAL_DAY_MARGIN_HOURS",
            2.0,
        ),
        "awc_local_day_min_hours": _env_float(
            "PM_AWC_FACT_REFRESH_LOCAL_DAY_MIN_HOURS",
            8.0,
        ),
        "market_ws_defer_during_live_ssot": _env_bool(
            "PM_MARKET_WS_DEFER_DURING_LIVE_SSOT",
            True,
        ),
        "market_ws_db_busy_timeout_ms": _env_float(
            "PM_MARKET_WS_DB_BUSY_TIMEOUT_MS",
            250.0,
        ),
        "market_ws_write_retry_attempts": _env_float(
            "PM_MARKET_WS_WRITE_RETRY_ATTEMPTS",
            2.0,
        ),
        "ui_snapshot_summary_live_market_hydrate": _env_bool(
            "PM_UI_SNAPSHOT_SUMMARY_LIVE_MARKET_HYDRATE",
            False,
        ),
        "ui_max_concurrent_requests": _env_float(
            "PM_UI_MAX_CONCURRENT_REQUESTS",
            6.0,
        ),
        "ui_max_concurrent_requests_hard_cap": _env_float(
            "PM_UI_MAX_CONCURRENT_REQUESTS_HARD_CAP",
            6.0,
        ),
        "ui_priority_max_concurrent_requests": _env_float(
            "PM_UI_PRIORITY_MAX_CONCURRENT_REQUESTS",
            2.0,
        ),
        "ui_heavy_max_concurrent_requests": _env_float(
            "PM_UI_HEAVY_MAX_CONCURRENT_REQUESTS",
            1.0,
        ),
        "ui_request_queue_size": _env_float(
            "PM_UI_REQUEST_QUEUE_SIZE",
            64.0,
        ),
        "most_likely_max_concurrency": _env_float(
            "PM_MOST_LIKELY_MAX_CONCURRENCY",
            1.0,
        ),
        "most_likely_history_row_limit": _env_float(
            "PM_MOST_LIKELY_HISTORY_ROW_LIMIT",
            12.0,
        ),
        "most_likely_history_query_timeout_sec": _env_float(
            "PM_MOST_LIKELY_HISTORY_QUERY_TIMEOUT_SEC",
            0.8,
        ),
        "most_likely_prewarm_all_weights": _env_bool(
            "PM_MOST_LIKELY_PREWARM_ALL_WEIGHTS",
            False,
        ),
        "hard_source_preempt_noncritical_refresh_enabled": _env_bool(
            "PM_HARD_SOURCE_PREEMPT_NONCRITICAL_REFRESH_ENABLED",
            True,
        ),
        "preempt_owners": sorted(preempt_owners),
        "live_owners": sorted(live_owners),
        "model_owners": sorted(model_owners),
        "market_owners": sorted(market_owners),
        "data_source_owners": sorted(data_source_owners),
        "facts_write_owners": sorted(facts_write_owners),
        "awc_lock_guard": _env_bool("PM_REFRESH_COORDINATOR_AWC_LOCK_GUARD", True),
        "enforce_owner_role": _env_bool("PM_REFRESH_COORDINATOR_ENFORCE_OWNER_ROLE", True),
        "allow_nonlive_awc_refresh": _env_bool(
            "PM_REFRESH_COORDINATOR_ALLOW_NONLIVE_AWC_REFRESH",
            False,
        ),
        "allow_scan_facts_materialize": _env_bool(
            "PM_REFRESH_COORDINATOR_ALLOW_SCAN_FACTS_MATERIALIZE",
            False,
        ),
    }
    checks = {
        "coordinator_enabled": cfg["coordinator_enabled"] is True,
        "live_protect_enabled": cfg["live_protect_enabled"] is True,
        "unified_lane_enabled": cfg["unified_lane_enabled"] is True,
        "lane_name_weather_refresh_lane": cfg["lane_name"] == "weather_refresh_lane",
        "role_contract_v3": cfg["role_contract_version"] == "refresh_coordinator_v3",
        "lane_aliases_weather_refresh_lane": (
            cfg["model_scan_lane_name"] == "weather_refresh_lane"
            and cfg["market_refresh_lane_name"] == "weather_refresh_lane"
            and cfg["daily_scan_lane_name"] == "weather_refresh_lane"
        ),
        "suppress_legacy_peer_guards": cfg["suppress_legacy_peer_guards"] is True,
        "market_refresh_db_lock_enabled": cfg["market_refresh_db_lock_enabled"] is True,
        "model_scan_db_lock_enabled": cfg["model_scan_db_lock_enabled"] is True,
        "weather_discovery_lane_enabled": cfg["weather_discovery_lane_enabled"] is True,
        "weather_discovery_lane_wait_zero": (
            float(cfg["weather_discovery_lane_wait_sec"]) <= 0.0
        ),
        "weather_discovery_does_not_kick_model_scan": (
            cfg["weather_discovery_kick_model_scan"] is False
        ),
        "legacy_model_scan_peer_guard_neutralized": (
            cfg["model_scan_ignore_market_refresh_active"] is True
            and cfg["model_scan_yield_to_market_refresh_when_tick_stale"] is False
        ),
        "legacy_market_refresh_peer_guard_neutralized": (
            cfg["market_refresh_ignore_model_scan_active"] is True
            and cfg["market_refresh_yield_to_model_scan_active"] is False
        ),
        "model_scan_awc_fact_refresh_default_off": (
            cfg["model_scan_awc_fact_refresh_enabled"] is False
        ),
        "model_scan_awc_fact_refresh_pre_lane_off": (
            cfg["model_scan_awc_fact_refresh_pre_lane_enabled"] is False
        ),
        "model_scan_awc_fact_refresh_lane_wait_zero": (
            float(cfg["model_scan_awc_fact_refresh_lane_wait_sec"]) <= 0.0
        ),
        "model_scan_awc_fact_refresh_db_wait_zero": (
            float(cfg["model_scan_awc_fact_refresh_db_lock_wait_sec"]) <= 0.0
        ),
        "model_scan_precip_nowcast_fact_refresh_default_off": (
            cfg["model_scan_precip_nowcast_fact_refresh_enabled"] is False
        ),
        "model_scan_precip_nowcast_fact_refresh_lane_wait_zero": (
            float(cfg["model_scan_precip_nowcast_fact_refresh_lane_wait_sec"]) <= 0.0
        ),
        "model_scan_precip_nowcast_fact_refresh_db_wait_zero": (
            float(cfg["model_scan_precip_nowcast_fact_refresh_db_lock_wait_sec"]) <= 0.0
        ),
        "model_scan_facts_materialize_on_yield_default_off": (
            cfg["model_scan_facts_materialize_on_yield_enabled"] is False
        ),
        "model_scan_facts_materialize_on_yield_lane_wait_zero": (
            float(cfg["model_scan_facts_materialize_on_yield_lane_wait_sec"]) <= 0.0
        ),
        "model_scan_facts_materialize_on_yield_db_wait_zero": (
            float(cfg["model_scan_facts_materialize_on_yield_db_lock_wait_sec"]) <= 0.0
        ),
        "facts_materialize_after_scan_default_off": cfg["facts_materialize_after_scan"] is False,
        "model_scan_hourly_facts_materialize_default_off": (
            cfg["model_scan_hourly_facts_materialize_enabled"] is False
        ),
        "awc_recent_window_preserves_local_day_max": (
            cfg["awc_recent_window_preserve_local_day_max"] is True
        ),
        "awc_local_day_margin_hours_gte_2": (
            float(cfg["awc_local_day_margin_hours"]) >= 2.0
        ),
        "awc_local_day_min_hours_gte_8": (
            float(cfg["awc_local_day_min_hours"]) >= 8.0
        ),
        "market_ws_defers_during_live_ssot": (
            cfg["market_ws_defer_during_live_ssot"] is True
        ),
        "market_ws_db_busy_timeout_lte_500ms": (
            float(cfg["market_ws_db_busy_timeout_ms"]) <= 500.0
        ),
        "market_ws_write_retry_attempts_lte_2": (
            float(cfg["market_ws_write_retry_attempts"]) <= 2.0
        ),
        "ui_summary_market_hydrate_disabled": (
            cfg["ui_snapshot_summary_live_market_hydrate"] is False
        ),
        "ui_max_concurrent_lte_6": float(cfg["ui_max_concurrent_requests"]) <= 6.0,
        "ui_hard_cap_lte_6": float(cfg["ui_max_concurrent_requests_hard_cap"]) <= 6.0,
        "ui_priority_concurrent_lte_2": (
            float(cfg["ui_priority_max_concurrent_requests"]) <= 2.0
        ),
        "ui_heavy_concurrent_lte_1": (
            float(cfg["ui_heavy_max_concurrent_requests"]) <= 1.0
        ),
        "ui_request_queue_lte_128": float(cfg["ui_request_queue_size"]) <= 128.0,
        "most_likely_concurrent_lte_1": (
            float(cfg["most_likely_max_concurrency"]) <= 1.0
        ),
        "most_likely_history_row_limit_lte_24": (
            float(cfg["most_likely_history_row_limit"]) <= 24.0
        ),
        "most_likely_history_query_timeout_lte_1s": (
            float(cfg["most_likely_history_query_timeout_sec"]) <= 1.0
        ),
        "most_likely_prewarm_all_weights_disabled": (
            cfg["most_likely_prewarm_all_weights"] is False
        ),
        "hard_source_preempt_noncritical_refresh_enabled": (
            cfg["hard_source_preempt_noncritical_refresh_enabled"] is True
        ),
        "preempt_owner_set_complete": required_preempt_owners.issubset(preempt_owners),
        "live_owner_set_complete": required_live_owners.issubset(live_owners),
        "model_owner_set_complete": required_model_owners.issubset(model_owners),
        "market_owner_set_complete": required_market_owners.issubset(market_owners),
        "data_source_owner_set_complete": required_data_source_owners.issubset(data_source_owners),
        "facts_write_owner_set_minimal": facts_write_owners == required_facts_write_owners,
        "awc_lock_guard_enabled": cfg["awc_lock_guard"] is True,
        "owner_role_enforcement_enabled": cfg["enforce_owner_role"] is True,
        "nonlive_awc_refresh_blocked": cfg["allow_nonlive_awc_refresh"] is False,
        "scan_facts_materialize_requires_explicit_override": (
            cfg["allow_scan_facts_materialize"] is False
        ),
    }
    script_requirements: dict[str, list[str]] = {
        "scripts/lib/weather_refresh_coordinator.sh": [
            "weather_refresh_apply_safe_defaults",
            "PM_REFRESH_COORDINATOR_LANE_NAME:=weather_refresh_lane",
            "PM_REFRESH_COORDINATOR_ROLE_CONTRACT_VERSION:=refresh_coordinator_v3",
            "PM_MODEL_SCAN_LANE_NAME:=${PM_REFRESH_COORDINATOR_LANE_NAME}",
            "PM_MARKET_REFRESH_LANE_NAME:=${PM_REFRESH_COORDINATOR_LANE_NAME}",
            "PM_DAILY_SCAN_LANE_NAME:=${PM_REFRESH_COORDINATOR_LANE_NAME}",
            "PM_REFRESH_COORDINATOR_LIVE_OWNERS:=",
            "PM_REFRESH_COORDINATOR_MODEL_OWNERS:=",
            "PM_REFRESH_COORDINATOR_MARKET_OWNERS:=",
            "PM_REFRESH_COORDINATOR_DATA_SOURCE_OWNERS:=",
            "PM_REFRESH_COORDINATOR_AWC_LOCK_GUARD:=1",
            "PM_REFRESH_COORDINATOR_ENFORCE_OWNER_ROLE:=1",
            "PM_REFRESH_COORDINATOR_ALLOW_NONLIVE_AWC_REFRESH:=0",
            "PM_REFRESH_COORDINATOR_FACTS_WRITE_OWNERS:=weatherctl_facts,daily_scan",
            "PM_REFRESH_COORDINATOR_ALLOW_SCAN_FACTS_MATERIALIZE:=0",
            "weather_refresh_register_entrypoint",
            "weather_refresh_role_for_owner",
            "weather_refresh_owner_is_live",
            "weather_refresh_awc_refresh_allowed",
            "PM_WEATHER_DISCOVERY_COORDINATOR_LANE_ENABLED:=1",
            "PM_FACTS_MATERIALIZE_AFTER_SCAN:=0",
            "PM_WEATHER_DISCOVERY_KICK_MODEL_SCAN:=0",
            "PM_MODEL_SCAN_AWC_FACT_REFRESH_ENABLED:=0",
            "PM_MODEL_SCAN_AWC_FACT_REFRESH_PRE_LANE_ENABLED:=0",
            "PM_MODEL_SCAN_AWC_FACT_REFRESH_LANE_WAIT_SEC:=0",
            "PM_MODEL_SCAN_AWC_FACT_REFRESH_DB_LOCK_WAIT_SEC:=0",
            "PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_LANE_WAIT_SEC:=0",
            "PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_DB_LOCK_WAIT_SEC:=0",
            "PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD:=0",
            "PM_MODEL_SCAN_HOURLY_FACTS_MATERIALIZE_ENABLED:=0",
            "PM_MARKET_WS_DEFER_DURING_LIVE_SSOT:=1",
            "PM_HARD_SOURCE_PREEMPT_NONCRITICAL_REFRESH_ENABLED:=1",
        ],
        "scripts/model_scan.sh": [
            "weather_refresh_coordinator.sh",
            "weather_refresh_register_entrypoint \"model_scan\" \"model\"",
            "weather_refresh_skip_if_live_ssot \"model_scan\"",
            "weather_scan_lane_acquire \"model_scan\"",
            "coordinator_child_writer_boundary:model_scan",
            "coordinator_child_writer_boundary:model_scan_awc_fact_refresh",
            "weather_refresh_awc_refresh_allowed \"model_scan_awc_fact_refresh\"",
        ],
        "scripts/market_refresh_1m.sh": [
            "weather_refresh_coordinator.sh",
            "weather_refresh_register_entrypoint \"market_refresh\" \"market\"",
            "weather_refresh_skip_if_live_ssot \"market_refresh\"",
            "weather_scan_lane_acquire \"market_refresh\"",
            "weather_refresh_db_writer_acquire \"market_refresh\"",
            "weather_scan_lane_acquire \"weather_discovery\"",
            "coordinator_child_writer_boundary:market_refresh_discovery",
            "PM_WEATHER_DISCOVERY_KICK_MODEL_SCAN",
        ],
        "scripts/market_refresh_frequent.sh": [
            "weather_refresh_coordinator.sh",
            "weather_refresh_register_entrypoint \"market_refresh_frequent\" \"market\"",
            "weather_refresh_skip_if_live_ssot \"market_refresh_frequent\"",
            "weather_scan_lane_acquire \"market_refresh_frequent\"",
            "weather_refresh_db_writer_acquire \"market_refresh_frequent\"",
        ],
        "scripts/market_refresh_urgent.sh": [
            "weather_refresh_coordinator.sh",
            "weather_refresh_register_entrypoint \"market_refresh_urgent\" \"market\"",
            "weather_refresh_skip_if_live_ssot \"market_refresh_urgent\"",
            "weather_scan_lane_acquire \"market_refresh_urgent\"",
            "weather_refresh_db_writer_acquire \"market_refresh_urgent\"",
        ],
        "scripts/market_ws_listener.py": [
            "_live_ssot_writer_active",
            "PM_MARKET_WS_DEFER_DURING_LIVE_SSOT",
            "PM_MARKET_WS_DB_BUSY_TIMEOUT_MS\", \"250\"",
            "PM_MARKET_WS_WRITE_RETRY_ATTEMPTS\", \"2\"",
        ],
        "scripts/model_scan_hourly.sh": [
            "weather_refresh_coordinator.sh",
            "weather_refresh_register_entrypoint \"model_scan_hourly\" \"model\"",
            "weather_refresh_skip_if_live_ssot \"model_scan_hourly\"",
            "weather_scan_lane_acquire \"model_scan_hourly\"",
            "Do not hold the archive writer lock around the network/model scan",
        ],
        "scripts/daily_scan_and_settlement.sh": [
            "weather_refresh_coordinator.sh",
            "weather_refresh_register_entrypoint \"daily_scan\" \"data_source\"",
            "weather_refresh_skip_if_live_ssot \"daily_scan\"",
            "weather_scan_lane_acquire \"daily_scan\"",
            "weather_refresh_db_writer_acquire \"daily_scan\"",
            "daily_refresh_release_locks",
        ],
        "scripts/model_release_watcher_tick.sh": [
            "weather_refresh_coordinator.sh",
            "weather_refresh_register_entrypoint \"model_release_watcher\" \"model\"",
            "weather_refresh_skip_if_live_ssot \"model_release_watcher\"",
        ],
        "scripts/weatherctl.sh": [
            "weather_refresh_coordinator.sh",
            "weather_refresh_register_entrypoint \"weatherctl_facts\" \"data_source\"",
            "weather_refresh_skip_if_live_ssot \"weatherctl_facts\"",
            "weather_scan_lane_acquire \"weatherctl_facts\"",
            "weather_refresh_db_writer_acquire \"weatherctl_facts\"",
        ],
        "ui/server.py": [
            "UI_HEAVY_REQUEST_PATHS",
            "UI_HEAVY_MAX_CONCURRENT_REQUESTS",
            "_heavy_request_slots",
            "_send_api_busy_and_close",
            "MOST_LIKELY_HISTORY_ROW_LIMIT",
            "MOST_LIKELY_HISTORY_QUERY_TIMEOUT_SEC",
            "MOST_LIKELY_PREWARM_ALL_WEIGHTS",
            "event_fact_summary_cache",
            "set_progress_handler",
            "PM_AWC_FACT_REFRESH_RECENT_WINDOW_PRESERVE_LOCAL_DAY_MAX",
            "local_day_max_window_preserved",
            "awc_today_max_source",
            "merged_fact_payload",
        ],
        "scan_weather_market.py": [
            "def _posterior_target_primary_entry_fast_path()",
            "PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ENTRY_FAST_SUBMIT",
            "direct_target_order_fast_path",
            "direct_target_fast_prepare",
            "direct_target_fast_submit",
            "direct_target_order_stage_timings",
            "order_stderr",
            "order_stdout",
            "order_raw_payload_json",
        ],
    }
    script_forbidden_defaults: dict[str, list[str]] = {
        "scripts/model_scan.sh": [
            "PM_FACTS_MATERIALIZE_AFTER_SCAN:=1",
            "PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD:-1",
            "PM_MODEL_SCAN_AWC_FACT_REFRESH_PRE_LANE_ENABLED:-1",
            "weather_refresh_configure_lane \"${PM_MODEL_SCAN_LANE_NAME:-model_scan_lane}\"",
        ],
        "scripts/market_refresh_1m.sh": [
            "PM_FACTS_MATERIALIZE_AFTER_SCAN:=1",
            "PM_WEATHER_DISCOVERY_KICK_MODEL_SCAN:-1",
            "weather_refresh_configure_lane \"${PM_MARKET_REFRESH_LANE_NAME:-market_refresh_lane}\"",
        ],
        "scripts/market_refresh_frequent.sh": [
            "weather_refresh_configure_lane \"${PM_MARKET_REFRESH_LANE_NAME:-market_refresh_lane}\"",
        ],
        "scripts/market_refresh_urgent.sh": [
            "weather_refresh_configure_lane \"${PM_MARKET_REFRESH_LANE_NAME:-market_refresh_lane}\"",
        ],
        "scripts/model_scan_hourly.sh": [
            "weather_refresh_configure_lane \"${PM_MODEL_SCAN_LANE_NAME:-model_scan_lane}\"",
        ],
        "scripts/daily_scan_and_settlement.sh": [
            "weather_refresh_configure_lane \"${PM_DAILY_SCAN_LANE_NAME:-daily_scan_lane}\"",
        ],
        "scripts/weatherctl.sh": [
            "weather_refresh_configure_lane \"${PM_WEATHERCTL_LANE_NAME:-weatherctl_lane}\"",
        ],
    }
    script_checks: dict[str, Any] = {}
    for rel_path, fragments in script_requirements.items():
        path = ROOT / rel_path
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            script_checks[rel_path] = {
                "ok": False,
                "missing": fragments,
                "error": f"{type(exc).__name__}: {exc}",
            }
            continue
        missing = [fragment for fragment in fragments if fragment not in text]
        forbidden = [
            fragment
            for fragment in script_forbidden_defaults.get(rel_path, [])
            if fragment in text
        ]
        script_checks[rel_path] = {
            "ok": not missing and not forbidden,
            "missing": missing,
            "forbidden_defaults": forbidden,
        }
    checks["refresh_entrypoints_use_coordinator"] = all(
        item.get("ok") for item in script_checks.values()
    )
    return {
        "ok": all(checks.values()),
        "config": cfg,
        "checks": checks,
        "script_checks": script_checks,
        "failed_checks": [key for key, passed in checks.items() if not passed],
    }


def _audit_active_auto_trade_jobs() -> dict[str, Any]:
    if not ARCHIVE_DB.exists():
        return {"ok": False, "reason": "archive_db_missing", "db": str(ARCHIVE_DB)}
    try:
        conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        return {
            "ok": False,
            "reason": "archive_db_open_failed",
            "db": str(ARCHIVE_DB),
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        rows = conn.execute(
            """
            SELECT id, state, source, priority, created_at, started_at, lease_until, last_error
            FROM auto_trade_jobs
            WHERE state IN ('queued','running','retrying')
            ORDER BY priority DESC, id ASC
            LIMIT 50
            """
        ).fetchall()
    except Exception as exc:
        conn.close()
        return {
            "ok": False,
            "reason": "active_jobs_query_failed",
            "db": str(ARCHIVE_DB),
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass

    now_ts = time.time()
    direct_stale_sec = _env_float("PM_POSTERIOR_TARGET_DIRECT_ACTIVE_JOB_STALE_SEC", 30.0)
    non_direct_stale_sec = _env_float("PM_AUTO_TRADE_NON_DIRECT_ACTIVE_JOB_STALE_SEC", 5.0)
    jobs = []
    stale_direct_jobs = []
    stale_non_direct_entry_jobs = []
    for row in rows:
        item = dict(row)
        created_ts = _parse_iso_utc(item.get("created_at"))
        started_ts = _parse_iso_utc(item.get("started_at"))
        lease_ts = _parse_iso_utc(item.get("lease_until"))
        age_ts = started_ts or created_ts
        age_sec = max(0.0, now_ts - age_ts) if age_ts else None
        item["age_sec"] = round(float(age_sec), 3) if age_sec is not None else None
        item["lease_expired"] = bool(lease_ts and lease_ts < now_ts)
        jobs.append(item)
        if str(item.get("source") or "") != "posterior_target_direct":
            source_text = str(item.get("source") or "").strip()
            state_text = str(item.get("state") or "").strip()
            if (
                state_text == "running"
                and (
                    source_text == "entry_1m_strategy_batch"
                    or source_text.startswith("entry_1m")
                )
                and age_sec is not None
                and age_sec > non_direct_stale_sec
            ):
                stale_non_direct_entry_jobs.append(item)
            continue
        if item["lease_expired"] or (age_sec is not None and age_sec > direct_stale_sec):
            stale_direct_jobs.append(item)
    return {
        "ok": not bool(stale_direct_jobs or stale_non_direct_entry_jobs),
        "db": str(ARCHIVE_DB),
        "active_count": len(jobs),
        "posterior_target_direct_stale_sec": direct_stale_sec,
        "non_direct_entry_stale_sec": non_direct_stale_sec,
        "jobs": jobs,
        "stale_posterior_target_direct_jobs": stale_direct_jobs,
        "stale_non_direct_entry_jobs": stale_non_direct_entry_jobs,
    }


def _is_same_fs_path(left: str, right: str) -> bool:
    try:
        left_path = os.path.realpath(os.path.expanduser(str(left).strip()))
        right_path = os.path.realpath(os.path.expanduser(str(right).strip()))
    except Exception:
        return False
    return left_path == right_path and bool(left_path)


def main() -> int:
    blockers: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}
    _load_dotenv()
    monitor_signal_log = str(os.environ.get("MONITOR_SIGNAL_LOG") or "").strip()
    poly_signal_path = str(os.environ.get("POLY_SIGNAL_PATH") or "").strip()
    details["monitor_signal_log"] = monitor_signal_log
    details["poly_signal_path"] = poly_signal_path
    details["monitor_signal_log_equals_poly_signal_path"] = bool(
        monitor_signal_log and poly_signal_path and _is_same_fs_path(monitor_signal_log, poly_signal_path)
    )
    if details["monitor_signal_log_equals_poly_signal_path"]:
        blockers.append("monitor_signal_log_pointing_to_poly_signal_path")
    hard_source_direct_enabled = str(os.environ.get("PM_HARD_SOURCE_DIRECT_ENABLED", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    details["hard_source_direct_enabled"] = hard_source_direct_enabled
    direct_scheduler_audit = _audit_direct_scheduler_config()
    details["direct_scheduler_config"] = direct_scheduler_audit
    if not direct_scheduler_audit.get("ok"):
        blockers.append("direct_scheduler_config_not_low_latency")
    clob_order_service_audit = _audit_clob_order_service()
    details["clob_order_service"] = clob_order_service_audit
    if clob_order_service_audit.get("enabled") and not clob_order_service_audit.get("ok"):
        blockers.append("clob_order_service_not_ready")
    refresh_coordinator_audit = _audit_refresh_coordinator_config()
    details["refresh_coordinator_config"] = refresh_coordinator_audit
    if not refresh_coordinator_audit.get("ok"):
        blockers.append("refresh_coordinator_config_not_unified")
    active_job_audit = _audit_active_auto_trade_jobs()
    details["active_auto_trade_jobs"] = active_job_audit
    if not active_job_audit.get("ok"):
        if active_job_audit.get("stale_posterior_target_direct_jobs"):
            blockers.append("posterior_target_direct_active_job_stale")
        else:
            warnings.append("active_auto_trade_jobs_audit_failed")

    launch_rows = _launchctl_list()
    details["launchctl_weather"] = launch_rows
    ps_rows = _ps_weather_rows()
    label_paths = _plist_labels()
    duplicate_labels = {label: paths for label, paths in label_paths.items() if len(paths) > 1}
    details["duplicate_launchagent_labels"] = duplicate_labels
    for label in duplicate_labels:
        blockers.append(f"{label}_duplicate_launchagent_label")

    for label, expected_fragment in WEATHER_REQUIRED.items():
        row = launch_rows.get(label)
        if row is None:
            blockers.append(f"{label}_not_loaded")
            continue
        plist = _plist(label)
        if row.get("pid") in {"", "-"} and _is_keepalive(plist) and not _is_interval_job(plist):
            blockers.append(f"{label}_not_running")
        text = _program_text(plist)
        if expected_fragment and expected_fragment not in text:
            warnings.append(f"{label}_program_drift")

    ui_port = str(os.environ.get("PM_UI_PORT") or "3002").strip() or "3002"
    heartbeat_url = f"http://127.0.0.1:{ui_port}/api/heartbeat"
    heartbeat_ok, heartbeat_payload, heartbeat_error, heartbeat_ms = _http_json(
        heartbeat_url,
        timeout=_env_float("PM_UI_HEARTBEAT_AUDIT_TIMEOUT_SEC", 2.0),
    )
    details["ui_heartbeat_audit"] = {
        "ok": bool(heartbeat_ok and heartbeat_payload.get("ok") is True),
        "url": heartbeat_url,
        "elapsed_ms": round(float(heartbeat_ms), 3),
        "payload": heartbeat_payload,
        "error": heartbeat_error,
    }
    if not details["ui_heartbeat_audit"]["ok"]:
        blockers.append("ui_heartbeat_unavailable")
    elif heartbeat_ms > 1000.0:
        warnings.append("ui_heartbeat_slow")

    trade_engine_row = launch_rows.get("com.weather.trade_engine")
    trade_engine_proc = _process_by_pid(ps_rows, trade_engine_row.get("pid") if trade_engine_row else "")
    details["trade_engine_process"] = trade_engine_proc or {}
    if trade_engine_proc:
        trade_engine_fresh_mtime = _latest_mtime(
            (
                ENV_PATH,
                ROOT / "scripts" / "trade_engine_loop.sh",
            )
        )
        trade_engine_started_at = _process_started_at(trade_engine_proc)
        details["trade_engine_started_after_env_mtime"] = bool(
            trade_engine_started_at + 1.0 >= trade_engine_fresh_mtime
        )
        if (
            trade_engine_fresh_mtime
            and trade_engine_started_at
            and trade_engine_started_at + 1.0 < trade_engine_fresh_mtime
        ):
            blockers.append("trade_engine_stale_env_or_code")

    auto_trade_worker_fragment = str(ROOT / "scan_weather_market.py")
    active_auto_trade_job_workers = [
        row
        for row in ps_rows
        if auto_trade_worker_fragment in str(row.get("command") or "")
        and "--auto-trade-job-worker" in str(row.get("command") or "")
    ]
    details["auto_trade_job_worker_processes"] = active_auto_trade_job_workers
    scan_code_mtime = _latest_mtime((ROOT / "scan_weather_market.py",))
    stale_worker_pids = []
    for worker_row in active_auto_trade_job_workers:
        worker_started_at = _process_started_at(worker_row)
        if scan_code_mtime and worker_started_at and worker_started_at + 1.0 < scan_code_mtime:
            stale_worker_pids.append(worker_row.get("pid"))
    details["auto_trade_job_worker_started_after_scan_code_mtime"] = not bool(stale_worker_pids)
    if stale_worker_pids:
        details["auto_trade_job_worker_stale_pids"] = stale_worker_pids
        blockers.append("auto_trade_job_worker_stale_code")

    for domain_label, expected_fragment in WEATHER_MONITOR_REQUIRED.items():
        source_plist = _load_plist_path(SOURCE_MONITOR_DAEMON_PLIST)
        source_program = _program_text(source_plist)
        installed_plist = _load_plist_path(MONITOR_DAEMON_PLIST)
        installed_program = _program_text(installed_plist)
        installed_workdir = str(installed_plist.get("WorkingDirectory") or "").strip()
        details[f"{domain_label}_source_plist_path"] = str(SOURCE_MONITOR_DAEMON_PLIST)
        details[f"{domain_label}_source_plist_exists"] = SOURCE_MONITOR_DAEMON_PLIST.exists()
        details[f"{domain_label}_source_plist_program"] = source_program
        details[f"{domain_label}_source_plist_fragment_ok"] = _program_contains_expected(
            source_program,
            expected_fragment,
        )
        details[f"{domain_label}_plist_path"] = str(MONITOR_DAEMON_PLIST)
        details[f"{domain_label}_plist_exists"] = MONITOR_DAEMON_PLIST.exists()
        details[f"{domain_label}_plist_working_directory"] = installed_workdir
        details[f"{domain_label}_plist_program"] = installed_program
        details[f"{domain_label}_installed_matches_source"] = bool(
            source_program and installed_program and source_program == installed_program
        )
        if source_program and installed_program and source_program != installed_program:
            warnings.append(f"{domain_label}_installed_plist_not_updated_from_source")
        if not MONITOR_DAEMON_PLIST.exists():
            blockers.append(f"{domain_label}_plist_missing")
        else:
            if installed_workdir and not _is_same_fs_path(installed_workdir, str(ROOT)):
                blockers.append(f"{domain_label}_plist_working_directory_drift")
            if expected_fragment and not _program_contains_expected(installed_program, expected_fragment):
                blockers.append(f"{domain_label}_plist_program_drift")
            if str(ROOT) not in installed_program and expected_fragment:
                blockers.append(f"{domain_label}_plist_root_drift")
        ok, row = _launchctl_print(domain_label)
        if not ok:
            blockers.append(f"{domain_label}_not_loaded")
            details[f"{domain_label}_repair_command"] = (
                "sudo bash scripts/install_monitor_launchdaemon.sh && "
                f"sudo launchctl kickstart -k {MONITOR_DAEMON_DOMAIN_LABEL}"
            )
            continue
        if row.get("status") != "running":
            blockers.append(f"{domain_label}_not_running")
        details[domain_label] = row
        if expected_fragment:
            # For monitor daemon health we only support one source command.
            details[f"{domain_label}_fragment_ok"] = True

        monitor_pid = None
        try:
            monitor_pid = int(str(row.get("pid") or "").strip())
        except Exception:
            monitor_pid = None
        poller_fragment = str(ROOT / "scripts" / "hard_source_direct_poller.py")
        poller_rows = [
            item
            for item in ps_rows
            if poller_fragment in str(item.get("command") or "")
            and " --loop" in str(item.get("command") or "")
        ]
        details["hard_source_direct_poller_processes"] = poller_rows
        if hard_source_direct_enabled:
            if not poller_rows:
                blockers.append("hard_source_direct_poller_loop_not_running")
            elif len(poller_rows) > 1:
                blockers.append("hard_source_direct_poller_loop_duplicate")
            else:
                poller = poller_rows[0]
                if monitor_pid is not None and int(poller.get("ppid") or -1) != monitor_pid:
                    blockers.append("hard_source_direct_poller_not_child_of_monitor_daemon")
                latest_code_mtime = 0.0
                for code_path in (
                    ENV_PATH,
                    ROOT / "scripts" / "hard_source_direct_poller.py",
                    ROOT / "scan_weather_market.py",
                ):
                    try:
                        latest_code_mtime = max(latest_code_mtime, code_path.stat().st_mtime)
                    except OSError:
                        pass
                try:
                    process_started_at = time.time() - float(poller.get("etimes") or 0)
                except Exception:
                    process_started_at = 0.0
                details["hard_source_direct_poller_started_after_code_mtime"] = bool(
                    process_started_at + 1.0 >= latest_code_mtime
                )
                if latest_code_mtime and process_started_at and process_started_at + 1.0 < latest_code_mtime:
                    blockers.append("hard_source_direct_poller_stale_code")

    if not hard_source_direct_enabled:
        details["awc_source_health_audit_skipped"] = "hard_source_direct_disabled"
    elif AWC_SOURCE_HEALTH_AUDIT.exists():
        returncode, audit_payload, audit_error = _run_json(
            [sys.executable or "python3", str(AWC_SOURCE_HEALTH_AUDIT)],
            timeout=12,
        )
        details["awc_source_health_audit"] = audit_payload
        details["awc_source_health_audit_returncode"] = returncode
        if audit_error:
            details["awc_source_health_audit_error"] = audit_error
        if returncode != 0 or not audit_payload.get("ok"):
            for blocker in audit_payload.get("blockers") or []:
                blockers.append(f"awc_source_health:{blocker}")
            if not audit_payload.get("blockers"):
                blockers.append("awc_source_health_audit_failed")
        for warning in audit_payload.get("warnings") or []:
            warnings.append(f"awc_source_health:{warning}")
    else:
        warnings.append("awc_source_health_audit_missing")

    if not hard_source_direct_enabled:
        details["posterior_target_live_job_audit_skipped"] = "hard_source_direct_disabled"
    elif POSTERIOR_TARGET_LIVE_JOB_AUDIT.exists():
        returncode, audit_payload, audit_error = _run_json(
            [sys.executable or "python3", str(POSTERIOR_TARGET_LIVE_JOB_AUDIT)],
            timeout=12,
        )
        details["posterior_target_live_job_audit"] = audit_payload
        details["posterior_target_live_job_audit_returncode"] = returncode
        if audit_error:
            details["posterior_target_live_job_audit_error"] = audit_error
        if returncode != 0 or not audit_payload.get("ok"):
            for blocker in audit_payload.get("blockers") or []:
                blockers.append(f"posterior_target_live_job:{blocker}")
            if not audit_payload.get("blockers"):
                blockers.append("posterior_target_live_job_audit_failed")
        for warning in audit_payload.get("warnings") or []:
            if warning == "posterior_target_postfix_job_pending":
                continue
            warnings.append(f"posterior_target_live_job:{warning}")
    else:
        warnings.append("posterior_target_live_job_audit_missing")

    if ORDER_FAILURE_PAYLOAD_TRACE_AUDIT.exists():
        returncode, audit_payload, audit_error = _run_json(
            [sys.executable or "python3", str(ORDER_FAILURE_PAYLOAD_TRACE_AUDIT)],
            timeout=12,
        )
        details["order_failure_payload_trace_audit"] = audit_payload
        details["order_failure_payload_trace_audit_returncode"] = returncode
        if audit_error:
            details["order_failure_payload_trace_audit_error"] = audit_error
        if returncode != 0 or not audit_payload.get("ok"):
            blockers.append("order_failure_payload_trace_audit_failed")
    else:
        warnings.append("order_failure_payload_trace_audit_missing")

    for label in WEATHER_LEGACY_MONITORS:
        launch_row = launch_rows.get(label)
        if launch_row is not None and launch_row.get("pid") not in {"", "-"}:
            blockers.append(f"legacy_monitor_label_running:{label}")
            details[f"{label}_launchctl_list"] = launch_row
        ok, row = _launchctl_print(label)
        if ok and row.get("status") == "running":
            blockers.append(f"legacy_monitor_label_running:{label}")
        elif ok:
            details[f"{label}_loaded"] = row
    for path in sorted(WEATHER_LEGACY_MONITOR_PLISTS):
        if path.exists():
            detail_key = "legacy_monitor_plist_exists:" + str(path)
            details[detail_key] = True
            blockers.append(f"legacy_monitor_plist_exists:{path}")

    for label in WEATHER_SCHEDULED:
        if not (LAUNCH_AGENTS / f"{label}.plist").exists():
            continue
        if label not in launch_rows:
            warnings.append(f"{label}_not_loaded")

    for label in WEATHER_LEGACY_CONFLICTS:
        row = launch_rows.get(label)
        if row is not None and row.get("pid") not in {"", "-"}:
            blockers.append(f"{label}_legacy_conflict_running")

    ps_text = _ps_weather()
    manual_patterns = {"scripts/auto_trade_worker_frequent.sh": "legacy_worker_frequent_running"}
    running_patterns = []
    for pattern, warning in manual_patterns.items():
        if pattern in ps_text:
            warnings.append(warning)
            running_patterns.append(pattern)
    details["manual_patterns_seen"] = running_patterns

    output = {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "details": details,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
