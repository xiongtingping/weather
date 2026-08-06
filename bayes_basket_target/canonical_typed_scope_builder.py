"""Read-only source-to-canonical typed-scope envelope builders.

Each mapping is explicit and one-way.  A missing producer or source field
produces a blocked receipt; it is never renamed, guessed, or defaulted into a
canonical natural key.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from .canonical_contract import (
    CanonicalRepublisherCrossSelectionFact,
    CanonicalRepublisherNoLiveActivationFact,
    CanonicalTypedScopeEnvelope,
    TYPED_SCOPE_ENVELOPE_SCHEMA,
    TYPED_SCOPE_TYPES,
    normalize_typed_scope_natural_key,
)
from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .republisher_no_live_p0_contract import (
    CanonicalForwardApplicabilityFact,
    CanonicalP0SnapshotFact,
)


RECEIPT_SCHEMA = "bayes_basket_target_canonical_typed_scope_build_receipt_v1"
SUPPORTED_SOURCE_SCOPES = {
    "P0_ACTIVATION",
    "P0_SNAPSHOT",
    "TRUTH_OBSERVATION",
    "TRUTH_DAY",
    "TAF_P0M",
    "TRAJECTORY_P1",
    "MARKET_WS",
}
MISSING_PRODUCERS = {
    "STRATEGY_POLICY": ("strategy_policy_activation_content_sha256",),
    "STRATEGY_UTILITY": (
        "candidate_inventory_sha256",
        "baseline_inventory_sha256",
    ),
    "RUNTIME": ("runtime_release_id",),
}
P0_SOURCE_REQUIRED_FIELDS = {
    "P0_ACTIVATION": ("activation_content_sha256",),
    "P0_SNAPSHOT": (
        "p0_snapshot_content_sha256",
        "probability_envelope_content_sha256",
        "market_bucket_bridge_content_sha256",
    ),
}

P0_NATURAL_KEY_REQUIRED_FIELDS = {
    "P0_ACTIVATION": (
        "station_id",
        "lead_bucket",
        "run_cycle",
        "run_cycle_scope",
        "run_cycle_vector_sha256",
        "authority_scope",
        "model_family",
        "available_system_set_sha256",
        "independence_family_set_sha256",
    ),
    "P0_SNAPSHOT": (
        "station_id",
        "event_date",
        "decision_at_utc",
        "lead_bucket",
        "run_cycle",
        "run_cycle_scope",
        "run_cycle_vector_sha256",
        "authority_scope",
        "model_family",
        "available_system_set_sha256",
        "independence_family_set_sha256",
        "probability_root_sha256",
        "activation_content_sha256",
        "forward_applicability_content_sha256",
    ),
}


class CanonicalTypedScopeBuildError(RuntimeError):
    """Fail-closed source mapping error."""


class _P0NaturalKeyLineageMismatch(CanonicalTypedScopeBuildError):
    """A P0 identity field cannot be proven from canonical source facts."""

    def __init__(self, scope_type: str, reason: str) -> None:
        super().__init__(reason)
        self.scope_type = scope_type
        self.reason = reason


def _utc_z(value: object, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanonicalTypedScopeBuildError(f"{label}_invalid") from exc
    if parsed.tzinfo is None:
        raise CanonicalTypedScopeBuildError(f"{label}_invalid")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha(value: object, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise CanonicalTypedScopeBuildError(f"{label}_invalid")
    return text


def _json_mapping(value: object, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CanonicalTypedScopeBuildError(f"{label}_invalid") from exc
    if not isinstance(parsed, dict):
        raise CanonicalTypedScopeBuildError(f"{label}_mapping_required")
    return parsed


def _json_list(value: object, label: str) -> list[Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CanonicalTypedScopeBuildError(f"{label}_invalid") from exc
    if not isinstance(parsed, list):
        raise CanonicalTypedScopeBuildError(f"{label}_list_required")
    return parsed


def _fact(
    *,
    scope_type: str,
    scope_kind: str,
    natural_key: Mapping[str, Any],
    available_at_utc: object,
    evidence_content_sha256: object,
    version: Mapping[str, Any] | None = None,
    state: str = "CANDIDATE_ONLY",
    blockers: Iterable[str] = ("ACTIVATION_ROOT_MISSING",),
) -> dict[str, Any]:
    identity = {
        "scope_type": scope_type,
        "scope_kind": scope_kind,
        "natural_key": normalize_typed_scope_natural_key(
            scope_type=scope_type,
            scope_kind=scope_kind,
            raw=natural_key,
        ),
        "version": None if version is None else dict(version),
        "as_of_utc": _utc_z(available_at_utc, "typed_scope_available_at_utc"),
        "state": state,
        "blockers": sorted(set(blockers)),
        "evidence_content_sha256": _sha(
            evidence_content_sha256, "typed_scope_evidence_content_sha256"
        ),
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _p0_selection_key(
    conn: sqlite3.Connection,
    *,
    activation: CanonicalRepublisherNoLiveActivationFact,
    cutoff: str,
) -> tuple[dict[str, Any], CanonicalRepublisherCrossSelectionFact, dict[str, str]]:
    """Derive a P0 identity only through the activation's exact selection fact.

    The activation fact intentionally does not duplicate the selected systems,
    independent families, or component run-cycle vector.  Reconstructing those
    values from constants would silently create a second contract.  The only
    permitted source is the exact `selection_content_sha256` reference stored
    in the same append-only canonical SSOT.
    """

    rows = conn.execute(
        "SELECT fact_id,fact_json,content_sha256 FROM canonical_ssot_fact "
        "WHERE fact_type='republisher_cross_selection' AND content_sha256=? "
        "AND ingested_at_utc<=? "
        "ORDER BY fact_id",
        (activation.selection_content_sha256, cutoff),
    ).fetchall()
    if len(rows) != 1:
        raise _P0NaturalKeyLineageMismatch(
            "P0_ACTIVATION", "p0_activation_selection_reference_not_unique"
        )
    fact_id, raw_json, stored_sha = rows[0]
    try:
        selection = CanonicalRepublisherCrossSelectionFact.from_mapping(
            _json_mapping(raw_json, "p0_activation_selection_fact_json")
        )
    except ContractError as exc:
        raise _P0NaturalKeyLineageMismatch(
            "P0_ACTIVATION", "p0_activation_selection_contract_invalid"
        ) from exc
    if (
        _sha(stored_sha, "p0_activation_selection_source_content_sha256")
        != selection.content_sha256
        or selection.content_sha256 != activation.selection_content_sha256
        or selection.selection_id != activation.selection_id
        or (
            selection.station_id,
            selection.settlement_timezone,
            selection.settlement_definition,
            selection.season_bin,
            selection.lead_bucket,
            selection.run_cycle,
            selection.run_cycle_scope,
            selection.source_class,
            selection.transport_provider_id,
            selection.authority_scope,
            selection.model_family,
        )
        != (
            activation.station_id,
            activation.settlement_timezone,
            activation.settlement_definition,
            activation.season_bin,
            activation.lead_bucket,
            activation.run_cycle,
            activation.run_cycle_scope,
            activation.source_class,
            activation.transport_provider_id,
            activation.authority_scope,
            activation.model_family,
        )
    ):
        raise _P0NaturalKeyLineageMismatch(
            "P0_ACTIVATION", "p0_activation_selection_scope_mismatch"
        )

    components = tuple(selection.component_artifact_refs)
    if not components or any(
        item.run_cycle != activation.run_cycle
        or item.run_cycle_scope != activation.run_cycle_scope
        for item in components
    ):
        raise _P0NaturalKeyLineageMismatch(
            "P0_ACTIVATION", "p0_activation_component_run_cycle_mismatch"
        )
    available_system_set = sorted(
        (
            {
                "upstream_model_id": item.upstream_model_id,
                "republisher_product_id": item.republisher_product_id,
            }
            for item in components
        ),
        key=lambda item: (item["upstream_model_id"], item["republisher_product_id"]),
    )
    independence_family_set = sorted(
        {item.independence_family_id for item in components}
    )
    if len(available_system_set) != len(components) or len(independence_family_set) < 2:
        raise _P0NaturalKeyLineageMismatch(
            "P0_ACTIVATION", "p0_activation_component_identity_invalid"
        )
    run_cycle_vector = [
        {
            "upstream_model_id": item["upstream_model_id"],
            "republisher_product_id": item["republisher_product_id"],
            "run_cycle": activation.run_cycle,
            "run_cycle_scope": activation.run_cycle_scope,
        }
        for item in available_system_set
    ]
    return (
        {
            "station_id": activation.station_id,
            "lead_bucket": activation.lead_bucket,
            "run_cycle": activation.run_cycle,
            "run_cycle_scope": activation.run_cycle_scope,
            "run_cycle_vector_sha256": content_sha256(run_cycle_vector),
            "authority_scope": activation.authority_scope,
            "model_family": activation.model_family,
            "available_system_set_sha256": content_sha256(available_system_set),
            "independence_family_set_sha256": content_sha256(
                independence_family_set
            ),
        },
        selection,
        {
            "source_id": str(fact_id),
            "content_sha256": selection.content_sha256,
        },
    )


def _activation_for_p0_snapshot(
    conn: sqlite3.Connection, *, activation_content_sha256: str, cutoff: str
) -> tuple[
    CanonicalRepublisherNoLiveActivationFact,
    dict[str, Any],
    CanonicalRepublisherCrossSelectionFact,
    list[dict[str, str]],
]:
    rows = conn.execute(
        "SELECT fact_id,fact_json,content_sha256 FROM canonical_ssot_fact "
        "WHERE fact_type='republisher_no_live_activation' AND content_sha256=? "
        "AND ingested_at_utc<=? "
        "ORDER BY fact_id",
        (activation_content_sha256, cutoff),
    ).fetchall()
    if len(rows) != 1:
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_activation_reference_not_unique"
        )
    fact_id, raw_json, stored_sha = rows[0]
    try:
        activation = CanonicalRepublisherNoLiveActivationFact.from_mapping(
            _json_mapping(raw_json, "p0_snapshot_activation_fact_json")
        )
    except ContractError as exc:
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_activation_contract_invalid"
        ) from exc
    if (
        _sha(stored_sha, "p0_snapshot_activation_source_content_sha256")
        != activation.content_sha256
        or activation.content_sha256 != activation_content_sha256
        or activation.activation_generation < 2
        or activation.authority_scope != "PRODUCTION_NO_LIVE_ONLY"
        or activation.formal_live_authority
    ):
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_activation_lineage_invalid"
        )
    key, selection, selection_source = _p0_selection_key(
        conn, activation=activation, cutoff=cutoff
    )
    return activation, key, selection, [
        {"source_id": str(fact_id), "content_sha256": activation.content_sha256},
        selection_source,
    ]


def _forward_for_p0_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot: CanonicalP0SnapshotFact,
    activation: CanonicalRepublisherNoLiveActivationFact,
    key: Mapping[str, Any],
    cutoff: str,
) -> tuple[CanonicalForwardApplicabilityFact, dict[str, str]]:
    forward_sha = str(snapshot.raw["forward_applicability_content_sha256"])
    rows = conn.execute(
        "SELECT fact_id,fact_json,content_sha256 FROM canonical_ssot_fact "
        "WHERE fact_type='forward_applicability' AND content_sha256=? "
        "AND ingested_at_utc<=? ORDER BY fact_id",
        (forward_sha, cutoff),
    ).fetchall()
    if len(rows) != 1:
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_forward_reference_not_unique"
        )
    fact_id, raw_json, stored_sha = rows[0]
    try:
        forward = CanonicalForwardApplicabilityFact.from_mapping(
            _json_mapping(raw_json, "p0_snapshot_forward_fact_json")
        )
    except ContractError as exc:
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_forward_contract_invalid"
        ) from exc
    if (
        _sha(stored_sha, "p0_snapshot_forward_source_content_sha256")
        != forward.content_sha256
        or forward.content_sha256 != forward_sha
        or (
            forward.raw["station_id"],
            forward.raw["settlement_timezone"],
            forward.raw["settlement_definition"],
            forward.raw["season_bin"],
            forward.raw["lead_bucket"],
            forward.raw["run_cycle"],
            forward.raw["run_cycle_scope"],
            forward.raw["authority_scope"],
            forward.raw["activation_id"],
            forward.raw["activation_generation"],
            forward.raw["activation_content_sha256"],
            forward.raw["selection_content_sha256"],
        )
        != (
            snapshot.raw["station_id"],
            snapshot.raw["settlement_timezone"],
            snapshot.raw["settlement_definition"],
            snapshot.raw["season_bin"],
            snapshot.raw["lead_bucket"],
            snapshot.raw["run_cycle"],
            snapshot.raw["run_cycle_scope"],
            snapshot.raw["authority_scope"],
            activation.activation_id,
            activation.activation_generation,
            activation.content_sha256,
            activation.selection_content_sha256,
        )
        or forward.raw["station_id"] != key["station_id"]
        or forward.raw["lead_bucket"] != key["lead_bucket"]
        or forward.raw["run_cycle"] != key["run_cycle"]
        or forward.raw["run_cycle_scope"] != key["run_cycle_scope"]
        or forward.raw["authority_scope"] != key["authority_scope"]
    ):
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_forward_scope_mismatch"
        )
    forward_systems = sorted(
        (
            {
                "upstream_model_id": item["upstream_model_id"],
                "republisher_product_id": item["republisher_product_id"],
            }
            for item in forward.raw["component_receipts"]
        ),
        key=lambda item: (item["upstream_model_id"], item["republisher_product_id"]),
    )
    forward_families = sorted(
        {item["independence_family_id"] for item in forward.raw["component_receipts"]}
    )
    forward_vector = [
        {
            "upstream_model_id": item["upstream_model_id"],
            "republisher_product_id": item["republisher_product_id"],
            "run_cycle": forward.raw["run_cycle"],
            "run_cycle_scope": forward.raw["run_cycle_scope"],
        }
        for item in forward_systems
    ]
    if (
        content_sha256(forward_systems) != key["available_system_set_sha256"]
        or content_sha256(forward_families) != key["independence_family_set_sha256"]
        or content_sha256(forward_vector) != key["run_cycle_vector_sha256"]
    ):
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_forward_component_set_mismatch"
        )
    return forward, {"source_id": str(fact_id), "content_sha256": forward.content_sha256}


def _bridge_for_p0_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot: CanonicalP0SnapshotFact,
    cutoff: str,
) -> tuple[Any, dict[str, str]]:
    """Resolve the exact persisted official bucket bridge for one P0 fact."""

    # Local import avoids the production_io -> release -> typed-builder cycle.
    from .market_bucket_bridge import CanonicalMarketBucketBridgeFact

    bridge_sha = str(snapshot.raw["market_bucket_bridge_sha256"])
    rows = conn.execute(
        "SELECT fact_id,fact_json,content_sha256 FROM canonical_ssot_fact "
        "WHERE fact_type='market_bucket_bridge' AND content_sha256=? "
        "AND ingested_at_utc<=? ORDER BY fact_id",
        (bridge_sha, cutoff),
    ).fetchall()
    if len(rows) != 1:
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_market_bridge_reference_not_unique"
        )
    fact_id, raw_json, stored_sha = rows[0]
    try:
        bridge = CanonicalMarketBucketBridgeFact.from_mapping(
            _json_mapping(raw_json, "p0_snapshot_market_bridge_fact_json")
        )
    except ContractError as exc:
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_market_bridge_contract_invalid"
        ) from exc
    intervals = [
        {
            "bucket_id": item["outcome"],
            "ordinal": item["ordinal"],
            "lower_c": item["lower_c"],
            "upper_c": item["upper_c"],
        }
        for item in bridge.raw["bucket_contracts"]
    ]
    if (
        _sha(stored_sha, "p0_snapshot_market_bridge_source_content_sha256")
        != bridge.content_sha256
        or bridge.content_sha256 != bridge_sha
        or bridge.raw["station_id"] != snapshot.raw["station_id"]
        or bridge.raw["event_date"] != snapshot.raw["event_date"]
        or bridge.raw["settlement_timezone"] != snapshot.raw["settlement_timezone"]
        or bridge.raw["settlement_definition"]
        != snapshot.raw["settlement_definition"]
        or intervals != snapshot.raw["bucket_intervals"]
        or content_sha256(intervals) != snapshot.raw["bucket_contract_sha256"]
    ):
        raise _P0NaturalKeyLineageMismatch(
            "P0_SNAPSHOT", "p0_snapshot_market_bridge_binding_mismatch"
        )
    return bridge, {"source_id": str(fact_id), "content_sha256": bridge.content_sha256}


def _p0_activation_records(
    conn: sqlite3.Connection, cutoff: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT fact_id,fact_json,content_sha256,ingested_at_utc "
        "FROM canonical_ssot_fact WHERE fact_type='republisher_no_live_activation' "
        "AND json_extract(fact_json,'$.activation_generation')>=2 "
        "AND json_extract(fact_json,'$.activated_at_utc')<=? "
        "AND ingested_at_utc<=? ORDER BY scope_key,fact_id",
        (cutoff, cutoff),
    ).fetchall()
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for fact_id, fact_json, stored_sha, _ in rows:
        raw = _json_mapping(fact_json, "p0_activation_fact_json")
        fact = CanonicalRepublisherNoLiveActivationFact.from_mapping(raw)
        evidence = _sha(stored_sha, "p0_activation_source_content_sha256")
        if (
            evidence != fact.content_sha256
            or fact.activation_generation < 2
            or fact.temperature_distribution_artifact_content_sha256 is None
            or fact.authority_scope != "PRODUCTION_NO_LIVE_ONLY"
            or fact.formal_live_authority
        ):
            raise _P0NaturalKeyLineageMismatch(
                "P0_ACTIVATION", "p0_activation_lineage_invalid"
            )
        natural_key, selection, selection_source = _p0_selection_key(
            conn, activation=fact, cutoff=cutoff
        )
        evidence = content_sha256(
            {
                "activation_content_sha256": fact.content_sha256,
                "selection_content_sha256": selection.content_sha256,
            }
        )
        records.append(
            _fact(
                scope_type="P0_ACTIVATION",
                scope_kind="ACTIVATION",
                natural_key=natural_key,
                available_at_utc=fact.activated_at_utc,
                evidence_content_sha256=evidence,
                version={"activation_generation": fact.activation_generation},
                state="ACTIVATION_READY",
                blockers=(),
            )
        )
        sources.extend(
            (
                {"source_id": str(fact_id), "content_sha256": fact.content_sha256},
                selection_source,
            )
        )
    return records, sources


def _p0_snapshot_records(
    conn: sqlite3.Connection, cutoff: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT fact_id,fact_json,content_sha256,ingested_at_utc "
        "FROM canonical_ssot_fact WHERE fact_type='p0_snapshot' "
        "AND json_extract(fact_json,'$.decision_at_utc')<=? "
        "AND ingested_at_utc<=? ORDER BY scope_key,fact_id",
        (cutoff, cutoff),
    ).fetchall()
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for fact_id, fact_json, stored_sha, _ in rows:
        snapshot = CanonicalP0SnapshotFact.from_mapping(
            _json_mapping(fact_json, "p0_snapshot_fact_json")
        )
        if _sha(stored_sha, "p0_snapshot_source_content_sha256") != snapshot.content_sha256:
            raise _P0NaturalKeyLineageMismatch(
                "P0_SNAPSHOT", "p0_snapshot_fact_hash_mismatch"
            )
        activation, activation_key, selection, activation_sources = (
            _activation_for_p0_snapshot(
                conn,
                activation_content_sha256=snapshot.raw["activation_content_sha256"],
                cutoff=cutoff,
            )
        )
        forward, forward_source = _forward_for_p0_snapshot(
            conn,
            snapshot=snapshot,
            activation=activation,
            key=activation_key,
            cutoff=cutoff,
        )
        _bridge, bridge_source = _bridge_for_p0_snapshot(
            conn, snapshot=snapshot, cutoff=cutoff
        )
        if (
            snapshot.raw["activation_generation"] != activation.activation_generation
            or snapshot.raw["selection_content_sha256"]
            != selection.content_sha256
            or snapshot.raw["station_id"] != activation_key["station_id"]
            or snapshot.raw["lead_bucket"] != activation_key["lead_bucket"]
            or snapshot.raw["run_cycle"] != activation_key["run_cycle"]
            or snapshot.raw["run_cycle_scope"] != activation_key["run_cycle_scope"]
            or snapshot.raw["authority_scope"] != activation_key["authority_scope"]
            or snapshot.raw["event_date"] != forward.raw["event_date"]
        ):
            raise _P0NaturalKeyLineageMismatch(
                "P0_SNAPSHOT", "p0_snapshot_activation_scope_mismatch"
            )
        envelope_rows = conn.execute(
            "SELECT stage,checkpoint_id,station_id,event_date,settlement_timezone,"
            "decision_at_utc,fresh_until_utc,bucket_ordinals_json,probabilities_json,"
            "posterior_version,posterior_sha256,bridge_sha256,lineage_json,content_sha256,created_at "
            "FROM basket_probability_envelope WHERE envelope_id=? AND state='COMMITTED'",
            (snapshot.raw["snapshot_id"],),
        ).fetchall()
        if len(envelope_rows) != 1:
            raise _P0NaturalKeyLineageMismatch(
                "P0_SNAPSHOT", "p0_snapshot_probability_envelope_missing"
            )
        row = envelope_rows[0]
        envelope = ProbabilityEnvelope.from_mapping(
            {
                "stage": row[0],
                "checkpoint_id": row[1],
                "station_id": row[2],
                "event_date": row[3],
                "settlement_timezone": row[4],
                "decision_at_utc": row[5],
                "fresh_until_utc": row[6],
                "bucket_ordinals": _json_mapping(row[7], "p0_bucket_ordinals_json"),
                "probabilities": _json_mapping(row[8], "p0_probabilities_json"),
                "posterior_version": row[9],
                "posterior_sha256": row[10],
                "bridge_sha256": row[11],
                "lineage": _json_mapping(row[12], "p0_lineage_json"),
            }
        )
        envelope_sha = _sha(row[13], "p0_probability_envelope_content_sha256")
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
            != snapshot.raw["forward_applicability_content_sha256"]
            or lineage.get("market_bucket_bridge_content_sha256")
            != snapshot.raw["market_bucket_bridge_sha256"]
            or lineage.get("release_state") != "MATERIALIZED_UNPUBLISHED"
            or str(row[14]) > cutoff
        ):
            raise _P0NaturalKeyLineageMismatch(
                "P0_SNAPSHOT", "p0_snapshot_probability_binding_mismatch"
            )
        evidence = content_sha256(
            {
                "p0_snapshot_content_sha256": snapshot.content_sha256,
                "probability_envelope_content_sha256": envelope_sha,
                "market_bucket_bridge_content_sha256": snapshot.raw[
                    "market_bucket_bridge_sha256"
                ],
                "activation_content_sha256": activation.content_sha256,
                "selection_content_sha256": selection.content_sha256,
                "forward_applicability_content_sha256": forward.content_sha256,
            }
        )
        fresh_at_release = (
            _utc_z(envelope.decision_at_utc, "p0_decision_at_utc")
            <= cutoff
            < _utc_z(envelope.fresh_until_utc, "p0_fresh_until_utc")
        )
        records.append(
            _fact(
                scope_type="P0_SNAPSHOT",
                scope_kind="SNAPSHOT",
                natural_key={
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
                available_at_utc=row[14],
                evidence_content_sha256=evidence,
                version={"activation_generation": activation.activation_generation},
                state="ACTIVATION_READY" if fresh_at_release else "BLOCKED",
                blockers=() if fresh_at_release else ("DATA_MISSING",),
            )
        )
        sources.extend(
            (
                {"source_id": str(fact_id), "content_sha256": snapshot.content_sha256},
                {"source_id": str(row[1]), "content_sha256": envelope_sha},
                *activation_sources,
                forward_source,
                bridge_source,
            )
        )
    return records, sources


def _truth_observation_records(
    conn: sqlite3.Connection, cutoff: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    query = """
        SELECT fact_id,fact_json,content_sha256
        FROM canonical_ssot_fact
        WHERE fact_type='truth_observation_revision'
          AND ingested_at_utc<=?
          AND json_extract(fact_json,'$.source_lane')='AWC'
          AND json_extract(fact_json,'$.authority_scope')='PRODUCTION_NO_LIVE_ONLY'
          AND json_extract(fact_json,'$.canonical_committed_at_utc')<=?
        ORDER BY json_extract(fact_json,'$.station_id'),
                 json_extract(fact_json,'$.event_date'),
                 json_extract(fact_json,'$.sequence'),fact_id
    """
    for fact_id, fact_json, stored_sha in conn.execute(query, (cutoff, cutoff)):
        raw = json.loads(str(fact_json))
        evidence = _sha(stored_sha, "truth_observation_source_content_sha256")
        records.append(
            _fact(
                scope_type="TRUTH_OBSERVATION",
                scope_kind="REVISION",
                natural_key={
                    "station_id": raw["station_id"],
                    "event_date": raw["event_date"],
                    "settlement_timezone": raw["settlement_timezone"],
                    "observation_id": raw["observation_id"],
                    "revision_no": int(raw["revision_no"]),
                    "observed_at_utc": raw["observed_at_utc"],
                    "canonical_committed_at_utc": raw[
                        "canonical_committed_at_utc"
                    ],
                    "source_lane": "AWC",
                },
                available_at_utc=raw["canonical_committed_at_utc"],
                evidence_content_sha256=evidence,
                state=(
                    "DATA_READY"
                    if raw["qc_state"] == "ACCEPTED"
                    and raw["halt_state"] == "CLEAR"
                    else "BLOCKED"
                ),
                blockers=(
                    ()
                    if raw["qc_state"] == "ACCEPTED"
                    and raw["halt_state"] == "CLEAR"
                    else ("TRUTH_REVISION_STALE",)
                ),
            )
        )
        sources.append({"source_id": str(fact_id), "content_sha256": evidence})
    return records, sources


def _truth_records(conn: sqlite3.Connection, cutoff: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    query = """
        SELECT fact_id,
               json_extract(fact_json,'$.station_id'),
               json_extract(fact_json,'$.event_date'),
               json_extract(fact_json,'$.settlement_timezone'),
               json_extract(fact_json,'$.bounds_version'),
               json_extract(fact_json,'$.canonical_committed_at_utc'),
               content_sha256
        FROM canonical_ssot_fact
        WHERE fact_type='truth_observation_day_bounds'
          AND ingested_at_utc<=?
          AND json_extract(fact_json,'$.source_lane')='AWC'
          AND json_extract(fact_json,'$.authority_scope')='PRODUCTION_NO_LIVE_ONLY'
          AND json_extract(fact_json,'$.closed_day')=1
          AND json_extract(fact_json,'$.canonical_committed_at_utc')<=?
        ORDER BY json_extract(fact_json,'$.station_id'),
                 json_extract(fact_json,'$.event_date'),
                 json_extract(fact_json,'$.bounds_version'),fact_id
    """
    for row in conn.execute(query, (cutoff, cutoff)):
        evidence = _sha(row[6], "truth_day_source_content_sha256")
        records.append(
            _fact(
                scope_type="TRUTH_DAY",
                scope_kind="DAY_BOUNDS",
                natural_key={
                    "station_id": str(row[1]),
                    "event_date": str(row[2]),
                    "settlement_timezone": str(row[3]),
                    "bounds_version": int(row[4]),
                },
                available_at_utc=row[5],
                evidence_content_sha256=evidence,
            )
        )
        sources.append({"source_id": str(row[0]), "content_sha256": evidence})
    return records, sources


def _probability_records(
    conn: sqlite3.Connection,
    cutoff: str,
    *,
    stage: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lineage_filter = (
        " AND json_extract(lineage_json,'$.source_lane')='AWC'"
        if stage == "P1"
        else ""
    )
    rows = conn.execute(
        f"""
        WITH ranked AS (
          SELECT envelope_id,station_id,event_date,decision_at_utc,
                 posterior_sha256,lineage_json,content_sha256,
                 ROW_NUMBER() OVER (
                   PARTITION BY station_id,event_date
                   ORDER BY decision_at_utc DESC,content_sha256 DESC
                 ) AS rank_no
          FROM basket_probability_envelope
          WHERE stage=? AND state='COMMITTED'
            AND decision_at_utc<=? AND created_at<=?
            {lineage_filter}
        )
        SELECT envelope_id,station_id,event_date,decision_at_utc,
               posterior_sha256,lineage_json,content_sha256
        FROM ranked WHERE rank_no=1
        ORDER BY station_id,event_date,decision_at_utc,envelope_id
        """,
        (stage, cutoff, cutoff),
    )
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for row in rows:
        lineage = _json_mapping(row[5], f"{stage.lower()}_lineage_json")
        evidence = _sha(row[6], f"{stage.lower()}_source_content_sha256")
        if stage == "P0-M":
            p0_root = _sha(
                lineage.get("p0_root_content_sha256"),
                "p0m_p0_root_sha256",
            )
            taf_revision = str(lineage.get("forecast_revision_id") or "").strip()
            if not taf_revision:
                raise CanonicalTypedScopeBuildError("p0m_taf_revision_id_missing")
            scope_type = "TAF_P0M"
            natural_key = {
                "station_id": str(row[1]),
                "event_date": str(row[2]),
                "decision_at_utc": _utc_z(row[3], "p0m_decision_at_utc"),
                "p0_root_sha256": p0_root,
                "taf_revision_id": taf_revision,
            }
        else:
            if str(lineage.get("source_lane") or "") != "AWC":
                raise CanonicalTypedScopeBuildError("p1_source_lane_not_awc")
            observation_id = str(
                lineage.get("canonical_observation_event_id") or ""
            ).strip()
            if not observation_id:
                raise CanonicalTypedScopeBuildError("p1_observation_id_missing")
            scope_type = "TRAJECTORY_P1"
            natural_key = {
                "station_id": str(row[1]),
                "event_date": str(row[2]),
                "decision_at_utc": _utc_z(row[3], "p1_decision_at_utc"),
                "probability_root_sha256": _sha(
                    row[4], "p1_probability_root_sha256"
                ),
                "observation_id": observation_id,
            }
        records.append(
            _fact(
                scope_type=scope_type,
                scope_kind="EVENT",
                natural_key=natural_key,
                available_at_utc=row[3],
                evidence_content_sha256=evidence,
            )
        )
        sources.append({"source_id": str(row[0]), "content_sha256": evidence})
    return records, sources


def _market_records(conn: sqlite3.Connection, cutoff: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        WITH ranked AS (
          SELECT snapshot_id,market_id,station_id,event_date,sequence,
                 observed_at,buckets_json,content_sha256,
                 ROW_NUMBER() OVER (
                   PARTITION BY station_id,event_date,market_id
                   ORDER BY sequence DESC,observed_at DESC,content_sha256 DESC
                 ) AS rank_no
          FROM basket_official_ws_snapshot
          WHERE quote_source='OFFICIAL_CLOB_WS' AND state='COMMITTED'
            AND observed_at<=? AND created_at<=?
        )
        SELECT snapshot_id,market_id,station_id,event_date,sequence,
               observed_at,buckets_json,content_sha256
        FROM ranked WHERE rank_no=1
        ORDER BY station_id,event_date,market_id,sequence,snapshot_id
        """,
        (cutoff, cutoff),
    )
    records: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for row in rows:
        evidence = _sha(row[7], "market_ws_source_content_sha256")
        buckets = _json_list(row[6], "market_ws_buckets_json")
        token_ids: set[str] = set()
        for bucket in buckets:
            if not isinstance(bucket, Mapping):
                raise CanonicalTypedScopeBuildError(
                    "market_ws_bucket_mapping_required"
                )
            if str(bucket.get("quote_source") or "") != "OFFICIAL_CLOB_WS":
                raise CanonicalTypedScopeBuildError(
                    "market_ws_bucket_source_not_official_ws"
                )
            token_id = str(bucket.get("token_id") or "").strip()
            if not token_id or token_id in token_ids:
                raise CanonicalTypedScopeBuildError(
                    "market_ws_yes_token_id_invalid"
                )
            token_ids.add(token_id)
            records.append(
                _fact(
                    scope_type="MARKET_WS",
                    scope_kind="SNAPSHOT",
                    natural_key={
                        "station_id": str(row[2]),
                        "event_date": str(row[3]),
                        "market_id": str(row[1]),
                        "yes_token_id": token_id,
                        "ws_sequence": int(row[4]),
                        "snapshot_at_utc": _utc_z(
                            row[5], "market_ws_snapshot_at_utc"
                        ),
                    },
                    available_at_utc=row[5],
                    evidence_content_sha256=evidence,
                )
            )
        sources.append({"source_id": str(row[0]), "content_sha256": evidence})
    return records, sources


