"""Charter 272: a training fact stores its plan lineage's large bodies by
reference, as the probability envelope does, and reads back the text it was
built from -- so its content address is unchanged.

16.8 KB of a 55 KB fact, 4,000 facts a day, were the envelope's three
dehydrated lineage documents written out again in full.
"""

from __future__ import annotations

import json
import sqlite3

from bayes_basket_target.contracts import canonical_json, content_sha256
from bayes_basket_target.delta_v2_training import (
    FACT_SCHEMA,
    hydrated_feature_json,
    stored_feature_json,
)
from bayes_basket_target.materialization import (
    DEHYDRATED_LINEAGE_KEYS,
    LINEAGE_DOCUMENT_REF_PREFIX,
)


def _feature() -> str:
    lineage = {key: json.dumps({"body": key * 400}) for key in DEHYDRATED_LINEAGE_KEYS}
    lineage["cash_phase_decision_sha256"] = "c" * 64
    return canonical_json({
        "plan": {"lineage": lineage, "delta_v2_audit": {"authorized": True}},
        "station_policy_outcome_materialization": {"x": 1},
    })


def test_the_bodies_are_stored_once_by_reference_and_read_back_exactly() -> None:
    conn = sqlite3.connect(":memory:")
    built = _feature()
    stored = stored_feature_json(conn, built, created_at_utc="2026-10-16T02:00:00Z")
    assert len(stored) < len(built) / 3
    lineage = json.loads(stored)["plan"]["lineage"]
    for key in DEHYDRATED_LINEAGE_KEYS:
        assert lineage[key].startswith(LINEAGE_DOCUMENT_REF_PREFIX)
    assert lineage["cash_phase_decision_sha256"] == "c" * 64  # read in place
    assert hydrated_feature_json(conn, stored) == built
    # The content address is over the text as built.
    identity = {"feature_json": hydrated_feature_json(conn, stored)}
    assert content_sha256({"schema": FACT_SCHEMA, **identity}) == content_sha256(
        {"schema": FACT_SCHEMA, "feature_json": built}
    )
    # A second fact with the same bodies adds no document.
    stored_feature_json(conn, built, created_at_utc="2026-10-16T02:05:00Z")
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_lineage_document"
    ).fetchone()[0] == len(DEHYDRATED_LINEAGE_KEYS)


def test_text_that_would_not_read_back_exactly_is_stored_as_built() -> None:
    conn = sqlite3.connect(":memory:")
    # Not canonical (spaces, unsorted keys): a re-serialisation would differ.
    built = json.dumps({"plan": {"lineage": {
        DEHYDRATED_LINEAGE_KEYS[0]: "{\"b\": 1}"}}, "a": 1}, indent=1)
    assert stored_feature_json(conn, built, created_at_utc="2026-10-16T02:00:00Z") == built


def test_a_fact_stored_whole_reads_as_it_was() -> None:
    conn = sqlite3.connect(":memory:")
    built = _feature()
    assert hydrated_feature_json(conn, built) == built
    assert hydrated_feature_json(conn, "{}") == "{}"
