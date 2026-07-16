"""Controlled AMSC bundle adapter for the isolated resident P10 shadow host.

An AMSC acquisition response is a *batch*, not an arrival.  This adapter has
one deliberately narrow responsibility: split an already normalised and
raw-archived AMSC bundle into individually validated primary METAR/SPECI
arrival candidates, in immutable first-seen (T1) order.  It neither fetches
AMSC nor writes the production archive, and it never derives T1 from the
current poll's completion timestamp.

Cross-batch physical deduplication is intentionally not implemented here.  It
belongs to ``ResidentAmscSpecliArrivalJournalShadow`` because only that
isolated durable journal can preserve a physical report's first T1 across a
host restart.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from amsc_specli_resident_arrival_contract import (
    ACCEPTED_REPORT_TYPES,
    build_amsc_specli_resident_arrival_envelope,
)
from source_observation_runtime import PRIMARY_METAR_SOURCE_ID


SCHEMA_VERSION = "amsc_p10_shadow_source_adapter_v1"
AMSC_BUNDLE_SCHEMA = "amsc_zuuu_metar_taf_observation_bundle_v1"


class _ResidentP10Host(Protocol):
    def process_source_observation(self, *, source_observation: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[datetime, datetime, str]:
    source = _mapping(candidate.get("source_observation"))
    # A candidate only exists after the envelope has validated T1.  The
    # defensive datetime.min fallback nevertheless keeps output deterministic
    # if a caller inspects a manually constructed result.
    t1 = _parse_dt(candidate.get("first_transport_seen_at")) or datetime.min.replace(tzinfo=timezone.utc)
    valid = _parse_dt(source.get("valid_time")) or datetime.min.replace(tzinfo=timezone.utc)
    return (t1, valid, _text(source.get("source_observation_id")))


def _base_result(bundle: Mapping[str, Any]) -> dict[str, Any]:
    source_bundle = _mapping(bundle)
    return {
        "schema": SCHEMA_VERSION,
        "source_bundle_schema": _text(source_bundle.get("schema")),
        "station_id": _text(source_bundle.get("station_id")).upper(),
        "read_only": True,
        "shadow_only": True,
        "boundary": {
            "acquisition_batch_is_not_an_arrival": True,
            "one_primary_metar_or_speci_per_dispatch": True,
            "t1_from_persisted_first_seen_only": True,
            "requires_raw_archive_lineage": True,
            "persistent_physical_deduplication_owned_by_journal": True,
            "network_acquisition": False,
            "generic_task_queue": False,
            "production_archive_write": False,
            "awc_wait": False,
            "clob": False,
        },
    }


def _archive_lineage_blockers(
    *,
    source: Mapping[str, Any],
    payload: Mapping[str, Any],
    raw_history_archive: Mapping[str, Any],
) -> list[str]:
    """Bind a candidate's T1 to the returned immutable raw object metadata.

    A non-empty object path in an untrusted source row is not enough.  The
    bundle must carry the archive receipt emitted for this acquisition, and
    the candidate must match that receipt byte-for-byte at the identity/T1
    fields that matter for causality.
    """

    blockers: list[str] = []
    archive = _mapping(raw_history_archive)
    manifest_ref = _text(archive.get("manifest_ref"))
    manifest_sha256 = _text(archive.get("manifest_sha256"))
    object_index = _mapping(archive.get("raw_objects_by_sha256"))
    raw_sha256 = _text(source.get("raw_sha256"))
    metadata = _mapping(object_index.get(raw_sha256))
    if not manifest_ref:
        blockers.append("amsc_p10_shadow_adapter_archive_manifest_ref_missing")
    if not manifest_sha256:
        blockers.append("amsc_p10_shadow_adapter_archive_manifest_sha256_missing")
    if not raw_sha256 or not metadata:
        blockers.append("amsc_p10_shadow_adapter_raw_archive_metadata_missing")
        return blockers
    if _text(metadata.get("raw_sha256")) != raw_sha256:
        blockers.append("amsc_p10_shadow_adapter_raw_archive_sha_mismatch")
    if not _text(payload.get("raw_object_ref")):
        blockers.append("amsc_p10_shadow_adapter_raw_object_lineage_missing")
    elif _text(payload.get("raw_object_ref")) != _text(metadata.get("raw_object_ref")):
        blockers.append("amsc_p10_shadow_adapter_raw_object_lineage_mismatch")
    if _text(payload.get("first_seen_at")) != _text(metadata.get("first_seen_at")):
        blockers.append("amsc_p10_shadow_adapter_archive_first_seen_mismatch")
    if _text(payload.get("arrival_time_quality")) != _text(metadata.get("arrival_time_quality")):
        blockers.append("amsc_p10_shadow_adapter_archive_arrival_quality_mismatch")
    transport = _mapping(payload.get("transport"))
    if _text(transport.get("raw_archive_manifest_ref")) != manifest_ref:
        blockers.append("amsc_p10_shadow_adapter_archive_manifest_ref_mismatch")
    if _text(transport.get("raw_archive_manifest_sha256")) != manifest_sha256:
        blockers.append("amsc_p10_shadow_adapter_archive_manifest_sha256_mismatch")
    return blockers


def build_amsc_p10_shadow_arrival_candidates(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Read one raw-archived AMSC bundle and return valid individual arrivals.

    Invalid primary rows are individually rejected rather than repaired from
    the enclosing poll.  Other bundle sources (TAF, AWC, diagnostics) are
    explicitly ignored; their presence cannot delay primary AMSC dispatch.
    """

    source_bundle = _mapping(bundle)
    out = _base_result(source_bundle)
    blockers: list[str] = []
    if _text(source_bundle.get("schema")) != AMSC_BUNDLE_SCHEMA:
        blockers.append("amsc_p10_shadow_adapter_bundle_schema_invalid")
    if source_bundle.get("read_only") is not True:
        blockers.append("amsc_p10_shadow_adapter_bundle_read_only_required")
    if _text(source_bundle.get("station_id")).upper() != "ZUUU":
        blockers.append("amsc_p10_shadow_adapter_station_required")
    observations = source_bundle.get("source_observations")
    if not isinstance(observations, list):
        blockers.append("amsc_p10_shadow_adapter_source_observations_invalid")

    out.update(
        {
            "ok": not blockers,
            "state": "ready" if not blockers else "blocked",
            "blockers": sorted(set(blockers)),
            "arrival_candidates": [],
            "rejected_primary_rows": [],
            "ignored_non_primary_rows": [],
            "duplicate_in_bundle_source_observation_ids": [],
        }
    )
    if blockers:
        return out

    candidates: list[dict[str, Any]] = []
    raw_history_archive = _mapping(source_bundle.get("raw_history_archive"))
    seen_source_observation_ids: set[str] = set()
    rejected: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    duplicate_ids: list[str] = []
    for index, raw in enumerate(observations):
        if not isinstance(raw, Mapping):
            ignored.append({"index": index, "reason": "amsc_p10_shadow_adapter_non_mapping_row"})
            continue
        source = copy.deepcopy(dict(raw))
        source_id = _text(source.get("source_id"))
        payload = _mapping(source.get("payload"))
        report_type = _text(payload.get("report_type") or source.get("report_type")).upper()
        if source_id != PRIMARY_METAR_SOURCE_ID:
            ignored.append(
                {
                    "index": index,
                    "source_observation_id": _text(source.get("source_observation_id")),
                    "source_id": source_id,
                    "report_type": report_type,
                    "reason": "amsc_p10_shadow_adapter_non_primary_source_ignored",
                }
            )
            continue

        source_observation_id = _text(source.get("source_observation_id"))
        if source_observation_id and source_observation_id in seen_source_observation_ids:
            duplicate_ids.append(source_observation_id)
            continue
        if source_observation_id:
            seen_source_observation_ids.add(source_observation_id)

        envelope = build_amsc_specli_resident_arrival_envelope(source)
        adapter_blockers = list(envelope.get("blockers") or []) + _archive_lineage_blockers(
            source=source,
            payload=payload,
            raw_history_archive=raw_history_archive,
        )
        if report_type not in ACCEPTED_REPORT_TYPES:
            adapter_blockers.append("amsc_p10_shadow_adapter_report_type_invalid")
        if adapter_blockers:
            rejected.append(
                {
                    "index": index,
                    "source_observation_id": source_observation_id,
                    "source_id": source_id,
                    "report_type": report_type,
                    "blockers": sorted(set(adapter_blockers)),
                }
            )
            continue
        candidates.append(envelope)

    candidates.sort(key=_candidate_sort_key)
    out.update(
        {
            "arrival_candidates": candidates,
            "candidate_count": len(candidates),
            "rejected_primary_rows": rejected,
            "ignored_non_primary_rows": ignored,
            "duplicate_in_bundle_source_observation_ids": sorted(set(duplicate_ids)),
            "state": "ready_with_rejections" if rejected else "ready",
        }
    )
    return out


