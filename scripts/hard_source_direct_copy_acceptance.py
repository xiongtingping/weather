#!/usr/bin/env python3
"""Run hard-source direct SSOT acceptance on a temporary archive.db copy."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from edge_trade_ledger_writer import (
    copy_acceptance_clear_target,
    copy_acceptance_seed_old,
    insert_edge_trade_row,
)

DEFAULT_DB = ROOT / "data" / "archive.db"
COPY_ACCEPTANCE_COMPOSER_VERSION = "hard_source_direct_copy_acceptance_fixture_v1"


def _copy_sqlite_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            dst_conn = sqlite3.connect(dst)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    except sqlite3.Error:
        shutil.copy2(src, dst)


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return []
    return [str(row[1]) for row in rows]


def _copy_rows(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: Sequence[Any],
    *,
    order_sql: str = "",
    limit: Optional[int] = None,
) -> int:
    src_cols = _table_columns(src, table)
    dst_cols = _table_columns(dst, table)
    cols = [col for col in src_cols if col in dst_cols]
    if not cols:
        return 0
    quoted = ", ".join(cols)
    sql = f"SELECT {quoted} FROM {table} WHERE {where_sql}"
    if order_sql:
        sql += f" ORDER BY {order_sql}"
    if limit is not None and int(limit) > 0:
        sql += f" LIMIT {int(limit)}"
    rows = src.execute(sql, tuple(params)).fetchall()
    if not rows:
        return 0
    placeholders = ", ".join(["?"] * len(cols))
    insert_sql = f"INSERT OR REPLACE INTO {table} ({quoted}) VALUES ({placeholders})"
    dst.executemany(insert_sql, [tuple(row[col] for col in cols) for row in rows])
    return len(rows)


def _copy_schema(src: sqlite3.Connection, dst: sqlite3.Connection) -> int:
    rows = src.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE sql IS NOT NULL
          AND name NOT LIKE 'sqlite_%'
        ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 1 WHEN 'index' THEN 2 ELSE 3 END, name
        """
    ).fetchall()
    count = 0
    for row in rows:
        sql = str(row["sql"] or "").strip()
        if not sql:
            continue
        try:
            dst.execute(sql)
            count += 1
        except sqlite3.Error:
            continue
    dst.commit()
    return count


def _resolve_event(conn: sqlite3.Connection, slug: str, event_date: str) -> tuple[str, str]:
    if slug and event_date:
        return slug, event_date
    clauses = ["h.slug LIKE 'highest-temperature-in-chengdu-%'"]
    params: list[Any] = []
    if slug:
        clauses.append("h.slug = ?")
        params.append(slug)
    if event_date:
        clauses.append("h.event_date = ?")
        params.append(event_date)
    row = conn.execute(
        f"""
        SELECT h.slug, h.event_date
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE {' AND '.join(clauses)}
        ORDER BY h.fact_as_of DESC, h.id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        return slug, event_date
    return str(row["slug"] or slug), str(row["event_date"] or event_date)


def _copy_event_subset(src_db: Path, dst_db: Path, *, slug: str, event_date: str, tick_limit: int) -> Dict[str, Any]:
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(dst_db)
    dst.row_factory = sqlite3.Row
    copied: Dict[str, Any] = {}
    try:
        copied["schema_objects"] = _copy_schema(src, dst)
        slug, event_date = _resolve_event(src, slug, event_date)
        copied["resolved_slug"] = slug
        copied["resolved_event_date"] = event_date
        fact = src.execute(
            """
            SELECT h.id
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE h.slug = ? AND h.event_date = ?
            ORDER BY h.fact_as_of DESC, h.id DESC
            LIMIT 1
            """,
            (slug, event_date),
        ).fetchone()
        if fact is not None:
            copied["event_fact_history"] = _copy_rows(
                src,
                dst,
                "event_fact_history",
                "id = ?",
                (fact["id"],),
            )
            copied["event_fact_latest"] = _copy_rows(
                src,
                dst,
                "event_fact_latest",
                "slug = ? AND event_date = ?",
                (slug, event_date),
            )
        else:
            copied["event_fact_history"] = 0
            copied["event_fact_latest"] = 0
        copied["market_tokens"] = _copy_rows(
            src,
            dst,
            "market_tokens",
            "slug = ? AND event_date = ?",
            (slug, event_date),
        )
        copied["edge_trades"] = _copy_rows(
            src,
            dst,
            "edge_trades",
            "slug = ? AND substr(event_date, 1, 10) = ?",
            (slug, event_date),
        )
        copied["event_market_ticks"] = _copy_rows(
            src,
            dst,
            "event_market_ticks",
            "slug = ? AND event_date = ?",
            (slug, event_date),
            order_sql="created_at DESC, id DESC",
            limit=max(100, int(tick_limit)),
        )
        for table in ("edge_trade_fills", "auto_orders"):
            if _table_columns(src, table) and _table_columns(dst, table):
                copied[table] = _copy_rows(
                    src,
                    dst,
                    table,
                    "slug = ? AND substr(event_date, 1, 10) = ?",
                    (slug, event_date),
                )
        dst.commit()
        return copied
    finally:
        src.close()
        dst.close()


def _override_env(values: Dict[str, str]):
    class _EnvGuard:
        def __enter__(self):
            self.previous = {key: os.environ.get(key) for key in values}
            for key, value in values.items():
                os.environ[key] = value
            return self

        def __exit__(self, exc_type, exc, tb):
            for key, value in self.previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            return False

    return _EnvGuard()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_loads(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(str(raw or ""))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _latest_fact_row(conn: sqlite3.Connection, *, slug: str, event_date: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT h.id,
               h.run_date,
               h.slug,
               h.event_date,
               h.fact_as_of,
               h.model_snapshot_id,
               h.market_snapshot_id,
               h.composer_version,
               h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        ORDER BY h.id DESC
        LIMIT 1
        """,
        (slug, event_date),
    ).fetchone()


