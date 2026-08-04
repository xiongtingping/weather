"""Read-only station-policy outcome-contract gap report.

Station policy is not a second weather forecaster.  The versioned raw-evidence
and metric declaration is immutable and present, while each decision fact
remains unbound until independently joined to candidate/baseline plans,
official CLOB WS, direct fill receipts and closed settlement truth.  Neither
state permits a candidate score, a baseline score, a prediction, or an action
proxy.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .station_policy_candidate import assert_candidate_bundle_boundary
from .station_policy_forward_outcome_binding import (
    HISTORICAL_STRICT_OOS,
    MATERIALIZATION_SCHEMA,
    METRIC_CONTRACT_STATE,
    OBSERVATIONAL_CANDIDATE_NOT_OOS,
    POLICY_FAMILIES,
    UNAVAILABLE_REASONS,
    assert_future_station_policy_outcome_materialization,
    build_station_policy_utility_contract,
    build_station_policy_utility_contract_pointer,
    validate_historical_station_policy_utility_binding,
)
from .settlement_time import settlement_day_window


SCHEMA = "station_policy_exact_stage_scorecard_v1"
STAGES = ("P0", "P0-M", "P1")
MAX_FACT_ROWS = 100_000
MAX_JSON_BYTES = 131_072
# A 45-station × three-family dry-run can legitimately retain more than the
# former 64 MiB cumulative evidence window.  Widen only this aggregate
# buffer; the 128 KiB per-cell cap, row cap, RSS guard and database bounds
# remain unchanged.
MAX_TOTAL_JSON_BYTES = 128 * 1024 * 1024
MAX_EVIDENCE_REFERENCE_ROWS = 100_000

# These evidence shapes are bound to the versioned metric declaration.  A
# forecast, posterior, outcome bucket, or decision reason is never a
# policy-utility substitute.  The dry-run exposes which defined raw inputs
# are still unbound per station without granting execution authority.
UTILITY_EVIDENCE_CONTRACTS: dict[str, dict[str, Any]] = {
    "target": {
        "frozen_decision_output": "target_yes_inventory_vector",
        "official_clob_ws": (
            "content_addressed_yes_book_snapshot_and_all_in_yes_ask_cost"
        ),
        "fill": "confirmed_yes_fill_or_explicit_zero_fill_receipt",
        "settlement": "frozen_official_settlement_label_and_yes_payout",
        "metric_state": METRIC_CONTRACT_STATE,
    },
    "reserve": {
        "frozen_decision_output": "yes_maker_reservation_price_and_size",
        "official_clob_ws": "content_addressed_yes_book_snapshot",
        "fill": "confirmed_yes_fill_and_queue_or_cancel_receipt",
        "settlement": "frozen_official_settlement_label_and_yes_payout",
        "metric_state": METRIC_CONTRACT_STATE,
    },
    "cadence": {
        "frozen_decision_output": "yes_execution_timing_or_wait_decision",
        "official_clob_ws": "content_addressed_yes_book_snapshot_at_decision_and_expiry",
        "fill": "confirmed_yes_fill_or_explicit_zero_fill_receipt",
        "settlement": "frozen_official_settlement_label_and_yes_payout",
        "metric_state": METRIC_CONTRACT_STATE,
    },
}

# The fact table can prove that a decision referenced immutable input objects.
# It deliberately does not claim that those references form a post-label
# utility outcome; an independently frozen binding remains required.
EVIDENCE_REFERENCE_COLUMNS = {
    "official_clob_ws_snapshot": "ws_snapshot_id",
    "official_clob_ws_quote": "quote_sha256",
    "inventory": "inventory_snapshot_id",
    "order_state": "order_state_snapshot_id",
    "decision": "decision_id",
    "plan": "plan_sha256",
    "yes_intent_set": "intent_set_sha256",
}
SETTLEMENT_REFERENCE_COLUMNS = {
    "official_settlement_truth": "truth_sha256",
    "official_settlement_outcome": "outcome_json",
}


def _utc(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"station_policy_scorecard_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"station_policy_scorecard_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: str, name: str) -> str:
    return _utc(value, name).isoformat().replace("+00:00", "Z")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _bounded_mapping(raw: Any, byte_length: Any) -> tuple[Mapping[str, Any] | None, str | None]:
    try:
        size = int(byte_length)
    except (TypeError, ValueError):
        return None, "feature_json_size_invalid"
    if size < 0 or size > MAX_JSON_BYTES or not isinstance(raw, str):
        return None, "feature_json_unavailable_or_oversized"
    if len(raw.encode("utf-8")) != size:
        return None, "feature_json_size_mismatch"
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "feature_json_malformed"
    return (value, None) if isinstance(value, Mapping) else (None, "feature_json_not_mapping")


def _partition(partition_key: str) -> tuple[str, str]:
    parts = str(partition_key or "").split("|")
    season = parts[1].strip() if len(parts) > 1 else ""
    lead = parts[3].strip() if len(parts) > 3 else ""
    return season or "UNKNOWN", lead or "UNKNOWN"


def _outcome_sha256(value: Any) -> str:
    """Return a canonical outcome hash, or an empty value for bad lineage."""

    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    return content_sha256(parsed) if isinstance(parsed, Mapping) else ""


def _label_rows(
    conn: sqlite3.Connection, *, as_of_utc: str
) -> tuple[dict[str, dict[str, Any]], Counter[str], Counter[str]]:
    """Read the latest closed label per fact and retain AWC lineage state.

    ``basket_policy_training_label`` is append-only and may retain earlier
    truth revisions.  A set of fact ids is therefore not enough for a strict
    historical utility join: the scorecard must identify the latest closed
    label and verify that its truth object is accepted AWC, rather than
    accidentally accepting an older revision or a retired source lane.

    Small in-memory tests and older read-only exports can legitimately lack
    the AWC truth table.  Those labels remain visible as *frozen label
    references*, but are explicitly unverified and never make a utility
    binding scoreable.
    """

    labels: dict[str, dict[str, Any]] = {}
    counters: Counter[str] = Counter()
    settlement_references: Counter[str] = Counter()
    if not _table_exists(conn, "basket_policy_training_label"):
        counters["label_table_missing"] += 1
        return labels, counters, settlement_references
    required = {
        "label_id", "fact_id", "truth_revision_no", "winning_bucket",
        "settlement_closed_at_utc", "labeled_at_utc", "authority_scope",
    }
    if required - _columns(conn, "basket_policy_training_label"):
        counters["label_schema_missing"] += 1
        return labels, counters, settlement_references
    columns = _columns(conn, "basket_policy_training_label")
    optional = {
        reference: column
        for reference, column in SETTLEMENT_REFERENCE_COLUMNS.items()
        if column in columns
    }
    for reference in set(SETTLEMENT_REFERENCE_COLUMNS) - set(optional):
        counters[f"{reference}_reference_column_missing"] += 1
    selected_optional = "".join(
        f",l.{column} AS {reference}"
        for reference, column in sorted(optional.items())
    )
    label_lineage_columns = {
        "truth_id": "truth_id",
        "truth_sha256": "truth_sha256",
        "outcome_json": "outcome_json",
        "content_sha256": "content_sha256",
    }
    selected_lineage = "".join(
        f",l.{column} AS label_{name}"
        for name, column in label_lineage_columns.items()
        if column in columns
    )
    missing_lineage = {
        name for name, column in label_lineage_columns.items()
        if column not in columns
    }
    for name in sorted(missing_lineage):
        counters[f"label_{name}_column_missing"] += 1

    truth_table = "calibration_station_daily_high_truth"
    truth_required = {
        "truth_id", "station_id", "local_date", "source_lane", "qc_status",
        "authority_scope", "content_sha256",
    }
    truth_table_usable = (
        "truth_id" in columns
        and _table_exists(conn, truth_table)
        and truth_required <= _columns(conn, truth_table)
    )
    if not truth_table_usable:
        counters["official_awc_truth_lineage_unverified"] += 1
    truth_projection = (
        ",t.station_id AS truth_station_id,t.local_date AS truth_local_date,"
        "t.source_lane AS truth_source_lane,t.qc_status AS truth_qc_status,"
        "t.authority_scope AS truth_authority_scope,"
        "t.content_sha256 AS truth_content_sha256"
        if truth_table_usable
        else ""
    )
    truth_join = (
        f" LEFT JOIN {truth_table} t ON t.truth_id=l.truth_id"
        if truth_table_usable
        else ""
    )
    cursor = conn.execute(
        "SELECT l.fact_id,l.label_id,l.truth_revision_no,l.winning_bucket,"
        "l.settlement_closed_at_utc,l.labeled_at_utc,l.authority_scope "
        + selected_optional
        + selected_lineage
        + truth_projection
        + " FROM basket_policy_training_label l"
        + truth_join
        + " WHERE julianday(l.settlement_closed_at_utc)<=julianday(?) "
        "AND julianday(l.labeled_at_utc)<=julianday(?) "
        "ORDER BY l.fact_id,l.truth_revision_no DESC,l.labeled_at_utc DESC,l.label_id DESC",
        (as_of_utc, as_of_utc),
    )
    for row in cursor:
        fact_id, label_id, revision_no, winning_bucket, settled_at, labeled_at, label_authority = row[:7]
        text = str(fact_id or "").strip()
        if not text:
            counters["label_fact_id_missing"] += 1
            continue
        # The query is newest-first.  An invalid latest label must remain an
        # explicit gap; silently falling back to an earlier truth revision is
        # precisely the revision leak that strict OOS must avoid.
        if text in labels:
            counters["superseded_label_not_used"] += 1
            continue
        try:
            settled_text = _utc_text(str(settled_at), "settlement_closed_at")
            labeled_text = _utc_text(str(labeled_at), "labeled_at")
        except ContractError:
            counters["label_timestamp_invalid"] += 1
            continue
        if _utc(labeled_text, "labeled_at") < _utc(settled_text, "settlement_closed_at"):
            counters["label_before_settlement_close"] += 1
        optional_offset = 7
        values = {
            reference: row[offset]
            for offset, reference in enumerate(
                sorted(optional), start=optional_offset
            )
        }
        lineage_offset = optional_offset + len(optional)
        lineage_values: dict[str, Any] = {}
        for offset, name in enumerate(
            [name for name, column in label_lineage_columns.items() if column in columns],
            start=lineage_offset,
        ):
            lineage_values[name] = row[offset]
        truth_offset = lineage_offset + len(lineage_values)
        truth_values: dict[str, Any] = {}
        if truth_table_usable:
            for offset, name in enumerate(
                (
                    "station_id", "local_date", "source_lane", "qc_status",
                    "authority_scope", "content_sha256",
                ),
                start=truth_offset,
            ):
                truth_values[name] = row[offset]
        truth_source_lane = str(truth_values.get("source_lane") or "").strip().upper()
        truth_qc_status = str(truth_values.get("qc_status") or "").strip().upper()
        official_awc_truth = (
            truth_table_usable
            and truth_source_lane == "AWC"
            and truth_qc_status == "ACCEPTED"
            and bool(str(truth_values.get("content_sha256") or "").strip())
        )
        labels[text] = {
            "fact_id": text,
            "label_id": str(label_id or "").strip(),
            "truth_revision_no": revision_no,
            "winning_bucket": str(winning_bucket or "").strip(),
            "settlement_closed_at_utc": settled_text,
            "labeled_at_utc": labeled_text,
            "label_authority_scope": str(label_authority or "").strip(),
            "truth_id": str(lineage_values.get("truth_id") or "").strip(),
            "truth_sha256": str(lineage_values.get("truth_sha256") or "").strip().lower(),
            "outcome_sha256": _outcome_sha256(
                lineage_values.get("outcome_json")
            ),
            "label_content_sha256": str(lineage_values.get("content_sha256") or "").strip().lower(),
            "truth_source_lane": truth_source_lane,
            "truth_qc_status": truth_qc_status,
            "truth_station_id": str(truth_values.get("station_id") or "").strip().upper(),
            "truth_local_date": str(truth_values.get("local_date") or "").strip(),
            "truth_authority_scope": str(truth_values.get("authority_scope") or "").strip(),
            "official_awc_truth": official_awc_truth,
            "truth_lineage_state": (
                "OFFICIAL_AWC_ACCEPTED" if official_awc_truth
                else ("UNVERIFIED_TABLE_MISSING" if not truth_table_usable else "NOT_OFFICIAL_AWC_ACCEPTED")
            ),
        }
        # ``selected_optional`` is emitted in sorted reference order.  Keep
        # the positional decoding in that same order: otherwise a partially
        # populated label can be reported as having the wrong settlement
        # evidence field (for example, ``outcome_json`` as truth lineage).
        for reference, value in values.items():
            if str(value or "").strip():
                settlement_references[reference] += 1
    counters["latest_frozen_label_count"] = len(labels)
    counters["official_awc_frozen_label_count"] = sum(
        label["official_awc_truth"] is True for label in labels.values()
    )
    return labels, counters, settlement_references


def _source_summary(stages: Sequence[str]) -> dict[str, Any]:
    return {
        "facts_read": 0,
        "facts_with_frozen_label": 0,
        "facts_with_official_awc_frozen_label": 0,
        # Future/open days are arrival/interface observations, not OOS facts
        # and not production_no_live blockers.  Keep their count separate so
        # they cannot accidentally inflate history or masquerade as a gap in
        # the closed historical lane.
        "observational_candidate_not_oos_count": 0,
        "observational_candidate_not_oos_by_stage": {
            stage: 0 for stage in stages
        },
        "score_rows": {stage: {family: 0 for family in POLICY_FAMILIES} for stage in stages},
        "family_outcome_materialization": {
            stage: {
                family: {
                    "frozen_labeled_fact_count": 0,
                    "unavailable_frozen_outcome_materialization_count": 0,
                    "legacy_frozen_fact_without_materialization_count": 0,
                    "unavailable_reasons": Counter(),
                }
                for family in POLICY_FAMILIES
            }
            for stage in stages
        },
        "stage_outcome_materialization_states": {stage: Counter() for stage in stages},
        "stage_station_ids": {stage: set() for stage in stages},
        "stage_labeled_station_ids": {stage: set() for stage in stages},
        "stage_seasons": {stage: set() for stage in stages},
        "stage_leads": {stage: set() for stage in stages},
        "station_fact_counts": {
            stage: Counter() for stage in stages
        },
        "station_labeled_fact_counts": {
            stage: Counter() for stage in stages
        },
        "station_blockers": {
            stage: {} for stage in stages
        },
        "station_evidence_reference_counts": {
            stage: {}
            for stage in stages
        },
        "evidence_reference_counts": {
            stage: Counter() for stage in stages
        },
        "blockers": Counter(),
    }


def _facts(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    stages: Sequence[str],
    labels: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Read append-only facts and count explicit post-label contract gaps."""

    summary = _source_summary(stages)
    if not _table_exists(conn, "basket_policy_training_fact"):
        summary["blockers"]["training_fact_table_missing"] += 1
        return summary
    required = {
        "fact_id", "station_id", "event_date", "settlement_timezone",
        "decision_at_utc", "captured_at_utc", "stage", "partition_key",
        "feature_json", "created_at_utc",
    }
    if required - _columns(conn, "basket_policy_training_fact"):
        summary["blockers"]["training_fact_schema_missing"] += 1
        return summary
    columns = _columns(conn, "basket_policy_training_fact")
    optional = {
        reference: column
        for reference, column in EVIDENCE_REFERENCE_COLUMNS.items()
        if column in columns
    }
    for reference in set(EVIDENCE_REFERENCE_COLUMNS) - set(optional):
        summary["blockers"][f"{reference}_reference_column_missing"] += 1
    selected_optional = "".join(
        f",{column} AS {reference}"
        for reference, column in sorted(optional.items())
    )
    placeholders = ",".join("?" for _ in stages)
    cursor = conn.execute(
        "SELECT fact_id,station_id,stage,partition_key,"
        "length(CAST(feature_json AS BLOB)) AS feature_bytes,"
        "CASE WHEN length(CAST(feature_json AS BLOB))<=? THEN feature_json ELSE NULL END "
        + selected_optional
        + ",event_date AS fact_event_date,settlement_timezone AS fact_settlement_timezone "
        + " "
        "FROM basket_policy_training_fact WHERE stage IN (" + placeholders + ") "
        "AND julianday(decision_at_utc)<=julianday(?) "
        "AND julianday(captured_at_utc)<=julianday(?) "
        "AND julianday(created_at_utc)<=julianday(?) "
        "ORDER BY stage,station_id,fact_id",
        (MAX_JSON_BYTES, *stages, as_of_utc, as_of_utc, as_of_utc),
    )
    retained_bytes = 0
    for row in cursor:
        fact_id_raw, station_id_raw, stage_raw, partition_key, feature_bytes, raw_feature = row[:6]
        summary["facts_read"] += 1
        if summary["facts_read"] > MAX_FACT_ROWS:
            raise ContractError("station_policy_scorecard_fact_limit_exceeded")
        fact_id = str(fact_id_raw or "").strip()
        station_id = str(station_id_raw or "").strip().upper()
        stage = str(stage_raw or "").strip()
        if not fact_id or not station_id or stage not in summary["stage_station_ids"]:
            summary["blockers"]["fact_identity_or_stage_invalid"] += 1
            continue
        if not fact_id.startswith("d2fact_"):
            summary["blockers"]["retired_non_v2_fact_excluded"] += 1
            summary["station_blockers"][stage].setdefault(
                station_id, Counter()
            )["retired_non_v2_fact_excluded"] += 1
            continue
        metadata_offset = 6 + len(optional)
        fact_event_date = str(row[metadata_offset] or "").strip()
        fact_settlement_timezone = str(row[metadata_offset + 1] or "").strip()
        try:
            settlement_closed_at = _utc(
                settlement_day_window(
                    fact_event_date, fact_settlement_timezone
                )["target_day_end_utc"],
                "future_event_settlement_closed_at",
            )
        except ContractError:
            summary["blockers"]["fact_settlement_scope_invalid"] += 1
            summary["station_blockers"].setdefault(stage, {}).setdefault(
                station_id, Counter()
            )["fact_settlement_scope_invalid"] += 1
            continue
        if settlement_closed_at > _utc(as_of_utc, "as_of"):
            # Keep this row visible as a bounded diagnostic, but never let an
            # open/future settlement day contribute to station coverage,
            # frozen-label counts, or any exact-stage score.
            summary["observational_candidate_not_oos_count"] += 1
            summary["observational_candidate_not_oos_by_stage"][stage] += 1
            continue
        summary["stage_station_ids"][stage].add(station_id)
        summary["station_fact_counts"][stage][station_id] += 1
        season, lead = _partition(str(partition_key or ""))
        summary["stage_seasons"][stage].add(season)
        summary["stage_leads"][stage].add(lead)
        label = labels.get(fact_id)
        if label is None:
            summary["blockers"]["fact_without_frozen_label"] += 1
            summary["station_blockers"][stage].setdefault(
                station_id, Counter()
            )["fact_without_frozen_label"] += 1
            continue
        summary["facts_with_frozen_label"] += 1
        summary["stage_labeled_station_ids"][stage].add(station_id)
        summary["station_labeled_fact_counts"][stage][station_id] += 1
        if label.get("official_awc_truth") is True:
            summary["facts_with_official_awc_frozen_label"] += 1
        else:
            summary["blockers"]["frozen_label_official_awc_truth_unverified"] += 1
            summary["station_blockers"][stage].setdefault(
                station_id, Counter()
            )["frozen_label_official_awc_truth_unverified"] += 1
        station_references = summary["station_evidence_reference_counts"][stage].setdefault(
            station_id, Counter()
        )
        # Match the sorted projection above.  This only corrects audit field
        # attribution; it never changes a fact, a label, or utility state.
        for offset, reference in enumerate(sorted(optional), start=6):
            if str(row[offset] or "").strip():
                summary["evidence_reference_counts"][stage][reference] += 1
                station_references[reference] += 1
        feature, feature_error = _bounded_mapping(raw_feature, feature_bytes)
        if feature is None:
            summary["blockers"][str(feature_error)] += 1
            summary["station_blockers"][stage].setdefault(
                station_id, Counter()
            )[str(feature_error)] += 1
            continue
        retained_bytes += int(feature_bytes or 0)
        if retained_bytes > MAX_TOTAL_JSON_BYTES:
            raise ContractError("station_policy_scorecard_json_limit_exceeded")
        materialization = feature.get("station_policy_outcome_materialization")
        is_current_skeleton = isinstance(materialization, Mapping) and materialization.get("schema") == MATERIALIZATION_SCHEMA
        if is_current_skeleton:
            try:
                assert_future_station_policy_outcome_materialization(materialization)
            except ValueError:
                is_current_skeleton = False
                summary["stage_outcome_materialization_states"][stage]["INVALID"] += 1
                summary["blockers"]["invalid_station_policy_outcome_materialization"] += 1
            else:
                summary["stage_outcome_materialization_states"][stage]["UNAVAILABLE"] += 1
        else:
            summary["stage_outcome_materialization_states"][stage]["LEGACY_FACT_NO_OUTCOME_MATERIALIZATION"] += 1
        for family in POLICY_FAMILIES:
            family_summary = summary["family_outcome_materialization"][stage][family]
            family_summary["frozen_labeled_fact_count"] += 1
            family_summary["unavailable_frozen_outcome_materialization_count"] += 1
            if is_current_skeleton:
                family_summary["unavailable_reasons"][UNAVAILABLE_REASONS[family]] += 1
            else:
                family_summary["legacy_frozen_fact_without_materialization_count"] += 1
                family_summary["unavailable_reasons"]["LEGACY_FACT_NO_OUTCOME_MATERIALIZATION"] += 1
    return summary


