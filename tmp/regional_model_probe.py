"""Phase 0: does a regional model beat the incumbents where the book bleeds?

Offline and read-only.  Nothing is written to the SSOT here -- the plan's own
kill line comes first, because landing a source that does not help is the
mistake this session has paid for twice already.

Every station carrying at least one failure flag is matched against whichever
high-resolution regional models cover its coordinates, sixty-plus days of
lead-separated archive are pulled, and the residual against settled truth is
compared with what the incumbent systems achieve *after* their own bias
correction.  The comparison is on dispersion about the mean, not on raw bias:
a constant offset is what ``bias_c`` exists to remove and every system gets
that for free, so a source that only wins on offset has added nothing.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from urllib.parse import quote
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
OUT = ROOT / "tmp" / "regional_model_probe.json"

# Regional models and the boxes they actually cover, west/east/south/north.
# Boxes are the published domains, deliberately trimmed inward at the edges:
# a station on a domain boundary gets the model's worst cells.
DOMAINS = [
    ("jma_msm", "JMA MSM 5km", 120.0, 150.0, 22.0, 47.0),
    ("meteofrance_arome_france_hd", "AROME-HD 1.3km",
     -5.0, 9.5, 41.0, 51.0),
    ("dwd_icon_d2", "ICON-D2 2km", 0.5, 17.0, 44.0, 57.0),
    ("knmi_harmonie_arome_netherlands", "HARMONIE NL 2km",
     -1.0, 11.0, 48.0, 56.0),
    ("ukmo_uk_deterministic_2km", "UKMO UK 2km", -11.0, 3.0, 48.0, 60.0),
    ("italia_meteo_arpae_icon_2i", "ICON-2I 2km", 5.0, 20.0, 35.0, 48.0),
    ("meteoswiss_icon_ch1", "ICON-CH1 1km", 5.0, 11.5, 44.5, 49.0),
    ("ncep_hrrr_conus", "HRRR 3km", -125.0, -67.0, 25.0, 50.0),
    ("cma_grapes_global", "GRAPES 15km", 70.0, 140.0, 5.0, 55.0),
]
ARCHIVE = "https://historical-forecast-api.open-meteo.com/v1/forecast"
DAYS_BACK = 75
PAUSE = 1.2


def _fetch(url: str, attempts: int = 3) -> dict | None:
    """Fetch through curl.

    ``urllib`` on this machine cannot verify the certificate chain -- the
    interpreter's trust store does not see the system roots -- while curl,
    which reads the same roots the rest of the box does, connects fine.
    Rather than disable verification to make a library work, use the client
    that already verifies correctly.
    """

    import subprocess

    for attempt in range(attempts):
        result = subprocess.run(
            ["curl", "-s", "--max-time", "90", url],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                return json.loads(result.stdout)
            except ValueError as exc:
                if attempt == attempts - 1:
                    print(f"      bad json: {str(exc)[:60]}")
                    return None
        elif attempt == attempts - 1:
            print(f"      curl rc={result.returncode}"
                  f" {result.stderr[:60]}")
            return None
        time.sleep(2.0 * (attempt + 1))
    return None


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120.0)

    import selection_market_dashboard as dash
    payload = dash.build_payload(DB)
    matrix = payload["station_failure_matrix"]
    candidates = [
        row for row in matrix["stations"] if row["flag_count"] >= 1
    ]
    print(f"带失败旗标的站: {len(candidates)}")

    coords = {
        str(row[0]): (float(row[1]), float(row[2]))
        for row in conn.execute(
            "SELECT DISTINCT station_id, latitude, longitude"
            " FROM calibration_station_forecast_physics"
        )
    }
    zones = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            "SELECT station_id, timezone_name"
            " FROM calibration_station_registry"
        )
    }
    truth: dict[tuple[str, str], float] = {}
    for station, day, value in conn.execute(
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth t"
        " WHERE revision_no = (SELECT MAX(revision_no)"
        "  FROM calibration_station_daily_high_truth x"
        "  WHERE x.station_id=t.station_id AND x.local_date=t.local_date)"
    ):
        truth[(str(station), str(day))] = float(value)

    # The bar: each incumbent system's residual about its own mean, on this
    # station, after bias_c.  Dispersion is what a new source has to beat --
    # offset is free.
    incumbent: dict[str, dict[str, float]] = defaultdict(dict)
    for station, system, bias_json in conn.execute(
        "SELECT station_id, ensemble_system_id, bias_parameters_json"
        " FROM calibration_native_ensemble_system_artifact"
        " WHERE lead_bucket='h24_48'"
        " GROUP BY station_id, ensemble_system_id"
    ):
        try:
            incumbent[str(station)][str(system)] = float(
                json.loads(bias_json)["bias_c"]
            )
        except (TypeError, ValueError, KeyError):
            continue
    raw: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for system, station, day, value in conn.execute(
        "SELECT ensemble_system_id, station_id, target_local_date,"
        " AVG(raw_tmax_c)"
        " FROM calibration_native_ensemble_historical_member_run"
        " WHERE qc_status='ACCEPTED' AND archive_lead_bucket='h24_48'"
        " GROUP BY 1,2,3"
    ):
        raw[(str(station), str(day))][str(system)] = float(value)

    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=DAYS_BACK)
    results = []
    for row in candidates:
        station = row["station_id"]
        if station not in coords:
            continue
        lat, lon = coords[station]
        models = [
            (key, label) for key, label, w, e, s, n in DOMAINS
            if w <= lon <= e and s <= lat <= n
        ]
        if not models:
            print(f"  {station:6s} [{row['flag_count']}/5] 无区域模型覆盖")
            continue
        zone = zones.get(station, "UTC")
        url = (
            f"{ARCHIVE}?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m"
            f"&models={','.join(k for k, _l in models)}"
            f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
            f"&timezone={quote(zone)}"
        )
        data = _fetch(url)
        time.sleep(PAUSE)
        if not data or "hourly" not in data:
            print(f"  {station:6s} 拉取失败")
            continue
        hourly = data["hourly"]
        times = hourly.get("time") or []
        elevation = data.get("elevation")

        entry = {
            "station_id": station,
            "flag_count": row["flag_count"],
            "centre_residual_c": row["centre_residual_c"],
            "grid_elevation_m": elevation,
            "models": [],
        }
        for key, label in models:
            column = (
                f"temperature_2m_{key}" if len(models) > 1
                else "temperature_2m"
            )
            series = hourly.get(column)
            if not series:
                continue
            daily: dict[str, float] = {}
            for stamp, value in zip(times, series):
                if value is None:
                    continue
                day = str(stamp)[:10]
                daily[day] = max(daily.get(day, -99.0), float(value))
            paired = [
                (truth[(station, day)] - value)
                for day, value in daily.items()
                if (station, day) in truth
            ]
            if len(paired) < 20:
                continue
            entry["models"].append({
                "model": key,
                "label": label,
                "paired_days": len(paired),
                "bias_c": mean(paired),
                "dispersion_c": pstdev(paired),
                "mae_after_bias_c": mean(
                    abs(v - mean(paired)) for v in paired
                ),
            })

        # The bar, on the same days.
        bar = []
        for system in {
            s for (st, _d), systems in raw.items() if st == station
            for s in systems
        }:
            shift = incumbent.get(station, {}).get(system)
            if shift is None:
                continue
            errs = [
                truth[(station, day)] - (value + shift)
                for (st, day), systems in raw.items()
                if st == station and system in systems
                for value in [systems[system]]
                if (station, day) in truth
            ]
            if len(errs) >= 20:
                bar.append({
                    "system": system, "days": len(errs),
                    "dispersion_c": pstdev(errs),
                    "mae_c": mean(abs(v) for v in errs),
                })
        entry["incumbents"] = sorted(
            bar, key=lambda b: b["dispersion_c"]
        )[:3]
        results.append(entry)

        best_new = min(
            (m["dispersion_c"] for m in entry["models"]), default=None
        )
        best_old = min(
            (b["dispersion_c"] for b in bar), default=None
        )
        verdict = "—"
        if best_new is not None and best_old is not None:
            ratio = best_new / best_old
            verdict = f"{ratio:.2f}×" + (" ✅过线" if ratio < 0.9 else "")
        print(
            f"  {station:6s} [{row['flag_count']}/5]"
            f" 海拔{elevation if elevation is not None else '—'}m"
            f" 模型{len(entry['models'])}"
            f" 新最好σ={best_new if best_new is None else round(best_new,2)}"
            f" 现役最好σ={best_old if best_old is None else round(best_old,2)}"
            f"  {verdict}"
        )

    OUT.write_text(
        json.dumps(
            {"window": [start.isoformat(), end.isoformat()],
             "results": results},
            ensure_ascii=False, indent=1, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"\n写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
