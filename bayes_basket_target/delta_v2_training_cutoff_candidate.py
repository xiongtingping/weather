"""Read-only target-owned training-cutoff candidates for ΔBasketScore v2.

The canonical forward scorecard deliberately refuses to infer a cash-policy
training cutoff from P1 probabilities, intents, or no-live outcomes.  This
module supplies the missing *evidence artifact*, built only from the target's
own settled ``d2fact_``/``d2label_`` joins.  It is not a runtime config and it
cannot activate or promote a cash policy.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .cash_phase_oos import DEFAULT_MIN_UNIQUE_EVENTS_PER_PHASE, PHASES
from .contracts import ContractError, content_sha256


SCHEMA = "delta_basket_score_v2_target_owned_training_cutoff_candidate_v1"
MAX_ROWS = 100_000
MAX_FEATURE_JSON_BYTES = 131_072
# Keep the complete canonical station lineage in one bounded candidate while
# retaining the per-feature and process-RSS limits.
MAX_TOTAL_FEATURE_JSON_BYTES = 128 * 1024 * 1024


def _utc(value: Any, name: str) -> tuple[str, datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"delta_v2_cutoff_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"delta_v2_cutoff_{name}_timezone_required")
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _feature_phase(raw: Any, byte_length: Any) -> tuple[str, str | None]:
    """Read only the persisted phase; unknown is not silently guessed."""

    try:
        size = int(byte_length)
    except (TypeError, ValueError):
        return "UNKNOWN", "feature_json_size_invalid"
    if size < 0 or size > MAX_FEATURE_JSON_BYTES or not isinstance(raw, str):
        return "UNKNOWN", "feature_json_unavailable_or_oversized"
    if len(raw.encode("utf-8")) != size:
        return "UNKNOWN", "feature_json_size_mismatch"
    try:
        feature = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "UNKNOWN", "feature_json_malformed"
    if not isinstance(feature, Mapping):
        return "UNKNOWN", "feature_json_not_mapping"
    decision = feature.get("cash_phase_decision")
    if not isinstance(decision, Mapping):
        return "UNKNOWN", "cash_phase_decision_missing"
    phase = str(decision.get("next_phase") or "").strip().upper()
    if phase not in PHASES:
        return "UNKNOWN", "cash_phase_invalid"
    return phase, None


def _latest_settled_rows(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Read the latest frozen label per d2 fact under fixed resource bounds."""

    as_of, _ = _utc(as_of_utc, "as_of_utc")
    counters: Counter[str] = Counter()
    required_facts = {
        "fact_id", "station_id", "event_date", "decision_at_utc",
        "stage", "feature_json", "created_at_utc", "content_sha256",
    }
    required_labels = {
        "label_id", "fact_id", "truth_revision_no", "truth_sha256",
        "settlement_closed_at_utc", "labeled_at_utc", "content_sha256",
    }
    fact_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(basket_policy_training_fact)")
    }
    label_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(basket_policy_training_label)")
    }
    if not required_facts <= fact_columns or not required_labels <= label_columns:
        counters["canonical_fact_or_label_schema_missing"] += 1
        return [], counters

    cursor = conn.execute(
        "SELECT f.fact_id,f.station_id,f.event_date,f.decision_at_utc,f.stage,"
        "length(CAST(f.feature_json AS BLOB)) AS feature_bytes,"
        "CASE WHEN length(CAST(f.feature_json AS BLOB))<=? THEN f.feature_json ELSE NULL END AS feature_json,"
        "f.created_at_utc,f.content_sha256,"
        "l.label_id,l.truth_revision_no,l.truth_sha256,l.settlement_closed_at_utc,"
        "l.labeled_at_utc,l.content_sha256 "
        "FROM basket_policy_training_fact f "
        "JOIN basket_policy_training_label l ON l.fact_id=f.fact_id "
        "WHERE f.fact_id LIKE 'd2fact_%' AND f.stage='P1' "
        "AND julianday(f.decision_at_utc)<=julianday(?) "
        "AND julianday(f.created_at_utc)<=julianday(?) "
        "AND julianday(l.settlement_closed_at_utc)<=julianday(?) "
        "AND julianday(l.labeled_at_utc)<=julianday(?) "
        "ORDER BY f.fact_id,l.truth_revision_no DESC,l.labeled_at_utc DESC,l.label_id DESC",
        (MAX_FEATURE_JSON_BYTES, as_of, as_of, as_of, as_of),
    )
    selected: dict[str, dict[str, Any]] = {}
    retained_bytes = 0
    for row in cursor:
        counters["raw_join_rows"] += 1
        if counters["raw_join_rows"] > MAX_ROWS:
            raise ContractError("delta_v2_cutoff_join_row_limit_exceeded")
        fact_id = str(row[0] or "")
        if fact_id in selected:
            continue
        try:
            decision_at, decision_dt = _utc(row[3], "decision_at_utc")
            closed_at, closed_dt = _utc(row[12], "settlement_closed_at_utc")
            labeled_at, _ = _utc(row[13], "labeled_at_utc")
        except ContractError:
            counters["join_timestamp_invalid"] += 1
            continue
        fact_sha = str(row[8] or "")
        label_sha = str(row[14] or "")
        truth_sha = str(row[11] or "")
        if any(len(value) != 64 for value in (fact_sha, label_sha, truth_sha)):
            counters["join_content_address_incomplete"] += 1
            continue
        phase, phase_error = _feature_phase(row[6], row[5])
        if row[6] is not None:
            retained_bytes += int(row[5] or 0)
            if retained_bytes > MAX_TOTAL_FEATURE_JSON_BYTES:
                raise ContractError("delta_v2_cutoff_feature_bytes_limit_exceeded")
        if phase_error:
            counters[phase_error] += 1
        selected[fact_id] = {
            "fact_id": fact_id,
            "station_id": str(row[1] or "").strip().upper(),
            "event_date": str(row[2] or "").strip(),
            "decision_at_utc": decision_at,
            "decision_dt": decision_dt,
            "settlement_closed_at_utc": closed_at,
            "settlement_closed_dt": closed_dt,
            "labeled_at_utc": labeled_at,
            "phase": phase,
            "fact_content_sha256": fact_sha,
            "label_content_sha256": label_sha,
            "truth_sha256": truth_sha,
        }
    counters["latest_frozen_join_count"] = len(selected)
    return sorted(
        selected.values(),
        key=lambda item: (item["station_id"], item["event_date"], item["decision_dt"], item["fact_id"]),
    ), counters


