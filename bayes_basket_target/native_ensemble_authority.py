"""Content-addressed native ensemble authority snapshot helpers.

The native ensemble SSOT is append-only, so selecting the newest active row
from SQLite is not sufficient to identify the cutoff authority.  This module
validates the read-only atomic snapshot produced by
``build_oos_atomic_snapshot_manifest.py`` and exposes exact pointer matching
for both coordinator preflight and the production P0 compiler.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .resource_bounds import load_bounded_json


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_SCHEMA = "bayes_basket_target_oos_atomic_snapshot_manifest_v1"
BINDING_SCHEMA = "native_p0_consumable_binding_v1"
POINTER_SCHEMA = "native_ensemble_artifact_pointer_v1"
NATIVE_AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
DEFAULT_NATIVE_OOS_SNAPSHOT_MANIFEST = (
    ROOT
    / "output"
    / "audits"
    / "oos_atomic_snapshot_manifest_45station_20260803_current_latest_eval_pair_p0_recheck.json"
)


def _sha256(value: Any) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _json_mapping(value: Any, *, code: str) -> dict[str, str]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(code) from exc
    if not isinstance(parsed, Mapping) or not parsed:
        raise ContractError(code)
    normalized = {
        str(key).strip(): str(item).strip()
        for key, item in parsed.items()
    }
    if any(not key or not item for key, item in normalized.items()):
        raise ContractError(code)
    return normalized


def _validate_pointer(pointer: Mapping[str, Any], *, station_id: str) -> dict[str, Any]:
    if str(pointer.get("schema") or "") != POINTER_SCHEMA:
        raise ContractError("native_oos_snapshot_pointer_schema_invalid")
    pointer_identity = {
        key: value for key, value in pointer.items() if key != "pointer_sha256"
    }
    if not _sha256(pointer.get("pointer_sha256")) or str(
        pointer.get("pointer_sha256")
    ).lower() != content_sha256(pointer_identity):
        raise ContractError("native_oos_snapshot_pointer_hash_invalid")
    if str(pointer.get("authority_scope") or "") != NATIVE_AUTHORITY_SCOPE:
        raise ContractError("native_oos_snapshot_pointer_scope_invalid")
    if str(pointer.get("write_policy") or "") != (
        "existing_append_only_no_live_only_no_write"
    ):
        raise ContractError("native_oos_snapshot_pointer_write_policy_invalid")
    for key in (
        "activation_id",
        "cross_ensemble_artifact_id",
        "selection_evidence_sha256",
        "cross_artifact_content_sha256",
        "activation_identity_sha256",
    ):
        value = pointer.get(key)
        if key.endswith("sha256"):
            if not _sha256(value):
                raise ContractError(f"native_oos_snapshot_pointer_{key}_invalid")
        elif not str(value or "").strip():
            raise ContractError(f"native_oos_snapshot_pointer_{key}_invalid")
    try:
        generation = int(pointer.get("generation"))
    except (TypeError, ValueError) as exc:
        raise ContractError("native_oos_snapshot_pointer_generation_invalid") from exc
    if generation < 0:
        raise ContractError("native_oos_snapshot_pointer_generation_invalid")
    return dict(pointer)


@dataclass(frozen=True)
class NativeOosSnapshot:
    """Validated immutable view of one native-P0 cutoff snapshot.

    Bindings are indexed by the scope they attest -- station, season, lead
    bucket and run cycle -- rather than by station alone.  One binding per
    station meant one attested lead per station: a station pinned at d2/18Z
    could never price its own d1 or d0 packet, however well calibrated, and
    every artifact trained at another lead was unreachable by construction.
    Skill is a property of the lead, so the attestation has to be too.
    """

    payload: Mapping[str, Any]
    bindings_by_scope: Mapping[tuple[str, str, str, str], Mapping[str, Any]]
    path: Path | None = None

    @property
    def content_sha256(self) -> str:
        return str(self.payload.get("content_sha256") or "")

    @property
    def station_ids(self) -> tuple[str, ...]:
        return tuple(sorted({scope[0] for scope in self.bindings_by_scope}))


def validate_native_oos_snapshot_manifest(
    value: Mapping[str, Any],
    *,
    path: Path | None = None,
) -> NativeOosSnapshot:
    """Validate the manifest and every content-addressed native pointer."""

    if not isinstance(value, Mapping):
        raise ContractError("native_oos_snapshot_manifest_mapping_required")
    payload = dict(value)
    if str(payload.get("schema") or "") != SNAPSHOT_SCHEMA:
        raise ContractError("native_oos_snapshot_manifest_schema_invalid")
    declared = str(payload.get("content_sha256") or "").lower()
    identity = {
        key: child for key, child in payload.items() if key != "content_sha256"
    }
    if not _sha256(declared) or declared != content_sha256(identity):
        raise ContractError("native_oos_snapshot_manifest_hash_invalid")
    expected_flags = {
        "read_only": True,
        "writes_target_database": False,
        "calls_order_gateway": False,
    }
    for key, expected in expected_flags.items():
        if payload.get(key) is not expected:
            raise ContractError(f"native_oos_snapshot_manifest_{key}_invalid")
    try:
        if int(payload.get("network_fetches")) != 0:
            raise ContractError("native_oos_snapshot_manifest_network_nonzero")
        if int(payload.get("orders_submitted")) != 0:
            raise ContractError("native_oos_snapshot_manifest_orders_nonzero")
    except (TypeError, ValueError) as exc:
        raise ContractError("native_oos_snapshot_manifest_orders_invalid") from exc

    binding = payload.get("native_p0_binding")
    if not isinstance(binding, Mapping):
        raise ContractError("native_oos_snapshot_binding_missing")
    binding_payload = dict(binding)
    if str(binding_payload.get("schema") or "") != BINDING_SCHEMA:
        raise ContractError("native_oos_snapshot_binding_schema_invalid")
    binding_declared = str(binding_payload.get("content_sha256") or "").lower()
    binding_identity = {
        key: child
        for key, child in binding_payload.items()
        if key != "content_sha256"
    }
    if not _sha256(binding_declared) or binding_declared != content_sha256(
        binding_identity
    ):
        raise ContractError("native_oos_snapshot_binding_hash_invalid")
    if str(binding_payload.get("authority_scope") or "") != NATIVE_AUTHORITY_SCOPE:
        raise ContractError("native_oos_snapshot_binding_scope_invalid")
    raw_bindings = binding_payload.get("bindings")
    raw_station_ids = binding_payload.get("station_ids")
    if not isinstance(raw_bindings, list) or not isinstance(raw_station_ids, list):
        raise ContractError("native_oos_snapshot_binding_rows_invalid")
    station_ids = [str(item or "").strip().upper() for item in raw_station_ids]
    if not station_ids or any(not item for item in station_ids):
        raise ContractError("native_oos_snapshot_binding_station_ids_invalid")
    if len(station_ids) != len(set(station_ids)):
        raise ContractError("native_oos_snapshot_binding_duplicate_station")
    try:
        if int(binding_payload.get("binding_count")) != len(raw_bindings):
            raise ContractError("native_oos_snapshot_binding_count_invalid")
    except (TypeError, ValueError) as exc:
        raise ContractError("native_oos_snapshot_binding_count_invalid") from exc

    bindings_by_scope: dict[
        tuple[str, str, str, str], Mapping[str, Any]
    ] = {}
    for item in raw_bindings:
        if not isinstance(item, Mapping):
            raise ContractError("native_oos_snapshot_binding_row_invalid")
        station = str(item.get("station_id") or "").strip().upper()
        if not station:
            raise ContractError("native_oos_snapshot_binding_row_invalid")
        # A station may be attested at several leads, but each scope names
        # exactly one activation: two bindings for the same scope would leave
        # the pointer ambiguous, which is the thing it exists to prevent.
        scope = (
            station,
            str(item.get("season_bin") or "").strip(),
            str(item.get("lead_bucket") or "").strip(),
            str(item.get("run_cycle") or "").strip(),
        )
        if not all(scope):
            raise ContractError("native_oos_snapshot_binding_scope_incomplete")
        if scope in bindings_by_scope:
            raise ContractError("native_oos_snapshot_binding_duplicate_scope")
        pointer = item.get("artifact_pointer")
        if not isinstance(pointer, Mapping):
            raise ContractError("native_oos_snapshot_pointer_missing")
        pointer_copy = _validate_pointer(pointer, station_id=station)
        selection_sha = str(item.get("selection_evidence_sha256") or "").lower()
        if not _sha256(selection_sha) or selection_sha != str(
            pointer_copy.get("selection_evidence_sha256") or ""
        ).lower():
            raise ContractError("native_oos_snapshot_selection_hash_mismatch")
        cycle_json = str(item.get("run_cycle_by_system_json") or "")
        cycle_sha = str(item.get("run_cycle_by_system_sha256") or "").lower()
        cycle_mapping = _json_mapping(
            cycle_json, code="native_oos_snapshot_cycle_mapping_invalid"
        )
        if not _sha256(cycle_sha) or cycle_sha != content_sha256(cycle_mapping):
            raise ContractError("native_oos_snapshot_cycle_mapping_hash_invalid")
        normalized = {
            **dict(item),
            "station_id": station,
            "selection_evidence_sha256": selection_sha,
            "run_cycle_by_system_json": cycle_json,
            "run_cycle_by_system_sha256": cycle_sha,
            "artifact_pointer": pointer_copy,
        }
        bindings_by_scope[scope] = normalized
    if set(station_ids) != {scope[0] for scope in bindings_by_scope}:
        raise ContractError("native_oos_snapshot_binding_station_set_mismatch")
    return NativeOosSnapshot(
        payload=payload,
        bindings_by_scope=bindings_by_scope,
        path=path,
    )


def load_native_oos_snapshot_manifest(
    path: Path | str = DEFAULT_NATIVE_OOS_SNAPSHOT_MANIFEST,
) -> NativeOosSnapshot:
    """Load and validate one bounded, non-symlink snapshot file."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ContractError("native_oos_snapshot_manifest_symlink_forbidden")
    try:
        resolved = candidate.resolve(strict=True)
        value = load_bounded_json(resolved)
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError("native_oos_snapshot_manifest_unavailable") from exc
    return validate_native_oos_snapshot_manifest(value, path=resolved)


