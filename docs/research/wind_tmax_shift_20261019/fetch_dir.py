"""Forecast 10 m wind direction for the polar component (PROTOCOL.md 追加):
Open-Meteo ``wind_direction_10m`` (the latest forecast), hourly per station
over the fit window, cached beside the speeds.  Research quota lane."""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fetch_low_d1", HERE.parent / "cloud_tmax_shift_20261017" / "fetch_low_d1.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
F.CACHE = HERE / "cache_dir"
F.FIELD = "wind_direction_10m"

if __name__ == "__main__":
    F.main()
