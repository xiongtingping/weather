import json
import sqlite3
from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "edge_realizable_calibration_coverage",
    ROOT / "scripts" / "edge_realizable_calibration_coverage.py",
)
coverage = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(coverage)


def test_coverage_report_splits_component_bucket_gaps(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    calibration_path = tmp_path / "edge_realizable_calibration.json"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            action TEXT,
            details_json TEXT,
            created_at TEXT
        )
        """
    )
    calibration_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-05-05T00:00:00+00:00",
                "fill_mode": "ticks",
                "p_converge_buckets": {
                    "conv-ok": {"p": 0.6, "hit": 30, "total": 50}
                },
                "p_fill_entry_buckets": {
                    "entry-low": {"p": 0.5, "filled": 2, "total": 3}
                },
                "p_fill_exit_buckets": {},
            }
        ),
        encoding="utf-8",
    )
    details = {
        "entry_strategy_family": "repricing_bucket",
        "weather_strategy_exit_plan": "repricing_exit_target",
        "entry_edge_repricing_exit_evidence_status": "default_only",
        "entry_edge_repricing_exit_evidence_missing": True,
        "entry_edge_repricing_exit_evidence_missing_tables": "p_fill_exit_buckets",
        "entry_edge_repricing_exit_evidence_default_tables": "p_fill_exit_buckets",
        "edge_realizable_p_converge_evidence_status": "bucket_evidence",
        "edge_realizable_p_converge_samples": 50,
        "edge_realizable_p_converge_bucket_key": "conv-ok",
        "edge_realizable_p_fill_entry_evidence_status": "low_sample",
        "edge_realizable_p_fill_entry_samples": 3,
        "edge_realizable_p_fill_entry_bucket_key": "entry-low",
        "edge_realizable_p_fill_exit_evidence_status": "default_only",
        "edge_realizable_p_fill_exit_bucket_key": "exit-missing",
    }
    conn.execute(
        "INSERT INTO auto_trade_decisions(run_id, action, details_json, created_at) VALUES (?, ?, ?, ?)",
        (42, "entry", json.dumps(details), "2026-05-05T12:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    report = coverage.build_report(
        db_path=db_path,
        calibration_path=calibration_path,
        days=30,
        min_samples=20,
        limit=100,
    )

    overall = report["overall"]
    assert overall["repricing_candidate_count"] == 1
    assert overall["missing_count"] == 1
    assert overall["missing_table_top"][0] == {"key": "p_fill_exit_buckets", "count": 1}

    components = overall["components"]
    assert components["p_converge"]["bucket_evidence_count"] == 1
    assert components["p_fill_entry"]["low_sample_count"] >= 1
    assert components["p_fill_exit"]["default_only_count"] == 1
    assert components["p_fill_exit"]["bucket_lookup_missing_count"] == 1
    assert components["p_fill_exit"]["missing_bucket_top"][0] == {
        "key": "exit-missing",
        "count": 1,
    }


def test_coverage_report_can_scope_to_latest_run(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    calibration_path = tmp_path / "edge_realizable_calibration.json"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            action TEXT,
            details_json TEXT,
            created_at TEXT
        )
        """
    )
    calibration_path.write_text(
        json.dumps(
            {
                "p_converge_buckets": {},
                "p_fill_entry_buckets": {},
                "p_fill_exit_buckets": {},
            }
        ),
        encoding="utf-8",
    )
    old_details = {
        "entry_strategy_family": "repricing_bucket",
        "weather_strategy_exit_plan": "repricing_exit_target",
        "entry_edge_repricing_exit_evidence_status": "default_only",
        "entry_edge_repricing_exit_evidence_missing": True,
        "edge_realizable_p_realize_missing": True,
    }
    latest_details = {
        "entry_strategy_family": "repricing_bucket",
        "weather_strategy_exit_plan": "repricing_exit_target",
        "entry_edge_repricing_exit_evidence_status": "bucket_evidence",
        "edge_realizable_p_converge_evidence_status": "bucket_evidence",
        "edge_realizable_p_fill_entry_evidence_status": "bucket_evidence",
        "edge_realizable_p_fill_exit_evidence_status": "bucket_evidence",
    }
    conn.execute(
        "INSERT INTO auto_trade_decisions(run_id, action, details_json, created_at) VALUES (?, ?, ?, ?)",
        (41, "entry", json.dumps(old_details), "2026-05-05T12:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions(run_id, action, details_json, created_at) VALUES (?, ?, ?, ?)",
        (42, "entry", json.dumps(latest_details), "2026-05-05T12:01:00+00:00"),
    )
    conn.commit()
    conn.close()

    report = coverage.build_report(
        db_path=db_path,
        calibration_path=calibration_path,
        days=30,
        min_samples=20,
        limit=100,
        latest_run=True,
    )

    assert report["window"]["run_id"] == 42
    assert report["overall"]["repricing_candidate_count"] == 1
    assert report["overall"]["missing_count"] == 0
    assert report["overall"]["bucket_evidence_count"] == 1


def test_coverage_report_normalizes_p_realize_flags_to_status(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    calibration_path = tmp_path / "edge_realizable_calibration.json"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            action TEXT,
            details_json TEXT,
            created_at TEXT
        )
        """
    )
    calibration_path.write_text(
        json.dumps(
            {
                "p_converge_buckets": {},
                "p_fill_entry_buckets": {},
                "p_fill_exit_buckets": {},
            }
        ),
        encoding="utf-8",
    )
    details = {
        "entry_strategy_family": "repricing_bucket",
        "weather_strategy_exit_plan": "repricing_exit_target",
        "edge_realizable_p_realize_missing": True,
        "edge_realizable_p_realize_uses_default": True,
    }
    conn.execute(
        "INSERT INTO auto_trade_decisions(run_id, action, details_json, created_at) VALUES (?, ?, ?, ?)",
        (42, "entry", json.dumps(details), "2026-05-05T12:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    report = coverage.build_report(
        db_path=db_path,
        calibration_path=calibration_path,
        days=30,
        min_samples=20,
        limit=100,
    )

    overall = report["overall"]
    assert overall["repricing_candidate_count"] == 1
    assert overall["missing_count"] == 1
    assert overall["default_only_count"] == 1
    assert overall["status_top"][0] == {"key": "default_only", "count": 1}
