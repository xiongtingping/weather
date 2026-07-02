#!/usr/bin/env python3
"""Read-only CLOB order service submit latency acceptance audit."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_SERVICE_URL = "http://127.0.0.1:8765"


def _load_json(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(str(value))
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _since_from_hours(hours: Optional[float]) -> str:
    if hours is None:
        return ""
    return (datetime.now(timezone.utc) - timedelta(hours=float(hours))).isoformat(timespec="seconds")


def _http_json(url: str, *, timeout: float = 1.5) -> tuple[bool, dict[str, Any], str, float]:
    started = time.monotonic()
    try:
        with urlopen(url, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
    except Exception as exc:
        return False, {}, f"{type(exc).__name__}: {exc}", (time.monotonic() - started) * 1000.0
    elapsed_ms = (time.monotonic() - started) * 1000.0
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except Exception as exc:
        return False, {}, f"json_parse_failed:{type(exc).__name__}: {exc}", elapsed_ms
    return True, payload if isinstance(payload, dict) else {}, "", elapsed_ms


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _order_id(details: Mapping[str, Any]) -> str:
    return str(
        details.get("order_id")
        or details.get("orderID")
        or details.get("orderId")
        or details.get("id")
        or ""
    ).strip()


def _is_service_order(details: Mapping[str, Any]) -> bool:
    return bool(
        details.get("clob_order_service") is True
        or str(details.get("source") or "").strip() == "clob_order_service"
        or details.get("service_submit_ms") is not None
        or details.get("clob_order_service_ms") is not None
    )


def _decision_summary(row: sqlite3.Row) -> dict[str, Any]:
    details = _load_json(row["details_json"])
    if not isinstance(details, dict):
        details = {}
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "slug": row["slug"],
        "outcome": row["outcome"],
        "side": row["side"],
        "action": row["action"],
        "decided_state": row["decided_state"],
        "reason_codes": _load_json(row["reason_codes_json"]) or [],
        "order_id": _order_id(details),
        "is_service_order": _is_service_order(details),
        "order_submit_ms": _to_float(details.get("order_submit_ms")),
        "service_submit_ms": _to_float(details.get("service_submit_ms")),
        "clob_order_service_ms": _to_float(details.get("clob_order_service_ms")),
        "service_pid": details.get("service_pid"),
        "idempotency_key": details.get("idempotency_key"),
    }


def audit(
    db_path: Path | str = DEFAULT_DB,
    *,
    service_url: str = DEFAULT_SERVICE_URL,
    since: str = "",
    since_hours: Optional[float] = None,
    since_service_start: bool = True,
    max_order_submit_ms: float = 3000.0,
    max_service_submit_ms: float = 3000.0,
    require_order: bool = False,
) -> dict[str, Any]:
    service_url = str(service_url or DEFAULT_SERVICE_URL).rstrip("/")
    health_ok, health, health_error, health_ms = _http_json(f"{service_url}/health", timeout=1.5)
    since_text = str(since or "").strip() or _since_from_hours(since_hours)
    if since_service_start and health.get("started_at"):
        service_started_at = str(health.get("started_at") or "")
        if service_started_at:
            since_text = service_started_at
    db = Path(db_path)
    conn = _connect_readonly(db)
    try:
        params: list[Any] = []
        where = ["reason_codes_json LIKE '%order_submitted%'"]
        if since_text:
            where.append("created_at >= ?")
            params.append(since_text)
        rows = conn.execute(
            f"""
            SELECT id, created_at, slug, outcome, side, action, decided_state,
                   reason_codes_json, details_json
            FROM auto_trade_decisions
            WHERE {' AND '.join(where)}
            ORDER BY id DESC
            LIMIT 50
            """,
            params,
        ).fetchall()
        failed_params: list[Any] = []
        failed_where = ["reason_codes_json LIKE '%place_order_failed%'"]
        if since_text:
            failed_where.append("created_at >= ?")
            failed_params.append(since_text)
        failed_count = conn.execute(
            f"SELECT COUNT(*) AS n FROM auto_trade_decisions WHERE {' AND '.join(failed_where)}",
            failed_params,
        ).fetchone()["n"]
    finally:
        conn.close()

    submitted = [_decision_summary(row) for row in rows]
    service_submitted = [item for item in submitted if item.get("is_service_order")]
    latest_service_order = service_submitted[0] if service_submitted else None
    latency_checks: dict[str, bool] = {}
    if latest_service_order:
        order_submit_ms = _to_float(latest_service_order.get("order_submit_ms"))
        service_submit_ms = _to_float(latest_service_order.get("service_submit_ms"))
        clob_service_ms = _to_float(latest_service_order.get("clob_order_service_ms"))
        latency_checks["order_id_present"] = bool(latest_service_order.get("order_id"))
        latency_checks["order_submit_ms_lte_budget"] = (
            order_submit_ms is not None and order_submit_ms <= float(max_order_submit_ms)
        )
        latency_checks["service_submit_ms_lte_budget"] = (
            service_submit_ms is not None and service_submit_ms <= float(max_service_submit_ms)
        )
        latency_checks["clob_order_service_ms_lte_budget"] = (
            clob_service_ms is not None and clob_service_ms <= float(max_order_submit_ms)
        )
    status = "verified" if latest_service_order and all(latency_checks.values()) else "verification_pending_no_service_order"
    if latest_service_order and not all(latency_checks.values()):
        status = "service_order_latency_or_confirmation_failed"
    blockers: list[str] = []
    if require_order and not latest_service_order:
        blockers.append("service_order_submit_missing")
    if latest_service_order:
        blockers.extend([name for name, passed in latency_checks.items() if not passed])
    return {
        "ok": not blockers,
        "status": status,
        "blockers": blockers,
        "service": {
            "url": service_url,
            "health_ok": bool(health_ok and health.get("ok") is True),
            "health_ms": round(float(health_ms), 3),
            "health_error": health_error,
            "started_at": health.get("started_at"),
            "pid": health.get("pid"),
            "metadata_cache_size": health.get("metadata_cache_size"),
            "last_heartbeat_at": health.get("last_heartbeat_at"),
            "last_heartbeat_error": health.get("last_heartbeat_error"),
        },
        "window": {
            "since": since_text,
            "since_service_start": bool(since_service_start),
        },
        "budgets": {
            "max_order_submit_ms": float(max_order_submit_ms),
            "max_service_submit_ms": float(max_service_submit_ms),
        },
        "place_order_failed_count": int(failed_count or 0),
        "latest_service_order": latest_service_order,
        "latest_submitted_order": submitted[0] if submitted else None,
        "service_order_count": len(service_submitted),
        "submitted_order_count": len(submitted),
        "latency_checks": latency_checks,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only CLOB order service submit latency acceptance audit.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--service-url", default=DEFAULT_SERVICE_URL)
    parser.add_argument("--since", default="")
    parser.add_argument("--since-hours", type=float, default=None)
    parser.add_argument("--since-service-start", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-order-submit-ms", type=float, default=3000.0)
    parser.add_argument("--max-service-submit-ms", type=float, default=3000.0)
    parser.add_argument("--require-order", action="store_true")
    args = parser.parse_args(argv)
    result = audit(
        args.db,
        service_url=args.service_url,
        since=args.since,
        since_hours=args.since_hours,
        since_service_start=args.since_service_start,
        max_order_submit_ms=args.max_order_submit_ms,
        max_service_submit_ms=args.max_service_submit_ms,
        require_order=args.require_order,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
