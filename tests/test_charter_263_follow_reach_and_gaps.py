"""Charter 263 (四): new stations are read into the event-run follow files.

ECMWF's D-3 18Z -- the run a D+2 day holds until ECMWF's own D-2 00Z lands
(charter 193 amendment C) -- was read only to +72h, which never reaches an
American station's D+2 day, so none of them ever held it; and a follow file
was topped up for stations listed after its first read only when some other
trigger happened to queue the follow job.  Now the follow-only lead reads on
to the station's day where +72h stops short, a held event-run file lacking a
listed station it covers queues the follow job, and the day's pass runs again
once that job is done.
"""
from __future__ import annotations

import json
from concurrent.futures import Future
from datetime import date, datetime, timedelta, timezone

import pytest

import bayes_basket_target.production_worker as pw
from bayes_basket_target import ecmwf_ifs_archive as ecmwf
from bayes_basket_target import follow_run, nwp_arrival_probe
from bayes_basket_target.contracts import ContractError, content_sha256

DAY = "2026-10-11"
D3_18Z = datetime(2026, 10, 8, 18, tzinfo=timezone.utc)
D2_00Z = datetime(2026, 10, 9, 0, tzinfo=timezone.utc)
AMERICAS = {
    "KATL": "America/New_York",
    "KDAL": "America/Chicago",
    "KBKF": "America/Denver",
    "KLAX": "America/Los_Angeles",
    "SAEZ": "America/Argentina/Buenos_Aires",
    "MMMX": "America/Mexico_City",
}


# ------------------------------------------------------------- reach rule


def test_the_follow_only_lead_reads_the_americas_whole_day() -> None:
    for station, zone in AMERICAS.items():
        with pytest.raises(ContractError):
            ecmwf._period_end_steps(
                event_date=DAY, settlement_timezone=zone, run_init_utc=D3_18Z
            )
        steps = ecmwf.station_period_end_steps(
            event_date=DAY, settlement_timezone=zone, run_init_utc=D3_18Z,
            lead_days=3,
        )
        assert max(steps) > ecmwf.LADDER_MAX_FORECAST_STEP, station
        assert max(steps) <= ecmwf.FOLLOW_ONLY_MAX_FORECAST_STEP
        zone_info = __import__("zoneinfo").ZoneInfo(zone)
        assert all(
            (D3_18Z + timedelta(hours=step)).astimezone(zone_info).date()
            == date.fromisoformat(DAY)
            for step in steps
        )
        assert ecmwf.follow_period_end_steps(
            event_date=DAY, settlement_timezone=zone, run_init_utc=D3_18Z
        ) == steps
    # New York: 10-11 local runs 04Z 10-11 to 04Z 10-12, +58h..+82h.
    assert ecmwf.station_period_end_steps(
        event_date=DAY, settlement_timezone="America/New_York",
        run_init_utc=D3_18Z, lead_days=3,
    ) == [60, 63, 66, 69, 72, 75, 78, 81]


def test_a_station_the_ladder_ceiling_seals_keeps_its_steps() -> None:
    # EDDM's D-3 18Z history and certificate were built on these seven steps.
    expected = ecmwf._period_end_steps(
        event_date=DAY, settlement_timezone="Europe/Berlin", run_init_utc=D3_18Z
    )
    assert expected == [54, 57, 60, 63, 66, 69, 72]
    assert ecmwf.station_period_end_steps(
        event_date=DAY, settlement_timezone="Europe/Berlin",
        run_init_utc=D3_18Z, lead_days=3,
    ) == expected


def test_the_ladder_leads_are_untouched() -> None:
    # D-2 00Z cannot seal Los Angeles' D+2 day under +72h; that stays a
    # refusal at every ladder lead -- only the follow-only lead reads on.
    with pytest.raises(ContractError):
        ecmwf.station_period_end_steps(
            event_date=DAY, settlement_timezone="America/Los_Angeles",
            run_init_utc=D2_00Z, lead_days=2,
        )
    assert ecmwf.station_period_end_steps(
        event_date=DAY, settlement_timezone="America/New_York",
        run_init_utc=D2_00Z, lead_days=2,
    ) == ecmwf._period_end_steps(
        event_date=DAY, settlement_timezone="America/New_York",
        run_init_utc=D2_00Z,
    )


def test_the_arrival_probe_waits_for_the_steps_the_capture_reads() -> None:
    steps = nwp_arrival_probe._station_steps(
        nwp_arrival_probe.ECMWF_FAMILY,
        event_date=DAY, settlement_timezone="America/Los_Angeles",
        run_init=D3_18Z,
    )
    assert steps == ecmwf.station_period_end_steps(
        event_date=DAY, settlement_timezone="America/Los_Angeles",
        run_init_utc=D3_18Z, lead_days=3,
    )