def _bounded_evidence_inventory(
    conn: sqlite3.Connection,
    *,
    table: str,
    timestamp_column: str,
    required_columns: set[str],
    as_of_utc: str,
    where_sql: str = "",
    params: Sequence[Any] = (),
) -> dict[str, Any]:
    """Count a raw evidence surface without treating it as an outcome link."""

    if not _table_exists(conn, table):
        return {
            "state": "TABLE_MISSING",
            "record_count": 0,
            "bounded": True,
            "blocker": f"{table}_table_missing",
        }
    if required_columns - _columns(conn, table):
        return {
            "state": "SCHEMA_MISSING",
            "record_count": 0,
            "bounded": True,
            "blocker": f"{table}_schema_missing",
        }
    query = (
        "SELECT 1 FROM " + table + " WHERE julianday(" + timestamp_column
        + ")<=julianday(?) " + where_sql + " LIMIT ?"
    )
    count = 0
    for _row in conn.execute(
        query, (as_of_utc, *params, MAX_EVIDENCE_REFERENCE_ROWS + 1)
    ):
        count += 1
        if count > MAX_EVIDENCE_REFERENCE_ROWS:
            raise ContractError("station_policy_scorecard_evidence_limit_exceeded")
    return {
        "state": "PRESENT_UNBOUND" if count else "EMPTY",
        "record_count": count,
        "bounded": True,
        "blocker": None if count else f"{table}_records_absent",
    }


