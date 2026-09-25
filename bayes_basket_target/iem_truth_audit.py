"""Every settled station-day against IEM's own copy of the same reports.

User ruling 2026-09-25 (「好」 to 「把 IEM 真值对账做成每日只读检查」).  IEM
(Iowa Environmental Mesonet) archives the same METAR/SPECI traffic the AWC
lane captures; it is an ``aggregator_republisher`` and never writes truth
here.  A settled day whose maximum IEM disagrees with is a prompt to look at
the official evidence -- the kind of day this caught by hand on 2026-09-24:
KATL 09-09 settled at 30.6C while its afternoon reports (backfilled into the
archive a fortnight later) read 31.7C, and EDDM 08-01 settled at 20C from ten
night reports during a capture outage while IEM read 30C.  Charter 207's
revision path decides what, if anything, changes.

Read-only by construction: IEM over the network, the SSOT through one short
read-only connection, and two files under ``data/``.  Temperatures come off
the raw report text through production's own rules (the body group token of
``metar_path_state``, the remark-tenths reading of ``metar_precision``), the
local day is ``settlement_time.settlement_day_window`` compared as instants,
and IEM's coverage is judged by the reconciliation lane's warm-window gap.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from .contracts import ContractError
from .metar_path_state import _TEMP_DEW
from .metar_precision import (
    REMARK_SEPARATOR,
    normalize_report_text,
    official_temperature_c,
)
from .settlement_reconciliation import warm_window_gap
from .settlement_time import settlement_day_window

SCHEMA = "iem_truth_audit_v1"
IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
USER_AGENT = "weather-iem-truth-audit/1"
# Half a Celsius degree: any disagreement at a whole-degree station, and about
# one Fahrenheit degree at the US ASOS stations, whose reports carry tenths.
DISAGREEMENT_C = 0.5
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_DAYS = 45

AGREE = "AGREE"
IEM_ABOVE = "IEM_ABOVE"
IEM_BELOW = "IEM_BELOW"
IEM_INCOMPLETE = "IEM_INCOMPLETE"
NO_IEM = "NO_IEM"
IEM_FETCH_FAILED = "IEM_FETCH_FAILED"
# IEM throttles per address (HTTP 429): a throttled request waits and is asked
# again, at most this many times, never longer than the cap per wait.
THROTTLE_ATTEMPTS = 4
THROTTLE_WAIT_CAP_S = 120.0

Report = dict[str, Any]
Fetch = Callable[[str, date, date], Sequence[Report]]


def iem_station_id(station_id: str) -> str:
    """IEM names the US ASOS stations without the leading K (``KATL`` -> ``ATL``)."""

    station = str(station_id).strip().upper()
    if len(station) == 4 and station.startswith("K"):
        return station[1:]
    return station


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def iem_query_url(iem_id: str, first_utc_date: date, last_utc_date: date) -> str:
    params = [
        ("station", iem_id),
        ("data", "metar"),
        ("year1", first_utc_date.year),
        ("month1", first_utc_date.month),
        ("day1", first_utc_date.day),
        ("year2", last_utc_date.year),
        ("month2", last_utc_date.month),
        ("day2", last_utc_date.day),
        ("tz", "Etc/UTC"),
        ("format", "onlycomma"),
        ("latlon", "no"),
        ("missing", "empty"),
        ("report_type", 3),
        ("report_type", 4),
    ]
    return IEM_ASOS_URL + "?" + urlencode(params)


def parse_iem_csv(text: str) -> list[Report]:
    """``station,valid,metar`` rows, ``valid`` in UTC, into timed raw reports."""

    reports: list[Report] = []
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not {"valid", "metar"} <= set(reader.fieldnames):
        if text.strip():
            raise ContractError("iem_truth_audit_response_not_csv")
        return reports
    for row in reader:
        raw = str(row.get("metar") or "").strip()
        stamp = str(row.get("valid") or "").strip()
        if not raw or not stamp:
            continue
        valid = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        reports.append({"valid_at_utc": valid, "raw": raw})
    return reports


def _throttle_wait(response: Any, attempt: int) -> float:
    try:
        stated = float(response.headers.get("Retry-After") or 0.0)
    except (TypeError, ValueError):
        stated = 0.0
    return min(THROTTLE_WAIT_CAP_S, stated if stated > 0 else 20.0 * attempt)


def fetch_iem_reports(
    client: Any,
    *,
    iem_id: str,
    first_utc_date: date,
    last_utc_date: date,
    max_bytes: int = MAX_RESPONSE_BYTES,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Report]:
    url = iem_query_url(iem_id, first_utc_date, last_utc_date)
    for attempt in range(1, THROTTLE_ATTEMPTS + 1):
        body = bytearray()
        with client.stream("GET", url) as response:
            if response.status_code in (429, 503) and attempt < THROTTLE_ATTEMPTS:
                sleep(_throttle_wait(response, attempt))
                continue
            if response.status_code != 200:
                raise ContractError(f"iem_truth_audit_http_{response.status_code}")
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ContractError("iem_truth_audit_response_too_large")
        return parse_iem_csv(body.decode("utf-8", errors="replace"))
    raise ContractError("iem_truth_audit_throttled")


def report_temperature_c(raw: str) -> float | None:
    """The temperature a report states, read the way settlement reads it."""

    body = normalize_report_text(raw).upper().split(REMARK_SEPARATOR, 1)[0]
    match = _TEMP_DEW.search(body)
    if match is None:
        return None
    token = match.group(1)
    whole = -float(token[1:]) if token.startswith("M") else float(token)
    return float(official_temperature_c(raw, whole_degree_c=whole)[0])


def iem_day_summary(
    reports: Sequence[Report], *, event_date: str, settlement_timezone: str
) -> dict[str, Any]:
    """IEM's maximum for one local day ``[start, end)``, compared as instants."""

    window = settlement_day_window(event_date, settlement_timezone)
    start = _utc(window["target_day_start_utc"])
    end = _utc(window["target_day_end_utc"])
    in_day = [
        report for report in reports if start <= report["valid_at_utc"] < end
    ]
    readings = [
        (temperature, report)
        for report in in_day
        if (temperature := report_temperature_c(report["raw"])) is not None
    ]
    gap = warm_window_gap(
        {
            "settlement_timezone": settlement_timezone,
            "observations": [
                {"valid_at_utc": report["valid_at_utc"].isoformat()}
                for _temperature, report in readings
            ],
        },
        event_date=event_date,
    )
    if not readings:
        return {"report_count": len(in_day), "max_c": None, "warm_window_gap": gap}
    max_c, max_report = max(readings, key=lambda pair: pair[0])
    return {
        "report_count": len(in_day),
        "max_c": max_c,
        "max_valid_utc": max_report["valid_at_utc"].isoformat().replace("+00:00", "Z"),
        "max_report": max_report["raw"],
        "warm_window_gap": gap,
    }


