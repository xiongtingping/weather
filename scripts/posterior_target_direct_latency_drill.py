#!/usr/bin/env python3
"""Run a posterior-target direct order latency drill on a temporary archive.db copy."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "archive.db"


def _utc_now_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_loads(raw: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(str(raw or ""))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return []
    return [str(row[1]) for row in rows]


def _insert_or_replace_by_available_columns(
    conn: sqlite3.Connection,
    table: str,
    values: Mapping[str, Any],
    *,
    replace: bool = False,
) -> None:
    cols = [col for col in _table_columns(conn, table) if col in values]
    if not cols:
        return
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(values.get(col) for col in cols),
    )


def _rows(payload: Mapping[str, Any]) -> list[Dict[str, Any]]:
    raw = payload.get("distribution")
    if not isinstance(raw, list):
        raw = payload.get("rows")
    return [item for item in (raw or []) if isinstance(item, dict)]


def _prob01(value: Any) -> Optional[float]:
    try:
        val = float(value)
    except Exception:
        return None
    if not math.isfinite(val):
        return None
    if val > 1.0 and val <= 100.0:
        val /= 100.0
    return max(0.0, min(1.0, val))


def _row_probability(row: Mapping[str, Any]) -> Optional[float]:
    for key in (
        "q_temperature_bucket_posterior",
        "q_bayes_posterior",
        "target_bucket_probability",
        "q_entry",
        "q_raw",
    ):
        val = _prob01(row.get(key))
        if val is not None:
            return val
    return None


def _target_from_payload(payload: Mapping[str, Any]) -> str:
    for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
        selection = payload.get(key)
        if isinstance(selection, Mapping):
            label = str(
                selection.get("target_bucket")
                or selection.get("selected_bucket")
                or selection.get("label")
                or ""
            ).strip()
            if label:
                return label
    best_label = ""
    best_prob = -1.0
    for row in _rows(payload):
        label = str(row.get("outcome") or row.get("label") or "").strip()
        prob = _row_probability(row)
        if label and prob is not None and prob > best_prob:
            best_label = label
            best_prob = float(prob)
    return best_label


def _pick_old_bucket(payload: Mapping[str, Any], target_bucket: str) -> str:
    labels = [
        str(row.get("outcome") or row.get("label") or "").strip()
        for row in _rows(payload)
        if str(row.get("outcome") or row.get("label") or "").strip()
    ]
    target = str(target_bucket or "").strip()
    if target in labels:
        idx = labels.index(target)
        if idx > 0:
            return labels[idx - 1]
        if len(labels) > 1:
            return labels[1]
    for label in reversed(labels):
        if label and label != target:
            return label
    return ""


def _row_for_label(payload: Mapping[str, Any], label: str) -> Dict[str, Any]:
    wanted = str(label or "").strip()
    for row in _rows(payload):
        if str(row.get("outcome") or row.get("label") or "").strip() == wanted:
            return row
    return {}


def _ensure_market_token(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    row: Mapping[str, Any],
    label: str,
    updated_at: str,
) -> Dict[str, str]:
    outcome = str(label or row.get("outcome") or row.get("label") or "").strip()
    yes_token = str(row.get("yes_token") or row.get("token_id") or "").strip()
    no_token = str(row.get("no_token") or "").strip()
    existing = None
    try:
        existing = conn.execute(
            """
            SELECT yes_token, no_token
            FROM market_tokens
            WHERE slug = ? AND event_date = ? AND outcome = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (slug, event_date, outcome),
        ).fetchone()
    except sqlite3.Error:
        existing = None
    if existing is not None:
        yes_token = yes_token or str(existing["yes_token"] or "").strip()
        no_token = no_token or str(existing["no_token"] or "").strip()
    if not yes_token:
        yes_token = f"drill-yes-{abs(hash((slug, event_date, outcome))) % 10**12}"
    if not no_token:
        no_token = f"drill-no-{abs(hash(('no', slug, event_date, outcome))) % 10**12}"
    try:
        conn.execute(
            """
            DELETE FROM market_tokens
            WHERE slug = ? AND event_date = ? AND outcome = ?
            """,
            (slug, event_date, outcome),
        )
    except sqlite3.Error:
        pass
    _insert_or_replace_by_available_columns(
        conn,
        "market_tokens",
        {
            "slug": slug,
            "event_date": event_date,
            "outcome": outcome,
            "yes_token": yes_token,
            "no_token": no_token,
            "market_active": 1,
            "market_closed": 0,
            "market_archived": 0,
            "market_accepting_orders": 1,
            "market_enable_orderbook": 1,
            "updated_at": updated_at,
        },
    )
    return {"yes_token": yes_token, "no_token": no_token}


