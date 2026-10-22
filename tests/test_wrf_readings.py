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


def test_live_forecast_dirs_keep_the_newest_init_per_target(tmp_path: Path) -> None:
    for name in ("20261022_from_2026102112", "20261022_from_2026102118", "20261023_from_2026102200"):
        d = tmp_path / "run" / "fcst" / name
        d.mkdir(parents=True)
        (d / "wrfout_d01_x").write_bytes(b"")
    runs = {(c, day): d.name for c, day, d in W.run_dirs(tmp_path)}
    assert runs == {(W.FORECAST, "20261022"): "20261022_from_2026102118",
                    (W.FORECAST, "20261023"): "20261023_from_2026102200"}
