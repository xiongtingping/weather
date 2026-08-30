"""Read-only settled attribution over the canonical DeltaBasketScore facts.

This module deliberately creates no table and writes no receipt.  It is a
bounded projection of the append-only ``basket_policy_training_fact`` and
``basket_policy_training_label`` authorities.  In ``production_no_live`` a
settlement label describes requested-intent *counterfactual* PnL, never a
fill.  Consequently realised PnL is reported as an explicit NA rather than
the tempting, but false, value ``0``.

The five-term attribution is only admitted when the label itself carries a
content-bound decomposition.  Existing labels which predate that payload are
still useful as settled counterfactual observations, but are deliberately
reported as ``NA_ATTRIBUTION_TERMS_MISSING``; the reader must not invent a
coverage/overlap/edge split from a total PnL.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .fleet import expected_station_count
from .contracts import ContractError, content_sha256
from .delta_v2_settlement_label import (
    LABEL_SCHEMA,
    SETTLEMENT_VERSION_BINDING_SCHEMA,
    TERMS_SCHEMA,
)
from .delta_v2_training import FACT_COLUMNS, FACT_SCHEMA
from .execution_version_vector import (
    ExecutionVersionVectorError,
    validate_execution_version_vector,
    validate_no_execution_version_policy,
)
from .resource_bounds import assert_process_memory_budget
from .settlement_evidence_packet import (
    validate_attribution_terms,
    validate_settlement_version_evidence_packet,
)


SCHEMA = "delta_basket_score_settled_attribution_v1"
FACT_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_FORWARD"
REQUESTED_INTENT_SEMANTICS = "hypothetical_requested_intents_not_fills"
MAX_FACTS = 256
MAX_LABEL_REVISIONS_PER_FACT = 32
MAX_JSON_BYTES = 262_144
MAX_JSON_NODES = 100_000
MAX_JSON_DEPTH = 32
EPS = 1e-8

_FACT_HASH_FIELDS = tuple(
    field for field in FACT_COLUMNS if field not in {"fact_id", "content_sha256"}
)
_LABEL_HASH_FIELDS = (
    "fact_id",
    "truth_id",
    "truth_revision_no",
    "truth_sha256",
    "official_tmax_c",
    "winning_bucket",
    "settlement_closed_at_utc",
    "labeled_at_utc",
    "outcome_json",
    "authority_scope",
    "supersedes_label_id",
)
_POLICY_REQUIRED_TEXT = (
    "strategy_version",
    "season_bin",
    "maturity_bin",
    "liquidity_bin",
    "cash_phase_policy_version",
    "delta_v2_artifact_id",
)
_POLICY_REQUIRED_SHA = (
    "cash_phase_policy_sha256",
    "delta_v2_artifact_sha256",
)


def _utc(value: Any, *, name: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"settled_attribution_{name}_missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"settled_attribution_{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"settled_attribution_{name}_timezone_required")
    normalized = parsed.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z"), normalized


def _sha(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        return None
    return result


def _number(value: Any, *, nonnegative: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or (nonnegative and result < 0.0):
        return None
    return result


def _bounded_mapping(value: Any, *, byte_length: Any) -> tuple[dict[str, Any] | None, str | None]:
    try:
        size = int(byte_length)
    except (TypeError, ValueError):
        return None, "json_size_invalid"
    if size < 0 or size > MAX_JSON_BYTES:
        return None, "json_size_out_of_bounds"
    if not isinstance(value, str) or len(value.encode("utf-8")) != size:
        return None, "json_size_mismatch"
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "json_malformed"
    nodes = 0

    def _walk(item: Any, depth: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return False
        if isinstance(item, dict):
            return all(isinstance(key, str) and _walk(child, depth + 1) for key, child in item.items())
        if isinstance(item, list):
            return all(_walk(child, depth + 1) for child in item)
        return True

    if not _walk(decoded, 0):
        return None, "json_structure_out_of_bounds"
    if not isinstance(decoded, Mapping):
        return None, "json_root_not_mapping"
    return dict(decoded), None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _row(cursor: sqlite3.Cursor, raw: Sequence[Any]) -> dict[str, Any]:
    return {
        str(description[0]): raw[index]
        for index, description in enumerate(cursor.description or ())
    }


def _fact_hash_error(fact: Mapping[str, Any]) -> str | None:
    stored = _sha(fact.get("content_sha256"))
    if stored is None:
        return "fact_content_sha256_invalid"
    expected = content_sha256({
        "schema": FACT_SCHEMA,
        **{field: fact.get(field) for field in _FACT_HASH_FIELDS},
    })
    if stored != expected:
        return "fact_content_sha256_drift"
    if str(fact.get("fact_id") or "") != f"d2fact_{expected[:40]}":
        return "fact_content_address_drift"
    return None


def _label_hash_error(label: Mapping[str, Any]) -> str | None:
    stored = _sha(label.get("content_sha256"))
    if stored is None:
        return "label_content_sha256_invalid"
    expected = content_sha256({
        "schema": LABEL_SCHEMA,
        **{field: label.get(field) for field in _LABEL_HASH_FIELDS},
    })
    if stored != expected:
        return "label_content_sha256_drift"
    if str(label.get("label_id") or "") != f"d2label_{expected[:40]}":
        return "label_content_address_drift"
    return None


def _policy_vector_error(raw: Mapping[str, Any]) -> str | None:
    if raw.get("schema") != "delta_basket_score_policy_version_vector_v2":
        return "policy_version_vector_schema_invalid"
    for name in _POLICY_REQUIRED_TEXT:
        if not isinstance(raw.get(name), str) or not raw[name].strip():
            return f"policy_version_vector_{name}_missing"
    for name in _POLICY_REQUIRED_SHA:
        if _sha(raw.get(name)) is None:
            return f"policy_version_vector_{name}_invalid"
    station_policy = raw.get("station_policy")
    if not isinstance(station_policy, Mapping) or _sha(station_policy.get("policy_sha256")) is None:
        return "policy_version_vector_station_policy_incomplete"
    return None


def _feature_vector_error(feature: Mapping[str, Any], *, intent_count: Any) -> tuple[str | None, list[str]]:
    boundary = feature.get("no_live_boundary")
    if not isinstance(boundary, Mapping):
        return "no_live_boundary_missing", []
    if (
        boundary.get("execution_mode") != "production_no_live"
        or boundary.get("live_submit_authorized") is not False
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("orders_submitted") != 0
    ):
        return "no_live_boundary_invalid", []
    if isinstance(intent_count, bool):
        return "intent_count_invalid", []
    try:
        normalized_count = int(intent_count)
    except (TypeError, ValueError):
        return "intent_count_invalid", []
    if normalized_count < 0:
        return "intent_count_invalid", []
    no_execution = feature.get("no_execution_policy")
    vectors = feature.get("execution_version_vectors")
    if no_execution is not None:
        if normalized_count != 0 or vectors not in (None, []):
            return "no_execution_version_vector_conflict", []
        try:
            policy = validate_no_execution_version_policy(no_execution)
        except (ExecutionVersionVectorError, TypeError, ValueError):
            return "no_execution_version_vector_invalid", []
        return None, [str(policy.policy_sha256)]
    if isinstance(vectors, (str, bytes, bytearray)) or not isinstance(vectors, Sequence):
        return "execution_version_vectors_missing", []
    if len(vectors) != normalized_count or len(vectors) > MAX_FACTS:
        return "execution_version_vector_count_invalid", []
    digests: list[str] = []
    for item in vectors:
        if not isinstance(item, Mapping):
            return "execution_version_vector_invalid", []
        try:
            vector = validate_execution_version_vector(item)
        except (ExecutionVersionVectorError, TypeError, ValueError):
            return "execution_version_vector_invalid", []
        if vector.vector_sha256 in digests:
            return "execution_version_vector_duplicate", []
        digests.append(str(vector.vector_sha256))
    if normalized_count == 0:
        return "zero_intent_no_execution_policy_missing", []
    return None, sorted(digests)


def _select_label(
    conn: sqlite3.Connection,
    *,
    fact_id: str,
    as_of_utc: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    cursor = conn.execute(
        "SELECT *,length(CAST(outcome_json AS BLOB)) AS outcome_bytes "
        "FROM basket_policy_training_label WHERE fact_id=? "
        "AND labeled_at_utc<=? AND settlement_closed_at_utc<=? "
        "ORDER BY truth_revision_no DESC,labeled_at_utc DESC,label_id DESC LIMIT ?",
        (fact_id, as_of_utc, as_of_utc, MAX_LABEL_REVISIONS_PER_FACT + 1),
    )
    rows = cursor.fetchall()
    return _validate_label_rows(cursor, rows)


def _validate_label_rows(
    cursor: sqlite3.Cursor,
    rows: Sequence[Sequence[Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    if not rows:
        return None, ["settlement_label_missing_or_not_yet_closed"]
    if len(rows) > MAX_LABEL_REVISIONS_PER_FACT:
        return None, ["label_revision_count_out_of_bounds"]
    labels = [_row(cursor, item) for item in rows]
    errors = [error for label in labels if (error := _label_hash_error(label))]
    if errors:
        return None, sorted(set(errors))
    highest = labels[0]
    for newer, older in zip(labels, labels[1:]):
        if int(newer["truth_revision_no"]) <= int(
            older["truth_revision_no"]
        ):
            return None, ["label_revision_order_invalid"]
        if str(newer.get("supersedes_label_id") or "") != str(
            older.get("label_id") or ""
        ):
            return None, ["label_revision_chain_incomplete"]
    if labels[-1].get("supersedes_label_id") not in (None, ""):
        return None, ["label_revision_chain_root_invalid"]
    return highest, []


def _select_label_batch(
    conn: sqlite3.Connection,
    *,
    fact_ids: Sequence[str],
    as_of_utc: str,
) -> dict[str, tuple[dict[str, Any] | None, list[str]]]:
    if not fact_ids:
        return {}
    placeholders = ",".join("?" for _ in fact_ids)
    cursor = conn.execute(
        "SELECT *,length(CAST(outcome_json AS BLOB)) AS outcome_bytes "
        "FROM basket_policy_training_label "
        f"WHERE fact_id IN ({placeholders}) "
        "AND labeled_at_utc<=? AND settlement_closed_at_utc<=? "
        "ORDER BY fact_id,truth_revision_no DESC,"
        "labeled_at_utc DESC,label_id DESC LIMIT ?",
        (
            *fact_ids,
            as_of_utc,
            as_of_utc,
            len(fact_ids) * (MAX_LABEL_REVISIONS_PER_FACT + 1) + 1,
        ),
    )
    grouped: dict[str, list[Sequence[Any]]] = defaultdict(list)
    columns = tuple(item[0] for item in (cursor.description or ()))
    fact_index = columns.index("fact_id")
    raw_rows = cursor.fetchall()
    if len(raw_rows) > len(fact_ids) * (
        MAX_LABEL_REVISIONS_PER_FACT + 1
    ):
        return {
            fact_id: (None, ["label_batch_revision_rows_out_of_bounds"])
            for fact_id in fact_ids
        }
    for raw in raw_rows:
        grouped[str(raw[fact_index])].append(raw)
    result: dict[str, tuple[dict[str, Any] | None, list[str]]] = {}
    for fact_id in fact_ids:
        rows = grouped.get(fact_id, [])
        result[fact_id] = _validate_label_rows(cursor, rows)
    return result


def _counterfactual_pnl(outcome: Mapping[str, Any]) -> tuple[float | None, str | None]:
    if outcome.get("label_semantics") != REQUESTED_INTENT_SEMANTICS:
        return None, "label_semantics_not_no_live_counterfactual"
    if outcome.get("orders_submitted") != 0 or outcome.get("calls_order_gateway") is not False:
        return None, "label_no_live_boundary_invalid"
    cost = _number(outcome.get("requested_cost_usdc"), nonnegative=True)
    shares = _number(outcome.get("winning_requested_shares"), nonnegative=True)
    reported = _number(outcome.get("hypothetical_requested_pnl_usdc"))
    if cost is None or shares is None or reported is None:
        return None, "counterfactual_pnl_fields_missing"
    expected = shares - cost
    if abs(reported - expected) > EPS:
        return None, "counterfactual_pnl_arithmetic_drift"
    return reported, None


def _settlement_version_binding(
    outcome: Mapping[str, Any],
    *,
    fact: Mapping[str, Any],
    label: Mapping[str, Any],
    policy_vector_sha256: str,
) -> tuple[str, str | None]:
    raw = outcome.get("settlement_version_binding")
    if raw is None:
        return "NA_SETTLEMENT_VERSION_BINDING_MISSING", None
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema") != SETTLEMENT_VERSION_BINDING_SCHEMA
    ):
        return "settlement_version_binding_schema_invalid", None
    stored_sha = _sha(raw.get("content_sha256"))
    core = {
        key: value
        for key, value in raw.items()
        if key != "content_sha256"
    }
    if stored_sha is None or content_sha256(core) != stored_sha:
        return "settlement_version_binding_content_drift", None
    expected = {
        "fact_id": fact.get("fact_id"),
        "fact_content_sha256": fact.get("content_sha256"),
        "station_id": fact.get("station_id"),
        "event_date": fact.get("event_date"),
        "settlement_timezone": fact.get("settlement_timezone"),
        "settlement_closed_at_utc": label.get(
            "settlement_closed_at_utc"
        ),
        "truth_id": label.get("truth_id"),
        "truth_revision_no": int(label.get("truth_revision_no")),
        "truth_sha256": label.get("truth_sha256"),
        "probability_envelope_id": fact.get(
            "probability_envelope_id"
        ),
        "posterior_sha256": fact.get("posterior_sha256"),
        "ws_snapshot_id": fact.get("ws_snapshot_id"),
        "quote_sha256": fact.get("quote_sha256"),
        "inventory_snapshot_id": fact.get("inventory_snapshot_id"),
        "inventory_sha256": fact.get("inventory_sha256"),
        "order_state_snapshot_id": fact.get(
            "order_state_snapshot_id"
        ),
        "order_state_sha256": fact.get("order_state_sha256"),
        "cycle_id": fact.get("cycle_id"),
        "decision_id": fact.get("decision_id"),
        "decision_sha256": fact.get("decision_sha256"),
        "plan_sha256": fact.get("plan_sha256"),
        "policy_version_vector_sha256": policy_vector_sha256,
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        return "settlement_version_binding_lineage_drift", None
    return "BOUND", stored_sha


def _terms(
    outcome: Mapping[str, Any],
    *,
    fact: Mapping[str, Any],
    label: Mapping[str, Any],
    policy_vector_sha256: str,
    counterfactual_pnl: float,
) -> tuple[dict[str, float] | None, str | None]:
    raw = outcome.get("settled_attribution")
    if raw is None:
        return None, "NA_ATTRIBUTION_TERMS_MISSING"
    if not isinstance(raw, Mapping):
        return None, "settled_attribution_not_mapping"
    if raw.get("schema") != TERMS_SCHEMA:
        return None, "settled_attribution_schema_invalid"
    if raw.get("content_sha256") is not None:
        validated, validation_error = validate_attribution_terms(
            raw,
            expected_fact_id=str(fact.get("fact_id") or ""),
            expected_fact_content_sha256=str(
                fact.get("content_sha256") or ""
            ),
            expected_truth_sha256=str(label.get("truth_sha256") or ""),
            expected_policy_version_vector_sha256=policy_vector_sha256,
        )
        if validation_error or validated is None:
            return None, validation_error or "settled_attribution_terms_invalid"
    if raw.get("fact_id") != fact.get("fact_id"):
        return None, "settled_attribution_fact_id_drift"
    if _sha(raw.get("fact_content_sha256")) != _sha(fact.get("content_sha256")):
        return None, "settled_attribution_fact_sha_drift"
    if _sha(raw.get("truth_sha256")) != _sha(label.get("truth_sha256")):
        return None, "settled_attribution_truth_sha_drift"
    if raw.get("winning_bucket") != label.get("winning_bucket"):
        return None, "settled_attribution_winning_bucket_drift"
    if _sha(raw.get("policy_version_vector_sha256")) != policy_vector_sha256:
        return None, "settled_attribution_policy_vector_drift"
    coverage = _number(raw.get("coverage_usdc"))
    overlap = _number(raw.get("overlap_usdc"))
    edge = _number(raw.get("edge_usdc"))
    execution_cost = _number(raw.get("execution_cost_usdc"), nonnegative=True)
    failure_cost = _number(raw.get("failure_cost_usdc"), nonnegative=True)
    reported = _number(raw.get("counterfactual_pnl_usdc"))
    if None in {coverage, overlap, edge, execution_cost, failure_cost, reported}:
        return None, "settled_attribution_terms_invalid"
    assert coverage is not None and overlap is not None and edge is not None
    assert execution_cost is not None and failure_cost is not None and reported is not None
    if abs(reported - counterfactual_pnl) > EPS:
        return None, "settled_attribution_counterfactual_pnl_drift"
    closed = coverage + overlap + edge - execution_cost - failure_cost
    if abs(closed - counterfactual_pnl) > EPS:
        return None, "settled_attribution_closure_failed"
    return {
        "coverage_usdc": coverage,
        "overlap_usdc": overlap,
        "edge_usdc": edge,
        "execution_cost_usdc": execution_cost,
        "failure_cost_usdc": failure_cost,
        "counterfactual_pnl_usdc": counterfactual_pnl,
        **(
            {"content_sha256": str(raw["content_sha256"])}
            if raw.get("content_sha256") is not None
            else {}
        ),
    }, None


def _settlement_evidence_packet(
    outcome: Mapping[str, Any],
    *,
    binding_sha256: str | None,
    terms: Mapping[str, Any] | None,
) -> tuple[str, str | None]:
    raw = outcome.get("settlement_evidence_packet")
    if raw is None:
        return "NA_SETTLEMENT_EVIDENCE_PACKET_MISSING", None
    packet, error = validate_settlement_version_evidence_packet(
        raw,
        expected_binding_sha256=binding_sha256,
    )
    if error or packet is None:
        return error or "settlement_evidence_packet_invalid", None
    packet_terms = packet.get("attribution_terms")
    if terms is None or not isinstance(packet_terms, Mapping):
        return "settlement_evidence_packet_terms_missing", None
    if packet_terms.get("content_sha256") != terms.get("content_sha256"):
        return "settlement_evidence_packet_terms_drift", None
    packet_sha = str(packet.get("content_sha256") or "")
    if outcome.get("settlement_evidence_packet_sha256") != packet_sha:
        return "settlement_evidence_packet_sha256_drift", None
    return "BOUND", packet_sha


def _empty_totals() -> dict[str, float]:
    return {
        "coverage_usdc": 0.0,
        "overlap_usdc": 0.0,
        "edge_usdc": 0.0,
        "execution_cost_usdc": 0.0,
        "failure_cost_usdc": 0.0,
        "counterfactual_pnl_usdc": 0.0,
    }


def build_settled_attribution(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    fact_id: str | None = None,
    max_facts: int = MAX_FACTS,
    after_fact_rowid: int = 0,
    station_ids: Sequence[str] | None = None,
    window_start_date: str | None = None,
    window_end_date: str | None = None,
) -> dict[str, Any]:
    """Project settled labels into a bounded, non-authorizing audit receipt.

    It accepts a connection opened by the caller but never calls ``commit`` or
    executes a mutating statement.  Hash, version-vector and label-chain
    drift are hard blockers.  A missing or not-yet-settled label is explicit
    NA, allowing a chronological audit without waiting for future days.
    """

    as_of, _as_of_dt = _utc(as_of_utc, name="as_of_utc")
    if isinstance(max_facts, bool) or not isinstance(max_facts, int) or not 1 <= max_facts <= MAX_FACTS:
        raise ContractError("settled_attribution_max_facts_invalid")
    if (
        isinstance(after_fact_rowid, bool)
        or not isinstance(after_fact_rowid, int)
        or after_fact_rowid < 0
    ):
        raise ContractError("settled_attribution_cursor_invalid")
    if fact_id is not None and (not isinstance(fact_id, str) or not fact_id.strip()):
        raise ContractError("settled_attribution_fact_id_invalid")
    station_scope: tuple[str, ...] | None = None
    if station_ids is not None:
        station_scope = tuple(sorted({
            str(value).strip().upper()
            for value in station_ids
            if str(value).strip()
        }))
        if not station_scope or len(station_scope) > expected_station_count():
            raise ContractError("settled_attribution_station_scope_invalid")
    if (window_start_date is None) != (window_end_date is None):
        raise ContractError("settled_attribution_window_incomplete")
    if window_start_date is not None:
        try:
            start_date = datetime.strptime(
                str(window_start_date), "%Y-%m-%d"
            ).date().isoformat()
            end_date = datetime.strptime(
                str(window_end_date), "%Y-%m-%d"
            ).date().isoformat()
        except (TypeError, ValueError) as exc:
            raise ContractError("settled_attribution_window_invalid") from exc
        if start_date > end_date:
            raise ContractError("settled_attribution_window_reversed")
    else:
        start_date = None
        end_date = None
    required_fact = set(FACT_COLUMNS)
    required_label = {"label_id", *_LABEL_HASH_FIELDS, "content_sha256"}
    table_errors: list[str] = []
    for table, required in (
        ("basket_policy_training_fact", required_fact),
        ("basket_policy_training_label", required_label),
    ):
        existing = _table_columns(conn, table)
        if not existing:
            table_errors.append(f"source_table_missing:{table}")
        elif missing := sorted(required - existing):
            table_errors.append("source_schema_incomplete:" + table + ":" + ",".join(missing))
    if table_errors:
        identity = {
            "schema": SCHEMA,
            "as_of_utc": as_of,
            "source_cursor_before": after_fact_rowid,
            "source_cursor_after": after_fact_rowid,
            "has_more": False,
            "state": "BLOCKED_FAIL_CLOSED",
            "source_tables": ["basket_policy_training_fact", "basket_policy_training_label"],
            "rows": [],
            "station_ids": (
                list(station_scope)
                if station_scope is not None else None
            ),
            "window_start_date": start_date,
            "window_end_date": end_date,
            "blockers": sorted(table_errors),
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
            "writes_production_db": False,
        }
        return {**identity, "content_sha256": content_sha256(identity)}

    params: list[Any] = [FACT_AUTHORITY_SCOPE]
    where = "authority_scope=? AND fact_id LIKE 'd2fact_%' AND rowid>?"
    params.append(after_fact_rowid)
    if fact_id is not None:
        where += " AND fact_id=?"
        params.append(fact_id.strip())
    if station_scope is not None:
        where += " AND station_id IN (" + ",".join(
            "?" for _ in station_scope
        ) + ")"
        params.extend(station_scope)
    if start_date is not None:
        where += " AND event_date>=? AND event_date<=?"
        params.extend((start_date, end_date))
    cursor = conn.execute(
        "SELECT rowid AS source_rowid,*,length(CAST(policy_version_vector_json AS BLOB)) AS policy_bytes,"
        "length(CAST(feature_json AS BLOB)) AS feature_bytes "
        "FROM basket_policy_training_fact WHERE " + where + " "
        "ORDER BY rowid,fact_id LIMIT ?",
        (*params, max_facts + 1),
    )
    raw_facts = cursor.fetchall()
    has_more = len(raw_facts) > max_facts
    raw_facts = raw_facts[:max_facts]
    facts = [_row(cursor, raw) for raw in raw_facts]
    total_bytes = sum(int(fact.get("policy_bytes") or 0) + int(fact.get("feature_bytes") or 0) for fact in facts)
    if total_bytes > MAX_FACTS * MAX_JSON_BYTES * 2:
        raise ContractError("settled_attribution_source_payload_out_of_bounds")
    assert_process_memory_budget(projected_bytes=total_bytes * 3)

    rows: list[dict[str, Any]] = []
    hard_blockers: list[str] = []
    aggregate: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "fact_count": 0,
        "attributed_count": 0,
        "counterfactual_pnl_count": 0,
        "realized_pnl_status": "NA_PRODUCTION_NO_LIVE",
        "totals": _empty_totals(),
    })
    labels_by_fact = _select_label_batch(
        conn,
        fact_ids=[str(fact.get("fact_id") or "") for fact in facts],
        as_of_utc=as_of,
    )
    for fact in facts:
        fact_key = str(fact.get("fact_id") or "")
        row_result: dict[str, Any] = {
            "fact_id": fact_key,
            "station_id": str(fact.get("station_id") or ""),
            "event_date": str(fact.get("event_date") or ""),
            "stage": str(fact.get("stage") or ""),
            "status": "BLOCKED_FAIL_CLOSED",
            "counterfactual_pnl_usdc": None,
            "realized_pnl_usdc": None,
            "realized_pnl_status": "NA_PRODUCTION_NO_LIVE",
            "attribution_terms": None,
            "blockers": [],
        }
        blockers: list[str] = []
        if error := _fact_hash_error(fact):
            blockers.append(error)
        policy, policy_error = _bounded_mapping(
            fact.get("policy_version_vector_json"), byte_length=fact.get("policy_bytes")
        )
        feature, feature_error = _bounded_mapping(
            fact.get("feature_json"), byte_length=fact.get("feature_bytes")
        )
        if policy_error:
            blockers.append("policy_version_vector_" + policy_error)
        if feature_error:
            blockers.append("feature_" + feature_error)
        policy_sha = None
        if policy is not None:
            if error := _policy_vector_error(policy):
                blockers.append(error)
            else:
                policy_sha = content_sha256(policy)
                row_result["policy_version_vector_sha256"] = policy_sha
                row_result["policy_version"] = {
                    key: policy[key] for key in _POLICY_REQUIRED_TEXT
                }
        if feature is not None:
            error, vector_sha256s = _feature_vector_error(
                feature, intent_count=fact.get("intent_count")
            )
            if error:
                blockers.append(error)
            else:
                row_result["execution_version_vector_sha256s"] = vector_sha256s
        if blockers:
            row_result["blockers"] = sorted(set(blockers))
            hard_blockers.extend(row_result["blockers"])
            rows.append(row_result)
            continue
        assert policy_sha is not None
        group = aggregate[policy_sha]
        group["fact_count"] += 1
        label, label_errors = labels_by_fact.get(
            fact_key,
            (None, ["settlement_label_missing_or_not_yet_closed"]),
        )
        if label_errors:
            if label is None and label_errors == ["settlement_label_missing_or_not_yet_closed"]:
                row_result["status"] = "NA_UNSETTLED_OR_LABEL_MISSING"
                row_result["na_reason"] = label_errors[0]
            else:
                row_result["blockers"] = label_errors
                hard_blockers.extend(label_errors)
            rows.append(row_result)
            continue
        assert label is not None
        outcome, outcome_error = _bounded_mapping(
            label.get("outcome_json"), byte_length=label.get("outcome_bytes")
        )
        if outcome_error:
            row_result["blockers"] = ["label_outcome_" + outcome_error]
            hard_blockers.extend(row_result["blockers"])
            rows.append(row_result)
            continue
        assert outcome is not None
        binding_status, binding_sha = _settlement_version_binding(
            outcome,
            fact=fact,
            label=label,
            policy_vector_sha256=policy_sha,
        )
        row_result["settlement_version_binding_status"] = (
            binding_status
        )
        row_result["settlement_version_binding_sha256"] = binding_sha
        if (
            binding_status != "BOUND"
            and not binding_status.startswith("NA_")
        ):
            row_result["blockers"] = [binding_status]
            hard_blockers.extend(row_result["blockers"])
            rows.append(row_result)
            continue
        pnl, pnl_error = _counterfactual_pnl(outcome)
        if pnl_error:
            row_result["blockers"] = [pnl_error]
            hard_blockers.extend(row_result["blockers"])
            rows.append(row_result)
            continue
        assert pnl is not None
        row_result["label_id"] = str(label.get("label_id") or "")
        row_result["truth_sha256"] = str(label.get("truth_sha256") or "")
        row_result["winning_bucket"] = str(label.get("winning_bucket") or "")
        row_result["counterfactual_pnl_usdc"] = pnl
        group["counterfactual_pnl_count"] += 1
        terms, terms_error = _terms(
            outcome,
            fact=fact,
            label=label,
            policy_vector_sha256=policy_sha,
            counterfactual_pnl=pnl,
        )
        if terms_error:
            if terms_error == "NA_ATTRIBUTION_TERMS_MISSING":
                row_result["status"] = terms_error
                row_result["na_reason"] = terms_error
            else:
                row_result["blockers"] = [terms_error]
                hard_blockers.extend(row_result["blockers"])
            rows.append(row_result)
            continue
        assert terms is not None
        packet_status, packet_sha = _settlement_evidence_packet(
            outcome,
            binding_sha256=binding_sha,
            terms=terms,
        )
        row_result["settlement_evidence_packet_status"] = packet_status
        row_result["settlement_evidence_packet_sha256"] = packet_sha
        if packet_status not in {"BOUND", "NA_SETTLEMENT_EVIDENCE_PACKET_MISSING"}:
            row_result["blockers"] = [packet_status]
            hard_blockers.extend(row_result["blockers"])
            rows.append(row_result)
            continue
        row_result["status"] = "COUNTERFACTUAL_CLOSED"
        row_result["attribution_terms"] = terms
        group["attributed_count"] += 1
        for name in _empty_totals():
            group["totals"][name] += float(terms[name])
        rows.append(row_result)

    for value in aggregate.values():
        totals = value["totals"]
        value["counterfactual_closure_usdc"] = (
            totals["coverage_usdc"] + totals["overlap_usdc"] + totals["edge_usdc"]
            - totals["execution_cost_usdc"] - totals["failure_cost_usdc"]
            - totals["counterfactual_pnl_usdc"]
        )
        if abs(value["counterfactual_closure_usdc"]) > EPS:
            hard_blockers.append("aggregate_counterfactual_closure_failed")

    any_na = any(str(row.get("status", "")).startswith("NA_") for row in rows)
    source_cursor_after = max(
        [after_fact_rowid] + [int(fact["source_rowid"]) for fact in facts]
    )
    if hard_blockers:
        state = "BLOCKED_FAIL_CLOSED"
    elif not facts:
        state = "NO_SETTLED_FACTS_NA"
    elif any_na:
        state = "INCOMPLETE_NA"
    else:
        state = "COMPLETE_COUNTERFACTUAL_NO_LIVE"
    identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of,
        "state": state,
        "source_tables": ["basket_policy_training_fact", "basket_policy_training_label"],
        "fact_count": len(facts),
        "station_ids": (
            list(station_scope) if station_scope is not None else None
        ),
        "window_start_date": start_date,
        "window_end_date": end_date,
        "source_cursor_before": after_fact_rowid,
        "source_cursor_after": source_cursor_after,
        "has_more": has_more,
        "rows": rows,
        "by_policy_version_vector": {
            key: aggregate[key] for key in sorted(aggregate)
        },
        "blockers": sorted(set(hard_blockers)),
        "counterfactual_only": True,
        "realized_pnl_status": "NA_PRODUCTION_NO_LIVE",
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
        "writes_production_db": False,
        "creates_parallel_store": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "FACT_AUTHORITY_SCOPE",
    "MAX_FACTS",
    "SCHEMA",
    "TERMS_SCHEMA",
    "build_settled_attribution",
]
