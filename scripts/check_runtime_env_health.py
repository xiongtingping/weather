#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
REPO_ENV = ROOT / ".env"
DOTENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PRIVATE_KEY_LITERAL_RE = re.compile(r"^0x[0-9A-Fa-f]{64}$")
PROXY_ENV_KEYS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "GLOBAL_AGENT_HTTP_PROXY",
    "PM_MARKET_WS_HTTP_PROXY",
)
TRADE_REQUIRED_KEYS = (
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_API_PASSPHRASE",
    "PRIVATE_KEY",
)
TRADE_ALIAS_KEYS = {
    "PRIVATE_KEY": ("PRIVATE_KEY", "POLY_PRIVATE_KEY"),
}
LAUNCHD_ENV_KEYS = (
    "PM_AUTO_TRADE_ENABLED",
    "PM_AUTO_TRADE_DRY_RUN",
    "PM_CLOB_FORCE_DIRECT",
    "PM_CLOB_FORCE_PROXY",
    "PM_PROXY_PORTS",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "GLOBAL_AGENT_HTTP_PROXY",
    *TRADE_REQUIRED_KEYS,
)
DEFAULT_LABELS = (
    "com.weather.monitor.daemon",
    "com.weather.market_ws",
    "com.weather.market_refresh_1m",
    "com.weather.trade_engine",
)
LEGACY_LABELS = (
    "com.weather.trade_sync_loop",
)


def _strip_quotes(value: str) -> str:
    raw = value.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        return raw[1:-1]
    return raw


def _parse_env_file(path: Path, *, allow_private_literal: bool = True) -> tuple[dict[str, Any], dict[str, str]]:
    values: dict[str, str] = {}
    duplicate_keys: dict[str, list[int]] = {}
    invalid_lines: list[dict[str, Any]] = []
    private_key_literal_lines: list[int] = []
    public: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "invalid_lines": invalid_lines,
        "invalid_count": 0,
        "private_key_literal_lines": private_key_literal_lines,
        "duplicate_keys": duplicate_keys,
        "keys_present": [],
    }
    if not path.exists():
        return public, values
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        public["read_error"] = exc.__class__.__name__
        return public, values
    seen: dict[str, list[int]] = {}
    for line_no, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            if allow_private_literal and PRIVATE_KEY_LITERAL_RE.fullmatch(stripped):
                values.setdefault("PRIVATE_KEY", stripped)
                private_key_literal_lines.append(line_no)
                seen.setdefault("PRIVATE_KEY", []).append(line_no)
                continue
            invalid_lines.append({"line": line_no, "reason": "not_key_value"})
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not DOTENV_KEY_RE.fullmatch(key):
            invalid_lines.append({"line": line_no, "reason": "invalid_key"})
            continue
        value = value.strip()
        if value and value[0] not in {"'", '"'} and " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = _strip_quotes(value)
        seen.setdefault(key, []).append(line_no)
    for key, lines in sorted(seen.items()):
        if len(lines) > 1:
            duplicate_keys[key] = lines
    public["invalid_count"] = len(invalid_lines)
    public["keys_present"] = sorted(values)
    return public, values


