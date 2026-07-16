"""One controlled in-process AMSC bundle → resident P10 shadow invocation.

This is the only composition point between a prepared isolated shadow host and
the AMSC bundle adapter.  It deliberately accepts data that has already been
acquired and raw-archived; no fetch, scheduler, subprocess, production DB, or
order capability exists here.  Deployment may wrap this runner later, but a
wrapper must not replace its per-arrival/T1/journal boundaries.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Mapping

from amsc_p10_shadow_source_adapter import dispatch_amsc_p10_shadow_bundle
from resident_p10_shadow_host import ResidentP10ShadowHost


SCHEMA_VERSION = "resident_p10_shadow_bundle_runner_v1"


def run_resident_p10_shadow_bundle(
    conn: sqlite3.Connection,
    *,
    prepared_cycle_id: str,
    amsc_observation_bundle: Mapping[str, Any],
    timestamp_observer: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Run already-received individual primary arrivals through P1→P9 shadow.

    ``ResidentP10ShadowHost`` loads the frozen prepared-cycle record and proves
    isolation/prewarm/checkpoint continuity;
    ``dispatch_amsc_p10_shadow_bundle`` proves the source batch cannot become
    an arrival.  Keeping the construction here makes it impossible for a
    caller to accidentally feed the entire poll result to the host.
    """

    host = ResidentP10ShadowHost(
        conn,
        prepared_cycle_id=prepared_cycle_id,
        timestamp_observer=timestamp_observer,
    )
    result = dispatch_amsc_p10_shadow_bundle(amsc_observation_bundle, host=host)
    return {
        "schema": SCHEMA_VERSION,
        "ok": result.get("ok") is True,
        "state": str(result.get("state") or ""),
        "host_readiness": host.readiness(),
        "bundle_dispatch": result,
        "shadow_only": True,
        "places_orders": False,
        "boundary": {
            "requires_persisted_prepared_cycle": True,
            "requires_isolated_shadow_db": True,
            "network_acquisition": False,
            "scheduler_or_watcher": False,
            "generic_task_queue": False,
            "subprocess_per_event": False,
            "production_archive_write": False,
            "clob": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "run_resident_p10_shadow_bundle"]
