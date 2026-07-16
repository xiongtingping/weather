import json
import sqlite3

from amsc_raw_history_archive import archive_amsc_poll, raw_text_sha256
from scripts import build_trajectory_label_samples as sample_script
from scripts import evaluate_trajectory_shadow as evaluation_script
from resolution_final_source_runtime import PHYSICAL_SOURCE_ID


RAW = "METAR ZUUU 050300Z 00000MPS CAVOK 36/20 Q1000="


def test_read_only_sample_cli_binds_archived_raw_to_formal_label(monkeypatch, capsys, tmp_path):
    archive_dir = tmp_path / "raw_history"
    archive_result = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=[
            {
                "source_id": "zuuu_metar_raw_amsc",
                "station_id": "ZUUU",
                "report_type": "METAR",
                "raw_text": RAW,
                "valid_time_utc": "2026-07-05T03:00:00Z",
                "valid_time_local": "2026-07-05T11:00:00+08:00",
            }
        ],
        poll_started_at="2026-07-05T03:00:30Z",
        response_completed_at="2026-07-05T03:01:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test_fetcher_v1",
    )
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE source_observations (
            source_event_id TEXT, source_observation_id TEXT, source_id TEXT, source_class TEXT,
            station_id TEXT, event_date TEXT, valid_time TEXT, fetched_at TEXT, parsed_at TEXT,
            response_completed_at TEXT, temperature_c REAL, dewpoint_c REAL, raw_ref TEXT,
            raw_sha256 TEXT, parser_version TEXT, observation_json TEXT
        );
        CREATE TABLE event_runs (
            id INTEGER, event_date TEXT, station_id TEXT, market_closed INTEGER,
            settlement_actual REAL, resolution_source TEXT, created_at TEXT
        );
        CREATE TABLE event_fact_history (
            event_date TEXT, fact_as_of TEXT, fact_snapshot_id TEXT, id INTEGER, fact_json TEXT
        );
        """
    )
    payload = {
        "payload": {
            "raw_text": RAW,
            "first_seen_at": "2026-07-05T03:01:00Z",
            "arrival_time_quality": "observed_first_seen",
            "raw_object_ref": archive_result["raw_objects_by_sha256"][raw_text_sha256(RAW)]["raw_object_ref"],
        }
    }
    conn.execute(
        "INSERT INTO source_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "srcevt-1",
            "srcobs-1",
            "zuuu_metar_raw_amsc",
            "hard_observation_candidate",
            "ZUUU",
            "2026-07-05",
            "2026-07-05T03:00:00Z",
            "2026-07-05T03:01:00Z",
            "2026-07-05T03:01:00Z",
            "2026-07-05T03:01:00Z",
            36.0,
            20.0,
            RAW,
            raw_text_sha256(RAW),
            "amsc_zuuu_metar_taf_fetcher_v1",
            json.dumps(payload),
        ),
    )
    conn.execute(
        "INSERT INTO event_runs VALUES (?,?,?,?,?,?,?)",
        (7, "2026-07-05", "ZUUU", 1, 38.0, "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU", "2026-07-06T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO event_fact_history VALUES (?,?,?,?,?)",
        (
            "2026-07-05",
            "2026-07-05T03:05:00Z",
            "fact-1",
            1,
            json.dumps({"metar_strong_likelihood": {"35°C": 0.1, "36°C": 0.1, "37°C": 0.2, "38°C": 0.5, "39°C or higher": 0.1}}),
        ),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        sample_script.sys,
        "argv",
        [
            "build_trajectory_label_samples.py",
            "--event-date",
            "2026-07-05",
            "--cutoff-local",
            "11:05",
            "--db",
            str(db_path),
            "--archive-dir",
            str(archive_dir),
        ],
    )

    assert sample_script.main() == 0
    result = json.loads(capsys.readouterr().out)
    # One raw report is useful diagnostic lineage but cannot satisfy the
    # complete station-day/as-of coverage required for settlement evaluation.
    assert result["manifest"]["evaluation_sample_count"] == 0
    assert result["manifest"]["shadow_diagnostic_sample_count"] == 1
    assert result["samples"][0]["quality_grade"] == "B"
    assert result["samples"][0]["coverage_manifest_persisted"] is True
    assert result["samples"][0]["label"]["remaining_increment_t"] == 2.0
    assert result["samples"][0]["fact_snapshot_ids"] == ["fact-1"]
    assert result["samples"][0]["production_baseline"]["available"] is True
    assert result["samples"][0]["guards"]["writes_facts_sqlite"] is False

    monkeypatch.setattr(
        evaluation_script.sys,
        "argv",
        [
            "evaluate_trajectory_shadow.py",
            "--event-date",
            "2026-07-05",
            "--cutoff-local",
            "11:05",
            "--db",
            str(db_path),
            "--archive-dir",
            str(archive_dir),
        ],
    )
    assert evaluation_script.main() == 0
    evaluation = json.loads(capsys.readouterr().out)
    assert evaluation["evaluation"]["event_day_count"] == 0
    assert evaluation["evaluation"]["paired_baseline_event_day_count"] == 0
    assert evaluation["evaluation"]["performance_conclusion_allowed"] is False
    assert evaluation["guards"]["writes_posterior"] is False


def test_wu_fallback_label_requires_exact_resolution_source_and_post_event_settlement_tier(tmp_path):
    db_path = tmp_path / "labels.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE event_runs (
            id INTEGER, event_date TEXT, station_id TEXT, market_closed INTEGER,
            settlement_actual REAL, resolution_source TEXT, created_at TEXT
        );
        CREATE TABLE station_observations (
            id INTEGER, event_date TEXT, station_id TEXT, resolution_source TEXT,
            source_kind TEXT, trust_tier TEXT, value REAL, unit TEXT,
            observed_at TEXT, created_at TEXT
        );
        """
    )
    resolution = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"
    conn.execute(
        "INSERT INTO event_runs VALUES (?,?,?,?,?,?,?)",
        (8, "2026-07-05", "ZUUU", 0, None, resolution, "2026-07-05T23:00:00Z"),
    )
    conn.executemany(
        "INSERT INTO station_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "2026-07-05", "ZUUU", resolution, "wunderground_history", "settlement_source", 36.0, "C", "2026-07-05T06:00:00Z", "2026-07-06T01:00:00Z"),
            (2, "2026-07-05", "ZUUU", resolution, "wunderground_history", "settlement_source", 37.4, "C", "2026-07-05T09:00:00Z", "2026-07-06T01:00:00Z"),
            (3, "2026-07-05", "ZUUU", "https://different.example", "wunderground_history", "settlement_source", 45.0, "C", "2026-07-05T09:00:00Z", "2026-07-06T01:00:00Z"),
        ],
    )
    conn.commit()
    conn.row_factory = sqlite3.Row

    label = sample_script._resolution_source_wu_label(conn, event_date="2026-07-05", station_id="ZUUU")

    assert label is not None
    assert label["label_kind"] == "resolution_source_wu"
    assert label["final_daily_max_c"] == 37.4
    assert label["resolution_source"] == resolution
    assert label["label_source_observation_ids"] == ["stationobs_1", "stationobs_2"]
    assert label["label_finality"] == "source_matched_wu_history_post_event_max"
    conn.close()


def test_physical_x_label_reader_uses_closed_station_day_source_without_market_finality(tmp_path):
    db_path = tmp_path / "physical_final_sources.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE resolution_final_source_observations (
            source_observation_id TEXT, source_id TEXT, station_id TEXT,
            event_date TEXT, raw_ref TEXT, raw_sha256 TEXT,
            observation_json TEXT, created_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO resolution_final_source_observations VALUES (?,?,?,?,?,?,?,?)",
        (
            "physical-x-1", PHYSICAL_SOURCE_ID, "ZUUU", "2026-07-05", "coverage/ref.json", "a" * 64,
            json.dumps({"fetched_at": "2026-07-06T00:10:00Z", "payload": {"label_finality": "final", "final_physical_high_c": 36.5}}),
            "2026-07-06T00:10:00Z",
        ),
    )
    conn.commit()
    conn.close()

    label = sample_script._physical_final_x_label(
        final_source_db_path=db_path,
        event_date="2026-07-05",
        station_id="ZUUU",
    )

    assert label is not None
    assert label["final_physical_high_c"] == 36.5
    assert label["label_available_at"] == "2026-07-06T00:10:00Z"
    assert label["label_provenance"]["capture_scope"] == "physical_station_day_not_market_target"