def _features(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("weather_likelihood_features")
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            out = float(value)
        except (TypeError, ValueError):
            continue
        if out == out:
            return float(out)
    return None


def _copy_acceptance_fact_source_trace(
    payload: Mapping[str, Any],
    *,
    row: sqlite3.Row,
    fact_as_of: str,
    write_reason: str,
) -> Dict[str, Any]:
    from weather_source_acquisition_manager import build_source_acquisition_trace

    features = _features(payload)
    event_date = str(row["event_date"] or payload.get("event_date") or "")
    station_id = _first_text(payload.get("station_id"), features.get("station_id"), "ZUUU")
    temperature_c = _first_float(
        features.get("zuuu_metar_raw_awc_temperature_c"),
        features.get("metar_temperature_c"),
        features.get("observed_today_max_c"),
        payload.get("temperature_c"),
    )
    observed_today_max_c = _first_float(
        features.get("zuuu_metar_raw_awc_today_max_c"),
        features.get("observed_today_max_c"),
        payload.get("observed_today_max_c"),
    )
    valid_time = _first_text(
        features.get("zuuu_metar_raw_awc_today_max_at"),
        features.get("zuuu_metar_raw_awc_latest_at"),
        payload.get("valid_time"),
        fact_as_of,
    )
    return build_source_acquisition_trace(
        sources=[
            {
                "source_id": "zuuu_metar_raw_awc",
                "station_id": station_id,
                "event_date": event_date,
                "valid_time": valid_time,
                "fetched_at": fact_as_of,
                "response_completed_at": fact_as_of,
                "temperature_c": temperature_c,
                "observed_today_max_c": observed_today_max_c,
                "raw_ref": f"hard_source_direct_copy_acceptance:{write_reason}:event_fact_history:{row['id']}",
                "raw_payload": {
                    "source_payload_schema": "hard_source_direct_copy_acceptance_fixture_source_v1",
                    "fixture_only": True,
                    "temporary_db_copy": True,
                    "write_reason": write_reason,
                    "parent_fact_id": row["id"],
                    "parent_fact_as_of": row["fact_as_of"],
                },
            }
        ],
        station_id=station_id,
        event_date=event_date,
        fetched_at=fact_as_of,
        include_payload=False,
    )


def build_copy_acceptance_fact_snapshot_writer_apply_plan(
    row: sqlite3.Row,
    *,
    payload: Mapping[str, Any],
    fact_as_of: str,
    write_reason: str,
) -> Dict[str, Any]:
    from weather_fact_snapshot_writer import build_fact_snapshot_write_plan

    slug = str(row["slug"] or payload.get("slug") or "")
    event_date = str(row["event_date"] or payload.get("event_date") or "")
    run_date = str(row["run_date"] or payload.get("run_date") or event_date)
    incoming = dict(payload)
    incoming.setdefault("slug", slug)
    incoming.setdefault("event_date", event_date)
    incoming.setdefault("run_date", run_date)
    incoming["fact_as_of"] = fact_as_of
    incoming["composer_version"] = COPY_ACCEPTANCE_COMPOSER_VERSION
    incoming["copy_acceptance_fixture_state"] = {
        "schema": "copy_acceptance_fixture_state_v1",
        "write_owner": "weather_fact_snapshot_writer",
        "write_reason": write_reason,
        "temporary_db_copy": True,
        "source_script": "scripts/hard_source_direct_copy_acceptance.py",
        "parent_fact_id": row["id"],
        "parent_fact_as_of": row["fact_as_of"],
        "created_at_represents_write_time_only": True,
        "historical_backfill": False,
    }
    source_trace = _copy_acceptance_fact_source_trace(
        incoming,
        row=row,
        fact_as_of=fact_as_of,
        write_reason=write_reason,
    )
    plan = build_fact_snapshot_write_plan(
        slug=slug,
        event_date=event_date,
        run_date=run_date,
        fact_as_of=fact_as_of,
        incoming_payload=incoming,
        current_fact_id=int(row["id"] or 0),
        current_fact_as_of=str(row["fact_as_of"] or ""),
        current_payload=_json_loads(row["fact_json"]),
        source_acquisition_trace=source_trace,
        composer_version=COPY_ACCEPTANCE_COMPOSER_VERSION,
        write_reason=write_reason,
        include_payload=True,
        enable_apply_contract=True,
        write_latest=True,
    )
    plan["caller"] = {
        "module": "scripts/hard_source_direct_copy_acceptance.py",
        "function": "build_copy_acceptance_fact_snapshot_writer_apply_plan",
        "lane": "materializer_rebuild_backfill",
        "temporary_db_copy": True,
    }
    plan["guards"] = {
        "writes_archive_db": False,
        "writes_temporary_archive_db_copy": True,
        "places_orders": False,
        "historical_backfill": False,
        "restarts_launchd": False,
        "executes_sql": False,
    }
    return plan


def _apply_copy_acceptance_fact_payload(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    fact_as_of: str,
    write_reason: str,
) -> Dict[str, Any]:
    from weather_fact_snapshot_writer import (
        apply_fact_snapshot_write_plan,
        compact_fact_snapshot_writer_apply_result_for_lineage,
    )

    plan = build_copy_acceptance_fact_snapshot_writer_apply_plan(
        row,
        payload=payload,
        fact_as_of=fact_as_of,
        write_reason=write_reason,
    )
    result = apply_fact_snapshot_write_plan(
        conn,
        plan,
        allow_apply=True,
        commit=True,
        created_at=fact_as_of,
    )
    result_summary = compact_fact_snapshot_writer_apply_result_for_lineage(result)
    return {
        **dict(result),
        "writer_plan_schema": plan.get("schema"),
        "writer_apply_result_summary": result_summary,
        "post_apply_persisted_verification_ok": bool(
            result_summary.get("post_apply_persisted_verification_ok")
        ),
        "write_reason": write_reason,
    }


def _freshen_hard_source_timestamps(db_path: Path, *, slug: str, event_date: str) -> Dict[str, Any]:
    now_iso = _utc_now_iso()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = _latest_fact_row(conn, slug=slug, event_date=event_date)
        if row is None:
            return {"ok": False, "reason": "latest_fact_missing"}
        payload = _json_loads(row["fact_json"])
        if not payload:
            return {"ok": False, "reason": "latest_fact_json_invalid"}
        features = payload.get("weather_likelihood_features")
        if not isinstance(features, dict):
            features = {}
            payload["weather_likelihood_features"] = features
        changed: list[str] = []
        for key in list(features.keys()):
            if key.endswith("_fetched_at") or key.endswith("_request_started_at"):
                features[key] = now_iso
                changed.append(key)
        for key in (
            "zuuu_metar_raw_awc_age_minutes",
            "wu_twc_observed_intraday_age_minutes",
        ):
            if key in features:
                features[key] = 0.0
                changed.append(key)
        apply_result = _apply_copy_acceptance_fact_payload(
            conn,
            row=row,
            payload=payload,
            fact_as_of=now_iso,
            write_reason="copy_acceptance_freshen_hard_source_timestamps",
        )
        if not apply_result.get("ok"):
            return {
                "ok": False,
                "reason": apply_result.get("block_reason") or apply_result.get("error") or "writer_apply_failed",
                "writer_apply_result": apply_result,
                "changed_keys": sorted(set(changed)),
            }
        return {
            "ok": True,
            "updated_at": now_iso,
            "changed_keys": sorted(set(changed)),
            "new_fact_id": apply_result.get("fact_id"),
            "write_owner": apply_result.get("write_owner"),
            "writer_apply_result_schema": apply_result.get("schema"),
            "latest_repointed": bool(apply_result.get("latest_repointed")),
        }
    finally:
        conn.close()


def _force_final_lock_scenario(db_path: Path, *, slug: str, event_date: str) -> Dict[str, Any]:
    now_iso = _utc_now_iso()
    forced_value_c = 33.0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = _latest_fact_row(conn, slug=slug, event_date=event_date)
        if row is None:
            return {"ok": False, "reason": "latest_fact_missing"}
        payload = _json_loads(row["fact_json"])
        if not payload:
            return {"ok": False, "reason": "latest_fact_json_invalid"}
        features = payload.get("weather_likelihood_features")
        if not isinstance(features, dict):
            features = {}
            payload["weather_likelihood_features"] = features
        features["hard_source_final_daily_max"] = True
        features["hard_source_final_daily_max_forced_acceptance"] = True
        features["zuuu_metar_raw_awc_today_max_c"] = forced_value_c
        features["zuuu_metar_raw_awc_today_max_at"] = now_iso
        features["zuuu_metar_raw_awc_latest_at"] = now_iso
        features["zuuu_metar_raw_awc_fetched_at"] = now_iso
        features["zuuu_metar_raw_awc_date_mismatch"] = False
        features["wu_twc_observed_intraday_today_max_c"] = forced_value_c
        features["wu_twc_observed_intraday_today_max_at"] = now_iso
        features["wu_twc_observed_intraday_current_at"] = now_iso
        features["wu_twc_observed_intraday_fetched_at"] = now_iso
        features["wu_twc_observed_intraday_stale"] = False
        apply_result = _apply_copy_acceptance_fact_payload(
            conn,
            row=row,
            payload=payload,
            fact_as_of=now_iso,
            write_reason="copy_acceptance_force_final_lock_scenario",
        )
        if not apply_result.get("ok"):
            return {
                "ok": False,
                "reason": apply_result.get("block_reason") or apply_result.get("error") or "writer_apply_failed",
                "writer_apply_result": apply_result,
                "forced_value_c": forced_value_c,
            }
        return {
            "ok": True,
            "updated_at": now_iso,
            "new_fact_id": apply_result.get("fact_id"),
            "write_owner": apply_result.get("write_owner"),
            "writer_apply_result_schema": apply_result.get("schema"),
            "latest_repointed": bool(apply_result.get("latest_repointed")),
            "forced_keys": [
                "hard_source_final_daily_max",
                "hard_source_final_daily_max_forced_acceptance",
                "zuuu_metar_raw_awc_today_max_c",
                "wu_twc_observed_intraday_today_max_c",
            ],
            "forced_value_c": forced_value_c,
        }
    finally:
        conn.close()


def _nearest_lower_bucket_from_latest_fact(
    db_path: Path,
    *,
    slug: str,
    event_date: str,
    target_bucket: str,
) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = _latest_fact_row(conn, slug=slug, event_date=event_date)
        if row is None:
            return ""
        payload = _json_loads(row["fact_json"])
        if not payload:
            return ""
        rows = payload.get("distribution") or payload.get("rows") or []
        labels = [
            str(item.get("outcome") or item.get("label") or "").strip()
            for item in rows
            if isinstance(item, dict)
        ]
        try:
            target_index = labels.index(str(target_bucket or "").strip())
        except ValueError:
            return ""
        if target_index <= 0:
            return ""
        return labels[target_index - 1]
    finally:
        conn.close()


def _insert_edge_trade(conn: sqlite3.Connection, values: Dict[str, Any]) -> None:
    insert_edge_trade_row(conn, values, table_columns=_table_columns(conn, "edge_trades"))


def _insert_market_tick(conn: sqlite3.Connection, values: Dict[str, Any]) -> bool:
    cols = [col for col in _table_columns(conn, "event_market_ticks") if col in values]
    if not cols:
        return False
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT INTO event_market_ticks({', '.join(cols)}) VALUES({placeholders})",
        tuple(values[col] for col in cols),
    )
    return True


