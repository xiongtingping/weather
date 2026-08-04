"""Read-only target-owned forward-observation projection for ΔBasketScore v2.

Forward facts intentionally keep their feature vector compact.  The exact
probability vector, official CLOB WS book and no-live settlement attribution
remain in the append-only rows to which each fact is content-address bound.
This module projects those immutable inputs into cash-phase OOS observations
without rewriting a fact, inferring a price, or granting promotion authority.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .cash_phase_oos import PHASES
from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .delta_v2_settlement_label import LABEL_SCHEMA
from .delta_v2_training import FACT_COLUMNS, FACT_SCHEMA


SCHEMA = "delta_basket_score_v2_target_owned_forward_observation_candidate_v1"
MAX_ROWS = 4_096
MAX_FEATURE_JSON_BYTES = 262_144
MAX_SNAPSHOT_JSON_BYTES = 4 * 1024 * 1024
MAX_LABEL_JSON_BYTES = 65_536
# Keep the aggregate candidate packet bounded while allowing the full
# canonical 45-station lineage to be retained.  Per-field limits and the
# process/database resource guards remain the controlling limits.
MAX_TOTAL_JSON_BYTES = 128 * 1024 * 1024

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


def _utc(value: Any, name: str) -> tuple[str, datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"delta_v2_forward_observation_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"delta_v2_forward_observation_{name}_timezone_required")
    normalized = parsed.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z"), normalized


def _mapping(
    value: Any,
    *,
    name: str,
    byte_limit: int,
) -> Mapping[str, Any]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > byte_limit:
        raise ContractError(f"delta_v2_forward_observation_{name}_invalid")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"delta_v2_forward_observation_{name}_invalid") from exc
    if not isinstance(parsed, Mapping):
        raise ContractError(f"delta_v2_forward_observation_{name}_mapping_required")
    return parsed


def _json_value(value: Any, *, name: str, byte_limit: int) -> Any:
    if not isinstance(value, str) or len(value.encode("utf-8")) > byte_limit:
        raise ContractError(f"delta_v2_forward_observation_{name}_invalid")
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"delta_v2_forward_observation_{name}_invalid") from exc


def _finite_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0.0 < number < 1.0 else None


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0.0 else None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fact_hash_valid(row: Mapping[str, Any]) -> bool:
    stored = str(row.get("content_sha256") or "").lower()
    if len(stored) != 64:
        return False
    identity = {field: row.get(field) for field in FACT_COLUMNS if field not in {"fact_id", "content_sha256"}}
    expected = content_sha256({"schema": FACT_SCHEMA, **identity})
    return (
        stored == expected
        and str(row.get("fact_id") or "") == f"d2fact_{expected[:40]}"
    )


def _label_hash_valid(row: Mapping[str, Any]) -> bool:
    stored = str(row.get("content_sha256") or "").lower()
    if len(stored) != 64:
        return False
    identity = {field: row.get(field) for field in _LABEL_HASH_FIELDS}
    expected = content_sha256({"schema": LABEL_SCHEMA, **identity})
    return (
        stored == expected
        and str(row.get("label_id") or "") == f"d2label_{expected[:40]}"
    )


def _latest_labels(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
) -> tuple[dict[str, dict[str, Any]], Counter[str], int]:
    labels: dict[str, dict[str, Any]] = {}
    seen_fact_ids: set[str] = set()
    counters: Counter[str] = Counter()
    retained_bytes = 0
    cursor = conn.execute(
        "SELECT label_id,fact_id,truth_id,truth_revision_no,truth_sha256,"
        "official_tmax_c,winning_bucket,settlement_closed_at_utc,labeled_at_utc,"
        "length(CAST(outcome_json AS BLOB)) AS outcome_bytes,"
        "CASE WHEN length(CAST(outcome_json AS BLOB))<=? THEN outcome_json ELSE NULL END AS outcome_json,"
        "authority_scope,supersedes_label_id,content_sha256 "
        "FROM basket_policy_training_label "
        "WHERE fact_id LIKE 'd2fact_%' "
        "AND julianday(settlement_closed_at_utc)<=julianday(?) "
        "AND julianday(labeled_at_utc)<=julianday(?) "
        "ORDER BY fact_id,truth_revision_no DESC,labeled_at_utc DESC,label_id DESC",
        (MAX_LABEL_JSON_BYTES, as_of_utc, as_of_utc),
    )
    columns = tuple(item[0] for item in cursor.description or ())
    for values in cursor:
        row = dict(zip(columns, values))
        counters["label_rows_read"] += 1
        if counters["label_rows_read"] > MAX_ROWS:
            raise ContractError("delta_v2_forward_observation_label_row_limit_exceeded")
        fact_id = str(row.get("fact_id") or "")
        if fact_id in seen_fact_ids:
            continue
        seen_fact_ids.add(fact_id)
        try:
            _utc(row.get("settlement_closed_at_utc"), "settlement_closed_at")
            _utc(row.get("labeled_at_utc"), "labeled_at")
            if not _label_hash_valid(row):
                raise ContractError("label_hash_invalid")
            outcome = _mapping(
                row.get("outcome_json"),
                name="label_outcome_json",
                byte_limit=MAX_LABEL_JSON_BYTES,
            )
        except ContractError as exc:
            counters[str(exc)] += 1
            continue
        retained_bytes += int(row.get("outcome_bytes") or 0)
        if retained_bytes > MAX_TOTAL_JSON_BYTES:
            raise ContractError("delta_v2_forward_observation_label_bytes_exceeded")
        labels[fact_id] = {**row, "outcome": outcome}
    counters["latest_valid_label_count"] = len(labels)
    return labels, counters, retained_bytes


def _selected_action(feature: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, bool]:
    plan = feature.get("plan")
    if not isinstance(plan, Mapping):
        raise ContractError("delta_v2_forward_observation_plan_missing")
    selection = plan.get("delta_v2_action_selection")
    if not isinstance(selection, Mapping):
        raise ContractError("delta_v2_forward_observation_action_selection_missing")
    selected = selection.get("selected_action")
    if selected is None:
        return None, False
    if not isinstance(selected, Mapping):
        raise ContractError("delta_v2_forward_observation_selected_action_invalid")
    action = selected.get("action")
    if not isinstance(action, Mapping):
        raise ContractError("delta_v2_forward_observation_selected_action_invalid")
    return action, selected.get("authorized") is True


def _snapshot_bucket_names(snapshot: Mapping[str, Any]) -> set[str]:
    buckets = snapshot.get("buckets")
    if not isinstance(buckets, Sequence) or isinstance(buckets, (str, bytes, bytearray)):
        raise ContractError("delta_v2_forward_observation_snapshot_buckets_invalid")
    names = {
        str(item.get("bucket") or "").strip()
        for item in buckets
        if isinstance(item, Mapping) and str(item.get("bucket") or "").strip()
    }
    if not names:
        raise ContractError("delta_v2_forward_observation_snapshot_buckets_invalid")
    return names


def build_from_connection(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
) -> dict[str, Any]:
    """Project only source-verified, latest-labelled P1 Δv2 facts."""

    as_of, _ = _utc(as_of_utc, "as_of_utc")
    labels, label_counts, retained_bytes = _latest_labels(conn, as_of_utc=as_of)
    counters: Counter[str] = Counter(label_counts)
    observations: list[dict[str, Any]] = []
    source_blockers: set[str] = set()
    cursor = conn.execute(
        "SELECT * FROM basket_policy_training_fact "
        "WHERE fact_id LIKE 'd2fact_%' AND stage='P1' "
        "AND julianday(decision_at_utc)<=julianday(?) "
        "AND julianday(created_at_utc)<=julianday(?) "
        "ORDER BY station_id,event_date,decision_at_utc,fact_id",
        (as_of, as_of),
    )
    columns = tuple(item[0] for item in cursor.description or ())
    for values in cursor:
        fact = dict(zip(columns, values))
        counters["p1_fact_rows_read"] += 1
        if counters["p1_fact_rows_read"] > MAX_ROWS:
            raise ContractError("delta_v2_forward_observation_fact_row_limit_exceeded")
        fact_id = str(fact.get("fact_id") or "")
        label = labels.get(fact_id)
        if label is None:
            counters["fact_without_latest_valid_label"] += 1
            continue
        try:
            if not _fact_hash_valid(fact):
                raise ContractError("delta_v2_forward_observation_fact_hash_invalid")
            feature = _mapping(
                fact.get("feature_json"),
                name="feature_json",
                byte_limit=MAX_FEATURE_JSON_BYTES,
            )
            retained_bytes += len(str(fact["feature_json"]).encode("utf-8"))
            if retained_bytes > MAX_TOTAL_JSON_BYTES:
                raise ContractError("delta_v2_forward_observation_total_json_bytes_exceeded")
            cash = feature.get("cash_phase_decision")
            if not isinstance(cash, Mapping):
                raise ContractError("delta_v2_forward_observation_cash_phase_missing")
            phase = str(cash.get("next_phase") or "").strip().upper()
            policy_version = str(cash.get("policy_version") or "").strip()
            if phase not in PHASES or not policy_version:
                raise ContractError("delta_v2_forward_observation_cash_phase_invalid")
            decision_at, decision_dt = _utc(fact.get("decision_at_utc"), "decision_at")
            envelope_cursor = conn.execute(
                "SELECT * FROM basket_probability_envelope "
                "WHERE envelope_id=? AND state='COMMITTED' LIMIT 2",
                (fact.get("probability_envelope_id"),),
            )
            envelope_columns = tuple(item[0] for item in envelope_cursor.description or ())
            envelope_rows = [dict(zip(envelope_columns, row)) for row in envelope_cursor.fetchall()]
            if len(envelope_rows) != 1:
                raise ContractError("delta_v2_forward_observation_probability_envelope_unresolved")
            envelope_row = envelope_rows[0]
            if (
                str(envelope_row.get("station_id")) != str(fact.get("station_id"))
                or str(envelope_row.get("event_date")) != str(fact.get("event_date"))
                or str(envelope_row.get("settlement_timezone")) != str(fact.get("settlement_timezone"))
                or str(envelope_row.get("stage")) != "P1"
                or str(envelope_row.get("posterior_sha256")) != str(fact.get("posterior_sha256"))
            ):
                raise ContractError("delta_v2_forward_observation_probability_envelope_drift")
            envelope_contract = ProbabilityEnvelope.from_mapping({
                "stage": envelope_row.get("stage"),
                "checkpoint_id": envelope_row.get("checkpoint_id"),
                "station_id": envelope_row.get("station_id"),
                "event_date": envelope_row.get("event_date"),
                "settlement_timezone": envelope_row.get("settlement_timezone"),
                "decision_at_utc": envelope_row.get("decision_at_utc"),
                "fresh_until_utc": envelope_row.get("fresh_until_utc"),
                "bucket_ordinals": _json_value(envelope_row.get("bucket_ordinals_json"), name="bucket_ordinals_json", byte_limit=MAX_FEATURE_JSON_BYTES),
                "probabilities": _json_value(envelope_row.get("probabilities_json"), name="probabilities_json", byte_limit=MAX_FEATURE_JSON_BYTES),
                "posterior_version": envelope_row.get("posterior_version"),
                "posterior_sha256": envelope_row.get("posterior_sha256"),
                "bridge_sha256": envelope_row.get("bridge_sha256"),
                "lineage": _json_value(envelope_row.get("lineage_json"), name="lineage_json", byte_limit=MAX_FEATURE_JSON_BYTES),
            })
            if envelope_contract.content_sha256 != str(envelope_row.get("content_sha256") or "").lower():
                raise ContractError("delta_v2_forward_observation_probability_envelope_hash_invalid")
            if not envelope_contract.is_fresh(decision_at):
                raise ContractError("delta_v2_forward_observation_probability_envelope_not_fresh_at_decision")
            probabilities = envelope_contract.probabilities
            snapshot_cursor = conn.execute(
                "SELECT * FROM basket_official_ws_snapshot "
                "WHERE snapshot_id=? AND state='COMMITTED' LIMIT 2",
                (fact.get("ws_snapshot_id"),),
            )
            snapshot_columns = tuple(item[0] for item in snapshot_cursor.description or ())
            snapshot_rows = [dict(zip(snapshot_columns, row)) for row in snapshot_cursor.fetchall()]
            if len(snapshot_rows) != 1:
                raise ContractError("delta_v2_forward_observation_ws_snapshot_unresolved")
            snapshot_row = snapshot_rows[0]
            if (
                str(snapshot_row.get("station_id")) != str(fact.get("station_id"))
                or str(snapshot_row.get("event_date")) != str(fact.get("event_date"))
                or str(snapshot_row.get("settlement_timezone")) != str(fact.get("settlement_timezone"))
                or str(snapshot_row.get("quote_source")) != "OFFICIAL_CLOB_WS"
                or str(snapshot_row.get("content_sha256")) != str(fact.get("quote_sha256"))
            ):
                raise ContractError("delta_v2_forward_observation_ws_snapshot_drift")
            quote_observed_at, quote_observed_dt = _utc(snapshot_row.get("quote_observed_at"), "quote_observed_at")
            quote_expires_at, quote_expires_dt = _utc(snapshot_row.get("quote_expires_at"), "quote_expires_at")
            if not quote_observed_dt <= decision_dt < quote_expires_dt:
                raise ContractError("delta_v2_forward_observation_quote_not_fresh_at_decision")
            buckets = _json_value(snapshot_row.get("buckets_json"), name="snapshot_buckets_json", byte_limit=MAX_SNAPSHOT_JSON_BYTES)
            cash_phase_input = _json_value(snapshot_row.get("cash_phase_input_json"), name="snapshot_cash_phase_input_json", byte_limit=MAX_SNAPSHOT_JSON_BYTES)
            snapshot_identity = {
                key: value for key, value in snapshot_row.items()
                if key != "content_sha256"
            }
            snapshot_identity["buckets_json"] = buckets
            snapshot_identity["cash_phase_input_json"] = cash_phase_input
            expected_snapshot_sha = content_sha256({
                "schema": "basket_official_ws_snapshot_v1",
                **snapshot_identity,
            })
            if expected_snapshot_sha != str(snapshot_row.get("content_sha256") or "").lower():
                raise ContractError("delta_v2_forward_observation_ws_snapshot_hash_invalid")
            snapshot = buckets if isinstance(buckets, Mapping) else {"buckets": buckets}
            bucket_names = _snapshot_bucket_names(snapshot)
            action, authorized = _selected_action(feature)
            candidate_price = None
            selected_bucket = None
            if action is not None:
                selected_bucket = str(action.get("bucket") or "").strip()
                candidate_price = _finite_price(action.get("limit_price"))
                if not selected_bucket or candidate_price is None:
                    raise ContractError("delta_v2_forward_observation_selected_price_invalid")
                if selected_bucket not in bucket_names:
                    raise ContractError("delta_v2_forward_observation_selected_bucket_not_in_ws_snapshot")
            outcome = label["outcome"]
            semantics = str(outcome.get("label_semantics") or "").strip()
            if semantics != "hypothetical_requested_intents_not_fills":
                raise ContractError("delta_v2_forward_observation_label_semantics_invalid")
            settlement_pnl = outcome.get("hypothetical_requested_pnl_usdc")
            execution_cost = outcome.get("requested_cost_usdc")
            if _finite_nonnegative(execution_cost) is None:
                raise ContractError("delta_v2_forward_observation_requested_cost_invalid")
            if _finite_number(settlement_pnl) is None:
                raise ContractError("delta_v2_forward_observation_settlement_pnl_invalid")
            label_closed_at, _ = _utc(label.get("settlement_closed_at_utc"), "label_closed_at")
            labeled_at, _ = _utc(label.get("labeled_at_utc"), "label_labeled_at")
        except ContractError as exc:
            source_blockers.add(str(exc))
            counters[str(exc)] += 1
            continue
        observation_core = {
            "event_id": f"{fact['station_id']}|{fact['event_date']}",
            "observation_id": fact_id,
            "station_id": str(fact["station_id"]),
            "station_timezone": str(fact["settlement_timezone"]),
            "phase": phase,
            "decision_at_utc": decision_at,
            "policy_version": policy_version,
            "probabilities": dict(probabilities),
            "candidate_price": candidate_price,
            "candidate_price_basis": (
                "exact_plan_post_only_maker_limit" if candidate_price is not None else "no_selected_candidate"
            ),
            "selected_bucket": selected_bucket,
            "candidate_authorized": authorized,
            "fill_status": "NO_EXECUTION",
            "no_execution_policy": feature.get("no_execution_policy"),
            "execution_cost_usdc": execution_cost,
            "settlement_pnl_usdc": settlement_pnl,
            "label_semantics": semantics,
            "evidence_owner": "bayes_basket_target",
            "production_mode": "production_no_live",
            "evaluation_split": "FORWARD",
            "labels": [{
                "label_id": str(label["label_id"]),
                "truth_revision_no": int(label["truth_revision_no"]),
                "truth_sha256": str(label["truth_sha256"]),
                "winning_bucket": str(label["winning_bucket"]),
                "settlement_closed_at_utc": label_closed_at,
                "labeled_at_utc": labeled_at,
            }],
            "source_lineage": {
                "fact_content_sha256": str(fact["content_sha256"]),
                "probability_envelope_id": str(fact["probability_envelope_id"]),
                "posterior_sha256": str(fact["posterior_sha256"]),
                "ws_snapshot_id": str(fact["ws_snapshot_id"]),
                "official_ws_quote_sha256": str(fact["quote_sha256"]),
                "quote_observed_at": quote_observed_at,
                "quote_expires_at": quote_expires_at,
                "label_content_sha256": str(label["content_sha256"]),
            },
        }
        observations.append({
            **observation_core,
            "content_sha256": content_sha256(observation_core),
        })
    phase_counts = Counter(str(item["phase"]) for item in observations)
    price_count = sum(item["candidate_price"] is not None for item in observations)
    attribution_count = sum(
        item["settlement_pnl_usdc"] is not None for item in observations
    )
    identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of,
        "source": {
            "owner": "bayes_basket_target",
            "fact_contract": FACT_SCHEMA,
            "label_contract": LABEL_SCHEMA,
            "probability_source": "basket_probability_envelope",
            "quote_source": "basket_official_ws_snapshot:OFFICIAL_CLOB_WS",
            "projection": "immutable_fact_bound_sources_only",
        },
        "counts": {
            **dict(sorted(counters.items())),
            "observation_count": len(observations),
            "station_count": len({item["station_id"] for item in observations}),
            "candidate_price_count": price_count,
            "counterfactual_settlement_attribution_count": attribution_count,
            "phase_counts": dict(sorted(phase_counts.items())),
        },
        "observations": observations,
        "source_blockers": sorted(source_blockers),
        "state": "READY_NO_LIVE" if observations and not source_blockers else "BLOCKED_EVIDENCE",
        "formal_promotion_allowed": False,
        "live_activation_allowed": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def assert_forward_observation_candidate(artifact: Mapping[str, Any]) -> None:
    if artifact.get("schema") != SCHEMA:
        raise ContractError("delta_v2_forward_observation_schema_invalid")
    identity = {key: value for key, value in artifact.items() if key != "content_sha256"}
    if artifact.get("content_sha256") != content_sha256(identity):
        raise ContractError("delta_v2_forward_observation_content_hash_invalid")
    if any(artifact.get(key) is not False for key in (
        "formal_promotion_allowed", "live_activation_allowed", "writes_production_db", "calls_order_gateway",
    )) or artifact.get("orders_submitted") != 0:
        raise ContractError("delta_v2_forward_observation_authority_violation")


__all__ = [
    "SCHEMA",
    "assert_forward_observation_candidate",
    "build_from_connection",
]
