"""Content-addressed quote freshness policy for exact execution vectors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .policy import POLICY_AUTHORITY_SCOPES


SCHEMA = "bayes_basket_quote_freshness_policy_v1"
MAX_QUOTE_AGE_HARD_LIMIT_SECONDS = 300.0


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> tuple[str, datetime]:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    normalized = parsed.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z"), normalized


@dataclass(frozen=True)
class QuoteFreshnessPolicy:
    policy_id: str
    max_quote_age_seconds: float
    valid_from_utc: str
    valid_until_utc: str
    authority_scope: str
    evidence_status: str
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "QuoteFreshnessPolicy":
        if not isinstance(raw, Mapping):
            raise ContractError("quote_freshness_policy_mapping_required")
        if raw.get("schema") != SCHEMA:
            raise ContractError("quote_freshness_policy_schema_invalid")
        policy_id = _text("quote_freshness_policy_id", raw.get("policy_id"))
        try:
            max_age = float(raw.get("max_quote_age_seconds"))
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "quote_freshness_max_age_invalid"
            ) from exc
        if (
            not math.isfinite(max_age)
            or max_age <= 0.0
            or max_age > MAX_QUOTE_AGE_HARD_LIMIT_SECONDS
        ):
            raise ContractError("quote_freshness_max_age_invalid")
        valid_from, valid_from_dt = _timestamp(
            "quote_freshness_valid_from_utc", raw.get("valid_from_utc")
        )
        valid_until, valid_until_dt = _timestamp(
            "quote_freshness_valid_until_utc", raw.get("valid_until_utc")
        )
        if valid_until_dt <= valid_from_dt:
            raise ContractError("quote_freshness_validity_window_invalid")
        authority_scope = _text(
            "quote_freshness_authority_scope", raw.get("authority_scope")
        ).upper()
        if authority_scope not in POLICY_AUTHORITY_SCOPES:
            raise ContractError("quote_freshness_authority_scope_invalid")
        evidence_status = _text(
            "quote_freshness_evidence_status", raw.get("evidence_status")
        )
        identity = {
            "schema": SCHEMA,
            "policy_id": policy_id,
            "max_quote_age_seconds": max_age,
            "valid_from_utc": valid_from,
            "valid_until_utc": valid_until,
            "authority_scope": authority_scope,
            "evidence_status": evidence_status,
        }
        expected = content_sha256(identity)
        supplied = _text(
            "quote_freshness_content_sha256", raw.get("content_sha256")
        ).lower()
        if supplied != expected:
            raise ContractError(
                "quote_freshness_policy_content_address_mismatch"
            )
        return cls(
            policy_id=policy_id,
            max_quote_age_seconds=max_age,
            valid_from_utc=valid_from,
            valid_until_utc=valid_until,
            authority_scope=authority_scope,
            evidence_status=evidence_status,
            content_sha256=expected,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "policy_id": self.policy_id,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "authority_scope": self.authority_scope,
            "evidence_status": self.evidence_status,
            "content_sha256": self.content_sha256,
        }

    def derive_expiry(
        self,
        *,
        quote_observed_at: str,
        required_authority_scope: str,
    ) -> str:
        observed_text, observed = _timestamp(
            "quote_observed_at", quote_observed_at
        )
        del observed_text
        valid_from = _timestamp(
            "quote_freshness_valid_from_utc", self.valid_from_utc
        )[1]
        valid_until = _timestamp(
            "quote_freshness_valid_until_utc", self.valid_until_utc
        )[1]
        required = _text(
            "required_quote_freshness_authority_scope",
            required_authority_scope,
        ).upper()
        if self.authority_scope != required:
            raise ContractError("quote_freshness_authority_scope_mismatch")
        if not valid_from <= observed < valid_until:
            raise ContractError("quote_freshness_policy_not_active")
        return (
            observed + timedelta(seconds=self.max_quote_age_seconds)
        ).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "MAX_QUOTE_AGE_HARD_LIMIT_SECONDS",
    "QuoteFreshnessPolicy",
    "SCHEMA",
]
