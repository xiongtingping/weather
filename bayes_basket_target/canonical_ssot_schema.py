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
FACT_TYPES_V8 = (*FACT_TYPES_V7, "republisher_cross_selection")
FACT_TYPES_V9 = (*FACT_TYPES_V8, "republisher_no_live_activation")
FACT_TYPES_V10 = (
    *FACT_TYPES_V9,
    "forward_applicability",
    "probability_distribution_artifact",
    "p0_snapshot",
)
FACT_TYPES_V11 = (*FACT_TYPES_V10, "market_bucket_bridge")
FACT_TYPES_V12 = (
    *FACT_TYPES_V11,
    "truth_observation_revision",
    "truth_observation_day_bounds",
)
FACT_TYPES_V13 = (*FACT_TYPES_V12, "model_oos_event", "model_oos_cohort")
FACT_TYPES_V14 = (
    *FACT_TYPES_V13,
    "p0_model_family_selection",
    "p0_calibrated_distribution",
    "p0_activation_root",
    "p0_forecast_snapshot",
)
FACT_TYPES_V15 = (*FACT_TYPES_V14, "truth_authority_supersession")
# V16 reserves separate fact types for native ensemble event and cohort
# evidence.  They are intentionally not aliases for the single-model OOS
# facts: a native ensemble event references a member-set vector rather than a
# singular raw object.
FACT_TYPES_V16 = (
    *FACT_TYPES_V15,
    "native_ensemble_oos_event",
    "native_ensemble_oos_cohort",
)
# V17 closes the probability-to-execution chain without reusing legacy
# envelope or observation tables.  Raw surface observations and their
# priority winner remain distinct facts; neither can be renamed into P1.
# ``substitute_observation_revision`` is the scheduled ASOS substitute
# lanes' parallel identity fact (user ruling 2026-08-28): the role
# ``truth_observation_revision`` plays for AWC reports, without ever
# entering the truth lane.  Added while V17 was still unapplied anywhere,
# so no database can hold the earlier V17 signature.
FACT_TYPES_V17 = (
    *FACT_TYPES_V16,
    "surface_observation",
    "surface_observation_winner",
    "p1_base",
    "pure_yes_basket_plan",
    "substitute_observation_revision",
)
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
V8_TRIGGERS = V7_TRIGGERS
V9_TRIGGERS = V8_TRIGGERS
V10_TRIGGERS = V9_TRIGGERS
V11_TRIGGERS = V10_TRIGGERS
V12_TRIGGERS = V11_TRIGGERS
V13_TRIGGERS = V12_TRIGGERS
V14_TRIGGERS = V13_TRIGGERS
V15_TRIGGERS = V14_TRIGGERS
V16_TRIGGERS = V15_TRIGGERS
V17_TRIGGERS = V16_TRIGGERS
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
            operation = (
                "update"
                if name.endswith("_no_update")
                else "delete"
                if name.endswith("_no_delete")
                else "insert"
                if name.endswith("_no_insert")
                else ""
            )
            if not operation:
                errors.append(f"trigger_semantics_mismatch:{name}")
                continue
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
    """Return an exact V2..V17/INVALID/ABSENT classification.

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
    elif values == FACT_TYPES_V8:
        state = "V8"
    elif values == FACT_TYPES_V9:
        state = "V9"
    elif values == FACT_TYPES_V10:
        state = "V10"
    elif values == FACT_TYPES_V11:
        state = "V11"
    elif values == FACT_TYPES_V12:
        state = "V12"
    elif values == FACT_TYPES_V13:
        state = "V13"
    elif values == FACT_TYPES_V14:
        state = "V14"
    elif values == FACT_TYPES_V15:
        state = "V15"
    elif values == FACT_TYPES_V16:
        state = "V16"
    elif values == FACT_TYPES_V17:
        state = "V17"
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
            else V17_TRIGGERS
            if state == "V17"
            else V16_TRIGGERS
            if state == "V16"
            else V15_TRIGGERS
            if state == "V15"
            else V14_TRIGGERS
            if state == "V14"
            else V13_TRIGGERS
            if state == "V13"
            else V12_TRIGGERS
            if state == "V12"
            else V11_TRIGGERS
            if state == "V11"
            else V10_TRIGGERS
            if state == "V10"
            else V9_TRIGGERS
            if state == "V9"
            else V8_TRIGGERS
            if state == "V8"
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
        "ok": state in {"V3", "V4", "V5", "V6", "V7", "V8", "V9", "V10", "V11", "V12", "V13", "V14", "V15", "V16", "V17"} and not errors,
        "errors": sorted(set(errors)),
        "columns": list(columns),
        "fact_types": list(values or ()),
        "unique_keys": [list(item) for item in sorted(unique_keys)],
        "trigger_names": list(expected_triggers),
    }


# Ordered oldest-to-newest.  Each canonical version is a strict superset of the
# previous one's fact types, so a consumer that needs version N is satisfied by
# any state at or after N.  Use `canonical_state_at_least` rather than an `==`
# comparison or a hand-written set literal: a `== "V14"` check in a readiness
# gate silently blocked the entire production mainline once the database had
# migrated on to V16, and inline set literals have to be edited in a dozen
# places every time a version is added.
CANONICAL_STATE_ORDER = (
    "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9", "V10",
    "V11", "V12", "V13", "V14", "V15", "V16", "V17",
)


def canonical_state_at_least(state: str, minimum: str) -> bool:
    """Return whether an inspected canonical state satisfies a minimum version.

    Unknown states (including "ABSENT" and "INVALID") are never satisfying.
    """

    if minimum not in CANONICAL_STATE_ORDER:
        raise ValueError(f"unknown_canonical_minimum:{minimum}")
    if state not in CANONICAL_STATE_ORDER:
        return False
    return CANONICAL_STATE_ORDER.index(state) >= CANONICAL_STATE_ORDER.index(minimum)


__all__ = [
    "ACTIVE_TRIGGERS",
    "CANONICAL_STATE_ORDER",
    "canonical_state_at_least",
    "EXPECTED_COLUMNS",
    "FACT_TYPES_V2",
    "FACT_TYPES_V3",
    "FACT_TYPES_V4",
    "FACT_TYPES_V5",
    "FACT_TYPES_V6",
    "FACT_TYPES_V7",
    "FACT_TYPES_V8",
    "FACT_TYPES_V9",
    "FACT_TYPES_V10",
    "FACT_TYPES_V11",
    "FACT_TYPES_V12",
    "FACT_TYPES_V13",
    "FACT_TYPES_V14",
    "FACT_TYPES_V15",
    "FACT_TYPES_V16",
    "FACT_TYPES_V17",
    "TABLE",
    "V3_TRIGGERS",
    "V4_TRIGGERS",
    "V5_TRIGGERS",
    "V6_TRIGGERS",
    "V7_TRIGGERS",
    "V8_TRIGGERS",
    "V9_TRIGGERS",
    "V10_TRIGGERS",
    "V11_TRIGGERS",
    "V12_TRIGGERS",
    "V13_TRIGGERS",
    "V14_TRIGGERS",
    "V15_TRIGGERS",
    "V16_TRIGGERS",
    "V17_TRIGGERS",
    "inspect_canonical_ssot_schema",
]
