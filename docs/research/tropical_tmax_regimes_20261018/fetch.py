"""IEM METAR/SPECI history for the tropical stations (PROTOCOL.md 数据).

One request per station-year, size-bounded, kept gzipped beside this file;
a year already on disk is not fetched again.

    uv run python docs/research/tropical_tmax_regimes_20261018/fetch.py
"""

from __future__ import annotations

import gzip
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).parent
STATIONS = {"WSSS": "WSSS", "WMKK": "WMKK"}
YEARS = range(2022, 2027)
MAX_BYTES = 40 * 1024 * 1024
URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def fetch(station: str, year: int) -> bytes:
    end = f"{year + 1}-01-01T00:00Z" if year < 2026 else "2026-10-18T00:00Z"
    q = urllib.parse.urlencode([
        ("station", station), ("sts", f"{year}-01-01T00:00Z"), ("ets", end),
        ("data", "metar"), ("data", "tmpf"), ("data", "dwpf"), ("data", "drct"), ("data", "sknt"),
        ("data", "skyc1"), ("data", "skyl1"), ("data", "skyc2"), ("data", "skyl2"), ("data", "wxcodes"),
        ("data", "p01i"), ("tz", "Etc/UTC"), ("format", "onlycomma"), ("missing", "M"),
        ("report_type", "3"), ("report_type", "4"),
    ])
    import httpx  # noqa: PLC0415 -- the resident's HTTP client, proxy-aware

    for attempt in range(5):
        try:
            body = bytearray()
            with httpx.Client(timeout=180, headers={"User-Agent": "weather-research-tropical/1"}) as client:
                with client.stream("GET", f"{URL}?{q}") as r:
                    r.raise_for_status()
                    for chunk in r.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            break
            body = bytes(body)
            if len(body) > MAX_BYTES:
                raise ValueError("response too large")
            return body
        except Exception as exc:  # noqa: BLE001
            print(f"  {station} {year} attempt {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"{station} {year} failed")


def main() -> int:
    for station, iem in STATIONS.items():
        for year in YEARS:
            out = HERE / f"iem_{station}_{year}.csv.gz"
            if out.exists():
                continue
            body = fetch(iem, year)
            out.write_bytes(gzip.compress(body))
            print(f"{station} {year}: {body.count(b'\n') - 1} rows, {len(body) // 1024} KB")
            time.sleep(3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