def test_the_backfill_counts_the_americas_coverable_at_d3() -> None:
    from scripts.backfill_ecmwf_ifs_multistation_archive import split_coverable

    scopes = [
        {"station_id": station, "settlement_timezone": zone}
        for station, zone in {**AMERICAS, "EDDM": "Europe/Berlin"}.items()
    ]
    kept, left_out = split_coverable(
        scopes, event_date=DAY, lead_days=3, run_cycle_hour=18
    )
    assert left_out == []
    assert len(kept) == len(scopes)


# ------------------------------------------------------------- gap check


def _write_follow(root, family, *, stations, run_init=D3_18Z, lead=3):
    path = follow_run.follow_archive_path(
        root, family, DAY, lead, run_init.hour
    )
    body = {
        "schema": follow_run.archive_schema(family),
        "event_date": DAY,
        "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
        "training_only": True,
        "holdout_eligible": False,
        "captures": [{"station_id": station} for station in stations],
    }
    body["content_sha256"] = content_sha256(body)
    path.write_text(json.dumps(body))
    return path


def test_a_held_event_run_lacking_a_listed_station_is_a_gap(tmp_path) -> None:
    _write_follow(tmp_path, follow_run.ECMWF_FAMILY, stations=["EDDM", "LLBG"])
    gaps = follow_run.event_run_station_gaps(
        tmp_path,
        event_date=DAY,
        station_timezones={
            "EDDM": "Europe/Berlin",
            "KATL": "America/New_York",
            "KLAX": "America/Los_Angeles",
        },
        as_of=datetime(2026, 10, 9, 7, 1, tzinfo=timezone.utc),
    )
    assert gaps == [{
        "family": follow_run.ECMWF_FAMILY,
        "run_init_utc": "2026-10-08T18:00:00Z",
        "path": "ecmwf_2026-10-11_d3_18z.json",
        "missing_station_ids": ["KATL", "KLAX"],
    }]


def test_no_gap_when_held_stale_tampered_or_complete(tmp_path) -> None:
    zones = {"EDDM": "Europe/Berlin", "KATL": "America/New_York"}
    path = _write_follow(tmp_path, follow_run.ECMWF_FAMILY, stations=["EDDM"])
    late = datetime(2026, 10, 9, 18, 1, tzinfo=timezone.utc)  # run > 24h old
    assert follow_run.event_run_station_gaps(
        tmp_path, event_date=DAY, station_timezones=zones, as_of=late
    ) == []
    body = json.loads(path.read_text())
    body["captures"].append({"station_id": "KATL"})  # hash no longer matches
    path.write_text(json.dumps(body))
    now = datetime(2026, 10, 9, 7, 1, tzinfo=timezone.utc)
    assert follow_run.event_run_station_gaps(
        tmp_path, event_date=DAY, station_timezones=zones, as_of=now
    ) == []
    _write_follow(tmp_path, follow_run.ECMWF_FAMILY, stations=["EDDM", "KATL"])
    assert follow_run.event_run_station_gaps(
        tmp_path, event_date=DAY, station_timezones=zones, as_of=now
    ) == []


# ------------------------------------------------------- queue and reseat


def _queue_worker(tmp_path, monkeypatch, gaps):
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    capture_root = tmp_path / "official_native_capture"
    capture_root.mkdir()
    # Every event-run file present, no ICON/ECCC rung: before 263 (四) the
    # job was never queued in this state.
    for family in follow_run.EVENT_RUN_FAMILIES:
        for lead, hour in follow_run.event_follow_runs(family):
            follow_run.follow_archive_path(
                capture_root, family, DAY, lead, hour
            ).write_text("{}")
    monkeypatch.setattr(
        worker, "_unfollowed_event_run_gaps",
        lambda **kwargs: set(gaps) - (
            getattr(worker, "_official_native_follow_gaps_seen", None) or set()
        ),
    )
    submitted = []

    class _Pool:
        def submit(self, fn):
            submitted.append(fn)
            future = Future()
            future.set_result({"state": "COMMITTED_NO_LIVE"})
            return future

    worker._official_native_follow_pool = _Pool()
    return worker, capture_root, submitted


def _enqueue(worker, capture_root):
    return worker._enqueue_follow_run_capture(
        event_date=DAY, run_ladder=[],
        registry_path=capture_root.parent / "market_registry_forward.json",
        capture_root=capture_root, env={},
    )


def test_a_gap_queues_the_follow_job_once_and_asks_for_a_reseat(
    tmp_path, monkeypatch
) -> None:
    gap = (DAY, "ecmwf_2026-10-11_d3_18z.json", ("KATL", "KDAL"))
    worker, capture_root, submitted = _queue_worker(tmp_path, monkeypatch, {gap})
    assert _enqueue(worker, capture_root) == "QUEUED"
    assert len(submitted) == 1
    assert worker._official_native_follow_reseat == {
        DAY: ["ecmwf_2026-10-11_d3_18z.json"]
    }
    # The same gap is not queued again once a job was given it.
    assert _enqueue(worker, capture_root) == "NO_FOLLOW_RUNG"
    assert len(submitted) == 1


