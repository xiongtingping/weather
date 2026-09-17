from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from types import SimpleNamespace

import pytest

import bayes_basket_target.canonical_ingress as canonical_ingress_module
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_contract import (
    CONTRACT_VERSION as CANONICAL_CONTRACT_VERSION,
    SCHEMA as CANONICAL_CONTRACT_SCHEMA,
)
from bayes_basket_target.canonical_ssot_schema import inspect_canonical_ssot_schema
from bayes_basket_target.canonical_ssot_v13_migration import (
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v14_migration import (
    TARGET_TABLE_DDL as V14_TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL as V14_TARGET_TRIGGER_DDL,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.market_bucket_bridge import (
    CONTRACT_SCHEMA as MARKET_BUCKET_BRIDGE_CONTRACT_SCHEMA,
    FACT_SCHEMA as MARKET_BUCKET_BRIDGE_FACT_SCHEMA,
)
from bayes_basket_target.model_oos_contract import (
    CALIBRATION_METHOD_VERSION,
    CONTRACT_SCHEMA,
    MODEL_RECEIPT_SCHEMA,
    CanonicalModelOOSEventFact,
    CanonicalModelOOSContract,
    derive_cohort_statistics,
)
from bayes_basket_target.model_oos_builder import build_model_oos_contract
from bayes_basket_target.model_product_identity import (
    PRODUCT_BY_ID,
    assert_formal_p0_native_ensemble_systems,
)
from bayes_basket_target.pr10_model_oos_basis import build_pr10_model_oos_terminal_basis
from bayes_basket_target.resource_bounds import write_bounded_json
from bayes_basket_target.model_oos_selection import (
    CanonicalModelOOSSelectionContract,
    build_historical_holdout_access_receipt,
    build_historical_holdout_reservation,
    build_model_oos_selection_terminal_receipt,
    build_model_oos_selection_contract,
    validate_model_oos_selection_terminal_receipt,
)
from bayes_basket_target.p0_calibrated_distribution import (
    CanonicalP0CalibratedDistributionContract,
    build_p0_calibrated_distribution_contract,
)
from bayes_basket_target.p0_activation_root import (
    CanonicalP0ActivationRootContract,
    build_p0_activation_root_contract,
)
from bayes_basket_target.p0_forecast_snapshot import (
    RECEIPT_SCHEMA as P0_FORWARD_RECEIPT_SCHEMA,
    CanonicalP0ForecastSnapshotContract,
    build_p0_forecast_snapshot_contract,
)
from tests.test_awc_observation_contract import _contract as awc_contract


def test_component_calibration_requires_strict_holdout_gain_over_raw() -> None:
    # A zero training bias leaves every holdout forecast unchanged.  The
    # calibrated and raw MAE are therefore equal, which is not evidence that
    # calibration added value.
    events = [
        SimpleNamespace(
            raw={
                "forecast_value_c": float(index),
                "truth_value_c": float(index),
            }
        )
        for index in range(28)
    ]

    parameters, _train, calibrated, raw, state = derive_cohort_statistics(events)

    assert parameters["bias_c"] == 0.0
    assert calibrated["mae_c"] == raw["mae_c"] == 0.0
    assert state == "OOS_REJECTED"


def _seal(value: dict[str, object]) -> dict[str, object]:
    return {**value, "content_sha256": content_sha256(value)}


def _registered_component(product_id: str) -> dict[str, object]:
    product = PRODUCT_BY_ID[product_id]
    return {
        key: product[key] for key in (
            "model_id", "ensemble_system_id", "model_family", "provider_id",
            "provider_product_id", "independence_family_id", "source_class",
        )
    }


def test_formal_p0_product_gate_requires_two_distinct_native_ensembles() -> None:
    ecmwf = _registered_component("ECMWF_IFS_ENS_OPEN_PF50")
    aifs = _registered_component("ECMWF_AIFS_ENS_GLOBAL")
    gefs = _registered_component("NOAA_GEFS_05")
    assert assert_formal_p0_native_ensemble_systems([ecmwf, gefs]) == (
        "ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05",
    )
    assert assert_formal_p0_native_ensemble_systems([aifs, gefs]) == (
        "ECMWF_AIFS_ENS_GLOBAL", "NOAA_GEFS_05",
    )

    republisher_aifs = {
        **aifs,
        "provider_id": "OPEN_METEO",
        "source_class": "aggregator_republisher",
    }
    deterministic_ecmwf = {
        **ecmwf,
        "ensemble_system_id": None,
        "model_family": "global_deterministic",
        "provider_id": "ecmwf_ifs025_official",
        "provider_product_id": "ecmwf_ifs025",
    }
    deterministic_gfs = {
        **gefs,
        "ensemble_system_id": None,
        "model_family": "global_deterministic",
        "provider_id": "gfs_0p25_official",
        "provider_product_id": "noaa_gfs_0p25",
    }
    invalid_masks = (
        [republisher_aifs, gefs],
        [deterministic_ecmwf, deterministic_gfs],
        [ecmwf],
        [ecmwf, dict(ecmwf)],
        [ecmwf, aifs],
    )
    for components in invalid_masks:
        with pytest.raises(ContractError, match="formal_p0_"):
            assert_formal_p0_native_ensemble_systems(components)


def _canonical_utc(value: object) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _model_oos_utc(value: object) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _market_bucket_bridge_contract() -> dict[str, object]:
    buckets = [
        {
            "ordinal": ordinal,
            "outcome": outcome,
            "lower_c": lower,
            "upper_c": upper,
            "slug": "katl-highest-temperature-on-august-2-2026",
            "market_id": f"market-{ordinal}",
            "condition_id": f"condition-{ordinal}",
            "yes_token": f"yes-{ordinal}",
            "no_token": f"no-{ordinal}",
            "rules_sha256": "a" * 64,
            "official_source_registry_sha256": "b" * 64,
        }
        for ordinal, (outcome, lower, upper) in enumerate(
            (
                ("below-30", None, "30.000000000000"),
                ("30-to-31", "30.000000000000", "32.000000000000"),
                ("32-or-above", "32.000000000000", None),
            )
        )
    ]
    snapshot_sha = "c" * 64
    rowset_sha = content_sha256(buckets)
    bridge_seed = {
        "station_id": "KATL",
        "event_date": "2026-08-02",
        "market_registry_snapshot_sha256": snapshot_sha,
        "registry_scope_rowset_sha256": rowset_sha,
    }
    fact = _seal(
        {
            "schema": MARKET_BUCKET_BRIDGE_FACT_SCHEMA,
            "bridge_id": "market-bucket-bridge-"
            + content_sha256(bridge_seed)[:40],
            "station_id": "KATL",
            "event_date": "2026-08-02",
            "settlement_timezone": "America/New_York",
            "settlement_definition": "official_daily_tmax_c",
            "settlement_unit": "C",
            "source_lane": "AWC",
            "market_registry_snapshot_sha256": snapshot_sha,
            "registry_scope_rowset_sha256": rowset_sha,
            "bucket_contracts": buckets,
            "state": "MARKET_BUCKET_BRIDGE_READY",
            "authority_scope": "OFFICIAL_MARKET_REGISTRY",
        }
    )
    return _seal(
        {
            "schema": MARKET_BUCKET_BRIDGE_CONTRACT_SCHEMA,
            "as_of_utc": "2026-08-02T05:00:00.000Z",
            "builder_id": "test.market_bucket_bridge.v1",
            "bridge_facts": [fact],
        }
    )


def _event(awc: dict[str, object]) -> dict[str, object]:
    bounds = awc["day_bounds_facts"][0]
    truth = _truth_fact(awc)
    receipt = _seal(
        {
            "schema": MODEL_RECEIPT_SCHEMA,
            "station_id": "KATL",
            "model_id": "AIFS",
            "provider_id": "OPEN_METEO",
            "provider_product_id": "ecmwf_aifs025_single",
            "source_class": "aggregator_republisher",
            "event_date": "2026-08-01",
            "model_available_at_utc": "2026-08-01T02:59:00.000Z",
            "forecast_decision_at_utc": "2026-08-01T03:00:00.000Z",
            "retrieved_at_utc": "2026-08-01T02:59:00.000Z",
            "availability_clock_source": "REPUBLISHER_PROSPECTIVE_RECEIVED_AT",
            "raw_object_sha256": "1" * 64,
            "model_input_content_sha256": "2" * 64,
        }
    )
    return _seal(
        {
            "station_id": "KATL",
            "model_id": "AIFS",
            "ensemble_system_id": None,
            "model_family": "global_deterministic_ai",
            "provider_id": "OPEN_METEO",
            "provider_product_id": "ecmwf_aifs025_single",
            "independence_family_id": "ECMWF",
            "source_class": "aggregator_republisher",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "event_date": "2026-08-01",
            "settlement_timezone": "America/New_York",
            "lead_bucket": "d0",
            "lead_bucket_scope": "SAME_SETTLEMENT_DAY",
            "run_init_utc": None,
            "run_cycle": None,
            "run_cycle_scope": "REPUBLISHER_NO_NATIVE_CYCLE",
            "forecast_decision_at_utc": "2026-08-01T03:00:00.000Z",
            "model_available_at_utc": "2026-08-01T02:59:00.000Z",
            "forecast_value_c": 30.0,
            "raw_object_sha256": "1" * 64,
            "model_input_content_sha256": "2" * 64,
            "model_receipt": receipt,
            "model_receipt_sha256": receipt["content_sha256"],
            "canonical_truth_fact_content_sha256": truth["content_sha256"],
            "truth_id": truth["truth_id"],
            "truth_revision_no": truth["truth_revision_no"],
            "truth_content_sha256": truth["truth_content_sha256"],
            "truth_value_c": truth["truth_value"],
            "station_day_end_utc": _model_oos_utc(
                truth["station_day_end_utc"]
            ),
            "truth_canonical_committed_at_utc": _model_oos_utc(
                truth["canonical_committed_at_utc"]
            ),
            "event_oos_protocol_version": "strict_historical_awc_v12_v1",
            "state": "MATERIALIZED_UNPUBLISHED",
        }
    )


def _packet(event: dict[str, object]) -> dict[str, object]:
    return _seal(
        {
            "schema": CONTRACT_SCHEMA,
            "as_of_utc": "2026-08-02T05:00:00.000Z",
            "builder_id": "tests.model_oos.v1",
            "event_facts": [event],
            "cohort_facts": [],
        }
    )


def _awc_full_coverage() -> dict[str, object]:
    awc = deepcopy(awc_contract())
    bounds = dict(awc["day_bounds_facts"][0])
    start = datetime.fromisoformat(bounds["day_start_utc"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(bounds["day_end_utc"].replace("Z", "+00:00"))
    slots: list[str] = []
    cursor = start
    while cursor < end:
        slots.append(cursor.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        cursor += timedelta(hours=1)
    coverage = {
        **bounds["coverage_receipt"],
        "expected_hourly_slots": slots,
        "observed_hourly_slots": slots,
        "expected_hourly_metar_count": len(slots),
        "observed_hourly_metar_count": len(slots),
    }
    coverage = _seal({key: value for key, value in coverage.items() if key != "content_sha256"})
    bounds["coverage_receipt"] = coverage
    bounds["coverage_receipt_sha256"] = coverage["content_sha256"]
    bounds = _seal({key: value for key, value in bounds.items() if key != "content_sha256"})
    awc["day_bounds_facts"] = [bounds]
    return _seal({key: value for key, value in awc.items() if key != "content_sha256"})


def _truth_fact(awc: dict[str, object]) -> dict[str, object]:
    bounds = awc["day_bounds_facts"][0]
    identity = {
        "truth_id": "awc-historical-" + str(bounds["event_date"]),
        "station_id": str(bounds["station_id"]),
        "event_date": str(bounds["event_date"]),
        "truth_revision_no": 0,
        "truth_value": float(bounds["running_max_c"]),
        "truth_content_sha256": content_sha256([
            bounds["station_id"], bounds["event_date"], bounds["running_max_c"]
        ]),
        "canonical_committed_at_utc": _canonical_utc(
            bounds["canonical_committed_at_utc"]
        ),
        "station_day_end_utc": _canonical_utc(bounds["day_end_utc"]),
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "America/New_York",
    }
    return _seal(identity)


def _truth_contract(awc: dict[str, object]) -> dict[str, object]:
    return _truth_contract_for_facts(awc, [_truth_fact(awc)])


def _truth_contract_for_facts(
    awc: dict[str, object], facts: list[dict[str, object]]
) -> dict[str, object]:
    identity = {
        "schema": CANONICAL_CONTRACT_SCHEMA,
        "contract_version": CANONICAL_CONTRACT_VERSION,
        "as_of_utc": str(awc["as_of_utc"]),
        "builder_id": "tests.canonical_truth.v1",
        "model_facts": [],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": facts,
        "residual_facts": [],
        "activation_facts": [],
    }
    return _seal(identity)


def _truth_revision(
    awc: dict[str, object],
    *,
    revision_no: int,
    committed_at_utc: str,
    closed_day: bool = True,
    settlement_timezone: str = "America/New_York",
) -> dict[str, object]:
    base = _truth_fact(awc)
    identity = {
        key: value for key, value in base.items() if key != "content_sha256"
    }
    identity.update(
        {
            "truth_revision_no": revision_no,
            "truth_content_sha256": content_sha256(
                [base["truth_id"], revision_no, base["truth_value"]]
            ),
            "canonical_committed_at_utc": _canonical_utc(committed_at_utc),
            "closed_day": closed_day,
            "settlement_timezone": settlement_timezone,
        }
    )
    return _seal(identity)


def _awc_for_day(offset_days: int) -> dict[str, object]:
    awc = _awc_full_coverage()
    revision = dict(awc["revision_facts"][0])
    event_day = datetime(2026, 8, 1, tzinfo=timezone.utc).date() + timedelta(days=offset_days)
    revision["event_date"] = event_day.isoformat()
    revision["observation_id"] = f"rtobs_{event_day.isoformat()}"
    revision["raw_object_sha256"] = content_sha256(["raw", event_day.isoformat()])
    for field in (
        "observed_at_utc", "received_at_utc", "first_seen_at_utc", "parsed_at_utc",
        "fetch_started_at_utc", "fetch_completed_at_utc", "origin_committed_at_utc",
        "source_observation_committed_at_utc", "canonical_committed_at_utc",
    ):
        parsed = datetime.fromisoformat(str(revision[field]).replace("Z", "+00:00"))
        revision[field] = (parsed + timedelta(days=offset_days)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
    revision = _seal({key: value for key, value in revision.items() if key != "content_sha256"})

    bounds = dict(awc["day_bounds_facts"][0])
    bounds["event_date"] = event_day.isoformat()
    for field in ("day_start_utc", "day_end_utc", "canonical_committed_at_utc"):
        parsed = datetime.fromisoformat(str(bounds[field]).replace("Z", "+00:00"))
        bounds[field] = (parsed + timedelta(days=offset_days)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
    coverage = dict(bounds["coverage_receipt"])
    coverage["event_date"] = event_day.isoformat()
    coverage["as_of_utc"] = bounds["canonical_committed_at_utc"]
    coverage["expected_hourly_slots"] = [
        (datetime.fromisoformat(str(item).replace("Z", "+00:00")) + timedelta(days=offset_days))
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for item in coverage["expected_hourly_slots"]
    ]
    coverage["observed_hourly_slots"] = list(coverage["expected_hourly_slots"])
    coverage = _seal({key: value for key, value in coverage.items() if key != "content_sha256"})
    bounds["coverage_receipt"] = coverage
    bounds["coverage_receipt_sha256"] = coverage["content_sha256"]
    bounds["max_observation_fact_sha256"] = revision["content_sha256"]
    bounds["observation_revision_vector_sha256"] = content_sha256([revision["content_sha256"]])
    bounds["max_tie_vector_sha256"] = content_sha256([revision["content_sha256"]])
    bounds = _seal({key: value for key, value in bounds.items() if key != "content_sha256"})
    identity = {
        "schema": awc["schema"],
        "as_of_utc": bounds["canonical_committed_at_utc"],
        "builder_id": awc["builder_id"],
        "revision_facts": [revision],
        "day_bounds_facts": [bounds],
    }
    return _seal(identity)


def _event_for_day(awc: dict[str, object], ordinal: int) -> dict[str, object]:
    event = _event(awc)
    bounds = awc["day_bounds_facts"][0]
    day_start = datetime.fromisoformat(bounds["day_start_utc"].replace("Z", "+00:00"))
    available = day_start - timedelta(hours=2)
    decision = day_start - timedelta(hours=1)
    raw_sha = content_sha256(["model_raw", ordinal])
    input_sha = content_sha256(["model_input", ordinal])
    truth = _truth_fact(awc)
    event.update({
        "event_date": bounds["event_date"],
        "model_available_at_utc": available.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "forecast_decision_at_utc": decision.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "forecast_value_c": 29.0 + (ordinal % 4),
        "raw_object_sha256": raw_sha,
        "model_input_content_sha256": input_sha,
        "canonical_truth_fact_content_sha256": truth["content_sha256"],
        "truth_id": truth["truth_id"],
        "truth_revision_no": truth["truth_revision_no"],
        "truth_content_sha256": truth["truth_content_sha256"],
        "truth_value_c": truth["truth_value"],
        "station_day_end_utc": _model_oos_utc(truth["station_day_end_utc"]),
        "truth_canonical_committed_at_utc": _model_oos_utc(
            truth["canonical_committed_at_utc"]
        ),
    })
    receipt = _seal({
        **{key: value for key, value in event["model_receipt"].items() if key != "content_sha256"},
        "event_date": event["event_date"],
        "model_available_at_utc": event["model_available_at_utc"],
        "forecast_decision_at_utc": event["forecast_decision_at_utc"],
        "retrieved_at_utc": available.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "raw_object_sha256": raw_sha,
        "model_input_content_sha256": input_sha,
    })
    event["model_receipt"] = receipt
    event["model_receipt_sha256"] = receipt["content_sha256"]
    return _seal({key: value for key, value in event.items() if key != "content_sha256"})


def _event_bound_to_truth(
    awc: dict[str, object], truth: dict[str, object]
) -> dict[str, object]:
    event = _event(awc)
    event.update(
        {
            "settlement_timezone": truth["settlement_timezone"],
            "canonical_truth_fact_content_sha256": truth["content_sha256"],
            "truth_id": truth["truth_id"],
            "truth_revision_no": truth["truth_revision_no"],
            "truth_content_sha256": truth["truth_content_sha256"],
            "truth_value_c": truth["truth_value"],
            "station_day_end_utc": _model_oos_utc(
                truth["station_day_end_utc"]
            ),
            "truth_canonical_committed_at_utc": _model_oos_utc(
                truth["canonical_committed_at_utc"]
            ),
        }
    )
    return _seal(
        {key: value for key, value in event.items() if key != "content_sha256"}
    )


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(TARGET_TABLE_DDL)
    for ddl in TARGET_TRIGGER_DDL:
        conn.execute(ddl)
    assert inspect_canonical_ssot_schema(conn)["state"] == "V13"
    return conn


def _database_v14() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(V14_TARGET_TABLE_DDL)
    for ddl in V14_TARGET_TRIGGER_DDL:
        conn.execute(ddl)
    assert inspect_canonical_ssot_schema(conn)["state"] == "V14"
    return conn


def test_model_oos_event_requires_proven_pre_close_availability() -> None:
    awc = _awc_full_coverage()
    event = _event(awc)
    event["model_available_at_utc"] = "2026-08-02T04:01:00.000Z"
    event = _seal({key: value for key, value in event.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="strict_asof_order_invalid"):
        CanonicalModelOOSContract.from_mapping(_packet(event))


def test_model_oos_event_rejects_free_form_independence_family() -> None:
    awc = _awc_full_coverage()
    event = _event(awc)
    event["independence_family_id"] = "FAKE_SECOND_FAMILY"
    event = _seal({key: value for key, value in event.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="canonical_model_family_registry_mismatch"):
        CanonicalModelOOSEventFact.from_mapping(event)

    event = _event(awc)
    event["provider_product_id"] = "unregistered_alias"
    receipt = dict(event["model_receipt"])
    receipt["provider_product_id"] = "unregistered_alias"
    event["model_receipt"] = _seal({
        key: value for key, value in receipt.items() if key != "content_sha256"
    })
    event["model_receipt_sha256"] = event["model_receipt"]["content_sha256"]
    event = _seal({key: value for key, value in event.items() if key != "content_sha256"})
    with pytest.raises(
        ContractError, match="canonical_model_family_registry_product_not_unique"
    ):
        CanonicalModelOOSEventFact.from_mapping(event)


def test_model_oos_contract_rejects_event_clock_after_contract_asof() -> None:
    awc = _awc_full_coverage()
    packet = _packet(_event(awc))
    packet["as_of_utc"] = "2026-08-01T02:00:00.000Z"
    packet = _seal({key: value for key, value in packet.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="fact_after_asof"):
        CanonicalModelOOSContract.from_mapping(packet)


def test_v13_single_ingress_commits_exact_v12_bound_event_and_replays() -> None:
    conn = _database()
    awc = _awc_full_coverage()
    awc_receipt = ingest_canonical_contract(
        conn, _truth_contract(awc), commit=True, ingested_at_utc="2026-08-02T05:00:00Z"
    )
    assert awc_receipt["inserted_count"] == 1
    packet = _packet(_event(awc))
    receipt = ingest_canonical_contract(
        conn, packet, commit=True, ingested_at_utc="2026-08-02T05:01:00Z"
    )
    assert receipt["inserted_count"] == 1
    assert receipt["validated_counts"] == {
        "model_oos_event": 1,
        "model_oos_cohort": 0,
    }
    replay = ingest_canonical_contract(
        conn, packet, commit=True, ingested_at_utc="2026-08-02T05:01:00Z"
    )
    assert replay["inserted_count"] == 0
    assert replay["replayed_count"] == 1

def test_v13_event_fails_closed_on_truth_timezone_or_hash_drift() -> None:
    conn = _database()
    awc = _awc_full_coverage()
    ingest_canonical_contract(
        conn, _truth_contract(awc), commit=True, ingested_at_utc="2026-08-02T05:00:00Z"
    )
    event = _event(awc)
    event["settlement_timezone"] = "America/Toronto"
    event = _seal({key: value for key, value in event.items() if key != "content_sha256"})
    packet = _packet(event)
    with pytest.raises(ContractError, match="truth_fact_lineage_mismatch"):
        ingest_canonical_contract(conn, packet)

    event = _event(awc)
    event["truth_content_sha256"] = "f" * 64
    event = _seal({key: value for key, value in event.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="truth_fact_lineage_mismatch"):
        ingest_canonical_contract(conn, _packet(event))


def test_v13_event_rejects_stale_canonical_truth_revision() -> None:
    conn = _database()
    awc = _awc_full_coverage()
    revision_zero = _truth_fact(awc)
    revision_one = _truth_revision(
        awc,
        revision_no=1,
        committed_at_utc="2026-08-02T04:10:00Z",
    )
    truth_contract_awc = deepcopy(awc)
    truth_contract_awc["as_of_utc"] = "2026-08-02T05:00:00Z"
    ingest_canonical_contract(
        conn,
        _truth_contract_for_facts(
            truth_contract_awc, [revision_zero, revision_one]
        ),
        commit=True,
        ingested_at_utc="2026-08-02T05:00:00Z",
    )
    with pytest.raises(ContractError, match="truth_revision_stale"):
        ingest_canonical_contract(conn, _packet(_event(awc)))


def test_v13_event_ignores_revision_committed_after_contract_asof() -> None:
    conn = _database()
    awc = _awc_full_coverage()
    revision_zero = _truth_fact(awc)
    revision_one = _truth_revision(
        awc,
        revision_no=1,
        committed_at_utc="2026-08-02T06:00:00Z",
    )
    truth_contract_awc = deepcopy(awc)
    truth_contract_awc["as_of_utc"] = "2026-08-02T06:10:00Z"
    ingest_canonical_contract(
        conn,
        _truth_contract_for_facts(
            truth_contract_awc, [revision_zero, revision_one]
        ),
        commit=True,
        ingested_at_utc="2026-08-02T06:10:00Z",
    )
    packet = _packet(_event(awc))
    packet["as_of_utc"] = "2026-08-02T05:00:00.000Z"
    packet = _seal(
        {key: value for key, value in packet.items() if key != "content_sha256"}
    )
    receipt = ingest_canonical_contract(conn, packet, commit=False)
    assert receipt["validated_counts"]["model_oos_event"] == 1


def test_v13_event_ignores_higher_unclosed_truth_revision() -> None:
    conn = _database()
    awc = _awc_full_coverage()
    revision_zero = _truth_fact(awc)
    revision_one = _truth_revision(
        awc,
        revision_no=1,
        committed_at_utc="2026-08-02T04:10:00Z",
        closed_day=False,
    )
    truth_contract_awc = deepcopy(awc)
    truth_contract_awc["as_of_utc"] = "2026-08-02T05:00:00Z"
    ingest_canonical_contract(
        conn,
        _truth_contract_for_facts(
            truth_contract_awc, [revision_zero, revision_one]
        ),
        commit=True,
        ingested_at_utc="2026-08-02T05:00:00Z",
    )
    receipt = ingest_canonical_contract(
        conn, _packet(_event(awc)), commit=False
    )
    assert receipt["validated_counts"]["model_oos_event"] == 1


def test_v13_direct_ingress_rejects_non_et_truth_lineage() -> None:
    conn = _database()
    awc = _awc_full_coverage()
    toronto_truth = _truth_revision(
        awc,
        revision_no=0,
        committed_at_utc="2026-08-02T04:05:00Z",
        settlement_timezone="America/Toronto",
    )
    ingest_canonical_contract(
        conn,
        _truth_contract_for_facts(awc, [toronto_truth]),
        commit=True,
        ingested_at_utc="2026-08-02T05:00:00Z",
    )
    with pytest.raises(ContractError, match="truth_fact_lineage_mismatch"):
        ingest_canonical_contract(
            conn, _packet(_event_bound_to_truth(awc, toronto_truth))
        )


def test_v13_event_rejects_unbound_model_receipt_and_incomplete_truth_day() -> None:
    awc = _awc_full_coverage()
    event = _event(awc)
    event["model_receipt_sha256"] = "f" * 64
    event = _seal({key: value for key, value in event.items() if key != "content_sha256"})
    with pytest.raises(ContractError, match="model_receipt_sha256_mismatch"):
        CanonicalModelOOSContract.from_mapping(_packet(event))

    conn = _database()
    incomplete = awc_contract()
    with pytest.raises(ContractError, match="truth_fact_missing"):
        ingest_canonical_contract(conn, _packet(_event(incomplete)), commit=False)


def test_v12_schema_rejects_model_oos_contract_without_write() -> None:
    from bayes_basket_target.canonical_ssot_v12_migration import (
        TARGET_TABLE_DDL as V12_DDL,
        TARGET_TRIGGER_DDL as V12_TRIGGERS,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute(V12_DDL)
    for ddl in V12_TRIGGERS:
        conn.execute(ddl)
    with pytest.raises(ContractError, match="canonical_model_oos_v13_required"):
        ingest_canonical_contract(conn, _packet(_event(awc_contract())))
    assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0


def test_v13_ingress_recomputes_complete_28_event_cohort() -> None:
    conn = _database()
    awc_packets = [_awc_for_day(index - 27) for index in range(28)]
    events = [_event_for_day(awc, index) for index, awc in enumerate(awc_packets)]
    for awc in awc_packets:
        ingest_canonical_contract(conn, _truth_contract(awc), commit=True, ingested_at_utc="2026-08-02T05:00:00Z")
    parsed = [CanonicalModelOOSEventFact.from_mapping(event) for event in events]
    parameters, train_metrics, holdout, baseline, evaluation = derive_cohort_statistics(parsed)
    train_hashes = [event["content_sha256"] for event in events[:20]]
    holdout_hashes = [event["content_sha256"] for event in events[20:]]
    vector = [
        *({"partition": "train", "event_content_sha256": item} for item in train_hashes),
        *({"partition": "holdout", "event_content_sha256": item} for item in holdout_hashes),
    ]
    cohort = _seal({
        "station_id": "KATL", "model_id": "AIFS", "ensemble_system_id": None,
        "model_family": "global_deterministic_ai", "provider_id": "OPEN_METEO",
        "provider_product_id": "ecmwf_aifs025_single", "independence_family_id": "ECMWF",
        "source_class": "aggregator_republisher", "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "America/New_York", "lead_bucket": "d0",
        "lead_bucket_scope": "SAME_SETTLEMENT_DAY", "run_cycle": None,
        "run_cycle_scope": "REPUBLISHER_NO_NATIVE_CYCLE", "season_bin": "ALL_YEAR",
        "event_oos_protocol_version": "strict_historical_awc_v12_v1",
        "calibration_method_version": CALIBRATION_METHOD_VERSION,
        "cohort_as_of_utc": "2026-08-02T05:00:00.000Z",
        "train_event_content_sha256s": train_hashes,
        "holdout_event_content_sha256s": holdout_hashes,
        "train_event_set_sha256": content_sha256(train_hashes),
        "holdout_event_set_sha256": content_sha256(holdout_hashes),
        "cohort_event_vector_sha256": content_sha256(vector),
        "calibration_parameters": parameters, "selection_train_metrics": train_metrics,
        "holdout_metrics": holdout,
        "baseline_holdout_metrics": baseline, "evaluation_state": evaluation,
        "state": "MATERIALIZED_UNPUBLISHED",
    })
    identity = {
        "schema": CONTRACT_SCHEMA, "as_of_utc": "2026-08-02T05:00:00.000Z",
        "builder_id": "tests.model_oos.cohort.v1", "event_facts": events,
        "cohort_facts": [cohort],
    }
    receipt = ingest_canonical_contract(conn, _seal(identity), commit=True)
    assert receipt["validated_counts"] == {"model_oos_event": 28, "model_oos_cohort": 1}

    drifted = deepcopy(cohort)
    drifted["holdout_metrics"]["mae_c"] += 0.1
    drifted = _seal({key: value for key, value in drifted.items() if key != "content_sha256"})
    bad_identity = {**identity, "event_facts": [], "cohort_facts": [drifted]}
    with pytest.raises(ContractError, match="cohort_statistics_mismatch"):
        ingest_canonical_contract(conn, _seal(bad_identity), commit=False)


def test_lead_family_builder_requires_explicit_exact_20_plus_8_partition() -> None:
    conn = _database()
    awc_packets = [_awc_for_day(index - 27) for index in range(28)]
    source_events = [_event_for_day(awc, index) for index, awc in enumerate(awc_packets)]
    for awc in awc_packets:
        ingest_canonical_contract(conn, _truth_contract(awc), commit=True, ingested_at_utc="2026-08-02T05:00:00Z")
    input_fields = {
        "station_id", "model_id", "ensemble_system_id", "model_family",
        "provider_id", "provider_product_id", "independence_family_id",
        "source_class", "event_date", "settlement_timezone", "lead_bucket",
        "lead_bucket_scope", "run_init_utc", "run_cycle", "run_cycle_scope",
        "forecast_value_c", "canonical_truth_fact_content_sha256",
        "event_oos_protocol_version", "model_receipt",
    }
    inputs = [{key: event[key] for key in input_fields} for event in source_events]
    dates = [event["event_date"] for event in source_events]
    spec = {
        "season_bin": "ALL_YEAR",
        "calibration_method_version": CALIBRATION_METHOD_VERSION,
        "train_event_dates": dates[:20],
        "holdout_event_dates": dates[20:],
    }
    packet = build_model_oos_contract(
        conn, as_of_utc="2026-08-02T05:00:00.000Z",
        event_inputs=inputs, cohort_spec=spec,
    )
    assert len(packet["event_facts"]) == 28
    assert len(packet["cohort_facts"]) == 1
    receipt = ingest_canonical_contract(conn, packet, commit=False)
    assert receipt["validated_counts"] == {"model_oos_event": 28, "model_oos_cohort": 1}

    bad = deepcopy(spec)
    bad["holdout_event_dates"] = list(reversed(bad["holdout_event_dates"]))
    with pytest.raises(ContractError, match="cohort_partition_invalid"):
        build_model_oos_contract(
            conn, as_of_utc="2026-08-02T05:00:00.000Z",
            event_inputs=inputs, cohort_spec=bad,
        )


def test_pr10_terminal_basis_does_not_promote_empty_v13_scope(tmp_path) -> None:
    conn = _database()
    identity = {
        "schema": "bayes_basket_target_canonical_expected_scope_basis_v1",
        "as_of_utc": "2026-08-02T05:00:00Z",
        "scope_type": "MODEL_OOS",
        "expected_keys": [{
            "scope_kind": "BUILDER",
            "natural_key": {
                "station_id": "KATL", "model_id": "AIFS", "ensemble_system_id": None,
                "model_family": "global_deterministic_ai", "provider_id": "OPEN_METEO",
                "provider_product_id": "ecmwf_aifs025_single",
                "independence_family_id": "ECMWF", "source_class": "aggregator_republisher",
                "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                "settlement_timezone": "America/New_York", "lead_bucket": "d0",
                "lead_bucket_scope": "SAME_SETTLEMENT_DAY", "run_cycle": None,
                "run_cycle_scope": "REPUBLISHER_NO_NATIVE_CYCLE",
                "event_oos_protocol_version": "strict_historical_awc_v12_v1",
            },
        }],
        "expected_key_evidence": [], "station_ids": ["KATL"],
        "source_content_sha256s": [], "unresolved_input_receipts": [],
        "canonical_registry_sha256": "a" * 64, "production_no_live": True,
        "writes_database": False, "calls_order_gateway": False, "orders_submitted": 0,
    }
    basis = _seal(identity)
    path = tmp_path / "basis.json"
    write_bounded_json(path, basis)
    terminal = build_pr10_model_oos_terminal_basis(
        conn, expected_basis_path=path, as_of_utc="2026-08-02T05:00:00Z"
    )
    assert terminal["state"] == "BLOCKED"
    assert terminal["blocker_counts"] == {"LINEAGE_MISMATCH": 1}
    assert terminal["consumable"] is False

