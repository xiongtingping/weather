#!/usr/bin/env python3
"""Read-only acceptance audit for hard-source posterior SSOT handoff."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_ENV = ROOT / ".env"
MARKET_PREFETCH_LOG = ROOT / "data" / "logs" / "hard_source_direct_market_prefetch_bg.log"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RUNTIME_ENV_KEYS = (
    "PM_AUTO_TRADE_ENABLED",
    "PM_AUTO_TRADE_DRY_RUN",
    "PM_HARD_SOURCE_DIRECT_WILL_ORDER",
    "PM_HARD_SOURCE_DIRECT_ENQUEUE_JOB",
    "PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE",
    "PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED",
    "PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE",
    "PM_HARD_SOURCE_DIRECT_PEAK_LEAD_HOURS",
    "PM_HARD_SOURCE_DIRECT_PEAK_POST_HOURS",
    "PM_HARD_SOURCE_DIRECT_NEAR_PEAK_ENABLED",
    "PM_HARD_SOURCE_DIRECT_NEAR_PEAK_POLL_SEC",
    "PM_HARD_SOURCE_WU_TWC_PEAK_POLL_SEC",
    "PM_HARD_SOURCE_WU_TWC_NORMAL_POLL_SEC",
    "PM_HARD_SOURCE_METAR_PWS_ENABLED",
    "PM_HARD_SOURCE_METAR_IEM_POLL_SEC",
    "PM_HARD_SOURCE_METAR_IEM_PEAK_POLL_SEC",
    "PM_HARD_SOURCE_METAR_IEM_NORMAL_POLL_SEC",
    "PM_HARD_SOURCE_METAR_AVWX_POLL_SEC",
    "PM_HARD_SOURCE_METAR_AVWX_PEAK_POLL_SEC",
    "PM_HARD_SOURCE_METAR_AVWX_NORMAL_POLL_SEC",
    "PM_HARD_SOURCE_METAR_CHECKWX_POLL_SEC",
    "PM_HARD_SOURCE_METAR_CHECKWX_PEAK_POLL_SEC",
    "PM_HARD_SOURCE_METAR_CHECKWX_NORMAL_POLL_SEC",
    "PM_HARD_SOURCE_TWC_PWS_RAPID_POLL_SEC",
    "PM_HARD_SOURCE_TWC_PWS_RAPID_PEAK_POLL_SEC",
    "PM_HARD_SOURCE_TWC_PWS_RAPID_NORMAL_POLL_SEC",
    "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_ENABLED",
    "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_HEARTBEAT_SEC",
    "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_MAX_OUTCOMES",
    "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_BUDGET_SEC",
    "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CLOB_TIMEOUT_SEC",
    "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CLOB_RETRIES",
    "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_WORKERS",
    "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CACHE_ONLY",
    "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CACHE_BOOTSTRAP",
    "PM_HARD_SOURCE_DIRECT_WORKER_PREFETCH_MARKET_TICKS",
    "PM_HARD_SOURCE_DIRECT_PREFETCH_MAX_ATTEMPTS",
    "PM_HARD_SOURCE_DIRECT_PREFETCH_BUDGET_SEC",
    "PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS",
    "PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS",
    "PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC",
    "PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_PRECHECK",
    "PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_JOB_PAYLOAD",
    "PM_POSTERIOR_TARGET_DIRECT_ENQUEUE_CLEANUP_ONLY",
    "PM_AUTO_TRADE_DIRECT_ORDER_TIMEOUT_SEC",
    "PM_AUTO_TRADE_DIRECT_ORDER_TIMEOUT_MIN_SEC",
    "PM_AUTO_TRADE_DIRECT_PROXY_FALLBACK_ENABLED",
    "PM_AUTO_TRADE_DIRECT_FORCE_PROXY_ON_FALLBACK",
    "PM_AUTO_TRADE_DIRECT_ENTRY_CLOB_TIMEOUT_CAP_MS",
    "PM_AUTO_TRADE_DIRECT_ENTRY_CLOB_TIMEOUT_PADDING_MS",
    "PM_AUTO_TRADE_DIRECT_ENTRY_OPERATION_TIMEOUT_MS",
    "PM_CLOB_ORDER_SERVICE_ENABLED",
    "PM_CLOB_ORDER_SERVICE_URL",
    "PM_CLOB_ORDER_SERVICE_PORT",
    "PM_CLOB_ORDER_SERVICE_REQUEST_TIMEOUT_SEC",
    "PM_CLOB_ORDER_SERVICE_IDEMPOTENCY_TTL_SEC",
)


def _load_json(text: Any) -> Any:
    if not text:
        return None
    try:
        return json.loads(str(text))
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        val = float(value)
    except Exception:
        return None
    if not math.isfinite(val):
        return None
    return val


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        parsed = _to_float(value)
        if parsed is not None:
            return parsed
    return None


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _utc_cutoff(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=float(hours))).isoformat(timespec="seconds")


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


def _age_sec_from_mtime(path: Path) -> Optional[float]:
    try:
        return max(0.0, datetime.now(timezone.utc).timestamp() - float(path.stat().st_mtime))
    except Exception:
        return None


def _env_truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _parse_env_file(path: Path = DEFAULT_ENV) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in RUNTIME_ENV_KEYS:
            continue
        value = value.strip()
        if value and value[0] in {"'", '"'} and value[-1:] == value[0]:
            value = value[1:-1]
        values[key] = value
    return values


@contextmanager
def _temporary_env_defaults(values: Mapping[str, Any]):
    previous: dict[str, Optional[str]] = {}
    changed: list[str] = []
    try:
        for key, value in values.items():
            if key not in RUNTIME_ENV_KEYS or value in (None, ""):
                continue
            if os.environ.get(key) not in (None, ""):
                continue
            previous[key] = os.environ.get(key)
            os.environ[key] = str(value)
            changed.append(key)
        yield
    finally:
        for key in changed:
            old = previous.get(key)
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _direct_entry_order_budget(file_env: Mapping[str, Any]) -> dict[str, Any]:
    try:
        import scan_weather_market as swm  # type: ignore
    except Exception as exc:
        return {
            "available": False,
            "reason": "scan_weather_market_import_failed",
            "error": f"{type(exc).__name__}:{exc}",
        }
    with _temporary_env_defaults(file_env):
        try:
            timeout_sec = float(swm._direct_target_order_timeout_sec())
            overrides = swm._direct_target_order_env_overrides(
                timeout_sec=timeout_sec,
                exit_order=False,
            )
            service_enabled = bool(swm._clob_order_service_enabled())
            service_url = str(swm._clob_order_service_url())
            service_request_timeout_sec = float(
                swm._clob_order_service_request_timeout_sec(timeout_sec)
            )
        except Exception as exc:
            return {
                "available": False,
                "reason": "direct_entry_order_budget_failed",
                "error": f"{type(exc).__name__}:{exc}",
            }
    return {
        "available": True,
        "timeout_sec": timeout_sec,
        "clob_timeout_ms": _to_float(overrides.get("PM_CLOB_TIMEOUT_MS")),
        "command_hard_timeout_ms": _to_float(overrides.get("PM_CLOB_COMMAND_HARD_TIMEOUT_MS")),
        "place_order_hard_timeout_ms": _to_float(overrides.get("PM_CLOB_PLACE_ORDER_HARD_TIMEOUT_MS")),
        "operation_timeout_ms": _to_float(overrides.get("PM_CLOB_PLACE_ORDER_OPERATION_TIMEOUT_MS")),
        "force_direct": _env_truthy(overrides.get("PM_CLOB_FORCE_DIRECT"), False),
        "force_proxy": _env_truthy(overrides.get("PM_CLOB_FORCE_PROXY"), False),
        "allow_proxy_fallback": _env_truthy(
            overrides.get("PM_CLOB_FAST_ALLOW_PROXY_FALLBACK"),
            False,
        ),
        "place_order_max_attempts": _to_float(overrides.get("PM_CLOB_PLACE_ORDER_MAX_ATTEMPTS")),
        "clob_order_service_enabled": service_enabled,
        "clob_order_service_url": service_url,
        "clob_order_service_request_timeout_sec": service_request_timeout_sec,
        "clob_order_service_idempotency_ttl_sec": _to_float(
            file_env.get("PM_CLOB_ORDER_SERVICE_IDEMPOTENCY_TTL_SEC")
        ),
    }


def _direct_entry_order_budget_ok(budget: Mapping[str, Any]) -> bool:
    if not budget.get("available"):
        return False
    timeout_sec = _to_float(budget.get("timeout_sec"))
    clob_timeout_ms = _to_float(budget.get("clob_timeout_ms"))
    command_hard_timeout_ms = _to_float(budget.get("command_hard_timeout_ms"))
    place_order_hard_timeout_ms = _to_float(budget.get("place_order_hard_timeout_ms"))
    operation_timeout_ms = _to_float(budget.get("operation_timeout_ms"))
    attempts = _to_float(budget.get("place_order_max_attempts"))
    if budget.get("clob_order_service_enabled") is True:
        service_timeout_sec = _to_float(budget.get("clob_order_service_request_timeout_sec"))
        service_ttl_sec = _to_float(budget.get("clob_order_service_idempotency_ttl_sec"))
        return bool(
            service_timeout_sec is not None
            and service_timeout_sec <= 8.0
            and service_ttl_sec is not None
            and service_ttl_sec > 0
        )
    return bool(
        timeout_sec is not None
        and timeout_sec <= 3.0
        and clob_timeout_ms is not None
        and clob_timeout_ms <= 2500.0
        and command_hard_timeout_ms is not None
        and command_hard_timeout_ms <= 3000.0
        and place_order_hard_timeout_ms is not None
        and place_order_hard_timeout_ms <= 3000.0
        and operation_timeout_ms is not None
        and operation_timeout_ms <= 2800.0
        and attempts is not None
        and attempts <= 1.0
        and budget.get("force_direct") is True
        and budget.get("force_proxy") is False
        and budget.get("allow_proxy_fallback") is False
    )


def _selected_runtime_config(env_path: Path = DEFAULT_ENV) -> dict[str, Any]:
    file_env = _parse_env_file(env_path)

    def value(name: str, default: Any = None) -> Any:
        raw = os.environ.get(name)
        if raw not in (None, ""):
            return raw
        return file_env.get(name, default)

    auto_trade_enabled = _env_truthy(value("PM_AUTO_TRADE_ENABLED"), False)
    dry_run = _env_truthy(value("PM_AUTO_TRADE_DRY_RUN"), True)
    will_order = _env_truthy(value("PM_HARD_SOURCE_DIRECT_WILL_ORDER"), True)
    worker_inline = _env_truthy(value("PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE"), True)
    enqueue_job = _env_truthy(value("PM_HARD_SOURCE_DIRECT_ENQUEUE_JOB"), True)
    hard_worker_prefetch = value("PM_HARD_SOURCE_DIRECT_WORKER_PREFETCH_MARKET_TICKS", "0")
    hard_prefetch_attempts = value("PM_HARD_SOURCE_DIRECT_PREFETCH_MAX_ATTEMPTS", "0")
    hard_prefetch_budget_sec = value("PM_HARD_SOURCE_DIRECT_PREFETCH_BUDGET_SEC", "0")
    return {
        "auto_trade_enabled": auto_trade_enabled,
        "auto_trade_dry_run": dry_run,
        "live_order_mode": bool(auto_trade_enabled and not dry_run and will_order),
        "hard_source_will_order": will_order,
        "hard_source_enqueue_job": enqueue_job,
        "hard_source_run_worker_once": worker_inline,
        "posterior_target_direct": {
            "primary_only_precheck": _env_truthy(
                value("PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_PRECHECK"),
                True,
            ),
            "primary_only_job_payload": _env_truthy(
                value("PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_JOB_PAYLOAD"),
                True,
            ),
            "enqueue_cleanup_only": _env_truthy(
                value("PM_POSTERIOR_TARGET_DIRECT_ENQUEUE_CLEANUP_ONLY"),
                False,
            ),
        },
        "decision_window": {
            "enabled": _env_truthy(value("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED"), True),
            "mode": value("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE", "wu_forecast"),
            "peak_lead_hours": value("PM_HARD_SOURCE_DIRECT_PEAK_LEAD_HOURS", "1.0"),
            "peak_post_hours": value("PM_HARD_SOURCE_DIRECT_PEAK_POST_HOURS", "1.5"),
            "near_peak_enabled": _env_truthy(value("PM_HARD_SOURCE_DIRECT_NEAR_PEAK_ENABLED"), True),
            "near_peak_poll_sec": value("PM_HARD_SOURCE_DIRECT_NEAR_PEAK_POLL_SEC", "1.0"),
            "wu_twc_peak_poll_sec": value("PM_HARD_SOURCE_WU_TWC_PEAK_POLL_SEC", "1"),
            "wu_twc_normal_poll_sec": value("PM_HARD_SOURCE_WU_TWC_NORMAL_POLL_SEC", "30"),
        },
        "inline_worker_prefetch": {
            "market_ticks_enabled": _env_truthy(hard_worker_prefetch, False),
            "max_attempts": hard_prefetch_attempts,
            "budget_sec": hard_prefetch_budget_sec,
            "source": "PM_HARD_SOURCE_DIRECT_WORKER_PREFETCH_MARKET_TICKS/PM_HARD_SOURCE_DIRECT_PREFETCH_* or hard-source default",
        },
        "hard_source_market_prefetch": {
            "enabled": _env_truthy(value("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_ENABLED"), True),
            "heartbeat_sec": value("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_HEARTBEAT_SEC", "5"),
            "max_outcomes": value("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_MAX_OUTCOMES", "6"),
            "budget_sec": value("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_BUDGET_SEC", "1.5"),
            "clob_timeout_sec": value("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CLOB_TIMEOUT_SEC", "0.75"),
            "clob_retries": value("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CLOB_RETRIES", "1"),
            "workers": value("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_WORKERS", "4"),
            "cache_only": _env_truthy(value("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CACHE_ONLY"), True),
            "cache_bootstrap": _env_truthy(
                value("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CACHE_BOOTSTRAP"),
                True,
            ),
        },
        "metar_pws_multi_source_polling": {
            "enabled": _env_truthy(value("PM_HARD_SOURCE_METAR_PWS_ENABLED"), False),
            "iem_noaaport": {
                "poll_sec_override": value("PM_HARD_SOURCE_METAR_IEM_POLL_SEC"),
                "peak_poll_sec": value("PM_HARD_SOURCE_METAR_IEM_PEAK_POLL_SEC", "30"),
                "normal_poll_sec": value("PM_HARD_SOURCE_METAR_IEM_NORMAL_POLL_SEC", "300"),
            },
            "avwx": {
                "poll_sec_override": value("PM_HARD_SOURCE_METAR_AVWX_POLL_SEC"),
                "peak_poll_sec": value("PM_HARD_SOURCE_METAR_AVWX_PEAK_POLL_SEC", "15"),
                "normal_poll_sec": value("PM_HARD_SOURCE_METAR_AVWX_NORMAL_POLL_SEC", "60"),
            },
            "checkwx": {
                "poll_sec_override": value("PM_HARD_SOURCE_METAR_CHECKWX_POLL_SEC"),
                "peak_poll_sec": value("PM_HARD_SOURCE_METAR_CHECKWX_PEAK_POLL_SEC", "15"),
                "normal_poll_sec": value("PM_HARD_SOURCE_METAR_CHECKWX_NORMAL_POLL_SEC", "60"),
            },
            "twc_pws_rapid": {
                "poll_sec_override": value("PM_HARD_SOURCE_TWC_PWS_RAPID_POLL_SEC"),
                "peak_poll_sec": value("PM_HARD_SOURCE_TWC_PWS_RAPID_PEAK_POLL_SEC", "10"),
                "normal_poll_sec": value("PM_HARD_SOURCE_TWC_PWS_RAPID_NORMAL_POLL_SEC", "60"),
            },
        },
        "general_worker_prefetch": {
            "market_ticks_enabled": _env_truthy(value("PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS"), True),
            "max_attempts": value("PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS"),
            "budget_sec": value("PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC"),
        },
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _latest_fact_for_window(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> Optional[sqlite3.Row]:
    if not (_table_exists(conn, "event_fact_latest") and _table_exists(conn, "event_fact_history")):
        return None
    where: list[str] = []
    params: list[Any] = []
    if slug:
        where.append("l.slug = ?")
        params.append(slug)
    else:
        where.append("l.slug LIKE 'highest-temperature-in-chengdu-%'")
    if event_date:
        where.append("l.event_date = ?")
        params.append(event_date)
    return conn.execute(
        f"""
        SELECT l.slug, l.event_date, l.updated_at AS latest_updated_at,
               h.fact_json, h.fact_as_of, h.created_at AS fact_created_at
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE {' AND '.join(where)}
        ORDER BY l.updated_at DESC, h.id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def _current_decision_window(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    file_env: Mapping[str, Any],
) -> dict[str, Any]:
    fact = _latest_fact_for_window(conn, slug=slug, event_date=event_date)
    if fact is None:
        return {"available": False, "reason": "latest_fact_missing"}
    payload = _load_json(fact["fact_json"])
    if not isinstance(payload, Mapping):
        return {
            "available": False,
            "reason": "latest_fact_payload_invalid",
            "slug": fact["slug"],
            "event_date": fact["event_date"],
        }
    try:
        from scripts import hard_source_direct_poller as poller
    except Exception:
        try:
            import hard_source_direct_poller as poller  # type: ignore
        except Exception as exc:
            return {
                "available": False,
                "reason": "decision_window_import_failed",
                "error": f"{type(exc).__name__}:{exc}",
                "slug": fact["slug"],
                "event_date": fact["event_date"],
            }
    with _temporary_env_defaults(file_env):
        window = poller._decision_window_state(payload, event_date=str(fact["event_date"] or event_date))
    if not isinstance(window, dict):
        window = {}
    return {
        "available": True,
        "slug": fact["slug"],
        "event_date": fact["event_date"],
        "fact_as_of": fact["fact_as_of"],
        "fact_created_at": fact["fact_created_at"],
        "latest_updated_at": fact["latest_updated_at"],
        **window,
    }


