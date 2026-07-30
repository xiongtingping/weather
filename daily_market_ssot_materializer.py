"""Retired legacy market-SSOT materializer.

The old implementation could repoint ``event_fact_latest`` from a resident
worker.  That path is retired under the target architecture.  Historical
callers may still use ``_days`` for offline reports, but production invocation
fails closed and performs no database, file, network, or order operation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "daily_market_ssot_materializer_retired_v2"
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def _days(now: datetime) -> list[str]:
    local_day = now.astimezone(LOCAL_ZONE).date()
    return [(local_day + timedelta(days=offset)).isoformat() for offset in (0, 1)]


class DailyMarketSsotMaterializer:
    """Compatibility shell for offline callers; never starts a writer."""

    def __init__(self, db_path: Any, **_: Any) -> None:
        self.db_path = db_path

    def run(self, **_: Any) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_retired_legacy_entry",
            "reason": "legacy_market_ssot_materializer_retired",
            "writes_archive_db": False,
            "writes_event_fact_latest": False,
            "places_orders": False,
        }


__all__ = ["DailyMarketSsotMaterializer", "SCHEMA_VERSION", "_days"]
