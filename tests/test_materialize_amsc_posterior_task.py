import json
import sqlite3
from pathlib import Path

from scripts.materialize_amsc_posterior_task import materialize_amsc_posterior_task
from tests.test_posterior_ssot_rematerialization_plan import _amsc_source_only_payload
from ui import server


def _source_only_db(tmp_path: Path) -> tuple[Path, int]:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    server.ensure_fact_tables(conn)
    payload = _amsc_source_only_payload()
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["event_date"],
            payload["slug"],
            payload["event_date"],
            "2026-07-05T16:00:14Z",
            "amsc_zuuu_metar_taf_fetcher_v1",
            json.dumps(payload),
            "2026-07-05T16:00:14Z",
        ),
    )
    fact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()
    return db_path, fact_id


def _payload(source_fact_id: int, **extra: object) -> dict:
    return {
        "schema": "amsc_posterior_materialization_task_payload_v1",
        "slug": "highest-temperature-in-chengdu-on-july-5-2026",
        "event_date": "2026-07-05",
        "amsc_source_fact_id": source_fact_id,
        "amsc_source_fact_as_of": "2026-07-05T16:00:14Z",
        **extra,
    }


def test_unpinned_or_legacy_task_cannot_borrow_current_weak_generation(tmp_path: Path) -> None:
    db_path, fact_id = _source_only_db(tmp_path)

    result = materialize_amsc_posterior_task(
        _payload(fact_id), db_path=db_path, generated_at="2026-07-05T16:01:00Z"
    )

    assert result["ok"] is False
    assert result["block_reason"] == "official_nwp_context_policy_missing"
    assert result["writes_posterior"] is False


def test_retired_v31_open_meteo_policy_is_not_a_compatibility_path(tmp_path: Path) -> None:
    db_path, fact_id = _source_only_db(tmp_path)

    result = materialize_amsc_posterior_task(
        _payload(
            fact_id,
            v31_multimodel_context_policy={
                "schema": "v31_multimodel_context_policy_v1",
                "mode": "prewarmed_cache_only_v1",
                "cache_generation_ref": {"schema": "v31_multimodel_cache_generation_v1"},
            },
        ),
        db_path=db_path,
        generated_at="2026-07-05T16:01:00Z",
    )

    assert result["ok"] is False
    assert result["block_reason"] == "official_nwp_context_policy_missing"
    assert result["writes_posterior"] is False


def test_official_policy_requires_a_complete_frozen_generation_reference(tmp_path: Path) -> None:
    db_path, fact_id = _source_only_db(tmp_path)

    result = materialize_amsc_posterior_task(
        _payload(
            fact_id,
            official_nwp_context_policy={
                "schema": "official_nwp_context_policy_v1",
                "mode": "prewarmed_cache_only_v1",
                "cache_generation_ref": {"schema": "official_nwp_multimodel_generation_v1"},
            },
        ),
        db_path=db_path,
        generated_at="2026-07-05T16:01:00Z",
    )

    assert result["ok"] is False
    assert result["block_reason"] == "official_nwp_context_policy_generation_ref_missing_or_invalid"
    assert result["writes_posterior"] is False