def test_without_a_gap_nothing_changes(tmp_path, monkeypatch) -> None:
    worker, capture_root, submitted = _queue_worker(tmp_path, monkeypatch, set())
    assert _enqueue(worker, capture_root) == "NO_FOLLOW_RUNG"
    assert submitted == []
    assert getattr(worker, "_official_native_follow_reseat", None) is None


def test_a_gap_met_by_a_running_job_is_not_marked_seen(
    tmp_path, monkeypatch
) -> None:
    gap = (DAY, "ecmwf_2026-10-11_d3_18z.json", ("KATL",))
    worker, capture_root, submitted = _queue_worker(tmp_path, monkeypatch, {gap})
    running = Future()  # not done
    worker._official_native_follow_inflight = {DAY: running}
    assert _enqueue(worker, capture_root) == "ALREADY_QUEUED"
    assert not getattr(worker, "_official_native_follow_gaps_seen", None)
    running.set_result({})
    assert _enqueue(worker, capture_root) == "QUEUED"


def _reseat_worker(tmp_path, monkeypatch, *, state):
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    rung = (2, 0, D2_00Z)
    monkeypatch.setattr(pw, "_official_native_run_ladder", lambda **k: [rung])
    monkeypatch.setattr(worker, "_arrival_probe_state", lambda: {})
    calls = []

    def fake_capture(**kwargs):
        calls.append(kwargs)
        return {"state": state}

    monkeypatch.setattr(worker, "_capture_official_native_packet", fake_capture)
    return worker, calls, rung


def test_the_day_is_passed_again_once_its_follow_job_is_done(
    tmp_path, monkeypatch
) -> None:
    worker, calls, rung = _reseat_worker(tmp_path, monkeypatch, state="SUBMITTED")
    pending = Future()
    worker._official_native_follow_inflight = {DAY: pending}
    worker._official_native_follow_reseat = {DAY: ["ecmwf_2026-10-11_d3_18z.json"]}
    inbox = tmp_path / "official_native_ensemble_forward_inbox.json"
    assert worker._reseat_after_listing_follow(
        as_of_utc="2026-10-09T07:05:00Z", inbox_path=inbox
    ) == []
    assert calls == []
    pending.set_result({})
    outcomes = worker._reseat_after_listing_follow(
        as_of_utc="2026-10-09T07:09:00Z", inbox_path=inbox
    )
    assert outcomes == [{
        "event_date": DAY,
        "follow_files": ["ecmwf_2026-10-11_d3_18z.json"],
        "state": "SUBMITTED",
    }]
    assert len(calls) == 1
    assert calls[0]["arrival"] == {"family": "listing_follow_reseat"}
    assert list(calls[0]["forced_rungs"]) == [rung]
    assert calls[0]["event_date"] == DAY
    assert worker._official_native_follow_reseat == {}


def test_a_reseat_that_found_the_slots_busy_is_tried_again(
    tmp_path, monkeypatch
) -> None:
    worker, calls, _rung = _reseat_worker(
        tmp_path, monkeypatch, state="DEFERRED_CAPTURE_SLOTS_BUSY"
    )
    worker._official_native_follow_reseat = {DAY: ["f"]}
    inbox = tmp_path / "inbox.json"
    worker._reseat_after_listing_follow(as_of_utc="2026-10-09T07:09:00Z", inbox_path=inbox)
    assert DAY in worker._official_native_follow_reseat
    worker._reseat_after_listing_follow(as_of_utc="2026-10-09T07:10:00Z", inbox_path=inbox)
    assert len(calls) == 2


def test_the_listing_step_retries_a_follow_job_that_met_a_running_one(
    tmp_path, monkeypatch
) -> None:
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    worker.station_allowlist = ()
    (tmp_path / "market_registry_forward.json").write_bytes(b"{}")
    registry = {
        "filters": {"include_not_started_for_p0": True},
        "rows": [{"station_id": "KATL", "event_date": DAY,
                  "settlement_timezone": "America/New_York"}],
    }
    monkeypatch.setattr(pw, "load_market_registry_snapshot", lambda path: registry)
    monkeypatch.setattr(pw, "_load_native_forward_inbox", lambda path, error_code: [])
    monkeypatch.setattr(
        worker, "_capture_official_native_packet",
        lambda **k: {"state": "SUBMITTED"},
    )
    answers = iter(["ALREADY_QUEUED", "QUEUED"])
    asked = []

    def fake_follow(*, event_date):
        asked.append(event_date)
        return next(answers)

    worker._enqueue_listing_follow = fake_follow
    first = worker.run_listing_triggered_captures(as_of_utc="2026-10-09T07:01:30Z")
    assert first["outcomes"][0]["follow"] == "ALREADY_QUEUED"
    assert worker._official_native_listing_follow_pending == {DAY}
    second = worker.run_listing_triggered_captures(as_of_utc="2026-10-09T07:01:45Z")
    assert second["follow_retries"] == {DAY: "QUEUED"}
    assert worker._official_native_listing_follow_pending == set()
    assert asked == [DAY, DAY]
