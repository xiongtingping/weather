from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_ssot_v10_migration import (
    TARGET_TABLE_DDL as V10_TABLE_DDL,
    TARGET_TRIGGER_DDL as V10_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v11_migration import (
    TARGET_TABLE_DDL as V11_TABLE_DDL,
    TARGET_TRIGGER_DDL as V11_TRIGGER_DDL,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.market_bucket_bridge import (
    CanonicalMarketBucketBridgeContract,
    build_market_bucket_bridge_contract,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "data/runtime/bayes_basket_target_nwp/market_registry_forward.json"
AS_OF = "2026-08-03T07:03:34.302Z"


def _registry() -> dict[str, object]:
    return json.loads(REGISTRY.read_text())


def _rehash_registry(registry: dict[str, object]) -> None:
    registry["snapshot_sha256"] = content_sha256(
        {key: value for key, value in registry.items() if key != "snapshot_sha256"}
    )


def _scope_rows(registry: dict[str, object]) -> list[dict[str, object]]:
    return [
        row
        for row in registry["rows"]
        if row["station_id"] == "CYYZ" and row["event_date"] == "2026-08-03"
    ]


def _database(version: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
    ddl = V11_TABLE_DDL if version == 11 else V10_TABLE_DDL
    triggers = V11_TRIGGER_DDL if version == 11 else V10_TRIGGER_DDL
    conn.execute(ddl)
    for trigger in triggers:
        conn.execute(trigger)
    return conn


def test_awc_registry_scope_builds_one_reproducible_bridge() -> None:
    packet = build_market_bucket_bridge_contract(
        _registry(), station_id="CYYZ", event_date="2026-08-03", as_of_utc=AS_OF
    )
    contract = CanonicalMarketBucketBridgeContract.from_mapping(packet)
    fact = contract.bridge_facts[0]
    assert fact.raw["source_lane"] == "AWC"
    assert fact.raw["settlement_timezone"] == "America/Toronto"
    assert fact.raw["registry_scope_rowset_sha256"] == content_sha256(
        fact.raw["bucket_contracts"]
    )
    assert fact.raw["bucket_contracts"][0]["lower_c"] is None
    assert fact.raw["bucket_contracts"][-1]["upper_c"] is None
    assert len({row["slug"] for row in fact.raw["bucket_contracts"]}) == 1
    assert len({row["market_id"] for row in fact.raw["bucket_contracts"]}) == len(
        fact.raw["bucket_contracts"]
    )
    for left, right in zip(
        fact.raw["bucket_contracts"], fact.raw["bucket_contracts"][1:]
    ):
        assert left["upper_c"] == right["lower_c"]


def test_bridge_requires_v11_and_unique_ingress_is_idempotent() -> None:
    packet = build_market_bucket_bridge_contract(
        _registry(), station_id="CYYZ", event_date="2026-08-03", as_of_utc=AS_OF
    )
    old = _database(10)
    try:
        with pytest.raises(ContractError, match="bridge_v11_required"):
            ingest_canonical_contract(old, packet, commit=True, ingested_at_utc=AS_OF)
    finally:
        old.close()
    conn = _database(11)
    try:
        first = ingest_canonical_contract(
            conn, packet, commit=True, ingested_at_utc=AS_OF
        )
        replay = ingest_canonical_contract(
            conn, packet, commit=True, ingested_at_utc=AS_OF
        )
        assert first["inserted_count"] == 1
        assert replay["inserted_count"] == 0
        assert replay["replayed_count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact "
            "WHERE fact_type='market_bucket_bridge'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_amsc_or_rehashed_token_tamper_fails_closed() -> None:
    registry = _registry()
    row = _scope_rows(registry)[0]
    row["source_lane"] = "amsc"
    nested = json.loads(row["registry_json"])
    nested["source_lane"] = "amsc"
    row["registry_json"] = json.dumps(
        nested, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    _rehash_registry(registry)
    with pytest.raises(ContractError, match="scope_not_awc_eligible"):
        build_market_bucket_bridge_contract(
            registry, station_id="CYYZ", event_date="2026-08-03", as_of_utc=AS_OF
        )

    packet = build_market_bucket_bridge_contract(
        _registry(), station_id="CYYZ", event_date="2026-08-03", as_of_utc=AS_OF
    )
    bad = copy.deepcopy(packet)
    fact = bad["bridge_facts"][0]
    fact["bucket_contracts"][0]["yes_token"] += "1"
    fact["content_sha256"] = content_sha256(
        {key: value for key, value in fact.items() if key != "content_sha256"}
    )
    bad["content_sha256"] = content_sha256(
        {key: value for key, value in bad.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="rowset_hash_mismatch"):
        CanonicalMarketBucketBridgeContract.from_mapping(bad)


@pytest.mark.parametrize(
    ("field", "nested_field"),
    (("market_active", "market_active"), ("market_accepting_orders", "market_accepting_orders")),
)
def test_inactive_or_not_accepting_scope_fails_closed(
    field: str, nested_field: str
) -> None:
    registry = _registry()
    row = _scope_rows(registry)[0]
    row[field] = 0
    nested = json.loads(row["registry_json"])
    nested[nested_field] = False
    row["registry_json"] = json.dumps(
        nested, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    _rehash_registry(registry)
    with pytest.raises(ContractError, match="scope_not_awc_eligible"):
        build_market_bucket_bridge_contract(
            registry, station_id="CYYZ", event_date="2026-08-03", as_of_utc=AS_OF
        )


def test_snapshot_and_rehashed_row_tamper_fail_closed() -> None:
    registry = _registry()
    registry["exported_at_utc"] = "2026-08-03T07:03:35.302Z"
    with pytest.raises(ContractError, match="snapshot_hash_mismatch"):
        build_market_bucket_bridge_contract(
            registry, station_id="CYYZ", event_date="2026-08-03", as_of_utc=AS_OF
        )

    registry = _registry()
    row = _scope_rows(registry)[0]
    row["yes_token"] = str(row["yes_token"]) + "1"
    _rehash_registry(registry)
    with pytest.raises(ContractError, match="registry_row_mismatch"):
        build_market_bucket_bridge_contract(
            registry, station_id="CYYZ", event_date="2026-08-03", as_of_utc=AS_OF
        )


def test_cross_side_token_collision_fails_closed() -> None:
    registry = _registry()
    row = _scope_rows(registry)[0]
    row["no_token"] = row["yes_token"]
    nested = json.loads(row["registry_json"])
    nested["no_token"] = nested["yes_token"]
    row["registry_json"] = json.dumps(
        nested, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    _rehash_registry(registry)
    with pytest.raises(ContractError, match="token_collision"):
        build_market_bucket_bridge_contract(
            registry, station_id="CYYZ", event_date="2026-08-03", as_of_utc=AS_OF
        )


def test_wrong_source_schema_or_future_registry_fails_closed() -> None:
    registry = _registry()
    registry["source_schema"] = "legacy_market_registry"
    _rehash_registry(registry)
    with pytest.raises(ContractError, match="source_schema_invalid"):
        build_market_bucket_bridge_contract(
            registry, station_id="CYYZ", event_date="2026-08-03", as_of_utc=AS_OF
        )

    registry = _registry()
    registry["exported_at_utc"] = "2026-08-03T07:03:35.000Z"
    _rehash_registry(registry)
    with pytest.raises(ContractError, match="registry_from_future"):
        build_market_bucket_bridge_contract(
            registry, station_id="CYYZ", event_date="2026-08-03", as_of_utc=AS_OF
        )
