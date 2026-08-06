"""Build non-writing authority supersession candidates for legacy AWC truth."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from .awc_observation_contract import (
    AUTHORITY_SCOPE, AWC_ORIGIN_AUTHORITIES, AWC_ORIGIN_SOURCE_IDS,
    AWC_ORIGIN_URL, CONTRACT_SCHEMA as AWC_CONTRACT_SCHEMA,
    COVERAGE_RECEIPT_SCHEMA, RAW_OBJECT_KIND, SETTLEMENT_TIMEZONE,
    CanonicalAWCObservationContract,
    CanonicalAWCObservationDayBoundsFact,
    CanonicalAWCObservationRevisionFact,
)
from .contracts import ContractError, content_sha256
from .pr12_authority_scope_projection import validate_pr12_authority_scope_projection


SCHEMA = "bayes_basket_target_truth_authority_supersession_bundle_v1"
CANDIDATE_SCHEMA = "bayes_basket_target_truth_authority_supersession_candidate_v1"
TERMINAL_SCHEMA = "bayes_basket_target_truth_authority_supersession_terminal_v1"
BUILDER_ID = "pr12.truth_authority_supersession.v1"
CONTRACT_SCHEMA = "bayes_basket_target_truth_authority_supersession_contract_v1"
FACT_SCHEMA = "bayes_basket_target_truth_authority_supersession_fact_v1"
REASON = "CANONICAL_DAY_AUTHORITY_BINDING"
REVIEW_AUDIT_SCHEMA = "bayes_basket_target_truth_authority_review_scope_audit_v1"


def _sha(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ContractError("truth_authority_supersession_" + label + "_invalid")
    return text


def _sealed(raw: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(raw)
    return {**identity, "content_sha256": content_sha256(identity)}


def _revision_semantic_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in raw.items()
        if key not in {
            "authority_scope_projection_content_sha256",
            "content_sha256",
        }
    }


def _revision_semantic_vector(rows: list[Mapping[str, Any]]) -> str:
    ordered = sorted(rows, key=lambda row: int(row["sequence"]))
    return content_sha256(
        [content_sha256(_revision_semantic_identity(row)) for row in ordered]
    )


def build_truth_authority_review_scope_audit(
    archive_conn: sqlite3.Connection,
    *,
    legacy_scopes: list[tuple[str, str, str]],
    authority_scope_projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal review-bearing V1 scopes that cannot be authority-rebound."""

    projection = validate_pr12_authority_scope_projection(
        authority_scope_projection
    )
    scope_set = {(station, event_date) for station, event_date, _ in legacy_scopes}
    blockers: list[dict[str, Any]] = []
    rows = archive_conn.execute(
        "SELECT station_id,event_date,review_id,decision,"
        "triggering_observation_id,evidence_sha256,content_sha256 "
        "FROM reported_trajectory_station_halt_reviews "
        "WHERE source_lane='awc' AND reviewed_at<=? "
        "ORDER BY station_id,event_date,reviewed_at,review_id",
        (projection["as_of_utc"],),
    ).fetchall()
    for row in rows:
        station = str(row[0]).upper()
        event = str(row[1])
        if (station, event) not in scope_set:
            continue
        blockers.append(
            {
                "station_id": station,
                "event_date": event,
                "settlement_timezone": SETTLEMENT_TIMEZONE,
                "review_id": str(row[2]),
                "decision": str(row[3]),
                "triggering_observation_id": str(row[4]),
                "review_evidence_sha256": _sha(row[5], "review_evidence_sha256"),
                "review_content_sha256": _sha(row[6], "review_content_sha256"),
                "blocker": "REVISION_NOT_REVIEWED",
                "diagnostic": "LEGACY_V1_REVIEW_LINEAGE_NOT_BOUND",
            }
        )
    identity = {
        "schema": REVIEW_AUDIT_SCHEMA,
        "as_of_utc": projection["as_of_utc"],
        "authority_scope_projection_content_sha256": projection["content_sha256"],
        "legacy_scope_count": len(scope_set),
        "review_scope_blocker_count": len(blockers),
        "review_scope_blockers": blockers,
    }
    return _sealed(identity)


