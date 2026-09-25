"""Read-only PR08/PR09 canonical single-system OOS terminal builder.

The builder consumes only active V6 canonical facts.  Retired readiness
audits may explain why these scopes were selected for remediation, but they
are not inputs and their metrics are never re-wrapped as canonical OOS.

V6 keeps member extraction, its exact receipt manifest, native arrivals and
their explicit bindings as four immutable facts.  A shared run/cycle is never
a legal inferred join, and one binding never implies whole-set coverage.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .canonical_contract import FORMAL_TRUTH_LANES
from .canonical_contract import (
    CanonicalEnsembleMemberArrivalBindingFact,
    CanonicalEnsembleMemberFact,
    CanonicalEnsembleMemberReceiptManifestFact,
    CanonicalNativeArrivalFact,
    CanonicalTruthFact,
)
from .canonical_ssot_schema import (
    canonical_state_at_least,
    inspect_canonical_ssot_schema,
)
from .contracts import ContractError, content_sha256
from .model_system_coverage import canonical_station_ids


SCHEMA = "native_ensemble_single_system_strict_historical_oos_manifest_v4"
BLOCKED_SCOPE_SCHEMA = (
    "native_ensemble_single_system_strict_historical_oos_blocked_scope_v3"
)

SYSTEM_SPECS: dict[str, dict[str, Any]] = {
    "NOAA_GEFS_05": {
        "provider_id": "NOAA",
        "independence_family_id": "NOAA_GEFS",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "expected_member_count": 31,
        "stations": canonical_station_ids(),
    },
    "ECCC_GEM_GLOBAL_ENS": {
        "provider_id": "ECCC",
        "independence_family_id": "ECCC_GEM",
        "lead_bucket": "d2",
        "run_cycle": "00Z",
        "expected_member_count": 21,
        "stations": (
            "CYYZ",
            "EPWA",
            "FACT",
            "LEMD",
            "LFPB",
            "MPMG",
            "OEJN",
            "OPKC",
            "RKPK",
            "SBGR",
        ),
    },
}
EXPECTED_ELIGIBILITY_COUNT = {
    system_id: len(spec["stations"]) for system_id, spec in SYSTEM_SPECS.items()
}


def _text(value: Any, code: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(code)
    return result


def _sha(value: Any, code: str) -> str:
    result = _text(value, code).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(code)
    return result


def _utc(value: Any, code: str) -> tuple[str, datetime]:
    raw = _text(value, code)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z"), parsed


def _load_fact(
    fact_type: str,
    fact_id: Any,
    stored_sha: Any,
    fact_json: Any,
    ingested_at_utc: Any,
    *,
    compiled_at: datetime,
) -> tuple[Any, datetime] | None:
    _ingested, ingested_at = _utc(
        ingested_at_utc, "native_system_artifact_ingested_at_invalid"
    )
    if ingested_at > compiled_at:
        return None
    try:
        mapping = json.loads(str(fact_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("native_system_artifact_fact_json_invalid") from exc
    if not isinstance(mapping, Mapping):
        raise ContractError("native_system_artifact_fact_mapping_required")
    sha = _sha(stored_sha, "native_system_artifact_stored_sha_invalid")
    if _sha(mapping.get("content_sha256"), "native_system_artifact_fact_sha_invalid") != sha:
        raise ContractError("native_system_artifact_fact_sha_mismatch")
    if str(fact_id) != f"{fact_type}:{sha}":
        raise ContractError("native_system_artifact_fact_id_mismatch")
    parser = {
        "ensemble_member": CanonicalEnsembleMemberFact.from_mapping,
        "native_arrival": CanonicalNativeArrivalFact.from_mapping,
        "ensemble_member_receipt_manifest": (
            CanonicalEnsembleMemberReceiptManifestFact.from_mapping
        ),
        "ensemble_member_arrival_binding": (
            CanonicalEnsembleMemberArrivalBindingFact.from_mapping
        ),
        "truth": CanonicalTruthFact.from_mapping,
    }[fact_type]
    return parser(mapping), ingested_at


def _canonical_catalog(
    conn: sqlite3.Connection, *, compiled_at: datetime
) -> dict[str, Any]:
    inspection = inspect_canonical_ssot_schema(conn)
    # A floor, not a closed set.  The set said {V8, V9, V10} and the store
    # reached V17, so this builder raised on every call it could receive --
    # including the four tests that exist to check it.  The columns have not
    # moved since V10 (identical seven, verified 2026-09-09) and no fact_type
    # has ever been withdrawn: V17 adds twelve to V10's fourteen and removes
    # none, and the four this builder reads are V6-era types that survive in
    # all of them.  So the bound that was meant is a minimum, and there is a
    # function for exactly that sitting one import away.  The error name is
    # kept -- it is what the callers and tests match on -- even though the
    # floor it now states is V8, which is the floor the closed set stated too.
    if (
        not canonical_state_at_least(str(inspection["state"]), "V8")
        or inspection["errors"]
    ):
        raise ContractError("native_system_artifact_canonical_ssot_v7_required")

    members: dict[tuple[str, str, str], list[CanonicalEnsembleMemberFact]] = defaultdict(list)
    arrivals: dict[str, list[CanonicalNativeArrivalFact]] = defaultdict(list)
    members_by_hash: dict[str, CanonicalEnsembleMemberFact] = {}
    arrivals_by_hash: dict[str, CanonicalNativeArrivalFact] = {}
    manifests_by_hash: dict[str, CanonicalEnsembleMemberReceiptManifestFact] = {}
    manifests_by_member_hash: dict[
        str, list[CanonicalEnsembleMemberReceiptManifestFact]
    ] = defaultdict(list)
    bindings_by_member_hash: dict[
        str, list[CanonicalEnsembleMemberArrivalBindingFact]
    ] = defaultdict(list)
    pending_bindings: list[CanonicalEnsembleMemberArrivalBindingFact] = []
    truths: dict[tuple[str, str], list[CanonicalTruthFact]] = defaultdict(list)
    counts: Counter[str] = Counter()
    for fact_type in (
        "ensemble_member",
        "native_arrival",
        "ensemble_member_receipt_manifest",
        "ensemble_member_arrival_binding",
        "truth",
    ):
        for row in conn.execute(
            "SELECT fact_id,content_sha256,fact_json,ingested_at_utc "
            "FROM canonical_ssot_fact WHERE fact_type=? ORDER BY fact_id",
            (fact_type,),
        ):
            loaded = _load_fact(
                fact_type, *row, compiled_at=compiled_at
            )
            if loaded is None:
                continue
            fact, _ingested_at = loaded
            counts[fact_type] += 1
            if fact_type == "ensemble_member":
                member = fact
                assert isinstance(member, CanonicalEnsembleMemberFact)
                if member.ensemble_system_id in SYSTEM_SPECS:
                    members[(member.ensemble_system_id, member.station_id, member.event_date)].append(member)
                    members_by_hash[member.content_sha256] = member
            elif fact_type == "native_arrival":
                arrival = fact
                assert isinstance(arrival, CanonicalNativeArrivalFact)
                if arrival.ensemble_system_id in SYSTEM_SPECS:
                    arrivals[arrival.ensemble_system_id].append(arrival)
                    arrivals_by_hash[arrival.content_sha256] = arrival
            elif fact_type == "ensemble_member_receipt_manifest":
                manifest = fact
                assert isinstance(
                    manifest, CanonicalEnsembleMemberReceiptManifestFact
                )
                manifests_by_hash[manifest.content_sha256] = manifest
                manifests_by_member_hash[
                    manifest.ensemble_member_content_sha256
                ].append(manifest)
            elif fact_type == "ensemble_member_arrival_binding":
                binding = fact
                assert isinstance(
                    binding, CanonicalEnsembleMemberArrivalBindingFact
                )
                pending_bindings.append(binding)
            else:
                truth = fact
                assert isinstance(truth, CanonicalTruthFact)
                if truth.source_lane in FORMAL_TRUTH_LANES and truth.closed_day:
                    _committed, committed_at = _utc(
                        truth.canonical_committed_at_utc,
                        "native_system_artifact_truth_commit_invalid",
                    )
                    _day_end, day_end = _utc(
                        truth.station_day_end_utc,
                        "native_system_artifact_truth_day_end_invalid",
                    )
                    if committed_at < day_end:
                        raise ContractError("native_system_artifact_truth_committed_before_day_end")
                    if committed_at <= compiled_at:
                        truths[(truth.station_id, truth.event_date)].append(truth)
    visible_manifest_member_hashes: set[str] = set()
    for member_hash, manifests in manifests_by_member_hash.items():
        member = members_by_hash.get(member_hash)
        if member is None:
            raise ContractError("native_system_artifact_manifest_member_missing")
        if len(manifests) != 1:
            raise ContractError("native_system_artifact_manifest_scope_ambiguous")
        manifest = manifests[0]
        if (
            member.source_class != "native_official"
            or manifest.source_raw_receipt_set_sha256
            != member.raw_receipt_set_sha256
        ):
            raise ContractError("native_system_artifact_manifest_lineage_mismatch")
        if any(
            _utc(
                ref.receipt_evidence_at_utc,
                "native_system_artifact_manifest_evidence_at_invalid",
            )[1]
            > compiled_at
            for ref in manifest.required_receipt_refs
        ):
            continue
        visible_manifest_member_hashes.add(member_hash)

    covered_ordinals_by_member: dict[str, set[int]] = defaultdict(set)
    for binding in pending_bindings:
        member = members_by_hash.get(binding.ensemble_member_content_sha256)
        arrival = arrivals_by_hash.get(binding.native_arrival_content_sha256)
        manifest = manifests_by_hash.get(
            binding.receipt_manifest_content_sha256
        )
        if member is None or arrival is None or manifest is None:
            raise ContractError("native_system_artifact_binding_endpoint_missing")
        if member.content_sha256 not in visible_manifest_member_hashes:
            continue
        if manifest.ensemble_member_content_sha256 != member.content_sha256:
            raise ContractError("native_system_artifact_binding_manifest_mismatch")
        if member.source_class != "native_official":
            raise ContractError("native_system_artifact_binding_member_not_native")
        if not (
            arrival.source_class == "native_official"
            and arrival.native_arrival_indexed
            and arrival.published_at_known
            and arrival.received_at_known
            and arrival.raw_object_sha256
        ):
            raise ContractError("native_system_artifact_binding_arrival_not_indexed")
        expected = (
            member.ensemble_system_id,
            member.provider_id,
            member.run_init_utc,
            member.lead_bucket,
            member.run_cycle,
        )
        if expected != (
            arrival.ensemble_system_id,
            arrival.provider_id,
            arrival.run_init_utc,
            arrival.lead_bucket,
            arrival.run_cycle,
        ):
            raise ContractError("native_system_artifact_binding_scope_mismatch")
        refs = manifest.required_receipt_refs
        if any(ordinal >= len(refs) for ordinal in binding.manifest_entry_ordinals):
            raise ContractError("native_system_artifact_binding_ordinal_out_of_range")
        if any(
            refs[ordinal].native_object_id != arrival.native_object_id
            or refs[ordinal].raw_object_sha256 != arrival.raw_object_sha256
            for ordinal in binding.manifest_entry_ordinals
        ):
            raise ContractError("native_system_artifact_binding_lineage_mismatch")
        _received_at, received_at = _utc(
            arrival.received_at_utc,
            "native_system_artifact_binding_received_at_invalid",
        )
        if received_at > compiled_at:
            continue
        covered = covered_ordinals_by_member[member.content_sha256]
        if covered.intersection(binding.manifest_entry_ordinals):
            raise ContractError("native_system_artifact_binding_ordinal_overlap")
        covered.update(binding.manifest_entry_ordinals)
        bindings_by_member_hash[member.content_sha256].append(binding)
    return {
        "members": members,
        "arrivals": arrivals,
        "truths": truths,
        "bindings_by_member_hash": bindings_by_member_hash,
        "manifests_by_member_hash": manifests_by_member_hash,
        "covered_ordinals_by_member": covered_ordinals_by_member,
        "visible_manifest_member_hashes": visible_manifest_member_hashes,
        "visible_fact_counts": dict(sorted(counts.items())),
        "member_arrival_binding_fact_type_present": True,
    }


def _scope_diagnostics(
    *, station_id: str, system_id: str, catalog: Mapping[str, Any]
) -> dict[str, Any]:
    spec = SYSTEM_SPECS[system_id]
    expected = int(spec["expected_member_count"])
    date_rows: list[dict[str, Any]] = []
    member_dates = sorted(
        event_date
        for candidate_system, candidate_station, event_date in catalog["members"]
        if candidate_system == system_id and candidate_station == station_id
    )
    for event_date in member_dates:
        members = catalog["members"][(system_id, station_id, event_date)]
        ids = [member.member_id for member in members]
        roles = Counter(member.member_role for member in members)
        exact_cohort = (
            len(members) == expected
            and len(set(ids)) == expected
            and roles["CONTROL"] == 1
            and roles["PERTURBED"] == expected - 1
            and all(member.provider_id == spec["provider_id"] for member in members)
            and all(member.source_class == "native_official" for member in members)
            and all(member.lead_bucket == spec["lead_bucket"] for member in members)
            and all(member.run_cycle == spec["run_cycle"] for member in members)
        )
        strict_cohort = exact_cohort and all(
            member.closed_day
            and member.strict_asof
            and member.oos_role == "historical_strict_oos"
            for member in members
        )
        exact_binding_coverage = exact_cohort and all(
            member.content_sha256
            in catalog["visible_manifest_member_hashes"]
            and
            len(catalog["manifests_by_member_hash"].get(member.content_sha256, ()))
            == 1
            and catalog["covered_ordinals_by_member"].get(
                member.content_sha256, set()
            )
            == set(
                range(
                    len(
                        catalog["manifests_by_member_hash"][
                            member.content_sha256
                        ][0].required_receipt_refs
                    )
                )
            )
            for member in members
        )
        truth_rows = catalog["truths"].get((station_id, event_date), [])
        date_rows.append(
            {
                "event_date": event_date,
                "member_count": len(members),
                "exact_member_cohort": exact_cohort,
                "strict_member_cohort": strict_cohort,
                "exact_member_receipt_coverage": exact_binding_coverage,
                "strict_bound_member_cohort": (
                    strict_cohort and exact_binding_coverage
                ),
                "closed_awc_truth_revision_count": len(truth_rows),
                "exact_closed_awc_truth": len(truth_rows) == 1,
            }
        )
    arrivals = catalog["arrivals"].get(system_id, [])
    return {
        "candidate_member_fact_count": sum(row["member_count"] for row in date_rows),
        "candidate_member_date_count": len(date_rows),
        "exact_member_cohort_date_count": sum(row["exact_member_cohort"] for row in date_rows),
        "strict_member_cohort_date_count": sum(row["strict_member_cohort"] for row in date_rows),
        "strict_bound_member_cohort_date_count": sum(
            row["strict_bound_member_cohort"] for row in date_rows
        ),
        "exact_member_receipt_coverage": bool(date_rows) and all(
            row["exact_member_receipt_coverage"] for row in date_rows
        ),
        "exact_member_truth_overlap_date_count": sum(
            row["exact_member_cohort"] and row["exact_closed_awc_truth"] for row in date_rows
        ),
        "indexed_native_arrival_fact_count": sum(
            arrival.native_arrival_indexed
            and arrival.published_at_known
            and arrival.received_at_known
            for arrival in arrivals
        ),
        "member_arrival_binding_fact_count": sum(
            len(catalog["bindings_by_member_hash"].get(member.content_sha256, ()))
            for event_date in member_dates
            for member in catalog["members"][(system_id, station_id, event_date)]
        ),
        "member_receipt_manifest_fact_count": sum(
            len(catalog["manifests_by_member_hash"].get(member.content_sha256, ()))
            for event_date in member_dates
            for member in catalog["members"][(system_id, station_id, event_date)]
        ),
        "expected_member_count": expected,
        "date_rows_content_sha256": content_sha256(date_rows),
    }


def _blocked_scope(
    *, station_id: str, system_id: str, catalog: Mapping[str, Any]
) -> dict[str, Any]:
    spec = SYSTEM_SPECS[system_id]
    diagnostics = _scope_diagnostics(
        station_id=station_id, system_id=system_id, catalog=catalog
    )
    detail: list[str] = []
    if diagnostics["strict_member_cohort_date_count"] == 0:
        detail.append("CANONICAL_MEMBERS_ARE_OBSERVATIONAL_NOT_STRICT_OOS")
    if diagnostics["indexed_native_arrival_fact_count"] == 0:
        detail.append("CANONICAL_NATIVE_ARRIVAL_MISSING")
    if diagnostics["member_arrival_binding_fact_count"] == 0:
        detail.append("CANONICAL_MEMBER_ARRIVAL_BINDING_MISSING")
    identity = {
        "schema": BLOCKED_SCOPE_SCHEMA,
        "state": "BLOCKED",
        "blockers": ["LINEAGE_MISMATCH"],
        "blocker_detail": detail,
        "station_id": station_id,
        "ensemble_system_id": system_id,
        "provider_id": spec["provider_id"],
        "model_family": "global_ensemble",
        "independence_family_id": spec["independence_family_id"],
        "lead_bucket": spec["lead_bucket"],
        "run_cycle": spec["run_cycle"],
        "canonical_diagnostics": diagnostics,
        "legacy_audit_read": False,
        "legacy_metrics_reused": False,
        "inferred_member_arrival_join": False,
        "artifact_count": 0,
        "cross_selection_count": 0,
        "cross_activation_count": 0,
        "p0_count": 0,
        "formal_live_authority": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_single_system_oos_artifacts(
    conn: sqlite3.Connection, *, compiled_at_utc: str
) -> dict[str, Any]:
    """Return all 55 registered scopes with canonical-only terminal states."""

    compiled_at, compiled_at_dt = _utc(
        compiled_at_utc, "native_system_artifact_compiled_at_invalid"
    )
    if int(conn.execute("PRAGMA query_only").fetchone()[0] or 0) != 1:
        raise ContractError("native_system_artifact_read_only_connection_required")
    catalog = _canonical_catalog(conn, compiled_at=compiled_at_dt)
    blocked_scopes = [
        _blocked_scope(station_id=station_id, system_id=system_id, catalog=catalog)
        for system_id, spec in sorted(SYSTEM_SPECS.items())
        for station_id in spec["stations"]
    ]
    blocked_scopes.sort(key=lambda row: (row["ensemble_system_id"], row["station_id"]))
    by_system = {
        system_id: sum(row["ensemble_system_id"] == system_id for row in blocked_scopes)
        for system_id in sorted(SYSTEM_SPECS)
    }
    if by_system != EXPECTED_ELIGIBILITY_COUNT:
        raise ContractError("native_system_artifact_exact_scope_registry_invalid")
    identity = {
        "schema": SCHEMA,
        "compiled_at_utc": compiled_at,
        "scope_registry_count": len(blocked_scopes),
        "scope_registry_count_by_system": EXPECTED_ELIGIBILITY_COUNT,
        "visible_canonical_fact_counts": catalog["visible_fact_counts"],
        "member_arrival_binding_fact_type_present": catalog[
            "member_arrival_binding_fact_type_present"
        ],
        "artifact_count": 0,
        "blocked_scope_count": len(blocked_scopes),
        "blocked_scope_count_by_system": by_system,
        "artifacts": [],
        "blocked_scopes": blocked_scopes,
        "writes_database": False,
        "legacy_audit_read": False,
        "legacy_metrics_reused": False,
        "cross_selection_count": 0,
        "cross_activation_count": 0,
        "p0_count": 0,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "formal_live_authority": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "BLOCKED_SCOPE_SCHEMA",
    "EXPECTED_ELIGIBILITY_COUNT",
    "SCHEMA",
    "SYSTEM_SPECS",
    "build_single_system_oos_artifacts",
]
