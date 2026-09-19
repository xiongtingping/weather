"""Every TAF the resident sees, kept whole: raw text and AWC's decoded record.

User ruling 2026-09-19 (「存原文」).  The forward capture keeps four numbers
per bulletin (TX, heating-window cloud, precipitation probability, wind
speed) and the raw text's hash; the text itself lives only in the rolling
inbox (about six days) and AWC answers ``date=`` queries for 30 days.  The
test of the TAF's unused physics (wind direction against the coast, gusts,
morning fog, CB/TCU, the hour of TX, TX - TN, change groups) had 30 days and
ran out of power: onshore days read 0.41C colder than P0-M, but a 23-day
walk-forward could not separate that from zero.  This file is what the next
test reads.

One line per bulletin, appended to ``<dir>/<UTC date received>.jsonl``: the
station, issue and AWC db-population times, when we received it, the request
it came in and AWC's whole row (``rawTAF`` plus ``fcsts`` -- the FM / BECMG /
TEMPO / PROB groups with their winds, visibility, weather and cloud layers).
Outside the SSOT: no writer lease, no lock, about 1 MB a day.

Archiving is a side effect of a capture and must never fail one: every error
is swallowed and counted.  A bulletin is written once per process (keyed on
station, issue time and raw-text hash; the day's and the previous day's
files seed the key set after a restart), so the per-round bulk probe that
returns every station's current bulletin costs one line only when a
bulletin is new.  Readers still dedupe on the same key -- two processes, or
a bulletin current across midnight, can write it twice.

``PM_TAF_RAW_ARCHIVE_DIR`` overrides the directory; ``off`` disables it
(the test suite sets that, as it does for every other real-data side
effect).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "taf_raw_archive_row_v1"
ENV = "PM_TAF_RAW_ARCHIVE_DIR"
DEFAULT_DIR = (
    Path(__file__).resolve().parents[1] / "data" / "runtime" / "taf_raw_archive"
)
# The key set is re-seeded from disk past this size, which bounds a
# long-lived resident's memory at a few days of bulletins.
MAX_SEEN_KEYS = 20_000

_SEEN: set[tuple[str, str, str]] = set()
_SEEDED: set[str] = set()
STATS = {"written": 0, "errors": 0}


def archive_dir() -> Path | None:
    value = str(os.environ.get(ENV) or "").strip()
    if value.lower() == "off":
        return None
    return Path(value).expanduser() if value else DEFAULT_DIR


def _key(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    raw = str(row.get("rawTAF") or "").strip()
    station = str(row.get("icaoId") or "").strip().upper()
    if not raw or not station:
        return None
    return (
        station,
        str(row.get("issueTime") or ""),
        hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def _seed(path: Path) -> None:
    if str(path) in _SEEDED:
        return
    _SEEDED.add(str(path))
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            _SEEN.add((
                str(entry.get("station_id") or ""),
                str(entry.get("issue_time_utc") or ""),
                str(entry.get("raw_taf_sha256") or ""),
            ))


def archive_rows(
    rows: Iterable[Any] | None,
    *,
    received_at: datetime,
    source_url: str,
    response_sha256: str | None = None,
) -> int:
    """Append each bulletin not archived yet; the count written, never raises."""

    try:
        directory = archive_dir()
        if directory is None:
            return 0
        received = received_at.astimezone(timezone.utc)
        path = directory / f"{received:%Y-%m-%d}.jsonl"
        if len(_SEEN) > MAX_SEEN_KEYS:
            _SEEN.clear()
            _SEEDED.clear()
        _seed(directory / f"{received - timedelta(days=1):%Y-%m-%d}.jsonl")
        _seed(path)
        lines = []
        fresh: set[tuple[str, str, str]] = set()
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            key = _key(row)
            if key is None or key in _SEEN or key in fresh:
                continue
            fresh.add(key)
            lines.append(json.dumps({
                "schema": SCHEMA,
                "station_id": key[0],
                "issue_time_utc": key[1],
                "db_pop_time_utc": row.get("dbPopTime"),
                "raw_taf_sha256": key[2],
                "raw_taf": str(row.get("rawTAF") or "").strip(),
                "received_at_utc": received.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "source_url": source_url,
                "response_sha256": response_sha256,
                "awc_row": dict(row),
            }, sort_keys=True, ensure_ascii=False))
        if lines:
            directory.mkdir(parents=True, exist_ok=True)
            # One write per round in append mode: lines from two processes
            # interleave whole, never torn.
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            # Marked seen only once on disk, so a failed write is retried
            # the next time the bulletin comes round.
            _SEEN.update(fresh)
            STATS["written"] += len(lines)
        return len(lines)
    except Exception:  # noqa: BLE001 -- archiving never fails a capture
        STATS["errors"] += 1
        return 0


__all__ = ["ENV", "SCHEMA", "STATS", "archive_dir", "archive_rows"]
