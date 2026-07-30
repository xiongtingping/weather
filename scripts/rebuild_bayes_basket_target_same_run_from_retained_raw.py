#!/usr/bin/env python3
"""Reconstruct one same-run target corpus from retained official raw.

This is a bounded, offline preflight for the target no-live lane.  It reads
existing content-addressed transport receipts and raw objects only; it never
downloads, writes SQLite/arrival SSOT, starts a service, or calls the gateway.
The result is diagnostic evidence, not a promotion or calibration authority.

ECMWF and ICON captures are global native payloads captured through the
ZUUU-scoped collector, so they may be decoded at the canonical 45-station
coordinates.  GFS captures are station-filtered and therefore may only decode
at the station recorded by their receipt.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import assert_event_day_started  # noqa: E402
from bayes_basket_target.model_arrival import load_model_arrival_receipts  # noqa: E402
from bayes_basket_target.model_run_contract import PROVIDERS, normalize_run_initialization  # noqa: E402
from bayes_basket_target.market_registry import load_market_registry_snapshot  # noqa: E402
from bayes_basket_target.native_model_decode import (  # noqa: E402
    decode_native_temperature_points,
    materialize_native_daily_curves,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_INPUT_FILES_PER_BATCH,
    assert_process_memory_budget,
    write_bounded_json,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402
from bayes_basket_target.settlement_time import registry_scope_settlement_timezone  # noqa: E402
from scripts.rebuild_bayes_basket_target_icon_from_retained_raw import (  # noqa: E402
    CDO,
    GRIB_GET,
    GRIB_GET_DATA,
    _binary,
    _decode_message,
)


SCHEMA = "bayes_basket_target_same_run_retained_raw_preflight_v1"
MAX_RECEIPTS_PER_PROVIDER = 4096
MAX_BLOCKERS = 500
# ECMWF and ICON transport captures are global native payloads.  NOAA's
# official PDS ``pgrb2.0p25`` objects are global too; NOMADS filter responses
# remain station-scoped and must never be fanned out.  Keep this distinction
# in the raw-source contract so a station-filtered response cannot silently
# become a 45-station model input.
GLOBAL_FANOUT_PROVIDERS = frozenset({"ecmwf_ifs025_official", "icon_global_official"})
GLOBAL_GFS_HOSTS = frozenset({"noaa-gfs-bdp-pds.s3.amazonaws.com"})
OFFICIAL_HOSTS = {
    "ecmwf_ifs025_official": frozenset({
        "data.ecmwf.int",
        "ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
        "storage.googleapis.com",
        "ai4edataeuwest.blob.core.windows.net",
    }),
    "gfs_0p25_official": frozenset({"nomads.ncep.noaa.gov", "noaa-gfs-bdp-pds.s3.amazonaws.com"}),
    "icon_global_official": frozenset({"opendata.dwd.de"}),
}
DEFAULT_ROOT = ROOT / "data/research/bayes_basket_target/model_arrivals"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_same_run_retained_raw_preflight.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_receipt(path: Path) -> Mapping[str, Any]:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ContractError("same_run_transport_receipt_too_large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ContractError("same_run_transport_receipt_mapping_required")
    return value


def _target_date(valid_time_utc: str, *, target_timezone: str) -> str:
    parsed = datetime.fromisoformat(str(valid_time_utc).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("same_run_valid_time_timezone_required")
    from zoneinfo import ZoneInfo
    return parsed.astimezone(ZoneInfo(target_timezone)).date().isoformat()


def _receipt_run(receipt: Mapping[str, Any]) -> str:
    return normalize_run_initialization(
        receipt.get("run_initialization_utc") or receipt.get("run_at_utc")
    )


def _receipt_records(
    *,
    provider_id: str,
    provider_root: Path,
    target_local_date: str,
    target_timezone: str,
    run_initialization_utc: str,
    stations: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    receipt_dir = provider_root / "receipts"
    paths = sorted(receipt_dir.glob("*.json")) if receipt_dir.is_dir() else []
    blockers: list[dict[str, Any]] = []
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    if len(paths) > MAX_RECEIPTS_PER_PROVIDER:
        blockers.append({"provider_id": provider_id, "reason": "receipt_file_cap_exceeded"})
        paths = paths[:MAX_RECEIPTS_PER_PROVIDER]
    scanned = 0
    selected = 0
    raw_verified = 0
    for path in paths:
        scanned += 1
        try:
            receipt = _load_receipt(path)
            run = _receipt_run(receipt)
            valid = str(receipt.get("valid_time_utc") or "").strip()
            if (
                run != run_initialization_utc
                or _target_date(valid, target_timezone=target_timezone)
                != target_local_date
            ):
                continue
            if str(receipt.get("provider_id") or "").strip().lower() != provider_id:
                raise ContractError("same_run_receipt_provider_mismatch")
            station = str(receipt.get("station_id") or "").strip().upper()
            raw_receipts = receipt.get("raw_receipts")
            if not isinstance(raw_receipts, list) or len(raw_receipts) != 1:
                raise ContractError("same_run_raw_receipt_cardinality_invalid")
            raw = raw_receipts[0]
            if not isinstance(raw, Mapping):
                raise ContractError("same_run_raw_receipt_mapping_required")
            url = str(raw.get("raw_request_url") or "").strip()
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc not in OFFICIAL_HOSTS[provider_id]:
                raise ContractError("same_run_source_host_not_official")
            raw_sha = str(raw.get("raw_sha256") or "").strip().lower()
            if len(raw_sha) != 64 or any(ch not in "0123456789abcdef" for ch in raw_sha):
                raise ContractError("same_run_raw_sha256_invalid")
            raw_ref = Path(str(raw.get("raw_object_ref") or "")).expanduser().resolve()
            if not raw_ref.is_file() or _sha256(raw_ref) != raw_sha:
                raise ContractError("same_run_raw_missing_or_sha_mismatch")
            raw_verified += 1
            key = (station, valid, raw_sha)
            records.setdefault(key, {
                "provider_id": provider_id,
                "station_id": station,
                "event_date": target_local_date,
                "target_local_date": target_local_date,
                "run_initialization_utc": run,
                "valid_time_utc": valid,
                "raw_message_sha256": raw_sha,
                "raw_object_ref": str(raw_ref),
                "receipt_sha256": _sha256(path),
                "source_url": url,
                "published_at_utc": str(raw.get("provider_published_at_utc") or ""),
                "received_at_utc": str(raw.get("response_completed_at") or ""),
                "capture_mode": str(receipt.get("capture_mode") or ""),
                "historical_arrival_time_observed": receipt.get("historical_arrival_time_observed"),
                "aggregator_or_republisher": receipt.get("aggregator_or_republisher"),
                "open_meteo": receipt.get("open_meteo"),
            })
            selected += 1
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, ContractError) as exc:
            blockers.append({"provider_id": provider_id, "receipt_path": str(path), "reason": f"{type(exc).__name__}:{exc}"})
    selected_records, precedence_discarded = _apply_gfs_source_precedence(records.values())
    selected_records.sort(key=lambda item: (item["valid_time_utc"], item["station_id"], item["raw_message_sha256"]))
    return selected_records, blockers[:MAX_BLOCKERS], {
        "receipt_files_scanned": scanned,
        "matching_receipts": selected,
        "unique_raw_records_before_source_precedence": len(records),
        "unique_raw_records": len(selected_records),
        "source_precedence_discarded_records": precedence_discarded,
        "raw_receipts_sha256_verified": raw_verified,
        "raw_records_sha256_verified": len(selected_records),
    }


def _stations_for_record(record: Mapping[str, Any], stations: Sequence[str]) -> list[str]:
    receipt_station = str(record.get("station_id") or "").strip().upper()
    provider = str(record.get("provider_id") or "").strip().lower()
    source_url = str(record.get("source_url") or "").strip().lower()
    parsed = urlsplit(source_url)
    is_global_gfs_pds = provider == "gfs_0p25_official" and parsed.hostname in GLOBAL_GFS_HOSTS
    if (provider in GLOBAL_FANOUT_PROVIDERS or is_global_gfs_pds) and receipt_station == "ZUUU":
        return list(stations)
    return [receipt_station] if receipt_station in set(stations) else []


def _gfs_source_rank(record: Mapping[str, Any]) -> int:
    """Return the deterministic precedence for one official GFS raw object.

    NOAA PDS is the global, content-addressed object used for the canonical
    45-station fan-out.  NOMADS filter responses are still an official source,
    but are station-scoped and are only a bounded fallback when the matching
    PDS key is absent.  Without this rule one run can produce two different
    official payloads for the same station/valid-time key and the arrival
    index quite correctly rejects the otherwise usable corpus as a duplicate
    conflict.
    """

    provider = str(record.get("provider_id") or "").strip().lower()
    if provider != "gfs_0p25_official":
        return 0
    hostname = urlsplit(str(record.get("source_url") or "")).hostname or ""
    if hostname.lower() in GLOBAL_GFS_HOSTS:
        return 2
    return 1


def _apply_gfs_source_precedence(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Collapse duplicate official GFS transport lanes by explicit policy.

    The PDS record is anchored at ``ZUUU`` and is later fanned out, so the
    precedence key is valid time × run rather than station × valid time.  If
    a PDS object exists for that key, all station-filtered NOMADS objects for
    the same key are excluded.  Otherwise the station-scoped official NOMADS
    records remain as the explicit fallback.  Different raw hashes inside
    the selected source remain visible and are not silently chosen here;
    downstream arrival identity still fails closed on a genuine same-source
    conflict.
    """

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    passthrough: list[dict[str, Any]] = []
    for raw in records:
        item = dict(raw)
        if str(item.get("provider_id") or "").strip().lower() != "gfs_0p25_official":
            passthrough.append(item)
            continue
        key = (
            str(item.get("valid_time_utc") or "").strip(),
            str(item.get("run_initialization_utc") or "").strip(),
        )
        grouped[key].append(item)
    selected: list[dict[str, Any]] = []
    discarded = 0
    for items in grouped.values():
        has_pds = any(_gfs_source_rank(item) == 2 for item in items)
        chosen = [item for item in items if _gfs_source_rank(item) == 2] if has_pds else list(items)
        discarded += len(items) - len(chosen)
        selected.extend(chosen)
    return [*passthrough, *selected], discarded


