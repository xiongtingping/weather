"""Build a target cash-phase ingress from an explicit promoted policy.

The producer accepts the current monotonic phase as an input from the
target-owned phase state.  It never infers phase from source-wallet history,
uses a default cash amount, or changes the weather probability stage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cash_phase import BasketCashPhaseInput, BasketCashPhasePolicyArtifact, CashPhase, evaluate_cash_phase
from .cash_phase_authority import (
    AUTHORITY_SCOPES,
    CANDIDATE_AUTHORITY_SCOPE,
    FORMAL_AUTHORITY_SCOPE,
    CashPhaseAuthorityManifest,
    cash_phase_authority_candidates,
)
from .contracts import ContractError, content_sha256
from .et_history import assert_event_day_started
from .cash_phase_oos import build_cash_phase_oos_scorecard
from .resource_bounds import write_bounded_json_if_changed
from .fleet import expected_station_count


SCHEMA = "bayes_basket_target_cash_phase_producer_v1"
INGRESS_SCHEMA = "bayes_basket_cash_phase_ingress_v1"
AUTHORITY_SET_SCHEMA = "bayes_basket_target_cash_phase_authority_set_v1"
MAX_AUTHORITY_STATIONS = expected_station_count()
MAX_AUTHORITY_ROWS = 4096


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _registry_timezones(
    registry_rows: Sequence[Mapping[str, Any]],
    *,
    expected_station_ids: Sequence[str] | None,
) -> dict[str, str]:
    if isinstance(registry_rows, (str, bytes)) or not isinstance(registry_rows, Sequence):
        raise ContractError("cash_phase_registry_rows_sequence_required")
    if len(registry_rows) > MAX_AUTHORITY_ROWS:
        raise ContractError("cash_phase_registry_rows_exceeded")
    by_station: dict[str, set[str]] = {}
    for raw in registry_rows:
        if not isinstance(raw, Mapping):
            raise ContractError("cash_phase_registry_row_mapping_required")
        station = _text("registry_station_id", raw.get("station_id"))
        timezone_name = _text(
            "registry_settlement_timezone",
            raw.get("settlement_timezone"),
        )
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ContractError("cash_phase_registry_settlement_timezone_invalid") from exc
        by_station.setdefault(station, set()).add(timezone_name)
    ambiguous = sorted(
        station for station, values in by_station.items() if len(values) != 1
    )
    if ambiguous:
        raise ContractError(
            "cash_phase_registry_settlement_timezone_not_unique:"
            + ",".join(ambiguous)
        )
    timezones = {
        station: next(iter(values)) for station, values in by_station.items()
    }
    expected = (
        sorted({_text("expected_station_id", item) for item in expected_station_ids})
        if expected_station_ids is not None
        else sorted(timezones)
    )
    if not expected:
        raise ContractError("cash_phase_authority_station_scope_empty")
    if len(expected) > MAX_AUTHORITY_STATIONS:
        raise ContractError("cash_phase_authority_station_scope_exceeded")
    if set(timezones) != set(expected):
        raise ContractError("cash_phase_authority_registry_station_scope_mismatch")
    return {station: timezones[station] for station in expected}


def _active_existing_manifests(
    manifests: Sequence[CashPhaseAuthorityManifest | Mapping[str, Any]],
    *,
    station_id: str,
    observed_at_utc: str,
    authority_scope: str = CANDIDATE_AUTHORITY_SCOPE,
) -> tuple[CashPhaseAuthorityManifest, ...]:
    observed_text = _timestamp("observed_at_utc", observed_at_utc)
    observed = datetime.fromisoformat(observed_text.replace("Z", "+00:00"))
    parsed = tuple(
        item
        if isinstance(item, CashPhaseAuthorityManifest)
        else CashPhaseAuthorityManifest.from_mapping(item)
        for item in manifests
    )
    return tuple(
        item
        for item in parsed
        if item.station_id == station_id
        and item.authority_scope == authority_scope
        and datetime.fromisoformat(
            item.valid_from_utc.replace("Z", "+00:00")
        )
        <= observed
        < datetime.fromisoformat(item.valid_until_utc.replace("Z", "+00:00"))
    )


def _manifest_windows_overlap(
    manifests: Sequence[CashPhaseAuthorityManifest],
    *,
    station_id: str,
    authority_scope: str = CANDIDATE_AUTHORITY_SCOPE,
) -> bool:
    scoped = sorted(
        (
            item
            for item in manifests
            if item.station_id == station_id
            and item.authority_scope == authority_scope
        ),
        key=lambda item: datetime.fromisoformat(
            item.valid_from_utc.replace("Z", "+00:00")
        ),
    )
    for left, right in zip(scoped, scoped[1:]):
        left_until = datetime.fromisoformat(
            left.valid_until_utc.replace("Z", "+00:00")
        )
        right_from = datetime.fromisoformat(
            right.valid_from_utc.replace("Z", "+00:00")
        )
        if right_from < left_until:
            return True
    return False


def build_cash_phase_authority_set(
    *,
    policies: Sequence[BasketCashPhasePolicyArtifact | Mapping[str, Any]],
    registry_rows: Sequence[Mapping[str, Any]],
    observed_at_utc: str,
    observed_cash_phases: Mapping[str, str],
    existing_manifests: Sequence[
        CashPhaseAuthorityManifest | Mapping[str, Any]
    ] = (),
    expected_station_ids: Sequence[str] | None = None,
    authority_scope: str = CANDIDATE_AUTHORITY_SCOPE,
) -> dict[str, Any]:
    """Build one content-addressed no-live authority set for all stations.

    Missing and duplicate persisted manifests are both execution blockers.
    When every formal policy input is sound, the packet carries a complete
    replacement candidate that an external atomic committer may install.
    This module itself never opens a database or reads source-account state.
    """

    observed = _timestamp("observed_at_utc", observed_at_utc)
    scope = str(authority_scope or "").strip().upper()
    if scope not in AUTHORITY_SCOPES:
        raise ContractError("cash_phase_authority_scope_invalid")
    if scope == FORMAL_AUTHORITY_SCOPE:
        raise ContractError("cash_phase_formal_oos_authority_schema_missing")
    if not isinstance(observed_cash_phases, Mapping):
        raise ContractError("cash_phase_observed_phases_mapping_required")
    if isinstance(policies, (str, bytes)) or not isinstance(policies, Sequence):
        raise ContractError("cash_phase_policies_sequence_required")
    if len(policies) > MAX_AUTHORITY_ROWS:
        raise ContractError("cash_phase_policy_rows_exceeded")
    if len(existing_manifests) > MAX_AUTHORITY_ROWS:
        raise ContractError("cash_phase_authority_manifest_rows_exceeded")
    parsed_existing = tuple(
        item
        if isinstance(item, CashPhaseAuthorityManifest)
        else CashPhaseAuthorityManifest.from_mapping(item)
        for item in existing_manifests
    )
    timezones = _registry_timezones(
        registry_rows,
        expected_station_ids=expected_station_ids,
    )

    desired: list[CashPhaseAuthorityManifest] = []
    blockers: list[dict[str, Any]] = []
    for station, timezone_name in timezones.items():
        try:
            candidates = cash_phase_authority_candidates(
                policies,
                station_id=station,
                station_timezone=timezone_name,
                observed_at_utc=observed,
                observed_cash_phase=observed_cash_phases.get(station),
                authority_scope=scope,
            )
        except ContractError as exc:
            candidates = ()
            blockers.append({
                "station_id": station,
                "reason": str(exc),
                "candidate_count": 0,
            })
        if len(candidates) != 1:
            if not any(row["station_id"] == station for row in blockers):
                blockers.append({
                    "station_id": station,
                    "reason": "cash_phase_authority_candidate_not_unique",
                    "candidate_count": len(candidates),
                })
            continue
        desired.append(candidates[0])

    if blockers:
        core = {
            "schema": AUTHORITY_SET_SCHEMA,
            "authority_scope": scope,
            "observed_at_utc": observed,
            "station_ids": list(timezones),
            "state": "BLOCKED_FAIL_CLOSED",
            "execution_authorized": False,
            "repair_candidate_complete": False,
            "authority_manifests": [],
            "blockers": blockers,
            "places_orders": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
        return {**core, "authority_set_sha256": content_sha256(core)}

    desired_by_station = {item.station_id: item for item in desired}
    extra_manifest_stations = sorted({
        item.station_id
        for item in parsed_existing
        if item.authority_scope == scope
    } - set(timezones))
    repair_reasons: list[dict[str, Any]] = [
        {
            "station_id": station,
            "reason": "cash_phase_authority_manifest_station_scope_mismatch",
            "active_manifest_count": len(_active_existing_manifests(
                parsed_existing,
                station_id=station,
                observed_at_utc=observed,
                authority_scope=scope,
            )),
        }
        for station in extra_manifest_stations
    ]
    for station in timezones:
        active = _active_existing_manifests(
            parsed_existing,
            station_id=station,
            observed_at_utc=observed,
            authority_scope=scope,
        )
        overlaps = _manifest_windows_overlap(
            parsed_existing,
            station_id=station,
            authority_scope=scope,
        )
        exact = (
            not overlaps
            and len(active) == 1
            and active[0].manifest_sha256
            == desired_by_station[station].manifest_sha256
        )
        if not exact:
            repair_reasons.append({
                "station_id": station,
                "reason": (
                    "cash_phase_authority_manifest_window_overlap"
                    if overlaps
                    else "cash_phase_authority_manifest_not_unique"
                ),
                "active_manifest_count": len(active),
            })
    state = "READY" if not repair_reasons else "BLOCKED_REPAIR_CANDIDATE"
    core = {
        "schema": AUTHORITY_SET_SCHEMA,
        "authority_scope": scope,
        "observed_at_utc": observed,
        "station_ids": list(timezones),
        "state": state,
        "execution_authorized": state == "READY",
        "repair_candidate_complete": True,
        "authority_manifests": [
            item.to_dict() for item in sorted(
                desired,
                key=lambda row: row.station_id,
            )
        ],
        "blockers": repair_reasons,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**core, "authority_set_sha256": content_sha256(core)}


def materialize_cash_phase_authority_set(
    output_path: Path | str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Validate completely, then atomically write one bounded candidate."""

    packet = build_cash_phase_authority_set(**kwargs)
    wrote = write_bounded_json_if_changed(output_path, packet)
    return {**packet, "wrote": wrote, "output_path": str(Path(output_path))}


