"""Migration registry for legacy additive Bayes evidence fields.

The registry is a read-only contract used by Phase 0 audits and later migration
work. It classifies old evidence fields into the new hierarchical posterior
layers without allowing them to remain production probability channels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "legacy_evidence_migration_registry_v1"

STATUS_MIGRATE = "migrate"
STATUS_RETIRE_COMPAT = "retired_compat"
STATUS_DIAGNOSTIC_ONLY = "diagnostic_only"
STATUS_UNCLASSIFIED = "unclassified"

TARGET_FORECAST_WEAK = "forecast_weak_likelihood"
TARGET_METAR_STRONG = "metar_strong_likelihood_internal"
TARGET_ROOT_PRIOR = "root_prior_weather_type_adjustment"
TARGET_DIAGNOSTIC = "diagnostic_only"
TARGET_NONE = "none"

LEGACY_SUFFIX_RE = re.compile(
    r"(?P<base>.+?_evidence)(?P<suffix>_(?:raw|base|scale|scale_value|scale_reason|scale_details|enabled|posterior_enabled|prob|meta|pp_cap|pp_cap_hit|pp_max_delta|pp_raw_max_delta))$"
)


@dataclass(frozen=True)
class LegacyEvidenceMigration:
    canonical_field: str
    status: str
    target_layer: str
    target_runtime: str
    replacement_field: str
    cleanup_action: str
    reason: str
    production_allowed: bool = False
    ui_primary_allowed: bool = False
    aliases: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "canonical_field": self.canonical_field,
            "status": self.status,
            "target_layer": self.target_layer,
            "target_runtime": self.target_runtime,
            "replacement_field": self.replacement_field,
            "cleanup_action": self.cleanup_action,
            "reason": self.reason,
            "production_allowed": self.production_allowed,
            "ui_primary_allowed": self.ui_primary_allowed,
            "aliases": list(self.aliases),
        }


LEGACY_EVIDENCE_MIGRATIONS: tuple[LegacyEvidenceMigration, ...] = (
    LegacyEvidenceMigration(
        canonical_field="q_bayes_model_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_FORECAST_WEAK,
        target_runtime="forecast_weak_likelihood_runtime.py",
        replacement_field="layer_contributions.forecast_weak_likelihood",
        cleanup_action="move_into_forecast_weak_source_group_then_retire_field",
        reason="ECMWF/GFS/ICON/Meteoblue and model blend are prediction sources, not hard observations.",
        aliases=("model_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_wu_forecast_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_FORECAST_WEAK,
        target_runtime="forecast_weak_likelihood_runtime.py",
        replacement_field="layer_contributions.forecast_weak_likelihood",
        cleanup_action="move_into_forecast_weak_source_group_then_retire_field",
        reason="WU forecast belongs to the prediction weak likelihood with source-group shrinkage.",
        aliases=("wu_forecast_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_weather_com_forecast_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_FORECAST_WEAK,
        target_runtime="forecast_weak_likelihood_runtime.py",
        replacement_field="layer_contributions.forecast_weak_likelihood",
        cleanup_action="move_into_forecast_weak_source_group_then_retire_field",
        reason="Weather.com/TWC forecast is a weak prediction source; current/display data is diagnostic unless explicitly accepted elsewhere.",
        aliases=("weather_com_forecast_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_taf_peak_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_FORECAST_WEAK,
        target_runtime="forecast_weak_likelihood_runtime.py",
        replacement_field="layer_contributions.forecast_weak_likelihood",
        cleanup_action="move_into_forecast_weak_source_group_then_retire_field",
        reason="TAF TX/TN is a forecast baseline, not a root prior or hard observation.",
        aliases=("taf_peak_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_observed_nowcast_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_METAR_STRONG,
        target_runtime="metar_strong_likelihood_runtime.py",
        replacement_field="hard_constraints + layer_contributions.metar_strong_likelihood",
        cleanup_action="split_hard_observed_floor_from_remaining_warming_likelihood_then_retire_field",
        reason="Observed nowcast overlaps current temperature, observed max, and trajectory evidence; it must be deduplicated inside the observation layer.",
        aliases=("observed_nowcast_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_nowcast_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_METAR_STRONG,
        target_runtime="metar_strong_likelihood_runtime.py",
        replacement_field="hard_constraints + layer_contributions.metar_strong_likelihood",
        cleanup_action="dedupe_with_observed_nowcast_and_metar_state_then_retire_field",
        reason="Nowcast evidence is observation-derived and must not remain an independent likelihood channel.",
        aliases=("nowcast_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_temperature_trajectory_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_METAR_STRONG,
        target_runtime="metar_strong_likelihood_runtime.py",
        replacement_field="remaining_warming_distribution:P(delta_t_remaining | S_t, M_t, time)",
        cleanup_action="move_into_remaining_warming_distribution_then_retire_field",
        reason="Temperature trajectory and momentum are results of the environment state, not an independent same-level evidence vote.",
        aliases=("temperature_trajectory_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_physical_likelihood_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_METAR_STRONG,
        target_runtime="metar_strong_likelihood_runtime.py",
        replacement_field="state_evidence_trace + layer_contributions.metar_strong_likelihood",
        cleanup_action="move_physical_groups_into_environment_state_trace_then_retire_field",
        reason="Cloud, rain, radiation, dewpoint, wind, and pressure must be deduplicated through S0/S1/S2/S3 state inference.",
        aliases=("physical_likelihood_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_independent_physics_residual_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_METAR_STRONG,
        target_runtime="metar_strong_likelihood_runtime.py",
        replacement_field="state_evidence_trace + dedupe_trace",
        cleanup_action="move_to_state_evidence_trace_then_retire_residual_field",
        reason="Independent physics residual is exactly the double-counting risk the layered model removes.",
        aliases=("independent_physics_residual_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_climo_regime_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_ROOT_PRIOR,
        target_runtime="temperature_prior_runtime.py",
        replacement_field="prior_weather_type_adjustment",
        cleanup_action="move_into_weather_type_conditioned_prior_then_retire_field",
        reason="Climate regime belongs in the historical/date/site/terrain/weather-type prior, not in the D0 evidence pool.",
        aliases=("climo_regime_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_post_cool_rebound_regime_prior_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_ROOT_PRIOR,
        target_runtime="temperature_prior_runtime.py",
        replacement_field="prior_weather_type_adjustment",
        cleanup_action="map_to_weather_type_conditioned_prior_or_retire_if_not_validated",
        reason="Post-cool rebound regime prior can only survive as a weather-type prior adjustment.",
        aliases=("post_cool_rebound_regime_prior_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_post_cool_rebound_intraday_likelihood_evidence",
        status=STATUS_MIGRATE,
        target_layer=TARGET_METAR_STRONG,
        target_runtime="metar_strong_likelihood_runtime.py",
        replacement_field="S3_convective_cutoff_or_state_risk_diagnostic",
        cleanup_action="map_to_state_transition_or_mark_diagnostic_then_retire_field",
        reason="Intraday rebound/cutoff logic must be expressed as an environment-state transition, not as a third probability channel.",
        aliases=("post_cool_rebound_intraday_likelihood_evidence",),
    ),
    LegacyEvidenceMigration(
        canonical_field="q_bayes_post_cool_rebound_risk_evidence",
        status=STATUS_DIAGNOSTIC_ONLY,
        target_layer=TARGET_DIAGNOSTIC,
        target_runtime="weather_posterior_audit.py",
        replacement_field="risk_explanation.post_cool_rebound",
        cleanup_action="keep_only_as_diagnostic_if_still_useful",
        reason="Risk fields may explain uncertainty but cannot trigger production probability or trading decisions.",
        aliases=("post_cool_rebound_risk_evidence",),
    ),
)


def _build_field_index() -> dict[str, LegacyEvidenceMigration]:
    out: dict[str, LegacyEvidenceMigration] = {}
    for item in LEGACY_EVIDENCE_MIGRATIONS:
        out[item.canonical_field] = item
        for alias in item.aliases:
            out[alias] = item
    return out


FIELD_INDEX = _build_field_index()


def normalize_legacy_evidence_field(field: str) -> tuple[str, str]:
    raw = str(field or "").strip()
    if not raw:
        return "", ""
    match = LEGACY_SUFFIX_RE.match(raw)
    if match:
        return match.group("base"), match.group("suffix").lstrip("_")
    return raw, ""


def classify_legacy_evidence_field(field: str) -> dict[str, Any]:
    canonical_or_base, suffix = normalize_legacy_evidence_field(field)
    item = FIELD_INDEX.get(canonical_or_base)
    if not item:
        return {
            "schema": SCHEMA_VERSION,
            "field": str(field or "").strip(),
            "canonical_field": canonical_or_base,
            "field_suffix": suffix,
            "status": STATUS_UNCLASSIFIED,
            "target_layer": TARGET_NONE,
            "target_runtime": "",
            "replacement_field": "",
            "cleanup_action": "add_registry_classification_before_migration",
            "reason": "legacy_evidence_field_not_in_registry",
            "production_allowed": False,
            "ui_primary_allowed": False,
            "known": False,
        }
    out = item.as_dict()
    out.update(
        {
            "field": str(field or "").strip(),
            "field_suffix": suffix,
            "known": True,
        }
    )
    return out


def legacy_evidence_registry_summary(fields: Iterable[str] | None = None) -> dict[str, Any]:
    items = [
        classify_legacy_evidence_field(field)
        for field in (fields if fields is not None else [item.canonical_field for item in LEGACY_EVIDENCE_MIGRATIONS])
    ]
    by_status: dict[str, int] = {}
    by_target_layer: dict[str, int] = {}
    unclassified: list[str] = []
    production_allowed: list[str] = []
    ui_primary_allowed: list[str] = []
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        by_target_layer[item["target_layer"]] = by_target_layer.get(item["target_layer"], 0) + 1
        if item["status"] == STATUS_UNCLASSIFIED:
            unclassified.append(item["field"])
        if bool(item.get("production_allowed")):
            production_allowed.append(item["field"])
        if bool(item.get("ui_primary_allowed")):
            ui_primary_allowed.append(item["field"])
    return {
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "registry_count": len(LEGACY_EVIDENCE_MIGRATIONS),
        "field_count": len(items),
        "by_status": dict(sorted(by_status.items())),
        "by_target_layer": dict(sorted(by_target_layer.items())),
        "unclassified_fields": sorted(set(unclassified)),
        "production_allowed_fields": sorted(set(production_allowed)),
        "ui_primary_allowed_fields": sorted(set(ui_primary_allowed)),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def registry_rows() -> list[dict[str, Any]]:
    return [item.as_dict() for item in LEGACY_EVIDENCE_MIGRATIONS]


__all__ = [
    "LEGACY_EVIDENCE_MIGRATIONS",
    "SCHEMA_VERSION",
    "STATUS_DIAGNOSTIC_ONLY",
    "STATUS_MIGRATE",
    "STATUS_RETIRE_COMPAT",
    "STATUS_UNCLASSIFIED",
    "classify_legacy_evidence_field",
    "legacy_evidence_registry_summary",
    "normalize_legacy_evidence_field",
    "registry_rows",
]
