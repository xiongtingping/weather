#!/usr/bin/env python3
"""Multi-year NCEI GHCNh daily-high truth for the band-reliability replay.

Research-only: writes the standalone replay evidence database and raw PSV
files under data/research; never opens archive.db or the production SSOT
for writing.  Three phases:

  resolve   Map each registry station to a GHCNh station id (exact ICAO
            match, else a pinned near-coordinate candidate) and record it.
  fetch     Download by-year PSV files for mapped stations and reduce them
            to one daily maximum per station-local settlement day.
  validate  Compare a closed validation window against the IEM METAR
            archive for the *target* airport (settlement semantics), plus
            any overlap with production settled truth, and mark each
            mapping ACCEPTED or REJECTED; only accepted stations feed the
            replay OOS.

The mapping acceptance is measured, not asserted: a pinned neighbour that
does not reproduce the target airport's METAR daily maxima is rejected
along with every day it produced.  Production truth cannot serve as the
only referee here: GHCNh's element processing lags months behind its
timestamps (probed 2026-08-15, temperatures end 2026-03-26), so there is
structurally no overlap with the live truth table yet.  The production
comparison stays in the receipt and starts binding as NCEI catches up.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)
from scripts.multiyear_band_replay_common import (  # noqa: E402
    DEFAULT_REPLAY_DB,
    DEFAULT_REPLAY_DIR,
    load_station_timezones,
    open_production_db_readonly,
    open_replay_db,
    utc_now_iso,
    write_json_report,
)

STATION_LIST_URL = (
    "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/"
    "hourly/doc/ghcnh-station-list.csv"
)
YEAR_URL = (
    "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/"
    "hourly/access/by-year/{year}/psv/GHCNh_{station}_{year}.psv"
)
# The by-year lane lags the present by months (KMIA's 2026 file, rebuilt
# 2026-08-14, ends 2026-03-26), so the current year comes from the tail of
# the fresh period-of-record file instead -- that lane ran ~3 days behind
# real time when probed.  Without it there is no overlap with production
# settled truth and the mapping validation cannot run.
POR_URL = (
    "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/"
    "hourly/access/by-station/GHCNh_{station}_por.psv"
)
POR_TAIL_BYTES = 96 * 1024 * 1024
POR_HEADER_BYTES = 64 * 1024
USER_AGENT = "weather-research/1.0 (multiyear band replay truth lane)"
MAX_YEAR_FILE_BYTES = 160 * 1024 * 1024
# ISD semantics: 5 means "passed all QC, NCEI source" and carries most METAR
# temperatures (KMIA 2025: 5,706 of 8,656 FM15 rows).  Treating it as bad --
# as the ZUUU research lane did -- silently reduces a station to its 3-hourly
# SYNOP rows.  Measured with 5 admitted, KMIA 2025 matches the IEM METAR
# archive on 352/352 overlap days exactly.
TEMPERATURE_QC_BAD = {"2", "3", "6", "7"}

# Stations whose ICAO never appears in the GHCNh station list.  Candidates
# were found by coordinate search over stations that still publish by-year
# files (probed 2026-08-15); every one of them must still pass the overlap
# validation before its rows count.  Stations with no candidate publishing
# recent data are UNRESOLVED and excluded from the replay truth lane.
PINNED_COORDINATE_CANDIDATES = {
    "CYYZ": ("CAN06158731", 0.3),   # Toronto Pearson Intl A
    "EPWA": ("PLI0000EPBC", 12.2),  # Warszawa Babice -- neighbour, must prove
    "FACT": ("SFM00068999", 0.3),   # Cape Town AWS on field
    "LFPB": ("FRI0000LFPG", 10.2),  # Charles de Gaulle -- neighbour, must prove
    "LIMC": ("ITMU0016064", 13.1),  # Cameri -- neighbour, must prove
    "LTAC": ("TUM00017130", 21.9),  # Ankara Central -- neighbour, must prove
    "MPMG": ("PMW00010718", 7.5),   # Ft Kobbe -- neighbour, must prove
    "RCSS": ("CHM00058968", 5.3),   # Taibei city -- neighbour, must prove
}
UNRESOLVED_STATIONS = ("OPKC", "ZBAA", "ZHHH", "ZSQD")

# Daily-max coverage gate.  A settlement day whose observation record
# cannot bracket the afternoon peak must not become truth.
MIN_DISTINCT_HOURS = 18
PEAK_WINDOW_LOCAL = (10, 20)
PEAK_WINDOW_MAX_GAP_MINUTES = 120

# Mapping acceptance thresholds, applied to the IEM comparison window.
VALIDATE_MIN_OVERLAP_DAYS = 200
VALIDATE_MIN_EXACT_RATE = 0.70
VALIDATE_MIN_WITHIN_ONE_RATE = 0.95
VALIDATE_MAX_ABS_MEAN_BIAS = 0.30

# IEM ASOS archive: raw METAR temperatures for the exact target airport,
# which is the settlement lane's own semantics.  Rate-limited, so requests
# are throttled and retried.
IEM_ASOS_URL = (
    "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    "?station={station}&data=tmpc&year1={y1}&month1={m1}&day1={d1}"
    "&year2={y2}&month2={m2}&day2={d2}&tz=Etc/UTC&format=onlycomma"
    "&latlon=no&missing=empty"
)
IEM_THROTTLE_SECONDS = 6.0
IEM_MAX_ATTEMPTS = 5


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=120.0,
        follow_redirects=False,
    )


def _round_half_up(value: float) -> int:
    import math

    return int(math.floor(float(value) + 0.5))


def resolve(args: argparse.Namespace) -> dict[str, Any]:
    replay = open_replay_db(args.replay_db)
    production = open_production_db_readonly(args.production_db)
    timezones = load_station_timezones(production)
    stations = sorted(coordinate_registry_manifest()["station_ids"])
    with _client() as client:
        response = client.get(STATION_LIST_URL)
        response.raise_for_status()
        text = response.text
    by_icao: dict[str, list[list[str]]] = defaultdict(list)
    for row in csv.reader(io.StringIO(text)):
        if len(row) > 9 and row[9].strip():
            by_icao[row[9].strip().upper()].append(row)
    written = []
    for station in stations:
        timezone_name = timezones.get(station, "")
        if not timezone_name:
            raise RuntimeError(f"replay_truth_timezone_missing:{station}")
        if station in UNRESOLVED_STATIONS:
            record = (station, "", "COORDINATE_NEAREST", -1.0, timezone_name,
                      "UNRESOLVED")
        elif station in PINNED_COORDINATE_CANDIDATES:
            ghcnh_id, distance = PINNED_COORDINATE_CANDIDATES[station]
            record = (station, ghcnh_id, "COORDINATE_NEAREST", distance,
                      timezone_name, "PROVISIONAL")
        else:
            rows = by_icao.get(station, [])
            if len(rows) != 1:
                raise RuntimeError(
                    f"replay_truth_icao_ambiguous:{station}:{len(rows)}"
                )
            record = (station, rows[0][0].strip(), "ICAO_EXACT", 0.0,
                      timezone_name, "PROVISIONAL")
        replay.execute(
            "INSERT INTO replay_truth_source(station_id, ghcnh_station_id,"
            " mapping_method, distance_km, settlement_timezone,"
            " acceptance_state, updated_at_utc)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(station_id) DO UPDATE SET"
            " ghcnh_station_id=excluded.ghcnh_station_id,"
            " mapping_method=excluded.mapping_method,"
            " distance_km=excluded.distance_km,"
            " settlement_timezone=excluded.settlement_timezone,"
            # Re-resolving re-opens the acceptance question; validate is
            # the only phase that closes it again.
            " acceptance_state=excluded.acceptance_state,"
            " acceptance_receipt_json='{}',"
            " updated_at_utc=excluded.updated_at_utc",
            (*record, utc_now_iso()),
        )
        written.append({"station_id": record[0], "ghcnh_station_id": record[1],
                        "state": record[5]})
    replay.commit()
    return {"phase": "resolve", "stations": written}


def _reduce_year(
    psv_text: str,
    *,
    ghcnh_id: str,
    timezone_name: str,
) -> dict[str, dict[str, Any]]:
    tz = ZoneInfo(timezone_name)
    per_day: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    reader = csv.DictReader(io.StringIO(psv_text), delimiter="|")
    for row in reader:
        if str(row.get("STATION") or "").strip() != ghcnh_id:
            raise RuntimeError(
                f"replay_truth_psv_station_mismatch:{row.get('STATION')}"
            )
        raw_temp = str(row.get("temperature") or "").strip()
        if not raw_temp:
            continue
        if str(row.get("temperature_Quality_Code") or "").strip() \
                in TEMPERATURE_QC_BAD:
            continue
        try:
            temp = float(raw_temp)
        except ValueError:
            continue
        if not -90.0 <= temp <= 60.0:
            continue
        raw_date = str(row.get("DATE") or "").strip()
        try:
            valid = datetime.fromisoformat(raw_date)
        except ValueError:
            continue
        if valid.tzinfo is None:
            valid = valid.replace(tzinfo=timezone.utc)
        local = valid.astimezone(tz)
        per_day[local.date().isoformat()].append((local, temp))
    summaries: dict[str, dict[str, Any]] = {}
    for day, observations in per_day.items():
        observations.sort(key=lambda item: item[0])
        hours = {obs[0].hour for obs in observations}
        peak = [
            obs for obs in observations
            if PEAK_WINDOW_LOCAL[0] <= obs[0].hour <= PEAK_WINDOW_LOCAL[1]
        ]
        peak_gap = None
        if len(peak) >= 2:
            peak_gap = max(
                int((right[0] - left[0]).total_seconds() // 60)
                for left, right in zip(peak, peak[1:])
            )
        coverage_ok = (
            len(hours) >= MIN_DISTINCT_HOURS
            and peak_gap is not None
            and peak_gap <= PEAK_WINDOW_MAX_GAP_MINUTES
            and min(hours) <= 2
            and max(hours) >= 22
        )
        tmax = max(obs[1] for obs in observations)
        summaries[day] = {
            "tmax_c": tmax,
            "tmax_int": _round_half_up(tmax),
            "observation_count": len(observations),
            "distinct_hour_count": len(hours),
            "peak_window_max_gap_minutes": peak_gap,
            "coverage_ok": 1 if coverage_ok else 0,
        }
    return summaries


def _fetch_year_text(
    client: httpx.Client,
    *,
    ghcnh_id: str,
    year: int,
) -> str | None:
    """One year of PSV rows, header line included, or None when absent.

    Closed years come from the by-year lane.  The current year comes from a
    bounded tail of the period-of-record file: the header is fetched from
    the file's head, the tail is cut at its first complete line, and rows
    from other years are dropped by the caller's reducer via the DATE
    filter below.
    """

    current_year = datetime.now(timezone.utc).year
    if year < current_year:
        response = client.get(YEAR_URL.format(year=year, station=ghcnh_id))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        if len(response.content) > MAX_YEAR_FILE_BYTES:
            raise RuntimeError(
                f"replay_truth_year_file_too_large:{ghcnh_id}:{year}"
            )
        return response.content.decode("utf-8", "replace")
    url = POR_URL.format(station=ghcnh_id)
    head = client.get(
        url, headers={"Range": f"bytes=0-{POR_HEADER_BYTES - 1}"}
    )
    if head.status_code == 404:
        return None
    if head.status_code not in (200, 206):
        head.raise_for_status()
    header_line = head.content.decode("utf-8", "replace").splitlines()[0]
    tail = client.get(url, headers={"Range": f"bytes=-{POR_TAIL_BYTES}"})
    if tail.status_code not in (200, 206):
        tail.raise_for_status()
    tail_text = tail.content.decode("utf-8", "replace")
    if tail.status_code == 206 and "\n" in tail_text:
        tail_text = tail_text.split("\n", 1)[1]
    if tail_text.startswith("STATION|"):
        tail_text = tail_text.split("\n", 1)[1] if "\n" in tail_text else ""
    year_prefix = f"{year}-"
    rows = [
        line
        for line in tail_text.splitlines()
        if line.split("|", 3)[2:3] and line.split("|", 3)[2].startswith(
            year_prefix
        )
    ]
    if not rows:
        return None
    return header_line + "\n" + "\n".join(rows) + "\n"


def fetch(args: argparse.Namespace) -> dict[str, Any]:
    replay = open_replay_db(args.replay_db)
    sources = replay.execute(
        "SELECT station_id, ghcnh_station_id, settlement_timezone"
        " FROM replay_truth_source"
        " WHERE acceptance_state IN ('PROVISIONAL','ACCEPTED')"
        + (" AND station_id=?" if args.station_id else ""),
        ((args.station_id,) if args.station_id else ()),
    ).fetchall()
    if not sources:
        raise RuntimeError("replay_truth_no_resolved_sources")
    years = _parse_years(args.years)
    raw_dir = Path(args.replay_db).parent / "ghcnh_raw"
    report: list[dict[str, Any]] = []
    with _client() as client:
        for station, ghcnh_id, timezone_name in sources:
            for year in years:
                raw_path = raw_dir / f"GHCNh_{ghcnh_id}_{year}.psv"
                if raw_path.exists() and not args.refresh:
                    text = raw_path.read_text(encoding="utf-8")
                else:
                    text = _fetch_year_text(
                        client, ghcnh_id=ghcnh_id, year=year
                    )
                    if text is None:
                        report.append({
                            "station_id": station, "year": year,
                            "state": "MISSING_YEAR_FILE",
                        })
                        continue
                    if args.keep_raw:
                        raw_path.parent.mkdir(parents=True, exist_ok=True)
                        raw_path.write_text(text, encoding="utf-8")
                sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                summaries = _reduce_year(
                    text, ghcnh_id=ghcnh_id, timezone_name=timezone_name
                )
                current_year = datetime.now(timezone.utc).year
                if year == current_year and summaries:
                    # The tail cut may open mid-day; drop the first local
                    # date, which can be partial without looking partial.
                    first_day = min(summaries)
                    summaries.pop(first_day, None)
                now = utc_now_iso()
                replay.executemany(
                    "INSERT INTO replay_daily_truth(station_id, local_date,"
                    " tmax_c, tmax_int, observation_count,"
                    " distinct_hour_count, peak_window_max_gap_minutes,"
                    " coverage_ok, ghcnh_station_id, source_year_sha256,"
                    " ingested_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(station_id, local_date) DO UPDATE SET"
                    " tmax_c=excluded.tmax_c, tmax_int=excluded.tmax_int,"
                    " observation_count=excluded.observation_count,"
                    " distinct_hour_count=excluded.distinct_hour_count,"
                    " peak_window_max_gap_minutes="
                    "  excluded.peak_window_max_gap_minutes,"
                    " coverage_ok=excluded.coverage_ok,"
                    " ghcnh_station_id=excluded.ghcnh_station_id,"
                    " source_year_sha256=excluded.source_year_sha256,"
                    " ingested_at_utc=excluded.ingested_at_utc",
                    [
                        (station, day, s["tmax_c"], s["tmax_int"],
                         s["observation_count"], s["distinct_hour_count"],
                         s["peak_window_max_gap_minutes"], s["coverage_ok"],
                         ghcnh_id, sha, now)
                        for day, s in sorted(summaries.items())
                    ],
                )
                replay.commit()
                covered = sum(
                    1 for s in summaries.values() if s["coverage_ok"]
                )
                report.append({
                    "station_id": station, "year": year, "state": "OK",
                    "days": len(summaries), "coverage_ok_days": covered,
                })
    return {"phase": "fetch", "years": years, "results": report}


def _iem_station_code(station_id: str) -> str:
    """IEM's ASOS identifiers drop the K prefix for the contiguous US."""

    if len(station_id) == 4 and station_id.startswith("K"):
        return station_id[1:]
    return station_id