def _poly_env_path(repo_values: dict[str, str]) -> Path:
    raw = str(os.environ.get("PM_SHARED_POLY_ENV_PATH") or repo_values.get("PM_SHARED_POLY_ENV_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "poly" / ".env"


def _file_mode(path: Path) -> str:
    try:
        return oct(path.stat().st_mode & 0o777)[2:]
    except OSError:
        return "missing"


def _effective_env(repo_values: dict[str, str], poly_values: dict[str, str]) -> dict[str, str]:
    values = dict(repo_values)
    for key, value in poly_values.items():
        if key in {
            "POLY_API_KEY",
            "POLY_API_SECRET",
            "POLY_API_PASSPHRASE",
            "POLY_PRIVATE_KEY",
            "PRIVATE_KEY",
            "POLY_RPC_URL",
            "POLY_DATA_API_HOST",
            "POLY_DATA_API_USER",
            "PM_FUNDER_ADDRESS",
            "POLY_FUNDER_ADDRESS",
            "PM_SIGNATURE_TYPE",
            "SIGNATURE_TYPE",
        }:
            values[key] = value
    if not values.get("PRIVATE_KEY") and values.get("POLY_PRIVATE_KEY"):
        values["PRIVATE_KEY"] = values["POLY_PRIVATE_KEY"]
    if not values.get("PM_FUNDER_ADDRESS") and values.get("POLY_FUNDER_ADDRESS"):
        values["PM_FUNDER_ADDRESS"] = values["POLY_FUNDER_ADDRESS"]
    return values


def _credentials_status(values: dict[str, str], poly_path: Path) -> dict[str, Any]:
    missing: list[str] = []
    present: dict[str, bool] = {}
    for key in TRADE_REQUIRED_KEYS:
        aliases = TRADE_ALIAS_KEYS.get(key, (key,))
        found = any(bool(str(values.get(alias) or "").strip()) for alias in aliases)
        present[key] = found
        if not found:
            missing.append(key)
    mode = _file_mode(poly_path)
    mode_warning = False
    if mode not in {"missing", "unknown"} and mode.isdigit():
        try:
            mode_warning = (int(mode, 8) & 0o077) != 0
        except ValueError:
            mode_warning = False
    return {
        "ok": not missing and not mode_warning,
        "present": present,
        "missing": missing,
        "poly_env_path": str(poly_path),
        "poly_env_mode": mode,
        "poly_env_mode_warning": mode_warning,
    }


def _parse_proxy(proxy_url: str) -> dict[str, Any]:
    raw = str(proxy_url or "").strip()
    if not raw:
        return {"present": False}
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        port = parsed.port
        scheme = parsed.scheme or "http"
    except Exception:
        return {"present": True, "parse_error": True}
    safe_url = f"{scheme}://{host}:{port}" if host and port else f"{scheme}://{host or '-'}"
    return {
        "present": True,
        "scheme": scheme,
        "host": host,
        "port": port,
        "safe_url": safe_url,
        "is_local": host in {"127.0.0.1", "localhost", "::1"},
    }


def _tcp_reachable(host: str, port: int | None, timeout: float = 0.4) -> bool:
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _parse_ports(raw: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,\s]+", str(raw or "")):
        if not part:
            continue
        try:
            port = int(part)
        except ValueError:
            continue
        if 0 < port <= 65535 and port not in out:
            out.append(port)
    return out


def _scutil_proxy_ports() -> list[int]:
    try:
        proc = subprocess.run(
            ["scutil", "--proxy"],
            capture_output=True,
            text=True,
            timeout=0.6,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    ports: list[int] = []
    for line in proc.stdout.splitlines():
        if not re.search(r"(HTTPPort|HTTPSPort|SOCKSPort)", line):
            continue
        match = re.search(r":\s*([0-9]+)\s*$", line)
        if not match:
            continue
        port = int(match.group(1))
        if 0 < port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def _proxy_status(values: dict[str, str]) -> dict[str, Any]:
    current_key = ""
    current_raw = ""
    for key in PROXY_ENV_KEYS:
        if str(values.get(key) or "").strip():
            current_key = key
            current_raw = values[key]
            break
    current = _parse_proxy(current_raw)
    candidates: list[int] = []
    for raw in (
        values.get("PM_PROXY_PORTS", ""),
        values.get("PM_PROXY_LOCAL_FALLBACK_PORTS", "1082"),
    ):
        for port in _parse_ports(raw):
            if port not in candidates:
                candidates.append(port)
    for key in PROXY_ENV_KEYS:
        parsed = _parse_proxy(values.get(key, ""))
        port = parsed.get("port")
        if isinstance(port, int) and port not in candidates:
            candidates.append(port)
    for port in _scutil_proxy_ports():
        if port not in candidates:
            candidates.append(port)
    candidate_rows = [
        {"host": "127.0.0.1", "port": port, "reachable": _tcp_reachable("127.0.0.1", port)}
        for port in candidates
    ]
    reachable = None
    if current.get("present") and current.get("host") and current.get("port"):
        reachable = _tcp_reachable(str(current["host"]), int(current["port"]))
    replacement = None
    if current.get("is_local") and reachable is False:
        for row in candidate_rows:
            if row["port"] == current.get("port"):
                continue
            if row["reachable"]:
                replacement = row
                break
    return {
        "current_key": current_key,
        "current": current,
        "current_reachable": reachable,
        "candidate_ports": candidate_rows,
        "replacement_available": replacement,
        "ok": not (current.get("is_local") and reachable is False and replacement is None),
    }


def _launchctl_getenv(key: str) -> str:
    try:
        proc = subprocess.run(
            ["launchctl", "getenv", key],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return str(proc.stdout or "").strip()


def _launchctl_print(domain_label: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["launchctl", "print", domain_label],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except Exception as exc:
        return 127, exc.__class__.__name__
    return proc.returncode, proc.stdout + proc.stderr


def _parse_launchd_pid(output: str) -> int | None:
    for pattern in (r"\bpid\s*=\s*([0-9]+)", r"\bPID\s*:\s*([0-9]+)"):
        match = re.search(pattern, output)
        if match:
            return int(match.group(1))
    return None


def _process_env_presence(pid: int | None) -> dict[str, Any]:
    if not pid:
        return {"available": False, "reason": "pid_missing"}
    try:
        proc = subprocess.run(
            ["ps", "eww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=0.7,
            check=False,
        )
    except Exception as exc:
        return {"available": False, "reason": exc.__class__.__name__}
    if proc.returncode != 0:
        return {"available": False, "reason": "ps_failed"}
    raw = proc.stdout or ""
    present = {}
    for key in LAUNCHD_ENV_KEYS:
        present[key] = bool(re.search(rf"(^|\s){re.escape(key)}=", raw))
    proxy_values = {}
    for key in PROXY_ENV_KEYS:
        match = re.search(rf"(^|\s){re.escape(key)}=([^\s]+)", raw)
        if match:
            proxy_values[key] = _parse_proxy(match.group(2)).get("safe_url", "<set>")
    if not any(present.values()) and not proxy_values:
        return {"available": False, "reason": "process_env_not_exposed", "command_visible": bool(raw.strip())}
    return {
        "available": True,
        "present": present,
        "missing_trade_required": [key for key in TRADE_REQUIRED_KEYS if not present.get(key)],
        "proxy": proxy_values,
    }


def _launchd_status(labels: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    uid = os.getuid()
    for label in labels:
        domains = [f"system/{label}", f"gui/{uid}/{label}"]
        found = None
        for domain_label in domains:
            code, output = _launchctl_print(domain_label)
            if code == 0:
                pid = _parse_launchd_pid(output)
                found = {
                    "label": label,
                    "domain": domain_label,
                    "loaded": True,
                    "pid": pid,
                    "running": bool(pid),
                    "env": _process_env_presence(pid),
                }
                break
        if found is None:
            found = {
                "label": label,
                "domain": None,
                "loaded": False,
                "pid": None,
                "running": False,
                "env": {"available": False, "reason": "launchctl_not_loaded_or_permission_denied"},
            }
        rows.append(found)
    return rows


def build_report(labels: list[str]) -> dict[str, Any]:
    repo_public, repo_values = _parse_env_file(REPO_ENV)
    poly_path = _poly_env_path(repo_values)
    poly_public, poly_values = _parse_env_file(poly_path)
    if poly_path.exists():
        poly_public["mode"] = _file_mode(poly_path)
    effective = _effective_env(repo_values, poly_values)
    launchctl_env = {key: bool(_launchctl_getenv(key)) for key in LAUNCHD_ENV_KEYS}
    report = {
        "ok": True,
        "repo_env": repo_public,
        "poly_env": poly_public,
        "credentials": _credentials_status(effective, poly_path),
        "proxy": _proxy_status(effective),
        "launchctl_getenv_present": launchctl_env,
        "launchd_services": _launchd_status(labels),
    }
    blockers: list[str] = []
    warnings: list[str] = []
    if repo_public.get("invalid_count"):
        warnings.append("repo_env_invalid_lines")
    if poly_public.get("invalid_count"):
        warnings.append("poly_env_invalid_lines")
    if not report["credentials"]["ok"]:
        blockers.append("trade_credentials_missing_or_insecure")
    proxy = report["proxy"]
    if proxy.get("current", {}).get("is_local") and proxy.get("current_reachable") is False:
        if proxy.get("replacement_available"):
            warnings.append("local_proxy_unreachable_replacement_available")
        else:
            blockers.append("local_proxy_unreachable_no_replacement")
    report["blockers"] = blockers
    report["warnings"] = warnings
    report["ok"] = not blockers
    return report


def _print_text(report: dict[str, Any]) -> None:
    status = "ok" if report.get("ok") else "blocked"
    print(f"runtime_env_health={status}")
    print(
        "repo_env="
        f"exists:{report['repo_env'].get('exists')} "
        f"invalid:{report['repo_env'].get('invalid_count', 0)} "
        f"private_key_literal_lines:{report['repo_env'].get('private_key_literal_lines', [])}"
    )
    print(
        "poly_env="
        f"exists:{report['poly_env'].get('exists')} "
        f"mode:{report['credentials'].get('poly_env_mode')} "
        f"invalid:{report['poly_env'].get('invalid_count', 0)}"
    )
    creds = report["credentials"]
    print(f"credentials={'ok' if creds.get('ok') else 'missing:' + ','.join(creds.get('missing') or [])}")
    proxy = report["proxy"]
    current = proxy.get("current") or {}
    print(
        "proxy="
        f"{proxy.get('current_key') or '-'} "
        f"{current.get('safe_url') or '-'} "
        f"reachable:{proxy.get('current_reachable')} "
        f"replacement:{proxy.get('replacement_available') or '-'}"
    )
    for row in report.get("launchd_services") or []:
        env = row.get("env") or {}
        missing = ",".join(env.get("missing_trade_required") or []) if env.get("available") else "-"
        if missing == "":
            missing = "none"
        print(
            "launchd="
            f"{row.get('domain') or row.get('label')} "
            f"loaded:{row.get('loaded')} "
            f"running:{row.get('running')} "
            f"pid:{row.get('pid') or '-'} "
            f"missing_trade_env:{missing}"
        )
    if report.get("warnings"):
        print("warnings=" + ",".join(report["warnings"]))
    if report.get("blockers"):
        print("blockers=" + ",".join(report["blockers"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only runtime env and launchd health check.")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="launchd label to inspect; can be repeated",
    )
    parser.add_argument(
        "--include-legacy-trade-sync",
        action="store_true",
        help="also inspect legacy com.weather.trade_sync_loop",
    )
    args = parser.parse_args(argv)
    labels = [str(item).strip() for item in (args.label or []) if str(item).strip()] or list(DEFAULT_LABELS)
    if args.include_legacy_trade_sync:
        for label in LEGACY_LABELS:
            if label not in labels:
                labels.append(label)
    report = build_report(labels)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
