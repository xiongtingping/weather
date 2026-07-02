#!/usr/bin/env python3
"""Read-only audit for the local weather decision core contract."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
FEEDBACK_SNAPSHOT_KIND = "exit_policy_audit"
AUDIT_ENV_KEYS = (
    "PM_AUTO_TRADE_ENTRY_ONLY_REALIZABLE",
    "PM_AUTO_TRADE_DRY_RUN",
    "PM_AUTO_TRADE_ENABLED",
)

EXCLUDED_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "_moved_from_old_root",
    "data",
    "dist",
    "logs",
    "node_modules",
    "output",
}

SCAN_SUFFIXES = {
    ".md",
    ".py",
    ".sh",
    ".js",
    ".mjs",
    ".ts",
    ".tsx",
    ".html",
}

FORBIDDEN_PATTERNS = (
    ("trust_weight", re.compile(r"\btrust_weight\b", re.I)),
    ("governance_projection", re.compile(r"\bGovernance\s+Projection\b", re.I)),
)

TERM_ALLOWLIST = {
    "AGENTS.md",
    "docs/WEATHER_DECISION_CORE.md",
    "scripts/weather_decision_core_audit.py",
    "tests/test_weather_decision_core_audit.py",
}

SOFT_ONLY_CODES = {
    "ask_above_cap",
    "depth_below_min",
    "depth_missing",
    "follow_no_price_low",
    "follow_signal_edge_low",
    "follow_signal_edge_missing",
    "follow_yes_price_high",
    "main_ask_above_max",
    "main_ask_above_q_limit",
    "main_ask_below_min",
    "main_no_ask_above_max",
    "main_no_ask_below_min",
    "main_no_blocked_by_yes_q",
    "main_q_above_max",
    "main_q_below_min",
    "main_spread_missing",
    "main_spread_negative",
    "main_spread_too_wide",
    "market_one_sided_thin_book",
    "market_spread_missing",
    "market_tick_missing",
    "market_tick_stale",
    "outcome_volume_below",
    "outcome_volume_below_live",
    "outcome_volume_missing",
    "outcome_volume_disabled",
    "orderbook_missing_recent",
    "q_missing_soft",
    "signal_edge_below_min",
    "spread_too_wide",
    "yes_top_q_cal_missing",
    "yes_top_q_market_mismatch",
    "yes_top_signal_edge_low",
    "yes_top_signal_edge_missing",
    "yes_top_ask_above_max",
    "yes_top_ask_below_min",
    "yes_top_spread_missing",
    "yes_top_spread_too_wide",
}

LEGACY_EDGE_CODES = {
    "edge_realizable_low",
    "edge_realizable_missing",
    "edge_realizable_v2_missing",
    "yes_top_exec_edge_entry_low",
    "yes_top_exec_edge_entry_missing",
    "follow_exec_edge_entry_low",
    "follow_exec_edge_entry_missing",
    "hard_bound_no_exec_edge_entry_low",
    "hard_bound_no_exec_edge_entry_missing",
    "adj_pair_exec_edge_entry_low",
    "adj_pair_exec_edge_entry_missing",
}

TARGET_REALIZATION_EXIT_PREFIX = "target_realization_"
EXIT_POLICY_PRIMARY_SOURCE = "target_realization_binary_settlement"
EXIT_POLICY_FAMILY_CODES = {"target_realization_exit"}
LEGACY_EXIT_POLICY_FAMILY_CODES = {"edge_exit", "repricing_exit", "risk_exit"}
LEGACY_EXIT_TRIGGER_CODES = {
    "edge_min",
    "edge_exit",
    "hold_edge_realizable",
    "lag",
    "lcb_q_peak_switch",
    "stop_loss",
    "trail_stop",
    "trend_down",
    "weather_repricing_target",
}
EXIT_OPERATIONAL_REASON_CODES = {
    "exit_holdoff",
    "exit_exchange_min_size",
    "exit_force_backoff",
    "exit_marketable_immediate_confirmed",
    "exit_marketable_immediate_confirm_pending",
    "exit_marketable_retry_failed",
    "exit_marketable_retry_no_fill",
    "exit_marketable_retry_skipped_price_floor",
    "exit_marketable_retry_submitted",
    "exit_no_trigger",
    "exit_orderbook_missing",
    "exit_price_invalid",
    "exit_price_missing",
    "exit_quote_stale",
    "exit_retry_blocked",
    "exit_wait_fill",
    "exit_wait_merge",
    "exit_wait_merge_min_size",
    "hold",
    "mirror_wait_source_exit",
    "order_already_live",
    "order_submitted",
    "place_order_failed",
    "synced_exit_fill_without_decision",
    "synced_fill_without_decision",
}
BAYES_TARGET_ENTRY_CODES = {
    "posterior_settlement_target",
    "posterior_target_direct",
    "posterior_target_probability_changed",
    "ssot_target_bucket_changed",
    "target_realization_order_intent",
    "hard_source_direct",
    "hard_source_direct_target",
}


def _utc_cutoff_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=float(hours))).isoformat(timespec="seconds")


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


def _reason_codes(raw: Any) -> List[str]:
    parsed = _safe_json(raw)
    if not isinstance(parsed, list):
        return []
    out: List[str] = []
    for item in parsed:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _truthy_env(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_env_line(line: str) -> Tuple[str, str] | None:
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[len("export ") :].strip()
    if "=" not in text:
        return None
    name, value = text.split("=", 1)
    name = name.strip()
    if not name:
        return None
    value = value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return name, value


def _hydrate_runtime_env() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                parsed = _parse_env_line(line)
                if not parsed:
                    continue
                name, value = parsed
                if name in AUDIT_ENV_KEYS:
                    os.environ[name] = value
        except OSError:
            pass
    for name in AUDIT_ENV_KEYS:
        if os.environ.get(name):
            continue
        try:
            proc = subprocess.run(
                ["launchctl", "getenv", name],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except Exception:
            continue
        value = proc.stdout.strip()
        if value:
            os.environ[name] = value


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "ok", "pass"}


def _is_target_realization_exit_code(raw_code: Any) -> bool:
    code = str(raw_code or "").strip().lower()
    return bool(code and code.startswith(TARGET_REALIZATION_EXIT_PREFIX))


def _is_legacy_exit_trigger_code(raw_code: Any) -> bool:
    code = str(raw_code or "").strip().lower()
    if not code or code in EXIT_OPERATIONAL_REASON_CODES:
        return False
    if _is_target_realization_exit_code(code):
        return False
    return True


def _exit_reason_family_for_code(raw_code: Any) -> str:
    if _is_target_realization_exit_code(raw_code):
        return "target_realization_exit"
    return ""


def _exit_policy_primary_edge_source(details: Mapping[str, Any]) -> str:
    return str(
        details.get("exit_policy_primary_edge_source")
        or details.get("exit_edge_primary_source")
        or ""
    ).strip()


def _exit_policy_primary_source(details: Mapping[str, Any]) -> str:
    return str(details.get("exit_policy_primary_source") or "").strip()


def _is_legacy_compacted_exit_policy_details(details: Mapping[str, Any]) -> bool:
    has_policy_source = bool(
        str(
            details.get("exit_policy_primary_edge_source")
            or details.get("exit_edge_primary_source")
            or ""
        ).strip()
    )
    has_legacy_edge_value = (
        _to_float(details.get("hold_edge_realizable")) is not None
        or _to_float(details.get("exec_edge_exit")) is not None
    )
    has_policy_min_value = (
        _to_float(details.get("exit_policy_edge_min")) is not None
        or _to_float(details.get("exit_hold_edge_realizable_min")) is not None
    )
    if not has_legacy_edge_value and not has_policy_min_value:
        return False
    legacy_markers = (
        "q_bayes_source",
        "source_kind",
        "source_fit_tier",
        "observed_nowcast_source_fit_tier",
        "weather_likelihood_features_source",
    )
    has_legacy_marker = any(str(details.get(key) or "").strip() for key in legacy_markers)
    if not has_legacy_marker:
        return False
    if has_legacy_edge_value:
        return True
    return has_policy_source or any(
        key in details
        for key in (
            "exit_policy_primary_edge_source",
            "exit_edge_primary_source",
            "exit_policy_rule",
            "exit_policy_version",
        )
    )


def _is_legacy_compacted_unverifiable_exit_details(
    details: Mapping[str, Any],
    reason_codes: Sequence[str],
) -> bool:
    if str(details.get("exit_policy_primary_edge_source") or details.get("exit_edge_primary_source") or "").strip():
        return False
    if str(details.get("exit_policy_version") or "").strip():
        return False
    edge_keys = (
        "hold_edge_realizable",
        "hold_edge_realizable_trigger",
        "prev_hold_edge_realizable",
        "exit_hold_edge_realizable_min",
        "exec_edge_exit",
    )
    if any(_to_float(details.get(key)) is not None for key in edge_keys):
        return False
    normalized_codes = {str(code or "").strip() for code in reason_codes}
    if not normalized_codes:
        return False
    if any(code not in EXIT_OPERATIONAL_REASON_CODES for code in normalized_codes):
        return False
    legacy_markers = (
        "q_bayes_source",
        "source_kind",
        "source_fit_tier",
        "source_fit_na_reason",
        "observed_nowcast_source_fit_tier",
        "weather_likelihood_features_source",
    )
    if not any(str(details.get(key) or "").strip() for key in legacy_markers):
        return False
    strategy = str(details.get("strategy") or "").strip().lower()
    return strategy in {"", "exit"}


def _rate(num: int, den: int) -> Optional[float]:
    return (int(num) / int(den)) if int(den) else None


def _is_legacy_q_entry_observation_row(
    details: Mapping[str, Any],
    *,
    reason_codes: Sequence[str],
    will_order: bool,
) -> bool:
    """Pre-tradeability probability observation rows are not executable entries."""

    normalized_codes = {str(code or "").strip() for code in reason_codes if str(code or "").strip()}
    if will_order:
        return False
    if normalized_codes and not normalized_codes.issubset({"contract_rule_missing"}):
        return False
    strategy = str(details.get("strategy") or "").strip().lower()
    if strategy not in {"", "main", "lcb_global", "lcb_global_main", "path_probability_follow", "hard_source_direct"}:
        return False
    if _to_float(details.get("q_entry")) is None:
        return False
    if not normalized_codes:
        return True
    executable_fields = (
        "edge_tradeability_profit_value",
        "edge_tradeability_edge_lcb",
        "entry_usable_edge",
        "entry_usable_edge_na_reason",
        "exec_edge_entry",
    )
    if any(details.get(key) not in (None, "", []) for key in executable_fields):
        return False
    if _truthy(details.get("edge_tradeability_pass")):
        return False
    return True


def _is_dry_run_decision_details(details: Mapping[str, Any]) -> bool:
    return _truthy(details.get("dry_run")) or str(details.get("status") or "").strip().lower() == "dry_run"


def _is_bayes_target_entry_row(
    details: Mapping[str, Any],
    *,
    reason_codes: Sequence[str],
) -> bool:
    normalized_codes = {str(code or "").strip() for code in reason_codes if str(code or "").strip()}
    if normalized_codes & BAYES_TARGET_ENTRY_CODES:
        return True
    strategy = str(details.get("strategy") or details.get("weather_strategy_family") or "").strip().lower()
    source = str(details.get("decision_source") or details.get("entry_source") or "").strip().lower()
    if _truthy(details.get("posterior_target_direct")):
        return True
    if str(details.get("decision_ssot") or "").strip() == "posterior_temperature_distribution":
        return True
    if str(details.get("entry_alpha_gate_source") or "").strip() == "posterior_settlement":
        return True
    if str(details.get("target_realization_reliability_source") or "").strip() == "posterior_target_direct":
        return True
    return bool(
        strategy in {"posterior_target_direct", "target_realization_order_intent", "target_realization_binary_settlement", "posterior_settlement"}
        or source in {"posterior_target_direct", "target_realization_order_intent"}
    )


def _first_finite_float(*values: Any) -> Optional[float]:
    for value in values:
        out = _to_float(value)
        if out is not None:
            return float(out)
    return None


def _iter_scan_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in rel_parts):
            continue
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        yield path


def scan_for_external_terms(root: Path = ROOT) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    for path in _iter_scan_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in TERM_ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for code, pattern in FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    hits.append(
                        {
                            "code": code,
                            "path": rel,
                            "line": lineno,
                            "text": line.strip()[:240],
                        }
                    )
    return hits


def _fetch_recent_decisions(conn: sqlite3.Connection, since_iso: str, limit: int) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, created_at, action, decided_state, will_order, reason_codes_json, details_json
        FROM auto_trade_decisions
        WHERE datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ?
        """,
        (since_iso, int(limit)),
    )
    return list(cur.fetchall() or [])


