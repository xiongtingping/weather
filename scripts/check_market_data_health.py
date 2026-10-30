#!/usr/bin/env python3
from __future__ import annotations

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

from bayes_basket_target.database import MARKET_WS_HOT_DIR

DB = ROOT / "data" / "archive.db"
# The archive above is the tick lane.  It is not the lane the basket trades on,
# and watching only it is how the target quote lane stopped for twenty-eight
# hours on 2026-08-22 with this check reporting ok the whole time: the target
# SSOT had hit its own byte ceiling and every quote write was failing closed,
# which nothing here could see because nothing here opened that database.
TARGET_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
HEARTBEAT = MARKET_WS_HOT_DIR / "market_ws_heartbeat.json"


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round(max(0.0, min(1.0, q)) * (len(ordered) - 1)))
    return ordered[idx]


def main() -> int:
    blockers: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}
    now = time.time()
    heartbeat_not_ok = False
    latest_age: int | None = None

    if HEARTBEAT.exists():
        try:
            heartbeat = json.loads(HEARTBEAT.read_text(encoding="utf-8")) or {}
        except Exception:
            heartbeat = {}
        hb_ts = _parse_ts(heartbeat.get("updated_at"))
        hb_age = int(max(0, now - hb_ts)) if hb_ts else None
        details["heartbeat"] = heartbeat
        details["heartbeat_age_sec"] = hb_age
        if hb_age is None or hb_age > 60:
            blockers.append("market_ws_heartbeat_stale")
        elif not heartbeat.get("ok"):
            heartbeat_not_ok = True
    else:
        blockers.append("market_ws_heartbeat_missing")

    if not DB.exists():
        blockers.append("archive_db_missing")
    else:
        with sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            latest = conn.execute(
                """
                SELECT id, created_at AS ts
                FROM event_market_ticks
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            latest_ts = _parse_ts(latest["ts"] if latest else "")
            latest_age = int(max(0, now - latest_ts)) if latest_ts else None
            details["latest_tick_at"] = latest["ts"] if latest else None
            details["latest_tick_age_sec"] = latest_age
            details["tick_high_watermark_id"] = int((latest["id"] if latest else 0) or 0)
            if latest_age is None or latest_age > 120:
                blockers.append("market_ticks_stale")

            recent_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
            recent_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM event_market_ticks WHERE created_at >= ?",
                (recent_cutoff,),
            ).fetchone()
            details["recent_tick_rows_10m"] = int((recent_rows["n"] if recent_rows else 0) or 0)

            rows = conn.execute(
                """
                SELECT slug, event_date, MAX(created_at) AS ts
                FROM event_market_ticks
                WHERE created_at >= ?
                  AND event_date >= date('now', '-1 day')
                  AND event_date <= date('now', '+3 day')
                GROUP BY slug, event_date
                """,
                (recent_cutoff,),
            ).fetchall()
            ages = []
            stale_examples = []
            for row in rows:
                ts = _parse_ts(row["ts"])
                if ts is None:
                    continue
                age = int(max(0, now - ts))
                ages.append(age)
                if age > 180 and len(stale_examples) < 5:
                    stale_examples.append(
                        {
                            "slug": row["slug"],
                            "event_date": row["event_date"],
                            "age_sec": age,
                        }
                    )
            details["event_tick_age_p90_sec"] = _percentile(ages, 0.90)
            details["event_tick_age_p99_sec"] = _percentile(ages, 0.99)
            details["recent_active_slug_events_10m"] = len(ages)
            details["stale_event_examples"] = stale_examples
            if details["recent_tick_rows_10m"] == 0:
                blockers.append("market_ticks_no_recent_rows")
            elif ages and (details["event_tick_age_p90_sec"] or 0) > 180:
                warnings.append("market_tick_event_p90_stale")

    # The target quote lane, which is what the basket actually prices against.
    # Two independent ways it dies, both observed: the evidence stops arriving,
    # or the store runs out of the bytes its own guard allows.  The second one
    # causes the first, so both are reported rather than folded together.
    if not TARGET_DB.exists():
        blockers.append("target_ssot_missing")
    else:
        quote_max_age = _env_int("PM_HEALTH_TARGET_QUOTE_MAX_AGE_SEC", 3600)
        try:
            with sqlite3.connect(f"file:{TARGET_DB}?mode=ro", uri=True, timeout=15) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT MAX(observed_at) AS ts"
                    " FROM basket_official_ws_evidence_cache"
                ).fetchone()
        except sqlite3.Error as exc:
            blockers.append("target_quote_lane_unreadable")
            details["target_quote_lane_error"] = f"{type(exc).__name__}:{exc}"
        else:
            quote_ts = _parse_ts(row["ts"] if row else "")
            quote_age = int(max(0, now - quote_ts)) if quote_ts else None
            details["target_quote_observed_at"] = row["ts"] if row else None
            details["target_quote_age_sec"] = quote_age
            details["target_quote_max_age_sec"] = quote_max_age
            if quote_age is None or quote_age > quote_max_age:
                blockers.append("target_quote_lane_stale")

        # Read the ceiling from the writers' own constant rather than repeating
        # the number here, so raising it in one place cannot leave this check
        # asserting a limit production no longer uses.
        try:
            from bayes_basket_target.resource_bounds import MAX_TARGET_DB_BYTES
        except Exception as exc:
            warnings.append("target_capacity_bound_unreadable")
            details["target_capacity_error"] = f"{type(exc).__name__}:{exc}"
        else:
            db_bytes = int(TARGET_DB.stat().st_size)
            remaining = MAX_TARGET_DB_BYTES - db_bytes
            warn_remaining = _env_int(
                "PM_HEALTH_TARGET_CAPACITY_WARN_BYTES", 1024 * 1024 * 1024
            )
            details["target_database_bytes"] = db_bytes
            details["target_database_remaining_bytes"] = remaining
            if remaining <= 0:
                blockers.append("target_database_bytes_exceeded")
            elif remaining < warn_remaining:
                warnings.append("target_database_headroom_low")

    if heartbeat_not_ok:
        grace_sec = _env_int("PM_HEALTH_MARKET_WS_ERROR_GRACE_SEC", 60)
        if grace_sec <= 0:
            grace_sec = 60
        if latest_age is not None and latest_age <= grace_sec:
            details["market_ws_heartbeat_not_ok_suppressed"] = {
                "latest_tick_age_sec": latest_age,
                "grace_sec": grace_sec,
                "reason": "latest market tick is still fresh",
            }
        else:
            warnings.append("market_ws_heartbeat_not_ok")

    output = {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "details": details,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
