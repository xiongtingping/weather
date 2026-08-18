"""Cache native-ensemble training cases per station for the affine-form study.

Read-only against the SSOT.  Writes a pickle of
{(station, system, season, lead, cycle): [(event_date, values, actual), ...]}
so the form-choice analyses can re-run without touching the 14GB database.
"""

from __future__ import annotations

import pickle
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/Users/xiong/weather")

from bayes_basket_target import native_ensemble_oos as oos

DB = "/Users/xiong/weather/data/bayes_basket_target_ssot.db"
OUT = "/Users/xiong/weather/tmp/affine_cases.pkl"


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA query_only=ON")
    stations = [
        str(row[0])
        for row in conn.execute(
            "SELECT station_id FROM calibration_station_registry "
            "ORDER BY station_id"
        ).fetchall()
    ]
    as_of = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    as_of = as_of.replace("+00:00", "Z")
    print(f"{len(stations)} stations, as_of={as_of}", flush=True)

    out: dict[tuple[str, str, str, str, str], list] = {}
    for index, station in enumerate(stations, start=1):
        start = time.time()
        try:
            cases = oos._load_cases(conn, station_id=station, as_of_utc=as_of)
        except Exception as error:  # noqa: BLE001 - study script
            print(f"[{index}/{len(stations)}] {station} SKIP {error}", flush=True)
            continue
        kept = 0
        for (system_id, season, lead, cycle), rows in cases.items():
            trimmed = [
                (
                    str(row["event_date"]),
                    [float(value) for value in row["values"]],
                    float(row["actual_tmax_c"]),
                )
                for row in rows
            ]
            if len(trimmed) >= 12:
                out[(station, system_id, season, lead, cycle)] = trimmed
                kept += 1
        print(
            f"[{index}/{len(stations)}] {station} "
            f"scopes={len(cases)} kept={kept} {time.time() - start:.1f}s",
            flush=True,
        )

    with open(OUT, "wb") as handle:
        pickle.dump(out, handle)
    print(f"wrote {len(out)} scopes -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
