"""Incremental PR12 evidence export (2026-09-19, 「做增量式证据导出」).

The identity pass read ~8,400 AWC rows every time and hashed each one, then
dropped the 87% that belong to station-days canonical SSOT has already closed.
Closed days are now located from the key columns alone and never hashed, and
the stored-revision lookup narrows on the scope-key index instead of running
json_extract over every revision fact.  The packet the sweep seals must be the
one the full read sealed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bayes_basket_target.awc_observation_builder import export_awc_ingress_evidence_v4
from bayes_basket_target.awc_observation_contract import AWC_ORIGIN_URL
from bayes_basket_target.canonical_ssot_v12_migration import (
    TARGET_TABLE_DDL as V12_TABLE_DDL,
    TARGET_TRIGGER_DDL as V12_TRIGGER_DDL,
)
from bayes_basket_target.contracts import ContractError
from tests.pr12_authority_projection_test_support import authority_projection

ZONE = "America/New_York"
SINCE = "2026-08-02T00:00:00.000Z"
AS_OF = "2026-08-03T20:00:00.000Z"


def _script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_pr12_awc_observation_contract.py"
    spec = importlib.util.spec_from_file_location("build_pr12_incr", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _archive(path: Path, *, corrupt: str = "") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE reported_trajectory_observations (
          observation_id TEXT PRIMARY KEY, source_lane TEXT NOT NULL, station_id TEXT NOT NULL,
          report_type TEXT NOT NULL, valid_at TEXT NOT NULL, reported_temperature_c INTEGER NOT NULL,
          raw_sha256 TEXT NOT NULL, raw_text TEXT NOT NULL, first_seen_at TEXT NOT NULL,
          parsed_at TEXT NOT NULL, canonical_committed_at TEXT NOT NULL, fetch_started_at TEXT NOT NULL,
          fetch_completed_at TEXT NOT NULL, source_poll_id TEXT NOT NULL,
          correction_suspected INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE reported_trajectory_source_polls (
          poll_id TEXT PRIMARY KEY, source_lane TEXT NOT NULL, station_ids_json TEXT NOT NULL,
          request_started_at TEXT NOT NULL, response_started_at TEXT, response_completed_at TEXT NOT NULL,
          response_status INTEGER, duration_ms REAL NOT NULL, report_count INTEGER NOT NULL,
          new_report_count INTEGER NOT NULL, error_code TEXT, poll_json TEXT NOT NULL);
        CREATE TABLE reported_trajectory_observation_origins (
          observation_id TEXT PRIMARY KEY, source_lane TEXT NOT NULL, station_id TEXT NOT NULL,
          origin_source_id TEXT NOT NULL, origin_country_code TEXT NOT NULL,
          origin_authority TEXT NOT NULL, origin_url TEXT NOT NULL, origin_transport TEXT NOT NULL,
          direct_country_official INTEGER NOT NULL, fallback_used INTEGER NOT NULL,
          fallback_reason TEXT NOT NULL, preferred_origin_source_id TEXT NOT NULL,
          preferred_origin_authority TEXT NOT NULL, official_source_registry_sha256 TEXT NOT NULL,
          origin_json TEXT NOT NULL, committed_at TEXT NOT NULL);
        CREATE TABLE reported_trajectory_station_halts (
          source_lane TEXT NOT NULL, station_id TEXT NOT NULL, event_date TEXT NOT NULL,
          state TEXT NOT NULL, reason_code TEXT NOT NULL, triggering_observation_id TEXT NOT NULL,
          created_at TEXT NOT NULL, cleared_at TEXT, PRIMARY KEY(source_lane,station_id,event_date));
        CREATE TABLE reported_trajectory_station_halt_reviews (
          review_id TEXT PRIMARY KEY, source_lane TEXT NOT NULL, station_id TEXT NOT NULL,
          event_date TEXT NOT NULL, triggering_observation_id TEXT NOT NULL, decision TEXT NOT NULL,
          reviewer TEXT NOT NULL, rationale TEXT NOT NULL, reviewed_at TEXT NOT NULL,
          supersedes_review_id TEXT, evidence_json TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
          content_sha256 TEXT NOT NULL UNIQUE);
        """
    )
    start = datetime(2026, 8, 1, 4, tzinfo=timezone.utc)  # 00:00 New York
    # KJFK reports hourly for three local days; T000 only on the first.
    for station, hours in (("KJFK", 72), ("T000", 24)):
        for hour in range(hours):
            valid = start + timedelta(hours=hour)
            obs_id = f"{station}-{hour}"
            # Two reports share each poll: the payload is hashed once.
            poll_id = f"poll-{station}-{hour // 2}"
            raw = f"METAR {station} {valid:%d%H%M}Z 00000KT CAVOK 25/10 A3000"
            sha = hashlib.sha256(raw.encode()).hexdigest()
            if obs_id == corrupt:
                sha = "0" * 64
            first = valid + timedelta(minutes=1)
            conn.execute(
                "INSERT INTO reported_trajectory_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (obs_id, "awc", station, "METAR", _stamp(valid), 25, sha, raw, _stamp(first),
                 _stamp(first + timedelta(milliseconds=100)), _stamp(first + timedelta(seconds=1)),
                 _stamp(valid + timedelta(seconds=30)), _stamp(first), poll_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO reported_trajectory_source_polls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (poll_id, "awc", json.dumps([station]), _stamp(valid), _stamp(valid),
                 _stamp(first), 200, 800.0, 2, 1, None,
                 json.dumps({"reports": [{"raw": raw, "n": hour}], "station": station})),
            )
            conn.execute(
                "INSERT INTO reported_trajectory_observation_origins VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (obs_id, "awc", station, "global_noaa_awc_relay", "US",
                 "NOAA/NWS Aviation Weather Center worldwide relay", AWC_ORIGIN_URL,
                 "official_awc_raw_metar_batch_direct", 0, 0, "", "", "", "a" * 64, "{}",
                 _stamp(first + timedelta(milliseconds=500))),
            )
    conn.commit()
    conn.close()


