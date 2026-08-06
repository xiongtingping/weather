"""PR10 canonical P0 terminal basis for native-first no-fallback receipts.

This builder closes a precise gap without manufacturing a P0 fact.  A native
probe can prove that the current official product is available while still
lacking a decoded station forecast and an activation with matching native
lineage.  That is neither a forward applicability fact nor a P0 snapshot.

The only safe output for that situation is an external, read-only terminal
basis.  It binds the immutable probe/terminal receipt to the exact canonical
republisher activation, selection, and distribution artifact, but deliberately
does *not* invent the typed ``P0_SNAPSHOT`` natural key fields which are absent
from the evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .canonical_contract import (
    CanonicalRepublisherCrossSelectionFact,
    CanonicalRepublisherNoLiveActivationFact,
)
from .contracts import ContractError, content_sha256
from .republisher_no_live_p0_contract import (
    CanonicalProbabilityDistributionArtifactFact,
)
from .resource_bounds import MAX_JSON_INPUT_BYTES, load_bounded_json
from .station_registry import coordinate_registry_manifest


SCHEMA = "bayes_basket_target_pr10_p0_terminal_basis_v1"
BUILDER_ID = "pr10.canonical_p0_native_terminal_basis.v1"
TERMINAL_RECEIPT_SCHEMA = "republisher_native_first_forward_materialization_receipt_v1"
PROBE_RESULT_SCHEMA = "republisher_native_forward_probe_result_v1"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
EXPECTED_FAILURE_CODE = "native_current_product_activation_lineage_unbound"
EXPECTED_BLOCKER = "LINEAGE_MISMATCH"
EXPECTED_NATIVE_STATUS = "NATIVE_COMPLETE_NO_FALLBACK"
MISSING_ACTIVATION_BLOCKER = "ACTIVATION_ROOT_MISSING"
MISSING_ACTIVATION_FAILURE_CODE = "canonical_gen2_activation_missing"


class PR10P0BasisError(ContractError):
    """A PR10 receipt or canonical parent fails the one-to-one contract."""


def _utc_z(value: object, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PR10P0BasisError(f"pr10_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PR10P0BasisError(f"pr10_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha(value: object, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise PR10P0BasisError(f"pr10_{label}_sha_invalid")
    return result


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PR10P0BasisError(f"pr10_{label}_mapping_required")
    return dict(value)


def _load_json(path: Path | str, label: str) -> tuple[Path, dict[str, Any]]:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        value = load_bounded_json(source, limit=MAX_JSON_INPUT_BYTES)
    except (OSError, ValueError) as exc:
        raise PR10P0BasisError(f"pr10_{label}_load_failed") from exc
    return source, _mapping(value, label)


def _verify_content_hash(raw: Mapping[str, Any], label: str) -> str:
    supplied = _sha(raw.get("content_sha256"), f"{label}_content")
    if supplied != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise PR10P0BasisError(f"pr10_{label}_content_hash_mismatch")
    return supplied


def _load_unique_fact(
    conn: sqlite3.Connection,
    *,
    fact_type: str,
    content_sha256_value: str,
    cutoff: str,
    label: str,
) -> tuple[dict[str, Any], str]:
    rows = conn.execute(
        "SELECT fact_json,content_sha256,ingested_at_utc FROM canonical_ssot_fact "
        "WHERE fact_type=? AND content_sha256=? AND ingested_at_utc<=?",
        (fact_type, content_sha256_value, cutoff),
    ).fetchall()
    if len(rows) != 1:
        raise PR10P0BasisError(f"pr10_{label}_canonical_fact_not_unique")
    fact_json, stored_sha, ingested_at = rows[0]
    try:
        parsed = load_bounded_json_bytes(str(fact_json).encode("utf-8"), label)
    except (UnicodeEncodeError, ValueError) as exc:
        raise PR10P0BasisError(f"pr10_{label}_canonical_json_invalid") from exc
    if _sha(stored_sha, f"{label}_stored") != content_sha256(
        {key: value for key, value in parsed.items() if key != "content_sha256"}
    ):
        raise PR10P0BasisError(f"pr10_{label}_stored_hash_mismatch")
    if _utc_z(ingested_at, f"{label}_ingested_at") > cutoff:
        raise PR10P0BasisError(f"pr10_{label}_after_asof")
    return parsed, _sha(stored_sha, f"{label}_stored")


def load_bounded_json_bytes(value: bytes, label: str) -> dict[str, Any]:
    """Parse a small row already bounded by SQLite; reject a second format."""

    if len(value) > MAX_JSON_INPUT_BYTES:
        raise PR10P0BasisError(f"pr10_{label}_canonical_json_too_large")
    import json

    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PR10P0BasisError(f"pr10_{label}_canonical_json_invalid") from exc
    return _mapping(parsed, label)


def _canonical_parent_chain(
    conn: sqlite3.Connection,
    *,
    activation_sha: str,
    cutoff: str,
) -> tuple[
    CanonicalRepublisherNoLiveActivationFact,
    CanonicalRepublisherCrossSelectionFact,
    CanonicalProbabilityDistributionArtifactFact,
    list[dict[str, str]],
]:
    activation_raw, activation_stored = _load_unique_fact(
        conn,
        fact_type="republisher_no_live_activation",
        content_sha256_value=activation_sha,
        cutoff=cutoff,
        label="activation",
    )
    try:
        activation = CanonicalRepublisherNoLiveActivationFact.from_mapping(activation_raw)
    except ContractError as exc:
        raise PR10P0BasisError("pr10_activation_contract_invalid") from exc
    if activation.content_sha256 != activation_stored:
        raise PR10P0BasisError("pr10_activation_hash_mismatch")
    if (
        activation.activation_generation < 2
        or activation.temperature_distribution_artifact_content_sha256 is None
        or activation.authority_scope != AUTHORITY_SCOPE
        or activation.p1_eligible
        or activation.formal_live_authority
        or not activation.production_no_live_only
    ):
        raise PR10P0BasisError("pr10_activation_authority_invalid")
    if _utc_z(activation.activated_at_utc, "activation_activated_at") > cutoff:
        raise PR10P0BasisError("pr10_activation_after_asof")

    selection_raw, selection_stored = _load_unique_fact(
        conn,
        fact_type="republisher_cross_selection",
        content_sha256_value=activation.selection_content_sha256,
        cutoff=cutoff,
        label="selection",
    )
    try:
        selection = CanonicalRepublisherCrossSelectionFact.from_mapping(selection_raw)
    except ContractError as exc:
        raise PR10P0BasisError("pr10_selection_contract_invalid") from exc
    if selection.content_sha256 != selection_stored:
        raise PR10P0BasisError("pr10_selection_hash_mismatch")
    if (
        selection.station_id != activation.station_id
        or selection.settlement_timezone != activation.settlement_timezone
        or selection.lead_bucket != activation.lead_bucket
        or selection.run_cycle != activation.run_cycle
        or selection.run_cycle_scope != activation.run_cycle_scope
        or selection.content_sha256 != activation.selection_content_sha256
    ):
        raise PR10P0BasisError("pr10_activation_selection_scope_mismatch")

    distribution_raw, distribution_stored = _load_unique_fact(
        conn,
        fact_type="probability_distribution_artifact",
        content_sha256_value=activation.temperature_distribution_artifact_content_sha256,
        cutoff=cutoff,
        label="distribution",
    )
    try:
        distribution = CanonicalProbabilityDistributionArtifactFact.from_mapping(
            distribution_raw
        )
    except ContractError as exc:
        raise PR10P0BasisError("pr10_distribution_contract_invalid") from exc
    if distribution.content_sha256 != distribution_stored:
        raise PR10P0BasisError("pr10_distribution_hash_mismatch")
    if (
        distribution.raw["station_id"] != activation.station_id
        or distribution.raw["selection_content_sha256"] != selection.content_sha256
        or distribution.raw["lead_bucket"] != activation.lead_bucket
        or distribution.raw["run_cycle"] != activation.run_cycle
        or distribution.raw["run_cycle_scope"] != activation.run_cycle_scope
        or distribution.raw["authority_scope"] != AUTHORITY_SCOPE
        or distribution.raw["p1_eligible"] is not False
        or distribution.raw["formal_live_authority"] is not False
        or distribution.raw["production_no_live_only"] is not True
    ):
        raise PR10P0BasisError("pr10_activation_distribution_scope_mismatch")

    historical_vector = [
        {
            "upstream_model_id": item.upstream_model_id,
            "republisher_product_id": item.republisher_product_id,
            "independence_family_id": item.independence_family_id,
            "artifact_content_sha256": item.artifact_content_sha256,
        }
        for item in selection.component_artifact_refs
    ]
    return activation, selection, distribution, historical_vector


def _native_vector(contract: Mapping[str, Any]) -> list[dict[str, str]]:
    product_results = contract.get("product_results")
    if not isinstance(product_results, list) or len(product_results) != 2:
        raise PR10P0BasisError("pr10_probe_product_set_invalid")
    vector: list[dict[str, str]] = []
    for expected_model, raw in zip(("AIFS", "ICON"), product_results):
        item = _mapping(raw, "probe_product")
        if item.get("upstream_model_id") != expected_model:
            raise PR10P0BasisError("pr10_probe_product_order_invalid")
        value = {
            "upstream_model_id": expected_model,
            "native_provider_id": str(item.get("native_provider_id") or ""),
            "native_product_id": str(item.get("native_product_id") or ""),
            "terminal_status": str(item.get("terminal_status") or ""),
        }
        if not all(value.values()) or value["terminal_status"] != EXPECTED_NATIVE_STATUS:
            raise PR10P0BasisError("pr10_probe_not_native_complete")
        vector.append(value)
    return vector


def _terminal_pair(
    conn: sqlite3.Connection,
    *,
    receipt_path: Path | str,
    probe_path: Path | str,
    cutoff: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    receipt_source, receipt = _load_json(receipt_path, "terminal_receipt")
    probe_source, probe = _load_json(probe_path, "probe_result")
    receipt_sha = _verify_content_hash(receipt, "terminal_receipt")
    probe_sha = _verify_content_hash(probe, "probe_result")
    if receipt.get("schema") != TERMINAL_RECEIPT_SCHEMA:
        raise PR10P0BasisError("pr10_terminal_receipt_schema_invalid")
    if probe.get("schema") != PROBE_RESULT_SCHEMA:
        raise PR10P0BasisError("pr10_probe_result_schema_invalid")
    if receipt.get("state") != "BLOCKED" or receipt.get("release_state") != "BLOCKED":
        raise PR10P0BasisError("pr10_terminal_receipt_state_invalid")
    if (
        receipt.get("blocker") != EXPECTED_BLOCKER
        or receipt.get("failure_code") != EXPECTED_FAILURE_CODE
        or receipt.get("native_first_disposition") != EXPECTED_NATIVE_STATUS
        or receipt.get("authority_scope") != AUTHORITY_SCOPE
        or receipt.get("canonical_rows_inserted") != 0
        or receipt.get("forward_applicability_written") is not False
        or receipt.get("p0_written") is not False
        or receipt.get("calls_order_gateway") is not False
        or receipt.get("orders_submitted") != 0
    ):
        raise PR10P0BasisError("pr10_terminal_receipt_semantics_invalid")
    if receipt.get("probe_result_content_sha256") != probe_sha:
        raise PR10P0BasisError("pr10_terminal_receipt_probe_hash_mismatch")
    if (
        probe.get("fallback_terminal_for_all_products") is not False
        or probe.get("native_complete_for_any_product") is not True
        or probe.get("indeterminate_for_any_product") is not False
        or probe.get("writes_target_database") is not False
        or probe.get("calls_order_gateway") is not False
        or probe.get("orders_submitted") != 0
    ):
        raise PR10P0BasisError("pr10_probe_result_semantics_invalid")
    checked_at = _utc_z(probe.get("checked_at_utc"), "probe_checked_at")
    if checked_at > cutoff:
        raise PR10P0BasisError("pr10_probe_after_asof")
    station_id = str(receipt.get("station_id") or "").strip().upper()
    event_date = str(receipt.get("event_date") or "")
    settlement_timezone = str(receipt.get("settlement_timezone") or "")
    if not station_id or not event_date or not settlement_timezone:
        raise PR10P0BasisError("pr10_terminal_receipt_scope_missing")
    if (
        probe.get("station_id") != station_id
        or probe.get("event_date") != event_date
        or probe.get("settlement_timezone") != settlement_timezone
    ):
        raise PR10P0BasisError("pr10_terminal_receipt_probe_scope_mismatch")
    activation_sha = _sha(receipt.get("activation_content_sha256"), "activation")
    selection_sha = _sha(receipt.get("selection_content_sha256"), "selection")
    activation, selection, distribution, historical_vector = _canonical_parent_chain(
        conn, activation_sha=activation_sha, cutoff=cutoff
    )
    if (
        activation.station_id != station_id
        or activation.settlement_timezone != settlement_timezone
        or selection.content_sha256 != selection_sha
        or receipt.get("historical_activation_product_vector") != historical_vector
    ):
        raise PR10P0BasisError("pr10_terminal_receipt_activation_lineage_mismatch")
    vector = _native_vector(probe)
    if receipt.get("native_current_product_vector") != vector:
        raise PR10P0BasisError("pr10_terminal_receipt_native_vector_mismatch")
    terminal = {
        "terminal_kind": "P0_SNAPSHOT_ATTEMPT",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "activation_content_sha256": activation.content_sha256,
        "selection_content_sha256": selection.content_sha256,
        "probability_distribution_artifact_content_sha256": distribution.content_sha256,
        "terminal_receipt_content_sha256": receipt_sha,
        "probe_result_content_sha256": probe_sha,
        "state": "BLOCKED",
        "blockers": [EXPECTED_BLOCKER],
        "failure_code": EXPECTED_FAILURE_CODE,
        "evidence_role": "observational_candidate_not_oos",
        "strict_oos_sample_eligible": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "production_no_live_only": True,
        "unproven_p0_snapshot_natural_key_fields": [
            "decision_at_utc",
            "forward_applicability_content_sha256",
            "probability_root_sha256",
        ],
    }
    inventory = {
        "station_id": station_id,
        "terminal_receipt_path": str(receipt_source),
        "terminal_receipt_content_sha256": receipt_sha,
        "probe_result_path": str(probe_source),
        "probe_result_content_sha256": probe_sha,
        "activation_content_sha256": activation.content_sha256,
        "selection_content_sha256": selection.content_sha256,
        "probability_distribution_artifact_content_sha256": distribution.content_sha256,
    }
    return terminal, inventory


def build_pr10_p0_terminal_basis(
    db_path: str | Path,
    *,
    as_of_utc: str,
    terminal_receipt_paths: Sequence[Path | str],
    probe_result_paths: Sequence[Path | str],
) -> dict[str, Any]:
    """Compile exact native-current terminal receipts without an SSOT write.

    Every current Gen2 activation must have exactly one matching pair.  The
    independent 45-station registry remains the expected scope: stations with
    no Gen2 activation receive an explicit station-level unresolved terminal
    instead of disappearing from the basis.
    """

    cutoff = _utc_z(as_of_utc, "as_of_utc")
    source = Path(db_path).expanduser().resolve(strict=True)
    if len(terminal_receipt_paths) != len(probe_result_paths) or not terminal_receipt_paths:
        raise PR10P0BasisError("pr10_terminal_pair_count_invalid")
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT content_sha256 FROM canonical_ssot_fact "
            "WHERE fact_type='republisher_no_live_activation' "
            "AND json_extract(fact_json,'$.activation_generation')>=2 "
            "AND json_extract(fact_json,'$.activated_at_utc')<=? "
            "AND ingested_at_utc<=? ORDER BY content_sha256",
            (cutoff, cutoff),
        ).fetchall()
        expected_activation_shas = [_sha(row[0], "activation_inventory") for row in rows]
        terminals: list[dict[str, Any]] = []
        inventory: list[dict[str, str]] = []
        for receipt_path, probe_path in zip(terminal_receipt_paths, probe_result_paths):
            terminal, source_item = _terminal_pair(
                conn,
                receipt_path=receipt_path,
                probe_path=probe_path,
                cutoff=cutoff,
            )
            terminals.append(terminal)
            inventory.append(source_item)
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.rollback()
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise PR10P0BasisError("pr10_canonical_read_failed") from exc
    finally:
        conn.close()

    terminals.sort(key=lambda item: (item["station_id"], item["event_date"]))
    inventory.sort(key=lambda item: item["station_id"])
    activation_shas = [item["activation_content_sha256"] for item in terminals]
    if len(set(activation_shas)) != len(activation_shas):
        raise PR10P0BasisError("pr10_terminal_activation_duplicate")
    if activation_shas != expected_activation_shas:
        raise PR10P0BasisError("pr10_terminal_activation_coverage_mismatch")
    canonical_station_ids = sorted(coordinate_registry_manifest()["station_ids"])
    activated_station_ids = sorted({item["station_id"] for item in terminals})
    if any(station_id not in canonical_station_ids for station_id in activated_station_ids):
        raise PR10P0BasisError("pr10_terminal_station_outside_registry")
    missing_activation_station_ids = sorted(
        set(canonical_station_ids) - set(activated_station_ids)
    )
    terminals.extend(
        {
            "terminal_kind": "P0_STATION_SCOPE",
            "station_id": station_id,
            "event_date": None,
            "settlement_timezone": None,
            "activation_content_sha256": None,
            "selection_content_sha256": None,
            "probability_distribution_artifact_content_sha256": None,
            "terminal_receipt_content_sha256": None,
            "probe_result_content_sha256": None,
            "state": "BLOCKED",
            "blockers": [MISSING_ACTIVATION_BLOCKER],
            "failure_code": MISSING_ACTIVATION_FAILURE_CODE,
            "evidence_role": "historical_activation_gap",
            "strict_oos_sample_eligible": False,
            "p1_eligible": False,
            "formal_live_authority": False,
            "production_no_live_only": True,
            "unproven_p0_snapshot_natural_key_fields": [
                "event_date",
                "settlement_timezone",
                "decision_at_utc",
                "activation_content_sha256",
                "forward_applicability_content_sha256",
                "probability_root_sha256",
            ],
        }
        for station_id in missing_activation_station_ids
    )
    terminals.sort(
        key=lambda item: (item["station_id"], str(item.get("event_date") or ""))
    )
    source_inventory_sha = content_sha256(
        {
            "canonical_gen2_activation_content_sha256s": expected_activation_shas,
            "terminal_pairs": inventory,
        }
    )
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": cutoff,
        "state": "BLOCKED",
        "release_state": "BLOCKED",
        "authority_scope": AUTHORITY_SCOPE,
        "blockers": sorted(
            {
                EXPECTED_BLOCKER,
                *(
                    [MISSING_ACTIVATION_BLOCKER]
                    if missing_activation_station_ids
                    else []
                ),
            }
        ),
        "canonical_station_count": len(canonical_station_ids),
        "canonical_gen2_activation_count": len(expected_activation_shas),
        "activation_bound_station_count": len(activated_station_ids),
        "activation_missing_station_count": len(missing_activation_station_ids),
        "activation_missing_station_ids": missing_activation_station_ids,
        "terminal_scope_count": len(terminals),
        "terminal_scopes": terminals,
        "source_inventory_content_sha256": source_inventory_sha,
        "database": str(source),
        "database_data_version": data_version,
        "database_schema_version": schema_version,
        "writes_database": False,
        "writes_canonical_fact": False,
        "writes_probability_envelope": False,
        "writes_pointer": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
        "future_data_counts_as_oos": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "AUTHORITY_SCOPE",
    "BUILDER_ID",
    "EXPECTED_BLOCKER",
    "EXPECTED_FAILURE_CODE",
    "MISSING_ACTIVATION_BLOCKER",
    "MISSING_ACTIVATION_FAILURE_CODE",
    "PR10P0BasisError",
    "SCHEMA",
    "build_pr10_p0_terminal_basis",
]
