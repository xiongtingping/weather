"""Orderless production candidate compiler for the unique Bayes SSOT path.

This module is the production-facing boundary for mathematical inference.  It
accepts only a canonical observation already persisted in the archive and a
fully frozen evidence/predecessor context, then returns one immutable
Transaction-B candidate.  It does not open a connection, mutate SQLite,
refresh forecasts, read markets, make a decision, or access any execution
credential.  The sole production writer is the separate Transaction-B host.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from sequential_bayes_shadow_pipeline import (
    SCHEMA_VERSION as CANDIDATE_PIPELINE_SCHEMA,
    build_canonical_event_transaction_b_candidate,
)


SCHEMA_VERSION = "production_sequential_bayes_candidate_compiler_v1"


def compile_production_transaction_b_candidate(
    conn: sqlite3.Connection,
    **frozen_inputs: Any,
) -> dict[str, Any]:
    """Compile an explicit production-mode candidate without side effects.

    Callers are forbidden from supplying a shadow marker.  This prevents a
    shadow-prepared bundle from crossing the production commit boundary by
    configuration accident.
    """

    inputs = dict(frozen_inputs)
    if "candidate_mode" in inputs or "shadow_only" in inputs:
        raise ValueError("production_candidate_compiler_mode_is_fixed")
    candidate = build_canonical_event_transaction_b_candidate(
        conn,
        candidate_mode="production",
        **inputs,
    )
    if candidate.get("candidate_mode") == "production" and candidate.get("shadow_only") is not False:
        raise RuntimeError("production_candidate_compiler_shadow_marker_invalid")
    checkpoint_row = candidate.get("checkpoint_row")
    if isinstance(checkpoint_row, Mapping):
        # The shared materializer is storage-shape code, not authority.  Its
        # legacy shadow state marker must not cross the production compiler.
        checkpoint_row = dict(checkpoint_row)
        # Compilation prepares an immutable production candidate; only the
        # Transaction-B writer may turn it into a committed checkpoint.
        checkpoint_row["state"] = "production_ready"
        candidate = {**candidate, "checkpoint_row": checkpoint_row}
    return {
        **candidate,
        "schema": SCHEMA_VERSION,
        "candidate_pipeline_schema": CANDIDATE_PIPELINE_SCHEMA,
        "production_candidate_compiler": True,
        "places_orders": False,
        "clob": False,
        "boundary": {
            "sqlite_mutation": False,
            "network": False,
            "forecast_refresh": False,
            "market_read": False,
            "decision": False,
            "execution": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "compile_production_transaction_b_candidate"]
