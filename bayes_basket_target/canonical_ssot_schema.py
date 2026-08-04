"""Strict schema inspection for the canonical append-only fact store."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Sequence


TABLE = "canonical_ssot_fact"
FACT_TYPES_V2 = ("model", "truth", "residual", "activation")
FACT_TYPES_V3 = (*FACT_TYPES_V2, "activation_supersession")
FACT_TYPES_V4 = (*FACT_TYPES_V3, "ensemble_member", "native_arrival")
FACT_TYPES_V5 = (*FACT_TYPES_V4, "ensemble_member_arrival_binding")
# V6 retains the V5 binding type for newly validated lineage, but adds an
# explicit receipt-manifest fact.  A V5 binding cannot stand in for that
# manifest: historical V5 binding rows must be rejected by the V6 migration.
FACT_TYPES_V6 = (*FACT_TYPES_V5, "ensemble_member_receipt_manifest")
FACT_TYPES_V7 = (*FACT_TYPES_V6, "republisher_oos_artifact")
EXPECTED_COLUMNS = (
    "fact_id",
    "fact_type",
    "scope_key",
    "content_sha256",
    "fact_json",
    "lineage_json",
    "ingested_at_utc",
)
V3_TRIGGERS = (
    "canonical_ssot_fact_no_update",
    "canonical_ssot_fact_no_delete",
)
V4_TRIGGERS = (*V3_TRIGGERS, "canonical_ssot_fact_ingress_only")
V5_TRIGGERS = V4_TRIGGERS
V6_TRIGGERS = V5_TRIGGERS
V7_TRIGGERS = V6_TRIGGERS
# Backward-compatible name used by the historical V2->V3 migration.  V4
# callers must request ``V4_TRIGGERS`` explicitly; the inspector selects the
# correct set from the exact fact-type schema.
ACTIVE_TRIGGERS = V3_TRIGGERS

_FACT_TYPE_CHECK_RE = re.compile(
    r"CHECK\s*\(\s*fact_type\s+IN\s*\((?P<values>[^)]*)\)\s*\)",
    re.IGNORECASE,
)
_QUOTED_VALUE_RE = re.compile(r"'([^']+)'\s*(?:,|$)")


def _fact_type_values(sql: str) -> tuple[str, ...] | None:
    matches = list(_FACT_TYPE_CHECK_RE.finditer(sql))
    if len(matches) != 1:
        return None
    raw = matches[0].group("values")
    values = tuple(match.group(1) for match in _QUOTED_VALUE_RE.finditer(raw))
    normalized_raw = re.sub(r"\s+", "", raw)
    normalized_values = ",".join(f"'{value}'" for value in values)
    if not values or normalized_raw != normalized_values:
        return None
    return values


def _unique_keys(conn: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = set()
    for row in conn.execute(f'PRAGMA index_list("{table}")'):
        if int(row[2]) != 1:
            continue
        name = str(row[1])
        columns = tuple(
            str(item[2])
            for item in conn.execute(f'PRAGMA index_info("{name}")')
        )
        if columns:
            keys.add(columns)
    return keys


def _trigger_errors(
    conn: sqlite3.Connection,
    *,
    table: str,
    trigger_names: Sequence[str],
) -> list[str]:
    rows = {
        str(name): str(sql or "")
        for name, sql in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        )
    }
    errors: list[str] = []
    if set(rows) != set(trigger_names):
        errors.append("trigger_set_mismatch")
        return errors
    for name, sql in rows.items():
        compact = re.sub(r"\s+", "", sql).lower()
        if name.endswith("_ingress_only"):
            if (
                f"beforeinserton{table.lower()}" not in compact
                or "canonical_ingress_authorized()!=1" not in compact
                or "raise(abort,'canonical_ingress_only')" not in compact
            ):
                errors.append(f"trigger_semantics_mismatch:{name}")
        else:
            operation = "update" if name.endswith("_no_update") else "delete"
            expected = f"before{operation}on{table.lower()}"
            if expected not in compact or "raise(abort,'append_only')" not in compact:
                errors.append(f"trigger_semantics_mismatch:{name}")
    return errors


def inspect_canonical_ssot_schema(
    conn: sqlite3.Connection,
    *,
    table: str = TABLE,
    trigger_names: Sequence[str] | None = None,
    require_triggers: bool = True,
) -> dict[str, Any]:
    """Return an exact V2/V3/V4/V5/V6/V7/INVALID/ABSENT classification.

    The inspector rejects extra fact types, lookalike string literals, missing
    UNIQUE constraints, column drift, and non-append-only trigger variants.
    """

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return {"state": "ABSENT", "ok": False, "errors": ["table_missing"]}
    sql = str(row[0] or "")
    errors: list[str] = []
    column_rows = list(conn.execute(f'PRAGMA table_info("{table}")'))
    columns = tuple(str(item[1]) for item in column_rows)
    if columns != EXPECTED_COLUMNS:
        errors.append("columns_mismatch")
    primary_key_columns = tuple(
        str(item[1]) for item in sorted(column_rows, key=lambda item: int(item[5]))
        if int(item[5]) > 0
    )
    if primary_key_columns != ("fact_id",):
        errors.append("primary_key_mismatch")
    values = _fact_type_values(sql)
    if values == FACT_TYPES_V2:
        state = "V2"
    elif values == FACT_TYPES_V3:
        state = "V3"
    elif values == FACT_TYPES_V4:
        state = "V4"
    elif values == FACT_TYPES_V5:
        state = "V5"
    elif values == FACT_TYPES_V6:
        state = "V6"
    elif values == FACT_TYPES_V7:
        state = "V7"
    else:
        state = "INVALID"
        errors.append("fact_type_check_mismatch")
    compact_sql = re.sub(r"\s+", "", sql).lower()
    if "check(length(content_sha256)=64)" not in compact_sql:
        errors.append("content_sha256_check_missing")
    unique_keys = _unique_keys(conn, table)
    for expected in (
        ("fact_id",),
        ("fact_type", "content_sha256"),
        ("fact_type", "scope_key"),
    ):
        if expected not in unique_keys:
            errors.append("unique_key_missing:" + ",".join(expected))
    if require_triggers:
        expected_triggers = (
            tuple(trigger_names)
            if trigger_names is not None
            else V7_TRIGGERS
            if state == "V7"
            else V6_TRIGGERS
            if state == "V6"
            else V5_TRIGGERS
            if state == "V5"
            else V4_TRIGGERS
            if state == "V4"
            else V3_TRIGGERS
        )
        errors.extend(
            _trigger_errors(
                conn,
                table=table,
                trigger_names=expected_triggers,
            )
        )
    else:
        expected_triggers = tuple(trigger_names or ())
    if errors:
        state = "INVALID"
    return {
        "state": state,
        # Older versions remain valid migration sources.  Runtime consumers
        # must require the exact version they need; the inspector never treats
        # an unknown or reordered fact-type list as compatible.
        "ok": state in {"V3", "V4", "V5", "V6", "V7"} and not errors,
        "errors": sorted(set(errors)),
        "columns": list(columns),
        "fact_types": list(values or ()),
        "unique_keys": [list(item) for item in sorted(unique_keys)],
        "trigger_names": list(expected_triggers),
    }


__all__ = [
    "ACTIVE_TRIGGERS",
    "EXPECTED_COLUMNS",
    "FACT_TYPES_V2",
    "FACT_TYPES_V3",
    "FACT_TYPES_V4",
    "FACT_TYPES_V5",
    "FACT_TYPES_V6",
    "FACT_TYPES_V7",
    "TABLE",
    "V3_TRIGGERS",
    "V4_TRIGGERS",
    "V5_TRIGGERS",
    "V6_TRIGGERS",
    "V7_TRIGGERS",
    "inspect_canonical_ssot_schema",
]
