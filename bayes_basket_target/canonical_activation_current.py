"""Fail-closed current pointers for canonical no-live activations.

``CanonicalActivationFact.scope_key`` intentionally includes its generation so
the append-only canonical contract can retain every fact.  Consumers that need
a current activation, however, must not infer one by sorting those generations.
This module projects a content-addressed, no-live-only index whose business
scope deliberately excludes generation.  A scope with anything other than one
activation is explicitly blocked.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from .canonical_contract import CanonicalActivationFact, MODEL_FAMILIES
from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_canonical_activation_current_index_v1"
NO_LIVE_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
CURRENT = "CURRENT"
BLOCKED = "BLOCKED"
ACTIVATION_CURRENT_AMBIGUOUS = "ACTIVATION_CURRENT_AMBIGUOUS"

_INDEX_FIELDS = frozenset(
    {
        "schema",
        "authority_scope",
        "state",
        "entry_count",
        "entries",
        "content_sha256",
    }
)
_SCOPE_FIELDS = frozenset(
    {"station_id", "model_family", "lead_bucket", "run_cycle"}
)
_REFERENCE_FIELDS = frozenset(
    {"activation_id", "activation_generation", "activation_content_sha256"}
)


def _business_scope(fact: CanonicalActivationFact) -> tuple[str, str, str, str]:
    return (
        fact.station_id,
        fact.model_family,
        fact.lead_bucket,
        fact.run_cycle,
    )


def _scope_mapping(scope: tuple[str, str, str, str]) -> dict[str, str]:
    station_id, model_family, lead_bucket, run_cycle = scope
    return {
        "station_id": station_id,
        "model_family": model_family,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
    }


def _activation_reference(fact: CanonicalActivationFact) -> dict[str, Any]:
    return {
        "activation_id": fact.activation_id,
        "activation_generation": fact.activation_generation,
        "activation_content_sha256": fact.activation_content_sha256,
    }


def _reference_sort_key(reference: Mapping[str, Any]) -> tuple[int, str, str]:
    return (
        int(reference["activation_generation"]),
        str(reference["activation_id"]),
        str(reference["activation_content_sha256"]),
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _strict_keys(raw: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if not isinstance(raw, Mapping):
        raise ContractError(f"canonical_activation_current_{label}_mapping_required")
    unknown = sorted(set(raw) - expected)
    missing = sorted(expected - set(raw))
    if unknown:
        raise ContractError(
            f"canonical_activation_current_{label}_unknown_field:{','.join(unknown)}"
        )
    if missing:
        raise ContractError(
            f"canonical_activation_current_{label}_missing_field:{','.join(missing)}"
        )


def _validate_scope(raw: Mapping[str, Any]) -> tuple[str, str, str, str]:
    _strict_keys(raw, _SCOPE_FIELDS, "scope")
    station_id = raw["station_id"]
    model_family = raw["model_family"]
    lead_bucket = raw["lead_bucket"]
    run_cycle = raw["run_cycle"]
    if not isinstance(station_id, str) or not station_id or station_id != station_id.upper():
        raise ContractError("canonical_activation_current_scope_station_id_invalid")
    if not isinstance(model_family, str) or model_family not in MODEL_FAMILIES:
        raise ContractError("canonical_activation_current_scope_model_family_invalid")
    if not isinstance(lead_bucket, str) or not lead_bucket:
        raise ContractError("canonical_activation_current_scope_lead_bucket_invalid")
    if not isinstance(run_cycle, str) or not run_cycle:
        raise ContractError("canonical_activation_current_scope_run_cycle_invalid")
    return station_id, model_family, lead_bucket, run_cycle


def _validate_reference(raw: Mapping[str, Any]) -> tuple[int, str, str]:
    _strict_keys(raw, _REFERENCE_FIELDS, "activation_reference")
    activation_id = raw["activation_id"]
    generation = raw["activation_generation"]
    activation_sha = raw["activation_content_sha256"]
    if not isinstance(activation_id, str) or not activation_id:
        raise ContractError("canonical_activation_current_activation_id_invalid")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ContractError("canonical_activation_current_activation_generation_invalid")
    if not _is_sha256(activation_sha):
        raise ContractError("canonical_activation_current_activation_content_sha256_invalid")
    return generation, activation_id, activation_sha


def _validate_entry(raw: Mapping[str, Any]) -> tuple[tuple[str, str, str, str], str]:
    if not isinstance(raw, Mapping):
        raise ContractError("canonical_activation_current_entry_mapping_required")
    state = raw.get("state")
    if state == CURRENT:
        _strict_keys(raw, frozenset({"scope", "state", "current_activation"}), "entry")
        scope = _validate_scope(raw["scope"])
        _validate_reference(raw["current_activation"])
        return scope, state
    if state == BLOCKED:
        _strict_keys(
            raw,
            frozenset({"scope", "state", "reason", "activation_candidates"}),
            "entry",
        )
        scope = _validate_scope(raw["scope"])
        if raw["reason"] != ACTIVATION_CURRENT_AMBIGUOUS:
            raise ContractError("canonical_activation_current_block_reason_invalid")
        candidates = raw["activation_candidates"]
        if not isinstance(candidates, list) or len(candidates) < 2:
            raise ContractError("canonical_activation_current_block_candidates_invalid")
        sort_keys = [_validate_reference(item) for item in candidates]
        if sort_keys != sorted(sort_keys):
            raise ContractError("canonical_activation_current_block_candidates_order_invalid")
        return scope, state
    raise ContractError("canonical_activation_current_entry_state_invalid")


def verify_canonical_activation_current_index(index: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a persisted index before a no-live consumer uses it.

    This checks the index's own content address and structural invariant.  The
    source activation facts are validated when the index is built; a consumer
    should use the resulting activation content hash to bind its subsequent
    canonical fact lookup.
    """

    _strict_keys(index, _INDEX_FIELDS, "index")
    if index["schema"] != SCHEMA:
        raise ContractError("canonical_activation_current_schema_invalid")
    if index["authority_scope"] != NO_LIVE_AUTHORITY_SCOPE:
        raise ContractError("canonical_activation_current_authority_scope_invalid")
    if index["state"] not in {CURRENT, BLOCKED}:
        raise ContractError("canonical_activation_current_state_invalid")
    entry_count = index["entry_count"]
    entries = index["entries"]
    if isinstance(entry_count, bool) or not isinstance(entry_count, int) or entry_count < 0:
        raise ContractError("canonical_activation_current_entry_count_invalid")
    if not isinstance(entries, list) or entry_count != len(entries):
        raise ContractError("canonical_activation_current_entries_invalid")
    scopes_and_states = [_validate_entry(entry) for entry in entries]
    scopes = [scope for scope, _state in scopes_and_states]
    if scopes != sorted(scopes) or len(set(scopes)) != len(scopes):
        raise ContractError("canonical_activation_current_scope_order_or_duplicate_invalid")
    has_blocked_scope = any(state == BLOCKED for _scope, state in scopes_and_states)
    expected_state = BLOCKED if has_blocked_scope else CURRENT
    if index["state"] != expected_state:
        raise ContractError("canonical_activation_current_global_state_invalid")
    supplied_hash = index["content_sha256"]
    if not _is_sha256(supplied_hash):
        raise ContractError("canonical_activation_current_content_sha256_invalid")
    identity = {key: value for key, value in index.items() if key != "content_sha256"}
    if supplied_hash != content_sha256(identity):
        raise ContractError("canonical_activation_current_content_sha256_mismatch")
    return dict(index)


