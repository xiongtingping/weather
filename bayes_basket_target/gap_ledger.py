"""One canonical gap ledger for model/data/OOS readiness.

The ledger grain is fixed to
``station × model × model_family × run_cycle × lead × event_date``.
It is deliberately a diagnostic record: no row can promote an artifact or
block production merely because a future observation is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

from .contracts import ContractError, content_sha256
from .canonical_contract import MODEL_FAMILIES


SCHEMA = "bayes_basket_target_canonical_gap_ledger_v1"
EVIDENCE_ROLES = ("historical_strict_oos", "observational_candidate_not_oos")
BLOCKERS = (
    "DATA_MISSING",
    "RUN_CYCLE_MISMATCH",
    "TRUTH_NOT_CLOSED",
    "TRUTH_REVISION_STALE",
    "LINEAGE_MISMATCH",
    "HASH_MISMATCH",
    "TIMEZONE_MISMATCH",
    "OOS_SAMPLE_SHORT",
    "ACTIVATION_ROOT_MISSING",
)
SCOPE_STATUS_FIELDS = frozenset(
    {
        "station_id",
        "lead_bucket",
        "model_family",
        "builder_id",
        "state",
        "blockers",
        "source_pointer",
        "content_sha256",
    }
)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"gap_{field}_type_invalid")
    result = value.strip()
    if not result:
        raise ContractError(f"gap_{field}_missing")
    return result


def _date(value: Any, field: str) -> str:
    result = _text(value, field)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ContractError(f"gap_{field}_invalid") from exc


@dataclass(frozen=True)
class GapRecord:
    station_id: str
    model_id: str
    model_family: str
    run_cycle: str
    lead_bucket: str
    event_date: str
    evidence_role: str
    native_arrival_verified: bool
    raw_receipt_hash_complete: bool
    awc_truth_closed: bool
    taf_revision_complete: bool
    train_count: int
    holdout_count: int
    state: str
    blockers: tuple[str, ...]
    evidence: tuple[str, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "GapRecord":
        fields = {
            "station_id", "model_id", "model_family", "run_cycle", "lead_bucket",
            "event_date", "evidence_role", "native_arrival_verified",
            "raw_receipt_hash_complete", "awc_truth_closed", "taf_revision_complete",
            "train_count", "holdout_count", "state", "blockers", "evidence",
            "content_sha256",
        }
        unknown = sorted(set(raw) - fields)
        missing = sorted(fields - set(raw))
        if unknown:
            raise ContractError(f"gap_record_unknown_field:{','.join(unknown)}")
        if missing:
            raise ContractError(f"gap_record_missing_field:{','.join(missing)}")
        role = _text(raw.get("evidence_role"), "evidence_role")
        if role not in EVIDENCE_ROLES:
            raise ContractError("gap_evidence_role_invalid")
        model_family = _text(raw.get("model_family"), "model_family")
        if model_family not in MODEL_FAMILIES:
            raise ContractError("gap_model_family_invalid")
        lead_bucket = _text(raw.get("lead_bucket"), "lead_bucket")
        if lead_bucket not in {"d0", "d1", "d2"}:
            raise ContractError("gap_lead_bucket_invalid")
        bools = {}
        for key in ("native_arrival_verified", "raw_receipt_hash_complete", "awc_truth_closed", "taf_revision_complete"):
            if not isinstance(raw.get(key), bool):
                raise ContractError(f"gap_{key}_must_be_bool")
            bools[key] = bool(raw[key])
        blockers = tuple(sorted({_text(item, "blocker") for item in raw.get("blockers") or []}))
        invalid = sorted(set(blockers) - set(BLOCKERS))
        if invalid:
            raise ContractError(f"gap_blocker_invalid:{','.join(invalid)}")
        counts: dict[str, int] = {}
        for key in ("train_count", "holdout_count"):
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"gap_{key}_invalid")
            counts[key] = value
        evidence = tuple(sorted({_text(item, "evidence") for item in raw.get("evidence") or []}))
        identity = {
            key: value
            for key, value in raw.items()
            if key != "content_sha256"
        }
        supplied = _text(raw.get("content_sha256"), "content_sha256").lower()
        if len(supplied) != 64 or supplied != content_sha256(identity):
            raise ContractError("gap_record_content_sha256_mismatch")
        return cls(
            station_id=_text(raw.get("station_id"), "station_id").upper(),
            model_id=_text(raw.get("model_id"), "model_id").upper(),
            model_family=model_family,
            run_cycle=_text(raw.get("run_cycle"), "run_cycle"),
            lead_bucket=lead_bucket,
            event_date=_date(raw.get("event_date"), "event_date"),
            evidence_role=role,
            native_arrival_verified=bools["native_arrival_verified"],
            raw_receipt_hash_complete=bools["raw_receipt_hash_complete"],
            awc_truth_closed=bools["awc_truth_closed"],
            taf_revision_complete=bools["taf_revision_complete"],
            train_count=counts["train_count"],
            holdout_count=counts["holdout_count"],
            state=_text(raw.get("state"), "state"),
            blockers=blockers,
            evidence=evidence,
            content_sha256=supplied,
        )

    def to_mapping(self) -> dict[str, Any]:
        identity = {
            "station_id": self.station_id,
            "model_id": self.model_id,
            "model_family": self.model_family,
            "run_cycle": self.run_cycle,
            "lead_bucket": self.lead_bucket,
            "event_date": self.event_date,
            "evidence_role": self.evidence_role,
            "native_arrival_verified": self.native_arrival_verified,
            "raw_receipt_hash_complete": self.raw_receipt_hash_complete,
            "awc_truth_closed": self.awc_truth_closed,
            "taf_revision_complete": self.taf_revision_complete,
            "train_count": self.train_count,
            "holdout_count": self.holdout_count,
            "state": self.state,
            "blockers": list(self.blockers),
            "evidence": list(self.evidence),
        }
        return {**identity, "content_sha256": content_sha256(identity)}

    @property
    def scope_key(self) -> str:
        return "|".join((self.station_id, self.model_id, self.model_family, self.run_cycle, self.lead_bucket, self.event_date))


@dataclass(frozen=True)
class BuilderScopeStatus:
    """A no-fact scope status; it never fabricates a model/event row."""

    station_id: str
    lead_bucket: str
    model_family: str
    builder_id: str
    state: str
    blockers: tuple[str, ...]
    source_pointer: str
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BuilderScopeStatus":
        unknown = sorted(set(raw) - SCOPE_STATUS_FIELDS)
        missing = sorted(SCOPE_STATUS_FIELDS - set(raw))
        if unknown:
            raise ContractError(f"gap_scope_status_unknown_field:{','.join(unknown)}")
        if missing:
            raise ContractError(f"gap_scope_status_missing_field:{','.join(missing)}")
        station = _text(raw.get("station_id"), "scope_status_station_id").upper()
        lead = _text(raw.get("lead_bucket"), "scope_status_lead_bucket")
        if lead not in {"d0", "d1", "d2"}:
            raise ContractError("gap_scope_status_lead_bucket_invalid")
        family = _text(raw.get("model_family"), "scope_status_model_family")
        if family not in MODEL_FAMILIES:
            raise ContractError("gap_scope_status_model_family_invalid")
        state = _text(raw.get("state"), "scope_status_state")
        builder = _text(raw.get("builder_id"), "scope_status_builder_id")
        pointer = _text(raw.get("source_pointer"), "scope_status_source_pointer")
        blockers_raw = raw.get("blockers")
        if not isinstance(blockers_raw, list) or any(not isinstance(item, str) or not item.strip() for item in blockers_raw):
            raise ContractError("gap_scope_status_blockers_invalid")
        blockers = tuple(sorted(set(item.strip() for item in blockers_raw)))
        identity = {
            "station_id": station,
            "lead_bucket": lead,
            "model_family": family,
            "builder_id": builder,
            "state": state,
            "blockers": list(blockers),
            "source_pointer": pointer,
        }
        supplied = _text(raw.get("content_sha256"), "scope_status_content_sha256").lower()
        if len(supplied) != 64 or supplied != content_sha256(identity):
            raise ContractError("gap_scope_status_content_sha256_mismatch")
        return cls(station, lead, family, builder, state, blockers, pointer, supplied)

    @property
    def scope_key(self) -> str:
        return "|".join((self.station_id, self.lead_bucket, self.model_family))

    def to_mapping(self) -> dict[str, Any]:
        identity = {
            "station_id": self.station_id,
            "lead_bucket": self.lead_bucket,
            "model_family": self.model_family,
            "builder_id": self.builder_id,
            "state": self.state,
            "blockers": list(self.blockers),
            "source_pointer": self.source_pointer,
        }
        return {**identity, "content_sha256": content_sha256(identity)}


class GapLedger:
    """Immutable in-memory ledger that serializes to one content-addressed JSON."""

    def __init__(
        self,
        records: Iterable[GapRecord],
        *,
        as_of_utc: str,
        source_pointers: Mapping[str, str] | None = None,
        builder_scope_statuses: Iterable[BuilderScopeStatus | Mapping[str, Any]] = (),
    ) -> None:
        self.as_of_utc = _text(as_of_utc, "as_of_utc")
        try:
            parsed_as_of = datetime.fromisoformat(self.as_of_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("gap_as_of_utc_invalid") from exc
        if parsed_as_of.tzinfo is None or parsed_as_of.utcoffset() is None:
            raise ContractError("gap_as_of_utc_timezone_required")
        self.as_of_utc = parsed_as_of.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.source_pointers = {str(key): _text(value, "source_pointer") for key, value in (source_pointers or {}).items()}
        self._records: dict[str, GapRecord] = {}
        for record in records:
            if record.scope_key in self._records:
                raise ContractError(f"gap_scope_duplicate:{record.scope_key}")
            self._records[record.scope_key] = record
        self._scope_statuses: dict[str, BuilderScopeStatus] = {}
        for raw in builder_scope_statuses:
            status = raw if isinstance(raw, BuilderScopeStatus) else BuilderScopeStatus.from_mapping(raw)
            if status.scope_key in self._scope_statuses:
                raise ContractError(f"gap_scope_status_duplicate:{status.scope_key}")
            self._scope_statuses[status.scope_key] = status

    @property
    def records(self) -> tuple[GapRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    @property
    def builder_scope_statuses(self) -> tuple[BuilderScopeStatus, ...]:
        return tuple(self._scope_statuses[key] for key in sorted(self._scope_statuses))

    def summary(self) -> dict[str, Any]:
        rows = self.records
        statuses = self.builder_scope_statuses
        return {
            "rows": len(rows),
            "historical_strict_oos_rows": sum(item.evidence_role == "historical_strict_oos" for item in rows),
            "observational_candidate_not_oos_rows": sum(item.evidence_role == "observational_candidate_not_oos" for item in rows),
            "states": {state: sum(item.state == state for item in rows) for state in sorted({item.state for item in rows})},
            "blockers": {blocker: sum(blocker in item.blockers for item in rows) for blocker in BLOCKERS},
            "builder_scope_status_rows": len(statuses),
            "builder_scope_blocked_rows": sum(item.state == "BLOCKED" for item in statuses),
            "builder_scope_blockers": {
                blocker: sum(blocker in item.blockers for item in statuses)
                for blocker in sorted({blocker for item in statuses for blocker in item.blockers})
            },
        }

    def to_mapping(self) -> dict[str, Any]:
        identity = {
            "schema": SCHEMA,
            "as_of_utc": self.as_of_utc,
            "source_pointers": dict(sorted(self.source_pointers.items())),
            "summary": self.summary(),
            "records": [item.to_mapping() for item in self.records],
            "builder_scope_statuses": [item.to_mapping() for item in self.builder_scope_statuses],
            "future_not_oos": True,
        }
        return {**identity, "content_sha256": content_sha256(identity)}


def build_gap_ledger(
    records: Iterable[Mapping[str, Any] | GapRecord],
    *,
    as_of_utc: str,
    source_pointers: Mapping[str, str] | None = None,
    builder_scope_statuses: Iterable[BuilderScopeStatus | Mapping[str, Any]] = (),
) -> dict[str, Any]:
    parsed = [item if isinstance(item, GapRecord) else GapRecord.from_mapping(item) for item in records]
    return GapLedger(
        parsed,
        as_of_utc=as_of_utc,
        source_pointers=source_pointers,
        builder_scope_statuses=builder_scope_statuses,
    ).to_mapping()


def gap_records_from_canonical_contract(
    contract: Mapping[str, Any],
    *,
    train_count: int,
    holdout_count: int,
    taf_revision_complete: bool,
) -> tuple[dict[str, Any], ...]:
    """Project one validated canonical contract into ledger rows.

    This is a projection only: it does not infer missing dates, models or
    lineage.  The canonical contract must already validate, and each emitted
    row points back to the exact model/truth hashes consumed by that scope.
    """

    from .canonical_contract import CanonicalContract

    parsed = CanonicalContract.from_mapping(contract)
    if isinstance(train_count, bool) or not isinstance(train_count, int) or train_count < 0:
        raise ContractError("gap_projection_train_count_invalid")
    if isinstance(holdout_count, bool) or not isinstance(holdout_count, int) or holdout_count < 0:
        raise ContractError("gap_projection_holdout_count_invalid")
    if not isinstance(taf_revision_complete, bool):
        raise ContractError("gap_projection_taf_revision_complete_must_be_bool")
    truths = {(item.station_id, item.event_date): item for item in parsed.truth_facts}
    activations = {
        (item.station_id, item.model_family, item.lead_bucket, item.run_cycle)
        for item in parsed.activation_facts
    }
    rows: list[dict[str, Any]] = []
    for model in parsed.model_facts:
        identifier = model.model_id or model.ensemble_system_id
        truth = truths.get((model.station_id, model.event_date))
        if truth is None:
            raise ContractError("gap_projection_truth_reference_missing")
        identity = {
            "station_id": model.station_id,
            "model_id": identifier,
            "model_family": model.model_family,
            "run_cycle": model.run_cycle,
            "lead_bucket": model.lead_bucket,
            "event_date": model.event_date,
            "evidence_role": model.oos_role,
            "native_arrival_verified": model.native_arrival_indexed,
            "raw_receipt_hash_complete": True,
            "awc_truth_closed": truth.closed_day,
            "taf_revision_complete": taf_revision_complete,
            "train_count": train_count,
            "holdout_count": holdout_count,
            "state": (
                "ACTIVATION_READY"
                if (model.station_id, model.model_family, model.lead_bucket, model.run_cycle) in activations
                else "OOS_READY"
            ),
            "blockers": [],
            "evidence": [
                f"model:{model.content_sha256}",
                f"truth:{truth.content_sha256}",
            ],
        }
        rows.append({**identity, "content_sha256": content_sha256(identity)})
    return tuple(rows)


__all__ = [
    "BLOCKERS",
    "EVIDENCE_ROLES",
    "GapLedger",
    "GapRecord",
    "BuilderScopeStatus",
    "SCHEMA",
    "build_gap_ledger",
    "gap_records_from_canonical_contract",
]