def build_cash_phase_ingress(
    *,
    policy: BasketCashPhasePolicyArtifact | Mapping[str, Any],
    station_id: str,
    event_date_local: str,
    station_timezone: str,
    observed_at_utc: str,
    current_phase: CashPhase | str,
    checkpoint_id: str,
) -> dict[str, Any]:
    artifact = policy if isinstance(policy, BasketCashPhasePolicyArtifact) else BasketCashPhasePolicyArtifact.from_mapping(policy)
    station = _text("station_id", station_id)
    event_date = _text("event_date_local", event_date_local)
    observed = _timestamp("observed_at_utc", observed_at_utc)
    timezone_name = _text("station_timezone", station_timezone)
    assert_event_day_started(
        event_date,
        as_of_utc=observed,
        target_timezone=timezone_name,
    )
    if artifact.station_id != station or artifact.station_timezone != timezone_name:
        raise ContractError("cash_phase_policy_scope_mismatch")
    try:
        phase = current_phase if isinstance(current_phase, CashPhase) else CashPhase(str(current_phase))
    except ValueError as exc:
        raise ContractError("current_cash_phase_invalid") from exc
    checkpoint = _text("checkpoint_id", checkpoint_id)
    # Route construction through the canonical contract parser so that
    # equivalent offset forms (for example +00:00 and Z) have one lineage
    # representation in both the input and the phase decision.
    phase_input = BasketCashPhaseInput.from_mapping({
        "policy_version": artifact.policy_version,
        "station_id": station,
        "current_phase": phase.value,
        "event_date_local": event_date,
        "observed_at_utc": observed,
        "station_timezone": timezone_name,
        "checkpoint_id": checkpoint,
    })
    decision = evaluate_cash_phase(artifact, phase_input)
    input_dict = phase_input.to_dict()
    return {
        "schema": INGRESS_SCHEMA,
        "producer_schema": SCHEMA,
        "policy_version": artifact.policy_version,
        "policy_sha256": artifact.content_sha256,
        "phase_decision_sha256": content_sha256(decision.to_dict()),
        "observed_at": phase_input.observed_at_utc,
        "markets": [{
            "station_id": station,
            "event_date": event_date,
            "cash_phase_input": input_dict,
            "cash_phase_decision": decision.to_dict(),
        }],
        "places_orders": False,
        "calls_order_gateway": False,
    }