def _seed_forced_switch_market_ticks(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    target_bucket: str,
    old_bucket: str,
    created_at: str,
) -> Dict[str, Any]:
    if not _table_columns(conn, "event_market_ticks"):
        return {"ok": False, "reason": "event_market_ticks_missing"}
    seeded = 0
    common = {
        "run_date": event_date,
        "event_date": event_date,
        "slug": slug,
        "side": "YES",
        "mid": 0.505,
        "spread": 0.03,
        "bid_depth_topN": 250.0,
        "ask_depth_topN": 250.0,
        "source": "copy_acceptance_forced_switch",
        "source_detail": "deterministic_target_old_bucket_ticks",
        "created_at": created_at,
    }
    seeded += int(
        _insert_market_tick(
            conn,
            {
                **common,
                "outcome": target_bucket,
                "best_bid": 0.50,
                "best_ask": 0.52,
                "mid": 0.51,
            },
        )
    )
    if old_bucket:
        seeded += int(
            _insert_market_tick(
                conn,
                {
                    **common,
                    "outcome": old_bucket,
                    "best_bid": 0.39,
                    "best_ask": 0.42,
                    "mid": 0.405,
                },
            )
        )
    return {"ok": bool(seeded), "seeded_ticks": seeded, "target_bucket": target_bucket, "old_bucket": old_bucket}


