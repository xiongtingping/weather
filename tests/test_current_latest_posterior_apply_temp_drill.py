import sqlite3
import os
import time
from pathlib import Path

from scripts import current_latest_posterior_apply_temp_drill as drill
from tests.test_apply_posterior_ssot_fact_repair import _create_source_only_latest_db, _quiet_maintenance_packet


def _runbook_packet(source_fact_id: int) -> dict:
    return {
        "schema": "current_latest_posterior_apply_runbook_v1",
        "ok": True,
        "read_only": True,
        "ready_for_controlled_apply_now": False,
        "start_blockers": ["writer_quiescence_not_satisfied"],
        "risk_blockers": [],
        "scope": {
            "apply_required": True,
            "candidate_fact_ids": [source_fact_id],
        },
        "single_fact_preflights": [
            {
                "fact_id": source_fact_id,
                "ok": True,
                "risk_level": "controlled_low_after_operator_confirmation",
                "blockers": [],
                "slug": "highest-temperature-in-chengdu-on-july-5-2026",
                "event_date": "2026-07-05",
                "projected_target_bucket": "37°C",
                "projected_target_probability": 1.0,
                "uses_shared_backup": True,
            }
        ],
    }


def _fact_counts(db_path: Path) -> tuple[int, int]:
    with sqlite3.connect(db_path) as conn:
        fact_count = int(conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0])
        latest_fact_id = int(conn.execute("SELECT fact_id FROM event_fact_latest").fetchone()[0])
    return fact_count, latest_fact_id


def test_current_latest_posterior_temp_drill_applies_on_temp_only(monkeypatch, tmp_path: Path) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)
    before_counts = _fact_counts(db_path)

    captured: dict[str, object] = {}

    def _fake_build_runbook(*_args, **kwargs):
        captured["fact_ids"] = kwargs.get("fact_ids")
        return _runbook_packet(source_fact_id)

    monkeypatch.setattr(drill.runbook, "build_runbook", _fake_build_runbook)

    result = drill.run_drill(
        db_path=db_path,
        fact_ids=[source_fact_id],
        limit=10,
        max_items_output=5,
        keep_temp=False,
    )

    assert result["ok"] is True
    assert result["read_only_for_production"] is True
    assert result["production_counts_unchanged"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_temp_archive_db"] is True
    assert result["source_runbook"]["candidate_fact_ids"] == [source_fact_id]
    assert captured["fact_ids"] == [source_fact_id]
    assert result["all_candidates_applied_on_temp"] is True
    assert result["apply_results"][0]["candidate_fact_id"] == source_fact_id
    assert result["apply_results"][0]["applied"] is True
    assert result["apply_results"][0]["target_bucket"] == "37°C"
    assert result["apply_results"][0]["maintenance_window_writer_quiescent"] is True
    assert result["post_apply_exact_acceptance"]["ok"] is True
    assert result["post_apply_exact_acceptance"]["candidate_count"] == 1
    assert result["post_apply_exact_acceptance"]["expected_projection_count"] == 1
    assert result["post_apply_exact_acceptance"]["expected_projection_source"] == "pre_apply_expected_projection"
    assert result["post_apply_exact_acceptance"]["accepted_count"] == 1
    assert result["post_apply_exact_acceptance"]["rejected_count"] == 0
    assert result["post_apply_exact_acceptance"]["items"][0]["candidate_fact_id"] == source_fact_id
    assert result["post_apply_exact_acceptance"]["items"][0]["latest_replaced_candidate"] is True
    assert result["post_apply_rematerialization"]["current_latest_issue_count"] == 0
    assert _fact_counts(db_path) == before_counts


def test_current_latest_posterior_temp_drill_treats_empty_runbook_as_noop(monkeypatch, tmp_path: Path) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)
    seed_result = drill.repair.apply_posterior_ssot_fact_repair(
        db_path=db_path,
        fact_id=source_fact_id,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        allow_apply=True,
        confirm=drill.repair.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        backup_dir=tmp_path / "seed_backups",
        generated_at="2026-07-05T16:01:00Z",
        maintenance_window_packet=_quiet_maintenance_packet(db_path),
    )
    assert seed_result["ok"] is True
    before_counts = _fact_counts(db_path)

    monkeypatch.setattr(
        drill.runbook,
        "build_runbook",
        lambda *_args, **_kwargs: {
            "schema": "current_latest_posterior_apply_runbook_v1",
            "ok": True,
            "read_only": True,
            "scope": {"apply_required": False, "candidate_fact_ids": []},
            "single_fact_preflights": [],
            "start_blockers": [],
            "risk_blockers": [],
        },
    )

    result = drill.run_drill(
        db_path=db_path,
        limit=10,
        max_items_output=5,
        keep_temp=False,
    )

    assert result["ok"] is True
    assert result["candidate_count"] == 0
    assert result["all_candidates_applied_on_temp"] is True
    assert result["post_apply_exact_acceptance"]["ok"] is True
    assert result["post_apply_exact_acceptance"]["skipped"] is True
    assert result["post_apply_exact_acceptance"]["skip_reason"] == "no_current_latest_candidates"
    assert result["next_boundary"] == "no_current_latest_posterior_apply_required"
    assert _fact_counts(db_path) == before_counts


