#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scan_weather_market as swm  # noqa: E402

DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "chengdu_model_history_cache_backfill_latest.json"
DEFAULT_STATION_ID = "ZUUU"
DEFAULT_LOC_KEY = "30.5583,103.9460"
DEFAULT_CONFIRM = "APPLY_CHENGDU_MODEL_HISTORY"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

MODEL_API_NAMES = {
    "gfs_seamless": "gfs_global",
    "icon_seamless": "icon_global",
    "ecmwf_ifs025": "ecmwf_ifs025",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _iter_dates(start_date: str, end_date: str) -> Iterable[str]:
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _parse_loc_key(loc_key: str) -> Tuple[float, float]:
    lat_raw, lon_raw = str(loc_key or "").split(",", 1)
    lat = float(lat_raw)
    lon = float(lon_raw)
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise ValueError("invalid_loc_key")
    return lat, lon


def _members_signature(values: List[float]) -> str:
    payload = json.dumps([float(v) for v in values], separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _model_field(model: str) -> str:
    api_name = MODEL_API_NAMES.get(model)
    if not api_name:
        raise ValueError(f"unsupported_model:{model}")
    return f"temperature_2m_max_{api_name}"


def _available_wu_range(conn: sqlite3.Connection, station_id: str) -> Tuple[str, str]:
    row = conn.execute(
        """
        SELECT MIN(event_date), MAX(event_date)
        FROM event_daily_temps
        WHERE upper(coalesce(station_id, '')) = upper(?)
          AND market_var = 'temperature_2m_max'
          AND market_unit = 'C'
          AND settlement_actual IS NOT NULL
          AND lower(coalesce(resolution_source, '')) LIKE '%wunderground.com/history/daily/cn/chengdu/zuuu%'
        """,
        (station_id,),
    ).fetchone()
    if row and row[0] and row[1]:
        return str(row[0]), str(row[1])
    today = date.today().isoformat()
    return today, today


def _resolve_window(conn: sqlite3.Connection, args: argparse.Namespace) -> Tuple[str, str, str]:
    station_id = str(args.station_id or DEFAULT_STATION_ID).strip().upper()
    available_start, available_end = _available_wu_range(conn, station_id)
    if args.start_date:
        return str(args.start_date), str(args.end_date or available_end), "explicit_start_date"
    if args.days and int(args.days) > 0:
        end = date.fromisoformat(str(args.end_date or available_end))
        start = end - timedelta(days=max(1, int(args.days)) - 1)
        return start.isoformat(), end.isoformat(), "explicit_days"
    if args.end_date:
        return available_start, str(args.end_date), "available_start_to_explicit_end"
    return available_start, available_end, "available_wu_history"


def _existing_cache_keys(
    conn: sqlite3.Connection,
    *,
    loc_key: str,
    start_date: str,
    end_date: str,
    models: List[str],
) -> set[Tuple[str, str]]:
    if not models:
        return set()
    placeholders = ",".join("?" for _ in models)
    rows = conn.execute(
        f"""
        SELECT model, event_date
        FROM model_member_cache
        WHERE loc_key = ?
          AND event_date >= ?
          AND event_date <= ?
          AND market_var = 'temperature_2m_max'
          AND market_unit = 'C'
          AND model IN ({placeholders})
        """,
        (loc_key, start_date, end_date, *models),
    ).fetchall()
    return {(str(row["model"]), str(row["event_date"])) for row in rows}


def _fetch_historical_forecast_chunk(
    *,
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
    timezone_name: str,
    models: List[str],
    timeout: int,
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Any]]:
    api_models = [MODEL_API_NAMES[m] for m in models if m in MODEL_API_NAMES]
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "start_date": start_date,
        "end_date": end_date,
        "timezone": timezone_name,
        "models": ",".join(api_models),
    }
    resp = swm.requests_get(
        HISTORICAL_FORECAST_URL,
        params=params,
        bypass_proxy=True,
        timeout=max(1, int(timeout)),
        retries=3,
        backoff=1.0,
    )
    meta = {
        "url": resp.url,
        "status_code": int(resp.status_code),
        "response_date": resp.headers.get("Date") or "",
    }
    resp.raise_for_status()
    payload = resp.json()
    daily = payload.get("daily") if isinstance(payload, Mapping) else None
    if not isinstance(daily, Mapping):
        return {}, {**meta, "ok": False, "reason": "daily_missing"}
    dates = daily.get("time")
    if not isinstance(dates, list):
        return {}, {**meta, "ok": False, "reason": "time_missing"}
    values_by_model: Dict[str, Dict[str, float]] = {m: {} for m in models}
    for model in models:
        field = _model_field(model)
        values = daily.get(field)
        if not isinstance(values, list):
            continue
        for idx, raw_date in enumerate(dates):
            if idx >= len(values):
                continue
            event_date = str(raw_date or "").strip()
            value = _to_float(values[idx])
            if event_date and value is not None:
                values_by_model.setdefault(model, {})[event_date] = float(value)
    return values_by_model, {**meta, "ok": True, "models": api_models}