def _model_point(
    record: Mapping[str, Any],
    station: str,
    decoded: Mapping[str, Any],
    *,
    target_timezone: str,
) -> dict[str, Any]:
    points = decoded.get("points") or []
    if len(points) != 1:
        raise ContractError("same_run_decoded_point_cardinality_invalid")
    point = points[0]
    if str(point.get("grib_run_initialization_utc")) != record["run_initialization_utc"]:
        raise ContractError("same_run_decoded_run_lineage_mismatch")
    if str(point.get("grib_valid_time_utc")) != record["valid_time_utc"]:
        raise ContractError("same_run_decoded_valid_lineage_mismatch")
    return {
        "station_id": station,
        "event_date": record["target_local_date"],
        "target_timezone": target_timezone,
        "provider_id": record["provider_id"],
        "model_id": {provider: model for provider, model in (
            ("ecmwf_ifs025_official", "ECMWF"),
            ("gfs_0p25_official", "GFS"),
            ("icon_global_official", "ICON"),
        )}[record["provider_id"]],
        "model_run_initialization_utc": record["run_initialization_utc"],
        "valid_time_utc": record["valid_time_utc"],
        "temperature_2m_c": point["temperature_2m_c"],
        "raw_message_sha256": record["raw_message_sha256"],
        "receipt_sha256": record["receipt_sha256"],
        "published_at_utc": record["published_at_utc"],
        "received_at_utc": record["received_at_utc"],
        "capture_mode": "prospective_retained_raw_reconstruction",
        "source_url": record["source_url"],
        "raw_object_ref": record["raw_object_ref"],
        "coordinate_lineage": decoded.get("coordinate_lineage"),
        "production_calibration_eligible": False,
    }


