"""Canonical unpublished activation root joining selection and calibration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .model_oos_selection import (
    CanonicalModelOOSSelectionFact,
    assert_model_oos_selection_rebuild,
)
from .p0_calibrated_distribution import (
    CanonicalP0CalibratedDistributionFact,
    CanonicalP0CalibratedDistributionContract,
    build_p0_calibrated_distribution_contract,
)


CONTRACT_SCHEMA = "bayes_basket_target_canonical_p0_activation_root_contract_v1"
FACT_TYPE = "p0_activation_root"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
STATE = "MATERIALIZED_UNPUBLISHED"
FACT_FIELDS = {
    "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
    "run_cycle", "run_cycle_scope", "season_bin", "source_class",
    "event_oos_protocol_version", "calibration_method_version",
    "authority_scope", "available_model_set_hash", "cohort_truth_vector_sha256",
    "selection_content_sha256", "calibrated_distribution_content_sha256",
    "activation_generation", "predecessor_activation_root_content_sha256",
    "activated_at_utc", "state", "content_sha256",
}


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"p0_activation_root_{label}_missing")
    return result


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"p0_activation_root_{label}_invalid")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"p0_activation_root_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"p0_activation_root_{label}_timezone_required")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class CanonicalP0ActivationRootFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalP0ActivationRootFact":
        if set(raw) != FACT_FIELDS:
            raise ContractError("p0_activation_root_fact_fields_mismatch")
        value = dict(raw)
        for field in (
            "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
            "run_cycle_scope", "season_bin", "source_class",
            "event_oos_protocol_version", "calibration_method_version",
        ):
            value[field] = _text(value[field], field)
        value["station_id"] = value["station_id"].upper()
        value["run_cycle"] = None if value["run_cycle"] is None else _text(value["run_cycle"], "run_cycle")
        if value["authority_scope"] != AUTHORITY_SCOPE or value["state"] != STATE:
            raise ContractError("p0_activation_root_authority_state_invalid")
        for field in (
            "available_model_set_hash", "cohort_truth_vector_sha256",
            "selection_content_sha256", "calibrated_distribution_content_sha256",
        ):
            value[field] = _sha(value[field], field)
        generation = value["activation_generation"]
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ContractError("p0_activation_root_generation_invalid")
        predecessor = value["predecessor_activation_root_content_sha256"]
        value["predecessor_activation_root_content_sha256"] = (
            None if predecessor is None else _sha(predecessor, "predecessor_sha256")
        )
        if (generation == 1) != (predecessor is None):
            raise ContractError("p0_activation_root_predecessor_generation_invalid")
        value["activated_at_utc"] = _utc(value["activated_at_utc"], "activated_at_utc")
        supplied = _sha(value["content_sha256"], "content_sha256")
        if supplied != content_sha256({key: item for key, item in value.items() if key != "content_sha256"}):
            raise ContractError("p0_activation_root_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(str(self.raw[key] if self.raw[key] is not None else "null") for key in (
            "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
            "run_cycle", "run_cycle_scope", "season_bin", "source_class",
            "event_oos_protocol_version", "calibration_method_version",
            "authority_scope", "available_model_set_hash", "activation_generation",
        ))


@dataclass(frozen=True)
class CanonicalP0ActivationRootContract:
    as_of_utc: str
    builder_id: str
    activation_root_facts: tuple[CanonicalP0ActivationRootFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalP0ActivationRootContract":
        if set(raw) != {"schema", "as_of_utc", "builder_id", "activation_root_facts", "content_sha256"}:
            raise ContractError("p0_activation_root_contract_fields_mismatch")
        if raw["schema"] != CONTRACT_SCHEMA:
            raise ContractError("p0_activation_root_contract_schema_invalid")
        facts_raw = raw["activation_root_facts"]
        if not isinstance(facts_raw, list) or not facts_raw:
            raise ContractError("p0_activation_root_fact_required")
        facts = tuple(CanonicalP0ActivationRootFact.from_mapping(item) for item in facts_raw)
        if len({item.scope_key for item in facts}) != len(facts):
            raise ContractError("p0_activation_root_scope_duplicate")
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(item.raw["activated_at_utc"] > as_of for item in facts):
            raise ContractError("p0_activation_root_fact_after_contract_asof")
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        if supplied != content_sha256({key: item for key, item in raw.items() if key != "content_sha256"}):
            raise ContractError("p0_activation_root_contract_content_sha256_mismatch")
        return cls(as_of, _text(raw["builder_id"], "builder_id"), facts, supplied)


def _load(conn: sqlite3.Connection, fact_type: str, sha: str, cls: Any) -> Any:
    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type=? AND content_sha256=?",
        (fact_type, _sha(sha, f"{fact_type}_sha256")),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError(f"p0_activation_root_{fact_type}_not_unique")
    return cls.from_mapping(json.loads(str(rows[0][0])))


def build_p0_activation_root_contract(
    conn: sqlite3.Connection,
    *,
    selection_content_sha256: str,
    calibrated_distribution_content_sha256: str,
    activation_generation: int,
    predecessor_activation_root_content_sha256: str | None,
    as_of_utc: str,
) -> dict[str, Any]:
    selection = _load(conn, "p0_model_family_selection", selection_content_sha256, CanonicalModelOOSSelectionFact)
    assert_model_oos_selection_rebuild(conn, selection)
    distribution = _load(
        conn, "p0_calibrated_distribution", calibrated_distribution_content_sha256,
        CanonicalP0CalibratedDistributionFact,
    )
    if distribution.raw["selection_content_sha256"] != selection.content_sha256:
        raise ContractError("p0_activation_root_parent_link_mismatch")
    inherited = (
        "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
        "run_cycle", "run_cycle_scope", "season_bin", "source_class",
        "event_oos_protocol_version", "calibration_method_version", "authority_scope",
        "available_model_set_hash", "cohort_truth_vector_sha256",
    )
    if any(distribution.raw[field] != selection.raw[field] for field in inherited):
        raise ContractError("p0_activation_root_parent_scope_mismatch")
    predecessor = None
    if predecessor_activation_root_content_sha256 is not None:
        predecessor = _load(
            conn, "p0_activation_root", predecessor_activation_root_content_sha256,
            CanonicalP0ActivationRootFact,
        )
        if (
            predecessor.raw["activation_generation"] + 1 != activation_generation
            or any(predecessor.raw[field] != selection.raw[field] for field in inherited)
        ):
            raise ContractError("p0_activation_root_predecessor_mismatch")
    if (activation_generation == 1) != (predecessor is None):
        raise ContractError("p0_activation_root_predecessor_generation_invalid")
    activated_at = _utc(as_of_utc, "activated_at_utc")
    if max(selection.raw["selection_as_of_utc"], distribution.raw["distribution_as_of_utc"]) > activated_at:
        raise ContractError("p0_activation_root_parent_after_asof")
    identity = {
        **{field: selection.raw[field] for field in inherited},
        "selection_content_sha256": selection.content_sha256,
        "calibrated_distribution_content_sha256": distribution.content_sha256,
        "activation_generation": activation_generation,
        "predecessor_activation_root_content_sha256": (
            None if predecessor is None else predecessor.content_sha256
        ),
        "activated_at_utc": activated_at,
        "state": STATE,
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    contract_identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": activated_at,
        "builder_id": "canonical.p0_activation_root.v1",
        "activation_root_facts": [fact],
    }
    contract = {**contract_identity, "content_sha256": content_sha256(contract_identity)}
    CanonicalP0ActivationRootContract.from_mapping(contract)
    return contract


def assert_p0_activation_root_rebuild(
    conn: sqlite3.Connection, activation: CanonicalP0ActivationRootFact,
) -> None:
    """Prove that a persisted root still has an admissible P0 ancestry.

    A content hash proves that a row was not modified after it was written; it
    does not prove that an older writer applied today's mandatory calibration
    gate.  Consumers therefore rebuild the distribution and the root from the
    exact stored parents.  A rejected component cannot remain accidentally
    consumable merely because an obsolete writer materialized its descendants.
    """

    selection = _load(
        conn,
        "p0_model_family_selection",
        activation.raw["selection_content_sha256"],
        CanonicalModelOOSSelectionFact,
    )
    assert_model_oos_selection_rebuild(conn, selection)
    distribution = _load(
        conn,
        "p0_calibrated_distribution",
        activation.raw["calibrated_distribution_content_sha256"],
        CanonicalP0CalibratedDistributionFact,
    )
    rebuilt_distribution = build_p0_calibrated_distribution_contract(
        conn,
        selection_content_sha256=selection.content_sha256,
        as_of_utc=distribution.raw["distribution_as_of_utc"],
    )
    expected_distribution = CanonicalP0CalibratedDistributionContract.from_mapping(
        rebuilt_distribution
    ).distribution_facts[0]
    if expected_distribution.raw != distribution.raw:
        raise ContractError("p0_activation_root_distribution_rebuild_mismatch")
    rebuilt = build_p0_activation_root_contract(
        conn,
        selection_content_sha256=selection.content_sha256,
        calibrated_distribution_content_sha256=distribution.content_sha256,
        activation_generation=activation.raw["activation_generation"],
        predecessor_activation_root_content_sha256=activation.raw[
            "predecessor_activation_root_content_sha256"
        ],
        as_of_utc=activation.raw["activated_at_utc"],
    )
    expected = CanonicalP0ActivationRootContract.from_mapping(
        rebuilt
    ).activation_root_facts[0]
    if expected.raw != activation.raw:
        raise ContractError("p0_activation_root_reference_rebuild_mismatch")


__all__ = [
    "CONTRACT_SCHEMA", "FACT_TYPE", "CanonicalP0ActivationRootContract",
    "CanonicalP0ActivationRootFact", "assert_p0_activation_root_rebuild",
    "build_p0_activation_root_contract",
]
