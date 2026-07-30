#!/usr/bin/env python3
"""Rehydrate missing target ICON t2m bodies from the official DWD endpoint.

This is a bounded, research-only repair.  It reads existing DWD receipts,
retries only their exact native t2m URLs, and retains a body only after the
response SHA-256 matches the receipt.  A rehydrated body is historical replay
evidence; its download time is never substituted for the original arrival or
publication time and it cannot authorize production calibration by itself.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from official_nwp_https_transport import open_official_https  # noqa: E402


SCHEMA = "bayes_basket_target_icon_history_rehydration_v1"
RECEIPT_SCHEMA = "bayes_basket_target_icon_history_rehydrated_receipt_v1"
PROVIDER_ID = "icon_global_official"
ICON_HOST = "opendata.dwd.de"
TARGET_TIMEZONE = "America/New_York"
DEFAULT_SOURCE_ROOTS = (
    ROOT / "data/runtime/official_nwp_curve_sources_v2/icon",
    ROOT / "data/runtime/official_nwp_sources/icon",
    ROOT / "data/research/bayes_basket_target/model_arrivals/icon_global_official",
)
DEFAULT_OUTPUT_ROOT = ROOT / "data/research/bayes_basket_target/icon_history_rehydrated"
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_RECEIPTS_PER_ROOT = 4096
MAX_URLS = 2048
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
T2M_URL_RE = re.compile(
    r"/t_2m/icon_global_icosahedral_single-level_(?P<run>\d{10})_(?P<lead>\d{3})_T_2M\.grib2\.bz2$"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_json(path: Path) -> Mapping[str, Any]:
    if path.stat().st_size > MAX_RECEIPT_BYTES:
        raise ContractError("icon_history_rehydration_receipt_too_large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ContractError("icon_history_rehydration_receipt_mapping_required")
    return value


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _event_date(valid_time_utc: str) -> str:
    parsed = _parse_timestamp(valid_time_utc)
    if parsed is None:
        raise ContractError("icon_history_rehydration_valid_time_invalid")
    return parsed.astimezone(ZoneInfo(TARGET_TIMEZONE)).date().isoformat()


def _date(value: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ContractError("icon_history_rehydration_date_invalid") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_source_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url.startswith("https://opendata.dwd.de/"):
        raise ContractError("icon_history_rehydration_source_not_allowlisted")
    if T2M_URL_RE.search(url) is None:
        raise ContractError("icon_history_rehydration_not_exact_t2m_url")
    return url


def collect_candidates(
    *,
    source_roots: Iterable[Path],
    start_date: date,
    end_date: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect exact missing t2m URLs without opening network connections."""

    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    stats = Counter()
    for source_root in source_roots:
        receipt_dir = source_root.expanduser().resolve() / "receipts"
        paths = sorted(receipt_dir.glob("*.json")) if receipt_dir.is_dir() else []
        if len(paths) > MAX_RECEIPTS_PER_ROOT:
            paths = paths[:MAX_RECEIPTS_PER_ROOT]
            stats["receipt_cap_applied"] += 1
        for receipt_path in paths:
            stats["receipt_files_scanned"] += 1
            try:
                receipt = _load_json(receipt_path)
            except (OSError, UnicodeDecodeError, ValueError, ContractError):
                stats["invalid_receipts"] += 1
                continue
            run = str(receipt.get("run_initialization_utc") or receipt.get("run_at_utc") or "").strip()
            valid = str(receipt.get("valid_time_utc") or "").strip()
            if not run or not valid:
                continue
            try:
                event_date = _event_date(valid)
            except ContractError:
                stats["invalid_valid_times"] += 1
                continue
            if not start_date.isoformat() <= event_date <= end_date.isoformat():
                continue
            raw_receipts = receipt.get("raw_receipts")
            if not isinstance(raw_receipts, list):
                continue
            for raw in raw_receipts:
                if not isinstance(raw, Mapping):
                    continue
                try:
                    url = _safe_source_url(raw.get("raw_request_url"))
                except ContractError:
                    continue
                expected = str(raw.get("raw_sha256") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    stats["invalid_hashes"] += 1
                    continue
                match = T2M_URL_RE.search(url)
                assert match is not None
                key = (run, valid, expected)
                if key in by_key:
                    continue
                local_ref = Path(str(raw.get("raw_object_ref") or "")).expanduser()
                try:
                    local_ref = local_ref.resolve()
                except OSError:
                    pass
                intact = local_ref.is_file()
                if intact:
                    try:
                        intact = _sha256(local_ref) == expected
                    except OSError:
                        intact = False
                if intact:
                    stats["already_intact"] += 1
                    continue
                by_key[key] = {
                    "run_initialization_utc": run,
                    "valid_time_utc": valid,
                    "event_date": event_date,
                    "forecast_hour": int(match.group("lead")),
                    "source_url": url,
                    "expected_sha256": expected,
                    "original_receipt_path": str(receipt_path.resolve()),
                    "original_received_at_utc": str(raw.get("response_completed_at") or ""),
                    "original_published_at_utc": str(raw.get("response_headers", {}).get("last-modified") or ""),
                }
    candidates = sorted(by_key.values(), key=lambda row: (row["event_date"], row["run_initialization_utc"], row["forecast_hour"], row["source_url"]))
    if len(candidates) > MAX_URLS:
        stats["candidate_cap_applied"] += 1
        candidates = candidates[:MAX_URLS]
    stats["candidate_count"] = len(candidates)
    return candidates, dict(stats)


def _header_size(response: Any) -> int | None:
    raw = str(getattr(response, "headers", {}).get("Content-Length") or "").strip()
    if not raw:
        return None


def _preflight_one(candidate: Mapping[str, Any], *, timeout_sec: float) -> tuple[str, dict[str, Any]]:
    """Probe one exact DWD object without retaining a response body."""

    url = str(candidate["source_url"])
    try:
        request = Request(url, method="HEAD", headers={"User-Agent": "bayes-basket-target-icon-history-rehydrator/1"})
        with open_official_https(request, timeout_sec=timeout_sec) as response:
            advertised = _header_size(response)
            if advertised is not None and advertised > MAX_RESPONSE_BYTES:
                return "BLOCKED_SIZE", {"reason": "icon_history_rehydration_response_too_large", "content_length_bytes": advertised}
            return "AVAILABLE", {"content_length_bytes": advertised}
    except HTTPError as exc:
        return "UNAVAILABLE", {"reason": "http_error", "http_status": int(exc.code)}
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return "ERROR", {"reason": type(exc).__name__}
    try:
        return int(raw)
    except ValueError:
        return None


def _download_one(
    candidate: Mapping[str, Any],
    *,
    output_root: Path,
    remaining_bytes: int,
    timeout_sec: float,
    preflight: tuple[str, Mapping[str, Any]] | None = None,
) -> tuple[str, dict[str, Any], int]:
    """Return status, compact evidence, and bytes newly retained."""

    url = str(candidate["source_url"])
    expected = str(candidate["expected_sha256"])
    if preflight is not None and preflight[0] != "AVAILABLE":
        return preflight[0], dict(preflight[1]), 0
    raw_dir = output_root / "raw"
    receipt_dir = output_root / "receipts"
    raw_dir.mkdir(parents=True, exist_ok=True)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    final_path = raw_dir / f"{expected}.grib2.bz2"
    if final_path.is_file() and _sha256(final_path) == expected:
        return "ALREADY_INTACT", {"source_url": url}, 0
    if shutil.disk_usage(output_root).free < MIN_FREE_BYTES:
        return "BLOCKED_RESOURCE", {"reason": "icon_history_rehydration_min_free_space"}, 0
    try:
        request = Request(url, method="GET", headers={"User-Agent": "bayes-basket-target-icon-history-rehydrator/1"})
        with open_official_https(request, timeout_sec=timeout_sec) as response:
            advertised = _header_size(response)
            if advertised is not None and advertised > MAX_RESPONSE_BYTES:
                return "BLOCKED_SIZE", {"reason": "icon_history_rehydration_response_too_large", "content_length_bytes": advertised}, 0
            temporary = raw_dir / f".{expected}.partial.{os.getpid()}"
            digest = hashlib.sha256()
            total = 0
            try:
                with temporary.open("wb") as handle:
                    while True:
                        chunk = bytes(response.read(CHUNK_BYTES) or b"")
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES or total > remaining_bytes:
                            raise ContractError("icon_history_rehydration_response_budget_exceeded")
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                actual = digest.hexdigest()
                if actual != expected:
                    temporary.unlink(missing_ok=True)
                    return "BLOCKED_HASH", {"reason": "icon_history_rehydration_sha256_mismatch", "actual_sha256": actual}, 0
                os.replace(temporary, final_path)
            finally:
                temporary.unlink(missing_ok=True)
    except HTTPError as exc:
        return "UNAVAILABLE", {"reason": "http_error", "http_status": int(exc.code)}, 0
    except (URLError, TimeoutError, OSError, ContractError, ValueError) as exc:
        return "ERROR", {"reason": type(exc).__name__}, 0

    identity = {
        "schema": RECEIPT_SCHEMA,
        "provider_id": PROVIDER_ID,
        "model_id": "ICON",
        "run_initialization_utc": candidate["run_initialization_utc"],
        "valid_time_utc": candidate["valid_time_utc"],
        "target_local_date": candidate["event_date"],
        "source_url": url,
        "raw_receipts": [{
            "raw_request_url": url,
            "raw_object_ref": str(final_path.resolve()),
            "raw_sha256": expected,
            "rehydrated_at_utc": _utc_now(),
            "original_received_at_utc": candidate.get("original_received_at_utc", ""),
            "original_published_at_utc": candidate.get("original_published_at_utc", ""),
        }],
        "capture_mode": "historical_rehydration",
        "historical_arrival_time_observed": False,
        "production_authority": False,
        "live_order_authorized": False,
        "writes_target_database": False,
        "calls_order_gateway": False,
    }
    receipt = {**identity, "receipt_sha256": content_sha256(identity)}
    receipt_path = receipt_dir / f"{content_sha256(identity)}.json"
    write_bounded_json(receipt_path, receipt)
    return "REHYDRATED", {"source_url": url, "raw_object_ref": str(final_path.resolve()), "receipt_path": str(receipt_path.resolve())}, total


def rehydrate(
    *,
    event_dates: Iterable[str],
    source_roots: Iterable[Path] = DEFAULT_SOURCE_ROOTS,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    max_urls: int = MAX_URLS,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    timeout_sec: float = 20.0,
) -> dict[str, Any]:
    dates = sorted({_date(value) for value in event_dates})
    if not dates:
        raise ContractError("icon_history_rehydration_event_dates_empty")
    start_date, end_date = dates[0], dates[-1]
    candidates, scan_stats = collect_candidates(source_roots=source_roots, start_date=start_date, end_date=end_date)
    candidates = candidates[: max(1, min(int(max_urls), MAX_URLS))]
    counts = Counter()
    retained_bytes = 0
    preflight_results: dict[int, tuple[str, dict[str, Any]]] = {}
    # DWD keeps the ICON tree rolling.  A bounded preflight avoids opening
    # multi-megabyte GETs for old 404 objects and caps network concurrency.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="icon-history-head") as executor:
        futures = {
            executor.submit(_preflight_one, candidate, timeout_sec=min(float(timeout_sec), 5.0)): index
            for index, candidate in enumerate(candidates)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                preflight_results[index] = future.result()
            except Exception as exc:  # pragma: no cover - transport boundary
                preflight_results[index] = ("ERROR", {"reason": type(exc).__name__})
    for index, candidate in enumerate(candidates):
        if retained_bytes >= max_total_bytes:
            counts["BLOCKED_TOTAL_BUDGET"] += 1
            continue
        status, evidence, added = _download_one(
            candidate,
            output_root=output_root.expanduser().resolve(),
            remaining_bytes=max_total_bytes - retained_bytes,
            timeout_sec=timeout_sec,
            preflight=preflight_results.get(index),
        )
        counts[status] += 1
        retained_bytes += added
    core = {
        "schema": SCHEMA,
        "rehydrated_at_utc": _utc_now(),
        "event_dates": [item.isoformat() for item in dates],
        "source_roots": [str(Path(item).expanduser().resolve()) for item in source_roots],
        "output_root": str(output_root.expanduser().resolve()),
        "scan": scan_stats,
        "attempted_count": len(candidates),
        "status_counts": dict(sorted(counts.items())),
        "retained_bytes_this_run": retained_bytes,
        "resource_budget": {
            "max_urls": int(max_urls),
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "max_total_bytes": int(max_total_bytes),
            "min_free_bytes": MIN_FREE_BYTES,
        },
        "boundary": {
            "target_only": True,
            "offline_research_only": True,
            "historical_arrival_time_not_rewritten": True,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "production_calibration_eligible": False,
            "protected_production_line_touched": False,
        },
    }
    result = {**core, "receipt_sha256": content_sha256(core)}
    output_root.mkdir(parents=True, exist_ok=True)
    write_bounded_json(output_root / "rehydration_audit_latest.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-date", action="append", required=True, help="closed ET date; repeatable")
    parser.add_argument("--source-root", action="append", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--max-urls", type=int, default=MAX_URLS)
    parser.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_BYTES)
    parser.add_argument("--timeout-sec", type=float, default=20.0)
    args = parser.parse_args()
    result = rehydrate(
        event_dates=args.event_date,
        source_roots=args.source_root or DEFAULT_SOURCE_ROOTS,
        output_root=args.output_root,
        max_urls=args.max_urls,
        max_total_bytes=args.max_total_bytes,
        timeout_sec=args.timeout_sec,
    )
    print(json.dumps({
        "schema": result["schema"],
        "event_dates": result["event_dates"],
        "attempted_count": result["attempted_count"],
        "status_counts": result["status_counts"],
        "retained_bytes_this_run": result["retained_bytes_this_run"],
        "output_root": result["output_root"],
        "production_calibration_eligible": False,
        "protected_production_line_touched": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
