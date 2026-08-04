"""唯一的模型/真值/激活 canonical contract。

这个模块刻意不做字段猜测。上游旧 packet 若要进入 SSOT，必须先经过
``canonical_ingress`` 中显式命名的 adapter；canonical contract 本身不接受
``target_local_date``、``model_run_initialization_utc`` 等别名。

未来日可以作为到达/接口观测保存，但永远不能伪装成 strict OOS。Open-Meteo
lineage 也必须显式标记为 ``aggregator_republisher``，不能写入 native arrival、
P1 或 formal/live authority。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_canonical_contract_v4"
CONTRACT_VERSION = 4
EVIDENCE_ROLES = ("historical_strict_oos", "observational_candidate_not_oos")
SOURCE_CLASSES = ("native_official", "aggregator_republisher")
MODEL_FAMILIES = ("global_deterministic", "global_ensemble", "regional")
AUTHORITY_SCOPES = ("PRODUCTION_NO_LIVE_ONLY", "FORMAL_NATIVE_ELIGIBLE")
FORMAL_AWC_TRUTH_ROLE = "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1"
HISTORICAL_AWC_REPLAY_TRUTH_ROLE = "AWC_HISTORICAL_REPLAY_INGRESS"
TRUTH_SOURCE_ROLES = (
    FORMAL_AWC_TRUTH_ROLE,
    HISTORICAL_AWC_REPLAY_TRUTH_ROLE,
)

MODEL_FACT_FIELDS = frozenset(
    {
        "station_id",
        "model_id",
        "ensemble_system_id",
        "model_family",
        "provider_id",
        "source_class",
        "event_date",
        "run_init_utc",
        "lead_bucket",
        "run_cycle",
        "settlement_timezone",
        "forecast_value",
        "raw_object_sha256",
        "content_sha256",
        "oos_role",
        "closed_day",
        "strict_asof",
        "native_arrival_indexed",
        "p1_eligible",
        "formal_live_authority",
        "published_at_utc",
        "received_at_utc",
    }
)
ENSEMBLE_MEMBER_FACT_FIELDS = frozenset(
    {
        "station_id",
        "ensemble_system_id",
        "model_family",
        "provider_id",
        "source_class",
        "event_date",
        "run_init_utc",
        "lead_bucket",
        "run_cycle",
        "member_id",
        "member_role",
        "extraction_version",
        "settlement_timezone",
        "forecast_value",
        # This is a hash of the extraction receipts, not the provider raw
        # object.  GEFS/GEPS historical rows previously called this a
        # ``source_object_sha`` even though it is a receipt-set hash.  The
        # canonical member fact must not preserve that false equivalence.
        "raw_receipt_set_sha256",
        # The immutable decoded source-member fact that this extraction was
        # derived from.  It is deliberately separate from this fact's own
        # content hash.
        "source_member_fact_sha",
        "archive_query_at_known",
        "archive_query_at_utc",
        "oos_role",
        "closed_day",
        "strict_asof",
        "content_sha256",
    }
)
NATIVE_ARRIVAL_FACT_FIELDS = frozenset(
    {
        "native_arrival_id",
        "ensemble_system_id",
        "provider_id",
        "source_class",
        "native_object_id",
        "run_init_utc",
        "lead_bucket",
        "run_cycle",
        # A native object hash is valid only on the independent official
        # arrival fact.  Some receipts establish an arrival before the object
        # can be retained, therefore an unindexed arrival may leave this
        # field explicitly empty rather than borrow a receipt-set SHA.
        "raw_object_sha256",
        "published_at_known",
        "published_at_utc",
        "received_at_known",
        "received_at_utc",
        "native_arrival_indexed",
        "content_sha256",
    }
)
ENSEMBLE_MEMBER_ARRIVAL_BINDING_FACT_FIELDS = frozenset(
    {
        "ensemble_member_content_sha256",
        "native_arrival_content_sha256",
        "receipt_manifest_content_sha256",
        "manifest_entry_ordinals",
        "content_sha256",
    }
)
ENSEMBLE_MEMBER_RECEIPT_REF_FIELDS = frozenset(
    {
        "ordinal",
        "native_object_id",
        "raw_object_sha256",
        "receipt_evidence_at_utc",
        "receipt_evidence_content_sha256",
        "extraction_selector_sha256",
    }
)
ENSEMBLE_MEMBER_RECEIPT_MANIFEST_FACT_FIELDS = frozenset(
    {
        "ensemble_member_content_sha256",
        "source_raw_receipt_set_sha256",
        "required_receipt_refs",
        "required_receipt_ref_set_sha256",
        "content_sha256",
    }
)
TRUTH_FACT_FIELDS = frozenset(
    {
        "truth_id",
        "station_id",
        "event_date",
        "truth_revision_no",
        "truth_value",
        "truth_content_sha256",
        "canonical_committed_at_utc",
        "station_day_end_utc",
        "closed_day",
        "source_lane",
        "source_role",
        "authority_scope",
        "settlement_timezone",
        "content_sha256",
    }
)
RESIDUAL_FACT_FIELDS = frozenset(
    {
        "residual_id",
        "truth_id",
        "model_fact_content_sha256",
        "station_id",
        "model_id",
        "event_date",
        "lead_bucket",
        "run_cycle",
        "residual_value",
        "paired_asof_utc",
        "content_sha256",
        "oos_role",
    }
)
ACTIVATION_FACT_FIELDS = frozenset(
    {
        "activation_id",
        "station_id",
        "model_family",
        "lead_bucket",
        "run_cycle",
        "candidate_models",
        "selected_models",
        "selected_weights",
        "available_model_set_hash",
        "selection_evidence_sha256",
        "artifact_content_sha256",
        "activation_generation",
        "activation_content_sha256",
        "authority_scope",
        "oos_role",
    }
)
TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "contract_version",
        "as_of_utc",
        "builder_id",
        "model_facts",
        "ensemble_member_facts",
        "native_arrival_facts",
        "ensemble_member_receipt_manifest_facts",
        "ensemble_member_arrival_binding_facts",
        "truth_facts",
        "residual_facts",
        "activation_facts",
        "content_sha256",
    }
)

TYPED_SCOPE_ENVELOPE_SCHEMA = "bayes_basket_target_canonical_typed_scope_envelope_v1"
TYPED_SCOPE_TYPES = (
    "TRUTH_DAY",
    "TAF_P0M",
    "TRAJECTORY_P1",
    "STRATEGY_UTILITY",
    "MARKET_WS",
    "RUNTIME",
)
TYPED_SCOPE_STATES = (
    "DATA_READY",
    "OOS_READY",
    "ACTIVATION_READY",
    "CANDIDATE_ONLY",
    "BLOCKED",
)
TYPED_SCOPE_BLOCKERS = (
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
TYPED_SCOPE_NATURAL_KEY_FIELDS = {
    ("TRUTH_DAY", "REVISION"): (
        "station_id", "event_date", "truth_revision_no",
    ),
    ("TAF_P0M", "ARTIFACT"): (
        "station_id", "settlement_timezone", "season", "lead_bucket", "run_cycle",
    ),
    ("TAF_P0M", "EVENT"): (
        "station_id", "event_date", "decision_at_utc", "p0_root_sha256", "taf_revision_id",
    ),
    ("TRAJECTORY_P1", "ARTIFACT"): (
        "station_id", "source_lane", "season", "intraday_bucket", "maturity",
        "settlement_definition",
    ),
    ("TRAJECTORY_P1", "EVENT"): (
        "station_id", "event_date", "decision_at_utc", "probability_root_sha256",
        "observation_id",
    ),
    ("STRATEGY_UTILITY", "EVALUATION"): (
        "station_id", "event_date", "decision_at_utc", "settlement_timezone",
        "policy_stage", "strategy_version", "season", "maturity", "liquidity_regime",
        "candidate_inventory_sha256", "baseline_inventory_sha256", "ws_snapshot_sha256",
    ),
    ("MARKET_WS", "REGISTRY"): (
        "station_id", "event_date", "market_id", "yes_token_id", "bucket_contract_sha256",
    ),
    ("MARKET_WS", "SNAPSHOT"): (
        "station_id", "event_date", "market_id", "yes_token_id", "ws_sequence",
        "snapshot_at_utc",
    ),
    ("RUNTIME", "SERVICE_RELEASE"): (
        "service_label", "launch_domain", "runtime_release_id",
    ),
}
TYPED_SCOPE_FACT_FIELDS = frozenset(
    {
        "scope_type",
        "scope_kind",
        "natural_key",
        "as_of_utc",
        "state",
        "blockers",
        "evidence_content_sha256",
        "authority_scope",
        "content_sha256",
    }
)
TYPED_SCOPE_ENVELOPE_FIELDS = frozenset(
    {
        "schema",
        "as_of_utc",
        "builder_id",
        "scope_type",
        "records",
        "production_no_live",
        "future_data_counts_as_oos",
        "content_sha256",
    }
)

ACTIVATION_SUPERSESSION_CONTRACT_SCHEMA = (
    "bayes_basket_target_canonical_activation_supersession_contract_v1"
)
ACTIVATION_SUPERSESSION_REASONS = (
    "IDENTITY_DEDUPLICATION",
    "EVIDENCE_REPLACEMENT",
)
ACTIVATION_SUPERSESSION_FACT_FIELDS = frozenset(
    {
        "supersession_id",
        "station_id",
        "model_family",
        "lead_bucket",
        "run_cycle",
        "predecessor_activation_content_sha256",
        "predecessor_generation",
        "successor_activation_content_sha256",
        "successor_generation",
        "candidate_activation_content_sha256s",
        "candidate_activation_set_sha256",
        "semantic_equivalence_sha256",
        "reason",
        "authority_as_of_utc",
        "authority_scope",
        "content_sha256",
    }
)
ACTIVATION_SUPERSESSION_CONTRACT_FIELDS = frozenset(
    {
        "schema",
        "as_of_utc",
        "builder_id",
        "supersession_facts",
        "content_sha256",
    }
)

REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA = (
    "bayes_basket_target_canonical_republisher_oos_artifact_contract_v1"
)
REPUBLISHER_OOS_COHORT_ROW_FIELDS = frozenset(
    {
        "event_date",
        "partition",
        "forecast_daily_max_c",
        "truth_value_c",
        "model_input_content_sha256",
        "source_receipt_sha256",
        "canonical_truth_fact_content_sha256",
        "awc_truth_id",
        "awc_truth_content_sha256",
        "awc_truth_revision_no",
        "awc_truth_asof_utc",
        "station_day_end_utc",
        "case_contract_sha256",
    }
)
REPUBLISHER_OOS_ARTIFACT_FACT_FIELDS = frozenset(
    {
        "artifact_id",
        "station_id",
        "upstream_model_id",
        "republisher_product_id",
        "independence_family_id",
        "transport_provider_id",
        "model_family",
        "source_class",
        "authority_scope",
        "settlement_timezone",
        "settlement_definition",
        "lead_bucket",
        "run_cycle",
        "run_cycle_scope",
        "season_bin",
        "forecast_extraction_version",
        "artifact_method_version",
        "oos_protocol_version",
        "train_n",
        "holdout_n",
        "cohort_rows",
        "train_case_set_sha256",
        "holdout_case_set_sha256",
        "strict_oos_cohort_sha256",
        "evaluation_content_sha256",
        "calibration_parameters",
        "model_metrics",
        "climatology_baseline_metrics",
        "state",
        "native_arrival_indexed",
        "p1_eligible",
        "formal_live_authority",
        "future_event_count",
        "content_sha256",
    }
)
REPUBLISHER_OOS_ARTIFACT_CONTRACT_FIELDS = frozenset(
    {
        "schema",
        "as_of_utc",
        "builder_id",
        "artifact_facts",
        "content_sha256",
    }
)
_OOS_METRIC_FIELDS = frozenset({"mae_c", "rmse_c", "mean_error_c"})
_OOS_CALIBRATION_PARAMETER_FIELDS = frozenset(
    {"bias_c", "train_climatology_tmax_c"}
)


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    # Canonical payloads are already normalized.  Coercing numbers, bytes or
    # arbitrary objects to text would make two semantically different source
    # fields look identical and would violate the one-to-one contract.
    if value is None:
        result = ""
    elif not isinstance(value, str):
        raise ContractError(f"canonical_{field}_type_invalid")
    else:
        result = value.strip()
    if not result and not allow_empty:
        raise ContractError(f"canonical_{field}_missing")
    return result


def _sha(value: Any, field: str, *, allow_empty: bool = False) -> str:
    result = _text(value, field, allow_empty=allow_empty).lower()
    if not result and allow_empty:
        return result
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"canonical_{field}_invalid")
    return result


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"canonical_{field}_invalid")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"canonical_{field}_invalid")
    return result


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"canonical_{field}_must_be_bool")
    return value


def _timestamp(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if value is not None and not isinstance(value, str):
        raise ContractError(f"canonical_{field}_type_invalid")
    result = _text(value, field, allow_empty=allow_empty)
    if not result and allow_empty:
        return result
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"canonical_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"canonical_{field}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _known_timestamp(
    raw: Mapping[str, Any],
    *,
    known_field: str,
    timestamp_field: str,
    label: str,
) -> tuple[bool, str]:
    """Normalize an explicitly known/unknown clock without inference.

    A missing native publication/receipt clock is semantic information, not a
    nullable convenience field.  Callers must write ``known=false`` together
    with the literal empty timestamp; supplying an archive-query time in its
    place is rejected by the native-member contract below.
    """

    known = _bool(raw.get(known_field), known_field)
    value = raw.get(timestamp_field)
    if known:
        return True, _timestamp(value, timestamp_field)
    if value != "":
        raise ContractError(f"canonical_{label}_{timestamp_field}_unknown_must_be_empty")
    return False, ""


def _event_date(value: Any, field: str = "event_date") -> str:
    result = _text(value, field)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ContractError(f"canonical_{field}_invalid") from exc


def _timezone(value: Any) -> str:
    result = _text(value, "settlement_timezone")
    try:
        ZoneInfo(result)
    except ZoneInfoNotFoundError as exc:
        raise ContractError("canonical_settlement_timezone_invalid") from exc
    return result


def _strict_keys(raw: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if not isinstance(raw, Mapping):
        raise ContractError(f"canonical_{label}_mapping_required")
    unknown = sorted(set(raw) - expected)
    missing = sorted(expected - set(raw))
    if unknown:
        raise ContractError(f"canonical_{label}_unknown_field:{','.join(unknown)}")
    if missing:
        raise ContractError(f"canonical_{label}_missing_field:{','.join(missing)}")


def _verify_hash(raw: Mapping[str, Any], *, label: str, hash_field: str = "content_sha256") -> None:
    supplied = _sha(raw.get(hash_field), f"{label}_{hash_field}")
    identity = {key: value for key, value in raw.items() if key != hash_field}
    if supplied != content_sha256(identity):
        raise ContractError(f"canonical_{label}_content_sha256_mismatch")


@dataclass(frozen=True)
class CanonicalModelFact:
    station_id: str
    model_id: str
    ensemble_system_id: str
    model_family: str
    provider_id: str
    source_class: str
    event_date: str
    run_init_utc: str
    lead_bucket: str
    run_cycle: str
    settlement_timezone: str
    forecast_value: float
    raw_object_sha256: str
    content_sha256: str
    oos_role: str
    closed_day: bool
    strict_asof: bool
    native_arrival_indexed: bool
    p1_eligible: bool
    formal_live_authority: bool
    published_at_utc: str
    received_at_utc: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalModelFact":
        _strict_keys(raw, MODEL_FACT_FIELDS, "model_fact")
        model_id = _text(raw.get("model_id"), "model_id", allow_empty=True).upper()
        ensemble = _text(
            raw.get("ensemble_system_id"), "ensemble_system_id", allow_empty=True
        ).upper()
        if bool(model_id) == bool(ensemble):
            raise ContractError("canonical_model_fact_identifier_must_be_exactly_one")
        model_family = _text(raw.get("model_family"), "model_family")
        if model_family not in MODEL_FAMILIES:
            raise ContractError("canonical_model_fact_model_family_invalid")
        source_class = _text(raw.get("source_class"), "source_class")
        if source_class not in SOURCE_CLASSES:
            raise ContractError("canonical_model_fact_source_class_invalid")
        role = _text(raw.get("oos_role"), "oos_role")
        if role not in EVIDENCE_ROLES:
            raise ContractError("canonical_model_fact_oos_role_invalid")
        closed = _bool(raw.get("closed_day"), "closed_day")
        strict = _bool(raw.get("strict_asof"), "strict_asof")
        native_indexed = _bool(raw.get("native_arrival_indexed"), "native_arrival_indexed")
        p1 = _bool(raw.get("p1_eligible"), "p1_eligible")
        formal = _bool(raw.get("formal_live_authority"), "formal_live_authority")
        if role == "historical_strict_oos" and not (closed and strict):
            raise ContractError("canonical_model_fact_historical_oos_not_closed_strict")
        if source_class == "aggregator_republisher" and (
            native_indexed or p1 or formal
        ):
            raise ContractError("canonical_model_fact_republisher_authority_forbidden")
        if role == "observational_candidate_not_oos" and formal:
            raise ContractError("canonical_model_fact_observation_formal_authority_forbidden")
        run_cycle = _text(raw.get("run_cycle"), "run_cycle")
        # ``run_init_utc`` is the upstream model initialization, not the
        # republisher receipt time.  If the source cannot provide that exact
        # semantic field, the scope is blocked; an empty value or a copied
        # arrival timestamp would create a false lineage.
        run_init = _timestamp(raw.get("run_init_utc"), "run_init_utc")
        result = cls(
            station_id=_text(raw.get("station_id"), "station_id").upper(),
            model_id=model_id,
            ensemble_system_id=ensemble,
            model_family=model_family,
            provider_id=_text(raw.get("provider_id"), "provider_id"),
            source_class=source_class,
            event_date=_event_date(raw.get("event_date")),
            run_init_utc=run_init,
            lead_bucket=_text(raw.get("lead_bucket"), "lead_bucket"),
            run_cycle=run_cycle,
            settlement_timezone=_timezone(raw.get("settlement_timezone")),
            forecast_value=_number(raw.get("forecast_value"), "forecast_value"),
            raw_object_sha256=_sha(raw.get("raw_object_sha256"), "raw_object_sha256"),
            content_sha256=_sha(raw.get("content_sha256"), "model_fact_content_sha256"),
            oos_role=role,
            closed_day=closed,
            strict_asof=strict,
            native_arrival_indexed=native_indexed,
            p1_eligible=p1,
            formal_live_authority=formal,
            published_at_utc=_timestamp(raw.get("published_at_utc"), "published_at_utc", allow_empty=True),
            received_at_utc=_timestamp(raw.get("received_at_utc"), "received_at_utc", allow_empty=True),
        )
        if result.oos_role == "historical_strict_oos" and not result.strict_asof:
            # Keep this check adjacent to the normalized object so an adapter
            # cannot accidentally create a historical row from a prospective
            # flag after type coercion.
            raise ContractError("canonical_model_fact_historical_oos_not_strict")
        _verify_hash(result.to_mapping(), label="model_fact")
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "model_id": self.model_id,
            "ensemble_system_id": self.ensemble_system_id,
            "model_family": self.model_family,
            "provider_id": self.provider_id,
            "source_class": self.source_class,
            "event_date": self.event_date,
            "run_init_utc": self.run_init_utc,
            "lead_bucket": self.lead_bucket,
            "run_cycle": self.run_cycle,
            "settlement_timezone": self.settlement_timezone,
            "forecast_value": self.forecast_value,
            "raw_object_sha256": self.raw_object_sha256,
            "content_sha256": self.content_sha256,
            "oos_role": self.oos_role,
            "closed_day": self.closed_day,
            "strict_asof": self.strict_asof,
            "native_arrival_indexed": self.native_arrival_indexed,
            "p1_eligible": self.p1_eligible,
            "formal_live_authority": self.formal_live_authority,
            "published_at_utc": self.published_at_utc,
            "received_at_utc": self.received_at_utc,
        }

    @property
    def scope_key(self) -> str:
        identifier = self.model_id or self.ensemble_system_id
        return "|".join(
            (
                self.station_id,
                identifier,
                self.event_date,
                self.run_init_utc,
                self.lead_bucket,
                self.run_cycle,
            )
        )


@dataclass(frozen=True)
class CanonicalNativeArrivalFact:
    """One official native object observed for one exact model run.

    This fact intentionally has no station/event/member dimension.  A GRIB,
    BUFR, or provider object may support many station/member extractions, but
    it has exactly one provider/run arrival lineage.  It does not by itself
    grant model, P1, activation, or live authority.
    """

    native_arrival_id: str
    ensemble_system_id: str
    provider_id: str
    source_class: str
    native_object_id: str
    run_init_utc: str
    lead_bucket: str
    run_cycle: str
    raw_object_sha256: str
    published_at_known: bool
    published_at_utc: str
    received_at_known: bool
    received_at_utc: str
    native_arrival_indexed: bool
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalNativeArrivalFact":
        _strict_keys(raw, NATIVE_ARRIVAL_FACT_FIELDS, "native_arrival_fact")
        source_class = _text(raw.get("source_class"), "source_class")
        # An aggregator may preserve a receipt for offline diagnosis, but it
        # cannot create a native-arrival fact or enter the native arrival
        # index.  Keep that prohibition at the contract boundary.
        if source_class != "native_official":
            raise ContractError("canonical_native_arrival_republisher_forbidden")
        published_known, published_at = _known_timestamp(
            raw,
            known_field="published_at_known",
            timestamp_field="published_at_utc",
            label="native_arrival",
        )
        received_known, received_at = _known_timestamp(
            raw,
            known_field="received_at_known",
            timestamp_field="received_at_utc",
            label="native_arrival",
        )
        indexed = _bool(
            raw.get("native_arrival_indexed"), "native_arrival_indexed"
        )
        raw_object_sha = _sha(
            raw.get("raw_object_sha256"), "raw_object_sha256", allow_empty=True
        )
        if indexed and not (
            published_known and received_known and raw_object_sha
        ):
            raise ContractError("canonical_native_arrival_indexed_timestamps_missing")
        if published_known and received_known and published_at > received_at:
            raise ContractError("canonical_native_arrival_timestamp_order_invalid")
        result = cls(
            native_arrival_id=_text(
                raw.get("native_arrival_id"), "native_arrival_id"
            ),
            ensemble_system_id=_text(
                raw.get("ensemble_system_id"), "ensemble_system_id"
            ).upper(),
            provider_id=_text(raw.get("provider_id"), "provider_id"),
            source_class=source_class,
            native_object_id=_text(
                raw.get("native_object_id"), "native_object_id"
            ),
            run_init_utc=_timestamp(raw.get("run_init_utc"), "run_init_utc"),
            lead_bucket=_text(raw.get("lead_bucket"), "lead_bucket"),
            run_cycle=_text(raw.get("run_cycle"), "run_cycle"),
            raw_object_sha256=raw_object_sha,
            published_at_known=published_known,
            published_at_utc=published_at,
            received_at_known=received_known,
            received_at_utc=received_at,
            native_arrival_indexed=indexed,
            content_sha256=_sha(
                raw.get("content_sha256"), "native_arrival_content_sha256"
            ),
        )
        _verify_hash(result.to_mapping(), label="native_arrival_fact")
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "native_arrival_id": self.native_arrival_id,
            "ensemble_system_id": self.ensemble_system_id,
            "provider_id": self.provider_id,
            "source_class": self.source_class,
            "native_object_id": self.native_object_id,
            "run_init_utc": self.run_init_utc,
            "lead_bucket": self.lead_bucket,
            "run_cycle": self.run_cycle,
            "raw_object_sha256": self.raw_object_sha256,
            "published_at_known": self.published_at_known,
            "published_at_utc": self.published_at_utc,
            "received_at_known": self.received_at_known,
            "received_at_utc": self.received_at_utc,
            "native_arrival_indexed": self.native_arrival_indexed,
            "content_sha256": self.content_sha256,
        }

    @property
    def scope_key(self) -> str:
        return "|".join(
            (
                self.ensemble_system_id,
                self.native_object_id,
                self.run_init_utc,
                self.lead_bucket,
                self.run_cycle,
            )
        )


@dataclass(frozen=True)
class CanonicalEnsembleMemberFact:
    """One immutable decoded station/member source fact.

    Native publication/receipt lineage is deliberately *not* duplicated here.
    It belongs to :class:`CanonicalNativeArrivalFact`, whose late arrival may
    be appended without rewriting this member natural key.  A later ensemble
    builder must consume both facts through an explicit activation evidence
    link; it may not infer an arrival from an archive query or receipt set.
    """

    station_id: str
    ensemble_system_id: str
    model_family: str
    provider_id: str
    source_class: str
    event_date: str
    run_init_utc: str
    lead_bucket: str
    run_cycle: str
    member_id: str
    member_role: str
    extraction_version: str
    settlement_timezone: str
    forecast_value: float
    raw_receipt_set_sha256: str
    source_member_fact_sha: str
    archive_query_at_known: bool
    archive_query_at_utc: str
    oos_role: str
    closed_day: bool
    strict_asof: bool
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalEnsembleMemberFact":
        _strict_keys(raw, ENSEMBLE_MEMBER_FACT_FIELDS, "ensemble_member_fact")
        source_class = _text(raw.get("source_class"), "source_class")
        if source_class not in SOURCE_CLASSES:
            raise ContractError("canonical_ensemble_member_source_class_invalid")
        if _text(raw.get("model_family"), "model_family") != "global_ensemble":
            raise ContractError("canonical_ensemble_member_model_family_invalid")
        role = _text(raw.get("oos_role"), "oos_role")
        if role not in EVIDENCE_ROLES:
            raise ContractError("canonical_ensemble_member_oos_role_invalid")
        member_role = _text(raw.get("member_role"), "member_role")
        if member_role not in {"CONTROL", "PERTURBED"}:
            raise ContractError("canonical_ensemble_member_role_invalid")
        archive_known, archive_at = _known_timestamp(
            raw,
            known_field="archive_query_at_known",
            timestamp_field="archive_query_at_utc",
            label="ensemble_member",
        )
        closed = _bool(raw.get("closed_day"), "closed_day")
        strict = _bool(raw.get("strict_asof"), "strict_asof")
        if role == "historical_strict_oos" and not (closed and strict):
            raise ContractError("canonical_ensemble_member_historical_oos_not_closed_strict")
        run_init = _timestamp(raw.get("run_init_utc"), "run_init_utc")
        if archive_known and archive_at < run_init:
            raise ContractError("canonical_ensemble_member_archive_before_run_init")
        result = cls(
            station_id=_text(raw.get("station_id"), "station_id").upper(),
            ensemble_system_id=_text(
                raw.get("ensemble_system_id"), "ensemble_system_id"
            ).upper(),
            model_family="global_ensemble",
            provider_id=_text(raw.get("provider_id"), "provider_id"),
            source_class=source_class,
            event_date=_event_date(raw.get("event_date")),
            run_init_utc=run_init,
            lead_bucket=_text(raw.get("lead_bucket"), "lead_bucket"),
            run_cycle=_text(raw.get("run_cycle"), "run_cycle"),
            member_id=_text(raw.get("member_id"), "member_id"),
            member_role=member_role,
            extraction_version=_text(
                raw.get("extraction_version"), "extraction_version"
            ),
            settlement_timezone=_timezone(raw.get("settlement_timezone")),
            forecast_value=_number(
                raw.get("forecast_value"), "forecast_value"
            ),
            raw_receipt_set_sha256=_sha(
                raw.get("raw_receipt_set_sha256"), "raw_receipt_set_sha256"
            ),
            source_member_fact_sha=_sha(
                raw.get("source_member_fact_sha"), "source_member_fact_sha"
            ),
            archive_query_at_known=archive_known,
            archive_query_at_utc=archive_at,
            oos_role=role,
            closed_day=closed,
            strict_asof=strict,
            content_sha256=_sha(
                raw.get("content_sha256"), "ensemble_member_content_sha256"
            ),
        )
        _verify_hash(result.to_mapping(), label="ensemble_member_fact")
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "ensemble_system_id": self.ensemble_system_id,
            "model_family": self.model_family,
            "provider_id": self.provider_id,
            "source_class": self.source_class,
            "event_date": self.event_date,
            "run_init_utc": self.run_init_utc,
            "lead_bucket": self.lead_bucket,
            "run_cycle": self.run_cycle,
            "member_id": self.member_id,
            "member_role": self.member_role,
            "extraction_version": self.extraction_version,
            "settlement_timezone": self.settlement_timezone,
            "forecast_value": self.forecast_value,
            "raw_receipt_set_sha256": self.raw_receipt_set_sha256,
            "source_member_fact_sha": self.source_member_fact_sha,
            "archive_query_at_known": self.archive_query_at_known,
            "archive_query_at_utc": self.archive_query_at_utc,
            "oos_role": self.oos_role,
            "closed_day": self.closed_day,
            "strict_asof": self.strict_asof,
            "content_sha256": self.content_sha256,
        }

    @property
    def scope_key(self) -> str:
        return "|".join(
            (
                self.station_id,
                self.ensemble_system_id,
                self.event_date,
                self.run_init_utc,
                self.lead_bucket,
                self.run_cycle,
                self.member_id,
                self.extraction_version,
            )
        )


@dataclass(frozen=True)
class CanonicalEnsembleMemberReceiptRef:
    """One required extraction receipt, in exact manifest order."""

    ordinal: int
    native_object_id: str
    raw_object_sha256: str
    receipt_evidence_at_utc: str
    receipt_evidence_content_sha256: str
    extraction_selector_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalEnsembleMemberReceiptRef":
        _strict_keys(
            raw,
            ENSEMBLE_MEMBER_RECEIPT_REF_FIELDS,
            "ensemble_member_receipt_ref",
        )
        ordinal = raw.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise ContractError("canonical_ensemble_member_receipt_ref_ordinal_invalid")
        return cls(
            ordinal=ordinal,
            native_object_id=_text(
                raw.get("native_object_id"), "receipt_ref_native_object_id"
            ),
            raw_object_sha256=_sha(
                raw.get("raw_object_sha256"), "receipt_ref_raw_object_sha256"
            ),
            receipt_evidence_at_utc=_timestamp(
                raw.get("receipt_evidence_at_utc"),
                "receipt_ref_evidence_at_utc",
            ),
            receipt_evidence_content_sha256=_sha(
                raw.get("receipt_evidence_content_sha256"),
                "receipt_ref_evidence_content_sha256",
            ),
            extraction_selector_sha256=_sha(
                raw.get("extraction_selector_sha256"),
                "receipt_ref_extraction_selector_sha256",
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "native_object_id": self.native_object_id,
            "raw_object_sha256": self.raw_object_sha256,
            "receipt_evidence_at_utc": self.receipt_evidence_at_utc,
            "receipt_evidence_content_sha256": self.receipt_evidence_content_sha256,
            "extraction_selector_sha256": self.extraction_selector_sha256,
        }


@dataclass(frozen=True)
class CanonicalEnsembleMemberReceiptManifestFact:
    """Exact receipt requirements for one already-content-addressed member.

    This is a separate append-only fact so retained V5 member identities never
    need to be rewritten.  Absence of this manifest is a lineage blocker, not
    permission to infer requirements from a run, filename, or archive query.
    """

    ensemble_member_content_sha256: str
    source_raw_receipt_set_sha256: str
    required_receipt_refs: tuple[CanonicalEnsembleMemberReceiptRef, ...]
    required_receipt_ref_set_sha256: str
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalEnsembleMemberReceiptManifestFact":
        _strict_keys(
            raw,
            ENSEMBLE_MEMBER_RECEIPT_MANIFEST_FACT_FIELDS,
            "ensemble_member_receipt_manifest_fact",
        )
        raw_refs = raw.get("required_receipt_refs")
        if (
            not isinstance(raw_refs, list)
            or not raw_refs
            or any(not isinstance(item, Mapping) for item in raw_refs)
        ):
            raise ContractError(
                "canonical_ensemble_member_receipt_manifest_refs_required"
            )
        refs = tuple(
            CanonicalEnsembleMemberReceiptRef.from_mapping(item)
            for item in raw_refs
        )
        if tuple(item.ordinal for item in refs) != tuple(range(len(refs))):
            raise ContractError(
                "canonical_ensemble_member_receipt_manifest_ordinal_set_invalid"
            )
        ref_mappings = [item.to_mapping() for item in refs]
        ref_set_sha = _sha(
            raw.get("required_receipt_ref_set_sha256"),
            "required_receipt_ref_set_sha256",
        )
        if ref_set_sha != content_sha256(ref_mappings):
            raise ContractError(
                "canonical_ensemble_member_receipt_manifest_ref_set_hash_mismatch"
            )
        result = cls(
            ensemble_member_content_sha256=_sha(
                raw.get("ensemble_member_content_sha256"),
                "receipt_manifest_ensemble_member_content_sha256",
            ),
            source_raw_receipt_set_sha256=_sha(
                raw.get("source_raw_receipt_set_sha256"),
                "receipt_manifest_source_raw_receipt_set_sha256",
            ),
            required_receipt_refs=refs,
            required_receipt_ref_set_sha256=ref_set_sha,
            content_sha256=_sha(
                raw.get("content_sha256"), "receipt_manifest_content_sha256"
            ),
        )
        _verify_hash(
            result.to_mapping(), label="ensemble_member_receipt_manifest_fact"
        )
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "ensemble_member_content_sha256": self.ensemble_member_content_sha256,
            "source_raw_receipt_set_sha256": self.source_raw_receipt_set_sha256,
            "required_receipt_refs": [
                item.to_mapping() for item in self.required_receipt_refs
            ],
            "required_receipt_ref_set_sha256": self.required_receipt_ref_set_sha256,
            "content_sha256": self.content_sha256,
        }

    @property
    def scope_key(self) -> str:
        return self.ensemble_member_content_sha256


@dataclass(frozen=True)
class CanonicalEnsembleMemberArrivalBindingFact:
    """Coverage of exact manifest ordinals by one official native object."""

    ensemble_member_content_sha256: str
    native_arrival_content_sha256: str
    receipt_manifest_content_sha256: str
    manifest_entry_ordinals: tuple[int, ...]
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalEnsembleMemberArrivalBindingFact":
        _strict_keys(
            raw,
            ENSEMBLE_MEMBER_ARRIVAL_BINDING_FACT_FIELDS,
            "ensemble_member_arrival_binding_fact",
        )
        raw_ordinals = raw.get("manifest_entry_ordinals")
        if not isinstance(raw_ordinals, list) or not raw_ordinals:
            raise ContractError(
                "canonical_ensemble_member_arrival_binding_ordinals_required"
            )
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in raw_ordinals
        ) or raw_ordinals != sorted(set(raw_ordinals)):
            raise ContractError(
                "canonical_ensemble_member_arrival_binding_ordinals_invalid"
            )
        result = cls(
            ensemble_member_content_sha256=_sha(
                raw.get("ensemble_member_content_sha256"),
                "binding_ensemble_member_content_sha256",
            ),
            native_arrival_content_sha256=_sha(
                raw.get("native_arrival_content_sha256"),
                "binding_native_arrival_content_sha256",
            ),
            receipt_manifest_content_sha256=_sha(
                raw.get("receipt_manifest_content_sha256"),
                "binding_receipt_manifest_content_sha256",
            ),
            manifest_entry_ordinals=tuple(raw_ordinals),
            content_sha256=_sha(
                raw.get("content_sha256"), "binding_content_sha256"
            ),
        )
        _verify_hash(
            result.to_mapping(), label="ensemble_member_arrival_binding_fact"
        )
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "ensemble_member_content_sha256": self.ensemble_member_content_sha256,
            "native_arrival_content_sha256": self.native_arrival_content_sha256,
            "receipt_manifest_content_sha256": self.receipt_manifest_content_sha256,
            "manifest_entry_ordinals": list(self.manifest_entry_ordinals),
            "content_sha256": self.content_sha256,
        }

    @property
    def scope_key(self) -> str:
        return "|".join(
            (
                self.ensemble_member_content_sha256,
                self.native_arrival_content_sha256,
            )
        )


@dataclass(frozen=True)
class CanonicalTruthFact:
    truth_id: str
    station_id: str
    event_date: str
    truth_revision_no: int
    truth_value: float
    truth_content_sha256: str
    canonical_committed_at_utc: str
    station_day_end_utc: str
    closed_day: bool
    source_lane: str
    source_role: str
    authority_scope: str
    settlement_timezone: str
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalTruthFact":
        _strict_keys(raw, TRUTH_FACT_FIELDS, "truth_fact")
        revision = raw.get("truth_revision_no")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ContractError("canonical_truth_revision_invalid")
        source_lane = _text(raw.get("source_lane"), "source_lane").upper()
        if source_lane != "AWC":
            raise ContractError("canonical_truth_source_lane_invalid")
        source_role = _text(raw.get("source_role"), "source_role")
        if source_role not in TRUTH_SOURCE_ROLES:
            raise ContractError("canonical_truth_source_role_invalid")
        authority = _text(raw.get("authority_scope"), "authority_scope")
        if authority not in AUTHORITY_SCOPES:
            raise ContractError("canonical_truth_authority_scope_invalid")
        closed = _bool(raw.get("closed_day"), "closed_day")
        committed = _timestamp(raw.get("canonical_committed_at_utc"), "canonical_committed_at_utc")
        day_end = _timestamp(raw.get("station_day_end_utc"), "station_day_end_utc")
        if closed and datetime.fromisoformat(committed.replace("Z", "+00:00")) < datetime.fromisoformat(day_end.replace("Z", "+00:00")):
            raise ContractError("canonical_truth_closed_before_station_day_end")
        if authority == "FORMAL_NATIVE_ELIGIBLE" and source_lane != "AWC":
            raise ContractError("canonical_truth_formal_requires_awc")
        if (
            source_role == FORMAL_AWC_TRUTH_ROLE
            and authority != "FORMAL_NATIVE_ELIGIBLE"
        ):
            raise ContractError("canonical_truth_formal_role_authority_mismatch")
        if (
            source_role == HISTORICAL_AWC_REPLAY_TRUTH_ROLE
            and authority != "PRODUCTION_NO_LIVE_ONLY"
        ):
            raise ContractError("canonical_truth_replay_role_authority_mismatch")
        result = cls(
            truth_id=_text(raw.get("truth_id"), "truth_id"),
            station_id=_text(raw.get("station_id"), "station_id").upper(),
            event_date=_event_date(raw.get("event_date")),
            truth_revision_no=revision,
            truth_value=_number(raw.get("truth_value"), "truth_value"),
            truth_content_sha256=_sha(raw.get("truth_content_sha256"), "truth_content_sha256"),
            canonical_committed_at_utc=committed,
            station_day_end_utc=day_end,
            closed_day=closed,
            source_lane=source_lane,
            source_role=source_role,
            authority_scope=authority,
            settlement_timezone=_timezone(raw.get("settlement_timezone")),
            content_sha256=_sha(raw.get("content_sha256"), "truth_fact_content_sha256"),
        )
        _verify_hash(result.to_mapping(), label="truth_fact")
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "truth_id": self.truth_id,
            "station_id": self.station_id,
            "event_date": self.event_date,
            "truth_revision_no": self.truth_revision_no,
            "truth_value": self.truth_value,
            "truth_content_sha256": self.truth_content_sha256,
            "canonical_committed_at_utc": self.canonical_committed_at_utc,
            "station_day_end_utc": self.station_day_end_utc,
            "closed_day": self.closed_day,
            "source_lane": self.source_lane,
            "source_role": self.source_role,
            "authority_scope": self.authority_scope,
            "settlement_timezone": self.settlement_timezone,
            "content_sha256": self.content_sha256,
        }

    @property
    def scope_key(self) -> str:
        # This is the StationDailyHighTruth natural key from the SSOT.  Source
        # role is content-addressed evidence, not permission to create a second
        # truth lane for the same revision.
        return f"{self.station_id}|{self.event_date}|{self.truth_revision_no}"


@dataclass(frozen=True)
class CanonicalResidualFact:
    residual_id: str
    truth_id: str
    model_fact_content_sha256: str
    station_id: str
    model_id: str
    event_date: str
    lead_bucket: str
    run_cycle: str
    residual_value: float
    paired_asof_utc: str
    content_sha256: str
    oos_role: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalResidualFact":
        _strict_keys(raw, RESIDUAL_FACT_FIELDS, "residual_fact")
        role = _text(raw.get("oos_role"), "oos_role")
        if role not in EVIDENCE_ROLES:
            raise ContractError("canonical_residual_oos_role_invalid")
        result = cls(
            residual_id=_text(raw.get("residual_id"), "residual_id"),
            truth_id=_text(raw.get("truth_id"), "truth_id"),
            model_fact_content_sha256=_sha(raw.get("model_fact_content_sha256"), "model_fact_content_sha256"),
            station_id=_text(raw.get("station_id"), "station_id").upper(),
            model_id=_text(raw.get("model_id"), "model_id").upper(),
            event_date=_event_date(raw.get("event_date")),
            lead_bucket=_text(raw.get("lead_bucket"), "lead_bucket"),
            run_cycle=_text(raw.get("run_cycle"), "run_cycle"),
            residual_value=_number(raw.get("residual_value"), "residual_value"),
            paired_asof_utc=_timestamp(raw.get("paired_asof_utc"), "paired_asof_utc"),
            content_sha256=_sha(raw.get("content_sha256"), "residual_fact_content_sha256"),
            oos_role=role,
        )
        if result.oos_role == "historical_strict_oos" and not result.paired_asof_utc:
            raise ContractError("canonical_residual_historical_oos_asof_missing")
        _verify_hash(result.to_mapping(), label="residual_fact")
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "residual_id": self.residual_id,
            "truth_id": self.truth_id,
            "model_fact_content_sha256": self.model_fact_content_sha256,
            "station_id": self.station_id,
            "model_id": self.model_id,
            "event_date": self.event_date,
            "lead_bucket": self.lead_bucket,
            "run_cycle": self.run_cycle,
            "residual_value": self.residual_value,
            "paired_asof_utc": self.paired_asof_utc,
            "content_sha256": self.content_sha256,
            "oos_role": self.oos_role,
        }

    @property
    def scope_key(self) -> str:
        # Residual identity is the exact truth revision paired with the exact
        # immutable model fact.  A revised truth must create a new residual;
        # station/date/model alone would collide and tempt an overwrite.
        return f"{self.truth_id}|{self.model_fact_content_sha256}"


@dataclass(frozen=True)
class CanonicalActivationFact:
    activation_id: str
    station_id: str
    model_family: str
    lead_bucket: str
    run_cycle: str
    candidate_models: tuple[str, ...]
    selected_models: tuple[str, ...]
    selected_weights: dict[str, float]
    available_model_set_hash: str
    selection_evidence_sha256: str
    artifact_content_sha256: str
    activation_generation: int
    activation_content_sha256: str
    authority_scope: str
    oos_role: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalActivationFact":
        _strict_keys(raw, ACTIVATION_FACT_FIELDS, "activation_fact")
        family = _text(raw.get("model_family"), "model_family")
        if family not in MODEL_FAMILIES:
            raise ContractError("canonical_activation_model_family_invalid")
        authority = _text(raw.get("authority_scope"), "authority_scope")
        if authority not in AUTHORITY_SCOPES:
            raise ContractError("canonical_activation_authority_scope_invalid")
        role = _text(raw.get("oos_role"), "oos_role")
        if role not in EVIDENCE_ROLES:
            raise ContractError("canonical_activation_oos_role_invalid")
        candidates = tuple(sorted({_text(item, "candidate_model").upper() for item in raw.get("candidate_models") or []}))
        selected = tuple(sorted({_text(item, "selected_model").upper() for item in raw.get("selected_models") or []}))
        if not candidates or not selected or not set(selected).issubset(candidates):
            raise ContractError("canonical_activation_model_set_invalid")
        weights_raw = raw.get("selected_weights")
        if not isinstance(weights_raw, Mapping):
            raise ContractError("canonical_activation_selected_weights_invalid")
        weights = {str(key).upper(): _number(value, "selected_weight") for key, value in weights_raw.items()}
        if set(weights) != set(selected) or any(value < 0 for value in weights.values()):
            raise ContractError("canonical_activation_selected_weights_scope_invalid")
        if abs(sum(weights.values()) - 1.0) > 1e-6:
            raise ContractError("canonical_activation_selected_weights_not_normalized")
        generation = raw.get("activation_generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ContractError("canonical_activation_generation_invalid")
        result = cls(
            activation_id=_text(raw.get("activation_id"), "activation_id"),
            station_id=_text(raw.get("station_id"), "station_id").upper(),
            model_family=family,
            lead_bucket=_text(raw.get("lead_bucket"), "lead_bucket"),
            run_cycle=_text(raw.get("run_cycle"), "run_cycle"),
            candidate_models=candidates,
            selected_models=selected,
            selected_weights=weights,
            available_model_set_hash=_sha(raw.get("available_model_set_hash"), "available_model_set_hash"),
            selection_evidence_sha256=_sha(raw.get("selection_evidence_sha256"), "selection_evidence_sha256"),
            artifact_content_sha256=_sha(raw.get("artifact_content_sha256"), "artifact_content_sha256"),
            activation_generation=generation,
            activation_content_sha256=_sha(raw.get("activation_content_sha256"), "activation_content_sha256"),
            authority_scope=authority,
            oos_role=role,
        )
        if authority == "FORMAL_NATIVE_ELIGIBLE" and role != "historical_strict_oos":
            raise ContractError("canonical_activation_formal_requires_historical_oos")
        identity = result.to_mapping()
        identity.pop("activation_content_sha256")
        if result.activation_content_sha256 != content_sha256(identity):
            raise ContractError("canonical_activation_content_sha256_mismatch")
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "activation_id": self.activation_id,
            "station_id": self.station_id,
            "model_family": self.model_family,
            "lead_bucket": self.lead_bucket,
            "run_cycle": self.run_cycle,
            "candidate_models": list(self.candidate_models),
            "selected_models": list(self.selected_models),
            "selected_weights": dict(sorted(self.selected_weights.items())),
            "available_model_set_hash": self.available_model_set_hash,
            "selection_evidence_sha256": self.selection_evidence_sha256,
            "artifact_content_sha256": self.artifact_content_sha256,
            "activation_generation": self.activation_generation,
            "activation_content_sha256": self.activation_content_sha256,
            "authority_scope": self.authority_scope,
            "oos_role": self.oos_role,
        }

    @property
    def scope_key(self) -> str:
        return f"{self.station_id}|{self.model_family}|{self.lead_bucket}|{self.run_cycle}|{self.activation_generation}"


def _typed_natural_key(
    *, scope_type: str, scope_kind: str, raw: Any
) -> dict[str, Any]:
    fields = TYPED_SCOPE_NATURAL_KEY_FIELDS.get((scope_type, scope_kind))
    if fields is None:
        raise ContractError("canonical_typed_scope_kind_invalid")
    if not isinstance(raw, Mapping) or set(raw) != set(fields):
        raise ContractError("canonical_typed_scope_natural_key_fields_invalid")
    result: dict[str, Any] = {}
    for field in fields:
        value = raw.get(field)
        if field == "station_id":
            result[field] = _text(value, field).upper()
        elif field == "event_date":
            result[field] = _event_date(value)
        elif field.endswith("_at_utc"):
            result[field] = _timestamp(value, field)
        elif field == "settlement_timezone":
            result[field] = _timezone(value)
        elif field.endswith("_sha256"):
            result[field] = _sha(value, field)
        elif field in {"truth_revision_no", "ws_sequence"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"canonical_typed_scope_{field}_invalid")
            result[field] = value
        else:
            result[field] = _text(value, field)
    if result.get("source_lane") not in {None, "AWC"}:
        raise ContractError("canonical_typed_scope_source_lane_invalid")
    launch_domain = result.get("launch_domain")
    if launch_domain is not None and (
        not launch_domain.startswith("gui/")
        or not launch_domain.removeprefix("gui/").isdigit()
    ):
        raise ContractError("canonical_typed_scope_launch_domain_invalid")
    return result


@dataclass(frozen=True)
class CanonicalTypedScopeFact:
    scope_type: str
    scope_kind: str
    natural_key: dict[str, Any]
    as_of_utc: str
    state: str
    blockers: tuple[str, ...]
    evidence_content_sha256: str
    authority_scope: str
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalTypedScopeFact":
        _strict_keys(raw, TYPED_SCOPE_FACT_FIELDS, "typed_scope_fact")
        scope_type = _text(raw.get("scope_type"), "scope_type")
        if scope_type not in TYPED_SCOPE_TYPES:
            raise ContractError("canonical_typed_scope_type_invalid")
        scope_kind = _text(raw.get("scope_kind"), "scope_kind")
        natural_key = _typed_natural_key(
            scope_type=scope_type,
            scope_kind=scope_kind,
            raw=raw.get("natural_key"),
        )
        state = _text(raw.get("state"), "state")
        if state not in TYPED_SCOPE_STATES:
            raise ContractError("canonical_typed_scope_state_invalid")
        raw_blockers = raw.get("blockers")
        if not isinstance(raw_blockers, list):
            raise ContractError("canonical_typed_scope_blockers_list_required")
        blockers = tuple(raw_blockers)
        if (
            any(not isinstance(item, str) or item not in TYPED_SCOPE_BLOCKERS for item in blockers)
            or list(blockers) != sorted(set(blockers))
        ):
            raise ContractError("canonical_typed_scope_blockers_invalid")
        if (state in {"DATA_READY", "OOS_READY", "ACTIVATION_READY"}) != (not blockers):
            raise ContractError("canonical_typed_scope_state_blocker_mismatch")
        authority = _text(raw.get("authority_scope"), "authority_scope")
        if authority != "PRODUCTION_NO_LIVE_ONLY":
            raise ContractError("canonical_typed_scope_authority_invalid")
        result = cls(
            scope_type=scope_type,
            scope_kind=scope_kind,
            natural_key=natural_key,
            as_of_utc=_timestamp(raw.get("as_of_utc"), "as_of_utc"),
            state=state,
            blockers=blockers,
            evidence_content_sha256=_sha(
                raw.get("evidence_content_sha256"), "evidence_content_sha256"
            ),
            authority_scope=authority,
            content_sha256=_sha(raw.get("content_sha256"), "typed_scope_content_sha256"),
        )
        _verify_hash(result.to_mapping(), label="typed_scope_fact")
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "scope_type": self.scope_type,
            "scope_kind": self.scope_kind,
            "natural_key": dict(self.natural_key),
            "as_of_utc": self.as_of_utc,
            "state": self.state,
            "blockers": list(self.blockers),
            "evidence_content_sha256": self.evidence_content_sha256,
            "authority_scope": self.authority_scope,
            "content_sha256": self.content_sha256,
        }

    @property
    def scope_key(self) -> str:
        return "|".join(
            (
                self.scope_type,
                self.scope_kind,
                json.dumps(self.natural_key, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        )


@dataclass(frozen=True)
class CanonicalTypedScopeEnvelope:
    as_of_utc: str
    builder_id: str
    scope_type: str
    records: tuple[CanonicalTypedScopeFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalTypedScopeEnvelope":
        _strict_keys(raw, TYPED_SCOPE_ENVELOPE_FIELDS, "typed_scope_envelope")
        if raw.get("schema") != TYPED_SCOPE_ENVELOPE_SCHEMA:
            raise ContractError("canonical_typed_scope_envelope_schema_invalid")
        if raw.get("production_no_live") is not True:
            raise ContractError("canonical_typed_scope_envelope_no_live_required")
        if raw.get("future_data_counts_as_oos") is not False:
            raise ContractError("canonical_typed_scope_envelope_future_oos_forbidden")
        scope_type = _text(raw.get("scope_type"), "scope_type")
        if scope_type not in TYPED_SCOPE_TYPES:
            raise ContractError("canonical_typed_scope_envelope_type_invalid")
        raw_records = raw.get("records")
        if (
            not isinstance(raw_records, list)
            or not raw_records
            or any(not isinstance(item, Mapping) for item in raw_records)
        ):
            raise ContractError("canonical_typed_scope_envelope_records_required")
        records = tuple(CanonicalTypedScopeFact.from_mapping(item) for item in raw_records)
        if any(item.scope_type != scope_type for item in records):
            raise ContractError("canonical_typed_scope_envelope_record_type_mismatch")
        _unique_scopes(records, "typed_scope_fact")
        as_of = _timestamp(raw.get("as_of_utc"), "as_of_utc")
        as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if any(
            datetime.fromisoformat(item.as_of_utc.replace("Z", "+00:00")) > as_of_dt
            for item in records
        ):
            raise ContractError("canonical_typed_scope_record_after_envelope_asof")
        supplied = _sha(
            raw.get("content_sha256"), "typed_scope_envelope_content_sha256"
        )
        if supplied != content_sha256(
            {key: value for key, value in raw.items() if key != "content_sha256"}
        ):
            raise ContractError("canonical_typed_scope_envelope_content_sha256_mismatch")
        return cls(
            as_of_utc=as_of,
            builder_id=_text(raw.get("builder_id"), "builder_id"),
            scope_type=scope_type,
            records=records,
            content_sha256=supplied,
        )

    def to_mapping(self) -> dict[str, Any]:
        identity = {
            "schema": TYPED_SCOPE_ENVELOPE_SCHEMA,
            "as_of_utc": self.as_of_utc,
            "builder_id": self.builder_id,
            "scope_type": self.scope_type,
            "records": [item.to_mapping() for item in self.records],
            "production_no_live": True,
            "future_data_counts_as_oos": False,
        }
        return {**identity, "content_sha256": content_sha256(identity)}


@dataclass(frozen=True)
class CanonicalActivationSupersessionFact:
    supersession_id: str
    station_id: str
    model_family: str
    lead_bucket: str
    run_cycle: str
    predecessor_activation_content_sha256: str
    predecessor_generation: int
    successor_activation_content_sha256: str
    successor_generation: int
    candidate_activation_content_sha256s: tuple[str, ...]
    candidate_activation_set_sha256: str
    semantic_equivalence_sha256: str
    reason: str
    authority_as_of_utc: str
    authority_scope: str
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalActivationSupersessionFact":
        _strict_keys(
            raw, ACTIVATION_SUPERSESSION_FACT_FIELDS, "activation_supersession_fact"
        )
        family = _text(raw.get("model_family"), "model_family")
        if family not in MODEL_FAMILIES:
            raise ContractError("canonical_activation_supersession_family_invalid")
        predecessor_generation = raw.get("predecessor_generation")
        successor_generation = raw.get("successor_generation")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (predecessor_generation, successor_generation)
        ):
            raise ContractError("canonical_activation_supersession_generation_invalid")
        if successor_generation <= predecessor_generation:
            raise ContractError("canonical_activation_supersession_generation_order_invalid")
        raw_candidates = raw.get("candidate_activation_content_sha256s")
        if not isinstance(raw_candidates, list) or len(raw_candidates) < 2:
            raise ContractError("canonical_activation_supersession_candidates_invalid")
        candidates = tuple(_sha(item, "candidate_activation_content_sha256") for item in raw_candidates)
        if list(candidates) != sorted(set(candidates)):
            raise ContractError("canonical_activation_supersession_candidates_order_invalid")
        predecessor = _sha(
            raw.get("predecessor_activation_content_sha256"),
            "predecessor_activation_content_sha256",
        )
        successor = _sha(
            raw.get("successor_activation_content_sha256"),
            "successor_activation_content_sha256",
        )
        if predecessor == successor or {predecessor, successor} - set(candidates):
            raise ContractError("canonical_activation_supersession_candidate_set_invalid")
        candidate_set_sha = _sha(
            raw.get("candidate_activation_set_sha256"),
            "candidate_activation_set_sha256",
        )
        if candidate_set_sha != content_sha256(list(candidates)):
            raise ContractError("canonical_activation_supersession_candidate_set_hash_mismatch")
        reason = _text(raw.get("reason"), "reason")
        if reason not in ACTIVATION_SUPERSESSION_REASONS:
            raise ContractError("canonical_activation_supersession_reason_invalid")
        authority = _text(raw.get("authority_scope"), "authority_scope")
        if authority != "PRODUCTION_NO_LIVE_ONLY":
            raise ContractError("canonical_activation_supersession_authority_invalid")
        result = cls(
            supersession_id=_text(raw.get("supersession_id"), "supersession_id"),
            station_id=_text(raw.get("station_id"), "station_id").upper(),
            model_family=family,
            lead_bucket=_text(raw.get("lead_bucket"), "lead_bucket"),
            run_cycle=_text(raw.get("run_cycle"), "run_cycle"),
            predecessor_activation_content_sha256=predecessor,
            predecessor_generation=predecessor_generation,
            successor_activation_content_sha256=successor,
            successor_generation=successor_generation,
            candidate_activation_content_sha256s=candidates,
            candidate_activation_set_sha256=candidate_set_sha,
            semantic_equivalence_sha256=_sha(
                raw.get("semantic_equivalence_sha256"),
                "semantic_equivalence_sha256",
            ),
            reason=reason,
            authority_as_of_utc=_timestamp(
                raw.get("authority_as_of_utc"), "authority_as_of_utc"
            ),
            authority_scope=authority,
            content_sha256=_sha(
                raw.get("content_sha256"), "activation_supersession_content_sha256"
            ),
        )
        _verify_hash(result.to_mapping(), label="activation_supersession_fact")
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "supersession_id": self.supersession_id,
            "station_id": self.station_id,
            "model_family": self.model_family,
            "lead_bucket": self.lead_bucket,
            "run_cycle": self.run_cycle,
            "predecessor_activation_content_sha256": self.predecessor_activation_content_sha256,
            "predecessor_generation": self.predecessor_generation,
            "successor_activation_content_sha256": self.successor_activation_content_sha256,
            "successor_generation": self.successor_generation,
            "candidate_activation_content_sha256s": list(self.candidate_activation_content_sha256s),
            "candidate_activation_set_sha256": self.candidate_activation_set_sha256,
            "semantic_equivalence_sha256": self.semantic_equivalence_sha256,
            "reason": self.reason,
            "authority_as_of_utc": self.authority_as_of_utc,
            "authority_scope": self.authority_scope,
            "content_sha256": self.content_sha256,
        }

    @property
    def scope_key(self) -> str:
        return "|".join(
            (
                self.station_id,
                self.model_family,
                self.lead_bucket,
                self.run_cycle,
                self.predecessor_activation_content_sha256,
            )
        )


@dataclass(frozen=True)
class CanonicalActivationSupersessionContract:
    as_of_utc: str
    builder_id: str
    supersession_facts: tuple[CanonicalActivationSupersessionFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalActivationSupersessionContract":
        _strict_keys(
            raw,
            ACTIVATION_SUPERSESSION_CONTRACT_FIELDS,
            "activation_supersession_contract",
        )
        if raw.get("schema") != ACTIVATION_SUPERSESSION_CONTRACT_SCHEMA:
            raise ContractError("canonical_activation_supersession_contract_schema_invalid")
        raw_facts = raw.get("supersession_facts")
        if (
            not isinstance(raw_facts, list)
            or not raw_facts
            or any(not isinstance(item, Mapping) for item in raw_facts)
        ):
            raise ContractError("canonical_activation_supersession_contract_facts_required")
        facts = tuple(
            CanonicalActivationSupersessionFact.from_mapping(item) for item in raw_facts
        )
        _unique_scopes(facts, "activation_supersession_fact")
        as_of = _timestamp(raw.get("as_of_utc"), "as_of_utc")
        if any(item.authority_as_of_utc > as_of for item in facts):
            raise ContractError("canonical_activation_supersession_fact_after_contract_asof")
        supplied = _sha(
            raw.get("content_sha256"),
            "activation_supersession_contract_content_sha256",
        )
        if supplied != content_sha256(
            {key: value for key, value in raw.items() if key != "content_sha256"}
        ):
            raise ContractError("canonical_activation_supersession_contract_hash_mismatch")
        return cls(
            as_of_utc=as_of,
            builder_id=_text(raw.get("builder_id"), "builder_id"),
            supersession_facts=facts,
            content_sha256=supplied,
        )

    def to_mapping(self) -> dict[str, Any]:
        identity = {
            "schema": ACTIVATION_SUPERSESSION_CONTRACT_SCHEMA,
            "as_of_utc": self.as_of_utc,
            "builder_id": self.builder_id,
            "supersession_facts": [item.to_mapping() for item in self.supersession_facts],
        }
        return {**identity, "content_sha256": content_sha256(identity)}


@dataclass(frozen=True)
class CanonicalRepublisherOOSCohortRow:
    event_date: str
    partition: str
    forecast_daily_max_c: float
    truth_value_c: float
    model_input_content_sha256: str
    source_receipt_sha256: str
    canonical_truth_fact_content_sha256: str
    awc_truth_id: str
    awc_truth_content_sha256: str
    awc_truth_revision_no: int
    awc_truth_asof_utc: str
    station_day_end_utc: str
    case_contract_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalRepublisherOOSCohortRow":
        _strict_keys(
            raw, REPUBLISHER_OOS_COHORT_ROW_FIELDS, "republisher_oos_cohort_row"
        )
        partition = _text(raw.get("partition"), "republisher_oos_partition")
        if partition not in {"train", "holdout"}:
            raise ContractError("canonical_republisher_oos_partition_invalid")
        revision = raw.get("awc_truth_revision_no")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ContractError("canonical_republisher_oos_truth_revision_invalid")
        as_of = _timestamp(raw.get("awc_truth_asof_utc"), "awc_truth_asof_utc")
        day_end = _timestamp(raw.get("station_day_end_utc"), "station_day_end_utc")
        if as_of < day_end:
            raise ContractError("canonical_republisher_oos_truth_before_day_end")
        identity = {
            "event_date": _event_date(raw.get("event_date")),
            "partition": partition,
            "forecast_daily_max_c": _number(
                raw.get("forecast_daily_max_c"), "forecast_daily_max_c"
            ),
            "truth_value_c": _number(raw.get("truth_value_c"), "truth_value_c"),
            "model_input_content_sha256": _sha(
                raw.get("model_input_content_sha256"),
                "model_input_content_sha256",
            ),
            "source_receipt_sha256": _sha(
                raw.get("source_receipt_sha256"), "source_receipt_sha256"
            ),
            "canonical_truth_fact_content_sha256": _sha(
                raw.get("canonical_truth_fact_content_sha256"),
                "canonical_truth_fact_content_sha256",
            ),
            "awc_truth_id": _text(raw.get("awc_truth_id"), "awc_truth_id"),
            "awc_truth_content_sha256": _sha(
                raw.get("awc_truth_content_sha256"), "awc_truth_content_sha256"
            ),
            "awc_truth_revision_no": revision,
            "awc_truth_asof_utc": as_of,
            "station_day_end_utc": day_end,
        }
        supplied = _sha(raw.get("case_contract_sha256"), "case_contract_sha256")
        if supplied != content_sha256(identity):
            raise ContractError("canonical_republisher_oos_case_hash_mismatch")
        return cls(**identity, case_contract_sha256=supplied)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "event_date": self.event_date,
            "partition": self.partition,
            "forecast_daily_max_c": self.forecast_daily_max_c,
            "truth_value_c": self.truth_value_c,
            "model_input_content_sha256": self.model_input_content_sha256,
            "source_receipt_sha256": self.source_receipt_sha256,
            "canonical_truth_fact_content_sha256": (
                self.canonical_truth_fact_content_sha256
            ),
            "awc_truth_id": self.awc_truth_id,
            "awc_truth_content_sha256": self.awc_truth_content_sha256,
            "awc_truth_revision_no": self.awc_truth_revision_no,
            "awc_truth_asof_utc": self.awc_truth_asof_utc,
            "station_day_end_utc": self.station_day_end_utc,
            "case_contract_sha256": self.case_contract_sha256,
        }


def _oos_metrics(raw: Any, label: str) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        raise ContractError(f"canonical_{label}_mapping_required")
    _strict_keys(raw, _OOS_METRIC_FIELDS, label)
    return {
        key: _number(raw.get(key), f"{label}_{key}")
        for key in sorted(_OOS_METRIC_FIELDS)
    }


@dataclass(frozen=True)
class CanonicalRepublisherOOSArtifactFact:
    artifact_id: str
    station_id: str
    upstream_model_id: str
    republisher_product_id: str
    independence_family_id: str
    transport_provider_id: str
    model_family: str
    source_class: str
    authority_scope: str
    settlement_timezone: str
    settlement_definition: str
    lead_bucket: str
    run_cycle: str
    run_cycle_scope: str
    season_bin: str
    forecast_extraction_version: str
    artifact_method_version: str
    oos_protocol_version: str
    train_n: int
    holdout_n: int
    cohort_rows: tuple[CanonicalRepublisherOOSCohortRow, ...]
    train_case_set_sha256: str
    holdout_case_set_sha256: str
    strict_oos_cohort_sha256: str
    evaluation_content_sha256: str
    calibration_parameters: Mapping[str, float]
    model_metrics: Mapping[str, float]
    climatology_baseline_metrics: Mapping[str, float]
    state: str
    native_arrival_indexed: bool
    p1_eligible: bool
    formal_live_authority: bool
    future_event_count: int
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalRepublisherOOSArtifactFact":
        _strict_keys(
            raw,
            REPUBLISHER_OOS_ARTIFACT_FACT_FIELDS,
            "republisher_oos_artifact_fact",
        )
        if raw.get("model_family") != "global_deterministic":
            raise ContractError("canonical_republisher_oos_model_family_invalid")
        if raw.get("source_class") != "aggregator_republisher":
            raise ContractError("canonical_republisher_oos_source_class_invalid")
        if raw.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY":
            raise ContractError("canonical_republisher_oos_authority_invalid")
        if raw.get("transport_provider_id") != "OPEN_METEO":
            raise ContractError("canonical_republisher_oos_transport_invalid")
        if raw.get("run_cycle") != "AGGREGATOR_UNKNOWN_NATIVE_CYCLE":
            raise ContractError("canonical_republisher_oos_run_cycle_invalid")
        if raw.get("run_cycle_scope") != "REPUBLISHER_HISTORICAL_DAY_SCOPE":
            raise ContractError("canonical_republisher_oos_run_cycle_scope_invalid")
        if raw.get("state") != "PRODUCTION_NO_LIVE_OOS_PASS":
            raise ContractError("canonical_republisher_oos_state_invalid")
        if any(
            _bool(raw.get(field), field)
            for field in (
                "native_arrival_indexed",
                "p1_eligible",
                "formal_live_authority",
            )
        ):
            raise ContractError("canonical_republisher_oos_authority_escalation")
        future_count = raw.get("future_event_count")
        train_n = raw.get("train_n")
        holdout_n = raw.get("holdout_n")
        if future_count != 0:
            raise ContractError("canonical_republisher_oos_future_event_forbidden")
        if train_n != 20 or holdout_n != 8:
            raise ContractError("canonical_republisher_oos_cohort_size_invalid")
        raw_rows = raw.get("cohort_rows")
        if (
            not isinstance(raw_rows, list)
            or len(raw_rows) != train_n + holdout_n
            or any(not isinstance(item, Mapping) for item in raw_rows)
        ):
            raise ContractError("canonical_republisher_oos_cohort_rows_invalid")
        rows = tuple(
            CanonicalRepublisherOOSCohortRow.from_mapping(item)
            for item in raw_rows
        )
        dates = [item.event_date for item in rows]
        if dates != sorted(set(dates)):
            raise ContractError("canonical_republisher_oos_cohort_dates_invalid")
        if [item.partition for item in rows] != ["train"] * 20 + ["holdout"] * 8:
            raise ContractError("canonical_republisher_oos_partition_order_invalid")
        train_rows = rows[:20]
        holdout_rows = rows[20:]
        if train_rows[-1].event_date >= holdout_rows[0].event_date:
            raise ContractError("canonical_republisher_oos_partition_dates_overlap")
        train_set_sha = _sha(
            raw.get("train_case_set_sha256"), "train_case_set_sha256"
        )
        holdout_set_sha = _sha(
            raw.get("holdout_case_set_sha256"), "holdout_case_set_sha256"
        )
        if train_set_sha != content_sha256(
            [item.case_contract_sha256 for item in train_rows]
        ):
            raise ContractError("canonical_republisher_oos_train_case_set_hash_mismatch")
        if holdout_set_sha != content_sha256(
            [item.case_contract_sha256 for item in holdout_rows]
        ):
            raise ContractError("canonical_republisher_oos_holdout_case_set_hash_mismatch")
        scope_identity = {
            "station_id": str(raw.get("station_id") or "").strip().upper(),
            "upstream_model_id": str(raw.get("upstream_model_id") or "").strip().upper(),
            "republisher_product_id": str(raw.get("republisher_product_id") or "").strip(),
            "independence_family_id": str(raw.get("independence_family_id") or "").strip(),
            "source_class": "aggregator_republisher",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "settlement_timezone": str(raw.get("settlement_timezone") or "").strip(),
            "settlement_definition": str(raw.get("settlement_definition") or "").strip(),
            "lead_bucket": str(raw.get("lead_bucket") or "").strip(),
            "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
            "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_SCOPE",
            "season_bin": str(raw.get("season_bin") or "").strip(),
            "forecast_extraction_version": str(raw.get("forecast_extraction_version") or "").strip(),
            "artifact_method_version": str(raw.get("artifact_method_version") or "").strip(),
            "oos_protocol_version": str(raw.get("oos_protocol_version") or "").strip(),
            "cohort_rows": [item.to_mapping() for item in rows],
        }
        cohort_sha = _sha(
            raw.get("strict_oos_cohort_sha256"), "strict_oos_cohort_sha256"
        )
        if cohort_sha != content_sha256(scope_identity):
            raise ContractError("canonical_republisher_oos_cohort_hash_mismatch")
        raw_parameters = raw.get("calibration_parameters")
        if not isinstance(raw_parameters, Mapping):
            raise ContractError("canonical_calibration_parameters_mapping_required")
        _strict_keys(
            raw_parameters,
            _OOS_CALIBRATION_PARAMETER_FIELDS,
            "calibration_parameters",
        )
        calibration_parameters = {
            key: _number(raw_parameters.get(key), f"calibration_parameters_{key}")
            for key in sorted(_OOS_CALIBRATION_PARAMETER_FIELDS)
        }
        expected_bias = sum(
            item.truth_value_c - item.forecast_daily_max_c for item in train_rows
        ) / len(train_rows)
        expected_climate = sum(item.truth_value_c for item in train_rows) / len(train_rows)
        if not math.isclose(
            calibration_parameters["bias_c"], expected_bias, rel_tol=0.0, abs_tol=1e-12
        ) or not math.isclose(
            calibration_parameters["train_climatology_tmax_c"],
            expected_climate,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ContractError("canonical_republisher_oos_parameters_mismatch")
        model_metrics = _oos_metrics(raw.get("model_metrics"), "model_metrics")
        baseline_metrics = _oos_metrics(
            raw.get("climatology_baseline_metrics"),
            "climatology_baseline_metrics",
        )

        def _computed_metrics(errors: Sequence[float]) -> dict[str, float]:
            return {
                "mae_c": sum(abs(value) for value in errors) / len(errors),
                "mean_error_c": sum(errors) / len(errors),
                "rmse_c": math.sqrt(
                    sum(value * value for value in errors) / len(errors)
                ),
            }

        expected_model_metrics = _computed_metrics(
            [
                item.forecast_daily_max_c + expected_bias - item.truth_value_c
                for item in holdout_rows
            ]
        )
        expected_baseline_metrics = _computed_metrics(
            [expected_climate - item.truth_value_c for item in holdout_rows]
        )
        if any(
            not math.isclose(model_metrics[key], expected_model_metrics[key], rel_tol=0.0, abs_tol=1e-12)
            for key in _OOS_METRIC_FIELDS
        ) or any(
            not math.isclose(baseline_metrics[key], expected_baseline_metrics[key], rel_tol=0.0, abs_tol=1e-12)
            for key in _OOS_METRIC_FIELDS
        ):
            raise ContractError("canonical_republisher_oos_metrics_mismatch")
        artifact_natural_identity = {
            key: scope_identity[key]
            for key in (
                "station_id",
                "upstream_model_id",
                "republisher_product_id",
                "independence_family_id",
                "source_class",
                "authority_scope",
                "settlement_timezone",
                "settlement_definition",
                "lead_bucket",
                "run_cycle",
                "run_cycle_scope",
                "season_bin",
                "forecast_extraction_version",
                "artifact_method_version",
                "oos_protocol_version",
            )
        }
        artifact_natural_identity.update(
            {
                "train_case_set_sha256": train_set_sha,
                "holdout_case_set_sha256": holdout_set_sha,
            }
        )
        expected_artifact_id = (
            "republisher-historical-oos-"
            + content_sha256(artifact_natural_identity)[:40]
        )
        if raw.get("artifact_id") != expected_artifact_id:
            raise ContractError("canonical_republisher_oos_artifact_id_mismatch")
        result = cls(
            artifact_id=expected_artifact_id,
            station_id=_text(raw.get("station_id"), "station_id").upper(),
            upstream_model_id=_text(
                raw.get("upstream_model_id"), "upstream_model_id"
            ).upper(),
            republisher_product_id=_text(
                raw.get("republisher_product_id"), "republisher_product_id"
            ),
            independence_family_id=_text(
                raw.get("independence_family_id"), "independence_family_id"
            ),
            transport_provider_id="OPEN_METEO",
            model_family="global_deterministic",
            source_class="aggregator_republisher",
            authority_scope="PRODUCTION_NO_LIVE_ONLY",
            settlement_timezone=_timezone(raw.get("settlement_timezone")),
            settlement_definition=_text(
                raw.get("settlement_definition"), "settlement_definition"
            ),
            lead_bucket=_text(raw.get("lead_bucket"), "lead_bucket"),
            run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
            run_cycle_scope="REPUBLISHER_HISTORICAL_DAY_SCOPE",
            season_bin=_text(raw.get("season_bin"), "season_bin"),
            forecast_extraction_version=_text(
                raw.get("forecast_extraction_version"),
                "forecast_extraction_version",
            ),
            artifact_method_version=_text(
                raw.get("artifact_method_version"), "artifact_method_version"
            ),
            oos_protocol_version=_text(
                raw.get("oos_protocol_version"), "oos_protocol_version"
            ),
            train_n=train_n,
            holdout_n=holdout_n,
            cohort_rows=rows,
            train_case_set_sha256=train_set_sha,
            holdout_case_set_sha256=holdout_set_sha,
            strict_oos_cohort_sha256=cohort_sha,
            evaluation_content_sha256=_sha(
                raw.get("evaluation_content_sha256"),
                "evaluation_content_sha256",
            ),
            calibration_parameters=calibration_parameters,
            model_metrics=model_metrics,
            climatology_baseline_metrics=baseline_metrics,
            state="PRODUCTION_NO_LIVE_OOS_PASS",
            native_arrival_indexed=False,
            p1_eligible=False,
            formal_live_authority=False,
            future_event_count=0,
            content_sha256=_sha(
                raw.get("content_sha256"),
                "republisher_oos_artifact_content_sha256",
            ),
        )
        _verify_hash(result.to_mapping(), label="republisher_oos_artifact_fact")
        return result

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "station_id": self.station_id,
            "upstream_model_id": self.upstream_model_id,
            "republisher_product_id": self.republisher_product_id,
            "independence_family_id": self.independence_family_id,
            "transport_provider_id": self.transport_provider_id,
            "model_family": self.model_family,
            "source_class": self.source_class,
            "authority_scope": self.authority_scope,
            "settlement_timezone": self.settlement_timezone,
            "settlement_definition": self.settlement_definition,
            "lead_bucket": self.lead_bucket,
            "run_cycle": self.run_cycle,
            "run_cycle_scope": self.run_cycle_scope,
            "season_bin": self.season_bin,
            "forecast_extraction_version": self.forecast_extraction_version,
            "artifact_method_version": self.artifact_method_version,
            "oos_protocol_version": self.oos_protocol_version,
            "train_n": self.train_n,
            "holdout_n": self.holdout_n,
            "cohort_rows": [item.to_mapping() for item in self.cohort_rows],
            "train_case_set_sha256": self.train_case_set_sha256,
            "holdout_case_set_sha256": self.holdout_case_set_sha256,
            "strict_oos_cohort_sha256": self.strict_oos_cohort_sha256,
            "evaluation_content_sha256": self.evaluation_content_sha256,
            "calibration_parameters": dict(self.calibration_parameters),
            "model_metrics": dict(self.model_metrics),
            "climatology_baseline_metrics": dict(
                self.climatology_baseline_metrics
            ),
            "state": self.state,
            "native_arrival_indexed": self.native_arrival_indexed,
            "p1_eligible": self.p1_eligible,
            "formal_live_authority": self.formal_live_authority,
            "future_event_count": self.future_event_count,
            "content_sha256": self.content_sha256,
        }

    @property
    def scope_key(self) -> str:
        return "|".join(
            (
                self.station_id,
                self.upstream_model_id,
                self.republisher_product_id,
                self.independence_family_id,
                self.source_class,
                self.authority_scope,
                self.settlement_timezone,
                self.settlement_definition,
                self.lead_bucket,
                self.run_cycle,
                self.run_cycle_scope,
                self.season_bin,
                self.forecast_extraction_version,
                self.artifact_method_version,
                self.oos_protocol_version,
                self.train_case_set_sha256,
                self.holdout_case_set_sha256,
            )
        )


@dataclass(frozen=True)
class CanonicalRepublisherOOSArtifactContract:
    as_of_utc: str
    builder_id: str
    artifact_facts: tuple[CanonicalRepublisherOOSArtifactFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalRepublisherOOSArtifactContract":
        _strict_keys(
            raw,
            REPUBLISHER_OOS_ARTIFACT_CONTRACT_FIELDS,
            "republisher_oos_artifact_contract",
        )
        if raw.get("schema") != REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA:
            raise ContractError("canonical_republisher_oos_contract_schema_invalid")
        raw_facts = raw.get("artifact_facts")
        if (
            not isinstance(raw_facts, list)
            or not raw_facts
            or any(not isinstance(item, Mapping) for item in raw_facts)
        ):
            raise ContractError("canonical_republisher_oos_contract_facts_required")
        facts = tuple(
            CanonicalRepublisherOOSArtifactFact.from_mapping(item)
            for item in raw_facts
        )
        _unique_scopes(facts, "republisher_oos_artifact_fact")
        as_of = _timestamp(raw.get("as_of_utc"), "as_of_utc")
        if any(
            item.awc_truth_asof_utc > as_of
            for fact in facts
            for item in fact.cohort_rows
        ):
            raise ContractError("canonical_republisher_oos_truth_after_contract_asof")
        supplied = _sha(
            raw.get("content_sha256"),
            "republisher_oos_artifact_contract_content_sha256",
        )
        if supplied != content_sha256(
            {key: value for key, value in raw.items() if key != "content_sha256"}
        ):
            raise ContractError("canonical_republisher_oos_contract_hash_mismatch")
        return cls(
            as_of_utc=as_of,
            builder_id=_text(raw.get("builder_id"), "builder_id"),
            artifact_facts=facts,
            content_sha256=supplied,
        )

    def to_mapping(self) -> dict[str, Any]:
        identity = {
            "schema": REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA,
            "as_of_utc": self.as_of_utc,
            "builder_id": self.builder_id,
            "artifact_facts": [item.to_mapping() for item in self.artifact_facts],
        }
        return {**identity, "content_sha256": content_sha256(identity)}


@dataclass(frozen=True)
class CanonicalContract:
    as_of_utc: str
    builder_id: str
    model_facts: tuple[CanonicalModelFact, ...]
    ensemble_member_facts: tuple[CanonicalEnsembleMemberFact, ...]
    native_arrival_facts: tuple[CanonicalNativeArrivalFact, ...]
    ensemble_member_receipt_manifest_facts: tuple[
        CanonicalEnsembleMemberReceiptManifestFact, ...
    ]
    ensemble_member_arrival_binding_facts: tuple[
        CanonicalEnsembleMemberArrivalBindingFact, ...
    ]
    truth_facts: tuple[CanonicalTruthFact, ...]
    residual_facts: tuple[CanonicalResidualFact, ...]
    activation_facts: tuple[CanonicalActivationFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalContract":
        _strict_keys(raw, TOP_LEVEL_FIELDS, "contract")
        if raw.get("schema") != SCHEMA or raw.get("contract_version") != CONTRACT_VERSION:
            raise ContractError("canonical_contract_schema_invalid")
        as_of = _timestamp(raw.get("as_of_utc"), "as_of_utc")
        builder_id = _text(raw.get("builder_id"), "builder_id")
        def _list(key: str) -> list[Mapping[str, Any]]:
            value = raw.get(key)
            if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
                raise ContractError(f"canonical_{key}_list_required")
            return list(value)
        model_facts = tuple(CanonicalModelFact.from_mapping(item) for item in _list("model_facts"))
        ensemble_member_facts = tuple(
            CanonicalEnsembleMemberFact.from_mapping(item)
            for item in _list("ensemble_member_facts")
        )
        native_arrival_facts = tuple(
            CanonicalNativeArrivalFact.from_mapping(item)
            for item in _list("native_arrival_facts")
        )
        ensemble_member_receipt_manifest_facts = tuple(
            CanonicalEnsembleMemberReceiptManifestFact.from_mapping(item)
            for item in _list("ensemble_member_receipt_manifest_facts")
        )
        ensemble_member_arrival_binding_facts = tuple(
            CanonicalEnsembleMemberArrivalBindingFact.from_mapping(item)
            for item in _list("ensemble_member_arrival_binding_facts")
        )
        truth_facts = tuple(CanonicalTruthFact.from_mapping(item) for item in _list("truth_facts"))
        residual_facts = tuple(CanonicalResidualFact.from_mapping(item) for item in _list("residual_facts"))
        activation_facts = tuple(CanonicalActivationFact.from_mapping(item) for item in _list("activation_facts"))
        if not (
            model_facts
            or ensemble_member_facts
            or native_arrival_facts
            or ensemble_member_receipt_manifest_facts
            or ensemble_member_arrival_binding_facts
            or truth_facts
            or residual_facts
            or activation_facts
        ):
            raise ContractError("canonical_contract_empty")
        _unique_scopes(model_facts, "model_fact")
        _unique_scopes(ensemble_member_facts, "ensemble_member_fact")
        _unique_scopes(native_arrival_facts, "native_arrival_fact")
        _unique_scopes(
            ensemble_member_receipt_manifest_facts,
            "ensemble_member_receipt_manifest_fact",
        )
        _unique_scopes(
            ensemble_member_arrival_binding_facts,
            "ensemble_member_arrival_binding_fact",
        )
        _unique_scopes(truth_facts, "truth_fact")
        _unique_scopes(residual_facts, "residual_fact")
        _unique_scopes(activation_facts, "activation_fact")
        members_by_hash = {
            item.content_sha256: item for item in ensemble_member_facts
        }
        arrivals_by_hash = {
            item.content_sha256: item for item in native_arrival_facts
        }
        manifests_by_hash = {
            item.content_sha256: item
            for item in ensemble_member_receipt_manifest_facts
        }
        manifests_by_member_hash = {
            item.ensemble_member_content_sha256: item
            for item in ensemble_member_receipt_manifest_facts
        }
        for manifest in ensemble_member_receipt_manifest_facts:
            member = members_by_hash.get(
                manifest.ensemble_member_content_sha256
            )
            if member is None:
                raise ContractError(
                    "canonical_ensemble_member_receipt_manifest_member_missing"
                )
            if member.source_class != "native_official":
                raise ContractError(
                    "canonical_ensemble_member_receipt_manifest_member_not_native"
                )
            if (
                manifest.source_raw_receipt_set_sha256
                != member.raw_receipt_set_sha256
            ):
                raise ContractError(
                    "canonical_ensemble_member_receipt_manifest_source_set_mismatch"
                )
            if any(
                ref.receipt_evidence_at_utc > as_of
                for ref in manifest.required_receipt_refs
            ):
                raise ContractError(
                    "canonical_ensemble_member_receipt_manifest_after_contract_asof"
                )
        packet_binding_coverage: dict[tuple[str, str], set[int]] = {}
        for binding in ensemble_member_arrival_binding_facts:
            member = members_by_hash.get(binding.ensemble_member_content_sha256)
            arrival = arrivals_by_hash.get(binding.native_arrival_content_sha256)
            manifest = manifests_by_hash.get(
                binding.receipt_manifest_content_sha256
            )
            if member is None:
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_member_missing"
                )
            if arrival is None:
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_arrival_missing"
                )
            if manifest is None:
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_manifest_missing"
                )
            if (
                manifests_by_member_hash.get(member.content_sha256)
                != manifest
            ):
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_manifest_member_mismatch"
                )
            if member.source_class != "native_official":
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_member_not_native"
                )
            if not (
                arrival.source_class == "native_official"
                and arrival.native_arrival_indexed
                and arrival.published_at_known
                and arrival.received_at_known
                and arrival.raw_object_sha256
            ):
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_arrival_not_indexed"
                )
            common = (
                member.ensemble_system_id,
                member.provider_id,
                member.run_init_utc,
                member.lead_bucket,
                member.run_cycle,
            )
            if common != (
                arrival.ensemble_system_id,
                arrival.provider_id,
                arrival.run_init_utc,
                arrival.lead_bucket,
                arrival.run_cycle,
            ):
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_arrival_scope_mismatch"
                )
            refs = manifest.required_receipt_refs
            if any(ordinal >= len(refs) for ordinal in binding.manifest_entry_ordinals):
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_ordinal_out_of_range"
                )
            covered_refs = tuple(
                refs[ordinal] for ordinal in binding.manifest_entry_ordinals
            )
            if any(
                ref.native_object_id != arrival.native_object_id
                or ref.raw_object_sha256 != arrival.raw_object_sha256
                for ref in covered_refs
            ):
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_manifest_object_mismatch"
                )
            if arrival.received_at_utc > as_of:
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_after_contract_asof"
                )
            coverage_key = (
                member.content_sha256,
                manifest.content_sha256,
            )
            covered = packet_binding_coverage.setdefault(coverage_key, set())
            if covered.intersection(binding.manifest_entry_ordinals):
                raise ContractError(
                    "canonical_ensemble_member_arrival_binding_ordinal_overlap"
                )
            covered.update(binding.manifest_entry_ordinals)
        models_by_hash = {item.content_sha256: item for item in model_facts}
        truths_by_id = {item.truth_id: item for item in truth_facts}
        for residual in residual_facts:
            model = models_by_hash.get(residual.model_fact_content_sha256)
            truth = truths_by_id.get(residual.truth_id)
            if model is None or truth is None:
                raise ContractError("canonical_residual_lineage_missing")
            if (model.station_id, model.model_id or model.ensemble_system_id, model.event_date, model.lead_bucket, model.run_cycle) != (
                residual.station_id, residual.model_id, residual.event_date, residual.lead_bucket, residual.run_cycle
            ) or truth.station_id != residual.station_id or truth.event_date != residual.event_date:
                raise ContractError("canonical_residual_scope_mismatch")
            if model.settlement_timezone != truth.settlement_timezone:
                raise ContractError("canonical_residual_settlement_timezone_mismatch")
        # An activation is not an independent label: its candidate mask must
        # be backed by the exact model facts in this same contract scope.
        # Without this check a caller could append a valid-looking activation
        # whose hashes point at an unrelated/old packet and silently bypass
        # the one-lineage rule.
        for activation in activation_facts:
            scoped_models = tuple(
                item
                for item in model_facts
                if item.station_id == activation.station_id
                and item.model_family == activation.model_family
                and item.lead_bucket == activation.lead_bucket
                and item.run_cycle == activation.run_cycle
            )
            if not scoped_models:
                raise ContractError("canonical_activation_model_lineage_missing")
            available_ids = {
                item.model_id or item.ensemble_system_id
                for item in scoped_models
            }
            candidate_ids = set(activation.candidate_models)
            if candidate_ids != available_ids:
                raise ContractError("canonical_activation_model_set_lineage_mismatch")
            if not set(activation.selected_models).issubset(available_ids):
                raise ContractError("canonical_activation_selected_model_lineage_missing")
            if any(item.oos_role != activation.oos_role for item in scoped_models):
                raise ContractError("canonical_activation_oos_role_lineage_mismatch")
            if activation.authority_scope == "FORMAL_NATIVE_ELIGIBLE":
                if any(
                    item.source_class != "native_official"
                    or not item.formal_live_authority
                    for item in scoped_models
                ):
                    raise ContractError("canonical_activation_formal_lineage_mismatch")
        identity = {key: value for key, value in raw.items() if key != "content_sha256"}
        supplied = _sha(raw.get("content_sha256"), "contract_content_sha256")
        if supplied != content_sha256(identity):
            raise ContractError("canonical_contract_content_sha256_mismatch")
        # Arrival/member/manifest facts intentionally have no implicit
        # cross-link.  Every packet carrying a manifest or binding must replay
        # its exact endpoints; canonical_ingress then verifies that replay
        # against the same transaction snapshot before appending anything.
        return cls(
            as_of_utc=as_of,
            builder_id=builder_id,
            model_facts=model_facts,
            ensemble_member_facts=ensemble_member_facts,
            native_arrival_facts=native_arrival_facts,
            ensemble_member_receipt_manifest_facts=(
                ensemble_member_receipt_manifest_facts
            ),
            ensemble_member_arrival_binding_facts=(
                ensemble_member_arrival_binding_facts
            ),
            truth_facts=truth_facts,
            residual_facts=residual_facts,
            activation_facts=activation_facts,
            content_sha256=supplied,
        )

    def to_mapping(self) -> dict[str, Any]:
        identity = {
            "schema": SCHEMA,
            "contract_version": CONTRACT_VERSION,
            "as_of_utc": self.as_of_utc,
            "builder_id": self.builder_id,
            "model_facts": [item.to_mapping() for item in self.model_facts],
            "ensemble_member_facts": [
                item.to_mapping() for item in self.ensemble_member_facts
            ],
            "native_arrival_facts": [
                item.to_mapping() for item in self.native_arrival_facts
            ],
            "ensemble_member_receipt_manifest_facts": [
                item.to_mapping()
                for item in self.ensemble_member_receipt_manifest_facts
            ],
            "ensemble_member_arrival_binding_facts": [
                item.to_mapping()
                for item in self.ensemble_member_arrival_binding_facts
            ],
            "truth_facts": [item.to_mapping() for item in self.truth_facts],
            "residual_facts": [item.to_mapping() for item in self.residual_facts],
            "activation_facts": [item.to_mapping() for item in self.activation_facts],
        }
        return {**identity, "content_sha256": content_sha256(identity)}


def _unique_scopes(items: Sequence[Any], label: str) -> None:
    scopes = [item.scope_key for item in items]
    if len(scopes) != len(set(scopes)):
        raise ContractError(f"canonical_{label}_scope_duplicate")


def hash_model_fact(raw: Mapping[str, Any]) -> str:
    """Return the canonical content hash after strict validation."""

    return CanonicalModelFact.from_mapping(raw).content_sha256


__all__ = [
    "ACTIVATION_FACT_FIELDS",
    "AUTHORITY_SCOPES",
    "CanonicalActivationFact",
    "CanonicalContract",
    "CanonicalEnsembleMemberFact",
    "CanonicalEnsembleMemberArrivalBindingFact",
    "CanonicalEnsembleMemberReceiptManifestFact",
    "CanonicalEnsembleMemberReceiptRef",
    "CanonicalModelFact",
    "CanonicalNativeArrivalFact",
    "CanonicalRepublisherOOSArtifactContract",
    "CanonicalRepublisherOOSArtifactFact",
    "CanonicalRepublisherOOSCohortRow",
    "CanonicalResidualFact",
    "CanonicalTruthFact",
    "CONTRACT_VERSION",
    "EVIDENCE_ROLES",
    "ENSEMBLE_MEMBER_FACT_FIELDS",
    "ENSEMBLE_MEMBER_ARRIVAL_BINDING_FACT_FIELDS",
    "ENSEMBLE_MEMBER_RECEIPT_MANIFEST_FACT_FIELDS",
    "ENSEMBLE_MEMBER_RECEIPT_REF_FIELDS",
    "MODEL_FAMILIES",
    "MODEL_FACT_FIELDS",
    "NATIVE_ARRIVAL_FACT_FIELDS",
    "REPUBLISHER_OOS_ARTIFACT_CONTRACT_FIELDS",
    "REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA",
    "REPUBLISHER_OOS_ARTIFACT_FACT_FIELDS",
    "REPUBLISHER_OOS_COHORT_ROW_FIELDS",
    "RESIDUAL_FACT_FIELDS",
    "FORMAL_AWC_TRUTH_ROLE",
    "HISTORICAL_AWC_REPLAY_TRUTH_ROLE",
    "SCHEMA",
    "SOURCE_CLASSES",
    "TOP_LEVEL_FIELDS",
    "TRUTH_SOURCE_ROLES",
    "TRUTH_FACT_FIELDS",
    "hash_model_fact",
]
