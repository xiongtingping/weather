"""Station-days whose settled truth cannot be trusted and cannot be repaired.

A handful of daily highs were published by a retired ingest path before the
local day could have produced them -- KSFO 2026-08-01 was settled at 05:28
local from three observations, recording 14C on a day the airport went on to
report 23C.  They are wrong by 3 to 10 degrees and they are all wrong in the
same direction, which is what a partial morning always looks like.

They are excluded rather than corrected.  The canonical facts behind them were
never written (the retired path did not write any), so appending a projection
revision would mean inventing a provenance to justify a number -- a worse
defect than the one being fixed, and one the settlement contract already
refuses by raising ``scope_content_conflict``.  Their sibling defects that
*do* have canonical facts were repaired the ordinary way, by appending a
revision through the settlement materializer.

Reading, not writing, is where this belongs: the rows stay byte-identical and
remain readable as history, and consumers that score models against truth skip
them.  Six station-days out of 1716 is a rounding error in coverage and a
measurable distortion in calibration -- excluding them moved the |z|>2 rate
from 4.4% to 2.3% against a 4.6% expectation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import ContractError


SCHEMA = "retired_ingest_truth_exclusions_v1"
DEFAULT_EXCLUSIONS_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "trading"
    / "retired_ingest_truth_exclusions_v1.json"
)
_CACHE: dict[str, frozenset[tuple[str, str]]] = {}


def load_truth_exclusions(
    path: Path | str | None = None,
) -> frozenset[tuple[str, str]]:
    """``(station_id, event_date)`` pairs that must not be scored against.

    A missing file is an empty exclusion set, not an error: the list is a
    record of specific known-bad history, so a deployment without it should
    train on everything rather than refuse to train.
    """

    resolved = Path(path) if path is not None else DEFAULT_EXCLUSIONS_PATH
    key = str(resolved)
    if key in _CACHE:
        return _CACHE[key]
    if not resolved.is_file():
        _CACHE[key] = frozenset()
        return _CACHE[key]
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA:
        raise ContractError("truth_exclusions_schema_invalid")
    pairs: set[tuple[str, str]] = set()
    for row in raw.get("excluded_station_days") or ():
        if not isinstance(row, Mapping):
            raise ContractError("truth_exclusions_row_invalid")
        station = str(row.get("station_id") or "").strip().upper()
        day = str(row.get("event_date") or "").strip()
        if not station or not day:
            raise ContractError("truth_exclusions_scope_invalid")
        pairs.add((station, day))
    _CACHE[key] = frozenset(pairs)
    return _CACHE[key]


def is_excluded(
    station_id: str,
    event_date: str,
    *,
    path: Path | str | None = None,
) -> bool:
    return (
        str(station_id or "").strip().upper(),
        str(event_date or "").strip(),
    ) in load_truth_exclusions(path)


def drop_excluded_truth_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    station_key: str = "station_id",
    date_key: str = "local_date",
    path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Filter truth rows, keeping the caller's own shape."""

    excluded = load_truth_exclusions(path)
    return [
        dict(row)
        for row in rows
        if (
            str(row.get(station_key) or "").strip().upper(),
            str(row.get(date_key) or "").strip(),
        )
        not in excluded
    ]


__all__ = [
    "DEFAULT_EXCLUSIONS_PATH",
    "SCHEMA",
    "drop_excluded_truth_rows",
    "is_excluded",
    "load_truth_exclusions",
]