def _sqlite_truthy(value: Any) -> bool:
    """Interpret SQLite boolean affinity without treating malformed data as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value or "").strip().lower() in {"1", "true"}


def _execution_receipt_inventory(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
) -> dict[str, Any]:
    """Classify receipt provenance without inferring a realised policy fill.

    A simulated no-live receipt, an internal authoritative flag, or a raw
    positive matched-size record is not a direct utility binding.  Keeping
    these counts separate prevents two dangerous shortcuts: treating a
    simulated FOK failure as an observed zero fill, and treating a future
    non-simulated receipt as if it were already joined to the frozen policy
    fact, its official WS cost and the settlement label.
    """

    table = "basket_target_execution_receipts"
    required = {
        "receipt_id",
        "order_intent_id",
        "cycle_id",
        "receipt_type",
        "authoritative",
        "execution_mode",
        "simulated",
        "observed_at_utc",
        "matched_size",
        "matched_notional_usdc",
    }
    if not _table_exists(conn, table):
        return {
            "state": "TABLE_MISSING",
            "record_count": 0,
            "bounded": True,
            "blocker": f"{table}_table_missing",
            "simulated_receipt_count": 0,
            "non_simulated_receipt_count": 0,
            "authoritative_receipt_count": 0,
            "authoritative_fill_candidate_count": 0,
            "direct_fill_binding": "UNBOUND",
            "receipt_to_policy_fact_binding": "MISSING",
        }
    if required - _columns(conn, table):
        return {
            "state": "SCHEMA_MISSING",
            "record_count": 0,
            "bounded": True,
            "blocker": f"{table}_schema_missing",
            "simulated_receipt_count": 0,
            "non_simulated_receipt_count": 0,
            "authoritative_receipt_count": 0,
            "authoritative_fill_candidate_count": 0,
            "direct_fill_binding": "UNBOUND",
            "receipt_to_policy_fact_binding": "MISSING",
        }
    count = 0
    simulated_count = 0
    non_simulated_count = 0
    authoritative_count = 0
    authoritative_fill_candidate_count = 0
    fill_types = {
        "FOK_FULL_FILL",
        "MAKER_FULL_FILL",
        "MAKER_PARTIAL_FILL",
    }
    query = (
        "SELECT receipt_type,authoritative,simulated,matched_size "
        "FROM basket_target_execution_receipts "
        "WHERE julianday(observed_at_utc)<=julianday(?) LIMIT ?"
    )
    for receipt_type, authoritative, simulated, matched_size in conn.execute(
        query, (as_of_utc, MAX_EVIDENCE_REFERENCE_ROWS + 1)
    ):
        count += 1
        if count > MAX_EVIDENCE_REFERENCE_ROWS:
            raise ContractError("station_policy_scorecard_evidence_limit_exceeded")
        is_simulated = _sqlite_truthy(simulated)
        is_authoritative = _sqlite_truthy(authoritative)
        simulated_count += int(is_simulated)
        non_simulated_count += int(not is_simulated)
        authoritative_count += int(is_authoritative)
        try:
            positive_size = float(matched_size) > 0.0
        except (TypeError, ValueError):
            positive_size = False
        authoritative_fill_candidate_count += int(
            not is_simulated
            and is_authoritative
            and str(receipt_type or "").upper() in fill_types
            and positive_size
        )
    if not count:
        state = "EMPTY"
        blocker = f"{table}_records_absent"
    elif non_simulated_count == 0:
        state = "PRESENT_UNBOUND_SIMULATED_ONLY"
        blocker = None
    elif authoritative_fill_candidate_count:
        state = "PRESENT_UNBOUND_REAL_FILL_CANDIDATES"
        blocker = None
    else:
        state = "PRESENT_UNBOUND_NON_SIMULATED_NO_CONFIRMED_FILL"
        blocker = None
    return {
        "state": state,
        "record_count": count,
        "bounded": True,
        "blocker": blocker,
        "simulated_receipt_count": simulated_count,
        "non_simulated_receipt_count": non_simulated_count,
        "authoritative_receipt_count": authoritative_count,
        "authoritative_fill_candidate_count": authoritative_fill_candidate_count,
        "direct_fill_binding": "UNBOUND",
        "receipt_to_policy_fact_binding": "MISSING",
    }


def _runtime_evidence_inventory(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    frozen_label_fact_count: int,
) -> tuple[dict[str, Any], set[str]]:
    """Audit CLOB/fill/settlement surfaces without inventing a utility link."""

    clob = _bounded_evidence_inventory(
        conn,
        table="basket_official_ws_snapshot",
        timestamp_column="observed_at",
        required_columns={"snapshot_id", "station_id", "event_date", "observed_at", "quote_source"},
        as_of_utc=as_of_utc,
        where_sql="AND quote_source=?",
        params=("OFFICIAL_CLOB_WS",),
    )
    order_events = _bounded_evidence_inventory(
        conn,
        table="basket_target_order_events",
        timestamp_column="observed_at",
        required_columns={"event_id", "order_intent_id", "event_type", "observed_at"},
        as_of_utc=as_of_utc,
    )
    candidate_execution_receipts = _execution_receipt_inventory(
        conn, as_of_utc=as_of_utc
    )
    candidate_order_intents = _bounded_evidence_inventory(
        conn,
        table="basket_target_order_intents",
        timestamp_column="created_at",
        required_columns={
            "order_intent_id",
            "decision_id",
            "cycle_id",
            "bucket",
            "side",
            "order_action",
            "state",
            "live_submit_authorized",
            "created_at",
        },
        as_of_utc=as_of_utc,
    )
    candidate_execution_cycles = _bounded_evidence_inventory(
        conn,
        table="basket_target_execution_cycles",
        timestamp_column="created_at",
        required_columns={
            "cycle_id",
            "station_id",
            "event_date",
            "settlement_timezone",
            "strategy_lane",
            "state",
            "created_at",
        },
        as_of_utc=as_of_utc,
    )
    settlement = {
        "state": (
            "PRESENT_UNBOUND" if frozen_label_fact_count else "EMPTY"
        ),
        "frozen_labeled_fact_count": frozen_label_fact_count,
        "blocker": (
            None if frozen_label_fact_count
            else "frozen_official_settlement_labels_absent"
        ),
    }
    blockers = {
        "station_policy_utility_evidence_binding_unbound",
        "confirmed_yes_fill_receipt_direct_binding_unbound",
    }
    for item in (clob, order_events, settlement):
        blocker = item.get("blocker")
        if blocker:
            blockers.add(str(blocker))
    if candidate_execution_receipts["record_count"] == 0:
        blockers.add("confirmed_yes_fill_receipt_records_absent")
    elif candidate_execution_receipts["non_simulated_receipt_count"] == 0:
        blockers.add("confirmed_yes_fill_real_receipt_records_absent")
    if candidate_execution_receipts["authoritative_fill_candidate_count"] == 0:
        blockers.add("confirmed_yes_fill_candidate_records_absent")
    return {
        "utility_contract_pointer": build_station_policy_utility_contract_pointer(),
        "official_clob_ws_snapshots": clob,
        # An order-event record is not automatically a confirmed YES fill;
        # the direct receipt binding is intentionally a separate blocker.
        "order_events": order_events,
        "candidate_execution_receipts": {
            **candidate_execution_receipts,
            "candidate_only_not_direct_fill_evidence": True,
        },
        "candidate_order_intents": {
            **candidate_order_intents,
            "candidate_only_not_fill_evidence": True,
        },
        "candidate_execution_cycles": {
            **candidate_execution_cycles,
            "candidate_only_not_fill_evidence": True,
        },
        "frozen_official_settlement_labels": settlement,
        "confirmed_yes_fill_receipt": {
            "state": "BINDING_UNBOUND",
            "inference_from_zero_events_forbidden": True,
            "source_execution_receipt_count": candidate_execution_receipts[
                "record_count"
            ],
            "non_simulated_receipt_count": candidate_execution_receipts[
                "non_simulated_receipt_count"
            ],
            "authoritative_fill_candidate_count": (
                candidate_execution_receipts[
                    "authoritative_fill_candidate_count"
                ]
            ),
            "direct_fact_to_receipt_link": "MISSING",
        },
    }, blockers


HISTORICAL_UTILITY_BINDINGS_FEATURE_KEY = (
    "station_policy_historical_utility_bindings_v1"
)


def _historical_utility_binding_inventory(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    stages: Sequence[str],
    labels: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Audit closed-day candidate/baseline utility bindings by exact stage.

    The scan intentionally consumes only append-only policy facts, official
    CLOB WS snapshots, direct receipt references embedded in an immutable
    binding, and accepted AWC settlement labels.  It does not reconstruct a
    baseline from a target plan, infer a zero fill from an absent receipt, or
    calculate a proxy PnL from weather probabilities or quotes.

    A future/open day is counted under ``observational_candidate_not_oos``
    and is deliberately absent from all strict-OOS denominators and blockers.
    """

    family_template = lambda: {
        "declared_binding_count": 0,
        "validated_raw_binding_count": 0,
        "score_row_count": 0,
        "blocker_counts": Counter(),
    }
    result: dict[str, Any] = {
        "schema": "station_policy_historical_utility_binding_inventory_v1",
        "evidence_role": HISTORICAL_STRICT_OOS,
        "observational_evidence_role": OBSERVATIONAL_CANDIDATE_NOT_OOS,
        "metric_contract_state": METRIC_CONTRACT_STATE,
        "score_emission": "FORBIDDEN_UNDER_UTILITY_CONTRACT_V2",
        "stages": {
            stage: {
                "closed_historical_fact_count": 0,
                "closed_frozen_label_count": 0,
                "closed_official_awc_label_count": 0,
                "closed_fresh_official_ws_count": 0,
                "observational_candidate_not_oos_count": 0,
                "closed_forward_observational_candidate_not_oos_count": 0,
                "closed_unverified_provenance_count": 0,
                "families": {family: family_template() for family in POLICY_FAMILIES},
                "blocker_counts": Counter(),
            }
            for stage in stages
        },
        "read_only": True,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "promotion_permitted": False,
    }
    fact_table = "basket_policy_training_fact"
    snapshot_table = "basket_official_ws_snapshot"
    required_fact = {
        "fact_id", "station_id", "event_date", "settlement_timezone",
        "decision_at_utc", "stage", "feature_json", "content_sha256",
        "plan_sha256", "inventory_sha256", "ws_snapshot_id",
        "authority_scope",
    }
    required_snapshot = {
        "snapshot_id", "content_sha256", "quote_source", "state",
        "quote_observed_at", "quote_expires_at",
    }
    if not _table_exists(conn, fact_table):
        result["state"] = "FACT_TABLE_MISSING"
        result["blockers"] = ["training_fact_table_missing"]
        return result
    if required_fact - _columns(conn, fact_table):
        result["state"] = "FACT_SCHEMA_MISSING"
        result["blockers"] = ["training_fact_schema_missing"]
        return result
    snapshot_usable = (
        _table_exists(conn, snapshot_table)
        and required_snapshot <= _columns(conn, snapshot_table)
    )
    if not snapshot_usable:
        for stage in stages:
            result["stages"][stage]["blocker_counts"][
                "official_clob_ws_snapshot_schema_or_table_missing"
            ] += 1
    placeholders = ",".join("?" for _ in stages)
    join = (
        f" LEFT JOIN {snapshot_table} s ON s.snapshot_id=f.ws_snapshot_id"
        if snapshot_usable
        else ""
    )
    snapshot_projection = (
        ",s.content_sha256 AS ws_snapshot_content_sha256,"
        "s.quote_source AS ws_quote_source,s.state AS ws_state,"
        "s.quote_observed_at AS ws_quote_observed_at,"
        "s.quote_expires_at AS ws_quote_expires_at"
        if snapshot_usable
        else ""
    )
    query = (
        "SELECT f.fact_id,f.station_id,f.event_date,f.settlement_timezone,"
        "f.decision_at_utc,f.stage,f.content_sha256,f.plan_sha256,"
        "f.inventory_sha256,f.ws_snapshot_id,f.authority_scope,"
        "length(CAST(f.feature_json AS BLOB)) AS feature_bytes,"
        "CASE WHEN length(CAST(f.feature_json AS BLOB))<=? "
        "THEN f.feature_json ELSE NULL END AS feature_json"
        + snapshot_projection
        + f" FROM {fact_table} f"
        + join
        + " WHERE f.stage IN (" + placeholders + ") "
        "AND julianday(f.decision_at_utc)<=julianday(?) "
        "ORDER BY f.stage,f.station_id,f.event_date,f.decision_at_utc,f.fact_id"
    )
    retained_bytes = 0
    read_count = 0
    for row in conn.execute(query, (MAX_JSON_BYTES, *stages, as_of_utc)):
        read_count += 1
        if read_count > MAX_FACT_ROWS:
            raise ContractError("station_policy_scorecard_fact_limit_exceeded")
        (
            fact_id_raw, station_id_raw, event_date_raw, timezone_raw,
            decision_at_raw, stage_raw, fact_sha, plan_sha, inventory_sha,
            ws_snapshot_id, fact_authority_scope, feature_bytes, raw_feature,
            *snapshot_row,
        ) = row
        fact_id = str(fact_id_raw or "").strip()
        station_id = str(station_id_raw or "").strip().upper()
        event_date = str(event_date_raw or "").strip()
        settlement_timezone = str(timezone_raw or "").strip()
        stage = str(stage_raw or "").strip()
        if stage not in result["stages"] or not fact_id.startswith("d2fact_"):
            continue
        stage_result = result["stages"][stage]
        try:
            closed_at = _utc(
                settlement_day_window(event_date, settlement_timezone)[
                    "target_day_end_utc"
                ],
                "historical_utility_settlement_closed_at",
            )
        except ContractError:
            stage_result["blocker_counts"]["fact_settlement_scope_invalid"] += 1
            continue
        if closed_at > _utc(as_of_utc, "historical_utility_asof"):
            stage_result["observational_candidate_not_oos_count"] += 1
            continue
        # A forward/no-live fact remains an observation even after its local
        # day closes.  A later AWC label verifies eventual truth; it cannot
        # retroactively make the forward decision a historical strict-as-of
        # candidate/baseline experiment.
        authority_scope = str(fact_authority_scope or "").strip().upper()
        if authority_scope == "PRODUCTION_NO_LIVE_FORWARD":
            stage_result["observational_candidate_not_oos_count"] += 1
            stage_result[
                "closed_forward_observational_candidate_not_oos_count"
            ] += 1
            continue
        if authority_scope not in {"HISTORICAL_STRICT_OOS", "HISTORICAL_OOS"}:
            stage_result["closed_unverified_provenance_count"] += 1
            stage_result["blocker_counts"][
                "historical_strict_oos_provenance_missing"
            ] += 1
            continue
        stage_result["closed_historical_fact_count"] += 1
        label = labels.get(fact_id)
        if label is None:
            stage_result["blocker_counts"]["fact_without_frozen_label"] += 1
            continue
        stage_result["closed_frozen_label_count"] += 1
        label_matches_fact = (
            str(label.get("truth_station_id") or "").upper() == station_id
            and str(label.get("truth_local_date") or "") == event_date
        )
        if label.get("official_awc_truth") is not True or not label_matches_fact:
            stage_result["blocker_counts"]["official_awc_closed_truth_binding_missing"] += 1
            continue
        stage_result["closed_official_awc_label_count"] += 1
        ws_values = {
            "ws_snapshot_content_sha256": "",
            "ws_quote_source": "",
            "ws_state": "",
            "ws_quote_observed_at": "",
            "ws_quote_expires_at": "",
        }
        if snapshot_usable:
            ws_values.update(dict(zip(ws_values, snapshot_row)))
        try:
            fresh_ws = (
                str(ws_values["ws_quote_source"] or "") == "OFFICIAL_CLOB_WS"
                and str(ws_values["ws_state"] or "") == "COMMITTED"
                and _utc(str(ws_values["ws_quote_observed_at"]), "ws_quote_observed_at")
                <= _utc(str(decision_at_raw), "fact_decision_at")
                <= _utc(str(ws_values["ws_quote_expires_at"]), "ws_quote_expires_at")
            )
        except (ContractError, TypeError):
            fresh_ws = False
        if not fresh_ws:
            stage_result["blocker_counts"]["official_clob_ws_not_fresh_at_decision"] += 1
            continue
        stage_result["closed_fresh_official_ws_count"] += 1
        feature, feature_error = _bounded_mapping(raw_feature, feature_bytes)
        if feature is None:
            stage_result["blocker_counts"][str(feature_error)] += 1
            continue
        retained_bytes += int(feature_bytes or 0)
        if retained_bytes > MAX_TOTAL_JSON_BYTES:
            raise ContractError("station_policy_scorecard_json_limit_exceeded")
        bindings = feature.get(HISTORICAL_UTILITY_BINDINGS_FEATURE_KEY)
        if not isinstance(bindings, Mapping):
            stage_result["blocker_counts"]["explicit_candidate_baseline_binding_missing"] += 1
            continue
        fact = {
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": settlement_timezone,
            "stage": stage,
            "decision_at_utc": str(decision_at_raw),
            "content_sha256": str(fact_sha or ""),
            "plan_sha256": str(plan_sha or ""),
            "inventory_sha256": str(inventory_sha or ""),
            "ws_snapshot_id": str(ws_snapshot_id or ""),
            "ws_snapshot_content_sha256": str(
                ws_values["ws_snapshot_content_sha256"] or ""
            ),
            "authority_scope": authority_scope,
        }
        for family in POLICY_FAMILIES:
            binding = bindings.get(family)
            family_result = stage_result["families"][family]
            if not isinstance(binding, Mapping):
                family_result["blocker_counts"]["family_candidate_baseline_binding_missing"] += 1
                continue
            family_result["declared_binding_count"] += 1
            validation = validate_historical_station_policy_utility_binding(
                binding,
                family=family,
                fact=fact,
                label=label,
                evaluation_as_of_utc=as_of_utc,
            )
            if validation["binding_valid"] is True:
                family_result["validated_raw_binding_count"] += 1
            else:
                family_result["blocker_counts"].update(validation["blockers"])

    for stage_result in result["stages"].values():
        for family_result in stage_result["families"].values():
            family_result["blocker_counts"] = dict(
                sorted(family_result["blocker_counts"].items())
            )
        stage_result["blocker_counts"] = dict(
            sorted(stage_result["blocker_counts"].items())
        )
    total_valid = sum(
        family["validated_raw_binding_count"]
        for stage in result["stages"].values()
        for family in stage["families"].values()
    )
    result["state"] = (
        "HISTORICAL_RAW_BINDINGS_COMPLETE_NOT_SCORED"
        if total_valid
        else "HISTORICAL_UTILITY_BINDINGS_UNAVAILABLE"
    )
    result["score_rows_emitted"] = 0
    result["content_sha256"] = content_sha256({
        key: value for key, value in result.items() if key != "content_sha256"
    })
    return result


