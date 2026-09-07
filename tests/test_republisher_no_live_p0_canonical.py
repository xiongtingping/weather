from __future__ import annotations

import copy
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ingress import (
    ingest_canonical_contract,
    stage_republisher_forward_and_p0,
    stage_republisher_no_live_p0,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.materialization import insert_probability_envelope
from bayes_basket_target.production_io import load_materialized_probability_envelopes
import bayes_basket_target.production_io as production_io_module
from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.market_bucket_bridge import build_market_bucket_bridge_contract
from bayes_basket_target.republisher_no_live_p0 import (
    build_distribution_activation_contract,
    build_forward_applicability_contract,
    build_p0_snapshot_contract,
    build_probability_distribution_contract,
)
from bayes_basket_target.republisher_forward_evidence_store import store_manifest
from bayes_basket_target.republisher_no_live_p0_contract import (
    CanonicalRepublisherNoLiveP0Contract,
)
from bayes_basket_target.canonical_typed_scope_builder import (
    build_canonical_typed_scope_envelope,
)
from bayes_basket_target.station_registry import REGISTRY_SHA256
from bayes_basket_target.ssot import apply_production_ssot_schema


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data/bayes_basket_target_ssot.db"
AS_OF = "2026-08-04T09:00:00Z"


def _fixture() -> sqlite3.Connection:
    source = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    target = sqlite3.connect(":memory:")
    target.create_function("canonical_ingress_authorized", 0, lambda: 1)
    assert apply_production_ssot_schema(target)["canonical_contract_schema_ok"] is True
    rows = source.execute(
        "SELECT fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json,ingested_at_utc "
        "FROM canonical_ssot_fact WHERE fact_type IN "
        "('truth','republisher_oos_artifact','republisher_cross_selection',"
        "'republisher_no_live_activation') "
        "AND (fact_type!='republisher_no_live_activation' "
        "OR json_extract(fact_json,'$.activation_generation')=1) "
        # As of AS_OF, and only as of it: seeded from the live SSOT, and a
        # truth revision production committed later makes the selection this
        # copies read stale.  Same repair as the sibling suites.
        "AND ingested_at_utc <= ?",
        (AS_OF,),
    ).fetchall()
    target.executemany("INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)", rows)
    target.commit()
    source.close()
    assert target.execute(
        "SELECT COUNT(*) FROM canonical_ssot_fact "
        "WHERE fact_type='republisher_no_live_activation'"
    ).fetchone()[0] == 2
    return target


def _selection_shas(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT content_sha256 FROM canonical_ssot_fact "
            "WHERE fact_type='republisher_cross_selection' ORDER BY scope_key"
        )
    ]


def _count(conn: sqlite3.Connection, fact_type: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type=?",
            (fact_type,),
        ).fetchone()[0]
    )


def _rehash(packet: dict[str, object]) -> None:
    packet["content_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "content_sha256"}
    )


