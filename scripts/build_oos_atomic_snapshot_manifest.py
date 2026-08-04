#!/usr/bin/env python3
"""Freeze the read-only P0/TAF/native evidence boundary for one ledger run.

The resident P0 inbox is intentionally mutable: expired packets are pruned by
the worker.  This manifest therefore reads each input from one bounded byte
snapshot, verifies the declared content hash, checks that no input changed
during the read window, and binds the four evidence objects to one scope
fingerprint.  Native activation linkage and its content-addressed pointer
must expose exactly the same station set; a split set fails closed.  It never
fetches data, opens the target database, writes SSOT, or calls an order
gateway.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    bounded_json_bytes,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402


SCHEMA = "bayes_basket_target_oos_atomic_snapshot_manifest_v1"
MAX_INPUT_BYTES = min(int(MAX_JSON_INPUT_BYTES), 64 * 1024 * 1024)
MAX_RETRIES = 5
READ_CHUNK_BYTES = 1024 * 1024

DEFAULTS = {
    "p0_inbox": ROOT / "data" / "bayes_basket_target_p0_inbox.json",
    "taf_audit": ROOT / "output" / "audits" / "taf_p0m_exact_join_45station_20260802_current_p0_inbox.json",
    # The native-P0 root is the closed-day cutoff pair.  The older pointer
    # refresh names a larger, superseded selection set and must not be mixed
    # with the 41 exact-PASS pointers below.
    "native_linkage": ROOT / "output" / "audits" / "native_ensemble_activation_oos_linkage_cutoff_20260803.json",
    "native_repair": ROOT / "output" / "audits" / "native_ensemble_activation_repair_plan_cutoff_20260803.json",
}
INPUT_SCHEMAS = {
    "p0_inbox": "bayes_basket_target_p0_inbox_v1",
    "taf_audit": "bayes_basket_target_taf_p0m_exact_join_45station_audit_v1",
    "native_linkage": "native_ensemble_activation_oos_linkage_audit_v1",
    "native_repair": "native_ensemble_activation_repair_plan_v1",
}


class SnapshotError(RuntimeError):
    """A bounded snapshot cannot be trusted."""


def _utc(value: object, *, field: str) -> tuple[str, datetime]:
    """Parse a canonical UTC evidence clock without guessing a timezone."""

    text = str(value or "").strip()
    if not text:
        raise SnapshotError(f"{field}_missing")
    if not text.endswith("Z"):
        raise SnapshotError(f"{field}_must_be_utc_z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise SnapshotError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotError(f"{field}_timezone_required")
    normalized = parsed.astimezone(timezone.utc)
    return (
        normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        normalized,
    )


def _not_after_snapshot_as_of(
    value: datetime, *, snapshot_as_of: datetime, field: str,
) -> None:
    if value > snapshot_as_of:
        raise SnapshotError(f"{field}_after_snapshot_as_of")


def _path(value: Path, *, output: bool = False) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise SnapshotError("snapshot_symlink_path_forbidden")
    if output:
        return path.resolve()
    return path.resolve(strict=True)


def _read_once(path: Path) -> tuple[bytes, os.stat_result, os.stat_result]:
    before = path.stat()
    if not path.is_file() or int(before.st_size) > MAX_INPUT_BYTES:
        raise SnapshotError(f"snapshot_input_invalid:{path}")
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_INPUT_BYTES:
                raise SnapshotError(f"snapshot_input_bytes_exceeded:{path}")
            chunks.append(chunk)
    after = path.stat()
    if (
        int(before.st_size) != int(after.st_size)
        or int(before.st_mtime_ns) != int(after.st_mtime_ns)
        or int(before.st_ino) != int(after.st_ino)
    ):
        raise SnapshotError(f"snapshot_input_changed:{path}")
    return b"".join(chunks), before, after


def _read_stable_json(path: Path, *, retries: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(1, min(int(retries), MAX_RETRIES))):
        try:
            raw, before, after = _read_once(path)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, Mapping):
                raise SnapshotError(f"snapshot_input_mapping_required:{path}")
            declared = str(value.get("content_sha256") or "").lower()
            identity = {key: child for key, child in value.items() if key != "content_sha256"}
            revalidated_content_sha256 = content_sha256(identity)
            declared_valid = bool(declared) and revalidated_content_sha256 == declared
            if declared and not declared_valid:
                raise SnapshotError(f"snapshot_declared_hash_mismatch:{path}")
            file_hash = hashlib.sha256(raw).hexdigest()
            return {
                "path": str(path),
                "file_sha256": file_hash,
                "file_bytes": len(raw),
                "declared_content_sha256": declared or None,
                "declared_content_hash_valid": declared_valid,
                "revalidated_content_sha256": revalidated_content_sha256,
                "generated_at_utc": value.get("generated_at_utc"),
                "as_of_utc": value.get("as_of_utc"),
                "schema": value.get("schema"),
                "value": dict(value),
                "stat": {
                    "size": int(after.st_size),
                    "mtime_ns": int(after.st_mtime_ns),
                    "inode": int(after.st_ino),
                },
            }
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotError) as exc:
            last_error = exc
            if attempt + 1 < max(1, min(int(retries), MAX_RETRIES)):
                time.sleep(0.05 * (attempt + 1))
    raise SnapshotError(str(last_error or f"snapshot_read_failed:{path}"))


def _station_ids(value: Mapping[str, Any], *, key: str) -> list[str]:
    raw = value.get(key)
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        if isinstance(item, Mapping):
            station = str(item.get("station_id") or "").strip().upper()
        else:
            station = str(item or "").strip().upper()
        if not station:
            raise SnapshotError(f"snapshot_station_entry_invalid:{key}")
        result.append(station)
    if len(result) != len(set(result)):
        raise SnapshotError(f"snapshot_station_duplicate:{key}")
    return sorted(result)


def _p0_stations(value: Mapping[str, Any]) -> list[str]:
    raw = value.get("packets")
    if not isinstance(raw, list):
        return []
    stations: list[str] = []
    for wrapper in raw:
        if not isinstance(wrapper, Mapping):
            raise SnapshotError("snapshot_station_entry_invalid:p0_inbox")
        packet = wrapper.get("packet")
        if not isinstance(packet, Mapping):
            raise SnapshotError("snapshot_station_entry_invalid:p0_inbox")
        station = str(packet.get("station_id") or "").strip().upper()
        if not station:
            raise SnapshotError("snapshot_station_entry_invalid:p0_inbox")
        stations.append(station)
    if len(stations) != len(set(stations)):
        raise SnapshotError("snapshot_station_duplicate:p0_inbox")
    return sorted(stations)


def _taf_stations(value: Mapping[str, Any]) -> list[str]:
    return _station_ids(value, key="entries")


def _taf_evidence_clock(value: Mapping[str, Any]) -> tuple[str, datetime, str]:
    """Return the TAF audit's declared evidence clock without file-time inference.

    An audit may freeze its evidence boundary with ``as_of_utc``.  Older
    canonical audit receipts use ``generated_at_utc`` instead.  Either is
    admissible only when it is a parseable UTC-Z value; filesystem mtime is
    intentionally not an evidence clock and must never be used as a fallback.
    """

    as_of = value.get("as_of_utc")
    if str(as_of or "").strip():
        text, parsed = _utc(as_of, field="taf_audit_as_of_utc")
        return text, parsed, "as_of_utc"
    text, parsed = _utc(
        value.get("generated_at_utc"),
        field="taf_audit_generated_at_utc",
    )
    return text, parsed, "generated_at_utc"


def _scope_blockers(
    *, canonical: list[str], scopes: Mapping[str, list[str]],
) -> list[str]:
    """Return typed readiness blockers without losing the frozen evidence.

    Partial P0/native coverage is an expected pre-PR10 business state.  It
    must block consumption, but it must still be materialized into the one
    immutable PR04 snapshot so the gap ledger can advance without inventing
    45-station readiness.  Malformed inputs, bad hashes and duplicate scopes
    continue to raise before this point.
    """

    canonical_set = set(canonical)
    blockers: list[str] = []
    for source in (
        "p0_inbox",
        "taf_audit",
        "native_linkage",
        "native_repair",
    ):
        source_set = set(scopes.get(source, []))
        if not source_set.issubset(canonical_set):
            raise SnapshotError(f"snapshot_station_scope_outside_canonical:{source}")
        if source_set != canonical_set:
            blockers.append(f"SNAPSHOT_SCOPE_MISMATCH:{source}")
    return sorted(blockers)


def _selected_system_mask(
    raw: object,
    *,
    error: str = "native_p0_selected_system_mask_invalid",
) -> list[str] | None:
    """Validate an optional exact native system mask from a source receipt."""

    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) < 2:
        raise SnapshotError(error)
    values = [str(item) for item in raw]
    if any(not item for item in values) or values != sorted(values) or len(set(values)) != len(values):
        raise SnapshotError(error)
    return values


def _native_stations(value: Mapping[str, Any]) -> list[str]:
    return _station_ids(
        value,
        key="active_exact_oos_consumable_activation_station_ids",
    )


def _scope_fingerprint(*, canonical: list[str], scopes: Mapping[str, list[str]]) -> str:
    return content_sha256({
        "canonical_station_ids": canonical,
        "scopes": {key: list(value) for key, value in sorted(scopes.items())},
    })


def _native_p0_binding(
    linkage: Mapping[str, Any], repair: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the only native-P0 roots a downstream manifest may consume.

    A station name alone is insufficient: runtime matching includes the exact
    selection hash, per-system cycle mapping and content-addressed artifact /
    activation pair.  This is deliberately a read-only projection of the
    existing repair plan, never a reconstruction of model data or OOS.
    """

    if str(linkage.get("schema") or "") != (
        "native_ensemble_activation_oos_linkage_audit_v1"
    ):
        raise SnapshotError("native_p0_linkage_schema_invalid")
    if str(repair.get("schema") or "") != (
        "native_ensemble_activation_repair_plan_v1"
    ):
        raise SnapshotError("native_p0_repair_schema_invalid")
    linkage_as_of, linkage_at = _utc(
        linkage.get("as_of_utc"), field="native_linkage_as_of_utc",
    )
    repair_as_of, repair_at = _utc(
        repair.get("as_of_utc"), field="native_repair_as_of_utc",
    )
    if linkage_at != repair_at:
        raise SnapshotError("native_p0_binding_as_of_mismatch")
    repair_mask = _selected_system_mask(repair.get("selected_system_ids_filter"))
    linkage_pointer = linkage.get("current_pointer")
    linkage_mask = None
    if isinstance(linkage_pointer, Mapping):
        linkage_mask = _selected_system_mask(
            linkage_pointer.get("selected_system_ids_filter")
        )
    if repair_mask is not None and linkage_mask != repair_mask:
        raise SnapshotError("native_p0_binding_selected_system_mask_mismatch")
    linkage_sha = str(linkage.get("content_sha256") or "").lower()
    repair_sha = str(repair.get("content_sha256") or "").lower()
    if len(linkage_sha) != 64 or len(repair_sha) != 64:
        raise SnapshotError("native_p0_binding_source_hash_missing")
    active = set(
        _station_ids(
            linkage,
            key="active_exact_oos_consumable_activation_station_ids",
        )
    )
    plans = repair.get("candidate_plans")
    if not isinstance(plans, list):
        raise SnapshotError("native_p0_binding_candidate_plans_missing")
    bindings: list[dict[str, Any]] = []
    for plan in plans:
        if not isinstance(plan, Mapping):
            raise SnapshotError("native_p0_binding_candidate_plan_invalid")
        if str(plan.get("action") or "") != "NOOP_ALREADY_CONSUMABLE":
            continue
        if str(plan.get("candidate_state") or "") != "PRODUCTION_NO_LIVE_OOS_PASS":
            raise SnapshotError("native_p0_binding_candidate_not_pass")
        scope = plan.get("scope")
        pointer = plan.get("artifact_pointer")
        expected = plan.get("expected_artifact")
        if not all(isinstance(value, Mapping) for value in (scope, pointer, expected)):
            raise SnapshotError("native_p0_binding_pointer_missing")
        if repair_mask is not None:
            if plan.get("selected_system_ids") != repair_mask:
                raise SnapshotError("native_p0_binding_selected_system_mask_mismatch")
            try:
                scope_mask = json.loads(str(scope.get("ensemble_system_ids_json") or ""))
            except (TypeError, json.JSONDecodeError) as exc:
                raise SnapshotError(
                    "native_p0_binding_selected_system_mask_mismatch"
                ) from exc
            if _selected_system_mask(scope_mask) != repair_mask:
                raise SnapshotError("native_p0_binding_selected_system_mask_mismatch")
        station_id = str(scope.get("station_id") or "").strip().upper()
        selection_sha = str(plan.get("selection_evidence_sha256") or "").lower()
        cycle_json = str(scope.get("run_cycle_by_system_json") or "")
        cycle_sha = str(scope.get("run_cycle_by_system_sha256") or "").lower()
        try:
            cycle_mapping = json.loads(cycle_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SnapshotError("native_p0_binding_cycle_mapping_invalid") from exc
        pointer_cross_sha = str(
            pointer.get("cross_artifact_content_sha256") or ""
        ).lower()
        expected_cross_sha = str(
            expected.get("cross_artifact_content_sha256") or ""
        ).lower()
        cross_sha_match = (
            not pointer_cross_sha and not expected_cross_sha
        ) or (
            len(pointer_cross_sha) == 64
            and len(expected_cross_sha) == 64
            and pointer_cross_sha == expected_cross_sha
        )
        pointer_activation_sha = str(
            pointer.get("activation_identity_sha256") or ""
        ).lower()
        expected_activation_sha = str(
            expected.get("activation_identity_sha256") or ""
        ).lower()
        activation_sha_match = (
            not pointer_activation_sha and not expected_activation_sha
        ) or (
            len(pointer_activation_sha) == 64
            and len(expected_activation_sha) == 64
            and pointer_activation_sha == expected_activation_sha
        )
        pointer_generation = pointer.get("generation")
        expected_generation = expected.get("generation")
        generation_match = (
            pointer_generation in (None, "")
            and expected_generation in (None, "")
        ) or (
            pointer_generation is not None
            and expected_generation is not None
            and str(pointer_generation) == str(expected_generation)
        )
        if (
            not station_id
            or station_id not in active
            or len(selection_sha) != 64
            or not isinstance(cycle_mapping, dict)
            or not cycle_mapping
            or content_sha256(cycle_mapping) != cycle_sha
            or str(pointer.get("authority_scope") or "")
            != "PRODUCTION_NO_LIVE_ONLY"
            or str(pointer.get("selection_evidence_sha256") or "").lower()
            != selection_sha
            or str(pointer.get("cross_ensemble_artifact_id") or "")
            != str(expected.get("cross_ensemble_artifact_id") or "")
            or not cross_sha_match
            or str(pointer.get("activation_id") or "")
            != str(expected.get("activation_id") or "")
            or not activation_sha_match
            or not generation_match
        ):
            raise SnapshotError("native_p0_binding_identity_mismatch")
        pointer_identity = {
            key: value for key, value in pointer.items()
            if key != "pointer_sha256"
        }
        if str(pointer.get("pointer_sha256") or "").lower() != content_sha256(pointer_identity):
            raise SnapshotError("native_p0_binding_pointer_hash_mismatch")
        bindings.append({
            "station_id": station_id,
            "season_bin": str(scope.get("season_bin") or ""),
            "lead_bucket": str(scope.get("lead_bucket") or ""),
            "run_cycle": str(scope.get("run_cycle") or ""),
            "run_cycle_by_system_json": cycle_json,
            "run_cycle_by_system_sha256": cycle_sha,
            "selection_evidence_sha256": selection_sha,
            "artifact_pointer": dict(pointer),
        })
    bindings.sort(key=lambda item: item["station_id"])
    station_ids = [item["station_id"] for item in bindings]
    if len(set(station_ids)) != len(station_ids):
        raise SnapshotError("native_p0_binding_duplicate_station")
    # The linkage audit and the repair plan are two views of one immutable
    # current pointer.  A subset check would silently allow an active DB row
    # (for example a prior cutoff's stale activation) to remain outside the
    # pointer consumed by downstream stages.  Require exact station-set
    # equality so the atomic snapshot fails closed on any split SSOT.
    if set(station_ids) != active:
        raise SnapshotError("native_p0_binding_station_set_mismatch")
    declared_count = int(repair.get("artifact_pointer_count") or 0)
    if not bindings or declared_count != len(bindings):
        raise SnapshotError("native_p0_binding_pointer_count_mismatch")
    identity = {
        "schema": "native_p0_consumable_binding_v1",
        "as_of_utc": linkage_as_of,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "source_linkage_content_sha256": linkage_sha,
        "source_repair_content_sha256": repair_sha,
        "binding_count": len(bindings),
        "station_ids": station_ids,
        "bindings": bindings,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build(args: argparse.Namespace) -> dict[str, Any]:
    input_paths = {
        "p0_inbox": _path(args.p0_inbox),
        "taf_audit": _path(args.taf_audit),
        "native_linkage": _path(args.native_linkage),
        "native_repair": _path(args.native_repair),
    }
    output = _path(args.output, output=True)
    if output in input_paths.values():
        raise SnapshotError("snapshot_output_input_collision")
    retries = max(1, min(int(args.max_retries), MAX_RETRIES))
    # A coherent manifest is a bounded read window.  If any source changes
    # between the beginning and end of the window, retry the whole set rather
    # than mixing one fresh P0 with three stale audits.
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            loaded: dict[str, dict[str, Any]] = {}
            for key, path in input_paths.items():
                try:
                    loaded[key] = _read_stable_json(path, retries=1)
                except SnapshotError as exc:
                    if str(exc).startswith("snapshot_declared_hash_mismatch:"):
                        raise SnapshotError(
                            f"snapshot_{key}_content_address_invalid"
                        ) from exc
                    raise
            # Confirm that every file is still byte-stable after the other
            # files were read.  This catches the resident daemon replacing P0
            # while TAF/native evidence was being read.
            for key, item in loaded.items():
                current = input_paths[key].stat()
                stat = item["stat"]
                if (
                    int(current.st_size) != int(stat["size"])
                    or int(current.st_mtime_ns) != int(stat["mtime_ns"])
                    or int(current.st_ino) != int(stat["inode"])
                ):
                    raise SnapshotError(f"snapshot_input_changed_after_window:{key}")
            break
        except (OSError, SnapshotError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(0.05 * (attempt + 1))
    else:
        raise SnapshotError(str(last_error or "snapshot_window_failed"))

    canonical = [
        str(item).strip().upper()
        for item in coordinate_registry_manifest()["station_ids"]
    ]
    if (
        len(canonical) != 45
        or len(set(canonical)) != 45
        or any(not station for station in canonical)
    ):
        raise SnapshotError("snapshot_canonical_station_count_not_45")
    values = {key: item["value"] for key, item in loaded.items()}
    for key, item in loaded.items():
        if item.get("schema") != INPUT_SCHEMAS[key]:
            raise SnapshotError(f"snapshot_{key}_schema_invalid")
        if item.get("declared_content_hash_valid") is not True:
            raise SnapshotError(f"snapshot_{key}_content_address_invalid")
    scopes = {
        "canonical": sorted(canonical),
        "p0_inbox": _p0_stations(values["p0_inbox"]),
        "taf_audit": _taf_stations(values["taf_audit"]),
        "native_linkage": _native_stations(values["native_linkage"]),
        "native_repair": _station_ids(values["native_repair"], key="canonical_station_ids"),
    }
    blockers = _scope_blockers(canonical=canonical, scopes=scopes)
    input_artifacts: dict[str, dict[str, Any]] = {}
    for key, item in loaded.items():
        metadata = {field: value for field, value in item.items() if field not in {"value", "stat"}}
        metadata["station_ids"] = scopes[key]
        if key == "p0_inbox":
            metadata["packet_count"] = len(values[key].get("packets") or [])
        if key == "taf_audit":
            metadata["entry_count"] = len(values[key].get("entries") or [])
            taf_inputs = values[key].get("input_artifacts")
            p0_input = taf_inputs.get("p0_inbox") if isinstance(taf_inputs, Mapping) else None
            metadata["p0_inbox_content_sha256"] = (
                p0_input.get("content_sha256")
                if isinstance(p0_input, Mapping)
                else values[key].get("p0_inbox_content_sha256")
            )
        input_artifacts[key] = metadata

    # If the TAF audit already carries the P0 input pointer, require the
    # pointer to match this exact byte snapshot.  Older receipts are explicitly
    # unusable for a coherent manifest and must be rebuilt, not guessed.
    taf_bound_p0 = input_artifacts["taf_audit"].get("p0_inbox_content_sha256")
    p0_declared = input_artifacts["p0_inbox"].get("declared_content_sha256")
    p0_revalidated = input_artifacts["p0_inbox"].get("revalidated_content_sha256")
    if not taf_bound_p0:
        raise SnapshotError("snapshot_taf_p0_input_pointer_missing")
    if (
        len(str(p0_declared or "")) != 64
        or str(p0_declared).lower() != str(p0_revalidated or "").lower()
    ):
        raise SnapshotError("snapshot_p0_content_address_invalid")
    # Bind the TAF audit to the content address recomputed from the bounded
    # P0 bytes just consumed, not merely to a self-declared field in that
    # inbox.  A resident replacement after the TAF audit was built is stale
    # evidence and must never be silently combined into an OOS snapshot.
    if str(taf_bound_p0).lower() != str(p0_revalidated).lower():
        raise SnapshotError("snapshot_taf_p0_input_pointer_mismatch")

    taf_inputs = values["taf_audit"].get("input_artifacts")
    taf_p0_input = taf_inputs.get("p0_inbox") if isinstance(taf_inputs, Mapping) else None
    taf_p0_generated = (
        taf_p0_input.get("generated_at_utc")
        if isinstance(taf_p0_input, Mapping)
        else None
    )
    p0_generated_text, p0_generated_at = _utc(
        values["p0_inbox"].get("generated_at_utc"),
        field="p0_inbox_generated_at_utc",
    )
    taf_p0_generated_text, taf_p0_generated_at = _utc(
        taf_p0_generated,
        field="taf_p0_inbox_generated_at_utc",
    )
    if p0_generated_at != taf_p0_generated_at:
        raise SnapshotError("snapshot_taf_p0_frozen_at_mismatch")

    linkage_as_of_text, linkage_as_of = _utc(
        values["native_linkage"].get("as_of_utc"),
        field="native_linkage_as_of_utc",
    )
    repair_as_of_text, repair_as_of = _utc(
        values["native_repair"].get("as_of_utc"),
        field="native_repair_as_of_utc",
    )
    requested_as_of = str(args.as_of_utc or "").strip()
    as_of_text, as_of = _utc(
        requested_as_of or linkage_as_of_text,
        field="snapshot_as_of_utc",
    )
    taf_evidence_clock_text, taf_evidence_clock, taf_evidence_clock_field = (
        _taf_evidence_clock(values["taf_audit"])
    )
    _not_after_snapshot_as_of(
        p0_generated_at, snapshot_as_of=as_of,
        field="p0_inbox_generated_at_utc",
    )
    _not_after_snapshot_as_of(
        taf_p0_generated_at, snapshot_as_of=as_of,
        field="taf_p0_inbox_generated_at_utc",
    )
    _not_after_snapshot_as_of(
        taf_evidence_clock, snapshot_as_of=as_of,
        field=f"taf_audit_{taf_evidence_clock_field}",
    )
    _not_after_snapshot_as_of(
        linkage_as_of, snapshot_as_of=as_of,
        field="native_linkage_as_of_utc",
    )
    _not_after_snapshot_as_of(
        repair_as_of, snapshot_as_of=as_of,
        field="native_repair_as_of_utc",
    )
    # Preserve the normalized direct-input clocks in the frozen artifact so
    # downstream readers never need to infer them from file metadata.
    input_artifacts["p0_inbox"]["generated_at_utc"] = p0_generated_text
    input_artifacts["taf_audit"][taf_evidence_clock_field] = (
        taf_evidence_clock_text
    )
    input_artifacts["taf_audit"]["evidence_clock_utc"] = taf_evidence_clock_text
    input_artifacts["taf_audit"]["evidence_clock_field"] = taf_evidence_clock_field
    input_artifacts["taf_audit"]["p0_inbox_generated_at_utc"] = taf_p0_generated_text
    input_artifacts["native_linkage"]["as_of_utc"] = linkage_as_of_text
    input_artifacts["native_repair"]["as_of_utc"] = repair_as_of_text

    scope_fingerprint = _scope_fingerprint(canonical=canonical, scopes=scopes)
    native_p0_binding = _native_p0_binding(
        values["native_linkage"], values["native_repair"],
    )
    identity = {
        "schema": SCHEMA,
        "state": "BLOCKED" if blockers else "FROZEN_READ_ONLY",
        "blockers": blockers,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "as_of_utc": as_of_text,
        "read_only": True,
        "production_no_live": True,
        "network_fetches": 0,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "max_retries": retries,
        "canonical_station_count": len(canonical),
        "canonical_station_ids": canonical,
        "scope_station_ids": scopes,
        "scope_fingerprint": scope_fingerprint,
        "input_artifacts": input_artifacts,
        "native_p0_binding": native_p0_binding,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for key, path in DEFAULTS.items():
        parser.add_argument(f"--{key.replace('_', '-')}", type=Path, default=path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of-utc", default=None)
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args(argv)
    result = build(args)
    wrote = write_bounded_json_if_changed(args.output, result, limit=MAX_INPUT_BYTES)
    print(json.dumps({
        "schema": SCHEMA,
        "state": result["state"],
        "blockers": result["blockers"],
        "wrote": wrote,
        "output": str(Path(args.output).expanduser().resolve()),
        "content_sha256": result["content_sha256"],
        "scope_fingerprint": result["scope_fingerprint"],
        "p0_station_count": len(result["scope_station_ids"]["p0_inbox"]),
        "taf_station_count": len(result["scope_station_ids"]["taf_audit"]),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