def _insert_market_tick(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
    best_bid: float,
    best_ask: float,
    created_at: str,
) -> None:
    _insert_or_replace_by_available_columns(
        conn,
        "event_market_ticks",
        {
            "run_date": event_date,
            "event_date": event_date,
            "slug": slug,
            "outcome": outcome,
            "side": side,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": (best_bid + best_ask) / 2.0,
            "spread": max(0.0, best_ask - best_bid),
            "bid_depth_topN": 250.0,
            "ask_depth_topN": 250.0,
            "source": "posterior_target_direct_latency_drill",
            "source_detail": "temporary_db_fake_live_drill",
            "created_at": created_at,
        },
    )


def _insert_previous_posterior_decision(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    previous_bucket: str,
    previous_probability: float,
    created_at: str,
) -> None:
    previous_at = (
        datetime.now(timezone.utc) - timedelta(seconds=30)
    ).isoformat(timespec="seconds")
    _insert_or_replace_by_available_columns(
        conn,
        "auto_trade_decisions",
        {
            "run_id": None,
            "run_date": event_date,
            "slug": slug,
            "event_date": event_date,
            "title": f"posterior target latency drill previous {slug}",
            "outcome": previous_bucket,
            "side": "YES",
            "action": "entry",
            "decided_state": "entry_diagnostic",
            "will_order": 0,
            "reason_codes_json": json.dumps(["posterior_target_direct"]),
            "reasons_json": json.dumps(["temporary drill previous posterior state"]),
            "details_json": json.dumps(
                {
                    "posterior_target_direct": True,
                    "posterior_target_source": "posterior_target_bucket_selection",
                    "target_adjustment_decision_version": "posterior_target_direct_v1",
                    "target_posterior": previous_probability,
                    "q_temperature_bucket_posterior": previous_probability,
                    "target_bucket": previous_bucket,
                    "decision_ssot": "posterior_temperature_distribution",
                    "drill_previous_state": True,
                }
            ),
            "created_at": previous_at or created_at,
        },
    )