def _validate_review_scope_audit(
    raw: Mapping[str, Any], *, projection_sha: str
) -> dict[str, Any]:
    fields = {
        "schema", "as_of_utc", "authority_scope_projection_content_sha256",
        "legacy_scope_count", "review_scope_blocker_count",
        "review_scope_blockers", "content_sha256",
    }
    if set(raw) != fields or raw.get("schema") != REVIEW_AUDIT_SCHEMA:
        raise ContractError("truth_authority_review_audit_fields_invalid")
    value = dict(raw)
    supplied = _sha(value.pop("content_sha256", ""), "review_audit_content_sha256")
    blocker_fields = {
        "station_id", "event_date", "settlement_timezone", "review_id",
        "decision", "triggering_observation_id", "review_evidence_sha256",
        "review_content_sha256", "blocker", "diagnostic",
    }
    keys: list[tuple[str, str, str]] = []
    for blocker in value.get("review_scope_blockers") or ():
        if not isinstance(blocker, Mapping) or set(blocker) != blocker_fields:
            raise ContractError("truth_authority_review_audit_blocker_invalid")
        if (
            blocker["settlement_timezone"] != SETTLEMENT_TIMEZONE
            or blocker["blocker"] != "REVISION_NOT_REVIEWED"
            or blocker["diagnostic"] != "LEGACY_V1_REVIEW_LINEAGE_NOT_BOUND"
        ):
            raise ContractError("truth_authority_review_audit_blocker_invalid")
        _sha(blocker["review_evidence_sha256"], "review_evidence_sha256")
        _sha(blocker["review_content_sha256"], "review_content_sha256")
        keys.append((
            str(blocker["station_id"]), str(blocker["event_date"]),
            str(blocker["settlement_timezone"]),
        ))
    if (
        supplied != content_sha256(value)
        or value["authority_scope_projection_content_sha256"] != projection_sha
        or int(value["review_scope_blocker_count"])
        != len(value["review_scope_blockers"])
        or len(keys) != len(set(keys))
    ):
        raise ContractError("truth_authority_review_audit_invalid")
    value["content_sha256"] = supplied
    return value


