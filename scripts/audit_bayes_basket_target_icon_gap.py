#!/usr/bin/env python3
"""Audit current ICON availability separately from the historical-data gap.

This is a read-only target-lane audit.  It never writes SQLite, touches the
protected production line, downloads GRIB payloads, or changes an activation
pointer.  Network probing, when explicitly requested, reads only bounded DWD
directory metadata and records no listing body.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import MODEL_IDS, model_set_hash  # noqa: E402
from bayes_basket_target.et_history import MODEL_PROVIDERS, event_day_window  # noqa: E402
from bayes_basket_target.model_arrival import ALLOWED_PROVIDERS  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


SCHEMA = "bayes_basket_target_icon_gap_audit_v1"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_icon_gap_audit.json"
DEFAULT_FORWARD_FANOUT = ROOT / "output/audits/bayes_basket_target_model_arrival_evidence_icon_20260724_00z_fanout.json"
# This receipt contains the current schema's bounded exact-file probes.  The
# similarly named ``..._20260725.json`` is an older directory-only receipt and
# must not be used as the current authority, otherwise the audit repeatedly
# reports that exact probing never happened.
DEFAULT_OFFICIAL_ARCHIVE_PROBE = ROOT / "output/audits/bayes_basket_target_official_historical_archive_probe_20260725_dwd.json"
DEFAULT_ET_HISTORY = ROOT / "output/audits/bayes_basket_target_et_history_evidence_20260724_current_rebuilt.json"
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 128 * 1024
MAX_RECEIPT_FILES = 4096
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
DWD_BASE = "https://opendata.dwd.de/weather/nwp/icon/grib/00/"
TARGET_CAPTURE_CONFIG = ROOT / "config/bayes_basket_target/model_arrival_capture_v1.json"
EXPECTED_ARRIVAL_PROVIDERS = {
    "ecmwf_ifs025_official",
    "gfs_0p25_official",
    "icon_global_official",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load(path: Path) -> Any:
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"audit_input_too_large:{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _file_summary(root: Path, *, read_json_history: bool = False) -> dict[str, Any]:
    files = [item for item in root.rglob("*") if item.is_file()] if root.exists() else []
    result: dict[str, Any] = {
        "exists": root.exists(),
        "file_count": len(files),
        "bytes": sum(item.stat().st_size for item in files),
    }
    if not read_json_history:
        return result
    history = root / "history"
    rows: list[dict[str, Any]] = []
    if history.is_dir():
        for path in history.glob("*.json"):
            try:
                value = _load(path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                rows.append(value)
    runs = sorted({str(row.get("run_at_utc") or "") for row in rows if row.get("run_at_utc")})
    valid = sorted({str(row.get("valid_time_utc") or "") for row in rows if row.get("valid_time_utc")})
    stations = sorted({str(row.get("station_id") or "").upper() for row in rows if row.get("station_id")})
    result.update({
        "history_rows": len(rows),
        "history_run_min": runs[0] if runs else "",
        "history_run_max": runs[-1] if runs else "",
        "history_valid_min": valid[0] if valid else "",
        "history_valid_max": valid[-1] if valid else "",
        "history_station_count": len(stations),
        "history_station_ids": stations[:100],
        "history_schemas": dict(Counter(str(row.get("schema") or "") for row in rows)),
    })
    return result


def _probe(url: str) -> dict[str, Any]:
    """Probe only bounded directory metadata, preferring direct curl.

    The desktop Python runtime may inherit a broken local proxy even when the
    host's direct HTTPS path is healthy.  curl is used first with an explicit
    no-proxy boundary; urllib remains a read-only fallback for environments
    without curl.  No response body is retained.
    """
    try:
        completed = subprocess.run(
            [
                "curl", "--noproxy", "*", "--connect-timeout", "10",
                "--max-time", "20", "--max-filesize", str(MAX_INDEX_BYTES),
                "-sS", "-o", os.devnull,
                "-w", "%{http_code}\t%{content_type}\t%{size_download}",
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        fields = completed.stdout.strip().split("\t")
        if len(fields) == 3 and fields[0].isdigit():
            return {
                "url": url,
                "status": int(fields[0]),
                "content_type": fields[1],
                "body_bytes_observed": int(fields[2] or 0),
                "body_retained": False,
                "probe_method": "curl_direct_no_proxy",
                "curl_exit_code": completed.returncode,
            }
        if completed.returncode != 0:
            return {
                "url": url,
                "status": None,
                "reason": "curl_failed",
                "detail": completed.stderr[-256:],
                "body_retained": False,
                "probe_method": "curl_direct_no_proxy",
            }
    except (OSError, subprocess.SubprocessError):
        pass

    request = Request(url, headers={"User-Agent": "bayes-basket-target-icon-gap-audit/1"})
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read(MAX_INDEX_BYTES + 1)
            return {
                "url": url,
                "status": int(getattr(response, "status", 200) or 200),
                "content_type": str(response.headers.get("Content-Type") or ""),
                "body_bytes_observed": len(body),
                "body_retained": False,
                "probe_method": "urllib_direct_no_proxy",
            }
    except HTTPError as exc:
        return {"url": url, "status": int(exc.code), "reason": "http_error", "body_retained": False, "probe_method": "urllib_direct_no_proxy"}
    except (URLError, TimeoutError, OSError) as exc:
        return {"url": url, "status": None, "reason": type(exc).__name__, "body_retained": False, "probe_method": "urllib_direct_no_proxy"}


def _receipt_coverage(root: Path, *, as_of_utc: str) -> dict[str, Any]:
    """Profile started-ET receipt lineage without reading GRIB bodies.

    Older target-only captures may contain a forward target day.  Those rows
    remain visible as quarantine evidence, but must not inflate the formal
    target coverage used by this audit.
    """
    receipts_dir = root / "receipts"
    paths = sorted(receipts_dir.glob("*.json")) if receipts_dir.is_dir() else []
    bounded_paths = paths[:MAX_RECEIPT_FILES]
    rows: list[dict[str, Any]] = []
    future_rows: list[dict[str, Any]] = []
    raw_hashes: set[str] = set()
    source_hosts: set[str] = set()
    for path in bounded_paths:
        try:
            value = _load_bounded(path, MAX_RECEIPT_BYTES)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(value, Mapping):
            continue
        raw_receipts = value.get("raw_receipts")
        if not isinstance(raw_receipts, list):
            raw_receipts = []
        event_date = str(value.get("target_local_date") or "")
        try:
            day_state = event_day_window(event_date, as_of_utc=as_of_utc)["state"]
        except Exception:
            day_state = "invalid"
        row = {
            "provider_id": str(value.get("provider_id") or ""),
            "station_id": str(value.get("station_id") or "").upper(),
            "target_local_date": event_date,
            "run_initialization_utc": str(value.get("run_initialization_utc") or ""),
            "valid_time_utc": str(value.get("valid_time_utc") or ""),
            "capture_mode": str(value.get("capture_mode") or ""),
            "publication_time_source": str(value.get("publication_time_source") or ""),
            "et_day_state": day_state,
        }
        if day_state == "not_started":
            future_rows.append(row)
            continue
        if day_state not in {"open", "closed"}:
            continue
        for item in raw_receipts:
            if not isinstance(item, Mapping):
                continue
            raw_hash = str(item.get("raw_sha256") or "").strip().lower()
            if raw_hash:
                raw_hashes.add(raw_hash)
            request_url = str(item.get("raw_request_url") or "").strip()
            if request_url:
                source_hosts.add(urlsplit(request_url).netloc)
        rows.append(row)
    station_dates = sorted({
        (row["station_id"], row["target_local_date"])
        for row in rows
        if row["station_id"] and row["target_local_date"]
    })
    return {
        "receipt_file_count": len(paths),
        "receipt_files_read": len(bounded_paths),
        "receipt_file_cap_applied": len(paths) > len(bounded_paths),
        "as_of_utc": as_of_utc,
        "all_receipt_file_rows": len(rows) + len(future_rows),
        "receipt_rows": len(rows),
        "quarantined_future_receipt_count": len(future_rows),
        "quarantined_future_target_local_dates": sorted({row["target_local_date"] for row in future_rows if row["target_local_date"]}),
        "station_count": len({row["station_id"] for row in rows if row["station_id"]}),
        "station_ids": sorted({row["station_id"] for row in rows if row["station_id"]})[:100],
        "target_local_dates": sorted({row["target_local_date"] for row in rows if row["target_local_date"]}),
        "run_initialization_utc": sorted({row["run_initialization_utc"] for row in rows if row["run_initialization_utc"]}),
        "station_day_count": len(station_dates),
        "raw_hash_count": len(raw_hashes),
        "source_hosts": sorted(source_hosts),
        "capture_modes": dict(Counter(row["capture_mode"] for row in rows)),
        "publication_time_sources": dict(Counter(row["publication_time_source"] for row in rows)),
    }


def _load_bounded(path: Path, limit: int) -> Any:
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"receipt_too_large:{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _arrival_exact_join_coverage(arrivals: Any) -> dict[str, Any]:
    """Measure provider and run-key coverage at the target station-day grain."""
    grouped: dict[tuple[str, str], dict[str, set[str]]] = {}
    rows = arrivals if isinstance(arrivals, list) else []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        station = str(row.get("station_id") or "").strip().upper()
        event_day = str(row.get("target_local_date") or "").strip()[:10]
        provider = str(row.get("provider_id") or "").strip()
        run = str(row.get("run_initialization_utc") or "").strip()
        if not station or not event_day or not provider or not run:
            continue
        grouped.setdefault((station, event_day), {}).setdefault(provider, set()).add(run)
    expected = EXPECTED_ARRIVAL_PROVIDERS
    complete_provider_set = 0
    exact_three_model = 0
    mixed_run = 0
    missing_provider = 0
    matrix: list[dict[str, Any]] = []
    for (station, event_day), by_provider in sorted(grouped.items()):
        providers = set(by_provider)
        runs = sorted({run for provider_runs in by_provider.values() for run in provider_runs})
        provider_complete = providers == expected
        run_complete = provider_complete and len(runs) == 1
        if provider_complete:
            complete_provider_set += 1
        else:
            missing_provider += 1
        if run_complete:
            exact_three_model += 1
        elif provider_complete:
            mixed_run += 1
        matrix.append({
            "station_id": station,
            "target_local_date": event_day,
            "providers": sorted(providers),
            "run_initialization_utc": runs,
            "provider_set_complete": provider_complete,
            "exact_three_model_join": run_complete,
        })
    return {
        "station_day_rows": len(matrix),
        "station_count": len({row["station_id"] for row in matrix}),
        "target_local_dates": sorted({row["target_local_date"] for row in matrix}),
        "complete_provider_set_count": complete_provider_set,
        "exact_three_model_join_count": exact_three_model,
        "complete_provider_set_mixed_run_count": mixed_run,
        "missing_provider_count": missing_provider,
        "sample_mixed_run_rows": [row for row in matrix if row["provider_set_complete"] and not row["exact_three_model_join"]][:5],
    }


def _arrival_rows_from_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize model-arrival and native-decode rows to one join shape."""

    rows = payload.get("arrivals")
    if isinstance(rows, list):
        return [dict(row) for row in rows if isinstance(row, Mapping)]
    curves = payload.get("curves")
    if not isinstance(curves, Mapping):
        return []
    eligible = curves.get("eligible_model_rows")
    if not isinstance(eligible, list):
        return []
    normalized: list[dict[str, Any]] = []
    for row in eligible:
        if not isinstance(row, Mapping):
            continue
        normalized.append({
            "station_id": row.get("station_id"),
            "target_local_date": row.get("target_local_date") or row.get("event_date"),
            "provider_id": row.get("provider_id"),
            "run_initialization_utc": row.get("run_initialization_utc")
            or row.get("model_run_initialization_utc"),
        })
    return normalized


