"""Read-only gate from the RKPK vertical slice to canonical 45-station no-live."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, canonical_json, content_sha256
from .station_registry import coordinate_registry_manifest


SCHEMA = "bayes_basket_target_45station_expansion_gate_v1"
MAX_PACKET_BYTES = 16 * 1024 * 1024
MAX_RKPK_GATE_FACTS = 512
EXPECTED_POLICY_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"
HISTORICAL_STRICT_OOS_ROLE = "historical_strict_oos"
OBSERVATIONAL_CANDIDATE_ROLE = "observational_candidate_not_oos"


def _fact_evidence_role(details_json: Any, event_date: Any) -> str:
    """Classify a training fact without promoting future observations.

    New rows must carry the explicit role; a future/observational row never
    enters strict-OOS label counts.  A legacy forward marker alone is not
    enough to infer historical OOS: forward packets remain observational even
    after their target date has passed.
    """

    try:
        details = json.loads(str(details_json or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        details = {}
    if not isinstance(details, Mapping):
        details = {}
    role = str(
        details.get("evidence_role")
        or details.get("oos_evidence_role")
        or details.get("future_observation_evidence_role")
        or ""
    ).strip()
    try:
        event = datetime.fromisoformat(str(event_date)[:10]).date()
    except (TypeError, ValueError):
        event = None
    today = datetime.now(timezone.utc).date()
    # Even a stale/mis-bound role claim cannot turn an open/current/future
    # event into an OOS sample.  A same-day row needs explicit closed-day and
    # strict-as-of proof; otherwise it remains observational.
    if event is None or (
        event >= today
        and not (
            details.get("closed_day") is True
            and details.get("strict_asof") is True
        )
    ):
        return OBSERVATIONAL_CANDIDATE_ROLE
    if role == HISTORICAL_STRICT_OOS_ROLE:
        return role
    if role == OBSERVATIONAL_CANDIDATE_ROLE or details.get(
        "future_event_not_oos"
    ) is True:
        return OBSERVATIONAL_CANDIDATE_ROLE
    # ``forward_observation_status=ELIGIBLE`` is deliberately *not* a
    # historical-OOS signal.  Older receipts without an explicit role stay
    # observational until an independent closed-day strict-as-of artifact is
    # bound.
    return OBSERVATIONAL_CANDIDATE_ROLE


def _table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _count(connection: sqlite3.Connection, table: str) -> int:
    if not _table(connection, table):
        return 0
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table(connection, table):
        return False
    return any(
        str(row[1]) == column
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _station_ids(
    connection: sqlite3.Connection,
    table: str,
) -> set[str]:
    if not _table(connection, table) or not _column(
        connection, table, "station_id"
    ):
        return set()
    return {
        str(row[0] or "").strip().upper()
        for row in connection.execute(
            f"SELECT DISTINCT station_id FROM {table} "
            "WHERE station_id IS NOT NULL AND station_id<>''"
        ).fetchall()
        if str(row[0] or "").strip()
    }


def evaluate_45station_no_live_expansion(
    ssot: sqlite3.Connection,
    forward_store: sqlite3.Connection,
    candidate_packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate the no-live vertical gate without using forward OOS.

    Forward/next-day rows may be read for bounded observational telemetry, but
    they are never used as a production expansion blocker or strict-OOS label
    source.
    """

    if not isinstance(candidate_packet, Mapping):
        raise ContractError("batch_expansion_candidate_packet_mapping_required")
    packet = dict(candidate_packet)
    if len(canonical_json(packet).encode("utf-8")) > MAX_PACKET_BYTES:
        raise ContractError("batch_expansion_candidate_packet_bytes_exceeded")
    supplied_sha = str(packet.get("packet_sha256") or "").strip().lower()
    identity = {key: value for key, value in packet.items() if key != "packet_sha256"}
    if supplied_sha != content_sha256(identity):
        raise ContractError("batch_expansion_candidate_packet_sha256_mismatch")
    expected_stations = tuple(coordinate_registry_manifest()["station_ids"])
    manifests = packet.get("cash_phase_authority_manifests")
    cash_policies = packet.get("cash_phase_policies")
    policy_artifacts = packet.get("policy_artifacts")
    policy_activations = packet.get("policy_activations")
    global_priors = packet.get("global_priors")
    if (
        not isinstance(manifests, list)
        or not isinstance(cash_policies, list)
        or not isinstance(policy_artifacts, list)
        or not isinstance(policy_activations, list)
        or not isinstance(global_priors, list)
    ):
        raise ContractError("batch_expansion_cash_policy_rows_required")
    manifest_stations = tuple(
        sorted(
            str(item.get("station_id") or "").strip().upper()
            for item in manifests
            if isinstance(item, Mapping)
        )
    )
    policy_stations = tuple(
        sorted(
            str(item.get("station_id") or "").strip().upper()
            for item in cash_policies
            if isinstance(item, Mapping)
        )
    )
    artifact_stations = tuple(
        sorted(
            {
                str(item.get("station_id") or "").strip().upper()
                for item in policy_artifacts
                if isinstance(item, Mapping)
            }
        )
    )
    activation_stations = tuple(
        sorted(
            {
                str(item.get("station_id") or "").strip().upper()
                for item in policy_activations
                if isinstance(item, Mapping)
            }
        )
    )
    blockers: list[str] = []
    settlement_scorecard_blockers: list[str] = []
    if (
        len(manifests) != 45
        or len(set(manifest_stations)) != 45
        or manifest_stations != expected_stations
    ):
        blockers.append("canonical_45station_authority_manifest_incomplete")
    if (
        len(cash_policies) != 45
        or len(set(policy_stations)) != 45
        or policy_stations != expected_stations
    ):
        blockers.append("canonical_45station_cash_policy_incomplete")
    artifact_count_by_station = {
        station: sum(
            1
            for item in policy_artifacts
            if isinstance(item, Mapping)
            and str(item.get("station_id") or "").strip().upper()
            == station
        )
        for station in expected_stations
    }
    if (
        len(policy_artifacts) != 135
        or artifact_stations != expected_stations
        or any(value != 3 for value in artifact_count_by_station.values())
    ):
        blockers.append("canonical_45station_policy_artifacts_incomplete")
    activation_stages_by_station = {
        station: {
            str(item.get("stage") or "").strip().upper()
            for item in policy_activations
            if isinstance(item, Mapping)
            and str(item.get("station_id") or "").strip().upper()
            == station
        }
        for station in expected_stations
    }
    if (
        len(policy_activations) != 135
        or activation_stations != expected_stations
        or any(
            stages != {"P0", "P0-M", "P1"}
            for stages in activation_stages_by_station.values()
        )
    ):
        blockers.append("canonical_45station_policy_activations_incomplete")
    artifact_ids = {
        str(item.get("artifact_id") or "").strip()
        for item in policy_artifacts
        if isinstance(item, Mapping)
    }
    global_prior_ids = {
        str(item.get("global_prior_id") or "").strip()
        for item in global_priors
        if isinstance(item, Mapping)
    }
    artifact_field_families = (
        ("target_artifact_id", "target"),
        ("reserve_artifact_id", "reserve"),
        ("cadence_artifact_id", "cadence"),
    )
    if (
        not global_prior_ids
        or any(
            not isinstance(item, Mapping)
            or str(item.get("global_prior_id") or "").strip()
            not in global_prior_ids
            or any(
                (
                    str(item.get(field) or "").strip()
                    not in artifact_ids
                    and family
                    not in {
                        str(value or "").strip()
                        for value in item.get("fallback_families") or []
                    }
                )
                for field, family in artifact_field_families
            )
            for item in policy_activations
        )
    ):
        blockers.append("canonical_45station_policy_binding_invalid")
    if (
        packet.get("execution_mode") != "production_no_live"
        or packet.get("policy_authority_scope") != EXPECTED_POLICY_SCOPE
        or packet.get("promoted") is not False
        or packet.get("live_order_authorized") is not False
        or packet.get("live_submit_authorized") is not False
        or packet.get("places_orders") is not False
        or packet.get("calls_order_gateway") is not False
        or int(packet.get("orders_submitted") or 0) != 0
    ):
        blockers.append("canonical_45station_no_live_boundary_invalid")

    registry = ssot.execute(
        "SELECT COUNT(*),COUNT(DISTINCT settlement_timezone) "
        "FROM basket_target_market_registry "
        "WHERE station_id='RKPK' AND settlement_timezone='Asia/Seoul'"
    ).fetchone()
    if registry is None or int(registry[0]) <= 0 or int(registry[1]) != 1:
        blockers.append("rkpk_registry_timezone_not_materialized")
    all_facts = ssot.execute(
        "SELECT f.fact_id,f.decision_id,f.event_date,"
        "d.details_json FROM basket_policy_training_fact f "
        "JOIN auto_trade_decisions d ON d.decision_id=f.decision_id "
        "WHERE f.station_id='RKPK' AND f.fact_id LIKE 'd2fact_%' "
        "ORDER BY f.rowid LIMIT ?",
        (MAX_RKPK_GATE_FACTS + 1,),
    ).fetchall()
    fact_scope_exceeded = len(all_facts) > MAX_RKPK_GATE_FACTS
    if fact_scope_exceeded:
        blockers.append("rkpk_delta_v2_fact_scope_cap_exceeded")
        all_facts = all_facts[:MAX_RKPK_GATE_FACTS]
    # Forward/next-day observations are telemetry, not the source of the
    # expansion gate.  Keep the bounded d2fact inventory intact and classify
    # strict-OOS evidence separately; filtering the inventory by
    # ``forward_observation_status=ELIGIBLE`` made a future observation a
    # hidden hard prerequisite for production_no_live.
    facts = list(all_facts)
    fact_roles = {
        str(row[0]): _fact_evidence_role(row[3], row[2])
        for row in facts
    }
    historical_facts = [
        row for row in facts
        if fact_roles.get(str(row[0])) == HISTORICAL_STRICT_OOS_ROLE
    ]
    observational_facts = [
        row for row in facts
        if fact_roles.get(str(row[0])) == OBSERVATIONAL_CANDIDATE_ROLE
    ]
    fact_ids = tuple(str(row[0]) for row in facts)
    decision_ids = tuple(str(row[1]) for row in facts)
    historical_fact_ids = tuple(str(row[0]) for row in historical_facts)
    historical_decision_ids = tuple(str(row[1]) for row in historical_facts)
    event_dates = tuple(sorted({str(row[2]) for row in historical_facts}))
    if not facts:
        blockers.append("rkpk_delta_v2_training_fact_missing")
    elif not historical_fact_ids:
        # A populated forward/observational inventory is not an empty source,
        # but it is also not a ready historical strict-OOS scorecard.  Keep
        # this distinction out of the no-live expansion blockers while making
        # formal/live promotion visibly pending.
        settlement_scorecard_blockers = [
            "historical_strict_oos_evidence_missing"
        ]
    else:
        settlement_scorecard_blockers = []
    label_count = 0
    stale_label_count = 0
    exact_labels_by_fact: dict[str, tuple[str, int, str, str]] = {}
    truth_table_ready = (
        _table(ssot, "calibration_station_daily_high_truth")
        and all(
            _column(
                ssot,
                "calibration_station_daily_high_truth",
                column,
            )
            for column in (
                "truth_id",
                "station_id",
                "local_date",
                "revision_no",
                "timestamps_complete",
                "qc_status",
                "content_sha256",
            )
        )
    )
    label_table_ready = (
        _table(ssot, "basket_policy_training_label")
        and all(
            _column(ssot, "basket_policy_training_label", column)
            for column in (
                "label_id",
                "fact_id",
                "truth_id",
                "truth_revision_no",
                "truth_sha256",
            )
        )
    )
    for fact_id, _, event_date, _ in historical_facts:
        if not truth_table_ready or not label_table_ready:
            continue
        latest_truth_rows = ssot.execute(
            "SELECT truth_id,revision_no,timestamps_complete,qc_status,"
            "content_sha256 FROM calibration_station_daily_high_truth "
            "WHERE station_id='RKPK' AND local_date=? "
            "AND revision_no=("
            "SELECT MAX(t2.revision_no) "
            "FROM calibration_station_daily_high_truth t2 "
            "WHERE t2.station_id='RKPK' AND t2.local_date=?"
            ") ORDER BY truth_id LIMIT 2",
            (str(event_date), str(event_date)),
        ).fetchall()
        if len(latest_truth_rows) > 1:
            settlement_scorecard_blockers.append(
                "rkpk_settlement_truth_revision_ambiguous"
            )
            continue
        if not latest_truth_rows:
            continue
        truth_id, revision_no, timestamps_complete, qc_status, truth_sha = (
            latest_truth_rows[0]
        )
        if (
            str(qc_status or "") != "ACCEPTED"
            or int(timestamps_complete or 0) != 1
        ):
            settlement_scorecard_blockers.append(
                "rkpk_latest_settlement_truth_not_accepted"
            )
            continue
        exact_rows = ssot.execute(
            "SELECT label_id FROM basket_policy_training_label "
            "WHERE fact_id=? AND truth_id=? AND truth_revision_no=? "
            "AND truth_sha256=? ORDER BY label_id LIMIT 2",
            (
                str(fact_id),
                str(truth_id),
                int(revision_no),
                str(truth_sha),
            ),
        ).fetchall()
        if len(exact_rows) > 1:
            settlement_scorecard_blockers.append(
                "rkpk_settlement_label_revision_ambiguous"
            )
            continue
        if not exact_rows:
            if ssot.execute(
                "SELECT 1 FROM basket_policy_training_label "
                "WHERE fact_id=? LIMIT 1",
                (str(fact_id),),
            ).fetchone() is not None:
                stale_label_count += 1
            continue
        label_id = str(exact_rows[0][0])
        exact_labels_by_fact[str(fact_id)] = (
            label_id,
            int(revision_no),
            str(truth_id),
            str(truth_sha),
        )
        label_count += 1
    if historical_fact_ids and label_count != len(historical_fact_ids):
        settlement_scorecard_blockers.append(
            "rkpk_same_day_settlement_labels_incomplete"
        )

    observation_count = 0
    if facts and _table(forward_store, "forward_observation"):
        placeholders = ",".join("?" for _ in decision_ids)
        observation_count = int(
            forward_store.execute(
                "SELECT COUNT(DISTINCT decision_id) FROM forward_observation "
                f"WHERE decision_id IN ({placeholders})",
                decision_ids,
            ).fetchone()[0]
        )
    # Forward observations are observational candidates only.  A missing
    # row, late receipt or repaired quarantine is retained as telemetry and
    # never becomes a production_no_live/batch-expansion blocker.
    unresolved_quarantine = 0
    if _table(forward_store, "forward_ingest_state"):
        repair_clause = (
            "AND NOT EXISTS(SELECT 1 FROM forward_ingest_repair r "
            "WHERE r.decision_id=s.decision_id)"
            if _table(forward_store, "forward_ingest_repair")
            else ""
        )
        unresolved_quarantine = int(
            forward_store.execute(
                "SELECT COUNT(*) FROM forward_ingest_state s "
                f"WHERE s.state='QUARANTINED' {repair_clause}"
            ).fetchone()[0]
        )
    forward_label_count = 0
    forward_label_table_ready = (
        _table(forward_store, "forward_settlement_label")
        and all(
            _column(forward_store, "forward_settlement_label", column)
            for column in (
                "observation_key",
                "revision",
                "truth_id",
                "truth_sha256",
                "source_fact_id",
                "source_label_id",
            )
        )
    )
    if forward_label_table_ready:
        for fact_id, decision_id in zip(
            historical_fact_ids,
            historical_decision_ids,
            strict=True,
        ):
            exact_label = exact_labels_by_fact.get(fact_id)
            if exact_label is None:
                continue
            observation_rows = forward_store.execute(
                "SELECT observation_key FROM forward_observation "
                "WHERE decision_id=? ORDER BY observation_key LIMIT 2",
                (decision_id,),
            ).fetchall()
            if len(observation_rows) != 1:
                continue
            label_id, revision_no, truth_id, truth_sha = exact_label
            projected = forward_store.execute(
                "SELECT 1 FROM forward_settlement_label "
                "WHERE observation_key=? AND revision=? "
                "AND truth_id=? AND truth_sha256=? "
                "AND source_fact_id=? AND source_label_id=? LIMIT 1",
                (
                    str(observation_rows[0][0]),
                    revision_no,
                    truth_id,
                    truth_sha,
                    fact_id,
                    label_id,
                ),
            ).fetchone()
            if projected is not None:
                forward_label_count += 1
    if (
        historical_fact_ids
        and label_count == len(historical_fact_ids)
        and forward_label_count < len(historical_fact_ids)
    ):
        settlement_scorecard_blockers.append(
            "rkpk_forward_settlement_label_projection_incomplete"
        )

    intent_count = _count(ssot, "basket_target_order_intents")
    live_authorized_intent_count = 0
    if intent_count:
        if not _column(
            ssot,
            "basket_target_order_intents",
            "live_submit_authorized",
        ):
            # A populated legacy intent table cannot prove the no-live
            # boundary and therefore fails closed.
            live_authorized_intent_count = intent_count
        else:
            live_authorized_intent_count = int(
                ssot.execute(
                    "SELECT COUNT(*) FROM basket_target_order_intents "
                    "WHERE live_submit_authorized<>0"
                ).fetchone()[0]
            )
    order_counts = {
        "basket_target_order_intents": intent_count,
        "live_authorized_order_intents": live_authorized_intent_count,
        "basket_target_order_events": _count(
            ssot, "basket_target_order_events"
        ),
        "basket_target_order_reconciliations": _count(
            ssot, "basket_target_order_reconciliations"
        ),
    }
    if (
        order_counts["live_authorized_order_intents"]
        or order_counts["basket_target_order_events"]
        or order_counts["basket_target_order_reconciliations"]
    ):
        blockers.append("rkpk_no_live_order_boundary_not_zero")
    materialized_station_ids = {
        "registry": _station_ids(
            ssot, "basket_target_market_registry"
        ),
        "probability": _station_ids(
            ssot, "basket_probability_envelope"
        ),
        "preselection": _station_ids(
            ssot, "basket_p0_preselection_plan"
        ),
        "official_ws": _station_ids(
            ssot, "basket_official_ws_snapshot"
        ),
        "inventory": _station_ids(
            ssot, "basket_confirmed_inventory_snapshot"
        ),
        "order_state": _station_ids(
            ssot, "basket_order_state_snapshot"
        ),
        "training_fact": _station_ids(
            ssot, "basket_policy_training_fact"
        ),
        "decision": _station_ids(ssot, "auto_trade_decisions"),
    }
    canonical_station_set = set(expected_stations)
    vertical_chain_stations = set.intersection(
        canonical_station_set,
        materialized_station_ids["probability"],
        materialized_station_ids["official_ws"],
        materialized_station_ids["inventory"],
        materialized_station_ids["order_state"],
        materialized_station_ids["training_fact"],
        materialized_station_ids["decision"],
    )
    missing_probability_stations = sorted(
        canonical_station_set
        - materialized_station_ids["probability"]
    )
    return {
        "schema": SCHEMA,
        "state": "READY" if not blockers else "BLOCKED_FAIL_CLOSED",
        "blockers": sorted(set(blockers)),
        "station_count": len(expected_stations),
        "station_ids": list(expected_stations),
        # READY above authorizes expansion from the proven RKPK slice; it
        # must not be misreported as 45 stations already materialized.
        "resident_materialization_state": (
            "COMPLETE"
            if vertical_chain_stations == canonical_station_set
            else "PENDING"
        ),
        "resident_materialized_station_counts": {
            key: len(values & canonical_station_set)
            for key, values in sorted(materialized_station_ids.items())
        },
        "resident_vertical_chain_station_count": len(
            vertical_chain_stations
        ),
        "resident_vertical_chain_station_ids": sorted(
            vertical_chain_stations
        ),
        "resident_missing_probability_station_count": len(
            missing_probability_stations
        ),
        "resident_missing_probability_station_ids": (
            missing_probability_stations
        ),
        "resident_materialization_blocks_expansion_authorization": False,
        "candidate_packet_sha256": supplied_sha,
        "rkpk_event_dates": list(event_dates),
        "rkpk_delta_v2_total_fact_count": len(all_facts),
        "rkpk_delta_v2_fact_scope_cap": MAX_RKPK_GATE_FACTS,
        "rkpk_delta_v2_fact_scope_exceeded": fact_scope_exceeded,
        # Retain the legacy field name for receipt compatibility, but make its
        # meaning explicit: these are observational/non-strict rows excluded
        # from historical OOS labels, not rows dropped from the bounded fact
        # inventory or a production expansion blocker.
        "rkpk_delta_v2_ineligible_fact_count": len(observational_facts),
        "rkpk_delta_v2_fact_count": len(fact_ids),
        "rkpk_delta_v2_historical_strict_oos_fact_count": len(
            historical_fact_ids
        ),
        "rkpk_delta_v2_observational_candidate_not_oos_fact_count": len(
            observational_facts
        ),
        "rkpk_settlement_label_count": label_count,
        "rkpk_stale_settlement_label_count": stale_label_count,
        "rkpk_forward_observation_count": observation_count,
        "rkpk_forward_settlement_label_count": forward_label_count,
        "rkpk_unresolved_quarantine_count": unresolved_quarantine,
        "future_observation_evidence_role": OBSERVATIONAL_CANDIDATE_ROLE,
        "future_observation_is_oos": False,
        "future_observation_counts_as_strict_oos": False,
        "forward_observational_scorecard": {
            "evidence_role": OBSERVATIONAL_CANDIDATE_ROLE,
            "fact_count": len(observational_facts),
            "decision_count": len(decision_ids),
            "observation_count": observation_count,
            "unresolved_quarantine_count": unresolved_quarantine,
            "settlement_label_projection_count": forward_label_count,
            "blocks_production_no_live_or_batch_expansion": False,
            "blocks_live_or_parameter_promotion": False,
        },
        "settlement_scorecard_state": (
            "READY"
            if not settlement_scorecard_blockers
            else "PENDING_OR_BLOCKED"
        ),
        "settlement_scorecard_blockers": sorted(
            set(settlement_scorecard_blockers)
        ),
        "settlement_labels_block_production_no_live_or_batch_expansion": (
            False
        ),
        "settlement_labels_block_live_or_parameter_promotion": True,
        "order_counts": order_counts,
        # Legacy keys remain for readers that still deserialize the previous
        # receipt.  They are explicitly non-blocking: forward data cannot be
        # an OOS gate.  The historical key below carries the real promotion
        # requirement.
        "forward_oos_consulted": False,
        "forward_oos_blocks_production_no_live_or_batch_expansion": False,
        "forward_oos_blocks_live_or_parameter_promotion": False,
        "historical_strict_oos_required_for_live_or_parameter_promotion": True,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "writes_production_db": False,
    }


__all__ = [
    "HISTORICAL_STRICT_OOS_ROLE",
    "MAX_RKPK_GATE_FACTS",
    "OBSERVATIONAL_CANDIDATE_ROLE",
    "SCHEMA",
    "_fact_evidence_role",
    "evaluate_45station_no_live_expansion",
]
