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
