"""Forecast relative humidity for the humidity -> Tmax line (PROTOCOL.md):
Open-Meteo ``relative_humidity_2m`` (the latest forecast, the card's basis),
hourly per station over the fit window, cached.  Research quota lane."""
from __future__ import annotations

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fetch_low_d1", HERE.parent / "cloud_tmax_shift_20261017" / "fetch_low_d1.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
F.CACHE = HERE / "cache"
F.FIELD = "relative_humidity_2m"

if __name__ == "__main__":
    F.main()
