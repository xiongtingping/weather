import json

from amsc_raw_history_archive import archive_amsc_poll
from scripts import amsc_raw_history_archive_audit as audit_script


def test_archive_audit_cli_reports_nonempty_append_only_archive(monkeypatch, capsys, tmp_path):
    archive_dir = tmp_path / "raw_history"
    archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=[
            {
                "source_id": "zuuu_metar_raw_amsc",
                "station_id": "ZUUU",
                "report_type": "METAR",
                "raw_text": "METAR ZUUU 051430Z 18003MPS CAVOK 32/20 Q1001 NOSIG=",
                "valid_time_utc": "2026-07-05T14:30:00Z",
                "valid_time_local": "2026-07-05T22:30:00+08:00",
            }
        ],
        poll_started_at="2026-07-05T14:30:55Z",
        response_completed_at="2026-07-05T14:31:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test_fetcher_v1",
    )
    monkeypatch.setattr(
        audit_script.sys,
        "argv",
        ["amsc_raw_history_archive_audit.py", "--archive-dir", str(archive_dir), "--require-nonempty", "--require-coverage"],
    )

    assert audit_script.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["object_count"] == 1
    assert report["manifest_count"] == 1
    assert report["coverage_manifest_count"] == 1
    assert report["guards"]["writes_archive_db"] is False
