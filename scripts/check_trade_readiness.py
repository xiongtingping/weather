#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import argparse
import math
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
ORDER_SCRIPT = ROOT / "scripts" / "polymarket_order.mjs"
ENV_HELPER = ROOT / "scripts" / "lib" / "load_launchctl_env.sh"
DEFAULT_CUTOVER_PROBE_EVENT_URL = (
    "https://polymarket.com/event/highest-temperature-in-chengdu-on-june-1-2026"
)
DEFAULT_CUTOVER_PROBE_DATE = "2026-06-01"
DEFAULT_CUTOVER_PROBE_LAT = "30.5728"
DEFAULT_CUTOVER_PROBE_LON = "104.0668"
DEFAULT_CUTOVER_PROBE_TZ = "Asia/Shanghai"
READINESS_ENV_KEYS = (
    "PM_AUTO_TRADE_ENABLED",
    "PM_AUTO_TRADE_DRY_RUN",
    "PM_AUTO_TRADE_READINESS_MIN_BALANCE_USDC",
    "PM_AUTO_TRADE_ENTRY_BUDGET_MIN_USDC",
    "PM_KELLY_MIN_NOTIONAL_USDC",
    "PM_ORDER_USDC",
    "PM_MIN_ORDER_SIZE",
    "PM_EXCHANGE_MIN_ORDER_SIZE",
    "PM_ENFORCE_EXCHANGE_MIN_ORDER_SIZE",
    "PM_TARGET_REALIZATION_V3_ENABLED",
    "PM_TARGET_REALIZATION_V3_ACTIVATION_DATE_ET",
    "PM_CLOB_FORCE_DIRECT",
    "PM_CLOB_FORCE_PROXY",
    "PM_PROXY_PORTS",
)
READINESS_ENV_OVERRIDE_KEYS = (
    "PM_AUTO_TRADE_ENABLED",
    "PM_AUTO_TRADE_DRY_RUN",
    "PM_CLOB_FORCE_DIRECT",
    "PM_CLOB_FORCE_PROXY",
    "PM_PROXY_PORTS",
    # Read-only cutover probes may use a wider timeout than the 3/5 second
    # signal-to-order execution budget.  Keep these command-scoped so the
    # live place-order path retains its strict latency contract.
    "PM_CLOB_BALANCE_HARD_TIMEOUT_MS",
    "PM_CLOB_FETCH_OPEN_ORDERS_HARD_TIMEOUT_MS",
    "PM_CLOB_COMMAND_HARD_TIMEOUT_MS",
    "PM_CLOB_TIMEOUT_MS",
)


def _shell_export_overrides() -> str:
    lines: list[str] = []
    for key in READINESS_ENV_OVERRIDE_KEYS:
        if key in os.environ:
            lines.append(f"export {key}={shlex.quote(str(os.environ[key]))}")
    return "\n".join(lines)
ENTRY_ALLOWED_HARD_REASON_CODES = {
    "live_disabled",
    "dry_run",
    "balance_insufficient",
    "token_missing",
    "contract_rule_missing",
    "station_mapping_missing",
    "orderbook_missing",
    "duplicate_open_order",
    "event_exposure_cap",
    "bucket_exposure_cap",
    "residual_exit_lock",
    "target_switch_non_target_entry_suppressed",
    "post_peak_near_settlement_price_blocked",
    "temperature_path_trade_follow_not_allowed",
    "target_realization_plan_notional_nonpositive",
    "clob_submit_failed",
    "manual_ignore",
    "manual_ignore_same_outcome",
    # A missed execution deadline is a valid fail-closed entry rejection,
    # not an unknown policy reason or authorization to place a late order.
    "sequential_bayes_signal_to_order_hard_deadline_exceeded",
}