def dispatch_amsc_p10_shadow_bundle(
    bundle: Mapping[str, Any],
    *,
    host: _ResidentP10Host,
) -> dict[str, Any]:
    """Dispatch valid candidates one-by-one to the isolated resident host.

    Delivery remains synchronous and ordered.  A host fault stops the current
    batch so a later event cannot overtake an earlier retryable event.  A later
    acquisition batch will hand the physical report back to the journal at the
    original T1; the adapter never schedules a retry or queues work itself.
    """

    selection = build_amsc_p10_shadow_arrival_candidates(bundle)
    if selection.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_before_host_dispatch",
            "selection": selection,
            "dispatches": [],
            "shadow_only": True,
            "places_orders": False,
        }

    dispatches: list[dict[str, Any]] = []
    halted = False
    for envelope in selection.get("arrival_candidates") or []:
        source = _mapping(_mapping(envelope).get("source_observation"))
        try:
            host_result = dict(host.process_source_observation(source_observation=source))
        except Exception as exc:  # Host fault must not permit overtaking.
            dispatches.append(
                {
                    "source_observation_id": _text(source.get("source_observation_id")),
                    "ok": False,
                    "state": "host_exception",
                    "reason": type(exc).__name__,
                }
            )
            halted = True
            break
        journal = _mapping(_mapping(host_result.get("result")).get("journal"))
        terminal_deadline = _text(journal.get("state")) == "deadline_blocked"
        dispatches.append(
            {
                "source_observation_id": _text(source.get("source_observation_id")),
                "ok": host_result.get("ok") is True,
                "state": _text(host_result.get("state")),
                "terminal_deadline_blocked": terminal_deadline,
                "host_result": host_result,
            }
        )
        # A terminal deadline-blocked physical arrival has already been
        # durably recorded by the journal.  It cannot be retried, so it is
        # safe for a later physical arrival to continue.  Every other failed
        # result may be retryable and must retain ordering.
        if host_result.get("ok") is not True and not terminal_deadline:
            halted = True
            break

    ok = not halted and all(row.get("ok") is True for row in dispatches)
    if not dispatches and not halted:
        # Empty is a normal poll outcome, never a manufactured arrival or a
        # synthetic latency success sample.
        state = "no_primary_arrival_candidates"
    elif halted:
        state = "dispatch_halted_before_overtake"
    elif any(row.get("terminal_deadline_blocked") is True for row in dispatches):
        state = "dispatched_with_terminal_deadline_block"
    else:
        state = "dispatched"
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "state": state,
        "selection": selection,
        "dispatch_count": len(dispatches),
        "dispatches": dispatches,
        "p10_latency_sample_created": any(row.get("ok") is True for row in dispatches),
        "shadow_only": True,
        "places_orders": False,
        "boundary": {
            **_base_result(bundle)["boundary"],
            "host_fault_stops_batch_before_overtake": True,
            "adapter_schedules_retry": False,
        },
    }


__all__ = [
    "AMSC_BUNDLE_SCHEMA",
    "SCHEMA_VERSION",
    "build_amsc_p10_shadow_arrival_candidates",
    "dispatch_amsc_p10_shadow_bundle",
]
