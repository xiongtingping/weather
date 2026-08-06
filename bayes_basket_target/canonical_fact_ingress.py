"""Retired PR12 legacy projection ingress.

Historical callers are kept fail-closed so an old script cannot recreate a
second observation writer.  New PR12 writes must use
``canonical_ingress.stage_awc_observation_contract``.
"""

from __future__ import annotations

from typing import Any

from .contracts import ContractError


SCHEMA = "bayes_basket_target_canonical_fact_ingress_retired_v1"
RETIRED_REASON = "canonical_observation_legacy_projection_ingress_retired"


def canonical_observation_fact_row(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise ContractError(RETIRED_REASON)


def ingest_canonical_observation_evidence(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise ContractError(RETIRED_REASON)


def stage_canonical_observation_evidence(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise ContractError(RETIRED_REASON)


__all__ = [
    "RETIRED_REASON",
    "SCHEMA",
    "canonical_observation_fact_row",
    "ingest_canonical_observation_evidence",
    "stage_canonical_observation_evidence",
]
