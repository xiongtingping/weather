#!/usr/bin/env python3
"""Build bounded same-run alignment evidence from retained official raw.

This is an offline research receipt.  It proves only that the three official
providers share a station/day/run key.  It does not turn a sparse ICON point
into a daily curve, does not promote calibration, and never writes SQLite or
calls a provider, service, or order gateway.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import MODEL_PROVIDERS, TARGET_TIMEZONE, event_day_window  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    MAX_TARGET_PROCESS_RSS_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)


SCHEMA = "bayes_basket_target_same_run_alignment_evidence_v1"
HISTORICAL_NATIVE_SCHEMA = "bayes_basket_target_historical_native_rebuild_evidence_v1"
ICON_RETAINED_SCHEMA = "bayes_basket_target_icon_retained_raw_rebuild_evidence_v1"
ET_HISTORY_SCHEMA = "bayes_basket_target_et_history_evidence_v1"
EXPECTED_CURVE_POINTS = 5
MAX_ROWS_PER_INPUT = 20_000
MAX_KEYS = 2_000
DEFAULT_ECMWF = ROOT / "output/audits/bayes_basket_target_historical_ecmwf_20260714_23_s3_fallback_v2.json"
DEFAULT_GFS = ROOT / "output/audits/bayes_basket_target_historical_native_rebuild_evidence.json"
DEFAULT_ICON = ROOT / "output/audits/bayes_basket_target_icon_retained_raw_rebuild_evidence.json"
DEFAULT_TRUTH = ROOT / "output/audits/bayes_basket_target_et_history_evidence.json"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_same_run_alignment_evidence.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    value = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise ContractError("alignment_input_mapping_required")
    result = dict(value)
    result["_path"] = str(resolved)
    result["_sha256"] = _sha256_file(resolved)
    return result


def _historical_rows(payload: Mapping[str, Any], *, provider_id: str) -> list[dict[str, Any]]:
    schema = str(payload.get("schema") or "")
    if provider_id == "icon_global_official":
        if schema != ICON_RETAINED_SCHEMA:
            raise ContractError("icon_alignment_schema_invalid")
        raw_rows = payload.get("model_points")
        default_reconstructed = str(payload.get("reconstructed_at_utc") or "")
    else:
        if schema != HISTORICAL_NATIVE_SCHEMA:
            raise ContractError("historical_alignment_schema_invalid")
        raw_rows = payload.get("model_rows")
        default_reconstructed = str(payload.get("reconstructed_at_utc") or "")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ContractError("alignment_model_rows_missing")
    if len(raw_rows) > MAX_ROWS_PER_INPUT:
        raise ContractError("alignment_model_rows_exceed_bound")
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ContractError("alignment_model_row_invalid")
        row = dict(raw)
        row["provider_id"] = provider_id
        row["station_id"] = str(row.get("station_id") or "").strip().upper()
        row["event_date"] = str(row.get("event_date") or "").strip()
        row["target_timezone"] = str(row.get("target_timezone") or "").strip()
        row["model_run_initialization_utc"] = str(row.get("model_run_initialization_utc") or "").strip()
        row["valid_time_utc"] = str(row.get("valid_time_utc") or "").strip()
        row["raw_message_sha256"] = str(row.get("raw_message_sha256") or "").strip().lower()
        row["receipt_sha256"] = str(row.get("receipt_sha256") or "").strip().lower()
        row["raw_object_ref"] = str(row.get("raw_object_ref") or "").strip()
        row["published_at_utc"] = str(row.get("published_at_utc") or "").strip()
        row["reconstructed_at_utc"] = str(row.get("reconstructed_at_utc") or default_reconstructed).strip()
        if not all(row[key] for key in (
            "station_id", "event_date", "target_timezone", "model_run_initialization_utc",
            "valid_time_utc", "raw_message_sha256", "receipt_sha256", "raw_object_ref",
        )):
            continue
        rows.append(row)
    return rows


def _truth_index(payload: Mapping[str, Any]) -> tuple[dict[tuple[str, str], dict[str, Any]], str]:
    if payload.get("schema") != ET_HISTORY_SCHEMA:
        raise ContractError("alignment_truth_schema_invalid")
    raw_rows = payload.get("truth_rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > MAX_ROWS_PER_INPUT:
        raise ContractError("alignment_truth_rows_invalid")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            continue
        key = (str(raw.get("station_id") or "").strip().upper(), str(raw.get("event_date") or "").strip())
        if all(key):
            result[key] = {
                "truth_id": str(raw.get("truth_id") or ""),
                "official_tmax_c": raw.get("official_tmax_c"),
                "truth_content_sha256": str(raw.get("content_sha256") or ""),
            }
    return result, str(payload.get("_sha256") or "")


def _raw_integrity(rows: list[dict[str, Any]], cache: dict[str, tuple[bool, str]]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    for row in rows:
        ref = Path(row["raw_object_ref"]).expanduser()
        if str(ref) not in cache:
            if not ref.is_file():
                cache[str(ref)] = (False, "missing")
            else:
                try:
                    digest = _sha256_file(ref)
                    cache[str(ref)] = (digest == row["raw_message_sha256"], digest)
                except OSError:
                    cache[str(ref)] = (False, "unreadable")
        valid, detail = cache[str(ref)]
        if not valid:
            blockers.append(f"raw_integrity:{detail}")
    return not blockers, sorted(set(blockers))


def build(*, ecmwf_path: Path, gfs_path: Path, icon_path: Path, truth_path: Path, as_of_utc: str, event_dates: set[str] | None = None) -> dict[str, Any]:
    assert_process_memory_budget(limit=MAX_TARGET_PROCESS_RSS_BYTES)
    sources = {
        "ecmwf_ifs025_official": _load(ecmwf_path),
        "gfs_0p25_official": _load(gfs_path),
        "icon_global_official": _load(icon_path),
    }
    truth_payload = _load(truth_path)
    truth_by_key, truth_sha = _truth_index(truth_payload)
    grouped: dict[str, dict[tuple[str, str, str], list[dict[str, Any]]]] = {}
    quarantined_non_closed_event_dates: set[str] = set()
    for provider, payload in sources.items():
        rows = _historical_rows(payload, provider_id=provider)
        grouped[provider] = defaultdict(list)
        for row in rows:
            if event_dates and row["event_date"] not in event_dates:
                continue
            if row["target_timezone"] != TARGET_TIMEZONE:
                continue
            window = event_day_window(row["event_date"], as_of_utc=as_of_utc)
            if window["state"] != "closed":
                quarantined_non_closed_event_dates.add(row["event_date"])
                continue
            grouped[provider][(row["station_id"], row["event_date"], row["model_run_initialization_utc"])].append(row)
    key_sets = {provider: set(items) for provider, items in grouped.items()}
    common_keys = sorted(set.intersection(*(key_sets[provider] for provider in MODEL_PROVIDERS)))
    if len(common_keys) > MAX_KEYS:
        raise ContractError("alignment_key_count_exceeds_bound")
    raw_cache: dict[str, tuple[bool, str]] = {}
    rows: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    for station_id, event_date, run_init in common_keys:
        window = event_day_window(event_date, as_of_utc=as_of_utc)
        provider_summary: dict[str, dict[str, Any]] = {}
        blockers: set[str] = set()
        for provider in sorted(MODEL_PROVIDERS):
            items = grouped[provider][(station_id, event_date, run_init)]
            valid_times = sorted({item["valid_time_utc"] for item in items})
            raw_ok, raw_blockers = _raw_integrity(items, raw_cache)
            published_complete = all(item["published_at_utc"] for item in items)
            if len(valid_times) != EXPECTED_CURVE_POINTS:
                blockers.add(f"{provider}:curve_incomplete")
            if not published_complete:
                blockers.add(f"{provider}:published_at_missing")
            if not raw_ok:
                blockers.update(f"{provider}:{item}" for item in raw_blockers)
            provider_summary[provider] = {
                "point_count": len(items),
                "unique_valid_time_count": len(valid_times),
                "valid_times_utc": valid_times,
                "published_at_complete": published_complete,
                "raw_integrity": raw_ok,
                "raw_message_sha256": sorted({item["raw_message_sha256"] for item in items}),
                "receipt_sha256": sorted({item["receipt_sha256"] for item in items}),
                "capture_modes": sorted({str(item.get("capture_mode") or "historical_rebuild") for item in items}),
                "production_calibration_eligible": False,
            }
        truth = truth_by_key.get((station_id, event_date))
        truth_available = truth is not None
        if not truth_available:
            blockers.add("truth_missing")
        if window["state"] != "closed":
            blockers.add(f"event_day_{window['state']}")
        for reason in blockers:
            reason_counts[reason] += 1
        rows.append({
            "station_id": station_id,
            "event_date": event_date,
            "run_initialization_utc": run_init,
            "target_timezone": TARGET_TIMEZONE,
            "event_day_state": window["state"],
            "same_run_aligned": True,
            "truth_available": truth_available,
            "truth": truth,
            "providers": provider_summary,
            "complete_three_model_curves": not any(
                provider_summary[provider]["unique_valid_time_count"] != EXPECTED_CURVE_POINTS
                for provider in MODEL_PROVIDERS
            ),
            "strict_asof_join_ready": not blockers,
            "production_calibration_eligible": False,
            "blockers": sorted(blockers),
        })
    by_date: dict[str, dict[str, int]] = defaultdict(lambda: {"same_run_keys": 0, "truth_keys": 0, "curve_complete_keys": 0, "strict_asof_ready_keys": 0})
    for row in rows:
        summary = by_date[row["event_date"]]
        summary["same_run_keys"] += 1
        summary["truth_keys"] += int(row["truth_available"])
        summary["curve_complete_keys"] += int(row["complete_three_model_curves"])
        summary["strict_asof_ready_keys"] += int(row["strict_asof_join_ready"])
    provider_key_counts = {
        provider: len(key_sets[provider])
        for provider in sorted(MODEL_PROVIDERS)
    }
    missing_common_provider_keys = {
        provider: len(set.union(*(key_sets[other] for other in MODEL_PROVIDERS)) - key_sets[provider])
        for provider in sorted(MODEL_PROVIDERS)
    }
    if not common_keys:
        reason_counts["three_model_exact_run_key_intersection_empty"] += 1
    core = {
        "schema": SCHEMA,
        "generated_at_utc": _now(),
        "as_of_utc": as_of_utc,
        "target_timezone": TARGET_TIMEZONE,
        "event_dates": sorted({row["event_date"] for row in rows}),
        "source_inputs": {
            provider: {"path": payload["_path"], "sha256": payload["_sha256"]}
            for provider, payload in sources.items()
        } | {"truth_evidence": {"path": truth_payload["_path"], "sha256": truth_sha}},
        "counts": {
            "same_run_alignment_keys": len(rows),
            "truth_covered_keys": sum(int(row["truth_available"]) for row in rows),
            "complete_three_model_curve_keys": sum(int(row["complete_three_model_curves"]) for row in rows),
            "strict_asof_ready_keys": sum(int(row["strict_asof_join_ready"]) for row in rows),
        },
        "by_event_date": {key: by_date[key] for key in sorted(by_date)},
        "reason_counts": dict(sorted(reason_counts.items())),
        "provider_key_counts": provider_key_counts,
        "missing_common_provider_key_counts": missing_common_provider_keys,
        "quarantined_non_closed_event_dates": sorted(quarantined_non_closed_event_dates),
        "quarantined_non_closed_row_count": sum(
            1
            for provider, payload in sources.items()
            for row in _historical_rows(payload, provider_id=provider)
            if row["event_date"] in quarantined_non_closed_event_dates
        ),
        "alignments": rows,
        "boundary": {
            "historical_rebuild_only": True,
            "same_run_alignment_is_not_curve_completion": True,
            "production_calibration_allowed": False,
            "promotion_allowed": False,
            "writes_target_db": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**core, "evidence_sha256": content_sha256(core)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ecmwf", type=Path, default=DEFAULT_ECMWF)
    parser.add_argument("--gfs", type=Path, default=DEFAULT_GFS)
    parser.add_argument("--icon", type=Path, default=DEFAULT_ICON)
    parser.add_argument("--truth-evidence", type=Path, default=DEFAULT_TRUTH)
    parser.add_argument("--event-date", action="append")
    parser.add_argument("--as-of-utc", default="2026-07-25T12:00:00Z")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    evidence = build(
        ecmwf_path=args.ecmwf,
        gfs_path=args.gfs,
        icon_path=args.icon,
        truth_path=args.truth_evidence,
        as_of_utc=args.as_of_utc,
        event_dates={str(item).strip() for item in (args.event_date or []) if str(item).strip()} or None,
    )
    write_bounded_json(args.output.expanduser().resolve(), evidence, limit=MAX_JSON_OUTPUT_BYTES)
    print(json.dumps({
        "schema": SCHEMA,
        "output": str(args.output.expanduser().resolve()),
        "counts": evidence["counts"],
        "reason_counts": evidence["reason_counts"],
        "promotion_allowed": False,
        "writes_target_db": False,
        "calls_order_gateway": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
