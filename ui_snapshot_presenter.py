"""UI snapshot presenter contract for the weather dashboard.

Runtime assembly currently lives in ``ui/server.py``. This module holds the
machine-readable presenter contract so audits can verify the single snapshot
surface without moving server code or introducing a second UI data path.
"""

from __future__ import annotations

from typing import Any, Mapping


SCHEMA_VERSION = "ui_snapshot_presenter_contract_v1"

UI_SNAPSHOT_SECTION_ORDER = (
    "facts",
    "source_to_posterior",
    "posterior",
    "decisions",
    "execution",
    "review",
)


def ui_snapshot_presenter_contract() -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "owner": "ui/server.py /api/ui_snapshot",
        "runtime_note": "runtime assembly remains in ui/server.py; this module defines the audit contract",
        "required_sections": list(UI_SNAPSHOT_SECTION_ORDER),
        "required_top_level_fields": [
            "snapshot_id",
            "snapshot_etag",
            "snapshot_freshness_state",
            "panel_freshness",
            "ui_sections",
            "facts",
        ],
        "boundaries": {
            "frontend_recompute_allowed": False,
            "fetches_external_weather_sources": False,
            "writes_archive_db": False,
            "places_orders": False,
        },
    }


def summarize_ui_snapshot_presenter(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    sections = snapshot.get("ui_sections") if isinstance(snapshot.get("ui_sections"), Mapping) else {}
    panel_freshness = (
        snapshot.get("panel_freshness") if isinstance(snapshot.get("panel_freshness"), Mapping) else {}
    )
    missing_sections = [key for key in UI_SNAPSHOT_SECTION_ORDER if key not in sections and key not in panel_freshness]
    return {
        "schema": "ui_snapshot_presenter_summary_v1",
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "snapshot_freshness_state": str(snapshot.get("snapshot_freshness_state") or ""),
        "section_order": list(UI_SNAPSHOT_SECTION_ORDER),
        "missing_sections": missing_sections,
        "ok": not missing_sections,
    }
