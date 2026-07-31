"""Bounded, read-only forward evidence scorecards for the target lane.

This module is intentionally a projection over the canonical append-only
``basket_policy_training_fact`` and ``basket_policy_training_label`` tables.
It never writes SQLite, calls a service, or treats a no-live requested intent
as a fill.  The returned receipt is content addressed so it can be replayed
and compared without introducing another SSOT.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .execution_version_vector import (
    ExecutionVersionVectorError,
    validate_no_execution_version_policy,
    validate_execution_version_vector,
)
from .delta_v2_training import FACT_SCHEMA
from .delta_v2_settlement_label import LABEL_SCHEMA
from .promotion_evidence import (
    validate_cash_phase_forward_scorecard,
    validate_station_policy_forward_scorecard,
)
from .cash_phase_oos import (
    build_cash_phase_oos_scorecard,
    assert_cash_phase_oos_promotable,
)


SCHEMA = "bayes_basket_target_forward_scorecard_v1"
MAKER_FORWARD_SCHEMA = "bayes_basket_target_maker_forward_scorecard_v1"
MAX_ROWS = 512
# Keep the worst-case retained JSON payload bounded at 64 MiB even when a
# complete station-day contains more than the former 256-row cap.
MAX_FEATURE_JSON_BYTES = 131_072
MAX_TOTAL_SCORECARD_ROWS = 20_000
MAX_TOTAL_SCORECARD_JSON_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 100_000
MAX_MAKER_TARGETS_PER_FACT = 64

REQUESTED_INTENT_SEMANTICS = "hypothetical_requested_intents_not_fills"
_ACTUAL_FILL_SEMANTICS = frozenset(
    {
        "actual_fills",
        "confirmed_fills",
        "confirmed_fill_events",
        "filled_orders",
        "live_fills",
    }
)
_REQUIRED_VECTOR_FIELDS = (
    "probability_sha256",
    "official_ws_quote_sha256",
    "inventory_sha256",
    "order_state_sha256",
    "station_policy_sha256",
    "cash_policy_decision_sha256",
    "economic_policy_schema",
    "economic_policy_artifact_sha256",
    "target_price_id",
    "quote_observed_at",
    "quote_expires_at",
    "vector_sha256",
)
_FACT_HASH_FIELDS = (
    "station_id",
    "event_date",
    "settlement_timezone",
    "decision_at_utc",
    "decision_scope_key",
    "captured_at_utc",
    "source_sequence",
    "quote_observed_at",
    "quote_expires_at",
    "quote_freshness_policy_sha256",
    "stage",
    "probability_envelope_id",
    "posterior_version",
    "posterior_sha256",
    "official_exclusion_mask_sha256",
    "ws_snapshot_id",
    "quote_version",
    "quote_sha256",
    "inventory_snapshot_id",
    "inventory_version",
    "inventory_sha256",
    "order_state_snapshot_id",
    "order_state_version",
    "order_state_sha256",
    "cycle_id",
    "decision_id",
    "decision_sha256",
    "plan_sha256",
    "intent_count",
    "intent_set_sha256",
    "policy_version_vector_json",
    "feature_json",
    "partition_key",
    "authority_scope",
    "created_at_utc",
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


class ForwardScorecardError(ValueError):
    """Invalid builder input; no database state is changed."""


def _as_utc(value: Any, *, name: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value.strip():
        raise ForwardScorecardError(f"{name}_required")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardScorecardError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardScorecardError(f"{name}_timezone_required")
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return normalized, parsed.astimezone(timezone.utc)


def _station_scope(value: Iterable[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        raise ForwardScorecardError("station_allowlist_must_be_iterable")
    try:
        values = tuple(sorted({str(item).strip() for item in value}))
    except TypeError as exc:
        raise ForwardScorecardError("station_allowlist_must_be_iterable") from exc
    if any(not item for item in values):
        raise ForwardScorecardError("station_allowlist_contains_empty_station")
    return values


def _event_date_scope(
    value: Iterable[str] | None,
) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        raise ForwardScorecardError(
            "event_date_allowlist_must_be_iterable"
        )
    try:
        values = tuple(sorted({str(item).strip() for item in value}))
    except TypeError as exc:
        raise ForwardScorecardError(
            "event_date_allowlist_must_be_iterable"
        ) from exc
    for item in values:
        try:
            parsed = datetime.strptime(item, "%Y-%m-%d")
        except ValueError as exc:
            raise ForwardScorecardError(
                "event_date_allowlist_contains_invalid_date"
            ) from exc
        if parsed.strftime("%Y-%m-%d") != item:
            raise ForwardScorecardError(
                "event_date_allowlist_contains_invalid_date"
            )
    return values


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _row_dict(cursor: sqlite3.Cursor, row: Sequence[Any]) -> dict[str, Any]:
    names = tuple(item[0] for item in (cursor.description or ()))
    return {name: row[index] for index, name in enumerate(names)}


def _bounded_json(value: Any, *, byte_length: Any) -> tuple[Mapping[str, Any] | None, str | None]:
    """Decode one feature/outcome without retaining unbounded JSON input."""

    try:
        size = int(byte_length)
    except (TypeError, ValueError):
        return None, "json_size_invalid"
    if size < 0 or size > MAX_FEATURE_JSON_BYTES:
        return None, "json_oversized"
    if isinstance(value, bytes):
        try:
            raw = value.decode("utf-8")
        except UnicodeDecodeError:
            return None, "json_encoding_invalid"
    elif isinstance(value, str):
        raw = value
    else:
        return None, "json_not_text"
    # Use the actual byte length as the authority; SQLite length can be NULL
    # on malformed legacy rows and must not silently permit them.
    if len(raw.encode("utf-8")) != size or size > MAX_FEATURE_JSON_BYTES:
        return None, "json_size_mismatch"
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "json_malformed"
    nodes = 0

    def walk(item: Any, depth: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return False
        if isinstance(item, dict):
            return all(isinstance(key, str) and walk(child, depth + 1) for key, child in item.items())
        if isinstance(item, list):
            return all(walk(child, depth + 1) for child in item)
        return True

    if not walk(decoded, 0):
        return None, "json_structure_out_of_bounds"
    if not isinstance(decoded, Mapping):
        return None, "json_root_not_mapping"
    return decoded, None


def _content_hash_status(
    row: Mapping[str, Any],
    *,
    schema: str,
    hash_fields: Sequence[str],
    id_prefix: str,
) -> str | None:
    """Validate a canonical row's stored SHA and derived content address."""

    stored = row.get("content_sha256")
    if not isinstance(stored, str) or len(stored) != 64 or any(
        char not in "0123456789abcdef" for char in stored.lower()
    ):
        return "content_sha256_invalid"
    if any(field not in row for field in hash_fields):
        return "content_sha256_fields_missing"
    identity = {field: row.get(field) for field in hash_fields}
    expected = content_sha256({"schema": schema, **identity})
    if stored.lower() != expected:
        return "content_sha256_mismatch"
    row_id = str(
        row.get(
            "fact_id"
            if id_prefix in {"sdfact_", "d2fact_"}
            else "label_id"
        )
        or ""
    )
    if row_id != f"{id_prefix}{expected[:40]}":
        return "content_address_id_mismatch"
    return None


def _embedded_scorecard_hash_status(raw: Mapping[str, Any]) -> str | None:
    """Require the formal nested scorecard itself to be content addressed."""

    stored = raw.get("content_sha256")
    if not isinstance(stored, str) or len(stored) != 64 or any(
        char not in "0123456789abcdef" for char in stored.lower()
    ):
        return "content_sha256_missing_or_invalid"
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    expected = content_sha256(identity)
    return None if stored.lower() == expected else "content_sha256_mismatch"


def _no_live_boundary(feature: Mapping[str, Any]) -> str | None:
    boundary = feature.get("no_live_boundary")
    if not isinstance(boundary, Mapping):
        return "no_live_boundary_missing"
    if boundary.get("execution_mode") != "production_no_live":
        return "no_live_execution_mode_invalid"
    if boundary.get("live_submit_authorized") is not False:
        return "no_live_submit_authorization_invalid"
    if boundary.get("calls_order_gateway") is not False:
        return "no_live_gateway_boundary_invalid"
    if boundary.get("orders_submitted") != 0:
        return "no_live_orders_submitted_nonzero"
    return None


