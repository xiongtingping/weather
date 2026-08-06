"""Unified, no-write MODEL_OOS basis for PR05--PR09.

This is a *ledger*, not another OOS event format.  It consumes only already
content-addressed domain bundles and keeps their fact schemas separate:

* V13 ``model_oos_event`` facts remain the only cohort-eligible events;
* PR07 member facts remain member facts until their own exact event adapter
  exists; and
* incomplete source terminals remain terminal-only instead of being joined by
  product aliases, station/date, or defaults.

The result makes the remaining 20 train + 8 holdout gap visible per exact
lead/model-family scope while PR04 stays frozen.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .model_oos_contract import CanonicalModelOOSContract
from .model_product_identity import (
    REGISTRY_CONTENT_SHA256,
    validate_model_product_identity_receipt,
)
from .resource_bounds import (
    MAX_INPUT_FILES_PER_BATCH,
    MAX_JSON_INPUT_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
)


SCHEMA = "bayes_basket_target_pr05_pr09_model_oos_basis_v1"
BUILDER_ID = "pr05-pr09.canonical_model_oos_basis.v1"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
STATE = "MATERIALIZED_UNPUBLISHED"
RELEASE_STATE = "BLOCKED"

REPUBLISHER_SCHEMA = "bayes_basket_target_republisher_historical_event_bundle_v1"
REPUBLISHER_COHORT_SCHEMA = "bayes_basket_target_pr10_exact_28_cohort_bundle_v1"
ICON_EPS_SCHEMA = "bayes_basket_target_pr07_icon_eps_basis_bundle_v1"
NATIVE_ENSEMBLE_SCHEMA = "bayes_basket_target_pr06_pr09_native_ensemble_et_gap_bundle_v1"
AIFS_SCHEMA = "bayes_basket_target_pr06_aifs_single_historical_terminal_bundle_v1"
COVERAGE_SCHEMA = "bayes_basket_target_pr05_pr09_v13_model_oos_scope_bundle_v1"
REGIONAL_RETAINED_SCHEMA = (
    "bayes_basket_target_pr05_pr09_regional_retained_terminal_bundle_v1"
)
ICON_LEGACY_SCHEMA = "bayes_basket_target_pr05_pr09_icon_legacy_terminal_bundle_v1"

V13_SCOPE_FIELDS = (
    "station_id", "model_id", "ensemble_system_id", "model_family",
    "provider_id", "provider_product_id", "independence_family_id",
    "source_class", "authority_scope", "settlement_timezone", "lead_bucket",
    "lead_bucket_scope", "run_cycle", "run_cycle_scope",
    "event_oos_protocol_version",
)
_NULLABLE_SCOPE_FIELDS = frozenset({"ensemble_system_id", "run_cycle"})


class PR05PR09ModelOOSBasisError(ContractError):
    """A source bundle cannot enter the unified no-live gap ledger."""


def _utc(value: Any, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PR05PR09ModelOOSBasisError(f"pr05_pr09_model_oos_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PR05PR09ModelOOSBasisError(
            f"pr05_pr09_model_oos_{label}_timezone_required"
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _sha(value: Any, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise PR05PR09ModelOOSBasisError(f"pr05_pr09_model_oos_{label}_invalid")
    return result


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PR05PR09ModelOOSBasisError(f"pr05_pr09_model_oos_{label}_mapping_required")
    return dict(value)


def _verified_hash(raw: Mapping[str, Any], label: str) -> str:
    supplied = _sha(raw.get("content_sha256"), f"{label}_content_sha256")
    if supplied != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise PR05PR09ModelOOSBasisError(
            f"pr05_pr09_model_oos_{label}_content_sha256_mismatch"
        )
    return supplied


def _load_artifact(
    path: Path | str, *, role: str, schema: str, as_of_utc: str
) -> tuple[dict[str, Any], dict[str, str]]:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        raw = load_bounded_json(source, limit=MAX_JSON_INPUT_BYTES)
    except (OSError, ValueError) as exc:
        raise PR05PR09ModelOOSBasisError(
            f"pr05_pr09_model_oos_{role}_load_failed"
        ) from exc
    value = _mapping(raw, role)
    artifact_sha = _verified_hash(value, role)
    source_as_of = _utc(value.get("as_of_utc"), f"{role}_as_of_utc")
    if (
        value.get("schema") != schema
        or value.get("authority_scope") != AUTHORITY_SCOPE
        or source_as_of > as_of_utc
        or value.get("release_state") != RELEASE_STATE
        or value.get("consumable") is not False
    ):
        raise PR05PR09ModelOOSBasisError(
            f"pr05_pr09_model_oos_{role}_semantics_invalid"
        )
    return value, {
        "artifact_role": role,
        "schema": schema,
        "content_sha256": artifact_sha,
        "source_as_of_utc": source_as_of,
    }


def _load_cohort_artifact(
    path: Path | str, *, as_of_utc: str
) -> tuple[dict[str, Any], dict[str, str]]:
    source = Path(path).expanduser().resolve(strict=True)
    raw = _mapping(
        load_bounded_json(source, limit=MAX_JSON_INPUT_BYTES),
        "republisher_cohorts",
    )
    artifact_sha = _verified_hash(raw, "republisher_cohorts")
    source_as_of = _utc(raw.get("as_of_utc"), "republisher_cohorts_as_of_utc")
    if (
        raw.get("schema") != REPUBLISHER_COHORT_SCHEMA
        or raw.get("authority_scope") != AUTHORITY_SCOPE
        or raw.get("state") != STATE
        or raw.get("consumable") is not False
        or source_as_of > as_of_utc
    ):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_republisher_cohorts_semantics_invalid"
        )
    return raw, {
        "artifact_role": "republisher_cohorts",
        "schema": REPUBLISHER_COHORT_SCHEMA,
        "content_sha256": artifact_sha,
        "source_as_of_utc": source_as_of,
    }


def _identity(raw: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    missing = [
        field for field in V13_SCOPE_FIELDS
        if field not in raw
        or (raw[field] is None and field not in _NULLABLE_SCOPE_FIELDS)
    ]
    if missing:
        raise PR05PR09ModelOOSBasisError(
            f"pr05_pr09_model_oos_{label}_canonical_scope_missing"
        )
    value = {field: raw[field] for field in V13_SCOPE_FIELDS}
    if value["authority_scope"] != AUTHORITY_SCOPE:
        raise PR05PR09ModelOOSBasisError(
            f"pr05_pr09_model_oos_{label}_authority_invalid"
        )
    return value


def _scope_token(identity: Mapping[str, Any]) -> str:
    """Hash only the canonical identity; evidence roles are not identity."""

    return content_sha256({"scope_identity": dict(identity)})


def _terminal_reference(raw: Mapping[str, Any], artifact_sha: str) -> dict[str, Any]:
    terminal_sha = _verified_hash(raw, "source_terminal")
    return {
        "source_artifact_content_sha256": artifact_sha,
        "terminal_content_sha256": terminal_sha,
        "blocker": str(raw.get("blocker") or ""),
        "diagnostic": str(raw.get("diagnostic") or ""),
    }


def _append_unscoped_terminal(
    target: list[dict[str, Any]], *, raw: Mapping[str, Any], artifact: Mapping[str, str]
) -> None:
    missing = [
        field for field in V13_SCOPE_FIELDS
        if field not in raw
        or (raw[field] is None and field not in _NULLABLE_SCOPE_FIELDS)
    ]
    target.append({
        "source_artifact_content_sha256": artifact["content_sha256"],
        "source_schema": artifact["schema"],
        "terminal_content_sha256": _verified_hash(raw, "unscoped_terminal"),
        "blocker": str(raw.get("blocker") or ""),
        "diagnostic": str(raw.get("diagnostic") or ""),
        "known_station_id": raw.get("station_id"),
        "known_event_date": raw.get("event_date"),
        "known_product_id": raw.get("product_id") or raw.get("model_product_id"),
        "missing_canonical_scope_fields": missing,
        "state": "BLOCKED",
    })


def _event_scope_rows(
    artifact: Mapping[str, Any], artifact_sha: str
) -> list[dict[str, Any]]:
    contracts = artifact.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise PR05PR09ModelOOSBasisError("pr05_pr09_model_oos_republisher_contracts_missing")
    result: list[dict[str, Any]] = []
    seen_events: set[str] = set()
    for raw_contract in contracts:
        contract = CanonicalModelOOSContract.from_mapping(
            _mapping(raw_contract, "republisher_contract")
        )
        for event in contract.event_facts:
            event_sha = event.content_sha256
            if event_sha in seen_events:
                raise PR05PR09ModelOOSBasisError(
                    "pr05_pr09_model_oos_republisher_event_duplicate"
                )
            seen_events.add(event_sha)
            result.append({
                # The exact V13 natural identity is intentionally the same
                # key emitted by a coverage identity receipt.  This is not a
                # product-name join: every V13 identity field is present.
                "scope_kind": "CANONICAL_MODEL_OOS_SCOPE",
                "scope_identity": _identity(event.raw, label="republisher_event"),
                "event_date": event.raw["event_date"],
                "fact_content_sha256": event_sha,
                "source_artifact_content_sha256": artifact_sha,
            })
    declared_count = artifact.get("event_count")
    if declared_count != len(result):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_republisher_event_count_mismatch"
        )
    return result


def _cohort_scope_rows(
    artifact: Mapping[str, Any], artifact_sha: str
) -> list[dict[str, Any]]:
    contracts = artifact.get("contracts")
    if not isinstance(contracts, list):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_republisher_cohort_contracts_missing"
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_contract in contracts:
        contract = CanonicalModelOOSContract.from_mapping(
            _mapping(raw_contract, "republisher_cohort_contract")
        )
        for cohort in contract.cohort_facts:
            if cohort.content_sha256 in seen:
                raise PR05PR09ModelOOSBasisError(
                    "pr05_pr09_model_oos_republisher_cohort_duplicate"
                )
            seen.add(cohort.content_sha256)
            result.append({
                "scope_kind": "CANONICAL_MODEL_OOS_SCOPE",
                "scope_identity": _identity(cohort.raw, label="republisher_cohort"),
                "cohort_content_sha256": cohort.content_sha256,
                "evaluation_state": cohort.raw["evaluation_state"],
                "source_artifact_content_sha256": artifact_sha,
            })
    if artifact.get("cohort_count") != len(result):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_republisher_cohort_count_mismatch"
        )
    return result


def _icon_eps_rows(
    artifact: Mapping[str, Any], artifact_sha: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    facts = artifact.get("facts")
    terminals = artifact.get("terminals")
    if not isinstance(facts, list) or not isinstance(terminals, list):
        raise PR05PR09ModelOOSBasisError("pr05_pr09_model_oos_icon_eps_rows_missing")
    fact_rows: list[dict[str, Any]] = []
    terminal_rows: list[dict[str, Any]] = []
    for raw in facts:
        value = _mapping(raw, "icon_eps_fact")
        if (
            value.get("schema")
            != "bayes_basket_target_pr07_icon_eps_historical_member_fact_v1"
            or value.get("state") != STATE
            or value.get("consumable") is not False
        ):
            raise PR05PR09ModelOOSBasisError(
                "pr05_pr09_model_oos_icon_eps_fact_semantics_invalid"
            )
        fact_rows.append({
            "scope_kind": "NATIVE_ENSEMBLE_MEMBER_HISTORY",
            "scope_identity": _identity(value, label="icon_eps_fact"),
            "event_date": str(value.get("event_date") or ""),
            "fact_content_sha256": _verified_hash(value, "icon_eps_fact"),
            "source_artifact_content_sha256": artifact_sha,
        })
    for raw in terminals:
        value = _mapping(raw, "icon_eps_terminal")
        if value.get("schema") != "bayes_basket_target_pr07_icon_eps_terminal_v1":
            raise PR05PR09ModelOOSBasisError(
                "pr05_pr09_model_oos_icon_eps_terminal_schema_invalid"
            )
        terminal_rows.append({
            "scope_kind": "NATIVE_ENSEMBLE_MEMBER_HISTORY",
            "scope_identity": _identity(value, label="icon_eps_terminal"),
            "terminal": _terminal_reference(value, artifact_sha),
        })
    if artifact.get("fact_count") != len(fact_rows) or artifact.get("terminal_count") != len(terminal_rows):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_icon_eps_count_mismatch"
        )
    return fact_rows, terminal_rows


def _coverage_rows(
    artifact: Mapping[str, Any], artifact_sha: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    expected = artifact.get("v13_expected_scopes")
    terminals = artifact.get("terminal_scopes")
    if not isinstance(expected, list) or not isinstance(terminals, list):
        raise PR05PR09ModelOOSBasisError("pr05_pr09_model_oos_coverage_rows_missing")
    if artifact.get("canonical_product_registry_content_sha256") != REGISTRY_CONTENT_SHA256:
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_coverage_product_registry_mismatch"
        )
    rows: list[dict[str, Any]] = []
    direct_terminals: list[dict[str, Any]] = []
    unscoped: list[dict[str, Any]] = []
    expected_by_receipt: dict[str, dict[str, Any]] = {}
    for raw in expected:
        value = _mapping(raw, "coverage_expected_scope")
        receipt_sha = _sha(
            value.get("model_product_identity_content_sha256"), "coverage_receipt"
        )
        if receipt_sha in expected_by_receipt:
            raise PR05PR09ModelOOSBasisError(
                "pr05_pr09_model_oos_coverage_expected_duplicate"
            )
        expected_by_receipt[receipt_sha] = value
        rows.append({
            "scope_kind": "CANONICAL_MODEL_OOS_SCOPE",
            "scope_identity": _identity(value, label="coverage_expected_scope"),
            "product_id": str(value.get("product_id") or ""),
            "model_product_identity_content_sha256": receipt_sha,
            "source_artifact_content_sha256": artifact_sha,
        })
    for raw in terminals:
        value = _mapping(raw, "coverage_terminal")
        if value.get("state") != "BLOCKED":
            raise PR05PR09ModelOOSBasisError(
                "pr05_pr09_model_oos_coverage_terminal_state_invalid"
            )
        receipt = value.get("model_product_identity_receipt")
        if value.get("expected_key_emitted") is True:
            # This receipt is historical, content-addressed evidence.  Do not
            # re-interpret it through today's mutable product registry: the
            # exact expected scope below is its only permitted companion.
            verified_receipt = _mapping(receipt, "coverage_terminal_receipt")
            receipt_sha = _verified_hash(
                verified_receipt, "coverage_terminal_receipt"
            )
            try:
                validate_model_product_identity_receipt(verified_receipt)
            except ContractError as exc:
                raise PR05PR09ModelOOSBasisError(
                    "pr05_pr09_model_oos_coverage_terminal_receipt_invalid"
                ) from exc
            expected_scope = expected_by_receipt.get(receipt_sha)
            if expected_scope is None:
                raise PR05PR09ModelOOSBasisError(
                    "pr05_pr09_model_oos_coverage_terminal_unbound"
                )
            if any(
                verified_receipt.get(field) != expected_scope[field]
                for field in V13_SCOPE_FIELDS
            ):
                raise PR05PR09ModelOOSBasisError(
                    "pr05_pr09_model_oos_coverage_terminal_scope_mismatch"
                )
            direct_terminals.append({
                "scope_kind": "CANONICAL_MODEL_OOS_SCOPE",
                "scope_identity": _identity(expected_scope, label="coverage_terminal"),
                "model_product_identity_content_sha256": receipt_sha,
                "terminal": _terminal_reference(value, artifact_sha),
            })
        else:
            _append_unscoped_terminal(
                unscoped,
                raw=value,
                artifact={
                    "schema": COVERAGE_SCHEMA,
                    "content_sha256": artifact_sha,
                },
            )
    if artifact.get("v13_expected_scope_count") != len(rows) or artifact.get("terminal_scope_count") != len(terminals):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_coverage_count_mismatch"
        )
    return rows, direct_terminals, unscoped


def _regional_retained_rows(
    artifact: Mapping[str, Any], artifact_sha: str,
    *, coverage_sha: str,
) -> list[dict[str, Any]]:
    if (
        artifact.get("coverage_scope_content_sha256") != coverage_sha
        or artifact.get("canonical_product_registry_content_sha256")
        != REGISTRY_CONTENT_SHA256
    ):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_regional_retained_parent_mismatch"
        )
    terminals = artifact.get("terminals")
    if not isinstance(terminals, list):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_regional_retained_terminals_missing"
        )
    result: list[dict[str, Any]] = []
    for raw in terminals:
        value = _mapping(raw, "regional_retained_terminal")
        if (
            value.get("schema")
            != "bayes_basket_target_regional_retained_scope_terminal_v1"
            or value.get("blocker") != "LINEAGE_MISMATCH"
            or value.get("state") != "BLOCKED"
            or value.get("release_state") != "BLOCKED"
            or value.get("consumable") is not False
        ):
            raise PR05PR09ModelOOSBasisError(
                "pr05_pr09_model_oos_regional_retained_terminal_semantics_invalid"
            )
        result.append({
            "scope_kind": "CANONICAL_MODEL_OOS_SCOPE",
            "scope_identity": _identity(value, label="regional_retained_terminal"),
            "model_product_identity_content_sha256": _sha(
                value.get("model_product_identity_content_sha256"),
                "regional_retained_identity_receipt",
            ),
            "source_artifact_content_sha256": artifact_sha,
            "terminal": _terminal_reference(value, artifact_sha),
        })
    if artifact.get("terminal_count") != len(result):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_regional_retained_count_mismatch"
        )
    return result


def _icon_legacy_rows(
    artifact: Mapping[str, Any], artifact_sha: str, *, coverage_sha: str,
) -> list[dict[str, Any]]:
    if artifact.get("coverage_scope_content_sha256") != coverage_sha:
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_icon_legacy_parent_mismatch"
        )
    terminals = artifact.get("terminals")
    if not isinstance(terminals, list):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_icon_legacy_terminals_missing"
        )
    result: list[dict[str, Any]] = []
    for raw in terminals:
        value = _mapping(raw, "icon_legacy_terminal")
        if (
            value.get("schema") != "bayes_basket_target_icon_legacy_scope_terminal_v1"
            or value.get("blocker") != "LINEAGE_MISMATCH"
            or value.get("state") != "BLOCKED"
            or value.get("release_state") != "BLOCKED"
            or value.get("consumable") is not False
        ):
            raise PR05PR09ModelOOSBasisError(
                "pr05_pr09_model_oos_icon_legacy_terminal_semantics_invalid"
            )
        result.append({
            "scope_kind": "CANONICAL_MODEL_OOS_SCOPE",
            "scope_identity": _identity(value, label="icon_legacy_terminal"),
            "model_product_identity_content_sha256": _sha(
                value.get("model_product_identity_content_sha256"),
                "icon_legacy_identity_receipt",
            ),
            "source_artifact_content_sha256": artifact_sha,
            "terminal": _terminal_reference(value, artifact_sha),
        })
    if artifact.get("terminal_count") != len(result):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_icon_legacy_count_mismatch"
        )
    return result


def _build_scope(
    *, kind: str, identity: Mapping[str, Any], event_facts: Sequence[Mapping[str, Any]],
    cohort_facts: Sequence[Mapping[str, Any]],
    member_facts: Sequence[Mapping[str, Any]], coverage_expected: Mapping[str, Any] | None,
    terminals: Sequence[Mapping[str, Any]], source_artifact_shas: Sequence[str],
    evidence_roles: Sequence[str],
) -> dict[str, Any]:
    event_by_date: dict[str, str] = {}
    for fact in event_facts:
        event_date = str(fact["event_date"])
        existing = event_by_date.setdefault(event_date, str(fact["fact_content_sha256"]))
        if existing != fact["fact_content_sha256"]:
            raise PR05PR09ModelOOSBasisError(
                "pr05_pr09_model_oos_event_date_duplicate"
            )
    member_by_date: dict[str, str] = {}
    for fact in member_facts:
        event_date = str(fact["event_date"])
        existing = member_by_date.setdefault(event_date, str(fact["fact_content_sha256"]))
        if existing != fact["fact_content_sha256"]:
            raise PR05PR09ModelOOSBasisError(
                "pr05_pr09_model_oos_member_date_duplicate"
            )
    event_dates = sorted(event_by_date)
    member_dates = sorted(member_by_date)
    train_available = min(20, len(event_dates))
    holdout_available = min(8, max(0, len(event_dates) - 20))
    blockers: list[str]
    scope_state = "BLOCKED"
    cohort_state = None
    cohort_sha = None
    if len(cohort_facts) > 1:
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_scope_cohort_duplicate"
        )
    if cohort_facts:
        cohort_state = str(cohort_facts[0]["evaluation_state"])
        cohort_sha = str(cohort_facts[0]["cohort_content_sha256"])
    terminal_blockers = sorted({
        str(item.get("blocker") or "") for item in terminals
        if str(item.get("blocker") or "")
    })
    if any(blocker != "DATA_MISSING" for blocker in terminal_blockers):
        terminal_blockers = [
            blocker for blocker in terminal_blockers if blocker != "DATA_MISSING"
        ]
    if kind == "NATIVE_ENSEMBLE_MEMBER_HISTORY":
        blockers = ["LINEAGE_MISMATCH", "OOS_SAMPLE_SHORT"]
    elif cohort_state == "OOS_PASS":
        blockers = []
        scope_state = "OOS_READY"
    elif cohort_state == "OOS_REJECTED":
        blockers = ["OOS_REJECTED"]
    elif len(event_dates) < 28:
        blockers = (
            ["OOS_SAMPLE_SHORT"] if event_dates
            else terminal_blockers or ["DATA_MISSING"]
        )
    else:
        blockers = ["COHORT_ARTIFACT_MISSING"]
    return {
        "scope_kind": kind,
        "scope_key": _scope_token(identity),
        "scope_identity": dict(identity),
        "evidence_roles": sorted(set(evidence_roles)),
        "model_product_id": None if coverage_expected is None else coverage_expected["product_id"],
        "model_product_identity_content_sha256": (
            None
            if coverage_expected is None
            else coverage_expected["model_product_identity_content_sha256"]
        ),
        "canonical_model_oos_event_count": len(event_dates),
        "canonical_model_oos_event_dates": event_dates,
        "canonical_model_oos_event_content_sha256s": [
            event_by_date[value] for value in event_dates
        ],
        "historical_member_fact_count": len(member_dates),
        "historical_member_fact_dates": member_dates,
        "historical_member_fact_content_sha256s": [
            member_by_date[value] for value in member_dates
        ],
        "cohort_eligible_event_count": len(event_dates),
        "canonical_model_oos_cohort_count": len(cohort_facts),
        "canonical_model_oos_cohort_content_sha256": cohort_sha,
        "cohort_evaluation_state": cohort_state,
        "train_available_count": train_available,
        "holdout_available_count": holdout_available,
        "train_shortfall_count": 20 - train_available,
        "holdout_shortfall_count": 8 - holdout_available,
        "blockers": blockers,
        "state": scope_state,
        "upstream_terminal_receipts": sorted(
            (dict(item) for item in terminals),
            key=lambda item: item["terminal_content_sha256"],
        ),
        "source_artifact_content_sha256s": sorted(set(source_artifact_shas)),
        "production_no_live_only": True,
        "consumable": False,
    }


def build_pr05_pr09_model_oos_basis(
    *,
    as_of_utc: str,
    republisher_fixed_lead_path: Path | str,
    republisher_cohort_path: Path | str,
    icon_eps_basis_path: Path | str,
    native_ensemble_terminal_path: Path | str,
    aifs_terminal_path: Path | str,
    coverage_scope_path: Path | str,
    regional_retained_terminal_path: Path | str,
    icon_legacy_terminal_path: Path | str,
) -> dict[str, Any]:
    """Materialize a bounded, exact-lineage PR05--PR09 gap ledger.

    No database, pointer, service, basket, or order operation is performed.
    In particular, source artifacts are *not* cross-joined: a V13 event and a
    coverage expectation only share a result row when their source explicitly
    carries the exact same canonical scope identity.
    """

    cutoff = _utc(as_of_utc, "as_of_utc")
    if MAX_INPUT_FILES_PER_BATCH < 8:
        raise PR05PR09ModelOOSBasisError("pr05_pr09_model_oos_input_limit_invalid")
    assert_process_memory_budget(projected_bytes=5 * 1024 * 1024)
    republisher, republisher_ref = _load_artifact(
        republisher_fixed_lead_path,
        role="republisher_fixed_lead",
        schema=REPUBLISHER_SCHEMA,
        as_of_utc=cutoff,
    )
    republisher_cohorts, republisher_cohorts_ref = _load_cohort_artifact(
        republisher_cohort_path, as_of_utc=cutoff
    )
    icon_eps, icon_eps_ref = _load_artifact(
        icon_eps_basis_path, role="icon_eps", schema=ICON_EPS_SCHEMA, as_of_utc=cutoff
    )
    native_ensemble, native_ensemble_ref = _load_artifact(
        native_ensemble_terminal_path,
        role="native_ensemble_terminals",
        schema=NATIVE_ENSEMBLE_SCHEMA,
        as_of_utc=cutoff,
    )
    aifs, aifs_ref = _load_artifact(
        aifs_terminal_path, role="aifs_terminals", schema=AIFS_SCHEMA, as_of_utc=cutoff
    )
    coverage, coverage_ref = _load_artifact(
        coverage_scope_path, role="coverage_scopes", schema=COVERAGE_SCHEMA, as_of_utc=cutoff
    )
    regional_retained, regional_retained_ref = _load_artifact(
        regional_retained_terminal_path,
        role="regional_retained_terminals",
        schema=REGIONAL_RETAINED_SCHEMA,
        as_of_utc=cutoff,
    )
    icon_legacy, icon_legacy_ref = _load_artifact(
        icon_legacy_terminal_path,
        role="icon_legacy_terminals",
        schema=ICON_LEGACY_SCHEMA,
        as_of_utc=cutoff,
    )

    event_rows = _event_scope_rows(republisher, republisher_ref["content_sha256"])
    cohort_rows = _cohort_scope_rows(
        republisher_cohorts, republisher_cohorts_ref["content_sha256"]
    )
    member_rows, member_terminals = _icon_eps_rows(icon_eps, icon_eps_ref["content_sha256"])
    coverage_rows, coverage_terminals, unscoped = _coverage_rows(
        coverage, coverage_ref["content_sha256"]
    )
    regional_terminals = _regional_retained_rows(
        regional_retained,
        regional_retained_ref["content_sha256"],
        coverage_sha=coverage_ref["content_sha256"],
    )
    icon_legacy_terminals = _icon_legacy_rows(
        icon_legacy,
        icon_legacy_ref["content_sha256"],
        coverage_sha=coverage_ref["content_sha256"],
    )
    for raw in native_ensemble.get("terminals", []):
        _append_unscoped_terminal(
            unscoped,
            raw=_mapping(raw, "native_ensemble_terminal"),
            artifact=native_ensemble_ref,
        )
    for raw in aifs.get("terminals", []):
        _append_unscoped_terminal(
            unscoped,
            raw=_mapping(raw, "aifs_terminal"),
            artifact=aifs_ref,
        )
    if native_ensemble.get("terminal_count") != len(native_ensemble.get("terminals", [])):
        raise PR05PR09ModelOOSBasisError(
            "pr05_pr09_model_oos_native_ensemble_count_mismatch"
        )
    if aifs.get("terminal_count") != len(aifs.get("terminals", [])):
        raise PR05PR09ModelOOSBasisError("pr05_pr09_model_oos_aifs_count_mismatch")

    grouped: dict[str, dict[str, Any]] = {}

    def add_row(row: Mapping[str, Any], bucket: str) -> None:
        kind = str(row["scope_kind"])
        identity = _mapping(row["scope_identity"], "scope_identity")
        token = _scope_token(identity)
        target = grouped.setdefault(token, {
            "kind": kind,
            "identity": identity,
            "events": [],
            "cohorts": [],
            "members": [],
            "coverage": None,
            "terminals": [],
            "sources": [],
            "evidence_roles": set(),
        })
        if target["identity"] != identity:
            raise PR05PR09ModelOOSBasisError("pr05_pr09_model_oos_scope_hash_collision")
        if target["kind"] != kind:
            # Different payload roles may share one exact canonical identity,
            # but member vectors remain member evidence and are never coerced
            # into scalar model_oos_event facts.
            target["kind"] = "CANONICAL_MODEL_OOS_SCOPE"
        if bucket == "event":
            target["events"].append(row)
            target["evidence_roles"].add("CANONICAL_EVENT")
        elif bucket == "cohort":
            target["cohorts"].append(row)
            target["evidence_roles"].add("CANONICAL_COHORT")
        elif bucket == "member":
            target["members"].append(row)
            target["evidence_roles"].add("MEMBER_FACT")
        elif bucket == "coverage":
            if target["coverage"] is not None:
                raise PR05PR09ModelOOSBasisError(
                    "pr05_pr09_model_oos_coverage_scope_duplicate"
                )
            target["coverage"] = row
            target["evidence_roles"].add("EXPECTED_SCOPE")
        else:
            target["terminals"].append(row["terminal"])
            target["evidence_roles"].add("TERMINAL_RECEIPT")
        target["sources"].append(str(row.get("source_artifact_content_sha256") or ""))

    for row in event_rows:
        add_row(row, "event")
    for row in cohort_rows:
        add_row(row, "cohort")
    for row in member_rows:
        add_row(row, "member")
    for row in coverage_rows:
        add_row(row, "coverage")
    for row in member_terminals:
        add_row(row, "terminal")
    for row in coverage_terminals:
        add_row(row, "terminal")
    for row in regional_terminals:
        add_row(row, "terminal")
    for row in icon_legacy_terminals:
        add_row(row, "terminal")

    scopes = [
        _build_scope(
            kind=item["kind"], identity=item["identity"], event_facts=item["events"],
            cohort_facts=item["cohorts"],
            member_facts=item["members"], coverage_expected=item["coverage"],
            terminals=item["terminals"], source_artifact_shas=item["sources"],
            evidence_roles=sorted(item["evidence_roles"]),
        )
        for item in grouped.values()
    ]
    scopes.sort(key=lambda item: (item["scope_kind"], item["scope_key"]))
    unscoped.sort(key=lambda item: item["terminal_content_sha256"])
    if len({item["scope_key"] for item in scopes}) != len(scopes):
        raise PR05PR09ModelOOSBasisError("pr05_pr09_model_oos_scope_duplicate")
    if len({item["terminal_content_sha256"] for item in unscoped}) != len(unscoped):
        raise PR05PR09ModelOOSBasisError("pr05_pr09_model_oos_unscoped_terminal_duplicate")

    blockers = Counter(
        blocker for scope in scopes for blocker in scope["blockers"]
    )
    blockers.update(item["blocker"] for item in unscoped)
    source_vector = sorted(
        (
            republisher_ref, republisher_cohorts_ref, icon_eps_ref,
            native_ensemble_ref, aifs_ref, coverage_ref,
            regional_retained_ref,
            icon_legacy_ref,
        ),
        key=lambda item: item["artifact_role"],
    )
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": cutoff,
        "state": STATE,
        "release_state": RELEASE_STATE,
        "authority_scope": AUTHORITY_SCOPE,
        "source_artifact_vector": source_vector,
        "source_artifact_vector_sha256": content_sha256(source_vector),
        "scope_count": len(scopes),
        "canonical_model_oos_event_count": sum(
            item["canonical_model_oos_event_count"] for item in scopes
        ),
        "historical_member_fact_count": sum(
            item["historical_member_fact_count"] for item in scopes
        ),
        "canonical_model_oos_cohort_count": sum(
            item["canonical_model_oos_cohort_count"] for item in scopes
        ),
        "oos_ready_scope_count": sum(item["state"] == "OOS_READY" for item in scopes),
        "blocked_scope_count": sum(item["state"] == "BLOCKED" for item in scopes),
        "unscoped_terminal_count": len(unscoped),
        "blocker_counts": dict(sorted(blockers.items())),
        "scopes": scopes,
        "unscoped_terminal_receipts": unscoped,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "reads_basket": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
        "future_data_counts_as_oos": False,
        "consumable": False,
    }
    assert_process_memory_budget()
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "AUTHORITY_SCOPE",
    "BUILDER_ID",
    "PR05PR09ModelOOSBasisError",
    "SCHEMA",
    "V13_SCOPE_FIELDS",
    "build_pr05_pr09_model_oos_basis",
]
