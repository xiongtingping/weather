#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scan_weather_market as swm  # noqa: E402
from bayesian_likelihood_runtime import estimate_peak_temperature_from_features  # noqa: E402
from bayesian_probability_runtime import apply_hard_source_posterior_override_to_distribution  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_LOG = ROOT / "data" / "logs" / "hard_source_direct_poller.log"
DEFAULT_AWC_SOURCE_HEALTH = ROOT / "data" / "awc_source_health_latest.json"
DEFAULT_POSTERIOR_TARGET_LATENCY_LATEST = ROOT / "data" / "posterior_target_latency_latest.json"
DEFAULT_POSTERIOR_TARGET_LATENCY_EVENTS = ROOT / "data" / "posterior_target_latency_events.jsonl"
CHENGDU_SLUG_PREFIX = "highest-temperature-in-chengdu-"
DIRECT_SOURCE = "hard_source_direct"
POSTERIOR_TARGET_SOURCE = "posterior_target_direct"
TARGET_REALIZATION_STRATEGY = "target_realization_binary_settlement"
CHENGDU_TWC_LAT = 30.5728
CHENGDU_TWC_LON = 104.0668
CODE_RELOAD_WATCH_PATHS = (
    Path(__file__).resolve(),
    ROOT / ".env",
    ROOT / "scan_weather_market.py",
    ROOT / "ui" / "server.py",
)


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        val = float(str(raw if raw is not None else default).strip())
    except Exception:
        val = float(default)
    if not math.isfinite(val):
        val = float(default)
    return float(val)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(float(str(raw if raw is not None else default).strip()))
    except Exception:
        return int(default)


def _env_int_first(names: Iterable[str], default: int) -> int:
    for name in names:
        raw = os.environ.get(str(name))
        if raw in (None, ""):
            continue
        try:
            return int(float(str(raw).strip()))
        except Exception:
            continue
    return int(default)


def _background_wu_twc_refresh_enabled() -> bool:
    # Keep the hard-source/AWC path uncontended unless WU/TWC background refresh is explicitly enabled.
    return _truthy(os.environ.get("PM_HARD_SOURCE_WU_TWC_BACKGROUND_REFRESH"), False)


def _metar_pws_blocking_enabled() -> bool:
    # METAR/PWS cross-checks can be slow; keep them out of the order fast path unless explicitly enabled.
    return _truthy(os.environ.get("PM_HARD_SOURCE_METAR_PWS_BLOCKING"), False)


def _background_metar_pws_refresh_enabled() -> bool:
    # Background METAR/PWS still writes archive.db; keep it opt-in so AWC->order stays uncontended.
    return _truthy(os.environ.get("PM_HARD_SOURCE_METAR_PWS_BACKGROUND_REFRESH"), False)


def _inline_wu_twc_refresh_after_decision_enabled() -> bool:
    # WU/TWC fallback may run before decisions when no hard observation exists; after decisions it is opt-in.
    return _truthy(os.environ.get("PM_HARD_SOURCE_WU_TWC_INLINE_REFRESH"), False)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_date(value: Any) -> str:
    try:
        return swm._normalize_date_key(str(value or "").strip())
    except Exception:
        text = str(value or "").strip()
        return text.split("T", 1)[0].split(" ", 1)[0]


def _chengdu_today() -> str:
    if ZoneInfo is not None:
        try:
            return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).date().isoformat()


def _run_date_et() -> str:
    try:
        return swm.today_date_str("America/New_York")
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def _local_hour_now() -> float:
    override = os.environ.get("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_HOUR_OVERRIDE")
    if override not in (None, ""):
        try:
            val = float(str(override).strip())
            if math.isfinite(val):
                return float(val)
        except Exception:
            pass
    now = datetime.now(timezone.utc)
    if ZoneInfo is not None:
        try:
            now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        except Exception:
            pass
    return float(now.hour) + float(now.minute) / 60.0 + float(now.second) / 3600.0


def _local_date_now() -> str:
    override = os.environ.get("PM_HARD_SOURCE_DIRECT_DECISION_LOCAL_DATE_OVERRIDE")
    if override not in (None, ""):
        event_key = _normalize_date(override)
        if event_key:
            return event_key
    now = datetime.now(timezone.utc)
    if ZoneInfo is not None:
        try:
            now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        except Exception:
            pass
    return now.date().isoformat()


def _payload_features(payload: Mapping[str, Any]) -> Dict[str, Any]:
    raw = payload.get("weather_likelihood_features")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _parse_iso_dt(value: Any, *, default_tz: timezone = timezone.utc) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt


def _latency_ms_between(start: Any, end: Any) -> Optional[float]:
    start_dt = _parse_iso_dt(start)
    end_dt = _parse_iso_dt(end)
    if start_dt is None or end_dt is None:
        return None
    try:
        return round(max(0.0, (end_dt - start_dt).total_seconds()) * 1000.0, 3)
    except Exception:
        return None


def _iso_not_before(value: Any, *floor_values: Any) -> str:
    best_text = str(value or "").strip()
    best_dt = _parse_iso_dt(best_text)
    for floor in floor_values:
        floor_text = str(floor or "").strip()
        if not floor_text:
            continue
        floor_dt = _parse_iso_dt(floor_text)
        if floor_dt is None:
            continue
        if best_dt is None or floor_dt > best_dt:
            best_dt = floor_dt
            best_text = floor_text
    return best_text or _utc_now_iso()


def _event_zone(payload: Mapping[str, Any]) -> timezone:
    tz_name = str(payload.get("event_tz") or payload.get("tz") or "Asia/Shanghai").strip() or "Asia/Shanghai"
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)  # type: ignore[return-value]
        except Exception:
            pass
    return timezone.utc


def _forecast_geocode(payload: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    lat = _to_float(payload.get("lat") or payload.get("latitude"))
    lon = _to_float(payload.get("lon") or payload.get("longitude"))
    if lat is not None and lon is not None:
        return float(lat), float(lon)
    station = str(payload.get("station_id") or "").strip().upper()
    text = " ".join(
        [
            station,
            str(payload.get("slug") or ""),
            str(payload.get("title") or ""),
            str(payload.get("resolution_source") or ""),
        ]
    ).lower()
    if station == "ZUUU" or "chengdu" in text or "/cn/chengdu/" in text:
        return CHENGDU_TWC_LAT, CHENGDU_TWC_LON
    return None, None


def _copy_peak_plateau_features(
    source_features: Dict[str, Any],
    *,
    prefix: str,
    metrics: Mapping[str, Any],
    canonical: bool = False,
    reference_peak_c: Optional[float] = None,
) -> None:
    raw_distribution = metrics.get("expected_peak_hour_distribution") or metrics.get("peak_hour_distribution")
    if not isinstance(raw_distribution, list) or not raw_distribution:
        return
    distribution: List[Dict[str, Any]] = []
    for row in raw_distribution:
        if not isinstance(row, Mapping):
            continue
        hour = _to_float(row.get("hour_local") or row.get("hour") or row.get("peak_hour"))
        prob = _to_float(row.get("prob") or row.get("probability") or row.get("weight"))
        if hour is None or prob is None or float(prob) <= 0:
            continue
        item: Dict[str, Any] = {
            "hour_local": max(0.0, min(23.99, float(hour))),
            "prob": max(0.0, float(prob)),
        }
        temp_c = _to_float(row.get("temperature_c"))
        if temp_c is not None:
            item["temperature_c"] = float(temp_c)
        if row.get("valid_time_local"):
            item["valid_time_local"] = str(row.get("valid_time_local") or "")
        distribution.append(item)
    total = sum(float(row["prob"]) for row in distribution)
    if not distribution or total <= 0:
        return
    plateau_max_c = _to_float(metrics.get("peak_plateau_max_temp_c"))
    if plateau_max_c is None and isinstance(metrics.get("peak_plateau"), Mapping):
        plateau_max_c = _to_float((metrics.get("peak_plateau") or {}).get("max_temp_c"))
    if reference_peak_c is not None and plateau_max_c is not None:
        max_gap = _env_float("PM_WU_TWC_PEAK_PLATEAU_MAX_DAILY_GAP_C", 1.0)
        if abs(float(reference_peak_c) - float(plateau_max_c)) > float(max_gap):
            if canonical and not source_features.get("expected_peak_hour_distribution"):
                _clear_canonical_peak_plateau_features(
                    source_features,
                    reason="hourly_plateau_max_far_from_daily_peak",
                )
            source_features[f"{prefix}_peak_hour_distribution_na_reason"] = "hourly_plateau_max_far_from_daily_peak"
            source_features[f"{prefix}_peak_plateau_daily_peak_c"] = float(reference_peak_c)
            source_features[f"{prefix}_peak_plateau_daily_gap_c"] = abs(float(reference_peak_c) - float(plateau_max_c))
            return
    for row in distribution:
        row["prob"] = float(row["prob"]) / float(total)
    distribution.sort(key=lambda row: float(row["hour_local"]))

    center = _to_float(metrics.get("expected_peak_hour_local"))
    if center is None:
        center = sum(float(row["hour_local"]) * float(row["prob"]) for row in distribution)
    start = _to_float(metrics.get("expected_peak_window_start_hour_local"))
    end = _to_float(metrics.get("expected_peak_window_end_hour_local"))
    if start is None:
        start = min(float(row["hour_local"]) for row in distribution)
    if end is None:
        end = max(float(row["hour_local"]) for row in distribution)
    source = str(metrics.get("source") or prefix).strip() or prefix

    source_features[f"{prefix}_peak_hour_distribution"] = distribution
    source_features[f"{prefix}_peak_hour_distribution_source"] = source
    source_features[f"{prefix}_peak_hour_distribution_basis"] = "hourly_near_peak_plateau"
    source_features[f"{prefix}_peak_window_start_hour_local"] = float(start)
    source_features[f"{prefix}_peak_window_end_hour_local"] = float(end)
    source_features[f"{prefix}_expected_peak_hour_local"] = float(center)
    if metrics.get("expected_peak_at_local"):
        source_features[f"{prefix}_expected_peak_at_local"] = str(metrics.get("expected_peak_at_local") or "")
    for key in ("peak_plateau_threshold_c", "peak_plateau_max_temp_c", "peak_plateau_count"):
        value = metrics.get(key)
        if value not in (None, ""):
            source_features[f"{prefix}_{key}"] = value
    plateau = metrics.get("peak_plateau")
    if isinstance(plateau, Mapping):
        source_features[f"{prefix}_peak_plateau"] = dict(plateau)

    if not canonical:
        return
    source_features["expected_peak_hour_distribution"] = distribution
    source_features["peak_hour_distribution"] = distribution
    source_features["expected_peak_hour_local"] = float(center)
    source_features["expected_peak_hour_source"] = f"{source}:hourly_near_peak_plateau"
    source_features["expected_peak_at_local"] = str(metrics.get("expected_peak_at_local") or "")
    source_features["expected_peak_window_start_hour_local"] = float(start)
    source_features["expected_peak_window_end_hour_local"] = float(end)
    source_features["expected_peak_window_source"] = f"{source}:hourly_near_peak_plateau"


def _clear_canonical_peak_plateau_features(source_features: Dict[str, Any], *, reason: str) -> None:
    source_features["_clear_expected_peak_hour_distribution"] = True
    source_features["expected_peak_hour_distribution"] = []
    source_features["peak_hour_distribution"] = []
    source_features["expected_peak_hour_distribution_na_reason"] = str(reason or "peak_plateau_unavailable")


def _peak_hour_distribution_meta_from_features(
    features: Mapping[str, Any],
    *,
    event_date: str,
    zone: timezone,
) -> Tuple[Optional[float], Dict[str, Any]]:
    raw = features.get("expected_peak_hour_distribution") or features.get("peak_hour_distribution")
    items: List[Tuple[float, float]] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            hour = _to_float(key)
            prob = _to_float(value)
            if hour is not None and prob is not None and float(prob) > 0:
                items.append((max(0.0, min(23.99, float(hour))), float(prob)))
    elif isinstance(raw, list):
        for row in raw:
            if not isinstance(row, Mapping):
                continue
            valid_local = str(row.get("valid_time_local") or row.get("expected_peak_at_local") or "").strip()
            if event_date and valid_local:
                dt = _parse_iso_dt(valid_local, default_tz=zone)
                if dt is not None and dt.astimezone(zone).date().isoformat() != event_date:
                    continue
            hour = _to_float(row.get("hour_local") or row.get("hour") or row.get("peak_hour"))
            prob = _to_float(row.get("prob") or row.get("probability") or row.get("weight"))
            if hour is not None and prob is not None and float(prob) > 0:
                items.append((max(0.0, min(23.99, float(hour))), float(prob)))
    total = sum(prob for _, prob in items)
    if not items or total <= 0:
        return None, {}
    center = sum(hour * prob for hour, prob in items) / total
    start = _to_float(features.get("expected_peak_window_start_hour_local") or features.get("peak_window_start_hour_local"))
    end = _to_float(features.get("expected_peak_window_end_hour_local") or features.get("peak_window_end_hour_local"))
    if start is None:
        start = min(hour for hour, _ in items)
    if end is None:
        end = max(hour for hour, _ in items)
    return max(0.0, min(23.99, float(center))), {
        "expected_peak_hour_source": str(features.get("expected_peak_hour_source") or "expected_peak_hour_distribution"),
        "expected_peak_hour_local": float(center),
        "expected_peak_at_local": str(features.get("expected_peak_at_local") or ""),
        "expected_peak_window_start_hour_local": float(start),
        "expected_peak_window_end_hour_local": float(end),
        "expected_peak_window_source": str(features.get("expected_peak_window_source") or "expected_peak_hour_distribution"),
        "expected_peak_hour_distribution": [
            {"hour_local": float(hour), "prob": float(prob) / total}
            for hour, prob in items
        ],
    }


def _forecast_peak_hour_from_features(
    features: Mapping[str, Any],
    *,
    event_date: str,
    zone: timezone,
) -> Tuple[Optional[float], Dict[str, Any]]:
    dist_hour, dist_meta = _peak_hour_distribution_meta_from_features(
        features,
        event_date=event_date,
        zone=zone,
    )
    if dist_hour is not None:
        return dist_hour, dist_meta
    for key in (
        "wu_forecast_peak_at",
        "wu_forecast_peak_valid_local",
        "weather_com_forecast_peak_at",
        "twc_forecast_peak_at",
    ):
        dt = _parse_iso_dt(features.get(key), default_tz=zone)
        if dt is None:
            continue
        local_dt = dt.astimezone(zone)
        if event_date and local_dt.date().isoformat() != event_date:
            continue
        hour = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0
        return max(0.0, min(23.99, float(hour))), {
            "expected_peak_hour_local": float(hour),
            "expected_peak_hour_source": key,
            "expected_peak_at_local": local_dt.isoformat(),
        }
    return None, {"expected_peak_hour_source": "wu_forecast_peak_time_missing"}


def _expected_peak_hour_local(
    payload: Mapping[str, Any],
    *,
    event_date: str,
) -> Tuple[Optional[float], Dict[str, Any]]:
    features = _payload_features(payload)
    event_key = _normalize_date(event_date)
    forecast_hour, forecast_meta = _forecast_peak_hour_from_features(
        features,
        event_date=event_key,
        zone=_event_zone(payload),
    )
    if forecast_hour is not None:
        return float(forecast_hour), forecast_meta
    return None, forecast_meta


def _decision_window_state(
    payload: Optional[Mapping[str, Any]] = None,
    *,
    event_date: str = "",
) -> Dict[str, Any]:
    if not _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED"), True):
        return {"allowed": True, "disabled": True}
    mode = str(os.environ.get("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_MODE") or "wu_forecast").strip().lower()
    event_key = _normalize_date(event_date)
    local_date = _local_date_now()
    hour = _local_hour_now()
    peak_hour = None
    peak_meta: Dict[str, Any] = {}
    if mode != "fixed":
        peak_hour, peak_meta = _expected_peak_hour_local(payload or {}, event_date=event_key)
        if peak_hour is None:
            return {
                "allowed": False,
                "disabled": False,
                "mode": mode or "wu_forecast",
                "reason": "wu_forecast_peak_time_missing",
                "local_hour": round(float(hour), 4),
                "local_date": local_date,
                "event_date": event_key,
                "start_local_hour": None,
                "end_local_hour": None,
                "peak_hour_local": None,
                "peak_hour_source": str(peak_meta.get("expected_peak_hour_source") or "wu_forecast_peak_time_missing"),
                "peak_at_local": "",
                "lead_hours": _env_float("PM_HARD_SOURCE_DIRECT_PEAK_LEAD_HOURS", 1.0),
                "post_hours": _env_float("PM_HARD_SOURCE_DIRECT_PEAK_POST_HOURS", 1.5),
                "timezone": "Asia/Shanghai",
            }
        lead_hours = max(0.0, _env_float("PM_HARD_SOURCE_DIRECT_PEAK_LEAD_HOURS", 1.0))
        post_hours = max(0.0, _env_float("PM_HARD_SOURCE_DIRECT_PEAK_POST_HOURS", 1.5))
        peak_window_start = _to_float(peak_meta.get("expected_peak_window_start_hour_local"))
        peak_window_end = _to_float(peak_meta.get("expected_peak_window_end_hour_local"))
        if peak_window_start is not None and peak_window_end is not None:
            start = max(0.0, float(peak_window_start) - float(lead_hours))
            end = min(23.99, float(peak_window_end) + float(post_hours))
        else:
            peak_window_start = None
            peak_window_end = None
            start = max(0.0, float(peak_hour) - float(lead_hours))
            end = min(23.99, float(peak_hour) + float(post_hours))
    else:
        start = _env_float("PM_HARD_SOURCE_DIRECT_DECISION_START_LOCAL_HOUR", 14.0)
        end = _env_float("PM_HARD_SOURCE_DIRECT_DECISION_END_LOCAL_HOUR", 17.5)
        peak_window_start = None
        peak_window_end = None
    if start <= end:
        allowed = start <= hour <= end
    else:
        allowed = hour >= start or hour <= end
    if event_key and local_date != event_key:
        allowed = False
    return {
        "allowed": bool(allowed),
        "disabled": False,
        "mode": mode or "wu_forecast",
        "reason": "inside_peak_window" if allowed else "outside_peak_window",
        "local_hour": round(float(hour), 4),
        "local_date": local_date,
        "event_date": event_key,
        "start_local_hour": float(start),
        "end_local_hour": float(end),
        "peak_hour_local": float(peak_hour) if peak_hour is not None else None,
        "peak_hour_source": str(peak_meta.get("expected_peak_hour_source") or ""),
        "peak_at_local": str(peak_meta.get("expected_peak_at_local") or ""),
        "peak_window_start_hour_local": float(peak_window_start) if peak_window_start is not None else None,
        "peak_window_end_hour_local": float(peak_window_end) if peak_window_end is not None else None,
        "peak_window_source": str(peak_meta.get("expected_peak_window_source") or ""),
        "lead_hours": _env_float("PM_HARD_SOURCE_DIRECT_PEAK_LEAD_HOURS", 1.0) if mode != "fixed" else None,
        "post_hours": _env_float("PM_HARD_SOURCE_DIRECT_PEAK_POST_HOURS", 1.5) if mode != "fixed" else None,
        "timezone": "Asia/Shanghai",
    }


def _to_float(value: Any) -> Optional[float]:
    try:
        val = swm.to_float(value)
    except Exception:
        val = None
    if val is None:
        try:
            val = float(value)
        except Exception:
            return None
    if not math.isfinite(float(val)):
        return None
    return float(val)


@contextmanager
def _connect(db_path: Path | str) -> Iterable[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path), timeout=float(_env_float("PM_HARD_SOURCE_DB_TIMEOUT_SEC", 2.0)))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={max(250, _env_int('PM_HARD_SOURCE_DB_BUSY_TIMEOUT_MS', 1500))}")
        yield conn
    finally:
        conn.close()


@contextmanager
def _temporary_env(overrides: Mapping[str, Optional[str]]) -> Iterable[None]:
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _active_pid(pid_text: Any) -> bool:
    try:
        pid = int(str(pid_text or "").strip())
    except Exception:
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _process_command(pid: int) -> str:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=0.3,
        )
        return out.strip()
    except Exception:
        return ""


def _child_pids(pid: int) -> List[int]:
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=0.3,
        )
    except Exception:
        return []
    children: List[int] = []
    for line in out.splitlines():
        try:
            child = int(line.strip())
        except Exception:
            continue
        if child > 1:
            children.append(child)
    return children


def _kill_tree(pid: int, sig: signal.Signals) -> None:
    for child in _child_pids(pid):
        _kill_tree(child, sig)
        try:
            os.kill(child, sig)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except Exception:
        pass


def _wait_pid_exit(pid: int, wait_sec: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(wait_sec))
    while time.monotonic() < deadline:
        if not _active_pid(pid):
            return True
        time.sleep(0.05)
    return not _active_pid(pid)


def _remove_lock_dir_for_pid(lock_dir: Path, pid: int) -> bool:
    pid_path = lock_dir / "pid"
    try:
        lock_pid = int(_read_text(pid_path))
    except Exception:
        return False
    if lock_pid != pid or _active_pid(lock_pid):
        return False
    try:
        for child in lock_dir.iterdir():
            child.unlink()
        lock_dir.rmdir()
        return True
    except Exception:
        return False


def _append_refresh_preempt_log(payload: Mapping[str, Any]) -> None:
    try:
        DEFAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEFAULT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{datetime.now(timezone.utc).isoformat()} "
                f"{json.dumps(dict(payload), ensure_ascii=True, sort_keys=True)}\n"
            )
    except Exception:
        pass


def _preempt_noncritical_refreshes_for_live(*, event_date: str = "", stage: str = "") -> Dict[str, Any]:
    if not _truthy(os.environ.get("PM_HARD_SOURCE_PREEMPT_NONCRITICAL_REFRESH_ENABLED"), True):
        return {"enabled": False}
    lane_name = str(os.environ.get("PM_REFRESH_COORDINATOR_LANE_NAME") or "weather_refresh_lane").strip()
    lane_name = "".join(ch for ch in lane_name if ch.isalnum() or ch in {"_", ".", "-"})
    if not lane_name:
        lane_name = "weather_refresh_lane"
    lock_dirs = [
        ("refresh_lane", ROOT / "data" / f"{lane_name}.lock"),
        ("db_writer", ROOT / "data" / "db_writer.lock"),
    ]
    raw_owners = os.environ.get(
        "PM_HARD_SOURCE_PREEMPT_NONCRITICAL_OWNERS",
        "model_scan,model_scan_hourly,market_refresh,market_refresh_frequent,"
        "market_refresh_urgent,weather_discovery,model_release_watcher,daily_scan,weatherctl_facts",
    )
    preemptible = {item.strip() for item in raw_owners.split(",") if item.strip()}
    kill_after_sec = max(0.0, _env_float("PM_HARD_SOURCE_PREEMPT_NONCRITICAL_KILL_AFTER_SEC", 1.0))
    term_wait_sec = max(0.05, _env_float("PM_HARD_SOURCE_PREEMPT_NONCRITICAL_TERM_WAIT_SEC", 0.5))
    seen: set[int] = set()
    actions: List[Dict[str, Any]] = []

    for lock_kind, lock_dir in lock_dirs:
        if not lock_dir.is_dir():
            continue
        owner = _read_text(lock_dir / "owner") or "unknown"
        pid_text = _read_text(lock_dir / "pid")
        try:
            pid = int(pid_text)
        except Exception:
            continue
        if pid <= 1 or pid == os.getpid():
            continue
        if owner not in preemptible:
            actions.append(
                {
                    "lock": lock_kind,
                    "owner": owner,
                    "pid": pid,
                    "action": "not_preemptible",
                }
            )
            continue
        if pid in seen:
            continue
        seen.add(pid)
        if not _active_pid(pid):
            removed = _remove_lock_dir_for_pid(lock_dir, pid)
            actions.append(
                {
                    "lock": lock_kind,
                    "owner": owner,
                    "pid": pid,
                    "action": "stale_lock_removed" if removed else "stale_lock_seen",
                }
            )
            continue
        cmdline = _process_command(pid)
        if str(ROOT) not in cmdline:
            actions.append(
                {
                    "lock": lock_kind,
                    "owner": owner,
                    "pid": pid,
                    "action": "skipped_outside_project",
                }
            )
            continue
        started = time.monotonic()
        _kill_tree(pid, signal.SIGTERM)
        exited = _wait_pid_exit(pid, term_wait_sec)
        killed = False
        if not exited and kill_after_sec > 0:
            remaining = max(0.0, kill_after_sec - (time.monotonic() - started))
            if remaining > 0:
                exited = _wait_pid_exit(pid, remaining)
            if not exited:
                _kill_tree(pid, signal.SIGKILL)
                killed = True
                exited = _wait_pid_exit(pid, 0.3)
        removed_locks = []
        if exited:
            for _, cleanup_dir in lock_dirs + [
                ("model_scan", ROOT / "data" / "model_scan.lock"),
                ("market_refresh", ROOT / "data" / "market_refresh.lock"),
            ]:
                if _remove_lock_dir_for_pid(cleanup_dir, pid):
                    removed_locks.append(cleanup_dir.name)
        actions.append(
            {
                "lock": lock_kind,
                "owner": owner,
                "pid": pid,
                "action": "preempted",
                "killed": killed,
                "exited": exited,
                "removed_locks": removed_locks,
                "elapsed_ms": round(max(0.0, time.monotonic() - started) * 1000.0, 3),
            }
        )

    result = {
        "enabled": True,
        "stage": str(stage or ""),
        "event_date": _normalize_date(event_date) or _chengdu_today(),
        "actions": actions,
    }
    if actions:
        _append_refresh_preempt_log({"refresh_preempt": result})
    return result