def _feature_evidence(
    feature: Mapping[str, Any],
    *,
    intent_count: Any,
) -> dict[str, Any]:
    """Validate either an exact execution vector or a zero-intent policy."""

    try:
        if isinstance(intent_count, bool):
            raise ValueError
        normalized_intent_count = int(intent_count)
    except (TypeError, ValueError):
        normalized_intent_count = -1
    if normalized_intent_count < 0:
        return {
            "vector_count": 0,
            "valid_vectors": 0,
            "invalid_vectors": 0,
            "exact_vector_coverage": 0,
            "no_execution_policy_coverage": 0,
            "version_evidence_coverage": 0,
            "error": "intent_count_invalid",
        }
    vectors = feature.get("execution_version_vectors")
    no_policy = feature.get("no_execution_policy")
    if no_policy is not None:
        try:
            policy = validate_no_execution_version_policy(no_policy)
        except (ExecutionVersionVectorError, TypeError, ValueError):
            return {
                "vector_count": 0,
                "valid_vectors": 0,
                "invalid_vectors": 0,
                "exact_vector_coverage": 0,
                "no_execution_policy_coverage": 0,
                "version_evidence_coverage": 0,
                "error": "no_execution_policy_invalid",
            }
        if normalized_intent_count != 0:
            return {
                "vector_count": 0,
                "valid_vectors": 0,
                "invalid_vectors": 0,
                "exact_vector_coverage": 0,
                "no_execution_policy_coverage": 0,
                "version_evidence_coverage": 0,
                "error": "no_execution_policy_requires_zero_intents",
            }
        if vectors not in (None, []):
            return {
                "vector_count": 0,
                "valid_vectors": 0,
                "invalid_vectors": 0,
                "exact_vector_coverage": 0,
                "no_execution_policy_coverage": 0,
                "version_evidence_coverage": 0,
                "error": "no_execution_policy_vectors_conflict",
            }
        # Accessing the normalized policy also proves that its digest is
        # content addressed; the object is intentionally not persisted here.
        _ = policy.policy_sha256
        return {
            "vector_count": 0,
            "valid_vectors": 0,
            "invalid_vectors": 0,
            "exact_vector_coverage": 0,
            "no_execution_policy_coverage": 1,
            "version_evidence_coverage": 1,
            "error": None,
        }
    if vectors is None:
        return {
            "vector_count": 0,
            "valid_vectors": 0,
            "invalid_vectors": 0,
            "exact_vector_coverage": 0,
            "no_execution_policy_coverage": 0,
            "version_evidence_coverage": 0,
            "error": "exact_vector_missing",
        }
    vector_count, valid, invalid, exact, error = _feature_vectors(feature)
    if error is None and vector_count != normalized_intent_count:
        error = "exact_vector_count_mismatch"
        exact = 0
    return {
        "vector_count": vector_count,
        "valid_vectors": valid,
        "invalid_vectors": invalid,
        "exact_vector_coverage": exact,
        "no_execution_policy_coverage": 0,
        "version_evidence_coverage": exact,
        "error": error,
    }


def _feature_vectors(feature: Mapping[str, Any]) -> tuple[int, int, int, int, str | None]:
    raw = feature.get("execution_version_vectors")
    if raw is None:
        return 0, 0, 0, 0, "exact_vector_missing"
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
        return 0, 0, 1, 0, "exact_vector_not_list"
    if len(raw) > MAX_ROWS:
        return 0, 0, 1, 0, "exact_vector_count_out_of_bounds"
    valid = 0
    invalid = 0
    vector_digests: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            invalid += 1
            continue
        try:
            vector = validate_execution_version_vector(item)
        except (ExecutionVersionVectorError, TypeError, ValueError):
            invalid += 1
            continue
        if vector.vector_sha256 in vector_digests:
            invalid += 1
            continue
        vector_digests.add(vector.vector_sha256)
        valid += 1
    error = None if raw and invalid == 0 and valid == len(raw) else "exact_vector_invalid"
    return len(raw), valid, invalid, 1 if error is None and valid > 0 else 0, error


def _phase(feature: Mapping[str, Any]) -> str:
    decision = feature.get("cash_phase_decision")
    if isinstance(decision, Mapping):
        # ``next_phase`` is the committed phase after evaluating the cash
        # clock.  Older fixtures used ``phase``; retain that only as a
        # compatibility fallback when the authoritative field is absent.
        for field in ("next_phase", "phase"):
            value = decision.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
    return "UNKNOWN"


def _empty_counts() -> dict[str, Any]:
    return {
        "fact_rows_read": 0,
        "fact_rows_evaluated": 0,
        "retired_non_v2_fact_rows_excluded": 0,
        "label_rows_read": 0,
        "valid_label_revisions": 0,
        "invalid_label_revisions": 0,
        "labeled_facts": 0,
        "unlabeled_facts": 0,
        "malformed_feature_facts": 0,
        "by_station": {},
        "by_stage": {},
        "by_phase": {},
        "by_scope": [],
    }


def _bump(bucket: dict[str, dict[str, int]], key: str, *, labeled: bool) -> None:
    row = bucket.setdefault(key, {"facts": 0, "labeled": 0, "unlabeled": 0})
    row["facts"] += 1
    row["labeled" if labeled else "unlabeled"] += 1


def _scope_rows(
    counts: dict[str, Any],
    facts: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
) -> None:
    grouped: dict[tuple[str, str, str], dict[str, int]] = defaultdict(
        lambda: {"facts": 0, "labeled": 0, "unlabeled": 0}
    )
    for fact in facts:
        key = (
            str(fact.get("station_id") or "UNKNOWN"),
            str(fact.get("stage") or "UNKNOWN"),
            str(fact.get("phase") or "UNKNOWN"),
        )
        labeled = str(fact.get("fact_id")) in labels
        row = grouped[key]
        row["facts"] += 1
        row["labeled" if labeled else "unlabeled"] += 1
        _bump(counts["by_station"], key[0], labeled=labeled)
        _bump(counts["by_stage"], key[1], labeled=labeled)
        _bump(counts["by_phase"], key[2], labeled=labeled)
    counts["by_scope"] = [
        {
            "station_id": key[0],
            "stage": key[1],
            "phase": key[2],
            **grouped[key],
        }
        for key in sorted(grouped)
    ]