def _fetch_latest_exit_decisions(
    conn: sqlite3.Connection,
    limit: int,
    *,
    since_iso: str = "",
) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    since_text = str(since_iso or "").strip()
    where_since = "AND datetime(created_at) >= datetime(?)" if since_text else ""
    params: Tuple[Any, ...] = (since_text, int(limit)) if since_text else (int(limit),)
    cur.execute(
        f"""
        SELECT id, created_at, action, decided_state, will_order, reason_codes_json, details_json
        FROM auto_trade_decisions
        WHERE action = 'exit'
          {where_since}
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ?
        """,
        params,
    )
    return list(cur.fetchall() or [])


def _configure_read_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA query_only=ON")


def _db_read_error_payload(
    db_path: Path,
    since_iso: str,
    *,
    exc: sqlite3.Error,
    term_hits: List[Dict[str, Any]],
    entry_only_realizable: bool,
    dry_run: bool,
    scan_terms: bool,
    exit_policy_only: bool,
) -> Dict[str, Any]:
    error = f"{type(exc).__name__}:{exc}"
    decision_audit: Dict[str, Any] = {
        "ok": False,
        "read_only": True,
        "skipped": True,
        "reason": "archive_db_read_error",
        "error": error,
    }
    latest_exit_policy_coverage: Dict[str, Any] = {
        "read_only": True,
        "skipped": True,
        "reason": "archive_db_read_error",
        "error": error,
    }
    return {
        "ok": False,
        "read_only": True,
        "db": str(db_path),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "entry_only_realizable": bool(entry_only_realizable),
            "dry_run": bool(dry_run),
            "scan_terms": bool(scan_terms),
            "exit_policy_only": bool(exit_policy_only),
        },
        "violations": {
            "archive_db_read_error": error,
            "external_terms": term_hits,
            "legacy_only_fit_rows": 0,
            "missing_top_fit_rows": 0,
            "missing_fit_na_reason_rows": 0,
            "missing_entry_usable_edge_rows": 0,
            "exit_policy_missing_rows": 0,
            "legacy_exit_policy_semantic_violations": [],
            "exit_policy_edge_source_violations": [],
            "soft_hard_block_counts": {},
            "legacy_edge_block_counts": {},
            "dry_run_entry_state_violations": [],
            "entry_unusable_edge_order_violations": [],
        },
        "decision_audit": decision_audit,
        "latest_exit_policy_coverage": latest_exit_policy_coverage,
        "notes": [
            "archive_db_read_error means the audit could not read archive.db; retry after database/temp I/O health is restored.",
        ],
    }


