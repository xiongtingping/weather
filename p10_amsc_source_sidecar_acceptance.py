"""Read-only acceptance audit for the direct AMSC P10 shadow source sidecar."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from amsc_p10_shadow_bundle_loader import FORWARD_NEW_ONLY, load_amsc_p10_shadow_bundle
from amsc_p10_shadow_source_adapter import build_amsc_p10_shadow_arrival_candidates


SCHEMA_VERSION = "p10_amsc_source_sidecar_acceptance_audit_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def audit_p10_amsc_source_sidecar(
    *,
    raw_archive_dir: Path,
    inbox_dir: Path,
    status_path: Path,
) -> dict[str, Any]:
    """Validate every immutable inbox bundle without generating an arrival."""

    files = sorted(path for path in inbox_dir.glob("*.json") if path.is_file()) if inbox_dir.is_dir() else []
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for path in files:
        payload = _load_json(path)
        loaded = load_amsc_p10_shadow_bundle(
            payload,
            raw_archive_dir=raw_archive_dir,
            mode=FORWARD_NEW_ONLY,
        )
        selection = (
            build_amsc_p10_shadow_arrival_candidates(loaded.get("bundle") or {})
            if loaded.get("ok") is True
            else {}
        )
        if loaded.get("ok") is not True or selection.get("ok") is not True or not selection.get("candidate_count"):
            rejected.append(
                {
                    "file": path.name,
                    "loader_blockers": list(loaded.get("blockers") or []),
                    "selection_blockers": list(selection.get("blockers") or []),
                    "candidate_count": int(selection.get("candidate_count") or 0),
                }
            )
            continue
        accepted.append(
            {
                "file": path.name,
                "manifest_ref": _text((loaded.get("manifest") or {}).get("manifest_ref")),
                "manifest_sha256": _text((loaded.get("manifest") or {}).get("manifest_sha256")),
                "candidate_count": int(selection.get("candidate_count") or 0),
                "arrival_candidates": [
                    {
                        "source_observation_id": _text(row.get("source_observation_id")),
                        "report_type": _text(row.get("report_type")),
                        "first_transport_seen_at": _text(row.get("first_transport_seen_at")),
                        "first_actionable_seen_at": _text(row.get("first_actionable_seen_at")),
                    }
                    for row in selection.get("arrival_candidates") or []
                    if isinstance(row, Mapping)
                ],
            }
        )
    archive_root = str(raw_archive_dir.expanduser().resolve())
    status = _load_json(status_path) if status_path.is_file() else {}
    status_archive_root = _text(status.get("raw_archive_dir"))
    status_for_current_raw_archive = bool(status) and status_archive_root == archive_root
    status_binding_state = (
        "matched"
        if status_for_current_raw_archive
        else "missing"
        if not status
        else "missing_raw_archive_identity"
        if not status_archive_root
        else "mismatched_raw_archive"
    )
    blockers: list[str] = []
    if rejected:
        blockers.append("p10_amsc_source_sidecar_invalid_inbox_bundle_present")
    forward_evidence_ready = bool(accepted)
    state = "forward_evidence_ready" if forward_evidence_ready and not blockers else "waiting_first_forward_capture" if not blockers else "blocked"
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": state,
        "blockers": blockers,
        "forward_evidence_ready": forward_evidence_ready,
        "accepted_bundle_count": len(accepted),
        "accepted_bundles": accepted[-20:],
        "rejected_bundles": rejected,
        # A legacy/raw-root-mismatched status is useful diagnostic evidence,
        # but is not this sidecar's transport receipt and must never appear as
        # current P10 source state.
        "latest_sidecar_status": status if status_for_current_raw_archive else {},
        "unbound_latest_sidecar_status": status if status and not status_for_current_raw_archive else {},
        "latest_sidecar_status_for_current_raw_archive": status_for_current_raw_archive,
        "latest_sidecar_status_binding_state": status_binding_state,
        "expected_raw_archive_dir": archive_root,
        "latest_sidecar_status_raw_archive_dir": status_archive_root,
        "boundary": {
            "read_only": True,
            "network_acquisition": False,
            "writes_archive_db": False,
            "writes_shadow_db": False,
            "creates_arrival": False,
            "places_orders": False,
            "t1_synthesized": False,
            "status_bound_to_raw_archive": True,
        },
    }


__all__ = ["SCHEMA_VERSION", "audit_p10_amsc_source_sidecar"]
