from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.p0_producer import bucket_intervals_from_registry_rows
from bayes_basket_target.resource_bounds import bounded_json_bytes


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/materialize_native_ensemble_scenario_roles.py"
SPEC = importlib.util.spec_from_file_location("materialize_scenario_roles", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


INTERVALS = {
    "24°C or below": [None, 24.5],
    "25°C": [24.5, 25.5],
    "26°C": [25.5, 26.5],
    "27°C": [26.5, 27.5],
    "28°C or higher": [27.5, None],
}


def _registry_row(station: str, event_date: str, outcome: str, index: int) -> dict[str, object]:
    lower = INTERVALS[outcome][0]
    upper = INTERVALS[outcome][1]
    if outcome == "24°C or below":
        registry_lower, registry_upper = None, 24
    elif outcome == "28°C or higher":
        registry_lower, registry_upper = 28, None
    else:
        value = int(outcome[:2])
        registry_lower, registry_upper = value, value
    registry = {
        "market_id": f"market-{station}-{index}",
        "condition_id": f"condition-{station}",
        "outcome": outcome,
        "lower": registry_lower,
        "upper": registry_upper,
        "settlement_timezone": "America/Los_Angeles",
    }
    return {
        "slug": f"highest-temperature-{station.lower()}-{event_date}",
        "event_date": event_date,
        "outcome": outcome,
        "city_slug": station.lower(),
        "station_id": station,
        "settlement_timezone": "America/Los_Angeles",
        "settlement_unit": "C",
        "source_lane": "awc",
        "source_eligible": 1,
        "source_block_reason": "",
        "resolution_url": "https://example.invalid",
        "rules_sha256": "a" * 64,
        "yes_token": f"yes-{station}-{index}",
        "no_token": f"no-{station}-{index}",
        "market_active": 1,
        "market_accepting_orders": 1,
        "registry_json": json.dumps(registry, sort_keys=True),
        "refreshed_at": "2026-07-31T00:00:00Z",
        "_lower": lower,
        "_upper": upper,
    }


def _registry(stations: list[str], event_date: str = "2026-08-01") -> dict[str, object]:
    rows = [
        _registry_row(station, event_date, outcome, index)
        for station in stations
        for index, outcome in enumerate(INTERVALS)
    ]
    return {
        "schema": "bayes_basket_target_market_registry_snapshot_v1",
        "snapshot_sha256": "b" * 64,
        "rows": rows,
        "row_count": len(rows),
    }


def _registry_export(stations: list[str]) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "bayes_basket_target_market_registry_snapshot_v1",
        "source_schema": "reported_trajectory_market_registry_v1",
        "source_scope": "shared_live_registry_export_only",
        "source_database_path": "/tmp/target-registry-source.db",
        "exported_at_utc": "2026-07-31T00:00:00Z",
        "source_row_count": len(stations) * len(INTERVALS),
        "row_count": len(stations) * len(INTERVALS),
        "rows": _registry(stations)["rows"],
        "stable_scope_sha256": "c" * 64,
        "writes_shared_live_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "forward_p0_catalog_only": True,
    }
    return {**payload, "snapshot_sha256": content_sha256(payload)}


def _members(values: list[float]) -> list[dict[str, object]]:
    return [
        {
            "member_id": f"m{index}",
            "member_role": "PERTURBED",
            "raw_tmax_c": value,
        }
        for index, value in enumerate(values)
    ]


