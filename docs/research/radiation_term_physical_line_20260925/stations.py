"""The 45 trading stations: coordinates, settlement timezone and unit.

Read from the same two places production reads them -- the AWC stationinfo
projection (``bayes_basket_target.station_registry``) and the settlement
contract in ``config/trading/awc_truth_ingress_v1.json`` -- so the study
cannot drift onto a station, zone or unit production does not use.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

CONFIG = ROOT / "config" / "trading" / "awc_truth_ingress_v1.json"


def station_table() -> dict[str, dict]:
    config = json.loads(CONFIG.read_text())
    zones = config["settlement_timezones"]
    table: dict[str, dict] = {}
    for city, row in config["settlement_source_contract"]["cities"].items():
        station = str(row["station_id"]).upper()
        coords = station_coordinates(station)
        table[station] = {
            "station_id": station,
            "city": city,
            "timezone": zones[city],
            "unit": str(row["unit"]).upper(),
            "latitude": float(coords["latitude"]),
            "longitude": float(coords["longitude"]),
        }
    if len(table) != 45:
        raise SystemExit(f"expected 45 stations, got {len(table)}")
    return dict(sorted(table.items()))


if __name__ == "__main__":
    for row in station_table().values():
        print(row)