def _exit_family_required(codes: Sequence[str]) -> bool:
    for raw_code in codes:
        code = str(raw_code or "").strip()
        if _is_target_realization_exit_code(code):
            return True
    return False


def _summarize_exit_policy_coverage(
    rows: Sequence[sqlite3.Row],
    *,
    limit: int,
) -> Dict[str, Any]:
    policy_missing = 0
    target_primary_source = 0
    hold_edge_present = 0
    family_present = 0
    family_required_rows = 0
    family_missing_required = 0
    examples: Dict[str, List[Dict[str, Any]]] = {
        "policy_missing_rows": [],
        "family_missing_rows": [],
    }
    latest_created_at = ""
    for row in rows:
        if not latest_created_at:
            latest_created_at = str(row["created_at"] or "")
        codes = _reason_codes(row["reason_codes_json"])
        details = _safe_json(row["details_json"])
        details_map = details if isinstance(details, dict) else {}
        policy_version = str(details_map.get("exit_policy_version") or "").strip()
        primary_source = _exit_policy_primary_source(details_map)
        if not policy_version:
            policy_missing += 1
            if len(examples["policy_missing_rows"]) < 5:
                examples["policy_missing_rows"].append(_row_example(row, codes))
        if primary_source == EXIT_POLICY_PRIMARY_SOURCE:
            target_primary_source += 1
        if _first_finite_float(
            details_map.get("exit_policy_edge_value"),
            details_map.get("hold_edge_realizable"),
        ) is not None:
            hold_edge_present += 1
        families = _reason_codes(details_map.get("exit_policy_family_codes"))
        if not families:
            families = _reason_codes(details_map.get("exit_reason_family_codes"))
        if families:
            family_present += 1
        family_required = _exit_family_required(codes)
        if family_required:
            family_required_rows += 1
        if family_required and not families:
            family_missing_required += 1
            if len(examples["family_missing_rows"]) < 5:
                examples["family_missing_rows"].append(_row_example(row, codes))
    rows_checked = len(rows)
    return {
        "read_only": True,
        "limit": int(limit),
        "rows_checked": int(rows_checked),
        "latest_created_at": latest_created_at,
        "policy_missing_count": int(policy_missing),
        "policy_version_present_rate": _rate(rows_checked - policy_missing, rows_checked),
        "target_realization_primary_source_rate": _rate(target_primary_source, rows_checked),
        "hold_edge_primary_source_rate": 0.0,
        "hold_edge_realizable_present_rate": _rate(hold_edge_present, rows_checked),
        "family_present_rate": _rate(family_present, rows_checked),
        "family_required_rows": int(family_required_rows),
        "family_missing_required_count": int(family_missing_required),
        "family_required_present_rate": _rate(
            family_required_rows - family_missing_required,
            family_required_rows,
        ),
        "examples": examples,
    }


