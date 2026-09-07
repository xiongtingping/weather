from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_contract import (
    CanonicalRepublisherNoLiveActivationContract,
)
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_ssot_v8_migration import (
    TARGET_TABLE_DDL as V8_TABLE_DDL,
    TARGET_TRIGGER_DDL as V8_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v9_migration import (
    TARGET_TABLE_DDL as V9_TABLE_DDL,
    TARGET_TRIGGER_DDL as V9_TRIGGER_DDL,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.republisher_no_live_activation import (
    build_republisher_no_live_activation_contract,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data/bayes_basket_target_ssot.db"
AS_OF = "2026-08-04T08:00:00Z"


def _fixture(*, v9: bool = True) -> sqlite3.Connection:
    source = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    target = sqlite3.connect(":memory:")
    target.create_function("canonical_ingress_authorized", 0, lambda: 1)
    target.execute(V9_TABLE_DDL if v9 else V8_TABLE_DDL)
    for ddl in V9_TRIGGER_DDL if v9 else V8_TRIGGER_DDL:
        target.execute(ddl)
    # As of AS_OF, and only as of it.  This fixture is seeded from the live
    # SSOT, so every truth revision production has committed since then used
    # to arrive here too -- and the builder refuses a selection whose truth
    # reference is not the latest, which is exactly what a later revision
    # makes it.  Twenty tests in this file were failing on
    # republisher_cross_selection_truth_revision_stale for that reason
    # alone: the world the fixture claims to reconstruct is the world at
    # AS_OF, and a fact ingested after it was not in that world.
    rows = source.execute(
        "SELECT fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json,ingested_at_utc "
        "FROM canonical_ssot_fact WHERE fact_type IN "
        "('truth','republisher_oos_artifact','republisher_cross_selection') "
        "AND ingested_at_utc <= ?",
        (AS_OF,),
    ).fetchall()
    assert sum(str(row[1]) == "republisher_cross_selection" for row in rows) == 2
    target.executemany("INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)", rows)
    target.commit()
    source.close()
    return target


def _packet(conn: sqlite3.Connection) -> dict[str, object]:
    return build_republisher_no_live_activation_contract(
        conn,
        station_ids=["CYYZ", "NZWN"],
        as_of_utc=AS_OF,
        builder_id="republisher-no-live-activation-test-v1",
    )


def _count(conn: sqlite3.Connection, fact_type: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type=?",
            (fact_type,),
        ).fetchone()[0]
    )


def _rehash_fact_and_contract(packet: dict[str, object], index: int = 0) -> None:
    facts = packet["activation_facts"]
    assert isinstance(facts, list)
    fact = facts[index]
    assert isinstance(fact, dict)
    root = {
        key: fact[key]
        for key in (
            "station_id",
            "settlement_timezone",
            "settlement_definition",
            "season_bin",
            "lead_bucket",
            "run_cycle",
            "run_cycle_scope",
        )
    }
    fact["activation_id"] = "republisher-no-live-activation-" + content_sha256(
        {
            "root": root,
            "activation_generation": fact["activation_generation"],
            "selection_content_sha256": fact["selection_content_sha256"],
            "predecessor_activation_content_sha256": fact[
                "predecessor_activation_content_sha256"
            ],
        }
    )[:40]
    fact["content_sha256"] = content_sha256(
        {key: value for key, value in fact.items() if key != "content_sha256"}
    )
    packet["content_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "content_sha256"}
    )


def test_builder_emits_two_gen1_static_no_live_activations() -> None:
    conn = _fixture()
    try:
        parsed = CanonicalRepublisherNoLiveActivationContract.from_mapping(
            _packet(conn)
        )
        assert {item.station_id for item in parsed.activation_facts} == {
            "CYYZ",
            "NZWN",
        }
        assert all(item.activation_generation == 1 for item in parsed.activation_facts)
        assert all(
            item.predecessor_activation_content_sha256 is None
            for item in parsed.activation_facts
        )
        assert all(item.run_cycle is None for item in parsed.activation_facts)
        assert all(not item.formal_live_authority for item in parsed.activation_facts)
    finally:
        conn.close()


def test_preview_commit_and_replay_touch_only_new_activation_type() -> None:
    conn = _fixture()
    try:
        packet = _packet(conn)
        before = int(conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0])
        preview = ingest_canonical_contract(conn, packet, commit=False)
        assert preview["inserted_count"] == 0
        assert _count(conn, "republisher_no_live_activation") == 0
        committed = ingest_canonical_contract(
            conn, packet, commit=True, ingested_at_utc=AS_OF
        )
        assert committed["inserted_count"] == 2
        assert _count(conn, "republisher_no_live_activation") == 2
        assert int(conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0]) == before + 2
        replay_packet = _packet(conn)
        replay = ingest_canonical_contract(
            conn, replay_packet, commit=True, ingested_at_utc=AS_OF
        )
        assert replay["inserted_count"] == 0
        assert replay["replayed_count"] == 2
        assert _count(conn, "activation") == 0
        assert _count(conn, "native_arrival") == 0
    finally:
        conn.close()


