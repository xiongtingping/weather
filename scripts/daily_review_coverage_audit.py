#!/usr/bin/env python3
"""Read-only coverage audit for persisted daily weather reviews.

This audit answers the post-schema question: for settled weather markets, do we
have one review record, and can that review be replayed back through the SSOT
fact/decision/execution chain with settlement and calibration fields intact?

It does not write archive.db, backfill reviews, fetch weather sources, change
parameters, restart launchd, or place orders.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daily_review_persistence_writer import daily_review_persistence_schema_status
from daily_weather_review_runtime import CHAIN_NODE_ORDER
from scripts import ssot_chain_replay_audit


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "daily_review_coverage_audit_v1"

REVIEW_FIELD_CAPABILITY_SCHEMA = "daily_review_field_capability_matrix_v1"
REVIEW_FIELD_SPECS: tuple[dict[str, str], ...] = (
    {"id": "first_bad_link", "category": "first_bad_link", "path": "review.first_bad_link"},
    {"id": "first_bad_link_reason", "category": "first_bad_link", "path": "review_json.first_bad_link_reason"},
    {"id": "actual_tmax_c", "category": "calibration_metrics", "path": "review_json.calibration_metrics.actual_tmax_c"},
    {"id": "settlement_bucket", "category": "calibration_metrics", "path": "review_json.calibration_metrics.settlement_bucket"},
    {"id": "target_hit", "category": "calibration_metrics", "path": "review_json.calibration_metrics.target_hit"},
    {
        "id": "posterior_actual_bucket_probability",
        "category": "calibration_metrics",
        "path": "review_json.calibration_metrics.posterior_actual_bucket_probability",
    },
    {"id": "posterior_brier", "category": "calibration_metrics", "path": "review_json.calibration_metrics.posterior_brier"},
    {
        "id": "posterior_log_loss",
        "category": "calibration_metrics",
        "path": "review_json.calibration_metrics.posterior_log_loss",
    },
    {"id": "calibration_bin", "category": "calibration_metrics", "path": "review_json.calibration_metrics.calibration_bin"},
    {
        "id": "posterior_rank_of_actual_bucket",
        "category": "calibration_metrics",
        "path": "review_json.calibration_metrics.posterior_rank_of_actual_bucket",
    },
    {
        "id": "hard_constraint_violation_count",
        "category": "calibration_metrics",
        "path": "review_json.calibration_metrics.hard_constraint_violation_count",
    },
    {
        "id": "D1_to_D0_probability_delta",
        "category": "calibration_metrics",
        "path": "review_json.calibration_metrics.D1_to_D0_probability_delta",
    },
    {
        "id": "source_latency_ms_by_source",
        "category": "calibration_metrics",
        "path": "review_json.calibration_metrics.source_latency_ms_by_source",
    },
    {
        "id": "decision_miss_reason",
        "category": "calibration_metrics",
        "path": "review_json.calibration_metrics.decision_miss_reason",
        "allow_empty": "true",
    },
    {
        "id": "execution_miss_reason",
        "category": "calibration_metrics",
        "path": "review_json.calibration_metrics.execution_miss_reason",
        "allow_empty": "true",
    },
    {"id": "primary_link", "category": "error_attribution", "path": "review_json.error_attribution.primary_link"},
    {"id": "components", "category": "error_attribution", "path": "review_json.error_attribution.components"},
    {
        "id": "daily_calibration_record_id",
        "category": "daily_calibration_record",
        "path": "review_json.daily_calibration_record.daily_calibration_record_id",
    },
    {
        "id": "daily_calibration_record_state",
        "category": "daily_calibration_record",
        "path": "review_json.daily_calibration_record.state",
    },
    {
        "id": "bias_correction_candidates",
        "category": "bias_correction_candidates",
        "path": "review_json.bias_correction_candidates",
    },
    {
        "id": "bias_candidate_state",
        "category": "bias_correction_candidates",
        "path": "review_json.bias_correction_candidates.state",
    },
    {
        "id": "bias_candidate_trigger_sample",
        "category": "bias_correction_candidates",
        "path": "review_json.bias_correction_candidates.trigger_sample",
    },
    {
        "id": "bias_candidate_impact_layer",
        "category": "bias_correction_candidates",
        "path": "review_json.bias_correction_candidates.impact_layer",
    },
    {
        "id": "bias_candidate_expected_improvement_metric",
        "category": "bias_correction_candidates",
        "path": "review_json.bias_correction_candidates.expected_improvement_metric",
    },
    {
        "id": "bias_candidate_possible_side_effects",
        "category": "bias_correction_candidates",
        "path": "review_json.bias_correction_candidates.possible_side_effects",
    },
    {
        "id": "bias_candidate_requires_historical_recompute",
        "category": "bias_correction_candidates",
        "path": "review_json.bias_correction_candidates.requires_historical_recompute",
    },
    {"id": "posterior_lineage_fact_snapshot_id", "category": "posterior_lineage", "path": "review_json.posterior_lineage.fact_snapshot_id"},
    {"id": "posterior_lineage_posterior_run_id", "category": "posterior_lineage", "path": "review_json.posterior_lineage.posterior_run_id"},
    {"id": "posterior_lineage_phase", "category": "posterior_lineage", "path": "review_json.posterior_lineage.phase"},
    {"id": "posterior_lineage_beta_weak", "category": "posterior_lineage", "path": "review_json.posterior_lineage.beta_weak"},
    {"id": "posterior_lineage_beta_strong", "category": "posterior_lineage", "path": "review_json.posterior_lineage.beta_strong"},
    {
        "id": "posterior_lineage_hard_mask_reason",
        "category": "posterior_lineage",
        "path": "review_json.posterior_lineage.hard_mask_reason",
    },
    {"id": "chain_node_trace", "category": "chain_node_trace", "path": "review_json.chain_node_trace"},
    {"id": "chain_node_trace_nodes", "category": "chain_node_trace", "path": "review_json.chain_node_trace.nodes"},
    {
        "id": "chain_node_trace_first_bad_link_node",
        "category": "chain_node_trace",
        "path": "review_json.chain_node_trace.first_bad_link_node_id",
        "allow_empty": "true",
    },
)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(conn, name):
        return set()
    return {str(row["name"] or "") for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


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


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _date_only(raw: Any) -> str:
    text = str(raw or "").strip()
    return text[:10] if len(text) >= 10 else text


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _since_date_from_days(since_days: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=max(1, int(since_days)))).isoformat()


def since_date_from_days(since_days: int) -> str:
    return _since_date_from_days(since_days)


def _row_dict(row: sqlite3.Row, keys: Sequence[str]) -> dict[str, Any]:
    row_keys = set(row.keys())
    return {key: row[key] for key in keys if key in row_keys}


def _event_market_scope_clause(market_scope: str) -> tuple[str, list[Any]]:
    scope = str(market_scope or "highest_temperature").strip()
    if scope in {"", "all", "*"}:
        return "", []
    if scope == "highest_temperature":
        return " AND slug LIKE ?", ["highest-temperature-%"]
    return " AND slug LIKE ?", [f"%{scope}%"]


def _load_settled_events(
    conn: sqlite3.Connection,
    *,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "event_runs"):
        return []
    columns = _table_columns(conn, "event_runs")
    required = {"slug", "event_date", "settlement_actual"}
    if not required.issubset(columns):
        return []
    clauses, params = _settled_event_filter_clauses(
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
    )
    select_keys = [
        key
        for key in (
            "id",
            "run_date",
            "event_date",
            "slug",
            "city",
            "station_id",
            "settlement_actual",
            "rounding_rule",
            "market_closed",
            "event_end_utc",
            "created_at",
        )
        if key in columns
    ]
    rows = conn.execute(
        f"""
        SELECT {", ".join(select_keys)}
        FROM event_runs
        WHERE {" AND ".join(clauses)}
        ORDER BY date(event_date) DESC, datetime(created_at) DESC, id DESC
        LIMIT ?
        """,
        tuple(params + [max(1, int(limit) * 4)]),
    ).fetchall()
    latest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        item = _row_dict(row, select_keys)
        key = (str(item.get("slug") or ""), _date_only(item.get("event_date")))
        if not key[0] or not key[1] or key in latest_by_key:
            continue
        latest_by_key[key] = item
        if len(latest_by_key) >= max(1, int(limit)):
            break
    return list(latest_by_key.values())


def _settled_event_filter_clauses(
    *,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
) -> tuple[list[str], list[Any]]:
    clauses = ["settlement_actual IS NOT NULL", "settlement_actual != ''"]
    params: list[Any] = []
    if since_date:
        clauses.append("date(event_date) >= date(?)")
        params.append(_date_only(since_date))
    if slug:
        clauses.append("slug = ?")
        params.append(slug)
    if event_date:
        clauses.append("event_date = ?")
        params.append(_date_only(event_date))
    scope_clause, scope_params = _event_market_scope_clause(market_scope)
    if scope_clause:
        clauses.append(scope_clause.replace(" AND ", "", 1))
        params.extend(scope_params)
    return clauses, params


def _count_settled_event_keys(
    conn: sqlite3.Connection,
    *,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
) -> int:
    if not _table_exists(conn, "event_runs"):
        return 0
    columns = _table_columns(conn, "event_runs")
    required = {"slug", "event_date", "settlement_actual"}
    if not required.issubset(columns):
        return 0
    clauses, params = _settled_event_filter_clauses(
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
    )
    rows = conn.execute(
        f"""
        SELECT slug, event_date
        FROM event_runs
        WHERE {" AND ".join(clauses)}
        """,
        tuple(params),
    ).fetchall()
    keys = {
        (str(row["slug"] or ""), _date_only(row["event_date"]))
        for row in rows
        if str(row["slug"] or "").strip() and _date_only(row["event_date"])
    }
    return len(keys)


def load_settled_events(
    conn: sqlite3.Connection,
    *,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
) -> list[dict[str, Any]]:
    return _load_settled_events(
        conn,
        since_date=since_date,
        slug=slug,
        event_date=event_date,
        market_scope=market_scope,
        limit=limit,
    )


def _manifest_decision_rows(manifest: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    payload = _as_mapping(manifest)
    raw_decisions = payload.get("decisions")
    if isinstance(raw_decisions, Mapping):
        rows = list(raw_decisions.values())
    else:
        rows = list(_as_sequence(raw_decisions))
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(_as_mapping(raw))
        if not row:
            continue
        decision = str(row.get("decision") or "").strip()
        if decision == "exclude_sample":
            out.append(row)
    return out


def _manifest_row_matches_event(row: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    row_event_run_id = str(row.get("event_run_id") or "").strip()
    event_run_id = str(event.get("id") or event.get("event_run_id") or "").strip()
    if row_event_run_id and event_run_id and row_event_run_id == event_run_id:
        return True
    row_slug = str(row.get("slug") or "").strip()
    row_event_date = _date_only(row.get("event_date"))
    event_slug = str(event.get("slug") or "").strip()
    event_date = _date_only(event.get("event_date"))
    return bool(row_slug and row_event_date and row_slug == event_slug and row_event_date == event_date)


def _split_manifest_excluded_events(
    events: Sequence[Mapping[str, Any]],
    exclusion_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not exclusion_rows:
        return [dict(_as_mapping(event)) for event in events], []
    effective: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for event in events:
        event_map = dict(_as_mapping(event))
        matched_row = next(
            (
                dict(_as_mapping(row))
                for row in exclusion_rows
                if _manifest_row_matches_event(_as_mapping(row), event_map)
            ),
            None,
        )
        if matched_row:
            excluded.append(
                {
                    "slug": event_map.get("slug"),
                    "event_date": _date_only(event_map.get("event_date")),
                    "event_run_id": event_map.get("id") or event_map.get("event_run_id"),
                    "decision": "exclude_sample",
                    "filter_reason": "operator_manifest_exclude_sample",
                    "repair_correlation_id": matched_row.get("repair_correlation_id"),
                    "repair_type": matched_row.get("repair_type"),
                    "reason": matched_row.get("reason"),
                    "state": matched_row.get("state"),
                }
            )
        else:
            effective.append(event_map)
    return effective, excluded


def _lineage_decision_manifest_audit(
    *,
    lineage_decision_manifest: Mapping[str, Any] | None,
    exclusion_rows: Sequence[Mapping[str, Any]],
    excluded_settled_events: Sequence[Mapping[str, Any]],
    excluded_scanned_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    payload = _as_mapping(lineage_decision_manifest)
    return {
        "schema": "daily_review_coverage_lineage_decision_manifest_audit_v1",
        "read_only": True,
        "manifest_provided": bool(payload),
        "manifest_schema": payload.get("schema"),
        "excluded_decision_count": len(exclusion_rows),
        "excluded_settled_event_count": len(excluded_settled_events),
        "excluded_settled_event_scanned_count": len(excluded_scanned_events),
        "excluded_event_run_ids": [
            item.get("event_run_id")
            for item in excluded_settled_events
            if str(item.get("event_run_id") or "").strip()
        ],
        "excluded_samples": [dict(_as_mapping(item)) for item in excluded_settled_events],
        "filter_reason": "operator_manifest_exclude_sample" if exclusion_rows else "",
        "guards": {
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
        },
    }


def _load_lineage_decision_manifest_file(path: Path | str | None) -> Mapping[str, Any]:
    if path is None:
        return {}
    manifest_path = Path(path)
    if not str(manifest_path).strip():
        return {}
    text = manifest_path.expanduser().read_text(encoding="utf-8")
    payload = json.loads(text)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _fact_snapshot_id_from_payload(payload: Mapping[str, Any]) -> str:
    lineage = _as_mapping(payload.get("lineage_state"))
    return _first_text(payload.get("fact_snapshot_id"), lineage.get("fact_snapshot_id"))


def _posterior_run_id_from_payload(payload: Mapping[str, Any]) -> str:
    lineage = _as_mapping(payload.get("lineage_state"))
    posterior_runtime = _as_mapping(payload.get("posterior_runtime"))
    ssot = _as_mapping(payload.get("posterior_ssot_fact"))
    return _first_text(
        ssot.get("posterior_run_id"),
        payload.get("posterior_run_id"),
        lineage.get("posterior_run_id"),
        posterior_runtime.get("posterior_run_id"),
    )


def _source_event_ids_from_payload(payload: Mapping[str, Any]) -> list[str]:
    out: list[str] = []

    def add(raw: Any) -> None:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)

    for row in payload.get("source_observations") or []:
        if isinstance(row, Mapping):
            add(row.get("source_event_id"))
    trace = _as_mapping(payload.get("source_acquisition_trace"))
    for row in trace.get("source_observations") or []:
        if isinstance(row, Mapping):
            add(row.get("source_event_id"))
    for value in trace.get("source_event_ids") or []:
        add(value)
    return out


def _find_fact_for_event(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    fact_snapshot_id: str = "",
) -> dict[str, Any]:
    if not _table_exists(conn, "event_fact_history"):
        return {"available": False, "reason": "event_fact_history_missing"}
    columns = _table_columns(conn, "event_fact_history")
    where: list[str] = []
    params: list[Any] = []
    if slug and "slug" in columns:
        where.append("slug = ?")
        params.append(slug)
    if event_date and "event_date" in columns:
        where.append("event_date = ?")
        params.append(_date_only(event_date))
    if fact_snapshot_id and "fact_json" in columns:
        where.append("fact_json LIKE ?")
        params.append(f"%{fact_snapshot_id}%")
    clause = "WHERE " + " AND ".join(where) if where else ""
    row = conn.execute(
        f"""
        SELECT *
        FROM event_fact_history
        {clause}
        ORDER BY datetime(fact_as_of) DESC, id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        return {"available": False, "reason": "fact_snapshot_not_found"}
    payload = _safe_json(row["fact_json"] if "fact_json" in row.keys() else "")
    payload_map = dict(payload) if isinstance(payload, Mapping) else {}
    return {
        "available": True,
        "fact_id": row["id"] if "id" in row.keys() else "",
        "fact_as_of": row["fact_as_of"] if "fact_as_of" in row.keys() else "",
        "created_at": row["created_at"] if "created_at" in row.keys() else "",
        "fact_snapshot_id": _fact_snapshot_id_from_payload(payload_map),
        "posterior_run_id": _posterior_run_id_from_payload(payload_map),
        "posterior_ssot_fact_present": bool(_as_mapping(payload_map.get("posterior_ssot_fact"))),
        "source_event_ids": _source_event_ids_from_payload(payload_map),
        "posterior_present": bool(
            payload_map.get("posterior_ssot_fact")
            or payload_map.get("posterior_temperature_distribution")
            or payload_map.get("distribution")
            or payload_map.get("rows")
        ),
        "source_lineage_present": bool(
            payload_map.get("source_observations") or payload_map.get("source_acquisition_trace")
        ),
    }