def _registry(projection: dict) -> dict:
    return {
        row["station_id"]: {
            "station_id": row["station_id"],
            "settlement_definition": "official_daily_tmax_c",
            "content_sha256": "b" * 64,
        }
        for row in projection["rows"]
    }


def _sealed(archive: Path, closed: dict, *, incremental: bool) -> tuple[dict, dict]:
    script = _script()
    projection = authority_projection(primary_station="KJFK", settlement_timezone=ZONE)
    raw = export_awc_ingress_evidence_v4(
        archive,
        station_registry=_registry(projection),
        authority_scope_projection=projection,
        as_of_utc=AS_OF,
        since_valid_at_utc=SINCE,
        skip_day_scopes=closed if incremental else None,
    )
    sealed = script._exclude_materialized_day_scopes(
        Path("/unused"), raw, projection["content_sha256"], existing=closed
    )
    return raw, sealed


def test_the_sealed_packet_is_the_one_the_full_read_made(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    _archive(archive)
    closed = {("KJFK", "2026-08-01"): ZONE, ("T000", "2026-08-01"): ZONE}
    full_raw, full = _sealed(archive, closed, incremental=False)
    incr_raw, incr = _sealed(archive, closed, incremental=True)

    assert incr == full
    assert json.dumps(incr, sort_keys=True) == json.dumps(full, sort_keys=True)
    # The closed day was dropped either way; the incremental read never hashed it.
    assert {r["event_date"] for r in full["rows"]} == {"2026-08-02", "2026-08-03"}
    assert len(incr_raw["rows"]) < len(full_raw["rows"])
    assert incr_raw["read_skipped_day_scopes"]["rows_located_by_key_only"] == 48
    assert full["materialized_day_scope_exclusion"]["excluded_evidence_row_count"] == 48
    # T000's only reports are closed: it is present, not DATA_MISSING, in both.
    missing = {g["station_id"] for g in incr["extraction_gaps"] if g["failure_code"] == "DATA_MISSING"}
    assert "T000" not in missing and "KJFK" not in missing
    assert "read_skipped_day_scopes" not in incr


def test_nothing_closed_is_the_full_read(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    _archive(archive)
    _, full = _sealed(archive, {}, incremental=False)
    _, incr = _sealed(archive, {}, incremental=True)
    assert incr == full


def test_a_closed_day_under_another_zone_still_fails_closed(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    _archive(archive)
    with pytest.raises(ContractError, match="pr12_existing_day_timezone_conflict:KJFK:2026-08-01"):
        _sealed(archive, {("KJFK", "2026-08-01"): "UTC"}, incremental=True)


def _row_gaps(evidence: dict) -> list[str]:
    return [g["observation_id"] for g in evidence["extraction_gaps"] if g["failure_code"] != "DATA_MISSING"]


def test_a_closed_days_report_is_not_reverified(tmp_path: Path) -> None:
    """The one narrowing, pinned: a corrupt row in a committed day no longer
    raises a gap each pass.  In an open day it still does."""

    archive = tmp_path / "archive.db"
    _archive(archive, corrupt="KJFK-3")          # 2026-08-01 local, closed
    closed = {("KJFK", "2026-08-01"): ZONE}
    _, full = _sealed(archive, closed, incremental=False)
    _, incr = _sealed(archive, closed, incremental=True)
    assert _row_gaps(full) == ["KJFK-3"]
    assert _row_gaps(incr) == []
    assert incr["rows"] == full["rows"]

    archive2 = tmp_path / "archive2.db"
    _archive(archive2, corrupt="KJFK-30")        # 2026-08-02 local, open
    _, incr2 = _sealed(archive2, closed, incremental=True)
    assert _row_gaps(incr2) == ["KJFK-30"]


def _target_with_facts(path: Path, facts: list[tuple[str, dict]]) -> None:
    conn = sqlite3.connect(path)
    conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
    conn.execute(V12_TABLE_DDL)
    for ddl in V12_TRIGGER_DDL:
        conn.execute(ddl)
    for index, (scope_key, body) in enumerate(facts):
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES (?,?,?,?,?,?,?)",
            (f"f{index}", "truth_observation_revision", scope_key, f"{index + 1:064x}",
             json.dumps(body), "{}", "2026-08-03T00:00:00Z"),
        )
    conn.commit()
    conn.close()


def test_stored_revisions_narrow_on_the_scope_key_and_match_the_body_query(tmp_path: Path) -> None:
    script = _script()
    sha, other = "p" * 64, "q" * 64

    def fact(station, obs, observed, day, projection=sha, *, key_parts=6):
        body = {"station_id": station, "observation_id": obs, "revision_no": 1,
                "observed_at_utc": observed, "event_date": day,
                "authority_scope_projection_content_sha256": projection}
        parts = [station, obs, "1", observed, observed, projection][:key_parts]
        return "|".join(parts), body

    facts = [
        fact("KJFK", "a", "2026-08-02T05:00:00.000Z", "2026-08-02"),           # kept
        fact("KJFK", "b", "2026-08-02T06:00:00.000Z", "2026-08-02", other),    # other authority
        fact("KLAX", "c", "2026-08-02T07:00:00.000Z", "2026-08-02"),           # other station
        fact("KJFK", "d", "2026-08-03T03:00:00.000Z", "2026-08-02"),           # late in the local day
        fact("KJFK", "e", "2026-07-20T12:00:00.000Z", "2026-07-20"),           # long closed
        fact("KJFK", "f", "2026-08-01T20:00:00.000Z", "2026-08-01"),           # recent, other day: body decides
        fact("KJFK", "g", "2026-08-02T08:00:00.000Z", "2026-08-02", key_parts=5),  # unparsed key: body decides
    ]
    target = tmp_path / "target.db"
    _target_with_facts(target, facts)
    evidence = {"rows": [{"station_id": "KJFK", "event_date": "2026-08-02"}]}
    got = script._attach_stored_revision_facts(target, dict(evidence), sha)["stored_revision_facts"]
    assert [f["observation_id"] for f in got] == ["a", "d", "g"]

    # The query it replaced, as the specification.
    with sqlite3.connect(target) as conn:
        spec = [
            json.loads(raw) for (raw,) in conn.execute(
                "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type='truth_observation_revision' "
                "AND json_extract(fact_json,'$.authority_scope_projection_content_sha256')=?", (sha,))
        ]
    spec = [f for f in spec if f["station_id"] == "KJFK" and f["event_date"] == "2026-08-02"]
    spec.sort(key=lambda f: (f["station_id"], f["observation_id"], f["revision_no"]))
    assert got == spec