@dataclass(frozen=True)
class CanonicalTruthAuthoritySupersessionFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalTruthAuthoritySupersessionFact":
        fields = {
            "schema", "station_id", "event_date", "settlement_timezone",
            "predecessor_truth_day_bounds_content_sha256",
            "predecessor_revision_semantic_vector_sha256",
            "successor_truth_day_bounds_content_sha256",
            "successor_revision_semantic_vector_sha256",
            "authority_scope_projection_content_sha256",
            "station_authority_row_content_sha256", "predecessor_bounds_version",
            "successor_bounds_version", "reason", "authority_as_of_utc",
            "authority_scope", "content_sha256",
        }
        if set(raw) != fields:
            raise ContractError("truth_authority_supersession_fact_fields_mismatch")
        value = dict(raw)
        if value["schema"] != FACT_SCHEMA:
            raise ContractError("truth_authority_supersession_fact_schema_invalid")
        for field in (
            "predecessor_truth_day_bounds_content_sha256",
            "predecessor_revision_semantic_vector_sha256",
            "successor_truth_day_bounds_content_sha256",
            "successor_revision_semantic_vector_sha256",
            "authority_scope_projection_content_sha256",
            "station_authority_row_content_sha256",
        ):
            value[field] = _sha(value[field], field)
        if (
            value["settlement_timezone"] != SETTLEMENT_TIMEZONE
            or value["authority_scope"] != AUTHORITY_SCOPE
            or value["reason"] != REASON
            or int(value["predecessor_bounds_version"]) != 1
            or int(value["successor_bounds_version"]) != 2
            or value["predecessor_revision_semantic_vector_sha256"]
            != value["successor_revision_semantic_vector_sha256"]
        ):
            raise ContractError("truth_authority_supersession_fact_semantics_invalid")
        value["predecessor_bounds_version"] = 1
        value["successor_bounds_version"] = 2
        supplied = _sha(value["content_sha256"], "fact_content_sha256")
        identity = {key: item for key, item in value.items() if key != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("truth_authority_supersession_fact_hash_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(
            str(self.raw[field])
            for field in (
                "station_id", "event_date", "settlement_timezone",
                "predecessor_truth_day_bounds_content_sha256",
            )
        )


@dataclass(frozen=True)
class CanonicalTruthAuthoritySupersessionContract:
    as_of_utc: str
    builder_id: str
    authority_scope_projection: dict[str, Any]
    review_scope_audit: dict[str, Any]
    awc_observation_contract: CanonicalAWCObservationContract
    supersession_facts: tuple[CanonicalTruthAuthoritySupersessionFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalTruthAuthoritySupersessionContract":
        fields = {
            "schema", "as_of_utc", "builder_id", "authority_scope_projection",
            "review_scope_audit", "awc_observation_contract",
            "supersession_facts", "content_sha256",
        }
        if set(raw) != fields or raw.get("schema") != CONTRACT_SCHEMA:
            raise ContractError("truth_authority_supersession_contract_fields_invalid")
        projection_raw = raw.get("authority_scope_projection")
        awc_raw = raw.get("awc_observation_contract")
        if not isinstance(projection_raw, Mapping) or not isinstance(awc_raw, Mapping):
            raise ContractError("truth_authority_supersession_contract_parent_missing")
        projection = validate_pr12_authority_scope_projection(projection_raw)
        review_audit_raw = raw.get("review_scope_audit")
        if not isinstance(review_audit_raw, Mapping):
            raise ContractError("truth_authority_review_audit_missing")
        review_audit = _validate_review_scope_audit(
            review_audit_raw, projection_sha=projection["content_sha256"]
        )
        awc = CanonicalAWCObservationContract.from_mapping(awc_raw)
        facts = tuple(
            CanonicalTruthAuthoritySupersessionFact.from_mapping(item)
            for item in raw.get("supersession_facts") or ()
        )
        blocked_keys = {
            (
                str(row["station_id"]), str(row["event_date"]),
                str(row["settlement_timezone"]),
            )
            for row in review_audit["review_scope_blockers"]
        }
        fact_keys = {
            (
                str(fact.raw["station_id"]), str(fact.raw["event_date"]),
                str(fact.raw["settlement_timezone"]),
            )
            for fact in facts
        }
        if not facts or awc.authority_scope_projection["content_sha256"] != projection["content_sha256"]:
            raise ContractError("truth_authority_supersession_contract_projection_mismatch")
        if (
            blocked_keys & fact_keys
            or int(review_audit["legacy_scope_count"])
            != len(fact_keys) + len(blocked_keys)
        ):
            raise ContractError("truth_authority_supersession_review_scope_mismatch")
        if any(
            fact.raw["authority_scope_projection_content_sha256"]
            != projection["content_sha256"]
            for fact in facts
        ):
            raise ContractError("truth_authority_supersession_contract_projection_mismatch")
        successor_bounds = {fact.content_sha256: fact for fact in awc.day_bounds_facts}
        revisions_by_day: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
        for revision in awc.revision_facts:
            key = tuple(
                str(revision.raw[field])
                for field in ("station_id", "event_date", "settlement_timezone")
            )
            revisions_by_day.setdefault(key, []).append(revision.raw)
        for fact in facts:
            successor = successor_bounds.get(
                fact.raw["successor_truth_day_bounds_content_sha256"]
            )
            key = tuple(
                str(fact.raw[field])
                for field in ("station_id", "event_date", "settlement_timezone")
            )
            authority = next(
                (row for row in projection["rows"] if row["station_id"] == key[0]),
                None,
            )
            if (
                successor is None
                or tuple(str(successor.raw[field]) for field in (
                    "station_id", "event_date", "settlement_timezone"
                )) != key
                or successor.raw["bounds_version"] != 2
                or _revision_semantic_vector(revisions_by_day.get(key, []))
                != fact.raw["successor_revision_semantic_vector_sha256"]
                or not isinstance(authority, Mapping)
                or authority["content_sha256"]
                != fact.raw["station_authority_row_content_sha256"]
            ):
                raise ContractError("truth_authority_supersession_successor_mismatch")
        supplied = _sha(raw.get("content_sha256"), "contract_content_sha256")
        identity = {key: item for key, item in raw.items() if key != "content_sha256"}
        if supplied != content_sha256(identity):
            raise ContractError("truth_authority_supersession_contract_hash_mismatch")
        return cls(
            str(raw["as_of_utc"]), str(raw["builder_id"]), projection,
            review_audit, awc, facts, supplied,
        )


def _legacy_fact(raw_json: str, *, kind: str) -> dict[str, Any]:
    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("truth_authority_supersession_legacy_json_invalid") from exc
    if not isinstance(raw, Mapping):
        raise ContractError("truth_authority_supersession_legacy_mapping_required")
    value = dict(raw)
    supplied = _sha(value.pop("content_sha256", ""), "legacy_content_sha256")
    if supplied != content_sha256(value):
        raise ContractError("truth_authority_supersession_legacy_hash_mismatch")
    value["content_sha256"] = supplied
    if (
        value.get("station_id") is None
        or value.get("event_date") is None
        or value.get("settlement_timezone") != SETTLEMENT_TIMEZONE
        or value.get("source_lane") != "AWC"
        or value.get("authority_scope") != AUTHORITY_SCOPE
        or "authority_scope_projection_content_sha256" in value
    ):
        raise ContractError("truth_authority_supersession_legacy_scope_invalid")
    if kind == "revision":
        if (
            value.get("raw_object_kind") != RAW_OBJECT_KIND
            or value.get("origin_source_id") not in AWC_ORIGIN_SOURCE_IDS
            or value.get("origin_authority") not in AWC_ORIGIN_AUTHORITIES
            or value.get("origin_url") != AWC_ORIGIN_URL
            or not str(value.get("origin_transport") or "").startswith("official_awc_")
            or value.get("correction_kind") != "NONE"
            or value.get("correction_state") != "NO_CORRECTION"
            or value.get("qc_state") != "ACCEPTED"
            or value.get("halt_state") != "CLEAR"
        ):
            raise ContractError("truth_authority_supersession_legacy_revision_invalid")
        for field in (
            "raw_object_sha256", "awc_source_receipt_sha256",
            "origin_registry_sha256", "origin_record_sha256",
        ):
            _sha(value.get(field), field)
    else:
        if (
            value.get("bounds_version") != 1
            or value.get("closed_day") is not True
            or value.get("correction_state") != "NO_CORRECTION"
            or value.get("halt_state") != "CLEAR"
        ):
            raise ContractError("truth_authority_supersession_legacy_bounds_invalid")
        coverage = value.get("coverage_receipt")
        if not isinstance(coverage, Mapping):
            raise ContractError("truth_authority_supersession_legacy_coverage_missing")
        coverage_value = dict(coverage)
        coverage_sha = _sha(
            coverage_value.pop("content_sha256", ""), "coverage_content_sha256"
        )
        if (
            coverage_value.get("schema") != COVERAGE_RECEIPT_SCHEMA
            or coverage_sha != content_sha256(coverage_value)
            or coverage_sha != value.get("coverage_receipt_sha256")
            or coverage_value.get("coverage_complete") is not True
            or coverage_value.get("expected_hourly_slots")
            != coverage_value.get("observed_hourly_slots")
        ):
            raise ContractError("truth_authority_supersession_legacy_coverage_invalid")
    return value


def build_canonical_truth_authority_supersession_contract(
    conn: sqlite3.Connection,
    *,
    authority_scope_projection: Mapping[str, Any],
    review_scope_audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebind immutable V1 AWC truth to the sealed authority parent.

    The returned contract is non-writing.  It contains V2 successors and the
    typed V1→V2 edge so the single canonical ingress can stage all rows in one
    transaction.
    """

    projection = validate_pr12_authority_scope_projection(
        authority_scope_projection
    )
    authority_by_station = {
        str(row["station_id"]): row for row in projection["rows"]
    }
    review_audit = _validate_review_scope_audit(
        review_scope_audit, projection_sha=projection["content_sha256"]
    )
    blocked_scopes = {
        (
            str(row["station_id"]),
            str(row["event_date"]),
            str(row["settlement_timezone"]),
        )
        for row in review_audit["review_scope_blockers"]
    }
    bounds_rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth_observation_day_bounds' ORDER BY scope_key"
    ).fetchall()
    revision_rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth_observation_revision' ORDER BY scope_key"
    ).fetchall()
    revisions: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for (raw_json,) in revision_rows:
        revision = _legacy_fact(str(raw_json), kind="revision")
        key = tuple(
            str(revision[field])
            for field in ("station_id", "event_date", "settlement_timezone")
        )
        if key in blocked_scopes:
            continue
        revisions.setdefault(key, []).append(revision)

    successor_revisions: list[dict[str, Any]] = []
    successor_bounds: list[dict[str, Any]] = []
    supersessions: list[dict[str, Any]] = []
    for (raw_json,) in bounds_rows:
        predecessor = _legacy_fact(str(raw_json), kind="bounds")
        key = tuple(
            str(predecessor[field])
            for field in ("station_id", "event_date", "settlement_timezone")
        )
        if key in blocked_scopes:
            continue
        scoped = sorted(revisions.get(key, []), key=lambda row: int(row["sequence"]))
        if not scoped or [int(row["sequence"]) for row in scoped] != list(
            range(1, len(scoped) + 1)
        ):
            raise ContractError("truth_authority_supersession_revision_sequence_invalid")
        if content_sha256([row["content_sha256"] for row in scoped]) != predecessor[
            "observation_revision_vector_sha256"
        ]:
            raise ContractError("truth_authority_supersession_revision_vector_mismatch")
        authority = authority_by_station.get(key[0])
        if not isinstance(authority, Mapping):
            raise ContractError("truth_authority_supersession_station_authority_missing")

        old_to_new: dict[str, dict[str, Any]] = {}
        for legacy in scoped:
            identity = {
                field: value
                for field, value in legacy.items()
                if field != "content_sha256"
            }
            identity["authority_scope_projection_content_sha256"] = projection[
                "content_sha256"
            ]
            successor = _sealed(identity)
            old_to_new[str(legacy["content_sha256"])] = successor
            successor_revisions.append(successor)

        new_scoped = [old_to_new[str(row["content_sha256"])] for row in scoped]
        successor_identity = {
            field: value
            for field, value in predecessor.items()
            if field != "content_sha256"
        }
        successor_identity.update(
            {
                "bounds_version": 2,
                "authority_scope_projection_content_sha256": projection[
                    "content_sha256"
                ],
                "max_observation_fact_sha256": old_to_new[
                    str(predecessor["max_observation_fact_sha256"])
                ]["content_sha256"],
                "observation_revision_vector_sha256": content_sha256(
                    [row["content_sha256"] for row in new_scoped]
                ),
            }
        )
        maximum_value = float(predecessor["running_max_c"])
        successor_identity["max_tie_vector_sha256"] = content_sha256(
            sorted(
                row["content_sha256"]
                for row in new_scoped
                if float(row["reported_temperature_c"]) == maximum_value
            )
        )
        successor = _sealed(successor_identity)
        successor_bounds.append(successor)
        semantic_vector = _revision_semantic_vector(scoped)
        supersessions.append(
            _sealed(
                {
                    "schema": FACT_SCHEMA,
                    "station_id": key[0],
                    "event_date": key[1],
                    "settlement_timezone": key[2],
                    "predecessor_truth_day_bounds_content_sha256": predecessor[
                        "content_sha256"
                    ],
                    "predecessor_revision_semantic_vector_sha256": semantic_vector,
                    "successor_truth_day_bounds_content_sha256": successor[
                        "content_sha256"
                    ],
                    "successor_revision_semantic_vector_sha256": _revision_semantic_vector(
                        new_scoped
                    ),
                    "authority_scope_projection_content_sha256": projection[
                        "content_sha256"
                    ],
                    "station_authority_row_content_sha256": authority[
                        "content_sha256"
                    ],
                    "predecessor_bounds_version": 1,
                    "successor_bounds_version": 2,
                    "reason": REASON,
                    "authority_as_of_utc": projection["as_of_utc"],
                    "authority_scope": AUTHORITY_SCOPE,
                }
            )
        )

    awc_identity = {
        "schema": AWC_CONTRACT_SCHEMA,
        "as_of_utc": projection["as_of_utc"],
        "builder_id": BUILDER_ID,
        "authority_scope_projection": projection,
        "revision_facts": successor_revisions,
        "day_bounds_facts": successor_bounds,
    }
    awc_contract = _sealed(awc_identity)
    CanonicalAWCObservationContract.from_mapping(awc_contract)
    identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": projection["as_of_utc"],
        "builder_id": BUILDER_ID,
        "authority_scope_projection": projection,
        "review_scope_audit": review_audit,
        "awc_observation_contract": awc_contract,
        "supersession_facts": supersessions,
    }
    contract = _sealed(identity)
    CanonicalTruthAuthoritySupersessionContract.from_mapping(contract)
    return contract


def assert_truth_authority_supersession_references(
    conn: sqlite3.Connection,
    contract: CanonicalTruthAuthoritySupersessionContract,
) -> None:
    """Prove every typed edge against the immutable persisted V1 ancestor."""

    revision_rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth_observation_revision' ORDER BY scope_key"
    ).fetchall()
    legacy_by_day: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for (raw_json,) in revision_rows:
        try:
            revision = _legacy_fact(str(raw_json), kind="revision")
        except ContractError:
            continue
        key = tuple(
            str(revision[field])
            for field in ("station_id", "event_date", "settlement_timezone")
        )
        legacy_by_day.setdefault(key, []).append(revision)
    for edge in contract.supersession_facts:
        raw = edge.raw
        row = conn.execute(
            "SELECT fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='truth_observation_day_bounds' AND content_sha256=?",
            (raw["predecessor_truth_day_bounds_content_sha256"],),
        ).fetchone()
        if row is None:
            raise ContractError(
                "truth_authority_supersession_predecessor_missing"
            )
        predecessor = _legacy_fact(str(row[0]), kind="bounds")
        key = tuple(
            str(raw[field])
            for field in ("station_id", "event_date", "settlement_timezone")
        )
        if tuple(
            str(predecessor[field])
            for field in ("station_id", "event_date", "settlement_timezone")
        ) != key:
            raise ContractError(
                "truth_authority_supersession_predecessor_scope_mismatch"
            )
        scoped = sorted(
            legacy_by_day.get(key, []), key=lambda item: int(item["sequence"])
        )
        if (
            not scoped
            or content_sha256([item["content_sha256"] for item in scoped])
            != predecessor["observation_revision_vector_sha256"]
            or _revision_semantic_vector(scoped)
            != raw["predecessor_revision_semantic_vector_sha256"]
        ):
            raise ContractError(
                "truth_authority_supersession_predecessor_semantics_mismatch"
            )


def resolve_effective_truth_day_root(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    authority_scope_projection_content_sha256: str,
    as_of_utc: str,
) -> dict[str, Any]:
    """Return only the V2 head of one persisted, fully verified V1→V2 edge."""

    station = str(station_id).strip().upper()
    projection_sha = _sha(
        authority_scope_projection_content_sha256,
        "resolver_authority_scope_projection_content_sha256",
    )
    edges: list[CanonicalTruthAuthoritySupersessionFact] = []
    for (raw_json,) in conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth_authority_supersession' ORDER BY scope_key"
    ):
        try:
            parsed = json.loads(str(raw_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(
                "truth_authority_supersession_resolver_json_invalid"
            ) from exc
        edge = CanonicalTruthAuthoritySupersessionFact.from_mapping(parsed)
        if (
            edge.raw["station_id"] == station
            and edge.raw["event_date"] == event_date
            and edge.raw["settlement_timezone"] == settlement_timezone
        ):
            edges.append(edge)
    if not edges:
        raise ContractError("TRUTH_AUTHORITY_SUPERSESSION_REQUIRED")
    if len(edges) != 1:
        raise ContractError("TRUTH_AUTHORITY_SUPERSESSION_NOT_UNIQUE")
    edge = edges[0]
    if (
        edge.raw["authority_scope_projection_content_sha256"] != projection_sha
        or edge.raw["authority_as_of_utc"] > as_of_utc
    ):
        raise ContractError("TRUTH_AUTHORITY_SUPERSESSION_PROJECTION_MISMATCH")
    predecessor_row = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth_observation_day_bounds' AND content_sha256=?",
        (edge.raw["predecessor_truth_day_bounds_content_sha256"],),
    ).fetchone()
    successor_row = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth_observation_day_bounds' AND content_sha256=?",
        (edge.raw["successor_truth_day_bounds_content_sha256"],),
    ).fetchone()
    if predecessor_row is None or successor_row is None:
        raise ContractError("TRUTH_AUTHORITY_SUPERSESSION_ENDPOINT_MISSING")
    predecessor = _legacy_fact(str(predecessor_row[0]), kind="bounds")
    try:
        successor_raw = json.loads(str(successor_row[0]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("TRUTH_AUTHORITY_SUPERSESSION_SUCCESSOR_INVALID") from exc
    successor = CanonicalAWCObservationDayBoundsFact.from_mapping(successor_raw).raw
    key = (station, event_date, settlement_timezone)
    if (
        tuple(
            str(predecessor[field])
            for field in ("station_id", "event_date", "settlement_timezone")
        )
        != key
        or tuple(
            str(successor[field])
            for field in ("station_id", "event_date", "settlement_timezone")
        )
        != key
        or successor["bounds_version"] != 2
        or successor["authority_scope_projection_content_sha256"] != projection_sha
        or successor["canonical_committed_at_utc"] > as_of_utc
    ):
        raise ContractError("TRUTH_AUTHORITY_SUPERSESSION_ENDPOINT_SCOPE_MISMATCH")
    revision_rows: list[dict[str, Any]] = []
    for (raw_json,) in conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth_observation_revision' ORDER BY scope_key"
    ):
        try:
            raw = json.loads(str(raw_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("TRUTH_AUTHORITY_SUPERSESSION_REVISION_INVALID") from exc
        if not isinstance(raw, Mapping) or raw.get(
            "authority_scope_projection_content_sha256"
        ) != projection_sha:
            continue
        revision = CanonicalAWCObservationRevisionFact.from_mapping(raw).raw
        if tuple(
            str(revision[field])
            for field in ("station_id", "event_date", "settlement_timezone")
        ) == key:
            revision_rows.append(revision)
    revision_rows.sort(key=lambda row: int(row["sequence"]))
    if (
        not revision_rows
        or content_sha256([row["content_sha256"] for row in revision_rows])
        != successor["observation_revision_vector_sha256"]
        or _revision_semantic_vector(revision_rows)
        != edge.raw["successor_revision_semantic_vector_sha256"]
    ):
        raise ContractError("TRUTH_AUTHORITY_SUPERSESSION_SUCCESSOR_SEMANTICS_MISMATCH")
    incoming = conn.execute(
        "SELECT COUNT(*) FROM canonical_ssot_fact "
        "WHERE fact_type='truth_authority_supersession' "
        "AND json_extract(fact_json,'$.successor_truth_day_bounds_content_sha256')=?",
        (successor["content_sha256"],),
    ).fetchone()[0]
    if int(incoming) != 1:
        raise ContractError("TRUTH_AUTHORITY_SUPERSESSION_SUCCESSOR_NOT_UNIQUE")
    return successor


def build_truth_authority_supersession_bundle(
    conn: sqlite3.Connection, *, authority_scope_projection: Mapping[str, Any],
) -> dict[str, Any]:
    projection = validate_pr12_authority_scope_projection(authority_scope_projection)
    authority_by_station = {
        str(row["station_id"]): row for row in projection["rows"]
    }
    bounds_rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth_observation_day_bounds' ORDER BY scope_key"
    ).fetchall()
    revision_rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth_observation_revision' ORDER BY scope_key"
    ).fetchall()
    revisions: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    terminals: list[dict[str, Any]] = []
    for (raw_json,) in revision_rows:
        try:
            revision = _legacy_fact(str(raw_json), kind="revision")
        except ContractError:
            continue
        key = (
            str(revision["station_id"]), str(revision["event_date"]),
            str(revision["settlement_timezone"]),
        )
        revisions.setdefault(key, []).append(revision)
    candidates: list[dict[str, Any]] = []
    for (raw_json,) in bounds_rows:
        try:
            bounds = _legacy_fact(str(raw_json), kind="bounds")
            key = (
                str(bounds["station_id"]), str(bounds["event_date"]),
                str(bounds["settlement_timezone"]),
            )
            scoped = sorted(revisions.get(key, []), key=lambda row: int(row["sequence"]))
            if not scoped or [int(row["sequence"]) for row in scoped] != list(
                range(1, len(scoped) + 1)
            ):
                raise ContractError("truth_authority_supersession_revision_sequence_invalid")
            revision_vector = [row["content_sha256"] for row in scoped]
            if content_sha256(revision_vector) != bounds["observation_revision_vector_sha256"]:
                raise ContractError("truth_authority_supersession_revision_vector_mismatch")
            maximum = max(float(row["reported_temperature_c"]) for row in scoped)
            if (
                maximum != float(bounds["running_max_c"])
                or bounds["max_observation_fact_sha256"] not in revision_vector
            ):
                raise ContractError("truth_authority_supersession_maximum_mismatch")
            authority = authority_by_station.get(key[0])
            if not isinstance(authority, Mapping):
                raise ContractError("truth_authority_supersession_station_authority_missing")
            semantic_identity = {
                "station_id": key[0], "event_date": key[1],
                "settlement_timezone": key[2],
                "truth_value_c": float(bounds["running_max_c"]),
                "predecessor_truth_day_bounds_content_sha256": bounds["content_sha256"],
                "predecessor_revision_vector_sha256": bounds[
                    "observation_revision_vector_sha256"
                ],
                "predecessor_coverage_receipt_sha256": bounds[
                    "coverage_receipt_sha256"
                ],
            }
            candidates.append(_sealed({
                "schema": CANDIDATE_SCHEMA,
                "builder_id": BUILDER_ID,
                "state": "MATERIALIZED_UNPUBLISHED",
                "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                **semantic_identity,
                "authority_scope_projection_content_sha256": projection[
                    "content_sha256"
                ],
                "station_authority_row_content_sha256": authority["content_sha256"],
                "semantic_equivalence_sha256": content_sha256(semantic_identity),
                "writes_database": False,
                "consumable": False,
            }))
        except ContractError as exc:
            terminals.append(_sealed({
                "schema": TERMINAL_SCHEMA, "builder_id": BUILDER_ID,
                "state": "BLOCKED", "blocker": "LINEAGE_MISMATCH",
                "detail": str(exc), "legacy_fact_json_sha256": content_sha256(str(raw_json)),
                "authority_scope_projection_content_sha256": projection["content_sha256"],
                "writes_database": False, "consumable": False,
            }))
    identity = {
        "schema": SCHEMA, "builder_id": BUILDER_ID,
        "state": "BLOCKED" if terminals else "MATERIALIZED_UNPUBLISHED",
        "release_state": "BLOCKED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "authority_scope_projection_content_sha256": projection["content_sha256"],
        "legacy_bounds_scope_count": len(bounds_rows),
        "candidate_count": len(candidates), "terminal_count": len(terminals),
        "candidates": candidates, "terminals": terminals,
        "writes_database": False, "publishes_pr04_pointer": False,
        "starts_services": False, "releases_live_pause": False,
        "orders_submitted": 0, "consumable": False,
    }
    return _sealed(identity)


__all__ = [
    "BUILDER_ID",
    "CONTRACT_SCHEMA",
    "FACT_SCHEMA",
    "REVIEW_AUDIT_SCHEMA",
    "CanonicalTruthAuthoritySupersessionContract",
    "CanonicalTruthAuthoritySupersessionFact",
    "assert_truth_authority_supersession_references",
    "build_canonical_truth_authority_supersession_contract",
    "build_truth_authority_review_scope_audit",
    "build_truth_authority_supersession_bundle",
    "resolve_effective_truth_day_root",
]