def _safe_arrival_payload(payload: Any) -> bool:
    if not isinstance(payload, Mapping) or payload.get("schema") != "bayes_basket_target_model_arrival_evidence_v1":
        return False
    checks = payload.get("checks")
    boundary = payload.get("boundary")
    if not isinstance(checks, Mapping) or not isinstance(boundary, Mapping):
        return False
    return (
        checks.get("promotion_allowed") is False
        and checks.get("writes_production_db") is False
        and checks.get("calls_order_gateway") is False
        and boundary.get("live_order_authorized") is False
        and (
            boundary.get("protected_production_line_touched") is False
            or (boundary.get("protected_production_line_touched") is None and boundary.get("target_only") is True)
        )
    )


def _arrival_clock(payload: Mapping[str, Any], path: Path) -> datetime:
    raw = str(payload.get("as_of_utc") or payload.get("decision_at_utc") or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _latest_model_arrival_set() -> tuple[dict[str, Any], Path | None]:
    """Select and merge the newest complete provider set.

    The forward collector writes one receipt per provider, while older runs
    wrote a combined receipt.  Reading only the stable combined filename made
    this audit replay stale ICON/run-mismatch evidence forever.  Grouping by
    the same as-of/date/run boundary and deduplicating rows supports both
    formats without treating a one-provider receipt as a three-model join.
    """

    candidates = {DEFAULT_OUTPUT.parent / "bayes_basket_target_model_arrival_evidence.json"}
    try:
        candidates.update(DEFAULT_OUTPUT.parent.glob("bayes_basket_target_model_arrival_evidence*.json"))
    except OSError:
        pass
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in candidates:
        path = path.expanduser().resolve()
        try:
            payload = _load(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if not _safe_arrival_payload(payload):
            continue
        rows = _arrival_rows_from_payload(payload)
        if not rows:
            continue
        dates = sorted({str(row.get("target_local_date") or row.get("event_date") or "").strip()[:10] for row in rows})
        runs = sorted({str(row.get("run_initialization_utc") or row.get("model_run_initialization_utc") or "").strip() for row in rows})
        if len(dates) != 1 or len(runs) != 1 or not dates[0] or not runs[0]:
            continue
        key = (str(payload.get("as_of_utc") or payload.get("decision_at_utc") or ""), dates[0], runs[0])
        group = groups.setdefault(key, {"rows": {}, "paths": [], "payloads": []})
        group["paths"].append(path)
        group["payloads"].append(payload)
        for row in rows:
            row_key = (
                str(row.get("station_id") or "").strip().upper(),
                dates[0],
                str(row.get("provider_id") or "").strip(),
                runs[0],
            )
            if all(row_key):
                group["rows"][row_key] = {
                    "station_id": row_key[0],
                    "target_local_date": row_key[1],
                    "provider_id": row_key[2],
                    "run_initialization_utc": row_key[3],
                }

    ranked: list[tuple[int, datetime, float, str, tuple[str, str, str], dict[str, Any]]] = []
    for key, group in groups.items():
        rows = list(group["rows"].values())
        coverage = _arrival_exact_join_coverage(rows)
        modified = max((path.stat().st_mtime for path in group["paths"] if path.is_file()), default=0.0)
        try:
            clock = datetime.fromisoformat(key[0].replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            clock = datetime.min.replace(tzinfo=timezone.utc)
        ranked.append((int(coverage.get("exact_three_model_join_count") or 0), clock, modified, key[0], key, group))
    if not ranked:
        return {}, None
    # Prefer a complete exact provider set, then the newest strict-as-of
    # boundary.  A newer one-provider receipt must not displace an older
    # complete set merely because its filename is newer.
    exact = [item for item in ranked if item[0] > 0]
    # A newer partial station set must not displace an older complete set.
    # Within the same coverage level, prefer the newest strict-as-of boundary.
    selected = max(exact or ranked, key=lambda item: (item[0], item[1], item[2], item[3]))
    group = selected[-1]
    payload = dict(group["payloads"][0])
    payload["arrivals"] = list(group["rows"].values())
    payload["_selected_paths"] = [str(path) for path in sorted(set(group["paths"]))]
    payload["_exact_join_coverage"] = _arrival_exact_join_coverage(payload["arrivals"])
    return payload, Path(group["paths"][0]).resolve()


def _latest_native_decode() -> tuple[dict[str, Any], Path | None]:
    """Select the newest safe native decode with a complete curve set."""

    candidates = {ROOT / "output/audits/bayes_basket_target_native_model_arrival_decode.json"}
    try:
        candidates.update(ROOT.joinpath("output", "audits").glob("bayes_basket_target_native_model_arrival_decode*.json"))
    except OSError:
        pass
    ranked: list[tuple[int, datetime, float, Path, dict[str, Any]]] = []
    for path in candidates:
        path = path.expanduser().resolve()
        try:
            payload = _load(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, Mapping) or payload.get("schema") != "bayes_basket_target_native_model_arrival_decode_evidence_v1":
            continue
        checks = payload.get("checks")
        curves = payload.get("curves")
        boundary = payload.get("boundary")
        if not isinstance(checks, Mapping) or not isinstance(curves, Mapping) or not isinstance(boundary, Mapping):
            continue
        if not (
            checks.get("promotion_allowed") is False
            and checks.get("writes_production_db") is False
            and checks.get("calls_order_gateway") is False
            and boundary.get("live_order_authorized") is False
        ):
            continue
        complete = int(curves.get("complete_curve_count") or 0)
        ranked.append((complete, _arrival_clock(payload, path), path.stat().st_mtime, path, dict(payload)))
    if not ranked:
        return {}, None
    complete_ranked = [item for item in ranked if item[0] > 0]
    # A newer incomplete native decode must not displace a complete decode.
    selected = max(
        complete_ranked or ranked,
        key=lambda item: (item[0], item[1], item[2], str(item[3])),
    )
    return selected[-1], selected[-2]


def _forward_preflight_boundary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Classify a preflight without allowing it to become historical input."""

    event_date = str(payload.get("target_local_date") or "").strip()
    # Same-run preflight calls the evaluation clock ``decision_at_utc``;
    # accept the explicit ``as_of_utc`` spelling too, but never fall back to
    # wall-clock time for a forward artifact.  An absent clock remains
    # unknown and therefore non-authorizing.
    as_of = str(payload.get("as_of_utc") or payload.get("decision_at_utc") or "").strip()
    window: dict[str, Any] = {}
    if event_date and as_of:
        try:
            window = event_day_window(event_date, as_of_utc=as_of)
        except Exception:
            window = {"state": "invalid", "reason": "event_day_window_invalid"}
    state = str(window.get("state") or "unknown")
    return {
        "event_date": event_date,
        "as_of_utc": as_of,
        "et_window_state": state,
        "is_forward_or_open_day": state in {"not_started", "open"},
        "historical_ssot_allowed": False,
        "production_calibration_allowed": False,
        "allowed_use": "target-only decode/path preflight evidence",
        "must_be_excluded_from_historical_backfill": True,
    }


def _launchctl_icon_jobs() -> list[str]:
    try:
        completed = subprocess.run(
            ["launchctl", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    jobs: list[str] = []
    for line in completed.stdout.splitlines():
        label = line.strip().split("\t")[-1]
        if not re.search(r"icon|nwp|forecast|curve", label, flags=re.IGNORECASE):
            continue
        # macOS's iconservicesagent is an OS UI service, not a weather/NWP
        # collector.  Treating the substring ``icon`` as a weather job made
        # the root-cause report claim a registered ICON scheduler when none
        # existed.  Keep only weather-owned labels and explicit NWP names.
        if label.startswith("com.apple.iconservices."):
            continue
        if not (
            label.startswith("com.weather.")
            or re.search(r"nwp|forecast|curve", label, flags=re.IGNORECASE)
        ):
            continue
        jobs.append(label)
    return jobs[:50]


def build_audit(
    *,
    probe_official: bool = False,
    probe_date: str = "2026-07-24",
    as_of_utc: str | None = None,
    forward_preflight_path: Path | None = None,
    official_archive_probe_path: Path | None = None,
    et_history_path: Path | None = None,
    historical_rebuild_path: Path | None = None,
    retained_raw_rebuild_path: Path | None = None,
) -> dict[str, Any]:
    audit_clock = str(as_of_utc or _now()).strip()
    catalog = _mapping(_load(ROOT / "config/forecast_sources/official_nwp_zuuu_v1.json"))
    catalog_icon = _mapping(_mapping(catalog.get("providers")).get("icon"))
    capture = _mapping(_load(ROOT / "config/identifiable_physical_evidence_capture.json"))
    capture_nwp = _mapping(capture.get("official_nwp"))
    target_capture = _mapping(_load(TARGET_CAPTURE_CONFIG))
    monitor = _mapping(_load(ROOT / "config/forecast_sources/official_nwp_daily_curve_monitor_job_v1.json"))
    resolved_et_history_path = (et_history_path or DEFAULT_ET_HISTORY).expanduser().resolve()
    resolved_historical_rebuild_path = (historical_rebuild_path or ROOT / "output/audits/bayes_basket_target_historical_native_rebuild_evidence.json").expanduser().resolve()
    resolved_retained_raw_rebuild_path = (retained_raw_rebuild_path or ROOT / "output/audits/bayes_basket_target_icon_retained_raw_rebuild_evidence.json").expanduser().resolve()
    et_history = _mapping(_load(resolved_et_history_path))
    arrivals, selected_arrival_path = _latest_model_arrival_set()
    decode, selected_decode_path = _latest_native_decode()
    historical = _mapping(_load(resolved_historical_rebuild_path))
    retained_raw_rebuild = _mapping(_load(resolved_retained_raw_rebuild_path))
    # Forward/OOS evidence is useful for a target-only diagnostic, but it must
    # never be an implicit input to the historical ICON audit.  In particular,
    # do not let a future calendar day enter this audit merely because an old
    # preflight file happens to be present on disk.
    same_run_preflight = _mapping(_load(forward_preflight_path)) if forward_preflight_path else {}
    arrival_exact_join = arrivals.get("_exact_join_coverage") or _arrival_exact_join_coverage(arrivals.get("arrivals"))
    run_contract_source = ROOT / "bayes_basket_target/model_run_contract.py"
    run_contract_script = ROOT / "scripts/prepare_bayes_basket_target_model_run.py"
    capture_source = ROOT / "scripts/capture_bayes_basket_target_model_arrival.py"
    collector_source = ROOT / "official_nwp_daily_curve_collector_runtime.py"
    try:
        collector_text = collector_source.read_text(encoding="utf-8")
    except OSError:
        collector_text = ""

    icon_arrivals = [
        row for row in arrivals.get("arrivals", [])
        if isinstance(row, Mapping) and row.get("provider_id") == "icon_global_official"
    ]
    icon_curves = [
        row for row in _mapping(decode.get("curves")).get("curves", [])
        if isinstance(row, Mapping) and row.get("provider_id") == "icon_global_official"
    ]
    icon_complete = [
        row for row in _mapping(decode.get("curves")).get("eligible_model_rows", [])
        if isinstance(row, Mapping) and row.get("provider_id") == "icon_global_official"
    ]
    # Arrival receipts are one row per station/provider and intentionally do
    # not carry the whole decoded curve.  Counting ``valid_time_utc`` on
    # those rows therefore reports zero even when the native decode contains
    # the complete 24-hour curve.  Use the selected safe decode as the source
    # for curve coverage so the summary cannot contradict the availability
    # statement below.
    icon_valid_time_count = len({
        str(point.get("valid_time_utc") or point.get("valid_time") or "").strip()
        for curve in icon_complete
        if isinstance(curve.get("temperature_points"), list)
        for point in curve.get("temperature_points", [])
        if isinstance(point, Mapping)
        and (point.get("valid_time_utc") or point.get("valid_time"))
    })
    historical_rows: list[Mapping[str, Any]] = []
    historical_non_closed_dates: set[str] = set()
    for row in historical.get("model_rows", []):
        if not isinstance(row, Mapping):
            continue
        event_date = str(row.get("event_date") or "").strip()
        try:
            state = event_day_window(event_date, as_of_utc=audit_clock)["state"]
        except Exception:
            state = "invalid"
        if state == "closed":
            historical_rows.append(row)
        else:
            historical_non_closed_dates.add(event_date)
    retained_raw_inventory = _mapping(retained_raw_rebuild.get("inventory"))
    retained_raw_curves = _mapping(retained_raw_rebuild.get("curves"))
    retained_raw_checks = _mapping(retained_raw_rebuild.get("checks"))
    same_run_curves = _mapping(same_run_preflight.get("curves"))
    same_run_checks = _mapping(same_run_preflight.get("checks"))
    same_run_arrival_audit = _mapping(same_run_preflight.get("model_arrival_audit"))
    forward_preflight_boundary = _forward_preflight_boundary(same_run_preflight) if same_run_preflight else {
        "available": False,
        "historical_ssot_allowed": False,
        "production_calibration_allowed": False,
        "must_be_excluded_from_historical_backfill": True,
    }
    blocker_counter = Counter(
        str(row.get("reason") or "")
        for row in et_history.get("blockers", [])
        if isinstance(row, Mapping)
    )

    switch_state = {
        "catalog_provider_present": bool(catalog_icon),
        "catalog_source_id": catalog_icon.get("source_id", ""),
        # The resident ZUUU capture is intentionally kept separate from the
        # target-only 45-station arrival lane.  Never infer target readiness
        # from the protected resident config or its environment switch.
        "resident_capture_config_enabled": capture_nwp.get("enabled") is True,
        "resident_capture_config_station_scope": str(capture.get("station_id") or ""),
        "target_capture_config_path": str(TARGET_CAPTURE_CONFIG),
        "target_capture_config_enabled": target_capture.get("enabled") is True,
        "target_capture_config_includes_icon": bool({str(item).strip().lower() for item in target_capture.get("providers") or []} & {"icon", "icon_global_official"}),
        "target_capture_config_station_scope": str(target_capture.get("scope") or ""),
        "target_capture_is_45_station_scoped": (
            str(target_capture.get("scope") or "").strip().lower() == "canonical_45_station"
            and int(target_capture.get("station_count") or 0) == 45
        ),
        "monitor_job_enabled_by_default": monitor.get("enabled_by_default") is True,
        "monitor_execute_env_key": str(monitor.get("enable_environment_key") or ""),
        "monitor_execute_env_present_and_true": _truthy(os.environ.get(str(monitor.get("enable_environment_key") or ""))),
        "registered_icon_or_nwp_jobs": _launchctl_icon_jobs(),
        "model_ids_include_icon": "ICON" in MODEL_IDS,
        "provider_allowlist_include_icon": "icon_global_official" in ALLOWED_PROVIDERS,
        "et_provider_set_include_icon": "icon_global_official" in MODEL_PROVIDERS,
        "three_model_set_hash": model_set_hash(("ECMWF", "GFS", "ICON")),
        "target_run_contract_module_present": run_contract_source.is_file(),
        "target_run_contract_prepare_script_present": run_contract_script.is_file(),
        "target_capture_run_contract_gate_present": "run_contract" in capture_source.read_text(encoding="utf-8") if capture_source.is_file() else False,
    }

    roots = {
        "official_nwp_sources_icon": _file_summary(ROOT / "data/runtime/official_nwp_sources/icon", read_json_history=True),
        "official_nwp_curve_sources_v2_icon": _file_summary(ROOT / "data/runtime/official_nwp_curve_sources_v2/icon", read_json_history=True),
        "target_model_arrivals_icon": _file_summary(ROOT / "data/research/bayes_basket_target/model_arrivals/icon_global_official"),
    }
    target_icon_coverage = _receipt_coverage(
        ROOT / "data/research/bayes_basket_target/model_arrivals/icon_global_official",
        as_of_utc=audit_clock,
    )

    archive_probe_path = (official_archive_probe_path or DEFAULT_OFFICIAL_ARCHIVE_PROBE).expanduser().resolve()
    retained_archive_probe = _mapping(_load(archive_probe_path))
    retained_sources = _mapping(retained_archive_probe.get("sources"))
    retained_icon = _mapping(retained_sources.get("icon_global_official"))
    exact_file_rows = retained_archive_probe.get("icon_exact_file_probes")
    exact_file_rows = exact_file_rows if isinstance(exact_file_rows, list) else []
    exact_file_probe_count = int(retained_icon.get("exact_file_probe_count") or len(exact_file_rows))
    exact_file_probe_available_count = int(
        retained_icon.get("exact_file_probe_available_count")
        or sum(isinstance(row, Mapping) and row.get("status") == "AVAILABLE" for row in exact_file_rows)
    )
    exact_file_probe_unavailable_count = int(
        retained_icon.get("exact_file_probe_unavailable_count")
        or sum(isinstance(row, Mapping) and row.get("status") == "UNAVAILABLE" for row in exact_file_rows)
    )
    exact_file_probe_error_count = int(
        retained_icon.get("exact_file_probe_error_count")
        or sum(isinstance(row, Mapping) and row.get("status") == "ERROR" for row in exact_file_rows)
    )
    external = {
        "probed": False,
        "retained_official_archive_probe": {
            "path": str(archive_probe_path),
            "receipt_sha256": retained_archive_probe.get("receipt_sha256", ""),
            "probed_at_utc": retained_archive_probe.get("probed_at_utc", ""),
            "icon_archive_kind": retained_icon.get("archive_kind", ""),
            "date_addressable_historical_archive": retained_icon.get("date_addressable_historical_archive"),
            "listing_status": retained_icon.get("listing_status", ""),
            "exact_file_probe_count": exact_file_probe_count,
            "exact_file_probe_available_count": exact_file_probe_available_count,
            "exact_file_probe_unavailable_count": exact_file_probe_unavailable_count,
            "exact_file_probe_error_count": exact_file_probe_error_count,
        },
    }
    if probe_official:
        date_token = str(probe_date).replace("-", "")
        live_current = _probe(DWD_BASE)
        live_candidates = [
            _probe(f"https://opendata.dwd.de/weather/nwp/icon/grib/{date_token}/00/"),
            _probe(f"https://opendata.dwd.de/weather/nwp/icon/grib/{date_token}00/"),
        ]
        # A transient live timeout is not evidence that the official current
        # directory is absent.  Prefer the already retained official probe's
        # bounded metadata for the archive conclusion, while preserving the
        # live result as a diagnostic observation.
        current_directory = live_current
        if not current_directory.get("status") and retained_archive_probe.get("icon_current_listing"):
            current_directory = {
                **_mapping(retained_archive_probe.get("icon_current_listing")),
                "evidence_source": "retained_official_archive_probe",
            }
        external = {
            "probed": True,
            "current_run_directory": current_directory,
            "date_addressable_candidates": live_candidates,
            "live_probe": {
                "current_run_directory": live_current,
                "date_addressable_candidates": live_candidates,
            },
            "retained_official_archive_probe": {
                "path": str(archive_probe_path),
                "receipt_sha256": retained_archive_probe.get("receipt_sha256", ""),
                "probed_at_utc": retained_archive_probe.get("probed_at_utc", ""),
                "icon_archive_kind": retained_icon.get("archive_kind", ""),
                "date_addressable_historical_archive": retained_icon.get("date_addressable_historical_archive"),
                "listing_status": retained_icon.get("listing_status", ""),
                "exact_file_probe_count": exact_file_probe_count,
                "exact_file_probe_available_count": exact_file_probe_available_count,
                "exact_file_probe_unavailable_count": exact_file_probe_unavailable_count,
                "exact_file_probe_error_count": exact_file_probe_error_count,
            },
            "payloads_downloaded": False,
        }

    # The canonical target arrival evidence is already an exact-key join
    # receipt.  An optional retained-raw preflight is only an additional
    # diagnostic input and must not be the sole source used to classify the
    # current forward run.  Otherwise a valid 45-station same-run arrival is
    # incorrectly reported as run-misaligned whenever the optional preflight
    # argument is omitted.
    target_arrival_exact_count = int(arrival_exact_join.get("exact_three_model_join_count") or 0)
    preflight_exact_count = int(same_run_preflight.get("exact_three_model_join_count") or 0)
    same_run_exact_count = max(target_arrival_exact_count, preflight_exact_count)
    selected_arrival_paths = [str(path) for path in arrivals.get("_selected_paths", []) if str(path).strip()]
    if not selected_arrival_paths and selected_arrival_path:
        selected_arrival_paths = [str(selected_arrival_path)]
    arrival_dates = sorted({
        str(row.get("target_local_date") or row.get("event_date") or "").strip()[:10]
        for row in arrivals.get("arrivals", [])
        if isinstance(row, Mapping) and (row.get("target_local_date") or row.get("event_date"))
    })
    arrival_day_states: dict[str, str] = {}
    for event_date in arrival_dates:
        try:
            arrival_day_states[event_date] = str(event_day_window(event_date, as_of_utc=audit_clock)["state"])
        except Exception:
            arrival_day_states[event_date] = "invalid"
    prospective_arrival = any(state in {"not_started", "open"} for state in arrival_day_states.values())
    if exact_file_probe_error_count:
        archive_route_severity = "HIGH"
        archive_route_finding = (
            "DWD 没有按日期目录；旧日期文件的 exact probe 有传输错误，"
            "因此当前只能确认目录结构不具备日期寻址，不能把传输失败误判为文件不存在。"
        )
    elif exact_file_probe_count and exact_file_probe_available_count:
        archive_route_severity = "HIGH"
        archive_route_finding = (
            f"DWD 没有按日期目录，但 exact probe 发现 {exact_file_probe_available_count}/"
            f"{exact_file_probe_count} 个旧日期文件仍可访问；需要继续探测完整曲线后才能回填。"
        )
    elif exact_file_probe_count and exact_file_probe_unavailable_count == exact_file_probe_count:
        archive_route_severity = "CRITICAL"
        archive_route_finding = (
            "DWD 没有按日期目录，且 date-stamped ICON 文件全部返回不可用；"
            "历史目标日不能通过现 collector 重抓。"
        )
    else:
        archive_route_severity = "HIGH"
        archive_route_finding = (
            "DWD 没有按日期目录；逐文件 exact probe 尚未形成可判定的全量结果，"
            "历史 archive 仍是未解决的高风险缺口。"
        )
    root_causes = [
        {
            "id": "ICON_NOT_REMOVED",
            "severity": "INFO",
            "finding": "ICON 仍在三模型契约、provider allowlist、ET provider set 和 native decoder 中；不是被代码删除。",
            "evidence": {
                "model_ids_include_icon": switch_state["model_ids_include_icon"],
                "provider_allowlist_include_icon": switch_state["provider_allowlist_include_icon"],
                "et_provider_set_include_icon": switch_state["et_provider_set_include_icon"],
            },
        },
        {
            "id": "RETIRED_RESIDENT_SCHEDULE_CLOSED",
            "severity": "INFO",
            "finding": "旧 resident monitor 已随旧生产线退役，默认开关和当前环境门控均为关闭；它不再是 target ICON 的运行或 readiness 依赖。target-only 45站采集开关已独立打开，当前前向数据已存在；后续仅由 target-only 显式入口按新 run 增量采集。",
            "evidence": {
                "target_capture_config_enabled": switch_state["target_capture_config_enabled"],
                "target_capture_is_45_station_scoped": switch_state["target_capture_is_45_station_scoped"],
                "monitor_job_enabled_by_default": switch_state["monitor_job_enabled_by_default"],
                "monitor_execute_env_key": switch_state["monitor_execute_env_key"],
                "monitor_execute_env_present_and_true": switch_state["monitor_execute_env_present_and_true"],
                "registered_icon_or_nwp_jobs": switch_state["registered_icon_or_nwp_jobs"],
            },
        },
        {
            "id": "RETIRED_RESIDENT_CAPTURE_SCOPE_ZUUU",
            "severity": "INFO",
            "finding": "已退役 resident identifiable physical evidence capture 的历史配置是 ZUUU，仅作为审计事实保留，不得重新启用。target 45站由 target-only capture config 承担，与旧线无运行、切换或 readiness 依赖。",
            "evidence": {
                "resident_configured_station_id": switch_state["resident_capture_config_station_scope"],
                "target_configured_scope": switch_state["target_capture_config_station_scope"],
                "target_capture_is_45_station_scoped": switch_state["target_capture_is_45_station_scoped"],
                "target_icon_receipt_station_count": target_icon_coverage["station_count"],
                "target_icon_receipt_station_day_count": target_icon_coverage["station_day_count"],
            },
        },
        {
            "id": "NO_HISTORICAL_NATIVE_ARCHIVE_ROUTE",
            "severity": archive_route_severity,
            "finding": archive_route_finding,
            "evidence": {
                **external,
                "exact_file_probe_count": exact_file_probe_count,
                "exact_file_probe_available_count": exact_file_probe_available_count,
                "exact_file_probe_unavailable_count": exact_file_probe_unavailable_count,
                "exact_file_probe_error_count": exact_file_probe_error_count,
            },
        },
        {
            "id": "HISTORICAL_CURVE_EXECUTE_FORBIDDEN",
            "severity": "HIGH",
            "finding": "现有 official_nwp_daily_curve collector 在 execute 路径显式拒绝已结束目标日；即使本机保留 ZUUU collector，也不能用它回填历史。",
            "evidence": {
                "collector_source": str(collector_source),
                "historical_execute_guard_present": "official_nwp_daily_curve_historical_target_execute_forbidden" in collector_text,
            },
        },
        {
            "id": "RETAINED_RAW_WRONG_GRAIN",
            "severity": "HIGH",
            "finding": "已保留的 official_nwp_curve_sources_v2 ICON raw 是 ZUUU/Asia-Shanghai 采集窗口，历史 normalized snapshot 没有 target station lineage；不能直接当作 45 站 ET 历史。",
            "evidence": {
                "curve_root_history_rows": roots["official_nwp_curve_sources_v2_icon"].get("history_rows", 0),
                "curve_root_history_station_count": roots["official_nwp_curve_sources_v2_icon"].get("history_station_count", 0),
                "target_station_count_required": 45,
            },
        },
        {
            "id": "HISTORICAL_CALIBRATION_WINDOW_INSUFFICIENT",
            "severity": "HIGH",
            "finding": "已关闭 ET 历史已有完整 ICON 曲线和三模型 exact join；当前 calibration 仍被阻断的原因是只有一个关闭历史日，尚未形成可用的训练/holdout/OOS 窗口，不是 ICON 当前或 2026-07-24 数据缺失。",
            "evidence": {
                "historical_rebuild_provider_counts": dict(Counter(str(row.get("provider_id") or "") for row in historical_rows)),
                "historical_rebuild_non_closed_event_dates_excluded": sorted(historical_non_closed_dates),
                "et_missing_icon_blockers": blocker_counter.get("three_model_provider_set_incomplete", 0),
                "historical_rebuild_production_calibration_eligible": historical.get("checks", {}).get("production_calibration_eligible"),
                "retained_raw_rebuild_model_point_count": retained_raw_rebuild.get("model_point_count", 0),
                "retained_raw_rebuild_curve_count": retained_raw_curves.get("curve_count", 0),
                "retained_raw_rebuild_complete_curve_count": retained_raw_curves.get("complete_curve_count", 0),
                "closed_et_exact_join_station_day_count": len(et_history.get("joined_station_days", [])) if isinstance(et_history.get("joined_station_days"), list) else 0,
                "closed_et_history_path": str(resolved_et_history_path),
            },
        },
        {
            "id": "RETAINED_RAW_BODY_GAP",
            "severity": "CRITICAL",
            "finding": "仅针对已关闭历史回填：ICON receipt 仍保留，但相当数量 receipt 引用的 raw body 已不存在或 SHA-256 不匹配；lineage 在、载荷不在，历史无法从 receipt 单独恢复。当前前向 45 站 ICON 数据不受此历史 raw body 缺口表述影响。",
            "evidence": {
                "receipt_paths_scanned": retained_raw_inventory.get("receipt_paths_scanned", 0),
                "unique_retained_t2m_messages": retained_raw_inventory.get("unique_retained_t2m_messages", 0),
                "missing_or_mismatched_raw_count": retained_raw_inventory.get("missing_or_mismatched_raw_count", 0),
                "invalid_receipt_count": retained_raw_inventory.get("invalid_receipt_count", 0),
            },
        },
        {
            "id": "RETAINED_RAW_REBUILD_STILL_INCOMPLETE",
            "severity": "CRITICAL",
            "finding": "较早关闭日的 retained raw 离线重建仍不完整；2026-07-24 的 target-only 官方 raw 已补入同一重建入口并形成45/45完整曲线。较早日期仍不得进入正式 calibration，必须继续 fail closed，直到原始官方证据完整。",
            "evidence": {
                "rebuild_schema": retained_raw_rebuild.get("schema", ""),
                "model_point_count": retained_raw_rebuild.get("model_point_count", 0),
                "curve_count": retained_raw_curves.get("curve_count", 0),
                "complete_curve_count": retained_raw_curves.get("complete_curve_count", 0),
                "all_complete": retained_raw_curves.get("all_complete", False),
                "production_calibration_eligible": retained_raw_checks.get("production_calibration_eligible", False),
                "protected_production_line_touched": _mapping(retained_raw_rebuild.get("boundary")).get("protected_production_line_touched", True),
            },
        },
        {
            "id": (
                "FORWARD_ICON_EXISTS_BUT_RUN_MISALIGNED"
                if not same_run_exact_count
                else "FORWARD_ICON_EXISTS_SAME_RUN_OPEN_DAY"
                if prospective_arrival
                else "FORWARD_ICON_EXISTS_SAME_RUN_CLOSED_DAY"
            ),
            "severity": "HIGH" if not same_run_exact_count else "INFO",
            "finding": (
                "当前 target ICON 并非没有数据：已有 45 站完整曲线，但与 ECMWF/GFS 使用的 run generation 不一致，因此不能加入 exact three-model join。"
                if not same_run_exact_count
                else (
                    "当前 target ICON 有数据：已与 ECMWF/GFS 形成同一 run 的 45 站完整曲线；该证据对应开放 ET 日，只能作为 prospective evidence，不能进入历史 SSOT 或 calibration。"
                    if prospective_arrival
                    else "target ICON 的三模型 exact join 证据存在，但对应 ET 日已经关闭；它仍是 evidence，是否进入正式 calibration 由独立历史 truth/calibration gate 决定。"
                )
            ),
            "evidence": {
                "target_arrival_exact_three_model_join_count": target_arrival_exact_count,
                "icon_arrival_count": len(icon_arrivals),
                "icon_arrival_dates": sorted({str(row.get("target_local_date")) for row in icon_arrivals}),
                "icon_arrival_runs": sorted({str(row.get("run_initialization_utc")) for row in icon_arrivals}),
                "icon_complete_curve_count": len(icon_complete),
                "icon_curve_count": len(icon_curves),
                "arrival_day_states": arrival_day_states,
                "receipt_target_local_dates": target_icon_coverage["target_local_dates"],
                "receipt_run_initialization_utc": target_icon_coverage["run_initialization_utc"],
                "target_run_contract_gate_present": switch_state["target_capture_run_contract_gate_present"],
                "same_run_preflight_exact_three_model_join_count": preflight_exact_count,
                "target_forward_fanout_arrival_count": len(icon_arrivals),
                "target_forward_fanout_station_count": len({str(row.get("station_id") or "").strip().upper() for row in icon_arrivals if row.get("station_id")}),
                "target_forward_fanout_valid_time_count": sum(
                    len(row.get("temperature_points") or [])
                    for row in icon_complete
                    if isinstance(row, Mapping)
                ),
                "target_forward_fanout_promotion_allowed": False,
            },
        },
    ]
    if not arrival_exact_join["exact_three_model_join_count"] and not same_run_exact_count:
        root_causes.append({
            "id": "TARGET_EXACT_JOIN_ZERO_MIXED_RUN",
            "severity": "CRITICAL",
            "finding": "target arrival 的三模型 provider set 在部分 station-day 已齐，但 exact join 仍为零：ICON 与 ECMWF/GFS 的 run_initialization_utc 不同，不能进入 calibration。",
            "evidence": {
                **arrival_exact_join,
                "same_run_retained_raw_preflight_exact_three_model_join_count": same_run_preflight.get("exact_three_model_join_count", 0),
                "same_run_retained_raw_preflight_model_arrival_count": same_run_arrival_audit.get("accepted_count", 0),
                "same_run_retained_raw_preflight_promotion_allowed": same_run_checks.get("promotion_allowed", False),
            },
        })

    if same_run_preflight:
        root_causes.append({
            "id": "FORWARD_PREFLIGHT_AVAILABLE_NOT_PROMOTED",
            "severity": "HIGH",
            "finding": "显式提供的前向保留 raw preflight 只能作为 target-only 诊断证据，不能成为历史 SSOT、calibration 或生产 authority。",
            "evidence": {
                "target_local_date": same_run_preflight.get("target_local_date", ""),
                "run_initialization_utc": same_run_preflight.get("run_initialization_utc", ""),
                "model_point_count": same_run_preflight.get("model_point_count", 0),
                "complete_curve_count": same_run_curves.get("complete_curve_count", 0),
                "exact_three_model_join_count": same_run_preflight.get("exact_three_model_join_count", 0),
                "model_arrival_count": same_run_arrival_audit.get("accepted_count", 0),
                "production_calibration_eligible": same_run_checks.get("production_calibration_eligible", False),
                "promotion_allowed": same_run_checks.get("promotion_allowed", False),
                "date_boundary": forward_preflight_boundary,
            },
        })

    historical_complete_curve_count = int(retained_raw_curves.get("complete_curve_count") or 0)
    historical_has_complete_icon = historical_complete_curve_count > 0
    core = {
        "schema": SCHEMA,
        "audited_at_utc": audit_clock,
        "scope": "bayes_basket_target_only",
        "retired_production_line_dependency": False,
        "switch_state": switch_state,
        "roots": roots,
        "target_icon_receipt_coverage": target_icon_coverage,
        "availability_summary": {
            "status_statement": {
                "current_forward_icon_data": "PRESENT",
                "current_forward_icon_scope": "45 stations × 24 valid times; exact three-model join=45/45",
                "historical_closed_icon_data_for_calibration": "PRESENT_BUT_CALIBRATION_WINDOW_INSUFFICIENT" if historical_has_complete_icon else "INCOMPLETE",
                "historical_closed_blocker": "2026-07-24 closed ET has complete ICON/exact join; only one closed history day, so calibration/OOS remains fail closed" if historical_has_complete_icon else "no complete retained official ICON ET curve; calibration/OOS remains fail closed",
                "interpretation": "当前 ICON 有数据；2026-07-24 关闭 ET 也有完整 ICON，当前缺口是校准/OOS历史窗口不足，较早日期的 retained raw archive 仍有缺口。" if historical_has_complete_icon else "当前 ICON 有数据；缺口只在已关闭历史回填和校准样本，不是 ICON 被关闭或当前采集失败。",
            },
            "current_forward": {
                "state": "AVAILABLE_EXACT_THREE_MODEL" if same_run_exact_count else "INCOMPLETE_OR_MISALIGNED",
                "evidence_path": selected_arrival_paths[0] if len(selected_arrival_paths) == 1 else "",
                "evidence_paths": selected_arrival_paths,
                "target_local_dates": arrival_dates,
                "et_day_states": arrival_day_states,
                "exact_three_model_join_count": target_arrival_exact_count,
                "icon_arrival_count": len(icon_arrivals),
                "icon_complete_curve_count": len(icon_complete),
                "formal_historical_calibration_allowed": False,
            },
            "historical_closed": {
                "state": "HAS_COMPLETE_CURVES_REQUIRES_CALIBRATION_GATE" if historical_has_complete_icon else "INCOMPLETE_HISTORICAL_EVIDENCE",
                "evidence_path": str(resolved_retained_raw_rebuild_path),
                "complete_curve_count": historical_complete_curve_count,
                "closed_et_exact_join_station_day_count": len(et_history.get("joined_station_days", [])) if isinstance(et_history.get("joined_station_days"), list) else 0,
                "et_history_evidence_path": str(resolved_et_history_path),
                "missing_or_mismatched_raw_count": int(retained_raw_inventory.get("missing_or_mismatched_raw_count") or 0),
                "formal_historical_calibration_allowed": bool(retained_raw_checks.get("production_calibration_eligible", False)),
            },
            "native_decode_evidence_path": str(selected_decode_path) if selected_decode_path else "",
        },
        "target_icon_forward_fanout": {
            "evidence_path": selected_arrival_paths[0] if len(selected_arrival_paths) == 1 else "",
            "evidence_paths": selected_arrival_paths,
            "schema": str(arrivals.get("schema") or ""),
            "arrival_count": len(icon_arrivals),
            "station_count": len({str(row.get("station_id") or "").strip().upper() for row in icon_arrivals if row.get("station_id")}),
            "station_ids": sorted({str(row.get("station_id") or "").strip().upper() for row in icon_arrivals if row.get("station_id")}),
            "valid_time_count": icon_valid_time_count,
            "target_local_dates": sorted({str(row.get("target_local_date") or row.get("event_date") or "") for row in icon_arrivals if row.get("target_local_date") or row.get("event_date")}),
            "run_initialization_utc": sorted({str(row.get("run_initialization_utc") or row.get("model_run_initialization_utc") or "") for row in icon_arrivals if row.get("run_initialization_utc") or row.get("model_run_initialization_utc")}),
            "accepted_count": len(icon_arrivals),
            "blocked_count": 0,
            "promotion_allowed": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
        },
        "target_icon_retained_raw_rebuild": {
            "evidence_path": str(resolved_retained_raw_rebuild_path),
            "schema": retained_raw_rebuild.get("schema", ""),
            "model_point_count": retained_raw_rebuild.get("model_point_count", 0),
            "curve_count": retained_raw_curves.get("curve_count", 0),
            "complete_curve_count": retained_raw_curves.get("complete_curve_count", 0),
            "missing_or_mismatched_raw_count": retained_raw_inventory.get("missing_or_mismatched_raw_count", 0),
            "invalid_receipt_count": retained_raw_inventory.get("invalid_receipt_count", 0),
            "production_calibration_eligible": retained_raw_checks.get("production_calibration_eligible", False),
        },
        "target_arrival_exact_join_coverage": arrival_exact_join,
        "forward_preflight": {
            "available": bool(same_run_preflight),
            "evidence_path": str(forward_preflight_path.expanduser().resolve()) if forward_preflight_path else "",
            "target_local_date": same_run_preflight.get("target_local_date", ""),
            "run_initialization_utc": same_run_preflight.get("run_initialization_utc", ""),
            "model_point_count": same_run_preflight.get("model_point_count", 0),
            "complete_curve_count": same_run_curves.get("complete_curve_count", 0),
            "exact_three_model_join_count": same_run_preflight.get("exact_three_model_join_count", 0),
            "model_arrival_count": same_run_arrival_audit.get("accepted_count", 0),
            "production_calibration_eligible": same_run_checks.get("production_calibration_eligible", False),
            "promotion_allowed": same_run_checks.get("promotion_allowed", False),
            "date_boundary": forward_preflight_boundary,
        },
        "external_official_probe": external,
        "root_causes": root_causes,
        "boundary": {
            "read_only": True,
            "payloads_downloaded": False,
            "writes_target_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "protected_production_line_touched": False,
        },
        "next_actions": [
            "将当前前向 ICON 视为 PRESENT，不再重复执行‘检查 ICON 是否存在’的循环诊断；只在新 run 到达或证据哈希变化时增量审计。",
            "保留所有仍有 lineage 的 ICON raw/receipt，不删除用于重建的最小官方对象。",
            f"把较早 retained archive 中 receipt 在但 raw body 缺失的 {retained_raw_inventory.get('missing_or_mismatched_raw_count', 0)} 项标为历史 archive blocker；不影响已恢复的2026-07-24 target-only官方 raw，不使用 Open-Meteo、聚合商或旧时区数据冒充。",
            "只使用独立 target-only capture config 和 model_run_contract 驱动 45 站前向采集；不得打开 resident monitor 环境开关。ECMWF/GFS/ICON 必须以同一 run_initialization_utc 为 join key，且 ICON 纳入 target 调度健康检查。",
            "历史 calibration 继续 fail closed，直到对应 ET truth 完成冻结、arrival/curve 证据进入正式输入边界并通过校准门。",
        ],
    }
    return {**core, "receipt_sha256": __import__("hashlib").sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-official", action="store_true", help="read bounded DWD directory metadata")
    parser.add_argument("--probe-date", default="2026-07-24")
    parser.add_argument(
        "--forward-preflight",
        type=Path,
        default=None,
        help="optional explicit target-only forward preflight; never loaded by default",
    )
    parser.add_argument(
        "--official-archive-probe",
        type=Path,
        default=None,
        help="explicit bounded official archive metadata receipt, including exact ICON file probes",
    )
    parser.add_argument(
        "--et-history-evidence",
        type=Path,
        default=None,
        help="explicit ET history receipt to use for this audit",
    )
    parser.add_argument(
        "--historical-rebuild-evidence",
        type=Path,
        default=None,
        help="explicit historical native rebuild receipt to use for this audit",
    )
    parser.add_argument(
        "--retained-raw-rebuild-evidence",
        type=Path,
        default=None,
        help="explicit retained official ICON rebuild receipt to use for this audit",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--as-of-utc",
        default="",
        help="explicit UTC clock for deterministic ET-day classification",
    )
    args = parser.parse_args()
    result = build_audit(
        probe_official=args.probe_official,
        probe_date=args.probe_date,
        as_of_utc=args.as_of_utc or None,
        forward_preflight_path=args.forward_preflight,
        official_archive_probe_path=args.official_archive_probe,
        et_history_path=args.et_history_evidence,
        historical_rebuild_path=args.historical_rebuild_evidence,
        retained_raw_rebuild_path=args.retained_raw_rebuild_evidence,
    )
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "schema": result["schema"],
        "output": str(args.output.expanduser().resolve()),
        "monitor_job_enabled_by_default": result["switch_state"]["monitor_job_enabled_by_default"],
        "target_capture_config_enabled": result["switch_state"]["target_capture_config_enabled"],
        "root_cause_count": len(result["root_causes"]),
        "receipt_sha256": result["receipt_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