@contextmanager
def _live_ssot_writer_lock(*, event_date: str = "", stage: str = "poll_once") -> Iterable[None]:
    if not _truthy(os.environ.get("PM_HARD_SOURCE_LIVE_SSOT_WRITER_LOCK_ENABLED"), True):
        yield
        return
    lock_dir = ROOT / "data" / "live_ssot_writer.lock"
    pid_path = lock_dir / "pid"
    meta_path = lock_dir / "meta.json"
    acquired = False
    stale_sec = max(15, _env_int("PM_HARD_SOURCE_LIVE_SSOT_WRITER_LOCK_STALE_SEC", 120))

    def _active_pid(pid_text: str) -> bool:
        try:
            pid = int(str(pid_text or "").strip())
        except Exception:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except Exception:
            return True

    try:
        try:
            lock_dir.mkdir(parents=True, exist_ok=False)
            acquired = True
        except FileExistsError:
            existing_pid = ""
            try:
                existing_pid = pid_path.read_text(encoding="utf-8").strip()
            except Exception:
                existing_pid = ""
            age = 0.0
            try:
                age = max(0.0, time.time() - lock_dir.stat().st_mtime)
            except Exception:
                age = 0.0
            if (not _active_pid(existing_pid)) or age > stale_sec:
                try:
                    for child in lock_dir.iterdir():
                        child.unlink()
                    lock_dir.rmdir()
                except Exception:
                    pass
                try:
                    lock_dir.mkdir(parents=True, exist_ok=False)
                    acquired = True
                except Exception:
                    acquired = False
        if acquired:
            payload = {
                "pid": os.getpid(),
                "stage": str(stage or "poll_once"),
                "event_date": _normalize_date(event_date) or _chengdu_today(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "owner": "hard_source_direct_poller",
            }
            pid_path.write_text(str(os.getpid()), encoding="utf-8")
            meta_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            preempt_result = _preempt_noncritical_refreshes_for_live(
                event_date=event_date,
                stage=stage,
            )
            if preempt_result.get("actions"):
                payload["preempt_noncritical_refresh"] = preempt_result
                meta_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        yield
    finally:
        if acquired:
            try:
                current = pid_path.read_text(encoding="utf-8").strip()
            except Exception:
                current = ""
            if not current or current == str(os.getpid()):
                try:
                    for child in lock_dir.iterdir():
                        child.unlink()
                    lock_dir.rmdir()
                except Exception:
                    pass


def _json_loads(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _load_latest_fact(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    slug: str = "",
) -> Optional[sqlite3.Row]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    if not event_key:
        return None
    params: List[Any] = [event_key]
    slug_sql = "AND h.slug >= ? AND h.slug < ?"
    params.extend([CHENGDU_SLUG_PREFIX, "highest-temperature-in-chengdu."])
    if slug:
        slug_sql = "AND h.slug = ?"
        params = [event_key, slug]
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT h.id, h.run_date, h.slug, h.event_date, h.fact_as_of,
               h.model_snapshot_id, h.market_snapshot_id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE h.event_date = ?
          {slug_sql}
        ORDER BY h.fact_as_of DESC, h.id DESC
        LIMIT 1
        """,
        tuple(params),
    )
    return cur.fetchone()


def _load_latest_fact_payload(
    db_path: Path | str,
    *,
    event_date: str,
    slug: str = "",
) -> Dict[str, Any]:
    try:
        with _connect(db_path) as conn:
            if _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_LATEST_PAYLOAD_SUMMARY_CACHE"), True):
                summary = _load_latest_fact_summary_payload(conn, event_date=event_date, slug=slug)
                if summary:
                    return summary
            latest = _load_latest_fact(conn, event_date=event_date, slug=slug)
            if latest is None:
                return {}
            return _json_loads(latest["fact_json"])
    except Exception:
        return {}


def _load_latest_fact_summary_payload(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    slug: str = "",
) -> Dict[str, Any]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    if not event_key:
        return {}
    params: List[Any]
    slug_sql = "AND l.slug >= ? AND l.slug < ?"
    params = [event_key, CHENGDU_SLUG_PREFIX, "highest-temperature-in-chengdu."]
    if slug:
        slug_sql = "AND l.slug = ?"
        params = [event_key, slug]
    try:
        row = conn.execute(
            f"""
            SELECT c.summary_json
            FROM event_fact_latest l
            JOIN event_fact_summary_cache c ON c.fact_id = l.fact_id
            WHERE l.event_date = ?
              {slug_sql}
            ORDER BY c.fact_as_of DESC, c.fact_id DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    return _json_loads(row["summary_json"])


def _posterior_target_summary_payload_usable(payload: Mapping[str, Any]) -> bool:
    if not isinstance(payload, Mapping) or not payload:
        return False
    for key in (
        "posterior_target_bucket_selection",
        "target_bucket_selection",
        "target_realization_capital_plan",
    ):
        if isinstance(payload.get(key), Mapping):
            bucket, _prob, _source = _canonical_target_from_payload(payload)
            return bool(bucket)
    return False


def _load_latest_fact_snapshot_for_posterior_target(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    slug: str = "",
) -> Tuple[Optional[Mapping[str, Any]], Dict[str, Any], str]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    if _truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_LATEST_PAYLOAD_SUMMARY_CACHE"), True):
        slug_sql = "AND l.slug >= ? AND l.slug < ?"
        params: List[Any] = [event_key, CHENGDU_SLUG_PREFIX, "highest-temperature-in-chengdu."]
        if slug:
            slug_sql = "AND l.slug = ?"
            params = [event_key, slug]
        try:
            row = conn.execute(
                f"""
                SELECT
                    h.id,
                    h.run_date,
                    h.slug,
                    h.event_date,
                    h.fact_as_of,
                    c.summary_json
                FROM event_fact_latest l
                JOIN event_fact_history h ON h.id = l.fact_id
                JOIN event_fact_summary_cache c ON c.fact_id = l.fact_id
                WHERE l.event_date = ?
                  {slug_sql}
                ORDER BY c.fact_as_of DESC, c.fact_id DESC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row:
            payload = _json_loads(row["summary_json"])
            if _posterior_target_summary_payload_usable(payload):
                latest = {
                    "id": row["id"],
                    "run_date": row["run_date"],
                    "slug": row["slug"],
                    "event_date": row["event_date"],
                    "fact_as_of": row["fact_as_of"],
                }
                return latest, payload, "summary_cache"
    latest = _load_latest_fact(conn, event_date=event_key, slug=slug)
    if latest is None:
        return None, {}, "missing"
    return latest, _json_loads(latest["fact_json"]), "fact_json"


def _latest_settlement_signal_from_event_runs(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> Dict[str, Any]:
    slug_key = str(slug or "").strip()
    event_key = _normalize_date(event_date)
    if not slug_key or not event_key:
        return {}
    try:
        row = conn.execute(
            """
            SELECT *
            FROM event_runs
            WHERE slug = ?
              AND substr(event_date, 1, 10) = ?
              AND settlement_actual IS NOT NULL
              AND TRIM(CAST(settlement_actual AS TEXT)) != ''
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (slug_key, event_key),
        ).fetchone()
    except sqlite3.Error:
        return {}
    if row is None:
        return {}
    actual = _to_float(row["settlement_actual"])
    if actual is None:
        return {}
    precision_profile = _event_run_settlement_precision_profile(row, actual)
    if bool(precision_profile.get("boundary_ambiguous")):
        return {
            "settlement_signal_blocked": True,
            "settlement_signal_block_reason": "boundary_ambiguous",
            "settlement_precision_profile": precision_profile,
            "settlement_signal_source": "event_runs",
            "settlement_signal_created_at": str(row["created_at"] or ""),
        }
    return {
        "settlement_actual": float(actual),
        "settlement_actual_c": float(actual),
        "market_closed": bool(_truthy(row["market_closed"], default=True)),
        "observed_today_max_final": True,
        "hard_source_final_daily_max": True,
        "hard_source_settlement_final": True,
        "temperature_observation_final_daily_max": True,
        "settlement_signal_source": "event_runs",
        "settlement_signal_created_at": str(row["created_at"] or ""),
    }


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        if key in row.keys():
            return row[key]
    except Exception:
        return default
    return default


def _event_run_settlement_precision_profile(row: sqlite3.Row, actual_c: float) -> Dict[str, Any]:
    rounding_rule = str(_row_value(row, "rounding_rule", "whole_degrees") or "whole_degrees")
    settlement_unit = str(_row_value(row, "market_unit", "C") or "C")
    native_value = _row_value(row, "settlement_native_value")
    native_unit = _row_value(row, "settlement_native_unit")
    native_precision = _row_value(row, "settlement_native_precision")
    if native_value is not None and str(native_unit or "").strip():
        return swm.temperature_settlement_precision_profile(
            native_value=native_value,
            native_unit=native_unit,
            settlement_unit=settlement_unit,
            settlement_policy=rounding_rule,
            native_precision=str(native_precision or ""),
            source="event_runs.settlement_actual_native",
            display_value=actual_c,
            display_unit=settlement_unit,
        )
    if abs(float(actual_c) - round(float(actual_c))) <= 1e-6:
        return {
            "source": "event_runs.settlement_actual",
            "native_value": float(actual_c),
            "native_unit": settlement_unit,
            "native_precision": "integer_c_display",
            "display_value": float(actual_c),
            "display_unit": settlement_unit,
            "settlement_unit": settlement_unit,
            "settlement_policy": rounding_rule,
            "boundary_ambiguous": False,
            "boundary_status": "ok",
            "ambiguous_reason": "",
        }
    f_value = float(actual_c) * 9.0 / 5.0 + 32.0
    f_integer = int(round(float(f_value)))
    if abs(float(f_value) - float(f_integer)) <= 1e-6:
        return swm.temperature_settlement_precision_profile(
            native_value=f_integer,
            native_unit="F",
            settlement_unit=settlement_unit,
            settlement_policy=rounding_rule,
            native_precision="integer_display",
            source="event_runs.settlement_actual_reverse_f_guard",
            display_value=actual_c,
            display_unit=settlement_unit,
        )
    return {
        "source": "event_runs.settlement_actual",
        "native_value": float(actual_c),
        "native_unit": settlement_unit,
        "native_precision": "numeric_without_native_metadata",
        "display_value": float(actual_c),
        "display_unit": settlement_unit,
        "settlement_unit": settlement_unit,
        "settlement_policy": rounding_rule,
        "boundary_ambiguous": False,
        "boundary_status": "native_precision_missing",
        "ambiguous_reason": "",
    }


def _final_settlement_signal_from_wu_twc_features(
    source_features: Mapping[str, Any],
    *,
    refreshed_at: str = "",
    rounding_rule: Optional[str] = "whole_degrees",
    settlement_unit: Optional[str] = "C",
) -> Dict[str, Any]:
    if not isinstance(source_features, Mapping):
        return {}
    final = any(
        _truthy(source_features.get(key), default=False)
        for key in (
            "observed_today_max_final",
            "hard_source_final_daily_max",
            "hard_source_settlement_final",
            "temperature_observation_final_daily_max",
        )
    )
    if not final:
        return {}
    precision_profile = swm.temperature_settlement_profile_from_wu_twc_features(
        source_features,
        rounding_rule=rounding_rule or "whole_degrees",
        settlement_unit=settlement_unit or "C",
    )
    if bool(precision_profile.get("boundary_ambiguous")):
        return {
            "settlement_signal_blocked": True,
            "settlement_signal_block_reason": "boundary_ambiguous",
            "settlement_precision_profile": precision_profile,
            "settlement_signal_source": "wu_twc_final_settlement",
            "settlement_signal_created_at": str(
                source_features.get("wu_twc_observed_intraday_fetched_at")
                or source_features.get("wu_twc_observed_intraday_today_max_at")
                or refreshed_at
                or ""
            ),
        }
    actual = None
    for key in (
        "settlement_actual",
        "settlement_actual_c",
        "observed_today_max_c",
        "wu_twc_observed_intraday_today_max_c",
    ):
        actual = _to_float(source_features.get(key))
        if actual is not None:
            break
    if actual is None:
        return {}
    return {
        "settlement_actual": float(actual),
        "settlement_actual_c": float(actual),
        "market_closed": True,
        "observed_today_max_final": True,
        "hard_source_final_daily_max": True,
        "hard_source_settlement_final": True,
        "temperature_observation_final_daily_max": True,
        "settlement_signal_source": "wu_twc_final_settlement",
        "settlement_signal_created_at": str(
            source_features.get("wu_twc_observed_intraday_fetched_at")
            or source_features.get("wu_twc_observed_intraday_today_max_at")
            or refreshed_at
            or ""
        ),
    }


def _merge_final_settlement_signal_into_payload(
    payload: Mapping[str, Any],
    signal: Mapping[str, Any],
) -> Dict[str, Any]:
    if not signal:
        return dict(payload)
    merged = dict(payload)
    features = _features(merged)
    if _truthy(signal.get("settlement_signal_blocked"), default=False):
        profile = signal.get("settlement_precision_profile")
        if isinstance(profile, Mapping):
            features["settlement_precision_profile"] = dict(profile)
            merged["settlement_precision_profile"] = dict(profile)
        reason = str(signal.get("settlement_signal_block_reason") or "blocked").strip()
        features["settlement_signal_blocked"] = True
        features["settlement_signal_block_reason"] = reason
        merged["settlement_signal_blocked"] = True
        merged["settlement_signal_block_reason"] = reason
        merged["weather_likelihood_features"] = features
        merged["weather_likelihood_features_compact"] = _compact_weather_features(features)
        merged["weather_likelihood_feature_count"] = len(features)
        return merged

    actual = _to_float(signal.get("settlement_actual"))
    if actual is not None and _to_float(merged.get("settlement_actual")) is None:
        merged["settlement_actual"] = float(actual)
    if actual is not None and _to_float(features.get("settlement_actual")) is None:
        features["settlement_actual"] = float(actual)
    if actual is not None and _to_float(features.get("settlement_actual_c")) is None:
        features["settlement_actual_c"] = float(actual)
    if actual is not None and _to_float(features.get("observed_today_max_c")) is None:
        features["observed_today_max_c"] = float(actual)
    if actual is not None and _to_float(features.get("wu_twc_observed_intraday_today_max_c")) is None:
        features["wu_twc_observed_intraday_today_max_c"] = float(actual)

    if _truthy(signal.get("market_closed"), default=False) and not _truthy(merged.get("market_closed"), default=False):
        merged["market_closed"] = 1
    if _truthy(signal.get("market_closed"), default=False) and not _truthy(features.get("market_closed"), default=False):
        features["market_closed"] = True

    for key in (
        "observed_today_max_final",
        "hard_source_final_daily_max",
        "hard_source_settlement_final",
        "temperature_observation_final_daily_max",
    ):
        if _truthy(signal.get(key), default=False) and not _truthy(features.get(key), default=False):
            features[key] = True
            merged.setdefault(key, True)

    source = str(signal.get("settlement_signal_source") or "event_runs").strip()
    created_at = str(signal.get("settlement_signal_created_at") or "").strip()
    if source:
        features.setdefault("settlement_signal_source", source)
        merged.setdefault("settlement_signal_source", source)
        if actual is not None:
            features.setdefault("settlement_actual_source", source)
            merged.setdefault("settlement_actual_source", source)
        features.setdefault("observed_today_max_source", source)
        features.setdefault("source", source)
    if created_at:
        features.setdefault("settlement_signal_created_at", created_at)
        merged.setdefault("settlement_signal_created_at", created_at)
        features.setdefault("wu_twc_observed_intraday_fetched_at", created_at)
        features.setdefault("wu_twc_observed_intraday_today_max_at", created_at)
    features.setdefault("observed_today_max_trusted", True)
    features.setdefault("wu_twc_observed_intraday_primary_max", True)
    features.setdefault("wu_twc_observed_intraday_stale", False)

    merged["weather_likelihood_features"] = features
    merged["weather_likelihood_features_compact"] = _compact_weather_features(features)
    merged["weather_likelihood_feature_count"] = len(features)

    final_row_fields = {
        "settlement_actual": merged.get("settlement_actual"),
        "market_closed": merged.get("market_closed"),
        "observed_today_max_final": features.get("observed_today_max_final"),
        "hard_source_final_daily_max": features.get("hard_source_final_daily_max"),
        "hard_source_settlement_final": features.get("hard_source_settlement_final"),
        "temperature_observation_final_daily_max": features.get("temperature_observation_final_daily_max"),
        "settlement_signal_source": source,
        "settlement_signal_created_at": created_at,
        "observed_today_max_c": features.get("observed_today_max_c"),
        "observed_today_max_source": features.get("observed_today_max_source"),
        "observed_today_max_trusted": features.get("observed_today_max_trusted"),
    }
    for rows_key in ("rows", "distribution"):
        raw_rows = merged.get(rows_key)
        if not isinstance(raw_rows, list):
            continue
        next_rows: List[Any] = []
        for row in raw_rows:
            if isinstance(row, Mapping):
                item = dict(row)
                for key, value in final_row_fields.items():
                    if value not in (None, "") and item.get(key) in (None, ""):
                        item[key] = value
                next_rows.append(item)
            else:
                next_rows.append(row)
        merged[rows_key] = next_rows

    provenance = dict(merged.get("fact_provenance") or {})
    provenance.setdefault(
        "event_runs_settlement_signal",
        {
            "source": source or "event_runs",
            "settlement_actual": actual,
            "market_closed": bool(_truthy(signal.get("market_closed"), default=False)),
            "created_at": created_at,
            "merge_policy": "fill_missing_final_settlement_signal",
        },
    )
    merged["fact_provenance"] = provenance
    return merged


def _features(payload: Mapping[str, Any]) -> Dict[str, Any]:
    raw = payload.get("weather_likelihood_features")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _rows(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    raw = payload.get("distribution") or payload.get("rows") or []
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _append_csv_token(raw: Any, token: str) -> str:
    parts = [part.strip() for part in str(raw or "").split(",") if part.strip()]
    if token and token not in parts:
        parts.append(token)
    return ",".join(parts)


def _compact_weather_features(features: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        from ui.server import _compact_weather_likelihood_features_for_ui

        compact = _compact_weather_likelihood_features_for_ui(dict(features))
        return dict(compact) if isinstance(compact, Mapping) else {}
    except Exception:
        return {
            key: value
            for key, value in dict(features).items()
            if key.startswith("zuuu_metar_raw_awc_") or key.startswith("wu_twc_observed_intraday_")
        }


def _merge_compact_weather_features_into_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(payload or {})
    try:
        from ui.server import _merge_compact_weather_features_into_fact_item

        _merge_compact_weather_features_into_fact_item(merged)
        return merged
    except Exception:
        pass

    features_raw = merged.get("weather_likelihood_features")
    compact_raw = merged.get("weather_likelihood_features_compact")
    features = dict(features_raw) if isinstance(features_raw, Mapping) else {}
    if isinstance(compact_raw, Mapping):
        for key, value in compact_raw.items():
            if features.get(key) in (None, "") and value not in (None, ""):
                features[key] = value
    if features:
        merged["weather_likelihood_features"] = features
        merged["weather_likelihood_features_compact"] = _compact_weather_features(features)
    return merged


def _merge_latest_awc_hard_observation_into_payload(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
    *,
    slug: str,
    event_date: str,
) -> Dict[str, Any]:
    merged = _merge_compact_weather_features_into_payload(payload)
    try:
        from ui.server import (
            _awc_features_from_fact_payload_for_merge_fill,
            _distribution_weather_feature_fields,
            _latest_awc_hard_observation_payload_for_merge_fill,
            _weather_feature_overlay_is_stronger,
        )
    except Exception:
        return merged

    try:
        overlay_as_of, overlay_payload = _latest_awc_hard_observation_payload_for_merge_fill(
            conn,
            str(slug or merged.get("slug") or ""),
            _normalize_date(event_date or merged.get("event_date") or ""),
        )
    except Exception:
        return merged
    overlay_features = _awc_features_from_fact_payload_for_merge_fill(overlay_payload)
    if not overlay_features:
        return merged
    features = _features(merged)
    if not _weather_feature_overlay_is_stronger(overlay_features, features):
        return merged
    refreshed_at = str(
        overlay_features.get("weather_likelihood_refreshed_at")
        or overlay_features.get("zuuu_metar_raw_awc_fetched_at")
        or overlay_as_of
        or ""
    )
    overlay_fields = _distribution_weather_feature_fields(
        overlay_features,
        refreshed_at=refreshed_at,
    )
    if not overlay_fields:
        return merged

    features = dict(features)
    features.update(overlay_fields)
    merged["weather_likelihood_features"] = features
    merged["weather_likelihood_features_compact"] = _compact_weather_features(features)
    merged["weather_likelihood_feature_count"] = len(features)
    provenance = dict(merged.get("fact_provenance") or {})
    provenance["awc_hard_observation_momentum_merge_fill"] = {
        "source": "hard_source_direct_poller.latest_awc_overlay",
        "existing_fact_as_of": overlay_as_of,
        "refreshed_at": refreshed_at,
    }
    merged["fact_provenance"] = provenance
    return merged


def _source_is_wu_twc_observed(source: Any) -> bool:
    return str(source or "").strip().startswith("wu_twc_observed_intraday")


def _flatten_feature_tokens(value: Any) -> List[str]:
    tokens: List[str] = []
    if isinstance(value, Mapping):
        for item in value.values():
            tokens.extend(_flatten_feature_tokens(item))
        return tokens
    if isinstance(value, (list, tuple, set)):
        for item in value:
            tokens.extend(_flatten_feature_tokens(item))
        return tokens
    text = str(value or "").strip()
    return [text.lower()] if text else []


def _wu_twc_intraday_hard_source_demotion_reason(features: Mapping[str, Any]) -> str:
    if _to_float(features.get("settlement_actual")) is not None or _to_float(features.get("settlement_actual_c")) is not None:
        return ""
    if any(
        _truthy(features.get(key), default=False)
        for key in (
            "observed_today_max_final",
            "hard_source_final_daily_max",
            "hard_source_settlement_final",
            "temperature_observation_final_daily_max",
        )
    ):
        return ""

    alert = features.get("weather_source_delay_deformation_alert")
    alert_map = alert if isinstance(alert, Mapping) else {}
    alert_tokens = _flatten_feature_tokens(alert_map)
    alert_text = " ".join(alert_tokens)
    if _truthy(alert_map.get("alert"), default=False) and (
        "wu" in alert_text
        or "twc" in alert_text
        or "nowcast" in alert_text
        or "temperature_deformation" in alert_text
        or "temperature_mismatch" in alert_text
    ):
        return "wu_twc_intraday_deformation_alert"

    field_tokens: List[str] = []
    for key in (
        "weather_source_alert_codes",
        "wu_twc_observed_intraday_current_error",
        "wu_twc_observed_intraday_max_error",
        "wu_twc_observed_intraday_history_error",
        "wu_twc_observed_intraday_date_mismatch_reason",
        "wu_twc_observed_intraday_event_date_mismatch_reason",
    ):
        field_tokens.extend(_flatten_feature_tokens(features.get(key)))
    field_text = " ".join(field_tokens)
    if "current_observation_event_date_mismatch" in field_text or "event_date_mismatch" in field_text:
        return "wu_twc_intraday_event_date_mismatch"
    if any(
        _truthy(features.get(key), default=False)
        for key in (
            "wu_twc_observed_intraday_date_mismatch",
            "wu_twc_observed_intraday_event_date_mismatch",
            "wu_twc_observed_intraday_current_observation_event_date_mismatch",
        )
    ):
        return "wu_twc_intraday_event_date_mismatch"
    return ""


def _awc_today_max_anchor(features: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    awc_max = _to_float(features.get("zuuu_metar_raw_awc_today_max_c"))
    if awc_max is None:
        return None, ""
    if any(
        _truthy(features.get(key), default=False)
        for key in (
            "zuuu_metar_raw_awc_date_mismatch",
            "metar_observation_date_mismatch",
        )
    ):
        return None, ""
    return float(awc_max), "zuuu_metar_raw_awc"


def _awc_current_temp_anchor(features: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    if any(
        _truthy(features.get(key), default=False)
        for key in (
            "zuuu_metar_raw_awc_date_mismatch",
            "metar_observation_date_mismatch",
        )
    ):
        return None, ""
    awc_current = _to_float(
        features.get("zuuu_metar_raw_awc_latest_temp_c")
        or features.get("metar_temperature_c")
    )
    if awc_current is None:
        return None, ""
    return float(awc_current), "zuuu_metar_raw_awc_latest"


def _canonicalize_wu_twc_observed_today_max(features: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the generic observed max anchored to hard AWC/METAR when present.

    WU/TWC intraday remains available under its own namespace for warning and
    high-frequency likelihood, but it must not become the hard observed max
    when AWC/METAR has a same-day hard observation.
    """
    wu_max = _to_float(features.get("wu_twc_observed_intraday_today_max_c"))
    if wu_max is None:
        return features
    demotion_reason = _wu_twc_intraday_hard_source_demotion_reason(features)
    if demotion_reason:
        features["wu_twc_observed_intraday_hard_source_eligible"] = False
        features["wu_twc_observed_intraday_hard_source_demoted"] = True
        features["wu_twc_observed_intraday_hard_source_demoted_reason"] = demotion_reason
        features["wu_twc_observed_intraday_likelihood_role"] = "intraday_likelihood_warning"
    else:
        features["wu_twc_observed_intraday_hard_source_eligible"] = True
    wu_source = str(
        features.get("wu_twc_observed_intraday_today_max_source")
        or "wu_twc_observed_intraday_today_max"
    ).strip()
    awc_max, awc_source = _awc_today_max_anchor(features)
    if awc_max is not None:
        features["observed_today_max_c"] = float(awc_max)
        features["observed_today_max_source"] = awc_source
        features["observed_today_max_trusted"] = True
        features["observed_today_max_hard_source_eligible"] = True
        max_at = features.get("zuuu_metar_raw_awc_today_max_at")
        if max_at not in (None, ""):
            features["observed_today_max_at"] = max_at
        return features
    generic_source = str(features.get("observed_today_max_source") or "").strip()
    generic_missing = _to_float(features.get("observed_today_max_c")) is None
    if generic_missing or _source_is_wu_twc_observed(generic_source):
        features["observed_today_max_c"] = float(wu_max)
        features["observed_today_max_source"] = wu_source
        features["observed_today_max_trusted"] = not bool(demotion_reason)
        if demotion_reason:
            features["observed_today_max_hard_source_eligible"] = False
            features["observed_today_max_hard_source_demoted_reason"] = demotion_reason
        max_at = features.get("wu_twc_observed_intraday_today_max_at")
        if max_at not in (None, ""):
            features["observed_today_max_at"] = max_at
    return features


def _canonicalize_wu_twc_observed_current(features: Dict[str, Any]) -> Dict[str, Any]:
    """Keep generic current observations anchored to AWC/METAR when WU is suspect."""
    wu_current = _to_float(features.get("wu_twc_observed_intraday_current_temp_c"))
    if wu_current is None:
        return features
    awc_current, awc_source = _awc_current_temp_anchor(features)
    demotion_reason = _wu_twc_intraday_hard_source_demotion_reason(features)
    if awc_current is not None and demotion_reason:
        features["observed_temp"] = float(awc_current)
        features["observed_temp_c"] = float(awc_current)
        features["observed_temp_source"] = awc_source
        return features
    generic_source = str(features.get("observed_temp_source") or "").strip()
    generic_missing = _to_float(features.get("observed_temp_c")) is None
    if generic_missing or _source_is_wu_twc_observed(generic_source):
        features["observed_temp"] = float(wu_current)
        features["observed_temp_c"] = float(wu_current)
        features["observed_temp_source"] = "wu_twc_observed_intraday_current"
    return features


def _merge_metar_pws_lineage(payload: Mapping[str, Any], source_features: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(payload)
    lineage = dict(merged.get("weather_source_lineage") or {})
    source_defs = (
        ("zuuu_metar_iem_noaaport", "IEM ASOS/METAR NOAAPort archive", "METAR cross_check"),
        ("zuuu_metar_avwx", "AVWX REST", "METAR cross_check"),
        ("zuuu_metar_checkwx", "CheckWX", "METAR cross_check"),
        ("twc_pws_rapid_history_aux", "Weather.com/TWC PWS", "auxiliary_intraday_warning_not_final_settlement"),
    )
    for prefix, provider, role in source_defs:
        if not any(str(key).startswith(f"{prefix}_") for key in source_features):
            continue
        lineage[prefix] = {
            "source_id": prefix,
            "provider": source_features.get(f"{prefix}_provider") or provider,
            "role": source_features.get(f"{prefix}_role") or role,
            "url": source_features.get(f"{prefix}_source_url") or source_features.get(f"{prefix}_url") or "",
            "station_id": source_features.get(f"{prefix}_station_id") or ("ZUUU" if prefix.startswith("zuuu_metar_") else ""),
            "available": bool(source_features.get(f"{prefix}_available")),
            "fetched_at": source_features.get(f"{prefix}_fetched_at") or "",
            "latest_at": source_features.get(f"{prefix}_latest_at") or source_features.get(f"{prefix}_today_max_at") or "",
            "latest_temp_c": source_features.get(f"{prefix}_latest_temp_c"),
            "today_max_c": source_features.get(f"{prefix}_today_max_c"),
            "today_max_at": source_features.get(f"{prefix}_today_max_at") or "",
            "error": source_features.get(f"{prefix}_error") or "",
        }
    merged["weather_source_lineage"] = lineage
    if source_features.get("metar_cross_check_summary") not in (None, ""):
        merged["metar_cross_check_summary"] = source_features.get("metar_cross_check_summary")
    if source_features.get("metar_cross_check_sources") not in (None, ""):
        merged["metar_cross_check_sources"] = source_features.get("metar_cross_check_sources")
    return merged


def _clear_stale_hard_source_constraints(rows: List[Dict[str, Any]]) -> None:
    prob_keys = (
        "q_raw",
        "q_round",
        "q_bayes_posterior",
        "q_bayes_lcb",
        "q_bayes_ucb",
        "q_entry",
        "q_calibrated",
        "q_calibrated_lcb",
        "q_calibrated_ucb",
        "q_quote",
        "q_quote_lcb",
        "q_quote_ucb",
        "q_temperature_bucket_posterior",
        "q_contract_payout_posterior",
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in prob_keys:
            value = _to_float(row.get(f"{key}_pre_hard_source_override"))
            if value is not None:
                row[key] = float(value)
        row["q_posterior_observed_constraints_hard_zero"] = False
        row["q_posterior_observed_constraints_reason_codes"] = []
        row["q_posterior_observed_constraints_hard_zero_reasons"] = []
        row["posterior_settlement_candidate_bucket_hard_zero"] = False
        row["posterior_settlement_candidate_bucket_hard_zero_reasons"] = []
        stale_hard_reasons = row.get("hard_reasons")
        if isinstance(stale_hard_reasons, list) and set(str(reason) for reason in stale_hard_reasons).issubset(
            {
                "observed_today_max_above_bucket",
                "hard_source_lower_bound_constraint",
                "final_bucket_lock",
                "hard_source_final_bucket_lock",
            }
        ):
            row["hard_reasons"] = []
        row["edge_min_exit_yes"] = False
        row["edge_min_exit_reasons_yes"] = []
        row["edge_min_exit_codes_yes"] = []
        row["target_adjustment_force_exit"] = False
        row.pop("target_adjustment_exit_reason_code", None)
        row.pop("target_adjustment_exit_reason", None)


def _recompute_hard_source_constraints_for_payload(
    payload: Dict[str, Any],
    *,
    event_date: str = "",
) -> None:
    rows = _rows(payload)
    if not rows:
        return
    with _temporary_env({"PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED": "0"}):
        meta = apply_hard_source_posterior_override_to_distribution(
            rows,
            _features(payload),
            base_probs=_posterior_base_probs(rows),
            market_var=str(payload.get("market_var") or "temperature_2m_max"),
            rounding_rule=str(payload.get("rounding_rule") or ""),
            event_tz=str(payload.get("event_tz") or payload.get("tz") or "Asia/Shanghai"),
            event_date=event_date or str(payload.get("event_date") or ""),
        )
    if bool(meta.get("applied")):
        payload["hard_source_posterior_override"] = True
        payload["hard_source_posterior_override_meta"] = dict(meta)
    else:
        _clear_stale_hard_source_constraints(rows)
        payload["hard_source_posterior_override"] = False
        payload["hard_source_posterior_override_meta"] = dict(meta)
    payload["distribution"] = rows
    payload["rows"] = rows


def _refresh_peak_estimator_features(features: Dict[str, Any]) -> None:
    try:
        peak = estimate_peak_temperature_from_features(features)
    except Exception:
        return
    if not isinstance(peak, Mapping):
        return
    peak_dict = dict(peak)
    features["peak_estimator"] = peak_dict
    features["peak_estimator_enabled"] = bool(peak_dict.get("enabled"))
    feature_map = {
        "source": "peak_estimator_source",
        "mu_c": "peak_estimator_mu_c",
        "sigma_c": "peak_estimator_sigma_c",
        "kalman_gain": "peak_estimator_kalman_gain",
        "extrapolation_c": "peak_estimator_extrapolation_c",
        "combined_discount": "peak_estimator_combined_discount",
        "observed_today_max_source": "peak_estimator_observed_today_max_source",
        "raw_observed_today_max_c": "peak_estimator_raw_observed_today_max_c",
        "raw_observed_today_max_source": "peak_estimator_raw_observed_today_max_source",
        "raw_observed_today_max_demoted_reason": "peak_estimator_raw_observed_today_max_demoted_reason",
    }
    for source_key, target_key in feature_map.items():
        value = peak_dict.get(source_key)
        if value not in (None, ""):
            features[target_key] = value


def _merge_source_features_into_payload(
    payload: Mapping[str, Any],
    source_features: Mapping[str, Any],
    *,
    source_id: str,
    refreshed_at: str,
) -> Dict[str, Any]:
    merged = _merge_compact_weather_features_into_payload(payload)
    features = _features(merged)
    if _truthy(source_features.get("_clear_expected_peak_hour_distribution"), False):
        for key in (
            "expected_peak_hour_distribution",
            "peak_hour_distribution",
            "expected_peak_hour_local",
            "expected_peak_hour_source",
            "expected_peak_at_local",
            "expected_peak_window_start_hour_local",
            "expected_peak_window_end_hour_local",
            "expected_peak_window_source",
            "weather_com_forecast_peak_at",
            "weather_com_forecast_peak_time_source",
            "twc_forecast_peak_at",
            "twc_forecast_peak_time_source",
        ):
            features.pop(key, None)
    for key, value in source_features.items():
        if str(key).startswith("_"):
            continue
        if key == "source":
            continue
        if value not in (None, ""):
            features[key] = value
    if source_id == "wu_twc_observed_intraday":
        features = _canonicalize_wu_twc_observed_current(features)
        features = _canonicalize_wu_twc_observed_today_max(features)
    if refreshed_at:
        features["weather_likelihood_refreshed_at"] = refreshed_at
        merged["weather_likelihood_refreshed_at"] = refreshed_at
    features["source"] = str(features.get("source") or source_id)
    _refresh_peak_estimator_features(features)
    merged["weather_likelihood_features"] = features
    merged["weather_likelihood_features_compact"] = _compact_weather_features(features)
    merged["weather_likelihood_aux_source"] = _append_csv_token(
        merged.get("weather_likelihood_aux_source"),
        source_id,
    )
    merged["weather_likelihood_feature_count"] = len(features)
    lineage = dict(merged.get("weather_source_lineage") or {})
    lineage[source_id] = {
        "source_id": source_id,
        "provider": source_features.get(f"{source_id}_provider")
        or ("NOAA AviationWeather Raw METAR" if source_id == "zuuu_metar_raw_awc" else "Weather.com/TWC"),
        "role": source_features.get(f"{source_id}_role") or "hard_observation",
        "url": source_features.get(f"{source_id}_source_url")
        or source_features.get(f"{source_id}_url")
        or "",
        "fetched_at": source_features.get(f"{source_id}_fetched_at") or refreshed_at,
        "latest_at": source_features.get(f"{source_id}_latest_at")
        or source_features.get(f"{source_id}_current_at")
        or source_features.get(f"{source_id}_today_max_at")
        or "",
        "today_max_c": source_features.get(f"{source_id}_today_max_c"),
    }
    merged["weather_source_lineage"] = lineage
    row_fields = {
        key: value
        for key, value in source_features.items()
        if key.startswith("zuuu_metar_raw_awc_")
        or key.startswith("zuuu_metar_iem_noaaport_")
        or key.startswith("zuuu_metar_avwx_")
        or key.startswith("zuuu_metar_checkwx_")
        or key.startswith("twc_pws_rapid_history_aux_")
        or key.startswith("wu_twc_observed_intraday_")
        or key.startswith("wu_forecast_peak_")
        or key.startswith("weather_com_forecast_peak_")
        or key.startswith("twc_forecast_peak_")
        or key
        in {
            "observed_temp",
            "observed_temp_c",
            "observed_temp_source",
            "observed_today_max_c",
            "observed_today_max_source",
            "observed_today_max_trusted",
            "observed_today_max_hard_source_eligible",
            "observed_today_max_hard_source_demoted_reason",
            "twc_provider",
            "twc_provider_role",
            "expected_peak_hour_distribution",
            "peak_hour_distribution",
            "expected_peak_hour_local",
            "expected_peak_hour_source",
            "expected_peak_at_local",
            "expected_peak_window_start_hour_local",
            "expected_peak_window_end_hour_local",
            "expected_peak_window_source",
        }
    }
    if refreshed_at:
        row_fields["weather_likelihood_refreshed_at"] = refreshed_at
    if source_id == "wu_twc_observed_intraday":
        for key in (
            "observed_temp",
            "observed_temp_c",
            "observed_temp_source",
            "observed_today_max_c",
            "observed_today_max_source",
            "observed_today_max_trusted",
            "observed_today_max_hard_source_eligible",
            "observed_today_max_hard_source_demoted_reason",
            "observed_today_max_at",
        ):
            if features.get(key) not in (None, ""):
                row_fields[key] = features.get(key)
    for key, value in features.items():
        if value in (None, ""):
            continue
        if (
            str(key).startswith("temperature_momentum_")
            or str(key).startswith("temperature_trajectory_stable_rate_")
            or str(key).startswith("temperature_rise_rate_")
            or str(key).startswith("observation_path_primary_")
        ):
            row_fields[key] = value
    for rows_key in ("rows", "distribution"):
        raw_rows = merged.get(rows_key)
        if not isinstance(raw_rows, list):
            continue
        next_rows: List[Any] = []
        for row in raw_rows:
            if isinstance(row, Mapping):
                item = dict(row)
                item.update(row_fields)
                next_rows.append(item)
            else:
                next_rows.append(row)
        merged[rows_key] = next_rows
    _recompute_hard_source_constraints_for_payload(
        merged,
        event_date=str(features.get("event_date") or merged.get("event_date") or ""),
    )
    provenance = dict(merged.get("fact_provenance") or {})
    provenance[f"{source_id}_hard_observation_refresh"] = {
        "source": source_id,
        "refreshed_at": refreshed_at,
        "today_max_c": source_features.get(f"{source_id}_today_max_c"),
        "fetch_duration_ms": source_features.get(f"{source_id}_fetch_duration_ms"),
    }
    merged["fact_provenance"] = provenance
    return _drop_temperature_path_derived_fields(merged)


def _drop_temperature_path_derived_fields(payload: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(payload or {})
    for key in (
        "chengdu_intraday_summary",
        "target_bucket_selection",
        "posterior_target_bucket_selection",
        "target_bucket_outlook",
        "temperature_path_distribution",
        "temperature_realization_reliability",
        "temperature_trajectory_status",
        "precipitation_evidence_coverage",
        "target_realization_capital_plan",
        "target_switch_context",
        "auto_trade_ssot",
    ):
        merged.pop(key, None)
    return merged


def _decision_ssot_context_from_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    out: Dict[str, Any] = {}
    plan = payload.get("target_realization_capital_plan")
    if isinstance(plan, Mapping):
        plan_copy = dict(plan)
        out["target_realization_capital_plan"] = plan_copy
        out["target_realization_capital_plan_version"] = plan_copy.get("version")
        out["capital_plan_target_bucket"] = plan_copy.get("target_bucket")
        out["capital_plan_target_posterior"] = plan_copy.get("target_posterior")
        out["capital_plan_second_target_bucket"] = plan_copy.get("second_target_bucket")
        out["capital_plan_second_target_posterior"] = plan_copy.get("second_target_posterior")
        out["capital_plan_summary"] = plan_copy.get("summary")
        for key in (
            "sell_old_buy_new_legs",
            "sell_old_legs",
            "buy_new_leg",
            "buy_new_legs",
            "buy_old_reverse_legs",
            "active_position_legs",
        ):
            value = plan_copy.get(key)
            if value not in (None, "", [], {}):
                out[key] = value
        if plan_copy.get("temperature_path_trade_follow_allowed") is not None:
            out["temperature_path_trade_follow_allowed"] = plan_copy.get(
                "temperature_path_trade_follow_allowed"
            )
        if plan_copy.get("temperature_path_trade_follow_reason") not in (None, ""):
            out["temperature_path_trade_follow_reason"] = plan_copy.get(
                "temperature_path_trade_follow_reason"
            )
    switch_context = payload.get("target_switch_context")
    if not isinstance(switch_context, Mapping):
        try:
            from ui.server import build_target_switch_context

            switch_context = build_target_switch_context(payload)
        except Exception as exc:
            switch_context = {
                "version": "target_switch_context_v1",
                "status": "error",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
    if isinstance(switch_context, Mapping):
        switch_copy = dict(switch_context)
        out["target_switch_context"] = switch_copy
        impact = switch_copy.get("trajectory_momentum_impact")
        if isinstance(impact, Mapping):
            out["trajectory_momentum_impact"] = dict(impact)
    path = payload.get("temperature_path_distribution")
    if isinstance(path, Mapping):
        path_copy = dict(path)
        out["temperature_path_distribution"] = path_copy
        trade_follow = path_copy.get("trade_follow")
        if isinstance(trade_follow, Mapping):
            if "temperature_path_trade_follow_allowed" not in out and trade_follow.get("allowed") is not None:
                out["temperature_path_trade_follow_allowed"] = trade_follow.get("allowed")
            if "temperature_path_trade_follow_reason" not in out and trade_follow.get("reason") not in (None, ""):
                out["temperature_path_trade_follow_reason"] = trade_follow.get("reason")
    selection = payload.get("target_bucket_selection")
    if isinstance(selection, Mapping):
        out["target_bucket_selection"] = dict(selection)
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def _merge_temperature_path_derived_fields(
    payload: Mapping[str, Any],
    *,
    slug: str,
    event_date: str,
    created_at: str,
) -> Dict[str, Any]:
    merged = dict(payload or {})
    try:
        from chengdu_intraday_summary import build_chengdu_intraday_summary_from_payload

        summary_payload = dict(merged)
        summary_payload["slug"] = str(slug or summary_payload.get("slug") or "")
        summary_payload["event_date"] = _normalize_date(event_date or summary_payload.get("event_date"))
        summary_payload.setdefault("run_date", summary_payload.get("event_date"))
        summary = build_chengdu_intraday_summary_from_payload(
            summary_payload,
            slug=str(slug or ""),
            event_date=_normalize_date(event_date),
            created_at=str(created_at or ""),
            snapshot_source="hard_source_direct_poller",
        )
    except Exception as exc:
        merged["temperature_path_derived_rebuild_error"] = f"{exc.__class__.__name__}: {exc}"
        return merged
    if not isinstance(summary, Mapping) or not summary.get("ok"):
        merged["temperature_path_derived_rebuild_error"] = str(
            (summary or {}).get("error") if isinstance(summary, Mapping) else "summary_rebuild_failed"
        )
        return merged
    for key in (
        "target_bucket_selection",
        "posterior_target_bucket_selection",
        "target_bucket_outlook",
        "temperature_path_distribution",
        "temperature_realization_reliability",
        "temperature_trajectory_status",
        "physical_likelihood",
        "physical_weather_modifier",
        "evidence_conclusions",
        "decision",
        "key_metrics",
    ):
        value = summary.get(key)
        if value not in (None, "", [], {}):
            merged[key] = value
    merged["chengdu_intraday_summary"] = summary
    merged["temperature_path_derived_rebuild"] = {
        "ok": True,
        "source": "hard_source_direct_poller.payload_summary_rebuild",
        "rebuilt_at": created_at,
    }
    return merged


def _insert_fact_snapshot(
    conn: sqlite3.Connection,
    *,
    latest_row: sqlite3.Row,
    item: Mapping[str, Any],
    fact_as_of: str,
) -> Optional[int]:
    try:
        from ui.server import (
            FACTS_COMPOSER_VERSION,
            _insert_event_fact_history,
            _upsert_event_fact_latest,
            ensure_fact_tables,
        )

        ensure_fact_tables(conn)
        fact_id = _insert_event_fact_history(
            conn,
            run_date=_normalize_date(item.get("run_date") or latest_row["run_date"]),
            slug=str(item.get("slug") or latest_row["slug"] or ""),
            event_date=_normalize_date(item.get("event_date") or latest_row["event_date"]),
            fact_as_of=fact_as_of,
            model_snapshot_id=_to_int(latest_row["model_snapshot_id"]),
            market_snapshot_id=_to_int(latest_row["market_snapshot_id"]),
            composer_version=FACTS_COMPOSER_VERSION,
            item=dict(item),
        )
        if fact_id:
            _upsert_event_fact_latest(
                conn,
                str(item.get("slug") or latest_row["slug"] or ""),
                _normalize_date(item.get("event_date") or latest_row["event_date"]),
                int(fact_id),
            )
            conn.commit()
        return int(fact_id) if fact_id else None
    except Exception:
        return None


def _event_fact_summary_cache_exists(conn: sqlite3.Connection, fact_id: int) -> bool:
    if not fact_id:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM event_fact_summary_cache WHERE fact_id = ? LIMIT 1",
            (int(fact_id),),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _materialize_ctp_payload(payload: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    try:
        from scripts.materialize_ctp_latest_facts import _materialize_payload
    except Exception as exc:
        return dict(payload or {}), {
            "ok": False,
            "reason": "ctp_materializer_import_failed",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    return _materialize_payload(payload)


def _payload_ctp_bucket_probabilities(payload: Mapping[str, Any]) -> Dict[str, float]:
    posterior = payload.get("posterior_temperature_distribution") if isinstance(payload, Mapping) else None
    bucket_probs = posterior.get("bucket_probabilities") if isinstance(posterior, Mapping) else None
    out: Dict[str, float] = {}
    if isinstance(bucket_probs, Mapping):
        for key, value in bucket_probs.items():
            prob = _to_float(value)
            if prob is not None:
                out[str(key)] = max(0.0, min(1.0, float(prob)))
        return out
    if isinstance(bucket_probs, list):
        for item in bucket_probs:
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("outcome") or item.get("label") or item.get("bucket") or "").strip()
            prob = _to_float(item.get("probability") or item.get("prob") or item.get("p"))
            if label and prob is not None:
                out[label] = max(0.0, min(1.0, float(prob)))
    return out


def _ctp_probability_changed(before: Mapping[str, float], after: Mapping[str, float]) -> bool:
    keys = set(before.keys()) | set(after.keys())
    if not keys:
        return False
    return any(abs(float(before.get(key, 0.0)) - float(after.get(key, 0.0))) > 1e-9 for key in keys)


def _recompute_ctp_latest_fact(
    db_path: Path | str,
    *,
    event_date: str,
    slug: str = "",
    reason: str,
) -> Dict[str, Any]:
    if not _truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_RECOMPUTE_CTP_INLINE"), True):
        return {"ok": True, "status": "disabled", "reason": "env_disabled"}
    started_mono = time.monotonic()
    recompute_started_at = _utc_now_iso()
    event_key = _normalize_date(event_date) or _chengdu_today()
    db = Path(db_path)
    try:
        conn_ctx = _connect(db)
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "status": "latest_fact_missing",
            "event_date": event_key,
            "error": f"{exc.__class__.__name__}: {exc}",
            "retryable": True,
        }
    with conn_ctx as conn:
        try:
            latest = _load_latest_fact(conn, event_date=event_key, slug=slug)
        except sqlite3.Error as exc:
            return {
                "ok": False,
                "status": "latest_fact_missing",
                "event_date": event_key,
                "error": f"{exc.__class__.__name__}: {exc}",
                "retryable": True,
            }
        if latest is None:
            return {"ok": False, "status": "latest_fact_missing", "event_date": event_key}
        payload = _json_loads(latest["fact_json"])
        if not payload:
            return {"ok": False, "status": "latest_fact_json_invalid", "event_date": event_key}
        before_probs = _payload_ctp_bucket_probabilities(payload)
        materialized, meta = _materialize_ctp_payload(payload)
        if not bool(meta.get("ok")):
            return {
                "ok": False,
                "status": "ctp_materialize_failed",
                "event_date": event_key,
                "slug": str(latest["slug"] or slug or payload.get("slug") or ""),
                "meta": dict(meta),
            }
        after_probs = _payload_ctp_bucket_probabilities(materialized)
        changed = _ctp_probability_changed(before_probs, after_probs)
        if before_probs and after_probs and not changed and _truthy(
            os.environ.get("PM_POSTERIOR_TARGET_DIRECT_SKIP_UNCHANGED_CTP_FACT"),
            True,
        ):
            return {
                "ok": True,
                "status": "unchanged",
                "event_date": event_key,
                "slug": str(latest["slug"] or slug or payload.get("slug") or ""),
                "fact_id": int(latest["id"] or 0),
                "reason": reason,
                "bucket_count": len(after_probs),
            }
        recomputed_at = _utc_now_iso()
        item = dict(materialized)
        item["slug"] = str(latest["slug"] or item.get("slug") or slug or "")
        item["event_date"] = _normalize_date(item.get("event_date") or latest["event_date"] or event_key)
        item["run_date"] = _normalize_date(item.get("run_date") or latest["run_date"] or event_key)
        item["posterior_target_direct_inline_ctp_recomputed_at"] = recomputed_at
        item["posterior_target_direct_inline_ctp_recompute_reason"] = str(reason or "source_fact_updated")
        item["posterior_target_direct_source_fact_id"] = int(latest["id"] or 0)
        item["posterior_target_direct_source_fact_as_of"] = str(latest["fact_as_of"] or "")
        item["posterior_target_direct_source_fact_to_ctp_recompute_ms"] = _latency_ms_between(
            str(latest["fact_as_of"] or ""),
            recomputed_at,
        )
        item["posterior_target_direct_ctp_recompute_ms"] = round(
            max(0.0, time.monotonic() - started_mono) * 1000.0,
            3,
        )
        provenance = dict(item.get("fact_provenance") or {})
        provenance["posterior_target_direct_inline_ctp_recompute"] = {
            "source": "hard_source_direct_poller",
            "reason": str(reason or "source_fact_updated"),
            "source_fact_id": int(latest["id"] or 0),
            "source_fact_as_of": str(latest["fact_as_of"] or ""),
            "recomputed_at": recomputed_at,
        }
        fast_trade_write = _truthy(
            os.environ.get("PM_POSTERIOR_TARGET_DIRECT_RECOMPUTE_FAST_TRADE_WRITE"),
            True,
        )
        if fast_trade_write:
            item["posterior_target_direct_fast_trade_write"] = True
            provenance["posterior_target_direct_fast_trade_write_requested"] = {
                "source": "hard_source_direct_poller",
                "reason": str(reason or "source_fact_updated"),
                "ui_rich_fields_deferred": True,
            }
        item["fact_provenance"] = provenance
        fact_id = _insert_fact_snapshot(conn, latest_row=latest, item=item, fact_as_of=recomputed_at)
        summary_cache_ok = False
        summary_cache_checked = False
        summary_cache_write_error = ""
        if fact_id:
            if fast_trade_write:
                summary_cache_checked = True
                summary_cache_ok = _event_fact_summary_cache_exists(conn, int(fact_id))
            else:
                try:
                    from scripts.materialize_ctp_latest_facts import _upsert_summary_cache

                    row = conn.execute("SELECT * FROM event_fact_history WHERE id = ?", (int(fact_id),)).fetchone()
                    if row is not None:
                        summary_cache_ok = bool(
                            _upsert_summary_cache(
                                conn,
                                fact_id=int(fact_id),
                                row=row,
                                payload=item,
                                created_at=recomputed_at,
                            )
                        )
                        conn.commit()
                except Exception as exc:
                    summary_cache_write_error = f"{exc.__class__.__name__}: {exc}"
                    summary_cache_ok = False
                summary_cache_checked = True
                if not summary_cache_ok:
                    summary_cache_ok = _event_fact_summary_cache_exists(conn, int(fact_id))
        return {
            "ok": bool(fact_id),
            "status": "recomputed" if fact_id else "insert_failed",
            "event_date": event_key,
            "slug": item.get("slug"),
            "source_fact_id": int(latest["id"] or 0),
            "fact_id": int(fact_id or 0) if fact_id else None,
            "reason": reason,
            "probability_changed": bool(changed),
            "bucket_count": len(after_probs),
            "source_fact_to_ctp_recompute_ms": item.get(
                "posterior_target_direct_source_fact_to_ctp_recompute_ms"
            ),
            "ctp_recompute_ms": item.get("posterior_target_direct_ctp_recompute_ms"),
            "summary_cache_ok": bool(summary_cache_ok),
            "summary_cache_checked": bool(summary_cache_checked),
            "summary_cache_write_error": summary_cache_write_error,
            "meta": dict(meta),
            "started_at": recompute_started_at,
            "recomputed_at": recomputed_at,
        }


def _refresh_result_updated(result: Mapping[str, Any]) -> bool:
    if not isinstance(result, Mapping):
        return False
    status = str(result.get("status") or "").strip().lower()
    if status in {"updated", "recorded", "inserted", "refreshed"}:
        return True
    return bool(result.get("fact_id") or result.get("ssot_fact_id"))


def _awc_release_watch_interval(now_utc: Optional[datetime] = None) -> Optional[float]:
    if not _truthy(os.environ.get("PM_HARD_SOURCE_AWC_RELEASE_WATCH_ENABLED"), True):
        return None
    window_min = max(0.0, _env_float("PM_HARD_SOURCE_AWC_RELEASE_WATCH_WINDOW_MIN", 8.0))
    if window_min <= 0:
        return None
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    minute_offset = float(current.minute) + float(current.second) / 60.0 + float(current.microsecond) / 60_000_000.0
    if minute_offset <= window_min:
        return max(0.2, _env_float("PM_HARD_SOURCE_AWC_RELEASE_WATCH_POLL_SEC", 1.0))
    return None


def _awc_refresh_hours(event_date: str = "", now_utc: Optional[datetime] = None) -> int:
    max_hours = max(1, _env_int("PM_HARD_SOURCE_AWC_HOURS", 48))
    if not _truthy(os.environ.get("PM_HARD_SOURCE_AWC_DYNAMIC_HOURS_ENABLED"), True):
        return int(max_hours)
    min_hours = max(1, _env_int("PM_HARD_SOURCE_AWC_DYNAMIC_MIN_HOURS", 8))
    margin_hours = max(0, _env_int("PM_HARD_SOURCE_AWC_DYNAMIC_MARGIN_HOURS", 2))
    event_key = _normalize_date(event_date)
    zone: timezone = timezone.utc
    if ZoneInfo is not None:
        try:
            zone = ZoneInfo("Asia/Shanghai")  # type: ignore[assignment]
        except Exception:
            zone = timezone.utc
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(zone)
    local_today = local_now.date().isoformat()
    if event_key and event_key > local_today:
        return int(min(max_hours, min_hours))
    if event_key and event_key == local_today:
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_hours = max(0.0, (local_now - local_start).total_seconds() / 3600.0)
        dynamic_hours = int(math.ceil(elapsed_hours + float(margin_hours)))
        return int(min(max_hours, max(min_hours, dynamic_hours)))
    return int(max_hours)


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _refresh_awc_fact(
    db_path: Path,
    *,
    event_date: str,
    hours: int,
    heartbeat_sec: int,
) -> Dict[str, Any]:
    previous = os.environ.get("PM_AWC_FACT_REFRESH_HEARTBEAT_SEC")
    write_heartbeat = _env_int("PM_HARD_SOURCE_AWC_FACT_WRITE_HEARTBEAT_SEC", 0)
    if write_heartbeat is None:
        write_heartbeat = 0
    os.environ["PM_AWC_FACT_REFRESH_HEARTBEAT_SEC"] = str(max(0, int(write_heartbeat)))
    try:
        from ui.server import refresh_awc_fact_for_chengdu_event

        try:
            with _temporary_env(
                {
                    "PM_FACT_REFRESH_WRITE_RETRY_ATTEMPTS": str(
                        max(1, _env_int("PM_HARD_SOURCE_AWC_WRITE_RETRY_ATTEMPTS", 1))
                    ),
                    "PM_FACT_REFRESH_WRITE_RETRY_SLEEP_SEC": str(
                        max(0.05, _env_float("PM_HARD_SOURCE_AWC_WRITE_RETRY_SLEEP_SEC", 0.05))
                    ),
                }
            ):
                return dict(
                    refresh_awc_fact_for_chengdu_event(
                        db_path,
                        event_date=event_date,
                        hours=hours,
                        connect_timeout_sec=max(
                            0.05,
                            _env_float("PM_HARD_SOURCE_AWC_DB_CONNECT_TIMEOUT_SEC", 0.6),
                        ),
                        busy_timeout_ms=max(
                            0,
                            _env_int("PM_HARD_SOURCE_AWC_DB_BUSY_TIMEOUT_MS", 750),
                        ),
                        include_cross_checks=_truthy(
                            os.environ.get("PM_HARD_SOURCE_AWC_INLINE_CROSS_CHECKS"),
                            False,
                        ),
                    )
                    or {}
                )
        except Exception as exc:
            return {
                "ok": False,
                "status": "awc_refresh_failed",
                "event_date": _normalize_date(event_date),
                "error": f"{exc.__class__.__name__}: {exc}",
                "retryable": isinstance(exc, sqlite3.OperationalError),
            }
    finally:
        if previous is None:
            os.environ.pop("PM_AWC_FACT_REFRESH_HEARTBEAT_SEC", None)
        else:
            os.environ["PM_AWC_FACT_REFRESH_HEARTBEAT_SEC"] = previous


def _refresh_wu_twc_fact(
    db_path: Path,
    *,
    event_date: str,
    slug: str = "",
    heartbeat_sec: int,
) -> Dict[str, Any]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    with _connect(db_path) as conn:
        latest = _load_latest_fact(conn, event_date=event_key, slug=slug)
        if latest is None:
            return {"ok": False, "status": "latest_fact_missing", "event_date": event_key}
        payload = _json_loads(latest["fact_json"])
        if not payload:
            return {"ok": False, "status": "latest_fact_json_invalid", "event_date": event_key}
        payload = _merge_latest_awc_hard_observation_into_payload(
            conn,
            payload,
            slug=str(latest["slug"] or slug or payload.get("slug") or ""),
            event_date=event_key,
        )
        existing = _features(payload)
        errors: List[str] = []
        source_features: Dict[str, Any] = {}
        try:
            pws_owned_by_multi_source_lane = _truthy(os.environ.get("PM_HARD_SOURCE_METAR_PWS_ENABLED"), False)
            pws_env = {"PM_TWC_PWS_RAPID_HISTORY_ENABLED": "0"} if pws_owned_by_multi_source_lane else {}
            with _temporary_env(pws_env):
                observed_features = swm.fetch_wu_twc_observed_intraday_features(
                    payload.get("resolution_source"),
                    payload.get("station_id") or "ZUUU",
                    event_key,
                    payload.get("market_var"),
                    payload.get("market_unit") or "C",
                    tz=payload.get("event_tz") or payload.get("tz") or "Asia/Shanghai",
                    reference_observed_at=existing.get("zuuu_metar_raw_awc_latest_at"),
                    rounding_rule=payload.get("rounding_rule") or "whole_degrees",
                    provenance={},
                )
            if observed_features:
                source_features.update(observed_features)
        except Exception as exc:
            errors.append(f"wu_twc_observed:{exc.__class__.__name__}: {exc}")
        if _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_REFRESH_WU_FORECAST"), True):
            try:
                wu_forecast = swm.fetch_wunderground_forecast_peak_metrics(
                    payload.get("resolution_source"),
                    event_key,
                    payload.get("market_var"),
                    payload.get("market_unit") or "C",
                )
                wu_peak_c = _to_float(wu_forecast.get("value_c")) if isinstance(wu_forecast, Mapping) else None
                if isinstance(wu_forecast, Mapping) and wu_forecast.get("ok") and wu_peak_c is not None:
                    source_features.update(
                        {
                            "wu_forecast_peak_c": float(wu_peak_c),
                            "wu_forecast_peak_value": wu_forecast.get("value"),
                            "wu_forecast_peak_unit": wu_forecast.get("unit"),
                            "wu_forecast_peak_at": wu_forecast.get("peak_at"),
                            "wu_forecast_peak_time_source": wu_forecast.get("peak_time_source"),
                            "wu_forecast_peak_source": wu_forecast.get("source"),
                            "wu_forecast_peak_source_fit_tier": wu_forecast.get("source_fit_tier"),
                            "wu_forecast_peak_source_trust_tier": wu_forecast.get("source_trust_tier"),
                            "wu_forecast_peak_usable_for_settlement": wu_forecast.get("source_usable_for_settlement"),
                            "twc_provider": "Weather.com/TWC",
                            "twc_provider_role": "weather_api_provider_wrapper_not_sensor_origin",
                        }
                    )
                    _copy_peak_plateau_features(
                        source_features,
                        prefix="wu_forecast",
                        metrics=wu_forecast,
                        canonical=True,
                    )
                    if (
                        not source_features.get("expected_peak_hour_distribution")
                        and wu_forecast.get("expected_peak_hour_distribution_na_reason")
                    ):
                        _clear_canonical_peak_plateau_features(
                            source_features,
                            reason=str(wu_forecast.get("expected_peak_hour_distribution_na_reason") or "wu_hourly_peak_plateau_unavailable"),
                        )
                elif isinstance(wu_forecast, Mapping) and wu_forecast.get("error"):
                    errors.append(f"wu_forecast:{wu_forecast.get('error')}")
            except Exception as exc:
                errors.append(f"wu_forecast:{exc.__class__.__name__}: {exc}")
        if _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_REFRESH_TWC_FORECAST"), True):
            try:
                forecast_lat, forecast_lon = _forecast_geocode(payload)
                twc_hourly_for_plateau: Dict[str, Any] = {}
                twc_hourly = swm.fetch_weather_com_hourly_peak_metrics(
                    forecast_lat,
                    forecast_lon,
                    event_key,
                    payload.get("market_var"),
                    payload.get("market_unit") or "C",
                    station_id=payload.get("station_id") or "ZUUU",
                    resolution_source=payload.get("resolution_source"),
                )
                twc_hourly_peak_c = _to_float(twc_hourly.get("value_c")) if isinstance(twc_hourly, Mapping) else None
                if isinstance(twc_hourly, Mapping) and twc_hourly.get("ok") and twc_hourly_peak_c is not None:
                    source_features.update(
                        {
                            "twc_forecast_hourly_peak_c": float(twc_hourly_peak_c),
                        }
                    )
                    twc_hourly_for_plateau = dict(twc_hourly)
                elif isinstance(twc_hourly, Mapping) and twc_hourly.get("error"):
                    errors.append(f"twc_hourly_forecast:{twc_hourly.get('error')}")
                twc_forecast = swm.fetch_weather_com_forecast_peak_metrics(
                    forecast_lat,
                    forecast_lon,
                    event_key,
                    payload.get("market_var"),
                    payload.get("market_unit") or "C",
                    station_id=payload.get("station_id") or "ZUUU",
                    resolution_source=payload.get("resolution_source"),
                )
                twc_peak_c = _to_float(twc_forecast.get("value_c")) if isinstance(twc_forecast, Mapping) else None
                if isinstance(twc_forecast, Mapping) and twc_forecast.get("ok") and twc_peak_c is not None:
                    source_features.update(
                        {
                            "weather_com_forecast_peak_c": float(twc_peak_c),
                            "weather_com_forecast_peak_value": twc_forecast.get("value"),
                            "weather_com_forecast_peak_unit": twc_forecast.get("unit"),
                            "weather_com_forecast_peak_source": twc_forecast.get("source"),
                            "weather_com_forecast_peak_source_url": twc_forecast.get("source_url"),
                            "weather_com_forecast_peak_valid_local": twc_forecast.get("forecast_valid_local"),
                            "twc_forecast_peak_c": float(twc_peak_c),
                            "twc_forecast_peak_value": twc_forecast.get("value"),
                            "twc_forecast_peak_unit": twc_forecast.get("unit"),
                            "twc_forecast_peak_source": twc_forecast.get("source"),
                            "twc_forecast_peak_source_url": twc_forecast.get("source_url"),
                            "twc_forecast_peak_valid_local": twc_forecast.get("forecast_valid_local"),
                            "twc_provider": "Weather.com/TWC",
                            "twc_provider_role": "weather_api_provider_wrapper_not_sensor_origin",
                        }
                    )
                    if twc_hourly_for_plateau:
                        _copy_peak_plateau_features(
                            source_features,
                            prefix="weather_com_forecast",
                            metrics=twc_hourly_for_plateau,
                            canonical=True,
                            reference_peak_c=float(twc_peak_c),
                        )
                        _copy_peak_plateau_features(
                            source_features,
                            prefix="twc_forecast",
                            metrics=twc_hourly_for_plateau,
                            canonical=False,
                            reference_peak_c=float(twc_peak_c),
                        )
                        if source_features.get("expected_peak_hour_distribution"):
                            source_features["weather_com_forecast_peak_at"] = twc_hourly_for_plateau.get("peak_at")
                            source_features["weather_com_forecast_peak_time_source"] = twc_hourly_for_plateau.get("source")
                            source_features["twc_forecast_peak_at"] = twc_hourly_for_plateau.get("peak_at")
                            source_features["twc_forecast_peak_time_source"] = twc_hourly_for_plateau.get("source")
                elif isinstance(twc_forecast, Mapping) and twc_forecast.get("error"):
                    errors.append(f"twc_forecast:{twc_forecast.get('error')}")
            except Exception as exc:
                errors.append(f"twc_forecast:{exc.__class__.__name__}: {exc}")
        if not source_features:
            return {
                "ok": False,
                "status": "wu_twc_features_missing",
                "event_date": event_key,
                "errors": errors,
            }
        existing_at = str(existing.get("wu_twc_observed_intraday_today_max_at") or "")
        incoming_at = str(source_features.get("wu_twc_observed_intraday_today_max_at") or "")
        existing_max = _to_float(existing.get("wu_twc_observed_intraday_today_max_c"))
        incoming_max = _to_float(source_features.get("wu_twc_observed_intraday_today_max_c"))
        existing_fetch = str(existing.get("wu_twc_observed_intraday_fetched_at") or "")
        incoming_fetch = str(source_features.get("wu_twc_observed_intraday_fetched_at") or _utc_now_iso())
        existing_wu_peak_at = str(existing.get("wu_forecast_peak_at") or "")
        incoming_wu_peak_at = str(source_features.get("wu_forecast_peak_at") or "")
        existing_twc_peak_at = str(existing.get("twc_forecast_peak_at") or existing.get("weather_com_forecast_peak_at") or "")
        incoming_twc_peak_at = str(source_features.get("twc_forecast_peak_at") or source_features.get("weather_com_forecast_peak_at") or "")
        existing_peak_distribution = json.dumps(
            existing.get("expected_peak_hour_distribution") or [],
            sort_keys=True,
            ensure_ascii=True,
        )
        incoming_peak_distribution = json.dumps(
            source_features.get("expected_peak_hour_distribution") or [],
            sort_keys=True,
            ensure_ascii=True,
        )
        existing_peak_window = (
            _to_float(existing.get("expected_peak_window_start_hour_local")),
            _to_float(existing.get("expected_peak_window_end_hour_local")),
        )
        incoming_peak_window = (
            _to_float(source_features.get("expected_peak_window_start_hour_local")),
            _to_float(source_features.get("expected_peak_window_end_hour_local")),
        )
        reason = ""
        if incoming_at and incoming_at != existing_at:
            reason = "wu_twc_today_max_at_changed"
        elif incoming_max is not None and (existing_max is None or abs(incoming_max - existing_max) > 1e-9):
            reason = "wu_twc_today_max_changed"
        elif incoming_wu_peak_at and incoming_wu_peak_at != existing_wu_peak_at:
            reason = "wu_forecast_peak_at_changed"
        elif incoming_twc_peak_at and incoming_twc_peak_at != existing_twc_peak_at:
            reason = "twc_forecast_peak_at_changed"
        elif (
            incoming_peak_distribution not in {"[]", "{}"}
            and incoming_peak_distribution != existing_peak_distribution
        ):
            reason = "expected_peak_hour_distribution_changed"
        elif (
            _truthy(source_features.get("_clear_expected_peak_hour_distribution"), False)
            and existing_peak_distribution not in {"[]", "{}"}
        ):
            reason = "expected_peak_hour_distribution_cleared"
        elif (
            incoming_peak_window[0] is not None
            and incoming_peak_window[1] is not None
            and incoming_peak_window != existing_peak_window
        ):
            reason = "expected_peak_window_changed"
        elif not existing_fetch:
            reason = "wu_twc_fetched_at_missing"
        elif heartbeat_sec > 0:
            try:
                incoming_dt = datetime.fromisoformat(incoming_fetch.replace("Z", "+00:00"))
                existing_dt = datetime.fromisoformat(existing_fetch.replace("Z", "+00:00"))
                if (incoming_dt - existing_dt).total_seconds() >= heartbeat_sec:
                    reason = "wu_twc_fetch_heartbeat"
            except Exception:
                reason = ""
        if not reason:
            return {
                "ok": True,
                "status": "skipped",
                "reason": "wu_twc_observation_unchanged",
                "event_date": event_key,
                "wu_twc_today_max_c": incoming_max,
            }
        refreshed_at = incoming_fetch or _utc_now_iso()
        item = _merge_source_features_into_payload(
            payload,
            source_features,
            source_id="wu_twc_observed_intraday",
            refreshed_at=refreshed_at,
        )
        final_settlement_signal = _final_settlement_signal_from_wu_twc_features(
            source_features,
            refreshed_at=refreshed_at,
            rounding_rule=payload.get("rounding_rule") or "whole_degrees",
            settlement_unit=payload.get("market_unit") or "C",
        )
        if final_settlement_signal:
            item = _merge_final_settlement_signal_into_payload(item, final_settlement_signal)
        item["slug"] = str(latest["slug"] or item.get("slug") or "")
        item["event_date"] = event_key
        item["run_date"] = _normalize_date(latest["run_date"] or item.get("run_date") or event_key)
        fact_id = _insert_fact_snapshot(conn, latest_row=latest, item=item, fact_as_of=refreshed_at)
        return {
            "ok": bool(fact_id),
            "status": "updated" if fact_id else "insert_failed",
            "reason": reason,
            "event_date": event_key,
            "slug": item["slug"],
            "fact_id": fact_id,
            "fact_as_of": refreshed_at,
            "wu_twc_today_max_c": incoming_max,
            "fetch_duration_ms": source_features.get("wu_twc_observed_intraday_fetch_duration_ms"),
        }


def _metar_pws_source_interval(
    source_key: str,
    *,
    window_allowed: bool,
    peak_default: int,
    normal_default: int,
) -> int:
    env_prefix = str(source_key or "").strip().upper()
    if not env_prefix:
        return int(peak_default if window_allowed else normal_default)
    mode_name = "PEAK" if window_allowed else "NORMAL"
    fallback = int(peak_default if window_allowed else normal_default)
    return max(
        0,
        _env_int_first(
            (
                f"PM_HARD_SOURCE_{env_prefix}_POLL_SEC",
                f"PM_HARD_SOURCE_{env_prefix}_{mode_name}_POLL_SEC",
                f"PM_HARD_SOURCE_{env_prefix}_NORMAL_POLL_SEC",
            ),
            fallback,
        ),
    )


def _metar_pws_source_specs(*, window_allowed: bool) -> List[Dict[str, Any]]:
    return [
        {
            "key": "iem",
            "stamp": "metar_iem_noaaport",
            "feature_prefix": "zuuu_metar_iem_noaaport",
            "enabled": _truthy(os.environ.get("PM_METAR_CROSS_CHECK_IEM_ENABLED"), True),
            "interval_sec": _metar_pws_source_interval(
                "METAR_IEM",
                window_allowed=window_allowed,
                peak_default=30,
                normal_default=300,
            ),
        },
        {
            "key": "avwx",
            "stamp": "metar_avwx",
            "feature_prefix": "zuuu_metar_avwx",
            "enabled": _truthy(os.environ.get("PM_METAR_CROSS_CHECK_AVWX_ENABLED"), True),
            "interval_sec": _metar_pws_source_interval(
                "METAR_AVWX",
                window_allowed=window_allowed,
                peak_default=15,
                normal_default=60,
            ),
        },
        {
            "key": "checkwx",
            "stamp": "metar_checkwx",
            "feature_prefix": "zuuu_metar_checkwx",
            "enabled": _truthy(os.environ.get("PM_METAR_CROSS_CHECK_CHECKWX_ENABLED"), True),
            "interval_sec": _metar_pws_source_interval(
                "METAR_CHECKWX",
                window_allowed=window_allowed,
                peak_default=15,
                normal_default=60,
            ),
        },
        {
            "key": "twc_pws_rapid",
            "stamp": "twc_pws_rapid",
            "feature_prefix": "twc_pws_rapid_history_aux",
            "enabled": _truthy(os.environ.get("PM_TWC_PWS_RAPID_HISTORY_ENABLED"), True),
            "interval_sec": _metar_pws_source_interval(
                "TWC_PWS_RAPID",
                window_allowed=window_allowed,
                peak_default=10,
                normal_default=60,
            ),
        },
    ]


def _metar_pws_due_sources(event_date: str, *, window_allowed: bool) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    due_specs: List[Dict[str, Any]] = []
    statuses: List[Dict[str, Any]] = []
    for spec in _metar_pws_source_specs(window_allowed=window_allowed):
        item = dict(spec)
        due = False
        if item.get("enabled"):
            due = _refresh_stamp_due(
                f"metar_pws_{item['stamp']}",
                event_key,
                interval_sec=int(item.get("interval_sec") or 0),
            )
        item["due"] = bool(due)
        statuses.append(
            {
                "key": item.get("key"),
                "feature_prefix": item.get("feature_prefix"),
                "enabled": bool(item.get("enabled")),
                "due": bool(due),
                "interval_sec": int(item.get("interval_sec") or 0),
            }
        )
        if due:
            due_specs.append(item)
    return due_specs, statuses


def _source_unavailable_features(prefix: str, *, station: str, fetched_at: str, error: str) -> Dict[str, Any]:
    provider_by_prefix = {
        "zuuu_metar_iem_noaaport": "IEM ASOS/METAR NOAAPort archive",
        "zuuu_metar_avwx": "AVWX REST",
        "zuuu_metar_checkwx": "CheckWX",
        "twc_pws_rapid_history_aux": "Weather.com/TWC PWS",
    }
    role = "auxiliary_intraday_warning_not_final_settlement" if prefix == "twc_pws_rapid_history_aux" else "METAR cross_check"
    return {
        f"{prefix}_provider": provider_by_prefix.get(prefix, prefix),
        f"{prefix}_role": role,
        f"{prefix}_station_id": station,
        f"{prefix}_available": False,
        f"{prefix}_fetched_at": fetched_at,
        f"{prefix}_error": error,
    }


def _refresh_metar_pws_multi_source_fact(
    db_path: Path,
    *,
    event_date: str,
    slug: str = "",
    window_allowed: bool = False,
) -> Dict[str, Any]:
    if not _truthy(os.environ.get("PM_METAR_CROSS_CHECK_ENABLED"), True) and not _truthy(
        os.environ.get("PM_TWC_PWS_RAPID_HISTORY_ENABLED"),
        True,
    ):
        return {"ok": True, "status": "skipped", "reason": "all_metar_pws_sources_disabled"}
    event_key = _normalize_date(event_date) or _chengdu_today()
    due_specs, source_statuses = _metar_pws_due_sources(event_key, window_allowed=bool(window_allowed))
    if not due_specs:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "not_due",
            "event_date": event_key,
            "sources": source_statuses,
        }
    with _connect(db_path) as conn:
        latest = _load_latest_fact(conn, event_date=event_key, slug=slug)
        if latest is None:
            return {
                "ok": False,
                "status": "latest_fact_missing",
                "event_date": event_key,
                "sources": source_statuses,
            }
        payload = _json_loads(latest["fact_json"])
        if not payload:
            return {
                "ok": False,
                "status": "latest_fact_json_invalid",
                "event_date": event_key,
                "sources": source_statuses,
            }
        payload = _merge_latest_awc_hard_observation_into_payload(
            conn,
            payload,
            slug=str(latest["slug"] or slug or payload.get("slug") or ""),
            event_date=event_key,
        )
        station = str(payload.get("station_id") or "ZUUU").strip().upper() or "ZUUU"
        tz_name = str(payload.get("event_tz") or payload.get("tz") or "Asia/Shanghai").strip() or "Asia/Shanghai"
        resolution_source = payload.get("resolution_source")
        existing = _features(payload)
        source_features: Dict[str, Any] = {}
        fetched_at = _utc_now_iso()
        errors: List[str] = []
        status_by_key = {str(item.get("key") or ""): dict(item) for item in source_statuses}
        for spec in due_specs:
            key = str(spec.get("key") or "")
            prefix = str(spec.get("feature_prefix") or "")
            started_at = time.time()
            try:
                if key == "iem":
                    features = swm._fetch_iem_noaaport_metar_cross_check_features(
                        station,
                        target_event_date=event_key,
                        tz=tz_name,
                        hours=_env_int("PM_HARD_SOURCE_METAR_IEM_HOURS", 48),
                    )
                elif key == "avwx":
                    features = swm._fetch_avwx_metar_cross_check_features(
                        station,
                        target_event_date=event_key,
                        tz=tz_name,
                    )
                elif key == "checkwx":
                    features = swm._fetch_checkwx_metar_cross_check_features(
                        station,
                        target_event_date=event_key,
                        tz=tz_name,
                    )
                elif key == "twc_pws_rapid":
                    features = swm.fetch_twc_pws_rapid_history_aux_features(
                        resolution_source,
                        station,
                        event_key,
                        tz=tz_name,
                    )
                else:
                    features = {}
                if not isinstance(features, Mapping):
                    features = {}
                if not features and prefix:
                    features = _source_unavailable_features(
                        prefix,
                        station=station,
                        fetched_at=_utc_now_iso(),
                        error="features_missing",
                    )
            except Exception as exc:
                errors.append(f"{key}:{exc.__class__.__name__}: {exc}")
                features = _source_unavailable_features(
                    prefix,
                    station=station,
                    fetched_at=_utc_now_iso(),
                    error=f"poll_error:{exc.__class__.__name__}",
                )
            elapsed_ms = round(max(0.0, time.time() - started_at) * 1000.0, 3)
            source_features.update(dict(features))
            if prefix:
                source_features.setdefault(f"{prefix}_fetch_duration_ms", elapsed_ms)
                status = status_by_key.get(key)
                if status is not None:
                    status["available"] = bool(source_features.get(f"{prefix}_available"))
                    status["error"] = source_features.get(f"{prefix}_error") or ""
                    status["fetched_at"] = source_features.get(f"{prefix}_fetched_at") or ""
                    status["latest_at"] = source_features.get(f"{prefix}_latest_at") or ""
                    status["today_max_c"] = source_features.get(f"{prefix}_today_max_c")
        combined_features = dict(existing)
        combined_features.update(source_features)
        if any(key.startswith("zuuu_metar_") for key in source_features):
            try:
                summary = swm._build_metar_cross_check_summary(combined_features)
                source_features["metar_cross_check_summary"] = summary
                source_features["metar_cross_check_sources"] = summary.get("sources")
            except Exception as exc:
                errors.append(f"metar_cross_check_summary:{exc.__class__.__name__}: {exc}")
        refreshed_at = str(
            source_features.get("twc_pws_rapid_history_aux_fetched_at")
            or source_features.get("zuuu_metar_avwx_fetched_at")
            or source_features.get("zuuu_metar_checkwx_fetched_at")
            or source_features.get("zuuu_metar_iem_noaaport_fetched_at")
            or fetched_at
        )
        item = _merge_source_features_into_payload(
            payload,
            source_features,
            source_id="metar_pws_multi_source",
            refreshed_at=refreshed_at,
        )
        item = _merge_metar_pws_lineage(item, source_features)
        item["slug"] = str(latest["slug"] or item.get("slug") or "")
        item["event_date"] = event_key
        item["run_date"] = _normalize_date(latest["run_date"] or item.get("run_date") or event_key)
        fact_id = _insert_fact_snapshot(conn, latest_row=latest, item=item, fact_as_of=refreshed_at)
    next_statuses = [status_by_key.get(str(item.get("key") or ""), item) for item in source_statuses]
    return {
        "ok": bool(fact_id),
        "status": "updated" if fact_id else "insert_failed",
        "event_date": event_key,
        "slug": item.get("slug") if "item" in locals() else "",
        "fact_id": fact_id,
        "fact_as_of": refreshed_at,
        "sources": next_statuses,
        "errors": errors,
    }


def _bucket_label_for_value(payload: Mapping[str, Any], value_c: float) -> str:
    rows = _rows(payload)
    rounding_rule = str(payload.get("rounding_rule") or "").strip() or None
    try:
        label = swm._bucket_label_for_value(rows, float(value_c), rounding_rule)
    except Exception:
        label = ""
    if label:
        return label
    if rounding_rule == "whole_degrees":
        return f"{int(math.floor(float(value_c)))}\u00b0C"
    return f"{int(round(float(value_c)))}\u00b0C"


def _hard_observation_from_payload(
    payload: Mapping[str, Any],
    *,
    allow_wu_twc_fallback: bool,
) -> Dict[str, Any]:
    features = _features(payload)
    candidates: List[Dict[str, Any]] = []
    wu_twc_aux_seen = False
    actual = _to_float(features.get("settlement_actual") or payload.get("settlement_actual"))
    if actual is not None:
        source_id = str(
            features.get("settlement_actual_source")
            or payload.get("settlement_actual_source")
            or features.get("settlement_signal_source")
            or payload.get("settlement_signal_source")
            or "settlement_actual"
        ).strip()
        observed_at = str(
            features.get("settlement_actual_at")
            or features.get("settlement_final_at")
            or features.get("settlement_signal_created_at")
            or payload.get("settlement_signal_created_at")
            or ""
        )
        candidates.append(
            {
                "ok": True,
                "source_id": source_id or "settlement_actual",
                "value_c": actual,
                "observed_at": observed_at,
                "fetched_at": str(features.get("settlement_actual_fetched_at") or observed_at),
                "fetch_duration_ms": _to_float(features.get("settlement_actual_fetch_duration_ms")),
                "raw": str(features.get("settlement_actual_raw") or ""),
                "priority": 1000,
                "final_bucket_lock": True,
            }
        )
    awc_max = _to_float(features.get("zuuu_metar_raw_awc_today_max_c"))
    awc_mismatch = bool(features.get("zuuu_metar_raw_awc_date_mismatch"))
    if awc_max is not None and not awc_mismatch:
        candidates.append(
            {
                "ok": True,
                "source_id": "zuuu_metar_raw_awc",
                "value_c": awc_max,
                "observed_at": str(features.get("zuuu_metar_raw_awc_today_max_at") or features.get("zuuu_metar_raw_awc_latest_at") or ""),
                "fetched_at": str(features.get("zuuu_metar_raw_awc_fetched_at") or ""),
                "fetch_duration_ms": _to_float(features.get("zuuu_metar_raw_awc_fetch_duration_ms")),
                "raw": str(features.get("zuuu_metar_raw_awc_today_max_raw") or features.get("zuuu_metar_raw_awc_latest_raw") or ""),
                "priority": 20,
            }
        )
    wu_max = _to_float(features.get("wu_twc_observed_intraday_today_max_c"))
    if allow_wu_twc_fallback and wu_max is not None and not bool(features.get("wu_twc_observed_intraday_stale")):
        wu_twc_aux_seen = True
    if candidates:
        candidates.sort(key=lambda item: (float(item.get("value_c") or float("-inf")), int(item.get("priority") or 0)), reverse=True)
        return candidates[0]
    return {
        "ok": False,
        "source_id": "",
        "reason": "wu_twc_intraday_auxiliary_not_hard_source" if wu_twc_aux_seen else "hard_observation_missing",
        "awc_date_mismatch": awc_mismatch,
    }


def _row_for_outcome(payload: Mapping[str, Any], outcome: str) -> Dict[str, Any]:
    for row in _rows(payload):
        if str(row.get("outcome") or "").strip() == str(outcome or "").strip():
            return row
    return {}


def _latest_market_tick(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str,
) -> Dict[str, Any]:
    try:
        row = conn.execute(
            """
            SELECT best_bid, best_ask, spread, bid_depth_topN, ask_depth_topN, created_at
            FROM event_market_ticks
            WHERE slug = ? AND event_date = ? AND outcome = ? AND side = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (slug, event_date, outcome, side),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return {}
    return {
        "best_bid": _to_float(row["best_bid"]),
        "best_ask": _to_float(row["best_ask"]),
        "spread": _to_float(row["spread"]),
        "bid_depth_topN": _to_float(row["bid_depth_topN"]),
        "ask_depth_topN": _to_float(row["ask_depth_topN"]),
        "created_at": str(row["created_at"] or ""),
    }


def _market_token(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
) -> Dict[str, Any]:
    try:
        row = conn.execute(
            """
            SELECT yes_token, no_token, market_active, market_closed,
                   market_archived, market_accepting_orders, market_enable_orderbook
            FROM market_tokens
            WHERE slug = ? AND event_date = ? AND outcome = ?
            LIMIT 1
            """,
            (slug, event_date, outcome),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return {}
    return {key: row[key] for key in row.keys()}


def _last_hard_source_entry(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> Dict[str, Any]:
    try:
        row = conn.execute(
            """
            SELECT outcome, details_json, created_at
            FROM auto_trade_decisions
            WHERE slug = ?
              AND substr(event_date, 1, 10) = ?
              AND action = 'entry'
              AND (
                    reason_codes_json LIKE '%hard_source_direct%'
                    OR reason_codes_json LIKE '%hard_source_posterior_constraint%'
                  )
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (slug, event_date),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return {}
    details = _json_loads(row["details_json"])
    return {
        "outcome": str(row["outcome"] or ""),
        "created_at": str(row["created_at"] or ""),
        "details": details,
    }


def _recent_same_entry_exists(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    within_sec: int,
) -> bool:
    if within_sec <= 0:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_sec)).isoformat(timespec="seconds")
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM auto_trade_decisions
            WHERE slug = ?
              AND substr(event_date, 1, 10) = ?
              AND outcome = ?
              AND side = 'YES'
              AND action = 'entry'
              AND (
                    reason_codes_json LIKE '%hard_source_direct%'
                    OR reason_codes_json LIKE '%hard_source_posterior_constraint%'
                  )
              AND created_at >= ?
            LIMIT 1
            """,
            (slug, event_date, outcome, cutoff),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _last_posterior_target_entry(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> Dict[str, Any]:
    try:
        row = conn.execute(
            """
            SELECT outcome, details_json, created_at
            FROM auto_trade_decisions
            WHERE slug = ?
              AND event_date = ?
              AND action = 'entry'
              AND reason_codes_json LIKE '%posterior_target_direct%'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (slug, event_date),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return {}
    details = _json_loads(row["details_json"])
    return {
        "outcome": str(row["outcome"] or ""),
        "created_at": str(row["created_at"] or ""),
        "details": details,
    }


def _recent_same_posterior_target_entry_exists(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    target_posterior: Optional[float],
    within_sec: int,
    epsilon: float,
) -> bool:
    if within_sec <= 0:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_sec)).isoformat(timespec="seconds")
    try:
        rows = conn.execute(
            """
            SELECT details_json
            FROM auto_trade_decisions
            WHERE slug = ?
              AND event_date = ?
              AND outcome = ?
              AND side = 'YES'
              AND action = 'entry'
              AND reason_codes_json LIKE '%posterior_target_direct%'
              AND created_at >= ?
            ORDER BY created_at DESC, id DESC
            LIMIT 5
            """,
            (slug, event_date, outcome, cutoff),
        ).fetchall()
    except sqlite3.Error:
        return False
    if not rows:
        return False
    if target_posterior is None:
        return True
    for row in rows:
        details = _json_loads(row["details_json"])
        prev = _prob01(
            details.get("target_posterior")
            if isinstance(details, Mapping)
            else None
        )
        if prev is None and isinstance(details, Mapping):
            prev = _prob01(details.get("q_temperature_bucket_posterior"))
        if prev is not None and abs(float(prev) - float(target_posterior)) <= float(epsilon):
            return True
    return False


def _posterior_target_job_exists_for_fact(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    fact_id: Optional[int],
    target_bucket: str,
    target_posterior: Optional[float],
    epsilon: float,
) -> bool:
    try:
        normalized_fact_id = int(fact_id or 0)
    except Exception:
        normalized_fact_id = 0
    if normalized_fact_id <= 0:
        return False
    bucket = str(target_bucket or "").strip()
    if not bucket:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    now_iso = _utc_now_iso()
    try:
        rows = conn.execute(
            """
            SELECT payload_json
            FROM auto_trade_jobs
            WHERE source = ?
              AND (
                    LOWER(COALESCE(state, '')) IN ('queued', 'retrying')
                 OR (
                    LOWER(COALESCE(state, '')) = 'running'
                    AND (
                        COALESCE(lease_until, '') = ''
                     OR julianday(lease_until) > julianday(?)
                    )
                 )
              )
              AND created_at >= ?
              AND json_extract(payload_json, '$.posterior_target_direct.fact_id') = ?
              AND json_extract(payload_json, '$.posterior_target_direct.target_bucket') = ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (POSTERIOR_TARGET_SOURCE, now_iso, cutoff, normalized_fact_id, bucket),
        ).fetchall()
    except sqlite3.Error:
        return False
    if not rows:
        return False
    if target_posterior is None:
        return True
    for row in rows:
        payload = _json_loads(row["payload_json"])
        if not isinstance(payload, Mapping):
            continue
        direct = payload.get("posterior_target_direct")
        if not isinstance(direct, Mapping):
            continue
        previous = _prob01(direct.get("target_posterior"))
        if previous is not None and abs(float(previous) - float(target_posterior)) <= float(epsilon):
            return True
    return False


def _recent_posterior_target_old_exit_cleanup_job_exists(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    target_bucket: str,
    within_sec: int,
) -> bool:
    if within_sec <= 0:
        return False
    bucket = str(target_bucket or "").strip()
    if not bucket:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_sec)).isoformat(
        timespec="seconds"
    )
    try:
        rows = conn.execute(
            """
            SELECT id, state
            FROM auto_trade_jobs
            WHERE source = ?
              AND LOWER(COALESCE(state, '')) NOT IN ('failed', 'stuck', 'superseded')
              AND (
                    created_at >= ?
                 OR updated_at >= ?
                 OR ended_at >= ?
                 OR lease_until >= ?
              )
              AND json_extract(payload_json, '$.posterior_target_direct.old_exit_cleanup_triggered') = 1
              AND json_extract(payload_json, '$.posterior_target_direct.target_bucket') = ?
              AND json_extract(payload_json, '$.include_event_keys[0].slug') = ?
              AND json_extract(payload_json, '$.include_event_keys[0].event_date') = ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (
                POSTERIOR_TARGET_SOURCE,
                cutoff,
                cutoff,
                cutoff,
                cutoff,
                bucket,
                slug,
                event_date,
            ),
        ).fetchall()
    except sqlite3.Error:
        return False
    for row in rows or []:
        try:
            job_id = int(row["id"] if isinstance(row, sqlite3.Row) else row[0])
        except Exception:
            job_id = 0
        state = str(row["state"] if isinstance(row, sqlite3.Row) else row[1] or "").strip().lower()
        if state in {"queued", "retrying", "running"}:
            return True
        if job_id > 0 and _posterior_target_old_exit_cleanup_job_has_failed_exit(
            conn,
            job_id=job_id,
            slug=slug,
            event_date=event_date,
            target_bucket=bucket,
        ):
            if _truthy(
                os.environ.get("PM_POSTERIOR_TARGET_DIRECT_RETRY_FAILED_OLD_EXIT_CLEANUP"),
                True,
            ):
                continue
            return True
        return True
    return False


def _posterior_target_old_exit_cleanup_job_has_failed_exit(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    slug: str,
    event_date: str,
    target_bucket: str,
) -> bool:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS failed_count
            FROM auto_trade_decisions
            WHERE action = 'exit'
              AND slug = ?
              AND substr(event_date, 1, 10) = ?
              AND json_extract(details_json, '$.posterior_target_job_id') = ?
              AND COALESCE(json_extract(details_json, '$.target_bucket'), ?) = ?
              AND (
                    decided_state = 'exit_failed'
                 OR reason_codes_json LIKE '%place_order_failed%'
                 OR json_extract(details_json, '$.exit_skip_reason') = 'exit_submit_failed'
              )
            """,
            (slug, event_date, int(job_id), target_bucket, target_bucket),
        ).fetchone()
    except sqlite3.Error:
        return False
    try:
        return int(row[0] if row is not None else 0) > 0
    except Exception:
        return False


def _active_old_yes_trades(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    target_outcome: str,
    target_side: str = "YES",
    allowed_remote_no_outcomes: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    target_side_key = str(target_side or "YES").strip().upper()
    if target_side_key not in {"YES", "NO"}:
        target_side_key = "YES"
    allowed_remote_no = {
        str(item or "").strip()
        for item in (allowed_remote_no_outcomes or [])
        if str(item or "").strip()
    }
    try:
        rows = conn.execute(
            """
            SELECT outcome, side, state, position_size, last_order_id,
                   entry_price, avg_price, last_status
            FROM edge_trades
            WHERE slug = ?
              AND event_date = ?
              AND UPPER(COALESCE(side, '')) IN ('YES', 'NO')
              AND (
                    state IN ('entry_pending', 'exit_pending')
                    OR COALESCE(position_size, 0) > 0.000001
                  )
            ORDER BY updated_at DESC, id DESC
            """,
            (slug, event_date),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        item = dict(row)
        outcome = str(item.get("outcome") or "").strip()
        side = str(item.get("side") or "").strip().upper()
        if not outcome or side not in {"YES", "NO"}:
            continue
        if outcome == target_outcome and side == target_side_key:
            continue
        if target_side_key == "YES" and side == "NO" and outcome in allowed_remote_no:
            continue
        if target_side_key == "NO" and side == "YES" and outcome in allowed_remote_no:
            continue
        out.append(item)
    return out


def _posterior_target_has_active_entry(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str = "YES",
) -> bool:
    side_key = str(side or "YES").strip().upper()
    if side_key not in {"YES", "NO"}:
        side_key = "YES"
    outcome_key = str(outcome or "").strip()
    if not outcome_key:
        return False
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM edge_trades
            WHERE slug = ?
              AND event_date = ?
              AND outcome = ?
              AND UPPER(COALESCE(side, '')) = ?
	              AND (
	                    COALESCE(position_size, 0) > 0.000001
	                    OR (
	                        LOWER(COALESCE(state, '')) IN ('entry_pending', 'exit_pending')
	                        AND (
	                            COALESCE(last_order_id, '') <> ''
	                            OR LOWER(COALESCE(last_status, '')) IN (
	                                'clob_order_service_timeout_pending',
	                                'clob_order_service_submit_pending',
	                                'order_submit_pending_confirmation'
	                            )
	                        )
	                        AND LOWER(COALESCE(last_status, '')) NOT LIKE '%not_found%'
	                        AND LOWER(COALESCE(last_status, '')) NOT LIKE '%order_missing%'
	                        AND LOWER(COALESCE(last_status, '')) NOT LIKE '%exec_failed%'
	                    )
                  )
            LIMIT 1
            """,
            (slug, event_date, outcome_key, side_key),
        ).fetchone()
        if row is not None:
            return True
    except sqlite3.Error:
        pass
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    try:
        row = conn.execute(
            """
            SELECT 1
            FROM auto_orders
            WHERE slug = ?
              AND event_date = ?
              AND outcome = ?
              AND UPPER(COALESCE(side, '')) = ?
              AND UPPER(COALESCE(action, 'BUY')) = 'BUY'
              AND COALESCE(order_id, '') <> ''
              AND COALESCE(created_at, '') >= ?
              AND LOWER(COALESCE(order_status, '')) IN (
                    'live', 'open', 'pending', 'unknown', 'matched', 'filled'
                  )
            LIMIT 1
            """,
            (slug, event_date, outcome_key, side_key, cutoff),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _posterior_target_previous_place_order_retry_block(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    outcome: str,
    side: str = "YES",
) -> Dict[str, Any]:
    side_key = str(side or "YES").strip().upper()
    if side_key not in {"YES", "NO"}:
        side_key = "YES"
    outcome_key = str(outcome or "").strip()
    if not slug or not event_date or not outcome_key:
        return {"blocked": False}
    try:
        row = conn.execute(
            """
            SELECT state, last_action, last_action_at, last_status, last_error,
                   entry_retry_count, updated_at
            FROM edge_trades
            WHERE slug = ?
              AND event_date = ?
              AND outcome = ?
              AND UPPER(COALESCE(side, '')) = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (slug, event_date, outcome_key, side_key),
        ).fetchone()
    except sqlite3.Error:
        return {"blocked": False}
    if row is None:
        return {"blocked": False}

    state = str(row["state"] or "").strip().lower()
    last_action = str(row["last_action"] or "").strip().lower()
    last_status = str(row["last_status"] or "").strip().lower()
    last_error = str(row["last_error"] or "").strip()
    last_error_lc = last_error.lower()
    marker_text = " ".join([state, last_action, last_status, last_error_lc])
    if "dry_run" in marker_text:
        return {"blocked": False}
    order_failure_markers = (
        "place-order",
        "place_order",
        "missing_order_id",
        "order_rejected",
        "exec_nonzero",
        "exec_failed",
        "timed out",
        "timeout",
    )
    place_order_failed = state == "entry_failed" and any(
        marker in marker_text for marker in order_failure_markers
    )
    if not place_order_failed:
        return {"blocked": False}

    try:
        retry_count = int(row["entry_retry_count"] or 0)
    except Exception:
        retry_count = 0
    max_retry = max(0, _env_int("PM_AUTO_TRADE_ENTRY_RETRY_MAX", 3))
    cooldown_sec = max(0, _env_int("PM_AUTO_TRADE_ENTRY_RETRY_COOLDOWN_SEC", 120))
    last_dt = _parse_iso_dt(row["last_action_at"]) or _parse_iso_dt(row["updated_at"])
    age_sec: Optional[float] = None
    if last_dt is not None:
        try:
            age_sec = max(0.0, (datetime.now(timezone.utc) - last_dt).total_seconds())
        except Exception:
            age_sec = None
    blocked_by_max = max_retry <= 0 or retry_count >= max_retry
    blocked_by_cooldown = bool(
        cooldown_sec > 0 and age_sec is not None and age_sec < float(cooldown_sec)
    )
    if not (blocked_by_max or blocked_by_cooldown):
        return {"blocked": False}
    reason = (
        "posterior_target_previous_place_order_failure_retry_exhausted"
        if blocked_by_max
        else "posterior_target_previous_place_order_failure_retry_cooldown"
    )
    return {
        "blocked": True,
        "reason": reason,
        "state": state,
        "last_action": str(row["last_action"] or ""),
        "last_status": str(row["last_status"] or ""),
        "last_error": last_error,
        "entry_retry_count": retry_count,
        "entry_retry_max": max_retry,
        "entry_retry_cooldown_sec": cooldown_sec,
        "last_action_at": str(row["last_action_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "age_sec": round(age_sec, 3) if age_sec is not None else None,
        "blocked_by_max_retry": bool(blocked_by_max),
        "blocked_by_cooldown": bool(blocked_by_cooldown),
    }


def _opposite_trade_side(side: Any) -> str:
    side_text = str(side or "").strip().upper()
    if side_text == "YES":
        return "NO"
    if side_text == "NO":
        return "YES"
    return ""


def _posterior_base_probs(rows: List[Dict[str, Any]]) -> List[float]:
    probs: List[float] = []
    for row in rows:
        value = _to_float(row.get("q_bayes_posterior_pre_hard_source_override"))
        if value is None:
            value = _to_float(row.get("q_bayes_posterior"))
        if value is None:
            value = _to_float(row.get("q_entry"))
        if value is None:
            value = _to_float(row.get("q_raw"))
        probs.append(float(value if value is not None else 0.0))
    return probs


def _top_posterior_row(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    best: Dict[str, Any] = {}
    best_q = -1.0
    for row in rows:
        q = _to_float(row.get("q_bayes_posterior"))
        if q is None:
            q = _to_float(row.get("q_temperature_bucket_posterior"))
        if q is None:
            q = _to_float(row.get("q_entry"))
        if q is None:
            q = _to_float(row.get("q_raw"))
        if q is not None and float(q) > best_q:
            best = row
            best_q = float(q)
    return best


def _prob01(value: Any) -> Optional[float]:
    prob = _to_float(value)
    if prob is None:
        return None
    if prob > 1.0 and prob <= 100.0:
        prob = prob / 100.0
    return max(0.0, min(1.0, float(prob)))


def _top_level_ctp_target_from_payload(payload: Mapping[str, Any]) -> Tuple[str, Optional[float], str]:
    probability_field = str(payload.get("target_bucket_probability_field") or "")
    target_source = str(payload.get("target_bucket_source") or "")
    posterior_source = str(payload.get("posterior_temperature_distribution_source") or "")
    has_ctp_contract = (
        "posterior_temperature_distribution" in probability_field
        or "continuous_temperature_posterior" in target_source
        or "continuous_temperature_posterior" in posterior_source
    )
    if not has_ctp_contract:
        return "", None, ""
    bucket = str(
        payload.get("target_bucket")
        or payload.get("ssot_target_bucket")
        or payload.get("posterior_highest_bucket_label")
        or ""
    ).strip()
    if not bucket:
        return "", None, ""
    for prob_key in (
        "target_bucket_probability_pct",
        "target_probability_pct",
        "posterior_highest_probability_pct",
        "target_bucket_probability",
        "target_probability",
        "target_posterior",
    ):
        prob = _prob01(payload.get(prob_key))
        if prob is not None:
            return bucket, prob, f"top_level_ctp_ssot.{prob_key}"
    return bucket, None, "top_level_ctp_ssot"


def _canonical_target_from_payload(payload: Mapping[str, Any]) -> Tuple[str, Optional[float], str]:
    top_bucket, top_probability, top_source = _top_level_ctp_target_from_payload(payload)
    if top_bucket:
        return top_bucket, top_probability, top_source
    for key, bucket_keys, prob_keys in (
        (
            "posterior_target_bucket_selection",
            ("target_bucket", "bucket", "outcome"),
            (
                "target_probability",
                "target_probability_pct",
                "target_bucket_probability",
                "target_bucket_probability_pct",
                "target_posterior",
            ),
        ),
        (
            "target_bucket_selection",
            ("target_bucket", "bucket", "outcome"),
            (
                "target_probability",
                "target_probability_pct",
                "target_bucket_probability",
                "target_bucket_probability_pct",
                "target_posterior",
            ),
        ),
        (
            "target_realization_capital_plan",
            ("target_bucket", "bucket", "outcome"),
            ("target_posterior", "posterior", "target_probability", "target_probability_pct"),
        ),
    ):
        container = payload.get(key)
        if not isinstance(container, Mapping):
            continue
        bucket = ""
        for bucket_key in bucket_keys:
            bucket = str(container.get(bucket_key) or "").strip()
            if bucket:
                break
        if not bucket:
            continue
        prob = None
        for prob_key in prob_keys:
            prob = _prob01(container.get(prob_key))
            if prob is not None:
                break
        return bucket, prob, key
    row = _top_posterior_row(_rows(payload))
    bucket = str(row.get("outcome") or row.get("label") or "").strip()
    if not bucket:
        return "", None, ""
    for prob_key in ("q_temperature_bucket_posterior", "q_bayes_posterior", "q_entry", "q_raw"):
        prob = _prob01(row.get(prob_key))
        if prob is not None:
            return bucket, prob, f"distribution.{prob_key}"
    return bucket, None, "distribution"


def _insert_decision(
    conn: sqlite3.Connection,
    *,
    run_date: str,
    slug: str,
    event_date: str,
    title: str,
    outcome: str,
    side: str,
    action: str,
    decided_state: str,
    will_order: bool,
    reason_codes: List[str],
    reasons: List[str],
    details: Mapping[str, Any],
    created_at: str,
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO auto_trade_decisions(
            run_id, run_date, slug, event_date, title,
            outcome, side, action, decided_state, will_order,
            reason_codes_json, reasons_json, details_json, created_at
        )
        VALUES(NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_date,
            slug,
            event_date,
            title,
            outcome,
            side,
            action,
            decided_state,
            1 if will_order else 0,
            json.dumps(reason_codes, ensure_ascii=True, separators=(",", ":")),
            json.dumps(reasons, ensure_ascii=True, separators=(",", ":")),
            json.dumps(dict(details), ensure_ascii=True, separators=(",", ":")),
            created_at,
        ),
    )
    return int(cur.lastrowid or 0)


def _build_expected_target(
    *,
    slug: str,
    event_date: str,
    outcome: str,
    observation: Mapping[str, Any],
    row: Mapping[str, Any],
    tick: Mapping[str, Any],
    token: Mapping[str, Any],
    payload: Optional[Mapping[str, Any]] = None,
    side: str = "YES",
    leg_role: str = "hard_source_direct_target",
    entry_reasons: Optional[List[str]] = None,
    held_target_bucket: str = "",
    held_target_side: str = "",
) -> Dict[str, Any]:
    side_upper = str(side or "").strip().upper()
    if side_upper not in {"YES", "NO"}:
        side_upper = "YES"
    ask = _to_float(tick.get("best_ask"))
    bid = _to_float(tick.get("best_bid"))
    if ask is None:
        ask = _to_float(row.get("best_ask" if side_upper == "YES" else "best_ask_no"))
    if bid is None:
        bid = _to_float(row.get("best_bid" if side_upper == "YES" else "best_bid_no"))
    spread = _to_float(tick.get("spread"))
    if spread is None:
        spread = _to_float(row.get("spread"))
    token_id = str(
        row.get("yes_token" if side_upper == "YES" else "no_token")
        or token.get("yes_token" if side_upper == "YES" else "no_token")
        or ""
    ).strip()
    if side_upper == "NO":
        q_direct = _to_float(row.get("q_bayes_posterior_no"))
        if q_direct is None:
            q_direct = _to_float(row.get("q_temperature_bucket_posterior_no"))
        if q_direct is None:
            q_direct = _to_float(row.get("q_entry_no"))
    else:
        q_direct = None
    if q_direct is None:
        q_direct = _to_float(row.get("q_bayes_posterior"))
        if side_upper == "NO" and q_direct is not None:
            q_direct = 1.0 - float(q_direct)
    if q_direct is None:
        q_direct = _to_float(row.get("q_temperature_bucket_posterior"))
        if side_upper == "NO" and q_direct is not None:
            q_direct = 1.0 - float(q_direct)
    if q_direct is None:
        q_direct = _to_float(row.get("q_entry"))
        if side_upper == "NO" and q_direct is not None:
            q_direct = 1.0 - float(q_direct)
    if q_direct is None:
        q_direct = float(_env_float("PM_HARD_SOURCE_DIRECT_Q", 0.995))
        if side_upper == "NO":
            q_direct = 1.0 - float(q_direct)
    q_direct = max(0.0, min(1.0, float(q_direct)))
    if ask is None or not math.isfinite(float(ask)) or ask <= 0:
        ask = _to_float(row.get("ask" if side_upper == "YES" else "ask_no"))
    if ask is None or not math.isfinite(float(ask)) or ask <= 0:
        ask = 0.99
    edge = max(0.0, q_direct - float(ask))
    reasons = list(entry_reasons or ["hard_source_direct", "target_realization_order_intent"])
    target = {
        "slug": slug,
        "event_date": event_date,
        "outcome": outcome,
        "side": side_upper,
        "strategy": TARGET_REALIZATION_STRATEGY,
        "strategy_source": DIRECT_SOURCE,
        "entry_ok": True,
        "entry_reasons": reasons,
        "hard_source_direct": True,
        "hard_source_posterior_override": True,
        "posterior_override": True,
        "posterior_override_source": observation.get("source_id"),
        "posterior_override_reason": "hard_source_lower_bound_constraint",
        "target_adjustment_decision_version": "hard_source_posterior_override_v1",
        "target_adjustment_action": "HARD_SOURCE_BUCKET_OVERRIDE",
        "target_adjustment_reason_codes": [
            "hard_source_direct",
            "hard_source_posterior_override",
        ],
        "decision_ssot": "auto_trade_target_bucket",
        "hard_source_source_id": observation.get("source_id"),
        "hard_source_observed_value_c": observation.get("value_c"),
        "hard_source_observed_at": observation.get("observed_at"),
        "hard_source_fetched_at": observation.get("fetched_at"),
        "q_entry": q_direct,
        "q_min_bound": q_direct,
        "q_raw": q_direct,
        "q_bayes_posterior": q_direct,
        "q_temperature_bucket_posterior": q_direct,
        "new_target_posterior": q_direct,
        "target_bucket_posterior": q_direct,
        "q_calibrated": q_direct,
        "q_quote": q_direct,
        "edge_min": edge,
        "edge_score": edge * 100.0,
        "exec_edge_entry": edge,
        "exec_edge_score": edge * 100.0,
        "edge_realizable": edge,
        "edge_realizable_v2": edge,
        "edge_realizable_score": edge * 100.0,
        "edge_realizable_p_realize": 1.0,
        "edge_realizable_p_converge": 1.0,
        "edge_tradeability_pass": True,
        "edge_tradeability_profit_value": edge,
        "ask": ask,
        "bid": bid,
        "best_ask": ask,
        "best_bid": bid,
        "spread": spread,
        "token_id": token_id,
        "market_tick_created_at": tick.get("created_at") or "",
        "target_realization_plan_action": "add",
        "target_realization_leg_role": leg_role,
        "target_realization_execution_action": "BUY",
        "target_realization_position_effect": (
            "open_or_add_old_bucket_opposite_side"
            if leg_role == "old_bucket_reverse"
            else "open_or_add_new_bucket_side"
        ),
        "sell_old_buy_new_semantics": (
            "buy_old_bucket_opposite_side"
            if leg_role == "old_bucket_reverse"
            else "buy_new_bucket_side"
        ),
        "paired_exit_outcome": held_target_bucket or None,
        "paired_exit_side": held_target_side or None,
        "paired_exit_execution_action": "SELL" if leg_role == "old_bucket_reverse" else None,
        "held_target_bucket": held_target_bucket or None,
        "held_target_side": held_target_side or None,
        "old_bucket_reverse_side": side_upper if leg_role == "old_bucket_reverse" else None,
        "weather_strategy_mode": "ssot_posterior_target_switch",
    }
    source_payload: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    _posterior_target_copy_rule_source_fields(target, row, source_payload, observation)
    if (
        _truthy(target.get("rules_contract_ok"), False)
        or _truthy(target.get("weather_strategy_rules_ok"), False)
    ) and (
        str(
            target.get("settlement_station")
            or target.get("weather_strategy_settlement_station")
            or target.get("station_id")
            or source_payload.get("station_id")
            or ""
        ).strip()
    ):
        target.setdefault("source_usable_for_settlement", True)
        target.setdefault("weather_strategy_source_usable_for_settlement", True)
    return target


def _apply_expected_entry_audit(
    target: Mapping[str, Any],
    *,
    reason_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    out = dict(target)
    try:
        override = swm._apply_hard_source_direct_expected_entry_override(
            out,
            outcome=str(out.get("outcome") or ""),
            side=str(out.get("side") or "YES"),
        )
        if isinstance(override, Mapping) and override:
            out.update(dict(override))
    except Exception:
        pass
    try:
        audit = swm._build_entry_usable_edge_audit(
            out,
            reason_codes=list(reason_codes or []),
        )
        if isinstance(audit, Mapping) and audit:
            out.update(dict(audit))
    except Exception:
        if "entry_usable_edge" not in out:
            out["entry_usable_edge"] = out.get("edge_realizable")
        if "entry_usable_edge_size_scale" not in out:
            out["entry_usable_edge_size_scale"] = 1.0
    return out


def _posterior_target_trade_follow_allowed(payload: Mapping[str, Any]) -> Tuple[bool, str]:
    plan = payload.get("target_realization_capital_plan")
    if isinstance(plan, Mapping):
        if plan.get("temperature_path_trade_follow_allowed") is False:
            return False, str(plan.get("temperature_path_trade_follow_reason") or "temperature_path_trade_follow_not_allowed")
        if plan.get("temperature_path_trade_follow_block_yes") is True:
            return False, str(plan.get("temperature_path_trade_follow_reason") or "temperature_path_trade_follow_not_allowed")
    path = payload.get("temperature_path_distribution")
    if isinstance(path, Mapping):
        trade_follow = path.get("trade_follow")
        if isinstance(trade_follow, Mapping) and trade_follow.get("allowed") is False:
            return False, str(trade_follow.get("reason") or "temperature_path_trade_follow_not_allowed")
    return True, "posterior_path_reliability_and_freshness_pass"


def _posterior_target_reliability(payload: Mapping[str, Any], default: float = 1.0) -> float:
    for container_key in ("target_realization_capital_plan", "temperature_realization_reliability"):
        container = payload.get(container_key)
        if not isinstance(container, Mapping):
            continue
        for key in (
            "target_realization_reliability",
            "reliability",
            "score",
            "path_reliability",
        ):
            value = _to_float(container.get(key))
            if value is not None and math.isfinite(float(value)):
                return max(0.0, min(1.0, float(value)))
    return max(0.0, min(1.0, float(default)))


def _strip_hard_source_expected_fields(target: Dict[str, Any]) -> None:
    for key in (
        "hard_source_direct",
        "hard_source_posterior_override",
        "hard_source_direct_target_entry_override",
        "hard_source_source_id",
        "hard_source_observed_value_c",
        "hard_source_observed_at",
        "hard_source_fetched_at",
        "hard_source_observed_bucket",
        "hard_source_constraint_mode",
        "hard_source_final_bucket_lock",
        "hard_source_final_lock_reason",
        "hard_source_direct_entry_override_version",
        "posterior_override",
        "posterior_override_source",
        "posterior_override_reason",
    ):
        target.pop(key, None)


_POSTERIOR_TARGET_RULE_SOURCE_FIELD_ALIASES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "rules_contract_ok",
        ("rules_contract_ok", "settlement_rules_ok", "contract_rules_ok", "weather_strategy_rules_ok"),
    ),
    (
        "weather_strategy_rules_ok",
        ("weather_strategy_rules_ok", "rules_contract_ok", "settlement_rules_ok", "contract_rules_ok"),
    ),
    (
        "rules_missing_reasons",
        ("rules_missing_reasons", "rules_na_reason", "weather_strategy_rules_na_reason"),
    ),
    (
        "weather_strategy_rules_na_reason",
        ("weather_strategy_rules_na_reason", "rules_missing_reasons", "rules_na_reason"),
    ),
    (
        "settlement_station",
        ("settlement_station", "weather_strategy_settlement_station", "station_id"),
    ),
    (
        "weather_strategy_settlement_station",
        ("weather_strategy_settlement_station", "settlement_station", "station_id"),
    ),
    ("station_id", ("station_id", "settlement_station", "weather_strategy_settlement_station")),
    ("source_kind", ("source_kind", "weather_strategy_source_kind", "contract_source_kind")),
    ("weather_strategy_source_kind", ("weather_strategy_source_kind", "source_kind", "contract_source_kind")),
    ("source_confidence", ("source_confidence", "weather_strategy_source_confidence", "contract_source_confidence")),
    ("weather_strategy_source_confidence", ("weather_strategy_source_confidence", "source_confidence", "contract_source_confidence")),
    ("source_fit_tier", ("source_fit_tier", "weather_strategy_source_fit_tier", "contract_source_fit_tier")),
    ("weather_strategy_source_fit_tier", ("weather_strategy_source_fit_tier", "source_fit_tier", "contract_source_fit_tier")),
    ("source_trust_tier", ("source_trust_tier", "weather_strategy_source_trust_tier", "contract_source_trust_tier")),
    ("weather_strategy_source_trust_tier", ("weather_strategy_source_trust_tier", "source_trust_tier", "contract_source_trust_tier")),
    (
        "source_usable_for_settlement",
        ("source_usable_for_settlement", "weather_strategy_source_usable_for_settlement", "contract_source_usable_for_settlement"),
    ),
    (
        "weather_strategy_source_usable_for_settlement",
        ("weather_strategy_source_usable_for_settlement", "source_usable_for_settlement", "contract_source_usable_for_settlement"),
    ),
    ("source_na_reason", ("source_na_reason", "weather_strategy_source_na_reason", "contract_source_na_reason")),
    ("weather_strategy_source_na_reason", ("weather_strategy_source_na_reason", "source_na_reason", "contract_source_na_reason")),
    ("resolution_source", ("resolution_source", "contract_resolution_source")),
    ("market_var", ("market_var",)),
    ("market_unit", ("market_unit",)),
)

_POSTERIOR_TARGET_RULE_SOURCE_FIELDS: Tuple[str, ...] = tuple(
    key for key, _aliases in _POSTERIOR_TARGET_RULE_SOURCE_FIELD_ALIASES
)


def _posterior_target_copy_rule_source_fields(
    target: Dict[str, Any],
    *sources: Mapping[str, Any],
) -> None:
    for target_key, aliases in _POSTERIOR_TARGET_RULE_SOURCE_FIELD_ALIASES:
        current = target.get(target_key)
        if current is not None and not (isinstance(current, str) and not current.strip()):
            continue
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            for alias in aliases:
                if alias not in source:
                    continue
                value = source.get(alias)
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                if isinstance(value, list):
                    if not all(isinstance(item, (str, int, float, bool)) or item is None for item in value[:30]):
                        continue
                    target[target_key] = list(value[:30])
                    break
                if isinstance(value, (str, int, float, bool)):
                    target[target_key] = value
                    break
            if target_key in target:
                break


def _build_posterior_target_expected_target(
    *,
    slug: str,
    event_date: str,
    outcome: str,
    row: Mapping[str, Any],
    tick: Mapping[str, Any],
    token: Mapping[str, Any],
    payload: Mapping[str, Any],
    target_bucket: str,
    target_posterior: Optional[float],
    target_source: str,
    side: str = "YES",
    leg_role: str = "posterior_target_direct_target",
    entry_reasons: Optional[List[str]] = None,
    held_target_bucket: str = "",
    held_target_side: str = "",
) -> Dict[str, Any]:
    target = _build_expected_target(
        slug=slug,
        event_date=event_date,
        outcome=outcome,
        observation={
            "source_id": POSTERIOR_TARGET_SOURCE,
            "observed_at": payload.get("fact_as_of") or payload.get("updated_at") or "",
            "fetched_at": payload.get("fact_as_of") or payload.get("updated_at") or "",
        },
        row=row,
        tick=tick,
        token=token,
        side=side,
        leg_role=leg_role,
        entry_reasons=entry_reasons
        or ["posterior_target_direct", "target_realization_order_intent"],
        held_target_bucket=held_target_bucket,
        held_target_side=held_target_side,
    )
    _strip_hard_source_expected_fields(target)
    selection = payload.get("posterior_target_bucket_selection")
    _posterior_target_copy_rule_source_fields(
        target,
        row,
        payload,
        selection if isinstance(selection, Mapping) else {},
    )
    if (
        _truthy(target.get("rules_contract_ok"), False)
        or _truthy(target.get("weather_strategy_rules_ok"), False)
    ) and (
        str(
            target.get("settlement_station")
            or target.get("weather_strategy_settlement_station")
            or target.get("station_id")
            or payload.get("station_id")
            or ""
        ).strip()
    ):
        target.setdefault("source_usable_for_settlement", True)
        target.setdefault("weather_strategy_source_usable_for_settlement", True)
    posterior = target_posterior
    if posterior is None:
        posterior = _prob01(target.get("q_temperature_bucket_posterior"))
    if posterior is None:
        posterior = _prob01(target.get("q_bayes_posterior"))
    posterior = 0.0 if posterior is None else max(0.0, min(1.0, float(posterior)))
    follow_allowed, follow_reason = _posterior_target_trade_follow_allowed(payload)
    reliability = _posterior_target_reliability(payload, default=1.0)
    side_upper = str(side or "YES").strip().upper()
    candidate_prob = _prob01(row.get("q_temperature_bucket_posterior"))
    if candidate_prob is None:
        candidate_prob = _prob01(row.get("q_bayes_posterior"))
    if candidate_prob is None:
        candidate_prob = _prob01(target.get("q_temperature_bucket_posterior"))
    candidate_prob = posterior if candidate_prob is None else float(candidate_prob)
    if side_upper == "NO":
        q_entry = max(0.0, min(1.0, 1.0 - float(candidate_prob)))
    else:
        q_entry = posterior if str(outcome or "").strip() == str(target_bucket or "").strip() else float(candidate_prob)
    direct_buy_new = bool(
        side_upper == "YES"
        and str(leg_role or "").strip() == "posterior_target_direct_target"
        and str(outcome or "").strip() == str(target_bucket or "").strip()
    )
    remote_no_leg = bool(side_upper == "NO" and str(leg_role or "").strip() == "remote_no")
    posterior_target_path_follow_overridden = bool(direct_buy_new and not follow_allowed)
    posterior_target_path_diagnostic_only = False
    allowed = bool(follow_allowed or remote_no_leg or direct_buy_new)
    target_realization_follow_reason = (
        "remote_no_distance_window_above_target"
        if remote_no_leg
        else (
            "posterior_target_direct_target_snapshot"
            if posterior_target_path_follow_overridden
            else follow_reason
        )
    )
    temperature_path_follow_reason = (
        "posterior_target_direct_target_snapshot"
        if posterior_target_path_follow_overridden
        else follow_reason
    )
    position_effect = "open_or_add_new_bucket_side"
    sell_old_buy_new_semantics = "buy_new_bucket_side"
    if leg_role == "old_bucket_reverse":
        position_effect = "open_or_add_old_bucket_opposite_side"
        sell_old_buy_new_semantics = "buy_old_bucket_opposite_side"
        strategy_lane = "old_bucket_reverse"
    elif remote_no_leg:
        position_effect = "open_or_add_remote_no_bucket_side"
        sell_old_buy_new_semantics = "buy_remote_no_bucket_side"
        strategy_lane = "remote_no_bucket"
    else:
        strategy_lane = "yes_realized_target" if direct_buy_new else "posterior_target_direct"
    size_scale = min(1.0, max(0.20, reliability)) if allowed else 0.0
    target.update(
        {
            "strategy": TARGET_REALIZATION_STRATEGY,
            "strategy_source": POSTERIOR_TARGET_SOURCE,
            "strategy_family": "posterior_settlement",
            "entry_strategy_family": "posterior_settlement",
            "weather_strategy_family": "posterior_settlement",
            "posterior_target_direct": True,
            "posterior_target_update": True,
            "posterior_target_source": target_source,
            "posterior_target_bucket": target_bucket,
            "target_bucket": target_bucket,
            "ssot_target_bucket": target_bucket,
            "posterior_settlement_target_bucket": target_bucket,
            "posterior_settlement_current_top_bucket": target_bucket,
            "current_top_bucket": target_bucket,
            "new_target_bucket": target_bucket,
            "target_posterior": posterior,
            "posterior_settlement_target_posterior": posterior,
            "posterior_settlement_current_top_posterior": posterior,
            "current_top_posterior": posterior,
            "new_target_posterior": posterior,
            "target_bucket_posterior": posterior,
            "posterior_target_direct_path_follow_diagnostic_only": bool(
                posterior_target_path_diagnostic_only
            ),
            "posterior_target_remote_no_path_follow_diagnostic_only": bool(
                remote_no_leg and not follow_allowed
            ),
            "temperature_path_trade_follow_allowed_before_posterior_target_direct": (
                follow_allowed if posterior_target_path_follow_overridden else None
            ),
            "temperature_path_trade_follow_reason_before_posterior_target_direct": (
                follow_reason if posterior_target_path_follow_overridden else None
            ),
            "posterior_settlement_candidate_bucket": outcome,
            "posterior_settlement_candidate_bucket_prob": float(candidate_prob),
            "target_realization_reliability": reliability,
            "target_realization_reliability_source": "posterior_target_direct",
            "posterior_settlement_entry_allowed": allowed,
            "posterior_settlement_entry_block_reason": "" if allowed else follow_reason,
            "posterior_settlement_size_scale": size_scale,
            "posterior_settlement_sort_score": reliability * 100.0 + posterior * 10.0,
            "temperature_path_trade_follow_allowed": allowed,
            "temperature_path_trade_follow_reason": temperature_path_follow_reason,
            "target_realization_trade_follow_allowed": allowed,
            "target_realization_trade_follow_reason": target_realization_follow_reason,
            "decision_ssot": "posterior_temperature_distribution",
            "target_adjustment_decision_version": "posterior_target_direct_v1",
            "target_adjustment_action": "POSTERIOR_TARGET_UPDATE",
            "strategy_lane": strategy_lane,
            "target_adjustment_reason_codes": [
                "posterior_target_direct",
                "posterior_probability_changed",
            ],
            "target_realization_leg_role": leg_role,
            "target_realization_execution_action": "BUY",
            "target_realization_position_effect": position_effect,
            "sell_old_buy_new_semantics": sell_old_buy_new_semantics,
        }
    )
    target["q_entry"] = float(q_entry)
    target["q_min_bound"] = float(q_entry)
    target["q_bayes_posterior"] = float(q_entry)
    target["q_temperature_bucket_posterior"] = float(q_entry)
    target["q_quote"] = float(q_entry)
    ask = _to_float(target.get("ask") or target.get("best_ask"))
    if ask is not None and math.isfinite(float(ask)):
        edge = max(0.0, float(q_entry) - float(ask))
        target["edge_min"] = edge
        target["edge_score"] = edge * 100.0
        target["exec_edge_entry"] = edge
        target["exec_edge_score"] = edge * 100.0
        target["edge_realizable"] = edge
        target["edge_realizable_v2"] = edge
        target["edge_realizable_score"] = edge * 100.0
        target["edge_tradeability_profit_value"] = edge
    if side_upper == "NO":
        target["remote_no_bucket"] = outcome
        target["remote_bucket_realization_prob"] = float(candidate_prob)
    return _apply_expected_entry_audit(
        target,
        reason_codes=["posterior_target_direct", "target_realization_order_intent"],
    )


def _posterior_target_remote_no_plan_legs(
    payload: Mapping[str, Any],
    *,
    target_bucket: str,
) -> List[Dict[str, Any]]:
    plan = payload.get("target_realization_capital_plan")
    if not isinstance(plan, Mapping):
        return []
    legs = plan.get("legs")
    if not isinstance(legs, list):
        return []
    target_bucket_key = str(target_bucket or plan.get("target_bucket") or "").strip()
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw_leg in legs:
        if not isinstance(raw_leg, Mapping):
            continue
        role = str(raw_leg.get("role") or raw_leg.get("target_realization_leg_role") or "").strip()
        side = str(raw_leg.get("side") or "").strip().upper()
        bucket = str(raw_leg.get("bucket") or raw_leg.get("outcome") or "").strip()
        if role != "remote_no" or side != "NO" or not bucket or bucket in seen:
            continue
        try:
            allowed, reason, distance, min_distance, max_distance = (
                _posterior_target_remote_no_plan_leg_allowed(raw_leg, target_bucket_key, plan)
            )
        except Exception:
            allowed, reason, distance, min_distance, max_distance = (
                False,
                "remote_no_distance_check_failed",
                None,
                2,
                3,
            )
        if not allowed:
            continue
        price_cap = _posterior_target_remote_no_price_cap(raw_leg, plan)
        raw_price = _to_float(
            raw_leg.get("price")
            or raw_leg.get("ask")
            or raw_leg.get("best_ask")
            or raw_leg.get("best_ask_no")
        )
        if (
            raw_price is not None
            and math.isfinite(float(raw_price))
            and float(raw_price) > float(price_cap) + 1e-9
        ):
            continue
        signed_distance = None
        try:
            signed_distance = swm.posterior_bucket_directional_distance(bucket, target_bucket_key)
        except Exception:
            signed_distance = None
        action = str(raw_leg.get("action") or raw_leg.get("target_realization_plan_action") or "").strip()
        block_reason = str(raw_leg.get("block_reason") or "").strip()
        if block_reason:
            continue
        if action and action != "add":
            continue
        target_notional = _to_float(raw_leg.get("target_notional"))
        delta_notional = _to_float(raw_leg.get("delta_notional"))
        if target_notional is not None and math.isfinite(float(target_notional)) and float(target_notional) <= 0.50:
            continue
        if delta_notional is not None and math.isfinite(float(delta_notional)) and float(delta_notional) <= 0.50:
            continue
        notional_cap = _posterior_target_remote_no_notional_cap(raw_leg, plan)
        notional_cap_applied = False
        if (
            target_notional is not None
            and math.isfinite(float(target_notional))
            and notional_cap is not None
            and float(target_notional) > float(notional_cap) + 1e-9
        ):
            target_notional = float(notional_cap)
            current_notional = _to_float(raw_leg.get("current_notional"))
            delta_notional = float(target_notional) - float(current_notional or 0.0)
            notional_cap_applied = True
        leg = dict(raw_leg)
        leg["bucket"] = bucket
        leg["outcome"] = bucket
        leg["side"] = "NO"
        leg["role"] = "remote_no"
        leg["target_realization_leg_role"] = "remote_no"
        leg["remote_no_bucket_direction"] = (
            "above_target"
            if signed_distance is not None and signed_distance > 0
            else "below_target"
        )
        leg["remote_no_bucket_distance"] = distance
        leg["remote_no_min_bucket_distance"] = min_distance
        leg["remote_no_max_bucket_distance"] = max_distance
        leg["remote_no_max_price"] = float(price_cap)
        leg["target_realization_order_price_cap"] = float(price_cap)
        if notional_cap is not None:
            leg["target_notional_cap_usdc"] = float(notional_cap)
        if target_notional is not None and math.isfinite(float(target_notional)):
            leg["target_notional"] = float(target_notional)
        if delta_notional is not None and math.isfinite(float(delta_notional)):
            leg["delta_notional"] = float(delta_notional)
        if notional_cap_applied:
            leg["target_notional_cap_applied"] = True
        if reason:
            leg["remote_no_reason"] = reason
        seen.add(bucket)
        out.append(leg)
    return out


def _posterior_target_allowed_remote_no_outcomes(
    payload: Mapping[str, Any],
    *,
    target_bucket: str,
) -> List[str]:
    plan = payload.get("target_realization_capital_plan")
    if not isinstance(plan, Mapping):
        return []
    legs = plan.get("legs")
    if not isinstance(legs, list):
        return []
    target_bucket_key = str(target_bucket or plan.get("target_bucket") or "").strip()
    out: List[str] = []
    seen: set[str] = set()
    for raw_leg in legs:
        if not isinstance(raw_leg, Mapping):
            continue
        role = str(raw_leg.get("role") or raw_leg.get("target_realization_leg_role") or "").strip()
        side = str(raw_leg.get("side") or "").strip().upper()
        bucket = str(raw_leg.get("bucket") or raw_leg.get("outcome") or "").strip()
        if role != "remote_no" or side != "NO" or not bucket or bucket in seen:
            continue
        try:
            allowed, _reason, _distance, _min_distance, _max_distance = (
                _posterior_target_remote_no_plan_leg_allowed(raw_leg, target_bucket_key, plan)
            )
        except Exception:
            allowed = False
        if not allowed:
            continue
        seen.add(bucket)
        out.append(bucket)
    return out


def _posterior_target_remote_no_price_cap(
    leg: Optional[Mapping[str, Any]] = None,
    plan: Optional[Mapping[str, Any]] = None,
) -> float:
    leg_ref = leg if isinstance(leg, Mapping) else {}
    plan_ref = plan if isinstance(plan, Mapping) else {}
    env_cap = _to_float(os.environ.get("PM_TARGET_REALIZATION_REMOTE_NO_MAX_PRICE"))
    if env_cap is not None and math.isfinite(float(env_cap)):
        return float(min(1.0, max(0.0, float(env_cap))))
    global_cap = _to_float(os.environ.get("PM_TARGET_REALIZATION_MAX_ENTRY_PRICE"))
    if global_cap is None or not math.isfinite(float(global_cap)):
        global_cap = 0.98
    cap = _to_float(leg_ref.get("target_realization_order_price_cap"))
    if cap is None:
        cap = _to_float(leg_ref.get("remote_no_max_price"))
    if cap is None:
        cap = _to_float(plan_ref.get("remote_no_max_price"))
    if cap is not None and math.isfinite(float(cap)):
        cap = max(float(cap), float(global_cap))
    if cap is None or not math.isfinite(float(cap)):
        cap = global_cap
    return float(min(1.0, max(0.0, float(cap))))


def _posterior_target_remote_no_notional_cap(
    leg: Optional[Mapping[str, Any]] = None,
    plan: Optional[Mapping[str, Any]] = None,
) -> Optional[float]:
    leg_ref = leg if isinstance(leg, Mapping) else {}
    plan_ref = plan if isinstance(plan, Mapping) else {}
    env_cap = _to_float(os.environ.get("PM_TARGET_REALIZATION_REMOTE_NO_MAX_NOTIONAL_USDC"))
    if env_cap is not None and math.isfinite(float(env_cap)) and float(env_cap) > 0:
        return float(env_cap)
    cap = _to_float(leg_ref.get("target_notional_cap_usdc"))
    if cap is None:
        cap = _to_float(plan_ref.get("remote_no_single_leg_max_notional_usdc"))
    if cap is None:
        cap = 100.0
    cap = max(float(cap), 100.0)
    if not math.isfinite(float(cap)) or float(cap) <= 0:
        return None
    return float(cap)


def _posterior_target_remote_no_plan_leg_allowed(
    leg: Mapping[str, Any],
    target_bucket: str,
    plan: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, str, Optional[int], int, int]:
    leg_ref = leg if isinstance(leg, Mapping) else {}
    bucket = str(leg_ref.get("bucket") or leg_ref.get("outcome") or "").strip()
    try:
        directional_distance = swm.posterior_bucket_directional_distance(bucket, target_bucket)
    except Exception:
        directional_distance = None
    if directional_distance is None:
        try:
            min_distance, max_distance = swm._target_realization_remote_no_distance_bounds()
        except Exception:
            min_distance, max_distance = 2, 4
        return False, "remote_no_direction_unavailable", None, min_distance, max_distance
    signed_distance = int(directional_distance)
    if signed_distance == 0:
        try:
            min_distance, max_distance = swm._target_realization_remote_no_distance_bounds()
        except Exception:
            min_distance, max_distance = 2, 4
        return False, "remote_no_must_not_be_target_bucket", 0, min_distance, max_distance
    direction = "above_target" if signed_distance > 0 else "below_target"
    try:
        fallback_min, fallback_max = swm._target_realization_remote_no_distance_bounds(direction)
    except Exception:
        fallback_min, fallback_max = (3, 4) if direction == "above_target" else (2, 3)
    plan_ref = plan if isinstance(plan, Mapping) else {}
    min_raw = _to_float(leg_ref.get("remote_no_min_bucket_distance"))
    max_raw = _to_float(leg_ref.get("remote_no_max_bucket_distance"))
    if min_raw is None:
        min_raw = _to_float(
            plan_ref.get(
                "remote_no_upper_min_bucket_distance"
                if direction == "above_target"
                else "remote_no_lower_min_bucket_distance"
            )
        )
    if max_raw is None:
        max_raw = _to_float(
            plan_ref.get(
                "remote_no_upper_max_bucket_distance"
                if direction == "above_target"
                else "remote_no_lower_max_bucket_distance"
            )
        )
    if min_raw is None or not math.isfinite(float(min_raw)):
        min_distance = int(fallback_min)
    else:
        min_distance = int(max(1, min(20, round(float(min_raw)))))
    if max_raw is None or not math.isfinite(float(max_raw)):
        max_distance = int(fallback_max)
    else:
        max_distance = int(max(min_distance, min(20, round(float(max_raw)))))
    distance = abs(signed_distance)
    if distance < min_distance:
        return False, "remote_no_bucket_not_far_enough", distance, min_distance, max_distance
    if distance > max_distance:
        return False, "remote_no_bucket_too_far", distance, min_distance, max_distance
    return True, "", distance, min_distance, max_distance


def _posterior_target_remote_no_expected_targets(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    snapshot_payload: Mapping[str, Any],
    target_bucket: str,
    target_posterior: Optional[float],
    target_source: str,
) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    for leg in _posterior_target_remote_no_plan_legs(
        snapshot_payload,
        target_bucket=target_bucket,
    ):
        outcome = str(leg.get("bucket") or leg.get("outcome") or "").strip()
        if not outcome:
            continue
        row = _row_for_outcome(snapshot_payload, outcome)
        if not row:
            continue
        row_for_target = dict(row)
        leg_posterior = _prob01(leg.get("posterior"))
        if leg_posterior is not None:
            row_for_target["q_temperature_bucket_posterior"] = float(leg_posterior)
            row_for_target["q_bayes_posterior"] = float(leg_posterior)
        token = _market_token(
            conn,
            slug=slug,
            event_date=event_date,
            outcome=outcome,
        )
        tick = _latest_market_tick(
            conn,
            slug=slug,
            event_date=event_date,
            outcome=outcome,
            side="NO",
        )
        target = _build_posterior_target_expected_target(
            slug=slug,
            event_date=event_date,
            outcome=outcome,
            row=row_for_target,
            tick=tick,
            token=token,
            payload=snapshot_payload,
            target_bucket=target_bucket,
            target_posterior=target_posterior,
            target_source=target_source or "posterior_temperature_distribution",
            side="NO",
            leg_role="remote_no",
            entry_reasons=[
                "posterior_target_direct",
                "target_realization_order_intent",
                "target_realization_remote_no",
            ],
        )
        plan_notional = _to_float(leg.get("target_notional"))
        delta_notional = _to_float(leg.get("delta_notional"))
        target.update(
            {
                "target_realization_plan_action": str(leg.get("action") or "add"),
                "target_realization_plan_notional": float(plan_notional)
                if plan_notional is not None and math.isfinite(float(plan_notional))
                else None,
                "target_realization_plan_notional_source": "target_realization_capital_plan",
                "kelly_target_notional": float(plan_notional)
                if plan_notional is not None and math.isfinite(float(plan_notional))
                else None,
                "kelly_sizing_source": "target_realization_capital_plan",
                "order_size_source": "target_realization_capital_plan",
                "target_realization_delta_notional": float(delta_notional)
                if delta_notional is not None and math.isfinite(float(delta_notional))
                else None,
                "remote_no_bucket_direction": leg.get("remote_no_bucket_direction"),
                "remote_no_bucket_distance": leg.get("remote_no_bucket_distance"),
                "remote_no_min_bucket_distance": leg.get("remote_no_min_bucket_distance"),
                "remote_no_max_bucket_distance": leg.get("remote_no_max_bucket_distance"),
                "remote_no_max_price": leg.get("remote_no_max_price"),
                "target_realization_reliability": leg.get(
                    "target_realization_reliability",
                    target.get("target_realization_reliability"),
                ),
                "target_realization_reliability_source": leg.get(
                    "target_realization_reliability_source",
                    target.get("target_realization_reliability_source"),
                ),
            }
        )
        targets.append(target)
    return targets


def _posterior_target_plan_has_actionable_secondary(payload: Mapping[str, Any]) -> bool:
    plan = payload.get("target_realization_capital_plan")
    if not isinstance(plan, Mapping):
        return False
    min_delta = max(0.0, _env_float("PM_POSTERIOR_TARGET_DIRECT_SECONDARY_MIN_DELTA_USDC", 0.50))
    for key in ("sell_old_legs", "buy_old_reverse_legs"):
        legs = plan.get(key)
        if not isinstance(legs, list):
            continue
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            if leg.get("leg_enabled") is False:
                continue
            return True
    for leg in plan.get("legs") or []:
        if not isinstance(leg, Mapping):
            continue
        role = str(leg.get("role") or "").strip()
        action = str(leg.get("action") or "").strip()
        if role != "remote_no" or action != "add":
            continue
        delta = _to_float(leg.get("delta_notional"))
        if delta is not None and float(delta) >= min_delta:
            return True
    return False


def _enqueue_direct_job(
    conn: sqlite3.Connection,
    *,
    run_date: str,
    slug: str,
    event_date: str,
    expected_target: Mapping[str, Any],
    expected_targets: Optional[List[Mapping[str, Any]]] = None,
    constrained_payload: Optional[Mapping[str, Any]] = None,
    observation: Mapping[str, Any],
    priority: int,
    primary_only_fast_path_allowed: Optional[bool] = None,
) -> Optional[int]:
    now_iso = _utc_now_iso()
    expected_with_timing = dict(expected_target)
    expected_with_timing["hard_source_job_enqueued_at"] = now_iso
    expected_with_timing["hard_source_ssot_to_job_enqueue_ms"] = _latency_ms_between(
        expected_with_timing.get("hard_source_ssot_written_at"),
        now_iso,
    )
    if expected_with_timing.get("hard_source_fetched_at"):
        expected_with_timing["hard_source_fetch_to_job_enqueue_ms"] = _latency_ms_between(
            expected_with_timing.get("hard_source_fetched_at"),
            now_iso,
        )
    targets_with_timing: List[Dict[str, Any]] = [expected_with_timing]
    primary_key = (
        str(expected_with_timing.get("outcome") or "").strip(),
        str(expected_with_timing.get("side") or "").strip().upper(),
    )
    seen_keys = {primary_key}
    for raw_target in expected_targets or []:
        target = dict(raw_target)
        key = (
            str(target.get("outcome") or "").strip(),
            str(target.get("side") or "").strip().upper(),
        )
        if not key[0] or key[1] not in {"YES", "NO"} or key in seen_keys:
            continue
        seen_keys.add(key)
        target["hard_source_job_enqueued_at"] = now_iso
        target["hard_source_ssot_to_job_enqueue_ms"] = _latency_ms_between(
            target.get("hard_source_ssot_written_at")
            or expected_with_timing.get("hard_source_ssot_written_at"),
            now_iso,
        )
        if target.get("hard_source_fetched_at"):
            target["hard_source_fetch_to_job_enqueue_ms"] = _latency_ms_between(
                target.get("hard_source_fetched_at"),
                now_iso,
            )
        targets_with_timing.append(target)
    if primary_only_fast_path_allowed is None:
        primary_only_fast_path_allowed = not bool(len(targets_with_timing) > 1)
    payload = {
        "include_event_keys": [{"slug": slug, "event_date": event_date}],
        "entry_expected_targets": targets_with_timing,
        "hard_source_direct": {
            "source": observation.get("source_id"),
            "value_c": observation.get("value_c"),
            "target_bucket": expected_with_timing.get("outcome"),
            "observed_bucket": expected_with_timing.get("hard_source_observed_bucket"),
            "decision_semantics": "ssot_posterior_constraint",
            "posterior_override": True,
            "posterior_override_q": expected_with_timing.get("q_bayes_posterior"),
            "decision_ssot": "auto_trade_target_bucket",
            "observed_at": observation.get("observed_at"),
            "fetched_at": observation.get("fetched_at"),
            "ssot_written_at": expected_with_timing.get("hard_source_ssot_written_at"),
            "ssot_write_ms": expected_with_timing.get("hard_source_ssot_write_ms"),
            "job_enqueued_at": now_iso,
            "fetch_to_ssot_ms": expected_with_timing.get("hard_source_fetch_to_ssot_ms"),
            "ssot_to_job_enqueue_ms": expected_with_timing.get("hard_source_ssot_to_job_enqueue_ms"),
            "primary_only_fast_path_allowed": bool(primary_only_fast_path_allowed),
        },
    }
    if isinstance(constrained_payload, Mapping):
        event_payload = dict(constrained_payload)
        event_payload["slug"] = slug
        event_payload["event_date"] = event_date
        event_payload["hard_source_direct_snapshot"] = True
        event_payload["hard_source_direct_snapshot_source"] = observation.get("source_id")
        event_payload["entry_expected_targets"] = [dict(target) for target in targets_with_timing]
        event_payload["hard_source_direct"] = dict(payload["hard_source_direct"])
        payload["event_payloads"] = [
            {
                "slug": slug,
                "event_date": event_date,
                "payload": event_payload,
                "source": DIRECT_SOURCE,
                "snapshot_semantics": "ssot_posterior_constraint",
            }
        ]
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO auto_trade_jobs(
            run_date, source, state, priority, payload_json,
            lease_until, retry_count, max_retry, next_retry_at, last_error,
            started_at, ended_at, run_id, created_at, updated_at
        )
        VALUES(?, ?, 'queued', ?, ?, '', 0, ?, ?, '', '', '', NULL, ?, ?)
        """,
        (
            run_date,
            DIRECT_SOURCE,
            int(priority),
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            max(0, _env_int("PM_HARD_SOURCE_DIRECT_JOB_MAX_RETRY", 1)),
            now_iso,
            now_iso,
            now_iso,
        ),
    )
    return int(cur.lastrowid or 0)


_POSTERIOR_TARGET_DIRECT_EVENT_KEYS = (
    "slug",
    "event_url",
    "title",
    "city",
    "station_id",
    "location_basis",
    "event_date",
    "run_date",
    "event_tz",
    "event_target_utc",
    "event_peak_time_utc",
    "event_end_utc",
    "market_var",
    "market_unit",
    "rounding_rule",
    "resolution_source",
    "settlement_actual",
    "market_closed",
    "rules_contract_ok",
    "rules_missing_reasons",
    *_POSTERIOR_TARGET_RULE_SOURCE_FIELDS,
    "posterior_target_bucket_selection",
    "posterior_temperature_distribution_source",
    "ctp_latest_materialized_at",
    "posterior_target_direct_inline_ctp_recomputed_at",
    "posterior_target_direct_inline_ctp_recompute_reason",
    "posterior_target_direct_source_fact_id",
    "posterior_target_direct_source_fact_as_of",
    "posterior_target_direct_source_fact_to_ctp_recompute_ms",
    "posterior_target_direct_ctp_recompute_ms",
    "fact_id",
    "fact_as_of",
)

_POSTERIOR_TARGET_DIRECT_ROW_KEYS = (
    "outcome",
    "label",
    "yes_token",
    "no_token",
    "token_id",
    "condition_id",
    "market_id",
    "market_slug",
    "market_url",
    "best_bid",
    "best_ask",
    "best_bid_no",
    "best_ask_no",
    "bid",
    "ask",
    "spread",
    "mid",
    "q_entry",
    "q_round",
    "q_raw",
    "q_calibrated",
    "q_quote",
    "q_bayes_posterior",
    "q_temperature_bucket_posterior",
    "q_contract_payout_posterior",
    "target_bucket_probability",
    "target_bucket_probability_field",
    *_POSTERIOR_TARGET_RULE_SOURCE_FIELDS,
    "market_active",
    "market_closed",
    "market_archived",
    "market_accepting_orders",
    "market_enable_orderbook",
)

_POSTERIOR_TARGET_DIRECT_TARGET_KEYS = (
    "slug",
    "event_slug",
    "event_date",
    "outcome",
    "side",
    "strategy",
    "strategy_source",
    "strategy_family",
    "entry_strategy_family",
    "weather_strategy_family",
    "entry_ok",
    "token_id",
    "yes_token",
    "no_token",
    "ask",
    "bid",
    "best_ask",
    "best_bid",
    "best_ask_no",
    "best_bid_no",
    "spread",
    "q_entry",
    "q_min_bound",
    "q_raw",
    "q_round",
    "q_calibrated",
    "q_quote",
    "q_bayes_posterior",
    "q_temperature_bucket_posterior",
    "q_contract_payout_posterior",
    "edge_min",
    "edge_score",
    "exec_edge_entry",
    "exec_edge_exit",
    "exec_edge_score",
    "edge_realizable",
    "edge_realizable_v2",
    "edge_realizable_score",
    "edge_tradeability_pass",
    "edge_tradeability_profit_value",
    "position_ratio",
    "order_size",
    "size",
    "posterior_target_direct",
    "posterior_target_update",
    "posterior_target_source",
    "posterior_target_bucket",
    "target_bucket",
    "ssot_target_bucket",
    "posterior_settlement_target_bucket",
    "posterior_settlement_current_top_bucket",
    "current_top_bucket",
    "new_target_bucket",
    "target_posterior",
    "posterior_settlement_target_posterior",
    "posterior_settlement_current_top_posterior",
    "current_top_posterior",
    "new_target_posterior",
    "target_bucket_posterior",
    "posterior_settlement_candidate_bucket",
    "posterior_settlement_candidate_bucket_prob",
    "target_realization_reliability",
    "target_realization_reliability_source",
    "posterior_settlement_entry_allowed",
    "posterior_settlement_entry_block_reason",
    "posterior_settlement_size_scale",
    "posterior_settlement_sort_score",
    "posterior_target_direct_path_follow_diagnostic_only",
    "posterior_target_remote_no_path_follow_diagnostic_only",
    "temperature_path_trade_follow_allowed",
    "temperature_path_trade_follow_reason",
    "temperature_path_trade_follow_allowed_before_posterior_target_direct",
    "temperature_path_trade_follow_reason_before_posterior_target_direct",
    "temperature_path_trade_follow_block_yes",
    "temperature_path_trade_follow_gate_source",
    "target_realization_trade_follow_allowed",
    "target_realization_trade_follow_reason",
    "decision_ssot",
    "target_adjustment_decision_version",
    "target_adjustment_action",
    "strategy_lane",
    "target_realization_leg_role",
    "target_realization_execution_action",
    "target_realization_position_effect",
    "target_realization_plan_action",
    "sell_old_buy_new_semantics",
    "remote_no_bucket",
    "remote_no_bucket_direction",
    "remote_no_bucket_distance",
    "remote_no_min_bucket_distance",
    "remote_no_max_bucket_distance",
    "remote_no_max_price",
    "remote_bucket_realization_prob",
    "held_target_bucket",
    "held_target_side",
    "old_bucket_reverse_side",
    "posterior_target_job_enqueued_at",
    "posterior_target_fact_id",
    "posterior_target_fact_as_of",
    "posterior_target_fact_to_job_enqueue_ms",
    "posterior_target_worker_started_at",
    "posterior_target_job_id",
    "posterior_target_job_enqueue_to_worker_ms",
    "posterior_target_fact_to_worker_ms",
    "source_kind",
    "source_confidence",
    "source_fit_tier",
    "source_trust_tier",
    "source_usable_for_settlement",
    "source_na_reason",
    "rules_contract_ok",
    "rules_missing_reasons",
    "settlement_station",
    "station_id",
    "weather_strategy_rules_ok",
    "weather_strategy_rules_na_reason",
    *_POSTERIOR_TARGET_RULE_SOURCE_FIELDS,
    "target_realization_sizing_active",
    "target_realization_plan_notional",
    "target_realization_plan_notional_source",
    "target_realization_bankroll_defaulted",
    "target_realization_bankroll_default_source",
    "market_active",
    "market_closed",
    "market_archived",
    "market_accepting_orders",
    "market_enable_orderbook",
)

_POSTERIOR_TARGET_DIRECT_TARGET_LIST_KEYS = (
    "entry_reasons",
    "target_adjustment_reason_codes",
    "rules_missing_reasons",
)

_POSTERIOR_TARGET_DIRECT_SELECTION_KEYS = (
    "target_bucket",
    "target_probability",
    "target_probability_pct",
    "target_probability_field",
    "target_bucket_probability",
    "target_bucket_probability_pct",
    "target_bucket_probability_field",
    "posterior_highest_bucket_label",
    "posterior_highest_probability",
    "posterior_highest_probability_pct",
    "second_target_bucket",
    "second_target_probability",
    "second_target_probability_pct",
    "selection_priority",
    "selection_runtime_recomputed",
    "ssot_source",
    "target_bucket_selection_source",
    "target_selection_priority_version",
)


def _compact_json_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _compact_mapping_scalars(
    payload: Mapping[str, Any],
    keys: Tuple[str, ...],
) -> Dict[str, Any]:
    return {
        key: payload.get(key)
        for key in keys
        if key in payload and _compact_json_scalar(payload.get(key))
    }


def _compact_posterior_target_expected_target(target: Mapping[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {}
    for key in _POSTERIOR_TARGET_DIRECT_TARGET_KEYS:
        if key not in target:
            continue
        value = target.get(key)
        if _compact_json_scalar(value):
            compact[key] = value
        elif key in _POSTERIOR_TARGET_DIRECT_TARGET_LIST_KEYS and isinstance(value, list):
            compact[key] = [
                item
                for item in value[:30]
                if _compact_json_scalar(item)
            ]
    for key in _POSTERIOR_TARGET_DIRECT_TARGET_LIST_KEYS:
        if key in compact or key not in target:
            continue
        value = target.get(key)
        if isinstance(value, list):
            compact[key] = [
                item
                for item in value[:30]
                if _compact_json_scalar(item)
            ]
    return compact


def _compact_posterior_target_expected_targets(
    targets: List[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    compacted: List[Dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        compact = _compact_posterior_target_expected_target(target)
        outcome = str(compact.get("outcome") or "").strip()
        side = str(compact.get("side") or "").strip().upper()
        if not outcome or side not in {"YES", "NO"}:
            continue
        compact["side"] = side
        compacted.append(compact)
    return compacted


def _compact_posterior_target_event_payload(
    snapshot_payload: Mapping[str, Any],
    *,
    slug: str,
    event_date: str,
    targets: List[Mapping[str, Any]],
    direct_block: Mapping[str, Any],
) -> Dict[str, Any]:
    compact: Dict[str, Any] = {
        key: snapshot_payload.get(key)
        for key in _POSTERIOR_TARGET_DIRECT_EVENT_KEYS
        if key != "posterior_target_bucket_selection"
        and snapshot_payload.get(key) not in (None, "")
        and (
            _compact_json_scalar(snapshot_payload.get(key))
            or (
                isinstance(snapshot_payload.get(key), list)
                and all(_compact_json_scalar(item) for item in snapshot_payload.get(key, [])[:30])
            )
        )
    }
    selection = snapshot_payload.get("posterior_target_bucket_selection")
    if isinstance(selection, Mapping):
        compact_selection = _compact_mapping_scalars(selection, _POSTERIOR_TARGET_DIRECT_SELECTION_KEYS)
        if compact_selection:
            compact["posterior_target_bucket_selection"] = compact_selection
    compact["slug"] = slug
    compact["event_date"] = event_date
    target_outcomes = {
        str(target.get("outcome") or "").strip()
        for target in targets
        if str(target.get("outcome") or "").strip()
    }
    rows_raw = snapshot_payload.get("rows")
    if not isinstance(rows_raw, list):
        rows_raw = snapshot_payload.get("distribution")
    compact_rows: List[Dict[str, Any]] = []
    if isinstance(rows_raw, list):
        for raw_row in rows_raw:
            if not isinstance(raw_row, Mapping):
                continue
            outcome = str(raw_row.get("outcome") or raw_row.get("label") or "").strip()
            if target_outcomes and outcome not in target_outcomes:
                continue
            row = {
                key: raw_row.get(key)
                for key in _POSTERIOR_TARGET_DIRECT_ROW_KEYS
                if raw_row.get(key) not in (None, "")
            }
            if outcome and not row.get("outcome"):
                row["outcome"] = outcome
            if row:
                compact_rows.append(row)
    if not compact_rows:
        for target in targets:
            outcome = str(target.get("outcome") or "").strip()
            if not outcome:
                continue
            compact_rows.append(
                {
                    key: target.get(key)
                    for key in _POSTERIOR_TARGET_DIRECT_ROW_KEYS
                    if target.get(key) not in (None, "")
                }
            )
            compact_rows[-1]["outcome"] = outcome
    compact["rows"] = compact_rows
    compact["posterior_target_direct_snapshot"] = True
    compact["posterior_target_direct_snapshot_source"] = POSTERIOR_TARGET_SOURCE
    compact["entry_expected_targets"] = _compact_posterior_target_expected_targets(targets)
    compact["posterior_target_direct"] = dict(direct_block)
    compact["job_payload_compact"] = True
    compact["job_payload_compact_source"] = "posterior_target_direct_event_payload_v1"
    return compact


def _enqueue_posterior_target_job(
    conn: sqlite3.Connection,
    *,
    run_date: str,
    slug: str,
    event_date: str,
    expected_target: Mapping[str, Any],
    expected_targets: Optional[List[Mapping[str, Any]]] = None,
    snapshot_payload: Optional[Mapping[str, Any]] = None,
    fact_id: Optional[int] = None,
    fact_as_of: str = "",
    priority: int,
    old_exit_cleanup_triggered: bool = False,
    exit_only: bool = False,
    primary_only_fast_path_allowed: Optional[bool] = None,
) -> Optional[int]:
    now_iso = _utc_now_iso()
    expected_with_timing = dict(expected_target)
    expected_with_timing["posterior_target_job_enqueued_at"] = now_iso
    expected_with_timing["posterior_target_fact_id"] = int(fact_id or 0) if fact_id else None
    expected_with_timing["posterior_target_fact_as_of"] = fact_as_of
    expected_with_timing["posterior_target_fact_to_job_enqueue_ms"] = _latency_ms_between(
        fact_as_of,
        now_iso,
    )
    targets_with_timing: List[Dict[str, Any]] = [expected_with_timing]
    primary_key = (
        str(expected_with_timing.get("outcome") or "").strip(),
        str(expected_with_timing.get("side") or "").strip().upper(),
    )
    seen_keys = {primary_key}
    for raw_target in expected_targets or []:
        target = dict(raw_target)
        key = (
            str(target.get("outcome") or "").strip(),
            str(target.get("side") or "").strip().upper(),
        )
        if not key[0] or key[1] not in {"YES", "NO"} or key in seen_keys:
            continue
        seen_keys.add(key)
        target["posterior_target_job_enqueued_at"] = now_iso
        target["posterior_target_fact_id"] = int(fact_id or 0) if fact_id else None
        target["posterior_target_fact_as_of"] = fact_as_of
        target["posterior_target_fact_to_job_enqueue_ms"] = _latency_ms_between(
            fact_as_of,
            now_iso,
        )
        targets_with_timing.append(target)
    targets_with_timing_compact = _compact_posterior_target_expected_targets(targets_with_timing)
    if primary_only_fast_path_allowed is None:
        primary_only_fast_path_allowed = not bool(
            old_exit_cleanup_triggered
            or exit_only
            or len(targets_with_timing_compact) > 1
        )
    payload = {
        "include_event_keys": [{"slug": slug, "event_date": event_date}],
        "entry_expected_targets": targets_with_timing_compact,
        "posterior_target_direct": {
            "source": POSTERIOR_TARGET_SOURCE,
            "decision_semantics": "ssot_posterior_target_probability_change",
            "decision_ssot": "posterior_temperature_distribution",
            "target_bucket": expected_with_timing.get("target_bucket")
            or expected_with_timing.get("outcome"),
            "target_posterior": expected_with_timing.get("target_posterior"),
            "target_source": expected_with_timing.get("posterior_target_source"),
            "fact_id": int(fact_id or 0) if fact_id else None,
            "fact_as_of": fact_as_of,
            "job_enqueued_at": now_iso,
            "fact_to_job_enqueue_ms": expected_with_timing.get(
                "posterior_target_fact_to_job_enqueue_ms"
            ),
            "primary_only_fast_path_allowed": bool(primary_only_fast_path_allowed),
        },
    }
    if exit_only:
        payload["exit_only"] = True
    if old_exit_cleanup_triggered or exit_only:
        payload["exit_only"] = bool(exit_only)
        payload["old_exit_cleanup"] = True
        payload["posterior_target_direct"]["old_exit_cleanup_triggered"] = True
        payload["posterior_target_direct"]["entry_blocked_exit_old_only"] = bool(
            exit_only and not old_exit_cleanup_triggered
        )
        payload["posterior_target_direct"]["old_exit_cleanup_reason"] = (
            "active_old_position_after_unchanged_target"
            if old_exit_cleanup_triggered
            else "entry_blocked_exit_old_only"
        )
        if exit_only:
            payload["posterior_target_direct"]["decision_semantics"] = (
                "ssot_posterior_target_old_exit_cleanup"
                if old_exit_cleanup_triggered
                else "ssot_posterior_target_entry_blocked_exit_old_only"
            )
    if isinstance(snapshot_payload, Mapping):
        event_payload = _compact_posterior_target_event_payload(
            snapshot_payload,
            slug=slug,
            event_date=event_date,
            targets=targets_with_timing_compact,
            direct_block=payload["posterior_target_direct"],
        )
        payload["event_payloads"] = [
            {
                "slug": slug,
                "event_date": event_date,
                "payload": event_payload,
                "source": POSTERIOR_TARGET_SOURCE,
                "snapshot_semantics": "ssot_posterior_target_probability_change",
            }
        ]
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO auto_trade_jobs(
            run_date, source, state, priority, payload_json,
            lease_until, retry_count, max_retry, next_retry_at, last_error,
            started_at, ended_at, run_id, created_at, updated_at
        )
        VALUES(?, ?, 'queued', ?, ?, '', 0, ?, ?, '', '', '', NULL, ?, ?)
        """,
        (
            run_date,
            POSTERIOR_TARGET_SOURCE,
            int(priority),
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            max(0, _env_int("PM_POSTERIOR_TARGET_DIRECT_JOB_MAX_RETRY", 1)),
            now_iso,
            now_iso,
            now_iso,
        ),
    )
    return int(cur.lastrowid or 0)


def record_posterior_target_direct_decisions(
    db_path: Path | str,
    *,
    event_date: str,
    slug: str = "",
    emit_unchanged_sec: int = 10,
    enqueue_job: bool = True,
) -> Dict[str, Any]:
    db = Path(db_path)
    event_key = _normalize_date(event_date) or _chengdu_today()
    run_date = _run_date_et()
    decision_started_at = _utc_now_iso()
    min_delta = _env_float("PM_POSTERIOR_TARGET_DIRECT_MIN_PROB_DELTA", 0.001)
    if min_delta < 0:
        min_delta = 0.0
    with _connect(db) as conn:
        try:
            latest, payload, payload_source = _load_latest_fact_snapshot_for_posterior_target(
                conn,
                event_date=event_key,
                slug=slug,
            )
        except sqlite3.Error as exc:
            return {
                "ok": False,
                "status": "latest_fact_missing",
                "event_date": event_key,
                "error": f"{exc.__class__.__name__}: {exc}",
                "retryable": True,
            }
        if latest is None:
            return {"ok": False, "status": "latest_fact_missing", "event_date": event_key}
        if not payload:
            return {"ok": False, "status": "latest_fact_json_invalid", "event_date": event_key}
        slug_key = str(latest["slug"] or payload.get("slug") or slug or "").strip()
        event_key = _normalize_date(latest["event_date"] or payload.get("event_date") or event_key)
        snapshot_payload = dict(payload)
        snapshot_payload.setdefault("slug", slug_key)
        snapshot_payload.setdefault("event_date", event_key)
        snapshot_payload.setdefault("run_date", latest["run_date"] or event_key)
        snapshot_payload["fact_id"] = int(latest["id"] or 0)
        snapshot_payload["fact_as_of"] = str(latest["fact_as_of"] or "")
        snapshot_payload["posterior_target_direct_latest_payload_source"] = payload_source
        if _truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_DECISION_WINDOW_ENABLED"), False):
            window = _decision_window_state(snapshot_payload, event_date=event_key)
            if not bool(window.get("allowed")):
                return {
                    "ok": True,
                    "status": "outside_decision_window",
                    "event_date": event_key,
                    "slug": slug_key,
                    "decision_window": window,
                }
        if _truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_UI_ALIGNMENT_ENABLED"), False):
            try:
                from ui.server import (
                    _align_snapshot_posterior_derived_fields,
                    _attach_target_realization_capital_plan,
                )

                _align_snapshot_posterior_derived_fields(snapshot_payload)
                _attach_target_realization_capital_plan(snapshot_payload)
                _align_snapshot_posterior_derived_fields(snapshot_payload)
            except Exception as exc:
                snapshot_payload["posterior_target_direct_alignment_error"] = (
                    f"{exc.__class__.__name__}: {exc}"
                )
        else:
            snapshot_payload["posterior_target_direct_alignment_skipped"] = (
                "ui_alignment_disabled_fast_path"
            )
        target_bucket, target_posterior, target_source = _canonical_target_from_payload(
            snapshot_payload
        )
        if not target_bucket:
            return {
                "ok": False,
                "status": "posterior_target_missing",
                "event_date": event_key,
                "slug": slug_key,
            }
        top_row = _row_for_outcome(snapshot_payload, target_bucket) or _top_posterior_row(
            _rows(snapshot_payload)
        )
        if target_posterior is None:
            target_posterior = _prob01(top_row.get("q_temperature_bucket_posterior"))
        if target_posterior is None:
            target_posterior = _prob01(top_row.get("q_bayes_posterior"))
        previous = _last_posterior_target_entry(conn, slug=slug_key, event_date=event_key)
        previous_bucket = str(previous.get("outcome") or "")
        previous_details = previous.get("details") if isinstance(previous.get("details"), Mapping) else {}
        previous_posterior = None
        if isinstance(previous_details, Mapping):
            previous_posterior = _prob01(previous_details.get("target_posterior"))
            if previous_posterior is None:
                previous_posterior = _prob01(previous_details.get("q_temperature_bucket_posterior"))
        bucket_changed = bool(previous_bucket and previous_bucket != target_bucket)
        probability_delta = None
        probability_changed = False
        if target_posterior is not None and previous_posterior is not None:
            probability_delta = float(target_posterior) - float(previous_posterior)
            probability_changed = abs(probability_delta) >= float(min_delta)
        elif target_posterior is not None and previous_posterior is None:
            probability_changed = True
        target_entry_already_active = _posterior_target_has_active_entry(
            conn,
            slug=slug_key,
            event_date=event_key,
            outcome=target_bucket,
            side="YES",
        )
        primary_target_entry_needed = not target_entry_already_active
        if _truthy(
            os.environ.get("PM_POSTERIOR_TARGET_DIRECT_FAST_RETRY_BLOCKED_DECISION"),
            True,
        ):
            if not target_entry_already_active:
                previous_place_order_retry_block = (
                    _posterior_target_previous_place_order_retry_block(
                        conn,
                        slug=slug_key,
                        event_date=event_key,
                        outcome=target_bucket,
                        side="YES",
                    )
                )
                if previous_place_order_retry_block.get("blocked"):
                    now_iso = _utc_now_iso()
                    block_reason = str(
                        previous_place_order_retry_block.get("reason")
                        or "posterior_target_previous_place_order_failure_retry_blocked"
                    )
                    reason_codes = [
                        "posterior_target_direct",
                        "target_realization_order_intent",
                    ]
                    if bucket_changed:
                        reason_codes.append("ssot_target_bucket_changed")
                    if probability_changed:
                        reason_codes.append("posterior_target_probability_changed")
                    if not previous_bucket:
                        reason_codes.append("posterior_target_initial")
                    reason_codes.append(block_reason)
                    title = str(
                        snapshot_payload.get("title")
                        or snapshot_payload.get("question")
                        or slug_key
                    )
                    details = {
                        "source": POSTERIOR_TARGET_SOURCE,
                        "posterior_target_direct": True,
                        "posterior_target_update": True,
                        "decision_basis": "posterior_target_probability_change",
                        "decision_ssot": "posterior_temperature_distribution",
                        "target_adjustment_decision_version": "posterior_target_direct_v1",
                        "target_adjustment_action": "POSTERIOR_TARGET_UPDATE",
                        "target_adjustment_reason_codes": list(reason_codes),
                        "target_bucket": target_bucket,
                        "ssot_target_bucket": target_bucket,
                        "target_posterior": target_posterior,
                        "q_bayes_posterior": target_posterior,
                        "q_temperature_bucket_posterior": target_posterior,
                        "new_target_posterior": target_posterior,
                        "target_source": target_source,
                        "previous_bucket": previous_bucket,
                        "previous_posterior": previous_posterior,
                        "bucket_changed": bucket_changed,
                        "probability_changed": probability_changed,
                        "probability_delta": probability_delta,
                        "min_probability_delta": float(min_delta),
                        "order_update_triggered": True,
                        "entry_order_update_triggered": True,
                        "target_entry_already_active": False,
                        "primary_target_entry_needed": False,
                        "target_execution_blocked": True,
                        "target_execution_block_reason": block_reason,
                        "previous_place_order_retry_block": dict(
                            previous_place_order_retry_block
                        ),
                        "job_skipped_reason": block_reason,
                        "old_exit_cleanup_triggered": False,
                        "old_exit_cleanup_recent": False,
                        "old_exit_cleanup_deferred_for_primary_entry": False,
                        "entry_blocked_exit_old_only": False,
                        "duplicate_job_for_fact": False,
                        "fact_id": int(latest["id"] or 0),
                        "fact_as_of": str(latest["fact_as_of"] or ""),
                        "decision_started_at": decision_started_at,
                        "posterior_target_direct_latest_payload_source": payload_source,
                        "posterior_target_direct_fast_retry_blocked_decision": True,
                        "expected_target": {
                            "slug": slug_key,
                            "event_date": event_key,
                            "outcome": target_bucket,
                            "side": "YES",
                            "target_bucket": target_bucket,
                            "ssot_target_bucket": target_bucket,
                            "target_posterior": target_posterior,
                            "q_bayes_posterior": target_posterior,
                            "q_temperature_bucket_posterior": target_posterior,
                            "target_execution_blocked": True,
                            "target_execution_block_reason": block_reason,
                        },
                        "entry_expected_targets": [],
                        "remote_no_entry_targets": [],
                        "posterior_target_direct_precheck_suppressed_old_exit_count": 0,
                        "posterior_target_direct_precheck_suppressed_remote_no_count": 0,
                    }
                    decision_id = _insert_decision(
                        conn,
                        run_date=run_date,
                        slug=slug_key,
                        event_date=event_key,
                        title=title,
                        outcome=target_bucket,
                        side="YES",
                        action="entry",
                        decided_state="blocked",
                        will_order=False,
                        reason_codes=reason_codes,
                        reasons=[
                            "SSOT posterior target still requires entry, but the latest real place-order attempt is retry-blocked."
                        ],
                        details=details,
                        created_at=now_iso,
                    )
                    conn.commit()
                    return {
                        "ok": True,
                        "status": "recorded",
                        "slug": slug_key,
                        "event_date": event_key,
                        "target_bucket": target_bucket,
                        "target_posterior": target_posterior,
                        "target_source": target_source,
                        "previous_bucket": previous_bucket,
                        "previous_posterior": previous_posterior,
                        "bucket_changed": bucket_changed,
                        "probability_changed": probability_changed,
                        "probability_delta": probability_delta,
                        "entry_order_update_triggered": True,
                        "old_exit_cleanup_triggered": False,
                        "old_exit_cleanup_recent": False,
                        "old_exit_cleanup_deferred_for_primary_entry": False,
                        "decision_ids": [decision_id],
                        "old_exit_count": 0,
                        "remote_no_entry_count": 0,
                        "job_id": None,
                        "job_enqueued": False,
                        "job_enqueue_eligible": False,
                        "job_enqueue_attempted": False,
                        "job_enqueue_error": "",
                        "job_skipped_reason": block_reason,
                        "fact_id": int(latest["id"] or 0),
                        "fact_as_of": str(latest["fact_as_of"] or ""),
                        "decision_started_at": decision_started_at,
                        "decision_created_at": now_iso,
                        "latest_fact_payload_source": payload_source,
                        "fast_retry_blocked_decision": True,
                    }
        old_exit_cleanup_on_unchanged = _truthy(
            os.environ.get("PM_POSTERIOR_TARGET_DIRECT_OLD_EXIT_CLEANUP_ON_UNCHANGED"),
            True,
        )
        allowed_remote_no_outcomes = _posterior_target_allowed_remote_no_outcomes(
            snapshot_payload,
            target_bucket=target_bucket,
        )
        old_trades = _active_old_yes_trades(
            conn,
            slug=slug_key,
            event_date=event_key,
            target_outcome=target_bucket,
            target_side="YES",
            allowed_remote_no_outcomes=allowed_remote_no_outcomes,
        )
        secondary_plan_actionable = _posterior_target_plan_has_actionable_secondary(
            snapshot_payload
        )
        unchanged_heartbeat_due = bool(
            not _truthy(
                os.environ.get("PM_POSTERIOR_TARGET_DIRECT_RECORD_UNCHANGED_HEARTBEAT"),
                False,
            )
            or _recent_same_posterior_target_entry_exists(
                conn,
                slug=slug_key,
                event_date=event_key,
                outcome=target_bucket,
                target_posterior=target_posterior,
                within_sec=emit_unchanged_sec,
                epsilon=max(float(min_delta), 1e-9),
            )
        )
        if (
            previous_bucket
            and not bucket_changed
            and not probability_changed
            and not primary_target_entry_needed
            and not old_exit_cleanup_on_unchanged
            and not old_trades
            and not secondary_plan_actionable
            and unchanged_heartbeat_due
        ):
            return {
                "ok": True,
                "status": "unchanged",
                "event_date": event_key,
                "slug": slug_key,
                "target_bucket": target_bucket,
                "target_posterior": target_posterior,
                "previous_bucket": previous_bucket,
                "previous_posterior": previous_posterior,
                "probability_delta": probability_delta,
                "job_skipped_reason": "posterior_target_unchanged_no_job",
                "fact_id": int(latest["id"] or 0),
                "latest_fact_payload_source": payload_source,
                "target_entry_already_active": True,
                "old_exit_count": None,
                "old_exit_count_na_reason": "skipped_fast_unchanged_primary_active",
                "old_exit_cleanup_triggered": False,
                "old_exit_cleanup_recent": False,
                "old_exit_cleanup_on_unchanged": False,
                "secondary_plan_actionable": False,
            }
        old_exit_cleanup_cooldown_sec = _env_int(
            "PM_POSTERIOR_TARGET_DIRECT_OLD_EXIT_CLEANUP_COOLDOWN_SEC",
            1800,
        )
        if old_exit_cleanup_cooldown_sec is None or old_exit_cleanup_cooldown_sec < 0:
            old_exit_cleanup_cooldown_sec = 600
        old_exit_cleanup_recent = False
        old_exit_cleanup_triggered = bool(
            enqueue_job
            and old_trades
            and previous_bucket
            and not bucket_changed
            and not probability_changed
            and old_exit_cleanup_on_unchanged
        )
        if old_exit_cleanup_triggered:
            old_exit_cleanup_recent = _recent_posterior_target_old_exit_cleanup_job_exists(
                conn,
                slug=slug_key,
                event_date=event_key,
                target_bucket=target_bucket,
                within_sec=int(old_exit_cleanup_cooldown_sec or 0),
            )
            if old_exit_cleanup_recent:
                old_exit_cleanup_triggered = False
        secondary_plan_deferred_for_active_target = bool(
            target_entry_already_active
            and not primary_target_entry_needed
            and secondary_plan_actionable
            and not bucket_changed
            and not probability_changed
            and not old_exit_cleanup_triggered
        )
        if secondary_plan_deferred_for_active_target:
            secondary_plan_actionable = False
        if (
            previous_bucket
            and not bucket_changed
            and not probability_changed
            and not old_exit_cleanup_triggered
            and not secondary_plan_actionable
            and not primary_target_entry_needed
            and unchanged_heartbeat_due
        ):
            return {
                "ok": True,
                "status": "unchanged",
                "event_date": event_key,
                "slug": slug_key,
                "target_bucket": target_bucket,
                "target_posterior": target_posterior,
                "previous_bucket": previous_bucket,
                "previous_posterior": previous_posterior,
                "probability_delta": probability_delta,
                "job_skipped_reason": "posterior_target_unchanged_no_job",
                "fact_id": int(latest["id"] or 0),
                "latest_fact_payload_source": payload_source,
                "old_exit_count": len(old_trades),
                "old_exit_cleanup_recent": bool(old_exit_cleanup_recent),
                "old_exit_cleanup_on_unchanged": bool(old_exit_cleanup_on_unchanged),
                "secondary_plan_actionable": False,
                "secondary_plan_deferred_for_active_target": bool(
                    secondary_plan_deferred_for_active_target
                ),
            }
        token = _market_token(conn, slug=slug_key, event_date=event_key, outcome=target_bucket)
        tick = _latest_market_tick(
            conn,
            slug=slug_key,
            event_date=event_key,
            outcome=target_bucket,
            side="YES",
        )
        expected = _build_posterior_target_expected_target(
            slug=slug_key,
            event_date=event_key,
            outcome=target_bucket,
            row=top_row,
            tick=tick,
            token=token,
            payload=snapshot_payload,
            target_bucket=target_bucket,
            target_posterior=target_posterior,
            target_source=target_source or "posterior_temperature_distribution",
        )
        target_execution_block_reason = ""
        target_execution_blocked = False
        previous_place_order_retry_block: Dict[str, Any] = {"blocked": False}
        if expected.get("target_realization_trade_follow_allowed") is False:
            target_execution_blocked = True
            target_execution_block_reason = str(
                expected.get("target_realization_trade_follow_reason")
                or expected.get("posterior_settlement_entry_block_reason")
                or "target_realization_trade_follow_not_allowed"
            )
        elif expected.get("posterior_settlement_entry_allowed") is False:
            target_execution_blocked = True
            target_execution_block_reason = str(
                expected.get("posterior_settlement_entry_block_reason")
                or "posterior_settlement_entry_not_allowed"
            )
        if not target_entry_already_active:
            previous_place_order_retry_block = _posterior_target_previous_place_order_retry_block(
                conn,
                slug=slug_key,
                event_date=event_key,
                outcome=target_bucket,
                side="YES",
            )
            if previous_place_order_retry_block.get("blocked"):
                target_execution_blocked = True
                target_execution_block_reason = str(
                    previous_place_order_retry_block.get("reason")
                    or "posterior_target_previous_place_order_failure_retry_blocked"
                )
                old_exit_cleanup_triggered = False
                old_exit_cleanup_recent = False
        if target_execution_blocked:
            primary_target_entry_needed = False
        old_exit_cleanup_deferred_for_primary_entry = False
        if (
            old_exit_cleanup_triggered
            and primary_target_entry_needed
            and not target_execution_blocked
        ):
            old_exit_cleanup_triggered = False
            old_exit_cleanup_deferred_for_primary_entry = True
        entry_order_update_triggered = bool(
            bucket_changed
            or probability_changed
            or not previous_bucket
            or primary_target_entry_needed
            or previous_place_order_retry_block.get("blocked")
        )
        secondary_entry_update_triggered = bool(
            secondary_plan_actionable
            and not previous_place_order_retry_block.get("blocked")
        )
        order_update_triggered = bool(
            entry_order_update_triggered
            or old_exit_cleanup_triggered
            or secondary_entry_update_triggered
        )
        reason_codes = ["posterior_target_direct"]
        if entry_order_update_triggered:
            reason_codes.append("target_realization_order_intent")
        elif old_exit_cleanup_triggered:
            reason_codes.append("posterior_target_old_exit_cleanup")
        elif secondary_entry_update_triggered:
            reason_codes.append("posterior_target_secondary_plan_adjustment")
        else:
            reason_codes.append("posterior_target_unchanged")
        if bucket_changed:
            reason_codes.append("ssot_target_bucket_changed")
        if probability_changed:
            reason_codes.append("posterior_target_probability_changed")
        if not previous_bucket:
            reason_codes.append("posterior_target_initial")
        if primary_target_entry_needed:
            reason_codes.append("posterior_target_active_entry_missing")
        expected = _apply_expected_entry_audit(expected, reason_codes=reason_codes)
        primary_only_precheck = _truthy(
            os.environ.get("PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_PRECHECK"),
            True,
        )
        primary_only_job_payload = _truthy(
            os.environ.get("PM_POSTERIOR_TARGET_DIRECT_PRIMARY_ONLY_JOB_PAYLOAD"),
            False,
        ) and bool(entry_order_update_triggered)
        build_secondary_targets = not primary_only_job_payload
        reverse_targets: List[Dict[str, Any]] = []
        if build_secondary_targets:
            for trade in old_trades:
                old_outcome = str(trade.get("outcome") or "").strip()
                old_side = str(trade.get("side") or "").strip().upper()
                reverse_side = _opposite_trade_side(old_side)
                if not old_outcome or not reverse_side:
                    continue
                old_row = _row_for_outcome(snapshot_payload, old_outcome)
                old_token = _market_token(
                    conn,
                    slug=slug_key,
                    event_date=event_key,
                    outcome=old_outcome,
                )
                old_tick = _latest_market_tick(
                    conn,
                    slug=slug_key,
                    event_date=event_key,
                    outcome=old_outcome,
                    side=reverse_side,
                )
                reverse_target = _build_posterior_target_expected_target(
                    slug=slug_key,
                    event_date=event_key,
                    outcome=old_outcome,
                    row=old_row,
                    tick=old_tick,
                    token=old_token,
                    payload=snapshot_payload,
                    target_bucket=target_bucket,
                    target_posterior=target_posterior,
                    target_source=target_source or "posterior_temperature_distribution",
                    side=reverse_side,
                    leg_role="old_bucket_reverse",
                    entry_reasons=[
                        "posterior_target_direct",
                        "target_realization_order_intent",
                        "target_realization_switch_old_bucket_reverse",
                    ],
                    held_target_bucket=old_outcome,
                    held_target_side=old_side,
                )
                reverse_targets.append(reverse_target)
        remote_no_targets = (
            _posterior_target_remote_no_expected_targets(
                conn,
                slug=slug_key,
                event_date=event_key,
                snapshot_payload=snapshot_payload,
                target_bucket=target_bucket,
                target_posterior=target_posterior,
                target_source=target_source or "posterior_temperature_distribution",
            )
            if build_secondary_targets
            else []
        )
        entry_blocked_exit_old_only = bool(
            target_execution_blocked
            and old_trades
            and order_update_triggered
            and not previous_place_order_retry_block.get("blocked")
        )
        cleanup_only_job = bool(
            (old_exit_cleanup_triggered and not entry_order_update_triggered)
            or entry_blocked_exit_old_only
        )
        cleanup_only_job_allowed = _truthy(
            os.environ.get("PM_POSTERIOR_TARGET_DIRECT_ENQUEUE_CLEANUP_ONLY"),
            True,
        )
        suppress_old_exit_decision_rows = bool(
            primary_only_precheck
            and old_trades
            and not old_exit_cleanup_triggered
            and enqueue_job
            and _truthy(os.environ.get("PM_AUTO_TRADE_DRY_RUN"), True)
            and not _truthy(
                os.environ.get("PM_HARD_SOURCE_DIRECT_DECISION_WINDOW_ENABLED"),
                False,
            )
            and payload_source != "summary_cache"
        )
        entry_targets = [
            dict(expected),
            *[dict(target) for target in reverse_targets],
            *[dict(target) for target in remote_no_targets],
        ]
        visible_entry_targets = [dict(expected)] if primary_only_precheck else entry_targets
        visible_remote_no_targets = [] if primary_only_precheck else [
            dict(target) for target in remote_no_targets
        ]
        now_iso = _utc_now_iso()
        title = str(snapshot_payload.get("title") or snapshot_payload.get("question") or slug_key)
        common_details = {
            "source": POSTERIOR_TARGET_SOURCE,
            "posterior_target_direct": True,
            "posterior_target_update": True,
            "decision_basis": "posterior_target_probability_change",
            "decision_ssot": "posterior_temperature_distribution",
            "target_adjustment_decision_version": "posterior_target_direct_v1",
            "target_adjustment_action": "POSTERIOR_TARGET_UPDATE",
            "target_adjustment_reason_codes": list(reason_codes),
            "target_bucket": target_bucket,
            "ssot_target_bucket": target_bucket,
            "target_posterior": target_posterior,
            "q_bayes_posterior": expected.get("q_bayes_posterior"),
            "q_temperature_bucket_posterior": expected.get("q_temperature_bucket_posterior"),
            "new_target_posterior": expected.get("new_target_posterior"),
            "target_source": target_source,
            "previous_bucket": previous_bucket,
            "previous_posterior": previous_posterior,
            "bucket_changed": bucket_changed,
            "probability_changed": probability_changed,
            "probability_delta": probability_delta,
            "min_probability_delta": float(min_delta),
            "order_update_triggered": bool(order_update_triggered),
            "entry_order_update_triggered": bool(entry_order_update_triggered),
            "secondary_entry_update_triggered": bool(secondary_entry_update_triggered),
            "secondary_plan_actionable": bool(secondary_plan_actionable),
            "target_entry_already_active": bool(target_entry_already_active),
            "primary_target_entry_needed": bool(primary_target_entry_needed),
            "target_execution_blocked": bool(target_execution_blocked),
            "target_execution_block_reason": target_execution_block_reason,
            "previous_place_order_retry_block": dict(previous_place_order_retry_block),
            "entry_blocked_exit_old_only": bool(entry_blocked_exit_old_only),
            "old_exit_cleanup_triggered": bool(old_exit_cleanup_triggered),
            "old_exit_cleanup_recent": bool(old_exit_cleanup_recent),
            "old_exit_cleanup_deferred_for_primary_entry": bool(
                old_exit_cleanup_deferred_for_primary_entry
            ),
            "old_exit_cleanup_deferred_reason": (
                "primary_target_entry_missing_priority"
                if old_exit_cleanup_deferred_for_primary_entry
                else ""
            ),
            "old_exit_cleanup_cooldown_sec": int(old_exit_cleanup_cooldown_sec or 0),
            "allowed_remote_no_outcomes": list(allowed_remote_no_outcomes),
            "fact_id": int(latest["id"] or 0),
            "fact_as_of": str(latest["fact_as_of"] or ""),
            "decision_started_at": decision_started_at,
            "expected_target": dict(expected),
            "entry_expected_targets": visible_entry_targets,
            "remote_no_entry_targets": visible_remote_no_targets,
            "posterior_target_direct_primary_only_precheck": bool(primary_only_precheck),
            "posterior_target_direct_primary_only_job_payload": bool(primary_only_job_payload),
            "posterior_target_direct_cleanup_only_job": bool(cleanup_only_job),
            "posterior_target_direct_cleanup_only_job_allowed": bool(cleanup_only_job_allowed),
            "posterior_target_direct_precheck_suppressed_old_exit_count": (
                len(old_trades) if suppress_old_exit_decision_rows else 0
            ),
            "posterior_target_direct_precheck_suppressed_remote_no_count": (
                len(remote_no_targets) if primary_only_precheck else 0
            ),
            "posterior_target_direct_job_payload_deferred_old_exit_count": (
                len(old_trades) if primary_only_job_payload else 0
            ),
            "posterior_target_direct_job_payload_deferred_remote_no_count": (
                0 if build_secondary_targets else None
            ),
            "sell_old_buy_new_semantics": "sell_old_position_side_then_buy_new_and_optional_old_opposite_side",
            "sell_old_buy_new_entry_legs": visible_entry_targets,
        }
        common_details.update(_decision_ssot_context_from_payload(snapshot_payload))
        common_details.update(
            {
                key: value
                for key, value in expected.items()
                if key.startswith("entry_usable_edge")
                or key.startswith("entry_edge_")
                or key.startswith("posterior_settlement_")
                or key in {
                    "target_realization_reliability",
                    "target_realization_reliability_source",
                    "strategy_lane",
                    "posterior_target_direct_path_follow_diagnostic_only",
                    "posterior_target_remote_no_path_follow_diagnostic_only",
                    "temperature_path_trade_follow_allowed",
                    "temperature_path_trade_follow_reason",
                    "temperature_path_trade_follow_allowed_before_posterior_target_direct",
                    "temperature_path_trade_follow_reason_before_posterior_target_direct",
                    "target_realization_trade_follow_allowed",
                    "target_realization_trade_follow_reason",
                }
            }
        )
        job_id: Optional[int] = None
        job_skipped_reason = ""
        job_enqueue_attempted = False
        job_enqueue_error = ""
        duplicate_job_for_fact = False
        if previous_place_order_retry_block.get("blocked"):
            job_skipped_reason = (
                target_execution_block_reason
                or "posterior_target_previous_place_order_failure_retry_blocked"
            )
        elif not order_update_triggered:
            job_skipped_reason = "posterior_target_unchanged_no_job"
        elif target_execution_blocked and (
            not old_trades or previous_place_order_retry_block.get("blocked")
        ):
            job_skipped_reason = (
                target_execution_block_reason
                or "target_realization_trade_follow_not_allowed"
            )
        elif cleanup_only_job and not cleanup_only_job_allowed:
            job_skipped_reason = "posterior_target_cleanup_only_job_deferred"
        elif old_exit_cleanup_triggered and not entry_order_update_triggered:
            duplicate_job_for_fact = False
        else:
            duplicate_job_for_fact = _posterior_target_job_exists_for_fact(
                conn,
                slug=slug_key,
                event_date=event_key,
                fact_id=int(latest["id"] or 0),
                target_bucket=target_bucket,
                target_posterior=target_posterior,
                epsilon=max(float(min_delta), 1e-9),
            )
            if duplicate_job_for_fact:
                job_skipped_reason = "posterior_target_job_already_enqueued_for_fact"
        if not enqueue_job:
            job_skipped_reason = job_skipped_reason or "posterior_target_enqueue_disabled"
            common_details["posterior_target_enqueue_disabled"] = True
        job_enqueue_eligible = bool(
            enqueue_job
            and order_update_triggered
            and not duplicate_job_for_fact
            and not previous_place_order_retry_block.get("blocked")
            and (not cleanup_only_job or cleanup_only_job_allowed)
            and (not target_execution_blocked or bool(old_trades))
        )
        if job_enqueue_eligible:
            job_enqueue_attempted = True
            try:
                job_id = _enqueue_posterior_target_job(
                    conn,
                    run_date=run_date,
                    slug=slug_key,
                    event_date=event_key,
                    expected_target=expected,
                    expected_targets=(
                        []
                        if primary_only_job_payload
                        else [*reverse_targets, *remote_no_targets]
                    ),
                    snapshot_payload=snapshot_payload,
                    fact_id=int(latest["id"] or 0),
                    fact_as_of=str(latest["fact_as_of"] or ""),
                    priority=_env_int("PM_POSTERIOR_TARGET_DIRECT_JOB_PRIORITY", 15000),
                    old_exit_cleanup_triggered=bool(old_exit_cleanup_triggered),
                    exit_only=bool(cleanup_only_job),
                    primary_only_fast_path_allowed=bool(primary_only_job_payload or not old_trades),
                )
            except Exception as exc:
                job_enqueue_error = f"{exc.__class__.__name__}: {exc}"
                job_skipped_reason = "posterior_target_enqueue_error"
            else:
                if not job_id:
                    job_enqueue_error = "enqueue returned no job_id"
                    job_skipped_reason = "posterior_target_enqueue_missing_job_id"
        if job_enqueue_error:
            common_details["posterior_target_enqueue_error"] = job_enqueue_error
            common_details["entry_suppressed_by_enqueue_error"] = True
            if job_skipped_reason and job_skipped_reason not in reason_codes:
                reason_codes.append(job_skipped_reason)
        common_details["target_adjustment_reason_codes"] = list(reason_codes)
        common_details["duplicate_job_for_fact"] = bool(duplicate_job_for_fact)
        common_details["job_enqueue_eligible"] = bool(job_enqueue_eligible)
        common_details["job_enqueue_attempted"] = bool(job_enqueue_attempted)
        common_details["job_enqueued"] = bool(job_id)
        common_details["job_id"] = int(job_id) if job_id else None
        common_details["job_skipped_reason"] = job_skipped_reason
        if job_enqueue_error:
            common_details["job_enqueue_error"] = job_enqueue_error
        entry_decision_will_order = (
            _truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_WILL_ORDER"), True)
            and enqueue_job
            and entry_order_update_triggered
            and not target_execution_blocked
            and not duplicate_job_for_fact
            and not job_enqueue_error
        )
        entry_decision_state = (
            "entry_dry_run"
            if entry_decision_will_order
            and _truthy(os.environ.get("PM_AUTO_TRADE_DRY_RUN"), True)
            else "entry"
            if entry_decision_will_order
            else "blocked"
        )
        entry_decision_reason_codes = list(reason_codes)
        entry_decision_reasons = [
            "SSOT posterior target probability changed; trade target follows latest posterior top bucket."
        ]
        if target_execution_blocked:
            entry_decision_reason_codes = [
                *entry_decision_reason_codes,
                target_execution_block_reason or "target_realization_trade_follow_not_allowed",
            ]
            if previous_place_order_retry_block.get("blocked"):
                entry_decision_reasons = [
                    "SSOT posterior target still requires entry, but the latest real place-order attempt is retry-blocked."
                ]
            else:
                entry_decision_reasons = [
                    "SSOT posterior target changed, but target execution is blocked by temperature path trade-follow gating."
                ]
        if duplicate_job_for_fact:
            entry_decision_reason_codes = [
                *entry_decision_reason_codes,
                "posterior_target_job_already_enqueued_for_fact",
            ]
            entry_decision_reasons = [
                "Same SSOT fact/target already has a posterior target direct job; heartbeat does not enqueue another order."
            ]
            common_details["entry_suppressed_by_duplicate_job_for_fact"] = True
        if job_enqueue_error:
            enqueue_reason_code = job_skipped_reason or "posterior_target_enqueue_error"
            if enqueue_reason_code not in entry_decision_reason_codes:
                entry_decision_reason_codes = [
                    *entry_decision_reason_codes,
                    enqueue_reason_code,
                ]
            entry_decision_reasons = [
                "SSOT posterior target still requires entry, but posterior target job enqueue failed; details include the raw enqueue error."
            ]
        if old_exit_cleanup_triggered and not entry_order_update_triggered:
            entry_decision_reason_codes = ["target_switch_non_target_entry_suppressed"]
            entry_decision_reasons = [
                "cleanup-only posterior target job suppresses new entry and exits old bucket side."
            ]
            if not cleanup_only_job_allowed:
                entry_decision_reason_codes.append("posterior_target_cleanup_only_job_deferred")
                entry_decision_reasons = [
                    "cleanup-only posterior target job is deferred; live worker remains reserved for primary target entry."
                ]
            common_details["entry_original_reason_codes"] = list(reason_codes)
            common_details["entry_suppressed_by_old_exit_cleanup"] = True
            common_details["entry_suppressed_reason"] = "posterior_target_old_exit_cleanup"
        if suppress_old_exit_decision_rows:
            common_details["old_exit_decision_rows_suppressed"] = True
            common_details["old_exit_decision_rows_suppressed_reason"] = (
                "posterior_target_direct_primary_entry_precheck"
            )
        decision_ids: List[int] = []
        decision_ids.append(
            _insert_decision(
                conn,
                run_date=run_date,
                slug=slug_key,
                event_date=event_key,
                title=title,
                outcome=target_bucket,
                side="YES",
                action="entry",
                decided_state=entry_decision_state,
                will_order=entry_decision_will_order,
                reason_codes=entry_decision_reason_codes,
                reasons=entry_decision_reasons,
                details=common_details,
                created_at=now_iso,
            )
        )
        for trade in ([] if suppress_old_exit_decision_rows else old_trades):
            exit_details = dict(common_details)
            exit_details["old_trade"] = dict(trade)
            old_side = str(trade.get("side") or "YES").strip().upper()
            reverse_target_for_trade = next(
                (
                    dict(target)
                    for target in reverse_targets
                    if str(target.get("outcome") or "") == str(trade.get("outcome") or "")
                    and str(target.get("held_target_side") or "").upper()
                    == str(trade.get("side") or "").upper()
                ),
                None,
            )
            exit_details["old_bucket_reverse_target"] = reverse_target_for_trade
            exit_details["target_realization_execution_action"] = "SELL"
            exit_details["target_realization_position_effect"] = "close_old_bucket_side"
            exit_details["sell_old_buy_new_transition"] = {
                "exit_leg": {
                    "outcome": str(trade.get("outcome") or ""),
                    "side": old_side if old_side in {"YES", "NO"} else "YES",
                    "execution_action": "SELL",
                    "position_effect": "close_old_bucket_side",
                },
                "new_entry_leg": {
                    "outcome": target_bucket,
                    "side": str(expected.get("side") or "YES").upper(),
                    "execution_action": "BUY",
                    "position_effect": "open_or_add_new_bucket_side",
                },
                "old_bucket_reverse_entry_leg": (
                    {
                        "outcome": str(reverse_target_for_trade.get("outcome") or ""),
                        "side": str(reverse_target_for_trade.get("side") or "").upper(),
                        "execution_action": "BUY",
                        "position_effect": "open_or_add_old_bucket_opposite_side",
                    }
                    if reverse_target_for_trade
                    else None
                ),
            }
            exit_details["exit_original_context_reason_codes"] = list(reason_codes)
            exit_reason_codes = ["target_realization_switch_buy_exit_old"]
            if target_execution_blocked:
                exit_details["entry_execution_blocked"] = True
                exit_details["entry_execution_block_reason"] = (
                    target_execution_block_reason
                    or "target_realization_trade_follow_not_allowed"
                )
                exit_details["exit_decoupled_from_entry_execution_block"] = True
            try:
                exit_details = swm._finalize_exit_decision_details(
                    exit_details,
                    decision_reason_codes=exit_reason_codes,
                )
            except Exception:
                exit_details["exit_policy_version"] = swm.EXIT_POLICY_VERSION
                exit_details["exit_policy_primary_source"] = swm.EXIT_POLICY_PRIMARY_SOURCE
                exit_details["exit_policy_trigger_reason_codes"] = list(exit_reason_codes)
                exit_details["exit_policy_family_codes"] = ["target_realization_exit"]
                exit_details["exit_reason_family_codes"] = ["target_realization_exit"]
            decision_ids.append(
                _insert_decision(
                    conn,
                    run_date=run_date,
                    slug=slug_key,
                    event_date=event_key,
                    title=title,
                    outcome=str(trade.get("outcome") or ""),
                    side=old_side if old_side in {"YES", "NO"} else "YES",
                    action="exit",
                    decided_state=str(trade.get("state") or "entry"),
                    will_order=(
                        _truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_WILL_ORDER"), True)
                        and enqueue_job
                        and order_update_triggered
                        and not duplicate_job_for_fact
                        and not job_enqueue_error
                    ),
                    reason_codes=exit_reason_codes,
                    reasons=[
                        "SSOT posterior target moved or repriced; exit old bucket side."
                    ],
                    details=exit_details,
                    created_at=now_iso,
                )
            )
        conn.commit()
    return {
        "ok": True,
        "status": "recorded",
        "slug": slug_key,
        "event_date": event_key,
        "target_bucket": target_bucket,
        "target_posterior": target_posterior,
        "target_source": target_source,
        "previous_bucket": previous_bucket,
        "previous_posterior": previous_posterior,
        "bucket_changed": bucket_changed,
        "probability_changed": probability_changed,
        "probability_delta": probability_delta,
        "entry_order_update_triggered": bool(entry_order_update_triggered),
        "old_exit_cleanup_triggered": bool(old_exit_cleanup_triggered),
        "old_exit_cleanup_recent": bool(old_exit_cleanup_recent),
        "old_exit_cleanup_deferred_for_primary_entry": bool(
            old_exit_cleanup_deferred_for_primary_entry
        ),
        "decision_ids": decision_ids,
        "old_exit_count": len(old_trades),
        "remote_no_entry_count": len(remote_no_targets),
        "job_id": job_id,
        "job_enqueued": bool(job_id),
        "job_enqueue_eligible": bool(job_enqueue_eligible),
        "job_enqueue_attempted": bool(job_enqueue_attempted),
        "job_enqueue_error": job_enqueue_error,
        "duplicate_job_for_fact": bool(duplicate_job_for_fact),
        "job_skipped_reason": job_skipped_reason,
        "fact_id": int(latest["id"] or 0),
        "fact_as_of": str(latest["fact_as_of"] or ""),
        "decision_started_at": decision_started_at,
        "decision_created_at": now_iso,
        "latest_fact_payload_source": payload_source,
    }


def record_hard_source_direct_decisions(
    db_path: Path | str,
    *,
    event_date: str,
    slug: str = "",
    emit_unchanged_sec: int = 60,
    enqueue_job: bool = True,
) -> Dict[str, Any]:
    db = Path(db_path)
    event_key = _normalize_date(event_date) or _chengdu_today()
    run_date = _run_date_et()
    hard_source_decision_started_at = _utc_now_iso()
    hard_source_decision_started_mono = time.monotonic()
    allow_wu_fallback = _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_ALLOW_WU_TWC_FALLBACK"), False)
    with _connect(db) as conn:
        latest = _load_latest_fact(conn, event_date=event_key, slug=slug)
        if latest is None:
            return {"ok": False, "status": "latest_fact_missing", "event_date": event_key}
        payload = _json_loads(latest["fact_json"])
        if not payload:
            return {"ok": False, "status": "latest_fact_json_invalid", "event_date": event_key}
        slug_key = str(latest["slug"] or payload.get("slug") or slug or "").strip()
        event_key = _normalize_date(latest["event_date"] or payload.get("event_date") or event_key)
        settlement_signal = _latest_settlement_signal_from_event_runs(
            conn,
            slug=slug_key,
            event_date=event_key,
        )
        if settlement_signal:
            payload = _merge_final_settlement_signal_into_payload(payload, settlement_signal)
        window = _decision_window_state(payload, event_date=event_key)
        if not bool(window.get("allowed")):
            return {
                "ok": True,
                "status": "outside_decision_window",
                "event_date": event_key,
                "slug": slug_key,
                "decision_window": window,
            }
        observation = _hard_observation_from_payload(payload, allow_wu_twc_fallback=allow_wu_fallback)
        if not observation.get("ok"):
            return {"ok": False, "status": "hard_observation_missing", "event_date": event_key, **observation}
        value_c = _to_float(observation.get("value_c"))
        if value_c is None:
            return {"ok": False, "status": "hard_observation_value_missing", "event_date": event_key}
        observed_bucket = _bucket_label_for_value(payload, value_c)
        if not observed_bucket:
            return {"ok": False, "status": "observed_bucket_missing", "event_date": event_key, "value_c": value_c}
        constrained_rows = _rows(payload)
        with _temporary_env({"PM_BAYES_HARD_SOURCE_POSTERIOR_OVERRIDE_WINDOW_ENABLED": "0"}):
            constraint_meta = apply_hard_source_posterior_override_to_distribution(
                constrained_rows,
                _features(payload),
                base_probs=_posterior_base_probs(constrained_rows),
                market_var=str(payload.get("market_var") or "temperature_2m_max"),
                rounding_rule=str(payload.get("rounding_rule") or ""),
                event_tz=str(payload.get("event_tz") or payload.get("tz") or "Asia/Shanghai"),
                event_date=event_key,
            )
        if isinstance(constraint_meta, dict):
            constraint_meta["window_source"] = "hard_source_direct_poller_decision_window"
            constraint_meta["direct_decision_window"] = dict(window)
        if not bool(constraint_meta.get("applied")):
            return {
                "ok": False,
                "status": "hard_source_posterior_constraint_not_applied",
                "event_date": event_key,
                "slug": slug_key,
                "observed_bucket": observed_bucket,
                "constraint_status": constraint_meta.get("status"),
                "constraint_meta": constraint_meta,
            }
        top_row = _top_posterior_row(constrained_rows)
        target_bucket = str(top_row.get("outcome") or top_row.get("label") or "").strip()
        if not target_bucket:
            return {"ok": False, "status": "target_bucket_missing", "event_date": event_key, "value_c": value_c}
        constrained_payload = _drop_temperature_path_derived_fields(payload)
        hard_source_ssot_written_at = _iso_not_before(
            _utc_now_iso(),
            latest["fact_as_of"],
        )
        hard_source_fetch_to_ssot_ms = _latency_ms_between(
            observation.get("fetched_at"),
            hard_source_ssot_written_at,
        )
        hard_source_seen_to_ssot_ms = _latency_ms_between(
            observation.get("observed_at"),
            hard_source_ssot_written_at,
        )
        constrained_payload["distribution"] = constrained_rows
        constrained_payload["rows"] = constrained_rows
        constrained_payload["hard_source_posterior_override"] = True
        constrained_payload["hard_source_posterior_override_meta"] = dict(constraint_meta)
        constrained_payload["hard_source_observed_bucket"] = observed_bucket
        constrained_payload["hard_source_observed_value_c"] = value_c
        constrained_payload["ssot_target_bucket"] = target_bucket
        constrained_payload["hard_source_direct_ssot_write"] = True
        constrained_payload["hard_source_direct_ssot_source"] = observation.get("source_id")
        constrained_payload["hard_source_direct_timing"] = {
            "seen_at": observation.get("observed_at"),
            "fetched_at": observation.get("fetched_at"),
            "decision_started_at": hard_source_decision_started_at,
            "ssot_written_at": hard_source_ssot_written_at,
            "seen_to_ssot_ms": hard_source_seen_to_ssot_ms,
            "fetch_to_ssot_ms": hard_source_fetch_to_ssot_ms,
        }
        for constrained_row in constrained_rows:
            if not isinstance(constrained_row, dict):
                continue
            constrained_row["hard_source_direct"] = True
            constrained_row["hard_source_posterior_override"] = True
            constrained_row["hard_source_seen_at"] = observation.get("observed_at")
            constrained_row["hard_source_fetched_at"] = observation.get("fetched_at")
            constrained_row["hard_source_decision_started_at"] = hard_source_decision_started_at
            constrained_row["hard_source_ssot_written_at"] = hard_source_ssot_written_at
            constrained_row["hard_source_seen_to_ssot_ms"] = hard_source_seen_to_ssot_ms
            constrained_row["hard_source_fetch_to_ssot_ms"] = hard_source_fetch_to_ssot_ms
        constrained_payload = _merge_temperature_path_derived_fields(
            constrained_payload,
            slug=slug_key,
            event_date=event_key,
            created_at=hard_source_ssot_written_at,
        )
        try:
            from ui.server import (
                _align_snapshot_posterior_derived_fields,
                _attach_target_realization_capital_plan,
            )

            _align_snapshot_posterior_derived_fields(constrained_payload)
            _attach_target_realization_capital_plan(constrained_payload)
            _align_snapshot_posterior_derived_fields(constrained_payload)
        except Exception as exc:
            constrained_payload["hard_source_direct_ssot_alignment_error"] = (
                f"{exc.__class__.__name__}: {exc}"
            )
        canonical_bucket, canonical_posterior, canonical_source = _canonical_target_from_payload(
            constrained_payload
        )
        if canonical_bucket:
            target_bucket = canonical_bucket
            constrained_payload["ssot_target_bucket"] = target_bucket
            constrained_payload["hard_source_direct_ssot_target_source"] = canonical_source
            top_row = _row_for_outcome(constrained_payload, target_bucket) or top_row
        ssot_write_started = time.monotonic()
        ssot_fact_id = _insert_fact_snapshot(
            conn,
            latest_row=latest,
            item=constrained_payload,
            fact_as_of=hard_source_ssot_written_at,
        )
        ssot_write_ms: Optional[float] = None
        try:
            ssot_write_ms = round(max(0.0, time.monotonic() - ssot_write_started) * 1000.0, 3)
        except Exception:
            ssot_write_ms = None
        hard_source_decision_to_ssot_ms = round(
            max(0.0, time.monotonic() - hard_source_decision_started_mono) * 1000.0,
            3,
        )
        previous = _last_hard_source_entry(conn, slug=slug_key, event_date=event_key)
        previous_bucket = str(previous.get("outcome") or "")
        bucket_changed = bool(previous_bucket and previous_bucket != target_bucket)
        if (
            not bucket_changed
            and _recent_same_entry_exists(
                conn,
                slug=slug_key,
                event_date=event_key,
                outcome=target_bucket,
                within_sec=emit_unchanged_sec,
            )
        ):
            return {
                "ok": True,
                "status": "unchanged",
                "event_date": event_key,
                "slug": slug_key,
                "target_bucket": target_bucket,
                "previous_bucket": previous_bucket,
                "source_id": observation.get("source_id"),
            }
        row = top_row or _row_for_outcome(payload, target_bucket)
        token = _market_token(conn, slug=slug_key, event_date=event_key, outcome=target_bucket)
        tick = _latest_market_tick(
            conn,
            slug=slug_key,
            event_date=event_key,
            outcome=target_bucket,
            side="YES",
        )
        expected = _build_expected_target(
            slug=slug_key,
            event_date=event_key,
            outcome=target_bucket,
            observation=observation,
            row=row,
            tick=tick,
            token=token,
            payload=constrained_payload,
        )
        if canonical_posterior is not None:
            expected["q_entry"] = float(canonical_posterior)
            expected["q_min_bound"] = float(canonical_posterior)
            expected["q_bayes_posterior"] = float(canonical_posterior)
            expected["q_temperature_bucket_posterior"] = float(canonical_posterior)
            expected["q_quote"] = float(canonical_posterior)
            expected["new_target_posterior"] = float(canonical_posterior)
            expected["target_bucket_posterior"] = float(canonical_posterior)
            expected["target_posterior"] = float(canonical_posterior)
            expected["target_posterior_source"] = canonical_source
            expected["new_target_posterior_source"] = canonical_source
        expected["hard_source_observed_bucket"] = observed_bucket
        expected["hard_source_constraint_mode"] = constraint_meta.get("constraint_mode")
        expected["hard_source_final_bucket_lock"] = bool(constraint_meta.get("final_bucket_lock"))
        expected["decision_basis"] = constraint_meta.get("decision_basis") or expected.get("decision_basis")
        expected["ssot_target_bucket"] = target_bucket
        expected["hard_source_seen_at"] = observation.get("observed_at")
        expected["hard_source_decision_started_at"] = hard_source_decision_started_at
        expected["hard_source_ssot_written_at"] = hard_source_ssot_written_at
        expected["hard_source_ssot_write_ms"] = ssot_write_ms
        expected["hard_source_seen_to_ssot_ms"] = hard_source_seen_to_ssot_ms
        expected["hard_source_fetch_to_ssot_ms"] = hard_source_fetch_to_ssot_ms
        expected["hard_source_decision_to_ssot_ms"] = hard_source_decision_to_ssot_ms
        reason_codes = ["hard_source_posterior_constraint"]
        if observation.get("source_id") == "zuuu_metar_raw_awc":
            reason_codes.append("hard_source_awc")
        else:
            reason_codes.append("hard_source_wu_twc")
        if bucket_changed:
            reason_codes.append("ssot_target_bucket_changed")
        expected = _apply_expected_entry_audit(expected, reason_codes=reason_codes)
        allowed_remote_no_outcomes = _posterior_target_allowed_remote_no_outcomes(
            constrained_payload,
            target_bucket=target_bucket,
        )
        old_trades = _active_old_yes_trades(
            conn,
            slug=slug_key,
            event_date=event_key,
            target_outcome=target_bucket,
            allowed_remote_no_outcomes=allowed_remote_no_outcomes,
        )
        reverse_targets: List[Dict[str, Any]] = []
        for trade in old_trades:
            old_outcome = str(trade.get("outcome") or "").strip()
            old_side = str(trade.get("side") or "").strip().upper()
            reverse_side = _opposite_trade_side(old_side)
            if not old_outcome or not reverse_side:
                continue
            old_row = _row_for_outcome(payload, old_outcome)
            old_token = _market_token(
                conn,
                slug=slug_key,
                event_date=event_key,
                outcome=old_outcome,
            )
            old_tick = _latest_market_tick(
                conn,
                slug=slug_key,
                event_date=event_key,
                outcome=old_outcome,
                side=reverse_side,
            )
            reverse_target = _build_expected_target(
                slug=slug_key,
                event_date=event_key,
                outcome=old_outcome,
                observation=observation,
                row=old_row,
                tick=old_tick,
                token=old_token,
                payload=constrained_payload,
                side=reverse_side,
                leg_role="old_bucket_reverse",
                entry_reasons=[
                    "hard_source_direct",
                    "target_realization_order_intent",
                    "target_realization_switch_old_bucket_reverse",
                ],
                held_target_bucket=old_outcome,
                held_target_side=old_side,
            )
            reverse_target["target_bucket"] = target_bucket
            reverse_target["new_target_bucket"] = target_bucket
            reverse_target["ssot_target_bucket"] = target_bucket
            reverse_target["hard_source_observed_bucket"] = observed_bucket
            reverse_target["hard_source_constraint_mode"] = constraint_meta.get("constraint_mode")
            reverse_target["hard_source_final_bucket_lock"] = bool(constraint_meta.get("final_bucket_lock"))
            reverse_target["decision_basis"] = constraint_meta.get("decision_basis") or expected.get("decision_basis")
            reverse_target["hard_source_seen_at"] = observation.get("observed_at")
            reverse_target["hard_source_decision_started_at"] = hard_source_decision_started_at
            reverse_target["hard_source_ssot_written_at"] = hard_source_ssot_written_at
            reverse_target["hard_source_ssot_write_ms"] = ssot_write_ms
            reverse_target["hard_source_seen_to_ssot_ms"] = hard_source_seen_to_ssot_ms
            reverse_target["hard_source_fetch_to_ssot_ms"] = hard_source_fetch_to_ssot_ms
            reverse_target["hard_source_decision_to_ssot_ms"] = hard_source_decision_to_ssot_ms
            reverse_target = _apply_expected_entry_audit(
                reverse_target,
                reason_codes=[
                    "hard_source_posterior_constraint",
                    "target_realization_switch_old_bucket_reverse",
                ],
            )
            reverse_targets.append(reverse_target)
        now_iso = _utc_now_iso()
        title = str(payload.get("title") or payload.get("question") or slug_key)
        common_details = {
            "source": DIRECT_SOURCE,
            "hard_source_direct": True,
            "hard_source_posterior_override": True,
            "posterior_override": True,
            "posterior_override_q": expected.get("q_bayes_posterior"),
            "posterior_override_source": observation.get("source_id"),
            "bayes_recomputed_async": True,
            "fact_recompute_async": True,
            "decision_basis": constraint_meta.get("decision_basis") or "hard_source_lower_bound_constraint",
            "decision_ssot": "auto_trade_target_bucket",
            "target_adjustment_decision_version": "hard_source_posterior_override_v1",
            "target_adjustment_action": "HARD_SOURCE_BUCKET_OVERRIDE",
            "target_adjustment_reason_codes": [
                "hard_source_posterior_constraint",
                "hard_source_posterior_override",
            ],
            "hard_source_observed_bucket": observed_bucket,
            "hard_source_constraint_mode": constraint_meta.get("constraint_mode"),
            "hard_source_final_bucket_lock": bool(constraint_meta.get("final_bucket_lock")),
            "hard_source_final_lock_reason": constraint_meta.get("final_lock_reason"),
            "ssot_target_bucket": target_bucket,
            "q_bayes_posterior": expected.get("q_bayes_posterior"),
            "q_temperature_bucket_posterior": expected.get("q_temperature_bucket_posterior"),
            "new_target_posterior": expected.get("new_target_posterior"),
            "source_id": observation.get("source_id"),
            "observed_value_c": value_c,
            "observed_at": observation.get("observed_at"),
            "fetched_at": observation.get("fetched_at"),
            "fetch_duration_ms": observation.get("fetch_duration_ms"),
            "hard_source_seen_at": observation.get("observed_at"),
            "hard_source_decision_started_at": hard_source_decision_started_at,
            "hard_source_ssot_written_at": hard_source_ssot_written_at,
            "hard_source_ssot_write_ms": ssot_write_ms,
            "hard_source_seen_to_ssot_ms": hard_source_seen_to_ssot_ms,
            "hard_source_fetch_to_ssot_ms": hard_source_fetch_to_ssot_ms,
            "hard_source_decision_to_ssot_ms": hard_source_decision_to_ssot_ms,
            "target_bucket": target_bucket,
            "previous_bucket": previous_bucket,
            "bucket_changed": bucket_changed,
            "fact_id": int(latest["id"] or 0),
            "ssot_fact_id": ssot_fact_id,
            "ssot_write_ms": ssot_write_ms,
            "fact_as_of": str(latest["fact_as_of"] or ""),
            "expected_target": dict(expected),
            "entry_expected_targets": [dict(expected), *[dict(target) for target in reverse_targets]],
            "allowed_remote_no_outcomes": list(allowed_remote_no_outcomes),
            "sell_old_buy_new_semantics": "sell_old_position_side_then_buy_new_and_optional_old_opposite_side",
            "sell_old_buy_new_entry_legs": [dict(expected), *[dict(target) for target in reverse_targets]],
        }
        common_details.update(_decision_ssot_context_from_payload(constrained_payload))
        common_details.update(
            {
                key: value
                for key, value in expected.items()
                if key.startswith("entry_usable_edge")
                or key.startswith("entry_edge_")
                or key in {
                    "entry_alpha_gate_source",
                    "hard_source_direct_entry_usable_edge_override",
                    "hard_source_direct_entry_override_version",
                    "hard_source_direct_signal_edge",
                    "posterior_settlement_entry_allowed",
                    "posterior_settlement_entry_block_reason",
                    "posterior_settlement_size_scale",
                    "target_realization_reliability",
                    "target_realization_reliability_source",
                    "kelly_realization_strength",
                    "kelly_realization_strength_included",
                }
            }
        )
        decision_ids: List[int] = []
        decision_ids.append(
            _insert_decision(
                conn,
                run_date=run_date,
                slug=slug_key,
                event_date=event_key,
                title=title,
                outcome=target_bucket,
                side="YES",
                action="entry",
                decided_state="entry_dry_run"
                if _truthy(os.environ.get("PM_AUTO_TRADE_DRY_RUN"), True)
                else "entry",
                will_order=_truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_WILL_ORDER"), True),
                reason_codes=reason_codes,
                reasons=["Hard source constrains posterior; trade target follows SSOT top bucket."],
                details=common_details,
                created_at=now_iso,
            )
        )
        for trade in old_trades:
            exit_details = dict(common_details)
            exit_details["old_trade"] = dict(trade)
            exit_details["old_bucket_reverse_target"] = next(
                (
                    dict(target)
                    for target in reverse_targets
                    if str(target.get("outcome") or "") == str(trade.get("outcome") or "")
                    and str(target.get("held_target_side") or "").upper()
                    == str(trade.get("side") or "").upper()
                ),
                None,
            )
            old_side = str(trade.get("side") or "YES").strip().upper()
            reverse_target_for_trade = (
                exit_details["old_bucket_reverse_target"]
                if isinstance(exit_details.get("old_bucket_reverse_target"), dict)
                else None
            )
            exit_details["target_realization_execution_action"] = "SELL"
            exit_details["target_realization_position_effect"] = "close_old_bucket_side"
            exit_details["sell_old_buy_new_transition"] = {
                "exit_leg": {
                    "outcome": str(trade.get("outcome") or ""),
                    "side": old_side if old_side in {"YES", "NO"} else "YES",
                    "execution_action": "SELL",
                    "position_effect": "close_old_bucket_side",
                },
                "new_entry_leg": {
                    "outcome": target_bucket,
                    "side": str(expected.get("side") or "YES").upper(),
                    "execution_action": "BUY",
                    "position_effect": "open_or_add_new_bucket_side",
                },
                "old_bucket_reverse_entry_leg": (
                    {
                        "outcome": str(reverse_target_for_trade.get("outcome") or ""),
                        "side": str(reverse_target_for_trade.get("side") or "").upper(),
                        "execution_action": "BUY",
                        "position_effect": "open_or_add_old_bucket_opposite_side",
                    }
                    if reverse_target_for_trade
                    else None
                ),
            }
            exit_details["exit_original_context_reason_codes"] = [
                "hard_source_posterior_constraint",
                "ssot_target_bucket_changed_old_bucket_invalidated",
            ]
            exit_reason_codes = ["target_realization_switch_buy_exit_old"]
            try:
                exit_details = swm._finalize_exit_decision_details(
                    exit_details,
                    decision_reason_codes=exit_reason_codes,
                )
            except Exception:
                exit_details["exit_policy_version"] = swm.EXIT_POLICY_VERSION
                exit_details["exit_policy_primary_source"] = swm.EXIT_POLICY_PRIMARY_SOURCE
                exit_details["exit_policy_trigger_reason_codes"] = list(exit_reason_codes)
                exit_details["exit_policy_family_codes"] = ["target_realization_exit"]
                exit_details["exit_reason_family_codes"] = ["target_realization_exit"]
            decision_ids.append(
                _insert_decision(
                    conn,
                    run_date=run_date,
                    slug=slug_key,
                    event_date=event_key,
                    title=title,
                    outcome=str(trade.get("outcome") or ""),
                    side=old_side if old_side in {"YES", "NO"} else "YES",
                    action="exit",
                    decided_state=str(trade.get("state") or "entry"),
                    will_order=_truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_WILL_ORDER"), True),
                    reason_codes=exit_reason_codes,
                    reasons=[
                        "SSOT posterior target moved after hard source constraint; exit old bucket side."
                    ],
                    details=exit_details,
                    created_at=now_iso,
                )
            )
        job_id = None
        if enqueue_job:
            job_id = _enqueue_direct_job(
                conn,
                run_date=run_date,
                slug=slug_key,
                event_date=event_key,
                expected_target=expected,
                expected_targets=reverse_targets,
                constrained_payload=constrained_payload,
                observation=observation,
                priority=_env_int("PM_HARD_SOURCE_DIRECT_JOB_PRIORITY", 20000),
                primary_only_fast_path_allowed=not bool(old_trades),
            )
        conn.commit()
    return {
        "ok": True,
        "status": "recorded",
        "slug": slug_key,
        "event_date": event_key,
        "target_bucket": target_bucket,
        "observed_bucket": observed_bucket,
        "constraint_mode": constraint_meta.get("constraint_mode"),
        "final_bucket_lock": bool(constraint_meta.get("final_bucket_lock")),
        "previous_bucket": previous_bucket,
        "bucket_changed": bucket_changed,
        "source_id": observation.get("source_id"),
        "value_c": value_c,
        "decision_ids": decision_ids,
        "old_exit_count": len(old_trades),
        "job_id": job_id,
        "ssot_fact_id": ssot_fact_id,
        "ssot_write_ms": ssot_write_ms,
    }


def _claimable_direct_target_job_id(
    db_path: Path | str,
    *,
    slug: str = "",
    event_date: str = "",
) -> int:
    slug_key = str(slug or "").strip()
    event_key = _normalize_date(event_date)
    slug_like = f'%"{slug_key}"%' if slug_key else "%"
    event_like = f'%"{event_key}"%' if event_key else "%"
    now_iso = _utc_now_iso()
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT id
                FROM auto_trade_jobs
                WHERE (
                        (
                            state IN ('queued', 'retrying')
                            AND (
                                COALESCE(next_retry_at, '') = ''
                             OR julianday(next_retry_at) <= julianday(?)
                            )
                        )
                     OR (
                            state = 'running'
                            AND COALESCE(lease_until, '') <> ''
                            AND julianday(lease_until) <= julianday(?)
                        )
                  )
                  AND (
                        LOWER(COALESCE(source, '')) IN ('hard_source_direct', 'posterior_target_direct')
                     OR COALESCE(payload_json, '') LIKE '%"hard_source_direct"%'
                     OR COALESCE(payload_json, '') LIKE '%"posterior_target_direct"%'
                     OR COALESCE(payload_json, '') LIKE '%"hard_source_direct_snapshot"%'
                     OR COALESCE(payload_json, '') LIKE '%"posterior_target_direct_snapshot"%'
                  )
                  AND (? = '' OR COALESCE(payload_json, '') LIKE ?)
                  AND (
                        ? = ''
                     OR COALESCE(payload_json, '') LIKE ?
                     OR run_date = ?
                  )
                ORDER BY priority DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (
                    now_iso,
                    now_iso,
                    slug_key,
                    slug_like,
                    event_key,
                    event_like,
                    event_key,
                ),
            ).fetchone()
    except sqlite3.Error:
        return 0
    if not row:
        return 0
    try:
        return int(row["id"] or 0)
    except Exception:
        return 0


def poll_once(
    db_path: Path | str = DEFAULT_DB,
    *,
    event_date: str = "",
    slug: str = "",
    background_wu_twc: bool = False,
) -> Dict[str, Any]:
    if not _truthy(os.environ.get("_PM_HARD_SOURCE_LIVE_SSOT_LOCK_HELD"), False):
        with _temporary_env({"_PM_HARD_SOURCE_LIVE_SSOT_LOCK_HELD": "1"}):
            with _live_ssot_writer_lock(event_date=event_date, stage="poll_once"):
                return poll_once(
                    db_path,
                    event_date=event_date,
                    slug=slug,
                    background_wu_twc=background_wu_twc,
                )
    db = Path(db_path)
    event_key = _normalize_date(event_date) or _chengdu_today()
    slug_key = str(slug or "").strip()
    awc_result: Dict[str, Any] = {}
    wu_result: Dict[str, Any] = {}
    metar_pws_result: Dict[str, Any] = {}
    market_prefetch_result: Dict[str, Any] = {}
    awc_ctp_recompute_result: Dict[str, Any] = {}
    ctp_recompute_result: Dict[str, Any] = {}
    posterior_fast_precheck: Dict[str, Any] = {}
    posterior_after_awc_ctp: Dict[str, Any] = {}
    posterior_after_ctp: Dict[str, Any] = {}
    posterior_decision: Dict[str, Any] = {}
    worker_result: Dict[str, Any] = {}
    posterior_fast_worker_result: Dict[str, Any] = {}
    posterior_after_ctp_worker_result: Dict[str, Any] = {}
    worker_requested_job_ids: set[int] = set()
    timings_ms: Dict[str, float] = {}

    def _finish_timing(key: str, started: float) -> None:
        timings_ms[key] = round(max(0.0, time.monotonic() - started) * 1000.0, 3)

    def _posterior_target_enabled() -> bool:
        return _truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_ENABLED"), True)

    def _record_posterior_target(stage: str, *, slug_value: str = "") -> Dict[str, Any]:
        if not _posterior_target_enabled():
            return {"ok": True, "status": "disabled", "stage": stage}
        started = time.monotonic()
        result = record_posterior_target_direct_decisions(
            db,
            event_date=event_key,
            slug=slug_value or slug,
            emit_unchanged_sec=_env_int("PM_POSTERIOR_TARGET_DIRECT_DECISION_HEARTBEAT_SEC", 10),
            enqueue_job=_truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_ENQUEUE_JOB"), True),
        )
        _finish_timing(f"posterior_{stage}_ms", started)
        if isinstance(result, dict):
            result.setdefault("stage", stage)
        return result

    def _run_worker_for_job(job_id: Any, *, env_key: str) -> Dict[str, Any]:
        try:
            normalized_job_id = int(job_id or 0)
        except Exception:
            normalized_job_id = 0
        if normalized_job_id <= 0:
            return {}
        if normalized_job_id in worker_requested_job_ids:
            return {"ok": True, "status": "worker_already_requested", "job_id": normalized_job_id}
        if not _truthy(os.environ.get(env_key), True):
            return {"ok": True, "status": "worker_disabled", "job_id": normalized_job_id}
        worker_requested_job_ids.add(normalized_job_id)
        started = time.monotonic()
        result = _run_worker_once_inline(db_path=db, requested_job_id=normalized_job_id)
        _finish_timing(f"worker_job_{normalized_job_id}_ms", started)
        if isinstance(result, dict):
            result.setdefault("requested_job_id", normalized_job_id)
        return result

    def _drain_claimable_direct_job(stage: str) -> Dict[str, Any]:
        nonlocal worker_result
        if worker_result or not _truthy(
            os.environ.get("PM_HARD_SOURCE_DIRECT_DRAIN_CLAIMABLE_JOB"),
            True,
        ):
            return {}
        drain_job_id = _claimable_direct_target_job_id(
            db,
            slug=slug_key or slug,
            event_date=event_key,
        )
        if not drain_job_id:
            return {}
        worker_result = _run_worker_for_job(
            drain_job_id,
            env_key="PM_HARD_SOURCE_DIRECT_DRAIN_WORKER_ONCE",
        )
        if isinstance(worker_result, dict):
            worker_result.setdefault("drain_claimable_direct_job", True)
            worker_result.setdefault("drain_claimable_direct_job_id", drain_job_id)
            worker_result.setdefault("drain_claimable_direct_stage", stage)
        return worker_result

    if _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_DRAIN_BEFORE_AWC"), True):
        _drain_claimable_direct_job("before_awc")
        if worker_result and _truthy(
            os.environ.get("PM_HARD_SOURCE_DIRECT_RETURN_AFTER_PRE_AWC_DRAIN"),
            True,
        ):
            decision = {
                "ok": True,
                "status": "skipped",
                "reason": "claimable_direct_job_drained_before_awc",
                "event_date": event_key,
                "slug": slug_key or slug,
            }
            awc_result = {
                "ok": True,
                "status": "skipped",
                "reason": "claimable_direct_job_drained_before_awc",
                "event_date": event_key,
            }
            return {
                "ok": True,
                "event_date": event_key,
                "timings_ms": timings_ms,
                "awc": awc_result,
                "metar_pws": metar_pws_result,
                "wu_twc": wu_result,
                "market_prefetch": market_prefetch_result,
                "awc_ctp_recompute": awc_ctp_recompute_result,
                "ctp_recompute": ctp_recompute_result,
                "decision": decision,
                "posterior_target_fast_precheck": posterior_fast_precheck,
                "posterior_target_after_awc_ctp": posterior_after_awc_ctp,
                "posterior_target_after_ctp": posterior_after_ctp,
                "posterior_target": posterior_decision,
                "posterior_target_fast_worker": posterior_fast_worker_result,
                "posterior_target_after_ctp_worker": posterior_after_ctp_worker_result,
                "worker": worker_result,
            }

    if _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_REFRESH_AWC"), True):
        started = time.monotonic()
        awc_result = _refresh_awc_fact(
            db,
            event_date=event_key,
            hours=_awc_refresh_hours(event_key),
            heartbeat_sec=_env_int("PM_HARD_SOURCE_FACT_HEARTBEAT_SEC", 15),
        )
        _finish_timing("awc_refresh_ms", started)
    awc_updated = _refresh_result_updated(awc_result)
    started = time.monotonic()
    window_payload = _load_latest_fact_payload(db, event_date=event_key, slug=slug)
    _finish_timing("load_window_payload_ms", started)
    window_state = _decision_window_state(window_payload, event_date=event_key)
    if not slug_key and isinstance(window_payload, Mapping):
        slug_key = str(window_payload.get("slug") or "").strip()
    if _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_DRAIN_BEFORE_DECISION"), False):
        _drain_claimable_direct_job("before_decision")

    def _record_posterior_after_ctp(stage: str) -> Dict[str, Any]:
        nonlocal posterior_after_ctp
        nonlocal posterior_decision
        nonlocal posterior_after_ctp_worker_result
        nonlocal worker_result
        result = _record_posterior_target(
            stage,
            slug_value=slug_key or slug,
        )
        replaces_primary = not (
            posterior_after_ctp.get("job_id") and not result.get("job_id")
        )
        if replaces_primary:
            posterior_after_ctp = result
            posterior_decision = result
        elif isinstance(result, dict):
            posterior_after_ctp.setdefault("later_ctp_stage", result.get("stage"))
            posterior_after_ctp.setdefault("later_ctp_status", result.get("status"))
        if result.get("job_id"):
            posterior_after_ctp_worker_result = _run_worker_for_job(
                result.get("job_id"),
                env_key="PM_POSTERIOR_TARGET_DIRECT_RUN_WORKER_ONCE",
            )
            worker_result = posterior_after_ctp_worker_result
        return result

    if awc_updated:
        started = time.monotonic()
        awc_ctp_recompute_result = _recompute_ctp_latest_fact(
            db,
            event_date=event_key,
            slug=slug_key or slug,
            reason="awc",
        )
        _finish_timing("awc_ctp_recompute_ms", started)
        ctp_recompute_result = awc_ctp_recompute_result
        if awc_ctp_recompute_result.get("status") == "recomputed" or awc_ctp_recompute_result.get("fact_id"):
            posterior_after_awc_ctp = _record_posterior_after_ctp("after_awc_ctp_recompute")
    if (
        not awc_updated
        and _truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_FAST_PRECHECK_ENABLED"), True)
    ):
        started = time.monotonic()
        posterior_fast_precheck = _record_posterior_target("fast_precheck")
        _finish_timing("fast_precheck_total_ms", started)
        posterior_decision = posterior_fast_precheck
        if posterior_fast_precheck.get("job_id"):
            posterior_fast_worker_result = _run_worker_for_job(
                posterior_fast_precheck.get("job_id"),
                env_key="PM_POSTERIOR_TARGET_DIRECT_RUN_WORKER_ONCE",
            )
            worker_result = posterior_fast_worker_result

    market_prefetch_due = (
        window_state.get("allowed")
        and not window_state.get("disabled")
        and slug_key
        and _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_ENABLED"), True)
        and _refresh_stamp_due(
            "market_prefetch",
            event_key,
            interval_sec=_env_int("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_HEARTBEAT_SEC", 5),
        )
    )
    market_prefetch_inline = _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_INLINE"), False)
    if market_prefetch_due:
        started = time.monotonic()
        if market_prefetch_inline:
            market_prefetch_result = _market_prefetch_once_with_lock(db, event_date=event_key, slug=slug_key)
        else:
            market_prefetch_result = _spawn_market_prefetch(db, event_date=event_key, slug=slug_key)
        _finish_timing("market_prefetch_ms", started)
    if _truthy(os.environ.get("PM_HARD_SOURCE_METAR_PWS_ENABLED"), False):
        if _metar_pws_blocking_enabled():
            started = time.monotonic()
            metar_pws_result = _refresh_metar_pws_multi_source_fact(
                db,
                event_date=event_key,
                slug=slug_key or slug,
                window_allowed=bool(window_state.get("allowed")),
            )
            _finish_timing("metar_pws_ms", started)
        elif _background_metar_pws_refresh_enabled():
            started = time.monotonic()
            metar_pws_result = _spawn_metar_pws_refresh(db, event_date=event_key, slug=slug_key or slug)
            _finish_timing("metar_pws_background_spawn_ms", started)
        else:
            metar_pws_result = {
                "ok": True,
                "status": "skipped",
                "reason": "metar_pws_non_blocking_fast_path",
                "event_date": event_key,
            }
    wu_fallback_enabled = _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_ALLOW_WU_TWC_FALLBACK"), False)
    wu_refresh_enabled = _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_REFRESH_WU_TWC"), True)
    wu_due = False
    if wu_refresh_enabled:
        wu_interval_default = (
            _env_int("PM_HARD_SOURCE_WU_TWC_PEAK_POLL_SEC", 1)
            if window_state.get("allowed")
            else _env_int("PM_HARD_SOURCE_WU_TWC_NORMAL_POLL_SEC", 30)
        )
        wu_due = _refresh_stamp_due(
            "wu_twc",
            event_key,
            interval_sec=_env_int("PM_HARD_SOURCE_WU_TWC_POLL_SEC", wu_interval_default),
        )
    hard_observation_ready = bool(
        _hard_observation_from_payload(
            window_payload,
            allow_wu_twc_fallback=wu_fallback_enabled,
        ).get("ok")
    )
    wu_before_decision = bool(
        wu_fallback_enabled
        and wu_refresh_enabled
        and wu_due
        and not hard_observation_ready
    )
    if wu_before_decision:
        started = time.monotonic()
        wu_result = _refresh_wu_twc_fact(
            db,
            event_date=event_key,
            slug=slug_key or slug,
            heartbeat_sec=_env_int("PM_HARD_SOURCE_FACT_HEARTBEAT_SEC", 15),
        )
        _finish_timing("wu_twc_before_decision_ms", started)
    ctp_recompute_reasons = [
        name
        for name, result in (
            ("metar_pws", metar_pws_result),
            ("wu_twc", wu_result if wu_before_decision else {}),
        )
        if _refresh_result_updated(result)
    ]
    if ctp_recompute_reasons:
        started = time.monotonic()
        ctp_recompute_result = _recompute_ctp_latest_fact(
            db,
            event_date=event_key,
            slug=slug_key or slug,
            reason=",".join(ctp_recompute_reasons),
        )
        _finish_timing("late_ctp_recompute_ms", started)
        if ctp_recompute_result.get("status") == "recomputed" or ctp_recompute_result.get("fact_id"):
            _record_posterior_after_ctp("after_ctp_recompute")
    posterior_job_recorded = bool(
        posterior_fast_precheck.get("job_id")
        or posterior_after_ctp.get("job_id")
    )
    hard_source_enqueue_job = _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_ENQUEUE_JOB"), True)
    suppress_hard_job_after_posterior = bool(
        posterior_job_recorded
        and _truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_SUPPRESS_HARD_SOURCE_JOB"), True)
    )
    hard_source_decision_started = time.monotonic()
    decision = record_hard_source_direct_decisions(
        db,
        event_date=event_key,
        slug=slug_key or slug,
        emit_unchanged_sec=_env_int("PM_HARD_SOURCE_DIRECT_DECISION_HEARTBEAT_SEC", 60),
        enqueue_job=(hard_source_enqueue_job and not suppress_hard_job_after_posterior),
    )
    _finish_timing("hard_source_decision_ms", hard_source_decision_started)
    if isinstance(decision, dict) and suppress_hard_job_after_posterior:
        decision["job_suppressed_by_posterior_target_direct"] = True
        decision["job_suppression_reason"] = "posterior_target_direct_job_already_queued"
    hard_source_recorded_job = bool(decision.get("ok") and decision.get("job_id"))
    if (
        _posterior_target_enabled()
        and not hard_source_recorded_job
        and not posterior_fast_precheck
        and not posterior_after_ctp
    ):
        posterior_decision = _record_posterior_target(
            "after_hard_source_decision",
            slug_value=slug_key or slug,
        )
    worker_job_id = decision.get("job_id") or posterior_decision.get("job_id")
    if worker_job_id:
        worker_result = _run_worker_for_job(
            worker_job_id,
            env_key=(
                "PM_POSTERIOR_TARGET_DIRECT_RUN_WORKER_ONCE"
                if posterior_decision.get("job_id") and not decision.get("job_id")
                else "PM_HARD_SOURCE_DIRECT_RUN_WORKER_ONCE"
            ),
        )
    _drain_claimable_direct_job("after_decision")
    if not wu_before_decision and wu_refresh_enabled and wu_due:
        started = time.monotonic()
        if background_wu_twc:
            wu_result = _spawn_wu_twc_refresh(db, event_date=event_key, slug=slug_key or slug)
        elif _inline_wu_twc_refresh_after_decision_enabled():
            wu_result = _refresh_wu_twc_fact(
                db,
                event_date=event_key,
                slug=slug_key or slug,
                heartbeat_sec=_env_int("PM_HARD_SOURCE_FACT_HEARTBEAT_SEC", 15),
            )
        else:
            wu_result = {
                "ok": True,
                "status": "skipped",
                "reason": "post_decision_wu_twc_refresh_opt_in",
                "event_date": event_key,
            }
        _finish_timing("wu_twc_after_decision_ms", started)
    return {
        "ok": bool(decision.get("ok")),
        "event_date": event_key,
        "timings_ms": timings_ms,
        "awc": awc_result,
        "metar_pws": metar_pws_result,
        "wu_twc": wu_result,
        "market_prefetch": market_prefetch_result,
        "awc_ctp_recompute": awc_ctp_recompute_result,
        "ctp_recompute": ctp_recompute_result,
        "decision": decision,
        "posterior_target_fast_precheck": posterior_fast_precheck,
        "posterior_target_after_awc_ctp": posterior_after_awc_ctp,
        "posterior_target_after_ctp": posterior_after_ctp,
        "posterior_target": posterior_decision,
        "posterior_target_fast_worker": posterior_fast_worker_result,
        "posterior_target_after_ctp_worker": posterior_after_ctp_worker_result,
        "worker": worker_result,
    }


def _refresh_stamp_due(name: str, event_date: str, *, interval_sec: int) -> bool:
    interval = max(0, int(interval_sec))
    if interval <= 0:
        return True
    stamp_dir = ROOT / "data"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(name or "source"))
    event_key = _normalize_date(event_date) or "current"
    path = stamp_dir / f"hard_source_direct.{safe_name}.{event_key}.last"
    now = time.time()
    try:
        previous = float(path.read_text(encoding="utf-8").strip())
    except Exception:
        previous = 0.0
    if previous > 0 and (now - previous) < float(interval):
        return False
    try:
        path.write_text(str(now), encoding="utf-8")
    except Exception:
        pass
    return True


def _background_lock_path(name: str, event_date: str) -> Path:
    safe_name = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(name or "source"))
    event_key = _normalize_date(event_date) or "current"
    return ROOT / "data" / f"hard_source_direct.{safe_name}.{event_key}.pid"


def _background_lock_active(name: str, event_date: str, *, stale_sec: int = 120) -> bool:
    path = _background_lock_path(name, event_date)
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid_text, started_text = (raw.split(",", 1) + [""])[:2]
        pid = int(pid_text)
        started = float(started_text or "0")
    except Exception:
        return False
    if started > 0 and (time.time() - started) > max(5, int(stale_sec)):
        try:
            path.unlink()
        except Exception:
            pass
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        try:
            path.unlink()
        except Exception:
            pass
        return False
    except Exception:
        return True


def _reap_finished_children() -> int:
    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        except AttributeError:
            return reaped
        except OSError:
            return reaped
        if pid <= 0:
            return reaped
        reaped += 1


def _spawn_wu_twc_refresh(db_path: Path, *, event_date: str, slug: str = "") -> Dict[str, Any]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    if _background_lock_active("wu_twc_bg", event_key, stale_sec=_env_int("PM_HARD_SOURCE_WU_TWC_BACKGROUND_STALE_SEC", 120)):
        return {"ok": True, "status": "background_already_running", "event_date": event_key}
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--db",
        str(db_path),
        "--event-date",
        event_key,
        "--wu-twc-refresh-once",
    ]
    if slug:
        cmd.extend(["--slug", slug])
    log_path = ROOT / "data" / "logs" / "hard_source_direct_wu_twc_bg.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("ab")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            handle.close()
        except Exception:
            pass
        _background_lock_path("wu_twc_bg", event_key).write_text(f"{proc.pid},{time.time()}", encoding="utf-8")
        return {"ok": True, "status": "background_started", "event_date": event_key, "pid": proc.pid}
    except Exception as exc:
        return {"ok": False, "status": "background_start_failed", "event_date": event_key, "error": f"{exc.__class__.__name__}: {exc}"}


def _spawn_metar_pws_refresh(db_path: Path, *, event_date: str, slug: str = "") -> Dict[str, Any]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    if not _refresh_stamp_due(
        "metar_pws_bg_spawn",
        event_key,
        interval_sec=_env_int("PM_HARD_SOURCE_METAR_PWS_BACKGROUND_POLL_SEC", 15),
    ):
        return {"ok": True, "status": "metar_pws_background_not_due", "event_date": event_key}
    if _background_lock_active(
        "metar_pws_bg",
        event_key,
        stale_sec=_env_int("PM_HARD_SOURCE_METAR_PWS_BACKGROUND_STALE_SEC", 180),
    ):
        return {"ok": True, "status": "metar_pws_background_already_running", "event_date": event_key}
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--db",
        str(db_path),
        "--event-date",
        event_key,
        "--metar-pws-refresh-once",
    ]
    if slug:
        cmd.extend(["--slug", slug])
    log_path = ROOT / "data" / "logs" / "hard_source_direct_metar_pws_bg.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("ab")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            handle.close()
        except Exception:
            pass
        _background_lock_path("metar_pws_bg", event_key).write_text(f"{proc.pid},{time.time()}", encoding="utf-8")
        return {"ok": True, "status": "metar_pws_background_started", "event_date": event_key, "pid": proc.pid}
    except Exception as exc:
        return {
            "ok": False,
            "status": "metar_pws_background_start_failed",
            "event_date": event_key,
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def _market_prefetch_args() -> argparse.Namespace:
    return argparse.Namespace(
        market_refresh_only=True,
        model_refresh_only=False,
        tz=os.environ.get("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_TZ") or "America/New_York",
        depth_levels=max(1, _env_int("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_DEPTH_LEVELS", 5)),
        market_refresh_max_outcomes=max(0, _env_int("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_MAX_OUTCOMES", 6)),
        market_refresh_time_budget_sec=max(
            0.0,
            _env_float("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_BUDGET_SEC", 1.5),
        ),
        market_refresh_clob_timeout_sec=max(
            0.1,
            _env_float("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CLOB_TIMEOUT_SEC", 0.75),
        ),
        market_refresh_clob_retries=max(
            1,
            _env_int("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CLOB_RETRIES", 1),
        ),
    )


def _market_prefetch_env() -> Dict[str, str]:
    return {
        "PM_SCAN_MARKET_REFRESH_FAST_MODE": "1",
        "PM_MARKET_REFRESH_CACHE_ONLY": os.environ.get("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CACHE_ONLY", "1"),
        "PM_MARKET_REFRESH_CACHE_ONLY_BOOTSTRAP": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_CACHE_BOOTSTRAP",
            "1",
        ),
        "PM_SCAN_MARKET_REFRESH_QUOTE_PREFETCH_WORKERS": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_WORKERS",
            "4",
        ),
        "PM_SCAN_MARKET_REFRESH_PRICE_AUGMENT_ENABLED": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_PRICE_AUGMENT_ENABLED",
            "0",
        ),
    }


def _market_prefetch_once_with_lock(db_path: Path, *, event_date: str, slug: str = "") -> Dict[str, Any]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    lock_path = _background_lock_path("market_prefetch_bg", event_key)
    try:
        lock_path.write_text(f"{os.getpid()},{time.time()}", encoding="utf-8")
    except Exception:
        pass
    prefetch_env = _market_prefetch_env()
    previous_env = {key: os.environ.get(key) for key in prefetch_env}
    previous_db_path = getattr(swm, "DB_PATH", None)
    previous_data_dir = getattr(swm, "DATA_DIR", None)
    try:
        for key, value in prefetch_env.items():
            os.environ[key] = str(value)
        db_file = Path(db_path)
        swm.DB_PATH = db_file
        swm.DATA_DIR = db_file.parent
        ok, err, count, _rows, created_at = swm.refresh_market_ticks_for_event(
            str(slug or ""),
            event_key,
            _market_prefetch_args(),
        )
        return {
            "ok": bool(ok),
            "status": "market_prefetch_refreshed" if ok else "market_prefetch_failed",
            "event_date": event_key,
            "slug": str(slug or ""),
            "count": int(count or 0),
            "created_at": created_at,
            "error": err or "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "market_prefetch_failed",
            "event_date": event_key,
            "slug": str(slug or ""),
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    finally:
        for key, previous in previous_env.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        if previous_db_path is not None:
            swm.DB_PATH = previous_db_path
        if previous_data_dir is not None:
            swm.DATA_DIR = previous_data_dir
        try:
            current = lock_path.read_text(encoding="utf-8").strip()
            if current.startswith(f"{os.getpid()},"):
                lock_path.unlink()
        except Exception:
            pass


def _spawn_market_prefetch(db_path: Path, *, event_date: str, slug: str = "") -> Dict[str, Any]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    if _background_lock_active(
        "market_prefetch_bg",
        event_key,
        stale_sec=_env_int("PM_HARD_SOURCE_DIRECT_MARKET_PREFETCH_STALE_SEC", 30),
    ):
        return {"ok": True, "status": "market_prefetch_background_already_running", "event_date": event_key}
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--db",
        str(db_path),
        "--event-date",
        event_key,
        "--market-prefetch-once",
    ]
    if slug:
        cmd.extend(["--slug", slug])
    log_path = ROOT / "data" / "logs" / "hard_source_direct_market_prefetch_bg.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("ab")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            handle.close()
        except Exception:
            pass
        _background_lock_path("market_prefetch_bg", event_key).write_text(f"{proc.pid},{time.time()}", encoding="utf-8")
        return {"ok": True, "status": "market_prefetch_background_started", "event_date": event_key, "pid": proc.pid}
    except Exception as exc:
        return {
            "ok": False,
            "status": "market_prefetch_background_start_failed",
            "event_date": event_key,
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def _refresh_wu_twc_once_with_lock(db_path: Path, *, event_date: str, slug: str = "") -> Dict[str, Any]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    lock_path = _background_lock_path("wu_twc_bg", event_key)
    try:
        lock_path.write_text(f"{os.getpid()},{time.time()}", encoding="utf-8")
    except Exception:
        pass
    try:
        return _refresh_wu_twc_fact(
            db_path,
            event_date=event_key,
            slug=slug,
            heartbeat_sec=_env_int("PM_HARD_SOURCE_FACT_HEARTBEAT_SEC", 15),
        )
    finally:
        try:
            current = lock_path.read_text(encoding="utf-8").strip()
            if current.startswith(f"{os.getpid()},"):
                lock_path.unlink()
        except Exception:
            pass


def _refresh_metar_pws_once_with_lock(db_path: Path, *, event_date: str, slug: str = "") -> Dict[str, Any]:
    event_key = _normalize_date(event_date) or _chengdu_today()
    lock_path = _background_lock_path("metar_pws_bg", event_key)
    try:
        lock_path.write_text(f"{os.getpid()},{time.time()}", encoding="utf-8")
    except Exception:
        pass
    timings_ms: Dict[str, float] = {}

    def _finish_timing(key: str, started: float) -> None:
        timings_ms[key] = round(max(0.0, time.monotonic() - started) * 1000.0, 3)

    try:
        window_payload = _load_latest_fact_payload(db_path, event_date=event_key, slug=slug)
        window_state = _decision_window_state(window_payload, event_date=event_key)
        started = time.monotonic()
        metar_pws = _refresh_metar_pws_multi_source_fact(
            db_path,
            event_date=event_key,
            slug=slug,
            window_allowed=bool(window_state.get("allowed")),
        )
        _finish_timing("metar_pws_ms", started)
        ctp: Dict[str, Any] = {}
        posterior: Dict[str, Any] = {}
        worker: Dict[str, Any] = {}
        if _refresh_result_updated(metar_pws):
            started = time.monotonic()
            ctp = _recompute_ctp_latest_fact(
                db_path,
                event_date=event_key,
                slug=slug,
                reason="metar_pws_background",
            )
            _finish_timing("ctp_recompute_ms", started)
            if ctp.get("status") == "recomputed" or ctp.get("fact_id"):
                started = time.monotonic()
                posterior = record_posterior_target_direct_decisions(
                    db_path,
                    event_date=event_key,
                    slug=slug,
                    emit_unchanged_sec=_env_int("PM_POSTERIOR_TARGET_DIRECT_DECISION_HEARTBEAT_SEC", 10),
                    enqueue_job=_truthy(os.environ.get("PM_POSTERIOR_TARGET_DIRECT_ENQUEUE_JOB"), True),
                )
                _finish_timing("posterior_target_ms", started)
                if posterior.get("job_id") and _truthy(
                    os.environ.get("PM_POSTERIOR_TARGET_DIRECT_RUN_WORKER_ONCE"),
                    True,
                ):
                    started = time.monotonic()
                    worker = _run_worker_once_inline(
                        db_path=db_path,
                        requested_job_id=int(posterior.get("job_id") or 0),
                    )
                    _finish_timing(f"worker_job_{posterior.get('job_id')}_ms", started)
        return {
            "ok": bool(metar_pws.get("ok")),
            "status": "metar_pws_background_done",
            "event_date": event_key,
            "timings_ms": timings_ms,
            "metar_pws": metar_pws,
            "ctp_recompute": ctp,
            "posterior_target": posterior,
            "worker": worker,
        }
    finally:
        try:
            current = lock_path.read_text(encoding="utf-8").strip()
            if current.startswith(f"{os.getpid()},"):
                lock_path.unlink()
        except Exception:
            pass


def _run_worker_once_inline(
    *,
    db_path: Optional[Path] = None,
    requested_job_id: Optional[int] = None,
) -> Dict[str, Any]:
    overrides = {
        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "1",
        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": os.environ.get("PM_HARD_SOURCE_DIRECT_WORKER_EVENT_TIMEOUT_SEC", "0"),
        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
        "PM_AUTO_TRADE_FACTS_LOCK_WAIT_SEC": os.environ.get("PM_HARD_SOURCE_DIRECT_FACTS_LOCK_WAIT_SEC", "0"),
        "PM_AUTO_TRADE_DB_LOCK_BEST_EFFORT": "1",
        "PM_AUTO_TRADE_EVENT_WORKER_SYNC_TRADES": "0",
        "PM_AUTO_TRADE_EVENT_WORKER_SYNC_OPEN_ORDERS": "0",
        "PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE": "0",
        "PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_WORKER_PREFETCH_MARKET_TICKS",
            "0",
        ),
        "PM_AUTO_TRADE_DIRECT_JOB_CLAIM_RETRY": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_WORKER_CLAIM_RETRY",
            "8",
        ),
        "PM_AUTO_TRADE_DIRECT_JOB_CLAIM_BACKOFF_SEC": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_WORKER_CLAIM_BACKOFF_SEC",
            "0.100",
        ),
        "PM_AUTO_TRADE_DIRECT_JOB_CLAIM_BACKOFF_CAP_SEC": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_WORKER_CLAIM_BACKOFF_CAP_SEC",
            "0.500",
        ),
        "PM_AUTO_TRADE_DIRECT_JOB_DB_CONNECT_TIMEOUT_SEC": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_WORKER_DB_CONNECT_TIMEOUT_SEC",
            "1.000",
        ),
        "PM_AUTO_TRADE_DIRECT_JOB_DB_BUSY_TIMEOUT_MS": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_WORKER_DB_BUSY_TIMEOUT_MS",
            "200",
        ),
        "PM_AUTO_TRADE_DIRECT_RUNTIME_LOCK_WAIT_SEC": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_WORKER_RUNTIME_LOCK_WAIT_SEC",
            "0",
        ),
        "PM_AUTO_TRADE_JOB_BOOKKEEPING_CONNECT_TIMEOUT_SEC": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_WORKER_BOOKKEEPING_CONNECT_TIMEOUT_SEC",
            "0.150",
        ),
        "PM_AUTO_TRADE_JOB_BOOKKEEPING_BUSY_TIMEOUT_MS": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_WORKER_BOOKKEEPING_BUSY_TIMEOUT_MS",
            "200",
        ),
        "PM_AUTO_TRADE_DIRECT_JOB_SKIP_RUN_ROW": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_WORKER_SKIP_RUN_ROW",
            "1",
        ),
        "PM_AUTO_TRADE_DIRECT_SKIP_ENTRY_BALANCE_GUARD": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_SKIP_ENTRY_BALANCE_GUARD",
            "1",
        ),
        "PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_PREFETCH_BUDGET_SEC",
            "0",
        ),
        "PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS": os.environ.get(
            "PM_HARD_SOURCE_DIRECT_PREFETCH_MAX_ATTEMPTS",
            "0",
        ),
        "PM_AUTO_TRADE_JOB_CLAIM_EXCLUDE_DIRECT_TARGET": "0",
    }
    previous = {key: os.environ.get(key) for key in overrides}
    previous_db_path = getattr(swm, "DB_PATH", None)
    previous_data_dir = getattr(swm, "DATA_DIR", None)
    try:
        for key, value in overrides.items():
            os.environ[key] = str(value)
        if db_path is not None:
            db_file = Path(db_path)
            swm.DB_PATH = db_file
            swm.DATA_DIR = db_file.parent
        try:
            normalized_job_id = int(requested_job_id or 0)
        except Exception:
            normalized_job_id = 0
        if normalized_job_id > 0:
            return dict(swm.run_auto_trade_job_worker_once(requested_job_id=normalized_job_id) or {})
        return dict(swm.run_auto_trade_job_worker_once() or {})
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    finally:
        if previous_db_path is not None:
            swm.DB_PATH = previous_db_path
        if previous_data_dir is not None:
            swm.DATA_DIR = previous_data_dir
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _near_peak_interval(
    db_path: Path | str = DEFAULT_DB,
    *,
    event_date: str = "",
    slug: str = "",
) -> float:
    normal = max(0.2, _env_float("PM_HARD_SOURCE_DIRECT_POLL_SEC", 5.0))
    near = max(0.2, _env_float("PM_HARD_SOURCE_DIRECT_NEAR_PEAK_POLL_SEC", 1.0))
    max_poll = max(0.2, _env_float("PM_HARD_SOURCE_DIRECT_MAX_POLL_SEC", 1.0))
    normal = min(normal, float(max_poll))
    near = min(near, float(max_poll))
    awc_release = _awc_release_watch_interval()
    if awc_release is not None:
        normal = min(normal, float(awc_release))
        near = min(near, float(awc_release))
    if not _truthy(os.environ.get("PM_HARD_SOURCE_DIRECT_NEAR_PEAK_ENABLED"), True):
        return normal
    event_key = _normalize_date(event_date) or _chengdu_today()
    payload = _load_latest_fact_payload(db_path, event_date=event_key, slug=slug)
    if _decision_window_state(payload, event_date=event_key).get("allowed"):
        return near
    return normal


