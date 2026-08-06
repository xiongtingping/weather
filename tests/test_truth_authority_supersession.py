from __future__ import annotations

from copy import deepcopy
import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_ssot_v15_migration import (
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.truth_authority_supersession import (
    REVIEW_AUDIT_SCHEMA,
    CanonicalTruthAuthoritySupersessionContract,
    build_canonical_truth_authority_supersession_contract,
    build_truth_authority_supersession_bundle,
)
from tests.pr12_authority_projection_test_support import authority_projection
from tests.test_awc_observation_contract import _bounds, _revision


def _legacy(value: dict) -> dict:
    result = deepcopy(value)
    result.pop("authority_scope_projection_content_sha256", None)
    result.pop("content_sha256", None)
    return {**result, "content_sha256": content_sha256(result)}


def _review_audit(*, blockers: list[dict] | None = None) -> dict:
    rows = blockers or []
    identity = {
        "schema": REVIEW_AUDIT_SCHEMA,
        "as_of_utc": authority_projection()["as_of_utc"],
        "authority_scope_projection_content_sha256": authority_projection()[
            "content_sha256"
        ],
        "legacy_scope_count": 1,
        "review_scope_blocker_count": len(rows),
        "review_scope_blockers": rows,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE canonical_ssot_fact("
        "fact_id TEXT PRIMARY KEY,fact_type TEXT,scope_key TEXT,content_sha256 TEXT,"
        "fact_json TEXT,lineage_json TEXT,ingested_at_utc TEXT)"
    )
    revision = _legacy(_revision())
    bounds = _legacy(_bounds(revision))
    conn.executemany(
        "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
        (
            (
                "revision", "truth_observation_revision", "revision-scope",
                revision["content_sha256"], json.dumps(revision), "{}",
                "2026-08-02T05:00:00Z",
            ),
            (
                "bounds", "truth_observation_day_bounds", "bounds-scope",
                bounds["content_sha256"], json.dumps(bounds), "{}",
                "2026-08-02T05:00:00Z",
            ),
        ),
    )
    return conn


def _v15_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
    conn.execute(TARGET_TABLE_DDL)
    for ddl in TARGET_TRIGGER_DDL:
        conn.execute(ddl)
    revision = _legacy(_revision())
    bounds = _legacy(_bounds(revision))
    conn.executemany(
        "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
        (
            (
                "revision", "truth_observation_revision", "revision-scope",
                revision["content_sha256"], json.dumps(revision), "{}",
                "2026-08-02T05:00:00Z",
            ),
            (
                "bounds", "truth_observation_day_bounds", "bounds-scope",
                bounds["content_sha256"], json.dumps(bounds), "{}",
                "2026-08-02T05:00:00Z",
            ),
        ),
    )
    conn.commit()
    return conn


def test_legacy_truth_becomes_one_non_writing_supersession_candidate() -> None:
    conn = _database()
    before = conn.total_changes
    bundle = build_truth_authority_supersession_bundle(
        conn, authority_scope_projection=authority_projection()
    )
    assert bundle["candidate_count"] == 1
    assert bundle["terminal_count"] == 0
    assert bundle["candidates"][0]["truth_value_c"] == 31.0
    assert bundle["candidates"][0]["consumable"] is False
    assert conn.total_changes == before


def test_legacy_truth_hash_tamper_yields_terminal_not_candidate() -> None:
    conn = _database()
    raw = json.loads(conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth_observation_day_bounds'"
    ).fetchone()[0])
    raw["running_max_c"] = 99.0
    conn.execute(
        "UPDATE canonical_ssot_fact SET fact_json=? "
        "WHERE fact_type='truth_observation_day_bounds'", (json.dumps(raw),)
    )
    bundle = build_truth_authority_supersession_bundle(
        conn, authority_scope_projection=authority_projection()
    )
    assert bundle["candidate_count"] == 0
    assert bundle["terminal_count"] == 1


def test_projection_tamper_is_rejected_before_legacy_scan() -> None:
    projection = authority_projection()
    projection["rows"][0]["source_lane"] = "AMSC"
    with pytest.raises(ContractError, match="authority_projection"):
        build_truth_authority_supersession_bundle(
            _database(), authority_scope_projection=projection
        )


def test_legacy_truth_builds_one_v2_successor_and_typed_edge() -> None:
    contract = build_canonical_truth_authority_supersession_contract(
        _database(), authority_scope_projection=authority_projection(),
        review_scope_audit=_review_audit(),
    )
    parsed = CanonicalTruthAuthoritySupersessionContract.from_mapping(contract)
    assert len(parsed.awc_observation_contract.revision_facts) == 1
    assert len(parsed.awc_observation_contract.day_bounds_facts) == 1
    assert parsed.awc_observation_contract.day_bounds_facts[0].raw["bounds_version"] == 2
    assert len(parsed.supersession_facts) == 1
    edge = parsed.supersession_facts[0].raw
    assert (
        edge["predecessor_revision_semantic_vector_sha256"]
        == edge["successor_revision_semantic_vector_sha256"]
    )


def test_contract_rejects_successor_truth_tamper() -> None:
    contract = build_canonical_truth_authority_supersession_contract(
        _database(), authority_scope_projection=authority_projection(),
        review_scope_audit=_review_audit(),
    )
    contract["awc_observation_contract"]["day_bounds_facts"][0]["running_max_c"] = 99.0
    with pytest.raises(ContractError, match="awc_observation"):
        CanonicalTruthAuthoritySupersessionContract.from_mapping(contract)


def test_v15_single_ingress_previews_commits_and_replays_atomically() -> None:
    conn = _v15_database()
    contract = build_canonical_truth_authority_supersession_contract(
        conn, authority_scope_projection=authority_projection(),
        review_scope_audit=_review_audit(),
    )
    preview = ingest_canonical_contract(conn, contract)
    assert preview["state"] == "VALIDATED_NO_WRITE"
    assert preview["inserted_count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM canonical_ssot_fact"
    ).fetchone()[0] == 2
    applied = ingest_canonical_contract(conn, contract, commit=True)
    assert applied["inserted_count"] == 3
    assert applied["validated_counts"] == {
        "truth_observation_revision": 1,
        "truth_observation_day_bounds": 1,
        "truth_authority_supersession": 1,
    }
    replay = ingest_canonical_contract(conn, contract, commit=True)
    assert replay["inserted_count"] == 0
    assert replay["replayed_count"] == 3
    conn.close()


def test_v15_edge_conflict_rolls_back_successor_rows() -> None:
    conn = _v15_database()
    contract = build_canonical_truth_authority_supersession_contract(
        conn, authority_scope_projection=authority_projection(),
        review_scope_audit=_review_audit(),
    )
    parsed = CanonicalTruthAuthoritySupersessionContract.from_mapping(contract)
    edge = parsed.supersession_facts[0]
    digest = "e" * 64
    conn.execute(
        "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
        (
            f"truth_authority_supersession:{digest}",
            "truth_authority_supersession",
            edge.scope_key,
            digest,
            "{}",
            "{}",
            "2026-08-05T12:00:00Z",
        ),
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0]
    with pytest.raises(ContractError, match="canonical_scope_content_conflict"):
        ingest_canonical_contract(conn, contract, commit=True)
    assert conn.execute(
        "SELECT COUNT(*) FROM canonical_ssot_fact"
    ).fetchone()[0] == before
    assert conn.execute(
        "SELECT COUNT(*) FROM canonical_ssot_fact "
        "WHERE fact_type='truth_observation_day_bounds' "
        "AND json_extract(fact_json,'$.bounds_version')=2"
    ).fetchone()[0] == 0
    conn.close()
