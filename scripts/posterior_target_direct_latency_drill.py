#!/usr/bin/env python3
"""Run a posterior-target direct order latency drill on a temporary archive.db copy."""

from __future__ import annotations

import argparse
import copy
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

import weather_fact_snapshot_writer
from lineage_identity_runtime import build_correlation_id
from source_observation_runtime import SOURCE_CLASSES, build_source_observation

DEFAULT_DB = ROOT / "data" / "archive.db"
DRILL_FACT_COMPOSER_VERSION = "posterior_target_direct_latency_drill_fact_writer_v1"
TEMPORARY_DRILL_DB_BOUNDARY_SCHEMA = "posterior_target_direct_latency_drill_boundary_v1"


def build_posterior_target_direct_latency_drill_boundary(
    *,
    source_db: str = "",
    tmp_db: str = "",
) -> Dict[str, Any]:
    return {
        "schema": TEMPORARY_DRILL_DB_BOUNDARY_SCHEMA,
        "read_only_for_source_db": True,
        "dry_run_for_live_system": True,
        "execution_mode": "temporary_db_copy_fake_live_drill_not_production_writer",
        "measurement_scope": "synthetic_fact_signal_to_order_local_path_only",
        "valid_p10_t1_to_t5_evidence": False,
        "real_amsc_t1_or_p10_telemetry_consumed": False,
        "source_db": str(source_db or ""),
        "tmp_db": str(tmp_db or ""),
        "fact_writer_integration": {
            "status": "uses_weather_fact_snapshot_writer_apply_on_temporary_db_copy",
            "write_owner": weather_fact_snapshot_writer.WRITE_OWNER,
            "composer_version": DRILL_FACT_COMPOSER_VERSION,
            "live_fact_apply": False,
        },
        "guards": {
            "writes_archive_db": False,
            "writes_temporary_archive_db_copy": True,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_event_fact_summary_cache": False,
            "writes_auto_trade_decisions": False,
            "writes_auto_trade_jobs": False,
            "writes_edge_trades": False,
            "writes_temporary_event_fact_history": True,
            "writes_temporary_event_fact_latest": True,
            "writes_temporary_event_fact_summary_cache": True,
            "writes_temporary_auto_trade_decisions": True,
            "writes_temporary_auto_trade_jobs": True,
            "writes_temporary_edge_trades": True,
            "places_orders": False,
            "uses_fake_order_runner": True,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "executes_sql_against_source_db": False,
        },
        "blocked_live_operations": [
            "append_live_event_fact_history",
            "repoint_live_event_fact_latest",
            "write_live_event_fact_summary_cache",
            "write_live_auto_trade_decisions",
            "write_live_auto_trade_jobs",
            "write_live_edge_trades",
            "place_real_order",
            "restart_launchd",
            "historical_backfill",
        ],
    }


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
    posterior_ssot = payload.get("posterior_ssot_fact")
    if isinstance(posterior_ssot, Mapping):
        label = str(
            posterior_ssot.get("target_bucket")
            or posterior_ssot.get("top_bucket")
            or posterior_ssot.get("selected_bucket")
            or ""
        ).strip()
        if label:
            return label
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
                    "decision_ssot": "posterior_ssot_fact",
                    "posterior_ssot_fact_used": True,
                    "target_probability_field": "posterior_ssot_fact.target_probability",
                    "drill_previous_state": True,
                }
            ),
            "created_at": previous_at or created_at,
        },
    )


