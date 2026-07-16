"""Classify source-observation lineage for strict replay without mutating facts."""

from __future__ import annotations

from typing import Any, Mapping


SCHEMA_VERSION = "source_observation_replay_eligibility_v1"
LEGACY_DERIVED_PARSER_VERSIONS = frozenset(
    {
        # These rows were projected from an already-built fact payload rather
        # than captured from an immutable provider response at first arrival.
        "posterior_ssot_projection_fact_feature_source_adapter_v1",
    }
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def classify_source_observation_replay_eligibility(value: Any) -> dict[str, Any]:
    """Return whether a row may count toward source-lineage replay checks.

    This classifier deliberately does not repair or reinterpret a legacy row.
    Detailed Tier-A forecast eligibility remains the responsibility of the
    strict capture contract; this only prevents post-hoc adapter rows from
    poisoning generic source-observation readiness counts.
    """

    row = _mapping(value)
    parser_version = _text(row.get("parser_version"))
    if parser_version in LEGACY_DERIVED_PARSER_VERSIONS:
        return {
            "schema": SCHEMA_VERSION,
            "state": "excluded_legacy_derived",
            "strict_source_lineage_candidate": False,
            "reason": "posthoc_fact_feature_adapter_not_raw_first_seen_source_observation",
            "parser_version": parser_version,
            "read_only": True,
        }
    return {
        "schema": SCHEMA_VERSION,
        "state": "candidate",
        "strict_source_lineage_candidate": True,
        "reason": "",
        "parser_version": parser_version,
        "read_only": True,
    }


__all__ = [
    "LEGACY_DERIVED_PARSER_VERSIONS",
    "SCHEMA_VERSION",
    "classify_source_observation_replay_eligibility",
]