def _market_bridge_packet(
    *, station_id: str, event_date: str, timezone_name: str
) -> dict[str, object]:
    specs = (
        ("24C or below", None, 24.0),
        ("25C to 27C", 25.0, 27.0),
        ("28C or higher", 28.0, None),
    )
    rows: list[dict[str, object]] = []
    slug = f"highest-temperature-{station_id.lower()}-{event_date}"
    official_sha = "a" * 64
    for ordinal, (outcome, lower, upper) in enumerate(specs):
        yes_token = f"yes-{ordinal}"
        no_token = f"no-{ordinal}"
        rules_sha = format(ordinal + 1, "064x")
        nested = {
            "schema": "reported_trajectory_market_registry_v1",
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "settlement_unit": "C",
            "source_lane": "awc",
            "source_eligible": True,
            "market_active": True,
            "market_accepting_orders": True,
            "outcome": outcome,
            "slug": slug,
            "yes_token": yes_token,
            "no_token": no_token,
            "rules_sha256": rules_sha,
            "market_id": f"market-{ordinal}",
            "condition_id": f"condition-{ordinal}",
            "lower": lower,
            "upper": upper,
            "official_source_registry_sha256": official_sha,
        }
        rows.append(
            {
                "station_id": station_id,
                "event_date": event_date,
                "settlement_timezone": timezone_name,
                "settlement_unit": "C",
                "source_lane": "awc",
                "source_eligible": 1,
                "market_active": 1,
                "market_accepting_orders": 1,
                "outcome": outcome,
                "slug": slug,
                "yes_token": yes_token,
                "no_token": no_token,
                "rules_sha256": rules_sha,
                "registry_json": json.dumps(
                    nested, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            }
        )
    export: dict[str, object] = {
        "schema": "bayes_basket_target_market_registry_snapshot_v1",
        "source_schema": "reported_trajectory_market_registry_v1",
        "source_scope": "shared_live_registry_export_only",
        "exported_at_utc": "2026-08-04T09:00:00Z",
        "row_count": len(rows),
        "rows": rows,
        "forward_p0_catalog_only": True,
        "writes_shared_live_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
    }
    export["snapshot_sha256"] = content_sha256(export)
    return build_market_bucket_bridge_contract(
        export,
        station_id=station_id,
        event_date=event_date,
        as_of_utc="2026-08-04T09:00:04Z",
    )


def _rehash_p0_fact(packet: dict[str, object]) -> None:
    fact = packet["p0_snapshot_facts"][0]
    fact["probability_root_sha256"] = content_sha256(
        {
            "bucket_contract_sha256": fact["bucket_contract_sha256"],
            "market_bucket_bridge_sha256": fact["market_bucket_bridge_sha256"],
            "bucket_intervals": fact["bucket_intervals"],
            "bucket_probabilities": fact["bucket_probabilities"],
        }
    )
    fact["posterior_sha256"] = content_sha256(
        {
            "selection_content_sha256": fact["selection_content_sha256"],
            "forward_applicability_content_sha256": fact[
                "forward_applicability_content_sha256"
            ],
            "probability_distribution_artifact_content_sha256": fact[
                "probability_distribution_artifact_content_sha256"
            ],
            "selected_forward_point_c": fact["selected_forward_point_c"],
            "scenario_temperature_set_sha256": fact[
                "scenario_temperature_set_sha256"
            ],
            "probability_root_sha256": fact["probability_root_sha256"],
        }
    )
    fact["content_sha256"] = content_sha256(
        {key: value for key, value in fact.items() if key != "content_sha256"}
    )
    _rehash(packet)


def _forward_evidence(
    root,
    *,
    station_id: str,
    event_date: str,
    timezone_name: str,
    components: list[dict[str, object]],
    native: list[dict[str, object]],
) -> dict[str, object]:
    plan_identity = {
        "schema": "republisher_native_forward_probe_v1",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
    }
    plan = {**plan_identity, "content_sha256": content_sha256(plan_identity)}
    result_identity = {
        "schema": "republisher_native_forward_probe_result_v1",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "product_results": [
            {
                "upstream_model_id": model,
                "object_receipts": [],
                "native_unavailability_receipt": receipt,
            }
            for model, receipt in zip(("AIFS", "ICON"), native)
        ],
        "fallback_terminal_for_all_products": True,
        "native_complete_for_any_product": False,
        "indeterminate_for_any_product": False,
    }
    result = {
        **result_identity,
        "content_sha256": content_sha256(result_identity),
    }
    captures = {
        model: {
            "raw_body": (model + "-raw").encode(),
            "normalized_curve": {"model": model, "values": [1.0]},
            "component_receipt": receipt,
        }
        for model, receipt in zip(("AIFS", "ICON"), components)
    }
    return store_manifest(
        root,
        station_id=station_id,
        event_date=event_date,
        settlement_timezone=timezone_name,
        pre_probe_plan=plan,
        pre_probe_result=result,
        captures=captures,
        post_probe_plan=plan,
        post_probe_result=result,
    )


def _receipt(identity: dict[str, object]) -> dict[str, object]:
    return {**identity, "receipt_content_sha256": content_sha256(identity)}


def _dynamic_chain(
    conn: sqlite3.Connection, station_id: str = "CYYZ"
) -> tuple[str, str]:
    distribution_packet = build_probability_distribution_contract(
        conn,
        selection_content_sha256s=_selection_shas(conn),
        as_of_utc=AS_OF,
    )
    ingest_canonical_contract(
        conn, distribution_packet, commit=True, ingested_at_utc=AS_OF
    )
    distribution_shas = [
        str(row[0])
        for row in conn.execute(
            "SELECT content_sha256 FROM canonical_ssot_fact "
            "WHERE fact_type='probability_distribution_artifact' ORDER BY scope_key"
        )
    ]
    activation_packet = build_distribution_activation_contract(
        conn,
        distribution_content_sha256s=distribution_shas,
        as_of_utc=AS_OF,
    )
    ingest_canonical_contract(
        conn, activation_packet, commit=True, ingested_at_utc=AS_OF
    )
    activation_sha = str(
        conn.execute(
            "SELECT content_sha256 FROM canonical_ssot_fact "
            "WHERE fact_type='republisher_no_live_activation' "
            "AND json_extract(fact_json,'$.station_id')=? "
            "AND json_extract(fact_json,'$.activation_generation')=2",
            (station_id,),
        ).fetchone()[0]
    )
    distribution_sha = str(
        conn.execute(
            "SELECT content_sha256 FROM canonical_ssot_fact "
            "WHERE fact_type='probability_distribution_artifact' "
            "AND json_extract(fact_json,'$.station_id')=?",
            (station_id,),
        ).fetchone()[0]
    )
    return activation_sha, distribution_sha


def _forward_receipts(
    *, station_id: str, event_date: str, timezone_name: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    components: list[dict[str, object]] = []
    coordinates = {
        "CYYZ": (43.679, -79.629),
        "NZWN": (-41.331, 174.806),
    }[station_id]
    for index, (model, product, family, value) in enumerate(
        (
            ("AIFS", "ecmwf_aifs025_single", "ECMWF", "27.500000000000"),
            ("ICON", "dwd_icon_global", "DWD", "26.500000000000"),
        )
    ):
        identity = {
            "upstream_model_id": model,
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "station_coordinate_registry_sha256": REGISTRY_SHA256,
            "republisher_product_id": product,
            "independence_family_id": family,
            "request_url": (
                "https://api.open-meteo.com/v1/forecast?"
                f"latitude={coordinates[0]:.6f}&longitude={coordinates[1]:.6f}"
                f"&hourly=temperature_2m&models={product}"
                f"&start_date={event_date}&end_date={event_date}"
                f"&timezone={timezone_name}"
            ),
            "http_status": 200,
            "requested_at_utc": f"2026-08-04T09:00:0{index}Z",
            "response_started_at_utc": f"2026-08-04T09:00:0{index + 1}Z",
            "response_completed_at_utc": f"2026-08-04T09:00:0{index + 2}Z",
            "raw_object_sha256": ("a" if model == "AIFS" else "b") * 64,
            "requested_latitude": f"{coordinates[0]:.12f}",
            "requested_longitude": f"{coordinates[1]:.12f}",
            "response_latitude": f"{coordinates[0]:.12f}",
            "response_longitude": f"{coordinates[1]:.12f}",
            "hourly_valid_time_count": 24,
            "hourly_curve_content_sha256": ("7" if model == "AIFS" else "8") * 64,
            "normalized_content_sha256": ("c" if model == "AIFS" else "d") * 64,
            "forecast_daily_max_c": value,
        }
        components.append(_receipt(identity))
    station_scope_sha = content_sha256(
        {
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
        }
    )
    native: list[dict[str, object]] = []
    for model, product, provider, marker in (
        ("AIFS", "ECMWF_AIFS_SINGLE_0P25", "ecmwf_aifs025_official", "e"),
        ("ICON", "DWD_ICON_GLOBAL", "icon_global_official", "f"),
    ):
        identity = {
            "upstream_model_id": model,
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": timezone_name,
            "station_scope_sha256": station_scope_sha,
            "native_provider_id": provider,
            "native_product_id": product,
            "candidate_cycle_policy_sha256": "1" * 64,
            "wait_deadline_policy_sha256": "2" * 64,
            "checked_at_utc": "2026-08-04T09:00:03Z",
            "expires_at_utc": "2026-08-04T09:05:03Z",
            "terminal_status": "NATIVE_UNAVAILABLE",
            "required_object_count": 24,
            "terminal_object_receipt_set_sha256": marker * 64,
        }
        native.append(_receipt(identity))
    return components, native


def _ready_p0_packet(
    conn: sqlite3.Connection, evidence_root: Path
) -> dict[str, object]:
    activation_sha, _ = _dynamic_chain(conn)
    components, native = _forward_receipts(
        station_id="CYYZ",
        event_date="2026-08-04",
        timezone_name="America/Toronto",
    )
    evidence = _forward_evidence(
        evidence_root,
        station_id="CYYZ",
        event_date="2026-08-04",
        timezone_name="America/Toronto",
        components=components,
        native=native,
    )
    forward_packet = build_forward_applicability_contract(
        conn,
        activation_content_sha256=activation_sha,
        station_id="CYYZ",
        event_date="2026-08-04",
        decision_at_utc="2026-08-04T09:00:04Z",
        component_receipts=components,
        native_unavailability_receipts=native,
        evidence_manifest_content_sha256=evidence["manifest_content_sha256"],
        evidence_manifest_content_address=evidence["manifest_content_address"],
        fresh_until_utc="2026-08-04T15:00:03Z",
        native_evidence_expires_at_utc="2026-08-04T09:05:03Z",
        as_of_utc="2026-08-04T09:00:04Z",
    )
    ingest_canonical_contract(
        conn,
        forward_packet,
        commit=True,
        ingested_at_utc="2026-08-04T09:00:04Z",
        external_evidence_root=evidence_root,
    )
    bridge_packet = _market_bridge_packet(
        station_id="CYYZ",
        event_date="2026-08-04",
        timezone_name="America/Toronto",
    )
    ingest_canonical_contract(
        conn,
        bridge_packet,
        commit=True,
        ingested_at_utc="2026-08-04T09:00:04Z",
    )
    bridge = bridge_packet["bridge_facts"][0]
    intervals = [
        {
            "bucket_id": row["outcome"],
            "ordinal": row["ordinal"],
            "lower_c": row["lower_c"],
            "upper_c": row["upper_c"],
        }
        for row in bridge["bucket_contracts"]
    ]
    return build_p0_snapshot_contract(
        conn,
        forward_applicability_content_sha256=forward_packet[
            "forward_applicability_facts"
        ][0]["content_sha256"],
        bucket_intervals=intervals,
        market_bucket_bridge_sha256=bridge["content_sha256"],
        materialized_at_utc="2026-08-04T09:00:05Z",
        as_of_utc="2026-08-04T09:00:05Z",
    )


@pytest.mark.skip(reason="V13 retires legacy republisher P0 ingress")
def test_train_only_distribution_is_reproducible_and_beats_climatology() -> None:
    conn = _fixture()
    try:
        packet = build_probability_distribution_contract(
            conn,
            selection_content_sha256s=_selection_shas(conn),
            as_of_utc=AS_OF,
        )
        parsed = CanonicalRepublisherNoLiveP0Contract.from_mapping(packet)
        by_station = {
            item.raw["station_id"]: item
            for item in parsed.probability_distribution_artifact_facts
        }
        assert set(by_station) == {"CYYZ", "NZWN"}
        assert by_station["CYYZ"].raw["selected_holdout_mean_crps_c"] == "0.300794581158"
        assert by_station["CYYZ"].raw["climatology_holdout_mean_crps_c"] == "1.440000000000"
        assert by_station["NZWN"].raw["selected_holdout_mean_crps_c"] == "0.436729795937"
        assert by_station["NZWN"].raw["climatology_holdout_mean_crps_c"] == "0.900000000000"
        assert all(len(item.raw["train_scenarios"]) == 20 for item in by_station.values())
        assert all(item.raw["train_residual_mean_c"] == "0.000000000000" for item in by_station.values())

        preview = ingest_canonical_contract(conn, packet, commit=False)
        assert preview["inserted_count"] == 0
        committed = ingest_canonical_contract(
            conn, packet, commit=True, ingested_at_utc=AS_OF
        )
        assert committed["inserted_count"] == 2
        assert _count(conn, "probability_distribution_artifact") == 2
        replay = ingest_canonical_contract(
            conn, packet, commit=True, ingested_at_utc=AS_OF
        )
        assert replay["inserted_count"] == 0
        assert replay["replayed_count"] == 2
    finally:
        conn.close()


def test_v13_ingress_rejects_legacy_republisher_p0_revival() -> None:
    conn = _fixture()
    try:
        packet = build_probability_distribution_contract(
            conn,
            selection_content_sha256s=_selection_shas(conn),
            as_of_utc=AS_OF,
        )
        with pytest.raises(
            ContractError, match="canonical_legacy_republisher_contract_retired"
        ):
            ingest_canonical_contract(conn, packet, commit=False)
    finally:
        conn.close()


@pytest.mark.skip(reason="V13 retires legacy republisher P0 ingress")
def test_distribution_gen2_activation_binds_exact_artifact() -> None:
    conn = _fixture()
    try:
        packet = build_probability_distribution_contract(
            conn,
            selection_content_sha256s=_selection_shas(conn),
            as_of_utc=AS_OF,
        )
        ingest_canonical_contract(conn, packet, commit=True, ingested_at_utc=AS_OF)
        distribution_shas = [
            str(item[0])
            for item in conn.execute(
                "SELECT content_sha256 FROM canonical_ssot_fact "
                "WHERE fact_type='probability_distribution_artifact' ORDER BY scope_key"
            )
        ]
        activation_packet = build_distribution_activation_contract(
            conn,
            distribution_content_sha256s=distribution_shas,
            as_of_utc=AS_OF,
        )
        preview = ingest_canonical_contract(conn, activation_packet, commit=False)
        assert preview["inserted_count"] == 0
        committed = ingest_canonical_contract(
            conn, activation_packet, commit=True, ingested_at_utc=AS_OF
        )
        assert committed["inserted_count"] == 2
        rows = conn.execute(
            "SELECT fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='republisher_no_live_activation' ORDER BY scope_key"
        ).fetchall()
        parsed = [json.loads(str(row[0])) for row in rows]
        gen2 = [item for item in parsed if item["activation_generation"] == 2]
        assert len(gen2) == 2
        assert {
            item["temperature_distribution_artifact_content_sha256"]
            for item in gen2
        } == set(distribution_shas)
    finally:
        conn.close()


def test_holdout_metric_tamper_and_future_authority_fail_zero_write() -> None:
    conn = _fixture()
    try:
        packet = build_probability_distribution_contract(
            conn,
            selection_content_sha256s=_selection_shas(conn),
            as_of_utc=AS_OF,
        )
        bad = copy.deepcopy(packet)
        facts = bad["probability_distribution_artifact_facts"]
        assert isinstance(facts, list) and isinstance(facts[0], dict)
        facts[0]["selected_holdout_mean_crps_c"] = "9.000000000000"
        facts[0]["content_sha256"] = content_sha256(
            {key: value for key, value in facts[0].items() if key != "content_sha256"}
        )
        _rehash(bad)
        before = _count(conn, "probability_distribution_artifact")
        with pytest.raises(ContractError, match="holdout_gate_failed"):
            ingest_canonical_contract(conn, bad, commit=True)
        assert _count(conn, "probability_distribution_artifact") == before

        bad = copy.deepcopy(packet)
        facts = bad["probability_distribution_artifact_facts"]
        assert isinstance(facts, list) and isinstance(facts[0], dict)
        facts[0]["formal_live_authority"] = True
        facts[0]["content_sha256"] = content_sha256(
            {key: value for key, value in facts[0].items() if key != "content_sha256"}
        )
        _rehash(bad)
        with pytest.raises(ContractError, match="formal_live_authority_invalid"):
            ingest_canonical_contract(conn, bad, commit=True)
        assert _count(conn, "probability_distribution_artifact") == before
    finally:
        conn.close()


@pytest.mark.skip(reason="V13 retires legacy republisher P0 ingress")
def test_forward_applicability_ingress_and_opaque_bridge_blocks_p0(tmp_path) -> None:
    conn = _fixture()
    try:
        activation_sha, _ = _dynamic_chain(conn)
        components, native = _forward_receipts(
            station_id="CYYZ",
            event_date="2026-08-04",
            timezone_name="America/Toronto",
        )
        evidence = _forward_evidence(
            tmp_path,
            station_id="CYYZ",
            event_date="2026-08-04",
            timezone_name="America/Toronto",
            components=components,
            native=native,
        )
        forward_packet = build_forward_applicability_contract(
            conn,
            activation_content_sha256=activation_sha,
            station_id="CYYZ",
            event_date="2026-08-04",
            decision_at_utc="2026-08-04T09:00:04Z",
            component_receipts=components,
            native_unavailability_receipts=native,
            evidence_manifest_content_sha256=evidence["manifest_content_sha256"],
            evidence_manifest_content_address=evidence["manifest_content_address"],
            fresh_until_utc="2026-08-04T15:00:03Z",
            native_evidence_expires_at_utc="2026-08-04T09:05:03Z",
            as_of_utc="2026-08-04T09:00:04Z",
        )
        with pytest.raises(ContractError, match="forward_evidence_root_required"):
            ingest_canonical_contract(
                conn,
                forward_packet,
                commit=True,
                ingested_at_utc="2026-08-04T09:00:04Z",
            )
        assert _count(conn, "forward_applicability") == 0
        committed = ingest_canonical_contract(
            conn,
            forward_packet,
            commit=True,
            ingested_at_utc="2026-08-04T09:00:04Z",
            external_evidence_root=tmp_path,
        )
        assert committed["inserted_count"] == 1
        applicability_sha = str(
            conn.execute(
                "SELECT content_sha256 FROM canonical_ssot_fact "
                "WHERE fact_type='forward_applicability'"
            ).fetchone()[0]
        )
        intervals = [
            {"bucket_id": "lt25", "ordinal": 0, "lower_c": None, "upper_c": "25.000000000000"},
            {"bucket_id": "25to28", "ordinal": 1, "lower_c": "25.000000000000", "upper_c": "28.000000000000"},
            {"bucket_id": "ge28", "ordinal": 2, "lower_c": "28.000000000000", "upper_c": None},
        ]
        with pytest.raises(ContractError, match="market_bucket_bridge_missing"):
            build_p0_snapshot_contract(
                conn,
                forward_applicability_content_sha256=applicability_sha,
                bucket_intervals=intervals,
                market_bucket_bridge_sha256="9" * 64,
                materialized_at_utc="2026-08-04T09:00:05Z",
                as_of_utc="2026-08-04T09:00:05Z",
            )
        assert _count(conn, "p0_snapshot") == 0

        bridge_packet = _market_bridge_packet(
            station_id="CYYZ",
            event_date="2026-08-04",
            timezone_name="America/Toronto",
        )
        ingest_canonical_contract(
            conn,
            bridge_packet,
            commit=True,
            ingested_at_utc="2026-08-04T09:00:04Z",
        )
        bridge = bridge_packet["bridge_facts"][0]
        bridge_intervals = [
            {
                "bucket_id": row["outcome"],
                "ordinal": row["ordinal"],
                "lower_c": row["lower_c"],
                "upper_c": row["upper_c"],
            }
            for row in bridge["bucket_contracts"]
        ]
        p0_packet = build_p0_snapshot_contract(
            conn,
            forward_applicability_content_sha256=applicability_sha,
            bucket_intervals=bridge_intervals,
            market_bucket_bridge_sha256=bridge["content_sha256"],
            materialized_at_utc="2026-08-04T09:00:05Z",
            as_of_utc="2026-08-04T09:00:05Z",
        )
        bad = copy.deepcopy(p0_packet)
        bad["p0_snapshot_facts"][0]["market_bucket_bridge_sha256"] = "9" * 64
        _rehash_p0_fact(bad)
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ContractError, match="market_bucket_bridge_missing"):
            stage_republisher_no_live_p0(
                conn,
                bad,
                external_evidence_root=tmp_path,
                created_at_utc="2026-08-04T09:00:05Z",
            )
        conn.rollback()
        assert _count(conn, "p0_snapshot") == 0

        wrong_bridge_packet = _market_bridge_packet(
            station_id="CYYZ",
            event_date="2026-08-05",
            timezone_name="America/Toronto",
        )
        ingest_canonical_contract(
            conn,
            wrong_bridge_packet,
            commit=True,
            ingested_at_utc="2026-08-04T09:00:04Z",
        )
        bad_scope = copy.deepcopy(p0_packet)
        bad_scope["p0_snapshot_facts"][0]["market_bucket_bridge_sha256"] = (
            wrong_bridge_packet["bridge_facts"][0]["content_sha256"]
        )
        _rehash_p0_fact(bad_scope)
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ContractError, match="bridge_binding_mismatch"):
            stage_republisher_no_live_p0(
                conn,
                bad_scope,
                external_evidence_root=tmp_path,
                created_at_utc="2026-08-04T09:00:05Z",
            )
        conn.rollback()
        assert _count(conn, "p0_snapshot") == 0

        with pytest.raises(ContractError, match="atomic_projection_required"):
            ingest_canonical_contract(
                conn,
                p0_packet,
                commit=True,
                ingested_at_utc="2026-08-04T09:00:05Z",
            )
        assert _count(conn, "p0_snapshot") == 0
        assert _count(conn, "native_arrival") == 0
        assert _count(conn, "activation") == 0
    finally:
        conn.close()