def _packet(
    station: str = "TEST",
    event_date: str = "2026-08-01",
    *,
    official: bool = False,
    no_consensus: bool = False,
) -> dict[str, object]:
    if no_consensus:
        ecmwf = [25.1] * 10
        icon = [27.1] * 10
    else:
        ecmwf = [28.1] * 6 + [26.1] * 4
        icon = [25.1] * 6 + [26.1] * 4
    systems = [
        {
            "ensemble_system_id": "ECMWF_IFS",
            "independence_family_id": "ECMWF",
            "members": _members(ecmwf),
            "member_set_sha256": "c" * 64,
        },
        {
            "ensemble_system_id": "ECMWF_AIFS",
            "independence_family_id": "ECMWF",
            "members": _members(
                [25.1] * 10 if no_consensus else [26.1] * 6 + [27.1] * 4
            ),
            "member_set_sha256": "d" * 64,
        },
        {
            "ensemble_system_id": "ICON_GLOBAL",
            "independence_family_id": "DWD_ICON_EPS",
            "members": _members(icon),
            "member_set_sha256": "e" * 64,
        },
        {
            "ensemble_system_id": "ICON_SEAMLESS",
            "independence_family_id": "DWD_ICON_EPS",
            "members": _members(icon),
            "member_set_sha256": "f" * 64,
        },
        {
            "ensemble_system_id": "GEM",
            "independence_family_id": "ECCC_GEM",
            "members": _members(
                [24.1] * 10 if no_consensus else [24.1] * 7 + [25.1] * 3
            ),
            "member_set_sha256": "1" * 64,
        },
    ]
    packet_identity: dict[str, object] = {
        "schema": (
            "official_native_ensemble_forward_packet_v1"
            if official
            else "open_meteo_native_ensemble_forward_packet_v1"
        ),
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": "America/Los_Angeles",
        "bucket_intervals": INTERVALS,
        "bucket_contract_sha256": content_sha256(INTERVALS),
        "systems": systems,
    }
    if official:
        packet_identity["source_archives"] = []
        packet_identity["source_class"] = "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT"
        packet_identity["calls_order_gateway"] = False
        packet_identity["orders_submitted"] = 0
        packet_identity["live_submit_authorized"] = False
        packet_identity["snapshot_content_sha256"] = content_sha256(packet_identity)
        return packet_identity
    packet_identity["source_authority"] = {
        "source_role": "aggregator_republisher",
        "formal_p0_eligible": False,
    }
    packet_identity["packet_content_sha256"] = content_sha256(packet_identity)
    return packet_identity


def _inbox(packets: list[dict[str, object]]) -> dict[str, object]:
    identity = {
        "schema": "native_ensemble_forward_inbox_v1",
        "packets": packets,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_open_meteo_packet_is_rebucketed_by_registry_and_is_no_live():
    artifact = MODULE.build_scenario_roles_artifact(
        inbox=_inbox([_packet()]),
        registry=_registry(["TEST"]),
        station_ids=["TEST"],
        event_date="2026-08-01",
    )
    candidate = artifact["candidates"][0]
    assert candidate["market_registry"]["bucket_contract_sha256"] == content_sha256(
        INTERVALS
    )
    assert candidate["source_kind"] == "open_meteo_aggregator"
    assert candidate["raw_member_probability_is_not_p0_position"] is True
    assert candidate["roles"]["M"] is not None
    assert candidate["calls_order_gateway"] is False
    assert candidate["orders_submitted"] == 0
    assert artifact["writes_p0"] is False
    assert artifact["writes_p0m"] is False
    assert artifact["writes_p1"] is False
    assert artifact["content_sha256"] == content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )


def test_official_packet_is_supported_without_touching_p0_or_database():
    artifact = MODULE.build_scenario_roles_artifact(
        inbox=_inbox([_packet(official=True)]),
        registry=_registry(["TEST"]),
        station_ids=["TEST"],
        event_date="2026-08-01",
    )
    candidate = artifact["candidates"][0]
    assert candidate["source_kind"] == "official_native"
    assert candidate["roles"]["M"] is not None
    assert artifact["writes_shared_live_database"] is False
    assert artifact["live_submit_authorized"] is False


