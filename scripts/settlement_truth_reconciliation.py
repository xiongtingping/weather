#!/usr/bin/env python3
"""Charter 207: reconcile our settled buckets against what the market paid.

A reader.  It asks Gamma for the fleet's highest-temperature events over the
last ``--days`` event dates, reads the standing truth labels from the SSOT
through a read-only connection held for a couple of seconds, and writes

* ``data/settlement_truth_reconciliation_status.json`` -- this pass: counts
  by verdict, the disagreements, the window; what the board reads; and
* ``data/settlement_truth_reconciliation_history.jsonl`` -- one line per
  disagreement the first time it is seen (and again if its verdict or either
  bucket changes), so a disagreement that later heals still has a record.

It never writes the SSOT, a truth, a label, an order or a registry row, so
it runs outside the resident's writer rotation (single-writer rule): a pass
that holds nothing cannot starve anything.

    python3 scripts/settlement_truth_reconciliation.py            # write status
    python3 scripts/settlement_truth_reconciliation.py --days 40  # wider window
    python3 scripts/settlement_truth_reconciliation.py --dry-run  # print only
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.candidate_window_evidence import standing_labels  # noqa: E402
from bayes_basket_target.settlement_reconciliation import (  # noqa: E402
    SCHEMA,
    reconcile,
    summarize,
)
from reported_trajectory_market_registry import (  # noqa: E402
    fetch_gamma_events_window,
    load_config,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATUS_JSON = ROOT / "data" / "settlement_truth_reconciliation_status.json"
HISTORY_JSONL = ROOT / "data" / "settlement_truth_reconciliation_history.jsonl"
# Gamma's tag listing returns at most four 100-row pages a call; three event
# dates of ~51 cities fit inside that with room to spare.
GAMMA_DAYS_PER_CALL = 3
MAX_DAYS = 45


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _events(start: date, end: date) -> list[dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    day = start
    while day <= end:
        last = min(end, day + timedelta(days=GAMMA_DAYS_PER_CALL - 1))
        for event in fetch_gamma_events_window(start_date=day, end_date=last, max_pages=4):
            events[str(event.get("slug") or "")] = event
        day = last + timedelta(days=1)
    return [events[slug] for slug in sorted(events) if slug]


def _standing(db: Path, floor: str) -> tuple[dict[tuple[str, str], dict[str, Any]], Any]:
    """Standing labels, plus a reader for one truth's evidence document."""

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10.0)
    rows, _counts = standing_labels(conn, floor_event_date=floor, only_unlabelled=False)
    ours = {
        (str(row[0]), str(row[1])): {
            "revision": int(row[2]), "bucket": str(row[3]), "tmax_c": float(row[4]),
        }
        for row in rows
    }

    def evidence_of(station: str, day: str) -> Any:
        found = conn.execute(
            "SELECT observation_ids_json FROM calibration_station_daily_high_truth"
            " WHERE station_id=? AND local_date=? AND qc_status='ACCEPTED'"
            " ORDER BY revision_no DESC LIMIT 1",
            (station, day),
        ).fetchone()
        try:
            return json.loads(found[0]) if found else None
        except (TypeError, ValueError):
            return None

    return ours, (conn, evidence_of)


def _append_history(path: Path, disagreements: list[dict[str, Any]], *, checked_at: str) -> int:
    seen: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["key"])
            except (ValueError, KeyError, TypeError):
                continue
    fresh = []
    for row in disagreements:
        key = "|".join(str(row.get(field)) for field in (
            "station_id", "event_date", "verdict", "our_bucket", "market_bucket", "our_revision",
        ))
        if key not in seen:
            fresh.append({"key": key, "first_seen_at": checked_at, **row})
    if fresh:
        with path.open("a", encoding="utf-8") as handle:
            for row in fresh:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(fresh)


def run(*, days: int, db: Path, status_path: Path, history_path: Path, dry_run: bool) -> dict[str, Any]:
    days = max(1, min(MAX_DAYS, int(days)))
    today = datetime.now(timezone.utc).date()
    start, end = today - timedelta(days=days), today - timedelta(days=1)
    config = load_config()
    cities = config["settlement_source_contract"]["cities"]
    city_stations = {city: str(entry["station_id"]) for city, entry in cities.items()}
    events = _events(start, end)
    ours, (conn, evidence_of) = _standing(db, start.isoformat())
    try:
        rows = reconcile(
            events=events, ours=ours, city_stations=city_stations, evidence_of=evidence_of,
        )
    finally:
        conn.close()
    checked_at = _now()
    summary = summarize(rows)
    status = {
        "schema": SCHEMA,
        "checked_at": checked_at,
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "event_count": len(events),
        "station_day_count": len(rows),
        **summary,
        "writes_ssot": False,
        "calls_order_gateway": False,
    }
    if dry_run:
        return status
    status["history_appended"] = _append_history(
        history_path, summary["disagreements"], checked_at=checked_at,
    )
    temporary = status_path.with_name(f".{status_path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temporary, status_path)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--status-out", type=Path, default=STATUS_JSON)
    parser.add_argument("--history", type=Path, default=HISTORY_JSONL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    status = run(
        days=args.days, db=args.db, status_path=args.status_out,
        history_path=args.history, dry_run=args.dry_run,
    )
    print(json.dumps(
        {key: status[key] for key in ("checked_at", "window", "counts", "agree_share")}
        | {"disagreement_count": len(status["disagreements"])},
        ensure_ascii=False, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
