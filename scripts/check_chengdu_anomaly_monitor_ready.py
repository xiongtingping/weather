#!/usr/bin/env python3
"""Readiness audit for the Chengdu weather anomaly monitor chain.

Default mode is read-only: it inspects scripts, launchd source/installed
plists, current launchctl state, env safety, and the latest archived anomaly
reports. With --run-once it also runs the one-shot Chengdu monitor into /tmp
and validates Gamma/CLOB/Data API evidence fields in that fresh snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
MONITOR_DAEMON_LABEL = os.environ.get("PM_WEATHER_MONITOR_LABEL", "com.weather.monitor.daemon").strip()
MONITOR_DAEMON_DOMAIN_LABEL = (
    MONITOR_DAEMON_LABEL if MONITOR_DAEMON_LABEL.startswith("system/") else f"system/{MONITOR_DAEMON_LABEL}"
)
MONITOR_DAEMON_NAME = MONITOR_DAEMON_DOMAIN_LABEL.rsplit("/", 1)[-1]
SOURCE_PLIST = ROOT / "launchd" / f"{MONITOR_DAEMON_NAME}.plist"
INSTALLED_PLIST = Path("/Library/LaunchDaemons") / f"{MONITOR_DAEMON_NAME}.plist"
REPORT_DIR = ROOT / "data" / "reports" / "weather_anomaly"

REQUIRED_SCRIPTS = {
    "anomaly_monitor": ROOT / "scripts" / "weather_market_anomaly_monitor.py",
    "chengdu_once": ROOT / "scripts" / "chengdu_weather_anomaly_once.sh",
    "monitor_daemon": ROOT / "scripts" / "polymarket_monitor_daemon.sh",
}


def run_cmd(args: list[str], *, timeout: int = 20, env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except Exception as exc:
        return {"returncode": None, "stdout": "", "stderr": repr(exc)}


def load_dotenv() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
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
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.realpath(os.path.expanduser(left)) == os.path.realpath(
            os.path.expanduser(right)
        )
    except Exception:
        return False


def load_plist(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return plistlib.load(handle) or {}
    except Exception:
        return {}


def program_text(plist: dict[str, Any]) -> str:
    args = plist.get("ProgramArguments")
    if isinstance(args, list):
        return " ".join(str(item) for item in args)
    return str(plist.get("Program") or "")


def text_uses_root(text: str, root: Path = ROOT) -> bool:
    return str(root) in str(text or "")


def launchctl_root_status(output: str, root: Path = ROOT) -> dict[str, Any]:
    text = str(output or "")
    program_lines: list[str] = []
    working_directory = ""
    lines = text.splitlines()
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("program = "):
            program_lines.append(line.split("=", 1)[1].strip())
        elif line == "arguments = {" or line.startswith("arguments = {"):
            for inner in lines[idx + 1 : idx + 12]:
                inner_line = inner.strip()
                if inner_line == "}":
                    break
                if inner_line:
                    program_lines.append(inner_line)
        elif line.startswith("working directory = "):
            working_directory = line.split("=", 1)[1].strip()
    combined = "\n".join(program_lines + ([working_directory] if working_directory else []))
    return {
        "program_lines": program_lines,
        "working_directory": working_directory,
        "uses_current_root": text_uses_root(combined, root),
        "contains_legacy_poly_root": "/Users/xiong/poly" in combined,
    }


def latest_json(pattern: str) -> Path | None:
    paths = sorted(REPORT_DIR.glob(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    return paths[-1] if paths else None


def load_json(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def validate_report_fields(report: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    events = report.get("events") or []
    if not events:
        missing.append("events")
        return missing
    for event in events:
        slug = event.get("slug") or "<missing-slug>"
        buckets = event.get("buckets") or []
        if not buckets:
            missing.append(f"{slug}:buckets")
            continue
        for bucket in buckets:
            label = bucket.get("label") or "<missing-label>"
            prefix = f"{slug}:{label}"
            book = bucket.get("book") or {}
            trades = bucket.get("trade_aggregate") or {}
            model = bucket.get("model") or {}
            checks = {
                "condition_id": bool(bucket.get("condition_id")),
                "yes_token": bool(bucket.get("yes_token")),
                "no_token": bool(bucket.get("no_token")),
                "book_ok": bool(book.get("ok")),
                "best_bid": book.get("best_bid") is not None,
                "best_ask": book.get("best_ask") is not None,
                "bid_depth_top5": book.get("bid_depth_top5") is not None,
                "ask_depth_top5": book.get("ask_depth_top5") is not None,
                "trade_count": (trades.get("trade_count") or 0) > 0,
                "unique_wallets": (trades.get("unique_wallets") or 0) > 0,
                "top1_wallet_share": trades.get("top1_wallet_share") is not None,
                "top3_wallet_share": trades.get("top3_wallet_share") is not None,
                "top5_wallet_share": trades.get("top5_wallet_share") is not None,
                "q_bayes_posterior": model.get("q_bayes_posterior") is not None,
                "q_calibrated": model.get("q_calibrated") is not None,
                "q_physical_likelihood": model.get("q_physical_likelihood") is not None,
            }
            for key, ok in checks.items():
                if not ok:
                    missing.append(f"{prefix}:{key}")
    return missing


def audit_static() -> tuple[dict[str, Any], list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    env_vars = load_dotenv()
    monitor_signal_log = env_vars.get("MONITOR_SIGNAL_LOG", "")
    poly_signal_path = env_vars.get("POLY_SIGNAL_PATH", "")
    env_safety = {
        "monitor_signal_log": monitor_signal_log,
        "poly_signal_path": poly_signal_path,
        "same_path": same_path(monitor_signal_log, poly_signal_path),
    }
    if env_safety["same_path"]:
        blockers.append("monitor_signal_log_points_to_poly_signal_path")

    scripts: dict[str, Any] = {}
    for name, path in REQUIRED_SCRIPTS.items():
        exists = path.exists()
        executable = os.access(path, os.X_OK) if exists else False
        scripts[name] = {"path": str(path), "exists": exists, "executable": executable}
        if not exists:
            blockers.append(f"{name}_missing")
        elif path.suffix == ".sh" and not executable:
            warnings.append(f"{name}_not_executable")

    syntax = {
        "chengdu_once_bash": run_cmd(["bash", "-n", str(REQUIRED_SCRIPTS["chengdu_once"])]),
        "monitor_daemon_bash": run_cmd(["bash", "-n", str(REQUIRED_SCRIPTS["monitor_daemon"])]),
        "anomaly_monitor_py": run_cmd(
            [sys.executable, "-m", "py_compile", str(REQUIRED_SCRIPTS["anomaly_monitor"])]
        ),
    }
    for name, result in syntax.items():
        if result["returncode"] != 0:
            blockers.append(f"{name}_failed")

    source_plist = load_plist(SOURCE_PLIST)
    installed_plist = load_plist(INSTALLED_PLIST)
    source_program = program_text(source_plist)
    installed_program = program_text(installed_plist)
    plist = {
        "source_path": str(SOURCE_PLIST),
        "source_exists": SOURCE_PLIST.exists(),
        "source_program": source_program,
        "source_uses_monitor_daemon": "scripts/polymarket_monitor_daemon.sh" in source_program,
        "installed_path": str(INSTALLED_PLIST),
        "installed_exists": INSTALLED_PLIST.exists(),
        "installed_program": installed_program,
        "installed_uses_monitor_daemon": "scripts/polymarket_monitor_daemon.sh" in installed_program,
        "installed_uses_current_root": text_uses_root(installed_program),
        "installed_contains_legacy_poly_root": "/Users/xiong/poly" in installed_program,
        "installed_matches_source": bool(source_program and source_program == installed_program),
    }
    if not plist["source_exists"]:
        blockers.append("source_monitor_plist_missing")
    if not plist["source_uses_monitor_daemon"]:
        blockers.append("source_monitor_plist_not_using_monitor_daemon")
    if not plist["installed_exists"]:
        warnings.append("installed_monitor_plist_missing")
    else:
        if not plist["installed_uses_monitor_daemon"]:
            blockers.append("installed_monitor_plist_not_using_monitor_daemon")
        if not plist["installed_uses_current_root"]:
            blockers.append("installed_monitor_plist_wrong_root")
        if plist["installed_contains_legacy_poly_root"]:
            blockers.append("installed_monitor_plist_legacy_poly_root")
        if not plist["installed_matches_source"]:
            warnings.append("installed_monitor_plist_not_updated_from_source")

    launchctl = run_cmd(["launchctl", "print", MONITOR_DAEMON_DOMAIN_LABEL], timeout=8)
    launchctl_root = launchctl_root_status(launchctl["stdout"])
    if launchctl["returncode"] != 0:
        warnings.append("launchctl_monitor_not_printable")
    else:
        if not launchctl_root["uses_current_root"]:
            blockers.append("launchctl_monitor_wrong_root")
        if launchctl_root["contains_legacy_poly_root"]:
            blockers.append("launchctl_monitor_legacy_poly_root")
    latest_report_path = latest_json("chengdu_weather_market_anomaly_*.json")
    latest_alerts_path = latest_json("chengdu_weather_market_alerts_*.json")
    latest_report = load_json(latest_report_path)
    latest_alerts = load_json(latest_alerts_path)
    report_missing = validate_report_fields(latest_report) if latest_report else ["latest_report"]
    if report_missing:
        warnings.append("latest_report_missing_required_fields")

    details = {
        "env_safety": env_safety,
        "scripts": scripts,
        "syntax": {
            name: {
                "returncode": result["returncode"],
                "stderr": result["stderr"][-500:],
            }
            for name, result in syntax.items()
        },
        "plist": plist,
        "launchctl_monitor": {
            "returncode": launchctl["returncode"],
            "stdout_head": "\n".join(launchctl["stdout"].splitlines()[:20]),
            "stderr": launchctl["stderr"][-500:],
            "root_status": launchctl_root,
        },
        "latest_report": {
            "path": str(latest_report_path) if latest_report_path else "",
            "summary": latest_report.get("summary", {}),
            "missing_fields": report_missing[:50],
        },
        "latest_alerts": {
            "path": str(latest_alerts_path) if latest_alerts_path else "",
            "alert_count": latest_alerts.get("alert_count"),
        },
    }
    return details, blockers, warnings


def run_once_validation(args: argparse.Namespace) -> tuple[dict[str, Any], list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="chengdu_anomaly_ready_") as tmp:
        tmp_path = Path(tmp)
        env = os.environ.copy()
        env.update(
            {
                "PM_CHENGDU_ANOMALY_OUT_DIR": str(tmp_path),
                "PM_CHENGDU_ANOMALY_STATE_PATH": str(tmp_path / "state.json"),
                "PM_CHENGDU_ANOMALY_LIMIT_TRADES": str(args.limit_trades),
                "PM_CHENGDU_ANOMALY_PAGE_LIMIT": str(args.limit_trades),
                "PM_CHENGDU_ANOMALY_ALERT_RISK_DELTA": "999",
            }
        )
        result = run_cmd(
            [str(REQUIRED_SCRIPTS["monitor_daemon"]), "--once", "--no-watcher"],
            timeout=args.timeout,
            env=env,
        )
        reports = sorted(tmp_path.glob("chengdu_weather_market_anomaly_*.json"))
        alerts = sorted(tmp_path.glob("chengdu_weather_market_alerts_*.json"))
        report = load_json(reports[-1] if reports else None)
        alerts_json = load_json(alerts[-1] if alerts else None)
        missing = validate_report_fields(report) if report else ["fresh_report"]
        if result["returncode"] != 0:
            blockers.append("run_once_failed")
        if missing:
            blockers.append("fresh_report_missing_required_fields")
        summary = report.get("summary", {})
        if (summary.get("events_analyzed") or 0) < 1:
            blockers.append("fresh_report_no_events")
        details = {
            "returncode": result["returncode"],
            "stdout": result["stdout"],
            "stderr": result["stderr"][-1000:],
            "report_summary": summary,
            "alert_count": alerts_json.get("alert_count"),
            "missing_fields": missing[:50],
        }
        if not alerts_json:
            warnings.append("fresh_alerts_json_missing")
        return details, blockers, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--limit-trades", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    details, blockers, warnings = audit_static()
    if args.run_once:
        fresh, run_blockers, run_warnings = run_once_validation(args)
        details["run_once"] = fresh
        blockers.extend(run_blockers)
        warnings.extend(run_warnings)

    payload = {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "details": details,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not blockers else 1


if __name__ == "__main__":
    raise SystemExit(main())
