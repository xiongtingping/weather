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