def _log(line: str, path: Path = DEFAULT_LOG) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{_utc_now_iso()} {line}\n")
    except Exception:
        pass


def _error_class(error: Any) -> str:
    text = str(error or "").strip()
    if not text:
        return ""
    head = text.split(":", 1)[0].strip()
    return head or text[:80]


def _awc_source_health_snapshot(result: Mapping[str, Any]) -> Dict[str, Any]:
    awc = result.get("awc") if isinstance(result, Mapping) else {}
    if not isinstance(awc, Mapping):
        awc = {}
    posterior = result.get("posterior_target_fast_precheck") if isinstance(result, Mapping) else {}
    if not isinstance(posterior, Mapping) or not posterior:
        posterior = result.get("posterior_target") if isinstance(result, Mapping) else {}
    if not isinstance(posterior, Mapping):
        posterior = {}
    timings = result.get("timings_ms") if isinstance(result, Mapping) else {}
    if not isinstance(timings, Mapping):
        timings = {}
    awc_ok = bool(awc.get("ok"))
    posterior_ok = bool(posterior.get("ok"))
    posterior_status = str(posterior.get("status") or "").strip()
    fast_precheck_covered = bool(
        (not awc_ok)
        and posterior_ok
        and posterior_status in {"unchanged", "recorded", "outside_decision_window", "disabled"}
    )
    return {
        "ok": bool(result.get("ok")) if isinstance(result, Mapping) else False,
        "generated_at": _utc_now_iso(),
        "event_date": str(result.get("event_date") or awc.get("event_date") or ""),
        "awc": {
            "ok": awc_ok,
            "status": str(awc.get("status") or ""),
            "reason": str(awc.get("reason") or ""),
            "error": str(awc.get("error") or ""),
            "error_class": _error_class(awc.get("error")),
            "latest_at": str(awc.get("awc_latest_at") or ""),
            "today_max_c": awc.get("awc_today_max_c"),
            "fetch_hours": awc.get("awc_fetch_hours"),
            "fetch_hours_reason": str(awc.get("awc_fetch_hours_reason") or ""),
            "latest_fact_payload_source": str(awc.get("latest_fact_payload_source") or ""),
            "timings_ms": dict(awc.get("timings_ms") or {}) if isinstance(awc.get("timings_ms"), Mapping) else {},
        },
        "posterior_fast_precheck": {
            "ok": posterior_ok,
            "status": posterior_status,
            "stage": str(posterior.get("stage") or ""),
            "fact_id": posterior.get("fact_id"),
            "target_bucket": str(posterior.get("target_bucket") or ""),
            "target_posterior": posterior.get("target_posterior"),
            "job_id": posterior.get("job_id"),
            "job_skipped_reason": str(posterior.get("job_skipped_reason") or ""),
            "latest_fact_payload_source": str(posterior.get("latest_fact_payload_source") or ""),
        },
        "fallback": {
            "fast_precheck_covered_awc_failure": fast_precheck_covered,
            "source": str(posterior.get("latest_fact_payload_source") or awc.get("latest_fact_payload_source") or ""),
            "probability_path_continued": bool(posterior_ok and posterior_status),
        },
        "timings_ms": dict(timings),
    }


