"""PR05 deterministic/regional benchmark coverage adapters.

The adapters consume only the active strict canonical model table and one
content-addressed regional candidate audit.  They produce coverage rows; they
never create calibration activation, cross-system selection, P0, or orders.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .contracts import ContractError, content_sha256
from .model_system_coverage import LEADS, canonical_station_ids, make_coverage_row


GLOBAL_DETERMINISTIC_PRODUCTS = ("CMA", "ECMWF", "GEM", "GFS", "ICON")
REGIONAL_BENCHMARK_PRODUCTS = (
    "AROME-FR",
    "HARMONIE-EU",
    "HARMONIE-NL",
    "HRRR",
    "ICON-D2",
    "ICON-EU",
    "JMA-MSM",
    "NAM",
    "NBM",
    "UKMO-2KM",
)


def _validated_content_packet(path: Path | str, schema: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ContractError("benchmark_coverage_source_symlink_forbidden")
    source = source.resolve(strict=True)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ContractError("benchmark_coverage_source_schema_invalid")
    claimed = value.get("content_sha256")
    if claimed != content_sha256({key: item for key, item in value.items() if key != "content_sha256"}):
        raise ContractError("benchmark_coverage_source_content_sha_mismatch")
    return value


def load_active_global_deterministic_facts(db: Path | str) -> tuple[dict[str, Any], ...]:
    source = Path(db).expanduser()
    if source.is_symlink():
        raise ContractError("benchmark_coverage_database_symlink_forbidden")
    source = source.resolve(strict=True)
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        schema = inspect_canonical_ssot_schema(conn)
        # V6 is the only active schema.  Accepting V5 would bypass the
        # receipt-manifest lineage contract shared by every PR05--PR09 matrix.
        if schema.get("state") != "V7" or schema.get("ok") is not True:
            raise ContractError("benchmark_coverage_canonical_v6_required")
        rows: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT fact_json,content_sha256 FROM canonical_ssot_fact "
            "WHERE fact_type='model' ORDER BY scope_key"
        ):
            fact = json.loads(str(row[0]))
            if fact.get("content_sha256") != str(row[1]):
                raise ContractError("benchmark_coverage_model_fact_hash_mismatch")
            if fact.get("model_family") == "global_deterministic":
                rows.append(fact)
        return tuple(rows)
    finally:
        conn.close()


def _station_statuses(receipt: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if receipt.get("schema") != "bayes_basket_target_d1_canonical_build_v2":
        raise ContractError("benchmark_coverage_d1_receipt_schema_invalid")
    if receipt.get("content_sha256") != content_sha256(
        {key: value for key, value in receipt.items() if key != "content_sha256"}
    ):
        raise ContractError("benchmark_coverage_d1_receipt_hash_mismatch")
    values = receipt.get("station_statuses")
    if not isinstance(values, list):
        raise ContractError("benchmark_coverage_d1_statuses_missing")
    result = {str(item.get("station_id") or "").upper(): item for item in values if isinstance(item, Mapping)}
    if set(result) != set(canonical_station_ids()) or len(values) != 45:
        raise ContractError("benchmark_coverage_d1_statuses_not_exact_45")
    return result


def build_global_deterministic_coverage_rows(
    facts: Iterable[Mapping[str, Any]],
    *,
    d1_build_receipt: Mapping[str, Any],
    as_of_utc: str,
) -> tuple[dict[str, Any], ...]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    product_identity: dict[str, tuple[str, str]] = {}
    for fact in facts:
        product = str(fact.get("model_id") or "")
        station = str(fact.get("station_id") or "").upper()
        lead = str(fact.get("lead_bucket") or "").lower()
        if product not in GLOBAL_DETERMINISTIC_PRODUCTS or station not in set(canonical_station_ids()) or lead not in LEADS:
            raise ContractError("benchmark_coverage_global_fact_scope_invalid")
        if fact.get("model_family") != "global_deterministic":
            raise ContractError("benchmark_coverage_global_family_invalid")
        if fact.get("source_class") != "aggregator_republisher":
            raise ContractError("benchmark_coverage_global_source_class_invalid")
        if fact.get("formal_live_authority") is not False or fact.get("native_arrival_indexed") is not False:
            raise ContractError("benchmark_coverage_republisher_authority_invalid")
        identity = (str(fact.get("provider_id") or ""), str(fact.get("source_class") or ""))
        if not identity[0]:
            raise ContractError("benchmark_coverage_global_provider_missing")
        previous = product_identity.setdefault(product, identity)
        if previous != identity:
            raise ContractError("benchmark_coverage_global_product_identity_mismatch")
        grouped[(product, station, lead)].append(fact)
    if set(product_identity) != set(GLOBAL_DETERMINISTIC_PRODUCTS):
        raise ContractError("benchmark_coverage_global_product_set_mismatch")
    statuses = _station_statuses(d1_build_receipt)
    receipt_sha = str(d1_build_receipt["content_sha256"])
    rows: list[dict[str, Any]] = []
    for product in GLOBAL_DETERMINISTIC_PRODUCTS:
        provider_id, source_class = product_identity[product]
        for station in canonical_station_ids():
            status = statuses[station]
            for lead in LEADS:
                scoped = grouped.get((product, station, lead), [])
                cycles = {str(item.get("run_cycle") or "") for item in scoped}
                evidence_sha = content_sha256(
                    {
                        "product_id": product,
                        "station_id": station,
                        "lead_bucket": lead,
                        "fact_content_sha256s": sorted(str(item.get("content_sha256") or "") for item in scoped),
                        "d1_build_receipt_sha256": receipt_sha if lead == "d1" else None,
                    }
                )
                if lead == "d0":
                    if scoped:
                        raise ContractError("benchmark_coverage_unexpected_d0_fact")
                    run_cycle = None
                    state = "BLOCKED"
                    blocker = "RUN_CYCLE_MISMATCH"
                    train_count = holdout_count = 0
                    strict_asof = closed_truth = False
                else:
                    expected_cycle = "PREVIOUS_DAY1" if lead == "d1" else "PREVIOUS_DAY2"
                    if cycles != {expected_cycle} or len(scoped) != 28:
                        raise ContractError("benchmark_coverage_global_exact_28_scope_missing")
                    run_cycle = expected_cycle
                    state = "DATA_READY"
                    blocker = "OOS_SAMPLE_SHORT"
                    if lead == "d1":
                        train_count = int(status.get("train_count") or 0)
                        holdout_count = int(status.get("holdout_count") or 0)
                        strict_asof = bool(train_count or holdout_count)
                        closed_truth = bool(train_count or holdout_count)
                    else:
                        train_count = holdout_count = 0
                        strict_asof = all(item.get("strict_asof") is True for item in scoped)
                        closed_truth = False
                rows.append(
                    make_coverage_row(
                        station_id=station,
                        product_id=product,
                        provider_id=provider_id,
                        model_family="global_deterministic",
                        independence_family_id=f"DETERMINISTIC_{product}",
                        source_class=source_class,
                        lead_bucket=lead,
                        run_cycle=run_cycle,
                        as_of_utc=as_of_utc,
                        state=state,
                        blockers=[blocker],
                        evidence_role="historical_strict_oos",
                        strict_asof=strict_asof,
                        closed_awc_truth=closed_truth,
                        train_count=train_count,
                        holdout_count=holdout_count,
                        source_evidence_sha256=evidence_sha,
                        raw_receipt_sha256=None,
                        oos_evidence_sha256=receipt_sha if lead == "d1" else None,
                        single_system_artifact_sha256=None,
                        native_arrival_indexed=False,
                        formal_live_authority=False,
                        cross_selection_count=0,
                        cross_activation_count=0,
                        p0_count=0,
                    )
                )
    return tuple(rows)


def build_regional_benchmark_coverage_rows(
    audit: Mapping[str, Any], *, as_of_utc: str
) -> tuple[dict[str, Any], ...]:
    if audit.get("schema") != "regional_model_candidate_closure_audit_v1":
        raise ContractError("benchmark_coverage_regional_schema_invalid")
    audit_sha = str(audit.get("content_sha256") or "")
    if audit_sha != content_sha256({key: value for key, value in audit.items() if key != "content_sha256"}):
        raise ContractError("benchmark_coverage_regional_hash_mismatch")
    models = {
        str(item.get("model_key") or ""): item
        for item in audit.get("models") or []
        if isinstance(item, Mapping) and item.get("model_key") in REGIONAL_BENCHMARK_PRODUCTS
    }
    if set(models) != set(REGIONAL_BENCHMARK_PRODUCTS):
        raise ContractError("benchmark_coverage_regional_product_set_mismatch")
    rows: list[dict[str, Any]] = []
    for product in REGIONAL_BENCHMARK_PRODUCTS:
        model = models[product]
        if model.get("geographic_scope") != "regional" or model.get("source_class") != "aggregator_republisher":
            raise ContractError("benchmark_coverage_regional_lineage_invalid")
        station_scope = {str(value).upper() for value in model.get("station_ids") or []}
        if not station_scope or not station_scope.issubset(set(canonical_station_ids())):
            raise ContractError("benchmark_coverage_regional_station_scope_invalid")
        for station in canonical_station_ids():
            for lead in LEADS:
                configured = station in station_scope
                rows.append(
                    make_coverage_row(
                        station_id=station,
                        product_id=product,
                        provider_id="OPEN_METEO",
                        model_family="regional",
                        independence_family_id=f"REGIONAL_{product}",
                        source_class="aggregator_republisher",
                        lead_bucket=lead,
                        run_cycle=None,
                        as_of_utc=as_of_utc,
                        state="CANDIDATE_ONLY" if configured else "BLOCKED",
                        blockers=["RUN_CYCLE_MISMATCH" if configured else "DATA_MISSING"],
                        evidence_role="observational_candidate_not_oos",
                        strict_asof=False,
                        closed_awc_truth=False,
                        train_count=0,
                        holdout_count=0,
                        source_evidence_sha256=audit_sha,
                        raw_receipt_sha256=None,
                        oos_evidence_sha256=None,
                        single_system_artifact_sha256=None,
                        native_arrival_indexed=False,
                        formal_live_authority=False,
                        cross_selection_count=0,
                        cross_activation_count=0,
                        p0_count=0,
                    )
                )
    return tuple(rows)


def load_pr05_inputs(
    *, db: Path | str, d1_build_receipt: Path | str, regional_audit: Path | str
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any], dict[str, Any]]:
    return (
        load_active_global_deterministic_facts(db),
        _validated_content_packet(d1_build_receipt, "bayes_basket_target_d1_canonical_build_v2"),
        _validated_content_packet(regional_audit, "regional_model_candidate_closure_audit_v1"),
    )


__all__ = [
    "GLOBAL_DETERMINISTIC_PRODUCTS",
    "REGIONAL_BENCHMARK_PRODUCTS",
    "build_global_deterministic_coverage_rows",
    "build_regional_benchmark_coverage_rows",
    "load_active_global_deterministic_facts",
    "load_pr05_inputs",
]
