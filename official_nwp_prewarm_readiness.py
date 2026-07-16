"""Read-only readiness for a frozen four-provider official-NWP generation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from official_nwp_multimodel_prewarm import generation_reference, validate_generation_reference


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def audit_official_nwp_prewarm_readiness(
    *,
    cache_path: Path,
    catalog_path: Path,
    as_of: str | datetime | None = None,
    max_cache_age_sec: int = 1200,
) -> dict[str, Any]:
    now = as_of.astimezone(timezone.utc) if isinstance(as_of, datetime) and as_of.tzinfo else _parse_utc(as_of)
    now = now or datetime.now(timezone.utc)
    try:
        import json

        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    reference = generation_reference(cache if isinstance(cache, dict) else {})
    validation = validate_generation_reference(reference, catalog_path=catalog_path)
    blockers = list(validation.get("issues") or [])
    completed = _parse_utc((cache if isinstance(cache, dict) else {}).get("response_completed_at"))
    age_sec: float | None = None
    if completed is None:
        blockers.append("official_nwp_prewarm_response_completed_at_missing")
    else:
        age_sec = round((now - completed).total_seconds(), 3)
        if age_sec < 0 or age_sec > max(1, int(max_cache_age_sec)):
            blockers.append("official_nwp_prewarm_cache_stale_or_future")
    return {
        "schema": "official_nwp_prewarm_readiness_v1",
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "cache_path": str(cache_path),
        "decision_as_of": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "response_completed_at": completed.isoformat(timespec="milliseconds").replace("+00:00", "Z") if completed else "",
        "response_age_sec": age_sec,
        "generation_validation": validation,
        "boundary": {"read_only": True, "network_acquisition": False, "writes_posterior": False, "places_orders": False},
    }


__all__ = ["audit_official_nwp_prewarm_readiness"]
