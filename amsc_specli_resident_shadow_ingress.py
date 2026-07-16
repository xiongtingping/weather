"""Resident AMSC/SPECI ingress for the isolated P10 shadow fast lane.

Acquisition is injected by the hosting process.  This module never performs
network I/O itself, starts no subprocess, and never touches a generic queue.
Its job is to reject a malformed/non-primary arrival before canonical writes,
then hand one real arrival directly to the resident fast-lane worker.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from amsc_specli_resident_arrival_contract import (
    validate_amsc_specli_resident_arrival_envelope,
)
from observation_fast_lane_resident_shadow_worker import (
    ResidentObservationFastLaneShadowWorker,
)
from observation_fast_lane_contract import HARD_EXECUTION_DEADLINE_MS
from source_observation_runtime import PRIMARY_METAR_SOURCE_ID, SOURCE_CLASSES


SCHEMA_VERSION = "amsc_specli_resident_shadow_ingress_v1"
ACCEPTED_REPORT_TYPES = frozenset({"METAR", "SPECI"})
HARD_T1_TO_DISPATCH_MS = HARD_EXECUTION_DEADLINE_MS


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _transport_seen_at(source_observation: Mapping[str, Any]) -> str:
    return _text(
        source_observation.get("first_transport_seen_at")
        or source_observation.get("response_completed_at")
        or source_observation.get("fetched_at")
    )


def validate_amsc_specli_resident_ingress(
    *,
    source_observation: Mapping[str, Any],
    first_actionable_seen_at: Any,
    decision_as_of: Any,
    canonicalized_at: Any,
) -> dict[str, Any]:
    """Validate one real primary arrival before it reaches canonical storage."""

    source = _mapping(source_observation)
    payload = _mapping(source.get("payload"))
    report_type = _text(payload.get("report_type") or source.get("report_type")).upper()
    source_id = _text(source.get("source_id"))
    source_class = _text(source.get("source_class"))
    role = _text(source.get("role") or source.get("source_role"))
    valid_time = _text(source.get("valid_time"))
    transport_seen_at = _transport_seen_at(source)
    actionable_at = _text(first_actionable_seen_at)
    as_of = _text(decision_as_of)
    canonical_at = _text(canonicalized_at)
    blockers: list[str] = []
    if source_id != PRIMARY_METAR_SOURCE_ID:
        blockers.append("amsc_specli_ingress_primary_source_required")
    if source_class != SOURCE_CLASSES["hard_observation"]:
        blockers.append("amsc_specli_ingress_hard_observation_class_required")
    if role != "metar_hard_observation_primary":
        blockers.append("amsc_specli_ingress_primary_role_required")
    if report_type not in ACCEPTED_REPORT_TYPES:
        blockers.append("amsc_specli_ingress_report_type_invalid")
    parsed = {
        "valid_time": _parse_dt(valid_time),
        "transport_seen_at": _parse_dt(transport_seen_at),
        "first_actionable_seen_at": _parse_dt(actionable_at),
        "decision_as_of": _parse_dt(as_of),
        "canonicalized_at": _parse_dt(canonical_at),
    }
    for name, value in parsed.items():
        if value is None:
            blockers.append(f"amsc_specli_ingress_{name}_missing_or_invalid")
    if all(parsed[name] is not None for name in ("valid_time", "transport_seen_at", "first_actionable_seen_at", "decision_as_of")):
        if not (
            parsed["valid_time"]
            <= parsed["transport_seen_at"]
            <= parsed["first_actionable_seen_at"]
            <= parsed["decision_as_of"]
        ):
            blockers.append("amsc_specli_ingress_causal_time_order_invalid")
    if (
        parsed["canonicalized_at"] is not None
        and parsed["first_actionable_seen_at"] is not None
        and parsed["canonicalized_at"] < parsed["first_actionable_seen_at"]
    ):
        blockers.append("amsc_specli_ingress_canonicalized_before_actionable")
    if (
        parsed["decision_as_of"] is not None
        and parsed["first_actionable_seen_at"] is not None
        and (parsed["decision_as_of"] - parsed["first_actionable_seen_at"]).total_seconds() * 1000
        > HARD_T1_TO_DISPATCH_MS
    ):
        blockers.append("amsc_specli_ingress_observation_hard_deadline_exceeded")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "source_id": source_id,
        "report_type": report_type,
        "timestamps": {
            "valid_time": valid_time,
            "first_transport_seen_at": transport_seen_at,
            "first_actionable_seen_at": actionable_at,
            "decision_as_of": as_of,
            "canonicalized_at": canonical_at,
        },
        "boundary": {
            "network_acquisition_owned_by_host": True,
            "no_awc_wait": True,
            "no_generic_task_queue": True,
            "no_subprocess_per_event": True,
            "no_synchronous_forecast_refresh": True,
            "hard_t1_to_dispatch_ms": HARD_T1_TO_DISPATCH_MS,
            "shadow_only": True,
        },
    }


class AmscSpecliResidentShadowIngress:
    """Host-owned resident ingress that dispatches one validated primary report."""

    def __init__(self, worker: ResidentObservationFastLaneShadowWorker) -> None:
        self._worker = worker

    def process_arrival_envelope(
        self,
        *,
        arrival_envelope: Mapping[str, Any],
        target_identity: str,
        event_date: str,
        decision_as_of: str,
        canonicalized_at: str,
        posterior_transaction_inputs: Mapping[str, Any],
        decision_cycle_inputs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Dispatch one contract-bound arrival without accepting caller T1.

        Deployment hosts must call this method. ``process_received_observation``
        remains a narrow source-validation test seam only.
        """

        envelope = validate_amsc_specli_resident_arrival_envelope(arrival_envelope)
        if envelope.get("ok") is not True:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked_before_canonical_write",
                "arrival_envelope": envelope,
                "shadow_only": True,
                "places_orders": False,
            }
        result = self.process_received_observation(
            source_observation=_mapping(envelope.get("dispatch_source_observation")),
            first_actionable_seen_at=_text(envelope.get("first_actionable_seen_at")),
            target_identity=target_identity,
            event_date=event_date,
            decision_as_of=decision_as_of,
            canonicalized_at=canonicalized_at,
            posterior_transaction_inputs=posterior_transaction_inputs,
            decision_cycle_inputs=decision_cycle_inputs,
        )
        result["arrival_envelope"] = envelope
        return result

    def process_received_observation(
        self,
        *,
        source_observation: Mapping[str, Any],
        first_actionable_seen_at: str,
        target_identity: str,
        event_date: str,
        decision_as_of: str,
        canonicalized_at: str,
        posterior_transaction_inputs: Mapping[str, Any],
        decision_cycle_inputs: Mapping[str, Any],
    ) -> dict[str, Any]:
        validation = validate_amsc_specli_resident_ingress(
            source_observation=source_observation,
            first_actionable_seen_at=first_actionable_seen_at,
            decision_as_of=decision_as_of,
            canonicalized_at=canonicalized_at,
        )
        if validation.get("ok") is not True:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked_before_canonical_write",
                "ingress": validation,
                "shadow_only": True,
                "places_orders": False,
            }
        result = self._worker.process_source_observation(
            source_observation=source_observation,
            target_identity=_text(target_identity),
            event_date=_text(event_date),
            decision_as_of=_text(decision_as_of),
            canonicalized_at=_text(canonicalized_at),
            posterior_transaction_inputs=_mapping(posterior_transaction_inputs),
            decision_cycle_inputs=_mapping(decision_cycle_inputs),
            allow_awc_substitute=False,
        )
        return {
            "schema": SCHEMA_VERSION,
            "ok": result.get("ok") is True,
            "state": "dispatched" if result.get("ok") is True else "canonical_or_fast_lane_blocked",
            "ingress": validation,
            "fast_lane": result,
            "shadow_only": True,
            "places_orders": False,
        }

    def process_received_batch(
        self,
        arrivals: Iterable[Mapping[str, Any]],
        *,
        dispatch_inputs_for: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Process a host-supplied batch in arrival order without acquisition work."""

        outcomes: list[dict[str, Any]] = []
        for raw in arrivals:
            arrival = _mapping(raw)
            source_observation = _mapping(arrival.get("source_observation"))
            dispatch = _mapping(dispatch_inputs_for(arrival))
            outcomes.append(
                self.process_received_observation(
                    source_observation=source_observation,
                    first_actionable_seen_at=_text(arrival.get("first_actionable_seen_at")),
                    target_identity=_text(dispatch.get("target_identity")),
                    event_date=_text(dispatch.get("event_date")),
                    decision_as_of=_text(dispatch.get("decision_as_of")),
                    canonicalized_at=_text(dispatch.get("canonicalized_at")),
                    posterior_transaction_inputs=_mapping(dispatch.get("posterior_transaction_inputs")),
                    decision_cycle_inputs=_mapping(dispatch.get("decision_cycle_inputs")),
                )
            )
        return {
            "schema": SCHEMA_VERSION,
            "ok": all(item.get("ok") is True for item in outcomes),
            "state": "batch_dispatched" if outcomes and all(item.get("ok") is True for item in outcomes) else "batch_partial_or_blocked",
            "arrival_count": len(outcomes),
            "outcomes": outcomes,
            "shadow_only": True,
            "places_orders": False,
        }


__all__ = [
    "ACCEPTED_REPORT_TYPES",
    "HARD_T1_TO_DISPATCH_MS",
    "SCHEMA_VERSION",
    "AmscSpecliResidentShadowIngress",
    "validate_amsc_specli_resident_ingress",
]