def _drill_fact_source_trace(
    *,
    slug: str,
    event_date: str,
    fact_as_of: str,
    current_fact_id: int,
) -> Dict[str, Any]:
    source_id = "posterior_target_direct_latency_drill"
    raw_sha = weather_fact_snapshot_writer.sha256_json(
        {
            "source_id": source_id,
            "slug": slug,
            "event_date": event_date,
            "fact_as_of": fact_as_of,
            "current_fact_id": current_fact_id,
        }
    )
    source_payload = {
        "source_payload_schema": "posterior_target_direct_latency_drill_source_payload_v1",
        "slug": slug,
        "event_date": event_date,
        "fact_as_of": fact_as_of,
        "current_fact_id": current_fact_id,
        "diagnostic_only": True,
        "temporary_db_copy": True,
    }
    observation_obj = build_source_observation(
        source_id=source_id,
        station_id="ZUUU",
        event_date=event_date,
        valid_time=fact_as_of,
        fetched_at=fact_as_of,
        response_completed_at=fact_as_of,
        raw_ref=f"posterior_target_direct_latency_drill:{current_fact_id}",
        raw_payload=source_payload,
        parser_version="posterior_target_direct_latency_drill_source_trace_v1",
        overrides={
            "source_class": SOURCE_CLASSES["diagnostic_current"],
            "role": "posterior_target_direct_latency_drill_synthetic_fact",
            "hard_eligible": False,
            "demotion_reason": "temporary_drill_diagnostic_not_hard_source",
        },
        payload=source_payload,
    )
    observation = observation_obj.as_dict(include_payload=True)
    observation["diagnostic_only"] = True
    observation["temporary_db_copy"] = True
    source_event_id = observation_obj.source_event_id
    source_observation_id = observation_obj.source_observation_id
    return {
        "schema": "posterior_target_direct_latency_drill_source_trace_v1",
        "read_only_for_source_db": True,
        "temporary_db_copy": True,
        "correlation_id": build_correlation_id(
            "posterior_target_direct_latency_drill",
            slug,
            event_date,
            fact_as_of,
            current_fact_id,
        ),
        "source_observations": [observation],
        "source_event_ids": [source_event_id],
        "source_observation_ids": [source_observation_id],
        "source_latency_trace": [
            {
                "source_event_id": source_event_id,
                "source_observation_id": source_observation_id,
                "source_id": source_id,
                "source_class": SOURCE_CLASSES["diagnostic_current"],
                "role": "posterior_target_direct_latency_drill_synthetic_fact",
                "valid_time": fact_as_of,
                "fetched_at": fact_as_of,
                "response_duration_ms": 0,
                "raw_sha256": raw_sha,
                "diagnostic_only": True,
            }
        ],
        "hard_observation_candidate_ids": [],
        "diagnostic_current_ids": [source_event_id],
        "forecast_weak_ids": [],
        "next_boundary": "weather_fact_snapshot_writer_apply_on_temporary_drill_db_only",
    }


def build_posterior_target_drill_fact_snapshot_writer_apply_plan(
    *,
    slug: str,
    event_date: str,
    fact_as_of: str,
    incoming_payload: Mapping[str, Any],
    current_payload: Mapping[str, Any],
    current_fact_id: int,
    current_fact_as_of: str = "",
) -> Dict[str, Any]:
    source_trace = _drill_fact_source_trace(
        slug=slug,
        event_date=event_date,
        fact_as_of=fact_as_of,
        current_fact_id=current_fact_id,
    )
    return weather_fact_snapshot_writer.build_fact_snapshot_write_plan(
        slug=slug,
        event_date=event_date,
        run_date=event_date,
        fact_as_of=fact_as_of,
        incoming_payload=incoming_payload,
        current_fact_id=current_fact_id,
        current_fact_as_of=current_fact_as_of,
        current_payload=current_payload,
        source_acquisition_trace=source_trace,
        composer_version=DRILL_FACT_COMPOSER_VERSION,
        write_reason="posterior_target_direct_latency_drill_temporary_db_fact_prepare",
        include_payload=True,
        enable_apply_contract=True,
        write_latest=True,
    )