def _station_candidate(
    station: str,
    rows: list[dict[str, Any]],
    *,
    min_unique_events_per_phase: int,
) -> dict[str, Any]:
    """Choose a causal train/evaluation split without splitting an event day."""

    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["event_date"]:
            by_event[row["event_date"]].append(row)
    event_days = sorted(by_event)
    possible: list[dict[str, Any]] = []
    for event_date in event_days:
        train_event_rows = by_event[event_date]
        cutoff_dt = max(row["settlement_closed_dt"] for row in train_event_rows)
        train_events = {
            date for date, values in by_event.items()
            if all(
                row["decision_dt"] < cutoff_dt
                and row["settlement_closed_dt"] <= cutoff_dt
                for row in values
            )
        }
        evaluation_events = {
            date for date, values in by_event.items()
            if all(row["decision_dt"] >= cutoff_dt for row in values)
        }
        # An event that crosses the cutoff could leak its same-day state and
        # therefore is excluded from both sides.
        overlap = train_events & evaluation_events
        train_events -= overlap
        evaluation_events -= overlap
        if not train_events or not evaluation_events:
            continue
        train_rows = [row for date in train_events for row in by_event[date]]
        evaluation_rows = [row for date in evaluation_events for row in by_event[date]]
        evaluation_start = min(row["decision_dt"] for row in evaluation_rows)
        if cutoff_dt > evaluation_start:
            continue
        phase_events = {
            phase: len({row["event_date"] for row in evaluation_rows if row["phase"] == phase})
            for phase in PHASES
        }
        possible.append({
            "training_cutoff_dt": cutoff_dt,
            "train_events": train_events,
            "evaluation_events": evaluation_events,
            "train_rows": train_rows,
            "evaluation_rows": evaluation_rows,
            "evaluation_start_dt": evaluation_start,
            "phase_unique_events": phase_events,
        })
    if not possible:
        return {
            "station_id": station,
            "state": "ACCUMULATING",
            "cutoff_available": False,
            "training_event_days": len(event_days),
            "evaluation_event_days": 0,
            "blockers": ["causal_train_evaluation_event_day_split_missing"],
            "policy_training_cutoff_utc": None,
            "evaluation_start_utc": None,
            "evaluation_end_utc": None,
            "phase_unique_event_counts": {phase: 0 for phase in PHASES},
            "phase_unique_event_deficits": {
                phase: min_unique_events_per_phase for phase in PHASES
            },
        }
    # Prefer the split with the largest balanced event-day evidence, then the
    # largest evaluation row count.  The final stable tiebreaker is the
    # latest cutoff; all three are recorded in the content-addressed result.
    selected = max(
        possible,
        key=lambda item: (
            min(len(item["train_events"]), len(item["evaluation_events"])),
            len(item["evaluation_rows"]),
            item["training_cutoff_dt"],
        ),
    )
    cutoff = selected["training_cutoff_dt"].isoformat().replace("+00:00", "Z")
    evaluation_start = selected["evaluation_start_dt"].isoformat().replace("+00:00", "Z")
    evaluation_end = (
        max(row["decision_dt"] for row in selected["evaluation_rows"])
    ).isoformat().replace("+00:00", "Z")
    phase_counts = selected["phase_unique_events"]
    deficits = {
        phase: max(0, min_unique_events_per_phase - phase_counts[phase])
        for phase in PHASES
    }
    blockers = []
    if any(deficits.values()):
        blockers.append("cash_phase_evaluation_unique_event_threshold_not_met")
    return {
        "station_id": station,
        "state": "ACCUMULATING" if blockers else "CANDIDATE_READY_NO_LIVE",
        "cutoff_available": True,
        "policy_training_cutoff_utc": cutoff,
        "evaluation_start_utc": evaluation_start,
        "evaluation_end_utc": evaluation_end,
        "training_event_days": len(selected["train_events"]),
        "evaluation_event_days": len(selected["evaluation_events"]),
        "training_settled_join_count": len(selected["train_rows"]),
        "evaluation_settled_join_count": len(selected["evaluation_rows"]),
        "phase_unique_event_counts": phase_counts,
        "phase_unique_event_deficits": deficits,
        "train_label_closed_no_later_than_cutoff": all(
            row["settlement_closed_dt"] <= selected["training_cutoff_dt"]
            for row in selected["train_rows"]
        ),
        "evaluation_decision_not_before_cutoff": all(
            row["decision_dt"] >= selected["training_cutoff_dt"]
            for row in selected["evaluation_rows"]
        ),
        "blockers": blockers,
    }