def audit_latest_exit_policy_coverage(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    since_iso: str = "",
) -> Dict[str, Any]:
    rows = _fetch_latest_exit_decisions(conn, limit)
    summary = _summarize_exit_policy_coverage(rows, limit=int(limit))
    since_text = str(since_iso or "").strip()
    current_rows = _fetch_latest_exit_decisions(conn, limit, since_iso=since_text) if since_text else []
    current_summary = _summarize_exit_policy_coverage(current_rows, limit=int(limit))
    return {
        **summary,
        "current_since": since_text,
        "current_rows_checked": int(current_summary["rows_checked"]),
        "current_policy_missing_count": int(current_summary["policy_missing_count"]),
        "current_policy_version_present_rate": current_summary["policy_version_present_rate"],
        "current_target_realization_primary_source_rate": current_summary["target_realization_primary_source_rate"],
        "current_hold_edge_primary_source_rate": current_summary["hold_edge_primary_source_rate"],
        "current_hold_edge_realizable_present_rate": current_summary["hold_edge_realizable_present_rate"],
        "current_family_required_rows": int(current_summary["family_required_rows"]),
        "current_family_missing_required_count": int(current_summary["family_missing_required_count"]),
        "current_family_required_present_rate": current_summary["family_required_present_rate"],
        "current_examples": current_summary["examples"],
        "current_window": current_summary,
        "note": "latest-exit coverage is diagnostic only; historical rows are not backfilled by this audit.",
    }