def _review_order_clause(columns: set[str]) -> str:
    if "created_at" in columns:
        return "datetime(created_at) DESC"
    if "id" in columns:
        return "id DESC"
    return "rowid DESC"


def _load_review_for_event(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    fact_snapshot_id: str = "",
) -> dict[str, Any]:
    if not _table_exists(conn, "daily_weather_reviews"):
        return {"available": False, "reason": "daily_weather_reviews_missing_planned_table"}
    columns = _table_columns(conn, "daily_weather_reviews")
    order = _review_order_clause(columns)
    row: Optional[sqlite3.Row] = None
    if fact_snapshot_id and "fact_snapshot_id" in columns:
        row = conn.execute(
            f"SELECT * FROM daily_weather_reviews WHERE fact_snapshot_id = ? ORDER BY {order} LIMIT 1",
            (fact_snapshot_id,),
        ).fetchone()
    if row is None and {"slug", "event_date"}.issubset(columns):
        row = conn.execute(
            f"""
            SELECT *
            FROM daily_weather_reviews
            WHERE slug = ? AND event_date = ?
            ORDER BY {order}
            LIMIT 1
            """,
            (slug, _date_only(event_date)),
        ).fetchone()
    if row is None and "review_json" in columns:
        row = conn.execute(
            f"""
            SELECT *
            FROM daily_weather_reviews
            WHERE review_json LIKE ? AND review_json LIKE ?
            ORDER BY {order}
            LIMIT 1
            """,
            (f"%{slug}%", f"%{_date_only(event_date)}%"),
        ).fetchone()
    if row is None:
        return {"available": False, "reason": "daily_review_missing"}
    review_json = _safe_json(row["review_json"] if "review_json" in columns else "")
    review_map = dict(review_json) if isinstance(review_json, Mapping) else {}
    review_id = _first_text(row["review_id"] if "review_id" in columns else "", review_map.get("review_id"))
    return {
        "available": True,
        "review_id": review_id,
        "row": _row_dict(
            row,
            (
                "review_id",
                "run_date",
                "slug",
                "event_date",
                "fact_snapshot_id",
                "first_bad_link",
                "created_at",
            ),
        ),
        "review_json": review_map,
        "first_bad_link": _first_text(
            row["first_bad_link"] if "first_bad_link" in columns else "",
            review_map.get("first_bad_link"),
        ),
        "fact_snapshot_id": _first_text(
            row["fact_snapshot_id"] if "fact_snapshot_id" in columns else "",
            review_map.get("fact_snapshot_id"),
        ),
    }