def _collect_history_values(
    *,
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
    timezone_name: str,
    models: List[str],
    timeout: int,
    chunk_days: int,
    sleep_sec: float,
) -> Tuple[Dict[str, Dict[str, float]], List[Dict[str, Any]]]:
    out: Dict[str, Dict[str, float]] = {m: {} for m in models}
    fetches: List[Dict[str, Any]] = []
    chunk = max(1, min(int(chunk_days or 31), 92))
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    while current <= end:
        chunk_end = min(end, current + timedelta(days=chunk - 1))
        chunk_start_s = current.isoformat()
        chunk_end_s = chunk_end.isoformat()
        try:
            values, meta = _fetch_historical_forecast_chunk(
                lat=lat,
                lon=lon,
                start_date=chunk_start_s,
                end_date=chunk_end_s,
                timezone_name=timezone_name,
                models=models,
                timeout=timeout,
            )
        except Exception as exc:
            values = {}
            meta = {
                "ok": False,
                "start_date": chunk_start_s,
                "end_date": chunk_end_s,
                "reason": f"{exc.__class__.__name__}:{exc}",
            }
        meta.setdefault("start_date", chunk_start_s)
        meta.setdefault("end_date", chunk_end_s)
        fetches.append(meta)
        for model, by_date in values.items():
            out.setdefault(model, {}).update(by_date)
        current = chunk_end + timedelta(days=1)
        if sleep_sec > 0 and current <= end:
            time.sleep(float(sleep_sec))
    return out, fetches


