"""Bounded registry for exact-scope, activated TAF P0-M artifacts.

The registry is deliberately a lossless envelope around already-validated
``TafRevisionArtifact`` objects.  It does not train, promote, or otherwise
alter any artifact; the P0-M worker remains responsible for selecting an
exact station/NWP scope from it.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .taf_revision import TafRevisionArtifact


SCHEMA = "bayes_basket_target_taf_revision_artifact_registry_v1"
MAX_ARTIFACTS = 45


def _scope_key(artifact: TafRevisionArtifact) -> tuple[str, str, str, str, str]:
    return (
        artifact.station_id,
        artifact.settlement_timezone,
        artifact.season_bin,
        artifact.lead_bucket,
        artifact.run_cycle,
    )


def build_taf_artifact_registry(
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a canonical registry without changing member artifacts.

    Each member must already be an activated strict-OOS P0-M artifact.  Exact
    duplicate scopes are rejected here rather than deferred to the worker,
    because selecting one at runtime would be nondeterministic.
    """

    if not artifacts or len(artifacts) > MAX_ARTIFACTS:
        raise ContractError("taf_artifact_registry_count_invalid")
    parsed = [TafRevisionArtifact.from_mapping(item) for item in artifacts]
    scope_keys = [_scope_key(item) for item in parsed]
    if len(set(scope_keys)) != len(scope_keys):
        raise ContractError("taf_artifact_registry_exact_scope_ambiguous")
    artifact_ids = [item.artifact_id for item in parsed]
    if len(set(artifact_ids)) != len(artifact_ids):
        raise ContractError("taf_artifact_registry_artifact_id_duplicate")
    serialized = [item.to_dict() for item in sorted(parsed, key=_scope_key)]
    identity = {"schema": SCHEMA, "artifacts": serialized}
    return {**identity, "content_sha256": content_sha256(identity)}


def validate_taf_artifact_registry(raw: Any) -> list[TafRevisionArtifact]:
    """Validate a registry produced by :func:`build_taf_artifact_registry`."""

    if not isinstance(raw, Mapping) or set(raw) != {
        "schema", "artifacts", "content_sha256"
    }:
        raise ContractError("taf_artifact_registry_schema_invalid")
    if raw.get("schema") != SCHEMA:
        raise ContractError("taf_artifact_registry_schema_invalid")
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, list):
        raise ContractError("taf_artifact_registry_count_invalid")
    identity = {
        "schema": raw["schema"],
        "artifacts": artifacts,
    }
    if str(raw.get("content_sha256") or "") != content_sha256(identity):
        raise ContractError("taf_artifact_registry_content_sha_mismatch")
    expected = build_taf_artifact_registry(artifacts)
    # ``build_*`` canonicalizes the member order.  Do not accept a valid hash
    # over an arbitrary order, so the same authority always has one encoding.
    if list(artifacts) != expected["artifacts"]:
        raise ContractError("taf_artifact_registry_not_canonical")
    return [TafRevisionArtifact.from_mapping(item) for item in artifacts]


__all__ = [
    "MAX_ARTIFACTS",
    "SCHEMA",
    "build_taf_artifact_registry",
    "validate_taf_artifact_registry",
]
