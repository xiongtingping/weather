"""PR08/PR09 parser for the shared model-system coverage contract.

The two historical audit manifests are only input evidence.  This module
does not introduce a terminal schema, ledger, artifact, activation, or P0
writer.  Every returned row is created by ``make_coverage_row`` and the
returned 270-row matrix is created by ``build_coverage_matrix``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .contracts import ContractError, content_sha256
from .model_system_coverage import (
    LEADS,
    canonical_station_ids,
    build_coverage_matrix,
    make_coverage_row,
)


AUDIT_SCHEMA = "native_ensemble_forward_oos_manifest_v1"
REQUIRED_TRAIN_COUNT = 20
REQUIRED_HOLDOUT_COUNT = 8


# These source contracts are deliberately limited to the official d2 archive
# evidence currently retained.  A non-native / unknown-cycle d0 row in an
# audit cannot be re-labelled as a native d0 fact by this adapter.
SYSTEM_SPECS: dict[str, dict[str, str]] = {
    "NOAA_GEFS_05": {
        "provider_id": "NOAA",
        "independence_family_id": "NOAA_GEFS",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
    },
    "ECCC_GEM_GLOBAL_ENS": {
        "provider_id": "ECCC",
        "independence_family_id": "ECCC_GEM",
        "lead_bucket": "d2",
        "run_cycle": "00Z",
    },
}


def _text(value: Any, code: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(code)
    return result


def _timestamp(value: Any, code: str) -> tuple[str, datetime]:
    result = _text(value, code)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z"), parsed


def _sha256(value: Any, code: str) -> str:
    result = _text(value, code).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(code)
    return result


def _count(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise ContractError(code)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(code) from exc
    if result < 0 or result != value:
        raise ContractError(code)
    return result


def _canonical_stations(station_ids: Sequence[str]) -> tuple[str, ...]:
    supplied = tuple(sorted(_text(value, "pr08_pr09_station_missing").upper() for value in station_ids))
    expected = canonical_station_ids()
    if supplied != expected:
        raise ContractError("pr08_pr09_station_set_must_match_canonical_registry")
    return expected


def _audit_reports(
    audit: Mapping[str, Any],
    *,
    system_id: str,
    audit_pointer: str,
    compiled_at: datetime,
) -> tuple[dict[str, Mapping[str, Any]], str]:
    if not isinstance(audit, Mapping) or audit.get("schema") != AUDIT_SCHEMA:
        raise ContractError("pr08_pr09_audit_schema_invalid")
    if _count(audit.get("station_count"), "pr08_pr09_audit_station_count_invalid") != 45:
        raise ContractError("pr08_pr09_audit_exact_45_required")
    if _count(audit.get("min_train_days"), "pr08_pr09_audit_train_threshold_invalid") != REQUIRED_TRAIN_COUNT:
        raise ContractError("pr08_pr09_audit_train_threshold_invalid")
    if _count(audit.get("min_holdout_days"), "pr08_pr09_audit_holdout_threshold_invalid") != REQUIRED_HOLDOUT_COUNT:
        raise ContractError("pr08_pr09_audit_holdout_threshold_invalid")
    if audit.get("calls_order_gateway") is not False or _count(audit.get("orders_submitted"), "pr08_pr09_audit_orders_invalid") != 0:
        raise ContractError("pr08_pr09_audit_order_side_effect_forbidden")
    if audit.get("live_submit_authorized") is not False:
        raise ContractError("pr08_pr09_audit_live_authority_forbidden")

    audit_sha = _sha256(audit.get("content_sha256"), "pr08_pr09_audit_content_sha_invalid")
    identity = dict(audit)
    identity.pop("content_sha256", None)
    if content_sha256(identity) != audit_sha:
        raise ContractError("pr08_pr09_audit_content_sha_mismatch")
    audit_as_of, audit_as_of_dt = _timestamp(audit.get("as_of_utc"), "pr08_pr09_audit_as_of_invalid")
    if audit_as_of_dt > compiled_at:
        raise ContractError("pr08_pr09_audit_after_compiled_at")

    reports = audit.get("reports")
    if not isinstance(reports, list) or len(reports) != 45:
        raise ContractError("pr08_pr09_audit_reports_exact_45_required")
    expected = canonical_station_ids()
    by_station: dict[str, Mapping[str, Any]] = {}
    for report in reports:
        if not isinstance(report, Mapping):
            raise ContractError("pr08_pr09_station_report_mapping_required")
        station_id = _text(report.get("station_id"), "pr08_pr09_station_missing").upper()
        if station_id in by_station:
            raise ContractError("pr08_pr09_station_report_duplicate")
        if not isinstance(report.get("readiness"), list):
            raise ContractError("pr08_pr09_readiness_list_required")
        by_station[station_id] = report
    if tuple(sorted(by_station)) != expected:
        raise ContractError("pr08_pr09_audit_station_set_mismatch")

    # The canonical coverage row has a SHA field, not a free-form pointer.
    # Bind the retained audit content, its strict-as-of time and its explicit
    # repository pointer into that one evidence address.
    source_evidence_sha = content_sha256({
        "audit_schema": AUDIT_SCHEMA,
        "audit_content_sha256": audit_sha,
        "audit_as_of_utc": audit_as_of,
        "audit_pointer": _text(audit_pointer, "pr08_pr09_audit_pointer_missing"),
        "ensemble_system_id": system_id,
    })
    return by_station, source_evidence_sha


def _matching_readiness(
    report: Mapping[str, Any],
    *,
    system_id: str,
    lead_bucket: str,
    run_cycle: str | None,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    exact: list[Mapping[str, Any]] = []
    incompatible: list[Mapping[str, Any]] = []
    for readiness in report["readiness"]:
        if not isinstance(readiness, Mapping):
            raise ContractError("pr08_pr09_readiness_mapping_required")
        if str(readiness.get("ensemble_system_id") or "").strip() != system_id:
            continue
        if str(readiness.get("lead_bucket") or "").strip().lower() != lead_bucket:
            continue
        if run_cycle is not None and str(readiness.get("run_cycle") or "").strip() == run_cycle:
            exact.append(readiness)
        else:
            incompatible.append(readiness)
    return exact, incompatible


def _readiness_counts(readiness: Mapping[str, Any]) -> tuple[int, int, int]:
    return (
        _count(readiness.get("train_n", readiness.get("training_case_count", 0)), "pr08_pr09_train_count_invalid"),
        _count(readiness.get("holdout_n", readiness.get("strict_holdout_case_count", 0)), "pr08_pr09_holdout_count_invalid"),
        _count(readiness.get("case_count"), "pr08_pr09_case_count_invalid"),
    )


def _blocked_row(
    *,
    station_id: str,
    system_id: str,
    lead_bucket: str,
    as_of_utc: str,
    source_evidence_sha: str,
    blocker: str,
) -> dict[str, Any]:
    spec = SYSTEM_SPECS[system_id]
    return make_coverage_row(
        station_id=station_id,
        product_id=system_id,
        provider_id=spec["provider_id"],
        model_family="global_ensemble",
        independence_family_id=spec["independence_family_id"],
        source_class="native_official",
        lead_bucket=lead_bucket,
        run_cycle=spec["run_cycle"] if lead_bucket == spec["lead_bucket"] else None,
        as_of_utc=as_of_utc,
        state="BLOCKED",
        blockers=[blocker],
        evidence_role="observational_candidate_not_oos",
        strict_asof=False,
        closed_awc_truth=False,
        train_count=0,
        holdout_count=0,
        source_evidence_sha256=source_evidence_sha,
        raw_receipt_sha256=None,
        oos_evidence_sha256=None,
        single_system_artifact_sha256=None,
        native_arrival_indexed=False,
        formal_live_authority=False,
        cross_selection_count=0,
        cross_activation_count=0,
        p0_count=0,
    )


def _d2_row(
    *,
    station_id: str,
    system_id: str,
    report: Mapping[str, Any],
    as_of_utc: str,
    source_evidence_sha: str,
) -> dict[str, Any]:
    spec = SYSTEM_SPECS[system_id]
    exact, incompatible = _matching_readiness(
        report,
        system_id=system_id,
        lead_bucket="d2",
        run_cycle=spec["run_cycle"],
    )
    if len(exact) > 1:
        raise ContractError("pr08_pr09_exact_readiness_duplicate")
    if not exact:
        return _blocked_row(
            station_id=station_id,
            system_id=system_id,
            lead_bucket="d2",
            as_of_utc=as_of_utc,
            source_evidence_sha=source_evidence_sha,
            blocker="RUN_CYCLE_MISMATCH" if incompatible else "DATA_MISSING",
        )

    # A legacy readiness row is diagnostic only.  Its historical pipeline
    # substituted run-init for an independently observed arrival clock, and
    # it is not backed by a canonical member/arrival/artifact root.  Do not
    # preserve its strict/OOS/sample claim in the shared coverage matrix.
    # A later version of this builder may consume the canonical-only artifact
    # manifest; until then every exact legacy d2 scope fails closed.
    _readiness_counts(exact[0])
    return _blocked_row(
        station_id=station_id,
        system_id=system_id,
        lead_bucket="d2",
        as_of_utc=as_of_utc,
        source_evidence_sha=source_evidence_sha,
        blocker="LINEAGE_MISMATCH",
    )


def _row_for_scope(
    *,
    station_id: str,
    system_id: str,
    lead_bucket: str,
    report: Mapping[str, Any],
    as_of_utc: str,
    source_evidence_sha: str,
) -> dict[str, Any]:
    if lead_bucket == "d2":
        return _d2_row(
            station_id=station_id,
            system_id=system_id,
            report=report,
            as_of_utc=as_of_utc,
            source_evidence_sha=source_evidence_sha,
        )
    _exact, incompatible = _matching_readiness(
        report,
        system_id=system_id,
        lead_bucket=lead_bucket,
        run_cycle=None,
    )
    return _blocked_row(
        station_id=station_id,
        system_id=system_id,
        lead_bucket=lead_bucket,
        as_of_utc=as_of_utc,
        source_evidence_sha=source_evidence_sha,
        blocker="RUN_CYCLE_MISMATCH" if incompatible else "DATA_MISSING",
    )


def build_pr08_pr09_terminal_matrix(
    *,
    gefs_audit: Mapping[str, Any],
    geps_audit: Mapping[str, Any],
    station_ids: Sequence[str],
    gefs_audit_pointer: str,
    geps_audit_pointer: str,
    compiled_at_utc: str,
) -> dict[str, Any]:
    """Return the shared canonical 45×d0/d1/d2×two-product matrix.

    There are no canonical single-system artifact hashes in these legacy
    audit inputs.  Consequently no d2 row may inherit strict-OOS state,
    sample counts, or ``CANDIDATE_ONLY`` authority from them.
    """

    stations = _canonical_stations(station_ids)
    compiled_at, compiled_at_dt = _timestamp(
        compiled_at_utc,
        "pr08_pr09_compiled_at_invalid",
    )
    inputs = {
        "NOAA_GEFS_05": (gefs_audit, gefs_audit_pointer),
        "ECCC_GEM_GLOBAL_ENS": (geps_audit, geps_audit_pointer),
    }
    rows: list[dict[str, Any]] = []
    for system_id, (audit, pointer) in inputs.items():
        reports, source_evidence_sha = _audit_reports(
            audit,
            system_id=system_id,
            audit_pointer=pointer,
            compiled_at=compiled_at_dt,
        )
        for station_id in stations:
            for lead_bucket in LEADS:
                rows.append(_row_for_scope(
                    station_id=station_id,
                    system_id=system_id,
                    lead_bucket=lead_bucket,
                    report=reports[station_id],
                    as_of_utc=compiled_at,
                    source_evidence_sha=source_evidence_sha,
                ))
    return build_coverage_matrix(
        rows,
        expected_product_ids=tuple(SYSTEM_SPECS),
        as_of_utc=compiled_at,
    )


__all__ = [
    "AUDIT_SCHEMA",
    "REQUIRED_HOLDOUT_COUNT",
    "REQUIRED_TRAIN_COUNT",
    "SYSTEM_SPECS",
    "build_pr08_pr09_terminal_matrix",
]