def _run_bash(command: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    prefix = f"""
set -euo pipefail
cd {str(ROOT)!r}
. {str(ENV_HELPER)!r}
load_repo_env_file .env
load_polymarket_launchctl_env
{_shell_export_overrides()}
"""
    return subprocess.run(
        ["bash", "-lc", prefix + "\n" + command],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _json_command(command: str, timeout: int = 60) -> tuple[bool, dict[str, Any]]:
    proc = _run_bash(f"node {str(ORDER_SCRIPT)!r} {command}", timeout=timeout)
    if proc.returncode != 0:
        return False, {
            "returncode": proc.returncode,
            "stderr": _redact(proc.stderr.strip())[-600:],
            "stdout": _redact(proc.stdout.strip())[-600:],
        }
    try:
        payload = json.loads(proc.stdout.strip() or "{}")
    except Exception as exc:
        return False, {
            "error": f"json_parse_failed:{exc}",
            "stdout": _redact(proc.stdout.strip())[-600:],
        }
    return bool(payload.get("ok", True)), payload


def _hydrate_runtime_env() -> None:
    proc = _run_bash("env", timeout=30)
    if proc.returncode != 0:
        return
    wanted = set(READINESS_ENV_KEYS)
    for raw in proc.stdout.splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        if key in wanted:
            os.environ[key] = value


def _redact(text: str) -> str:
    if not text:
        return text
    out = str(text)
    out = re.sub(r"0x[0-9a-fA-F]{64}", "0x***", out)
    out = re.sub(r"(?i)(POLY_[A-Z_]*|PRIVATE_KEY|PASSPHRASE|SECRET|SIGNATURE)(=|\"\\s*:\\s*\")([^\\s\",]+)", r"\1\2***", out)
    return out


def _amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        vals = [_amount(item) for item in value.values()]
        vals = [item for item in vals if item is not None]
        return max(vals) if vals else None
    if isinstance(value, (list, tuple, set)):
        vals = [_amount(item) for item in value]
        vals = [item for item in vals if item is not None]
        return max(vals) if vals else None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = float(raw)
        except Exception:
            return None
        if re.fullmatch(r"-?\d+", raw):
            return parsed / 1_000_000.0
        return parsed
    return None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(str(raw).strip())
    except Exception:
        return None
    if value != value:
        return None
    return value


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _safe_json_dict(raw: Any) -> dict[str, Any]:
    parsed = _safe_json(raw)
    return parsed if isinstance(parsed, dict) else {}


def _reason_codes(raw: Any) -> list[str]:
    parsed = _safe_json(raw)
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        code = str(item or "").strip()
        if code:
            out.append(code)
    return out


def _is_legacy_posterior_target_cleanup_entry(row: sqlite3.Row, codes: list[str]) -> bool:
    code_set = {str(code or "").strip() for code in codes if str(code or "").strip()}
    if not code_set or not code_set.issubset(
        {"posterior_target_direct", "posterior_target_old_exit_cleanup"}
    ):
        return False
    if "posterior_target_old_exit_cleanup" not in code_set:
        return False
    details = _safe_json_dict(row["details_json"])
    return (
        details.get("old_exit_cleanup_triggered") is True
        and details.get("entry_order_update_triggered") is False
    )


def _is_queued_posterior_target_order_intent(
    details: dict[str, Any],
    codes: set[str],
) -> bool:
    if not isinstance(details, dict):
        return False
    if "target_realization_order_intent" not in codes:
        return False
    if "order_submitted" in codes or "place_order_failed" in codes:
        return False
    if str(details.get("order_id") or "").strip():
        return False
    if str(details.get("posterior_target_order_submitted_at") or "").strip():
        return False
    return bool(details.get("posterior_target_direct") or details.get("posterior_target_update"))


def _is_pre_submit_failed_live_intent(
    decided_state: str,
    details: dict[str, Any],
    codes: set[str],
) -> bool:
    if "order_submitted" in codes:
        return False
    if str(details.get("order_id") or "").strip():
        return False
    state = str(decided_state or "").strip().lower()
    status = str(details.get("status") or "").strip().lower()
    if "place_order_failed" in codes or "clob_submit_failed" in codes:
        return True
    if state in {"entry_failed", "order_failed", "place_order_failed"} or state.startswith("error_"):
        return True
    return status in {"place_order_failed", "order_failed", "submit_failed", "error_timeout"}


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (str(table_name),),
    ).fetchone()
    return row is not None


def _configure_readonly_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA query_only=ON")


def _db_read_error_payload(
    db_path: Path,
    exc: sqlite3.Error,
    *,
    since_iso: str,
    live_cutover: bool,
    live_trading: bool,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "ok": False,
        "db": str(db_path),
        "read_only": True,
        "since": since_iso,
        "live_cutover": bool(live_cutover),
        "live_trading": bool(live_trading),
        "dry_run": bool(dry_run),
        "blockers": ["archive_db_read_error"],
        "warnings": [],
        "error": f"{type(exc).__name__}:{exc}",
    }


def _entry_min_order_size() -> float:
    min_size = _env_float("PM_MIN_ORDER_SIZE")
    if min_size is None or not math.isfinite(min_size) or min_size < 0:
        min_size = 0.0
    if _env_bool("PM_ENFORCE_EXCHANGE_MIN_ORDER_SIZE", True):
        exchange_min = _env_float("PM_EXCHANGE_MIN_ORDER_SIZE")
        if exchange_min is None or not math.isfinite(exchange_min) or exchange_min <= 0:
            exchange_min = 5.0
        min_size = max(float(min_size), float(exchange_min))
    return max(0.0, float(min_size))


def _live_trading_enabled() -> bool:
    return _env_bool("PM_AUTO_TRADE_ENABLED", False) and not _env_bool("PM_AUTO_TRADE_DRY_RUN", True)


def _readiness_min_balance_usdc() -> float:
    explicit = _env_float("PM_AUTO_TRADE_READINESS_MIN_BALANCE_USDC")
    if explicit is not None and explicit >= 0:
        return float(explicit)
    candidates = [1.0]
    for name in (
        "PM_AUTO_TRADE_ENTRY_BUDGET_MIN_USDC",
        "PM_KELLY_MIN_NOTIONAL_USDC",
        "PM_ORDER_USDC",
    ):
        value = _env_float(name)
        if value is not None and value > 0:
            candidates.append(float(value))
    return max(candidates)


def _dry_run_probe_command() -> str:
    event_url = os.environ.get("PM_LIVE_CUTOVER_PROBE_EVENT_URL") or DEFAULT_CUTOVER_PROBE_EVENT_URL
    event_date = os.environ.get("PM_LIVE_CUTOVER_PROBE_DATE") or DEFAULT_CUTOVER_PROBE_DATE
    lat = os.environ.get("PM_LIVE_CUTOVER_PROBE_LAT") or DEFAULT_CUTOVER_PROBE_LAT
    lon = os.environ.get("PM_LIVE_CUTOVER_PROBE_LON") or DEFAULT_CUTOVER_PROBE_LON
    tz = os.environ.get("PM_LIVE_CUTOVER_PROBE_TZ") or DEFAULT_CUTOVER_PROBE_TZ
    parts = [
        "PM_AUTO_TRADE_DRY_RUN=1",
        "PM_AUTO_TRADE_ENABLED=0",
        f"{shlex.quote(str(ROOT / '.venv' / 'bin' / 'python'))}",
        "scan_weather_market.py",
        "--event-url",
        shlex.quote(str(event_url)),
        "--date",
        shlex.quote(str(event_date)),
        "--lat",
        shlex.quote(str(lat)),
        "--lon",
        shlex.quote(str(lon)),
        "--tz",
        shlex.quote(str(tz)),
        "--archive",
        "--auto-trade",
        "--model-cache-only",
        "--no-orderbook-fetch",
        "--once",
    ]
    return " ".join(parts)


def _identity() -> dict[str, Any]:
    js = """
const { Wallet } = require("ethers");
const signer = new Wallet(process.env.PRIVATE_KEY || process.env.POLY_PRIVATE_KEY);
const funder = process.env.PM_FUNDER_ADDRESS || process.env.POLY_FUNDER_ADDRESS || signer.address;
const dataUser = process.env.POLY_DATA_API_USER || funder;
console.log(JSON.stringify({
  signer: signer.address,
  funder,
  dataUser,
  sameSignerFunder: signer.address.toLowerCase() === String(funder).toLowerCase(),
  signatureType: process.env.PM_SIGNATURE_TYPE || process.env.SIGNATURE_TYPE || "0",
  clobHost: process.env.PM_CLOB_HOST || "https://clob.polymarket.com",
  hasApiCreds: Boolean(process.env.POLY_API_KEY && process.env.POLY_API_SECRET && process.env.POLY_API_PASSPHRASE),
  hasPrivateKey: Boolean(process.env.PRIVATE_KEY || process.env.POLY_PRIVATE_KEY)
}));
"""
    proc = _run_bash(
        "node -e " + shlex.quote(js),
        timeout=30,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": _redact(proc.stderr.strip() or proc.stdout.strip())[-600:]}
    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        return {"ok": False, "error": f"identity_parse_failed:{exc}"}
    return {"ok": True, **data}


def _example(
    row: sqlite3.Row,
    *,
    problem: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "problem": problem,
        "id": row["id"] if "id" in row.keys() else None,
        "created_at": row["created_at"] if "created_at" in row.keys() else "",
        "slug": row["slug"] if "slug" in row.keys() else "",
        "event_date": row["event_date"] if "event_date" in row.keys() else "",
        "outcome": row["outcome"] if "outcome" in row.keys() else "",
        "side": row["side"] if "side" in row.keys() else "",
    }
    if details:
        out.update(details)
    return out


def _add_problem(
    store: dict[str, list[dict[str, Any]]],
    code: str,
    row: sqlite3.Row,
    *,
    details: dict[str, Any] | None = None,
    max_examples: int = 5,
) -> None:
    bucket = store.setdefault(code, [])
    if len(bucket) < max_examples:
        bucket.append(_example(row, problem=code, details=details))


def _audit_recent_entry_intents(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
    limit: int,
    dry_run: bool,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, created_at, slug, event_date, outcome, side,
               decided_state, will_order, reason_codes_json, details_json
        FROM auto_trade_decisions
        WHERE action = 'entry'
          AND datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ?
        """,
        (since_iso, int(limit)),
    ).fetchall()
    min_size = _entry_min_order_size()
    problems: dict[str, list[dict[str, Any]]] = {}
    warnings: dict[str, list[dict[str, Any]]] = {}
    will_order_rows = 0
    queued_order_intent_rows = 0
    dry_run_intents = 0
    live_intents = 0
    complete_intents = 0
    failed_pre_submit_intent_rows = 0
    seen_intent_keys: set[tuple[str, str, str, str]] = set()
    for row in rows:
        will_order = bool(row["will_order"])
        if not will_order:
            continue
        details = _safe_json_dict(row["details_json"])
        codes = set(_reason_codes(row["reason_codes_json"]))
        if _is_queued_posterior_target_order_intent(details, codes):
            queued_order_intent_rows += 1
            continue
        decided_state = str(row["decided_state"] or "").strip()
        if not dry_run and _is_pre_submit_failed_live_intent(decided_state, details, codes):
            failed_pre_submit_intent_rows += 1
            _add_problem(
                warnings,
                "live_intent_pre_submit_failed",
                row,
                details={"decided_state": decided_state, "reason_codes": sorted(codes)},
            )
            continue
        intent_key = (
            str(row["slug"] or "").strip(),
            str(row["event_date"] or "").strip(),
            str(row["outcome"] or "").strip(),
            str(row["side"] or "").strip().upper(),
        )
        if intent_key in seen_intent_keys:
            continue
        seen_intent_keys.add(intent_key)
        will_order_rows += 1
        detail_dry_run = bool(details.get("dry_run"))
        status = str(details.get("status") or "").strip().lower()
        order_id = str(details.get("order_id") or "").strip()
        token_id = str(details.get("token_id") or "").strip()
        side = str(row["side"] or "").strip().upper()
        price = _to_float(
            details.get("order_price")
            if details.get("order_price") is not None
            else details.get("price")
            if details.get("price") is not None
            else details.get("exec_entry_price")
        )
        size = _to_float(
            details.get("order_size")
            if details.get("order_size") is not None
            else details.get("size")
        )
        order_type = str(details.get("order_type") or "").strip().upper()
        usable_edge = _to_float(details.get("entry_usable_edge"))
        edge_tradeability_pass = details.get("edge_tradeability_pass")
        edge_tradeability_override_keys = [
            "edge_tradeability_signal_soft_band_override",
            "edge_tradeability_signal_near_zero_override",
            "edge_tradeability_signal_near_zero_probe_pass",
            "edge_tradeability_lcb_canary_override",
            "edge_tradeability_consensus_yes_top_override",
            "edge_tradeability_near_miss_override",
            "edge_tradeability_target_realization_override",
        ]
        edge_tradeability_override = next(
            (key for key in edge_tradeability_override_keys if details.get(key) is True),
            "",
        )
        source_fit = str(details.get("source_fit_tier") or "").strip()
        source_fit_na = str(details.get("source_fit_na_reason") or details.get("source_na_reason") or "").strip()
        row_ok = True
        if "order_submitted" not in codes:
            _add_problem(problems, "intent_missing_order_submitted_code", row)
            row_ok = False
        if side not in {"YES", "NO"}:
            _add_problem(problems, "intent_side_invalid", row, details={"side": side})
            row_ok = False
        if not token_id:
            _add_problem(problems, "intent_missing_token_id", row)
            row_ok = False
        if price is None or price <= 0 or price >= 1:
            _add_problem(problems, "intent_order_price_invalid", row, details={"order_price": price})
            row_ok = False
        if size is None or size <= 0:
            _add_problem(problems, "intent_order_size_invalid", row, details={"order_size": size})
            row_ok = False
        elif min_size > 0 and size + 1e-9 < min_size:
            _add_problem(
                problems,
                "intent_order_size_below_exchange_min",
                row,
                details={"order_size": size, "min_order_size": min_size},
            )
            row_ok = False
        if order_type not in {"GTC", "GTD", "FAK", "FOK"}:
            _add_problem(problems, "intent_order_type_invalid", row, details={"order_type": order_type})
            row_ok = False
        if usable_edge is None and not str(details.get("entry_usable_edge_na_reason") or "").strip():
            _add_problem(problems, "intent_missing_entry_usable_edge", row)
            row_ok = False
        if edge_tradeability_pass is not True:
            if edge_tradeability_override and details.get("edge_tradeability_blocked") is not True:
                _add_problem(
                    warnings,
                    "intent_tradeability_override_used",
                    row,
                    details={"override": edge_tradeability_override},
                )
            else:
                _add_problem(
                    problems,
                    "intent_edge_tradeability_not_pass",
                    row,
                    details={
                        "edge_tradeability_pass": edge_tradeability_pass,
                        "edge_tradeability_blocked": details.get("edge_tradeability_blocked"),
                        "override": edge_tradeability_override,
                    },
                )
                row_ok = False
        if not source_fit and not source_fit_na:
            _add_problem(warnings, "intent_missing_source_fit_or_na_reason", row)
        for key, code in (
            ("market_closed", "intent_market_closed"),
            ("market_archived", "intent_market_archived"),
        ):
            if details.get(key) is True:
                _add_problem(problems, code, row)
                row_ok = False
        for key, code in (
            ("market_active", "intent_market_not_active"),
            ("market_accepting_orders", "intent_market_not_accepting_orders"),
            ("market_enable_orderbook", "intent_orderbook_not_enabled"),
        ):
            if details.get(key) is False:
                _add_problem(problems, code, row)
                row_ok = False

        if decided_state == "entry_dry_run" or detail_dry_run or status == "dry_run":
            dry_run_intents += 1
        else:
            live_intents += 1

        if dry_run:
            if decided_state != "entry_dry_run":
                _add_problem(
                    problems,
                    "dry_run_intent_state_not_entry_dry_run",
                    row,
                    details={"decided_state": decided_state},
                )
                row_ok = False
            if detail_dry_run is not True or status != "dry_run":
                _add_problem(
                    problems,
                    "dry_run_intent_missing_dry_run_marker",
                    row,
                    details={"dry_run": detail_dry_run, "status": status},
                )
                row_ok = False
        else:
            if detail_dry_run or decided_state == "entry_dry_run":
                _add_problem(problems, "live_intent_still_marked_dry_run", row)
                row_ok = False
            if not order_id:
                _add_problem(problems, "live_intent_missing_order_id", row)
                row_ok = False

        if row_ok:
            complete_intents += 1

    return {
        "ok": not problems,
        "since": since_iso,
        "rows_scanned": len(rows),
        "will_order_rows": will_order_rows,
        "queued_order_intent_rows": queued_order_intent_rows,
        "failed_pre_submit_intent_rows": failed_pre_submit_intent_rows,
        "dry_run_intents": dry_run_intents,
        "live_intents": live_intents,
        "complete_intents": complete_intents,
        "unique_intent_keys": len(seen_intent_keys),
        "min_order_size": min_size,
        "problems": problems,
        "warnings": warnings,
    }


def _audit_order_persistence(conn: sqlite3.Connection, *, since_iso: str, limit: int) -> dict[str, Any]:
    problems: dict[str, list[dict[str, Any]]] = {}
    warnings: dict[str, list[dict[str, Any]]] = {}
    live_rows = conn.execute(
        """
        SELECT id, created_at, slug, event_date, outcome, side,
               decided_state, will_order, reason_codes_json, details_json
        FROM auto_trade_decisions
        WHERE action = 'entry'
          AND will_order = 1
          AND datetime(created_at) >= datetime(?)
          AND lower(COALESCE(decided_state, '')) != 'entry_dry_run'
          AND COALESCE(json_extract(details_json, '$.dry_run'), 0) = 0
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ?
        """,
        (since_iso, int(limit)),
    ).fetchall()
    checked_live_rows = 0
    for row in live_rows:
        details = _safe_json_dict(row["details_json"])
        codes = set(_reason_codes(row["reason_codes_json"]))
        if _is_queued_posterior_target_order_intent(details, codes):
            continue
        if _is_pre_submit_failed_live_intent(str(row["decided_state"] or ""), details, codes):
            continue
        checked_live_rows += 1
        order_id = str(details.get("order_id") or "").strip()
        if not order_id:
            _add_problem(problems, "live_entry_missing_order_id", row)
            continue
        found = conn.execute("SELECT 1 FROM auto_orders WHERE order_id = ? LIMIT 1", (order_id,)).fetchone()
        if found is None:
            _add_problem(problems, "live_entry_order_not_recorded", row, details={"order_id": order_id})

    auto_count_row = conn.execute(
        """
        SELECT COUNT(1),
               MAX(created_at),
               MAX(NULLIF(TRIM(order_checked_at), '')),
               SUM(CASE WHEN order_checked_at IS NULL OR TRIM(order_checked_at) = '' THEN 1 ELSE 0 END)
        FROM auto_orders
        """
    ).fetchone()
    auto_count = int(auto_count_row[0] or 0) if auto_count_row else 0
    last_order_at = str(auto_count_row[1] or "") if auto_count_row else ""
    last_checked_at = str(auto_count_row[2] or "") if auto_count_row else ""
    auto_without_checked_at = int(auto_count_row[3] or 0) if auto_count_row else 0
    recent_count_row = conn.execute(
        """
        SELECT COUNT(1),
               MAX(created_at),
               MAX(NULLIF(TRIM(order_checked_at), '')),
               SUM(CASE WHEN order_checked_at IS NULL OR TRIM(order_checked_at) = '' THEN 1 ELSE 0 END)
        FROM auto_orders
        WHERE datetime(created_at) >= datetime(?)
        """,
        (since_iso,),
    ).fetchone()
    recent_auto_orders = int(recent_count_row[0] or 0) if recent_count_row else 0
    recent_last_order_at = str(recent_count_row[1] or "") if recent_count_row else ""
    recent_last_checked_at = str(recent_count_row[2] or "") if recent_count_row else ""
    recent_without_checked_at = int(recent_count_row[3] or 0) if recent_count_row else 0
    legacy_without_checked_at_row = conn.execute(
        """
        SELECT COUNT(1)
        FROM auto_orders
        WHERE (order_checked_at IS NULL OR TRIM(order_checked_at) = '')
          AND (created_at IS NULL OR datetime(created_at) < datetime(?))
        """,
        (since_iso,),
    ).fetchone()
    legacy_without_checked_at = int(legacy_without_checked_at_row[0] or 0) if legacy_without_checked_at_row else 0
    if auto_count <= 0:
        warnings.setdefault("no_auto_orders_history", [])
    elif recent_auto_orders > 0 and recent_without_checked_at > 0:
        warnings.setdefault(
            "auto_order_status_sync_has_no_checked_at",
            [
                {
                    "problem": "auto_order_status_sync_has_no_checked_at",
                    "recent_last_order_at": recent_last_order_at,
                    "recent_last_order_checked_at": recent_last_checked_at,
                    "recent_auto_orders": recent_auto_orders,
                    "recent_auto_orders_without_checked_at": recent_without_checked_at,
                    "auto_orders": auto_count,
                }
            ],
        )

    return {
        "ok": not problems,
        "since": since_iso,
        "live_entry_rows": checked_live_rows,
        "auto_orders": auto_count,
        "last_order_at": last_order_at,
        "last_order_checked_at": last_checked_at,
        "auto_orders_without_checked_at": auto_without_checked_at,
        "legacy_auto_orders_without_checked_at": legacy_without_checked_at,
        "recent_auto_orders": recent_auto_orders,
        "recent_last_order_at": recent_last_order_at,
        "recent_last_order_checked_at": recent_last_checked_at,
        "recent_auto_orders_without_checked_at": recent_without_checked_at,
        "problems": problems,
        "warnings": warnings,
    }


def _audit_entry_hard_reason_policy(conn: sqlite3.Connection, *, since_iso: str, limit: int) -> dict[str, Any]:
    problems: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        """
        SELECT id, created_at, slug, event_date, outcome, side,
               decided_state, will_order, reason_codes_json, details_json
        FROM auto_trade_decisions
        WHERE action = 'entry'
          AND COALESCE(will_order, 0) = 0
          AND datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ?
        """,
        (since_iso, int(limit)),
    ).fetchall()
    scanned_with_codes = 0
    for row in rows:
        codes = _reason_codes(row["reason_codes_json"])
        if not codes:
            continue
        scanned_with_codes += 1
        bad_codes = [code for code in codes if code not in ENTRY_ALLOWED_HARD_REASON_CODES]
        if not bad_codes:
            continue
        if _is_legacy_posterior_target_cleanup_entry(row, codes):
            continue
        _add_problem(
            problems,
            "entry_hard_reason_code_not_allowed",
            row,
            details={
                "bad_reason_codes": bad_codes,
                "allowed_reason_codes": sorted(ENTRY_ALLOWED_HARD_REASON_CODES),
            },
        )
    return {
        "ok": not problems,
        "since": since_iso,
        "rows_scanned": len(rows),
        "rows_with_hard_reason_codes": scanned_with_codes,
        "problems": problems,
        "warnings": {},
    }


def _audit_fills_positions_exit(conn: sqlite3.Connection, *, since_iso: str, limit: int) -> dict[str, Any]:
    problems: dict[str, list[dict[str, Any]]] = {}
    warnings: dict[str, list[dict[str, Any]]] = {}
    fills_row = conn.execute("SELECT COUNT(1), MAX(created_at) FROM edge_trade_fills").fetchone()
    fills_count = int(fills_row[0] or 0) if fills_row else 0
    last_fill_at = str(fills_row[1] or "") if fills_row else ""
    if fills_count <= 0:
        warnings.setdefault("no_fill_history", [])

    orphan_fill_rows = conn.execute(
        """
        SELECT f.id, f.created_at, f.slug, f.event_date, f.outcome, f.outcome_side AS side,
               f.order_id, f.trade_side
        FROM edge_trade_fills f
        LEFT JOIN auto_orders o ON o.order_id = f.order_id
        WHERE datetime(f.created_at) >= datetime(?)
          AND COALESCE(f.order_id, '') <> ''
          AND o.order_id IS NULL
        ORDER BY datetime(f.created_at) DESC, f.id DESC
        LIMIT ?
        """,
        (since_iso, int(limit)),
    ).fetchall()
    for row in orphan_fill_rows:
        _add_problem(
            warnings,
            "fill_missing_auto_order_provenance",
            row,
            details={"order_id": str(row["order_id"] or ""), "trade_side": str(row["trade_side"] or "")},
        )

    buy_fill_without_trade = conn.execute(
        """
        SELECT f.id, f.created_at, f.slug, f.event_date, f.outcome, f.outcome_side AS side,
               f.order_id, f.trade_side
        FROM edge_trade_fills f
        LEFT JOIN edge_trades t
          ON t.slug = f.slug
         AND t.event_date = f.event_date
         AND t.outcome = f.outcome
         AND t.side = f.outcome_side
        WHERE datetime(f.created_at) >= datetime(?)
          AND UPPER(COALESCE(f.trade_side, '')) = 'BUY'
          AND t.id IS NULL
        ORDER BY datetime(f.created_at) DESC, f.id DESC
        LIMIT ?
        """,
        (since_iso, int(limit)),
    ).fetchall()
    for row in buy_fill_without_trade:
        _add_problem(
            problems,
            "buy_fill_missing_edge_trade_state",
            row,
            details={"order_id": str(row["order_id"] or "")},
        )

    open_rows = conn.execute(
        """
        SELECT id, updated_at AS created_at, slug, event_date, outcome, side,
               state, position_size, exit_skip_reason, exit_skip_detail, exit_skip_at
        FROM edge_trades
        WHERE COALESCE(position_size, 0) > 1e-9
           OR state IN ('entry', 'entry_pending')
        ORDER BY datetime(updated_at) DESC, id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    for row in open_rows:
        position_size = _to_float(row["position_size"]) or 0.0
        state = str(row["state"] or "").strip().lower()
        if position_size > 1e-9 and (
            not str(row["exit_skip_reason"] or "").strip()
            or not str(row["exit_skip_at"] or "").strip()
        ):
            _add_problem(
                problems,
                "open_position_missing_exit_skip_audit",
                row,
                details={"state": state, "position_size": position_size},
            )

    return {
        "ok": not problems,
        "since": since_iso,
        "fills": fills_count,
        "last_fill_at": last_fill_at,
        "open_positions": len(open_rows),
        "problems": problems,
        "warnings": warnings,
    }


def _target_realization_activation_quiet_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """Explain an intentional no-intent window before the configured D0 takeover."""

    if not _env_bool("PM_TARGET_REALIZATION_V3_ENABLED", False):
        return {}
    activation_date = str(
        os.environ.get("PM_TARGET_REALIZATION_V3_ACTIVATION_DATE_ET", "") or ""
    ).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", activation_date):
        return {}
    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    if today_et >= activation_date:
        return {}
    row = conn.execute(
        """
        SELECT id, created_at, event_date, action, decided_state, will_order,
               reason_codes_json
        FROM auto_trade_decisions
        WHERE action = 'hold'
          AND decided_state = 'frozen'
          AND COALESCE(will_order, 0) = 0
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return {}
    codes = _reason_codes(row["reason_codes_json"])
    reason = "target_realization_d1_existing_positions_frozen"
    if reason not in codes:
        return {}
    return {
        "active": True,
        "reason": reason,
        "activation_date_et": activation_date,
        "today_et": today_et,
        "decision_id": int(row["id"] or 0),
        "decision_created_at": str(row["created_at"] or ""),
        "event_date": str(row["event_date"] or ""),
    }


def build_db_live_readiness(
    db_path: Path = DEFAULT_DB,
    *,
    since: str = "",
    since_minutes: float = 60.0,
    limit: int = 200,
    live_cutover: bool = False,
    live_trading: bool | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    if live_trading is None:
        live_trading = _live_trading_enabled()
    if dry_run is None:
        dry_run = _env_bool("PM_AUTO_TRADE_DRY_RUN", True)
    since_iso = str(since or "").strip()
    if not since_iso:
        since_dt = datetime.now(timezone.utc) - timedelta(minutes=max(1.0, float(since_minutes)))
        since_iso = since_dt.isoformat(timespec="seconds")
    blockers: list[str] = []
    warnings: list[str] = []
    if not db_path.exists():
        return {
            "ok": False,
            "db": str(db_path),
            "read_only": True,
            "blockers": ["archive_db_missing"],
            "warnings": [],
        }
    conn: sqlite3.Connection | None = None
    intentional_quiet_state: dict[str, Any] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        _configure_readonly_connection(conn)
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        return _db_read_error_payload(
            db_path,
            exc,
            since_iso=since_iso,
            live_cutover=bool(live_cutover),
            live_trading=bool(live_trading),
            dry_run=bool(dry_run),
        )
    conn.row_factory = sqlite3.Row
    try:
        try:
            required_tables = [
                "auto_trade_decisions",
                "auto_orders",
                "edge_trades",
                "edge_trade_fills",
            ]
            missing_tables = [name for name in required_tables if not _table_exists(conn, name)]
            if missing_tables:
                blockers.extend([f"missing_table:{name}" for name in missing_tables])
                return {
                    "ok": False,
                    "db": str(db_path),
                    "read_only": True,
                    "blockers": blockers,
                    "warnings": warnings,
                    "missing_tables": missing_tables,
                }
            entry_intents = _audit_recent_entry_intents(
                conn,
                since_iso=since_iso,
                limit=int(limit),
                dry_run=bool(dry_run),
            )
            entry_hard_reason_policy = _audit_entry_hard_reason_policy(
                conn,
                since_iso=since_iso,
                limit=int(limit),
            )
            order_persistence = _audit_order_persistence(conn, since_iso=since_iso, limit=int(limit))
            fills_positions_exit = _audit_fills_positions_exit(conn, since_iso=since_iso, limit=int(limit))
            intentional_quiet_state = _target_realization_activation_quiet_state(conn)
        except sqlite3.Error as exc:
            return _db_read_error_payload(
                db_path,
                exc,
                since_iso=since_iso,
                live_cutover=bool(live_cutover),
                live_trading=bool(live_trading),
                dry_run=bool(dry_run),
            )
    finally:
        conn.close()

    recent_order_intent_rows = int(entry_intents.get("will_order_rows") or 0) + int(
        entry_intents.get("queued_order_intent_rows") or 0
    )
    if recent_order_intent_rows <= 0:
        code = "no_recent_entry_order_intent"
        if intentional_quiet_state:
            pass
        elif live_cutover or live_trading:
            blockers.append(code)
        else:
            warnings.append(code)
    for code, examples in (entry_intents.get("problems") or {}).items():
        if examples:
            blockers.append(str(code))
    for code, examples in (entry_hard_reason_policy.get("problems") or {}).items():
        if examples:
            blockers.append(str(code))
    for code, examples in (order_persistence.get("problems") or {}).items():
        if examples:
            blockers.append(str(code))
    for code, examples in (fills_positions_exit.get("problems") or {}).items():
        if examples:
            blockers.append(str(code))
    for section in (entry_intents, entry_hard_reason_policy, order_persistence, fills_positions_exit):
        for code, examples in (section.get("warnings") or {}).items():
            if examples or str(code).startswith("no_"):
                warnings.append(str(code))

    return {
        "ok": not blockers,
        "db": str(db_path),
        "read_only": True,
        "since": since_iso,
        "live_cutover": bool(live_cutover),
        "live_trading": bool(live_trading),
        "dry_run": bool(dry_run),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "entry_intents": entry_intents,
        "entry_hard_reason_policy": entry_hard_reason_policy,
        "order_persistence": order_persistence,
        "fills_positions_exit": fills_positions_exit,
        "intentional_quiet_state": intentional_quiet_state,
    }


def build_clob_readiness(*, live_cutover: bool = False) -> dict[str, Any]:
    ident = _identity()
    balance_ok, balance = _json_command("balance --refresh 1", timeout=90)
    open_ok, open_orders = _json_command("fetch-open-orders", timeout=90)
    balance_value = _amount(balance.get("balance")) if isinstance(balance, dict) else None
    allowance_value = _amount(balance.get("allowance")) if isinstance(balance, dict) else None
    allowance_max = _amount(balance.get("allowances")) if isinstance(balance, dict) else None
    if allowance_value is None:
        allowance_value = allowance_max
    balance_allowance = _amount(balance.get("balance_allowance") or balance.get("balanceAllowance")) if isinstance(balance, dict) else None
    if balance_allowance is None and balance_value is not None and allowance_value is not None:
        balance_allowance = min(max(0.0, balance_value), max(0.0, allowance_value))
    open_count = len(open_orders.get("open_orders") or []) if isinstance(open_orders, dict) else None
    live_trading = _live_trading_enabled()
    min_balance_usdc = _readiness_min_balance_usdc()

    blockers: list[str] = []
    warnings: list[str] = []
    if not ident.get("ok") or not ident.get("hasPrivateKey") or not ident.get("hasApiCreds"):
        blockers.append("missing_clob_credentials")
    if ident.get("ok") and not ident.get("sameSignerFunder") and str(ident.get("signatureType") or "0") == "0":
        warnings.append("signer_funder_different_but_signature_type_0")
    if not balance_ok:
        blockers.append("clob_balance_unavailable")
    elif balance_allowance is not None and balance_allowance <= 0:
        blockers.append("collateral_balance_allowance_zero")
    elif balance_value is not None and balance_value <= 0:
        blockers.append("collateral_balance_zero")
    elif allowance_value is not None and allowance_value <= 0:
        blockers.append("collateral_allowance_zero")
    elif (
        balance_allowance is not None
        and min_balance_usdc > 0
        and balance_allowance < min_balance_usdc
    ):
        if live_trading or live_cutover:
            blockers.append("collateral_balance_allowance_below_min_order")
        else:
            warnings.append("collateral_balance_allowance_below_min_order")
    if not open_ok:
        blockers.append("open_orders_unavailable")
    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "identity": ident,
        "balance": {
            "ok": balance_ok,
            "balance": balance_value,
            "allowance": allowance_value,
            "allowance_max": allowance_max,
            "balance_allowance": balance_allowance,
            "min_required_usdc": min_balance_usdc,
            "min_required_ok": (
                None
                if balance_allowance is None
                else bool(balance_allowance >= min_balance_usdc)
            ),
            "raw_keys": sorted(balance.keys()) if isinstance(balance, dict) else [],
            "error": (
                {
                    "stderr": balance.get("stderr"),
                    "stdout": balance.get("stdout"),
                    "returncode": balance.get("returncode"),
                    "error": balance.get("error"),
                }
                if isinstance(balance, dict) and not balance_ok
                else None
            ),
        },
        "live_trading": {
            "enabled": live_trading,
            "auto_trade_enabled": _env_bool("PM_AUTO_TRADE_ENABLED", False),
            "dry_run": _env_bool("PM_AUTO_TRADE_DRY_RUN", True),
        },
        "open_orders": {
            "ok": open_ok,
            "count": open_count,
            "source": open_orders.get("source") if isinstance(open_orders, dict) else None,
            "error": (
                {
                    "stderr": open_orders.get("stderr"),
                    "stdout": open_orders.get("stdout"),
                    "returncode": open_orders.get("returncode"),
                    "error": open_orders.get("error"),
                }
                if isinstance(open_orders, dict) and not open_ok
                else None
            ),
        },
    }


def build_live_readiness(
    *,
    db_path: Path = DEFAULT_DB,
    since: str = "",
    since_minutes: float = 60.0,
    limit: int = 200,
    live_cutover: bool = False,
    skip_clob: bool = False,
) -> dict[str, Any]:
    _hydrate_runtime_env()
    live_trading = _live_trading_enabled()
    dry_run = _env_bool("PM_AUTO_TRADE_DRY_RUN", True)
    clob = (
        {
            "ok": True,
            "skipped": True,
            "blockers": [],
            "warnings": ["clob_preflight_skipped"],
        }
        if skip_clob
        else build_clob_readiness(live_cutover=bool(live_cutover))
    )
    db = build_db_live_readiness(
        db_path=db_path,
        since=str(since or ""),
        since_minutes=float(since_minutes),
        limit=int(limit),
        live_cutover=bool(live_cutover),
        live_trading=bool(live_trading),
        dry_run=bool(dry_run),
    )
    blockers = sorted(set([*(clob.get("blockers") or []), *(db.get("blockers") or [])]))
    warnings = sorted(set([*(clob.get("warnings") or []), *(db.get("warnings") or [])]))
    balance = clob.get("balance") if isinstance(clob.get("balance"), dict) else {}
    balance_allowance = _to_float(balance.get("balance_allowance")) if balance else None
    min_required = _to_float(balance.get("min_required_usdc")) if balance else None
    entry_intents = db.get("entry_intents") if isinstance(db.get("entry_intents"), dict) else {}
    will_order_rows = int(entry_intents.get("will_order_rows") or 0) if entry_intents else 0
    complete_intents = int(entry_intents.get("complete_intents") or 0) if entry_intents else 0
    blocker_clearance: dict[str, Any] = {
        "ok": not blockers,
        "blockers": list(blockers),
        "items": [],
        "next_mainline_task": (
            "observe_first_d0_fact"
            if bool(live_trading) and not blockers
            else "enable_live_trading"
            if bool(live_cutover) and not blockers
            else "paper_trading_validation"
            if not blockers
            else "live_cutover_readiness_blocker_clearance"
            if bool(live_cutover)
            else "paper_trading_readiness_blocker_clearance"
        ),
    }
    if "collateral_balance_allowance_below_min_order" in blockers:
        shortfall = None
        if balance_allowance is not None and min_required is not None:
            shortfall = max(0.0, float(min_required) - float(balance_allowance))
        blocker_clearance["items"].append(
            {
                "blocker": "collateral_balance_allowance_below_min_order",
                "status": "blocked",
                "current_usdc": balance_allowance,
                "required_usdc": min_required,
                "shortfall_usdc": shortfall,
                "clearance": "fund_clob_collateral_balance_allowance_to_min_required",
            }
        )
    if "no_recent_entry_order_intent" in blockers:
        blocker_clearance["items"].append(
            {
                "blocker": "no_recent_entry_order_intent",
                "status": "blocked",
                "will_order_rows": will_order_rows,
                "complete_intents": complete_intents,
                "min_order_size": entry_intents.get("min_order_size") if entry_intents else None,
                "clearance": "wait_for_or_trigger_complete_dry_run_entry_intent",
                "dry_run_probe_command": _dry_run_probe_command(),
            }
        )
    if "clob_balance_unavailable" in blockers:
        blocker_clearance["items"].append(
            {
                "blocker": "clob_balance_unavailable",
                "status": "blocked",
                "clearance": "restore_read_only_clob_balance_preflight",
                "diagnostic_command": (
                    ".venv/bin/python scripts/check_trade_readiness.py "
                    "--live-cutover --since-minutes 180"
                ),
            }
        )
    if "open_orders_unavailable" in blockers:
        blocker_clearance["items"].append(
            {
                "blocker": "open_orders_unavailable",
                "status": "blocked",
                "clearance": "restore_read_only_clob_open_orders_preflight",
                "diagnostic_command": (
                    ".venv/bin/python scripts/check_trade_readiness.py "
                    "--live-cutover --since-minutes 180"
                ),
            }
        )
    cutover_steps = [
        {
            "step": "dry_run_probe",
            "status": "ready" if "no_recent_entry_order_intent" in blockers else "passed",
            "command": _dry_run_probe_command(),
        },
        {
            "step": "clob_preflight",
            "status": "passed" if not any(str(code).startswith("clob_") for code in blockers) else "blocked",
        },
        {
            "step": "manual_live_enable",
            "status": "already_enabled" if live_trading else "requires_confirmation",
            "env": {
                "PM_AUTO_TRADE_ENABLED": "1",
                "PM_AUTO_TRADE_DRY_RUN": "0",
            },
        },
        {
            "step": "post_cutover_verify",
            "status": "passed" if live_trading and not blockers else "pending",
            "command": (
                ".venv/bin/python scripts/check_trade_readiness.py "
                "--live-cutover --since-minutes 180"
            ),
        },
    ]
    return {
        "ok": not blockers,
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": {
            "live_cutover": bool(live_cutover),
            "live_trading": bool(live_trading),
            "auto_trade_enabled": _env_bool("PM_AUTO_TRADE_ENABLED", False),
            "dry_run": bool(dry_run),
        },
        "blockers": blockers,
        "warnings": warnings,
        "blocker_clearance": blocker_clearance,
        "cutover_steps": cutover_steps,
        "clob": clob,
        "db_audit": db,
        "conclusion": (
            "live_running_ready"
            if not blockers and live_trading
            else "ready_to_switch_live"
            if not blockers and live_cutover
            else "paper_trading_ready"
            if not blockers
            else "not_ready"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only live readiness audit for Polymarket weather auto-trading.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since", default="", help="Absolute UTC/ISO cutoff for DB audit.")
    parser.add_argument("--since-minutes", type=float, default=60.0)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--out", default="")
    parser.add_argument("--live-cutover", action="store_true", help="Treat this as a pre-live cutover gate.")
    parser.add_argument("--skip-clob", action="store_true", help="Skip CLOB credential/balance/open-order preflight.")
    args = parser.parse_args()

    output = build_live_readiness(
        db_path=Path(args.db),
        since=str(args.since or ""),
        since_minutes=float(args.since_minutes),
        limit=int(args.limit),
        live_cutover=bool(args.live_cutover),
        skip_clob=bool(args.skip_clob),
    )
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
