#!/usr/bin/env python3
"""Daily IEM truth audit: settled station-days against IEM's copy of the reports.

User ruling 2026-09-25 (「好」 to 「把 IEM 真值对账做成每日只读检查」); the
rules are ``bayes_basket_target.iem_truth_audit``.  A reader: the SSOT is read
once through a read-only connection that is closed before any network work,
IEM is queried once per station through the explicit official-download
proxy, and the only writes are ``data/iem_truth_audit_status.json`` (replaced
atomically) and ``data/iem_truth_audit_history.jsonl`` (each disagreement
appended once).  It never takes a writer turn and never changes truth.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from bayes_basket_target.iem_truth_audit import (  # noqa: E402
    MAX_DAYS,
    USER_AGENT,
    audit,
    fetch_iem_reports,
    history_key,
    iem_station_id,
    standing_truths,
    status_payload,
)
from official_nwp_https_transport import official_nwp_proxy_url  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATUS_JSON = ROOT / "data" / "iem_truth_audit_status.json"
HISTORY_JSONL = ROOT / "data" / "iem_truth_audit_history.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _append_history(path: Path, disagreements: list[dict[str, Any]], *, checked_at: str) -> int:
    seen: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["key"])
            except (ValueError, KeyError, TypeError):
                continue
    fresh = [
        {"key": history_key(row), "first_seen_at": checked_at, **row}
        for row in disagreements
        if history_key(row) not in seen
    ]
    if fresh:
        with path.open("a", encoding="utf-8") as handle:
            for row in fresh:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(fresh)


def run(
    *,
    days: int,
    db: Path,
    status_path: Path,
    history_path: Path,
    dry_run: bool,
    pause_seconds: float,
    retry_pause_seconds: float = 90.0,
) -> dict[str, Any]:
    days = max(1, min(MAX_DAYS, int(days)))
    today = datetime.now(timezone.utc).date()
    first_date, last_date = (today - timedelta(days=days)).isoformat(), today.isoformat()
    reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    try:
        reader.execute("PRAGMA query_only=1")
        truths = standing_truths(reader, first_date=first_date, last_date=last_date)
    finally:
        reader.close()
    with httpx.Client(
        proxy=official_nwp_proxy_url() or None,
        trust_env=False,
        timeout=httpx.Timeout(90.0, connect=20.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=False,
    ) as client:
        calls = 0

        def fetch(station: str, first_utc_date, last_utc_date):
            nonlocal calls
            if calls:
                time.sleep(pause_seconds)
            calls += 1
            try:
                return fetch_iem_reports(
                    client,
                    iem_id=iem_station_id(station),
                    first_utc_date=first_utc_date,
                    last_utc_date=last_utc_date,
                )
            except httpx.HTTPError as exc:
                raise OSError(f"{type(exc).__name__}:{exc}") from exc

        rows = audit(truths, fetch=fetch)
        # One second pass for the stations IEM throttled or refused, after a
        # longer pause; what still fails stays IEM_FETCH_FAILED and is listed.
        failed = {row["station_id"] for row in rows if row.get("fetch_error")}
        if failed:
            time.sleep(retry_pause_seconds)
            again = audit(
                {key: value for key, value in truths.items() if key[0] in failed},
                fetch=fetch,
            )
            retried = {(row["station_id"], row["event_date"]): row for row in again}
            rows = [retried.get((row["station_id"], row["event_date"]), row) for row in rows]
    checked_at = _now()
    status = status_payload(rows, first_date=first_date, last_date=last_date, checked_at=checked_at)
    if dry_run:
        return status
    status["history_appended"] = _append_history(
        history_path, status["disagreements"], checked_at=checked_at
    )
    temporary = status_path.with_name(f".{status_path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temporary, status_path)
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--status-out", type=Path, default=STATUS_JSON)
    parser.add_argument("--history-out", type=Path, default=HISTORY_JSONL)
    parser.add_argument("--pause-seconds", type=float, default=4.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    status = run(
        days=args.days, db=args.db, status_path=args.status_out,
        history_path=args.history_out, dry_run=args.dry_run,
        pause_seconds=args.pause_seconds,
    )
    brief = {key: status[key] for key in ("checked_at", "window", "station_day_count", "counts", "agree_share")}
    brief["disagreements"] = len(status["disagreements"])
    brief["fetch_failed_stations"] = sorted(status["fetch_failed_stations"])
    print(json.dumps(brief, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
