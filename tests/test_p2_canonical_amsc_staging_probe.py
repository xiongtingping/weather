from __future__ import annotations

import sqlite3

from tests.test_strict_amsc_hard_observation_reader import _initialize_source_observations, _insert, _strict_primary
from scripts.p2_canonical_amsc_staging_probe import build_p2_canonical_amsc_staging_probe


def test_memory_probe_proves_strict_primary_amsc_to_idempotent_canonical_events(tmp_path) -> None:
    archive_db = tmp_path / "archive.db"
    with sqlite3.connect(archive_db) as conn:
        _initialize_source_observations(conn)
        _insert(conn, _strict_primary(tmp_path / "raw"))

    result = build_p2_canonical_amsc_staging_probe(
        archive_db=archive_db,
        raw_archive_dir=tmp_path / "raw",
        event_date="2026-07-12",
        target_identity="highest-temperature-in-chengdu-on-july-12-2026",
    )

    assert result["ok"] is True, result
    assert result["state"] == "proved"
    assert result["strict_source_observation_count"] == 1
    assert result["canonical_accepted_event_count"] == 1
    assert result["retry_append"]["inserted_events"] == 0
    assert result["retry_append"]["inserted_relations"] == 0
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["creates_posterior"] is False