def _latest_market_prefetch_runtime(log_path: Optional[Path] = None) -> dict[str, Any]:
    if log_path is None:
        log_path = MARKET_PREFETCH_LOG
    if not log_path.exists():
        return {"available": False, "reason": "market_prefetch_log_missing", "path": str(log_path)}
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {
            "available": False,
            "reason": "market_prefetch_log_unreadable",
            "path": str(log_path),
            "error": f"{type(exc).__name__}:{exc}",
        }
    latest: dict[str, Any] = {}
    for line in reversed(lines):
        parsed = _load_json(line.strip())
        if isinstance(parsed, dict):
            latest = parsed
            break
    if not latest:
        return {
            "available": False,
            "reason": "market_prefetch_log_json_missing",
            "path": str(log_path),
            "age_sec": _age_sec_from_mtime(log_path),
        }
    created_at = _parse_iso(latest.get("created_at"))
    created_age = None
    if created_at is not None:
        created_age = max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds())
    return {
        "available": True,
        "path": str(log_path),
        "age_sec": _age_sec_from_mtime(log_path),
        "created_age_sec": created_age,
        "ok": bool(latest.get("ok")),
        "status": latest.get("status"),
        "slug": latest.get("slug"),
        "event_date": latest.get("event_date"),
        "count": latest.get("count"),
        "created_at": latest.get("created_at"),
        "error": latest.get("error"),
    }