def _load_bundle(path: Path, stage: str) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ContractError("station_policy_scorecard_bundle_mapping_required")
    assert_candidate_bundle_boundary(raw)
    if raw.get("scope", {}).get("stage") != stage:
        raise ContractError("station_policy_scorecard_bundle_stage_mismatch")
    if int(raw.get("station_count", 0)) != 45 or int(raw.get("artifact_count", 0)) != 135:
        raise ContractError("station_policy_scorecard_bundle_exact_45x3_required")
    return dict(raw)


def _utility_evidence_contract(family: str) -> dict[str, Any]:
    """Return a JSON-safe declaration without a scoring implementation."""

    utility_contract = build_station_policy_utility_contract()
    return {
        **UTILITY_EVIDENCE_CONTRACTS[family],
        "contract_family": utility_contract["families"][family],
        "utility_contract_pointer": build_station_policy_utility_contract_pointer(
            utility_contract
        ),
        "binding_state": "UNBOUND",
        "metric_state": METRIC_CONTRACT_STATE,
        "weather_or_execution_proxy_scoring_forbidden": True,
    }


def _family_gap_report(
    *,
    family: str,
    source_blockers: set[str],
    utility_evidence_blockers: set[str],
) -> dict[str, Any]:
    blockers = set(source_blockers)
    blockers.update(utility_evidence_blockers)
    return {
        "state": "METRIC_CONTRACT_DEFINED_UNBOUND",
        "metric_contract_status": METRIC_CONTRACT_STATE,
        "row_count": 0,
        "report_count": 45,
        "station_count_with_scores": 0,
        "strict_oos": {"complete": False, "sample_count": 0},
        "leave_one_station_out": {"complete": False, "sample_count": 0},
        "cross_season": {"complete": False, "sample_count": 0},
        "windows_3_7_15_30": {"complete": False, "sample_count": 0},
        "unavailable_reason": UNAVAILABLE_REASONS[family],
        "utility_evidence_contract": _utility_evidence_contract(family),
        "blockers": sorted(blockers),
        "promotion_permitted": False,
    }


