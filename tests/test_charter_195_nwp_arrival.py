"""Charter 195: model runs captured on arrival, compiled on capture.

(一) a run merged into the official inbox mid-cycle is compiled into P0 at the
next fast-lane checkpoint -- P0, then P0-M, then P1 for what it re-rooted --
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
    worker.taf_inbox_path = tmp_path / "taf_inbox.json"
    worker.taf_inbox_path.write_bytes(b"{}")
    worker.canonical_source_conn = object()
    worker._probability_chain_ran = chain_ran
    worker._p0m_covered_taf_watermark = pw._taf_inbox_watermark(worker.taf_inbox_path)
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


def test_a_merged_run_after_the_chain_goes_p0_then_p0m_then_p1(monkeypatch, tmp_path) -> None:
    worker, inbox = _worker(tmp_path)
    inbox.write_bytes(b'{"packets": ["new run"]}')
    order = []

    def fake_p0(**kwargs):
        order.append(("p0", kwargs["materialize"]))
        assert kwargs["official_watermark"] == pw._official_inbox_watermark(inbox)
        return {"state": "RAN", "new_envelopes": True, "rowid_before": 700}

    def fake_p0m(**kwargs):
        order.append(("p0m", kwargs["force"]))
        return {"ran": True, "new_envelopes": True, "rowid_before": 710}

    rooted = {700: [("ZUUU", "2026-09-23")], 710: [("ZUUU", "2026-09-23"), ("KLAX", "2026-09-22")]}
    monkeypatch.setattr(worker, "_fast_lane_official_p0", fake_p0)
    monkeypatch.setattr(worker, "_materialize_early_p0m", fake_p0m)
    monkeypatch.setattr(worker, "_scopes_rooted_after", lambda mark: rooted.get(mark, []))
    seen = {}

    def fake_p1(**kwargs):
        order.append(("p1", None))
        seen.update(kwargs)
        return {"ran": True, "p1": {"state": "COMMITTED"}}

    monkeypatch.setattr(worker, "_materialize_early_p1", fake_p1)
    receipt = worker._fast_lane_checkpoint("before_decisions")
    assert order == [("p0", True), ("p0m", True), ("p1", None)]
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
    monkeypatch.setattr(worker, "_materialize_early_p0m", lambda **k: pytest.fail("the chain re-roots"))
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