def _candidate_rows(
    *,
    loc_key: str,
    values_by_model: Mapping[str, Mapping[str, float]],
    existing: set[Tuple[str, str]],
    force: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    now = _utc_now()
    for model, values in sorted(values_by_model.items()):
        for event_date, value in sorted((values or {}).items()):
            key = (str(model), str(event_date))
            if key in existing and not force:
                skipped.append({"model": model, "event_date": event_date, "reason": "already_cached"})
                continue
            members = [float(value)]
            rows.append(
                {
                    "model": model,
                    "loc_key": loc_key,
                    "event_date": event_date,
                    "market_var": "temperature_2m_max",
                    "market_unit": "C",
                    "members": members,
                    "members_json": json.dumps(members, ensure_ascii=True, separators=(",", ":")),
                    "created_at": now,
                    "official_updated_at": f"{event_date}T00:00:00+00:00",
                    "official_updated_source": "open_meteo_historical_forecast",
                    "members_signature": f"historical_forecast:{_members_signature(members)}",
                }
            )
    return rows, skipped


def _apply_rows(conn: sqlite3.Connection, rows: List[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    cur = conn.cursor()
    inserted = 0
    for row in rows:
        cur.execute(
            """
            INSERT INTO model_member_cache (
                model, loc_key, event_date, market_var, market_unit,
                members_json, created_at, official_updated_at, official_updated_source,
                members_signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["model"],
                row["loc_key"],
                row["event_date"],
                row["market_var"],
                row["market_unit"],
                row["members_json"],
                row["created_at"],
                row["official_updated_at"],
                row["official_updated_source"],
                row["members_signature"],
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    station_id = str(args.station_id or DEFAULT_STATION_ID).strip().upper() or DEFAULT_STATION_ID
    loc_key = str(args.loc_key or DEFAULT_LOC_KEY).strip() or DEFAULT_LOC_KEY
    models = [
        swm.normalize_model_name(part.strip())
        for part in str(args.models or "").split(",")
        if part.strip()
    ]
    models = [m for m in models if m in MODEL_API_NAMES]
    if not models:
        models = list(MODEL_API_NAMES.keys())
    lat, lon = _parse_loc_key(loc_key)
    db_path = Path(args.db)
    write_mode = bool(args.apply)
    if write_mode and str(args.confirm_apply or "") != DEFAULT_CONFIRM:
        raise SystemExit(f"--confirm-apply must equal {DEFAULT_CONFIRM}")
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        start_date, end_date, window_policy = _resolve_window(conn, args)
        existing = _existing_cache_keys(
            conn,
            loc_key=loc_key,
            start_date=start_date,
            end_date=end_date,
            models=models,
        )
        values_by_model, fetches = _collect_history_values(
            lat=lat,
            lon=lon,
            start_date=start_date,
            end_date=end_date,
            timezone_name=str(args.timezone or "Asia/Shanghai"),
            models=models,
            timeout=int(args.timeout or 30),
            chunk_days=int(args.chunk_days or 31),
            sleep_sec=float(args.sleep_sec or 0.0),
        )
        rows, skipped = _candidate_rows(
            loc_key=loc_key,
            values_by_model=values_by_model,
            existing=existing,
            force=bool(args.force),
        )
        if args.max_rows and int(args.max_rows) > 0:
            rows = rows[: int(args.max_rows)]
        inserted = _apply_rows(conn, rows) if write_mode else 0
    finally:
        conn.close()

    dates_by_model = {
        model: sorted((values_by_model.get(model) or {}).keys())
        for model in models
    }
    candidate_dates_by_model: Dict[str, List[str]] = {}
    for row in rows:
        candidate_dates_by_model.setdefault(str(row["model"]), []).append(str(row["event_date"]))
    return {
        "ok": True,
        "applied": write_mode,
        "inserted": int(inserted),
        "read_only": not write_mode,
        "db_path": str(db_path),
        "scope": "chengdu_zuuu_temperature_2m_max_model_history_cache",
        "station_id": station_id,
        "loc_key": loc_key,
        "lat": lat,
        "lon": lon,
        "source": "open_meteo_historical_forecast_api",
        "source_url": HISTORICAL_FORECAST_URL,
        "source_note": (
            "This expands Chengdu historical model samples with Open-Meteo historical forecast daily values. "
            "It is not a recovered original ensemble-member cache; each model/date is stored as a one-member historical forecast sample."
        ),
        "models": models,
        "api_models": {m: MODEL_API_NAMES[m] for m in models},
        "start_date": start_date,
        "end_date": end_date,
        "history_window_policy": window_policy,
        "generated_at": _utc_now(),
        "existing_model_date_keys": len(existing),
        "fetched_date_ranges_by_model": {
            model: {
                "n_dates": len(dates),
                "min_date": min(dates) if dates else "",
                "max_date": max(dates) if dates else "",
            }
            for model, dates in sorted(dates_by_model.items())
        },
        "candidate_rows": len(rows),
        "candidate_dates_by_model": {
            model: {
                "n_dates": len(dates),
                "min_date": min(dates) if dates else "",
                "max_date": max(dates) if dates else "",
            }
            for model, dates in sorted(candidate_dates_by_model.items())
        },
        "skipped_existing_rows": len(skipped),
        "fetches": fetches,
        "rows": rows[: int(args.preview_rows or 20)],
        "skipped_examples": skipped[: int(args.preview_rows or 20)],
        "apply_confirm_phrase": DEFAULT_CONFIRM,
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill Chengdu/ZUUU historical model cache samples")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--station-id", default=DEFAULT_STATION_ID)
    parser.add_argument("--loc-key", default=DEFAULT_LOC_KEY)
    parser.add_argument("--models", default="gfs_seamless,icon_seamless,ecmwf_ifs025")
    parser.add_argument("--days", type=int, default=0, help="0 means all available WU history")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--chunk-days", type=int, default=31)
    parser.add_argument("--sleep-sec", type=float, default=0.2)
    parser.add_argument("--max-rows", type=int, default=0)
    parser.add_argument("--preview-rows", type=int, default=20)
    parser.add_argument("--force", action="store_true", help="insert even if model/date already exists")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-apply", default="")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        summary = {
            "ok": report.get("ok"),
            "applied": report.get("applied"),
            "inserted": report.get("inserted"),
            "out": str(out),
            "start_date": report.get("start_date"),
            "end_date": report.get("end_date"),
            "existing_model_date_keys": report.get("existing_model_date_keys"),
            "candidate_rows": report.get("candidate_rows"),
            "candidate_dates_by_model": report.get("candidate_dates_by_model"),
            "source_note": report.get("source_note"),
            "apply_confirm_phrase": report.get("apply_confirm_phrase"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
