from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from production_probability_activation_contract import activation_contract_sha256
from scripts.probability_chain_freshness_probe import _source_revision_rows, probe_chain


SLUG = "highest-temperature-in-chengdu-on-july-21-2026"
DATE = "2026-07-21"
TARGET = "physical_weather_day_v1:ZUUU:2026-07-21"
CHECKPOINT = "checkpoint-1"
SNAPSHOT = "snapshot-1"
FACT_SNAPSHOT = "fact-1"


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _db(*, source="2026-01-01T00:09:30Z", snapshot="2026-01-01T00:09:40Z", checkpoint="2026-01-01T00:09:50Z", fact="2026-01-01T00:09:55Z", ssot_checkpoint=CHECKPOINT):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE source_observations(
          source_event_id TEXT, source_id TEXT, event_date TEXT, response_completed_at TEXT,
          fetched_at TEXT, parsed_at TEXT, created_at TEXT, raw_ref TEXT, raw_sha256 TEXT,
          observation_json TEXT
        );
        CREATE TABLE weak_forecast_evidence_snapshots(
          forecast_evidence_snapshot_id TEXT, target_identity TEXT, event_date TEXT,
          decision_as_of TEXT, created_at TEXT, snapshot_json TEXT, snapshot_sha256 TEXT,
          source_revision_ids_json TEXT
        );
        CREATE TABLE posterior_checkpoints(
          posterior_checkpoint_id TEXT, target_identity TEXT, posterior_run_id TEXT,
          forecast_evidence_snapshot_id TEXT, joint_posterior_json TEXT,
          joint_posterior_sha256 TEXT, committed_at TEXT, created_at TEXT, replay_generation INTEGER
        );
        CREATE TABLE event_fact_latest(
          slug TEXT, event_date TEXT, fact_id INTEGER, updated_at TEXT, fact_snapshot_id TEXT
        );
        CREATE TABLE event_fact_history(
          id INTEGER, slug TEXT, event_date TEXT, fact_as_of TEXT, created_at TEXT,
          fact_snapshot_id TEXT, posterior_run_id TEXT, fact_json TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO source_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("src-1", "gfs", DATE, source, source, source, source, "nwprev_src-1.json", "a" * 64, "{}"),
    )
    snapshot_payload = {"schema": "test", "forecast_evidence_snapshot_id": SNAPSHOT}
    conn.execute(
        "INSERT INTO weak_forecast_evidence_snapshots VALUES(?,?,?,?,?,?,?,?)",
        (SNAPSHOT, TARGET, DATE, snapshot, snapshot, json.dumps(snapshot_payload), _sha(snapshot_payload), json.dumps(["src-1"])),
    )
    posterior_payload = [{"x": 34, "probability": 1.0}]
    conn.execute(
        "INSERT INTO posterior_checkpoints VALUES(?,?,?,?,?,?,?,?,?)",
        (CHECKPOINT, TARGET, "run-1", SNAPSHOT, json.dumps(posterior_payload), _sha(posterior_payload), checkpoint, checkpoint, 1),
    )
    fact_payload = {
        "posterior_ssot_fact": {
            "posterior_checkpoint_id": ssot_checkpoint,
            "posterior_run_id": "run-1",
            "fact_snapshot_id": FACT_SNAPSHOT,
        }
    }
    conn.execute(
        "INSERT INTO event_fact_history VALUES(?,?,?,?,?,?,?,?)",
        (1, SLUG, DATE, fact, fact, FACT_SNAPSHOT, "run-1", json.dumps(fact_payload)),
    )
    conn.execute("INSERT INTO event_fact_latest VALUES(?,?,?,?,?)", (SLUG, DATE, 1, fact, FACT_SNAPSHOT))
    conn.commit()
    return conn


def _activation(runtime="r" * 64):
    payload = {"runtime_manifest_sha256": runtime}
    payload["activation_contract_sha256"] = activation_contract_sha256(payload)
    return payload


def _ui(**overrides):
    payload = {
        "slug": SLUG,
        "event_date": DATE,
        "fact_id": 1,
        "fact_snapshot_id": FACT_SNAPSHOT,
        "posterior_checkpoint_id": CHECKPOINT,
    }
    payload.update(overrides)
    return payload


def _probe(conn, **kwargs):
    options = {
        "connection": conn,
        "now": datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
        "activation_payload": _activation(),
        "runtime_manifest_sha256": "r" * 64,
        "ui_fetcher": lambda _url, _timeout: (_ui(), {"ok": True, "http_status": 200}),
    }
    options.update(kwargs)
    return probe_chain(
        **options,
    )


def test_fresh_chain_reports_one_compact_state():
    report = _probe(_db())
    assert report["state"] == "fresh"
    assert report["broken_at"] is None
    assert report["latest_checkpoint"] == CHECKPOINT
    assert report["latest_fact"] == FACT_SNAPSHOT
    assert report["source"]["hash_match"] is True
    assert report["snapshot"]["hash_match"] is True
    assert report["checkpoint"]["hash_match"] is True
    assert report["ui"]["hash_match"] is True


def test_source_age_boundary_is_stale():
    report = _probe(_db(source="2026-01-01T00:08:00Z"))
    assert report["state"] == "stale"
    assert report["broken_at"] == "source"
    assert "source_stale" in {item["reason"] for item in report["blockers"]}


def test_snapshot_before_source_is_stale():
    report = _probe(_db(source="2026-01-01T00:09:50Z"))
    assert report["state"] == "stale"
    assert report["broken_at"] == "snapshot"
    assert report["snapshot"]["blocker"] == "snapshot_stale"


