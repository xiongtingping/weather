"""Legacy research-only cooldown receipt for historical Open-Meteo probes.

This module is intentionally outside the production source catalog and may not
be imported by live weak likelihood, prewarm, posterior, decision, or order
paths.  It remains only so already-isolated historical research collectors do
not turn a 429 into retry traffic while their legacy evidence is retained.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def request_gate(*, state_path: Path, now: str) -> dict[str, Any]:
    current = _parse_utc(now) or datetime.now(timezone.utc)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    deadline = _parse_utc(state.get("retry_not_before") if isinstance(state, Mapping) else "")
    if deadline is not None and current < deadline:
        return {"ok": False, "reason": "open_meteo_rate_limit_cooldown_active", "retry_not_before": _iso(deadline)}
    return {"ok": True, "reason": "no_active_cooldown"}


def record_rate_limit(
    *, state_path: Path, observed_at: str, source: str, retry_after_seconds: Any = None, http_status: int = 429
) -> dict[str, Any]:
    observed = _parse_utc(observed_at) or datetime.now(timezone.utc)
    try:
        seconds = max(1, min(int(float(retry_after_seconds)), 3600))
    except (TypeError, ValueError):
        seconds = 300
    deadline = observed + timedelta(seconds=seconds)
    value = {
        "schema": "legacy_open_meteo_research_cooldown_v1",
        "scope": "legacy_research_only_not_production_source",
        "observed_at": _iso(observed),
        "retry_not_before": _iso(deadline),
        "source": str(source or ""),
        "http_status": int(http_status),
        "boundary": {"stores_prediction": False, "writes_posterior": False, "places_orders": False},
    }
    _atomic_json(state_path, value)
    return value
