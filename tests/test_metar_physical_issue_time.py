"""The planner issues a day at noon plus the arrival grace, from what had arrived (v5).

User ruling 2026-09-25, 「按当时数据重算」: the forecast a recompute writes for a
day must be the one a live cycle could have written -- the parameters valid
and the reports first seen by the issue time -- and the live cycle waits for
that same moment.  So the answer no longer depends on when it is computed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from bayes_basket_target import metar_physical_forward as mpf

UTC = timezone.utc
EVENT_DATE = "2026-09-20"
# ZUUU (Asia/Shanghai): the local day opens 09-19T16:00Z, noon is 04:00Z and
# the day is issued at 04:30Z.
DAY_START = datetime(2026, 9, 19, 16, tzinfo=UTC)
NOON = datetime(2026, 9, 20, 4, tzinfo=UTC)


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _ssot() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_system(ensemble_system_id TEXT PRIMARY KEY,"
        " canonical_name TEXT, independence_family_id TEXT, provider_id TEXT, source_class TEXT,"
        " aggregator_or_republisher INTEGER, expected_member_count INTEGER,"
        " member_identity_contract_version TEXT, publication_contract_version TEXT,"
        " valid_from_utc TEXT, valid_until_utc TEXT, content_sha256 TEXT)"
    )
    conn.execute("CREATE TABLE calibration_station_registry(station_id TEXT, settlement_definition TEXT)")
    conn.execute("INSERT INTO calibration_station_registry VALUES('ZUUU','whole_degree_c')")
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_historical_member_run(station_id TEXT,"
        " target_local_date TEXT, ensemble_system_id TEXT, archive_lead_bucket TEXT,"
        " extraction_version TEXT, run_cycle TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_evidence_artifact(station_id TEXT, scope_key TEXT,"
        " trained_through_utc TEXT, valid_from_utc TEXT, artifact_json TEXT, artifact_kind TEXT)"
    )
    # Three afternoon artifacts: valid before noon, between noon and the issue
    # time, and after it.  Newer training in that order.
    for day, marker, available in (
        (17, "before-noon", "2026-09-19T20:00:00Z"),
        (18, "before-issue", "2026-09-20T04:10:00Z"),
        (19, "after-issue", "2026-09-20T04:40:00Z"),
    ):
        conn.execute(
            "INSERT INTO basket_probability_evidence_artifact VALUES(?,?,?,?,?,?)",
            ("ZUUU", "ZUUU|x|AFTERNOON", f"2026-09-{day}T15:59:59Z", available,
             json.dumps({"remaining_warming_distribution": {"0.0": 1.0}, "content_sha256": marker}),
             "METAR_TRAJECTORY_LIKELIHOOD"),
        )
    return conn


def _archive() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reported_trajectory_observations(observation_id TEXT, station_id TEXT,"
        " source_lane TEXT, valid_at TEXT, reported_temperature_c REAL, raw_text TEXT,"
        " first_seen_at TEXT)"
    )
    for hour in range(13):
        valid = DAY_START + timedelta(hours=hour)
        seen = valid + timedelta(minutes=4)
        if hour == 10:
            seen = valid + timedelta(hours=9)       # a backfill of a feed gap
        if hour == 12:
            seen = valid + timedelta(minutes=12)    # the noon report, a little late
        conn.execute(
            "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?)",
            (f"h{hour:02d}", "ZUUU", "awc", _stamp(valid), 20.0 + hour / 2, "METAR", _stamp(seen)),
        )
    return conn


@pytest.fixture
def planner(monkeypatch):
    seen = {"rows": [], "artifacts": []}

    def compile_state(rows, **_kwargs):
        seen["rows"].append([row["observation_id"] for row in rows])
        return object()

    def build(_state, *, artifact_content_sha256, **_kwargs):
        seen["artifacts"].append(artifact_content_sha256)
        return {"members": [{"raw_tmax_c": 26.0}], "content_sha256": "b" * 64}

    monkeypatch.setattr(mpf, "registry_scope_timezone_triples",
                        lambda _conn: [("ZUUU", EVENT_DATE, "Asia/Shanghai")])
    monkeypatch.setattr(mpf, "compile_conditional_metar_path_state", compile_state)
    monkeypatch.setattr(mpf, "build_physical_tmax_members", build)
    ssot, archive = _ssot(), _archive()

    def plan(as_of: datetime):
        return mpf.plan_metar_physical_member_runs(
            ssot, archive_conn=archive, as_of_utc=_stamp(as_of), earliest_event_date=EVENT_DATE
        )

    return plan, seen


def test_a_day_is_not_issued_before_noon_plus_the_grace(planner) -> None:
    plan, seen = planner
    pending, receipt = plan(NOON + timedelta(minutes=29))
    assert pending == [] and seen["rows"] == []
    assert receipt["skipped"]["anchor_not_passed"] == 1


def test_the_issued_day_holds_what_had_arrived_by_then(planner) -> None:
    plan, seen = planner
    pending, _ = plan(NOON + timedelta(minutes=31))
    assert [(station, day) for _params, station, day in pending] == [("ZUUU", EVENT_DATE)]
    ids = seen["rows"][0]
    assert "h10" not in ids                 # backfilled after the issue time
    assert ids[-1] == "h12"                 # the late noon report made it
    assert seen["artifacts"] == ["before-issue"]


def test_a_later_recompute_sees_the_same_day(planner) -> None:
    plan, seen = planner
    live, _ = plan(NOON + timedelta(minutes=31))
    later, _ = plan(datetime(2026, 9, 25, 7, tzinfo=UTC))
    assert seen["rows"][0] == seen["rows"][1]
    assert seen["artifacts"] == ["before-issue", "before-issue"]
    assert [params for params, _s, day in later if day == EVENT_DATE][0][:15] == live[0][0][:15]
