"""The WRF board reading: the local day's maximum and a missing volume."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bayes_basket_target import wrf_readings as W


def test_day_max_takes_the_local_day_and_marks_peak_coverage() -> None:
    zone = ZoneInfo("Asia/Shanghai")
    frames = {f"2026-10-19_{h:02d}:00:00": 20.0 + h for h in range(0, 16)}  # local 08..23
    frames["2026-10-19_16:00:00"] = 99.0  # local 00:00 next day: not this day
    m = W._day_max(frames, "2026-10-19", zone)
    assert m["tmax_c"] == 35.0 and m["at_local"] == "23:00" and m["peak_covered"]
    short = {k: v for k, v in frames.items() if k < "2026-10-19_06:00:00"}  # through local 13:00
    assert not W._day_max(short, "2026-10-19", zone)["peak_covered"]
    assert W._day_max(frames, "2026-10-21", zone) is None


def test_missing_volume_is_reported_not_raised(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    out = W.compute(conn, root=tmp_path / "absent", now=datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert out == {"state": "volume_missing", "root": str(tmp_path / "absent")}


def test_a_forecast_run_covers_every_day_from_its_init_to_its_last_day(tmp_path: Path) -> None:
    """One run/forecast3d.sh run covers today and the next three days (user 2026-10-22
    「把未来3日都计算」); oldest init first, so a newer init replaces an older one only
    for the days it has reached."""

    for name in ("20261022_from_2026102118", "20261025_from_2026102200"):
        d = tmp_path / "run" / "fcst" / name
        d.mkdir(parents=True)
        (d / "wrfout_d01_x").write_bytes(b"")
    runs = [(c, day, d.name) for c, day, d in W.run_dirs(tmp_path) if c == W.FORECAST]
    assert runs == [(W.FORECAST, "20261021", "20261022_from_2026102118"),
                    (W.FORECAST, "20261022", "20261022_from_2026102118"),
                    (W.FORECAST, "20261022", "20261025_from_2026102200"),
                    (W.FORECAST, "20261023", "20261025_from_2026102200"),
                    (W.FORECAST, "20261024", "20261025_from_2026102200"),
                    (W.FORECAST, "20261025", "20261025_from_2026102200")]


def test_nowcast_arms_and_same_moment_record(tmp_path: Path) -> None:
    """The forward test (user 2026-10-22 「做当天临近预报的前向测试」): both arms are
    runs of their own, and the day's latest log record (market and served belief as
    of the moment the nowcast came out) is what the day carries."""

    import json  # noqa: PLC0415

    base = tmp_path / "run" / "nowcast"
    for arm in ("sat", "ctl"):
        d = base / "20261022" / arm
        d.mkdir(parents=True)
        (d / "wrfout_d01_x").write_bytes(b"")
    runs = sorted((c, day) for c, day, _ in W.run_dirs(tmp_path))
    assert runs == [("nowcast_ctl", "20261022"), ("nowcast_sat", "20261022")]
    lines = [{"day": "2026-10-22", "station": "ZUUU", "ready_utc": "2026-10-22T03:00:00Z", "market": {"mean_c": 18.0}},
             {"day": "2026-10-22", "station": "ZUUU", "ready_utc": "2026-10-22T04:05:00Z", "market": {"mean_c": 18.6}}]
    (base / "log.jsonl").write_text("\n".join(json.dumps(r) for r in lines) + "\nnot json\n", encoding="utf-8")
    recs = W.nowcast_records(tmp_path)
    assert list(recs) == ["2026-10-22"] and recs["2026-10-22"]["market"]["mean_c"] == 18.6


def test_stored_view_for_the_station_board(tmp_path: Path, monkeypatch) -> None:
    """The station board's WRF card reads the stored reading only: ZUUU's day, nothing
    for a station WRF does not run, a state for a missing day."""

    from bayes_basket_target import board_readings  # noqa: PLC0415

    value = {"state": "ok", "as_of_utc": "t", "stations": {"ZUUU": {
        "timezone": "Asia/Shanghai", "scores": {"satcld_rhc85": {"n": 13, "mae_c": 1.02}},
        "nowcast_scores": {}, "days": {"2026-10-22": {
            "truth_c": None, "gfs": {"tmax_c": 16.5}, "nowcast": {"sat_obs_c": 19.4},
            "configs": {"nowcast_sat": {"tmax_c": 19.4, "series": [], "status": "complete"},
                        "cu_gf": {"tmax_c": 1.0}}}}}}}
    monkeypatch.setattr(board_readings, "load_reading", lambda name: {"value": value})
    v = W.stored("ZUUU", "2026-10-22")
    assert v["state"] == "ok" and list(v["runs"]) == ["nowcast_sat"] and v["oracle_score"]["mae_c"] == 1.02
    assert W.stored("KLAX", "2026-10-22") is None
    assert W.stored("ZUUU", "2026-10-30") == {"state": "no_run"}


def test_archived_hindcast_runs_are_filled_from_the_frozen_summary(tmp_path: Path, monkeypatch) -> None:
    """Hindcast files archived (user 2026-10-22 「归档实验数据，冻结配置」): the frozen
    summary keeps their day maxima and errors on the board; a run still on disk wins."""

    import json  # noqa: PLC0415

    (tmp_path / "run").mkdir()
    (tmp_path / "run" / W.FROZEN_HINDCAST).write_text(json.dumps({"stations": {"ZUUU": {"days": {
        "2026-10-14": {"gfs": {"tmax_c": 25.5}, "configs": {
            "dom_big": {"tmax_c": 26.3, "status": "complete", "peak_covered": True, "series": [], "error_c": 9.9}}}}}}}),
        encoding="utf-8")
    monkeypatch.setattr(W, "_zone", lambda conn, st: ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(W, "_truth", lambda conn, st: {"2026-10-14": 25.0})
    out = W.compute(sqlite3.connect(":memory:"), root=tmp_path, now=datetime(2026, 10, 22, tzinfo=timezone.utc))
    S = out["stations"]["ZUUU"]
    assert S["days"]["2026-10-14"]["configs"]["dom_big"]["error_c"] == 1.3   # recomputed, not the stored 9.9
    assert S["scores"]["dom_big"] == {"n": 1, "bias_c": 1.3, "mae_c": 1.3, "hit": 0.0}
    assert S["gfs_score"]["n"] == 1


def test_forecast_history_survives_removed_runs_and_gives_the_change(tmp_path: Path, monkeypatch) -> None:
    """Auto-updating forecasts (user 2026-10-22 「WRF应该是自动更新变化的」): every init's
    maximum per day is kept after its run directory is gone, and the standing run
    reports its change against the run before it."""

    d = tmp_path / "run" / "fcst" / "20261025_from_2026102206"
    d.mkdir(parents=True)
    (d / "wrfout_d01_x").write_bytes(b"")
    monkeypatch.setattr(W, "_zone", lambda conn, st: ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(W, "_truth", lambda conn, st: {})
    monkeypatch.setattr(W, "_domain_files", lambda d: ("d01", ["f"]))
    monkeypatch.setattr(W, "_status", lambda d, now: "complete")

    class Cache:
        read = 0

        def frames(self, path, var, point):
            # 2026-10-23 local 10:00..18:00 = 02..10Z, peak 20.0 C
            return {f"2026-10-23_{h:02d}:00:00": 15.0 + h / 2 for h in range(2, 11)} if var == "T2" else {}

    runs = {"ZUUU": {"2026-10-22T00:00:00Z": {"2026-10-23": {"tmax_c": 19.4, "at_local": "16:30", "peak_covered": True, "status": "complete"}}}}
    out = W.compute(sqlite3.connect(":memory:"), root=tmp_path, now=datetime(2026, 10, 22, 12, tzinfo=timezone.utc),
                    cache=Cache(), fcst_runs=runs)
    E = out["stations"]["ZUUU"]["days"]["2026-10-23"]
    assert E["fcst_history"] == [["2026-10-22T00:00:00Z", 19.4], ["2026-10-22T06:00:00Z", 20.0]]
    assert E["configs"]["fcst"]["change_c"] == 0.6 and E["configs"]["fcst"]["prev_tmax_c"] == 19.4
    assert runs["ZUUU"]["2026-10-22T06:00:00Z"]["2026-10-23"]["tmax_c"] == 20.0


def test_a_newer_run_that_misses_the_peak_does_not_replace_a_whole_day(tmp_path: Path, monkeypatch) -> None:
    """User 2026-10-25「wrf…未来三天都要」: the 10-24 12Z run starts at 20:00 local, so its
    10-24 is an evening reading; the 10-22 00Z run's whole-day 10-24 maximum stands."""

    for name in ("20261025_from_2026102200", "20261027_from_2026102412"):
        d = tmp_path / "run" / "fcst" / name
        d.mkdir(parents=True)
        (d / "wrfout_d01_x").write_bytes(b"")
    monkeypatch.setattr(W, "_zone", lambda conn, st: ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(W, "_truth", lambda conn, st: {})
    monkeypatch.setattr(W, "_domain_files", lambda d: ("d01", [str(d)]))
    monkeypatch.setattr(W, "_status", lambda d, now: "complete")

    class Cache:
        read = 0

        def frames(self, path, var, point):
            if var != "T2":
                return {}
            if path.endswith("2026102200"):   # 10-24 local 08..20 = 00..12Z, peak 21.0
                return {f"2026-10-24_{h:02d}:00:00": 15.0 + h / 2 for h in range(0, 13)}
            return {f"2026-10-24_{h:02d}:00:00": 18.0 for h in range(12, 16)}   # evening only

    out = W.compute(sqlite3.connect(":memory:"), root=tmp_path, now=datetime(2026, 10, 25, tzinfo=timezone.utc),
                    cache=Cache(), fcst_runs={})
    rec = out["stations"]["ZUUU"]["days"]["2026-10-24"]["configs"]["fcst"]
    assert rec["init_utc"] == "2026-10-22T00:00:00Z" and rec["tmax_c"] == 21.0


def test_rerun_on_newer_scans_leads_the_card_and_is_not_scored(tmp_path: Path, monkeypatch) -> None:
    """The sat arm re-run on every new scan (user 2026-10-25 「不应该只跑一次，有新数据就应该跑」):
    every issue is listed by ready time, the card leads with the newest, the frozen issue
    stays beside it and the forward score is the frozen issue's alone."""

    import json  # noqa: PLC0415

    from bayes_basket_target import board_readings  # noqa: PLC0415

    base = tmp_path / "run" / "nowcast"
    base.mkdir(parents=True)
    (base / "log.jsonl").write_text(json.dumps(
        {"day": "2026-10-25", "station": "ZUUU", "ready_utc": "2026-10-25T03:27:26Z", "last_sat_hour_utc": 2,
         "wrf_sat_tmax_with_obs_c": 25.12}) + "\n", encoding="utf-8")
    ups = [{"day": "2026-10-25", "station": "ZUUU", "kind": "update", "run": f"upd/{h:02d}",
            "ready_utc": f"2026-10-25T{h + 1:02d}:30:00Z", "last_sat_hour_utc": h, "wrf_sat_tmax_with_obs_c": v}
           for h, v in ((4, 24.1), (3, 24.6))]
    (base / W.NOWCAST_UPDATE_LOG).write_text("\n".join(json.dumps(r) for r in ups) + "\n", encoding="utf-8")
    issues = W.nowcast_issues(tmp_path)["2026-10-25"]
    assert [r["last_sat_hour_utc"] for r in issues] == [2, 3, 4]
    assert W.nowcast_records(tmp_path)["2026-10-25"]["last_sat_hour_utc"] == 2  # the frozen issue

    value = {"state": "ok", "stations": {"ZUUU": {"timezone": "Asia/Shanghai", "scores": {}, "nowcast_scores": {},
             "days": {"2026-10-25": {
                 "truth_c": None, "nowcast": W._nowcast_view(issues[0]),
                 "nowcast_updates": [W._nowcast_view(r) for r in issues[1:]],
                 "nowcast_update_run": {"tmax_c": 24.0, "series": [["t", 24.0]], "status": "complete", "run": "upd/04"},
                 "configs": {"nowcast_sat": {"tmax_c": 25.12, "series": [], "status": "complete"}}}}}}}
    monkeypatch.setattr(board_readings, "load_reading", lambda name: {"value": value})
    v = W.stored("ZUUU", "2026-10-25")
    assert v["nowcast"]["last_sat_hour_utc"] == 4 and v["nowcast"]["sat_obs_c"] == 24.1
    assert v["runs"]["nowcast_sat"]["tmax_c"] == 24.0 and v["nowcast_first"]["sat_obs_c"] == 25.12


def test_replay_segment_is_scored_on_its_own(tmp_path: Path, monkeypatch) -> None:
    """The strict as-of replay (user 2026-10-25 「单独成段，不计入前向天数」): its arms, the
    market and the belief scored apart from the forward test, and each arm paired against
    the sat arm on the same days."""

    import json  # noqa: PLC0415

    (tmp_path / "run" / "replay").mkdir(parents=True)
    recs = [{"day": d, "station": "ZUUU", "kind": "replay", "ready_utc": f"{d}T03:30:00Z",
             "wrf_sat_tmax_with_obs_c": s, "wrf_ctl_tmax_with_obs_c": c, "wrf_sat_aer_tmax_with_obs_c": a,
             "market": {"mean_c": m}, "belief": {"mean_c": m}}
            for d, s, c, a, m in (("2026-10-01", 26.0, 25.0, 25.5, 26.5), ("2026-10-02", 23.0, 24.0, 22.5, 22.0))]
    (tmp_path / "run" / W.REPLAY_LOG).write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    monkeypatch.setattr(W, "_zone", lambda conn, st: ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(W, "_truth", lambda conn, st: {"2026-10-01": 27.0, "2026-10-02": 22.0})
    out = W.compute(sqlite3.connect(":memory:"), root=tmp_path, now=datetime(2026, 10, 25, tzinfo=timezone.utc))
    S = out["stations"]["ZUUU"]
    assert S["replay_scores"]["sat_obs_c"] == {"n": 2, "bias_c": 0.0, "mae_c": 1.0, "hit": 0.0}
    assert S["replay_vs_sat"]["sat_aer_obs_c"] == {"n": 2, "mean_c": 0.0, "sd_c": 0.707, "t": 0.0}
    assert S["nowcast_scores"] == {} and S["days"]["2026-10-02"]["replay"]["ctl_obs_c"] == 24.0


def test_card_leads_with_the_newest_issue_early_frozen_or_rerun(monkeypatch) -> None:
    """Early issues before 11:10 (user 2026-10-26 「在 11:10 之前加两轮带卫星云的早版」): before the
    frozen issue the newest early one leads; once the frozen issue is out it leads until a
    later re-run."""

    from bayes_basket_target import board_readings  # noqa: PLC0415

    def card(frozen, updates):
        value = {"state": "ok", "stations": {"ZUUU": {"timezone": "Asia/Shanghai", "scores": {}, "days": {"2026-10-27": {
            "truth_c": None, "nowcast": frozen, "nowcast_updates": updates,
            "nowcast_update_run": {"tmax_c": 21.0, "series": [], "status": "complete", "run": "upd/x"},
            "configs": {"nowcast_sat": {"tmax_c": 22.0, "series": [], "status": "complete"}} if frozen else {}}}}}}
        monkeypatch.setattr(board_readings, "load_reading", lambda name: {"value": value})
        return W.stored("ZUUU", "2026-10-27")

    early = [{"ready_utc": "2026-10-27T01:45:00Z", "kind": "early"}, {"ready_utc": "2026-10-27T02:45:00Z", "kind": "early"}]
    frozen = {"ready_utc": "2026-10-27T03:27:00Z", "kind": "frozen"}
    assert card(None, early)["nowcast"]["ready_utc"] == "2026-10-27T02:45:00Z"
    v = card(frozen, early)
    assert v["nowcast"]["kind"] == "frozen" and v["runs"]["nowcast_sat"]["tmax_c"] == 22.0
    later = early + [{"ready_utc": "2026-10-27T04:30:00Z", "kind": "update"}]
    assert card(frozen, later)["nowcast"]["kind"] == "update"


def test_issue_view_names_the_newest_scan() -> None:
    """Half-hour re-runs (user 2026-10-26 「重跑改成每 30 分钟：用最新的整点和半点云图」): an issue says which
    scan it saw; a record from before carries its hour."""

    assert W._nowcast_view({"last_sat_hour_utc": 4, "last_scan_utc": "04:30"})["last_scan_utc"] == "04:30"
    assert W._nowcast_view({"last_sat_hour_utc": 2})["last_scan_utc"] == "02:00"
    assert W._nowcast_view({})["last_scan_utc"] is None


def test_two_driver_forecast_is_the_mean_of_its_members_daily_maxima(tmp_path: Path, monkeypatch) -> None:
    """User 2026-10-31 「驱动方案：上 IFS+GDPS 平均」: a run with ifs/ and gdps/ member runs stands
    as the mean of the members' daily maxima, and a day only one member reached is not shown."""

    d = tmp_path / "run" / "fcst" / "20261025_from_2026102200"
    for m in W.FCST_MEMBERS:
        (d / m).mkdir(parents=True)
        (d / m / "wrfout_d01_x").write_bytes(b"")
    assert {day for c, day, _ in W.run_dirs(tmp_path) if c == W.FORECAST} == {"20261022", "20261023", "20261024", "20261025"}
    monkeypatch.setattr(W, "_zone", lambda conn, st: ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(W, "_truth", lambda conn, st: {"2026-10-23": 20.0})
    monkeypatch.setattr(W, "_domain_files", lambda d: ("d01", [str(d)]))
    monkeypatch.setattr(W, "_status", lambda d, now: "complete")

    class Cache:
        read = 0

        def frames(self, path, var, point):
            if var != "T2":
                return {}
            # 10-23 local 08..20 = 00..12Z; IFS peaks 22.0 at 14 local, GDPS 18.0 at 16 local
            if path.endswith("ifs"):
                f = {f"2026-10-23_{h:02d}:00:00": 22.0 - abs(h - 6) for h in range(0, 13)}
                f["2026-10-24_02:00:00"] = 20.0   # IFS alone reaches 10-24
                return f
            return {f"2026-10-23_{h:02d}:00:00": 18.0 - abs(h - 8) / 2 for h in range(0, 13)}

    runs: dict = {}
    out = W.compute(sqlite3.connect(":memory:"), root=tmp_path, now=datetime(2026, 10, 25, tzinfo=timezone.utc),
                    cache=Cache(), fcst_runs=runs)
    days = out["stations"]["ZUUU"]["days"]
    rec = days["2026-10-23"]["configs"]["fcst"]
    assert rec["members"] == {"ifs": 22.0, "gdps": 18.0} and rec["tmax_c"] == 20.0
    assert rec["peak_covered"] and rec["status"] == "complete" and rec["error_c"] == 0.0
    assert "fcst" not in (days.get("2026-10-24") or {}).get("configs", {})
    assert runs["ZUUU"]["2026-10-22T00:00:00Z"]["2026-10-23"]["members"] == {"ifs": 22.0, "gdps": 18.0}


def test_a_frame_slot_left_as_nul_bytes_by_a_killed_chunk_ends_the_file(tmp_path: Path) -> None:
    """wrf.exe killed at its chunk alarm leaves the Times slot it was writing as NUL bytes
    (10-29..10-31 every board refresh raised on it); the frames before it still count."""

    import h5py
    import numpy as np

    path = tmp_path / "wrfout_d01_x"
    stamps = [b"2026-10-31_00:00:00", b"2026-10-31_01:00:00", b"\x00" * 19]
    with h5py.File(path, "w") as f:
        f["Times"] = np.array([np.frombuffer(s, dtype="S1") for s in stamps])
        f["XLAT"] = np.full((3, 2, 2), 30.6)
        f["XLONG"] = np.full((3, 2, 2), 103.9)
        f["T2"] = np.full((3, 2, 2), 293.15)
    frames = W._file_frames(str(path), "T2", (30.6, 103.9))
    assert frames == {"2026-10-31_00:00:00": 20.0, "2026-10-31_01:00:00": 20.0}


def test_a_frame_stamped_but_still_the_netcdf_fill_is_left_for_the_next_chunk(tmp_path: Path) -> None:
    """A chunk killed after writing a frame's Times but before its T2 leaves the netCDF fill
    (9.97e36); the 10-30 12Z GDPS member's 11-02 maximum read 5e36 and broke the board chart."""

    import h5py
    import numpy as np

    path = tmp_path / "wrfout_d01_x"
    with h5py.File(path, "w") as f:
        f["Times"] = np.array([np.frombuffer(s, dtype="S1") for s in (b"2026-11-02_03:00:00", b"2026-11-02_03:30:00")])
        f["XLAT"] = np.full((2, 2, 2), 30.6)
        f["XLONG"] = np.full((2, 2, 2), 103.9)
        f["T2"] = np.array([np.full((2, 2), 293.15), np.full((2, 2), 9.969209968386869e36)])
    assert W._file_frames(str(path), "T2", (30.6, 103.9)) == {"2026-11-02_03:00:00": 20.0}


def test_backfill_runs_are_their_own_series_and_leave_the_live_forecast_alone(tmp_path: Path, monkeypatch) -> None:
    """User 2026-10-31 「接回放+补 00/12Z」: two-driver re-runs of past cycles under run/fcst_backfill
    stand as ``fcst_bf`` with their own history; the live GFS run for the same init keeps ``fcst``
    and the persisted live history gains nothing."""

    live = tmp_path / "run" / "fcst" / "20261025_from_2026102200"
    live.mkdir(parents=True)
    (live / "wrfout_d01_x").write_bytes(b"")
    for init in ("2026102200", "2026102212"):
        for m in W.FCST_MEMBERS:
            md = tmp_path / "run" / "fcst_backfill" / f"20261025_from_{init}" / m
            md.mkdir(parents=True)
            (md / "wrfout_d01_x").write_bytes(b"")
    monkeypatch.setattr(W, "_zone", lambda conn, st: ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(W, "_truth", lambda conn, st: {"2026-10-23": 20.0})
    monkeypatch.setattr(W, "_domain_files", lambda d: ("d01", [str(d)]))
    monkeypatch.setattr(W, "_status", lambda d, now: "complete")

    class Cache:
        read = 0

        def frames(self, path, var, point):
            if var != "T2":
                return {}
            base = 17.0 if "/fcst/" in path else 21.0 if "2026102200/ifs" in path else 19.0 if "2026102200" in path else 22.0
            return {f"2026-10-23_{h:02d}:00:00": base - abs(h - 6) / 4 for h in range(0, 13)}

    runs: dict = {}
    out = W.compute(sqlite3.connect(":memory:"), root=tmp_path, now=datetime(2026, 10, 25, tzinfo=timezone.utc),
                    cache=Cache(), fcst_runs=runs)
    E = out["stations"]["ZUUU"]["days"]["2026-10-23"]
    assert E["configs"]["fcst"]["tmax_c"] == 17.0 and "members" not in E["configs"]["fcst"]
    bf = E["configs"]["fcst_bf"]
    assert bf["init_utc"] == "2026-10-22T12:00:00Z" and bf["tmax_c"] == 22.0 and bf["change_c"] == 2.0
    assert E["fcst_bf_history"] == [["2026-10-22T00:00:00Z", 20.0], ["2026-10-22T12:00:00Z", 22.0]]
    assert E["fcst_history"] == [["2026-10-22T00:00:00Z", 17.0]]
    assert list(runs["ZUUU"]) == ["2026-10-22T00:00:00Z"]
    assert out["stations"]["ZUUU"]["scores"]["fcst_bf"]["n"] == 1


def test_ifs_gdps_nowcast_is_its_own_series_scored_with_the_gfs_route_at_noon(tmp_path: Path, monkeypatch) -> None:
    """The IFS+GDPS route (user 2026-11-01 「另立一路」): every version on the day with its source
    cycle and members; scored on the newest version out by 04:00Z, beside the GFS route's newest
    (frozen issue or re-run) by the same moment -- later versions are shown, not scored."""

    import json  # noqa: PLC0415

    (tmp_path / "run" / "nowcast_ig").mkdir(parents=True)
    (tmp_path / "run" / "nowcast").mkdir(parents=True)
    ig = [{"day": "2026-11-01", "station": "ZUUU", "kind": "ifs_gdps", "ready_utc": r, "wrf_sat_tmax_with_obs_c": s,
           "source_cycle_utc": "2026-10-31T12:00:00Z", "wrf_sat_members": {"ifs": {"tmax_c": s + 1}, "gdps": {"tmax_c": s - 1}}}
          for r, s in (("2026-11-01T02:20:00Z", 21.0), ("2026-11-01T03:50:00Z", 22.5), ("2026-11-01T05:00:00Z", 30.0))]
    (tmp_path / "run" / W.NOWCAST_IG_LOG).write_text("\n".join(json.dumps(r) for r in ig) + "\n", encoding="utf-8")
    gfs = [{"day": "2026-11-01", "station": "ZUUU", "kind": k, "ready_utc": r, "wrf_sat_tmax_with_obs_c": s}
           for k, r, s in (("early", "2026-11-01T02:30:00Z", 20.0), ("update", "2026-11-01T04:30:00Z", 25.0))]
    (tmp_path / "run" / "nowcast" / W.NOWCAST_UPDATE_LOG).write_text("\n".join(json.dumps(r) for r in gfs) + "\n", encoding="utf-8")
    monkeypatch.setattr(W, "_zone", lambda conn, st: ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(W, "_truth", lambda conn, st: {"2026-11-01": 23.0})
    out = W.compute(sqlite3.connect(":memory:"), root=tmp_path, now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    S = out["stations"]["ZUUU"]
    E = S["days"]["2026-11-01"]
    assert [u["sat_obs_c"] for u in E["nowcast_ig_updates"]] == [21.0, 22.5, 30.0]
    assert E["nowcast_ig_updates"][0]["members_sat_c"] == {"ifs": 22.0, "gdps": 20.0}
    assert E["nowcast_ig_updates"][0]["source_cycle_utc"] == "2026-10-31T12:00:00Z"
    sc = S["nowcast_ig_scores"]
    assert sc["sat_obs_c"]["bias_c"] == -0.5 and sc["gfs_sat_obs_c"]["bias_c"] == -3.0
    assert sc["at_utc"] == "04:00" and sc["ig_vs_gfs"] == {"n": 1}
    assert S["nowcast_scores"] == {}  # the frozen GFS issue's own score is untouched


def test_today_chart_series_ig_mean_by_hour_and_reports_over_the_local_day(tmp_path: Path) -> None:
    """The today card's hourly chart (user 2026-11-01 「增加当天的曲线图」): the IFS+GDPS route's
    members averaged hour by hour where both have the hour, and the airport's reports over the
    local day, a re-sent report counted once."""

    rec = {"day": "2026-11-01", "wrf_sat_members": {"ifs": {"hourly": {"01": 18.0, "02": 20.0}},
                                                   "gdps": {"hourly": {"01": 17.0, "03": 21.0}}}}
    assert W._ig_series(rec) == [["2026-11-01T01:00:00Z", 17.5]]
    db = tmp_path / "archive.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE reported_trajectory_observations (station_id, valid_at, reported_temperature_c)")
    c.executemany("INSERT INTO reported_trajectory_observations VALUES (?,?,?)", [
        ("ZUUU", "2026-10-31T15:30:00.000Z", 15), ("ZUUU", "2026-10-31T16:00:00.000Z", 16),
        ("ZUUU", "2026-11-01T02:00:00.000Z", 18), ("ZUUU", "2026-11-01T02:00:00.000Z", 19),
        ("ZUUU", "2026-11-01T16:00:00.000Z", 14), ("KSEA", "2026-11-01T02:00:00.000Z", 9)])
    c.commit(); c.close()
    obs = W._obs_series("ZUUU", "2026-11-01", ZoneInfo("Asia/Shanghai"), db)
    assert obs == [["2026-10-31T16:00:00Z", 16.0], ["2026-11-01T02:00:00Z", 19.0]]
    assert W._obs_series("ZUUU", "2026-11-01", ZoneInfo("Asia/Shanghai"), tmp_path / "none.db") == []
