"""Resident, source-injected P10 shadow service.

The service intentionally begins *after* acquisition and raw archival.  It
keeps one prepared ``ResidentP10ShadowHost`` alive across AMSC bundles so the
hot path does not reconstruct a host, bootstrap a prior, refresh a forecast,
or create a per-event subprocess.  Network polling and any scheduler remain
outside this module and must deliver the immutable bundle through the source
adapter contract.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Callable, Mapping

from amsc_p10_shadow_source_adapter import dispatch_amsc_p10_shadow_bundle
from resident_p10_shadow_host import ResidentP10ShadowHost


SCHEMA_VERSION = "resident_p10_shadow_service_v1"


class ResidentP10ShadowService:
    """Single-target resident service over one isolated prepared cycle."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        prepared_cycle_id: str,
        timestamp_observer: Callable[[], str] | None = None,
    ) -> None:
        self._host = ResidentP10ShadowHost(
            conn,
            prepared_cycle_id=prepared_cycle_id,
            timestamp_observer=timestamp_observer,
        )
        self._lock = threading.Lock()
        self._bundle_dispatch_count = 0
        self._arrival_dispatch_count = 0

    @property
    def current_checkpoint(self) -> dict[str, Any]:
        return self._host.current_checkpoint

    def readiness(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "ok": self._host.readiness().get("ok") is True,
            "host_readiness": self._host.readiness(),
            "bundle_dispatch_count": self._bundle_dispatch_count,
            "arrival_dispatch_count": self._arrival_dispatch_count,
            "shadow_only": True,
            "places_orders": False,
            "boundary": {
                "resident_host_reused_across_bundles": True,
                "network_acquisition": False,
                "scheduler_or_watcher": False,
                "generic_task_queue": False,
                "subprocess_per_event": False,
                "production_archive_write": False,
                "clob": False,
            },
        }

    def process_amsc_bundle(self, *, amsc_observation_bundle: Mapping[str, Any]) -> dict[str, Any]:
        """Synchronously deliver one already archived bundle to the resident host.

        A service-level lock prevents two batches from interleaving their
        canonical arrival order.  The lower-level journal owns physical
        deduplication and original-T1 reuse; this service never invents an
        arrival, schedules a retry, or replaces a prewarmed dependency.
        """

        if not self._lock.acquire(blocking=False):
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "resident_p10_shadow_service_concurrent_bundle_blocked",
                "shadow_only": True,
                "places_orders": False,
            }
        try:
            ready = self._host.readiness()
            if ready.get("ok") is not True:
                return {
                    "schema": SCHEMA_VERSION,
                    "ok": False,
                    "state": "blocked",
                    "reason": "resident_p10_shadow_service_not_ready",
                    "host_readiness": ready,
                    "shadow_only": True,
                    "places_orders": False,
                }
            dispatch = dispatch_amsc_p10_shadow_bundle(
                amsc_observation_bundle,
                host=self._host,
            )
            self._bundle_dispatch_count += 1
            self._arrival_dispatch_count += int(dispatch.get("dispatch_count") or 0)
            return {
                "schema": SCHEMA_VERSION,
                "ok": dispatch.get("ok") is True,
                "state": str(dispatch.get("state") or ""),
                "bundle_dispatch": dispatch,
                "current_checkpoint_id": str(self._host.current_checkpoint.get("posterior_checkpoint_id") or ""),
                "bundle_dispatch_count": self._bundle_dispatch_count,
                "arrival_dispatch_count": self._arrival_dispatch_count,
                "shadow_only": True,
                "places_orders": False,
                "boundary": {
                    "resident_host_reused_across_bundles": True,
                    "network_acquisition": False,
                    "scheduler_or_watcher": False,
                    "generic_task_queue": False,
                    "subprocess_per_event": False,
                    "production_archive_write": False,
                    "clob": False,
                },
            }
        finally:
            self._lock.release()


__all__ = ["SCHEMA_VERSION", "ResidentP10ShadowService"]
