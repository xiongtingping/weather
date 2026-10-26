"""Charter 195: model runs captured on arrival, compiled on capture.

(一) a run merged into the official inbox mid-cycle is compiled into P0 at the
next fast-lane checkpoint -- P0, then P1 for what it re-rooted (P0-M retired,
charter 283) --
instead of waiting for the next cycle's replay compile; and the arrival probe
records when each run is actually on the server the capture reads, without
starting anything.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import bayes_basket_target.production_worker as pw
from bayes_basket_target import nwp_arrival_probe as probe

MARK = (10, 0, "2026-09-22")


# ---------------------------------------------------------------- checkpoint


def _worker(tmp_path, *, chain_ran=True):
    worker = object.__new__(pw.TargetProductionWorker)
    worker._stage_timings_ms = {}
    worker.canonical_source_conn = object()
    worker._probability_chain_ran = chain_ran
    worker._p1_covered_watermark = MARK
    worker.nwp_runtime_root = tmp_path
    inbox = tmp_path / "official_native_ensemble_forward_inbox.json"
    inbox.write_bytes(b'{"packets": []}')
    worker._p0_covered_official_watermark = pw._official_inbox_watermark(inbox)
    return worker, inbox


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(pw, "_live_observation_watermark", lambda conn: MARK)
    monkeypatch.setattr(pw, "utc_now", lambda: "2026-09-22T13:45:00Z")
    monkeypatch.setattr(pw.TargetProductionWorker, "_mark",
                        lambda self, name: self._stage_timings_ms.__setitem__(name, 1.0), raising=False)


def test_an_unmoved_inbox_costs_nothing(monkeypatch, tmp_path) -> None:
    worker, _inbox = _worker(tmp_path)
    monkeypatch.setattr(worker, "_fast_lane_official_p0", lambda **k: pytest.fail("nothing moved"))
    assert worker._fast_lane_checkpoint("before_decisions") is None


def test_a_merged_run_after_the_chain_goes_p0_then_p1(monkeypatch, tmp_path) -> None:
    worker, inbox = _worker(tmp_path)
    inbox.write_bytes(b'{"packets": ["new run"]}')
    order = []

    def fake_p0(**kwargs):
        order.append(("p0", kwargs["materialize"]))
        assert kwargs["official_watermark"] == pw._official_inbox_watermark(inbox)
        return {"state": "RAN", "new_envelopes": True, "rowid_before": 700}

    rooted = {700: [("ZUUU", "2026-09-23"), ("KLAX", "2026-09-22")]}
    monkeypatch.setattr(worker, "_fast_lane_official_p0", fake_p0)
    monkeypatch.setattr(worker, "_scopes_rooted_after", lambda mark: rooted.get(mark, []))
    seen = {}

    def fake_p1(**kwargs):
        order.append(("p1", None))
        seen.update(kwargs)
        return {"ran": True, "p1": {"state": "COMMITTED"}}

    monkeypatch.setattr(worker, "_materialize_early_p1", fake_p1)
    receipt = worker._fast_lane_checkpoint("before_decisions")
    assert order == [("p0", True), ("p1", None)]
    assert seen["only_scopes"] == {("ZUUU", "2026-09-23"), ("KLAX", "2026-09-22")}
    assert receipt["nwp_p0"]["new_envelopes"] is True
    # No new report: the covered report mark stays where it was.
    assert worker._p1_covered_watermark == MARK


def test_before_the_chain_a_merged_run_only_reaches_the_p0_inbox(monkeypatch, tmp_path) -> None:
    worker, inbox = _worker(tmp_path, chain_ran=False)
    inbox.write_bytes(b'{"packets": ["new run"]}')
    seen = {}
    monkeypatch.setattr(worker, "_fast_lane_official_p0",
                        lambda **k: seen.update(k) or {"state": "RAN", "new_envelopes": False})
    monkeypatch.setattr(worker, "_materialize_early_p1", lambda **k: pytest.fail("the chain re-roots"))
    worker._fast_lane_checkpoint("before_decisions")
    assert seen["materialize"] is False


def test_the_lane_waits_for_a_replay_compile_to_cover_the_inbox(monkeypatch, tmp_path) -> None:
    worker, inbox = _worker(tmp_path)
    worker._p0_covered_official_watermark = None
    inbox.write_bytes(b'{"packets": ["new run"]}')
    monkeypatch.setattr(worker, "_fast_lane_official_p0", lambda **k: pytest.fail("not covered yet"))
    assert worker._fast_lane_checkpoint("before_decisions") is None


def test_the_switch_restores_the_old_checkpoint(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pw, "FAST_LANE_NWP_P0_ENABLED", False)
    worker, inbox = _worker(tmp_path)
    inbox.write_bytes(b'{"packets": ["new run"]}')
    monkeypatch.setattr(worker, "_fast_lane_official_p0", lambda **k: pytest.fail("switched off"))
    assert worker._fast_lane_checkpoint("before_decisions") is None


def _packet(station, event_date, sha):
    return {
        "schema": pw.OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": "Asia/Shanghai",
        "snapshot_content_sha256": sha,
    }


def test_the_compile_reads_only_snapshots_that_moved(monkeypatch, tmp_path) -> None:
    worker, _inbox = _worker(tmp_path)
    worker.commit_conn = object()
    worker.native_oos_snapshot = None
    worker.p0_inbox_path = tmp_path / "p0_inbox.json"
    worker.station_allowlist = ()
    worker.event_date_allowlist = ()
    worker._p0_replay_active_stations = {"ZUUU", "ZGGG"}
    worker._p0_replay_compiled_shas = {("ZUUU", "2026-09-23"): "old", ("ZGGG", "2026-09-23"): "same"}
    packets = [
        _packet("ZUUU", "2026-09-23", "new"),     # moved -> compiled
        _packet("ZGGG", "2026-09-23", "same"),    # compiled by the replay -> skipped
        _packet("KLAX", "2026-09-23", "x"),       # no activation -> skipped
        _packet("ZUUU", "2026-09-20", "closed"),  # day over -> skipped
    ]
    monkeypatch.setattr(pw, "_load_native_forward_inbox", lambda path, error_code: packets)
    compiled = []
    monkeypatch.setattr(
        pw, "_compile_native_p0_with_republisher_fallback",
        lambda conn, **k: compiled.append(k["scope"]) or ({"scope": k["scope"]}, [], "official_native"),
    )
    monkeypatch.setattr(pw, "persist_native_ensemble_forward_prediction", lambda conn, packet: {"wrote": True})
    upserts = []
    monkeypatch.setattr(pw, "_upsert_native_p0_inbox",
                        lambda path, compiled_packets, generated_at_utc: upserts.append(compiled_packets) or {"accepted_count": len(compiled_packets)})
    ceilings = iter([100, 104])
    monkeypatch.setattr(worker, "_envelope_rowid_ceiling", lambda: next(ceilings))
    monkeypatch.setattr(worker, "materialize_resident_p0", lambda **k: {"state": "COMMITTED"})
    mark = (5, "abc")
    receipt = worker._fast_lane_official_p0(
        as_of_utc="2026-09-22T13:45:00Z", official_watermark=mark, materialize=True,
    )
    assert compiled == [("ZUUU", "2026-09-23")]
    assert len(upserts) == 1 and len(upserts[0]) == 1
    assert receipt["new_envelopes"] is True and receipt["rowid_before"] == 100
    assert worker._p0_covered_official_watermark == mark
    assert worker._p0_replay_compiled_shas[("ZUUU", "2026-09-23")] == "new"
    # A second checkpoint on the same inbox compiles nothing.
    compiled.clear()
    worker._fast_lane_official_p0(
        as_of_utc="2026-09-22T13:46:00Z", official_watermark=mark, materialize=False,
    )
    assert compiled == []


def test_a_refused_compile_is_not_retried_every_checkpoint(monkeypatch, tmp_path) -> None:
    worker, _inbox = _worker(tmp_path)
    worker.commit_conn = object()
    worker.native_oos_snapshot = None
    worker.p0_inbox_path = tmp_path / "p0_inbox.json"
    worker._p0_replay_active_stations = None
    worker._p0_replay_compiled_shas = {}
    monkeypatch.setattr(pw, "_load_native_forward_inbox",
                        lambda path, error_code: [_packet("ZUUU", "2026-09-23", "bad")])
    calls = []
    monkeypatch.setattr(
        pw, "_compile_native_p0_with_republisher_fallback",
        lambda conn, **k: calls.append(1) or (None, [{"reason": "stale"}], "official_native"),
    )
    for _ in range(2):
        receipt = worker._fast_lane_official_p0(
            as_of_utc="2026-09-22T13:45:00Z", official_watermark=(1, "a"), materialize=True,
        )
    assert calls == [1]
    assert receipt["compiled_count"] == 0 and receipt["new_envelopes"] is False


def test_the_replay_stage_records_what_it_covered() -> None:
    source = Path(pw.__file__).read_text()
    stage = source[source.index("official_mark = _official_inbox_watermark(official_inbox_path)"):]
    stage = stage[: stage.index('self._mark("native_p0_replay_compile")')]
    # The mark is read before the inbox is loaded, and recorded with the
    # snapshots and activations the replay compiled against.
    assert stage.index("official_mark =") < stage.index("_load_native_forward_inbox(")
    for name in ("_p0_replay_compiled_shas", "_p0_replay_active_stations", "_p0_covered_official_watermark"):
        assert f"self.{name} =" in stage


# -------------------------------------------------------------------- probe


ZONES = {"ZUUU": "Asia/Shanghai", "KLAX": "America/Los_Angeles", "NZWN": "Pacific/Auckland"}


def _registry(tmp_path, dates=("2026-09-21", "2026-09-22", "2026-09-23")):
    rows = [
        {"event_date": d, "registry_json": json.dumps({"station_id": s, "settlement_timezone": z})}
        for d in dates for s, z in ZONES.items()
    ]
    path = tmp_path / "market_registry_forward.json"
    path.write_text(json.dumps({"rows": rows}))
    return path


def test_the_needed_step_is_the_capture_modules_own() -> None:
    run = datetime(2026, 9, 21, 6, tzinfo=timezone.utc)
    # The 06Z run's last period ends for these three at +24, +48, +72.
    assert probe.needed_max_step(probe.ECMWF_FAMILY, run_init=run, event_date="2026-09-21", station_timezones=ZONES) == 24
    assert probe.needed_max_step(probe.ECMWF_FAMILY, run_init=run, event_date="2026-09-22", station_timezones=ZONES) == 48
    assert probe.needed_max_step(probe.ECMWF_FAMILY, run_init=run, event_date="2026-09-23", station_timezones=ZONES) == 72
    # Three days out only the far east is inside +72h; the far west is not.
    assert probe.needed_max_step(probe.ECMWF_FAMILY, run_init=run, event_date="2026-09-24",
                                 station_timezones={"NZWN": "Pacific/Auckland"}) == 72
    assert probe.needed_max_step(probe.ECMWF_FAMILY, run_init=run, event_date="2026-09-24",
                                 station_timezones={"KLAX": "America/Los_Angeles"}) is None


def _fake_server(present):
    asked = []

    def fetch(method, url):
        asked.append((method, url))
        for needle, body in present.items():
            if needle in url:
                return 200, {"Last-Modified": "Mon, 21 Sep 2026 13:25:45 GMT"}, body
        return 404, {}, ""

    return fetch, asked


def test_a_run_is_recorded_once_when_its_last_step_is_on_the_replica(tmp_path) -> None:
    registry = _registry(tmp_path)
    run = datetime(2026, 9, 21, 6, tzinfo=timezone.utc)
    now = datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc)
    stamp = f"{run:%Y%m%d%H%M%S}"
    present = {
        f"storage.googleapis.com/ecmwf-open-data/20260921/06z/ifs/0p25/enfo/{stamp}-{step}h-enfo-ef": ""
        for step in (24, 48)
    }
    fetch, asked = _fake_server(present)
    receipt = probe.probe_round(runtime_root=tmp_path, now=now, fetch=fetch,
                                registry_path=registry, families=(probe.ECMWF_FAMILY,))
    got = {(r["source"], r["event_date"]) for r in receipt["new_rows"]}
    assert got == {("gcs", "2026-09-21"), ("gcs", "2026-09-22")}
    state = probe.load_state(tmp_path / probe.STATE_FILE_NAME)
    assert probe.newest_ready_run(state, family=probe.ECMWF_FAMILY, event_date="2026-09-22") == run
    assert probe.newest_ready_run(state, family=probe.ECMWF_FAMILY, event_date="2026-09-23") is None
    # Recorded runs are not asked about again.
    asked.clear()
    probe.probe_round(runtime_root=tmp_path, now=now, fetch=fetch,
                      registry_path=registry, families=(probe.ECMWF_FAMILY,))
    assert not any("-24h-" in url or "-48h-" in url for _m, url in asked if "googleapis" in url)
    log = (tmp_path / probe.LOG_FILE_NAME).read_text().splitlines()
    assert len(log) == 2


def test_the_index_alone_is_not_arrival(tmp_path) -> None:
    registry = _registry(tmp_path, dates=("2026-09-21",))
    now = datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc)
    fetch, _asked = _fake_server({"-24h-enfo-ef.index": ""})
    receipt = probe.probe_round(runtime_root=tmp_path, now=now, fetch=fetch,
                                registry_path=registry, families=(probe.ECMWF_FAMILY,))
    assert receipt["new_rows"] == []


def test_gefs_needs_every_member(tmp_path) -> None:
    registry = _registry(tmp_path, dates=("2026-09-21",))
    now = datetime(2026, 9, 21, 10, 30, tzinfo=timezone.utc)
    members = {f"/{m}.t06z.pgrb2a.0p50.f024.idx": "" for m in probe.GEFS_MEMBER_PREFIXES[:-2]}
    members["/gep30.t06z.pgrb2a.0p50.f024.idx"] = ""
    fetch, _asked = _fake_server(members)  # gep29 missing
    receipt = probe.probe_round(runtime_root=tmp_path, now=now, fetch=fetch,
                                registry_path=registry, families=(probe.GEFS_FAMILY,))
    assert not [r for r in receipt["new_rows"] if r["run_init_utc"] == "2026-09-21T06:00:00Z"]
    members["/gep29.t06z.pgrb2a.0p50.f024.idx"] = ""
    fetch, _asked = _fake_server(members)
    receipt = probe.probe_round(runtime_root=tmp_path, now=now, fetch=fetch,
                                registry_path=registry, families=(probe.GEFS_FAMILY,))
    assert [r["event_date"] for r in receipt["new_rows"] if r["run_init_utc"] == "2026-09-21T06:00:00Z"] == ["2026-09-21"]


def test_icon_is_read_off_one_listing(tmp_path) -> None:
    registry = _registry(tmp_path, dates=("2026-09-22",))
    now = datetime(2026, 9, 21, 3, 30, tzinfo=timezone.utc)
    listing = "\n".join(
        f'<a href="icon-eps_global_icosahedral_single-level_2026092100_{h:03d}_t_2m.grib2.bz2">x</a> 21-Sep-2026 02:{40 + h % 10:02d}:00  1'
        for h in [*range(0, 49), *range(51, 73, 3)]
    )
    fetch, asked = _fake_server({"icon-eps/grib/00/t_2m/": listing})
    receipt = probe.probe_round(runtime_root=tmp_path, now=now, fetch=fetch,
                                registry_path=registry, families=(probe.ICON_FAMILY,))
    rows = [r for r in receipt["new_rows"] if r["run_init_utc"] == "2026-09-21T00:00:00Z"]
    assert [r["event_date"] for r in rows] == ["2026-09-22"]
    assert rows[0]["last_modified"].startswith("21-Sep-2026 02:")
    # One listing per cycle directory, however many days it serves.
    assert sum(1 for _m, url in asked if "icon-eps/grib/00/" in url) == 1


def test_a_spent_budget_leaves_the_rest_pending(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(probe, "MAX_REQUESTS_PER_ROUND", 1)
    registry = _registry(tmp_path)
    now = datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc)
    fetch, _asked = _fake_server({"ecmwf-open-data": "", "ecmwf-forecasts": ""})
    receipt = probe.probe_round(runtime_root=tmp_path, now=now, fetch=fetch,
                                registry_path=registry, families=(probe.ECMWF_FAMILY,))
    assert receipt["budget_exhausted"] is True and receipt["new_ready"] == 0


def test_the_probe_is_shadow_only() -> None:
    source = Path(probe.__file__).read_text()
    for forbidden in ("sqlite3", "subprocess", "_capture_official_native_packet", "write_bytes"):
        assert forbidden not in source


def test_the_daemon_round_is_off_on_the_switch(monkeypatch) -> None:
    import scripts.weather_basket_yes_daemon as daemon

    monkeypatch.setenv(probe.PROBE_ENV, "off")
    assert daemon.run_nwp_arrival_probe_round() == {"state": "disabled"}


# ------------------------------------------------- (二) arrival-driven start


def _probe_state(*, updated="2026-09-21T13:15:00Z", listed=("2026-09-23",), ready=()):
    rows = {}
    for family, run, event_date in ready:
        run_dt = datetime.fromisoformat(run.replace("Z", "+00:00"))
        key = probe.ready_key(family, probe.DOWNLOAD_SOURCE[family], run_dt, event_date)
        rows[key] = {
            "family": family, "source": probe.DOWNLOAD_SOURCE[family], "acts_on_capture": True,
            "run_init_utc": run, "event_date": event_date, "first_seen_at_utc": updated,
        }
    return {"schema": probe.STATE_SCHEMA, "updated_at_utc": updated,
            "last_round": {"listed_dates": list(listed)}, "ready": rows}


def _runs(ladder):
    return [rung[2].strftime("%d %HZ") for rung in ladder]


def test_the_ladder_drops_rungs_neither_required_family_has_landed() -> None:
    as_of = "2026-09-21T13:15:00Z"
    clock = pw._official_native_run_ladder(event_date="2026-09-23", as_of_utc=as_of)
    assert _runs(clock) == ["21 12Z", "21 06Z", "21 00Z"]
    state = _probe_state(ready=[(probe.GEFS_FAMILY, "2026-09-21T06:00:00Z", "2026-09-23")])
    probed = pw._official_native_run_ladder(event_date="2026-09-23", as_of_utc=as_of, probe_state=state)
    # 12Z: on neither server yet -> dropped.  06Z: GEFS has it -> kept (ECMWF
    # holds).  00Z: past GEFS's window, landed long ago -> kept.
    assert _runs(probed) == ["21 06Z", "21 00Z"]


def test_a_probe_that_cannot_speak_leaves_the_clock_ladder() -> None:
    as_of = "2026-09-21T13:15:00Z"
    clock = pw._official_native_run_ladder(event_date="2026-09-23", as_of_utc=as_of)
    stale = _probe_state(updated="2026-09-21T12:00:00Z")
    unlisted = _probe_state(listed=("2026-09-22",))
    for state in (stale, unlisted, {"schema": "x"}):
        assert pw._official_native_run_ladder(
            event_date="2026-09-23", as_of_utc=as_of, probe_state=state
        ) == clock


@pytest.fixture
def capture_worker(tmp_path):
    instance = object.__new__(pw.TargetProductionWorker)
    instance.nwp_runtime_root = tmp_path
    instance._official_native_capture_pool = None
    yield instance
    pool = getattr(instance, "_official_native_capture_pool", None)
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _capture(worker, tmp_path, **kwargs):
    return worker._capture_official_native_packet(
        station_id="", event_date="2026-09-23", as_of_utc="2026-09-21T13:15:00Z",
        inbox_path=tmp_path / "inbox.json", **kwargs,
    )


def test_an_arrival_is_not_held_by_the_retry_floor(capture_worker, tmp_path) -> None:
    first = _capture(capture_worker, tmp_path)
    assert first["reason"] == "forward_market_registry_missing"
    assert _capture(capture_worker, tmp_path)["state"] == "THROTTLED"
    arrival = {"family": probe.ECMWF_FAMILY, "run_init_utc": "2026-09-21T06:00:00Z"}
    assert _capture(capture_worker, tmp_path, arrival=arrival)["state"] != "THROTTLED"


def test_a_forced_rung_is_not_retired_by_the_held_ecmwf_gefs_run(capture_worker, tmp_path) -> None:
    ladder = pw._official_native_run_ladder(event_date="2026-09-23", as_of_utc="2026-09-21T13:15:00Z")
    capture_worker._official_native_capture_runs = {
        "2026-09-23": ladder[0][2].isoformat().replace("+00:00", "Z"),
    }
    assert _capture(capture_worker, tmp_path)["state"] == "RUN_ALREADY_CAPTURED"
    forced = _capture(
        capture_worker, tmp_path,
        arrival={"family": probe.ICON_FAMILY, "run_init_utc": "2026-09-21T12:00:00Z"},
        forced_rungs=ladder[:1],
    )
    assert forced["state"] != "RUN_ALREADY_CAPTURED"


def _trigger_worker(tmp_path, monkeypatch, state):
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    monkeypatch.setattr(worker, "_arrival_probe_state", lambda: state)
    calls = []

    def fake_capture(**kwargs):
        calls.append(kwargs)
        return {"state": "SUBMITTED"}

    monkeypatch.setattr(worker, "_capture_official_native_packet", fake_capture)
    follows = []
    monkeypatch.setattr(worker, "_enqueue_arrival_follow",
                        lambda **k: follows.append(k) or "QUEUED")
    return worker, calls, follows


def test_each_landed_run_starts_its_capture_once(monkeypatch, tmp_path) -> None:
    state = _probe_state(ready=[
        (probe.ECMWF_FAMILY, "2026-09-21T06:00:00Z", "2026-09-23"),
        (probe.ECMWF_FAMILY, "2026-09-21T06:00:00Z", "2026-09-22"),
    ], listed=("2026-09-22", "2026-09-23"))
    worker, calls, _follows = _trigger_worker(tmp_path, monkeypatch, state)
    receipt = worker.run_arrival_triggered_captures(as_of_utc="2026-09-21T13:16:00Z")
    assert receipt["state"] == "RAN"
    assert sorted(call["event_date"] for call in calls) == ["2026-09-22", "2026-09-23"]
    assert all(call["forced_rungs"] is None and call["arrival"]["family"] == probe.ECMWF_FAMILY
               for call in calls)
    calls.clear()
    worker.run_arrival_triggered_captures(as_of_utc="2026-09-21T13:17:00Z")
    assert calls == []


def test_one_pass_per_day_per_call_and_a_busy_day_is_asked_again(monkeypatch, tmp_path) -> None:
    state = _probe_state(ready=[
        (probe.GEFS_FAMILY, "2026-09-21T06:00:00Z", "2026-09-23"),
        (probe.ECMWF_FAMILY, "2026-09-21T06:00:00Z", "2026-09-23"),
    ])
    worker, calls, _follows = _trigger_worker(tmp_path, monkeypatch, state)
    monkeypatch.setattr(worker, "_capture_official_native_packet",
                        lambda **k: calls.append(k) or {"state": "IN_FLIGHT"})
    worker.run_arrival_triggered_captures(as_of_utc="2026-09-21T13:16:00Z")
    assert len(calls) == 1
    worker.run_arrival_triggered_captures(as_of_utc="2026-09-21T13:17:00Z")
    assert len(calls) == 2  # still in flight -> not marked, asked again


def test_icon_own_cycle_rides_its_leads_newest_rung(monkeypatch, tmp_path) -> None:
    state = _probe_state(updated="2026-09-21T03:00:00Z", listed=("2026-09-22",), ready=[
        (probe.ICON_FAMILY, "2026-09-21T00:00:00Z", "2026-09-22"),
    ])
    worker, calls, follows = _trigger_worker(tmp_path, monkeypatch, state)
    worker.run_arrival_triggered_captures(as_of_utc="2026-09-21T03:00:30Z")
    assert follows == []
    (call,) = calls
    (rung,) = call["forced_rungs"]
    # d1 of 09-22: the rung reads ICON's own 00Z of 09-21, and its ECMWF/GEFS
    # run is the newest of that lead by the clock -- never older than held.
    assert rung[0] == 1 and rung[2] == datetime(2026, 9, 21, 0, tzinfo=timezone.utc)


def test_a_followed_icon_cycle_goes_to_the_follow_lane(monkeypatch, tmp_path) -> None:
    state = _probe_state(updated="2026-09-21T15:00:00Z", listed=("2026-09-22",), ready=[
        (probe.ICON_FAMILY, "2026-09-21T12:00:00Z", "2026-09-22"),
    ])
    worker, calls, follows = _trigger_worker(tmp_path, monkeypatch, state)
    worker.run_arrival_triggered_captures(as_of_utc="2026-09-21T15:00:30Z")
    assert calls == []
    assert follows == [{"event_date": "2026-09-22", "lead": 1,
                        "run_init": datetime(2026, 9, 21, 12, tzinfo=timezone.utc)}]
    # Once its job is done the day's pass re-reads it for seating.
    worker._official_native_follow_inflight = {}
    worker.run_arrival_triggered_captures(as_of_utc="2026-09-21T15:01:00Z")
    assert len(calls) == 1 and calls[0]["forced_rungs"]


def test_a_stale_or_absent_probe_starts_nothing(monkeypatch, tmp_path) -> None:
    state = _probe_state(updated="2026-09-21T12:00:00Z", ready=[
        (probe.ECMWF_FAMILY, "2026-09-21T06:00:00Z", "2026-09-23"),
    ])
    worker, calls, _follows = _trigger_worker(tmp_path, monkeypatch, state)
    assert worker.run_arrival_triggered_captures(as_of_utc="2026-09-21T13:16:00Z")["state"] == "PROBE_STALE"
    monkeypatch.setattr(worker, "_arrival_probe_state", lambda: None)
    assert worker.run_arrival_triggered_captures(as_of_utc="2026-09-21T13:16:00Z")["state"] == "NO_PROBE_STATE"
    assert calls == []


def test_the_trigger_switch_restores_the_clock_ladder(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pw, "ARRIVAL_TRIGGER_ENABLED", False)
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    (tmp_path / probe.STATE_FILE_NAME).write_text(json.dumps(_probe_state()))
    assert worker._arrival_probe_state() is None
    assert worker.run_arrival_triggered_captures(as_of_utc="2026-09-21T13:16:00Z")["state"] == "DISABLED"


def test_the_follow_lane_asks_the_probe_first() -> None:
    from bayes_basket_target import follow_run

    run = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    as_of = datetime(2026, 9, 21, 14, 50, tzinfo=timezone.utc)  # 2h50m: past the 2.5h lag
    landed = _probe_state(updated="2026-09-21T14:50:00Z", listed=("2026-09-22",),
                          ready=[(probe.ICON_FAMILY, "2026-09-21T12:00:00Z", "2026-09-22")])
    not_yet = _probe_state(updated="2026-09-21T14:50:00Z", listed=("2026-09-22",))
    kwargs = dict(as_of=as_of, event_date="2026-09-22")
    assert follow_run.follow_publication_due(follow_run.ICON_FAMILY, run, probe_state=landed, **kwargs)
    assert not follow_run.follow_publication_due(follow_run.ICON_FAMILY, run, probe_state=not_yet, **kwargs)
    # No probe: the fixed lag, as before.
    assert follow_run.follow_publication_due(follow_run.ICON_FAMILY, run, as_of=as_of)
    early = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
    assert not follow_run.follow_publication_due(follow_run.ICON_FAMILY, run, as_of=early)


def test_every_attempt_lands_in_the_attempt_log(monkeypatch, tmp_path) -> None:
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    monkeypatch.setattr(pw, "CAPTURE_ATTEMPT_LOG_MAX_BYTES", 40)
    worker._log_capture_attempt({"event_date": "2026-09-23", "state": "CAPTURED"})
    worker._log_capture_attempt({"event_date": "2026-09-23", "state": "FAILED"})
    log = tmp_path / pw.CAPTURE_ATTEMPT_LOG_NAME
    assert json.loads(log.read_text().splitlines()[-1])["state"] == "FAILED"
    assert (tmp_path / (pw.CAPTURE_ATTEMPT_LOG_NAME + ".1")).is_file()


def test_a_real_pass_logs_its_rungs(monkeypatch, capture_worker, tmp_path) -> None:
    (tmp_path / "market_registry_forward.json").write_text("{}")

    class _Done:
        returncode = 3
        stdout = ""
        stderr = "Traceback ...\nContractError: official_native_forward_required_family_unavailable"

    monkeypatch.setattr(pw.subprocess, "run", lambda *a, **k: _Done())
    monkeypatch.setattr(pw.follow_run, "follow_enabled", lambda: False)
    result = _capture(
        capture_worker, tmp_path,
        arrival={"family": probe.GEFS_FAMILY, "run_init_utc": "2026-09-21T06:00:00Z"},
    )
    assert result["state"] == "SUBMITTED"
    assert result["trigger"] == "arrival:NOAA_GEFS_PDS_S3|2026-09-21T06:00:00Z"
    capture_worker._official_native_capture_inflight[("2026-09-23",)].result(timeout=30)
    (row,) = [json.loads(line) for line in (tmp_path / pw.CAPTURE_ATTEMPT_LOG_NAME).read_text().splitlines()]
    assert row["state"] == "FAILED" and row["trigger"].startswith("arrival:")
    assert [rung["returncode"] for rung in row["rungs"]] == [3] * len(row["rungs"]) and row["rungs"]
    assert "required_family_unavailable" in row["rungs"][0]["stderr_tail"]


# ------------------------------------ (三) one download per step object


import hashlib
import threading
import time as _time

from bayes_basket_target import native_raw_object_store as store_mod


def _fetcher(payloads, calls, *, delay=0.0):
    def make(key):
        def fetch():
            calls.append(key)
            if delay:
                _time.sleep(delay)
            return payloads[key], "Mon, 21 Sep 2026 14:45:59 GMT"
        return fetch
    return make


def test_a_whole_object_is_fetched_once_and_answered_from_the_store(tmp_path) -> None:
    store_mod.object_store_stats(reset=True)
    root = tmp_path / "icon_eps" / "raw"
    payloads = {"12/t_2m/x_2026092112_024_t_2m.grib2.bz2": b"A" * 1000}
    calls = []
    make = _fetcher(payloads, calls)
    key = next(iter(payloads))
    first = store_mod.cached_whole_object(object_key=key, fetch=make(key), root=root)
    second = store_mod.cached_whole_object(object_key=key, fetch=make(key), root=root)
    assert calls == [key]
    assert first[0] == second[0] and first[2] is False and second[2] is True
    # The Last-Modified of the original response comes back on a hit, so a
    # receipt built from it is the same one the download produced.
    assert second[1] == first[1] == "Mon, 21 Sep 2026 14:45:59 GMT"
    stats = store_mod.object_store_stats()["icon_eps"]
    assert stats == {"hits": 1, "hit_bytes": 1000, "downloads": 1, "downloaded_bytes": 1000}


def test_two_captures_of_one_run_download_a_shared_object_once(tmp_path) -> None:
    root = tmp_path / "icon_eps" / "raw"
    key = "00/t_2m/x_2026092100_030_t_2m.grib2.bz2"
    calls = []
    make = _fetcher({key: b"B" * 5000}, calls, delay=0.2)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(
            store_mod.cached_whole_object(object_key=key, fetch=make(key), root=root)))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == [key]
    assert sorted(hit for _data, _lm, hit in results) == [False, True, True]


def test_no_store_is_exactly_the_fetch(tmp_path) -> None:
    calls = []
    make = _fetcher({"k": b"C"}, calls)
    assert store_mod.cached_whole_object(object_key="k", fetch=make("k"), root=None)[2] is False
    assert store_mod.cached_whole_object(object_key="k", fetch=make("k"), root=None)[2] is False
    assert calls == ["k", "k"]


def test_a_corrupt_or_evicted_body_is_downloaded_again(tmp_path) -> None:
    root = tmp_path / "eccc_geps" / "raw"
    key = "20260921/WXO-DD/x_2026092100_P030_allmbrs.grib2"
    calls = []
    make = _fetcher({key: b"D" * 300}, calls)
    store_mod.cached_whole_object(object_key=key, fetch=make(key), root=root)
    body = root / (hashlib.sha256(b"D" * 300).hexdigest() + ".grib2")
    body.write_bytes(b"not the object")
    data, _lm, hit = store_mod.cached_whole_object(object_key=key, fetch=make(key), root=root)
    assert hit is False and data == b"D" * 300 and len(calls) == 2
    body.unlink()
    assert store_mod.cached_whole_object(object_key=key, fetch=make(key), root=root)[2] is False


def test_eviction_drops_whole_old_runs_keeps_the_newest_and_the_young(tmp_path) -> None:
    from bayes_basket_target import dwd_icon_eps_archive as icon

    root = tmp_path / "icon_eps" / "raw"
    for run in ("2026092018", "2026092100", "2026092106", "2026092112"):
        for step in (0, 3):
            key = f"x/t_2m/icon-eps_global_icosahedral_single-level_{run}_{step:03d}_t_2m.grib2.bz2"
            store_mod.cached_whole_object(
                object_key=key, fetch=lambda run=run, step=step: (f"{run}{step}".encode() * 100, ""), root=root,
            )
    later = _time.time() + 3600
    receipt = store_mod.evict_runs_over_cap(
        root, cap_bytes=2500, keep_newest_runs=2, run_of=icon.object_run_of, now=later,
    )
    assert receipt["dropped_runs"] == ["2026092018", "2026092100"]
    assert receipt["bytes_after"] <= 4 * 1200
    left = {icon.object_run_of(json.loads(p.read_text())["object_key"]) for p in (root / "ranges").glob("*.json")}
    assert left == {"2026092106", "2026092112"}
    # Nothing stored in the last ten minutes is touched, whatever the cap.
    store_mod.cached_whole_object(
        object_key="x/t_2m/icon-eps_global_icosahedral_single-level_2026091800_000_t_2m.grib2.bz2",
        fetch=lambda: (b"E" * 5000, ""), root=root,
    )
    young = store_mod.evict_runs_over_cap(root, cap_bytes=1, keep_newest_runs=0, run_of=icon.object_run_of)
    assert "2026091800" not in young["dropped_runs"]


def test_icon_and_eccc_objects_go_through_the_store(monkeypatch, tmp_path) -> None:
    from bayes_basket_target import dwd_icon_eps_archive as icon
    from bayes_basket_target import eccc_geps_archive as eccc

    monkeypatch.setenv("ICON_EPS_OBJECT_STORE", str(tmp_path / "icon"))
    monkeypatch.setenv("ECCC_GEPS_OBJECT_STORE", str(tmp_path / "eccc"))
    assert icon._object_store() == (tmp_path / "icon").resolve()
    assert eccc._object_store() == (tmp_path / "eccc").resolve()
    monkeypatch.setenv("ICON_EPS_OBJECT_STORE", "")
    assert icon._object_store() is None
    assert icon.object_run_of("00/t_2m/icon-eps_global_icosahedral_single-level_2026092100_054_t_2m.grib2.bz2") == "2026092100"
    assert eccc.object_run_of("20260921/WXO-DD/ensemble/geps/grib2/raw/00/030/CMC_geps-raw_TMP_TGL_2m_latlon0p5x0p5_2026092100_P030_allmbrs.grib2") == "2026092100"
    # ECCC: the receipt's Last-Modified is the original response's either way.
    seen = []
    monkeypatch.setattr(eccc, "_get_bytes", lambda url, timeout_seconds: seen.append(url) or (b"GRIB", {"last-modified": "LM"}))
    monkeypatch.setattr(eccc, "_split_grib_messages", lambda raw: [raw])
    monkeypatch.setattr(eccc, "_member_metadata", lambda raw: [{"forecast_hour": 30}])
    monkeypatch.setenv("ECCC_GEPS_OBJECT_STORE", str(tmp_path / "eccc"))
    run = datetime(2026, 9, 21, 0, tzinfo=timezone.utc)
    first = eccc._download_field(run_init=run, forecast_hour=30, timeout_seconds=5.0)
    second = eccc._download_field(run_init=run, forecast_hour=30, timeout_seconds=5.0)
    assert len(seen) == 1 and first[3] == second[3] and first[3]["last_modified"] == "LM"


def test_an_ecmwf_range_and_its_index_are_fetched_once(monkeypatch, tmp_path) -> None:
    from bayes_basket_target import ecmwf_ifs_archive as ecmwf

    store = tmp_path / "ecmwf_pf50" / "raw"
    calls = []

    def fake_get(url, *, timeout_seconds, max_bytes, headers=None):
        calls.append(url)
        return b"M" * 64, {"last-modified": "LM"}

    monkeypatch.setattr(ecmwf, "_get_bytes", fake_get)
    row = {"_offset": 100, "_length": 64, "number": 7, "step": 24}
    kwargs = dict(grib_url="https://x/a.grib2", index_sha256="i" * 64, object_key="a.grib2",
                  row=row, timeout_seconds=5.0, raw_roots=(store,), raw_store=store)
    first = ecmwf._download_message(**kwargs)
    second = ecmwf._download_message(**kwargs)
    assert len(calls) == 1 and first[2] is False and second[2] is True
    assert first[1] == second[1]


def test_a_gefs_field_and_its_idx_are_fetched_once(monkeypatch, tmp_path) -> None:
    from bayes_basket_target import noaa_gefs_archive as gefs

    store = tmp_path / "gefs" / "raw"
    idx = (
        b"62:11400000:d=2026092106:RH:2 m above ground:24 hour fcst:ENS=+7\n"
        b"63:11450212:d=2026092106:TMP:2 m above ground:24 hour fcst:ENS=+7\n"
        b"64:11450276:d=2026092106:UGRD:10 m above ground:24 hour fcst:ENS=+7\n"
    )
    calls = []

    def fake_get(url, *, client, timeout_seconds, max_bytes, headers=None):
        calls.append(url)
        if url.endswith(".idx"):
            return idx, {}
        return b"G" * 64, {"last-modified": "LM"}

    monkeypatch.setattr(gefs, "_get_bytes", fake_get)
    kwargs = dict(client=None, run_day="20260921", cycle="06", object_member="gep07",
                  member_id="perturbed_07", forecast_hour=24, timeout_seconds=5.0,
                  raw_roots=(store,), raw_store=store)
    first = gefs._download_gefs_field(**kwargs)
    second = gefs._download_gefs_field(**kwargs)
    assert len(calls) == 2  # one .idx, one range -- the second read fetched nothing
    assert first[0] == second[0] and first[1] == second[1]


def test_a_newly_listed_station_is_captured_now_once(capture_worker, tmp_path) -> None:
    ladder = pw._official_native_run_ladder(event_date="2026-09-23", as_of_utc="2026-09-21T13:15:00Z")
    capture_worker._official_native_capture_runs = {
        "2026-09-23": ladder[0][2].isoformat().replace("+00:00", "Z"),
    }
    assert _capture(capture_worker, tmp_path)["state"] == "RUN_ALREADY_CAPTURED"
    first = _capture(capture_worker, tmp_path, listing_station="ZUUU")
    assert first["reason"] == "forward_market_registry_missing"  # got as far as the pass
    # Once per (day, station): the next visit is the ordinary rotation again.
    assert _capture(capture_worker, tmp_path, listing_station="ZUUU")["state"] == "RUN_ALREADY_CAPTURED"


def test_the_store_answers_reach_the_attempt_log(monkeypatch, capture_worker, tmp_path) -> None:
    (tmp_path / "market_registry_forward.json").write_text("{}")
    receipt = {
        "run_init_utc_by_family": {"NOAA_GEFS_PDS_S3": "2026-09-21T06:00:00Z",
                                   "ECMWF_OPEN_DATA_S3": "2026-09-21T06:00:00Z"},
        "nwp_object_store": {"icon_eps": {"hits": 11, "hit_bytes": 396000000, "downloads": 8,
                                          "downloaded_bytes": 288000000}},
    }

    class _Ok:
        returncode = 0
        stdout = json.dumps(receipt)
        stderr = ""

    monkeypatch.setattr(pw.subprocess, "run", lambda *a, **k: _Ok())
    monkeypatch.setattr(pw.follow_run, "follow_enabled", lambda: False)
    monkeypatch.setattr(pw, "_merge_official_native_inbox", lambda *a, **k: {"packet_count": 0, "pruned": 0})
    monkeypatch.setattr(pw, "_active_registry_scopes", lambda *a, **k: set())
    monkeypatch.setattr(pw, "_fitted_lead_buckets", lambda *a, **k: {})
    capture_worker._official_native_capture_merge_lock = threading.Lock()
    capture_worker.conn = None
    result = _capture(capture_worker, tmp_path, arrival={"family": probe.ECMWF_FAMILY,
                                                          "run_init_utc": "2026-09-21T06:00:00Z"})
    assert result["state"] == "SUBMITTED"
    capture_worker._official_native_capture_inflight[("2026-09-23",)].result(timeout=30)
    (row,) = [json.loads(line) for line in (tmp_path / pw.CAPTURE_ATTEMPT_LOG_NAME).read_text().splitlines()]
    assert row["state"] == "CAPTURED"
    assert row["rungs"][0]["object_store"]["icon_eps"]["hits"] == 11


def test_a_run_beyond_the_ladder_goes_to_the_follow_lane(monkeypatch, tmp_path) -> None:
    state = _probe_state(updated="2026-09-22T05:10:00Z", listed=("2026-09-24",), ready=[
        (probe.ECMWF_FAMILY, "2026-09-21T18:00:00Z", "2026-09-24"),
    ])
    worker, calls, follows = _trigger_worker(tmp_path, monkeypatch, state)
    worker.run_arrival_triggered_captures(as_of_utc="2026-09-22T05:10:30Z")
    assert calls == []
    assert follows == [{"event_date": "2026-09-24", "lead": 3,
                        "run_init": datetime(2026, 9, 21, 18, tzinfo=timezone.utc)}]


# ------------------------------------------ (四) outage is not "not published"


from bayes_basket_target import transport_breaker as tb


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _breaker(probe_answers, **kwargs):
    clock = _Clock()
    answers = list(probe_answers)
    probes = []

    def probe(url):
        probes.append(clock.now)
        return answers.pop(0) if answers else True

    breaker = tb.TransportBreaker(
        threshold=kwargs.get("threshold", 4), pause_min_sec=30, pause_max_sec=60,
        max_wait_sec=kwargs.get("max_wait_sec", 300), probe=probe, clock=clock, sleep=clock.sleep,
    )
    return breaker, clock, probes


def test_a_blip_pauses_then_carries_on() -> None:
    breaker, clock, probes = _breaker([True])
    for _ in range(4):
        breaker.note_transport_error("https://storage.googleapis.com/x")
    breaker.guard("https://storage.googleapis.com/x")   # waits, probes once, closes
    assert len(probes) == 1 and 1030 <= probes[0] <= 1060
    snap = breaker.snapshot()
    assert snap["trips"] == 1 and snap["open"] is False and snap["paused_sec"] >= 30
    breaker.guard("https://storage.googleapis.com/x")   # closed: no wait
    assert len(probes) == 1


def test_an_outage_gives_up_with_the_outage_marker() -> None:
    breaker, clock, probes = _breaker([False] * 50)
    for _ in range(4):
        breaker.note_transport_error("https://noaa-gefs-pds.s3.amazonaws.com/x")
    with pytest.raises(tb.ContractError, match=r"^nwp_transport_outage:noaa-gefs-pds"):
        breaker.guard("https://noaa-gefs-pds.s3.amazonaws.com/x")
    assert 4 <= len(probes) <= 10
    # Once given up, every later request fails at once rather than waiting again.
    with pytest.raises(tb.ContractError):
        breaker.guard("https://noaa-gefs-pds.s3.amazonaws.com/y")


def test_an_http_answer_resets_the_count() -> None:
    breaker, _clock, probes = _breaker([])
    for _ in range(3):
        breaker.note_transport_error("https://x/")
    breaker.note_answer()           # a 404 is an answer: not the network
    breaker.note_transport_error("https://x/")
    breaker.guard("https://x/")
    assert probes == [] and breaker.snapshot()["trips"] == 0


def test_every_family_waits_at_the_breaker_and_reports_to_it(monkeypatch) -> None:
    import httpx

    from bayes_basket_target import dwd_icon_eps_archive as icon
    from bayes_basket_target import eccc_geps_archive as eccc
    from bayes_basket_target import ecmwf_ifs_archive as ecmwf
    from bayes_basket_target import noaa_gefs_archive as gefs

    events = []
    monkeypatch.setattr(tb, "guard", lambda url: events.append(("guard", url)))
    monkeypatch.setattr(tb, "note_transport_error", lambda url: events.append(("error", url)))
    monkeypatch.setattr(tb, "note_answer", lambda: events.append(("answer", None)))
    monkeypatch.setattr(ecmwf.time_module, "sleep", lambda s: None)
    monkeypatch.setattr(gefs.time_module, "sleep", lambda s: None)
    monkeypatch.setattr(eccc.time_module, "sleep", lambda s: None)

    class _Boom:
        def stream(self, *a, **k):
            raise httpx.ConnectError("tunnel down")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ecmwf, "_thread_http_client", lambda: _Boom())
    with pytest.raises(Exception):
        ecmwf._get_bytes("https://storage.googleapis.com/e", timeout_seconds=1, max_bytes=10)
    with pytest.raises(Exception):
        gefs._get_bytes("https://noaa-gefs-pds.s3.amazonaws.com/g", client=_Boom(), timeout_seconds=1, max_bytes=10)
    monkeypatch.setattr(eccc.httpx, "Client", lambda **k: _Boom())
    with pytest.raises(Exception):
        eccc._get_bytes("https://dd.weather.gc.ca/c", timeout_seconds=1)
    with pytest.raises(Exception):
        icon._fetch(_Boom(), "https://opendata.dwd.de/i", attempts=2, sleep=lambda s: None)
    for host in ("storage.googleapis.com", "noaa-gefs-pds", "dd.weather.gc.ca", "opendata.dwd.de"):
        guards = [e for e in events if e[0] == "guard" and host in e[1]]
        errors = [e for e in events if e[0] == "error" and host in e[1]]
        assert guards and len(errors) == len(guards), host
    assert not [e for e in events if e[0] == "answer"]


def test_a_held_family_is_not_asked_for(monkeypatch, tmp_path) -> None:
    """The required-first recovery pass holds ICON/ECCC without the network."""

    from tests.test_charter_148_hold_from_other_lead import (
        ECCC, ECMWF, GEFS, ICON, _archive, _setup,
    )
    import scripts.capture_official_native_ensemble_forward as capture

    registry_path, _reads, _written, _commands = _setup(
        monkeypatch, tmp_path,
        on_disk={
            "eccc_2026-08-01_d0.json": _archive(ECCC, "2026-07-31T00:00:00Z"),
            "icon_2026-08-01_d0.json": _archive(ICON, "2026-07-31T00:00:00Z"),
        },
        fresh={
            "capture_gefs_station_days": _archive(GEFS, "2026-08-01T00:00:00Z"),
            "capture_ecmwf_station_days": _archive(ECMWF, "2026-08-01T00:00:00Z"),
            "capture_geps_station_days": _archive(ECCC, "2026-08-01T00:00:00Z"),
            "capture_icon_eps_station_days": _archive(ICON, "2026-08-01T00:00:00Z"),
        },
    )
    asked = []
    for name in ("capture_geps_station_days", "capture_icon_eps_station_days"):
        monkeypatch.setattr(capture, name, lambda name=name, **_kw: asked.append(name))
    day = "2026-08-01"
    receipt = capture.capture_official_native_forward(
        registry_path=registry_path, event_date=day,
        gefs_output=tmp_path / f"gefs_{day}_d0.json", ecmwf_output=tmp_path / f"ecmwf_{day}_d0.json",
        eccc_output=tmp_path / f"eccc_{day}_d0.json", icon_output=tmp_path / f"icon_{day}_d0.json",
        inbox_output=tmp_path / "inbox.json", timeout_seconds=30.0, lead_days=0, run_cycle_hour=0,
        hold_families=("ECCC_DD_WEATHER", "DWD_OPENDATA_ICON_EPS"),
    )
    assert asked == []
    by_family = receipt["run_init_utc_by_family"]
    assert by_family["NOAA_GEFS_PDS_S3"] == by_family["ECMWF_OPEN_DATA_S3"] == "2026-08-01T00:00:00Z"
    assert by_family["ECCC_DD_WEATHER"] == "2026-07-31T00:00:00Z"
    assert "family_held_by_caller" in receipt["family_held_on_earlier_run"]["DWD_OPENDATA_ICON_EPS"]


# --------------------------- (四) worker: network backoff, no walk, recovery


def test_network_text_is_told_from_publication_text() -> None:
    assert pw._capture_text_is_network("ContractError: nwp_transport_outage:storage.googleapis.com:301s")
    assert pw._capture_text_is_network("NOAA_GEFS_PDS_S3=ContractError:gefs_archive_transport_failed:ConnectError")
    assert pw._capture_text_is_network("httpx.PoolTimeout")
    assert not pw._capture_text_is_network("ECMWF_OPEN_DATA_S3=HTTPStatusError:Client error '404 Not Found'")
    assert not pw._capture_text_is_network("icon_eps_archive_object_unavailable")


class _Completed:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _pass_worker(monkeypatch, tmp_path, runs):
    (tmp_path / "market_registry_forward.json").write_text("{}")
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    worker._official_native_capture_pool = None
    worker._official_native_capture_merge_lock = threading.Lock()
    worker.conn = None
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return runs[min(len(calls), len(runs)) - 1]

    monkeypatch.setattr(pw.subprocess, "run", fake_run)
    monkeypatch.setattr(pw.follow_run, "follow_enabled", lambda: False)
    monkeypatch.setattr(pw, "_merge_official_native_inbox", lambda *a, **k: {"packet_count": 0, "pruned": 0})
    monkeypatch.setattr(pw, "_active_registry_scopes", lambda *a, **k: set())
    monkeypatch.setattr(pw, "_fitted_lead_buckets", lambda *a, **k: {})
    return worker, calls


def _finish(worker, day="2026-09-23"):
    return worker._official_native_capture_inflight[(day,)].result(timeout=30)


def test_a_network_failure_neither_walks_the_ladder_nor_spends_the_floor(monkeypatch, tmp_path) -> None:
    down = _Completed(2, stderr="ContractError: official_native_forward_required_family_unavailable:"
                                 "ECMWF_OPEN_DATA_S3,NOAA_GEFS_PDS_S3:NOAA_GEFS_PDS_S3=ContractError:nwp_transport_outage:x:300s")
    worker, calls = _pass_worker(monkeypatch, tmp_path, [down])
    # The egress check reads the live supply watch and probes the network; a
    # healthy afternoon answered "back" and turned this red.  Down until the
    # test says otherwise.
    monkeypatch.setattr(worker, "_egress_answered_since", lambda since: False)
    try:
        assert _capture(worker, tmp_path)["state"] == "SUBMITTED"
        outcome = _finish(worker)
        assert outcome["state"] == "NETWORK_UNAVAILABLE"
        assert len(calls) == 1                         # no walk down the ladder
        # Reaped: the floor is released and a backoff of 60s +-20% set.
        second = _capture(worker, tmp_path)
        assert second["state"] == "NETWORK_BACKOFF"
        assert 40 <= second["retry_after_sec"] <= 72
        # The egress answered since: the day goes at once.
        monkeypatch.setattr(worker, "_egress_answered_since", lambda since: True)
        assert _capture(worker, tmp_path)["state"] == "SUBMITTED"
    finally:
        worker._official_native_capture_pool.shutdown(wait=True)


def test_a_backoff_grows_to_ten_minutes(monkeypatch, tmp_path) -> None:
    down = _Completed(2, stderr="nwp_transport_outage:x:300s")
    worker, _calls = _pass_worker(monkeypatch, tmp_path, [down])
    monkeypatch.setattr(worker, "_egress_answered_since", lambda since: False)
    try:
        delays = []
        for _ in range(6):
            backoff = worker._official_native_network_backoff_map()
            if "2026-09-23" in backoff:
                backoff["2026-09-23"]["until_mono"] = 0.0
            assert _capture(worker, tmp_path)["state"] == "SUBMITTED"
            _finish(worker)
            delays.append(_capture(worker, tmp_path)["retry_after_sec"])
        assert delays[0] < 80 and 470 <= delays[-1] <= 720
    finally:
        worker._official_native_capture_pool.shutdown(wait=True)


def test_a_required_family_held_while_its_run_still_lands_is_waited_for(monkeypatch, tmp_path) -> None:
    receipt = json.dumps({
        "family_held_on_earlier_run": {"ECMWF_OPEN_DATA_S3": "HTTPStatusError:404 Not Found"},
        # ECMWF still on 00Z, so the old walk would go on to the 06Z rung.
        "run_init_utc_by_family": {"NOAA_GEFS_PDS_S3": "2026-09-21T12:00:00Z", "ECMWF_OPEN_DATA_S3": "2026-09-21T00:00:00Z"},
    })
    ok = _Completed(0, stdout=receipt)
    worker, calls = _pass_worker(monkeypatch, tmp_path, [ok])
    run = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    ladder = [(2, 12, run), (2, 6, datetime(2026, 9, 21, 6, tzinfo=timezone.utc))]
    publishing = _probe_state(updated=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                              ready=[(probe.GEFS_FAMILY, "2026-09-21T12:00:00Z", "2026-09-23")])
    monkeypatch.setattr(worker, "_arrival_probe_state", lambda: publishing)
    monkeypatch.setattr(pw, "_probe_run_publishing", lambda state, **k: k["family"] == probe.ECMWF_FAMILY)
    try:
        _capture(worker, tmp_path, forced_rungs=ladder)
        _finish(worker)
        assert len(calls) == 1          # stayed on 12Z: no walk to 06Z
    finally:
        worker._official_native_capture_pool.shutdown(wait=True)
    # Without the probe saying so, the walk is as before.
    worker2, calls2 = _pass_worker(monkeypatch, tmp_path, [ok])
    monkeypatch.setattr(pw, "_probe_run_publishing", lambda state, **k: False)
    try:
        _capture(worker2, tmp_path, forced_rungs=ladder)
        _finish(worker2)
        assert len(calls2) == 2
    finally:
        worker2._official_native_capture_pool.shutdown(wait=True)


def test_the_publishing_reading_needs_the_probe_to_speak() -> None:
    now = datetime(2026, 9, 21, 7, 50, tzinfo=timezone.utc)
    run = datetime(2026, 9, 21, 0, tzinfo=timezone.utc)
    state = _probe_state(updated="2026-09-21T07:50:00Z", listed=("2026-09-23",))
    assert pw._probe_run_publishing(state, family=probe.ECMWF_FAMILY, run_init=run, event_date="2026-09-23", now=now)
    landed = _probe_state(updated="2026-09-21T07:50:00Z", listed=("2026-09-23",),
                          ready=[(probe.ECMWF_FAMILY, "2026-09-21T00:00:00Z", "2026-09-23")])
    assert not pw._probe_run_publishing(landed, family=probe.ECMWF_FAMILY, run_init=run, event_date="2026-09-23", now=now)
    assert not pw._probe_run_publishing(None, family=probe.ECMWF_FAMILY, run_init=run, event_date="2026-09-23", now=now)
    stale = _probe_state(updated="2026-09-21T06:00:00Z", listed=("2026-09-23",))
    assert not pw._probe_run_publishing(stale, family=probe.ECMWF_FAMILY, run_init=run, event_date="2026-09-23", now=now)


def test_recovery_takes_the_required_families_first_then_the_full_passes(monkeypatch, tmp_path) -> None:
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    backoff = worker._official_native_network_backoff_map()
    for day in ("2026-09-22", "2026-09-23", "2026-09-24"):
        backoff[day] = {"attempts": 3, "until_mono": 1e12, "since_utc": "2026-09-22T07:00:00Z"}
    answered = {"now": False}
    monkeypatch.setattr(worker, "_egress_answered_since", lambda since: answered["now"])
    monkeypatch.setattr(worker, "_arrival_probe_state", lambda: None)
    submitted = []

    class _Future:
        def __init__(self):
            self.finished = False

        def done(self):
            return self.finished

        def result(self):
            return {"state": "CAPTURED"}

    futures = {}

    def fake_capture(**kwargs):
        submitted.append((kwargs["event_date"], tuple(kwargs.get("hold_families") or ()), bool(kwargs.get("forced_rungs"))))
        future = _Future()
        futures[kwargs["event_date"]] = future
        worker._official_native_capture_inflight = {(d,): f for d, f in futures.items()}
        return {"state": "SUBMITTED"}

    monkeypatch.setattr(worker, "_capture_official_native_packet", fake_capture)
    assert worker.run_network_recovery_recaptures(as_of_utc="2026-09-22T07:40:00Z")["state"] == "EGRESS_DOWN"
    assert submitted == []
    answered["now"] = True
    worker.run_network_recovery_recaptures(as_of_utc="2026-09-22T07:50:00Z")
    # D+1 first, then D+0, then D+2 -- all required-only, ICON/ECCC held.
    assert [d for d, _h, _f in submitted] == ["2026-09-23", "2026-09-22", "2026-09-24"]
    assert all(h == pw.RECOVERY_HOLD_FAMILIES for _d, h, _f in submitted)
    submitted.clear()
    futures["2026-09-23"].finished = True
    worker.run_network_recovery_recaptures(as_of_utc="2026-09-22T07:52:00Z")
    assert submitted == []            # no full pass while a required-only pass runs
    for future in futures.values():
        future.finished = True
    monkeypatch.setattr(pw, "_official_native_run_ladder", lambda **k: [(1, None, datetime(2026, 9, 22, tzinfo=timezone.utc))])
    worker.run_network_recovery_recaptures(as_of_utc="2026-09-22T07:55:00Z")
    assert [(d, h, f) for d, h, f in submitted] == [
        ("2026-09-23", (), True), ("2026-09-22", (), True), ("2026-09-24", (), True),
    ]


def test_the_supply_watch_priority_orders_the_recovery(monkeypatch, tmp_path) -> None:
    from bayes_basket_target import supply_watch

    watch = tmp_path / "supply_watch.json"
    watch.write_text(json.dumps({"recapture_priority": [{"event_date": "2026-09-22"}, {"event_date": "2026-09-23"}]}))
    monkeypatch.setattr(supply_watch, "DEFAULT_OUTPUT", watch)
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    for day in ("2026-09-23", "2026-09-22"):
        worker._official_native_network_backoff_map()[day] = {"attempts": 1, "until_mono": 1e12, "since_utc": "2026-09-22T07:00:00Z"}
    monkeypatch.setattr(worker, "_egress_answered_since", lambda since: True)
    order = []
    monkeypatch.setattr(worker, "_capture_official_native_packet",
                        lambda **k: order.append(k["event_date"]) or {"state": "SUBMITTED"})
    worker.run_network_recovery_recaptures(as_of_utc="2026-09-22T07:50:00Z")
    assert order == ["2026-09-22", "2026-09-23"]


def test_the_egress_reading_uses_the_supply_watch_first(monkeypatch, tmp_path) -> None:
    from bayes_basket_target import supply_watch

    watch = tmp_path / "supply_watch.json"
    watch.write_text(json.dumps({"generated_at_utc": "2026-09-22T07:50:00Z", "egress": {"state": "ok"}}))
    monkeypatch.setattr(supply_watch, "DEFAULT_OUTPUT", watch)
    monkeypatch.setattr(supply_watch, "http_head_probe", lambda *a: pytest.fail("the file answered"))
    worker = object.__new__(pw.TargetProductionWorker)
    assert worker._egress_answered_since("2026-09-22T07:40:00Z") is True
    # Older than the failure: ask directly, at most once a minute.
    asked = []
    monkeypatch.setattr(supply_watch, "http_head_probe", lambda url, proxy, t: asked.append(url) or {"outcome": "transport_error"})
    assert worker._egress_answered_since("2026-09-22T08:00:00Z") is False
    assert worker._egress_answered_since("2026-09-22T08:00:00Z") is False
    assert len(asked) == 1


def test_a_pass_that_held_a_required_family_on_the_network_backs_off_too(monkeypatch, tmp_path) -> None:
    receipt = json.dumps({
        "family_held_on_earlier_run": {"NOAA_GEFS_PDS_S3": "ContractError:gefs_archive_transport_failed:ConnectError"},
        "run_init_utc_by_family": {"NOAA_GEFS_PDS_S3": "2026-09-21T00:00:00Z", "ECMWF_OPEN_DATA_S3": "2026-09-21T06:00:00Z"},
    })
    worker, calls = _pass_worker(monkeypatch, tmp_path, [_Completed(0, stdout=receipt)])
    monkeypatch.setattr(worker, "_egress_answered_since", lambda since: False)
    try:
        _capture(worker, tmp_path)
        outcome = _finish(worker)
        assert outcome["state"] == "CAPTURED" and outcome["network_held_families"] == ["NOAA_GEFS_PDS_S3"]
        assert len(calls) == 1
        assert _capture(worker, tmp_path)["state"] == "NETWORK_BACKOFF"
    finally:
        worker._official_native_capture_pool.shutdown(wait=True)


def test_the_recovery_pass_keeps_the_follow_step_and_queues_no_follow_capture(monkeypatch, tmp_path) -> None:
    """2026-09-24 14:05Z: the required-first recovery pass ran without the
    follow step, so its packets carried no AIFS ENS or AIGEFS -- the follow
    step is the only way those two enter a packet -- and the inbox merge took
    them over the six-system packets for every D+1 station until 16:03Z.
    The step reads retained files and certificates only; what the recovery
    pass must not do is queue follow captures."""

    receipt = json.dumps({
        "run_init_utc_by_family": {"NOAA_GEFS_PDS_S3": "2026-09-21T06:00:00Z", "ECMWF_OPEN_DATA_S3": "2026-09-21T06:00:00Z"},
    })
    worker, calls = _pass_worker(monkeypatch, tmp_path, [_Completed(0, stdout=receipt)])
    monkeypatch.setattr(pw.follow_run, "follow_enabled", lambda: True)
    queued = []
    monkeypatch.setattr(worker, "_enqueue_follow_run_capture", lambda **kw: queued.append(kw))
    try:
        _capture(worker, tmp_path, hold_families=("ECCC_DD_WEATHER", "DWD_OPENDATA_ICON_EPS"))
        assert _finish(worker)["state"] == "CAPTURED"
    finally:
        worker._official_native_capture_pool.shutdown(wait=True)
    command = calls[0]
    assert "--follow-new-runs" in command
    assert command.count("--hold-family") == 2
    assert queued == []


def test_a_nested_fetch_lock_rides_the_stripe_already_held(tmp_path) -> None:
    """2026-09-22 10:39Z: GEFS took its field's stripe, then the .idx's -- a
    second flock on the same stripe file blocked its own thread forever."""

    done = []

    def nested():
        for outer in range(40):
            with store_mod.fetch_lock(tmp_path, f"field-{outer}"):
                with store_mod.fetch_lock(tmp_path, f"field-{outer}.idx"):
                    with store_mod.fetch_lock(tmp_path, f"field-{outer}"):
                        pass
        done.append(True)

    threads = [threading.Thread(target=nested) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert done == [True] * 4


def test_the_gefs_field_and_idx_do_not_deadlock_across_threads(monkeypatch, tmp_path) -> None:
    from bayes_basket_target import noaa_gefs_archive as gefs

    idx = (
        b"62:11400000:d=2026092106:RH:2 m above ground:24 hour fcst:ENS=+7\n"
        b"63:11450212:d=2026092106:TMP:2 m above ground:24 hour fcst:ENS=+7\n"
        b"64:11450276:d=2026092106:UGRD:10 m above ground:24 hour fcst:ENS=+7\n"
    )
    monkeypatch.setattr(
        gefs, "_get_bytes",
        lambda url, *, client, timeout_seconds, max_bytes, headers=None:
            (idx, {}) if url.endswith(".idx") else (b"G" * 64, {"last-modified": "LM"}),
    )
    store = tmp_path / "gefs" / "raw"
    results = []

    def one(member):
        for hour in range(0, 75, 3):
            results.append(gefs._download_gefs_field(
                client=None, run_day="20260921", cycle="06", object_member=member,
                member_id=member, forecast_hour=hour, timeout_seconds=5.0,
                raw_roots=(store,), raw_store=store,
            )[0])

    threads = [threading.Thread(target=one, args=(f"gep{n:02d}",)) for n in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == 8 * 25


def test_an_icon_or_eccc_arrival_rides_the_newest_landed_rung_not_the_clocks(monkeypatch, tmp_path) -> None:
    # Charter 193 (三), 2026-09-23 08:00Z: ECCC 00Z's arrival for 09-24 took
    # the clock-newest 06Z rung, which neither ECMWF nor GEFS had published,
    # and both held 09-22 18Z while ECMWF 00Z was on the replica.
    state = _probe_state(updated="2026-09-23T08:01:00Z", listed=("2026-09-24",), ready=[
        (probe.ECCC_FAMILY, "2026-09-23T00:00:00Z", "2026-09-24"),
        (probe.GEFS_FAMILY, "2026-09-23T00:00:00Z", "2026-09-24"),
        (probe.ECMWF_FAMILY, "2026-09-23T00:00:00Z", "2026-09-24"),
    ])
    for row in state["ready"].values():
        # ECCC seen first (05:05Z in production), so its pass takes the day
        row["first_seen_at_utc"] = "2026-09-23T05:30:00Z" if row["family"] == probe.ECCC_FAMILY else "2026-09-23T07:59:00Z"
    worker, calls, _follows = _trigger_worker(tmp_path, monkeypatch, state)
    worker.run_arrival_triggered_captures(as_of_utc="2026-09-23T08:01:30Z")
    (call,) = calls
    assert call["arrival"]["family"] == probe.ECCC_FAMILY
    (rung,) = call["forced_rungs"]
    assert rung[0] == 1 and rung[2] == datetime(2026, 9, 23, 0, tzinfo=timezone.utc)