def classify(truth_c: float, summary: Mapping[str, Any]) -> str:
    if summary.get("max_c") is None:
        return NO_IEM
    gap = summary.get("warm_window_gap")
    if not isinstance(gap, Mapping) or gap.get("complete") is not True:
        return IEM_INCOMPLETE
    diff = float(summary["max_c"]) - float(truth_c)
    if diff >= DISAGREEMENT_C:
        return IEM_ABOVE
    if diff <= -DISAGREEMENT_C:
        return IEM_BELOW
    return AGREE


def standing_truths(
    conn: sqlite3.Connection, *, first_date: str, last_date: str
) -> dict[tuple[str, str], dict[str, Any]]:
    """The latest accepted revision of every settled station-day in the window."""

    truths: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT station_id,local_date,official_tmax_c,revision_no,source_lane,"
        "settlement_timezone FROM calibration_station_daily_high_truth "
        "WHERE qc_status='ACCEPTED' AND local_date>=? AND local_date<=? "
        "ORDER BY revision_no",
        (first_date, last_date),
    ):
        truths[(str(row[0]), str(row[1]))] = {
            "station_id": str(row[0]),
            "event_date": str(row[1]),
            "truth_c": float(row[2]),
            "revision_no": int(row[3]),
            "source_lane": str(row[4]),
            "settlement_timezone": str(row[5]),
        }
    return truths


