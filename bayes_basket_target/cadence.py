"""Pure, fail-closed cadence evaluation for the basket candidate layer.

Cadence is a trigger/latency policy only.  It cannot create an order, intent,
reservation, or gateway call.  ``ALLOW_CANDIDATE`` means that a caller may
continue to its independent B13/B14 and execution gates; it is never an order
authorization.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class CadenceDecision(str, Enum):
    HOLD = "HOLD"
    REEVALUATE = "REEVALUATE"
    ALLOW_CANDIDATE = "ALLOW_CANDIDATE"


_TERMINAL_OR_SAFE_ORDER_STATES = frozenset({
    "FLAT",
    "NO_OPEN_ORDER",
    "RESOLVED",
    "RESOLVED_CANCELLED",
    "RESOLVED_FILLED",
})
_KNOWN_NONCANDIDATE_ORDER_STATES = frozenset({
    "OPEN",
    "PENDING",
    "PARTIAL",
})
_BLOCKED_ORDER_STATES = frozenset({
    "UNKNOWN",
    "RECONCILE_REQUIRED",
    "AMBIGUOUS",
})
_VERSION_FIELDS = (
    "checkpoint_version",
    "quote_version",
    "fill_version",
    "time_gate_version",
)
CADENCE_GATE_SECONDS_KEY = "gate_seconds_intercept"
DEFAULT_ALLOW_CANDIDATE_HAZARD_THRESHOLD = 0.67


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class CadenceThresholds:
    """Validated thresholds consumed by the pure evaluator.

    ``gate_seconds_intercept`` is inclusive.  The hazard threshold is also
    inclusive, so exact boundary behavior is deterministic and testable.
    """

    gate_seconds_intercept: float
    allow_candidate_hazard_threshold: float = DEFAULT_ALLOW_CANDIDATE_HAZARD_THRESHOLD

    @property
    def reevaluate_after_seconds(self) -> float:
        """Compatibility name for callers that describe the elapsed gate."""

        return self.gate_seconds_intercept

    def valid(self) -> bool:
        interval = _finite(self.gate_seconds_intercept)
        hazard = _finite(self.allow_candidate_hazard_threshold)
        return (
            interval is not None
            and interval > 0.0
            and hazard is not None
            and 0.0 <= hazard <= 1.0
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CadenceThresholds | None":
        if not isinstance(raw, Mapping):
            return None
        required = {CADENCE_GATE_SECONDS_KEY, "allow_candidate_hazard_threshold"}
        if not required.issubset(raw):
            return None
        value = cls(
            gate_seconds_intercept=raw.get(CADENCE_GATE_SECONDS_KEY),
            allow_candidate_hazard_threshold=raw.get("allow_candidate_hazard_threshold"),
        )
        return value if value.valid() else None

    @classmethod
    def from_station_policy_theta(
        cls,
        raw: Mapping[str, Any],
        *,
        allow_candidate_hazard_threshold: float = DEFAULT_ALLOW_CANDIDATE_HAZARD_THRESHOLD,
    ) -> "CadenceThresholds | None":
        """Resolve the actual station-policy cadence artifact contract.

        The station artifact owns the elapsed gate under
        ``gate_seconds_intercept``.  The artifact does not own a live hazard
        threshold, so that threshold remains a validated primitive constant.
        Missing or malformed artifact data is never replaced with a guessed
        interval.
        """

        if not isinstance(raw, Mapping) or CADENCE_GATE_SECONDS_KEY not in raw:
            return None
        value = cls(
            gate_seconds_intercept=raw.get(CADENCE_GATE_SECONDS_KEY),
            allow_candidate_hazard_threshold=allow_candidate_hazard_threshold,
        )
        return value if value.valid() else None


@dataclass(frozen=True)
class CadenceSnapshot:
    """One immutable observation used by :func:`evaluate_cadence`.

    All four version fields are required so quote/fill/checkpoint/time changes
    are explicit rather than inferred from mutable objects.
    """

    observed_at_utc: str | datetime
    order_state: str
    hazard_score: float
    safety_gates_passed: bool
    candidate_present: bool
    checkpoint_version: str
    quote_version: str
    fill_version: str
    time_gate_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CadenceSnapshot | None":
        if not isinstance(raw, Mapping):
            return None
        try:
            value = cls(
                observed_at_utc=raw.get("observed_at_utc"),
                order_state=raw.get("order_state"),
                hazard_score=raw.get("hazard_score"),
                safety_gates_passed=raw.get("safety_gates_passed"),
                candidate_present=raw.get("candidate_present"),
                checkpoint_version=raw.get("checkpoint_version"),
                quote_version=raw.get("quote_version"),
                fill_version=raw.get("fill_version"),
                time_gate_version=raw.get("time_gate_version"),
            )
        except (TypeError, ValueError):
            return None
        return value if value.valid() else None

    def valid(self) -> bool:
        timestamp = _timestamp(self.observed_at_utc)
        hazard = _finite(self.hazard_score)
        state = self.normalized_order_state
        gates = _boolean(self.safety_gates_passed)
        candidate = _boolean(self.candidate_present)
        return (
            timestamp is not None
            and hazard is not None
            and 0.0 <= hazard <= 1.0
            and state is not None
            and state not in _BLOCKED_ORDER_STATES
            and gates is not None
            and candidate is not None
            and all(_version(getattr(self, field)) is not None for field in _VERSION_FIELDS)
        )

    @property
    def normalized_order_state(self) -> str | None:
        if not isinstance(self.order_state, str):
            return None
        value = self.order_state.strip().upper()
        known = _TERMINAL_OR_SAFE_ORDER_STATES | _KNOWN_NONCANDIDATE_ORDER_STATES | _BLOCKED_ORDER_STATES
        return value if value in known else None

    @property
    def observed_datetime(self) -> datetime | None:
        return _timestamp(self.observed_at_utc)

    @property
    def versions(self) -> tuple[str, ...] | None:
        values = tuple(_version(getattr(self, field)) for field in _VERSION_FIELDS)
        return values if all(value is not None for value in values) else None


def _coerce_thresholds(value: CadenceThresholds | Mapping[str, Any] | Any) -> CadenceThresholds | None:
    if isinstance(value, CadenceThresholds):
        return value if value.valid() else None
    if isinstance(value, Mapping):
        return CadenceThresholds.from_mapping(value)
    return None


def _coerce_snapshot(value: CadenceSnapshot | Mapping[str, Any] | Any) -> CadenceSnapshot | None:
    if isinstance(value, CadenceSnapshot):
        return value if value.valid() else None
    if isinstance(value, Mapping):
        return CadenceSnapshot.from_mapping(value)
    return None


def evaluate_cadence(
    current: CadenceSnapshot | Mapping[str, Any] | Any,
    previous: CadenceSnapshot | Mapping[str, Any] | None = None,
    thresholds: CadenceThresholds | Mapping[str, Any] | Any = CadenceThresholds(
        gate_seconds_intercept=300.0
    ),
) -> str:
    """Return exactly one of ``HOLD``, ``REEVALUATE``, or ``ALLOW_CANDIDATE``.

    The function is deliberately total: malformed input, an unknown order
    state, a failed safety gate, a non-finite value, or a backwards clock all
    fail closed to ``HOLD``.  It does not mutate either snapshot and has no
    external side effects.
    """

    policy = _coerce_thresholds(thresholds)
    current_value = _coerce_snapshot(current)
    previous_value = None if previous is None else _coerce_snapshot(previous)
    if policy is None or current_value is None or (previous is not None and previous_value is None):
        return CadenceDecision.HOLD.value

    current_time = current_value.observed_datetime
    if current_time is None:
        return CadenceDecision.HOLD.value
    if previous_value is not None:
        previous_time = previous_value.observed_datetime
        if previous_time is None or current_time < previous_time:
            return CadenceDecision.HOLD.value
        if previous_value.normalized_order_state in _BLOCKED_ORDER_STATES:
            return CadenceDecision.HOLD.value

    if current_value.normalized_order_state in _BLOCKED_ORDER_STATES:
        return CadenceDecision.HOLD.value
    if not current_value.safety_gates_passed:
        return CadenceDecision.HOLD.value

    # A prior snapshot is required to establish a cadence interval.  An
    # initial observation is an explicit reevaluation request, never an
    # implicit candidate release.
    if previous_value is None:
        return CadenceDecision.REEVALUATE.value

    changed = current_value.versions != previous_value.versions
    if previous_value is None:
        elapsed = None
    else:
        elapsed = (current_time - previous_value.observed_datetime).total_seconds()
        if not math.isfinite(elapsed) or elapsed < 0.0:
            return CadenceDecision.HOLD.value
    if not changed and elapsed is not None and elapsed < policy.gate_seconds_intercept:
        return CadenceDecision.HOLD.value

    if (
        current_value.normalized_order_state in _TERMINAL_OR_SAFE_ORDER_STATES
        and current_value.candidate_present
        and current_value.hazard_score >= policy.allow_candidate_hazard_threshold
    ):
        return CadenceDecision.ALLOW_CANDIDATE.value
    return CadenceDecision.REEVALUATE.value


__all__ = [
    "CADENCE_GATE_SECONDS_KEY",
    "CadenceDecision",
    "CadenceSnapshot",
    "CadenceThresholds",
    "DEFAULT_ALLOW_CANDIDATE_HAZARD_THRESHOLD",
    "evaluate_cadence",
]
