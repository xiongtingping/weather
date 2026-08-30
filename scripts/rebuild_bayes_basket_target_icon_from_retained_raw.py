#!/usr/bin/env python3
"""Rebuild bounded target ICON points from retained official DWD raw bodies.

This is an offline, target-only repair.  It never downloads a provider, copies
raw bodies, writes SQLite, changes a production pointer, or calls the order
gateway.  Only retained official ICON ``t_2m`` bodies with an intact receipt
and SHA-256 identity are decoded.  Missing bodies remain explicit blockers;
they are never replaced by normalized ZUUU values or another provider.
"""

from __future__ import annotations

import argparse
import bz2
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import assert_event_day_closed  # noqa: E402
from bayes_basket_target.native_model_decode import (  # noqa: E402
    MAX_GRIB_GET_STDOUT_BYTES,
    MAX_DECOMPRESSED_PAYLOAD_BYTES,
    MIN_FREE_BYTES,
    materialize_native_daily_curves,
    parse_grib_get_temperature_output,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_INPUT_FILES_PER_BATCH,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.settlement_time import registry_scope_settlement_timezone  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402


SCHEMA = "bayes_basket_target_icon_retained_raw_rebuild_evidence_v1"
PROVIDER_ID = "icon_global_official"
DEFAULT_TIMEZONE_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_SOURCE_ROOTS = (
    ROOT / "data/runtime/official_nwp_sources/icon",
    ROOT / "data/runtime/official_nwp_curve_sources_v2/icon",
    # Target-only prospective captures become eligible historical evidence
    # after their settlement day closes. Keep the source root in the same
    # content-addressed, official-provider-only resolver so a valid target
    # body is not misreported as absent merely because it was captured by the
    # target arrival lane instead of the legacy resident collector.
    ROOT / "data/research/bayes_basket_target/model_arrivals/icon_global_official",
    # Historical bodies rehydrated from the exact official DWD URL remain
    # replay-only evidence.  The rehydrator marks their arrival time as
    # unobserved, so this root can repair retrospective curves without
    # authorizing strict-as-of calibration or production.
    ROOT / "data/research/bayes_basket_target/icon_history_rehydrated",
)
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_icon_retained_raw_rebuild_evidence.json"
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_RECEIPTS = 4096
MAX_BLOCKERS = 500
GRIB_GET = "/opt/homebrew/opt/eccodes/bin/grib_get"
GRIB_GET_DATA = "/opt/homebrew/opt/eccodes/bin/grib_get_data"
CDO = "/opt/homebrew/bin/cdo"
ICON_SUPPORT_ROOT = ROOT / "data/runtime/official_nwp_sources/icon/support/ICON_GLOBAL2WORLD_025_EASY"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> Mapping[str, Any]:
    if path.stat().st_size > MAX_RECEIPT_BYTES:
        raise ContractError("icon_retained_raw_receipt_too_large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ContractError("icon_retained_raw_receipt_mapping_required")
    return value


def _event_date(valid_time_utc: str, *, target_timezone: str) -> str:
    parsed = datetime.fromisoformat(str(valid_time_utc).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("icon_retained_raw_valid_time_timezone_required")
    return parsed.astimezone(ZoneInfo(target_timezone)).date().isoformat()


def _registry_timezone(
    path: Path,
    *,
    station_ids: Sequence[str],
    event_dates: Sequence[str],
) -> str:
    value = load_bounded_json(path.expanduser().resolve())
    rows = value.get("rows") if isinstance(value, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("icon_retained_raw_timezone_registry_rows_missing")
    return registry_scope_settlement_timezone(
        (row for row in rows if isinstance(row, Mapping)),
        station_ids=station_ids,
        event_dates=event_dates,
    )


def _publication_timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = parsedate_to_datetime(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("icon_retained_raw_publication_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _binary(path: str, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ContractError(f"icon_retained_raw_{label}_unavailable")
    return candidate


def _free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _collect_messages(
    *,
    source_roots: Sequence[Path],
    event_dates: set[str],
    target_timezone: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    messages: dict[tuple[str, str, str], dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    receipt_paths = 0
    raw_hash_cache: dict[str, dict[str, bool]] = {}
    receipt_hash_cache: dict[Path, str] = {}
    raw_alias_cache: dict[str, Path | None] = {}
    raw_alias_recovered: set[tuple[str, str, str]] = set()

    def resolve_retained_raw(raw_sha: str, original_ref: Path) -> tuple[Path | None, bool]:
        """Resolve a retained body by content address without copying it.

        Older target-only collectors wrote the same content-addressed object
        under different retained roots.  A receipt path may therefore be
        stale even though the exact official body still exists locally.  Only
        a filename candidate whose full SHA-256 matches is accepted; no
        provider or cross-source fallback is introduced.
        """

        if raw_sha in raw_alias_cache:
            resolved = raw_alias_cache[raw_sha]
            return resolved, resolved is not None and resolved != original_ref
        candidates = [original_ref]
        for source_root in source_roots:
            raw_dir = source_root / "raw"
            if not raw_dir.is_dir():
                continue
            candidates.extend(sorted(raw_dir.glob(f"{raw_sha}*")))
        seen_candidates: set[str] = set()
        for candidate in candidates:
            candidate = candidate.expanduser().resolve()
            if str(candidate) in seen_candidates or not candidate.is_file():
                continue
            seen_candidates.add(str(candidate))
            candidate_key = str(candidate)
            per_path = raw_hash_cache.setdefault(raw_sha, {})
            if candidate_key not in per_path:
                per_path[candidate_key] = _sha256(candidate) == raw_sha
            if per_path[candidate_key]:
                raw_alias_cache[raw_sha] = candidate
                return candidate, candidate != original_ref
        raw_alias_cache[raw_sha] = None
        return None, False

    for source_root in source_roots:
        receipt_dir = source_root / "receipts"
        paths = sorted(receipt_dir.glob("*.json")) if receipt_dir.is_dir() else []
        if len(paths) > MAX_RECEIPTS:
            invalid.append({"source_root": str(source_root), "reason": "receipt_file_cap_exceeded"})
            paths = paths[:MAX_RECEIPTS]
        for receipt_path in paths:
            receipt_paths += 1
            try:
                receipt = _load_json(receipt_path)
                # Target arrival receipts use the canonical run field.  The
                # older retained ZUUU collector used ``run_at_utc``; accept
                # it only as a backward-compatible lineage read so existing
                # retained evidence remains reconstructable.
                run = str(
                    receipt.get("run_initialization_utc")
                    or receipt.get("run_at_utc")
                    or ""
                ).strip()
                valid = str(receipt.get("valid_time_utc") or "").strip()
                if not run or not valid:
                    continue
                event_date = _event_date(valid, target_timezone=target_timezone)
                if event_dates and event_date not in event_dates:
                    continue
                receipt_sha = receipt_hash_cache.setdefault(receipt_path, _sha256(receipt_path))
                raw_receipts = receipt.get("raw_receipts")
                if not isinstance(raw_receipts, list):
                    continue
                for raw_receipt in raw_receipts:
                    if not isinstance(raw_receipt, Mapping):
                        continue
                    url = str(raw_receipt.get("raw_request_url") or "").strip()
                    if "/t_2m/" not in url:
                        continue
                    raw_sha = str(raw_receipt.get("raw_sha256") or "").strip().lower()
                    raw_ref = Path(str(raw_receipt.get("raw_object_ref") or "")).expanduser().resolve()
                    key = (run, valid, raw_sha)
                    resolved_ref, alias_used = resolve_retained_raw(raw_sha, raw_ref) if raw_sha else (None, False)
                    if resolved_ref is None:
                        missing.append({
                            "run_initialization_utc": run,
                            "valid_time_utc": valid,
                            "event_date": event_date,
                            "raw_sha256": raw_sha,
                            "raw_object_ref": str(raw_ref),
                            "source_url": url,
                            "receipt_sha256": receipt_sha,
                            "reason": "raw_body_missing_or_sha256_mismatch",
                        })
                        continue
                    if alias_used:
                        raw_alias_recovered.add((raw_sha, str(raw_ref), str(resolved_ref)))
                    raw_ref = resolved_ref
                    messages.setdefault(key, {
                        "provider_id": PROVIDER_ID,
                        "model_id": "ICON",
                        "event_date": event_date,
                        "run_initialization_utc": run,
                        "valid_time_utc": valid,
                        "raw_message_sha256": raw_sha,
                        "raw_object_ref": str(raw_ref),
                        "receipt_sha256": receipt_sha,
                        "source_url": url,
                        "published_at_utc": _publication_timestamp(
                            raw_receipt.get("response_headers", {}).get("last-modified")
                        ),
                        "received_at_utc": str(raw_receipt.get("response_completed_at") or ""),
                        "source_root": str(source_root),
                    })
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ContractError) as exc:
                invalid.append({"receipt_path": str(receipt_path), "reason": f"{type(exc).__name__}:{exc}"})
    return sorted(messages.values(), key=lambda row: (row["event_date"], row["valid_time_utc"], row["run_initialization_utc"])), {
        "receipt_paths_scanned": receipt_paths,
        "unique_retained_t2m_messages": len(messages),
        "missing_or_mismatched_raw_count": len(missing),
        "missing_or_mismatched_raw_sample": missing[:MAX_BLOCKERS],
        "raw_alias_recovered_count": len(raw_alias_recovered),
        "raw_alias_recovered_sample": [
            {"raw_sha256": sha, "receipt_ref": old, "resolved_ref": new}
            for sha, old, new in sorted(raw_alias_recovered)[:MAX_BLOCKERS]
        ],
        "invalid_receipt_count": len(invalid),
        "invalid_receipt_sample": invalid[:MAX_BLOCKERS],
    }


def _decode_message(
    message: Mapping[str, Any],
    *,
    stations: Sequence[str],
    grib_get: Path,
    grib_get_data: Path,
    cdo: Path,
    target_timezone: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_path = Path(str(message["raw_object_ref"])).resolve()
    if _free_bytes(raw_path.parent) < MIN_FREE_BYTES + MAX_DECOMPRESSED_PAYLOAD_BYTES:
        raise ContractError("icon_retained_raw_min_free_space_guard")
    grid = ICON_SUPPORT_ROOT / "target_grid_world_025.txt"
    weights = ICON_SUPPORT_ROOT / "weights_icogl2world_025.nc"
    if not grid.is_file() or not weights.is_file():
        raise ContractError("icon_retained_raw_remap_assets_missing")
    points: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="bayes-basket-icon-retained-") as temp_dir:
        temp_root = Path(temp_dir)
        decompressed = temp_root / "source.grib2"
        world_remapped = temp_root / "world-remapped.grib2"
        station_grid = temp_root / "station-grid.txt"
        remapped = temp_root / "station-remapped.grib2"
        with bz2.open(raw_path, "rb") as source, decompressed.open("wb") as target:
            size = 0
            for block in iter(lambda: source.read(1024 * 1024), b""):
                size += len(block)
                if size > MAX_DECOMPRESSED_PAYLOAD_BYTES:
                    raise ContractError("icon_retained_raw_decompressed_payload_too_large")
                target.write(block)
        coordinates_by_station = {station_id: station_coordinates(station_id) for station_id in stations}
        station_grid.write_text(
            "gridtype = unstructured\n"
            f"gridsize = {len(stations)}\n"
            "xvals = " + " ".join(str(coordinates_by_station[station_id]["longitude"]) for station_id in stations) + "\n"
            "yvals = " + " ".join(str(coordinates_by_station[station_id]["latitude"]) for station_id in stations) + "\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [str(cdo), "-s", "-f", "grb2", f"remap,{grid},{weights}", str(decompressed), str(world_remapped)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0 or not world_remapped.is_file() or world_remapped.stat().st_size <= 0:
            raise ContractError(f"icon_retained_raw_remap_failed:{completed.stderr[-400:]}")
        point_remap = subprocess.run(
            [str(cdo), "-s", "-f", "grb2", f"remapnn,{station_grid}", str(world_remapped), str(remapped)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if point_remap.returncode != 0 or not remapped.is_file() or remapped.stat().st_size <= 0:
            raise ContractError(f"icon_retained_raw_station_remap_failed:{point_remap.stderr[-400:]}")
        selector = "shortName=2t,typeOfLevel=heightAboveGround,level=2"
        metadata = subprocess.run(
            [str(grib_get), "-w", selector, "-p", "dataDate,dataTime,validityDate,validityTime,step", str(remapped)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        metadata_stdout = str(metadata.stdout or "")
        if metadata.returncode != 0 or len(metadata_stdout.split()) < 5:
            raise ContractError("icon_retained_raw_decoded_metadata_invalid")
        metadata_tokens = metadata_stdout.split()
        if metadata_tokens[0] + metadata_tokens[1].zfill(4) != str(message["run_initialization_utc"]).replace("-", "").replace("T", "").replace(":", "")[:12]:
            raise ContractError("icon_retained_raw_decoded_run_lineage_mismatch")
        expected_valid_compact = str(message["valid_time_utc"]).replace("-", "").replace("T", "").replace(":", "")[:12]
        if metadata_tokens[2] + metadata_tokens[3].zfill(4) != expected_valid_compact:
            raise ContractError("icon_retained_raw_decoded_valid_lineage_mismatch")
        decoded = subprocess.run(
            [str(grib_get_data), "-w", selector, "-F", "%.10f", str(remapped)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        stdout = str(decoded.stdout or "")
        if len(stdout.encode("utf-8")) > MAX_GRIB_GET_STDOUT_BYTES:
            raise ContractError("icon_retained_raw_decoder_output_too_large")
        if decoded.returncode != 0:
            raise ContractError("icon_retained_raw_temperature_decode_failed")
        values: list[float] = []
        for line in stdout.splitlines():
            token = line.strip()
            if not token or token.lower().startswith("value"):
                continue
            try:
                values.append(float(token))
            except ValueError:
                continue
        if len(values) != len(stations):
            raise ContractError(f"icon_retained_raw_station_value_cardinality_invalid:{len(values)}:{len(stations)}")
        for station_id, value in zip(stations, values):
            assert_process_memory_budget()
            temperature_c = value - 273.15 if value > 150.0 else value
            points.append({
                "station_id": station_id,
                "event_date": message["event_date"],
                "target_timezone": target_timezone,
                "provider_id": PROVIDER_ID,
                "model_id": "ICON",
                "model_run_initialization_utc": message["run_initialization_utc"],
                "valid_time_utc": message["valid_time_utc"],
                "temperature_2m_c": round(temperature_c, 6),
                "raw_message_sha256": message["raw_message_sha256"],
                "receipt_sha256": message["receipt_sha256"],
                "published_at_utc": message["published_at_utc"],
                "received_at_utc": message["received_at_utc"],
                "capture_mode": "historical_rebuild_from_retained_official_raw",
                "production_calibration_eligible": False,
                "source_url": message["source_url"],
                "raw_object_ref": message["raw_object_ref"],
            })
    return points, blockers


def rebuild(
    *,
    event_dates: Sequence[str],
    station_ids: Sequence[str],
    source_roots: Sequence[Path],
    output: Path,
    target_timezone: str,
    grib_get: str = GRIB_GET,
    grib_get_data: str = GRIB_GET_DATA,
    cdo: str = CDO,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    requested_dates = sorted({str(value)[:10] for value in event_dates if str(value).strip()})
    requested_stations = sorted({str(value).strip().upper() for value in station_ids if str(value).strip()})
    if not requested_dates or len(requested_dates) > 31:
        raise ContractError("icon_retained_raw_event_dates_invalid")
    if not requested_stations or len(requested_stations) > expected_station_count():
        raise ContractError("icon_retained_raw_station_ids_invalid")
    reconstruction_clock = str(as_of_utc or _now()).strip()
    for event_date in requested_dates:
        # Retained raw is a historical repair input.  A current open day is
        # prospective evidence, not a final daily curve, and a future day is
        # never admitted to this rebuild at all.
        assert_event_day_closed(
            event_date,
            as_of_utc=reconstruction_clock,
            target_timezone=target_timezone,
        )
    grib_get_path = _binary(grib_get, "grib_get")
    grib_get_data_path = _binary(grib_get_data, "grib_get_data")
    cdo_path = _binary(cdo, "cdo")
    messages, inventory = _collect_messages(
        source_roots=source_roots,
        event_dates=set(requested_dates),
        target_timezone=target_timezone,
    )
    model_points: list[dict[str, Any]] = []
    decode_blockers: list[dict[str, Any]] = []
    for index, message in enumerate(messages, start=1):
        if index > MAX_INPUT_FILES_PER_BATCH:
            decode_blockers.append({"reason": "icon_retained_raw_message_batch_cap_exceeded"})
            break
        try:
            points, blockers = _decode_message(
                message,
                stations=requested_stations,
                grib_get=grib_get_path,
                grib_get_data=grib_get_data_path,
                cdo=cdo_path,
                target_timezone=target_timezone,
            )
            model_points.extend(points)
            if blockers:
                decode_blockers.append({"message": message, "station_blockers": blockers[:100]})
        except (OSError, subprocess.SubprocessError, ValueError, ContractError) as exc:
            decode_blockers.append({"message": message, "reason": f"{type(exc).__name__}:{exc}"})
    curves = materialize_native_daily_curves(model_points, target_timezone=target_timezone) if model_points else {
        "curve_count": 0, "complete_curve_count": 0, "eligible_model_rows": [], "curves": [], "all_complete": False,
    }
    identity = {
        "schema": SCHEMA,
        "reconstructed_at_utc": _now(),
        "target_timezone": target_timezone,
        "event_dates": requested_dates,
        "as_of_utc": reconstruction_clock,
        "station_ids": requested_stations,
        "provider_id": PROVIDER_ID,
        "capture_mode": "historical_rebuild_from_retained_official_raw",
        "model_points": model_points,
        "model_point_count": len(model_points),
        "curves": curves,
        "inventory": inventory,
        "decode_blocker_count": len(decode_blockers),
        "decode_blockers": decode_blockers[:MAX_BLOCKERS],
        "checks": {
            "official_provider_only": True,
            "raw_bodies_are_retained_and_sha256_verified": True,
            "no_open_meteo": True,
            "no_aggregator_or_republisher": True,
            "no_legacy_timezone_input": True,
            "production_calibration_eligible": False,
            "complete_curve_count_positive": bool(curves.get("complete_curve_count")),
            "writes_shared_live_database": False,
            "writes_target_database": False,
            "reads_probability_state": False,
            "reads_market_state": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
        "resource_budget": {
            "max_decompressed_payload_bytes": MAX_DECOMPRESSED_PAYLOAD_BYTES,
            "max_grib_get_stdout_bytes": MAX_GRIB_GET_STDOUT_BYTES,
            "min_free_bytes": MIN_FREE_BYTES,
            "raw_bodies_copied": False,
        },
        "boundary": {
            "target_only": True,
            "offline_research_only": True,
            "historical_rebuild": True,
            "production_calibration_eligible": False,
            "live_submit_authorized": False,
            "protected_production_line_touched": False,
        },
    }
    result = {**identity, "evidence_sha256": content_sha256(identity)}
    result["receipt_sha256"] = content_sha256(result)
    write_bounded_json(output, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", action="append", required=True)
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument("--timezone-registry", type=Path, default=DEFAULT_TIMEZONE_REGISTRY)
    parser.add_argument("--source-root", type=Path, action="append", default=[])
    parser.add_argument("--grib-get", default=GRIB_GET)
    parser.add_argument("--grib-get-data", default=GRIB_GET_DATA)
    parser.add_argument("--cdo", default=CDO)
    parser.add_argument("--as-of-utc", default="", help="Historical cutoff; every requested settlement day must already be closed.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    roots = [path.expanduser().resolve() for path in (args.source_root or list(DEFAULT_SOURCE_ROOTS))]
    result = rebuild(
        event_dates=args.event_date,
        station_ids=args.station_id,
        source_roots=roots,
        output=args.output.expanduser().resolve(),
        target_timezone=_registry_timezone(
            args.timezone_registry,
            station_ids=args.station_id,
            event_dates=args.event_date,
        ),
        grib_get=args.grib_get,
        grib_get_data=args.grib_get_data,
        cdo=args.cdo,
        as_of_utc=args.as_of_utc or None,
    )
    print(json.dumps({
        "schema": result["schema"],
        "output": str(args.output.expanduser().resolve()),
        "model_point_count": result["model_point_count"],
        "complete_curve_count": result["curves"].get("complete_curve_count", 0),
        "missing_or_mismatched_raw_count": result["inventory"]["missing_or_mismatched_raw_count"],
        "decode_blocker_count": result["decode_blocker_count"],
        "production_calibration_eligible": result["checks"]["production_calibration_eligible"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
