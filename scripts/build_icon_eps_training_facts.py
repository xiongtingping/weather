#!/usr/bin/env python3
"""Build offline ICON Global/EPS closed-day training facts from SSOT AWC truth."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.icon_eps_training_facts import (  # noqa: E402
    build_icon_eps_training_facts,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)


DEFAULT_ARCHIVE = ROOT / "output/audits/icon_global_eps_daily_members_20260802.json"
DEFAULT_DB = ROOT / "data/bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output/audits/icon_global_eps_training_facts_20260802.json"
MAX_STATIONS = expected_station_count()


def _utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("icon_eps_training_cli_as_of_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("icon_eps_training_cli_as_of_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _archive_scope(archive: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    event_date = str(archive.get("event_date") or "").strip()
    captures = archive.get("captures")
    if not event_date or not isinstance(captures, list) or len(captures) != MAX_STATIONS:
        raise ContractError("icon_eps_training_cli_archive_scope_invalid")
    timezones: dict[str, str] = {}
    for capture in captures:
        if not isinstance(capture, Mapping):
            raise ContractError("icon_eps_training_cli_archive_capture_invalid")
        station = str(capture.get("station_id") or "").strip().upper()
        timezone_name = str(capture.get("settlement_timezone") or "").strip()
        if not station or not timezone_name or station in timezones:
            raise ContractError("icon_eps_training_cli_archive_scope_invalid")
        timezones[station] = timezone_name
    return event_date, timezones


def read_ssot_awc_closed_truths(
    db_path: Path,
    *,
    event_date: str,
    station_timezones: Mapping[str, str],
    as_of_utc: str,
) -> list[dict[str, Any]]:
    """Read only latest formal AWC truth revisions known at ``as_of_utc``."""

    if not station_timezones or len(station_timezones) > MAX_STATIONS:
        raise ContractError("icon_eps_training_cli_station_scope_invalid")
    # SQLite parameters are used for every dynamic value; truth is never
    # materialized or repaired here.
    stations = sorted(station_timezones)
    marks = ",".join("?" for _ in stations)
    query = f"""
        WITH ranked AS (
          SELECT truth_id,station_id,local_date,official_tmax_c,source_lane,
                 settlement_definition,canonical_committed_at_utc,qc_status,
                 authority_scope,content_sha256,revision_no,
                 ROW_NUMBER() OVER (
                   PARTITION BY station_id,local_date
                   ORDER BY revision_no DESC,canonical_committed_at_utc DESC,truth_id DESC
                 ) AS rank_no
          FROM calibration_station_daily_high_truth
          WHERE station_id IN ({marks})
            AND local_date=?
            AND source_lane='AWC'
            AND settlement_definition='official_daily_tmax_c'
            AND qc_status='ACCEPTED'
            AND authority_scope='FORMAL_NATIVE_ELIGIBLE'
            AND julianday(canonical_committed_at_utc)<=julianday(?)
        )
        SELECT truth_id,station_id,local_date,official_tmax_c,source_lane,
               settlement_definition,canonical_committed_at_utc,qc_status,
               authority_scope,content_sha256
        FROM ranked WHERE rank_no=1 ORDER BY station_id
    """
    resolved = db_path.expanduser().resolve(strict=True)
    uri = f"file:{resolved}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30.0) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(query, (*stations, event_date, as_of_utc)).fetchall()
    truths: list[dict[str, Any]] = []
    for row in rows:
        station = str(row[1]).strip().upper()
        timezone_name = station_timezones.get(station)
        if timezone_name is None:
            raise ContractError("icon_eps_training_cli_truth_scope_invalid")
        truths.append({
            "truth_id": str(row[0]),
            "station_id": station,
            "local_date": str(row[2]),
            "official_tmax_c": row[3],
            "source_lane": str(row[4]),
            "settlement_definition": str(row[5]),
            "canonical_committed_at_utc": str(row[6]),
            "qc_status": str(row[7]),
            "authority_scope": str(row[8]),
            "content_sha256": str(row[9]),
            "settlement_timezone": timezone_name,
        })
    return truths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--as-of-utc",
        default=datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    )
    args = parser.parse_args()
    as_of = _utc(args.as_of_utc)
    archive = load_bounded_json(args.archive.expanduser().resolve(strict=True))
    if not isinstance(archive, Mapping):
        raise ContractError("icon_eps_training_cli_archive_invalid")
    event_date, station_timezones = _archive_scope(archive)
    truths = read_ssot_awc_closed_truths(
        args.db,
        event_date=event_date,
        station_timezones=station_timezones,
        as_of_utc=as_of,
    )
    result = build_icon_eps_training_facts(archive, truths, as_of_utc=as_of)
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, result)
    print(json.dumps({
        "state": result["state"],
        "fact_count": result["fact_count"],
        "unresolved_station_day_count": len(result["unresolved_station_days"]),
        "output": str(output),
        "wrote": wrote,
        "writes_target_database": False,
        "downloads_raw": False,
        "formal_p0_eligible": False,
        "orders_submitted": 0,
        "content_sha256": result["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