@pytest.mark.skip(reason="V13 retires legacy republisher P0 ingress")
def test_native_complete_or_wrong_forward_product_fail_before_write(tmp_path) -> None:
    conn = _fixture()
    try:
        activation_sha, _ = _dynamic_chain(conn)
        components, native = _forward_receipts(
            station_id="CYYZ",
            event_date="2026-08-04",
            timezone_name="America/Toronto",
        )
        native[0]["terminal_status"] = "NATIVE_COMPLETE"
        native[0]["receipt_content_sha256"] = content_sha256(
            {
                key: value
                for key, value in native[0].items()
                if key != "receipt_content_sha256"
            }
        )
        evidence = _forward_evidence(
            tmp_path / "native-complete",
            station_id="CYYZ",
            event_date="2026-08-04",
            timezone_name="America/Toronto",
            components=components,
            native=native,
        )
        with pytest.raises(ContractError, match="native_receipt_status_invalid"):
            build_forward_applicability_contract(
                conn,
                activation_content_sha256=activation_sha,
                station_id="CYYZ",
                event_date="2026-08-04",
                decision_at_utc="2026-08-04T09:00:04Z",
                component_receipts=components,
                native_unavailability_receipts=native,
                evidence_manifest_content_sha256=evidence["manifest_content_sha256"],
                evidence_manifest_content_address=evidence["manifest_content_address"],
                fresh_until_utc="2026-08-04T15:00:03Z",
                native_evidence_expires_at_utc="2026-08-04T09:05:03Z",
                as_of_utc="2026-08-04T09:00:04Z",
            )
        assert _count(conn, "forward_applicability") == 0

        components, native = _forward_receipts(
            station_id="CYYZ",
            event_date="2026-08-04",
            timezone_name="America/Toronto",
        )
        components[0]["republisher_product_id"] = "ecmwf_aifs025_ensemble"
        components[0]["receipt_content_sha256"] = content_sha256(
            {
                key: value
                for key, value in components[0].items()
                if key != "receipt_content_sha256"
            }
        )
        evidence = _forward_evidence(
            tmp_path / "wrong-product",
            station_id="CYYZ",
            event_date="2026-08-04",
            timezone_name="America/Toronto",
            components=components,
            native=native,
        )
        with pytest.raises(ContractError, match="forward_component_product_invalid"):
            build_forward_applicability_contract(
                conn,
                activation_content_sha256=activation_sha,
                station_id="CYYZ",
                event_date="2026-08-04",
                decision_at_utc="2026-08-04T09:00:04Z",
                component_receipts=components,
                native_unavailability_receipts=native,
                evidence_manifest_content_sha256=evidence["manifest_content_sha256"],
                evidence_manifest_content_address=evidence["manifest_content_address"],
                fresh_until_utc="2026-08-04T15:00:03Z",
                native_evidence_expires_at_utc="2026-08-04T09:05:03Z",
                as_of_utc="2026-08-04T09:00:04Z",
            )
        assert _count(conn, "forward_applicability") == 0
    finally:
        conn.close()


