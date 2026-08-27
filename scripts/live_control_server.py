"""The dashboard's write path for the three live-preparation knobs.

Serves 127.0.0.1 only.  GET /api/state returns the three control files and
the contract facts the page's checklist renders; the POST endpoints below
move exactly one knob each, atomically (tmp + rename), inside hard bounds,
and append every change to an audit trail.  The page works without this
server -- controls simply render disabled with the command to start it.

What the live toggle does, stated here so nobody reads more into it: the
worker is contract-locked to production_no_live three times over (mode
string, activation file, runtime config), so arming intent places no order
and cannot.  It records that the operator asked, and the go-live migration
that mints the live contract is required to refuse while this flag is off.

POST /api/kelly   {"value": 0.35}          -> portfolio_risk_fraction
POST /api/budget  {"value": 514.29}        -> target_remaining_budget_usdc
POST /api/live    {"enabled": true, "confirm": "LIVE", "by": "..."}
POST /api/allowlist {"stations": ["ZBAA", ...]}
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config/trading/portfolio_capital_policy_v1.json"
RUNTIME_PATH = ROOT / "config/bayes_basket_target/no_live_runtime_v1.json"
INTENT_PATH = ROOT / "config/trading/live_intent_v1.json"
AUDIT_PATH = ROOT / "data" / "live_control_audit.jsonl"
PORT = 8788

KELLY_BOUNDS = (0.05, 0.50)
KELLY_STEP_MAX = 0.50
BUDGET_BOUNDS = (0.0, 2000.0)
STATION_RE = re.compile(r"^[A-Z0-9]{3,6}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_atomic(path: Path, payload: dict) -> None:
    handle, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name, suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _audit(entry: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"at_utc": _now(), **entry}, ensure_ascii=False,
        ) + "\n")


def state() -> dict:
    policy = _read(POLICY_PATH)
    runtime = _read(RUNTIME_PATH)
    intent = _read(INTENT_PATH)
    return {
        "schema": "live_control_state_v1",
        "at_utc": _now(),
        "kelly": {
            "portfolio_risk_fraction": policy["portfolio_risk_fraction"],
            "bounds": KELLY_BOUNDS,
            "step": 0.05,
        },
        "budget": {
            "target_remaining_budget_usdc": runtime[
                "target_remaining_budget_usdc"
            ],
            "bounds": BUDGET_BOUNDS,
            "step": 50.0,
        },
        "live": {
            "enabled": intent["live_enabled"],
            "armed_by": intent.get("armed_by"),
            "armed_at_utc": intent.get("armed_at_utc"),
            "station_allowlist": intent.get("station_allowlist") or [],
        },
        "contracts": {
            "execution_mode": runtime.get("execution_mode"),
            "live_submit_authorized": runtime.get("live_submit_authorized"),
            "places_orders": runtime.get("places_orders"),
            "policy_authority_scope": policy.get("authority_scope"),
        },
    }


def set_kelly(value: float) -> dict:
    if not KELLY_BOUNDS[0] <= value <= KELLY_BOUNDS[1]:
        raise ValueError(
            f"kelly out of bounds {KELLY_BOUNDS}: {value}"
        )
    policy = _read(POLICY_PATH)
    before = float(policy["portfolio_risk_fraction"])
    policy["portfolio_risk_fraction"] = round(float(value), 4)
    _write_atomic(POLICY_PATH, policy)
    _audit({
        "knob": "portfolio_risk_fraction",
        "before": before, "after": policy["portfolio_risk_fraction"],
    })
    return state()


def set_budget(value: float) -> dict:
    if not BUDGET_BOUNDS[0] <= value <= BUDGET_BOUNDS[1]:
        raise ValueError(
            f"budget out of bounds {BUDGET_BOUNDS}: {value}"
        )
    runtime = _read(RUNTIME_PATH)
    before = float(runtime["target_remaining_budget_usdc"])
    runtime["target_remaining_budget_usdc"] = round(float(value), 2)
    _write_atomic(RUNTIME_PATH, runtime)
    _audit({
        "knob": "target_remaining_budget_usdc",
        "before": before, "after": runtime["target_remaining_budget_usdc"],
    })
    return state()


def set_live(enabled: bool, confirm: str, by: str) -> dict:
    if enabled and confirm != "LIVE":
        raise ValueError("arming live intent requires confirm='LIVE'")
    intent = _read(INTENT_PATH)
    before = bool(intent["live_enabled"])
    intent["live_enabled"] = bool(enabled)
    intent["armed_by"] = str(by or "dashboard") if enabled else None
    intent["armed_at_utc"] = _now() if enabled else None
    intent["updated_at_utc"] = _now()
    _write_atomic(INTENT_PATH, intent)
    _audit({
        "knob": "live_enabled", "before": before,
        "after": intent["live_enabled"], "by": intent["armed_by"],
    })
    return state()


def set_allowlist(stations: list) -> dict:
    cleaned = sorted({str(s).strip().upper() for s in stations if s})
    for station in cleaned:
        if not STATION_RE.match(station):
            raise ValueError(f"bad station id: {station}")
    if not cleaned:
        raise ValueError("allowlist cannot be empty")
    intent = _read(INTENT_PATH)
    before = intent.get("station_allowlist") or []
    intent["station_allowlist"] = cleaned
    intent["updated_at_utc"] = _now()
    _write_atomic(INTENT_PATH, intent)
    _audit({
        "knob": "station_allowlist", "before": before, "after": cleaned,
    })
    return state()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/api/state":
            self._send(200, state())
        else:
            self._send(404, {"error": "unknown endpoint"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path.rstrip("/") == "/api/kelly":
                out = set_kelly(float(body["value"]))
            elif self.path.rstrip("/") == "/api/budget":
                out = set_budget(float(body["value"]))
            elif self.path.rstrip("/") == "/api/live":
                out = set_live(
                    bool(body.get("enabled")),
                    str(body.get("confirm") or ""),
                    str(body.get("by") or ""),
                )
            elif self.path.rstrip("/") == "/api/allowlist":
                out = set_allowlist(list(body.get("stations") or []))
            else:
                self._send(404, {"error": "unknown endpoint"})
                return
            self._send(200, out)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})

    def log_message(self, *_args) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"live control: http://127.0.0.1:{PORT}/api/state  (Ctrl-C 停止)")
    server.serve_forever()


if __name__ == "__main__":
    main()
