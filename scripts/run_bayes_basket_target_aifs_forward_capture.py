#!/usr/bin/env python3
"""Capture official ECMWF AIFS Single 2 m temperature prospectively.

The runner reads the official ECMWF Open Data ``.index`` for each six-hour
valid time, selects exactly one AIFS ``2t`` GRIB message, and range-downloads
only that message.  One global raw object is then fanned out by reference to
the explicit station scope.  It never refetches IFS/GEFS, writes SQLite,
creates P0 authority, or calls an order gateway.  Official AIFS remains in the
ECMWF independence family and requires its own strict historical closed-day
OOS activation before a probability consumer may prefer it to IFS.  This
prospective capture is only arrival/interface evidence; it is never an OOS
sample.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from urllib.request import Request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.et_history import TARGET_TIMEZONE, event_day_window  # noqa: E402
from bayes_basket_target.model_arrival import validate_prospective_capture_window  # noqa: E402
from bayes_basket_target.market_registry import load_market_registry_snapshot  # noqa: E402
from bayes_basket_target.model_arrival_capture_config import (  # noqa: E402
    load_target_capture_config,
    normalize_resource_budget,
)
from bayes_basket_target.model_run_contract import (  # noqa: E402
    build_model_run_contract,
    normalize_run_initialization,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    assert_target_raw_retention_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402
from scripts.build_bayes_basket_target_global_model_fanout import fanout  # noqa: E402
from scripts.capture_bayes_basket_target_model_arrival import (  # noqa: E402
    DEFAULT_ROOT as DEFAULT_ARRIVAL_ROOT,
    capture,
)
from official_nwp_https_transport import open_official_https  # noqa: E402


SCHEMA = "bayes_basket_target_aifs_forward_capture_scheduler_v1"
PROVIDER_ID = "ecmwf_aifs025_official"
MODEL_FAMILY_ID = "ECMWF"
PRODUCT_ID = "ECMWF_AIFS_SINGLE_0P25"
BASE_URL = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
MAX_INDEX_BYTES = 1024 * 1024
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_VALID_TIMES = 8
DEFAULT_TIMEOUT_SEC = 60.0
DEFAULT_OUTPUT_ROOT = ROOT / "output/audits/bayes_basket_target_aifs_forward_runs"
CAPTURE_CONFIG = ROOT / "config/bayes_basket_target/model_arrival_capture_v1.json"
DEFAULT_MARKET_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("aifs_forward_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("aifs_forward_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _latest_mature_cycle(as_of_utc: str) -> str:
    # ECMWF publishes AIFS Single at 00/06/12/18Z.  Prefer the preceding
    # completed cycle; execute_plan still fails closed if its index is absent.
    now = _parse_utc(as_of_utc).replace(minute=0, second=0, microsecond=0)
    cycle = now.replace(hour=(now.hour // 6) * 6) - timedelta(hours=6)
    return cycle.isoformat(timespec="seconds").replace("+00:00", "Z")


def _urls(run: str, forecast_hour: int) -> tuple[str, str]:
    cycle = _parse_utc(run)
    prefix = (
        f"{BASE_URL}/{cycle:%Y%m%d}/{cycle:%H}z/aifs-single/0p25/oper/"
        f"{cycle:%Y%m%d%H}0000-{forecast_hour}h-oper-fc"
    )
    return prefix + ".grib2", prefix + ".index"


def _valid_hours(
    *, event_date: str, run: str, as_of_utc: str, target_timezone: str
) -> list[dict[str, Any]]:
    window = event_day_window(
        event_date,
        as_of_utc=as_of_utc,
        target_timezone=target_timezone,
    )
    if window["state"] == "closed":
        raise ContractError("aifs_forward_target_day_closed")
    run_at = _parse_utc(run)
    start = _parse_utc(window["target_day_start_utc"])
    end = _parse_utc(window["target_day_end_utc"])
    # AIFS Open Data publishes deterministic fields every six hours.  Select
    # all steps whose valid time lies in the settlement day; this is a P0
    # forecast curve, so future valid times may be captured after publication.
    rows: list[dict[str, Any]] = []
    for forecast_hour in range(0, 145, 6):
        valid = run_at + timedelta(hours=forecast_hour)
        if start <= valid < end:
            grib_url, index_url = _urls(run, forecast_hour)
            rows.append({
                "valid_time_utc": valid.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
                "forecast_hour": forecast_hour,
                "grib_url": grib_url,
                "index_url": index_url,
            })
    # At UTC+12 (for example NZWN), a 00/06/12/18Z native cadence has
    # exactly three in-day samples.  The decoder's separate AIFS 6-hour
    # endpoint/gap coverage gate accepts this complete edge-aligned curve.
    if len(rows) < 3 or len(rows) > MAX_VALID_TIMES:
        raise ContractError("aifs_forward_valid_time_scope_invalid")
    return rows


def _station_timezone_map(
    *,
    station_ids: Sequence[str],
    event_date: str,
    supplied: Mapping[str, str] | None,
    registry_path: Path | None,
    registry_sha256: str,
    fallback_timezone: str,
) -> tuple[dict[str, str], str]:
    """Bind each station to the one settlement timezone in the registry.

    A global raw field is shared, but its station-arrival records are not: an
    AIFS daily curve is meaningful only in that market's settlement timezone.
    A multi-station plan therefore cannot silently inherit its anchor's zone.
    """

    stations = {str(value).strip().upper() for value in station_ids}
    explicit = {
        str(station).strip().upper(): str(timezone_name).strip()
        for station, timezone_name in (supplied or {}).items()
        if str(station).strip() and str(timezone_name).strip()
    }
    actual_sha = str(registry_sha256 or "").strip().lower()
    if registry_path is not None:
        registry = load_market_registry_snapshot(registry_path)
        snapshot_sha = str(registry["snapshot_sha256"])
        if actual_sha and actual_sha != snapshot_sha:
            raise ContractError("aifs_forward_registry_sha_mismatch")
        actual_sha = snapshot_sha
        resolved: dict[str, set[str]] = {station: set() for station in stations}
        for row in registry["rows"]:
            if (
                str(row.get("station_id") or "").strip().upper() in resolved
                and str(row.get("event_date") or "") == event_date
            ):
                timezone_name = str(row.get("settlement_timezone") or "").strip()
                if timezone_name:
                    resolved[str(row["station_id"]).strip().upper()].add(
                        timezone_name
                    )
        if any(len(values) != 1 for values in resolved.values()):
            raise ContractError("aifs_forward_registry_station_timezone_missing")
        from_registry = {
            station: next(iter(values)) for station, values in resolved.items()
        }
        if explicit and explicit != from_registry:
            raise ContractError("aifs_forward_station_timezone_registry_mismatch")
        explicit = from_registry
    if not explicit:
        if len(stations) != 1:
            raise ContractError("aifs_forward_station_timezone_registry_required")
        explicit = {next(iter(stations)): str(fallback_timezone).strip()}
    if set(explicit) != stations:
        raise ContractError("aifs_forward_station_timezone_scope_invalid")
    try:
        for timezone_name in explicit.values():
            ZoneInfo(timezone_name)
    except Exception as exc:
        raise ContractError("aifs_forward_station_timezone_invalid") from exc
    return dict(sorted(explicit.items())), actual_sha


def build_plan(
    *,
    event_date: str | None = None,
    run_initialization_utc: str | None = None,
    as_of_utc: str | None = None,
    station_ids: Sequence[str] | None = None,
    source_station_id: str = "ZUUU",
    target_timezone: str = TARGET_TIMEZONE,
    station_timezones: Mapping[str, str] | None = None,
    market_registry_snapshot_path: Path | None = None,
    market_registry_snapshot_sha256: str = "",
) -> dict[str, Any]:
    clock = as_of_utc or _now_utc()
    day = event_date or _parse_utc(clock).astimezone(ZoneInfo(target_timezone)).date().isoformat()
    run = normalize_run_initialization(
        run_initialization_utc or _latest_mature_cycle(clock)
    )
    stations = sorted({
        str(value).strip().upper()
        for value in (
            station_ids or coordinate_registry_manifest()["station_ids"]
        )
        if str(value).strip()
    })
    if not stations or len(stations) > expected_station_count():
        raise ContractError("aifs_forward_station_scope_invalid")
    anchor = str(source_station_id or "").strip().upper()
    if anchor not in stations:
        raise ContractError("aifs_forward_source_station_outside_scope")
    registry_sha = str(market_registry_snapshot_sha256 or "").strip().lower()
    if registry_sha and not re.fullmatch(r"[0-9a-f]{64}", registry_sha):
        raise ContractError("aifs_forward_registry_sha_invalid")
    timezones, registry_sha = _station_timezone_map(
        station_ids=stations,
        event_date=day,
        supplied=station_timezones,
        registry_path=market_registry_snapshot_path,
        registry_sha256=registry_sha,
        fallback_timezone=target_timezone,
    )
    for timezone_name in sorted(set(timezones.values())):
        validate_prospective_capture_window(
            target_local_date=day,
            target_timezone=timezone_name,
            run_initialization_utc=run,
            decision_at_utc=clock,
        )
    capture_config = load_target_capture_config(CAPTURE_CONFIG)
    contract = build_model_run_contract(
        target_local_date=day,
        run_initialization_utc=run,
        station_ids=stations,
        target_timezone=timezones[anchor],
        market_registry_snapshot_sha256=registry_sha,
        station_timezones=timezones,
    )
    station_valid_hours = {
        station_id: _valid_hours(
            event_date=day,
            run=run,
            as_of_utc=clock,
            target_timezone=timezones[station_id],
        )
        for station_id in stations
    }
    valid_by_time: dict[str, dict[str, Any]] = {}
    for station_id, rows in station_valid_hours.items():
        for row in rows:
            valid_time = str(row["valid_time_utc"])
            existing = valid_by_time.get(valid_time)
            if existing is None:
                existing = {**row, "station_ids": []}
                valid_by_time[valid_time] = existing
            existing["station_ids"].append(station_id)
    valid_hours = []
    for valid_time in sorted(valid_by_time):
        row = valid_by_time[valid_time]
        row["station_ids"] = sorted(set(row["station_ids"]))
        valid_hours.append(row)
    identity = {
        "schema": SCHEMA,
        "provider_id": PROVIDER_ID,
        "product_id": PRODUCT_ID,
        "independence_family_id": MODEL_FAMILY_ID,
        "target_local_date": day,
        "target_timezone": timezones[anchor],
        "station_timezones": timezones,
        "run_initialization_utc": run,
        "as_of_utc": clock,
        "station_ids": stations,
        "source_station_id": anchor,
        "market_registry_snapshot_sha256": registry_sha,
        "station_valid_hours": station_valid_hours,
        "valid_hours": valid_hours,
        "capture_config_sha256": content_sha256(capture_config),
        "model_run_contract_sha256": contract["contract_sha256"],
        "resource_budget": normalize_resource_budget(
            capture_config["resource_budget"]
        ),
        "authority": {
            "source_role": "official_native",
            "native_arrival_index_eligible": True,
            "formal_p0_eligible_without_strict_oos": False,
            "counts_as_independent_family_from_ifs": False,
            "global_raw_transport_anchor_only": True,
            "registry_station_fanout_required": True,
        },
        "boundary": {
            "target_only": True,
            "prospective_only": True,
            "execution_mode": "production_no_live",
            "writes_target_database": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "promotion_allowed": False,
        },
    }
    return {
        **identity,
        "capture_config": capture_config,
        "model_run_contract": contract,
        "plan_sha256": content_sha256(identity),
    }


def _read_index(index_url: str, *, timeout_sec: float) -> bytes:
    request = Request(
        index_url,
        headers={"User-Agent": "weather-aifs-native-forward/1"},
    )
    with open_official_https(request, timeout_sec=timeout_sec) as response:
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_INDEX_BYTES:
            raise ContractError("aifs_forward_index_too_large")
        raw = response.read(MAX_INDEX_BYTES + 1)
    if not raw or len(raw) > MAX_INDEX_BYTES:
        raise ContractError("aifs_forward_index_too_large")
    return raw


def _temperature_range(index_raw: bytes, *, forecast_hour: int) -> tuple[int, int, str]:
    matches: list[Mapping[str, Any]] = []
    try:
        lines = index_raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ContractError("aifs_forward_index_invalid") from exc
    if len(lines) > 512:
        raise ContractError("aifs_forward_index_row_bound_exceeded")
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ContractError("aifs_forward_index_invalid") from exc
        if (
            isinstance(item, Mapping)
            and item.get("model") == "aifs-single"
            and item.get("stream") == "oper"
            and item.get("type") == "fc"
            and item.get("levtype") == "sfc"
            and item.get("param") == "2t"
            and int(item.get("step") or -1) == int(forecast_hour)
        ):
            matches.append(item)
    if len(matches) != 1:
        raise ContractError("aifs_forward_2t_index_match_invalid")
    start = int(matches[0].get("_offset") or -1)
    length = int(matches[0].get("_length") or 0)
    if start < 0 or length <= 0 or length > MAX_MESSAGE_BYTES:
        raise ContractError("aifs_forward_2t_range_invalid")
    return start, start + length - 1, hashlib.sha256(index_raw).hexdigest()


def _existing_receipt(
    source_root: Path, *, day: str, run: str, valid: str, station: str
) -> Path | None:
    directory = source_root / PROVIDER_ID / "receipts"
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if len(paths) > 5000:
        raise ContractError("aifs_forward_receipt_scan_bound_exceeded")
    for path in paths:
        try:
            value = load_bounded_json(path)
        except Exception:
            continue
        if (
            isinstance(value, Mapping)
            and value.get("provider_id") == PROVIDER_ID
            and value.get("station_id") == station
            and value.get("target_local_date") == day
            and value.get("run_initialization_utc") == run
            and value.get("valid_time_utc") == valid
            and value.get("capture_mode") == "prospective"
        ):
            # A receipt alone is not reusable evidence: the exact
            # content-addressed raw range has to remain available and still
            # hash to the value that the native arrival receipt commits to.
            # Otherwise a later fanout could turn a stale JSON receipt into
            # apparent AIFS availability without a decodable source object.
            raw_receipts = value.get("raw_receipts")
            if not isinstance(raw_receipts, list) or len(raw_receipts) != 1:
                continue
            raw = raw_receipts[0]
            if not isinstance(raw, Mapping):
                continue
            expected = str(raw.get("raw_sha256") or "").strip().lower()
            raw_ref = str(raw.get("raw_object_ref") or "").strip()
            if not re.fullmatch(r"[0-9a-f]{64}", expected) or not raw_ref:
                continue
            try:
                raw_path = Path(raw_ref).expanduser().resolve(strict=True)
            except OSError:
                continue
            if not raw_path.is_file() or raw_path.is_symlink():
                continue
            digest = hashlib.sha256()
            try:
                with raw_path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError:
                continue
            if digest.hexdigest() != expected:
                continue
            return path.resolve()
    return None


def _global_transport_lineage_receipt(
    receipt_path: Path,
    *,
    anchor_station_id: str,
    output_root: Path,
) -> Path:
    """Append an immutable global-raw carrier receipt without copying raw.

    AIFS is a global grid field.  The station used to satisfy the bounded raw
    collector's local-day guard proves only transport eligibility; every
    forecast-station authority is instead the later registry-bound fanout.
    Keep that distinction in a content-addressed derived receipt so neither
    KLAX nor the historical ZUUU anchor can be mistaken for authority.
    """

    source = load_bounded_json(receipt_path)
    if not isinstance(source, Mapping):
        raise ContractError("aifs_forward_transport_receipt_invalid")
    supplied = str(source.get("receipt_content_sha256") or "").strip().lower()
    identity = {
        key: value for key, value in source.items()
        if key != "receipt_content_sha256"
    }
    if supplied != content_sha256(identity):
        raise ContractError("aifs_forward_transport_receipt_sha_mismatch")
    source_sha = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    derived_identity = {
        **identity,
        "global_raw_transport": {
            "global_raw_transport_anchor_only": True,
            "anchor_station_id": str(anchor_station_id).strip().upper(),
            "station_forecast_authority": False,
            "registry_station_fanout_required": True,
        },
        "source_transport_receipt_sha256": source_sha,
    }
    digest = content_sha256(derived_identity)
    destination = (
        output_root / PROVIDER_ID / "receipts" / f"{digest}.json"
    )
    write_bounded_json(
        destination,
        {**derived_identity, "receipt_content_sha256": digest},
    )
    return destination.resolve()


def execute_plan(
    plan: Mapping[str, Any],
    *,
    source_root: Path = DEFAULT_ARRIVAL_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    reuse_only: bool = False,
    incremental_capture_root: Path | None = None,
) -> dict[str, Any]:
    identity = {
        key: value
        for key, value in plan.items()
        if key not in {"capture_config", "model_run_contract", "plan_sha256"}
    }
    if str(plan.get("plan_sha256") or "") != content_sha256(identity):
        raise ContractError("aifs_forward_plan_sha_mismatch")
    if (
        plan.get("provider_id") != PROVIDER_ID
        or plan.get("authority", {}).get("source_role") != "official_native"
        or plan.get("authority", {}).get("formal_p0_eligible_without_strict_oos")
        is not False
        or plan.get("boundary", {}).get("live_submit_authorized") is not False
    ):
        raise ContractError("aifs_forward_plan_authority_invalid")
    configured = normalize_resource_budget(plan["capture_config"]["resource_budget"])
    if configured != dict(plan["resource_budget"]):
        raise ContractError("aifs_forward_resource_budget_mismatch")
    assert_process_memory_budget(limit=configured["max_process_rss_bytes"])
    source = source_root.expanduser().resolve()
    incremental_root = (
        incremental_capture_root.expanduser().resolve()
        if incremental_capture_root is not None
        else source
    )
    destination = (
        output_root.expanduser().resolve()
        / (
            f"{plan['target_local_date']}_"
            f"{str(plan['run_initialization_utc']).replace(':', '').replace('-', '')}_"
            f"{str(plan['plan_sha256'])[:16]}"
        )
    )
    destination.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    anchor = str(plan["source_station_id"])
    for item in plan["valid_hours"]:
        assert_process_memory_budget(limit=configured["max_process_rss_bytes"])
        assert_target_raw_retention_budget(
            # The global collection root also retains unrelated provider
            # evidence.  Reuse-only must validate the exact AIFS corpus it
            # reads, not fail because an unrelated model used its own bound.
            (source if reuse_only else incremental_root) / PROVIDER_ID,
            bytes_needed=0 if reuse_only else MAX_MESSAGE_BYTES,
            limit=configured["max_raw_bytes"],
            min_free_bytes=configured["min_free_bytes"],
        )
        valid = str(item["valid_time_utc"])
        valid_stations = [
            str(station_id).strip().upper()
            for station_id in item.get("station_ids") or []
            if str(station_id).strip()
        ]
        raw_anchor = anchor if anchor in valid_stations else valid_stations[0]
        existing_station = raw_anchor
        existing = _existing_receipt(
            source,
            day=str(plan["target_local_date"]),
            run=str(plan["run_initialization_utc"]),
            valid=valid,
            station=raw_anchor,
        )
        # The four immutable pre-fix transport receipts were all physically
        # anchored at ZUUU.  They remain reusable global raw evidence, but
        # their new station arrivals are emitted only through the registry
        # map below; never rewrite those old receipts.
        if existing is None and raw_anchor != anchor:
            existing = _existing_receipt(
                source,
                day=str(plan["target_local_date"]),
                run=str(plan["run_initialization_utc"]),
                valid=valid,
                station=anchor,
            )
            if existing is not None:
                existing_station = anchor
        if existing is None and incremental_root != source:
            existing = _existing_receipt(
                incremental_root,
                day=str(plan["target_local_date"]),
                run=str(plan["run_initialization_utc"]),
                valid=valid,
                station=raw_anchor,
            )
        if existing is not None:
            results.append({
                "valid_time_utc": valid,
                "forecast_hour": int(item["forecast_hour"]),
                "source_station_id": existing_station,
                "status": "ALREADY_CAPTURED",
                "transport_receipt_path": str(existing),
            })
            continue
        if reuse_only:
            results.append({
                "valid_time_utc": valid,
                "forecast_hour": int(item["forecast_hour"]),
                "source_station_id": raw_anchor,
                "status": "MISSING_RETAINED_NATIVE_RAW",
                "reason_codes": ["aifs_forward_reuse_only_missing_raw"],
            })
            continue
        index_raw = _read_index(str(item["index_url"]), timeout_sec=timeout_sec)
        start, end, index_sha = _temperature_range(
            index_raw,
            forecast_hour=int(item["forecast_hour"]),
        )
        captured = capture(
            provider_id=PROVIDER_ID,
            urls=[str(item["grib_url"])],
            # Bind publication time to the selected native GRIB range itself,
            # not to the neighbouring .index object used to locate the range.
            publication_url=None,
            station_id=raw_anchor,
            target_local_date=str(plan["target_local_date"]),
            run_initialization_utc=str(plan["run_initialization_utc"]),
            valid_time_utc=valid,
            decision_at_utc=None,
            target_timezone=str(plan["station_timezones"][raw_anchor]),
            output_root=incremental_root,
            timeout_sec=timeout_sec,
            max_response_bytes=min(
                int(configured["max_response_bytes"]), MAX_MESSAGE_BYTES
            ),
            max_capture_bytes=int(configured["max_capture_bytes"]),
            min_free_bytes=int(configured["min_free_bytes"]),
            range_start=start,
            range_end=end,
            run_contract=plan["model_run_contract"],
        )
        results.append({
            "valid_time_utc": valid,
            "forecast_hour": int(item["forecast_hour"]),
            "source_station_id": raw_anchor,
            "status": captured.get("status"),
            "transport_receipt_path": captured.get("transport_receipt_path"),
            "index_sha256": index_sha,
            "range_start": start,
            "range_end": end,
            "reason_codes": (captured.get("receipt") or {}).get(
                "reason_codes", []
            ),
        })
    reusable = [
        item
        for item in results
        if item["status"] in {"CAPTURED", "ALREADY_CAPTURED"}
    ]
    # Materialize small, append-only lineage envelopes only.  The content
    # addressed native GRIB objects remain where they were first retained.
    for item in reusable:
        receipt_text = str(item.get("transport_receipt_path") or "").strip()
        if not receipt_text:
            raise ContractError("aifs_forward_transport_receipt_missing")
        item["transport_lineage_receipt_path"] = str(
            _global_transport_lineage_receipt(
                Path(receipt_text).expanduser().resolve(),
                anchor_station_id=str(item["source_station_id"]),
                output_root=incremental_root,
            )
        )
    decision_at = _now_utc()
    fanout_result = None
    if reusable:
        fanout_result = fanout(
            provider=PROVIDER_ID,
            target_local_date=str(plan["target_local_date"]),
            run_initialization_utc=str(plan["run_initialization_utc"]),
            decision_at_utc=decision_at,
            target_timezone=str(plan["target_timezone"]),
            station_timezones=plan["station_timezones"],
            station_valid_times={
                station_id: [
                    str(item["valid_time_utc"])
                    for item in rows
                ]
                for station_id, rows in plan["station_valid_hours"].items()
            },
            station_ids=[str(value) for value in plan["station_ids"]],
            source_station_id=anchor,
            source_station_ids=sorted({
                anchor,
                *(str(item.get("source_station_id") or anchor) for item in results),
            }),
            source_root=source,
            supplemental_source_roots=(
                [incremental_root] if incremental_root != source else None
            ),
            output_dir=destination / "station_arrivals",
        )
    result_identity = {
        "schema": SCHEMA,
        "state": (
            "CAPTURED_NATIVE_FORWARD_CANDIDATE"
            if len(reusable) == len(results)
            else "BLOCKED_FAIL_CLOSED"
        ),
        "plan_sha256": plan["plan_sha256"],
        "provider_id": PROVIDER_ID,
        "product_id": PRODUCT_ID,
        "independence_family_id": MODEL_FAMILY_ID,
        "target_local_date": plan["target_local_date"],
        "station_timezones": dict(plan["station_timezones"]),
        "station_valid_hours": {
            str(station_id): list(rows)
            for station_id, rows in plan["station_valid_hours"].items()
        },
        "run_initialization_utc": plan["run_initialization_utc"],
        "capture_results": results,
        "captured_count": sum(item["status"] == "CAPTURED" for item in results),
        "reused_count": sum(
            item["status"] == "ALREADY_CAPTURED" for item in results
        ),
        "blocked_count": sum(
            item["status"] not in {"CAPTURED", "ALREADY_CAPTURED"}
            for item in results
        ),
        "fanout": fanout_result,
        "authority": dict(plan["authority"]),
        "boundary": dict(plan["boundary"]),
        "execution_decision_at_utc": decision_at,
        "incremental_capture_root": str(incremental_root),
        "global_raw_transport_anchor_only": True,
    }
    result = {
        **result_identity,
        "content_sha256": content_sha256(result_identity),
    }
    write_bounded_json(destination / "run_result.json", result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date")
    parser.add_argument("--run-initialization-utc")
    parser.add_argument("--as-of-utc")
    parser.add_argument("--station-id", action="append", dest="station_ids")
    parser.add_argument("--source-station-id", default="ZUUU")
    parser.add_argument("--target-timezone", default=TARGET_TIMEZONE)
    parser.add_argument(
        "--station-timezone",
        action="append",
        default=[],
        metavar="STATION=IANA_TIMEZONE",
    )
    parser.add_argument("--market-registry-snapshot", type=Path)
    parser.add_argument("--market-registry-snapshot-sha256", default="")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_ARRIVAL_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--timeout-sec", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--reuse-only", action="store_true")
    parser.add_argument("--incremental-capture-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        station_timezones: dict[str, str] = {}
        for item in args.station_timezone:
            station_id, separator, timezone_name = str(item).partition("=")
            if not separator or not station_id.strip() or not timezone_name.strip():
                parser.error("--station-timezone must be STATION=IANA_TIMEZONE")
            station_timezones[station_id.strip().upper()] = timezone_name.strip()
        plan = build_plan(
            event_date=args.event_date,
            run_initialization_utc=args.run_initialization_utc,
            as_of_utc=args.as_of_utc,
            station_ids=args.station_ids,
            source_station_id=args.source_station_id,
            target_timezone=args.target_timezone,
            station_timezones=station_timezones or None,
            market_registry_snapshot_path=args.market_registry_snapshot,
            market_registry_snapshot_sha256=args.market_registry_snapshot_sha256,
        )
        result = (
            execute_plan(
                plan,
                source_root=args.source_root,
                output_root=args.output_root,
                timeout_sec=args.timeout_sec,
                reuse_only=args.reuse_only,
                incremental_capture_root=args.incremental_capture_root,
            )
            if args.execute
            else {"state": "PLAN_ONLY", **plan}
        )
        output = args.output.expanduser().resolve() if args.output else (
            args.output_root.expanduser().resolve() / "latest_plan_or_result.json"
        )
        write_bounded_json(output, result)
        print(json.dumps({
            "state": result.get("state"),
            "provider_id": PROVIDER_ID,
            "target_local_date": result.get("target_local_date"),
            "run_initialization_utc": result.get("run_initialization_utc"),
            "valid_time_count": len(result.get("valid_hours") or result.get("capture_results") or []),
            "captured_count": result.get("captured_count", 0),
            "reused_count": result.get("reused_count", 0),
            "blocked_count": result.get("blocked_count", 0),
            "output": str(output),
            "writes_target_database": False,
            "calls_order_gateway": False,
            "promotion_allowed": False,
        }, ensure_ascii=False, sort_keys=True))
        return 0 if not args.execute or result.get("blocked_count", 0) == 0 else 2
    except Exception as exc:
        blocked = {
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "blockers": [f"{type(exc).__name__}:{exc}"],
            "provider_id": PROVIDER_ID,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "promotion_allowed": False,
        }
        if args.output:
            write_bounded_json(args.output.expanduser().resolve(), blocked)
        print(json.dumps(blocked, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
