from __future__ import annotations

import sqlite3

from p2_canonical_amsc_forward_capture_audit import audit_p2_canonical_amsc_forward_capture
from p2_canonical_amsc_forward_writer import apply_p2_canonical_amsc_forward_append
from sequential_bayes_schema_contract import apply_forward_schema
from tests.test_strict_amsc_hard_observation_reader import (
    _initialize_source_observations,
    _insert,
    _strict_primary,
)


def _ready_capture(tmp_path) -> tuple[sqlite3.Connection, object]:
    raw = tmp_path / "raw"
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    apply_forward_schema(conn)
    _insert(conn, _strict_primary(raw))
    conn.commit()
    apply_p2_canonical_amsc_forward_append(
        conn,
        event_date="2026-07-12",
        raw_archive_dir=raw,
        decision_as_of="2026-07-12T01:00:00Z",
    )
    return conn, raw


def test_audit_proves_primary_source_relation_and_original_t1(tmp_path) -> None:
    conn, raw = _ready_capture(tmp_path)
    try:
        result = audit_p2_canonical_amsc_forward_capture(
            conn,
            event_date="2026-07-12",
            raw_archive_dir=raw,
        )
        assert result["ok"] is True, result
        assert result["strict_primary"]["count"] == 1
        assert result["canonical"]["event_count"] == 1
        assert result["canonical"]["relation_count"] == 1
        assert result["boundary"]["posterior_or_transaction_b_evidence"] is False
    finally:
        conn.close()


def test_audit_rejects_missing_canonical_capture(tmp_path) -> None:
    raw = tmp_path / "raw"
    conn = sqlite3.connect(":memory:")
    try:
        _initialize_source_observations(conn)
        apply_forward_schema(conn)
        _insert(conn, _strict_primary(raw))
        conn.commit()
        result = audit_p2_canonical_amsc_forward_capture(
            conn,
            event_date="2026-07-12",
            raw_archive_dir=raw,
        )
        assert result["ok"] is False
        assert "p2_canonical_primary_capture_missing" in result["blockers"]
    finally:
        conn.close()
