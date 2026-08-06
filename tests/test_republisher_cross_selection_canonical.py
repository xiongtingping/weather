from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_contract import (
    CanonicalRepublisherCrossSelectionContract,
)
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_ssot_v7_migration import (
    TARGET_TABLE_DDL as V7_TABLE_DDL,
    TARGET_TRIGGER_DDL as V7_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v8_migration import (
    TARGET_TABLE_DDL as V8_TABLE_DDL,
    TARGET_TRIGGER_DDL as V8_TRIGGER_DDL,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.republisher_cross_selection import (
    build_republisher_cross_selection_contract,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data/bayes_basket_target_ssot.db"
AS_OF = "2026-08-04T05:16:39Z"


def _copy_source_facts(*, v8: bool = True) -> sqlite3.Connection:
    assert SOURCE_DB.is_file(), "canonical source fixture missing"
    source = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    source.execute("PRAGMA query_only=ON")
    target = sqlite3.connect(":memory:")
    target.create_function("canonical_ingress_authorized", 0, lambda: 1)
    target.execute(V8_TABLE_DDL if v8 else V7_TABLE_DDL)
    for ddl in V8_TRIGGER_DDL if v8 else V7_TRIGGER_DDL:
        target.execute(ddl)
    rows = source.execute(
        "SELECT fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json,ingested_at_utc "
        "FROM canonical_ssot_fact WHERE fact_type IN ('truth','republisher_oos_artifact')"
    ).fetchall()
    assert sum(str(row[1]) == "republisher_oos_artifact" for row in rows) == 4
    target.executemany("INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)", rows)
    target.commit()
    source.close()
    return target


def _rehash(packet: dict[str, object]) -> None:
    facts = packet["selection_facts"]
    assert isinstance(facts, list)
    for fact in facts:
        assert isinstance(fact, dict)
        fact["content_sha256"] = content_sha256(
            {key: value for key, value in fact.items() if key != "content_sha256"}
        )
    packet["content_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "content_sha256"}
    )


def _packet(conn: sqlite3.Connection) -> dict[str, object]:
    return build_republisher_cross_selection_contract(
        conn,
        station_ids=["CYYZ", "NZWN"],
        as_of_utc=AS_OF,
        builder_id="republisher-cross-selection-test-v1",
    )


def _count(conn: sqlite3.Connection, fact_type: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type=?",
            (fact_type,),
        ).fetchone()[0]
    )


def test_real_four_artifacts_build_expected_two_selections() -> None:
    conn = _copy_source_facts()
    try:
        packet = _packet(conn)
        parsed = CanonicalRepublisherCrossSelectionContract.from_mapping(packet)
        by_station = {item.station_id: item for item in parsed.selection_facts}
        assert set(by_station) == {"CYYZ", "NZWN"}
        assert by_station["CYYZ"].selected_weights == {
            "AIFS": "0.355619265054",
            "ICON": "0.644380734946",
        }
        assert by_station["CYYZ"].selected_holdout_metrics["rmse_c"] == "0.425104250271"
        assert by_station["NZWN"].selected_weights == {
            "AIFS": "0.463848096005",
            "ICON": "0.536151903995",
        }
        assert by_station["NZWN"].selected_holdout_metrics["rmse_c"] == "0.739349758551"
        assert [
            item.upstream_model_id
            for item in by_station["CYYZ"].component_artifact_refs
        ] == ["AIFS", "ICON"]
        assert all(item.run_cycle is None for item in parsed.selection_facts)
    finally:
        conn.close()


def test_selection_preview_commit_and_replay_touch_only_selection_fact() -> None:
    conn = _copy_source_facts()
    try:
        packet = _packet(conn)
        before_total = int(
            conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0]
        )
        preview = ingest_canonical_contract(conn, packet, commit=False)
        assert preview["state"] == "VALIDATED_NO_WRITE"
        assert preview["inserted_count"] == 0
        assert _count(conn, "republisher_cross_selection") == 0
        committed = ingest_canonical_contract(
            conn, packet, commit=True, ingested_at_utc=AS_OF
        )
        assert committed["inserted_count"] == 2
        assert _count(conn, "republisher_cross_selection") == 2
        assert int(conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0]) == before_total + 2
        replay = ingest_canonical_contract(conn, packet, commit=True, ingested_at_utc=AS_OF)
        assert replay["inserted_count"] == 0
        assert replay["replayed_count"] == 2
        assert _count(conn, "activation") == 0
        for forbidden in (
            "calibration_native_ensemble_activation",
            "calibration_cross_native_ensemble_artifact",
            "calibration_republisher_ensemble_activation",
            "basket_probability_envelope",
        ):
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                (forbidden,),
            ).fetchone()[0] == 0
    finally:
        conn.close()


def test_selection_requires_exact_v8_schema() -> None:
    source = _copy_source_facts()
    packet = _packet(source)
    source.close()
    conn = _copy_source_facts(v8=False)
    try:
        with pytest.raises(
            ContractError, match="canonical_republisher_cross_selection_v8_required"
        ):
            ingest_canonical_contract(conn, packet, commit=False)
        assert _count(conn, "republisher_oos_artifact") == 4
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("mutator", "match"),
    (
        (
            lambda fact: fact.__setitem__("run_cycle", "00Z"),
            "canonical_republisher_cross_selection_run_cycle_invalid",
        ),
        (
            lambda fact: fact.__setitem__("formal_live_authority", True),
            "canonical_republisher_cross_selection_formal_live_authority_invalid",
        ),
        (
            lambda fact: fact["selected_weights"].__setitem__(
                "AIFS", "0.500000000000"
            ),
            "canonical_republisher_cross_selection_weights_invalid",
        ),
    ),
)
def test_selection_contract_rejects_cycle_authority_and_weight_tamper(
    mutator: object, match: str
) -> None:
    conn = _copy_source_facts()
    try:
        bad = copy.deepcopy(_packet(conn))
        fact = bad["selection_facts"][0]
        mutator(fact)  # type: ignore[operator]
        _rehash(bad)
        with pytest.raises(ContractError, match=match):
            CanonicalRepublisherCrossSelectionContract.from_mapping(bad)
    finally:
        conn.close()


def test_selection_ingress_recomputes_parent_refs_and_metrics() -> None:
    conn = _copy_source_facts()
    try:
        bad = copy.deepcopy(_packet(conn))
        fact = bad["selection_facts"][0]
        fact["component_artifact_refs"][0]["evaluation_content_sha256"] = "f" * 64
        _rehash(bad)
        with pytest.raises(
            ContractError, match="republisher_cross_selection_not_reproducible"
        ):
            ingest_canonical_contract(conn, bad, commit=False)
        assert _count(conn, "republisher_cross_selection") == 0
    finally:
        conn.close()