def _binding_rows(
    snapshot: NativeOosSnapshot | Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    """Every validated binding row, whatever shape the caller holds."""

    if snapshot is None:
        return []
    if isinstance(snapshot, NativeOosSnapshot):
        return list(snapshot.bindings_by_scope.values())
    # Accept a validated mapping-shaped adapter for callers that already
    # materialized the binding projection.  Never infer a pointer from a
    # station name or from SQLite rows here.
    rows = snapshot.get("native_p0_binding")
    if not isinstance(rows, Mapping):
        return []
    return [
        item for item in rows.get("bindings") or [] if isinstance(item, Mapping)
    ]


def station_has_pointer(
    snapshot: NativeOosSnapshot | Mapping[str, Any] | None,
    station_id: str,
) -> bool:
    """Whether this station is attested at any scope at all.

    Separate from resolving a scope because the two failures mean different
    things to an operator: a station with no binding was never attested, while
    a station attested at other leads simply has nothing for the lead in hand.
    """

    station = str(station_id or "").strip().upper()
    return any(
        str(item.get("station_id") or "").strip().upper() == station
        for item in _binding_rows(snapshot)
    )


def pointer_for_scope(
    snapshot: NativeOosSnapshot | Mapping[str, Any] | None,
    station_id: str,
    *,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
) -> Mapping[str, Any] | None:
    """Return the binding attesting exactly this scope, if there is one.

    The pointer names one activation by id and content hash, so it can only
    speak for the scope that activation was fitted in.  Asking by station
    alone returned whichever lead happened to be attested and then rejected
    every activation from any other -- which read as "no artifact" when the
    truth was "no attestation for this lead".
    """

    station = str(station_id or "").strip().upper()
    scope = (station, str(season_bin or ""), str(lead_bucket or ""), str(run_cycle or ""))
    return next(
        (
            item
            for item in _binding_rows(snapshot)
            if (
                str(item.get("station_id") or "").strip().upper(),
                str(item.get("season_bin") or "").strip(),
                str(item.get("lead_bucket") or "").strip(),
                str(item.get("run_cycle") or "").strip(),
            )
            == scope
        ),
        None,
    )


def pointer_activation_mismatch_reasons(
    binding: Mapping[str, Any],
    *,
    activation_id: str,
    artifact_id: str,
    activation_sha256: str,
    artifact_sha256: str,
    selection_evidence_sha256: str,
    generation: int,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    run_cycle_by_system_json: str,
    authority_scope: str,
) -> list[str]:
    """Compare a DB activation row to its exact immutable pointer."""

    pointer = binding.get("artifact_pointer")
    if not isinstance(pointer, Mapping):
        return ["pointer_missing"]
    reasons: list[str] = []

    def compare(label: str, actual: Any, expected: Any) -> None:
        if str(actual or "") != str(expected or ""):
            reasons.append(label)

    compare("activation_id_mismatch", activation_id, pointer.get("activation_id"))
    compare("artifact_id_mismatch", artifact_id, pointer.get("cross_ensemble_artifact_id"))
    compare(
        "activation_sha256_mismatch",
        str(activation_sha256 or "").lower(),
        str(pointer.get("activation_identity_sha256") or "").lower(),
    )
    compare(
        "artifact_sha256_mismatch",
        str(artifact_sha256 or "").lower(),
        str(pointer.get("cross_artifact_content_sha256") or "").lower(),
    )
    compare(
        "selection_evidence_sha256_mismatch",
        str(selection_evidence_sha256 or "").lower(),
        str(binding.get("selection_evidence_sha256") or "").lower(),
    )
    try:
        if int(generation) != int(pointer.get("generation")):
            reasons.append("generation_mismatch")
    except (TypeError, ValueError):
        reasons.append("generation_mismatch")
    compare("season_bin_mismatch", season_bin, binding.get("season_bin"))
    compare("lead_bucket_mismatch", lead_bucket, binding.get("lead_bucket"))
    compare("run_cycle_mismatch", run_cycle, binding.get("run_cycle"))
    compare(
        "run_cycle_by_system_json_mismatch",
        run_cycle_by_system_json,
        binding.get("run_cycle_by_system_json"),
    )
    compare("authority_scope_mismatch", authority_scope, pointer.get("authority_scope"))
    return reasons


__all__ = [
    "BINDING_SCHEMA",
    "DEFAULT_NATIVE_OOS_SNAPSHOT_MANIFEST",
    "NATIVE_AUTHORITY_SCOPE",
    "NativeOosSnapshot",
    "POINTER_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "load_native_oos_snapshot_manifest",
    "pointer_activation_mismatch_reasons",
    "pointer_for_scope",
    "station_has_pointer",
    "validate_native_oos_snapshot_manifest",
]
