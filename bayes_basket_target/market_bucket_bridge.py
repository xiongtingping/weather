"""Canonical PR15.A market bucket bridge contract and builder."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Mapping, Sequence

from .canonical_contract import _event_date, _sha, _text, _timestamp, _timezone
from .contracts import ContractError, content_sha256
from .p0_producer import (
    bucket_intervals_from_registry_rows,
    market_bucket_bridge_identity,
)


CONTRACT_SCHEMA = "bayes_basket_target_canonical_market_bucket_bridge_contract_v1"
FACT_SCHEMA = "bayes_basket_target_canonical_market_bucket_bridge_fact_v1"
REGISTRY_EXPORT_SCHEMA = "bayes_basket_target_market_registry_snapshot_v1"
FACT_TYPE = "market_bucket_bridge"

_BUCKET_FIELDS = frozenset(
    {
        "ordinal",
        "outcome",
        "lower_c",
        "upper_c",
        "slug",
        "market_id",
        "condition_id",
        "yes_token",
        "no_token",
        "rules_sha256",
        "official_source_registry_sha256",
    }
)
_FACT_FIELDS = frozenset(
    {
        "schema",
        "bridge_id",
        "station_id",
        "event_date",
        "settlement_timezone",
        "settlement_definition",
        "settlement_unit",
        "source_lane",
        "market_registry_snapshot_sha256",
        "registry_scope_rowset_sha256",
        "bucket_contracts",
        "state",
        "authority_scope",
        "content_sha256",
    }
)
_CONTRACT_FIELDS = frozenset(
    {"schema", "as_of_utc", "builder_id", "bridge_facts", "content_sha256"}
)


def _strict(raw: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    if frozenset(raw) != fields:
        raise ContractError(f"canonical_market_bucket_bridge_{label}_fields_mismatch")


def _decimal12(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError(f"canonical_market_bucket_bridge_{field}_invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError(f"canonical_market_bucket_bridge_{field}_invalid") from exc
    if not parsed.is_finite() or value != format(parsed, ".12f"):
        raise ContractError(f"canonical_market_bucket_bridge_{field}_invalid")
    return value


def _bucket(raw: Mapping[str, Any], expected_ordinal: int, total: int) -> dict[str, Any]:
    _strict(raw, _BUCKET_FIELDS, "bucket")
    ordinal = raw.get("ordinal")
    if isinstance(ordinal, bool) or ordinal != expected_ordinal:
        raise ContractError("canonical_market_bucket_bridge_ordinal_invalid")
    lower = _decimal12(raw.get("lower_c"), "lower_c")
    upper = _decimal12(raw.get("upper_c"), "upper_c")
    if (expected_ordinal == 0) != (lower is None) or (
        expected_ordinal == total - 1
    ) != (upper is None):
        raise ContractError("canonical_market_bucket_bridge_tail_invalid")
    if lower is not None and upper is not None and Decimal(lower) >= Decimal(upper):
        raise ContractError("canonical_market_bucket_bridge_interval_invalid")
    return {
        "ordinal": expected_ordinal,
        "outcome": _text(raw.get("outcome"), "outcome"),
        "lower_c": lower,
        "upper_c": upper,
        "slug": _text(raw.get("slug"), "slug"),
        "market_id": _text(raw.get("market_id"), "market_id"),
        "condition_id": _text(raw.get("condition_id"), "condition_id"),
        "yes_token": _text(raw.get("yes_token"), "yes_token"),
        "no_token": _text(raw.get("no_token"), "no_token"),
        "rules_sha256": _sha(raw.get("rules_sha256"), "rules_sha256"),
        "official_source_registry_sha256": _sha(
            raw.get("official_source_registry_sha256"),
            "official_source_registry_sha256",
        ),
    }


@dataclass(frozen=True)
class CanonicalMarketBucketBridgeFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalMarketBucketBridgeFact":
        _strict(raw, _FACT_FIELDS, "fact")
        if (
            raw.get("schema") != FACT_SCHEMA
            or raw.get("settlement_definition") != "official_daily_tmax_c"
            or raw.get("settlement_unit") != "C"
            or raw.get("source_lane") != "AWC"
            or raw.get("state") != "MARKET_BUCKET_BRIDGE_READY"
            or raw.get("authority_scope") != "OFFICIAL_MARKET_REGISTRY"
        ):
            raise ContractError("canonical_market_bucket_bridge_role_invalid")
        buckets_raw = raw.get("bucket_contracts")
        if not isinstance(buckets_raw, list) or len(buckets_raw) < 2:
            raise ContractError("canonical_market_bucket_bridge_buckets_missing")
        buckets = [
            _bucket(item, ordinal, len(buckets_raw))
            for ordinal, item in enumerate(buckets_raw)
        ]
        for left, right in zip(buckets, buckets[1:]):
            if left["upper_c"] != right["lower_c"]:
                raise ContractError("canonical_market_bucket_bridge_gap_or_overlap")
        if len({item["slug"] for item in buckets}) != 1:
            raise ContractError("canonical_market_bucket_bridge_event_slug_mixed")
        # Every temperature bucket is a distinct binary market, but all buckets
        # for one station-day legitimately share the parent event slug.
        # Contract/token identities must remain unique; the event slug must not.
        for field in ("outcome", "market_id", "condition_id", "yes_token", "no_token"):
            if len({item[field] for item in buckets}) != len(buckets):
                raise ContractError(f"canonical_market_bucket_bridge_{field}_duplicate")
        token_ids = [
            token
            for item in buckets
            for token in (item["yes_token"], item["no_token"])
        ]
        if len(set(token_ids)) != len(token_ids):
            raise ContractError("canonical_market_bucket_bridge_token_collision")
        station = _text(raw.get("station_id"), "station_id").upper()
        event = _event_date(raw.get("event_date"))
        timezone_name = _timezone(raw.get("settlement_timezone"))
        snapshot_sha = _sha(
            raw.get("market_registry_snapshot_sha256"),
            "market_registry_snapshot_sha256",
        )
        rowset_sha = _sha(
            raw.get("registry_scope_rowset_sha256"),
            "registry_scope_rowset_sha256",
        )
        if rowset_sha != content_sha256(buckets):
            raise ContractError("canonical_market_bucket_bridge_rowset_hash_mismatch")
        bridge_seed = {
            "station_id": station,
            "event_date": event,
            "market_registry_snapshot_sha256": snapshot_sha,
            "registry_scope_rowset_sha256": rowset_sha,
        }
        bridge_id = "market-bucket-bridge-" + content_sha256(bridge_seed)[:40]
        if raw.get("bridge_id") != bridge_id:
            raise ContractError("canonical_market_bucket_bridge_id_mismatch")
        result = {
            **dict(raw),
            "bridge_id": bridge_id,
            "station_id": station,
            "event_date": event,
            "settlement_timezone": timezone_name,
            "market_registry_snapshot_sha256": snapshot_sha,
            "registry_scope_rowset_sha256": rowset_sha,
            "bucket_contracts": buckets,
            "content_sha256": _sha(raw.get("content_sha256"), "content_sha256"),
        }
        expected = content_sha256(
            {key: value for key, value in result.items() if key != "content_sha256"}
        )
        if result["content_sha256"] != expected:
            raise ContractError("canonical_market_bucket_bridge_hash_mismatch")
        return cls(result)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(
            (
                self.raw["station_id"],
                self.raw["event_date"],
                self.raw["market_registry_snapshot_sha256"],
            )
        )

    def to_mapping(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class CanonicalMarketBucketBridgeContract:
    as_of_utc: str
    builder_id: str
    bridge_facts: tuple[CanonicalMarketBucketBridgeFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalMarketBucketBridgeContract":
        _strict(raw, _CONTRACT_FIELDS, "contract")
        if raw.get("schema") != CONTRACT_SCHEMA:
            raise ContractError("canonical_market_bucket_bridge_contract_schema_invalid")
        facts_raw = raw.get("bridge_facts")
        if not isinstance(facts_raw, list) or not facts_raw:
            raise ContractError("canonical_market_bucket_bridge_contract_facts_missing")
        facts = tuple(CanonicalMarketBucketBridgeFact.from_mapping(item) for item in facts_raw)
        if len({item.scope_key for item in facts}) != len(facts):
            raise ContractError("canonical_market_bucket_bridge_scope_duplicate")
        supplied = _sha(raw.get("content_sha256"), "content_sha256")
        if supplied != content_sha256(
            {key: value for key, value in raw.items() if key != "content_sha256"}
        ):
            raise ContractError("canonical_market_bucket_bridge_contract_hash_mismatch")
        return cls(
            as_of_utc=_timestamp(raw.get("as_of_utc"), "as_of_utc"),
            builder_id=_text(raw.get("builder_id"), "builder_id"),
            bridge_facts=facts,
            content_sha256=supplied,
        )


def build_market_bucket_bridge_contract(
    registry_export: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
    builder_id: str = "official_awc_market_bucket_bridge_builder_v1",
) -> dict[str, Any]:
    if registry_export.get("schema") != REGISTRY_EXPORT_SCHEMA:
        raise ContractError("market_bucket_bridge_registry_export_schema_invalid")
    snapshot_sha = _sha(
        registry_export.get("snapshot_sha256"), "market_registry_snapshot_sha256"
    )
    if snapshot_sha != content_sha256(
        {key: value for key, value in registry_export.items() if key != "snapshot_sha256"}
    ):
        raise ContractError("market_bucket_bridge_registry_snapshot_hash_mismatch")
    if (
        registry_export.get("source_scope") != "shared_live_registry_export_only"
        or registry_export.get("forward_p0_catalog_only") is not True
        or registry_export.get("writes_shared_live_database") is not False
        or registry_export.get("calls_order_gateway") is not False
        or registry_export.get("places_orders") is not False
    ):
        raise ContractError("market_bucket_bridge_registry_export_role_invalid")
    rows_raw = registry_export.get("rows")
    if not isinstance(rows_raw, list):
        raise ContractError("market_bucket_bridge_registry_rows_invalid")
    if registry_export.get("row_count") != len(rows_raw):
        raise ContractError("market_bucket_bridge_registry_row_count_mismatch")
    station = _text(station_id, "station_id").upper()
    event = _event_date(event_date)
    canonical_as_of = _timestamp(as_of_utc, "as_of_utc")
    canonical_builder_id = _text(builder_id, "builder_id")
    exported_at_utc = _timestamp(
        registry_export.get("exported_at_utc"), "market_registry_exported_at_utc"
    )
    if registry_export.get("source_schema") != "reported_trajectory_market_registry_v1":
        raise ContractError("market_bucket_bridge_registry_source_schema_invalid")
    if exported_at_utc > canonical_as_of:
        raise ContractError("market_bucket_bridge_registry_from_future")
    rows = [
        dict(item)
        for item in rows_raw
        if isinstance(item, Mapping)
        and str(item.get("station_id") or "").upper() == station
        and item.get("event_date") == event
    ]
    if not rows:
        raise ContractError("market_bucket_bridge_scope_missing")
    if any(
        item.get("source_lane") != "awc"
        or item.get("source_eligible") != 1
        or item.get("market_active") != 1
        or item.get("market_accepting_orders") != 1
        for item in rows
    ):
        raise ContractError("market_bucket_bridge_scope_not_awc_eligible")
    timezone_names = {str(item.get("settlement_timezone") or "") for item in rows}
    if len(timezone_names) != 1:
        raise ContractError("market_bucket_bridge_timezone_mixed")
    timezone_name = timezone_names.pop()
    intervals = bucket_intervals_from_registry_rows(rows)
    base = market_bucket_bridge_identity(
        rows,
        bucket_intervals=intervals,
        settlement_timezone=timezone_name,
    )
    by_outcome = {str(item["outcome"]): item for item in rows}
    buckets = []
    official_registry_hashes: set[str] = set()
    for ordinal, item in enumerate(base["bucket_contracts"]):
        source = by_outcome[str(item["outcome"])]
        try:
            registry = json.loads(str(source["registry_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("market_bucket_bridge_registry_json_invalid") from exc
        if not isinstance(registry, Mapping):
            raise ContractError("market_bucket_bridge_registry_json_invalid")
        expected_registry_fields = {
            "schema": "reported_trajectory_market_registry_v1",
            "station_id": station,
            "event_date": event,
            "settlement_timezone": timezone_name,
            "settlement_unit": source.get("settlement_unit"),
            "source_lane": "awc",
            "source_eligible": True,
            "market_active": True,
            "market_accepting_orders": True,
            "outcome": source.get("outcome"),
            "slug": source.get("slug"),
            "yes_token": source.get("yes_token"),
            "no_token": source.get("no_token"),
            "rules_sha256": source.get("rules_sha256"),
        }
        if any(registry.get(key) != value for key, value in expected_registry_fields.items()):
            raise ContractError("market_bucket_bridge_registry_row_mismatch")
        official_registry_sha = _sha(
            registry.get("official_source_registry_sha256"),
            "official_source_registry_sha256",
        )
        official_registry_hashes.add(official_registry_sha)
        bucket = {
            "ordinal": ordinal,
            "outcome": str(item["outcome"]),
            "lower_c": None if item["lower_c"] is None else format(Decimal(str(item["lower_c"])), ".12f"),
            "upper_c": None if item["upper_c"] is None else format(Decimal(str(item["upper_c"])), ".12f"),
            "slug": str(source["slug"]),
            "market_id": str(item["market_id"]),
            "condition_id": str(item["condition_id"]),
            "yes_token": str(item["yes_token"]),
            "no_token": str(item["no_token"]),
            "rules_sha256": str(item["rules_sha256"]),
            "official_source_registry_sha256": official_registry_sha,
        }
        buckets.append(bucket)
    if len(official_registry_hashes) != 1:
        raise ContractError("market_bucket_bridge_official_registry_mixed")
    rowset_sha = content_sha256(buckets)
    bridge_seed = {
        "station_id": station,
        "event_date": event,
        "market_registry_snapshot_sha256": snapshot_sha,
        "registry_scope_rowset_sha256": rowset_sha,
    }
    fact_identity = {
        "schema": FACT_SCHEMA,
        "bridge_id": "market-bucket-bridge-" + content_sha256(bridge_seed)[:40],
        "station_id": station,
        "event_date": event,
        "settlement_timezone": timezone_name,
        "settlement_definition": "official_daily_tmax_c",
        "settlement_unit": "C",
        "source_lane": "AWC",
        "market_registry_snapshot_sha256": snapshot_sha,
        "registry_scope_rowset_sha256": rowset_sha,
        "bucket_contracts": buckets,
        "state": "MARKET_BUCKET_BRIDGE_READY",
        "authority_scope": "OFFICIAL_MARKET_REGISTRY",
    }
    fact = {**fact_identity, "content_sha256": content_sha256(fact_identity)}
    identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": canonical_as_of,
        "builder_id": canonical_builder_id,
        "bridge_facts": [fact],
    }
    packet = {**identity, "content_sha256": content_sha256(identity)}
    parsed = CanonicalMarketBucketBridgeContract.from_mapping(packet)
    return {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": parsed.as_of_utc,
        "builder_id": parsed.builder_id,
        "bridge_facts": [item.to_mapping() for item in parsed.bridge_facts],
        "content_sha256": parsed.content_sha256,
    }


__all__ = [
    "CONTRACT_SCHEMA",
    "FACT_SCHEMA",
    "FACT_TYPE",
    "CanonicalMarketBucketBridgeContract",
    "CanonicalMarketBucketBridgeFact",
    "build_market_bucket_bridge_contract",
]