def _station_blocker_report(
    *,
    station_id: str,
    stage: str,
    source: Mapping[str, Any],
    source_blockers: set[str],
    utility_evidence_blockers: set[str],
) -> dict[str, Any]:
    """Materialize the no-score gap for one exact station/stage pair.

    A row deliberately reports only immutable-reference presence.  It never
    infers fill quality or realised utility from a quote, a posterior, a
    weather outcome, or the absence of an order event.
    """

    fact_count = int(source["station_fact_counts"][stage].get(station_id, 0))
    labeled_count = int(
        source["station_labeled_fact_counts"][stage].get(station_id, 0)
    )
    evidence = source["station_evidence_reference_counts"][stage].get(
        station_id, Counter()
    )
    blockers = set(source_blockers)
    blockers.update(
        source["station_blockers"][stage].get(station_id, Counter())
    )
    if not fact_count:
        blockers.add("exact_stage_training_fact_coverage_incomplete")
    if not labeled_count:
        blockers.add("exact_stage_frozen_label_coverage_incomplete")
    if fact_count > labeled_count:
        blockers.add("fact_without_frozen_label")
    references = {
        reference: int(evidence.get(reference, 0))
        for reference in EVIDENCE_REFERENCE_COLUMNS
    }
    references["frozen_settlement_label"] = labeled_count
    reference_state = {
        reference: (
            "REFERENCE_PRESENT" if count else "REFERENCE_MISSING"
        )
        for reference, count in references.items()
    }
    # There is no direct fact-to-confirmed-fill mapping in this contract yet.
    # A zero event count cannot be interpreted as a zero-fill outcome, so it
    # remains explicitly unbound even in a no-live history.
    reference_state["confirmed_yes_fill_receipt"] = "BINDING_UNBOUND"
    families = {}
    for family in POLICY_FAMILIES:
        family_blockers = set(blockers)
        family_blockers.update(utility_evidence_blockers)
        families[family] = {
            "state": "METRIC_CONTRACT_DEFINED_UNBOUND",
            "utility_evidence_contract": _utility_evidence_contract(family),
            "blockers": sorted(family_blockers),
            "promotion_permitted": False,
        }
    return {
        "station_id": station_id,
        "stage": stage,
        "state": "BLOCKED",
        "frozen_fact_count": fact_count,
        "frozen_labeled_fact_count": labeled_count,
        "decision_evidence_reference_counts": references,
        "decision_evidence_reference_state": reference_state,
        "families": families,
        "blockers": sorted(blockers),
        "promotion_permitted": False,
    }


