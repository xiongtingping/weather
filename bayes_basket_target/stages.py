"""Stage checkpoint routing with explicit no-silent-downgrade semantics."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .contracts import ContractError, ProbabilityEnvelope, STAGES


STAGE_PRECEDENCE = {stage: index for index, stage in enumerate(STAGES)}


def select_latest_checkpoint(
    envelopes: Sequence[ProbabilityEnvelope | dict[str, Any]] | Iterable[ProbabilityEnvelope | dict[str, Any]],
    *,
    at_utc: str,
) -> ProbabilityEnvelope:
    """Select the newest checkpoint; stage precedence only breaks exact ties."""

    rows = [item if isinstance(item, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(item) for item in envelopes]
    if not rows:
        raise ContractError("no_probability_envelope")
    station_days = {(row.station_id, row.event_date) for row in rows}
    if len(station_days) != 1:
        raise ContractError("probability_envelope_scope_mismatch")
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row.stage, row.checkpoint_id)
        if key in seen:
            raise ContractError("duplicate_probability_checkpoint")
        seen.add(key)

    visible = [row for row in rows if row.decision_at_utc <= at_utc]
    if not visible:
        raise ContractError("no_probability_checkpoint_asof")
    latest_at = max(row.decision_at_utc for row in visible)
    latest = [row for row in visible if row.decision_at_utc == latest_at]
    highest = max(STAGE_PRECEDENCE[row.stage] for row in latest)
    winners = [row for row in latest if STAGE_PRECEDENCE[row.stage] == highest]
    if len(winners) != 1:
        raise ContractError("ambiguous_stage_checkpoint")
    winner = winners[0]
    if not winner.is_servable(at_utc):
        raise ContractError("latest_stage_stale_fail_closed")
    return winner


def select_current_stage(
    envelopes: Sequence[ProbabilityEnvelope | dict[str, Any]] | Iterable[ProbabilityEnvelope | dict[str, Any]],
    *,
    at_utc: str,
) -> ProbabilityEnvelope:
    """Select the highest submitted stage in the unique SSOT stage chain.

    NWP P0 drives initial basket selection. Once TAF/P0-M or METAR/P1 exists, a
    later model refresh must rebuild the base and replay those higher-stage
    facts; it must never downgrade the current authority back to raw P0.
    """
    rows = [item if isinstance(item, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(item) for item in envelopes]
    if not rows:
        raise ContractError("no_probability_envelope")
    station_days = {(row.station_id, row.event_date) for row in rows}
    if len(station_days) != 1:
        raise ContractError("probability_envelope_scope_mismatch")
    for stage in reversed(STAGES):
        candidates = [
            row
            for row in rows
            if row.stage == stage and row.decision_at_utc <= at_utc
        ]
        if not candidates:
            continue
        latest_at = max(row.decision_at_utc for row in candidates)
        latest = [row for row in candidates if row.decision_at_utc == latest_at]
        if len(latest) != 1:
            raise ContractError("ambiguous_stage_checkpoint")
        winner = latest[0]
        if not winner.is_servable(at_utc):
            raise ContractError("latest_stage_stale_fail_closed")
        return winner
    raise ContractError("probability_stage_invalid")


__all__ = ["STAGE_PRECEDENCE", "select_current_stage", "select_latest_checkpoint"]
