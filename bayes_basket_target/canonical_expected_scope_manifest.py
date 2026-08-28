"""Independent expected-scope basis contracts for the PR04 release gate.

The release layer observes terminal rows.  This module owns the opposite side
of that comparison: domain builders declare their expected natural keys from
their authoritative input inventories.  It never reads a release preview or a
typed observed envelope, so expected coverage cannot be inferred from what
happened to materialize.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from .canonical_activation_release import (
    EXPECTED_SCOPE_MANIFEST_SCHEMA,
    MODEL_OOS_SCOPE,
    REQUIRED_SCOPE_TYPES,
)
from .canonical_contract import (
    CanonicalRepublisherNoLiveActivationFact,
    TYPED_SCOPE_NATURAL_KEY_FIELDS,
    normalize_typed_scope_natural_key,
)
from .canonical_typed_scope_builder import (
    _P0NaturalKeyLineageMismatch,
    _activation_for_p0_snapshot,
    _bridge_for_p0_snapshot,
    _forward_for_p0_snapshot,
    _json_mapping,
    _p0_selection_key,
)
from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .model_system_coverage import validate_coverage_matrix
from .republisher_no_live_p0_contract import CanonicalP0SnapshotFact
from .station_registry import coordinate_registry_manifest
from .fleet import expected_station_count


EXPECTED_SCOPE_BASIS_SCHEMA = (
    "bayes_basket_target_canonical_expected_scope_basis_v1"
)
_HEX = frozenset("0123456789abcdef")
_RUNTIME_LABELS = (
    "com.weather.clob_order_service",
    "com.weather.market_ws",
    "com.weather.weather_basket_yes",
)
P0_EXPECTED_SCOPE_BUNDLE_SCHEMA = (
    "bayes_basket_target_p0_expected_scope_basis_bundle_v1"
)
P0_EXPECTED_SCOPE_UNRESOLVED_SCHEMA = (
    "bayes_basket_target_p0_expected_scope_unresolved_receipt_v1"
)


class CanonicalExpectedScopeError(ContractError):
    """An expected-scope declaration cannot be proven without guessing."""


def _utc_z(value: object, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanonicalExpectedScopeError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CanonicalExpectedScopeError(f"{field}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha(value: object, field: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(character not in _HEX for character in result):
        raise CanonicalExpectedScopeError(f"{field}_invalid")
    return result


def _canonical_stations() -> tuple[str, ...]:
    stations = tuple(coordinate_registry_manifest()["station_ids"])
    if len(stations) != expected_station_count() or stations != tuple(sorted(set(stations))):
        raise CanonicalExpectedScopeError("expected_scope_registry_not_exact_45")
    return stations


def _normalized_key(scope_type: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CanonicalExpectedScopeError("expected_scope_basis_key_invalid")
    scope_kind = str(value.get("scope_kind") or "").strip().upper()
    natural_key = value.get("natural_key")
    if not scope_kind or not isinstance(natural_key, Mapping):
        raise CanonicalExpectedScopeError("expected_scope_basis_key_invalid")
    natural_key = dict(natural_key)
    if scope_type == MODEL_OOS_SCOPE:
        if scope_kind != "BUILDER" or set(natural_key) != {
            "station_id", "model_family", "lead_bucket", "run_cycle"
        }:
            raise CanonicalExpectedScopeError("expected_scope_basis_model_key_invalid")
    else:
        required = TYPED_SCOPE_NATURAL_KEY_FIELDS.get((scope_type, scope_kind))
        if required is None or set(natural_key) != set(required):
            raise CanonicalExpectedScopeError("expected_scope_basis_typed_key_invalid")
        try:
            natural_key = normalize_typed_scope_natural_key(
                scope_type=scope_type,
                scope_kind=scope_kind,
                raw=natural_key,
            )
        except ContractError as exc:
            raise CanonicalExpectedScopeError(
                "expected_scope_basis_typed_key_invalid"
            ) from exc
    if "station_id" in natural_key:
        station = str(natural_key["station_id"] or "").strip().upper()
        if station not in set(_canonical_stations()):
            raise CanonicalExpectedScopeError("expected_scope_basis_station_invalid")
        natural_key["station_id"] = station
    return {"scope_kind": scope_kind, "natural_key": natural_key}


def _ordered_keys(
    scope_type: str, values: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    normalized = [_normalized_key(scope_type, value) for value in values]
    ordered = sorted(
        normalized,
        key=lambda value: json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )
    tokens = [content_sha256(value) for value in ordered]
    if len(tokens) != len(set(tokens)):
        raise CanonicalExpectedScopeError("expected_scope_basis_key_duplicate")
    return ordered


def build_scope_basis(
    *,
    scope_type: str,
    as_of_utc: str,
    expected_keys: Iterable[Mapping[str, Any]],
    source_content_sha256s: Iterable[str],
    expected_key_evidence: Iterable[Mapping[str, Any]] = (),
    unresolved_input_receipts: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one immutable domain declaration without reading observed rows."""

    scope = str(scope_type or "").strip().upper()
    if scope not in REQUIRED_SCOPE_TYPES:
        raise CanonicalExpectedScopeError("expected_scope_basis_type_invalid")
    cutoff = _utc_z(as_of_utc, "expected_scope_basis_as_of_utc")
    keys = _ordered_keys(scope, expected_keys)
    evidence_rows: list[dict[str, Any]] = []
    for raw in expected_key_evidence:
        if not isinstance(raw, Mapping):
            raise CanonicalExpectedScopeError(
                "expected_scope_basis_key_evidence_invalid"
            )
        key = _normalized_key(scope, raw.get("expected_key") or {})
        evidence = raw.get("evidence")
        if not isinstance(evidence, Mapping):
            raise CanonicalExpectedScopeError(
                "expected_scope_basis_key_evidence_invalid"
            )
        evidence = dict(evidence)
        supplied = _sha(
            raw.get("evidence_content_sha256"),
            "expected_scope_basis_key_evidence_sha256",
        )
        if supplied != content_sha256(evidence):
            raise CanonicalExpectedScopeError(
                "expected_scope_basis_key_evidence_sha256_mismatch"
            )
        evidence_rows.append(
            {
                "expected_key": key,
                "evidence": evidence,
                "evidence_content_sha256": supplied,
            }
        )
    evidence_rows.sort(
        key=lambda item: json.dumps(
            item["expected_key"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    evidence_tokens = [
        content_sha256(item["expected_key"]) for item in evidence_rows
    ]
    key_tokens = [content_sha256(item) for item in keys]
    if scope == MODEL_OOS_SCOPE:
        if evidence_tokens != key_tokens:
            raise CanonicalExpectedScopeError(
                "expected_scope_basis_model_key_evidence_incomplete"
            )
    elif evidence_rows:
        raise CanonicalExpectedScopeError(
            "expected_scope_basis_typed_key_evidence_forbidden"
        )
    sources = sorted(
        {_sha(value, "expected_scope_basis_source_sha256") for value in source_content_sha256s}
    )
    if not sources:
        raise CanonicalExpectedScopeError("expected_scope_basis_sources_missing")
    unresolved = sorted(
        {_sha(value, "expected_scope_basis_unresolved_sha256") for value in unresolved_input_receipts}
    )
    stations = list(_canonical_stations())
    registry_sha = _sha(
        coordinate_registry_manifest()["registry_sha256"],
        "expected_scope_basis_registry_sha256",
    )
    if scope == "RUNTIME":
        labels = tuple(
            sorted(str(item["natural_key"].get("service_label") or "") for item in keys)
        )
        if labels and labels != _RUNTIME_LABELS:
            raise CanonicalExpectedScopeError("expected_scope_basis_runtime_services_invalid")
        if not labels and len(unresolved) != len(_RUNTIME_LABELS):
            raise CanonicalExpectedScopeError(
                "expected_scope_basis_runtime_unresolved_services_invalid"
            )
    identity = {
        "schema": EXPECTED_SCOPE_BASIS_SCHEMA,
        "scope_type": scope,
        "as_of_utc": cutoff,
        "canonical_registry_sha256": registry_sha,
        "station_ids": stations,
        "expected_keys": keys,
        "expected_key_evidence": evidence_rows,
        "source_content_sha256s": sources,
        "unresolved_input_receipts": unresolved,
        "production_no_live": True,
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def validate_scope_basis(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CanonicalExpectedScopeError("expected_scope_basis_mapping_required")
    supplied = _sha(value.get("content_sha256"), "expected_scope_basis_content_sha256")
    identity = {key: child for key, child in value.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise CanonicalExpectedScopeError("expected_scope_basis_content_sha256_mismatch")
    if value.get("schema") != EXPECTED_SCOPE_BASIS_SCHEMA:
        raise CanonicalExpectedScopeError("expected_scope_basis_schema_invalid")
    rebuilt = build_scope_basis(
        scope_type=str(value.get("scope_type") or ""),
        as_of_utc=str(value.get("as_of_utc") or ""),
        expected_keys=value.get("expected_keys") or (),
        expected_key_evidence=value.get("expected_key_evidence") or (),
        source_content_sha256s=value.get("source_content_sha256s") or (),
        unresolved_input_receipts=value.get("unresolved_input_receipts") or (),
    )
    if dict(value) != rebuilt:
        raise CanonicalExpectedScopeError("expected_scope_basis_identity_mismatch")
    return rebuilt


def build_model_oos_scope_basis(
    coverage_matrix: Mapping[str, Any], *, release_as_of_utc: str
) -> dict[str, Any]:
    """Declare MODEL_OOS builder keys from the frozen PR05--PR09 matrix."""

    matrix = validate_coverage_matrix(coverage_matrix)
    source_as_of = _utc_z(matrix["as_of_utc"], "model_oos_basis_source_as_of_utc")
    release_as_of = _utc_z(release_as_of_utc, "model_oos_basis_release_as_of_utc")
    if source_as_of > release_as_of:
        raise CanonicalExpectedScopeError("model_oos_basis_source_after_release")
    grouped: dict[tuple[str, str, str, object], list[Mapping[str, Any]]] = {}
    for row in matrix["rows"]:
        key = (
            str(row["station_id"]),
            str(row["model_family"]),
            str(row["lead_bucket"]),
            row.get("run_cycle"),
        )
        grouped.setdefault(key, []).append(row)
    keys = set(grouped)
    expected_keys = [
        {
            "scope_kind": "BUILDER",
            "natural_key": {
                "station_id": station_id,
                "model_family": model_family,
                "lead_bucket": lead_bucket,
                "run_cycle": run_cycle,
            },
        }
        for station_id, model_family, lead_bucket, run_cycle in keys
    ]
    expected_key_evidence = []
    for station_id, model_family, lead_bucket, run_cycle in keys:
        expected_key = {
            "scope_kind": "BUILDER",
            "natural_key": {
                "station_id": station_id,
                "model_family": model_family,
                "lead_bucket": lead_bucket,
                "run_cycle": run_cycle,
            },
        }
        components = [
            {
                "product_id": str(row["product_id"]),
                "provider_id": str(row["provider_id"]),
                "source_class": str(row["source_class"]),
                "independence_family_id": str(row["independence_family_id"]),
                "run_cycle": row.get("run_cycle"),
                "coverage_row_content_sha256": str(row["content_sha256"]),
            }
            for row in grouped[(station_id, model_family, lead_bucket, run_cycle)]
        ]
        components.sort(
            key=lambda item: (
                item["product_id"],
                item["provider_id"],
                item["source_class"],
                item["independence_family_id"],
            )
        )
        if len({item["product_id"] for item in components}) != len(components):
            raise CanonicalExpectedScopeError(
                "model_oos_basis_product_vector_duplicate"
            )
        evidence = {
            "schema": "bayes_basket_target_model_oos_family_builder_basis_v1",
            "coverage_matrix_content_sha256": matrix["content_sha256"],
            "coverage_matrix_as_of_utc": source_as_of,
            "components": components,
        }
        expected_key_evidence.append(
            {
                "expected_key": expected_key,
                "evidence": evidence,
                "evidence_content_sha256": content_sha256(evidence),
            }
        )
    return build_scope_basis(
        scope_type=MODEL_OOS_SCOPE,
        as_of_utc=release_as_of,
        expected_keys=expected_keys,
        expected_key_evidence=expected_key_evidence,
        source_content_sha256s=(matrix["content_sha256"],),
    )


def _p0_unresolved_receipt(
    *,
    scope_type: str,
    as_of_utc: str,
    source_inventory_content_sha256: str,
    reason: str,
    source_fact_id: object = "",
    source_content_sha256: object = "",
) -> dict[str, Any]:
    identity = {
        "schema": P0_EXPECTED_SCOPE_UNRESOLVED_SCHEMA,
        "scope_type": str(scope_type),
        "as_of_utc": as_of_utc,
        "reason": str(reason or "p0_expected_scope_unresolved"),
        "source_inventory_content_sha256": _sha(
            source_inventory_content_sha256,
            "p0_basis_source_inventory_content_sha256",
        ),
        "source_fact_id": str(source_fact_id or ""),
        "source_content_sha256": (
            ""
            if not source_content_sha256
            else _sha(source_content_sha256, "p0_basis_source_content_sha256")
        ),
        "production_no_live": True,
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _p0_probability_envelope(
    conn: sqlite3.Connection,
    *,
    snapshot: CanonicalP0SnapshotFact,
    activation: CanonicalRepublisherNoLiveActivationFact,
    forward_content_sha256: str,
    cutoff: str,
) -> tuple[ProbabilityEnvelope, str, str, dict[str, str]]:
    rows = conn.execute(
        "SELECT stage,checkpoint_id,station_id,event_date,settlement_timezone,"
        "decision_at_utc,fresh_until_utc,bucket_ordinals_json,probabilities_json,"
        "posterior_version,posterior_sha256,bridge_sha256,lineage_json,content_sha256,created_at "
        "FROM basket_probability_envelope WHERE envelope_id=? AND state='COMMITTED'",
        (snapshot.raw["snapshot_id"],),
    ).fetchall()
    if len(rows) != 1:
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_probability_envelope_missing"
        )
    row = rows[0]
    try:
        envelope = ProbabilityEnvelope.from_mapping(
            {
                "stage": row[0],
                "checkpoint_id": row[1],
                "station_id": row[2],
                "event_date": row[3],
                "settlement_timezone": row[4],
                "decision_at_utc": row[5],
                "fresh_until_utc": row[6],
                "bucket_ordinals": _json_mapping(
                    row[7], "p0_basis_bucket_ordinals_json"
                ),
                "probabilities": _json_mapping(
                    row[8], "p0_basis_probabilities_json"
                ),
                "posterior_version": row[9],
                "posterior_sha256": row[10],
                "bridge_sha256": row[11],
                "lineage": _json_mapping(row[12], "p0_basis_lineage_json"),
            }
        )
    except ContractError as exc:
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_probability_envelope_contract_invalid"
        ) from exc
    envelope_sha = _sha(row[13], "p0_basis_probability_envelope_content_sha256")
    created_at = _utc_z(row[14], "p0_basis_probability_envelope_created_at")
    lineage = dict(envelope.lineage)
    if (
        envelope.content_sha256 != envelope_sha
        or envelope.stage != "P0"
        or envelope.station_id != snapshot.raw["station_id"]
        or envelope.event_date != snapshot.raw["event_date"]
        or envelope.settlement_timezone != snapshot.raw["settlement_timezone"]
        or envelope.decision_at_utc != snapshot.raw["decision_at_utc"]
        or envelope.fresh_until_utc != snapshot.raw["fresh_until_utc"]
        or envelope.posterior_sha256 != snapshot.raw["posterior_sha256"]
        or envelope.bridge_sha256 != snapshot.raw["market_bucket_bridge_sha256"]
        or lineage.get("p0_snapshot_content_sha256") != snapshot.content_sha256
        or lineage.get("activation_content_sha256")
        != snapshot.raw["activation_content_sha256"]
        or lineage.get("forward_applicability_content_sha256")
        != forward_content_sha256
        or lineage.get("market_bucket_bridge_content_sha256")
        != snapshot.raw["market_bucket_bridge_sha256"]
        or lineage.get("release_state") != "MATERIALIZED_UNPUBLISHED"
        or created_at > cutoff
        or snapshot.raw["activation_generation"] != activation.activation_generation
        or snapshot.raw["probability_distribution_artifact_content_sha256"]
        != activation.temperature_distribution_artifact_content_sha256
    ):
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_probability_binding_mismatch"
        )
    return envelope, envelope_sha, created_at, {
        "source_id": str(row[1]),
        "content_sha256": envelope_sha,
    }


def _unique_p0_expected_keys(
    *,
    scope_type: str,
    candidates: Sequence[Mapping[str, Any]],
    cutoff: str,
    inventory_sha: str,
    receipts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in candidates:
        grouped.setdefault(content_sha256(item["expected_key"]), []).append(item)
    keys: list[dict[str, Any]] = []
    for rows in grouped.values():
        if len(rows) == 1:
            keys.append(dict(rows[0]["expected_key"]))
            continue
        receipts.append(
            _p0_unresolved_receipt(
                scope_type=scope_type,
                as_of_utc=cutoff,
                source_inventory_content_sha256=inventory_sha,
                reason="p0_expected_scope_natural_key_ambiguous",
                source_fact_id="|".join(sorted(str(row["source_fact_id"]) for row in rows)),
            )
        )
    return keys


def build_p0_expected_scope_bases(
    db_path: str | Path, *, release_as_of_utc: str
) -> dict[str, Any]:
    """Build PR10 P0 bases directly from one read-only canonical snapshot.

    This function never reads a release preview or typed observed envelope.
    Unprovable lineage produces content-addressed unresolved receipts, never a
    guessed natural key.
    """

    cutoff = _utc_z(release_as_of_utc, "p0_basis_release_as_of_utc")
    source = Path(db_path).expanduser().resolve(strict=True)
    activation_candidates: list[dict[str, Any]] = []
    snapshot_candidates: list[dict[str, Any]] = []
    activation_receipts: list[dict[str, Any]] = []
    snapshot_receipts: list[dict[str, Any]] = []
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        inventory_rows = [
            {
                "fact_id": str(row[0]),
                "fact_type": str(row[1]),
                "content_sha256": _sha(row[2], "p0_basis_inventory_content_sha256"),
                "ingested_at_utc": _utc_z(row[3], "p0_basis_inventory_ingested_at_utc"),
            }
            for row in conn.execute(
                "SELECT fact_id,fact_type,content_sha256,ingested_at_utc "
                "FROM canonical_ssot_fact WHERE fact_type IN "
                "('republisher_cross_selection','republisher_no_live_activation',"
                "'forward_applicability','p0_snapshot','market_bucket_bridge') "
                "AND ingested_at_utc<=? ORDER BY fact_type,scope_key,fact_id",
                (cutoff,),
            )
        ]
        envelope_rows = [
            {
                "envelope_id": str(row[0]),
                "checkpoint_id": str(row[1]),
                "content_sha256": _sha(row[2], "p0_basis_inventory_envelope_sha256"),
                "created_at_utc": _utc_z(row[3], "p0_basis_inventory_envelope_created_at"),
            }
            for row in conn.execute(
                "SELECT envelope_id,checkpoint_id,content_sha256,created_at "
                "FROM basket_probability_envelope WHERE stage='P0' "
                "AND state='COMMITTED' AND created_at<=? ORDER BY envelope_id",
                (cutoff,),
            )
        ]
        inventory_identity = {
            "schema": "bayes_basket_target_p0_expected_scope_source_inventory_v1",
            "as_of_utc": cutoff,
            "database_data_version": int(conn.execute("PRAGMA data_version").fetchone()[0]),
            "database_schema_version": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
            "canonical_facts": inventory_rows,
            "p0_probability_envelopes": envelope_rows,
        }
        inventory_sha = content_sha256(inventory_identity)

        activation_rows = conn.execute(
            "SELECT fact_id,fact_json,content_sha256 FROM canonical_ssot_fact "
            "WHERE fact_type='republisher_no_live_activation' "
            "AND json_extract(fact_json,'$.activation_generation')>=2 "
            "AND json_extract(fact_json,'$.activated_at_utc')<=? "
            "AND ingested_at_utc<=? ORDER BY scope_key,fact_id",
            (cutoff, cutoff),
        ).fetchall()
        for fact_id, fact_json, stored_sha in activation_rows:
            try:
                activation = CanonicalRepublisherNoLiveActivationFact.from_mapping(
                    _json_mapping(fact_json, "p0_basis_activation_fact_json")
                )
                if (
                    _sha(stored_sha, "p0_basis_activation_source_sha256")
                    != activation.content_sha256
                    or activation.activation_generation < 2
                    or activation.temperature_distribution_artifact_content_sha256 is None
                    or activation.authority_scope != "PRODUCTION_NO_LIVE_ONLY"
                    or activation.formal_live_authority
                ):
                    raise _P0NaturalKeyLineageMismatch(
                        "P0_ACTIVATION", "p0_activation_lineage_invalid"
                    )
                key, _selection, _selection_source = _p0_selection_key(
                    conn, activation=activation, cutoff=cutoff
                )
                activation_candidates.append(
                    {
                        "expected_key": {
                            "scope_kind": "ACTIVATION",
                            "natural_key": key,
                        },
                        "source_fact_id": str(fact_id),
                    }
                )
            except (ContractError, _P0NaturalKeyLineageMismatch) as exc:
                activation_receipts.append(
                    _p0_unresolved_receipt(
                        scope_type="P0_ACTIVATION",
                        as_of_utc=cutoff,
                        source_inventory_content_sha256=inventory_sha,
                        reason=getattr(exc, "reason", str(exc) or exc.__class__.__name__),
                        source_fact_id=fact_id,
                        source_content_sha256=stored_sha,
                    )
                )

        snapshot_rows = conn.execute(
            "SELECT fact_id,fact_json,content_sha256 FROM canonical_ssot_fact "
            "WHERE fact_type='p0_snapshot' "
            "AND json_extract(fact_json,'$.decision_at_utc')<=? "
            "AND ingested_at_utc<=? ORDER BY scope_key,fact_id",
            (cutoff, cutoff),
        ).fetchall()
        for fact_id, fact_json, stored_sha in snapshot_rows:
            try:
                snapshot = CanonicalP0SnapshotFact.from_mapping(
                    _json_mapping(fact_json, "p0_basis_snapshot_fact_json")
                )
                if _sha(stored_sha, "p0_basis_snapshot_source_sha256") != snapshot.content_sha256:
                    raise _P0NaturalKeyLineageMismatch(
                        "P0_SNAPSHOT", "p0_snapshot_fact_hash_mismatch"
                    )
                activation, activation_key, selection, _activation_sources = (
                    _activation_for_p0_snapshot(
                        conn,
                        activation_content_sha256=snapshot.raw[
                            "activation_content_sha256"
                        ],
                        cutoff=cutoff,
                    )
                )
                forward, _forward_source = _forward_for_p0_snapshot(
                    conn,
                    snapshot=snapshot,
                    activation=activation,
                    key=activation_key,
                    cutoff=cutoff,
                )
                _bridge, _bridge_source = _bridge_for_p0_snapshot(
                    conn, snapshot=snapshot, cutoff=cutoff
                )
                _envelope, _envelope_sha, _created_at, _envelope_source = (
                    _p0_probability_envelope(
                        conn,
                        snapshot=snapshot,
                        activation=activation,
                        forward_content_sha256=forward.content_sha256,
                        cutoff=cutoff,
                    )
                )
                if (
                    snapshot.raw["activation_generation"]
                    != activation.activation_generation
                    or snapshot.raw["selection_content_sha256"]
                    != selection.content_sha256
                    or snapshot.raw["station_id"] != activation_key["station_id"]
                    or snapshot.raw["lead_bucket"] != activation_key["lead_bucket"]
                    or snapshot.raw["run_cycle"] != activation_key["run_cycle"]
                    or snapshot.raw["run_cycle_scope"]
                    != activation_key["run_cycle_scope"]
                    or snapshot.raw["authority_scope"]
                    != activation_key["authority_scope"]
                    or snapshot.raw["event_date"] != forward.raw["event_date"]
                    or snapshot.raw["decision_at_utc"]
                    != forward.raw["decision_at_utc"]
                    or snapshot.raw["fresh_until_utc"]
                    != forward.raw["fresh_until_utc"]
                ):
                    raise _P0NaturalKeyLineageMismatch(
                        "P0_SNAPSHOT", "p0_snapshot_activation_scope_mismatch"
                    )
                snapshot_candidates.append(
                    {
                        "expected_key": {
                            "scope_kind": "SNAPSHOT",
                            "natural_key": {
                                **activation_key,
                                "event_date": snapshot.raw["event_date"],
                                "decision_at_utc": snapshot.raw["decision_at_utc"],
                                "probability_root_sha256": snapshot.raw[
                                    "probability_root_sha256"
                                ],
                                "activation_content_sha256": snapshot.raw[
                                    "activation_content_sha256"
                                ],
                                "forward_applicability_content_sha256": snapshot.raw[
                                    "forward_applicability_content_sha256"
                                ],
                            },
                        },
                        "source_fact_id": str(fact_id),
                    }
                )
            except (ContractError, _P0NaturalKeyLineageMismatch) as exc:
                snapshot_receipts.append(
                    _p0_unresolved_receipt(
                        scope_type="P0_SNAPSHOT",
                        as_of_utc=cutoff,
                        source_inventory_content_sha256=inventory_sha,
                        reason=getattr(exc, "reason", str(exc) or exc.__class__.__name__),
                        source_fact_id=fact_id,
                        source_content_sha256=stored_sha,
                    )
                )
        conn.rollback()
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise CanonicalExpectedScopeError("p0_expected_scope_source_query_failed") from exc
    finally:
        conn.close()

    activation_keys = _unique_p0_expected_keys(
        scope_type="P0_ACTIVATION",
        candidates=activation_candidates,
        cutoff=cutoff,
        inventory_sha=inventory_sha,
        receipts=activation_receipts,
    )
    snapshot_keys = _unique_p0_expected_keys(
        scope_type="P0_SNAPSHOT",
        candidates=snapshot_candidates,
        cutoff=cutoff,
        inventory_sha=inventory_sha,
        receipts=snapshot_receipts,
    )
    if not activation_keys and not activation_receipts:
        activation_receipts.append(
            _p0_unresolved_receipt(
                scope_type="P0_ACTIVATION",
                as_of_utc=cutoff,
                source_inventory_content_sha256=inventory_sha,
                reason="p0_activation_canonical_source_empty",
            )
        )
    if not snapshot_keys and not snapshot_receipts:
        snapshot_receipts.append(
            _p0_unresolved_receipt(
                scope_type="P0_SNAPSHOT",
                as_of_utc=cutoff,
                source_inventory_content_sha256=inventory_sha,
                reason="p0_snapshot_canonical_source_empty",
            )
        )
    activation_receipts.sort(key=lambda item: item["content_sha256"])
    snapshot_receipts.sort(key=lambda item: item["content_sha256"])
    activation_basis = build_scope_basis(
        scope_type="P0_ACTIVATION",
        as_of_utc=cutoff,
        expected_keys=activation_keys,
        source_content_sha256s=(inventory_sha,),
        unresolved_input_receipts=(
            item["content_sha256"] for item in activation_receipts
        ),
    )
    snapshot_basis = build_scope_basis(
        scope_type="P0_SNAPSHOT",
        as_of_utc=cutoff,
        expected_keys=snapshot_keys,
        source_content_sha256s=(inventory_sha,),
        unresolved_input_receipts=(
            item["content_sha256"] for item in snapshot_receipts
        ),
    )
    identity = {
        "schema": P0_EXPECTED_SCOPE_BUNDLE_SCHEMA,
        "as_of_utc": cutoff,
        "source_inventory": {
            **inventory_identity,
            "content_sha256": inventory_sha,
        },
        "activation_basis": activation_basis,
        "snapshot_basis": snapshot_basis,
        "unresolved_receipts": [*activation_receipts, *snapshot_receipts],
        "production_no_live": True,
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def compile_expected_scope_manifest(
    bases: Sequence[Mapping[str, Any]], *, as_of_utc: str
) -> dict[str, Any]:
    """Compile exactly eleven independent domain bases into one manifest."""

    cutoff = _utc_z(as_of_utc, "expected_scope_manifest_as_of_utc")
    validated = [validate_scope_basis(value) for value in bases]
    by_type = {str(value["scope_type"]): value for value in validated}
    if len(by_type) != len(validated) or set(by_type) != set(REQUIRED_SCOPE_TYPES):
        raise CanonicalExpectedScopeError("expected_scope_basis_set_not_exact")
    stations = list(_canonical_stations())
    expectations: list[dict[str, Any]] = []
    for scope_type in REQUIRED_SCOPE_TYPES:
        basis = by_type[scope_type]
        if basis["as_of_utc"] != cutoff:
            raise CanonicalExpectedScopeError("expected_scope_basis_as_of_mismatch")
        keys = list(basis["expected_keys"])
        station_counts = []
        if scope_type != "RUNTIME":
            station_counts = [
                {
                    "station_id": station_id,
                    "expected_key_count": sum(
                        item["natural_key"].get("station_id") == station_id
                        for item in keys
                    ),
                }
                for station_id in stations
            ]
        expectations.append(
            {
                "scope_type": scope_type,
                "basis_content_sha256": basis["content_sha256"],
                "expected_key_count": len(keys),
                "expected_key_set_sha256": content_sha256(keys),
                "expected_keys": keys,
                "expected_key_evidence": [
                    {
                        "expected_key": item["expected_key"],
                        "evidence_content_sha256": item[
                            "evidence_content_sha256"
                        ],
                    }
                    for item in basis["expected_key_evidence"]
                ],
                "station_expected_key_counts": station_counts,
                "unresolved_input_receipts": list(
                    basis["unresolved_input_receipts"]
                ),
            }
        )
    registry_sha = coordinate_registry_manifest()["registry_sha256"]
    identity = {
        "schema": EXPECTED_SCOPE_MANIFEST_SCHEMA,
        "as_of_utc": cutoff,
        "canonical_registry_sha256": registry_sha,
        "station_ids": stations,
        "scope_expectations": expectations,
        "production_no_live": True,
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "CanonicalExpectedScopeError",
    "EXPECTED_SCOPE_BASIS_SCHEMA",
    "P0_EXPECTED_SCOPE_BUNDLE_SCHEMA",
    "build_model_oos_scope_basis",
    "build_p0_expected_scope_bases",
    "build_scope_basis",
    "compile_expected_scope_manifest",
    "validate_scope_basis",
]
