#!/usr/bin/env python3
"""PROTOCOL.md: fetch each station's block of the NWS Point Forecast Matrices, as issued.

Source: the IEM AFOS archive (``aggregator_republisher``) of each WFO's PFM
text.  Only the station's own block is kept; the rest of each product is
discarded as it is read.  Each block's column times are derived from the WMO
header time and the UTC row, converted to the station's zone, and must agree
with the block's local-time row and date labels, or the whole product is not
used.  Read-only against SSOT (nothing is read from it).  Writes
pfm_station_blocks.jsonl.gz and fetch_summary.json.

    python3 fetch_pfm.py
"""
from __future__ import annotations

import gzip
import json
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
URL = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py?pil=PFM{wfo}&fmt=text&limit=9999&sdate={s}&edate={e}"
SOURCE_LABEL = "IEM AFOS archive of NWS PFM (aggregator_republisher)"
MAX_RESPONSE_BYTES = 40 * 1024 * 1024
MIN_FREE_DISK_BYTES = 4 * 1024 ** 3
FIRST, LAST = date(2026, 8, 11), date(2026, 9, 25)        # [FIRST, LAST)
WINDOW_DAYS = 5

# station -> (wfo, point name, "lat lon" as printed, zone); KHOU's point is IAH (proxy, not in the verdict)
POINTS = {
    "KATL": ("FFC", "Atlanta-Fulton GA", ("33.64", "84.43"), "America/New_York"),
    "KAUS": ("EWX", "Austin Bergstrom-Travis TX", ("30.19", "97.67"), "America/Chicago"),
    "KBKF": ("BOU", "Buckley AFB-Arapahoe CO", ("39.71", "104.76"), "America/Denver"),
    "KDAL": ("FWD", "Dallas Love Field-Dallas TX", ("32.86", "96.85"), "America/Chicago"),
    "KLAX": ("LOX", "Los Angeles Airport CA", ("33.94", "118.39"), "America/Los_Angeles"),
    "KLGA": ("OKX", "LaGuardia Airport-Queens NY", ("40.77", "73.90"), "America/New_York"),
    "KMIA": ("MFL", "Miami-Miami Dade FL", ("25.82", "80.28"), "America/New_York"),
    "KORD": ("LOT", "Chicago O'Hare International Airport-Cook IL", ("41.99", "87.91"), "America/Chicago"),
    "KSEA": ("SEW", "Seattle-King WA", ("47.44", "122.31"), "America/Los_Angeles"),
    "KSFO": ("MTR", "San Francisco Airport-San Mateo CA", ("37.62", "122.38"), "America/Los_Angeles"),
    "KHOU": ("HGX", "Bush Intercontinental Airport-Inland Harris TX", ("29.96", "95.35"), "America/Chicago"),
}
ZONE_OFFSET = {"EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "MDT": -6, "MST": -7, "PDT": -7, "PST": -8}
MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
COORD = re.compile(r"\s*(\d+\.\d+)N\s+(\d+\.\d+)W\s+Elev\.")
LABEL = re.compile(r"(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?(\d{2})/(\d{2})/(\d{2})")


class BlockError(ValueError):
    pass


def fetch(wfo, start, end):
    if shutil.disk_usage(HERE).free < MIN_FREE_DISK_BYTES:
        raise SystemExit("free disk below the floor; stopping")
    url = URL.format(wfo=wfo, s=start.isoformat(), e=end.isoformat())
    # curl, not urllib: it is the client whose trust store this host's proxy is configured for
    body = subprocess.run(["curl", "-sS", "--fail", "-m", "120", "--max-filesize", str(MAX_RESPONSE_BYTES), url],
                          capture_output=True, timeout=150, check=True).stdout
    if len(body) > MAX_RESPONSE_BYTES:
        raise SystemExit(f"{wfo} {start}: response over the cap; shrink the window")
    return body.decode("latin-1")


def issued_utc(text):
    """The WMO header's DDHHMM, with year and month taken from the issuance line."""

    header = re.search(r"^FOUS\d\d K(\w{3}) (\d{2})(\d{2})(\d{2})(?: [A-Z]{3})?\s*$", text, re.M)
    local = re.search(r"^(\d{1,2})(\d{2}) (AM|PM) ([A-Z]{3}) \w{3} (\w{3}) +(\d{1,2}) (\d{4})\s*$", text, re.M)
    if not header or not local or local.group(4) not in ZONE_OFFSET:
        raise BlockError("header or issuance line not readable")
    hour = int(local.group(1)) % 12 + (12 if local.group(3) == "PM" else 0)
    local_dt = datetime(int(local.group(7)), MONTHS[local.group(5)], int(local.group(6)), hour, int(local.group(2)))
    approx = (local_dt - timedelta(hours=ZONE_OFFSET[local.group(4)])).replace(tzinfo=timezone.utc)
    day, hh, mm = int(header.group(2)), int(header.group(3)), int(header.group(4))
    for shift in (0, -1, 1):
        base = approx + timedelta(days=shift)
        try:
            candidate = base.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            continue
        if abs((candidate - approx).total_seconds()) <= 3 * 3600:
            return candidate, header.group(0).strip()
    raise BlockError("WMO header time does not match the issuance line")


def station_block(text, coords):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = COORD.match(line)
        if m and (m.group(1), m.group(2)) == coords:
            end = i + 1
            while end < len(lines) and not lines[end].startswith("$$"):
                end += 1
            return lines[i - 1].strip(), lines[i + 1:end]
    return None, None


def _tokens(line):
    """(value, start, end) of each two-digit hour, positions in the whole line."""

    return [(int(m.group(0)), m.start() + 10, m.end() - 1 + 10) for m in re.finditer(r"\d{2}", line[10:])]


def _labels(line):
    """(start, date, weekday or None) of each date label; a label may lack its weekday."""

    return [(m.start(), date(2000 + int(m.group(4)), int(m.group(2)), int(m.group(3))), m.group(1))
            for m in LABEL.finditer(line)]


def _columns(anchor, step, local_tokens, utc_tokens, labels, tz):
    """The section's columns if this anchor makes every row and every label agree, else None.

    A label names the local date of the column under its first character (the
    column covering that position, else the next one to its right)."""

    columns = []
    for n, ((lh, lstart, lend), (uh, ustart, uend)) in enumerate(zip(local_tokens, utc_tokens)):
        at = anchor + timedelta(hours=step * n)
        local = at.astimezone(tz)
        if at.hour != uh or local.hour != lh or (lstart, lend) != (ustart, uend):
            return None
        columns.append((ustart, uend, local.date(), local.hour, at))
    for start, day, weekday in labels:
        column = next((c for c in columns if c[1] >= start), None)
        if column is None or column[2] != day or (weekday and day.strftime("%a") != weekday):
            return None
    return columns


def parse_sections(block, issued, zone):
    """Every section's columns with UTC time and local date; Max values by local date.

    A matrix may begin hours before the product was issued, so the first
    column's day is not assumed: every candidate day is tried, and exactly one
    must make the UTC row, the local row (in the station's zone) and every date
    label agree.  The 6-hourly section must start after the 3-hourly one."""

    tz = ZoneInfo(zone)
    maxima = {}
    previous_last = None
    i = 0
    while i < len(block):
        if not block[i].startswith("Date"):
            i += 1
            continue
        labels = _labels(block[i])
        local_line, utc_line = block[i + 1], block[i + 2]
        step = 3 if "3hrly" in local_line else 6 if "6hrly" in local_line else None
        if step is None or not utc_line.startswith("UTC") or not labels:
            raise BlockError("section rows not recognised")
        local_tokens, utc_tokens = _tokens(local_line), _tokens(utc_line)
        if len(local_tokens) != len(utc_tokens) or not utc_tokens:
            raise BlockError("local and UTC rows differ in length")
        first_hour = utc_tokens[0][0]
        lo, hi = ((issued - timedelta(hours=48), issued + timedelta(hours=24)) if previous_last is None
                  else (previous_last + timedelta(minutes=1), previous_last + timedelta(hours=48)))
        candidate = lo.replace(minute=0, second=0, microsecond=0)
        passing = []
        while candidate <= hi:
            if candidate >= lo and candidate.hour == first_hour:
                columns = _columns(candidate, step, local_tokens, utc_tokens, labels, tz)
                if columns is not None:
                    passing.append(columns)
            candidate += timedelta(hours=1)
        if len(passing) != 1:
            raise BlockError("date labels do not fix the column days" if not passing else "column days ambiguous")
        columns = passing[0]
        previous_last = columns[-1][4]
        row = next((line for line in block[i + 3:i + 8] if line.startswith(("Max/Min", "Min/Max"))), None)
        if row is not None:
            for m in re.finditer(r"-?\d+", row[7:]):
                end = m.end() - 1 + 7
                column = next((c for c in columns if abs(c[1] - end) <= 1), None)
                if column is None:
                    raise BlockError("Max/Min value not under a column")
                if column[3] >= 12:
                    if column[2] in maxima:
                        raise BlockError("two maxima for one date")
                    maxima[column[2]] = int(m.group(0))
        i += 3
    return maxima


def main():
    summary = Counter()
    rows = []
    for station, (wfo, name, coords, zone) in POINTS.items():
        start = FIRST
        while start < LAST:
            end = min(LAST, start + timedelta(days=WINDOW_DAYS))
            text = fetch(wfo, start, end)
            for product in text.split("\x01"):
                if "PFM" + wfo not in product:
                    continue
                summary[f"{station}:products"] += 1
                try:
                    issued, header = issued_utc(product)
                    point, block = station_block(product, coords)
                    if block is None:
                        summary[f"{station}:no_station_block"] += 1
                        continue
                    maxima = parse_sections(block, issued, zone)
                except BlockError as exc:
                    summary[f"{station}:rejected:{exc}"] += 1
                    continue
                if not maxima:
                    summary[f"{station}:no_max"] += 1
                    continue
                summary[f"{station}:used"] += 1
                rows.append({"station": station, "wfo": wfo, "point": point, "expected_point": name,
                             "issued_utc": issued.strftime("%Y-%m-%dT%H:%M:%SZ"), "wmo_header": header,
                             "max_f": {d.isoformat(): v for d, v in sorted(maxima.items())},
                             "source": SOURCE_LABEL, "block": "\n".join(block)})
            start = end
            time.sleep(1.0)
        print(station, {k.split(":", 1)[1]: v for k, v in summary.items() if k.startswith(station + ":")}, flush=True)
    seen = set()
    unique = []
    for row in sorted(rows, key=lambda r: (r["station"], r["issued_utc"], r["wmo_header"])):
        key = (row["station"], row["wmo_header"], json.dumps(row["max_f"], sort_keys=True))
        if key in seen:
            summary[f"{row['station']}:duplicate"] += 1
            continue
        seen.add(key)
        unique.append(row)
    with gzip.open(HERE / "pfm_station_blocks.jsonl.gz", "wt", encoding="utf-8") as out:
        for row in unique:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    names = Counter((r["station"], r["point"]) for r in unique)
    (HERE / "fetch_summary.json").write_text(json.dumps(
        {"source": SOURCE_LABEL, "window": [FIRST.isoformat(), LAST.isoformat()], "blocks_kept": len(unique),
         "counts": dict(sorted(summary.items())), "point_names": {f"{s}|{p}": n for (s, p), n in sorted(names.items())}},
        ensure_ascii=False, indent=1))
    print("kept", len(unique), file=sys.stderr)


if __name__ == "__main__":
    main()
