#!/usr/bin/env python3
"""Read-only watcher for the next live AWC/posterior target -> order path."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from posterior_target_live_job_audit import audit as audit_posterior_job


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_SLUG = "highest-temperature-in-chengdu-on-july-2-2026"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _connect_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


def _load_json(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or ""))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _float_or_none(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _target_block(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _features(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("weather_likelihood_features")
    if isinstance(value, Mapping):
        return value
    value = payload.get("weather_likelihood_features_compact")
    return value if isinstance(value, Mapping) else {}


def _latest_fact_signal(conn: sqlite3.Connection, *, slug: str, event_date: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT h.id, h.fact_as_of, h.slug, h.event_date, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE (? = '' OR h.slug = ?)
          AND (? = '' OR h.event_date = ?)
        ORDER BY h.id DESC
        LIMIT 1
        """,
        (slug, slug, event_date, event_date),
    ).fetchone()
    if row is None:
        return {"ok": False, "reason": "latest_fact_missing"}
    payload = _load_json(row["fact_json"])
    target = _target_block(payload)
    features = _features(payload)
    target_bucket = str(
        target.get("target_bucket")
        or payload.get("target_bucket")
        or payload.get("ssot_target_bucket")
        or ""
    ).strip()
    target_probability = _float_or_none(
        target.get("target_probability")
        or target.get("target_posterior")
        or payload.get("target_bucket_probability_pct")
    )
    if target_probability is not None and target_probability > 1.0:
        target_probability /= 100.0
    signature = {
        "fact_id": int(row["id"]),
        "target_bucket": target_bucket,
        "target_probability": target_probability,
        "awc_latest_at": str(features.get("zuuu_metar_raw_awc_latest_at") or ""),
        "awc_today_max_c": _float_or_none(features.get("zuuu_metar_raw_awc_today_max_c")),
    }
    return {
        "ok": True,
        "fact_id": int(row["id"]),
        "fact_as_of": row["fact_as_of"],
        "slug": row["slug"],
        "event_date": row["event_date"],
        "target_bucket": target_bucket,
        "target_probability": target_probability,
        "awc_latest_at": signature["awc_latest_at"],
        "awc_today_max_c": signature["awc_today_max_c"],
        "awc_fetch_hours": features.get("zuuu_metar_raw_awc_fetch_hours"),
        "awc_fetch_hours_reason": features.get("zuuu_metar_raw_awc_fetch_hours_reason"),
        "signature": signature,
    }


