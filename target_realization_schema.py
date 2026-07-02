from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional


SCHEMA_VERSION = "target_realization_alias_v1"


# Canonical "target bucket realization" fields mapped to legacy edge_* fields.
# Phase 1 is read-path aliasing only: legacy fields remain the runtime source
# of truth while new readers can consume target_realization_* names.
FIELD_ALIASES: Dict[str, str] = {
    "target_realization_signal_alpha": "edge_signal_alpha",
    "target_realization_signal_lcb": "edge_signal_lcb",
    "target_realization_signal_sigma": "edge_signal_sigma",
    "target_realization_signal_confidence": "edge_signal_confidence",
    "target_realization_signal_q_delta": "edge_signal_components_q_edge",
    "target_realization_signal_trend": "edge_signal_components_trend",
    "target_realization_signal_trend_consistency": "edge_signal_components_trend_consistency",
    "target_realization_signal_model_spread": "edge_signal_components_model_spread",
    "target_realization_signal_freshness_sec": "edge_signal_components_freshness_sec",
    "target_realization_path_p_reach": "edge_path_p_reach",
    "target_realization_path_lcb": "edge_path_p_reach_lcb",
    "target_realization_path_ucb": "edge_path_p_reach_ucb",
    "target_realization_path_sample_n": "edge_path_sample_n",
    "target_realization_path_bucket_key": "edge_path_bucket_key",
    "target_realization_path_source": "edge_path_source",
    "target_realization_diagnostic_value": "edge_realizable",
    "target_realization_diagnostic_score": "edge_realizable_score",
    "target_realization_v2_value": "edge_realizable_v2",
    "target_realization_v2_score": "edge_realizable_score_v2",
    "target_realization_p_realize": "edge_realizable_p_realize",
    "target_realization_p_converge": "edge_realizable_p_converge",
    "target_realization_p_fill_entry": "edge_realizable_p_fill_entry",
    "target_realization_p_fill_exit": "edge_realizable_p_fill_exit",
    "target_realization_hold_cost": "edge_realizable_hold_cost",
    "target_realization_bucket_key": "edge_realizable_bucket_key",
    "target_realization_target_exit_price": "edge_realizable_target_exit_price",
    "target_realization_delta_exit": "edge_realizable_delta_exit",
    "target_realization_tradeability_pass": "edge_tradeability_pass",
    "target_realization_tradeability_blocked": "edge_tradeability_blocked",
    "target_realization_tradeability_shadow_block": "edge_tradeability_shadow_block",
    "target_realization_tradeability_reasons": "edge_tradeability_reasons",
    "target_realization_tradeability_mode": "edge_tradeability_mode",
    "target_realization_tradeability_policy_enforce": "edge_tradeability_policy_enforce",
    "target_realization_tradeability_lcb": "edge_tradeability_profit_value",
    "target_realization_execution_expected": "edge_tradeability_edge_expected",
    "target_realization_execution_lcb": "edge_tradeability_edge_lcb",
    "target_realization_execution_ucb": "edge_tradeability_edge_ucb",
    "target_realization_execution_miss_cost": "edge_tradeability_miss_cost",
    "target_realization_execution_slippage_buffer": "edge_tradeability_slippage_buffer",
    "target_realization_execution_floor_mode": "edge_tradeability_edge_floor_mode",
    "target_realization_signal_lcb_min": "edge_tradeability_signal_lcb_min",
    "target_realization_path_lcb_min": "edge_tradeability_path_lcb_min",
    "target_realization_tradeability_min": "edge_tradeability_profit_min",
    "target_realization_execution_lcb_min": "edge_tradeability_edge_lcb_min",
    "target_execution_margin_entry": "exec_edge_entry",
    "target_execution_margin_exit": "exec_edge_exit",
    "target_execution_margin_score": "exec_edge_score",
    "target_execution_entry_price": "exec_entry_price",
    "target_execution_exit_price": "exec_exit_price",
    "target_probability_delta": "q_edge",
}

LEGACY_TO_CANONICAL: Dict[str, str] = {legacy: canonical for canonical, legacy in FIELD_ALIASES.items()}
DEFAULT_SUFFIXES = ("", "_yes", "_no")


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def legacy_field_for(canonical_field: str, suffix: str = "") -> Optional[str]:
    legacy = FIELD_ALIASES.get(str(canonical_field or ""))
    if legacy is None:
        return None
    return f"{legacy}{suffix}"


def canonical_field_for(legacy_field: str, suffix: str = "") -> Optional[str]:
    canonical = LEGACY_TO_CANONICAL.get(str(legacy_field or ""))
    if canonical is None:
        return None
    return f"{canonical}{suffix}"


def copy_legacy_to_canonical(
    row: MutableMapping[str, Any],
    *,
    suffixes: Iterable[str] = DEFAULT_SUFFIXES,
    overwrite: bool = False,
) -> MutableMapping[str, Any]:
    if not isinstance(row, MutableMapping):
        return row
    for suffix in suffixes:
        suffix_text = str(suffix or "")
        for canonical, legacy in FIELD_ALIASES.items():
            canonical_key = f"{canonical}{suffix_text}"
            legacy_key = f"{legacy}{suffix_text}"
            if not overwrite and _has_value(row.get(canonical_key)):
                continue
            value = row.get(legacy_key)
            if _has_value(value):
                row[canonical_key] = value
    return row


def copy_canonical_to_legacy(
    row: MutableMapping[str, Any],
    *,
    suffixes: Iterable[str] = DEFAULT_SUFFIXES,
    overwrite: bool = False,
) -> MutableMapping[str, Any]:
    if not isinstance(row, MutableMapping):
        return row
    for suffix in suffixes:
        suffix_text = str(suffix or "")
        for canonical, legacy in FIELD_ALIASES.items():
            canonical_key = f"{canonical}{suffix_text}"
            legacy_key = f"{legacy}{suffix_text}"
            if not overwrite and _has_value(row.get(legacy_key)):
                continue
            value = row.get(canonical_key)
            if _has_value(value):
                row[legacy_key] = value
    return row


def apply_target_realization_aliases(
    row: MutableMapping[str, Any],
    *,
    suffixes: Iterable[str] = DEFAULT_SUFFIXES,
    include_meta: bool = True,
) -> MutableMapping[str, Any]:
    copy_legacy_to_canonical(row, suffixes=suffixes, overwrite=False)
    if include_meta and isinstance(row, MutableMapping):
        row.setdefault("target_realization_schema_version", SCHEMA_VERSION)
        row.setdefault("target_realization_legacy_prefix", "edge_*")
        row.setdefault("target_realization_alias_mode", "legacy_read_alias")
    return row


def alias_snapshot() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "legacy_read_alias",
        "canonical_to_legacy": dict(FIELD_ALIASES),
    }


def missing_canonical_aliases(row: Mapping[str, Any], *, suffix: str = "") -> Dict[str, str]:
    missing: Dict[str, str] = {}
    suffix_text = str(suffix or "")
    for canonical, legacy in FIELD_ALIASES.items():
        legacy_key = f"{legacy}{suffix_text}"
        canonical_key = f"{canonical}{suffix_text}"
        if _has_value(row.get(legacy_key)) and not _has_value(row.get(canonical_key)):
            missing[canonical_key] = legacy_key
    return missing
