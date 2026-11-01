#!/usr/bin/env python3
"""Bring the local egress proxy back when nothing listens on its port.

Every collector, the order gateway's equity read and the WS listener leave the
machine through one explicit proxy (``official_nwp_proxy_url``), served by the
Shadowrocket VPN tunnel.  On 2026-10-31 the Shadowrocket app restarted at
16:50Z and its tunnel did not come back: the port sat unbound for 571 minutes,
production refused every scope on stale equity and the 11-02 P0 fleet expired
before anyone looked (user 2026-11-01 「自动拉起 Shadowrocket 的守护」).

One pass per launchd tick (a minute): connect to the proxy port; refused twice
in a row (two ticks, so a manual reconnect in progress is not raced) -> start
the app if it is not running, then ``scutil --nc start`` its VPN service.
Attempts are spaced at least ``RETRY_SPACING_SEC`` apart.  Writes nothing but
its own state file and log lines; never touches the SSOT.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from official_nwp_https_transport import official_nwp_proxy_url  # noqa: E402

STATE_PATH = ROOT / "data" / "runtime" / "egress_proxy_watchdog.json"
VPN_SERVICE = "Shadowrocket"
APP_NAME = "Shadowrocket"
REFUSALS_BEFORE_ACTION = 2
RETRY_SPACING_SEC = 180.0
CONNECT_TIMEOUT_SEC = 3.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def proxy_endpoint() -> tuple[str, int]:
    url = urlsplit(official_nwp_proxy_url())
    if not url.hostname or not url.port:
        raise SystemExit("egress_proxy_watchdog: no explicit proxy configured")
    return url.hostname, int(url.port)


def port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SEC):
            return True
    except OSError:
        return False


def _run(cmd: list[str]) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return (done.stdout + done.stderr).strip()


def app_running() -> bool:
    return subprocess.run(["/usr/bin/pgrep", "-x", APP_NAME], capture_output=True).returncode == 0


def revive() -> list[str]:
    steps = []
    if not app_running():
        steps.append("open: " + (_run(["/usr/bin/open", "-g", "-a", APP_NAME]) or "ok"))
        time.sleep(10)
    steps.append("vpn status before: " + _run(["/usr/sbin/scutil", "--nc", "status", VPN_SERVICE]).splitlines()[0])
    steps.append("vpn start: " + (_run(["/usr/sbin/scutil", "--nc", "start", VPN_SERVICE]) or "ok"))
    return steps


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def main() -> int:
    host, port = proxy_endpoint()
    state = read_state()
    now = _now()
    if port_listening(host, port):
        if state.get("refusals"):
            print(f"[{now}] {host}:{port} listening again after {state['refusals']} refusals", flush=True)
        state.update(refusals=0, last_ok_at_utc=now)
        write_state(state)
        return 0
    state["refusals"] = int(state.get("refusals") or 0) + 1
    state["last_refused_at_utc"] = now
    print(f"[{now}] {host}:{port} refused ({state['refusals']} in a row)", flush=True)
    last_attempt = state.get("last_attempt_epoch") or 0
    if state["refusals"] >= REFUSALS_BEFORE_ACTION and time.time() - last_attempt >= RETRY_SPACING_SEC:
        steps = revive()
        state.update(last_attempt_epoch=time.time(), last_attempt_at_utc=now, last_attempt_steps=steps)
        state["attempts"] = int(state.get("attempts") or 0) + 1
        for step in steps:
            print(f"[{now}]   {step}", flush=True)
        time.sleep(5)
        print(f"[{now}]   listening after revive: {port_listening(host, port)}", flush=True)
    write_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