def _prepare_forced_switch_scenario(
    db_path: Path,
    *,
    slug: str,
    event_date: str,
    target_bucket: str,
    observed_bucket: str,
) -> Dict[str, Any]:
    if not slug or not event_date or not target_bucket:
        return {"ok": False, "reason": "missing_event_or_target"}
    old_bucket = observed_bucket if observed_bucket and observed_bucket != target_bucket else ""
    if not old_bucket:
        old_bucket = _nearest_lower_bucket_from_latest_fact(
            db_path,
            slug=slug,
            event_date=event_date,
            target_bucket=target_bucket,
        )
    if not old_bucket:
        return {"ok": False, "reason": "old_bucket_unavailable", "target_bucket": target_bucket}
    now_iso = _utc_now_iso()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cleared_target = copy_acceptance_clear_target(
            conn,
            now_iso=now_iso,
            slug=slug,
            event_date=event_date,
            target_bucket=target_bucket,
        )
        updated_old = copy_acceptance_seed_old(
            conn,
            now_iso=now_iso,
            slug=slug,
            event_date=event_date,
            old_bucket=old_bucket,
        )
        if not updated_old:
            _insert_edge_trade(
                conn,
                {
                    "slug": slug,
                    "event_date": event_date,
                    "outcome": old_bucket,
                    "side": "YES",
                    "state": "entry",
                    "entry_at": now_iso,
                    "entry_price": 0.25,
                    "entry_size": 8.0,
                    "position_size": 8.0,
                    "avg_price": 0.25,
                    "last_order_id": "copy-acceptance-old",
                    "last_action": "copy_acceptance_seed_old",
                    "last_status": "copy_acceptance_forced_switch",
                    "updated_at": now_iso,
                },
            )
        seeded_ticks = _seed_forced_switch_market_ticks(
            conn,
            slug=slug,
            event_date=event_date,
            target_bucket=target_bucket,
            old_bucket=old_bucket,
            created_at=now_iso,
        )
        conn.commit()
        return {
            "ok": True,
            "target_bucket": target_bucket,
            "old_bucket": old_bucket,
            "cleared_target_rows": int(cleared_target or 0),
            "updated_old_rows": int(updated_old or 0),
            "seeded_market_ticks": seeded_ticks,
        }
    finally:
        conn.close()