def test_hard_metar_after_forecast_source_does_not_make_snapshot_stale():
    conn = _db()
    conn.execute(
        "INSERT INTO source_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("metar-2", "zuuu_metar_raw_amsc", DATE, "2026-01-01T00:09:59Z",
         "2026-01-01T00:09:59Z", "2026-01-01T00:09:59Z", "2026-01-01T00:09:59Z",
         "metar-2.json", "b" * 64, "{}"),
    )
    conn.commit()
    report = _probe(conn)
    assert report["state"] == "fresh"
    assert report["source"]["selected_source_ids"] == ["gfs"]


def test_checkpoint_before_snapshot_is_stale():
    report = _probe(_db(checkpoint="2026-01-01T00:09:30Z"))
    assert report["state"] == "stale"
    assert report["broken_at"] == "checkpoint"
    assert report["checkpoint"]["blocker"] == "posterior_stale"


def test_fact_pointer_mismatch_is_ssot_stale():
    report = _probe(_db(ssot_checkpoint="checkpoint-old"))
    assert report["state"] == "stale"
    assert report["broken_at"] == "fact_latest"
    assert report["fact_latest"]["blocker"] == "ssot_stale"


def test_manifest_drift_is_blocked():
    report = _probe(_db(), runtime_manifest_sha256="x" * 64)
    assert report["state"] == "blocked"
    assert report["broken_at"] == "manifest"
    assert report["manifest"]["blocker"] == "manifest_drift"


def test_ui_payload_mismatch_is_ui_stale():
    report = _probe(_db(), ui_fetcher=lambda _url, _timeout: (_ui(fact_id=999), {"ok": True, "http_status": 200}))
    assert report["state"] == "stale"
    assert report["broken_at"] == "ui"
    assert report["ui"]["blocker"] == "ui_stale"


def test_ui_timeout_is_ui_stale():
    report = _probe(_db(), ui_fetcher=lambda _url, _timeout: (None, {"ok": False, "error": "timeout"}))
    assert report["state"] == "stale"
    assert report["broken_at"] == "ui"
    assert report["ui"]["blocker"] == "ui_stale"


def test_ui_check_can_be_skipped_without_creating_a_ui_break():
    report = _probe(_db(), include_ui=False)
    assert report["state"] == "fresh"
    assert report["broken_at"] is None
    assert report["ui"]["blocker"] == "ui_unchecked"
    assert report["ui"]["fetch"]["skipped"] is True


def test_ui_summary_primary_identity_aliases_are_accepted():
    report = _probe(
        _db(),
        ui_fetcher=lambda _url, _timeout: (
            {
                "primary_fact_id": 1,
                "primary_fact_snapshot_id": FACT_SNAPSHOT,
                "event_date": DATE,
                "slug": SLUG,
            },
            {"ok": True, "http_status": 200},
        ),
    )
    assert report["state"] == "fresh"
    assert report["ui"]["hash_match"] is True


def test_multicity_source_resolution_is_station_scoped():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE source_observations(
          source_event_id TEXT, source_id TEXT, station_id TEXT, event_date TEXT,
          response_completed_at TEXT, fetched_at TEXT, parsed_at TEXT, created_at TEXT,
          raw_ref TEXT, raw_sha256 TEXT, observation_json TEXT
        );
        CREATE TABLE official_nwp_station_snapshots(
          snapshot_id TEXT, provider_id TEXT, station_id TEXT, event_date TEXT,
          valid_time_utc TEXT, source_snapshot_sha256 TEXT, state TEXT, created_at TEXT
        );
        CREATE TABLE amsc_taf_snapshots(
          source_revision_id TEXT, station_id TEXT, event_date TEXT, state TEXT,
          issue_time_utc TEXT, raw_sha256 TEXT, created_at TEXT
        );
        """
    )
    for station, digest in (("ZUUU", "u" * 64), ("ZSPD", "s" * 64)):
        conn.execute(
            "INSERT INTO official_nwp_station_snapshots VALUES(?,?,?,?,?,?,?,?)",
            (f"{station}-ecmwf", "ecmwf", station, DATE, "2026-01-01T12:00:00Z", digest, "available", "2026-01-01T00:09:30Z"),
        )
    conn.execute(
        "INSERT INTO amsc_taf_snapshots VALUES(?,?,?,?,?,?,?)",
        ("tafrev_zspd", "ZSPD", DATE, "available", "2026-01-01T00:05:00Z", "t" * 64, "2026-01-01T00:09:30Z"),
    )
    snapshot = {
        "target_identity": "physical_weather_day_v1:ZSPD:" + DATE,
        "source_revision_ids_json": json.dumps(["multicity-zspd-ecmwf-placeholder", "tafrev_zspd"]),
        "source_revisions_json": json.dumps([
            {"source_id": "ecmwf", "source_revision_id": "multicity-zspd-ecmwf-placeholder", "raw_sha256": ""},
            {"source_id": "taf", "source_revision_id": "tafrev_zspd", "raw_sha256": "t" * 64},
        ]),
    }
    rows, _ids, metadata = _source_revision_rows(
        conn,
        event_date=DATE,
        snapshot=snapshot,
        clock=datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
    )
    assert metadata["station_id"] == "ZSPD"
    assert {row["source_id"] for row in rows} == {"ecmwf", "taf"}
    nwp = next(row for row in rows if row["source_id"] == "ecmwf")
    assert nwp["source_snapshot_ids"] == ["ZSPD-ecmwf"]
