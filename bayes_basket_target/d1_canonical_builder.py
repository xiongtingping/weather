"""Canonical d1 global-deterministic residual builder.

The builder consumes only the active canonical fact table.  It never reads
the retired truth/residual projections and never emits an activation unless
the exact fixed 20-train + 8-holdout cohort exists for all five models.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .canonical_contract import (
    CONTRACT_VERSION,
    SCHEMA as CANONICAL_CONTRACT_SCHEMA,
    CanonicalContract,
    CanonicalModelFact,
    CanonicalTruthFact,
)
from .contracts import ContractError, MODEL_IDS, content_sha256


BUILDER_ID = "d1.global_deterministic.canonical_historical.v2"
BUILD_SCHEMA = "bayes_basket_target_d1_canonical_build_v2"
LEAD_BUCKET = "d1"
RUN_CYCLE = "PREVIOUS_DAY1"
MODEL_FAMILY = "global_deterministic"
EXPECTED_MODELS = tuple(sorted(MODEL_IDS))
TRAIN_DATES = tuple(f"2026-07-{day:02d}" for day in range(1, 21))
HOLDOUT_DATES = tuple(f"2026-07-{day:02d}" for day in range(22, 30))
ALL_DATES = TRAIN_DATES + HOLDOUT_DATES


def _utc(value: str, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    return parsed.astimezone(timezone.utc)


def _read_active_facts(
    db: Path | str,
) -> tuple[list[CanonicalModelFact], list[CanonicalTruthFact], str]:
    path = Path(db).expanduser().resolve(strict=True)
    if path.is_symlink():
        raise ContractError("d1_canonical_database_symlink_forbidden")
    models: list[CanonicalModelFact] = []
    truths: list[CanonicalTruthFact] = []
    root_rows: list[dict[str, str]] = []
    # ``immutable=1`` is forbidden here: it makes SQLite ignore a live WAL
    # and can therefore build from a stale pre-commit snapshot.  A read-only
    # transaction sees the committed WAL while still preventing writes and
    # keeps the source-root scan on one consistent snapshot.
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_ssot_fact'"
        ).fetchone()
        if table is None:
            raise ContractError("d1_canonical_active_table_missing")
        for fact_type, scope_key, content_hash, fact_json in conn.execute(
            "SELECT fact_type,scope_key,content_sha256,fact_json FROM canonical_ssot_fact "
            "WHERE fact_type IN ('model','truth') ORDER BY fact_type,scope_key,fact_id"
        ):
            try:
                raw = json.loads(str(fact_json))
            except json.JSONDecodeError as exc:
                raise ContractError("d1_canonical_fact_json_invalid") from exc
            if fact_type == "model":
                fact = CanonicalModelFact.from_mapping(raw)
                models.append(fact)
            else:
                fact = CanonicalTruthFact.from_mapping(raw)
                truths.append(fact)
            if str(scope_key) != fact.scope_key or str(content_hash) != (
                fact.content_sha256
            ):
                raise ContractError("d1_canonical_fact_storage_mismatch")
            root_rows.append(
                {
                    "fact_type": str(fact_type),
                    "scope_key": str(scope_key),
                    "content_sha256": str(content_hash),
                }
            )
    return models, truths, content_sha256(root_rows)


def build_d1_canonical_residual_contract(
    *,
    db: Path | str,
    stations: Sequence[str],
    as_of_utc: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    as_of = _utc(as_of_utc, "d1_canonical_as_of_invalid")
    normalized_as_of = as_of.isoformat(timespec="seconds").replace("+00:00", "Z")
    station_ids = tuple(sorted({str(item or "").strip().upper() for item in stations if str(item or "").strip()}))
    if not station_ids:
        raise ContractError("d1_canonical_stations_missing")
    models, truths, source_root = _read_active_facts(db)
    model_by_scope: dict[tuple[str, str, str], CanonicalModelFact] = {}
    for model in models:
        if (
            model.station_id not in station_ids
            or model.model_family != MODEL_FAMILY
            or model.lead_bucket != LEAD_BUCKET
            or model.run_cycle != RUN_CYCLE
            or model.event_date not in ALL_DATES
        ):
            continue
        identifier = model.model_id or model.ensemble_system_id
        if identifier not in EXPECTED_MODELS:
            raise ContractError("d1_canonical_model_identifier_invalid")
        expected_init = datetime.combine(
            date.fromisoformat(model.event_date) - timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        if _utc(model.run_init_utc, "d1_canonical_run_init_invalid") != expected_init:
            raise ContractError("d1_canonical_run_cycle_semantics_invalid")
        if (
            model.oos_role != "historical_strict_oos"
            or not model.closed_day
            or not model.strict_asof
            or model.formal_live_authority
            or model.p1_eligible
        ):
            raise ContractError("d1_canonical_model_lineage_invalid")
        key = (model.station_id, model.event_date, identifier)
        if key in model_by_scope:
            raise ContractError("d1_canonical_model_scope_duplicate")
        model_by_scope[key] = model

    truth_by_scope: dict[tuple[str, str], CanonicalTruthFact] = {}
    for truth in truths:
        if truth.station_id not in station_ids or truth.event_date not in ALL_DATES:
            continue
        if (
            truth.source_lane != "AWC"
            or truth.source_role != "AWC_HISTORICAL_REPLAY_INGRESS"
            or truth.authority_scope != "PRODUCTION_NO_LIVE_ONLY"
            or not truth.closed_day
            or _utc(truth.canonical_committed_at_utc, "d1_canonical_truth_commit_invalid") > as_of
        ):
            raise ContractError("d1_canonical_truth_lineage_invalid")
        key = (truth.station_id, truth.event_date)
        if key in truth_by_scope:
            raise ContractError("d1_canonical_truth_scope_duplicate")
        truth_by_scope[key] = truth

    selected_models: dict[str, CanonicalModelFact] = {}
    selected_truths: dict[str, CanonicalTruthFact] = {}
    residuals: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    for station in station_ids:
        complete_dates: list[str] = []
        missing_truth_dates: list[str] = []
        missing_model_scopes: list[str] = []
        for event_date in ALL_DATES:
            truth = truth_by_scope.get((station, event_date))
            if truth is None:
                missing_truth_dates.append(event_date)
                continue
            date_models: list[CanonicalModelFact] = []
            missing_models = []
            for model_id in EXPECTED_MODELS:
                model = model_by_scope.get((station, event_date, model_id))
                if model is None:
                    missing_models.append(model_id)
                else:
                    date_models.append(model)
            if missing_models:
                missing_model_scopes.extend(
                    f"{event_date}|{model_id}" for model_id in missing_models
                )
                continue
            if any(model.settlement_timezone != truth.settlement_timezone for model in date_models):
                raise ContractError("d1_canonical_truth_model_timezone_mismatch")
            complete_dates.append(event_date)
            selected_truths[truth.content_sha256] = truth
            for model in date_models:
                selected_models[model.content_sha256] = model
                residual_identity = {
                    "residual_id": "d1-residual-" + content_sha256({
                        "truth_id": truth.truth_id,
                        "model_fact_content_sha256": model.content_sha256,
                    })[:32],
                    "truth_id": truth.truth_id,
                    "model_fact_content_sha256": model.content_sha256,
                    "station_id": station,
                    "model_id": model.model_id,
                    "event_date": event_date,
                    "lead_bucket": LEAD_BUCKET,
                    "run_cycle": RUN_CYCLE,
                    "residual_value": truth.truth_value - model.forecast_value,
                    "paired_asof_utc": truth.canonical_committed_at_utc,
                    "oos_role": "historical_strict_oos",
                }
                residuals.append(
                    {
                        **residual_identity,
                        "content_sha256": content_sha256(residual_identity),
                    }
                )
        train_dates = [item for item in TRAIN_DATES if item in set(complete_dates)]
        holdout_dates = [item for item in HOLDOUT_DATES if item in set(complete_dates)]
        blockers: list[str] = []
        if missing_truth_dates or missing_model_scopes:
            blockers.append("DATA_MISSING")
        if len(train_dates) < len(TRAIN_DATES) or len(holdout_dates) < len(HOLDOUT_DATES):
            blockers.append("OOS_SAMPLE_SHORT")
        statuses.append(
            {
                "station_id": station,
                "state": "READY" if not blockers else "BLOCKED",
                "blockers": blockers,
                "train_count": len(train_dates),
                "holdout_count": len(holdout_dates),
                "complete_dates": complete_dates,
                "missing_truth_dates": missing_truth_dates,
                "missing_model_scopes": missing_model_scopes,
            }
        )

    if not residuals:
        raise ContractError("d1_canonical_residual_contract_empty")
    contract_identity = {
        "schema": CANONICAL_CONTRACT_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": normalized_as_of,
        "builder_id": BUILDER_ID,
        "model_facts": [
            selected_models[key].to_mapping() for key in sorted(selected_models)
        ],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [
            selected_truths[key].to_mapping() for key in sorted(selected_truths)
        ],
        "residual_facts": sorted(
            residuals,
            key=lambda item: (
                item["station_id"],
                item["event_date"],
                item["model_id"],
            ),
        ),
        "activation_facts": [],
    }
    contract = CanonicalContract.from_mapping(
        {**contract_identity, "content_sha256": content_sha256(contract_identity)}
    ).to_mapping()
    receipt_identity = {
        "schema": BUILD_SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": normalized_as_of,
        "source_canonical_root_sha256": source_root,
        "canonical_contract_sha256": contract["content_sha256"],
        "station_count": len(statuses),
        "ready_station_count": sum(item["state"] == "READY" for item in statuses),
        "blocked_station_count": sum(item["state"] == "BLOCKED" for item in statuses),
        "model_fact_count": len(contract["model_facts"]),
        "truth_fact_count": len(contract["truth_facts"]),
        "residual_fact_count": len(contract["residual_facts"]),
        "activation_fact_count": 0,
        "station_statuses": statuses,
        "production_no_live": True,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    receipt = {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }
    return contract, receipt


__all__ = [
    "ALL_DATES",
    "BUILDER_ID",
    "BUILD_SCHEMA",
    "EXPECTED_MODELS",
    "HOLDOUT_DATES",
    "TRAIN_DATES",
    "build_d1_canonical_residual_contract",
]