@pytest.mark.skip(reason="V13 retires legacy republisher P0 ingress")
def test_forward_and_p0_stage_as_one_unpublished_outer_transaction(
    tmp_path: Path,
) -> None:
    conn = _fixture()
    try:
        activation_sha, _ = _dynamic_chain(conn)
        components, native = _forward_receipts(
            station_id="CYYZ",
            event_date="2026-08-04",
            timezone_name="America/Toronto",
        )
        evidence = _forward_evidence(
            tmp_path / "evidence",
            station_id="CYYZ",
            event_date="2026-08-04",
            timezone_name="America/Toronto",
            components=components,
            native=native,
        )
        forward_packet = build_forward_applicability_contract(
            conn,
            activation_content_sha256=activation_sha,
            station_id="CYYZ",
            event_date="2026-08-04",
            decision_at_utc="2026-08-04T09:00:04Z",
            component_receipts=components,
            native_unavailability_receipts=native,
            evidence_manifest_content_sha256=evidence["manifest_content_sha256"],
            evidence_manifest_content_address=evidence["manifest_content_address"],
            fresh_until_utc="2026-08-04T15:00:03Z",
            native_evidence_expires_at_utc="2026-08-04T09:05:03Z",
            as_of_utc="2026-08-04T09:00:04Z",
        )
        bridge_packet = _market_bridge_packet(
            station_id="CYYZ",
            event_date="2026-08-04",
            timezone_name="America/Toronto",
        )
        ingest_canonical_contract(
            conn,
            bridge_packet,
            commit=True,
            ingested_at_utc="2026-08-04T09:00:04Z",
        )
        bridge = bridge_packet["bridge_facts"][0]
        intervals = [
            {
                "bucket_id": row["outcome"],
                "ordinal": row["ordinal"],
                "lower_c": row["lower_c"],
                "upper_c": row["upper_c"],
            }
            for row in bridge["bucket_contracts"]
        ]

        with pytest.raises(ContractError, match="outer_transaction_required"):
            stage_republisher_forward_and_p0(
                conn,
                forward_packet,
                bucket_intervals=intervals,
                market_bucket_bridge_sha256=bridge["content_sha256"],
                external_evidence_root=tmp_path / "evidence",
                created_at_utc="2026-08-04T09:00:05Z",
                as_of_utc="2026-08-04T09:00:05Z",
            )

        for fault in ("after_forward", "after_p0"):
            conn.execute("BEGIN IMMEDIATE")
            with pytest.raises(RuntimeError, match=fault):
                stage_republisher_forward_and_p0(
                    conn,
                    forward_packet,
                    bucket_intervals=intervals,
                    market_bucket_bridge_sha256=bridge["content_sha256"],
                    external_evidence_root=tmp_path / "evidence",
                    created_at_utc="2026-08-04T09:00:05Z",
                    as_of_utc="2026-08-04T09:00:05Z",
                    inject_fault_at=fault,
                )
            assert _count(conn, "forward_applicability") == 0
            assert _count(conn, "p0_snapshot") == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM basket_probability_envelope"
            ).fetchone()[0] == 0
            conn.rollback()

        conn.execute("BEGIN IMMEDIATE")
        staged = stage_republisher_forward_and_p0(
            conn,
            forward_packet,
            bucket_intervals=intervals,
            market_bucket_bridge_sha256=bridge["content_sha256"],
            external_evidence_root=tmp_path / "evidence",
            created_at_utc="2026-08-04T09:00:05Z",
            as_of_utc="2026-08-04T09:00:05Z",
        )
        assert staged["release_state"] == "MATERIALIZED_UNPUBLISHED"
        assert staged["authority_scope"] == "PRODUCTION_NO_LIVE_ONLY"
        assert _count(conn, "forward_applicability") == 1
        assert _count(conn, "p0_snapshot") == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0] == 1
        conn.commit()
        assert _count(conn, "forward_applicability") == 1
        assert _count(conn, "p0_snapshot") == 1
        assert staged["calls_order_gateway"] is False
        assert staged["orders_submitted"] == 0
    finally:
        conn.close()