def _prepare_posterior_target_scenario(
    db_path: Path,
    *,
    slug: str,
    event_date: str,
    target_bucket: str,
    old_bucket: str,
    target_probability: float,
    previous_probability: float,
    target_ask: float,
    old_bid: float,
) -> Dict[str, Any]:
    now_iso = _utc_now_iso_ms()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            """
            SELECT h.id, h.fact_json
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE l.slug = ? AND l.event_date = ?
            ORDER BY h.id DESC
            LIMIT 1
            """,
            (slug, event_date),
        ).fetchone()
        if latest is None:
            return {"ok": False, "reason": "latest_fact_missing"}
        payload = _json_loads(latest["fact_json"])
        if not payload:
            return {"ok": False, "reason": "latest_fact_json_invalid"}
        target = target_bucket or _target_from_payload(payload)
        if not target:
            return {"ok": False, "reason": "target_bucket_missing"}
        old = old_bucket or _pick_old_bucket(payload, target)
        if not old:
            return {"ok": False, "reason": "old_bucket_missing", "target_bucket": target}
        target_prob = max(0.0, min(1.0, float(target_probability)))
        other_prob = 0.0 if target_prob >= 0.999 else max(0.0, 1.0 - target_prob)
        rows = _rows(payload)
        for row in rows:
            label = str(row.get("outcome") or row.get("label") or "").strip()
            row["market_active"] = True
            row["market_closed"] = False
            row["market_archived"] = False
            row["market_accepting_orders"] = True
            row["market_enable_orderbook"] = True
            if label == target:
                row["q_temperature_bucket_posterior"] = target_prob
                row["q_bayes_posterior"] = target_prob
                row["q_entry"] = target_prob
                row["best_bid"] = max(0.0, min(1.0, float(target_ask) - 0.002))
                row["best_ask"] = float(target_ask)
                row["ask"] = float(target_ask)
                row["spread"] = 0.002
                row["local_peak_hour"] = 16.0
            else:
                row["q_temperature_bucket_posterior"] = other_prob if label == old else 0.0
                row["q_bayes_posterior"] = other_prob if label == old else 0.0
                row["q_entry"] = other_prob if label == old else 0.0
            if label == old:
                row["best_bid"] = float(old_bid)
                row["best_ask"] = min(1.0, float(old_bid) + 0.03)
                row["spread"] = 0.03
        payload["slug"] = slug
        payload["event_date"] = event_date
        payload["run_date"] = event_date
        payload["fact_as_of"] = now_iso
        payload["posterior_target_bucket_selection"] = {
            "target_bucket": target,
            "target_probability": target_prob,
            "target_probability_pct": target_prob * 100.0,
            "target_posterior": target_prob,
            "target_bucket_probability": target_prob,
            "target_bucket_probability_pct": target_prob * 100.0,
            "target_probability_field": "posterior_temperature_distribution.bucket_probabilities",
            "target_bucket_source": "posterior_target_direct_latency_drill",
            "target_bucket_source_semantics": "posterior_temperature_distribution",
            "source": "posterior_target_direct_latency_drill",
        }
        payload["target_bucket_selection"] = dict(payload["posterior_target_bucket_selection"])
        payload["target_realization_capital_plan"] = {
            "ok": True,
            "version": "posterior_target_direct_latency_drill",
            "target_bucket": target,
            "target_posterior": target_prob,
            "temperature_path_trade_follow_allowed": True,
            "temperature_path_trade_follow_reason": "posterior_target_direct_latency_drill",
            "buy_new_leg": {
                "outcome": target,
                "side": "YES",
                "target_realization_execution_action": "BUY",
                "leg_enabled": True,
            },
        }
        target_row = _row_for_label(payload, target)
        old_row = _row_for_label(payload, old)
        target_tokens = _ensure_market_token(
            conn,
            slug=slug,
            event_date=event_date,
            row=target_row,
            label=target,
            updated_at=now_iso,
        )
        old_tokens = _ensure_market_token(
            conn,
            slug=slug,
            event_date=event_date,
            row=old_row,
            label=old,
            updated_at=now_iso,
        )
        if target_row:
            target_row["yes_token"] = target_tokens["yes_token"]
            target_row["no_token"] = target_tokens["no_token"]
        if old_row:
            old_row["yes_token"] = old_tokens["yes_token"]
            old_row["no_token"] = old_tokens["no_token"]
        conn.execute(
            "DELETE FROM auto_trade_jobs WHERE source = 'posterior_target_direct'"
        )
        conn.execute(
            """
            DELETE FROM auto_trade_decisions
            WHERE slug = ? AND event_date = ?
              AND (reason_codes_json LIKE '%posterior_target_direct%'
                   OR details_json LIKE '%posterior_target_direct%')
            """,
            (slug, event_date),
        )
        conn.execute(
            """
            UPDATE edge_trades
            SET state = 'exit', position_size = 0, updated_at = ?
            WHERE slug = ? AND substr(event_date, 1, 10) = ? AND outcome = ? AND side = 'YES'
            """,
            (now_iso, slug, event_date, target),
        )
        conn.execute(
            """
            UPDATE edge_trades
            SET state = 'exit', position_size = 0, updated_at = ?
            WHERE slug = ? AND substr(event_date, 1, 10) = ? AND outcome != ? AND side = 'YES'
            """,
            (now_iso, slug, event_date, old),
        )
        updated_old = conn.execute(
            """
            UPDATE edge_trades
            SET state = 'entry',
                position_size = 8.0,
                avg_price = 0.25,
                last_order_id = 'posterior-drill-old-order',
                last_action = 'posterior_target_direct_latency_drill_seed_old',
                last_status = 'posterior_target_direct_latency_drill',
                updated_at = ?
            WHERE slug = ? AND substr(event_date, 1, 10) = ? AND outcome = ? AND side = 'YES'
            """,
            (now_iso, slug, event_date, old),
        ).rowcount
        if not updated_old:
            _insert_or_replace_by_available_columns(
                conn,
                "edge_trades",
                {
                    "slug": slug,
                    "event_date": event_date,
                    "outcome": old,
                    "side": "YES",
                    "state": "entry",
                    "entry_at": now_iso,
                    "last_order_id": "posterior-drill-old-order",
                    "entry_price": 0.25,
                    "entry_size": 8.0,
                    "last_action": "posterior_target_direct_latency_drill_seed_old",
                    "last_action_price": 0.25,
                    "last_action_size": 8.0,
                    "last_action_order_type": "GTC",
                    "last_action_at": now_iso,
                    "last_status": "posterior_target_direct_latency_drill",
                    "position_size": 8.0,
                    "avg_price": 0.25,
                    "updated_at": now_iso,
                },
            )
        _insert_market_tick(
            conn,
            slug=slug,
            event_date=event_date,
            outcome=target,
            side="YES",
            best_bid=max(0.0, min(1.0, float(target_ask) - 0.002)),
            best_ask=float(target_ask),
            created_at=now_iso,
        )
        _insert_market_tick(
            conn,
            slug=slug,
            event_date=event_date,
            outcome=old,
            side="YES",
            best_bid=float(old_bid),
            best_ask=min(1.0, float(old_bid) + 0.03),
            created_at=now_iso,
        )
        _insert_previous_posterior_decision(
            conn,
            slug=slug,
            event_date=event_date,
            previous_bucket=old,
            previous_probability=previous_probability,
            created_at=now_iso,
        )
        conn.execute(
            """
            UPDATE event_fact_history
            SET fact_json = ?, fact_as_of = ?, created_at = ?
            WHERE id = ?
            """,
            (json.dumps(payload, ensure_ascii=False), now_iso, now_iso, latest["id"]),
        )
        conn.execute(
            """
            UPDATE event_fact_latest
            SET updated_at = ?
            WHERE slug = ? AND event_date = ?
            """,
            (now_iso, slug, event_date),
        )
        conn.commit()
        return {
            "ok": True,
            "fact_id": int(latest["id"]),
            "fact_as_of": now_iso,
            "target_bucket": target,
            "target_probability": target_prob,
            "old_bucket": old,
            "previous_probability": previous_probability,
            "target_yes_token": target_tokens["yes_token"],
            "old_yes_token": old_tokens["yes_token"],
            "target_ask": float(target_ask),
            "old_bid": float(old_bid),
        }
    finally:
        conn.close()


