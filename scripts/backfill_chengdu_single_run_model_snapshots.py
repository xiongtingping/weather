#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scan_weather_market as swm  # noqa: E402
import ui.server as ui_server  # noqa: E402

DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "chengdu_single_run_model_snapshot_backfill_latest.json"
CONFIRM_PHRASE = "APPLY_CHENGDU_SINGLE_RUN_MODEL_SNAPSHOTS"
SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
SOURCE = "open_meteo_single_runs"
LOCAL_MODELS = ("gfs_seamless", "icon_seamless", "ecmwf_ifs025")
API_MODELS = {
    "gfs_seamless": "gfs_global",
    "icon_seamless": "icon_global",
    "ecmwf_ifs025": "ecmwf_ifs025",
}


@dataclass(frozen=True)
class Candidate:
    event_date: str
    slug: str
    lead_day: str
    run_init_utc: str
    estimated_publish_lag_hours: int = 6

    @property
    def key(self) -> str:
        return f"{self.slug}|{self.event_date}|{self.lead_day}|{self.run_init_utc}"


CANDIDATES: Tuple[Candidate, ...] = (
    Candidate("2026-05-31", "highest-temperature-in-chengdu-on-may-31-2026", "D0", "2026-05-30T18:00"),
    Candidate("2026-06-01", "highest-temperature-in-chengdu-on-june-1-2026", "D1", "2026-05-31T00:00"),
    Candidate("2026-06-04", "highest-temperature-in-chengdu-on-june-4-2026", "D1", "2026-06-03T06:00"),
    Candidate("2026-06-04", "highest-temperature-in-chengdu-on-june-4-2026", "D0", "2026-06-03T12:00"),
    Candidate("2026-06-08", "highest-temperature-in-chengdu-on-june-8-2026", "D1", "2026-06-07T06:00"),
    Candidate("2026-06-08", "highest-temperature-in-chengdu-on-june-8-2026", "D0", "2026-06-08T00:00"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_run_ts(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _run_date_for_lead(event_date: str, lead_day: str) -> str:
    return ui_server._lead_day_run_date_key(event_date, lead_day)  # type: ignore[attr-defined]


def _load_event_meta(conn: sqlite3.Connection, slug: str, event_date: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM event_runs
        WHERE slug = ? AND event_date = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (slug, event_date),
    ).fetchone()
    if not row:
        return {}
    return {key: row[key] for key in row.keys()}


def _latest_market_snapshot(
    conn: sqlite3.Connection,
    slug: str,
    event_date: str,
    window_start: str,
    window_end: str,
    model_created_at: str,
    sync_window_sec: float,
) -> Tuple[Optional[int], str, Dict[str, Any], Dict[str, Any]]:
    max_ts = ui_server._iso_add_seconds(model_created_at, sync_window_sec)  # type: ignore[attr-defined]
    row = conn.execute(
        """
        SELECT id, payload_json, meta_json, created_at
        FROM market_snapshots
        WHERE slug = ?
          AND event_date = ?
          AND created_at >= ?
          AND created_at < ?
          AND created_at >= ?
          AND created_at <= ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (slug, event_date, window_start, window_end, model_created_at, max_ts),
    ).fetchone()
    if not row:
        return None, "", {}, {}
    payload = ui_server.safe_json_loads(row["payload_json"])
    meta = ui_server.safe_json_loads(row["meta_json"])
    return int(row["id"]), str(row["created_at"] or ""), payload if isinstance(payload, dict) else {}, meta if isinstance(meta, dict) else {}


def _market_token_rows(conn: sqlite3.Connection, slug: str, event_date: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT outcome, yes_token, no_token, outcome_volume, liquidity,
               market_active, market_closed, market_archived,
               market_accepting_orders, market_enable_orderbook, updated_at
        FROM market_tokens
        WHERE slug = ? AND event_date = ?
        ORDER BY outcome
        """,
        (slug, event_date),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        outcome = str(row["outcome"] or "").strip()
        if not outcome:
            continue
        out.append(
            {
                "outcome": outcome,
                "yes_token": row["yes_token"],
                "no_token": row["no_token"],
                "outcome_volume": row["outcome_volume"],
                "liquidity": row["liquidity"],
                "market_active": bool(row["market_active"]),
                "market_closed": bool(row["market_closed"]),
                "market_archived": bool(row["market_archived"]),
                "market_accepting_orders": bool(row["market_accepting_orders"]),
                "market_enable_orderbook": bool(row["market_enable_orderbook"]),
                "token_mapping_source": "market_tokens",
                "token_mapping_updated_at": str(row["updated_at"] or ""),
            }
        )
    return out


def _fetch_single_run_value(
    *,
    lat: float,
    lon: float,
    event_date: str,
    run_init_utc: str,
    api_model: str,
    timeout: int,
) -> Tuple[float, str]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "timezone": "Asia/Shanghai",
        "run": run_init_utc,
        "forecast_days": 4,
        "models": api_model,
    }
    url = SINGLE_RUNS_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=max(1, int(timeout))) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    daily = payload.get("daily") if isinstance(payload, Mapping) else None
    if not isinstance(daily, Mapping):
        raise ValueError(f"daily_missing:{api_model}:{run_init_utc}")
    times = daily.get("time")
    values = daily.get("temperature_2m_max")
    if not isinstance(times, list) or not isinstance(values, list) or event_date not in times:
        raise ValueError(f"event_date_missing:{api_model}:{run_init_utc}:{event_date}")
    idx = times.index(event_date)
    if idx >= len(values):
        raise ValueError(f"value_missing:{api_model}:{run_init_utc}:{event_date}")
    value = _to_float(values[idx])
    if value is None:
        raise ValueError(f"value_not_numeric:{api_model}:{run_init_utc}:{event_date}")
    return float(value), url


def _fetch_candidate_values(
    candidate: Candidate,
    *,
    lat: float,
    lon: float,
    timeout: int,
) -> Tuple[Dict[str, float], Dict[str, str]]:
    values: Dict[str, float] = {}
    urls: Dict[str, str] = {}
    for local_model in LOCAL_MODELS:
        value, url = _fetch_single_run_value(
            lat=lat,
            lon=lon,
            event_date=candidate.event_date,
            run_init_utc=candidate.run_init_utc,
            api_model=API_MODELS[local_model],
            timeout=timeout,
        )
        values[local_model] = value
        urls[local_model] = url
    return values, urls


def _existing_external_snapshot_id(conn: sqlite3.Connection, candidate: Candidate) -> Optional[int]:
    rows = conn.execute(
        """
        SELECT id, meta_json
        FROM model_snapshots
        WHERE slug = ? AND event_date = ?
        ORDER BY id DESC
        """,
        (candidate.slug, candidate.event_date),
    ).fetchall()
    for row in rows:
        meta = ui_server.safe_json_loads(row["meta_json"])
        if not isinstance(meta, dict):
            continue
        if meta.get("external_single_run_key") == candidate.key:
            return int(row["id"])
    return None


def _build_model_payload(
    *,
    candidate: Candidate,
    event_meta: Mapping[str, Any],
    market_payload: Mapping[str, Any],
    token_rows: Iterable[Mapping[str, Any]],
    model_values: Mapping[str, float],
    model_urls: Mapping[str, str],
    run_date: str,
    model_created_at: str,
) -> Dict[str, Any]:
    rounding_rule = str(event_meta.get("rounding_rule") or "whole_degrees")
    market_var = str(event_meta.get("market_var") or "temperature_2m_max")
    market_unit = str(event_meta.get("market_unit") or "C")
    model_member_bucket_meta = swm.model_member_bucket_rule_meta(
        rounding_rule,
        market_var=market_var,
        market_unit=market_unit,
        resolution_source=str(event_meta.get("resolution_source") or ""),
    )
    model_member_bucket_rule = str(model_member_bucket_meta.get("rounding_rule") or rounding_rule)
    lat = _to_float(event_meta.get("lat")) or 30.5583
    lon = _to_float(event_meta.get("lon")) or 103.9460
    market_rows = [row for row in (market_payload.get("rows") or []) if isinstance(row, Mapping)]
    rows_by_outcome: Dict[str, Dict[str, Any]] = {}
    for token_row in token_rows:
        label = str(token_row.get("outcome") or "").strip()
        if label:
            rows_by_outcome[label] = dict(token_row)
    for market_row in market_rows:
        label = str(market_row.get("outcome") or "").strip()
        if not label:
            continue
        merged = dict(rows_by_outcome.get(label) or {})
        merged.update(dict(market_row))
        rows_by_outcome[label] = merged
    labels = list(rows_by_outcome.keys())
    if not labels:
        raise ValueError(f"market_outcomes_missing:{candidate.key}")

    models = [model for model in LOCAL_MODELS if model in model_values]
    weights = swm.scenario_weights(models, run_date, candidate.event_date, rounding_rule, market_unit, labels, lat, lon)
    if not weights:
        weights = {model: 1.0 / len(models) for model in models}
    total_weight = sum(float(weights.get(model, 0.0) or 0.0) for model in models)
    if total_weight <= 0:
        weights = {model: 1.0 / len(models) for model in models}
    else:
        weights = {model: float(weights.get(model, 0.0) or 0.0) / total_weight for model in models}

    model_members = {model: [float(model_values[model])] for model in models}
    features = swm._build_weather_likelihood_features(  # type: ignore[attr-defined]
        model_members=model_members,
        market_var=market_var,
        market_unit=market_unit,
    )
    if isinstance(features, dict):
        features["source"] = "open_meteo_single_runs_model_members"
        features["external_archive"] = True
        features["history_only"] = True
        features["single_run_initialized_at"] = candidate.run_init_utc
        features["single_run_estimated_available_at"] = model_created_at

    rows: List[Dict[str, Any]] = []
    def _label_sort_key(label: str) -> Tuple[float, float, str]:
        return ui_server.bucket_sort_key(label)  # type: ignore[attr-defined]

    ordered_rows = [rows_by_outcome[label] for label in sorted(labels, key=_label_sort_key)]
    for market_row in ordered_rows:
        label = str(market_row.get("outcome") or "").strip()
        if not label:
            continue
        spec = swm.parse_outcome_label(label)
        atomic_spec = swm.atomic_temperature_outcome_spec(spec)
        per_model_q: Dict[str, Dict[str, Any]] = {}
        per_model_q_atomic: Dict[str, Dict[str, Any]] = {}
        for model in models:
            values = swm.np.asarray(model_members[model], dtype=float)
            band = swm.compute_q_band(values, spec, model_member_bucket_rule)
            atomic_band = swm.compute_q_band(values, atomic_spec, model_member_bucket_rule)
            band["dir_value"] = float(model_values[model])
            atomic_band["dir_value"] = float(model_values[model])
            band["settlement_rounding_rule"] = rounding_rule
            band["model_member_bucket_rule"] = model_member_bucket_rule
            atomic_band["settlement_rounding_rule"] = rounding_rule
            atomic_band["model_member_bucket_rule"] = model_member_bucket_rule
            per_model_q[model] = band
            per_model_q_atomic[model] = atomic_band
        q = {
            "q_round": 0.0,
            "q_floor": 0.0,
            "q_ceil": 0.0,
            "q_nearest": 0.0,
            "q_min": 0.0,
            "q_max": 0.0,
        }
        for field in ("q_round", "q_floor", "q_ceil", "q_nearest"):
            q[field] = sum(float(weights.get(model, 0.0) or 0.0) * per_model_q[model][field] for model in models)
        q["q_min"] = min(per_model_q[model]["q_min"] for model in models)
        q["q_max"] = max(per_model_q[model]["q_max"] for model in models)
        q["q_raw"] = q["q_round"]

        row = dict(market_row)
        row.update(q)
        row.update(
            {
                "q_model_round": q["q_round"],
                "q_model_temperature_bucket_nearest": q["q_nearest"],
                "q_equal": sum(per_model_q[model]["q_round"] for model in models) / len(models),
                "q_auto": q["q_round"],
                "q_scenario": q["q_round"],
                "q_entry": q["q_round"],
                "q_quote": q["q_round"],
                "q_calibrated": q["q_round"],
                "q_entry_no": 1.0 - q["q_round"],
                "q_quote_no": 1.0 - q["q_round"],
                "q_calibrated_no": 1.0 - q["q_round"],
                "q_raw_no": 1.0 - q["q_round"],
                "q_round_no": 1.0 - q["q_round"],
                "per_model_q": per_model_q,
                "per_model_q_atomic": per_model_q_atomic,
                "q_model_member_bucket_rule": model_member_bucket_rule,
                "q_model_member_bucket_rule_meta": dict(model_member_bucket_meta),
                "q_model_settlement_rounding_rule": rounding_rule,
                "active_models": models,
                "model_snapshot_source": SOURCE,
                "external_archive": True,
                "history_only": True,
                "event_date": candidate.event_date,
                "run_date": run_date,
            }
        )
        rows.append(row)

    members = [float(model_values[model]) for model in models]
    top_label = ""
    top_q = -1.0
    for row in rows:
        qv = _to_float(row.get("q_round")) or 0.0
        if qv > top_q:
            top_q = qv
            top_label = str(row.get("outcome") or "")

    return {
        "slug": candidate.slug,
        "event_url": str(event_meta.get("event_url") or ""),
        "title": str(event_meta.get("title") or ""),
        "description": str(event_meta.get("title") or ""),
        "date": candidate.event_date,
        "station_id": str(event_meta.get("station_id") or "ZUUU"),
        "resolution_source": str(event_meta.get("resolution_source") or ""),
        "market_var": market_var,
        "market_unit": market_unit,
        "rounding_rule": rounding_rule,
        "rows": rows,
        "members": members,
        "models": models,
        "weights": weights,
        "model_members": model_members,
        "model_members_decimal_raw": model_members,
        "model_source_raw_members": model_members,
        "model_member_bucket_rule": model_member_bucket_rule,
        "model_member_bucket_rule_meta": dict(model_member_bucket_meta),
        "settlement_rounding_rule": rounding_rule,
        "model_provenance": {
            model: {
                "source": SOURCE,
                "api_model": API_MODELS[model],
                "single_run_initialized_at": candidate.run_init_utc,
                "estimated_available_at": model_created_at,
                "url": model_urls.get(model, ""),
                "history_only": True,
            }
            for model in models
        },
        "weather_likelihood_features": features,
        "weather_likelihood_refreshed_at": model_created_at,
        "city_most_likely_label": top_label,
        "city_most_likely_q": top_q if top_q >= 0 else None,
        "external_archive": True,
        "history_only": True,
        "source": SOURCE,
        "token_mapping_source": "market_tokens+market_snapshots",
        "single_run_initialized_at": candidate.run_init_utc,
        "single_run_estimated_available_at": model_created_at,
    }


def _insert_model_snapshot(
    conn: sqlite3.Connection,
    *,
    candidate: Candidate,
    run_date: str,
    payload: Mapping[str, Any],
    meta: Mapping[str, Any],
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO model_snapshots (run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_date,
            candidate.event_date,
            candidate.slug,
            json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")),
            json.dumps(dict(meta), ensure_ascii=False, separators=(",", ":")),
            created_at,
        ),
    )
    return int(cur.lastrowid)


def _prepare_candidate(
    conn: sqlite3.Connection,
    candidate: Candidate,
    *,
    timeout: int,
    sync_window_sec: float,
) -> Dict[str, Any]:
    event_meta = _load_event_meta(conn, candidate.slug, candidate.event_date)
    if not event_meta:
        return {"ok": False, "candidate": candidate.key, "error": "event_meta_missing"}
    settlement_meta = ui_server._load_lead_day_materialization_settlement_meta_map(  # type: ignore[attr-defined]
        conn,
        candidate.event_date,
        candidate.event_date,
        scope="chengdu_zuuu",
    )
    meta = settlement_meta.get((candidate.slug, candidate.event_date), {})
    window_start, window_end = ui_server._lead_day_window_for_materialization(  # type: ignore[attr-defined]
        candidate.event_date,
        candidate.lead_day,
        str(meta.get("settlement_known_at") or ""),
        str(meta.get("event_end_utc") or event_meta.get("event_end_utc") or ""),
    )
    if not window_start or not window_end:
        return {"ok": False, "candidate": candidate.key, "error": "bad_lead_day_window"}
    run_date = _run_date_for_lead(candidate.event_date, candidate.lead_day)
    model_created_at = _iso(_parse_run_ts(candidate.run_init_utc) + timedelta(hours=candidate.estimated_publish_lag_hours))
    if not (window_start <= model_created_at < window_end):
        return {
            "ok": False,
            "candidate": candidate.key,
            "error": "estimated_available_at_outside_window",
            "window_start": window_start,
            "window_end": window_end,
            "model_created_at": model_created_at,
        }
    market_id, market_at, market_payload, market_meta = _latest_market_snapshot(
        conn,
        candidate.slug,
        candidate.event_date,
        window_start,
        window_end,
        model_created_at,
        sync_window_sec,
    )
    if market_id is None:
        return {
            "ok": False,
            "candidate": candidate.key,
            "error": "synced_market_snapshot_missing",
            "window_start": window_start,
            "window_end": window_end,
            "model_created_at": model_created_at,
        }
    lat = _to_float(event_meta.get("lat")) or 30.5583
    lon = _to_float(event_meta.get("lon")) or 103.9460
    token_rows = _market_token_rows(conn, candidate.slug, candidate.event_date)
    values, urls = _fetch_candidate_values(candidate, lat=lat, lon=lon, timeout=timeout)
    payload = _build_model_payload(
        candidate=candidate,
        event_meta=event_meta,
        market_payload=market_payload,
        token_rows=token_rows,
        model_values=values,
        model_urls=urls,
        run_date=run_date,
        model_created_at=model_created_at,
    )
    snapshot_meta = {
        "slug": candidate.slug,
        "event_url": str(event_meta.get("event_url") or ""),
        "title": str(event_meta.get("title") or ""),
        "city": str(event_meta.get("city") or "Chengdu"),
        "lat": lat,
        "lon": lon,
        "tz": str(event_meta.get("tz") or "UTC"),
        "location_basis": str(event_meta.get("location_basis") or "station"),
        "resolution_source": str(event_meta.get("resolution_source") or ""),
        "station_id": str(event_meta.get("station_id") or "ZUUU"),
        "rounding_rule": str(event_meta.get("rounding_rule") or "whole_degrees"),
        "market_var": str(event_meta.get("market_var") or "temperature_2m_max"),
        "market_unit": str(event_meta.get("market_unit") or "C"),
        "market_open_at": str(event_meta.get("market_open_at") or ""),
        "event_end_utc": str(event_meta.get("event_end_utc") or meta.get("event_end_utc") or ""),
        "market_closed": 0,
        "external_archive": True,
        "history_only": True,
        "source": SOURCE,
        "source_policy": "single_run_original_model_run_not_historical_forecast_stitching",
        "external_single_run_key": candidate.key,
        "single_run_initialized_at": candidate.run_init_utc,
        "single_run_estimated_available_at": model_created_at,
        "estimated_publish_lag_hours": candidate.estimated_publish_lag_hours,
        "lead_day": candidate.lead_day,
        "market_snapshot_id_for_outcomes": market_id,
        "market_snapshot_at_for_sync": market_at,
        "token_mapping_source": "market_tokens",
        "token_mapping_outcome_count": len(token_rows),
        "sync_window_sec": sync_window_sec,
        "generated_at": _utc_now(),
    }
    return {
        "ok": True,
        "candidate": candidate.key,
        "event_date": candidate.event_date,
        "slug": candidate.slug,
        "lead_day": candidate.lead_day,
        "run_date": run_date,
        "run_init_utc": candidate.run_init_utc,
        "model_created_at": model_created_at,
        "window_start": window_start,
        "window_end": window_end,
        "market_snapshot_id": market_id,
        "market_snapshot_at": market_at,
        "model_values": values,
        "payload": payload,
        "meta": snapshot_meta,
    }


def _materialize_candidate_facts(
    conn: sqlite3.Connection,
    candidates: Iterable[Candidate],
    *,
    apply: bool,
) -> List[Dict[str, Any]]:
    reports: List[Dict[str, Any]] = []
    for candidate in candidates:
        reports.append(
            ui_server.materialize_first_prediction_lead_day_facts(
                conn,
                start_date=candidate.event_date,
                end_date=candidate.event_date,
                scope="chengdu_zuuu",
                lead_days=(candidate.lead_day,),
                apply=apply,
                limit=0,
            )
        )
    return reports


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    apply = bool(args.apply)
    if apply and str(args.confirm_apply or "") != CONFIRM_PHRASE:
        raise SystemExit(f"--confirm-apply must equal {CONFIRM_PHRASE}")
    db_path = Path(args.db)
    work_db = db_path
    temp_dir: Optional[tempfile.TemporaryDirectory[str]] = None
    if not apply:
        temp_dir = tempfile.TemporaryDirectory(prefix="chengdu_single_run_dry_run_")
        work_db = Path(temp_dir.name) / "archive.db"
        shutil.copy2(db_path, work_db)
    conn = sqlite3.connect(str(work_db), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    sync_window_sec = float(args.sync_window_sec or 10800.0)
    prepared: List[Dict[str, Any]] = []
    inserted_snapshot_ids: List[int] = []
    skipped_existing: List[Dict[str, Any]] = []
    try:
        for candidate in CANDIDATES:
            existing_id = _existing_external_snapshot_id(conn, candidate)
            if existing_id is not None:
                skipped_existing.append({"candidate": candidate.key, "model_snapshot_id": existing_id})
                continue
            item = _prepare_candidate(
                conn,
                candidate,
                timeout=int(args.timeout or 30),
                sync_window_sec=sync_window_sec,
            )
            prepared.append(item)
            if not item.get("ok"):
                continue
            snapshot_id = _insert_model_snapshot(
                conn,
                candidate=candidate,
                run_date=str(item["run_date"]),
                payload=item["payload"],
                meta=item["meta"],
                created_at=str(item["model_created_at"]),
            )
            inserted_snapshot_ids.append(snapshot_id)
            item["model_snapshot_id"] = snapshot_id
        fact_reports = _materialize_candidate_facts(conn, CANDIDATES, apply=apply)
        hit_rate_report: Dict[str, Any] = {}
        if bool(args.refresh_hit_rate):
            hit_rate_report = ui_server.materialize_source_lead_day_hit_rates(conn, days=0, scope="chengdu_zuuu")
        if apply:
            conn.commit()
    finally:
        conn.close()
        if temp_dir is not None:
            temp_dir.cleanup()
    compact_prepared = []
    for item in prepared:
        compact = {key: value for key, value in item.items() if key not in {"payload", "meta"}}
        compact_prepared.append(compact)
    fact_summary = {
        "would_insert": sum(int(((report.get("stats") or {}).get("would_insert") or 0)) for report in fact_reports),
        "inserted": sum(int(((report.get("stats") or {}).get("inserted") or 0)) for report in fact_reports),
        "existing_usable": sum(int(((report.get("stats") or {}).get("existing_usable") or 0)) for report in fact_reports),
        "reports": fact_reports,
    }
    return {
        "ok": True,
        "applied": apply,
        "db_path": str(db_path),
        "work_db": str(work_db) if not apply else str(db_path),
        "source": SOURCE,
        "source_url": SINGLE_RUNS_URL,
        "generated_at": _utc_now(),
        "confirm_phrase": CONFIRM_PHRASE,
        "candidate_count": len(CANDIDATES),
        "prepared_count": sum(1 for item in prepared if item.get("ok")),
        "inserted_model_snapshots": len(inserted_snapshot_ids) if apply else 0,
        "would_insert_model_snapshots": len(inserted_snapshot_ids) if not apply else 0,
        "model_snapshot_ids": inserted_snapshot_ids,
        "skipped_existing": skipped_existing,
        "candidates": compact_prepared,
        "fact_materialization": fact_summary,
        "hit_rate_refresh": hit_rate_report,
        "write_policy": (
            "model_snapshots_insert + history_only_event_fact_history_append + source_lead_day_hit_rates_refresh"
            if apply
            else "dry_run_temp_db_simulation_no_real_db_write"
        ),
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill Chengdu lead-day model_snapshots from Open-Meteo Single Runs")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--sync-window-sec", type=float, default=10800.0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-apply", default="")
    parser.add_argument("--refresh-hit-rate", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
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
            "candidate_count": report.get("candidate_count"),
            "prepared_count": report.get("prepared_count"),
            "would_insert_model_snapshots": report.get("would_insert_model_snapshots"),
            "inserted_model_snapshots": report.get("inserted_model_snapshots"),
            "fact_would_insert": (report.get("fact_materialization") or {}).get("would_insert"),
            "fact_inserted": (report.get("fact_materialization") or {}).get("inserted"),
            "hit_rate_eligible": ((report.get("hit_rate_refresh") or {}).get("coverage") or {}).get("eligible_events"),
            "hit_rate_settled": ((report.get("hit_rate_refresh") or {}).get("coverage") or {}).get("settled_events"),
            "out": str(out),
            "confirm_phrase": CONFIRM_PHRASE,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