def _apply_posterior_target_drill_fact_payload(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    fact_as_of: str,
    payload: Mapping[str, Any],
    current_payload: Mapping[str, Any],
    current_fact_id: int,
    current_fact_as_of: str = "",
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    plan = build_posterior_target_drill_fact_snapshot_writer_apply_plan(
        slug=slug,
        event_date=event_date,
        fact_as_of=fact_as_of,
        incoming_payload=payload,
        current_payload=current_payload,
        current_fact_id=current_fact_id,
        current_fact_as_of=current_fact_as_of,
    )
    result = weather_fact_snapshot_writer.apply_fact_snapshot_write_plan(
        conn,
        plan,
        allow_apply=True,
        commit=False,
        created_at=fact_as_of,
    )
    result["writer_apply_result_summary"] = (
        weather_fact_snapshot_writer.compact_fact_snapshot_writer_apply_result_for_lineage(result)
    )
    return plan, result


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
            SELECT h.id, h.fact_as_of, h.fact_json
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
        current_payload = _json_loads(latest["fact_json"])
        if not current_payload:
            return {"ok": False, "reason": "latest_fact_json_invalid"}
        payload = copy.deepcopy(current_payload)
        previous_fact_id = int(latest["id"])
        previous_fact_as_of = str(latest["fact_as_of"] or current_payload.get("fact_as_of") or "")
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
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "target_bucket_probability_field": "posterior_ssot_fact.target_probability",
            "target_bucket_source": "posterior_ssot_fact.top_bucket",
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            "target_bucket_source_semantics": "bayes_posterior_ssot_fact_v1",
            "target_selection_priority_version": "bayes_posterior_ssot_fact_v1",
            "selection_priority": "posterior_ssot_fact_top_bucket",
            "ssot_source": "posterior_ssot_fact",
            "source": "posterior_ssot_fact",
            "selection_runtime_recomputed": False,
        }
        payload["target_bucket_selection"] = dict(payload["posterior_target_bucket_selection"])
        # And the SSOT block, which is the decision authority the two
        # selections above both say they read.  The drill moved the rows and
        # the selections but left whatever block the source fact carried, so
        # the writer refused the staged scenario on a target mismatch -- or,
        # when the source fact had no block at all, on a dozen
        # posterior_ssot_fact_* issues.  Built by the writer's own builder
        # rather than assembled here, so the staged payload and a production
        # one cannot drift apart.
        payload["posterior_temperature_distribution"] = [
            {
                "bucket": str(row.get("outcome") or row.get("label") or "").strip(),
                "probability": float(row.get("q_temperature_bucket_posterior") or 0.0),
            }
            for row in rows
            if str(row.get("outcome") or row.get("label") or "").strip()
        ]
        payload["posterior_temperature_distribution_map"] = {
            str(item["bucket"]): float(item["probability"])
            for item in payload["posterior_temperature_distribution"]
        }
        payload.setdefault("production_model_version", "joint_weak_physical_strong_composer_v1")
        payload.setdefault("production_model_ready", True)
        payload["posterior_ssot_fact"] = weather_fact_snapshot_writer.build_posterior_ssot_fact(
            payload
        )
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
        payload["posterior_target_direct_latency_drill_state"] = {
            "schema": "posterior_target_direct_latency_drill_state_v1",
            "temporary_db_copy": True,
            "source_fact_id": previous_fact_id,
            "source_fact_as_of": previous_fact_as_of,
            "target_bucket": target,
            "old_bucket": old,
            "target_probability": target_prob,
            "previous_probability": previous_probability,
            "live_archive_write": False,
            "order_runner": "fake",
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
        writer_plan, writer_apply = _apply_posterior_target_drill_fact_payload(
            conn,
            slug=slug,
            event_date=event_date,
            fact_as_of=now_iso,
            payload=payload,
            current_payload=current_payload,
            current_fact_id=previous_fact_id,
            current_fact_as_of=previous_fact_as_of,
        )
        if not writer_apply.get("ok") or not writer_apply.get("latest_repointed"):
            conn.rollback()
            return {
                "ok": False,
                "reason": "fact_writer_apply_failed",
                "previous_fact_id": previous_fact_id,
                "writer_plan_schema": writer_plan.get("schema"),
                "writer_plan_write_owner": writer_plan.get("write_owner"),
                "writer_plan_apply_supported": writer_plan.get("apply_supported"),
                "writer_plan_fact_snapshot_id": writer_plan.get("fact_snapshot_id"),
                "writer_apply": writer_apply,
            }
        conn.commit()
        return {
            "ok": True,
            "fact_id": int(writer_apply.get("fact_id") or 0),
            "previous_fact_id": previous_fact_id,
            "fact_as_of": now_iso,
            "write_owner": writer_apply.get("write_owner"),
            "fact_snapshot_id": writer_apply.get("fact_snapshot_id"),
            "latest_repointed": bool(writer_apply.get("latest_repointed")),
            "summary_cache_written": bool(writer_apply.get("summary_cache_written")),
            "writer_plan_schema": writer_plan.get("schema"),
            "writer_apply_result_schema": writer_apply.get("schema"),
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
        order_context: Any = None,
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
    # A diagnostic command must be safe when invoked without an explicit
    # execute switch.  The executable drill only ever mutates a temporary
    # copy, but keeping the default audit-only prevents accidental expensive
    # copy/worker runs from being mistaken for production remediation.
    if not bool(getattr(args, "execute_temporary_drill", False)):
        return {
            "ok": True,
            "state": "audit_only_no_temporary_apply",
            "mode": "temporary_db_copy_fake_live_drill_not_production_writer",
            "measurement_scope": "synthetic_fact_signal_to_order_local_path_only",
            "valid_p10_t1_to_t5_evidence": False,
            "boundary": build_posterior_target_direct_latency_drill_boundary(source_db=str(args.db)),
            "next_action": "rerun_with_--execute-temporary-drill_to_run_the_isolated_fake_order_drill",
        }
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
            "mode": "posterior_target_direct_temporary_db_synthetic_signal_only",
            "measurement_scope": "synthetic_fact_signal_to_order_local_path_only",
            "valid_p10_t1_to_t5_evidence": False,
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
    parser.add_argument(
        "--execute-temporary-drill",
        action="store_true",
        help="Run the synthetic drill against a temporary archive.db copy; default is audit-only and performs no copy or writes.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_drill(args)
    print(json.dumps(_compact_for_print(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