class _PatchGuard:
    def __init__(self, obj: Any, updates: Mapping[str, Any]):
        self.obj = obj
        self.updates = dict(updates)
        self.previous: Dict[str, Any] = {}

    def __enter__(self):
        for key, value in self.updates.items():
            self.previous[key] = getattr(self.obj, key)
            setattr(self.obj, key, value)
        return self

    def __exit__(self, exc_type, exc, tb):
        for key, value in self.previous.items():
            setattr(self.obj, key, value)
        return False


def _run_fake_live_worker(
    db_path: Path,
    *,
    target_token: str,
    old_token: str,
) -> Tuple[Dict[str, Any], list[Dict[str, Any]]]:
    import scan_weather_market as swm

    placed: list[Dict[str, Any]] = []

    def fake_run_node_order(
        cmd: str,
        token_id: str = "",
        side: str = "",
        *,
        price: Any = None,
        size: Any = None,
        order_type: Any = None,
        amount: Any = None,
    ):
        placed.append(
            {
                "cmd": cmd,
                "token_id": token_id,
                "side": side,
                "price": price,
                "size": size,
                "order_type": order_type,
                "amount": amount,
            }
        )
        if cmd == "cancel-orders":
            return True, {"status": "cancelled", "count": 1}
        if side == "BUY" and token_id == target_token:
            return True, {"orderID": "posterior-drill-buy-target", "status": "submitted"}
        if side == "SELL" and token_id == old_token:
            return True, {"orderID": "posterior-drill-sell-old", "status": "submitted"}
        return True, {"orderID": f"posterior-drill-{side.lower()}-{token_id}", "status": "submitted"}

    def fake_run_node_get_order(order_id: str):
        if order_id == "posterior-drill-buy-target":
            buy = next(
                (
                    item
                    for item in placed
                    if item["cmd"] == "place-order"
                    and item["side"] == "BUY"
                    and item["token_id"] == target_token
                ),
                {},
            )
            return True, {
                "order": {
                    "status": "confirmed",
                    "size_matched": str(buy.get("size") or "8"),
                    "price": str(buy.get("price") or "0.999"),
                    "side": "BUY",
                }
            }
        if order_id == "posterior-drill-sell-old":
            return True, {
                "order": {
                    "status": "confirmed",
                    "size_matched": "8.0",
                    "price": "0.39",
                    "side": "SELL",
                }
            }
        return True, {"order": {"status": "confirmed", "price": "0.40", "side": "BUY"}}

    def fake_run_node_balance(token_id: str = "", refresh: bool = True, asset_type: str = "CONDITIONAL"):
        if str(asset_type or "").upper() == "COLLATERAL":
            return True, {
                "balance": "100000000",
                "allowance": "100000000",
                "balance_allowance": "100000000",
            }
        return True, {
            "balance": "8000000",
            "allowance": "8000000",
            "balance_allowance": "8000000",
        }

    data_dir = db_path.parent
    with _PatchGuard(
        swm,
        {
            "DB_PATH": db_path,
            "DATA_DIR": data_dir,
            "POSITIONS_SYNC_ENABLED": False,
            "POSITIONS_ASSUME_ZERO": False,
            "_run_node_order": fake_run_node_order,
            "_run_node_get_order": fake_run_node_get_order,
            "_run_node_balance": fake_run_node_balance,
        },
    ):
        result = swm.run_auto_trade_job_worker_once()
    return result, placed