def test_current_latest_posterior_temp_drill_uses_scoped_copy_when_full_copy_disk_low(monkeypatch, tmp_path: Path) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)
    before_counts = _fact_counts(db_path)

    monkeypatch.setattr(
        drill.runbook,
        "build_runbook",
        lambda *_args, **_kwargs: _runbook_packet(source_fact_id),
    )
    monkeypatch.setattr(
        drill,
        "_temp_drill_disk_preflight",
        lambda _db_path: {
            "schema": "current_latest_posterior_temp_drill_disk_preflight_v1",
            "ok": False,
            "free_bytes": 1,
            "required_free_bytes": 999,
            "block_reason": "insufficient_temp_drill_disk_space",
        },
    )

    result = drill.run_drill(
        db_path=db_path,
        fact_ids=[source_fact_id],
        limit=10,
        max_items_output=5,
        keep_temp=False,
    )

    assert result["ok"] is True
    assert result["execution_mode"] == "current_latest_posterior_apply_temp_drill"
    assert result["read_only_for_production"] is True
    assert result["candidate_count"] == 1
    assert result["temp_copy_fallback_used"] is True
    assert result["temp_copy_fallback_reason"] == "full_temp_copy_insufficient_disk_space"
    assert result["temp_copy_mode"] == "scoped_same_slug_event_date"
    assert result["temp_copy_disk_preflight"]["required_free_bytes"] == 999
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_temp_archive_db"] is True
    assert result["all_candidates_applied_on_temp"] is True
    assert _fact_counts(db_path) == before_counts


def test_cleanup_stale_temp_dirs_only_removes_drill_prefixes(tmp_path: Path) -> None:
    stale = tmp_path / "weather_current_latest_posterior_drill_old"
    scoped = tmp_path / "weather_current_latest_posterior_drill_scoped_old"
    unrelated = tmp_path / "weather_current_latest_other"
    fresh = tmp_path / "weather_current_latest_posterior_drill_fresh"
    for path in (stale, scoped, unrelated, fresh):
        path.mkdir()
        (path / "payload.db").write_text("x" * 1024)

    old_mtime = time.time() - 7200
    for path in (stale, scoped, unrelated):
        os.utime(path, (old_mtime, old_mtime))

    report = drill.cleanup_stale_temp_dirs(
        temp_root=tmp_path,
        min_age_seconds=3600,
        apply=True,
        max_items_output=10,
    )

    assert report["ok"] is True
    assert report["eligible_count"] == 2
    assert report["removed_count"] == 2
    assert not stale.exists()
    assert not scoped.exists()
    assert unrelated.exists()
    assert fresh.exists()
    assert report["boundary"] == "only_directories_with_current_latest_posterior_temp_drill_prefix_under_temp_root"


def test_cleanup_only_cli_reports_no_temp_backup(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(drill.tempfile, "gettempdir", lambda: str(tmp_path))

    code = drill.main(["--cleanup-only", "--max-items-output", "5"])
    out = capsys.readouterr().out

    assert code == 0
    assert '"execution_mode": "current_latest_posterior_temp_drill_cleanup_only"' in out
    assert '"creates_temp_backup": false' in out
    assert '"writes_temp_archive_db": false' in out
