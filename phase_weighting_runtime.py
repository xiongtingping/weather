"""D1/D0 phase weighting runtime for hierarchical weather posterior.

This module owns the phase and beta schedule used by the hierarchical
temperature posterior. It is intentionally read-only: it does not write DB
state, create decisions, restart daemons, or place orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "phase_weighting_runtime_v1"
DEFAULT_STATION_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class PhaseScheduleEntry:
    phase: str
    beta_weak: float
    beta_strong: float
    phase_reason: str
    start_minute: int | None = None
    end_minute: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "beta_weak": self.beta_weak,
            "beta_strong": self.beta_strong,
            "phase_reason": self.phase_reason,
            "start_minute": self.start_minute,
            "end_minute": self.end_minute,
        }


DEFAULT_PHASE_BETA_SCHEDULE: tuple[PhaseScheduleEntry, ...] = (
    PhaseScheduleEntry(
        phase="D1_early",
        beta_weak=0.65,
        beta_strong=0.0,
        phase_reason="target_date_future_early_forecast_sources_dominate_but_distribution_stays_wide",
    ),
    PhaseScheduleEntry(
        phase="D1_late",
        beta_weak=0.85,
        beta_strong=0.0,
        phase_reason="target_date_next_day_forecasts_converged_weak_likelihood_weight_highest",
    ),
    PhaseScheduleEntry(
        phase="D0_pre_dawn",
        beta_weak=0.70,
        beta_strong=0.15,
        start_minute=0,
        end_minute=8 * 60,
        phase_reason="d0_pre_dawn_observation_informative_but_not_representative_of_daily_max",
    ),
    PhaseScheduleEntry(
        phase="D0_morning",
        beta_weak=0.55,
        beta_strong=0.35,
        start_minute=8 * 60,
        end_minute=10 * 60,
        phase_reason="d0_morning_low_cloud_visibility_dewpoint_start_to_carry_information",
    ),
    PhaseScheduleEntry(
        phase="D0_breakout_window",
        beta_weak=0.40,
        beta_strong=0.65,
        start_minute=10 * 60,
        end_minute=12 * 60,
        phase_reason="d0_chengdu_breakout_window_metar_strong_likelihood_starts_to_dominate",
    ),
    PhaseScheduleEntry(
        phase="D0_midday",
        beta_weak=0.20,
        beta_strong=0.85,
        start_minute=12 * 60,
        end_minute=14 * 60,
        phase_reason="d0_midday_current_temperature_state_and_momentum_constrain_daily_max",
    ),
    PhaseScheduleEntry(
        phase="D0_late",
        beta_weak=0.08,
        beta_strong=1.00,
        start_minute=14 * 60,
        end_minute=None,
        phase_reason="d0_late_remaining_warming_time_short_observation_sets_upper_bound",
    ),
    PhaseScheduleEntry(
        phase="D0_hard",
        beta_weak=0.0,
        beta_strong=1.0,
        phase_reason="hard_mask_or_settlement_fact_directly_constrains_distribution",
    ),
)

SCHEDULE_BY_PHASE = {entry.phase: entry for entry in DEFAULT_PHASE_BETA_SCHEDULE}
HARD_LOCK_REASONS = {
    "hard_source_direct_target_realized",
    "settlement_actual",
    "official_final_result",
    "target_realization_lock",
    "hard_source_lock",
}


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def phase_weighting_contract() -> dict[str, Any]:
    return {
        "schema": "phase_weighting_contract_v1",
        "read_only": True,
        "runtime_schema": SCHEMA_VERSION,
        "owner": "phase_weighting_runtime.build_phase_weighting_snapshot",
        "formula_position": "likelihood_tempering_not_source_trust",
        "required_output_fields": [
            "phase",
            "beta_weak",
            "beta_strong",
            "phase_reason",
            "hard_mask_reason",
        ],
        "field_sources": {
            "phase": "station_local_event_date_and_as_of_time_or_hard_lock",
            "beta_weak": "DEFAULT_PHASE_BETA_SCHEDULE_or_explicit_reviewed_override",
            "beta_strong": "DEFAULT_PHASE_BETA_SCHEDULE_or_explicit_reviewed_override",
            "phase_reason": "selected_schedule_entry_reason_plus_override_reason_if_any",
            "hard_mask_reason": "hard_source_or_settlement_or_observed_max_signal_summary",
        },
        "schedule": [entry.as_dict() for entry in DEFAULT_PHASE_BETA_SCHEDULE],
        "awc_windows": {
            "hour_plus_0_to_plus_5": {
                "window": "awc_preparation_window",
                "can_place_orders": False,
                "allowed_actions": ["market_prefetch", "market_mapping", "orderbook", "positions", "capital_caps"],
            },
            "hour_plus_5_to_plus_6": {
                "window": "awc_hard_source_signal_window",
                "can_place_orders": "only_after_accepted_awc_hard_source_signal_and_auto_trade_decision",
                "slo_ms": 3000,
            },
        },
        "constraints": {
            "beta_weak_and_beta_strong_need_not_sum_to_one": True,
            "hard_mask_not_tempered_by_beta": True,
            "weak_sources_never_upgrade_to_hard_observation": True,
            "metar_internal_fields_must_not_be_counted_as_independent_likelihoods": True,
            "phase_fields_are_fact_snapshot_contract_fields": True,
        },
        "guards": _guards(),
    }


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError("event_date_required")
    return date.fromisoformat(text[:10])


def _station_timezone(name: str | None) -> ZoneInfo:
    return ZoneInfo(str(name or DEFAULT_STATION_TIMEZONE))


def _parse_as_of(as_of: Any, station_tz: ZoneInfo) -> tuple[datetime, datetime]:
    if as_of is None or as_of == "":
        local_dt = datetime.now(station_tz)
    elif isinstance(as_of, datetime):
        local_dt = as_of if as_of.tzinfo else as_of.replace(tzinfo=station_tz)
        local_dt = local_dt.astimezone(station_tz)
    else:
        text = str(as_of).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=station_tz)
        local_dt = parsed.astimezone(station_tz)
    return local_dt, local_dt.astimezone(timezone.utc)


def _minute_of_day(dt: datetime) -> int:
    return int(dt.hour) * 60 + int(dt.minute)


def _d0_entry_for_time(local_dt: datetime) -> PhaseScheduleEntry:
    minute = _minute_of_day(local_dt)
    for entry in DEFAULT_PHASE_BETA_SCHEDULE:
        if not entry.phase.startswith("D0_") or entry.phase == "D0_hard":
            continue
        start = entry.start_minute
        end = entry.end_minute
        if start is None:
            continue
        if minute >= start and (end is None or minute < end):
            return entry
    return SCHEDULE_BY_PHASE["D0_late"]


def _d1_entry_for_date(event_day: date, local_dt: datetime) -> PhaseScheduleEntry:
    days_to_event = (event_day - local_dt.date()).days
    if days_to_event <= 1 and local_dt.time() >= time(12, 0):
        return SCHEDULE_BY_PHASE["D1_late"]
    return SCHEDULE_BY_PHASE["D1_early"]


def awc_release_window_for_time(local_dt: datetime) -> dict[str, Any]:
    minute_after_hour = int(local_dt.minute)
    second_after_minute = int(local_dt.second)
    if 0 <= minute_after_hour < 5:
        window = "awc_preparation_window"
        reason = "hour_boundary_to_plus_5m_prepare_orderbook_positions_risk_without_ordering"
    elif minute_after_hour == 5:
        window = "awc_hard_source_signal_window"
        reason = "plus_5m_to_plus_6m_awc_hard_source_signal_acceptance_window"
    else:
        window = "outside_awc_hard_source_signal_window"
        reason = "not_in_awc_preparation_or_plus_5m_signal_window"
    return {
        "window": window,
        "minute_after_hour": minute_after_hour,
        "second_after_minute": second_after_minute,
        "reason": reason,
    }


def _hard_mask_reason_from_signals(hard_mask_signals: Mapping[str, Any] | None) -> tuple[bool, str, list[str]]:
    signals = dict(hard_mask_signals or {})
    reasons: list[str] = []
    hard_lock = _to_bool(signals.get("hard_lock"))
    explicit_reason = str(signals.get("hard_mask_reason") or "").strip()
    if explicit_reason:
        explicit_parts = [part.strip() for part in explicit_reason.split("+") if part.strip()]
        reasons.extend(explicit_parts or [explicit_reason])
        if explicit_reason in HARD_LOCK_REASONS or any(part in HARD_LOCK_REASONS for part in explicit_parts):
            hard_lock = True
    if _to_bool(signals.get("hard_source_direct_target_realized")):
        hard_lock = True
        reasons.append("hard_source_direct_target_realized")
    if signals.get("settlement_actual") not in (None, ""):
        hard_lock = True
        reasons.append("settlement_actual")
    if signals.get("official_final_result") not in (None, ""):
        hard_lock = True
        reasons.append("official_final_result")
    if signals.get("observed_today_max_c") not in (None, ""):
        reasons.append("observed_today_max_c")
    if signals.get("current_temperature_c") not in (None, ""):
        reasons.append("current_temperature_c")
    if signals.get("masked_bucket_count") not in (None, "", 0, "0"):
        reasons.append("masked_bucket_count")
    if not reasons:
        return hard_lock, "none", []
    unique = list(dict.fromkeys(str(reason) for reason in reasons if str(reason)))
    return hard_lock, "+".join(unique), unique


def build_phase_weighting_snapshot(
    *,
    event_date: Any,
    as_of: Any = None,
    station_timezone: str = DEFAULT_STATION_TIMEZONE,
    hard_mask_signals: Mapping[str, Any] | None = None,
    phase_override: str = "",
    beta_override: Mapping[str, Any] | None = None,
    include_schedule: bool = True,
) -> dict[str, Any]:
    station_tz = _station_timezone(station_timezone)
    event_day = _to_date(event_date)
    local_dt, utc_dt = _parse_as_of(as_of, station_tz)
    hard_lock, hard_mask_reason, hard_mask_reason_parts = _hard_mask_reason_from_signals(hard_mask_signals)
    phase_reason_suffix = ""

    if phase_override:
        entry = SCHEDULE_BY_PHASE.get(str(phase_override), SCHEDULE_BY_PHASE["D0_hard"] if hard_lock else _d0_entry_for_time(local_dt))
        phase_reason_suffix = f"phase_override:{phase_override}"
    elif hard_lock:
        entry = SCHEDULE_BY_PHASE["D0_hard"]
    elif local_dt.date() < event_day:
        entry = _d1_entry_for_date(event_day, local_dt)
    elif local_dt.date() == event_day:
        entry = _d0_entry_for_time(local_dt)
    else:
        entry = PhaseScheduleEntry(
            phase="post_event_pending_settlement",
            beta_weak=0.0,
            beta_strong=1.0,
            phase_reason="event_date_past_without_settlement_lock_waiting_for_official_fact",
        )

    beta_weak = float(entry.beta_weak)
    beta_strong = float(entry.beta_strong)
    beta_source = "default_schedule"
    if beta_override:
        if beta_override.get("beta_weak") is not None:
            beta_weak = max(0.0, float(beta_override["beta_weak"]))
            beta_source = "explicit_override"
        if beta_override.get("beta_strong") is not None:
            beta_strong = max(0.0, float(beta_override["beta_strong"]))
            beta_source = "explicit_override"

    phase_reason = entry.phase_reason
    if phase_reason_suffix:
        phase_reason = f"{phase_reason};{phase_reason_suffix}"
    if hard_lock and entry.phase != "D0_hard":
        phase_reason = f"{phase_reason};hard_lock_signal_present"

    awc_window = awc_release_window_for_time(local_dt)
    schedule_rows = [item.as_dict() for item in DEFAULT_PHASE_BETA_SCHEDULE] if include_schedule else []
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "read_only": True,
        "production_entry_enabled": False,
        "guards": _guards(),
        "event_date": event_day.isoformat(),
        "station_timezone": str(station_tz.key),
        "as_of_local": local_dt.isoformat(),
        "as_of_utc": utc_dt.isoformat().replace("+00:00", "Z"),
        "phase": entry.phase,
        "beta_weak": beta_weak,
        "beta_strong": beta_strong,
        "phase_reason": phase_reason,
        "hard_mask_reason": hard_mask_reason,
        "hard_mask_reason_parts": hard_mask_reason_parts,
        "hard_lock_active": bool(hard_lock),
        "beta_source": beta_source,
        "beta_constraints": {
            "beta_weak_and_beta_strong_need_not_sum_to_one": True,
            "hard_mask_not_tempered_by_beta": True,
            "weak_sources_never_upgrade_to_hard_observation": True,
            "metar_internal_fields_must_not_be_counted_as_independent_likelihoods": True,
        },
        "awc_release_window": awc_window,
        "schedule": schedule_rows,
        "next_boundary": "sequential_remaining_warming_bayes_runtime_no_standalone_production_entry",
    }


__all__ = [
    "DEFAULT_PHASE_BETA_SCHEDULE",
    "DEFAULT_STATION_TIMEZONE",
    "SCHEMA_VERSION",
    "awc_release_window_for_time",
    "build_phase_weighting_snapshot",
    "phase_weighting_contract",
]
