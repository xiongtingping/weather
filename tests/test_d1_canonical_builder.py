from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.canonical_contract import CONTRACT_VERSION, SCHEMA
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.contracts import ContractError, MODEL_IDS, content_sha256
from bayes_basket_target.d1_canonical_builder import (
    ALL_DATES,
    HOLDOUT_DATES,
    TRAIN_DATES,
    build_d1_canonical_residual_contract,
)
from bayes_basket_target.model_builders import build_scope
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts.ingest_canonical_contract_no_live import _assert_no_live_authority


def _model(event_date: str, model_id: str, *, timezone_name: str = "Asia/Shanghai") -> dict:
    previous = date.fromisoformat(event_date) - timedelta(days=1)
    identity = {
        "station_id": "ZBAA",
        "model_id": model_id,
        "ensemble_system_id": "",
        "model_family": "global_deterministic",
        "provider_id": "OPEN_METEO_" + model_id,
        "source_class": "aggregator_republisher",
        "event_date": event_date,
        "run_init_utc": previous.isoformat() + "T00:00:00Z",
        "lead_bucket": "d1",
        "run_cycle": "PREVIOUS_DAY1",
        "settlement_timezone": timezone_name,
        "forecast_value": 30.0,
        "raw_object_sha256": content_sha256([event_date, model_id]),
        "oos_role": "historical_strict_oos",
        "closed_day": True,
        "strict_asof": True,
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "published_at_utc": "",
        "received_at_utc": "",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _truth(event_date: str) -> dict:
    identity = {
        "truth_id": "truth-" + event_date,
        "station_id": "ZBAA",
        "event_date": event_date,
        "truth_revision_no": 0,
        "truth_value": 32.0,
        "truth_content_sha256": content_sha256(["truth", event_date]),
        "canonical_committed_at_utc": "2026-08-03T12:00:00Z",
        "station_day_end_utc": event_date + "T16:00:00Z",
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "Asia/Shanghai",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _contract(models: list[dict], truths: list[dict]) -> dict:
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": "2026-08-03T12:00:00Z",
        "builder_id": "d1.test.seed",
        "model_facts": models,
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": truths,
        "residual_facts": [],
        "activation_facts": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _database(tmp_path: Path, truth_dates: tuple[str, ...]) -> Path:
    path = tmp_path / "ssot.db"
    with sqlite3.connect(path) as conn:
        apply_production_ssot_schema(conn)
        models = [
            _model(event_date, model_id)
            for event_date in ALL_DATES
            for model_id in sorted(MODEL_IDS)
        ]
        truths = [_truth(event_date) for event_date in truth_dates]
        ingest_canonical_contract(conn, _contract(models, truths), commit=True)
    return path


def test_d1_builder_recomputes_residuals_from_canonical_truth_and_models(tmp_path: Path) -> None:
    path = _database(tmp_path, ALL_DATES)
    contract, receipt = build_d1_canonical_residual_contract(
        db=path,
        stations=["ZBAA"],
        as_of_utc="2026-08-03T12:00:00Z",
    )
    assert receipt["ready_station_count"] == 1
    assert receipt["residual_fact_count"] == 28 * 5
    assert len(contract["activation_facts"]) == 0
    assert {row["residual_value"] for row in contract["residual_facts"]} == {2.0}
    assert all(row["truth_id"].startswith("truth-") for row in contract["residual_facts"])


@pytest.mark.parametrize(
    "truth_dates,expected_train,expected_holdout",
    [
        (TRAIN_DATES[:-1] + HOLDOUT_DATES, 19, 8),
        (TRAIN_DATES + HOLDOUT_DATES[:-1], 20, 7),
    ],
)
def test_d1_builder_blocks_short_exact_cohort(
    tmp_path: Path,
    truth_dates: tuple[str, ...],
    expected_train: int,
    expected_holdout: int,
) -> None:
    path = _database(tmp_path, truth_dates)
    _contract_value, receipt = build_d1_canonical_residual_contract(
        db=path,
        stations=["ZBAA"],
        as_of_utc="2026-08-03T12:00:00Z",
    )
    status = receipt["station_statuses"][0]
    assert status["state"] == "BLOCKED"
    assert status["blockers"] == ["DATA_MISSING", "OOS_SAMPLE_SHORT"]
    assert status["train_count"] == expected_train
    assert status["holdout_count"] == expected_holdout


def test_d1_builder_rejects_truth_model_timezone_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "ssot.db"
    with sqlite3.connect(path) as conn:
        apply_production_ssot_schema(conn)
        ingest_canonical_contract(
            conn,
            _contract(
                [_model(ALL_DATES[0], model_id, timezone_name="America/New_York") for model_id in sorted(MODEL_IDS)],
                [_truth(ALL_DATES[0])],
            ),
            commit=True,
        )
    with pytest.raises(ContractError, match="truth_model_timezone_mismatch"):
        build_d1_canonical_residual_contract(
            db=path,
            stations=["ZBAA"],
            as_of_utc="2026-08-03T12:00:00Z",
        )


def test_no_live_ingress_rejects_formal_truth_authority() -> None:
    formal = _truth(ALL_DATES[0])
    formal["source_role"] = "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1"
    formal["authority_scope"] = "FORMAL_NATIVE_ELIGIBLE"
    formal["content_sha256"] = content_sha256(
        {key: value for key, value in formal.items() if key != "content_sha256"}
    )
    contract = _contract([], [formal])
    from bayes_basket_target.canonical_contract import CanonicalContract

    with pytest.raises(ContractError, match="no_live_truth_authority_forbidden"):
        _assert_no_live_authority(CanonicalContract.from_mapping(contract))


def test_d1_registry_build_does_not_require_generation(tmp_path: Path) -> None:
    path = _database(tmp_path, TRAIN_DATES[:-1] + HOLDOUT_DATES)
    result = build_scope(
        lead_bucket="d1",
        model_family="global_deterministic",
        db=path,
        stations=["ZBAA"],
        as_of_utc="2026-08-03T12:00:00Z",
    )
    assert result["state"] == "BUILT"
    assert result["canonical_contract"]["activation_facts"] == []
    assert result["receipt"]["station_statuses"][0]["state"] == "BLOCKED"


def test_d1_builder_reads_committed_wal_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "wal-ssot.db"
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        apply_production_ssot_schema(writer)
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        models = [
            _model(event_date, model_id)
            for event_date in ALL_DATES
            for model_id in sorted(MODEL_IDS)
        ]
        truths = [_truth(event_date) for event_date in ALL_DATES]
        ingest_canonical_contract(
            writer,
            _contract(models, truths),
            commit=True,
        )
        assert Path(str(path) + "-wal").stat().st_size > 0
        _contract_value, receipt = build_d1_canonical_residual_contract(
            db=path,
            stations=["ZBAA"],
            as_of_utc="2026-08-03T12:00:00Z",
        )
        assert receipt["ready_station_count"] == 1
        assert receipt["residual_fact_count"] == 28 * 5
    finally:
        writer.close()
