#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT_DIR / "data" / "model_scan.log"


def load_sms_sender() -> Tuple[Optional[Callable[[str, str], bool]], bool, str]:
    try:
        from scan_weather_market import _send_sms_message, SMS_ENABLED, SMS_RECIPIENT
    except Exception:
        return None, False, ""
    return _send_sms_message, SMS_ENABLED, SMS_RECIPIENT


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
    parser.add_argument("--pattern", action="append", default=["auto-trade"],
                        help="Regex pattern to trigger notifications. Can be repeated.")
    parser.add_argument("--from-start", action="store_true",
                        help="Start from the beginning of the log instead of tailing.")
    parser.add_argument("--once", action="store_true", help="Check once and exit.")
    parser.add_argument("--sms", action="store_true", help="Force SMS notifications on.")
    parser.add_argument("--no-sms", action="store_true", help="Disable SMS notifications.")
    args = parser.parse_args()

    log_path = Path(args.log)
    patterns = compile_patterns(args.pattern)
    send_sms, sms_enabled, sms_recipient = load_sms_sender()
    if args.sms:
        sms_enabled = True
    if args.no_sms:
        sms_enabled = False
    if sms_enabled and not sms_recipient:
        sms_enabled = False

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
