from __future__ import annotations

import copy
import math
import sqlite3
from datetime import date, timedelta
from typing import Any

import pytest

from bayes_basket_target.canonical_contract import (
    CONTRACT_VERSION,
    REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA,
    SCHEMA,
    CanonicalRepublisherOOSArtifactContract,
)
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_ssot_v7_migration import (
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.deterministic_republisher_oos import (
    CANDIDATE_SCHEMA,
    SCHEMA as REPUBLISHER_REPORT_SCHEMA,
)
from bayes_basket_target.republisher_oos_artifact import (
    build_republisher_oos_artifact_contract,
)


STATION_ID = "KAAA"
TIMEZONE = "America/New_York"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
AS_OF_UTC = "2026-02-15T00:00:00Z"


def _truth(*, event_date: str, revision: int = 1, value: float) -> dict[str, Any]:
    day = date.fromisoformat(event_date)
    day_end = (day + timedelta(days=1)).isoformat() + "T05:00:00Z"
    committed = (day + timedelta(days=2)).isoformat() + "T05:00:00Z"
    identity = {
        "truth_id": f"awc-{STATION_ID.lower()}-{event_date}-r{revision}",
        "station_id": STATION_ID,
        "event_date": event_date,
        "truth_revision_no": revision,
        "truth_value": value,
        "truth_content_sha256": content_sha256(
            {"awc": STATION_ID, "event_date": event_date, "revision": revision}
        ),
        "canonical_committed_at_utc": committed,
        "station_day_end_utc": day_end,
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": TIMEZONE,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _truth_contract(truths: list[dict[str, Any]]) -> dict[str, Any]:
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": AS_OF_UTC,
        "builder_id": "republisher-oos-artifact-test.truths",
        "model_facts": [],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": truths,
        "residual_facts": [],
        "activation_facts": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _metrics(errors: list[float]) -> dict[str, float]:
    return {
        "mae_c": sum(abs(value) for value in errors) / len(errors),
        "rmse_c": math.sqrt(sum(value * value for value in errors) / len(errors)),
        "mean_error_c": sum(errors) / len(errors),
    }


def _cohort(truths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ordinal, truth in enumerate(truths):
        partition = "train" if ordinal < 20 else "holdout"
        # Training implies a +1°C bias correction.  Holdout errors remain
        # nonzero so metric recomputation tests cannot pass accidentally.
        forecast = truth["truth_value"] - (1.0 if partition == "train" else 0.5)
        identity = {
            "event_date": truth["event_date"],
            "partition": partition,
            "forecast_daily_max_c": forecast,
            "truth_value_c": truth["truth_value"],
            "model_input_content_sha256": content_sha256(
                {"model-input": truth["event_date"]}
            ),
            "source_receipt_sha256": content_sha256(
                {"receipt": truth["event_date"]}
            ),
            "canonical_truth_fact_content_sha256": truth["content_sha256"],
            "awc_truth_id": truth["truth_id"],
            "awc_truth_content_sha256": truth["truth_content_sha256"],
            "awc_truth_revision_no": truth["truth_revision_no"],
            "awc_truth_asof_utc": truth["canonical_committed_at_utc"],
            "station_day_end_utc": truth["station_day_end_utc"],
        }
        rows.append({**identity, "case_contract_sha256": content_sha256(identity)})
    return rows


def _report(truths: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _cohort(truths)
    train = rows[:20]
    holdout = rows[20:]
    bias = sum(
        row["truth_value_c"] - row["forecast_daily_max_c"] for row in train
    ) / len(train)
    climate = sum(row["truth_value_c"] for row in train) / len(train)
    candidate_identity = {
        "schema": CANDIDATE_SCHEMA,
        "state": "PRODUCTION_NO_LIVE_OOS_PASS",
        "station_id": STATION_ID,
        "model_id": "AIFS",
        "provider_id": "ecmwf_aifs_single_open_meteo",
        "transport_provider_id": "OPEN_METEO",
        "source_role": "aggregator_republisher",
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "formal_p0_eligible": False,
        "native_arrival_index_eligible": False,
        "p1_eligible": False,
        "future_event_count": 0,
        "settlement_timezone": TIMEZONE,
        "lead_bucket": "d0",
        "run_cycle": None,
        "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE",
        "season_bin": "DJF",
        "forecast_extraction_version": "open_meteo_daily_tmax_v1",
        "train_n": 20,
        "holdout_n": 8,
        "cohort_rows": rows,
        "train_case_set_sha256": content_sha256(
            [row["case_contract_sha256"] for row in train]
        ),
        "holdout_case_set_sha256": content_sha256(
            [row["case_contract_sha256"] for row in holdout]
        ),
        "train_bias_c": bias,
        "train_climatology_tmax_c": climate,
        "model_metrics": _metrics(
            [
                row["forecast_daily_max_c"] + bias - row["truth_value_c"]
                for row in holdout
            ]
        ),
        "climatology_baseline_metrics": _metrics(
            [climate - row["truth_value_c"] for row in holdout]
        ),
    }
    candidate = {
        **candidate_identity,
        "content_sha256": content_sha256(candidate_identity),
    }
    report_identity = {
        "schema": REPUBLISHER_REPORT_SCHEMA,
        "as_of_utc": AS_OF_UTC,
        "model_id": "AIFS",
        "provider_id": "ecmwf_aifs_single_open_meteo",
        "transport_provider_id": "OPEN_METEO",
        "source_role": "aggregator_republisher",
        "truth_source": "canonical_ssot_fact_v7",
        "legacy_truth_projection_read": False,
        "formal_p0_eligible": False,
        "candidates": [candidate],
    }
    return {**report_identity, "content_sha256": content_sha256(report_identity)}


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(TARGET_TABLE_DDL)
    for trigger in TARGET_TRIGGER_DDL:
        conn.execute(trigger)
    # Deliberately do not create any native-arrival, manifest, or activation
    # table: a successful artifact flow proves it has no hidden native-table
    # dependency.
    conn.execute(
        "CREATE TABLE calibration_station_registry("
        "station_id TEXT NOT NULL,settlement_definition TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO calibration_station_registry(station_id,settlement_definition) "
        "VALUES(?,?)",
        (STATION_ID, SETTLEMENT_DEFINITION),
    )
    # The ingress owns its own final transaction for committed packets.
    conn.commit()
    return conn


def _packet_and_connection() -> tuple[sqlite3.Connection, dict[str, Any]]:
    start = date(2026, 1, 1)
    truths = [
        _truth(
            event_date=(start + timedelta(days=offset)).isoformat(),
            value=10.0 + offset,
        )
        for offset in range(28)
    ]
    conn = _connection()
    truth_receipt = ingest_canonical_contract(
        conn, _truth_contract(truths), commit=True
    )
    assert truth_receipt["inserted_count"] == 28
    return conn, build_republisher_oos_artifact_contract(
        _report(truths), conn, builder_id="republisher-oos-artifact-test.builder"
    )


def _rehash(packet: dict[str, Any]) -> None:
    """Recompute the nested canonical identities after a deliberate mutation."""

    fact = packet["artifact_facts"][0]
    for row in fact["cohort_rows"]:
        row["case_contract_sha256"] = content_sha256(
            {key: value for key, value in row.items() if key != "case_contract_sha256"}
        )
    train = fact["cohort_rows"][:20]
    holdout = fact["cohort_rows"][20:]
    fact["train_case_set_sha256"] = content_sha256(
        [row["case_contract_sha256"] for row in train]
    )
    fact["holdout_case_set_sha256"] = content_sha256(
        [row["case_contract_sha256"] for row in holdout]
    )
    cohort_identity = {
        key: fact[key]
        for key in (
            "station_id",
            "upstream_model_id",
            "republisher_product_id",
            "independence_family_id",
            "source_class",
            "authority_scope",
            "settlement_timezone",
            "settlement_definition",
            "lead_bucket",
            "run_cycle",
            "run_cycle_scope",
            "season_bin",
            "forecast_extraction_version",
            "artifact_method_version",
            "oos_protocol_version",
        )
    }
    cohort_identity["cohort_rows"] = fact["cohort_rows"]
    fact["strict_oos_cohort_sha256"] = content_sha256(cohort_identity)
    natural_identity = {
        key: cohort_identity[key]
        for key in cohort_identity
        if key != "cohort_rows"
    }
    natural_identity.update(
        {
            "train_case_set_sha256": fact["train_case_set_sha256"],
            "holdout_case_set_sha256": fact["holdout_case_set_sha256"],
        }
    )
    fact["artifact_id"] = (
        "republisher-historical-oos-" + content_sha256(natural_identity)[:40]
    )
    fact["content_sha256"] = content_sha256(
        {key: value for key, value in fact.items() if key != "content_sha256"}
    )
    packet["content_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "content_sha256"}
    )


def _count(conn: sqlite3.Connection, fact_type: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type=?", (fact_type,)
        ).fetchone()[0]
    )


def test_complete_20_8_builder_contract_preview_commit_replay_and_no_native_tables() -> None:
    conn, packet = _packet_and_connection()
    try:
        assert packet["schema"] == REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA
        assert CanonicalRepublisherOOSArtifactContract.from_mapping(packet).to_mapping() == packet
        assert _count(conn, "republisher_oos_artifact") == 0

        conn.execute("PRAGMA query_only=ON")
        preview = ingest_canonical_contract(conn, packet, commit=False)
        assert preview["state"] == "VALIDATED_NO_WRITE"
        assert preview["inserted_count"] == 0
        assert _count(conn, "republisher_oos_artifact") == 0
        conn.execute("PRAGMA query_only=OFF")

        committed = ingest_canonical_contract(conn, packet, commit=True)
        assert committed["state"] == "COMMITTED"
        assert committed["inserted_count"] == 1
        assert committed["replayed_count"] == 0
        assert _count(conn, "republisher_oos_artifact") == 1

        replay = ingest_canonical_contract(conn, packet, commit=True)
        assert replay["inserted_count"] == 0
        assert replay["replayed_count"] == 1
        assert _count(conn, "republisher_oos_artifact") == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name LIKE '%native%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("train_n", "holdout_n"), ((19, 8), (20, 7)))
def test_contract_rejects_non_20_8_partitions(train_n: int, holdout_n: int) -> None:
    conn, packet = _packet_and_connection()
    try:
        bad = copy.deepcopy(packet)
        fact = bad["artifact_facts"][0]
        fact["train_n"] = train_n
        fact["holdout_n"] = holdout_n
        fact["cohort_rows"] = fact["cohort_rows"][: train_n + holdout_n]
        _rehash(bad)
        with pytest.raises(ContractError, match="cohort_size_invalid"):
            CanonicalRepublisherOOSArtifactContract.from_mapping(bad)
    finally:
        conn.close()


@pytest.mark.parametrize("mutation", ("duplicate", "unordered"))
def test_contract_rejects_duplicate_or_unordered_case_dates(mutation: str) -> None:
    conn, packet = _packet_and_connection()
    try:
        bad = copy.deepcopy(packet)
        rows = bad["artifact_facts"][0]["cohort_rows"]
        if mutation == "duplicate":
            rows[1]["event_date"] = rows[0]["event_date"]
        else:
            rows[0], rows[1] = rows[1], rows[0]
        _rehash(bad)
        with pytest.raises(ContractError, match="cohort_dates_invalid"):
            CanonicalRepublisherOOSArtifactContract.from_mapping(bad)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "field,value",
    (
        ("awc_truth_revision_no", 2),
        ("awc_truth_content_sha256", "d" * 64),
        ("awc_truth_asof_utc", "2026-01-31T05:00:00Z"),
        ("station_day_end_utc", "2026-01-02T06:00:00Z"),
    ),
)
def test_ingress_rejects_truth_revision_hash_asof_or_day_end_mismatch(
    field: str, value: Any
) -> None:
    conn, packet = _packet_and_connection()
    try:
        bad = copy.deepcopy(packet)
        bad["artifact_facts"][0]["cohort_rows"][0][field] = value
        _rehash(bad)
        with pytest.raises(ContractError, match="truth_fact_lineage_mismatch"):
            ingest_canonical_contract(conn, bad, commit=False)
        assert _count(conn, "republisher_oos_artifact") == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "field", ("native_arrival_indexed", "p1_eligible", "formal_live_authority")
)
def test_contract_rejects_any_native_p1_or_formal_authority_flag(field: str) -> None:
    conn, packet = _packet_and_connection()
    try:
        bad = copy.deepcopy(packet)
        bad["artifact_facts"][0][field] = True
        _rehash(bad)
        with pytest.raises(ContractError, match="authority_escalation"):
            CanonicalRepublisherOOSArtifactContract.from_mapping(bad)
    finally:
        conn.close()


@pytest.mark.parametrize("mutation", ("parameters", "metrics", "fact_hash"))
def test_contract_rejects_unrecomputable_parameters_metrics_or_content_hash(
    mutation: str,
) -> None:
    conn, packet = _packet_and_connection()
    try:
        bad = copy.deepcopy(packet)
        fact = bad["artifact_facts"][0]
        if mutation == "parameters":
            fact["calibration_parameters"]["bias_c"] += 0.25
            _rehash(bad)
            expected = "parameters_mismatch"
        elif mutation == "metrics":
            fact["model_metrics"]["rmse_c"] += 0.25
            _rehash(bad)
            expected = "metrics_mismatch"
        else:
            fact["content_sha256"] = "e" * 64
            # Keep the enclosing packet hash valid so the failure is exactly
            # the artifact's content-address invariant.
            bad["content_sha256"] = content_sha256(
                {key: value for key, value in bad.items() if key != "content_sha256"}
            )
            expected = "artifact_fact_content_sha256_mismatch"
        with pytest.raises(ContractError, match=expected):
            CanonicalRepublisherOOSArtifactContract.from_mapping(bad)
    finally:
        conn.close()


def test_ingress_rejects_same_scope_different_artifact_content() -> None:
    conn, packet = _packet_and_connection()
    try:
        assert ingest_canonical_contract(conn, packet, commit=True)["inserted_count"] == 1
        conflict = copy.deepcopy(packet)
        conflict["artifact_facts"][0]["evaluation_content_sha256"] = content_sha256(
            {"reviewed-evaluation": "replacement"}
        )
        _rehash(conflict)
        with pytest.raises(ContractError, match="canonical_scope_content_conflict"):
            ingest_canonical_contract(conn, conflict, commit=True)
        assert _count(conn, "republisher_oos_artifact") == 1
    finally:
        conn.close()