def build_canonical_activation_current_index(
    activation_facts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the immutable no-live current-activation index.

    Facts are examined in their supplied order solely to detect a generation
    regression.  The finished index is deterministically ordered by business
    scope and never selects a highest generation: duplicate rows and multiple
    generations are both recorded as an explicit blocked scope.
    """

    if (
        isinstance(activation_facts, (str, bytes))
        or not isinstance(activation_facts, Sequence)
    ):
        raise ContractError("canonical_activation_current_facts_sequence_required")

    facts_by_scope: dict[tuple[str, str, str, str], list[CanonicalActivationFact]] = defaultdict(list)
    greatest_generation_seen: dict[tuple[str, str, str, str], int] = {}
    for raw in activation_facts:
        fact = CanonicalActivationFact.from_mapping(raw)
        if fact.authority_scope != NO_LIVE_AUTHORITY_SCOPE:
            raise ContractError("canonical_activation_current_authority_scope_invalid")
        scope = _business_scope(fact)
        prior_generation = greatest_generation_seen.get(scope)
        if prior_generation is not None and fact.activation_generation < prior_generation:
            raise ContractError("canonical_activation_current_generation_regression")
        greatest_generation_seen[scope] = fact.activation_generation
        facts_by_scope[scope].append(fact)

    entries: list[dict[str, Any]] = []
    for scope in sorted(facts_by_scope):
        facts = facts_by_scope[scope]
        if len(facts) == 1:
            entries.append(
                {
                    "scope": _scope_mapping(scope),
                    "state": CURRENT,
                    "current_activation": _activation_reference(facts[0]),
                }
            )
            continue
        candidates = sorted(
            (_activation_reference(fact) for fact in facts),
            key=_reference_sort_key,
        )
        entries.append(
            {
                "scope": _scope_mapping(scope),
                "state": BLOCKED,
                "reason": ACTIVATION_CURRENT_AMBIGUOUS,
                "activation_candidates": candidates,
            }
        )

    identity = {
        "schema": SCHEMA,
        "authority_scope": NO_LIVE_AUTHORITY_SCOPE,
        "state": BLOCKED if any(entry["state"] == BLOCKED for entry in entries) else CURRENT,
        "entry_count": len(entries),
        "entries": entries,
    }
    return verify_canonical_activation_current_index(
        {**identity, "content_sha256": content_sha256(identity)}
    )


__all__ = (
    "ACTIVATION_CURRENT_AMBIGUOUS",
    "BLOCKED",
    "CURRENT",
    "NO_LIVE_AUTHORITY_SCOPE",
    "SCHEMA",
    "build_canonical_activation_current_index",
    "verify_canonical_activation_current_index",
)
