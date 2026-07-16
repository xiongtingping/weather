from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from resolution_target_contract_runtime import (
    EXTRACTION_SCHEMA_VERSION,
    EXTRACTOR_VERSION,
    build_market_resolution_target_contract_proposal,
)
from resolution_target_contract_shadow_writer import (
    TARGET_TABLE,
    initialize_isolated_resolution_target_contract_schema,
    load_latest_resolution_target_contract_shadow,
    persist_resolution_target_contract_shadow,
)
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())
DAY = "2026-07-03"
SLUG = "highest-temperature-in-chengdu-on-july-3-2026"
SOURCE = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"


def _raw(rounding: str = "nearest-whole") -> bytes:
    return (
        '{"slug":"highest-temperature-in-chengdu-on-july-3-2026",'
        '"event_date":"2026-07-03","station":"ZUUU",'
        '"resolution_source":"https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",'
        f'"unit":"C","rounding":"{rounding}",'
        '"buckets":["31C","32C"]}'
    ).encode("utf-8")


def _extraction(rounding: str = "nearest-whole") -> dict:
    return {
        "schema": EXTRACTION_SCHEMA_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "public_slug": SLUG,
        "resolution_local_date": DAY,
        "station_id": "ZUUU",
        "resolution_source": SOURCE,
        "temperature_unit": "C",
        "rounding_rule": rounding,
        "bucket_contract": {
            "31C": {"lower_inclusive": 30.5, "upper_exclusive": 31.5},
            "32C": {"lower_inclusive": 31.5, "upper_exclusive": 32.5},
        },
        "extraction_evidence_fragments": [_raw(rounding).decode("utf-8")],
    }


def _proposal(*, supersedes: str = "", rounding: str = "nearest-whole") -> dict:
    raw = _raw(rounding)
    return build_market_resolution_target_contract_proposal(
        authority_contract=AUTHORITY,
        target_identity=f"market_resolution_target_v1:{SLUG}",
        public_slug=SLUG,
        resolution_local_date=DAY,
        station_id="ZUUU",
        resolution_source=SOURCE,
        market_rule_raw_payload=raw,
        market_rule_raw_ref="market-rules/2026-07-03/rule.json",
        market_rule_raw_sha256=hashlib.sha256(raw).hexdigest(),
        market_rule_extraction=_extraction(rounding),
        supersedes_target_contract_sha256=supersedes,
    )


def test_target_contract_is_raw_bound_and_excludes_market_semantics() -> None:
    result = _proposal()
    assert result["ok"] is True
    assert result["bridge_closed"] is False
    assert result["boundary"]["does_not_read_market_outcome"] is True
    assert result["bucket_contract"]["31C"]["lower_inclusive"] == 30.5

    extraction = _extraction()
    extraction["outcomePrices"] = [0.1, 0.9]
    raw = _raw()
    blocked = build_market_resolution_target_contract_proposal(
        authority_contract=AUTHORITY,
        target_identity=f"market_resolution_target_v1:{SLUG}",
        public_slug=SLUG,
        resolution_local_date=DAY,
        station_id="ZUUU",
        resolution_source=SOURCE,
        market_rule_raw_payload=raw,
        market_rule_raw_ref="market-rules/2026-07-03/rule.json",
        market_rule_extraction=extraction,
    )
    assert blocked["ok"] is False
    assert "target_contract_extraction_contains_forbidden_market_semantics" in blocked["block_reasons"]

    extraction = _extraction()
    extraction["extraction_evidence_fragments"] = ['{"outcomePrices":[0.1,0.9]}']
    raw_with_price = _raw() + b'{"outcomePrices":[0.1,0.9]}'
    blocked_evidence = build_market_resolution_target_contract_proposal(
        authority_contract=AUTHORITY,
        target_identity=f"market_resolution_target_v1:{SLUG}",
        public_slug=SLUG,
        resolution_local_date=DAY,
        station_id="ZUUU",
        resolution_source=SOURCE,
        market_rule_raw_payload=raw_with_price,
        market_rule_raw_ref="market-rules/2026-07-03/rule.json",
        market_rule_extraction=extraction,
    )
    assert blocked_evidence["ok"] is False
    assert "target_contract_extraction_evidence_contains_forbidden_market_semantics" in blocked_evidence["block_reasons"]


def test_target_contract_registry_is_append_only_with_explicit_successor() -> None:
    initial = _proposal()
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_resolution_target_contract_schema(conn)
    created_at = "2026-07-03T10:00:00Z"
    first = persist_resolution_target_contract_shadow(conn, initial, created_at=created_at)
    assert first["idempotent"] is False
    assert persist_resolution_target_contract_shadow(conn, initial, created_at=created_at)["idempotent"] is True
    assert conn.execute(f"SELECT COUNT(*) FROM {TARGET_TABLE}").fetchone()[0] == 1
    loaded = load_latest_resolution_target_contract_shadow(
        conn, target_identity=initial["target_identity"], resolution_local_date=DAY
    )
    assert loaded and loaded["target_contract_sha256"] == initial["target_contract_sha256"]

    successor = _proposal(supersedes=initial["target_contract_sha256"], rounding="nearest-whole-v2")
    second = persist_resolution_target_contract_shadow(conn, successor, created_at="2026-07-03T10:01:00Z")
    assert second["revision_no"] == 1

    tampered = dict(successor)
    tampered["rounding_rule"] = "tampered"
    with pytest.raises(ValueError, match="target_contract_sha256_mismatch"):
        persist_resolution_target_contract_shadow(conn, tampered, created_at="2026-07-03T10:02:00Z")

    orphan = _proposal(supersedes="e" * 64, rounding="nearest-whole-v3")
    with pytest.raises(ShadowTransactionIntegrityError, match="successor_not_bound_to_latest"):
        persist_resolution_target_contract_shadow(conn, orphan, created_at="2026-07-03T10:03:00Z")