def build_exact_stage_scorecard(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    station_ids: Sequence[str],
    bundle_paths: Mapping[str, Path],
) -> dict[str, Any]:
    """Build a no-live, no-score report from immutable facts and labels only."""

    as_of = _utc_text(as_of_utc, "as_of")
    expected = tuple(sorted({str(station).strip().upper() for station in station_ids if str(station).strip()}))
    if len(expected) != 45:
        raise ContractError("station_policy_scorecard_exact_45_station_scope_required")
    if set(bundle_paths) != set(STAGES):
        raise ContractError("station_policy_scorecard_all_exact_stages_required")
    bundles = {stage: _load_bundle(Path(bundle_paths[stage]), stage) for stage in STAGES}
    labels, label_counts, settlement_reference_counts = _label_rows(
        conn, as_of_utc=as_of
    )
    source = _facts(conn, as_of_utc=as_of, stages=STAGES, labels=labels)
    historical_utility_bindings = _historical_utility_binding_inventory(
        conn,
        as_of_utc=as_of,
        stages=STAGES,
        labels=labels,
    )
    historical_binding_stages = historical_utility_bindings.get("stages", {})
    historical_inventory_usable = historical_utility_bindings.get("state") not in {
        "FACT_TABLE_MISSING",
        "FACT_SCHEMA_MISSING",
    }
    inventory_observational_by_stage = {
        stage: int(
            (historical_binding_stages.get(stage) or {}).get(
                "observational_candidate_not_oos_count", 0
            )
        )
        for stage in STAGES
    }
    observational_by_stage = (
        inventory_observational_by_stage
        if historical_inventory_usable
        else dict(source["observational_candidate_not_oos_by_stage"])
    )
    runtime_evidence, utility_evidence_blockers = _runtime_evidence_inventory(
        conn,
        as_of_utc=as_of,
        frozen_label_fact_count=int(
            label_counts.get("official_awc_frozen_label_count", 0)
        ),
    )
    stage_results: dict[str, Any] = {}
    label_metric_keys = {
        "latest_frozen_label_count",
        "official_awc_frozen_label_count",
        "superseded_label_not_used",
    }
    all_blockers = {
        key for key, value in label_counts.items()
        if key not in label_metric_keys and int(value) > 0
    }
    all_blockers.update(str(key) for key in source["blockers"])
    all_blockers.update(utility_evidence_blockers)
    for stage in STAGES:
        stage_static_blockers = {
            code for code in source["blockers"]
            if code in {"training_fact_table_missing", "training_fact_schema_missing"}
            or code.endswith("_reference_column_missing")
        }
        stage_data_blockers = {
            code
            for counters in source["station_blockers"][stage].values()
            for code in counters
        }
        observed = set(source["stage_station_ids"][stage])
        labeled = set(source["stage_labeled_station_ids"][stage])
        missing_observed = sorted(set(expected) - observed)
        missing_labeled = sorted(set(expected) - labeled)
        stage_blockers = set(stage_static_blockers) | stage_data_blockers
        if missing_observed:
            stage_blockers.add("exact_stage_training_fact_coverage_incomplete")
        if missing_labeled:
            stage_blockers.add("exact_stage_frozen_label_coverage_incomplete")
        if stage != "P0-M":
            stage_blockers.add("source_formula_not_exact_stage_trained")
        family_materialization = {
            family: {
                **source["family_outcome_materialization"][stage][family],
                "unavailable_reasons": dict(sorted(source["family_outcome_materialization"][stage][family]["unavailable_reasons"].items())),
                "accepted_feature_paths": [],
                "post_label_metric": None,
                "metric_contract_status": METRIC_CONTRACT_STATE,
                "weather_or_execution_proxy_scoring_forbidden": True,
            }
            for family in POLICY_FAMILIES
        }
        families = {
            family: _family_gap_report(
                family=family,
                source_blockers=stage_blockers,
                utility_evidence_blockers=utility_evidence_blockers,
            )
            for family in POLICY_FAMILIES
        }
        for family in families.values():
            all_blockers.update(family["blockers"])
        station_reports = [
            _station_blocker_report(
                station_id=station_id,
                stage=stage,
                source=source,
                source_blockers=(
                    stage_static_blockers
                    | ({"source_formula_not_exact_stage_trained"}
                       if stage != "P0-M" else set())
                ),
                utility_evidence_blockers=utility_evidence_blockers,
            )
            for station_id in expected
        ]
        station_blocker_type_counts: Counter[str] = Counter()
        family_blocker_type_counts: dict[str, Counter[str]] = {
            family: Counter() for family in POLICY_FAMILIES
        }
        for report in station_reports:
            station_blocker_type_counts.update(report["blockers"])
            for family, family_report in report["families"].items():
                family_blocker_type_counts[family].update(
                    family_report["blockers"]
                )
                all_blockers.update(family_report["blockers"])
        stage_results[stage] = {
            "candidate_bundle": {
                "path": str(bundle_paths[stage]),
                "bundle_sha256": bundles[stage]["bundle_sha256"],
                "station_count": bundles[stage]["station_count"],
                "artifact_count": bundles[stage]["artifact_count"],
                "activation_count": bundles[stage]["activation_count"],
                "promotion_permitted": False,
            },
            "fact_label_coverage": {
                "observed_station_count": len(observed),
                "frozen_labeled_station_count": len(labeled),
                "official_awc_frozen_label_count": int(
                    historical_utility_bindings["stages"][stage][
                        "closed_official_awc_label_count"
                    ]
                ),
                "missing_observed_station_ids": missing_observed,
                "missing_frozen_label_station_ids": missing_labeled,
                "season_bins": sorted(source["stage_seasons"][stage]),
                "lead_buckets": sorted(source["stage_leads"][stage]),
            },
            "historical_utility_binding_inventory": (
                historical_utility_bindings["stages"][stage]
            ),
            "family_outcome_materialization": family_materialization,
            "forward_outcome_materialization_state_counts": dict(sorted(source["stage_outcome_materialization_states"][stage].items())),
            "decision_evidence_reference_counts": {
                reference: int(
                    source["evidence_reference_counts"][stage].get(reference, 0)
                )
                for reference in EVIDENCE_REFERENCE_COLUMNS
            },
            "frozen_settlement_reference_counts": {
                reference: int(settlement_reference_counts.get(reference, 0))
                for reference in SETTLEMENT_REFERENCE_COLUMNS
            },
            "station_reports": station_reports,
            "station_blocker_type_counts": dict(
                sorted(station_blocker_type_counts.items())
            ),
            "family_blocker_type_counts": {
                family: dict(sorted(counts.items()))
                for family, counts in family_blocker_type_counts.items()
            },
            "families": families,
            "promotion_permitted": False,
        }
    identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of,
        "station_ids": list(expected),
        "station_count": len(expected),
        "stages": stage_results,
        "input_counts": {
            "facts_read": source["facts_read"],
            "facts_with_frozen_label": source["facts_with_frozen_label"],
            "facts_with_official_awc_frozen_label": source[
                "facts_with_official_awc_frozen_label"
            ],
            "open_or_future_observational_candidate_not_oos_count": source[
                "observational_candidate_not_oos_count"
            ],
            "closed_forward_observational_candidate_not_oos_count": sum(
                int(
                    (historical_binding_stages.get(stage) or {}).get(
                        "closed_forward_observational_candidate_not_oos_count",
                        0,
                    )
                )
                for stage in STAGES
            ),
            "observational_candidate_not_oos_count": sum(
                observational_by_stage.values()
            ),
            "observational_candidate_not_oos_by_stage": observational_by_stage,
            "latest_frozen_label_count": int(label_counts.get("latest_frozen_label_count", 0)),
            "official_awc_frozen_label_count": int(
                label_counts.get("official_awc_frozen_label_count", 0)
            ),
            "frozen_settlement_reference_counts": {
                reference: int(settlement_reference_counts.get(reference, 0))
                for reference in SETTLEMENT_REFERENCE_COLUMNS
            },
            "score_rows": source["score_rows"],
            "family_outcome_materialization": {
                stage: {
                    family: {
                        **values,
                        "unavailable_reasons": dict(sorted(values["unavailable_reasons"].items())),
                    }
                    for family, values in families.items()
                }
                for stage, families in source["family_outcome_materialization"].items()
            },
            "stage_outcome_materialization_states": {
                stage: dict(sorted(values.items()))
                for stage, values in source["stage_outcome_materialization_states"].items()
            },
        },
        # This pointer proves that a versioned metric/evidence declaration
        # exists.  It never says any post-label fact is bound, scored or
        # promotable.
        "utility_contract_pointer": build_station_policy_utility_contract_pointer(),
        "utility_evidence_inventory": runtime_evidence,
        "historical_utility_binding_inventory": historical_utility_bindings,
        "checks": {
            "exact_stages_p0_p0m_p1": True,
            "exact_45_station_scope": True,
            "policy_weather_probability_not_recomputed": True,
            "policy_utility_metric_contracts_defined": True,
            "policy_utility_metric_binding_complete": all(
                int(
                    historical_utility_bindings["stages"][stage]["families"][family][
                        "validated_raw_binding_count"
                    ]
                ) > 0
                for stage in STAGES
                for family in POLICY_FAMILIES
            ),
            "policy_utility_score_rows_emitted": False,
            # The declaration exists; this flag does not mean that any
            # post-label utility fact is bound or promotable.
            "policy_utility_contracts_defined": True,
            "policy_utility_contract_schema_present": True,
            "policy_utility_contract_binding_state": (
                "VALIDATED_RAW_EVIDENCE_NOT_SCORED"
                if historical_utility_bindings["state"]
                == "HISTORICAL_RAW_BINDINGS_COMPLETE_NOT_SCORED"
                else "UNBOUND"
            ),
            "historical_utility_oos_closed_awc_only": True,
            "observational_candidate_not_oos_blocks_production_no_live": False,
            "facts_and_labels_read_only": True,
            "promotion_permitted": False,
        },
        "blockers": sorted(all_blockers),
        "production_no_live": True,
        "live_order_authorized": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = ["POLICY_FAMILIES", "SCHEMA", "STAGES", "build_exact_stage_scorecard"]