def audit_recent_decisions(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
    limit: int = 5000,
    entry_only_realizable: Optional[bool] = None,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    rows = _fetch_recent_decisions(conn, since_iso, limit)
    reason_counts: Counter[str] = Counter()
    soft_hard_blocks: Counter[str] = Counter()
    legacy_edge_blocks: Counter[str] = Counter()
    fit_rows = 0
    legacy_only_fit_rows = 0
    missing_top_fit_rows = 0
    missing_fit_na_reason_rows = 0
    missing_entry_usable_edge_rows = 0
    legacy_q_entry_observation_rows = 0
    bayes_target_entry_rows = 0
    dry_run_entry_unusable_edge_order_rows = 0
    source_station_mismatch_rows = 0
    exit_policy_missing_rows = 0
    legacy_exit_policy_semantic_violations: List[Dict[str, Any]] = []
    legacy_compacted_unverifiable_exit_rows = 0
    exit_policy_rows: List[Dict[str, Any]] = []
    dry_run_entry_state_violations: List[Dict[str, Any]] = []
    entry_unusable_edge_order_violations: List[Dict[str, Any]] = []
    examples: Dict[str, List[Dict[str, Any]]] = {
        "soft_hard_blocks": [],
        "legacy_edge_blocks": [],
        "legacy_only_fit_rows": [],
        "missing_top_fit_rows": [],
        "missing_fit_na_reason_rows": [],
        "missing_entry_usable_edge_rows": [],
        "legacy_q_entry_observation_rows": [],
        "bayes_target_entry_rows": [],
        "dry_run_entry_unusable_edge_order_rows": [],
        "source_station_mismatch_rows": [],
        "exit_policy_missing_rows": [],
        "legacy_exit_policy_semantic_violations": [],
        "dry_run_entry_state_violations": [],
        "entry_unusable_edge_order_violations": [],
    }

    for row in rows:
        codes = _reason_codes(row["reason_codes_json"])
        reason_counts.update(codes)
        details = _safe_json(row["details_json"])
        details_map = details if isinstance(details, dict) else {}
        action = str(row["action"] or "")
        decided_state = str(row["decided_state"] or "")
        will_order = bool(row["will_order"])
        is_bayes_target_entry = bool(
            action == "entry"
            and _is_bayes_target_entry_row(details_map, reason_codes=codes)
        )
        if is_bayes_target_entry:
            bayes_target_entry_rows += 1
            if len(examples["bayes_target_entry_rows"]) < 5:
                examples["bayes_target_entry_rows"].append(_row_example(row, codes))
        top_source_fit = details_map.get("source_fit_tier")
        prefixed_source_fit = (
            details_map.get("weather_strategy_source_fit_tier")
            or details_map.get("observed_nowcast_source_fit_tier")
        )
        source_fit = top_source_fit or prefixed_source_fit
        legacy_fit = (
            details_map.get("source_trust_tier")
            or details_map.get("weather_strategy_source_trust_tier")
            or details_map.get("observed_nowcast_source_trust_tier")
        )
        if source_fit:
            fit_rows += 1
            if not top_source_fit:
                missing_top_fit_rows += 1
                if len(examples["missing_top_fit_rows"]) < 5:
                    examples["missing_top_fit_rows"].append(_row_example(row, codes))
        elif legacy_fit:
            legacy_only_fit_rows += 1
            if len(examples["legacy_only_fit_rows"]) < 5:
                examples["legacy_only_fit_rows"].append(_row_example(row, codes))
        else:
            source_na_reason = (
                details_map.get("source_fit_na_reason")
                or details_map.get("source_na_reason")
                or details_map.get("weather_strategy_source_na_reason")
                or details_map.get("observed_nowcast_source_na_reason")
            )
            needs_source_fit_reason = bool(
                action == "entry"
                and (
                    details_map.get("strategy")
                    or details_map.get("weather_strategy_family")
                    or details_map.get("resolution_source")
                    or details_map.get("station_id")
                    or details_map.get("market_var")
                )
            )
            if needs_source_fit_reason and _is_legacy_q_entry_observation_row(
                details_map,
                reason_codes=codes,
                will_order=will_order,
            ):
                needs_source_fit_reason = False
            if needs_source_fit_reason and is_bayes_target_entry:
                needs_source_fit_reason = False
            if needs_source_fit_reason and will_order and _is_dry_run_decision_details(details_map):
                needs_source_fit_reason = False
            if needs_source_fit_reason and not source_na_reason:
                missing_fit_na_reason_rows += 1
                if len(examples["missing_fit_na_reason_rows"]) < 5:
                    examples["missing_fit_na_reason_rows"].append(_row_example(row, codes))

        station_match = details_map.get("source_station_match")
        station_mismatch = (
            station_match is False
            or str(details_map.get("source_fit_tier") or "").strip() == "station_mismatch"
            or str(details_map.get("source_na_reason") or "").strip() == "resolution_station_mismatch"
            or str(details_map.get("weather_strategy_source_station_match") or "").strip().lower() == "false"
        )
        if action == "entry" and station_mismatch:
            source_station_mismatch_rows += 1
            if len(examples["source_station_mismatch_rows"]) < 5:
                examples["source_station_mismatch_rows"].append(_row_example(row, codes))

        if action == "entry" and not will_order and entry_only_realizable is True:
            soft_codes_hit = sorted(set(codes) & SOFT_ONLY_CODES)
            legacy_codes_hit = sorted(set(codes) & LEGACY_EDGE_CODES)
            for code in soft_codes_hit:
                soft_hard_blocks[code] += 1
            for code in legacy_codes_hit:
                legacy_edge_blocks[code] += 1
            if soft_codes_hit and len(examples["soft_hard_blocks"]) < 5:
                examples["soft_hard_blocks"].append(_row_example(row, soft_codes_hit))
            if legacy_codes_hit and len(examples["legacy_edge_blocks"]) < 5:
                examples["legacy_edge_blocks"].append(_row_example(row, legacy_codes_hit))

        if action == "entry" and entry_only_realizable is True and not is_bayes_target_entry:
            usable_edge = _to_float(details_map.get("entry_usable_edge"))
            usable_na_reason = str(details_map.get("entry_usable_edge_na_reason") or "").strip()
            if usable_edge is None and not usable_na_reason:
                if will_order and (dry_run is True or _is_dry_run_decision_details(details_map)):
                    pass
                elif _is_legacy_q_entry_observation_row(
                    details_map,
                    reason_codes=codes,
                    will_order=will_order,
                ):
                    legacy_q_entry_observation_rows += 1
                    if len(examples["legacy_q_entry_observation_rows"]) < 5:
                        examples["legacy_q_entry_observation_rows"].append(
                            _row_example(row, codes)
                        )
                else:
                    missing_entry_usable_edge_rows += 1
                    if len(examples["missing_entry_usable_edge_rows"]) < 5:
                        examples["missing_entry_usable_edge_rows"].append(_row_example(row, codes))

        if (
            dry_run is True
            and action == "entry"
            and "order_submitted" in codes
            and decided_state != "entry_dry_run"
        ):
            item = _row_example(row, codes)
            item["decided_state"] = decided_state
            dry_run_entry_state_violations.append(item)
            if len(examples["dry_run_entry_state_violations"]) < 5:
                examples["dry_run_entry_state_violations"].append(item)

        if action == "entry" and will_order and not is_bayes_target_entry:
            usable_edge = _to_float(details_map.get("entry_usable_edge"))
            size_scale = _to_float(details_map.get("entry_usable_edge_size_scale"))
            bad_edge = usable_edge is None or not math.isfinite(usable_edge) or usable_edge <= 1e-9
            bad_scale = size_scale is None or not math.isfinite(size_scale) or size_scale <= 1e-9
            if bad_edge or bad_scale:
                item = _row_example(row, codes)
                item["entry_usable_edge"] = usable_edge
                item["entry_usable_edge_size_scale"] = size_scale
                if (
                    (dry_run is True or _is_dry_run_decision_details(details_map))
                    and usable_edge is None
                    and size_scale is None
                ):
                    dry_run_entry_unusable_edge_order_rows += 1
                    if len(examples["dry_run_entry_unusable_edge_order_rows"]) < 5:
                        examples["dry_run_entry_unusable_edge_order_rows"].append(item)
                    continue
                entry_unusable_edge_order_violations.append(item)
                if len(examples["entry_unusable_edge_order_violations"]) < 5:
                    examples["entry_unusable_edge_order_violations"].append(item)

        if action == "exit":
            legacy_compacted_unverifiable = _is_legacy_compacted_unverifiable_exit_details(
                details_map,
                codes,
            )
            if legacy_compacted_unverifiable:
                legacy_compacted_unverifiable_exit_rows += 1
            policy_version = str(details_map.get("exit_policy_version") or "").strip()
            policy_source = _exit_policy_primary_source(details_map)
            policy_edge_source = _exit_policy_primary_edge_source(details_map)
            exit_policy_rows.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "action": action,
                    "reason_codes": list(codes),
                    "details": _exit_policy_snapshot_details(details_map),
                }
            )
            if legacy_compacted_unverifiable:
                continue
            if not policy_version:
                if not _is_legacy_compacted_exit_policy_details(details_map):
                    exit_policy_missing_rows += 1
                    if len(examples["exit_policy_missing_rows"]) < 5:
                        examples["exit_policy_missing_rows"].append(_row_example(row, codes))
            families = _reason_codes(details_map.get("exit_policy_family_codes"))
            if not families:
                families = _reason_codes(details_map.get("exit_reason_family_codes"))
            policy_triggers = _reason_codes(details_map.get("exit_policy_trigger_reason_codes"))
            legacy_semantic_reasons: List[str] = []
            if policy_edge_source:
                legacy_semantic_reasons.append("legacy_exit_edge_primary_source")
            if policy_version.startswith("exit_policy_v2") and policy_source != EXIT_POLICY_PRIMARY_SOURCE:
                legacy_semantic_reasons.append("missing_target_realization_primary_source")
            if policy_source and policy_source != EXIT_POLICY_PRIMARY_SOURCE:
                legacy_semantic_reasons.append("legacy_exit_policy_primary_source")
            legacy_families = [
                family for family in families if str(family or "").strip() not in EXIT_POLICY_FAMILY_CODES
            ]
            if legacy_families:
                legacy_semantic_reasons.append("legacy_exit_policy_family")
            legacy_codes = [
                code
                for code in list(codes) + list(policy_triggers)
                if _is_legacy_exit_trigger_code(code)
            ]
            if legacy_codes:
                legacy_semantic_reasons.append("legacy_exit_trigger_code")
            if (
                _truthy(details_map.get("exit_policy_edge_triggered"))
                or _truthy(details_map.get("exit_policy_repricing_triggered"))
                or _truthy(details_map.get("exit_policy_risk_triggered"))
            ):
                legacy_semantic_reasons.append("legacy_exit_policy_trigger_flag")
            if legacy_semantic_reasons:
                item = _row_example(row, codes)
                item["legacy_exit_policy_semantic_reasons"] = sorted(set(legacy_semantic_reasons))
                item["exit_policy_primary_edge_source"] = policy_edge_source
                item["exit_policy_primary_source"] = policy_source
                item["exit_policy_family_codes"] = families
                item["exit_policy_trigger_reason_codes"] = policy_triggers
                legacy_exit_policy_semantic_violations.append(item)
                if len(examples["legacy_exit_policy_semantic_violations"]) < 5:
                    examples["legacy_exit_policy_semantic_violations"].append(item)

    return {
        "since": since_iso,
        "limit": int(limit),
        "rows_scanned": len(rows),
        "reason_counts": dict(reason_counts.most_common(30)),
        "source_fit_rows": fit_rows,
        "legacy_only_fit_rows": legacy_only_fit_rows,
        "missing_top_fit_rows": missing_top_fit_rows,
        "missing_fit_na_reason_rows": missing_fit_na_reason_rows,
        "missing_entry_usable_edge_rows": missing_entry_usable_edge_rows,
        "legacy_q_entry_observation_rows": legacy_q_entry_observation_rows,
        "bayes_target_entry_rows": bayes_target_entry_rows,
        "dry_run_entry_unusable_edge_order_rows": dry_run_entry_unusable_edge_order_rows,
        "source_station_mismatch_rows": source_station_mismatch_rows,
        "exit_policy_missing_rows": exit_policy_missing_rows,
        "exit_policy_rows": exit_policy_rows,
        "legacy_compacted_unverifiable_exit_rows": legacy_compacted_unverifiable_exit_rows,
        "legacy_exit_policy_semantic_violations": legacy_exit_policy_semantic_violations,
        "exit_policy_edge_source_violations": legacy_exit_policy_semantic_violations,
        "soft_hard_block_counts": dict(soft_hard_blocks.most_common()),
        "legacy_edge_block_counts": dict(legacy_edge_blocks.most_common()),
        "dry_run_entry_state_violations": dry_run_entry_state_violations,
        "entry_unusable_edge_order_violations": entry_unusable_edge_order_violations,
        "examples": examples,
    }


