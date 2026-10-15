"""JAXA's own Himawari shortwave on the station board (user 2026-10-15
「接入：http://hokusai.eorc.jaxa.jp/」): grid cells, the hour a local stamp
reads, the background reader's cache, and one synthetic file read end to end."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from bayes_basket_target import jaxa_ptree_radiation as jaxa
from bayes_basket_target import station_day_board as board


def test_grid_cells_and_coverage() -> None:
    assert jaxa.pixel(30.576, 103.95) == (588, 679)         # ZUUU: the 30.60N cell
    assert jaxa.covers(-41.33, 174.8) and jaxa.covers(21.0, -158.0)   # Wellington, past 180
    assert not jaxa.covers(51.5, 0.05) and not jaxa.covers(33.9, -118.4)   # London, Los Angeles
    assert jaxa.pixel(61.0, 120.0) is None


def _h5(path, rows=jaxa.ROWS, cols=jaxa.COLS, values=None) -> None:
    import h5py

    with h5py.File(path, "w") as f:
        swr = np.full((rows, cols), -32768, dtype=np.int16)
        for (r, c), v in (values or {}).items():
            swr[r, c] = v
        d = f.create_dataset("SWR", data=swr)
        d.attrs["scale_factor"] = np.array([0.05], dtype=np.float32)
        d.attrs["missing_value"] = np.array([-32768], dtype=np.int16)
        f.create_dataset("latitude", data=(60.0 - 0.05 * np.arange(rows)).astype(np.float32))
        f.create_dataset("longitude", data=(70.0 + 0.05 * np.arange(cols)).astype(np.float32))


def test_one_hour_is_read_for_every_station_and_the_file_is_deleted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jaxa, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(jaxa, "_remote_file", lambda hour: "/pub/himawari/L3/PAR/021/202610/14/x.nc")
    written: list[str] = []

    class Done:
        returncode, stderr = 0, b""

    def fake_curl(*args, timeout=60):
        path = args[args.index("-o") + 1]
        _h5(path, values={jaxa.pixel(30.576, 103.95): 8254})   # ZUUU 412.7 W/m2
        written.append(path)
        return Done()

    monkeypatch.setattr(jaxa, "_curl", fake_curl)
    got = jaxa.read_hour(datetime(2026, 10, 14, 5, tzinfo=timezone.utc),
                         {"ZUUU": (30.576, 103.95), "RJTT": (35.553, 139.781), "EGLC": (51.5, 0.05)})
    assert got == {"ZUUU": 412.7, "RJTT": None}          # missing value is None; outside the disk is left out
    assert written and not any((tmp_path / "jaxa_ptree_tmp").iterdir())


def test_the_reader_serves_from_its_cache_and_queues_the_rest(tmp_path, monkeypatch) -> None:
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"schema": jaxa.CACHE_SCHEMA, "hours": {"2026-10-14T05:00Z": {"ZUUU": 412.7}}}))
    monkeypatch.setattr(jaxa, "CACHE_PATH", path)
    monkeypatch.setattr(jaxa, "_CACHE", None)
    monkeypatch.setattr(jaxa, "_PENDING", set())
    monkeypatch.setattr(jaxa, "_MISSES", {})
    monkeypatch.setattr(jaxa, "_fleet", lambda: {"ZUUU": (30.576, 103.95)})
    monkeypatch.setattr(jaxa, "read_hour", lambda hour, stations: {"ZUUU": 500.0})
    hours = [datetime(2026, 10, 14, 5, tzinfo=timezone.utc), datetime(2026, 10, 14, 6, tzinfo=timezone.utc)]
    assert jaxa.measured("ZUUU", hours) == {"2026-10-14T05:00Z": 412.7}
    for _ in range(50):
        if "2026-10-14T06:00Z" in json.loads(path.read_text())["hours"]:
            break
        time.sleep(0.05)
    assert jaxa.measured("ZUUU", hours) == {"2026-10-14T05:00Z": 412.7, "2026-10-14T06:00Z": 500.0}


def test_a_local_hour_reads_the_utc_hour_that_holds_its_middle(monkeypatch) -> None:
    asked: list[list[datetime]] = []

    def fake_measured(station, hours):
        asked.append(hours)
        return {h.strftime("%Y-%m-%dT%H:00Z"): 100.0 + h.hour for h in hours}

    monkeypatch.setattr(jaxa, "measured", fake_measured)
    monkeypatch.setattr(jaxa, "status", lambda: {"last_read_at_utc": "2026-10-14T20:00:00Z"})
    # Never the live reader from a test (it would download real scans).
    monkeypatch.setattr(jaxa, "clouds", lambda station, scans: {
        s.strftime("%Y-%m-%dT%H:%MZ"): {"state": "ok", "cover": 0.5, "cells": 25, "type": 8} for s in scans})
    monkeypatch.setattr(jaxa, "latest", lambda station: None)
    out = board._jaxa_hours("ZUUU", 30.576, 103.95, "2026-10-14", ZoneInfo("Asia/Shanghai"))
    # Local 14:00 (the hour 13:00-14:00) is 05:00-06:00 UTC.
    assert out["values"][14] == 105.0
    assert out["values"][2] == 0.0                     # night
    assert out["pending_hours"] == 0 and out["fetched_at_utc"] == "2026-10-14T20:00:00Z"
    # The cloud for local 14:00 is the 05:30 UTC scan, named for the panel.
    assert out["clouds"][14]["scan_utc"] == "2026-10-14T05:30Z" and out["clouds"][14]["type_name"] == "层积云"
    assert out["clouds"][2] is None
    assert board._satellite_product(103.95) == "jaxa_ptree_himawari"
    assert board.SATELLITE_VIA["jaxa_ptree_himawari"].startswith("JAXA P-Tree 官方")


def test_the_account_stays_out_of_the_tree() -> None:
    source = (jaxa.ROOT / "bayes_basket_target" / "jaxa_ptree_radiation.py").read_text(encoding="utf-8")
    assert '"--netrc"' in source and "password" not in source.lower().replace("~/.netrc", "")
    assert jaxa.CA_FILE.exists()


def test_the_newest_l2_scan_is_found_in_this_or_the_last_hour_folder(monkeypatch) -> None:
    folders = {
        "/pub/himawari/L2/PAR": ["001", "021"],
        "/pub/himawari/L2/PAR/021/202610/15/00": ["H09_20261015_0030_RFL021_FLDK.02801_02401.nc",
                                                  "H09_20261015_0040_RFL021_FLDK.02801_02401.nc",
                                                  "H09_20261015_0040_rFL021_FLDK.02701_02601.nc"],
    }

    def fake_listing(directory):
        if directory not in folders:
            raise RuntimeError("no folder")
        return folders[directory]

    monkeypatch.setattr(jaxa, "_listing", fake_listing)
    scan, remote = jaxa._latest_l2(datetime(2026, 10, 15, 1, 5, tzinfo=timezone.utc))
    assert scan == datetime(2026, 10, 15, 0, 40, tzinfo=timezone.utc)
    assert remote.endswith("/00/H09_20261015_0040_RFL021_FLDK.02801_02401.nc")   # the 5 km file


def test_latest_serves_the_read_scan_and_asks_for_a_newer_one(tmp_path, monkeypatch) -> None:
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"schema": jaxa.CACHE_SCHEMA, "hours": {}, "l2_latest": {
        "scan_utc": "2026-10-15T00:40Z", "file": "/a.nc", "read_at_utc": "2026-10-15T01:05:00Z",
        "values": {"ZUUU": 269.7}}}))
    monkeypatch.setattr(jaxa, "CACHE_PATH", path)
    monkeypatch.setattr(jaxa, "_CACHE", None)
    monkeypatch.setattr(jaxa, "_L2_STATE", {"pending": False, "checked": -1e9})
    monkeypatch.setattr(jaxa, "_latest_l2", lambda now: (datetime(2026, 10, 15, 0, 50, tzinfo=timezone.utc), "/b.nc"))
    monkeypatch.setattr(jaxa, "read_file", lambda remote, stations: {"ZUUU": 301.0})
    monkeypatch.setattr(jaxa, "_fleet", lambda: {"ZUUU": (30.576, 103.95)})
    assert jaxa.latest("ZUUU") == {"scan_utc": "2026-10-15T00:40Z", "sw_wm2": 269.7, "read_at_utc": "2026-10-15T01:05:00Z"}
    for _ in range(50):
        if (jaxa._L2_STATE.get("latest") or {}).get("file") == "/b.nc":
            break
        time.sleep(0.05)
    assert jaxa.latest("ZUUU")["sw_wm2"] == 301.0 and jaxa.latest("RJTT") is None
    assert json.loads(path.read_text())["l2_latest"]["scan_utc"] == "2026-10-15T00:50Z"


def test_the_panel_shows_the_scan() -> None:
    source = (jaxa.ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "scanHour === Number(r.hour) ? scanCell" in source and "const L = sat.latest;" in source


def test_a_cloud_scan_is_read_as_cover_type_and_top(tmp_path, monkeypatch) -> None:
    import h5py

    monkeypatch.setattr(jaxa, "CACHE_PATH", tmp_path / "cache.json")
    r, c = jaxa.clp_cell(30.576, 103.95)

    def fake_download(remote):
        path = str(tmp_path / "c.nc")
        n = jaxa.CLP_SIZE
        with h5py.File(path, "w") as f:
            qa = np.zeros((n, n), dtype=np.uint16)
            # retrieval 101 (success, high), mask 11 (cloudy) on 20 cells; 010 clear, mask 00 on 5
            box = qa[r - 2:r + 3, c - 2:c + 3]
            box[:] = 0b11_101
            box[0, :] = 0b00_010
            f.create_dataset("QA", data=qa)
            t = np.full((n, n), 255, dtype=np.int16)
            t[r - 2:r + 3, c - 2:c + 3] = 8
            f.create_dataset("CLTYPE", data=t)
            for name, raw, scale in (("CLTH", 2100, 0.001), ("CLOT", 750, 0.01)):
                d = f.create_dataset(name, data=np.full((n, n), raw, dtype=np.int16))
                d.attrs["scale_factor"] = np.array([scale], dtype=np.float32)
                d.attrs["missing_value"] = np.array([-32768], dtype=np.int16)
            f.create_dataset("latitude", data=(60.0 - 0.05 * np.arange(n)).astype(np.float32))
            f.create_dataset("longitude", data=(80.0 + 0.05 * np.arange(n)).astype(np.float32))
        return path

    monkeypatch.setattr(jaxa, "_download", fake_download)
    got = jaxa.read_clp_file("/x.nc", {"ZUUU": (30.576, 103.95), "EGLC": (51.5, 0.05)})
    assert got == {"ZUUU": {"state": "ok", "cover": 0.8, "cells": 25, "type": 8, "top_km": 2.1,
                            "optical_thickness": 7.5}}
    assert not (tmp_path / "c.nc").exists()


def test_the_cloud_cell_and_its_column() -> None:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    source = (jaxa.ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    sky = source[source.index("function sdSky(day){"):]
    assert sky.index("const satCloud") < sky.index("const rows = ")   # declared before the rows use it
    start = source.index("function sdSatCloudCell(r){")
    end = source.index("function sdRadTotal(R){")
    script = ('const sdEsc = s => String(s == null ? "" : s);\n'
              'const sdN = (v, d) => v == null || !isFinite(v) ? "—" : Number(v).toFixed(d == null ? 2 : d);\n'
              'function sdLocal(t){ return String(t).slice(5, 16); }\n' + source[start:end]
              + 'console.log(sdSatCloudCell({sat_cloud: {state: "ok", cover: 0.8, cells: 25, type_name: "层积云", '
                'top_km: 2.1, optical_thickness: 7.5, scan_utc: "2026-10-14T05:30Z"}}).replace(/<[^>]+>/g, "|"));')
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "80%| |层积云" in out.stdout