def _derive_hard_source_switch_target(
    db_path: Path,
    *,
    poller: Any,
    slug: str,
    event_date: str,
) -> Dict[str, Any]:
    payload = poller._load_latest_fact_payload(db_path, event_date=event_date, slug=slug)
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "latest_fact_payload_missing"}
    observation = poller._hard_observation_from_payload(
        payload,
        allow_wu_twc_fallback=True,
    )
    if not observation.get("ok"):
        return {"ok": False, "reason": "hard_observation_missing", "observation": observation}
    value_c = poller._to_float(observation.get("value_c"))
    if value_c is None:
        return {"ok": False, "reason": "hard_observation_value_missing", "observation": observation}
    observed_bucket = poller._bucket_label_for_value(payload, value_c)
    if not observed_bucket:
        return {
            "ok": False,
            "reason": "observed_bucket_missing",
            "source_id": observation.get("source_id"),
            "value_c": value_c,
        }
    projection_result = poller._recompute_ctp_latest_fact(
        db_path,
        event_date=event_date,
        slug=slug,
        reason="hard_source_direct_copy_acceptance",
    )
    if not bool(projection_result.get("ok")):
        return {
            "ok": False,
            "reason": "posterior_projection_failed",
            "projection_result": projection_result,
            "observed_bucket": observed_bucket,
        }
    projected_payload = poller._load_latest_fact_payload(db_path, event_date=event_date, slug=slug)
    posterior_ssot = (
        projected_payload.get("posterior_ssot_fact")
        if isinstance(projected_payload, Mapping)
        and isinstance(projected_payload.get("posterior_ssot_fact"), Mapping)
        else {}
    )
    target_bucket = str(posterior_ssot.get("target_bucket") or "").strip()
    if not target_bucket:
        return {
            "ok": False,
            "reason": "target_bucket_missing",
            "observed_bucket": observed_bucket,
        }
    return {
        "ok": True,
        "target_bucket": target_bucket,
        "observed_bucket": observed_bucket,
        "projection_result": projection_result,
        "source_id": observation.get("source_id"),
        "value_c": value_c,
        "constraint_mode": constraint_meta.get("constraint_mode"),
        "final_bucket_lock": bool(constraint_meta.get("final_bucket_lock")),
    }


