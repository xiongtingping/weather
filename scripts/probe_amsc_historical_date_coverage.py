#!/usr/bin/env python3
"""Read-only AMSC H-retention/coverage probe using the schedule-bound contract.

Unlike the retired probe, report DDHHMMZ is bound to the queried physical
station-day window rather than to the current download date.  It never writes
raw reports, SQLite, facts, posterior state, or any trading input.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_historical_backfill_archive import historical_station_day_query_contract, prepare_amsc_historical_backfill  # noqa: E402
from scripts.capture_amsc_historical_backfill import fetch_historical_amsc_payload  # noqa: E402
from scripts.fetch_zuuu_metar_taf import token_candidates  # noqa: E402
from zuuu_metar_reporting_schedule import SCHEDULE_ID, expected_regular_metar_slots_utc  # noqa: E402


def _dates(values: list[str]) -> list[str]:
    results: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if ":" not in text:
            date.fromisoformat(text)
            results.add(text)
            continue
        start_text, end_text = (part.strip() for part in text.split(":", 1))
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
        if end < start or (end - start).days > 31:
            raise ValueError("event_date_range_invalid_or_exceeds_31_days")
        for offset in range((end - start).days + 1):
            results.add((start + timedelta(days=offset)).isoformat())
    return sorted(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event-date",
        action="append",
        required=True,
        help="Asia/Shanghai physical date or inclusive range YYYY-MM-DD:YYYY-MM-DD; repeatable, max 31 days per range",
    )
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        event_dates = _dates(args.event_date)
    except ValueError as exc:
        print(json.dumps({"schema": "amsc_historical_retention_probe_v2", "ok": False, "block_reasons": [str(exc)]}, ensure_ascii=False))
        return 2
    tokens = token_candidates()
    if not tokens:
        print(json.dumps({"schema": "amsc_historical_retention_probe_v2", "ok": False, "block_reasons": ["amsc_session_unavailable"]}, ensure_ascii=False))
        return 2
    results: list[dict] = []
    for event_date in event_dates:
        query = historical_station_day_query_contract(event_date=event_date)
        try:
            payload, received_at, transport = fetch_historical_amsc_payload(query=query, token=tokens[0], timeout=args.timeout)
            prepared = prepare_amsc_historical_backfill(
                payload=payload,
                query=query,
                query_received_at=received_at,
                query_transport=transport,
            )
            metar_slots = {
                str(item.get("verified_valid_time_utc") or "")
                for item in prepared.get("records") or []
                if str(item.get("report_type") or "").upper() == "METAR"
            }
            expected_slots = set(expected_regular_metar_slots_utc(event_date))
            results.append({
                "event_date": event_date,
                "response_code": payload.get("code"),
                "ready": prepared.get("ok") is True,
                "block_reasons": prepared.get("block_reasons") or [],
                "returned_metar_or_speci_count": prepared.get("returned_raw_count"),
                "returned_regular_metar_slot_count": len(metar_slots),
                "expected_regular_metar_slot_count": len(expected_slots),
                "missing_regular_metar_slot_count": len(expected_slots - metar_slots),
                "unexpected_regular_metar_slot_count": len(metar_slots - expected_slots),
                "schedule_complete": bool(expected_slots) and metar_slots == expected_slots,
                "transport_mode": (prepared.get("manifest_core") or {}).get("query_transport", {}).get("mode"),
            })
        except Exception as exc:
            results.append({
                "event_date": event_date,
                "ready": False,
                "block_reasons": [f"amsc_historical_probe_fetch_failed:{type(exc).__name__}"],
            })
    report = {
        "schema": "amsc_historical_retention_probe_v2",
        "ok": all(item.get("ready") is True for item in results),
        "station_id": "ZUUU",
        "reporting_schedule_id": SCHEDULE_ID,
        "results": results,
        "boundary": {
            "read_only": True,
            "credentials_persisted": False,
            "historical_data_class": "H",
            "creates_canonical_t1": False,
            "creates_p10_arrival_evidence": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
