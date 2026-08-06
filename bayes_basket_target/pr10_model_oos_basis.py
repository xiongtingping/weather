"""PR10 810-scope V13 model-OOS terminal basis.

This is the one gap-ledger projection for the existing MODEL_OOS expected
scope basis.  It never creates facts and never treats legacy OOS rows as V13.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .contracts import ContractError, content_sha256
from .model_oos_contract import CanonicalModelOOSCohortFact, CanonicalModelOOSEventFact
from .resource_bounds import load_bounded_json


SCHEMA = "bayes_basket_target_pr10_model_oos_terminal_basis_v1"
BUILDER_ID = "pr10.v13_model_oos_expected_scope_basis.v1"
V13_NATURAL_KEY_FIELDS = {
    "station_id", "model_id", "ensemble_system_id", "model_family",
    "provider_id", "provider_product_id", "independence_family_id",
    "source_class", "authority_scope", "settlement_timezone", "lead_bucket",
    "lead_bucket_scope", "run_cycle", "run_cycle_scope",
    "event_oos_protocol_version",
}


def _load_facts(conn: sqlite3.Connection, fact_type: str, cls: Any) -> list[Any]:
    result: list[Any] = []
    for (raw_json,) in conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type=? ORDER BY scope_key",
        (fact_type,),
    ):
        try:
            raw = json.loads(str(raw_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("pr10_model_oos_fact_json_invalid") from exc
        result.append(cls.from_mapping(raw))
    return result


def build_pr10_model_oos_terminal_basis(
    conn: sqlite3.Connection,
    *,
    expected_basis_path: Path | str,
    as_of_utc: str,
) -> dict[str, Any]:
    inspection = inspect_canonical_ssot_schema(conn)
    if inspection.get("state") not in {"V13", "V14"} or inspection.get("errors"):
        raise ContractError("pr10_model_oos_basis_v13_required")
    expected = load_bounded_json(expected_basis_path)
    if not isinstance(expected, Mapping) or expected.get("scope_type") != "MODEL_OOS":
        raise ContractError("pr10_model_oos_expected_basis_invalid")
    supplied = str(expected.get("content_sha256") or "")
    if supplied != content_sha256({key: value for key, value in expected.items() if key != "content_sha256"}):
        raise ContractError("pr10_model_oos_expected_basis_hash_mismatch")
    keys = expected.get("expected_keys")
    if not isinstance(keys, list) or not keys:
        raise ContractError("pr10_model_oos_expected_keys_required")

    invalid_key_count = sum(
        not isinstance(item, Mapping)
        or not isinstance(item.get("natural_key"), Mapping)
        or set(item["natural_key"]) != V13_NATURAL_KEY_FIELDS
        for item in keys
    )
    if invalid_key_count:
        identity = {
            "schema": SCHEMA,
            "builder_id": BUILDER_ID,
            "as_of_utc": as_of_utc,
            "state": "BLOCKED",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "expected_basis_content_sha256": supplied,
            "legacy_declared_scope_count": len(keys),
            "expected_scope_count": 0,
            "oos_ready_scope_count": 0,
            "blocked_scope_count": 0,
            "ready_scopes": [],
            "terminal_scopes": [],
            "blocker_counts": {"CANONICAL_SCOPE_MISMATCH": 1},
            "unresolved_input_receipts": [{
                "state": "BLOCKED",
                "blocker": "CANONICAL_SCOPE_MISMATCH",
                "invalid_key_count": invalid_key_count,
                "required_natural_key_fields": sorted(V13_NATURAL_KEY_FIELDS),
            }],
            "writes_database": False,
            "writes_pointer": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "production_no_live": True,
            "consumable": False,
        }
        return {**identity, "content_sha256": content_sha256(identity)}

    events = _load_facts(conn, "model_oos_event", CanonicalModelOOSEventFact)
    cohorts = _load_facts(conn, "model_oos_cohort", CanonicalModelOOSCohortFact)
    terminals: list[dict[str, Any]] = []
    ready: list[dict[str, Any]] = []
    for raw_key in keys:
        if not isinstance(raw_key, Mapping) or raw_key.get("scope_kind") != "BUILDER":
            raise ContractError("pr10_model_oos_expected_key_invalid")
        natural = raw_key.get("natural_key")
        if not isinstance(natural, Mapping) or set(natural) != V13_NATURAL_KEY_FIELDS:
            raise ContractError("pr10_model_oos_natural_key_invalid")
        matching_events = [event for event in events if all(
            event.raw[field] == natural[field]
            for field in V13_NATURAL_KEY_FIELDS
        )]
        matching_cohorts = [cohort for cohort in cohorts if all(
            cohort.raw[field] == natural[field]
            for field in V13_NATURAL_KEY_FIELDS
        )]
        passing = [cohort for cohort in matching_cohorts if cohort.raw["evaluation_state"] == "OOS_PASS"]
        row = {
            "natural_key": dict(natural),
            "event_count": len(matching_events),
            "cohort_count": len(matching_cohorts),
            "oos_pass_cohort_count": len(passing),
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "formal_live_authority": False,
        }
        if passing:
            ready.append({
                **row,
                "state": "OOS_READY",
                "cohort_content_sha256s": sorted(item.content_sha256 for item in passing),
            })
        else:
            blocker = "LINEAGE_MISMATCH" if not matching_events else "OOS_SAMPLE_SHORT"
            terminals.append({
                **row,
                "state": "BLOCKED",
                "blocker": blocker,
                "required_train_count": 20,
                "required_holdout_count": 8,
            })
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": as_of_utc,
        "state": "BLOCKED" if terminals else "OOS_READY",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "expected_basis_content_sha256": supplied,
        "expected_scope_count": len(keys),
        "oos_ready_scope_count": len(ready),
        "blocked_scope_count": len(terminals),
        "ready_scopes": ready,
        "terminal_scopes": terminals,
        "blocker_counts": {
            value: sum(row["blocker"] == value for row in terminals)
            for value in sorted({row["blocker"] for row in terminals})
        },
        "writes_database": False,
        "writes_pointer": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
        "consumable": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = ["SCHEMA", "build_pr10_model_oos_terminal_basis"]
