#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
DEFAULT_OUTPUT = DATA_DIR / "model_release_watcher_latest.json"

MODEL_LABELS = {
    "gfs_seamless": "GFS",
    "icon_seamless": "ICON",
    "ecmwf_ifs025": "ECMWF IFS 0.25°",
}

MODEL_CYCLES = {
    "gfs_seamless": [0, 6, 12, 18],
    "icon_seamless": [0, 6, 12, 18],
    "ecmwf_ifs025": [0, 6, 12, 18],
}

MODEL_MIN_RELEASE_DELAY_SEC = {
    "gfs_seamless": int(float(os.environ.get("PM_MODEL_RELEASE_GFS_DELAY_SEC", "9000") or 9000)),
    "icon_seamless": int(float(os.environ.get("PM_MODEL_RELEASE_ICON_DELAY_SEC", "7200") or 7200)),
    "ecmwf_ifs025": int(float(os.environ.get("PM_MODEL_RELEASE_ECMWF_DELAY_SEC", "18000") or 18000)),
}

PEAK_TZ = ZoneInfo(os.environ.get("PM_MODEL_RELEASE_TARGET_TZ", "Asia/Shanghai") or "Asia/Shanghai")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(text: Any) -> Optional[datetime]:
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _parse_models(raw: str) -> List[str]:
    models = []
    for part in str(raw or "").split(","):
        model = part.strip()
        if model in MODEL_CYCLES:
            models.append(model)
    return models or ["gfs_seamless", "icon_seamless", "ecmwf_ifs025"]


