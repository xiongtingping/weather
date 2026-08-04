"""PR04 immutable no-live activation release and single current root.

This module is the only bridge from append-only canonical facts to a current
activation view.  It does not infer a head from the greatest generation.  A
business scope with more than one activation remains blocked until a separate,
content-addressed head authority exists.  Release publication writes immutable
leaf objects first and atomically replaces only the small current-root pointer.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Mapping, Sequence

from .canonical_contract import (
    CanonicalActivationFact,
    CanonicalActivationSupersessionFact,
    CanonicalContract,
    CanonicalModelFact,
    CanonicalTypedScopeEnvelope,
    CanonicalTypedScopeFact,
    CanonicalTruthFact,
    TYPED_SCOPE_TYPES,
)
from .contracts import ContractError, content_sha256
from .resource_bounds import (
    MAX_JSON_INPUT_BYTES,
    bounded_json_bytes,
    load_bounded_json,
)


LEDGER_SCHEMA = "bayes_basket_target_canonical_gap_ledger_v2"
ACTIVATION_INDEX_SCHEMA = "bayes_basket_target_canonical_activation_current_index_v2"
BINDING_SCHEMA = "bayes_basket_target_activation_ledger_binding_v1"
RELEASE_MANIFEST_SCHEMA = "bayes_basket_target_activation_release_manifest_v1"
ROOT_POINTER_SCHEMA = "bayes_basket_target_current_root_pointer_v2"
INNER_SCHEMA = "bayes_basket_target_oos_atomic_snapshot_manifest_v1"
TRUTH_GAP_SCHEMA = "bayes_basket_target_historical_awc_truth_contract_build_v1"
NO_LIVE_AUTHORITY = "PRODUCTION_NO_LIVE_ONLY"

CURRENT = "CURRENT"
BLOCKED = "BLOCKED"
OOS_READY = "OOS_READY"
ACTIVATION_READY = "ACTIVATION_READY"

ACTIVATION_CURRENT_AMBIGUOUS = "ACTIVATION_CURRENT_AMBIGUOUS"
ACTIVATION_CONTRACT_BINDING_MISSING = "ACTIVATION_CONTRACT_BINDING_MISSING"
ACTIVATION_MODEL_SET_MISMATCH = "ACTIVATION_MODEL_SET_MISMATCH"
ACTIVATION_SAMPLE_COHORT_MISMATCH = "ACTIVATION_SAMPLE_COHORT_MISMATCH"
ACTIVATION_ROOT_MISSING = "ACTIVATION_ROOT_MISSING"
INNER_OOS_SNAPSHOT_INVALID = "INNER_OOS_SNAPSHOT_INVALID"
INNER_OOS_SNAPSHOT_AS_OF_MISMATCH = "INNER_OOS_SNAPSHOT_AS_OF_MISMATCH"
LINEAGE_MISMATCH = "LINEAGE_MISMATCH"
DATA_MISSING = "DATA_MISSING"
OOS_SAMPLE_SHORT = "OOS_SAMPLE_SHORT"
TYPED_SCOPE_BLOCKED = "TYPED_SCOPE_BLOCKED"

MODEL_OOS_SCOPE = "MODEL_OOS"
REQUIRED_SCOPE_TYPES = (
    MODEL_OOS_SCOPE,
    "TRUTH_DAY",
    "TAF_P0M",
    "TRAJECTORY_P1",
    "STRATEGY_UTILITY",
    "MARKET_WS",
    "RUNTIME",
)

DATA_READY = "DATA_READY"
CANDIDATE_ONLY = "CANDIDATE_ONLY"

_HEX = frozenset("0123456789abcdef")


class CanonicalActivationReleaseError(ContractError):
    """Raised when a current root cannot be materialized without guessing."""


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise CanonicalActivationReleaseError(f"{field}_type_invalid")
    result = value.strip().lower()
    if len(result) != 64 or any(character not in _HEX for character in result):
        raise CanonicalActivationReleaseError(f"{field}_invalid")
    return result


def _utc_z(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or not value.strip().endswith("Z"):
        raise CanonicalActivationReleaseError(f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.strip()[:-1] + "+00:00")
    except ValueError as exc:
        raise CanonicalActivationReleaseError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CanonicalActivationReleaseError(f"{field}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _hashed(identity: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(identity)
    result["content_sha256"] = content_sha256(result)
    return result


def _scope(fact: CanonicalActivationFact | CanonicalModelFact) -> tuple[str, str, str, str]:
    return (fact.station_id, fact.model_family, fact.lead_bucket, fact.run_cycle)


def _scope_mapping(scope: tuple[str, str, str, str]) -> dict[str, str]:
    return dict(zip(("station_id", "model_family", "lead_bucket", "run_cycle"), scope))


def _stable_json(path: Path | str) -> tuple[Path, bytes, Mapping[str, Any]]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise CanonicalActivationReleaseError("activation_release_input_symlink_forbidden")
    try:
        source = candidate.resolve(strict=True)
        before = source.stat()
    except OSError as exc:
        raise CanonicalActivationReleaseError("activation_release_input_missing") from exc
    if not source.is_file() or before.st_size > MAX_JSON_INPUT_BYTES:
        raise CanonicalActivationReleaseError("activation_release_input_invalid")
    value = load_bounded_json(source)
    raw = source.read_bytes()
    after = source.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise CanonicalActivationReleaseError("activation_release_input_changed_during_read")
    if not isinstance(value, Mapping):
        raise CanonicalActivationReleaseError("activation_release_input_mapping_required")
    return source, raw, value


def load_inner_snapshot(path: Path | str) -> dict[str, Any]:
    source, raw, value = _stable_json(path)
    supplied = _sha(value.get("content_sha256"), "inner_oos_snapshot_content_sha256")
    identity = {key: child for key, child in value.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise CanonicalActivationReleaseError("inner_oos_snapshot_content_sha256_mismatch")
    stations = value.get("canonical_station_ids")
    exact_stations = (
        isinstance(stations, list)
        and len(stations) == 45
        and len(set(stations)) == 45
        and stations == sorted(stations)
        and all(isinstance(item, str) and item and item == item.upper() for item in stations)
    )
    raw_blockers = value.get("blockers", [])
    allowed_blockers = {
        f"SNAPSHOT_SCOPE_MISMATCH:{source}"
        for source in ("p0_inbox", "taf_audit", "native_linkage", "native_repair")
    }
    if (
        not isinstance(raw_blockers, list)
        or any(not isinstance(item, str) or item not in allowed_blockers for item in raw_blockers)
        or raw_blockers != sorted(set(raw_blockers))
    ):
        raise CanonicalActivationReleaseError("inner_oos_snapshot_blockers_invalid")
    state = value.get("state")
    if state is not None and state not in {"FROZEN_READ_ONLY", BLOCKED}:
        raise CanonicalActivationReleaseError("inner_oos_snapshot_state_invalid")
    if state == BLOCKED and not raw_blockers:
        raise CanonicalActivationReleaseError("inner_oos_snapshot_blocked_without_blocker")
    if raw_blockers and state != BLOCKED:
        raise CanonicalActivationReleaseError("inner_oos_snapshot_blocker_state_mismatch")
    boundary_valid = all(
        (
            value.get("schema") == INNER_SCHEMA,
            value.get("canonical_station_count") == 45,
            exact_stations,
            value.get("read_only") is True,
            value.get("production_no_live") is True,
            value.get("network_fetches") == 0,
            value.get("writes_target_database") is False,
            value.get("writes_production_database") is False,
            value.get("calls_order_gateway") is False,
            value.get("orders_submitted") == 0,
        )
    )
    return {
        "path": str(source),
        "schema": value.get("schema"),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": supplied,
        "as_of_utc": _utc_z(value.get("as_of_utc"), "inner_oos_snapshot_as_of_utc"),
        "canonical_station_ids": list(stations) if exact_stations else [],
        "boundary_valid": boundary_valid,
        "state": state or (BLOCKED if raw_blockers else "FROZEN_READ_ONLY"),
        "blockers": list(raw_blockers),
    }


def _extend_inner_snapshot_blockers(
    blockers: list[str], inner_snapshot: Mapping[str, Any],
) -> None:
    for blocker in inner_snapshot.get("blockers", []):
        if str(blocker).startswith("SNAPSHOT_SCOPE_MISMATCH:"):
            blockers.append(f"INNER_OOS_{blocker}")


def load_truth_gap_evidence(
    path: Path | str,
    *,
    release_as_of_utc: str,
    canonical_station_ids: Sequence[str],
    contract_evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Load one exact-45 historical AWC truth-gap receipt.

    The receipt is evidence only: it may block model records and builders but
    can never create an activation or make a record activation-ready.
    """

    source, raw, value = _stable_json(path)
    supplied = _sha(value.get("content_sha256"), "truth_gap_evidence_content_sha256")
    identity = {key: child for key, child in value.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise CanonicalActivationReleaseError("truth_gap_evidence_content_sha256_mismatch")
    if value.get("schema") != TRUTH_GAP_SCHEMA:
        raise CanonicalActivationReleaseError("truth_gap_evidence_schema_invalid")
    cutoff = _utc_z(release_as_of_utc, "activation_release_as_of_utc")
    evidence_as_of = _utc_z(value.get("as_of_utc"), "truth_gap_evidence_as_of_utc")
    if evidence_as_of > cutoff:
        raise CanonicalActivationReleaseError("truth_gap_evidence_after_release_as_of")
    boundary = (
        value.get("production_no_live") is True
        and value.get("writes_target_database") is False
        and value.get("writes_production_database") is False
        and value.get("calls_order_gateway") is False
        and value.get("orders_submitted") == 0
    )
    if not boundary:
        raise CanonicalActivationReleaseError("truth_gap_evidence_boundary_invalid")
    expected_stations = list(canonical_station_ids)
    if (
        len(expected_stations) != 45
        or expected_stations != sorted(expected_stations)
        or len(set(expected_stations)) != 45
    ):
        raise CanonicalActivationReleaseError("truth_gap_evidence_station_authority_invalid")
    stations = value.get("stations")
    if not isinstance(stations, list) or value.get("station_count") != 45:
        raise CanonicalActivationReleaseError("truth_gap_evidence_station_count_invalid")
    allowed_blockers = {DATA_MISSING, OOS_SAMPLE_SHORT}
    normalized: dict[str, dict[str, Any]] = {}
    date_set: tuple[str, ...] | None = None
    total_truths = 0
    for row in stations:
        if not isinstance(row, Mapping):
            raise CanonicalActivationReleaseError("truth_gap_evidence_station_invalid")
        station_id = str(row.get("station_id") or "").strip().upper()
        if not station_id or station_id in normalized:
            raise CanonicalActivationReleaseError("truth_gap_evidence_station_duplicate")
        ready = row.get("truth_ready_dates")
        blocked = row.get("truth_blocked_dates")
        blockers = row.get("blockers")
        diagnostics = row.get("missing_date_diagnostics")
        if not all(isinstance(item, list) for item in (ready, blocked, blockers, diagnostics)):
            raise CanonicalActivationReleaseError("truth_gap_evidence_station_shape_invalid")
        ready_dates = [str(item) for item in ready]
        blocked_dates = [str(item) for item in blocked]
        if (
            ready_dates != sorted(set(ready_dates))
            or blocked_dates != sorted(set(blocked_dates))
            or set(ready_dates) & set(blocked_dates)
        ):
            raise CanonicalActivationReleaseError("truth_gap_evidence_dates_invalid")
        combined = tuple(sorted(set(ready_dates) | set(blocked_dates)))
        if date_set is None:
            date_set = combined
        elif combined != date_set:
            raise CanonicalActivationReleaseError("truth_gap_evidence_date_window_mismatch")
        normalized_blockers = [str(item) for item in blockers]
        if not normalized_blockers or not set(normalized_blockers) <= allowed_blockers:
            raise CanonicalActivationReleaseError("truth_gap_evidence_blocker_invalid")
        diagnostic_dates = {
            str(item.get("event_date") or "")
            for item in diagnostics
            if isinstance(item, Mapping)
        }
        if diagnostic_dates != set(blocked_dates):
            raise CanonicalActivationReleaseError("truth_gap_evidence_diagnostics_mismatch")
        truth_count = row.get("truth_fact_count")
        if isinstance(truth_count, bool) or not isinstance(truth_count, int):
            raise CanonicalActivationReleaseError("truth_gap_evidence_truth_count_invalid")
        if truth_count != len(ready_dates):
            raise CanonicalActivationReleaseError("truth_gap_evidence_truth_count_mismatch")
        total_truths += truth_count
        normalized[station_id] = {
            "ready_dates": frozenset(ready_dates),
            "blocked_dates": frozenset(blocked_dates),
            "truth_fact_count": truth_count,
        }
    if sorted(normalized) != expected_stations:
        raise CanonicalActivationReleaseError("truth_gap_evidence_station_set_mismatch")
    if value.get("truth_fact_count") != total_truths:
        raise CanonicalActivationReleaseError("truth_gap_evidence_total_truth_count_mismatch")
    canonical_contract_sha = _sha(
        value.get("canonical_contract_sha256"),
        "truth_gap_evidence_canonical_contract_sha256",
    )
    valid_contract_hashes = {
        str(item.get("content_sha256") or "") for item in contract_evidence
    }
    if canonical_contract_sha not in valid_contract_hashes:
        raise CanonicalActivationReleaseError("truth_gap_evidence_contract_binding_missing")
    source_evidence = {
        "source_type": "TRUTH_GAP_EVIDENCE",
        "path": str(source),
        "schema": TRUTH_GAP_SCHEMA,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": supplied,
        "as_of_utc": evidence_as_of,
        "builder_id": str(value.get("builder_id") or ""),
        "canonical_contract_content_sha256": canonical_contract_sha,
        "station_count": 45,
        "truth_fact_count": total_truths,
        "complete_station_count": value.get("complete_station_count"),
        "blocked_station_count": value.get("blocked_station_count"),
        "date_set_sha256": content_sha256(list(date_set or ())),
    }
    return {
        "source_evidence": source_evidence,
        "stations_by_id": normalized,
        "date_set": list(date_set or ()),
        "content_sha256": supplied,
    }


def load_contracts(paths: Sequence[Path | str]) -> tuple[list[CanonicalContract], list[dict[str, Any]]]:
    if not paths:
        raise CanonicalActivationReleaseError("activation_release_contracts_missing")
    contracts: list[CanonicalContract] = []
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        source, raw, value = _stable_json(path)
        contract = CanonicalContract.from_mapping(value)
        if contract.content_sha256 in seen:
            raise CanonicalActivationReleaseError("activation_release_contract_duplicate")
        seen.add(contract.content_sha256)
        contracts.append(contract)
        evidence.append(
            {
                "path": str(source),
                "file_sha256": hashlib.sha256(raw).hexdigest(),
                "content_sha256": contract.content_sha256,
                "as_of_utc": contract.as_of_utc,
                "builder_id": contract.builder_id,
            }
        )
    return contracts, sorted(evidence, key=lambda item: item["content_sha256"])


def load_typed_scope_envelopes(
    paths: Sequence[Path | str],
    *,
    release_as_of_utc: str,
) -> tuple[list[CanonicalTypedScopeEnvelope], list[dict[str, Any]]]:
    """Load non-model scope inventories without inventing model-shaped keys."""

    cutoff = _utc_z(release_as_of_utc, "typed_scope_release_as_of_utc")
    envelopes: list[CanonicalTypedScopeEnvelope] = []
    evidence: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    seen_hashes: set[str] = set()
    for path in paths:
        source, raw, value = _stable_json(path)
        envelope = CanonicalTypedScopeEnvelope.from_mapping(value)
        if envelope.as_of_utc > cutoff:
            raise CanonicalActivationReleaseError("typed_scope_envelope_after_release_asof")
        if envelope.scope_type in seen_types:
            raise CanonicalActivationReleaseError("typed_scope_envelope_type_duplicate")
        if envelope.content_sha256 in seen_hashes:
            raise CanonicalActivationReleaseError("typed_scope_envelope_content_duplicate")
        seen_types.add(envelope.scope_type)
        seen_hashes.add(envelope.content_sha256)
        envelopes.append(envelope)
        evidence.append(
            {
                "path": str(source),
                "file_sha256": hashlib.sha256(raw).hexdigest(),
                "content_sha256": envelope.content_sha256,
                "as_of_utc": envelope.as_of_utc,
                "builder_id": envelope.builder_id,
                "scope_type": envelope.scope_type,
                "record_count": len(envelope.records),
            }
        )
    return (
        sorted(envelopes, key=lambda item: item.scope_type),
        sorted(evidence, key=lambda item: item["scope_type"]),
    )


def audit_contract_inputs(paths: Sequence[Path | str]) -> list[dict[str, Any]]:
    """Record invalid legacy contracts without adapting their missing fields."""

    if not paths:
        raise CanonicalActivationReleaseError("activation_release_contracts_missing")
    evidence: list[dict[str, Any]] = []
    for path in paths:
        source, raw, value = _stable_json(path)
        supplied = value.get("content_sha256")
        declared_valid = (
            isinstance(supplied, str)
            and len(supplied) == 64
            and supplied == content_sha256(
                {key: child for key, child in value.items() if key != "content_sha256"}
            )
        )
        parse_error = ""
        try:
            CanonicalContract.from_mapping(value)
        except ContractError as exc:
            parse_error = str(exc)
        evidence.append(
            {
                "path": str(source),
                "file_sha256": hashlib.sha256(raw).hexdigest(),
                "declared_content_sha256": str(supplied or ""),
                "declared_content_hash_valid": declared_valid,
                "canonical_contract_valid": not parse_error,
                "canonical_contract_error": parse_error,
            }
        )
    return sorted(evidence, key=lambda item: item["path"])


def load_db_audit_snapshot(
    db_path: Path | str,
    *,
    as_of_utc: str,
    _connection: sqlite3.Connection | None = None,
) -> tuple[list[CanonicalModelFact], list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Read valid model/activation facts and classify invalid truth as blocked."""

    cutoff = _utc_z(as_of_utc, "activation_release_as_of_utc")
    source = Path(db_path).expanduser().resolve(strict=True)
    owns_connection = _connection is None
    conn = _connection or sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    models: list[CanonicalModelFact] = []
    activations: list[dict[str, Any]] = []
    truths: dict[tuple[str, str], dict[str, Any]] = {}
    projection_rows: list[dict[str, Any]] = []
    try:
        if owns_connection:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
        for fact_id, fact_type, content_hash, fact_json, ingested_at in conn.execute(
            "SELECT fact_id,fact_type,content_sha256,fact_json,ingested_at_utc "
            "FROM canonical_ssot_fact WHERE ingested_at_utc<=? "
            "ORDER BY fact_type,scope_key,fact_id",
            (cutoff,),
        ):
            try:
                raw = json.loads(str(fact_json))
            except json.JSONDecodeError as exc:
                raise CanonicalActivationReleaseError("canonical_db_fact_json_invalid") from exc
            if not isinstance(raw, Mapping):
                raise CanonicalActivationReleaseError("canonical_db_fact_mapping_required")
            stored_hash = _sha(content_hash, "canonical_db_fact_content_sha256")
            supplied_key = "activation_content_sha256" if fact_type == "activation" else "content_sha256"
            if raw.get(supplied_key) != stored_hash:
                raise CanonicalActivationReleaseError("canonical_db_fact_stored_hash_mismatch")
            projection_rows.append(
                {
                    "fact_id": str(fact_id),
                    "fact_type": str(fact_type),
                    "content_sha256": stored_hash,
                    "ingested_at_utc": str(ingested_at),
                }
            )
            if fact_type == "model":
                models.append(CanonicalModelFact.from_mapping(raw))
            elif fact_type == "activation":
                activations.append(CanonicalActivationFact.from_mapping(raw).to_mapping())
            elif fact_type == "activation_supersession":
                CanonicalActivationSupersessionFact.from_mapping(raw)
            elif fact_type == "truth":
                station = str(raw.get("station_id") or "").strip().upper()
                event_date = str(raw.get("event_date") or "").strip()
                if not station or not event_date:
                    raise CanonicalActivationReleaseError("canonical_db_truth_scope_missing")
                key = (station, event_date)
                if key in truths:
                    raise CanonicalActivationReleaseError("canonical_db_truth_scope_ambiguous")
                try:
                    truth = CanonicalTruthFact.from_mapping(raw)
                except ContractError as exc:
                    truths[key] = {
                        "valid": False,
                        "content_sha256": stored_hash,
                        "error": str(exc),
                    }
                else:
                    truths[key] = {
                        "valid": True,
                        "content_sha256": truth.content_sha256,
                        "truth": truth,
                    }
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        if owns_connection:
            conn.rollback()
    finally:
        if owns_connection:
            conn.close()
    identity = {
        "db_path": str(source),
        "as_of_utc": cutoff,
        "data_version": data_version,
        "schema_version": schema_version,
        "row_count": len(projection_rows),
        "rows": projection_rows,
    }
    return models, activations, truths, _hashed(identity)


def _fact_mapping_and_hash(fact: object) -> tuple[str, Mapping[str, Any], str]:
    mapping = fact.to_mapping()  # type: ignore[attr-defined]
    if isinstance(fact, CanonicalActivationFact):
        return "activation", mapping, fact.activation_content_sha256
    name = type(fact).__name__
    fact_type = {
        "CanonicalModelFact": "model",
        "CanonicalTruthFact": "truth",
        "CanonicalResidualFact": "residual",
    }.get(name)
    if fact_type is None:
        raise CanonicalActivationReleaseError("activation_release_fact_type_invalid")
    return fact_type, mapping, str(mapping["content_sha256"])


def load_db_projection(
    db_path: Path | str,
    *,
    as_of_utc: str,
    contracts: Sequence[CanonicalContract],
    _connection: sqlite3.Connection | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read one strict-as-of SQLite snapshot and prove contract facts exist."""

    cutoff = _utc_z(as_of_utc, "activation_release_as_of_utc")
    source = Path(db_path).expanduser().resolve(strict=True)
    uri = f"file:{source}?mode=ro"
    owns_connection = _connection is None
    conn = _connection or sqlite3.connect(uri, uri=True)
    try:
        if owns_connection:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_ssot_fact'"
        ).fetchone() is None:
            raise CanonicalActivationReleaseError("canonical_ssot_fact_table_missing")
        expected: dict[str, tuple[str, str]] = {}
        covered_scopes: set[tuple[str, str, str, str]] = set()
        for contract in contracts:
            for collection in (
                contract.model_facts,
                contract.truth_facts,
                contract.residual_facts,
                contract.activation_facts,
            ):
                for fact in collection:
                    fact_type, mapping, fact_hash = _fact_mapping_and_hash(fact)
                    fact_id = f"{fact_type}:{fact_hash}"
                    fact_json = json.dumps(
                        mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    prior = expected.get(fact_id)
                    if prior is not None and prior != (fact_type, fact_json):
                        raise CanonicalActivationReleaseError("activation_release_contract_fact_conflict")
                    expected[fact_id] = (fact_type, fact_json)
            covered_scopes.update(_scope(item) for item in contract.activation_facts)

        projection_rows: list[dict[str, Any]] = []
        for fact_id in sorted(expected):
            row = conn.execute(
                "SELECT fact_type,content_sha256,fact_json,ingested_at_utc "
                "FROM canonical_ssot_fact WHERE fact_id=? AND ingested_at_utc<=?",
                (fact_id, cutoff),
            ).fetchone()
            if row is None:
                raise CanonicalActivationReleaseError("activation_release_contract_fact_missing_from_db")
            fact_type, fact_json = expected[fact_id]
            if str(row[0]) != fact_type or str(row[1]) != fact_id.split(":", 1)[1] or str(row[2]) != fact_json:
                raise CanonicalActivationReleaseError("activation_release_contract_fact_db_mismatch")
            projection_rows.append(
                {
                    "fact_id": fact_id,
                    "fact_type": fact_type,
                    "content_sha256": str(row[1]),
                    "ingested_at_utc": str(row[3]),
                }
            )

        activation_facts: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT fact_json,content_sha256,ingested_at_utc FROM canonical_ssot_fact "
            "WHERE fact_type='activation' AND ingested_at_utc<=? ORDER BY ingested_at_utc,fact_id",
            (cutoff,),
        ):
            try:
                fact = CanonicalActivationFact.from_mapping(json.loads(str(row[0])))
            except (json.JSONDecodeError, ContractError) as exc:
                raise CanonicalActivationReleaseError("activation_release_db_activation_invalid") from exc
            if _scope(fact) not in covered_scopes:
                continue
            if fact.activation_content_sha256 != str(row[1]):
                raise CanonicalActivationReleaseError("activation_release_db_activation_hash_mismatch")
            activation_facts.append(fact.to_mapping())
            projection_rows.append(
                {
                    "fact_id": f"activation:{fact.activation_content_sha256}",
                    "fact_type": "activation_candidate",
                    "content_sha256": fact.activation_content_sha256,
                    "ingested_at_utc": str(row[2]),
                }
            )
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        if owns_connection:
            conn.rollback()
    finally:
        if owns_connection:
            conn.close()
    unique_projection = {
        (row["fact_id"], row["fact_type"], row["content_sha256"], row["ingested_at_utc"]): row
        for row in projection_rows
    }
    ordered = [unique_projection[key] for key in sorted(unique_projection)]
    identity = {
        "db_path": str(source),
        "as_of_utc": cutoff,
        "data_version": data_version,
        "schema_version": schema_version,
        "row_count": len(ordered),
        "rows": ordered,
    }
    return activation_facts, _hashed(identity)


def load_db_supersession_facts(
    db_path: Path | str,
    *,
    as_of_utc: str,
    _connection: sqlite3.Connection | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cutoff = _utc_z(as_of_utc, "activation_supersession_as_of_utc")
    source = Path(db_path).expanduser().resolve(strict=True)
    owns_connection = _connection is None
    conn = _connection or sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    facts: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    try:
        if owns_connection:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
        for fact_json, stored_hash, ingested_at in conn.execute(
            "SELECT fact_json,content_sha256,ingested_at_utc FROM canonical_ssot_fact "
            "WHERE fact_type='activation_supersession' AND ingested_at_utc<=? "
            "ORDER BY scope_key,fact_id",
            (cutoff,),
        ):
            try:
                fact = CanonicalActivationSupersessionFact.from_mapping(
                    json.loads(str(fact_json))
                )
            except (TypeError, ValueError, json.JSONDecodeError, ContractError) as exc:
                raise CanonicalActivationReleaseError(
                    "activation_release_db_supersession_invalid"
                ) from exc
            if fact.content_sha256 != str(stored_hash):
                raise CanonicalActivationReleaseError(
                    "activation_release_db_supersession_hash_mismatch"
                )
            if _utc_z(
                fact.authority_as_of_utc,
                "activation_supersession_authority_as_of_utc",
            ) > cutoff:
                raise CanonicalActivationReleaseError(
                    "activation_release_db_supersession_after_as_of"
                )
            candidate_hashes: list[str] = []
            for activation_json, activation_hash in conn.execute(
                "SELECT fact_json,content_sha256 FROM canonical_ssot_fact "
                "WHERE fact_type='activation' AND ingested_at_utc<=? ORDER BY fact_id",
                (fact.authority_as_of_utc,),
            ):
                try:
                    activation = CanonicalActivationFact.from_mapping(
                        json.loads(str(activation_json))
                    )
                except (TypeError, ValueError, json.JSONDecodeError, ContractError) as exc:
                    raise CanonicalActivationReleaseError(
                        "activation_release_db_supersession_activation_invalid"
                    ) from exc
                if activation.activation_content_sha256 != str(activation_hash):
                    raise CanonicalActivationReleaseError(
                        "activation_release_db_supersession_activation_hash_mismatch"
                    )
                if _scope(activation) == (
                    fact.station_id,
                    fact.model_family,
                    fact.lead_bucket,
                    fact.run_cycle,
                ):
                    candidate_hashes.append(activation.activation_content_sha256)
            if tuple(sorted(candidate_hashes)) != fact.candidate_activation_content_sha256s:
                raise CanonicalActivationReleaseError(
                    "activation_release_db_supersession_candidate_set_mismatch"
                )
            facts.append(fact.to_mapping())
            rows.append(
                {
                    "fact_id": f"activation_supersession:{fact.content_sha256}",
                    "fact_type": "activation_supersession",
                    "content_sha256": fact.content_sha256,
                    "ingested_at_utc": str(ingested_at),
                }
            )
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        if owns_connection:
            conn.rollback()
    finally:
        if owns_connection:
            conn.close()
    identity = {
        "db_path": str(source),
        "as_of_utc": cutoff,
        "data_version": data_version,
        "schema_version": schema_version,
        "row_count": len(rows),
        "rows": rows,
    }
    return facts, _hashed(identity)


def verify_supersession_projection_consistency(
    supersession_projection: Mapping[str, Any],
    canonical_projection: Mapping[str, Any],
) -> None:
    """Prove a dedicated supersession read exactly matches the DB snapshot."""

    for label, value in (
        ("supersession", supersession_projection),
        ("canonical", canonical_projection),
    ):
        supplied = _sha(value.get("content_sha256"), f"{label}_projection_content_sha256")
        if supplied != content_sha256(
            {key: child for key, child in value.items() if key != "content_sha256"}
        ):
            raise CanonicalActivationReleaseError(
                f"{label}_projection_content_sha256_mismatch"
            )
    for field in ("db_path", "as_of_utc", "data_version", "schema_version"):
        if supersession_projection.get(field) != canonical_projection.get(field):
            raise CanonicalActivationReleaseError(
                "activation_release_supersession_projection_snapshot_mismatch"
            )
    dedicated_rows = {
        (
            str(row.get("fact_id") or ""),
            str(row.get("fact_type") or ""),
            str(row.get("content_sha256") or ""),
            str(row.get("ingested_at_utc") or ""),
        )
        for row in supersession_projection.get("rows", [])
        if isinstance(row, Mapping)
    }
    canonical_rows = {
        (
            str(row.get("fact_id") or ""),
            str(row.get("fact_type") or ""),
            str(row.get("content_sha256") or ""),
            str(row.get("ingested_at_utc") or ""),
        )
        for row in canonical_projection.get("rows", [])
        if isinstance(row, Mapping)
        and str(row.get("fact_type") or "") == "activation_supersession"
    }
    if dedicated_rows != canonical_rows:
        raise CanonicalActivationReleaseError(
            "activation_release_supersession_projection_snapshot_mismatch"
        )


def verify_projection_snapshot_consistency(
    contract_projection: Mapping[str, Any],
    canonical_projection: Mapping[str, Any],
) -> None:
    """Prove the two read phases describe one append-only as-of fact set."""

    for label, value in (
        ("contract", contract_projection),
        ("canonical", canonical_projection),
    ):
        supplied = _sha(value.get("content_sha256"), f"{label}_projection_content_sha256")
        if supplied != content_sha256(
            {key: child for key, child in value.items() if key != "content_sha256"}
        ):
            raise CanonicalActivationReleaseError(f"{label}_projection_content_sha256_mismatch")
    for field in ("db_path", "as_of_utc", "data_version", "schema_version"):
        if contract_projection.get(field) != canonical_projection.get(field):
            raise CanonicalActivationReleaseError("activation_release_projection_snapshot_mismatch")
    canonical_rows = {
        (
            str(row.get("fact_id") or ""),
            str(row.get("fact_type") or ""),
            str(row.get("content_sha256") or ""),
            str(row.get("ingested_at_utc") or ""),
        )
        for row in canonical_projection.get("rows", [])
        if isinstance(row, Mapping)
    }
    for row in contract_projection.get("rows", []):
        if not isinstance(row, Mapping):
            raise CanonicalActivationReleaseError("activation_release_contract_projection_row_invalid")
        fact_type = str(row.get("fact_type") or "")
        normalized_type = "activation" if fact_type == "activation_candidate" else fact_type
        normalized = (
            str(row.get("fact_id") or ""),
            normalized_type,
            str(row.get("content_sha256") or ""),
            str(row.get("ingested_at_utc") or ""),
        )
        if normalized not in canonical_rows:
            raise CanonicalActivationReleaseError("activation_release_projection_snapshot_mismatch")


def load_db_activation_release_snapshot(
    db_path: Path | str,
    *,
    as_of_utc: str,
    contracts: Sequence[CanonicalContract],
) -> tuple[
    list[dict[str, Any]],
    list[CanonicalModelFact],
    dict[tuple[str, str], dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    """Load every activation-release input inside one SQLite snapshot.

    Returns contract-scoped activations, models, truths, supersessions, the
    contract projection, the complete canonical projection, and the dedicated
    supersession projection.  Callers must not mix these values with loaders
    from another connection.
    """

    source = Path(db_path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        contract_activations, contract_projection = load_db_projection(
            source,
            as_of_utc=as_of_utc,
            contracts=contracts,
            _connection=conn,
        )
        models, _all_activations, truths, canonical_projection = (
            load_db_audit_snapshot(
                source,
                as_of_utc=as_of_utc,
                _connection=conn,
            )
        )
        supersessions, supersession_projection = load_db_supersession_facts(
            source,
            as_of_utc=as_of_utc,
            _connection=conn,
        )
        verify_projection_snapshot_consistency(
            contract_projection, canonical_projection
        )
        verify_supersession_projection_consistency(
            supersession_projection, canonical_projection
        )
        conn.rollback()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    return (
        contract_activations,
        models,
        truths,
        supersessions,
        contract_projection,
        canonical_projection,
        supersession_projection,
    )


def _activation_ref(fact: CanonicalActivationFact) -> dict[str, Any]:
    return {
        "activation_id": fact.activation_id,
        "activation_generation": fact.activation_generation,
        "activation_content_sha256": fact.activation_content_sha256,
        "artifact_content_sha256": fact.artifact_content_sha256,
        "selection_evidence_sha256": fact.selection_evidence_sha256,
    }


def build_activation_index_v2(
    activation_facts: Sequence[Mapping[str, Any]],
    *,
    as_of_utc: str,
    canonical_projection_content_sha256: str,
    supersession_facts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    cutoff = _utc_z(as_of_utc, "activation_index_as_of_utc")
    projection_sha = _sha(
        canonical_projection_content_sha256,
        "activation_index_canonical_projection_content_sha256",
    )
    grouped: dict[tuple[str, str, str, str], list[CanonicalActivationFact]] = defaultdict(list)
    seen_hashes: set[str] = set()
    for raw in activation_facts:
        fact = CanonicalActivationFact.from_mapping(raw)
        if fact.authority_scope != NO_LIVE_AUTHORITY or fact.oos_role != "historical_strict_oos":
            raise CanonicalActivationReleaseError("activation_index_authority_invalid")
        if fact.activation_content_sha256 in seen_hashes:
            raise CanonicalActivationReleaseError("activation_index_duplicate_fact")
        seen_hashes.add(fact.activation_content_sha256)
        grouped[_scope(fact)].append(fact)
    supersessions_by_scope: dict[
        tuple[str, str, str, str], list[CanonicalActivationSupersessionFact]
    ] = defaultdict(list)
    supersession_hashes: set[str] = set()
    for raw in supersession_facts:
        fact = CanonicalActivationSupersessionFact.from_mapping(raw)
        if fact.content_sha256 in supersession_hashes:
            raise CanonicalActivationReleaseError("activation_index_duplicate_supersession")
        supersession_hashes.add(fact.content_sha256)
        supersessions_by_scope[
            (fact.station_id, fact.model_family, fact.lead_bucket, fact.run_cycle)
        ].append(fact)
    entries: list[dict[str, Any]] = []
    for scope in sorted(grouped):
        facts = grouped[scope]
        if len(facts) == 1:
            entries.append(
                {
                    "scope": _scope_mapping(scope),
                    "state": CURRENT,
                    "current_activation": _activation_ref(facts[0]),
                }
            )
        else:
            candidates = sorted(
                (_activation_ref(fact) for fact in facts),
                key=lambda item: (
                    item["activation_generation"],
                    item["activation_id"],
                    item["activation_content_sha256"],
                ),
            )
            facts_by_hash = {
                fact.activation_content_sha256: fact for fact in facts
            }
            candidate_hashes = tuple(sorted(facts_by_hash))
            authorities = supersessions_by_scope.get(scope, [])
            edges: dict[str, str] = {}
            reverse: dict[str, str] = {}
            valid_authority = bool(authorities)
            for authority in authorities:
                predecessor = authority.predecessor_activation_content_sha256
                successor = authority.successor_activation_content_sha256
                before = facts_by_hash.get(predecessor)
                after = facts_by_hash.get(successor)
                authority_candidates = set(
                    authority.candidate_activation_content_sha256s
                )
                if (
                    _utc_z(
                        authority.authority_as_of_utc,
                        "activation_supersession_authority_as_of_utc",
                    ) > cutoff
                    or not authority_candidates.issubset(set(candidate_hashes))
                    or predecessor not in authority_candidates
                    or successor not in authority_candidates
                    or before is None
                    or after is None
                    or before.activation_generation != authority.predecessor_generation
                    or after.activation_generation != authority.successor_generation
                    or (predecessor in edges and edges[predecessor] != successor)
                    or (successor in reverse and reverse[successor] != predecessor)
                ):
                    valid_authority = False
                    break
                edges[predecessor] = successor
                reverse[successor] = predecessor
            heads = set(candidate_hashes) - set(edges)
            if valid_authority and len(heads) == 1 and len(edges) == len(candidate_hashes) - 1:
                head = next(iter(heads))
                for candidate in candidate_hashes:
                    visited: set[str] = set()
                    cursor = candidate
                    while cursor in edges:
                        if cursor in visited:
                            valid_authority = False
                            break
                        visited.add(cursor)
                        cursor = edges[cursor]
                    if cursor != head:
                        valid_authority = False
                    if not valid_authority:
                        break
            else:
                valid_authority = False
            if valid_authority:
                entries.append(
                    {
                        "scope": _scope_mapping(scope),
                        "state": CURRENT,
                        "current_activation": _activation_ref(facts_by_hash[head]),
                        "supersession_authority_content_sha256s": sorted(
                            item.content_sha256 for item in authorities
                        ),
                    }
                )
            else:
                entries.append(
                    {
                        "scope": _scope_mapping(scope),
                        "state": BLOCKED,
                        "blocker": ACTIVATION_CURRENT_AMBIGUOUS,
                        "activation_candidates": candidates,
                    }
                )
    identity = {
        "schema": ACTIVATION_INDEX_SCHEMA,
        "as_of_utc": cutoff,
        "authority_scope": NO_LIVE_AUTHORITY,
        "state": BLOCKED if any(item["state"] == BLOCKED for item in entries) else CURRENT,
        "canonical_projection_content_sha256": projection_sha,
        "activation_input_set_sha256": content_sha256(sorted(seen_hashes)),
        "supersession_input_set_sha256": content_sha256(sorted(supersession_hashes)),
        "entry_count": len(entries),
        "entries": entries,
        "production_no_live": True,
        "formal_live_authority": False,
    }
    return verify_activation_index_v2(_hashed(identity))


def verify_activation_index_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    supplied = _sha(value.get("content_sha256"), "activation_index_content_sha256")
    if supplied != content_sha256({key: child for key, child in value.items() if key != "content_sha256"}):
        raise CanonicalActivationReleaseError("activation_index_content_sha256_mismatch")
    if value.get("schema") != ACTIVATION_INDEX_SCHEMA:
        raise CanonicalActivationReleaseError("activation_index_schema_invalid")
    entries = value.get("entries")
    if not isinstance(entries, list) or value.get("entry_count") != len(entries):
        raise CanonicalActivationReleaseError("activation_index_entries_invalid")
    scopes: list[tuple[str, str, str, str]] = []
    blocked = False
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("scope"), Mapping):
            raise CanonicalActivationReleaseError("activation_index_entry_invalid")
        scope = tuple(str(entry["scope"].get(key) or "") for key in (
            "station_id", "model_family", "lead_bucket", "run_cycle"
        ))
        if not all(scope):
            raise CanonicalActivationReleaseError("activation_index_scope_invalid")
        scopes.append(scope)  # type: ignore[arg-type]
        if entry.get("state") == CURRENT:
            if not isinstance(entry.get("current_activation"), Mapping):
                raise CanonicalActivationReleaseError("activation_index_current_invalid")
            _sha(entry["current_activation"].get("activation_content_sha256"), "activation_index_current_activation_sha256")
            authorities = entry.get("supersession_authority_content_sha256s")
            if authorities is not None:
                if (
                    not isinstance(authorities, list)
                    or not authorities
                    or authorities != sorted(set(authorities))
                ):
                    raise CanonicalActivationReleaseError("activation_index_supersession_authority_invalid")
                for authority in authorities:
                    _sha(authority, "activation_index_supersession_authority_sha256")
        elif entry.get("state") == BLOCKED:
            blocked = True
            if entry.get("blocker") != ACTIVATION_CURRENT_AMBIGUOUS:
                raise CanonicalActivationReleaseError("activation_index_blocker_invalid")
            candidates = entry.get("activation_candidates")
            if not isinstance(candidates, list) or len(candidates) < 2:
                raise CanonicalActivationReleaseError("activation_index_candidates_invalid")
        else:
            raise CanonicalActivationReleaseError("activation_index_state_invalid")
    if scopes != sorted(scopes) or len(scopes) != len(set(scopes)):
        raise CanonicalActivationReleaseError("activation_index_scope_order_invalid")
    if value.get("state") != (BLOCKED if blocked else CURRENT):
        raise CanonicalActivationReleaseError("activation_index_global_state_invalid")
    if "supersession_input_set_sha256" in value:
        _sha(
            value.get("supersession_input_set_sha256"),
            "activation_index_supersession_input_set_sha256",
        )
    return dict(value)


def _base_record(
    model: CanonicalModelFact,
    *,
    truth_content_sha256: str,
    contract: CanonicalContract,
    train_count: int,
    holdout_count: int,
    taf_revision_complete: bool,
) -> dict[str, Any]:
    return {
        "station_id": model.station_id,
        "model_id": model.model_id or model.ensemble_system_id,
        "model_family": model.model_family,
        "provider_id": model.provider_id,
        "source_class": model.source_class,
        "run_cycle": model.run_cycle,
        "lead_bucket": model.lead_bucket,
        "event_date": model.event_date,
        "evidence_role": model.oos_role,
        "native_arrival_verified": model.native_arrival_indexed,
        "raw_object_sha256": model.raw_object_sha256,
        "model_fact_content_sha256": model.content_sha256,
        "truth_content_sha256": truth_content_sha256,
        "canonical_contract_content_sha256": contract.content_sha256,
        "train_count": train_count,
        "holdout_count": holdout_count,
        "taf_revision_complete": taf_revision_complete,
    }


def _typed_scope_ledger_projection(
    envelopes: Sequence[CanonicalTypedScopeEnvelope],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    by_type: dict[str, dict[str, Any]] = {}
    seen_scopes: set[str] = set()
    for envelope in sorted(envelopes, key=lambda item: item.scope_type):
        type_blockers: set[str] = set()
        for fact in sorted(envelope.records, key=lambda item: item.scope_key):
            if fact.scope_key in seen_scopes:
                raise CanonicalActivationReleaseError("typed_scope_release_scope_duplicate")
            seen_scopes.add(fact.scope_key)
            fact_mapping = fact.to_mapping()
            record = _hashed(
                {
                    "scope_type": fact.scope_type,
                    "scope_kind": fact.scope_kind,
                    "natural_key": dict(fact.natural_key),
                    "as_of_utc": fact.as_of_utc,
                    "state": fact.state,
                    "blockers": list(fact.blockers),
                    "evidence_content_sha256": fact.evidence_content_sha256,
                    "authority_scope": fact.authority_scope,
                    "typed_scope_fact_content_sha256": fact.content_sha256,
                }
            )
            records.append(record)
            if fact.blockers:
                type_blockers.update(fact.blockers)
            status = _hashed(
                {
                    "scope_type": fact.scope_type,
                    "scope_kind": fact.scope_kind,
                    "natural_key": dict(fact.natural_key),
                    "builder_id": envelope.builder_id,
                    "state": (
                        "BUILT"
                        if fact.state in {DATA_READY, OOS_READY, ACTIVATION_READY}
                        else BLOCKED
                    ),
                    "blockers": list(fact.blockers),
                    "canonical_contract_content_sha256": envelope.content_sha256,
                    "record_count": 1,
                    "record_set_sha256": content_sha256([record["content_sha256"]]),
                }
            )
            statuses.append(status)
        by_type[envelope.scope_type] = {
            "scope_type": envelope.scope_type,
            "state": BLOCKED if type_blockers else "READY",
            "scope_count": len(envelope.records),
            "blockers": sorted(type_blockers),
            "envelope_content_sha256": envelope.content_sha256,
        }
    return records, statuses, by_type


def build_release_artifacts(
    *,
    contracts: Sequence[CanonicalContract],
    contract_evidence: Sequence[Mapping[str, Any]],
    activation_facts: Sequence[Mapping[str, Any]],
    canonical_projection: Mapping[str, Any],
    inner_snapshot: Mapping[str, Any],
    as_of_utc: str,
    train_count: int,
    holdout_count: int,
    taf_revision_complete: bool,
    supersession_facts: Sequence[Mapping[str, Any]] = (),
    typed_scope_envelopes: Sequence[CanonicalTypedScopeEnvelope] = (),
    typed_scope_evidence: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build ledger v2, current index v2 and their exact binding."""

    cutoff = _utc_z(as_of_utc, "activation_release_as_of_utc")
    if isinstance(train_count, bool) or not isinstance(train_count, int) or train_count < 0:
        raise CanonicalActivationReleaseError("activation_release_train_count_invalid")
    if isinstance(holdout_count, bool) or not isinstance(holdout_count, int) or holdout_count < 0:
        raise CanonicalActivationReleaseError("activation_release_holdout_count_invalid")
    if not isinstance(taf_revision_complete, bool):
        raise CanonicalActivationReleaseError("activation_release_taf_revision_invalid")
    projection_sha = _sha(canonical_projection.get("content_sha256"), "canonical_projection_content_sha256")
    index = build_activation_index_v2(
        activation_facts,
        as_of_utc=cutoff,
        canonical_projection_content_sha256=projection_sha,
        supersession_facts=supersession_facts,
    )
    index_by_scope = {
        tuple(entry["scope"][key] for key in ("station_id", "model_family", "lead_bucket", "run_cycle")): entry
        for entry in index["entries"]
    }
    contracts_by_activation: dict[str, CanonicalContract] = {}
    records_by_contract_scope: dict[
        tuple[str, tuple[str, str, str, str]], list[dict[str, Any]]
    ] = defaultdict(list)
    all_records_by_scope: dict[
        tuple[str, str, str, str], dict[str, dict[str, Any]]
    ] = defaultdict(dict)
    for contract in contracts:
        if _utc_z(contract.as_of_utc, "canonical_contract_as_of_utc") > cutoff:
            raise CanonicalActivationReleaseError("activation_release_contract_after_as_of")
        truths = {(truth.station_id, truth.event_date): truth for truth in contract.truth_facts}
        for activation in contract.activation_facts:
            scope = _scope(activation)
            prior = contracts_by_activation.get(
                activation.activation_content_sha256
            )
            if prior is not None and prior.content_sha256 != contract.content_sha256:
                raise CanonicalActivationReleaseError(
                    "activation_release_activation_contract_ambiguous"
                )
            contracts_by_activation[activation.activation_content_sha256] = contract
        for model in contract.model_facts:
            truth = truths.get((model.station_id, model.event_date))
            if truth is None or not truth.closed_day or truth.source_lane != "AWC":
                raise CanonicalActivationReleaseError("activation_release_truth_binding_invalid")
            if truth.settlement_timezone != model.settlement_timezone:
                raise CanonicalActivationReleaseError("activation_release_timezone_mismatch")
            scope = _scope(model)
            base = _base_record(
                model,
                truth_content_sha256=truth.content_sha256,
                contract=contract,
                train_count=train_count,
                holdout_count=holdout_count,
                taf_revision_complete=taf_revision_complete,
            )
            records_by_contract_scope[(contract.content_sha256, scope)].append(base)
            prior_base = all_records_by_scope[scope].get(model.content_sha256)
            if prior_base is not None:
                comparable_prior = {
                    key: value
                    for key, value in prior_base.items()
                    if key != "canonical_contract_content_sha256"
                }
                comparable_current = {
                    key: value
                    for key, value in base.items()
                    if key != "canonical_contract_content_sha256"
                }
                if comparable_prior != comparable_current:
                    raise CanonicalActivationReleaseError(
                        "activation_release_model_contract_fact_conflict"
                    )
            else:
                all_records_by_scope[scope][model.content_sha256] = base

    if set(index_by_scope) != set(all_records_by_scope):
        raise CanonicalActivationReleaseError(
            "activation_release_index_scope_mismatch"
        )

    ledger_records: list[dict[str, Any]] = []
    builder_statuses: list[dict[str, Any]] = []
    binding_entries: list[dict[str, Any]] = []
    for scope in sorted(all_records_by_scope):
        index_entry = index_by_scope.get(scope)
        blocker: str | None = None
        activation_sha = ""
        activation: CanonicalActivationFact | None = None
        contract: CanonicalContract | None = None
        if index_entry is None:
            blocker = ACTIVATION_ROOT_MISSING
        elif index_entry["state"] == BLOCKED:
            blocker = str(index_entry["blocker"])
        else:
            activation_sha = str(index_entry["current_activation"]["activation_content_sha256"])
            contract = contracts_by_activation.get(activation_sha)
            if contract is None:
                blocker = ACTIVATION_CONTRACT_BINDING_MISSING
            else:
                activation = next(
                    item for item in contract.activation_facts
                    if item.activation_content_sha256 == activation_sha
                )
        if contract is not None:
            records = records_by_contract_scope.get(
                (contract.content_sha256, scope), []
            )
            if not records:
                blocker = ACTIVATION_CONTRACT_BINDING_MISSING
        else:
            # An ambiguous/no-current scope may still publish OOS evidence, but
            # it cannot claim a particular generation's contract binding.
            records = [
                {**record, "canonical_contract_content_sha256": ""}
                for record in all_records_by_scope[scope].values()
            ]
        if blocker is None and activation is not None:
                model_ids = {str(record["model_id"]).upper() for record in records}
                if model_ids != set(activation.candidate_models):
                    blocker = ACTIVATION_MODEL_SET_MISMATCH
                else:
                    dates_by_model: dict[str, set[str]] = defaultdict(set)
                    for record in records:
                        dates_by_model[str(record["model_id"]).upper()].add(str(record["event_date"]))
                    cohorts = list(dates_by_model.values())
                    expected_samples = train_count + holdout_count
                    if (
                        not cohorts
                        or any(len(dates) != expected_samples for dates in cohorts)
                        or any(dates != cohorts[0] for dates in cohorts[1:])
                    ):
                        blocker = ACTIVATION_SAMPLE_COHORT_MISMATCH
        finalized_scope_records: list[dict[str, Any]] = []
        for base in sorted(records, key=lambda item: (
            item["station_id"], item["model_id"], item["event_date"]
        )):
            identity = {
                **base,
                "state": ACTIVATION_READY if blocker is None else OOS_READY,
                "blockers": [],
                "activation_content_sha256": activation_sha if blocker is None else "",
            }
            finalized_scope_records.append(_hashed(identity))
        ledger_records.extend(finalized_scope_records)
        record_hashes = [item["content_sha256"] for item in finalized_scope_records]
        contract_sha = contract.content_sha256 if contract is not None else ""
        status_identity = {
            **_scope_mapping(scope),
            "builder_id": contract.builder_id if contract is not None else "UNBOUND",
            "state": "BUILT" if blocker is None else BLOCKED,
            "blockers": [] if blocker is None else [blocker],
            "canonical_contract_content_sha256": contract_sha,
            "record_count": len(finalized_scope_records),
            "record_set_sha256": content_sha256(record_hashes),
        }
        status = _hashed(status_identity)
        builder_statuses.append(status)
        if blocker is None and activation is not None:
            binding_entries.append(
                _hashed(
                    {
                        **_scope_mapping(scope),
                        "state": "BOUND",
                        "activation_content_sha256": activation.activation_content_sha256,
                        "activation_generation": activation.activation_generation,
                        "canonical_contract_content_sha256": contract_sha,
                        "builder_status_content_sha256": status["content_sha256"],
                        "record_count": len(record_hashes),
                        "record_set_sha256": content_sha256(record_hashes),
                    }
                )
            )
        else:
            binding_entries.append(
                _hashed(
                    {
                        **_scope_mapping(scope),
                        "state": BLOCKED,
                        "blocker": blocker,
                        "activation_candidates": (
                            index_entry.get("activation_candidates", [])
                            if isinstance(index_entry, Mapping) else []
                        ),
                        "builder_status_content_sha256": status["content_sha256"],
                        "record_count": len(record_hashes),
                        "record_set_sha256": content_sha256(record_hashes),
                    }
                )
            )

    record_keys = [
        "|".join(str(item[key]) for key in (
            "station_id", "model_id", "model_family", "run_cycle", "lead_bucket", "event_date"
        ))
        for item in ledger_records
    ]
    if len(record_keys) != len(set(record_keys)):
        raise CanonicalActivationReleaseError("activation_release_ledger_record_duplicate")
    typed_records, typed_statuses, typed_by_type = _typed_scope_ledger_projection(
        typed_scope_envelopes
    )
    ledger_records.extend(typed_records)
    builder_statuses.extend(typed_statuses)
    ledger_records.sort(
        key=lambda item: (
            str(item.get("scope_type") or MODEL_OOS_SCOPE),
            str(item.get("station_id") or ""),
            str(item.get("model_id") or ""),
            str(item.get("model_family") or ""),
            str(item.get("run_cycle") or ""),
            str(item.get("lead_bucket") or ""),
            str(item.get("event_date") or ""),
            str(item.get("scope_kind") or ""),
            json.dumps(
                item.get("natural_key") or {},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    )
    builder_statuses.sort(
        key=lambda item: (
            str(item.get("scope_type") or MODEL_OOS_SCOPE),
            str(item.get("station_id") or ""),
            str(item.get("model_family") or ""),
            str(item.get("lead_bucket") or ""),
            str(item.get("run_cycle") or ""),
            str(item.get("scope_kind") or ""),
            json.dumps(
                item.get("natural_key") or {},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    )
    model_statuses = [
        item
        for item in builder_statuses
        if str(item.get("scope_type") or MODEL_OOS_SCOPE) == MODEL_OOS_SCOPE
    ]
    model_blockers = sorted(
        {
            str(blocker)
            for status in model_statuses
            for blocker in status.get("blockers", [])
        }
    )
    scope_type_statuses = [
        {
            "scope_type": MODEL_OOS_SCOPE,
            "state": BLOCKED if model_blockers else "READY",
            "scope_count": len(model_statuses),
            "blockers": model_blockers,
        },
        *[
            dict(
                typed_by_type.get(
                    scope_type,
                    {
                        "scope_type": scope_type,
                        "state": BLOCKED,
                        "scope_count": 0,
                        "blockers": [ACTIVATION_ROOT_MISSING],
                    },
                )
            )
            for scope_type in REQUIRED_SCOPE_TYPES
            if scope_type != MODEL_OOS_SCOPE
        ],
    ]
    ledger_identity = {
        "schema": LEDGER_SCHEMA,
        "as_of_utc": cutoff,
        "canonical_projection_content_sha256": projection_sha,
        "source_contracts": [
            *[dict(item) for item in contract_evidence],
            *[dict(item) for item in typed_scope_evidence],
        ],
        "required_scope_types": list(REQUIRED_SCOPE_TYPES),
        "scope_type_statuses": scope_type_statuses,
        "typed_scope_coverage_complete": all(
            scope_type in typed_by_type for scope_type in TYPED_SCOPE_TYPES
        ),
        "summary": {
            "record_count": len(ledger_records),
            "activation_ready_count": sum(item["state"] == ACTIVATION_READY for item in ledger_records),
            "oos_ready_count": sum(item["state"] == OOS_READY for item in ledger_records),
            "blocked_count": sum(item["state"] == BLOCKED for item in ledger_records),
            "data_ready_count": sum(item["state"] == DATA_READY for item in ledger_records),
            "candidate_only_count": sum(
                item["state"] == CANDIDATE_ONLY for item in ledger_records
            ),
            "builder_scope_count": len(builder_statuses),
            "builder_blocked_count": sum(item["state"] == BLOCKED for item in builder_statuses),
        },
        "records": ledger_records,
        "builder_scope_statuses": builder_statuses,
        "future_data_counts_as_oos": False,
        "production_no_live": True,
    }
    ledger = _hashed(ledger_identity)
    global_blockers: list[str] = []
    if any(item["state"] == BLOCKED for item in binding_entries):
        global_blockers.append("ACTIVATION_BINDING_SCOPE_BLOCKED")
    if any(
        status.get("state") == BLOCKED
        for status in scope_type_statuses
        if status.get("scope_type") != MODEL_OOS_SCOPE
    ):
        global_blockers.append(TYPED_SCOPE_BLOCKED)
    if not bool(inner_snapshot.get("boundary_valid")):
        global_blockers.append(INNER_OOS_SNAPSHOT_INVALID)
    if str(inner_snapshot.get("as_of_utc")) != cutoff:
        global_blockers.append(INNER_OOS_SNAPSHOT_AS_OF_MISMATCH)
    _extend_inner_snapshot_blockers(global_blockers, inner_snapshot)
    binding_identity = {
        "schema": BINDING_SCHEMA,
        "as_of_utc": cutoff,
        "state": BLOCKED if global_blockers else "BOUND",
        "blockers": sorted(global_blockers),
        "canonical_projection_content_sha256": projection_sha,
        "ledger_content_sha256": ledger["content_sha256"],
        "activation_index_content_sha256": index["content_sha256"],
        "inner_oos_snapshot": dict(inner_snapshot),
        "entry_count": len(binding_entries),
        "entries": binding_entries,
        "production_no_live": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "writes_production_db": False,
        "writes_target_database": False,
    }
    return ledger, index, _hashed(binding_identity)


def build_truth_gap_release(
    *,
    contracts: Sequence[CanonicalContract],
    models: Sequence[CanonicalModelFact],
    activation_facts: Sequence[Mapping[str, Any]],
    truths: Mapping[tuple[str, str], Mapping[str, Any]],
    canonical_projection: Mapping[str, Any],
    contract_evidence: Sequence[Mapping[str, Any]],
    truth_gap_evidence: Mapping[str, Any],
    inner_snapshot: Mapping[str, Any],
    as_of_utc: str,
    train_count: int,
    holdout_count: int,
    taf_revision_complete: bool,
    typed_scope_envelopes: Sequence[CanonicalTypedScopeEnvelope] = (),
    typed_scope_evidence: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Project exact historical truth gaps onto the complete model inventory.

    This is the pre-activation PR04 release path.  It preserves valid model
    rows, exposes real DATA_MISSING/OOS_SAMPLE_SHORT blockers, and never turns
    a truth receipt into activation authority.
    """

    cutoff = _utc_z(as_of_utc, "activation_release_as_of_utc")
    if isinstance(train_count, bool) or not isinstance(train_count, int) or train_count < 0:
        raise CanonicalActivationReleaseError("activation_release_train_count_invalid")
    if isinstance(holdout_count, bool) or not isinstance(holdout_count, int) or holdout_count < 0:
        raise CanonicalActivationReleaseError("activation_release_holdout_count_invalid")
    if not isinstance(taf_revision_complete, bool):
        raise CanonicalActivationReleaseError("activation_release_taf_revision_invalid")
    projection_sha = _sha(
        canonical_projection.get("content_sha256"),
        "canonical_projection_content_sha256",
    )
    # A truth-gap receipt is not promotion authority.  Even if the backing DB
    # still contains historical/current activation rows, this pre-activation
    # release publishes an empty index and preserves those rows only in the
    # canonical audit projection.
    index = build_activation_index_v2(
        (),
        as_of_utc=cutoff,
        canonical_projection_content_sha256=projection_sha,
    )
    index_by_scope = {
        tuple(entry["scope"][key] for key in (
            "station_id", "model_family", "lead_bucket", "run_cycle"
        )): entry
        for entry in index["entries"]
    }
    contract_by_model_hash: dict[str, CanonicalContract] = {}
    for contract in contracts:
        if _utc_z(contract.as_of_utc, "canonical_contract_as_of_utc") > cutoff:
            raise CanonicalActivationReleaseError("activation_release_contract_after_as_of")
        for model in contract.model_facts:
            prior = contract_by_model_hash.get(model.content_sha256)
            if prior is not None and prior.content_sha256 != contract.content_sha256:
                raise CanonicalActivationReleaseError("activation_release_model_contract_ambiguous")
            contract_by_model_hash[model.content_sha256] = contract

    stations_by_id = truth_gap_evidence.get("stations_by_id")
    source_evidence = truth_gap_evidence.get("source_evidence")
    if not isinstance(stations_by_id, Mapping) or not isinstance(source_evidence, Mapping):
        raise CanonicalActivationReleaseError("truth_gap_evidence_normalized_shape_invalid")

    records: list[dict[str, Any]] = []
    by_scope: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for model in models:
        station_gap = stations_by_id.get(model.station_id)
        if not isinstance(station_gap, Mapping):
            raise CanonicalActivationReleaseError("truth_gap_evidence_station_projection_missing")
        ready_dates = station_gap.get("ready_dates")
        blocked_dates = station_gap.get("blocked_dates")
        truth = truths.get((model.station_id, model.event_date))
        record_blocker: str | None = None
        truth_sha = ""
        if model.event_date in blocked_dates:
            record_blocker = DATA_MISSING
        elif model.event_date not in ready_dates:
            record_blocker = DATA_MISSING
        elif not truth or truth.get("valid") is not True:
            record_blocker = LINEAGE_MISMATCH
            truth_sha = str(truth.get("content_sha256") if truth else "")
        else:
            truth_fact = truth.get("truth")
            if not isinstance(truth_fact, CanonicalTruthFact):
                raise CanonicalActivationReleaseError("truth_gap_db_truth_shape_invalid")
            truth_sha = truth_fact.content_sha256
            if not truth_fact.closed_day or truth_fact.source_lane != "AWC":
                record_blocker = LINEAGE_MISMATCH
            elif truth_fact.settlement_timezone != model.settlement_timezone:
                record_blocker = LINEAGE_MISMATCH
        contract = contract_by_model_hash.get(model.content_sha256)
        identity = {
            "scope_type": MODEL_OOS_SCOPE,
            "station_id": model.station_id,
            "model_id": model.model_id or model.ensemble_system_id,
            "model_family": model.model_family,
            "provider_id": model.provider_id,
            "source_class": model.source_class,
            "run_cycle": model.run_cycle,
            "lead_bucket": model.lead_bucket,
            "event_date": model.event_date,
            "evidence_role": model.oos_role,
            "native_arrival_verified": model.native_arrival_indexed,
            "raw_object_sha256": model.raw_object_sha256,
            "model_fact_content_sha256": model.content_sha256,
            "truth_content_sha256": truth_sha,
            "canonical_contract_content_sha256": (
                contract.content_sha256 if contract is not None else ""
            ),
            "train_count": train_count,
            "holdout_count": holdout_count,
            "taf_revision_complete": taf_revision_complete,
            "state": OOS_READY if record_blocker is None else BLOCKED,
            "blockers": [] if record_blocker is None else [record_blocker],
            "activation_content_sha256": "",
        }
        record = _hashed(identity)
        records.append(record)
        by_scope[_scope(model)].append(record)

    builder_statuses: list[dict[str, Any]] = []
    binding_entries: list[dict[str, Any]] = []
    expected_samples = train_count + holdout_count
    for scope in sorted(by_scope):
        scoped_records = by_scope[scope]
        # Seed every model in the scope before collecting READY dates.  If one
        # model has zero truth-complete dates, omitting that empty set would
        # overstate the common cohort of the remaining models.
        dates_by_model: dict[str, set[str]] = {
            str(item["model_id"]): set() for item in scoped_records
        }
        non_data_blockers: list[str] = []
        for record in scoped_records:
            if record["state"] == OOS_READY:
                dates_by_model[str(record["model_id"])].add(str(record["event_date"]))
            else:
                blocker = str(record["blockers"][0])
                if blocker != DATA_MISSING:
                    non_data_blockers.append(blocker)
        common_dates = (
            set.intersection(*(set(value) for value in dates_by_model.values()))
            if dates_by_model
            else set()
        )
        index_entry = index_by_scope.get(scope)
        if non_data_blockers:
            blocker = sorted(non_data_blockers)[0]
        elif len(common_dates) < expected_samples:
            blocker = OOS_SAMPLE_SHORT
        elif index_entry is None:
            blocker = ACTIVATION_ROOT_MISSING
        elif index_entry.get("state") == BLOCKED:
            blocker = str(index_entry.get("blocker") or ACTIVATION_CURRENT_AMBIGUOUS)
        else:
            # Truth evidence cannot promote or bind a current activation.  The
            # full activation-contract path remains responsible for that step.
            blocker = ACTIVATION_CONTRACT_BINDING_MISSING
        record_hashes = sorted(str(item["content_sha256"]) for item in scoped_records)
        contract_hashes = sorted({
            str(item["canonical_contract_content_sha256"])
            for item in scoped_records
            if item["canonical_contract_content_sha256"]
        })
        status = _hashed(
            {
                "scope_type": MODEL_OOS_SCOPE,
                **_scope_mapping(scope),
                "builder_id": "canonical.truth_gap_projection.v1",
                "state": BLOCKED,
                "blockers": [blocker],
                "canonical_contract_content_sha256": (
                    contract_hashes[0] if len(contract_hashes) == 1 else ""
                ),
                "record_count": len(record_hashes),
                "record_set_sha256": content_sha256(record_hashes),
            }
        )
        builder_statuses.append(status)
        binding_entries.append(
            _hashed(
                {
                    "scope_type": MODEL_OOS_SCOPE,
                    **_scope_mapping(scope),
                    "state": BLOCKED,
                    "blocker": blocker,
                    "activation_candidates": (
                        index_entry.get("activation_candidates", [])
                        if isinstance(index_entry, Mapping) else []
                    ),
                    "builder_status_content_sha256": status["content_sha256"],
                    "record_count": len(record_hashes),
                    "record_set_sha256": content_sha256(record_hashes),
                }
            )
        )

    typed_records, typed_statuses, typed_by_type = _typed_scope_ledger_projection(
        typed_scope_envelopes
    )
    records.extend(typed_records)
    builder_statuses.extend(typed_statuses)
    records.sort(
        key=lambda item: (
            str(item.get("scope_type") or MODEL_OOS_SCOPE),
            str(item.get("station_id") or ""),
            str(item.get("model_id") or ""),
            str(item.get("model_family") or ""),
            str(item.get("run_cycle") or ""),
            str(item.get("lead_bucket") or ""),
            str(item.get("event_date") or ""),
            str(item.get("scope_kind") or ""),
            json.dumps(item.get("natural_key") or {}, sort_keys=True, separators=(",", ":")),
        )
    )
    builder_statuses.sort(
        key=lambda item: (
            str(item.get("scope_type") or MODEL_OOS_SCOPE),
            str(item.get("station_id") or ""),
            str(item.get("model_family") or ""),
            str(item.get("lead_bucket") or ""),
            str(item.get("run_cycle") or ""),
            str(item.get("scope_kind") or ""),
            json.dumps(item.get("natural_key") or {}, sort_keys=True, separators=(",", ":")),
        )
    )
    source_contracts = [
        *contract_evidence,
        dict(source_evidence),
        *[dict(item) for item in typed_scope_evidence],
    ]
    model_builder_blockers = sorted({
        str(blocker)
        for status in builder_statuses
        for blocker in status.get("blockers", [])
    })
    scope_type_statuses = [
        {
            "scope_type": MODEL_OOS_SCOPE,
            "state": BLOCKED if builder_statuses else "READY",
            "scope_count": len(builder_statuses),
            "blockers": model_builder_blockers,
        },
        *[
            dict(
                typed_by_type.get(
                    scope_type,
                    {
                        "scope_type": scope_type,
                        "state": BLOCKED,
                        "scope_count": 0,
                        "blockers": [ACTIVATION_ROOT_MISSING],
                    },
                )
            )
            for scope_type in REQUIRED_SCOPE_TYPES
            if scope_type != MODEL_OOS_SCOPE
        ],
    ]
    ledger = _hashed(
        {
            "schema": LEDGER_SCHEMA,
            "as_of_utc": cutoff,
            "canonical_projection_content_sha256": projection_sha,
            "source_contracts": source_contracts,
            "required_scope_types": list(REQUIRED_SCOPE_TYPES),
            "scope_type_statuses": scope_type_statuses,
            "typed_scope_coverage_complete": all(
                scope_type in typed_by_type for scope_type in TYPED_SCOPE_TYPES
            ),
            "summary": {
                "record_count": len(records),
                "activation_ready_count": 0,
                "oos_ready_count": sum(item["state"] == OOS_READY for item in records),
                "blocked_count": sum(item["state"] == BLOCKED for item in records),
                "data_ready_count": sum(item["state"] == DATA_READY for item in records),
                "candidate_only_count": sum(item["state"] == CANDIDATE_ONLY for item in records),
                "builder_scope_count": len(builder_statuses),
                "builder_blocked_count": len(builder_statuses),
            },
            "records": records,
            "builder_scope_statuses": builder_statuses,
            "future_data_counts_as_oos": False,
            "production_no_live": True,
        }
    )
    blockers = ["ACTIVATION_BINDING_SCOPE_BLOCKED", ACTIVATION_ROOT_MISSING]
    if any(
        status.get("state") == BLOCKED
        for status in scope_type_statuses
        if status.get("scope_type") != MODEL_OOS_SCOPE
    ):
        blockers.append(TYPED_SCOPE_BLOCKED)
    if not bool(inner_snapshot.get("boundary_valid")):
        blockers.append(INNER_OOS_SNAPSHOT_INVALID)
    if str(inner_snapshot.get("as_of_utc")) != cutoff:
        blockers.append(INNER_OOS_SNAPSHOT_AS_OF_MISMATCH)
    _extend_inner_snapshot_blockers(blockers, inner_snapshot)
    binding = _hashed(
        {
            "schema": BINDING_SCHEMA,
            "as_of_utc": cutoff,
            "state": BLOCKED,
            "blockers": sorted(set(blockers)),
            "canonical_projection_content_sha256": projection_sha,
            "ledger_content_sha256": ledger["content_sha256"],
            "activation_index_content_sha256": index["content_sha256"],
            "inner_oos_snapshot": dict(inner_snapshot),
            "entry_count": len(binding_entries),
            "entries": binding_entries,
            "production_no_live": True,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "writes_production_db": False,
            "writes_target_database": False,
        }
    )
    return ledger, index, binding


def build_blocked_audit_release(
    *,
    models: Sequence[CanonicalModelFact],
    activation_facts: Sequence[Mapping[str, Any]],
    truths: Mapping[tuple[str, str], Mapping[str, Any]],
    canonical_projection: Mapping[str, Any],
    contract_evidence: Sequence[Mapping[str, Any]],
    inner_snapshot: Mapping[str, Any],
    as_of_utc: str,
    train_count: int,
    holdout_count: int,
    taf_revision_complete: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Materialize invalid legacy truth as explicit blocked facts, never an adapter."""

    cutoff = _utc_z(as_of_utc, "activation_release_as_of_utc")
    projection_sha = _sha(canonical_projection.get("content_sha256"), "canonical_projection_content_sha256")
    index = build_activation_index_v2(
        activation_facts,
        as_of_utc=cutoff,
        canonical_projection_content_sha256=projection_sha,
    )
    records: list[dict[str, Any]] = []
    by_scope: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for model in models:
        truth = truths.get((model.station_id, model.event_date))
        truth_valid = bool(truth and truth.get("valid"))
        blocker = LINEAGE_MISMATCH if not truth_valid else ACTIVATION_CONTRACT_BINDING_MISSING
        identity = {
            "station_id": model.station_id,
            "model_id": model.model_id or model.ensemble_system_id,
            "model_family": model.model_family,
            "provider_id": model.provider_id,
            "source_class": model.source_class,
            "run_cycle": model.run_cycle,
            "lead_bucket": model.lead_bucket,
            "event_date": model.event_date,
            "evidence_role": model.oos_role,
            "native_arrival_verified": model.native_arrival_indexed,
            "raw_object_sha256": model.raw_object_sha256,
            "model_fact_content_sha256": model.content_sha256,
            "truth_content_sha256": str(truth.get("content_sha256") if truth else ""),
            "canonical_contract_content_sha256": "",
            "train_count": train_count,
            "holdout_count": holdout_count,
            "taf_revision_complete": taf_revision_complete,
            "state": BLOCKED,
            "blockers": [blocker],
            "activation_content_sha256": "",
        }
        record = _hashed(identity)
        records.append(record)
        by_scope[_scope(model)].append(record)
    builder_statuses: list[dict[str, Any]] = []
    binding_entries: list[dict[str, Any]] = []
    for scope in sorted(by_scope):
        scoped = sorted(item["content_sha256"] for item in by_scope[scope])
        status = _hashed(
            {
                **_scope_mapping(scope),
                "builder_id": "canonical.contract.invalid.blocked.audit.v1",
                "state": BLOCKED,
                "blockers": [LINEAGE_MISMATCH],
                "canonical_contract_content_sha256": "",
                "record_count": len(scoped),
                "record_set_sha256": content_sha256(scoped),
            }
        )
        builder_statuses.append(status)
        binding_entries.append(
            _hashed(
                {
                    **_scope_mapping(scope),
                    "state": BLOCKED,
                    "blocker": LINEAGE_MISMATCH,
                    "activation_candidates": [],
                    "builder_status_content_sha256": status["content_sha256"],
                    "record_count": len(scoped),
                    "record_set_sha256": content_sha256(scoped),
                }
            )
        )
    records.sort(key=lambda item: (
        item["station_id"], item["model_id"], item["model_family"],
        item["run_cycle"], item["lead_bucket"], item["event_date"],
    ))
    ledger = _hashed(
        {
            "schema": LEDGER_SCHEMA,
            "as_of_utc": cutoff,
            "canonical_projection_content_sha256": projection_sha,
            "source_contracts": list(contract_evidence),
            "summary": {
                "record_count": len(records),
                "activation_ready_count": 0,
                "oos_ready_count": 0,
                "blocked_count": len(records),
                "builder_scope_count": len(builder_statuses),
                "builder_blocked_count": len(builder_statuses),
            },
            "records": records,
            "builder_scope_statuses": builder_statuses,
            "future_data_counts_as_oos": False,
            "production_no_live": True,
        }
    )
    blockers = ["ACTIVATION_BINDING_SCOPE_BLOCKED", LINEAGE_MISMATCH]
    if not bool(inner_snapshot.get("boundary_valid")):
        blockers.append(INNER_OOS_SNAPSHOT_INVALID)
    if str(inner_snapshot.get("as_of_utc")) != cutoff:
        blockers.append(INNER_OOS_SNAPSHOT_AS_OF_MISMATCH)
    _extend_inner_snapshot_blockers(blockers, inner_snapshot)
    binding = _hashed(
        {
            "schema": BINDING_SCHEMA,
            "as_of_utc": cutoff,
            "state": BLOCKED,
            "blockers": sorted(set(blockers)),
            "canonical_projection_content_sha256": projection_sha,
            "ledger_content_sha256": ledger["content_sha256"],
            "activation_index_content_sha256": index["content_sha256"],
            "inner_oos_snapshot": dict(inner_snapshot),
            "entry_count": len(binding_entries),
            "entries": binding_entries,
            "production_no_live": True,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "writes_production_db": False,
            "writes_target_database": False,
        }
    )
    return ledger, index, binding


def verify_ledger_v2(value: Mapping[str, Any]) -> dict[str, Any]:
    supplied = _sha(value.get("content_sha256"), "ledger_v2_content_sha256")
    if supplied != content_sha256({key: child for key, child in value.items() if key != "content_sha256"}):
        raise CanonicalActivationReleaseError("ledger_v2_content_sha256_mismatch")
    if value.get("schema") != LEDGER_SCHEMA:
        raise CanonicalActivationReleaseError("ledger_v2_schema_invalid")
    records = value.get("records")
    statuses = value.get("builder_scope_statuses")
    if not isinstance(records, list) or not isinstance(statuses, list):
        raise CanonicalActivationReleaseError("ledger_v2_shape_invalid")
    record_scopes: set[tuple[str, ...]] = set()
    ready_scopes: set[tuple[str, str, str, str]] = set()
    state_counts = {
        ACTIVATION_READY: 0,
        OOS_READY: 0,
        DATA_READY: 0,
        CANDIDATE_ONLY: 0,
        BLOCKED: 0,
    }
    for record in records:
        if not isinstance(record, Mapping):
            raise CanonicalActivationReleaseError("ledger_v2_record_invalid")
        record_hash = _sha(record.get("content_sha256"), "ledger_v2_record_content_sha256")
        if record_hash != content_sha256({key: child for key, child in record.items() if key != "content_sha256"}):
            raise CanonicalActivationReleaseError("ledger_v2_record_content_sha256_mismatch")
        scope_type = str(record.get("scope_type") or MODEL_OOS_SCOPE)
        if scope_type == MODEL_OOS_SCOPE:
            key = tuple(str(record.get(field) or "") for field in (
                "station_id", "model_id", "model_family", "run_cycle", "lead_bucket", "event_date"
            ))
            if not all(key):
                raise CanonicalActivationReleaseError("ledger_v2_record_scope_invalid")
            unique_key = (scope_type, *key)
        else:
            typed_mapping = {
                "scope_type": scope_type,
                "scope_kind": record.get("scope_kind"),
                "natural_key": record.get("natural_key"),
                "as_of_utc": record.get("as_of_utc"),
                "state": record.get("state"),
                "blockers": record.get("blockers"),
                "evidence_content_sha256": record.get("evidence_content_sha256"),
                "authority_scope": record.get("authority_scope"),
                "content_sha256": record.get("typed_scope_fact_content_sha256"),
            }
            fact = CanonicalTypedScopeFact.from_mapping(typed_mapping)
            unique_key = (scope_type, fact.scope_kind, fact.scope_key)
            key = ("", "", "", "", "", "")
        if unique_key in record_scopes:
            raise CanonicalActivationReleaseError("ledger_v2_record_scope_invalid")
        record_scopes.add(unique_key)
        state = record.get("state")
        blockers = record.get("blockers")
        activation_sha = record.get("activation_content_sha256")
        if state not in state_counts or not isinstance(blockers, list):
            raise CanonicalActivationReleaseError("ledger_v2_record_state_invalid")
        state_counts[state] += 1
        if scope_type != MODEL_OOS_SCOPE:
            if state in {DATA_READY, OOS_READY, ACTIVATION_READY}:
                if blockers != []:
                    raise CanonicalActivationReleaseError("ledger_v2_typed_ready_semantics_invalid")
            elif state in {CANDIDATE_ONLY, BLOCKED}:
                if not isinstance(blockers, list) or not blockers:
                    raise CanonicalActivationReleaseError("ledger_v2_typed_blocked_semantics_invalid")
            continue
        if state == ACTIVATION_READY:
            if blockers or not isinstance(activation_sha, str):
                raise CanonicalActivationReleaseError("ledger_v2_ready_semantics_invalid")
            _sha(activation_sha, "ledger_v2_record_activation_sha256")
            ready_scopes.add((key[0], key[2], key[4], key[3]))
        elif state == OOS_READY:
            if blockers or activation_sha != "":
                raise CanonicalActivationReleaseError("ledger_v2_oos_semantics_invalid")
        elif len(blockers) != 1 or activation_sha != "":
            raise CanonicalActivationReleaseError("ledger_v2_blocked_semantics_invalid")
    status_scopes: set[tuple[str, ...]] = set()
    blocked_statuses = 0
    for status in statuses:
        if not isinstance(status, Mapping):
            raise CanonicalActivationReleaseError("ledger_v2_builder_status_invalid")
        status_hash = _sha(status.get("content_sha256"), "ledger_v2_builder_status_content_sha256")
        if status_hash != content_sha256({key: child for key, child in status.items() if key != "content_sha256"}):
            raise CanonicalActivationReleaseError("ledger_v2_builder_status_content_sha256_mismatch")
        scope_type = str(status.get("scope_type") or MODEL_OOS_SCOPE)
        if scope_type == MODEL_OOS_SCOPE:
            scope = tuple(str(status.get(field) or "") for field in (
                "station_id", "model_family", "lead_bucket", "run_cycle"
            ))
            scope_key = (scope_type, *scope)
            if not all(scope):
                raise CanonicalActivationReleaseError("ledger_v2_builder_scope_invalid")
        else:
            natural_key = status.get("natural_key")
            scope_kind = str(status.get("scope_kind") or "")
            if not isinstance(natural_key, Mapping) or not scope_kind:
                raise CanonicalActivationReleaseError("ledger_v2_builder_scope_invalid")
            scope_key = (
                scope_type,
                scope_kind,
                json.dumps(natural_key, sort_keys=True, separators=(",", ":")),
            )
            scope = ("", "", "", "")
        if scope_key in status_scopes:
            raise CanonicalActivationReleaseError("ledger_v2_builder_scope_invalid")
        status_scopes.add(scope_key)
        blockers = status.get("blockers")
        if status.get("state") == "BUILT":
            if blockers != []:
                raise CanonicalActivationReleaseError("ledger_v2_builder_built_semantics_invalid")
        elif status.get("state") == BLOCKED:
            blocked_statuses += 1
            if (
                not isinstance(blockers, list)
                or not blockers
                or (scope_type == MODEL_OOS_SCOPE and len(blockers) != 1)
            ):
                raise CanonicalActivationReleaseError("ledger_v2_builder_blocked_semantics_invalid")
            if scope_type == MODEL_OOS_SCOPE and scope in ready_scopes:
                raise CanonicalActivationReleaseError("ledger_v2_builder_record_contradiction")
        else:
            raise CanonicalActivationReleaseError("ledger_v2_builder_state_invalid")
    summary = value.get("summary")
    if not isinstance(summary, Mapping):
        raise CanonicalActivationReleaseError("ledger_v2_summary_invalid")
    expected_summary = {
        "record_count": len(records),
        "activation_ready_count": state_counts[ACTIVATION_READY],
        "oos_ready_count": state_counts[OOS_READY],
        "builder_scope_count": len(statuses),
        "builder_blocked_count": blocked_statuses,
    }
    for key, expected in expected_summary.items():
        if summary.get(key) != expected:
            raise CanonicalActivationReleaseError("ledger_v2_summary_mismatch")
    if "blocked_count" in summary and summary.get("blocked_count") != state_counts[BLOCKED]:
        raise CanonicalActivationReleaseError("ledger_v2_summary_mismatch")
    for key, state in (
        ("data_ready_count", DATA_READY),
        ("candidate_only_count", CANDIDATE_ONLY),
    ):
        if key in summary and summary.get(key) != state_counts[state]:
            raise CanonicalActivationReleaseError("ledger_v2_summary_mismatch")
    required_scope_types = value.get("required_scope_types")
    scope_type_statuses = value.get("scope_type_statuses")
    if required_scope_types is not None or scope_type_statuses is not None:
        if required_scope_types != list(REQUIRED_SCOPE_TYPES):
            raise CanonicalActivationReleaseError("ledger_v2_required_scope_types_invalid")
        if not isinstance(scope_type_statuses, list):
            raise CanonicalActivationReleaseError("ledger_v2_scope_type_statuses_invalid")
        status_types = [str(item.get("scope_type") or "") for item in scope_type_statuses if isinstance(item, Mapping)]
        if status_types != list(REQUIRED_SCOPE_TYPES):
            raise CanonicalActivationReleaseError("ledger_v2_scope_type_statuses_invalid")
        non_model_covered = all(
            isinstance(item, Mapping)
            and item.get("scope_type") == scope_type
            and isinstance(item.get("scope_count"), int)
            and item.get("scope_count") > 0
            for scope_type, item in zip(REQUIRED_SCOPE_TYPES[1:], scope_type_statuses[1:])
        )
        if value.get("typed_scope_coverage_complete") != non_model_covered:
            raise CanonicalActivationReleaseError("ledger_v2_typed_scope_coverage_mismatch")
    return dict(value)


def verify_binding_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    supplied = _sha(value.get("content_sha256"), "binding_content_sha256")
    if supplied != content_sha256({key: child for key, child in value.items() if key != "content_sha256"}):
        raise CanonicalActivationReleaseError("binding_content_sha256_mismatch")
    if value.get("schema") != BINDING_SCHEMA:
        raise CanonicalActivationReleaseError("binding_schema_invalid")
    entries = value.get("entries")
    if not isinstance(entries, list) or value.get("entry_count") != len(entries):
        raise CanonicalActivationReleaseError("binding_entries_invalid")
    any_blocked = False
    scopes: set[tuple[str, str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise CanonicalActivationReleaseError("binding_entry_invalid")
        entry_hash = _sha(entry.get("content_sha256"), "binding_entry_content_sha256")
        if entry_hash != content_sha256({key: child for key, child in entry.items() if key != "content_sha256"}):
            raise CanonicalActivationReleaseError("binding_entry_content_sha256_mismatch")
        scope = tuple(str(entry.get(field) or "") for field in (
            "station_id", "model_family", "lead_bucket", "run_cycle"
        ))
        if not all(scope) or scope in scopes:
            raise CanonicalActivationReleaseError("binding_scope_invalid")
        scopes.add(scope)  # type: ignore[arg-type]
        if entry.get("state") == BLOCKED:
            any_blocked = True
            if not isinstance(entry.get("blocker"), str) or not entry.get("blocker"):
                raise CanonicalActivationReleaseError("binding_blocker_invalid")
        elif entry.get("state") == "BOUND":
            _sha(entry.get("activation_content_sha256"), "binding_activation_content_sha256")
        else:
            raise CanonicalActivationReleaseError("binding_entry_state_invalid")
    global_blockers = value.get("blockers")
    if (
        not isinstance(global_blockers, list)
        or any(not isinstance(item, str) or not item for item in global_blockers)
        or global_blockers != sorted(set(global_blockers))
    ):
        raise CanonicalActivationReleaseError("binding_global_blockers_invalid")
    expected_state = BLOCKED if any_blocked or global_blockers else "BOUND"
    if value.get("state") != expected_state:
        raise CanonicalActivationReleaseError("binding_global_state_invalid")
    return dict(value)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_fsynced(path: Path, raw: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _leaf_identity(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    raw = path.read_bytes()
    supplied = _sha(value.get("content_sha256"), "release_leaf_content_sha256")
    if supplied != content_sha256({key: child for key, child in value.items() if key != "content_sha256"}):
        raise CanonicalActivationReleaseError("release_leaf_content_sha256_mismatch")
    return {
        "path": str(path.resolve()),
        "schema": value.get("schema"),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": supplied,
        "bytes": len(raw),
    }


def _current_content_sha(path: Path) -> str | None:
    if not path.exists():
        return None
    _source, _raw, value = _stable_json(path)
    return _sha(value.get("content_sha256"), "current_root_content_sha256")


def verify_current_root_pointer_v2(
    pointer: Mapping[str, Any],
    *,
    open_release: bool = True,
    expected_release_root: Path | str | None = None,
) -> dict[str, Any]:
    supplied = _sha(pointer.get("content_sha256"), "current_root_content_sha256")
    identity = {key: child for key, child in pointer.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise CanonicalActivationReleaseError("current_root_content_sha256_mismatch")
    if pointer.get("schema") != ROOT_POINTER_SCHEMA:
        raise CanonicalActivationReleaseError("current_root_schema_invalid")
    if pointer.get("production_no_live") is not True or pointer.get("orders_submitted") != 0:
        raise CanonicalActivationReleaseError("current_root_no_live_boundary_invalid")
    if pointer.get("calls_order_gateway") is not False or pointer.get("writes_production_db") is not False:
        raise CanonicalActivationReleaseError("current_root_write_boundary_invalid")
    if pointer.get("state") not in {BLOCKED, "VERIFIED_NO_LIVE"}:
        raise CanonicalActivationReleaseError("current_root_state_invalid")
    if not open_release:
        return dict(pointer)
    manifest_path = Path(str(pointer.get("release_manifest_path") or "")).expanduser()
    if expected_release_root is not None:
        release_root = Path(expected_release_root).expanduser().resolve()
        expected_manifest = (
            release_root
            / str(pointer.get("release_id") or "")
            / "release_manifest.json"
        ).resolve()
        if manifest_path.resolve() != expected_manifest:
            raise CanonicalActivationReleaseError("current_root_manifest_outside_release_root")
    source, raw, manifest = _stable_json(manifest_path)
    if str(source) != str(manifest_path.resolve()):
        raise CanonicalActivationReleaseError("current_root_manifest_path_invalid")
    if hashlib.sha256(raw).hexdigest() != pointer.get("release_manifest_file_sha256"):
        raise CanonicalActivationReleaseError("current_root_manifest_file_sha256_mismatch")
    if manifest.get("content_sha256") != pointer.get("release_manifest_content_sha256"):
        raise CanonicalActivationReleaseError("current_root_manifest_content_sha256_mismatch")
    if manifest.get("schema") != RELEASE_MANIFEST_SCHEMA:
        raise CanonicalActivationReleaseError("current_root_manifest_schema_invalid")
    if _sha(manifest.get("content_sha256"), "release_manifest_content_sha256") != content_sha256(
        {key: child for key, child in manifest.items() if key != "content_sha256"}
    ):
        raise CanonicalActivationReleaseError("release_manifest_content_sha256_mismatch")
    for name in ("ledger", "activation_index", "binding"):
        leaf = manifest.get("artifacts", {}).get(name)
        if not isinstance(leaf, Mapping):
            raise CanonicalActivationReleaseError("release_manifest_leaf_missing")
        leaf_source, leaf_raw, leaf_value = _stable_json(Path(str(leaf.get("path") or "")))
        if expected_release_root is not None:
            expected_leaf = (
                Path(expected_release_root).expanduser().resolve()
                / str(pointer.get("release_id") or "")
                / f"{name}.json"
            ).resolve()
            if leaf_source != expected_leaf:
                raise CanonicalActivationReleaseError("release_manifest_leaf_outside_release_root")
        if str(leaf_source) != str(Path(str(leaf.get("path"))).resolve()):
            raise CanonicalActivationReleaseError("release_manifest_leaf_path_invalid")
        if hashlib.sha256(leaf_raw).hexdigest() != leaf.get("file_sha256"):
            raise CanonicalActivationReleaseError("release_manifest_leaf_file_sha256_mismatch")
        if leaf_value.get("content_sha256") != leaf.get("content_sha256"):
            raise CanonicalActivationReleaseError("release_manifest_leaf_content_sha256_mismatch")
        if name == "ledger":
            verify_ledger_v2(leaf_value)
        elif name == "activation_index":
            verify_activation_index_v2(leaf_value)
        else:
            verify_binding_v1(leaf_value)
    return dict(pointer)


def verify_consumable_current_root_v2(
    pointer: Mapping[str, Any],
    *,
    expected_release_root: Path | str,
) -> dict[str, Any]:
    """Verify a current root for consumers, not merely for audit publication."""

    verified = verify_current_root_pointer_v2(
        pointer,
        open_release=True,
        expected_release_root=expected_release_root,
    )
    if verified.get("state") != "VERIFIED_NO_LIVE" or verified.get("blockers") != []:
        raise CanonicalActivationReleaseError("current_root_not_consumable")
    release_dir = (
        Path(expected_release_root).expanduser().resolve()
        / str(verified["release_id"])
    )
    ledger = load_bounded_json(release_dir / "ledger.json")
    index = load_bounded_json(release_dir / "activation_index.json")
    binding = load_bounded_json(release_dir / "binding.json")
    if not all(isinstance(value, Mapping) for value in (ledger, index, binding)):
        raise CanonicalActivationReleaseError("current_root_consumer_leaf_invalid")
    verify_ledger_v2(ledger)
    verify_activation_index_v2(index)
    verify_binding_v1(binding)
    if ledger.get("typed_scope_coverage_complete") is not True:
        raise CanonicalActivationReleaseError("current_root_typed_scope_coverage_incomplete")
    if index.get("state") != CURRENT or int(index.get("entry_count") or 0) <= 0:
        raise CanonicalActivationReleaseError("current_root_activation_index_not_consumable")
    if binding.get("state") != "BOUND" or binding.get("blockers") != []:
        raise CanonicalActivationReleaseError("current_root_binding_not_consumable")
    if (
        ledger.get("content_sha256") != verified.get("ledger_content_sha256")
        or index.get("content_sha256") != verified.get("activation_index_content_sha256")
        or binding.get("content_sha256") != verified.get("binding_content_sha256")
    ):
        raise CanonicalActivationReleaseError("current_root_consumer_hash_mismatch")
    return verified


def publish_release(
    *,
    ledger: Mapping[str, Any],
    activation_index: Mapping[str, Any],
    binding: Mapping[str, Any],
    canonical_projection: Mapping[str, Any],
    contract_evidence: Sequence[Mapping[str, Any]],
    release_root: Path | str,
    current_pointer: Path | str,
    expected_current_content_sha256: str | None,
) -> dict[str, Any]:
    """Publish immutable leaves, then atomically replace the one current root."""

    verified_ledger = verify_ledger_v2(ledger)
    verified_index = verify_activation_index_v2(activation_index)
    verified_binding = verify_binding_v1(binding)
    if verified_binding.get("ledger_content_sha256") != verified_ledger.get("content_sha256"):
        raise CanonicalActivationReleaseError("publish_release_ledger_binding_mismatch")
    if verified_binding.get("activation_index_content_sha256") != verified_index.get("content_sha256"):
        raise CanonicalActivationReleaseError("publish_release_index_binding_mismatch")
    root = Path(release_root).expanduser().resolve()
    current = Path(current_pointer).expanduser().resolve()
    if current.parent == root or root in current.parents:
        raise CanonicalActivationReleaseError("current_root_must_be_outside_immutable_release_root")
    root.mkdir(parents=True, exist_ok=True)
    current.parent.mkdir(parents=True, exist_ok=True)
    expected = (
        None if expected_current_content_sha256 is None
        else _sha(expected_current_content_sha256, "expected_current_content_sha256")
    )
    if _current_content_sha(current) != expected:
        raise CanonicalActivationReleaseError("current_root_compare_and_swap_mismatch")
    leaf_values = {
        "ledger": dict(ledger),
        "activation_index": dict(activation_index),
        "binding": dict(binding),
    }
    leaf_bytes = {name: bounded_json_bytes(value) for name, value in leaf_values.items()}
    release_identity = {
        "ledger_content_sha256": ledger["content_sha256"],
        "activation_index_content_sha256": activation_index["content_sha256"],
        "binding_content_sha256": binding["content_sha256"],
        "canonical_projection_content_sha256": canonical_projection["content_sha256"],
    }
    release_id = content_sha256(release_identity)
    release_dir = root / release_id
    staging = root / f".{release_id}.tmp.{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        if not release_dir.exists():
            staging.mkdir(mode=0o700)
            for name, raw in leaf_bytes.items():
                _write_fsynced(staging / f"{name}.json", raw)
            artifacts = {}
            for name in leaf_values:
                leaf = _leaf_identity(staging / f"{name}.json", leaf_values[name])
                leaf["path"] = str((release_dir / f"{name}.json").resolve())
                artifacts[name] = leaf
            manifest_identity = {
                "schema": RELEASE_MANIFEST_SCHEMA,
                "release_id": release_id,
                "as_of_utc": ledger["as_of_utc"],
                "state": binding["state"],
                "canonical_projection": dict(canonical_projection),
                "source_contracts": list(contract_evidence),
                "artifacts": artifacts,
                "production_no_live": True,
                "calls_order_gateway": False,
                "orders_submitted": 0,
                "writes_production_db": False,
                "writes_target_database": False,
            }
            manifest = _hashed(manifest_identity)
            _write_fsynced(staging / "release_manifest.json", bounded_json_bytes(manifest))
            _fsync_directory(staging)
            os.rename(staging, release_dir)
            _fsync_directory(root)
        # Re-open every immutable leaf; an existing release id is accepted
        # only when its exact bytes match this deterministic build.
        for name, raw in leaf_bytes.items():
            path = release_dir / f"{name}.json"
            if not path.is_file() or path.read_bytes() != raw:
                raise CanonicalActivationReleaseError("immutable_release_content_conflict")
        manifest_path = release_dir / "release_manifest.json"
        _manifest_source, manifest_raw, manifest = _stable_json(manifest_path)
        # Paths in a staging-built manifest must point at the immutable final
        # directory.  Rebuild it once with final leaf paths before publication.
        artifacts = {
            name: _leaf_identity(release_dir / f"{name}.json", leaf_values[name])
            for name in leaf_values
        }
        final_manifest_identity = {
            "schema": RELEASE_MANIFEST_SCHEMA,
            "release_id": release_id,
            "as_of_utc": ledger["as_of_utc"],
            "state": binding["state"],
            "canonical_projection": dict(canonical_projection),
            "source_contracts": list(contract_evidence),
            "artifacts": artifacts,
            "production_no_live": True,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "writes_production_db": False,
            "writes_target_database": False,
        }
        final_manifest = _hashed(final_manifest_identity)
        final_manifest_bytes = bounded_json_bytes(final_manifest)
        if manifest_raw != final_manifest_bytes:
            # The immutable manifest cannot be patched after directory rename.
            # Build final paths in staging by using the deterministic future
            # release path; this branch protects older/incomplete attempts.
            raise CanonicalActivationReleaseError("immutable_release_manifest_path_conflict")
        manifest = final_manifest
        if _current_content_sha(current) != expected:
            raise CanonicalActivationReleaseError("current_root_compare_and_swap_mismatch")
        pointer_identity = {
            "schema": ROOT_POINTER_SCHEMA,
            "pointer_version": "2.0.0",
            "state": BLOCKED if binding["state"] == BLOCKED else "VERIFIED_NO_LIVE",
            "blockers": list(binding.get("blockers") or []),
            "release_id": release_id,
            "release_manifest_path": str(manifest_path),
            "release_manifest_file_sha256": hashlib.sha256(final_manifest_bytes).hexdigest(),
            "release_manifest_content_sha256": manifest["content_sha256"],
            "ledger_path": artifacts["ledger"]["path"],
            "ledger_schema": ledger["schema"],
            "ledger_file_sha256": artifacts["ledger"]["file_sha256"],
            "ledger_content_sha256": ledger["content_sha256"],
            "record_count": ledger["summary"]["record_count"],
            "effective_as_of_utc": ledger["as_of_utc"],
            "activation_index_path": artifacts["activation_index"]["path"],
            "activation_index_file_sha256": artifacts["activation_index"]["file_sha256"],
            "activation_index_content_sha256": activation_index["content_sha256"],
            "binding_path": artifacts["binding"]["path"],
            "binding_file_sha256": artifacts["binding"]["file_sha256"],
            "binding_content_sha256": binding["content_sha256"],
            "inner_oos_snapshot_content_sha256": binding["inner_oos_snapshot"]["content_sha256"],
            "canonical_projection_content_sha256": canonical_projection["content_sha256"],
            "production_no_live": True,
            "formal_p0_eligible": False,
            "formal_live_for_republisher": False,
            "future_data_counts_as_oos": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "writes_production_db": False,
            "writes_target_database": False,
        }
        pointer = _hashed(pointer_identity)
        verify_current_root_pointer_v2(pointer, open_release=False)
        temporary = current.with_name(f".{current.name}.tmp.{os.getpid()}")
        try:
            _write_fsynced(temporary, bounded_json_bytes(pointer))
            _source, _raw, staged_pointer = _stable_json(temporary)
            verify_current_root_pointer_v2(staged_pointer, open_release=True)
            if _current_content_sha(current) != expected:
                raise CanonicalActivationReleaseError("current_root_compare_and_swap_mismatch")
            os.replace(temporary, current)
            try:
                _fsync_directory(current.parent)
            except OSError as exc:
                raise CanonicalActivationReleaseError("current_root_publish_outcome_uncertain") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return verify_current_root_pointer_v2(pointer, open_release=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


__all__ = [
    "ACTIVATION_INDEX_SCHEMA",
    "BINDING_SCHEMA",
    "CanonicalActivationReleaseError",
    "LEDGER_SCHEMA",
    "RELEASE_MANIFEST_SCHEMA",
    "ROOT_POINTER_SCHEMA",
    "audit_contract_inputs",
    "build_activation_index_v2",
    "build_blocked_audit_release",
    "build_release_artifacts",
    "load_contracts",
    "load_db_activation_release_snapshot",
    "load_db_audit_snapshot",
    "load_db_projection",
    "load_db_supersession_facts",
    "load_inner_snapshot",
    "load_typed_scope_envelopes",
    "publish_release",
    "verify_activation_index_v2",
    "verify_binding_v1",
    "verify_consumable_current_root_v2",
    "verify_current_root_pointer_v2",
    "verify_ledger_v2",
    "verify_projection_snapshot_consistency",
    "verify_supersession_projection_consistency",
]