def _max_id(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COALESCE(MAX(id), 0) AS max_id FROM {table}").fetchone()
        return int(row["max_id"] or 0)
    except Exception:
        return 0


def _latest_job_after(
    conn: sqlite3.Connection,
    *,
    after_id: int,
    slug: str,
    event_date: str,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM auto_trade_jobs
        WHERE id > ?
          AND source = 'posterior_target_direct'
          AND (? = '' OR payload_json LIKE ?)
          AND (? = '' OR payload_json LIKE ?)
        ORDER BY id DESC
        LIMIT 1
        """,
        (after_id, slug, f"%{slug}%", event_date, f"%{event_date}%"),
    ).fetchone()


def _recent_failed_decision_after(
    conn: sqlite3.Connection,
    *,
    after_id: int,
    slug: str,
    event_date: str,
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT *
        FROM auto_trade_decisions
        WHERE id > ?
          AND reason_codes_json LIKE '%place_order_failed%'
          AND (? = '' OR slug = ?)
          AND (? = '' OR event_date = ?)
        ORDER BY id DESC
        LIMIT 1
        """,
        (after_id, slug, slug, event_date, event_date),
    ).fetchone()
    if row is None:
        return None
    details = _load_json(row["details_json"])
    return {
        "id": int(row["id"]),
        "created_at": row["created_at"],
        "outcome": row["outcome"],
        "side": row["side"],
        "action": row["action"],
        "decided_state": row["decided_state"],
        "raw_return_fields_present": {
            "order_stderr": "order_stderr" in details,
            "order_stdout": "order_stdout" in details,
            "order_raw_payload_json": "order_raw_payload_json" in details,
        },
    }


def _signature_changes(baseline: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, bool]:
    base = baseline.get("signature") if isinstance(baseline.get("signature"), Mapping) else {}
    now = current.get("signature") if isinstance(current.get("signature"), Mapping) else {}
    return {
        "fact_id_changed": base.get("fact_id") != now.get("fact_id"),
        "target_bucket_changed": base.get("target_bucket") != now.get("target_bucket"),
        "target_probability_changed": base.get("target_probability") != now.get("target_probability"),
        "awc_latest_at_changed": base.get("awc_latest_at") != now.get("awc_latest_at"),
        "awc_today_max_changed": base.get("awc_today_max_c") != now.get("awc_today_max_c"),
    }


def watch(
    db: Path,
    *,
    slug: str,
    event_date: str,
    timeout_sec: float,
    poll_sec: float,
    max_job_wait_sec: float,
) -> dict[str, Any]:
    started_at = _iso_now()
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    with _connect_ro(db) as conn:
        baseline = _latest_fact_signal(conn, slug=slug, event_date=event_date)
        baseline_job_id = _max_id(conn, "auto_trade_jobs")
        baseline_decision_id = _max_id(conn, "auto_trade_decisions")
    last_current = baseline
    event_seen_at = ""
    triggered_by: list[str] = []
    job_row: Optional[sqlite3.Row] = None
    while time.monotonic() <= deadline:
        with _connect_ro(db) as conn:
            current = _latest_fact_signal(conn, slug=slug, event_date=event_date)
            changes = _signature_changes(baseline, current)
            changed_keys = [key for key, changed in changes.items() if changed]
            job_row = _latest_job_after(
                conn,
                after_id=baseline_job_id,
                slug=slug,
                event_date=event_date,
            )
            last_current = current
        if changed_keys or job_row is not None:
            event_seen_at = _iso_now()
            triggered_by = changed_keys + (["posterior_target_direct_job"] if job_row else [])
            break
        time.sleep(max(0.2, float(poll_sec)))

    audit_result: dict[str, Any] = {}
    if job_row is not None:
        wait_deadline = time.monotonic() + max(0.0, float(max_job_wait_sec))
        while True:
            audit_result = audit_posterior_job(
                db,
                slug=slug,
                event_date=event_date,
                since=started_at,
                require_job=True,
            )
            order_decision = (audit_result.get("details") or {}).get("order_decision")
            if order_decision or time.monotonic() >= wait_deadline:
                break
            time.sleep(max(0.2, float(poll_sec)))

    with _connect_ro(db) as conn:
        failed_decision = _recent_failed_decision_after(
            conn,
            after_id=baseline_decision_id,
            slug=slug,
            event_date=event_date,
        )

    target_changed = any(
        key in triggered_by for key in ("target_bucket_changed", "target_probability_changed")
    )
    status = "timeout_no_change"
    ok = True
    blockers: list[str] = []
    if triggered_by:
        if job_row is not None:
            status = "job_seen"
            ok = bool(audit_result.get("ok"))
            blockers = list(audit_result.get("blockers") or [])
        elif target_changed:
            status = "target_changed_without_job"
            ok = False
            blockers.append("target_or_probability_changed_without_posterior_target_job")
        else:
            status = "fact_or_awc_changed_target_unchanged_no_job_expected"

    return {
        "ok": ok,
        "status": status,
        "blockers": blockers,
        "started_at": started_at,
        "event_seen_at": event_seen_at,
        "triggered_by": triggered_by,
        "baseline": baseline,
        "current": last_current,
        "job": (
            {
                "id": int(job_row["id"]),
                "state": job_row["state"],
                "source": job_row["source"],
                "created_at": job_row["created_at"],
                "started_at": job_row["started_at"],
                "ended_at": job_row["ended_at"],
                "updated_at": job_row["updated_at"],
                "last_error": job_row["last_error"],
            }
            if job_row is not None
            else None
        ),
        "posterior_target_live_job_audit": audit_result or None,
        "place_order_failed_after_start": failed_decision,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--slug", default=DEFAULT_SLUG)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--timeout-sec", type=float, default=70.0)
    parser.add_argument("--poll-sec", type=float, default=1.0)
    parser.add_argument("--max-job-wait-sec", type=float, default=10.0)
    args = parser.parse_args()
    result = watch(
        Path(args.db),
        slug=str(args.slug or ""),
        event_date=str(args.event_date or ""),
        timeout_sec=float(args.timeout_sec),
        poll_sec=float(args.poll_sec),
        max_job_wait_sec=float(args.max_job_wait_sec),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