def _latest_market_prefetch_runtime_from_db(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> dict[str, Any]:
    if not _table_exists(conn, "event_market_ticks"):
        return {"available": False, "reason": "event_market_ticks_missing"}
    where: list[str] = []
    params: list[Any] = []
    if slug:
        where.append("slug = ?")
        params.append(slug)
    if event_date:
        where.append("event_date = ?")
        params.append(event_date)
    where_sql = " AND ".join(where) if where else "slug LIKE 'highest-temperature-in-chengdu-%'"
    row = conn.execute(
        f"""
        SELECT created_at
        FROM event_market_ticks
        WHERE {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None or not row["created_at"]:
        return {"available": False, "reason": "event_market_ticks_empty"}
    latest_created_at = str(row["created_at"])
    count_row = conn.execute(
        f"""
        SELECT COUNT(*) AS row_count,
               COUNT(DISTINCT outcome) AS outcome_count,
               COUNT(DISTINCT outcome || ':' || side) AS outcome_side_count
        FROM event_market_ticks
        WHERE {where_sql} AND created_at = ?
        """,
        [*params, latest_created_at],
    ).fetchone()
    created_at_dt = _parse_iso(latest_created_at)
    created_age = None
    if created_at_dt is not None:
        created_age = max(0.0, (datetime.now(timezone.utc) - created_at_dt).total_seconds())
    return {
        "available": True,
        "source": "event_market_ticks",
        "ok": True,
        "status": "market_prefetch_ticks_present",
        "slug": slug,
        "event_date": event_date,
        "count": int(count_row["row_count"] or 0) if count_row is not None else 0,
        "outcome_count": int(count_row["outcome_count"] or 0) if count_row is not None else 0,
        "outcome_side_count": int(count_row["outcome_side_count"] or 0) if count_row is not None else 0,
        "created_at": latest_created_at,
        "created_age_sec": created_age,
    }


def _latest_hard_source_job(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    since: str = "",
    require_ssot_semantics: bool = True,
) -> Optional[sqlite3.Row]:
    params: list[Any] = []
    where = [
        "source = 'hard_source_direct'",
    ]
    if since:
        where.append("created_at >= ?")
        params.append(str(since))
    if slug:
        where.append("payload_json LIKE ?")
        params.append(f"%{slug}%")
    if event_date:
        where.append("payload_json LIKE ?")
        params.append(f"%{event_date}%")
    rows = conn.execute(
        f"""
        SELECT *
        FROM auto_trade_jobs
        WHERE {' AND '.join(where)}
        ORDER BY CASE WHEN state = 'success' THEN 0 ELSE 1 END, id DESC
        LIMIT 50
        """,
        params,
    ).fetchall()
    for row in rows:
        if not require_ssot_semantics:
            return row
        payload = _load_json(row["payload_json"])
        if not isinstance(payload, Mapping):
            continue
        hard_block = payload.get("hard_source_direct")
        if isinstance(hard_block, Mapping) and hard_block.get("decision_semantics") == "ssot_posterior_constraint":
            return row
        for item in payload.get("event_payloads") or []:
            if isinstance(item, Mapping) and item.get("snapshot_semantics") == "ssot_posterior_constraint":
                return row
    return None


def _latest_posterior_target_job(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    since: str = "",
    require_semantics: bool = True,
) -> Optional[sqlite3.Row]:
    params: list[Any] = []
    where = ["source = 'posterior_target_direct'"]
    if since:
        where.append("created_at >= ?")
        params.append(str(since))
    if slug:
        where.append("payload_json LIKE ?")
        params.append(f"%{slug}%")
    if event_date:
        where.append("payload_json LIKE ?")
        params.append(f"%{event_date}%")
    rows = conn.execute(
        f"""
        SELECT *
        FROM auto_trade_jobs
        WHERE {' AND '.join(where)}
        ORDER BY CASE WHEN state = 'success' THEN 0 ELSE 1 END, id DESC
        LIMIT 50
        """,
        params,
    ).fetchall()
    for row in rows:
        if not require_semantics:
            return row
        payload = _load_json(row["payload_json"])
        if not isinstance(payload, Mapping):
            continue
        block = payload.get("posterior_target_direct")
        if (
            isinstance(block, Mapping)
            and block.get("decision_semantics") == "ssot_posterior_target_probability_change"
        ):
            return row
        for item in payload.get("event_payloads") or []:
            if isinstance(item, Mapping) and item.get("snapshot_semantics") == "ssot_posterior_target_probability_change":
                return row
    return None


def _legacy_job_detail(job: Optional[sqlite3.Row]) -> dict[str, Any]:
    if job is None:
        return {}
    payload = _load_json(job["payload_json"])
    hard_block = payload.get("hard_source_direct") if isinstance(payload, Mapping) else {}
    if not isinstance(hard_block, Mapping):
        hard_block = {}
    return {
        "id": job["id"],
        "state": job["state"],
        "source": job["source"],
        "run_id": job["run_id"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "ended_at": job["ended_at"],
        "decision_semantics": hard_block.get("decision_semantics"),
        "target_bucket": hard_block.get("target_bucket"),
    }


def _find_order_decision(
    conn: sqlite3.Connection,
    *,
    run_id: Optional[int],
    slug: str,
    event_date: str,
    outcome: str,
    action: str,
    dry_run: bool,
) -> Optional[sqlite3.Row]:
    reason_pattern = "%order_submitted%" if action == "entry" or dry_run else "%exit_marketable_immediate_confirmed%"
    run_id_filters = [run_id] if run_id else [None]
    if run_id:
        run_id_filters.append(None)
    for run_id_filter in run_id_filters:
        params: list[Any] = [outcome, action, reason_pattern]
        where = [
            "outcome = ?",
            "action = ?",
            "reason_codes_json LIKE ?",
        ]
        if run_id_filter:
            where.append("run_id = ?")
            params.append(int(run_id_filter))
        if slug:
            where.append("slug = ?")
            params.append(slug)
        if event_date:
            where.append("event_date = ?")
            params.append(event_date)
        row = conn.execute(
            f"""
            SELECT *
            FROM auto_trade_decisions
            WHERE {' AND '.join(where)}
            ORDER BY id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is not None:
            return row
    return None


def _latest_hard_source_fact(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT h.*
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()


def _details(row: Optional[sqlite3.Row]) -> dict[str, Any]:
    if row is None:
        return {}
    parsed = _load_json(row["details_json"])
    return parsed if isinstance(parsed, dict) else {}


def _has_sla(details: Mapping[str, Any]) -> bool:
    return bool(
        details.get("hard_source_direct")
        and details.get("hard_source_ssot_written_at")
        and details.get("hard_source_job_enqueued_at")
        and details.get("hard_source_worker_started_at")
        and details.get("hard_source_order_submitted_at")
        and details.get("hard_source_job_enqueue_to_order_ms") is not None
    )


def _has_posterior_target_sla(details: Mapping[str, Any]) -> bool:
    return bool(
        details.get("posterior_target_direct")
        and details.get("posterior_target_worker_started_at")
        and details.get("posterior_target_order_submitted_at")
        and details.get("posterior_target_job_enqueue_to_order_ms") is not None
    )


def _is_hard_source_override_applied(payload: Mapping[str, Any]) -> bool:
    override = payload.get("hard_source_posterior_override")
    if override is True:
        return True
    if isinstance(override, Mapping):
        return bool(override.get("applied") is True or override.get("status") == "applied")
    return bool(
        payload.get("hard_source_posterior_override_applied") is True
        or payload.get("hard_source_posterior_override_meta", {}).get("applied") is True
    )


def _zeroed_buckets(payload: Mapping[str, Any]) -> list[str]:
    rows = payload.get("rows") or payload.get("distribution") or []
    return [
        str(row.get("outcome") or row.get("label") or "").strip()
        for row in rows
        if isinstance(row, Mapping)
        and row.get("q_posterior_observed_constraints_hard_zero") is True
        and _to_float(row.get("q_bayes_posterior")) == 0.0
    ]


def _distribution_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = payload.get("rows") or payload.get("distribution") or []
    return [row for row in rows if isinstance(row, Mapping)]


def _row_label(row: Mapping[str, Any]) -> str:
    return str(row.get("outcome") or row.get("label") or "").strip()


def _row_posterior(row: Mapping[str, Any]) -> Optional[float]:
    return _first_float(
        row.get("q_bayes_posterior"),
        row.get("q_temperature_bucket_posterior"),
        row.get("q_entry"),
        row.get("q_round"),
    )


def _top_posterior_bucket(payload: Mapping[str, Any]) -> str:
    best_label = ""
    best_q: Optional[float] = None
    for row in _distribution_rows(payload):
        label = _row_label(row)
        q = _row_posterior(row)
        if not label or q is None:
            continue
        if best_q is None or q > best_q:
            best_label = label
            best_q = q
    return best_label


def _target_index(payload: Mapping[str, Any], target_bucket: str) -> Optional[int]:
    for idx, row in enumerate(_distribution_rows(payload)):
        label = _row_label(row)
        if label == target_bucket:
            return int(idx)
    return None


def _lower_bucket_zero_status(payload: Mapping[str, Any], target_bucket: str) -> dict[str, Any]:
    rows = _distribution_rows(payload)
    target_index = _target_index(payload, target_bucket)
    if target_index is None:
        return {
            "ok": False,
            "target_index": None,
            "required": [],
            "missing": [],
            "reason": "target_bucket_missing_from_distribution",
        }
    required_rows = rows[:target_index]
    missing = [
        _row_label(row)
        for row in required_rows
        if not (
            row.get("q_posterior_observed_constraints_hard_zero") is True
            and _to_float(row.get("q_bayes_posterior")) == 0.0
        )
    ]
    return {
        "ok": not missing,
        "target_index": target_index,
        "required": [_row_label(row) for row in required_rows],
        "missing": missing,
    }


def _final_lock_status(payload: Mapping[str, Any], target_bucket: str) -> dict[str, Any]:
    rows = _distribution_rows(payload)
    meta = payload.get("hard_source_posterior_override_meta")
    if not isinstance(meta, Mapping):
        meta = {}
    final_lock = bool(meta.get("final_bucket_lock"))
    target_q: Optional[float] = None
    non_target_nonzero: list[str] = []
    for row in rows:
        label = _row_label(row)
        q = _row_posterior(row)
        if label == target_bucket:
            target_q = q
            final_lock = bool(final_lock or row.get("hard_source_posterior_override_final_bucket_lock"))
            continue
        if q is not None and q > 1e-9:
            non_target_nonzero.append(label)
    if not final_lock:
        return {
            "ok": True,
            "final_bucket_lock": False,
            "target_q": target_q,
            "non_target_nonzero": non_target_nonzero,
        }
    return {
        "ok": bool(target_q is not None and target_q >= 0.99 and not non_target_nonzero),
        "final_bucket_lock": True,
        "target_q": target_q,
        "non_target_nonzero": non_target_nonzero,
    }


def _job_snapshot_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    for item in payload.get("event_payloads") or []:
        if not isinstance(item, Mapping):
            continue
        event_payload = item.get("payload")
        if isinstance(event_payload, Mapping):
            return dict(item), dict(event_payload)
    return {}, {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_bool_true(*values: Any) -> bool:
    for value in values:
        if isinstance(value, bool):
            if value is True:
                return True
            continue
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
    return False


def _posterior_target_rule_source_proof(
    *,
    target: Mapping[str, Any],
    snapshot_payload: Mapping[str, Any],
    target_bucket: str,
) -> dict[str, Any]:
    rows_raw = snapshot_payload.get("rows")
    if not isinstance(rows_raw, list):
        rows_raw = snapshot_payload.get("distribution")
    target_row: Mapping[str, Any] = {}
    for raw in rows_raw or []:
        if not isinstance(raw, Mapping):
            continue
        outcome = str(raw.get("outcome") or raw.get("label") or "").strip()
        if outcome == str(target_bucket or "").strip():
            target_row = raw
            break

    rules_ok = _first_bool_true(
        target.get("rules_contract_ok"),
        target.get("weather_strategy_rules_ok"),
        target_row.get("rules_contract_ok"),
        target_row.get("weather_strategy_rules_ok"),
        snapshot_payload.get("rules_contract_ok"),
        snapshot_payload.get("weather_strategy_rules_ok"),
    )
    source_usable = _first_bool_true(
        target.get("source_usable_for_settlement"),
        target.get("weather_strategy_source_usable_for_settlement"),
        target_row.get("source_usable_for_settlement"),
        target_row.get("weather_strategy_source_usable_for_settlement"),
        snapshot_payload.get("source_usable_for_settlement"),
        snapshot_payload.get("weather_strategy_source_usable_for_settlement"),
    )
    settlement_station = _first_text(
        target.get("settlement_station"),
        target.get("weather_strategy_settlement_station"),
        target.get("station_id"),
        target_row.get("settlement_station"),
        target_row.get("weather_strategy_settlement_station"),
        target_row.get("station_id"),
        snapshot_payload.get("settlement_station"),
        snapshot_payload.get("weather_strategy_settlement_station"),
        snapshot_payload.get("station_id"),
    )
    source_fit_tier = _first_text(
        target.get("source_fit_tier"),
        target.get("weather_strategy_source_fit_tier"),
        target_row.get("source_fit_tier"),
        target_row.get("weather_strategy_source_fit_tier"),
        snapshot_payload.get("source_fit_tier"),
        snapshot_payload.get("weather_strategy_source_fit_tier"),
    )
    resolution_source = _first_text(
        target.get("resolution_source"),
        target.get("weather_strategy_resolution_source"),
        target_row.get("resolution_source"),
        target_row.get("weather_strategy_resolution_source"),
        snapshot_payload.get("resolution_source"),
        snapshot_payload.get("weather_strategy_resolution_source"),
    )
    posterior_target_direct = _first_bool_true(
        target.get("posterior_target_direct"),
        target_row.get("posterior_target_direct"),
        snapshot_payload.get("posterior_target_direct_snapshot"),
    )
    compact_settlement_proof = bool(
        posterior_target_direct and target_row and settlement_station and resolution_source
    )
    rules_proven = bool(rules_ok or compact_settlement_proof)
    source_proven = bool(source_usable or compact_settlement_proof)
    return {
        "ok": bool(rules_proven and source_proven and settlement_station),
        "rules_contract_ok": bool(rules_proven),
        "source_usable_for_settlement": bool(source_proven),
        "settlement_station": settlement_station,
        "source_fit_tier": source_fit_tier,
        "resolution_source": resolution_source,
        "compact_settlement_proof": compact_settlement_proof,
        "target_row_found": bool(target_row),
    }


def _has_legacy_bypass(details: Mapping[str, Any]) -> bool:
    return "bypass_bayes" in details or "bypass_fact_recompute" in details


def _audit_posterior_target(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    since: str,
    details: dict[str, Any],
    checks: dict[str, bool],
    blockers: list[str],
    warnings: list[str],
    max_job_enqueue_to_worker_ms: Optional[float],
    max_job_enqueue_to_order_ms: Optional[float],
    max_fact_to_order_ms: Optional[float],
) -> dict[str, Any]:
    job = _latest_posterior_target_job(
        conn,
        slug=slug,
        event_date=event_date,
        since=since,
        require_semantics=True,
    )
    checks["posterior_target_job_present"] = job is not None
    if job is None:
        legacy = _latest_posterior_target_job(
            conn,
            slug=slug,
            event_date=event_date,
            since=since,
            require_semantics=False,
        )
        if legacy is not None:
            warnings.append("legacy_posterior_target_job_ignored")
            details["legacy_posterior_target_job_ignored"] = {
                "id": legacy["id"],
                "state": legacy["state"],
                "source": legacy["source"],
                "run_id": legacy["run_id"],
                "created_at": legacy["created_at"],
            }
        blockers.append("posterior_target_job_missing")
        return {"ok": False, "checks": checks, "blockers": blockers, "warnings": warnings, "details": details}
    payload = _load_json(job["payload_json"])
    if not isinstance(payload, dict):
        blockers.append("posterior_target_job_payload_invalid")
        return {"ok": False, "checks": checks, "blockers": blockers, "warnings": warnings, "details": details}
    block = payload.get("posterior_target_direct")
    target = (payload.get("entry_expected_targets") or [{}])[0]
    if not isinstance(target, Mapping):
        target = {}
    if not slug:
        event_keys = payload.get("include_event_keys") or []
        if event_keys and isinstance(event_keys[0], Mapping):
            slug = str(event_keys[0].get("slug") or "")
    if not event_date:
        event_keys = payload.get("include_event_keys") or []
        if event_keys and isinstance(event_keys[0], Mapping):
            event_date = str(event_keys[0].get("event_date") or "")
    target_bucket = str(
        target.get("outcome")
        or (block or {}).get("target_bucket")
        or ""
    ).strip()
    run_id = int(job["run_id"]) if job["run_id"] else None
    details.update(
        {
            "mode": "posterior_target",
            "job_id": job["id"],
            "job_state": job["state"],
            "run_id": run_id,
            "slug": slug,
            "event_date": event_date,
            "target_bucket": target_bucket,
            "job_created_at": job["created_at"],
        }
    )
    checks["job_semantics_ssot_posterior_target_probability_change"] = bool(
        isinstance(block, Mapping)
        and block.get("decision_semantics") == "ssot_posterior_target_probability_change"
    )
    checks["job_has_expected_target"] = bool(target_bucket and target.get("posterior_target_direct") is True)
    checks["job_has_posterior_timing"] = bool(
        isinstance(block, Mapping)
        and block.get("fact_as_of")
        and block.get("job_enqueued_at")
    )
    snapshot_item, snapshot_payload = _job_snapshot_payload(payload)
    snapshot_top_bucket = _top_posterior_bucket(snapshot_payload) if snapshot_payload else ""
    details["snapshot_top_posterior_bucket"] = snapshot_top_bucket
    checks["job_has_posterior_target_snapshot"] = bool(
        snapshot_payload
        and snapshot_item.get("source") == "posterior_target_direct"
        and snapshot_item.get("snapshot_semantics") == "ssot_posterior_target_probability_change"
        and snapshot_payload.get("posterior_target_direct_snapshot") is True
    )
    rule_source_proof = _posterior_target_rule_source_proof(
        target=target,
        snapshot_payload=snapshot_payload,
        target_bucket=target_bucket,
    )
    details["posterior_target_rule_source_proof"] = rule_source_proof
    checks["job_payload_has_settlement_rule_source_proof"] = bool(rule_source_proof.get("ok"))
    if not checks["job_semantics_ssot_posterior_target_probability_change"]:
        checks["job_semantics_ssot_posterior_target_probability_change"] = bool(
            snapshot_item.get("snapshot_semantics") == "ssot_posterior_target_probability_change"
        )
    checks["job_snapshot_target_is_top_posterior"] = bool(target_bucket and snapshot_top_bucket == target_bucket)

    entry_row = _find_order_decision(
        conn,
        run_id=run_id,
        slug=slug,
        event_date=event_date,
        outcome=target_bucket,
        action="entry",
        dry_run=True,
    )
    entry_details = _details(entry_row)
    checks["target_entry_order_decision_present"] = entry_row is not None
    checks["target_entry_has_posterior_target_sla"] = _has_posterior_target_sla(entry_details)
    details["entry_decision_id"] = entry_row["id"] if entry_row is not None else None

    old_exit_rows = []
    run_id_filters = [run_id] if run_id else [None]
    if run_id:
        run_id_filters.append(None)
    for run_id_filter in run_id_filters:
        old_exit_rows = conn.execute(
            """
            SELECT *
            FROM auto_trade_decisions
            WHERE action = 'exit'
              AND side = 'YES'
              AND (
                    reason_codes_json LIKE '%order_submitted%'
                    OR reason_codes_json LIKE '%exit_marketable_immediate_confirmed%'
                  )
              AND details_json LIKE '%posterior_target_direct%'
              AND (? = '' OR outcome != ?)
              AND (? IS NULL OR run_id = ?)
              AND (? = '' OR slug = ?)
              AND (? = '' OR event_date = ?)
            ORDER BY id DESC
            """,
            (target_bucket, target_bucket, run_id_filter, run_id_filter, slug, slug, event_date, event_date),
        ).fetchall()
        if old_exit_rows:
            break
    posterior_runtime = details.get("runtime_config", {}).get("posterior_target_direct", {})
    cleanup_only_required = _env_truthy(
        posterior_runtime.get("enqueue_cleanup_only") if isinstance(posterior_runtime, Mapping) else None,
        False,
    )
    details["old_bucket_exit_required"] = bool(cleanup_only_required)
    details["old_bucket_exit_policy"] = (
        "required" if cleanup_only_required else "deferred_by_cleanup_only_config"
    )
    checks["old_bucket_exit_order_decision_present"] = (
        bool(old_exit_rows) if cleanup_only_required else True
    )
    exit_details = _details(old_exit_rows[0]) if old_exit_rows else {}
    checks["old_bucket_exit_has_posterior_target_sla"] = (
        _has_posterior_target_sla(exit_details) if cleanup_only_required else True
    )
    details["exit_decision_id"] = old_exit_rows[0]["id"] if old_exit_rows else None
    details["exit_outcome"] = old_exit_rows[0]["outcome"] if old_exit_rows else ""
    checks["order_decisions_follow_posterior_target"] = bool(
        target_bucket
        and entry_details.get("target_bucket") == target_bucket
        and (not exit_details or exit_details.get("target_bucket") == target_bucket)
    )
    posterior_target_sources = {
        "posterior_target_bucket_selection",
        "top_level_ctp_ssot.target_bucket_probability_pct",
    }
    checks["order_decisions_keep_posterior_target_semantics"] = bool(
        entry_details.get("posterior_target_direct") is True
        and str(entry_details.get("posterior_target_source") or "") in posterior_target_sources
        and entry_details.get("target_adjustment_decision_version") == "posterior_target_direct_v1"
        and (
            not exit_details
            or (
                exit_details.get("posterior_target_direct") is True
                and str(exit_details.get("posterior_target_source") or "") in posterior_target_sources
            )
        )
    )
    checks["order_decisions_no_legacy_bypass_semantics"] = bool(
        not _has_legacy_bypass(entry_details)
        and (not exit_details or not _has_legacy_bypass(exit_details))
    )

    latencies = {
        "job_enqueue_to_worker_ms": _first_float(
            (block or {}).get("job_enqueue_to_worker_ms") if isinstance(block, Mapping) else None,
            target.get("posterior_target_job_enqueue_to_worker_ms"),
            entry_details.get("posterior_target_job_enqueue_to_worker_ms"),
            exit_details.get("posterior_target_job_enqueue_to_worker_ms"),
        ),
        "entry_fact_to_order_ms": _to_float(entry_details.get("posterior_target_fact_to_order_ms")),
        "entry_job_enqueue_to_order_ms": _to_float(entry_details.get("posterior_target_job_enqueue_to_order_ms")),
        "entry_worker_to_order_ms": _to_float(entry_details.get("posterior_target_worker_to_order_ms")),
        "exit_fact_to_order_ms": _to_float(exit_details.get("posterior_target_fact_to_order_ms")),
        "exit_job_enqueue_to_order_ms": _to_float(exit_details.get("posterior_target_job_enqueue_to_order_ms")),
        "exit_worker_to_order_ms": _to_float(exit_details.get("posterior_target_worker_to_order_ms")),
    }
    details["latencies_ms"] = latencies
    sla_specs = [
        ("job_enqueue_to_worker_within_sla", max_job_enqueue_to_worker_ms, ["job_enqueue_to_worker_ms"]),
        (
            "job_enqueue_to_order_within_sla",
            max_job_enqueue_to_order_ms,
            ["entry_job_enqueue_to_order_ms", "exit_job_enqueue_to_order_ms"],
        ),
        (
            "fact_to_order_within_sla",
            max_fact_to_order_ms,
            ["entry_fact_to_order_ms", "exit_fact_to_order_ms"],
        ),
    ]
    any_sla_requested = False
    for check_name, limit, fields in sla_specs:
        if limit is None:
            continue
        any_sla_requested = True
        observed = [latencies.get(field) for field in fields if latencies.get(field) is not None]
        checks[check_name] = bool(observed) and all(float(value) <= float(limit) for value in observed)
    if not any_sla_requested:
        checks["posterior_target_latency_recorded"] = any(v is not None for v in latencies.values())
    for name, passed in checks.items():
        if not passed:
            blockers.append(name)
    return {
        "ok": not blockers,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "details": details,
    }


def audit(
    db_path: Path | str = DEFAULT_DB,
    *,
    mode: str = "hard_source",
    slug: str = "",
    event_date: str = "",
    since: str = "",
    since_hours: Optional[float] = None,
    max_fetch_to_ssot_ms: Optional[float] = None,
    max_ssot_to_job_enqueue_ms: Optional[float] = None,
    max_job_enqueue_to_worker_ms: Optional[float] = None,
    max_ssot_to_order_ms: Optional[float] = None,
    max_job_enqueue_to_order_ms: Optional[float] = None,
    max_fetch_to_order_ms: Optional[float] = None,
    max_fact_to_order_ms: Optional[float] = None,
) -> dict[str, Any]:
    db = Path(db_path)
    since_text = str(since or "").strip()
    if not since_text and since_hours is not None:
        since_text = _utc_cutoff(float(since_hours))
    file_env = _parse_env_file()
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {
        "db": str(db),
        "since": since_text,
        "runtime_config": _selected_runtime_config(),
    }
    direct_entry_budget = _direct_entry_order_budget(file_env)
    runtime_config = details.get("runtime_config")
    if isinstance(runtime_config, dict):
        runtime_config["direct_entry_order_budget"] = direct_entry_budget
    checks["direct_entry_order_budget_3s_configured"] = _direct_entry_order_budget_ok(
        direct_entry_budget
    )
    blockers: list[str] = []
    warnings: list[str] = []
    with _connect_readonly(db) as conn:
        details["current_decision_window"] = _current_decision_window(
            conn,
            slug=slug,
            event_date=event_date,
            file_env=file_env,
        )
        db_prefetch_runtime = _latest_market_prefetch_runtime_from_db(
            conn,
            slug=slug,
            event_date=event_date,
        )
        details["market_prefetch_runtime"] = (
            db_prefetch_runtime
            if db_prefetch_runtime.get("available")
            else _latest_market_prefetch_runtime()
        )
        if str(mode or "").strip().lower() in {"posterior_target", "posterior_target_direct"}:
            return _audit_posterior_target(
                conn,
                slug=slug,
                event_date=event_date,
                since=since_text,
                details=details,
                checks=checks,
                blockers=blockers,
                warnings=warnings,
                max_job_enqueue_to_worker_ms=max_job_enqueue_to_worker_ms,
                max_job_enqueue_to_order_ms=max_job_enqueue_to_order_ms,
                max_fact_to_order_ms=max_fact_to_order_ms
                if max_fact_to_order_ms is not None
                else max_fetch_to_order_ms,
            )
        job = _latest_hard_source_job(
            conn,
            slug=slug,
            event_date=event_date,
            since=since_text,
            require_ssot_semantics=True,
        )
        checks["hard_source_job_present"] = job is not None
        if job is None:
            legacy = _latest_hard_source_job(
                conn,
                slug=slug,
                event_date=event_date,
                since=since_text,
                require_ssot_semantics=False,
            )
            legacy_detail = _legacy_job_detail(legacy)
            if legacy_detail:
                warnings.append("legacy_hard_source_job_ignored")
                details["legacy_hard_source_job_ignored"] = legacy_detail
            blockers.append("hard_source_ssot_job_missing")
            return {"ok": False, "checks": checks, "blockers": blockers, "warnings": warnings, "details": details}
        payload = _load_json(job["payload_json"])
        if not isinstance(payload, dict):
            blockers.append("hard_source_job_payload_invalid")
            return {"ok": False, "checks": checks, "blockers": blockers, "warnings": warnings, "details": details}
        hard_block = payload.get("hard_source_direct")
        target = (payload.get("entry_expected_targets") or [{}])[0]
        if not isinstance(target, Mapping):
            target = {}
        if not slug:
            event_keys = payload.get("include_event_keys") or []
            if event_keys and isinstance(event_keys[0], Mapping):
                slug = str(event_keys[0].get("slug") or "")
        if not event_date:
            event_keys = payload.get("include_event_keys") or []
            if event_keys and isinstance(event_keys[0], Mapping):
                event_date = str(event_keys[0].get("event_date") or "")
        target_bucket = str(target.get("outcome") or (hard_block or {}).get("target_bucket") or "").strip()
        run_id = int(job["run_id"]) if job["run_id"] else None
        details.update(
            {
                "job_id": job["id"],
                "job_state": job["state"],
                "run_id": run_id,
                "slug": slug,
                "event_date": event_date,
                "target_bucket": target_bucket,
                "job_created_at": job["created_at"],
            }
        )
        checks["job_semantics_ssot_posterior_constraint"] = isinstance(hard_block, Mapping) and hard_block.get("decision_semantics") == "ssot_posterior_constraint"
        checks["job_has_expected_target"] = bool(target_bucket and target.get("posterior_override") is True)
        checks["job_has_ssot_timing"] = bool((hard_block or {}).get("ssot_written_at") and (hard_block or {}).get("job_enqueued_at"))
        snapshot_item, snapshot_payload = _job_snapshot_payload(payload)
        snapshot_zeroed = _zeroed_buckets(snapshot_payload) if snapshot_payload else []
        snapshot_target_index = _target_index(snapshot_payload, target_bucket) if snapshot_payload else None
        snapshot_top_bucket = _top_posterior_bucket(snapshot_payload) if snapshot_payload else ""
        snapshot_lower_zero_status = (
            _lower_bucket_zero_status(snapshot_payload, target_bucket)
            if snapshot_payload
            else {"ok": False, "reason": "job_snapshot_missing"}
        )
        snapshot_final_lock_status = (
            _final_lock_status(snapshot_payload, target_bucket)
            if snapshot_payload
            else {"ok": False, "reason": "job_snapshot_missing"}
        )
        details["snapshot_zeroed_lower_buckets"] = snapshot_zeroed
        details["snapshot_lower_buckets_zero_required"] = bool(snapshot_target_index is None or snapshot_target_index > 0)
        details["snapshot_top_posterior_bucket"] = snapshot_top_bucket
        details["snapshot_lower_bucket_zero_status"] = snapshot_lower_zero_status
        details["snapshot_final_lock_status"] = snapshot_final_lock_status
        checks["job_has_constrained_snapshot"] = bool(
            snapshot_payload
            and snapshot_item.get("source") == "hard_source_direct"
            and snapshot_item.get("snapshot_semantics") == "ssot_posterior_constraint"
            and snapshot_payload.get("hard_source_direct_snapshot") is True
        )
        checks["job_snapshot_matches_ssot_target"] = bool(
            snapshot_payload
            and target_bucket
            and str(snapshot_payload.get("ssot_target_bucket") or "") == target_bucket
        )
        checks["job_snapshot_target_is_top_posterior"] = bool(target_bucket and snapshot_top_bucket == target_bucket)
        checks["job_snapshot_lower_buckets_zeroed"] = bool(snapshot_lower_zero_status.get("ok"))
        checks["job_snapshot_final_lock_strict_if_applicable"] = bool(snapshot_final_lock_status.get("ok"))

        fact = _latest_hard_source_fact(conn, slug=slug, event_date=event_date) if slug and event_date else None
        fact_payload = _load_json(fact["fact_json"]) if fact is not None else None
        checks["latest_fact_is_hard_source_ssot"] = bool(
            isinstance(fact_payload, dict)
            and fact_payload.get("hard_source_direct_ssot_write") is True
            and _is_hard_source_override_applied(fact_payload)
        )
        checks["latest_fact_matches_job_target"] = bool(
            isinstance(fact_payload, dict)
            and target_bucket
            and str(fact_payload.get("ssot_target_bucket") or "") == target_bucket
        )
        if isinstance(fact_payload, dict):
            zeroed = _zeroed_buckets(fact_payload)
            fact_target_index = _target_index(fact_payload, target_bucket)
            fact_top_bucket = _top_posterior_bucket(fact_payload)
            fact_lower_zero_status = _lower_bucket_zero_status(fact_payload, target_bucket)
            fact_final_lock_status = _final_lock_status(fact_payload, target_bucket)
            details["zeroed_lower_buckets"] = zeroed
            details["lower_buckets_zero_required"] = bool(fact_target_index is None or fact_target_index > 0)
            details["latest_fact_top_posterior_bucket"] = fact_top_bucket
            details["latest_fact_lower_bucket_zero_status"] = fact_lower_zero_status
            details["latest_fact_final_lock_status"] = fact_final_lock_status
            checks["latest_fact_target_is_top_posterior"] = bool(target_bucket and fact_top_bucket == target_bucket)
            checks["lower_buckets_zeroed"] = bool(fact_lower_zero_status.get("ok"))
            checks["latest_fact_final_lock_strict_if_applicable"] = bool(fact_final_lock_status.get("ok"))
        else:
            checks["latest_fact_target_is_top_posterior"] = False
            checks["lower_buckets_zeroed"] = False
            checks["latest_fact_final_lock_strict_if_applicable"] = False
        fact_timing = fact_payload.get("hard_source_direct_timing") if isinstance(fact_payload, dict) else {}
        if not isinstance(fact_timing, Mapping):
            fact_timing = {}

        entry_row = _find_order_decision(
            conn,
            run_id=run_id,
            slug=slug,
            event_date=event_date,
            outcome=target_bucket,
            action="entry",
            dry_run=True,
        )
        entry_details = _details(entry_row)
        checks["target_entry_order_decision_present"] = entry_row is not None
        checks["target_entry_has_hard_source_sla"] = _has_sla(entry_details)
        details["entry_decision_id"] = entry_row["id"] if entry_row is not None else None

        old_exit_rows = []
        run_id_filters = [run_id] if run_id else [None]
        if run_id:
            run_id_filters.append(None)
        for run_id_filter in run_id_filters:
            old_exit_rows = conn.execute(
                """
                SELECT *
                FROM auto_trade_decisions
                WHERE action = 'exit'
                  AND side = 'YES'
                  AND (
                        reason_codes_json LIKE '%order_submitted%'
                        OR reason_codes_json LIKE '%exit_marketable_immediate_confirmed%'
                      )
                  AND details_json LIKE '%hard_source_direct%'
                  AND (? = '' OR outcome != ?)
                  AND (? IS NULL OR run_id = ?)
                  AND (? = '' OR slug = ?)
                  AND (? = '' OR event_date = ?)
                ORDER BY id DESC
                """,
                (target_bucket, target_bucket, run_id_filter, run_id_filter, slug, slug, event_date, event_date),
            ).fetchall()
            if old_exit_rows:
                break
        checks["old_bucket_exit_order_decision_present"] = bool(old_exit_rows)
        exit_details = _details(old_exit_rows[0]) if old_exit_rows else {}
        checks["old_bucket_exit_has_hard_source_sla"] = _has_sla(exit_details)
        details["exit_decision_id"] = old_exit_rows[0]["id"] if old_exit_rows else None
        details["exit_outcome"] = old_exit_rows[0]["outcome"] if old_exit_rows else ""
        checks["order_decisions_follow_ssot_target"] = bool(
            target_bucket
            and entry_details.get("ssot_target_bucket") == target_bucket
            and (not exit_details or exit_details.get("ssot_target_bucket") == target_bucket)
        )
        checks["order_decisions_keep_ssot_semantics"] = bool(
            entry_details.get("decision_ssot") == "auto_trade_target_bucket"
            and entry_details.get("decision_basis") == "hard_source_lower_bound_constraint"
            and (not exit_details or exit_details.get("decision_ssot") == "auto_trade_target_bucket")
        )
        checks["order_decisions_mark_async_recompute"] = bool(
            entry_details.get("bayes_recomputed_async") is True
            and entry_details.get("fact_recompute_async") is True
            and (
                not exit_details
                or (
                    exit_details.get("bayes_recomputed_async") is True
                    and exit_details.get("fact_recompute_async") is True
                )
            )
        )
        checks["order_decisions_no_legacy_bypass_semantics"] = bool(
            not _has_legacy_bypass(entry_details)
            and (not exit_details or not _has_legacy_bypass(exit_details))
        )
        checks["hard_source_fast_path_not_waiting_full_fact_bayes"] = checks["order_decisions_mark_async_recompute"]

        latencies = {
            "fetch_to_ssot_ms": _first_float(
                fact_timing.get("fetch_to_ssot_ms"),
                (hard_block or {}).get("fetch_to_ssot_ms") if isinstance(hard_block, Mapping) else None,
                target.get("hard_source_fetch_to_ssot_ms"),
                entry_details.get("hard_source_fetch_to_ssot_ms"),
            ),
            "ssot_to_job_enqueue_ms": _first_float(
                (hard_block or {}).get("ssot_to_job_enqueue_ms") if isinstance(hard_block, Mapping) else None,
                target.get("hard_source_ssot_to_job_enqueue_ms"),
                entry_details.get("hard_source_ssot_to_job_enqueue_ms"),
            ),
            "job_enqueue_to_worker_ms": _first_float(
                (hard_block or {}).get("job_enqueue_to_worker_ms") if isinstance(hard_block, Mapping) else None,
                target.get("hard_source_job_enqueue_to_worker_ms"),
                entry_details.get("hard_source_job_enqueue_to_worker_ms"),
                exit_details.get("hard_source_job_enqueue_to_worker_ms"),
            ),
            "entry_ssot_to_order_ms": _to_float(entry_details.get("hard_source_ssot_to_order_ms")),
            "entry_job_enqueue_to_order_ms": _to_float(entry_details.get("hard_source_job_enqueue_to_order_ms")),
            "entry_fetch_to_order_ms": _to_float(entry_details.get("hard_source_fetch_to_order_ms")),
            "entry_worker_to_order_ms": _to_float(entry_details.get("hard_source_worker_to_order_ms")),
            "exit_ssot_to_order_ms": _to_float(exit_details.get("hard_source_ssot_to_order_ms")),
            "exit_job_enqueue_to_order_ms": _to_float(exit_details.get("hard_source_job_enqueue_to_order_ms")),
            "exit_fetch_to_order_ms": _to_float(exit_details.get("hard_source_fetch_to_order_ms")),
            "exit_worker_to_order_ms": _to_float(exit_details.get("hard_source_worker_to_order_ms")),
        }
        details["latencies_ms"] = latencies
        sla_specs = [
            ("fetch_to_ssot_within_sla", max_fetch_to_ssot_ms, ["fetch_to_ssot_ms"]),
            ("ssot_to_job_enqueue_within_sla", max_ssot_to_job_enqueue_ms, ["ssot_to_job_enqueue_ms"]),
            ("job_enqueue_to_worker_within_sla", max_job_enqueue_to_worker_ms, ["job_enqueue_to_worker_ms"]),
            (
                "ssot_to_order_within_sla",
                max_ssot_to_order_ms,
                ["entry_ssot_to_order_ms", "exit_ssot_to_order_ms"],
            ),
            (
                "job_enqueue_to_order_within_sla",
                max_job_enqueue_to_order_ms,
                ["entry_job_enqueue_to_order_ms", "exit_job_enqueue_to_order_ms"],
            ),
            (
                "fetch_to_order_within_sla",
                max_fetch_to_order_ms,
                ["entry_fetch_to_order_ms", "exit_fetch_to_order_ms"],
            ),
        ]
        any_sla_requested = False
        for check_name, limit, fields in sla_specs:
            if limit is None:
                continue
            any_sla_requested = True
            observed = [latencies.get(field) for field in fields if latencies.get(field) is not None]
            checks[check_name] = bool(observed) and all(float(value) <= float(limit) for value in observed)
        if max_ssot_to_order_ms is not None:
            pass
        elif any_sla_requested:
            checks["hard_source_segment_latency_recorded"] = all(
                latencies.get(field) is not None
                for field in ("fetch_to_ssot_ms", "ssot_to_job_enqueue_ms", "job_enqueue_to_worker_ms")
            )
        else:
            checks["ssot_to_order_latency_recorded"] = any(v is not None for v in latencies.values())

    for name, passed in checks.items():
        if not passed:
            blockers.append(name)
    return {
        "ok": not blockers,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "details": details,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only hard-source direct SSOT acceptance audit.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--mode",
        choices=["hard_source", "posterior_target", "posterior_target_direct"],
        default="hard_source",
    )
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--since-hours", type=float, default=None)
    parser.add_argument("--max-fetch-to-ssot-ms", type=float, default=None)
    parser.add_argument("--max-ssot-to-job-enqueue-ms", type=float, default=None)
    parser.add_argument("--max-job-enqueue-to-worker-ms", type=float, default=None)
    parser.add_argument("--max-ssot-to-order-ms", type=float, default=None)
    parser.add_argument("--max-job-enqueue-to-order-ms", type=float, default=None)
    parser.add_argument("--max-fetch-to-order-ms", type=float, default=None)
    parser.add_argument("--max-fact-to-order-ms", type=float, default=None)
    args = parser.parse_args(argv)
    result = audit(
        args.db,
        mode=args.mode,
        slug=args.slug,
        event_date=args.event_date,
        since=args.since,
        since_hours=args.since_hours,
        max_fetch_to_ssot_ms=args.max_fetch_to_ssot_ms,
        max_ssot_to_job_enqueue_ms=args.max_ssot_to_job_enqueue_ms,
        max_job_enqueue_to_worker_ms=args.max_job_enqueue_to_worker_ms,
        max_ssot_to_order_ms=args.max_ssot_to_order_ms,
        max_job_enqueue_to_order_ms=args.max_job_enqueue_to_order_ms,
        max_fetch_to_order_ms=args.max_fetch_to_order_ms,
        max_fact_to_order_ms=args.max_fact_to_order_ms,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
