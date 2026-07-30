"""Exact active authority for cash-phase policy resolution.

Cash-phase policy rows are append-only candidates/artifacts.  They do not
become active merely because one row has the highest version or because a
query happens to return one row.  A caller must provide the exact active
manifest that names the policy content, station timezone, validity window,
and authority scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import sqlite3
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cash_phase import BasketCashPhasePolicyArtifact
from .contracts import ContractError
from .contracts import content_sha256


AUTHORITY_MANIFEST_SCHEMA = "basket_cash_phase_authority_manifest_v1"
FORMAL_AUTHORITY_SCOPE = "FORMAL_PROMOTED"
CANDIDATE_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"
AUTHORITY_SCOPES = frozenset({
    FORMAL_AUTHORITY_SCOPE,
    CANDIDATE_AUTHORITY_SCOPE,
})
FORMAL_POLICY_SCOPES = AUTHORITY_SCOPES
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_ROWS_PER_SCOPE = 4096


def _text(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"cash_phase_authority_{name}_missing")
    return text


def _utc(name: str, value: Any) -> str:
    parsed = _utc_datetime(name, value)
    return parsed.isoformat().replace("+00:00", "Z")


def _utc_datetime(name: str, value: Any) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"cash_phase_authority_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"cash_phase_authority_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timezone(name: str, value: Any) -> str:
    text = _text(name, value)
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError(f"cash_phase_authority_{name}_invalid") from exc
    return text


@dataclass(frozen=True)
class CashPhaseAuthorityManifest:
    """The one exact authority binding a cash-phase policy for a scope."""

    station_id: str
    policy_version: str
    artifact_sha256: str
    station_timezone: str
    valid_from_utc: str
    valid_until_utc: str
    authority_scope: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CashPhaseAuthorityManifest":
        if not isinstance(raw, Mapping):
            raise ContractError("cash_phase_authority_manifest_mapping_required")
        schema = raw.get("schema")
        if schema is not None and str(schema) != AUTHORITY_MANIFEST_SCHEMA:
            raise ContractError("cash_phase_authority_manifest_schema_invalid")
        station_id = _text("station_id", raw.get("station_id"))
        policy_version = _text("policy_version", raw.get("policy_version"))
        artifact_sha256 = _text("artifact_sha256", raw.get("artifact_sha256"))
        if _SHA256.fullmatch(artifact_sha256) is None:
            raise ContractError("cash_phase_authority_artifact_sha256_invalid")
        station_timezone = _timezone("station_timezone", raw.get("station_timezone"))
        valid_from = _utc("valid_from_utc", raw.get("valid_from_utc"))
        valid_until = _utc("valid_until_utc", raw.get("valid_until_utc"))
        if _utc_datetime("valid_from_utc", valid_from) >= _utc_datetime(
            "valid_until_utc",
            valid_until,
        ):
            raise ContractError("cash_phase_authority_validity_window_invalid")
        authority_scope = str(raw.get("authority_scope") or "").strip().upper()
        if authority_scope not in AUTHORITY_SCOPES:
            raise ContractError("cash_phase_authority_scope_invalid")
        result = cls(
            station_id=station_id,
            policy_version=policy_version,
            artifact_sha256=artifact_sha256,
            station_timezone=station_timezone,
            valid_from_utc=valid_from,
            valid_until_utc=valid_until,
            authority_scope=authority_scope,
        )
        supplied = raw.get("manifest_sha256")
        if supplied not in (None, "") and str(supplied) != result.manifest_sha256:
            raise ContractError("cash_phase_authority_manifest_content_mismatch")
        return result

    @classmethod
    def create(cls, **kwargs: Any) -> "CashPhaseAuthorityManifest":
        return cls.from_mapping({"schema": AUTHORITY_MANIFEST_SCHEMA, **kwargs})

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": AUTHORITY_MANIFEST_SCHEMA,
            "station_id": self.station_id,
            "policy_version": self.policy_version,
            "artifact_sha256": self.artifact_sha256,
            "station_timezone": self.station_timezone,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "authority_scope": self.authority_scope,
            "manifest_sha256": self.manifest_sha256,
        }

    @property
    def manifest_sha256(self) -> str:
        return content_sha256({
            "schema": AUTHORITY_MANIFEST_SCHEMA,
            "station_id": self.station_id,
            "policy_version": self.policy_version,
            "artifact_sha256": self.artifact_sha256,
            "station_timezone": self.station_timezone,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "authority_scope": self.authority_scope,
        })


def _manifest(raw: CashPhaseAuthorityManifest | Mapping[str, Any] | None) -> CashPhaseAuthorityManifest:
    if raw is None:
        raise ContractError("cash_phase_authority_manifest_required")
    return raw if isinstance(raw, CashPhaseAuthorityManifest) else CashPhaseAuthorityManifest.from_mapping(raw)


def _policy(
    raw: BasketCashPhasePolicyArtifact | Mapping[str, Any],
) -> BasketCashPhasePolicyArtifact:
    return (
        raw
        if isinstance(raw, BasketCashPhasePolicyArtifact)
        else BasketCashPhasePolicyArtifact.from_mapping(raw)
    )


def _overlaps(
    left: BasketCashPhasePolicyArtifact,
    right: BasketCashPhasePolicyArtifact,
) -> bool:
    return (
        _utc_datetime("valid_from_utc", left.valid_from_utc)
        < _utc_datetime("valid_until_utc", right.valid_until_utc)
        and _utc_datetime("valid_from_utc", right.valid_from_utc)
        < _utc_datetime("valid_until_utc", left.valid_until_utc)
    )


def cash_phase_authority_candidates(
    artifacts: Sequence[BasketCashPhasePolicyArtifact | Mapping[str, Any]],
    *,
    station_id: str,
    station_timezone: str,
    observed_at_utc: str,
    observed_cash_phase: str | None,
    authority_scope: str = CANDIDATE_AUTHORITY_SCOPE,
) -> tuple[CashPhaseAuthorityManifest, ...]:
    """Return exact formal or candidate authority candidates.

    This function deliberately returns zero candidates for an unknown or
    unobserved cash phase.  It never guesses a phase and never substitutes a
    value learned from a source account.  Formal policy windows for the same
    station must be globally non-overlapping; a latent overlap is rejected
    even when only one row happens to be active at the requested instant.
    """

    station = _text("station_id", station_id)
    timezone_name = _timezone("station_timezone", station_timezone)
    observed = _utc_datetime("observed_at_utc", observed_at_utc)
    scope = str(authority_scope or "").strip().upper()
    if scope not in FORMAL_POLICY_SCOPES:
        raise ContractError("cash_phase_authority_scope_invalid")
    if scope == FORMAL_AUTHORITY_SCOPE:
        raise ContractError("cash_phase_formal_oos_authority_schema_missing")
    phase = str(observed_cash_phase or "").strip().upper()
    if phase not in {"HEAVY", "STANDARD", "TAIL"}:
        return ()

    rows = [_policy(item) for item in artifacts]
    scoped = sorted(
        (
            item
            for item in rows
            if item.station_id == station
            and (
                (
                    scope == FORMAL_AUTHORITY_SCOPE
                    and item.promoted
                )
                or (
                    scope == CANDIDATE_AUTHORITY_SCOPE
                    and not item.promoted
                    and item.evidence_status != "PROMOTED"
                )
            )
        ),
        key=lambda item: (
            _utc_datetime("valid_from_utc", item.valid_from_utc),
            _utc_datetime("valid_until_utc", item.valid_until_utc),
            item.policy_version,
            item.content_sha256,
        ),
    )
    for index, left in enumerate(scoped):
        for right in scoped[index + 1:]:
            if _utc_datetime(
                "valid_from_utc",
                right.valid_from_utc,
            ) >= _utc_datetime("valid_until_utc", left.valid_until_utc):
                break
            if _overlaps(left, right):
                raise ContractError("cash_phase_authority_policy_window_overlap")

    active = [
        item
        for item in scoped
        if _utc_datetime("valid_from_utc", item.valid_from_utc)
        <= observed
        < _utc_datetime("valid_until_utc", item.valid_until_utc)
        and item.station_timezone == timezone_name
        and phase in item.cash_by_phase
    ]
    return tuple(
        CashPhaseAuthorityManifest.create(
            station_id=item.station_id,
            policy_version=item.policy_version,
            artifact_sha256=item.content_sha256,
            station_timezone=timezone_name,
            valid_from_utc=item.valid_from_utc,
            valid_until_utc=item.valid_until_utc,
            authority_scope=scope,
        )
        for item in active
    )


def resolve_active_cash_phase_policy(
    artifacts: Sequence[BasketCashPhasePolicyArtifact | Mapping[str, Any]],
    *,
    authority_manifest: CashPhaseAuthorityManifest | Mapping[str, Any] | None,
    station_id: str,
    observed_at_utc: str,
    required_authority_scope: str,
) -> BasketCashPhasePolicyArtifact:
    """Resolve only the artifact named by the exact active manifest.

    All checks are intentionally strict.  In particular, the candidate scope
    may select a candidate artifact for no-live planning, but a candidate can
    never satisfy ``FORMAL_PROMOTED`` merely by being the only or newest row.
    """

    manifest = _manifest(authority_manifest)
    required_scope = str(required_authority_scope or "").strip().upper()
    if required_scope not in AUTHORITY_SCOPES:
        raise ContractError("cash_phase_authority_scope_invalid")
    if required_scope == FORMAL_AUTHORITY_SCOPE:
        raise ContractError("cash_phase_formal_oos_authority_schema_missing")
    if manifest.authority_scope != required_scope:
        raise ContractError("cash_phase_authority_scope_mismatch")
    station = _text("station_id", station_id)
    if manifest.station_id != station:
        raise ContractError("cash_phase_authority_station_scope_mismatch")
    observed = _utc_datetime("observed_at_utc", observed_at_utc)
    if not (
        _utc_datetime("valid_from_utc", manifest.valid_from_utc)
        <= observed
        < _utc_datetime("valid_until_utc", manifest.valid_until_utc)
    ):
        raise ContractError("cash_phase_authority_validity_mismatch")

    rows = [
        item if isinstance(item, BasketCashPhasePolicyArtifact)
        else BasketCashPhasePolicyArtifact.from_mapping(item)
        for item in artifacts
    ]
    scoped = [
        item for item in rows
        if item.station_id == station and item.policy_version == manifest.policy_version
    ]
    if not scoped:
        raise ContractError("cash_phase_authority_artifact_mismatch")
    exact_sha = [item for item in scoped if item.content_sha256 == manifest.artifact_sha256]
    if not exact_sha:
        raise ContractError("cash_phase_authority_artifact_mismatch")
    if len(exact_sha) != 1:
        raise ContractError("cash_phase_authority_ambiguous")
    policy = exact_sha[0]
    if policy.station_timezone != manifest.station_timezone:
        raise ContractError("cash_phase_authority_timezone_mismatch")
    if (
        policy.valid_from_utc != manifest.valid_from_utc
        or policy.valid_until_utc != manifest.valid_until_utc
    ):
        raise ContractError("cash_phase_authority_validity_mismatch")
    if required_scope == FORMAL_AUTHORITY_SCOPE and not policy.promoted:
        raise ContractError("cash_phase_authority_candidate_not_formal")
    if required_scope == CANDIDATE_AUTHORITY_SCOPE and policy.promoted:
        raise ContractError("cash_phase_authority_formal_not_candidate")
    if required_scope == CANDIDATE_AUTHORITY_SCOPE and policy.evidence_status == "PROMOTED":
        raise ContractError("cash_phase_authority_formal_not_candidate")
    return policy


def load_cash_phase_authority_manifest(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    observed_at_utc: str,
    required_authority_scope: str,
) -> CashPhaseAuthorityManifest:
    """Load exactly one persisted active manifest for a station and instant."""

    station = _text("station_id", station_id)
    observed = _utc_datetime("observed_at_utc", observed_at_utc)
    scope = str(required_authority_scope or "").strip().upper()
    if scope not in AUTHORITY_SCOPES:
        raise ContractError("cash_phase_authority_scope_invalid")
    rows = conn.execute(
        "SELECT station_id,policy_version,artifact_sha256,station_timezone,"
        "valid_from_utc,valid_until_utc,authority_scope,manifest_sha256 "
        "FROM basket_cash_phase_authority_manifest "
        "WHERE station_id=? AND authority_scope=? "
        "ORDER BY valid_from_utc,valid_until_utc,manifest_sha256 LIMIT ?",
        (station, scope, MAX_MANIFEST_ROWS_PER_SCOPE + 1),
    ).fetchall()
    if len(rows) > MAX_MANIFEST_ROWS_PER_SCOPE:
        raise ContractError("cash_phase_authority_manifest_rows_exceeded")
    active_rows = [
        row
        for row in rows
        if _utc_datetime("valid_from_utc", row[4])
        <= observed
        < _utc_datetime("valid_until_utc", row[5])
    ]
    if len(active_rows) != 1:
        raise ContractError("cash_phase_authority_manifest_not_unique")
    row = active_rows[0]
    return CashPhaseAuthorityManifest.from_mapping({
        "schema": AUTHORITY_MANIFEST_SCHEMA,
        "station_id": row[0],
        "policy_version": row[1],
        "artifact_sha256": row[2],
        "station_timezone": row[3],
        "valid_from_utc": row[4],
        "valid_until_utc": row[5],
        "authority_scope": row[6],
        "manifest_sha256": row[7],
    })


# Short name for callers that already speak in terms of authority resolution.
resolve_cash_phase_authority = resolve_active_cash_phase_policy


__all__ = [
    "AUTHORITY_MANIFEST_SCHEMA",
    "AUTHORITY_SCOPES",
    "CANDIDATE_AUTHORITY_SCOPE",
    "CashPhaseAuthorityManifest",
    "FORMAL_POLICY_SCOPES",
    "FORMAL_AUTHORITY_SCOPE",
    "MAX_MANIFEST_ROWS_PER_SCOPE",
    "cash_phase_authority_candidates",
    "load_cash_phase_authority_manifest",
    "resolve_active_cash_phase_policy",
    "resolve_cash_phase_authority",
]
