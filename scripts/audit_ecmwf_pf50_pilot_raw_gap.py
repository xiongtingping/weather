#!/usr/bin/env python3
"""Build the one PF50 pilot gap ledger without network access."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.ecmwf_pf50_raw_reuse import (  # noqa: E402
    SYSTEM_ID,
    find_content_addressed_message,
    load_retained_field_index,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)


HORIZONS = (36, 39, 42, 45, 48, 51, 54, 57)
MEMBERS = tuple(f"perturbed_{number:02d}" for number in range(1, 51))


def _days(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first or (last - first).days >= 64:
        raise ContractError("ecmwf_pf50_gap_date_range_invalid")
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def build_gap_ledger(
    *,
    start_date: str,
    end_date: str,
    retained_packet_dir: Path,
    pilot_packet_dir: Path,
    raw_roots: tuple[Path, ...],
) -> dict[str, object]:
    fields = load_retained_field_index(
        retained_packet_dir.glob("ECMWF_PF50_45station_*_official_d2.json")
    )
    rows: list[dict[str, object]] = []
    ready_days = 0
    receipt_ready_raw_missing = 0
    receipt_missing = 0
    raw_ready = 0
    for event_date in _days(start_date, end_date):
        compact = event_date.replace("-", "")
        pilot_path = pilot_packet_dir / f"ECMWF_PF50_{compact}_ET.json"
        if pilot_path.is_file():
            packet = load_bounded_json(pilot_path)
            if (
                not isinstance(packet, dict)
                or packet.get("event_date") != event_date
                or packet.get("station_ids") != ["CYYZ", "RJTT", "RKSI"]
                or packet.get("union_forecast_steps") != list(HORIZONS)
            ):
                raise ContractError("ecmwf_pf50_gap_existing_pilot_invalid")
            supplied = str(packet.get("content_sha256") or "")
            identity = {key: value for key, value in packet.items() if key != "content_sha256"}
            if supplied != content_sha256(identity):
                raise ContractError("ecmwf_pf50_gap_existing_pilot_sha_mismatch")
            ready_days += 1
            rows.append({
                "event_date": event_date,
                "state": "CANONICAL_ET_PACKET_READY",
                "pilot_packet_ref": str(pilot_path.resolve()),
                "pilot_packet_content_sha256": supplied,
                "network_required": False,
                "field_gaps": [],
            })
            continue
        run_init = datetime.combine(
            date.fromisoformat(event_date) - timedelta(days=2),
            datetime.min.time().replace(hour=18),
            timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        gaps: list[dict[str, object]] = []
        day_raw_ready = 0
        for member_id in MEMBERS:
            for step in HORIZONS:
                field = fields.get((SYSTEM_ID, run_init, member_id, step))
                if field is None:
                    receipt_missing += 1
                    gaps.append({
                        "member_id": member_id,
                        "forecast_step": step,
                        "state": "RETAINED_RECEIPT_MISSING",
                    })
                    continue
                local = find_content_addressed_message(
                    field.message_sha256, raw_roots
                )
                if local is None:
                    receipt_ready_raw_missing += 1
                    gaps.append({
                        "member_id": member_id,
                        "forecast_step": step,
                        "message_sha256": field.message_sha256,
                        "object_key": field.receipt.get("object_key"),
                        "range_start": field.receipt.get("range_start"),
                        "range_end": field.receipt.get("range_end"),
                        "state": "RETAINED_RECEIPT_RAW_BYTES_MISSING",
                    })
                else:
                    raw_ready += 1
                    day_raw_ready += 1
        rows.append({
            "event_date": event_date,
            "state": "RAW_GAPS_PRESENT" if gaps else "RAW_OBJECTS_READY_FOR_ET_PROJECTION",
            "retained_raw_ready_count": day_raw_ready,
            "field_gap_count": len(gaps),
            "field_gaps": gaps,
            "network_required": bool(gaps),
        })
    identity = {
        "schema": "ecmwf_pf50_pilot_raw_gap_ledger_v1",
        "ensemble_system_id": SYSTEM_ID,
        "station_ids": ["CYYZ", "RJTT", "RKSI"],
        "settlement_timezone": "America/New_York",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "start_date": start_date,
        "end_date": end_date,
        "expected_member_count": len(MEMBERS),
        "expected_forecast_steps": list(HORIZONS),
        "canonical_et_packet_ready_day_count": ready_days,
        "retained_raw_ready_field_count": raw_ready,
        "retained_receipt_raw_bytes_missing_count": receipt_ready_raw_missing,
        "retained_receipt_missing_count": receipt_missing,
        "days": rows,
        "network_policy": "FETCH_ONLY_RETAINED_RECEIPT_RAW_BYTES_MISSING_OR_RECEIPT_MISSING",
        "writes_ssot": False,
        "calls_network": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2026-07-02")
    parser.add_argument("--end-date", default="2026-07-29")
    parser.add_argument(
        "--retained-packet-dir",
        type=Path,
        default=ROOT / "output/audits/native_ensemble_history",
    )
    parser.add_argument(
        "--pilot-packet-dir",
        type=Path,
        default=ROOT / "output/audits/native_ensemble_pilot_et/ecmwf",
    )
    parser.add_argument(
        "--raw-root",
        action="append",
        type=Path,
        default=[],
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    roots = tuple(args.raw_root) or (
        ROOT / "data/runtime/official_nwp_sources/ecmwf_pf50/raw",
        ROOT / "data/runtime/official_nwp_sources/ecmwf/raw",
        ROOT / "output/audits/bayes_basket_target_45station_native_history/ecmwf_18z_d2_objects/ecmwf_ifs025_official/raw",
    )
    ledger = build_gap_ledger(
        start_date=args.start_date,
        end_date=args.end_date,
        retained_packet_dir=args.retained_packet_dir.resolve(),
        pilot_packet_dir=args.pilot_packet_dir.resolve(),
        raw_roots=tuple(path.resolve() for path in roots),
    )
    write_bounded_json_if_changed(args.output.resolve(), ledger)
    print(json.dumps({
        key: ledger[key]
        for key in (
            "canonical_et_packet_ready_day_count",
            "retained_raw_ready_field_count",
            "retained_receipt_raw_bytes_missing_count",
            "retained_receipt_missing_count",
            "calls_network",
            "content_sha256",
        )
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
