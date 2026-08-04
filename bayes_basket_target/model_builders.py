"""One builder registry: exactly one builder per lead × model family.

The registry is intentionally small.  A scope without an implemented
builder is represented as ``BLOCKED``; it is never silently routed to another
lead or another family.  This prevents d0/d1/d2 samples or global/regional
lineages from being mixed merely because a similarly shaped packet exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from .canonical_contract import (
    MODEL_FAMILIES,
    SCHEMA as CANONICAL_CONTRACT_SCHEMA,
    CanonicalContract,
    CanonicalModelFact,
)
from .contracts import ContractError, content_sha256


# Historical strict-OOS is the only evidence role accepted by these model
# builders.  Forward observations are useful for arrival/interface audits,
# but they never become training or holdout evidence here.
HISTORICAL_OOS_ROLE = "historical_strict_oos"
OBSERVATIONAL_ROLE = "observational_candidate_not_oos"
NATIVE_SOURCE_CLASS = "native_official"
REPUBLISHER_SOURCE_CLASS = "aggregator_republisher"
MIN_TRAIN_COUNT = 20
MIN_HOLDOUT_COUNT = 8
GLOBAL_MODEL_IDS = frozenset({"ECMWF", "GFS", "ICON", "GEM", "CMA"})
BUILDER_RESULT_SCHEMA = "bayes_basket_target_builder_result_v1"
AUDIT_SCHEMA = "bayes_basket_target_model_builder_audit_v1"
BUILDER_EVIDENCE_SCHEMA = "bayes_basket_target_builder_evidence_v1"
BUILDER_EVIDENCE_FIELDS = frozenset(
    {"schema", "canonical_contract", "rows", "source_pointer", "content_sha256"}
)
BUILDER_ROW_FIELDS = frozenset(
    {
        "station_id", "model_id", "ensemble_system_id", "model_family",
        "provider_id", "source_class", "event_date", "run_init_utc",
        "lead_bucket", "run_cycle", "settlement_timezone", "forecast_value",
        "raw_object_sha256", "content_sha256", "oos_role", "closed_day",
        "strict_asof", "native_arrival_indexed", "p1_eligible",
        "formal_live_authority", "published_at_utc", "received_at_utc",
        "train_count", "holdout_count", "independence_family_id",
    }
)

# Keep the explicit ingress surface narrow.  In particular, a caller cannot
# pass an arbitrary ``model``/``source`` object and rely on field guessing.
_ALLOWED_EVIDENCE_ARGS = frozenset(
    {
        "strict_oos_evidence",
        "canonical_contract",
        "source_pointer",
    }
)


LEADS = ("d0", "d1", "d2")
Builder = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class BuilderSpec:
    lead_bucket: str
    model_family: str
    builder_id: str
    builder: Builder | None
    state: str = "READY"


_REGISTRY: dict[tuple[str, str], BuilderSpec] = {}


def register_builder(
    *, lead_bucket: str, model_family: str, builder_id: str, builder: Builder | None, state: str = "READY"
) -> BuilderSpec:
    lead = str(lead_bucket).strip().lower()
    family = str(model_family).strip()
    if lead not in LEADS:
        raise ContractError("builder_lead_invalid")
    if family not in MODEL_FAMILIES:
        raise ContractError("builder_model_family_invalid")
    key = (lead, family)
    if key in _REGISTRY:
        raise ContractError(f"builder_duplicate_scope:{lead}:{family}")
    if state not in {"READY", "BLOCKED"}:
        raise ContractError("builder_state_invalid")
    if state == "READY" and builder is None:
        raise ContractError("builder_ready_callable_missing")
    spec = BuilderSpec(lead, family, str(builder_id).strip(), builder, state)
    if not spec.builder_id:
        raise ContractError("builder_id_missing")
    _REGISTRY[key] = spec
    return spec


def builder_for(lead_bucket: str, model_family: str) -> BuilderSpec:
    key = (str(lead_bucket).strip().lower(), str(model_family).strip())
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise ContractError(f"builder_scope_unregistered:{key[0]}:{key[1]}") from exc


def registered_builders() -> tuple[BuilderSpec, ...]:
    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))


def build_scope(*, lead_bucket: str, model_family: str, **kwargs: Any) -> Mapping[str, Any]:
    spec = builder_for(lead_bucket, model_family)
    if spec.state != "READY" or spec.builder is None:
        return _blocked_scope_result(
            lead_bucket=spec.lead_bucket,
            model_family=spec.model_family,
            builder_id=spec.builder_id,
            blockers=("BUILDER_NOT_IMPLEMENTED",),
        )
    result = dict(spec.builder(**kwargs))
    if result.get("writes_target_database") or result.get("writes_production_database"):
        raise ContractError("builder_direct_database_write_forbidden")
    if result.get("calls_order_gateway") or result.get("orders_submitted"):
        raise ContractError("builder_order_gateway_forbidden")
    result.setdefault("builder_id", spec.builder_id)
    result.setdefault("lead_bucket", spec.lead_bucket)
    result.setdefault("model_family", spec.model_family)
    return result


def build_all_scopes(
    *,
    scope_inputs: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    **common_kwargs: Any,
) -> tuple[Mapping[str, Any], ...]:
    """Run every registered scope without routing around a blocked scope.

    Each lead/family pair owns exactly one builder.  A blocked scope remains
    blocked in the returned tuple; no other builder is allowed to substitute
    its data or OOS cohort.
    """

    provided = scope_inputs or {}
    unknown = sorted(set(provided) - set(_REGISTRY))
    if unknown:
        raise ContractError(
            "builder_scope_input_unregistered:"
            + ",".join(f"{lead}:{family}" for lead, family in unknown)
        )
    results: list[Mapping[str, Any]] = []
    for lead, family in sorted(_REGISTRY):
        scoped = provided.get((lead, family), {})
        if not isinstance(scoped, Mapping):
            raise ContractError(f"builder_scope_input_mapping_required:{lead}:{family}")
        params = dict(common_kwargs)
        params.update(scoped)
        results.append(build_scope(lead_bucket=lead, model_family=family, **params))
    return tuple(results)


def _blocked_scope_result(
    *,
    lead_bucket: str,
    model_family: str,
    builder_id: str,
    blockers: Iterable[str],
    source_classes: Iterable[str] = (),
    oos_role: str | None = None,
    train_count: int = 0,
    holdout_count: int = 0,
    error: str | None = None,
    source_pointer: str | None = None,
) -> dict[str, Any]:
    """Create a stable, no-write audit for a blocked builder scope."""

    blocker_values = tuple(sorted({str(item).strip() for item in blockers if str(item).strip()}))
    classes = tuple(sorted({str(item).strip() for item in source_classes if str(item).strip()}))
    identity = {
        "builder_id": builder_id,
        "lead_bucket": lead_bucket,
        "model_family": model_family,
        "source_classes": list(classes),
        "oos_role": oos_role or "",
        "train_count": max(0, int(train_count)),
        "holdout_count": max(0, int(holdout_count)),
        "blockers": list(blocker_values),
        "source_pointer": str(source_pointer or "").strip(),
    }
    contract_hash = content_sha256(identity)
    audit = {
        "schema": AUDIT_SCHEMA,
        "builder_id": builder_id,
        "scope": {"lead_bucket": lead_bucket, "model_family": model_family},
        "source_class": classes[0] if len(classes) == 1 else None,
        "source_classes": list(classes),
        "oos_role": oos_role,
        "train_count": max(0, int(train_count)),
        "holdout_count": max(0, int(holdout_count)),
        "blockers": list(blocker_values),
        "contract_sha256": contract_hash,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    if error:
        audit["error"] = str(error)
    return {
        "schema": BUILDER_RESULT_SCHEMA,
        "state": "BLOCKED",
        "builder_id": builder_id,
        "lead_bucket": lead_bucket,
        "model_family": model_family,
        "blocker": blocker_values[0] if blocker_values else "BUILDER_INPUT_INVALID",
        "blockers": list(blocker_values),
        "source_class": classes[0] if len(classes) == 1 else None,
        "source_classes": list(classes),
        "oos_role": oos_role,
        "train_count": max(0, int(train_count)),
        "holdout_count": max(0, int(holdout_count)),
        "contract_sha256": contract_hash,
        "audit": audit,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


def _built_scope_result(
    *,
    lead_bucket: str,
    model_family: str,
    builder_id: str,
    source_class: str,
    train_count: int,
    holdout_count: int,
    contract_hash: str,
    model_ids: Sequence[str],
    source_pointer: str | None = None,
    canonical_contract: Mapping[str, Any] | None = None,
    evidence_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    classes = [source_class]
    audit = {
        "schema": AUDIT_SCHEMA,
        "builder_id": builder_id,
        "scope": {"lead_bucket": lead_bucket, "model_family": model_family},
        "source_class": source_class,
        "source_classes": classes,
        "oos_role": HISTORICAL_OOS_ROLE,
        "train_count": int(train_count),
        "holdout_count": int(holdout_count),
        "blockers": [],
        "contract_sha256": contract_hash,
        "model_ids": list(model_ids),
        "source_pointer": str(source_pointer or "").strip(),
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    result: dict[str, Any] = {
        "schema": BUILDER_RESULT_SCHEMA,
        "state": "BUILT",
        "builder_id": builder_id,
        "lead_bucket": lead_bucket,
        "model_family": model_family,
        "source_class": source_class,
        "source_classes": classes,
        "oos_role": HISTORICAL_OOS_ROLE,
        "train_count": int(train_count),
        "holdout_count": int(holdout_count),
        "contract_sha256": contract_hash,
        "model_ids": list(model_ids),
        "evidence_count": len(evidence_rows),
        # Keep the same shape as the deterministic builder.  A raw audit
        # packet without canonical facts is still read-only evidence and is
        # deliberately marked ``None`` so ``ingest_built_scope`` blocks it
        # instead of inventing a second ingress path.
        "canonical_contract": None,
        "audit": audit,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    if canonical_contract is not None:
        result["canonical_contract"] = dict(canonical_contract)
        from .gap_ledger import gap_records_from_canonical_contract

        result["gap_records"] = list(
            gap_records_from_canonical_contract(
                canonical_contract,
                train_count=int(train_count),
                holdout_count=int(holdout_count),
                # TAF revision evidence is a separate scope; recording the
                # explicit false value keeps the gap visible without treating
                # model evidence as a TAF substitute.
                taf_revision_complete=False,
            )
        )
    return result


def _read_only_db_evidence(db: Any) -> Mapping[str, Any]:
    """Read canonical facts from an explicitly supplied DB, never write it."""

    owned = False
    conn: sqlite3.Connection
    if isinstance(db, sqlite3.Connection):
        conn = db
    elif isinstance(db, (str, Path)):
        path = Path(db).expanduser().resolve()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        owned = True
    else:
        raise ContractError("builder_db_connection_invalid")
    try:
        conn.execute("PRAGMA query_only=ON")
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_ssot_fact'"
        ).fetchone()
        if table is None:
            raise ContractError("builder_canonical_ssot_fact_table_missing")
        rows = conn.execute(
            "SELECT fact_type,fact_json FROM canonical_ssot_fact "
            "WHERE fact_type IN ('model','truth') ORDER BY fact_type,scope_key"
        ).fetchall()
        model_facts: list[dict[str, Any]] = []
        truth_facts: list[dict[str, Any]] = []
        for fact_type, fact_json in rows:
            try:
                value = json.loads(str(fact_json))
            except (TypeError, ValueError) as exc:
                raise ContractError("builder_canonical_fact_json_invalid") from exc
            if not isinstance(value, Mapping):
                raise ContractError("builder_canonical_fact_mapping_required")
            (model_facts if fact_type == "model" else truth_facts).append(dict(value))
        return {"model_facts": model_facts, "truth_facts": truth_facts, "source_pointer": str(db)}
    finally:
        if owned:
            conn.close()


def _resolve_evidence(kwargs: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any] | None]:
    unknown = sorted(set(kwargs) - _ALLOWED_EVIDENCE_ARGS)
    if unknown:
        raise ContractError(f"historical_builder_unknown_argument:{','.join(unknown)}")
    explicit = [key for key in ("strict_oos_evidence", "canonical_contract", "db") if kwargs.get(key) is not None]
    if len(explicit) > 1:
        raise ContractError("historical_builder_multiple_evidence_sources")
    if not explicit:
        return None, None
    key = explicit[0]
    value = kwargs[key]
    if isinstance(value, CanonicalContract):
        packet = value.to_mapping()
        return value, packet
    if isinstance(value, Mapping):
        return value, value
    if key != "strict_oos_evidence" or not isinstance(value, Mapping):
        raise ContractError("historical_builder_evidence_mapping_required")
    return value, value


def _packet_rows(packet: Any) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None, CanonicalContract | None]:
    if isinstance(packet, CanonicalContract):
        return [item.to_mapping() for item in packet.model_facts], packet.to_mapping(), packet
    if not isinstance(packet, Mapping):
        raise ContractError("historical_builder_evidence_mapping_required")
    if packet.get("schema") == CANONICAL_CONTRACT_SCHEMA:
        try:
            contract = CanonicalContract.from_mapping(packet)
        except ContractError:
            raise
        return [item.to_mapping() for item in contract.model_facts], packet, contract
    if packet.get("schema") != BUILDER_EVIDENCE_SCHEMA:
        raise ContractError("historical_builder_evidence_schema_invalid")
    unknown = sorted(set(packet) - BUILDER_EVIDENCE_FIELDS)
    missing = sorted(BUILDER_EVIDENCE_FIELDS - set(packet))
    if unknown:
        raise ContractError(f"historical_builder_evidence_unknown_field:{','.join(unknown)}")
    if missing:
        raise ContractError(f"historical_builder_evidence_missing_field:{','.join(missing)}")
    supplied_hash = packet.get("content_sha256")
    identity = {key: packet[key] for key in packet if key != "content_sha256"}
    if not isinstance(supplied_hash, str) or supplied_hash != content_sha256(identity):
        raise ContractError("historical_builder_evidence_content_sha256_mismatch")
    contract_payload = packet.get("canonical_contract")
    if not isinstance(contract_payload, Mapping):
        raise ContractError("historical_builder_evidence_canonical_contract_missing")
    contract = CanonicalContract.from_mapping(contract_payload)
    rows = packet.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise ContractError("historical_builder_evidence_rows_required")
    contract_models = {item.content_sha256: item for item in contract.model_facts}
    normalized: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ContractError("historical_builder_evidence_row_invalid")
        row_unknown = sorted(set(row) - BUILDER_ROW_FIELDS)
        row_missing = sorted(BUILDER_ROW_FIELDS - set(row))
        if row_unknown:
            raise ContractError(f"historical_builder_evidence_row_unknown_field:{','.join(row_unknown)}")
        if row_missing:
            raise ContractError(f"historical_builder_evidence_row_missing_field:{','.join(row_missing)}")
        model_payload = {key: row[key] for key in BUILDER_ROW_FIELDS if key not in {"train_count", "holdout_count", "independence_family_id"}}
        model = CanonicalModelFact.from_mapping(model_payload)
        if model.content_sha256 not in contract_models:
            raise ContractError("historical_builder_evidence_model_not_in_contract")
        if model.to_mapping() != contract_models[model.content_sha256].to_mapping():
            raise ContractError("historical_builder_evidence_model_content_mismatch")
        normalized.append(dict(row))
    return normalized, packet, contract


def _value(row: Mapping[str, Any], packet: Mapping[str, Any] | None, key: str) -> Any:
    # Builder evidence rows are already canonical.  Packet-level fallback
    # would silently broadcast one station/model value to another row.
    return row[key] if key in row else None


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _count(row: Mapping[str, Any], packet: Mapping[str, Any] | None, key: str) -> tuple[int | None, bool]:
    value = _value(row, packet, key)
    if value is None:
        return None, False
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, True
    return int(value), False


def _hash_complete(row: Mapping[str, Any], packet: Mapping[str, Any] | None) -> bool:
    raw_hash = _value(row, packet, "raw_object_sha256")
    content_hash = _value(row, packet, "content_sha256")
    if not isinstance(raw_hash, str) or not isinstance(content_hash, str):
        return False
    return (
        len(raw_hash) == 64
        and len(content_hash) == 64
        and all(char in "0123456789abcdefABCDEF" for char in raw_hash + content_hash)
    )


def _canonical_truth_context(contract: CanonicalContract | None) -> dict[tuple[str, str], Mapping[str, Any]]:
    if contract is None:
        return {}
    return {
        (item.station_id, item.event_date): item.to_mapping()
        for item in contract.truth_facts
    }


def _try_build_canonical_contract(**_: Any) -> Mapping[str, Any] | None:
    """No implicit adapter exists for non-canonical family evidence."""

    return None


def _historical_family_builder(
    *,
    lead_bucket: str,
    model_family: str,
    builder_id: str,
    **kwargs: Any,
) -> Mapping[str, Any]:
    """Validate one independent model-family historical OOS scope.

    This is intentionally a validator/packet builder, not a downloader or a
    database writer.  Native and republisher evidence are separate runs; a
    mixed packet is blocked instead of silently normalising its lineage.
    """

    try:
        source, packet = _resolve_evidence(kwargs)
        if source is None:
            return _blocked_scope_result(
                lead_bucket=lead_bucket,
                model_family=model_family,
                builder_id=builder_id,
                blockers=("DATA_MISSING",),
                source_pointer=kwargs.get("source_pointer"),
            )
        rows, packet_mapping, canonical = _packet_rows(source)
    except ContractError as exc:
        return _blocked_scope_result(
            lead_bucket=lead_bucket,
            model_family=model_family,
            builder_id=builder_id,
            blockers=("DATA_MISSING", "CONTRACT_INVALID"),
            error=str(exc),
            source_pointer=kwargs.get("source_pointer"),
        )

    if not rows:
        return _blocked_scope_result(
            lead_bucket=lead_bucket,
            model_family=model_family,
            builder_id=builder_id,
            blockers=("DATA_MISSING",),
            source_pointer=kwargs.get("source_pointer") or (packet_mapping or {}).get("source_pointer"),
        )

    blockers: set[str] = set()
    source_classes: set[str] = set()
    roles: set[str] = set()
    model_ids: list[str] = []
    train_counts: list[int] = []
    holdout_counts: list[int] = []
    seen_ids: set[str] = set()
    truth_context = _canonical_truth_context(canonical)

    for row in rows:
        family = _text(_value(row, packet_mapping, "model_family"))
        if not family:
            blockers.add("DATA_MISSING")
        elif family != model_family:
            blockers.add("LINEAGE_MISMATCH")
        lead = _text(_value(row, packet_mapping, "lead_bucket"))
        if not lead:
            blockers.add("DATA_MISSING")
        elif lead != lead_bucket:
            blockers.add("LINEAGE_MISMATCH")

        source_class = _text(_value(row, packet_mapping, "source_class"))
        if source_class not in {NATIVE_SOURCE_CLASS, REPUBLISHER_SOURCE_CLASS}:
            blockers.add("LINEAGE_MISMATCH")
        else:
            source_classes.add(source_class)
        role = _text(_value(row, packet_mapping, "oos_role"))
        if not role:
            blockers.add("DATA_MISSING")
        else:
            roles.add(role)
            if role != HISTORICAL_OOS_ROLE:
                # This explicit blocker is intentionally not collapsed into
                # sample-short: a forward row is never an OOS sample.
                blockers.add("FUTURE_NOT_OOS")

        run_cycle = _text(_value(row, packet_mapping, "run_cycle"))
        if not run_cycle:
            blockers.add("RUN_CYCLE_MISMATCH")

        station = _text(_value(row, packet_mapping, "station_id")).upper()
        event_date = _text(_value(row, packet_mapping, "event_date"))
        if not station or not event_date:
            blockers.add("DATA_MISSING")
        truth = truth_context.get((station, event_date))
        closed = _value(row, packet_mapping, "closed_day")
        if closed is None and truth is not None:
            closed = truth.get("closed_day")
        if closed is not True:
            blockers.add("TRUTH_NOT_CLOSED")
        strict = _value(row, packet_mapping, "strict_asof")
        if strict is not True:
            blockers.add("TRUTH_NOT_CLOSED")
        truth_lane = _text(truth.get("source_lane")) if truth is not None else ""
        if truth_lane != "AWC":
            blockers.add("LINEAGE_MISMATCH")
        if truth is None or not isinstance(truth.get("truth_revision_no"), int):
            blockers.add("TRUTH_REVISION_STALE")

        if not _hash_complete(row, packet_mapping):
            blockers.add("HASH_MISMATCH")

        identifier = (
            _text(_value(row, packet_mapping, "ensemble_system_id"))
            if model_family == "global_ensemble"
            else _text(_value(row, packet_mapping, "model_id"))
        )
        if not identifier:
            blockers.add("DATA_MISSING")
        else:
            identifier = identifier.upper()
            if model_family == "regional" and identifier in GLOBAL_MODEL_IDS:
                blockers.add("LINEAGE_MISMATCH")
            if identifier in seen_ids and model_family == "regional":
                blockers.add("LINEAGE_MISMATCH")
            seen_ids.add(identifier)
            model_ids.append(identifier)

        if source_class == REPUBLISHER_SOURCE_CLASS:
            for key in ("native_arrival_indexed", "p1_eligible", "formal_live_authority"):
                if _value(row, packet_mapping, key) is True:
                    blockers.add("LINEAGE_MISMATCH")
        elif source_class == NATIVE_SOURCE_CLASS:
            if _value(row, packet_mapping, "native_arrival_indexed") is not True:
                blockers.add("LINEAGE_MISMATCH")

        train, train_invalid = _count(row, packet_mapping, "train_count")
        holdout, holdout_invalid = _count(row, packet_mapping, "holdout_count")
        if train_invalid or holdout_invalid:
            blockers.add("OOS_SAMPLE_SHORT")
        if train is None or holdout is None:
            blockers.add("OOS_SAMPLE_SHORT")
        else:
            train_counts.append(train)
            holdout_counts.append(holdout)

    if len(source_classes) > 1:
        blockers.add("LINEAGE_MISMATCH")
    if len(roles) > 1:
        blockers.add("LINEAGE_MISMATCH")
    train_count = min(train_counts) if train_counts else 0
    holdout_count = min(holdout_counts) if holdout_counts else 0
    if train_count < MIN_TRAIN_COUNT or holdout_count < MIN_HOLDOUT_COUNT:
        blockers.add("OOS_SAMPLE_SHORT")

    if model_family == "global_ensemble":
        if len(set(model_ids)) < 2:
            blockers.add("INDEPENDENT_FAMILY_SHORT")
        explicit_families = {
            _text(_value(row, packet_mapping, "independence_family_id"))
            for row in rows
            if _text(_value(row, packet_mapping, "independence_family_id"))
        }
        if explicit_families and len(explicit_families) < 2:
            blockers.add("INDEPENDENT_FAMILY_SHORT")
    elif model_family == "regional" and any(identifier in GLOBAL_MODEL_IDS for identifier in model_ids):
        blockers.add("LINEAGE_MISMATCH")

    source_pointer = kwargs.get("source_pointer") or (packet_mapping or {}).get("source_pointer")
    if blockers:
        return _blocked_scope_result(
            lead_bucket=lead_bucket,
            model_family=model_family,
            builder_id=builder_id,
            blockers=blockers,
            source_classes=source_classes,
            oos_role=next(iter(roles), None),
            train_count=train_count,
            holdout_count=holdout_count,
            source_pointer=source_pointer,
        )

    if canonical is not None:
        canonical_packet: Mapping[str, Any] | None = canonical.to_mapping()
        contract_hash = canonical.content_sha256
    else:
        canonical_packet = _try_build_canonical_contract(
            rows=rows,
            packet=packet_mapping,
            lead_bucket=lead_bucket,
            model_family=model_family,
            builder_id=builder_id,
            source_class=next(iter(source_classes)),
            as_of_utc=kwargs.get("as_of_utc"),
        )
        contract_hash = (
            str(canonical_packet["content_sha256"])
            if canonical_packet is not None
            else content_sha256(
                {
                    "lead_bucket": lead_bucket,
                    "model_family": model_family,
                    "source_class": next(iter(source_classes)),
                    "oos_role": HISTORICAL_OOS_ROLE,
                    "rows": [dict(row) for row in rows],
                }
            )
        )
    return _built_scope_result(
        lead_bucket=lead_bucket,
        model_family=model_family,
        builder_id=builder_id,
        source_class=next(iter(source_classes)),
        train_count=train_count,
        holdout_count=holdout_count,
        contract_hash=contract_hash,
        model_ids=tuple(sorted(set(model_ids))),
        source_pointer=source_pointer,
        canonical_contract=canonical_packet,
        evidence_rows=rows,
    )


def _load_d1_global_ensemble_builder(**kwargs: Any) -> Mapping[str, Any]:
    return _historical_family_builder(
        lead_bucket="d1",
        model_family="global_ensemble",
        builder_id="d1.global_ensemble.historical_strict.v1",
        **kwargs,
    )


def _load_d0_global_ensemble_builder(**kwargs: Any) -> Mapping[str, Any]:
    """Build d0 only from governed historical ensemble-member contracts."""

    from .historical_scope_builders import build_d0_global_ensemble

    db = kwargs.pop("db")
    stations = kwargs.pop("stations")
    as_of_utc = kwargs.pop("as_of_utc")
    generation = kwargs.pop("generation")
    if kwargs:
        raise ContractError("d0_global_ensemble_builder_unknown_argument")
    packet, receipt = build_d0_global_ensemble(
        db=db,
        stations=list(stations),
        as_of_utc=str(as_of_utc),
        generation=int(generation),
    )
    if packet is None:
        blockers = sorted({
            str(blocker)
            for status in receipt.get("station_statuses") or []
            for blocker in status.get("blockers") or []
        }) or ["DATA_MISSING"]
        return _blocked_scope_result(
            lead_bucket="d0",
            model_family="global_ensemble",
            builder_id="d0.global_ensemble.historical_strict.v1",
            blockers=blockers,
            source_classes=(REPUBLISHER_SOURCE_CLASS,),
            oos_role=HISTORICAL_OOS_ROLE,
            train_count=int(receipt.get("train_count") or 0),
            holdout_count=int(receipt.get("holdout_count") or 0),
        ) | {"receipt": receipt}
    result = _built_scope_result(
        lead_bucket="d0",
        model_family="global_ensemble",
        builder_id="d0.global_ensemble.historical_strict.v1",
        source_class=REPUBLISHER_SOURCE_CLASS,
        train_count=int(receipt["train_count"]),
        holdout_count=int(receipt["holdout_count"]),
        contract_hash=str(packet["content_sha256"]),
        model_ids=tuple(sorted({
            str(fact["ensemble_system_id"])
            for fact in packet["model_facts"]
        })),
        canonical_contract=packet,
        evidence_rows=packet["model_facts"],
    )
    return {**result, "receipt": receipt}


def _load_d2_global_deterministic_builder(**kwargs: Any) -> Mapping[str, Any]:
    """Build d2 only from the exact PREVIOUS_DAY2 five-model history."""

    from .historical_scope_builders import build_d2_global_deterministic

    db = kwargs.pop("db")
    stations = kwargs.pop("stations")
    as_of_utc = kwargs.pop("as_of_utc")
    generation = kwargs.pop("generation")
    if kwargs:
        raise ContractError("d2_global_deterministic_builder_unknown_argument")
    packet, receipt = build_d2_global_deterministic(
        db=db,
        stations=list(stations),
        as_of_utc=str(as_of_utc),
        generation=int(generation),
    )
    if packet is None:
        blockers = sorted({
            str(blocker)
            for status in receipt.get("station_statuses") or []
            for blocker in status.get("blockers") or []
        }) or ["DATA_MISSING"]
        return _blocked_scope_result(
            lead_bucket="d2",
            model_family="global_deterministic",
            builder_id="d2.global_deterministic.historical_strict.v1",
            blockers=blockers,
            source_classes=(REPUBLISHER_SOURCE_CLASS,),
            oos_role=HISTORICAL_OOS_ROLE,
            train_count=int(receipt.get("train_count") or 0),
            holdout_count=int(receipt.get("holdout_count") or 0),
        ) | {"receipt": receipt}
    result = _built_scope_result(
        lead_bucket="d2",
        model_family="global_deterministic",
        builder_id="d2.global_deterministic.historical_strict.v1",
        source_class=REPUBLISHER_SOURCE_CLASS,
        train_count=int(receipt["train_count"]),
        holdout_count=int(receipt["holdout_count"]),
        contract_hash=str(packet["content_sha256"]),
        model_ids=tuple(sorted({str(fact["model_id"]) for fact in packet["model_facts"]})),
        canonical_contract=packet,
        evidence_rows=packet["model_facts"],
    )
    return {**result, "receipt": receipt}


def _load_d1_regional_builder(**kwargs: Any) -> Mapping[str, Any]:
    return _historical_family_builder(
        lead_bucket="d1",
        model_family="regional",
        builder_id="d1.regional.historical_strict.v1",
        **kwargs,
    )


def ingest_built_scope(
    conn: Any,
    *,
    lead_bucket: str,
    model_family: str,
    commit: bool = False,
    **kwargs: Any,
) -> Mapping[str, Any]:
    """Build then pass the *only* accepted contract to the canonical ingress.

    Builders may emit legacy audit projections for inspection, but a scope is
    ingestible only when it also supplies an exact ``canonical_contract``.
    There is no fallback adapter, field aliasing, or direct SQL path here.
    """

    result = dict(build_scope(lead_bucket=lead_bucket, model_family=model_family, **kwargs))
    if result.get("state") != "BUILT":
        return result
    packet = result.get("canonical_contract")
    if not isinstance(packet, Mapping):
        return {
            **result,
            "state": "BLOCKED",
            "blocker": "CANONICAL_CONTRACT_MISSING",
            "writes_target_database": False,
            "calls_order_gateway": False,
        }
    # The import is intentionally lazy: canonical_ingress already depends on
    # the materialization stack and must not be imported for registry reads.
    from .canonical_ingress import ingest_canonical_contract

    contract = CanonicalContract.from_mapping(packet)
    receipt = ingest_canonical_contract(conn, contract, commit=commit)
    return {**result, "ingress": receipt, "state": "INGRESSED" if commit else "VALIDATED"}


def _blocked_builder(*, lead_bucket: str, model_family: str, **_: Any) -> Mapping[str, Any]:
    return {
        "schema": "bayes_basket_target_builder_result_v1",
        "state": "BLOCKED",
        "lead_bucket": lead_bucket,
        "model_family": model_family,
        "blocker": "BUILDER_NOT_IMPLEMENTED",
        "writes_target_database": False,
        "calls_order_gateway": False,
    }


def _load_d1_builder(**kwargs: Any) -> Mapping[str, Any]:
    from .d1_canonical_builder import build_d1_canonical_residual_contract

    db = kwargs.pop("db")
    stations = kwargs.pop("stations")
    as_of_utc = kwargs.pop("as_of_utc")
    # Residual construction is prior to activation generation.  Keep the
    # registry call shape stable but reject any attempt to use generation as
    # permission to mint a legacy activation.
    kwargs.pop("generation", None)
    if kwargs:
        raise ContractError("d1_builder_unknown_argument")
    canonical_contract, receipt = build_d1_canonical_residual_contract(
        db=db,
        stations=list(stations),
        as_of_utc=as_of_utc,
    )
    result = {
        "schema": "bayes_basket_target_builder_result_v1",
        "state": "BUILT",
        "receipt": receipt,
        "canonical_contract": canonical_contract,
        "writes_target_database": False,
        "calls_order_gateway": False,
    }
    # No activation exists until one exact station has 20+8.  Projecting these
    # partial residuals as OOS_READY would erase the station-level sample gap,
    # so the sole readiness output is the receipt's explicit station statuses.
    result["gap_records"] = []
    return result


def _install_defaults() -> None:
    for lead in LEADS:
        for family in MODEL_FAMILIES:
            if lead == "d0" and family == "global_ensemble":
                register_builder(
                    lead_bucket=lead,
                    model_family=family,
                    builder_id="d0.global_ensemble.historical_strict.v1",
                    builder=_load_d0_global_ensemble_builder,
                )
            elif lead == "d1" and family == "global_deterministic":
                register_builder(
                    lead_bucket=lead,
                    model_family=family,
                    builder_id="d1.global_deterministic.historical_strict.v1",
                    builder=_load_d1_builder,
                )
            elif lead == "d1" and family == "global_ensemble":
                register_builder(
                    lead_bucket=lead,
                    model_family=family,
                    builder_id="d1.global_ensemble.historical_strict.v1",
                    builder=_load_d1_global_ensemble_builder,
                )
            elif lead == "d1" and family == "regional":
                register_builder(
                    lead_bucket=lead,
                    model_family=family,
                    builder_id="d1.regional.historical_strict.v1",
                    builder=_load_d1_regional_builder,
                )
            elif lead == "d2" and family == "global_deterministic":
                register_builder(
                    lead_bucket=lead,
                    model_family=family,
                    builder_id="d2.global_deterministic.historical_strict.v1",
                    builder=_load_d2_global_deterministic_builder,
                )
            else:
                register_builder(
                    lead_bucket=lead,
                    model_family=family,
                    builder_id=f"{lead}.{family}.historical_strict.blocked.v1",
                    builder=lambda _lead=lead, _family=family, **kwargs: _blocked_builder(
                        lead_bucket=_lead, model_family=_family, **kwargs
                    ),
                    state="BLOCKED",
                )


_install_defaults()


__all__ = [
    "AUDIT_SCHEMA",
    "BuilderSpec",
    "BUILDER_RESULT_SCHEMA",
    "HISTORICAL_OOS_ROLE",
    "LEADS",
    "MIN_HOLDOUT_COUNT",
    "MIN_TRAIN_COUNT",
    "NATIVE_SOURCE_CLASS",
    "REPUBLISHER_SOURCE_CLASS",
    "build_all_scopes",
    "build_scope",
    "builder_for",
    "ingest_built_scope",
    "register_builder",
    "registered_builders",
]