def _review_settlement_actual(review: Mapping[str, Any]) -> float | None:
    review_json = _as_mapping(review.get("review_json"))
    metrics = _as_mapping(review_json.get("calibration_metrics"))
    for raw in (
        review_json.get("settlement_actual"),
        metrics.get("actual_tmax_c"),
        metrics.get("settlement_actual"),
    ):
        value = _float_or_none(raw)
        if value is not None:
            return value
    return None


def _settlement_matches(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    return abs(float(left) - float(right)) <= 0.05


def _review_json_has(mapping: Mapping[str, Any], key: str) -> bool:
    return bool(_as_mapping(mapping.get("review_json")).get(key))


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if value == "":
        return False
    if isinstance(value, (list, tuple, set, dict)) and not value:
        return False
    return True


def _field_present(value: Any, spec: Mapping[str, str]) -> bool:
    if str(spec.get("allow_empty") or "").lower() == "true":
        return value is not None
    return _is_present(value)


def _value_at_path(root: Mapping[str, Any], path: str) -> Any:
    node: Any = root
    for part in str(path or "").split("."):
        if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
            values = []
            for item in node:
                item_map = _as_mapping(item)
                if part in item_map:
                    values.append(item_map.get(part))
            node = values
            continue
        node = _as_mapping(node).get(part)
    return node


def _review_field_na_reason(review_json: Mapping[str, Any], field_id: str) -> str:
    metrics = _as_mapping(review_json.get("calibration_metrics"))
    na_reasons = _as_mapping(metrics.get("na_reasons"))
    return str(na_reasons.get(field_id) or "").strip()


def _review_field_capability(
    review: Mapping[str, Any],
    review_json: Mapping[str, Any],
) -> dict[str, Any]:
    review_available = bool(review.get("available"))
    blocked_reason = str(review.get("reason") or "daily_review_missing").strip()
    bias_candidates = review_json.get("bias_correction_candidates")
    if bias_candidates is None:
        bias_candidates = review_json.get("calibration_candidates")
    has_bias_candidates = bool(_as_sequence(bias_candidates))
    root = {
        "review": review,
        "review_json": review_json,
    }
    fields: dict[str, dict[str, Any]] = {}
    category_counts: dict[str, dict[str, int]] = {}
    for spec in REVIEW_FIELD_SPECS:
        field_id = str(spec["id"])
        category = str(spec["category"])
        category_counts.setdefault(category, {"available": 0, "missing": 0, "blocked": 0, "not_applicable": 0})
        if not review_available:
            status = "blocked"
            reason = blocked_reason or "daily_review_missing"
            sample_value: Any = None
        elif category == "bias_correction_candidates" and not has_bias_candidates:
            status = "not_applicable"
            reason = "not_applicable_no_bias_correction_candidates"
            sample_value = None
        else:
            sample_value = _value_at_path(root, str(spec["path"]))
            if _field_present(sample_value, spec):
                status = "available"
                reason = ""
            else:
                status = "missing"
                reason = _review_field_na_reason(review_json, field_id) or f"{field_id}_missing"
        category_counts[category][status] += 1
        fields[field_id] = {
            "category": category,
            "path": str(spec["path"]),
            "status": status,
            "reason": reason,
            "present": status == "available",
            "sample": sample_value if isinstance(sample_value, (str, int, float, bool)) else None,
        }
    return {
        "schema": REVIEW_FIELD_CAPABILITY_SCHEMA,
        "review_available": review_available,
        "field_count": len(fields),
        "fields": fields,
        "category_counts": category_counts,
    }


def _bias_candidate_contract_blockers(review_json: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    candidates = review_json.get("bias_correction_candidates")
    if candidates is None:
        candidates = review_json.get("calibration_candidates")
    rows = [dict(_as_mapping(item)) for item in _as_sequence(candidates) if isinstance(item, Mapping)]
    if not rows:
        return blockers
    required_fields = (
        "state",
        "trigger_sample",
        "impact_layer",
        "expected_improvement_metric",
        "possible_side_effects",
        "requires_historical_recompute",
    )
    for row in rows:
        for field in required_fields:
            if not _is_present(row.get(field)):
                blocker = f"bias_candidate_{field}_missing"
                if blocker not in blockers:
                    blockers.append(blocker)
        if str(row.get("state") or "").strip() != "proposed":
            blocker = "bias_candidate_state_not_proposed"
            if blocker not in blockers:
                blockers.append(blocker)
    return blockers


def _compact_review_field_capability(capability: Mapping[str, Any]) -> dict[str, Any]:
    fields = _as_mapping(capability.get("fields"))
    compact_fields: dict[str, dict[str, Any]] = {}
    missing_or_blocked: list[str] = []
    available_count = 0
    for field_id, raw in fields.items():
        field = _as_mapping(raw)
        status = str(field.get("status") or "").strip()
        if status == "available":
            available_count += 1
        elif status != "not_applicable" and field_id:
            missing_or_blocked.append(str(field_id))
        compact_fields[str(field_id)] = {
            "category": str(field.get("category") or ""),
            "status": status,
            "reason": str(field.get("reason") or ""),
            "present": bool(field.get("present")),
        }
    return {
        "schema": REVIEW_FIELD_CAPABILITY_SCHEMA,
        "review_available": bool(capability.get("review_available")),
        "field_count": int(capability.get("field_count") or len(compact_fields)),
        "available_field_count": available_count,
        "missing_or_blocked_field_count": len(missing_or_blocked),
        "missing_or_blocked_fields": missing_or_blocked,
        "category_counts": dict(_as_mapping(capability.get("category_counts"))),
        "fields": compact_fields,
    }


def _aggregate_review_field_capability(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {}
    categories: dict[str, dict[str, int]] = {}
    item_count = len(items)
    for spec in REVIEW_FIELD_SPECS:
        field_id = str(spec["id"])
        category = str(spec["category"])
        counters = {"available": 0, "missing": 0, "blocked": 0, "not_applicable": 0}
        reasons: dict[str, int] = {}
        for item in items:
            capability = _as_mapping(item.get("review_field_capability"))
            field_state = _as_mapping(_as_mapping(capability.get("fields")).get(field_id))
            status = str(field_state.get("status") or "blocked").strip()
            if status not in counters:
                status = "missing"
            counters[status] += 1
            reason = str(field_state.get("reason") or "").strip()
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        if item_count <= 0:
            overall = "not_applicable"
        elif counters["available"] == item_count:
            overall = "available"
        elif counters["blocked"] == item_count:
            overall = "blocked"
        elif counters["not_applicable"] == item_count:
            overall = "not_applicable"
        else:
            overall = "partial"
        fields[field_id] = {
            "category": category,
            "path": str(spec["path"]),
            "status": overall,
            "available_count": counters["available"],
            "missing_count": counters["missing"],
            "blocked_count": counters["blocked"],
            "not_applicable_count": counters["not_applicable"],
            "reason_counts": dict(sorted(reasons.items())),
        }
        category_entry = categories.setdefault(category, {"available": 0, "partial": 0, "blocked": 0, "not_applicable": 0})
        category_entry[overall] = category_entry.get(overall, 0) + 1
    missing_or_blocked = [
        field_id
        for field_id, field in fields.items()
        if field.get("status") not in {"available", "not_applicable"}
    ]
    return {
        "schema": REVIEW_FIELD_CAPABILITY_SCHEMA,
        "read_only": True,
        "settled_event_count": item_count,
        "field_count": len(fields),
        "available_field_count": sum(1 for field in fields.values() if field.get("status") == "available"),
        "missing_or_blocked_field_count": len(missing_or_blocked),
        "missing_or_blocked_fields": missing_or_blocked,
        "categories": categories,
        "fields": fields,
    }


def _historical_settled_date_regression(
    *,
    required: bool,
    slug: str,
    event_date: str,
    raw_settled_event_total_count: int,
    settled_event_total_count: int,
    settled_count: int,
    coverage_scope_complete: bool,
    reviewed_count: int,
    missing_review_count: int,
    chain_blocked_count: int,
    items: Sequence[Mapping[str, Any]],
    field_capability_matrix: Mapping[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    explicit_anchor = bool(str(slug or "").strip() or _date_only(event_date))
    if not required:
        return {
            "schema": "historical_settled_date_regression_v1",
            "enabled": False,
            "ok": True,
            "reason": "not_requested",
            "blockers": [],
        }
    if not explicit_anchor:
        blockers.append("historical_regression_requires_slug_or_event_date")
    if raw_settled_event_total_count <= 0:
        blockers.append("historical_regression_settled_event_not_found")
    if settled_event_total_count <= 0:
        blockers.append("historical_regression_effective_settled_scope_empty")
    if settled_count <= 0:
        blockers.append("historical_regression_no_scanned_settled_event")
    if not coverage_scope_complete:
        blockers.append("historical_regression_scope_truncated")
    if missing_review_count > 0 or reviewed_count < settled_count:
        blockers.append("historical_regression_daily_review_missing")
    if chain_blocked_count > 0:
        blockers.append("historical_regression_chain_blocked")

    required_fields = (
        "actual_tmax_c",
        "settlement_bucket",
        "posterior_actual_bucket_probability",
        "posterior_brier",
        "posterior_log_loss",
        "posterior_lineage_fact_snapshot_id",
        "posterior_lineage_posterior_run_id",
        "chain_node_trace",
        "chain_node_trace_nodes",
    )
    fields = _as_mapping(field_capability_matrix.get("fields"))
    missing_fields: list[str] = []
    for field_id in required_fields:
        status = str(_as_mapping(fields.get(field_id)).get("status") or "").strip()
        if status != "available":
            missing_fields.append(field_id)
    if missing_fields:
        blockers.append("historical_regression_required_fields_missing")

    item_failures: list[dict[str, Any]] = []
    for item in items:
        item_map = _as_mapping(item)
        item_blockers = list(item_map.get("blockers") or [])
        if item_blockers:
            item_failures.append(
                {
                    "slug": item_map.get("slug"),
                    "event_date": item_map.get("event_date"),
                    "blockers": item_blockers[:10],
                }
            )
        if not bool(item_map.get("review_fact_snapshot_matches_latest")):
            blockers.append("historical_regression_fact_snapshot_lineage_mismatch")
        if not bool(item_map.get("review_posterior_run_matches_latest")):
            blockers.append("historical_regression_posterior_run_lineage_mismatch")
        review_replay = _as_mapping(item_map.get("review_replay"))
        if review_replay and not bool(review_replay.get("ok")):
            blockers.append("historical_regression_replay_failed")

    return {
        "schema": "historical_settled_date_regression_v1",
        "enabled": True,
        "ok": not blockers,
        "explicit_anchor": explicit_anchor,
        "slug": str(slug or "").strip(),
        "event_date": _date_only(event_date),
        "raw_settled_event_total_count": int(raw_settled_event_total_count),
        "settled_event_total_count": int(settled_event_total_count),
        "settled_event_count": int(settled_count),
        "coverage_scope_complete": bool(coverage_scope_complete),
        "reviewed_event_count": int(reviewed_count),
        "missing_review_count": int(missing_review_count),
        "chain_blocked_count": int(chain_blocked_count),
        "required_fields": list(required_fields),
        "missing_required_fields": missing_fields,
        "item_failures": item_failures[:10],
        "blockers": sorted(set(blockers)),
        "guards": {
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "changes_parameters": False,
        },
    }


def _posterior_lineage_from_review(review_json: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(review_json.get("posterior_lineage"))


def _replay_anchor_state(replay: Mapping[str, Any]) -> dict[str, Any]:
    anchor_trace = _as_mapping(replay.get("anchor_trace"))
    resolved = _as_mapping(anchor_trace.get("resolved"))
    anchors = _as_mapping(anchor_trace.get("anchors"))
    review_anchor = _as_mapping(anchors.get("review"))
    review_fact_anchor = _as_mapping(review_anchor.get("fact_anchor"))
    fact_anchor = _as_mapping(anchors.get("fact"))
    fact_available = bool(
        review_fact_anchor.get("available")
        or fact_anchor.get("available")
        or str(resolved.get("fact_id") or "").strip()
        or str(resolved.get("fact_snapshot_id") or "").strip()
    )
    hard_source_expected = resolved.get("hard_source_signal_expected")
    if hard_source_expected is None:
        hard_source_expected = review_anchor.get("hard_source_signal_expected")
    return {
        "schema": "daily_review_replay_anchor_state_v1",
        "fact_anchor_available": fact_available,
        "fact_id": str(resolved.get("fact_id") or review_fact_anchor.get("fact_id") or fact_anchor.get("fact_id") or ""),
        "fact_snapshot_id": str(
            resolved.get("fact_snapshot_id")
            or review_fact_anchor.get("fact_snapshot_id")
            or fact_anchor.get("fact_snapshot_id")
            or ""
        ),
        "posterior_run_id": str(
            resolved.get("posterior_run_id")
            or review_fact_anchor.get("posterior_run_id")
            or fact_anchor.get("posterior_run_id")
            or ""
        ),
        "hard_source_signal_expected": hard_source_expected if hard_source_expected is not None else None,
        "anchor_not_applicable": bool(resolved.get("anchor_not_applicable")),
        "anchor_not_applicable_reason": str(resolved.get("anchor_not_applicable_reason") or ""),
        "warnings": list(replay.get("warnings") or [])[:10],
        "blockers": list(replay.get("blockers") or [])[:10],
    }


def _replay_fact_source_required(replay: Mapping[str, Any], anchor_state: Mapping[str, Any]) -> bool:
    if not bool(replay.get("ok")):
        return True
    if bool(anchor_state.get("fact_anchor_available")):
        return False
    hard_source_expected = anchor_state.get("hard_source_signal_expected")
    if hard_source_expected is False:
        return False
    return True


def _audit_review_item(
    db: Path,
    conn: sqlite3.Connection,
    event: Mapping[str, Any],
    *,
    since_hours: float,
    replay_limit: int,
) -> dict[str, Any]:
    slug = str(event.get("slug") or "")
    event_date = _date_only(event.get("event_date"))
    settlement_actual = _float_or_none(event.get("settlement_actual"))
    latest_fact = _find_fact_for_event(conn, slug=slug, event_date=event_date)
    review = _load_review_for_event(
        conn,
        slug=slug,
        event_date=event_date,
        fact_snapshot_id=str(latest_fact.get("fact_snapshot_id") or ""),
    )
    blockers: list[str] = []
    warnings: list[str] = []
    if not latest_fact.get("available"):
        blockers.append(str(latest_fact.get("reason") or "fact_snapshot_not_found"))
    if not review.get("available"):
        blockers.append(str(review.get("reason") or "daily_review_missing"))
    review_json = _as_mapping(review.get("review_json"))
    review_fact_snapshot_id = _first_text(review.get("fact_snapshot_id"))
    referenced_fact = {}
    if review_fact_snapshot_id:
        referenced_fact = _find_fact_for_event(
            conn,
            slug=slug,
            event_date=event_date,
            fact_snapshot_id=review_fact_snapshot_id,
        )
        if not referenced_fact.get("available"):
            blockers.append("review_fact_snapshot_not_found")
    elif review.get("available"):
        blockers.append("review_fact_snapshot_id_missing")

    if review.get("available") and not _first_text(review.get("first_bad_link")):
        blockers.append("first_bad_link_missing")
    if review.get("available") and not _review_json_has(review, "calibration_metrics"):
        blockers.append("calibration_metrics_missing")
    if review.get("available") and not _review_json_has(review, "error_attribution"):
        blockers.append("error_attribution_missing")
    if review.get("available") and not _review_json_has(review, "daily_calibration_record"):
        blockers.append("daily_calibration_record_missing")
    review_actual = _review_settlement_actual(review)
    if review.get("available") and review_actual is None:
        blockers.append("review_settlement_actual_missing")
    elif review.get("available") and not _settlement_matches(settlement_actual, review_actual):
        blockers.append("review_settlement_actual_mismatch")
    if review.get("available") and not _as_mapping(review_json.get("decision_summary")):
        blockers.append("decision_summary_missing")
    if review.get("available") and not _as_mapping(review_json.get("execution_summary")):
        blockers.append("execution_summary_missing")
    chain_node_trace = _as_mapping(review_json.get("chain_node_trace"))
    chain_nodes = [
        dict(_as_mapping(item))
        for item in chain_node_trace.get("nodes") or []
        if isinstance(item, Mapping)
    ]
    if review.get("available") and not chain_node_trace:
        blockers.append("chain_node_trace_missing")
    elif review.get("available"):
        node_ids = [str(node.get("node_id") or "").strip() for node in chain_nodes]
        missing_required_nodes = [node_id for node_id in CHAIN_NODE_ORDER if node_id not in node_ids]
        if missing_required_nodes:
            blockers.append("chain_node_trace_required_nodes_missing")
        if not chain_nodes:
            blockers.append("chain_node_trace_nodes_missing")
        for node in chain_nodes:
            if not _first_text(node.get("terminal_state")):
                blockers.append("chain_node_trace_terminal_state_missing")
                break
            if not isinstance(node.get("input_refs"), Mapping):
                blockers.append("chain_node_trace_input_refs_missing")
                break
            if not isinstance(node.get("output_refs"), Mapping):
                blockers.append("chain_node_trace_output_refs_missing")
                break
            if not isinstance(node.get("timestamps"), Mapping):
                blockers.append("chain_node_trace_timestamps_missing")
                break
    if review.get("available") and int(review_json.get("posterior_row_count") or 0) <= 0:
        blockers.append("posterior_summary_missing")
    field_capability = _review_field_capability(review, review_json)
    if review.get("available"):
        blockers.extend(_bias_candidate_contract_blockers(review_json))
    posterior_lineage = _posterior_lineage_from_review(review_json)
    review_posterior_run_id = _first_text(
        posterior_lineage.get("posterior_run_id"),
        review_json.get("posterior_run_id"),
    )
    latest_posterior_run_id = _first_text(latest_fact.get("posterior_run_id"))
    if review.get("available") and not posterior_lineage:
        blockers.append("posterior_lineage_missing")
    if review.get("available") and posterior_lineage and posterior_lineage.get("ok") is False:
        blockers.append("posterior_lineage_failed")
    if latest_fact.get("posterior_ssot_fact_present") and posterior_lineage:
        if _first_text(posterior_lineage.get("posterior_target_source")) != "posterior_ssot_fact":
            blockers.append("posterior_lineage_not_from_posterior_ssot_fact")
    if latest_posterior_run_id and review_posterior_run_id and latest_posterior_run_id != review_posterior_run_id:
        blockers.append("review_posterior_run_id_mismatch")

    replay_compact: dict[str, Any] = {}
    review_id = _first_text(review.get("review_id"))
    if review_id:
        replay = ssot_chain_replay_audit.audit(
            db_path=db,
            review_id=review_id,
            since_hours=since_hours,
            limit=replay_limit,
            max_signals_output=0,
        )
        replay_anchor_state = _replay_anchor_state(replay)
        replay_compact = {
            "ok": bool(replay.get("ok")),
            "signal_count": replay.get("signal_count"),
            "source_counts": dict(_as_mapping(replay.get("source_counts"))),
            "anchor_state": replay_anchor_state,
            "warnings": list(replay.get("warnings") or [])[:10],
            "blockers": list(replay.get("blockers") or [])[:10],
        }
        if not replay.get("ok"):
            blockers.append("review_replay_failed")
        source_counts = _as_mapping(replay.get("source_counts"))
        if int(source_counts.get("event_fact_history") or 0) <= 0 and _replay_fact_source_required(
            replay,
            replay_anchor_state,
        ):
            blockers.append("review_replay_fact_missing")
    elif review.get("available"):
        blockers.append("review_id_missing")

    return {
        "slug": slug,
        "event_date": event_date,
        "settlement_actual": settlement_actual,
        "event_run_id": event.get("id"),
        "latest_fact": latest_fact,
        "review_id": review_id,
        "review_fact_snapshot_id": review_fact_snapshot_id,
        "review_fact_snapshot_matches_latest": bool(
            review_fact_snapshot_id
            and review_fact_snapshot_id == str(latest_fact.get("fact_snapshot_id") or "")
        ),
        "review_posterior_run_id": review_posterior_run_id,
        "latest_fact_posterior_run_id": latest_posterior_run_id,
        "review_posterior_run_matches_latest": bool(
            review_posterior_run_id
            and latest_posterior_run_id
            and review_posterior_run_id == latest_posterior_run_id
        ),
        "posterior_lineage": dict(posterior_lineage),
        "referenced_fact_available": bool(referenced_fact.get("available")),
        "first_bad_link": review.get("first_bad_link"),
        "review_settlement_actual": review_actual,
        "review_replay": replay_compact,
        "review_field_capability": _compact_review_field_capability(field_capability),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
        "ok": not blockers,
    }


def build_report(
    db_path: Path | str = DEFAULT_DB,
    *,
    since_days: int = 45,
    since_date: str = "",
    slug: str = "",
    event_date: str = "",
    market_scope: str = "highest_temperature",
    limit: int = 100,
    max_items_output: int = 20,
    since_hours: float = 24 * 90,
    replay_limit: int = 500,
    lineage_decision_manifest: Mapping[str, Any] | None = None,
    require_historical_regression: bool = False,
) -> dict[str, Any]:
    db = Path(db_path)
    effective_since_date = _date_only(since_date) or _since_date_from_days(int(since_days))
    blockers: list[str] = []
    warnings: list[str] = []
    with _connect_readonly(db) as conn:
        schema_status = daily_review_persistence_schema_status(conn)
        event_runs_exists = _table_exists(conn, "event_runs")
        raw_settled_event_total_count = _count_settled_event_keys(
            conn,
            since_date=effective_since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
        )
        raw_scanned_events = _load_settled_events(
            conn,
            since_date=effective_since_date,
            slug=slug,
            event_date=event_date,
            market_scope=market_scope,
            limit=limit,
        )
        exclusion_rows = _manifest_decision_rows(lineage_decision_manifest)
        manifest_scope_events = raw_scanned_events
        if exclusion_rows and raw_settled_event_total_count > len(raw_scanned_events):
            manifest_scope_events = _load_settled_events(
                conn,
                since_date=effective_since_date,
                slug=slug,
                event_date=event_date,
                market_scope=market_scope,
                limit=max(int(limit), int(raw_settled_event_total_count)),
            )
        _unused_effective_scope, excluded_settled_events = _split_manifest_excluded_events(
            manifest_scope_events,
            exclusion_rows,
        )
        settled_events, excluded_scanned_events = _split_manifest_excluded_events(
            raw_scanned_events,
            exclusion_rows,
        )
        lineage_decision_manifest_audit = _lineage_decision_manifest_audit(
            lineage_decision_manifest=lineage_decision_manifest,
            exclusion_rows=exclusion_rows,
            excluded_settled_events=excluded_settled_events,
            excluded_scanned_events=excluded_scanned_events,
        )
        if not event_runs_exists:
            blockers.append("event_runs_missing")
        if settled_events and not _table_exists(conn, "daily_weather_reviews"):
            blockers.append("daily_weather_reviews_missing_planned_table")
        items = [
            _audit_review_item(db, conn, event, since_hours=since_hours, replay_limit=replay_limit)
            for event in settled_events
        ]
    settled_count = len(settled_events)
    excluded_settled_event_count = int(lineage_decision_manifest_audit.get("excluded_settled_event_count") or 0)
    settled_event_total_count = max(0, int(raw_settled_event_total_count) - excluded_settled_event_count)
    coverage_scope_truncated_count = max(0, int(settled_event_total_count) - settled_count)
    coverage_scope_complete = coverage_scope_truncated_count == 0
    if not coverage_scope_complete:
        blockers.append("daily_review_coverage_scope_truncated")
        warnings.append("daily_review_coverage_limit_below_settled_scope")
    reviewed_count = sum(1 for item in items if item.get("review_id"))
    missing_review_count = max(0, settled_count - reviewed_count)
    item_blocker_counts: dict[str, int] = {}
    for item in items:
        for blocker in item.get("blockers") or []:
            item_blocker_counts[str(blocker)] = item_blocker_counts.get(str(blocker), 0) + 1
    blockers.extend(sorted(item_blocker_counts))
    field_capability_matrix = _aggregate_review_field_capability(items)
    historical_regression = _historical_settled_date_regression(
        required=bool(require_historical_regression),
        slug=slug,
        event_date=event_date,
        raw_settled_event_total_count=int(raw_settled_event_total_count),
        settled_event_total_count=int(settled_event_total_count),
        settled_count=settled_count,
        coverage_scope_complete=coverage_scope_complete,
        reviewed_count=reviewed_count,
        missing_review_count=missing_review_count,
        chain_blocked_count=sum(1 for item in items if not item.get("ok")),
        items=items,
        field_capability_matrix=field_capability_matrix,
    )
    if require_historical_regression and not historical_regression.get("ok"):
        blockers.extend(list(historical_regression.get("blockers") or []))
    blockers = sorted(set(blockers))
    visible_limit = max(0, int(max_items_output))
    visible_items = items[:visible_limit] if visible_limit else []
    return {
        "ok": not blockers,
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "db": str(db),
        "since_date": effective_since_date,
        "market_scope": market_scope,
        "requested_limit": int(limit),
        "raw_settled_event_total_count": int(raw_settled_event_total_count),
        "raw_settled_event_scanned_count": len(raw_scanned_events),
        "settled_event_total_count": int(settled_event_total_count),
        "settled_event_count": settled_count,
        "settled_event_scanned_count": settled_count,
        "excluded_settled_event_count": excluded_settled_event_count,
        "excluded_settled_event_scanned_count": int(
            lineage_decision_manifest_audit.get("excluded_settled_event_scanned_count") or 0
        ),
        "excluded_samples": list(lineage_decision_manifest_audit.get("excluded_samples") or []),
        "lineage_decision_manifest_audit": lineage_decision_manifest_audit,
        "coverage_scope_complete": coverage_scope_complete,
        "coverage_scope_truncated_count": coverage_scope_truncated_count,
        "reviewed_event_count": reviewed_count,
        "missing_review_count": missing_review_count,
        "coverage_ratio": (reviewed_count / settled_count) if settled_count else None,
        "chain_ok_count": sum(1 for item in items if item.get("ok")),
        "chain_blocked_count": sum(1 for item in items if not item.get("ok")),
        "review_field_capability_matrix": field_capability_matrix,
        "historical_settled_date_regression": historical_regression,
        "item_blocker_counts": item_blocker_counts,
        "daily_review_persistence_schema_status": schema_status,
        "blockers": blockers,
        "warnings": sorted(set(warnings)),
        "items": visible_items,
        "items_truncated": len(items) > len(visible_items),
        "guards": {
            "writes_archive_db": False,
            "writes_daily_weather_reviews": False,
            "writes_bias_correction_candidates": False,
            "writes_parameter_change_events": False,
            "historical_backfill": False,
            "fetches_external_weather_sources": False,
            "places_orders": False,
            "restarts_launchd": False,
            "changes_parameters": False,
        },
        "next_boundary": (
            "apply_daily_review_schema_then_persist_reviews"
            if "daily_weather_reviews_missing_planned_table" in blockers
            else "fix_first_daily_review_coverage_blocker"
            if blockers
            else "wire_daily_review_coverage_audit_into_phase8_acceptance"
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since-days", type=int, default=45)
    parser.add_argument("--since-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--since-hours", type=float, default=24 * 90)
    parser.add_argument("--replay-limit", type=int, default=500)
    parser.add_argument("--lineage-decision-manifest", type=Path, default=None)
    parser.add_argument("--require-historical-regression", action="store_true")
    args = parser.parse_args(argv)
    lineage_decision_manifest = _load_lineage_decision_manifest_file(args.lineage_decision_manifest)
    report = build_report(
        args.db,
        since_days=int(args.since_days),
        since_date=args.since_date,
        slug=args.slug,
        event_date=args.event_date,
        market_scope=args.market_scope,
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        since_hours=float(args.since_hours),
        replay_limit=int(args.replay_limit),
        lineage_decision_manifest=lineage_decision_manifest,
        require_historical_regression=bool(args.require_historical_regression),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