def _iem_daily_max(
    client: httpx.Client,
    *,
    station_id: str,
    timezone_name: str,
    window: tuple[str, str],
) -> dict[str, int]:
    import time as time_module

    start = datetime.fromisoformat(window[0])
    end = datetime.fromisoformat(window[1])
    url = IEM_ASOS_URL.format(
        station=_iem_station_code(station_id),
        y1=start.year, m1=start.month, d1=start.day,
        y2=end.year, m2=end.month, d2=end.day,
    )
    text = ""
    for attempt in range(IEM_MAX_ATTEMPTS):
        response = client.get(url)
        if response.status_code == 200 and response.text.startswith(
            "station,"
        ):
            text = response.text
            break
        time_module.sleep(IEM_THROTTLE_SECONDS * (attempt + 1))
    else:
        raise RuntimeError(f"replay_truth_iem_unavailable:{station_id}")
    tz = ZoneInfo(timezone_name)
    per_day: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 3 or not parts[2].strip():
            continue
        try:
            valid = datetime.strptime(
                parts[1].strip(), "%Y-%m-%d %H:%M"
            ).replace(tzinfo=timezone.utc)
            temp = float(parts[2])
        except ValueError:
            continue
        if not -90.0 <= temp <= 60.0:
            continue
        local = valid.astimezone(tz)
        per_day[local.date().isoformat()].append((local, temp))
    result: dict[str, int] = {}
    for day, observations in per_day.items():
        observations.sort(key=lambda item: item[0])
        hours = {obs[0].hour for obs in observations}
        peak = [
            obs for obs in observations
            if PEAK_WINDOW_LOCAL[0] <= obs[0].hour <= PEAK_WINDOW_LOCAL[1]
        ]
        peak_gap = None
        if len(peak) >= 2:
            peak_gap = max(
                int((right[0] - left[0]).total_seconds() // 60)
                for left, right in zip(peak, peak[1:])
            )
        if (
            len(hours) >= MIN_DISTINCT_HOURS
            and peak_gap is not None
            and peak_gap <= PEAK_WINDOW_MAX_GAP_MINUTES
            and min(hours) <= 2
            and max(hours) >= 22
        ):
            result[day] = _round_half_up(
                max(obs[1] for obs in observations)
            )
    return result


def _compare_pairs(pairs: list[tuple[int, int]]) -> dict[str, Any]:
    exact = sum(1 for g, o in pairs if g == o) / len(pairs)
    within_one = sum(1 for g, o in pairs if abs(g - o) <= 1) / len(pairs)
    bias = sum(g - o for g, o in pairs) / len(pairs)
    return {
        "days": len(pairs),
        "exact_rate": round(exact, 4),
        "within_one_rate": round(within_one, 4),
        "mean_bias_c": round(bias, 4),
        "passes": (
            exact >= VALIDATE_MIN_EXACT_RATE
            and within_one >= VALIDATE_MIN_WITHIN_ONE_RATE
            and abs(bias) <= VALIDATE_MAX_ABS_MEAN_BIAS
        ),
    }


def validate(args: argparse.Namespace) -> dict[str, Any]:
    import time as time_module

    replay = open_replay_db(args.replay_db)
    production = open_production_db_readonly(args.production_db)
    window = tuple(str(args.validate_window).split(":", 1))
    if len(window) != 2:
        raise RuntimeError("replay_truth_validate_window_invalid")
    official: dict[tuple[str, str], int] = {}
    for station, day, tmax in production.execute(
        "SELECT t.station_id, t.local_date, t.official_tmax_c"
        " FROM calibration_station_daily_high_truth AS t"
        " WHERE t.qc_status='ACCEPTED'"
        " AND t.revision_no=("
        "  SELECT MAX(t2.revision_no)"
        "  FROM calibration_station_daily_high_truth AS t2"
        "  WHERE t2.station_id=t.station_id AND t2.local_date=t.local_date"
        "  AND t2.qc_status='ACCEPTED')"
    ):
        official[(str(station), str(day))] = _round_half_up(float(tmax))
    results = []
    sources = replay.execute(
        "SELECT station_id, ghcnh_station_id, settlement_timezone,"
        " acceptance_state FROM replay_truth_source"
        + (" WHERE station_id=?" if args.station_id else "")
        + " ORDER BY station_id",
        ((args.station_id,) if args.station_id else ()),
    ).fetchall()
    with _client() as client:
        for station, ghcnh_id, timezone_name, state in sources:
            if state == "UNRESOLVED":
                results.append({"station_id": station, "state": "UNRESOLVED"})
                continue
            ghcnh_days: dict[str, int] = {
                str(day): int(tmax_int)
                for day, tmax_int in replay.execute(
                    "SELECT local_date, tmax_int FROM replay_daily_truth"
                    " WHERE station_id=? AND coverage_ok=1"
                    " AND local_date>=? AND local_date<=?",
                    (station, window[0], window[1]),
                )
            }
            receipt: dict[str, Any] = {
                "ghcnh_station_id": ghcnh_id,
                "validation_window": list(window),
                "validated_at_utc": utc_now_iso(),
            }
            production_pairs = [
                (int(tmax_int), official[(station, day)])
                for day, tmax_int in replay.execute(
                    "SELECT local_date, tmax_int FROM replay_daily_truth"
                    " WHERE station_id=? AND coverage_ok=1",
                    (station,),
                )
                if (station, str(day)) in official
            ]
            if production_pairs:
                receipt["production_overlap"] = _compare_pairs(
                    production_pairs
                )
            if not ghcnh_days:
                new_state = "REJECTED"
                receipt["reason"] = "no_ghcnh_days_in_window"
            else:
                iem_days = _iem_daily_max(
                    client,
                    station_id=station,
                    timezone_name=timezone_name,
                    window=(window[0], window[1]),
                )
                pairs = [
                    (ghcnh_days[day], iem_days[day])
                    for day in sorted(set(ghcnh_days) & set(iem_days))
                ]
                receipt["iem_days"] = len(iem_days)
                if len(pairs) < VALIDATE_MIN_OVERLAP_DAYS:
                    new_state = "REJECTED"
                    receipt["reason"] = "iem_overlap_too_small"
                    receipt["overlap_days"] = len(pairs)
                else:
                    comparison = _compare_pairs(pairs)
                    receipt["iem_comparison"] = comparison
                    prod = receipt.get("production_overlap")
                    production_veto = bool(
                        isinstance(prod, dict)
                        and prod.get("days", 0)
                        >= VALIDATE_MIN_OVERLAP_DAYS // 4
                        and not prod.get("passes")
                    )
                    new_state = (
                        "ACCEPTED"
                        if comparison["passes"] and not production_veto
                        else "REJECTED"
                    )
                time_module.sleep(IEM_THROTTLE_SECONDS)
            replay.execute(
                "UPDATE replay_truth_source SET acceptance_state=?,"
                " acceptance_receipt_json=?, updated_at_utc=?"
                " WHERE station_id=?",
                (new_state, json.dumps(receipt, sort_keys=True),
                 utc_now_iso(), station),
            )
            results.append({
                "station_id": station, "state": new_state, **receipt,
            })
    replay.commit()
    return {"phase": "validate", "results": results}


def _parse_years(value: str) -> list[int]:
    text = str(value or "").strip()
    if ":" in text:
        start_text, end_text = text.split(":", 1)
        years = list(range(int(start_text), int(end_text) + 1))
    else:
        years = sorted({int(item) for item in text.split(",") if item.strip()})
    now_year = datetime.now(timezone.utc).year
    if not years or min(years) < 2015 or max(years) > now_year:
        raise RuntimeError("replay_truth_years_invalid")
    return years


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["resolve", "fetch", "validate"])
    parser.add_argument("--replay-db", type=Path, default=DEFAULT_REPLAY_DB)
    parser.add_argument(
        "--production-db", type=Path, default=DEFAULT_TARGET_DB
    )
    parser.add_argument("--station-id")
    parser.add_argument("--years", default="2020:2026")
    parser.add_argument(
        "--validate-window",
        default="2025-01-01:2025-12-31",
        help="Closed local-date window compared against the IEM archive.",
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--keep-raw",
        action="store_true",
        help=(
            "Retain downloaded PSV text on disk.  Off by default: the "
            "reduced daily rows and their source sha are the evidence; "
            "the files are re-downloadable."
        ),
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.station_id:
        args.station_id = args.station_id.strip().upper()
    result = {"resolve": resolve, "fetch": fetch, "validate": validate}[
        args.phase
    ](args)
    result["guards"] = {
        "writes_archive_db": False,
        "writes_production_ssot": False,
        "writes_facts_sqlite": False,
        "places_orders": False,
    }
    if args.report:
        write_json_report(args.report, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
