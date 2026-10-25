"""Aerosol on the station board (user 2026-10-25「增加气溶胶的数据」): CAMS
forecast optical depth against the satellite's measured one -- JAXA Himawari
over Asia/Oceania, GOES over the Americas, forecast only elsewhere."""

from __future__ import annotations

import io
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from bayes_basket_target import goes_surface_radiation as goes
from bayes_basket_target import jaxa_ptree_radiation as jaxa
from bayes_basket_target import open_meteo_quota as Q
from bayes_basket_target import station_day_board as board

ROOT = Path(__file__).resolve().parents[1]


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)


def test_asia_reads_himawari_the_americas_goes_and_europe_nothing(monkeypatch) -> None:
    monkeypatch.setattr(board, "datetime", Clock)
    asked = {}
    monkeypatch.setattr(jaxa, "aerosol", lambda st, hours: asked.setdefault("jaxa", hours) and
                        {datetime(2026, 10, 25, 3, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:00Z"): {"aod": 0.42}})
    out = board._satellite_aerosol("ZUUU", 30.578, 103.947, "2026-10-25", ZoneInfo("Asia/Shanghai"))
    assert out["covered"] and out["values"][11] == 0.42          # 11:00 local = 03Z
    assert max(asked["jaxa"]) <= datetime(2026, 10, 25, 7, 25, tzinfo=timezone.utc)   # published ~95 min on

    monkeypatch.setattr(goes, "measured", lambda st, lat, lon, inst, kind="dsr": asked.setdefault("goes", kind) and {})
    out = board._satellite_aerosol("KATL", 33.64, -84.43, "2026-10-25", ZoneInfo("America/New_York"))
    assert out["covered"] and asked["goes"] == "aod" and out["label"] == goes.AOD_LABEL

    out = board._satellite_aerosol("LEMD", 40.47, -3.56, "2026-10-25", ZoneInfo("Europe/Madrid"))
    assert out["covered"] is False and out["values"] == [None] * 24


def test_the_forecast_is_charged_to_its_own_lane_and_kept(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Q, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(board, "_AEROSOL_CACHE", {})
    monkeypatch.setattr(board, "_DAY_REFUSED_UNTIL", [])
    calls = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _open(req, **_k):
        calls.append(req.full_url)
        return _Resp(json.dumps({"hourly": {"time": ["t"] * 24, "aerosol_optical_depth": [0.2] * 24,
                                            "dust": [3.0] * 24}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    a = board._open_meteo_aerosol(30.58, 103.95, "Asia/Shanghai", "2026-10-25")
    b = board._open_meteo_aerosol(30.58, 103.95, "Asia/Shanghai", "2026-10-25")
    assert a["aod"][12] == 0.2 and b is a and len(calls) == 1 and "air-quality-api" in calls[0]
    assert Q.spent(board.AEROSOL_QUOTA_LANE, path=tmp_path / "ledger.json") == 1


def test_the_himawari_file_reads_pure_then_merged(tmp_path, monkeypatch) -> None:
    h5py = pytest.importorskip("h5py")
    n = jaxa.CLP_SIZE
    path = tmp_path / "arp.nc"
    pure = np.full((n, n), -32768, dtype=np.int16)
    merged = np.full((n, n), -32768, dtype=np.int16)
    r, c = jaxa.clp_cell(30.578, 103.947)
    pure[r, c] = 1500                       # 0.3 at 0.0002 a count
    r2, c2 = jaxa.clp_cell(40.08, 116.58)
    merged[r2 - 1:r2 + 2, c2] = 2500        # 0.5, no pure cell there
    with h5py.File(path, "w") as f:
        f["latitude"] = np.float32(60.0 - 0.05 * np.arange(n))
        f["longitude"] = np.float32(80.0 + 0.05 * np.arange(n))
        for name, data in (("AOT_Pure", pure), ("AOT_Merged", merged)):
            f[name] = data
            f[name].attrs["scale_factor"] = np.float32([0.0002])
            f[name].attrs["add_offset"] = np.float32([0.0])
            f[name].attrs["missing_value"] = np.int16([-32768])
    monkeypatch.setattr(jaxa, "_download", lambda remote: str(path))
    monkeypatch.setattr("os.unlink", lambda p: None)
    out = jaxa.read_arp_file("x", {"ZUUU": (30.578, 103.947), "ZBAA": (40.08, 116.58), "RJTT": (35.55, 139.78)})
    assert out["ZUUU"] == {"aod": 0.3, "kind": "pure", "cells": 1}
    assert out["ZBAA"] == {"aod": 0.5, "kind": "merged", "cells": 3}
    assert out["RJTT"]["aod"] is None


def test_goes_reads_aod_as_a_kind() -> None:
    assert goes._READERS["aod"] is goes.read_aod and goes.AOD_PRODUCT == "ABI-L2-AODF"


def test_the_board_card() -> None:
    dash = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert 'sdSunCard("aerosol", "气溶胶 · 实况对比预报"' in dash and 'sdSunCard("aerosol", "气溶胶 · 预报"' in dash
    assert "toned(aerosolCard, toneOf([], aeD, false, 0.1))" in dash


def test_the_mos_recompute_reads_no_skin_or_aerosol(monkeypatch) -> None:
    """estimate_hit walks closed days through the same panel; the extras stay off."""

    src = (ROOT / "bayes_basket_target" / "estimate_hit_readings.py").read_text(encoding="utf-8")
    assert "B._hourly_vs_forecast(conn, station, day, zone, metar, extras=False)" in src
    for name in ("_goes_skin_hours", "_open_meteo_aerosol", "_satellite_aerosol", "_skin_normal"):
        monkeypatch.setattr(board, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(name)))
    monkeypatch.setattr(board, "_station_lat_lon", lambda conn, st: (30.578, 103.947))
    monkeypatch.setattr(board, "_open_meteo_hourly", lambda *a: {"available": False, "hourly": {}})
    monkeypatch.setattr(board, "_satellite_product", lambda lon: None)
    monkeypatch.setattr(board, "_goes_hours", lambda *a: {"available": False, "values": [None] * 24})
    out = board._hourly_vs_forecast(None, "ZUUU", "2026-10-20", ZoneInfo("Asia/Shanghai"), [], extras=False)
    assert out["aerosol"]["reason"] == "not_read"
