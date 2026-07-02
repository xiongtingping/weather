from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_ctp_direct_replacement.py"


def load_audit_module():
    spec = importlib.util.spec_from_file_location("check_ctp_direct_replacement", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_db(db_path: Path, payload: dict) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            fact_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_id INTEGER NOT NULL
        )
        """
    )
    cur = conn.execute(
        """
        INSERT INTO event_fact_history(slug, event_date, fact_as_of, fact_json)
        VALUES (?, ?, ?, ?)
        """,
        (
            payload["slug"],
            payload["event_date"],
            "2026-06-29T08:00:00+00:00",
            json.dumps(payload),
        ),
    )
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id) VALUES (?, ?, ?)",
        (payload["slug"], payload["event_date"], cur.lastrowid),
    )
    conn.commit()
    conn.close()


def sample_payload(with_ctp: bool) -> dict:
    payload = {
        "slug": "highest-temperature-in-chengdu-on-june-29-2026",
        "event_date": "2026-06-29",
        "distribution": [
            {"outcome": "31°C", "q_temperature_bucket_posterior": 0.6},
            {"outcome": "32°C", "q_temperature_bucket_posterior": 0.4},
        ],
    }
    if with_ctp:
        payload["posterior_temperature_distribution"] = {
            "grid": [{"temp_c": 31.0, "density": 1.0}],
            "cdf_values": [{"temp_c": 31.0, "cdf": 1.0}],
            "quantiles_c": {"p50": 31.0},
            "quantile_grid": [{"q": 0.5, "temp_c": 31.0}],
            "bucket_probabilities": [{"outcome": "31°C", "probability": 1.0}],
        }
    return payload


def test_latest_fact_ctp_coverage_fails_when_distribution_missing(tmp_path: Path) -> None:
    module = load_audit_module()
    db_path = tmp_path / "archive.db"
    create_db(db_path, sample_payload(with_ctp=False))

    issues = module.audit_latest_fact_ctp_coverage(db_path)

    assert [issue["code"] for issue in issues] == ["posterior_temperature_distribution_coverage_incomplete"]
    assert "1/1" in issues[0]["detail"]


def test_latest_fact_ctp_coverage_passes_when_distribution_complete(tmp_path: Path) -> None:
    module = load_audit_module()
    db_path = tmp_path / "archive.db"
    create_db(db_path, sample_payload(with_ctp=True))

    assert module.audit_latest_fact_ctp_coverage(db_path) == []
