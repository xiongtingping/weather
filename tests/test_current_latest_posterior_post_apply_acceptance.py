from pathlib import Path

from scripts import apply_posterior_ssot_fact_repair as repair
from scripts import current_latest_posterior_post_apply_acceptance as acceptance
from tests.test_apply_posterior_ssot_fact_repair import (
    _create_source_only_latest_db,
    _quiet_maintenance_packet,
)


def test_current_latest_posterior_post_apply_acceptance_requires_replacement(tmp_path: Path) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)

    report = acceptance.build_acceptance(
        db_path,
        candidate_fact_ids=[source_fact_id],
        max_items_output=5,
    )

    assert report["ok"] is False
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["candidate_count"] == 1
    assert "candidate_fact_still_latest" in report["blockers"]
    assert "latest_posterior_ssot_fact_not_ok" in report["blockers"]
    item = report["items"][0]
    assert item["candidate_fact_id"] == source_fact_id
    assert item["latest_replaced_candidate"] is False


def test_current_latest_posterior_post_apply_acceptance_passes_after_controlled_apply(
    tmp_path: Path,
) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)
    apply_result = repair.apply_posterior_ssot_fact_repair(
        db_path=db_path,
        fact_id=source_fact_id,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        allow_apply=True,
        confirm=repair.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-05T16:01:00Z",
        maintenance_window_packet=_quiet_maintenance_packet(db_path),
    )
    assert apply_result["ok"] is True

    report = acceptance.build_acceptance(
        db_path,
        candidate_fact_ids=[source_fact_id],
        expected_projections=[
            {
                "fact_id": source_fact_id,
                "target_bucket": "37°C",
                "target_probability": 1.0,
            }
        ],
        max_items_output=5,
    )

    assert report["ok"] is True
    assert report["expected_projection_source"] == "pre_apply_expected_projection"
    assert report["expected_projection_count"] == 1
    assert report["candidate_count"] == 1
    assert report["accepted_count"] == 1
    assert report["rejected_count"] == 0
    assert report["blockers"] == []
    item = report["items"][0]
    assert item["candidate_preserved_in_history"] is True
    assert item["latest_replaced_candidate"] is True
    assert item["latest_fact_id"] == apply_result["new_fact_id"]
    assert item["target_bucket_matches_projection"] is True
    assert item["target_probability_matches_projection"] is True
    assert item["latest_posterior"]["target_bucket"] == "37°C"
    assert item["latest_posterior"]["target_probability"] == 1.0


def test_current_latest_posterior_post_apply_acceptance_requires_candidate_ids(
    tmp_path: Path,
) -> None:
    db_path, _source_fact_id = _create_source_only_latest_db(tmp_path)

    report = acceptance.build_acceptance(db_path, candidate_fact_ids=[], max_items_output=5)

    assert report["ok"] is False
    assert report["candidate_count"] == 0
    assert report["accepted_count"] == 0
    assert "candidate_fact_ids_required" in report["blockers"]
