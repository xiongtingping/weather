"""Pure validation primitive for an atomic-ledger execution version vector.

This module deliberately has no database, service, network, or gateway
dependency.  It freezes the exact evidence references that an eventual
atomic-ledger transaction must bind together.  The vector is content
addressed: changing any evidence digest, price target, or quote clock changes
the vector digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Iterator, Mapping

from .contracts import ContractError


SCHEMA = "bayes_basket_target_execution_version_vector_v2"
LEGACY_SCHEMA = "bayes_basket_target_execution_version_vector_v1"
DELTA_V2_ECONOMIC_POLICY_SCHEMA = "delta_basket_score_policy_artifact_v2"
NO_EXECUTION_POLICY_SCHEMA = "bayes_basket_target_no_execution_vector_policy_v1"
EXECUTION_VECTOR_POLICY = "EXECUTION"
NO_EXECUTION_VECTOR_POLICY = "NO_EXECUTION"
DIGEST_FIELDS = (
    "probability_sha256",
    "official_ws_quote_sha256",
    "inventory_sha256",
    "order_state_sha256",
    "station_policy_sha256",
    "cash_policy_decision_sha256",
    "economic_policy_artifact_sha256",
)
LEGACY_DIGEST_FIELDS = DIGEST_FIELDS[:-1] + ("strict_delta_artifact_sha256",)
CLOCK_FIELDS = ("quote_observed_at", "quote_expires_at")
IDENTITY_FIELDS = ("economic_policy_schema", "target_price_id")
LEGACY_IDENTITY_FIELDS = ("target_price_id",)
VERSION_FIELDS = DIGEST_FIELDS + IDENTITY_FIELDS + CLOCK_FIELDS
LEGACY_VERSION_FIELDS = (
    LEGACY_DIGEST_FIELDS + LEGACY_IDENTITY_FIELDS + CLOCK_FIELDS
)
OPTIONAL_FIELDS = ("schema", "vector_sha256")
ALLOWED_FIELDS = frozenset(VERSION_FIELDS + OPTIONAL_FIELDS)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_UNKNOWN_VALUES = frozenset(
    {
        "?",
        "unknown",
        "none",
        "null",
        "missing",
        "unavailable",
        "not_available",
        "n/a",
        "na",
        "pending",
    }
)


class ExecutionVersionVectorError(ContractError):
    """Raised when an execution evidence vector cannot be trusted."""


def _require_mapping(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ExecutionVersionVectorError("execution_version_vector_mapping_required")
    return raw


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ExecutionVersionVectorError(f"{name}_missing")
    result = value.strip()
    if not result or result.lower() in _UNKNOWN_VALUES:
        raise ExecutionVersionVectorError(f"{name}_missing_or_unknown")
    return result


def _require_sha256(name: str, value: Any) -> str:
    result = _require_text(name, value).lower()
    if _SHA256_RE.fullmatch(result) is None:
        raise ExecutionVersionVectorError(f"{name}_invalid_sha256")
    return result


def _require_timestamp(name: str, value: Any) -> tuple[str, datetime]:
    text = _require_text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionVersionVectorError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionVersionVectorError(f"{name}_timezone_required")
    # Canonicalize equivalent offsets to one UTC representation.  This keeps
    # the content digest stable for the same instant and avoids clock aliases.
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return normalized, parsed


def _canonical_digest(
    payload: Mapping[str, Any],
    *,
    schema: str = SCHEMA,
) -> str:
    encoded = json.dumps(
        {"schema": schema, **dict(payload)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ExecutionVersionVector(Mapping[str, str]):
    """Immutable, normalized evidence vector ready for an atomic bind."""

    probability_sha256: str
    official_ws_quote_sha256: str
    inventory_sha256: str
    order_state_sha256: str
    station_policy_sha256: str
    cash_policy_decision_sha256: str
    economic_policy_artifact_sha256: str
    economic_policy_schema: str
    target_price_id: str
    quote_observed_at: str
    quote_expires_at: str
    vector_sha256: str

    @property
    def schema(self) -> str:
        return SCHEMA

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": SCHEMA,
            **{
                field: getattr(self, field)
                for field in VERSION_FIELDS + ("vector_sha256",)
            },
        }

    def __getitem__(self, key: str) -> str:
        if key == "schema":
            return SCHEMA
        if key not in VERSION_FIELDS and key != "vector_sha256":
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        yield from VERSION_FIELDS
        yield "vector_sha256"

    def __len__(self) -> int:
        return len(VERSION_FIELDS) + 1


@dataclass(frozen=True)
class LegacyExecutionVersionVector(Mapping[str, str]):
    """Read-only decoder shape for historical strict-delta vectors."""

    probability_sha256: str
    official_ws_quote_sha256: str
    inventory_sha256: str
    order_state_sha256: str
    station_policy_sha256: str
    cash_policy_decision_sha256: str
    strict_delta_artifact_sha256: str
    target_price_id: str
    quote_observed_at: str
    quote_expires_at: str
    vector_sha256: str

    @property
    def schema(self) -> str:
        return LEGACY_SCHEMA

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": LEGACY_SCHEMA,
            **{
                field: getattr(self, field)
                for field in LEGACY_VERSION_FIELDS + ("vector_sha256",)
            },
        }

    def __getitem__(self, key: str) -> str:
        if key == "schema":
            return LEGACY_SCHEMA
        if key not in LEGACY_VERSION_FIELDS and key != "vector_sha256":
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        yield from LEGACY_VERSION_FIELDS
        yield "vector_sha256"

    def __len__(self) -> int:
        return len(LEGACY_VERSION_FIELDS) + 1


@dataclass(frozen=True)
class NoExecutionVersionPolicy(Mapping[str, Any]):
    """Explicit vector policy for a cycle with no executable intent.

    This is deliberately a separate contract from an execution vector.  It
    carries no target price and no quote clock, so a blocked cycle cannot be
    made to look executable by inserting sentinel values.
    """

    policy: str
    reason_codes: tuple[str, ...]
    policy_sha256: str

    @property
    def schema(self) -> str:
        return NO_EXECUTION_POLICY_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": NO_EXECUTION_POLICY_SCHEMA,
            "policy": self.policy,
            "reason_codes": list(self.reason_codes),
            "policy_sha256": self.policy_sha256,
        }

    def __getitem__(self, key: str) -> Any:
        if key == "schema":
            return NO_EXECUTION_POLICY_SCHEMA
        if key == "policy":
            return self.policy
        if key == "reason_codes":
            return list(self.reason_codes)
        if key == "policy_sha256":
            return self.policy_sha256
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield from ("policy", "reason_codes", "policy_sha256")

    def __len__(self) -> int:
        return 3


def validate_execution_version_vector(
    raw: Mapping[str, Any],
) -> ExecutionVersionVector:
    """Validate, normalize, and content-address one complete vector.

    Missing, unknown, malformed, or extra fields are rejected.  The optional
    supplied ``vector_sha256`` is checked against the deterministic digest;
    callers may omit it when constructing a new vector because it is derived
    here.
    """

    source = _require_mapping(raw)
    unknown_fields = sorted(
        str(field) for field in source if field not in ALLOWED_FIELDS
    )
    if unknown_fields:
        raise ExecutionVersionVectorError(
            "execution_version_vector_unknown_fields:" + ",".join(unknown_fields)
        )
    if source.get("schema") != SCHEMA:
        raise ExecutionVersionVectorError("execution_version_vector_schema_invalid")

    normalized: dict[str, str] = {}
    for field in DIGEST_FIELDS:
        normalized[field] = _require_sha256(field, source.get(field))
    normalized["economic_policy_schema"] = _require_text(
        "economic_policy_schema", source.get("economic_policy_schema")
    )
    if normalized["economic_policy_schema"] != DELTA_V2_ECONOMIC_POLICY_SCHEMA:
        raise ExecutionVersionVectorError(
            "economic_policy_schema_not_delta_v2"
        )
    normalized["target_price_id"] = _require_text(
        "target_price_id", source.get("target_price_id")
    )
    observed_text, observed = _require_timestamp(
        "quote_observed_at", source.get("quote_observed_at")
    )
    expires_text, expires = _require_timestamp(
        "quote_expires_at", source.get("quote_expires_at")
    )
    if expires <= observed:
        raise ExecutionVersionVectorError("quote_expires_at_must_be_after_quote_observed_at")
    normalized["quote_observed_at"] = observed_text
    normalized["quote_expires_at"] = expires_text

    digest = _canonical_digest(normalized)
    supplied_digest = source.get("vector_sha256")
    if supplied_digest not in (None, ""):
        supplied = _require_sha256("vector_sha256", supplied_digest)
        if supplied != digest:
            raise ExecutionVersionVectorError(
                "execution_version_vector_content_address_mismatch"
            )

    return ExecutionVersionVector(**normalized, vector_sha256=digest)


def decode_legacy_execution_version_vector(
    raw: Mapping[str, Any],
) -> LegacyExecutionVersionVector:
    """Decode a historical v1 vector without exposing it to formal writers."""

    source = _require_mapping(raw)
    allowed = frozenset(LEGACY_VERSION_FIELDS + OPTIONAL_FIELDS)
    unknown_fields = sorted(str(field) for field in source if field not in allowed)
    if unknown_fields:
        raise ExecutionVersionVectorError(
            "legacy_execution_version_vector_unknown_fields:"
            + ",".join(unknown_fields)
        )
    if source.get("schema") != LEGACY_SCHEMA:
        raise ExecutionVersionVectorError(
            "legacy_execution_version_vector_schema_invalid"
        )
    normalized: dict[str, str] = {}
    for field in LEGACY_DIGEST_FIELDS:
        normalized[field] = _require_sha256(field, source.get(field))
    normalized["target_price_id"] = _require_text(
        "target_price_id", source.get("target_price_id")
    )
    observed_text, observed = _require_timestamp(
        "quote_observed_at", source.get("quote_observed_at")
    )
    expires_text, expires = _require_timestamp(
        "quote_expires_at", source.get("quote_expires_at")
    )
    if expires <= observed:
        raise ExecutionVersionVectorError(
            "quote_expires_at_must_be_after_quote_observed_at"
        )
    normalized["quote_observed_at"] = observed_text
    normalized["quote_expires_at"] = expires_text
    digest = _canonical_digest(normalized, schema=LEGACY_SCHEMA)
    supplied_digest = source.get("vector_sha256")
    if supplied_digest not in (None, ""):
        supplied = _require_sha256("vector_sha256", supplied_digest)
        if supplied != digest:
            raise ExecutionVersionVectorError(
                "legacy_execution_version_vector_content_address_mismatch"
            )
    return LegacyExecutionVersionVector(
        **normalized,
        vector_sha256=digest,
    )


def decode_execution_version_vector(
    raw: Mapping[str, Any],
) -> ExecutionVersionVector | LegacyExecutionVersionVector:
    """Explicitly dispatch v2 formal or v1 historical vector decoding."""

    schema = _require_mapping(raw).get("schema")
    if schema == SCHEMA:
        return validate_execution_version_vector(raw)
    if schema == LEGACY_SCHEMA:
        return decode_legacy_execution_version_vector(raw)
    raise ExecutionVersionVectorError("execution_version_vector_schema_invalid")


def build_execution_version_vector(raw: Mapping[str, Any]) -> ExecutionVersionVector:
    """Alias emphasizing that this is a pure constructor, not a DB writer."""

    return validate_execution_version_vector(raw)


def execution_version_vector_sha256(raw: Mapping[str, Any]) -> str:
    """Return the deterministic digest after full fail-closed validation."""

    return validate_execution_version_vector(raw).vector_sha256


def validate_no_execution_version_policy(
    raw: Mapping[str, Any],
) -> NoExecutionVersionPolicy:
    """Validate and content-address an explicit no-execution policy."""

    source = _require_mapping(raw)
    allowed = frozenset({"schema", "policy", "reason_codes", "reason", "policy_sha256"})
    unknown_fields = sorted(str(field) for field in source if field not in allowed)
    if unknown_fields:
        raise ExecutionVersionVectorError(
            "no_execution_policy_unknown_fields:" + ",".join(unknown_fields)
        )
    supplied_schema = source.get("schema")
    if supplied_schema not in (None, NO_EXECUTION_POLICY_SCHEMA):
        raise ExecutionVersionVectorError("no_execution_policy_schema_invalid")
    if source.get("policy") != NO_EXECUTION_VECTOR_POLICY:
        raise ExecutionVersionVectorError("no_execution_policy_invalid")
    raw_reasons = source.get("reason_codes")
    if raw_reasons is None and source.get("reason") not in (None, ""):
        raw_reasons = [source.get("reason")]
    if isinstance(raw_reasons, str) or not isinstance(raw_reasons, (list, tuple)):
        raise ExecutionVersionVectorError("no_execution_policy_reason_codes_required")
    reasons = tuple(_require_text("no_execution_reason_code", value) for value in raw_reasons)
    if not reasons or len(set(reasons)) != len(reasons):
        raise ExecutionVersionVectorError("no_execution_policy_reason_codes_invalid")
    identity = {
        "policy": NO_EXECUTION_VECTOR_POLICY,
        "reason_codes": list(reasons),
    }
    digest = _canonical_digest(identity, schema=NO_EXECUTION_POLICY_SCHEMA)
    supplied_digest = source.get("policy_sha256")
    if supplied_digest not in (None, ""):
        supplied = _require_sha256("policy_sha256", supplied_digest)
        if supplied != digest:
            raise ExecutionVersionVectorError("no_execution_policy_content_address_mismatch")
    return NoExecutionVersionPolicy(
        policy=NO_EXECUTION_VECTOR_POLICY,
        reason_codes=reasons,
        policy_sha256=digest,
    )


def build_no_execution_version_policy(
    raw: Mapping[str, Any],
) -> NoExecutionVersionPolicy:
    """Pure constructor for a zero-intent blocked-cycle policy."""

    return validate_no_execution_version_policy(raw)


__all__ = [
    "ALLOWED_FIELDS",
    "CLOCK_FIELDS",
    "DIGEST_FIELDS",
    "ExecutionVersionVector",
    "LegacyExecutionVersionVector",
    "ExecutionVersionVectorError",
    "EXECUTION_VECTOR_POLICY",
    "IDENTITY_FIELDS",
    "LEGACY_SCHEMA",
    "DELTA_V2_ECONOMIC_POLICY_SCHEMA",
    "NO_EXECUTION_POLICY_SCHEMA",
    "NO_EXECUTION_VECTOR_POLICY",
    "NoExecutionVersionPolicy",
    "SCHEMA",
    "VERSION_FIELDS",
    "build_execution_version_vector",
    "build_no_execution_version_policy",
    "decode_execution_version_vector",
    "decode_legacy_execution_version_vector",
    "execution_version_vector_sha256",
    "validate_no_execution_version_policy",
    "validate_execution_version_vector",
]