def _row_example(row: Mapping[str, Any], codes: Sequence[str]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "action": row["action"],
        "reason_codes": list(codes),
    }


def _exit_policy_snapshot_details(details_map: Mapping[str, Any]) -> Dict[str, Any]:
    keep_keys = (
        "exit_policy_version",
        "exit_policy_primary_source",
        "exit_policy_primary_edge_source",
        "exit_edge_primary_source",
        "exit_policy_edge_value",
        "exit_policy_legacy_edge_value",
        "hold_edge_realizable",
        "exit_policy_edge_min",
        "exit_policy_legacy_edge_min",
        "exit_hold_edge_realizable_min",
        "exit_policy_family_codes",
        "exit_reason_family_codes",
        "exit_policy_primary_family",
        "exit_policy_trigger_reason_codes",
        "exit_policy_target_realization_triggered",
        "exit_policy_edge_triggered",
        "exit_policy_repricing_triggered",
        "exit_policy_risk_triggered",
        "exit_policy_no_trigger",
        "exit_policy_order_submitted",
        "exit_policy_marketable_submitted",
    )
    return {key: details_map.get(key) for key in keep_keys if key in details_map}


def build_exit_policy_snapshot(audit_payload: Mapping[str, Any]) -> Dict[str, Any]:
    decision_audit = (
        audit_payload.get("decision_audit")
        if isinstance(audit_payload.get("decision_audit"), Mapping)
        else {}
    )
    examples = (
        decision_audit.get("examples")
        if isinstance(decision_audit.get("examples"), Mapping)
        else {}
    )
    rows = (
        decision_audit.get("exit_policy_rows")
        if isinstance(decision_audit.get("exit_policy_rows"), list)
        else []
    )
    exit_rows = 0
    version_present = 0
    target_primary_source = 0
    hold_edge_present = 0
    family_counts: Counter[str] = Counter()
    primary_family_counts: Counter[str] = Counter()
    trigger_counts: Counter[str] = Counter()
    policy_trigger_counts: Counter[str] = Counter()
    target_realization_triggered = 0
    edge_triggered = 0
    repricing_triggered = 0
    risk_triggered = 0
    no_trigger = 0
    order_submitted = 0
    marketable_submitted = 0
    edge_values: List[float] = []
    edge_min_values: List[float] = []

    for row in rows:
        action = str(row.get("action") or "").strip()
        if action != "exit":
            continue
        exit_rows += 1
        details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
        codes = row.get("reason_codes") if isinstance(row.get("reason_codes"), list) else []
        if str(details.get("exit_policy_version") or "").strip():
            version_present += 1
        primary_source = _exit_policy_primary_source(details)
        if primary_source == EXIT_POLICY_PRIMARY_SOURCE:
            target_primary_source += 1
        if _to_float(details.get("hold_edge_realizable")) is not None:
            hold_edge_present += 1
        edge_value = _first_finite_float(
            details.get("exit_policy_edge_value"),
            details.get("hold_edge_realizable"),
        )
        if edge_value is not None:
            edge_values.append(float(edge_value))
        edge_min = _first_finite_float(
            details.get("exit_policy_edge_min"),
            details.get("exit_hold_edge_realizable_min"),
        )
        if edge_min is not None:
            edge_min_values.append(float(edge_min))
        families = _reason_codes(details.get("exit_policy_family_codes"))
        if not families:
            families = _reason_codes(details.get("exit_reason_family_codes"))
        if not families:
            families = [_exit_reason_family_for_code(code) for code in codes]
        families = [str(family or "").strip() for family in families if str(family or "").strip()]
        for family in families:
            family_counts[family] += 1
        primary_family = str(details.get("exit_policy_primary_family") or "").strip()
        if primary_family:
            primary_family_counts[primary_family] += 1
        for code in codes:
            trigger_counts[str(code)] += 1
        for code in _reason_codes(details.get("exit_policy_trigger_reason_codes")):
            policy_trigger_counts[str(code)] += 1
        if _truthy(details.get("exit_policy_target_realization_triggered")):
            target_realization_triggered += 1
        if _truthy(details.get("exit_policy_edge_triggered")):
            edge_triggered += 1
        if _truthy(details.get("exit_policy_repricing_triggered")):
            repricing_triggered += 1
        if _truthy(details.get("exit_policy_risk_triggered")):
            risk_triggered += 1
        if _truthy(details.get("exit_policy_no_trigger")) or "exit_no_trigger" in codes:
            no_trigger += 1
        if _truthy(details.get("exit_policy_order_submitted")) or "order_submitted" in codes:
            order_submitted += 1
        if _truthy(details.get("exit_policy_marketable_submitted")):
            marketable_submitted += 1

    missing_rows = int(decision_audit.get("exit_policy_missing_rows") or 0)
    legacy_semantic_violations = decision_audit.get("legacy_exit_policy_semantic_violations") or []
    return {
        "ok": bool(audit_payload.get("ok")),
        "kind": FEEDBACK_SNAPSHOT_KIND,
        "generated_at": str(audit_payload.get("generated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        "since": str(decision_audit.get("since") or ""),
        "limit": int(decision_audit.get("limit") or 0),
        "rows_scanned": int(decision_audit.get("rows_scanned") or 0),
        "exit_rows": int(exit_rows),
        "exit_policy_version_present_count": int(version_present),
        "exit_policy_version_present_rate": _rate(version_present, exit_rows),
        "target_realization_primary_source_count": int(target_primary_source),
        "target_realization_primary_source_rate": _rate(target_primary_source, exit_rows),
        "hold_edge_primary_source_count": 0,
        "hold_edge_primary_source_rate": 0.0,
        "hold_edge_realizable_present_count": int(hold_edge_present),
        "hold_edge_realizable_present_rate": _rate(hold_edge_present, exit_rows),
        "exit_policy_missing_rows": int(missing_rows),
        "legacy_exit_policy_semantic_violation_count": (
            len(legacy_semantic_violations) if isinstance(legacy_semantic_violations, list) else 0
        ),
        "exit_policy_edge_source_violation_count": (
            len(legacy_semantic_violations) if isinstance(legacy_semantic_violations, list) else 0
        ),
        "family_counts": dict(family_counts.most_common()),
        "primary_family_counts": dict(primary_family_counts.most_common()),
        "trigger_reason_counts": dict(trigger_counts.most_common(20)),
        "policy_trigger_reason_counts": dict(policy_trigger_counts.most_common(20)),
        "target_realization_triggered_count": int(target_realization_triggered),
        "edge_triggered_count": int(edge_triggered),
        "repricing_triggered_count": int(repricing_triggered),
        "risk_triggered_count": int(risk_triggered),
        "no_trigger_count": int(no_trigger),
        "order_submitted_count": int(order_submitted),
        "marketable_submitted_count": int(marketable_submitted),
        "avg_hold_edge_realizable": (sum(edge_values) / len(edge_values)) if edge_values else None,
        "avg_exit_policy_edge_min": (sum(edge_min_values) / len(edge_min_values)) if edge_min_values else None,
        "examples": {
            "exit_policy_missing_rows": examples.get("exit_policy_missing_rows") or [],
            "legacy_exit_policy_semantic_violations": examples.get("legacy_exit_policy_semantic_violations") or [],
            "exit_policy_edge_source_violations": examples.get("legacy_exit_policy_semantic_violations") or [],
        },
        "field_contract": [
            "exit_rows",
            "exit_policy_version_present_rate",
            "target_realization_primary_source_rate",
            "family_counts",
            "target_realization_triggered_count",
            "edge_triggered_count",
            "repricing_triggered_count",
            "risk_triggered_count",
            "no_trigger_count",
        ],
    }


def _ensure_feedback_snapshot_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            days INTEGER NOT NULL,
            end_date TEXT,
            payload_json TEXT NOT NULL,
            ok INTEGER NOT NULL DEFAULT 1,
            error TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feedback_snapshots_lookup
        ON feedback_snapshots(kind, days, created_at DESC)
        """
    )


def save_feedback_snapshot(
    db_path: Path,
    payload: Mapping[str, Any],
    *,
    kind: str = FEEDBACK_SNAPSHOT_KIND,
    days: int = 1,
) -> Dict[str, Any]:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        _ensure_feedback_snapshot_table(conn)
        conn.execute(
            """
            INSERT INTO feedback_snapshots(kind, days, end_date, payload_json, ok, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(kind or FEEDBACK_SNAPSHOT_KIND),
                int(days),
                str(payload.get("generated_at") or "")[:10],
                json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":")),
                1 if bool(payload.get("ok")) else 0,
                "" if bool(payload.get("ok")) else "audit_not_ok",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "kind": str(kind or FEEDBACK_SNAPSHOT_KIND), "days": int(days)}