def _env_guard(values: Mapping[str, str]):
    class Guard:
        def __enter__(self):
            self.previous = {key: os.environ.get(key) for key in values}
            for key, value in values.items():
                os.environ[key] = str(value)
            return self

        def __exit__(self, exc_type, exc, tb):
            for key, value in self.previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            return False

    return Guard()


def run_drill(args: argparse.Namespace) -> Dict[str, Any]:
    db_path = Path(args.db)
    if not db_path.exists():
        return {"ok": False, "blockers": ["archive_db_missing"], "details": {"db": str(db_path)}}
    from scripts import hard_source_direct_acceptance_audit
    from scripts import hard_source_direct_copy_acceptance
    from scripts import hard_source_direct_poller

    tmp_ctx = None
    if args.keep_tmp:
        tmp_dir = tempfile.mkdtemp(prefix="posterior-target-drill-")
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="posterior-target-drill-")
        tmp_dir = tmp_ctx.__enter__()
    try:
        tmp_db = Path(tmp_dir) / "archive.db"
        subset = hard_source_direct_copy_acceptance._copy_event_subset(
            db_path,
            tmp_db,
            slug=args.slug or "",
            event_date=args.event_date or "",
            tick_limit=int(args.tick_limit),
        )
        event_slug = str(args.slug or subset.get("resolved_slug") or "").strip()
        event_date = str(args.event_date or subset.get("resolved_event_date") or "").strip()
        prepare = _prepare_posterior_target_scenario(
            tmp_db,
            slug=event_slug,
            event_date=event_date,
            target_bucket=str(args.target_bucket or ""),
            old_bucket=str(args.old_bucket or ""),
            target_probability=float(args.target_probability),
            previous_probability=float(args.previous_probability),
            target_ask=float(args.target_ask),
            old_bid=float(args.old_bid),
        )
        if not prepare.get("ok"):
            return {
                "ok": False,
                "blockers": ["scenario_prepare_failed"],
                "mode": "temporary_db_copy",
                "source_db": str(db_path),
                "tmp_db": str(tmp_db),
                "tmp_db_removed": not args.keep_tmp,
                "subset_copy": subset,
                "prepare": prepare,
            }
        env = {
            "PM_AUTO_TRADE_ENABLED": "1",
            "PM_AUTO_TRADE_DRY_RUN": "0",
            "PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED": "0",
            "PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA": "0.001",
            "PM_POSTERIOR_TARGET_DIRECT_WILL_ORDER": "1",
            "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
            "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
            "PM_AUTO_TRADE_FACTS_LOCK_WAIT_SEC": "0",
            "PM_AUTO_TRADE_DB_LOCK_WAIT_SEC": "0",
            "PM_AUTO_TRADE_SYNC_TRADES": "0",
            "PM_AUTO_TRADE_SYNC_OPEN_ORDERS": "0",
            "PM_AUTO_TRADE_EVENT_WORKER_SYNC_TRADES": "0",
            "PM_AUTO_TRADE_EVENT_WORKER_SYNC_OPEN_ORDERS": "0",
            "PM_POSITIONS_SYNC": "0",
            "PM_POSITIONS_ASSUME_ZERO": "0",
            "PM_AUTO_TRADE_CONFIRM_ENTRY_MIN_AGE_SEC": "0",
            "PM_AUTO_TRADE_RULES_LIVE_HYDRATE": "0",
            "PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS": "0",
            "PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS": "0",
            "PM_AUTO_TRADE_ENTRY_REFRESH_ON_STALE": "0",
            "PM_AUTO_TRADE_EXIT_REFRESH_ON_STALE": "0",
            "PM_AUTO_TRADE_MARKET_EXIT_PROBE_REFRESH_OPEN_ORDERS": "0",
            "PM_AUTO_TRADE_CONFIRM_NOT_FOUND_RECHECK_DELAY_SEC": "0",
            "PM_AUTO_TRADE_CONFIRM_NOT_FOUND_REFRESH_OPEN_ORDERS": "0",
            "PM_AUTO_TRADE_MIN_EXIT_SIZE": "1",
            "PM_AUTO_TRADE_EXIT_DUST_MIN": "0.01",
            "PM_MIN_ORDER_SIZE": "1",
            "PM_AUTO_TRADE_EXIT_ALLOW_SMALL": "1",
            "PM_AUTO_TRADE_BLOCK_POST_PEAK_NEAR_CERTAIN_ENTRY": "1",
            "PM_AUTO_TRADE_POST_PEAK_NEAR_CERTAIN_PRICE_MIN": "0.99",
            "PM_POSTERIOR_TARGET_DIRECT_BYPASS_POST_PEAK_PRICE_GUARD": "1",
        }
        with _env_guard(env):
            started = time.perf_counter()
            record = hard_source_direct_poller.record_posterior_target_direct_decisions(
                tmp_db,
                event_date=event_date,
                slug=event_slug,
                emit_unchanged_sec=0,
                enqueue_job=True,
            )
            record_wall_ms = (time.perf_counter() - started) * 1000.0
            worker, placed = _run_fake_live_worker(
                tmp_db,
                target_token=str(prepare.get("target_yes_token") or ""),
                old_token=str(prepare.get("old_yes_token") or ""),
            )
            audit = hard_source_direct_acceptance_audit.audit(
                tmp_db,
                mode="posterior_target",
                slug=event_slug,
                event_date=event_date,
                max_job_enqueue_to_worker_ms=float(args.max_job_enqueue_to_worker_ms),
                max_job_enqueue_to_order_ms=float(args.max_job_enqueue_to_order_ms),
                max_fact_to_order_ms=float(args.max_fact_to_order_ms),
            )
        buy_new = [
            item
            for item in placed
            if item.get("cmd") == "place-order"
            and item.get("side") == "BUY"
            and item.get("token_id") == prepare.get("target_yes_token")
        ]
        sell_old = [
            item
            for item in placed
            if item.get("cmd") == "place-order"
            and item.get("side") == "SELL"
            and item.get("token_id") == prepare.get("old_yes_token")
        ]
        blockers: list[str] = []
        if not record.get("ok") or not record.get("job_id"):
            blockers.append("posterior_target_job_not_enqueued")
        if not worker.get("ok") or not worker.get("picked"):
            blockers.append("worker_did_not_pick_job")
        if not buy_new:
            blockers.append("buy_new_order_missing")
        audit_details = audit.get("details") if isinstance(audit.get("details"), dict) else {}
        old_exit_required = bool(audit_details.get("old_bucket_exit_required"))
        if old_exit_required and not sell_old:
            blockers.append("sell_old_order_missing")
        if not audit.get("ok"):
            blockers.append("acceptance_audit_failed")
        return {
            "ok": not blockers,
            "blockers": blockers,
            "mode": "posterior_target_direct_temporary_db_fake_live",
            "source_db": str(db_path),
            "tmp_db": str(tmp_db),
            "tmp_db_removed": not args.keep_tmp,
            "subset_copy": subset,
            "prepare": prepare,
            "record": record,
            "record_wall_ms": round(record_wall_ms, 3),
            "worker": worker,
            "placed_orders": placed,
            "buy_new_orders": buy_new,
            "sell_old_orders": sell_old,
            "acceptance_audit": audit,
        }
    finally:
        if tmp_ctx is not None:
            tmp_ctx.__exit__(None, None, None)


def _compact_for_print(value: Any, *, max_string: int = 1200) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if key in {"payload_json", "fact_json", "details_json"} and isinstance(item, str):
                out[key] = f"<omitted {len(item)} chars>"
                continue
            out[key] = _compact_for_print(item, max_string=max_string)
        return out
    if isinstance(value, list):
        return [_compact_for_print(item, max_string=max_string) for item in value]
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string] + f"...<truncated {len(value) - max_string} chars>"
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--target-bucket", default="")
    parser.add_argument("--old-bucket", default="")
    parser.add_argument("--target-probability", type=float, default=1.0)
    parser.add_argument("--previous-probability", type=float, default=0.42)
    parser.add_argument("--target-ask", type=float, default=0.999)
    parser.add_argument("--old-bid", type=float, default=0.39)
    parser.add_argument("--tick-limit", type=int, default=5000)
    parser.add_argument("--max-job-enqueue-to-worker-ms", type=float, default=3000.0)
    parser.add_argument("--max-job-enqueue-to-order-ms", type=float, default=3000.0)
    parser.add_argument("--max-fact-to-order-ms", type=float, default=3000.0)
    parser.add_argument("--keep-tmp", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_drill(args)
    print(json.dumps(_compact_for_print(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
