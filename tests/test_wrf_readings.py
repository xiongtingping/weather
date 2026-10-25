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