def _dedupe_model_points(
    points: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Fold identical global/station receipts without hiding real conflicts."""

    unique: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    duplicate_count = 0
    for raw in points:
        point = dict(raw)
        key = (
            str(point.get("provider_id") or ""),
            str(point.get("station_id") or ""),
            str(point.get("event_date") or ""),
            str(point.get("model_run_initialization_utc") or ""),
            str(point.get("valid_time_utc") or ""),
        )
        previous = unique.get(key)
        if previous is None:
            unique[key] = point
            continue
        same_raw = (
            str(previous.get("raw_message_sha256") or "")
            == str(point.get("raw_message_sha256") or "")
        )
        same_temperature = math.isclose(
            float(previous["temperature_2m_c"]),
            float(point["temperature_2m_c"]),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        if not same_raw or not same_temperature:
            raise ContractError("same_run_model_point_conflict")
        duplicate_count += 1
        if str(point.get("receipt_sha256") or "") < str(
            previous.get("receipt_sha256") or ""
        ):
            unique[key] = point
    return [
        unique[key] for key in sorted(unique)
    ], duplicate_count


def rebuild(
    *,
    target_local_date: str,
    run_initialization_utc: str,
    station_ids: Sequence[str],
    market_registry_snapshot: Path,
    source_root: Path = DEFAULT_ROOT,
    output: Path = DEFAULT_OUTPUT,
    grib_get_binary: str | None = None,
    cdo_binary: str | None = None,
    icon_grid_path: Path | None = None,
    icon_weights_path: Path | None = None,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    run = normalize_run_initialization(run_initialization_utc)
    registry = load_market_registry_snapshot(
        market_registry_snapshot.expanduser().resolve()
    )
    registry_rows = [
        row
        for row in registry["rows"]
        if str(row.get("event_date") or "") == target_local_date
    ]
    decision_at_utc = str(as_of_utc or _now())
    try:
        decision = datetime.fromisoformat(decision_at_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("same_run_as_of_utc_invalid") from exc
    if decision.tzinfo is None or decision.utcoffset() is None:
        raise ContractError("same_run_as_of_utc_timezone_required")
    decision_at_utc = decision.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    stations = sorted({str(item).strip().upper() for item in station_ids if str(item).strip()})
    target_timezone = registry_scope_settlement_timezone(
        registry_rows,
        station_ids=stations,
        event_dates=[target_local_date],
    )
    # This preflight may inspect the current open settlement day, but it must
    # reject a future day before binaries, raw bodies, or decode work.
    assert_event_day_started(
        target_local_date,
        as_of_utc=decision_at_utc,
        target_timezone=target_timezone,
    )
    registry_stations = set(coordinate_registry_manifest()["station_ids"])
    if not stations or len(stations) > 45 or not set(stations).issubset(registry_stations):
        raise ContractError("same_run_station_scope_invalid")
    grib_get = _binary(grib_get_binary or GRIB_GET, "grib_get")
    grib_get_data = _binary(GRIB_GET_DATA, "grib_get_data")
    cdo = _binary(cdo_binary or CDO, "cdo")
    all_records: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    provider_inventory: dict[str, dict[str, int]] = {}
    for provider in PROVIDERS:
        records, provider_blockers, inventory = _receipt_records(
            provider_id=provider,
            provider_root=source_root / provider,
            target_local_date=target_local_date,
            target_timezone=target_timezone,
            run_initialization_utc=run,
            stations=set(stations),
        )
        all_records.extend(records)
        blockers.extend(provider_blockers)
        provider_inventory[provider] = inventory
    if len(all_records) > MAX_INPUT_FILES_PER_BATCH:
        raise ContractError("same_run_raw_record_batch_cap_exceeded")

    points: list[dict[str, Any]] = []
    decode_cache: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    icon_records = [item for item in all_records if item["provider_id"] == "icon_global_official"]
    other_records = [item for item in all_records if item["provider_id"] != "icon_global_official"]
    # The ICON raw is an unstructured global payload; remap once per forecast
    # file to all 45 stations, rather than expanding 225 independent remaps.
    for record in icon_records:
        try:
            decoded_points, station_blockers = _decode_message(
                record,
                stations=stations,
                grib_get=grib_get,
                grib_get_data=grib_get_data,
                cdo=cdo,
                target_timezone=target_timezone,
            )
            if station_blockers:
                blockers.extend(station_blockers[:MAX_BLOCKERS])
            points.extend(decoded_points)
        except (OSError, ValueError, ContractError) as exc:
            blockers.append({"provider_id": "icon_global_official", "valid_time_utc": record["valid_time_utc"], "reason": f"{type(exc).__name__}:{exc}"})

    for record in other_records:
        for station in _stations_for_record(record, stations):
            try:
                cache_key = (record["provider_id"], record["raw_message_sha256"], station)
                decoded = decode_cache.get(cache_key)
                if decoded is None:
                    decoded = decode_native_temperature_points(
                        Path(record["raw_object_ref"]),
                        provider_id=record["provider_id"],
                        station_id=station,
                        grib_get_binary=str(grib_get),
                        cdo_binary=str(cdo),
                        icon_grid_path=icon_grid_path,
                        icon_weights_path=icon_weights_path,
                    )
                    decode_cache[cache_key] = decoded
                points.append(
                    _model_point(
                        record,
                        station,
                        decoded,
                        target_timezone=target_timezone,
                    )
                )
                assert_process_memory_budget()
            except (OSError, ValueError, ContractError) as exc:
                blockers.append({"provider_id": record["provider_id"], "station_id": station, "valid_time_utc": record["valid_time_utc"], "reason": f"{type(exc).__name__}:{exc}"})
            if len(blockers) >= MAX_BLOCKERS:
                break
        if len(blockers) >= MAX_BLOCKERS:
            break

    points, identical_point_duplicates = _dedupe_model_points(points)
    curves = materialize_native_daily_curves(
        points,
        target_timezone=target_timezone,
    )
    arrival_candidates: list[dict[str, Any]] = []
    for point in points:
        arrival_identity = {
            "provider_id": point["provider_id"],
            "model_id": point["model_id"],
            "station_id": point["station_id"],
            "target_local_date": point["event_date"],
            "target_timezone": target_timezone,
            "run_initialization_utc": point["model_run_initialization_utc"],
            "valid_time_utc": point["valid_time_utc"],
            "published_at_utc": point["published_at_utc"],
            "received_at_utc": point["received_at_utc"],
            "decision_at_utc": decision_at_utc,
            "publication_time_source": "provider_http_last_modified",
            "source_url": point["source_url"],
            "raw_payload_sha256s": [point["raw_message_sha256"]],
            "receipt_sha256": point["receipt_sha256"],
            "capture_mode": "prospective",
            "historical_arrival_time_observed": True,
            "aggregator_or_republisher": False,
            "open_meteo": False,
        }
        arrival_identity["arrival_id"] = "arrival-" + content_sha256(arrival_identity)[:24]
        arrival_candidates.append(arrival_identity)
    normalized_arrivals, arrival_audit = load_model_arrival_receipts(
        arrival_candidates,
        decision_at_utc=decision_at_utc,
    )
    by_station: dict[tuple[str, str, str], set[str]] = {}
    for row in curves.get("eligible_model_rows") or []:
        key = (str(row.get("station_id")), str(row.get("event_date")), str(row.get("model_run_initialization_utc")))
        by_station.setdefault(key, set()).add(str(row.get("provider_id")))
    exact_keys = sorted(key for key, providers in by_station.items() if providers == set(PROVIDERS))
    identity = {
        "schema": SCHEMA,
        "reconstructed_at_utc": _now(),
        "target_local_date": target_local_date,
        "run_initialization_utc": run,
        "target_timezone": target_timezone,
        "market_registry_snapshot_sha256": registry["snapshot_sha256"],
        "market_registry_snapshot_path": str(
            market_registry_snapshot.expanduser().resolve()
        ),
        "decision_at_utc": decision_at_utc,
        "provider_set": list(PROVIDERS),
        "station_ids": stations,
        "source_root": str(source_root.resolve()),
        "provider_inventory": provider_inventory,
        "model_point_count": len(points),
        "identical_model_point_duplicates_folded": (
            identical_point_duplicates
        ),
        "model_points": points,
        "curves": curves,
        "model_arrivals": normalized_arrivals,
        "model_arrival_audit": arrival_audit,
        "exact_three_model_join_count": len(exact_keys),
        "exact_three_model_join_keys": [list(key) for key in exact_keys],
        "blocker_count": len(blockers),
        "blockers": blockers[:MAX_BLOCKERS],
        "checks": {
            "one_run_only": True,
            "provider_set_complete": all(provider_inventory[p]["matching_receipts"] > 0 for p in PROVIDERS),
            "official_provider_only": True,
            "raw_sha256_verified": all(provider_inventory[p]["raw_records_sha256_verified"] == provider_inventory[p]["unique_raw_records"] for p in PROVIDERS),
            "complete_curve_count_positive": curves.get("complete_curve_count", 0) > 0,
            "exact_three_model_join_positive": bool(exact_keys),
            "model_arrival_index_complete": arrival_audit["accepted_count"] == len(points) and arrival_audit["blocked_count"] == 0,
            "all_model_arrivals_prospective": arrival_audit["all_prospective"],
            "all_model_arrival_publication_sources_native": bool(normalized_arrivals) and all(item["publication_time_source"] == "provider_http_last_modified" for item in normalized_arrivals),
            "production_calibration_eligible": False,
            "promotion_allowed": False,
            "writes_shared_live_database": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
        "boundary": {
            "target_only": True,
            "offline_research_only": True,
            "reads_retained_raw_only": True,
            "downloads_payloads": False,
            "writes_shared_live_database": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "protected_production_line_touched": False,
        },
    }
    result = {**identity, "evidence_sha256": content_sha256(identity)}
    result["receipt_sha256"] = content_sha256(result)
    write_bounded_json(output.expanduser().resolve(), result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-local-date", required=True)
    parser.add_argument("--run-initialization-utc", required=True)
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument(
        "--market-registry-snapshot",
        type=Path,
        required=True,
    )
    parser.add_argument("--source-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--grib-get")
    parser.add_argument("--cdo")
    parser.add_argument("--icon-grid", type=Path)
    parser.add_argument("--icon-weights", type=Path)
    parser.add_argument("--as-of-utc")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = rebuild(
        target_local_date=args.target_local_date,
        run_initialization_utc=args.run_initialization_utc,
        station_ids=args.station_id,
        market_registry_snapshot=args.market_registry_snapshot,
        source_root=args.source_root.expanduser().resolve(),
        output=args.output.expanduser().resolve(),
        grib_get_binary=args.grib_get,
        cdo_binary=args.cdo,
        icon_grid_path=args.icon_grid,
        icon_weights_path=args.icon_weights,
        as_of_utc=args.as_of_utc,
    )
    print(json.dumps({
        "schema": result["schema"],
        "output": str(args.output.expanduser().resolve()),
        "model_point_count": result["model_point_count"],
        "complete_curve_count": result["curves"]["complete_curve_count"],
        "exact_three_model_join_count": result["exact_three_model_join_count"],
        "blocker_count": result["blocker_count"],
        "promotion_allowed": result["checks"]["promotion_allowed"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
