"""PR11 45-station unresolved TAF/P0-M basis.

Station-level terminals are builder evidence, not canonical TAF_P0M artifact
facts.  Until a canonical historical P0 root supplies timezone, season, lead,
and run-cycle, the typed artifact key remains unresolved and must not be
guessed from legacy TAF audits or probability envelopes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .canonical_expected_scope_manifest import build_scope_basis
from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .contracts import ContractError, content_sha256
from .model_oos_selection import (
    CanonicalModelOOSSelectionContract,
    TERMINAL_RECEIPT_SCHEMA as PR10_SELECTION_TERMINAL_SCHEMA,
    validate_model_oos_selection_terminal_receipt,
)
from .p0_activation_root import CanonicalP0ActivationRootContract
from .p0_calibrated_distribution import CanonicalP0CalibratedDistributionContract
from .resource_bounds import MAX_JSON_INPUT_BYTES, load_bounded_json
from .station_registry import coordinate_registry_manifest
from .fleet import expected_station_count


SCHEMA = "bayes_basket_target_pr11_taf_p0m_terminal_bundle_v2"
TERMINAL_BASIS_SCHEMA = "bayes_basket_target_pr11_taf_p0m_station_terminal_basis_v1"
TERMINAL_ROW_SCHEMA = "bayes_basket_target_pr11_taf_p0m_station_terminal_v1"
BUILDER_ID = "pr11.canonical_historical_p0_taf_basis.v2"
REQUIRED_CANONICAL_FACT_TYPES = (
    "p0_activation_root",
    "official_taf_revision",
    "taf_p0m_exact_join",
)
REQUIRED_ARTIFACT_KEY_FIELDS = (
    "station_id",
    "settlement_timezone",
    "season",
    "lead_bucket",
    "run_cycle",
)
UNPROVEN_ARTIFACT_KEY_FIELDS = REQUIRED_ARTIFACT_KEY_FIELDS[1:]
EXPECTED_STATION_COUNT = expected_station_count()
LEGACY_GAP_EVIDENCE_SCHEMA = (
    "bayes_basket_target_taf_p0m_exact_join_45station_audit_v1"
)
PR10_SELECTION_TERMINAL_BUNDLE_SCHEMA = (
    "bayes_basket_target_pr10_model_oos_selection_terminal_bundle_v1"
)
PR10_RESERVED_P0_BUNDLE_SCHEMA = "bayes_basket_target_pr10_reserved_p0_bundle_v1"


class PR11TafP0mBasisError(ContractError):
    """The canonical source snapshot cannot establish the PR11 root."""


def _utc_z(value: object, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PR11TafP0mBasisError(f"pr11_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PR11TafP0mBasisError(f"pr11_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _load_gap_evidence(
    path: str | Path | None, *, station_ids: list[str], cutoff: str
) -> tuple[dict[str, dict[str, Any]], str | None, dict[str, int]]:
    if path is None:
        return {}, None, {
            "sample_count_total": 0,
            "max_sample_count": 0,
            "qualified_scope_count_at_24": 0,
        }
    source = Path(path).expanduser().resolve(strict=True)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PR11TafP0mBasisError("pr11_taf_gap_evidence_json_invalid") from exc
    if not isinstance(raw, Mapping) or raw.get("schema") != LEGACY_GAP_EVIDENCE_SCHEMA:
        raise PR11TafP0mBasisError("pr11_taf_gap_evidence_schema_invalid")
    supplied = str(raw.get("content_sha256") or "").lower()
    if supplied != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise PR11TafP0mBasisError("pr11_taf_gap_evidence_hash_mismatch")
    if int(raw.get("canonical_station_count") or -1) != EXPECTED_STATION_COUNT:
        raise PR11TafP0mBasisError("pr11_taf_gap_evidence_station_count_invalid")
    entries = raw.get("entries")
    if not isinstance(entries, list) or len(entries) != EXPECTED_STATION_COUNT:
        raise PR11TafP0mBasisError("pr11_taf_gap_evidence_entries_invalid")
    by_station: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise PR11TafP0mBasisError("pr11_taf_gap_evidence_entry_invalid")
        station = str(entry.get("station_id") or "").upper()
        if station not in station_ids or station in by_station:
            raise PR11TafP0mBasisError("pr11_taf_gap_evidence_station_invalid")
        exact_join = entry.get("exact_join")
        sample_count = int(
            (exact_join.get("sample_count") if isinstance(exact_join, Mapping) else 0)
            or entry.get("sample_count")
            or 0
        )
        missing_to_24 = int(entry.get("missing_to_24") or max(0, 24 - sample_count))
        if sample_count < 0 or missing_to_24 != max(0, 24 - sample_count):
            raise PR11TafP0mBasisError("pr11_taf_gap_evidence_sample_count_invalid")
        by_station[station] = {
            "sample_count": sample_count,
            "missing_to_24": missing_to_24,
            "state": str(entry.get("state") or "BLOCKED"),
            "diagnostic_codes": sorted(
                {str(value) for value in entry.get("blockers") or []}
            ),
        }
    if sorted(by_station) != station_ids:
        raise PR11TafP0mBasisError("pr11_taf_gap_evidence_registry_mismatch")
    totals_raw = raw.get("totals")
    if not isinstance(totals_raw, Mapping):
        raise PR11TafP0mBasisError("pr11_taf_gap_evidence_totals_invalid")
    totals = {
        "sample_count_total": int(totals_raw.get("sample_count_total") or 0),
        "max_sample_count": int(totals_raw.get("max_sample_count") or 0),
        "qualified_scope_count_at_24": int(
            totals_raw.get("qualified_scope_count_at_24") or 0
        ),
    }
    if (
        totals["sample_count_total"] != sum(item["sample_count"] for item in by_station.values())
        or totals["max_sample_count"] != max(item["sample_count"] for item in by_station.values())
        or totals["qualified_scope_count_at_24"]
        != sum(item["sample_count"] >= 24 for item in by_station.values())
    ):
        raise PR11TafP0mBasisError("pr11_taf_gap_evidence_totals_mismatch")
    return by_station, supplied, totals


def _load_pr10_selection_terminal(
    path: str | Path | None, *, cutoff: str
) -> tuple[dict[str, list[str]], str | None, list[str]]:
    if path is None:
        return {}, None, []
    source = Path(path).expanduser().resolve(strict=True)
    raw = load_bounded_json(source, limit=MAX_JSON_INPUT_BYTES)
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema") != PR10_SELECTION_TERMINAL_BUNDLE_SCHEMA
    ):
        raise PR11TafP0mBasisError(
            "pr11_pr10_selection_terminal_schema_invalid"
        )
    supplied = str(raw.get("content_sha256") or "").lower()
    if supplied != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise PR11TafP0mBasisError(
            "pr11_pr10_selection_terminal_hash_mismatch"
        )
    if (
        _utc_z(raw.get("as_of_utc"), "pr10_selection_terminal_as_of") > cutoff
        or raw.get("state") != "BLOCKED"
        or raw.get("blocker") != "PRE_REGISTERED_MASK_RECEIPT_MISSING"
        or raw.get("selection_contract_count") != 0
        or raw.get("consumable") is not False
    ):
        raise PR11TafP0mBasisError(
            "pr11_pr10_selection_terminal_semantics_invalid"
        )
    terminals = raw.get("terminals")
    if not isinstance(terminals, list) or len(terminals) != raw.get(
        "terminal_count"
    ):
        raise PR11TafP0mBasisError(
            "pr11_pr10_selection_terminal_count_invalid"
        )
    by_station: dict[str, list[str]] = {}
    for item in terminals:
        try:
            terminal = validate_model_oos_selection_terminal_receipt(item)
        except ContractError as exc:
            raise PR11TafP0mBasisError(
                "pr11_pr10_selection_terminal_child_invalid"
            ) from exc
        if terminal["schema"] != PR10_SELECTION_TERMINAL_SCHEMA:
            raise PR11TafP0mBasisError(
                "pr11_pr10_selection_terminal_child_schema_invalid"
            )
        station = str(terminal["station_id"]).upper()
        by_station.setdefault(station, []).append(terminal["content_sha256"])
    return (
        {station: sorted(hashes) for station, hashes in sorted(by_station.items())},
        supplied,
        ["UPSTREAM_PR10_SELECTION_TERMINAL_BLOCKED"],
    )


def _load_pr10_p0_bundle(
    path: str | Path | None, *, cutoff: str
) -> tuple[list[dict[str, Any]], str | None]:
    if path is None:
        return [], None
    source = Path(path).expanduser().resolve(strict=True)
    raw = load_bounded_json(source, limit=64 * 1024 * 1024)
    if not isinstance(raw, Mapping) or raw.get("schema") != PR10_RESERVED_P0_BUNDLE_SCHEMA:
        raise PR11TafP0mBasisError("pr11_pr10_p0_bundle_schema_invalid")
    supplied = str(raw.get("content_sha256") or "").lower()
    if supplied != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise PR11TafP0mBasisError("pr11_pr10_p0_bundle_hash_mismatch")
    if (
        _utc_z(raw.get("as_of_utc"), "pr10_p0_bundle_as_of") > cutoff
        or _utc_z(raw.get("evaluated_at_utc"), "pr10_p0_bundle_evaluated_at") > cutoff
        or raw.get("state") != "MATERIALIZED_UNPUBLISHED"
        or raw.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY"
        or raw.get("consumable") is not False
        or raw.get("publishes_pr04_pointer") is not False
        or raw.get("starts_services") is not False
        or int(raw.get("orders_submitted") or 0) != 0
    ):
        raise PR11TafP0mBasisError("pr11_pr10_p0_bundle_semantics_invalid")

    selection_raw = raw.get("selection_contracts")
    distribution_raw = raw.get("distribution_contracts")
    activation_raw = raw.get("activation_contracts")
    if not all(isinstance(value, list) for value in (
        selection_raw, distribution_raw, activation_raw
    )):
        raise PR11TafP0mBasisError("pr11_pr10_p0_bundle_contracts_invalid")
    try:
        selections = [
            CanonicalModelOOSSelectionContract.from_mapping(value)
            for value in selection_raw
        ]
        distributions = [
            CanonicalP0CalibratedDistributionContract.from_mapping(value)
            for value in distribution_raw
        ]
        activations = [
            CanonicalP0ActivationRootContract.from_mapping(value)
            for value in activation_raw
        ]
    except ContractError as exc:
        raise PR11TafP0mBasisError("pr11_pr10_p0_bundle_child_invalid") from exc

    selection_facts = {
        fact.content_sha256: fact
        for contract in selections for fact in contract.selection_facts
    }
    distribution_facts = {
        fact.content_sha256: fact
        for contract in distributions for fact in contract.distribution_facts
    }
    root_facts = [fact for contract in activations for fact in contract.activation_root_facts]
    selection_fact_count = sum(len(contract.selection_facts) for contract in selections)
    distribution_fact_count = sum(
        len(contract.distribution_facts) for contract in distributions
    )
    if (
        len(selection_facts) != selection_fact_count
        or len(distribution_facts) != distribution_fact_count
        or selection_fact_count != int(raw.get("selection_count") or -1)
        or distribution_fact_count != int(raw.get("p0_distribution_count") or -1)
        or len(root_facts) != int(raw.get("p0_root_count") or -1)
        or not root_facts
    ):
        raise PR11TafP0mBasisError("pr11_pr10_p0_bundle_count_invalid")
    if any(
        _utc_z(contract.as_of_utc, "pr10_p0_child_as_of") > cutoff
        for contract in (*selections, *distributions, *activations)
    ):
        raise PR11TafP0mBasisError("pr11_pr10_p0_bundle_child_after_cutoff")
    roots: list[dict[str, Any]] = []
    seen_scopes: set[tuple[str, str, str, str, str]] = set()
    for root in root_facts:
        value = root.raw
        selection = selection_facts.get(value["selection_content_sha256"])
        distribution = distribution_facts.get(
            value["calibrated_distribution_content_sha256"]
        )
        if selection is None or distribution is None:
            raise PR11TafP0mBasisError("pr11_pr10_p0_bundle_parent_missing")
        if distribution.raw["selection_content_sha256"] != selection.content_sha256:
            raise PR11TafP0mBasisError("pr11_pr10_p0_bundle_parent_link_mismatch")
        inherited = (
            "station_id", "settlement_timezone", "lead_bucket", "lead_bucket_scope",
            "run_cycle", "run_cycle_scope", "season_bin", "source_class",
            "event_oos_protocol_version", "calibration_method_version",
            "authority_scope", "available_model_set_hash", "cohort_truth_vector_sha256",
        )
        if any(
            value[field] != selection.raw[field]
            or value[field] != distribution.raw[field]
            for field in inherited
        ):
            raise PR11TafP0mBasisError("pr11_pr10_p0_bundle_parent_scope_mismatch")
        if (
            value["source_class"] != "aggregator_republisher"
            or value["authority_scope"] != "PRODUCTION_NO_LIVE_ONLY"
            or value["state"] != "MATERIALIZED_UNPUBLISHED"
            or _utc_z(value["activated_at_utc"], "pr10_p0_root_activated_at") > cutoff
        ):
            raise PR11TafP0mBasisError("pr11_pr10_p0_root_semantics_invalid")
        scope = (
            value["station_id"], value["settlement_timezone"], value["season_bin"],
            value["lead_bucket"], str(value["run_cycle"] or ""),
        )
        if scope in seen_scopes:
            raise PR11TafP0mBasisError("pr11_pr10_p0_root_scope_duplicate")
        seen_scopes.add(scope)
        roots.append(dict(value))
    roots.sort(key=lambda value: (
        value["station_id"], value["settlement_timezone"], value["season_bin"],
        value["lead_bucket"], str(value["run_cycle"] or ""),
    ))
    return roots, supplied


def build_pr11_taf_p0m_artifacts(
    db_path: str | Path, *, as_of_utc: str,
    taf_gap_evidence_path: str | Path | None = None,
    pr10_selection_terminal_path: str | Path | None = None,
    pr10_p0_bundle_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build empty typed expected basis plus 45 explicit unresolved terminals."""

    cutoff = _utc_z(as_of_utc, "as_of_utc")
    source = Path(db_path).expanduser().resolve(strict=True)
    registry = coordinate_registry_manifest()
    station_ids = list(registry["station_ids"])
    if len(station_ids) != EXPECTED_STATION_COUNT or station_ids != sorted(
        set(station_ids)
    ):
        raise PR11TafP0mBasisError("pr11_registry_not_exact_45")
    gap_evidence, gap_evidence_sha, gap_totals = _load_gap_evidence(
        taf_gap_evidence_path, station_ids=station_ids, cutoff=cutoff
    )
    pr10_terminals_by_station, pr10_terminal_sha, pr10_blockers = (
        _load_pr10_selection_terminal(
            pr10_selection_terminal_path, cutoff=cutoff
        )
    )
    if not set(pr10_terminals_by_station).issubset(station_ids):
        raise PR11TafP0mBasisError(
            "pr11_pr10_selection_terminal_station_invalid"
        )
    p0_roots, pr10_p0_bundle_sha = _load_pr10_p0_bundle(
        pr10_p0_bundle_path, cutoff=cutoff
    )
    if not {root["station_id"] for root in p0_roots}.issubset(station_ids):
        raise PR11TafP0mBasisError("pr11_pr10_p0_root_station_invalid")
    roots_by_station: dict[str, list[dict[str, Any]]] = {}
    for root in p0_roots:
        roots_by_station.setdefault(root["station_id"], []).append(root)

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        inspection = inspect_canonical_ssot_schema(conn)
        schema_state = str(inspection.get("state") or "")
        if schema_state not in {"V12", "V13", "V14"} or inspection.get("ok") is not True:
            raise PR11TafP0mBasisError("pr11_canonical_v12_required")
        fact_type_counts = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT fact_type,COUNT(*) FROM canonical_ssot_fact "
                "WHERE ingested_at_utc<=? GROUP BY fact_type ORDER BY fact_type",
                (cutoff,),
            )
        }
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.rollback()
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise PR11TafP0mBasisError("pr11_canonical_read_failed") from exc
    finally:
        conn.close()

    unexpected = sorted(
        {"official_taf_revision", "taf_p0m_exact_join"} & set(fact_type_counts)
    )
    if unexpected:
        # V12 has no schema/ingress for these fact types.  Their mere presence
        # is corruption or an unversioned contract change, never readiness.
        raise PR11TafP0mBasisError("pr11_unrecognized_required_fact_type_present")
    source_inventory_sha = content_sha256(
        {"canonical_fact_type_counts_at_asof": fact_type_counts}
    )
    required_fact_type_set_sha = content_sha256(list(REQUIRED_CANONICAL_FACT_TYPES))

    terminal_rows: list[dict[str, Any]] = []
    for station_id in station_ids:
        gap = gap_evidence.get(
            station_id,
            {
                "sample_count": 0,
                "missing_to_24": 24,
                "state": "NOT_SUPPLIED",
                "diagnostic_codes": [],
            },
        )
        station_roots = roots_by_station.get(station_id, [])
        has_p0_root = bool(station_roots)
        row_identity = {
            "schema": TERMINAL_ROW_SCHEMA,
            "station_id": station_id,
            "as_of_utc": cutoff,
            "builder_id": BUILDER_ID,
            "canonical_registry_sha256": registry["registry_sha256"],
            "state": "BLOCKED",
            "blockers": ["OOS_SAMPLE_SHORT"] if has_p0_root else ["ACTIVATION_ROOT_MISSING"],
            "required_canonical_fact_types": list(REQUIRED_CANONICAL_FACT_TYPES),
            "required_canonical_fact_type_set_sha256": required_fact_type_set_sha,
            "required_artifact_key_fields": list(REQUIRED_ARTIFACT_KEY_FIELDS),
            "unproven_artifact_key_fields": (
                [] if has_p0_root else list(UNPROVEN_ARTIFACT_KEY_FIELDS)
            ),
            "source_inventory_content_sha256": source_inventory_sha,
            "typed_scope_emitted": has_p0_root,
            "legacy_taf_audit_used_as_canonical_input": False,
            "legacy_taf_gap_evidence_used": gap_evidence_sha is not None,
            "legacy_taf_gap_evidence_content_sha256": gap_evidence_sha,
            "legacy_exact_join_sample_count": gap["sample_count"],
            "legacy_exact_join_missing_to_24": gap["missing_to_24"],
            "legacy_exact_join_state": gap["state"],
            "legacy_exact_join_diagnostic_codes": gap["diagnostic_codes"],
            "legacy_probability_envelope_used": False,
            "canonical_schema_state": schema_state,
            "canonical_p0_activation_root_mapper_ready": has_p0_root,
            "canonical_p0_activation_root_content_sha256s": [
                root["content_sha256"] for root in station_roots
            ],
            "upstream_pr10_p0_bundle_content_sha256": pr10_p0_bundle_sha,
            "upstream_pr10_selection_terminal_bundle_content_sha256": (
                pr10_terminal_sha
            ),
            "upstream_pr10_station_terminal_content_sha256s": (
                pr10_terminals_by_station.get(station_id, [])
            ),
            "upstream_pr10_blockers": (
                pr10_blockers if station_id in pr10_terminals_by_station else []
            ),
        }
        terminal_rows.append(
            {**row_identity, "content_sha256": content_sha256(row_identity)}
        )
    terminal_basis_identity = {
        "schema": TERMINAL_BASIS_SCHEMA,
        "as_of_utc": cutoff,
        "builder_id": BUILDER_ID,
        "state": "BLOCKED",
        "release_state": "BLOCKED",
        "canonical_registry_sha256": registry["registry_sha256"],
        "station_ids": station_ids,
        "expected_station_count": EXPECTED_STATION_COUNT,
        "terminal_scope_count": len(terminal_rows),
        "terminal_scopes": terminal_rows,
        "source_inventory_content_sha256": source_inventory_sha,
        "legacy_taf_gap_evidence_content_sha256": gap_evidence_sha,
        "upstream_pr10_selection_terminal_bundle_content_sha256": (
            pr10_terminal_sha
        ),
        "upstream_pr10_bound_station_ids": sorted(pr10_terminals_by_station),
        "legacy_exact_join_sample_count_total": gap_totals["sample_count_total"],
        "legacy_exact_join_max_sample_count": gap_totals["max_sample_count"],
        "legacy_exact_join_qualified_scope_count_at_24": gap_totals[
            "qualified_scope_count_at_24"
        ],
        "production_no_live": True,
        "future_data_counts_as_oos": False,
        "writes_database": False,
        "writes_canonical_fact": False,
        "writes_probability_envelope": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    terminal_basis = {
        **terminal_basis_identity,
        "content_sha256": content_sha256(terminal_basis_identity),
    }

    expected_keys = [
        {
            "scope_kind": "ARTIFACT",
            "natural_key": {
                "station_id": root["station_id"],
                "settlement_timezone": root["settlement_timezone"],
                "season": root["season_bin"],
                "lead_bucket": root["lead_bucket"],
                "run_cycle": root["run_cycle"],
            },
        }
        for root in p0_roots
    ]
    expected_basis = build_scope_basis(
        scope_type="TAF_P0M",
        as_of_utc=cutoff,
        expected_keys=expected_keys,
        source_content_sha256s=(
            registry["registry_sha256"],
            source_inventory_sha,
            terminal_basis["content_sha256"],
            *(value for value in (pr10_p0_bundle_sha,) if value is not None),
        ),
        unresolved_input_receipts=(
            row["content_sha256"] for row in terminal_rows
        ),
    )
    bundle_identity = {
        "schema": SCHEMA,
        "as_of_utc": cutoff,
        "builder_id": BUILDER_ID,
        "state": "BLOCKED",
        "release_state": "BLOCKED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "blockers": sorted({
            *( ["ACTIVATION_ROOT_MISSING"] if len(roots_by_station) < EXPECTED_STATION_COUNT else [] ),
            *( ["OOS_SAMPLE_SHORT"] if p0_roots else [] ),
            *pr10_blockers,
        }),
        "canonical_p0_activation_root_count": len(p0_roots),
        "canonical_official_taf_revision_count": 0,
        "canonical_taf_p0m_exact_join_count": 0,
        "canonical_schema_state": schema_state,
        "canonical_p0_activation_root_mapper_ready": bool(p0_roots),
        "expected_station_count": EXPECTED_STATION_COUNT,
        "terminal_scope_count": len(terminal_rows),
        "typed_expected_key_count": len(expected_basis["expected_keys"]),
        "unresolved_input_receipt_count": len(
            expected_basis["unresolved_input_receipts"]
        ),
        "terminal_basis_content_sha256": terminal_basis["content_sha256"],
        "expected_basis_content_sha256": expected_basis["content_sha256"],
        "source_inventory_content_sha256": source_inventory_sha,
        "legacy_taf_gap_evidence_content_sha256": gap_evidence_sha,
        "upstream_pr10_selection_terminal_bundle_content_sha256": (
            pr10_terminal_sha
        ),
        "upstream_pr10_bound_station_ids": sorted(pr10_terminals_by_station),
        "upstream_pr10_p0_bundle_content_sha256": pr10_p0_bundle_sha,
        "upstream_pr10_p0_bound_station_ids": sorted(roots_by_station),
        "legacy_exact_join_sample_count_total": gap_totals["sample_count_total"],
        "legacy_exact_join_max_sample_count": gap_totals["max_sample_count"],
        "legacy_exact_join_qualified_scope_count_at_24": gap_totals[
            "qualified_scope_count_at_24"
        ],
        "database": str(source),
        "database_data_version": data_version,
        "database_schema_version": schema_version,
        "writes_database": False,
        "writes_canonical_fact": False,
        "writes_probability_envelope": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
        "future_data_counts_as_oos": False,
    }
    bundle = {**bundle_identity, "content_sha256": content_sha256(bundle_identity)}
    return expected_basis, terminal_basis, bundle


__all__ = [
    "BUILDER_ID",
    "EXPECTED_STATION_COUNT",
    "PR11TafP0mBasisError",
    "REQUIRED_CANONICAL_FACT_TYPES",
    "SCHEMA",
    "build_pr11_taf_p0m_artifacts",
]
