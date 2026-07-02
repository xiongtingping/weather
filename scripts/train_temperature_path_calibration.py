#!/usr/bin/env python3
"""Build a read-only candidate calibration for temperature path error bands."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_CALIBRATION_PATH = ROOT / "data" / "temperature_path_calibration.json"
DEFAULT_HOUR_BINS: Tuple[Tuple[float, float], ...] = (
    (0.0, 10.0),
    (10.0, 14.0),
    (14.0, 18.0),
    (18.0, 24.0),
)
DEFAULT_HOURS_TO_END_BINS: Tuple[Tuple[float, float], ...] = (
    (0.0, 3.0),
    (3.0, 8.0),
    (8.0, 16.0),
    (16.0, 48.0),
    (48.0, 9999.0),
)
DEFAULT_MIN_DEPLOYABLE_SIGMA_C = 0.35
PEAK_SOURCE_KEYS: Tuple[str, ...] = (
    "peak_estimator_mu_c",
    "forecast_peak_temp_c",
    "forecast_peak_temp",
    "settlement_source_forecast_peak_c",
    "wu_forecast_peak_c",
    "weather_com_forecast_peak_c",
    "twc_forecast_peak_c",
    "taf_forecast_peak_c",
    "open_meteo_peak_temp_c",
)


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        num = float(value)
    except Exception:
        return None
    return num if math.isfinite(num) else None


def _parse_json_object(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _payload_features(payload: Mapping[str, Any]) -> Dict[str, Any]:
    for key in ("weather_likelihood_features", "weather_likelihood_features_compact", "likelihood_features", "bayes_likelihood_features"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    distribution = payload.get("distribution")
    if isinstance(distribution, Sequence) and not isinstance(distribution, (str, bytes)):
        for row in distribution:
            if not isinstance(row, Mapping):
                continue
            for key in ("weather_likelihood_features", "weather_likelihood_features_compact", "likelihood_features", "bayes_likelihood_features"):
                value = row.get(key)
                if isinstance(value, Mapping):
                    return dict(value)
    return {}


def _first_peak_mu(features: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    for key in PEAK_SOURCE_KEYS:
        value = _to_float(features.get(key))
        if value is not None:
            source = "forecast_peak_temp_c" if key == "forecast_peak_temp" else key
            return value, source
    return None, ""


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _zone(name: Any) -> ZoneInfo:
    text = str(name or "").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(text)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _local_hour(created_at: datetime, tz: ZoneInfo) -> float:
    local = created_at.astimezone(tz)
    return local.hour + local.minute / 60.0 + local.second / 3600.0


def _hour_bin(local_hour: float, bins: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    for lo, hi in bins:
        if float(lo) <= local_hour < float(hi):
            return float(lo), float(hi)
    return float(bins[-1][0]), float(bins[-1][1])


def _pre_settlement_snapshot(row: Mapping[str, Any], created_at: datetime, tz: ZoneInfo) -> bool:
    end_at = _event_end_at(row, tz)
    if end_at is None:
        return True
    return created_at.astimezone(tz) <= end_at


def _event_end_at(row: Mapping[str, Any], tz: ZoneInfo) -> Optional[datetime]:
    event_date = str(row.get("event_date") or "").split("T")[0]
    if not event_date:
        return None
    try:
        event_day = datetime.fromisoformat(event_date).date()
    except Exception:
        return None
    return datetime.combine(event_day, time(23, 59, 59), tzinfo=tz)


def _hours_to_event_end(row: Mapping[str, Any], created_at: datetime, tz: ZoneInfo) -> Optional[float]:
    end_at = _event_end_at(row, tz)
    if end_at is None:
        return None
    return (end_at - created_at.astimezone(tz)).total_seconds() / 3600.0


def iter_training_samples(
    db_path: Path,
    *,
    city: str = "",
    station: str = "",
    include_post_event_snapshots: bool = False,
) -> Iterable[Dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    filters = ["settlement_actual IS NOT NULL", "payload_json IS NOT NULL"]
    params: List[Any] = []
    if city:
        filters.append("LOWER(COALESCE(city, '')) = LOWER(?)")
        params.append(city)
    if station:
        filters.append("UPPER(COALESCE(station_id, '')) = UPPER(?)")
        params.append(station)
    sql = f"""
        SELECT id, city, station_id, event_date, tz, settlement_actual, payload_json, created_at
        FROM event_runs
        WHERE {' AND '.join(filters)}
        ORDER BY created_at ASC, id ASC
    """
    try:
        for raw_row in conn.execute(sql, params):
            row = dict(raw_row)
            created_at = _parse_dt(row.get("created_at"))
            if created_at is None:
                continue
            tz = _zone(row.get("tz"))
            if not include_post_event_snapshots and not _pre_settlement_snapshot(row, created_at, tz):
                continue
            payload = _parse_json_object(row.get("payload_json"))
            features = _payload_features(payload)
            peak_mu, peak_source = _first_peak_mu(features)
            actual = _to_float(row.get("settlement_actual"))
            if peak_mu is None or actual is None:
                continue
            hour = _local_hour(created_at, tz)
            hours_to_end = _hours_to_event_end(row, created_at, tz)
            yield {
                "id": row.get("id"),
                "city": str(row.get("city") or ""),
                "station_id": str(row.get("station_id") or features.get("station_id") or "").strip(),
                "event_date": str(row.get("event_date") or ""),
                "created_at": row.get("created_at"),
                "local_hour": hour,
                "hours_to_event_end": hours_to_end,
                "peak_mu_source": peak_source,
                "predicted_peak_mu_c": peak_mu,
                "settlement_actual_c": actual,
                "residual_c": actual - peak_mu,
            }
    finally:
        conn.close()


def _stats(samples: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    residuals = [float(item["residual_c"]) for item in samples]
    abs_errors = [abs(value) for value in residuals]
    sq_errors = [value * value for value in residuals]
    bias = mean(residuals)
    variance = mean([(value - bias) ** 2 for value in residuals]) if residuals else 0.0
    rmse = math.sqrt(mean(sq_errors)) if sq_errors else 0.0
    return {
        "sample_n": len(samples),
        "mae_c": round(mean(abs_errors), 3) if abs_errors else None,
        "rmse_c": round(rmse, 3),
        "bias_c": round(bias, 3),
        "residual_std_c": round(math.sqrt(max(0.0, variance)), 3),
        "sigma_c": round(max(0.05, rmse), 3),
    }


def _bin_value(value: Optional[float], bins: Sequence[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    if value is None or not math.isfinite(float(value)):
        return None
    for lo, hi in bins:
        if float(lo) <= float(value) < float(hi):
            return float(lo), float(hi)
    return float(bins[-1][0]), float(bins[-1][1])


def _mark_quality(row: Dict[str, Any], min_sample_n: int, min_deployable_sigma_c: float) -> Dict[str, Any]:
    sigma = _to_float(row.get("sigma_c"))
    sample_n = int(row.get("sample_n") or 0)
    row["usable"] = bool(sample_n >= min_sample_n)
    row["deployable_candidate"] = bool(
        row["usable"]
        and sigma is not None
        and sigma >= float(min_deployable_sigma_c)
    )
    if row["usable"] and not row["deployable_candidate"]:
        row["deploy_block_reason"] = "sigma_below_conservative_floor"
    elif not row["usable"]:
        row["deploy_block_reason"] = "sample_n_below_minimum"
    else:
        row["deploy_block_reason"] = ""
    return row


def _stats_with_quality(
    samples: Sequence[Mapping[str, Any]],
    min_sample_n: int,
    min_deployable_sigma_c: float,
) -> Dict[str, Any]:
    out = _stats(samples)
    return _mark_quality(out, min_sample_n, min_deployable_sigma_c)


def _hours_to_end_buckets(
    samples: Sequence[Mapping[str, Any]],
    *,
    min_sample_n: int,
    bins: Sequence[Tuple[float, float]],
    min_deployable_sigma_c: float,
) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[float, float], List[Mapping[str, Any]]] = defaultdict(list)
    missing = 0
    for sample in samples:
        bucket = _bin_value(_to_float(sample.get("hours_to_event_end")), bins)
        if bucket is None:
            missing += 1
            continue
        grouped[bucket].append(sample)
    rows: List[Dict[str, Any]] = []
    for lo, hi in bins:
        group = grouped.get((float(lo), float(hi)), [])
        stats = _stats_with_quality(group, min_sample_n, min_deployable_sigma_c) if group else {
            "sample_n": 0,
            "mae_c": None,
            "rmse_c": None,
            "bias_c": None,
            "residual_std_c": None,
            "sigma_c": None,
            "usable": False,
            "deployable_candidate": False,
            "deploy_block_reason": "sample_n_below_minimum",
        }
        row = {
            "hours_to_event_end_min": float(lo),
            "hours_to_event_end_max": float(hi),
            **stats,
        }
        rows.append(row)
    if missing:
        rows.append(
            {
                "hours_to_event_end_min": None,
                "hours_to_event_end_max": None,
                "sample_n": missing,
                "mae_c": None,
                "rmse_c": None,
                "bias_c": None,
                "residual_std_c": None,
                "sigma_c": None,
                "usable": False,
                "deployable_candidate": False,
                "deploy_block_reason": "sample_n_below_minimum",
                "na_reason": "hours_to_event_end_missing",
            }
        )
    return rows


def _source_hours_to_end_buckets(
    samples: Sequence[Mapping[str, Any]],
    *,
    min_sample_n: int,
    bins: Sequence[Tuple[float, float]],
    min_deployable_sigma_c: float,
) -> Dict[str, List[Dict[str, Any]]]:
    by_source: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for sample in samples:
        source = str(sample.get("peak_mu_source") or "").strip()
        if source:
            by_source[source].append(sample)
    return {
        source: _hours_to_end_buckets(
            group,
            min_sample_n=min_sample_n,
            bins=bins,
            min_deployable_sigma_c=min_deployable_sigma_c,
        )
        for source, group in sorted(by_source.items())
    }


def _coverage_report(
    samples: Sequence[Mapping[str, Any]],
    *,
    min_sample_n: int,
    hours_to_end_bins: Sequence[Tuple[float, float]],
    min_deployable_sigma_c: float,
) -> Dict[str, Any]:
    hours = [_to_float(sample.get("hours_to_event_end")) for sample in samples]
    known_hours = sorted(float(value) for value in hours if value is not None)
    near_close = [sample for sample in samples if (_to_float(sample.get("hours_to_event_end")) or 999999.0) < 3.0]
    early = [sample for sample in samples if (_to_float(sample.get("hours_to_event_end")) or -1.0) >= 8.0]
    return {
        "sample_n": len(samples),
        "hours_to_event_end_min": round(known_hours[0], 3) if known_hours else None,
        "hours_to_event_end_max": round(known_hours[-1], 3) if known_hours else None,
        "near_close_sample_n": len(near_close),
        "near_close_share": round(len(near_close) / len(samples), 4) if samples else 0.0,
        "early_sample_n": len(early),
        "early_share": round(len(early) / len(samples), 4) if samples else 0.0,
        "near_close_warning": bool(samples and len(near_close) / len(samples) > 0.35),
        "hours_to_event_end_buckets": _hours_to_end_buckets(
            samples,
            min_sample_n=min_sample_n,
            bins=hours_to_end_bins,
            min_deployable_sigma_c=min_deployable_sigma_c,
        ),
        "by_peak_mu_source_hours_to_event_end": _source_hours_to_end_buckets(
            samples,
            min_sample_n=min_sample_n,
            bins=hours_to_end_bins,
            min_deployable_sigma_c=min_deployable_sigma_c,
        ),
    }


def build_calibration(
    samples: Sequence[Mapping[str, Any]],
    *,
    min_sample_n: int = 20,
    hour_bins: Sequence[Tuple[float, float]] = DEFAULT_HOUR_BINS,
    hours_to_end_bins: Sequence[Tuple[float, float]] = DEFAULT_HOURS_TO_END_BINS,
    min_deployable_sigma_c: float = DEFAULT_MIN_DEPLOYABLE_SIGMA_C,
) -> Dict[str, Any]:
    by_bucket: Dict[Tuple[str, str, float, float], List[Mapping[str, Any]]] = defaultdict(list)
    by_source: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for sample in samples:
        station = str(sample.get("station_id") or "").strip()
        source = str(sample.get("peak_mu_source") or "").strip()
        if not source:
            continue
        lo, hi = _hour_bin(float(sample.get("local_hour") or 0.0), hour_bins)
        by_bucket[(station, source, lo, hi)].append(sample)
        by_source[source].append(sample)

    buckets: List[Dict[str, Any]] = []
    for (station, source, lo, hi), group in sorted(by_bucket.items(), key=lambda item: (item[0][0], item[0][1], item[0][2])):
        row = {
            "station_id": station,
            "peak_mu_source": source,
            "local_hour_min": lo,
            "local_hour_max": hi,
            **_stats(group),
        }
        _mark_quality(row, min_sample_n, min_deployable_sigma_c)
        buckets.append(row)

    by_peak_mu_source = {
        source: _stats_with_quality(group, min_sample_n, min_deployable_sigma_c)
        for source, group in sorted(by_source.items())
    }
    default = _stats(samples) if samples else {
        "sample_n": 0,
        "mae_c": None,
        "rmse_c": None,
        "bias_c": None,
        "residual_std_c": None,
        "sigma_c": 1.25,
    }
    _mark_quality(default, min_sample_n, min_deployable_sigma_c)
    return {
        "version": "temperature_path_error_calibration_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "scripts/train_temperature_path_calibration.py",
        "mode": "candidate_read_only",
        "min_sample_n": int(min_sample_n),
        "min_deployable_sigma_c": float(min_deployable_sigma_c),
        "default_sigma_c": float(default.get("sigma_c") or 1.25),
        "sample_n": len(samples),
        "default": default,
        "by_peak_mu_source": by_peak_mu_source,
        "buckets": buckets,
        "coverage_report": _coverage_report(
            samples,
            min_sample_n=min_sample_n,
            hours_to_end_bins=hours_to_end_bins,
            min_deployable_sigma_c=min_deployable_sigma_c,
        ),
        "semantics": {
            "residual_c": "settlement_actual_c - predicted_peak_mu_c",
            "sigma_c": "rmse_c used as conservative path error band",
            "snapshot_filter": "default excludes snapshots after local event_date end to avoid settlement leakage",
            "hours_to_event_end": "local event_date 23:59:59 minus snapshot created_at; low values may be near-close nowcast-like samples",
        },
    }


def parse_hour_bins(text: str) -> Tuple[Tuple[float, float], ...]:
    if not text.strip():
        return DEFAULT_HOUR_BINS
    bins: List[Tuple[float, float]] = []
    for part in text.split(","):
        lo_text, hi_text = part.split("-", 1)
        lo = float(lo_text)
        hi = float(hi_text)
        if hi <= lo:
            raise argparse.ArgumentTypeError(f"invalid hour bin: {part}")
        bins.append((lo, hi))
    return tuple(bins)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--city", default="")
    parser.add_argument("--station", default="")
    parser.add_argument("--min-sample-n", type=int, default=20)
    parser.add_argument("--min-deployable-sigma-c", type=float, default=DEFAULT_MIN_DEPLOYABLE_SIGMA_C)
    parser.add_argument("--hour-bins", default="0-10,10-14,14-18,18-24")
    parser.add_argument("--hours-to-end-bins", default="0-3,3-8,8-16,16-48,48-9999")
    parser.add_argument("--include-post-event-snapshots", action="store_true")
    parser.add_argument("--output", default="", help="Optional candidate JSON output path. Defaults to stdout.")
    parser.add_argument("--allow-official-write", action="store_true", help="Required to write data/temperature_path_calibration.json.")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    hour_bins = parse_hour_bins(args.hour_bins)
    hours_to_end_bins = parse_hour_bins(args.hours_to_end_bins)
    output_path = Path(args.output) if args.output else None
    if output_path and output_path.resolve() == OFFICIAL_CALIBRATION_PATH.resolve() and not args.allow_official_write:
        print(
            "refusing to write official data/temperature_path_calibration.json without --allow-official-write",
            file=sys.stderr,
        )
        return 2
    samples = list(
        iter_training_samples(
            db_path,
            city=args.city,
            station=args.station,
            include_post_event_snapshots=bool(args.include_post_event_snapshots),
        )
    )
    calibration = build_calibration(
        samples,
        min_sample_n=args.min_sample_n,
        hour_bins=hour_bins,
        hours_to_end_bins=hours_to_end_bins,
        min_deployable_sigma_c=args.min_deployable_sigma_c,
    )
    text = json.dumps(calibration, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output_path:
        out_path = output_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote candidate calibration: {out_path} (sample_n={calibration['sample_n']})")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