def _parse_http_date(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
    except Exception:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _event_day(value: str) -> date:
    raw = str(value or "").strip().split("T", 1)[0]
    return date.fromisoformat(raw)


def _cycle_candidates(model: str, now: datetime) -> List[datetime]:
    hours = MODEL_CYCLES.get(model) or []
    today0 = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = []
    for offset in range(-2, 1):
        base = today0 + timedelta(days=offset)
        for hour in hours:
            cycle = base + timedelta(hours=hour)
            if cycle <= now:
                candidates.append(cycle)
    candidates.sort(reverse=True)
    return candidates


def _required_forecast_hours_for_peak_window(
    *,
    model: str,
    cycle: datetime,
    event_date: date,
    peak_start_hour: int,
    peak_end_hour: int,
) -> List[int]:
    if cycle.tzinfo is None:
        cycle = cycle.replace(tzinfo=timezone.utc)
    step = 1
    if model == "ecmwf_ifs025":
        step = 3
    hours: List[int] = []
    local_start = datetime(
        event_date.year,
        event_date.month,
        event_date.day,
        max(0, min(23, peak_start_hour)),
        0,
        tzinfo=PEAK_TZ,
    )
    local_end = datetime(
        event_date.year,
        event_date.month,
        event_date.day,
        max(0, min(23, peak_end_hour)),
        0,
        tzinfo=PEAK_TZ,
    )
    if local_end < local_start:
        local_end = local_start
    t = local_start
    while t <= local_end:
        fh = round((t.astimezone(timezone.utc) - cycle.astimezone(timezone.utc)).total_seconds() / 3600.0)
        if fh >= 0 and fh % step == 0:
            hours.append(int(fh))
        t += timedelta(hours=1)
    if not hours:
        center = local_start + (local_end - local_start) / 2
        fh = round((center.astimezone(timezone.utc) - cycle.astimezone(timezone.utc)).total_seconds() / 3600.0)
        if fh >= 0:
            if step > 1:
                fh = int(round(fh / step) * step)
            hours.append(max(0, int(fh)))
    return sorted(set(hours))


def _gfs_url(cycle: datetime, forecast_hour: int) -> str:
    ymd = cycle.strftime("%Y%m%d")
    hh = cycle.strftime("%H")
    return f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{forecast_hour:03d}"


def _gfs_prefix(cycle: datetime) -> str:
    ymd = cycle.strftime("%Y%m%d")
    hh = cycle.strftime("%H")
    return f"gfs.{ymd}/{hh}/atmos/"


def _icon_url(cycle: datetime, forecast_hour: int) -> str:
    ymdh = cycle.strftime("%Y%m%d%H")
    hh = cycle.strftime("%H")
    return (
        "https://opendata.dwd.de/weather/nwp/icon/grib/"
        f"{hh}/t_2m/icon_global_icosahedral_single-level_{ymdh}_{forecast_hour:03d}_T_2M.grib2.bz2"
    )


def _ecmwf_ifs_url(cycle: datetime, forecast_hour: int) -> str:
    ymd = cycle.strftime("%Y%m%d")
    hh = cycle.strftime("%H")
    ymdhms = cycle.strftime("%Y%m%d%H0000")
    return (
        "https://ecmwf-forecasts.s3.amazonaws.com/"
        f"{ymd}/{hh}z/ifs/0p25/oper/{ymdhms}-{forecast_hour}h-oper-fc.grib2"
    )


def _ecmwf_prefix(cycle: datetime, *, family: str = "ifs") -> str:
    ymd = cycle.strftime("%Y%m%d")
    hh = cycle.strftime("%H")
    if family == "aifs-single":
        return f"{ymd}/{hh}z/aifs-single/0p25/oper/"
    return f"{ymd}/{hh}z/ifs/0p25/oper/"


def _head(session: requests.Session, url: str, timeout_sec: float) -> Tuple[bool, int, str, str]:
    try:
        resp = session.head(url, timeout=timeout_sec, allow_redirects=True)
    except requests.RequestException as exc:
        return False, 0, "", str(exc)
    last_modified = _parse_http_date(resp.headers.get("Last-Modified", ""))
    return 200 <= int(resp.status_code) < 300, int(resp.status_code), last_modified, ""


def _list_s3_keys(session: requests.Session, prefix: str, timeout_sec: float, *, max_keys: int = 1000) -> Tuple[List[Dict[str, str]], str]:
    url = (
        "https://ecmwf-forecasts.s3.amazonaws.com/"
        f"?list-type=2&max-keys={max(1, min(int(max_keys), 1000))}&prefix={quote_plus(prefix)}"
    )
    try:
        resp = session.get(url, timeout=timeout_sec)
    except requests.RequestException as exc:
        return [], str(exc)
    if not (200 <= int(resp.status_code) < 300):
        return [], f"http_{resp.status_code}"
    text = resp.text or ""
    pairs = []
    for match in re.finditer(r"<Contents>.*?<Key>(.*?)</Key>.*?<LastModified>(.*?)</LastModified>.*?</Contents>", text, re.S):
        pairs.append(
            {
                "key": match.group(1),
                "last_modified": _parse_iso(match.group(2).replace("Z", "+00:00")).isoformat()
                if _parse_iso(match.group(2).replace("Z", "+00:00"))
                else match.group(2),
            }
        )
    return pairs, ""


def _list_noaa_gfs_keys(
    session: requests.Session, prefix: str, timeout_sec: float, *, max_keys: int = 1000
) -> Tuple[List[Dict[str, str]], str]:
    url = (
        "https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
        f"?list-type=2&max-keys={max(1, min(int(max_keys), 1000))}&prefix={quote_plus(prefix)}"
    )
    try:
        resp = session.get(url, timeout=timeout_sec)
    except requests.RequestException as exc:
        return [], str(exc)
    if not (200 <= int(resp.status_code) < 300):
        return [], f"http_{resp.status_code}"
    text = resp.text or ""
    pairs = []
    for match in re.finditer(r"<Contents>.*?<Key>(.*?)</Key>.*?<LastModified>(.*?)</LastModified>.*?</Contents>", text, re.S):
        parsed = _parse_iso(match.group(2).replace("Z", "+00:00"))
        pairs.append(
            {
                "key": match.group(1),
                "last_modified": parsed.isoformat() if parsed else match.group(2),
            }
        )
    return pairs, ""


def _load_previous(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _previous_model(prev: Mapping[str, Any], model: str, run_id: str) -> Mapping[str, Any]:
    for item in prev.get("models") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("model") or "") == model and str(item.get("run_id") or "") == run_id:
            return item
    return {}


def _probe_head_model(
    *,
    session: requests.Session,
    model: str,
    cycle: datetime,
    required_hours: List[int],
    timeout_sec: float,
) -> Dict[str, Any]:
    url_fn = _gfs_url if model == "gfs_seamless" else _icon_url
    available: List[int] = []
    missing: List[int] = []
    sample_urls: List[str] = []
    last_modified_values: List[str] = []
    errors: List[str] = []
    status_codes: Dict[str, int] = {}
    metadata: Dict[str, Any] = {}
    gfs_keys: List[Dict[str, str]] = []
    if model == "gfs_seamless":
        prefix = _gfs_prefix(cycle)
        gfs_keys, list_error = _list_noaa_gfs_keys(session, prefix, timeout_sec, max_keys=1000)
        key_names = [str(entry.get("key") or "") for entry in gfs_keys]
        hh = cycle.strftime("%H")
        metadata.update(
            {
                "gfs_official_prefix": prefix,
                "gfs_official_prefix_key_count": len(gfs_keys),
                "gfs_official_prefix_empty": not gfs_keys and not list_error,
                "gfs_official_prefix_list_error": list_error,
                "gfs_required_hour_keys_present": {
                    str(fh): any(f"gfs.t{hh}z.pgrb2.0p25.f{fh:03d}" in key for key in key_names)
                    for fh in required_hours
                },
                "gfs_prefix_sample_keys": key_names[:8],
            }
        )
    for fh in required_hours:
        url = url_fn(cycle, fh)
        if len(sample_urls) < 4:
            sample_urls.append(url)
        ok, status_code, last_modified, error = _head(session, url, timeout_sec)
        status_codes[str(fh)] = status_code
        if ok:
            available.append(fh)
            if last_modified:
                last_modified_values.append(last_modified)
        else:
            missing.append(fh)
            if error:
                errors.append(error)
    return {
        "available_forecast_hours": available,
        "missing_forecast_hours": missing,
        "sample_urls": sample_urls,
        "latest_file_last_modified": max(last_modified_values) if last_modified_values else "",
        "probe_errors": errors[:3],
        "status_codes": status_codes,
        **metadata,
    }


def _probe_ecmwf(
    *,
    session: requests.Session,
    cycle: datetime,
    required_hours: List[int],
    timeout_sec: float,
) -> Dict[str, Any]:
    ifs_prefix = _ecmwf_prefix(cycle, family="ifs")
    aifs_prefix = _ecmwf_prefix(cycle, family="aifs-single")
    ifs_keys, ifs_error = _list_s3_keys(session, ifs_prefix, timeout_sec, max_keys=200)
    aifs_keys, aifs_error = _list_s3_keys(session, aifs_prefix, timeout_sec, max_keys=200)
    available: List[int] = []
    missing: List[int] = []
    last_modified_values: List[str] = []
    key_by_hour: Dict[int, Dict[str, str]] = {}
    for entry in ifs_keys:
        key = str(entry.get("key") or "")
        match = re.search(r"-(\d+)h-oper-fc\.grib2$", key)
        if not match:
            continue
        fh = int(match.group(1))
        key_by_hour[fh] = entry
    for fh in required_hours:
        entry = key_by_hour.get(int(fh))
        if entry:
            available.append(int(fh))
            if entry.get("last_modified"):
                last_modified_values.append(str(entry["last_modified"]))
        else:
            missing.append(int(fh))
    sample_urls = [_ecmwf_ifs_url(cycle, fh) for fh in required_hours[:4]]
    aifs_sample = [entry.get("key") for entry in aifs_keys[:4] if entry.get("key")]
    return {
        "available_forecast_hours": available,
        "missing_forecast_hours": missing,
        "sample_urls": sample_urls,
        "latest_file_last_modified": max(last_modified_values) if last_modified_values else "",
        "probe_errors": [err for err in [ifs_error] if err][:3],
        "status_codes": {},
        "ecmwf_ifs_prefix": ifs_prefix,
        "ecmwf_ifs_key_count": len(ifs_keys),
        "ecmwf_aifs_prefix": aifs_prefix,
        "ecmwf_aifs_key_count": len(aifs_keys),
        "ecmwf_aifs_sample_keys": aifs_sample,
        "ecmwf_aifs_error": aifs_error,
    }


def _model_source(model: str) -> str:
    if model == "gfs_seamless":
        return "NOAA GFS AWS public dataset"
    if model == "icon_seamless":
        return "DWD OpenData ICON"
    if model == "ecmwf_ifs025":
        return "ECMWF Open Data AWS replica"
    return "official"


def _watch_model(
    *,
    session: requests.Session,
    model: str,
    event_date: date,
    now: datetime,
    timeout_sec: float,
    peak_start_hour: int,
    peak_end_hour: int,
    previous: Mapping[str, Any],
) -> Dict[str, Any]:
    cycles = _cycle_candidates(model, now)
    selected: Optional[datetime] = None
    required: List[int] = []
    for cycle in cycles:
        req = _required_forecast_hours_for_peak_window(
            model=model,
            cycle=cycle,
            event_date=event_date,
            peak_start_hour=peak_start_hour,
            peak_end_hour=peak_end_hour,
        )
        if req:
            selected = cycle
            required = req
            break
    if selected is None:
        selected = cycles[0] if cycles else now.replace(hour=0, minute=0, second=0, microsecond=0)
    run_id = selected.strftime("%Y%m%d%H")
    expected_due_at = selected + timedelta(seconds=MODEL_MIN_RELEASE_DELAY_SEC.get(model, 7200))
    if model == "ecmwf_ifs025":
        probe = _probe_ecmwf(session=session, cycle=selected, required_hours=required, timeout_sec=timeout_sec)
    else:
        probe = _probe_head_model(
            session=session,
            model=model,
            cycle=selected,
            required_hours=required,
            timeout_sec=timeout_sec,
        )
    available = [int(v) for v in probe.get("available_forecast_hours") or []]
    missing = [int(v) for v in probe.get("missing_forecast_hours") or []]
    complete = bool(required) and len(available) == len(required)
    any_available = bool(available)
    if complete:
        status = "complete"
        reason = "chengdu_peak_window_forecast_hours_available"
    elif now < expected_due_at and not any_available:
        status = "not_yet_due"
        reason = "official_run_not_due"
    elif any_available:
        status = "partial"
        reason = "official_run_partially_available"
    else:
        status = "no_files"
        if model == "gfs_seamless" and probe.get("gfs_official_prefix_empty"):
            reason = "official_run_prefix_empty"
        elif model == "gfs_seamless" and probe.get("gfs_official_prefix_key_count"):
            reason = "official_peak_window_forecast_hours_not_available"
        else:
            reason = "official_prefix_or_forecast_hours_not_available"
    prev = _previous_model(previous, model, run_id)
    first_seen_at = str(prev.get("first_seen_at") or "")
    complete_first_seen_at = str(prev.get("complete_first_seen_at") or "")
    checked_at = _iso(now)
    if any_available and not first_seen_at:
        first_seen_at = checked_at
    if complete and not complete_first_seen_at:
        complete_first_seen_at = checked_at
    return {
        "model": model,
        "label": MODEL_LABELS.get(model, model),
        "run_id": run_id,
        "cycle_time_utc": _iso(selected),
        "expected_due_at": _iso(expected_due_at),
        "official_source": _model_source(model),
        "source_role": "official_release_watcher",
        "event_date": event_date.isoformat(),
        "peak_window_local": {
            "tz": str(PEAK_TZ),
            "start_hour": peak_start_hour,
            "end_hour": peak_end_hour,
        },
        "required_forecast_hours": required,
        "available_forecast_hours": available,
        "missing_forecast_hours": missing,
        "complete_for_chengdu_peak": complete,
        "run_complete": complete,
        "first_seen_at": first_seen_at,
        "complete_first_seen_at": complete_first_seen_at,
        "latest_file_last_modified": str(probe.get("latest_file_last_modified") or ""),
        "checked_at": checked_at,
        "status": status,
        "status_reason": reason,
        "sample_urls": probe.get("sample_urls") or [],
        "probe_errors": probe.get("probe_errors") or [],
        "status_codes": probe.get("status_codes") or {},
        "probe_metadata": {
            key: value
            for key, value in probe.items()
            if key.startswith(("ecmwf_", "gfs_"))
        },
    }


def build_payload(
    *,
    event_date: date,
    models: Iterable[str],
    timeout_sec: float,
    previous: Mapping[str, Any],
) -> Dict[str, Any]:
    now = _now_utc()
    peak_start = _env_int("PM_MODEL_RELEASE_PEAK_START_LOCAL", 12)
    peak_end = _env_int("PM_MODEL_RELEASE_PEAK_END_LOCAL", 18)
    session = requests.Session()
    watched = [
        _watch_model(
            session=session,
            model=model,
            event_date=event_date,
            now=now,
            timeout_sec=timeout_sec,
            peak_start_hour=peak_start,
            peak_end_hour=peak_end,
            previous=previous,
        )
        for model in models
    ]
    return {
        "schema": "model_release_watcher_v1",
        "generated_at": _iso(now),
        "event_date": event_date.isoformat(),
        "target": "chengdu_peak_window",
        "models": watched,
        "notes": [
            "HEAD/list official release files only; no GRIB payload download.",
            "Open-Meteo cache remains fallback/packaging, not first-time official release SSOT.",
        ],
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Watch official NWP release files for Chengdu peak-window availability.")
    parser.add_argument("--event-date", default="", help="Asia/Shanghai event date, YYYY-MM-DD. Default: today local.")
    parser.add_argument("--models", default="gfs_seamless,icon_seamless,ecmwf_ifs025")
    parser.add_argument("--timeout-sec", type=float, default=float(os.environ.get("PM_MODEL_RELEASE_WATCHER_TIMEOUT_SEC", "6") or 6))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    if args.event_date:
        day = _event_day(args.event_date)
    else:
        day = datetime.now(PEAK_TZ).date()
    output = Path(args.output).expanduser()
    previous = _load_previous(output)
    payload = build_payload(
        event_date=day,
        models=_parse_models(args.models),
        timeout_sec=max(1.0, float(args.timeout_sec or 6.0)),
        previous=previous,
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.no_write:
        print(text)
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(output)
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
