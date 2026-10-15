"""JAXA GSMaP_NOW satellite rain on the station board (user 2026-10-15
「云雨数据呢」 → 「可以」): grid cells, one synthetic file end to end, the hour
a local stamp reads, and the rain chart's satellite row."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from bayes_basket_target import jaxa_gsmap as gsmap
from bayes_basket_target import station_day_board as board

ROOT = Path(__file__).resolve().parents[1]


def test_cells_on_the_global_south_first_grid() -> None:
    r, c = gsmap.cell(30.576, 103.95)
    assert abs(-89.95 + 0.1 * r - 30.576) <= 0.05 and abs(-179.95 + 0.1 * c - 103.95) <= 0.05
    assert gsmap.cell(40.78, -73.88) is not None            # LaGuardia
    assert gsmap.cell(60.32, 24.96) is None                  # Helsinki: north of the filled band


def test_one_slot_is_read_for_every_station(tmp_path, monkeypatch) -> None:
    import h5py

    monkeypatch.setattr(gsmap, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(gsmap, "_account", lambda: ("u", "p"))
    r, c = gsmap.cell(1.368, 103.982)                        # Singapore

    class FakeFTP:
        def __init__(self, *a, **k):
            pass

        def login(self, *a):
            return "230"

        def retrbinary(self, cmd, sink):
            path = str(tmp_path / "src.nc")
            with h5py.File(path, "w") as f:
                for name in ("hourlyPrecipRate", "hourlyPrecipRateGC"):
                    data = np.zeros((1, gsmap.ROWS, gsmap.COLS), dtype=np.float32)
                    data[0, r - 1:r + 2, c - 1:c + 2] = 3.0
                    data[0, r, c] = 6.0 if name == "hourlyPrecipRate" else 5.0
                    d = f.create_dataset(name, data=data, compression="gzip")
                    d.attrs["missing_value"] = np.array([-9999.9], dtype=np.float32)
                f.create_dataset("Latitude", data=(-89.95 + 0.1 * np.arange(gsmap.ROWS)).astype(np.float32))
                f.create_dataset("Longitude", data=(-179.95 + 0.1 * np.arange(gsmap.COLS)).astype(np.float32))
            sink(Path(path).read_bytes())

        def quit(self):
            pass

    monkeypatch.setattr(gsmap.ftplib, "FTP", FakeFTP)
    got = gsmap.read_slot(datetime(2026, 10, 15, 4, tzinfo=timezone.utc), {"WSSS": (1.368, 103.982), "EFHK": (60.3, 25.0)})
    assert got == {"WSSS": {"mm": 6.0, "mm_area": round((6 + 8 * 3) / 9, 2), "mm_gc": 5.0,
                            "mm_gc_area": round((5 + 8 * 3) / 9, 2)}}
    assert not any((tmp_path / "jaxa_gsmap_tmp").iterdir())


def test_a_local_stamp_reads_the_hour_that_ends_at_it(monkeypatch) -> None:
    asked: list[datetime] = []

    def fake_rain(station, starts):
        asked.extend(starts)
        return {gsmap.slot_key(s): {"mm": 1.0 if s.hour == 5 else 0.0} for s in starts}

    monkeypatch.setattr(gsmap, "rain", fake_rain)
    monkeypatch.setattr(gsmap, "status", lambda: {"last_read_at_utc": "2026-10-14T20:00:00Z"})
    out = board._precip_sat("ZUUU", "2026-10-14", ZoneInfo("Asia/Shanghai"))
    # Stamp 14 (13:00-14:00 local) is the 05:00 UTC file.
    assert out["hours"]["14"]["mm"] == 1.0 and out["sum_mm"] == 1.0
    assert min(asked) == datetime(2026, 10, 13, 15, tzinfo=timezone.utc)   # stamp 00: 23:00-24:00 the day before
    assert board._precip_sat("EFHK", "2026-10-14", ZoneInfo("Europe/Helsinki")) is None


def test_the_rain_chart_draws_the_satellite_row() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    start = source.index("function sdRainSvg(P, O, S){")
    depth = 0
    for i in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        if depth == 0:
            fn = source[start:i + 1]
            break
    P = {"hourly_mm": {f"{h:02d}": 0.0 for h in range(24)}}
    S = {"hours": {"14": {"mm": 2.5, "mm_area": 1.2, "mm_gc": 2.0}, "13": {"mm": 0.0, "mm_area": 0.0, "mm_gc": 0.0}}}
    script = ('const sdEsc = s => String(s == null ? "" : s);\n'
              'const sdN = (v, d) => v == null || !isFinite(v) ? "—" : Number(v).toFixed(d == null ? 2 : d);\n'
              + fn + f"\nconsole.log(sdRainSvg({json.dumps(P)}, null, {json.dumps(S)}));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "14:00 卫星雨量（GSMaP_NOW）2.50 mm" in out.stdout and 'fill="#2f9e44"' in out.stdout and ">卫星<" in out.stdout
    assert "${sdRainSvg(P, O, day.precip_sat)}" in source and "卫星 今日已降" in source
