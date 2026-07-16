#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT_DIR / "data" / "model_scan.log"
DEFAULT_TRADE_PATTERNS = [
    r"\[auto-trade\]\s+entry\b(?!\s+(?:skip|blocked|market skip|failed|candidates failed)\b)",
    r"\[auto-trade\]\s+exit\b(?!\s+(?:skip|dry-run|failed|retry skipped|retry failed|only focus rows)\b)",
    r"\[auto-trade\]\s+exit retry (?:submitted|confirm|pending)\b",
    r"\[auto-trade\]\s+synced (?:fill|exit) decision backfilled\b",
]


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _send_sms_message(message: str, recipient: str) -> bool:
    safe = _escape_applescript(str(message or "").strip())
    if not safe or not recipient:
        return False
    script = (
        'tell application "Messages"\n'
        f'  set targetBuddy to "{recipient}"\n'
        '  set targetService to missing value\n'
        '  try\n'
        '    set targetService to first service whose service type is SMS\n'
        '  end try\n'
        '  if targetService is missing value then\n'
        '    try\n'
        '      set targetService to first service whose service type is iMessage\n'
        '    end try\n'
        '  end if\n'
        '  if targetService is missing value then\n'
        '    error "no messaging service"\n'
        '  end if\n'
        '  send "'
        + safe
        + '" to buddy targetBuddy of targetService\n'
        'end tell'
    )
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        print(f"[auto-trade watcher] sms osascript error: {exc}", flush=True)
        return False
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        print(f"[auto-trade watcher] sms failed: {err}", flush=True)
        return False
    return True


def load_sms_sender() -> Tuple[Optional[Callable[[str, str], bool]], bool, str]:
    sms_enabled = os.environ.get("PM_SMS_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
    recipient = str(os.environ.get("PM_SMS_RECIPIENT", "") or "").strip()
    return _send_sms_message, sms_enabled, recipient


def compile_patterns(values: List[str]) -> List[re.Pattern]:
    patterns = []
    for value in values:
        if not value:
            continue
        try:
            patterns.append(re.compile(value, re.IGNORECASE))
        except re.error:
            patterns.append(re.compile(re.escape(value), re.IGNORECASE))
    return patterns


def env_patterns() -> List[str]:
    raw = os.environ.get("PM_WATCH_PATTERNS", "").strip()
    if not raw:
        return list(DEFAULT_TRADE_PATTERNS)
    values = []
    for part in re.split(r"[\n;]+", raw):
        part = part.strip()
        if part:
            values.append(part)
    return values or list(DEFAULT_TRADE_PATTERNS)


def redact_recipient(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:3]}***{text[-2:]}"


def read_new_lines(path: Path, offset: int, carry: str) -> Tuple[int, str, List[str]]:
    if not path.exists():
        return offset, carry, []
    size = path.stat().st_size
    if size < offset:
        offset = 0
        carry = ""
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
        offset = handle.tell()
    if not chunk:
        return offset, carry, []
    text = carry + chunk.decode("utf-8", errors="ignore")
    if text and not text.endswith(("\n", "\r")):
        parts = text.splitlines()
        carry = parts.pop() if parts else text
    else:
        parts = text.splitlines()
        carry = ""
    return offset, carry, parts


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch scan logs and notify on auto-trade triggers.")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="Log file to watch.")
    parser.add_argument("--interval", type=float, default=float(os.environ.get("PM_WATCH_INTERVAL_SEC", "30")),
                        help="Polling interval in seconds.")
    parser.add_argument("--pattern", action="append", default=None,
                        help="Regex pattern to trigger notifications. Can be repeated.")
    parser.add_argument("--from-start", action="store_true",
                        help="Start from the beginning of the log instead of tailing.")
    parser.add_argument("--once", action="store_true", help="Check once and exit.")
    parser.add_argument("--sms", action="store_true", help="Force SMS notifications on.")
    parser.add_argument("--no-sms", action="store_true", help="Disable SMS notifications.")
    args = parser.parse_args()

    log_path = Path(args.log)
    pattern_values = args.pattern if args.pattern is not None else env_patterns()
    patterns = compile_patterns(pattern_values)
    send_sms, sms_enabled, sms_recipient = load_sms_sender()
    if args.sms:
        sms_enabled = True
    if args.no_sms:
        sms_enabled = False
    if sms_enabled and not sms_recipient:
        print("[auto-trade watcher] sms disabled: missing recipient", flush=True)
        sms_enabled = False
    print(
        "[auto-trade watcher] start "
        f"log={log_path} interval={args.interval:g}s patterns={len(patterns)} "
        f"sms={'on' if sms_enabled and send_sms else 'off'} "
        f"recipient={redact_recipient(sms_recipient)}",
        flush=True,
    )

    offset = 0
    carry = ""
    if log_path.exists() and not args.from_start:
        offset = log_path.stat().st_size

    heartbeat_sec_raw = os.environ.get("PM_WATCH_HEARTBEAT_SEC", "60").strip()
    try:
        heartbeat_sec = float(heartbeat_sec_raw)
    except Exception:
        heartbeat_sec = 60.0
    if not (heartbeat_sec > 0 and heartbeat_sec < 24 * 3600):
        heartbeat_sec = 60.0
    last_heartbeat_at = 0.0

    while True:
        offset, carry, lines = read_new_lines(log_path, offset, carry)
        matched = [line for line in lines if any(p.search(line) for p in patterns)]
        if matched:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            payload = "\n".join(matched)
            message = f"[auto-trade watcher {now}]\n{payload}"
            print(message, flush=True)
            if sms_enabled and send_sms:
                send_sms(message, sms_recipient)
            last_heartbeat_at = time.time()
        now_ts = time.time()
        if heartbeat_sec and (now_ts - last_heartbeat_at) >= heartbeat_sec:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[auto-trade watcher heartbeat {now}] ok", flush=True)
            last_heartbeat_at = now_ts
        if args.once:
            break
        time.sleep(max(0.5, args.interval))
    return 0


if __name__ == "__main__":
    sys.exit(main())
