from __future__ import annotations

import sqlite3

import pytest

from p2_canonical_amsc_forward_writer import apply_p2_canonical_amsc_forward_append
from p2_canonical_shadow_mirror import mirror_verified_p2_canonical_events_to_isolated_shadow
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_schema_contract import apply_forward_schema
from tests.test_strict_amsc_hard_observation_reader import (
    _initialize_source_observations,
    _insert,
    _strict_primary,
)


def _source_with_p2_capture(tmp_path) -> tuple[sqlite3.Connection, object]:
    raw = tmp_path / "raw"
    source = sqlite3.connect(":memory:")
    _initialize_source_observations(source)
    apply_forward_schema(source)
    _insert(source, _strict_primary(raw))
    source.commit()
    apply_p2_canonical_amsc_forward_append(
        source,
        event_date="2026-07-12",
        raw_archive_dir=raw,
        decision_as_of="2026-07-12T01:00:00Z",
    )
    return source, raw


def _isolated_shadow() -> sqlite3.Connection:
    shadow = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(shadow)
    apply_forward_schema(shadow)
    shadow.commit()
    return shadow


def test_mirror_moves_only_verified_canonical_evidence_into_isolated_shadow(tmp_path) -> None:
    source, raw = _source_with_p2_capture(tmp_path)
    shadow = _isolated_shadow()
    try:
        result = mirror_verified_p2_canonical_events_to_isolated_shadow(
            source,
            shadow,
            event_date="2026-07-12",
            raw_archive_dir=raw,
        )
        assert result["ok"] is True, result
        assert result["writes"]["production_archive"] is False
        assert result["writes"]["shadow_canonical_events"] == 1
        assert result["writes"]["shadow_canonical_relations"] == 1
        assert result["writes"]["forbidden_shadow_table_changes"] == {}
        assert shadow.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 1
        assert shadow.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
        assert shadow.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0
        assert source.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 1

        retry = mirror_verified_p2_canonical_events_to_isolated_shadow(
            source,
            shadow,
            event_date="2026-07-12",
            raw_archive_dir=raw,
        )
        assert retry["persistence"]["inserted_events"] == 0
        assert retry["persistence"]["inserted_relations"] == 0
    finally:
        source.close()
        shadow.close()


def test_mirror_rejects_non_isolated_destination(tmp_path) -> None:
    source, raw = _source_with_p2_capture(tmp_path)
    unsafe_destination = sqlite3.connect(":memory:")
    apply_forward_schema(unsafe_destination)
    unsafe_destination.commit()
    try:
        with pytest.raises(ValueError, match="destination_manifest_missing"):
            mirror_verified_p2_canonical_events_to_isolated_shadow(
                source,
                unsafe_destination,
                event_date="2026-07-12",
                raw_archive_dir=raw,
            )
    finally:
        source.close()
        unsafe_destination.close()