def _label_semantics(
    labels: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    counts = Counter()
    blockers: list[str] = []
    malformed = 0
    requested = 0
    actual_fills = 0
    unknown = 0
    no_live_violations = 0
    for label in labels.values():
        outcome = label.get("outcome")
        if not isinstance(outcome, Mapping):
            malformed += 1
            blockers.append("label_outcome_json_malformed")
            continue
        semantics = outcome.get("label_semantics")
        if not isinstance(semantics, str) or not semantics.strip():
            semantics = "UNKNOWN"
        else:
            semantics = semantics.strip()
        counts[semantics] += 1
        submitted = outcome.get("orders_submitted")
        gateway = outcome.get("calls_order_gateway")
        if submitted not in (None, 0) or gateway is True:
            no_live_violations += 1
        for fill_key in ("fill_count", "filled_shares", "fills"):
            value = outcome.get(fill_key)
            if value not in (None, 0, [], {}, False):
                no_live_violations += 1
                break
        if semantics == REQUESTED_INTENT_SEMANTICS:
            requested += 1
        elif semantics in _ACTUAL_FILL_SEMANTICS:
            actual_fills += 1
        else:
            unknown += 1
    if actual_fills:
        # This builder is target production_no_live evidence.  A row claiming
        # live fills cannot silently turn this no-live receipt into a fill
        # scorecard; it requires a separately governed source and remains a
        # contradiction here.
        blockers.append("actual_fill_evidence_conflicts_with_no_live_boundary")
    if unknown:
        blockers.append("label_semantics_unknown")
    if no_live_violations:
        blockers.append("label_no_live_boundary_violation")
    return {
        "label_rows_evaluated": len(labels),
        "malformed_outcome_rows": malformed,
        "requested_hypothetical_intents_not_fills": requested,
        "actual_fill_evidence": actual_fills,
        "unknown_semantics": unknown,
        "no_live_boundary_violations": no_live_violations,
        "by_label_semantics": dict(sorted(counts.items())),
        "blockers": sorted(set(blockers)),
    }


def _cash_evidence(
    facts: Sequence[Mapping[str, Any]],
    semantics: Mapping[str, Any],
) -> dict[str, Any]:
    candidates: list[tuple[str, Mapping[str, Any], dict[str, Any]]] = []
    blockers: list[str] = []
    for fact in facts:
        feature = fact.get("feature")
        raw = feature.get("cash_phase_scorecard") if isinstance(feature, Mapping) else None
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            blockers.append("cash_formal_scorecard_not_mapping")
            continue
        hash_error = _embedded_scorecard_hash_status(raw)
        if hash_error:
            blockers.append("cash_formal_scorecard_" + hash_error)
            continue
        try:
            validated = validate_cash_phase_forward_scorecard(raw)
        except (ContractError, TypeError, ValueError, KeyError) as exc:
            # Formal validator failures are data blockers, never a PASS.
            blockers.append("cash_formal_scorecard_invalid:" + type(exc).__name__)
            continue
        if validated.get("valid") is not True:
            blockers.append("cash_formal_scorecard_checks_not_complete")
            continue
        candidates.append((str(fact.get("fact_id") or ""), raw, validated))
    candidate_hashes = sorted({content_sha256(raw) for _, raw, _ in candidates})
    if len(candidate_hashes) > 1:
        blockers.append("cash_formal_scorecard_ambiguous")
    if not candidates:
        blockers.append("cash_formal_scorecard_missing_or_invalid")
    fact_hashes = {
        str(fact.get("fact_id") or ""): fact.get("fact_content_hash_valid") is True
        for fact in facts
    }
    content_addressed = bool(candidates) and all(
        fact_hashes.get(fact_id) is True for fact_id, _, _ in candidates
    )
    if candidates and not content_addressed:
        blockers.append("cash_formal_scorecard_not_content_addressed")
    if int(semantics.get("actual_fill_evidence", 0)):
        blockers.append("cash_real_fill_evidence_mixed_in")
    selected = candidates[0][2] if candidates else None
    return {
        "formal_validator": "validate_cash_phase_forward_scorecard",
        "candidate_count": len(candidates),
        "candidate_content_sha256": candidate_hashes,
        "content_addressed_by_fact_sha256": content_addressed,
        "selected": selected,
        "candidate_fill_semantics": "no_live_candidate_fill_simulation_or_receipt",
        "real_fill_evidence": int(semantics.get("actual_fill_evidence", 0)),
        "structurally_complete": not blockers,
        "blockers": sorted(set(blockers)),
    }


def _station_evidence(
    facts: Sequence[Mapping[str, Any]],
    station_allowlist: Sequence[str] | None = None,
) -> dict[str, Any]:
    observed_station_ids = tuple(sorted({str(fact.get("station_id") or "") for fact in facts}))
    expected_station_ids = tuple(
        sorted(station_allowlist)
        if station_allowlist is not None
        else observed_station_ids
    )
    candidates: list[tuple[str, Mapping[str, Any], dict[str, Any]]] = []
    blockers: list[str] = []
    if set(observed_station_ids) != set(expected_station_ids):
        blockers.append("station_allowlist_scope_incomplete")
    for fact in facts:
        feature = fact.get("feature")
        if not isinstance(feature, Mapping):
            continue
        raw = feature.get("station_policy_scorecard")
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            blockers.append("station_formal_scorecard_not_mapping")
            continue
        hash_error = _embedded_scorecard_hash_status(raw)
        if hash_error:
            blockers.append("station_formal_scorecard_" + hash_error)
            continue
        try:
            validated = validate_station_policy_forward_scorecard(
                raw,
                expected_station_ids=expected_station_ids,
            )
        except (ContractError, TypeError, ValueError, KeyError) as exc:
            # Formal validator failures are data blockers, never a PASS.
            blockers.append("station_formal_scorecard_invalid:" + type(exc).__name__)
            continue
        if validated.get("valid") is not True:
            blockers.append("station_formal_scorecard_checks_not_complete")
            continue
        candidates.append((str(fact.get("fact_id") or ""), raw, validated))
    candidate_hashes = sorted({content_sha256(raw) for _, raw, _ in candidates})
    if len(candidate_hashes) > 1:
        blockers.append("station_formal_scorecard_ambiguous")
    if not candidates:
        blockers.append("station_formal_scorecard_missing_or_invalid")
    fact_hashes = {
        str(fact.get("fact_id") or ""): fact.get("fact_content_hash_valid") is True
        for fact in facts
    }
    content_addressed = bool(candidates) and all(
        fact_hashes.get(fact_id) is True for fact_id, _, _ in candidates
    )
    if candidates and not content_addressed:
        blockers.append("station_formal_scorecard_not_content_addressed")
    return {
        "formal_validator": "validate_station_policy_forward_scorecard",
        "observed_station_ids": list(observed_station_ids),
        "expected_station_ids": list(expected_station_ids),
        "candidate_count": len(candidates),
        "candidate_content_sha256": candidate_hashes,
        "content_addressed_by_fact_sha256": content_addressed,
        "selected": candidates[0][2] if candidates else None,
        "structurally_complete": not blockers,
        "blockers": sorted(blockers),
    }


def _cash_observation_adapter(
    facts: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Project canonical fact/label rows without inventing economics.

    The canonical training fact currently does not always retain the winning
    bucket probability, exact all-in ask, policy training cutoff, or governed
    fault cases needed by the formal cash scorecard.  Those fields must remain
    explicit blockers rather than being reconstructed from basket scores or
    Maker reservation prices.
    """

    observations: list[dict[str, Any]] = []
    blockers: set[str] = set()
    field_coverage = Counter()
    event_ids: set[str] = set()
    for fact in facts:
        label = labels.get(str(fact.get("fact_id") or ""))
        if label is None:
            continue
        feature = fact.get("feature")
        if not isinstance(feature, Mapping):
            blockers.add("cash_adapter_feature_missing")
            continue
        decision = feature.get("cash_phase_decision")
        if not isinstance(decision, Mapping):
            blockers.add("cash_adapter_phase_decision_missing")
            continue
        policy_version = str(decision.get("policy_version") or "").strip()
        if not policy_version:
            blockers.add("cash_adapter_policy_version_missing")
        phase = _phase(feature)
        if phase == "UNKNOWN":
            blockers.add("cash_adapter_phase_missing")
        event_id = (
            str(fact.get("station_id") or "")
            + "|"
            + str(fact.get("event_date") or "")
        )
        event_ids.add(event_id)
        outcome = label.get("outcome")
        semantics = (
            str(outcome.get("label_semantics") or "")
            if isinstance(outcome, Mapping)
            else ""
        )
        probability_by_bucket = feature.get("probability_by_bucket")
        if not isinstance(probability_by_bucket, Mapping):
            probability_by_bucket = None
            blockers.add("cash_adapter_winning_bucket_probability_missing")
        else:
            field_coverage["winning_bucket_probability"] += 1
        all_in_yes_ask = feature.get("all_in_yes_ask")
        if _finite_number(all_in_yes_ask, positive=True) is None:
            all_in_yes_ask = None
            blockers.add("cash_adapter_all_in_yes_ask_missing")
        else:
            field_coverage["all_in_yes_ask"] += 1
        observation = {
            "event_id": event_id,
            "observation_id": str(fact.get("fact_id") or ""),
            "station_id": str(fact.get("station_id") or ""),
            "station_timezone": str(
                fact.get("settlement_timezone") or ""
            ),
            "phase": phase,
            "decision_at_utc": str(fact.get("decision_at_utc") or ""),
            "policy_version": policy_version,
            "probability_by_bucket": probability_by_bucket,
            "all_in_yes_ask": all_in_yes_ask,
            "label": {
                "label_id": str(label.get("label_id") or ""),
                "truth_revision_no": label.get("truth_revision_no"),
                "winning_bucket": str(label.get("winning_bucket") or ""),
                "settlement_closed_at_utc": str(
                    label.get("settlement_closed_at_utc") or ""
                ),
                "labeled_at_utc": str(label.get("labeled_at_utc") or ""),
            },
            "label_semantics": semantics,
            "fill_status": "NO_EXECUTION",
            "no_execution_policy": feature.get("no_execution_policy"),
            "candidate_authorized": False,
            "execution_cost_usdc": 0.0,
            "evidence_owner": "bayes_basket_target",
            "production_mode": "production_no_live",
            "evaluation_split": "FORWARD",
            "content_sha256": str(fact.get("content_sha256") or ""),
        }
        observations.append(observation)
    if not observations:
        blockers.add("cash_adapter_observations_missing")
    # These are authority inputs, not properties that can be inferred from a
    # fact/label pair.
    blockers.add("cash_adapter_policy_training_cutoff_missing")
    blockers.add("cash_adapter_required_fault_cases_missing")
    receipt: dict[str, Any] = {
        "schema": "bayes_basket_target_cash_observation_adapter_v1",
        "state": "BLOCKED_EVIDENCE" if blockers else "READY",
        "observation_count": len(observations),
        "unique_event_count": len(event_ids),
        "field_coverage": {
            "winning_bucket_probability": int(
                field_coverage["winning_bucket_probability"]
            ),
            "all_in_yes_ask": int(field_coverage["all_in_yes_ask"]),
        },
        "phase_counts": dict(
            sorted(Counter(row["phase"] for row in observations).items())
        ),
        "observations": observations,
        "blockers": sorted(blockers),
        "production_no_live": True,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    receipt["content_sha256"] = content_sha256(receipt)
    return receipt


def _station_policy_candidate(
    facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Describe transfer evidence coverage without fabricating OOS families."""

    station_ids = sorted(
        {str(fact.get("station_id") or "") for fact in facts}
    )
    event_ids = {
        (
            str(fact.get("station_id") or ""),
            str(fact.get("event_date") or ""),
        )
        for fact in facts
    }
    season_bins: set[str] = set()
    lead_buckets: set[str] = set()
    for fact in facts:
        parts = str(fact.get("partition_key") or "").split("|")
        if len(parts) >= 2 and parts[1]:
            season_bins.add(parts[1])
        if len(parts) >= 4 and parts[3]:
            lead_buckets.add(parts[3])
    blockers = {
        "station_candidate_four_seasons_incomplete"
        if len(season_bins) < 4 else "",
        "station_candidate_multi_station_incomplete"
        if len(station_ids) < 2 else "",
        "station_candidate_lead_issuance_matrix_incomplete",
        "station_candidate_family_candidate_baseline_scores_missing",
        "station_candidate_training_cutoff_missing",
    }
    blockers.discard("")
    receipt: dict[str, Any] = {
        "schema": "bayes_basket_target_station_policy_candidate_v1",
        "state": "BLOCKED_EVIDENCE" if blockers else "READY",
        "station_ids": station_ids,
        "station_count": len(station_ids),
        "event_count": len(event_ids),
        "season_bins": sorted(season_bins),
        "season_count": len(season_bins),
        "lead_buckets": sorted(lead_buckets),
        "run_cycles": [],
        "candidate_baseline_family_count": 0,
        "blockers": sorted(blockers),
        "production_no_live": True,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    receipt["content_sha256"] = content_sha256(receipt)
    return receipt


def _finite_number(
    value: Any,
    *,
    positive: bool = False,
) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not (-float("inf") < parsed < float("inf")):
        return None
    if positive and parsed <= 0.0:
        return None
    return parsed


def _maker_targets(
    facts: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract exact Maker candidates from canonical strict-delta facts."""

    candidates: list[dict[str, Any]] = []
    blockers: list[str] = []
    seen: dict[str, str] = {}
    required_lineage = (
        "maker_grid_artifact_sha256",
        "maker_grid_artifact_curve_sha256",
        "maker_grid_point_sha256",
        "maker_grid_curve_sha256",
    )
    for fact in facts:
        feature = fact.get("feature")
        plan = feature.get("plan") if isinstance(feature, Mapping) else None
        if not isinstance(plan, Mapping):
            continue
        targets = plan.get("targets")
        if targets is None:
            continue
        if (
            isinstance(targets, (str, bytes, bytearray))
            or not isinstance(targets, Sequence)
            or len(targets) > MAX_MAKER_TARGETS_PER_FACT
        ):
            blockers.append("maker_targets_invalid_or_out_of_bounds")
            continue
        for raw in targets:
            if not isinstance(raw, Mapping):
                blockers.append("maker_target_not_mapping")
                continue
            execution = str(raw.get("execution_candidate") or "").strip().upper()
            candidate_id = str(raw.get("maker_candidate_id") or "").strip()
            if execution != "MAKER" and not candidate_id:
                continue
            if execution != "MAKER" or not candidate_id:
                blockers.append("maker_candidate_execution_identity_incomplete")
                continue
            price = _finite_number(raw.get("candidate_price"), positive=True)
            shares = _finite_number(raw.get("clip_shares"), positive=True)
            timeout = _finite_number(
                raw.get("maker_timeout_seconds"),
                positive=True,
            )
            effective_price = _finite_number(
                raw.get("effective_price"),
                positive=True,
            )
            bucket = str(raw.get("bucket") or "").strip()
            target_price_id = str(raw.get("target_price_id") or "").strip()
            if (
                price is None
                or not 0.0 < price < 1.0
                or shares is None
                or timeout is None
                or timeout > 7 * 24 * 60 * 60
                or effective_price is None
                or not 0.0 < effective_price < 1.0
                or not bucket
                or not target_price_id
            ):
                blockers.append("maker_candidate_contract_incomplete")
                continue
            lineage: dict[str, str] = {}
            for field in required_lineage:
                value = str(raw.get(field) or "").strip().lower()
                if (
                    len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)
                ):
                    blockers.append("maker_candidate_grid_lineage_incomplete")
                    lineage = {}
                    break
                lineage[field] = value
            if not lineage:
                continue
            candidate = {
                "candidate_id": candidate_id,
                "fact_id": str(fact.get("fact_id") or ""),
                "station_id": str(fact.get("station_id") or ""),
                "event_date": str(fact.get("event_date") or ""),
                "settlement_timezone": str(
                    fact.get("settlement_timezone") or ""
                ),
                "decision_at_utc": str(fact.get("decision_at_utc") or ""),
                "ws_snapshot_id": str(fact.get("ws_snapshot_id") or ""),
                "official_ws_quote_sha256": str(
                    fact.get("quote_sha256") or ""
                ).lower(),
                "bucket": bucket,
                "target_price_id": target_price_id,
                "candidate_price": price,
                "effective_price": effective_price,
                "requested_shares": shares,
                "timeout_seconds": timeout,
                **lineage,
            }
            candidate_sha = content_sha256(candidate)
            previous = seen.get(candidate_id)
            if previous is not None and previous != candidate_sha:
                blockers.append("maker_candidate_id_content_conflict")
                continue
            if previous is None:
                candidates.append(candidate)
                seen[candidate_id] = candidate_sha
    return candidates, sorted(set(blockers))


def _official_ws_rows_for_maker(
    conn: sqlite3.Connection,
    *,
    station_ids: Sequence[str],
    event_dates: Sequence[str],
    as_of_utc: str,
    max_rows: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read and verify one bounded official-WS path without another store."""

    blockers: list[str] = []
    if not station_ids or not event_dates:
        return [], blockers
    if not _table_exists(conn, "basket_official_ws_snapshot"):
        return [], ["maker_official_ws_snapshot_table_missing"]
    columns = _table_columns(conn, "basket_official_ws_snapshot")
    required = {
        "snapshot_id", "market_id", "station_id", "event_date",
        "settlement_timezone", "sequence", "observed_at", "quote_version",
        "quote_observed_at", "quote_expires_at",
        "quote_freshness_policy_sha256", "quote_source",
        "inventory_version", "order_state_version",
        "remaining_budget_usdc", "buckets_json", "cash_phase_input_json",
        "content_sha256", "state", "created_at",
    }
    missing = sorted(required - columns)
    if missing:
        return [], [
            "maker_official_ws_schema_columns_missing:" + ",".join(missing)
        ]
    station_placeholders = ",".join("?" for _ in station_ids)
    event_placeholders = ",".join("?" for _ in event_dates)
    def _ws_pages() -> Iterable[dict[str, Any]]:
        last_key: tuple[str, str, int, str, str] | None = None
        while True:
            page_where = [
                "state='COMMITTED'",
                f"station_id IN ({station_placeholders})",
                f"event_date IN ({event_placeholders})",
                "(julianday(quote_observed_at)<=julianday(?) "
                "OR julianday(quote_observed_at) IS NULL)",
            ]
            page_params: list[Any] = [*station_ids, *event_dates, as_of_utc]
            if last_key is not None:
                page_where.append(
                    "(station_id>? OR (station_id=? AND event_date>?) "
                    "OR (station_id=? AND event_date=? AND sequence>?) "
                    "OR (station_id=? AND event_date=? AND sequence=? "
                    "AND quote_observed_at>?) OR (station_id=? "
                    "AND event_date=? AND sequence=? AND quote_observed_at=? "
                    "AND snapshot_id>?))"
                )
                page_params.extend(
                    (
                        last_key[0],
                        last_key[0], last_key[1],
                        last_key[0], last_key[1], last_key[2],
                        last_key[0], last_key[1], last_key[2], last_key[3],
                        last_key[0], last_key[1], last_key[2], last_key[3], last_key[4],
                    )
                )
            query = (
                "SELECT snapshot_id,market_id,station_id,event_date,"
                "settlement_timezone,sequence,observed_at,quote_version,"
                "quote_observed_at,quote_expires_at,quote_freshness_policy_sha256,"
                "quote_source,inventory_version,order_state_version,"
                "remaining_budget_usdc,"
                "length(CAST(buckets_json AS BLOB)) AS buckets_bytes,"
                "CASE WHEN length(CAST(buckets_json AS BLOB))<=? "
                "THEN buckets_json ELSE NULL END AS buckets_json,"
                "length(CAST(cash_phase_input_json AS BLOB)) AS cash_bytes,"
                "CASE WHEN length(CAST(cash_phase_input_json AS BLOB))<=? "
                "THEN cash_phase_input_json ELSE NULL END AS cash_phase_input_json,"
                "content_sha256,state,created_at "
                "FROM basket_official_ws_snapshot WHERE "
                + " AND ".join(page_where)
                + " ORDER BY station_id,event_date,sequence,quote_observed_at,snapshot_id LIMIT ?"
            )
            cursor = conn.execute(
                query,
                [MAX_FEATURE_JSON_BYTES, MAX_FEATURE_JSON_BYTES, *page_params, max_rows],
            )
            page = cursor.fetchall()
            if not page:
                return
            page_rows = [_row_dict(cursor, raw) for raw in page]
            for page_row in page_rows:
                yield page_row
            if len(page_rows) < max_rows:
                return
            tail = page_rows[-1]
            try:
                sequence = int(tail.get("sequence"))
            except (TypeError, ValueError):
                sequence = -1
            last_key = (
                str(tail.get("station_id") or ""),
                str(tail.get("event_date") or ""),
                sequence,
                str(tail.get("quote_observed_at") or ""),
                str(tail.get("snapshot_id") or ""),
            )
    rows: list[dict[str, Any]] = []
    last_sequence: dict[tuple[str, str], int] = {}
    last_observed: dict[tuple[str, str], datetime] = {}
    for row in _ws_pages():
        buckets, bucket_error = _bounded_json(
            row.get("buckets_json"),
            byte_length=row.get("buckets_bytes"),
        )
        # buckets_json is canonically a list, while _bounded_json intentionally
        # accepts only mappings. Decode it separately under the same byte and
        # structure bounds.
        if bucket_error == "json_root_not_mapping":
            try:
                buckets = json.loads(str(row.get("buckets_json")))
                bucket_error = None if isinstance(buckets, list) else bucket_error
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        cash, cash_error = _bounded_json(
            row.get("cash_phase_input_json"),
            byte_length=row.get("cash_bytes"),
        )
        if bucket_error or cash_error:
            blockers.append("maker_official_ws_json_invalid")
            continue
        if not isinstance(buckets, list) or len(buckets) > MAX_ROWS:
            blockers.append("maker_official_ws_bucket_count_out_of_bounds")
            continue
        if any(not isinstance(item, Mapping) for item in buckets):
            blockers.append("maker_official_ws_bucket_not_mapping")
            continue
        try:
            observed_text, observed = _as_utc(
                row.get("quote_observed_at"),
                name="maker_quote_observed_at",
            )
            expires_text, expires = _as_utc(
                row.get("quote_expires_at"),
                name="maker_quote_expires_at",
            )
            _created_text, created = _as_utc(
                row.get("created_at"),
                name="maker_quote_created_at",
            )
            sequence = int(row.get("sequence"))
        except (ForwardScorecardError, TypeError, ValueError):
            blockers.append("maker_official_ws_timestamp_or_sequence_invalid")
            continue
        if sequence < 0 or expires <= observed or created > datetime.fromisoformat(
            as_of_utc.replace("Z", "+00:00")
        ):
            blockers.append("maker_official_ws_clock_invalid")
            continue
        key = (str(row.get("station_id") or ""), str(row.get("event_date") or ""))
        if sequence <= last_sequence.get(key, -1):
            blockers.append("maker_official_ws_sequence_not_strictly_increasing")
            continue
        if observed < last_observed.get(key, datetime.min.replace(tzinfo=timezone.utc)):
            blockers.append("maker_official_ws_observed_at_rewind")
            continue
        identity = {
            key_name: row.get(key_name)
            for key_name in (
                "snapshot_id", "market_id", "station_id", "event_date",
                "settlement_timezone", "sequence", "observed_at",
                "quote_version", "quote_observed_at", "quote_expires_at",
                "quote_freshness_policy_sha256", "quote_source",
                "inventory_version", "order_state_version",
                "remaining_budget_usdc", "state", "created_at",
            )
        }
        identity["buckets_json"] = buckets
        identity["cash_phase_input_json"] = cash
        expected_sha = content_sha256({
            "schema": "basket_official_ws_snapshot_v1",
            **identity,
        })
        if (
            str(row.get("content_sha256") or "").lower() != expected_sha
            or row.get("quote_source") != "OFFICIAL_CLOB_WS"
        ):
            blockers.append("maker_official_ws_content_or_authority_invalid")
            continue
        last_sequence[key] = sequence
        last_observed[key] = observed
        rows.append({
            **identity,
            "quote_observed_at": observed_text,
            "content_sha256": expected_sha,
        })
    return rows, sorted(set(blockers))


def _maker_forward_evidence(
    conn: sqlite3.Connection,
    *,
    facts: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
    as_of_utc: str,
    max_rows: int,
) -> dict[str, Any]:
    """Project no-live Maker queue/fill/PnL evidence from canonical rows."""

    candidates, blockers = _maker_targets(facts)
    ws_rows, ws_blockers = _official_ws_rows_for_maker(
        conn,
        station_ids=tuple(sorted({item["station_id"] for item in candidates})),
        event_dates=tuple(sorted({item["event_date"] for item in candidates})),
        as_of_utc=as_of_utc,
        max_rows=max_rows,
    )
    blockers.extend(ws_blockers)
    by_scope: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in ws_rows:
        by_scope[(str(row["station_id"]), str(row["event_date"]))].append(row)
    status_counts: Counter[str] = Counter()
    queue_known = 0
    grid_lineage_complete = 0
    labeled_pnl: list[float] = []
    candidate_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            decision_dt = _as_utc(
                candidate["decision_at_utc"],
                name="maker_candidate_decision_at_utc",
            )[1]
        except ForwardScorecardError:
            blockers.append("maker_candidate_decision_at_invalid")
            continue
        deadline = decision_dt.timestamp() + float(candidate["timeout_seconds"])
        path = []
        initial: Mapping[str, Any] | None = None
        terminal_seen = False
        for snapshot in by_scope.get(
            (candidate["station_id"], candidate["event_date"]),
            (),
        ):
            observed_dt = _as_utc(
                snapshot["quote_observed_at"],
                name="maker_path_quote_observed_at",
            )[1]
            if initial is None:
                if (
                    snapshot["snapshot_id"] != candidate["ws_snapshot_id"]
                    or snapshot["content_sha256"]
                    != candidate["official_ws_quote_sha256"]
                ):
                    continue
                if observed_dt > decision_dt:
                    blockers.append("maker_candidate_initial_ws_not_strict_asof")
                    break
                initial = snapshot
                path.append(snapshot)
                if observed_dt.timestamp() >= deadline:
                    terminal_seen = True
                    break
                continue
            if observed_dt < decision_dt:
                continue
            path.append(snapshot)
            if observed_dt.timestamp() >= deadline:
                terminal_seen = True
                break
        if initial is None:
            blockers.append("maker_candidate_initial_ws_binding_missing")
            status_counts["INVALID"] += 1
            continue
        token_id = ""
        initial_queue: float | None = None
        crossed_at: str | None = None
        path_sha256: list[str] = []
        valid_path = True
        for snapshot in path:
            matches = [
                item
                for item in snapshot["buckets_json"]
                if str(item.get("bucket") or "") == candidate["bucket"]
            ]
            if len(matches) != 1:
                blockers.append("maker_candidate_bucket_path_not_exact")
                valid_path = False
                break
            bucket = matches[0]
            current_token = str(bucket.get("token_id") or "")
            if not current_token or (token_id and token_id != current_token):
                blockers.append("maker_candidate_token_path_drift")
                valid_path = False
                break
            token_id = current_token
            if bucket.get("maker_queue_known") is not True:
                blockers.append("maker_candidate_queue_unknown")
                valid_path = False
                break
            queue = _finite_number(
                bucket.get("maker_queue_ahead_shares"),
            )
            best_ask = _finite_number(bucket.get("best_ask"), positive=True)
            if queue is None or queue < 0.0 or best_ask is None:
                blockers.append("maker_candidate_queue_or_ask_invalid")
                valid_path = False
                break
            if initial_queue is None:
                initial_queue = queue
                if candidate["candidate_price"] + 1e-12 >= best_ask:
                    blockers.append("maker_candidate_not_post_only_at_creation")
                    valid_path = False
                    break
            if (
                crossed_at is None
                and best_ask <= candidate["candidate_price"] + 1e-12
            ):
                crossed_at = str(snapshot["quote_observed_at"])
            path_sha256.append(str(snapshot["content_sha256"]))
        if not valid_path:
            status_counts["INVALID"] += 1
            continue
        queue_known += 1
        grid_lineage_complete += 1
        if crossed_at is not None:
            status = "COUNTERFACTUAL_CROSS_OBSERVED"
        elif terminal_seen:
            status = "COUNTERFACTUAL_NONFILL_TIMEOUT"
        else:
            status = "CENSORED_AS_OF"
        status_counts[status] += 1
        label = labels.get(candidate["fact_id"])
        pnl = None
        if label is not None and status == "COUNTERFACTUAL_CROSS_OBSERVED":
            winner = str(label.get("winning_bucket") or "")
            settlement = (
                float(candidate["requested_shares"])
                if winner == candidate["bucket"]
                else 0.0
            )
            cost = (
                float(candidate["requested_shares"])
                * float(candidate["effective_price"])
            )
            pnl = settlement - cost
            labeled_pnl.append(pnl)
        candidate_rows.append({
            "candidate_id": candidate["candidate_id"],
            "fact_id": candidate["fact_id"],
            "station_id": candidate["station_id"],
            "event_date": candidate["event_date"],
            "bucket": candidate["bucket"],
            "target_price_id": candidate["target_price_id"],
            "status": status,
            "candidate_price": candidate["candidate_price"],
            "effective_price": candidate["effective_price"],
            "requested_shares": candidate["requested_shares"],
            "timeout_seconds": candidate["timeout_seconds"],
            "initial_queue_ahead_shares": initial_queue,
            "counterfactual_cross_observed_at_utc": crossed_at,
            "counterfactual_pnl_usdc": pnl,
            "official_ws_path_count": len(path_sha256),
            "official_ws_path_sha256": content_sha256(path_sha256),
            "maker_grid_artifact_sha256": candidate[
                "maker_grid_artifact_sha256"
            ],
            "maker_grid_point_sha256": candidate[
                "maker_grid_point_sha256"
            ],
            "evidence_class": "NO_LIVE_COUNTERFACTUAL_PROXY",
            "formal_activation_eligible": False,
        })
    if not candidates:
        blockers.append("maker_forward_candidates_missing")
    resolved = (
        status_counts["COUNTERFACTUAL_CROSS_OBSERVED"]
        + status_counts["COUNTERFACTUAL_NONFILL_TIMEOUT"]
    )
    if candidates and resolved != len(candidates):
        blockers.append("maker_forward_lifecycle_incomplete")
    structural_blockers = {
        item for item in blockers
        if item
        not in {
            "realized_maker_lifecycle_required_for_formal_promotion",
        }
    }
    receipt: dict[str, Any] = {
        "schema": MAKER_FORWARD_SCHEMA,
        "as_of_utc": as_of_utc,
        "evidence_class": "NO_LIVE_COUNTERFACTUAL_PROXY",
        "formal_activation_eligible": False,
        "production_no_live": True,
        "live_order_authorized": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "source_tables": [
            "basket_policy_training_fact",
            "basket_policy_training_label",
            "basket_official_ws_snapshot",
        ],
        "creates_parallel_store": False,
        "candidate_count": len(candidates),
        "resolved_lifecycle_count": resolved,
        "queue_known_count": queue_known,
        "grid_lineage_complete_count": grid_lineage_complete,
        "status_counts": dict(sorted(status_counts.items())),
        "counterfactual_pnl": {
            "labeled_count": len(labeled_pnl),
            "sum_usdc": sum(labeled_pnl),
            "mean_usdc": (
                sum(labeled_pnl) / len(labeled_pnl)
                if labeled_pnl else None
            ),
        },
        "fault_scorecard": {
            "fault_count": len(structural_blockers),
            "fault_codes": sorted(structural_blockers),
        },
        "candidate_rows": sorted(
            candidate_rows,
            key=lambda item: (
                item["station_id"],
                item["event_date"],
                item["fact_id"],
                item["bucket"],
                item["candidate_id"],
            ),
        ),
        "ready_for_no_live_forward_scorecard": bool(
            candidates and resolved == len(candidates) and not structural_blockers
        ),
        "ready_for_formal_maker_grid_promotion": False,
        "promotion_blockers": [
            "realized_maker_lifecycle_required_for_formal_promotion"
        ],
        "blockers": sorted(set(blockers)),
    }
    receipt["content_sha256"] = content_sha256(receipt)
    return receipt


def build_forward_scorecard(
    conn: sqlite3.Connection,
    as_of: str | None = None,
    station_allowlist: Iterable[str] | None = None,
    max_rows: int = MAX_ROWS,
    *,
    as_of_utc: str | None = None,
    event_date_allowlist: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build one deterministic scorecard from canonical training rows.

    ``as_of`` is applied to both fact creation/decision time and label time.
    ``max_rows`` is a per-page memory bound, not a denominator cap.  Facts,
    labels, and official-WS paths are consumed through keyset pages until
    exhaustion; no page boundary may hide rows from the scorecard.
    """

    if as_of is None:
        as_of = as_of_utc
    elif as_of_utc is not None and str(as_of).strip() != str(as_of_utc).strip():
        raise ForwardScorecardError("as_of_and_as_of_utc_conflict")
    as_of_utc, as_of_dt = _as_utc(as_of, name="as_of")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_ROWS:
        raise ForwardScorecardError(
            f"max_rows_must_be_between_1_and_{MAX_ROWS}"
        )
    allowlist = _station_scope(station_allowlist)
    event_dates = _event_date_scope(event_date_allowlist)
    counts = _empty_counts()
    blockers: set[str] = set()
    facts: list[dict[str, Any]] = []
    labels: dict[str, dict[str, Any]] = {}
    retained_json_bytes = 0

    if not _table_exists(conn, "basket_policy_training_fact"):
        blockers.add("training_fact_table_missing")
    else:
        fact_columns = _table_columns(conn, "basket_policy_training_fact")
        fact_base = {
            "fact_id",
            "station_id",
            "event_date",
            "decision_at_utc",
            "created_at_utc",
            "stage",
            "feature_json",
        }
        missing_base = sorted(fact_base - fact_columns)
        if missing_base:
            blockers.add("training_fact_schema_columns_missing:" + ",".join(missing_base))
            fact_columns = set()
        if fact_columns:
        # julianday handles equivalent UTC offsets.  Invalid timestamps are
        # deliberately retained (the OR NULL branches) so Python can report
        # them as blockers instead of silently filtering bad evidence out.
            where = [
                "(julianday(decision_at_utc)<=julianday(?) OR julianday(decision_at_utc) IS NULL)",
                "(julianday(created_at_utc)<=julianday(?) OR julianday(created_at_utc) IS NULL)",
            ]
            params: list[Any] = [as_of_utc, as_of_utc]
            if "captured_at_utc" in fact_columns:
                where.append(
                    "(julianday(captured_at_utc)<=julianday(?) OR julianday(captured_at_utc) IS NULL)"
                )
                params.append(as_of_utc)
            if allowlist is not None:
                if allowlist:
                    where.append("station_id IN (" + ",".join("?" for _ in allowlist) + ")")
                    params.extend(allowlist)
                else:
                    where.append("1=0")
            if event_dates is not None:
                if event_dates:
                    where.append(
                        "event_date IN ("
                        + ",".join("?" for _ in event_dates)
                        + ")"
                    )
                    params.extend(event_dates)
                else:
                    where.append("1=0")
            select_fields = [
                "fact_id", "station_id", "event_date", "decision_at_utc",
                "created_at_utc", "stage", "feature_json",
            ]
            for field in _FACT_HASH_FIELDS:
                if field in fact_columns and field not in select_fields:
                    select_fields.append(field)
            if "content_sha256" in fact_columns:
                select_fields.append("content_sha256")
            projection: list[str] = []
            for field in select_fields:
                if field == "feature_json":
                    projection.extend(
                        (
                            "length(CAST(feature_json AS BLOB)) AS feature_bytes",
                            "CASE WHEN length(CAST(feature_json AS BLOB))<=? "
                            "THEN feature_json ELSE NULL END AS feature_json",
                        )
                    )
                else:
                    projection.append(field)
            def _fact_pages() -> Iterable[dict[str, Any]]:
                nonlocal retained_json_bytes
                last_key: tuple[str, str, str, str, str] | None = None
                while True:
                    page_where = list(where)
                    page_params = list(params)
                    if last_key is not None:
                        page_where.append(
                            "(event_date>? OR (event_date=? AND station_id>?) "
                            "OR (event_date=? AND station_id=? AND stage>?) "
                            "OR (event_date=? AND station_id=? AND stage=? "
                            "AND decision_at_utc>?) OR (event_date=? "
                            "AND station_id=? AND stage=? AND decision_at_utc=? "
                            "AND fact_id>?))"
                        )
                        page_params.extend(
                            (
                                last_key[0],
                                last_key[0], last_key[1],
                                last_key[0], last_key[1], last_key[2],
                                last_key[0], last_key[1], last_key[2], last_key[3],
                                last_key[0], last_key[1], last_key[2], last_key[3], last_key[4],
                            )
                        )
                    query = (
                        "SELECT " + ",".join(projection)
                        + " FROM basket_policy_training_fact WHERE "
                        + " AND ".join(page_where)
                        + " ORDER BY event_date,station_id,stage,decision_at_utc,fact_id"
                        + " LIMIT ?"
                    )
                    cursor = conn.execute(
                        query,
                        [MAX_FEATURE_JSON_BYTES, *page_params, max_rows],
                    )
                    page = cursor.fetchall()
                    counts["fact_rows_read"] += len(page)
                    if counts["fact_rows_read"] > MAX_TOTAL_SCORECARD_ROWS:
                        raise ForwardScorecardError(
                            "forward_scorecard_fact_rows_limit_exceeded"
                        )
                    if not page:
                        return
                    page_rows = [_row_dict(cursor, raw) for raw in page]
                    retained_json_bytes += sum(
                        int(row.get("feature_bytes") or 0)
                        for row in page_rows
                        if isinstance(row.get("feature_bytes"), (int, float))
                        and 0 <= int(row.get("feature_bytes") or 0)
                        <= MAX_FEATURE_JSON_BYTES
                    )
                    if retained_json_bytes > MAX_TOTAL_SCORECARD_JSON_BYTES:
                        raise ForwardScorecardError(
                            "forward_scorecard_json_bytes_limit_exceeded"
                        )
                    for page_row in page_rows:
                        yield page_row
                    if len(page_rows) < max_rows:
                        return
                    tail = page_rows[-1]
                    last_key = (
                        str(tail.get("event_date") or ""),
                        str(tail.get("station_id") or ""),
                        str(tail.get("stage") or ""),
                        str(tail.get("decision_at_utc") or ""),
                        str(tail.get("fact_id") or ""),
                    )

            for row in _fact_pages():
                fact_id = str(row.get("fact_id") or "")
                if not fact_id:
                    blockers.add("fact_id_missing")
                    continue
                # This scorecard is the formal DeltaBasketScore v2 authority.
                # Older ``sdfact_`` rows remain immutable audit evidence, but
                # their content address was built under a retired schema and
                # must not be reinterpreted as FACT_SCHEMA v2 evidence.
                if not fact_id.startswith("d2fact_"):
                    counts["retired_non_v2_fact_rows_excluded"] += 1
                    continue
                try:
                    timestamps = [
                        _as_utc(str(row.get("decision_at_utc") or ""), name="decision_at_utc")[1],
                        _as_utc(str(row.get("created_at_utc") or ""), name="created_at_utc")[1],
                    ]
                    if "captured_at_utc" in row:
                        timestamps.append(
                            _as_utc(str(row.get("captured_at_utc") or ""), name="captured_at_utc")[1]
                        )
                except ForwardScorecardError:
                    blockers.add("fact_timestamp_invalid")
                    continue
                if any(value > as_of_dt for value in timestamps):
                    blockers.add("future_fact_excluded")
                    continue
                feature, feature_error = _bounded_json(
                    row.get("feature_json"), byte_length=row.get("feature_bytes")
                )
                evidence = {
                    "vector_count": 0,
                    "valid_vectors": 0,
                    "invalid_vectors": 0,
                    "exact_vector_coverage": 0,
                    "no_execution_policy_coverage": 0,
                    "version_evidence_coverage": 0,
                    "error": "feature_not_decoded",
                }
                phase = "UNKNOWN"
                boundary_error = "feature_not_decoded"
                hash_error = None
                if feature is not None:
                    phase = _phase(feature)
                    evidence = _feature_evidence(
                        feature, intent_count=row.get("intent_count")
                    )
                    boundary_error = _no_live_boundary(feature)
                    hash_error = _content_hash_status(
                        row,
                        schema=FACT_SCHEMA,
                        hash_fields=_FACT_HASH_FIELDS,
                        id_prefix="d2fact_",
                    )
                else:
                    feature_error = feature_error or "feature_not_decoded"
                    hash_error = "content_sha256_unverifiable_feature"
                if feature_error:
                    blockers.add("malformed_feature_json:" + feature_error)
                    counts["malformed_feature_facts"] += 1
                if evidence["error"]:
                    blockers.add(str(evidence["error"]))
                if boundary_error:
                    blockers.add(boundary_error)
                if hash_error:
                    blockers.add("fact_" + hash_error)
                facts.append(
                    {
                        "fact_id": fact_id,
                        "station_id": str(row.get("station_id") or "UNKNOWN"),
                        "event_date": str(row.get("event_date") or ""),
                        "settlement_timezone": str(
                            row.get("settlement_timezone") or ""
                        ),
                        "decision_at_utc": str(
                            row.get("decision_at_utc") or ""
                        ),
                        "ws_snapshot_id": str(row.get("ws_snapshot_id") or ""),
                        "quote_sha256": str(row.get("quote_sha256") or ""),
                        "partition_key": str(
                            row.get("partition_key") or ""
                        ),
                        "content_sha256": str(
                            row.get("content_sha256") or ""
                        ),
                        "stage": str(row.get("stage") or "UNKNOWN"),
                        "phase": phase,
                        "feature": feature,
                        "vector_count": evidence["vector_count"],
                        "valid_vectors": evidence["valid_vectors"],
                        "invalid_vectors": evidence["invalid_vectors"],
                        "exact_vector_coverage": evidence["exact_vector_coverage"],
                        "no_execution_policy_coverage": evidence["no_execution_policy_coverage"],
                        "version_evidence_coverage": evidence["version_evidence_coverage"],
                        "fact_content_hash_valid": hash_error is None,
                    }
                )
    counts["fact_rows_evaluated"] = len(facts)
    if not facts:
        blockers.add("no_training_facts")

    if not _table_exists(conn, "basket_policy_training_label"):
        blockers.add("training_label_table_missing")
    elif facts:
        label_columns = _table_columns(conn, "basket_policy_training_label")
        label_base = {
            "label_id", "fact_id", "truth_revision_no", "labeled_at_utc", "outcome_json"
        }
        missing_label_base = sorted(label_base - label_columns)
        if missing_label_base:
            blockers.add(
                "training_label_schema_columns_missing:" + ",".join(missing_label_base)
            )
        else:
            fact_ids = tuple(row["fact_id"] for row in facts)
            placeholders = ",".join("?" for _ in fact_ids)
            where = [
                "fact_id IN (" + placeholders + ")",
                "(julianday(labeled_at_utc)<=julianday(?) OR julianday(labeled_at_utc) IS NULL)",
            ]
            params: list[Any] = [*fact_ids, as_of_utc]
            if "settlement_closed_at_utc" in label_columns:
                where.append(
                    "(julianday(settlement_closed_at_utc)<=julianday(?) "
                    "OR julianday(settlement_closed_at_utc) IS NULL)"
                )
                params.append(as_of_utc)
            else:
                blockers.add("training_label_settlement_closed_at_missing")
            select_fields = [
                "label_id", "fact_id", "truth_revision_no", "labeled_at_utc", "outcome_json"
            ]
            for field in _LABEL_HASH_FIELDS:
                if field in label_columns and field not in select_fields:
                    select_fields.append(field)
            if "settlement_closed_at_utc" in label_columns and "settlement_closed_at_utc" not in select_fields:
                select_fields.append("settlement_closed_at_utc")
            if "content_sha256" in label_columns:
                select_fields.append("content_sha256")
            projection: list[str] = []
            for field in select_fields:
                if field == "outcome_json":
                    projection.extend(
                        (
                            "length(CAST(outcome_json AS BLOB)) AS outcome_bytes",
                            "CASE WHEN length(CAST(outcome_json AS BLOB))<=? "
                            "THEN outcome_json ELSE NULL END AS outcome_json",
                        )
                    )
                else:
                    projection.append(field)
            def _label_pages() -> Iterable[dict[str, Any]]:
                nonlocal retained_json_bytes
                # SQLite's variable limit is commonly 999; keep the scope
                # predicate below it while the page size remains the caller's
                # explicit memory bound.
                for offset in range(0, len(fact_ids), 500):
                    ids = fact_ids[offset:offset + 500]
                    id_placeholders = ",".join("?" for _ in ids)
                    base_where = [
                        "fact_id IN (" + id_placeholders + ")",
                        "(julianday(labeled_at_utc)<=julianday(?) "
                        "OR julianday(labeled_at_utc) IS NULL)",
                    ]
                    base_params: list[Any] = [*ids, as_of_utc]
                    if "settlement_closed_at_utc" in label_columns:
                        base_where.append(
                            "(julianday(settlement_closed_at_utc)<=julianday(?) "
                            "OR julianday(settlement_closed_at_utc) IS NULL)"
                        )
                        base_params.append(as_of_utc)
                    last_key: tuple[str, int, str, str] | None = None
                    while True:
                        page_where = list(base_where)
                        page_params = list(base_params)
                        if last_key is not None:
                            page_where.append(
                                "(fact_id>? OR (fact_id=? AND truth_revision_no<?) "
                                "OR (fact_id=? AND truth_revision_no=? "
                                "AND julianday(labeled_at_utc)<julianday(?)) "
                                "OR (fact_id=? AND truth_revision_no=? "
                                "AND julianday(labeled_at_utc)=julianday(?) "
                                "AND label_id>?))"
                            )
                            page_params.extend(
                                (
                                    last_key[0],
                                    last_key[0], last_key[1],
                                    last_key[0], last_key[1], last_key[2],
                                    last_key[0], last_key[1], last_key[2], last_key[3],
                                )
                            )
                        query = (
                            "SELECT " + ",".join(projection)
                            + " FROM basket_policy_training_label WHERE "
                            + " AND ".join(page_where)
                            + " ORDER BY fact_id,truth_revision_no DESC,"
                            "julianday(labeled_at_utc) DESC,label_id DESC LIMIT ?"
                        )
                        cursor = conn.execute(
                            query,
                            [MAX_FEATURE_JSON_BYTES, *page_params, max_rows],
                        )
                        page = cursor.fetchall()
                        counts["label_rows_read"] += len(page)
                        if counts["label_rows_read"] > MAX_TOTAL_SCORECARD_ROWS:
                            raise ForwardScorecardError(
                                "forward_scorecard_label_rows_limit_exceeded"
                            )
                        if not page:
                            break
                        page_rows = [_row_dict(cursor, raw) for raw in page]
                        retained_json_bytes += sum(
                            int(row.get("outcome_bytes") or 0)
                            for row in page_rows
                            if isinstance(
                                row.get("outcome_bytes"), (int, float)
                            )
                            and 0 <= int(row.get("outcome_bytes") or 0)
                            <= MAX_FEATURE_JSON_BYTES
                        )
                        if (
                            retained_json_bytes
                            > MAX_TOTAL_SCORECARD_JSON_BYTES
                        ):
                            raise ForwardScorecardError(
                                "forward_scorecard_json_bytes_limit_exceeded"
                            )
                        for page_row in page_rows:
                            yield page_row
                        if len(page_rows) < max_rows:
                            break
                        tail = page_rows[-1]
                        try:
                            revision = int(tail.get("truth_revision_no"))
                        except (TypeError, ValueError):
                            revision = -1
                        last_key = (
                            str(tail.get("fact_id") or ""),
                            revision,
                            str(tail.get("labeled_at_utc") or ""),
                            str(tail.get("label_id") or ""),
                        )

            for row in _label_pages():
                fact_id = str(row.get("fact_id") or "")
                if fact_id not in fact_ids:
                    blockers.add("label_fact_scope_mismatch")
                    counts["invalid_label_revisions"] += 1
                    continue
                invalid_reason: str | None = None
                try:
                    revision = int(row.get("truth_revision_no"))
                    if revision < 0:
                        invalid_reason = "truth_revision_invalid"
                    label_dt = _as_utc(
                        str(row.get("labeled_at_utc") or ""),
                        name="labeled_at_utc",
                    )[1]
                    if label_dt > as_of_dt:
                        invalid_reason = "future_label_excluded"
                    if "settlement_closed_at_utc" in row:
                        closed_dt = _as_utc(
                            str(row.get("settlement_closed_at_utc") or ""),
                            name="settlement_closed_at_utc",
                        )[1]
                        if closed_dt > as_of_dt:
                            invalid_reason = "future_settlement_label_excluded"
                except (ForwardScorecardError, TypeError, ValueError):
                    invalid_reason = invalid_reason or "label_timestamp_or_revision_invalid"
                outcome, error = _bounded_json(
                    row.get("outcome_json"), byte_length=row.get("outcome_bytes")
                )
                if error:
                    invalid_reason = invalid_reason or "outcome_json_" + error
                hash_error = _content_hash_status(
                    row,
                    schema=LABEL_SCHEMA,
                    hash_fields=_LABEL_HASH_FIELDS,
                    id_prefix="d2label_",
                )
                if hash_error:
                    invalid_reason = invalid_reason or hash_error
                if invalid_reason:
                    counts["invalid_label_revisions"] += 1
                    blockers.add("invalid_label_revision_skipped:" + invalid_reason)
                    continue
                if fact_id in labels:
                    # Rows are ordered newest-first.  The first valid row is
                    # therefore the latest valid revision; older valid rows
                    # are retained only in the source table, not double-counted.
                    continue
                labels[fact_id] = {
                    "label_id": str(row.get("label_id") or ""),
                    "truth_revision_no": int(row["truth_revision_no"]),
                    "winning_bucket": str(row.get("winning_bucket") or ""),
                    "settlement_closed_at_utc": str(
                        row.get("settlement_closed_at_utc") or ""
                    ),
                    "labeled_at_utc": str(
                        row.get("labeled_at_utc") or ""
                    ),
                    "outcome": outcome,
                    "outcome_error": None,
                }
                counts["valid_label_revisions"] += 1
    counts["labeled_facts"] = sum(1 for fact in facts if fact["fact_id"] in labels)
    counts["unlabeled_facts"] = len(facts) - counts["labeled_facts"]
    if counts["unlabeled_facts"]:
        blockers.add("unlabeled_training_facts")
    _scope_rows(counts, facts, labels)

    semantics = _label_semantics(labels)
    blockers.update(semantics["blockers"])
    no_live_fill_isolation = {
        "source": "basket_policy_training_label.outcome_json",
        "external_fill_tables_read": False,
        "requested_hypothetical_intents": semantics[
            "requested_hypothetical_intents_not_fills"
        ],
        "actual_fill_evidence": semantics["actual_fill_evidence"],
        "no_live_boundary_violations": semantics["no_live_boundary_violations"],
        "isolated": (
            semantics["actual_fill_evidence"] == 0
            and semantics["no_live_boundary_violations"] == 0
        ),
    }
    vector_totals = {
        "facts_with_feature_json": sum(1 for fact in facts if fact["feature"] is not None),
        "facts_with_exact_vector": sum(1 for fact in facts if fact["exact_vector_coverage"] == 1),
        "facts_with_no_execution_policy": sum(
            1 for fact in facts if fact["no_execution_policy_coverage"] == 1
        ),
        "facts_with_version_evidence": sum(
            1 for fact in facts if fact["version_evidence_coverage"] == 1
        ),
        "vector_count": sum(int(fact["vector_count"]) for fact in facts),
        "valid_vector_count": sum(int(fact["valid_vectors"]) for fact in facts),
        "invalid_vector_count": sum(int(fact["invalid_vectors"]) for fact in facts),
        "exact_vector_coverage_ratio": (
            sum(int(fact["exact_vector_coverage"]) for fact in facts) / len(facts)
            if facts else 0.0
        ),
        # Backward-compatible name for consumers that used the original
        # exact-vector ratio; the broader version-evidence ratio is explicit
        # above and includes validated NO_EXECUTION rows.
        "coverage_ratio": (
            sum(int(fact["exact_vector_coverage"]) for fact in facts) / len(facts)
            if facts else 0.0
        ),
        "version_evidence_coverage_ratio": (
            sum(int(fact["version_evidence_coverage"]) for fact in facts) / len(facts)
            if facts else 0.0
        ),
        "required_fields": list(_REQUIRED_VECTOR_FIELDS),
    }
    fact_sha_coverage = {
        "facts_with_valid_content_sha256": sum(
            1 for fact in facts if fact["fact_content_hash_valid"]
        ),
        "fact_count": len(facts),
        "coverage_ratio": (
            sum(1 for fact in facts if fact["fact_content_hash_valid"]) / len(facts)
            if facts else 0.0
        ),
    }
    cash = _cash_evidence(facts, semantics)
    station = _station_evidence(facts, allowlist)
    cash_observation_candidate = _cash_observation_adapter(facts, labels)
    station_policy_candidate = _station_policy_candidate(facts)
    maker = _maker_forward_evidence(
        conn,
        facts=facts,
        labels=labels,
        as_of_utc=as_of_utc,
        max_rows=max_rows,
    )
    blockers.update(cash["blockers"])
    blockers.update(station["blockers"])
    if counts["malformed_feature_facts"]:
        blockers.add("feature_integrity_incomplete")
    if (
        vector_totals["invalid_vector_count"]
        or vector_totals["facts_with_version_evidence"] != len(facts)
    ):
        blockers.add("exact_vector_coverage_incomplete")
    if fact_sha_coverage["facts_with_valid_content_sha256"] != len(facts):
        blockers.add("fact_content_sha_coverage_incomplete")
    cash_blockers = set(cash["blockers"])
    station_blockers = set(station["blockers"])
    global_blockers = set(blockers) - cash_blockers - station_blockers

    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "as_of_utc": as_of_utc,
        "station_allowlist": list(allowlist) if allowlist is not None else None,
        "event_date_allowlist": (
            list(event_dates) if event_dates is not None else None
        ),
        "max_rows": max_rows,
        "production_no_live": True,
        "live_order_authorized": False,
        "writes_production_db": False,
        "read_only": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "counts": counts,
        "requested_semantics": semantics,
        "no_live_fill_isolation": no_live_fill_isolation,
        "exact_vector_coverage": vector_totals,
        "fact_sha_coverage": fact_sha_coverage,
        "cash_phase_evidence": cash,
        "cash_observation_candidate": cash_observation_candidate,
        "station_policy_evidence": station,
        "station_policy_candidate": station_policy_candidate,
        "maker_forward_evidence": maker,
        "global_blockers": sorted(global_blockers),
        "blockers": sorted(set(blockers)),
    }
    receipt["ready_for_cash_scorecard"] = bool(
        cash["structurally_complete"] and not cash_blockers and not global_blockers
    )
    receipt["ready_for_station_scorecard"] = bool(
        station["structurally_complete"]
        and not station_blockers
        and not global_blockers
    )
    receipt["ready_for_maker_no_live_scorecard"] = bool(
        maker["ready_for_no_live_forward_scorecard"]
    )
    receipt["ready_for_maker_formal_promotion"] = False
    # Hash only the immutable receipt body.  A caller cannot alter a field and
    # retain the prior identity without changing this digest.
    receipt["content_sha256"] = content_sha256(receipt)
    return receipt


build_forward_scorecard_receipt = build_forward_scorecard


def build_cash_phase_forward_scorecard(
    observations: Iterable[Mapping[str, Any]],
    *,
    as_of_utc: str,
    policy_version: str,
    policy_promoted: bool = False,
    station_id: str | None = None,
    max_rows: int = MAX_ROWS,
) -> dict[str, Any]:
    """Expose the target-owned cash OOS builder beside DB scorecards.

    The database-backed forward receipt above remains the strict evidence
    projection.  This companion is the pure cash-phase attribution producer;
    it shares the same as-of/no-live/content-addressed contract and never
    reads an external fill source.
    """

    return build_cash_phase_oos_scorecard(
        observations,
        as_of_utc=as_of_utc,
        policy_version=policy_version,
        policy_promoted=policy_promoted,
        expected_station_id=station_id,
        max_rows=max_rows,
    )


__all__ = [
    "ForwardScorecardError",
    "MAKER_FORWARD_SCHEMA",
    "MAX_FEATURE_JSON_BYTES",
    "MAX_TOTAL_SCORECARD_JSON_BYTES",
    "MAX_TOTAL_SCORECARD_ROWS",
    "MAX_ROWS",
    "REQUESTED_INTENT_SEMANTICS",
    "SCHEMA",
    "build_forward_scorecard",
    "build_forward_scorecard_receipt",
    "build_cash_phase_forward_scorecard",
    "assert_cash_phase_oos_promotable",
]
