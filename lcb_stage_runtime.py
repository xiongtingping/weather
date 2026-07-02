from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import os
from typing import Any, Dict, Optional


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_stage_map(raw: Any) -> Dict[str, float]:
    out: Dict[str, float] = {}
    text = str(raw or "").strip()
    if not text:
        return out
    for part in text.replace(";", ",").split(","):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, val = item.split("=", 1)
        stage = key.strip().lower().replace("-", "_")
        num = _to_float(val)
        if stage and num is not None and num > 0:
            out[stage] = float(num)
    return out


def lcb_stage_for_days(
    days_to_event: Any,
    *,
    decision_ts: Any = None,
    tz_name: Optional[str] = None,
) -> str:
    days = _to_float(days_to_event)
    if days is None:
        return "unknown"
    if days >= 2.0:
        return "d2_plus"
    if days >= 1.0:
        return "d1"
    hour: Optional[int] = None
    if decision_ts is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(decision_ts, datetime):
        dt = decision_ts
    else:
        try:
            dt = datetime.fromisoformat(str(decision_ts).replace("Z", "+00:00"))
        except Exception:
            dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if tz_name:
        try:
            dt = dt.astimezone(ZoneInfo(str(tz_name)))
        except Exception:
            dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    try:
        hour = int(dt.hour)
    except Exception:
        hour = None
    if hour is not None and hour >= 12:
        return "d0_afternoon"
    return "d0_morning"


def resolve_lcb_stage_z(
    env_key: str,
    *,
    default: float,
    days_to_event: Any = None,
    decision_ts: Any = None,
    tz_name: Optional[str] = None,
    stage_defaults: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    base = _to_float(os.environ.get(env_key))
    if base is None or base <= 0:
        base = float(default)
    stage = lcb_stage_for_days(days_to_event, decision_ts=decision_ts, tz_name=tz_name)
    enabled = _env_bool("PM_LCB_STAGE_Z_ENABLED", True)
    if not enabled:
        return {"z": float(base), "stage": stage, "source": f"{env_key}:base_disabled"}

    stage_norm = str(stage or "unknown").lower()
    direct_key = f"{env_key}_{stage_norm.upper()}"
    direct = _to_float(os.environ.get(direct_key))
    if direct is not None and direct > 0:
        return {"z": float(direct), "stage": stage_norm, "source": direct_key}

    stage_map = _parse_stage_map(os.environ.get(f"{env_key}_BY_STAGE"))
    if stage_norm in stage_map:
        return {"z": float(stage_map[stage_norm]), "stage": stage_norm, "source": f"{env_key}_BY_STAGE"}
    if stage_norm.startswith("d0_") and "d0" in stage_map:
        return {"z": float(stage_map["d0"]), "stage": stage_norm, "source": f"{env_key}_BY_STAGE:d0"}

    default_enabled = _env_bool("PM_LCB_STAGE_Z_DEFAULTS_ENABLED", True)
    defaults = dict(stage_defaults or {})
    if default_enabled and defaults:
        if stage_norm in defaults:
            return {"z": float(defaults[stage_norm]), "stage": stage_norm, "source": "stage_default"}
        if stage_norm.startswith("d0_") and "d0" in defaults:
            return {"z": float(defaults["d0"]), "stage": stage_norm, "source": "stage_default:d0"}

    return {"z": float(base), "stage": stage_norm, "source": f"{env_key}:base"}


DEFAULT_ENTRY_STAGE_Z = {
    "d2_plus": 1.64,
    "d1": 1.28,
    "d0_morning": 1.0,
    "d0_afternoon": 0.8,
    "unknown": 1.28,
}


DEFAULT_BAYES_STAGE_Z = {
    "d2_plus": 1.64,
    "d1": 1.28,
    "d0_morning": 1.0,
    "d0_afternoon": 0.8,
    "unknown": 1.28,
}