@pytest.mark.skip(reason="V13 retires legacy republisher P0 ingress")
def test_canonical_p0_and_probability_envelope_are_one_atomic_unpublished_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _fixture()
    try:
        packet = _ready_p0_packet(conn, tmp_path / "evidence")
        with pytest.raises(ContractError, match="outer_transaction_required"):
            stage_republisher_no_live_p0(
                conn,
                packet,
                external_evidence_root=tmp_path / "evidence",
                created_at_utc="2026-08-04T09:00:05Z",
            )

        for fault in ("after_canonical", "after_envelope"):
            conn.execute("BEGIN IMMEDIATE")
            with pytest.raises(RuntimeError, match=fault):
                stage_republisher_no_live_p0(
                    conn,
                    packet,
                    external_evidence_root=tmp_path / "evidence",
                    created_at_utc="2026-08-04T09:00:05Z",
                    inject_fault_at=fault,
                )
            assert _count(conn, "p0_snapshot") == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM basket_probability_envelope"
            ).fetchone()[0] == 0
            conn.rollback()

        conn.execute("BEGIN IMMEDIATE")
        staged = stage_republisher_no_live_p0(
            conn,
            packet,
            external_evidence_root=tmp_path / "evidence",
            created_at_utc="2026-08-04T09:00:05Z",
        )
        assert staged["state"] == "STAGED_IN_CALLER_TRANSACTION"
        assert _count(conn, "p0_snapshot") == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0] == 1
        conn.rollback()
        assert _count(conn, "p0_snapshot") == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0] == 0

        conn.execute("BEGIN IMMEDIATE")
        stage_republisher_no_live_p0(
            conn,
            packet,
            external_evidence_root=tmp_path / "evidence",
            created_at_utc="2026-08-04T09:00:05Z",
        )
        conn.commit()
        assert _count(conn, "p0_snapshot") == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0] == 1

        conn.execute("BEGIN IMMEDIATE")
        replay = stage_republisher_no_live_p0(
            conn,
            packet,
            external_evidence_root=tmp_path / "evidence",
            created_at_utc="2026-08-04T09:00:05Z",
        )
        conn.commit()
        assert replay["canonical_ingress_receipt"]["replayed_count"] == 1
        assert replay["probability_envelope_receipt"]["state"] == "ALREADY_COMMITTED"

        with pytest.raises(ContractError, match="materialized_probability_envelope_missing"):
            load_materialized_probability_envelopes(
                conn,
                station_id="CYYZ",
                event_date="2026-08-04",
                evaluation_at_utc="2026-08-04T09:00:06Z",
                required_probability_pathway=None,
            )

        current_pointer = tmp_path / "current.json"
        current_pointer.write_text("{}", encoding="utf-8")
        authorized: list[dict] = []
        monkeypatch.setattr(
            production_io_module,
            "verify_released_p0_envelope_authority",
            lambda pointer, **kwargs: authorized.append(
                {"pointer": pointer, **kwargs}
            ) or {"production_no_live": True},
        )
        released = load_materialized_probability_envelopes(
            conn,
            station_id="CYYZ",
            event_date="2026-08-04",
            evaluation_at_utc="2026-08-04T09:00:06Z",
            required_probability_pathway=None,
            canonical_current_pointer=current_pointer,
            canonical_release_root=tmp_path / "releases",
        )
        assert len(released) == 1
        assert authorized[0]["envelope"]["stage"] == "P0"

        row = conn.execute(
            "SELECT stage,checkpoint_id,station_id,event_date,settlement_timezone,"
            "decision_at_utc,fresh_until_utc,bucket_ordinals_json,probabilities_json,"
            "posterior_version,posterior_sha256,bridge_sha256,lineage_json "
            "FROM basket_probability_envelope"
        ).fetchone()
        envelope = ProbabilityEnvelope.from_mapping(
            {
                "stage": row[0],
                "checkpoint_id": row[1],
                "station_id": row[2],
                "event_date": row[3],
                "settlement_timezone": row[4],
                "decision_at_utc": row[5],
                "fresh_until_utc": row[6],
                "bucket_ordinals": json.loads(row[7]),
                "probabilities": json.loads(row[8]),
                "posterior_version": row[9],
                "posterior_sha256": row[10],
                "bridge_sha256": row[11],
                "lineage": json.loads(row[12]),
            }
        )
        with pytest.raises(ContractError, match="direct_envelope_writer_forbidden"):
            insert_probability_envelope(
                conn, envelope, created_at_utc="2026-08-04T09:00:06Z"
            )
    finally:
        conn.close()


