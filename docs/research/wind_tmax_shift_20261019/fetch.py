"""Forecast 10 m wind for the wind -> Tmax line (PROTOCOL.md): Open-Meteo
``wind_speed_10m`` (the latest forecast, the card's basis), hourly per station
over the fit window, cached; km/h as served (fit.py converts).  Research
quota lane (as fetch_low_d1.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fetch_low_d1", HERE.parent / "cloud_tmax_shift_20261017" / "fetch_low_d1.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
F.CACHE = HERE / "cache"
F.FIELD = "wind_speed_10m"

if __name__ == "__main__":
    F.main()
