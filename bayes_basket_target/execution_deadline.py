"""Typed execution-deadline contract for the target order lane.

The deadline is an execution admission fact.  It is deliberately separate
from quote freshness, probability, and strategy state.  This module has no
database or gateway dependency so both the atomic writer and the local
gateway adapter can enforce the same parsing and expiry semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import ContractError


TARGET_EXECUTION_HARD_DEADLINE_MS = 3_000


class TargetExecutionDeadlineError(ContractError):
    """Raised when a target execution deadline is not safely usable."""


def _parse_aware(value: Any, *, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise TargetExecutionDeadlineError(f"{name}_missing")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TargetExecutionDeadlineError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TargetExecutionDeadlineError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class TargetExecutionHardDeadline:
    """Canonical, timezone-aware target execution deadline."""

    at_utc: datetime

    def __post_init__(self) -> None:
        if self.at_utc.tzinfo is None or self.at_utc.utcoffset() is None:
            raise TargetExecutionDeadlineError("target_execution_hard_deadline_timezone_required")
        object.__setattr__(self, "at_utc", self.at_utc.astimezone(timezone.utc))

    @classmethod
    def from_value(cls, value: Any) -> "TargetExecutionHardDeadline":
        return cls(_parse_aware(value, name="target_execution_hard_deadline"))

    @classmethod
    def from_first_actionable(
        cls,
        first_actionable_at: Any,
        *,
        quote_expires_at: Any | None = None,
        deadline_ms: int = TARGET_EXECUTION_HARD_DEADLINE_MS,
    ) -> "TargetExecutionHardDeadline":
        first = _parse_aware(first_actionable_at, name="target_first_actionable")
        if isinstance(deadline_ms, bool) or int(deadline_ms) <= 0:
            raise TargetExecutionDeadlineError("target_execution_hard_deadline_policy_invalid")
        derived = first + timedelta(milliseconds=int(deadline_ms))
        if quote_expires_at is not None:
            expires = _parse_aware(quote_expires_at, name="target_quote_expires")
            if expires <= first:
                raise TargetExecutionDeadlineError("target_quote_expiry_invalid")
            derived = min(derived, expires)
        return cls(derived)

    @property
    def isoformat(self) -> str:
        return _iso(self.at_utc)

    def expired_at(self, now: Any) -> bool:
        current = _parse_aware(now, name="target_execution_check_time")
        return current >= self.at_utc

    def assert_not_expired(self, now: Any) -> None:
        if self.expired_at(now):
            raise TargetExecutionDeadlineError("target_execution_hard_deadline_expired")


def normalize_target_execution_hard_deadline(value: Any) -> str:
    """Return canonical UTC text, rejecting missing/naive/invalid values."""

    return TargetExecutionHardDeadline.from_value(value).isoformat


def derive_target_execution_hard_deadline(
    first_actionable_at: Any,
    *,
    quote_expires_at: Any | None = None,
) -> str:
    """Derive the fixed 3-second target admission deadline."""

    return TargetExecutionHardDeadline.from_first_actionable(
        first_actionable_at,
        quote_expires_at=quote_expires_at,
    ).isoformat


__all__ = [
    "TARGET_EXECUTION_HARD_DEADLINE_MS",
    "TargetExecutionDeadlineError",
    "TargetExecutionHardDeadline",
    "derive_target_execution_hard_deadline",
    "normalize_target_execution_hard_deadline",
]