def _forced_inside_peak_window_env(
    db_path: Path,
    *,
    poller: Any,
    slug: str,
    event_date: str,
) -> Dict[str, Any]:
    payload = poller._load_latest_fact_payload(db_path, event_date=event_date, slug=slug)
    if not isinstance(payload, dict) or not payload:
        return {"ok": False, "reason": "latest_fact_payload_missing"}
    peak_hour, peak_meta = poller._expected_peak_hour_local(payload, event_date=event_date)
    if peak_hour is None:
        return {
            "ok": False,
            "reason": "wu_forecast_peak_time_missing",
            "peak_meta": peak_meta,
        }
    local_hour = max(0.0, min(23.99, float(peak_hour)))
    hour_text = f"{local_hour:.4f}".rstrip("0").rstrip(".")
    return {
        "ok": True,
        "local_hour": local_hour,
        "event_date": event_date,
        "peak_meta": peak_meta,
        "env": {
            "PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED": "1",
            "PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED": "1",
            "PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE": event_date,
            "PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE": hour_text,
            "PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_LOCAL_DATE_OVERRIDE": event_date,
            "PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_LOCAL_HOUR_OVERRIDE": hour_text,
        },
    }


def run_copy_acceptance(args: argparse.Namespace) -> Dict[str, Any]:
    db_path = Path(args.db)
    if not db_path.exists():
        return {"ok": False, "blockers": ["archive_db_missing"], "details": {"db": str(db_path)}}
    tmp_ctx = None
    if bool(getattr(args, "keep_tmp", False)):
        tmp = tempfile.mkdtemp(prefix="hard-source-copy-")
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="hard-source-copy-")
        tmp = tmp_ctx.__enter__()
    try:
        tmp_db = Path(tmp) / "archive.db"
        if args.full_copy:
            _copy_sqlite_db(db_path, tmp_db)
            subset = {"mode": "full_copy"}
        else:
            subset = _copy_event_subset(
                db_path,
                tmp_db,
                slug=args.slug or "",
                event_date=args.event_date or "",
                tick_limit=int(args.tick_limit),
            )
        from scripts import audit_hard_source_direct_ssot
        from scripts import hard_source_direct_acceptance_audit
        from scripts import hard_source_direct_poller

        event_slug = args.slug or subset.get("resolved_slug") or ""
        event_date = args.event_date or subset.get("resolved_event_date") or ""
        forced_inside_window = (
            _forced_inside_peak_window_env(
                tmp_db,
                poller=hard_source_direct_poller,
                slug=event_slug,
                event_date=event_date,
            )
            if args.force_inside_peak_window
            else {"ok": False, "skipped": True}
        )
        window_enabled = bool(args.use_decision_window or args.force_inside_peak_window)
        env = {
            "PM_AUTO_TRADE_DRY_RUN": "1",
            "PM_AUTO_TRADE_ENABLED": "1",
            "PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED": "1" if window_enabled else "0",
            "PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED": "1" if window_enabled else "0",
            "PM_HARD_SOURCE_DIRECT_REFRESH_AWC": "1" if args.refresh_awc else "0",
            "PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC": "1" if args.refresh_wu_twc else "0",
            "PM_HARD_SOURCE_DIRECT_ENQUEUE_JOB": "1",
            "PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE": "1",
            "PM_HARD_SOURCE_DIRECT_DECISION_HEARTBEAT_SEC": "0",
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
            "PM_AUTO_TRADE_RULES_LIVE_HYDRATE": "0",
            "PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS": "0",
            "PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS": "0",
            "PM_AUTO_TRADE_ENTRY_REFRESH_ON_STALE": "0",
            "PM_AUTO_TRADE_EXIT_REFRESH_ON_STALE": "0",
            "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_ENABLED": "0",
            "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_INLINE": "0",
        }
        if isinstance(forced_inside_window.get("env"), dict):
            env.update({str(k): str(v) for k, v in forced_inside_window["env"].items()})
        with _override_env(env):
            market_prefetch_prewarm = (
                hard_source_direct_poller._market_prefetch_once_with_lock(
                    tmp_db,
                    event_date=event_date,
                    slug=event_slug,
                )
                if window_enabled and event_slug
                else {"ok": False, "skipped": True}
            )
            freshen = (
                _freshen_hard_source_timestamps(tmp_db, slug=event_slug, event_date=event_date)
                if args.freshen_hard_source_timestamps
                else {"ok": False, "skipped": True}
            )
            forced_final_lock = (
                _force_final_lock_scenario(tmp_db, slug=event_slug, event_date=event_date)
                if args.force_final_lock_scenario
                else {"ok": False, "skipped": True}
            )
            forced_switch = {"ok": False, "skipped": True}
            if args.force_switch_scenario:
                planned_switch = _derive_hard_source_switch_target(
                    tmp_db,
                    poller=hard_source_direct_poller,
                    slug=event_slug,
                    event_date=event_date,
                )
                if planned_switch.get("ok"):
                    forced_switch = _prepare_forced_switch_scenario(
                        tmp_db,
                        slug=event_slug,
                        event_date=event_date,
                        target_bucket=str(planned_switch.get("target_bucket") or ""),
                        observed_bucket=str(planned_switch.get("observed_bucket") or ""),
                    )
                    forced_switch["planned"] = planned_switch
                else:
                    forced_switch = planned_switch
            poll = hard_source_direct_poller.poll_once(
                tmp_db,
                event_date=event_date,
                slug=event_slug,
            )
            if forced_switch.get("ok") and poll.get("decision", {}).get("ok"):
                forced_switch["poll_target_bucket"] = poll.get("decision", {}).get("target_bucket")
                forced_switch["poll_observed_bucket"] = poll.get("decision", {}).get("observed_bucket")
                target_matches = bool(
                    forced_switch.get("target_bucket") == poll.get("decision", {}).get("target_bucket")
                )
                old_matches_observed = bool(
                    forced_switch.get("old_bucket") == poll.get("decision", {}).get("observed_bucket")
                )
                observed_is_target = bool(
                    poll.get("decision", {}).get("observed_bucket")
                    == poll.get("decision", {}).get("target_bucket")
                )
                forced_switch["matches_poll"] = bool(
                    target_matches and (old_matches_observed or observed_is_target)
                )
            worker = {}
            if isinstance(poll.get("worker"), dict) and poll["worker"]:
                worker = poll["worker"]
            elif poll.get("decision", {}).get("job_id"):
                worker = hard_source_direct_poller._run_worker_once_inline(db_path=tmp_db)
            audit_args = argparse.Namespace(
                db=str(tmp_db),
                since="",
                since_hours=float(args.since_hours),
                slug=event_slug,
                event_date=event_date,
                max_fetch_to_ssot_ms=float(args.max_fetch_to_ssot_ms),
                max_ssot_to_job_enqueue_ms=float(args.max_ssot_to_job_enqueue_ms),
                max_job_enqueue_to_worker_ms=float(args.max_job_enqueue_to_worker_ms),
                max_ssot_to_order_ms=float(args.max_ssot_to_order_ms),
                max_job_enqueue_to_order_ms=float(args.max_job_enqueue_to_order_ms),
                max_worker_to_order_ms=float(args.max_worker_to_order_ms),
            )
            ssot_audit = audit_hard_source_direct_ssot.run_audit(audit_args)
            legacy_audit = hard_source_direct_acceptance_audit.audit(
                tmp_db,
                slug=event_slug,
                event_date=event_date,
                max_ssot_to_order_ms=float(args.max_ssot_to_order_ms),
            )
        return {
            "ok": bool(poll.get("ok")) and bool(ssot_audit.get("ok")) and bool(legacy_audit.get("ok")),
            "mode": "temporary_db_copy",
            "source_db": str(db_path),
            "tmp_db": str(tmp_db),
            "tmp_db_removed": not bool(getattr(args, "keep_tmp", False)),
            "subset_copy": subset,
            "forced_inside_peak_window": forced_inside_window,
            "market_prefetch_prewarm": market_prefetch_prewarm,
            "freshen_hard_source_timestamps": freshen,
            "forced_final_lock_scenario": forced_final_lock,
            "forced_switch_scenario": forced_switch,
            "poll": poll,
            "worker": worker,
            "ssot_audit": ssot_audit,
            "acceptance_audit": legacy_audit,
        }
    finally:
        if tmp_ctx is not None:
            tmp_ctx.__exit__(None, None, None)


def _compact_for_print(value: Any, *, max_string: int = 1200) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if key in {"payload_json", "fact_json"} and isinstance(item, str):
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
    parser.add_argument("--full-copy", action="store_true")
    parser.add_argument("--tick-limit", type=int, default=5000)
    parser.add_argument("--refresh-awc", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--refresh-wu-twc", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--freshen-hard-source-timestamps", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--force-switch-scenario", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--force-final-lock-scenario", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--use-decision-window", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--force-inside-peak-window", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--since-hours", type=float, default=6.0)
    parser.add_argument("--max-fetch-to-ssot-ms", type=float, default=1000.0)
    parser.add_argument("--max-ssot-to-job-enqueue-ms", type=float, default=1000.0)
    parser.add_argument("--max-job-enqueue-to-worker-ms", type=float, default=5000.0)
    parser.add_argument("--max-ssot-to-order-ms", type=float, default=10000.0)
    parser.add_argument("--max-job-enqueue-to-order-ms", type=float, default=10000.0)
    parser.add_argument("--max-worker-to-order-ms", type=float, default=5000.0)
    parser.add_argument("--keep-tmp", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_copy_acceptance(args)
    print(json.dumps(_compact_for_print(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
