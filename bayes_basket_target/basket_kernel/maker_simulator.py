"""Deterministic zero-order Maker queue, cancel, and fill simulator."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    EPS,
    TargetBasketPolicy,
    TargetBasketSnapshot,
    BucketTarget,
    ContractError,
    ExecutionCandidate,
    MakerEventType,
    MakerExchangeStatus,
    MakerLifecycleState,
    TargetMakerCandidate,
    TargetMakerEvent,
    TargetMakerReceipt,
    content_sha256,
)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _append_reason(reasons: list[str], value: str) -> None:
    if value not in reasons:
        reasons.append(value)


def build_target_maker_candidate(
    snapshot: TargetBasketSnapshot,
    policy: TargetBasketPolicy,
    target: BucketTarget,
) -> TargetMakerCandidate:
    """Materialize a counterfactual Maker candidate, never an order or intent."""

    if target.execution_candidate is not ExecutionCandidate.MAKER:
        raise ContractError("maker_candidate_requires_maker_target")
    if target.clip_shares <= EPS or target.candidate_price <= EPS:
        raise ContractError("maker_candidate_clip_missing")
    if not any(row.bucket == target.bucket for row in snapshot.buckets):
        raise ContractError("maker_candidate_bucket_unknown")
    if not target.maker_queue_known:
        raise ContractError("maker_candidate_queue_unknown")
    core = {
        "schema": "target_target_maker_candidate_identity_v1",
        "snapshot_sha256": snapshot.content_sha256,
        "policy_sha256": content_sha256(policy.to_dict()),
        "bucket": target.bucket,
        "requested_shares": target.clip_shares,
        "limit_price": target.limit_price,
        "all_in_limit_cost_per_share": target.all_in_limit_cost_per_share,
        "maker_price": target.candidate_price,
        "effective_price": target.effective_price,
        "quote_version": snapshot.quote_version,
        "order_state_version": snapshot.order_state_version,
    }
    candidate_id = "maker_candidate_" + content_sha256(core)[:32]
    value = TargetMakerCandidate(
        candidate_id=candidate_id,
        market_id=snapshot.market_id,
        event_date=snapshot.event_date,
        bucket=target.bucket,
        created_at=snapshot.observed_at,
        requested_shares=target.clip_shares,
        limit_price=target.limit_price,
        all_in_limit_cost_per_share=target.all_in_limit_cost_per_share,
        maker_price=target.candidate_price,
        effective_price=target.effective_price,
        best_ask_at_creation=target.ask_price,
        initial_queue_ahead_shares=target.maker_queue_ahead_shares,
        timeout_seconds=(
            target.maker_timeout_seconds
            if target.maker_timeout_seconds > EPS
            else policy.maker_timeout_seconds
        ),
        quote_version=snapshot.quote_version,
        order_state_version=snapshot.order_state_version,
        lineage={
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_sha256": snapshot.content_sha256,
            "posterior_version": snapshot.evidence.posterior_version,
            "posterior_sha256": snapshot.evidence.posterior_sha256,
            "quote_version": snapshot.quote_version,
            "inventory_version": snapshot.inventory_version,
            "order_state_version": snapshot.order_state_version,
            "target_price_id": target.target_price_id,
            **(
                {
                    "maker_grid_artifact_sha256": (
                        target.maker_grid_artifact_sha256
                    ),
                    "maker_grid_artifact_curve_sha256": (
                        target.maker_grid_artifact_curve_sha256
                    ),
                    "maker_grid_point_sha256": (
                        target.maker_grid_point_sha256
                    ),
                    "maker_grid_curve_sha256": (
                        target.maker_grid_curve_sha256
                    ),
                }
                if target.maker_grid_artifact_sha256
                and target.maker_grid_artifact_curve_sha256
                and target.maker_grid_point_sha256
                and target.maker_grid_curve_sha256
                else {}
            ),
        },
    )
    return TargetMakerCandidate.from_mapping(value.to_dict())


def _known_state(
    *,
    filled: float,
    requested: float,
    cancel_requested: bool,
    cancel_acknowledged: bool,
    rejected: bool,
) -> MakerLifecycleState:
    if filled + EPS >= requested:
        return MakerLifecycleState.FILLED
    if rejected:
        return (
            MakerLifecycleState.RECONCILE_REQUIRED
            if filled > EPS
            else MakerLifecycleState.REJECTED
        )
    if cancel_acknowledged:
        return (
            MakerLifecycleState.CANCELLED_PARTIAL
            if filled > EPS
            else MakerLifecycleState.CANCELLED
        )
    if cancel_requested:
        return MakerLifecycleState.CANCEL_REQUESTED
    if filled > EPS:
        return MakerLifecycleState.PARTIALLY_FILLED
    return MakerLifecycleState.WORKING


def simulate_maker_lifecycle(
    candidate: TargetMakerCandidate | Mapping[str, Any],
    events: Iterable[TargetMakerEvent | Mapping[str, Any]] = (),
) -> TargetMakerReceipt:
    """Fold queue and authoritative events with duplicate/conflict protection."""

    maker = (
        candidate
        if isinstance(candidate, TargetMakerCandidate)
        else TargetMakerCandidate.from_mapping(candidate)
    )
    rows = [
        item if isinstance(item, TargetMakerEvent) else TargetMakerEvent.from_mapping(item)
        for item in events
    ]
    seen_ids: dict[str, str] = {}
    processed_ids: list[str] = []
    duplicate_count = 0
    last_sequence: int | None = None
    last_timestamp: datetime | None = _timestamp(maker.created_at)

    modeled_filled = 0.0
    authoritative_filled: float | None = None
    queue_ahead = maker.initial_queue_ahead_shares
    cancel_requested = False
    cancel_acknowledged = False
    rejected = False
    unknown = False
    state = MakerLifecycleState.WORKING
    reasons: list[str] = ["maker_waiting"]

    def effective_filled() -> float:
        return authoritative_filled if authoritative_filled is not None else modeled_filled

    for event in rows:
        if event.candidate_id != maker.candidate_id:
            raise ContractError("maker_event_candidate_mismatch")
        digest = event.content_sha256
        previous_digest = seen_ids.get(event.event_id)
        if previous_digest is not None:
            if previous_digest != digest:
                raise ContractError("maker_event_id_content_conflict")
            duplicate_count += 1
            continue
        seen_ids[event.event_id] = digest
        if last_sequence is not None and event.sequence <= last_sequence:
            raise ContractError("maker_event_sequence_not_strictly_increasing")
        observed = _timestamp(event.observed_at)
        if last_timestamp is not None and observed < last_timestamp:
            raise ContractError("maker_event_timestamp_moved_backwards")
        last_sequence = event.sequence
        last_timestamp = observed
        processed_ids.append(event.event_id)

        terminal = state in {
            MakerLifecycleState.CANCELLED,
            MakerLifecycleState.CANCELLED_PARTIAL,
            MakerLifecycleState.FILLED,
            MakerLifecycleState.REJECTED,
        }
        if event.event_type is MakerEventType.QUEUE_TRADE:
            if terminal or unknown:
                _append_reason(reasons, "queue_trade_after_terminal_or_unknown_ignored")
            else:
                consumed = min(queue_ahead, event.shares)
                queue_ahead = max(0.0, queue_ahead - consumed)
                available_for_candidate = max(0.0, event.shares - consumed)
                modeled_filled = min(
                    maker.requested_shares,
                    modeled_filled + available_for_candidate,
                )
                _append_reason(reasons, "queue_trade_applied")
                if authoritative_filled is None:
                    state = _known_state(
                        filled=modeled_filled,
                        requested=maker.requested_shares,
                        cancel_requested=cancel_requested,
                        cancel_acknowledged=cancel_acknowledged,
                        rejected=rejected,
                    )
        elif event.event_type is MakerEventType.QUEUE_SNAPSHOT:
            if terminal or unknown:
                _append_reason(reasons, "queue_snapshot_after_terminal_or_unknown_ignored")
            else:
                if event.queue_ahead_shares is None:
                    raise ContractError("maker_queue_snapshot_value_missing")
                if event.queue_ahead_shares > queue_ahead + EPS:
                    _append_reason(reasons, "queue_priority_worsened")
                queue_ahead = event.queue_ahead_shares
                _append_reason(reasons, "queue_snapshot_applied")
        elif event.event_type in {MakerEventType.TIMEOUT, MakerEventType.CANCEL_REQUEST}:
            if not terminal:
                cancel_requested = True
                state = MakerLifecycleState.CANCEL_REQUESTED
                _append_reason(
                    reasons,
                    "maker_timeout_cancel_requested"
                    if event.event_type is MakerEventType.TIMEOUT
                    else "maker_cancel_requested",
                )
        elif event.event_type is MakerEventType.CANCEL_ACK:
            cancel_requested = True
            cancel_acknowledged = True
            unknown = False
            state = _known_state(
                filled=effective_filled(),
                requested=maker.requested_shares,
                cancel_requested=cancel_requested,
                cancel_acknowledged=cancel_acknowledged,
                rejected=rejected,
            )
            _append_reason(reasons, "maker_cancel_acknowledged")
        elif event.event_type is MakerEventType.FILL_REPORT:
            cumulative = event.cumulative_filled_shares
            if cumulative is None:
                raise ContractError("maker_fill_report_cumulative_missing")
            if cumulative > maker.requested_shares + EPS:
                raise ContractError("maker_fill_report_exceeds_requested")
            if authoritative_filled is not None and cumulative + EPS < authoritative_filled:
                raise ContractError("maker_authoritative_fill_moved_backwards")
            if cancel_acknowledged and cumulative > effective_filled() + EPS:
                _append_reason(reasons, "late_fill_after_cancel_ack_reconciled")
            if abs(cumulative - modeled_filled) > EPS:
                _append_reason(reasons, "queue_model_reconciled_to_authoritative_fill")
            authoritative_filled = cumulative
            unknown = False
            state = _known_state(
                filled=effective_filled(),
                requested=maker.requested_shares,
                cancel_requested=cancel_requested,
                cancel_acknowledged=cancel_acknowledged,
                rejected=rejected,
            )
            _append_reason(reasons, "authoritative_fill_applied")
        elif event.event_type is MakerEventType.REJECT:
            rejected = True
            unknown = False
            state = _known_state(
                filled=effective_filled(),
                requested=maker.requested_shares,
                cancel_requested=cancel_requested,
                cancel_acknowledged=cancel_acknowledged,
                rejected=rejected,
            )
            _append_reason(reasons, "maker_rejected")
        elif event.event_type is MakerEventType.UNKNOWN_POST:
            unknown = True
            state = MakerLifecycleState.RECONCILE_REQUIRED
            _append_reason(reasons, "unknown_post_requires_reconcile")
        elif event.event_type is MakerEventType.AUTHORITATIVE_STATUS:
            cumulative = event.cumulative_filled_shares
            status = event.exchange_status
            if cumulative is None or status is None:
                raise ContractError("maker_authoritative_status_incomplete")
            if cumulative > maker.requested_shares + EPS:
                raise ContractError("maker_authoritative_fill_exceeds_requested")
            if authoritative_filled is not None and cumulative + EPS < authoritative_filled:
                raise ContractError("maker_authoritative_fill_moved_backwards")
            authoritative_filled = cumulative
            unknown = False
            if status is MakerExchangeStatus.OPEN:
                cancel_acknowledged = False
                rejected = False
                state = _known_state(
                    filled=effective_filled(),
                    requested=maker.requested_shares,
                    cancel_requested=cancel_requested,
                    cancel_acknowledged=False,
                    rejected=False,
                )
            elif status is MakerExchangeStatus.CANCELLED:
                cancel_requested = True
                cancel_acknowledged = True
                rejected = False
                state = _known_state(
                    filled=effective_filled(),
                    requested=maker.requested_shares,
                    cancel_requested=True,
                    cancel_acknowledged=True,
                    rejected=False,
                )
            elif status is MakerExchangeStatus.FILLED:
                if cumulative + EPS < maker.requested_shares:
                    state = MakerLifecycleState.RECONCILE_REQUIRED
                    unknown = True
                    _append_reason(reasons, "filled_status_quantity_mismatch")
                else:
                    state = MakerLifecycleState.FILLED
            elif status is MakerExchangeStatus.REJECTED:
                rejected = True
                state = _known_state(
                    filled=effective_filled(),
                    requested=maker.requested_shares,
                    cancel_requested=cancel_requested,
                    cancel_acknowledged=cancel_acknowledged,
                    rejected=True,
                )
            _append_reason(reasons, "authoritative_status_applied")

    filled = min(maker.requested_shares, effective_filled())
    remaining = max(0.0, maker.requested_shares - filled)
    reconcile_required = state is MakerLifecycleState.RECONCILE_REQUIRED or unknown
    core = {
        "schema": "target_target_maker_receipt_identity_v1",
        "candidate": maker.to_dict(),
        "state": state.value,
        "modeled_filled_shares": modeled_filled,
        "authoritative_filled_shares": authoritative_filled,
        "filled_shares": filled,
        "remaining_shares": remaining,
        "queue_ahead_shares": queue_ahead,
        "cancel_requested": cancel_requested,
        "cancel_acknowledged": cancel_acknowledged,
        "reconcile_required": reconcile_required,
        "reason_codes": reasons,
        "processed_event_ids": processed_ids,
        "duplicate_event_count": duplicate_count,
    }
    return TargetMakerReceipt(
        receipt_id="maker_receipt_" + content_sha256(core)[:32],
        candidate_id=maker.candidate_id,
        state=state,
        requested_shares=maker.requested_shares,
        modeled_filled_shares=modeled_filled,
        authoritative_filled_shares=authoritative_filled,
        filled_shares=filled,
        remaining_shares=remaining,
        queue_ahead_shares=queue_ahead,
        cancel_requested=cancel_requested,
        cancel_acknowledged=cancel_acknowledged,
        reconcile_required=reconcile_required,
        reason_codes=tuple(reasons),
        processed_event_ids=tuple(processed_ids),
        duplicate_event_count=duplicate_count,
    )


__all__ = [
    "build_target_maker_candidate",
    "simulate_maker_lifecycle",
]
