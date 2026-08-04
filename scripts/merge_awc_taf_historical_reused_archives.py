#!/usr/bin/env python3
"""Merge existing formal AWC historical TAF archives without refetching.

The historical capture producer writes one archive per station and date
window.  This small, offline merger makes one bounded window from already
retained archives, keeping only the current formal AWC lane.  Retired AMSC
or temporary calibration labels are deliberately ignored rather than
re-enveloped as AWC.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_awc_taf_calibration_dataset_v1"
SOURCE_CLASS = "official_date_addressed_historical_replay"
FORMAL_LABEL = "FORMAL_STATION_LANE_TRUTH"
AUTHORITY = {
    "station_source_lane": "AWC",
    "evidence_source_lane": "AWC",
    "truth_label_authority": FORMAL_LABEL,
    "reference_only": False,
    "enters_realtime_truth": False,
    "enters_p1": False,
    "enters_hard_no": False,
    "replacement_required_when_canonical_history_available": False,
}
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_STATIONS = 45


def _date(value: str, *, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"{name}_invalid") from exc


def _load_sources(directories: list[Path]) -> dict[str, list[tuple[int, Path, Mapping[str, Any]]]]:
    result: dict[str, list[tuple[int, Path, Mapping[str, Any]]]] = {}
    for priority, directory in enumerate(directories):
        directory = directory.expanduser().resolve(strict=True)
        if not directory.is_dir() or directory.is_symlink():
            raise RuntimeError("taf_reuse_source_dir_invalid")
        for source in sorted(directory.glob("AWC_TAF_*.json")):
            if source.is_symlink():
                raise RuntimeError("taf_reuse_source_symlink_forbidden")
            payload = load_bounded_json(source)
            if not isinstance(payload, Mapping):
                raise RuntimeError(f"taf_reuse_archive_mapping_required:{source}")
            if payload.get("schema") != SCHEMA:
                continue
            station = str(payload.get("station_id") or "").strip().upper()
            if not station:
                raise RuntimeError(f"taf_reuse_station_missing:{source}")
            result.setdefault(station, []).append((priority, source, payload))
    if not result or len(result) > MAX_STATIONS:
        raise RuntimeError("taf_reuse_station_scope_invalid")
    return result


def _is_formal_awc(payload: Mapping[str, Any]) -> bool:
    return (
        str(payload.get("source_class") or "") == SOURCE_CLASS
        and str(payload.get("source_lane") or "").strip().upper() == "AWC"
        and str(payload.get("truth_label_authority") or "") == FORMAL_LABEL
        and payload.get("truth_authority") == AUTHORITY
    )


def _event_date(item: Mapping[str, Any]) -> str:
    return str(item.get("event_date") or "").strip()


def _merge_station(
    station: str,
    sources: list[tuple[int, Path, Mapping[str, Any]]],
    *,
    start: date,
    end: date,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # Lower priority wins only when two retained archives contain the same
    # event date.  Callers pass overlay directories before broad archives.
    source_rows = [item for item in sources if _is_formal_awc(item[2])]
    if not source_rows:
        raise RuntimeError(f"taf_reuse_formal_awc_archive_missing:{station}")
    source_rows.sort(key=lambda item: (item[0], str(item[1])))
    selected: dict[str, tuple[int, Path, Mapping[str, Any]]] = {}
    selected_truth_only: dict[str, tuple[int, Path, Mapping[str, Any]]] = {}
    excluded: dict[tuple[str, str], dict[str, Any]] = {}
    blockers: dict[tuple[str, str], dict[str, Any]] = {}
    source_meta: list[dict[str, Any]] = []
    actual_response_bytes = 0
    for priority, source, payload in source_rows:
        source_meta.append({
            "path": str(source),
            "priority": priority,
            "content_sha256": str(payload.get("content_sha256") or ""),
            "sample_count": int(payload.get("sample_count") or 0),
            "truth_only_sample_count": int(payload.get("truth_only_sample_count") or 0),
        })
        actual_response_bytes += int(
            (payload.get("resource_bounds") or {}).get("actual_response_bytes") or 0
        )
        for key in ("samples", "truth_only_samples"):
            rows = payload.get(key, [])
            if not isinstance(rows, list):
                raise RuntimeError(f"taf_reuse_rows_invalid:{source}")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise RuntimeError(f"taf_reuse_row_invalid:{source}")
                event_date = _event_date(row)
                if not event_date:
                    raise RuntimeError(f"taf_reuse_event_date_missing:{source}")
                parsed = _date(event_date, name="taf_reuse_event_date")
                if parsed < start or parsed > end:
                    continue
                if key == "truth_only_samples":
                    # A truth-only sidecar is useful only when it retains the
                    # selected official TAF bulletin and response receipt.
                    # Older captures pre-dating the sidecar contract contain
                    # METAR truth but no TAF raw/receipt; carrying those rows
                    # would falsely suggest that the TAF gap was recoverable.
                    observation = row.get("taf_observation")
                    receipts = row.get("receipts")
                    if (
                        not isinstance(observation, Mapping)
                        or not str(observation.get("raw_taf") or "").strip()
                        or not isinstance(receipts, Mapping)
                        or not str(receipts.get("taf_response_sha256") or "").strip()
                    ):
                        continue
                if key == "samples":
                    taf = row.get("taf")
                    if isinstance(taf, Mapping):
                        issued = str(taf.get("issue_time_utc") or "")
                        available = str(taf.get("available_time_utc") or "")
                        # Keep the source archive pointer in the receipt, but
                        # do not carry an impossible provider timestamp into
                        # the strict historical ingress.  This is the same
                        # quarantine contract used by the capture producer.
                        if issued and available and issued > available:
                            excluded.setdefault((event_date, "historical_taf_strict_asof_violation"), {
                                "event_date": event_date,
                                "reason": "historical_taf_strict_asof_violation",
                                "classification": "official_taf_arrival_timestamp_invalid",
                            })
                            continue
                target = selected_truth_only if key == "truth_only_samples" else selected
                # Preserve an explicitly overlaid archive, never merge two
                # revisions for one station-day into an ambiguous sample.
                if event_date not in target or priority < target[event_date][0]:
                    target[event_date] = (priority, source, row)
        for key, target in (("excluded_samples", excluded), ("blockers", blockers)):
            rows = payload.get(key, [])
            if not isinstance(rows, list):
                raise RuntimeError(f"taf_reuse_{key}_invalid:{source}")
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                event_date = _event_date(row)
                if not event_date:
                    continue
                parsed = _date(event_date, name="taf_reuse_event_date")
                if parsed < start or parsed > end:
                    continue
                reason = str(row.get("reason") or "")
                target.setdefault((event_date, reason), dict(row))

    # A valid TAF sample is stronger than a truth-only sidecar for the same
    # station-day.  Keep the former and retain the sidecar's source pointer
    # in the merge receipt; do not manufacture a duplicate day in the output
    # archive.
    for event_date in set(selected) & set(selected_truth_only):
        selected_truth_only.pop(event_date, None)
    samples = [dict(selected[key][2]) for key in sorted(selected)]
    truth_only_samples = [
        dict(selected_truth_only[key][2])
        for key in sorted(selected_truth_only)
    ]
    # A successful sample supersedes an older exclusion/blocker for that day.
    done_dates = set(selected) | set(selected_truth_only)
    excluded_rows = [
        value for (event_date, _reason), value in sorted(excluded.items())
        if event_date not in done_dates
    ]
    blocker_rows = [
        value for (event_date, _reason), value in sorted(blockers.items())
        if event_date not in done_dates
    ]
    metadata = source_rows[0][2]
    identity = {
        "schema": SCHEMA,
        "provider": str(metadata.get("provider") or "NOAA Aviation Weather Center"),
        "source_class": SOURCE_CLASS,
        "station_id": station,
        "settlement_timezone": str(metadata.get("settlement_timezone") or ""),
        "source_lane": "AWC",
        "truth_label_authority": FORMAL_LABEL,
        "truth_authority": AUTHORITY,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "sample_count": len(samples),
        "samples": samples,
        "truth_only_sample_count": len(truth_only_samples),
        "truth_only_samples": truth_only_samples,
        "excluded_sample_count": len(excluded_rows),
        "excluded_samples": excluded_rows,
        "blockers": blocker_rows,
        "boundary": {
            "native_local_receipt": False,
            "official_awc_database_availability_preserved": True,
            "historical_replay_only": True,
            "truth_days_closed": True,
            "truth_only_samples_are_not_taf_samples": True,
            "truth_only_enters_p0m": False,
            "truth_only_writes_production_db": False,
            "reference_only": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
            "replacement_required_when_canonical_history_available": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
        "resource_bounds": {
            "actual_response_bytes": actual_response_bytes,
            "max_days": 90,
            "max_response_bytes": 2 * 1024 * 1024,
            "max_total_response_bytes": 256 * 1024 * 1024,
            "max_output_bytes": MAX_OUTPUT_BYTES,
        },
    }
    artifact = {**identity, "content_sha256": content_sha256(identity)}
    receipt = {
        "station_id": station,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "source_count": len(source_meta),
        "sources": source_meta,
        "selected_sample_count": len(samples),
        "selected_truth_only_sample_count": len(truth_only_samples),
        "selected_excluded_sample_count": len(excluded_rows),
        "network_fetches": 0,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return artifact, receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", action="append", type=Path, required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    start = _date(args.start_date, name="taf_reuse_start_date")
    end = _date(args.end_date, name="taf_reuse_end_date")
    if end < start or (end - start).days + 1 > 90:
        raise RuntimeError("taf_reuse_date_range_invalid")
    sources = _load_sources(args.source_dir)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    receipts: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for station in sorted(sources):
        try:
            artifact, receipt = _merge_station(
                station,
                sources[station],
                start=start,
                end=end,
            )
            output = output_dir / (
                f"AWC_TAF_{station}_{start.strftime('%Y%m%d')}_"
                f"{end.strftime('%Y%m%d')}.json"
            )
            write_bounded_json(output, artifact, limit=MAX_OUTPUT_BYTES)
            receipts.append({**receipt, "output": str(output), "content_sha256": artifact["content_sha256"]})
        except Exception as exc:
            failures.append({"station_id": station, "reason": f"{type(exc).__name__}:{exc}"})
    receipts.sort(key=lambda row: str(row["station_id"]))
    failures.sort(key=lambda row: str(row["station_id"]))
    result = {
        "schema": "awc_taf_historical_reuse_merge_receipt_v1",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "station_count": len(receipts),
        "failed_station_count": len(failures),
        "sample_count": sum(int(row["selected_sample_count"]) for row in receipts),
        "truth_only_sample_count": sum(int(row["selected_truth_only_sample_count"]) for row in receipts),
        "network_fetches": 0,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "outputs": receipts,
        "failures": failures,
    }
    receipt_path = args.receipt.expanduser().resolve()
    write_bounded_json(receipt_path, {**result, "content_sha256": content_sha256(result)}, limit=MAX_OUTPUT_BYTES)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