def run_audit(args: argparse.Namespace) -> Dict[str, Any]:
    _hydrate_runtime_env()
    since_iso = str(args.since or "").strip() or _utc_cutoff_iso(float(args.since_hours))
    env_entry_only = _env_bool("PM_AUTO_TRADE_ENTRY_ONLY_REALIZABLE", True)
    env_dry_run = _env_bool("PM_AUTO_TRADE_DRY_RUN", True)
    entry_only_realizable = env_entry_only if args.entry_only_realizable is None else bool(args.entry_only_realizable)
    dry_run = env_dry_run if args.dry_run is None else bool(args.dry_run)

    term_hits = scan_for_external_terms(ROOT) if args.scan_terms else []
    db_path = Path(args.db)
    decision_audit: Dict[str, Any] = {
        "skipped": True,
        "reason": "db_missing",
    }
    if db_path.exists():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30) as conn:
                _configure_read_connection(conn)
                decision_audit = audit_recent_decisions(
                    conn,
                    since_iso=since_iso,
                    limit=int(args.limit),
                    entry_only_realizable=entry_only_realizable,
                    dry_run=dry_run,
                )
                latest_exit_policy_coverage = audit_latest_exit_policy_coverage(
                    conn,
                    limit=min(100, max(1, int(args.limit))),
                    since_iso=since_iso,
                )
        except sqlite3.Error as exc:
            return _db_read_error_payload(
                db_path,
                since_iso,
                exc=exc,
                term_hits=term_hits,
                entry_only_realizable=bool(entry_only_realizable),
                dry_run=bool(dry_run),
                scan_terms=bool(args.scan_terms),
                exit_policy_only=bool(getattr(args, "exit_policy_only", False)),
            )
    else:
        latest_exit_policy_coverage = {
            "read_only": True,
            "skipped": True,
            "reason": "db_missing",
        }

    violations = {
        "external_terms": term_hits,
        "legacy_only_fit_rows": int(decision_audit.get("legacy_only_fit_rows") or 0),
        "missing_top_fit_rows": int(decision_audit.get("missing_top_fit_rows") or 0),
        "missing_fit_na_reason_rows": int(decision_audit.get("missing_fit_na_reason_rows") or 0),
        "missing_entry_usable_edge_rows": int(decision_audit.get("missing_entry_usable_edge_rows") or 0),
        "legacy_q_entry_observation_rows": int(decision_audit.get("legacy_q_entry_observation_rows") or 0),
        "dry_run_entry_unusable_edge_order_rows": int(decision_audit.get("dry_run_entry_unusable_edge_order_rows") or 0),
        "source_station_mismatch_rows": int(decision_audit.get("source_station_mismatch_rows") or 0),
        "exit_policy_missing_rows": int(decision_audit.get("exit_policy_missing_rows") or 0),
        "legacy_exit_policy_semantic_violations": decision_audit.get("legacy_exit_policy_semantic_violations") or [],
        "exit_policy_edge_source_violations": decision_audit.get("exit_policy_edge_source_violations") or [],
        "soft_hard_block_counts": decision_audit.get("soft_hard_block_counts") or {},
        "legacy_edge_block_counts": decision_audit.get("legacy_edge_block_counts") or {},
        "dry_run_entry_state_violations": decision_audit.get("dry_run_entry_state_violations") or [],
        "entry_unusable_edge_order_violations": decision_audit.get("entry_unusable_edge_order_violations") or [],
    }
    if bool(getattr(args, "exit_policy_only", False)):
        ok = not term_hits
        ok = ok and not violations["exit_policy_missing_rows"]
        ok = ok and not violations["legacy_exit_policy_semantic_violations"]
    else:
        ok = not term_hits
        ok = ok and not violations["dry_run_entry_state_violations"]
        ok = ok and not violations["entry_unusable_edge_order_violations"]
        ok = ok and not violations["source_station_mismatch_rows"]
        ok = ok and not violations["exit_policy_missing_rows"]
        ok = ok and not violations["legacy_exit_policy_semantic_violations"]
        if entry_only_realizable:
            ok = (
                ok
                and not violations["soft_hard_block_counts"]
                and not violations["legacy_edge_block_counts"]
                and not violations["missing_top_fit_rows"]
                and not violations["missing_fit_na_reason_rows"]
                and not violations["missing_entry_usable_edge_rows"]
            )

    return {
        "ok": bool(ok),
        "read_only": True,
        "db": str(db_path),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "entry_only_realizable": bool(entry_only_realizable),
            "dry_run": bool(dry_run),
            "scan_terms": bool(args.scan_terms),
            "exit_policy_only": bool(getattr(args, "exit_policy_only", False)),
        },
        "violations": violations,
        "decision_audit": decision_audit,
        "latest_exit_policy_coverage": latest_exit_policy_coverage,
        "notes": [
            "source_trust_tier is allowed only as a compatibility alias; source_fit_tier is the current decision-core term.",
            "When PM_AUTO_TRADE_ENTRY_ONLY_REALIZABLE=1, q/ask/spread/depth-style checks should be penalties/diagnostics, not entry hard blockers.",
            "Bayes target-bucket entry rows are audited by posterior/target realization semantics, not legacy entry_usable_edge hard gates.",
            "entry_usable_edge must be present, or have entry_usable_edge_na_reason, for entry decisions in entry-only-realizable mode.",
            "Entry orders must not submit when entry_usable_edge is nonpositive or entry_usable_edge_size_scale is zero.",
            "Exit decisions must include exit_policy_* audit fields; current strategy primary source is target_realization_binary_settlement and legacy edge/stop-loss/repricing semantics must not trigger exits.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only weather decision core audit.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since-hours", type=float, default=1.0)
    parser.add_argument("--since", default="")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--feedback-snapshot",
        action="store_true",
        help="Also write compact exit policy coverage into feedback_snapshots(kind=exit_policy_audit).",
    )
    parser.add_argument("--scan-terms", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--exit-policy-only",
        action="store_true",
        help="Evaluate ok using only exit-policy violations; still reports entry audit details.",
    )
    parser.add_argument("--entry-only-realizable", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()

    payload = run_audit(args)
    if args.feedback_snapshot:
        save_feedback_snapshot(
            Path(args.db),
            build_exit_policy_snapshot(payload),
            days=max(1, int(round(float(args.since_hours or 1.0) / 24.0))),
        )
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({"ok": payload["ok"], "out": args.out or "", "rows": payload["decision_audit"].get("rows_scanned", 0)}, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
