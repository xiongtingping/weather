#!/usr/bin/env python3
"""Controlled system-domain cutover for the unique reported-trajectory-NO owner."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "archive.db"
CONFIG_PATH = ROOT / "config" / "trading" / "reported_trajectory_no_v1.json"
PAUSE = ROOT / "data" / "live_paused.flag"
CORE_ONLY_FLAG = ROOT / "data" / "reported_trajectory_core_only.enabled"
SOURCE_PLIST = ROOT / "launchd" / "com.weather.reported_trajectory_no.plist"
LABEL = "com.weather.reported_trajectory_no"
GATEWAY_LABEL = "com.weather.clob_order_service"
MARKET_WS_LABEL = "com.weather.market_ws"
LEGACY_LABELS = (
    "com.weather.dynamic_three_leg_execution",
    "com.weather.trade_engine",
    "com.weather.auto_trade_entry_1m",
    "com.weather.auto_trade_worker",
    "com.weather.target_realization_worker",
    "com.weather.trade_sync_loop",
)
NON_CORE_LABELS = (
    *LEGACY_LABELS,
    "com.weather.archive_db_auto_guard",
    "com.weather.archive_db_governance_offpeak",
    "com.weather.cleanup_weekly",
    "com.weather.daily_scan",
    "com.weather.daily_trade_sms_summary",
    "com.weather.feedback_loop_hourly",
    "com.weather.market_refresh_1m",
    "com.weather.market_refresh_urgent",
    "com.weather.model_scan",
    "com.weather.model_scan_hourly",
    "com.weather.monitor.daemon",
    "com.weather.q_calibrated_writeback_hourly",
    "com.weather.task_queue_worker",
    "com.xiong.weather.ui",
)
ACTIONABLE_LEGACY_STATES = (
    "prepared", "claimed", "submitting", "gateway_accepted", "reconcile_required",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=check)


def launch_target(label: str) -> str:
    return f"system/{label}"


def user_launch_target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def launch_loaded(label: str) -> bool:
    result = command("launchctl", "print", launch_target(label), check=False)
    return result.returncode == 0 and "Could not find service" not in result.stdout + result.stderr and "Bad request" not in result.stdout + result.stderr


def stop_and_disable(label: str) -> None:
    target = user_launch_target(label)
    command("launchctl", "bootout", target, check=False)
    command("launchctl", "disable", target, check=False)


def user_launch_loaded(label: str) -> bool:
    result = command("launchctl", "print", user_launch_target(label), check=False)
    return result.returncode == 0 and "Could not find service" not in result.stdout + result.stderr


def system_service_pid(label: str) -> int:
    result = command("launchctl", "print", launch_target(label), check=False)
    if result.returncode != 0:
        raise RuntimeError(f"system_service_not_loaded:{label}")
    match = re.search(r"\bpid = (\d+)", result.stdout)
    if not match:
        raise RuntimeError(f"system_service_pid_missing:{label}")
    return int(match.group(1))


def restart_system_service(label: str, *, timeout_sec: float = 25.0) -> int:
    """Restart a UserName-owned LaunchDaemon without interactive sudo."""

    prior_pid = system_service_pid(label)
    os.kill(prior_pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            pid = system_service_pid(label)
        except RuntimeError:
            time.sleep(0.1)
            continue
        if pid != prior_pid:
            return pid
        time.sleep(0.1)
    raise RuntimeError(f"system_service_restart_timeout:{label}:{prior_pid}")


def wait_market_ws(timeout_sec: float = 35.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = gateway_health()
            if (
                last.get("ws_snapshot_ready") is True
                and int(last.get("ws_active_shards") or 0) == int(last.get("ws_expected_shards") or 0)
                and int(last.get("ws_materialized_token_count") or 0)
                == int(last.get("ws_subscribed_token_count") or 0)
            ):
                return last
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"market_ws_full_materialization_timeout:{last}")


def gateway_health() -> dict[str, Any]:
    with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=2.0) as response:
        return dict(json.loads(response.read().decode("utf-8")))


def terminate_unmanaged_gateway() -> list[int]:
    """Retire an orphan gateway only after the global live pause is present."""

    if launch_loaded(GATEWAY_LABEL):
        return []
    needle = str(ROOT / "scripts" / "clob_order_service.mjs")
    result = command("ps", "-axo", "pid=,command=")
    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2 or needle not in parts[1]:
            continue
        pid = int(parts[0])
        if pid != os.getpid():
            os.kill(pid, signal.SIGTERM)
            pids.append(pid)
    deadline = time.monotonic() + 3.0
    while pids and time.monotonic() < deadline:
        pids = [pid for pid in pids if Path(f"/proc/{pid}").exists()] if Path("/proc").exists() else [
            pid for pid in pids if command("kill", "-0", str(pid), check=False).returncode == 0
        ]
        if pids:
            time.sleep(0.05)
    for pid in pids:
        os.kill(pid, signal.SIGKILL)
    return pids


def wait_gateway(*, require_prefetched: bool, timeout_sec: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = gateway_health()
            ready = last.get("ready") is True and last.get("read_only") is not True
            prefetched = last.get("local_prefetched_evidence_ready") is True
            if ready and (prefetched or not require_prefetched):
                return last
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"clob_gateway_readiness_timeout:{last}")


def install_plist_without_start() -> Path:
    destination = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with SOURCE_PLIST.open("rb") as handle:
        data = plistlib.load(handle)
    data["WorkingDirectory"] = str(ROOT)
    data["ProgramArguments"] = ["/bin/bash", str(ROOT / "scripts" / "reported_trajectory_no_daemon.sh")]
    env = dict(data.get("EnvironmentVariables") or {})
    env.update(
        {
            "HOME": str(Path.home()),
            "USER": os.environ.get("USER", ""),
            "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", "")),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        }
    )
    data["EnvironmentVariables"] = env
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    with temporary.open("wb") as handle:
        plistlib.dump(data, handle, fmt=plistlib.FMT_XML, sort_keys=False)
    temporary.chmod(0o644)
    os.replace(temporary, destination)
    command("plutil", "-lint", str(destination))
    return destination


def bootstrap_user_plist(label: str, plist: Path) -> None:
    deadline = time.monotonic() + 3.0
    last_error = ""
    while time.monotonic() < deadline:
        if launch_loaded(label):
            return
        result = command("launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist), check=False)
        if result.returncode == 0:
            return
        last_error = (result.stderr or result.stdout)[-300:]
        time.sleep(0.1)
    raise RuntimeError(f"launchagent_bootstrap_failed:{label}:{last_error}")


def validate_config(config: dict[str, Any]) -> None:
    if config.get("production_strategy_allowlist") != ["reported_trajectory_no"]:
        raise RuntimeError("strategy_allowlist_not_unique")
    allowlist = config.get("production_source_allowlist")
    if allowlist not in (["amsc"], ["amsc", "awc"]):
        raise RuntimeError("production_source_allowlist_invalid")
    awc_enabled = allowlist == ["amsc", "awc"]
    if awc_enabled and config.get("source_policy", {}).get("awc", {}).get("production_ingest") is not True:
        raise RuntimeError("awc_production_ingest_not_enabled")
    if not awc_enabled and config.get("source_policy", {}).get("awc", {}).get("production_ingest") is not False:
        raise RuntimeError("awc_production_ingest_not_disabled")
    if config.get("order_policy", {}).get("maximum_buy_price") != 0.99:
        raise RuntimeError("maximum_buy_price_not_0_99")
    if config.get("order_policy", {}).get("order_type") != "FAK":
        raise RuntimeError("order_type_not_fak")
    if config.get("order_policy", {}).get("outcome_side") != "NO":
        raise RuntimeError("outcome_side_not_no")
    if config.get("order_policy", {}).get("allow_market") is not False:
        raise RuntimeError("naked_market_order_not_disabled")
    if config.get("order_policy", {}).get("unified_capital_pool") is not True:
        raise RuntimeError("unified_capital_pool_not_enabled")
    if config.get("order_policy", {}).get("deploy_all_available") is not False:
        raise RuntimeError("deploy_all_available_not_disabled")
    if config.get("order_policy", {}).get("maximum_order_notional_usdc") != 50.0:
        raise RuntimeError("maximum_order_notional_not_50")
    if config.get("order_policy", {}).get("collateral_balance_check") is not False:
        raise RuntimeError("collateral_balance_check_not_disabled")
    if config.get("position_policy") != {
        "entry_priority": "NO",
        "existing_yes_positions": "liquidate_same_bucket_before_no_entry",
        "accepted_sell_terminal_before_subsequent_buy": True,
        "automatic_yes_liquidation": True,
        "yes_liquidation_order_type": "FAK",
        "partial_yes_liquidation_blocks_no": True,
        "concurrent_yes_sell_no_buy": False,
    }:
        raise RuntimeError("no_priority_sell_before_buy_policy_not_exact")
    if any(config["source_policy"][lane].get("taf_requested") is not False for lane in ("amsc", "awc")):
        raise RuntimeError("taf_not_disabled")
    exit_policy = config.get("exit_policy", {})
    if exit_policy != {
        "enabled": True, "side": "SELL", "limit_price": 0.99,
        "order_type": "GTC", "post_only": False,
    }:
        raise RuntimeError("exit_0_99_policy_not_exact")


def preflight(conn: sqlite3.Connection, config: dict[str, Any]) -> dict[str, Any]:
    from reported_trajectory_no_schema import apply_reported_trajectory_no_schema

    schema = apply_reported_trajectory_no_schema(conn)
    if not schema["ok"]:
        raise RuntimeError("reported_trajectory_schema_incomplete")
    # Schema application may open an implicit SQLite transaction.  Complete
    # it before the later activation linearization starts BEGIN IMMEDIATE.
    conn.commit()
    health = gateway_health()
    # A resumed cutover intentionally arrives with the shared pause already
    # present.  The gateway must be operational, but the pause itself is not a
    # readiness failure because no exchange POST can cross it.
    if health.get("ready") is not True or health.get("read_only") is True:
        raise RuntimeError("clob_gateway_not_live_ready")
    if health.get("inflight_count") or health.get("idempotency_unresolved_count"):
        raise RuntimeError("clob_gateway_has_unresolved_work")
    placeholders = ",".join("?" for _ in ACTIONABLE_LEGACY_STATES)
    legacy_actionable = int(
        conn.execute(
            f"SELECT COUNT(*) FROM posterior_execution_intents WHERE state IN ({placeholders})",
            ACTIONABLE_LEGACY_STATES,
        ).fetchone()[0]
    )
    if legacy_actionable:
        raise RuntimeError("legacy_actionable_intents_present")
    allowlist = list(config.get("production_source_allowlist") or [])
    registry = conn.execute(
        """SELECT MAX(refreshed_at),
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='amsc' THEN slug||'|'||event_date END),
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='awc' THEN slug||'|'||event_date END),
                  COUNT(DISTINCT CASE WHEN source_eligible=1 AND source_lane='blocked' THEN slug||'|'||event_date END)
           FROM reported_trajectory_market_registry"""
    ).fetchone()
    if not registry or not registry[0] or int(registry[1] or 0) <= 0:
        raise RuntimeError("eligible_market_registry_incomplete")
    if allowlist == ["amsc"] and int(registry[2] or 0) != 0:
        raise RuntimeError("awc_production_registry_not_empty")
    if allowlist == ["amsc", "awc"] and int(registry[2] or 0) <= 0:
        raise RuntimeError("awc_production_registry_incomplete")
    refreshed = datetime.fromisoformat(str(registry[0]).replace("Z", "+00:00")).astimezone(timezone.utc)
    registry_age = (datetime.now(timezone.utc) - refreshed).total_seconds()
    if registry_age < 0 or registry_age > 600:
        raise RuntimeError("market_registry_stale")
    return {
        "gateway_pid": health.get("pid"),
        "legacy_actionable_intents": legacy_actionable,
        "registry_refreshed_at": str(registry[0]),
        "registry_age_sec": round(registry_age, 3),
        "amsc_market_days": int(registry[1] or 0),
        "awc_market_days": int(registry[2] or 0),
        "blocked_market_days": int(registry[3] or 0),
    }


def activate(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    config_sha: str,
    production_source_allowlist: list[str],
) -> dict[str, Any]:
    prior = conn.execute(
        "SELECT generation FROM reported_trajectory_activation_state WHERE singleton=1"
    ).fetchone()
    generation = int(prior[0] or 0) + 1 if prior else 1
    payload = {
        "schema": "reported_trajectory_no_activation_v1",
        "generation": generation,
        "state": "enabled",
        "strategy_id": "reported_trajectory_no_v1",
        "maximum_buy_price": float(config["order_policy"]["maximum_buy_price"]),
        "order_type": "FAK",
        "capital_policy": {
            "unified_pool": True,
            "deploy_all_available": False,
            "maximum_order_notional_usdc": float(config["order_policy"]["maximum_order_notional_usdc"]),
        },
        "position_policy": dict(config["position_policy"]),
        "production_source_allowlist": list(production_source_allowlist),
        "exit_policy": {"side": "SELL", "limit_price": 0.99, "order_type": "GTC"},
        "old_bayes_execution_disabled": True,
        "awc_amsc_disjoint": True,
        "config_sha256": config_sha,
        "activated_at": utc_now(),
    }
    activation_sha = canonical_sha(payload)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """INSERT INTO reported_trajectory_activation_state(
                 singleton,generation,state,activation_sha256,config_sha256,
                 old_bayes_execution_disabled,awc_amsc_disjoint,activation_json,updated_at
               ) VALUES(1,?,'enabled',?,?,1,1,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                 generation=excluded.generation,state=excluded.state,
                 activation_sha256=excluded.activation_sha256,config_sha256=excluded.config_sha256,
                 old_bayes_execution_disabled=1,awc_amsc_disjoint=1,
                 activation_json=excluded.activation_json,updated_at=excluded.updated_at""",
            (generation, activation_sha, config_sha, json.dumps(payload, ensure_ascii=False, sort_keys=True), payload["activated_at"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {**payload, "activation_sha256": activation_sha}


def mark_paused(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE reported_trajectory_activation_state SET state='paused',updated_at=? WHERE singleton=1",
        (utc_now(),),
    )
    conn.commit()


def main() -> int:
    if os.getuid() == 0:
        raise RuntimeError("root_cutover_forbidden_run_as_system_service_owner")
    if len(sys.argv) != 2 or sys.argv[1] != "--confirm-live-reported-trajectory-no-v1":
        print("confirmation_required", file=sys.stderr)
        return 2
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    validate_config(config)
    config_sha = canonical_sha(config)
    # Refresh the official market contract before taking the linearization lock.
    registry_refresh = command(
        sys.executable, str(ROOT / "scripts" / "reported_trajectory_no_daemon.py"), "--once", "registry",
        check=False,
    )
    if registry_refresh.returncode != 0:
        raise RuntimeError(f"official_registry_refresh_failed:{registry_refresh.stderr[-300:]}")
    conn = sqlite3.connect(DB, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    paused = False
    activation: dict[str, Any] | None = None
    try:
        evidence = preflight(conn, config)
        PAUSE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE.write_text(f"reported_trajectory_no_cutover {utc_now()}\n", encoding="utf-8")
        paused = True
        CORE_ONLY_FLAG.write_text(f"reported_trajectory_no_v1 {utc_now()}\n", encoding="utf-8")
        for label in NON_CORE_LABELS:
            stop_and_disable(label)
        stop_deadline = time.monotonic() + 3.0
        while time.monotonic() < stop_deadline and any(user_launch_loaded(label) for label in NON_CORE_LABELS):
            time.sleep(0.05)
        remaining = [label for label in NON_CORE_LABELS if user_launch_loaded(label)]
        if remaining:
            raise RuntimeError("non_core_launchagents_still_loaded:" + ",".join(remaining))

        for core_label in (LABEL, MARKET_WS_LABEL, GATEWAY_LABEL):
            if not launch_loaded(core_label):
                raise RuntimeError(f"system_core_service_not_loaded:{core_label}")
        restart_system_service(GATEWAY_LABEL)
        wait_gateway(require_prefetched=False)
        restart_system_service(MARKET_WS_LABEL)
        wait_market_ws()
        activation = activate(
            conn, config, config_sha, list(config.get("production_source_allowlist") or [])
        )
        restart_system_service(LABEL)
        PAUSE.unlink(missing_ok=True)
        paused = False
        # Existing rows created before the numeric run-id contract are
        # diagnostic-only; normalise them once so dormant UI readers cannot
        # hold or crash a future maintenance query.
        conn.execute(
            "UPDATE auto_trade_decisions SET run_id=0 WHERE decision_id LIKE 'rtdecision_%'"
        )
        conn.commit()
        wait_gateway(require_prefetched=True)
        time.sleep(0.5)
        if not launch_loaded(LABEL):
            raise RuntimeError("reported_trajectory_launchagent_not_loaded")
        output = {
            "schema": "reported_trajectory_no_cutover_v1",
            "ok": True,
            "completed_at": utc_now(),
            "service_domain": "system",
            "non_core_labels_disabled": list(NON_CORE_LABELS),
            "core_labels": [LABEL, MARKET_WS_LABEL, GATEWAY_LABEL],
            "preflight": evidence,
            "activation": activation,
            "historical_shadow_bounds_replayed": False,
            "historical_replay_reason": "prevent_activation_burst_only_next_fresh_bound_advances_trade",
            "sudo_required": False,
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception:
        if activation is not None:
            mark_paused(conn)
        if paused or activation is not None:
            PAUSE.parent.mkdir(parents=True, exist_ok=True)
            PAUSE.write_text(f"reported_trajectory_no_cutover_failed {utc_now()}\n", encoding="utf-8")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
