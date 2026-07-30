#!/usr/bin/env python3
"""Controlled cutover to basket YES + reported-trajectory hard NO."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "archive.db"
PAUSE = ROOT / "data" / "live_paused.flag"
ACTIVATION_PATH = ROOT / "config" / "trading" / "basket_plus_hard_no_v1.json"
LABEL = "com.weather.reported_trajectory_no"
LEGACY_LABELS = ("com.weather.dynamic_three_leg_execution", "com.weather.target_realization_worker")
CONFIRM = "--confirm-live-basket-plus-hard-no"

if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from basket_strategy.activation import activation_contract_sha256, load_activation  # noqa: E402
from basket_strategy.schema import apply_basket_strategy_schema  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], cwd=ROOT, text=True, capture_output=True, check=False)


def _disable_legacy() -> None:
    domain = f"gui/{os.getuid()}"
    for label in LEGACY_LABELS:
        _launchctl("bootout", domain, f"{domain}/{label}")
        _launchctl("disable", f"{domain}/{label}")


def _system_pid(label: str) -> int:
    result = _launchctl("print", f"system/{label}")
    if result.returncode != 0:
        raise RuntimeError(f"system_service_not_loaded:{label}")
    for line in result.stdout.splitlines():
        text = line.strip()
        if text.startswith("pid = "):
            return int(text.split("=", 1)[1].strip())
    raise RuntimeError(f"system_service_pid_missing:{label}")


def _restart_reported_trajectory() -> int:
    prior = _system_pid(LABEL)
    os.kill(prior, signal.SIGTERM)
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        try:
            current = _system_pid(LABEL)
        except RuntimeError:
            time.sleep(0.1)
            continue
        if current != prior:
            return current
        time.sleep(0.1)
    raise RuntimeError(f"reported_trajectory_restart_timeout:{prior}")


def _gateway_ready() -> dict[str, Any]:
    import urllib.request

    with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=2.0) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict) or value.get("ready") is not True or value.get("read_only") is True:
        raise RuntimeError("clob_gateway_not_live_ready")
    if int(value.get("inflight_count") or 0) or int(value.get("idempotency_unresolved_count") or 0):
        raise RuntimeError("clob_gateway_has_unresolved_work")
    return value


def _persist_activation(conn: sqlite3.Connection, activation: dict[str, Any]) -> dict[str, Any]:
    apply_basket_strategy_schema(conn)
    row = conn.execute("SELECT generation FROM basket_strategy_activation_state WHERE singleton=1").fetchone()
    generation = int(row[0] or 0) + 1 if row else 1
    payload = {**activation, "generation": generation, "activated_at": _now()}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """INSERT INTO basket_strategy_activation_state(singleton,generation,state,strategy_id,activation_sha256,activation_json,updated_at)
               VALUES(1,?,'enabled',?,?,?,?)
               ON CONFLICT(singleton) DO UPDATE SET generation=excluded.generation,state='enabled',strategy_id=excluded.strategy_id,
                 activation_sha256=excluded.activation_sha256,activation_json=excluded.activation_json,updated_at=excluded.updated_at""",
            (generation, payload["strategy_id"], payload["activation_contract_sha256"], json.dumps(payload, ensure_ascii=False, sort_keys=True), payload["activated_at"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(CONFIRM, action="store_true", dest="confirm")
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args()
    if not args.confirm:
        print(f"confirmation_required:{CONFIRM}", flush=True)
        return 2
    activation = load_activation(ACTIVATION_PATH)
    if activation.get("state") != "enabled" or activation.get("live_submit_authorized") is not True:
        raise RuntimeError("basket_activation_not_live_enabled")
    if activation_contract_sha256(activation) != activation.get("activation_contract_sha256"):
        raise RuntimeError("basket_activation_hash_mismatch")
    health = _gateway_ready()
    conn = sqlite3.connect(args.db, timeout=15.0, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=15000")
    paused = False
    try:
        PAUSE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE.write_text(f"basket_plus_hard_no_cutover {_now()}\n", encoding="utf-8")
        paused = True
        _disable_legacy()
        payload = _persist_activation(conn, activation)
        pid = _restart_reported_trajectory()
        if not _launchctl("print", f"system/{LABEL}").returncode == 0:
            raise RuntimeError("reported_trajectory_service_not_loaded_after_restart")
        PAUSE.unlink(missing_ok=True)
        paused = False
        result = {
            "schema": "basket_plus_hard_no_cutover_v1",
            "ok": True,
            "state": "production_enabled",
            "strategy": "basket_yes_plus_reported_trajectory_no",
            "basket_activation": payload,
            "hard_no_service_pid": pid,
            "hard_no_remains_active": True,
            "three_leg_disabled": True,
            "legacy_labels": list(LEGACY_LABELS),
            "gateway_pid": health.get("pid"),
            "live_pause_present": False,
            "places_orders_during_cutover": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception:
        if paused:
            PAUSE.write_text("basket_plus_hard_no_cutover_failed_closed\n", encoding="utf-8")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