def build_target_owned_training_cutoff_candidate(
    rows: Iterable[Mapping[str, Any]],
    *,
    as_of_utc: str,
    min_unique_events_per_phase: int = DEFAULT_MIN_UNIQUE_EVENTS_PER_PHASE,
) -> dict[str, Any]:
    """Build a no-live candidate from normalized target-owned settled joins."""

    as_of, _ = _utc(as_of_utc, "as_of_utc")
    if isinstance(min_unique_events_per_phase, bool) or min_unique_events_per_phase < 1:
        raise ContractError("delta_v2_cutoff_minimum_unique_events_invalid")
    materialized = list(rows)
    if len(materialized) > MAX_ROWS:
        raise ContractError("delta_v2_cutoff_join_row_limit_exceeded")
    station_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    blockers: set[str] = set()
    for row in materialized:
        if not isinstance(row, Mapping):
            raise ContractError("delta_v2_cutoff_join_mapping_required")
        station = str(row.get("station_id") or "").strip().upper()
        event_date = str(row.get("event_date") or "").strip()
        if not station or not event_date:
            blockers.add("settled_join_identity_incomplete")
            continue
        try:
            decision_text, decision_dt = _utc(row.get("decision_at_utc"), "decision_at_utc")
            closed_text, closed_dt = _utc(row.get("settlement_closed_at_utc"), "settlement_closed_at_utc")
        except ContractError:
            blockers.add("settled_join_timestamp_invalid")
            continue
        if closed_dt > _utc(as_of, "as_of_utc")[1]:
            blockers.add("settled_join_after_asof")
            continue
        phase = str(row.get("phase") or "UNKNOWN").upper()
        if phase not in PHASES:
            blockers.add("cash_phase_missing_or_invalid")
        station_rows[station].append({
            **dict(row),
            "decision_at_utc": decision_text,
            "decision_dt": decision_dt,
            "settlement_closed_at_utc": closed_text,
            "settlement_closed_dt": closed_dt,
            "phase": phase,
        })
    candidates = {
        station: _station_candidate(
            station,
            values,
            min_unique_events_per_phase=min_unique_events_per_phase,
        )
        for station, values in sorted(station_rows.items())
    }
    cutoffs = {
        station: detail["policy_training_cutoff_utc"]
        for station, detail in candidates.items()
        if detail["cutoff_available"] is True
        and isinstance(detail["policy_training_cutoff_utc"], str)
    }
    candidate_ready = [
        station for station, detail in candidates.items()
        if detail["state"] == "CANDIDATE_READY_NO_LIVE"
    ]
    if not cutoffs:
        blockers.add("no_causal_training_cutoff_available")
    if len(candidate_ready) != len(cutoffs):
        blockers.add("cash_phase_cutoff_sample_accumulating")
    identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of,
        "source": {
            "owner": "bayes_basket_target",
            "fact_table": "basket_policy_training_fact",
            "label_table": "basket_policy_training_label",
            "fact_contract": "d2fact_ P1 only",
            "label_contract": "latest frozen label as of artifact time",
            "settled_join_count": len(materialized),
        },
        "consumer_contract": {
            "forward_scorecard_behavior": "does_not_infer_training_cutoff_from_fact_label_pair",
            "cash_oos_consumer": "cash_phase_oos.build_cash_phase_oos_evidence",
            "required_field": "policy_training_cutoffs[station_id]",
            "cutoff_must_not_exceed_evaluation_start": True,
            "runtime_or_activation_authority": False,
        },
        "thresholds": {
            "minimum_unique_target_owned_events_per_phase": min_unique_events_per_phase,
            "required_phases": list(PHASES),
            "selection": "maximize balanced disjoint event-day train/evaluation split",
        },
        "policy_training_cutoffs": cutoffs,
        "station_candidates": candidates,
        "candidate_cutoff_station_count": len(cutoffs),
        "candidate_ready_station_count": len(candidate_ready),
        "state": "CANDIDATE_READY_NO_LIVE" if cutoffs and not blockers else "ACCUMULATING",
        "accumulation_blockers": sorted(blockers),
        "formal_promotion_allowed": False,
        "live_activation_allowed": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_from_connection(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    min_unique_events_per_phase: int = DEFAULT_MIN_UNIQUE_EVENTS_PER_PHASE,
) -> dict[str, Any]:
    rows, read_counts = _latest_settled_rows(conn, as_of_utc=as_of_utc)
    artifact = build_target_owned_training_cutoff_candidate(
        rows,
        as_of_utc=as_of_utc,
        min_unique_events_per_phase=min_unique_events_per_phase,
    )
    identity = {key: value for key, value in artifact.items() if key != "content_sha256"}
    identity["source"] = {**dict(identity["source"]), "read_counts": dict(sorted(read_counts.items()))}
    return {**identity, "content_sha256": content_sha256(identity)}


def assert_target_owned_training_cutoff_candidate(artifact: Mapping[str, Any]) -> None:
    if artifact.get("schema") != SCHEMA:
        raise ContractError("delta_v2_cutoff_schema_invalid")
    expected = content_sha256({key: value for key, value in artifact.items() if key != "content_sha256"})
    if artifact.get("content_sha256") != expected:
        raise ContractError("delta_v2_cutoff_content_sha_mismatch")
    if artifact.get("formal_promotion_allowed") is not False or artifact.get("live_activation_allowed") is not False:
        raise ContractError("delta_v2_cutoff_authority_forbidden")
    if artifact.get("writes_production_db") is not False or artifact.get("calls_order_gateway") is not False:
        raise ContractError("delta_v2_cutoff_write_or_gateway_forbidden")
    if artifact.get("orders_submitted") != 0:
        raise ContractError("delta_v2_cutoff_orders_forbidden")


__all__ = [
    "SCHEMA",
    "assert_target_owned_training_cutoff_candidate",
    "build_from_connection",
    "build_target_owned_training_cutoff_candidate",
]