def _ordered_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda item: (
            str(item["scope_kind"]),
            json.dumps(
                item["natural_key"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )


def _p0_lineage_blocked_receipt(
    *,
    scope: str,
    cutoff: str,
    source: Path,
    table: str,
    data_version: int,
    schema_version: int,
    reason: str,
    source_row_count: int,
) -> dict[str, Any]:
    """Build terminal evidence without inventing an unprovable P0 key."""

    identity = {
        "schema": RECEIPT_SCHEMA,
        "scope_type": scope,
        "as_of_utc": cutoff,
        "state": "BLOCKED",
        "blockers": ["LINEAGE_MISMATCH"],
        "missing_producer_fields": [],
        "unproven_natural_key_fields": list(P0_NATURAL_KEY_REQUIRED_FIELDS[scope]),
        "failure_code": reason,
        "source_table": table,
        "source_row_count": source_row_count,
        "record_count": 0,
        "source_selection_sha256": content_sha256([]),
        "envelope_content_sha256": "",
        "database": str(source),
        "database_data_version": data_version,
        "database_schema_version": schema_version,
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_canonical_typed_scope_envelope(
    db_path: Path | str,
    *,
    scope_type: str,
    as_of_utc: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build one scope envelope or an explicit missing-producer receipt."""

    scope = str(scope_type).strip().upper()
    if scope not in TYPED_SCOPE_TYPES:
        raise CanonicalTypedScopeBuildError("typed_scope_builder_scope_invalid")
    cutoff = _utc_z(as_of_utc, "typed_scope_builder_as_of_utc")
    source = Path(db_path).expanduser().resolve(strict=True)
    if scope in MISSING_PRODUCERS:
        identity = {
            "schema": RECEIPT_SCHEMA,
            "scope_type": scope,
            "as_of_utc": cutoff,
            "state": "BLOCKED",
            "blockers": ["DATA_MISSING"],
            "missing_producer_fields": list(MISSING_PRODUCERS[scope]),
            "source_table": "",
            "source_row_count": 0,
            "record_count": 0,
            "source_selection_sha256": content_sha256([]),
            "envelope_content_sha256": "",
            "writes_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "production_no_live": True,
        }
        return None, {**identity, "content_sha256": content_sha256(identity)}

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    table = ""
    data_version = 0
    schema_version = 0
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if scope == "P0_ACTIVATION":
            records, sources = _p0_activation_records(conn, cutoff)
            table = "canonical_ssot_fact"
        elif scope == "P0_SNAPSHOT":
            records, sources = _p0_snapshot_records(conn, cutoff)
            table = "canonical_ssot_fact+basket_probability_envelope"
        elif scope == "TRUTH_OBSERVATION":
            records, sources = _truth_observation_records(conn, cutoff)
            table = "canonical_ssot_fact"
        elif scope == "TRUTH_DAY":
            records, sources = _truth_records(conn, cutoff)
            table = "canonical_ssot_fact"
        elif scope == "TAF_P0M":
            records, sources = _probability_records(conn, cutoff, stage="P0-M")
            table = "basket_probability_envelope"
        elif scope == "TRAJECTORY_P1":
            records, sources = _probability_records(conn, cutoff, stage="P1")
            table = "basket_probability_envelope"
        elif scope == "MARKET_WS":
            records, sources = _market_records(conn, cutoff)
            table = "basket_official_ws_snapshot"
        else:  # pragma: no cover - guarded by the constant sets above
            raise CanonicalTypedScopeBuildError("typed_scope_builder_unreachable")
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.rollback()
    except (CanonicalTypedScopeBuildError, ContractError) as exc:
        if scope not in P0_SOURCE_REQUIRED_FIELDS:
            raise
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        source_row_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type=?",
                (
                    "republisher_no_live_activation"
                    if scope == "P0_ACTIVATION"
                    else "p0_snapshot",
                ),
            ).fetchone()[0]
        )
        if conn.in_transaction:
            conn.rollback()
        return None, _p0_lineage_blocked_receipt(
            scope=scope,
            cutoff=cutoff,
            source=source,
            table=table,
            data_version=data_version,
            schema_version=schema_version,
            reason=getattr(exc, "reason", str(exc) or exc.__class__.__name__),
            source_row_count=source_row_count,
        )
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise CanonicalTypedScopeBuildError(
            f"typed_scope_builder_source_query_failed:{scope}"
        ) from exc
    finally:
        conn.close()

    if not records and scope in P0_SOURCE_REQUIRED_FIELDS:
        identity = {
            "schema": RECEIPT_SCHEMA,
            "scope_type": scope,
            "as_of_utc": cutoff,
            "state": "BLOCKED",
            "blockers": ["DATA_MISSING"],
            "missing_producer_fields": list(P0_SOURCE_REQUIRED_FIELDS[scope]),
            "source_table": table,
            "source_row_count": 0,
            "record_count": 0,
            "source_selection_sha256": content_sha256([]),
            "envelope_content_sha256": "",
            "database": str(source),
            "database_data_version": data_version,
            "database_schema_version": schema_version,
            "writes_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "production_no_live": True,
        }
        return None, {**identity, "content_sha256": content_sha256(identity)}
    if not records:
        raise CanonicalTypedScopeBuildError(
            f"typed_scope_builder_source_rows_missing:{scope}"
        )
    ordered = _ordered_records(records)
    envelope_identity = {
        "schema": TYPED_SCOPE_ENVELOPE_SCHEMA,
        "as_of_utc": cutoff,
        "builder_id": f"canonical.{scope.lower()}.source_projection.v1",
        "scope_type": scope,
        "records": ordered,
        "production_no_live": True,
        "future_data_counts_as_oos": False,
    }
    envelope = {
        **envelope_identity,
        "content_sha256": content_sha256(envelope_identity),
    }
    try:
        validated = CanonicalTypedScopeEnvelope.from_mapping(envelope)
    except ContractError as exc:
        raise CanonicalTypedScopeBuildError(
            f"typed_scope_builder_contract_invalid:{scope}"
        ) from exc
    canonical_envelope = validated.to_mapping()
    source_selection = sorted(
        sources,
        key=lambda item: (item["source_id"], item["content_sha256"]),
    )
    all_activation_ready = all(
        item["state"] == "ACTIVATION_READY" and item["blockers"] == []
        for item in canonical_envelope["records"]
    )
    record_blockers = sorted(
        {
            str(blocker)
            for item in canonical_envelope["records"]
            for blocker in item["blockers"]
        }
    )
    any_blocked = any(
        item["state"] == "BLOCKED" for item in canonical_envelope["records"]
    )
    receipt_identity = {
        "schema": RECEIPT_SCHEMA,
        "scope_type": scope,
        "as_of_utc": cutoff,
        "state": (
            "BLOCKED"
            if any_blocked
            else "ACTIVATION_READY" if all_activation_ready else "CANDIDATE_ONLY"
        ),
        "blockers": (
            record_blockers
            if any_blocked
            else [] if all_activation_ready else ["ACTIVATION_ROOT_MISSING"]
        ),
        "missing_producer_fields": [],
        "source_table": table,
        "source_row_count": len(source_selection),
        "record_count": len(canonical_envelope["records"]),
        "source_selection_sha256": content_sha256(source_selection),
        "envelope_content_sha256": canonical_envelope["content_sha256"],
        "database": str(source),
        "database_data_version": data_version,
        "database_schema_version": schema_version,
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
    }
    return canonical_envelope, {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }


__all__ = [
    "CanonicalTypedScopeBuildError",
    "RECEIPT_SCHEMA",
    "build_canonical_typed_scope_envelope",
]