def test_activation_requires_exact_v9_schema() -> None:
    source = _fixture()
    packet = _packet(source)
    source.close()
    conn = _fixture(v9=False)
    try:
        with pytest.raises(
            ContractError,
            match="canonical_republisher_no_live_activation_v9_required",
        ):
            ingest_canonical_contract(conn, packet, commit=False)
        assert _count(conn, "republisher_cross_selection") == 2
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("run_cycle", "00Z", "run_cycle_invalid"),
        ("formal_live_authority", True, "formal_live_authority_invalid"),
        ("future_event_count", 1, "future_event_count_invalid"),
        ("authority_scope", "FORMAL_NATIVE_ELIGIBLE", "authority_scope_invalid"),
    ),
)
def test_contract_rejects_cycle_and_authority_escalation(
    field: str, value: object, match: str
) -> None:
    conn = _fixture()
    try:
        bad = copy.deepcopy(_packet(conn))
        facts = bad["activation_facts"]
        assert isinstance(facts, list) and isinstance(facts[0], dict)
        facts[0][field] = value
        _rehash_fact_and_contract(bad)
        with pytest.raises(ContractError, match=match):
            CanonicalRepublisherNoLiveActivationContract.from_mapping(bad)
    finally:
        conn.close()


def test_ingress_rejects_missing_selection_and_cross_station_scope() -> None:
    conn = _fixture()
    try:
        missing = copy.deepcopy(_packet(conn))
        facts = missing["activation_facts"]
        assert isinstance(facts, list) and isinstance(facts[0], dict)
        facts[0]["selection_content_sha256"] = "f" * 64
        _rehash_fact_and_contract(missing)
        with pytest.raises(
            ContractError, match="republisher_cross_selection_persisted_fact_missing"
        ):
            ingest_canonical_contract(conn, missing, commit=False)

        cross = copy.deepcopy(_packet(conn))
        facts = cross["activation_facts"]
        assert isinstance(facts, list) and isinstance(facts[0], dict)
        facts[0]["settlement_timezone"] = "Pacific/Auckland"
        _rehash_fact_and_contract(cross)
        with pytest.raises(
            ContractError, match="republisher_no_live_activation_selection_scope_mismatch"
        ):
            ingest_canonical_contract(conn, cross, commit=False)
        assert _count(conn, "republisher_no_live_activation") == 0
    finally:
        conn.close()


def test_generation_skip_and_predecessor_forgery_fail_closed() -> None:
    conn = _fixture()
    try:
        bad = copy.deepcopy(_packet(conn))
        facts = bad["activation_facts"]
        assert isinstance(facts, list) and isinstance(facts[0], dict)
        facts[0]["activation_generation"] = 2
        facts[0]["predecessor_activation_content_sha256"] = "e" * 64
        _rehash_fact_and_contract(bad)
        with pytest.raises(
            ContractError,
            match="republisher_no_live_activation_generation_not_contiguous",
        ):
            ingest_canonical_contract(conn, bad, commit=False)
        assert _count(conn, "republisher_no_live_activation") == 0
    finally:
        conn.close()


def test_persisted_selection_truth_revision_tamper_is_rejected() -> None:
    conn = _fixture()
    try:
        packet = _packet(conn)
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        row = conn.execute(
            "SELECT fact_id,fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='republisher_cross_selection' ORDER BY fact_id LIMIT 1"
        ).fetchone()
        assert row is not None
        raw = json.loads(str(row[1]))
        raw["holdout_truth_refs"][0]["awc_truth_revision_no"] += 1
        # Bypass is impossible through production ingress; this direct fixture
        # mutation deliberately proves the persisted parent is re-parsed.
        conn.execute("DROP TRIGGER canonical_ssot_fact_no_update")
        conn.execute(
            "UPDATE canonical_ssot_fact SET fact_json=? WHERE fact_id=?",
            (json.dumps(raw, sort_keys=True, separators=(",", ":")), row[0]),
        )
        with pytest.raises(ContractError):
            ingest_canonical_contract(conn, packet, commit=False)
        assert _count(conn, "republisher_no_live_activation") == 0
    finally:
        conn.close()
