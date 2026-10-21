"""Forecast dew point for the dew -> Tmax line (PROTOCOL.md): Open-Meteo
``dew_point_2m`` (the latest forecast, the card's basis) and its day-ahead run,
hourly, per station over the fit window, cached per station.  Charged to the
research lane of the production quota ledger (as fetch_low_d1.py)."""
from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fetch_low_d1", HERE.parent / "cloud_tmax_shift_20261017" / "fetch_low_d1.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
F.CACHE = HERE / "cache"
F.FIELD = "dew_point_2m,dew_point_2m_previous_day1"

if __name__ == "__main__":
    F.main()
