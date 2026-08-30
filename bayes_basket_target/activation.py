"""Immutable activation contract for the isolated target no-live lane."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .policy import POLICY_AUTHORITY_SCOPES
from .quote_freshness import QuoteFreshnessPolicy
from .resource_bounds import load_bounded_json


SCHEMA = "bayes_basket_target_production_no_live_activation_v1"
DEFAULT_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "trading"
    / "bayes_basket_target_production_no_live_activation_v1.json"
)
REQUIRED_SCOPE_KEYS = ("season_bin", "maturity_bin", "liquidity_bin", "strategy_version")
REQUIRED_INPUT_KEYS = frozenset(
    {
        "strict_asof_current_model_runs",
        "production_no_live_only_calibration_activations",
        "exact_available_model_set_ensemble_mask",
        "materialized_stage_probability_envelope",
        "materialized_regime_artifact",
        "official_clob_ws_quote_snapshot",
        "resolved_station_policy_activation",
        "confirmed_inventory_snapshot",
        "production_no_live_order_state_snapshot",
        "basket_cash_phase_authority_manifest",
        "content_addressed_quote_freshness_policy",
    }
)
TARGET_STRATEGY_ID = "bayes_basket_target_v1"


def validate_target_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    supplied = str(row.pop("activation_contract_sha256", "") or "").strip()
    if row.get("schema") != SCHEMA:
        raise ContractError("target_activation_schema_invalid")
    if row.get("state") not in {"production_no_live", "enabled"}:
        raise ContractError("target_activation_state_invalid")
    if row.get("execution_mode") != "production_no_live":
        raise ContractError("target_activation_execution_mode_invalid")
    if row.get("target_only") is not True:
        raise ContractError("target_activation_target_only_required")
    for key in (
        "authorizes_order",
        "live_decision_write_authorized",
        "live_order_authorized",
        "live_submit_authorized",
        "production_mutation",
        "places_orders",
        "writes_production_db",
        "calls_order_gateway",
        "network_access",
    ):
        if row.get(key) is not False:
            raise ContractError(f"target_activation_{key}_must_be_false")
    required_inputs = row.get("required_inputs")
    if not isinstance(required_inputs, list) or not required_inputs:
        raise ContractError("target_activation_required_inputs_missing")
    missing_inputs = sorted(
        REQUIRED_INPUT_KEYS.difference(str(value) for value in required_inputs)
    )
    if missing_inputs:
        raise ContractError(
            "target_activation_required_inputs_incomplete:"
            + ",".join(missing_inputs)
        )
    policy_authority_scope = str(row.get("policy_authority_scope") or "").strip().upper()
    if policy_authority_scope not in POLICY_AUTHORITY_SCOPES:
        raise ContractError("target_activation_policy_authority_scope_invalid")
    row["policy_authority_scope"] = policy_authority_scope
    quote_freshness_policy = QuoteFreshnessPolicy.from_mapping(
        row.get("quote_freshness_policy")
    )
    if quote_freshness_policy.authority_scope != policy_authority_scope:
        raise ContractError(
            "target_activation_quote_freshness_authority_scope_mismatch"
        )
    scope = row.get("target_scope")
    if not isinstance(scope, Mapping):
        raise ContractError("target_activation_target_scope_missing")
    for key in REQUIRED_SCOPE_KEYS:
        if not str(scope.get(key) or "").strip():
            raise ContractError(f"target_activation_scope_{key}_missing")
    if len(supplied) != 64 or supplied != content_sha256(row):
        raise ContractError("target_activation_contract_sha256_mismatch")
    return {**row, "activation_contract_sha256": supplied}


LIVE_SCHEMA = "bayes_basket_target_production_live_activation_v1"
LIVE_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "trading"
    / "bayes_basket_target_production_live_activation_v1.json"
)
# The live contract keeps the no-live policy scope on purpose: the field
# selects which station-policy artifacts may be resolved, not whether orders
# may be sent, and every stored artifact carries this one.  See the note in
# scripts/go_live_migration.py::mint_live_activation.
LIVE_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"


def validate_live_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    """The live contract, validated separately rather than by relaxing the other.

    ``validate_target_activation`` above is deliberately untouched: it is what
    production runs today and what every replay is scored against, and a
    version of it that could also pass a live contract would no longer prove
    anything about the no-live lane.  The two live side by side and
    ``load_activation`` dispatches on the schema.

    The asymmetry in what is demanded is the point.  The no-live contract
    proves nine boundaries false; this one requires the seven that an order
    actually needs to be true, and still holds ``production_mutation`` and
    ``writes_production_db`` false -- the target lane owns its own database
    and borrowing the resident's was never part of going live.  A contract
    that turned every boundary on at once would authorise things nobody
    decided to authorise.
    """

    row = dict(value)
    supplied = str(row.pop("activation_contract_sha256", "") or "").strip()
    if row.get("schema") != LIVE_SCHEMA:
        raise ContractError("live_activation_schema_invalid")
    if row.get("state") != "production_live":
        raise ContractError("live_activation_state_invalid")
    if row.get("execution_mode") != "production_live":
        raise ContractError("live_activation_execution_mode_invalid")
    if row.get("target_only") is not True:
        raise ContractError("live_activation_target_only_required")
    for key in (
        "authorizes_order",
        "live_decision_write_authorized",
        "live_order_authorized",
        "live_submit_authorized",
        "places_orders",
        "calls_order_gateway",
        "network_access",
    ):
        if row.get(key) is not True:
            raise ContractError(f"live_activation_{key}_must_be_true")
    for key in ("production_mutation", "writes_production_db"):
        if row.get(key) is not False:
            raise ContractError(f"live_activation_{key}_must_be_false")
    required_inputs = row.get("required_inputs")
    if not isinstance(required_inputs, list) or not required_inputs:
        raise ContractError("live_activation_required_inputs_missing")
    missing_inputs = sorted(
        REQUIRED_INPUT_KEYS.difference(str(item) for item in required_inputs)
    )
    if missing_inputs:
        raise ContractError(
            "live_activation_required_inputs_incomplete:" + ",".join(missing_inputs)
        )
    if str(row.get("policy_authority_scope") or "") != LIVE_AUTHORITY_SCOPE:
        raise ContractError("live_activation_policy_authority_scope_invalid")
    quote_freshness_policy = QuoteFreshnessPolicy.from_mapping(
        row.get("quote_freshness_policy")
    )
    if quote_freshness_policy.authority_scope != LIVE_AUTHORITY_SCOPE:
        raise ContractError("live_activation_quote_freshness_authority_scope_mismatch")
    scope = row.get("target_scope")
    if not isinstance(scope, Mapping):
        raise ContractError("live_activation_target_scope_missing")
    for key in REQUIRED_SCOPE_KEYS:
        if not str(scope.get(key) or "").strip():
            raise ContractError(f"live_activation_scope_{key}_missing")
    # The seal is what makes the mint auditable: a hand-edited boundary
    # changes the hash, and a contract whose hash does not match its body is
    # refused rather than obeyed.
    if len(supplied) != 64 or supplied != content_sha256(row):
        raise ContractError("live_activation_contract_sha256_mismatch")
    return {**row, "activation_contract_sha256": supplied}


def load_activation(path: Path | str) -> dict[str, Any]:
    """Load either contract, choosing the validator by the file's own schema.

    Never guesses: a file that names neither schema is refused rather than
    given the benefit of the no-live validator, because the failure mode of
    guessing wrong here is a live contract being checked against the rules
    for a lane that cannot trade.
    """

    try:
        parsed = load_bounded_json(Path(path))
    except Exception as exc:
        raise ContractError("activation_json_invalid") from exc
    if not isinstance(parsed, Mapping):
        raise ContractError("activation_mapping_required")
    schema = str(parsed.get("schema") or "")
    if schema == SCHEMA:
        return validate_target_activation(parsed)
    if schema == LIVE_SCHEMA:
        return validate_live_activation(parsed)
    raise ContractError("activation_schema_unknown:" + (schema or "missing"))


def load_target_activation(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    try:
        parsed = load_bounded_json(Path(path))
    except Exception as exc:
        raise ContractError("target_activation_json_invalid") from exc
    if not isinstance(parsed, Mapping):
        raise ContractError("target_activation_mapping_required")
    return validate_target_activation(parsed)


def assert_target_database_owner(conn: sqlite3.Connection) -> None:
    """Refuse target mutation when the shared DB is owned by a live lane."""

    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "basket_strategy_activation_state" not in tables:
        return
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(basket_strategy_activation_state)")
    }
    if not {"strategy_id", "activation_json"}.issubset(columns):
        return
    row = conn.execute(
        "SELECT strategy_id,activation_json FROM basket_strategy_activation_state "
        "WHERE singleton=1 ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return
    try:
        details = json.loads(str(row[1] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("target_shared_activation_owner_state_invalid") from exc
    if not isinstance(details, Mapping):
        raise ContractError("target_shared_activation_owner_state_invalid")
    is_live = (
        details.get("execution_mode") == "production_live"
        or details.get("live_submit_authorized") is True
        or details.get("places_orders") is True
        or details.get("calls_order_gateway") is True
    )
    if str(row[0] or "") != TARGET_STRATEGY_ID and is_live:
        raise ContractError("target_shared_live_activation_owner_conflict")


__all__ = [
    "DEFAULT_PATH",
    "LIVE_AUTHORITY_SCOPE",
    "LIVE_PATH",
    "LIVE_SCHEMA",
    "load_activation",
    "validate_live_activation",
    "REQUIRED_INPUT_KEYS",
    "REQUIRED_SCOPE_KEYS",
    "SCHEMA",
    "TARGET_STRATEGY_ID",
    "assert_target_database_owner",
    "load_target_activation",
    "validate_target_activation",
]