def build_cash_phase_forward_scorecard(
    *,
    policy: BasketCashPhasePolicyArtifact | Mapping[str, Any],
    observations: Any,
    as_of_utc: str,
    evaluation_start_utc: str | None = None,
    evaluation_end_utc: str | None = None,
    policy_training_cutoff_utc: str | None = None,
    fault_cases: Sequence[Mapping[str, Any]] = (),
    minimum_unique_events_per_phase: int = 30,
    strict_target_owned: bool = True,
    max_rows: int = 4096,
) -> dict[str, Any]:
    """Build target-owned cash-phase OOS evidence from explicit observations.

    This is a pure producer facade.  The policy is resolved by the caller and
    its promotion bit is carried into the scorecard; an unpromoted policy
    therefore produces a durable-looking audit packet but remains fail closed
    for any promotion consumer.  No ingress/scorecard path places an order.
    """

    artifact = (
        policy
        if isinstance(policy, BasketCashPhasePolicyArtifact)
        else BasketCashPhasePolicyArtifact.from_mapping(policy)
    )
    _timestamp("as_of_utc", as_of_utc)
    return build_cash_phase_oos_scorecard(
        observations,
        as_of_utc=as_of_utc,
        policy_version=artifact.policy_version,
        policy_promoted=artifact.promoted,
        policy_evidence_status=artifact.evidence_status,
        expected_station_id=artifact.station_id,
        expected_station_timezone=artifact.station_timezone,
        evaluation_start_utc=evaluation_start_utc,
        evaluation_end_utc=evaluation_end_utc,
        policy_training_cutoff_utc=policy_training_cutoff_utc,
        fault_cases=fault_cases,
        minimum_unique_events_per_phase=minimum_unique_events_per_phase,
        strict_target_owned=strict_target_owned,
        max_rows=max_rows,
    )


# Explicit alias for callers using the roadmap's shorter name.
build_cash_phase_scorecard = build_cash_phase_forward_scorecard


__all__ = [
    "INGRESS_SCHEMA",
    "AUTHORITY_SET_SCHEMA",
    "MAX_AUTHORITY_STATIONS",
    "SCHEMA",
    "build_cash_phase_authority_set",
    "build_cash_phase_forward_scorecard",
    "build_cash_phase_ingress",
    "build_cash_phase_scorecard",
    "materialize_cash_phase_authority_set",
]
