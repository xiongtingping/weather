#!/usr/bin/env python3
"""Collect a direct ECMWF Open Data IFS subset for the ZUUU weak source.

Run this script with the dedicated ``weather-official-nwp-venv`` interpreter.
It uses ECMWF's official request-based Open Data client, not a mirrored whole
file or an aggregator.  Two server-side subsets (2m temperature and pressure
levels) are immutable-receipted, concatenated only for local ecCodes decoding,
and published as one provider snapshot outside the posterior hot path.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import sys
import tempfile
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_snapshot_runtime import (  # noqa: E402
    assemble_grib2_payload,
    build_and_persist_official_nwp_snapshot,
    decode_grib2_nearest_point,
)
from official_nwp_request_control import (  # noqa: E402
    record_provider_response,
    request_control_path as shared_request_control_path,
    reserve_request_slot,
)
from official_nwp_https_transport import configure_official_requests_session  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _as_url_ranges(result: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in getattr(result, "urls", []) or []:
        try:
            url, ranges = item
        except (TypeError, ValueError):
            continue
        rows.append({"url": str(url), "byte_ranges": [list(pair) for pair in ranges or []]})
    return rows


def _bounded_float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _retrieve(
    client: Any,
    *,
    request: dict[str, Any],
    target: Path,
    request_control_path: Path,
    min_request_interval_sec: float,
    wall_clock_sec: float,
) -> tuple[Any, dict[str, Any]]:
    """Fetch exact official index ranges while observing transport headers.

    ``ecmwf.opendata.Client.retrieve`` hides response headers inside its
    downloader.  P1 Tier-A cannot infer them after the fact, so resolve the
    same official URL/range plan and perform each HTTPS range request through
    the client's certificate-verified session ourselves.
    """

    # ecmwf-opendata delegates index lookup to multiurl.robust with a
    # library default of 500 attempts and 120 seconds between attempts.  A
    # DNS/503 incident would therefore pin the resident child for hours and
    # invite another collector to overlap.  Keep the official transport but
    # bound only the planner's index retry loop; payload requests below keep
    # their explicit observed request/Retry-After path.
    import ecmwf.opendata.client as ecmwf_client_module
    from multiurl.retry import robust as multiurl_robust

    max_attempts = _bounded_int_env("PM_ECMWF_INDEX_MAX_RETRIES", 3, minimum=1, maximum=4)
    retry_after = _bounded_float_env(
        "PM_ECMWF_INDEX_RETRY_AFTER_SEC", 2.0, minimum=0.0, maximum=10.0
    )
    index_timeout_sec = _bounded_float_env(
        "PM_ECMWF_INDEX_TIMEOUT_SEC", 15.0, minimum=1.0, maximum=30.0
    )
    index_wall_clock_sec = min(
        float(wall_clock_sec),
        _bounded_float_env(
            "PM_ECMWF_INDEX_WALL_CLOCK_SEC", 45.0, minimum=5.0, maximum=90.0
        ),
    )
    payload_timeout_sec = _bounded_float_env(
        "PM_ECMWF_PAYLOAD_TIMEOUT_SEC", 30.0, minimum=2.0, maximum=60.0
    )
    payload_attempts = _bounded_int_env(
        "PM_ECMWF_PAYLOAD_MAX_RETRIES", 2, minimum=1, maximum=3
    )
    retry_sleep_cap_sec = _bounded_float_env(
        "PM_ECMWF_RETRY_SLEEP_CAP_SEC", 30.0, minimum=1.0, maximum=120.0
    )
    cooldown_cap_sec = _bounded_float_env(
        "PM_ECMWF_PROVIDER_COOLDOWN_CAP_SEC", 300.0, minimum=5.0, maximum=900.0
    )
    deadline = time.monotonic() + max(1.0, float(wall_clock_sec))

    def _deadline_sleep(seconds: float) -> None:
        remaining = deadline - time.monotonic()
        if seconds > remaining:
            raise TimeoutError("ecmwf_provider_retry_deadline_exceeded")
        if seconds > 0.0:
            time.sleep(seconds)

    def _reserve_slot() -> dict[str, Any]:
        if time.monotonic() >= deadline:
            raise TimeoutError("ecmwf_provider_retry_deadline_exceeded")
        return reserve_request_slot(
            path=request_control_path,
            provider_id="ecmwf",
            min_interval_sec=min_request_interval_sec,
            sleep_fn=_deadline_sleep,
        )

    original_robust = ecmwf_client_module.robust
    original_get = client.session.get

    def bounded_robust(call: Any) -> Any:
        return multiurl_robust(
            call,
            maximum_tries=max_attempts,
            retry_after=retry_after,
            use_server_retry_after=True,
        )

    def bounded_get(url: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", index_timeout_sec)
        return original_get(url, *args, **kwargs)

    ecmwf_client_module.robust = bounded_robust
    client.session.get = bounded_get
    timeout_supported = threading.current_thread() is threading.main_thread()
    previous_alarm = signal.getitimer(signal.ITIMER_REAL) if timeout_supported else (0.0, 0.0)
    previous_handler = signal.getsignal(signal.SIGALRM) if timeout_supported else signal.SIG_DFL
    if timeout_supported:
        def alarm_handler(_signum: int, _frame: Any) -> None:
            raise TimeoutError("ecmwf_official_index_wall_clock_timeout")

        signal.signal(signal.SIGALRM, alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, index_wall_clock_sec)
    try:
        result = client._get_urls(request, target=str(target), use_index=True)  # noqa: SLF001 - official client range planner
    finally:
        if timeout_supported:
            signal.setitimer(signal.ITIMER_REAL, *previous_alarm)
            signal.signal(signal.SIGALRM, previous_handler)
        ecmwf_client_module.robust = original_robust
        client.session.get = original_get
    import requests

    fetched: list[tuple[int, bytes, dict[str, Any]]] = []
    for sequence, (url, raw_ranges) in enumerate(getattr(result, "urls", []) or []):
        ranges = [(int(offset), int(length)) for offset, length in (raw_ranges or ())]
        groups = (
            [[item] for item in ranges]
            if ranges and getattr(client.source, "accept_multiple_ranges", None) is False
            else [ranges]
        )
        for group_sequence, request_ranges in enumerate(groups):
            range_header = ",".join(f"{offset}-{offset + length - 1}" for offset, length in request_ranges)
            headers = {"User-Agent": "weather-ecmwf-official-range-collector/1"}
            if range_header:
                headers["Range"] = f"bytes={range_header}"
            slot = _reserve_slot()
            requested_at = _now()
            response = None
            for attempt in range(payload_attempts):
                try:
                    response = None
                    with requests.Session() as session:
                        configure_official_requests_session(session)
                        response = session.get(
                            str(url),
                            headers=headers,
                            timeout=payload_timeout_sec,
                            stream=True,
                        )
                        response_started_at = _now()
                        response_headers = dict(response.headers.items())
                        response.raise_for_status()
                        body = response.content
                        response_completed_at = _now()
                    control = record_provider_response(
                        path=request_control_path,
                        provider_id="ecmwf",
                        status_code=int(response.status_code),
                        headers=response_headers,
                        max_cooldown_sec=cooldown_cap_sec,
                    )
                    break
                except requests.RequestException as exc:
                    response_headers = dict(getattr(response, "headers", {}) or {})
                    status_code = getattr(response, "status_code", None)
                    control = record_provider_response(
                        path=request_control_path,
                        provider_id="ecmwf",
                        status_code=int(status_code) if status_code is not None else None,
                        headers=response_headers,
                        error=type(exc).__name__,
                        max_cooldown_sec=cooldown_cap_sec,
                    )
                    if status_code is not None and int(status_code) not in {408, 425, 429, 500, 502, 503, 504}:
                        raise
                    if attempt >= payload_attempts - 1:
                        raise
                    retry_wait = max(float(2**attempt), float(control.get("cooldown_sec") or 0.0))
                    if retry_wait > retry_sleep_cap_sec:
                        raise TimeoutError("ecmwf_provider_retry_deferred_by_cooldown")
                    _deadline_sleep(retry_wait)
                    _reserve_slot()
            if len(request_ranges) == 1:
                ordered_body = body
            elif len(request_ranges) > 1:
                content_type = str(response.headers.get("content-type") or "")
                if not content_type.lower().startswith("multipart/byteranges"):
                    raise RuntimeError("ecmwf_official_multirange_response_not_multipart")
                message = BytesParser(policy=policy.default).parsebytes(
                    f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body
                )
                parts: dict[int, bytes] = {}
                for part in message.iter_parts():
                    content_range = str(part.get("Content-Range") or "")
                    try:
                        start = int(content_range.split()[1].split("-")[0])
                    except (IndexError, ValueError):
                        raise RuntimeError("ecmwf_official_multirange_content_range_invalid") from None
                    parts[start] = part.get_payload(decode=True) or b""
                if any(len(parts.get(offset, b"")) != length for offset, length in request_ranges):
                    raise RuntimeError("ecmwf_official_multirange_length_mismatch")
                ordered_body = b"".join(parts[offset] for offset, _length in request_ranges)
            else:
                ordered_body = body
            if request_ranges and len(ordered_body) != sum(length for _offset, length in request_ranges):
                raise RuntimeError("ecmwf_official_range_length_mismatch")
            fetched.append((sequence * 1000 + group_sequence, ordered_body, {
                "url": str(url),
                "byte_ranges": [[offset, length] for offset, length in request_ranges],
                "http_status": int(response.status_code),
                "requested_at": requested_at,
                "response_started_at": response_started_at,
                "response_completed_at": response_completed_at,
                "response_headers": {
                    key.lower(): value
                    for key, value in response_headers.items()
                    if key.lower() in {"content-type", "content-range", "content-length", "etag", "last-modified"}
                },
                "provider_slot_wait_sec": float(slot.get("wait_sec") or 0.0),
                "provider_retry_after_sec": control.get("retry_after_sec"),
            }))
    receipts = [row[2] for row in fetched]
    with target.open("wb") as output:
        for _sequence, body, _receipt in fetched:
            output.write(body)
    if not receipts or not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("ecmwf_official_range_transport_empty")
    result.urls = getattr(result, "urls", [])
    return result, {
        "request": request,
        "requested_at": min(row["requested_at"] for row in receipts),
        "response_started_at": min(row["response_started_at"] for row in receipts),
        "response_completed_at": max(row["response_completed_at"] for row in receipts),
        "response_headers_observed": True,
        "url_ranges": _as_url_ranges(result),
        "range_receipts": receipts,
    }


def collect(
    *,
    run_at_utc: str,
    valid_time_utc: str,
    source_root: Path,
    catalog_path: Path,
    min_request_interval_sec: float = 2.0,
) -> dict[str, Any]:
    try:
        from ecmwf.opendata import Client
    except ImportError as exc:
        missing = str(getattr(exc, "name", "") or "unknown").strip()
        return {
            "ok": False,
            "state": "blocked",
            "blockers": [f"ecmwf_opendata_runtime_unavailable:{missing}"],
        }
    run = datetime.fromisoformat(run_at_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    valid = datetime.fromisoformat(valid_time_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    lead_seconds = (valid - run).total_seconds()
    step_hours = int(lead_seconds // 3600)
    if step_hours < 0 or lead_seconds != step_hours * 3600:
        return {"ok": False, "state": "blocked", "blockers": ["ecmwf_run_valid_time_invalid"]}
    temporary_root = Path(tempfile.mkdtemp(prefix="ecmwf-zuuu-"))
    try:
        # ECMWF publishes the same Open Data corpus through its origin and
        # official AWS/Azure/Google replicas.  Prefer the AWS replica because
        # the origin is connection-capped and its client can otherwise enter
        # a 120-second retry loop.  The chosen transport remains explicit in
        # the immutable receipt and every request URL/response header is still
        # observed directly; this is transport redundancy, not a data-source
        # substitution or an aggregator fallback.
        transport_source = str(os.environ.get("PM_ECMWF_OPEN_DATA_SOURCE") or "aws").strip().lower()
        if transport_source not in {"ecmwf", "aws", "azure", "google"}:
            return {"ok": False, "state": "blocked", "blockers": ["ecmwf_official_transport_source_invalid"]}
        client = Client(source=transport_source, model="ifs", resol="0p25")
        # Official Open Data transport uses a certificate-verified session.
        # It ignores ambient proxy variables, but honors the one explicit
        # PM_OFFICIAL_NWP_PROXY tunnel used by the workstation when needed.
        configure_official_requests_session(client.session)
        request_control_state_path = shared_request_control_path(source_root, "ecmwf")
        retrieve_wall_clock_sec = _bounded_float_env(
            "PM_ECMWF_RETRIEVE_WALL_CLOCK_SEC", 120.0, minimum=15.0, maximum=300.0
        )
        shared = {"date": run.date().isoformat(), "time": run.hour, "stream": "oper", "type": "fc", "step": step_hours}
        surface_path = temporary_root / "surface.grib2"
        pressure_path = temporary_root / "pressure.grib2"
        # All fields are direct IFS Open Data parameters.  Accumulated TP
        # retains its GRIB start/end-step metadata in the decoder trace.
        surface_result, surface_receipt = _retrieve(
            client,
            request={**shared, "param": ["2t", "tcc", "tp", "mucape", "10fg", "ewss", "nsss"]},
            target=surface_path,
            request_control_path=request_control_state_path,
            min_request_interval_sec=min_request_interval_sec,
            wall_clock_sec=retrieve_wall_clock_sec,
        )
        pressure_result, pressure_receipt = _retrieve(
            client,
            request={**shared, "levtype": "pl", "levelist": [500, 700, 850], "param": ["t", "r", "gh", "u", "v"]},
            target=pressure_path,
            request_control_path=request_control_state_path,
            min_request_interval_sec=min_request_interval_sec,
            wall_clock_sec=retrieve_wall_clock_sec,
        )
        raw_paths: list[Path] = []
        raw_receipts: list[dict[str, Any]] = []
        for source_path, receipt in ((surface_path, surface_receipt), (pressure_path, pressure_receipt)):
            body = source_path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            stored = source_root / "ecmwf" / "raw" / f"{digest}.grib2"
            if stored.is_file() and stored.read_bytes() != body:
                return {"ok": False, "state": "blocked", "blockers": ["ecmwf_raw_sha256_identity_collision"]}
            if not stored.is_file():
                _atomic_bytes(stored, body)
            urls = [row["url"] for row in receipt["url_ranges"]]
            if not urls:
                return {"ok": False, "state": "blocked", "blockers": ["ecmwf_official_request_url_missing"]}
            raw_paths.append(stored)
            raw_receipts.append({**receipt, "raw_object_ref": str(stored.resolve()), "raw_sha256": digest, "raw_request_urls": urls})
        manifest = {"schema": "ecmwf_open_data_subset_receipt_v2", "provider_id": "ecmwf", "run_at_utc": run_at_utc, "valid_time_utc": valid_time_utc, "official_transport_source": transport_source, "official_cloud_replica": transport_source in {"aws", "azure", "google"}, "raw_receipts": raw_receipts, "open_meteo": False, "aggregator_or_republisher": False}
        manifest_digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        manifest_path = source_root / "ecmwf" / "receipts" / f"{manifest_digest}.json"
        if not manifest_path.is_file():
            _atomic_json(manifest_path, manifest)
        manifest_file_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        combined_path, temporary_combined = assemble_grib2_payload(raw_paths)
        try:
            decoded = decode_grib2_nearest_point(combined_path, provider_id="ecmwf")
        finally:
            temporary_combined.unlink(missing_ok=True)
        raw_urls = [url for receipt in raw_receipts for url in receipt["raw_request_urls"]]
        completed_at = max(str(receipt["response_completed_at"]) for receipt in raw_receipts)
        persisted = build_and_persist_official_nwp_snapshot(
            catalog_path=catalog_path,
            provider_id="ecmwf",
            raw_path=raw_paths[0],
            raw_receipt_path=manifest_path,
            raw_request_url=raw_urls[0],
            raw_request_urls=raw_urls,
            raw_payload_sha256s=[str(receipt["raw_sha256"]) for receipt in raw_receipts],
            run_at_utc=run_at_utc,
            valid_time_utc=valid_time_utc,
            response_completed_at=completed_at,
            requested_at=min(str(receipt.get("requested_at") or "") for receipt in raw_receipts),
            response_started_at=min(str(receipt.get("response_started_at") or "") for receipt in raw_receipts),
            response_headers_observed=all(receipt.get("response_headers_observed") is True for receipt in raw_receipts),
            decoded=decoded,
            current_path=source_root / "ecmwf" / "current.json",
        )
        return {**persisted, "raw_object_ref": str(manifest_path), "raw_sha256": manifest_file_sha256, "request_control_path": str(request_control_state_path), "boundary": {"writes_archive_db": False, "writes_posterior": False, "writes_task_queue": False, "places_orders": False}}
    except Exception as exc:
        return {"ok": False, "state": "blocked", "blockers": [f"ecmwf_official_subset_collect_failed:{type(exc).__name__}"]}
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-at-utc", required=True)
    parser.add_argument("--valid-time-utc", required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT / "data" / "runtime" / "official_nwp_sources")
    parser.add_argument("--catalog", type=Path, default=ROOT / "config" / "forecast_sources" / "official_nwp_zuuu_v1.json")
    args = parser.parse_args(argv)
    result = collect(run_at_utc=args.run_at_utc, valid_time_utc=args.valid_time_utc, source_root=args.source_root.expanduser().resolve(), catalog_path=args.catalog.expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