def test_missing_m_h_l_is_left_empty_and_not_forced():
    artifact = MODULE.build_scenario_roles_artifact(
        inbox=_inbox([_packet(no_consensus=True)]),
        registry=_registry(["TEST"]),
        station_ids=["TEST"],
        event_date="2026-08-01",
    )
    buckets = artifact["candidates"][0]["buckets"]
    assert buckets == {"L": None, "M": None, "H": None}
    assert artifact["candidates"][0]["blockers"]


def test_duplicate_family_products_do_not_add_votes():
    packet = _packet()
    packet["systems"].append({
        "ensemble_system_id": "ECMWF_DUPLICATE",
        "independence_family_id": "ECMWF",
        "members": _members([26.1] * 10),
        "member_set_sha256": "2" * 64,
    })
    packet_identity = {
        key: value for key, value in packet.items() if key != "packet_content_sha256"
    }
    packet["packet_content_sha256"] = content_sha256(packet_identity)
    artifact = MODULE.build_scenario_roles_artifact(
        inbox=_inbox([packet]),
        registry=_registry(["TEST"]),
        station_ids=["TEST"],
        event_date="2026-08-01",
    )
    m = artifact["candidates"][0]["roles"]["M"]
    assert m["score"]["independent_family_count"] == 2
    assert [
        row["independence_family_id"] for row in m["supporting_products"]
    ].count("ECMWF") == 1


def test_forty_five_station_batch_is_bounded_and_sorted():
    stations = [f"S{index:02d}" for index in range(45)]
    packets = [_packet(station=station) for station in stations]
    artifact = MODULE.build_scenario_roles_artifact(
        inbox=_inbox(packets),
        registry=_registry(stations),
        max_stations=45,
        event_date="2026-08-01",
    )
    assert artifact["station_count"] == 45
    assert artifact["station_scope"] == sorted(stations)
    assert artifact["resource_bounds"]["actual_station_count"] == 45
    assert bounded_json_bytes(artifact)
    candidate = artifact["candidates"][0]
    assert "role_selection" not in candidate
    assert len(candidate["role_selection_lineage"]["content_sha256"]) == 64
    assert candidate["role_selection_lineage"]["source_product_ids"]


def test_station_bound_rejects_forty_six():
    stations = [f"S{index:02d}" for index in range(46)]
    with pytest.raises(ContractError, match="station_scope_exceeds_bound"):
        MODULE.build_scenario_roles_artifact(
            inbox=_inbox([_packet(station=station) for station in stations]),
            registry=_registry(stations),
            max_stations=45,
            event_date="2026-08-01",
        )


def test_packet_hash_mismatch_blocks_before_output():
    packet = _packet()
    packet["packet_content_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="packet_hash_mismatch"):
        MODULE.build_scenario_roles_artifact(
            inbox=_inbox([packet]),
            registry=_registry(["TEST"]),
            station_ids=["TEST"],
            event_date="2026-08-01",
        )


def test_single_cli_materialization_is_atomic_and_idempotent(tmp_path):
    inbox_path = tmp_path / "native_ensemble_forward_inbox.json"
    registry_path = tmp_path / "market_registry_forward.json"
    output_path = tmp_path / "scenario_roles.json"
    inbox_path.write_text(
        json.dumps(_inbox([_packet()])),
        encoding="utf-8",
    )
    registry_path.write_text(
        json.dumps(_registry_export(["TEST"])),
        encoding="utf-8",
    )
    first, first_wrote = MODULE.materialize_scenario_roles(
        inbox_path=inbox_path,
        registry_path=registry_path,
        output_path=output_path,
        station_ids=["TEST"],
        event_date="2026-08-01",
    )
    second, second_wrote = MODULE.materialize_scenario_roles(
        inbox_path=inbox_path,
        registry_path=registry_path,
        output_path=output_path,
        station_ids=["TEST"],
        event_date="2026-08-01",
    )
    assert first_wrote is True
    assert second_wrote is False
    assert first == second
    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    assert persisted["content_sha256"] == first["content_sha256"]
