"""Versioned cash-phase and order-sizing contracts for production no-live.

This module intentionally contains no source-wallet amounts, database, network,
gateway, scheduler, or service imports.  A caller must provide an explicit
``BasketCashPhasePolicyArtifact``; missing, stale, unknown, or unobserved
policy data fails closed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError, content_sha256


EPS = 1e-9
PHASES = ("HEAVY", "STANDARD", "TAIL")
PHASE_RANK = {name: index for index, name in enumerate(PHASES)}
EVIDENCE_STATUSES = {"DESIGN_CANDIDATE", "OFFLINE_VALIDATED", "PROMOTED"}


class CashPhase(str, Enum):
    HEAVY = "HEAVY"
    STANDARD = "STANDARD"
    TAIL = "TAIL"


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise ContractError(f"{name}_invalid")
    return result


def _nonnegative(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result < 0:
        raise ContractError(f"{name}_negative")
    return result


def _positive(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result <= 0:
        raise ContractError(f"{name}_nonpositive")
    return result


def _date(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    return result


def _datetime(name: str, value: Any) -> datetime:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed


def _utc_iso(name: str, value: Any) -> str:
    parsed = _datetime(name, value).astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def _time(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = time.fromisoformat(result)
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_hhmm") from exc
    if parsed.second or parsed.microsecond:
        raise ContractError(f"{name}_must_be_minute_boundary")
    return parsed.strftime("%H:%M")


def _zone(name: str, value: Any) -> ZoneInfo:
    text = _text(name, value)
    try:
        return ZoneInfo(text)
    except ZoneInfoNotFoundError as exc:
        raise ContractError(f"{name}_unknown_iana_timezone") from exc


def _sha(value: Any) -> str:
    result = _text("content_sha256", value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError("content_sha256_invalid")
    return result


def _boolean(name: str, value: Any, *, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    return value


def _floor_step(value: float, step: float) -> float:
    """Floor a positive quantity to an explicit exchange share quantum."""

    if value <= EPS:
        return 0.0
    return round(max(0.0, math.floor((value + EPS) / step) * step), 10)


@dataclass(frozen=True)
class BasketCashPhasePolicyArtifact:
    """Explicit self-owned cash policy; source-wallet values are never defaults."""

    policy_version: str
    station_id: str
    station_timezone: str
    heavy_to_standard_local_time: str
    standard_to_tail_local_time: str
    cash_by_phase: Mapping[str, float]
    valid_from_utc: str
    valid_until_utc: str
    evidence_status: str
    content_sha256: str
    promoted: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BasketCashPhasePolicyArtifact":
        if not isinstance(raw, Mapping):
            raise ContractError("cash_phase_policy_mapping_required")
        version = _text("policy_version", raw.get("policy_version"))
        station = _text("station_id", raw.get("station_id"))
        timezone_name = _text("station_timezone", raw.get("station_timezone"))
        _zone("station_timezone", timezone_name)
        heavy_time = _time("heavy_to_standard_local_time", raw.get("heavy_to_standard_local_time"))
        tail_time = _time("standard_to_tail_local_time", raw.get("standard_to_tail_local_time"))
        phase_raw = raw.get("cash_by_phase")
        if not isinstance(phase_raw, Mapping) or not phase_raw:
            raise ContractError("cash_by_phase_missing")
        unknown = set(str(key) for key in phase_raw) - set(PHASES)
        if unknown:
            raise ContractError("cash_by_phase_unknown_phase")
        cash_by_phase = {str(key): _positive(f"cash_by_phase_{key}", value) for key, value in phase_raw.items()}
        valid_from = _datetime("valid_from_utc", raw.get("valid_from_utc"))
        valid_until = _datetime("valid_until_utc", raw.get("valid_until_utc"))
        if valid_from >= valid_until:
            raise ContractError("cash_phase_policy_valid_window_invalid")
        evidence_status = _text("evidence_status", raw.get("evidence_status")).upper()
        if evidence_status not in EVIDENCE_STATUSES:
            raise ContractError("cash_phase_policy_evidence_status_invalid")
        promoted = _boolean("promoted", raw.get("promoted"), default=False)
        identity = {
            "schema": "basket_cash_phase_policy_artifact_v1",
            "policy_version": version,
            "station_id": station,
            "station_timezone": timezone_name,
            "heavy_to_standard_local_time": heavy_time,
            "standard_to_tail_local_time": tail_time,
            "cash_by_phase": cash_by_phase,
            "valid_from_utc": valid_from.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "valid_until_utc": valid_until.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "evidence_status": evidence_status,
            "promoted": promoted,
        }
        supplied_sha = _sha(raw.get("content_sha256"))
        if supplied_sha != content_sha256(identity):
            raise ContractError("cash_phase_policy_content_sha_mismatch")
        return cls(
            policy_version=version,
            station_id=station,
            station_timezone=timezone_name,
            heavy_to_standard_local_time=heavy_time,
            standard_to_tail_local_time=tail_time,
            cash_by_phase=cash_by_phase,
            valid_from_utc=identity["valid_from_utc"],
            valid_until_utc=identity["valid_until_utc"],
            evidence_status=evidence_status,
            content_sha256=supplied_sha,
            promoted=promoted,
        )

    @classmethod
    def create(cls, **kwargs: Any) -> "BasketCashPhasePolicyArtifact":
        raw = dict(kwargs)
        raw.setdefault("promoted", False)
        raw["promoted"] = _boolean("promoted", raw.get("promoted"), default=False)
        raw["heavy_to_standard_local_time"] = _time(
            "heavy_to_standard_local_time", raw.get("heavy_to_standard_local_time")
        )
        raw["standard_to_tail_local_time"] = _time(
            "standard_to_tail_local_time", raw.get("standard_to_tail_local_time")
        )
        raw["valid_from_utc"] = _utc_iso("valid_from_utc", raw.get("valid_from_utc"))
        raw["valid_until_utc"] = _utc_iso("valid_until_utc", raw.get("valid_until_utc"))
        raw["evidence_status"] = _text("evidence_status", raw.get("evidence_status")).upper()
        raw["cash_by_phase"] = dict(raw.get("cash_by_phase") or {})
        raw["content_sha256"] = content_sha256(
            {
                "schema": "basket_cash_phase_policy_artifact_v1",
                "policy_version": raw.get("policy_version"),
                "station_id": raw.get("station_id"),
                "station_timezone": raw.get("station_timezone"),
                "heavy_to_standard_local_time": raw.get("heavy_to_standard_local_time"),
                "standard_to_tail_local_time": raw.get("standard_to_tail_local_time"),
                "cash_by_phase": dict(raw.get("cash_by_phase") or {}),
                "valid_from_utc": raw.get("valid_from_utc"),
                "valid_until_utc": raw.get("valid_until_utc"),
                "evidence_status": str(raw.get("evidence_status") or "").upper(),
            "promoted": raw["promoted"],
            }
        )
        return cls.from_mapping(raw)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": "basket_cash_phase_policy_artifact_v1",
            "policy_version": self.policy_version,
            "station_id": self.station_id,
            "station_timezone": self.station_timezone,
            "heavy_to_standard_local_time": self.heavy_to_standard_local_time,
            "standard_to_tail_local_time": self.standard_to_tail_local_time,
            "cash_by_phase": dict(self.cash_by_phase),
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "evidence_status": self.evidence_status,
            "promoted": self.promoted,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_dict(), "content_sha256": self.content_sha256}


@dataclass(frozen=True)
class BasketCashPhaseInput:
    policy_version: str
    station_id: str
    current_phase: CashPhase
    event_date_local: str
    observed_at_utc: str
    station_timezone: str
    checkpoint_id: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BasketCashPhaseInput":
        raw_phase = raw.get("current_phase")
        try:
            phase = raw_phase if isinstance(raw_phase, CashPhase) else CashPhase(str(raw_phase or ""))
        except ValueError as exc:
            raise ContractError("current_cash_phase_invalid") from exc
        _datetime("observed_at_utc", raw.get("observed_at_utc"))
        _date("event_date_local", raw.get("event_date_local"))
        _zone("station_timezone", raw.get("station_timezone"))
        return cls(
            policy_version=_text("policy_version", raw.get("policy_version")),
            station_id=_text("station_id", raw.get("station_id")),
            current_phase=phase,
            event_date_local=str(raw.get("event_date_local")),
            observed_at_utc=_utc_iso("observed_at_utc", raw.get("observed_at_utc")),
            station_timezone=_text("station_timezone", raw.get("station_timezone")),
            checkpoint_id=_text("checkpoint_id", raw.get("checkpoint_id")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "current_phase": self.current_phase.value}


@dataclass(frozen=True)
class BasketCashPhaseDecision:
    policy_version: str
    policy_sha256: str
    checkpoint_id: str
    previous_phase: CashPhase
    clock_phase: CashPhase
    next_phase: CashPhase
    transitioned: bool
    observed_at_local: str
    heavy_to_standard_at_local: str
    standard_to_tail_at_local: str
    cash_template_usdc: float
    reason_code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "basket_cash_phase_decision_v1",
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
            "checkpoint_id": self.checkpoint_id,
            "previous_phase": self.previous_phase.value,
            "clock_phase": self.clock_phase.value,
            "next_phase": self.next_phase.value,
            "transitioned": self.transitioned,
            "observed_at_local": self.observed_at_local,
            "heavy_to_standard_at_local": self.heavy_to_standard_at_local,
            "standard_to_tail_at_local": self.standard_to_tail_at_local,
            "cash_template_usdc": self.cash_template_usdc,
            "reason_code": self.reason_code,
        }


def evaluate_cash_phase(
    policy: BasketCashPhasePolicyArtifact | Mapping[str, Any],
    value: BasketCashPhaseInput | Mapping[str, Any],
) -> BasketCashPhaseDecision:
    artifact = policy if isinstance(policy, BasketCashPhasePolicyArtifact) else BasketCashPhasePolicyArtifact.from_mapping(policy)
    phase_input = value if isinstance(value, BasketCashPhaseInput) else BasketCashPhaseInput.from_mapping(value)
    if phase_input.policy_version != artifact.policy_version:
        raise ContractError("cash_phase_policy_version_mismatch")
    if phase_input.station_id != artifact.station_id:
        raise ContractError("cash_phase_station_mismatch")
    if phase_input.station_timezone != artifact.station_timezone:
        raise ContractError("cash_phase_timezone_lineage_mismatch")
    observed = _datetime("observed_at_utc", phase_input.observed_at_utc)
    valid_from = _datetime("valid_from_utc", artifact.valid_from_utc)
    valid_until = _datetime("valid_until_utc", artifact.valid_until_utc)
    if not (valid_from <= observed < valid_until):
        raise ContractError("cash_phase_policy_outside_validity")
    zone = _zone("station_timezone", artifact.station_timezone)
    event_date = date.fromisoformat(phase_input.event_date_local)
    observed_local = observed.astimezone(zone)
    heavy_clock = datetime.combine(
        event_date + timedelta(days=-1),
        time.fromisoformat(artifact.heavy_to_standard_local_time),
        tzinfo=zone,
    )
    tail_clock = datetime.combine(
        event_date,
        time.fromisoformat(artifact.standard_to_tail_local_time),
        tzinfo=zone,
    )
    if heavy_clock >= tail_clock:
        raise ContractError("cash_phase_cutoffs_not_increasing")
    if observed_local >= tail_clock:
        clock_phase = CashPhase.TAIL
    elif observed_local >= heavy_clock:
        clock_phase = CashPhase.STANDARD
    else:
        clock_phase = CashPhase.HEAVY
    next_phase = max(
        (phase_input.current_phase, clock_phase),
        key=lambda item: PHASE_RANK[item.value],
    )
    cash = artifact.cash_by_phase.get(next_phase.value)
    if cash is None:
        raise ContractError("cash_phase_unobserved_phase_fail_closed")
    current_rank = PHASE_RANK[phase_input.current_phase.value]
    next_rank = PHASE_RANK[next_phase.value]
    transitioned = next_rank > current_rank
    if next_rank - current_rank > 1:
        reason = "CLOCK_CATCH_UP_MULTI_PHASE"
    elif transitioned:
        reason = "CLOCK_THRESHOLD_ADVANCE"
    elif PHASE_RANK[clock_phase.value] < current_rank:
        reason = "MONOTONIC_HOLD_NO_REGRESSION"
    else:
        reason = "CLOCK_PHASE_UNCHANGED"
    return BasketCashPhaseDecision(
        policy_version=artifact.policy_version,
        policy_sha256=artifact.content_sha256,
        checkpoint_id=phase_input.checkpoint_id,
        previous_phase=phase_input.current_phase,
        clock_phase=clock_phase,
        next_phase=next_phase,
        transitioned=transitioned,
        observed_at_local=observed_local.isoformat(),
        heavy_to_standard_at_local=heavy_clock.isoformat(),
        standard_to_tail_at_local=tail_clock.isoformat(),
        cash_template_usdc=float(cash),
        reason_code=reason,
    )


@dataclass(frozen=True)
class OrderSizingDecision:
    bucket: str
    policy_sha256: str
    phase_decision_sha256: str
    delta_shares: float
    depth_shares_cap: float
    target_clip_notional_usdc: float | None
    minimum_clip_notional_usdc: float
    cash_template_caps_target: bool
    share_quantum: float
    r_limit: float
    c_base: float
    remaining_budget_before: float
    executable_depth_cash: float
    raw_c_order: float
    c_order: float
    q_order: float
    remaining_budget_after: float
    candidate_present: bool
    eligible: bool
    reason_code: str
    content_sha256: str

    @classmethod
    def compute(
        cls,
        *,
        bucket: str,
        phase_decision: BasketCashPhaseDecision,
        delta_shares: float,
        r_limit: float,
        remaining_budget: float,
        executable_depth_cash: float,
        candidate_present: bool,
        target_clip_notional_usdc: float | None = None,
        minimum_clip_notional_usdc: float = 0.0,
        cash_template_caps_target: bool = True,
        share_quantum: float = 0.01,
    ) -> "OrderSizingDecision":
        bucket = _text("bucket", bucket)
        delta = _nonnegative("delta_shares", delta_shares)
        limit = _nonnegative("r_limit", r_limit)
        budget = _nonnegative("remaining_budget", remaining_budget)
        depth = _nonnegative("executable_depth_cash", executable_depth_cash)
        minimum_clip = _nonnegative("minimum_clip_notional_usdc", minimum_clip_notional_usdc)
        if not isinstance(cash_template_caps_target, bool):
            raise ContractError("cash_template_caps_target_invalid")
        quantum = _positive("share_quantum", share_quantum)
        if target_clip_notional_usdc is not None:
            target_clip = _nonnegative("target_clip_notional_usdc", target_clip_notional_usdc)
        else:
            target_clip = None
        c_base = _positive("cash_template_usdc", phase_decision.cash_template_usdc)
        reason = "sized"
        if not isinstance(candidate_present, bool):
            raise ContractError("candidate_present_invalid")
        eligible = candidate_present
        if not candidate_present:
            eligible = False
            reason = "execution_candidate_missing"
        elif limit <= EPS:
            eligible = False
            reason = "r_limit_nonpositive"
        elif delta <= EPS:
            eligible = False
            reason = "no_positive_inventory_gap"
        elif budget <= EPS:
            eligible = False
            reason = "remaining_budget_exhausted"
        elif depth <= EPS:
            eligible = False
            reason = "executable_depth_empty"
        elif target_clip is not None and target_clip <= EPS:
            eligible = False
            reason = "target_clip_empty"
        raw_c_order = 0.0
        c_order = 0.0
        depth_shares_cap = depth / limit if limit > EPS else 0.0
        if eligible:
            caps = [limit * delta, budget, depth]
            if cash_template_caps_target or target_clip is None:
                caps.append(c_base)
            if target_clip is not None:
                caps.append(target_clip)
            raw_c_order = min(caps)
            raw_q_order = min(delta, depth_shares_cap, raw_c_order / limit) if limit > EPS else 0.0
            q_order = _floor_step(raw_q_order, quantum)
            c_order = q_order * limit
            if raw_c_order <= EPS or q_order <= EPS:
                eligible = False
                reason = "share_quantum_rounds_to_zero" if raw_c_order > EPS else "cash_clip_zero_after_constraints"
                c_order = 0.0
                q_order = 0.0
            elif c_order + EPS < minimum_clip:
                eligible = False
                reason = "clip_below_minimum"
                c_order = 0.0
                q_order = 0.0
            else:
                reason = "sized_discrete"
        else:
            q_order = 0.0
        q_order = c_order / limit if limit > EPS else 0.0
        if abs(c_order - q_order * limit) > 1e-8:
            raise ContractError("cash_share_identity_broken")
        after = max(0.0, budget - c_order)
        identity = {
            "bucket": bucket,
            "policy_sha256": phase_decision.policy_sha256,
            "phase_decision_sha256": content_sha256(phase_decision.to_dict()),
            "delta_shares": delta,
            "depth_shares_cap": depth_shares_cap,
            "target_clip_notional_usdc": target_clip,
            "minimum_clip_notional_usdc": minimum_clip,
            "cash_template_caps_target": cash_template_caps_target,
            "share_quantum": quantum,
            "r_limit": limit,
            "c_base": c_base,
            "remaining_budget_before": budget,
            "executable_depth_cash": depth,
            "raw_c_order": raw_c_order,
            "c_order": c_order,
            "q_order": q_order,
            "remaining_budget_after": after,
            "candidate_present": bool(candidate_present),
            "eligible": eligible,
            "reason_code": reason,
        }
        content_identity = {"schema": "basket_order_sizing_decision_v1", **identity}
        return cls(
            **identity,
            content_sha256=content_sha256(content_identity),
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "schema": "basket_order_sizing_decision_v1"}


def resolve_cash_phase_policy(
    artifacts: Sequence[BasketCashPhasePolicyArtifact | Mapping[str, Any]],
    *,
    policy_version: str,
    station_id: str,
    observed_at_utc: str,
    required_authority_scope: str = "FORMAL_PROMOTED",
) -> BasketCashPhasePolicyArtifact:
    """Resolve one exact, valid policy; gaps and ambiguity fail closed."""

    from .policy import POLICY_AUTHORITY_SCOPES

    authority_scope = str(required_authority_scope or "").strip().upper()
    if authority_scope not in POLICY_AUTHORITY_SCOPES:
        raise ContractError("cash_phase_policy_authority_scope_invalid")
    if authority_scope == "FORMAL_PROMOTED":
        raise ContractError(
            "cash_phase_formal_resolution_requires_manifest_and_oos_receipt"
        )
    rows = [item if isinstance(item, BasketCashPhasePolicyArtifact) else BasketCashPhasePolicyArtifact.from_mapping(item) for item in artifacts]
    observed = _datetime("observed_at_utc", observed_at_utc)
    matches = [
        item for item in rows
        if item.policy_version == policy_version and item.station_id == station_id
        and _datetime("valid_from_utc", item.valid_from_utc) <= observed < _datetime("valid_until_utc", item.valid_until_utc)
        and (item.promoted or authority_scope == "PRODUCTION_NO_LIVE_CANDIDATE")
    ]
    if len(matches) != 1:
        raise ContractError("cash_phase_policy_resolution_not_unique")
    return matches[0]


__all__ = [
    "BasketCashPhaseDecision",
    "BasketCashPhaseInput",
    "BasketCashPhasePolicyArtifact",
    "CashPhase",
    "OrderSizingDecision",
    "evaluate_cash_phase",
    "resolve_cash_phase_policy",
]
