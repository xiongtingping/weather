#!/usr/bin/env python3
"""Read-only production identity merge-fill plan.

The plan derives deterministic IDs for production rows that still have empty
lineage columns, checks uniqueness risks, and emits an operator-facing
no-apply contract. It never writes archive.db.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lineage_identity_runtime import (  # noqa: E402
    build_fact_snapshot_id,
    build_correlation_id,
    build_source_observation_id,
    lineage_id_from_parts,
)
from scripts import phase0_schema_probe  # noqa: E402
from scripts import production_identity_coverage_audit  # noqa: E402
from scripts import production_identity_duplicate_fact_snapshot_packet  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "production_identity_merge_fill_plan_v1"
CONFIRMATION_PHRASE = "APPLY_PRODUCTION_IDENTITY_MERGE_FILL"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_auto_trade_decisions": False,
        "writes_auto_trade_jobs": False,
        "writes_auto_orders": False,
        "writes_source_observations": False,
        "writes_manifest_file": False,
        "executes_sql_writes": False,
        "creates_backup": False,
        "historical_backfill": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "changes_parameters": False,
    }


def _connect(db: Path, *, immutable: bool) -> sqlite3.Connection:
    uri = f"file:{db}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _json_loads_map(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _first_text_from_mappings(*mappings: Mapping[str, Any], keys: Sequence[str]) -> str:
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            continue
        for key in keys:
            text = _text(mapping.get(key))
            if text:
                return text
    return ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_empty(value: Any) -> bool:
    return _text(value) == ""


def _nonnull(value: Any) -> Any:
    return None if _is_empty(value) else value


def _q(value: Path | str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _load_json_file(path: str | os.PathLike[str] | None) -> dict[str, Any] | None:
    if not path:
        return None
    payload_path = Path(path).expanduser()
    with payload_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload) if isinstance(payload, Mapping) else {}


def _existing_nonempty(row: sqlite3.Row, field: str) -> str:
    return _text(row[field]) if field in row.keys() else ""


def _set_if_empty(row: sqlite3.Row, fields: dict[str, Any], field: str, value: Any) -> None:
    if field not in row.keys():
        return
    if _existing_nonempty(row, field):
        return
    text = _text(value)
    if text:
        fields[field] = text


def _source_event_ids_from_payload(payload: Mapping[str, Any]) -> list[str]:
    values = payload.get("source_event_ids")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        return [_text(item) for item in values if _text(item)]
    trace = payload.get("source_acquisition_trace")
    if isinstance(trace, Mapping):
        trace_values = trace.get("source_event_ids")
        if isinstance(trace_values, Sequence) and not isinstance(trace_values, (str, bytes, bytearray)):
            return [_text(item) for item in trace_values if _text(item)]
    return []


def _fact_snapshot_id_for_history_row(row: sqlite3.Row | Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    payload_id = _text(payload.get("fact_snapshot_id"))
    if payload_id:
        return payload_id
    column_id = _text(row["fact_snapshot_id"]) if "fact_snapshot_id" in row.keys() else ""
    if column_id:
        return column_id
    source_event_ids = _source_event_ids_from_payload(payload)
    slug = _text(row["slug"]) if "slug" in row.keys() else ""
    event_date = _text(row["event_date"])[:10] if "event_date" in row.keys() else ""
    fact_as_of = _text(row["fact_as_of"]) if "fact_as_of" in row.keys() else ""
    if source_event_ids:
        return build_fact_snapshot_id(
            slug=slug,
            event_date=event_date,
            fact_as_of=fact_as_of,
            source_event_ids=source_event_ids,
        )
    legacy_row_id = row["id"] if "id" in row.keys() else ""
    return lineage_id_from_parts(
        "fact_snapshot_id",
        {
            "slug": slug,
            "event_date": event_date,
            "fact_as_of": fact_as_of,
            "legacy_row_id": legacy_row_id,
        },
    )


def _build_event_fact_history_proposals(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    where = "fact_snapshot_id IS NULL OR trim(CAST(fact_snapshot_id AS TEXT)) = '' OR correlation_id IS NULL OR trim(CAST(correlation_id AS TEXT)) = ''"
    rows = conn.execute(
        f"""
        SELECT
            id, slug, event_date, fact_as_of, fact_json,
            correlation_id, source_event_id, fact_snapshot_id, posterior_run_id
        FROM event_fact_history
        WHERE {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    proposals: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_loads_map(row["fact_json"])
        fact_snapshot_id = _fact_snapshot_id_for_history_row(row, payload)
        posterior_run_id = _text(payload.get("posterior_run_id"))
        source_event_ids = _source_event_ids_from_payload(payload)
        set_fields: dict[str, Any] = {}
        _set_if_empty(row, set_fields, "fact_snapshot_id", fact_snapshot_id)
        _set_if_empty(row, set_fields, "posterior_run_id", posterior_run_id)
        _set_if_empty(row, set_fields, "source_event_id", source_event_ids[0] if source_event_ids else "")
        corr = _text(payload.get("correlation_id")) or build_correlation_id(
            "event_fact_history",
            {
                "id": row["id"],
                "slug": row["slug"],
                "event_date": row["event_date"],
                "fact_as_of": row["fact_as_of"],
                "fact_snapshot_id": fact_snapshot_id,
            },
        )
        _set_if_empty(row, set_fields, "correlation_id", corr)
        blockers = []
        proposals.append(
            {
                "table": "event_fact_history",
                "primary_key": {"id": row["id"]},
                "natural_key": {
                    "slug": row["slug"],
                    "event_date": row["event_date"],
                    "fact_as_of": row["fact_as_of"],
                },
                "set_fields": set_fields,
                "source": "fact_json_payload_plus_lineage_identity_runtime",
                "blockers": blockers,
            }
        )
    return proposals


def _history_payload_by_id(conn: sqlite3.Connection, fact_ids: Sequence[Any]) -> dict[int, dict[str, Any]]:
    ids = [int(item) for item in fact_ids if str(item).strip().isdigit()]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    out: dict[int, dict[str, Any]] = {}
    for row in conn.execute(
        f"""
        SELECT id, slug, event_date, fact_as_of, fact_json, fact_snapshot_id
        FROM event_fact_history
        WHERE id IN ({placeholders})
        """,
        ids,
    ).fetchall():
        payload = _json_loads_map(row["fact_json"])
        payload["_identity_fact_snapshot_id"] = _fact_snapshot_id_for_history_row(row, payload)
        out[int(row["id"])] = payload
    return out


def _build_event_fact_latest_proposals(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            slug, event_date, fact_id, updated_at,
            fact_snapshot_id, correlation_id
        FROM event_fact_latest
        WHERE fact_snapshot_id IS NULL OR trim(CAST(fact_snapshot_id AS TEXT)) = ''
           OR correlation_id IS NULL OR trim(CAST(correlation_id AS TEXT)) = ''
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    payload_by_id = _history_payload_by_id(conn, [row["fact_id"] for row in rows])
    proposals: list[dict[str, Any]] = []
    for row in rows:
        payload = payload_by_id.get(int(row["fact_id"] or 0), {})
        fact_snapshot_id = _text(payload.get("fact_snapshot_id")) or _text(payload.get("_identity_fact_snapshot_id"))
        set_fields: dict[str, Any] = {}
        _set_if_empty(row, set_fields, "fact_snapshot_id", fact_snapshot_id)
        corr = _text(payload.get("correlation_id")) or build_correlation_id(
            "event_fact_latest",
            {
                "slug": row["slug"],
                "event_date": row["event_date"],
                "fact_id": row["fact_id"],
                "fact_snapshot_id": fact_snapshot_id,
            },
        )
        _set_if_empty(row, set_fields, "correlation_id", corr)
        blockers = []
        proposals.append(
            {
                "table": "event_fact_latest",
                "primary_key": {"slug": row["slug"], "event_date": row["event_date"]},
                "natural_key": {"fact_id": row["fact_id"], "updated_at": row["updated_at"]},
                "set_fields": set_fields,
                "source": "referenced_event_fact_history_payload_plus_lineage_identity_runtime",
                "blockers": blockers,
            }
        )
    return proposals


def _build_decision_proposals(
    conn: sqlite3.Connection,
    limit: int,
    *,
    required_fields: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    required = {str(field or "").strip() for field in required_fields or [] if str(field or "").strip()}
    optional_json_fields = {"signal_id", "order_intent_id", "fact_snapshot_id", "posterior_run_id"}
    scan_optional_json = bool(not required or required.intersection(optional_json_fields))
    where_parts = [
        "decision_id IS NULL OR trim(CAST(decision_id AS TEXT)) = ''",
        "correlation_id IS NULL OR trim(CAST(correlation_id AS TEXT)) = ''",
    ]
    if scan_optional_json:
        where_parts.extend(
            [
                """
                (
                    (signal_id IS NULL OR trim(CAST(signal_id AS TEXT)) = '')
                    AND details_json LIKE '%"signal_id"%'
                )
                """,
                """
                (
                    (order_intent_id IS NULL OR trim(CAST(order_intent_id AS TEXT)) = '')
                    AND details_json LIKE '%"order_intent_id"%'
                )
                """,
                """
                (
                    (fact_snapshot_id IS NULL OR trim(CAST(fact_snapshot_id AS TEXT)) = '')
                    AND details_json LIKE '%"fact_snapshot_id"%'
                )
                """,
                """
                (
                    (posterior_run_id IS NULL OR trim(CAST(posterior_run_id AS TEXT)) = '')
                    AND details_json LIKE '%"posterior_run_id"%'
                )
                """,
            ]
        )
    where_sql = "\n           OR ".join(f"({part})" for part in where_parts)
    select_fields = ["id", "decision_id", "correlation_id"]
    if scan_optional_json:
        select_fields.extend(["signal_id", "order_intent_id", "fact_snapshot_id", "posterior_run_id", "details_json"])
    select_sql = ", ".join(f'"{field}"' for field in select_fields)
    fast_missing_ids: list[Any] = []
    if not scan_optional_json:
        fast_missing_ids = _ids_missing_partial_nonempty_fields(
            conn,
            table="auto_trade_decisions",
            id_field="id",
            fields=("decision_id", "correlation_id"),
            limit=limit,
        )
    if fast_missing_ids:
        rows = _rows_by_ids(
            conn,
            table="auto_trade_decisions",
            id_field="id",
            select_fields=select_fields,
            ids=fast_missing_ids,
        )
    else:
        rows = conn.execute(
            f"""
            SELECT
                {select_sql}
            FROM auto_trade_decisions
            WHERE {where_sql}
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    proposals: list[dict[str, Any]] = []
    for row in rows:
        details: dict[str, Any] = {}
        nested_timing: Mapping[str, Any] = {}
        if scan_optional_json:
            details = _json_loads_map(row["details_json"])
            nested_timing = details.get("signal_to_order_timing")
            if not isinstance(nested_timing, Mapping):
                nested_timing = {}
        decision_id = lineage_id_from_parts(
            "decision_id",
            {"table": "auto_trade_decisions", "id": row["id"]},
        )
        set_fields: dict[str, Any] = {}
        _set_if_empty(row, set_fields, "decision_id", decision_id)
        if scan_optional_json:
            _set_if_empty(row, set_fields, "signal_id", _first_text_from_mappings(details, nested_timing, keys=("signal_id",)))
            _set_if_empty(
                row,
                set_fields,
                "order_intent_id",
                _first_text_from_mappings(details, nested_timing, keys=("order_intent_id",)),
            )
            _set_if_empty(
                row,
                set_fields,
                "fact_snapshot_id",
                _first_text_from_mappings(
                    details,
                    nested_timing,
                    keys=("fact_snapshot_id", "posterior_target_fact_snapshot_id", "primary_fact_snapshot_id"),
                ),
            )
            _set_if_empty(
                row,
                set_fields,
                "posterior_run_id",
                _first_text_from_mappings(
                    details,
                    nested_timing,
                    keys=("posterior_run_id", "posterior_target_posterior_run_id"),
                ),
            )
        corr = build_correlation_id(
            "auto_trade_decision",
            {"decision_id": decision_id},
        )
        _set_if_empty(row, set_fields, "correlation_id", corr)
        proposals.append(
            {
                "table": "auto_trade_decisions",
                "primary_key": {"id": row["id"]},
                "natural_key": {"id": row["id"], "display_scope": "identity_fast_scan"},
                "set_fields": set_fields,
                "source": "stable_decision_row_natural_key",
                "blockers": [],
            }
        )
    return proposals


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _decision_identity_by_signal(
    conn: sqlite3.Connection,
    signal_ids: Sequence[str],
) -> dict[str, dict[str, str]]:
    if "signal_id" not in _table_columns(conn, "auto_trade_decisions"):
        return {}
    wanted = list(dict.fromkeys(_text(signal_id) for signal_id in signal_ids if _text(signal_id)))
    if not wanted:
        return {}
    rows = conn.execute(
        f"""
        SELECT id, decision_id, signal_id, order_intent_id, details_json
        FROM auto_trade_decisions
        WHERE signal_id IN ({_placeholders(wanted)})
        ORDER BY id DESC
        """,
        wanted,
    ).fetchall()
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)

    def add_row(row: sqlite3.Row) -> None:
        details = _json_loads_map(row["details_json"])
        nested_timing = details.get("signal_to_order_timing")
        if not isinstance(nested_timing, Mapping):
            nested_timing = {}
        signal_id = _text(row["signal_id"]) or _first_text_from_mappings(details, nested_timing, keys=("signal_id",))
        if not signal_id:
            return
        decision_id = _text(row["decision_id"]) or lineage_id_from_parts(
            "decision_id",
            {"table": "auto_trade_decisions", "id": row["id"]},
        )
        grouped[signal_id].append(
            {
                "decision_id": decision_id,
                "order_intent_id": _text(row["order_intent_id"])
                or _first_text_from_mappings(details, nested_timing, keys=("order_intent_id",)),
            }
        )

    for row in rows:
        add_row(row)
    missing = [signal_id for signal_id in wanted if signal_id not in grouped]
    for signal_id in missing:
        fallback_rows = conn.execute(
            """
            SELECT id, decision_id, signal_id, order_intent_id, details_json
            FROM auto_trade_decisions
            WHERE details_json LIKE ?
            ORDER BY id DESC
            """,
            (f"%{signal_id}%",),
        ).fetchall()
        for row in fallback_rows:
            add_row(row)
    return {signal_id: rows[0] for signal_id, rows in grouped.items() if rows}


def _build_signal_timing_proposals(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    if "signal_to_order_timing" not in phase0_schema_probe.TABLE_ID_CONSTRAINTS:
        return []
    rows = conn.execute(
        """
        SELECT signal_id, decision_id, order_intent_id, created_at
        FROM signal_to_order_timing
        WHERE signal_id IS NOT NULL AND trim(CAST(signal_id AS TEXT)) <> ''
          AND (
               decision_id IS NULL OR trim(CAST(decision_id AS TEXT)) = ''
            OR order_intent_id IS NULL OR trim(CAST(order_intent_id AS TEXT)) = ''
          )
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return []
    identity_by_signal = _decision_identity_by_signal(
        conn,
        [_text(row["signal_id"]) for row in rows],
    )
    proposals: list[dict[str, Any]] = []
    for row in rows:
        identity = identity_by_signal.get(_text(row["signal_id"]), {})
        set_fields: dict[str, Any] = {}
        _set_if_empty(row, set_fields, "decision_id", identity.get("decision_id"))
        _set_if_empty(row, set_fields, "order_intent_id", identity.get("order_intent_id"))
        proposals.append(
            {
                "table": "signal_to_order_timing",
                "primary_key": {"signal_id": row["signal_id"]},
                "natural_key": {"signal_id": row["signal_id"], "created_at": row["created_at"]},
                "set_fields": set_fields,
                "source": "auto_trade_decisions_signal_identity",
                "blockers": [],
            }
        )
    return proposals


def _build_job_proposals(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    fast_missing_ids = _ids_missing_partial_nonempty_fields(
        conn,
        table="auto_trade_jobs",
        id_field="id",
        fields=("order_intent_id", "correlation_id"),
        limit=limit,
    )
    if fast_missing_ids:
        rows = _rows_by_ids(
            conn,
            table="auto_trade_jobs",
            id_field="id",
            select_fields=("id", "order_intent_id", "correlation_id"),
            ids=fast_missing_ids,
        )
    else:
        rows = conn.execute(
            """
            SELECT
                id, order_intent_id, correlation_id
            FROM auto_trade_jobs
            WHERE order_intent_id IS NULL OR trim(CAST(order_intent_id AS TEXT)) = ''
               OR correlation_id IS NULL OR trim(CAST(correlation_id AS TEXT)) = ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    proposals: list[dict[str, Any]] = []
    for row in rows:
        order_intent_id = lineage_id_from_parts(
            "order_intent_id",
            {"table": "auto_trade_jobs", "id": row["id"]},
        )
        set_fields: dict[str, Any] = {}
        _set_if_empty(row, set_fields, "order_intent_id", order_intent_id)
        corr = build_correlation_id("auto_trade_job", {"order_intent_id": order_intent_id})
        _set_if_empty(row, set_fields, "correlation_id", corr)
        proposals.append(
            {
                "table": "auto_trade_jobs",
                "primary_key": {"id": row["id"]},
                "natural_key": {"id": row["id"], "display_scope": "identity_fast_scan"},
                "set_fields": set_fields,
                "source": "stable_job_row_natural_key",
                "blockers": [],
            }
        )
    return proposals


def _build_order_proposals(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id, order_id, slug, event_date, outcome, side, action, created_at,
            order_intent_id, correlation_id
        FROM auto_orders
        WHERE order_intent_id IS NULL OR trim(CAST(order_intent_id AS TEXT)) = ''
           OR correlation_id IS NULL OR trim(CAST(correlation_id AS TEXT)) = ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    proposals: list[dict[str, Any]] = []
    for row in rows:
        order_intent_id = lineage_id_from_parts(
            "order_intent_id",
            {
                "table": "auto_orders",
                "id": row["id"],
                "order_id": row["order_id"],
            },
        )
        set_fields: dict[str, Any] = {}
        _set_if_empty(row, set_fields, "order_intent_id", order_intent_id)
        corr = build_correlation_id(
            "auto_order",
            {"order_id": row["order_id"], "order_intent_id": order_intent_id},
        )
        _set_if_empty(row, set_fields, "correlation_id", corr)
        proposals.append(
            {
                "table": "auto_orders",
                "primary_key": {"id": row["id"]},
                "natural_key": {
                    "order_id": row["order_id"],
                    "slug": row["slug"],
                    "event_date": row["event_date"],
                    "outcome": row["outcome"],
                    "side": row["side"],
                    "action": row["action"],
                    "created_at": row["created_at"],
                },
                "set_fields": set_fields,
                "source": "stable_order_row_natural_key",
                "blockers": [],
            }
        )
    return proposals


def _build_source_observation_proposals(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    select_sql = _select_sql_for_existing_columns(
        conn,
        "source_observations",
        (
            "source_event_id",
            "source_id",
            "station_id",
            "event_date",
            "valid_time",
            "valid_time_local",
            "fetched_at",
            "temperature_c",
            "dewpoint_c",
            "observed_today_max_c",
            "forecast_peak_c",
            "raw_ref",
            "raw_sha256",
            "source_observation_id",
            "created_at",
        ),
    )
    rows = conn.execute(
        f"""
        SELECT {select_sql}
        FROM source_observations
        WHERE source_observation_id IS NULL OR trim(CAST(source_observation_id AS TEXT)) = ''
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    proposals: list[dict[str, Any]] = []
    for row in rows:
        source_observation_id = build_source_observation_id(
            source_id=_text(row["source_id"]),
            station_id=_text(row["station_id"]),
            event_date=_text(row["event_date"]),
            valid_time=_text(row["valid_time"]),
            valid_time_local=_text(row["valid_time_local"]) if "valid_time_local" in row.keys() else "",
            fetched_at=_text(row["fetched_at"]),
            temperature_c=_nonnull(row["temperature_c"]),
            dewpoint_c=_nonnull(row["dewpoint_c"]) if "dewpoint_c" in row.keys() else None,
            observed_today_max_c=_nonnull(row["observed_today_max_c"]) if "observed_today_max_c" in row.keys() else None,
            forecast_peak_c=_nonnull(row["forecast_peak_c"]) if "forecast_peak_c" in row.keys() else None,
            raw_ref=_text(row["raw_ref"]),
            raw_sha256=_text(row["raw_sha256"]),
        )
        set_fields: dict[str, Any] = {}
        _set_if_empty(row, set_fields, "source_observation_id", source_observation_id)
        proposals.append(
            {
                "table": "source_observations",
                "primary_key": {"source_event_id": row["source_event_id"]},
                "natural_key": {
                    "source_id": row["source_id"],
                    "station_id": row["station_id"],
                    "event_date": row["event_date"],
                    "valid_time": row["valid_time"],
                    "fetched_at": row["fetched_at"],
                },
                "set_fields": set_fields,
                "source": "source_observation_runtime_natural_key",
                "blockers": [],
            }
        )
    return proposals


def _proposal_key(table: str, proposal: Mapping[str, Any]) -> tuple[Any, ...]:
    primary = proposal.get("primary_key")
    if not isinstance(primary, Mapping):
        return (table, json.dumps(primary, sort_keys=True, default=str))
    return tuple([table, *[(key, primary.get(key)) for key in sorted(primary)]])


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"] or "") for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _select_existing_columns(conn: sqlite3.Connection, table: str, columns: Sequence[str]) -> list[str]:
    available = _table_columns(conn, table)
    return [str(column) for column in columns if str(column) in available]


def _select_sql_for_existing_columns(conn: sqlite3.Connection, table: str, columns: Sequence[str]) -> str:
    selected = _select_existing_columns(conn, table, columns)
    if not selected:
        return "*"
    return ", ".join(f'"{column}"' for column in selected)


def _ids_missing_partial_nonempty_fields(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_field: str,
    fields: Sequence[str],
    limit: int,
) -> list[Any]:
    ids: list[Any] = []
    seen: set[Any] = set()
    table_quoted = production_identity_coverage_audit._quote_identifier(table)
    id_quoted = production_identity_coverage_audit._quote_identifier(id_field)
    for field in fields:
        field_text = str(field or "").strip()
        if not field_text:
            continue
        index_name = production_identity_coverage_audit._partial_nonempty_index(
            conn,
            table=table,
            column=field_text,
        )
        if not index_name:
            continue
        index_quoted = production_identity_coverage_audit._quote_identifier(index_name)
        predicate = production_identity_coverage_audit._simple_nonempty_predicate(field_text)
        max_row = conn.execute(f"SELECT MAX({id_quoted}) AS max_id FROM {table_quoted}").fetchone()
        max_indexed_row = conn.execute(
            f"""
            SELECT MAX({id_quoted}) AS max_id
            FROM {table_quoted} INDEXED BY {index_quoted}
            WHERE {predicate}
            """
        ).fetchone()
        max_id = max_row["max_id"] if max_row else None
        max_indexed_id = max_indexed_row["max_id"] if max_indexed_row else None
        if max_id is not None and (max_indexed_id is None or max_id > max_indexed_id):
            tail_rows = conn.execute(
                f"""
                SELECT {id_quoted} AS id
                FROM {table_quoted}
                WHERE {id_quoted} > ?
                ORDER BY {id_quoted} DESC
                LIMIT ?
                """,
                (max_indexed_id or 0, max(1, int(limit))),
            ).fetchall()
            for row in tail_rows:
                row_id = row["id"]
                if row_id in seen:
                    continue
                seen.add(row_id)
                ids.append(row_id)
                if len(ids) >= max(1, int(limit)):
                    return ids
            if ids:
                continue
        rows = conn.execute(
            f"""
            SELECT {id_quoted} AS id
            FROM {table_quoted}
            WHERE {id_quoted} NOT IN (
                SELECT {id_quoted}
                FROM {table_quoted} INDEXED BY {index_quoted}
                WHERE {predicate}
            )
            ORDER BY {id_quoted} DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            row_id = row["id"]
            if row_id in seen:
                continue
            seen.add(row_id)
            ids.append(row_id)
            if len(ids) >= max(1, int(limit)):
                return ids
    return ids


def _rows_by_ids(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_field: str,
    select_fields: Sequence[str],
    ids: Sequence[Any],
) -> list[sqlite3.Row]:
    wanted = [item for item in ids if str(item).strip()]
    if not wanted:
        return []
    table_quoted = production_identity_coverage_audit._quote_identifier(table)
    id_quoted = production_identity_coverage_audit._quote_identifier(id_field)
    fields = list(dict.fromkeys([id_field, *select_fields]))
    select_sql = ", ".join(production_identity_coverage_audit._quote_identifier(field) for field in fields)
    placeholders = ", ".join("?" for _ in wanted)
    return conn.execute(
        f"""
        SELECT {select_sql}
        FROM {table_quoted}
        WHERE {id_quoted} IN ({placeholders})
        ORDER BY {id_quoted} DESC
        """,
        list(wanted),
    ).fetchall()


def _identity_select_fields(table: str, unique_key: Sequence[str]) -> list[str]:
    primary = production_identity_coverage_audit.IDENTITY_ROW_CONTRACT.get(table, {}).get("primary_key_fields") or ()
    return list(dict.fromkeys([*primary, *unique_key]))


def _existing_unique_values(
    conn: sqlite3.Connection,
    *,
    table: str,
    unique_key: Sequence[str],
    candidate_values: Sequence[Sequence[Any]] | None = None,
) -> dict[tuple[Any, ...], set[tuple[Any, ...]]]:
    columns = _table_columns(conn, table)
    if not set(unique_key).issubset(columns):
        return {}
    select_fields = _identity_select_fields(table, unique_key)
    if not set(select_fields).issubset(columns):
        return {}
    wanted_values = []
    seen_values: set[tuple[Any, ...]] = set()
    for values in candidate_values or ():
        value_tuple = tuple(values)
        if len(value_tuple) != len(unique_key):
            continue
        if any(_is_empty(value) for value in value_tuple):
            continue
        if value_tuple in seen_values:
            continue
        seen_values.add(value_tuple)
        wanted_values.append(value_tuple)
    if candidate_values is not None and not wanted_values:
        return {}
    select_sql = ", ".join(f'"{field}"' for field in select_fields)
    out: dict[tuple[Any, ...], set[tuple[Any, ...]]] = defaultdict(set)
    if candidate_values is None:
        where = " AND ".join(f'"{column}" IS NOT NULL AND trim(CAST("{column}" AS TEXT)) <> ""' for column in unique_key)
        rows = conn.execute(f"SELECT {select_sql} FROM {table} WHERE {where}").fetchall()
        for row in rows:
            values = tuple(row[column] for column in unique_key)
            primary = {field: row[field] for field in select_fields if field not in unique_key}
            if not primary:
                primary = {field: row[field] for field in select_fields}
            out[values].add(_proposal_key(table, {"primary_key": primary}))
        return out
    table_quoted = production_identity_coverage_audit._quote_identifier(table)
    indexed_by = ""
    extra_predicates: list[str] = []
    if len(unique_key) == 1:
        index_name = production_identity_coverage_audit._partial_nonempty_index(
            conn,
            table=table,
            column=str(unique_key[0]),
        )
        if index_name:
            indexed_by = " INDEXED BY " + production_identity_coverage_audit._quote_identifier(index_name)
            extra_predicates.append(production_identity_coverage_audit._simple_nonempty_predicate(str(unique_key[0])))
    where_parts = [f'"{column}" = ?' for column in unique_key]
    where_parts.extend(extra_predicates)
    where = " AND ".join(where_parts)
    for values in wanted_values:
        rows = conn.execute(f"SELECT {select_sql} FROM {table_quoted}{indexed_by} WHERE {where}", values).fetchall()
        for row in rows:
            primary = {field: row[field] for field in select_fields if field not in unique_key}
            if not primary:
                primary = {field: row[field] for field in select_fields}
            out[values].add(_proposal_key(table, {"primary_key": primary}))
    return out


def _with_uniqueness_blockers(conn: sqlite3.Connection, proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for proposal in proposals:
        by_table[str(proposal.get("table") or "")].append(proposal)
    for table, rows in by_table.items():
        contract = phase0_schema_probe.TABLE_ID_CONSTRAINTS.get(table, {})
        unique_keys = [tuple(key) for key in contract.get("unique_keys") or ()]
        candidate_values_by_key: dict[tuple[str, ...], list[tuple[Any, ...]]] = defaultdict(list)
        for proposal in rows:
            set_fields = proposal.get("set_fields") if isinstance(proposal.get("set_fields"), Mapping) else {}
            for unique_key in unique_keys:
                values = []
                complete = True
                for column in unique_key:
                    value = set_fields.get(column)
                    if value is None or _is_empty(value):
                        complete = False
                        break
                    values.append(value)
                if complete:
                    candidate_values_by_key[unique_key].append(tuple(values))
        existing_values_by_key = {
            unique_key: _existing_unique_values(
                conn,
                table=table,
                unique_key=unique_key,
                candidate_values=candidate_values_by_key.get(unique_key, ()),
            )
            for unique_key in unique_keys
        }
        seen: dict[tuple[str, tuple[Any, ...]], list[tuple[Any, ...]]] = defaultdict(list)
        for proposal in rows:
            set_fields = proposal.get("set_fields") if isinstance(proposal.get("set_fields"), Mapping) else {}
            for unique_key in unique_keys:
                values = []
                complete = True
                for column in unique_key:
                    value = set_fields.get(column)
                    if value is None:
                        complete = False
                        break
                    if _is_empty(value):
                        complete = False
                        break
                    values.append(value)
                if not complete:
                    continue
                key = (",".join(unique_key), tuple(values))
                proposal_identity = _proposal_key(table, proposal)
                seen[key].append(proposal_identity)
                existing_identities = existing_values_by_key.get(unique_key, {}).get(tuple(values), set())
                if existing_identities and existing_identities != {proposal_identity}:
                    proposal.setdefault("blockers", []).append(f"unique_key_existing_conflict:{','.join(unique_key)}")
        for (unique_key_name, values), proposal_keys in seen.items():
            if len(proposal_keys) <= 1:
                continue
            duplicates = set(proposal_keys)
            for proposal in rows:
                if _proposal_key(table, proposal) in duplicates:
                    proposal.setdefault("blockers", []).append(f"unique_key_duplicate_proposal:{unique_key_name}")
                    proposal.setdefault("duplicate_unique_value", {})[unique_key_name] = list(values)
    return proposals


def _candidate_builders(
    conn: sqlite3.Connection,
    limit: int,
    *,
    table_names: Sequence[str] | None = None,
    required_fields_by_table: Mapping[str, Sequence[str]] | None = None,
    candidate_limits_by_table: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    builders = {
        "event_fact_history": _build_event_fact_history_proposals,
        "event_fact_latest": _build_event_fact_latest_proposals,
        "auto_trade_decisions": _build_decision_proposals,
        "signal_to_order_timing": _build_signal_timing_proposals,
        "auto_trade_jobs": _build_job_proposals,
        "auto_orders": _build_order_proposals,
        "source_observations": _build_source_observation_proposals,
    }
    requested = [str(item) for item in table_names or [] if str(item or "").strip()]
    selected = (
        [(table, builders[table]) for table in requested if table in builders]
        if requested
        else list(builders.items())
    )
    required_map = required_fields_by_table or {}
    limit_map = candidate_limits_by_table or {}
    for table, builder in selected:
        table_limit = max(1, min(max(1, int(limit)), int(limit_map.get(table) or limit)))
        if table == "auto_trade_decisions":
            proposals.extend(
                builder(
                    conn,
                    table_limit,
                    required_fields=required_map.get(table) or (),
                )
            )
        else:
            proposals.extend(builder(conn, table_limit))
    return _with_uniqueness_blockers(conn, proposals)


def _required_candidate_counts_from_coverage(coverage: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    tables = coverage.get("tables")
    if not isinstance(tables, Mapping):
        return out
    for table, item in tables.items():
        if not isinstance(item, Mapping):
            continue
        counts = item.get("empty_required_field_counts")
        if not isinstance(counts, Mapping):
            continue
        required = 0
        for field in item.get("required_nonempty_fields") or []:
            try:
                required = max(required, int(counts.get(field) or 0))
            except Exception:
                continue
        if required > 0:
            out[str(table)] = required
    return out


def _required_fields_from_coverage(coverage: Mapping[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    tables = coverage.get("tables")
    if not isinstance(tables, Mapping):
        return out
    for table, item in tables.items():
        if not isinstance(item, Mapping):
            continue
        counts = item.get("empty_required_field_counts")
        if not isinstance(counts, Mapping):
            continue
        fields: list[str] = []
        for field in item.get("required_nonempty_fields") or []:
            field_text = str(field or "").strip()
            if not field_text:
                continue
            try:
                missing = int(counts.get(field_text) or 0)
            except Exception:
                missing = 0
            if missing > 0:
                fields.append(field_text)
        if fields:
            out[str(table)] = fields
    return out


def build_plan(
    db_path: Path,
    *,
    immutable: bool = False,
    limit: int = 1000000,
    max_items_output: int = 20,
    duplicate_decision_manifest: Mapping[str, Any] | None = None,
    include_coverage: bool = True,
    coverage_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    if not db.exists():
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "read_only": True,
            "db": str(db),
            "blockers": ["archive_db_missing"],
            "guards": _guards(),
        }
    with _connect(db, immutable=immutable) as conn:
        if coverage_report:
            coverage = dict(coverage_report)
        elif include_coverage:
            coverage = production_identity_coverage_audit.audit(
                db,
                immutable=immutable,
                sample_limit=max(1, int(max_items_output)),
            )
        else:
            coverage = {
                "schema": production_identity_coverage_audit.SCHEMA_VERSION,
                "ok": True,
                "skipped": True,
                "skip_reason": "coverage_audit_skipped_by_identity_merge_fill_temp_drill",
                "gap_tables": [],
                "tables": {},
            }
        scoped_tables = sorted(_required_candidate_counts_from_coverage(coverage))
        required_fields_by_table = _required_fields_from_coverage(coverage)
        required_candidate_counts_for_limits = _required_candidate_counts_from_coverage(coverage)
        candidate_limits_by_table = {
            table: max(int(count or 0) + max(8, int(max_items_output)), int(count or 0), 1)
            for table, count in required_candidate_counts_for_limits.items()
        }
        proposals = _candidate_builders(
            conn,
            max(1, int(limit)),
            table_names=scoped_tables or None,
            required_fields_by_table=required_fields_by_table,
            candidate_limits_by_table=candidate_limits_by_table,
        )
    proposal_tables = {str(item.get("table") or "") for item in proposals}
    duplicate_precondition_required = bool(proposal_tables.intersection({"event_fact_history", "event_fact_latest"}))
    if duplicate_precondition_required:
        duplicate_packet = production_identity_duplicate_fact_snapshot_packet.build_packet(
            db,
            immutable=immutable,
            max_items_output=max_items_output,
            decision_manifest=duplicate_decision_manifest,
        )
    else:
        duplicate_packet = {
            "schema": production_identity_duplicate_fact_snapshot_packet.SCHEMA_VERSION,
            "ok": True,
            "not_applicable": True,
            "not_applicable_reason": "identity_merge_fill_candidates_do_not_touch_fact_snapshot_tables",
            "duplicate_group_count": 0,
            "duplicate_row_count": 0,
            "hard_blockers": [],
            "manifest_resolved_blockers": [],
            "decision_manifest_audit": {
                "schema": "production_identity_duplicate_fact_snapshot_decision_manifest_audit_v1",
                "manifest_provided": False,
                "blocking_for_current_apply": False,
                "expected_decision_count": 0,
                "ready_decision_count": 0,
                "hard_blockers": [],
            },
        }

    table_counts: dict[str, dict[str, int]] = {}
    blockers: list[str] = []
    blocked_count = 0
    safe_count = 0
    required_candidate_counts = _required_candidate_counts_from_coverage(coverage)
    for proposal in proposals:
        table = str(proposal.get("table") or "")
        table_counts.setdefault(
            table,
            {
                "required_candidate_count": int(required_candidate_counts.get(table) or 0),
                "candidate_count": 0,
                "safe_count": 0,
                "blocked_count": 0,
                "candidate_scope_truncated": 0,
            },
        )
        table_counts[table]["candidate_count"] += 1
        proposal_blockers = [str(item) for item in proposal.get("blockers") or [] if str(item or "").strip()]
        if proposal_blockers:
            blocked_count += 1
            table_counts[table]["blocked_count"] += 1
            blockers.extend(proposal_blockers)
        else:
            safe_count += 1
            table_counts[table]["safe_count"] += 1
    for table, required_count in required_candidate_counts.items():
        table_counts.setdefault(
            table,
            {
                "required_candidate_count": int(required_count or 0),
                "candidate_count": 0,
                "safe_count": 0,
                "blocked_count": 0,
                "candidate_scope_truncated": 0,
            },
        )
        if int(table_counts[table].get("candidate_count") or 0) < int(required_count or 0):
            table_counts[table]["candidate_scope_truncated"] = 1
    candidate_scope_truncated = any(int(item.get("candidate_scope_truncated") or 0) for item in table_counts.values())
    if candidate_scope_truncated:
        blockers.append("candidate_scope_truncated")
    duplicate_hard_blockers = [
        str(item)
        for item in duplicate_packet.get("hard_blockers") or []
        if str(item or "").strip()
    ]
    blockers.extend(f"duplicate_fact_snapshot:{blocker}" for blocker in duplicate_hard_blockers)
    needs_apply = bool(proposals)
    duplicate_ready = not duplicate_hard_blockers
    safe_to_apply_all = bool(
        needs_apply
        and blocked_count == 0
        and not candidate_scope_truncated
        and duplicate_ready
    )
    duplicate_operator_commands = {
        "duplicate_operator_runbook_command": "",
        "duplicate_decision_manifest_template_command": "",
        "duplicate_decision_manifest_audit_command": "",
        "duplicate_apply_contract_command": "",
        "duplicate_apply_command": "",
    }
    duplicate_post_apply_verification_commands: list[str] = []
    if duplicate_precondition_required:
        duplicate_operator_commands = {
            "duplicate_operator_runbook_command": (
                "python3 scripts/production_identity_duplicate_fact_snapshot_operator_runbook.py "
                f"--db {_q(db)} --summary --max-items-output 8"
            ),
            "duplicate_decision_manifest_template_command": (
                "python3 scripts/production_identity_duplicate_fact_snapshot_packet.py "
                f"--db {_q(db)} --emit-decision-manifest-template > <reviewed_duplicate_manifest.json>"
            ),
            "duplicate_decision_manifest_audit_command": (
                "python3 scripts/production_identity_duplicate_fact_snapshot_packet.py "
                f"--db {_q(db)} --summary --max-items-output 8 "
                "--decision-manifest <reviewed_duplicate_manifest.json>"
            ),
            "duplicate_apply_contract_command": (
                "python3 scripts/production_identity_duplicate_fact_snapshot_apply_contract.py "
                f"--db {_q(db)} --summary --max-items-output 8 "
                "--decision-manifest <reviewed_duplicate_manifest.json>"
            ),
            "duplicate_apply_command": (
                "python3 scripts/production_identity_duplicate_fact_snapshot_apply.py "
                f"--db {_q(db)} --decision-manifest <reviewed_duplicate_manifest.json> "
                "--apply --confirm APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR "
                "--backup <fresh_archive_backup.bak> --writer-quiescent"
            ),
        }
        duplicate_post_apply_verification_commands = [
            "python3 scripts/production_identity_duplicate_fact_snapshot_packet.py --summary --max-items-output 8 --decision-manifest <reviewed_duplicate_manifest.json>",
        ]
    backup = ROOT / "data" / "backups" / "archive.db.production-identity-merge-fill.bak"
    manifest = ROOT / "data" / "backups" / "archive.db.production-identity-merge-fill.manifest.json"
    return {
        "schema": SCHEMA_VERSION,
        "ok": safe_to_apply_all or not needs_apply,
        "read_only": True,
        "dry_run": True,
        "execution_mode": "production_identity_merge_fill_plan_no_apply",
        "db": str(db),
        "coverage_schema": coverage.get("schema"),
        "coverage_ok_before_apply": bool(coverage.get("ok")),
        "coverage_gap_tables_before_apply": list(coverage.get("gap_tables") or []),
        "needs_apply": needs_apply,
        "safe_to_apply_all_candidates": safe_to_apply_all,
        "candidate_scope_truncated": candidate_scope_truncated,
        "candidate_count": len(proposals),
        "safe_candidate_count": safe_count,
        "blocked_candidate_count": blocked_count,
        "duplicate_fact_snapshot_precondition": {
            "schema": duplicate_packet.get("schema"),
            "ok": bool(duplicate_packet.get("ok")),
            "not_applicable": bool(duplicate_packet.get("not_applicable")),
            "not_applicable_reason": str(duplicate_packet.get("not_applicable_reason") or ""),
            "duplicate_group_count": int(duplicate_packet.get("duplicate_group_count") or 0),
            "duplicate_row_count": int(duplicate_packet.get("duplicate_row_count") or 0),
            "hard_blockers": duplicate_hard_blockers,
            "manifest_resolved_blockers": list(duplicate_packet.get("manifest_resolved_blockers") or []),
            "decision_manifest_audit": {
                "schema": (duplicate_packet.get("decision_manifest_audit") or {}).get("schema")
                if isinstance(duplicate_packet.get("decision_manifest_audit"), Mapping)
                else "",
                "manifest_provided": bool(
                    (duplicate_packet.get("decision_manifest_audit") or {}).get("manifest_provided")
                )
                if isinstance(duplicate_packet.get("decision_manifest_audit"), Mapping)
                else False,
                "blocking_for_current_apply": bool(
                    (duplicate_packet.get("decision_manifest_audit") or {}).get("blocking_for_current_apply")
                )
                if isinstance(duplicate_packet.get("decision_manifest_audit"), Mapping)
                else False,
                "expected_decision_count": int(
                    (duplicate_packet.get("decision_manifest_audit") or {}).get("expected_decision_count") or 0
                )
                if isinstance(duplicate_packet.get("decision_manifest_audit"), Mapping)
                else 0,
                "ready_decision_count": int(
                    (duplicate_packet.get("decision_manifest_audit") or {}).get("ready_decision_count") or 0
                )
                if isinstance(duplicate_packet.get("decision_manifest_audit"), Mapping)
                else 0,
                "hard_blockers": list(
                    (duplicate_packet.get("decision_manifest_audit") or {}).get("hard_blockers") or []
                )
                if isinstance(duplicate_packet.get("decision_manifest_audit"), Mapping)
                else [],
            },
        },
        "table_counts": table_counts,
        "blockers": sorted(set(blockers)),
        "candidate_samples": proposals[: max(0, int(max_items_output))],
        "candidate_operations": proposals,
        "candidate_operations_truncated": False,
        "operator_contract": {
            "schema": "production_identity_merge_fill_operator_contract_v1",
            "confirmation_phrase": CONFIRMATION_PHRASE,
            "requires_backup": True,
            "requires_writer_quiescence": True,
            "requires_manifest_review": True,
            "requires_duplicate_fact_snapshot_manifest_audit": duplicate_precondition_required,
            "allow_apply_now": False,
            "backup_path": str(backup),
            "manifest_path": str(manifest),
            **duplicate_operator_commands,
            "backup_command": f"mkdir -p {_q(backup.parent)} && sqlite3 {_q(db)} \".backup {_q(backup)}\"",
            "verify_writer_quiescence_command": (
                f"python3 scripts/controlled_apply_maintenance_window_packet.py --db {_q(db)} "
                "--summary --max-items-output 8"
            ),
            "post_apply_verification_commands": [
                *duplicate_post_apply_verification_commands,
                "python3 scripts/production_identity_coverage_audit.py --summary --max-items-output 8",
                "python3 scripts/hierarchical_joint_plan_coverage_audit.py --summary --limit 1000000 --max-items-output 8",
            ],
        },
        "guards": _guards(),
        "next_boundary": (
            "review_duplicate_or_conflicting_identity_candidates_before_apply"
            if blocked_count or not duplicate_ready
            else "increase_limit_or_run_chunked_identity_plan_before_apply"
            if candidate_scope_truncated
            else "operator_can_materialize_manifest_and_apply_after_confirmation"
            if needs_apply
            else "production_identity_merge_fill_not_required"
        ),
    }


def compact_plan(plan: Mapping[str, Any], *, max_items_output: int = 8) -> dict[str, Any]:
    samples = list(plan.get("candidate_samples") or [])[: max(0, int(max_items_output))]
    return {
        "schema": str(plan.get("schema") or SCHEMA_VERSION) + "_summary",
        "source_schema": str(plan.get("schema") or ""),
        "ok": bool(plan.get("ok")),
        "read_only": bool(plan.get("read_only")),
        "dry_run": bool(plan.get("dry_run")),
        "db": str(plan.get("db") or ""),
        "coverage_ok_before_apply": bool(plan.get("coverage_ok_before_apply")),
        "coverage_gap_tables_before_apply": list(plan.get("coverage_gap_tables_before_apply") or []),
        "needs_apply": bool(plan.get("needs_apply")),
        "safe_to_apply_all_candidates": bool(plan.get("safe_to_apply_all_candidates")),
        "candidate_scope_truncated": bool(plan.get("candidate_scope_truncated")),
        "candidate_count": int(plan.get("candidate_count") or 0),
        "safe_candidate_count": int(plan.get("safe_candidate_count") or 0),
        "blocked_candidate_count": int(plan.get("blocked_candidate_count") or 0),
        "duplicate_fact_snapshot_precondition": dict(plan.get("duplicate_fact_snapshot_precondition") or {}),
        "table_counts": dict(plan.get("table_counts") or {}),
        "blockers": list(plan.get("blockers") or [])[:50],
        "candidate_samples": samples,
        "candidate_operations_count": len(plan.get("candidate_operations") or []),
        "candidate_operations_truncated": bool(plan.get("candidate_operations_truncated")),
        "operator_contract": dict(plan.get("operator_contract") or {}),
        "guards": dict(plan.get("guards") or {}),
        "next_boundary": str(plan.get("next_boundary") or ""),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only production identity merge-fill plan.")
    parser.add_argument("--db", default=str(os.environ.get("PM_ARCHIVE_DB_PATH") or DEFAULT_DB))
    parser.add_argument("--immutable", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--limit", type=int, default=1000000)
    parser.add_argument("--max-items-output", type=int, default=8)
    parser.add_argument("--duplicate-decision-manifest", default="")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    plan = build_plan(
        Path(args.db),
        immutable=bool(args.immutable),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        duplicate_decision_manifest=_load_json_file(args.duplicate_decision_manifest),
    )
    output: Mapping[str, Any] = compact_plan(plan, max_items_output=int(args.max_items_output)) if args.summary else plan
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if plan.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