@pytest.mark.skip(reason="V13 retires legacy republisher P0 ingress")
def test_typed_p0_scopes_bind_selection_sets_and_keep_generation_out_of_key(
    tmp_path: Path,
) -> None:
    """PR04.C must not turn a generation or SHA into a substitute P0 scope."""

    conn = _fixture()
    try:
        packet = _ready_p0_packet(conn, tmp_path / "evidence")
        conn.execute("BEGIN IMMEDIATE")
        stage_republisher_no_live_p0(
            conn,
            packet,
            external_evidence_root=tmp_path / "evidence",
            created_at_utc="2026-08-04T09:00:05Z",
        )
        conn.commit()

        disk_path = tmp_path / "typed-p0.db"
        with sqlite3.connect(disk_path) as disk:
            conn.backup(disk)

        activation_envelope, activation_receipt = (
            build_canonical_typed_scope_envelope(
                disk_path,
                scope_type="P0_ACTIVATION",
                as_of_utc="2026-08-04T09:01:00Z",
            )
        )
        assert activation_envelope is not None
        assert activation_receipt["state"] == "ACTIVATION_READY"
        activation = next(
            item
            for item in activation_envelope["records"]
            if item["natural_key"]["station_id"] == "CYYZ"
        )
        assert set(activation["natural_key"]) == {
            "station_id",
            "lead_bucket",
            "run_cycle",
            "run_cycle_scope",
            "run_cycle_vector_sha256",
            "authority_scope",
            "model_family",
            "available_system_set_sha256",
            "independence_family_set_sha256",
        }
        assert activation["natural_key"]["run_cycle"] is None
        assert activation["natural_key"]["authority_scope"] == "PRODUCTION_NO_LIVE_ONLY"
        assert activation["version"] == {"activation_generation": 2}
        assert "activation_generation" not in activation["natural_key"]

        snapshot_envelope, snapshot_receipt = build_canonical_typed_scope_envelope(
            disk_path,
            scope_type="P0_SNAPSHOT",
            as_of_utc="2026-08-04T09:01:00Z",
        )
        assert snapshot_envelope is not None
        assert snapshot_receipt["state"] == "ACTIVATION_READY"
        snapshot = snapshot_envelope["records"][0]
        assert snapshot["natural_key"]["station_id"] == "CYYZ"
        assert snapshot["natural_key"]["lead_bucket"] == "d0"
        assert snapshot["natural_key"]["run_cycle"] is None
        assert snapshot["natural_key"]["run_cycle_vector_sha256"] == activation[
            "natural_key"
        ]["run_cycle_vector_sha256"]
        assert snapshot["natural_key"]["available_system_set_sha256"] == activation[
            "natural_key"
        ]["available_system_set_sha256"]
        assert snapshot["natural_key"]["independence_family_set_sha256"] == activation[
            "natural_key"
        ]["independence_family_set_sha256"]
        assert snapshot["version"] == {"activation_generation": 2}
        assert "activation_generation" not in snapshot["natural_key"]

        stale_envelope, stale_receipt = build_canonical_typed_scope_envelope(
            disk_path,
            scope_type="P0_SNAPSHOT",
            as_of_utc="2026-08-04T15:00:03Z",
        )
        assert stale_envelope is not None
        assert stale_receipt["state"] == "BLOCKED"
        assert stale_envelope["records"][0]["state"] == "BLOCKED"
        assert stale_envelope["records"][0]["blockers"] == ["DATA_MISSING"]

        # A p0 fact without its exact materialized ProbabilityEnvelope has no
        # canonical snapshot identity.  Build a minimal read-only-shaped copy
        # rather than violating append-only triggers on the successful fixture.
        broken_path = tmp_path / "typed-p0-missing-envelope.db"
        with sqlite3.connect(disk_path) as source, sqlite3.connect(broken_path) as broken:
            broken.executescript(
                """
                CREATE TABLE canonical_ssot_fact(
                  fact_id TEXT PRIMARY KEY,fact_type TEXT,scope_key TEXT,
                  content_sha256 TEXT,fact_json TEXT,lineage_json TEXT,
                  ingested_at_utc TEXT);
                CREATE TABLE basket_probability_envelope(
                  envelope_id TEXT PRIMARY KEY,stage TEXT,checkpoint_id TEXT,
                  station_id TEXT,event_date TEXT,settlement_timezone TEXT,
                  decision_at_utc TEXT,fresh_until_utc TEXT,bucket_ordinals_json TEXT,
                  probabilities_json TEXT,posterior_version TEXT,posterior_sha256 TEXT,
                  bridge_sha256 TEXT,lineage_json TEXT,content_sha256 TEXT,
                  state TEXT,created_at TEXT);
                """
            )
            rows = source.execute(
                "SELECT fact_id,fact_type,scope_key,content_sha256,fact_json,"
                "lineage_json,ingested_at_utc FROM canonical_ssot_fact"
            ).fetchall()
            broken.executemany(
                "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)", rows
            )
        blocked_envelope, blocked_receipt = build_canonical_typed_scope_envelope(
            broken_path,
            scope_type="P0_SNAPSHOT",
            as_of_utc="2026-08-04T09:01:00Z",
        )
        assert blocked_envelope is None
        assert blocked_receipt["state"] == "BLOCKED"
        assert blocked_receipt["blockers"] == ["LINEAGE_MISMATCH"]
        assert blocked_receipt["failure_code"] == "p0_snapshot_probability_envelope_missing"
        assert blocked_receipt["unproven_natural_key_fields"]
        assert blocked_receipt["record_count"] == 0
    finally:
        conn.close()