def _write_awc_source_health(result: Mapping[str, Any], path: Path = DEFAULT_AWC_SOURCE_HEALTH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _awc_source_health_snapshot(result)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        pass


def _posterior_latency_record_from_result(result: Mapping[str, Any]) -> Tuple[str, Mapping[str, Any]]:
    ordered_keys = (
        "posterior_target_after_awc_ctp",
        "posterior_target_after_ctp",
        "posterior_target_fast_precheck",
        "posterior_target",
    )
    fallback_key = ""
    fallback_record: Mapping[str, Any] = {}
    for key in ordered_keys:
        raw = result.get(key)
        if not isinstance(raw, Mapping) or not raw:
            continue
        if not fallback_record:
            fallback_key = key
            fallback_record = raw
        skipped = str(raw.get("job_skipped_reason") or "").strip()
        if (
            raw.get("bucket_changed")
            or raw.get("probability_changed")
            or raw.get("job_id")
            or skipped
            and skipped != "posterior_target_unchanged_no_job"
        ):
            return key, raw
    return fallback_key, fallback_record


def _posterior_latency_root_cause_hint(posterior: Mapping[str, Any]) -> str:
    skipped = str(posterior.get("job_skipped_reason") or "").strip()
    if skipped == "posterior_target_enqueue_disabled":
        return "posterior_target_enqueue_job_disabled"
    if skipped == "posterior_target_enqueue_error":
        return "posterior_target_enqueue_error"
    if skipped == "posterior_target_enqueue_missing_job_id":
        return "posterior_target_enqueue_missing_job_id"
    if skipped == "posterior_target_job_already_enqueued_for_fact":
        return "posterior_target_duplicate_job_suppressed"
    if skipped.startswith("posterior_target_previous_place_order_failure_retry"):
        return "posterior_target_previous_place_order_failure_retry_block"
    if skipped and skipped != "posterior_target_unchanged_no_job":
        return skipped
    if posterior.get("job_id"):
        return "posterior_target_job_enqueued"
    if posterior.get("bucket_changed") or posterior.get("probability_changed"):
        return "target_update_without_job_enqueue_detail"
    return "posterior_target_unchanged"


def _posterior_latency_worker_snapshot(result: Mapping[str, Any]) -> Dict[str, Any]:
    for key in (
        "posterior_target_after_ctp_worker",
        "posterior_target_fast_worker",
        "worker",
    ):
        raw = result.get(key)
        if isinstance(raw, Mapping) and raw:
            return {
                "stage_key": key,
                "ok": raw.get("ok"),
                "status": str(raw.get("status") or raw.get("state") or ""),
                "job_id": raw.get("job_id") or raw.get("requested_job_id"),
                "picked": raw.get("picked"),
                "error": str(raw.get("error") or raw.get("last_error") or "")[:300],
            }
    return {}


def _posterior_target_latency_snapshot(result: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(result, Mapping):
        return {}
    stage_key, posterior = _posterior_latency_record_from_result(result)
    if not posterior:
        return {}
    generated_at = _utc_now_iso()
    skipped = str(posterior.get("job_skipped_reason") or "").strip()
    target_update = bool(posterior.get("bucket_changed") or posterior.get("probability_changed"))
    job_id = posterior.get("job_id")
    persist_event = bool(
        target_update
        or job_id
        or skipped
        and skipped != "posterior_target_unchanged_no_job"
    )
    fact_as_of = str(posterior.get("fact_as_of") or "")
    decision_created_at = str(posterior.get("decision_created_at") or "")
    fact_to_decision_ms = _latency_ms_between(fact_as_of, decision_created_at or generated_at)
    awc = result.get("awc") if isinstance(result.get("awc"), Mapping) else {}
    timings = result.get("timings_ms") if isinstance(result.get("timings_ms"), Mapping) else {}
    event_key_parts = [
        str(result.get("event_date") or posterior.get("event_date") or ""),
        str(posterior.get("fact_id") or ""),
        str(posterior.get("target_bucket") or ""),
        str(posterior.get("target_posterior") or ""),
        str(job_id or ""),
        skipped,
        "changed" if target_update else "same",
    ]
    return {
        "ok": bool(result.get("ok")),
        "generated_at": generated_at,
        "event_key": "|".join(event_key_parts),
        "event_date": str(result.get("event_date") or posterior.get("event_date") or ""),
        "stage_key": stage_key,
        "status": str(posterior.get("status") or ""),
        "target_update_detected": target_update,
        "persist_event": persist_event,
        "root_cause_hint": _posterior_latency_root_cause_hint(posterior),
        "fact_id": posterior.get("fact_id"),
        "fact_as_of": fact_as_of,
        "latest_fact_payload_source": str(posterior.get("latest_fact_payload_source") or ""),
        "target_bucket": str(posterior.get("target_bucket") or ""),
        "target_posterior": posterior.get("target_posterior"),
        "previous_bucket": str(posterior.get("previous_bucket") or ""),
        "previous_posterior": posterior.get("previous_posterior"),
        "bucket_changed": bool(posterior.get("bucket_changed")),
        "probability_changed": bool(posterior.get("probability_changed")),
        "probability_delta": posterior.get("probability_delta"),
        "job_id": job_id,
        "job_enqueued": bool(posterior.get("job_enqueued") or job_id),
        "job_enqueue_eligible": posterior.get("job_enqueue_eligible"),
        "job_enqueue_attempted": posterior.get("job_enqueue_attempted"),
        "job_enqueue_error": str(posterior.get("job_enqueue_error") or "")[:500],
        "job_skipped_reason": skipped,
        "duplicate_job_for_fact": posterior.get("duplicate_job_for_fact"),
        "entry_order_update_triggered": posterior.get("entry_order_update_triggered"),
        "old_exit_cleanup_triggered": posterior.get("old_exit_cleanup_triggered"),
        "decision_started_at": str(posterior.get("decision_started_at") or ""),
        "decision_created_at": decision_created_at,
        "latencies_ms": {
            "fact_to_decision_ms": fact_to_decision_ms,
            "awc_refresh_ms": timings.get("awc_refresh_ms"),
            "ctp_recompute_ms": (
                timings.get("awc_ctp_recompute_ms")
                if timings.get("awc_ctp_recompute_ms") is not None
                else timings.get("late_ctp_recompute_ms")
            ),
            "posterior_stage_ms": timings.get(
                f"posterior_{str(posterior.get('stage') or '').strip()}_ms"
            ),
            "posterior_fast_precheck_ms": timings.get("posterior_fast_precheck_ms"),
            "fast_precheck_total_ms": timings.get("fast_precheck_total_ms"),
            "hard_source_decision_ms": timings.get("hard_source_decision_ms"),
        },
        "awc": {
            "ok": awc.get("ok"),
            "status": str(awc.get("status") or ""),
            "reason": str(awc.get("reason") or ""),
            "latest_at": str(awc.get("awc_latest_at") or ""),
            "today_max_c": awc.get("awc_today_max_c"),
            "latest_fact_payload_source": str(awc.get("latest_fact_payload_source") or ""),
        },
        "worker": _posterior_latency_worker_snapshot(result),
    }


def _write_posterior_target_latency_trace(
    result: Mapping[str, Any],
    *,
    latest_path: Path = DEFAULT_POSTERIOR_TARGET_LATENCY_LATEST,
    events_path: Path = DEFAULT_POSTERIOR_TARGET_LATENCY_EVENTS,
) -> None:
    if not _truthy(os.environ.get("PM_POSTERIOR_TARGET_LATENCY_TRACE_ENABLED"), True):
        return
    try:
        payload = _posterior_target_latency_snapshot(result)
        if not payload or not payload.get("persist_event"):
            return
        previous_event_key = ""
        try:
            previous = json.loads(latest_path.read_text(encoding="utf-8"))
            if isinstance(previous, Mapping):
                previous_event_key = str(previous.get("event_key") or "")
        except Exception:
            previous_event_key = ""
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = latest_path.with_suffix(latest_path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(latest_path)
        if str(payload.get("event_key") or "") != previous_event_key:
            with events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _maybe_reconcile_clob_order_service_pending(
    db: Path,
    *,
    event_date: str = "",
    slug: str = "",
) -> Dict[str, Any]:
    if not _truthy(os.environ.get("PM_CLOB_ORDER_SERVICE_PENDING_RECONCILE_ENABLED"), True):
        return {"ok": True, "status": "disabled"}
    started = time.monotonic()
    try:
        result = swm.reconcile_clob_order_service_pending_orders(
            db,
            slug=slug,
            event_date=_normalize_date(event_date) or "",
            limit=_env_int("PM_CLOB_ORDER_SERVICE_PENDING_RECONCILE_LIMIT", 12),
        )
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("stage", "post_poll_order_status_reconcile")
            result.setdefault("elapsed_ms", round(max(0.0, time.monotonic() - started) * 1000.0, 3))
            return result
    except Exception as exc:
        return {
            "ok": False,
            "status": "reconcile_failed",
            "error": f"{exc.__class__.__name__}: {str(exc)[:240]}",
            "elapsed_ms": round(max(0.0, time.monotonic() - started) * 1000.0, 3),
        }
    return {"ok": False, "status": "invalid_reconcile_result"}


def _code_reload_snapshot() -> Dict[str, int]:
    snapshot: Dict[str, int] = {}
    for path in CODE_RELOAD_WATCH_PATHS:
        try:
            resolved = str(Path(path).resolve())
            snapshot[resolved] = int(Path(path).stat().st_mtime_ns)
        except OSError:
            continue
    return snapshot


def _code_reload_changed(snapshot: Mapping[str, int]) -> Tuple[bool, str]:
    for raw_path, old_mtime in snapshot.items():
        try:
            current = int(Path(raw_path).stat().st_mtime_ns)
        except OSError:
            return True, raw_path
        if int(old_mtime or 0) != current:
            return True, raw_path
    return False, ""


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Poll hard weather sources and refresh SSOT posterior constraints.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--event-date", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--wu-twc-refresh-once", action="store_true")
    parser.add_argument("--metar-pws-refresh-once", action="store_true")
    parser.add_argument("--market-prefetch-once", action="store_true")
    args = parser.parse_args(argv)
    db = Path(args.db)
    if args.wu_twc_refresh_once:
        result = _refresh_wu_twc_once_with_lock(db, event_date=args.event_date, slug=args.slug)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    if args.metar_pws_refresh_once:
        result = _refresh_metar_pws_once_with_lock(db, event_date=args.event_date, slug=args.slug)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    if args.market_prefetch_once:
        result = _market_prefetch_once_with_lock(db, event_date=args.event_date, slug=args.slug)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    if args.once or not args.loop:
        try:
            result = poll_once(db, event_date=args.event_date, slug=args.slug)
        except Exception as exc:
            result = {
                "ok": False,
                "event_date": _normalize_date(args.event_date) or _chengdu_today(),
                "status": "poll_once_failed",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        result = dict(result)
        result["clob_order_service_pending_reconcile"] = _maybe_reconcile_clob_order_service_pending(
            db,
            event_date=args.event_date,
            slug=args.slug,
        )
        _write_awc_source_health(result)
        _write_posterior_target_latency_trace(result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    reload_on_code_change = _truthy(
        os.environ.get("PM_HARD_SOURCE_DIRECT_RELOAD_ON_CODE_CHANGE"),
        True,
    )
    code_snapshot = _code_reload_snapshot() if reload_on_code_change else {}
    while True:
        _reap_finished_children()
        if reload_on_code_change:
            changed, changed_path = _code_reload_changed(code_snapshot)
            if changed:
                _log(
                    json.dumps(
                        {
                            "ok": True,
                            "status": "code_changed_exit_for_launchd_restart",
                            "changed_path": changed_path,
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                    )
                )
                return 75
        try:
            result = poll_once(
                db,
                event_date=args.event_date,
                slug=args.slug,
                background_wu_twc=_background_wu_twc_refresh_enabled(),
            )
        except Exception as exc:
            result = {
                "ok": False,
                "event_date": _normalize_date(args.event_date) or _chengdu_today(),
                "status": "poll_once_failed",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        reaped = _reap_finished_children()
        if reaped:
            result = dict(result)
            result["reaped_children"] = reaped
        else:
            result = dict(result)
        result["clob_order_service_pending_reconcile"] = _maybe_reconcile_clob_order_service_pending(
            db,
            event_date=args.event_date,
            slug=args.slug,
        )
        _write_awc_source_health(result)
        _write_posterior_target_latency_trace(result)
        _log(json.dumps(result, ensure_ascii=True, sort_keys=True))
        time.sleep(_near_peak_interval(db, event_date=args.event_date, slug=args.slug))


if __name__ == "__main__":
    raise SystemExit(main())
