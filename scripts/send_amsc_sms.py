#!/usr/bin/env python3
"""Submit one AMSC notification to the existing Messages SMS/iMessage sender."""

from __future__ import annotations

import argparse
import sys

from auto_trade_watcher import _send_sms_message


def main() -> int:
    parser = argparse.ArgumentParser(description="Send an AMSC notification by Messages.")
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--message", default="")
    parser.add_argument("--stdin", action="store_true", help="Read message text from standard input.")
    parser.add_argument("--send", action="store_true", help="Actually send; otherwise only validate input.")
    args = parser.parse_args()

    message = sys.stdin.read() if args.stdin else args.message
    message = str(message or "").strip()
    recipient = str(args.recipient or "").strip()
    if not message or not recipient:
        parser.error("recipient and message are required")
    if not args.send:
        print("[amsc_sms] validated; pass --send to submit to Messages")
        return 0
    if not _send_sms_message(message, recipient):
        print("[amsc_sms] Messages submission failed", file=sys.stderr)
        return 1
    # osascript acknowledges the local Messages command, not handset delivery.
    print(f"[amsc_sms] submitted to Messages for {recipient[:3]}***{recipient[-2:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