def audit(
    truths: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    fetch: Fetch,
) -> list[dict[str, Any]]:
    """One row per settled station-day: the truth, IEM's maximum, the verdict."""

    by_station: dict[str, list[Mapping[str, Any]]] = {}
    for (station, _day), truth in sorted(truths.items()):
        by_station.setdefault(station, []).append(truth)
    rows: list[dict[str, Any]] = []
    for station, days in sorted(by_station.items()):
        dates = sorted(date.fromisoformat(str(t["event_date"])) for t in days)
        try:
            reports = list(fetch(
                station, dates[0] - timedelta(days=1), dates[-1] + timedelta(days=1)
            ))
            fetch_error = ""
        except (ContractError, OSError, ValueError) as exc:
            reports, fetch_error = [], f"{type(exc).__name__}:{str(exc)[:160]}"
        for truth in days:
            summary = iem_day_summary(
                reports,
                event_date=str(truth["event_date"]),
                settlement_timezone=str(truth["settlement_timezone"]),
            )
            verdict = (
                IEM_FETCH_FAILED
                if fetch_error
                else classify(float(truth["truth_c"]), summary)
            )
            row = {
                **dict(truth),
                "iem_id": iem_station_id(station),
                "verdict": verdict,
                "iem_max_c": summary.get("max_c"),
                "iem_report_count": summary.get("report_count"),
                "iem_warm_window_gap": summary.get("warm_window_gap"),
            }
            if summary.get("max_c") is not None:
                row["diff_c"] = round(float(summary["max_c"]) - float(truth["truth_c"]), 2)
                row["iem_max_valid_utc"] = summary.get("max_valid_utc")
                row["iem_max_report"] = summary.get("max_report")
            if fetch_error:
                row["fetch_error"] = fetch_error
            rows.append(row)
    return rows


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["verdict"])] = counts.get(str(row["verdict"]), 0) + 1
    judged = counts.get(AGREE, 0) + counts.get(IEM_ABOVE, 0) + counts.get(IEM_BELOW, 0)
    disagreements = [
        dict(row) for row in rows if row["verdict"] in (IEM_ABOVE, IEM_BELOW)
    ]
    disagreements.sort(key=lambda row: (-abs(float(row.get("diff_c") or 0.0)), row["station_id"], row["event_date"]))
    fetch_failed = {
        str(row["station_id"]): str(row["fetch_error"])
        for row in rows
        if row.get("fetch_error")
    }
    return {
        "counts": dict(sorted(counts.items())),
        "judged": judged,
        "agree_share": round(counts.get(AGREE, 0) / judged, 4) if judged else None,
        "disagreements": disagreements,
        "fetch_failed_stations": dict(sorted(fetch_failed.items())),
    }


def history_key(row: Mapping[str, Any]) -> str:
    return "|".join(
        str(row.get(field))
        for field in ("station_id", "event_date", "verdict", "truth_c", "revision_no", "iem_max_c")
    )


def status_payload(
    rows: Sequence[Mapping[str, Any]], *, first_date: str, last_date: str, checked_at: str
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "checked_at": checked_at,
        "window": {"first_date": first_date, "last_date": last_date},
        "station_day_count": len(rows),
        "disagreement_c": DISAGREEMENT_C,
        **summarize(rows),
        "source_role": "aggregator_republisher",
        "writes_ssot": False,
        "calls_order_gateway": False,
    }


__all__ = [
    "AGREE",
    "IEM_ABOVE",
    "IEM_BELOW",
    "IEM_FETCH_FAILED",
    "IEM_INCOMPLETE",
    "NO_IEM",
    "SCHEMA",
    "audit",
    "classify",
    "fetch_iem_reports",
    "history_key",
    "iem_day_summary",
    "iem_query_url",
    "iem_station_id",
    "parse_iem_csv",
    "report_temperature_c",
    "standing_truths",
    "status_payload",
    "summarize",
]
